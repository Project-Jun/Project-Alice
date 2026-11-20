"""JavaScript / TypeScript analyzer.

Parses with tree-sitter when it is installed. Without it (TREE_SITTER = False)
a regular-expression fallback still finds imports, requires, dynamic imports,
HTTP calls, routes and simple top-level functions.
The analyzer contract is in docs/architecture.md, section 3.
"""
from __future__ import annotations

import bisect
import json
import re
import weakref

from codevision.describe import is_boilerplate, join_wrapped
from codevision.model import (
    FileFacts,
    HttpCall,
    HttpMount,
    HttpRoute,
    ImportRef,
    ProjectIndex,
    Symbol,
    join_http_path,
    join_rel,
    normalize_http_path,
    parent_dir,
)

try:
    import tree_sitter_javascript
    import tree_sitter_typescript
    from tree_sitter import Language, Parser

    TREE_SITTER = True
except Exception:  # pragma: no cover - depends on the installed packages
    TREE_SITTER = False

EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")
_RESOLVE_EXTENSIONS = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
    ".vue", ".svelte", ".json", ".css", ".scss", ".sass", ".less",
)
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}

COMPONENT_HINT = "화면 조각(컴포넌트)"
UI_ENTRY = "화면을 처음 띄우는 파일"
SERVER_ENTRY = "서버를 켜는 파일"
PACKAGE_ENTRY = "package.json에 적힌 시작 파일"
HTML_ENTRY = "{name}에서 처음 불러오는 파일"

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_ROUTE_METHODS = _HTTP_METHODS + ("all",)
_KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_CLIENTS = frozenset({"axios", "ky", "ofetch", "$fetch"})
_CLIENT_PACKAGES = frozenset({"axios", "ky", "ofetch", "redaxios", "ky-universal"})
# Names that look like an imported HTTP client instance (api.get("/todos")).
_CLIENT_LIKE = re.compile(r"(?i)api|http|client|axios|fetcher")
_NOT_CLIENT = re.compile(r"(?i)router|server")
# Objects that look like a server app even when the file does not import the framework.
_ROUTER_LIKE = re.compile(r"(?i)^(?:app|server|\w*router)$")
_ROUTE_FRAMEWORKS = frozenset({"express", "koa", "fastify", "hono"})
_ABSOLUTE_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_ABSOLUTE_HOST = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]*")
_LEADING_HOLES = re.compile(r"^(?:\$\{\})+")

_FRAMEWORKS = {
    "react": "react", "react-dom": "react",
    "vue": "vue", "svelte": "svelte", "next": "next",
    "express": "express", "koa": "koa", "koa-router": "koa", "@koa/router": "koa",
    "fastify": "fastify", "hono": "hono",
    "@prisma/client": "prisma", "mongodb": "mongodb", "mongoose": "mongodb",
    "pg": "postgres", "postgres": "postgres", "mysql2": "mysql", "mysql": "mysql",
    "sqlite3": "sqlite", "better-sqlite3": "sqlite", "sqlite": "sqlite",
    "node:sqlite": "sqlite", "bun:sqlite": "sqlite",
    "sequelize": "sequelize", "typeorm": "typeorm",
    "firebase": "firebase", "firebase-admin": "firebase",
    "redis": "redis", "ioredis": "redis",
}
_FRAMEWORK_SCOPES = {"@angular": "angular", "@nestjs": "nestjs", "@supabase": "supabase"}

_FUNCTION_TYPES = ("arrow_function", "function_expression", "function", "generator_function")
_JSX_TYPES = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})
_PRAGMA = re.compile(
    r"(?i)^(?:eslint|prettier|@ts-|ts-|@flow|jshint|jslint|globals?\s|istanbul|c8\s|@jsx|"
    r"@vitest|@jest|#(?:end)?region|<reference|webpack|use strict)"
)


# ---------------------------------------------------------------- analyze


def analyze(rel_path: str, source: str) -> FileFacts:
    ext = _ext(rel_path)
    language = "typescript" if ext in _TS_EXTENSIONS else "javascript"
    lines = len(source.splitlines())
    facts = FileFacts(path=rel_path, language=language, lines=lines)
    try:
        if TREE_SITTER:
            _TreeAnalyzer(facts, source, ext).run()
        else:
            _regex_analyze(facts, source, ext)
    except Exception:
        facts = FileFacts(path=rel_path, language=language, lines=lines, parse_error=True)
        try:
            _regex_analyze(facts, source, ext)
        except Exception:
            pass
    try:
        _next_file_routes(facts)
    except Exception:
        pass
    facts.imports.sort(key=lambda i: i.line)
    facts.http_calls.sort(key=lambda c: c.line)
    facts.http_routes.sort(key=lambda r: r.line)
    return facts


def _ext(rel_path: str) -> str:
    name = rel_path.rsplit("/", 1)[-1].lower()
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


_LANGUAGES: dict = {}


def _parser(ext: str):
    key = "tsx" if ext == ".tsx" else "ts" if ext in _TS_EXTENSIONS else "js"
    lang = _LANGUAGES.get(key)
    if lang is None:
        if key == "js":
            capsule = tree_sitter_javascript.language()
        elif key == "ts":
            capsule = tree_sitter_typescript.language_typescript()
        else:
            capsule = tree_sitter_typescript.language_tsx()
        lang = _LANGUAGES[key] = Language(capsule)
    return Parser(lang)


def _descendants(node):
    """Pre-order walk of a node and everything below it."""
    cursor = node.walk()
    while True:
        yield cursor.node
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return


