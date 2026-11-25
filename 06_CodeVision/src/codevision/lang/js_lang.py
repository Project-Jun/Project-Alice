"""JavaScript / TypeScript analyzer, also for the <script> blocks of Vue and Svelte files.

Parses with tree-sitter when it is installed. Without it (TREE_SITTER = False)
a regular-expression fallback still finds imports, requires, dynamic imports,
HTTP calls, routes and simple top-level functions.
A .vue or .svelte file is analyzed as the code of its top-level <script> blocks, left at their
own lines (everything else is blanked), so line numbers are those of the original file.
The analyzer contract is in docs/architecture.md, section 3.
"""
from __future__ import annotations

import bisect
import json
import re
import weakref

try:  # Python's own regular-expression parser: what a pattern from a project file may cost
    from re import _constants as _sre_constants
    from re import _parser as _sre_parser
except ImportError:  # pragma: no cover - Python 3.10
    import sre_constants as _sre_constants
    import sre_parse as _sre_parser

from codevision.describe import is_boilerplate, join_wrapped
from codevision.model import (
    ExportName,
    FileFacts,
    HelperCall,
    HttpCall,
    HttpHelper,
    HttpMount,
    HttpRoute,
    ImportRef,
    ProjectIndex,
    Symbol,
    atomic,
    helper_request,
    is_pattern,
    join_http_path,
    join_rel,
    join_stars,
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

EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte")
_TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")
_SFC_FRAMEWORKS = {".vue": "vue", ".svelte": "svelte"}  # single-file components: the file is the framework's
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
_MOUNT_METHODS = frozenset({"use", "route", "register", "listen"})  # what a router or app does that is no request
# Calls that show a function's first parameter is a Fastify instance: a route, a route table, a plugin
_PLUGIN_METHODS = frozenset({*_ROUTE_METHODS, "route", "register"})
# Node's own modules: a call of theirs with a path in it (path.join("/a", "b")) is never a request helper
_NODE_BUILTINS = frozenset({
    "assert", "buffer", "child_process", "cluster", "crypto", "dgram", "dns", "events", "fs", "http", "http2",
    "https", "net", "os", "path", "perf_hooks", "process", "querystring", "readline", "stream", "string_decoder",
    "timers", "tls", "url", "util", "v8", "vm", "worker_threads", "zlib"})
_KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_CLIENTS = frozenset({"axios", "ky", "ofetch", "$fetch"})
_NUXT_CLIENTS = frozenset({"useFetch", "useLazyFetch"})  # Nuxt (and VueUse) composables in Vue code
_CLIENT_PACKAGES = frozenset({"axios", "ky", "ofetch", "redaxios", "ky-universal"})
# Names that look like an imported HTTP client instance (api.get("/todos")).
_CLIENT_LIKE = re.compile(r"(?i)api|http|client|axios|fetcher")
_NOT_CLIENT = re.compile(r"(?i)router|server")
# Objects that look like a server app even when the file does not import the framework.
_ROUTER_LIKE = re.compile(r"(?i)^(?:app|server|\w*router)$")
# First parameters of a Fastify plugin whose name says so: their routes may name their handler
_INSTANCE_LIKE = re.compile(r"(?i)fastify|^instance$")
_ROUTE_FRAMEWORKS = frozenset({"express", "koa", "fastify", "hono"})
_KOA_ROUTERS = frozenset({"koa-router", "@koa/router"})  # packages whose Router takes { prefix }
# Methods a router declaration can chain before its value is the router: new Hono().basePath("/api"),
# new Router({ prefix }).get(...).
_CHAIN_METHODS = frozenset({"basePath", "route", "use", *_ROUTE_METHODS})
# A prefix of plain path segments only (no :id, {id}, ${}, ?, #): normalized by squeezing "//" and
# dropping a trailing "/", so a normalized route path can simply follow it.
_LITERAL_PREFIX = re.compile(r"/[\w.~-]*(?:/[\w.~-]*)*")
_ABSOLUTE_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_ABSOLUTE_HOST = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]*")
_LEADING_HOLES = re.compile(r"^(?:\$\{\})+")

_FRAMEWORKS = {
    "react": "react", "react-dom": "react",
    "vue": "vue", "svelte": "svelte", "next": "next",
    # Nuxt's own modules (#app, #imports) and SvelteKit's ($app/stores, @sveltejs/kit and its adapters)
    "nuxt": "nuxt", "#app": "nuxt", "#imports": "nuxt", "@sveltejs/kit": "sveltekit", "$app": "sveltekit",
    "express": "express", "koa": "koa", "koa-router": "koa", "@koa/router": "koa",
    "fastify": "fastify", "fastify-plugin": "fastify", "hono": "hono",
    "@prisma/client": "prisma", "mongodb": "mongodb", "mongoose": "mongodb",
    "pg": "postgres", "postgres": "postgres", "mysql2": "mysql", "mysql": "mysql",
    "sqlite3": "sqlite", "better-sqlite3": "sqlite", "sqlite": "sqlite",
    "node:sqlite": "sqlite", "bun:sqlite": "sqlite",
    "sequelize": "sequelize", "typeorm": "typeorm",
    "firebase": "firebase", "firebase-admin": "firebase",
    "redis": "redis", "ioredis": "redis",
}
_FRAMEWORK_SCOPES = {"@angular": "angular", "@nestjs": "nestjs", "@supabase": "supabase", "@fastify": "fastify"}

_FUNCTION_TYPES = ("arrow_function", "function_expression", "function", "generator_function")
_JSX_TYPES = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})
_PRAGMA = re.compile(
    r"(?i)^(?:eslint|prettier|@ts-|ts-|@flow|jshint|jslint|globals?\s|istanbul|c8\s|@jsx|"
    r"@vitest|@jest|#(?:end)?region|<reference|webpack|use strict)"
)


# ---------------------------------------------------------------- analyze


def analyze(rel_path: str, source: str) -> FileFacts:
    ext = _ext(rel_path)
    lines = len(source.splitlines())
    sfc = _SFC_FRAMEWORKS.get(ext)
    srcs: list[tuple[str, int]] = []  # Vue <script src>, <template src>, <style src>
    unread = False
    if sfc:
        try:
            source, ext, srcs = _sfc_code(source, ext)
        except Exception:  # never raise: the file is then analyzed as if it had no script, and said so
            source, ext, unread = "", ".js", True
    language = "typescript" if ext in _TS_EXTENSIONS else "javascript"
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
        _file_routes(facts, source)
    except Exception:
        pass
    config = None if sfc else _CONFIG_FRAMEWORKS.match(rel_path.rsplit("/", 1)[-1])
    if sfc:
        facts.frameworks.add(sfc)
        facts.imports.extend(ImportRef(spec, [], "side-effect", 0, line) for spec, line in srcs)
        facts.parse_error = facts.parse_error or unread
    elif config:  # nuxt.config.ts: a Nuxt project's code imports nothing from nuxt
        facts.frameworks.add(config.group(1).lower())
    facts.imports.sort(key=lambda i: i.line)
    facts.http_calls.sort(key=lambda c: c.line)
    facts.http_routes.sort(key=lambda r: r.line)
    return facts


# ---------------------------------------------------------------- Vue and Svelte single-file components

# Script languages the JS/TS parser reads, by the lang attribute: the extension the code is parsed as.
_SCRIPT_LANGS = {"": ".js", "js": ".js", "javascript": ".js", "jsx": ".jsx", "ts": ".ts", "typescript": ".ts",
                 "tsx": ".tsx"}
_SCRIPT_TYPES = frozenset({"", "module", "text/javascript", "application/javascript", "text/babel",
                           "text/typescript", "application/typescript", "ts"})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                        "source", "track", "wbr"})
_RX_SFC_VUE = re.compile(r"<|\{\{")  # the next tag, or a {{ }} expression of the template
_RX_SFC_SVELTE = re.compile(r"[<{]")  # the next tag, or a { } expression of the markup
_RX_SFC_NAME = re.compile(r"[A-Za-z][\w:.-]*")
_RX_SFC_ATTR = re.compile(r"""([^\s"'=<>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")
_RX_SFC_CLOSE = {name: re.compile(rf"</{name}\s*>", re.I) for name in ("script", "style", "template")}
_RX_JS_TOKEN = re.compile(r"[{}'\"`/]")  # what changes the brace depth of a JavaScript expression
_RX_NOT_BREAK = re.compile(r"[^\r\n]+")
# nuxt.config.ts, next.config.mjs: a framework whose app code needs no import of it
_CONFIG_FRAMEWORKS = re.compile(r"(?i)^(nuxt|next)\.config\.[cm]?[jt]s$")


def _blank(text: str) -> str:
    """The same text with every character but line breaks turned into a space: line numbers stay."""
    return _RX_NOT_BREAK.sub(lambda m: " " * len(m.group()), text)


_RX_STRING_STOP = {"'": re.compile(r"[\\'\r\n]"), '"': re.compile(r'[\\"\r\n]'), "`": re.compile(r"[\\`$]")}
# ${ } parts of template literals inside each other that the expression scanner follows; deeper ones
# (only broken markup has them) are plain text, so no file can run the scanner out of stack.
_MAX_NESTING = 32


def _skip_js_string(text: str, i: int, budget: list[int], nesting: int = 0) -> int:
    """Index just after the string or template literal that starts at text[i] (the end if unclosed).
    A template literal's ${ } parts may hold strings and templates of their own, _MAX_NESTING deep."""
    quote, n = text[i], len(text)
    stop = _RX_STRING_STOP[quote]
    j = i + 1
    while budget[0] > 0:
        m = stop.search(text, j)
        if m is None:
            budget[0] -= n - j
            return n
        k = m.start()
        budget[0] -= k - j + 1
        j = k
        c = text[j]
        if c == "\\":
            j += 2
        elif c == quote:
            return j + 1
        elif c == "$":
            if text.startswith("${", j) and nesting < _MAX_NESTING:
                j = _skip_braces(text, j + 1, budget, n, nesting + 1)
            else:
                j += 1
        else:
            return j  # a quote that never closes ends with its line
    return n


def _skip_braces(text: str, i: int, budget: list[int], fail: int | None = None, nesting: int = 0) -> int:
    """Index just after the } that closes the { at text[i] (a Svelte expression, a ${ } part), skipping
    strings, template literals and comments; `fail` (i + 1 by default) when it never closes.

    Every character read spends one of `budget`, which _sfc_blocks sizes to the file: in a broken file
    whose braces never close, each "{" would otherwise read to the end again (a "{" every few hundred
    characters made a 1 MB file take seconds). Spent, nothing closes."""
    depth, j, n = 0, i, len(text)
    while budget[0] > 0:
        m = _RX_JS_TOKEN.search(text, j)
        if m is None:
            budget[0] -= n - j
            break
        k = m.start()
        budget[0] -= k - j + 1
        c = text[k]
        if c == "{":
            depth += 1
            j = k + 1
        elif c == "}":
            depth -= 1
            j = k + 1
            if depth == 0:
                return j
        elif c == "/":
            if text.startswith("//", k):
                j = text.find("\n", k)
                j = n if j == -1 else j
            elif text.startswith("/*", k):
                j = text.find("*/", k + 2)
                j = n if j == -1 else j + 2
            else:
                j = k + 1  # division, or a regular expression: rare enough in markup to leave
            budget[0] -= j - k  # a comment that never ends reads to the end once, not once per "{"
        else:
            j = _skip_js_string(text, k, budget, nesting)
        if j >= n:
            break
    return i + 1 if fail is None else fail


def _tag_end(text: str, i: int, svelte: bool, budget: list[int]) -> tuple[int, bool]:
    """(index just after the ">" that ends a start tag whose attributes begin at text[i], self-closing).
    Quoted values may hold ">" and "<"; so may Svelte's {expressions}, also inside quotes."""
    n = len(text)
    while i < n:
        c = text[i]
        if c == ">":
            return i + 1, text[i - 1] == "/"
        if c in "\"'":
            k = i + 1
            while k < n and text[k] != c:
                k = _skip_braces(text, k, budget) if svelte and text[k] == "{" else k + 1
            i = k + 1
        elif c == "{" and svelte:
            i = _skip_braces(text, i, budget)
        else:
            i += 1
    return n, False


def _sfc_blocks(text: str, svelte: bool) -> list[tuple[str, dict[str, str], int, int]]:
    """Top-level <script>, <style> and <template> elements of a .vue or .svelte file:
    (tag, attributes, content start, content end), in file order.

    A small HTML scanner. Comments, quoted attribute values, the raw text of script and style
    elements, and the expressions of the markup ({{ }} in a Vue template, { } in Svelte) are
    skipped, so "<script" written inside any of them never starts a block. Elements are tracked
    as a stack, so a <script> nested in markup (Svelte's <svelte:head>) is not a block either;
    a closing tag closes the elements still open inside it, as HTML does.
    """
    out: list[tuple[str, dict[str, str], int, int]] = []
    stack: list[str] = []
    opened: dict[str, int] = {}  # tag -> how many are on the stack: a closing tag finds its own at once
    special = _RX_SFC_SVELTE if svelte else _RX_SFC_VUE
    n, i = len(text), 0
    budget = [4 * n + 100_000]  # characters the expression scanner may read (see _skip_braces)
    closes = True  # Vue: a "}}" is still ahead (once none is, no later {{ closes either)
    while True:
        m = special.search(text, i)
        if m is None:
            return out
        i = m.start()
        if text[i] == "{":
            if svelte:
                i = _skip_braces(text, i, budget)
            else:  # {{ }} only means something inside the template
                k = text.find("}}", i + 2) if stack and closes else -1
                closes = closes and (k != -1 or not stack)
                i = i + 2 if k == -1 else k + 2
            continue
        if text.startswith("<!--", i):
            k = text.find("-->", i + 4)
            i = n if k == -1 else k + 3
            continue
        nxt = text[i + 1:i + 2]
        if nxt in ("!", "?"):  # <!doctype>, <?xml ?>
            k = text.find(">", i + 2)
            i = n if k == -1 else k + 1
            continue
        closing = nxt == "/"
        name = _RX_SFC_NAME.match(text, i + 2 if closing else i + 1)
        if name is None:  # "a < b" in text
            i += 1
            continue
        tag = name.group().lower()
        if closing:
            k = text.find(">", name.end())
            i = n if k == -1 else k + 1
            if opened.get(tag):
                while True:
                    last = stack.pop()
                    opened[last] -= 1
                    if last == tag:
                        break
            continue
        end, self_closing = _tag_end(text, name.end(), svelte, budget)
        block = tag in ("script", "style", "template") or (not stack and not svelte)
        attrs = _sfc_attrs(text[name.end():end - 1]) if block else {}
        # Raw text up to the closing tag: code, CSS, and in Vue every top-level block but an HTML
        # template (a Pug template, custom blocks such as <i18n> and <docs>), as Vue reads them.
        raw = tag in ("script", "style") or (not stack and not svelte and not (
            tag == "template" and attrs.get("lang", "html").lower() in ("html", "")))
        if raw and not self_closing:
            closer = _RX_SFC_CLOSE.get(tag) or re.compile(rf"</{re.escape(tag)}\s*>", re.I)
            close = closer.search(text, end)
            stop, after = (close.start(), close.end()) if close else (n, n)
            if not stack:
                out.append((tag, attrs, end, stop))
            i = after
            continue
        if not stack and tag in ("script", "style", "template"):
            out.append((tag, attrs, end, end))  # <script src="./x.js" />, or a template read on below
        if not self_closing and tag not in _VOID_TAGS:
            stack.append(tag)
            opened[tag] = opened.get(tag, 0) + 1
        i = end


def _sfc_attrs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _RX_SFC_ATTR.finditer(text):
        value = m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4)
        out.setdefault(m.group(1).lower(), value or "")
    return out


