"""Graph builder: scan + analyzers -> the graph JSON of docs/architecture.md section 4.

The rules (areas, entries, orphans, HTTP links, focus) are in section 5, with these additions:
- files a web page loads (<script src>, <link href>) are connections like imports, and the
  JavaScript a page runs is screen code (ui) even without a screen framework;
- routers mounted under a prefix (app.use("/api", router), include_router(..., prefix=...))
  answer under it before HTTP calls are matched; a mount that names one router of a file
  (include_router(routes.todos_router)) moves only that router's routes, following the names
  JavaScript files pass on (export { default as todos } from "./todos");
- calls of request helpers made in other files (api("/todos"), http.get("/todos") with http an
  axios.create() client) are HTTP calls of the calling file, at the address the helper builds;
- Fastify plugins in files that import no framework keep their routes when the project uses Fastify;
- a Python file whose name no import can spell (__pip-runner__.py) is never a cleanup candidate;
- an import that names its modules by a pattern (import.meta.glob("./pages/*.vue"),
  import_module(f"pkg.plugins.{name}")) links to at most PATTERN_LIMIT of the files it matches,
  and the files past the limit are not cleanup candidates either;
- files a framework loads by itself (an analyzer's optional project_loaded(): Nuxt pages and
  components, SvelteKit's +page.svelte) are never cleanup candidates, and say so in `d`;
- the stack, the legend's server label and the UI fallback come from the project's own code,
  not from example, demo, fixture or test folders;
- focus: the project's own code first, then examples and tests, then vendored code.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import sys
import time
from collections import Counter

from . import describe, lang
from .describe import DB_FRAMEWORKS, LANGUAGE_NAMES, SERVER_FRAMEWORKS, UI_FRAMEWORKS, WEB_SERVERS
from .model import (
    FileFacts,
    HttpCall,
    HttpRoute,
    helper_request,
    http_methods_match,
    http_paths_match,
    is_pattern,
    join_http_path,
    join_rel,
    normalize_http_path,
    parent_dir,
    strip_http_base,
)
from .scan import MAX_DEPTH, scan

log = logging.getLogger(__name__)

AI_DEFAULT = {"provider": "none", "available": False, "message": "AI 설명이 아직 연결되지 않았어요."}
# Files one pattern import links to at most: import.meta.glob("./**/*.vue") in a big app, or pip's
# import_module(f"pip._vendor.{name}"), would otherwise tie one file to hundreds.
PATTERN_LIMIT = 50
# What the panel says about a file that a capped pattern matched but that has no line drawn to it.
PATTERN_MORE = "다른 파일이 이름 규칙으로 한꺼번에 불러오는 파일 중 하나예요. 맞는 파일이 너무 많아서 연결선은 일부만 그렸어요."

PY_EXTS = frozenset({".py", ".pyw", ".pyi"})
JS_EXTS = frozenset({".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"})
TS_EXTS = frozenset({".ts", ".tsx", ".mts", ".cts"})
UI_EXTS = frozenset({".jsx", ".tsx", ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".sass", ".less"})
NOT_ORPHAN_NAMES = frozenset({"__init__.py", "setup.py", "conftest.py", "manage.py"})
MIGRATION_DIRS = frozenset({"migrations", "alembic"})  # Django and Alembic load these files by themselves
README_NAMES = ("readme.md", "readme.markdown", "readme.rst", "readme.txt", "readme")
# Code copied in from other projects (pip/_vendor/rich): not where the project's own story starts.
VENDOR_DIRS = frozenset({"vendor", "_vendor", "vendors", "vendored", "_vendored", "third_party", "third-party",
                         "thirdparty", "3rdparty", "extern", "external", "externals"})
# Folders whose code shows how something is used rather than what the project is made of: the
# libraries they import do not describe the project (CodeVision's own demo app uses React and FastAPI).
EXAMPLE_DIRS = frozenset({"example", "examples", "demo", "demos", "sample", "samples", "fixture", "fixtures",
                          "test", "tests", "__tests__", "spec", "e2e", "testdata", "test_data", "mocks", "__mocks__"})
# "GET · /todos", "GET, POST · /todos", "GET · /v1/todos, /v2/todos" (a router mounted twice)
_ROUTE_HINT = re.compile(
    r"^((?:GET|POST|PUT|PATCH|DELETE|ANY)(?:, (?:GET|POST|PUT|PATCH|DELETE|ANY))*) · (/\S*(?:, /\S*)*)$")

# package.json dependencies that mark a package as screen (ui) or server (sv) code
UI_DEPS = frozenset({"react", "react-dom", "vue", "svelte", "next", "@angular/core", "preact", "solid-js",
                     "nuxt", "@sveltejs/kit", "vite"})
SERVER_DEPS = frozenset({"express", "koa", "fastify", "@nestjs/core", "hono", "@hapi/hapi", "restify"})

# Raw package names an analyzer might report instead of the canonical name.
ALIASES = {
    "react-dom": "react", "@angular/core": "angular", "@nestjs/core": "nestjs",
    "sqlite3": "sqlite", "better-sqlite3": "sqlite", "aiosqlite": "sqlite",
    "postgresql": "postgres", "psycopg": "postgres", "psycopg2": "postgres", "asyncpg": "postgres", "pg": "postgres",
    "pymysql": "mysql", "mysql2": "mysql", "mysqlclient": "mysql",
    "pymongo": "mongodb", "motor": "mongodb", "mongoose": "mongodb",
    "@prisma/client": "prisma", "@supabase/supabase-js": "supabase", "firebase-admin": "firebase", "ioredis": "redis",
}
DOTTED_NAMES = frozenset({"http.server"})  # canonical names that keep their dot
# ORMs whose database is only visible in a connection URL or setting.
DB_HINT_FRAMEWORKS = frozenset({"sqlalchemy", "sqlmodel", "sequelize", "typeorm", "knex", "django", "peewee",
                                "tortoise", "databases"})
_DB_HINT = re.compile(
    r"\b(sqlite3?|postgres(?:ql)?|mysql|mariadb|mongodb)(?:\+\w+)?:/"
    r"|dialect\s*[:=]\s*['\"](sqlite|postgres|mysql|mariadb)['\"]"
    r"|django\.db\.backends\.(sqlite3|postgresql|mysql)",
    re.IGNORECASE,
)
# Every _DB_HINT match holds one of these words. Looking for them first is many times faster than
# the regex on code that names no database (each model file of a Django project).
_DB_WORDS = ("sqlite", "postgres", "mysql", "mariadb", "mongodb")
# Bytes of code text that build step 1 holds until the step is over. Letting go of each text the
# moment its file is analyzed costs about 1% of a build (the heap takes the memory back and hands
# it out again for the next file); past this much, texts are let go at once, so 300 big files
# never add up to hundreds of MB.
TEXT_HOLD = 16_000_000
ANALYZER_LABELS = {
    "codevision.lang.python_lang": ("Python", "Python 파일"),
    "codevision.lang.js_lang": ("JavaScript/TypeScript", "JavaScript·TypeScript 파일"),
}


def _ci(path: str) -> tuple[str, str]:
    return (path.lower(), path)


def _name_key(path: str) -> tuple[str, str]:
    return _ci(describe.base_name(path))


def _in_dirs(rel: str, names: frozenset) -> bool:
    return any(part in names for part in rel.lower().split("/")[:-1])


def _by_path_only(rel: str, facts: FileFacts | None) -> bool:
    """A Python file no import statement can name (__pip-runner__.py, unicode15-0-0.py): it is run
    or loaded by its path, so nobody importing it says nothing about whether it is used."""
    if facts is None or facts.language != "python":
        return False
    return not describe.base_name(rel).rsplit(".", 1)[0].isidentifier()


def _canon(names) -> set[str]:
    out: set[str] = set()
    for raw in names or ():
        name = str(raw).strip().lower()
        if name.startswith("@"):
            name = "/".join(name.split("/")[:2])
        elif name not in DOTTED_NAMES:
            name = name.split("/")[0].split(".")[0]
        if name:
            out.add(ALIASES.get(name, name))
    return out


# ---------------------------------------------------------------- analyzer calls (never raise)

# While a private GitHub snapshot is analyzed (see private_names), these logs name no file, no import and
# give only the exception's type: logs never tell what a private repo holds.
_PRIVATE = contextvars.ContextVar("codevision_graph_private", default=False)


@contextlib.contextmanager
def private_names():
    """Within this block, the analysis logs no file names and no exception texts (a private repo)."""
    token = _PRIVATE.set(True)
    try:
        yield
    finally:
        _PRIVATE.reset(token)


def _shown(value):
    """A file name, an import or an exception as a log line shows it: in a private repo, an exception's type
    and nothing else."""
    if not _PRIVATE.get():
        return value
    return type(value).__name__ if isinstance(value, BaseException) else "(private)"


def _analyze(module, rel: str, text: str) -> FileFacts:
    try:
        facts = module.analyze(rel, text)
        if isinstance(facts, FileFacts):
            return facts
    except Exception as exc:
        log.warning("analyze failed for %s: %s", _shown(rel), _shown(exc))
    ext = describe.extension(rel)
    language = "python" if ext in PY_EXTS else "typescript" if ext in TS_EXTS else "javascript"
    return FileFacts(path=rel, language=language, lines=len(text.splitlines()), parse_error=True)


def _resolve(module, imp, rel: str, index) -> list[str]:
    try:
        return [t for t in (module.resolve(imp, rel, index) or []) if isinstance(t, str)]
    except Exception as exc:
        log.warning("resolve failed for %s (%s): %s", _shown(rel), _shown(getattr(imp, "spec", "?")), _shown(exc))
        return []


def _project_entries(module, index) -> dict[str, str]:
    try:
        return {str(k): str(v or "") for k, v in dict(module.project_entries(index) or {}).items()}
    except Exception as exc:
        log.warning("project_entries failed in %s: %s", getattr(module, "__name__", module), _shown(exc))
        return {}


def _project_links(module, index) -> dict[str, list[str]]:
    """Optional analyzer hook: files a file loads without an import in code (index.html -> app.js)."""
    hook = getattr(module, "project_links", None)
    if hook is None:
        return {}
    try:
        return {str(k): [t for t in (v or []) if isinstance(t, str)] for k, v in dict(hook(index) or {}).items()}
    except Exception as exc:
        log.warning("project_links failed in %s: %s", getattr(module, "__name__", module), _shown(exc))
        return {}


def _project_loaded(module, index) -> dict[str, str]:
    """Optional analyzer hook: files a framework loads by itself (Nuxt pages and components, SvelteKit's
    +page.svelte), with a Korean note on how. No import names them, yet they are in use."""
    hook = getattr(module, "project_loaded", None)
    if hook is None:
        return {}
    try:
        return {str(k): str(v or "") for k, v in dict(hook(index) or {}).items()}
    except Exception as exc:
        log.warning("project_loaded failed in %s: %s", getattr(module, "__name__", module), _shown(exc))
        return {}


def _join_prefix(outer: str, inner: str) -> str:
    """Two raw URL prefixes one after the other: ("/api", "/todos") -> "/api/todos"."""
    if not outer:
        return inner
    if not inner:
        return outer
    return outer.rstrip("/") + "/" + inner.lstrip("/")


def _segments(path: str) -> list[str]:
    return [part for part in (path or "").split("/") if part]


def _drop_start(prefix: str, start: str) -> str:
    """A raw prefix without the raw prefix it begins with: ("/root/api", "/root") -> "/api", "" if none is left."""
    parts, head = _segments(prefix), _segments(start)
    if not head or parts[:len(head)] != head:
        return prefix
    rest = parts[len(head):]
    return "/" + "/".join(rest) if rest else ""


class _Exports:
    """The names project files export (FileFacts.exports), followed from file to file. Each file's
    table is made when first asked for, and each name is followed once per build: a barrel file of
    export * lines, imported everywhere, is walked once."""

    def __init__(self, facts: dict[str, FileFacts], owner: dict, index):
        self.facts, self.owner, self.index = facts, owner, index
        self.tables: dict[str, dict[str, list]] = {}
        self.known: dict[tuple[str, str, bool], list[tuple[str, str]]] = {}

    def table(self, rel: str) -> dict[str, list]:
        found = self.tables.get(rel)
        if found is None:
            found = self.tables[rel] = {}
            for entry in getattr(self.facts.get(rel), "exports", None) or ():
                found.setdefault(entry.name, []).append(entry)
        return found

    def follow(self, rel: str, name: str, loose: bool = True, depth: int = 0) -> list[tuple[str, str]]:
        """(file, local name) pairs an exported name of a project file stands for, through the names
        it passes on from other modules: with routes/index.js holding export { default as todos } from
        "./todos", ("routes/index.js", "todos") leads to ("routes/todos.js", "router"). A local name
        "*" is a whole module (export * as api from "./api"). `loose`: a name the file's table does
        not hold stands for itself there (a Python module's attribute, a JavaScript router the table
        leaves out), but not in a module an export * reaches, whose names its table lists. Nine files
        deep at most; a name that leads back to itself leads nowhere more."""
        key = (rel, name, loose)
        if key in self.known:
            return self.known[key]
        self.known[key] = []  # while it is worked out: a loop back here finds nothing
        entries = self.table(rel).get(name)
        star = False
        if not entries and name != "default":
            entries, star = self.table(rel).get("*"), True  # export * from "./api": every name but the default
        out: list[tuple[str, str]] = []
        for entry in entries or ():
            if entry.local:
                if not star:
                    out.append((rel, entry.local))
                continue
            if entry.source is None or depth > 8:
                continue
            original = name if star else entry.original
            for target in _resolve(self.owner[rel], entry.source, rel, self.index):
                if target not in self.facts:
                    continue
                if original == "*" and not star:  # export * as api from "./api": the module itself
                    out.append((target, "*"))
                else:
                    out += self.follow(target, original, not star, depth + 1)
        if not out and loose:
            out = [(rel, name)]
        self.known[key] = out = list(dict.fromkeys(out))
        return out


def _routers(ff: FileFacts) -> set[str]:
    """The routers of a file that own routes or mounts of their own (HttpRoute.router, HttpMount.owner)."""
    return ({getattr(r, "router", "") for r in ff.http_routes} | {getattr(m, "owner", "") for m in ff.http_mounts}) - {""}


def _mount_targets(rel: str, mount, exports: _Exports, routers) -> list[tuple[str, str | None]]:
    """(route file, its router the mount takes) pairs of one mount; the router is None for the whole
    file: the mount names no router (HttpMount.router), or none the route file has. A Python mount
    names the routers it may take in order (routes.todos.router: the router `router` of the submodule
    todos, or the router `todos` of routes), and the last that is a router of the file counts."""
    out: list[tuple[str, str | None]] = []
    facts = exports.facts
    name = str(getattr(mount, "router", "") or "")
    for target in _resolve(exports.owner[rel], mount.target, rel, exports.index):
        if target == rel or target not in facts:
            continue
        picked = False
        for found, local in (exports.follow(target, name) if name else ()):
            if found == rel or found not in facts:
                continue
            known = routers(found)
            # a name another module passes on leads to that module: the whole of it if the router is not known
            choice = next((part for part in reversed(local.split(".")) if part in known), None)
            if choice or found != target:
                out.append((found, choice))
                picked = True
        if not picked:
            out.append((target, None))
    return list(dict.fromkeys(out))


def _bare(mount) -> bool:
    """A mount with no prefix of its own, on the app (or on a router this file does not place)."""
    return not mount.prefix and not getattr(mount, "base", "") and not getattr(mount, "replace", False)


def _mount_key(mount) -> tuple:
    imp = mount.target
    return imp.spec, tuple(imp.names), imp.kind, imp.level, str(getattr(mount, "router", "") or "")


def _fastify_plugins(facts: dict[str, FileFacts], own_files: set, exports: _Exports) -> None:
    """Fastify plugin files that import no server framework (module.exports = async function (fastify)
    { fastify.get(...) }) keep their routes (FileFacts.plugin_routes, plugin_mounts) when the project
    uses Fastify: its own code imports it, or a Fastify file registers the plugin (a few deep)."""
    waiting = {rel for rel, ff in facts.items() if getattr(ff, "plugin_routes", None) or getattr(ff, "plugin_mounts", None)}
    if not waiting:
        return
    owner, index = exports.owner, exports.index

    def keep(rel: str) -> None:
        ff = facts[rel]
        ff.http_routes = ff.http_routes + ff.plugin_routes
        ff.http_mounts = ff.http_mounts + ff.plugin_mounts
        ff.plugin_routes, ff.plugin_mounts = [], []

    fastify = {rel for rel, ff in facts.items() if "fastify" in _canon(ff.frameworks)}
    if fastify & own_files:
        for rel in waiting:
            keep(rel)
        return
    todo = list(fastify)
    while todo and waiting:
        rel = todo.pop()
        for mount in facts[rel].http_mounts:
            for target in _resolve(owner[rel], mount.target, rel, index):
                found = {file for file, _ in exports.follow(target, mount.router or "default")}
                for file in ({target} | found) & waiting:
                    keep(file)
                    waiting.discard(file)
                    todo.append(file)


def _apply_mounts(facts: dict[str, FileFacts], owner: dict, index, exports: _Exports | None = None) -> None:
    """Routers mounted under a prefix answer under it.

    app.use("/api/todos", todos) in server/index.js puts "/api/todos" in front of the routes of
    server/routes/todos.js, through chains (app -> api router -> todos router). A mount that names one
    router of a file (include_router(routes.todos_router), import { todos } from "./routes") moves only
    that router's routes (HttpRoute.router), and a mount of a router reaches the mounts hanging under it
    (HttpMount.owner); a mount of the whole file moves every route, as it always did. A Flask
    register_blueprint(bp, url_prefix=...) takes the place of the blueprint's own url_prefix instead
    (a blueprint registered on another blueprint still answers under that one). A mount with no prefix
    keeps the paths as written beside the others; one of a whole file counts only next to another mount
    of the same router in its file, or when the router it hangs under is mounted in turn (app.use(auth)
    may be middleware). The route file's route hints (shown next to its functions) get the prefixes too
    when all of them are known: a router mounted twice (under /v1 and /v2) shows both addresses.
    """
    exports = exports or _Exports(facts, owner, index)
    routers_of: dict[str, set[str]] = {}

    def routers(rel: str) -> set[str]:
        if rel not in routers_of:
            routers_of[rel] = _routers(facts[rel])
        return routers_of[rel]

    # (route file, router or None for the whole file) -> [(mounting file, HttpMount, whole file?)]
    mounted: dict[tuple[str, str | None], list[tuple[str, object, bool]]] = {}
    paired: dict[str, set] = {}  # file -> its mounts under a prefix: a mount with none beside one is an address
    for rel, ff in facts.items():
        mounts = getattr(ff, "http_mounts", None) or ()
        if mounts:
            paired[rel] = {_mount_key(m) for m in mounts if not _bare(m)}
        for mount in mounts:
            for target, router in _mount_targets(rel, mount, exports, routers):
                mounted.setdefault((target, router), []).append((rel, mount, router is None))
    if not mounted:
        return

    known: dict[tuple[str, str, int], list[tuple[str, bool]]] = {}

    def prefixes(rel: str, router: str, trail: frozenset) -> list[tuple[str, bool]]:
        """(prefix, replaces) pairs the routes of `router` in rel answer under ("" for routes of no known
        router: only mounts of the whole file reach them); replaces: the last mount was a Flask
        url_prefix, so it stands instead of the routes' own prefix (their `base`). Each router is
        worked out once per depth: files that mount the next one many times, nine deep, would
        otherwise take minutes."""
        mounts = mounted.get((rel, None), []) + (mounted.get((rel, router), []) if router else [])
        if not mounts or (rel, router) in trail or len(trail) > 8:
            return [("", False)]
        key = (rel, router, len(trail))
        if key in known:
            return known[key]
        out: list[tuple[str, bool]] = []
        for parent, mount, whole in mounts:
            prefix, base = str(mount.prefix or ""), str(getattr(mount, "base", "") or "")
            own = str(getattr(mount, "base_own", "") or "")
            outers = prefixes(parent, str(getattr(mount, "owner", "") or ""), trail | {(rel, router)})
            if (whole and _bare(mount) and _mount_key(mount) not in paired.get(parent, ())
                    and not any(o.strip("/") or replaced for o, replaced in outers)):
                continue  # app.use(x) alone, x a whole file: as written, and x may be middleware
            for outer, replaced in outers:
                # the router this one is mounted on keeps its prefix, but a Flask url_prefix that mounted
                # its file stands instead of the outermost blueprint's own one (root_bp -> api_bp: /v1/api)
                holder = _join_prefix(outer, _drop_start(base, own) if replaced else base)
                out.append((_join_prefix(holder, prefix), bool(getattr(mount, "replace", False))))
        known[key] = result = list(dict.fromkeys(out))[:8] or [("", False)]
        return result

    for rel in dict.fromkeys(rel for rel, _ in mounted):
        ff = facts[rel]
        if not ff.http_routes:
            continue
        found_by: dict[str, list[tuple[str, bool]]] = {}

        def found_for(router: str) -> list[tuple[str, bool]]:
            if router not in found_by:
                found_by[router] = prefixes(rel, router, frozenset())
            return found_by[router]

        routes: dict[tuple[str, str], HttpRoute] = {}
        changed = False
        for route in ff.http_routes:
            router = str(getattr(route, "router", "") or "")
            found = found_for(router)
            if not any(p.strip("/") or replaces for p, replaces in found):
                routes.setdefault((route.method, route.path), route)  # no mount reaches its router
                continue
            changed = True
            for prefix, replaces in found:
                path = join_http_path(prefix, strip_http_base(route.path, route.base) if replaces else route.path)
                if path and (route.method, path) not in routes:
                    routes[(route.method, path)] = HttpRoute(route.method, path, route.handler, route.line, router=router)
        if not changed:
            continue
        handlers = {}  # handler -> (its router, its router's own prefix, which a Flask url_prefix takes the place of)
        for route in ff.http_routes:
            handlers.setdefault(route.handler, (str(getattr(route, "router", "") or ""), route.base))
        ff.http_routes = list(routes.values())
        if ff.language != "python":
            continue  # only Python hints show the file's own addresses; a JavaScript hint is a request it sends
        for sym in ff.symbols:
            hint = _ROUTE_HINT.match(sym.hint or "")
            if not hint:
                continue
            router, own = handlers.get(sym.name, ("", ""))
            found = found_for(router)
            if not any(p.strip("/") or replaces for p, replaces in found) or any("${" in p for p, _ in found):
                continue  # not mounted, or a prefix only known at run time: the hint keeps the paths as written
            shown: list[str] = []  # a router mounted twice (/v1 and /v2) answers at both addresses
            for prefix, replaces in found:
                base = "/" + prefix.strip("/")
                for path in re.split(r", (?=/)", hint.group(2)):
                    if replaces and own:  # the hint shows the path as written: drop the own prefix
                        path = "/" + "/".join(_segments(path)[len(_segments(own)):])
                    full = base if path == "/" else base.rstrip("/") + path
                    if full not in shown:
                        shown.append(full)
            sym.hint = f"{hint.group(1)} · {', '.join(shown[:8])}"  # 8 addresses at most, like the prefixes


def _helper_for(rel: str, call, exports: _Exports, helpers) -> tuple | None:
    """(HttpHelper, client method) a call of an imported request helper reaches (HelperCall), or None.
    The import and the module's exports lead to the helper: api("/x") to the function api, http.get(
    "/x") to the client http and its get method, api.get("/x") to the function get of an object api;
    and with a default import or a require(), api.get("/x") may name the module's function get."""
    member = call.member
    for target in _resolve(exports.owner[rel], call.target, rel, exports.index):
        if target == rel or target not in exports.facts:
            continue
        for found, local in exports.follow(target, call.name):
            if local == "*":  # export * as api from "./api": a function of that module
                for file, name in (exports.follow(found, member) if member else ()):
                    helper = helpers(file).get(name)
                    if helper is not None and not helper.client:
                        return helper, ""
                continue
            own = helpers(found)
            if member:
                helper = own.get(f"{local}.{member}")
                if helper is not None and not helper.client:
                    return helper, ""
                helper = own.get(local)
                if helper is not None and helper.client:
                    return helper, member
            else:
                helper = own.get(local)
                if helper is not None and not helper.client:
                    return helper, ""
        if member and call.name == "default":  # module.exports = { get(path) {...} }: require("./api").get
            for file, name in exports.follow(target, member):
                helper = helpers(file).get(name)
                if helper is not None and not helper.client:
                    return helper, ""
    return None


