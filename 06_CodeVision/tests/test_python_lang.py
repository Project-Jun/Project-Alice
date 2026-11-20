from pathlib import Path

import pytest

from codevision.lang import python_lang as py
from codevision.model import ImportRef, ProjectIndex


def index_for(root: Path) -> ProjectIndex:
    files, dirs = [], []
    for path in root.rglob("*"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        (dirs if path.is_dir() else files).append(rel)
    return ProjectIndex(root, files, dirs)


def fake_index(*files: str) -> ProjectIndex:
    """Index of files that do not need to exist on disk (resolve() never reads them)."""
    dirs = set()
    for rel in files:
        parts = rel.split("/")[:-1]
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return ProjectIndex("unused-root", files, dirs)


def imports_of(facts):
    return [(i.spec, i.names, i.kind, i.level, i.line) for i in facts.imports]


# ---------------------------------------------------------------- demo project

@pytest.fixture
def demo(demo_root):
    index = index_for(demo_root)
    facts = {rel: py.analyze(rel, index.read_text(rel)) for rel in index.files if rel.endswith(".py")}
    return index, facts


def uses(index, facts, rel):
    return sorted({path for imp in facts[rel].imports for path in py.resolve(imp, rel, index)})


def test_demo_files_parse(demo):
    _, facts = demo
    assert set(facts) == {
        "backend/database.py", "backend/main.py", "backend/models.py",
        "backend/routes/todos.py", "helpers_old.py",
    }
    for rel, f in facts.items():
        assert f.path == rel
        assert f.language == "python"
        assert not f.parse_error
        assert f.lines > 0
    assert facts["backend/main.py"].lines == 11


def test_demo_main_resolves_imports(demo):
    index, facts = demo
    by_spec = {imp.spec: imp for imp in facts["backend/main.py"].imports}
    assert by_spec["routes"].names == ["todos"]
    assert py.resolve(by_spec["routes"], "backend/main.py", index) == ["backend/routes/todos.py"]
    assert by_spec["database"].names == ["Base", "engine"]
    assert py.resolve(by_spec["database"], "backend/main.py", index) == ["backend/database.py"]
    assert py.resolve(by_spec["fastapi"], "backend/main.py", index) == []
    assert py.resolve(by_spec["fastapi.middleware.cors"], "backend/main.py", index) == []


def test_demo_import_edges(demo):
    index, facts = demo
    assert uses(index, facts, "backend/main.py") == ["backend/database.py", "backend/routes/todos.py"]
    assert uses(index, facts, "backend/routes/todos.py") == ["backend/database.py", "backend/models.py"]
    assert uses(index, facts, "backend/models.py") == ["backend/database.py"]
    assert uses(index, facts, "backend/database.py") == []
    assert uses(index, facts, "helpers_old.py") == []


def test_demo_routes_and_symbols(demo):
    _, facts = demo
    todos = facts["backend/routes/todos.py"]
    assert [(r.method, r.path, r.handler) for r in todos.http_routes] == [
        ("GET", "/todos", "get_todos"),
        ("POST", "/todos", "create_todo"),
        ("DELETE", "/todos/*", "delete_todo"),
    ]
    assert [(s.kind, s.signature, s.hint) for s in todos.symbols] == [
        ("class", "class TodoIn", "데이터 모양"),
        ("function", "get_todos(db)", "GET · /todos"),
        ("function", "create_todo(todo, db)", "POST · /todos"),
        ("function", "delete_todo(todo_id, db)", "DELETE · /todos/{todo_id}"),
    ]
    assert todos.http_calls == []
    assert [(s.signature, s.hint) for s in facts["backend/models.py"].symbols] == [("class Todo", "데이터 모양")]
    helpers = facts["helpers_old.py"]
    assert [(s.signature, s.doc) for s in helpers.symbols] == [
        ("format_date(value)", "날짜를 '2026-11-19' 같은 글자로 바꿔요."),
        ("old_save_todo(todos, title)", "예전 저장 방식: 목록에 바로 추가해요."),
    ]
    assert helpers.module_doc == "예전에 쓰던 도우미 함수 모음 (지금은 쓰지 않음)."


def test_demo_frameworks(demo):
    _, facts = demo
    assert facts["backend/main.py"].frameworks == {"fastapi"}
    assert facts["backend/database.py"].frameworks == {"sqlalchemy", "sqlite"}
    assert facts["backend/routes/todos.py"].frameworks == {"fastapi", "sqlalchemy"}
    assert facts["helpers_old.py"].frameworks == set()


def test_demo_entries(demo):
    index, facts = demo
    main = facts["backend/main.py"]
    assert main.is_entry and main.entry_reason == "서버를 만드는 파일"
    for rel in ("helpers_old.py", "backend/database.py", "backend/models.py", "backend/routes/todos.py"):
        assert not facts[rel].is_entry, rel
    entries = py.project_entries(index)
    assert entries.get("backend/main.py") == "서버 실행 명령에 들어 있는 파일"  # README: cd backend, uvicorn main:app
    assert "helpers_old.py" not in entries


# ---------------------------------------------------------------- imports

IMPORT_FORMS = '''\
import os, a.b as c
from x.y import a, b
from . import sibling
from ..models import Todo
from .. import *


def load():
    import json
    try:
        from yaml import safe_load
    except ImportError:
        safe_load = None
'''


def test_import_forms_including_nested():
    facts = py.analyze("pkg/sub/mod.py", IMPORT_FORMS)
    assert imports_of(facts) == [
        ("os", [], "import", 0, 1),
        ("a.b", [], "import", 0, 1),
        ("x.y", ["a", "b"], "from", 0, 2),
        ("", ["sibling"], "from", 1, 3),
        ("models", ["Todo"], "from", 2, 4),
        ("", ["*"], "from", 2, 5),
        ("json", [], "import", 0, 9),
        ("yaml", ["safe_load"], "from", 0, 11),
    ]


RELATIVE = '''\
from . import deps
from .. import models
from ..models import Todo
from ..core.config import settings
from . import missing
from .deps import get_db
from .... import far
import app.core.config as cfg
'''


def test_relative_imports_resolve():
    index = fake_index(
        "app/__init__.py", "app/main.py", "app/models.py",
        "app/api/__init__.py", "app/api/routes.py", "app/api/deps.py",
        "app/core/config.py",  # app/core has no __init__.py (namespace package)
    )
    facts = py.analyze("app/api/routes.py", RELATIVE)
    assert [py.resolve(imp, "app/api/routes.py", index) for imp in facts.imports] == [
        ["app/api/deps.py"],
        ["app/models.py"],
        ["app/models.py"],
        ["app/core/config.py"],
        ["app/api/__init__.py"],  # not a submodule -> the package itself
        ["app/api/deps.py"],
        [],  # climbs above the project root
        ["app/core/config.py"],
    ]


def test_absolute_imports_search_order():
    index = fake_index(
        "helpers.py", "tools/helpers.py", "tools/scripts/run.py",
        "pkg/__init__.py", "pkg/a.py", "pkg/sub/__init__.py",
        "ns/b.py", "src/lib/core.py",
    )
    importer = "tools/scripts/run.py"

    def res(spec, names=(), kind="from"):
        return py.resolve(ImportRef(spec, list(names), kind), importer, index)

    assert res("helpers", kind="import") == ["tools/helpers.py"]  # closest ancestor wins over the root
    assert res("pkg", ["a", "thing"]) == ["pkg/a.py", "pkg/__init__.py"]
    assert res("pkg", ["sub"]) == ["pkg/sub/__init__.py"]
    assert res("pkg", ["thing"]) == ["pkg/__init__.py"]
    assert res("pkg", ["*"]) == ["pkg/__init__.py"]
    assert res("pkg", kind="import") == ["pkg/__init__.py"]
    assert res("pkg.a", kind="import") == ["pkg/a.py"]
    assert res("pkg.a", kind="dynamic") == ["pkg/a.py"]
    assert res("ns", ["b"]) == ["ns/b.py"]
    assert res("ns", ["nothing"]) == []  # bare namespace folder
    assert res("ns", kind="import") == []
    assert res("lib", ["core"]) == ["src/lib/core.py"]  # src layout
    assert res("os", kind="import") == []
    assert res("fastapi", ["FastAPI"]) == []
    assert py.resolve(ImportRef("helpers", [], "import"), "helpers.py", index) == []  # never itself


def test_real_module_beats_namespace_folder():
    index = fake_index("app/main.py", "app/models/data.json", "models.py")
    imp = ImportRef("models", ["Todo"], "from")
    assert py.resolve(imp, "app/main.py", index) == ["models.py"]


DYNAMIC = '''\
import importlib
from importlib import import_module

plugin = importlib.import_module("plugins.alpha")
helper = import_module(".beta", __package__)
legacy = __import__("json")
name = "x"
skipped = importlib.import_module(name)
'''


def test_dynamic_imports():
    facts = py.analyze("loader.py", DYNAMIC)
    dynamic = [(i.spec, i.level, i.line) for i in facts.imports if i.kind == "dynamic"]
    assert dynamic == [("plugins.alpha", 0, 4), ("beta", 1, 5), ("json", 0, 6)]
    index = fake_index("loader.py", "plugins/alpha.py", "beta.py")
    resolved = [py.resolve(i, "loader.py", index) for i in facts.imports if i.kind == "dynamic"]
    assert resolved == [["plugins/alpha.py"], ["beta.py"], []]


# ---------------------------------------------------------------- HTTP

CLIENT = '''\
import requests
import httpx as hx
from requests import post

BASE = "http://localhost:8000"
TODOS = BASE + "/todos"


def list_todos():
    return requests.get(f"{BASE}/todos")


def get_todo(todo_id):
    return requests.get(f"{BASE}/todos/{todo_id}", timeout=5)


def add(title):
    return post(TODOS, json={"title": title})


def remove(todo_id):
    return hx.delete("http://localhost:8000/todos/" + str(todo_id))


def rename(todo_id):
    with hx.Client(base_url=BASE) as client:
        client.patch(f"/todos/{todo_id}")


def replace(todo_id):
    session = requests.Session()
    session.request("PUT", "/todos/%s" % todo_id)


def fetch(url):
    return requests.get(url)


def lookup(cache):
    return cache.get("/todos")
'''


def test_http_calls_with_fstrings_and_clients():
    facts = py.analyze("client.py", CLIENT)
    assert [(c.method, c.path) for c in facts.http_calls] == [
        ("GET", "/todos"),
        ("GET", "/todos/*"),
        ("POST", "/todos"),
        ("DELETE", "/todos/*"),
        ("PATCH", "/todos/*"),
        ("PUT", "/todos/*"),
    ]
    assert facts.http_calls[0].line == 10
    assert facts.http_routes == []


ROUTER = '''\
from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/")
async def list_users():
    """사용자 목록을 돌려줘요."""


@router.api_route("/{user_id}", methods=["PUT", "PATCH"])
async def update_user(user_id: int, *, body: dict = None):
    ...


@router.post("")
def create_user(self, *args, **kwargs):
    ...
'''


def test_fastapi_router_prefix():
    facts = py.analyze("routes/users.py", ROUTER)
    assert [(r.method, r.path, r.handler) for r in facts.http_routes] == [
        ("GET", "/users", "list_users"),
        ("PUT", "/users/*", "update_user"),
        ("PATCH", "/users/*", "update_user"),
        ("POST", "/users", "create_user"),
    ]
    assert [(s.signature, s.doc, s.hint) for s in facts.symbols] == [
        ("list_users()", "사용자 목록을 돌려줘요.", "GET · /users"),
        ("update_user(user_id, body)", "", "PUT, PATCH · /users/{user_id}"),
        ("create_user(*args, **kwargs)", "", "POST · /users"),
    ]
    assert not facts.is_entry


FLASK_APP = '''\
from flask import Blueprint, Flask

app = Flask(__name__)
bp = Blueprint("todos", __name__, url_prefix="/api/todos")


@app.route("/")
def index():
    return "hi"


@app.route("/items", methods=["GET", "POST"])
def items():
    return "items"


@bp.route("/<int:todo_id>", methods=["DELETE"])
def remove(todo_id):
    return ""


@bp.get("/")
def list_todos():
    return []


def create_app():
    inner = Flask(__name__)

    @inner.route("/health")
    def health():
        return "ok"

    return inner
'''


def test_flask_routes_and_blueprint_prefix():
    facts = py.analyze("app.py", FLASK_APP)
    assert [(r.method, r.path, r.handler) for r in facts.http_routes] == [
        ("GET", "/", "index"),
        ("GET", "/items", "items"),
        ("POST", "/items", "items"),
        ("DELETE", "/api/todos/*", "remove"),
        ("GET", "/api/todos", "list_todos"),
        ("GET", "/health", "health"),
    ]
    hints = {s.name: s.hint for s in facts.symbols}
    assert hints == {
        "index": "GET · /",
        "items": "GET, POST · /items",
        "remove": "DELETE · /api/todos/<int:todo_id>",
        "list_todos": "GET · /api/todos",
        "create_app": "",
    }
    assert facts.frameworks == {"flask"}
    assert facts.is_entry and facts.entry_reason == "서버를 만드는 파일"


# ---------------------------------------------------------------- symbols, docs, entries

SHAPES = '''\
"""모델 모음이에요.

자세한 설명은 여기 있어요.
"""
from django.db import models
from pydantic import BaseModel
import sqlmodel

__all__ = ["Item", "helper"]


class Item(models.Model):
    """할 일 한 개예요."""


class Payload(BaseModel):
    pass


class Hero(sqlmodel.SQLModel, table=True):
    pass


class Plain:
    pass


def helper(self, a: int = 1, /, b=2, *rest, key: str, **extra) -> None:
    pass


async def _private(cls):
    pass


if __name__ == "__main__":
    helper(1)
'''


def test_symbols_signatures_and_data_shapes():
    facts = py.analyze("models.py", SHAPES)
    assert [(s.name, s.kind, s.signature, s.doc, s.hint, s.exported) for s in facts.symbols] == [
        ("Item", "class", "class Item", "할 일 한 개예요.", "데이터 모양", True),
        ("Payload", "class", "class Payload", "", "데이터 모양", False),
        ("Hero", "class", "class Hero", "", "데이터 모양", False),
        ("Plain", "class", "class Plain", "", "", False),
        ("helper", "function", "helper(a, b, *rest, key, **extra)", "", "", True),
        ("_private", "function", "_private()", "", "", False),
    ]
    assert facts.module_doc == "모델 모음이에요."
    assert facts.is_entry and facts.entry_reason == "직접 실행하는 파일"
    assert facts.frameworks == {"django", "sqlalchemy"}


def test_module_doc_from_leading_comment():
    source = (
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        "# ==========\n"
        "# 할 일을 정리하는 스크립트예요.\n"
        "import sys\n"
    )
    facts = py.analyze("tool.py", source)
    assert facts.module_doc == "할 일을 정리하는 스크립트예요."
    assert not facts.is_entry
    assert py.analyze("a.py", "import sys\n# 설명이 아니에요\n").module_doc == ""
    crlf = py.analyze("b.py", "\ufeff# 첫 줄이에요\r\nimport os\r\n")
    assert (crlf.module_doc, crlf.lines, [i.spec for i in crlf.imports]) == ("첫 줄이에요", 2, ["os"])


def test_server_app_inside_function_is_not_an_entry():
    facts = py.analyze("factory.py", "from flask import Flask\n\ndef make():\n    app = Flask(__name__)\n    return app\n")
    assert not facts.is_entry


def test_package_main_file_is_an_entry():
    facts = py.analyze("src/tool/__main__.py", "from tool.cli import main\n\nmain()\n")
    assert facts.is_entry and facts.entry_reason == "직접 실행하는 파일"


def test_unknown_router_prefix_still_gives_a_readable_hint():
    source = (
        "from fastapi import APIRouter\n"
        "from config import settings\n"
        "router = APIRouter(prefix=settings.API_PREFIX)\n"
        "@router.get('/todos/{todo_id}')\n"
        "def read(todo_id):\n"
        "    ...\n"
    )
    facts = py.analyze("api.py", source)
    assert [(r.method, r.path) for r in facts.http_routes] == [("GET", "/todos/*")]
    assert facts.symbols[0].hint == "GET · /todos/{todo_id}"


# ---------------------------------------------------------------- syntax errors

BROKEN = '''\
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 고장 난 파일이에요
import os, json as j
from flask import (
    Flask,  # web
    jsonify,
)
from .utils import helper
plugin = importlib.import_module("plugins.alpha")
engine = create_engine("sqlite:///app.db")
app = Flask(__name__)

def broken(:
    pass
'''


def test_syntax_error_falls_back_to_regex():
    facts = py.analyze("broken.py", BROKEN)
    assert facts.parse_error
    assert facts.lines == len(BROKEN.splitlines()) == 15
    assert imports_of(facts) == [
        ("os", [], "import", 0, 4),
        ("json", [], "import", 0, 4),
        ("flask", ["Flask", "jsonify"], "from", 0, 5),
        ("utils", ["helper"], "from", 1, 9),
        ("plugins.alpha", [], "dynamic", 0, 10),
    ]
    assert facts.frameworks == {"flask", "sqlite"}
    assert facts.is_entry and facts.entry_reason == "서버를 만드는 파일"
    assert facts.module_doc == "고장 난 파일이에요"


def test_long_elif_chains_and_long_sums_are_not_syntax_errors():
    # valid Python whose tree is deep: each elif is an `if` inside the one before, and a + b + c
    # nests the same way. ast.parse takes it, so the walk must not give up on it either.
    in_function = "".join(f"    elif cmd == 'c{i}':\n        return {i}\n" for i in range(1, 800))
    at_top = "".join(f"elif MODE == 'm{i}':\n    helpers.setup({i})\n" for i in range(1, 800))
    terms = " +\n    ".join(f"'part{i} '" for i in range(1500))
    source = ('"""Command dispatcher."""\nimport helpers\nimport requests\n\n\n'
              f"def run(cmd):\n    if cmd == 'c0':\n        return 0\n{in_function}\n\n"
              f"MODE = 'x'\nif MODE == 'm0':\n    pass\n{at_top}"
              f"QUERY = (\n    {terms}\n)\nimport after_the_chains\n\n"
              "if __name__ == '__main__':\n    requests.get('http://localhost:8000/api/run')\n")
    facts = py.analyze("dispatch.py", source)
    assert not facts.parse_error
    assert [i.spec for i in facts.imports] == ["helpers", "requests", "after_the_chains"]
    assert [s.name for s in facts.symbols] == ["run"]
    assert facts.module_doc == "Command dispatcher."
    assert facts.is_entry and facts.entry_reason == "직접 실행하는 파일"
    assert [(c.method, c.path) for c in facts.http_calls] == [("GET", "/api/run")]


def test_a_docstring_of_one_word_lines_is_not_a_syntax_error():
    words = "\n".join(f"Word{i}" for i in range(1200))
    facts = py.analyze("words.py", f'"""\n{words}\n"""\nimport helpers\n\n\ndef run():\n    return 1\n')
    assert not facts.parse_error
    assert [s.name for s in facts.symbols] == ["run"] and [i.spec for i in facts.imports] == ["helpers"]
    assert facts.module_doc == ""  # one-word lines only: no sentence to show
    comments = "\n".join(f"# Word{i}" for i in range(1200))
    facts = py.analyze("notes.py", f"{comments}\n# Runs the nightly job.\nimport helpers\n")
    assert not facts.parse_error and facts.module_doc == "Runs the nightly job."


def test_a_one_word_line_is_judged_by_the_lines_as_written():
    # "help.py:" + "downloads" is one word once the "help.py:" start is taken out, and an image
    # link without text leaves "Cleaner" alone, but each sentence goes on: shown whole, as before
    facts = py.analyze("tools/help.py", '"""help.py:\ndownloads\nfiles from the server.\n"""\nimport os\n')
    assert facts.module_doc == "Downloads files from the server."
    facts = py.analyze("cleaner.py", '"""Cleaner\n[](https://example.com/badge.svg)\nfor messy data files.\n"""\n')
    assert facts.module_doc == "Cleaner for messy data files."


def test_lines_that_each_say_one_word_are_read_once(monkeypatch):
    # "Word7 [](badge.svg)" is one word once the image link goes, so the next line is tried. Each try
    # once joined the whole rest of the paragraph: 3,000 such lines read 4.5 million lines (16,000 took 6 s).
    real = py.join_wrapped
    read = []

    def counting(lines):
        def each():
            for line in lines:
                read.append(line)
                yield line
        return real(each())

    monkeypatch.setattr(py, "join_wrapped", counting)
    badges = "\n".join(f"Word{i} [](https://example.com/badge{i}.svg)" for i in range(3000))
    facts = py.analyze("badges.py", f'"""\n{badges}\nBuilds the badges.\n"""\n')
    assert facts.module_doc == "Builds the badges." and not facts.parse_error
    assert len(read) < 4 * 3000


def test_markup_is_looked_for_in_the_start_of_a_long_sentence_only(monkeypatch):
    # the role and link patterns take time that grows with the square of a long run they cannot
    # match: a one-line docstring of 160 KB (":a:a:a...") once took 40 s, "[[[[..." 47 s
    real, seen = py._RST_ROLE, []

    class Counting:
        def sub(self, repl, text):
            seen.append(len(text))
            return real.sub(repl, text)

    monkeypatch.setattr(py, "_RST_ROLE", Counting())
    for body in (":a" * 80_000, "[" * 160_000):
        facts = py.analyze("m.py", f'def f():\n    """Starts {body} end."""\n')
        assert facts.symbols[0].doc.startswith("Starts ") and len(facts.symbols[0].doc) <= 300
    assert seen and max(seen) <= 4_000
    long = "Loads the settings file " + "and the defaults " * 10 + "for :class:`~app.Config` objects."
    assert py.analyze("m.py", f'"""{long}"""\n').module_doc == long.replace(":class:`~app.Config`", "Config")


def test_a_recursion_error_after_parsing_keeps_what_was_found(monkeypatch):
    import ast

    real = py._STATEMENTS[ast.Assign]

    def deep(visitor, node):
        if getattr(node.targets[0], "id", "") == "DEEP":
            raise RecursionError("maximum recursion depth exceeded")
        return real(visitor, node)

    monkeypatch.setitem(py._STATEMENTS, ast.Assign, deep)
    source = '"""Small build tools."""\nimport first\nDEEP = 1\nimport second\n\ndef run():\n    pass\n'
    facts = py.analyze("tools.py", source)
    assert not facts.parse_error  # Python itself reads this file: it is no syntax error
    assert [i.spec for i in facts.imports] == ["first", "second"]  # the walk went on after the deep part
    assert [s.name for s in facts.symbols] == ["run"] and facts.module_doc == "Small build tools."

    monkeypatch.setattr(py, "_symbols", lambda *args: (_ for _ in ()).throw(RecursionError()))
    facts = py.analyze("tools.py", source)
    assert not facts.parse_error and [i.spec for i in facts.imports] == ["first", "second"]


def test_never_raises_on_odd_input():
    for source in ("", "\x00\x00", "def (:\n", "x = (" * 500, '"""unterminated\n'):
        facts = py.analyze("odd.py", source)
        assert facts.language == "python"
    assert py.analyze("empty.py", "").lines == 0
    doc = py.analyze("doc.py", '"""고친 중이에요\nimport os\n')
    assert doc.parse_error and doc.module_doc == "고친 중이에요"


# ---------------------------------------------------------------- project entries

def test_project_entries(tmp_path):
    files = {
        "pyproject.toml": '[project]\nname = "todo"\n\n[project.scripts]\n'
                          'todo = "todo_cli.main:run"\nbroken = "missing.mod:main"\n',
        "src/todo_cli/__init__.py": "",
        "src/todo_cli/main.py": "def run():\n    pass\n",
        "web/manage.py": "import django\n",
        "Procfile": "web: gunicorn -w 2 --bind 0.0.0.0:8000 server.app:app\n",
        "server/__init__.py": "",
        "server/app.py": "app = object()\n",
        "api/Dockerfile": 'FROM python:3.12\nCMD ["uvicorn", "main:app", "--host", "0.0.0.0"]\n',
        "api/main.py": "app = object()\n",
        "README.md": "# 실행\n\n```\ncd dash\nstreamlit run board.py\n```\n",
        "dash/board.py": "import streamlit as st\n",
        "helpers.py": "x = 1\n",
        "old/pyproject.toml": "this is [not toml",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert py.project_entries(index_for(tmp_path)) == {
        "src/todo_cli/main.py": "명령어로 실행하는 파일",
        "web/manage.py": "Django 관리 명령",
        "server/app.py": "서버 실행 명령에 들어 있는 파일",
        "api/main.py": "서버 실행 명령에 들어 있는 파일",
        "dash/board.py": "서버 실행 명령에 들어 있는 파일",
    }


def test_registry_picks_python_analyzer():
    from codevision.lang import analyzer_for

    assert analyzer_for("backend/main.py") is py
    assert py.EXTENSIONS == (".py",)


# ---------------------------------------------------------------- real-code fixes

def package_index(root_name: str, *files: str) -> ProjectIndex:
    """Like fake_index, but the analyzed folder has a name (resolve() looks at it)."""
    index = fake_index(*files)
    return ProjectIndex(Path("C:/somewhere") / root_name, index.files, index.dirs)


def test_root_folder_name_works_as_the_top_package():
    # Analyzing the pip folder itself: its code imports "pip...." although no folder is called pip.
    index = package_index(
        "pip", "__init__.py", "__main__.py", "_internal/__init__.py", "_internal/cli/__init__.py",
        "_internal/cli/main.py", "_internal/commands/__init__.py", "_internal/commands/install.py",
    )
    importer = "_internal/commands/install.py"

    def res(spec, names=(), kind="from", who=importer):
        return py.resolve(ImportRef(spec, list(names), kind), who, index)

    assert res("pip._internal.cli", ["main"]) == ["_internal/cli/main.py"]
    assert res("pip._internal.cli.main", ["main"]) == ["_internal/cli/main.py"]
    assert res("pip", kind="import") == ["__init__.py"]
    assert res("pip", ["__version__"]) == ["__init__.py"]
    assert res("pip._internal.missing", kind="import") == []
    assert res("pip._internal.cli.main", ["cli"], who="__main__.py") == ["_internal/cli/main.py"]
    # a name that cannot be a package ("my-app") or a different first name is not affected
    other = package_index("my-app", "__init__.py", "core.py")
    assert py.resolve(ImportRef("my-app.core", [], "import"), "x.py", other) == []
    assert res("requests.models", ["Response"]) == []


def test_project_folder_beats_the_root_package_name():
    # A clone of "app" holding the package app/ and tests/: the normal search still wins.
    index = package_index("app", "app/__init__.py", "app/models.py", "tests/test_models.py", "models.py")
    imp = ImportRef("app.models", ["Todo"], "from")
    assert py.resolve(imp, "tests/test_models.py", index) == ["app/models.py"]


def test_import_main_means_the_running_script():
    index = fake_index("__main__.py", "tool/__main__.py", "tool/run.py")
    assert py.resolve(ImportRef("__main__", [], "import"), "tool/run.py", index) == []


def test_type_checking_imports_are_not_frameworks():
    source = (
        "from typing import TYPE_CHECKING\n"
        "import typing\n"
        "if TYPE_CHECKING:\n"
        "    from redis import Redis\n"
        "if typing.TYPE_CHECKING:\n"
        "    import sqlalchemy\n"
        "else:\n"
        "    import flask\n"
    )
    facts = py.analyze("cache.py", source)
    assert [i.spec for i in facts.imports] == ["typing", "typing", "redis", "sqlalchemy", "flask"]
    assert facts.frameworks == {"flask"}  # type-only imports never run


def test_module_names_in_a_registry_with_import_module():
    # pip/_internal/commands/__init__.py: module paths as strings, loaded with import_module(name)
    source = (
        "import importlib\n"
        "commands_dict = {\n"
        '    "install": ("pip._internal.commands.install", "InstallCommand"),\n'
        '    "list": ("pip._internal.commands.list", "ListCommand"),\n'
        "}\n"
        "def create(name):\n"
        "    module_path, class_name = commands_dict[name]\n"
        "    return getattr(importlib.import_module(module_path), class_name)\n"
    )
    facts = py.analyze("_internal/commands/__init__.py", source)
    dynamic = [(i.spec, i.line) for i in facts.imports if i.kind == "dynamic"]
    assert dynamic == [("pip._internal.commands.install", 3), ("pip._internal.commands.list", 4)]
    index = package_index("pip", "__init__.py", "_internal/__init__.py", "_internal/commands/__init__.py",
                          "_internal/commands/install.py", "_internal/commands/list.py")
    targets = {t for i in facts.imports for t in py.resolve(i, "_internal/commands/__init__.py", index)}
    assert targets == {"_internal/commands/install.py", "_internal/commands/list.py"}
    # without a computed import_module(), dotted strings inside functions stay plain strings
    plain = py.analyze("a.py", 'import importlib\ndef f():\n    log("pkg.mod")\nimportlib.import_module("other")\n')
    assert [i.spec for i in plain.imports if i.kind == "dynamic"] == ["other"]


def test_module_paths_in_module_level_tables():
    # pygments/lexers/_mapping.py: a table of module paths that another file loads by name
    source = (
        "LEXERS = {\n"
        "    'PythonLexer': ('pygments.lexers.python', 'Python', ('python', 'py'), ('*.py',), ()),\n"
        "    'JsonLexer': ('pygments.lexers.data', 'JSON', ('json',), ('*.json',), ()),\n"
        "}\n"
        "WORDS = ['alpha', 'beta.gamma.txt-like', 'not a module']\n"
        "ROOT_URLCONF = 'mysite.urls'\n"
        "def f():\n    local = ['inside.function']\n"
    )
    facts = py.analyze("pygments/lexers/_mapping.py", source)
    assert [(i.spec, i.line) for i in facts.imports if i.kind == "dynamic"] == [
        ("pygments.lexers.python", 2), ("pygments.lexers.data", 3), ("mysite.urls", 6),
    ]
    index = fake_index("pygments/__init__.py", "pygments/lexers/__init__.py", "pygments/lexers/_mapping.py",
                       "pygments/lexers/python.py", "pygments/lexers/data.py", "mysite/urls.py")
    targets = {t for i in facts.imports for t in py.resolve(i, "pygments/lexers/_mapping.py", index)}
    assert targets == {"pygments/lexers/python.py", "pygments/lexers/data.py", "mysite/urls.py"}


def test_django_settings_and_app_config_strings():
    manage = (
        "import os, sys\n\ndef main():\n"
        '    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")\n'
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n"
    )
    facts = py.analyze("manage.py", manage)
    assert ("mysite.settings", "dynamic") in {(i.spec, i.kind) for i in facts.imports}
    index = fake_index("manage.py", "mysite/__init__.py", "mysite/settings.py", "polls/__init__.py", "polls/apps.py")
    settings = py.analyze("mysite/settings.py", 'INSTALLED_APPS = ["django.contrib.admin", "polls.apps.PollsConfig"]\n')
    targets = {t for i in settings.imports for t in py.resolve(i, "mysite/settings.py", index)}
    assert targets == {"polls/apps.py"}  # the class name is dropped, Django itself is outside the project
    assert py.resolve(ImportRef("polls.apps.PollsConfig", [], "from"), "x.py", index) == []  # only for names


def test_bare_url_scheme_is_not_a_database():
    facts = py.analyze("lexer.py", 'SCHEMES = ["irc://", "redis://", "mailto:"]\n')
    assert facts.frameworks == set()
    assert py.analyze("c.py", 'URL = "redis://localhost:6379/0"\n').frameworks == {"redis"}
    assert py.analyze("m.py", 'engine = create_engine("sqlite://")\n').frameworks == {"sqlite"}


@pytest.mark.parametrize("source, expected", [
    # license headers are skipped, the real description follows
    ("# SPDX-FileCopyrightText: 2015 Eric Larson\n#\n# SPDX-License-Identifier: Apache-2.0\n"
     '"""Caching for HTTP requests."""\n', "Caching for HTTP requests."),
    ("# This file is dual licensed under the terms of the Apache License, Version\n"
     "# 2.0, and the BSD License. See the LICENSE file in the root of this repository\n"
     "# for complete details.\nimport os\n", ""),
    ("#   Copyright 2000-2008 Michael Hudson-Doyle\n#\n#                        All Rights Reserved\n#\n"
     "# THE AUTHOR MICHAEL HUDSON DISCLAIMS ALL WARRANTIES WITH REGARD TO\n# THIS SOFTWARE, INCLUDING ALL\n"
     "#\n# 화면 입력을 읽는 코드예요.\nimport os\n", "화면 입력을 읽는 코드예요."),
    # titles that only repeat the module name (pygments, requests, curses style)
    ('"""\n    pygments.lexer\n    ~~~~~~~~~~~~~~\n\n    Base lexer classes.\n\n'
     '    :copyright: Copyright 2006 by the Pygments team.\n"""\n', "Base lexer classes."),
    ('"""curses\n\nThe main package for curses support for Python.\n"""\n',
     "The main package for curses support for Python."),
    # wrapped first sentence, reST markup, "name -- text"
    ('"""The ``distro`` package (``distro`` stands for Linux Distribution) provides\n'
     'information about the Linux distribution it runs on.\n\nMore text.\n"""\n',
     "The distro package (distro stands for Linux Distribution) provides information about the Linux "
     "distribution it runs on."),
    ('"""Command-line interface for the :mod:`idna` package."""\n', "Command-line interface for the idna package."),
    ('"""idlelib.config -- Manage IDLE configuration information."""\n', "Manage IDLE configuration information."),
    ('"""lexer.py: Implement the lexer."""\n', "Implement the lexer."),
    ('"""CodeVision: see the structure."""\n', "CodeVision: see the structure."),
    # noise that is not a description
    ('"""$Id: lexer.py 3375 2008-02-13 fredrik $\nelementtree lexer\n"""\n', "elementtree lexer"),
    ("# From: https://github.com/ActiveState/code/blob/master/recipe.py  # noqa\nimport os\n", ""),
    ('"""!!! abstract "Usage Documentation"\n    [JSON Schema](../json_schema.md)\n\nBuild a schema.\n"""\n',
     "Build a schema."),
    ("#----------\n# ** ATTENTION **\n# This code was automatically generated from the file:\n# _ast.cfg\n"
     "#----------\nimport os\n", "This code was automatically generated from the file:"),
    ('"""LLMFunction."""\n', ""),
])
def test_module_doc_on_real_world_headers(source, expected):
    assert py.analyze("lexer.py", source).module_doc == expected


def test_symbol_doc_joins_the_wrapped_first_sentence():
    source = 'def load(path):\n    """Load the todo list from the file and\n    return it as a list.\n\n    More.\n    """\n'
    (symbol,) = py.analyze("store.py", source).symbols
    assert symbol.doc == "Load the todo list from the file and return it as a list."


@pytest.mark.parametrize("doc, expected", [
    # a role at the start of a line goes on with the sentence (urllib3's ConnectionPool)
    ("Base class for all connection pools, such as\n:class:`.HTTPConnectionPool` and :class:`.HTTPSConnectionPool`.",
     "Base class for all connection pools, such as HTTPConnectionPool and HTTPSConnectionPool."),
    ("Raised during\n:meth:`~anyio.abc.ByteReceiveStream.receive_until` if no delimiter is found.",
     "Raised during receive_until if no delimiter is found."),
    (":mod:`asyncio` implementation of a WebSocket client connection.\n\nMore.",
     "asyncio implementation of a WebSocket client connection."),
    # each role as Sphinx shows it
    ("Used by :class:`Foo <a.Foo>` and :py:func:`~a.b.c` objects.", "Used by Foo and c objects."),
    ("Same as :class:`!Plain`, but :data:`.CONSTANT`.", "Same as Plain, but CONSTANT."),
    ("Follows :pep:`8` and :rfc:`6570` closely.", "Follows PEP 8 and RFC 6570 closely."),
    ("Reads :file:`~/.bashrc` on start.", "Reads ~/.bashrc on start."),  # a file name: "~" is part of it
    ("Either :class:`Option`\\s or :class:`Argument`\\ s.", "Either Options or Arguments."),
    # field lists still end the summary, or are passed over
    ("Reads the file\n:param path: where it is\n:returns: the text", "Reads the file"),
    (":param path: where it is\n:raises ValueError: when it is not there\n\nReads the file.", "Reads the file."),
    # the block under a directive is the directive's (distro.linux_distribution)
    (".. deprecated:: 1.6.0\n\n    :func:`distro.linux_distribution()` is deprecated.\n\nReturn the OS name.",
     "Return the OS name."),
    (".. testsetup::\n\n    from packaging.version import parse\n\nParse a version.", "Parse a version."),
])
def test_doc_summary_reads_rest_roles_as_text(doc, expected):
    source = f'def fn():\n    """{doc}\n    """\n'
    (symbol,) = py.analyze("roles.py", source).symbols
    assert symbol.doc == expected


# ---------------------------------------------------------------- mounted routers, http.server, computed module names

def test_routers_mounted_with_a_prefix_are_recorded():
    source = (
        "from fastapi import FastAPI\nfrom routes import todos, users\nimport api.v1 as v1\nimport api.v2\n"
        "from .admin import router as admin_router\nAPI = '/api'\n"
        "app = FastAPI()\napp.include_router(todos.router, prefix='/api/todos')\napp.include_router(users.router)\n"
        "app.include_router(v1.router, prefix='/v1')\napp.include_router(api.v2.router, prefix=API + '/v2')\n"
        "app.include_router(admin_router, prefix='/admin')\n"
    )
    mounts = [(m.prefix, m.target.spec, m.target.names, m.target.kind, m.target.level)
              for m in py.analyze("main.py", source).http_mounts]
    assert mounts == [  # include_router without a prefix changes nothing, so it is not a mount
        ("/api/todos", "routes", ["todos"], "from", 0),
        ("/v1", "api.v1", [], "import", 0),
        ("/api/v2", "api.v2", [], "import", 0),
        ("/admin", "admin", ["router"], "from", 1),
    ]
    flask = ("from flask import Flask\n\ndef create_app():\n    from .views import bp\n    app = Flask(__name__)\n"
             "    app.register_blueprint(bp, url_prefix='/api')\n    return app\n")
    assert [(m.prefix, m.target.spec, m.target.names) for m in py.analyze("app.py", flask).http_mounts] == [
        ("/api", "views", ["bp"])]


def test_flask_url_prefix_takes_the_place_of_the_blueprints_own():
    # Flask: register_blueprint(bp, url_prefix=...) stands instead of Blueprint(url_prefix=...);
    # a blueprint registered on another one answers under that one's prefix
    source = (
        "from flask import Blueprint, Flask\nfrom views.users import users_bp\nfrom views.admin import admin_bp\n"
        "app = Flask(__name__)\n"
        "todos = Blueprint('todos', __name__, url_prefix='/todos')\n"
        "api = Blueprint('api', __name__, url_prefix='/api')\n"
        "items = Blueprint('items', __name__, url_prefix='/items')\n\n"
        "@todos.route('/<int:todo_id>')\ndef get_todo(todo_id):\n    return {}\n\n"
        "@items.get('/')\ndef list_items():\n    return []\n\n"
        "app.register_blueprint(todos, url_prefix='/api/todos')\n"
        "api.register_blueprint(items)\n"
        "app.register_blueprint(api, url_prefix='/v1')\n"
        "api.register_blueprint(users_bp, url_prefix='/users')\n"
        "app.register_blueprint(admin_bp, url_prefix=None)\n"
    )
    facts = py.analyze("app.py", source)
    assert [(r.method, r.path, r.base) for r in facts.http_routes] == [
        ("GET", "/api/todos/*", ""),  # not /api/todos/todos/*
        ("GET", "/v1/items", ""),  # api's own /api gave way to /v1, items keeps its own /items
    ]
    assert {s.name: s.hint for s in facts.symbols} == {
        "get_todo": "GET · /api/todos/<int:todo_id>", "list_items": "GET · /v1/items"}
    # users_bp of views/users.py: its own url_prefix gives way to /users, under api, which answers at /v1
    assert [(m.prefix, m.target.spec, m.base, m.replace) for m in facts.http_mounts] == [
        ("/users", "views.users", "/v1", True)]

    routes = py.analyze("views/users.py", "from flask import Blueprint\nusers_bp = Blueprint('users', __name__, "
                        "url_prefix='/people')\n\n@users_bp.get('/<int:user_id>')\ndef get_user(user_id):\n    pass\n")
    assert [(r.path, r.base) for r in routes.http_routes] == [("/people/*", "/people")]  # /people is what gives way

    # root -> api -> users_bp: a url_prefix that mounts this file stands instead of root's /root only
    chain = py.analyze("api.py", "from flask import Blueprint\nfrom views.users import users_bp\n"
                       "root = Blueprint('root', __name__, url_prefix='/root')\n"
                       "api = Blueprint('api', __name__, url_prefix='/api')\n"
                       "root.register_blueprint(api)\napi.register_blueprint(users_bp)\n")
    assert [(m.prefix, m.base, m.base_own, m.replace) for m in chain.http_mounts] == [("", "/root/api", "/root", False)]


def test_fastapi_prefixes_are_joined():
    source = (
        "from fastapi import APIRouter, FastAPI\nfrom routes import todos\n"
        "app = FastAPI()\napi = APIRouter(prefix='/api')\nitems = APIRouter(prefix='/items')\n\n"
        "@items.get('/{item_id}')\ndef get_item(item_id: int):\n    return {}\n\n"
        "api.include_router(items, prefix='/v1')\napi.include_router(todos.router)\napp.include_router(api)\n"
    )
    facts = py.analyze("main.py", source)
    assert [(r.method, r.path) for r in facts.http_routes] == [("GET", "/api/v1/items/*")]
    assert facts.symbols[0].hint == "GET · /api/v1/items/{item_id}"
    # todos.router answers under api's own /api, in front of its own prefix
    assert [(m.prefix, m.target.spec, m.target.names, m.base, m.replace) for m in facts.http_mounts] == [
        ("", "routes", ["todos"], "/api", False)]


def test_a_mount_without_a_prefix_counts_next_to_another_one():
    # on the app as it is, a router answers at the paths as written: alone that changes nothing, but
    # next to a mount under /v1 it is the second address
    source = ("from fastapi import FastAPI\nfrom routes.todos import router\nfrom routes import users\napp = FastAPI()\n"
              "app.include_router(router, prefix='/v1')\napp.include_router(router)\napp.include_router(users.router)\n")
    mounts = [(m.prefix, m.target.spec, m.target.names, m.replace) for m in py.analyze("main.py", source).http_mounts]
    assert mounts == [("/v1", "routes.todos", ["router"], False), ("", "routes.todos", ["router"], False)]

    local = py.analyze("main.py", "from fastapi import APIRouter, FastAPI\napp = FastAPI()\nrouter = APIRouter(prefix='/todos')\n\n"
                                  "@router.get('/')\ndef index():\n    return []\n\n"
                                  "app.include_router(router)\napp.include_router(router, prefix='/v1')\n")
    assert [r.path for r in local.http_routes] == ["/todos", "/v1/todos"]
    assert local.symbols[0].hint == "GET · /todos, /v1/todos"
    alone = py.analyze("main.py", "from fastapi import APIRouter, FastAPI\napp = FastAPI()\nrouter = APIRouter(prefix='/todos')\n\n"
                                  "@router.get('/')\ndef index():\n    return []\n\napp.include_router(router)\n")
    assert [(r.path, r.base) for r in alone.http_routes] == [("/todos", "/todos")]  # unchanged: its own prefix stays


def test_router_mounted_in_its_own_file_moves_its_routes():
    source = ("from fastapi import APIRouter, FastAPI\napp = FastAPI()\nrouter = APIRouter(prefix='/todos')\n\n"
              "@router.get('/')\ndef index():\n    return []\n\n@app.get('/health')\ndef health():\n    return 'ok'\n\n"
              "app.include_router(router, prefix='/api')\n")
    facts = py.analyze("main.py", source)
    assert {(r.method, r.path) for r in facts.http_routes} == {("GET", "/api/todos"), ("GET", "/health")}
    assert facts.http_mounts == []


def test_http_server_routes():
    source = (
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n\n"
        "STATIC = {'/': 'index.html', '/app.js': 'app.js'}\n"
        "API = {('GET', '/api/recent'): 'recent', ('POST', '/api/pick'): 'pick'}\n\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        path = self.path.split('?')[0]\n"
        "        if path == '/api/todos':\n            pass\n"
        "        elif path.startswith('/api/todos/'):\n            pass\n"
        "        elif self.path in ('/a', '/b'):\n            pass\n"
        "    def do_DELETE(self):\n"
        "        if urlparse(self.path).path.startswith('/api/todos/'):\n            pass\n"
    )
    facts = py.analyze("server.py", source)
    assert "http.server" in facts.frameworks
    assert [(r.method, r.path) for r in facts.http_routes] == [  # "/" and "/app.js" are the page and its files
        ("GET", "/api/recent"), ("POST", "/api/pick"),
        ("GET", "/api/todos"), ("GET", "/api/todos/*"), ("GET", "/a"), ("GET", "/b"), ("DELETE", "/api/todos/*"),
    ]
    assert py.analyze("client.py", "import http.client\nif path == '/x':\n    pass\n").http_routes == []


def test_module_names_built_at_run_time_point_to_the_matching_files():
    # rich loads its unicode tables (unicode15-0-0.py, ...) by a name made at run time
    source = ("from importlib import import_module\n\ndef load(version):\n"
              "    name = f'.unicode{version}'\n    return import_module(name, __package__)\n\n"
              "def other(kind):\n    return import_module('pkg.lexers.' + kind)\n")  # no fixed name part: nothing
    facts = py.analyze("pkg/tables/__init__.py", source)
    (table,) = [i for i in facts.imports if i.kind == "dynamic"]
    assert (table.spec, table.names, table.level) == ("", ["unicode*"], 1)
    index = fake_index("pkg/__init__.py", "pkg/tables/__init__.py", "pkg/tables/unicode15-0-0.py",
                       "pkg/tables/unicode9-0-0.py", "pkg/tables/_versions.py", "pkg/tables/unicode_extra/__init__.py")
    assert py.resolve(table, "pkg/tables/__init__.py", index) == [
        "pkg/tables/unicode15-0-0.py", "pkg/tables/unicode9-0-0.py", "pkg/tables/unicode_extra/__init__.py"]