def _sfc_code(text: str, ext: str) -> tuple[str, str, list[tuple[str, int]]]:
    """The code of a .vue or .svelte file for the JS/TS analyzer: its top-level <script> blocks
    (Vue <script> and <script setup>, Svelte's instance script and <script context="module"> or
    <script module>) at their own places, everything else blanked. Also the extension the code is
    parsed as (lang="ts" or "tsx") and the files a Vue block names with src= (with their lines).

    The blocks of one file share their names (Vue's <script> and <script setup>, Svelte's module
    and instance scripts), so they are read as one program. A ";" in place of the "<" of a block's
    </script> keeps the next block a statement of its own, even on the same line.
    """
    svelte = ext == ".svelte"
    blocks = _sfc_blocks(text, svelte)
    scripts: list[tuple[int, int]] = []
    kinds: set[str] = set()
    srcs: list[tuple[str, int]] = []
    for tag, attrs, start, stop in blocks:
        if not svelte and attrs.get("src"):
            srcs.append((attrs["src"], text.count("\n", 0, start) + 1))
        if tag != "script":
            continue
        lang, kind = attrs.get("lang", "").strip().lower(), attrs.get("type", "").strip().lower()
        if lang not in _SCRIPT_LANGS or kind not in _SCRIPT_TYPES:
            continue  # lang="coffee", type="application/ld+json": not code this parser reads
        kinds.add(_SCRIPT_LANGS[lang] if lang else ".ts" if "typescript" in kind or kind == "ts" else ".js")
        scripts.append((start, stop))
    parts: list[str] = []
    pos = 0
    for k, (start, stop) in enumerate(scripts):
        parts.append(_blank(text[pos:start]))
        parts.append(_dedent(text[start:stop]))
        pos = stop
        if k + 1 < len(scripts) and stop < len(text) and text[start:stop].strip():
            parts.append(";")
            pos = stop + 1
    parts.append(_blank(text[pos:]))
    parse_as = next((e for e in (".tsx", ".ts", ".jsx") if e in kinds), ".js")
    return "".join(parts), parse_as, srcs


_RX_INDENT = re.compile(r"(?m)^[ \t]+(?=\S)")


