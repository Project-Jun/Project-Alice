"""Tests for the local web server and the command-line entry point.

No real browser and no real folder dialog: codevision.graph is replaced by a
fake module and the dialog function is monkeypatched.
"""
import contextlib
import http.client
import io
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import quote

import pytest

from codevision import __main__ as cli
from codevision import server as cv_server
from codevision.explain import prompts

SRC = Path(__file__).resolve().parents[1] / "src"
INDEX = '<!doctype html><meta name="cv-token" content="{{CV_TOKEN}}"><title>코드비전</title>'
APP_JS = "console.log('코드비전');"
APP_CSS = "body { color: #534AB7; }"
ICONS_CSS = ".ti-folder{--ti:url(\"data:image/svg+xml,%3Csvg%3E%3C/svg%3E\")}"
# No config file: local AI is off. conftest makes Ollama look missing (no program, nothing listening).
AI_OFF = {"provider": "local", "enabled": False, "available": False, "model": "", "models": [],
          "recommended": list(prompts.RECOMMENDED_MODELS), "ollama": "missing", "can_start": False,
          "message": "AI 설명이 꺼져 있어요."}
AI_ROUTES = [("GET", "/api/ai/status"), ("POST", "/api/ai/settings"), ("POST", "/api/ai/start"),
             ("POST", "/api/ai/explain"), ("POST", "/api/ai/explain-all"), ("GET", "/api/ai/job?id=j1"),
             ("POST", "/api/ai/job/cancel")]
GH_ROUTES = [("GET", "/api/github/status"), ("POST", "/api/github/login"), ("POST", "/api/github/login/cancel"),
             ("POST", "/api/github/logout"), ("GET", "/api/github/repos"),
             ("GET", "/api/github/branches?owner=octocat&repo=Hello-World"), ("POST", "/api/github/resolve"),
             ("POST", "/api/github/open"), ("GET", "/api/github/job?id=g0123456789ab"),
             ("POST", "/api/github/job/cancel"),
             ("GET", "/api/analyze?github=octocat%2FHello-World&branch=main&sha=" + "a" * 40)]
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": cv_server.CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window pops up on Windows


def fake_graph(root: str) -> dict:
    name = Path(root).name
    return {
        "version": 1,
        "source": {"kind": "local", "path": root},
        "name": name,
        "root": ".",
        "nodes": {
            ".": {"n": name, "p": name + "/", "t": "d", "a": "ot", "s": "루트", "d": "맨 위 폴더예요.", "k": ["main.py"]},
            "main.py": {"n": "main.py", "p": "main.py", "t": "f", "a": "sv", "l": 1, "s": "시작 파일",
                        "d": "여기서 시작해요.", "f": [], "u": [], "b": [], "lang": "python"},
        },
    }


class Client:
    def __init__(self, httpd, built):
        self.httpd = httpd
        self.built = built

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.httpd.port}"

    def request(self, method, path, *, token=True, host=None, body=None):
        headers = {}
        if token is True:
            headers["X-CV-Token"] = self.httpd.token
        elif token:
            headers["X-CV-Token"] = token
        if host:
            headers["Host"] = host
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def send(self, method, path, headers, body=None):
        """Send exactly these headers: no automatic Host or Content-Length."""
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                conn.putheader(name, value)
            conn.endheaders(body)
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def json(self, method, path, **kwargs):
        status, headers, raw = self.request(method, path, **kwargs)
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        return status, json.loads(raw.decode("utf-8"))


@contextlib.contextmanager
def running(httpd, built=None):
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield Client(httpd, built if built is not None else [])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path / "home"))
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(INDEX, encoding="utf-8")
    (web / "app.js").write_text(APP_JS, encoding="utf-8")
    (web / "app.css").write_text(APP_CSS, encoding="utf-8")
    (web / "icons.css").write_text(ICONS_CSS, encoding="utf-8")
    (web / "secret.txt").write_text("not on the list", encoding="utf-8")

    built = []
    graph_module = types.ModuleType("codevision.graph")

    def build(root):
        built.append(str(root))
        return fake_graph(str(root))

    graph_module.build = build
    monkeypatch.setitem(sys.modules, "codevision.graph", graph_module)

    with running(cv_server.create_server(port=0, web_dir=web), built) as client:
        yield client


def analyze_url(folder) -> str:
    return "/api/analyze?path=" + quote(str(folder), safe="")


