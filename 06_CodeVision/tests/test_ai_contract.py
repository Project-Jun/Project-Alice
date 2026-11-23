"""AI contract additions after the verification step, against tests/fake_ollama.py like test_ai.py.

- node["xs"]: while AI is on, /api/analyze marks the code files the AI never gets ("secret", "too_big",
  "unreadable"), so the screen leaves them out of the 전체 설명 estimate and says why instead of asking.
- GET /api/ai/status asks Ollama every time, so an Ollama started or stopped outside CodeVision shows at
  once; /api/analyze keeps reusing a recent block, and fresh requests that come together share a probe.
- tests/fixtures/sample-graph.json, the documented example graph, follows the contract.
"""
import json
import re
import sys
import threading
import time
from pathlib import Path

from codevision import explain
from codevision import server as cv_server
from codevision.explain import messages as M
from codevision.explain import prompts, safety, service
from test_ai import cv  # noqa: F401  (the CodeVision server fixture of test_ai.py)

SAMPLE_GRAPH = Path(__file__).resolve().parent / "fixtures" / "sample-graph.json"
STATUS_KEYS = {"provider", "enabled", "available", "model", "models", "recommended", "ollama", "can_start", "message"}


def marks(graph: dict) -> dict:
    return {i: n["xs"] for i, n in graph["nodes"].items() if "xs" in n}


def with_extra_nodes(monkeypatch, extra: dict) -> None:
    """test_ai.py's fake analyzer, with more file nodes (each listed in its parent folder)."""
    graph_module = sys.modules["codevision.graph"]
    real = graph_module.build

    def build(path):
        graph = real(path)
        for node_id, node in extra.items():
            graph["nodes"][node_id] = dict(node)
            parent = node_id.rsplit("/", 1)[0] if "/" in node_id else "."
            graph["nodes"][parent]["k"].append(node_id)
        return graph

    monkeypatch.setattr(graph_module, "build", build)


def code_node(rel: str, lang: str = "python") -> dict:
    return {"n": rel.rsplit("/", 1)[-1], "p": rel, "t": "f", "a": "sv", "l": 1, "s": "규칙 요약", "d": "규칙 설명",
            "f": [], "u": [], "b": [], "lang": lang}


# --- node["xs"] --------------------------------------------------------------------------------------

def test_analyze_marks_the_code_files_the_ai_never_gets(cv, monkeypatch):
    monkeypatch.setattr(explain, "MAX_SOURCE_BYTES", 2000)
    (cv.root / "huge.py").write_text("x = 1\n" * 1000, encoding="utf-8")  # 6,000 bytes: too big now
    (cv.root / "app" / "credentials.js").write_text("export const password = 'hunter2-js';\n", encoding="utf-8")
    with_extra_nodes(monkeypatch, {"app/credentials.js": code_node("app/credentials.js", "javascript"),
                                   "huge.py": code_node("huge.py"),
                                   "gone.py": code_node("gone.py")})  # gone.py is not on disk (deleted, locked)
    cv.enable()
    graph = cv.analyze()
    assert marks(graph) == {"secrets.py": "secret", "app/credentials.js": "secret", "huge.py": "too_big",
                            "gone.py": "unreadable"}  # .env and README.md are no code files: never asked anyway
    assert cv.fake.chats() == []
    # what the screen used to learn only by asking, one file at a time
    assert cv.explain("app/credentials.js") == (422, {"error": M.SECRET, "code": "secret"})
    assert cv.explain("huge.py") == (422, {"error": M.TOO_BIG, "code": "too_big"})
    assert cv.explain("gone.py") == (404, {"error": M.FILE_UNREADABLE, "code": "not_found"})

    block = cv.wait_job(cv.start_job()[1]["job"])
    assert block["state"] == "done" and block["errors"] == 0
    assert not set(dict(block["results"])) & set(marks(graph))
    graph = cv.analyze()
    nodes = graph["nodes"]
    # after 전체 설명 every code file and folder has an explanation or a mark: the estimate counts 0
    assert [i for i, n in nodes.items() if (n.get("lang") or n["t"] == "d") and "x" not in n and "xs" not in n] == []
    assert not [i for i, n in nodes.items() if "x" in n and "xs" in n]
    assert "hunter2-js" not in cv.fake.raw_requests()


def test_marks_come_only_while_ai_is_on_and_need_no_model(cv, monkeypatch):
    assert marks(cv.analyze()) == {}
    monkeypatch.setenv("CODEVISION_OLLAMA_URL", "http://127.0.0.1:1")  # Ollama off, so no model is chosen yet
    block = cv.enable()
    assert block["enabled"] is True and block["model"] == ""
    graph = cv.analyze()
    assert marks(graph) == {"secrets.py": "secret"}
    assert not any("x" in n for n in graph["nodes"].values())
    cv.post("/api/ai/settings", {"enabled": False})
    assert marks(cv.analyze()) == {}


def test_annotate_reads_each_code_file_once_and_never_a_secret_one(cv, monkeypatch):
    reads = []
    real = safety.ProjectFiles.read

    def counting(self, rel, limit):
        reads.append(rel)
        return real(self, rel, limit)

    monkeypatch.setattr(safety.ProjectFiles, "read", counting)
    cv.analyze()
    assert reads == []  # AI off: no file is looked at
    cv.enable()
    cv.analyze()
    assert sorted(reads) == ["app/api.js", "app/util.py", "big.py", "main.py"]


def test_a_failing_annotate_leaves_no_marks_behind(cv, monkeypatch):
    cv.enable()

    def broken(*args, **kwargs):
        raise RuntimeError("the cache broke")

    monkeypatch.setattr(service.AIService, "_cached_xs", broken)  # after the marks were put in
    graph = cv.analyze()
    assert graph["ai"]["message"] == M.ANNOTATE_FAILED
    assert not any("x" in n or "xs" in n for n in graph["nodes"].values())


