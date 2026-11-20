import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import codevision.lang as lang
from codevision import describe
from codevision.graph import build
from codevision.model import (
    FileFacts,
    HttpCall,
    HttpRoute,
    ImportRef,
    ProjectIndex,
    Symbol,
    http_paths_match,
    join_rel,
    normalize_http_path,
    parent_dir,
)

# ---------------------------------------------------------------- fake analyzers
#
# Fake sources use one instruction per line:
#   import <path relative to the file>   fw <name>   entry <reason>   doc <text>
#   call|route <METHOD> <path>   def <signature> | <doc>   class <Name>   component <Name>
#   boom (analyze raises)   any other line is ignored


def make_analyzer(language, extensions, entries=None):
    calls = {"project_entries": 0}

    def analyze(rel, source):
        facts = FileFacts(path=rel, language=language, lines=len(source.splitlines()))
        for raw in source.splitlines():
            word, _, rest = raw.strip().partition(" ")
            rest = rest.strip()
            if word == "boom":
                raise RuntimeError("analyzer bug")
            if word == "import":
                facts.imports.append(ImportRef(spec=rest))
            elif word == "fw":
                facts.frameworks.add(rest)
            elif word == "entry":
                facts.is_entry, facts.entry_reason = True, rest
            elif word == "doc":
                facts.module_doc = rest
            elif word in ("call", "route"):
                method, path = rest.split()
                if word == "call":
                    facts.http_calls.append(HttpCall(method=method, path=normalize_http_path(path)))
                else:
                    facts.http_routes.append(HttpRoute(method=method, path=normalize_http_path(path)))
            elif word == "def":
                sig, _, doc = rest.partition("|")
                name = sig.split("(")[0].strip()
                facts.symbols.append(Symbol(name=name, kind="function", signature=sig.strip(), doc=doc.strip()))
            elif word == "class":
                facts.symbols.append(Symbol(name=rest, kind="class", signature=f"class {rest}"))
            elif word == "component":
                facts.symbols.append(Symbol(name=rest, kind="component", signature=f"{rest}()"))
        return facts

    def resolve(imp, importer, index):
        if imp.spec == "explode":
            raise RuntimeError("resolver bug")
        target = join_rel(parent_dir(importer), imp.spec)
        return [target] if target and index.exists(target) else []

    def project_entries(index):
        calls["project_entries"] += 1
        return dict(entries or {})

    return SimpleNamespace(__name__=f"fake_{language}", EXTENSIONS=extensions, analyze=analyze,
                           resolve=resolve, project_entries=project_entries, calls=calls)


@pytest.fixture
def fake_lang(monkeypatch):
    py = make_analyzer("python", (".py",))
    js = make_analyzer("javascript", (".js", ".jsx"),
                       entries={"web/src/main.js": "index.html에서 불러와요", "not/here.js": "없는 파일"})
    modules = [py, js]

    def analyzer_for(rel):
        name = rel.rsplit("/", 1)[-1].lower()
        return next((m for m in modules if name.endswith(m.EXTENSIONS)), None)

    monkeypatch.setattr(lang, "analyzers", lambda: list(modules))
    monkeypatch.setattr(lang, "analyzer_for", analyzer_for)
    monkeypatch.setattr(lang, "load_errors", lambda: {})
    return SimpleNamespace(py=py, js=js, modules=modules)


