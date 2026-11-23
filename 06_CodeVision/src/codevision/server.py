"""Local web server for CodeVision (standard library only).

Security rules from docs/architecture.md section 6: listen on 127.0.0.1 only,
reject foreign Host headers (DNS rebinding), require the per-run token on
/api/*, and serve only a fixed list of static files.

Every reply, errors included, is JSON or a whitelisted file with the same
security headers. Wrong methods get 405, bodies are capped at MAX_BODY, and
a failing analysis answers with a Korean 500 while its traceback goes to
stderr. Each request runs in its own daemon thread, so a slow analysis never
holds up the others.

Local AI explanations (/api/ai/*) live in codevision.explain.AIService: one per
server, with one worker thread that makes every model call. /api/analyze only
adds graph["ai"], the cached node["x"] and node["xs"] (code files the AI never
gets); it never calls the model.
"""
from __future__ import annotations

import hmac
import importlib
import json
import logging
import os
import secrets
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from codevision import explain
from codevision.explain import messages as ai_text

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_RECENT = 10
MAX_BODY = 1_000_000
MAX_PATH_CHARS = 4096  # longer than any real folder path (Linux PATH_MAX); keeps junk input out
REQUEST_TIMEOUT = 60  # seconds a client may stall mid-request; the work itself has no limit

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
DEMO_DIR = PACKAGE_DIR / "demo" / "my-todo-app"

# Everything the screen needs is served from here (icons included), so it works offline and
# no other site is ever asked for anything. The icons are SVG masks in icons.css (data: images).
CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data:; connect-src 'self'"
)

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/icons.css": ("icons.css", "text/css; charset=utf-8"),
}

API_ROUTES = {
    ("GET", "/api/analyze"): "api_analyze",
    ("POST", "/api/pick-folder"): "api_pick_folder",
    ("GET", "/api/recent"): "api_recent",
    ("GET", "/api/ai/status"): "api_ai_status",
    ("POST", "/api/ai/settings"): "api_ai_settings",
    ("POST", "/api/ai/start"): "api_ai_start",
    ("POST", "/api/ai/explain"): "api_ai_explain",
    ("POST", "/api/ai/explain-all"): "api_ai_explain_all",
    ("GET", "/api/ai/job"): "api_ai_job",
    ("POST", "/api/ai/job/cancel"): "api_ai_job_cancel",
}
DEMO_PROJECT = "demo"  # what the screen sends as "project" for the example project

DIALOG_TITLE = "분석할 폴더를 골라 주세요"
PICKER_UNAVAILABLE = "이 컴퓨터에서는 폴더 선택 창을 열 수 없어요. 폴더 경로를 직접 붙여 넣어 주세요."
ANALYSIS_FAILED = "폴더를 분석하다가 문제가 생겼어요. 자세한 내용은 CodeVision 실행 기록에 남겼어요."

# Requests that http.server itself turns away before our code runs (see Handler.send_error).
PROTOCOL_ERRORS = {
    400: "요청을 이해하지 못했어요.",
    414: "요청 주소가 너무 길어요.",
    431: "요청 헤더가 너무 커요.",
    501: "이 요청 방식은 쓸 수 없어요.",
    505: "지원하지 않는 HTTP 버전이에요.",
}

log = logging.getLogger(__name__)
_recent_lock = threading.Lock()
_pick_lock = threading.Lock()


# --- recent folders -------------------------------------------------------

def recent_file() -> Path:
    return explain.data_dir() / "recent.json"


def load_recent() -> list[dict]:
    """Recently analyzed folders, newest first: [{"path", "name", "at"}]."""
    try:
        data = json.loads(recent_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("path"), str)][:MAX_RECENT]


def add_recent(path: str) -> list[dict]:
    """Put a folder at the top of the recent list (no duplicates, at most MAX_RECENT)."""
    with _recent_lock:
        key = os.path.normcase(path)
        items = [i for i in load_recent() if os.path.normcase(i["path"]) != key]
        items.insert(0, {"path": path, "name": Path(path).name or path, "at": time.time()})
        del items[MAX_RECENT:]
        target = recent_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        return items


