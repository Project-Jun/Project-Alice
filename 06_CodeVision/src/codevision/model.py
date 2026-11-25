"""Shared data model for the CodeVision analyzers, graph builder and server.

Each analyzer in ``codevision.lang`` turns one source file into ``FileFacts``.
``codevision.graph`` combines them into the JSON graph that the web UI renders.
The full contract is in docs/architecture.md.
"""
from __future__ import annotations

import itertools
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

AREAS = ("ui", "sv", "ot")


@dataclass
class ImportRef:
    """One import, require or re-export, as written in the source.

    A module whose name is only partly written in the code is a pattern (see is_pattern()):
    `*` stands for the part only known at run time, and the import points to every project file
    that matches. import(`./locales/${lang}.json`) -> "./locales/*.json",
    import_module(f"pkg.plugins.{name}") -> "pkg.plugins.*". A "glob" import is one the code
    writes as a pattern itself: import.meta.glob("./pages/*.vue"), whose `names` hold the other
    patterns of the call ("!./pages/Draft.vue" leaves files out), and require.context("./dir",
    true, /\\.js$/) -> "./dir/**" with the regular expression in `names` ("/\\.js$/").
    """

    spec: str  # raw specifier: "./api", "react", "routes.todos"; "" for `from . import x`; a pattern
    names: list[str] = field(default_factory=list)  # imported names, if any
    kind: str = "import"  # import | from | require | dynamic | reexport | side-effect | glob
    level: int = 0  # Python relative-import level (number of leading dots)
    line: int = 0


def is_pattern(imp: ImportRef) -> bool:
    """An import that names its modules by a pattern (see ImportRef): it may point to many files."""
    return imp.kind == "glob" or "*" in (imp.spec or "")


_ATOMIC_NAMES = itertools.count()  # group names for atomic(): unique in every pattern


def atomic(inner: str) -> str:
    """Regular expression text that matches `inner` at its first place and never tries it again: an
    atomic group Python 3.10 can read too (a lookahead never backtracks; the backreference takes its text)."""
    name = f"a{next(_ATOMIC_NAMES)}"
    return f"(?=(?P<{name}>{inner}))(?P={name})"


def join_stars(pieces: list[str], star: str = "[^/]") -> str:
    """Regular expression text for fixed pieces with `star`* between each two: one name of a glob
    (.gitignore rules, import.meta.glob) or of a module name built at run time.

    Written out plainly, "*a*a*a*a*a*a*b" tries every way to split a long name between its stars, which
    takes minutes for one name, and Python's re holds the GIL all the while: a hostile repo could stop the
    whole server. Like fnmatch, the piece after each inner star is taken at its first place and never
    tried again. That is always right when every piece matches a fixed number of characters, none of them
    one that `star` refuses (the star before can then take what the earlier place leaves)."""
    if len(pieces) == 1:
        return pieces[0]
    middle = "".join(atomic(f"{star}*?{piece}") for piece in pieces[1:-1])
    return f"{pieces[0]}{middle}{star}*{pieces[-1]}"


@dataclass
class Symbol:
    """A top-level function, class or component worth listing in the panel."""

    name: str
    kind: str  # function | class | component | method | constant
    signature: str  # what the panel shows, e.g. "addTodo(title)", "class Todo"
    doc: str = ""  # first sentence of the docstring or JSDoc, wrapped lines joined, stripped
    hint: str = ""  # short Korean hint from heuristics, e.g. "GET · /todos"
    line: int = 0
    exported: bool = False
    # A class's bases as written, the last part of each name: class SMTPSenderRefused(SMTPResponseException)
    # -> ("SMTPResponseException",), class Timeout(errors.Error) -> ("Error",), and in JavaScript what it
    # extends: class NotFound extends errors.HttpError -> ("HttpError",).
    bases: tuple[str, ...] = ()


@dataclass
class HttpCall:
    """A client-side HTTP request found in code (fetch, axios, requests, ...)."""

    method: str  # GET | POST | PUT | PATCH | DELETE | ANY
    path: str  # normalized with normalize_http_path()
    line: int = 0


