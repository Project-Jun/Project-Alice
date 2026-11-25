import time
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


# --- routers placed on other routers of the same file (2026-11-20)

PLACED_EXPRESS = '''\
const express = require("express");
const todos = require("./routes/todos");
const app = express();
const api = express.Router();
const items = express.Router();
items.get("/", list);
items.delete("/:id", remove);
api.use("/items", items);
api.use("/todos", todos);
app.use("/api", api);
'''
PLACED_ROUTER_IMPORT = '''\
import express, { Router } from "express";
import users from "./users.js";
const app = express();
const api = Router();
const v1 = Router();
v1.get("/items", list);
v1.use("/users", users);
api.use("/v1", v1);
app.use("/api", api);
'''
PLACED_TWICE = '''\
const express = require("express");
const todos = require("./routes/todos");
const app = express();
const api = express.Router();
api.get("/health", health);
api.use("/todos", todos);
app.use("/v1", api);
app.use("/v2", api);
'''
# no prefix: a router answers where the app or router it is mounted on does
PLACED_WITHOUT_PREFIX = '''\
const express = require("express");
const todos = require("./routes/todos");
const notes = require("./routes/notes");
const app = express();
const api = express.Router();
const top = express.Router();
const extra = express.Router();
const sub = express.Router();
top.get("/x", x);
extra.get("/y", y);
sub.get("/items", items);
app.use(top);
app.use("/v1", extra);
app.use(extra);
api.use(sub);
api.use(todos);
top.use(notes);
app.use(express.json());
app.use("/api", api);
'''
PLACED_KOA = '''\
const Koa = require("koa");
const Router = require("@koa/router");
const users = require("./routes/users");
const app = new Koa();
const api = new Router({ prefix: "/api" });
const todos = new Router();
todos.get("/", list);
api.use("/todos", todos.routes(), todos.allowedMethods());
api.use("/users", users.routes());
api.get("/health", health);
app.use(api.routes());
'''
KOA_OWN_PREFIX = '''\
import Router from "koa-router";
const router = new Router({ prefix: "/items" });
router.get("/", list);
router.get("/:id", show);
export default router;
'''
PLACED_HONO = '''\
import { Hono } from "hono";
import books from "./books";
const app = new Hono();
const api = new Hono().basePath("/api");
const tags = new Hono();
tags.get("/:id", show);
api.route("/tags", tags);
api.route("/books", books);
app.route("/", api);
const v1 = app.basePath("/v1");
v1.get("/items", list);
'''
PLACED_FASTIFY = '''\
const fastify = require("fastify")();
const todos = require("./routes/todos");
async function api(fastify, opts) {
  fastify.get("/items", list);
  fastify.route({ method: "POST", url: "/items", handler: create });
  fastify.register(todos, { prefix: "/todos" });
  fastify.register(async (f) => {
    f.get("/ping", ping);
  }, { prefix: "/v1" });
  fastify.register(deep);
}
const deep = async (app) => { app.get("/deep", deepest); };
fastify.register(api, { prefix: "/api" });
fastify.get("/health", health);
'''
# parts known only at run time stay ${} in the raw prefix and are dropped or become * when joined
PLACED_UNKNOWN = '''\
const express = require("express");
const todos = require("./routes/todos");
const app = express();
const api = express.Router();
const local = express.Router();
local.get("/z", z);
api.use("/todos", todos);
api.use(`${process.env.SUB}/local`, local);
app.use(`${process.env.BASE}/api`, api);
app.use(`${process.env.ONLY}`, other);
'''
# two routers of this file side by side, each with an imported router on it: each mount learns the
# place of its own router (2026-11-21: the second one answered where it was written, /users)
PLACED_SIDE_BY_SIDE = '''\
const express = require("express");
const todos = require("./routes/todos");
const users = require("./routes/users");
const app = express();
const api = express.Router();
const admin = express.Router();
app.use("/api", api);
app.use("/admin", admin);
api.use("/todos", todos);
admin.use("/users", users);
'''


@pytest.mark.parametrize("tree_sitter", [True, False])
@pytest.mark.parametrize("rel, source, routes, mounts", [
    ("server/index.js", PLACED_EXPRESS, {("GET", "/api/items"), ("DELETE", "/api/items/*")},
     [("/todos", "./routes/todos", "/api")]),
    ("server/app.mjs", PLACED_ROUTER_IMPORT, {("GET", "/api/v1/items")}, [("/users", "./users.js", "/api/v1")]),
    # placed twice: each place answers, and the imported router gets one mount per place
    ("server/index.js", PLACED_TWICE, {("GET", "/v1/health"), ("GET", "/v2/health")},
     [("/todos", "./routes/todos", "/v1"), ("/todos", "./routes/todos", "/v2")]),
    # top.use(notes) is kept too: top is an express.Router() (base "", it moves only if another file mounts this one)
    ("server/index.js", PLACED_WITHOUT_PREFIX,
     {("GET", "/x"), ("GET", "/v1/y"), ("GET", "/y"), ("GET", "/api/items")},
     [("", "./routes/todos", "/api"), ("", "./routes/notes", "")]),
    ("server/index.js", PLACED_KOA, {("GET", "/api/todos"), ("GET", "/api/health")},
     [("/users", "./routes/users", "/api")]),
    ("server/routes.js", KOA_OWN_PREFIX, {("GET", "/items"), ("GET", "/items/*")}, []),
    ("server/index.ts", PLACED_HONO, {("GET", "/api/tags/*"), ("GET", "/v1/items")}, [("/books", "./books", "/api")]),
    ("server/index.js", PLACED_FASTIFY,
     {("GET", "/api/items"), ("POST", "/api/items"), ("GET", "/api/v1/ping"), ("GET", "/api/deep"), ("GET", "/health")},
     [("/todos", "./routes/todos", "/api")]),
    ("server/index.js", PLACED_UNKNOWN, {("GET", "/api/*/local/z")}, [("/todos", "./routes/todos", "${}/api")]),
    ("server/index.js", PLACED_SIDE_BY_SIDE, set(),
     [("/todos", "./routes/todos", "/api"), ("/users", "./routes/users", "/admin")]),
])
def test_routers_answer_where_they_are_placed_in_their_file(monkeypatch, tree_sitter, rel, source, routes, mounts):
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    facts = js_lang.analyze(rel, source)
    if not tree_sitter:
        routes = routes - {("POST", "/api/items")}  # fastify.route({ ... }) is read with tree-sitter only
    assert {(r.method, r.path) for r in facts.http_routes} == routes
    assert len(facts.http_routes) == len(routes)
    assert [(m.prefix, m.target.spec, m.base) for m in facts.http_mounts if m.target.spec.startswith(".")] == mounts
    assert all(not m.replace and not m.base_own for m in facts.http_mounts)  # no Flask-style url_prefix in JS


