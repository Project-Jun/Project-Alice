"""Server tests for local AI explanations (/api/ai/*), always against tests/fake_ollama.py.

The analyzer is replaced by a fixed graph of a small project written to tmp_path, so these
tests do not depend on the analyzers. One test at the end uses the real analyzer on the demo.
Nothing here reaches the real Ollama (conftest.py refuses port 11434 and the real program).
"""
import contextlib
import http.client
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from codevision import explain
from codevision import server as cv_server
from codevision.explain import messages as M
from codevision.explain import prompts, service
from codevision.explain.base import FileContext
from fake_ollama import answer_for, read_prompt, shape_answer

TESTS = Path(__file__).resolve().parent
FAKE_SCRIPT = TESTS / "fake_ollama.py"
SECRET_TOKEN = "sk-live-0123456789abcdefghijklmnopqrstuv"
SECRET_PASSWORD = "hunter2-very-secret"
SECRET_KEY_LINE = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
SECRET_DB_PASSWORD = "pa55w0rd-db"

FILES = {
    "main.py": (
        "import os\n"
        f'API_KEY = "{SECRET_TOKEN}"\n'
        f'password = "{SECRET_PASSWORD}"\n'
        f'DB_URL = "postgres://app:{SECRET_DB_PASSWORD}@localhost/app"\n'
        'KEY = """-----BEGIN RSA PRIVATE KEY-----\n'
        f"{SECRET_KEY_LINE}\n"
        '-----END RSA PRIVATE KEY-----"""\n\n'
        "def main():\n    helper(1)\n\n"
        "def helper(x):\n    return x + 1\n"
    ),
    "app/api.js": "export function fetchTodos() { return fetch('/todos'); }\nexport async function addTodo(title) {}\n",
    "app/util.py": "def slug(text):\n    return text.lower()\n",
    "README.md": "# demo\n",
    ".env": f"OPENAI_API_KEY={SECRET_TOKEN}\n",
    "secrets.py": f'TOKEN = "{SECRET_TOKEN}"\n',
    "big.py": "x = 1\n" * 50,
}


def project_graph(root: Path) -> dict:
    """What the analyzer would say about the files above (rule-based texts)."""
    name = root.name
    return {
        "version": 1,
        "source": {"kind": "local", "path": str(root)},
        "name": name,
        "root": ".",
        "nodes": {
            ".": {"n": name, "p": name + "/", "t": "d", "a": "ot", "s": "규칙 루트 요약", "d": "규칙 루트 설명",
                  "k": ["app", ".env", "big.py", "main.py", "README.md", "secrets.py"]},
            "app": {"n": "app", "p": "app/", "t": "d", "a": "ui", "s": "규칙 앱 요약", "d": "규칙 앱 설명",
                    "k": ["app/api.js", "app/util.py"]},
            "app/api.js": {"n": "api.js", "p": "app/api.js", "t": "f", "a": "ui", "l": 2, "s": "서버 요청 함수",
                           "d": "규칙 api 설명", "f": [["fetchTodos()", "목록을 가져와요."], ["addTodo(title)", ""]],
                           "u": [], "b": [], "lang": "javascript"},
            "app/util.py": {"n": "util.py", "p": "app/util.py", "t": "f", "a": "sv", "l": 2, "s": "도우미",
                            "d": "규칙 util 설명", "f": [["slug(text)", "규칙 slug 설명"]], "u": [], "b": [],
                            "lang": "python"},
            "main.py": {"n": "main.py", "p": "main.py", "t": "f", "a": "sv", "l": 12, "s": "시작 파일",
                        "d": "규칙 main 설명", "f": [["main()", ""], ["helper(x)", "도움 함수"]], "u": [], "b": [],
                        "lang": "python", "e": 1},
            "README.md": {"n": "README.md", "p": "README.md", "t": "f", "a": "ot", "l": 1, "s": "소개 문서",
                          "d": "규칙 readme 설명", "f": [], "u": [], "b": []},
            ".env": {"n": ".env", "p": ".env", "t": "f", "a": "ot", "l": 1, "s": "환경 변수", "d": "설정",
                     "f": [], "u": [], "b": []},
            "secrets.py": {"n": "secrets.py", "p": "secrets.py", "t": "f", "a": "sv", "l": 1, "s": "비밀 값",
                           "d": "규칙 secrets 설명", "f": [], "u": [], "b": [], "lang": "python"},
            "big.py": {"n": "big.py", "p": "big.py", "t": "f", "a": "sv", "l": 50, "s": "큰 파일",
                       "d": "규칙 big 설명", "f": [], "u": [], "b": [], "lang": "python"},
        },
    }