def write(root, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


SHOP = {
    "README.md": "# Shop\n\n[![badge](https://x/y.svg)](https://x)\n\n작은 쇼핑몰 예제예요. 화면과 서버가 있어요.\n",
    ".gitignore": "secret/\n",
    "secret/key.txt": "hidden by .gitignore",
    "node_modules/react/index.js": "fw react",
    "web/package.json": '{"dependencies": {"react": "^18.3.1"}}',
    "web/index.html": '<script type="module" src="/src/main.js"></script>\n',
    "web/vite.config.js": "fw vite\n",
    "web/src/main.js": "import ./App.jsx\nfw react\nentry createRoot로 화면을 그려요\n",
    "web/src/App.jsx": "import ./api.js\nimport ./Missing.js\nimport explode\ncomponent App\nfw react\n",
    "web/src/api.js": "call GET ${}/items\ncall DELETE ${}/items/${}\ndef getItems() | 목록을 가져와요\ndef deleteItem(id)\n",
    "web/src/logo.png": b"\x89PNG\x00\x00",
    "server/app.py": ("doc 서버 앱이에요\nimport db.py\nfw fastapi\nentry 서버를 만드는 파일\n"
                      "route GET /items\nroute DELETE /items/{item_id}\nroute POST /orders\n"),
    "server/db.py": "fw sqlalchemy\nengine = create_engine('sqlite:///./shop.db')\n",
    "server/old_helpers.py": "def format_price(value)\n",
    "server/test_app.py": "import app.py\n",
    "server/__init__.py": "",
}


def check_consistency(graph):
    """Shape checks that every graph must pass."""
    assert json.loads(json.dumps(graph, ensure_ascii=False)) == graph  # no sets or tuples
    nodes = graph["nodes"]
    assert graph["root"] == "." and graph["focus"] in nodes
    for node_id, node in nodes.items():
        assert node["s"] and node["d"], node_id
        assert node["a"] in ("ui", "sv", "ot")
        if node["t"] == "d":
            assert all(k in nodes for k in node["k"])
            folders = [k for k in node["k"] if nodes[k]["t"] == "d"]
            assert node["k"][:len(folders)] == folders  # folders first
        else:
            assert isinstance(node["l"], int) and len(node["f"]) <= 8
            for key in ("u", "b", "h"):
                assert node[key] == sorted(node[key], key=lambda p: (p.lower(), p))
                assert all(t in nodes for t in node[key])
            for target in node["u"]:
                assert node_id in nodes[target]["b"]
            for target in node["h"]:
                assert target in node["u"] or target in node["b"]


def test_build_with_fake_analyzers(tmp_path, fake_lang):
    write(tmp_path, SHOP)
    graph = build(str(tmp_path))
    check_consistency(graph)
    nodes = graph["nodes"]

    assert graph["name"] == tmp_path.name
    assert graph["source"] == {"kind": "local", "path": str(tmp_path)}
    assert graph["hidden"] == ["node_modules"]
    assert graph["stats"] == {"folders": 3, "files": 14, "entries": 2, "orphans": 1, "skipped": 0}
    assert graph["areas"] == {"ui": "화면 코드", "sv": "서버 코드", "ot": "문서·설정"}
    assert graph["stack"] == [
        {"area": "ui", "label": "화면", "tech": "React"},
        {"area": "sv", "label": "서버", "tech": "FastAPI"},
        {"area": "sv", "label": "DB", "tech": "SQLite"},
    ]
    assert graph["warnings"] == [] and graph["truncated"] is False
    assert graph["ai"]["provider"] == "none"
    assert graph["focus"] == "server/app.py"
    assert fake_lang.py.calls["project_entries"] == 1 and fake_lang.js.calls["project_entries"] == 1

    assert nodes["."]["k"] == ["server", "web", ".gitignore", "README.md"]
    assert nodes["web/src"]["k"] == ["web/src/api.js", "web/src/App.jsx", "web/src/logo.png", "web/src/main.js"]

    app, api = nodes["server/app.py"], nodes["web/src/api.js"]
    assert app["u"] == ["server/db.py"]
    assert app["b"] == ["server/test_app.py", "web/src/api.js"]
    assert app["h"] == ["web/src/api.js"] and api["h"] == ["server/app.py"]
    assert api["u"] == ["server/app.py"] and api["b"] == ["web/src/App.jsx"]
    assert nodes["web/src/App.jsx"]["u"] == ["web/src/api.js"]
    assert nodes["web/src/main.js"]["u"] == ["web/src/App.jsx"]

    entries = {k for k, v in nodes.items() if v.get("e")}
    assert entries == {"server/app.py", "web/src/main.js"}
    orphans = {k for k, v in nodes.items() if v.get("o")}
    assert orphans == {"server/old_helpers.py"}

    areas = {k: v["a"] for k, v in nodes.items()}
    assert areas["server/app.py"] == "sv" and areas["server"] == "sv"
    assert areas["web/src/api.js"] == "ui"  # plain .js inside a React package
    assert areas["web/index.html"] == "ui" and areas["web"] == "ui" and areas["web/src"] == "ui"
    assert areas["web/vite.config.js"] == "ot" and areas["web/package.json"] == "ot"
    assert areas["README.md"] == "ot" and areas["web/src/logo.png"] == "ot" and areas["."] == "ot"

    icons = {k: v.get("i") for k, v in nodes.items() if v["t"] == "f"}
    assert icons["README.md"] == "ti-file-text"
    assert icons["web/package.json"] == "ti-file-settings" and icons["web/vite.config.js"] == "ti-file-settings"
    assert icons[".gitignore"] == "ti-file-settings"
    assert icons["web/src/logo.png"] == "ti-photo"
    assert icons["server/old_helpers.py"] == "ti-file-alert"
    assert icons["server/app.py"] is None and icons["web/src/api.js"] is None

    assert api["lang"] == "javascript" and app["lang"] == "python" and "lang" not in nodes["README.md"]
    assert api["f"] == [["getItems()", "목록을 가져와요"], ["deleteItem(id)", ""]]
    assert api["l"] == 4 and nodes["web/src/logo.png"]["l"] == 0

    # Korean descriptions
    assert nodes["."]["s"] == "작은 쇼핑몰 예제예요"
    assert nodes["."]["d"] == "작은 쇼핑몰 예제예요. 화면과 서버가 있어요."
    assert app["s"] == "서버 앱이에요"  # module_doc wins
    assert "다른 파일 1개를 불러 쓰고, 2개 파일이 이 파일을 써요." in app["d"]
    assert nodes["server/old_helpers.py"]["s"] == "사용되지 않는 파일"
    assert "불러 쓰지 않아요" in nodes["server/old_helpers.py"]["d"]
    assert nodes["web/src/main.js"]["s"] == "화면 시작점"
    assert "index.html에서 불러와요" in nodes["web/src/main.js"]["d"]
    assert nodes["README.md"]["s"] == "프로젝트 소개 문서"
    assert nodes["web/package.json"]["s"] == "라이브러리·실행 설정"
    assert nodes["server/test_app.py"]["s"] == "테스트 코드"
    assert nodes["web/src/App.jsx"]["s"] == "화면 전체 틀"
    assert nodes["server/db.py"]["s"] == "DB 연결 준비"
    assert nodes["web/src/logo.png"]["s"] == "이미지 파일"
    assert nodes["web"]["s"] == "사용자가 보는 화면 (React)"
    assert nodes["server"]["s"] == "데이터 처리 서버 (FastAPI)"
    assert nodes["server"]["d"].endswith("파일 5개가 들어 있어요.")
    assert nodes["web"]["d"].endswith("파일 3개, 폴더 1개가 들어 있어요.")


def test_warnings_for_load_errors_skipped_and_truncated(tmp_path, fake_lang, monkeypatch):
    write(tmp_path, {"a.py": "x\n", "b.py": "boom\n", "big.txt": "x" * 1_000_001})
    monkeypatch.setattr(lang, "load_errors", lambda: {"codevision.lang.js_lang": "No module named tree_sitter"})
    graph = build(tmp_path)
    check_consistency(graph)
    text = " ".join(graph["warnings"])
    assert "JavaScript" in text
    assert "1MB" in text and graph["stats"]["skipped"] == 1
    assert "끝까지 읽지 못한 파일이 1개" in text  # the analyzer crashed on b.py
    assert graph["nodes"]["big.txt"]["s"] == "너무 커서 건너뛴 파일"

    small = build(tmp_path, max_files=2)
    assert small["truncated"] is True
    assert small["stats"]["files"] == 2
    assert any("2개만" in w for w in small["warnings"])


def test_js_area_and_label_fallbacks(tmp_path, fake_lang):
    write(tmp_path, {"tool.js": "def run()\n"})
    graph = build(tmp_path)
    assert graph["nodes"]["tool.js"]["a"] == "sv"  # no screen framework anywhere
    assert graph["areas"]["sv"] == "코드" and graph["stack"] == []

    write(tmp_path, {"view.jsx": "fw react\nimport tool.js\n", "api/package.json": '{"dependencies": {"express": "4"}}',
                     "api/handler.js": "x\n", "api/server.js": "fw express\nimport handler.js\nentry 서버를 켜요\n"})
    graph = build(tmp_path)
    nodes = graph["nodes"]
    assert nodes["tool.js"]["a"] == "ui"  # project has a screen framework
    assert nodes["api/handler.js"]["a"] == "sv"  # inside a server package
    assert nodes["api/server.js"]["a"] == "sv" and nodes["api/server.js"]["s"] == "서버 시작점"
    assert graph["areas"]["sv"] == "서버 코드"
    assert [item["tech"] for item in graph["stack"]] == ["React", "Express"]


def test_focus_tie_prefers_entry_then_path(tmp_path, fake_lang):
    write(tmp_path, {"a.py": "import x.py\n", "b.py": "import y.py\nentry 직접 실행\n", "x.py": "", "y.py": ""})
    assert build(tmp_path)["focus"] == "b.py"


def test_http_method_mismatch_is_not_linked(tmp_path, fake_lang):
    write(tmp_path, {"web/api.js": "call POST /items\n", "srv/app.py": "route GET /items\nentry 서버\n"})
    nodes = build(tmp_path)["nodes"]
    assert nodes["web/api.js"]["u"] == [] and nodes["srv/app.py"]["h"] == []


def test_functions_are_capped_at_eight(tmp_path, fake_lang):
    write(tmp_path, {"many.py": "".join(f"def f{i}()\n" for i in range(12))})
    assert len(build(tmp_path)["nodes"]["many.py"]["f"]) == 8


def test_broken_project_entries_do_not_crash(tmp_path, fake_lang, monkeypatch):
    def broken(index):
        raise RuntimeError("bug")

    monkeypatch.setattr(fake_lang.js, "project_entries", broken)
    write(tmp_path, {"main.js": "import explode\n"})
    graph = build(tmp_path)
    check_consistency(graph)
    assert graph["nodes"]["main.js"]["o"] == 1


def test_empty_project(tmp_path, fake_lang):
    graph = build(tmp_path)
    check_consistency(graph)
    assert graph["focus"] == "." and graph["nodes"]["."]["k"] == []
    assert graph["nodes"]["."]["s"] == "프로젝트 폴더"


def test_describe_helpers():
    assert describe.connections(2, 3) == "다른 파일 2개를 불러 쓰고, 3개 파일이 이 파일을 써요."
    assert describe.counts(4, 1) == "파일 4개, 폴더 1개가 들어 있어요."
    long_line = describe.short("아주 긴 설명이 들어 있는 모듈 문서 첫 줄입니다 정말 길어요")
    assert long_line.endswith("…") and len(long_line) <= 25
    s, d = describe.describe_folder("src/components", files=2)
    assert s == "화면 부품 모음" and d.endswith("파일 2개가 들어 있어요.")
    s, d = describe.describe_root("x", readme=None, languages=["Python"], files=3, folders=1)
    assert s == "Python 프로젝트" and d == "Python 코드로 된 프로젝트예요. 파일 3개, 폴더 1개가 들어 있어요."
    assert describe.readme_line("# T\n\n```\ncode\n```\n- list\n**굵은** [링크](u) 설명이에요.\n") == "굵은 링크 설명이에요."
    assert describe.describe_file("web/source.config.ts")[0] == "source 설정"
    assert describe.describe_file(".eslintrc.cjs")[0] == "ESLint 설정"
    assert describe.describe_file(".env.example")[0] == "환경 변수 예시"
    assert describe.describe_file("docs/guide.md", title="설치 안내서")[0] == "설치 안내서"
    s, d = describe.describe_file("app.py", FileFacts(path="app.py", language="python", module_doc=".", parse_error=True))
    assert s == "Python 코드" and "문법 오류" in d


def test_describe_wording_on_real_code():
    # s keeps a whole first sentence, or the name before "name: details", never one stray word
    assert describe.short("Tests for the AI explanation skeleton. No AI service is ever called.") == \
        "Tests for the AI explanation skeleton"
    assert describe.short("Graph builder: scan + analyzers -> the graph JSON of the docs") == "Graph builder"
    assert describe.short("Important: don't add things to this module, as they end up in globals") != "Important"
    assert describe.short("-" * 46 + " turtleDemo - Help") not in ("", "…")
    assert describe.short("e.g. this is not a second sentence but one long line of text") .endswith("…")
    assert describe.counts(3642, 478) == "파일 3,642개, 폴더 478개가 들어 있어요."
    assert describe.connections(1200, 0) == "다른 파일 1,200개를 불러 써요."
    # roles that real projects showed wrongly
    http = FileFacts(path="urllib3/connection.py", language="python", symbols=[Symbol("C", "class", "class C")])
    assert describe.describe_file("urllib3/connection.py", http)[0] != "DB 연결 준비"
    marker = FileFacts(path="utils/__init__.py", language="python")
    s, d = describe.describe_file("utils/__init__.py", marker)
    assert s == "패키지 표시 파일" and "이어져 있지 않아요" not in d
    assert describe.describe_file("utils/__init__.py", marker, uses=3)[0] == "패키지 입구 파일"
    assert describe.describe_file("tests/conftest.py", FileFacts(path="tests/conftest.py", language="python"))[0] == \
        "테스트 공통 준비"
    assert describe.describe_file("app/tests.py", FileFacts(path="app/tests.py", language="python"))[0] == "테스트 코드"
    generated = FileFacts(path="_version.py", language="python",
                          module_doc="file generated by vcs-versioning don't change, don't track in version control")
    assert describe.describe_file("_version.py", generated)[0] == "자동으로 만든 코드"
    demo = FileFacts(path="rich/console.py", language="python", module_doc="Console output.")
    assert "직접 실행할 수도 있어요." in describe.describe_file("rich/console.py", demo, used_by=50, runnable=True)[1]
    entry = FileFacts(path="web/app.js", language="javascript", module_doc="CodeVision web UI.")
    s, d = describe.describe_file("web/app.js", entry, entry=True, entry_reason="index.html에서 처음 불러오는 파일")
    assert s == "CodeVision web UI" and "index.html에서 처음 불러오는 파일" in d and "이어져 있지 않아요" not in d
    assert describe.describe_file("lock.txt", unreadable=True)[0] == "열 수 없는 파일"
    kinds = {name: describe.describe_file(name, binary=True)[0]
             for name in ("t64.exe", "_speedups.pyd", "model.pkl", "bun.lockb", "font.woff2")}
    assert kinds == {"t64.exe": "실행 프로그램", "_speedups.pyd": "컴파일된 파일", "model.pkl": "데이터·모델 파일",
                     "bun.lockb": "라이브러리 버전 기록", "font.woff2": "글꼴 파일"}
    assert describe.describe_file("pkg/py.typed")[0] == "타입 정보 파일"
    assert describe.describe_file("certifi/cacert.pem")[0] == "인증서 파일"
    # folders
    assert describe.describe_folder("pygments/styles", area="sv")[0] == "서버 코드 모음"  # Python code, not CSS
    assert describe.describe_folder("web/styles", area="ui")[0] == "화면 꾸미기(CSS)"
    assert describe.describe_folder("pip/_vendor")[0] == "외부에서 가져온 코드"
    assert describe.describe_folder("engine", doc="게임 엔진이에요.")[0] == "게임 엔진이에요"
    assert describe.describe_folder("utils", doc="Small helpers.")[0] == "도우미 코드 모음"  # Korean dictionary first
    assert describe.describe_folder("utils", doc="문자열 도우미예요.")[0] == "문자열 도우미예요"
    assert describe.describe_folder("pkg", area="sv", package=True)[1].startswith("함께 쓰는 Python 코드")
    assert describe.describe_folder("site", mixed="server")[0] == "화면·서버 코드 모음"
    assert describe.describe_folder("app", mixed="server")[0] == "앱 본체"  # a known name says more
    assert "권한" in describe.describe_folder("locked", unreadable=True)[1]
    assert "다 보지 못했어요" in describe.describe_folder("big", files=0, cut_off=True)[1]
    s, d = describe.describe_root("rich", doc="Rich text and beautiful formatting in the terminal.",
                                  languages=["Python"], files=3)
    assert s == "Rich text and beautiful…" and d.startswith("Rich text and beautiful formatting in the terminal.")
    assert "Python 코드로 된 프로젝트예요." in d


def test_root_languages_ignore_a_stray_file(tmp_path, fake_lang):
    write(tmp_path, {f"m{i}.py": "x\n" for i in range(30)})
    write(tmp_path, {"worker.js": "x\n"})
    assert build(tmp_path)["nodes"]["."]["s"] == "Python 프로젝트"


def test_mixed_folder_is_not_called_screen_code(tmp_path, fake_lang):
    write(tmp_path, {"site/web/App.jsx": "fw react\n", "site/web/List.jsx": "fw react\n",
                     "site/srv/app.py": "fw fastapi\nentry 서버\n", "site/srv/db.py": "import app.py\n"})
    nodes = build(tmp_path)["nodes"]
    assert nodes["site"]["s"] == "화면·서버 코드 모음"
    assert nodes["site/web"]["s"] == "사용자가 보는 화면 (React)"


def test_unreadable_folder_and_file_and_nul_bytes(tmp_path, fake_lang, monkeypatch):
    write(tmp_path, {"ok/a.py": "x\n", "locked/b.py": "x\n", "notes.txt": "hello", "secret.txt": "x",
                     "utf16.txt": "할 일".encode("utf-16")})
    real_scandir = os.scandir

    def fake_scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError("denied")
        return real_scandir(path)

    import codevision.graph as graph_mod
    import codevision.scan as scan_mod
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path).endswith("secret.txt"):
            raise PermissionError("in use")
        return real_open(path, *args, **kwargs)

    real_read = ProjectIndex.read_text
    monkeypatch.setattr(scan_mod.os, "scandir", fake_scandir)
    monkeypatch.setattr(graph_mod, "open", fake_open, raising=False)
    monkeypatch.setattr(ProjectIndex, "read_text",
                        lambda self, rel, limit=2_000_000, **kw: None if rel == "secret.txt"
                        else real_read(self, rel, limit, **kw))
    graph = build(tmp_path)
    check_consistency(graph)
    nodes = graph["nodes"]
    assert "권한" in nodes["locked"]["d"]
    assert nodes["secret.txt"]["s"] == "열 수 없는 파일" and nodes["secret.txt"]["i"] == "ti-lock"
    assert nodes["utf16.txt"]["s"] == "글자가 아닌 파일"  # NUL bytes: not text after all
    assert nodes["notes.txt"]["l"] == 1
    text = " ".join(graph["warnings"])
    assert "열어 볼 수 없는 폴더 1개" in text and "열 수 없는 파일 1개" in text


