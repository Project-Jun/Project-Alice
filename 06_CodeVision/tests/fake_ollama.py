#!/usr/bin/env python3
"""A fake Ollama server for CodeVision tests and screen checks. No model, no GPU, no network.

It answers /api/version, /api/tags, /api/ps, /api/chat and /api/generate the way Ollama
0.32 does. Answers are made from the prompt itself, so the same prompt always gets the
same answer: plain Korean 해요체 sentences, and "f" reuses the signatures listed in the
prompt (or in the "format" schema). Only the standard library is used, and it only ever
listens on a loopback address.

Run it on its own (stop it with Ctrl+C or POST /__fake/stop):

    python tests/fake_ollama.py --port 11500 [--cold SECONDS] [--delay SECONDS]
        [--models qwen2.5:7b,gemma3:4b] [--fail none|500|badjson|slow|drop]

CodeVision's "Ollama 켜기" can start it instead of the real Ollama:

    set CODEVISION_OLLAMA_EXE=C:\\...\\tests\\fake_ollama.py
    CodeVision then runs "python fake_ollama.py serve". Serve mode takes its address
    from OLLAMA_HOST (CodeVision sets it from the configured base_url) and its options
    from FAKE_OLLAMA_MODELS, FAKE_OLLAMA_COLD, FAKE_OLLAMA_DELAY, FAKE_OLLAMA_FAIL,
    FAKE_OLLAMA_START_DELAY, FAKE_OLLAMA_MAX_LIFE, FAKE_OLLAMA_PID_FILE, FAKE_OLLAMA_LOG.

From a test:

    from fake_ollama import FakeOllama
    with FakeOllama(models=["qwen2.5:7b"]) as fake:
        ...  # fake.url, fake.chats(), fake.prompts(), fake.hold(), fake.release()

Control endpoints (no token; only reachable from this computer):
    GET /__fake/calls       every request so far: [{"method", "path", "body", "at"}]
    POST /__fake/reset      forget calls and loaded models, answer normally again
    POST /__fake/set        {"fail": "500", "fail_count": 1, "delay": 0.5, "cold": 2, "models": [...]}
    POST /__fake/stop       stop the server (serve mode and --port mode then exit)

Fail modes: "500" answers HTTP 500 like a crashed model runner, "badjson" answers 200 with
broken JSON text, "slow" waits --slow seconds (default 300) before answering, "drop" closes
the connection without any reply. With fail_count > 0 only that many answers fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.32.6"
DEFAULT_MODELS = ("qwen2.5:7b", "gemma3:4b")
REAL_OLLAMA_PORT = 11434
FAIL_MODES = ("none", "500", "badjson", "slow", "drop")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 8_000_000

# Installed-model facts as Ollama reports them (sizes of the real downloads).
CATALOG = {
    "qwen2.5:7b": {"size": 4683087332, "family": "qwen2", "params": "7.6B", "quant": "Q4_K_M"},
    "gemma3:4b": {"size": 3338801804, "family": "gemma3", "params": "4.3B", "quant": "Q4_K_M"},
    "llama3.2:3b": {"size": 2019393189, "family": "llama", "params": "3.2B", "quant": "Q4_K_M"},
    "nomic-embed-text:latest": {"size": 274302450, "family": "nomic-bert", "params": "137M", "quant": "F16"},
}
MODIFIED_AT = "2026-08-07T12:53:00.1234567+09:00"


def model_entry(name: str) -> dict:
    """One item of /api/tags, shaped like Ollama's."""
    info = CATALOG.get(name) or {"size": 1_000_000_000, "family": name.split(":")[0] or "unknown",
                                 "params": "1.0B", "quant": "Q4_K_M"}
    return {
        "name": name,
        "model": name,
        "modified_at": MODIFIED_AT,
        "size": info["size"],
        "digest": hashlib.sha256(name.encode("utf-8")).hexdigest(),
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": info["family"],
            "families": [info["family"]],
            "parameter_size": info["params"],
            "quantization_level": info["quant"],
        },
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# --- reading the prompt -------------------------------------------------------