def test_chained_hono_routes_answer_where_their_router_is_placed():
    if not js_lang.TREE_SITTER:
        pytest.skip("chained routes are read with tree-sitter only")
    facts = js_lang.analyze("server/index.ts", '''\
import { Hono } from "hono";
const api = new Hono().basePath("/api");
const todos = new Hono()
  .get("/", list)
  .post("/", create);
api.route("/todos", todos);
export default new Hono().basePath("/v2").get("/ping", ping);
''')
    assert {(r.method, r.path) for r in facts.http_routes} == {
        ("GET", "/api/todos"), ("POST", "/api/todos"), ("GET", "/v2/ping")}


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_routers_placed_many_times_on_each_other_take_no_time(monkeypatch, tree_sitter):
    # nine routers, each mounted six times on the one before: 6 ** 8 ways down the chain without the memo
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    lines = ['const express = require("express");', 'const leaf = require("./leaf");', "const app = express();"]
    lines += [f"const r{i} = express.Router();" for i in range(9)]
    lines += ['r8.get("/x", x);', 'r8.use("/leaf", leaf);']
    lines += [f'r{i}.use("/p{k}", r{i + 1});' for i in range(8) for k in range(6)]
    source = "\n".join(lines + ['app.use("/api", r0);']) + "\n"
    facts = js_lang.analyze("server/index.js", source)
    paths = [r.path for r in facts.http_routes]
    assert len(paths) == 8 and paths[0] == "/api/p0/p0/p0/p0/p0/p0/p0/p0/x"  # 8 addresses at most
    assert len(facts.http_mounts) == 8 and facts.http_mounts[0].base == "/api/p0/p0/p0/p0/p0/p0/p0/p0"
    steps = []
    real = js_lang._join_raw
    monkeypatch.setattr(js_lang, "_join_raw", lambda a, b: steps.append(1) or real(a, b))
    js_lang.analyze("server/index.js", source)
    assert len(steps) < 5_000


# middleware first and no path: the router answers where the app or router it is mounted on does
PLACED_AFTER_MIDDLEWARE = '''\
const express = require("express");
const cors = require("cors");
const todos = require("./routes/todos");
const app = express();
const api = express.Router();
const sub = express.Router();
sub.get("/items", list);
api.use(express.json(), sub);
api.use(cors(), todos);
app.use("/api", api);
'''
KOA_ROUTES_AND_METHODS = '''\
const Router = require("@koa/router");
const todos = require("./routes/todos");
const api = new Router({ prefix: "/api" });
const v1 = new Router({ prefix: "/v1" });
v1.get("/ping", ping);
api.use(v1.routes(), v1.allowedMethods());
api.use(todos.routes(), todos.allowedMethods());
module.exports = api;
'''
# no path on a router made in this file: recorded even when that router is placed nowhere here,
# because another file can mount this one (python_lang does the same); alone on the app it is not
AGGREGATOR = '''\
const express = require("express");
const { Router } = require("express");
const KoaRouter = require("@koa/router");
const cors = require("cors");
const auth = require("./auth");
const users = require("./users");
const notes = require("./notes");
const tags = require("./tags");
const extra = require("./extra");
const app = express();
const router = Router();
const legacy = express.Router();
const koa = new KoaRouter();
router.use(auth);
router.use("/users", users);
legacy.use(cors(), notes);
koa.use(tags.routes());
app.use(extra);
module.exports = router;
'''


@pytest.mark.parametrize("tree_sitter", [True, False])
@pytest.mark.parametrize("source, routes, mounts", [
    (PLACED_AFTER_MIDDLEWARE, {("GET", "/api/items")}, [("", "./routes/todos", "/api")]),
    (KOA_ROUTES_AND_METHODS, {("GET", "/api/v1/ping")}, [("", "./routes/todos", "/api")]),
    # app.use(extra) on the app moves nothing alone, but is recorded: another file may mount extra under a prefix
    (AGGREGATOR, set(), [("", "./auth", ""), ("/users", "./users", ""), ("", "./notes", ""), ("", "./tags", ""),
                         ("", "./extra", "")]),
], ids=["express-middleware-first", "koa-routes-and-allowed-methods", "bare-on-a-router-of-the-file"])
def test_routers_mounted_with_no_path_answer_where_their_parent_does(monkeypatch, tree_sitter, source, routes, mounts):
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    facts = js_lang.analyze("server/index.js", source)
    assert {(r.method, r.path) for r in facts.http_routes} == routes
    assert [(m.prefix, m.target.spec, m.base) for m in facts.http_mounts if m.target.spec.startswith(".")] == mounts


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_a_long_plugin_written_in_register_keeps_its_prefix(monkeypatch, tree_sitter):
    # the fallback reads a call's arguments within 5,000 characters: { prefix } comes after the whole plugin
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    body = "\n".join(f"  f.get('/r{i}', async () => {i});" for i in range(200))
    source = f"const fastify = require('fastify')();\nfastify.register(async (f) => {{\n{body}\n}}, {{ prefix: '/api' }});\n"
    assert len(source) > 6_000
    paths = [r.path for r in js_lang.analyze("server/index.js", source).http_routes]
    assert len(paths) == 200 and all(p.startswith("/api/r") for p in paths)


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_a_variable_before_a_router_is_not_taken_for_middleware(monkeypatch, tree_sitter):
    # app.use(PREFIX, r): PREFIX may well be a path (or middleware): the mount stays unread, as before
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    facts = js_lang.analyze("server/index.js", '''\
const express = require("express");
const { PREFIX } = require("./config");
const todos = require("./routes/todos");
const app = express();
const api = express.Router();
api.get("/x", x);
app.use(PREFIX, api);
app.use(PREFIX, todos);
app.use("/v1", todos);
''')
    assert {(r.method, r.path) for r in facts.http_routes} == {("GET", "/x")}
    assert [(m.prefix, m.target.spec, m.base) for m in facts.http_mounts] == [("/v1", "./routes/todos", "")]


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


# --- Vue and Svelte files (2026-11-20): the code of their <script> blocks, at its own lines

VUE_SFC = '''\
<!-- <script>import Bad from "./Bad.vue"</script> -->
<template>
  <ul :title="'<script>'" @click="pick('</script>')">
    <li v-for="t in todos" :key="t.id">{{ '<script>' }} {{ a < b }}</li>
    <template v-if="more"><p>unclosed<br></template>
  </ul>
</template>

<script lang="ts">
// The todo list screen part
import { defineComponent } from "vue"
export default defineComponent({ name: "TodoList" })
</script>
<script setup lang="ts" generic="T extends Record<string, unknown>">
import TodoItem from "./TodoItem.vue"
import { api } from "@/api"
const props = defineProps<{ items: T[] }>()
async function remove(id: number) {
  await fetch(`/api/todos/${id}`, { method: "DELETE" })
}
</script>

<style scoped>
li::before { content: "<script>"; }
</style>
'''