def test_text_files_are_counted_without_keeping_their_text(tmp_path, monkeypatch):
    # 300 data files just under 1MB once stayed decoded in memory until the graph was done
    rows = "a,b\r1,2\r\n3,4\n5,6\u20287,8\x0c9\n"
    write(tmp_path, {"main.py": "import helpers\n", "helpers.py": "x = 1\n", "data/rows.csv": rows,
                     "notes.md": "# 회의 메모\n\n다음 주에 다시 봐요.\n"})
    import codevision.graph as graph_mod

    seen = {}
    real_scan = graph_mod.scan

    def keep_index(*args, **kwargs):
        result = real_scan(*args, **kwargs)
        seen["index"] = result.index
        return result

    monkeypatch.setattr(graph_mod, "scan", keep_index)
    nodes = build(tmp_path)["nodes"]
    assert nodes["data/rows.csv"]["l"] == len(rows.splitlines()) == 6  # counted as before
    assert nodes["notes.md"]["l"] == 3 and "회의 메모" in nodes["notes.md"]["s"] + nodes["notes.md"]["d"]
    assert nodes["main.py"]["l"] == 1 and nodes["main.py"]["u"] == ["helpers.py"]
    assert not set(seen["index"]._text) & {"data/rows.csv", "notes.md", "main.py", "helpers.py"}


