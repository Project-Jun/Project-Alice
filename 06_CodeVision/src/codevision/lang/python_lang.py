"""Python analyzer built on the standard ``ast`` module.

Interface (docs/architecture.md section 3): EXTENSIONS, analyze(), resolve(), project_entries().
"""
from __future__ import annotations

import ast
import keyword
import re
import warnings
import weakref

from codevision.describe import is_boilerplate, join_wrapped, starts_with_role
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
    strip_http_base,
)

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

EXTENSIONS = (".py",)

# Korean texts shown in the UI
HINT_DATA = "데이터 모양"
REASON_SERVER = "서버를 만드는 파일"
REASON_MAIN = "직접 실행하는 파일"
REASON_SCRIPT = "명령어로 실행하는 파일"
REASON_DJANGO = "Django 관리 명령"
REASON_COMMAND = "서버 실행 명령에 들어 있는 파일"

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_VERBS = {m.lower() for m in _METHODS}

# top-level import name -> canonical name (architecture.md, "frameworks 표준 이름")
_FRAMEWORKS = {
    "fastapi": "fastapi", "flask": "flask", "django": "django", "rest_framework": "django",
    "starlette": "starlette", "aiohttp": "aiohttp",
    "sqlalchemy": "sqlalchemy", "sqlmodel": "sqlalchemy", "flask_sqlalchemy": "sqlalchemy",
    "sqlite3": "sqlite", "aiosqlite": "sqlite",
    "psycopg2": "postgres", "psycopg": "postgres", "asyncpg": "postgres",
    "pymysql": "mysql", "MySQLdb": "mysql", "mysql": "mysql", "aiomysql": "mysql",
    "pymongo": "mongodb", "motor": "mongodb", "mongoengine": "mongodb", "beanie": "mongodb",
    "redis": "redis", "aioredis": "redis",
    "supabase": "supabase", "firebase_admin": "firebase", "prisma": "prisma",
    # other notable libraries keep their lowercase package name
    "streamlit": "streamlit", "gradio": "gradio", "tornado": "tornado", "sanic": "sanic",
    "quart": "quart", "bottle": "bottle", "falcon": "falcon", "litestar": "litestar",
    "celery": "celery", "pandas": "pandas", "numpy": "numpy", "tkinter": "tkinter",
    "pygame": "pygame", "openai": "openai", "anthropic": "anthropic", "langchain": "langchain",
}
# database URL scheme -> canonical name ("sqlite:///./todo.db", "postgresql+psycopg2://...")
_DSN = {
    "sqlite": "sqlite", "postgres": "postgres", "postgresql": "postgres", "mysql": "mysql",
    "mariadb": "mysql", "mongodb": "mongodb", "redis": "redis", "rediss": "redis",
}
_DSN_RE = re.compile(r"([a-z]+)(?:\+[\w-]+)?://")

_HTTP_LIBS = {"requests", "httpx", "aiohttp"}
_CALL_NAMES = _VERBS | {"request"}
_CLIENT_CLASSES = {"Session", "session", "Client", "AsyncClient", "ClientSession"}
_ROUTER_PREFIX = {"APIRouter": "prefix", "Blueprint": "url_prefix"}  # class -> prefix keyword
_MOUNT_PREFIX = {"include_router": "prefix", "register_blueprint": "url_prefix"}  # method -> prefix keyword
_APP_FACTORIES = {
    "FastAPI", "Flask", "Starlette", "Quart", "Sanic", "Litestar",
    "get_wsgi_application", "get_asgi_application",
}
_DATA_BASES = {"Base", "BaseModel", "Model", "SQLModel"}
_DYNAMIC_IMPORTS = {"importlib.import_module", "import_module", "__import__"}
_HOLE = "${}"  # placeholder for parts of a URL that are only known at run time
_MODULE_PATH = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")  # "pip._internal.commands.install"
_QUOTED_MODULE = re.compile(r"""["'][A-Za-z_]\w*\.[A-Za-z_][\w.]*["']""")
_COLLECTIONS = (ast.Dict, ast.List, ast.Tuple, ast.Set)

# Quick looks at the raw text decide which expression details are worth collecting.
# Most files of a big library need none of them, so their expressions are never walked.
# Plain substring tests come first because a regex over every file costs more than parsing.
_NEEDS_HTTP = re.compile(
    r"^[ \t]*(?:import[ \t][^\n#]*\b(?:requests|httpx|aiohttp)\b|from[ \t]+(?:requests|httpx|aiohttp)[ \t]+import\b)",
    re.M,
)
_DSN_WORDS = ("postgres", "mysql", "mariadb", "mongodb", "redis", "POSTGRES", "MYSQL")
_NEEDS_ROUTES = re.compile(r"@[ \t]*[\w.]+[ \t]*\.[ \t]*(?:get|post|put|patch|delete|route|api_route)\b")
_TRY_TYPES = tuple(getattr(ast, name) for name in ("Try", "TryStar") if hasattr(ast, name))


def _needs_http(text: str) -> bool:
    return ("requests" in text or "httpx" in text or "aiohttp" in text) and bool(_NEEDS_HTTP.search(text))


def _needs_dsn(text: str) -> bool:
    """Could the file hold a database URL ("sqlite:///x.db", "postgresql://...")?"""
    if "sqlite:" in text or "SQLITE:" in text:
        return True
    return "://" in text and any(word in text for word in _DSN_WORDS)


def _needs_routes(text: str) -> bool:
    return "@" in text and bool(_NEEDS_ROUTES.search(text))


# ---------------------------------------------------------------- analyze

def analyze(rel_path: str, source: str) -> FileFacts:
    """Collect the facts of one Python file. Never raises."""
    text = (source or "").replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. SyntaxWarning for "\d" in the user's code
            tree = ast.parse(text, filename=rel_path or "<file>")
        facts = FileFacts(path=rel_path, language="python", lines=lines)
        try:
            _fill_from_tree(facts, tree, text)
        except RecursionError:  # valid Python nested deeper than the walk can follow: keep what was found
            pass
    except Exception:  # SyntaxError, ValueError (null bytes), RecursionError, MemoryError, ...
        facts = FileFacts(path=rel_path, language="python", lines=lines, parse_error=True)
        try:
            _fill_from_text(facts, text)
        except Exception:
            pass
    if not facts.is_entry and (rel_path or "").rsplit("/", 1)[-1] == "__main__.py":  # python -m pkg
        facts.is_entry, facts.entry_reason = True, REASON_MAIN
    return facts


def _fill_from_tree(facts: FileFacts, tree: ast.Module, text: str) -> None:
    visitor = _Visitor(facts, text)
    visitor.run(tree)
    facts.imports.sort(key=lambda imp: imp.line)
    facts.symbols = _symbols(tree, visitor.handler_routes)
    if visitor.server_app:
        facts.is_entry, facts.entry_reason = True, REASON_SERVER
    elif visitor.main_guard:
        facts.is_entry, facts.entry_reason = True, REASON_MAIN
    name = _own_name(facts.path)
    facts.module_doc = _doc_summary(ast.get_docstring(tree), name) or _leading_comment(text, name)


def _own_name(rel_path: str) -> str:
    """The name a docstring title would repeat: the module, or the package for __init__/__main__."""
    parts = (rel_path or "").split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    if stem in ("__init__", "__main__") and len(parts) > 1:
        return parts[-2]
    return stem


