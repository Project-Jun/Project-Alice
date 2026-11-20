from pathlib import Path

import pytest

from codevision.lang import js_lang
from codevision.model import ImportRef, ProjectIndex

FRONT = "frontend/src"


def make_index(root: Path) -> ProjectIndex:
    files, dirs = [], []
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        (dirs if p.is_dir() else files).append(rel)
    return ProjectIndex(root, files, dirs)


def write_project(root: Path, files: dict[str, str]) -> ProjectIndex:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return make_index(root)


def resolve_all(index: ProjectIndex, rel: str) -> set[str]:
    facts = js_lang.analyze(rel, index.read_text(rel))
    out: set[str] = set()
    for imp in facts.imports:
        out.update(js_lang.resolve(imp, rel, index))
    return out


def resolve_spec(index: ProjectIndex, spec: str, importer: str) -> list[str]:
    return js_lang.resolve(ImportRef(spec=spec), importer, index)


@pytest.fixture
def demo_index(demo_root) -> ProjectIndex:
    return make_index(demo_root)


# --- demo project


def test_demo_todolist_imports_resolve(demo_index):
    assert resolve_all(demo_index, f"{FRONT}/components/TodoList.jsx") == {
        f"{FRONT}/api.js",
        f"{FRONT}/components/TodoItem.jsx",
    }
    assert resolve_all(demo_index, f"{FRONT}/main.jsx") == {f"{FRONT}/App.jsx"}
    assert resolve_all(demo_index, f"{FRONT}/components/TodoItem.jsx") == {f"{FRONT}/api.js"}
    react = ImportRef(spec="react")
    assert js_lang.resolve(react, f"{FRONT}/main.jsx", demo_index) == []


def test_demo_api_http_calls_and_symbols(demo_index):
    rel = f"{FRONT}/api.js"
    facts = js_lang.analyze(rel, demo_index.read_text(rel))
    assert facts.language == "javascript"
    assert not facts.parse_error
    calls = {(c.method, c.path) for c in facts.http_calls}
    assert calls == {("GET", "/todos"), ("POST", "/todos"), ("DELETE", "/todos/*")}
    symbols = {s.name: s for s in facts.symbols}
    assert [s.signature for s in facts.symbols] == ["fetchTodos()", "addTodo(title)", "deleteTodo(id)"]
    assert symbols["fetchTodos"].doc == "할 일 목록을 서버에서 가져와요."
    assert symbols["fetchTodos"].hint == "GET · /todos"
    assert symbols["deleteTodo"].hint == "DELETE · /todos/*"
    assert all(s.exported and s.kind == "function" for s in facts.symbols)


def test_demo_main_is_entry(demo_index):
    rel = f"{FRONT}/main.jsx"
    facts = js_lang.analyze(rel, demo_index.read_text(rel))
    assert facts.is_entry
    assert facts.entry_reason == "화면을 처음 띄우는 파일"
    assert facts.frameworks == {"react"}
    entries = js_lang.project_entries(demo_index)
    assert entries == {rel: "index.html에서 처음 불러오는 파일"}


def test_demo_components(demo_index):
    app = js_lang.analyze(f"{FRONT}/App.jsx", demo_index.read_text(f"{FRONT}/App.jsx"))
    (symbol,) = app.symbols
    assert (symbol.name, symbol.kind, symbol.signature) == ("App", "component", "App()")
    assert symbol.hint == "화면 조각(컴포넌트)"
    assert symbol.exported
    item = js_lang.analyze(
        f"{FRONT}/components/TodoItem.jsx", demo_index.read_text(f"{FRONT}/components/TodoItem.jsx")
    )
    assert [(s.kind, s.signature) for s in item.symbols] == [("component", "TodoItem({ todo, onDeleted })")]
    config = js_lang.analyze("frontend/vite.config.js", demo_index.read_text("frontend/vite.config.js"))
    assert not config.is_entry and config.symbols == []


# --- imports and symbols from inline sources