def _resolve_helpers(facts: dict[str, FileFacts], owner: dict, index, exports: _Exports | None = None) -> None:
    """Calls of request helpers made in other files (FileFacts.helper_calls) become HTTP calls of the
    file that makes them, at the address the helper builds (FileFacts.http_helpers): api("/todos")
    with api = (path) => fetch("/api" + path) is GET /api/todos. A call the file had read on its own
    (http.get("/todos") without the client's base address) gives way to it, and the function that makes
    the call shows the request as its hint. The helper's own fetch(BASE + path) is no request."""
    exports = exports or _Exports(facts, owner, index)
    by_file: dict[str, dict] = {}

    def helpers(rel: str) -> dict:
        if rel not in by_file:
            by_file[rel] = {h.name: h for h in getattr(facts.get(rel), "http_helpers", None) or ()}
        return by_file[rel]

    for rel, ff in facts.items():
        calls = getattr(ff, "helper_calls", None)
        if not calls:
            continue
        changed = False
        # Made once per file, when first needed: a generated API client with thousands of helper calls
        # would otherwise look through all its calls and functions again for each one.
        places: dict[int, int] | None = None  # id of a call the file read on its own -> where it is
        named: dict[str, list] | None = None  # function name -> its symbols
        for call in calls:
            found = _helper_for(rel, call, exports, helpers)
            request = helper_request(found[0], call.args, call.options, found[1]) if found else None
            path = normalize_http_path(request[1]) if request else None
            if path is None:
                continue
            new = HttpCall(request[0], path, call.line)
            guess = call.guess
            at = None
            if guess is not None:
                if places is None:
                    places = {id(c): i for i, c in enumerate(ff.http_calls)}
                at = places.get(id(guess))
                if at is not None and ff.http_calls[at] is not guess:
                    at = None
            if at is None:
                ff.http_calls.append(new)
            else:
                ff.http_calls[at] = new
            changed = True
            old = f"{guess.method} · {guess.path}" if guess is not None else None
            if call.symbols and named is None:
                named = {}
                for sym in ff.symbols:
                    named.setdefault(sym.name, []).append(sym)
            for name in dict.fromkeys(call.symbols):
                for sym in named.get(name, ()):
                    if not sym.hint or sym.hint == old:
                        sym.hint = f"{new.method} · {new.path}"
        if changed:
            ff.http_calls.sort(key=lambda c: c.line)