@pytest.mark.parametrize("root, label", [
    ("C:\\Users\\me\\my-app", ("my-app", "my-app/")),
    ("C:\\Users\\me\\my-app\\", ("my-app", "my-app/")),
    ("/", ("/", "/")),
    pytest.param("C:\\", ("C:", "C:/"), marks=pytest.mark.skipif(os.name != "nt", reason="Windows drive")),
    pytest.param("D:/", ("D:", "D:/"), marks=pytest.mark.skipif(os.name != "nt", reason="Windows drive")),
])
def test_the_analyzed_folder_gets_a_clean_name(root, label):
    from codevision.graph import _root_label

    assert _root_label(root) == label  # a drive root is "C:" and "C:/", not "C:\\" and "C:\\/"


def test_counts_in_warnings_have_thousands_separators(fake_lang):
    from codevision.graph import _warnings

    result = SimpleNamespace(skipped=["big.txt"] * 1234, truncated=False, unreadable=[f"d{i}" for i in range(1500)],
                             file_limit=False, crowded=[], max_entries=0, too_deep=False)
    facts = {f"f{i}.py": FileFacts(path=f"f{i}.py", language="python", parse_error=True) for i in range(1001)}
    text = " ".join(_warnings(result, facts, 5000, 2000))
    for count in ("폴더 1,500개", "열 수 없는 파일 2,000개", "1MB가 넘는 파일 1,234개", "못한 파일이 1,001개"):
        assert count in text


def test_truncated_folders_are_not_shown_as_empty(tmp_path, fake_lang):
    write(tmp_path, {"a.py": "x\n", "b.py": "x\n", "later/c.py": "x\n", "later/inner/d.py": "x\n"})
    graph = build(tmp_path, max_files=2)
    check_consistency(graph)
    nodes = graph["nodes"]
    assert graph["truncated"] is True and nodes["later"]["k"] == ["later/inner"]
    for folder in ("later", "later/inner"):  # found, but the limit came first
        assert "다 보지 못했어요" in nodes[folder]["d"] and "비어 있는" not in nodes[folder]["d"]


# ---------------------------------------------------------------- real analyzers on small real-world shapes

def write_real(root, files):
    write(root, files)
    return build(str(root))


def test_package_analyzed_as_the_root_has_no_false_orphans(tmp_path):
    root = tmp_path / "mytool"
    graph = write_real(root, {
        "__init__.py": '"""Small tool that renames photos."""\n',
        "__main__.py": "from mytool.cli import main\nmain()\n",
        "cli.py": "from mytool.core import rename\n\ndef main():\n    rename()\n",
        "core.py": "def rename():\n    pass\n",
        "commands/__init__.py": "import importlib\n"
                                "NAMES = ['mytool.commands.copy', 'mytool.commands.move']\n"
                                "def load(name):\n    return importlib.import_module(name)\n",
        "commands/copy.py": "x = 1\n", "commands/move.py": "x = 1\n",
    })
    nodes = graph["nodes"]
    assert nodes["__main__.py"]["u"] == ["cli.py"] and nodes["cli.py"]["u"] == ["core.py"]
    assert nodes["commands/__init__.py"]["u"] == ["commands/copy.py", "commands/move.py"]
    assert graph["stats"]["orphans"] == 0
    assert nodes["."]["s"] == "Small tool that renames photos"  # the root package's docstring


def test_demo_main_block_in_an_imported_module_is_not_a_start_point(tmp_path):
    graph = write_real(tmp_path, {
        "main.py": "from helpers import add\n\nif __name__ == '__main__':\n    print(add(1, 2))\n",
        "helpers.py": "def add(a, b):\n    return a + b\n\nif __name__ == '__main__':\n    print(add(2, 3))\n",
        "tool.py": "if __name__ == '__main__':\n    print('only run directly')\n",
        "tests/test_tool.py": "import tool\n",
        "lib/__init__.py": "", "lib/__main__.py": "from lib import helpers\n",
    })
    nodes = graph["nodes"]
    assert nodes["main.py"].get("e") == 1
    assert "e" not in nodes["helpers.py"] and "o" not in nodes["helpers.py"]
    assert "직접 실행할 수도 있어요" in nodes["helpers.py"]["d"]
    assert nodes["tool.py"].get("e") == 1  # only a test imports it
    assert nodes["lib/__main__.py"].get("e") == 1


def test_django_project_files_are_not_false_orphans(tmp_path):
    graph = write_real(tmp_path, {
        "manage.py": "import os\n\ndef main():\n"
                     "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')\n",
        "mysite/__init__.py": "",
        "mysite/settings.py": "ROOT_URLCONF = 'mysite.urls'\nINSTALLED_APPS = ['polls.apps.PollsConfig']\n",
        "mysite/urls.py": "from django.urls import include, path\n"
                          "urlpatterns = [path('polls/', include('polls.urls'))]\n",
        "polls/__init__.py": "", "polls/apps.py": "from django.apps import AppConfig\n",
        "polls/urls.py": "from . import views\n", "polls/views.py": "def index(request):\n    pass\n",
        "polls/migrations/__init__.py": "", "polls/migrations/0001_initial.py": "from django.db import migrations\n",
    })
    nodes = graph["nodes"]
    assert nodes["manage.py"]["u"] == ["mysite/settings.py"]
    assert nodes["mysite/settings.py"]["u"] == ["mysite/urls.py", "polls/apps.py"]
    assert nodes["mysite/urls.py"]["u"] == ["polls/urls.py"]
    assert {k for k, v in nodes.items() if v.get("o")} == set()


def test_package_docstring_names_the_folder(tmp_path):
    graph = write_real(tmp_path, {
        "engine/__init__.py": '"""게임 화면을 그리는 엔진이에요."""\n',
        "engine/draw.py": "x = 1\n",
        "main.py": "from engine import draw\n",
    })
    assert graph["nodes"]["engine"]["s"] == "게임 화면을 그리는 엔진이에요"


# ---------------------------------------------------------------- review fixes (2026-11-19)

def test_truncation_warnings_say_why(tmp_path, fake_lang, monkeypatch):
    import codevision.scan as scan_mod

    write(tmp_path / "many", {"a.py": "x\n", "b.py": "x\n", "c.py": "x\n"})
    graph = build(tmp_path / "many", max_files=2)
    assert graph["truncated"] is True
    assert graph["warnings"] == ["파일이 너무 많아서 처음 2개만 분석했어요."]

    # A folder with a huge number of entries (hidden by .gitignore here): not the file limit
    write(tmp_path / "crowded", {".gitignore": "*.log\n", "main.py": "x\n", **{f"logs/{i:03d}.log": "" for i in range(30)}})
    monkeypatch.setattr(scan_mod, "MIN_ENTRIES", 10)
    graph = build(tmp_path / "crowded", max_files=5)
    check_consistency(graph)
    assert graph["truncated"] is True and graph["stats"]["files"] == 2
    assert graph["warnings"] == ["안에 든 것이 아주 많은 폴더(logs/)는 처음 20개 항목만 살펴봤어요."]
    assert "다 보지 못했어요" in graph["nodes"]["logs"]["d"]


