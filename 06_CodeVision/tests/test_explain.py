"""Tests for the AI explanation pieces that need no server: config, cache, x, annotate, prompts.

No real AI service is ever called (see conftest.py). Server-level tests are in test_ai.py.
"""
import json
import os
from pathlib import Path

import pytest

from codevision import explain
from codevision.explain import messages, prompts, providers, service
from codevision.explain.base import AIError, Explanation, FileContext, FolderContext, data_dir
from codevision.explain.cache import ExplanationCache, blob_sha
from codevision.explain.providers import NoneExplainer, ai_settings, get_explainer, read_config


def small_graph() -> dict:
    return {
        "name": "app",
        "root": ".",
        "nodes": {
            ".": {"n": "app", "p": "app/", "t": "d", "a": "ot", "s": "규칙 요약", "d": "규칙 설명",
                  "k": ["lib", "main.py", "README.md"]},
            "lib": {"n": "lib", "p": "lib/", "t": "d", "a": "sv", "s": "도우미 모음", "d": "도우미가 있어요.",
                    "k": ["lib/util.py"]},
            "lib/util.py": {"n": "util.py", "p": "lib/util.py", "t": "f", "a": "sv", "l": 1, "s": "도우미",
                            "d": "작은 도우미예요.", "f": [["slug(text)", "글을 주소용으로 바꿔요."]],
                            "u": [], "b": ["main.py"], "lang": "python"},
            "main.py": {"n": "main.py", "p": "main.py", "t": "f", "a": "sv", "l": 2, "s": "시작 파일",
                        "d": "규칙으로 만든 설명이에요.", "f": [["main()", ""], ["helper(x)", "도움 함수"]],
                        "u": ["lib/util.py"], "b": [], "lang": "python"},
            "README.md": {"n": "README.md", "p": "README.md", "t": "f", "a": "ot", "l": 1, "s": "소개 문서",
                          "d": "프로젝트 소개예요.", "f": [], "u": [], "b": []},
        },
    }


SOURCES = {
    "main.py": b"def main():\n    helper(1)\n",
    "lib/util.py": b"def slug(text):\n    return text.lower()\n",
    "README.md": b"# app\n",
}


def write_project(root: Path) -> Path:
    for rel, data in SOURCES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


# --- data dir and cache -------------------------------------------------------------

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


# --- config -----------------------------------------------------------------------------

def test_config_defaults_to_local_ai_that_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    monkeypatch.delenv("CODEVISION_OLLAMA_URL")
    assert read_config() == {"ai": {"provider": "local", "enabled": False, "model": "",
                                    "base_url": "http://127.0.0.1:11434"}}
    settings = ai_settings()
    assert (settings.provider, settings.enabled, settings.model) == ("local", False, "")
    assert settings.base_url == "http://127.0.0.1:11434" and settings.url_error == ""
    assert not (tmp_path / "config.json").exists()  # reading never writes

    (tmp_path / "config.json").write_text("{broken", encoding="utf-8")
    assert read_config() == providers.default_config()
    (tmp_path / "config.json").write_text('{"ai": {"provider": "none", "enabled": "yes", "model": 3}}', encoding="utf-8")
    assert read_config()["ai"] == {"provider": "none", "enabled": False, "model": "", "base_url": "http://127.0.0.1:11434"}


def test_config_is_written_atomically_and_keeps_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path / "home"))
    path = tmp_path / "home" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "dark", "ai": {"provider": "local", "cloud": {"later": True}}}), encoding="utf-8")
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(providers.os, "replace", lambda a, b: (replaced.append((Path(a), Path(b))), real_replace(a, b)))

    providers.update_ai(enabled=True, model="gemma3:4b")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["ai"] == {"provider": "local", "cloud": {"later": True}, "enabled": True, "model": "gemma3:4b",
                          "base_url": "http://127.0.0.1:11434"}
    assert replaced and replaced[0][1] == path and replaced[0][0].parent == path.parent
    assert sorted(p.name for p in path.parent.iterdir()) == ["config.json"]  # no temporary file left
    with pytest.raises(ValueError):
        providers.update_ai(colour="red")


@pytest.mark.parametrize("url, normalized", [
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
    ("http://LOCALHOST:11500", "http://localhost:11500"),
    ("http://[::1]:11434", "http://[::1]:11434"),
    ("http://127.0.0.1", "http://127.0.0.1:11434"),
    ("  http://localhost:8080  ", "http://localhost:8080"),
])
def test_loopback_base_urls_are_accepted(url, normalized):
    assert explain.check_base_url(url) == normalized


