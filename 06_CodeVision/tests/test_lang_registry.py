"""The analyzer registry loads its modules once, even when the first analyses run at the same time.

The server analyzes each request in its own thread. Right after a start, two tabs that open at
once both trigger the first load; neither may see a half-filled list (which analyzed every file
without an analyzer: no connections, no start points and no warning).
"""
import importlib
import threading
import time
from types import SimpleNamespace

import codevision.lang as lang
from codevision.graph import build


def slow_importer(monkeypatch, fail=(), started=None):
    calls = []

    def import_module(name):
        calls.append(name)
        if started is not None:
            started.set()
        time.sleep(0.05)  # an import that takes a while, like tree-sitter on a cold start
        if name in fail:
            raise ImportError("No module named tree_sitter")
        return importlib.import_module(name)

    monkeypatch.setattr(lang, "_loaded", None)
    monkeypatch.setattr(lang, "_errors", {})
    monkeypatch.setattr(lang, "importlib", SimpleNamespace(import_module=import_module))
    return calls


def test_concurrent_first_use_sees_every_analyzer(monkeypatch):
    calls = slow_importer(monkeypatch)
    barrier = threading.Barrier(4)
    seen = []

    def work():
        barrier.wait()
        seen.append([m.__name__ for m in lang.analyzers()])

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert seen == [list(lang._MODULES)] * 4
    assert calls == list(lang._MODULES)  # imported once, not once per thread


def test_load_errors_waits_for_a_load_in_progress(monkeypatch):
    started = threading.Event()
    slow_importer(monkeypatch, fail={"codevision.lang.js_lang"}, started=started)
    loader = threading.Thread(target=lang.analyzers)
    loader.start()
    assert started.wait(5)
    errors = lang.load_errors()  # while the other thread is still importing
    loader.join(10)
    assert set(errors) == {"codevision.lang.js_lang"}
    assert [m.__name__ for m in lang.analyzers()] == ["codevision.lang.python_lang"]


def test_two_first_builds_at_once_give_the_same_graph(monkeypatch, demo_root):
    slow_importer(monkeypatch)
    barrier = threading.Barrier(2)
    graphs = []

    def work():
        barrier.wait()
        graphs.append(build(str(demo_root)))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(graphs) == 2
    for graph in graphs:
        edges = sum(len(n["u"]) for n in graph["nodes"].values() if n["t"] == "f")
        assert edges == 12 and graph["stats"]["entries"] == 2 and graph["stats"]["orphans"] == 1
        assert graph["nodes"]["backend/main.py"]["lang"] == "python" and graph["warnings"] == []