@dataclass
class HttpRoute:
    """A server-side route definition (FastAPI, Flask, Express, ...)."""

    method: str  # GET | POST | PUT | PATCH | DELETE | ANY
    path: str  # normalized with normalize_http_path()
    handler: str = ""
    line: int = 0
    # The start of `path` that a Flask register_blueprint(bp, url_prefix=...) in another file takes
    # the place of: the own url_prefix of the file's blueprint the route answers under
    # (Blueprint(url_prefix="/todos")), normalized; "" if none.
    base: str = ""
    # The router of this file the route answers under, by its variable: `todos_router` for
    # @todos_router.get(...), `router` for router.get(...), a Fastify plugin function. A router that
    # this file mounts on another of its routers answers under that one, so it is the outermost
    # router of the file. A mount in another file that names one router (include_router(
    # routes.todos_router)) moves only that router's routes. "" when not known (file-path routes).
    router: str = ""


@dataclass
class HttpMount:
    """Routes of another module made reachable under a URL prefix.

    Express app.use("/api/todos", todos), Hono app.route("/api", api), Fastify
    register(todos, {prefix}), FastAPI include_router(todos.router, prefix="/api"),
    Flask register_blueprint(bp, url_prefix="/api"). `target` is the import that
    brought the mounted router into this file; the graph resolves it like any import
    and puts `prefix` in front of the routes of the file it points to.

    Mounted on a router of its own (api_bp.register_blueprint(todos_bp)), the router's
    own prefix is `base`: it goes in front of `prefix`. When this file is mounted in turn
    with a Flask url_prefix, that takes the place of `base_own`, the start of `base` that
    is the own url_prefix of the outermost blueprint (root_bp in root_bp -> api_bp -> todos_bp).
    Only the Python analyzer fills `replace` and `base_own`; the JavaScript analyzer fills `base` only
    (JavaScript has no Flask-style url_prefix, so `replace` stays False).

    A mount that names one router of the target module (`router`: include_router(
    routes.todos_router), app.use("/todos", todos) with import { todos } from "./routes") moves only
    the routes of that router (HttpRoute.router); one that takes the whole module, or names something
    the graph does not know as a router there, moves every route of the file. `owner` is the router
    of this file the target hangs under, so that a mount of this file in turn reaches it only through
    that router. A mount with no prefix on the app is recorded too: alone it moves nothing, but next
    to a mount of the same router under a prefix (in any file) the paths as written stay an address.
    """

    prefix: str  # raw prefix text, "${}" for unknown parts; normalized when it is applied
    target: ImportRef
    line: int = 0
    base: str = ""  # raw prefix the router the target is mounted on answers under in this file ("" for an app)
    # Flask: the url_prefix takes the place of the mounted blueprint's own prefix (the routes'
    # `base`) instead of going in front of it. FastAPI and Express put prefixes in front.
    replace: bool = False
    base_own: str = ""  # raw start of `base` that a Flask url_prefix mounting this file takes the place of
    # The mounted router's name in the module `target` points to: an exported name in JavaScript
    # ("todos", "default" for a default import or a require()), the names written after the import in
    # Python ("todos_router", "todos.router": the graph takes the last one that is a router of that
    # file). "" for the whole module.
    router: str = ""
    owner: str = ""  # the router of this file the target is mounted on (its routes' HttpRoute.router)