def raw_exchange(port: int, data: bytes, *, shut_write: bool = False) -> bytes:
    """Send raw bytes and read until the server hangs up (it answers one request per connection)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(data)
        if shut_write:
            sock.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def parse_reply(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return int(lines[0].split()[1]), headers, body


# --- pages and static files -------------------------------------------------

def test_index_gets_the_token_and_security_headers(app):
    status, headers, body = app.request("GET", "/", token=False)
    assert status == 200
    html = body.decode("utf-8")
    assert f'content="{app.httpd.token}"' in html
    assert "{{CV_TOKEN}}" not in html
    assert "코드비전" in html
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Security-Policy"] == (  # nothing from other sites: the screen works offline
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'"  # injected markup can neither re-base links nor send a form
    )
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert headers["Server"] == "CodeVision"  # no Python version
    assert app.request("GET", "/?path=C%3A%5Cx", token=False)[0] == 200


def test_each_server_gets_its_own_token(app, tmp_path):
    other = cv_server.create_server(port=0, web_dir=tmp_path)
    try:
        assert other.token != app.httpd.token
        assert len(app.httpd.token) >= 32
    finally:
        other.server_close()


def test_static_files(app):
    status, headers, body = app.request("GET", "/app.js", token=False)
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert body.decode("utf-8") == APP_JS
    status, headers, body = app.request("GET", "/app.css", token=False)
    assert status == 200
    assert headers["Content-Type"] == "text/css; charset=utf-8"
    assert body.decode("utf-8") == APP_CSS
    status, headers, body = app.request("GET", "/icons.css", token=False)
    assert status == 200 and headers["Content-Type"] == "text/css; charset=utf-8"
    assert body.decode("utf-8") == ICONS_CSS


def test_the_screen_loads_nothing_from_other_sites():
    """CodeVision is a local tool: offline, or with a CDN blocked, the icons must still be there."""
    web = cv_server.WEB_DIR
    html = (web / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""\b(?:src|href)\s*=\s*["']?(?:[a-z]+:)?//""", html, re.I)
    for name in ("app.css", "icons.css"):
        css = (web / name).read_text(encoding="utf-8")
        assert "@import" not in css and not re.search(r"""url\(\s*["']?(?:[a-z]+:)?//""", css, re.I), name
    assert "//" not in cv_server.CSP  # no other host is allowed either
    for path in re.findall(r"""\b(?:src|href)\s*=\s*["']\./([^"']+)""", html):
        assert "/" + path in cv_server.STATIC_FILES  # everything the page asks for is served


def test_every_icon_in_use_is_drawn():
    web = cv_server.WEB_DIR
    pattern = r"\bti-[a-z0-9]+(?:-[a-z0-9]+)*"
    used = set(re.findall(pattern, (web / "app.js").read_text(encoding="utf-8")))
    used |= set(re.findall(pattern, (web / "index.html").read_text(encoding="utf-8")))
    used |= set(re.findall(r'"(ti-[a-z0-9-]+)"', (cv_server.PACKAGE_DIR / "graph.py").read_text(encoding="utf-8")))
    used |= {"ti-plus", "ti-minus"}  # app.js builds these two names: `ti-${open ? 'minus' : 'plus'}`
    drawn = set(re.findall(r"\.(ti-[a-z0-9-]+)\{", (web / "icons.css").read_text(encoding="utf-8")))
    assert {"ti-folder", "ti-file-code", "ti-file-alert", "ti-player-play"} <= used
    assert used - drawn == set()


@pytest.mark.parametrize("path", [
    "/../pyproject.toml",
    "/%2e%2e/pyproject.toml",
    "/..%2fpyproject.toml",
    "/..%5c..%5cpyproject.toml",
    "/app.js/../../pyproject.toml",
    "/web/index.html",
    "/server.py",
    "/demo/my-todo-app/README.md",
    # encoded, doubly encoded and backslash forms
    "/.%2e/.%2e/pyproject.toml",
    "/%252e%252e/pyproject.toml",
    "/%2e%2e%5cpyproject.toml",
    "/..\\pyproject.toml",
    "/..\\..\\src\\codevision\\server.py",
    "/\\app.js",
    "/%5capp.js",
    "/%2fapp.js",
    "/C:/Windows/win.ini",
    "/C:%5cWindows%5cwin.ini",
    # names that Windows would map to app.js on disk
    "/APP.JS",
    "/App.js",
    "/app.js.",
    "/app.js%20",
    "/app.js::$DATA",
    "/app.js%00",
    "/app.js/",
    "/./app.js",
    "/%61pp.js",
    "/app%2ejs",
    # files in the web folder that are not on the list
    "/secret.txt",
    "/favicon.ico",
])
def test_static_paths_outside_the_whitelist_are_404(app, path):
    status, headers, body = app.request("GET", path, token=False)
    assert status == 404
    assert "error" in json.loads(body.decode("utf-8"))
    assert b"not on the list" not in body


# --- Host header and token --------------------------------------------------

def test_api_needs_the_token(app):
    status, data = app.json("GET", "/api/recent", token=False)
    assert status == 403 and "권한" in data["error"]
    assert app.json("GET", "/api/recent", token="wrong-token")[0] == 403
    assert app.json("GET", "/api/analyze?demo=1", token=False)[0] == 403
    assert app.json("POST", "/api/pick-folder", token=False)[0] == 403
    body = json.dumps({"project": "demo", "node": "main.py"}).encode("utf-8")
    for method, path in AI_ROUTES + GH_ROUTES:
        for token in (False, "wrong-token"):
            status, data = app.json(method, path, token=token, body=body if method == "POST" else None)
            assert status == 403 and "권한" in data["error"], (method, path, token)
    assert app.json("GET", "/api/github/nothing-here", token=False)[0] == 403  # GitHub routes stay hidden too
    assert app.json("GET", "/api/github/logout", token=False)[0] == 403
    assert app.httpd.github.status()["logged_in"] is False  # no login or logout was started
    assert app.json("GET", "/api/nothing-here", token=False)[0] == 403
    assert app.json("GET", "/api/pick-folder", token=False)[0] == 403  # no hint about methods without the token
    assert app.json("GET", "/api/ai/explain", token=False)[0] == 403
    assert app.json("GET", "/api/recent")[0] == 200
    assert app.built == []