SVELTE_SFC = '''\
<script context="module">
  export const prerender = true;
</script>

<script lang="ts">
  import Nav from "$lib/Nav.svelte";
  let { data } = $props();
  function save(title: string) {
    return fetch("/api/todos", { method: "POST", body: title });
  }
</script>

<svelte:head>
  <script>import Ads from "./Ads.svelte";</script>
</svelte:head>
<p title={"<script>"}>{"<script>import Bad from './Bad.svelte'</script>"}</p>
{#if data}<span>{data.n > 1 ? "</p>" : `<script>${data.n}`}</span>{/if}
<script module>
  import { later } from "./later.js";
</script>
'''


def line_in(source: str, text: str) -> int:
    return source.splitlines().index(text) + 1


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_vue_script_blocks_are_analyzed_at_their_own_lines(monkeypatch, tree_sitter):
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    facts = js_lang.analyze("src/components/TodoList.vue", VUE_SFC)
    assert (facts.language, facts.lines, facts.parse_error) == ("typescript", len(VUE_SFC.splitlines()), False)
    assert facts.frameworks == {"vue"}  # the file is a Vue component, whatever it imports
    # <script> and <script setup> both count; "<script" in the comment, the template and the style does not
    assert [(i.kind, i.spec, i.line) for i in facts.imports] == [
        ("import", "vue", line_in(VUE_SFC, 'import { defineComponent } from "vue"')),
        ("import", "./TodoItem.vue", line_in(VUE_SFC, 'import TodoItem from "./TodoItem.vue"')),
        ("import", "@/api", line_in(VUE_SFC, 'import { api } from "@/api"')),
    ]
    remove = line_in(VUE_SFC, "async function remove(id: number) {")
    assert [(s.name, s.line, s.hint) for s in facts.symbols] == [("remove", remove, "DELETE · /api/todos/*")]
    assert [(c.method, c.path, c.line) for c in facts.http_calls] == [("DELETE", "/api/todos/*", remove + 1)]
    assert facts.module_doc == "The todo list screen part"


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_svelte_script_blocks_are_analyzed_and_markup_is_not(monkeypatch, tree_sitter):
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    facts = js_lang.analyze("src/routes/+page.svelte", SVELTE_SFC)
    assert facts.language == "typescript" and facts.frameworks == {"svelte"} and not facts.parse_error
    # the module script (context="module" and Svelte 5's <script module>) and the instance script;
    # not the <script> inside <svelte:head>, nor "<script" in { } expressions and attributes
    assert [(i.spec, i.line) for i in facts.imports] == [
        ("$lib/Nav.svelte", line_in(SVELTE_SFC, '  import Nav from "$lib/Nav.svelte";')),
        ("./later.js", line_in(SVELTE_SFC, '  import { later } from "./later.js";')),
    ]
    save = line_in(SVELTE_SFC, "  function save(title: string) {")
    assert [(s.name, s.line, s.hint) for s in facts.symbols] == [("save", save, "POST · /api/todos")]


def test_sfc_files_without_scripts_and_with_odd_blocks():
    static = js_lang.analyze("src/Static.vue", "<template>\n  <p>static</p>\n</template>\n<style>p { }</style>\n")
    assert (static.language, static.lines, static.imports, static.symbols) == ("javascript", 4, [], [])
    assert static.frameworks == {"vue"} and not static.parse_error
    assert js_lang.analyze("src/Empty.svelte", "").frameworks == {"svelte"}
    # a Pug template is raw text; other script languages and JSON data are not code to read
    pug = js_lang.analyze("src/P.vue", '<template lang="pug">\n  div(title="<script>")\n</template>\n'
                                       '<script lang="coffee">\nimport X from "./X.vue"\n</script>\n'
                                       '<script type="application/ld+json">{"a": 1}</script>\n'
                                       '<script>\nimport Y from "./Y.vue"\n</script>\n')
    assert [(i.spec, i.line) for i in pug.imports] == [("./Y.vue", 9)]
    # Vue's custom blocks are raw text too: "<script>" in an <i18n> block starts nothing
    i18n = js_lang.analyze("src/I.vue", '<i18n>{ "en": { "tag": "<script>" } }</i18n>\n'
                                        '<script setup>\nimport Z from "./Z.vue"\n</script>\n')
    assert [(i.spec, i.line) for i in i18n.imports] == [("./Z.vue", 3)]
    # Vue blocks from other files: <script src>, <template src> and <style src> are imports
    split = js_lang.analyze("src/Split.vue", '<template src="./split.html"></template>\n'
                                             '<script src="./split.js"></script>\n<style src="./split.css" />\n')
    assert [(i.kind, i.spec, i.line) for i in split.imports] == [
        ("side-effect", "./split.html", 1), ("side-effect", "./split.js", 2), ("side-effect", "./split.css", 3)]
    if js_lang.TREE_SITTER:  # two blocks on one line stay two statements
        one = js_lang.analyze("src/One.vue", '<script>import A from "./A.vue"</script><script setup>'
                                             'import B from "./B.vue"</script><template><A /></template>')
        assert [i.spec for i in one.imports] == ["./A.vue", "./B.vue"] and not one.parse_error


def test_a_vue_file_whose_blocks_cannot_be_read_says_so(monkeypatch):
    def broken(source, ext):
        raise ValueError("odd markup")
    monkeypatch.setattr(js_lang, "_sfc_code", broken)
    facts = js_lang.analyze("src/Odd.vue", '<script setup>\nimport A from "./A.vue"\n</script>\n')
    assert facts.parse_error and facts.frameworks == {"vue"} and facts.imports == [] and facts.lines == 3


def test_broken_markup_takes_no_time():
    import time

    svelte = "<script>\nimport A from './A.svelte'\n</script>\n" + "<p title=\"{" * 20_000 + "{" * 20_000 + "\n"
    vue = ("<template><div>" + "<p>{{ a " * 20_000 + "</i>" * 20_000 + "</div></template>\n"
           "<script>\nimport B from './B.vue'\n</script>\n")
    start = time.perf_counter()
    x, y = js_lang.analyze("a/X.svelte", svelte), js_lang.analyze("a/Y.vue", vue)
    assert time.perf_counter() - start < 3  # each unclosed { or {{ would otherwise read to the end again
    assert ([i.spec for i in x.imports], x.frameworks) == (["./A.svelte"], {"svelte"})
    assert ([i.spec for i in y.imports], y.frameworks) == (["./B.vue"], {"vue"})


def test_broken_markup_is_read_a_few_times_at_most(monkeypatch):
    # 2026-11-21: the scanner's budget counted steps, and a step reads up to the next brace, so an
    # unclosed { every few hundred characters made a 1 MB file take seconds; now it counts characters
    read = [0]
    real = js_lang._RX_JS_TOKEN

    class Counting:
        def search(self, text, pos):
            m = real.search(text, pos)
            read[0] += (m.start() if m else len(text)) - pos
            return m

    monkeypatch.setattr(js_lang, "_RX_JS_TOKEN", Counting())
    svelte = "<script>\nimport A from './A.svelte'\n</script>\n" + ("{" + "a" * 199) * 1_500 + "\n"
    facts = js_lang.analyze("a/X.svelte", svelte)
    assert [i.spec for i in facts.imports] == ["./A.svelte"]
    assert read[0] < 8 * len(svelte)


