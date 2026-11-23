"""Korean plain-language summaries made without AI (docs/architecture.md section 5).

``s`` is one short line (about 24 characters), ``d`` is one or two sentences.
Wording is polite 해요체 that a non-developer can follow.
"""
from __future__ import annotations

import builtins
import re

from .model import FileFacts
from .scan import ARCHIVE_EXTS, COMPILED_EXTS, DATABASE_EXTS, DOCUMENT_EXTS, FONT_EXTS, IMAGE_EXTS, MEDIA_EXTS

# Canonical framework name -> display name, in stack-strip order.
UI_FRAMEWORKS = {"react": "React", "vue": "Vue", "svelte": "Svelte", "next": "Next.js", "nuxt": "Nuxt",
                 "sveltekit": "SvelteKit", "angular": "Angular"}
# Single-file components: the framework a .vue or .svelte file is written for.
SFC_TECH = {".vue": "Vue", ".svelte": "Svelte"}
SERVER_FRAMEWORKS = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "express": "Express",
    "nestjs": "NestJS", "koa": "Koa", "fastify": "Fastify", "hono": "Hono",
    "http.server": "http.server",  # Python's own small web server (standard library)
}
DB_FRAMEWORKS = {
    "sqlite": "SQLite", "postgres": "PostgreSQL", "mysql": "MySQL", "mongodb": "MongoDB",
    "prisma": "Prisma", "supabase": "Supabase", "firebase": "Firebase", "redis": "Redis",
}
WEB_SERVERS = frozenset(SERVER_FRAMEWORKS) | {"starlette", "aiohttp"}
LANGUAGE_NAMES = {"python": "Python", "javascript": "JavaScript", "typescript": "TypeScript"}