_PATH_LINE = re.compile(r"^\s*(?:[-*]\s*)?(파일|폴더)\s*경로\s*[:：]\s*(.*?)\s*$", re.M)
_SIG_HEADER = re.compile(r"^\s*(?:[-*]\s*)?(?:주요\s*함수|주요\s*함수와\s*클래스|함수\s*목록)[^:：\n]*[:：]\s*(.*?)\s*$")
_CHILD_HEADER = re.compile(r"안에\s*든\s*것|들어\s*있는\s*것|자식\s*항목")
_BULLET = re.compile(r"^(?:[-*•]|\d+[.)])\s+(.*)$")
_NONE_WORDS = {"", "없음", "(없음)", "없어요", "none", "-"}


def _split_top_level(text: str) -> list[str]:
    """Split "a(x, y), b(), class C" on commas that are not inside brackets."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip().strip("`").strip() for p in parts if p.strip().strip("`").strip()]


def _cut_description(item: str) -> str:
    """"addTodo(title): 할 일을 추가해요" -> "addTodo(title)" (only a ':' or dash outside brackets)."""
    depth = 0
    for i, ch in enumerate(item):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and (item.startswith(": ", i) or item.startswith(" — ", i) or item.startswith(" - ", i)):
            return item[:i].strip().strip("`").strip()
    return item.strip().strip("`").strip()


def find_signatures(text: str) -> list[str]:
    """The signatures the prompt lists under 주요 함수 (inline list, JSON list or bullet lines)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _SIG_HEADER.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.lower() in _NONE_WORDS and rest:
            return []
        if rest.startswith("["):
            try:
                items = json.loads(rest)
            except ValueError:
                items = None
            if isinstance(items, list):
                return [str(x[0] if isinstance(x, list) and x else x) for x in items if x]
        if rest:
            return _split_top_level(rest)
        sigs = []
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s:
                if sigs:
                    break
                continue
            bm = _BULLET.match(s)
            if not bm:
                break
            item = _cut_description(bm.group(1))
            if item and item.lower() not in _NONE_WORDS:
                sigs.append(item)
        return sigs
    return []


_FRAME = re.compile(r"^\s*틀\s*[:：]\s*(\{.*\})\s*$", re.M)


def _frame_signatures(text: str) -> list[str] | None:
    """The "f" keys of an answer frame line such as  틀: {"s": "", "d": "", "f": {"run()": ""}}."""
    for m in _FRAME.finditer(text):
        try:
            frame = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(frame, dict) and isinstance(frame.get("f"), dict):
            return [str(k) for k in frame["f"]]
    return None


def _schema_signatures(schema) -> list[str] | None:
    """Signatures spelled out in the format schema itself ("f": {"properties": {...}})."""
    if not isinstance(schema, dict):
        return None
    f = (schema.get("properties") or {}).get("f")
    if isinstance(f, dict) and isinstance(f.get("properties"), dict) and f["properties"]:
        return list(f["properties"])
    return None


def read_prompt(text: str, schema=None) -> dict:
    """What the fake needs from a CodeVision prompt: kind, path, name, signatures, child count."""
    m = _PATH_LINE.search(text)
    kind = "folder" if m and m.group(1) == "폴더" else "file"
    path = m.group(2).strip() if m else ""
    if kind == "folder" and (not path or path.startswith("(")):
        name = "프로젝트 맨 위"
    else:
        name = path.rstrip("/").rsplit("/", 1)[-1] or "이"
    sigs = _schema_signatures(schema)
    if sigs is None and kind == "file":
        sigs = find_signatures(text) or _frame_signatures(text) or []
    sigs = sigs or []
    children = 0
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _CHILD_HEADER.search(line):
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if not _BULLET.match(s):
                    break
                if "비어 있" not in s:
                    children += 1
            break
    return {"kind": kind, "path": path, "name": name, "signatures": sigs, "children": children}


def _bare(sig: str) -> str:
    name = sig.split("(", 1)[0].strip()
    for prefix in ("export default ", "export ", "async ", "def ", "function ", "class "):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip() or sig


def answer_for(text: str, schema=None) -> dict:
    """The fake model's answer for one prompt: {"s", "d", "f": {signature: text}} in 해요체."""
    info = read_prompt(text, schema)
    name = info["name"]
    if info["kind"] == "folder":
        n = info["children"]
        return {
            "s": f"{name} 폴더의 파일 모음",
            "d": f"{name} 폴더에는 항목 {n:,}개가 들어 있어요. 비슷한 일을 하는 파일을 모아 둔 곳이에요.",
            "f": {},
        }
    stem = name.rsplit(".", 1)[0] if "." in name.strip(".") else name
    sigs = info["signatures"]
    d = f"{name} 파일은 이 프로젝트에서 {stem} 일을 맡아요."
    if sigs:
        d += f" 주요 함수 {len(sigs):,}개로 일을 나눠서 해요."
    return {"s": f"{stem} 일을 맡은 파일", "d": d, "f": {sig: f"{_bare(sig)} 일을 해요." for sig in sigs}}