def test_ai_routes_check_the_host_too(app):
    port = app.httpd.port
    for method, path in AI_ROUTES + GH_ROUTES:  # no body: a foreign Host is refused before any body is read
        status, data = app.json(method, path, host=f"evil.example:{port}")
        assert status == 403 and "열 수 없어요" in data["error"], path
    assert app.built == []


def test_token_is_compared_in_constant_time(app, monkeypatch):
    calls = []
    real = cv_server.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(cv_server.hmac, "compare_digest", spy)
    token = app.httpd.token
    assert token.swapcase() != token
    for wrong in ("", token[:-1], token + "x", token.swapcase(), token + " ", "토큰".encode("utf-8")):
        status, _, raw = app.send("GET", "/api/recent", [("Host", app.host), ("X-CV-Token", wrong)])
        assert status == 403, wrong
        assert "권한" in json.loads(raw)["error"]
    assert app.send("GET", "/api/recent", [("Host", app.host), ("X-CV-Token", token)])[0] == 200
    assert calls and all(pair == (bytes, bytes) for pair in calls)


def test_wrong_host_header_is_rejected(app):
    port = app.httpd.port
    status, data = app.json("GET", "/", host=f"evil.example:{port}")
    assert status == 403 and data["error"]
    assert app.request("GET", "/api/recent", host=f"evil.example:{port}")[0] == 403
    assert app.request("GET", "/", host="127.0.0.1")[0] == 403
    assert app.request("GET", "/", host=f"127.0.0.1:{port + 1}")[0] == 403
    assert app.request("GET", "/", host=f"localhost:{port}")[0] == 200
    assert app.request("GET", "/api/recent", host=f"LOCALHOST:{port}")[0] == 200


def test_host_header_must_be_exactly_our_address_once(app):
    port = app.httpd.port
    token = [("X-CV-Token", app.httpd.token)]
    for host in ["", f"localhost.:{port}", f"127.0.0.1:0{port}", f"[::1]:{port}", f"127.0.0.2:{port}",
                 f"0.0.0.0:{port}", f"127.0.0.1:{port}@evil.example", f"evil.example#127.0.0.1:{port}",
                 f"127.0.0.1:{port}/", f"127.0.0.1:{port}.evil.example", f"127.0.0.1:{port}, evil.example"]:
        assert app.send("GET", "/api/recent", token + [("Host", host)])[0] == 403, host
    for host in [f"127.0.0.1:{port}", f"localhost:{port}", f"LocalHost:{port}", f" 127.0.0.1:{port} "]:
        assert app.send("GET", "/api/recent", token + [("Host", host)])[0] == 200, host

    assert app.send("GET", "/api/recent", token)[0] == 403  # no Host at all
    assert app.send("GET", "/api/recent", token + [("Host", app.host)] * 2)[0] == 403
    assert app.send("GET", "/api/recent", token + [("Host", app.host), ("Host", "evil.example")])[0] == 403
    assert app.send("GET", "/api/recent", token + [("Host", "evil.example"), ("Host", app.host)])[0] == 403

    status, headers, body = parse_reply(raw_exchange(port, b"GET / HTTP/1.0\r\n\r\n"))  # HTTP/1.0 without Host
    assert status == 403 and "열 수 없어요" in json.loads(body)["error"]
    # HTTP/0.9 has no headers (and no status line in the reply): still refused, not a crash
    assert "열 수 없어요" in json.loads(raw_exchange(port, b"GET /\r\n"))["error"]


# --- methods ----------------------------------------------------------------

@pytest.mark.parametrize("method, path, allow", [
    ("GET", "/api/pick-folder", "POST"),
    ("GET", "/api/ai/settings", "POST"),
    ("GET", "/api/ai/start", "POST"),
    ("GET", "/api/ai/explain", "POST"),
    ("GET", "/api/ai/explain-all", "POST"),
    ("POST", "/api/ai/job?id=j1", "GET"),
    ("GET", "/api/ai/job/cancel", "POST"),
    ("POST", "/api/analyze?demo=1", "GET"),
    ("POST", "/api/recent", "GET"),
    ("POST", "/api/ai/status", "GET"),
    ("POST", "/api/github/status", "GET"),
    ("GET", "/api/github/login", "POST"),
    ("GET", "/api/github/login/cancel", "POST"),
    ("GET", "/api/github/logout", "POST"),
    ("POST", "/api/github/repos", "GET"),
    ("POST", "/api/github/branches?owner=octocat&repo=Hello-World", "GET"),
    ("GET", "/api/github/resolve", "POST"),
    ("GET", "/api/github/open", "POST"),
    ("POST", "/api/github/job?id=g0123456789ab", "GET"),
    ("GET", "/api/github/job/cancel", "POST"),
    ("POST", "/api/analyze?github=octocat%2FHello-World&branch=main&sha=" + "a" * 40, "GET"),
    ("POST", "/", "GET"),
    ("POST", "/index.html", "GET"),
    ("POST", "/app.js", "GET"),
    ("POST", "/app.css", "GET"),
    ("POST", "/icons.css", "GET"),
])
def test_wrong_method_is_405(app, monkeypatch, method, path, allow):
    opened = []
    monkeypatch.setattr(cv_server, "pick_folder", lambda: opened.append(1))
    status, headers, raw = app.request(method, path)
    assert status == 405
    assert headers["Allow"] == allow
    assert json.loads(raw.decode("utf-8")) == {"error": f"이 주소는 {allow} 방식으로만 부를 수 있어요."}
    assert opened == [] and app.built == []
    assert app.httpd.github.status()["flow"] is None  # a GET never starts a login


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "FOO"])
def test_other_methods_get_a_korean_501(app, method):
    status, headers, raw = app.request(method, "/api/recent")
    assert status == 501
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(raw.decode("utf-8")) == {"error": "이 요청 방식은 쓸 수 없어요."}
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value
    assert "Access-Control-Allow-Origin" not in headers  # a CORS preflight never succeeds