# --- native folder dialog -------------------------------------------------

class PickerUnavailable(RuntimeError):
    """No folder dialog can be shown here (tkinter missing or no display)."""


# Runs in its own Python process, because Tk wants to own the main thread.
_DIALOG_SCRIPT = """\
import sys
try:
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
except Exception:
    sys.exit(3)
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(parent=root, title=sys.argv[1], mustexist=True)
root.destroy()
sys.stdout.buffer.write((path or "").encode("utf-8"))
"""


def pick_folder() -> str | None:
    """Show the OS folder dialog. Returns the chosen folder, or None when cancelled."""
    if getattr(sys, "frozen", False):
        # In a PyInstaller build sys.executable is the app itself, so "-c" does not work.
        return _pick_in_thread()
    return _pick_in_subprocess()


def _pick_in_subprocess() -> str | None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _DIALOG_SCRIPT, DIALOG_TITLE],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode == 3:
        raise PickerUnavailable(PICKER_UNAVAILABLE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}")
    path = proc.stdout.decode("utf-8", "replace")
    return os.path.normpath(path) if path else None


def _ask_directory() -> str | None:
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
    except Exception as exc:
        raise PickerUnavailable(PICKER_UNAVAILABLE) from exc
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(parent=root, title=DIALOG_TITLE, mustexist=True)
    finally:
        root.destroy()
    return os.path.normpath(path) if path else None


def _pick_in_thread() -> str | None:
    result: dict = {}

    def run() -> None:
        try:
            result["path"] = _ask_directory()
        except BaseException as exc:  # hand every error back to the request thread
            result["error"] = exc

    worker = threading.Thread(target=run, name="codevision-folder-dialog", daemon=True)
    worker.start()
    worker.join()
    if "error" in result:
        raise result["error"]
    return result.get("path")


# --- the folder to analyze ------------------------------------------------

class BadPath(ValueError):
    """The ?path= text cannot name a folder. The message is Korean, for the screen."""


def folder_path(text: str) -> Path:
    """The absolute path for the ?path= text, checked without touching the disk.

    Any absolute path is the user's choice: other drives, UNC shares
    (\\\\server\\share\\app) and long-path forms (\\\\?\\C:\\...) are all fine.
    Whether the folder exists is checked by the caller.
    """
    if not text:
        raise BadPath("분석할 폴더 경로를 알려 주세요.")
    if len(text) > MAX_PATH_CHARS:
        raise BadPath("폴더 경로가 너무 길어요. 경로를 다시 확인해 주세요.")
    if "\x00" in text:
        raise BadPath("폴더 경로에 쓸 수 없는 글자가 들어 있어요. 경로를 다시 확인해 주세요.")
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        raise BadPath("폴더의 전체 경로를 적어 주세요. 예: C:\\Users\\이름\\my-app")
    return Path(os.path.abspath(expanded))


# --- the project the screen analyzed ----------------------------------------

def build_graph(root: Path, demo: bool = False) -> dict:
    """Analyze a folder. codevision.graph is imported lazily, so the server starts regardless."""
    graph_module = importlib.import_module("codevision.graph")
    graph = graph_module.build(str(root))
    if demo:
        graph["source"] = {"kind": "demo", "path": ""}  # the screen shows "예제 프로젝트", not our install folder
    return graph


def project_key(root: Path, demo: bool = False) -> str:
    """How the AI side names a project: "demo", or the folder path in the OS's case rules."""
    return DEMO_PROJECT if demo else os.path.normcase(str(root))


def project_from_text(text: str) -> tuple[str, Path, bool]:
    """(key, root, demo) for the "project" the screen sends: "demo" or the analyzed folder's path."""
    if text == DEMO_PROJECT:
        return DEMO_PROJECT, DEMO_DIR, True
    root = folder_path(text.strip().strip('"').strip())
    return project_key(root), root, False


# --- server ---------------------------------------------------------------

class CodeVisionServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second server take a port that is already in use.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port: int, web_dir: Path, cache):
        self.token = secrets.token_urlsafe(32)
        self.web_dir = Path(web_dir)
        self.cache = cache
        super().__init__((HOST, port), Handler)
        self.ai = explain.AIService(cache, build_graph)

    def server_bind(self) -> None:
        # Same as HTTPServer.server_bind minus socket.getfqdn(), which can stall on some networks.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def server_close(self) -> None:
        ai = getattr(self, "ai", None)
        if ai is not None:
            ai.close()  # waiting AI requests are answered; the worker stops after its current call
        super().server_close()

    def handle_error(self, request, client_address) -> None:
        log.debug("connection problem with %s", client_address, exc_info=True)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"


def create_server(port: int = DEFAULT_PORT, web_dir: str | Path | None = None, cache=None) -> CodeVisionServer:
    """Bind 127.0.0.1:port (a free port if that one is taken). Call serve_forever() next."""
    web_dir = Path(web_dir) if web_dir else WEB_DIR
    if cache is None:
        cache = explain.ExplanationCache()
    try:
        return CodeVisionServer(port, web_dir, cache)
    except OSError:
        if port == 0:
            raise
        log.info("port %s is busy, using a free one", port)
        return CodeVisionServer(0, web_dir, cache)