class _TreeAnalyzer:
    def __init__(self, facts: FileFacts, source: str, ext: str):
        self.facts = facts
        self.ext = ext
        self.src = source.encode("utf-8", "replace")
        self.consts: dict[str, str] = {}  # top-level string constants, for URLs
        self.clients = set(_CLIENTS)
        self.client_bases: dict[str, str] = {}
        self.calls: list = []
        self.news: list = []
        self.declarators: list = []
        self.exported_names: set[str] = set()
        self.spans: list = []  # (Symbol, start_byte, end_byte)
        self.http_spans: list = []  # (start_byte, HttpCall)
        self.route_owners: list = []  # (HttpRoute, variable the route was added to)
        self.local_mounts: list = []  # (prefix, variable): a router of this file mounted under a prefix
        self.bindings: dict | None = None  # imported name -> ImportRef
        self.functions: list = []  # function declarations at any depth
        self.wrappers: dict = {}  # request helper name -> (url template, url arg, method, method arg, options arg)
        self.wrapper_fetches: set = set()  # start bytes of the fetch calls inside request helpers
        self.route_file = False
        self.vue_app = False
        self.vue_mount = False

    # --- helpers
    def text(self, node) -> str:
        return self.src[node.start_byte:node.end_byte].decode("utf-8", "replace")

    @staticmethod
    def args(call) -> list:
        node = call.child_by_field_name("arguments")
        if node is None:
            return []
        return [a for a in node.named_children if a.type != "comment"]

    def literal(self, node) -> str | None:
        """Value of a plain string literal (or a template without ${})."""
        if node is None:
            return None
        if node.type == "string":
            return self.text(node)[1:-1]
        if node.type == "template_string" and not any(
            c.type == "template_substitution" for c in node.named_children
        ):
            return self.text(node)[1:-1]
        return None

    def url(self, node, depth: int = 0) -> str | None:
        """Best-effort URL text; unknown parts become ${}."""
        if node is None or depth > 20:
            return None
        t = node.type
        if t == "string":
            return self.text(node)[1:-1]
        if t == "template_string":
            out, pos = [], node.start_byte + 1
            for ch in node.named_children:
                if ch.type != "template_substitution":
                    continue
                out.append(self.src[pos:ch.start_byte].decode("utf-8", "replace"))
                inner = [c for c in ch.named_children if c.type != "comment"]
                value = None
                if len(inner) == 1 and inner[0].type == "identifier":
                    value = self.consts.get(self.text(inner[0]))
                out.append("${}" if value is None else value)
                pos = ch.end_byte
            out.append(self.src[pos:node.end_byte - 1].decode("utf-8", "replace"))
            return "".join(out)
        if t == "binary_expression":
            op = node.child_by_field_name("operator")
            if op is None or self.text(op) != "+":
                return "${}"
            left = self.url(node.child_by_field_name("left"), depth + 1)
            right = self.url(node.child_by_field_name("right"), depth + 1)
            if left is None or right is None:
                return None
            return left + right
        if t == "parenthesized_expression":
            inner = [c for c in node.named_children if c.type != "comment"]
            return self.url(inner[0], depth + 1) if inner else None
        if t == "identifier":
            return self.consts.get(self.text(node), "${}")
        if t == "number":
            return self.text(node)
        if t in ("member_expression", "call_expression", "subscript_expression", "await_expression",
                 "ternary_expression", "as_expression", "non_null_expression"):
            return "${}"
        return None

    def key_name(self, key) -> str:
        return self.text(key)[1:-1] if key.type == "string" else self.text(key)

    def prop(self, obj, key: str):
        """Value node of `key` in an object literal, or None."""
        if obj is None or obj.type != "object":
            return None
        for ch in obj.named_children:
            if ch.type == "pair":
                k = ch.child_by_field_name("key")
                if k is not None and self.key_name(k) == key:
                    return ch.child_by_field_name("value")
            elif ch.type == "shorthand_property_identifier" and self.text(ch) == key:
                return ch
        return None

    def method(self, obj, default: str = "GET") -> str:
        value = self.prop(obj, "method")
        if value is None:
            return default
        lit = self.literal(value)
        if lit is None:
            return "ANY"
        lit = lit.strip().upper()
        return lit if lit in _KNOWN_METHODS else "ANY"

    def object_name(self, node) -> str:
        if node.type == "identifier":
            return self.text(node)
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            return self.text(prop) if prop is not None else ""
        return ""

    def callee_name(self, call) -> str:
        fn = call.child_by_field_name("function")
        if fn is None:
            return ""
        return self.object_name(fn) if fn.type in ("identifier", "member_expression") else ""

    def entry(self, reason: str):
        if not self.facts.is_entry:
            self.facts.is_entry = True
            self.facts.entry_reason = reason

    # --- main flow
    def run(self):
        root = _parser(self.ext).parse(self.src).root_node
        self.facts.parse_error = root.has_error
        for stmt in root.named_children:
            self.top_level(stmt, stmt, False)
        self.facts.module_doc = self.module_doc(root)
        for node in _descendants(root):
            t = node.type
            if t == "call_expression":
                self.calls.append(node)
            elif t == "import_statement":
                self.import_statement(node)
            elif t == "export_statement":
                src = node.child_by_field_name("source")
                if src is not None:
                    self.reexport(node, src)
            elif t == "variable_declarator":
                self.declarators.append(node)
            elif t == "new_expression":
                self.news.append(node)
            elif t in ("function_declaration", "generator_function_declaration"):
                self.functions.append(node)
        # require() and import() are imports too, so collect them before reading frameworks.
        for call in self.calls:
            self.import_call(call)
        self.facts.frameworks = _frameworks(self.facts.imports)
        self.route_file = bool(self.facts.frameworks & _ROUTE_FRAMEWORKS)
        for node in self.declarators:
            self.client_instance(node)
        self.find_wrappers()
        for call in self.calls:
            self.call(call)
        for node in self.news:
            self.new_expression(node)
        if self.vue_app and self.vue_mount:
            self.entry(UI_ENTRY)
        self.finish()

    def finish(self):
        _mount_local_routes(self.facts, self.route_owners, self.local_mounts)
        for sym in self.facts.symbols:
            if sym.name in self.exported_names:
                sym.exported = True
        starts = [pos for pos, _ in self.http_spans]  # already in document order
        for sym, start, end in self.spans:
            i = bisect.bisect_left(starts, start)
            if not sym.hint and i < len(starts) and starts[i] < end:
                call = self.http_spans[i][1]
                sym.hint = f"{call.method} · {call.path}"

    # --- module doc and symbols
    def module_doc(self, root) -> str:
        symbol_lines = {s.line for s in self.facts.symbols}
        block: list[str] = []  # consecutive // lines
        last_row = -2
        for ch in root.children:
            t = ch.type
            if t == "hash_bang_line":
                continue
            if t == "comment" and self.text(ch).startswith("//"):
                if block and ch.start_point.row != last_row + 1:
                    line = _line_block_doc(block)
                    if line:
                        return line
                    block = []
                block.append(self.text(ch))
                last_row = ch.end_point.row
                continue
            if block:  # a // block ends at anything else
                line = _line_block_doc(block)
                if line:
                    return line
                block = []
            if t == "expression_statement":
                inner = ch.named_children
                if len(inner) == 1 and inner[0].type == "string":  # "use client"
                    continue
                return ""
            if t != "comment":
                return ""
            text = self.text(ch)
            nxt = ch.next_sibling
            if (text.startswith("/**") and nxt is not None and nxt.start_point.row + 1 in symbol_lines
                    and nxt.start_point.row <= ch.end_point.row + 1):
                return ""  # JSDoc of the first function, not of the file
            line = _comment_line(text)
            if line and not _PRAGMA.match(line) and not is_boilerplate(line):
                return line
        return _line_block_doc(block) if block else ""

    def jsdoc(self, anchor) -> str:
        prev = anchor.prev_sibling
        if prev is None or prev.type != "comment":
            return ""
        text = self.text(prev)
        if not text.startswith("/**") or prev.end_point.row < anchor.start_point.row - 1:
            return ""
        return _comment_line(text)

    def top_level(self, node, anchor, exported: bool):
        t = node.type
        if t == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is not None:
                self.top_level(decl, anchor, True)
                return
            value = node.child_by_field_name("value")
            if value is not None:  # export default X / export default memo(X)
                if value.type == "identifier":
                    self.exported_names.add(self.text(value))
                elif value.type == "call_expression":
                    for a in self.args(value):
                        if a.type == "identifier":
                            self.exported_names.add(self.text(a))
                elif value.type in _FUNCTION_TYPES:
                    # export default () => ...: name it after the file (TodoItem.jsx -> TodoItem)
                    stem = self.facts.path.rsplit("/", 1)[-1].split(".", 1)[0]
                    if re.fullmatch(r"[A-Za-z_$][\w$]*", stem):
                        self.add_function(stem, value, anchor, True)
                return
            if node.child_by_field_name("source") is None:
                for clause in node.named_children:
                    if clause.type != "export_clause":
                        continue
                    for spec in clause.named_children:
                        name = spec.child_by_field_name("name") if spec.type == "export_specifier" else None
                        if name is not None:
                            self.exported_names.add(self.text(name))
            return
        if t in ("function_declaration", "generator_function_declaration"):
            name = node.child_by_field_name("name")
            if name is not None:
                self.add_function(self.text(name), node, anchor, exported)
        elif t in ("class_declaration", "abstract_class_declaration"):
            name = node.child_by_field_name("name")
            if name is not None:
                self.add_class(self.text(name), node, anchor, exported)
        elif t in ("lexical_declaration", "variable_declaration"):
            for decl in node.named_children:
                if decl.type == "variable_declarator":
                    self.top_declarator(decl, anchor, exported)
        elif t == "expression_statement":
            self.commonjs_export(node, anchor)

    def top_declarator(self, decl, anchor, exported: bool):
        name = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name is None or value is None or name.type != "identifier":
            return
        var = self.text(name)
        if value.type in _FUNCTION_TYPES:
            self.add_function(var, value, anchor, exported)
        elif value.type == "call_expression" and self.callee_name(value) in ("memo", "forwardRef"):
            inner = self.args(value)
            if inner and inner[0].type in _FUNCTION_TYPES:
                self.add_function(var, inner[0], anchor, exported)
        elif value.type in ("string", "template_string", "binary_expression"):
            url = self.url(value)
            if url is not None:
                self.consts[var] = url

    def commonjs_export(self, stmt, anchor):
        expr = stmt.named_children[0] if stmt.named_children else None
        if expr is None or expr.type != "assignment_expression":
            return
        left = expr.child_by_field_name("left")
        right = expr.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            return
        target = re.sub(r"\s+", "", self.text(left))
        if target == "module.exports":
            if right.type == "identifier":
                self.exported_names.add(self.text(right))
            elif right.type == "object":
                for ch in right.named_children:
                    if ch.type == "shorthand_property_identifier":
                        self.exported_names.add(self.text(ch))
                    elif ch.type == "pair":
                        value = ch.child_by_field_name("value")
                        if value is not None and value.type == "identifier":
                            self.exported_names.add(self.text(value))
        elif target.startswith(("exports.", "module.exports.")):
            prop = left.child_by_field_name("property")
            if prop is None:
                return
            if right.type in _FUNCTION_TYPES:
                self.add_function(self.text(prop), right, anchor, True)
            elif right.type == "identifier":
                self.exported_names.add(self.text(right))

    def add_function(self, name: str, fn, anchor, exported: bool):
        params = fn.child_by_field_name("parameters")
        if params is None:
            params = fn.child_by_field_name("parameter")  # x => ...
        inner = ""
        if params is not None:
            inner = self.text(params)
            if params.type == "formal_parameters":
                inner = inner[1:-1]
        body = fn.child_by_field_name("body")
        component = name[:1].isupper() and body is not None and _has_jsx(body)
        self.add_symbol(Symbol(
            name=name,
            kind="component" if component else "function",
            signature=f"{name}({_squeeze_params(inner)})",
            doc=self.jsdoc(anchor),
            hint=COMPONENT_HINT if component else "",
            line=anchor.start_point.row + 1,
            exported=exported,
        ), fn)

    def add_class(self, name: str, node, anchor, exported: bool):
        body = node.child_by_field_name("body")
        component = body is not None and _has_jsx(body)
        self.add_symbol(Symbol(
            name=name,
            kind="component" if component else "class",
            signature=f"class {name}",
            doc=self.jsdoc(anchor),
            hint=COMPONENT_HINT if component else "",
            line=anchor.start_point.row + 1,
            exported=exported,
        ), node)

    def add_symbol(self, sym: Symbol, node):
        self.facts.symbols.append(sym)
        self.spans.append((sym, node.start_byte, node.end_byte))

    # --- imports
    def add_import(self, spec: str, names: list[str], kind: str, line: int):
        spec = spec.strip()
        if not spec:
            return
        self.facts.imports.append(ImportRef(spec=spec, names=names, kind=kind, line=line))
        if kind in ("import", "require") and _package_name(spec) in _CLIENT_PACKAGES:
            self.clients.update(names)

    def import_statement(self, node):
        src = node.child_by_field_name("source")
        require_clause = None
        if src is None:  # TypeScript: import fs = require("fs")
            for ch in node.named_children:
                if ch.type == "import_require_clause":
                    require_clause = ch
                    src = ch.child_by_field_name("source")
        spec = self.literal(src)
        if spec is None:
            return
        line = node.start_point.row + 1
        if require_clause is not None:
            names = [self.text(c) for c in require_clause.named_children if c.type == "identifier"]
            self.add_import(spec, names[:1], "require", line)
            return
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            self.add_import(spec, [], "side-effect", line)
            return
        names = []
        for ch in clause.named_children:
            if ch.type == "identifier":
                names.append(self.text(ch))
            elif ch.type == "namespace_import":
                ids = [c for c in ch.named_children if c.type == "identifier"]
                if ids:
                    names.append(self.text(ids[-1]))
            elif ch.type == "named_imports":
                for item in ch.named_children:
                    if item.type != "import_specifier":
                        continue
                    alias = item.child_by_field_name("alias")
                    name = alias if alias is not None else item.child_by_field_name("name")
                    if name is not None:
                        names.append(self.text(name))
        self.add_import(spec, names, "import", line)

    def reexport(self, node, src):
        spec = self.literal(src)
        if spec is None:
            return
        names, star = [], False
        for ch in node.children:
            if ch.type == "export_clause":
                for item in ch.named_children:
                    if item.type != "export_specifier":
                        continue
                    alias = item.child_by_field_name("alias")
                    name = alias if alias is not None else item.child_by_field_name("name")
                    if name is not None:
                        names.append(self.text(name))
            elif ch.type == "namespace_export":
                ids = [c for c in ch.named_children if c.type == "identifier"]
                if ids:
                    names.append(self.text(ids[-1]))
            elif ch.type == "*":
                star = True
        if star and not names:
            names = ["*"]
        self.add_import(spec, names, "reexport", node.start_point.row + 1)

    def import_call(self, node):
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "import":
            args = self.args(node)
            spec = self.literal(args[0]) if args else None
            if spec:
                self.add_import(spec, [], "dynamic", node.start_point.row + 1)
        elif fn.type == "identifier" and self.text(fn) == "require":
            args = self.args(node)
            spec = self.literal(args[0]) if args else None
            if spec:
                self.add_import(spec, self.binding_names(node), "require", node.start_point.row + 1)

    def binding_names(self, call) -> list[str]:
        parent = call.parent
        if parent is None or parent.type != "variable_declarator":
            return []
        name = parent.child_by_field_name("name")
        if name is None:
            return []
        if name.type == "identifier":
            return [self.text(name)]
        out = []
        if name.type == "object_pattern":
            for ch in name.named_children:
                if ch.type == "shorthand_property_identifier_pattern":
                    out.append(self.text(ch))
                elif ch.type == "pair_pattern":
                    value = ch.child_by_field_name("value")
                    if value is not None and value.type == "identifier":
                        out.append(self.text(value))
        return out

    # --- HTTP clients, routes, entry clues
    def client_instance(self, decl):
        """const api = axios.create({ baseURL }) makes `api` an HTTP client too."""
        name = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name is None or value is None or name.type != "identifier" or value.type != "call_expression":
            return
        fn = value.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        if obj is None or prop is None or self.object_name(obj) not in self.clients:
            return
        if self.text(prop) not in ("create", "extend"):
            return
        var = self.text(name)
        self.clients.add(var)
        args = self.args(value)
        if args and args[0].type == "object":
            base = self.prop(args[0], "baseURL")
            if base is None:
                base = self.prop(args[0], "prefixUrl")
            base_url = self.url(base) if base is not None else None
            if base_url:
                self.client_bases[var] = base_url

    def add_call(self, method: str, raw: str | None, node):
        path = normalize_http_path(raw) if raw else None
        if path is None:
            return
        call = HttpCall(method=method, path=path, line=node.start_point.row + 1)
        self.facts.http_calls.append(call)
        self.http_spans.append((node.start_byte, call))

    def client_call(self, client: str, method: str, url_node, node):
        raw = self.url(url_node)
        if raw is None:
            return
        if not _ABSOLUTE_URL.match(raw) and not raw.startswith("${"):
            base = self.client_bases.get(client, "")
            raw = base.rstrip("/") + "/" + raw.lstrip("/")
        self.add_call(method, raw, node)

    def client_config(self, client: str, node, args):
        """axios({ url, method }) or axios(url, { method })."""
        if not args:
            return
        if args[0].type == "object":
            url_node = self.prop(args[0], "url")
            method = self.method(args[0])
        else:
            url_node = args[0]
            method = self.method(args[1]) if len(args) > 1 else "GET"
        if url_node is not None:
            self.client_call(client, method, url_node, node)

    def call(self, node):
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        args = self.args(node)
        if fn.type == "identifier":
            name = self.text(fn)
            if name == "fetch":
                self.fetch(node, args)
            elif name in self.clients:
                self.client_config(name, node, args)
            elif name in self.wrappers:
                self.wrapper_call(name, node, args)
            elif name in ("createRoot", "hydrateRoot", "bootstrapApplication"):
                self.entry(UI_ENTRY)
            elif name in ("createApp", "createSSRApp"):
                self.vue_app = True
            elif name in ("mount", "hydrate") and len(args) > 1 and self.prop(args[1], "target") is not None:
                self.entry(UI_ENTRY)  # Svelte 5: mount(App, { target })
            elif name == "serve" and any(i.spec == "@hono/node-server" for i in self.facts.imports):
                self.entry(SERVER_ENTRY)
            return
        if fn.type != "member_expression":
            return
        obj = fn.child_by_field_name("object")
        prop_node = fn.child_by_field_name("property")
        if obj is None or prop_node is None:
            return
        prop = self.text(prop_node)
        obj_name = self.object_name(obj)
        if prop == "fetch" and obj_name in ("window", "globalThis", "self"):
            self.fetch(node, args)
        elif prop in ("createRoot", "hydrateRoot", "bootstrapModule"):
            self.entry(UI_ENTRY)
        elif prop in ("render", "hydrate") and obj_name == "ReactDOM":
            self.entry(UI_ENTRY)
        elif prop in ("mount", "$mount"):
            self.vue_mount = True
        elif prop == "listen" and args and args[0].type not in _FUNCTION_TYPES:
            self.entry(SERVER_ENTRY)
        elif prop == "serve" and obj_name in ("Bun", "Deno"):
            self.entry(SERVER_ENTRY)
        elif prop == "request" and obj_name in self.clients:
            self.client_config(obj_name, node, args)
        elif prop == "route" and self.route_file and args and args[0].type == "object":
            self.object_route(node, args[0])  # fastify.route({ method, url })
        elif prop == "use" and len(args) >= 2:
            self.mount(obj_name, args[0], args[-1], node)  # Express, Koa router: app.use("/api", router)
        elif prop == "route" and len(args) == 2 and args[0].type in ("string", "template_string"):
            self.mount(obj_name, args[0], args[1], node)  # Hono: app.route("/api", api)
        elif prop == "register" and len(args) >= 2 and args[1].type == "object":
            prefix = self.prop(args[1], "prefix")  # Fastify: app.register(routes, { prefix: "/api" })
            if prefix is not None:
                self.mount(obj_name, prefix, args[0], node)
        elif prop in _ROUTE_METHODS:
            self.member_http(node, obj, obj_name, prop, args)

    def fetch(self, node, args):
        if not args or node.start_byte in self.wrapper_fetches:
            return  # inside a request helper the URL is its parameter: the helper's callers say where
        method = self.method(args[1]) if len(args) > 1 else "GET"
        self.add_call(method, self.url(args[0]), node)

    # --- request helpers: async function api(url, method) { ... fetch(url, {...}) }
    def params(self, fn) -> list:
        """Parameter names of a function in order (None for a destructured one)."""
        node = fn.child_by_field_name("parameters")
        if node is None:
            single = fn.child_by_field_name("parameter")  # x => ...
            return [self.text(single)] if single is not None and single.type == "identifier" else []
        out = []
        for p in node.named_children:
            if p.type == "comment":
                continue
            if p.type == "identifier":
                out.append(self.text(p))
                continue
            inner = p.child_by_field_name("left" if p.type == "assignment_pattern" else "pattern")
            out.append(self.text(inner) if inner is not None and inner.type == "identifier" else None)
        return out

    def find_wrappers(self):
        found = [(self.text(n), f) for f in self.functions if (n := f.child_by_field_name("name")) is not None]
        for decl in self.declarators:
            name, value = decl.child_by_field_name("name"), decl.child_by_field_name("value")
            if name is not None and value is not None and name.type == "identifier" and value.type in _FUNCTION_TYPES:
                found.append((self.text(name), value))
        for name, fn in found:
            if name != "fetch" and name not in self.clients and name not in self.wrappers:
                spec = self.wrapper_spec(fn)
                if spec:
                    self.wrappers[name] = spec

    def wrapper_spec(self, fn):
        """How a helper builds its request: the URL from one of its parameters, and the method."""
        params = self.params(fn)
        body = fn.child_by_field_name("body")
        if not any(params) or body is None:
            return None
        for node in _descendants(body):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if callee is None or not (
                    (callee.type == "identifier" and self.text(callee) == "fetch")
                    or (callee.type == "member_expression" and self.object_name(callee) == "fetch"
                        and self.object_name(callee.child_by_field_name("object")) in ("window", "globalThis", "self"))):
                continue
            args = self.args(node)
            if not args:
                continue
            saved = self.consts
            self.consts = dict(saved, **{p: f"\x00{i}\x00" for i, p in enumerate(params) if p})
            try:
                raw = self.url(args[0])
            finally:
                self.consts = saved
            slots = re.findall(r"\x00(\d+)\x00", raw or "")
            if len(slots) != 1 or not raw.endswith("\x00"):
                continue
            # The parameter must be the address itself, or what follows a base URL (BASE + path, "/api" + path).
            # `/todos/${id}` and `?path=${x}` take an id or a value: that function makes one request itself.
            base = _LEADING_HOLES.sub("", _ABSOLUTE_HOST.sub("", raw[:raw.index("\x00")]))
            if base.endswith(("/", "=", "?", "&", "#")) or (base and not base.startswith("/")):
                continue
            method, method_at, options_at = "GET", None, None
            if len(args) > 1:
                opt = args[1]
                if opt.type == "identifier" and self.text(opt) in params:  # fetch(url, options)
                    method, options_at = "ANY", params.index(self.text(opt))
                elif opt.type == "object":
                    value = self.prop(opt, "method")
                    lit = self.literal(value) if value is not None else None
                    if value is None:
                        method = "GET"
                    elif lit is not None:
                        method = lit.strip().upper() if lit.strip().upper() in _KNOWN_METHODS else "ANY"
                    elif value.type in ("identifier", "shorthand_property_identifier") and self.text(value) in params:
                        method, method_at = "ANY", params.index(self.text(value))
                    else:
                        method = "ANY"  # method: post ? "POST" : "GET"
                else:
                    method = "ANY"
            self.wrapper_fetches.add(node.start_byte)
            return raw, int(slots[0]), method, method_at, options_at
        return None

    def wrapper_call(self, name: str, node, args):
        """api("/api/pick-folder", "POST") with `api` a request helper."""
        template, url_at, method, method_at, options_at = self.wrappers[name]
        part = self.url(args[url_at]) if url_at < len(args) else None
        if part is None:
            return
        if method_at is not None:
            lit = self.literal(args[method_at]) if method_at < len(args) else None
            method = lit.strip().upper() if lit and lit.strip().upper() in _KNOWN_METHODS else "ANY"
        elif options_at is not None:
            opt = args[options_at] if options_at < len(args) else None
            method = "GET" if opt is None else self.method(opt) if opt.type == "object" else "ANY"
        if method == "ANY":  # a method word among the other arguments
            for i, arg in enumerate(args):
                lit = self.literal(arg) if i != url_at else None
                if lit and lit.strip().upper() in _KNOWN_METHODS:
                    method = lit.strip().upper()
                    break
        self.add_call(method, template.replace(f"\x00{url_at}\x00", part), node)

    def member_http(self, node, obj, obj_name: str, prop: str, args):
        if obj_name in self.clients and obj.type in ("identifier", "member_expression"):
            if prop != "all" and args:
                self.client_call(obj_name, prop.upper(), args[0], node)
            return
        handler_like = bool(args) and (args[-1].type in _FUNCTION_TYPES or args[-1].type == "identifier")
        if self.route_file or (_ROUTER_LIKE.match(obj_name or "") and handler_like):
            if self.route(node, obj, prop, args):
                return
        if prop != "all" and args and obj_name and _CLIENT_LIKE.search(obj_name) and not _NOT_CLIENT.search(obj_name):
            self.add_call(prop.upper(), self.url(args[0]), node)

    def route(self, node, obj, prop: str, args) -> bool:
        raw = self.route_chain_path(obj)
        if raw is None:
            if len(args) < 2:
                return False
            raw = self.url(args[0])
        path = normalize_http_path(raw) if raw else None
        if path is None:
            return False
        handler = ""
        if args and args[-1].type in ("identifier", "member_expression"):
            handler = self.text(args[-1])
        method = "ANY" if prop == "all" else prop.upper()
        route = HttpRoute(method=method, path=path, handler=handler, line=node.start_point.row + 1)
        self.facts.http_routes.append(route)
        self.route_owners.append((route, self.root_name(obj)))
        return True

    def root_name(self, node) -> str:
        """The variable at the start of router.route("/x").get(h): "router"."""
        for _ in range(50):
            if node is None:
                return ""
            if node.type == "identifier":
                return self.text(node)
            if node.type == "call_expression":
                node = node.child_by_field_name("function")
            elif node.type == "member_expression":
                node = node.child_by_field_name("object")
            else:
                return ""
        return ""

    def mount(self, obj_name: str, path_node, target_node, node):
        """app.use("/api/todos", todos): the routes of `todos` answer under /api/todos."""
        if not (self.route_file or _ROUTER_LIKE.match(obj_name or "")):
            return
        raw = self.url(path_node)
        if not raw or normalize_http_path(raw) is None:
            return  # app.use(express.json()) or a prefix that is not a path
        target = self.mount_target(target_node)
        if isinstance(target, ImportRef):
            self.facts.http_mounts.append(HttpMount(prefix=raw, target=target, line=node.start_point.row + 1))
        elif target:
            self.local_mounts.append((raw, target))

    def mount_target(self, node, depth: int = 0):
        """The import a mounted router comes from, or the name of a router made in this file."""
        if node is None or depth > 10:
            return None
        t = node.type
        if t == "identifier":
            name = self.text(node)
            if self.bindings is None:
                self.bindings = {}
                for imp in self.facts.imports:
                    if imp.kind in ("import", "require"):
                        for bound in imp.names:
                            self.bindings.setdefault(bound, imp)
            return self.bindings.get(name, name)
        if t == "member_expression":  # todos.router
            return self.mount_target(node.child_by_field_name("object"), depth + 1)
        if t == "await_expression":
            inner = [c for c in node.named_children if c.type != "comment"]
            return self.mount_target(inner[0], depth + 1) if inner else None
        if t == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None:
                return None
            if fn.type == "import" or (fn.type == "identifier" and self.text(fn) == "require"):
                args = self.args(node)  # app.use("/api", require("./routes"))
                spec = self.literal(args[0]) if args else None
                kind = "dynamic" if fn.type == "import" else "require"
                return ImportRef(spec=spec, kind=kind, line=node.start_point.row + 1) if spec else None
            if fn.type == "member_expression":  # todos.routes() (Koa)
                return self.mount_target(fn.child_by_field_name("object"), depth + 1)
            if fn.type in ("identifier", "call_expression"):  # makeTodosRouter(db), require("./todos")(db)
                target = self.mount_target(fn, depth + 1)
                return target if isinstance(target, ImportRef) else None
        return None

    def route_chain_path(self, obj) -> str | None:
        """Path of router.route("/x") in router.route("/x").get(h).post(h)."""
        node = obj
        while node is not None and node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                return None
            prop = fn.child_by_field_name("property")
            name = self.text(prop) if prop is not None else ""
            if name == "route":
                args = self.args(node)
                return self.url(args[0]) if args else None
            if name not in _ROUTE_METHODS:
                return None
            node = fn.child_by_field_name("object")
        return None

    def object_route(self, node, obj):
        url_node = self.prop(obj, "url")
        if url_node is None:
            url_node = self.prop(obj, "path")
        raw = self.url(url_node) if url_node is not None else None
        path = normalize_http_path(raw) if raw else None
        if path is None:
            return
        method_node = self.prop(obj, "method")
        lit = self.literal(method_node) if method_node is not None else None
        method = lit.upper() if lit and lit.upper() in _KNOWN_METHODS else "ANY"
        handler_node = self.prop(obj, "handler")
        handler = self.text(handler_node) if handler_node is not None and handler_node.type == "identifier" else ""
        self.facts.http_routes.append(
            HttpRoute(method=method, path=path, handler=handler, line=node.start_point.row + 1)
        )

    def new_expression(self, node):
        ctor = node.child_by_field_name("constructor")
        if ctor is None or ctor.type != "identifier":
            return
        name = self.text(ctor)
        args = self.args(node)
        if name == "Vue":
            self.vue_app = True
        elif name[:1].isupper() and args and self.prop(args[0], "target") is not None:
            self.entry(UI_ENTRY)  # Svelte 4: new App({ target })