def _dedent(code: str) -> str:
    """A script block without the indentation all its lines share (Svelte's are often indented), so
    the regular-expression fallback, which looks for declarations at the start of a line, finds them.
    Only the columns change: the lines stay where they are."""
    lines = code.split("\n")
    indents = [m.group() for m in (_RX_INDENT.match(line) for line in lines[1:]) if m]
    if not indents or len(indents) < sum(1 for line in lines[1:] if line.strip()):
        return code  # a line at the left edge: nothing to take off
    common = min(indents, key=len)
    while common and not all(i.startswith(common) for i in indents):
        common = common[:-1]
    if not common:
        return code
    return "\n".join([lines[0]] + [line[len(common):] if line.startswith(common) else line for line in lines[1:]])


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
        self.route_owners: list = []  # (HttpRoute, call node, object node the route was added to)
        # (prefix or None, target, call node, object node it is mounted on, line, verb): target is the
        # ImportRef of a router from another file, or the variable (or plugin key) of one made here
        self.mounts: list = []
        self.inline_plugins: list = []  # (function node, key) of the plugins written right in register()
        self.hono = False  # the file imports hono: basePath() adds a prefix
        self.koa_routers: set = set()  # names bound to a Koa Router class, which takes { prefix }
        self.functions: list = []  # function declarations at any depth
        self.wrappers: dict = {}  # request helper name ("api", "api.get" for a function of an object) -> HttpHelper
        self.wrapper_fetches: set = set()  # start bytes of the fetch calls inside request helpers
        self.instances: dict = {}  # HTTP clients made here (const http = axios.create(...)) -> their base address
        self.origins: dict = {}  # imported local name -> (ImportRef, its name in that module: "api", "default", "*")
        self.required: dict = {}  # start byte of a require("./x") call -> its ImportRef
        self.raw_exports: dict = {}  # exported name -> the local name it stands for (see export_value)
        self.export_nodes: dict = {}  # an exported value written in the export ("default") -> its node
        self.export_requires: list = []  # (exported name, require() call): module.exports = { todos: require(...) }
        self.helper_spans: list = []  # (start byte, HelperCall) of the calls of imported request helpers
        self.plugins: list = []  # (first parameter, start, end, key) of the Fastify plugin functions the file exports
        self.plugin_params: list = []  # (parameter, start, end) whose route calls count in a file without a framework
        self.maybe: set = set()  # plugin keys whose routes count only in a project that uses Fastify
        self.route_file = False
        self.vue_app = False
        self.vue_mount = False
        self.globs = False  # the text may call import.meta.glob() or require.context()

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
        # import.meta.glob() and require.context() are looked for only when the text has them.
        self.globs = b"import.meta" in self.src or b"require.context" in self.src
        for call in self.calls:
            self.import_call(call)
        self.facts.frameworks = _frameworks(self.facts.imports)
        if "nuxt" in self.facts.frameworks or self.facts.path.lower().endswith(".vue"):
            self.clients.update(_NUXT_CLIENTS)  # useFetch("/api/todos") in a Nuxt page
        self.route_file = bool(self.facts.frameworks & _ROUTE_FRAMEWORKS)
        self.hono = "hono" in self.facts.frameworks
        self.koa_routers = _bound_names(self.facts.imports, _KOA_ROUTERS)
        for node in self.declarators:
            self.client_instance(node)
        for name, value in self.export_nodes.items():  # export default axios.create({ baseURL })
            if value.type == "call_expression":
                self.client_instance_of(name, value)
        self.find_plugins()
        self.find_wrappers()
        for call in self.calls:
            self.call(call)
        for node in self.news:
            self.new_expression(node)
        if self.vue_app and self.vue_mount:
            self.entry(UI_ENTRY)
        self.finish()

    def finish(self):
        self.place_routers()
        for sym in self.facts.symbols:
            if sym.name in self.exported_names:
                sym.exported = True
        starts = [pos for pos, _ in self.http_spans]  # already in document order
        helpers = [pos for pos, _ in self.helper_spans]
        guessed = {id(call.guess): call for _, call in self.helper_spans if call.guess is not None}
        for sym, start, end in self.spans:
            if sym.hint:
                continue
            i = bisect.bisect_left(starts, start)
            if i < len(starts) and starts[i] < end:
                call = self.http_spans[i][1]
                sym.hint = f"{call.method} · {call.path}"
                if id(call) in guessed:  # http.get("/todos"): the graph may learn the full address
                    guessed[id(call)].symbols.append(sym.name)
                continue
            i = bisect.bisect_left(helpers, start)
            if i < len(helpers) and helpers[i] < end:  # api("/todos"): the hint comes once the graph finds api
                self.helper_spans[i][1].symbols.append(sym.name)
        self.exports()
        if self.maybe:  # a Fastify plugin's routes in a file that imports no server framework
            facts = self.facts
            facts.plugin_routes = [r for r in facts.http_routes if r.router in self.maybe]
            facts.http_routes = [r for r in facts.http_routes if r.router not in self.maybe]
            facts.plugin_mounts = [m for m in facts.http_mounts if m.owner in self.maybe]
            facts.http_mounts = [m for m in facts.http_mounts if m.owner not in self.maybe]

    def exports(self):
        """FileFacts.exports and http_helpers: the routers, request helpers and Fastify plugins this
        file exports, and the names it passes on from other modules."""
        facts = self.facts
        heads = {name.split(".", 1)[0] for name in self.wrappers}
        known = (heads | set(self.instances) | {r.router for r in facts.http_routes} | {m.owner for m in facts.http_mounts}
                 | {key for *_, key in self.plugins})
        known.discard("")
        exported = set()
        for name, local in self.raw_exports.items():
            value = self.export_nodes.get(local) if local == name else None
            imp = self.required.get(value.start_byte) if value is not None and value.type == "call_expression" else None
            if imp is not None:  # module.exports = require("./routes"), exports.todos = require("./todos")
                facts.exports.append(ExportName(name, source=imp, original="default"))
                if name == "default":
                    facts.exports.append(ExportName("*", source=imp, original="*"))
            elif local in self.origins:  # import { api } from "./api"; export { api }
                imp, original = self.origins[local]
                facts.exports.append(ExportName(name, source=imp, original=original))
            elif local in known or local.split(".", 1)[0] in heads:
                facts.exports.append(ExportName(name, local=local))
                exported.add(local)
        for name, call in self.export_requires:  # module.exports = { todos: require("./todos") }
            imp = self.required.get(call.start_byte)
            if imp is not None:
                facts.exports.append(ExportName(name, source=imp, original="default"))
        for name, helper in self.wrappers.items():
            if name in exported or name.split(".", 1)[0] in exported:
                facts.http_helpers.append(helper)
        for name, base in self.instances.items():
            if name in exported and name not in self.wrappers:
                facts.http_helpers.append(HttpHelper(name, base, client=True))

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
                self.export_declaration(node, decl)
                self.top_level(decl, anchor, True)
                return
            value = node.child_by_field_name("value")
            if value is not None:  # export default X / export default memo(X)
                self.export_value("default", value)
                if value.type == "identifier":
                    self.exported_names.add(self.text(value))
                elif value.type == "call_expression":
                    for a in self.args(value):
                        if a.type == "identifier":
                            self.exported_names.add(self.text(a))
                        elif a.type == "object":  # export default defineComponent({ methods: {...} })
                            self.vue_methods(a)
                elif value.type == "object":  # a Vue component's options: export default { methods: {...} }
                    self.vue_methods(value)
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
                            alias = spec.child_by_field_name("alias")
                            self.raw_exports.setdefault(self.key_name(alias if alias is not None else name),
                                                        self.text(name))
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

    def vue_methods(self, options):
        """The methods of a .vue file's Options API component (methods: { addTodo() { ... } }): its
        functions, which the panel lists like any other."""
        if not self.facts.path.lower().endswith(".vue"):
            return
        methods = self.prop(options, "methods")
        if methods is None or methods.type != "object":
            return
        for item in methods.named_children:
            if item.type == "method_definition":
                name = item.child_by_field_name("name")
                if name is not None:
                    self.add_function(self.text(name), item, item, False)
            elif item.type == "pair":
                key, fn = item.child_by_field_name("key"), item.child_by_field_name("value")
                if key is not None and fn is not None and fn.type in _FUNCTION_TYPES:
                    self.add_function(self.key_name(key), fn, item, False)

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
            self.export_value("default", right)
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
            self.export_value(self.text(prop), right)
            if right.type in _FUNCTION_TYPES:
                self.add_function(self.text(prop), right, anchor, True)
            elif right.type == "identifier":
                self.exported_names.add(self.text(right))

    # --- the export table: what other files can import from this one (FileFacts.exports)
    def export_declaration(self, stmt, decl):
        """export const todos = Router(), export function api(...), export default function api(...)."""
        default = any(ch.type == "default" for ch in stmt.children)
        if decl.type in ("lexical_declaration", "variable_declaration"):
            for item in decl.named_children:
                name = item.child_by_field_name("name") if item.type == "variable_declarator" else None
                if name is not None and name.type == "identifier":
                    self.raw_exports.setdefault(self.text(name), self.text(name))
            return
        name = decl.child_by_field_name("name")
        if name is not None:
            self.raw_exports.setdefault("default" if default else self.text(name), self.text(name))

    def export_value(self, name: str, value):
        """An exported value: a variable (export default router, module.exports = router), or one written
        right there, which stands under the export's own name ("default"; "default.get" for the get
        function of module.exports = { get(path) {...} }): a function, an object of them, axios.create()."""
        if value.type == "identifier":
            self.raw_exports.setdefault(name, self.text(value))
            return
        if value.type == "parenthesized_expression":
            inner = [c for c in value.named_children if c.type != "comment"]
            if len(inner) == 1:
                self.export_value(name, inner[0])
            return
        if value.type not in (*_FUNCTION_TYPES, "call_expression", "object", "await_expression"):
            return
        self.raw_exports.setdefault(name, name)
        self.export_nodes.setdefault(name, value)
        if value.type == "object" and name == "default":  # module.exports = { todos, users }: named exports too
            for ch in value.named_children:
                if ch.type == "shorthand_property_identifier":
                    self.raw_exports.setdefault(self.text(ch), self.text(ch))
                elif ch.type in ("pair", "method_definition"):
                    key = ch.child_by_field_name("key" if ch.type == "pair" else "name")
                    item = ch.child_by_field_name("value") if ch.type == "pair" else ch
                    if key is None or item is None:
                        continue
                    if item.type == "identifier":
                        self.raw_exports.setdefault(self.key_name(key), self.text(item))
                    elif item.type == "call_expression":  # todos: require("./todos")
                        self.export_requires.append((self.key_name(key), item))
                    else:
                        self.raw_exports.setdefault(self.key_name(key), f"default.{self.key_name(key)}")

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
            bases=self.class_bases(node),
        ), node)

    def class_bases(self, node) -> tuple[str, ...]:
        """What a class extends, the last part of each name as in Python (class NotFound extends
        errors.HttpError -> ("HttpError",)). One made by a call or an index (extends mixin(Base)) is none."""
        for heritage in node.named_children:
            if heritage.type != "class_heritage":
                continue
            values = []
            for item in heritage.named_children:
                if item.type == "extends_clause":  # TypeScript: extends Base<T>, before any implements
                    values += item.children_by_field_name("value")
                elif item.type != "implements_clause":
                    values.append(item)
            names = []
            for value in values:
                if value.type == "member_expression":
                    value = value.child_by_field_name("property")
                if value is not None and value.type in ("identifier", "property_identifier"):
                    names.append(self.text(value))
            return tuple(names)
        return ()

    def add_symbol(self, sym: Symbol, node):
        self.facts.symbols.append(sym)
        self.spans.append((sym, node.start_byte, node.end_byte))

    # --- imports
    def add_import(self, spec: str, names: list[str], kind: str, line: int, origins=()):
        """Record an import; `origins` are (local name, name in that module) pairs of what it binds."""
        spec = spec.strip()
        if not spec:
            return None
        imp = ImportRef(spec=spec, names=names, kind=kind, line=line)
        self.facts.imports.append(imp)
        if kind in ("import", "require") and _package_name(spec) in _CLIENT_PACKAGES:
            self.clients.update(names)
        for local, original in origins:
            self.origins.setdefault(local, (imp, original))
        return imp

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
            self.add_import(spec, names[:1], "require", line, [(n, "default") for n in names[:1]])
            return
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            self.add_import(spec, [], "side-effect", line)
            return
        names, origins = [], []
        for ch in clause.named_children:
            if ch.type == "identifier":
                names.append(self.text(ch))
                origins.append((names[-1], "default"))
            elif ch.type == "namespace_import":
                ids = [c for c in ch.named_children if c.type == "identifier"]
                if ids:
                    names.append(self.text(ids[-1]))
                    origins.append((names[-1], "*"))
            elif ch.type == "named_imports":
                for item in ch.named_children:
                    if item.type != "import_specifier":
                        continue
                    original = item.child_by_field_name("name")
                    alias = item.child_by_field_name("alias")
                    name = alias if alias is not None else original
                    if name is not None:
                        names.append(self.text(name))
                        origins.append((names[-1], self.key_name(original) if original is not None else names[-1]))
        self.add_import(spec, names, "import", line, origins)

    def reexport(self, node, src):
        spec = self.literal(src)
        if spec is None:
            return
        names, star, passed = [], False, []  # passed: (exported name, its name in the module)
        for ch in node.children:
            if ch.type == "export_clause":
                for item in ch.named_children:
                    if item.type != "export_specifier":
                        continue
                    original = item.child_by_field_name("name")
                    alias = item.child_by_field_name("alias")
                    name = alias if alias is not None else original
                    if name is not None:
                        names.append(self.text(name))
                        passed.append((names[-1], self.key_name(original) if original is not None else names[-1]))
            elif ch.type == "namespace_export":
                ids = [c for c in ch.named_children if c.type == "identifier"]
                if ids:
                    names.append(self.text(ids[-1]))
                    passed.append((names[-1], "*"))
            elif ch.type == "*":
                star = True
        if star and not names:
            names = ["*"]
            passed = [("*", "*")]
        imp = self.add_import(spec, names, "reexport", node.start_point.row + 1)
        if imp is not None:
            self.facts.exports.extend(ExportName(name, source=imp, original=original) for name, original in passed)

    def import_call(self, node):
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "import":
            args = self.args(node)
            spec = self.literal(args[0]) if args else None
            if spec:
                self.add_import(spec, [], "dynamic", node.start_point.row + 1)
            elif args:
                self.computed_import(args[0], "dynamic", node)
        elif fn.type == "identifier" and self.text(fn) == "require":
            args = self.args(node)
            spec = self.literal(args[0]) if args else None
            if spec:
                imp = self.add_import(spec, self.binding_names(node), "require", node.start_point.row + 1,
                                      self.binding_origins(node))
                if imp is not None:
                    self.required[node.start_byte] = imp
            elif args:
                self.computed_import(args[0], "require", node)
        elif fn.type == "member_expression" and self.globs:
            obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
            if obj is None or prop is None:
                return
            name = self.text(prop)
            if obj.type == "meta_property" and self.text(obj).replace(" ", "") == "import.meta":
                if name in ("glob", "globEager"):
                    self.glob_import(node)
                elif name == "webpackContext":
                    self.context_import(node, True)
            elif name == "context" and obj.type == "identifier" and self.text(obj) == "require":
                self.context_import(node, False)

    def computed_import(self, arg, kind: str, node):
        """import(`./locales/${lang}.json`), require("./plugins/" + name): every file the name can be.
        A name whose parts are all known constants is an ordinary import; one with no written part
        that says where to look (require(name), import(`./${name}`)) is left out."""
        if arg.type not in ("template_string", "binary_expression"):
            return
        raw = self.url(arg)
        if not raw:
            return
        line = node.start_point.row + 1
        if "${}" not in raw:
            self.add_import(raw, self.binding_names(node) if kind == "require" else [], kind, line)
            return
        spec = _pattern_spec(raw)
        if spec:
            self.add_import(spec, [], kind, line)  # no names: which module a variable holds is not known

    def glob_import(self, node):
        """import.meta.glob("./pages/*.vue"), import.meta.glob(["./a/*.js", "!./a/skip.js"]) (Vite)."""
        args = self.args(node)
        if not args:
            return
        items = [c for c in args[0].named_children if c.type != "comment"] if args[0].type == "array" else [args[0]]
        patterns = [self.literal(item) for item in items]
        if not patterns or any(p is None for p in patterns):
            return  # Vite takes only written patterns
        patterns = [p.strip() for p in patterns if p.strip()]
        keep = [p for p in patterns if not p.startswith("!")]
        if keep:
            self.add_import(keep[0], keep[1:] + [p for p in patterns if p.startswith("!")], "glob",
                            node.start_point.row + 1)

    def context_import(self, node, options: bool):
        """require.context("./dir", true, /\\.js$/) and import.meta.webpackContext("./dir", { recursive,
        regExp }) (webpack): the files of a folder, all the way down unless told otherwise, whose
        path from it ("./sub/a.js") the regular expression finds."""
        args = self.args(node)
        folder = self.literal(args[0]) if args else None
        if folder is None:
            return
        deep, regex = True, None
        if options:
            opts = args[1] if len(args) > 1 else None
            recursive, regex = self.prop(opts, "recursive"), self.prop(opts, "regExp")
            deep = recursive is None or recursive.type != "false"
        else:
            deep = len(args) < 2 or args[1].type != "false"
            regex = args[2] if len(args) > 2 else None
        if regex is not None and regex.type != "regex":
            return  # a regular expression made at run time
        spec = (folder.strip().rstrip("/") or ".") + ("/**" if deep else "/*")
        self.add_import(spec, [self.text(regex)] if regex is not None else [], "glob", node.start_point.row + 1)

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

    def binding_origins(self, call) -> list[tuple[str, str]]:
        """(local name, name in the module) of what a require() binds: const api = require("./api")
        -> ("api", "default") (module.exports), const { get: load } = require("./api") -> ("load",
        "get"), const api = require("./api").client -> ("api", "client")."""
        parent, member = call.parent, "default"
        if parent is not None and parent.type == "member_expression" and parent.child_by_field_name("object") == call:
            prop = parent.child_by_field_name("property")
            member = self.text(prop) if prop is not None else ""
            parent = parent.parent
        if not member or parent is None or parent.type != "variable_declarator":
            return []
        name = parent.child_by_field_name("name")
        if name is None:
            return []
        if name.type == "identifier":
            return [(self.text(name), member)]
        out = []
        if name.type == "object_pattern" and member == "default":
            for ch in name.named_children:
                if ch.type == "shorthand_property_identifier_pattern":
                    out.append((self.text(ch), self.text(ch)))
                elif ch.type == "pair_pattern":
                    key, value = ch.child_by_field_name("key"), ch.child_by_field_name("value")
                    if key is not None and value is not None and value.type == "identifier":
                        out.append((self.text(value), self.key_name(key)))
        return out

    # --- HTTP clients, routes, entry clues
    def client_instance(self, decl):
        """const api = axios.create({ baseURL }) makes `api` an HTTP client too."""
        name = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name is None or value is None or name.type != "identifier" or value.type != "call_expression":
            return
        self.client_instance_of(self.text(name), value)

    def client_instance_of(self, var: str, value):
        """`var` holds the client axios.create({ baseURL }) (or ky.create, ofetch.create) makes."""
        fn = value.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        if obj is None or prop is None or self.object_name(obj) not in self.clients:
            return
        if self.text(prop) not in ("create", "extend"):
            return
        self.clients.add(var)
        self.instances.setdefault(var, "")
        args = self.args(value)
        if args and args[0].type == "object":
            base = self.prop(args[0], "baseURL")
            if base is None:
                base = self.prop(args[0], "prefixUrl")
            base_url = self.url(base) if base is not None else None
            if base_url:
                self.client_bases[var] = base_url
                self.instances[var] = base_url

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
            elif name in self.origins and args:
                self.helper_call(node, name, "", args)  # api("/todos") with api imported from a project file
            return
        if fn.type != "member_expression":
            return
        obj = fn.child_by_field_name("object")
        prop_node = fn.child_by_field_name("property")
        if obj is None or prop_node is None:
            return
        prop = self.text(prop_node)
        obj_name = self.object_name(obj)
        if obj.type == "identifier" and f"{obj_name}.{prop}" in self.wrappers:
            self.wrapper_call(f"{obj_name}.{prop}", node, args)  # api.get("/todos") with api = { get(path) {...} }
            return
        if (obj.type == "identifier" and obj_name in self.origins and obj_name not in self.clients and args
                and prop not in _MOUNT_METHODS):
            calls, routes = len(self.facts.http_calls), len(self.facts.http_routes)
            self.member_call(node, obj, obj_name, prop, args)
            if len(self.facts.http_routes) == routes:  # router.get("/x", handler) is a route, not a request
                guess = self.facts.http_calls[calls] if len(self.facts.http_calls) > calls else None
                self.helper_call(node, obj_name, prop, args, guess)  # http.get("/todos") with http imported
            return
        self.member_call(node, obj, obj_name, prop, args)

    def member_call(self, node, obj, obj_name: str, prop: str, args):
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
        elif prop == "route" and (self.route_file or self.in_plugin(obj, node)) and args and args[0].type == "object":
            self.object_route(node, obj, args[0])  # fastify.route({ method, url })
        elif prop == "use" and len(args) >= 2 and not _middleware_node(args[0]):
            self.mount(node, obj, obj_name, args[0], args[-1], prop)  # Express, Koa router: app.use("/api", router)
        elif prop == "use" and args and self.route_file:
            # no path: app.use(router), api.use(sub.routes()), api.use(cors(), sub), api.use(v1.routes(), v1.allowedMethods())
            self.mount(node, obj, obj_name, None, args[-1], prop)
        elif prop == "route" and len(args) == 2 and args[0].type in ("string", "template_string"):
            self.mount(node, obj, obj_name, args[0], args[1], prop)  # Hono: app.route("/api", api)
        elif prop == "register" and args and (len(args) == 1 or args[1].type == "object"):
            prefix = self.prop(args[1], "prefix") if len(args) > 1 else None
            if prefix is not None:  # Fastify: app.register(routes, { prefix: "/api" })
                self.mount(node, obj, obj_name, prefix, args[0], prop)
            elif self.route_file or self.in_plugin(obj, node):  # app.register(routes): where the app or plugin it is registered on answers
                self.mount(node, obj, obj_name, None, args[0], prop)
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
        """Request helpers: functions (and functions of an exported object, api = { get(path) {...} })
        whose fetch() address is one of their parameters, after a base address of their own."""
        found = [(self.text(n), f) for f in self.functions if (n := f.child_by_field_name("name")) is not None]
        exported = set(self.raw_exports.values())
        for decl in self.declarators:
            name, value = decl.child_by_field_name("name"), decl.child_by_field_name("value")
            if name is None or value is None or name.type != "identifier":
                continue
            if value.type in _FUNCTION_TYPES:
                found.append((self.text(name), value))
            elif value.type == "object" and self.text(name) in exported:
                found.extend(self.object_functions(self.text(name), value))
        for name, value in self.export_nodes.items():  # export default (url) => fetch(url), module.exports = {...}
            if value.type in _FUNCTION_TYPES:
                found.append((name, value))
            elif value.type == "object":
                found.extend(self.object_functions(name, value))
        for name, fn in found:
            if name != "fetch" and name not in self.clients and name not in self.wrappers:
                spec = self.wrapper_spec(fn, name)
                if spec:
                    self.wrappers[name] = spec

    def object_functions(self, name: str, obj) -> list:
        """("api.get", function) for each function written in an object literal: { get: (p) => ..., post(p) {...} }."""
        out = []
        for ch in obj.named_children:
            if ch.type == "method_definition":
                key = ch.child_by_field_name("name")
                if key is not None:
                    out.append((f"{name}.{self.key_name(key)}", ch))
            elif ch.type == "pair":
                key, value = ch.child_by_field_name("key"), ch.child_by_field_name("value")
                if key is not None and value is not None and value.type in _FUNCTION_TYPES:
                    out.append((f"{name}.{self.key_name(key)}", value))
        return out

    def param_default(self, fn, at: int) -> str:
        """The written default of a function's parameter at `at` (method = "GET"), "" if none."""
        node = fn.child_by_field_name("parameters")
        params = [p for p in node.named_children if p.type != "comment"] if node is not None else []
        if at >= len(params):
            return ""
        param = params[at]
        value = param.child_by_field_name("right" if param.type == "assignment_pattern" else "value")
        lit = (self.literal(value) or "").strip().upper()
        return lit if lit in _KNOWN_METHODS else ""

    def wrapper_spec(self, fn, name: str = ""):
        """How a helper builds its request (an HttpHelper): the URL from one of its parameters, and the method."""
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
            return HttpHelper(name, raw[:raw.index("\x00")], int(slots[0]), method, method_at,
                              self.param_default(fn, method_at) if method_at is not None else "", options_at,
                              line=fn.start_point.row + 1)
        return None

    def call_args(self, args) -> tuple[list, list]:
        """A call's arguments as model.helper_request() reads them: each as address text, and the method
        of each options object."""
        return [self.url(a) for a in args], [self.method(a) if a.type == "object" else None for a in args]

    def wrapper_call(self, name: str, node, args):
        """api("/api/pick-folder", "POST") with `api` a request helper."""
        found = helper_request(self.wrappers[name], *self.call_args(args))
        if found:
            self.add_call(found[0], found[1], node)

    def helper_call(self, node, name: str, member: str, args, guess=None):
        """A call of a request helper or HTTP client imported from another file: what the graph needs to
        find the helper and build the request (HelperCall). Only calls that pass an address count."""
        imp, original = self.origins[name]
        if imp.spec.startswith("node:") or _package_name(imp.spec) in _NODE_BUILTINS:
            return  # path.join("/a", "b"), fs.readFile("/etc/hosts")
        texts, options = self.call_args(args)
        if member in _HTTP_METHODS:
            if texts[0] is None:
                return  # http.get(url) with url a variable: no address
        elif not any(t and normalize_http_path(t) for t in texts):
            return  # format(date), t("title"): not a request
        if original in ("default", "*") or not member:
            export, member = (member, "") if original == "*" else (original, member)
        else:
            export = original  # http.get with import { http }: the get method of the client http
        call = HelperCall(imp, export, member, texts, options, node.start_point.row + 1, guess)
        self.facts.helper_calls.append(call)
        self.helper_spans.append((node.start_byte, call))

    def member_http(self, node, obj, obj_name: str, prop: str, args):
        if obj_name in self.clients and obj.type in ("identifier", "member_expression"):
            if prop != "all" and args:
                self.client_call(obj_name, prop.upper(), args[0], node)
            return
        handler_like = bool(args) and (args[-1].type in _FUNCTION_TYPES or args[-1].type == "identifier")
        if self.route_file or (handler_like and (_ROUTER_LIKE.match(obj_name or "") or self.in_plugin(obj, node))):
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
        self.route_owners.append((route, node, obj))
        return True

    def mount(self, node, obj, obj_name: str, path_node, target_node, verb: str):
        """app.use("/api/todos", todos): the routes of `todos` answer under /api/todos. With no path
        (path_node None: app.use(router), register(routes)) they answer where `obj` does."""
        if not (self.route_file or _ROUTER_LIKE.match(obj_name or "") or self.in_plugin(obj, node)):
            return
        raw = None
        if path_node is not None:
            raw = self.url(path_node)
            if not raw or normalize_http_path(raw) is None:
                return  # app.use(express.json()) or a prefix that is not a path
        if verb == "register" and target_node.type in _FUNCTION_TYPES:  # register(async (f) => {...}, { prefix })
            target = f"\x00{target_node.start_byte}"
            self.inline_plugins.append((target_node, target))
        else:
            target = self.mount_target(target_node)
        if target:
            self.mounts.append((raw, target, node, obj, node.start_point.row + 1, verb))

    def mount_target(self, node, depth: int = 0):
        """The name of a router made in this file, or (the import a mounted router comes from, its name
        in that module): ("default") for a default import, a require() or import(), the name of a
        named import, the property of a module (routes.todos with import * as routes or require("./
        routes")), "" for the whole module (what a factory there makes: makeTodosRouter(db))."""
        if node is None or depth > 10:
            return None
        t = node.type
        if t == "identifier":
            name = self.text(node)
            found = self.origins.get(name)
            if found is None or found[0].kind not in ("import", "require"):
                return name
            return found[0], "" if found[1] == "*" else found[1]
        if t == "member_expression":  # todos.router, routes.todos
            obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
            target = self.mount_target(obj, depth + 1)
            if not isinstance(target, tuple):
                return target
            whole = obj.type in ("call_expression", "parenthesized_expression", "await_expression") or (
                obj.type == "identifier" and self.origins[self.text(obj)][1] in ("default", "*"))
            return target[0], self.text(prop) if whole and prop is not None else ""
        if t in ("await_expression", "parenthesized_expression"):
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
                return (ImportRef(spec=spec, kind=kind, line=node.start_point.row + 1), "default") if spec else None
            if fn.type == "member_expression":  # todos.routes() (Koa)
                return self.mount_target(fn.child_by_field_name("object"), depth + 1)
            if fn.type in ("identifier", "call_expression"):  # makeTodosRouter(db), require("./todos")(db)
                target = self.mount_target(fn, depth + 1)
                return (target[0], "") if isinstance(target, tuple) else None
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

    def object_route(self, node, owner, obj):
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
        route = HttpRoute(method=method, path=path, handler=handler, line=node.start_point.row + 1)
        self.facts.http_routes.append(route)
        self.route_owners.append((route, node, owner))

    # --- routers placed in this file: api.use("/todos", todos); app.use("/api", api)
    def place_routers(self):
        own, under = self.router_declarations()
        if not (own or under or self.mounts or self.hono or self.route_owners):
            return
        # Routes move only under a router of this file, or under a Hono basePath() in their own chain.
        local = own or under or self.hono or any(isinstance(m[1], str) for m in self.mounts)
        owners = self.route_owners
        routes_at = [self.where(call, obj) for _, call, obj in owners]
        mounts_at = [self.where(m[2], m[3]) for m in self.mounts]
        scopes = self.plugin_scopes()  # fastify.get() in async function routes(fastify): a route of `routes`
        if scopes:
            routes_at = _in_plugins(routes_at, [call.start_byte for _, call, _ in owners], scopes)
            mounts_at = _in_plugins(mounts_at, [m[2].start_byte for m in self.mounts], scopes)
        if not local:  # nothing moves: each route belongs to the router it is added to
            for (route, _, _), (name, _, _) in zip(owners, routes_at):
                route.router = name
            owners = routes_at = []
        _place_routers(self.facts, [(route, at) for (route, _, _), at in zip(owners, routes_at)],
                       under + [(m[0], m[1], at, m[4]) for m, at in zip(self.mounts, mounts_at)], own)

    def where(self, call, obj) -> tuple[str, str, bool]:
        """The router a route or mount call adds to, the raw prefix the call itself puts in front, and
        whether that router is made right in the call's chain (see _place_routers):

        api.get(...) -> ("api", "", False); app.basePath("/v1").get(...) -> ("app", "/v1", False);
        const todos = new Hono().basePath("/api").get(...) -> ("todos", "/api", True).
        """
        if obj.type == "identifier":
            return self.text(obj), "", False
        root, parts = self.chain_root(obj)
        head = ""
        for raw in reversed(parts):
            head = _join_raw(head, raw)
        if root is None:  # this.router.get(...)
            return "", head, False
        if root.type == "identifier":
            return self.text(root), head, False
        return self.chain_variable(call), _join_raw(self.made_prefix(root), head), True

    def chain_root(self, node):
        """(router at the start, Hono basePath()s on the way, outermost first) of a call chain.

        app.basePath("/v1").get(...) -> (app, ["/v1"]); router.route("/x").get(h) -> (router, []);
        new Hono().basePath("/api") -> (new Hono(), ["/api"]); express.Router() is a router made there.
        None when the chain starts with something else (this.router, a call in parentheses).
        """
        parts: list[str] = []
        for _ in range(50):
            if node is None:
                break
            t = node.type
            if t in ("identifier", "new_expression"):
                return node, parts
            if t == "member_expression":
                node = node.child_by_field_name("object")
                continue
            if t != "call_expression":
                break
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                return node, parts  # Router(), Fastify(): made here
            prop = fn.child_by_field_name("property")
            name = self.text(prop) if prop is not None else ""
            if name == "Router":
                return node, parts  # express.Router()
            if name == "basePath" and self.hono:
                args = self.args(node)
                raw = self.url(args[0]) if args else None
                if raw:
                    parts.append(raw)
            node = fn.child_by_field_name("object")
        return None, parts

    def made_prefix(self, root) -> str:
        """Own prefix of a router made right here: Koa new Router({ prefix: "/api" })."""
        if not self.koa_routers or root.type not in ("new_expression", "call_expression"):
            return ""
        ctor = root.child_by_field_name("constructor" if root.type == "new_expression" else "function")
        if ctor is None or ctor.type != "identifier" or self.text(ctor) not in self.koa_routers:
            return ""
        args = self.args(root)
        value = self.prop(args[0], "prefix") if args else None
        return (self.url(value) or "") if value is not None else ""

    def chain_variable(self, call) -> str:
        """Variable a call chain is assigned to: todos in const todos = new Hono().get(...).post(...)."""
        node = call
        for _ in range(200):
            parent = node.parent
            if parent is None:
                return ""
            if parent.type == "member_expression" and parent.child_by_field_name("object") == node:
                node = parent.parent  # the next call of the chain
                if node is None or node.type != "call_expression" or node.child_by_field_name("function") != parent:
                    return ""
                continue
            if parent.type == "variable_declarator" and parent.child_by_field_name("value") == node:
                name = parent.child_by_field_name("name")
                return self.text(name) if name is not None and name.type == "identifier" else ""
            return ""
        return ""

    def router_declarations(self) -> tuple[dict, list]:
        """Own prefixes of the routers made in this file (Koa new Router({ prefix: "/api" }), Hono
        new Hono().basePath("/api")), and Hono routers made under another one (const v1 =
        app.basePath("/v1")), as mounts of the new variable."""
        own: dict[str, str] = {}
        under: list = []
        if not (self.koa_routers or self.hono):
            return own, under
        for decl in self.declarators:
            name, value = decl.child_by_field_name("name"), decl.child_by_field_name("value")
            if name is None or value is None or name.type != "identifier":
                continue
            if value.type == "new_expression" or value.type == "call_expression":  # quick look first
                fn = value.child_by_field_name("constructor" if value.type == "new_expression" else "function")
                if fn is None or (fn.type == "identifier" and self.text(fn) not in self.koa_routers):
                    continue  # new Hono() or make(): no prefix of its own
                if fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop is None or self.text(prop) not in _CHAIN_METHODS:
                        continue  # path.join(...), express.Router()
            else:
                continue
            root, parts = self.chain_root(value)
            if root is None:
                continue
            head = ""
            for raw in reversed(parts):
                head = _join_raw(head, raw)
            var = self.text(name)
            if root.type != "identifier":
                prefix = _join_raw(self.made_prefix(root), head)
                if prefix:
                    own[var] = prefix
            elif head and self.text(root) != var:
                under.append((head, var, (self.text(root), "", False), decl.start_point.row + 1))
        return own, under

    def plugin_scopes(self) -> list:
        """(first parameter, start, end, key) of the Fastify plugin functions of this file that a
        register() call names (async function routes(fastify) { ... }) or holds (register(async (f) => ...)),
        and of the plugin functions it exports (see find_plugins)."""
        names = {m[1] for m in self.mounts if m[5] == "register" and isinstance(m[1], str)}
        found = list(self.inline_plugins)
        if names:
            for fn in self.functions:
                name = fn.child_by_field_name("name")
                if name is not None and self.text(name) in names:
                    found.append((fn, self.text(name)))
            for decl in self.declarators:
                name, value = decl.child_by_field_name("name"), decl.child_by_field_name("value")
                if (name is not None and value is not None and name.type == "identifier"
                        and value.type in _FUNCTION_TYPES and self.text(name) in names):
                    found.append((value, self.text(name)))
        scopes = list(self.plugins)
        for fn, key in found:
            params = self.params(fn)
            if params and params[0]:
                scopes.append((params[0], fn.start_byte, fn.end_byte, key))
        return scopes

    def local_functions(self) -> dict:
        """Function name -> its function node, for the functions declared at the top of the file
        (function routes(f) {...}, const routes = async (f) => {...})."""
        out = {}
        for fn in self.functions:
            name = fn.child_by_field_name("name")
            if name is not None and fn.parent is not None and fn.parent.type in ("program", "export_statement"):
                out.setdefault(self.text(name), fn)
        for decl in self.declarators:
            name, value = decl.child_by_field_name("name"), decl.child_by_field_name("value")
            top = decl.parent.parent if decl.parent is not None else None
            if (name is not None and value is not None and name.type == "identifier" and value.type in _FUNCTION_TYPES
                    and top is not None and top.type in ("program", "export_statement")):
                out.setdefault(self.text(name), value)
        return out

    def find_plugins(self):
        """Fastify plugin functions this file exports: module.exports = async function (fastify, opts)
        { ... }, export default async function routes(fastify) { ... }, export const todos = async (app)
        => { ... }, module.exports = fp(async (fastify) => { ... }). The function must be an export and
        use its first parameter as a Fastify instance: a route with a handler written right there
        (fastify.get("/x", async () => ...)), fastify.route({ ... }) or fastify.register(...); a route to
        a handler given by name (fastify.get("/x", list)) only when the parameter is named like an
        instance (fastify, instance, app, …router). A parameter named like an HTTP client (http, api)
        is one: makeApi(http) { http.get("/x", options) } sends requests, and cache.get("/x", load) is
        no route either. Its routes belong to the plugin (HttpRoute.router is its export's
        local name), so another file's register() moves them. In a file that imports no server
        framework, calls on that parameter count only for this reason (self.plugin_params, with the
        parameters of the plugins registered on it there), so the graph keeps them only in a project
        that uses Fastify (self.maybe: unless the parameter has a router's name, app.get(...) counts as
        it always did)."""
        if not self.raw_exports:
            return
        local = None
        candidates: dict = {}  # plugin key (the export's local name) -> function node
        for name in dict.fromkeys(self.raw_exports.values()):
            value = self.export_nodes.get(name)
            if value is not None:  # written in the export: module.exports = async function (fastify) {...}
                if value.type == "call_expression":  # module.exports = fp(async (fastify) => {...})
                    args = self.args(value)
                    value = args[0] if args else None
                if value is not None and value.type in _FUNCTION_TYPES:
                    candidates[name] = value
                continue
            if local is None:
                local = self.local_functions()
            if name in local:
                candidates[name] = local[name]
        if not candidates:
            return
        uses: dict = {}  # name -> start bytes of the calls name.get(...), name.route(...), name.register(...)
        for call in self.calls:
            fn = call.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                continue
            obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
            if obj is None or prop is None or obj.type != "identifier" or self.text(prop) not in _PLUGIN_METHODS:
                continue
            args = self.args(call)
            method = self.text(prop)
            if method in _ROUTE_METHODS:  # fastify.get("/todos", handler): a path first, a handler last
                last = args[-1].type if args else ""
                route = (len(args) >= 2 and args[0].type in ("string", "template_string")
                         and (last in _FUNCTION_TYPES or (last in ("identifier", "member_expression")
                                                          and _instance_like(self.text(obj)))))
            else:  # fastify.route({ method, url, handler }), fastify.register(plugin)
                route = bool(args) and (method == "register" or args[0].type == "object")
            if route:
                uses.setdefault(self.text(obj), []).append(call)
        for key, fn in candidates.items():
            params = self.params(fn)
            param = params[0] if params else None
            calls = uses.get(param or "", [])
            if not param or _client_like(param) or not any(fn.start_byte <= c.start_byte < fn.end_byte for c in calls):
                continue
            self.plugins.append((param, fn.start_byte, fn.end_byte, key))
            if not self.route_file:
                self.plugin_params.append((param, fn.start_byte, fn.end_byte))
                if not _ROUTER_LIKE.match(param):
                    self.maybe.add(key)
        if not self.plugin_params:
            return
        registers = [c for calls in uses.values() for c in calls if self.text(
            c.child_by_field_name("function").child_by_field_name("property")) == "register"]
        functions = None
        for _ in range(5):  # plugins registered on a plugin, a few deep
            added = False
            for call in registers:
                fn = call.child_by_field_name("function")
                if not self.in_plugin(fn.child_by_field_name("object"), call):
                    continue
                target = self.args(call)[0]
                if target.type == "identifier":
                    if functions is None:
                        functions = self.local_functions()
                    target = functions.get(self.text(target))
                if target is None or target.type not in _FUNCTION_TYPES + ("function_declaration",):
                    continue
                params = self.params(target)
                span = (params[0], target.start_byte, target.end_byte) if params and params[0] else None
                if span and span not in self.plugin_params:
                    self.plugin_params.append(span)
                    added = True
            if not added:
                break

    def in_plugin(self, obj, node) -> bool:
        """Is this call made on a Fastify plugin's parameter, inside that plugin (see find_plugins)?"""
        if not self.plugin_params or obj is None or obj.type != "identifier":
            return False
        name, pos = self.text(obj), node.start_byte
        return any(param == name and start <= pos < end for param, start, end in self.plugin_params)

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


