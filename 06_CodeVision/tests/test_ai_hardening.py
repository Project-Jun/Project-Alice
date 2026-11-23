"""Hardening found by the backend verification, always against tests/fake_ollama.py or tiny local servers.

- request bodies nested too deeply for the JSON reader are a 400, not a 500;
- secret fallbacks and defaults (process.env.X || "...", os.getenv("X", "..."), obj["password"] = "...",
  password == "...", DB_PASS = "...") are masked before anything goes to the model;
- Ollama ending in the middle of an answer reads as "not running", and the status says so at once;
- a reply cut off in the middle is a dropped connection ("failed"), not an unreadable answer;
- an Ollama that answers but gives no model list is not reported as "no models installed".
"""
import contextlib
import http.server
import json
import threading
import time

import pytest

from codevision.explain import messages as M
from codevision.explain import ollama, safety
from codevision.explain.base import AIError, FileContext
from test_ai import cv  # noqa: F401  (the server-and-fake fixture)

MASK = safety.MASK


@contextlib.contextmanager
def tiny_server(handler):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


# --- request bodies ------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/ai/settings", "/api/ai/start", "/api/ai/explain", "/api/ai/explain-all",
                                  "/api/ai/job/cancel"])
@pytest.mark.parametrize("body", [b"[" * 100_000, b'{"a":' * 60_000, b'{"node": ' + b"[" * 100_000],
                         ids=["arrays", "objects", "inside-an-object"])
def test_json_nested_too_deeply_is_a_400(cv, path, body):
    status, data = cv.call("POST", path, body)
    assert (status, data) == (400, {"error": M.BAD_REQUEST})
    assert cv.fake.chats() == []


# --- secrets that are fallbacks, defaults or comparisons ----------------------------------------------

@pytest.mark.parametrize("line, secret", [
    ('const JWT_SECRET = process.env.JWT_SECRET || "fallback-jwt-secret";', "fallback-jwt-secret"),
    ('const SESSION_SECRET = process.env.SESSION_SECRET ?? "nullish-secret";', "nullish-secret"),
    ("const t = jwt.sign(payload, process.env.JWT_SECRET || 'sign-fallback');", "sign-fallback"),
    ('SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-0123456789")', "django-insecure-0123456789"),
    ('password = os.getenv("DB_PASSWORD") or "hunter2-fallback"', "hunter2-fallback"),
    ('JWT = os.getenv("JWT_SECRET") or "jwt-fallback"', "jwt-fallback"),
    ('DB_PASS = os.getenv("DB_PASS", "getenv-default-pass")', "getenv-default-pass"),
    ("SECRET_KEY = env('SECRET_KEY', default='abc-default-key')", "abc-default-key"),
    ('os.environ.setdefault("API_KEY", "setdefault-key")', "setdefault-key"),
    ("api_key: str = os.getenv('OPENAI_API_KEY', 'local-dev-key')", "local-dev-key"),
    ('password = os.environ["PW"] if prod else "devpass-else"', "devpass-else"),
    ('  secret: process.env.S || "object-default",', "object-default"),
    ('return os.environ.get("API_TOKEN") or "return-fallback"', "return-fallback"),
    ('headers.set("X-API-Key", "header-key-123")', "header-key-123"),
    ('os.environ["SECRET_KEY"] = "bracket-secret"', "bracket-secret"),
    ('config["password"] = "bracket-pass"', "bracket-pass"),
    ('if password == "admin123":', "admin123"),
    ('if (token === "letmein-token") {', "letmein-token"),
    ('if (req.body.password !== "compare-secret") return;', "compare-secret"),
    ('SMTP_PASS = "mail-pass-99"', "mail-pass-99"),
    ('userPass = "abc12345"', "abc12345"),
    ('const dbPwd = "xyz12345"', "xyz12345"),
])
def test_secret_fallbacks_defaults_and_comparisons_are_masked(line, secret):
    masked = safety.mask_secrets(line)
    assert secret not in masked and MASK in masked, masked


@pytest.mark.parametrize("line", [
    'value = os.getenv("HOME", "/home/user")',
    'debug = os.environ.get("DEBUG", "false")',
    'const port = process.env.PORT || "3000";',
    'host = os.getenv("DB_HOST") or "localhost"',
    'name = user.name or "guest"',
    'url = os.getenv("API_URL", "http://localhost:8000")',
    'api_key = os.environ.get("OPENAI_API_KEY", "")',
    'const apiBase = process.env.API_BASE ?? "/api";',
    'parser.add_argument("--token", help="the access token")',
    'bypass = "abc12345"',
    'compass_heading = "north-west"',
    'if mode == "password-reset":',
    'if kind == "token": return parse(text)',
    'msg = "Wrong password" if failed else "Welcome back"',
    'title = "Reset your password" or "Hello there"',
    'SENSITIVE_FIELDS = ["password", "token", "secret"]',
    "    pass  # nothing to do",
    "for token in tokens: print(token)",
])
def test_ordinary_lines_near_secret_words_are_left_alone(line):
    assert safety.mask_secrets(line) == line