@pytest.mark.parametrize("url", [
    "http://192.168.0.10:11434", "http://10.0.0.1:11434", "http://example.com:11434", "https://127.0.0.1:11434",
    "http://0.0.0.0:11434", "http://127.0.0.2:11434", "http://127.0.0.1.example.com", "http://127.0.0.1@evil.example",
    "http://user:pw@127.0.0.1:11434", "http://127.0.0.1:11434/api", "http://127.0.0.1:11434?x=1", "ftp://127.0.0.1",
    "file:///etc/passwd", "127.0.0.1:11434", "", "http://127.0.0.1:99999", "http://[::ffff:8.8.8.8]:11434",
    "http://ⓛocalhost:11434", None, 11434,
])
def test_other_base_urls_are_refused_in_korean(url):
    with pytest.raises(ValueError, match="이 컴퓨터 안의 주소"):
        explain.check_base_url(url)


def test_a_config_with_a_remote_base_url_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    monkeypatch.delenv("CODEVISION_OLLAMA_URL")
    (tmp_path / "config.json").write_text(
        json.dumps({"ai": {"provider": "local", "enabled": True, "base_url": "http://203.0.113.5:11434"}}), encoding="utf-8")
    settings = ai_settings()
    assert settings.url_error == messages.BAD_URL
    with pytest.raises(ValueError, match="이 컴퓨터 안의 주소"):
        get_explainer()
    block = explain.status()
    assert block["available"] is False and block["can_start"] is False
    assert block["message"] == messages.BAD_URL

    monkeypatch.setenv("CODEVISION_OLLAMA_URL", "http://198.51.100.7:11434")  # the override is checked too
    monkeypatch.setattr(providers, "read_config", lambda: {"ai": dict(providers.DEFAULT_AI)})
    assert ai_settings().url_error == messages.BAD_URL