def _has_jsx(node) -> bool:
    return any(n.type in _JSX_TYPES for n in _descendants(node))


def _mount_local_routes(facts: FileFacts, owners: list, mounts: list) -> None:
    """Routes added to a router of this file move under the prefix that router is mounted at.

    const router = express.Router(); router.get("/", h); app.use("/api/todos", router)
    """
    if not mounts or not owners:
        return
    prefixes: dict[str, list[str]] = {}
    for prefix, name in mounts:
        prefixes.setdefault(name, []).append(prefix)
    moved: dict[int, list[HttpRoute]] = {}
    for route, owner in owners:
        if owner in prefixes:
            paths = dict.fromkeys(p for p in (join_http_path(pre, route.path) for pre in prefixes[owner]) if p)
            moved[id(route)] = [HttpRoute(route.method, p, route.handler, route.line) for p in paths] or [route]
    if moved:
        routes: list[HttpRoute] = []
        for route in facts.http_routes:
            routes.extend(moved.get(id(route), [route]))
        facts.http_routes = routes


_DOC_TAGS = ("@file", "@fileoverview", "@overview", "@module", "@description", "@summary")


def _comment_line(text: str) -> str:
    """First sentence of a // or /* */ comment ("" for tags such as @param or @license).

    Wrapped lines of that sentence are joined ("Fetches the list and" + "returns it.").
    """
    body = text[2:]
    if text.startswith("/*") and body.endswith("*/"):
        body = body[:-2]
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip().lstrip("*!/").strip()
        if not line:
            if lines:
                break
            continue
        if line.startswith("@"):
            if lines:
                break
            tag, _, rest = line.partition(" ")
            if tag not in _DOC_TAGS:
                return ""
            line = rest.strip()
            if not line:
                continue  # "@fileoverview" alone, the text follows on the next line
        lines.append(line)
    return join_wrapped(lines)


