"""Tests for the AI explanation skeleton. No AI service is ever called."""
import copy
import json
import os
from pathlib import Path

import pytest

from codevision import explain
from codevision.explain import prompts
from codevision.explain.base import Explainer, Explanation, FileContext, FolderContext, data_dir
from codevision.explain.cache import ExplanationCache, blob_sha
from codevision.explain.providers import NoneExplainer, get_explainer, read_config

NOT_CONNECTED = {"provider": "none", "available": False, "message": "AI 설명이 아직 연결되지 않았어요."}

SOURCES = {
    "main.py": b"def main():\n    helper(1)\n",
    "README.md": b"# app\n",
}


def small_graph() -> dict:
    return {
        "name": "app",
        "root": ".",
        "nodes": {
            ".": {"n": "app", "p": "app/", "t": "d", "a": "ot", "s": "규칙 요약", "d": "규칙 설명",
                  "k": ["main.py", "README.md"]},
            "main.py": {"n": "main.py", "p": "main.py", "t": "f", "a": "sv", "l": 2, "s": "시작 파일",
                        "d": "규칙으로 만든 설명이에요.", "f": [["main()", ""], ["helper(x)", "도움 함수"]],
                        "u": [], "b": [], "lang": "python"},
            "README.md": {"n": "README.md", "p": "README.md", "t": "f", "a": "ot", "l": 1, "s": "소개 문서",
                          "d": "프로젝트 소개예요.", "f": [], "u": [], "b": []},
        },
    }


class FakeExplainer(Explainer):
    name = "fake"
    model = "fake-1"

    def __init__(self):
        self.files = []
        self.folders = []

    def available(self):
        return True

    def explain_file(self, ctx):
        self.files.append(ctx)
        return Explanation(s="AI 요약", d="AI가 만든 설명이에요.", f={"main()": "프로그램을 시작해요."})

    def explain_folder(self, ctx):
        self.folders.append(ctx)
        return Explanation(s="AI 폴더 요약", d="AI가 만든 폴더 설명이에요.")