# ---------------------------------------------------------------- areas

def _package_kind(text: str | None) -> str | None:
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    deps: set[str] = set()
    if isinstance(data, dict):
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(data.get(key), dict):
                deps |= set(data[key])
    ui, sv = bool(deps & UI_DEPS), bool(deps & SERVER_DEPS)
    return "ui" if ui and not sv else "sv" if sv and not ui else None


def _nearest_package(rel: str, packages: dict[str, str | None]) -> str | None:
    folder = parent_dir(rel)
    while True:
        if folder in packages:
            return packages[folder]
        if not folder:
            return None
        folder = parent_dir(folder)


def _file_area(rel: str, facts: FileFacts | None, packages: dict, project_ui: bool,
               in_page: frozenset | set = frozenset()) -> str:
    ext = describe.extension(rel)
    language = facts.language if facts else ""
    if describe.is_config(rel):
        return "ot"
    if ext in PY_EXTS or language == "python":
        return "sv"
    if ext in UI_EXTS:
        return "ui"
    if ext in JS_EXTS or language in ("javascript", "typescript"):
        fws = _canon(facts.frameworks) if facts else set()
        if fws & set(UI_FRAMEWORKS):
            return "ui"
        if fws & WEB_SERVERS:
            return "sv"
        if rel in in_page:  # a web page loads it with <script src> (or a file such a script imports)
            return "ui"
        return _nearest_package(rel, packages) or ("ui" if project_ui else "sv")
    return "ot"