IMPORTS_TS = '''\
// Todo store helpers
import api, { getTodos as fetchAll, type Todo } from "./api";
import * as utils from "./utils";
import "./styles.css";
import type { Props } from "./types";
export { Button } from "./Button";
export * from "./shared";
const fs = require("fs");
const { join, resolve: r } = require("path");

/** Loads the page lazily. */
export const loadPage = async (name: string) => {
  const mod = await import("./pages/Home");
  return mod;
};

export class TodoStore {}
'''


def test_import_kinds_and_names():
    facts = js_lang.analyze("src/store.ts", IMPORTS_TS)
    assert facts.language == "typescript"
    got = [(i.kind, i.spec, i.names) for i in facts.imports]
    assert got == [
        ("import", "./api", ["api", "fetchAll", "Todo"]),
        ("import", "./utils", ["utils"]),
        ("side-effect", "./styles.css", []),
        ("import", "./types", ["Props"]),
        ("reexport", "./Button", ["Button"]),
        ("reexport", "./shared", ["*"]),
        ("require", "fs", ["fs"]),
        ("require", "path", ["join", "r"]),
        ("dynamic", "./pages/Home", []),
    ]
    assert facts.imports[0].line == 2
    assert facts.module_doc == "Todo store helpers"
    load, store = facts.symbols
    assert (load.signature, load.doc, load.exported) == ("loadPage(name: string)", "Loads the page lazily.", True)
    assert (store.kind, store.signature) == ("class", "class TodoStore")


def test_long_parameters_are_squeezed():
    src = "export function save(\n  title,\n  description,\n  { priority, dueDate, tags },\n) {}\n"
    (symbol,) = js_lang.analyze("a.js", src).symbols
    assert "\n" not in symbol.signature
    assert symbol.signature.startswith("save(title, description, { priority")
    assert symbol.signature.endswith("…)")


def test_parse_error_does_not_raise():
    facts = js_lang.analyze("broken.js", 'import x from "./x"\nfunction ( {\n')
    assert facts.parse_error
    assert [i.spec for i in facts.imports] == ["./x"]


# --- resolve


def test_tsconfig_paths_alias(tmp_path):
    index = write_project(tmp_path, {
        "web/package.json": "{}",
        "web/tsconfig.json": """{
  // aliases for the app
  "extends": "./tsconfig.base.json",
  "compilerOptions": {
    "paths": {
      "@/*": ["src/*"],   /* main alias */
      "@ui/*": ["src/components/ui/*",],
    },
  },
}""",
        "web/tsconfig.base.json": '{"compilerOptions": {"baseUrl": "."}}',
        "web/src/app.ts": "",
        "web/src/lib/util.ts": "",
        "web/src/components/ui/Button.tsx": "",
        "web/src/hooks/index.ts": "",
    })
    importer = "web/src/app.ts"
    assert resolve_spec(index, "@/lib/util", importer) == ["web/src/lib/util.ts"]
    assert resolve_spec(index, "@ui/Button", importer) == ["web/src/components/ui/Button.tsx"]
    assert resolve_spec(index, "@/hooks", importer) == ["web/src/hooks/index.ts"]
    assert resolve_spec(index, "src/lib/util", importer) == ["web/src/lib/util.ts"]  # baseUrl
    assert resolve_spec(index, "react", importer) == []
    assert resolve_spec(index, "@scope/pkg", importer) == []


def test_at_alias_without_config_maps_to_src(tmp_path):
    index = write_project(tmp_path, {
        "package.json": "{}",
        "src/pages/Home.vue": "",
        "src/utils/format.js": "",
        "src/main.js": "",
    })
    assert resolve_spec(index, "@/utils/format", "src/main.js") == ["src/utils/format.js"]
    assert resolve_spec(index, "~/pages/Home.vue", "src/main.js") == ["src/pages/Home.vue"]
    assert resolve_spec(index, "/src/utils/format.js", "src/main.js") == ["src/utils/format.js"]


def test_js_specifier_maps_to_ts_and_index(tmp_path):
    index = write_project(tmp_path, {
        "server/src/index.ts": 'import { db } from "./db.js";\nimport routes from "./routes";\n',
        "server/src/db.ts": "",
        "server/src/routes/index.ts": "",
        "server/src/data.json": "",
    })
    assert resolve_all(index, "server/src/index.ts") == {"server/src/db.ts", "server/src/routes/index.ts"}
    assert resolve_spec(index, "./data.json", "server/src/index.ts") == ["server/src/data.json"]
    assert resolve_spec(index, "../../outside", "server/src/index.ts") == []
    assert resolve_spec(index, "node:fs", "server/src/index.ts") == []