def test_comments_that_never_end_in_markup_take_no_time():
    import time

    # each "{/*" looked for its "*/" to the end of the file again: 240 KB took 5 to 10 seconds, 1 MB minutes
    svelte = "<script>\nimport A from './A.svelte'\n</script>\n" + "{/*" * 80_000
    start = time.perf_counter()
    facts = js_lang.analyze("a/X.svelte", svelte)
    assert time.perf_counter() - start < 2
    assert [i.spec for i in facts.imports] == ["./A.svelte"]


def test_template_literals_nested_without_end_keep_the_script():
    # 2026-11-21: each `${ inside a markup expression went one call deeper, and a few hundred of them
    # ran out of Python's recursion limit: the file lost its script
    svelte = "<script>\nimport A from './A.svelte'\n</script>\n" + "<p>{`${" * 2_000 + "\n"
    facts = js_lang.analyze("a/X.svelte", svelte)
    assert [i.spec for i in facts.imports] == ["./A.svelte"] and facts.frameworks == {"svelte"}


def test_vue_options_api_methods_are_listed():
    src = ('<script>\nexport default {\n  name: "TodoItem",\n  methods: {\n'
           '    /** Removes the todo. */\n    remove(id) {\n      return fetch(`/api/todos/${id}`, { method: "DELETE" })\n    },\n'
           '    rename: function (title) {},\n  },\n}\n</script>\n')
    facts = js_lang.analyze("src/TodoItem.vue", src)
    if js_lang.TREE_SITTER:
        assert [(s.signature, s.line, s.doc, s.hint) for s in facts.symbols] == [
            ("remove(id)", 6, "Removes the todo.", "DELETE · /api/todos/*"), ("rename(title)", 9, "", "")]
    # a plain .js file's export default object is not a component's options
    assert js_lang.analyze("src/config.js", "export default { methods: { a() {} } }\n").symbols == []


def test_nuxt_and_sveltekit_are_frameworks():
    assert js_lang.analyze("nuxt.config.ts", "export default defineNuxtConfig({})\n").frameworks == {"nuxt"}
    assert js_lang.analyze("app/Next.config.js", "module.exports = {}\n").frameworks == {"next"}
    assert js_lang.analyze("plugins/x.ts", 'import { defineNuxtPlugin } from "#app"\n').frameworks == {"nuxt"}
    assert js_lang.analyze("svelte.config.js", 'import adapter from "@sveltejs/adapter-node";\n'
                                               "export default { kit: { adapter: adapter() } };\n").frameworks == {"sveltekit"}
    kit = js_lang.analyze("src/routes/+page.svelte", '<script>\n  import { page } from "$app/stores";\n</script>\n')
    assert kit.frameworks == {"svelte", "sveltekit"}
    assert js_lang.analyze("vite.config.js", 'import { svelte } from "@sveltejs/vite-plugin-svelte";\n').frameworks == set()


def test_use_fetch_in_vue_code_is_a_request():
    page = js_lang.analyze("pages/index.vue", "<script setup>\nconst { data } = await useFetch('/api/todos')\n"
                                              "await useLazyFetch('/api/todos', { method: 'POST' })\n</script>\n")
    assert [(c.method, c.path, c.line) for c in page.http_calls] == [("GET", "/api/todos", 2), ("POST", "/api/todos", 3)]
    # a React app's own useFetch hook is not taken for one
    assert js_lang.analyze("src/App.jsx", "const { data } = useFetch('/api/todos');\n").http_calls == []


def test_sveltekit_and_nuxt_server_files_are_routes():
    kit = js_lang.analyze("src/routes/api/todos/[id]/+server.ts",
                          "export async function GET() {}\nexport const DELETE = async () => {};\n"
                          "export const fallback = () => {};\n")
    assert {(r.method, r.path) for r in kit.http_routes} == {("GET", "/api/todos/*"), ("DELETE", "/api/todos/*"),
                                                             ("ANY", "/api/todos/*")}
    root = js_lang.analyze("src/routes/(app)/+server.js", "export function POST() {}\n")
    assert [(r.method, r.path) for r in root.http_routes] == [("POST", "/")]
    nuxt = js_lang.analyze("server/api/todos/[id].delete.ts", "export default defineEventHandler(() => ({}))\n")
    assert [(r.method, r.path) for r in nuxt.http_routes] == [("DELETE", "/api/todos/*")]
    index = js_lang.analyze("server/routes/health/index.ts", "export default eventHandler(() => 'ok')\n")
    assert [(r.method, r.path) for r in index.http_routes] == [("ANY", "/health")]
    # an Express project's server/api folder is not Nuxt's: no defineEventHandler, no route from the path
    assert js_lang.analyze("server/api/todos.js", "module.exports = (req, res) => res.json([]);\n").http_routes == []


# --- module names built at run time and globs (2026-11-20)

PATTERN_JS = '''\
const pages = import.meta.glob("./pages/**/*.vue");
const some = import.meta.glob(["./a/*.js", "!./a/skip.js"], { eager: true });
const icons = require.context("./icons", false, /\\.svg$/i);
const locales = require.context("../locales");
const views = import.meta.webpackContext("./views", { recursive: false, regExp: /\\.vue$/ });
export const load = (lang) => import(`./locales/${lang}.json`);
const plugin = require("./plugins/" + name);
const DIR = "./fixed";
const fixed = require(DIR + "/one");
const unknown = require(name);
const anyFile = import(`./${name}`);
const fromPackage = import(`lodash/${fn}`);
const byConfig = require(config.plugins[0]);
'''


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_globs_and_built_module_names_are_patterns(monkeypatch, tree_sitter):
    from codevision.model import is_pattern

    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    facts = js_lang.analyze("src/app.js", PATTERN_JS)
    assert [(i.kind, i.spec, i.names, i.line) for i in facts.imports] == [
        ("glob", "./pages/**/*.vue", [], 1),
        ("glob", "./a/*.js", ["!./a/skip.js"], 2),
        ("glob", "./icons/*", ["/\\.svg$/i"], 3),
        ("glob", "../locales/**", [], 4),
        ("glob", "./views/*", ["/\\.vue$/"], 5),
        ("dynamic", "./locales/*.json", [], 6),
        ("require", "./plugins/*", [], 7),  # no names: which module the variable holds is not known
        ("require", "./fixed/one", ["fixed"], 9),  # every part a known constant: an ordinary require
    ]  # require(name), import(`./${name}`), a package's modules and a name from config: nothing to know
    assert all(is_pattern(i) for i in facts.imports[:7]) and not is_pattern(facts.imports[7])
    assert facts.frameworks == set()