CODE_LANGUAGES = frozenset(LANGUAGE_NAMES)
DOC_EXTS = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc"})
STYLE_EXTS = frozenset({".css", ".scss", ".sass", ".less", ".styl"})
HTML_EXTS = frozenset({".html", ".htm"})
CONFIG_EXTS = frozenset({".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".lock", ".properties"})

_LOCK = ("라이브러리 버전 기록", "설치된 라이브러리의 정확한 버전이 자동으로 기록돼요. 직접 고칠 일은 거의 없어요.")
_REQS = ("필요한 라이브러리 목록", "이 Python 프로그램을 실행하려면 설치해야 하는 라이브러리 목록이에요.")
_COMPOSE = ("도커 묶음 실행 설정", "여러 프로그램을 도커(Docker)로 한꺼번에 띄우는 방법이 적혀 있어요.")
_VERSION = ("사용할 버전 지정", "이 프로젝트에 맞는 Node.js나 Python 버전이 적혀 있어요.")
CONFIG_FILES = {
    "package.json": ("라이브러리·실행 설정", "사용하는 라이브러리 목록과 실행 명령(npm run …)이 적혀 있어요."),
    "package-lock.json": _LOCK, "yarn.lock": _LOCK, "pnpm-lock.yaml": _LOCK, "poetry.lock": _LOCK,
    "pipfile.lock": _LOCK, "bun.lockb": _LOCK, "uv.lock": _LOCK,
    "requirements.txt": _REQS, "pipfile": _REQS,
    "pyproject.toml": ("Python 프로젝트 설정", "프로젝트 이름, 필요한 라이브러리, 도구 설정이 적혀 있어요."),
    "setup.py": ("설치 설정", "이 Python 프로젝트를 설치하는 방법을 정해요."),
    "setup.cfg": ("설치·도구 설정", "Python 프로젝트 설치 방법과 도구 설정이 적혀 있어요."),
    "tsconfig.json": ("TypeScript 설정", "TypeScript 코드를 어떻게 검사하고 변환할지 정해요."),
    "jsconfig.json": ("JavaScript 설정", "편집기와 도구가 JavaScript 코드를 어떻게 읽을지 정해요."),
    "dockerfile": ("도커 실행 설정", "이 프로젝트를 도커(Docker) 컨테이너로 만들어 실행하는 방법이 적혀 있어요."),
    "docker-compose.yml": _COMPOSE, "docker-compose.yaml": _COMPOSE, "compose.yml": _COMPOSE, "compose.yaml": _COMPOSE,
    ".gitignore": ("깃 제외 목록", "깃(Git)에 올리지 않을 파일과 폴더를 적어 두는 곳이에요."),
    ".gitattributes": ("깃 파일 처리 설정", "깃(Git)이 파일을 어떻게 다룰지 정해요."),
    ".dockerignore": ("도커 제외 목록", "도커(Docker)로 묶을 때 뺄 파일과 폴더를 적어 두는 곳이에요."),
    ".editorconfig": ("편집기 설정", "들여쓰기 같은 코드 편집 규칙을 정해요."),
    ".npmrc": ("npm 설정", "라이브러리를 내려받는 도구(npm)의 설정이에요."),
    ".nvmrc": _VERSION, ".python-version": _VERSION, ".node-version": _VERSION,
    ".gitkeep": ("빈 폴더 표시", "빈 폴더를 깃(Git)에 남겨 두려고 넣은 빈 파일이에요."),
    ".flake8": ("코드 검사 설정", "Python 코드 검사 도구(flake8)의 규칙이 적혀 있어요."),
    "makefile": ("자주 쓰는 명령 모음", "make 명령으로 실행할 작업들이 적혀 있어요."),
    "procfile": ("서버 실행 명령", "배포 서비스가 서버를 어떻게 켤지 적어 둔 파일이에요."),
}
TOOL_NAMES = {
    "vite": "Vite", "next": "Next.js", "nuxt": "Nuxt", "tailwind": "Tailwind", "postcss": "PostCSS",
    "eslint": "ESLint", "prettier": "Prettier", "babel": "Babel", "jest": "Jest", "vitest": "Vitest",
    "webpack": "webpack", "svelte": "Svelte", "astro": "Astro", "playwright": "Playwright", "rollup": "Rollup",
}

FOLDERS: dict[str, tuple[str, str]] = {}
# (s, d) for a code file inside such a folder, when nothing more specific is known about it.
FILE_ROLES: dict[str, tuple[str, str]] = {}


def _folders(names: str, s: str, d: str, file: tuple[str, str] | None = None) -> None:
    for name in names.split():
        FOLDERS[name] = (s, d)
        if file:
            FILE_ROLES[name] = file


_folders("components component", "화면 부품 모음", "버튼, 목록처럼 여러 번 다시 쓰는 화면 조각을 모아 둔 곳이에요.",
         file=("화면 부품 코드", "버튼, 목록처럼 여러 번 다시 쓰는 화면 조각(부품)과 관련된 코드예요."))
_folders("pages screens", "화면 페이지 모음", "주소마다 보여 줄 화면 페이지가 들어 있어요.",
         file=("화면 페이지 코드", "주소마다 보여 줄 화면 페이지와 관련된 코드예요."))
_folders("routes router routers", "주소별 요청 처리", "어떤 주소로 요청이 오면 무엇을 할지 정해 둔 곳이에요.",
         file=("주소별 요청 처리", "어떤 주소로 요청이 오면 무엇을 할지 정해 두는 코드예요."))
_folders("api apis endpoints", "서버와 주고받는 코드", "서버와 데이터를 주고받는 코드가 모여 있어요.",
         file=("서버와 주고받는 코드", "서버와 데이터를 주고받는 코드예요."))
_folders("utils util helpers helper common shared", "도우미 코드 모음", "여러 곳에서 같이 쓰는 작은 기능들을 모아 둔 곳이에요.",
         file=("도우미 함수 모음", "여러 곳에서 같이 쓰는 작은 기능들을 모아 둔 파일이에요."))
_folders("lib libs", "공용 코드 모음", "여러 곳에서 불러 쓰는 공용 코드가 모여 있어요.",
         file=("도우미 함수 모음", "여러 곳에서 같이 쓰는 작은 기능들을 모아 둔 파일이에요."))
_folders("hooks composables", "화면 동작 도우미(훅)", "여러 화면 조각이 같이 쓰는 동작을 모아 둔 곳이에요.",
         file=("화면 동작 도우미(훅)", "여러 화면 조각이 같이 쓰는 동작을 담은 코드예요."))
_folders("models model entities", "데이터 모양 정의", "저장하고 주고받는 데이터가 어떤 항목을 갖는지 정해 둔 곳이에요.")
_folders("schemas schema", "데이터 형식 정의", "주고받는 데이터의 형식과 검사 규칙이 모여 있어요.")
_folders("services service", "핵심 기능 처리", "실제 일을 처리하는 코드(서비스)가 모여 있어요.",
         file=("핵심 기능 처리", "실제 일을 처리하는 코드(서비스)예요."))
_folders("store stores state redux context contexts", "화면 상태 저장소", "여러 화면이 같이 쓰는 데이터(상태)를 보관해요.",
         file=("화면 상태 저장소", "여러 화면이 같이 쓰는 데이터(상태)를 보관해요."))
_folders("styles style css scss", "화면 꾸미기(CSS)", "색, 글꼴, 배치처럼 화면 모양을 정하는 파일이 모여 있어요.",
         file=("화면 꾸미기 코드", "색, 글꼴, 배치처럼 화면 모양을 정하는 코드예요."))
_folders("assets", "그림·글꼴 같은 자료", "그림, 아이콘, 글꼴처럼 화면에 쓰는 자료 파일이 모여 있어요.")
_folders("images img icons", "이미지 모음", "화면에 쓰는 그림과 아이콘이 모여 있어요.")
_folders("public static", "그대로 내보내는 파일", "그림이나 아이콘처럼 브라우저에 그대로 전달되는 파일이 들어 있어요.")
_folders("screenshots screenshot", "화면 캡처 모음", "프로그램 화면을 찍어 둔 그림이 모여 있어요.")
_folders("design designs mockup mockups wireframe wireframes", "디자인 자료 모음",
         "화면을 어떻게 만들지 정할 때 쓰는 시안과 참고 자료가 모여 있어요.")
_folders("tests test __tests__ spec e2e", "테스트 코드", "코드가 제대로 동작하는지 자동으로 확인하는 코드가 모여 있어요.")
_folders("fixtures mocks __mocks__", "테스트용 예시 데이터", "테스트할 때 쓰는 가짜 데이터가 모여 있어요.",
         file=("테스트용 예시 데이터", "테스트할 때 쓰는 가짜 데이터나 코드예요."))
_folders("docs doc", "설명 문서 모음", "프로젝트를 설명하는 문서가 모여 있어요.",
         file=("설명 문서에 딸린 코드", "설명 문서와 함께 둔 코드예요."))
_folders("scripts bin tools", "작업용 스크립트", "설치, 배포처럼 반복하는 일을 자동으로 해 주는 스크립트가 모여 있어요.",
         file=("작업용 스크립트", "반복하는 일을 자동으로 해 주는 스크립트예요."))
_folders("config configs settings conf", "설정 모음", "프로그램이 읽는 설정값이 모여 있어요.",
         file=("설정 코드", "프로그램이 쓰는 설정값을 다루는 코드예요."))
_folders("src source", "소스 코드", "실제 프로그램 코드가 모여 있어요.")
_folders("app", "앱 본체", "앱의 핵심 코드가 모여 있어요.")
_folders("server backend", "데이터 처리 서버", "화면에서 온 요청을 받아 처리하고 데이터를 저장하는 서버 쪽 코드예요.",
         file=("서버 쪽 코드", "서버 쪽 프로그램의 코드예요."))
_folders("client frontend web ui", "사용자가 보는 화면", "사용자가 브라우저에서 보는 화면 쪽 코드예요.",
         file=("화면 쪽 코드", "사용자가 보는 화면 쪽 프로그램의 코드예요."))
_folders("migrations", "DB 구조 변경 기록", "데이터베이스 표 구조를 바꾼 기록이 차례대로 쌓여 있어요.",
         file=("DB 구조 변경 기록", "데이터베이스 표 구조를 바꾼 기록 하나예요."))
_folders("alembic", "DB 구조 변경 기록", "데이터베이스 표 구조를 바꾼 기록이 차례대로 쌓여 있어요.")
_folders("templates template", "화면 틀(템플릿)", "서버가 내용을 채워서 보내는 HTML 틀이 모여 있어요.")
_folders("types typings interfaces @types", "데이터 타입 정의", "코드에서 쓰는 데이터의 타입(모양)을 정해 둔 곳이에요.",
         file=("데이터 타입 정의", "코드에서 쓰는 데이터의 타입(모양)을 정해요."))
_folders("middleware middlewares", "요청 중간 처리", "요청이 오면 본 처리 전에 검사하거나 기록하는 코드가 모여 있어요.",
         file=("요청 중간 처리", "요청이 오면 본 처리 전에 검사하거나 기록하는 코드예요."))
_folders("controllers controller handlers", "요청 처리 담당", "요청을 받아서 어떤 일을 할지 결정하는 코드가 모여 있어요.",
         file=("요청 처리 담당", "요청을 받아서 어떤 일을 할지 결정하는 코드예요."))
_folders("views view", "화면 담당 코드", "사용자에게 보여 줄 화면이나 응답을 만드는 코드가 모여 있어요.",
         file=("화면 담당 코드", "사용자에게 보여 줄 화면이나 응답을 만드는 코드예요."))
_folders("layouts layout", "화면 배치 틀", "여러 페이지가 같이 쓰는 화면 배치가 모여 있어요.",
         file=("화면 배치 틀", "여러 페이지가 같이 쓰는 화면 배치를 정하는 코드예요."))
_folders("db database", "데이터베이스 관련 코드", "데이터베이스 연결과 저장에 관련된 코드가 모여 있어요.",
         file=("데이터베이스 관련 코드", "데이터베이스 연결과 저장에 관련된 코드예요."))
_folders("data", "데이터 파일", "프로그램이 읽고 쓰는 데이터가 모여 있어요.",
         file=("데이터 관련 코드", "프로그램이 쓰는 데이터와 관련된 코드예요."))
_folders("prisma", "DB 설계(Prisma)", "Prisma로 정의한 데이터베이스 구조와 변경 기록이 들어 있어요.")
_folders("auth", "로그인·권한 처리", "로그인과 접근 권한을 다루는 코드가 모여 있어요.",
         file=("로그인·권한 처리", "로그인과 접근 권한을 다루는 코드예요."))
_folders("core", "핵심 코드", "프로그램의 중심이 되는 코드가 모여 있어요.",
         file=("핵심 코드", "프로그램의 중심이 되는 코드예요."))
_folders("features modules", "기능별 코드 묶음", "기능마다 필요한 코드를 한데 묶어 둔 곳이에요.",
         file=("기능별 코드", "기능 하나에 필요한 코드예요."))
_folders("plugins", "추가 기능(플러그인)", "기본 기능에 덧붙이는 플러그인이 모여 있어요.",
         file=("추가 기능(플러그인)", "기본 기능에 덧붙이는 플러그인과 관련된 코드예요."))
_folders("locales i18n translations", "여러 언어 문구", "화면에 보이는 문구를 언어별로 모아 둔 곳이에요.",
         file=("언어별 문구", "화면에 보이는 문구를 언어별로 정리한 코드예요."))
_folders("examples example demo samples", "예제 모음", "사용법을 보여 주는 예제가 모여 있어요.",
         file=("예제 코드", "사용법을 보여 주는 예제예요."))
_folders("vendor vendors vendored third_party third-party thirdparty", "외부에서 가져온 코드",
         "다른 곳에서 가져와 그대로 넣어 둔 코드예요.", file=("외부에서 가져온 코드", "다른 곳에서 가져와 그대로 넣어 둔 코드예요."))
_folders(".github", "GitHub 설정", "GitHub에서 쓰는 설정과 자동 작업이 들어 있어요.")
_folders("workflows", "자동 작업 설정", "코드를 올릴 때마다 자동으로 실행할 작업이 적혀 있어요.")
_folders("cli", "명령어 실행 부분", "터미널에서 명령어로 실행할 때 쓰는 코드가 모여 있어요.",
         file=("명령어 실행 부분", "터미널에서 명령어로 실행할 때 쓰는 코드예요."))
_folders("commands", "명령어 모음", "명령어마다 할 일을 정해 둔 코드가 모여 있어요.",
         file=("{name} 명령", "{name} 명령이 할 일을 정해 둔 코드예요."))  # commands/install.py
_folders("internal", "내부 코드", "바깥에 내보이지 않고 안에서만 쓰는 코드가 모여 있어요.",
         file=("내부 코드", "바깥에 내보이지 않고 안에서만 쓰는 코드예요."))
_folders("exceptions errors", "오류 종류 정의", "프로그램에서 생길 수 있는 오류의 종류를 정해 둔 곳이에요.",
         file=("오류 종류 정의", "프로그램에서 생길 수 있는 오류의 종류를 정해 둔 코드예요."))
_folders("network net", "네트워크 통신", "인터넷으로 데이터를 주고받는 코드가 모여 있어요.",
         file=("네트워크 통신", "인터넷으로 데이터를 주고받는 코드예요."))
_folders("tasks jobs workers", "작업 처리 코드", "시간이 걸리는 일을 따로 맡아서 처리하는 코드가 모여 있어요.",
         file=("작업 처리 코드", "시간이 걸리는 일을 따로 맡아서 처리하는 코드예요."))
_folders("contrib extras", "덧붙인 기능", "기본 기능에 더해 골라 쓰는 추가 기능이 모여 있어요.",
         file=("덧붙인 기능", "기본 기능에 더해 골라 쓰는 추가 기능이에요."))
_folders("vcs", "버전 관리 도구 연결", "Git 같은 버전 관리 도구를 다루는 코드가 모여 있어요.",
         file=("버전 관리 도구 연결", "Git 같은 버전 관리 도구를 다루는 코드예요."))

# Names that mean screen code only in a screen project ("styles" in a Python project is not CSS).
_UI_ONLY = frozenset({
    "components", "component", "pages", "screens", "hooks", "composables", "store", "stores", "state",
    "redux", "context", "contexts", "styles", "style", "css", "scss", "layouts", "layout",
})
_UI_SIDE = frozenset({"client", "frontend", "web", "ui"})
_SV_SIDE = frozenset({"server", "backend"})
# Design and picture folders hold no program code (a "design" package of Python code is something else,
# and so is a src/design/ whose Button.tsx the app imports: describe_folder's `used_outside`).
NOT_CODE_FOLDERS = frozenset({"screenshots", "screenshot", "design", "designs", "mockup", "mockups", "wireframe",
                              "wireframes"})
# File roles that say little more than where the file is: used only when nothing else is known,
# and only for the folder right above the file.
_WEAK_ROLES = frozenset({"core", "features", "modules", "internal"}) | _UI_SIDE | _SV_SIDE
_VENDOR_DIRS = frozenset({"vendor", "vendors", "vendored", "third_party", "third-party", "thirdparty"})
# A file named like one of these folders has that role too (auth.py, views.py, routes.js, cli.py).
_NAME_ROLES = frozenset({"routes", "router", "routers", "services", "service", "views", "auth", "plugins", "cli",
                         "network", "controllers", "controller", "examples", "example", "demo", "hooks", "styles"})
# Stems in a commands folder that are not one command (commands/base.py, commands/utils.py).
_NOT_COMMANDS = frozenset({
    "base", "common", "core", "utils", "util", "helpers", "helper", "abstract", "mixin", "mixins", "registry",
    "types", "typing", "constants", "const", "errors", "exceptions", "options", "parser", "decorators", "shared",
    "compat", "interface", "interfaces", "protocol", "protocols", "misc", "main", "command", "commands", "cmd",
    "context", "config", "settings",
})
# Images that sit in these folders are the screen's own pictures even when no import shows it.
_ASSET_DIRS = frozenset({"assets", "static", "public", "images", "img", "icons"})
_OTHER_CODE = {
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".c": "C", ".cpp": "C++", ".h": "C", ".swift": "Swift", ".dart": "Dart",
}
_METHOD_WORDS = (("GET", "조회"), ("POST", "추가"), ("PUT", "수정"), ("PATCH", "수정"), ("DELETE", "삭제"))
_HANGUL = re.compile(r"[가-힣]")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# License and copyright headers say nothing about what a file does.
_BOILERPLATE = re.compile(
    r"(?i)\bspdx-|\bcopyright\b|(?:\(c\)|©)\s*\d{4}|\blicen[cs]ed\b|\blicen[cs]es\b|all rights reserved"
    r"|hereby granted|\bwarrant(?:y|ies)\b|this file is part of\b|this source code form"
    r"|\bsee (?:the )?(?:accompanying )?licen[cs]e|\blicen[cs]e,? version\b|https?://\S*licen[cs]e"
    r"|\b(?:apache|mit|bsd|gpl|lgpl|mpl)(?:-[\d.]+)? licen[cs]e\b|^@(?:license|preserve|copyright)\b"
)
# "Generated by the protocol buffer compiler. DO NOT EDIT!", "Automatically generated by ..."
_GENERATED = re.compile(r"(?i)\b(?:auto-?generated|(?:automatically|code|file) generated|generated (?:by|from|with|code))\b|do not edit")


_SENTENCE_BREAK = re.compile(r"(?<=[.!?。])\s+(?=[A-Z0-9가-힣\"'(\[])")
_LIST_ITEM = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")


def is_boilerplate(text: str) -> bool:
    """True for a license or copyright line, which is useless as a summary."""
    return bool(_BOILERPLATE.search(text or ""))


# Last words of a line that only leads into what follows it ("Spinners are from:", "... such as:").
_LEAD_WORDS = frozenset("""
    a an the this these those that which as at by for from in into of on onto to via with without and or nor
    is are was were be been has have include includes including contain contains containing follow follows
    following below above like namely e.g i.e eg ie such example examples usage note notes see means
""".split())
# "..., such as:" closing a summary whose words before it still say something.
_LEAD_TAIL = re.compile(r"(?i)(,?)\s*\b(?:such as|like|as follows|as below|for example|for instance|e\.g\.?|i\.e\.?"
                        r"|namely|including|see(?: also)?)\s*:+$")
_WORD = re.compile(r"\w+(?:\.\w+)*")
# The address part of a reST link ("XDG Basedir Spec <https://...>"): the words before it say enough.
_LINK_TARGET = re.compile(r"\s*<(?:https?|ftp)://[^<>\s]*>")
# What is left of a Sphinx reference ":class:`~rich.console.Console`": Sphinx itself shows "Console".
_SHORT_REF = re.compile(r"(?<![\w~/])~(?:[A-Za-z_]\w*\.)+([A-Za-z_]\w*)")
# A short line that points at what comes after it ("Run the following command:", "다음 기능을 제공해요:").
_POINTS_AHEAD = re.compile(r"(?i)\bthe following\b|\bbelow\b|\bas follows\b|다음|아래")
# Words a summary cannot end with: the analyzer stopped where the next docstring line began with markup
# ("Base class for all connection pools, such as" + ":class:`.HTTPConnectionPool`", "..., e.g." + "'10/1000'").
# Prepositions are left alone ("all auth implementations derive from" is a whole sentence), and so is
# "a" (often a variable: "the square of a").
_CUT_TAIL = re.compile(
    r"(?:[\s,;(]*\b(?:such as|as well as|including|namely|e\.g\.?|i\.e\.?)|,\s*like"
    r"|(?<!logical)(?<!bitwise)(?<!boolean)\s+(?:and|or|nor|but)"
    r"|(?:\s+(?:of|to|with|by|for|from|via|into|in|on|at|as))?\s+(?:the|an))$")
# The last words of every _CUT_TAIL match. A summary that ends with none of them (most end with
# ".") is not searched: the search tries every position of the text and is the slow part here.
# (_doc_text passes one line, so "$" cannot match before a newline instead.)
_CUT_ENDS = ("such as", "as well as", "including", "namely", "e.g", "e.g.", "i.e", "i.e.", "like", "and", "or",
             "nor", "but", "the", "an")


def _without_cut_tail(t: str) -> str:
    cut = t
    while cut.endswith(_CUT_ENDS) and (tail := _CUT_TAIL.search(cut)) is not None:
        cut = cut[:tail.start()].rstrip(" ,;(")
    if cut == t:
        return t
    return cut if len(_WORD.findall(cut)) >= 3 else ""


def _doc_text(text: str) -> str:
    """A docstring summary ready to show, or "" when it only leads into what follows it.

    "Spinners are from:", "Download packages from:", "From RFC7231:", "Usage:" and "Run the
    following command:" lead into a list or a quote that the summary does not include, so they say
    nothing. "For PsySH console output, such as:" keeps the words before "such as", and so does a
    summary cut off before its examples ("Base class for all connection pools, such as").
    "Format text with some attributes::" (a reST code example follows), "A plugin manager with
    extra functionality:" and a long sentence that ends with "as follows:" still say something:
    only the closing ":" goes.
    """
    if not text:  # most functions have no docstring
        return ""
    t = " ".join(text.split())
    if "<" in t:  # each pattern needs its first character: most summaries have neither
        t = _LINK_TARGET.sub("", t)
    if "~" in t:
        t = _SHORT_REF.sub(r"\1", t)
    t = t.strip()
    if not t.endswith(":"):
        return _without_cut_tail(t)
    words = _WORD.findall(t.lower())
    tail = _LEAD_TAIL.search(t)
    if tail and (tail.group(1) or len(words) < 10):  # after a comma, the words before are a whole phrase
        head = t[:tail.start()].rstrip(" ,;:")
        head_words = _WORD.findall(head.lower())
        if len(head_words) >= 3 and head_words[-1] not in _LEAD_WORDS:
            return head
    if len(words) <= 2 or (len(words) < 10 and (words[-1] in _LEAD_WORDS or _POINTS_AHEAD.search(t))):
        return ""
    return t.rstrip(":").rstrip()


def is_lead_in(text: str) -> bool:
    """True for a summary that ends with ":" and says nothing by itself ("Spinners are from:")."""
    t = " ".join((text or "").split())
    return t.endswith(":") and not _doc_text(t)


# A Sphinx role at the start of a line (":class:`.HTTPConnectionPool` and ..."): running text. A field
# list (":param x:", ":returns:", ":raises ValueError:") has no backtick right after its name.
_ROLE_START = re.compile(r":[\w.+-]+(?::[\w.+-]+)*:`")


def starts_with_role(line: str) -> bool:
    """True for a line that starts with a reST role such as :class:`Foo`, which is no field list."""
    return bool(_ROLE_START.match(line))


def join_wrapped(lines) -> str:
    """First sentence of a paragraph whose lines were wrapped by hand.

    "The distro package provides" + "information about ..." are joined; a line that starts
    like a new sentence (a capital letter) or a list item is not. A line that starts with a
    Sphinx role goes on with the sentence ("..., such as" + ":class:`.HTTPConnectionPool` and").
    Used by the analyzers for docstrings and comments. `lines` is read only as far as the
    sentence goes, and each line is looked at once, so a paragraph of 10,000 wrapped lines
    takes no longer than its text.
    """
    parts: list[str] = []
    for raw in lines:
        nxt = raw.strip()
        if not nxt:
            continue
        if parts:
            last = parts[-1]  # a sentence that ends before `nxt` can only end in the line just joined
            if _SENTENCE_BREAK.search(last + " X") or _LIST_ITEM.match(nxt):
                break
            first = nxt[:1]
            wrapped = first.islower() or first.isdigit() or first in "([{\"'`" or (first == ":" and starts_with_role(nxt))
            if not (wrapped or (_HANGUL.match(last[-1:]) and _HANGUL.match(first))):
                break
        parts.append(nxt)
    return _SENTENCE_BREAK.split(" ".join(parts), maxsplit=1)[0].strip()


# ---------------------------------------------------------------- file kinds

def base_name(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def extension(rel: str) -> str:
    name = base_name(rel)
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _stem(rel: str) -> str:
    return base_name(rel).split(".")[0]


def _folder_parts(rel: str) -> list[str]:
    return [p.lower() for p in rel.split("/")[:-1]]


def is_config(rel: str) -> bool:
    name = base_name(rel).lower()
    if name in CONFIG_FILES or name.startswith((".env", "dockerfile", "tsconfig.")):
        return True
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    if ".config." in name or (name.startswith(".") and ("rc." in name or name.endswith("rc"))):
        return True
    return extension(rel) in CONFIG_EXTS


def is_test(rel: str) -> bool:
    name = base_name(rel).lower()
    if any(p in ("tests", "test", "__tests__", "spec", "e2e") for p in _folder_parts(rel)):
        return True
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")
                                 or name in ("tests.py", "test.py")):  # Django's tests.py
        return True
    return ".test." in name or ".spec." in name


def is_doc(rel: str) -> bool:
    stem = _stem(rel).lower()
    return extension(rel) in DOC_EXTS or stem in ("readme", "license", "licence", "changelog", "contributing", "authors")


def is_image(rel: str) -> bool:
    return extension(rel) in IMAGE_EXTS


# ---------------------------------------------------------------- text helpers

_LAST_PAREN = re.compile(r"\s*[(\[（][^)\]）]*[)\]）]\s*$")  # "Foo bar (see baz)" -> "Foo bar"
_NAME_COLON = re.compile(r"^(\w.*?)\s*(?::|\s[-–—]{1,2})\s")  # "Graph builder: scan ..."


def short(text: str, limit: int | None = None) -> str:
    """Squeeze text into one short line.

    Prefers a whole first sentence, then the part before a "name: details" colon or dash,
    and only then cuts at a word boundary with "…".
    """
    t = " ".join(text.split()).rstrip(" .。")
    if limit is None:
        limit = 24 if _HANGUL.search(t) else 40
    if len(t) <= limit:
        return t
    first = _first_sentence(t).rstrip(" .。")
    if len(first) <= limit:
        return first
    no_paren = _LAST_PAREN.sub("", first)
    if no_paren and len(no_paren) <= limit:
        return no_paren
    # "Graph builder: scan + analyzers -> ...", but not "Important: don't ..." (one word says nothing)
    head = _NAME_COLON.match(first)
    if head and len(head.group(1)) <= limit and " " in head.group(1):
        return head.group(1).rstrip(" ,·:;-")
    cut = first[:limit]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    cut = cut.rstrip(" ,·:;-.(")
    return (cut or first[:limit]) + "…"


def sentence(text: str, limit: int = 140) -> str:
    t = " ".join(text.split())
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    elif t and t[-1] not in ".!?。…":
        t += "."
    return t


def _first_sentence(text: str) -> str:
    # The next sentence must start like one, so "e.g. this" or "v1. x" is not cut.
    return _SENTENCE_BREAK.split(text, maxsplit=1)[0]


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def connections(uses: int, used_by: int) -> str:
    if uses and used_by:
        return f"다른 파일 {uses:,}개를 불러 쓰고, {used_by:,}개 파일이 이 파일을 써요."
    if uses:
        return f"다른 파일 {uses:,}개를 불러 써요."
    if used_by:
        return f"{used_by:,}개 파일이 이 파일을 써요."
    return ""


def counts(files: int, folders: int) -> str:
    parts = [f"파일 {files:,}개"] * bool(files) + [f"폴더 {folders:,}개"] * bool(folders)
    return f"{', '.join(parts)}가 들어 있어요." if parts else "비어 있는 폴더예요."


def _paths(items) -> str:
    seen: list[str] = []
    for item in items:
        if not item.path:
            continue
        path = re.sub(r"(/\*)+$", "", item.path) or "/"
        if path not in seen:
            seen.append(path)
    text = ", ".join(seen[:3])
    return text + " 등" if len(seen) > 3 else text


def _methods(items) -> str:
    words: list[str] = []
    for method, word in _METHOD_WORDS:
        if word not in words and any(item.method == method for item in items):
            words.append(word)
    return "·".join(words) + " " if words else ""


def readme_line(text: str) -> str:
    """First plain paragraph line of a README (no headings, badges, lists or code).

    A line like "Install it with:" only leads into the block below it (a command, a list), so
    that line and that block are both passed over.
    """
    in_code = False
    lead = ""  # "": none; "next": the block a lead-in line introduces is coming; "in": inside it
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_code = not in_code
            lead = ""  # the fenced block was the one the lead-in introduced
            continue
        if in_code:
            continue
        if not line:
            lead = "" if lead == "in" else lead
            continue
        if lead:
            lead = "in"
            continue
        if line.startswith(("#", "<", "|", "![", "[!", "..", ":")):
            continue
        if re.match(r"^([-*+]\s|\d+[.)]\s|[=\-~^*_]{3,}$)", line):
            continue
        line = _MD_LINK.sub(r"\1", line.lstrip("> "))
        line = re.sub(r"<[^>]+>", "", line).replace("**", "").replace("__", "").replace("`", "")
        line = " ".join(line.split())
        if len(line) >= 2:
            if not is_lead_in(line):
                return line
            lead = "next"
    return ""


def doc_title(text: str) -> str:
    """First Markdown heading, used as the summary of a plain document."""
    for raw in text.splitlines()[:40]:
        line = raw.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


# ---------------------------------------------------------------- functions

# A class whose name ends so is for errors or warnings. The error-file role's fourth ending, "Exc",
# is left out here: pytest's RaisesExc is no error class.
_EXCEPTION_ENDS = ("Error", "Exception", "Warning")
# Python's own exception and warning classes (OSError, KeyboardInterrupt, UserWarning, ...).
_BUILTIN_EXCEPTIONS = frozenset(name for name, value in vars(builtins).items()
                                if isinstance(value, type) and issubclass(value, BaseException))


def _is_exception(sym, earlier: set[str]) -> bool:
    """A class for errors or warnings: named so (SMTPDataError), or made from one of Python's own
    (class SMTPException(OSError)), from one named so, or from one of `earlier`, the exception
    classes the file defines before it (class SMTPSenderRefused(SMTPResponseException))."""
    if sym.kind != "class" or not sym.name:
        return False
    return sym.name.endswith(_EXCEPTION_ENDS) or any(
        base in earlier or base in _BUILTIN_EXCEPTIONS or base.endswith(_EXCEPTION_ENDS) for base in sym.bases)


def _symbol_rank(sym, exception: bool = False) -> int:
    """0: the file's public face (exported, or no leading underscore); 1: its public error and
    warning classes, which many modules define first (smtplib's ten come before class SMTP);
    2: a private class or a documented private helper; 3: other private helpers."""
    name = sym.name or ""
    if sym.exported or not name.startswith("_"):
        return 1 if exception else 0
    return 2 if sym.kind in ("class", "component") or sym.doc else 3


def functions(facts: FileFacts | None, limit: int = 8) -> list[list[str]]:
    """Up to `limit` symbols for the panel: public ones first, then the public error and warning
    classes, private helpers only in the room left, each group in source order (install.py shows
    class InstallCommand, not 8 helpers; smtplib.py shows class SMTP, not 8 of its errors)."""
    if facts is None:
        return []
    exceptions: set[str] = set()  # the file's exception classes so far, public or private
    ranked = []
    for i, sym in enumerate(facts.symbols):
        exception = sym.kind == "class" and _is_exception(sym, exceptions)
        if exception:
            exceptions.add(sym.name)
        ranked.append((_symbol_rank(sym, exception), i, sym))
    ranked.sort()  # by rank, then source order (i): i is unique, so two symbols are never compared
    return [[sym.signature or sym.name, _doc_text(sym.doc) or sym.hint or ""] for _, _, sym in ranked[:limit]]


# ---------------------------------------------------------------- files

_DATA_MODEL_EXTS = frozenset({".pkl", ".npy", ".npz", ".h5", ".parquet", ".onnx", ".pt", ".ckpt"})


def _picture_place(rel: str) -> str:
    """What the folders around a picture say it is for ("" when nothing): screenshots, design material
    and the pictures of the docs are kept for people, test pictures for tests; none is shown by the
    program, even in an images/ or assets/ folder (docs/images/)."""
    parts = set(_folder_parts(rel))
    if parts & {"screenshots", "screenshot"}:
        return "프로그램 화면을 찍어 둔"
    if parts & (NOT_CODE_FOLDERS - {"screenshots", "screenshot"}):
        return "디자인 자료로 넣어 둔"
    if parts & {"docs", "doc"}:
        return "설명 문서와 함께 둔"
    if is_test(rel) or parts & {"fixtures", "mocks", "__mocks__"}:
        return "테스트할 때 쓰는"
    return ""


def _image_role(rel: str, used_by: int = 0) -> tuple[str, str]:
    """An image is the screen's own picture only when a file loads it or it sits in an asset folder
    of the program itself."""
    if used_by:
        return "이미지 파일", "화면에 쓰이는 그림이나 아이콘이에요."
    place = _picture_place(rel)
    if place:
        return "이미지 파일", f"{place} 그림이에요."
    if set(_folder_parts(rel)) & _ASSET_DIRS:
        return "이미지 파일", "화면에 쓰이는 그림이나 아이콘이에요."
    return "이미지 파일", "그림 파일이에요."


def _binary_kind(rel: str, used_by: int = 0) -> tuple[str, str]:
    ext = extension(rel)
    if base_name(rel).lower() in CONFIG_FILES:  # bun.lockb
        return CONFIG_FILES[base_name(rel).lower()]
    if ext in IMAGE_EXTS:
        return _image_role(rel, used_by)
    if ext in FONT_EXTS:
        return "글꼴 파일", "화면에 쓰는 글꼴이에요."
    if ext in MEDIA_EXTS:
        return "소리·영상 파일", "소리나 영상이 담긴 파일이에요."
    if ext in ARCHIVE_EXTS:
        return "압축 파일", "여러 파일을 하나로 묶어 둔 파일이에요."
    if ext in DATABASE_EXTS:
        return "데이터베이스 파일", "프로그램이 저장한 데이터가 들어 있어요. 내용은 분석하지 않았어요."
    if ext in DOCUMENT_EXTS:
        return "문서 파일", "PDF나 오피스 문서예요. 내용은 분석하지 않았어요."
    if ext == ".exe":
        return "실행 프로그램", "바로 실행되는 프로그램 파일이에요. 내용은 분석하지 않았어요."
    if ext in _DATA_MODEL_EXTS:
        return "데이터·모델 파일", "프로그램이 저장해 둔 데이터나 학습된 AI 모델이에요. 내용은 분석하지 않았어요."
    if ext in COMPILED_EXTS:
        return "컴파일된 파일", "코드를 컴퓨터가 바로 읽는 형태로 바꿔 둔 파일이라서 내용은 분석하지 않았어요."
    return "글자가 아닌 파일", "글자로 된 파일이 아니라서 내용은 분석하지 않았어요."


def _config_role(rel: str) -> tuple[str, str]:
    low = base_name(rel).lower()
    if low in CONFIG_FILES:
        return CONFIG_FILES[low]
    if low.startswith(".env"):
        if any(word in low for word in ("example", "sample", "template")):
            return "환경 변수 예시", "필요한 설정값의 이름과 예시가 적혀 있어요. 실제 값은 .env 파일에 넣어요."
        return "환경 변수 설정", "비밀 키나 서버 주소 같은 설정값이 들어 있어요. 다른 사람과 공유하지 않도록 조심하세요."
    if low.startswith("requirements"):
        return _REQS
    if low.startswith("dockerfile"):
        return CONFIG_FILES["dockerfile"]
    if low.startswith("tsconfig."):
        return CONFIG_FILES["tsconfig.json"]
    tool_file = re.match(r"^(.+?)\.config\.[^.]+$", low) or re.match(r"^\.([a-z0-9_-]+?)rc(?:\.[^.]+)?$", low)
    if tool_file:
        key = tool_file.group(1).lstrip(".")
        tool = TOOL_NAMES.get(key, key)
        return f"{tool} 설정", f"{tool} 도구가 어떻게 동작할지 정하는 설정 파일이에요."
    if "workflows" in _folder_parts(rel):
        return "자동 작업 설정", "코드를 올릴 때마다 GitHub에서 자동으로 실행할 작업이 적혀 있어요."
    if extension(rel) == ".json":
        return "설정·데이터 파일", "프로그램이 읽는 설정값이나 데이터가 들어 있어요."
    return "설정 파일", "프로그램이 읽는 설정값이 들어 있어요."


def _doc_role(rel: str, title: str) -> tuple[str, str]:
    stem = _stem(rel).lower()
    if stem == "readme":
        if "/" in rel:
            return "폴더 소개 문서", "이 폴더에 무엇이 들어 있는지 설명하는 문서예요."
        return "프로젝트 소개 문서", "프로젝트가 무엇인지, 어떻게 실행하는지 적어 두는 문서예요."
    if stem in ("license", "licence"):
        return "사용 허락 조건(라이선스)", "다른 사람이 이 코드를 어떤 조건으로 쓸 수 있는지 적혀 있어요."
    if stem in ("changelog", "history"):
        return "바뀐 내용 기록", "버전마다 무엇이 바뀌었는지 적어 둔 문서예요."
    if stem == "contributing":
        return "참여 방법 안내", "이 프로젝트에 참여하는 방법을 적어 둔 문서예요."
    if title:
        return short(title), "글로 적어 둔 설명 문서예요."
    return "설명 문서", "글로 적어 둔 설명 문서예요."


def _entry_side(facts: FileFacts | None, area: str) -> str:
    if area == "ui":
        return "화면"
    if facts and (facts.http_routes or set(facts.frameworks) & WEB_SERVERS):
        return "서버"
    return "프로그램"


def _is_component(rel: str, facts: FileFacts | None) -> bool:
    ext = extension(rel)
    if ext in (".vue", ".svelte"):
        return True
    if facts and any(sym.kind == "component" for sym in facts.symbols):
        return True
    return ext in (".jsx", ".tsx") and _stem(rel)[:1].isupper()


def _is_model(rel: str) -> bool:
    low = base_name(rel).lower()
    if _stem(rel).lower() in ("model", "models", "schema", "schemas", "entity", "entities"):
        return True
    if any(f".{word}." in low for word in ("model", "schema", "entity")):
        return True
    return bool(set(_folder_parts(rel)[-1:]) & {"models", "model", "schemas", "entities"})


def _names(facts: FileFacts, kinds: tuple[str, ...], limit: int = 2) -> str:
    return ", ".join([sym.name for sym in facts.symbols if sym.kind in kinds and sym.name][:limit])


_ERROR_NAME = re.compile(r"(?:Error|Exception|Warning|Exc)$")
_ERROR_ENDS = ("Error", "Exception", "Warning", "Exc")  # the same test on a name, without the regex
_ERROR_STEMS = frozenset({"errors", "error", "exceptions", "exception", "exc"})
_NOT_COMMAND_WORDS = frozenset({"base", "abstract", "mixin", "mixins", "command", "commands"})


_NOT_KEY = re.compile(r"[^0-9a-z]")


def _key(name: str) -> str:
    return _NOT_KEY.sub("", name.lower())


def _error_role(rel: str, facts: FileFacts) -> tuple[str, str] | None:
    """errors.py or exceptions.py with error classes in it, or a file of nothing but error classes
    (not only warnings: a warning is not an error)."""
    errors = [sym for sym in facts.symbols if sym.kind == "class" and (sym.name or "").endswith(_ERROR_ENDS)]
    if not errors:
        return None
    only_errors = (len(errors) >= 2 and len(errors) == len(facts.symbols)
                   and any(not sym.name.endswith("Warning") for sym in errors))
    if only_errors or _stem(rel).lower().strip("_") in _ERROR_STEMS:
        return FILE_ROLES["errors"]
    return None


_WORD_SEPARATORS = re.compile(r"[-_.]+")


def _is_command(stem: str, facts: FileFacts) -> bool:
    """commands/install.py is the install command; commands/base.py or a JS commands/index.js is not one."""
    low = stem.lower()
    words = _WORD_SEPARATORS.split(low)
    if not low or low.startswith("_") or low in _NOT_COMMANDS or {words[0], words[-1]} & _NOT_COMMAND_WORDS:
        return False
    return low != "index" or facts.language == "python"  # pip's commands/index.py is `pip index`


def _folder_role(rel: str, facts: FileFacts, area: str) -> tuple[str, str] | None:
    """Role of the nearest folder whose name says what its code is for (routes/, services/, cli/),
    else of the file's own name (auth.py, views.py)."""
    stem = _stem(rel)
    for depth, part in enumerate(reversed(rel.split("/")[:-1])):
        name = part.lower()
        key = name if name in FILE_ROLES else name.lstrip("_")  # "_internal"
        if key not in FILE_ROLES or key in _WEAK_ROLES or key in _VENDOR_DIRS or (key in _UI_ONLY and area == "sv"):
            continue
        if key == "commands":  # only the file right inside is one command
            if depth == 0 and _is_command(stem, facts):
                s, d = FILE_ROLES[key]
                return s.format(name=stem), d.format(name=stem)
            continue
        return FILE_ROLES[key]
    low = stem.lower()
    if low in _NAME_ROLES and not (low in _UI_ONLY and area == "sv"):
        return FILE_ROLES[low]
    return None


def _main_symbol(rel: str, facts: FileFacts):
    """The public class or function the file is about: the one named after the file (align.py ->
    Align, install.py -> InstallCommand), else the only public one. A class or function whose name
    ends in Error, Exception, Warning or Exc never is; one error class beside the only public class
    is fine (PluginManager), a dozen warning classes around one warn() function are not."""
    found = [sym for sym in facts.symbols if sym.kind in ("class", "function") and sym.name]
    shown = [sym for sym in found if sym.exported] or [sym for sym in found if not sym.name.startswith("_")]
    public = [sym for sym in shown if not sym.name.endswith(_ERROR_ENDS)]
    key = _key(_stem(rel))
    picks = []
    if key:
        keyed = [(sym, _key(sym.name)) for sym in public]  # each name once: modules with 100 functions
        picks = [sym for sym, name in keyed if name == key]
        if not picks:
            starts = [sym for sym, name in keyed if name.startswith(key)]
            classes = [sym for sym in starts if sym.kind == "class"]
            picks = classes if len(classes) == 1 else starts if len({sym.name for sym in starts}) == 1 else []
    names = {sym.name for sym in public}
    if not picks and len(names) == 1 and len({sym.name for sym in shown}) <= 2:  # one error class beside it at most
        picks = public
    return next((sym for sym in picks if sym.doc), picks[0] if picks else None)  # @overload copies


def _main_symbol_role(rel: str, facts: FileFacts) -> tuple[str, str] | None:
    """The docstring of the file's main class or function, when the module itself has none."""
    sym = _main_symbol(rel, facts)
    doc = _doc_text(sym.doc) if sym is not None else ""
    summary = short(doc) if doc else ""
    if not summary or _GENERATED.search(doc):
        return None
    kind = "클래스" if sym.kind == "class" else "함수"
    return summary, _join(sentence(doc), f"이 파일의 대표 {kind}인 {sym.name}에 적힌 설명이에요.")


def _vendor_role(rel: str) -> tuple[str, str] | None:
    """pip/_vendor/rich/console.py: copied in from rich."""
    parts = rel.split("/")[:-1]
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower().lstrip("_") in _VENDOR_DIRS:
            if i + 1 == len(parts):
                return FILE_ROLES["vendor"]
            pkg = parts[i + 1]
            s = f"{pkg}에서 가져온 코드" if len(pkg) <= 14 else FILE_ROLES["vendor"][0]
            return s, f"다른 곳({pkg})에서 가져와 그대로 넣어 둔 코드예요."
    return None


def _weak_role(rel: str, area: str) -> tuple[str, str] | None:
    parts = _folder_parts(rel)
    if not parts:
        return None
    key = parts[-1] if parts[-1] in _WEAK_ROLES else parts[-1].lstrip("_")
    if key not in _WEAK_ROLES or (key in _SV_SIDE and area != "sv") or (key in _UI_SIDE and area != "ui"):
        return None
    return FILE_ROLES[key]


_HOOK_NAME = re.compile(r"^use[A-Z]")  # useTodos.js: a React hook
_HELPER_NAME = re.compile(r"util|helper")


_KIT_DATA = re.compile(r"\+(?:page|layout)(?:@[^./]*)?(?:\.server)?\.[cm]?[jt]s")
_KIT_HOOKS = re.compile(r"hooks\.(?:server|client)\.[cm]?[jt]s")


def _component_role(rel: str, tech: str, parts: set[str]) -> tuple[str, str]:
    """A Vue or Svelte file: a screen part named after the component (TodoItem.vue), or the app's
    frame, a page, a layout or the error screen (App.vue, pages/, SvelteKit's +page.svelte)."""
    stem = _stem(rel)
    if stem.lower() == "index" and _folder_parts(rel):  # components/TodoItem/index.vue
        stem = rel.split("/")[-2]
    kind = stem[1:].split("@", 1)[0] if stem.startswith("+") else ""  # SvelteKit's +page, +layout, +error
    made = f"{tech}로 만들었어요."  # the same words as for React components (화면 조각), with the framework
    if stem.lower() == "app":
        return "화면 전체 틀", _join("화면 조각들을 모아 전체 화면을 짜는 뼈대예요.", made)
    if kind == "error" or (stem.lower() == "error" and _folder_parts(rel) in ([], ["app"])):  # Nuxt's error.vue
        return "오류 화면", _join("문제가 생겼을 때 대신 보여 주는 화면이에요.", made)  # (app/ in Nuxt 4)
    if kind == "layout" or (not kind and parts & {"layouts", "layout"}):
        return "화면 배치 틀", _join("여러 페이지가 같이 쓰는 화면 배치를 정해요.", made)
    if kind == "page" or (not kind and parts & {"pages", "screens", "views"}):
        return "화면 페이지", _join("주소마다 보이는 화면 한 페이지예요.", made)
    return f"{tech} 화면 조각", f"화면의 한 부분({stem})을 그리는 {tech} 코드예요."


def _code_role(rel: str, facts: FileFacts, uses: int = 0, area: str = "ot") -> tuple[str, str]:
    stem = _stem(rel)
    low_stem = stem.lower()
    parts = set(_folder_parts(rel))
    lang_name = LANGUAGE_NAMES.get(facts.language, "코드")
    if base_name(rel).lower() == "conftest.py":
        return "테스트 공통 준비", "테스트를 돌리기 전에 pytest가 먼저 읽는 공통 준비 코드예요."
    if is_test(rel):
        return "테스트 코드", "다른 코드가 제대로 동작하는지 자동으로 확인하는 코드예요."
    tech = SFC_TECH.get(extension(rel))
    if tech:
        return _component_role(rel, tech, parts)
    if facts.http_routes:
        return "주소별 요청 처리", f"{_paths(facts.http_routes)} 주소로 오는 {_methods(facts.http_routes)}요청을 처리해요."
    if _KIT_DATA.fullmatch(base_name(rel)):  # SvelteKit's +page.ts: the data of the page beside it
        where = "서버에서 " if ".server." in base_name(rel) else ""
        return "페이지 데이터 준비", f"화면에 보여 줄 데이터를 {where}미리 불러오는 코드예요."
    if _KIT_HOOKS.fullmatch(base_name(rel)):  # SvelteKit's hooks.server.ts, not a React hook
        return "요청 중간 처리", "요청이 올 때마다 본 처리 전에 먼저 거쳐 가는 코드예요."
    if low_stem == "__init__" and not facts.symbols:  # before the folder-name roles: utils/__init__.py
        if uses:
            return "패키지 입구 파일", "폴더 안의 모듈을 불러와서 바깥에서 쓰기 쉽게 모아 주는 파일이에요."
        return "패키지 표시 파일", "이 폴더를 Python 패키지로 쓰게 해 주는 파일이에요."
    if _is_model(rel):
        names = _names(facts, ("class",), 3)
        what = f"{names} 데이터가" if names else "저장하거나 주고받는 데이터가"
        return "데이터 모양 정의", f"{what} 어떤 항목을 갖는지 정해요."
    if _is_component(rel, facts):
        if low_stem == "app":
            return "화면 전체 틀", "화면 조각들을 모아 전체 화면을 짜는 뼈대예요."
        if parts & {"pages", "screens", "views"}:
            return "화면 페이지", "주소마다 보이는 화면 한 페이지예요."
        names = _names(facts, ("component",))
        part = f"화면의 한 부분({names})" if names else "화면의 한 부분"
        return "화면 조각", f"{part}을 그리는 코드예요."
    if _HOOK_NAME.match(stem):
        if "composables" in parts or set(facts.frameworks) & {"vue", "nuxt", "svelte", "sveltekit"}:
            return FILE_ROLES["composables"]  # a Vue composable (useTodos.ts): not a React hook
        return "화면 동작 도우미(훅)", "여러 화면 조각이 같이 쓰는 동작(React 훅)이에요."
    if facts.http_calls:
        summary = "서버 요청 함수 모음" if len(facts.symbols) >= 2 else "서버 요청 코드"
        return summary, f"서버의 {_paths(facts.http_calls)} 주소로 {_methods(facts.http_calls)}요청을 보내요."
    db_libs = set(facts.frameworks) & (set(DB_FRAMEWORKS) | {"sqlalchemy"})
    if low_stem in ("db", "database") or db_libs:  # "connection" alone is often HTTP (urllib3)
        return "DB 연결 준비", "데이터베이스에 연결하고 데이터를 꺼내 쓸 준비를 하는 코드예요."
    if low_stem in ("store", "stores", "slice") or parts & {"store", "stores", "redux"}:
        return "화면 상태 저장소", "여러 화면이 같이 쓰는 데이터(상태)를 보관해요."
    if "middleware" in low_stem or parts & {"middleware", "middlewares"}:
        return "요청 중간 처리", "요청이 오면 본 처리 전에 검사하거나 기록하는 코드예요."
    if _HELPER_NAME.search(low_stem) or parts & {"utils", "util", "helpers", "lib"}:
        return "도우미 함수 모음", "여러 곳에서 같이 쓰는 작은 기능들을 모아 둔 파일이에요."
    if base_name(rel).lower().endswith(".d.ts") or low_stem in ("types", "typings"):
        return "데이터 타입 정의", "코드에서 쓰는 데이터의 타입(모양)을 정해요."
    if low_stem in ("config", "settings", "constants", "const"):
        return "설정값 모음", "프로그램 곳곳에서 쓰는 설정값을 모아 둔 코드예요."
    count = len(facts.symbols)
    if low_stem == "__init__":
        return "패키지 입구 파일", f"이 폴더(패키지)를 불러오면 먼저 실행되는 파일이에요. 함수나 클래스 {count:,}개가 들어 있어요."
    # Before "Python 코드": what the folder, the file's name or its main class shows, strongest first.
    role = (_error_role(rel, facts) or _folder_role(rel, facts, area) or _main_symbol_role(rel, facts)
            or _vendor_role(rel) or _weak_role(rel, area))
    if role:
        return role
    if count:
        return f"{lang_name} 코드", f"함수나 클래스 {count:,}개가 들어 있는 {lang_name} 코드예요."
    return f"{lang_name} 코드", f"{lang_name} 코드 파일이에요."


def _plain_role(rel: str, area: str, title: str, used_by: int = 0) -> tuple[str, str]:
    ext = extension(rel)
    if is_image(rel):
        return _binary_kind(rel, used_by)  # .svg is text
    if _stem(rel).lower() == "readme":
        return _doc_role(rel, title)
    if is_config(rel):
        return _config_role(rel)
    if is_doc(rel):
        return _doc_role(rel, title)
    if ext in STYLE_EXTS:
        return "화면 꾸미기(CSS)", "색, 글자 크기, 배치처럼 화면의 모양을 정해요."
    if ext in HTML_EXTS:
        if _stem(rel).lower() == "index":
            return "웹 페이지 뼈대(HTML)", "브라우저가 처음 여는 페이지예요. 화면 코드를 불러와 붙일 자리를 만들어요."
        return "웹 페이지(HTML)", "브라우저에 보이는 페이지의 뼈대예요."
    if ext in SFC_TECH:  # not analyzed (no JavaScript analyzer)
        return f"{SFC_TECH[ext]} 화면 조각", f"화면의 한 부분을 그리는 {SFC_TECH[ext]} 코드예요."
    if ext == ".sql":
        return "DB 명령(SQL)", "데이터베이스에 표를 만들거나 데이터를 넣는 명령이 적혀 있어요."
    if ext in (".csv", ".tsv"):
        return "표 데이터", "엑셀 표처럼 줄과 칸으로 정리된 데이터예요."
    if ext in (".sh", ".bash", ".bat", ".cmd", ".ps1"):
        return "실행 명령 모음", "터미널에서 실행할 명령을 차례대로 적어 둔 파일이에요."
    if ext in _OTHER_CODE:
        name = _OTHER_CODE[ext]
        return f"{name} 코드", f"{name} 언어로 쓴 코드예요. 이 언어의 연결은 아직 분석하지 않아요."
    if ext == ".pyi" or base_name(rel).lower() == "py.typed":
        return "타입 정보 파일", "편집기와 검사 도구가 읽는, 함수와 데이터의 모양(타입)을 적어 둔 파일이에요."
    if ext in (".pem", ".crt", ".cer"):
        return "인증서 파일", "안전한 연결(HTTPS)을 확인할 때 쓰는 인증서가 들어 있어요."
    if ext == ".log":
        return "기록(로그) 파일", "프로그램이 실행되면서 남긴 기록이에요."
    return "기타 파일", "프로젝트에 들어 있는 파일이에요."


def describe_file(
    rel: str,
    facts: FileFacts | None = None,
    *,
    area: str = "ot",
    entry: bool = False,
    entry_reason: str = "",
    orphan: bool = False,
    uses: int = 0,
    used_by: int = 0,
    binary: bool = False,
    skipped: bool = False,
    title: str = "",
    unreadable: bool = False,
    runnable: bool = False,
    by_path: bool = False,
    loaded: str = "",
) -> tuple[str, str]:
    """Return (s, d) for one file.

    `runnable`: a module that other files import but that can also be run directly
    (a demo `if __name__ == "__main__":` block), so it is not shown as a start point.
    `by_path`: a Python file whose name no import statement can spell (__pip-runner__.py),
    so it is run or loaded by its path.
    `loaded`: how the file is used although no import names it, one Korean sentence ("Nuxt가
    화면에 쓰인 이름을 보고 알아서 불러와요."). It follows the role, in place of "다른 파일과 직접
    이어져 있지 않아요.". A start point says why it starts instead.
    """
    links = connections(uses, used_by)
    if orphan:
        return "사용되지 않는 파일", "어느 파일에서도 이 파일을 불러 쓰지 않아요. 예전에 만들었다가 남겨 둔 파일일 수 있어요."
    if unreadable:
        return "열 수 없는 파일", "권한이 없거나 다른 프로그램이 쓰고 있어서 내용을 읽지 못했어요."
    if binary:
        s, d = _binary_kind(rel, used_by)
        return s, _join(d, links)
    if skipped:
        return "너무 커서 건너뛴 파일", _join("파일이 1MB보다 커서 내용을 분석하지 않았어요.", links)
    code = facts is not None and facts.language in CODE_LANGUAGES and not is_config(rel)
    marker = base_name(rel).lower() in ("__init__.py", "conftest.py")  # run by Python or pytest, not imported
    note = sentence(loaded) if loaded and loaded.strip() and not entry else ""
    if code and not links and not entry and not marker and not note:
        links = ("파일 이름 때문에 import 문으로는 불러올 수 없어서, 다른 코드가 파일 경로로 불러오거나 "
                 "직접 실행하는 파일일 거예요." if by_path else "다른 파일과 직접 이어져 있지 않아요.")

    start = ""
    if entry:
        side = _entry_side(facts, area)
        reason = entry_reason.strip().rstrip(".")
        start = f"실행하면 여기서부터 시작돼요({reason})." if reason else "실행하면 여기서부터 시작돼요."
        role = (f"{side} 시작점", start)
    elif code:
        role = _code_role(rel, facts, uses, area)
    else:
        role = _plain_role(rel, area, title, used_by)

    also = "직접 실행할 수도 있어요." if runnable and not entry else ""
    broken = "코드에 문법 오류가 있어서 읽을 수 있는 부분만 분석했어요." if facts is not None and facts.parse_error else ""
    raw = (facts.module_doc or "").strip() if facts is not None else ""
    doc = _doc_text(raw)  # "Spinners are from:" says nothing: the role above is used
    if raw and _GENERATED.search(raw):
        if not entry:
            role = ("자동으로 만든 코드", "도구가 자동으로 만든 파일이에요. 직접 고치면 다시 만들 때 덮어써질 수 있어요.")
    elif doc and (summary := short(doc)):
        return summary, _join(sentence(doc), start, note, also, links, broken)
    return role[0], _join(role[1], note, also, links, broken)


# ---------------------------------------------------------------- folders and root

def describe_folder(
    rel: str,
    *,
    area: str = "ot",
    files: int = 0,
    folders: int = 0,
    area_label: str = "",
    ui_tech: str = "",
    sv_tech: str = "",
    doc: str = "",
    package: bool = False,
    unreadable: bool = False,
    mixed: str = "",
    cut_off: bool = False,
    images: int = 0,
    used_outside: int = 0,
) -> tuple[str, str]:
    """Return (s, d) for a folder; files/folders count its direct children.

    `doc` is the docstring of the folder's __init__.py. A Korean one beats the folder-name
    dictionary; an English one is used only for names the dictionary does not know.
    `mixed` is "server" or "code" when screen code and other code share the folder.
    `cut_off`: the file limit was reached before the folder was looked through.
    `images`: how many of the direct child files are images (all of them: a picture folder).
    `used_outside`: how many code files in the folder, at any depth, code outside it uses. A
    design/ folder whose Button.tsx the app imports holds program code, not design material.
    """
    name = base_name(rel).lower()
    bare = name.lstrip("_")  # "_vendor", "_internal"
    known = FOLDERS.get(name) or (FOLDERS.get(bare) if bare else None)
    if known and (bare in _UI_ONLY or bare in NOT_CODE_FOLDERS) and area == "sv":
        known = None  # pygments/styles holds Python code, not CSS
    if known and bare in NOT_CODE_FOLDERS and used_outside:
        known = None  # src/design/ of a React app: its components and tokens are code
    place = _picture_place(rel) if known and bare in ("images", "img", "icons") and area != "sv" else ""
    if place:  # docs/images holds the pictures of the docs, not of the screen
        known = (known[0], f"{place} 그림이 모여 있어요.")
    if unreadable:
        contents = "열어 볼 권한이 없어서 안에 무엇이 있는지 보지 못했어요."
    elif cut_off:
        contents = "파일이 너무 많아서 이 폴더 안은 다 보지 못했어요."
    else:
        contents = counts(files, folders)
    raw = (doc or "").strip()
    doc = _doc_text(raw)
    if doc and short(doc) and not _GENERATED.search(raw) and (_HANGUL.search(doc) or not known):
        return short(doc), _join(sentence(doc), contents)
    if known:
        s, d = known
        tech = ui_tech if name in _UI_SIDE else sv_tech if name in _SV_SIDE else ""
        return (f"{s} ({tech})" if tech else s), _join(d, contents)
    if files and images >= files and area == "ot" and not unreadable:
        return "그림 모음", _join("그림 파일이 모여 있어요.", contents)
    if mixed == "server":
        return "화면·서버 코드 모음", _join("화면 쪽 코드와 서버 쪽 코드가 함께 있는 폴더예요.", contents)
    if mixed == "code":
        return "화면 코드와 그 밖의 코드", _join("화면 쪽 코드와 다른 코드가 함께 있는 폴더예요.", contents)
    label = area_label or {"ui": "화면 코드", "sv": "서버 코드"}.get(area, "문서·설정")
    if package:
        return f"{label} 모음", _join("함께 쓰는 Python 코드를 묶어 둔 폴더(패키지)예요.", contents)
    return f"{label} 모음", contents


def describe_root(
    name: str,
    *,
    readme: str | None = None,
    languages: list[str] | None = None,
    files: int = 0,
    folders: int = 0,
    doc: str = "",
    unreadable: bool = False,
) -> tuple[str, str]:
    """Return (s, d) for the project root.

    The README's first paragraph line, else the root package's docstring (when the
    analyzed folder is itself a package), else the languages and counts.
    """
    if unreadable:
        return "열어 볼 수 없는 폴더", "폴더를 열어 볼 권한이 없어서 안에 무엇이 있는지 보지 못했어요."
    line = _doc_text(readme_line(readme)) if readme else ""
    if line and short(_first_sentence(line), 30):
        return short(_first_sentence(line), 30), sentence(line, 160)
    langs = ", ".join(languages or [])
    made = f"{langs} 코드로 된 프로젝트예요." if langs else ""
    raw = (doc or "").strip()
    doc = _doc_text(raw)
    if doc and short(doc, 30) and not _GENERATED.search(raw):
        return short(doc, 30), _join(sentence(doc, 160), made, counts(files, folders))
    if langs:
        return f"{langs} 프로젝트", _join(made, counts(files, folders))
    return "프로젝트 폴더", counts(files, folders)