def _client_like(name: str) -> bool:
    """A variable named like an HTTP client (http, api, httpClient): its get("/x") is a request."""
    return bool(_CLIENT_LIKE.search(name) and not _NOT_CLIENT.search(name))


def _instance_like(name: str) -> bool:
    """A variable named like a Fastify instance or a router (fastify, instance, app, server, apiRouter)."""
    return bool(_INSTANCE_LIKE.search(name) or _ROUTER_LIKE.match(name))


def _middleware_node(node) -> bool:
    """A first argument of use() that cannot be its path: cors(), v1.routes(), new X(), a function
    written right there. Express and @koa/router take a path only as a string (or a list of them),
    so the router after it answers where the app or router it is mounted on does. A variable
    (app.use(PREFIX, r)) may be either, so it is not taken for middleware."""
    return node.type in ("call_expression", "new_expression") or node.type in _FUNCTION_TYPES


def _join_raw(outer: str, inner: str) -> str:
    """Two raw URL prefixes one after the other: ("/api", "/todos") -> "/api/todos"; "" and "/" add nothing."""
    if not outer:
        return inner
    if not inner.strip("/"):
        return outer
    return outer.rstrip("/") + "/" + inner.lstrip("/")


def _bound_names(imports: list[ImportRef], packages: frozenset) -> set[str]:
    """Names that imports or requires of these packages bind: {"Router"} for require("@koa/router")."""
    return {name for imp in imports if imp.kind in ("import", "require") and _package_name(imp.spec) in packages
            for name in imp.names}


def _in_plugins(wheres: list, positions: list, scopes: list) -> list:
    """`wheres` (see _place_routers) of calls at `positions`, with the variable replaced by the plugin
    function the call is made in when that variable is the function's first parameter:
    fastify.get(...) in async function routes(fastify) { ... } adds a route to the plugin `routes`.
    The innermost such function counts.

    scopes: (first parameter, start, end, key) of the plugin functions. One pass over both, in
    source order.
    """
    ordered = sorted(scopes, key=lambda s: (s[1], -s[2]))
    out = list(wheres)
    stack: list = []  # the scopes open at the current position, outermost first
    i = 0
    for n in sorted(range(len(wheres)), key=positions.__getitem__):
        (name, head, made_here), pos = wheres[n], positions[n]
        while i < len(ordered) and ordered[i][1] <= pos:
            while stack and stack[-1][2] <= ordered[i][1]:
                stack.pop()
            stack.append(ordered[i])
            i += 1
        while stack and stack[-1][2] <= pos:
            stack.pop()
        if name and not made_here:
            out[n] = (next((key for param, start, end, key in reversed(stack)
                            if param == name and start <= pos < end), name), head, made_here)
    return out