def _empty_for(schema):
    t = schema.get("type") if isinstance(schema, dict) else None
    return {"string": "", "array": [], "object": {}, "integer": 0, "number": 0, "boolean": False}.get(t, "")


def _fit(value, schema):
    t = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(value, dict):
        if t == "array":
            items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
            if items.get("type") == "object" and isinstance(items.get("properties"), dict) and len(items["properties"]) >= 2:
                first, second = list(items["properties"])[:2]
                return [{first: k, second: v} for k, v in value.items()]
            if items.get("type") == "string":
                return [f"{k}: {v}" for k, v in value.items()]
            return [[k, v] for k, v in value.items()]
        return dict(value)
    if t == "array":
        return [value] if value else []
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    return value


def shape_answer(answer: dict, schema) -> object:
    """Fit {"s", "d", "f"} to the JSON schema the client sent as "format" (other keys get empty values)."""
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return answer
    return {key: (_fit(answer[key], sub) if key in answer else _empty_for(sub))
            for key, sub in schema["properties"].items()}


# --- the server ----------------------------------------------------------------

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:  # skip the reverse DNS lookup of HTTPServer.server_bind
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Server6(_Server):
    address_family = socket.AF_INET6


class _Drop(Exception):
    """Close the connection without any reply."""


class FakeOllama:
    """A fake Ollama on 127.0.0.1. Thread-safe; settings can be changed while it runs."""

    def __init__(self, port: int = 0, *, models=DEFAULT_MODELS, cold: float = 0.0, delay: float = 0.0,
                 fail: str = "none", slow: float = 300.0, host: str = "127.0.0.1", version: str = VERSION,
                 log_path: str | None = None, allow_default_port: bool = False):
        if host not in LOOPBACK_HOSTS:
            raise ValueError("the fake Ollama only listens on 127.0.0.1, localhost or ::1")
        if port == REAL_OLLAMA_PORT and not allow_default_port:
            raise ValueError("port 11434 belongs to the real Ollama; pick another port")
        if fail not in FAIL_MODES:
            raise ValueError(f"fail must be one of {', '.join(FAIL_MODES)}")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = port
        self.models = list(models)
        self.cold = float(cold)
        self.delay = float(delay)
        self.fail = fail
        self.fail_count = 0  # > 0: only this many answers fail, then they are normal again
        self.slow = float(slow)
        self.version = version
        self.log_path = log_path
        self.loaded: dict[str, float] = {}  # model -> expiry (time.time())
        self.calls: list[dict] = []
        self.answered = 0
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._model_lock = threading.Lock()  # one answer at a time, like a single model runner
        self._gate = threading.Event()
        self._gate.set()
        self._stopped = threading.Event()
        self.httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    # --- life cycle ---

    def start(self) -> FakeOllama:
        server_class = _Server6 if ":" in self.host else _Server
        self.httpd = server_class((self.host, self.port), _Handler)
        self.httpd.fake = self
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="fake-ollama", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        self._gate.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def crash(self) -> None:
        """Like an Ollama that crashed: nothing listens any more, then answers in progress end with no reply."""
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        self._stopped.set()
        self._gate.set()

    def __enter__(self) -> FakeOllama:
        return self.start() if self.httpd is None else self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    # --- test helpers ---

    def hold(self) -> None:
        """Make answers wait (they are still recorded on arrival) until release()."""
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
            self.loaded.clear()
            self.answered = 0
            self.fail = "none"
            self.fail_count = 0
        self._gate.set()

    def chats(self) -> list[dict]:
        """Bodies of the /api/chat and /api/generate requests that asked for an answer."""
        with self._lock:
            return [c["body"] for c in self.calls if c["path"] in ("/api/chat", "/api/generate")
                    and isinstance(c["body"], dict) and _wants_answer(c["path"], c["body"])]

    def prompts(self) -> list[str]:
        """The user text of every answer request, in arrival order."""
        out = []
        for body in self.chats():
            if "messages" in body:
                users = [m.get("content", "") for m in body.get("messages") or []
                         if isinstance(m, dict) and m.get("role") == "user"]
                out.append(users[-1] if users else "")
            else:
                out.append(str(body.get("prompt", "")))
        return out

    def raw_requests(self) -> str:
        """Everything the clients sent, as one text (handy for "this secret was never sent" checks)."""
        with self._lock:
            return "\n".join(c.get("raw", "") for c in self.calls)

    def wait_for_chats(self, count: int, timeout: float = 10.0) -> bool:
        """Wait until at least `count` answer requests have arrived."""
        deadline = time.monotonic() + timeout
        with self._arrived:
            while len([c for c in self.calls if c.get("answer")]) < count:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._arrived.wait(left)
            return True

    def set(self, **changes) -> None:
        """Change settings while running: fail, fail_count, delay, cold, slow, models, version."""
        with self._lock:
            for key, value in changes.items():
                if key == "fail":
                    if value not in FAIL_MODES:
                        raise ValueError(f"fail must be one of {', '.join(FAIL_MODES)}")
                    self.fail = value
                elif key in ("delay", "cold", "slow"):
                    setattr(self, key, float(value))
                elif key == "fail_count":
                    self.fail_count = int(value)
                elif key == "models":
                    self.models = [str(m) for m in value]
                elif key == "version":
                    self.version = str(value)
                else:
                    raise ValueError(f"unknown setting: {key}")

    # --- request handling (called by _Handler) ---

    def _record(self, method: str, path: str, body, raw: str) -> None:
        entry = {"method": method, "path": path, "body": body, "at": time.time(), "raw": raw,
                 "answer": path in ("/api/chat", "/api/generate") and isinstance(body, dict)
                 and _wants_answer(path, body)}
        with self._arrived:
            self.calls.append(entry)
            self._arrived.notify_all()
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({k: v for k, v in entry.items() if k != "raw"}, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._stopped.wait(seconds)

    def _take_fail(self) -> str:
        with self._lock:
            mode = self.fail
            if mode != "none" and self.fail_count > 0:
                self.fail_count -= 1
                if self.fail_count == 0:
                    self.fail = "none"
            return mode

    def _expire(self) -> None:
        now = time.time()
        for name in [n for n, until in self.loaded.items() if until <= now]:
            del self.loaded[name]

    def tags(self) -> dict:
        with self._lock:
            return {"models": [model_entry(m) for m in self.models]}

    def ps(self) -> dict:
        with self._lock:
            self._expire()
            items = []
            for name, until in self.loaded.items():
                entry = model_entry(name)
                entry.pop("modified_at", None)
                entry["expires_at"] = datetime.fromtimestamp(until, timezone.utc).isoformat()
                entry["size_vram"] = entry["size"]
                entry["context_length"] = 8192
                items.append(entry)
            return {"models": items}

    def answer(self, path: str, body: dict) -> tuple[int, dict | list]:
        """Handle /api/chat or /api/generate. Returns (status, reply) or raises _Drop."""
        generate = path == "/api/generate"
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return 400, {"error": "model is required"}
        with self._lock:
            installed = model in self.models
        if not installed:
            return 404, {"error": f'model "{model}" not found, try pulling it first'}
        if not _wants_answer(path, body):
            unload = body.get("keep_alive") in (0, "0", "0s", "0m")
            with self._lock:
                if unload:
                    self.loaded.pop(model, None)
                else:
                    self.loaded[model] = time.time() + _keep_alive_seconds(body.get("keep_alive"))
            reply = {"model": model, "created_at": _now(), "done_reason": "unload" if unload else "load", "done": True}
            reply["response" if generate else "message"] = "" if generate else {"role": "assistant", "content": ""}
            return 200, reply
        while not self._gate.wait(0.05):  # held by the test
            if self._stopped.is_set():
                raise _Drop()
        with self._model_lock:
            mode = self._take_fail()
            if mode == "slow":
                self._sleep(self.slow)
            with self._lock:
                self._expire()
                cold = model not in self.loaded
            started = time.perf_counter()
            if cold:
                self._sleep(self.cold)
            load_s = time.perf_counter() - started
            self._sleep(self.delay)
            if self._stopped.is_set():
                raise _Drop()
            with self._lock:
                self.loaded[model] = time.time() + _keep_alive_seconds(body.get("keep_alive"))
                self.answered += 1
            if mode == "drop":
                raise _Drop()
            if mode == "500":
                return 500, {"error": "llama runner process has terminated: exit status 2"}
            text = _prompt_text(path, body)
            schema = body.get("format")
            if mode == "badjson":
                content = '{"s": "요약이에요", "d": "설명이 중간에서 끊'
            else:
                data = shape_answer(answer_for(text, schema if isinstance(schema, dict) else None),
                                    schema if isinstance(schema, dict) else None)
                content = json.dumps(data, ensure_ascii=False)
                if not schema:  # without "format" a model tends to wrap its JSON in a code block
                    content = "```json\n" + content + "\n```"
            total = time.perf_counter() - started
            reply = {
                "model": model,
                "created_at": _now(),
                "done_reason": "stop",
                "done": True,
                "total_duration": int(total * 1e9),
                "load_duration": int(load_s * 1e9),
                "prompt_eval_count": max(1, len(text) // 3),
                "prompt_eval_duration": 12_000_000,
                "eval_count": max(1, len(content) // 3),
                "eval_duration": max(1, int((total - load_s) * 1e9)),
            }
            if generate:
                reply["response"] = content
                reply["context"] = [1, 2, 3]
            else:
                reply["message"] = {"role": "assistant", "content": content}
            return 200, reply


def _wants_answer(path: str, body: dict) -> bool:
    if path == "/api/generate":
        return bool(str(body.get("prompt") or "").strip())
    return bool(body.get("messages"))


def _keep_alive_seconds(value) -> float:
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else 10 ** 9
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([smh]?)\s*", value)
        if m:
            n = float(m.group(1))
            if n < 0:
                return 10 ** 9
            return n * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    return 300.0  # Ollama's default: five minutes


def _prompt_text(path: str, body: dict) -> str:
    if path == "/api/generate":
        return str(body.get("prompt") or "")
    users = [m.get("content", "") for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") == "user"]
    return str(users[-1]) if users else ""


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-ollama"
    sys_version = ""
    timeout = 30

    def log_message(self, format, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("HEAD")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        fake: FakeOllama = self.server.fake
        path = self.path.split("?", 1)[0]
        raw = b""
        length = self.headers.get("Content-Length")
        if length and length.isdigit() and int(length) <= MAX_BODY:
            raw = self.rfile.read(int(length))
        text = raw.decode("utf-8", "replace")
        body = None
        if raw:
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        fake._record(method, path, body if body is not None else text, text)
        try:
            if path.startswith("/__fake/"):
                return self._control(method, path, body)
            if path == "/" and method in ("GET", "HEAD"):
                return self._send(200, b"Ollama is running", "text/plain; charset=utf-8")
            if path == "/api/version" and method in ("GET", "HEAD"):
                with fake._lock:
                    version = fake.version
                return self._json(200, {"version": version})
            if path == "/api/tags" and method in ("GET", "HEAD"):
                return self._json(200, fake.tags())
            if path == "/api/ps" and method in ("GET", "HEAD"):
                return self._json(200, fake.ps())
            if path in ("/api/chat", "/api/generate") and method == "POST":
                if raw and body is None:
                    return self._json(400, {"error": "invalid character looking for beginning of value"})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "missing request body"})
                status, reply = fake.answer(path, body)
                if status == 200 and body.get("stream", True) is not False:
                    return self._stream(path, reply)
                return self._json(status, reply)
            return self._send(404, b"404 page not found", "text/plain; charset=utf-8")
        except _Drop:
            self.close_connection = True
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def _control(self, method: str, path: str, body) -> None:
        fake: FakeOllama = self.server.fake
        if path == "/__fake/calls" and method == "GET":
            with fake._lock:
                calls = [{k: v for k, v in c.items() if k != "raw"} for c in fake.calls]
            return self._json(200, calls)
        if path == "/__fake/reset" and method == "POST":
            fake.reset()
            return self._json(200, {"ok": True})
        if path == "/__fake/set" and method == "POST":
            try:
                fake.set(**(body if isinstance(body, dict) else {}))
            except (TypeError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"ok": True})
        if path == "/__fake/stop" and method == "POST":
            self._json(200, {"ok": True})
            threading.Thread(target=fake.stop, daemon=True).start()
            return None
        return self._send(404, b"404 page not found", "text/plain; charset=utf-8")

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _stream(self, path: str, reply: dict) -> None:
        """Ollama streams by default: one JSON object per line, the last one with "done": true."""
        key = "response" if path == "/api/generate" else "message"
        content = reply[key] if key == "response" else reply[key]["content"]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for i in range(0, len(content), 12):
            piece = content[i:i + 12]
            chunk = {"model": reply["model"], "created_at": _now(), "done": False}
            chunk[key] = piece if key == "response" else {"role": "assistant", "content": piece}
            self.wfile.write((json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8"))
        last = dict(reply)
        last[key] = "" if key == "response" else {"role": "assistant", "content": ""}
        self.wfile.write((json.dumps(last, ensure_ascii=False) + "\n").encode("utf-8"))
        self.close_connection = True


# --- command line -------------------------------------------------------------------

def _host_port(value: str) -> tuple[str, int]:
    """Read OLLAMA_HOST the way Ollama does: "host:port", "http://host:port", ":port"."""
    text = value.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    elif text.count(":") == 1:
        host, port = text.split(":")
    else:
        host, port = text, ""
    host = host or "127.0.0.1"
    return host, int(port) if port else REAL_OLLAMA_PORT


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    serve = bool(argv) and argv[0] == "serve"
    if serve:
        argv = argv[1:]
    env = os.environ
    parser = argparse.ArgumentParser(prog="fake_ollama.py", description="A fake Ollama for CodeVision tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--models", default=env.get("FAKE_OLLAMA_MODELS", ",".join(DEFAULT_MODELS)) if serve
                        else ",".join(DEFAULT_MODELS), help="comma-separated model names ('' for none)")
    parser.add_argument("--cold", type=float, default=float(env.get("FAKE_OLLAMA_COLD", "0") if serve else 0),
                        help="seconds before the first answer of a model that is not loaded")
    parser.add_argument("--delay", type=float, default=float(env.get("FAKE_OLLAMA_DELAY", "0") if serve else 0),
                        help="seconds before every answer")
    parser.add_argument("--fail", choices=FAIL_MODES, default=env.get("FAKE_OLLAMA_FAIL", "none") if serve else "none")
    parser.add_argument("--slow", type=float, default=300.0, help="seconds a 'slow' answer takes")
    parser.add_argument("--start-delay", type=float,
                        default=float(env.get("FAKE_OLLAMA_START_DELAY", "0") if serve else 0),
                        help="seconds to wait before listening (like a slow start)")
    parser.add_argument("--max-life", type=float, default=float(env.get("FAKE_OLLAMA_MAX_LIFE", "3600")),
                        help="stop by itself after this many seconds (0: never)")
    parser.add_argument("--pid-file", default=env.get("FAKE_OLLAMA_PID_FILE") if serve else None)
    parser.add_argument("--log", default=env.get("FAKE_OLLAMA_LOG") if serve else None,
                        help="append every request as one JSON line to this file")
    parser.add_argument("--allow-default-port", action="store_true",
                        help="allow port 11434 (normally refused: it belongs to the real Ollama)")
    args = parser.parse_args(argv)

    host, port = args.host, args.port
    if serve and port is None:
        if not env.get("OLLAMA_HOST"):
            print("fake_ollama serve: set OLLAMA_HOST (for example 127.0.0.1:11500)", file=sys.stderr)
            return 2
        host, port = _host_port(env["OLLAMA_HOST"])
    if port is None:
        parser.error("--port is required")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.start_delay > 0:
        time.sleep(args.start_delay)
    try:
        fake = FakeOllama(port, models=models, cold=args.cold, delay=args.delay, fail=args.fail, slow=args.slow,
                          host=host, log_path=args.log, allow_default_port=args.allow_default_port).start()
    except (OSError, ValueError) as exc:
        print(f"fake_ollama: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return 1
    if args.pid_file:
        try:
            with open(args.pid_file, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass
    print(f"fake ollama {VERSION} on {fake.url} (models: {', '.join(models) or 'none'})", flush=True)
    deadline = time.monotonic() + args.max_life if args.max_life > 0 else None
    try:
        while not fake.stopped:
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
        if args.pid_file:
            try:
                os.remove(args.pid_file)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