# --- servers and clients

EXPRESS_TS = '''\
import express from "express";
import { listTodos, createTodo } from "./handlers";

const app = express();
const router = express.Router();

router.get("/todos", listTodos);
router.post("/todos", createTodo);
app.delete("/todos/:id", (req, res) => res.sendStatus(204));
router.all("/health", (req, res) => res.send("ok"));
router.route("/items/:id").get(getItem).put(putItem);
app.get("env");
app.use("/api", router);

app.listen(3000, () => console.log("ready"));
'''


def test_express_routes_and_listen():
    facts = js_lang.analyze("server/app.ts", EXPRESS_TS)
    routes = {(r.method, r.path, r.handler) for r in facts.http_routes}
    assert routes == {  # `router` is mounted with app.use("/api", router); app's own route is not
        ("GET", "/api/todos", "listTodos"),
        ("POST", "/api/todos", "createTodo"),
        ("DELETE", "/todos/*", ""),
        ("ANY", "/api/health", ""),
        ("GET", "/api/items/*", "getItem"),
        ("PUT", "/api/items/*", "putItem"),
    }
    assert facts.http_mounts == []  # the mounted router is made in this file
    assert facts.http_calls == []
    assert facts.frameworks == {"express"}
    assert facts.is_entry and facts.entry_reason == "서버를 켜는 파일"


CLIENT_TS = '''\
import axios from "axios";
const API = import.meta.env.VITE_API_URL;
const http = axios.create({ baseURL: "http://localhost:8000/api" });

export const list = () => http.get<Todo[]>("/todos");
export const create = (title: string) => axios.post(API + "/todos", { title });
export const remove = (id: number) => axios({ url: `${API}/todos/${id}`, method: "delete" });
export const update = (id) => fetch("/todos/" + id, { method: "PATCH" });
export const ping = () => fetch(someUrl);
'''


def test_axios_fetch_and_url_shapes():
    facts = js_lang.analyze("web/src/api.ts", CLIENT_TS)
    calls = [(c.method, c.path) for c in facts.http_calls]
    assert calls == [
        ("GET", "/api/todos"),
        ("POST", "/todos"),
        ("DELETE", "/todos/*"),
        ("PATCH", "/todos/*"),
    ]
    assert facts.http_routes == []
    assert not facts.is_entry


def test_next_file_routes_and_anonymous_default():
    page = js_lang.analyze("src/pages/api/todos/[id].js", "export default function handler(req, res) {}\n")
    assert [(r.method, r.path) for r in page.http_routes] == [("ANY", "/api/todos/*")]
    route = js_lang.analyze(
        "app/(shop)/api/items/route.ts",
        "export async function GET() {}\nexport const POST = async (req: Request) => {};\n",
    )
    assert {(r.method, r.path) for r in route.http_routes} == {("GET", "/api/items"), ("POST", "/api/items")}
    assert js_lang.analyze("server/app/route.js", "const x = 1;\n").http_routes == []
    header = js_lang.analyze("src/Header.jsx", "export default () => <header>hi</header>;\n")
    assert [(s.name, s.kind, s.exported) for s in header.symbols] == [("Header", "component", True)]


def test_vue_and_frameworks():
    src = 'import { createApp } from "vue";\nimport { PrismaClient } from "@prisma/client";\n' \
          'const app = createApp(App);\napp.use(router);\napp.mount("#app");\n'
    facts = js_lang.analyze("src/main.js", src)
    assert facts.frameworks == {"vue", "prisma"}
    assert facts.is_entry and facts.entry_reason == "화면을 처음 띄우는 파일"