@dataclass
class HttpHelper:
    """A request helper that other files of the project call with the address they want.

    A function that requests the path it is given, after a base address of its own: export const api
    = (path, opts) => fetch(BASE + path, opts) is HttpHelper("api", BASE, url_at=0, method="ANY",
    options_at=1). Or an HTTP client made with a base address, whose methods take the path first:
    export const http = axios.create({ baseURL: "/api" }) is HttpHelper("http", "/api", client=True),
    and http.get("/todos") in another file asks for GET /api/todos. See helper_request().
    """

    name: str  # the variable holding it ("api"; "api.get" for a function in an object); FileFacts.exports says how others import it
    base: str = ""  # raw address text in front of the path: "/api", "http://localhost:8000", "${}" (only known at run time)
    url_at: int = 0  # the argument that is the path
    method: str = "GET"  # the method it sends, when no argument chooses it
    method_at: int | None = None  # the argument that names the method: request(url, method)
    method_default: str = ""  # that parameter's written default (request(url, method = "GET")), "" if none
    options_at: int | None = None  # the argument holding fetch()'s options: api(path, { method: "POST" })
    client: bool = False  # an HTTP client (axios.create): its get/post/put/patch/delete methods take the path
    line: int = 0


@dataclass
class HelperCall:
    """A call of a request helper or HTTP client that this file imports from another project file:
    api("/todos", { method: "POST" }), http.get("/todos"), api.request("/todos") through
    import * as api. The graph finds the helper (HttpHelper) through the import and the file's
    exports, and turns the call into an HttpCall of this file at the address the helper builds."""

    target: ImportRef  # the import the helper came in with
    name: str  # the imported name in that module: "api", "default" (default import, require()), "*" (import * as x)
    member: str = ""  # the method called on it: "get" in http.get("/todos"); "" for a plain call
    args: list[str | None] = field(default_factory=list)  # each argument as address text ("${}" for unknown parts), None if not one
    options: list[str | None] = field(default_factory=list)  # the method each options-object argument sets ("GET" if none, "ANY" unknown); None for others
    line: int = 0
    # What this file made of the call on its own (http.get("/todos") with no base address): the graph
    # puts the full address in its place when it finds the helper, and leaves it when it does not.
    guess: HttpCall | None = None
    symbols: list[str] = field(default_factory=list)  # functions of this file whose request hint is this call


@dataclass
class ExportName:
    """A name this file exports that the graph follows from other files: a router another file mounts,
    a request helper another file calls. Either a variable of this file (`local`), or a name passed on
    from another module (`source`): export { api } from "./api", export * from "./api" (name "*"),
    module.exports = { todos: require("./todos") }, import { api } from "./api"; export { api }."""

    name: str  # the name others import: "api", "todos", "default"; "*" passes every name of `source` on
    local: str = ""  # the variable of this file it stands for ("router" in export { router as todos })
    source: ImportRef | None = None  # the module a passed-on name comes from
    original: str = ""  # its name in `source`: "default" in export { default as api } from "./api"; "*" the module itself


_METHOD_WORDS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_ABSOLUTE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def helper_request(helper: HttpHelper, args: list, options: list, member: str = "") -> tuple[str, str] | None:
    """(method, raw address) of one call of a request helper, or None when the call gives no address.

    `args` holds each argument as address text, `options` what each options-object argument sets
    (see HelperCall). A function helper puts the path argument after its base, and takes the method
    from its method argument (or that parameter's default when the call leaves it out), from the
    options argument (fetch's GET when there is none), or else from a method word among the other
    arguments. A client's `member` is the method (http.post("/todos", body)), and its base goes in
    front of a relative path the way axios joins them.
    """
    if helper.client:
        method = member.upper()
        raw = args[0] if args else None
        if method not in _METHOD_WORDS or raw is None:
            return None
        if not _ABSOLUTE.match(raw) and not raw.startswith("${"):
            raw = helper.base.rstrip("/") + "/" + raw.lstrip("/")
        return method, raw
    if member:
        return None
    at = helper.url_at
    part = args[at] if at < len(args) else None
    if part is None:
        return None
    method = helper.method
    if helper.method_at is not None:
        if helper.method_at < len(args):
            word = (args[helper.method_at] or "").strip().upper()
            method = word if word in _METHOD_WORDS else "ANY"
        else:
            method = helper.method_default or "ANY"
    elif helper.options_at is not None:
        method = (options[helper.options_at] if helper.options_at < len(options) else None) or (
            "GET" if helper.options_at >= len(args) else "ANY")
    if method == "ANY":  # a method word among the other arguments: send("/todos", "POST")
        for i, arg in enumerate(args):
            word = (arg or "").strip().upper() if i != at else ""
            if word in _METHOD_WORDS:
                method = word
                break
    return method, helper.base + part