def test_head_gets_headers_only(app):
    raw = raw_exchange(app.httpd.port, f"HEAD / HTTP/1.1\r\nHost: {app.host}\r\n\r\n".encode())
    head, sep, body = raw.partition(b"\r\n\r\n")
    assert sep and body == b""
    assert head.startswith(b"HTTP/1.0 501 ")
    assert b"\r\nX-Frame-Options: DENY" in head


def test_requests_http_server_refuses_get_korean_json(app):
    port = app.httpd.port
    # Each request ends where the server stops reading, so no unread byte can reset the connection.
    cases = [
        (b"NONSENSE\r\n", 400),
        (b"GET / HTTX/1.1\r\n", 400),
        (b"POST /\r\n", 400),  # HTTP/0.9 only knew GET
        (b"GET / HTTP/2.0\r\n", 505),
        (b"GET /" + b"a" * 65532, 414),  # 65,537 bytes: one more than a request line may have
        (b"GET / HTTP/1.1\r\n" + b"X-A: 1\r\n" * 101, 431),
    ]
    for data, code in cases:
        status, headers, body = parse_reply(raw_exchange(port, data))
        assert status == code
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert json.loads(body.decode("utf-8")) == {"error": cv_server.PROTOCOL_ERRORS[code]}
        for name, value in SECURITY_HEADERS.items():
            assert headers[name] == value
    assert app.request("GET", "/", token=False)[0] == 200


# --- request bodies ---------------------------------------------------------

def test_post_bodies_are_limited(app, monkeypatch):
    monkeypatch.setattr(cv_server, "MAX_BODY", 64)
    monkeypatch.setattr(cv_server, "pick_folder", lambda: None)
    ok = [("Host", app.host), ("X-CV-Token", app.httpd.token)]

    assert app.send("POST", "/api/pick-folder", ok + [("Content-Length", "64")], b"x" * 64)[0] == 200
    assert app.send("POST", "/api/pick-folder", ok)[0] == 200  # no body at all is fine too
    for path in ("/api/pick-folder", "/api/ai/explain", "/api/ai/settings"):
        for length in ("65", "00065", str(10 ** 12), "9" * 5000):  # refused from the header alone, no body sent
            status, _, raw = app.send("POST", path, ok + [("Content-Length", length)])
            assert status == 413 and json.loads(raw)["error"] == "보낸 내용이 너무 커요."
    for length in ("", "abc", "-1", "+5", "5_0", "1e3", "0x10", "6 4"):
        status, _, raw = app.send("POST", "/api/ai/explain", ok + [("Content-Length", length)])
        assert status == 400 and json.loads(raw)["error"] == "요청 내용을 읽을 수 없어요.", length
    status, _, raw = app.send("POST", "/api/ai/explain", ok + [("Content-Length", "1"), ("Content-Length", "2")])
    assert status == 400
    status, _, raw = app.send("POST", "/api/ai/explain", ok + [("Transfer-Encoding", "chunked")])
    assert status == 411 and "길이" in json.loads(raw)["error"]

    short = (f"POST /api/ai/explain HTTP/1.1\r\nHost: {app.host}\r\nX-CV-Token: {app.httpd.token}\r\n"
             "Content-Length: 10\r\n\r\nabc").encode()
    status, _, body = parse_reply(raw_exchange(app.httpd.port, short, shut_write=True))
    assert status == 400 and json.loads(body)["error"] == "요청 내용을 다 받지 못했어요."


def test_a_client_that_stops_sending_is_dropped(app, monkeypatch, caplog):
    monkeypatch.setattr(cv_server.Handler, "timeout", 0.3)
    stalled_requests = [
        f"GET / HTTP/1.1\r\nHost: {app.host}\r\n",  # the headers never end
        f"POST /api/ai/explain HTTP/1.1\r\nHost: {app.host}\r\nContent-Length: 10\r\n\r\nabc",  # nor the body
    ]
    for data in stalled_requests:
        with socket.create_connection(("127.0.0.1", app.httpd.port), timeout=10) as stalled:
            stalled.sendall(data.encode())
            assert app.request("GET", "/", token=False)[0] == 200  # everyone else is still served
            started = time.monotonic()
            try:
                assert stalled.recv(1024) == b""  # the server hung up without a reply
            except ConnectionResetError:
                pass
            assert time.monotonic() - started < 5
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- analysis ---------------------------------------------------------------