MOUNTS_JS = '''\
const express = require("express");
const todos = require("./routes/todos");
import users from "./routes/users.js";
import { makeNotesRouter } from "./routes/notes.js";
import { Hono } from "hono";
import api from "./api";
import todoRoutes from "./todos";
import items from "./items";
const app = express();
app.use(express.json());
app.use("/api/todos", todos);
app.use("/api/users", auth, users);
app.use("/api/admin", require("./routes/admin"));
const hono = new Hono();
hono.route("/v2", api);
app.register(todoRoutes, { prefix: "/todo-list" });
const router = express.Router();
router.use("/items", items.routes());
app.use("/api/notes", makeNotesRouter(db));
app.use("/api/tags", require("./routes/tags")(db));
app.use("/api/local", localFactory(db));
'''


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_mounted_routers_are_recorded(monkeypatch, tree_sitter):
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    facts = js_lang.analyze("server/index.js", MOUNTS_JS)
    got = [(m.prefix, m.target.spec) for m in facts.http_mounts if m.target.spec.startswith(".")]
    assert got == [
        ("/api/todos", "./routes/todos"), ("/api/users", "./routes/users.js"), ("/api/admin", "./routes/admin"),
        ("/v2", "./api"), ("/todo-list", "./todos"), ("/items", "./items"),
        ("/api/notes", "./routes/notes.js"), ("/api/tags", "./routes/tags"),  # router factories
    ]


HELPERS_JS = '''\
const BASE = "http://localhost:8000";
(() => {
  async function api(url, method) {
    const r = await fetch(url, { method: method === "POST" ? "POST" : "GET" });
    return r.json();
  }
  const get = (path) => fetch(BASE + path).then((r) => r.json());
  function save(path, options) { return fetch("/api" + path, options); }
  async function remove(id) { await fetch(`${BASE}/todos/${id}`, { method: "DELETE" }); }
  api("/api/recent");
  api("/api/pick-folder", "POST");
  get("/todos");
  save("/notes", { method: "PUT" });
  api(someUrl);
})();
'''


def test_calls_through_request_helpers_are_requests():
    if not js_lang.TREE_SITTER:
        pytest.skip("request helpers are found with tree-sitter only")
    facts = js_lang.analyze("web/app.js", HELPERS_JS)
    assert [(c.method, c.path) for c in facts.http_calls] == [
        ("DELETE", "/todos/*"),  # remove(id) makes its own request: an id is not an address
        ("ANY", "/api/recent"), ("POST", "/api/pick-folder"), ("GET", "/todos"), ("PUT", "/api/notes"),
    ]


def test_project_links_from_html(tmp_path):
    index = write_project(tmp_path, {
        "web/index.html": '<!doctype html>\n<link rel="stylesheet" href="https://cdn.example.com/x.css">\n'
                          '<link rel="stylesheet" href="./app.css">\n<link rel="icon" href="/favicon.svg">\n'
                          '<script src="./app.js?v=2" defer></script>\n<!-- <script src="old.js"></script> -->\n',
        "web/app.js": "", "web/app.css": "", "web/old.js": "",
        "site/package.json": "{}", "site/index.html": '<script type="module" src="/src/main.ts"></script>',
        "site/src/main.ts": "",
    })
    assert js_lang.project_links(index) == {
        "site/index.html": ["site/src/main.ts"],
        "web/index.html": ["web/app.js", "web/app.css"],
    }


# --- project entries


def test_package_json_entries(tmp_path):
    index = write_project(tmp_path, {
        "api/package.json": """{
  "main": "src/index.js",
  "bin": {"todo": "./bin/cli.js"},
  "scripts": {
    "start": "node server.js",
    "dev": "nodemon --watch src src/app.ts",
    "worker": "cross-env NODE_ENV=dev tsx watch jobs/worker.ts && echo done",
    "build": "tsc -p ."
  }
}""",
        "api/src/index.js": "",
        "api/src/app.ts": "",
        "api/bin/cli.js": "",
        "api/server.js": "",
        "api/jobs/worker.ts": "",
        "web/index.html": '<script type="module" src="./main.ts"></script>\n<!-- <script src="old.js"></script> -->\n'
                          '<script src="https://cdn.example.com/x.js"></script>',
        "web/main.ts": "",
        "web/old.js": "",
    })
    entries = js_lang.project_entries(index)
    reason = "package.json에 적힌 시작 파일"
    assert entries == {
        "api/src/index.js": reason,
        "api/bin/cli.js": reason,
        "api/server.js": reason,
        "api/src/app.ts": reason,
        "api/jobs/worker.ts": reason,
        "web/main.ts": "index.html에서 처음 불러오는 파일",
    }