def _place_routers(facts: FileFacts, routes: list, mounts: list, own: dict) -> None:
    """Routers made in this file answer where they are placed in it, and mounts of imported routers
    learn the prefix of the router they are mounted on (HttpMount.base).

    const api = express.Router(); api.use("/todos", todos); app.use("/api", api): the routes of todos
    answer under /api/todos, and so do routes added to a router of this file mounted the same way.
    A router placed more than once (app.use("/v1", api) and app.use("/v2", api)) answers at each
    place: one route, or one mount, per place, at most 8, nine routers deep (as in python_lang).
    One mounted with no prefix answers where the router or app it is mounted on does.

    routes: (HttpRoute, where). mounts: (prefix or None, target, where, line), target being (the
    ImportRef of a router from another file, its name there: see HttpMount.router) or the variable
    (or plugin key) of a router of this file. `where` is (router variable, raw prefix the call itself
    puts in front, made_here): see _TreeAnalyzer.where(). A router made right in the chain (new
    Hono().basePath("/api").get(...)) has its own prefix in that head already.
    own: router variable -> its own raw prefix (Koa new Router({ prefix }), Hono basePath()).
    Every route and every mount of an imported router learns the outermost router of this file it
    answers under (HttpRoute.router, HttpMount.owner), once per place; a mount of an imported router
    with no prefix is always recorded, like python_lang: another file may mount this one (routes/
    index.js: router.use(auth), app.js: app.use("/api", routes)), or mount the same router under a prefix.
    """
    places: dict[str, list] = {}  # router variable -> [(prefix, where it is mounted)]
    for prefix, target, where, _ in mounts:
        if isinstance(target, str):
            places.setdefault(target, []).append((prefix or "", where))
    known: dict[tuple[str, int], list[tuple[str, str]]] = {}

    def outer(name: str, trail: frozenset) -> list[tuple[str, str]]:
        """(raw prefix `name` is placed under in this file before its own one, the outermost router of
        that place) pairs. Each router is worked out once per depth: routers mounted many times on each
        other could otherwise take minutes."""
        if not name or name not in places or name in trail or len(trail) > 8:
            return [("", name)]
        key = (name, len(trail))
        if key not in known:
            inner = trail | {name}
            found = [(_join_raw(start, prefix), top) for prefix, where in places[name]
                     for start, top in starts(where, inner)]
            known[key] = list(dict.fromkeys(found))[:8]
        return known[key]

    def starts(where: tuple, trail: frozenset = frozenset()) -> list[tuple[str, str]]:
        """(raw prefix the addresses of a call at `where` begin with, its outermost router) pairs."""
        name, head, made_here = where
        mine = "" if made_here else own.get(name, "")
        return list(dict.fromkeys((_join_raw(_join_raw(start, mine), head), top) for start, top in outer(name, trail)))

    plain: dict[str, str | None] = {}  # literal prefix -> normalized ("/api/" -> "/api"); None for any other

    def under(base: str, path: str) -> str | None:
        """join_http_path(base, path) for a normalized route path, without normalizing it again for a
        literal prefix: routes moved by thousands would spend most of their time there."""
        done = plain.get(base, False)
        if done is False:
            done = plain[base] = re.sub(r"/{2,}", "/", base).rstrip("/") if _LITERAL_PREFIX.fullmatch(base) else None
        if done is None or path[-1:].isspace():  # "/x #top" is normalized to "/x ", which joining strips
            return join_http_path(base, path)
        return (done if path == "/" else done + path) or "/"

    seen: dict[tuple, list[tuple[str, str]]] = {}  # where -> starts(where): routes of one router share it
    moved: dict[int, list[HttpRoute]] = {}
    for route, where in routes:
        bases = seen.get(where)
        if bases is None:
            bases = seen[where] = starts(where)
        tops = list(dict.fromkeys(top for _, top in bases))
        route.router = tops[0] if tops else ""
        if not any(base.strip("/") for base, _ in bases):  # answers where it is written
            if len(tops) > 1:  # under two routers of this file: another file may mount either
                moved[id(route)] = [route] + [HttpRoute(route.method, route.path, route.handler, route.line,
                                                        route.base, top) for top in tops[1:]]
            continue
        paths = dict.fromkeys((p, top) for p, top in ((under(base, route.path), top) for base, top in bases) if p)
        moved[id(route)] = [HttpRoute(route.method, p, route.handler, route.line, router=top) for p, top in paths] or [route]
    if moved:
        facts.http_routes = [r for route in facts.http_routes for r in moved.get(id(route), [route])]

    for prefix, target, where, line in mounts:
        if isinstance(target, tuple):
            bases = seen.get(where)
            if bases is None:
                bases = seen[where] = starts(where)
            imp, name = target
            # one mount per place (`places` stays the table outer() reads for the mounts still to come)
            spots = dict.fromkeys((base if base.strip("/") else "", top) for base, top in bases)
            facts.http_mounts.extend(HttpMount(prefix or "", imp, line, base, router=name, owner=top) for base, top in spots)


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
        if is_pattern(imp):  # import(`lodash/${name}`): which module is not known
            continue
        pkg = _package_name(imp.spec)
        name = _FRAMEWORKS.get(pkg) or _FRAMEWORK_SCOPES.get(pkg.split("/", 1)[0])
        if name is None and pkg.startswith("@sveltejs/adapter-"):  # svelte.config.js of a SvelteKit app
            name = "sveltekit"
        if name:
            found.add(name)
    return found


_NEXT_PAGES_API = re.compile(r"(?:^|/)pages/(api(?:/.*)?)\.[cm]?[jt]sx?$")
_NEXT_APP_ROUTE = re.compile(r"(?:^|/)app/(.*?)/?route\.[cm]?[jt]s$")
_KIT_SERVER_ROUTE = re.compile(r"(?:^|/)routes((?:/[^/]+)*?)/\+server\.[cm]?[jt]s$")
_NUXT_SERVER_ROUTE = re.compile(r"(?:^|/)server/(api|routes)/(.+?)(?:\.(get|post|put|patch|delete))?\.[cm]?[jt]s$")
_RX_EVENT_HANDLER = re.compile(r"\b(?:define(?:Cached)?EventHandler|eventHandler)\s*[<(]")


def _route_address(folders: str) -> str:
    """Address of a file-routing folder path: "/(shop)/todos/[id]" -> "/todos/[id]". Route groups in
    parentheses add nothing, and an optional [[param]] is read as a [param]."""
    segments = [s for s in folders.split("/") if s and not (s.startswith("(") and s.endswith(")"))]
    return "/" + "/".join(re.sub(r"^\[\[(.*)\]\]$", r"[\1]", s) for s in segments)


def _file_routes(facts: FileFacts, source: str):
    """API routes defined by the file path: Next.js (pages/api/x.js, app/x/route.ts), SvelteKit
    (src/routes/x/+server.ts, its exported GET, POST, ...) and Nuxt (server/api/x.get.ts whose
    default export is a defineEventHandler())."""
    m = _NEXT_PAGES_API.search(facts.path)
    if m:
        url = "/" + re.sub(r"(?:^|/)index$", "", m.group(1))
        methods = ["ANY"]
    elif (m := _NEXT_APP_ROUTE.search(facts.path)) or (m := _KIT_SERVER_ROUTE.search(facts.path)):
        url = _route_address(m.group(1))
        methods = [s.name for s in facts.symbols if s.exported and s.name in _KNOWN_METHODS]
        if m.re is _KIT_SERVER_ROUTE and any(s.exported and s.name == "fallback" for s in facts.symbols):
            methods.append("ANY")  # SvelteKit's fallback answers the other methods
    elif (m := _NUXT_SERVER_ROUTE.search(facts.path)) and _RX_EVENT_HANDLER.search(source):
        rest = re.sub(r"(?:^|/)index$", "", m.group(2))
        url = ("/api/" if m.group(1) == "api" else "/") + rest
        methods = [m.group(3).upper() if m.group(3) else "ANY"]
    else:
        return
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
_RX_CLASS = re.compile(  # export default class extends Error {} has no name
    r"^(export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?!extends(?![\w$]))([A-Za-z_$][\w$]*)", re.M)
# What a class extends, read right after its name (class NotFound extends errors.HttpError ->
# errors.HttpError), its type parameters skipped (class Box<T extends { id: string }> extends Base<T>,
# two levels of <> at most). A call or an index (extends mixin(Base), extends parts[0]) is no name.
_RX_EXTENDS = re.compile(
    r"\s*(?:<(?:[^<>{};]|\{[^{}]*\}|<(?:[^<>{};]|\{[^{}]*\})*>)*>\s*)?"
    r"extends\s+([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)(?![\w$])(?!\s*[.(\[])")
_RX_EXPORT_NAMES = re.compile(r"^export\s*\{([^}]*)\}\s*;?[ \t]*\r?$", re.M)
_RX_EXPORT_DEFAULT = re.compile(r"^export\s+default\s+([A-Za-z_$][\w$]*)\s*;?[ \t]*\r?$", re.M)
_RX_MODULE_EXPORTS = re.compile(r"\bmodule\.exports\s*=\s*(\{[^}]*\}|[A-Za-z_$][\w$]*)")
_RX_JSX = re.compile(r"<[A-Za-z][\w.]*(?:\s[^<>]*)?/?>|<>")
_RX_LEADING = re.compile(r"\s*(?:#![^\n]*|(['\"])use [\w ]+\1;?|//[^\n]*|/\*[\s\S]*?\*/)")
_RX_MOUNT_CALL = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\.\s*(use|route|register)\s*\(")
_RX_MOUNT_TARGET = re.compile(
    r"^(?:await\s+)?(?:(require|import)\s*\(\s*(['\"`])([^'\"`\n]+)\2\s*\)|([A-Za-z_$][\w$]*))"
    r"(?:\s*\.\s*([A-Za-z_$][\w$]*))?(\s*\()?"  # routes.todos, todos.routes(), require("./todos")(db)
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


_RX_IMPORT_CALL = re.compile(r"(?<![\w$.])(import|require)\s*\(\s*")
_RX_BINDING_BEFORE = re.compile(r"\b(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*$")
_RX_GLOB_CALL = re.compile(
    r"(?<![\w$.])import\s*\.\s*meta\s*\.\s*(glob|globEager|webpackContext)\s*(?:<[^()]*?>\s*)?\("
    r"|(?<![\w$.])require\s*\.\s*context\s*\("
)
_RX_STRING = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\])*)\1""")
_RX_REGEX_LITERAL = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/[a-z]*")


def _rx_string(item: str) -> str | None:
    """The text of one plain string literal (a template without ${}), else None."""
    m = _RX_STRING.fullmatch(item.strip())
    if m is None or (m.group(1) == "`" and "${" in m.group(2)):
        return None
    return m.group(2)


def _rx_computed_imports(text: str, consts: dict, line_of) -> list:
    """import(`./locales/${lang}.json`) and require("./plugins/" + name) in the fallback, as
    (position, ImportRef): see _TreeAnalyzer.computed_import."""
    out = []
    n = len(text)
    for m in _RX_IMPORT_CALL.finditer(text):
        raw, after = _rx_url(text, m.end(), consts)
        arg = text[m.end():after]
        if not raw or ("+" not in arg and "${" not in arg):
            continue  # require(name), or a plain string found above
        j = after
        while j < n and text[j].isspace():
            j += 1
        if j >= n or text[j] not in "),":
            continue  # more than strings and names joined by +: not followed
        kind = "dynamic" if m.group(1) == "import" else "require"
        if "${}" not in raw:  # every part a known constant: an ordinary import
            bound = _RX_BINDING_BEFORE.search(text, max(0, m.start() - 200), m.start()) if kind == "require" else None
            out.append((m.start(), ImportRef(raw, _rx_names(bound.group(1)) if bound else [], kind, 0,
                                             line_of(m.start()))))
            continue
        spec = _pattern_spec(raw)
        if spec:
            out.append((m.start(), ImportRef(spec, [], kind, 0, line_of(m.start()))))
    return out


def _rx_glob_imports(text: str, line_of) -> list:
    """import.meta.glob(), require.context() and import.meta.webpackContext() in the fallback, as
    (position, ImportRef): see _TreeAnalyzer.glob_import and context_import."""
    out = []
    for m in _RX_GLOB_CALL.finditer(text):
        args = _rx_args(text[m.end():_balanced_end(text, m.end() - 1) - 1])
        if not args:
            continue
        line = line_of(m.start())
        if m.group(1) in ("glob", "globEager"):
            first = args[0]
            items = _rx_args(first[1:-1]) if first.startswith("[") and first.endswith("]") else [first]
            patterns = [_rx_string(item) for item in items]
            if not patterns or any(p is None for p in patterns):
                continue
            patterns = [p.strip() for p in patterns if p.strip()]
            keep = [p for p in patterns if not p.startswith("!")]
            if keep:
                out.append((m.start(), ImportRef(keep[0], keep[1:] + [p for p in patterns if p.startswith("!")],
                                                 "glob", 0, line)))
            continue
        folder = _rx_string(args[0])
        if folder is None:
            continue
        if m.group(1) == "webpackContext":
            options = args[1] if len(args) > 1 else ""
            deep = re.search(r"\brecursive\s*:\s*false\b", options) is None
            found = re.search(r"\bregExp\s*:\s*", options)
            regex = _RX_REGEX_LITERAL.match(options, found.end()) if found else None
            if found and regex is None:
                continue  # a regular expression made at run time
            regex = regex.group() if regex else None
        else:
            deep = len(args) < 2 or args[1] != "false"
            regex = args[2] if len(args) > 2 else None
            if regex is not None and not _RX_REGEX_LITERAL.fullmatch(regex):
                continue
        spec = (folder.strip().rstrip("/") or ".") + ("/**" if deep else "/*")
        out.append((m.start(), ImportRef(spec, [regex] if regex else [], "glob", 0, line)))
    return out


def _rx_mount_target(target_text: str, origins: dict, line: int):
    """_TreeAnalyzer.mount_target in the fallback: a router variable of this file, or (ImportRef, the
    router's name in that module), from the text of the mounted argument."""
    found = _RX_MOUNT_TARGET.match(target_text)
    if not found:
        return None
    prop, called = found.group(5), found.group(6)
    if found.group(3):  # require("./routes"), require("./routes").todos, require("./todos")(db)
        kind = "dynamic" if found.group(1) == "import" else "require"
        imp = ImportRef(found.group(3), [], kind, 0, line)
        return imp, "" if called else prop or "default"
    name = found.group(4)
    if name not in origins or origins[name][0].kind not in ("import", "require"):
        return name
    imp, original = origins[name]
    if prop and not called:  # routes.todos: a router of a module, or a property of a router
        return imp, prop if original in ("default", "*") else ""
    if called and not prop:  # makeRouter(db): what a factory makes
        return imp, ""
    return imp, "" if original == "*" else original  # todos, todos.routes() (Koa)


def _rx_mounts(facts: FileFacts, text: str, consts: dict, route_file: bool, line_of,
               origins: dict | None = None, in_plugin=None) -> tuple[list, list]:
    """app.use("/api", router), app.route("/api", api), app.register(routes, { prefix }) in the fallback,
    and in a server file app.use(router) and app.register(routes) with no prefix.

    Returns the mounts as (prefix or None, target, variable mounted on, line, position, verb), target
    being (the ImportRef of a router from another file, its name there) or the variable of a router
    made in this file (a key for a plugin function written in the register() call), and the scopes of
    those plugin functions (see _rx_fn_scope). `origins`: imported local name -> (ImportRef, its name in
    that module); `in_plugin(variable, position)`: a call on a Fastify plugin's parameter (see _rx_plugins).
    """
    origins = origins or {}
    mounts, scopes = [], []
    for m in _RX_MOUNT_CALL.finditer(text):
        obj, verb = m.group(1), m.group(2)
        plugin = in_plugin is not None and in_plugin(obj, m.start())
        if not (route_file or _ROUTER_LIKE.match(obj) or plugin):
            continue
        scope = None
        if verb == "register":  # register(async (f) => { ... }, { prefix }): a plugin written right here
            start = _RX_SPACE.match(text, m.end()).end()
            scope = _rx_fn_scope(text, start)
        if scope:  # the arguments after the plugin, read from its end: the function can be long
            region = "plugin" + text[scope[2]:_rx_block_end(text, scope[2], 1) - 1]
        else:
            region = text[m.end():_balanced_end(text, m.end() - 1) - 1]
        args = _rx_args(region)
        if verb == "register":
            options = args[1] if len(args) >= 2 else ""
            prefix = re.search(r"\bprefix\s*:\s*", options) if options.startswith("{") else None
            if prefix:
                raw = _rx_url(options, prefix.end(), consts)[0] or ""  # "": a prefix this cannot read
            elif (route_file or plugin) and args and (not options or options.startswith("{")):
                raw = None  # register(routes): where the app or plugin it is registered on answers
            else:
                continue
            target_text = args[0]
        elif verb == "use" and route_file and (len(args) == 1 or (len(args) >= 2 and _RX_MIDDLEWARE.match(args[0]))):
            # no path: app.use(router), api.use(sub.routes()), api.use(cors(), sub), api.use(v1.routes(), v1.allowedMethods())
            raw, target_text = None, args[-1]
        else:
            if len(args) < 2 or (verb == "route" and len(args) != 2) or args[0][:1] not in "'\"`":
                continue
            raw = _rx_url(args[0], 0, consts)[0]
            target_text = args[-1]
        if raw is not None and (not raw or normalize_http_path(raw) is None):
            continue
        line = line_of(m.start())
        if scope:
            key = f"\x00{start}"
            scopes.append((*scope, key))
            mounts.append((raw, key, obj, line, m.start(), verb))
            continue
        target = _rx_mount_target(target_text, origins, line)
        if target:
            mounts.append((raw, target, obj, line, m.start(), verb))
    return mounts, scopes


_RX_SPACE = re.compile(r"\s*")
# A first argument of use() that cannot be its path (see _middleware_node): cors(), v1.routes(),
# new X(), require("./x")(), or a function written right there.
_RX_MIDDLEWARE = re.compile(
    r"(?:new\s+)?[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*\s*(?:<[^()]*?>\s*)?\("
    r"|(?:async\s+)?(?:function\b|\([^()]*\)\s*(?::[^=;{}()]*)?=>|[A-Za-z_$][\w$]*\s*=>)"
)
_RX_FN_START = re.compile(r"(?:async\s+)?(?:function\b\s*\*?\s*(?:[A-Za-z_$][\w$]*)?\s*(\()|(\()|([A-Za-z_$][\w$]*)\s*=>)")
_RX_ARROW = re.compile(r"\s*(?::[^=;{}()]*)?=>")  # after (params), with a TypeScript return type
_RX_BODY = re.compile(r"\s*(?::[^{;=]*)?\{")
_RX_FIRST_PARAM = re.compile(r"\s*([A-Za-z_$][\w$]*)")
# Runs of plain text, whole strings (as _balanced_end reads them: a backslash takes the next
# character, an unclosed string runs to the end) and single brackets.
_RX_BRACKET_SCAN = re.compile(
    r"""[^()\[\]{}'"`]+|'(?:\\[\s\S]|[^'\\])*'?|"(?:\\[\s\S]|[^"\\])*"?|`(?:\\[\s\S]|[^`\\])*`?|([(\[{])|([)\]}])"""
)


def _rx_block_end(text: str, i: int, depth: int = 0) -> int:
    """_balanced_end(text, i) with no length limit, a run of text at a time: a function body can be long.
    With depth 1, i is inside a bracket already: the index just after the one that closes it."""
    for m in _RX_BRACKET_SCAN.finditer(text, i):
        if m.group(1) is not None:
            depth += 1
        elif m.group(2) is not None:
            depth -= 1
            if depth == 0:
                return m.end()
    return len(text)


def _rx_fn_scope(text: str, start: int):
    """(first parameter, body start, body end) of the function written at text[start:], or None.

    async function routes(fastify, opts) { ... }, async (f) => { ... }, app => { ... }
    """
    m = _RX_FN_START.match(text, start)
    if not m:
        return None
    if m.group(3) is not None:  # app => { ... }
        param, after = m.group(3), m.end()
    else:
        opening = m.start(1) if m.group(1) is not None else m.start(2)
        close = _rx_block_end(text, opening)
        first = _RX_FIRST_PARAM.match(text, opening + 1, close)
        if not first:
            return None
        param, after = first.group(1), close
        if m.group(2) is not None:  # (f) => { ... }: an arrow, not (require("./x"))
            arrow = _RX_ARROW.match(text, after)
            if not arrow:
                return None
            after = arrow.end()
    body = _RX_BODY.match(text, after)
    if not body:
        return None
    return param, body.end() - 1, _rx_block_end(text, body.end() - 1)


_RX_FN_DEF = re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*")


def _rx_plugin_scopes(text: str, names) -> list:
    """Scopes (first parameter, body start, body end, name) of the plugin functions named in
    register() calls: async function routes(fastify) { ... }, const routes = async (app) => { ... }.
    The first function of each name counts; one pass over the text."""
    wanted, scopes = set(names), []
    for m in _RX_FN_DEF.finditer(text):
        name = m.group(1) or m.group(2)
        if name in wanted:
            scope = _rx_fn_scope(text, m.start() if m.group(1) else m.end())
            if scope:
                wanted.discard(name)
                scopes.append((*scope, name))
    return scopes


_RX_ROUTER_DECL = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(new\s+)?([A-Za-z_$][\w$]*)\s*(?:<[^()]*?>\s*)?(\()?"
)
_RX_BASE_PATH = re.compile(r"\s*\.\s*basePath\s*\(")


def _rx_router_prefixes(facts: FileFacts, text: str, consts: dict, line_of) -> tuple[dict, list]:
    """Own prefixes of the routers made in this file, in the fallback (Koa new Router({ prefix: "/api" }),
    Hono new Hono().basePath("/api")), and Hono routers made under another one (const v1 =
    app.basePath("/v1")), as mounts of the new variable."""
    own: dict[str, str] = {}
    under: list = []
    koa = _bound_names(facts.imports, _KOA_ROUTERS)
    hono = "hono" in facts.frameworks
    if not koa and not hono:
        return own, under

    def base_paths(pos: int) -> str:
        head = ""
        while hono:
            call = _RX_BASE_PATH.match(text, pos)
            if not call:
                break
            raw = _rx_url(text, call.end(), consts)[0]
            if raw:
                head = _join_raw(head, raw)
            pos = _balanced_end(text, call.end() - 1)
        return head

    for m in _RX_ROUTER_DECL.finditer(text):
        name, made, callee = m.group(1), m.group(2), m.group(3)
        if m.group(4) and (made or callee in koa):  # new Router({ prefix }), Router({ prefix }), new Hono()
            close = _balanced_end(text, m.end() - 1)
            prefix = ""
            if callee in koa:
                region = text[m.end():close - 1]
                found = re.search(r"\bprefix\s*:\s*", region)
                prefix = (_rx_url(region, found.end(), consts)[0] or "") if found else ""
            prefix = _join_raw(prefix, base_paths(close))
            if prefix:
                own[name] = prefix
        elif not m.group(4) and not made and callee != name:  # const v1 = app.basePath("/v1")
            head = base_paths(m.end())
            if head:
                under.append((head, name, (callee, "", False), line_of(m.start())))
    return own, under


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


_RX_EXPORT_DECL = re.compile(
    r"^export\s+(default\s+)?(?:declare\s+)?(?:async\s+)?(?:function\s*\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)", re.M)
_RX_EXPORT_WRITTEN = re.compile(r"^export\s+default\s+(?![A-Za-z_$][\w$]*\s*;?[ \t]*\r?$)", re.M)
_RX_CJS_EXPORT = re.compile(
    r"(?<![\w$.])(?:module\s*\.\s*exports(?:\s*\.\s*([A-Za-z_$][\w$]*))?|exports\s*\.\s*([A-Za-z_$][\w$]*))\s*=(?![=>])\s*")
_RX_IDENT_END = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:;|\r?$|(?=\}))", re.M)
_RX_REQUIRE_VALUE = re.compile(r"require\s*\(\s*(['\"`])([^'\"`\n]+)\1\s*\)\s*(?:;|\r?$|(?=[,}]))", re.M)
_RX_WRAPPED = re.compile(r"[A-Za-z_$][\w$.]*\s*\(\s*")  # fp(async (fastify) => ...): a plugin in a wrapper