class Client:
    def __init__(self, httpd, root, built, fake, home):
        self.httpd, self.root, self.built, self.fake, self.home = httpd, root, built, fake, home

    @property
    def project(self) -> str:
        return str(self.root)

    def call(self, method, path, data=None, *, token=True, timeout=30):
        headers = {"X-CV-Token": self.httpd.token} if token else {}
        body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
            assert resp.headers["Content-Security-Policy"] == cv_server.CSP
            return resp.status, json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.call("POST", path, {} if data is None else data, **kw)

    def analyze(self):
        status, graph = self.get("/api/analyze?path=" + _quote(self.project))
        assert status == 200, graph
        return graph

    def enable(self, **extra):
        status, data = self.post("/api/ai/settings", {"enabled": True, **extra})
        assert status == 200, data
        return data

    def explain(self, node, project=None):
        return self.post("/api/ai/explain", {"project": project or self.project, "node": node})

    def start_job(self):
        return self.post("/api/ai/explain-all", {"project": self.project})

    def wait_job(self, job_id, until=("done", "cancelled", "failed"), timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            status, block = self.get(f"/api/ai/job?id={job_id}")
            assert status == 200, block
            if block["state"] in until or time.monotonic() > deadline:
                return block
            time.sleep(0.02)


def _quote(text):
    from urllib.parse import quote
    return quote(text, safe="")


@contextlib.contextmanager
def serving(httpd):
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def write_files(root: Path) -> Path:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def cv(tmp_path, monkeypatch, fake_ollama):
    """A CodeVision server on a free port, its AI pointed at a running fake Ollama, AI still off."""
    home = tmp_path / "home"
    monkeypatch.setenv("CODEVISION_HOME", str(home))
    root = write_files(tmp_path / "proj")
    built = []
    graph_module = types.ModuleType("codevision.graph")

    def build(path):
        built.append(str(path))
        return project_graph(Path(path))

    graph_module.build = build
    monkeypatch.setitem(sys.modules, "codevision.graph", graph_module)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>{{CV_TOKEN}}", encoding="utf-8")
    httpd = cv_server.create_server(port=0, web_dir=web)
    with serving(httpd):
        yield Client(httpd, root, built, fake_ollama, home)


def answers(fake) -> list[str]:
    """The node of every answer request, in arrival order ("." for the project root)."""
    out = []
    for text in fake.prompts():
        info = read_prompt(text)
        root = info["kind"] == "folder" and (not info["path"] or info["path"].startswith("("))
        out.append("." if root else info["path"])
    return out


def expected_x(cv, node_id: str, prompt: str) -> dict:
    """x for a file the fake answered: the fake's answer through prompts.parse_response/postprocess."""
    node = project_graph(cv.root)["nodes"][node_id]
    content = json.dumps(shape_answer(answer_for(prompt, prompts.ANSWER_SCHEMA), prompts.ANSWER_SCHEMA),
                         ensure_ascii=False)
    ctx = FileContext(rel_path=node_id, language=node["lang"], source=FILES[node_id], summary_hint=node["s"],
                      symbols=[sig for sig, _ in node["f"]], uses=node["u"], used_by=node["b"])
    return service.make_x(node, prompts.postprocess(prompts.parse_response(content), ctx))


# --- status, settings -----------------------------------------------------------------------

def test_status_with_ollama_running_but_ai_off(cv):
    status, block = cv.get("/api/ai/status")
    assert status == 200
    assert block == {
        "provider": "local", "enabled": False, "available": False, "model": "qwen2.5:7b",
        "models": [{"name": "qwen2.5:7b", "size": 4683087332, "params": "7.6B"},
                   {"name": "gemma3:4b", "size": 3338801804, "params": "4.3B"}],
        "recommended": list(prompts.RECOMMENDED_MODELS), "ollama": "running", "can_start": False,
        "message": M.OFF,
    }
    assert cv.fake.chats() == []


def test_enabling_saves_the_choice_and_makes_ai_available(cv):
    block = cv.enable()
    assert block["enabled"] is True and block["available"] is True
    assert block["model"] == "qwen2.5:7b" and block["message"] == M.READY.format(model="qwen2.5:7b")
    config = json.loads((cv.home / "config.json").read_text(encoding="utf-8"))
    assert config["ai"] == {"provider": "local", "enabled": True, "model": "qwen2.5:7b",
                            "base_url": "http://127.0.0.1:11434"}  # the automatic choice is kept
    assert sorted(p.name for p in cv.home.iterdir()) == ["config.json"]  # no temporary files left

    status, block = cv.post("/api/ai/settings", {"model": "gemma3:4b"})
    assert status == 200 and block["model"] == "gemma3:4b" and block["available"] is True
    status, block = cv.post("/api/ai/settings", {"enabled": False})
    assert status == 200 and block["enabled"] is False and block["available"] is False and block["message"] == M.OFF
    assert cv.get("/api/ai/status")[1]["model"] == "gemma3:4b"
    assert cv.fake.chats() == []  # settings never load a model


@pytest.mark.parametrize("body, error", [
    ({"model": "nope:1b"}, "설치되지 않은 모델이에요: nope:1b"),
    ({"enabled": True, "model": "bad name!"}, M.BAD_MODEL),
    ({"model": 7}, M.BAD_MODEL),
    ({"model": "x" * 300}, M.BAD_MODEL),
    ({"enabled": "yes"}, M.BAD_REQUEST),
    ({}, M.BAD_REQUEST),
    ({"colour": "red"}, M.BAD_REQUEST),
    (b"[1, 2]", M.BAD_REQUEST),
    (b"{broken", M.BAD_REQUEST),
    (b"\xff\xfe", M.BAD_REQUEST),
])
def test_bad_settings_are_400s(cv, body, error):
    status, data = cv.call("POST", "/api/ai/settings", body)
    assert status == 400 and data == {"error": error}
    assert not (cv.home / "config.json").exists()


def test_a_model_that_is_not_installed(cv, monkeypatch):
    cv.enable()
    providers = explain.providers
    providers.update_ai(model="llama3:70b")  # chosen earlier, since deleted from Ollama
    cv.httpd.ai.invalidate()
    block = cv.get("/api/ai/status")[1]
    assert block["available"] is False and block["model"] == "llama3:70b"
    assert block["message"] == M.MODEL_MISSING.format(model="llama3:70b")
    assert cv.explain("app/api.js") == (409, {"error": M.NO_CHOSEN_MODEL, "code": "no_model"})
    assert cv.start_job()[0] == 409

    cv.fake.set(models=[])
    providers.update_ai(model="")
    cv.httpd.ai.invalidate()
    block = cv.get("/api/ai/status")[1]
    assert block["model"] == "" and block["models"] == [] and block["message"] == M.NO_MODELS
    assert cv.explain("app/api.js") == (409, {"error": M.NO_MODEL, "code": "no_model"})
    assert cv.fake.chats() == []


def test_ollama_not_running(cv, monkeypatch):
    cv.enable()
    monkeypatch.setenv("CODEVISION_OLLAMA_URL", "http://127.0.0.1:1")  # nothing listens on port 1
    cv.httpd.ai.invalidate()
    started = time.monotonic()
    block = cv.get("/api/ai/status")[1]
    assert time.monotonic() - started < 2  # a stopped Ollama is noticed at once
    assert block["ollama"] == "missing" and block["can_start"] is False  # conftest: no program either
    assert block["available"] is False and block["models"] == [] and block["message"] == M.MISSING
    assert block["model"] == "qwen2.5:7b"  # saved when AI was turned on

    monkeypatch.setenv("CODEVISION_OLLAMA_EXE", str(FAKE_SCRIPT))
    cv.httpd.ai.invalidate()
    block = cv.get("/api/ai/status")[1]
    assert block["ollama"] == "stopped" and block["can_start"] is True and block["message"] == M.STOPPED
    assert cv.explain("app/api.js") == (409, {"error": M.NOT_RUNNING, "code": "not_running"})
    assert cv.start_job() == (409, {"error": M.NOT_RUNNING, "code": "not_running"})


def test_the_automatic_model_is_kept_once_used(cv, monkeypatch):
    monkeypatch.setenv("CODEVISION_OLLAMA_URL", "http://127.0.0.1:1")  # Ollama is off while AI is turned on
    block = cv.enable()
    assert block["enabled"] is True and block["model"] == "" and block["available"] is False
    assert json.loads((cv.home / "config.json").read_text(encoding="utf-8"))["ai"]["model"] == ""
    monkeypatch.setenv("CODEVISION_OLLAMA_URL", cv.fake.url)  # Ollama is back
    cv.httpd.ai.invalidate()
    assert cv.get("/api/ai/status")[1]["model"] == "qwen2.5:7b"
    assert cv.explain("app/util.py")[0] == 200
    assert json.loads((cv.home / "config.json").read_text(encoding="utf-8"))["ai"]["model"] == "qwen2.5:7b"
    cv.fake.stop()
    cv.httpd.ai.invalidate()
    graph = cv.analyze()  # Ollama is off again, and the explanation made with qwen2.5:7b still shows
    assert graph["ai"]["model"] == "qwen2.5:7b" and "x" in graph["nodes"]["app/util.py"]


def test_provider_none_and_cloud(cv):
    (cv.home).mkdir(exist_ok=True)
    (cv.home / "config.json").write_text(json.dumps({"ai": {"provider": "none", "enabled": True}}), encoding="utf-8")
    block = cv.get("/api/ai/status")[1]
    assert (block["provider"], block["enabled"], block["available"], block["message"]) == ("none", False, False, M.NONE)
    assert block["ollama"] == "running"  # still shown, so the screen can offer local AI
    assert cv.explain("main.py")[1]["code"] == "off"

    (cv.home / "config.json").write_text(json.dumps({"ai": {"provider": "cloud", "enabled": True}}), encoding="utf-8")
    cv.httpd.ai.invalidate()
    block = cv.get("/api/ai/status")[1]
    assert (block["provider"], block["enabled"], block["message"]) == ("none", False, M.CLOUD)

    block = cv.enable()  # turning AI on from the screen picks the local AI
    assert block["provider"] == "local" and block["available"] is True


# --- explaining one node ----------------------------------------------------------------------

def test_explain_a_file_then_the_cache_answers(cv):
    cv.enable()
    status, reply = cv.explain("app/api.js")
    assert status == 200 and reply["node"] == "app/api.js" and reply["cached"] is False
    assert reply["x"] == expected_x(cv, "app/api.js", cv.fake.prompts()[0])
    assert [sig for sig, _ in reply["x"]["f"]] == ["fetchTodos()", "addTodo(title)"]
    assert reply["x"]["s"] and reply["x"]["s"] != "서버 요청 함수"  # the AI text, not the rule text

    body = cv.fake.chats()[0]
    assert body["model"] == "qwen2.5:7b" and body["stream"] is False and body["keep_alive"] == "10m"
    assert body["format"] == prompts.ANSWER_SCHEMA and body["options"] == prompts.MODEL_OPTIONS
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == prompts.SYSTEM_PROMPT
    assert "export function fetchTodos" in body["messages"][1]["content"]

    status, again = cv.explain("app/api.js")
    assert status == 200 and again == {**reply, "cached": True}
    assert len(cv.fake.chats()) == 1  # no second model call


def test_f_follows_the_node_signatures(cv):
    cv.enable()
    cv.fake.set(models=["qwen2.5:7b"])
    status, reply = cv.explain("main.py")
    assert status == 200
    assert [sig for sig, _ in reply["x"]["f"]] == ["main()", "helper(x)"]  # same signatures, same order
    assert reply["x"]["f"][0][1]  # main() had no rule text; the AI gave one


def test_explain_folders_bottom_up_from_children_summaries(cv):
    cv.enable()
    api = cv.explain("app/api.js")[1]["x"]
    status, reply = cv.explain("app")
    assert status == 200 and reply["cached"] is False
    folder_prompt = cv.fake.prompts()[-1]
    assert api["s"] in folder_prompt  # the AI summary of the explained child
    assert "도우미" in folder_prompt  # the rule summary of the child that has none
    assert reply["x"]["s"] and reply["x"]["s"] != "규칙 앱 요약" and reply["x"]["f"] == []

    status, root = cv.explain(".")
    assert status == 200 and root["x"]["s"] and root["x"]["f"] == []
    assert reply["x"]["s"] in cv.fake.prompts()[-1]
    assert cv.explain("app")[1]["cached"] is True
    assert len(cv.fake.chats()) == 3


def test_analyze_never_calls_the_model_and_adds_x_only_while_on(cv):
    graph = cv.analyze()
    assert graph["ai"]["enabled"] is False and not any("x" in n for n in graph["nodes"].values())
    cv.enable()
    graph = cv.analyze()
    assert graph["ai"]["available"] is True and not any("x" in n for n in graph["nodes"].values())
    assert cv.fake.chats() == []

    made = {node: cv.explain(node)[1]["x"] for node in ("app/api.js", "app/util.py", "app")}
    calls = len(cv.fake.chats())
    graph = cv.analyze()
    nodes = graph["nodes"]
    assert {i for i, n in nodes.items() if "x" in n} == {"app/api.js", "app/util.py", "app"}
    for node_id, x in made.items():
        assert nodes[node_id]["x"] == x
    assert nodes["app/api.js"]["s"] == "서버 요청 함수"  # the rule-based texts are never overwritten
    assert nodes["app/api.js"]["f"] == [["fetchTodos()", "목록을 가져와요."], ["addTodo(title)", ""]]
    assert len(cv.fake.chats()) == calls  # analyze asked the model nothing

    cv.fake.stop()  # Ollama goes away: what is cached still shows
    cv.httpd.ai.invalidate()
    graph = cv.analyze()
    assert graph["ai"]["ollama"] == "missing" and graph["ai"]["model"] == "qwen2.5:7b"
    assert {i for i, n in graph["nodes"].items() if "x" in n} == {"app/api.js", "app/util.py", "app"}

    cv.post("/api/ai/settings", {"enabled": False})
    graph = cv.analyze()
    assert not any("x" in n for n in graph["nodes"].values())


def test_a_changed_file_is_explained_again(cv):
    cv.enable()
    first = cv.explain("app/util.py")[1]
    (cv.root / "app" / "util.py").write_text("def slug(text):\n    return text.upper()\n", encoding="utf-8")
    second = cv.explain("app/util.py")[1]
    assert first["cached"] is False and second["cached"] is False
    assert len(cv.fake.chats()) == 2
    assert "text.upper()" in cv.fake.prompts()[-1]


@pytest.mark.parametrize("node, status, code, message", [
    (".env", 422, "secret", M.SECRET),
    ("secrets.py", 422, "secret", M.SECRET),
    ("README.md", 422, "not_code", M.NOT_CODE),
    ("nothing.py", 404, "not_found", M.NODE_NOT_FOUND),
    ("../proj/main.py", 404, "not_found", M.NODE_NOT_FOUND),
    ("C:/Windows/win.ini", 404, "not_found", M.NODE_NOT_FOUND),
])
def test_nodes_that_are_not_explained(cv, node, status, code, message):
    cv.enable()
    assert cv.explain(node) == (status, {"error": message, "code": code})
    assert cv.fake.chats() == []


def test_too_big_files(cv, monkeypatch):
    cv.enable()
    monkeypatch.setattr(explain, "MAX_SOURCE_BYTES", 100)
    assert cv.explain("big.py") == (422, {"error": M.TOO_BIG, "code": "too_big"})
    assert cv.explain("app/util.py")[0] == 200  # small files still go
    assert len(cv.fake.chats()) == 1


def test_secrets_are_never_sent(cv):
    cv.enable()
    status, reply = cv.explain("main.py")
    assert status == 200
    status, job = cv.start_job()
    assert status == 202
    assert cv.wait_job(job["job"])["state"] == "done"
    sent = cv.fake.raw_requests()
    assert "def helper(x)" in sent and "[가린 값]" in sent  # the code went, the secrets did not
    for secret in (SECRET_TOKEN, SECRET_PASSWORD, SECRET_KEY_LINE, SECRET_DB_PASSWORD, "OPENAI_API_KEY"):
        assert secret not in sent, secret
    paths = answers(cv.fake)
    assert ".env" not in paths and "secrets.py" not in paths


def test_bad_requests_are_400s(cv):
    cv.enable()
    for body in ({}, {"project": cv.project}, {"node": "main.py"}, {"project": "", "node": "main.py"},
                 {"project": cv.project, "node": ""}, {"project": cv.project, "node": 5}, b"[]", b"{"):
        status, data = cv.call("POST", "/api/ai/explain", body)
        assert status == 400 and set(data) == {"error"}, body
    status, data = cv.explain("main.py", project="relative/folder")
    assert status == 400 and "전체 경로" in data["error"]
    status, data = cv.explain("main.py", project=str(cv.root / "gone"))
    assert (status, data) == (404, {"error": M.PROJECT_NOT_FOUND, "code": "not_found"})
    assert cv.fake.chats() == []


def test_explain_when_ai_is_off(cv):
    assert cv.explain("main.py") == (409, {"error": M.EXPLAIN_OFF, "code": "off"})
    assert cv.start_job() == (409, {"error": M.EXPLAIN_OFF, "code": "off"})
    assert cv.built == []


# --- model failures ---------------------------------------------------------------------------

def test_a_slow_model_times_out(cv):
    cv.enable()
    cv.httpd.ai.answer_timeout = 0.5
    cv.fake.set(fail="slow", slow=2, fail_count=1)
    started = time.monotonic()
    assert cv.explain("app/util.py") == (504, {"error": M.TIMEOUT, "code": "timeout"})
    assert time.monotonic() - started < 2
    cv.httpd.ai.answer_timeout = 30  # the fake answers the next one once the slow one is over
    status, reply = cv.explain("app/util.py")
    assert status == 200 and reply["cached"] is False  # nothing broken was cached


@pytest.mark.parametrize("fail, status, code, message", [
    ("badjson", 502, "bad_answer", M.BAD_ANSWER),
    ("500", 502, "failed", M.FAILED),
    ("drop", 502, "failed", M.FAILED),
])
def test_model_failures(cv, fail, status, code, message):
    cv.enable()
    cv.fake.set(fail=fail, fail_count=1)
    assert cv.explain("app/util.py") == (status, {"error": message, "code": code})
    status2, reply = cv.explain("app/util.py")  # the next try works and is not a cache hit
    assert status2 == 200 and reply["cached"] is False


def test_an_empty_answer_is_a_bad_answer(cv, monkeypatch):
    cv.enable()
    monkeypatch.setattr(prompts, "postprocess", lambda exp, ctx: explain.Explanation())
    assert cv.explain("app/util.py") == (502, {"error": M.BAD_ANSWER, "code": "bad_answer"})


def test_ollama_stopping_before_a_call(cv):
    cv.enable()
    cv.get("/api/ai/status")
    cv.fake.stop()  # the cached status still says "running"
    assert cv.explain("app/util.py") == (409, {"error": M.NOT_RUNNING, "code": "not_running"})


# --- jobs -------------------------------------------------------------------------------------------

def test_explain_all_order_files_first_then_folders_root_last(cv):
    cv.enable()
    status, job = cv.start_job()
    assert status == 202 and job["state"] == "running" and job["total"] == 6 and job["message"] == M.JOB_STARTED
    block = cv.wait_job(job["job"])
    order = [node for node, _ in block["results"]]
    assert order == ["app/api.js", "app/util.py", "main.py", "big.py", "app", "."]  # .env and secrets.py never go
    assert answers(cv.fake) == order
    assert block["state"] == "done" and block["errors"] == 0 and block["message"] == M.JOB_DONE
    assert block["total"] == 6 and block["done"] == 6 and block["next"] == 6 and block["current"] == ""
    assert set(block) == {"job", "state", "done", "total", "errors", "current", "message", "results", "next"}
    later = cv.get(f"/api/ai/job?id={job['job']}&since=4")[1]
    assert [n for n, _ in later["results"]] == ["app", "."] and later["next"] == 6
    assert cv.get(f"/api/ai/job?id={job['job']}&since=99")[1]["results"] == []

    # the root's prompt was built from the children's fresh AI summaries
    root_prompt = cv.fake.prompts()[-1]
    app_x = dict(block["results"])["app"]
    assert app_x["s"] in root_prompt

    # every result went to the cache: analyze shows them all, a new job asks the model nothing
    calls = len(cv.fake.chats())
    graph = cv.analyze()
    assert {i for i, n in graph["nodes"].items() if "x" in n} == set(order)
    status, again = cv.start_job()
    assert status == 202 and again["job"] != job["job"]
    assert cv.wait_job(again["job"])["state"] == "done"
    assert len(cv.fake.chats()) == calls


def test_one_running_job_per_project_and_cancel(cv):
    cv.enable()
    cv.fake.hold()
    status, job = cv.start_job()
    assert status == 202
    assert cv.fake.wait_for_chats(1)
    status, same = cv.start_job()
    assert status == 200 and same["job"] == job["job"]

    status, block = cv.post("/api/ai/job/cancel", {"job": job["job"]})
    assert status == 200 and block["state"] == "running" and block["message"] == M.JOB_STOPPING
    assert block["current"] == "app/api.js"
    cv.fake.release()
    block = cv.wait_job(job["job"])
    assert block["state"] == "cancelled" and block["done"] == 1
    assert [n for n, _ in block["results"]] == ["app/api.js"]  # it stopped after the current item
    assert block["message"] == M.JOB_CANCELLED.format(n=1)
    time.sleep(0.2)
    assert len(cv.fake.chats()) == 1

    status, again = cv.start_job()  # a cancelled job does not block a new one
    assert status == 202 and again["job"] != job["job"]
    assert cv.wait_job(again["job"])["state"] == "done"


def test_on_demand_requests_go_ahead_of_a_running_job(cv):
    cv.enable()
    cv.fake.hold()
    status, job = cv.start_job()
    assert status == 202 and cv.fake.wait_for_chats(1)  # the job's first item is with the model
    reply = {}
    asker = threading.Thread(target=lambda: reply.update(r=cv.explain("main.py")))
    asker.start()
    deadline = time.monotonic() + 10
    while cv.httpd.ai.pending() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cv.httpd.ai.pending() == 1
    cv.fake.release()
    asker.join(15)
    assert reply["r"][0] == 200 and reply["r"][1]["cached"] is False
    block = cv.wait_job(job["job"])
    assert block["state"] == "done"
    order = answers(cv.fake)
    assert order[:2] == ["app/api.js", "main.py"]  # the request went right after the item in progress
    assert order.count("main.py") == 1  # the job found main.py in the cache
    assert [n for n, _ in block["results"]] == ["app/api.js", "app/util.py", "main.py", "big.py", "app", "."]


def test_the_same_question_twice_makes_one_model_call(cv):
    cv.enable()
    cv.fake.hold()
    replies = []
    threads = [threading.Thread(target=lambda: replies.append(cv.explain("app/util.py"))) for _ in range(2)]
    for t in threads:
        t.start()
    assert cv.fake.wait_for_chats(1)
    time.sleep(0.2)
    cv.fake.release()
    for t in threads:
        t.join(15)
    assert [r[0] for r in replies] == [200, 200] and replies[0][1]["x"] == replies[1][1]["x"]
    assert len(cv.fake.chats()) == 1


def test_too_many_waiting_requests_are_busy(cv, monkeypatch):
    cv.enable()
    monkeypatch.setattr(service, "MAX_PENDING", 1)
    cv.fake.hold()
    replies = []
    first = threading.Thread(target=lambda: replies.append(cv.explain("app/util.py")))
    first.start()
    assert cv.fake.wait_for_chats(1)  # the worker is busy with it
    second = threading.Thread(target=lambda: replies.append(cv.explain("app/api.js")))
    second.start()
    deadline = time.monotonic() + 10
    while cv.httpd.ai.pending() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cv.explain("main.py") == (409, {"error": M.BUSY, "code": "busy"})
    cv.fake.release()
    first.join(15)
    second.join(15)
    assert sorted(r[0] for r in replies) == [200, 200]


def test_ollama_stopping_mid_job_fails_the_job_and_keeps_what_it_made(cv):
    cv.enable()
    cv.fake.set(delay=0.1)
    status, job = cv.start_job()
    assert status == 202
    deadline = time.monotonic() + 10
    while not cv.get(f"/api/ai/job?id={job['job']}")[1]["results"] and time.monotonic() < deadline:
        time.sleep(0.01)
    cv.fake.stop()
    block = cv.wait_job(job["job"])
    assert block["state"] == "failed"
    kept = len(block["results"])
    assert 1 <= kept < 6
    assert block["message"] == M.JOB_OLLAMA_STOPPED.format(n=kept)
    assert cv.get("/api/ai/status")[1]["ollama"] != "running"


def test_changing_the_model_stops_a_running_job(cv):
    cv.enable()
    cv.fake.hold()
    status, job = cv.start_job()
    assert status == 202 and cv.fake.wait_for_chats(1)
    status, block = cv.post("/api/ai/settings", {"model": "gemma3:4b"})
    assert status == 200 and block["model"] == "gemma3:4b"
    cv.fake.release()
    block = cv.wait_job(job["job"])
    assert block["state"] == "cancelled" and block["message"] == M.JOB_SETTINGS_CHANGED.format(n=1)


def test_job_errors_count_and_too_many_in_a_row_stop_it(cv):
    cv.enable()
    cv.fake.set(fail="badjson", fail_count=1)
    block = cv.wait_job(cv.start_job()[1]["job"])
    assert block["state"] == "done" and block["errors"] == 1 and block["done"] == 6
    assert block["message"] == M.JOB_DONE_ERRORS.format(n=1)
    assert len(block["results"]) == 5

    (cv.root / "app" / "util.py").write_text("def slug(t):\n    return t\n", encoding="utf-8")  # not cached any more
    cv.fake.set(fail="badjson", fail_count=0)
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        (cv.root / name).write_text(f"# {name}\n", encoding="utf-8")
    graph_module = sys.modules["codevision.graph"]
    real = graph_module.build

    def build(path):
        graph = real(path)
        for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
            graph["nodes"][name] = {"n": name, "p": name, "t": "f", "a": "sv", "l": 1, "s": name, "d": name,
                                    "f": [], "u": [], "b": [], "lang": "python"}
            graph["nodes"]["."]["k"].append(name)
        return graph

    graph_module.build = build
    cv.analyze()
    block = cv.wait_job(cv.start_job()[1]["job"])
    assert block["state"] == "failed" and block["message"].startswith("AI가 계속")


def test_job_lookups(cv):
    cv.enable()
    assert cv.get("/api/ai/job?id=nope") == (404, {"error": M.JOB_NOT_FOUND, "code": "not_found"})
    assert cv.post("/api/ai/job/cancel", {"job": "nope"}) == (404, {"error": M.JOB_NOT_FOUND, "code": "not_found"})
    for query in ("", "?id=", "?id=j1&since=-1", "?id=j1&since=x", "?id=j1&since=1e3", "?id=" + "j" * 65):
        status, data = cv.get("/api/ai/job" + query)
        assert status == 400 and set(data) == {"error"}, query
    assert cv.post("/api/ai/job/cancel", {})[0] == 400
    job = cv.start_job()[1]
    assert cv.wait_job(job["job"])["state"] == "done"
    status, block = cv.post("/api/ai/job/cancel", {"job": job["job"]})
    assert status == 200 and block["state"] == "done"  # a finished job stays finished


# --- projects in memory -------------------------------------------------------------------------

def test_projects_not_in_memory_are_analyzed_again(cv, monkeypatch, tmp_path):
    cv.enable()
    assert cv.explain("app/util.py")[0] == 200  # never analyzed through the screen: analyzed now
    assert cv.built == [str(cv.root)]
    assert cv.explain("app/api.js")[0] == 200
    assert cv.built == [str(cv.root)]  # kept in memory

    monkeypatch.setattr(service, "MAX_PROJECTS", 1)
    other = write_files(tmp_path / "other")
    status, _ = cv.get("/api/analyze?path=" + _quote(str(other)))
    assert status == 200 and cv.built[-1] == str(other)
    assert cv.explain("main.py")[0] == 200  # the first project fell out: analyzed again
    assert cv.built[-1] == str(cv.root) and len(cv.built) == 3
    assert cv.explain("main.py", project=f'"{cv.project}"')[1]["cached"] is True  # a quoted path is the same project


def test_a_failing_analysis_is_a_failed_502(cv, monkeypatch):
    cv.enable()

    def broken(path):
        raise RuntimeError("analyzer crashed")

    monkeypatch.setattr(sys.modules["codevision.graph"], "build", broken)
    assert cv.explain("app/util.py") == (502, {"error": M.ANALYSIS_FAILED, "code": "failed"})
    assert cv.start_job() == (502, {"error": M.ANALYSIS_FAILED, "code": "failed"})


def test_a_link_out_of_the_project_is_never_followed(cv, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("print('outside')\n", encoding="utf-8")
    link = cv.root / "linked"
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            os.symlink(outside, link, target_is_directory=True)
    except (ImportError, OSError, AttributeError) as exc:
        pytest.skip(f"cannot make a link here: {exc}")
    graph_module = sys.modules["codevision.graph"]
    real = graph_module.build

    def build(path):
        graph = real(path)
        graph["nodes"]["linked"] = {"n": "linked", "p": "linked/", "t": "d", "a": "sv", "s": "링크", "d": "링크",
                                    "k": ["linked/evil.py"]}
        graph["nodes"]["linked/evil.py"] = {"n": "evil.py", "p": "linked/evil.py", "t": "f", "a": "sv", "l": 1,
                                            "s": "밖", "d": "밖", "f": [], "u": [], "b": [], "lang": "python"}
        graph["nodes"]["."]["k"].append("linked")
        return graph

    graph_module.build = build
    cv.enable()
    cv.analyze()
    assert cv.explain("linked/evil.py") == (404, {"error": M.NODE_NOT_FOUND, "code": "not_found"})
    assert cv.explain("linked") == (404, {"error": M.NODE_NOT_FOUND, "code": "not_found"})
    block = cv.wait_job(cv.start_job()[1]["job"])
    assert "linked/evil.py" not in dict(block["results"]) and block["errors"] == 0
    assert "print('outside')" not in cv.fake.raw_requests()


# --- starting Ollama ------------------------------------------------------------------------------

def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """CODEVISION_OLLAMA_EXE points at the fake's serve mode, on a port where nothing runs yet."""
    url = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setenv("CODEVISION_OLLAMA_EXE", str(FAKE_SCRIPT))
    monkeypatch.setenv("CODEVISION_OLLAMA_URL", url)
    monkeypatch.setenv("FAKE_OLLAMA_MAX_LIFE", "60")  # never outlives the test run for long
    monkeypatch.setenv("FAKE_OLLAMA_PID_FILE", str(tmp_path / "stub.pid"))
    monkeypatch.delenv("FAKE_OLLAMA_START_DELAY", raising=False)
    procs = []
    yield types.SimpleNamespace(url=url, procs=procs)
    for proc in procs:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_start_launches_the_installed_program_and_waits_for_it(cv, stub, monkeypatch):
    monkeypatch.setenv("FAKE_OLLAMA_START_DELAY", "1")
    block = cv.get("/api/ai/status")[1]
    assert block["ollama"] == "stopped" and block["can_start"] is True
    started = time.monotonic()
    status, block = cv.post("/api/ai/start")
    stub.procs.append(cv.httpd.ai.started_process)
    assert status == 200, block
    assert time.monotonic() - started >= 1
    assert block["ollama"] == "running" and block["can_start"] is False
    assert [m["name"] for m in block["models"]] == ["qwen2.5:7b", "gemma3:4b"]
    proc = cv.httpd.ai.started_process
    assert proc.args[0] == sys.executable and proc.args[1:] == [str(FAKE_SCRIPT), "serve"]

    status, again = cv.post("/api/ai/start")  # already running: nothing new is started
    assert status == 200 and cv.httpd.ai.started_process is proc

    cv.enable()
    assert cv.explain("app/util.py")[0] == 200  # the started stub answers like Ollama


def test_start_errors(cv, stub, monkeypatch):
    monkeypatch.setenv("CODEVISION_OLLAMA_EXE", str(TESTS / "no-ollama-here.exe"))
    assert cv.post("/api/ai/start") == (409, {"error": M.START_MISSING, "code": "missing"})

    monkeypatch.setenv("CODEVISION_OLLAMA_EXE", str(FAKE_SCRIPT))
    monkeypatch.setenv("FAKE_OLLAMA_COLD", "not-a-number")  # the stub ends at once, like a failing "serve"
    status, data = cv.post("/api/ai/start")
    stub.procs.append(cv.httpd.ai.started_process)
    assert (status, data) == (502, {"error": M.START_FAILED, "code": "failed"})

    monkeypatch.delenv("FAKE_OLLAMA_COLD")
    monkeypatch.setenv("FAKE_OLLAMA_START_DELAY", "5")
    cv.httpd.ai.start_wait = 0.5
    status, data = cv.post("/api/ai/start")
    stub.procs.append(cv.httpd.ai.started_process)
    assert (status, data) == (504, {"error": M.START_TIMEOUT, "code": "timeout"})

    monkeypatch.setenv("CODEVISION_OLLAMA_URL", "http://203.0.113.9:11434")
    status, data = cv.post("/api/ai/start")
    assert (status, data) == (409, {"error": M.BAD_URL, "code": "failed"})


def test_find_ollama_runs_the_server_never_the_tray_app(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEVISION_OLLAMA_EXE")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramW6432", str(tmp_path / "pf"))
    if os.name != "nt":
        pytest.skip("the Windows install layout")
    folder = tmp_path / "Programs" / "Ollama"
    assert explain.find_ollama() is None
    folder.mkdir(parents=True)
    (folder / "ollama.exe").write_bytes(b"")
    found = explain.find_ollama()
    assert found.command == [str(folder / "ollama.exe"), "serve"]
    (folder / "ollama app.exe").write_bytes(b"")  # the tray app opens a window and can update itself
    assert explain.find_ollama().command == [str(folder / "ollama.exe"), "serve"]


def test_launch_is_hidden_on_its_own_and_serves_where_we_look(monkeypatch, tmp_path):
    from codevision.explain import ollama
    seen = {}

    class Recorder:
        def __init__(self, args, **kwargs):
            seen.update(args=args, **kwargs)

    monkeypatch.setattr(ollama.subprocess, "Popen", Recorder)
    install = ollama.OllamaInstall([str(tmp_path / "ollama.exe"), "serve"], tmp_path / "ollama.exe")
    ollama.launch(install, "http://127.0.0.1:11500")
    assert seen["args"] == [str(tmp_path / "ollama.exe"), "serve"]
    assert seen["env"]["OLLAMA_HOST"] == "127.0.0.1:11500"
    assert seen["stdin"] is seen["stdout"] is seen["stderr"] is ollama.subprocess.DEVNULL
    assert seen["cwd"] == str(tmp_path)
    if os.name == "nt":
        # a console of its own with no window, which the model runners it starts share
        assert seen["creationflags"] & ollama.subprocess.CREATE_NO_WINDOW
        assert seen["creationflags"] & ollama.subprocess.CREATE_NEW_PROCESS_GROUP
        assert not seen["creationflags"] & ollama.subprocess.DETACHED_PROCESS
        assert seen["startupinfo"].wShowWindow == 0
    else:
        assert seen["start_new_session"] is True
    with pytest.raises(ValueError):
        ollama.launch(install, "http://198.51.100.1:11434")


# --- letting go of the model ----------------------------------------------------------------------

def _unloads(fake) -> list[str]:
    """The models the client asked Ollama to let go of (keep_alive 0, no prompt)."""
    with fake._lock:
        calls = list(fake.calls)
    return [c["body"]["model"] for c in calls if c["path"] in ("/api/generate", "/api/chat")
            and isinstance(c["body"], dict) and c["body"].get("keep_alive") == 0 and not c.get("answer")]


def test_turning_ai_off_lets_go_of_the_model(cv):
    cv.enable()
    assert cv.explain("app/util.py")[0] == 200
    assert cv.fake.ps()["models"] and _unloads(cv.fake) == []
    status, block = cv.post("/api/ai/settings", {"enabled": False})
    assert status == 200 and block["enabled"] is False
    deadline = time.monotonic() + 10
    while cv.fake.ps()["models"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cv.fake.ps()["models"] == [] and _unloads(cv.fake) == ["qwen2.5:7b"]
    assert len(cv.fake.chats()) == 1  # letting go is not a model call


def test_turning_ai_off_waits_for_the_call_in_progress(cv):
    cv.enable()
    cv.fake.hold()
    replies = []
    asking = threading.Thread(target=lambda: replies.append(cv.explain("app/util.py")))
    asking.start()
    assert cv.fake.wait_for_chats(1)
    assert cv.post("/api/ai/settings", {"enabled": False})[0] == 200
    time.sleep(0.3)
    assert _unloads(cv.fake) == []  # not while the model is still answering
    cv.fake.release()
    asking.join(10)
    deadline = time.monotonic() + 10
    while not _unloads(cv.fake) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _unloads(cv.fake) == ["qwen2.5:7b"] and cv.fake.ps()["models"] == []
    assert replies and replies[0][0] == 200


def test_nothing_is_let_go_of_that_codevision_did_not_use(cv):
    cv.enable()
    cv.fake.loaded["gemma3:4b"] = time.time() + 600  # loaded by another program
    assert cv.post("/api/ai/settings", {"enabled": False})[0] == 200
    time.sleep(0.5)
    assert _unloads(cv.fake) == [] and [m["name"] for m in cv.fake.ps()["models"]] == ["gemma3:4b"]


def test_closing_the_server_lets_go_of_the_model(cv):
    cv.enable()
    assert cv.explain("app/util.py")[0] == 200
    cv.httpd.ai.close()
    assert _unloads(cv.fake) == ["qwen2.5:7b"] and cv.fake.ps()["models"] == []


def test_letting_go_with_ollama_off_is_quiet(cv):
    cv.enable()
    assert cv.explain("app/util.py")[0] == 200
    cv.fake.stop()
    started = time.monotonic()
    assert cv.httpd.ai.unload_models() == []
    assert time.monotonic() - started < 2


# --- shutting down and the real analyzer ------------------------------------------------------------

def test_closing_the_server_answers_waiting_requests(cv):
    cv.enable()
    cv.fake.hold()
    replies = []
    first = threading.Thread(target=lambda: replies.append(cv.explain("app/util.py")))
    first.start()
    assert cv.fake.wait_for_chats(1)
    second = threading.Thread(target=lambda: replies.append(cv.explain("app/api.js")))
    second.start()
    deadline = time.monotonic() + 10
    while cv.httpd.ai.pending() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    cv.httpd.ai.close()
    second.join(10)
    assert (409, {"error": M.CLOSING, "code": "busy"}) in replies
    cv.fake.release()
    first.join(10)


def test_the_demo_with_the_real_analyzer(tmp_path, monkeypatch, fake_ollama):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    httpd = cv_server.create_server(port=0, web_dir=tmp_path)
    with serving(httpd):
        client = Client(httpd, cv_server.DEMO_DIR, [], fake_ollama, tmp_path)
        client.enable()
        status, graph = client.get("/api/analyze?demo=1")
        assert status == 200 and graph["ai"]["available"] is True
        node_id = "backend/routes/todos.py"
        signatures = [sig for sig, _ in graph["nodes"][node_id]["f"]]
        status, reply = client.explain(node_id, project="demo")
        assert status == 200 and [sig for sig, _ in reply["x"]["f"]] == signatures
        assert all(text for _, text in reply["x"]["f"])
        status, graph = client.get("/api/analyze?demo=1")
        assert graph["nodes"][node_id]["x"] == reply["x"]
        assert len(fake_ollama.chats()) == 1
        installed = json.dumps(str(cv_server.DEMO_DIR.parent), ensure_ascii=False)[1:-1]
        assert installed not in json.dumps(graph, ensure_ascii=False)