def test_get_explainer(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    assert isinstance(get_explainer({"ai": {"provider": "none"}}), NoneExplainer)
    assert NoneExplainer().available() is False
    local = get_explainer({"ai": {"provider": "local", "model": "gemma3:4b"}})
    assert isinstance(local, explain.OllamaExplainer)
    assert (local.name, local.model) == ("local", "gemma3:4b")
    assert local.base_url == os.environ["CODEVISION_OLLAMA_URL"]  # conftest's dead port
    assert local.available() is False  # nothing listens there
    with pytest.raises(NotImplementedError, match="준비 중"):
        get_explainer({"ai": {"provider": "cloud"}})
    with pytest.raises(ValueError, match="알 수 없는"):
        get_explainer({"ai": {"provider": "magic"}})


# --- x, folder contexts and annotate ---------------------------------------------------------

def test_x_keeps_the_node_signatures_in_order_and_falls_back_to_rule_text():
    node = small_graph()["nodes"]["main.py"]
    exp = Explanation(s="AI 요약", d="AI 설명이에요.", f={"helper(x)": "AI 도움 설명", "other()": "목록에 없는 함수"})
    assert service.make_x(node, exp) == {"s": "AI 요약", "d": "AI 설명이에요.",
                                         "f": [["main()", ""], ["helper(x)", "AI 도움 설명"]]}
    # a model that drops the "(...)" part still lands on the right signature when the name is unique
    loose = Explanation(s="", d="", f={"main": "프로그램을 시작해요.", "helper": "도와요."})
    x = service.make_x(node, loose)
    assert x == {"s": "시작 파일", "d": "규칙으로 만든 설명이에요.",
                 "f": [["main()", "프로그램을 시작해요."], ["helper(x)", "도와요."]]}
    assert node["s"] == "시작 파일" and node["f"][0] == ["main()", ""]  # the rule texts are never changed

    twins = {"f": [["get(a)", "규칙 1"], ["get(a, b)", "규칙 2"]]}
    assert service.make_x(twins, Explanation(s="s", f={"get": "모호해요"}))["f"] == [["get(a)", "규칙 1"], ["get(a, b)", "규칙 2"]]
    assert service.make_x({"t": "d", "s": "폴더"}, Explanation(d="폴더 설명")) == {"s": "폴더", "d": "폴더 설명", "f": []}


def test_folder_context_uses_ai_summaries_of_children_when_there_are_some():
    nodes = small_graph()["nodes"]
    ctx = service.folder_context(nodes, ".", {"lib": {"s": "AI가 본 lib"}})
    assert ctx == FolderContext(rel_path="", name="app", summary_hint="규칙 요약",
                                children=[("lib", "AI가 본 lib"), ("main.py", "시작 파일"), ("README.md", "소개 문서")])
    assert service.folder_key(ctx) != service.folder_key(service.folder_context(nodes, ".", {}))
    assert service.folder_context(nodes, "lib", {}).rel_path == "lib"


def test_job_items_are_files_then_folders_deepest_first_and_the_root_last():
    nodes = small_graph()["nodes"]
    nodes["lib/deep"] = {"n": "deep", "t": "d", "s": "", "k": []}
    nodes[".env"] = {"n": ".env", "t": "f", "s": "환경 설정"}
    nodes["secrets.py"] = {"n": "secrets.py", "t": "f", "lang": "python", "s": "비밀"}
    assert service.job_items(nodes) == ["lib/util.py", "main.py", "lib/deep", "lib", "."]


def test_annotate_adds_cached_x_only_while_ai_is_on_and_never_calls_the_model(tmp_path):
    root = write_project(tmp_path / "app")
    cache = ExplanationCache(tmp_path / "cache.sqlite")
    built = []
    ai = service.AIService(cache, lambda r, demo: built.append(r) or small_graph())
    model = "qwen2.5:7b"
    on = {"enabled": True, "model": model}

    graph = ai.annotate(small_graph(), root, status=dict(on))
    assert graph["ai"] == on and not any("x" in n for n in graph["nodes"].values())

    cache.put("local", model, prompts.PROMPT_VERSION, blob_sha(SOURCES["lib/util.py"]),
              Explanation(s="AI 도우미", d="AI가 본 도우미예요.", f={"slug(text)": "주소용 글로 바꿔요."}))
    lib_ctx = service.folder_context(small_graph()["nodes"], "lib", {"lib/util.py": {"s": "AI 도우미"}})
    cache.put("local", model, prompts.PROMPT_VERSION, service.folder_key(lib_ctx), Explanation(s="AI lib"), kind="folder")

    graph = ai.annotate(small_graph(), root, status=dict(on))
    nodes = graph["nodes"]
    assert nodes["lib/util.py"]["x"] == {"s": "AI 도우미", "d": "AI가 본 도우미예요.", "f": [["slug(text)", "주소용 글로 바꿔요."]]}
    assert nodes["lib/util.py"]["s"] == "도우미"  # rule text untouched
    assert nodes["lib"]["x"]["s"] == "AI lib"  # the folder key was built from the child's cached summary
    assert "x" not in nodes["main.py"] and "x" not in nodes["."] and "x" not in nodes["README.md"]

    off = ai.annotate(small_graph(), root, status={"enabled": False, "model": model})
    assert not any("x" in n for n in off["nodes"].values())
    other_model = ai.annotate(small_graph(), root, status={"enabled": True, "model": "gemma3:4b"})
    assert not any("x" in n for n in other_model["nodes"].values())
    assert built == []  # annotate never analyzes or explains anything
    cache.close()


def test_ai_error_replies():
    assert AIError("secret", messages.SECRET).reply() == {"error": messages.SECRET, "code": "secret"}
    assert AIError("secret", "x").status == 422
    assert AIError("timeout", "x").status == 504
    assert AIError("off", "x").status == 409
    assert AIError("bad_request", "잘못").reply() == {"error": "잘못"}
    assert AIError("bad_request", "잘못").status == 400


def test_all_korean_messages_are_plain_haeyo_sentences():
    texts = [v for k, v in vars(messages).items() if k.isupper() and isinstance(v, str)]
    assert len(texts) > 30
    for text in texts:
        assert text.endswith(("요.", "요:", "요: {model}", "요.\"", "요)")) or text.rstrip(".").endswith("요"), text
        assert "합니다" not in text and "습니다" not in text, text


# --- prompts.py contract (the wording belongs to the prompt work) -------------------------------

def test_prompts_contract():
    assert prompts.PROMPT_VERSION
    assert isinstance(prompts.SYSTEM_PROMPT, str) and prompts.SYSTEM_PROMPT
    assert isinstance(prompts.ANSWER_SCHEMA, dict)
    assert isinstance(prompts.MODEL_OPTIONS, dict)
    assert list(prompts.RECOMMENDED_MODELS) and all(isinstance(m, str) for m in prompts.RECOMMENDED_MODELS)
    ctx = FileContext(rel_path="src/api.js", language="javascript", source="export function a() {}",
                      summary_hint="요청 함수", symbols=["a()"], used_by=["src/App.jsx"])
    text = prompts.file_prompt(ctx)
    for part in ("src/api.js", "a()", "export function a() {}"):
        assert part in text
    long_source = "x = 1\n" * 20_000
    assert len(prompts.file_prompt(FileContext("big.py", "python", long_source))) < len(long_source)
    folder = prompts.folder_prompt(FolderContext(rel_path="src", name="src", children=[("api.js", "요청 함수")]))
    assert "api.js" in folder and "요청 함수" in folder


def test_parse_response_and_postprocess_contract():
    answer = '```json\n{"s": "요약", "d": "설명이에요.", "f": {"a()": "하는 일"}}\n```'
    exp = prompts.parse_response(answer)
    assert isinstance(exp, Explanation) and exp.s == "요약" and exp.f.get("a()") == "하는 일"
    assert prompts.parse_response("잘 모르겠어요") is None
    assert prompts.parse_response("{not json}") is None
    ctx = FileContext("a.py", "python", "def a(): pass", symbols=["a()"])
    assert isinstance(prompts.postprocess(exp, ctx), Explanation)