def _rx_origins(clause: str, default: str = "default") -> list[tuple[str, str]]:
    """(local name, name in the module) of an import clause or a require() binding: "api, { get as load }"
    -> [("api", "default"), ("load", "get")]; "* as ns" -> [("ns", "*")]."""
    clause = re.sub(r"\btype\s+", "", clause)
    out = []
    m = re.search(r"\{([^}]*)\}", clause)
    head = clause[:m.start()] if m else clause
    for part in head.split(","):
        part = part.strip()
        ns = re.match(r"\*\s*as\s+([\w$]+)$", part)
        if ns:
            out.append((ns.group(1), "*"))
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", part):
            out.append((part, default))
    for item in (m.group(1).split(",") if m else ()):
        pair = re.fullmatch(r"\s*([\w$]+)\s*(?:(?:as|:)\s*([\w$]+))?\s*", item)
        if pair:
            out.append((pair.group(2) or pair.group(1), pair.group(1)))
    return out


def _rx_exports(text: str) -> tuple[dict, dict, list]:
    """The fallback's export table: (exported name -> local name, local name of a value written in
    the export -> its position, [(exported name, spec of the module it passes on)]). See
    _TreeAnalyzer.export_value for what a written value stands under."""
    raw: dict[str, str] = {}
    written: dict[str, int] = {}
    passed: list[tuple[str, str]] = []
    for m in _RX_EXPORT_DECL.finditer(text):
        raw.setdefault("default" if m.group(1) else m.group(2), m.group(2))
    for m in _RX_EXPORT_DEFAULT.finditer(text):
        raw.setdefault("default", m.group(1))
    for m in _RX_EXPORT_WRITTEN.finditer(text):
        if not re.match(r"(?:async\s+)?(?:function|class)\s*\*?\s*[A-Za-z_$]", text[m.end():m.end() + 100]):
            raw.setdefault("default", "default")
            written.setdefault("default", m.end())
    for m in _RX_EXPORT_NAMES.finditer(text):
        for item in m.group(1).split(","):
            pair = re.fullmatch(r"\s*([\w$]+)\s*(?:as\s+([\w$]+))?\s*", item)
            if pair:
                raw.setdefault(pair.group(2) or pair.group(1), pair.group(1))
    for m in _RX_CJS_EXPORT.finditer(text):
        name, pos = m.group(1) or m.group(2) or "default", m.end()
        required = _RX_REQUIRE_VALUE.match(text, pos)
        if required:  # module.exports = require("./routes")
            passed.append((name, required.group(2)))
            if name == "default":
                passed.append(("*", required.group(2)))
            continue
        ident = _RX_IDENT_END.match(text, pos)
        if ident:
            raw.setdefault(name, ident.group(1))
            continue
        if name == "default" and text.startswith("{", pos):  # module.exports = { todos, users: require("./users") }
            for item in _rx_args(text[pos + 1:_balanced_end(text, pos) - 1]):
                pair = re.fullmatch(r"([\w$]+)\s*(?::\s*([\s\S]+))?", item.strip())
                if not pair:
                    continue
                value = (pair.group(2) or pair.group(1)).strip()
                required = _RX_REQUIRE_VALUE.fullmatch(value)
                if required:
                    passed.append((pair.group(1), required.group(2)))
                elif re.fullmatch(r"[A-Za-z_$][\w$]*", value):
                    raw.setdefault(pair.group(1), value)
            continue
        raw.setdefault(name, name)
        written.setdefault(name, pos)
    return raw, written, passed


def _rx_export_table(facts: FileFacts, raw: dict, required: list, passed: list, origins: dict, plugins: list):
    """FileFacts.exports in the fallback (see _TreeAnalyzer.exports; the fallback finds no request helpers)."""
    known = ({r.router for r in facts.http_routes} | {m.owner for m in facts.http_mounts}
             | {key for *_, key in plugins}) - {""}
    for name, local in raw.items():
        if local in origins:  # import todos from "./todos"; export { todos }
            facts.exports.append(ExportName(name, source=origins[local][0], original=origins[local][1]))
        elif local in known:
            facts.exports.append(ExportName(name, local=local))
    requires = {}
    for imp in facts.imports:
        if imp.kind == "require":
            requires.setdefault(imp.spec, imp)
    for name, spec in required:
        imp = requires.get(spec) or ImportRef(spec, [], "require")
        facts.exports.append(ExportName(name, source=imp, original="*" if name == "*" else "default"))
    facts.exports.extend(ExportName(name, source=imp, original=original) for name, imp, original in passed)


_RX_HANDLER = re.compile(r"(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$.]*\s*(?:=>|$))")
_RX_HANDLER_WRITTEN = re.compile(r"(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)")  # no bare name
_RX_AFTER_REQUIRE = re.compile(r"(?:\s*\.\s*([A-Za-z_$][\w$]*))?(\s*\()?")
_RX_OPEN = re.compile(r"\s*\(")


def _rx_route_call(text: str, pos: int, named: bool = True) -> bool:
    """Do the arguments after a route method at text[pos:] (fastify.get) look like a route: a path
    written first and a handler last ("/todos", async () => []), not map.get("/key", 1)? With `named`
    False the handler must be written right there: a name (cache.get("/key", load)) may be anything."""
    call = _RX_OPEN.match(text, pos)
    if not call:
        return False
    args = _rx_args(text[call.end():_balanced_end(text, call.end() - 1) - 1])
    handler = _RX_HANDLER if named else _RX_HANDLER_WRITTEN
    return len(args) >= 2 and args[0][:1] in "'\"`" and bool(handler.match(args[-1]))


def _rx_plugins(text: str, raw: dict, written: dict, route_file: bool) -> tuple[list, list, set]:
    """find_plugins() in the fallback: (scopes of the Fastify plugin functions this file exports, as
    _in_plugins reads them; (parameter, start, end) of those whose route calls count only because
    of it, with the plugins registered on them; the plugin keys that count only in a Fastify project)."""
    scopes, params, maybe = [], [], set()
    found = []
    named = set()
    for local in dict.fromkeys(raw.values()):
        pos = written.get(local)
        if pos is None:
            named.add(local)
            continue
        scope = _rx_fn_scope(text, pos)
        if scope is None:
            wrapped = _RX_WRAPPED.match(text, pos)  # module.exports = fp(async (fastify) => { ... })
            scope = _rx_fn_scope(text, wrapped.end()) if wrapped else None
        if scope:
            found.append((*scope, local))
    found += _rx_plugin_scopes(text, named) if named else []
    for param, start, end, key in found:
        if _client_like(param):
            continue  # makeApi(http) { http.get("/todos", options) }: requests, as they always were
        instance = _instance_like(param)
        uses = re.compile(rf"(?<![\w$.]){re.escape(param)}\s*\.\s*(?:(get|post|put|patch|delete|all)|route\s*\(\s*\{{"
                          r"|register\s*\()")
        if not any(not m.group(1) or _rx_route_call(text, m.end(), instance) for m in uses.finditer(text, start, end)):
            continue
        scopes.append((param, start, end, key))
        if not route_file:
            params.append((param, start, end))
            if not _ROUTER_LIKE.match(param):
                maybe.add(key)
    for _ in range(5 if params else 0):  # plugins written right in a register() on a plugin's parameter
        added = False
        for param, start, end in list(params):
            for m in re.finditer(rf"(?<![\w$.]){re.escape(param)}\s*\.\s*register\s*\(\s*", text[start:end]):
                scope = _rx_fn_scope(text, start + m.end())
                if scope and (scope[0], scope[1], scope[2]) not in params:
                    params.append((scope[0], scope[1], scope[2]))
                    added = True
        if not added:
            break
    return scopes, params, maybe