def test_python_files_run_by_path_are_not_cleanup_candidates(tmp_path):
    graph = write_real(tmp_path, {
        "__pip-runner__.py": '"""Execute exactly this copy of pip, within a different environment."""\n',
        "old_helper.py": "def unused():\n    pass\n",
        "tables/__init__.py": "from importlib import import_module\n\n"
                              "def load(version):\n    name = f'.unicode{version}'\n"
                              "    return import_module(name, __package__)\n",
        "tables/unicode15-0-0.py": "TABLE = []\n", "tables/unicode16-0-0.py": "TABLE = []\n",
        "tables/_versions.py": "VERSIONS = []\n",
    })
    check_consistency(graph)
    nodes = graph["nodes"]
    runner = nodes["__pip-runner__.py"]
    assert "o" not in runner and runner["s"] == "Execute exactly this copy of pip…"  # its docstring stays
    assert "import 문으로는 불러올 수 없어서" in runner["d"]
    # import_module(f".unicode{version}", __package__) loads one of the package's unicode*.py modules
    assert nodes["tables/__init__.py"]["u"] == ["tables/unicode15-0-0.py", "tables/unicode16-0-0.py"]
    assert nodes["tables/unicode15-0-0.py"]["b"] == ["tables/__init__.py"]
    assert {k for k, v in nodes.items() if v.get("o")} == {"old_helper.py", "tables/_versions.py"}


EXPRESS_APP = {
    "server/package.json": '{"dependencies": {"express": "^4"}}',
    "server/index.js": "const express = require('express');\nconst todos = require('./routes/todos');\n"
                       "const app = express();\napp.use(express.json());\napp.use('/api/todos', todos);\napp.listen(3000);\n",
    "server/routes/todos.js": "const express = require('express');\nconst router = express.Router();\n"
                              "router.get('/', (req, res) => res.json([]));\nrouter.post('/', (req, res) => res.json({}));\n"
                              "router.delete('/:id', (req, res) => res.json({}));\nmodule.exports = router;\n",
    "client/package.json": '{"dependencies": {"react": "^18"}}',
    "client/src/api.js": "export const getTodos = () => fetch('/api/todos').then(r => r.json());\n"
                         "export const addTodo = (t) => fetch('/api/todos', {method: 'POST', body: JSON.stringify(t)});\n"
                         "export const delTodo = (id) => fetch(`/api/todos/${id}`, {method: 'DELETE'});\n",
}


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_express_router_mounted_under_a_prefix_is_linked(tmp_path, monkeypatch, tree_sitter):
    from codevision.lang import js_lang

    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    nodes = write_real(tmp_path, EXPRESS_APP)["nodes"]
    assert nodes["client/src/api.js"]["h"] == ["server/routes/todos.js"]
    assert nodes["server/routes/todos.js"]["h"] == ["client/src/api.js"]
    assert nodes["server/routes/todos.js"]["d"].startswith("/api/todos 주소로 오는 조회·추가·삭제 요청을 처리해요.")


def test_express_mounts_chain_through_routers(tmp_path):
    nodes = write_real(tmp_path, {
        "server.js": "import express from 'express';\nimport api from './api/index.js';\n"
                     "const app = express();\napp.use('/api', api);\napp.listen(3000);\n",
        "api/index.js": "import { Router } from 'express';\nimport todos from './todos.js';\n"
                        "const router = Router();\nrouter.use('/todos', todos);\nexport default router;\n",
        "api/todos.js": "import { Router } from 'express';\nconst router = Router();\n"
                        "router.get('/:id', (req, res) => res.json({}));\nexport default router;\n",
        "web/main.js": "fetch(`/api/todos/${id}`);\n",
    })["nodes"]
    assert nodes["web/main.js"]["h"] == ["api/todos.js"]  # /api + /todos + /:id


@pytest.mark.parametrize("main", [
    "from fastapi import FastAPI\nfrom routes import todos\napp = FastAPI()\n"
    "app.include_router(todos.router, prefix='/api/todos')\n",
    "from fastapi import FastAPI\nfrom routes.todos import router as todo_router\napp = FastAPI()\n"
    "app.include_router(todo_router, prefix='/api/todos')\n",
    "import fastapi\nimport routes.todos\nAPI = '/api'\napp = fastapi.FastAPI()\n"
    "app.include_router(routes.todos.router, prefix=API + '/todos')\n",
])
def test_fastapi_include_router_prefix_is_linked(tmp_path, main):
    nodes = write_real(tmp_path, {
        "backend/main.py": main,
        "backend/routes/__init__.py": "",
        "backend/routes/todos.py": "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
                                   "@router.get('/')\ndef list_todos():\n    return []\n\n"
                                   "@router.delete('/{todo_id}')\ndef delete(todo_id: int):\n    return {}\n",
        "frontend/src/api.js": "export const getTodos = () => fetch('/api/todos');\n"
                               "export const del = (id) => fetch(`/api/todos/${id}`, {method: 'DELETE'});\n",
    })["nodes"]
    todos = nodes["backend/routes/todos.py"]
    assert nodes["frontend/src/api.js"]["h"] == ["backend/routes/todos.py"]
    assert todos["f"] == [["list_todos()", "GET · /api/todos"], ["delete(todo_id)", "DELETE · /api/todos/{todo_id}"]]
    assert todos["d"].startswith("/api/todos 주소로 오는")


def test_flask_blueprint_registered_with_a_prefix_is_linked(tmp_path):
    nodes = write_real(tmp_path, {
        "app.py": "from flask import Flask\nfrom views.todos import bp\n\ndef create_app():\n"
                  "    app = Flask(__name__)\n    app.register_blueprint(bp, url_prefix='/api/todos')\n    return app\n",
        "views/__init__.py": "",
        "views/todos.py": "from flask import Blueprint\nbp = Blueprint('todos', __name__)\n\n"
                          "@bp.route('/')\ndef index():\n    return []\n",
        "web/todos.js": "export const load = () => fetch('/api/todos');\n",
    })["nodes"]
    assert nodes["web/todos.js"]["h"] == ["views/todos.py"]


FLASK_TODOS = ("from flask import Blueprint\nbp = Blueprint('todos', __name__, url_prefix='/todos')\n\n"
               "@bp.route('/<int:todo_id>', methods=['DELETE'])\ndef remove(todo_id):\n    return ''\n")


@pytest.mark.parametrize("files, path", [
    # register_blueprint's url_prefix stands instead of the blueprint's own one: /api/todos, not /api/todos/todos
    ({"app.py": "from flask import Flask\nfrom views.todos import bp\napp = Flask(__name__)\n"
                "app.register_blueprint(bp, url_prefix='/api/todos')\n"}, "/api/todos"),
    ({"app.py": "from flask import Flask\nfrom views.todos import bp\napp = Flask(__name__)\n"
                "app.register_blueprint(bp)\n"}, "/todos"),
    # registered on another blueprint: under that one's prefix, with its own (or the given) prefix after it
    ({"app.py": "from flask import Flask\nfrom api import api_bp\napp = Flask(__name__)\napp.register_blueprint(api_bp)\n",
      "api.py": "from flask import Blueprint\nfrom views.todos import bp\n"
                "api_bp = Blueprint('api', __name__, url_prefix='/api')\napi_bp.register_blueprint(bp)\n"}, "/api/todos"),
    ({"app.py": "from flask import Flask\nfrom api import api_bp\napp = Flask(__name__)\n"
                "app.register_blueprint(api_bp, url_prefix='/v1')\n",
      "api.py": "from flask import Blueprint\nfrom views.todos import bp\n"
                "api_bp = Blueprint('api', __name__, url_prefix='/api')\n"
                "api_bp.register_blueprint(bp, url_prefix='/items')\n"}, "/v1/items"),
    # three levels, two of them in api.py: /v1 stands instead of root's own prefix only, so the
    # blueprint between them still adds its part (/api, or the /x it was registered with)
    ({"app.py": "from flask import Flask\nfrom api import root\napp = Flask(__name__)\n"
                "app.register_blueprint(root, url_prefix='/v1')\n",
      "api.py": "from flask import Blueprint\nfrom views.todos import bp\n"
                "root = Blueprint('root', __name__, url_prefix='/root')\n"
                "api_bp = Blueprint('api', __name__, url_prefix='/api')\n"
                "root.register_blueprint(api_bp)\napi_bp.register_blueprint(bp)\n"}, "/v1/api/todos"),
    ({"app.py": "from flask import Flask\nfrom api import root\napp = Flask(__name__)\n"
                "app.register_blueprint(root, url_prefix='/v1')\n",
      "api.py": "from flask import Blueprint\nfrom views.todos import bp\nroot = Blueprint('root', __name__)\n"
                "api_bp = Blueprint('api', __name__, url_prefix='/api')\n"
                "root.register_blueprint(api_bp, url_prefix='/x')\napi_bp.register_blueprint(bp)\n"}, "/v1/x/todos"),
])
def test_flask_register_prefix_takes_the_place_of_the_blueprints_own(tmp_path, files, path):
    todos = write_real(tmp_path, {**files, "views/__init__.py": "", "views/todos.py": FLASK_TODOS})["nodes"]["views/todos.py"]
    assert todos["f"] == [["remove(todo_id)", f"DELETE · {path}/<int:todo_id>"]]
    assert todos["d"].startswith(f"{path} 주소로 오는 삭제 요청을 처리해요.")