def pattern_index(root):
    return write_project(root, {
        "package.json": "{}", "src/main.js": "", "outside.js": "",
        "src/pages/Home.vue": "", "src/pages/About.vue": "", "src/pages/admin/Users.vue": "",
        "src/pages/notes.md": "", "src/pages/.Draft.vue": "",
        "src/plugins/a.js": "", "src/plugins/b.ts": "", "src/plugins/c/index.js": "", "src/plugins/readme.md": "",
        "src/plugins/types.d.ts": "", "src/plugins/deep/x/y.js": "",
        "src/locales/en.json": "", "src/locales/ko.json": "", "src/locales/sub/x.json": "",
        "src/icons/a.svg": "", "src/icons/B.SVG": "", "src/icons/deep/c.svg": "", "src/icons/d.png": "",
        "src/views/List.ts": "", "src/views/List.vue": "",
    })


def test_pattern_imports_point_to_the_files_they_match(tmp_path):
    index = pattern_index(tmp_path)

    def hits(spec, kind="glob", names=(), importer="src/main.js"):
        return js_lang.resolve(ImportRef(spec, list(names), kind), importer, index)

    about, home, users = "src/pages/About.vue", "src/pages/Home.vue", "src/pages/admin/Users.vue"
    assert hits("./pages/*.vue") == [about, home]  # one folder; dotfiles left out like Vite does
    assert hits("./pages/**/*.vue") == [about, users, home]
    assert hits("./pages/**/*.vue", names=["!./pages/admin/**"]) == [about, home]
    assert hits("/src/pages/{Home,About}.vue") == [about, home]  # from the package root
    assert hits("@/pages/[A-H]*.vue") == [about, home]  # an alias (here @/ -> src/)
    assert hits("./pages/*.vue", names=["./locales/*.json"]) == ["src/locales/en.json", "src/locales/ko.json",
                                                                  about, home]  # every pattern of the call, sorted
    # require.context: the folder, all the way down or not, and the regular expression on "./path"
    assert hits("./icons/*", names=["/\\.svg$/i"]) == ["src/icons/a.svg", "src/icons/B.SVG"]
    assert hits("./icons/**", names=["/\\.svg$/"]) == ["src/icons/a.svg", "src/icons/deep/c.svg"]
    assert hits("./icons/**", names=["/(?<=x)y/"]) == []  # a regular expression this cannot read: no links
    # a name built at run time: one name for each ${}, with the extensions and index files of a bundler
    assert hits("./locales/*.json", "dynamic") == ["src/locales/en.json", "src/locales/ko.json"]
    assert hits("./plugins/*", "require") == ["src/plugins/a.js", "src/plugins/b.ts", "src/plugins/c/index.js"]
    assert hits("./views/*.js", "dynamic") == ["src/views/List.ts"]  # import(`./views/${v}.js`) of TypeScript
    # never outside the project, and never a package's files
    assert hits("../*.js") == ["outside.js"]
    assert hits("../../*.js") == [] and hits("../../../x/*", "require") == []
    assert hits("lodash/*", "dynamic") == [] and hits("*.js") == []


def test_hostile_patterns_cost_no_time(tmp_path):
    # Written out plainly, each of these took minutes on one long name in Python's re, which holds the GIL:
    # a repo's own JS file could stop the whole server while it was analyzed
    deep = "/".join(["a"] * 30)
    index = write_project(tmp_path, {
        "package.json": "{}", "src/main.js": "", f"src/{'a' * 60}.js": "", f"src/{'a' * 12}b": "",
        f"src/{deep}/x.b": "", f"src/{deep}/y.c": "", "src/p/aaab.js": "",
    })

    def hits(spec, kind="glob", names=()):
        return js_lang.resolve(ImportRef(spec, list(names), kind), "src/main.js", index)

    started = time.monotonic()
    assert hits("./" + "*a" * 12 + "*b") == [f"src/{'a' * 12}b"]
    assert hits("./" + "/".join(["**", "a"] * 12) + "/*.b") == [f"src/{deep}/x.b"]
    assert hits("./p/" + "*a" * 3 + "*b.js", "dynamic") == ["src/p/aaab.js"]  # import(`./p/${x}a${y}a...`)
    assert hits("./*", names=["/(a|aa)+$/"]) == [] and hits("./*", names=["/" + "a*" * 12 + "b/"]) == []
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("body, cheap", [
    (r"\.svg$", True), (r"\.(png|jpe?g|gif|svg)$", True), (r"Base[A-Z]\w+\.(vue|js)$", True),
    (r"^\.\/(?!.*\.spec\.).*\.tsx?$", True), (r"^\.\/((?!\/)[\s\S])+\/index\.vue$", True),
    (r"(a|aa)+$", False), (r"(a+)+", False), (r"a*a*b", False), (r"^a*a*a*b", False), (r"(\w)\1", False),
    (r"a?a?a?a?a?aaaaa", False), ("x" * 400, False),
])
def test_require_context_patterns_that_could_cost_too_much_are_not_used(body, cheap):
    assert js_lang._regex_is_cheap(body, 0) is cheap
    assert (js_lang._js_regex(f"/{body}/") is not None) is cheap


def test_a_glob_class_never_matches_a_folder_separator(tmp_path):
    index = write_project(tmp_path, {"package.json": "{}", "src/main.js": "", "src/a/b.js": "", "src/ab.js": ""})
    # [!x] used to match the "/" of a/b.js, so "*[!x]b.js" reached into a folder
    assert js_lang.resolve(ImportRef("./*[!x]b.js", [], "glob"), "src/main.js", index) == ["src/ab.js"]


def test_a_brace_group_beside_two_stars_and_dotted_parts_between_globstars_give_no_links(tmp_path):
    index = pattern_index(tmp_path)
    assert js_lang.resolve(ImportRef("./pages/*{Home,About}*.vue", [], "glob"), "src/main.js", index) == []
    assert js_lang.resolve(ImportRef("./**/.x/**/*.vue", [], "glob"), "src/main.js", index) == []
    assert js_lang.resolve(ImportRef("./pages/{Home,About}*", [], "glob"), "src/main.js", index) == [
        "src/pages/About.vue", "src/pages/Home.vue"]  # one star is fine