class Handler(BaseHTTPRequestHandler):
    server: CodeVisionServer
    server_version = "CodeVision"
    timeout = REQUEST_TIMEOUT  # a client that stops sending is dropped instead of holding a thread
    _replied = False  # set once the reply has started; one request per connection (HTTP/1.0)

    def log_message(self, format, *args) -> None:  # quiet: no console line per request
        log.debug("%s %s", self.address_string(), format % args)

    def version_string(self) -> str:
        return self.server_version  # the Server header does not need to tell the Python version

    def send_error(self, code, message=None, explain=None) -> None:
        """Errors that http.server finds itself (bad request line, unknown method, huge headers).

        They get the same Korean JSON and security headers as every other reply,
        instead of the stock HTML page.
        """
        self.log_error("code %d, message %s", code, message)
        self.close_connection = True
        if self.request_version == "HTTP/0.9":  # a garbled request line: still answer with a status line and headers
            self.request_version = "HTTP/1.0"
        self._error(code, PROTOCOL_ERRORS.get(code, "요청을 처리하지 못했어요."))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                return self._error(403, "이 주소로는 열 수 없어요. CodeVision이 알려 준 127.0.0.1 주소로 열어 주세요.")
            path, _, query = self.path.partition("?")
            if path.startswith("/api/"):
                return self._api(method, path, parse_qs(query))
            if path in STATIC_FILES:
                if method != "GET":
                    return self._method_not_allowed(["GET"])
                return self._static(*STATIC_FILES[path])
            self._error(404, "찾는 페이지가 없어요.")
        except (ConnectionError, TimeoutError):
            self.close_connection = True  # the browser went away or stopped sending
        except Exception:
            if self._replied:  # the reply had started, so writing it failed: nothing more to send
                log.debug("reply cut off: %s %s", method, self.path, exc_info=True)
                return
            log.exception("request failed: %s %s", method, self.path)
            self._error(500, "서버에서 문제가 생겼어요. 잠시 뒤에 다시 해 주세요.")

    def _api(self, method: str, path: str, params: dict) -> None:
        body = b""
        if method == "POST":
            # Read the body first: closing with unread bytes would reset the connection
            # and the browser would lose even an error reply.
            body = self._read_body()
            if body is None:
                return  # _read_body has replied
        if not self._token_ok():
            return self._error(403, "접근 권한이 없어요. CodeVision 화면을 새로 고쳐 주세요.")
        allowed = [m for m, p in API_ROUTES if p == path]
        if not allowed:
            return self._error(404, "찾는 주소가 없어요.")
        if method not in allowed:
            return self._method_not_allowed(allowed)
        getattr(self, API_ROUTES[(method, path)])(params, body)

    # --- endpoints ---

    def api_analyze(self, params: dict, body: bytes) -> None:
        demo = params.get("demo", [""])[0] in ("1", "true")
        if demo:
            root = DEMO_DIR
        else:
            raw = params.get("path", [""])[0].strip().strip('"').strip()
            try:
                root = folder_path(raw)
            except BadPath as exc:
                return self._error(400, str(exc))
            if not os.path.exists(root):
                return self._error(400, f"폴더를 찾을 수 없어요: {raw}")
            if not os.path.isdir(root):
                return self._error(400, f"파일이 아니라 폴더를 골라 주세요: {raw}")
        ai = self.server.ai
        ai.prefetch_status()  # Ollama is asked in the background while the folder is analyzed
        try:
            graph = build_graph(root, demo)
        except Exception:
            # The traceback goes to stderr (the console or the desktop app's log), never into the reply.
            log.exception("analysis failed: %s", root)
            return self._error(500, ANALYSIS_FAILED)
        try:
            # graph["ai"], cached explanations (x) and the files the AI never gets (xs): the model is never called here
            ai.annotate(graph, root)
        except Exception:
            log.exception("adding cached AI explanations failed: %s", root)
            for node in (graph.get("nodes") or {}).values():
                if isinstance(node, dict):
                    node.pop("x", None)
                    node.pop("xs", None)
            graph["ai"] = ai.fallback_status(ai_text.ANNOTATE_FAILED)
        ai.remember(project_key(root, demo), root, graph, demo)
        if not demo:
            try:
                add_recent(str(root))
            except OSError:
                log.warning("최근 폴더 목록을 저장하지 못했어요.", exc_info=True)
        self._json(200, graph)

    def api_pick_folder(self, params: dict, body: bytes) -> None:
        if not _pick_lock.acquire(blocking=False):
            return self._error(409, "폴더 선택 창이 이미 열려 있어요.")
        try:
            path = pick_folder()
        except PickerUnavailable as exc:
            return self._error(501, str(exc))
        except Exception:
            log.exception("folder dialog failed")
            return self._error(500, "폴더 선택 창을 열지 못했어요. 폴더 경로를 직접 붙여 넣어 주세요.")
        finally:
            _pick_lock.release()
        self._json(200, {"path": path})

    def api_recent(self, params: dict, body: bytes) -> None:
        self._json(200, {"items": load_recent()})

    def api_ai_status(self, params: dict, body: bytes) -> None:
        # Asked when the AI panel opens, to see an Ollama started or stopped outside CodeVision: a fresh
        # probe (a few ms), not the block /api/analyze reuses for a few seconds.
        self._json(200, self.server.ai.status(fresh=True))

    def api_ai_settings(self, params: dict, body: bytes) -> None:
        data = self._body_object(body)
        if data is not None:
            self._ai_call(lambda: (200, self.server.ai.update_settings(data)))

    def api_ai_start(self, params: dict, body: bytes) -> None:
        if self._body_object(body) is not None:
            self._ai_call(lambda: (200, self.server.ai.start_ollama()))

    def api_ai_explain(self, params: dict, body: bytes) -> None:
        data = self._body_object(body)
        if data is None:
            return
        node = data.get("node")
        if not isinstance(node, str) or not node or len(node) > MAX_PATH_CHARS:
            return self._error(400, ai_text.BAD_REQUEST)
        project = self._project(data)
        if project is not None:
            self._ai_call(lambda: (200, self.server.ai.explain(*project, node)))

    def api_ai_explain_all(self, params: dict, body: bytes) -> None:
        data = self._body_object(body)
        project = self._project(data) if data is not None else None
        if project is not None:
            self._ai_call(lambda: self.server.ai.explain_all(*project))

    def api_ai_job(self, params: dict, body: bytes) -> None:
        job_id = params.get("id", [""])[0]
        since = params.get("since", ["0"])[0] or "0"
        if not job_id or len(job_id) > 64 or not (since.isascii() and since.isdigit()) or len(since) > 9:
            return self._error(400, ai_text.BAD_REQUEST)
        self._ai_call(lambda: (200, self.server.ai.job(job_id, int(since))))

    def api_ai_job_cancel(self, params: dict, body: bytes) -> None:
        data = self._body_object(body)
        if data is None:
            return
        job_id = data.get("job")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 64:
            return self._error(400, ai_text.BAD_REQUEST)
        self._ai_call(lambda: (200, self.server.ai.cancel_job(job_id)))

    # --- helpers ---

    def _body_object(self, body: bytes) -> dict | None:
        """The POST body as a JSON object ({} when empty). On a problem, reply 400 and return None."""
        if not body.strip():
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: nested too deeply ("[[[[...")
            data = None
        if not isinstance(data, dict):
            self._error(400, ai_text.BAD_REQUEST)
            return None
        return data

    def _project(self, data: dict) -> tuple[str, Path, bool] | None:
        """(key, root, demo) for data["project"]. On a problem, reply 400 and return None."""
        text = data.get("project")
        if not isinstance(text, str) or not text.strip():
            self._error(400, ai_text.BAD_REQUEST)
            return None
        try:
            return project_from_text(text)
        except BadPath as exc:
            self._error(400, str(exc))
            return None

    def _ai_call(self, call) -> None:
        """Run call() -> (status, data); an AIError becomes {"error", "code"} with its status."""
        try:
            status, data = call()
        except explain.AIError as exc:
            return self._json(exc.status, exc.reply())
        self._json(status, data)

    def _host_ok(self) -> bool:
        """Exactly one Host header, naming our own address and port (DNS rebinding guard)."""
        get_all = getattr(self.headers, "get_all", None)  # HTTP/0.9 requests carry no headers at all
        hosts = (get_all("Host") if get_all else None) or []
        if len(hosts) != 1:
            return False
        return hosts[0].strip().lower() in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _token_ok(self) -> bool:
        sent = self.headers.get("X-CV-Token") or ""
        return hmac.compare_digest(sent.encode("utf-8"), self.server.token.encode("utf-8"))

    def _read_body(self) -> bytes | None:
        """The POST body, at most MAX_BODY bytes. On a problem, reply with the error and return None."""
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(411, "요청 내용의 길이를 알 수 없어요.")
            return None
        values = {v.strip() for v in self.headers.get_all("Content-Length") or []}
        if not values:
            return b""
        text = values.pop() if len(values) == 1 else ""
        if not (text.isascii() and text.isdigit()):  # int() alone would also take "+5", " 5" and "5_000"
            self._error(400, "요청 내용을 읽을 수 없어요.")
            return None
        digits = text.lstrip("0") or "0"
        if len(digits) > len(str(MAX_BODY)) or int(digits) > MAX_BODY:  # never int() thousands of digits
            self._error(413, "보낸 내용이 너무 커요.")
            return None
        length = int(digits)
        body = self.rfile.read(length) if length else b""
        if len(body) < length:
            self._error(400, "요청 내용을 다 받지 못했어요.")
            return None
        return body

    def _method_not_allowed(self, allowed: list[str]) -> None:
        names = ", ".join(allowed)
        self._error(405, f"이 주소는 {names} 방식으로만 부를 수 있어요.", {"Allow": names})

    def _static(self, name: str, content_type: str) -> None:
        try:
            data = (self.server.web_dir / name).read_bytes()
        except OSError:
            return self._error(404, f"화면 파일({name})을 찾을 수 없어요.")
        if name == "index.html":
            data = data.decode("utf-8").replace("{{CV_TOKEN}}", self.server.token).encode("utf-8")
        self._send(200, data, content_type)

    def _json(self, status: int, data, headers: dict | None = None) -> None:
        # "replace" keeps a rare undecodable file name from failing the whole response
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str, headers: dict | None = None) -> None:
        self._json(status, {"error": message}, headers)

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        """Every reply goes out here, so every reply carries the security headers."""
        self._replied = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":  # a reply to HEAD is headers only
            self.wfile.write(body)
