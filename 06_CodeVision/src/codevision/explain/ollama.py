"""The "local" provider: Ollama running on this computer, reached over HTTP with urllib.

AI traffic stays on this PC:
- the base URL may only name 127.0.0.1, localhost or ::1 (check_base_url);
- the connected peer must be a loopback address, whatever the name resolved to;
- proxies are never used and redirects are never followed.

Starting Ollama (POST /api/ai/start) only runs the installed program; nothing is
downloaded. CODEVISION_OLLAMA_EXE replaces that program (tests point it at a stub),
and CODEVISION_OLLAMA_URL replaces the configured base URL (tests point it at a fake).
"""
from __future__ import annotations

import functools
import http.client
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import messages as M
from . import prompts
from .base import AIError, Explainer, Explanation, FileContext, FolderContext

DEFAULT_PORT = 11434
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
CONNECT_TIMEOUT = 2.0    # seconds to reach Ollama at all
PROBE_TIMEOUT = 3.0      # seconds for /api/version and /api/tags to answer
ANSWER_TIMEOUT = 120.0   # seconds for one explanation; a cold model load alone takes about 30 s
KEEP_ALIVE = "10m"       # Ollama keeps the model loaded this long after the last answer
START_WAIT = 20.0        # seconds POST /api/ai/start waits for Ollama to answer
MAX_REPLY_BYTES = 4_000_000
EXE_ENV = "CODEVISION_OLLAMA_EXE"
URL_ENV = "CODEVISION_OLLAMA_URL"
_TCP_NOSYNRETRIES = 9  # Windows: report a refused connection at once instead of retrying for ~2 s

log = logging.getLogger(__name__)


# --- the address ------------------------------------------------------------------