def test_sveltekit_lib_and_nuxt_aliases(tmp_path):
    kit = write_project(tmp_path / "kit", {
        "package.json": "{}", "tsconfig.json": '{"extends": "./.svelte-kit/tsconfig.json"}',
        "src/lib/Nav.svelte": "", "src/lib/index.ts": "", "src/lib/icons/a.svg": "", "src/routes/+page.svelte": "",
    })
    page = "src/routes/+page.svelte"
    assert resolve_spec(kit, "$lib/Nav.svelte", page) == ["src/lib/Nav.svelte"]
    assert resolve_spec(kit, "$lib", page) == ["src/lib/index.ts"]
    assert resolve_spec(kit, "$app/stores", page) == []  # SvelteKit's own modules
    assert js_lang.resolve(ImportRef("$lib/icons/*.svg", [], "glob"), page, kit) == ["src/lib/icons/a.svg"]
    nuxt = write_project(tmp_path / "nuxt", {
        "package.json": "{}", "nuxt.config.ts": "", "tsconfig.json": '{"extends": "./.nuxt/tsconfig.json"}',
        "utils/format.ts": "", "pages/index.vue": "", "components/A.vue": "",
    })
    assert resolve_spec(nuxt, "~/utils/format", "pages/index.vue") == ["utils/format.ts"]  # Nuxt 3: the package
    assert resolve_spec(nuxt, "@/components/A.vue", "pages/index.vue") == ["components/A.vue"]
    assert resolve_spec(nuxt, "~~/utils/format", "pages/index.vue") == ["utils/format.ts"]
    nuxt4 = write_project(tmp_path / "nuxt4", {
        "package.json": "{}", "nuxt.config.ts": "", "app/app.vue": "", "app/utils/format.ts": "",
    })
    assert resolve_spec(nuxt4, "~/utils/format", "app/app.vue") == ["app/utils/format.ts"]  # Nuxt 4: app/
    plain = write_project(tmp_path / "plain", {"package.json": "{}", "utils/format.ts": "", "main.js": ""})
    assert resolve_spec(plain, "~/utils/format", "main.js") == []  # no Nuxt, no src/: no guess


NUXT_FILES = {
    "package.json": '{"dependencies": {"nuxt": "^3.12.0"}}', "nuxt.config.ts": "",
    "app.vue": "", "error.vue": "", "pages/index.vue": "", "pages/todos/[id].vue": "", "pages/(admin)/users.vue": "",
    "layouts/default.vue": "", "components/AppHeader.vue": "", "components/todo/Item.vue": "",
    "composables/useTodos.ts": "", "utils/format.ts": "", "plugins/analytics.client.ts": "", "middleware/auth.ts": "",
    "server/api/todos.get.ts": "", "server/middleware/log.ts": "", "assets/main.css": "", "lib/other.ts": "",
}


def test_nuxt_files_are_started_or_loaded_by_nuxt(tmp_path):
    index = write_project(tmp_path, NUXT_FILES)
    assert js_lang.project_entries(index) == {"app.vue": "Nuxt가 처음 띄우는 화면"}
    loaded = js_lang.project_loaded(index)
    assert loaded["pages/todos/[id].vue"] == "Nuxt가 /todos/[id] 주소에 맞춰 알아서 불러와요."
    assert loaded["pages/index.vue"] == "Nuxt가 / 주소에 맞춰 알아서 불러와요."
    assert loaded["pages/(admin)/users.vue"] == "Nuxt가 /users 주소에 맞춰 알아서 불러와요."
    assert loaded["components/todo/Item.vue"] == "Nuxt가 화면에 쓰인 이름을 보고 알아서 불러와요."
    assert loaded["server/api/todos.get.ts"] == "Nuxt 서버가 알아서 불러와요."
    assert {"layouts/default.vue", "composables/useTodos.ts", "utils/format.ts", "plugins/analytics.client.ts",
            "middleware/auth.ts", "error.vue", "server/middleware/log.ts"} <= set(loaded)
    assert "lib/other.ts" not in loaded and "assets/main.css" not in loaded and "nuxt.config.ts" not in loaded
    # the same folders in a project that is not a Nuxt app say nothing
    plain = write_project(tmp_path / "plain", {k: v for k, v in NUXT_FILES.items()
                                               if k not in ("package.json", "nuxt.config.ts")})
    assert js_lang.project_loaded(plain) == {} and js_lang.project_entries(plain) == {}


def test_nuxt_4_app_folder_and_home_page_entry(tmp_path):
    index = write_project(tmp_path, {"nuxt.config.ts": "", "app/pages/index.vue": "", "app/components/A.vue": "",
                                     "server/api/x.ts": "", "pages/old.vue": ""})
    assert js_lang.project_entries(index) == {"app/pages/index.vue": "Nuxt가 처음 띄우는 화면"}  # no app.vue
    assert set(js_lang.project_loaded(index)) == {"app/pages/index.vue", "app/components/A.vue", "server/api/x.ts"}


def test_sveltekit_route_files_are_known_by_their_names(tmp_path):
    index = write_project(tmp_path, {
        "package.json": '{"devDependencies": {"@sveltejs/kit": "^2"}}',
        "src/routes/+layout.svelte": "", "src/routes/+page.svelte": "", "src/routes/+page.ts": "",
        "src/routes/+error.svelte": "", "src/routes/todos/[id]/+page.svelte": "",
        "src/routes/todos/[id]/+page.server.ts": "", "src/routes/(app)/admin/+layout@.svelte": "",
        "src/routes/api/todos/+server.ts": "", "src/routes/Helper.svelte": "", "src/routes/notes.md": "",
        "src/hooks.server.ts": "", "src/hooks.client.js": "", "src/params/integer.ts": "", "src/service-worker.ts": "",
        "src/lib/Nav.svelte": "",
    })
    assert js_lang.project_entries(index) == {"src/routes/+layout.svelte": "SvelteKit이 처음 띄우는 화면"}
    loaded = js_lang.project_loaded(index)
    assert loaded["src/routes/todos/[id]/+page.svelte"] == "SvelteKit이 /todos/[id] 주소에 맞춰 알아서 불러와요."
    assert loaded["src/routes/todos/[id]/+page.server.ts"] == "SvelteKit이 /todos/[id] 주소의 화면을 열 때 알아서 불러와요."
    assert loaded["src/routes/+layout.svelte"] == "SvelteKit이 모든 페이지에 씌울 때 알아서 불러와요."
    assert loaded["src/routes/(app)/admin/+layout@.svelte"] == "SvelteKit이 /admin 아래 페이지에 씌울 때 알아서 불러와요."
    assert loaded["src/routes/api/todos/+server.ts"] == "SvelteKit이 /api/todos 주소로 오는 요청에 알아서 불러와요."
    assert loaded["src/routes/+error.svelte"] == "문제가 생기면 SvelteKit이 알아서 띄워요."
    assert {"src/hooks.server.ts", "src/hooks.client.js", "src/params/integer.ts", "src/service-worker.ts"} <= set(loaded)
    assert not {"src/routes/Helper.svelte", "src/lib/Nav.svelte", "src/routes/notes.md"} & set(loaded)
    # with no root layout, the root page starts the app
    page_only = write_project(tmp_path / "p", {"src/routes/+page.svelte": "", "src/routes/a/+page.svelte": ""})
    assert js_lang.project_entries(page_only) == {"src/routes/+page.svelte": "SvelteKit이 처음 띄우는 화면"}