def test_analyze_demo(app):
    status, headers, raw = app.request("GET", "/api/analyze?demo=1")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    data = json.loads(raw.decode("utf-8"))
    assert Path(app.built[-1]) == cv_server.DEMO_DIR
    assert data["source"] == {"kind": "demo", "path": ""}  # the screen says 예제 프로젝트, not our install folder
    assert data["nodes"]["main.py"]["s"] == "시작 파일"
    assert data["ai"] == AI_OFF
    assert "x" not in data["nodes"]["main.py"]
    assert app.json("GET", "/api/recent") == (200, {"items": []})  # the demo is not remembered


def test_demo_with_the_real_analyzer_does_not_show_the_install_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    with running(cv_server.create_server(port=0, web_dir=tmp_path)) as client:
        status, headers, raw = client.request("GET", "/api/analyze?demo=1")
    assert status == 200
    data = json.loads(raw.decode("utf-8"))
    assert data["source"] == {"kind": "demo", "path": ""}
    assert data["name"] == "my-todo-app"
    assert "backend/routes/todos.py" in data["nodes"]
    installed = json.dumps(str(cv_server.DEMO_DIR.parent), ensure_ascii=False)[1:-1]
    assert installed not in raw.decode("utf-8")


def test_analyze_folder_and_recent_list(app, tmp_path):
    first = tmp_path / "프로젝트 하나"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for folder in (first, second, first):
        status, data = app.json("GET", analyze_url(folder))
        assert status == 200
        assert data["source"] == {"kind": "local", "path": str(folder)}
    assert app.built == [str(first), str(second), str(first)]

    status, data = app.json("GET", "/api/recent")
    assert status == 200
    assert [i["path"] for i in data["items"]] == [str(first), str(second)]
    assert data["items"][0]["name"] == "프로젝트 하나"
    assert isinstance(data["items"][0]["at"], float)
    assert (tmp_path / "home" / "recent.json").is_file()


def test_analyze_errors_are_korean_400s(app, tmp_path):
    status, data = app.json("GET", analyze_url(tmp_path / "없는 폴더"))
    assert status == 400 and "폴더를 찾을 수 없어요" in data["error"]

    note = tmp_path / "note.txt"
    note.write_text("메모", encoding="utf-8")
    status, data = app.json("GET", analyze_url(note))
    assert status == 400 and "폴더를 골라 주세요" in data["error"]

    status, data = app.json("GET", "/api/analyze")
    assert status == 400 and "경로" in data["error"]
    status, data = app.json("GET", "/api/analyze?path=relative%2Fdir")
    assert status == 400 and "전체 경로" in data["error"]
    for relative in ("./my-app", "..\\my-app", "my-app"):
        status, data = app.json("GET", analyze_url(relative))
        assert status == 400 and "전체 경로" in data["error"], relative
    assert app.built == []
    assert app.json("GET", "/api/recent")[1] == {"items": []}


def test_overlong_or_broken_paths_are_400s(app, tmp_path):
    longest = "C:\\" + "a" * (cv_server.MAX_PATH_CHARS - 3)
    status, data = app.json("GET", analyze_url(longest + "a"))
    assert status == 400 and data["error"] == "폴더 경로가 너무 길어요. 경로를 다시 확인해 주세요."
    status, data = app.json("GET", analyze_url(longest))  # at the limit: an ordinary "not found"
    assert status == 400 and "폴더를 찾을 수 없어요" in data["error"]

    status, data = app.json("GET", analyze_url(str(tmp_path) + "\x00"))
    assert status == 400 and "쓸 수 없는 글자" in data["error"]
    assert app.built == []


@pytest.mark.skipif(os.name != "nt", reason="UNC paths and drive letters are Windows forms")
def test_unc_and_long_path_forms_are_the_users_choice(app, tmp_path):
    assert cv_server.folder_path(r"\\server\share\my-app") == Path(r"\\server\share\my-app")
    assert cv_server.folder_path("//server/share/my-app") == Path(r"\\server\share\my-app")
    assert cv_server.folder_path("D:\\work\\..\\my-app") == Path("D:\\my-app")
    with pytest.raises(cv_server.BadPath, match="전체 경로"):
        cv_server.folder_path("C:")  # relative to the current folder on drive C

    long_form = "\\\\?\\" + str(tmp_path)
    status, data = app.json("GET", analyze_url(long_form))
    assert status == 200 and app.built == [long_form]


def test_analyze_accepts_a_quoted_path(app, tmp_path):
    status, data = app.json("GET", analyze_url(f'"{tmp_path}"'))
    assert status == 200
    assert app.built == [str(tmp_path)]