def _regex_analyze(facts: FileFacts, source: str, ext: str):
    text = _blank_comments(source)
    newlines = [m.start() for m in re.finditer("\n", text)]

    def line_of(pos: int) -> int:
        return bisect.bisect_right(newlines, pos) + 1

    _rx_symbols(facts, source, text, ext, line_of)
    facts.module_doc = _rx_module_doc(source, {s.line for s in facts.symbols}, line_of)
    found = []  # (pos, ImportRef)
    origins: dict[str, tuple[ImportRef, str]] = {}  # imported local name -> (ImportRef, its name in that module)
    passed: list = []  # (exported name, ImportRef, its name in that module): export { api } from "./api"
    for m in _RX_IMPORT.finditer(text):
        imp = ImportRef(m.group(3), _rx_names(m.group(1)), "import", 0, line_of(m.start()))
        found.append((m.start(), imp))
        for local, original in _rx_origins(m.group(1)):
            origins.setdefault(local, (imp, original))
    for m in _RX_SIDE_EFFECT.finditer(text):
        found.append((m.start(), ImportRef(m.group(2), [], "side-effect", 0, line_of(m.start()))))
    for m in _RX_REEXPORT.finditer(text):
        clause = m.group(1)
        names = ["*"] if clause.strip() == "*" else _rx_names(clause)
        imp = ImportRef(m.group(3), names, "reexport", 0, line_of(m.start()))
        found.append((m.start(), imp))
        if clause.strip() == "*":
            passed.append(("*", imp, "*"))
        else:
            passed += [(local, imp, original) for local, original in _rx_origins(clause, "")]
    for m in _RX_REQUIRE.finditer(text):
        if "${" in m.group(3):
            continue  # require(`./x/${name}`): a pattern, below
        names = _rx_names(m.group(1)) if m.group(1) else []
        imp = ImportRef(m.group(3), names, "require", 0, line_of(m.start()))
        found.append((m.start(), imp))
        after = _RX_AFTER_REQUIRE.match(text, m.end())  # require("./x").todos, require("express").Router()
        if after.group(2) or (after.group(1) and not re.fullmatch(r"[A-Za-z_$][\w$]*", m.group(1) or "")):
            continue  # what a call returns, or a property of the module destructured: not the module's names
        for local, original in _rx_origins(m.group(1) or "", after.group(1) or "default"):
            origins.setdefault(local, (imp, original))
    for m in _RX_DYNAMIC.finditer(text):
        if "${" not in m.group(2):
            found.append((m.start(), ImportRef(m.group(2), [], "dynamic", 0, line_of(m.start()))))

    consts: dict[str, str] = {}
    for m in _RX_CONST.finditer(text):
        value, _ = _rx_url(text, m.end(), consts)
        if value is not None:
            consts[m.group(1)] = value

    found += _rx_computed_imports(text, consts, line_of)
    if "import.meta" in text or "require.context" in text:
        found += _rx_glob_imports(text, line_of)
    found.sort(key=lambda item: item[0])
    facts.imports = [imp for _, imp in found]
    facts.frameworks = _frameworks(facts.imports)
    route_file = bool(facts.frameworks & _ROUTE_FRAMEWORKS)
    raw_exports, written, required = _rx_exports(text)
    plugins, plugin_params, maybe = _rx_plugins(text, raw_exports, written, route_file) if raw_exports else ([], [], set())

    def in_plugin(name: str, pos: int) -> bool:
        return any(param == name and start <= pos < end for param, start, end in plugin_params)

    clients = set(_CLIENTS)
    if "nuxt" in facts.frameworks or facts.path.lower().endswith(".vue"):
        clients |= _NUXT_CLIENTS  # useFetch("/api/todos") in a Nuxt page
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

    owners = []  # (HttpRoute, variable the route was added to, position)
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
        if more_args and (route_file or _ROUTER_LIKE.match(obj) or (
                in_plugin(obj, m.start()) and _rx_route_call(text, m.end() - 1))):  # a plugin's fastify.get("/x", h)
            path = normalize_http_path(raw) if raw else None
            if path is not None:
                method = "ANY" if prop == "all" else prop.upper()
                route = HttpRoute(method=method, path=path, handler="", line=line_of(m.start()))
                facts.http_routes.append(route)
                owners.append((route, obj, m.start()))
                continue
        if prop != "all" and _CLIENT_LIKE.search(obj) and not _NOT_CLIENT.search(obj):
            add_call(prop.upper(), raw, m.start())
    mounts, scopes = _rx_mounts(facts, text, consts, route_file, line_of, origins, in_plugin if plugin_params else None)
    own, under = _rx_router_prefixes(facts, text, consts, line_of)
    if mounts or own or under or owners:
        routes_at = [(obj, "", False) for _, obj, _ in owners]
        mounts_at = [(m[2], "", False) for m in mounts]
        named = [m[1] for m in mounts if m[5] == "register" and isinstance(m[1], str) and m[1][:1] != "\x00"]
        scopes += (_rx_plugin_scopes(text, named) if named else []) + plugins
        if scopes:  # fastify.get() in async function routes(fastify): a route of the plugin `routes`
            routes_at = _in_plugins(routes_at, [pos for _, _, pos in owners], scopes)
            mounts_at = _in_plugins(mounts_at, [m[4] for m in mounts], scopes)
        if not (own or under or any(isinstance(m[1], str) for m in mounts)):
            for (route, _, _), (name, _, _) in zip(owners, routes_at):
                route.router = name  # no router of this file is mounted: no route moves
            owners = routes_at = []
        _place_routers(facts, [(route, at) for (route, _, _), at in zip(owners, routes_at)],
                       under + [(m[0], m[1], at, m[3]) for m, at in zip(mounts, mounts_at)], own)
    _rx_export_table(facts, raw_exports, required, passed, origins, plugins)
    if maybe:  # a Fastify plugin's routes in a file that imports no server framework
        facts.plugin_routes = [r for r in facts.http_routes if r.router in maybe]
        facts.http_routes = [r for r in facts.http_routes if r.router not in maybe]
        facts.plugin_mounts = [m for m in facts.http_mounts if m.owner in maybe]
        facts.http_mounts = [m for m in facts.http_mounts if m.owner not in maybe]

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
    bases: dict[int, tuple[str, ...]] = {}  # a class's start -> what it extends, the last part of the name
    for m in _RX_CLASS.finditer(text):
        found.append((m.start(), m.end(), m.group(2), "class", None, bool(m.group(1))))
        extends = _RX_EXTENDS.match(text, m.end())
        if extends:
            bases[m.start()] = (re.split(r"\s*\.\s*", extends.group(1))[-1],)
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
            bases=bases.get(start, ()),
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
    importer_dir = parent_dir(importer)
    if is_pattern(imp):  # import.meta.glob("./pages/*.vue"), import(`./locales/${lang}.json`)
        return _resolve_pattern(imp, importer_dir, index)
    spec = (imp.spec or "").strip().split("?", 1)[0]
    if not spec or re.match(r"^[a-zA-Z][\w+.-]*:", spec):  # node:fs, https://..., virtual:x
        return []
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
    if not covered:
        for candidate in _alias_roots(spec, importer_dir, index):
            hit = _resolve_path(candidate, index)
            if hit:
                return hit
    return []


def _alias_roots(spec: str, importer_dir: str, index: ProjectIndex) -> list[str]:
    """Where an alias that no tsconfig paths cover usually points, in the package of the importer:
    @/ and ~/ to src/ (Vue CLI, Vite templates), and in a Nuxt app (nuxt.config.* beside package.json)
    to the app's own folder (app/ in Nuxt 4, else the package itself); ~~/ and @@/ to the Nuxt
    package; SvelteKit's $lib to src/lib."""
    root = _package_root(importer_dir, index)
    nuxt = _nuxt_package(root, index)
    if spec[:2] in ("@/", "~/"):
        rest = spec[2:]
        src = join_rel(root, "src")
        out = [join_rel(src, rest)] if src is not None and index.is_dir(src) else []
        if nuxt:
            app = join_rel(root, "app")
            out += ([join_rel(app, rest)] if app is not None and index.is_dir(app) else []) + [join_rel(root, rest)]
        return [c for c in out if c is not None]
    if nuxt and spec[:3] in ("~~/", "@@/"):
        candidate = join_rel(root, spec[3:])
        return [candidate] if candidate is not None else []
    if spec == "$lib" or spec.startswith("$lib/"):
        candidate = join_rel(root, "src/lib", spec[5:])
        return [candidate] if candidate is not None else []
    return []


def _nuxt_package(folder: str, index: ProjectIndex) -> bool:
    """A Nuxt app: nuxt.config.ts (or .js, .mjs, ...) in the package folder."""
    return any(_join(folder, f"nuxt.config{ext}") in index.files for ext in (".ts", ".js", ".mjs", ".mts", ".cjs"))


def _alias_folder(spec: str, importer_dir: str, index: ProjectIndex) -> str | None:
    """The project folder an aliased folder name points to ("@/views" -> "src/views"), as
    _resolve_alias finds files: tsconfig paths and baseUrl, then the usual aliases."""
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
                folder = join_rel(paths_dir, target.replace("*", star, 1)) if isinstance(target, str) else None
                if folder is not None and index.is_dir(folder):
                    return folder
        if base is not None:
            folder = join_rel(base, spec)
            if folder is not None and index.is_dir(folder):
                return folder
    if not covered:
        return next((c for c in _alias_roots(spec, importer_dir, index) if index.is_dir(c)), None)
    return None


# ---------------------------------------------------------------- patterns: files a glob or a computed name can be

_GLOB_CHARS = "*?[{"
_RESOLVE_ALTERNATIVES = "|".join(re.escape(ext[1:]) for ext in _RESOLVE_EXTENSIONS)
_PATTERN_STARTS = ("./", "../", "/", "@/", "~/", "~~/", "@@/", "$lib/")
_RX_HOLES = re.compile(r"(?:\$\{\})+")
_RX_FILE_EXT = re.compile(r"\.[A-Za-z0-9]+$")


def _pattern_spec(raw: str) -> str | None:
    """A module path built at run time as a pattern: "./locales/${}.json" -> "./locales/*.json".

    None unless the path starts with a written folder (./, ../, /, or an alias such as @/) and has a
    written part besides that start: "./${}" could be any file, and "lodash/${}" is a package's."""
    spec = _RX_HOLES.sub("*", raw.strip()).split("?", 1)[0].split("#", 1)[0]
    if spec in (".", "..") or not spec.startswith(_PATTERN_STARTS):
        return None
    rest = re.sub(r"^(?:\.\.?/)+|^(?:@/|~~?/|@@/|\$lib/|/)", "", spec)
    return spec if rest.replace("*", "").strip("/") else None


def _sorted_files(index: ProjectIndex) -> list[str]:
    """The files of the index in order: the files under a folder are next to each other."""
    cache = _cache(index)
    files = cache.get("sorted")
    if files is None:
        files = cache["sorted"] = sorted(index.files)
    return files


def _resolve_pattern(imp: ImportRef, importer_dir: str, index: ProjectIndex) -> list[str]:
    """Project files a pattern import points to, sorted (see ImportRef). A "glob" import reads its
    patterns as Vite does (`*` within a folder, `**` through folders, `?`, [abc], {a,b}, and "!" to
    leave files out); a name built at run time reads `*` as part of one name and finds the files a
    bundler would load for it (with its extensions and index files)."""
    cache = _cache(index)
    key = ("pattern", importer_dir, imp.kind, imp.spec, tuple(imp.names))
    if key in cache:
        return cache[key]
    glob = imp.kind == "glob"
    names = [n.strip() for n in imp.names if n and n.strip()] if glob else []
    filters = []
    for name in names:  # require.context's regular expression: "/\\.js$/i"
        if name.startswith("/"):
            found = _js_regex(name)
            if found is None:  # one this cannot read: better no link than a wrong one
                cache[key] = []
                return []
            filters.append(found)
    hits: set[str] = set()
    for pattern in [imp.spec] + [n for n in names if not n.startswith(("!", "/"))]:
        hits.update(_pattern_files(pattern, importer_dir, index, glob, filters))
    for pattern in (n[1:] for n in names if n.startswith("!")):
        hits.difference_update(_pattern_files(pattern, importer_dir, index, True, []))
    cache[key] = result = sorted(hits, key=lambda p: (p.lower(), p))
    return result


def _js_regex(literal: str):
    """A JavaScript regular expression literal ("/\\.js$/i") as a Python one, or None (also for one
    that could cost too much: see _regex_is_cheap)."""
    end = literal.rfind("/")
    if end <= 0:
        return None
    body, flags = literal[1:end], literal[end + 1:]
    body = re.sub(r"\(\?<([A-Za-z_$][\w$]*)>", r"(?P<\1>", body)  # named groups
    flags = (re.I if "i" in flags else 0) | (re.S if "s" in flags else 0) | (re.M if "m" in flags else 0)
    try:
        found = re.compile(body, flags)
    except (re.error, OverflowError, RecursionError):
        return None
    return found if _regex_is_cheap(body, flags) else None