def _page_scripts(loaded: set[str], uses: dict[str, set[str]], facts: dict[str, FileFacts]) -> set[str]:
    """JavaScript run by a web page: what an HTML file loads, and the JS files those import."""
    todo = [rel for rel in loaded if describe.extension(rel) in JS_EXTS | UI_EXTS and rel in facts]
    seen = set(todo)
    while todo:
        for target in uses.get(todo.pop(), ()):
            if target not in seen and describe.extension(target) in JS_EXTS and target in facts:
                seen.add(target)
                todo.append(target)
    return seen


def _majority(votes: Counter | None) -> str:
    if not votes:
        return "ot"
    return "ui" if votes["ui"] >= votes["sv"] else "sv"


# ---------------------------------------------------------------- stack, icons, warnings

def _db_hints(text: str) -> set[str]:
    """Databases a code file names: "sqlite:///todo.db", dialect: 'postgres', django.db.backends.mysql."""
    found: set[str] = set()
    low = (text or "").lower()
    if not low.isascii():  # the regex, ignoring case, also takes "ı" and "İ" for i and "ſ" for s
        low = low.replace("ı", "i").replace("i\u0307", "i").replace("ſ", "s")  # "İ".lower() is "i" + U+0307
    if not any(word in low for word in _DB_WORDS):
        return found
    for match in _DB_HINT.finditer(text):
        word = next(g for g in match.groups() if g).lower()
        if word.startswith("sqlite"):
            found.add("sqlite")
        elif word.startswith("postgres"):
            found.add("postgres")
        elif word in ("mysql", "mariadb"):
            found.add("mysql")
        else:
            found.add("mongodb")
    return found


