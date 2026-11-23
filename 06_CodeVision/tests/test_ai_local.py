"""Unit tests for the local AI pieces: secret masking, path safety, the Ollama client, the fake Ollama.

Every network call goes to tests/fake_ollama.py or a tiny server on another loopback port.
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from codevision.explain import messages as M
from codevision.explain import ollama, safety
from codevision.explain.base import AIError, FileContext
from fake_ollama import FakeOllama, answer_for, find_signatures, read_prompt, shape_answer

TESTS = Path(__file__).resolve().parent
MASK = safety.MASK


# --- secret files ----------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.production", "backend/.env.example", "prod.env", ".envrc", ".npmrc", ".pypirc",
    ".netrc", "id_rsa", "id_ed25519.pub", "keys/server.pem", "cert.key", "store.p12", "app.pfx", "release.keystore",
    "secrets.py", "secret.js", "credentials.json", "config/credentials", "client_secret_123.json",
    "service-account.json", "infra/terraform.tfstate", "prod.tfvars", ".ssh/config", "home/.aws/config",
    ".docker/config.json", ".git-credentials", "Secrets.PY",
])
def test_secret_files(path):
    assert safety.is_secret_file(path)


@pytest.mark.parametrize("path", [
    "main.py", "env.py", "environment.ts", "src/tokens.js", "token.py", "keyboard.py", "keys.py", "api/auth.py",
    "README.md", "package.json", "settings.py", "src/secretary.js", "docs/environment.md",
])
def test_ordinary_files(path):
    assert not safety.is_secret_file(path)


# --- masking -----------------------------------------------------------------------------------

def test_private_key_blocks_are_masked_and_the_rest_stays():
    text = ('KEY = """-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\n-----END RSA PRIVATE KEY-----"""\n'
            "def helper(x):\n    return x\n"
            "PGP = '-----BEGIN PGP PRIVATE KEY BLOCK-----\\nlQOYBGabc\\n-----END PGP PRIVATE KEY BLOCK-----'\n"
            "after = 1\n")
    masked = safety.mask_secrets(text)
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEF" not in masked and "lQOYBGabc" not in masked
    assert "def helper(x):\n    return x\n" in masked and "after = 1" in masked
    assert masked.count(MASK) == 2
    cut = safety.mask_secrets("x = 1\n-----BEGIN PRIVATE KEY-----\nMIIabc\n")  # no END line: masked to the end
    assert cut == "x = 1\n" + MASK


@pytest.mark.parametrize("secret", [
    "ghp_" + "A1b2C3d4" * 5, "github_pat_" + "11ABCDEFG0" * 4, "glpat-" + "x" * 20, "sk-" + "a1B2" * 10,
    "sk-proj-" + "Zz9" * 12, "sk-ant-api03-" + "q" * 30, "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "AKIAIOSFODNN7EXAMPLE", "AIza" + "S" * 35, "xoxb-123456789012-abcdefghijkl", "hf_" + "a" * 34,
    "npm_" + "b" * 36, "SG." + "c" * 22 + "." + "d" * 43,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX",
])
def test_well_known_tokens_are_masked(secret):
    masked = safety.mask_secrets(f"value = call({secret!r})\n")
    assert secret not in masked and MASK in masked


@pytest.mark.parametrize("line, kept", [
    ('API_KEY = "abc12345xyz"', 'API_KEY = "{}"'),
    ("password='hunter22'", "password='{}'"),
    ('const apiKey = "abc12345"', 'const apiKey = "{}"'),
    ('const token: string = "abc12345"', 'const token: string = "{}"'),
    ('  "client_secret": "abc12345",', '  "client_secret": "{}",'),
    ('connect(user="me", password="abc12345")', 'connect(user="me", password="{}")'),
    ("SECRET_KEY = b'django-insecure-abc'", "SECRET_KEY = b'{}'"),
    ("db_passwd := `p4ssw0rd`", "db_passwd := `{}`"),
    ('url = "postgres://app:s3cr3t-pw@db:5432/app"', 'url = "postgres://app:{}@db:5432/app"'),
    ("headers = {'Authorization': 'Bearer abcdefghijklmnop123456'}", "headers = {'Authorization': 'Bearer {}'}"),
    ('conn = "Server=x;Database=y;Password=hunter22;"', 'conn = "Server=x;Database=y;Password={};"'),
])
def test_secret_assignments_are_masked(line, kept):
    assert safety.mask_secrets(line) == kept.replace("{}", MASK)


@pytest.mark.parametrize("line", [
    'password = input("Password: ")',
    'login(user, password=get_password("me"))',
    "password=pw,",
    'token = request.headers["X-Token"]',
    "def check_password(password):",
    'label = "비밀번호를 입력해 주세요"',
    "fetch('/api/todos', { method: 'POST' })",
    "const KEY_CODES = { enter: 13 }",
    'TOKEN_KEY = "cv"',  # too short to be a secret
    "http://localhost:8000/path",
    "pass",
])
def test_ordinary_code_is_left_alone(line):
    assert safety.mask_secrets(line) == line


def test_masking_a_big_file_is_quick():
    text = ("def f%d(a, b):\n    token = compute(a)\n    return a + b  # password check\n" * 30_000) % tuple(range(30_000))
    started = time.monotonic()
    assert safety.mask_secrets(text) == text
    assert time.monotonic() - started < 5
    blob = "A" * 1_000_000
    started = time.monotonic()
    safety.mask_secrets(blob)
    assert time.monotonic() - started < 5


# --- paths ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "..", "../x.py", "a/../../x.py", "a/./b.py", "./a.py", "a//b.py", "/etc/passwd", "\\\\server\\share\\x.py",
    "a\\..\\..\\x.py", "C:/Windows/win.ini", "C:x.py", "a\x00.py", "", "a/", "//server/share",
])
def test_unsafe_node_ids(tmp_path, rel):
    if rel == "":
        assert safety.safe_path(tmp_path, rel) == Path(os.path.realpath(tmp_path))  # "" is the root itself
        return
    if os.name != "nt" and rel in ("C:x.py", "C:/Windows/win.ini", "a\\..\\..\\x.py", "\\\\server\\share\\x.py"):
        pytest.skip("an ordinary file name outside Windows")
    with pytest.raises(safety.UnsafePath):
        safety.safe_path(tmp_path, rel)


def test_safe_paths_and_reading(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"print(1)\n")
    assert safety.safe_path(tmp_path, "src/a.py") == Path(os.path.realpath(tmp_path / "src" / "a.py"))
    assert safety.safe_path(tmp_path, ".") == Path(os.path.realpath(tmp_path))
    assert safety.read_file(tmp_path, "src/a.py", 100) == b"print(1)\n"
    with pytest.raises(safety.TooBig):
        safety.read_file(tmp_path, "src/a.py", 4)
    with pytest.raises(OSError):
        safety.read_file(tmp_path, "src", 100)  # a folder is not a file
    with pytest.raises(OSError):
        safety.read_file(tmp_path, "src/gone.py", 100)


def test_links_that_leave_the_folder(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "x.py").write_bytes(b"x = 1\n")
    (root / "inside").mkdir()
    (root / "inside" / "y.py").write_bytes(b"y = 1\n")
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(root / "out"))
            _winapi.CreateJunction(str(root / "inside"), str(root / "in"))
        else:
            os.symlink(outside, root / "out")
            os.symlink(root / "inside", root / "in")
    except (ImportError, OSError, AttributeError) as exc:
        pytest.skip(f"cannot make links here: {exc}")
    with pytest.raises(safety.UnsafePath):
        safety.safe_path(root, "out/x.py")
    with pytest.raises(safety.UnsafePath):
        safety.safe_path(root, "out")
    with pytest.raises(safety.UnsafePath):  # like the scanner, links are never followed, even inside
        safety.read_file(root, "in/y.py", 100)
    assert safety.read_file(root, "inside/y.py", 100) == b"y = 1\n"


def test_a_file_swapped_after_the_check_is_refused(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_bytes(b"a = 1\n")
    files = safety.ProjectFiles(tmp_path)
    files.path("a.py")
    real_fstat = os.fstat

    def other_file(fd):
        st = real_fstat(fd)
        return os.stat_result((st.st_mode, st.st_ino + 1) + tuple(st)[2:])

    monkeypatch.setattr(safety.os, "fstat", other_file)
    with pytest.raises(safety.UnsafePath):
        files.read("a.py", 100)


# --- the Ollama client ------------------------------------------------------------------------------

def test_client_talks_to_the_fake(fake_ollama):
    client = ollama.OllamaClient(fake_ollama.url)
    assert client.version() == "0.32.6" and client.is_up()
    assert client.tags() == [{"name": "qwen2.5:7b", "size": 4683087332, "params": "7.6B"},
                             {"name": "gemma3:4b", "size": 3338801804, "params": "4.3B"}]
    text = client.chat("gemma3:4b", "시스템", "파일 경로: a/b.py\n주요 함수: run()")
    assert json.loads(text)["f"] == {"run()": "run 일을 해요."}
    with pytest.raises(ollama.OllamaHTTPError) as caught:
        client.chat("llama3:70b", "s", "u")
    assert caught.value.status == 404 and "not found" in caught.value.message


def test_a_stopped_ollama_is_noticed_at_once():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))  # bound, never listening: every connection is refused
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        client = ollama.OllamaClient(url)
        started = time.monotonic()
        assert client.is_up() is False
        with pytest.raises(ollama.OllamaUnreachable):
            client.tags()
        assert time.monotonic() - started < 1.5  # Windows would retry a refused connection for ~2 s each
        explainer = ollama.OllamaExplainer(url, "qwen2.5:7b")
        with pytest.raises(AIError) as caught:
            explainer.explain_file(FileContext("a.py", "python", "x = 1"))
        assert caught.value.code == "not_running" and caught.value.message == M.NOT_RUNNING


def test_proxies_are_never_used(fake_ollama, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert ollama.OllamaClient(fake_ollama.url).version() == "0.32.6"


class _Redirect(http.server.BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *args):
        pass


def test_redirects_are_never_followed(fake_ollama):
    _Redirect.target = fake_ollama.url + "/api/version"
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        client = ollama.OllamaClient(f"http://127.0.0.1:{httpd.server_address[1]}")
        with pytest.raises(ollama.OllamaHTTPError) as caught:
            client.version()
        assert caught.value.status == 302
        assert fake_ollama.calls == []  # the redirect target was never asked
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_names_that_resolve_elsewhere_are_refused(monkeypatch):
    connects = []
    monkeypatch.setattr(ollama.socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 11500))])
    real_connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: (connects.append(addr), real_connect(self, addr)))
    with pytest.raises(ollama.OllamaUnreachable):
        ollama.OllamaClient("http://localhost:11500").version()
    assert connects == []  # not even tried


def test_client_errors_map_to_contract_codes(fake_ollama):
    explainer = ollama.OllamaExplainer(fake_ollama.url, "qwen2.5:7b", answer_timeout=0.5)
    ctx = FileContext("a.py", "python", "def run(): pass", symbols=["run()"])
    cases = [("500", "failed", M.FAILED), ("badjson", "bad_answer", M.BAD_ANSWER), ("drop", "failed", M.FAILED)]
    for fail, code, message in cases:
        fake_ollama.set(fail=fail, fail_count=1)
        with pytest.raises(AIError) as caught:
            explainer.explain_file(ctx)
        assert (caught.value.code, caught.value.message) == (code, message), fail
    fake_ollama.set(fail="slow", slow=1.5, fail_count=1)
    with pytest.raises(AIError) as caught:
        explainer.explain_file(ctx)
    assert caught.value.code == "timeout" and caught.value.status == 504
    time.sleep(1.2)  # let the fake finish the slow answer
    missing = ollama.OllamaExplainer(fake_ollama.url, "llama3:70b")
    with pytest.raises(AIError) as caught:
        missing.explain_file(ctx)
    assert caught.value.code == "no_model"
    assert explainer.explain_file(ctx).f == {"run()": "run 일을 해요."}


def test_the_answer_format_is_the_shared_schema_or_the_prompts_own(fake_ollama, monkeypatch):
    from codevision.explain import prompts
    explainer = ollama.OllamaExplainer(fake_ollama.url, "qwen2.5:7b")
    ctx = FileContext("a.py", "python", "def run(): pass", symbols=["run()"])
    explainer.explain_file(ctx)
    assert fake_ollama.chats()[-1]["format"] == prompts.ANSWER_SCHEMA
    own = {"type": "object", "properties": {"s": {"type": "string"}, "d": {"type": "string"},
                                            "f": {"type": "object", "properties": {"run()": {"type": "string"}},
                                                  "additionalProperties": False}},
           "required": ["s", "d", "f"]}
    monkeypatch.setattr(prompts, "answer_schema", lambda c: own if c is ctx else None, raising=False)
    assert explainer.explain_file(ctx).f == {"run()": "run 일을 해요."}
    assert fake_ollama.chats()[-1]["format"] == own  # a per-file schema, when prompts.py offers one

    def broken(c):
        raise RuntimeError("no schema today")

    monkeypatch.setattr(prompts, "answer_schema", broken, raising=False)
    explainer.explain_file(ctx)
    assert fake_ollama.chats()[-1]["format"] == prompts.ANSWER_SCHEMA


def test_loaded_models_and_letting_go(fake_ollama):
    client = ollama.OllamaClient(fake_ollama.url)
    assert client.loaded() == []
    client.chat("gemma3:4b", "시스템", "파일 경로: a.py")
    assert client.loaded() == ["gemma3:4b"]
    client.unload("gemma3:4b")
    assert client.loaded() == [] and len(fake_ollama.chats()) == 1  # letting go is not an answer


# --- the fake Ollama ------------------------------------------------------------------------------------

def _get(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read().decode("utf-8")


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read().decode("utf-8")


def test_fake_answers_like_ollama():
    with FakeOllama(models=["qwen2.5:7b"], cold=0.3) as fake:
        assert _get(fake.url + "/")[2] == "Ollama is running"
        assert json.loads(_get(fake.url + "/api/version")[2]) == {"version": "0.32.6"}
        tags = json.loads(_get(fake.url + "/api/tags")[2])["models"]
        assert tags[0]["name"] == "qwen2.5:7b" and tags[0]["details"]["parameter_size"] == "7.6B"
        assert json.loads(_get(fake.url + "/api/ps")[2]) == {"models": []}

        body = {"model": "qwen2.5:7b", "stream": False, "messages": [{"role": "user", "content": "파일 경로: x.py"}]}
        started = time.monotonic()
        reply = json.loads(_post(fake.url + "/api/chat", body)[2])
        assert time.monotonic() - started >= 0.3  # cold: the model had to load
        assert reply["done"] is True and reply["done_reason"] == "stop" and reply["message"]["role"] == "assistant"
        assert reply["message"]["content"].startswith("```json")  # no "format": fenced JSON, like a real model
        assert json.loads(_get(fake.url + "/api/ps")[2])["models"][0]["name"] == "qwen2.5:7b"
        started = time.monotonic()
        _post(fake.url + "/api/chat", body)
        assert time.monotonic() - started < 0.3  # warm now

        status, ctype, text = _post(fake.url + "/api/chat", {**body, "stream": True})  # Ollama's default
        lines = [json.loads(line) for line in text.splitlines()]
        assert ctype == "application/x-ndjson" and lines[-1]["done"] is True and not lines[0]["done"]
        assert json.loads("".join(line["message"]["content"] for line in lines[:-1]).strip("`json\n"))["s"]

        reply = json.loads(_post(fake.url + "/api/generate", {"model": "qwen2.5:7b", "prompt": "폴더 경로: src",
                                                              "stream": False, "format": "json"})[2])
        assert json.loads(reply["response"])["s"] == "src 폴더의 파일 모음"
        unload = json.loads(_post(fake.url + "/api/chat", {"model": "qwen2.5:7b", "messages": [], "keep_alive": 0})[2])
        assert unload["done_reason"] == "unload"
        assert json.loads(_get(fake.url + "/api/ps")[2]) == {"models": []}
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(fake.url + "/api/chat", {"model": "nope", "messages": [{"role": "user", "content": "x"}]})
        assert caught.value.code == 404
        assert len(fake.chats()) == 5 and fake.answered == 4


def test_fake_control_endpoints():
    with FakeOllama() as fake:
        assert json.loads(_post(fake.url + "/__fake/set", {"fail": "500", "fail_count": 1})[2]) == {"ok": True}
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(fake.url + "/api/chat", {"model": "gemma3:4b", "stream": False,
                                           "messages": [{"role": "user", "content": "x"}]})
        assert caught.value.code == 500
        assert _post(fake.url + "/api/chat", {"model": "gemma3:4b", "stream": False,
                                              "messages": [{"role": "user", "content": "x"}]})[0] == 200
        calls = json.loads(_get(fake.url + "/__fake/calls")[2])
        assert [c["path"] for c in calls if c["path"].startswith("/api/")] == ["/api/chat", "/api/chat"]
        _post(fake.url + "/__fake/reset", {})
        assert len(json.loads(_get(fake.url + "/__fake/calls")[2])) == 1  # only this request
        with pytest.raises(urllib.error.HTTPError):
            _post(fake.url + "/__fake/set", {"fail": "sometimes"})


def test_fake_reads_the_signatures_in_any_listing_style():
    assert find_signatures("주요 함수: a(x, y), b(), class C") == ["a(x, y)", "b()", "class C"]
    assert find_signatures('주요 함수: ["add(cart, item)", "total(cart)"]') == ["add(cart, item)", "total(cart)"]
    assert find_signatures("주요 함수:\n- add(a, b): 더해요\n- `class Todo`\n\n다음 글") == ["add(a, b)", "class Todo"]
    assert find_signatures("주요 함수: 없음") == []
    assert find_signatures("아무 목록도 없어요") == []
    framed = read_prompt('파일 경로: a.py\n틀: {"s": "", "d": "", "f": {"run(x)": "", "class A": ""}}')
    assert framed["signatures"] == ["run(x)", "class A"]  # no 주요 함수 line: the answer frame's keys
    assert read_prompt("폴더 경로: (프로젝트 맨 위 폴더)\n안에 든 것:\n- a: 요약\n- b: 요약")["children"] == 2
    schema = {"type": "object", "properties": {"s": {"type": "string"}, "f": {
        "type": "array", "items": {"type": "object", "properties": {"sig": {}, "text": {}}}}, "n": {"type": "integer"}}}
    shaped = shape_answer(answer_for("파일 경로: a.py\n주요 함수: run()"), schema)
    assert shaped == {"s": "a 일을 맡은 파일", "f": [{"sig": "run()", "text": "run 일을 해요."}], "n": 0}


def test_fake_refuses_the_real_ollama_port_and_other_hosts():
    with pytest.raises(ValueError):
        FakeOllama(port=11434)
    with pytest.raises(ValueError):
        FakeOllama(host="0.0.0.0")


def test_fake_serve_mode_needs_ollama_host(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "OLLAMA_HOST"}
    proc = subprocess.run([sys.executable, str(TESTS / "fake_ollama.py"), "serve"], env=env, capture_output=True,
                          timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 2 and b"OLLAMA_HOST" in proc.stderr
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    proc = subprocess.run([sys.executable, str(TESTS / "fake_ollama.py"), "serve"], env=env, capture_output=True,
                          timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 1 and b"11434" in proc.stderr  # never the real Ollama's port