def test_fastapi_prefixes_are_joined_through_routers(tmp_path):
    nodes = write_real(tmp_path, {
        "main.py": "from fastapi import FastAPI\nfrom api import api_router\napp = FastAPI()\n"
                   "app.include_router(api_router, prefix='/v1')\n",
        "api.py": "from fastapi import APIRouter\nfrom routes import todos\napi_router = APIRouter(prefix='/api')\n"
                  "api_router.include_router(todos.router, prefix='/x')\n",
        "routes/__init__.py": "",
        "routes/todos.py": "from fastapi import APIRouter\nrouter = APIRouter(prefix='/todos')\n\n"
                           "@router.delete('/{todo_id}')\ndef remove(todo_id: int):\n    return {}\n",
        "web/todos.js": "export const del = (id) => fetch(`/v1/api/x/todos/${id}`, {method: 'DELETE'});\n",
    })["nodes"]
    todos = nodes["routes/todos.py"]
    assert nodes["web/todos.js"]["h"] == ["routes/todos.py"]
    assert todos["f"] == [["remove(todo_id)", "DELETE · /v1/api/x/todos/{todo_id}"]]  # every prefix, in order
    assert todos["d"].startswith("/v1/api/x/todos 주소로 오는 삭제 요청을 처리해요.")


def test_python_router_mounted_in_its_own_file(tmp_path):
    nodes = write_real(tmp_path, {
        "main.py": "from fastapi import APIRouter, FastAPI\napp = FastAPI()\nrouter = APIRouter()\n\n"
                   "@router.get('/')\ndef items():\n    return []\n\napp.include_router(router, prefix='/api/items')\n",
        "web/api.js": "export const load = () => fetch('/api/items');\n",
    })["nodes"]
    assert nodes["web/api.js"]["h"] == ["main.py"]


TODOS_ROUTER = "from fastapi import APIRouter\nrouter = APIRouter()\n\n@router.get('/todos')\ndef list_todos():\n    return []\n"


@pytest.mark.parametrize("files, rel, hint", [
    # the same router mounted twice in one file answers at both addresses, and its hint says so
    ({"routes/todos.py": TODOS_ROUTER,
      "main.py": "from fastapi import FastAPI\nfrom routes.todos import router\napp = FastAPI()\n"
                 "app.include_router(router, prefix='/v1')\napp.include_router(router, prefix='/v2')\n"},
     "routes/todos.py", "GET · /v1/todos, /v2/todos"),
    ({"main.py": TODOS_ROUTER + "\nfrom fastapi import FastAPI\napp = FastAPI()\n"
                 "app.include_router(router, prefix='/v1')\napp.include_router(router, prefix='/v2')\n"},
     "main.py", "GET · /v1/todos, /v2/todos"),
    # mounted twice in its own file, and that file mounted in turn: every address gets the outer prefix
    ({"api.py": TODOS_ROUTER + "api = APIRouter()\napi.include_router(router, prefix='/v1')\n"
                "api.include_router(router, prefix='/v2')\n",
      "main.py": "from fastapi import FastAPI\nfrom api import api\napp = FastAPI()\napp.include_router(api, prefix='/api')\n"},
     "api.py", "GET · /api/v1/todos, /api/v2/todos"),
    ({"views/__init__.py": "", "views/todos.py": FLASK_TODOS,
      "app.py": "from flask import Flask\nfrom views.todos import bp\napp = Flask(__name__)\n"
                "app.register_blueprint(bp, url_prefix='/v1/todos')\n"
                "app.register_blueprint(bp, url_prefix='/v2/todos', name='todos_v2')\n"},
     "views/todos.py", "DELETE · /v1/todos/<int:todo_id>, /v2/todos/<int:todo_id>"),
])
def test_a_router_mounted_twice_shows_both_addresses(tmp_path, files, rel, hint):
    assert write_real(tmp_path, files)["nodes"][rel]["f"][0][1] == hint


def test_a_router_on_a_router_mounted_twice_answers_at_both(tmp_path):
    nodes = write_real(tmp_path, {
        "routes/todos.py": TODOS_ROUTER,
        "main.py": "from fastapi import APIRouter, FastAPI\nfrom routes.todos import router as todos\napi = APIRouter()\n"
                   "api.include_router(todos)\napp = FastAPI()\napp.include_router(api, prefix='/v1')\n"
                   "app.include_router(api, prefix='/v2')\n",
        "web/v2.js": "export const load = () => fetch('/v2/todos');\n",
    })["nodes"]
    assert nodes["web/v2.js"]["h"] == ["routes/todos.py"]  # only /v1 was known before
    assert nodes["routes/todos.py"]["f"] == [["list_todos()", "GET · /v1/todos, /v2/todos"]]


@pytest.mark.parametrize("files, rel, hint", [
    # mounted once under /v1 and once as it is: it answers at both, and a call to either is linked
    ({"routes/todos.py": TODOS_ROUTER,
      "main.py": "from fastapi import FastAPI\nfrom routes.todos import router\napp = FastAPI()\n"
                 "app.include_router(router, prefix='/v1')\napp.include_router(router)\n"},
     "routes/todos.py", "GET · /v1/todos, /todos"),
    ({"main.py": TODOS_ROUTER + "\nfrom fastapi import FastAPI\napp = FastAPI()\n"
                 "app.include_router(router)\napp.include_router(router, prefix='/v1')\n"},
     "main.py", "GET · /todos, /v1/todos"),
    ({"views/__init__.py": "", "views/todos.py": FLASK_TODOS,  # url_prefix=None keeps the blueprint's own /todos
      "app.py": "from flask import Flask\nfrom views.todos import bp\napp = Flask(__name__)\n"
                "app.register_blueprint(bp, url_prefix='/v1')\napp.register_blueprint(bp, url_prefix=None, name='b2')\n"},
     "views/todos.py", "DELETE · /v1/<int:todo_id>, /todos/<int:todo_id>"),
])
def test_a_router_mounted_with_and_without_a_prefix_answers_at_both(tmp_path, files, rel, hint):
    files = {**files, "web/plain.js": "export const go = () => fetch('/todos/3', { method: 'DELETE' });\n"
                                      "export const all = () => fetch('/todos');\n"}
    nodes = write_real(tmp_path, files)["nodes"]
    assert nodes[rel]["f"][0][1] == hint
    assert nodes["web/plain.js"]["h"] == [rel]  # the address as written was missed before