def _first(table: dict[str, str], found: set[str]) -> str:
    return next((name for key, name in table.items() if key in found), "")


def _stack(frameworks: set[str], databases: set[str]) -> list[dict]:
    stack = []
    for area, label, table, found in (
        ("ui", "화면", UI_FRAMEWORKS, frameworks),
        ("sv", "서버", SERVER_FRAMEWORKS, frameworks),
        ("sv", "DB", DB_FRAMEWORKS, databases),
    ):
        techs = [name for key, name in table.items() if key in found]
        if techs:
            stack.append({"area": area, "label": label, "tech": ", ".join(techs)})
    return stack


def _icon(rel: str, orphan: bool, binary: bool, unreadable: bool = False) -> str | None:
    if orphan:
        return "ti-file-alert"
    if unreadable:
        return "ti-lock"
    if describe.is_image(rel):
        return "ti-photo"
    if describe.is_config(rel):
        return "ti-file-settings"
    if describe.is_doc(rel):
        return "ti-file-text"
    return "ti-file" if binary else None


def _warnings(result, facts: dict[str, FileFacts], max_files: int, unreadable_files: int,
              capped: list[str] = ()) -> list[str]:
    """`capped`: the files with a pattern import that matched more than PATTERN_LIMIT files."""
    out = []
    for module_name in sorted(lang.load_errors()):
        label, what = ANALYZER_LABELS.get(module_name, (module_name.rsplit(".", 1)[-1], "그 언어 파일"))
        out.append(f"{label} 분석기를 불러오지 못해서 {what}의 연결과 함수 목록이 빠져 있어요.")
    blocked = getattr(result, "unreadable", [])
    if "" in blocked:
        out.append("이 폴더를 열어 볼 수 없어서 안에 무엇이 있는지 보지 못했어요. 폴더 권한을 확인해 주세요.")
    elif blocked:
        out.append(f"열어 볼 수 없는 폴더 {len(blocked):,}개는 안에 무엇이 있는지 보지 못했어요.")
    if unreadable_files:
        out.append(f"열 수 없는 파일 {unreadable_files:,}개는 내용을 분석하지 않았어요.")
    if result.skipped:
        out.append(f"1MB가 넘는 파일 {len(result.skipped):,}개는 내용을 분석하지 않았어요.")
    file_limit = getattr(result, "file_limit", result.truncated)
    crowded = list(getattr(result, "crowded", []))
    if file_limit:
        out.append(f"파일이 너무 많아서 처음 {max_files:,}개만 분석했어요.")
    if crowded:
        cap = getattr(result, "max_entries", 0)
        names = ", ".join((f"{c}/" if c else "맨 위 폴더") for c in crowded[:2]) + (" 등" if len(crowded) > 2 else "")
        many = f"처음 {cap:,}개 항목" if cap else "일부"
        out.append(f"안에 든 것이 아주 많은 폴더({names})는 {many}만 살펴봤어요.")
    if getattr(result, "too_deep", False):
        out.append(f"폴더가 너무 깊이 들어가 있어서 {MAX_DEPTH:,}단계보다 깊은 곳은 보지 않았어요.")
    if capped:
        names = ", ".join(capped[:2]) + (" 등" if len(capped) > 2 else "")
        out.append(f"이름 규칙으로 여러 파일을 한꺼번에 불러오는 코드({names})는 맞는 파일이 너무 많아서 "
                   f"{PATTERN_LIMIT:,}개까지만 연결했어요.")
    broken = sum(1 for ff in facts.values() if ff.parse_error)
    if broken:
        out.append(f"문법 오류 등으로 끝까지 읽지 못한 파일이 {broken:,}개 있어요. 읽은 부분만 보여 줘요.")
    return out