def _line_block_doc(comments: list[str]) -> str:
    """Summary of consecutive // comments: lint lines dropped, license paragraphs skipped."""
    paragraphs: list[list[str]] = [[]]
    for comment in comments:
        line = comment.lstrip("/").strip()
        if not any(ch.isalnum() for ch in line):  # "//", "// -----"
            paragraphs.append([])
        elif not _PRAGMA.match(line):
            paragraphs[-1].append(line)
    for para in paragraphs:
        summary = join_wrapped(para) if para and not para[0].startswith("@") else ""
        if summary and not is_boilerplate(summary):
            return summary
    return ""


def _squeeze_params(text: str, limit: int = 40) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    s = re.sub(r"\s*,\s*([}\])])", r" \1", s)  # trailing commas
    s = re.sub(r"\s*,$", "", s)
    s = re.sub(r"([(\[]) ", r"\1", s)
    s = re.sub(r" ([)\]])", r"\1", s)
    if len(s) > limit:
        s = s[:limit].rstrip(" ,") + "…"
    return s


def _package_name(spec: str) -> str:
    if not spec or spec.startswith((".", "/")):
        return ""
    parts = spec.split("/")
    if spec.startswith("@") and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _frameworks(imports: list[ImportRef]) -> set[str]:
    found = set()
    for imp in imports:
        pkg = _package_name(imp.spec)
        name = _FRAMEWORKS.get(pkg) or _FRAMEWORK_SCOPES.get(pkg.split("/", 1)[0])
        if name:
            found.add(name)
    return found