@pytest.mark.parametrize("local", [True, False])
def test_routers_mounted_many_times_on_each_other_take_no_time(tmp_path, monkeypatch, local):
    # nine routers, each included six times on the one before: 6 ** 8 ways down the chain, which
    # once meant millions of steps (30 s for nine files that mount the next one eight times)
    import codevision.graph as graph_mod
    import codevision.lang.python_lang as py

    steps = []
    for mod, name in ((py, "_join_url"), (graph_mod, "_join_prefix")):
        real = getattr(mod, name)
        monkeypatch.setattr(mod, name, lambda a, b, _real=real: steps.append(1) or _real(a, b))
    route = "@r8.get('/x')\ndef x():\n    return 1\n"
    if local:
        lines = ["from fastapi import APIRouter, FastAPI", "app = FastAPI()"] + [f"r{i} = APIRouter()" for i in range(9)]
        lines += [route] + [f"r{i}.include_router(r{i + 1}, prefix='/p{k}')" for i in range(8) for k in range(6)]
        files, rel = {"main.py": "\n".join(lines + ["app.include_router(r0, prefix='/api')"]) + "\n"}, "main.py"
    else:
        files = {f"l{i}.py": f"from fastapi import APIRouter\nfrom l{i + 1} import router as child\nrouter = APIRouter()\n"
                             + "".join(f"router.include_router(child, prefix='/p{k}')\n" for k in range(6)) for i in range(8)}
        files["l8.py"] = "from fastapi import APIRouter\nrouter = APIRouter()\n" + route.replace("r8", "router")
        files["main.py"] = "from fastapi import FastAPI\nfrom l0 import router\napp = FastAPI()\napp.include_router(router, prefix='/api')\n"
        rel = "l8.py"
    hint = write_real(tmp_path, files)["nodes"][rel]["f"][0][1]
    assert len(steps) < 5_000
    assert hint.startswith("GET · /api/p0/p0/p0/p0/p0/p0/p0/p0/x, ") and hint.count(", ") == 7  # 8 addresses at most


def test_a_web_page_is_linked_to_the_files_it_loads(tmp_path):
    graph = write_real(tmp_path, {
        "web/index.html": '<link rel="stylesheet" href="https://cdn.example.com/x.css">\n'
                          '<link rel="stylesheet" href="./app.css">\n<script src="./app.js?v=2" defer></script>\n',
        "web/app.js": "import { draw } from './draw.js';\ndraw();\n",
        "web/draw.js": "export function draw() {}\n",
        "web/app.css": "body { color: red; }\n",
        "tool.py": "print(1)\n",
    })
    check_consistency(graph)
    nodes = graph["nodes"]
    assert nodes["web/index.html"]["u"] == ["web/app.css", "web/app.js"]
    assert nodes["web/app.js"]["b"] == ["web/index.html"] and nodes["web/app.css"]["b"] == ["web/index.html"]
    # browser code although no screen framework is used anywhere: the page runs it
    assert nodes["web/app.js"]["a"] == "ui" and nodes["web/draw.js"]["a"] == "ui"