@pytest.mark.parametrize("text", [
    ("password = " + "a" * 190 + "\n") * 5000,  # one long name on every line
    "var token=1;" + 'a||"xyz",' * 100_000,      # a minified file: one line, many fallbacks
    'a||"xyz",' * 100_000 + "var token=1;",
], ids=["long-names", "minified-secret-first", "minified-secret-last"])
def test_masking_stays_quick_on_big_odd_files(text):
    started = time.monotonic()
    safety.mask_secrets(text)
    assert time.monotonic() - started < 3


def test_secret_fallbacks_never_reach_the_model(cv):
    source = ('import os\n'
              'SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-e2e-0123")\n'
              'JWT = os.getenv("JWT_SECRET") or "jwt-fallback-e2e"\n'
              'config = {}\n'
              'config["password"] = "bracket-e2e-pass"\n\n'
              'def slug(text):\n    return text.lower()\n')
    (cv.root / "app" / "util.py").write_text(source, encoding="utf-8")
    cv.enable()
    assert cv.explain("app/util.py")[0] == 200
    sent = cv.fake.raw_requests()
    assert "def slug(text):" in sent and MASK in sent
    for secret in ("django-insecure-e2e-0123", "jwt-fallback-e2e", "bracket-e2e-pass"):
        assert secret not in sent, secret


# --- Ollama ending in the middle of an answer -----------------------------------------------------------

def test_ollama_ending_mid_answer_reads_as_not_running(cv):
    cv.enable()
    cv.fake.hold()
    replies = []
    asking = threading.Thread(target=lambda: replies.append(cv.explain("app/util.py")))
    asking.start()
    assert cv.fake.wait_for_chats(1)
    cv.fake.crash()  # the model was answering: nothing listens any more, and the answer ends with no reply
    asking.join(15)
    assert replies == [(409, {"error": M.NOT_RUNNING, "code": "not_running"})]
    block = cv.get("/api/ai/status")[1]
    assert block["ollama"] != "running" and block["available"] is False  # not a stale "running" for 3 s


def test_a_slow_answer_from_a_running_ollama_stays_a_timeout(cv):
    cv.enable()
    cv.httpd.ai.answer_timeout = 0.5
    cv.fake.set(fail="slow", slow=1.5, fail_count=1)
    assert cv.explain("app/util.py") == (504, {"error": M.TIMEOUT, "code": "timeout"})


# --- a reply cut off in the middle -------------------------------------------------------------------------

class _CutOff(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "1000")  # promises more than it sends
        self.end_headers()
        self.wfile.write(b'{"version": "0.3')
        self.close_connection = True

    do_GET = do_POST = _reply


def test_a_reply_cut_off_midway_is_a_dropped_connection():
    with tiny_server(_CutOff) as url:
        client = ollama.OllamaClient(url)
        with pytest.raises(ollama.OllamaDisconnected):
            client.version()
        explainer = ollama.OllamaExplainer(url, "qwen2.5:7b", client=client)
        with pytest.raises(AIError) as err:
            explainer.explain_file(FileContext(rel_path="a.py", language="python", source="x = 1\n"))
        assert err.value.code == "failed"


# --- an Ollama that answers but gives no model list ------------------------------------------------------

class _NoList(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._json(200, {"version": "0.32.6"})
        self._json(500, {"error": "busy"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._json(500, {"error": "busy"})


def test_an_ollama_that_gives_no_model_list_is_not_called_empty(cv, monkeypatch):
    with tiny_server(_NoList) as url:
        monkeypatch.setenv("CODEVISION_OLLAMA_URL", url)
        cv.httpd.ai.invalidate()
        block = cv.enable()
        assert (block["ollama"], block["available"], block["models"]) == ("running", False, [])
        assert block["message"] == M.NO_ANSWER  # not "no models installed"
        assert cv.explain("app/util.py") == (409, {"error": M.NO_ANSWER, "code": "not_running"})
        assert cv.start_job() == (409, {"error": M.NO_ANSWER, "code": "not_running"})
        status, block = cv.post("/api/ai/settings", {"model": "qwen2.5:7b"})  # not "not installed": unknown
        assert status == 200 and block["model"] == "qwen2.5:7b"
    assert cv.fake.chats() == []