def test_next_files_are_started_or_loaded_by_next(tmp_path):
    index = write_project(tmp_path, {
        "package.json": '{"dependencies": {"next": "14", "react": "18"}}',
        "src/app/layout.tsx": "", "src/app/page.tsx": "", "src/app/(shop)/cart/page.tsx": "",
        "src/app/@modal/login/page.tsx": "", "src/app/api/todos/route.ts": "", "src/app/cart/Button.tsx": "",
        "pages/_app.tsx": "", "pages/about.tsx": "", "pages/api/hello.ts": "", "pages/types.d.ts": "",
        "middleware.ts": "", "components/Header.tsx": "",
    })
    assert js_lang.project_entries(index) == {"pages/_app.tsx": "Next.js가 처음 띄우는 화면",
                                              "src/app/layout.tsx": "Next.js가 처음 띄우는 화면"}
    loaded = js_lang.project_loaded(index)
    assert loaded["src/app/(shop)/cart/page.tsx"] == "Next.js가 /cart 주소에 맞춰 알아서 불러와요."
    assert loaded["src/app/@modal/login/page.tsx"] == "Next.js가 /login 주소에 맞춰 알아서 불러와요."
    assert loaded["pages/about.tsx"] == "Next.js가 /about 주소에 맞춰 알아서 불러와요."
    assert loaded["middleware.ts"] == "Next.js가 요청마다 알아서 불러와요."
    assert {"src/app/page.tsx", "src/app/api/todos/route.ts", "pages/api/hello.ts", "pages/_app.tsx"} <= set(loaded)
    assert not {"src/app/cart/Button.tsx", "components/Header.tsx", "pages/types.d.ts"} & set(loaded)
    home = write_project(tmp_path / "home", {"next.config.js": "", "pages/index.js": "", "pages/a.js": ""})
    assert js_lang.project_entries(home) == {"pages/index.js": "Next.js가 처음 띄우는 화면"}


# --- request helpers, routers and Fastify plugins that other files use (2026-11-21)

def use_tree_sitter(monkeypatch, tree_sitter):
    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)


HELPER_LIB = '''\
import axios from "axios";
const BASE = "http://localhost:8000";
export const api = (path, opts) => fetch(BASE + path, opts);
export async function request(url: string, method = "GET") {
  const res = await fetch(`/api${url}`, { method });
  return res.json();
}
export const http = axios.create({ baseURL: "/api" });
export const endpoints = {
  get: (path) => fetch(BASE + path),
  post(path, body) { return fetch(BASE + path, { method: "POST", body }); },
};
async function hidden(path) { return fetch(path); }
export default async (url) => fetch(url, { method: "DELETE" });
'''


def test_exported_request_helpers_say_how_they_send():
    if not js_lang.TREE_SITTER:
        pytest.skip("request helpers are found with tree-sitter only")
    facts = js_lang.analyze("web/lib/api.ts", HELPER_LIB)
    helpers = {h.name: (h.base, h.url_at, h.method, h.method_at, h.method_default, h.options_at, h.client)
               for h in facts.http_helpers}
    assert helpers == {
        "api": ("http://localhost:8000", 0, "ANY", None, "", 1, False),  # the method comes with the options
        "request": ("/api", 0, "ANY", 1, "GET", None, False),  # a method argument, GET when it is left out
        "http": ("/api", 0, "GET", None, "", None, True),  # an axios client: http.get(path)
        "endpoints.get": ("http://localhost:8000", 0, "GET", None, "", None, False),
        "endpoints.post": ("http://localhost:8000", 0, "POST", None, "", None, False),
        "default": ("", 0, "DELETE", None, "", None, False),
    }  # hidden() is no export
    assert {(e.name, e.local) for e in facts.exports} == {
        ("api", "api"), ("request", "request"), ("http", "http"), ("endpoints", "endpoints"), ("default", "default")}
    assert facts.http_calls == []  # a helper's own fetch(BASE + path) asks for no address