def test_picture_and_design_folders_follow_what_is_in_them(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + bytes(32)
    graph = write_real(tmp_path, {
        "package.json": '{"dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"}}\n',
        "src/App.tsx": "import { Button } from './design/Button';\nimport { colors } from './design/tokens';\n"
                       "export default function App() { return <Button color={colors.primary} />; }\n",
        "src/design/Button.tsx": "export function Button(props) { return <button {...props} />; }\n",
        "src/design/tokens.ts": "export const colors = { primary: '#3366ff' };\n",
        "design/mockup.html": "<!doctype html><title>mockup</title>\n",
        "design/reference.png": png,
        "photos/beach.png": png, "photos/city.png": png, "photos/forest.png": png,
    })
    check_consistency(graph)
    nodes = graph["nodes"]
    # a folder of nothing but pictures reads as one, whatever its name
    assert (nodes["photos"]["s"], nodes["photos"]["d"]) == ("그림 모음", "그림 파일이 모여 있어요. 파일 3개가 들어 있어요.")
    assert (nodes["photos/beach.png"]["s"], nodes["photos/beach.png"]["d"]) == ("이미지 파일", "그림 파일이에요.")
    # src/design/ holds code the app imports; design/ holds a mockup and a picture that nothing uses
    assert nodes["src/design/Button.tsx"]["b"] == ["src/App.tsx"]
    assert (nodes["src/design"]["s"], nodes["src/design"]["d"]) == ("화면 코드 모음", "파일 2개가 들어 있어요.")
    assert nodes["design"]["s"] == "디자인 자료 모음"
    assert nodes["design/reference.png"]["d"] == "디자인 자료로 넣어 둔 그림이에요."


def test_a_fetch_helper_reaches_an_http_server_handler(tmp_path):
    graph = write_real(tmp_path, {
        "server.py": "from http.server import BaseHTTPRequestHandler, HTTPServer\n\n"
                     "ROUTES = {('GET', '/api/todos'): 'list_todos'}\n\n"
                     "class Handler(BaseHTTPRequestHandler):\n"
                     "    def do_POST(self):\n        if self.path == '/api/todos':\n            pass\n\n"
                     "    def do_DELETE(self):\n        if self.path.startswith('/api/todos/'):\n            pass\n",
        "web/index.html": '<script src="app.js"></script>\n',
        "web/app.js": "(() => {\n  async function api(url, method) {\n"
                      "    const r = await fetch(url, {method: method || 'GET'});\n    return r.json();\n  }\n"
                      "  api('/api/todos');\n})();\n",
        "web/add.js": "async function send(path, method) { return fetch(path, {method}); }\n"
                      "send('/api/todos', 'POST');\nsend('/api/todos/' + id, 'DELETE');\n",
    })
    nodes = graph["nodes"]
    assert nodes["web/app.js"]["h"] == ["server.py"] and nodes["web/add.js"]["h"] == ["server.py"]
    assert {"area": "sv", "label": "서버", "tech": "http.server"} in graph["stack"]
    assert graph["areas"]["sv"] == "서버 코드"


def test_http_paths_match_rules():
    assert http_paths_match("/todos", "/todos") and http_paths_match("/todos/*", "/todos/*")
    assert http_paths_match("/api/todos", "/todos") and http_paths_match("/api/todos/*", "/todos/*")  # mounted
    assert not http_paths_match("/todos/*", "/health")  # a wildcard alone does not reach a literal route
    assert not http_paths_match("/todos/*", "/app.js")
    assert not http_paths_match("/api/*", "/*") and not http_paths_match("/todos", "/")


def test_static_files_of_an_http_server_are_not_api_routes(tmp_path):
    nodes = write_real(tmp_path, {
        "server.py": "from http.server import BaseHTTPRequestHandler\n\n"
                     "STATIC = {'/': 'index.html', '/app.js': 'app.js', '/health': 'ok'}\n\n"
                     "class Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n"
                     "        if self.path == '/index.html':\n            pass\n",
        "client/api.js": "export const del = (id) => fetch(`/todos/${id}`, {method: 'DELETE'});\n",
    })["nodes"]
    assert nodes["client/api.js"]["h"] == [] and nodes["server.py"]["h"] == []


def test_example_and_test_folders_do_not_describe_the_project(tmp_path):
    graph = write_real(tmp_path, {
        "tool/cli.py": "import sqlite3\n\ndef main():\n    pass\n",
        "examples/web/src/App.jsx": "import React from 'react';\nexport default function App() { return <div/>; }\n",
        "examples/api/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "tests/test_cli.py": "from flask import Flask\n",
    })
    assert graph["stack"] == [{"area": "sv", "label": "DB", "tech": "SQLite"}]
    assert graph["areas"]["sv"] == "코드"
    assert graph["nodes"]["examples/web"]["s"] == "사용자가 보는 화면 (React)"  # the example itself still says so


def test_database_names_are_found_without_reading_the_code_twice(tmp_path, monkeypatch):
    # a Django project: every file uses Django, so looking for a database URL in a second pass
    # once read every file again
    import builtins

    import codevision.model as model_mod

    opened = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        opened.append(Path(file).relative_to(tmp_path).as_posix())
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(model_mod, "open", counting_open, raising=False)
    graph = write_real(tmp_path, {
        "mysite/settings.py": "import django\nDATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql'}}\n",
        "shop/models.py": "from django.db import models\n\n\nclass Item(models.Model):\n    name = models.TextField()\n",
        "tests/test_shop.py": "from django.test import TestCase\nOTHER = 'mysql://localhost/test'\n",
    })
    assert graph["stack"] == [{"area": "sv", "label": "서버", "tech": "Django"},
                              {"area": "sv", "label": "DB", "tech": "PostgreSQL"}]  # tests do not describe it
    assert sorted(opened) == ["mysite/settings.py", "shop/models.py", "tests/test_shop.py"]


@pytest.mark.parametrize("text, found", [
    ("engine = create_engine('PostgreSQL+psycopg2://me@localhost/shop')", {"postgres"}),
    ("SQLALCHEMY_DATABASE_URI = 'SQLITE:///todo.db'", {"sqlite"}),
    ("const db = new Sequelize({ dialect: 'mariadb' });", {"mysql"}),
    ("# 한글 주석이 있는 파일\nURL = 'mongodb://localhost/app'\n", {"mongodb"}),  # not ASCII: looked at the same way
    ("ENGINE = 'django.db.backends.sqlite3'", {"sqlite"}),
    ("from django.db import models\n\n\nclass Item(models.Model):\n    pass\n", set()),  # names no database
    ("", set()),
])
def test_database_names_in_code(text, found):
    from codevision import graph as graph_mod

    assert graph_mod._db_hints(text) == found


@pytest.mark.parametrize("text", [
    "URL = 'SQLİTE:///todo.db'",  # "İ".lower() is two characters: "i" and a dot above
    "dialect: 'marİadb'", "URL = 'sqlıte:///x'", "URL = 'ſqlite:///x'",
    "URL = 'Postgres://me@h/db'", "# 한글\nENGINE = 'django.db.backends.mysql'\n", "nothing here",
])
def test_the_database_word_check_never_changes_the_answer(text, monkeypatch):
    from codevision import graph as graph_mod

    found = graph_mod._db_hints(text)
    monkeypatch.setattr(graph_mod, "_DB_WORDS", ("",))  # "" is in every text: the regex always runs
    assert graph_mod._db_hints(text) == found


def test_code_texts_are_held_only_up_to_a_budget(tmp_path, monkeypatch):
    # step 1 holds the texts it read until it is over (letting go of each at once is slower), but
    # only up to TEXT_HOLD bytes: 20 code files of 300 KB must not all stay in memory
    import tracemalloc

    import codevision.graph as graph_mod

    write(tmp_path, {f"gen/table{i}.py": "X = '" + "a" * 300_000 + "'\n" for i in range(20)})
    peaks = {}
    for hold in (1_000_000, 100_000_000):
        monkeypatch.setattr(graph_mod, "TEXT_HOLD", hold)
        tracemalloc.start()
        graph = graph_mod.build(str(tmp_path))
        peaks[hold] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert graph["nodes"]["gen/table0.py"]["l"] == 1
    assert peaks[1_000_000] < 6_000_000 < peaks[100_000_000]


def test_focus_prefers_the_projects_own_code(tmp_path, fake_lang):
    write(tmp_path, {
        "app.py": "import lib.py\n", "lib.py": "",
        "_vendor/big.py": "import a.py\nimport b.py\nimport c.py\n", "_vendor/a.py": "", "_vendor/b.py": "",
        "_vendor/c.py": "",
        "tests/test_app.py": "import ../app.py\nimport ../lib.py\nimport helpers.py\n", "tests/helpers.py": "",
    })
    assert build(tmp_path)["focus"] == "app.py"  # not the vendored file or the test with more links
    write(tmp_path / "only_vendor", {"_vendor/x.py": "import y.py\n", "_vendor/y.py": "", "notes.txt": "x"})
    assert build(tmp_path / "only_vendor")["focus"] == "_vendor/x.py"  # still a linked file when that is all


def test_functions_list_the_public_face_first():
    facts = FileFacts(path="install.py", language="python", symbols=[
        Symbol("_warn", "function", "_warn(module)", doc="Warn once."),
        Symbol("_check", "function", "_check(module)"),
        Symbol("InstallCommand", "class", "class InstallCommand", doc="Install packages.", exported=True),
        Symbol("decide", "function", "decide(a)", exported=True),
        Symbol("_Helper", "class", "class _Helper"),
    ])
    assert [f[0] for f in describe.functions(facts)] == [
        "class InstallCommand", "decide(a)", "_warn(module)", "class _Helper", "_check(module)"]
    assert [f[0] for f in describe.functions(facts, limit=2)] == ["class InstallCommand", "decide(a)"]


# ---------------------------------------------------------------- integration: the demo project

LANG_DIR = Path(lang.__file__).parent

EXPECTED_USES = {
    "frontend/index.html": {"frontend/src/main.jsx"},  # <script type="module" src="/src/main.jsx">
    "frontend/src/main.jsx": {"frontend/src/App.jsx"},
    "frontend/src/App.jsx": {"frontend/src/components/TodoList.jsx"},
    "frontend/src/components/TodoList.jsx": {"frontend/src/api.js", "frontend/src/components/TodoItem.jsx"},
    "frontend/src/components/TodoItem.jsx": {"frontend/src/api.js"},
    "frontend/src/api.js": {"backend/routes/todos.py"},
    "backend/main.py": {"backend/database.py", "backend/routes/todos.py"},
    "backend/routes/todos.py": {"backend/database.py", "backend/models.py"},
    "backend/models.py": {"backend/database.py"},
}


def test_demo_project_matches_architecture_section_8(demo_root):
    missing = [n for n in ("python_lang.py", "js_lang.py") if not (LANG_DIR / n).exists()]
    if missing:
        pytest.skip(f"analyzers not written yet: {missing}")
    graph = build(str(demo_root))
    assert lang.load_errors() == {}
    check_consistency(graph)
    nodes = graph["nodes"]

    folders = sorted(k for k, v in nodes.items() if v["t"] == "d" and k != ".")
    assert folders == ["backend", "backend/routes", "frontend", "frontend/src", "frontend/src/components"]
    assert graph["stats"]["folders"] == 5 and graph["stats"]["files"] == 15
    assert nodes["."]["k"] == ["backend", "frontend", "helpers_old.py", "README.md"]

    actual_uses = {k: set(v["u"]) for k, v in nodes.items() if v["t"] == "f" and v["u"]}
    assert actual_uses == EXPECTED_USES
    assert nodes["frontend/src/api.js"]["h"] == ["backend/routes/todos.py"]
    assert nodes["backend/routes/todos.py"]["h"] == ["frontend/src/api.js"]

    assert {k for k, v in nodes.items() if v.get("e")} == {"frontend/src/main.jsx", "backend/main.py"}
    assert {k for k, v in nodes.items() if v.get("o")} == {"helpers_old.py"}
    assert graph["stack"] == [
        {"area": "ui", "label": "화면", "tech": "React"},
        {"area": "sv", "label": "서버", "tech": "FastAPI"},
        {"area": "sv", "label": "DB", "tech": "SQLite"},
    ]
    assert graph["focus"] == "backend/routes/todos.py"
    assert graph["areas"]["sv"] == "서버 코드"
    assert graph["warnings"] == []
    assert nodes["frontend"]["a"] == "ui" and nodes["backend"]["a"] == "sv"
    assert nodes["frontend/src/api.js"]["a"] == "ui" and nodes["frontend/vite.config.js"]["a"] == "ot"