def _unread_kind(index, rel: str) -> str:
    """Why read_text() gave nothing: "unreadable" (locked, no permission) or "binary" (NUL bytes)."""
    try:
        with open(os.path.join(index.root, rel), "rb") as fh:
            fh.read(1)
    except OSError:
        return "unreadable"
    return "binary"


def _root_label(root: str) -> tuple[str, str]:
    """Name and shown path of the analyzed folder: ("my-app", "my-app/").

    A drive root has no folder name: C:\\ is ("C:", "C:/"), not ("C:\\", "C:\\/").
    """
    name = os.path.basename(root.rstrip("\\/"))
    if not name:
        name = os.path.splitdrive(root)[0].rstrip("\\/") or root  # "C:", "\\\\server\\share"; "/" elsewhere
    return name, name if name.endswith(("/", "\\")) else f"{name}/"


def _used_outside(folder: str, files: list[str], file_area: dict[str, str], used_by: dict[str, set[str]]) -> int:
    """How many code files in a design or screenshot folder (design/, mockups/), at any depth, code
    outside the folder uses. Such a name says the folder holds no program code; code that the rest of
    the program imports (src/design/Button.tsx) says otherwise. Other folders are not looked through."""
    if describe.base_name(folder).lower().lstrip("_") not in describe.NOT_CODE_FOLDERS:
        return 0
    head = folder + "/"
    return sum(1 for rel in files if rel.startswith(head) and file_area[rel] != "ot"
               and any(not user.startswith(head) for user in used_by[rel]))


def _demo_main(rel: str, facts: FileFacts | None, module) -> bool:
    """A Python `if __name__ == "__main__":` block, the only evidence that the file is a start point."""
    reason = getattr(module, "REASON_MAIN", None)
    return (facts is not None and reason is not None and facts.entry_reason == reason
            and describe.base_name(rel) != "__main__.py")


# ---------------------------------------------------------------- build