def test_calls_of_helpers_from_other_files_are_kept_for_the_graph():
    if not js_lang.TREE_SITTER:
        pytest.skip("request helpers are found with tree-sitter only")
    facts = js_lang.analyze("web/TodoList.jsx", '''\
import { api as send } from "./lib/api";
import http from "./lib/http";
import * as lib from "./lib";
const { request } = require("./lib/request");
import path from "path";
export async function load() { return send("/todos", { method: "POST" }); }
export async function remove(id) { return http.delete(`/todos/${id}`); }
export function save() { return lib.request("/notes", "PUT"); }
export function later() { return request("/later"); }
format("/not-imported");
send(someUrl);
path.join("/static", "x");
''')
    assert [(c.target.spec, c.name, c.member, c.args, c.options, c.symbols) for c in facts.helper_calls] == [
        ("./lib/api", "api", "", ["/todos", None], [None, "POST"], ["load"]),  # its name in the module, not "send"
        ("./lib/http", "default", "delete", ["/todos/${}"], [None], ["remove"]),
        ("./lib", "request", "", ["/notes", "PUT"], [None, None], ["save"]),  # import * as lib: the module's request
        ("./lib/request", "request", "", ["/later"], [None], ["later"]),
    ]  # send(someUrl) gives no address, and Node's path module makes no requests
    guess = facts.helper_calls[1].guess  # http.get-style calls were already read as requests, without a base address
    assert (guess.method, guess.path) == ("DELETE", "/todos/*") and facts.http_calls == [guess]


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_mounts_name_the_router_they_take(monkeypatch, tree_sitter):
    use_tree_sitter(monkeypatch, tree_sitter)
    app = js_lang.analyze("server/app.js", '''\
const express = require("express");
import { todos, users as people } from "./routes";
import * as admin from "./admin";
const legacy = require("./legacy");
const make = require("./make");
const app = express();
const api = express.Router();
api.use("/todos", todos);
app.use("/people", people);
app.use("/admin", admin.panel);
app.use("/old", legacy);
app.use("/v1", require("./v1").router);
app.use("/made", make(db));
app.use("/api", api);
''')
    assert [(m.prefix, m.target.spec, m.router, m.owner, m.base) for m in app.http_mounts] == [
        ("/todos", "./routes", "todos", "app", "/api"),  # on api, which answers under /api on the app
        ("/people", "./routes", "users", "app", ""),  # the name in the module, not the local one
        ("/admin", "./admin", "panel", "app", ""),
        ("/old", "./legacy", "default", "app", ""),  # module.exports
        ("/v1", "./v1", "router", "app", ""),
        ("/made", "./make", "", "app", ""),  # what a factory makes: the whole module
    ]
    routes = js_lang.analyze("server/routes.js", '''\
const express = require("express");
const todos = express.Router();
const users = express.Router();
const api = express.Router();
todos.get("/", list);
users.get("/items", items);
api.use("/users", users);
module.exports = { todos, api };
''')
    assert [(r.method, r.path, r.router) for r in routes.http_routes] == [
        ("GET", "/", "todos"), ("GET", "/users/items", "api")]  # users answers under api, which other files mount
    assert {(e.name, e.local) for e in routes.exports} == {("todos", "todos"), ("api", "api")}


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_exports_pass_routers_of_other_modules_on(monkeypatch, tree_sitter):
    use_tree_sitter(monkeypatch, tree_sitter)
    es = js_lang.analyze("routes/index.js", 'export { default as todos } from "./todos";\nexport * from "./users";\n')
    assert {(e.name, e.local, e.source.spec, e.original) for e in es.exports} == {
        ("todos", "", "./todos", "default"), ("*", "", "./users", "*")}
    cjs = js_lang.analyze("routes/index.js", 'const users = require("./users");\n'
                                             'module.exports = { todos: require("./todos"), users };\n')
    assert {(e.name, e.local, e.source.spec, e.original) for e in cjs.exports} == {
        ("todos", "", "./todos", "default"), ("users", "", "./users", "default")}


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_a_fastify_plugin_in_a_file_of_its_own_gives_its_routes(monkeypatch, tree_sitter):
    use_tree_sitter(monkeypatch, tree_sitter)
    plugin = js_lang.analyze("routes/todos.js", '''\
module.exports = async function (fastify, opts) {
  fastify.get("/todos", async () => []);
  fastify.register(async (f) => {
    f.post("/stats", async () => ({}));
  }, { prefix: "/admin" });
  fastify.register(require("./more"), { prefix: "/more" });
};
''')
    # no framework import: they count once the graph sees the project use Fastify
    assert plugin.http_routes == [] and plugin.http_mounts == []
    assert [(r.method, r.path, r.router) for r in plugin.plugin_routes] == [
        ("GET", "/todos", "default"), ("POST", "/admin/stats", "default")]
    assert [(m.prefix, m.target.spec, m.router, m.owner) for m in plugin.plugin_mounts] == [
        ("/more", "./more", "default", "default")]
    assert [(e.name, e.local) for e in plugin.exports] == [("default", "default")]
    named = js_lang.analyze("routes/users.mjs", "export default async function routes(fastify) {\n"
                                                "  fastify.get('/', async () => []);\n}\n")
    assert [(r.path, r.router) for r in named.plugin_routes] == [("/", "routes")]
    assert [(e.name, e.local) for e in named.exports] == [("default", "routes")]
    # no evidence: a first parameter that is no Fastify instance, a function the file does not export
    other = js_lang.analyze("lib/util.js", "module.exports = function (map) {\n  map.get('/key', 1);\n};\n"
                                           "function setup(fastify) {\n  fastify.get('/hidden', async () => 1);\n}\n")
    assert other.http_routes == other.plugin_routes == other.exports == []
    # a parameter with a router's name counted before, and still does; fastify-plugin is Fastify
    app = js_lang.analyze("routes/app.js", "module.exports = function (app) {\n"
                                           "  app.get('/todos', (req, res) => res.json([]));\n};\n")
    assert [(r.path, r.router) for r in app.http_routes] == [("/todos", "default")] and app.plugin_routes == []
    wrapped = js_lang.analyze("plugins/x.js", "const fp = require('fastify-plugin');\n"
                                              "module.exports = fp(async (fastify) => {\n  fastify.get('/x', async () => 1);\n});\n")
    assert wrapped.frameworks == {"fastify"} and [(r.path, r.router) for r in wrapped.http_routes] == [("/x", "default")]


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_functions_that_take_a_client_or_a_cache_are_no_fastify_plugins(monkeypatch, tree_sitter):
    # 2026-11-21: http.get("/todos", options) in makeTodoApi(http) was taken for a plugin's route, so in
    # a project that uses Fastify the requests turned into routes
    use_tree_sitter(monkeypatch, tree_sitter)
    api = js_lang.analyze("web/todoApi.js", '''\
module.exports = function makeTodoApi(http) {
  return {
    list: () => http.get("/todos", options),
    add: (todo) => http.post("/todos", todo),
  };
};
const options = { timeout: 1000 };
''')
    assert api.plugin_routes == api.http_routes == [] and api.plugin_mounts == []
    assert [(c.method, c.path) for c in api.http_calls] == [("GET", "/todos"), ("POST", "/todos")]  # as before
    cache = js_lang.analyze("web/warm.js", "export default function warm(cache) {\n  cache.get('/settings', load);\n}\n"
                                           "function load() { return {}; }\n")
    assert cache.plugin_routes == cache.http_routes == cache.exports == []
    # a parameter named like a Fastify instance may give its routes a handler by name
    named = js_lang.analyze("routes/items.js", "const { getItems } = require('../controllers/items');\n"
                                               "module.exports = async function (fastify) {\n"
                                               "  fastify.get('/items', getItems);\n};\n")
    assert [(r.path, r.router) for r in named.plugin_routes] == [("/items", "default")]


# --- what a class extends (2026-11-21): describe lists the error classes after the file's other names

@pytest.mark.parametrize("tree_sitter", [True, False])
def test_class_bases_are_recorded_from_extends(monkeypatch, tree_sitter):
    # 2026-11-21: the JavaScript analyzer filled no bases, so class NotFound extends HttpError stayed among
    # the main names. Like Python's: the last part of each name; one made by a call or an index is none.
    use_tree_sitter(monkeypatch, tree_sitter)
    js = js_lang.analyze("src/errors.js", "export class HttpError extends Error {}\n"
                                          "export class NotFound extends errors.HttpError {}\n"
                                          "export default class Page\n  extends React.Component {}\n"
                                          "class Mixed extends mixin(Base) {}\n"
                                          "class Picked extends parts[0] {}\n"
                                          "class Plain {}\n")
    assert [(s.name, s.bases) for s in js.symbols] == [
        ("HttpError", ("Error",)), ("NotFound", ("HttpError",)), ("Page", ("Component",)), ("Mixed", ()),
        ("Picked", ()), ("Plain", ())]
    ts = js_lang.analyze("src/errors.ts", "export abstract class AppError extends Error {}\n"
                                          "export class Gone<T extends Base<U>> extends AppError<T> implements Problem {}\n"
                                          "export class Keyed<T extends { id: string }> extends Base {}\n"
                                          "export class Client implements Api {}\n"
                                          "export class Box<T> {}\n")
    assert [(s.name, s.bases) for s in ts.symbols] == [
        ("AppError", ("Error",)), ("Gone", ("AppError",)), ("Keyed", ("Base",)), ("Client", ()), ("Box", ())]
    tsx = js_lang.analyze("src/Boundary.tsx", "export class Boundary extends React.Component<Props> {\n"
                                              "  render() { return <div />; }\n}\n")
    assert [(s.name, s.kind, s.bases) for s in tsx.symbols] == [("Boundary", "component", ("Component",))]


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_a_default_class_without_a_name_is_no_symbol(monkeypatch, tree_sitter):
    # 2026-11-21: the regular expressions took the word after "class" for its name, so the panel listed
    # export default class extends Error {} as "class extends"
    use_tree_sitter(monkeypatch, tree_sitter)
    facts = js_lang.analyze("src/errors.js", "export default class extends Error {}\nexport class Named {}\n")
    assert [s.name for s in facts.symbols] == ["Named"]