class _Visitor:
    """One pass over the statements in source order: imports, HTTP routes and calls,
    frameworks and entry clues.

    Expressions are only searched when the text shows they can matter (HTTP libraries,
    dynamic imports, database URLs). That keeps big libraries fast: most of their
    files have none of these, so only the statements are visited.
    """

    def __init__(self, facts: FileFacts, text: str):
        self.facts = facts
        self.scopes: list[dict[str, str]] = [{}]  # variable -> string value, one dict per def/class
        self.depth = 0  # > 0 inside a function or class body
        self.type_only = 0  # > 0 inside `if TYPE_CHECKING:` (imports there never run)
        self.http_libs: dict[str, str] = {}  # local alias -> requests | httpx | aiohttp
        self.http_funcs: dict[str, str] = {}  # `from requests import get` -> {"get": "get"}
        self.client_classes: set[str] = set()  # `from httpx import Client`
        self.clients: set[str] = set()  # dotted names bound to a session/client object
        self.prefixes: dict[str, str] = {}  # router/blueprint variable -> its own URL prefix
        self.handler_routes: dict[ast.AST, list[list[str]]] = {}  # def node -> [[method, path shown]]
        # route -> router variable it was added to, its path as written, its [method, path shown] hints
        self.route_owners: list[tuple[HttpRoute, str, str, list[list[str]]]] = []
        # routers of this file mounted in it: (prefix or None, router, router or app it is mounted on, Flask?)
        self.local_mounts: list[tuple[str | None, str, str, bool]] = []
        self.mount_parents: list[tuple[HttpMount, str]] = []  # mounts of imported routers -> what they are mounted on
        self.bare_mounts: list[HttpMount] = []  # imported routers mounted on the app without a prefix
        self.bindings: dict[str, tuple] = {}  # local name -> how an import bound it (for mounts)
        self.server_app = False
        self.main_guard = False
        self.module_strings: list[tuple[str, int]] = []  # dotted strings, for import_module(name)
        self.computed_import = False  # import_module() with a name only known at run time
        self.registry: list[tuple[str, int]] = []  # module paths listed in a module-level table
        self.source = text
        self.lines: list[str] | None = None
        http = _needs_http(text)
        self.want_calls = http or "import_module" in text or "__import__" in text
        if "DJANGO_SETTINGS_MODULE" in text:  # manage.py, wsgi.py: the settings module is named by a string
            self.want_calls = self.computed_import = True
        self.want_dsn = _needs_dsn(text)
        # app.include_router(todos.router, prefix="/api"), app.register_blueprint(bp, url_prefix="/api")
        self.want_mounts = "include_router" in text or "register_blueprint" in text
        # A standard-library server (http.server): its routes are `self.path == "/x"` checks in
        # do_GET/do_POST and tables keyed by path, not decorators.
        self.want_server = "HTTPRequestHandler" in text and "http.server" in text
        self.funcs: list[str] = []  # names of the functions being visited, innermost last
        # string constants for URLs and for module names built at run time
        self.want_strings = http or self.want_mounts or "import_module" in text or _needs_routes(text)
        self.want_exprs = self.want_calls or self.want_dsn or self.want_server

    def run(self, tree: ast.Module) -> None:
        dispatch = _STATEMENTS
        for node in tree.body:  # self.block(tree.body), one statement at a time
            try:
                handler = dispatch.get(type(node))
                if handler is not None:
                    handler(self, node)
                elif self.want_exprs:
                    self.expr(node)
            except RecursionError:  # nested deeper than this walk can follow: skip the rest of it
                self.depth = self.type_only = 0  # back at module level, as after any statement
                del self.scopes[1:]
                self.funcs.clear()
        self._place_routers()
        # Modules loaded by name: importlib.import_module(name) with the names written as strings
        # somewhere in the file, or a module-level table of module paths that another file loads
        # (pygments' LEXERS, pip's commands_dict, Django's INSTALLED_APPS).
        found = (self.module_strings if self.computed_import else []) + self.registry
        seen = {imp.spec for imp in self.facts.imports if not imp.level}
        for spec, line in found:
            if spec not in seen:
                seen.add(spec)
                self.facts.imports.append(ImportRef(spec, [], "dynamic", 0, line))

    def _table(self, value: ast.expr) -> None:
        """Collect "pkg.module" strings assigned at module level: a dict, list, tuple or set of
        them, or one string (ROOT_URLCONF = "mysite.urls")."""
        if type(value) is ast.Constant:
            if type(value.value) is str and len(value.value) < 200 and _MODULE_PATH.fullmatch(value.value):
                self.registry.append((value.value, value.lineno))
            return
        if not isinstance(value, _COLLECTIONS):
            return
        if self.lines is None:
            self.lines = self.source.split("\n")
        span = self.lines[value.lineno - 1:getattr(value, "end_lineno", value.lineno) or value.lineno]
        if not any(_QUOTED_MODULE.search(line) for line in span):
            return  # most tables (unicode data, word lists) hold no module paths
        todo = [value]
        while todo:
            item = todo.pop()
            kind = type(item)
            if kind is ast.Dict:  # registries keep module paths in the values; keys are names
                todo.extend(reversed([v for v in item.values if v is not None]))
            elif kind is ast.Constant:
                text = item.value
                if type(text) is str and len(text) < 200 and _MODULE_PATH.fullmatch(text):
                    self.registry.append((text, item.lineno))
            else:
                todo.extend(reversed(list(ast.iter_child_nodes(item))))

    # statements
    def block(self, body: list[ast.stmt]) -> None:
        dispatch = _STATEMENTS  # a class-level table: no reference cycle, so the tree is freed at once
        for node in body:
            handler = dispatch.get(type(node))
            if handler is not None:
                handler(self, node)
            elif self.want_exprs:  # Expr, Return, AugAssign, Raise, ...
                self.expr(node)

    def visit_If(self, node: ast.If) -> None:
        while True:  # an elif is an If inside the orelse: a loop, so 1,000 branches are not 1,000 calls deep
            if self.depth == 0 and _is_main_guard(node.test):
                self.main_guard = True
            self.expr(node.test)
            if _is_type_checking(node.test):
                self.type_only += 1
                self.block(node.body)
                self.type_only -= 1
            else:
                self.block(node.body)
            orelse = node.orelse
            if len(orelse) == 1 and type(orelse[0]) is ast.If:
                node = orelse[0]
                continue
            self.block(orelse)
            return

    def visit_Try(self, node) -> None:
        self.block(node.body)
        for handler in node.handlers:
            self.expr(handler.type)
            self.block(handler.body)
        self.block(node.orelse)
        self.block(node.finalbody)

    def visit_For(self, node) -> None:
        self.expr(node.iter)
        self.block(node.body)
        self.block(node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self.expr(node.test)
        self.block(node.body)
        self.block(node.orelse)

    def visit_Match(self, node) -> None:
        self.expr(node.subject)
        for case in node.cases:
            self.expr(case.guard)
            self.block(case.body)

    # expressions: calls (dynamic imports, HTTP) and string constants (database URLs)
    def expr(self, node: ast.AST | None) -> None:
        if node is None or not self.want_exprs:
            return
        todo = [node]
        while todo:
            item = todo.pop()
            kind = type(item)
            if kind is ast.Constant:
                value = item.value
                if type(value) is str:
                    if self.want_dsn:
                        name = _dsn_framework(value)
                        if name:
                            self.facts.frameworks.add(name)
                    if self.want_calls and len(value) < 200 and _MODULE_PATH.fullmatch(value):
                        self.module_strings.append((value, item.lineno))
                continue
            if kind is ast.Call and self.want_calls:
                self._dynamic_import(item)
                self._http_call(item)
            if self.want_server and (kind is ast.Compare or kind is ast.Call):
                self._server_check(item)
            children = list(ast.iter_child_nodes(item))
            children.reverse()  # keep source order
            todo.extend(children)

    # imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add_import(ImportRef(alias.name, [], "import", 0, node.lineno))
            if alias.name in _HTTP_LIBS:
                self.http_libs[alias.asname or alias.name] = alias.name
            if self.want_mounts:  # `import routes.todos` binds "routes"; `import x.y as t` binds "t"
                self.bindings[alias.asname or alias.name.split(".", 1)[0]] = ("import", alias.name, bool(alias.asname))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        names = [alias.name for alias in node.names]
        self._add_import(ImportRef(module, names, "from", node.level or 0, node.lineno))
        if self.want_mounts:
            for alias in node.names:
                self.bindings[alias.asname or alias.name] = ("from", module, alias.name, node.level or 0)
        if not node.level and module in _HTTP_LIBS:
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in _CALL_NAMES:
                    self.http_funcs[local] = alias.name
                elif alias.name in _CLIENT_CLASSES:
                    self.client_classes.add(local)

    def _add_import(self, imp: ImportRef) -> None:
        self.facts.imports.append(imp)
        name = _framework_of(imp) if not self.type_only else None  # type hints only: not a real use
        if name:
            self.facts.frameworks.add(name)

    # calls
    def _dynamic_import(self, node: ast.Call) -> None:
        if _dotted(node.func) not in _DYNAMIC_IMPORTS or not node.args:
            return
        spec = (_literal(node.args[0]) or "").strip()
        if spec:
            level = len(spec) - len(spec.lstrip("."))
            self._add_import(ImportRef(spec[level:], [], "dynamic", level, node.lineno))
            return
        self.computed_import = True
        # import_module(f".unicode{version}", __package__), directly or through a variable: one of the
        # package's unicode*.py modules (rich's tables, named unicode15-0-0.py and so on)
        text = self.text(node.args[0])
        if _HOLE not in text:
            return
        text = text.split(_HOLE, 1)[0]
        level = len(text) - len(text.lstrip("."))
        package = node.args[1] if len(node.args) > 1 else _kw(node, "package")
        if level and not (isinstance(package, ast.Name) and package.id in ("__package__", "__name__")):
            return  # relative to a package named some other way
        module, _, prefix = text[level:].rpartition(".")
        if prefix and re.fullmatch(r"\w+", prefix) and (not module or _MODULE_NAME.fullmatch(module)):
            self._add_import(ImportRef(module, [prefix + "*"], "dynamic", level, node.lineno))

    def _http_call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            owner = _dotted(func.value)
            if owner is None or (owner not in self.http_libs and owner not in self.clients):
                return
            verb = func.attr
        elif isinstance(func, ast.Name) and func.id in self.http_funcs:
            verb = self.http_funcs[func.id]
        else:
            return
        args = list(node.args)
        if verb == "request":  # requests.request("GET", url)
            method = (_literal(args.pop(0) if args else _kw(node, "method")) or "ANY").upper()
            if method not in _METHODS and method != "ANY":
                return
        elif verb in _VERBS:
            method = verb.upper()
        else:
            return
        url = args[0] if args else _kw(node, "url")
        path = normalize_http_path(self.text(url)) if url is not None else None
        if path:
            self.facts.http_calls.append(HttpCall(method, path, node.lineno))

    def _is_client(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr in _CLIENT_CLASSES and _dotted(func.value) in self.http_libs
        return isinstance(func, ast.Name) and func.id in self.client_classes

    # http.server handlers
    def _server_route(self, method: str, raw: str, line: int) -> None:
        path = normalize_http_path(raw)
        last = path.rsplit("/", 1)[-1] if path else ""
        if not path or path == "/" or ("." in last and last != "*"):
            return  # the page itself and static files (/app.js): what a fetch() asks for is an API path
        if not any(r.method == method and r.path == path for r in self.facts.http_routes):
            self.facts.http_routes.append(HttpRoute(method, path, self.funcs[-1] if self.funcs else "", line))

    def _server_check(self, node: ast.Compare | ast.Call) -> None:
        """`self.path == "/api/todos"` or `path.startswith("/api/todos/")` in a request handler."""
        func = self.funcs[-1] if self.funcs else ""
        method = func[3:] if func.startswith("do_") and func[3:] in _METHODS else "ANY"
        if type(node) is ast.Compare:
            if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.In)):
                return
            sides = [node.left, node.comparators[0]]
            if not any(_is_path_expr(s) for s in sides):
                return
            for side in sides:
                values = side.elts if isinstance(side, (ast.Tuple, ast.List, ast.Set)) else [side]
                for value in values:
                    text = _literal(value)
                    if text and text.startswith("/"):
                        self._server_route(method, text, node.lineno)
            return
        func_node = node.func
        if (isinstance(func_node, ast.Attribute) and func_node.attr == "startswith" and node.args
                and _is_path_expr(func_node.value)):
            text = _literal(node.args[0])
            if text and text.startswith("/"):  # "/api/todos/" + an id
                self._server_route(method, text + "*" if text.endswith("/") and len(text) > 1 else text, node.lineno)

    def _server_table(self, value: ast.expr) -> None:
        """A module-level table keyed by path: {"/": ..., ("GET", "/api/analyze"): ...}."""
        if type(value) is not ast.Dict:
            return
        for key in value.keys:
            if isinstance(key, ast.Tuple) and len(key.elts) == 2:
                method, path = _literal(key.elts[0]), _literal(key.elts[1])
                if method and path and method.upper() in _METHODS and path.startswith("/"):
                    self._server_route(method.upper(), path, key.lineno)
            else:
                path = _literal(key)
                if path and path.startswith("/"):
                    self._server_route("ANY", path, key.lineno)

    # mounted routers
    def visit_Expr(self, node: ast.Expr) -> None:
        if self.want_mounts and type(node.value) is ast.Call:
            self._mount(node.value)
        if self.want_exprs:
            self.expr(node)

    def _mount(self, call: ast.Call) -> None:
        """app.include_router(todos.router, prefix="/api/todos"): the routes of routes/todos.py
        answer under /api/todos. Flask: app.register_blueprint(bp, url_prefix="/api") answers under
        /api instead of the blueprint's own url_prefix. A router mounted on another router of this
        file (api_bp.register_blueprint(bp)) answers under that router's prefix, even without one.
        One mounted on the app without a prefix answers at the paths as written: that counts only
        next to another mount of the same router in this file (under /v1 and as written: both)."""
        func = call.func
        keyword = _MOUNT_PREFIX.get(func.attr) if isinstance(func, ast.Attribute) else None
        router = _dotted(call.args[0]) if call.args else None
        if not keyword or not router:
            return
        prefix_node = _kw(call, keyword)
        if isinstance(prefix_node, ast.Constant) and prefix_node.value is None:
            prefix_node = None  # url_prefix=None: the router's own prefix stays
        parent = _dotted(func.value) or ""
        prefix = self.text(prefix_node) if prefix_node is not None else None
        flask = keyword == "url_prefix"
        head = router.split(".", 1)[0]
        bound = self.bindings.get(head)
        if bound is None:  # a router made in this file
            self.local_mounts.append((prefix, router, parent, flask))
            return
        if bound[0] == "from":  # from routes import todos / from routes.todos import router
            _, module, name, level = bound
            target = ImportRef(module, [name], "from", level, call.lineno)
        else:  # import routes.todos -> routes.todos.router; import routes.todos as t -> t.router
            _, module, aliased = bound
            spec = module if aliased or "." not in router else router.rsplit(".", 1)[0]
            target = ImportRef(spec, [], "import", 0, call.lineno)
        mount = HttpMount(prefix or "", target, call.lineno, replace=flask and prefix is not None)
        self.facts.http_mounts.append(mount)
        self.mount_parents.append((mount, parent))  # its base is known once the whole file is read
        if prefix is None and parent not in self.prefixes:
            self.bare_mounts.append(mount)

    def _place_routers(self) -> None:
        """Routers mounted on a router or the app of this file answer under that mount, and the
        mounts of imported routers learn the prefix of the router they are mounted on.

        FastAPI joins the prefixes: APIRouter(prefix="/todos") included with prefix="/api" answers
        under /api/todos. Flask: a url_prefix given to register_blueprint takes the place of the
        blueprint's own one, and a blueprint registered on another one answers under its prefix.
        """
        if not self.local_mounts and not self.mount_parents:
            return
        if self.bare_mounts:  # on the app as written: only next to another mount of the same router
            def key(m: HttpMount) -> tuple:
                return m.target.spec, tuple(m.target.names), m.target.kind, m.target.level

            bare = {id(m) for m in self.bare_mounts}
            others = {key(m) for m in self.facts.http_mounts if id(m) not in bare}
            alone = {id(m) for m in self.bare_mounts if key(m) not in others}
            self.facts.http_mounts = [m for m in self.facts.http_mounts if id(m) not in alone]
            self.mount_parents = [(m, parent) for m, parent in self.mount_parents if id(m) not in alone]
        mounts: dict[str, list[tuple[str | None, str, bool]]] = {}  # router -> where it is mounted
        for prefix, router, parent, flask in self.local_mounts:
            mounts.setdefault(router, []).append((prefix, parent, flask))
        for router, places in list(mounts.items()):  # only on the app, as written: nothing moves
            if all(prefix is None and parent not in self.prefixes for prefix, parent, _ in places):
                del mounts[router]
        known: dict[tuple[str, int], list[tuple[str, str]]] = {}

        def placed(name: str, trail: frozenset = frozenset()) -> list[tuple[str, str]]:
            """(prefix the routes of `name` answer under, own prefix of the router the chain starts
            from) pairs. The second is what a Flask url_prefix from another file takes the place of.
            Each router is worked out once per depth: routers mounted many times on each other, nine
            deep, would otherwise take minutes."""
            own = self.prefixes.get(name, "")
            if name not in mounts or name in trail or len(trail) > 8:
                return [(own, own)]
            key = (name, len(trail))
            if key in known:
                return known[key]
            out: list[tuple[str, str]] = []
            for prefix, parent, flask in mounts[name]:
                for outer, top in placed(parent, trail | {name}):
                    if flask:  # register_blueprint(bp, url_prefix=...) stands instead of bp's own url_prefix
                        out.append((_join_url(outer, own if prefix is None else prefix), top))
                    else:  # include_router(router, prefix=...) goes in front of the router's own prefix
                        out.append((_join_url(_join_url(outer, prefix or ""), own), top))
            known[key] = result = list(dict.fromkeys(out))[:8]
            return result

        for mount, parent in self.mount_parents:
            (mount.base, mount.base_own), *more = placed(parent)
            for base, own in more:  # the router it is mounted on is mounted twice (/v1 and /v2): one mount each
                self.facts.http_mounts.append(HttpMount(mount.prefix, mount.target, mount.line, base, mount.replace, own))
        moved: dict[int, list[HttpRoute]] = {}
        for route, owner, rule, hints in self.route_owners:
            if owner not in mounts:
                continue
            options = placed(owner)
            rest = strip_http_base(route.path, normalize_http_path(self.prefixes.get(owner, "")) or "")
            paths: dict[str, HttpRoute] = {}
            shown: list[str] = []  # each address as written, for the hint next to the function
            for prefix, top in options:
                path = join_http_path(prefix, rest)
                if not path:
                    continue
                if path not in paths:
                    paths[path] = HttpRoute(route.method, path, route.handler, route.line, normalize_http_path(top) or "")
                written = re.sub(r"^(?:\$\{\})+", "", _join_url(prefix, rule)) or path  # unknown prefix: the rest
                if written not in shown:
                    shown.append(written)
            moved[id(route)] = list(paths.values()) or [route]
            if shown:  # mounted twice (/v1 and /v2): both addresses
                for hint in hints:
                    hint[1] = ", ".join(shown)
        if moved:
            routes: list[HttpRoute] = []
            for route in self.facts.http_routes:
                routes.extend(moved.get(id(route), [route]))
            self.facts.http_routes = routes

    # assignments and blocks
    def visit_Assign(self, node: ast.Assign) -> None:
        self.expr(node.value)
        if self.depth == 0:
            self._table(node.value)
            if self.want_server:
                self._server_table(node.value)
        self._bind(node.targets, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.expr(node.value)
            if self.depth == 0:
                self._table(node.value)
            self._bind([node.target], node.value)

    def _bind(self, targets: list[ast.expr], value: ast.expr) -> None:
        if isinstance(value, ast.Call):
            names = [name for name in map(_dotted, targets) if name]
            callee = _callee(value)
            if callee in _ROUTER_PREFIX:
                prefix = _kw(value, _ROUTER_PREFIX[callee])
                for name in names:
                    self.prefixes[name] = self.text(prefix) if prefix is not None else ""
            if self._is_client(value):
                self.clients.update(names)
            if self.depth == 0 and callee in _APP_FACTORIES:
                self.server_app = True
        if self.want_strings:
            for target in targets:
                if isinstance(target, ast.Name):
                    self.scopes[-1][target.id] = self.text(value)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if isinstance(item.context_expr, ast.Call) and self._is_client(item.context_expr):
                name = _dotted(item.optional_vars) if item.optional_vars is not None else None
                if name:
                    self.clients.add(name)
            self.expr(item.context_expr)
        self.block(node.body)

    # functions and classes
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        routes = [route for dec in node.decorator_list for route in self._routes(dec, node.name)]
        if routes:
            self.handler_routes[node] = routes
        a = node.args
        for item in (*node.decorator_list, *a.defaults, *a.kw_defaults):
            self.expr(item)
        scope: dict[str, str] = {}
        if self.want_strings:  # parameters hide outer constants
            params = a.posonlyargs + a.args + a.kwonlyargs + [p for p in (a.vararg, a.kwarg) if p]
            scope = {p.arg: _HOLE for p in params}
        self.funcs.append(node.name)
        self._enter(node.body, scope)
        self.funcs.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for item in (*node.bases, *(k.value for k in node.keywords), *node.decorator_list):
            self.expr(item)
        self._enter(node.body, {})

    def _enter(self, body: list[ast.stmt], scope: dict[str, str]) -> None:
        self.scopes.append(scope)
        self.depth += 1
        self.block(body)
        self.depth -= 1
        self.scopes.pop()

    def _routes(self, dec: ast.expr, handler: str) -> list[list[str]]:
        """Routes from @app.get("/x"), @router.post(...), @app.route("/x", methods=[...])."""
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            return []
        verb = dec.func.attr
        if verb in _VERBS:
            methods = [verb.upper()]
        elif verb in ("route", "api_route"):
            methods = _route_methods(dec)
        else:
            return []
        path_node = dec.args[0] if dec.args else (_kw(dec, "path") or _kw(dec, "rule"))
        if path_node is None:
            return []
        owner = _dotted(dec.func.value) or ""
        own, rule = self.prefixes.get(owner, ""), self.text(path_node)
        raw = _join_url(own, rule)
        path = normalize_http_path(raw)
        if not path:
            return []
        base = normalize_http_path(own) or ""  # the part a Flask url_prefix of another file stands instead of
        routes = [HttpRoute(m, path, handler, dec.lineno, base) for m in methods]
        self.facts.http_routes.extend(routes)
        shown = re.sub(r"^(?:\$\{\})+", "", raw) or path  # unknown prefix: show the rest
        hints = [[m, shown] for m in methods]  # lists: a mount later in the file can change the path
        self.route_owners.extend((route, owner, rule, hints) for route in routes)
        return hints

    # strings
    def text(self, node: ast.expr) -> str:
        return re.sub(r"(?:\$\{\})+", "${}", _string_of(node, self.lookup))

    def lookup(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None


_STATEMENTS = {
    ast.Expr: _Visitor.visit_Expr,
    ast.Import: _Visitor.visit_Import,
    ast.ImportFrom: _Visitor.visit_ImportFrom,
    ast.FunctionDef: _Visitor.visit_FunctionDef,
    ast.AsyncFunctionDef: _Visitor.visit_FunctionDef,
    ast.ClassDef: _Visitor.visit_ClassDef,
    ast.Assign: _Visitor.visit_Assign,
    ast.AnnAssign: _Visitor.visit_AnnAssign,
    ast.If: _Visitor.visit_If,
    ast.With: _Visitor.visit_With,
    ast.AsyncWith: _Visitor.visit_With,
    ast.For: _Visitor.visit_For,
    ast.AsyncFor: _Visitor.visit_For,
    ast.While: _Visitor.visit_While,
    **{kind: _Visitor.visit_Try for kind in _TRY_TYPES},
    **({ast.Match: _Visitor.visit_Match} if hasattr(ast, "Match") else {}),
}


def _string_of(node: ast.expr, lookup) -> str:
    """Best-effort text of a string expression; parts not known before run time become ${}."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else _HOLE
    if isinstance(node, ast.JoinedStr):  # f"{BASE}/todos/{todo_id}"
        return "".join(_string_of(part, lookup) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _string_of(node.value, lookup) if isinstance(node.value, ast.Name) else _HOLE
    if isinstance(node, ast.Name):
        value = lookup(node.id)
        return _HOLE if value is None else value
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):  # "a" + b + "c": the chain is walked in a loop, however long
            parts = []
            while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                parts.append(node.right)
                node = node.left
            parts.append(node)
            return "".join(_string_of(part, lookup) for part in reversed(parts))
        if isinstance(node.op, ast.Mod) and _literal(node.left) is not None:  # "/todos/%s" % id
            return re.sub(r"%(?:\(\w+\))?[^a-zA-Z%]*[a-zA-Z]", _HOLE, node.left.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        template = _literal(node.func.value)  # "/todos/{}".format(id)
        if template is not None:
            return re.sub(r"\{[^{}]*\}", _HOLE, template)
    return _HOLE


def _route_methods(call: ast.Call) -> list[str]:
    node = _kw(call, "methods")
    if node is None:
        return ["GET"]  # Flask route and FastAPI api_route default
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return ["ANY"]
    found: list[str] = []
    for element in node.elts:
        value = _literal(element)
        if value is None:
            return ["ANY"]
        method = value.upper()
        if method in _METHODS and method not in found:
            found.append(method)
    return found


def _join_url(prefix: str, path: str) -> str:
    if not prefix:
        return path
    if not path.strip("/"):
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _symbols(tree: ast.Module, handler_routes: dict) -> list[Symbol]:
    public = _dunder_all(tree)
    out: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind, signature = "function", _signature(node)
            hint = _route_hint(handler_routes.get(node, []))
        elif isinstance(node, ast.ClassDef):
            kind, signature = "class", f"class {node.name}"
            hint = HINT_DATA if any(_last_name(base) in _DATA_BASES for base in node.bases) else ""
        else:
            continue
        exported = node.name in public if public is not None else not node.name.startswith("_")
        doc = _doc_summary(ast.get_docstring(node))
        out.append(Symbol(node.name, kind, signature, doc, hint, node.lineno, exported))
    return out


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """create_todo(todo, db): no self/cls, annotations or defaults; keeps *args and **kwargs."""
    a = fn.args
    params = [p.arg for p in a.posonlyargs + a.args]
    if params and params[0] in ("self", "cls"):
        params.pop(0)
    if a.vararg:
        params.append("*" + a.vararg.arg)
    params += [p.arg for p in a.kwonlyargs]
    if a.kwarg:
        params.append("**" + a.kwarg.arg)
    return f"{fn.name}({', '.join(params)})"


def _route_hint(routes: list[tuple[str, str]]) -> str:
    if not routes:
        return ""
    methods = ", ".join(dict.fromkeys(method for method, _ in routes))
    return f"{methods} · {routes[0][1]}"


def _dunder_all(tree: ast.Module) -> set[str] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return {name for name in map(_literal, node.value.elts) if name}
    return None


def _is_main_guard(test: ast.expr) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = (test.left, test.comparators[0])
    return (any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
            and any(_literal(s) == "__main__" for s in sides))


def _is_type_checking(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:` (code there only runs for type checkers)."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


# ---------------------------------------------------------------- small AST helpers

def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _dotted(node: ast.AST | None) -> str | None:
    """"a.b.c" for a Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _callee(call: ast.Call) -> str:
    """Last name of the called function: "FastAPI" for fastapi.FastAPI(...)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _last_name(node: ast.expr) -> str:
    if isinstance(node, ast.Subscript):  # Generic[T]
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def _framework_of(imp: ImportRef) -> str | None:
    if imp.level or not imp.spec:
        return None
    if imp.spec == "http.server" or (imp.spec == "http" and "server" in imp.names):  # the standard library's
        return "http.server"
    return _FRAMEWORKS.get(imp.spec.split(".", 1)[0])


def _is_path_expr(node: ast.expr) -> bool:
    """self.path, path, parsed.path, urlparse(self.path).path: the request path in a handler."""
    if isinstance(node, ast.Attribute):
        last = node.attr
    elif isinstance(node, ast.Name):
        last = node.id
    else:
        return False
    return last.lower() in ("path", "route", "pathname", "url_path", "request_path")


def _dsn_framework(value: str) -> str | None:
    """Database of a connection URL. A bare scheme ("redis://" in a list of URL kinds) is not one."""
    head = value[:60].strip().lower()
    if head.startswith("sqlite:"):
        return "sqlite" if head.startswith("sqlite://") else None  # "sqlite://" is SQLAlchemy's in-memory DB
    match = _DSN_RE.match(head)
    return _DSN.get(match.group(1)) if match and len(head) > match.end() else None


# ---------------------------------------------------------------- docs and comments

_PRAGMA = re.compile(r"(?:-\*-|vim?:|(?:en)?coding[:=]|noqa|type:|pylint:|flake8|mypy:|ruff:|fmt:|isort:|pyright:)", re.I)
_NAME_DASH = re.compile(r"^[A-Za-z_][\w.]*\s+--?\s+(?=\S+\s+\S)")  # "idlelib.config -- Manage IDLE ..."
_RCS_KEYWORD = re.compile(r"\$[A-Za-z]+:[^$\n]*\$")  # "$Id: __init__.py 3375 2008-02-13 fredrik $"
_TRAILING_PRAGMA = re.compile(r"\s+#\s*(?:noqa|type:|pragma|nosec|fmt:|pylint:)[^#]*$", re.I)
_FILE_PREFIX = re.compile(r"^[\w.-]+\.py\s*(?::|--?)\s+(?=\S)")
_URL = re.compile(r"\b(?:https?|ftp)://\S+")
_SECTION_TITLES = frozenset({
    "parameters", "returns", "yields", "raises", "examples", "example", "notes", "note", "see also",
    "references", "attributes", "methods", "args", "arguments", "usage", "contents",
})
_BULLET = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+(?=[A-Z0-9가-힣\"'(\[])")
# :mod:`idna`, :class:`Foo <pkg.Foo>`; a backslash right after one only joins it to the next word
# (":class:`Option`\s" is shown as "Options")
_RST_ROLE = re.compile(r":([\w.:+-]+):`([^`]*?)(?:\s*<([^`<>]*)>)?`(?:\\ ?)?")
_DIRECTIVE = re.compile(r"\.\.\s+[\w:.+-]+::")  # ".. note::", ".. deprecated:: 1.6", ".. code-block:: python"
# Roles that name a code object: in them "~", a leading "." and "!" are link markup, not text.
_OBJECT_ROLES = frozenset({
    "mod", "module", "func", "function", "meth", "method", "class", "exc", "exception", "attr", "attribute",
    "data", "const", "obj", "any", "type", "property", "deco", "member", "var", "macro", "struct", "union", "enum",
    "enumerator", "expr", "texpr",
})
_BACKTICKS = re.compile(r"``(.+?)``|`([^`]+)`_{0,2}")
_SUMMARY_SCAN = 4_000  # characters of a paragraph's first sentence that _summary_text looks at


def _role_text(match: re.Match) -> str:
    """The text Sphinx shows for a role: :class:`.HTTPConnectionPool` -> HTTPConnectionPool,
    :func:`~a.b.c` -> c, :class:`Foo <a.Foo>` -> Foo, :class:`!Foo` -> Foo, :pep:`8` -> PEP 8."""
    kind, text, target = match.group(1).lower().rsplit(":", 1)[-1], match.group(2).strip(), match.group(3)
    if target is not None:
        return text or target.strip()  # a title written out is shown as it is
    if kind in ("pep", "rfc"):
        return f"{kind.upper()} {text}"
    if kind not in _OBJECT_ROLES:
        return text  # :file:`~/.bashrc`, :kbd:`Ctrl+C`, :ref:`install`
    if text.startswith("!"):  # no link: the rest as written
        return text[1:]
    text = text.lstrip(".")  # ".HTTPConnectionPool": look in this module first
    if text.startswith("~"):  # "~a.b.c": only the last part
        text = text[1:].rsplit(".", 1)[-1]
    return text


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _doc_summary(doc: str | None, name: str = "") -> str:
    """First sentence of the first real paragraph of a docstring, "" if there is none.

    Wrapped lines are joined, so the sentence is not cut at a line break. A title that
    only names the module ("pygments.lexer" over "~~~~", "curses"), reST directives with
    the block under them, doctests, field lists and license or copyright paragraphs are skipped.
    """
    text = doc or ""
    lines = text.splitlines()
    return _first_summary(_paragraphs(_without_directive_blocks(lines) if ".." in text else lines), name)


def _without_directive_blocks(lines: list[str]):
    """The lines, with the indented block under a reST directive made blank: the text under
    ".. deprecated:: 1.6.0" (":func:`distro.linux_distribution()` is deprecated.") is the notice's,
    not what the function does."""
    depth = None  # indent of the directive whose block is being passed over
    for line in lines:
        text = line.lstrip()
        indent = len(line) - len(text)
        if depth is not None:
            if not text or indent > depth:
                yield ""
                continue
            depth = None
        if text[:2] == ".." and _DIRECTIVE.match(text):
            depth = indent
        yield line


def _leading_comment(text: str, name: str = "") -> str:
    """First sentence of the comment block at the top of a file.

    Skips the shebang, coding and lint lines, "# ====" rulers and license or copyright
    paragraphs. Wrapped comment lines are joined like a docstring paragraph.
    """
    lines: list[str] = []
    for number, raw in enumerate(text[:20_000].split("\n")):
        line = raw.strip()
        if line and not line.startswith("#"):
            break
        body = line.lstrip("#").strip()
        if (number == 0 and line.startswith("#!")) or (body and _PRAGMA.match(body)):
            continue
        lines.append(body)
    return _first_summary(_paragraphs(lines), name)


def _first_summary(paragraphs, name: str) -> str:
    own = name.lower()
    for para in paragraphs:
        if len(para) == 1:
            title = para[0].rstrip(".").lower()
            if _MODULE_PATH.fullmatch(title) or (own and (title == own or title.endswith("." + own))):
                continue  # a bare "requests.certs" or "curses" title
        summary = _paragraph_summary(para, own)
        if summary:
            return summary
    return ""


def _paragraphs(lines: list[str]):
    """Groups of lines, lazily. Blank lines and rulers ("------", "~~~~", "####") separate them."""
    current: list[str] = []
    for raw in lines:
        line = raw.strip()
        if any(ch.isalnum() for ch in line):
            current.append(line)
        elif current:
            yield current
            current = []
    if current:
        yield current


def _paragraph_summary(lines: list[str], own: str = "") -> str:
    """First sentence of a paragraph, "" if it has none worth showing.

    One-word lines at its top ("ATTENTION", "__init__.py", "LLMFunction") are passed over in a
    loop, so a paragraph of a thousand of them is no deeper than one. Whether the sentence goes
    on past a one-word line shows in that line and the next one, so the rest of the paragraph
    is joined only when it does. The rest is read from `start` on without copying it, and only
    as far as the sentence goes: lines that each say one word ("Word1 [](badge.svg)") cost one
    line each, not the whole paragraph each.
    """
    for start, head in enumerate(lines):
        if (head.startswith((".. ", ">>>", "!!!", "???")) or (head[:1] == ":" and not starts_with_role(head))
                or head.lower().rstrip(":") in _SECTION_TITLES):
            return ""  # reST directive, doctest, field list (":copyright: ..."), mkdocs box, "Args:"
        bullet = _BULLET.match(head)
        if bullet:
            text = _summary_text(head[bullet.end():], own)
        elif " " in head or len(lines) <= start + 2:
            text = _summary_text(join_wrapped(_rest(lines, start)), own)
        else:  # one word on the line: joined with the next line or not, as join_wrapped(lines[start:]) would
            pair = join_wrapped(lines[start:start + 2])
            # judged before the markup is taken out: "help.py:" + "downloads" + "files from ..." goes
            # on, although "Downloads" alone is one word
            text = _summary_text(join_wrapped(_rest(lines, start)) if " " in pair else pair, own)
        if not text or is_boilerplate(text):
            return ""
        if " " not in text.strip():  # one word: "ATTENTION", "__init__.py", "LLMFunction"
            if bullet:
                return ""
            continue
        if _URL.search(text) and len(_URL.sub(" ", text).split()) < 3:
            return ""  # only a link: "From: https://github.com/...", "Extracted from https://..."
        return _SENTENCE_END.split(text, maxsplit=1)[0][:300].strip()
    return ""


def _rest(lines: list[str], start: int):
    """lines[start:] without copying them: join_wrapped reads only as far as the sentence goes."""
    return lines if not start else map(lines.__getitem__, range(start, len(lines)))


def _summary_text(text: str, own: str) -> str:
    """A paragraph's text without markup, RCS keywords, a "name -- " start or a trailing lint pragma.

    Only the first _SUMMARY_SCAN characters are looked at: a summary shows 300, and some of these
    patterns take time that grows with the square of a long run (":a:a:a...", "[[[[...") they
    cannot match, so a 160 KB one-line docstring once took 40 s.
    """
    text = text[:_SUMMARY_SCAN]
    text = _BACKTICKS.sub(lambda m: m.group(1) or m.group(2), _RST_ROLE.sub(_role_text, text))
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # Markdown links
    text = " ".join(_RCS_KEYWORD.sub(" ", text).replace("**", "").split())
    named = _NAME_DASH.match(text)  # "codecontext - display the block ..." -> "Display the block ..."
    if not named and own and text[:len(own) + 3].lower() == own + ".py":
        named = _FILE_PREFIX.match(text)  # "help.py: Implement the Idle help menu" (keep "CodeVision: see ...")
    if named:
        text = text[named.end():]
        text = text[:1].upper() + text[1:]
    return _TRAILING_PRAGMA.sub("", text)


# ---------------------------------------------------------------- fallback for files with syntax errors

_RE_IMPORT = re.compile(r"^[ \t]*import[ \t]+([^\n#;]+)", re.M)
_RE_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)[ \t]*([\w.]*)[ \t]+import[ \t]+([^\n#;]+)", re.M)
_RE_DYNAMIC = re.compile(r"""(?:\bimport_module|\b__import__)\(\s*["']([.\w]+)["']""")
_RE_MAIN = re.compile(r"""^if\s+__name__\s*==\s*["']__main__["']\s*:""", re.M)
_RE_SERVER = re.compile(
    r"^[A-Za-z_]\w*[ \t]*(?::[^=\n]*)?=[ \t]*(?:[A-Za-z_]\w*\.)*(?:%s)[ \t]*\(" % "|".join(sorted(_APP_FACTORIES)),
    re.M,
)
_RE_DSN = re.compile(r"""["']([A-Za-z][\w+-]*:/{0,3}[^"'\s]{0,40})""")
_RE_DOCSTRING = re.compile(r"""\A(?:[ \t]*(?:#[^\n]*)?\n)*[ \t]*[rRuU]?(\"\"\"|'''|"|')""")


def _fill_from_text(facts: FileFacts, text: str) -> None:
    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    imports: list[ImportRef] = []
    for m in _RE_IMPORT.finditer(text):
        for name in _split_names(m.group(1)):
            if name != "*":
                imports.append(ImportRef(name, [], "import", 0, line_of(m.start())))
    for m in _RE_FROM.finditer(text):
        part = m.group(3)
        if "(" in part and ")" not in part:  # from x import (\n a,\n b)
            close = text.find(")", m.end())
            part += text[m.end(): close if close != -1 else None]
        imports.append(ImportRef(m.group(2), _split_names(part), "from", len(m.group(1)), line_of(m.start())))
    for m in _RE_DYNAMIC.finditer(text):
        spec = m.group(1)
        level = len(spec) - len(spec.lstrip("."))
        imports.append(ImportRef(spec[level:], [], "dynamic", level, line_of(m.start())))
    imports.sort(key=lambda imp: imp.line)

    facts.imports = imports
    facts.frameworks = {name for name in map(_framework_of, imports) if name}
    facts.frameworks.update(name for name in (_dsn_framework(m.group(1)) for m in _RE_DSN.finditer(text)) if name)
    if _RE_SERVER.search(text):
        facts.is_entry, facts.entry_reason = True, REASON_SERVER
    elif _RE_MAIN.search(text):
        facts.is_entry, facts.entry_reason = True, REASON_MAIN
    name = _own_name(facts.path)
    facts.module_doc = _regex_docstring(text, name) or _leading_comment(text, name)


def _split_names(part: str) -> list[str]:
    part = re.sub(r"#[^\n]*", "", part)
    names = []
    for piece in part.replace("(", " ").replace(")", " ").replace("\\", " ").split(","):
        words = piece.split()  # "a.b as c" -> "a.b"
        if words and (words[0] == "*" or re.fullmatch(r"[\w.]+", words[0])):
            names.append(words[0])
    return names


def _regex_docstring(text: str, name: str = "") -> str:
    match = _RE_DOCSTRING.match(text[:20_000])
    if not match:
        return ""
    end = text.find(match.group(1), match.end())
    if end == -1:  # unterminated: only the first line is surely part of it
        return _first_line(text[match.end(): match.end() + 2000])
    return _doc_summary(text[match.end():end], name)


# ---------------------------------------------------------------- resolve

_CACHES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _cache(index: ProjectIndex) -> dict:
    """Per-index memo for search folders and module lookups (big projects resolve thousands of imports)."""
    try:
        cache = _CACHES.get(index)
        if cache is None:
            cache = _CACHES[index] = {}
        return cache
    except TypeError:  # an index that cannot be weakly referenced
        return {}


def resolve(imp: ImportRef, importer: str, index: ProjectIndex) -> list[str]:
    """Project files an import points to; [] for the standard library and outside packages."""
    parts = [p for p in (imp.spec or "").split(".") if p]
    if imp.level > 0:
        base = parent_dir(importer)
        for _ in range(imp.level - 1):
            if not base:
                return []  # climbs above the project root
            base = parent_dir(base)
        found = _find_module(index, [base], parts)
    elif parts:
        if parts == ["__main__"]:
            return []  # the running script, not a file named __main__.py
        found = _find_absolute(index, parent_dir(importer), parts)
        if found is None and imp.kind == "dynamic" and len(parts) > 2 and parts[-1][:1].isupper():
            found = _find_absolute(index, parent_dir(importer), parts[:-1])  # "polls.apps.PollsConfig"
    else:
        return []

    if found is None:
        return []
    kind, path = found
    if kind == "file":
        out = [path]
    elif imp.kind == "dynamic" and imp.names and all(n.endswith("*") for n in imp.names):
        out = [f for n in imp.names for f in _submodules_like(index, path, n[:-1])]  # import_module(f".x{v}")
    else:
        names = imp.names if imp.kind == "from" else []
        out = [f for f in (_submodule_file(index, path, name) for name in names) if f]
        # names that are not submodules come from the package's __init__.py
        if kind == "package" and len(out) < max(len(names), 1):
            out.append(join_rel(path, "__init__.py"))
    return [p for p in dict.fromkeys(out) if p != importer]


def _find_absolute(index: ProjectIndex, folder: str, parts: list[str]) -> tuple[str, str] | None:
    """Absolute import from a file in `folder`: its ancestor folders (and src/), then the root package.

    When the analyzed folder is itself a package (the pip folder), `from pip._internal import x`
    names the root folder by its own name, which no folder inside the project has.
    """
    cache = _cache(index)
    key = ("abs", folder, tuple(parts))
    if key in cache:
        return cache[key]
    found = _find_module(index, _search_bases(folder, index), parts)
    if (found is None or found[0] == "namespace") and parts[0] == _root_package(index):
        own = _find_module(index, [""], parts[1:])
        if own is not None and (found is None or own[0] != "namespace"):
            found = own
    cache[key] = found
    return found


def _root_package(index: ProjectIndex) -> str | None:
    """Name of the analyzed folder when it can be a Python package name ("pip"), else None."""
    cache = _cache(index)
    if "root" not in cache:
        name = getattr(getattr(index, "root", None), "name", "") or ""
        cache["root"] = name if name.isidentifier() and not keyword.iskeyword(name) else None
    return cache["root"]


def _search_bases(folder: str, index: ProjectIndex) -> list[str]:
    """Ancestor folders of the importer, closest first, ending at the root; then their src/ folders."""
    cache = _cache(index)
    key = ("bases", folder)
    if key in cache:
        return cache[key]
    bases: list[str] = []
    while True:
        bases.append(folder)
        if not folder:
            break
        folder = parent_dir(folder)
    src = [join_rel(base, "src") for base in bases]
    bases += [s for s in src if index.is_dir(s) and s not in bases]
    cache[key] = bases
    return bases


def _find_module(index: ProjectIndex, bases: list[str], parts: list[str]) -> tuple[str, str] | None:
    """("file" | "package" | "namespace", path). A real module anywhere beats a bare folder."""
    namespace = None
    for base in bases:
        path = join_rel(base, *parts)
        if path is None:
            continue
        if parts and path + ".py" in index.files:
            return "file", path + ".py"
        if join_rel(path, "__init__.py") in index.files:
            return "package", path
        if namespace is None and index.is_dir(path):
            namespace = ("namespace", path)
    return namespace


def _submodules_like(index: ProjectIndex, folder: str, prefix: str) -> list[str]:
    """Modules of a package whose names start with prefix: unicode*.py and unicode*/__init__.py."""
    cache = _cache(index)
    if "children" not in cache:
        children: dict[str, list[str]] = {}  # folder -> its modules: x.py files and x/__init__.py packages
        for rel in index.files:
            if rel.endswith(".py"):
                children.setdefault(parent_dir(rel), []).append(rel)
                if rel.endswith("/__init__.py"):
                    children.setdefault(parent_dir(parent_dir(rel)), []).append(rel)
        cache["children"] = children
    out = []
    for rel in sorted(cache["children"].get(folder, [])):
        name = rel[len(folder) + 1:] if folder else rel
        head = name.split("/", 1)[0]
        stem = head[:-3] if head.endswith(".py") else head
        if stem.startswith(prefix) and stem != "__init__":
            out.append(rel)
    return out


def _submodule_file(index: ProjectIndex, folder: str, name: str) -> str | None:
    if name == "*":
        return None
    for candidate in (join_rel(folder, name + ".py"), join_rel(folder, name, "__init__.py")):
        if candidate in index.files:
            return candidate
    return None


# ---------------------------------------------------------------- project entries

_COMMAND_FILES = {
    "procfile", "dockerfile", "makefile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
}
_SERVER_CMD = re.compile(r"\b(?:uvicorn|gunicorn|hypercorn|daphne)\b")
_APP_TARGET = re.compile(r"""(?<![\w.:/-])["']?([A-Za-z_][\w.]*):[A-Za-z_]\w*""")  # main:app
_FILE_CMD = re.compile(r"""\b(?:streamlit\s+run|fastapi\s+(?:run|dev))\s+["']?([\w./\\-]+\.py)\b""")
_CD = re.compile(r"""\bcd\s+["']?([\w./\\-]+)""")
_APP_DIR = re.compile(r"""--(?:app-dir|chdir)[=\s]+["']?([\w./\\-]+)""")
_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def project_entries(index: ProjectIndex) -> dict[str, str]:
    """Start files known from project files: pyproject scripts, manage.py, server commands."""
    scripts: dict[str, str] = {}
    django: dict[str, str] = {}
    commands: dict[str, str] = {}
    for rel in sorted(index.files):
        name = rel.rsplit("/", 1)[-1]
        try:
            if name == "pyproject.toml":
                _pyproject_entries(index, rel, scripts)
            elif name == "manage.py":
                django[rel] = REASON_DJANGO
            elif _is_command_file(name.lower()):
                _command_entries(index, rel, commands)
        except Exception:  # one broken config file must not stop the others
            continue
    out: dict[str, str] = {}
    for found in (scripts, django, commands):
        for rel, reason in found.items():
            out.setdefault(rel, reason)
    return out


def _is_command_file(low: str) -> bool:
    return (low in _COMMAND_FILES
            or low.startswith(("readme", "procfile", "dockerfile."))
            or low.endswith((".dockerfile", ".sh", ".bat", ".cmd", ".ps1")))


def _pyproject_entries(index: ProjectIndex, rel: str, out: dict[str, str]) -> None:
    text = index.read_text(rel)
    if not text or tomllib is None:
        return
    data = tomllib.loads(text)
    project, tool = _table(data, "project"), _table(data, "tool")
    targets = [
        *_table(project, "scripts").values(),
        *_table(project, "gui-scripts").values(),
        *_table(_table(tool, "poetry"), "scripts").values(),
    ]
    folder = parent_dir(rel)
    for target in targets:
        if isinstance(target, str):  # "pkg.mod:func"
            path = _module_file(index, [folder, join_rel(folder, "src")], target.split(":", 1)[0].strip())
            if path:
                out.setdefault(path, REASON_SCRIPT)


def _table(data, key: str) -> dict:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _command_entries(index: ProjectIndex, rel: str, out: dict[str, str]) -> None:
    text = index.read_text(rel)
    if not text:
        return
    folder = parent_dir(rel)
    for line in text.splitlines():
        cmd = _SERVER_CMD.search(line)
        target = _APP_TARGET.search(line, cmd.end()) if cmd else None
        if target:  # uvicorn main:app, gunicorn app.wsgi:application
            module = target.group(1)
            path = (_module_file(index, _command_bases(folder, line, cmd.start()), module)
                    or _unique_file(index, module.replace(".", "/") + ".py"))
            if path:
                out.setdefault(path, REASON_COMMAND)
        run = _FILE_CMD.search(line)
        if run:  # streamlit run app.py, fastapi dev main.py
            file = join_rel("", run.group(1))
            if file:
                path = _first_file(index, _command_bases(folder, line, run.start()), [file]) or _unique_file(index, file)
                if path:
                    out.setdefault(path, REASON_COMMAND)


def _command_bases(folder: str, line: str, start: int) -> list[str | None]:
    """Folders a command runs from: `cd x` before it, --app-dir/--chdir, the file's folder, src/."""
    subs = _CD.findall(line[:start])[-1:] + _APP_DIR.findall(line[start:])[:1]
    return [join_rel(folder, s.replace("\\", "/")) for s in subs] + [folder, join_rel(folder, "src")]


def _module_file(index: ProjectIndex, bases: list[str | None], dotted: str) -> str | None:
    if not _MODULE_NAME.fullmatch(dotted):
        return None
    rel = dotted.replace(".", "/")
    return _first_file(index, bases, [rel + ".py", rel + "/__init__.py"])


def _first_file(index: ProjectIndex, bases: list[str | None], rels: list[str]) -> str | None:
    for base in bases:
        if base is None:
            continue
        for rel in rels:
            candidate = join_rel(base, rel)
            if candidate and candidate in index.files:
                return candidate
    return None


def _unique_file(index: ProjectIndex, rel: str) -> str | None:
    """The only project file whose path ends with rel (used when the run folder is unknown)."""
    hits = [f for f in index.files if f == rel or f.endswith("/" + rel)]
    return hits[0] if len(hits) == 1 else None