def build(root_abs: str, *, max_files: int = 5000) -> dict:
    """Scan and analyze a project folder and return the graph as a JSON-ready dict."""
    root = os.path.abspath(str(root_abs))
    result = scan(root, max_files=max_files)
    index = result.index
    files = sorted(index.files, key=_ci)
    dirs = sorted((d for d in index.dirs if d), key=_ci)
    binary, skipped = set(result.binary), set(result.skipped)

    # 1. analyze every code file with its analyzer. The texts are not kept: the facts, and the
    #    databases the project's own code names (a database URL), are all that later steps need.
    #    Examples, demos, fixtures and tests do not describe the project (see 4).
    own_files = {rel for rel in files if not _in_dirs(rel, EXAMPLE_DIRS) and not describe.is_test(rel)}
    modules = list(lang.analyzers())
    facts: dict[str, FileFacts] = {}
    owner: dict[str, object] = {}
    databases: set[str] = set()
    held: list[str] = []  # texts let go of together when the step is over, up to TEXT_HOLD bytes
    held_size = 0
    for rel in files:
        if rel in binary or rel in skipped:
            continue
        module = lang.analyzer_for(rel)
        text = index.read_text(rel, cache=False) if module is not None else None
        if text is None:
            continue
        if all(m is not module for m in modules):
            modules.append(module)
        facts[rel] = ff = _analyze(module, rel, text)
        owner[rel] = module
        if rel in own_files and _canon(ff.frameworks) & DB_HINT_FRAMEWORKS:
            databases |= _db_hints(text)
        if held_size < TEXT_HOLD:
            held.append(text)
            held_size += sys.getsizeof(text)
    del held

    # 2. connections: imports, files a page loads (index.html -> app.js, app.css), then HTTP calls
    #    matched to server routes once mounted routers have their prefixes
    uses: dict[str, set[str]] = {rel: set() for rel in files}
    used_by: dict[str, set[str]] = {rel: set() for rel in files}
    http: dict[str, set[str]] = {rel: set() for rel in files}
    capped: list[str] = []  # files with a pattern import that matched more than PATTERN_LIMIT files
    more: set[str] = set()  # files such an import matched past the limit: in use, but no line drawn
    for rel, ff in facts.items():
        for imp in ff.imports:
            targets = _resolve(owner[rel], imp, rel, index)
            if len(targets) > PATTERN_LIMIT and is_pattern(imp):
                targets = [t for t in targets if t != rel and t in index.files]
                if len(targets) > PATTERN_LIMIT:
                    more.update(targets[PATTERN_LIMIT:])
                    targets = targets[:PATTERN_LIMIT]
                    if not capped or capped[-1] != rel:
                        capped.append(rel)
            for target in targets:
                if target != rel and target in index.files:
                    uses[rel].add(target)
                    used_by[target].add(rel)
    page_loaded: set[str] = set()
    for module in modules:
        for page, targets in _project_links(module, index).items():
            if page not in index.files:
                continue
            for target in targets:
                if target != page and target in index.files:
                    uses[page].add(target)
                    used_by[target].add(page)
                    page_loaded.add(target)
    page_js = _page_scripts(page_loaded, uses, facts)  # before HTTP links join the uses
    exports = _Exports(facts, owner, index)  # names files export, followed across files (routers, helpers)
    _fastify_plugins(facts, own_files, exports)
    _apply_mounts(facts, owner, index, exports)
    _resolve_helpers(facts, owner, index, exports)
    servers = [(rel, ff.http_routes) for rel, ff in facts.items() if ff.http_routes]
    for rel, ff in facts.items():
        if not ff.http_calls:
            continue
        for server, routes in servers:
            if server != rel and any(
                call.path and route.path
                and http_paths_match(call.path, route.path)
                and http_methods_match(str(call.method).upper(), str(route.method).upper())
                for call in ff.http_calls for route in routes
            ):
                uses[rel].add(server)
                used_by[server].add(rel)
                http[rel].add(server)
                http[server].add(rel)

    # 3. entries: project-level evidence first, then file-local evidence
    entries: dict[str, str] = {}
    for module in modules:
        for rel, reason in _project_entries(module, index).items():
            if rel in index.files and not entries.get(rel):
                entries[rel] = reason
    runnable: set[str] = set()  # library modules that only have a demo `__main__` block
    for rel, ff in facts.items():
        if not ff.is_entry or entries.get(rel):
            continue
        # rich/console.py (and many generated helper modules) has `if __name__ == "__main__":`
        # for a demo, but other code imports it: it is a part, not where the program starts.
        if _demo_main(rel, ff, owner.get(rel)) and any(not describe.is_test(user) for user in used_by[rel]):
            runnable.add(rel)
            continue
        entries[rel] = ff.entry_reason or ""
    loaded: dict[str, str] = {}  # files a framework loads by itself -> how (Nuxt pages, SvelteKit routes)
    for module in modules:
        for rel, note in _project_loaded(module, index).items():
            if rel in index.files and rel not in loaded:
                loaded[rel] = note

    # 4. frameworks, areas and orphans. The project-level picture (한눈에 보기, legend names) comes
    #    from the project's own code: examples, demos, fixtures and tests do not describe it.
    packages = {parent_dir(rel): _package_kind(index.read_text(rel, cache=False))
                for rel in files if describe.base_name(rel) == "package.json" and rel not in skipped}
    own_fws: set[str] = set()
    folder_fws: dict[str, set[str]] = {}
    for rel, ff in facts.items():
        fws = _canon(ff.frameworks)
        if rel in own_files:
            own_fws |= fws
        folder = parent_dir(rel)
        while folder:
            folder_fws.setdefault(folder, set()).update(fws)
            folder = parent_dir(folder)
    own_packages = [kind for folder, kind in packages.items() if not _in_dirs(f"{folder}/package.json", EXAMPLE_DIRS)]
    project_ui = bool(own_fws & set(UI_FRAMEWORKS)) or "ui" in own_packages
    file_area = {rel: _file_area(rel, facts.get(rel), packages, project_ui, page_js) for rel in files}
    votes: dict[str, Counter] = {}
    for rel in files:
        if file_area[rel] == "ot":
            continue
        folder = parent_dir(rel)
        while folder:
            votes.setdefault(folder, Counter())[file_area[rel]] += 1
            folder = parent_dir(folder)
    folder_area = {d: _majority(votes.get(d)) for d in dirs}
    orphans = {
        rel for rel, ff in facts.items()
        if ff.language in describe.CODE_LANGUAGES and not used_by[rel] and rel not in entries
        and not describe.is_test(rel) and not describe.is_config(rel)
        and describe.base_name(rel).lower() not in NOT_ORPHAN_NAMES and not rel.lower().endswith(".d.ts")
        and not MIGRATION_DIRS & set(rel.lower().split("/")[:-1])  # loaded by the migration tool itself
        and not _by_path_only(rel, ff)  # run or loaded by its path, which no import shows
        and rel not in loaded and rel not in more  # loaded by a framework, or by a capped pattern
    }
    areas = {"ui": "화면 코드", "sv": "서버 코드" if own_fws & WEB_SERVERS else "코드", "ot": "문서·설정"}

    # 5. nodes
    child_dirs: dict[str, list[str]] = {}
    child_files: dict[str, list[str]] = {}
    for d in dirs:
        child_dirs.setdefault(parent_dir(d), []).append(d)
    for rel in files:
        child_files.setdefault(parent_dir(rel), []).append(rel)

    def kids(folder: str) -> list[str]:
        return (sorted(child_dirs.get(folder, []), key=_name_key)
                + sorted(child_files.get(folder, []), key=_name_key))

    name, shown = _root_label(root)
    top = {rel.lower(): rel for rel in child_files.get("", [])}
    readme = next((top[n] for n in README_NAMES if n in top and top[n] not in binary | skipped), None)
    lang_counts = Counter(ff.language for ff in facts.values() if ff.language in LANGUAGE_NAMES)
    code_total = sum(lang_counts.values())
    blocked = set(getattr(result, "unreadable", []))
    cut_off = set(getattr(result, "cut_off", [])) | set(getattr(result, "crowded", []))
    root_init = facts.get("__init__.py")  # the analyzed folder is itself a package (pip, rich)
    s, d = describe.describe_root(
        name, readme=index.read_text(readme) if readme else None,
        # a language with one stray file among hundreds does not describe the project
        languages=[LANGUAGE_NAMES[k] for k, c in lang_counts.most_common() if c * 20 >= code_total],
        files=len(files), folders=len(dirs), doc=root_init.module_doc if root_init else "",
        unreadable="" in blocked,
    )
    nodes: dict[str, dict] = {".": {"n": name, "p": shown, "t": "d", "a": "ot", "s": s, "d": d, "k": kids("")}}

    for folder in dirs:
        fws = folder_fws.get(folder, set())
        init = facts.get(join_rel(folder, "__init__.py"))
        vote = votes.get(folder) or Counter()
        mixed = ""  # frontend and backend side by side (my-todo-app/)
        if vote["ui"] and vote["sv"] and min(vote["ui"], vote["sv"]) * 4 >= vote["ui"] + vote["sv"]:
            mixed = "server" if fws & WEB_SERVERS else "code"
        direct = child_files.get(folder, [])
        s, d = describe.describe_folder(
            folder, area=folder_area[folder], files=len(direct),
            folders=len(child_dirs.get(folder, [])), area_label=areas[folder_area[folder]],
            ui_tech=_first(UI_FRAMEWORKS, fws), sv_tech=_first(SERVER_FRAMEWORKS, fws),
            doc=init.module_doc if init else "", package=init is not None, unreadable=folder in blocked,
            mixed=mixed, cut_off=folder in cut_off, images=sum(1 for f in direct if describe.is_image(f)),
            used_outside=_used_outside(folder, files, file_area, used_by),
        )
        nodes[folder] = {"n": describe.base_name(folder), "p": f"{folder}/", "t": "d", "a": folder_area[folder],
                         "s": s, "d": d, "k": kids(folder)}

    unreadable_files = 0
    for rel in files:
        ff = facts.get(rel)
        is_binary, is_unreadable, text = rel in binary, False, None
        document = describe.extension(rel) in (".md", ".markdown")
        # An analyzed file knows its lines. Any other text file is read here only for its line
        # count (and a document's title), so its text is not kept (300 big .csv files add up).
        if not is_binary and rel not in skipped and (ff is None or not ff.lines or document):
            text = index.read_text(rel, cache=False)
            if text is None and ff is None:  # NUL bytes (UTF-16, .DS_Store) or locked
                if _unread_kind(index, rel) == "unreadable":
                    is_unreadable = True
                    unreadable_files += 1
                else:
                    is_binary = True
        lines = (ff.lines if ff is not None else 0) or (len(text.splitlines()) if text else 0)
        title = describe.doc_title(text) if text and document else ""
        s, d = describe.describe_file(
            rel, ff, area=file_area[rel], entry=rel in entries, entry_reason=entries.get(rel, ""),
            orphan=rel in orphans, uses=len(uses[rel]), used_by=len(used_by[rel]),
            binary=is_binary, skipped=rel in skipped, title=title, unreadable=is_unreadable,
            runnable=rel in runnable, by_path=_by_path_only(rel, ff),
            loaded=loaded.get(rel) or (PATTERN_MORE if rel in more and not used_by[rel] else ""),
        )
        node = {
            "n": describe.base_name(rel), "p": rel, "t": "f", "a": file_area[rel], "l": lines, "s": s, "d": d,
            "f": describe.functions(ff), "u": sorted(uses[rel], key=_ci), "b": sorted(used_by[rel], key=_ci),
            "h": sorted(http[rel], key=_ci),
        }
        if rel in entries:
            node["e"] = 1
        if rel in orphans:
            node["o"] = 1
        icon = _icon(rel, rel in orphans, is_binary, is_unreadable)
        if icon:
            node["i"] = icon
        if ff is not None and ff.language in LANGUAGE_NAMES:
            node["lang"] = ff.language
        nodes[rel] = node

    def focus_key(rel: str):
        links = len(uses[rel]) + len(used_by[rel])
        # the project's own code first, then examples and tests, then copied-in (vendored) code
        tier = 3 if not links else 2 if _in_dirs(rel, VENDOR_DIRS) else 0 if rel in own_files else 1
        return tier, -links, rel not in entries, _ci(rel)

    focus = min(files, key=focus_key) if files else "."
    return {
        "version": 1,
        "source": {"kind": "local", "path": root},
        "name": name,
        "root": ".",
        "focus": focus,
        "stats": {"folders": len(dirs), "files": len(files), "entries": len(entries),
                  "orphans": len(orphans), "skipped": len(result.skipped)},
        "hidden": list(result.hidden),
        "areas": areas,
        "stack": _stack(own_fws, (own_fws & set(DB_FRAMEWORKS)) | databases),
        "ai": dict(AI_DEFAULT),
        "warnings": _warnings(result, facts, max_files, unreadable_files, capped),
        "truncated": result.truncated,
        "generated": time.time(),
        "nodes": nodes,
    }