def check_base_url(url) -> str:
    """The normalized Ollama address ("http://127.0.0.1:11434"). Raises ValueError (Korean) unless
    it is plain http on 127.0.0.1, localhost or ::1, with no user, path, query or fragment."""
    if not isinstance(url, str) or not url.strip() or len(url) > 200:
        raise ValueError(M.BAD_URL)
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise ValueError(M.BAD_URL) from None
    host = (parts.hostname or "").lower()
    if (parts.scheme.lower() != "http" or host not in LOOPBACK_HOSTS or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError(M.BAD_URL)
    return f"http://{'[::1]' if host == '::1' else host}:{port or DEFAULT_PORT}"


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


# --- HTTP with urllib, loopback only --------------------------------------------------

class NotLoopback(OSError):
    """The address resolved to, or connected to, something that is not this computer."""


class _LoopbackConnection(http.client.HTTPConnection):
    """An HTTP connection that only reaches loopback addresses, with its own connect timeout."""

    def __init__(self, host, port=None, *, connect_timeout: float = CONNECT_TIMEOUT, **kwargs):
        super().__init__(host, port, **kwargs)
        self.connect_timeout = connect_timeout

    def connect(self) -> None:
        last: OSError = OSError("no address")
        for family, socktype, proto, _, address in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
            if not _is_loopback(address[0]):
                last = NotLoopback(address[0])
                continue
            sock = socket.socket(family, socktype, proto)
            try:
                if os.name == "nt":
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, _TCP_NOSYNRETRIES, 1)
                    except OSError:
                        pass
                sock.settimeout(self.connect_timeout)
                sock.connect(address)
                if not _is_loopback(sock.getpeername()[0]):
                    raise NotLoopback(address[0])
                read_timeout = self.timeout if isinstance(self.timeout, (int, float)) else None
                sock.settimeout(read_timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                sock.close()
                last = exc
                continue
            self.sock = sock
            return
        raise last


class _LoopbackHandler(urllib.request.HTTPHandler):
    def __init__(self, connect_timeout: float):
        super().__init__()
        self._connect_timeout = connect_timeout

    def http_open(self, req):
        return self.do_open(functools.partial(_LoopbackConnection, connect_timeout=self._connect_timeout), req)


def _opener(connect_timeout: float) -> urllib.request.OpenerDirector:
    """http only, no proxy handler (so no proxy is ever used) and no redirect handler."""
    opener = urllib.request.OpenerDirector()
    for handler in (_LoopbackHandler(connect_timeout), urllib.request.UnknownHandler(),
                    urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


class OllamaError(Exception):
    """Talking to Ollama failed."""


class OllamaUnreachable(OllamaError):
    """Nothing answers at the address: Ollama is not running (or the address is not allowed)."""


class OllamaTimeout(OllamaError):
    """Ollama took longer than the answer timeout."""


class OllamaDisconnected(OllamaError):
    """Ollama closed the connection without a full reply (it stopped or crashed)."""


class OllamaBadReply(OllamaError):
    """The reply was not the JSON Ollama sends."""


class OllamaHTTPError(OllamaError):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status} {message}")
        self.status = status
        self.message = message


class OllamaClient:
    """Ollama's HTTP API: /api/version, /api/tags, /api/ps, /api/chat, and /api/generate to unload."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, connect_timeout: float = CONNECT_TIMEOUT):
        self.base_url = check_base_url(base_url)
        self._opener = _opener(connect_timeout)

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = PROBE_TIMEOUT) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "CodeVision"})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_REPLY_BYTES + 1)
                missing = resp.length  # bytes the reply promised (Content-Length) but never sent
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read(65536).decode("utf-8", "replace")
            except OSError:
                text = ""
            finally:
                exc.close()
            try:
                message = str(json.loads(text).get("error") or text)
            except (ValueError, AttributeError):
                message = text
            raise OllamaHTTPError(exc.code, message.strip()[:500]) from None
        except urllib.error.URLError as exc:  # could not connect (refused, timed out, not loopback)
            raise OllamaUnreachable(str(exc.reason)) from None
        except TimeoutError:
            raise OllamaTimeout() from None
        except (http.client.HTTPException, ConnectionError) as exc:
            raise OllamaDisconnected(type(exc).__name__) from None
        except OSError as exc:
            raise OllamaDisconnected(str(exc)) from None
        if len(raw) > MAX_REPLY_BYTES:
            raise OllamaBadReply("reply too large")
        if missing:  # the connection closed in the middle of the reply: Ollama stopped or crashed
            raise OllamaDisconnected("reply cut off")
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OllamaBadReply("not JSON") from None
        if not isinstance(reply, dict):
            raise OllamaBadReply("not an object")
        return reply

    def version(self, timeout: float = PROBE_TIMEOUT) -> str:
        return str(self._call("GET", "/api/version", timeout=timeout).get("version") or "")

    def is_up(self, timeout: float = PROBE_TIMEOUT) -> bool:
        try:
            self.version(timeout)
            return True
        except OllamaError:
            return False

    def tags(self, timeout: float = PROBE_TIMEOUT) -> list[dict]:
        """Installed models: [{"name", "size", "params"}] in Ollama's order."""
        out = []
        for item in self._call("GET", "/api/tags", timeout=timeout).get("models") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if not isinstance(name, str) or not name:
                continue
            size = item.get("size")
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            out.append({"name": name, "size": size if isinstance(size, int) and size >= 0 else 0,
                        "params": str(details.get("parameter_size") or "")})
        return out

    def loaded(self, timeout: float = PROBE_TIMEOUT) -> list[str]:
        """The models Ollama holds in memory right now (/api/ps)."""
        names = []
        for item in self._call("GET", "/api/ps", timeout=timeout).get("models") or []:
            name = (item.get("name") or item.get("model")) if isinstance(item, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
        return names

    def unload(self, model: str, timeout: float = PROBE_TIMEOUT) -> None:
        """Let go of a model now (no prompt and keep_alive 0), so its memory is free for other programs."""
        self._call("POST", "/api/generate", {"model": model, "keep_alive": 0, "stream": False}, timeout=timeout)

    def chat(self, model: str, system: str, user: str, timeout: float = ANSWER_TIMEOUT, schema: dict | None = None) -> str:
        """One non-streamed answer; returns the message text. schema: the answer's JSON schema
        (prompts.ANSWER_SCHEMA when None)."""
        body = {
            "model": model,
            "stream": False,
            "format": schema if schema is not None else prompts.ANSWER_SCHEMA,
            "options": prompts.MODEL_OPTIONS,
            "keep_alive": KEEP_ALIVE,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        reply = self._call("POST", "/api/chat", body, timeout=timeout)
        message = reply.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaBadReply("no message")
        return content


# --- the explainer ------------------------------------------------------------------------

def answer_schema(ctx: FileContext | FolderContext) -> dict:
    """The JSON schema the answer must follow ("format"): prompts.answer_schema(ctx) when prompts.py has one
    (for example one that allows only this file's own "f" keys), else prompts.ANSWER_SCHEMA."""
    make = getattr(prompts, "answer_schema", None)
    if callable(make):
        try:
            schema = make(ctx)
        except Exception:
            log.exception("prompts.answer_schema failed; the shared schema is used")
            schema = None
        if isinstance(schema, dict) and schema:
            return schema
    return prompts.ANSWER_SCHEMA


class OllamaExplainer(Explainer):
    """Explains one file or folder with a model in Ollama. Failures raise AIError with a contract code."""

    name = "local"

    def __init__(self, base_url: str = DEFAULT_BASE_URL, model: str = "", client: OllamaClient | None = None,
                 answer_timeout: float | None = None):
        self.client = client or OllamaClient(base_url)
        self.base_url = self.client.base_url
        self.model = model
        self.answer_timeout = answer_timeout

    def available(self) -> bool:
        try:
            names = [m["name"] for m in self.client.tags()]
        except OllamaError:
            return False
        return bool(self.model) and self.model in names

    def explain_file(self, ctx: FileContext) -> Explanation:
        return self._ask(prompts.file_prompt(ctx), ctx)

    def explain_folder(self, ctx: FolderContext) -> Explanation:
        return self._ask(prompts.folder_prompt(ctx), ctx)

    def _ask(self, prompt: str, ctx: FileContext | FolderContext) -> Explanation:
        if not self.model:
            raise AIError("no_model", M.NO_MODEL)
        timeout = self.answer_timeout if self.answer_timeout is not None else ANSWER_TIMEOUT
        try:
            text = self.client.chat(self.model, prompts.SYSTEM_PROMPT, prompt, timeout=timeout, schema=answer_schema(ctx))
        except OllamaUnreachable:
            raise AIError("not_running", M.NOT_RUNNING) from None
        except OllamaTimeout:
            raise AIError("timeout", M.TIMEOUT) from None
        except OllamaHTTPError as exc:
            if exc.status == 404 and "not found" in exc.message.lower():
                raise AIError("no_model", M.NO_CHOSEN_MODEL) from None
            log.warning("Ollama answered %s: %s", exc.status, exc.message)
            raise AIError("failed", M.FAILED) from None
        except OllamaBadReply as exc:
            log.warning("Ollama sent an odd reply: %s", exc)
            raise AIError("bad_answer", M.BAD_ANSWER) from None
        except OllamaDisconnected as exc:
            log.warning("Ollama closed the connection: %s", exc)
            raise AIError("failed", M.FAILED) from None
        try:
            exp = prompts.parse_response(text)
            if exp is not None:
                exp = prompts.postprocess(exp, ctx)
        except Exception:
            log.exception("reading the model answer failed")
            exp = None
        if not isinstance(exp, Explanation) or not (exp.s or exp.d or exp.f):
            raise AIError("bad_answer", M.BAD_ANSWER)
        return exp


# --- finding and starting the installed Ollama ---------------------------------------------

@dataclass
class OllamaInstall:
    command: list[str]  # what to run to start it
    path: Path          # the program file that was found


def _windows_candidates() -> list[Path]:
    found = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    on_path = shutil.which("ollama")
    if on_path:
        found.append(Path(on_path))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if base:
            found.append(Path(base) / "Ollama" / "ollama.exe")
    return found


def _posix_candidates() -> list[Path]:
    found = []
    on_path = shutil.which("ollama")
    if on_path:
        found.append(Path(on_path))
    for path in ("/usr/local/bin/ollama", "/usr/bin/ollama", "/opt/homebrew/bin/ollama",
                 "/Applications/Ollama.app/Contents/Resources/ollama"):
        found.append(Path(path))
    return found


def find_ollama() -> OllamaInstall | None:
    """The installed Ollama, or None. Every system runs the server alone, "ollama serve" (ollama.exe on
    Windows). The Windows tray app ("ollama app.exe") is never started: it opens a window of its own and
    can update itself. CODEVISION_OLLAMA_EXE replaces the program; a .py file there runs with this Python."""
    override = os.environ.get(EXE_ENV)
    if override:
        path = Path(override)
        if not path.is_file():
            return None
        if path.suffix.lower() == ".py":
            return OllamaInstall([sys.executable, str(path), "serve"], path)
        return OllamaInstall([str(path), "serve"], path)
    if os.name == "nt":
        for exe in _windows_candidates():
            if exe.is_file():
                return OllamaInstall([str(exe), "serve"], exe)
        return None
    for exe in _posix_candidates():
        if exe.is_file() and os.access(exe, os.X_OK):
            return OllamaInstall([str(exe), "serve"], exe)
    return None


def launch(install: OllamaInstall, base_url: str) -> subprocess.Popen:
    """Start Ollama on its own, with no window, and return the process. Raises OSError.

    On Windows "ollama serve" gets a console of its own that has no window (CREATE_NO_WINDOW), in its own
    process group. The model runner it starts (llama-server.exe in Ollama 0.32) shares that hidden console,
    so it opens no window either; under a process with no console at all (DETACHED_PROCESS) a console
    program it starts could get a console window of its own. Ollama keeps running when CodeVision ends,
    like an Ollama the user started; the models CodeVision used are let go of then (AIService.close)."""
    env = dict(os.environ)
    base_url = check_base_url(base_url)
    if base_url != DEFAULT_BASE_URL or "OLLAMA_HOST" not in env:
        env["OLLAMA_HOST"] = urllib.parse.urlsplit(base_url).netloc  # serve where we will look
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True, "env": env, "cwd": str(install.path.parent)}
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = info
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(install.command, **kwargs)