@dataclass
class FileFacts:
    """Everything one analyzer learned about one file."""

    path: str  # project-relative POSIX path
    language: str  # python | javascript | typescript
    lines: int = 0
    imports: list[ImportRef] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    http_calls: list[HttpCall] = field(default_factory=list)
    http_routes: list[HttpRoute] = field(default_factory=list)
    http_mounts: list[HttpMount] = field(default_factory=list)  # routers mounted here under a prefix
    http_helpers: list[HttpHelper] = field(default_factory=list)  # request helpers other files can call
    helper_calls: list[HelperCall] = field(default_factory=list)  # calls of request helpers imported from project files
    exports: list[ExportName] = field(default_factory=list)  # exported routers, helpers and plugins, and names passed on
    # Routes and mounts made on the first parameter of a Fastify plugin function the file exports when
    # the file does not import a server framework (module.exports = async function (fastify) {
    # fastify.get(...) }): the graph counts them only when the project uses Fastify.
    plugin_routes: list[HttpRoute] = field(default_factory=list)
    plugin_mounts: list[HttpMount] = field(default_factory=list)
    frameworks: set[str] = field(default_factory=set)  # canonical lowercase names, see architecture.md
    is_entry: bool = False  # file-local evidence that execution starts here
    entry_reason: str = ""  # Korean, e.g. "서버를 만드는 파일"
    module_doc: str = ""  # first sentence of the module docstring or leading comment, wrapped lines joined
    parse_error: bool = False


class ProjectIndex:
    """All files and folders the scanner kept, with cached text access.

    Paths are project-relative POSIX strings. The root folder is "". Files the scanner
    marked as too big (`skipped`) or binary are listed but never read.
    """

    def __init__(self, root: str | Path, files, dirs, *, skipped=(), binary=()):
        self.root = Path(root)
        self._root = os.fspath(self.root)  # for open(): a str path opens faster than a new Path per file
        self.files = frozenset(files)
        self.dirs = frozenset(dirs) | {""}
        self.unread = frozenset(skipped) | frozenset(binary)  # never read: over the size limit, or binary
        self._text: dict[str, str | None] = {}
        self._buffer: memoryview | None = None  # one buffer for every read, not a new big one per file

    def exists(self, rel: str) -> bool:
        return rel in self.files

    def is_dir(self, rel: str) -> bool:
        return rel in self.dirs

    def read_text(self, rel: str, limit: int = 2_000_000, *, cache: bool = True) -> str | None:
        """Return the file as text (UTF-8, falling back to cp949, then lossy), or None.

        At most `limit` bytes are read. None for files that are not in the index, cannot be
        opened, hold NUL bytes or were marked as too big or binary by the scanner. The text
        is kept for the next call unless `cache` is False (a text that is used only once).
        """
        if rel in self._text:
            return self._text[rel]
        text = None
        if rel in self.files and rel not in self.unread:
            limit = max(limit, 0)
            buffer = self._buffer
            if buffer is None or len(buffer) < limit:
                buffer = self._buffer = memoryview(bytearray(limit))
            try:
                with open(os.path.join(self._root, rel), "rb", buffering=0) as fh:
                    size = fh.readinto(buffer[:limit])  # at most `limit` bytes, however big the file is
            except (OSError, ValueError):  # locked, vanished, a folder now, a bad name
                size = None
            if size is not None and buffer.obj.find(b"\x00", 0, min(size, 8192)) == -1:
                # "utf-8-sig" by hand: the BOM is skipped here, and plain "utf-8" is decoded without
                # the Python-level codec wrapper that "utf-8-sig" goes through
                bom = 3 if buffer.obj.startswith(b"\xef\xbb\xbf", 0, size) else 0
                raw = buffer[bom:size]
                try:
                    text = str(raw, "utf-8")
                except UnicodeDecodeError:
                    raw.release()
                    raw = buffer[:size]
                    try:
                        text = str(raw, "cp949")
                    except UnicodeDecodeError:
                        text = str(raw, "utf-8", "replace")
                raw.release()
        if cache:
            self._text[rel] = text
        return text