def test_blob_sha_matches_git():
    assert blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_cache_round_trip(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = ExplanationCache(path)
    sha = blob_sha(b"print('hi')\n")
    exp = Explanation(s="요약", d="설명이에요.", f={"a()": "하는 일"})
    assert cache.get("fake", "m1", "1", sha) is None
    cache.put("fake", "m1", "1", sha, exp)
    assert cache.get("fake", "m1", "1", sha) == exp
    assert cache.get("fake", "m2", "1", sha) is None  # other model
    assert cache.get("fake", "m1", "2", sha) is None  # other prompt version
    assert cache.get("other", "m1", "1", sha) is None  # other provider
    assert cache.get("fake", "m1", "1", sha, kind="folder") is None
    cache.close()

    reopened = ExplanationCache(path)
    assert reopened.get("fake", "m1", "1", sha) == exp
    reopened.close()


def test_cache_lives_in_data_dir_and_is_lazy(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    cache = ExplanationCache()
    assert cache.path == tmp_path / "cache.sqlite"
    assert not cache.path.exists()


def test_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    assert data_dir() == tmp_path
    monkeypatch.delenv("CODEVISION_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    expected = tmp_path / "local" / "CodeVision" if os.name == "nt" else Path.home() / ".codevision"
    assert data_dir() == expected


def test_enrich_without_ai_leaves_nodes_untouched(tmp_path):
    graph = small_graph()
    before = copy.deepcopy(graph["nodes"])
    cache = ExplanationCache(tmp_path / "cache.sqlite")

    result = explain.enrich(graph, NoneExplainer(), cache, SOURCES.get)

    assert result is graph
    assert graph["nodes"] == before
    assert graph["ai"] == NOT_CONNECTED
    assert explain.status(NoneExplainer()) == NOT_CONNECTED
    assert not (tmp_path / "cache.sqlite").exists()


def test_enrich_with_fake_explainer_fills_texts_and_uses_cache(tmp_path):
    cache = ExplanationCache(tmp_path / "cache.sqlite")
    first = FakeExplainer()
    graph = explain.enrich(small_graph(), first, cache, SOURCES.get)

    node = graph["nodes"]["main.py"]
    assert node["s"] == "AI 요약"
    assert node["d"] == "AI가 만든 설명이에요."
    assert node["f"] == [["main()", "프로그램을 시작해요."], ["helper(x)", "도움 함수"]]
    assert graph["nodes"]["README.md"]["s"] == "소개 문서"  # only code files go to the AI
    assert graph["nodes"]["."]["s"] == "AI 폴더 요약"
    assert graph["ai"] == {"provider": "fake", "available": True, "message": "AI 설명을 쓸 수 있어요."}

    ctx = first.files[0]
    assert len(first.files) == 1
    assert (ctx.rel_path, ctx.language) == ("main.py", "python")
    assert "def main" in ctx.source
    assert ctx.symbols == ["main()", "helper(x)"]
    assert first.folders[0].children == [("main.py", "AI 요약"), ("README.md", "소개 문서")]

    second = FakeExplainer()
    again = explain.enrich(small_graph(), second, cache, SOURCES.get)
    assert second.files == [] and second.folders == []  # everything came from the cache
    assert again["nodes"]["main.py"]["s"] == "AI 요약"
    assert again["nodes"]["."]["d"] == "AI가 만든 폴더 설명이에요."

    changed = {**SOURCES, "main.py": b"def main():\n    return 2\n"}
    third = FakeExplainer()
    explain.enrich(small_graph(), third, cache, changed.get)
    assert [c.rel_path for c in third.files] == ["main.py"]  # new content, new blob sha
    cache.close()


def test_enrich_survives_a_failing_explainer():
    class Broken(FakeExplainer):
        def explain_file(self, ctx):
            raise RuntimeError("service down")

    graph = explain.enrich(small_graph(), Broken(), None, SOURCES.get)
    assert graph["nodes"]["main.py"]["s"] == "시작 파일"
    assert graph["ai"]["available"] is True
    assert "못" in graph["ai"]["message"]


def test_read_config_and_get_explainer(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    assert read_config() == {"ai": {"provider": "none"}}
    explainer = get_explainer(read_config())
    assert isinstance(explainer, NoneExplainer)
    assert explainer.available() is False

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"ai": {"provider": "local", "model": "x"}}), encoding="utf-8")
    assert read_config()["ai"] == {"provider": "local", "model": "x"}
    config_file.write_text("{broken", encoding="utf-8")
    assert read_config() == {"ai": {"provider": "none"}}

    for planned in ("local", "cloud"):
        with pytest.raises(NotImplementedError, match="준비 중"):
            get_explainer({"ai": {"provider": planned}})
    with pytest.raises(ValueError, match="알 수 없는"):
        get_explainer({"ai": {"provider": "magic"}})


def test_prompts_are_provider_neutral_drafts():
    assert prompts.PROMPT_VERSION
    ctx = FileContext(rel_path="src/api.js", language="javascript", source="export function a() {}",
                      summary_hint="요청 함수", symbols=["a()"], used_by=["src/App.jsx"])
    text = prompts.file_prompt(ctx)
    for part in ("src/api.js", "a()", "src/App.jsx", "export function a() {}", '"s"', '"d"', '"f"'):
        assert part in text
    assert "JSON" in prompts.SYSTEM_PROMPT

    long = prompts.file_prompt(FileContext("big.py", "python", "x" * (prompts.MAX_SOURCE_CHARS + 50)))
    assert "생략" in long

    folder = prompts.folder_prompt(FolderContext(rel_path="src", name="src", children=[("api.js", "요청 함수")]))
    assert "api.js: 요청 함수" in folder


def test_parse_response():
    answer = '```json\n{"s": "요약", "d": "설명이에요.", "f": {"a()": "하는 일", "b()": ""}}\n```'
    assert prompts.parse_response(answer) == Explanation(s="요약", d="설명이에요.", f={"a()": "하는 일"})
    assert prompts.parse_response("잘 모르겠어요") is None
    assert prompts.parse_response("{not json}") is None