_NEXT_PAGES_API = re.compile(r"(?:^|/)pages/(api(?:/.*)?)\.[cm]?[jt]sx?$")
_NEXT_APP_ROUTE = re.compile(r"(?:^|/)app/(.*?)/?route\.[cm]?[jt]s$")


def _next_file_routes(facts: FileFacts):
    """Next.js API routes are defined by the file path (pages/api/x.js, app/x/route.ts)."""
    m = _NEXT_PAGES_API.search(facts.path)
    if m:
        url = "/" + re.sub(r"(?:^|/)index$", "", m.group(1))
        methods = ["ANY"]
    else:
        m = _NEXT_APP_ROUTE.search(facts.path)
        if not m:
            return
        segments = [s for s in m.group(1).split("/") if s and not (s.startswith("(") and s.endswith(")"))]
        url = "/" + "/".join(segments)
        methods = [s.name for s in facts.symbols if s.exported and s.name in _KNOWN_METHODS]
    path = normalize_http_path(url)
    if path is None:
        return
    for method in methods:
        facts.http_routes.append(HttpRoute(method=method, path=path, handler="", line=1))


# ---------------------------------------------------------------- regex fallback

_RX_CODE_TOKENS = re.compile(
    r"//[^\n]*|/\*[\s\S]*?(?:\*/|$)|'(?:\\.|[^'\\\n])*'?|\"(?:\\.|[^\"\\\n])*\"?|`(?:\\[\s\S]|[^`\\])*`?"
)
_RX_IMPORT = re.compile(r"^[ \t]*import\s+(?:type\s+)?([\w$*{}\s,]+?)\s*from\s*(['\"])([^'\"\n]+)\2", re.M)
_RX_SIDE_EFFECT = re.compile(r"^[ \t]*import\s*(['\"])([^'\"\n]+)\1", re.M)
_RX_REEXPORT = re.compile(
    r"^[ \t]*export\s+(?:type\s+)?(\*(?:\s*as\s+[\w$]+)?|\{[^}]*\})\s*from\s*(['\"])([^'\"\n]+)\2", re.M
)
_RX_REQUIRE = re.compile(
    r"(?:\b(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*)?(?<![\w$.])require\s*\(\s*(['\"`])([^'\"`\n]+)\2\s*\)"
)
_RX_DYNAMIC = re.compile(r"(?<![\w$.])import\s*\(\s*(['\"`])([^'\"`\n]+)\1\s*\)")
_RX_CONST = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?=['\"`])", re.M)
_RX_INSTANCE = re.compile(r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*([\w$]+)\s*\.\s*(?:create|extend)\s*\(")
_RX_FETCH = re.compile(r"(?<![\w$.])(?:(?:window|globalThis|self)\s*\.\s*)?fetch\s*\(")
_RX_MEMBER_CALL = re.compile(
    r"(?<![\w$])(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete|all|request)"
    r"\s*(?:<[^()]*?>\s*)?\("  # optional TypeScript type arguments: http.get<Todo[]>(...)
)
_RX_METHOD = re.compile(r"\bmethod\s*:\s*(?:(['\"`])(\w+)\1)?")
_RX_UI_ENTRY = re.compile(
    r"(?<![\w$])(?:createRoot|hydrateRoot|bootstrapApplication|bootstrapModule)\s*\("
    r"|\bReactDOM\s*\.\s*(?:render|hydrate)\s*\("
)
_RX_VUE_APP = re.compile(r"(?<![\w$])(?:createApp|createSSRApp)\s*\(|\bnew\s+Vue\s*\(")
_RX_MOUNT = re.compile(r"\.\s*\$?mount\s*\(")
_RX_LISTEN = re.compile(r"\.\s*listen\s*\(\s*(?![)(]|async\b|function\b|[\w$]+\s*=>)")
_RX_SERVE = re.compile(r"\b(?:Bun|Deno)\s*\.\s*serve\s*\(")
_RX_FUNC = re.compile(
    r"^(export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(?:<[^>(]*>\s*)?(?=\()", re.M
)
_RX_VAR = re.compile(
    r"^(export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s+)?", re.M
)
_RX_CLASS = re.compile(r"^(export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)
_RX_EXPORT_NAMES = re.compile(r"^export\s*\{([^}]*)\}\s*;?[ \t]*\r?$", re.M)
_RX_EXPORT_DEFAULT = re.compile(r"^export\s+default\s+([A-Za-z_$][\w$]*)\s*;?[ \t]*\r?$", re.M)
_RX_MODULE_EXPORTS = re.compile(r"\bmodule\.exports\s*=\s*(\{[^}]*\}|[A-Za-z_$][\w$]*)")
_RX_JSX = re.compile(r"<[A-Za-z][\w.]*(?:\s[^<>]*)?/?>|<>")
_RX_LEADING = re.compile(r"\s*(?:#![^\n]*|(['\"])use [\w ]+\1;?|//[^\n]*|/\*[\s\S]*?\*/)")
_RX_MOUNT_CALL = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\.\s*(use|route|register)\s*\(")
_RX_MOUNT_TARGET = re.compile(
    r"^(?:await\s+)?(?:(require|import)\s*\(\s*(['\"`])([^'\"`\n]+)\2\s*\)|([A-Za-z_$][\w$]*))"
)


def _rx_args(region: str) -> list[str]:
    """Top-level comma-separated arguments of a call (brackets and strings kept whole)."""
    out: list[str] = []
    depth, quote, start, i = 0, None, 0, 0
    while i < len(region):
        c = region[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(region[start:i].strip())
            start = i + 1
        i += 1
    tail = region[start:].strip()
    if tail:
        out.append(tail)
    return out


def _rx_mounts(facts: FileFacts, text: str, consts: dict, route_file: bool, line_of) -> list:
    """app.use("/api", router), app.route("/api", api), app.register(routes, { prefix }) in the fallback.

    Mounts of imported routers go to facts.http_mounts; mounts of routers made in this file
    are returned as (prefix, variable).
    """
    bindings: dict[str, ImportRef] = {}
    for imp in facts.imports:
        if imp.kind in ("import", "require"):
            for bound in imp.names:
                bindings.setdefault(bound, imp)
    local = []
    for m in _RX_MOUNT_CALL.finditer(text):
        obj, verb = m.group(1), m.group(2)
        if not (route_file or _ROUTER_LIKE.match(obj)):
            continue
        args = _rx_args(text[m.end():_balanced_end(text, m.end() - 1) - 1])
        if verb == "register":
            prefix = re.search(r"\bprefix\s*:\s*", args[1]) if len(args) >= 2 and args[1].startswith("{") else None
            raw = _rx_url(args[1], prefix.end(), consts)[0] if prefix else None
            target_text = args[0] if args else ""
        else:
            if len(args) < 2 or (verb == "route" and len(args) != 2) or args[0][:1] not in "'\"`":
                continue
            raw = _rx_url(args[0], 0, consts)[0]
            target_text = args[-1]
        if not raw or normalize_http_path(raw) is None:
            continue
        found = _RX_MOUNT_TARGET.match(target_text)
        if not found:
            continue
        line = line_of(m.start())
        if found.group(3):
            kind = "dynamic" if found.group(1) == "import" else "require"
            facts.http_mounts.append(HttpMount(raw, ImportRef(found.group(3), [], kind, 0, line), line))
        elif found.group(4) in bindings:
            facts.http_mounts.append(HttpMount(raw, bindings[found.group(4)], line))
        else:
            local.append((raw, found.group(4)))
    return local


def _blank_comments(source: str) -> str:
    """Replace comments with spaces (keeping newlines) so positions stay the same."""
    def repl(m):
        s = m.group()
        return re.sub(r"[^\n]", " ", s) if s.startswith(("//", "/*")) else s
    return _RX_CODE_TOKENS.sub(repl, source)


def _balanced_end(text: str, i: int, limit: int = 5000) -> int:
    """Index just after the bracket that closes the one at text[i]."""
    depth, quote, j, end = 0, None, i, min(len(text), i + limit)
    while j < end:
        c = text[j]
        if quote:
            if c == "\\":
                j += 1
            elif c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return end


def _rx_url(text: str, pos: int, consts: dict[str, str]) -> tuple[str | None, int]:
    """Parse string literals and identifiers joined by + starting at pos."""
    parts, i, n = [], pos, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            lit = text[i + 1:j]
            if c == "`":
                lit = re.sub(r"\$\{\s*([A-Za-z_$][\w$]*)\s*\}", lambda m: consts.get(m.group(1), "${}"), lit)
                lit = re.sub(r"\$\{[^}]*\}", "${}", lit)
            parts.append(lit)
            i = j + 1
        else:
            m = re.compile(r"[A-Za-z_$][\w$.]*").match(text, i)
            if not m:
                break
            parts.append(consts.get(m.group(), "${}"))
            i = m.end()
        j = i
        while j < n and text[j].isspace():
            j += 1
        if j < n and text[j] == "+":
            i = j + 1
            continue
        break
    return ("".join(parts) if parts else None), i


def _rx_jsdoc(source: str, start: int) -> str:
    before = source[max(0, start - 2000):start]
    end = before.rfind("*/")
    if end < 0 or before[end + 2:].strip() or before[end + 2:].count("\n") > 1:
        return ""
    begin = before.rfind("/**", 0, end)
    return _comment_line(before[begin:end + 2]) if begin >= 0 else ""


def _rx_names(clause: str) -> list[str]:
    clause = re.sub(r"\btype\s+", "", clause)
    names = []
    m = re.search(r"\{([^}]*)\}", clause)
    head = clause[:m.start()] if m else clause
    for part in head.split(","):
        part = part.strip()
        if part.startswith("*"):
            ns = re.search(r"as\s+([\w$]+)", part)
            if ns:
                names.append(ns.group(1))
        elif part:
            names.append(part)
    if m:
        for item in m.group(1).split(","):
            item = item.strip()
            if item:
                names.append(re.split(r"\s+as\s+", item)[-1].strip())
    return names


def _rx_module_doc(source: str, symbol_lines: set[int], line_of) -> str:
    pos = 0
    block: list[str] = []  # consecutive // lines
    block_end = -1
    while True:
        m = _RX_LEADING.match(source, pos)
        token = m.group().strip() if m and m.end() > pos else ""
        start = m.end() - len(m.group().lstrip()) if token else len(source)
        if block and not (token.startswith("//") and source.count("\n", block_end, start) <= 1):
            line = _line_block_doc(block)
            if line:
                return line
            block = []
        if not token:
            return ""
        pos = m.end()
        if token.startswith("//"):
            block.append(token)
            block_end = pos
            continue
        if not token.startswith("/*"):
            continue
        if token.startswith("/**"):
            nxt = len(source) - len(source[pos:].lstrip())
            if source.count("\n", pos, nxt) <= 1 and line_of(nxt) in symbol_lines:
                return ""  # JSDoc of the first function, not of the file
        line = _comment_line(token)
        if line and not _PRAGMA.match(line) and not is_boilerplate(line):
            return line


def _regex_analyze(facts: FileFacts, source: str, ext: str):
    text = _blank_comments(source)
    newlines = [m.start() for m in re.finditer("\n", text)]

    def line_of(pos: int) -> int:
        return bisect.bisect_right(newlines, pos) + 1

    _rx_symbols(facts, source, text, ext, line_of)
    facts.module_doc = _rx_module_doc(source, {s.line for s in facts.symbols}, line_of)
    found = []  # (pos, ImportRef)
    for m in _RX_IMPORT.finditer(text):
        found.append((m.start(), ImportRef(m.group(3), _rx_names(m.group(1)), "import", 0, line_of(m.start()))))
    for m in _RX_SIDE_EFFECT.finditer(text):
        found.append((m.start(), ImportRef(m.group(2), [], "side-effect", 0, line_of(m.start()))))
    for m in _RX_REEXPORT.finditer(text):
        clause = m.group(1)
        names = ["*"] if clause.strip() == "*" else _rx_names(clause)
        found.append((m.start(), ImportRef(m.group(3), names, "reexport", 0, line_of(m.start()))))
    for m in _RX_REQUIRE.finditer(text):
        names = _rx_names(m.group(1)) if m.group(1) else []
        found.append((m.start(), ImportRef(m.group(3), names, "require", 0, line_of(m.start()))))
    for m in _RX_DYNAMIC.finditer(text):
        if "${" not in m.group(2):
            found.append((m.start(), ImportRef(m.group(2), [], "dynamic", 0, line_of(m.start()))))
    found.sort(key=lambda item: item[0])
    facts.imports = [imp for _, imp in found]
    facts.frameworks = _frameworks(facts.imports)
    route_file = bool(facts.frameworks & _ROUTE_FRAMEWORKS)

    consts: dict[str, str] = {}
    for m in _RX_CONST.finditer(text):
        value, _ = _rx_url(text, m.end(), consts)
        if value is not None:
            consts[m.group(1)] = value

    clients = set(_CLIENTS)
    for imp in facts.imports:
        if imp.kind in ("import", "require") and _package_name(imp.spec) in _CLIENT_PACKAGES:
            clients.update(imp.names)
    bases: dict[str, str] = {}
    for m in _RX_INSTANCE.finditer(text):
        if m.group(2) in clients:
            clients.add(m.group(1))
            region = text[m.end():_balanced_end(text, m.end() - 1)]
            b = re.search(r"\b(?:baseURL|prefixUrl)\s*:\s*", region)
            if b:
                value, _ = _rx_url(region, b.end(), consts)
                if value:
                    bases[m.group(1)] = value

    def add_call(method: str, raw: str | None, pos: int):
        path = normalize_http_path(raw) if raw else None
        if path is not None:
            facts.http_calls.append(HttpCall(method=method, path=path, line=line_of(pos)))

    def method_in(region: str, default: str = "GET") -> str:
        mm = _RX_METHOD.search(region)
        if not mm:
            return default
        value = (mm.group(2) or "").upper()
        return value if value in _KNOWN_METHODS else "ANY"

    def client_url(client: str, raw: str | None) -> str | None:
        if raw and not _ABSOLUTE_URL.match(raw) and not raw.startswith("${"):
            raw = bases.get(client, "").rstrip("/") + "/" + raw.lstrip("/")
        return raw

    for m in _RX_FETCH.finditer(text):
        raw, after = _rx_url(text, m.end(), consts)
        region = text[after:_balanced_end(text, m.end() - 1)]
        add_call(method_in(region) if region.lstrip().startswith(",") else "GET", raw, m.start())

    client_call = re.compile(r"(?<![\w$.])(" + "|".join(re.escape(c) for c in sorted(clients)) + r")\s*\(")
    for m in client_call.finditer(text):
        region = text[m.end():_balanced_end(text, m.end() - 1)]
        if region.lstrip().startswith("{"):
            u = re.search(r"\burl\s*:\s*", region)
            raw = _rx_url(region, u.end(), consts)[0] if u else None
            add_call(method_in(region), client_url(m.group(1), raw), m.start())
        else:
            raw, after = _rx_url(text, m.end(), consts)
            add_call(method_in(text[after:m.end() + len(region)]), client_url(m.group(1), raw), m.start())

    owners = []  # (HttpRoute, variable the route was added to)
    for m in _RX_MEMBER_CALL.finditer(text):
        obj, prop = m.group(1), m.group(2)
        raw, after = _rx_url(text, m.end(), consts)
        more_args = text[after:after + 200].lstrip().startswith(",")
        if prop == "request":
            if obj in clients:
                region = text[m.end():_balanced_end(text, m.end() - 1)]
                u = re.search(r"\burl\s*:\s*", region)
                if u:
                    add_call(method_in(region), client_url(obj, _rx_url(region, u.end(), consts)[0]), m.start())
            continue
        if obj in clients:
            if prop != "all":
                add_call(prop.upper(), client_url(obj, raw), m.start())
            continue
        if (route_file or _ROUTER_LIKE.match(obj)) and more_args:
            path = normalize_http_path(raw) if raw else None
            if path is not None:
                method = "ANY" if prop == "all" else prop.upper()
                route = HttpRoute(method=method, path=path, handler="", line=line_of(m.start()))
                facts.http_routes.append(route)
                owners.append((route, obj))
                continue
        if prop != "all" and _CLIENT_LIKE.search(obj) and not _NOT_CLIENT.search(obj):
            add_call(prop.upper(), raw, m.start())
    _mount_local_routes(facts, owners, _rx_mounts(facts, text, consts, route_file, line_of))

    if _RX_UI_ENTRY.search(text) or (_RX_VUE_APP.search(text) and _RX_MOUNT.search(text)):
        facts.is_entry, facts.entry_reason = True, UI_ENTRY
    elif _RX_LISTEN.search(text) or _RX_SERVE.search(text):
        facts.is_entry, facts.entry_reason = True, SERVER_ENTRY

    # Hint: the first HTTP call between a symbol and the next one.
    calls = sorted(facts.http_calls, key=lambda c: c.line)
    call_lines = [c.line for c in calls]
    for i, sym in enumerate(facts.symbols):
        end = facts.symbols[i + 1].line if i + 1 < len(facts.symbols) else len(newlines) + 2
        j = bisect.bisect_left(call_lines, sym.line)
        if not sym.hint and j < len(calls) and call_lines[j] < end:
            sym.hint = f"{calls[j].method} · {calls[j].path}"


def _rx_symbols(facts: FileFacts, source: str, text: str, ext: str, line_of):
    found = []  # (start, body_from, name, kind, params, exported)
    for m in _RX_FUNC.finditer(text):
        close = _balanced_end(text, m.end())
        found.append((m.start(), close, m.group(2), "function", text[m.end() + 1:close - 1], bool(m.group(1))))
    for m in _RX_VAR.finditer(text):
        rest = text[m.end():]
        fn = re.match(r"function\b[^(]*", rest)
        if fn:
            open_pos = m.end() + fn.end()
            close = _balanced_end(text, open_pos)
            params = text[open_pos + 1:close - 1]
        elif rest.startswith("("):
            close = _balanced_end(text, m.end())
            if not re.match(r"\s*(?::[^=;\n]+)?=>", text[close:close + 200]):
                continue
            params = text[m.end() + 1:close - 1]
        else:
            single = re.match(r"([A-Za-z_$][\w$]*)\s*=>", rest)
            if not single:
                continue
            close = m.end() + single.end()
            params = single.group(1)
        found.append((m.start(), close, m.group(2), "function", params, bool(m.group(1))))
    for m in _RX_CLASS.finditer(text):
        found.append((m.start(), m.end(), m.group(2), "class", None, bool(m.group(1))))
    found.sort(key=lambda item: item[0])

    exported = set()
    for m in _RX_EXPORT_NAMES.finditer(text):
        exported.update(re.split(r"\s+as\s+", n.strip())[0] for n in m.group(1).split(",") if n.strip())
    exported.update(m.group(1) for m in _RX_EXPORT_DEFAULT.finditer(text))
    for m in _RX_MODULE_EXPORTS.finditer(text):
        value = m.group(1)
        if value.startswith("{"):
            for item in value[1:-1].split(","):
                item = item.split(":")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", item):
                    exported.add(item)
        else:
            exported.add(value)

    jsx_possible = ext not in (".ts", ".mts", ".cts")
    for i, (start, body_from, name, kind, params, is_exported) in enumerate(found):
        body_to = found[i + 1][0] if i + 1 < len(found) else len(text)
        component = jsx_possible and name[:1].isupper() and bool(_RX_JSX.search(text, body_from, body_to))
        signature = f"class {name}" if params is None else f"{name}({_squeeze_params(params)})"
        facts.symbols.append(Symbol(
            name=name,
            kind="component" if component else kind,
            signature=signature,
            doc=_rx_jsdoc(source, start),
            hint=COMPONENT_HINT if component else "",
            line=line_of(start),
            exported=is_exported or name in exported,
        ))


# ---------------------------------------------------------------- resolve

_CACHES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _cache(index: ProjectIndex) -> dict:
    try:
        cache = _CACHES.get(index)
        if cache is None:
            cache = _CACHES[index] = {}
        return cache
    except TypeError:
        return {}


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def resolve(imp: ImportRef, importer: str, index: ProjectIndex) -> list[str]:
    spec = (imp.spec or "").strip().split("?", 1)[0]
    if not spec or re.match(r"^[a-zA-Z][\w+.-]*:", spec):  # node:fs, https://..., virtual:x
        return []
    importer_dir = parent_dir(importer)
    if spec in (".", "..") or spec.startswith(("./", "../")):
        folder_only = spec.endswith("/") or spec.rsplit("/", 1)[-1] in (".", "..")
        return _resolve_path(join_rel(importer_dir, spec), index, folder_only)
    if spec.startswith("/"):
        return _resolve_path(join_rel(_package_root(importer_dir, index), spec), index, spec.endswith("/"))
    return _resolve_alias(spec, importer_dir, index)


def _resolve_path(base: str | None, index: ProjectIndex, folder_only: bool = False) -> list[str]:
    if base is None:
        return []
    files = index.files
    if not folder_only:
        if base in files:
            return [base]
        for ext in _RESOLVE_EXTENSIONS:
            if base + ext in files:
                return [base + ext]
        for js_ext, ts_exts in _JS_TO_TS.items():
            if base.endswith(js_ext):
                for ts_ext in ts_exts:
                    candidate = base[: -len(js_ext)] + ts_ext
                    if candidate in files:
                        return [candidate]
    for ext in _RESOLVE_EXTENSIONS:
        candidate = _join(base, "index" + ext)
        if candidate in files:
            return [candidate]
    return []


def _package_root(start: str, index: ProjectIndex) -> str:
    """Nearest folder (from start upward) holding package.json, else start."""
    cache = _cache(index)
    key = ("pkg", start)
    if key not in cache:
        folder, result = start, start
        while True:
            if _join(folder, "package.json") in index.files:
                result = folder
                break
            if not folder:
                break
            folder = parent_dir(folder)
        cache[key] = result
    return cache[key]


def _resolve_alias(spec: str, importer_dir: str, index: ProjectIndex) -> list[str]:
    covered = False
    config = _tsconfig(importer_dir, index)
    if config is not None:
        base, paths, owner = config
        paths_dir = base if base is not None else owner
        for pattern in sorted(paths, key=lambda p: len(p.split("*", 1)[0]), reverse=True):
            star = _match_pattern(pattern, spec)
            if star is None:
                continue
            covered = True
            targets = paths[pattern]
            for target in [targets] if isinstance(targets, str) else targets if isinstance(targets, list) else []:
                if isinstance(target, str):
                    hit = _resolve_path(join_rel(paths_dir, target.replace("*", star, 1)), index)
                    if hit:
                        return hit
        if base is not None:
            hit = _resolve_path(join_rel(base, spec), index)
            if hit:
                return hit
    if not covered and spec[:2] in ("@/", "~/"):
        src = join_rel(_package_root(importer_dir, index), "src")
        if src is not None and index.is_dir(src):
            return _resolve_path(join_rel(src, spec[2:]), index)
    return []


def _match_pattern(pattern: str, spec: str) -> str | None:
    if "*" not in pattern:
        return "" if spec == pattern else None
    prefix, _, suffix = pattern.partition("*")
    if spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix) + len(suffix):
        return spec[len(prefix):len(spec) - len(suffix)]
    return None


def _tsconfig(start: str, index: ProjectIndex):
    """(baseUrl folder or None, paths, folder of the config that set paths) of the nearest config."""
    cache = _cache(index)
    key = ("cfg", start)
    if key not in cache:
        result, folder = None, start
        while True:
            for name in ("tsconfig.json", "jsconfig.json"):
                path = _join(folder, name)
                if path in index.files:
                    result = _load_config(path, index, 0)
                    break
            if result is not None or not folder:
                break
            folder = parent_dir(folder)
        cache[key] = result
    return cache[key]


def _load_config(path: str, index: ProjectIndex, depth: int):
    text = index.read_text(path)
    if text is None:
        return None
    try:
        data = _loads_jsonc(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    options = data.get("compilerOptions")
    options = options if isinstance(options, dict) else {}
    folder = parent_dir(path)
    base, paths, owner = None, {}, folder
    parent = data.get("extends")
    if isinstance(parent, list):
        parent = parent[-1] if parent else None
    if isinstance(parent, str) and parent.startswith(".") and depth < 5:
        target = join_rel(folder, parent)
        if target is not None and not target.endswith(".json"):
            target += ".json"
        inherited = _load_config(target, index, depth + 1) if target else None
        if inherited is not None:
            base, paths, owner = inherited
    if isinstance(options.get("baseUrl"), str):
        base = join_rel(folder, options["baseUrl"])
    if isinstance(options.get("paths"), dict):
        paths, owner = options["paths"], folder
    return base, paths, owner


_RX_JSON_COMMENTS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
_RX_JSON_COMMAS = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')


def _loads_jsonc(text: str):
    """json.loads for tsconfig-style JSON with comments and trailing commas."""
    def keep_strings(m):
        return m.group() if m.group().startswith('"') else ""
    text = _RX_JSON_COMMENTS.sub(keep_strings, text)
    text = _RX_JSON_COMMAS.sub(keep_strings, text)
    return json.loads(text)


# ---------------------------------------------------------------- project entries

_RUNNERS = frozenset({"node", "nodemon", "ts-node", "ts-node-dev", "tsx", "bun", "babel-node"})
_RUNNER_WORDS = frozenset({"watch", "run", "start", "--"})
_RX_SCRIPT_SRC = re.compile(r"<script\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)


def project_entries(index: ProjectIndex) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in sorted(index.files):
        name = rel.rsplit("/", 1)[-1]
        try:
            if name == "package.json":
                _package_entries(rel, index, out)
            elif name.lower().endswith((".html", ".htm")):
                _html_entries(rel, name, index, out)
        except Exception:
            continue
    return out


def _package_entries(rel: str, index: ProjectIndex, out: dict[str, str]):
    text = index.read_text(rel)
    data = _loads_jsonc(text) if text else None
    if not isinstance(data, dict):
        return
    folder = parent_dir(rel)
    targets = [data[k] for k in ("main", "module") if isinstance(data.get(k), str)]
    bins = data.get("bin")
    if isinstance(bins, str):
        targets.append(bins)
    elif isinstance(bins, dict):
        targets.extend(v for v in bins.values() if isinstance(v, str))
    for target in targets:
        folder_only = target.strip().rstrip("/") in ("", ".")
        for hit in _resolve_path(join_rel(folder, target.strip()), index, folder_only):
            out.setdefault(hit, PACKAGE_ENTRY)
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        for command in scripts.values():
            if isinstance(command, str):
                for hit in _script_entries(command, folder, index):
                    out.setdefault(hit, PACKAGE_ENTRY)


def _script_entries(command: str, folder: str, index: ProjectIndex) -> list[str]:
    hits = []
    for part in re.split(r"&&|\|\||[;|&]", command):
        tokens = [t.strip("'\"") for t in part.split()]
        start = None
        for i, token in enumerate(tokens):
            exe = token.replace("\\", "/").rsplit("/", 1)[-1]
            if exe in _RUNNERS:
                start = i + 1
            elif exe == "deno" and i + 1 < len(tokens) and tokens[i + 1] == "run":
                start = i + 2
            if start is not None:
                break
        if start is None:
            continue
        for token in tokens[start:]:
            if not token or token.startswith("-") or token in _RUNNER_WORDS or "=" in token:
                continue
            target = join_rel(folder, token)
            folder_only = token.rstrip("/") in ("", ".")
            if target is None or (index.is_dir(target) and not folder_only):
                continue  # a flag value such as `--watch src`
            hit = _resolve_path(target, index, folder_only)
            if hit:
                hits.extend(hit)
                break
    return hits


def _html_entries(rel: str, name: str, index: ProjectIndex, out: dict[str, str]):
    for hit in _html_scripts(rel, index):
        out.setdefault(hit, HTML_ENTRY.format(name=name))


_RX_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_RX_HREF = re.compile(r"\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)


def _html_text(rel: str, index: ProjectIndex) -> str:
    text = index.read_text(rel)
    return re.sub(r"<!--[\s\S]*?-->", "", text) if text else ""


def _html_target(src: str, rel: str, index: ProjectIndex) -> list[str]:
    """Project file a page's src or href points to ([] for other sites, data: and the like)."""
    src = (src or "").strip()
    if not src or re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*:|//|#)", src):
        return []
    src = src.split("?", 1)[0].split("#", 1)[0]
    html_dir = parent_dir(rel)
    base = join_rel(_package_root(html_dir, index), src) if src.startswith("/") else join_rel(html_dir, src)
    return _resolve_path(base, index)


def _html_scripts(rel: str, index: ProjectIndex) -> list[str]:
    hits: list[str] = []
    for m in _RX_SCRIPT_SRC.finditer(_html_text(rel, index)):
        hits.extend(_html_target(m.group(1) or m.group(2) or m.group(3) or "", rel, index))
    return list(dict.fromkeys(hits))


def project_links(index: ProjectIndex) -> dict[str, list[str]]:
    """Files a web page loads by itself: <script src> and <link href> (style sheets, icons).

    index.html -> app.js, app.css. They are connections the graph shows like imports.
    """
    out: dict[str, list[str]] = {}
    for rel in sorted(index.files):
        if not rel.lower().endswith((".html", ".htm")):
            continue
        try:
            hits = _html_scripts(rel, index)
            for tag in _RX_LINK_TAG.finditer(_html_text(rel, index)):
                href = _RX_HREF.search(tag.group())
                if href:
                    hits.extend(_html_target(href.group(1) or href.group(2) or href.group(3) or "", rel, index))
        except Exception:
            continue
        hits = [h for h in dict.fromkeys(hits) if h != rel]
        if hits:
            out[rel] = hits
    return out