def test_the_page_shows_the_servers_note_for_every_mark():
    text = (cv_server.WEB_DIR / "app.js").read_text(encoding="utf-8")
    found = re.search(r"const XS_NOTE = \{(.*?)\};", text, re.S)
    assert found, "app.js has no XS_NOTE"
    assert dict(re.findall(r"(\w+): '([^']*)'", found.group(1))) == M.MARK_NOTES
    assert set(service.MARKS) == set(M.MARK_NOTES) == {"secret", "too_big", "unreadable"}
    assert M.MARK_NOTES["secret"] == M.SECRET and M.MARK_NOTES["too_big"] == M.TOO_BIG  # the 422 sentences


# --- a fresh GET /api/ai/status ----------------------------------------------------------------------

def tags_calls(fake) -> int:
    return len([c for c in list(fake.calls) if c["path"] == "/api/tags"])


def test_ai_status_asks_ollama_every_time(cv):
    assert cv.enable()["message"] == M.READY.format(model="qwen2.5:7b")
    cv.fake.set(models=[])  # the models were removed outside CodeVision, a moment later
    block = cv.get("/api/ai/status")[1]
    assert (block["models"], block["available"], block["message"]) == ([], False, M.NO_MODELS)
    cv.fake.set(models=["qwen2.5:7b"])
    assert cv.get("/api/ai/status")[1]["message"] == M.READY.format(model="qwen2.5:7b")
    cv.fake.stop()  # Ollama was closed outside CodeVision
    block = cv.get("/api/ai/status")[1]
    assert block["ollama"] != "running" and block["available"] is False and block["models"] == []


def test_analyze_still_reuses_a_recent_status(cv):
    cv.enable()
    cv.get("/api/ai/status")
    probes = tags_calls(cv.fake)
    cv.analyze()
    cv.analyze()
    assert tags_calls(cv.fake) == probes  # both used the block of the status request
    cv.get("/api/ai/status")
    assert tags_calls(cv.fake) == probes + 1


def test_fresh_requests_that_come_together_share_a_probe(monkeypatch):
    probes = []
    lock = threading.Lock()

    def slow_probe(settings=None):
        with lock:
            probes.append(1)
            n = len(probes)
        time.sleep(0.3)
        return {"probe": n}

    monkeypatch.setattr(service, "compute_status", slow_probe)
    ai = service.AIService(None, lambda root, demo: {})
    gate = threading.Barrier(8)
    blocks = []

    def ask():
        gate.wait()
        blocks.append(ai.status(fresh=True))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(blocks) == 8 and len(probes) <= 2
    before = len(probes)
    block = ai.status(fresh=True)  # a new request never takes the answer of a probe begun before it
    assert len(probes) == before + 1 and block == {"probe": len(probes)}
    assert ai.status() == block and len(probes) == before + 1  # a plain request reuses it


def test_a_probe_begun_before_invalidate_is_not_reused(monkeypatch):
    began, go_on = threading.Event(), threading.Event()
    probes = []

    def probe(settings=None):
        probes.append(1)
        if len(probes) == 1:
            began.set()
            go_on.wait(5)
            return {"old": True}
        return {"old": False}

    monkeypatch.setattr(service, "compute_status", probe)
    ai = service.AIService(None, lambda root, demo: {})
    first = []
    thread = threading.Thread(target=lambda: first.append(ai.status()))
    thread.start()
    assert began.wait(5)
    ai.invalidate()  # the settings changed while that probe was on its way
    go_on.set()
    thread.join(5)
    assert first == [{"old": True}]  # its own caller gets it
    assert ai.status() == {"old": False}  # nobody after the change does


# --- the example graph --------------------------------------------------------------------------------

def test_the_example_graph_follows_the_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    graph = json.loads(SAMPLE_GRAPH.read_text(encoding="utf-8"))
    ai = graph["ai"]
    assert set(ai) == STATUS_KEYS == set(service.compute_status())
    assert ai["provider"] in ("local", "none") and ai["ollama"] in ("running", "stopped", "missing")
    assert all(isinstance(ai[k], bool) for k in ("enabled", "available", "can_start"))
    assert isinstance(ai["model"], str) and isinstance(ai["message"], str) and ai["message"]
    assert ai["recommended"] == prompts.RECOMMENDED_MODELS
    for model in ai["models"]:
        assert set(model) == {"name", "size", "params"}
        assert isinstance(model["name"], str) and isinstance(model["size"], int) and isinstance(model["params"], str)
    if ai["ollama"] != "running":
        assert ai["models"] == []
    if ai["available"]:
        assert ai["enabled"] and ai["model"] in [m["name"] for m in ai["models"]]
        assert ai["message"] == M.READY.format(model=ai["model"])

    nodes = graph["nodes"]
    with_x = [i for i, n in nodes.items() if "x" in n]
    assert with_x and ai["enabled"]  # an example of x, which only comes while AI is on
    for node_id in with_x:
        node, x = nodes[node_id], nodes[node_id]["x"]
        assert node["t"] == "d" or node.get("lang"), node_id
        assert set(x) == {"s", "d", "f"} and isinstance(x["s"], str) and isinstance(x["d"], str)
        assert [sig for sig, _ in x["f"]] == [sig for sig, _ in node.get("f", [])], node_id
        assert "xs" not in node
    for node_id, node in nodes.items():
        if "xs" in node:
            assert node["xs"] in service.MARKS and node["t"] == "f" and node.get("lang"), node_id