def parent_dir(rel: str) -> str:
    """Parent folder of a project-relative path ("" for top-level entries)."""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def join_rel(base: str, *parts: str) -> str | None:
    """Join and normalize project-relative POSIX parts. Returns None if it escapes the root."""
    out: list[str] = [p for p in base.split("/") if p] if base else []
    for part in parts:
        for seg in part.replace("\\", "/").split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not out:
                    return None
                out.pop()
            else:
                out.append(seg)
    return "/".join(out)


_SCHEME_HOST = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]*")
_LEADING_PLACEHOLDERS = re.compile(r"^(?:\$\{[^}]*\})+")
_PARAM = re.compile(r"\{[^}/]*\}|<[^>/]*>|:[A-Za-z_]\w*|\$\{[^}]*\}|\[[^\]/]*\]")


def normalize_http_path(raw: str) -> str | None:
    """Turn a URL or route pattern into a comparable path, or None if it is not one.

    Template placeholders must be passed as ``${}`` (or ``${name}``). A leading
    placeholder is treated as a base URL and dropped. Path parameters in any
    style ({id}, <int:id>, :id, ${id}, [id]) become ``*``.

        "http://localhost:8000/todos"  -> "/todos"
        "${API}/todos/${id}"           -> "/todos/*"
        "/todos/{todo_id}"             -> "/todos/*"
        "/todos/<int:todo_id>/"        -> "/todos/*"
        "todos"                        -> None
    """
    if not raw:
        return None
    s = raw.strip()
    m = _SCHEME_HOST.match(s)
    if m:
        s = s[m.end():] or "/"
    s = _LEADING_PLACEHOLDERS.sub("", s)
    s = s.split("?", 1)[0].split("#", 1)[0]
    if not s.startswith("/"):
        return None
    s = _PARAM.sub("*", s)
    s = re.sub(r"/{2,}", "/", s)
    if len(s) > 1:
        s = s.rstrip("/") or "/"
    return s


def join_http_path(prefix: str, path: str) -> str | None:
    """A route path under a mount prefix, normalized: ("/api/todos", "/") -> "/api/todos"."""
    prefix = (prefix or "").strip()
    if not prefix.startswith(("/", "${")):
        prefix = "/" + prefix
    full = prefix.rstrip("/") + "/" + (path or "").lstrip("/")
    return normalize_http_path(full)


def strip_http_base(path: str, base: str) -> str:
    """A normalized route path without the router prefix it starts with: ("/todos/*", "/todos") -> "/*"."""
    head = [p for p in (base or "").split("/") if p]
    parts = [p for p in (path or "").split("/") if p]
    if head and parts[:len(head)] == head:
        return "/" + "/".join(parts[len(head):])
    return path


def http_paths_match(call: str, route: str) -> bool:
    """True if a client call path can reach a server route path.

    Segments match when equal or when either side is ``*``. A call may also carry
    extra leading segments (a router mounted under a prefix such as /api), as
    long as one literal segment of the route equals the call's: /api/todos/*
    reaches /todos/*, but /todos/* does not reach /health through its wildcard alone.
    """
    a = [p for p in call.split("/") if p]
    b = [p for p in route.split("/") if p]

    def seg(x: str, y: str) -> bool:
        return x == y or x == "*" or y == "*"

    if len(a) == len(b):
        return all(seg(x, y) for x, y in zip(a, b))
    if b and len(a) > len(b):
        tail = a[-len(b):]
        return all(seg(x, y) for x, y in zip(tail, b)) and any(x == y != "*" for x, y in zip(tail, b))
    return False


def http_methods_match(m1: str, m2: str) -> bool:
    return m1 == m2 or m1 == "ANY" or m2 == "ANY"
