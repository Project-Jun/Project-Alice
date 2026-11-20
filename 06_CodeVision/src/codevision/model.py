"""Shared data model for the CodeVision analyzers, graph builder and server.

Each analyzer in ``codevision.lang`` turns one source file into ``FileFacts``.
``codevision.graph`` combines them into the JSON graph that the web UI renders.
The full contract is in docs/architecture.md.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

AREAS = ("ui", "sv", "ot")


@dataclass
class ImportRef:
    """One import, require or re-export, as written in the source."""

    spec: str  # raw specifier: "./api", "react", "routes.todos"; "" for `from . import x`
    names: list[str] = field(default_factory=list)  # imported names, if any
    kind: str = "import"  # import | from | require | dynamic | reexport | side-effect
    level: int = 0  # Python relative-import level (number of leading dots)
    line: int = 0


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
    Only the Python analyzer fills `base`, `replace` and `base_own`; JavaScript mounts keep the defaults.
    """

    prefix: str  # raw prefix text, "${}" for unknown parts; normalized when it is applied
    target: ImportRef
    line: int = 0
    base: str = ""  # raw prefix the router the target is mounted on answers under in this file ("" for an app)
    # Flask: the url_prefix takes the place of the mounted blueprint's own prefix (the routes'
    # `base`) instead of going in front of it. FastAPI and Express put prefixes in front.
    replace: bool = False
    base_own: str = ""  # raw start of `base` that a Flask url_prefix mounting this file takes the place of


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