_REGEX_MAX = 300        # characters of a require.context regular expression that is used at all
_REGEX_CHOICES = 16     # alternatives and optional parts one such pattern may try at each place
_FILTER_PATH_MAX = 300  # a longer "./path" is never tested against such a pattern
_REPEATS = tuple(op for op in (getattr(_sre_constants, name, None) for name in
                               ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")) if op is not None)
_PLAIN_OPS = tuple(op for op in (getattr(_sre_constants, name, None) for name in
                                 ("LITERAL", "NOT_LITERAL", "ANY", "IN", "AT", "CATEGORY")) if op is not None)
_ATOMIC = getattr(_sre_constants, "ATOMIC_GROUP", None)  # Python 3.11+


def _regex_is_cheap(body: str, flags: int) -> bool:
    """True when a regular expression taken from a project file costs little on any path.

    Python's re backtracks, and it holds the GIL while it does: /(a|aa)+$/ or /a*a*a*a*a*b/ from a
    hostile repo would stop the whole server for minutes. Allowed: one open-ended or counted repeat
    (two when the pattern starts with ^, so it is tried at the start only), nothing repeated inside a
    repeat, no alternatives inside a repeat, no backreferences, and few choices in all. Every pattern
    people write for require.context fits (/\\.vue$/, /\\.(png|jpe?g)$/, /^\\.\\/(?!.*\\.spec\\.).*\\.js$/)."""
    if len(body) > _REGEX_MAX:
        return False
    try:
        items = list(_sre_parser.parse(body, flags))
    except Exception:  # whatever the parser raises: not a pattern to use
        return False
    wide = 0

    def choices(items, repeated: bool):  # the ways the items can be tried at one place; None: refused
        nonlocal wide
        total = 1
        for op, av in items:
            if op in _PLAIN_OPS:
                continue
            if op in _REPEATS:
                low, high, sub = av
                if repeated:
                    return None
                if high > 1:
                    wide += 1
                    inner = choices(sub, True)
                else:  # x? and (...)?
                    inner = choices(sub, False)
                    inner = None if inner is None else inner + 1
            elif op is _sre_constants.BRANCH:
                if repeated:
                    return None
                inner = 0
                for alternative in av[1]:
                    one = choices(alternative, False)
                    if one is None:
                        return None
                    inner += one
            elif op is _sre_constants.SUBPATTERN:
                inner = choices(av[-1], repeated)
            elif _ATOMIC is not None and op is _ATOMIC:
                inner = choices(av, repeated)
            elif op in (_sre_constants.ASSERT, _sre_constants.ASSERT_NOT):
                inner = choices(av[1], repeated)
            else:  # backreferences, conditionals and anything else
                return None
            if inner is None:
                return None
            total *= inner
        return total

    total = choices(items, False)
    anchored = bool(items) and items[0] == (_sre_constants.AT, _sre_constants.AT_BEGINNING)
    return total is not None and total <= _REGEX_CHOICES and wide <= (2 if anchored else 1)


def _split_glob(pattern: str) -> list[str]:
    """Folder parts of a glob, not splitting inside {a,b/c}."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(pattern):
        if c == "{":
            depth += 1
        elif c == "}" and depth:
            depth -= 1
        elif c == "/" and not depth:
            parts.append(pattern[start:i])
            start = i + 1
    parts.append(pattern[start:])
    return parts


# Globs and run-time names become regular expressions that never backtrack their way into minutes
# (see model.join_stars). A part with a {a,b} group has pieces of different lengths, so it may hold
# one star at most. Several "**" parts are handled the same way one level up (see _glob_regex).

def _glob_part(part: str) -> str | None:
    """Regular expression for one folder part of a glob; None for a {a,b} group next to two stars."""
    out, i, n = [""], 0, len(part)  # the fixed pieces between the stars
    group_stars = -1  # stars inside {a,b} groups (-1: no group)
    while i < n:
        c = part[i]
        if c == "*":
            while i + 1 < n and part[i + 1] == "*":
                i += 1
            out.append("")
        elif c == "?":
            out[-1] += "[^/]"
        elif c == "[" and part.find("]", i + 2) != -1:
            j = part.find("]", i + 2)
            body = part[i + 1:j].replace("\\", "\\\\")  # never "/", as in fast-glob (and a fixed piece for join_stars)
            out[-1] += "(?!/)[" + ("^" + body[1:] if body[:1] in "!^" and len(body) > 1 else body) + "]"
            i = j
        elif c == "{":
            depth, j = 0, i
            while j < n:
                depth += part[j] == "{"
                depth -= part[j] == "}"
                if depth == 0:
                    break
                j += 1
            if j >= n:
                out[-1] += re.escape(c)
            else:
                options, level, start = [], 0, i + 1
                for k in range(i + 1, j):
                    level += part[k] == "{"
                    level -= part[k] == "}"
                    if part[k] == "," and level == 0:
                        options.append(part[start:k])
                        start = k + 1
                options.append(part[start:j])
                alternatives = []
                for option in options:
                    subs = [_glob_part(p) for p in option.split("/")]
                    if None in subs:
                        return None
                    alternatives.append("/".join(subs))
                out[-1] += "(?:" + "|".join(alternatives) + ")"
                group_stars = max(group_stars, 0) + sum(option.count("*") for option in options)
                i = j
        else:
            out[-1] += re.escape(c)
        i += 1
    lead = "(?!\\.)" if part[:1] in _GLOB_CHARS else ""  # like fast-glob: a wildcard skips dotfiles
    if group_stars < 0:
        return lead + join_stars(out)
    if len(out) - 1 + group_stars > 1:
        return None  # the pieces have no fixed length: two stars could cost too much
    return lead + "[^/]*".join(out)


_DSTAR = "(?:(?!\\.)[^/]+/)*"  # "**" before more parts: any number of folders, none starting with "."


def _glob_regex(parts: list[str]) -> str | None:
    """A glob's parts as one regular expression, or None. With several "**" parts, the parts after each
    "**" but the last are matched at their first place (the folders between can be anything "**" takes).
    That is only right when none of those parts can match a name starting with ".", which "**" never
    takes, so such a glob gives None."""
    runs: list[list[str]] = [[]]  # the parts between "**" parts
    dotted = [False]
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**" and not last:
            runs.append([])
            dotted.append(False)
            continue
        found = "(?:(?!\\.)[^/]+/)*(?!\\.)[^/]+" if part == "**" else _glob_part(part)
        if found is None:
            return None
        runs[-1].append(found + ("" if last else "/"))
        dotted[-1] = dotted[-1] or part.startswith(".")
    out = "".join(runs[0])
    for k in range(1, len(runs)):
        if k == len(runs) - 1:
            out += _DSTAR + "".join(runs[k])
        elif dotted[k]:
            return None
        else:
            out += atomic(_DSTAR[:-1] + "*?" + "".join(runs[k]))
    return out


def _pattern_regex(parts: list[str], glob: bool):
    """Regular expression for the path, below the pattern's folder, of the files the parts match."""
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None  # "./x/../*.js" or "a//b": not a pattern this follows
    out = []
    if glob:
        found = _glob_regex(parts)
        if found is None:
            return None
        out.append(found)
        parts = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        # a name built at run time: `*` is part of one name, which is never empty
        piece = "[^/]+" if part == "*" else join_stars([re.escape(p) for p in part.split("*")])
        if not last:
            out.append(piece + "/")
            continue
        ext = _RX_FILE_EXT.search(part) if not part.endswith("*") else None
        if ext is None:  # require("./plugins/" + name): plugins/x.js, plugins/x/index.js
            out.append(f"{piece}(?:\\.(?:{_RESOLVE_ALTERNATIVES})|/index\\.(?:{_RESOLVE_ALTERNATIVES}))")
        elif ext.group().lower() in _JS_TO_TS:  # import(`./x/${name}.js`) of TypeScript files
            stem = join_stars([re.escape(p) for p in part[:ext.start()].split("*")])
            out.append(stem + "\\.(?:" + "|".join(e[1:] for e in (ext.group().lower(), *_JS_TO_TS[ext.group().lower()])) + ")")
        else:
            out.append(piece)
    try:
        return re.compile("".join(out))
    except (re.error, OverflowError, RecursionError):
        return None


def _pattern_files(pattern: str, importer_dir: str, index: ProjectIndex, glob: bool, filters: list) -> list[str]:
    """Project files one pattern matches. Its folder is the written part before the first wildcard:
    from the importer (./, ../), from the package root (/) or through an alias (@/, $lib)."""
    wild = _GLOB_CHARS if glob else "*"
    parts = _split_glob(pattern.strip()) if glob else pattern.strip().split("/")
    k = 0
    while k < len(parts) and not any(c in parts[k] for c in wild):
        k += 1
    k = min(k, len(parts) - 1)  # a glob with no wildcard names one file
    head, rest = "/".join(parts[:k]), parts[k:]
    if head in (".", "..") or head.startswith(("./", "../")):
        base = join_rel(importer_dir, head)
    elif head.startswith("/") or (not head and pattern.startswith("/")):
        base = join_rel(_package_root(importer_dir, index), head)
    elif head and head[0] not in _GLOB_CHARS:
        base = _alias_folder(head, importer_dir, index)
    else:
        base = None  # "*.js": Vite takes only ./, / and aliases
    regex = _pattern_regex(rest, glob) if base is not None else None
    if regex is None:
        return []
    files = _sorted_files(index)
    prefix = f"{base}/" if base else ""
    out = []
    for i in range(bisect.bisect_left(files, prefix) if prefix else 0, len(files)):
        rel = files[i]
        if not rel.startswith(prefix):
            break
        tail = rel[len(prefix):]
        if (regex.fullmatch(tail) and (not filters or len(tail) + 2 <= _FILTER_PATH_MAX)
                and all(f.search("./" + tail) for f in filters) and (glob or not tail.endswith(".d.ts"))):
            out.append(rel)
    return out


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
    try:  # the screen a Nuxt, SvelteKit or Next.js app starts from (app.vue, src/routes/+layout.svelte)
        for rel, reason in _conventions(index)[0].items():
            out.setdefault(rel, reason)
    except Exception:
        pass
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


# ---------------------------------------------------------------- files a framework loads by itself

APP_ENTRY = "{name} 처음 띄우는 화면"
_APP_NAMES = {"nuxt": "Nuxt가", "sveltekit": "SvelteKit이", "next": "Next.js가"}  # with their particle
_APP_DEPS = {"nuxt": "nuxt", "next": "next"}  # package.json dependency -> app kind (SvelteKit: by file names)
_RX_APP_CONFIG = re.compile(r"(nuxt|next)\.config\.[cm]?[jt]s")
_RX_CODE_FILE = re.compile(r"(?i)\.(?:vue|svelte|[cm]?[jt]sx?)$")
_RX_KIT_FILE = re.compile(r"\+(page|layout|error|server)(?:@[^./]*)?(\.server)?\.(svelte|[cm]?[jt]s)")
_RX_KIT_SRC = re.compile(r"(?:hooks(?:\.server|\.client)?|service-worker(?:/index)?|params/[^/]+)\.[cm]?[jt]s")
_NEXT_APP_FILES = frozenset({"page", "layout", "template", "loading", "error", "global-error", "not-found",
                             "default", "route", "forbidden", "unauthorized"})
_NUXT_FOLDERS = {  # folder of a Nuxt app -> how Nuxt loads its files
    "layouts": "Nuxt가 페이지에 씌울 때 알아서 불러와요.",
    "components": "Nuxt가 화면에 쓰인 이름을 보고 알아서 불러와요.",
    "composables": "Nuxt가 코드에 쓰인 이름을 보고 알아서 불러와요.",
    "utils": "Nuxt가 코드에 쓰인 이름을 보고 알아서 불러와요.",
    "plugins": "Nuxt가 시작할 때 알아서 불러와요.",
    "middleware": "Nuxt가 페이지를 열기 전에 알아서 불러와요.",
}


def project_loaded(index: ProjectIndex) -> dict[str, str]:
    """Files a framework loads by itself with no import naming them (rel_path -> Korean note): the
    pages, layouts and components of a Nuxt app, SvelteKit's route files (+page.svelte, +page.ts,
    +server.ts) and hooks, Next.js's pages/ and app/ files. The graph does not take them for
    cleanup candidates. The start files among them are project_entries()."""
    return dict(_conventions(index)[1])


def _files_under(files: list[str], folder: str):
    """The files below `folder` ("" for all) of a sorted file list."""
    prefix = f"{folder}/" if folder else ""
    for i in range(bisect.bisect_left(files, prefix), len(files)):
        if not files[i].startswith(prefix):
            return
        yield files[i]


def _conventions(index: ProjectIndex) -> tuple[dict[str, str], dict[str, str]]:
    """(start files, files the framework loads) of the Nuxt, SvelteKit and Next.js apps of a project."""
    cache = _cache(index)
    found = cache.get("conventions")
    if found is None:
        entries: dict[str, str] = {}
        loaded: dict[str, str] = {}
        files = _sorted_files(index)
        for kind, root in _framework_apps(index, files):
            (_nuxt_files if kind == "nuxt" else _next_files)(root, index, files, entries, loaded)
        _kit_files(index, files, entries, loaded)
        cache["conventions"] = found = (entries, loaded)
    return found


def _framework_apps(index: ProjectIndex, files: list[str]) -> list[tuple[str, str]]:
    """("nuxt" or "next", package folder) of the apps a package.json dependency or a config file
    (nuxt.config.ts, next.config.js) shows."""
    found: dict[tuple[str, str], None] = {}
    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        if name == "package.json":
            try:
                data = _loads_jsonc(index.read_text(rel) or "")
            except ValueError:
                continue
            deps: set = set()
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                if isinstance(data, dict) and isinstance(data.get(key), dict):
                    deps |= set(data[key])
            found.update(((kind, parent_dir(rel)), None) for dep, kind in _APP_DEPS.items() if dep in deps)
        elif name.startswith(("nuxt.", "next.")) and _RX_APP_CONFIG.fullmatch(name):
            found[(name.split(".", 1)[0], parent_dir(rel))] = None
    return list(found)


def _page_address(path: str) -> str:
    """Address of a file in a pages/ folder: "todos/[id].vue" -> "/todos/[id]", "index.vue" -> "/"."""
    stem = re.sub(r"(?:^|/)index$", "", path.rsplit(".", 1)[0])
    return _route_address(stem)


def _nuxt_files(root: str, index: ProjectIndex, files: list[str], entries: dict, loaded: dict):
    """A Nuxt app: app.vue starts it; it loads pages/ by address, layouts/, components/ and
    composables/ by name, plugins/ and middleware/ by itself, and the server/ folder (Nitro).
    Its own folder is app/ in Nuxt 4 (app/app.vue or app/pages/), else the package folder."""
    app = join_rel(root, "app")
    src = app if app is not None and (_join(app, "app.vue") in index.files or index.is_dir(_join(app, "pages"))) else root
    for rel in _files_under(files, src):
        if not _RX_CODE_FILE.search(rel):
            continue
        tail = rel[len(src) + 1:] if src else rel
        top = tail.split("/", 1)[0]
        if "/" in tail and top == "pages":
            loaded[rel] = f"Nuxt가 {_page_address(tail[6:])} 주소에 맞춰 알아서 불러와요."
        elif "/" in tail and top in _NUXT_FOLDERS:
            loaded[rel] = _NUXT_FOLDERS[top]
        elif tail in ("app.vue", "error.vue"):
            loaded[rel] = "Nuxt가 알아서 불러와요." if tail == "app.vue" else "문제가 생기면 Nuxt가 알아서 띄워요."
    for rel in _files_under(files, root):  # the server and local modules stay in the package folder
        tail = rel[len(root) + 1:] if root else rel
        if _RX_CODE_FILE.search(rel) and tail.startswith(("server/", "modules/")):
            loaded[rel] = "Nuxt 서버가 알아서 불러와요." if tail.startswith("server/") else "Nuxt가 시작할 때 알아서 불러와요."
    entry = next((f for f in (_join(src, "app.vue"), _join(src, "pages/index.vue")) if f in index.files), None)
    if entry:
        entries.setdefault(entry, APP_ENTRY.format(name=_APP_NAMES["nuxt"]))


def _next_files(root: str, index: ProjectIndex, files: list[str], entries: dict, loaded: dict):
    """A Next.js app: it loads pages/ and the special files of app/ (page, layout, route, ...) by
    address, and middleware by itself. The root layout (app/layout.tsx) or pages/_app.tsx starts it."""
    starts: list[str] = []
    homes: list[str] = []
    for base in (root, join_rel(root, "src")):
        if base is None:
            continue
        app = _join(base, "app")
        for rel in _files_under(files, app):
            inner = rel[len(app) + 1:]
            folders, _, name = inner.rpartition("/")
            if _RX_CODE_FILE.search(name) and name.rsplit(".", 1)[0] in _NEXT_APP_FILES:
                # parallel routes (@modal) add nothing to the address, like route groups
                address = _route_address("/".join(p for p in folders.split("/") if not p.startswith("@")))
                loaded[rel] = f"Next.js가 {address} 주소에 맞춰 알아서 불러와요."
                if not folders and name.startswith(("layout.", "page.")):
                    (starts if name.startswith("layout.") else homes).append(rel)
        pages = _join(base, "pages")
        for rel in _files_under(files, pages):
            if _RX_CODE_FILE.search(rel) and not rel.endswith(".d.ts"):
                tail = rel[len(pages) + 1:]
                loaded[rel] = (f"Next.js가 {_page_address(tail)} 주소에 맞춰 알아서 불러와요." if not tail.startswith("_")
                               else "Next.js가 모든 페이지에 씌울 때 알아서 불러와요.")
                if tail.startswith("_app."):
                    starts.append(rel)
                elif tail.startswith("index."):
                    homes.append(rel)
        for name in ("middleware", "instrumentation"):
            for ext in (".ts", ".js", ".mjs", ".tsx", ".jsx"):
                if _join(base, name + ext) in index.files:
                    loaded[_join(base, name + ext)] = "Next.js가 요청마다 알아서 불러와요."
    for rel in starts or homes[:1]:  # no root layout or _app: the home page
        entries.setdefault(rel, APP_ENTRY.format(name=_APP_NAMES["next"]))


def _kit_files(index: ProjectIndex, files: list[str], entries: dict, loaded: dict):
    """SvelteKit's route files, known by their names (+page.svelte, +layout.ts, +server.ts, ...) in
    a routes/ folder, and the hooks, param matchers and service worker beside that folder. The
    root layout (or the root page) starts the app."""
    roots: dict[str, None] = {}
    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        if not name.startswith("+"):
            continue
        m = _RX_KIT_FILE.fullmatch(name)
        parts = rel.split("/")
        if m is None or "routes" not in parts[:-1]:
            continue
        i = parts.index("routes")
        roots["/".join(parts[:i + 1])] = None
        address = _route_address("/".join(parts[i + 1:-1]))
        kind, svelte = m.group(1), m.group(3) == "svelte"
        if kind == "server":
            loaded[rel] = f"SvelteKit이 {address} 주소로 오는 요청에 알아서 불러와요."
        elif kind == "error":
            loaded[rel] = "문제가 생기면 SvelteKit이 알아서 띄워요."
        elif kind == "layout" and svelte:
            where = "모든 페이지에" if address == "/" else f"{address} 아래 페이지에"
            loaded[rel] = f"SvelteKit이 {where} 씌울 때 알아서 불러와요."
        elif svelte:
            loaded[rel] = f"SvelteKit이 {address} 주소에 맞춰 알아서 불러와요."
        else:  # +page.ts, +page.server.ts, +layout.ts: the data of that address's page
            loaded[rel] = f"SvelteKit이 {address} 주소의 화면을 열 때 알아서 불러와요."
    for routes in roots:
        entry = next((f for f in (_join(routes, "+layout.svelte"), _join(routes, "+page.svelte")) if f in index.files),
                     None)
        if entry:
            entries.setdefault(entry, APP_ENTRY.format(name=_APP_NAMES["sveltekit"]))
        src = parent_dir(routes)
        for rel in _files_under(files, src):
            tail = rel[len(src) + 1:] if src else rel
            if _RX_KIT_SRC.fullmatch(tail):
                loaded[rel] = "SvelteKit이 알아서 불러와요."