def test_analysis_crash_is_a_korean_500_without_the_traceback(app, tmp_path, caplog):
    def boom(root):
        raise RuntimeError("parser exploded in C:/secret/internal.py")

    sys.modules["codevision.graph"].build = boom
    status, headers, raw = app.request("GET", analyze_url(tmp_path))
    assert status == 500
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(raw.decode("utf-8")) == {"error": cv_server.ANALYSIS_FAILED}
    assert "분석하다가" in cv_server.ANALYSIS_FAILED
    for leak in (b"Traceback", b"RuntimeError", b"exploded", b"secret", b'File "'):
        assert leak not in raw
    errors = [r for r in caplog.records if r.name == "codevision.server" and r.levelno == logging.ERROR]
    assert errors and errors[0].exc_info[0] is RuntimeError  # the traceback goes to the log instead
    assert app.json("GET", "/api/recent")[1] == {"items": []}  # a failed folder is not remembered


def test_analysis_traceback_goes_to_stderr(tmp_path):
    script = (
        "import http.client, json, sys, threading, types\n"
        "graph = types.ModuleType('codevision.graph')\n"
        "def build(root):\n"
        "    raise RuntimeError('parser exploded in C:/secret/internal.py')\n"
        "graph.build = build\n"
        "sys.modules['codevision.graph'] = graph\n"
        "from codevision import server\n"
        "httpd = server.create_server(port=0, web_dir=sys.argv[1])\n"
        "threading.Thread(target=httpd.serve_forever, daemon=True).start()\n"
        "conn = http.client.HTTPConnection('127.0.0.1', httpd.port, timeout=30)\n"
        "conn.request('GET', '/api/analyze?demo=1', headers={'X-CV-Token': httpd.token})\n"
        "reply = conn.getresponse()\n"
        "sys.stdout.write(json.dumps({'status': reply.status, 'body': reply.read().decode('utf-8')}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC), CODEVISION_HOME=str(tmp_path))
    proc = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, env=env,
                          timeout=60, creationflags=NO_WINDOW)
    reply = json.loads(proc.stdout.decode("utf-8"))
    assert reply["status"] == 500
    assert json.loads(reply["body"]) == {"error": cv_server.ANALYSIS_FAILED}
    err = proc.stderr.decode("utf-8", "replace")
    assert "analysis failed" in err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: parser exploded in C:/secret/internal.py" in err


def test_unexpected_errors_are_a_korean_500_without_details(app, monkeypatch, caplog):
    def broken():
        raise KeyError("internal detail")

    monkeypatch.setattr(cv_server, "load_recent", broken)
    status, headers, raw = app.request("GET", "/api/recent")
    assert status == 500
    assert json.loads(raw.decode("utf-8")) == {"error": "서버에서 문제가 생겼어요. 잠시 뒤에 다시 해 주세요."}
    assert b"internal detail" not in raw and b"Traceback" not in raw
    assert any(r.exc_info and r.exc_info[0] is KeyError for r in caplog.records)


def test_a_slow_analysis_does_not_hold_up_other_requests(app, tmp_path):
    started, release = threading.Event(), threading.Event()
    seen = {}

    def slow_build(root):
        seen["daemon"] = threading.current_thread().daemon
        started.set()
        release.wait(30)
        return fake_graph(root)

    sys.modules["codevision.graph"].build = slow_build
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("reply", app.json("GET", analyze_url(tmp_path))))
    worker.start()
    try:
        assert started.wait(10)
        begin = time.monotonic()
        assert app.request("GET", "/", token=False)[0] == 200
        assert app.json("GET", "/api/recent")[0] == 200
        assert app.json("GET", "/api/ai/status")[0] == 200
        assert time.monotonic() - begin < 5
        assert worker.is_alive()  # still analyzing
    finally:
        release.set()
        worker.join(10)
    assert result["reply"][0] == 200
    assert seen["daemon"] is True  # request threads never keep the program alive


def test_closing_the_server_does_not_wait_for_a_running_analysis(app, tmp_path):
    started, release = threading.Event(), threading.Event()

    def stuck_build(root):
        started.set()
        release.wait(30)
        return fake_graph(root)

    sys.modules["codevision.graph"].build = stuck_build
    worker = threading.Thread(target=lambda: app.request("GET", analyze_url(tmp_path)), daemon=True)
    worker.start()
    try:
        assert started.wait(10)
        begin = time.monotonic()
        app.httpd.shutdown()
        app.httpd.server_close()
        assert time.monotonic() - begin < 3
    finally:
        release.set()
        worker.join(10)