# --- module doc on real-world headers

HEADERS = [
    ("// Copyright 2018 The Chromium Authors. All rights reserved.\n"
     "// Use of this source code is governed by a BSD-style license that can be\n"
     "// found in the LICENSE file.\n\n// Draws the todo list.\nexport function draw() {}\n",
     "Draws the todo list."),
    ("/*\n * Copyright (c) 2014-present, Facebook, Inc.\n *\n * This source code is licensed under the MIT license.\n"
     " */\n// 할 일 목록을 그려요.\nexport const x = 1;\n", "할 일 목록을 그려요."),
    ("// SPDX-License-Identifier: MIT\n/** Todo API client. */\nexport {}\n", "Todo API client."),
    ("// Fetches the todo list from the server and\n// returns it as an array.\nexport function load() {}\n",
     "Fetches the todo list from the server and returns it as an array."),
    ("/**\n * @fileoverview\n * Shows the todo list and\n * the header.\n */\nimport x from './x';\n",
     "Shows the todo list and the header."),
    ("/* eslint-disable no-console */\n'use strict';\n// Server entry.\nconst a = 1;\n", "Server entry."),
]


@pytest.mark.parametrize("tree_sitter", [True, False])
@pytest.mark.parametrize("source, expected", HEADERS)
def test_module_doc_skips_license_headers(monkeypatch, tree_sitter, source, expected):
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    assert js_lang.analyze("src/app.js", source).module_doc == expected


def test_jsdoc_joins_the_wrapped_first_sentence():
    src = "/**\n * Loads the todos from the server and\n * returns them.\n * @returns {Promise}\n */\n" \
          "export async function load() {}\n"
    (symbol,) = js_lang.analyze("api.js", src).symbols
    assert symbol.doc == "Loads the todos from the server and returns them."


# --- regex fallback


def test_regex_fallback(monkeypatch, demo_index):
    monkeypatch.setattr(js_lang, "TREE_SITTER", False)
    api = js_lang.analyze(f"{FRONT}/api.js", demo_index.read_text(f"{FRONT}/api.js"))
    assert {(c.method, c.path) for c in api.http_calls} == {
        ("GET", "/todos"), ("POST", "/todos"), ("DELETE", "/todos/*"),
    }
    assert [s.signature for s in api.symbols] == ["fetchTodos()", "addTodo(title)", "deleteTodo(id)"]
    assert resolve_all(demo_index, f"{FRONT}/components/TodoList.jsx") == {
        f"{FRONT}/api.js",
        f"{FRONT}/components/TodoItem.jsx",
    }
    main = js_lang.analyze(f"{FRONT}/main.jsx", demo_index.read_text(f"{FRONT}/main.jsx"))
    assert main.is_entry and main.frameworks == {"react"}

    facts = js_lang.analyze("src/store.ts", IMPORTS_TS)
    assert not facts.parse_error
    kinds = {(i.kind, i.spec) for i in facts.imports}
    assert {("require", "fs"), ("require", "path"), ("dynamic", "./pages/Home"),
            ("side-effect", "./styles.css"), ("reexport", "./shared"), ("import", "./api")} <= kinds
    client = js_lang.analyze("web/src/api.ts", CLIENT_TS)
    assert {(c.method, c.path) for c in client.http_calls} == {
        ("GET", "/api/todos"), ("POST", "/todos"), ("DELETE", "/todos/*"), ("PATCH", "/todos/*"),
    }
    server = js_lang.analyze("server/app.ts", EXPRESS_TS)
    assert ("GET", "/api/todos") in {(r.method, r.path) for r in server.http_routes}  # mounted at /api
    assert ("DELETE", "/todos/*") in {(r.method, r.path) for r in server.http_routes}
    assert server.is_entry and server.entry_reason == "서버를 켜는 파일"
    crlf = js_lang.analyze("lib.js", "function helper(a) {}\r\nmodule.exports = { helper };\r\nexport { helper };\r\n")
    assert [(s.signature, s.exported) for s in crlf.symbols] == [("helper(a)", True)]