def test_every_reply_has_the_security_headers(app, monkeypatch):
    monkeypatch.setattr(cv_server, "MAX_BODY", 8)
    too_big = [("Host", app.host), ("X-CV-Token", app.httpd.token), ("Content-Length", "9")]
    replies = [
        app.request("GET", "/", token=False),
        app.request("GET", "/app.js", token=False),
        app.request("GET", "/api/recent"),
        app.request("GET", "/api/analyze?demo=1"),
        app.request("GET", "/", host="evil.example"),
        app.request("GET", "/api/recent", token="wrong"),
        app.request("GET", "/nothing-here", token=False),
        app.request("GET", "/api/pick-folder"),
        app.request("GET", "/api/analyze"),
        app.send("POST", "/api/ai/explain", too_big),
        app.request("PUT", "/api/recent"),
        app.request("GET", "/api/ai/status"),
        app.request("POST", "/api/ai/start", body=b"{}"),  # conftest: no Ollama program -> 409 missing
        app.request("GET", "/api/ai/job?id=nope"),
        app.request("GET", "/api/github/status"),
        app.request("GET", "/api/github/repos"),  # not logged in: 401 login_needed
        app.request("POST", "/api/github/resolve", body=b"{}"),  # 400 bad_input (bodies fit in MAX_BODY 8 here)
        app.request("GET", "/api/github/job?id=g0123456789ab"),  # 404 job_not_found
        app.request("GET", "/api/github/branches?owner=octocat&repo=x"),  # conftest's GitHub never answers: 502
        app.request("GET", "/api/analyze?github=octocat%2Fx&branch=main&sha=" + "a" * 40),  # 404 snapshot_missing
        app.request("POST", "/api/github/logout", body=b"[]"),  # 400: not a JSON object
    ]
    monkeypatch.setattr(cv_server, "load_recent", lambda: 1 / 0)
    replies.append(app.request("GET", "/api/recent"))
    assert [r[0] for r in replies] == [200, 200, 200, 200, 403, 403, 404, 405, 400, 413, 501, 200, 409, 404,
                                       200, 401, 400, 404, 502, 404, 400, 500]
    for status, headers, body in replies:
        for name, value in SECURITY_HEADERS.items():
            assert headers[name] == value, (status, name)
        assert headers["Server"] == "CodeVision"
        assert "Access-Control-Allow-Origin" not in headers


# --- recent folders, AI, folder dialog ----------------------------------------

def test_recent_keeps_ten_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    folders = [str(tmp_path / f"p{i}") for i in range(12)]
    for folder in folders:
        cv_server.add_recent(folder)
    cv_server.add_recent(folders[5])
    items = cv_server.load_recent()
    assert len(items) == 10
    assert [i["path"] for i in items[:4]] == [folders[5], folders[11], folders[10], folders[9]]
    assert len({i["path"] for i in items}) == 10
    assert folders[0] not in {i["path"] for i in items}


def test_recent_ignores_a_broken_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    (tmp_path / "recent.json").write_text("{broken", encoding="utf-8")
    assert cv_server.load_recent() == []
    assert [i["path"] for i in cv_server.add_recent(str(tmp_path))] == [str(tmp_path)]


def test_ai_status_and_the_old_explain_placeholder_is_gone(app):
    assert app.json("GET", "/api/ai/status") == (200, AI_OFF)
    status, data = app.json("POST", "/api/explain", body=json.dumps({"id": "main.py"}).encode("utf-8"))
    assert status == 404 and data == {"error": "찾는 주소가 없어요."}
    status, data = app.json("POST", "/api/ai/explain", body=json.dumps({"project": "demo", "node": "main.py"}).encode())
    assert status == 409 and data == {"error": "AI 설명이 꺼져 있어요. 먼저 AI 설명을 켜 주세요.", "code": "off"}
    assert app.built == []  # with AI off nothing is analyzed for it


def test_pick_folder_endpoint(app, monkeypatch):
    monkeypatch.setattr(cv_server, "pick_folder", lambda: "C:\\Users\\나\\my-app")
    assert app.json("POST", "/api/pick-folder") == (200, {"path": "C:\\Users\\나\\my-app"})

    monkeypatch.setattr(cv_server, "pick_folder", lambda: None)
    assert app.json("POST", "/api/pick-folder") == (200, {"path": None})

    def unavailable():
        raise cv_server.PickerUnavailable(cv_server.PICKER_UNAVAILABLE)

    monkeypatch.setattr(cv_server, "pick_folder", unavailable)
    status, data = app.json("POST", "/api/pick-folder")
    assert status == 501 and "폴더 선택 창" in data["error"]

    status, headers, _ = app.request("GET", "/api/pick-folder")
    assert status == 405 and headers["Allow"] == "POST"


def test_pick_folder_runs_tkinter_in_a_subprocess(monkeypatch):
    calls = []
    replies = iter([(0, "C:/Users/나/app".encode("utf-8")), (0, b""), (3, b"")])

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        code, out = next(replies)
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=b"")

    monkeypatch.setattr(cv_server.subprocess, "run", fake_run)
    assert cv_server.pick_folder() == os.path.normpath("C:/Users/나/app")
    assert calls[0][0] == sys.executable and "-c" in calls[0]
    assert cv_server.pick_folder() is None
    with pytest.raises(cv_server.PickerUnavailable):
        cv_server.pick_folder()


def test_pick_folder_uses_a_worker_thread_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    threads = []

    def ask():
        threads.append(threading.current_thread())
        return "D:\\work"

    monkeypatch.setattr(cv_server, "_ask_directory", ask)
    monkeypatch.setattr(cv_server.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess when frozen"))
    assert cv_server.pick_folder() == "D:\\work"
    assert threads and threads[0] is not threading.current_thread()


def test_busy_port_falls_back_to_a_free_one(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        httpd = cv_server.create_server(port=port, web_dir=tmp_path)
        try:
            assert httpd.server_address[0] == "127.0.0.1"
            assert httpd.port not in (0, port)
            assert httpd.url == f"http://127.0.0.1:{httpd.port}"
        finally:
            httpd.server_close()


# --- command line -------------------------------------------------------------

class FakeServer:
    def __init__(self, port=8765):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.closed = False

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


class Stream(io.TextIOWrapper):
    """Stands in for sys.stdout: a console (tty) or a pipe into a log file."""

    def __init__(self, encoding, tty):
        super().__init__(io.BytesIO(), encoding=encoding, errors="strict")
        self.tty = tty

    def isatty(self):
        return self.tty

    def raw_bytes(self) -> bytes:
        self.flush()
        return self.buffer.getvalue()


def test_main_opens_the_browser_with_the_folder(tmp_path, monkeypatch, capsys):
    fake = FakeServer()
    opened = []
    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: fake)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    assert cli.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "CodeVision이 열렸어요: http://127.0.0.1:8765  (끄려면 Ctrl+C)" in out
    assert "사용 중" not in out
    assert opened == ["http://127.0.0.1:8765/?path=" + quote(str(tmp_path), safe="")]
    assert fake.closed


def test_main_without_browser_and_with_a_bad_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: FakeServer())
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    assert cli.main(["--no-browser"]) == 0
    assert opened == []
    assert cli.main([str(tmp_path / "없는 폴더")]) == 2
    assert "폴더를 찾을 수 없어요" in capsys.readouterr().err


def test_main_says_which_port_it_used_when_the_asked_one_was_busy(monkeypatch, capsys):
    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: FakeServer(port=8800))
    assert cli.main(["--port", "8799", "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "8799번 포트가 사용 중이라 8800번으로 열었어요\n" in out
    assert out.index("사용 중") < out.index("CodeVision이 열렸어요: http://127.0.0.1:8800  (끄려면 Ctrl+C)")

    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: FakeServer(port=8799))
    assert cli.main(["--port", "8799", "--no-browser"]) == 0
    assert "사용 중" not in capsys.readouterr().out
    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: FakeServer(port=51234))
    assert cli.main(["--port", "0", "--no-browser"]) == 0  # 0 means "any free port": nothing to explain
    assert "사용 중" not in capsys.readouterr().out


def test_main_writes_utf8_when_the_output_is_not_a_console(tmp_path, monkeypatch):
    out, err = Stream("cp949", tty=False), Stream("cp949", tty=False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: FakeServer(port=8800))

    assert cli.main(["--port", "8799", "--no-browser"]) == 0
    assert cli.main([str(tmp_path / "없는 폴더")]) == 2
    for stream in (out, err):
        assert (stream.encoding, stream.errors) == ("utf-8", "replace")
    text = out.raw_bytes().decode("utf-8")
    assert "8799번 포트가 사용 중이라 8800번으로 열었어요" in text
    assert "CodeVision이 열렸어요" in text and "CodeVision을 껐어요." in text
    assert "폴더를 찾을 수 없어요" in err.raw_bytes().decode("utf-8")


def test_main_keeps_the_console_encoding_but_never_crashes_on_it(monkeypatch):
    out, err = Stream("cp949", tty=True), Stream("cp949", tty=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: FakeServer())

    assert cli.main(["--no-browser"]) == 0
    for stream in (out, err):
        assert (stream.encoding, stream.errors) == ("cp949", "replace")
    print("이모지 🙂", file=out)  # cp949 has no emoji: replaced instead of a crash
    assert "CodeVision이 열렸어요" in out.raw_bytes().decode("cp949")


def test_main_runs_without_any_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)  # pythonw.exe or a windowed build
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(cv_server, "create_server", lambda **kwargs: FakeServer(port=8800))
    assert cli.main(["--port", "8799", "--no-browser"]) == 0


def read_until(stream, marker: bytes, timeout: float = 30) -> bytes:
    """Collect a child's output until it contains marker, without hanging if it never does."""
    lines = queue.Queue()

    def pump():
        for line in iter(stream.readline, b""):
            lines.put(line)
        lines.put(b"")

    threading.Thread(target=pump, daemon=True).start()
    seen = b""
    deadline = time.monotonic() + timeout
    while marker not in seen and time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if not line:
            break
        seen += line
    return seen


def test_a_captured_log_is_utf8_and_names_the_port_really_used(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")}
    env.update(PYTHONPATH=str(SRC), CODEVISION_HOME=str(tmp_path))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        # -X utf8=0: the plain Windows default, where a pipe gets the ANSI code page (cp949)
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8=0", "-m", "codevision", "--no-browser", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, creationflags=NO_WINDOW)
        try:
            raw = read_until(proc.stdout, b"Ctrl+C")
            text = raw.decode("utf-8")  # strict: cp949 bytes would fail here
            busy_line = next(line for line in text.splitlines() if "사용 중이라" in line)
            assert busy_line.startswith(f"{port}번 포트가 사용 중이라 ") and busy_line.endswith("번으로 열었어요")
            used = int(busy_line.split("사용 중이라 ")[1].split("번")[0])
            assert used != port
            assert f"CodeVision이 열렸어요: http://127.0.0.1:{used}  (끄려면 Ctrl+C)" in text.splitlines()
            assert text.index(busy_line) < text.index("CodeVision이 열렸어요")
            conn = http.client.HTTPConnection("127.0.0.1", used, timeout=10)
            conn.request("GET", "/")
            assert conn.getresponse().status == 200  # the port it names is the one it serves
            conn.close()
        finally:
            proc.kill()
            proc.wait(10)
            proc.stdout.close()
