# CodeVision 구조와 모듈 계약

이 문서는 모듈 사이의 약속(계약)이다. 모듈을 고칠 때 이 문서와 `src/codevision/model.py`가 기준이다.

## 1. 흐름

```
폴더 경로
  → scan.scan()            파일·폴더 목록 (무시 규칙 적용) → ProjectIndex
  → lang.<언어>.analyze()  파일마다 FileFacts (import, 함수, HTTP, 시작점 단서)
  → lang.<언어>.resolve()  import → 프로젝트 안의 파일 경로
  → graph.build()          연결(u/b/h), 시작점, 정리 후보, 영역, 요약 → 그래프 JSON
  → explain.enrich()       AI 설명 채우기 (지금은 뼈대만, 공급자 "none")
  → server                 /api/analyze 로 JSON 전달
  → web/app.js             트리 도식과 패널 그리기
```

## 2. 파일과 담당

| 파일 | 하는 일 |
|---|---|
| `src/codevision/model.py` | 공용 데이터 클래스(FileFacts 등), ProjectIndex, HTTP 경로 정규화·매칭 |
| `src/codevision/lang/__init__.py` | 분석기 등록부 (`analyzer_for(path)`) |
| `src/codevision/lang/python_lang.py` | Python 분석기 (`ast`) |
| `src/codevision/lang/js_lang.py` | JS/TS 분석기 (tree-sitter, 없으면 정규식 대체) |
| `src/codevision/scan.py` | 폴더 탐색, 무시 규칙, ProjectIndex 생성 |
| `src/codevision/graph.py` | `build(root_abs) -> dict` 그래프 JSON 생성 |
| `src/codevision/describe.py` | AI 없이 만드는 한국어 요약·설명 (규칙 기반) |
| `src/codevision/explain/` | AI 설명 뼈대: 인터페이스, 공급자 선택, 캐시, 프롬프트 초안 |
| `src/codevision/server.py` | 로컬 웹 서버 (표준 라이브러리 `http.server`) |
| `src/codevision/__main__.py` | 실행 진입점 `python -m codevision` |
| `src/codevision/web/` | 화면: `index.html`, `app.js`, `app.css`, `icons.css`(아이콘. 인터넷 없이도 보이도록 앱 안에 들어 있음) |
| `src/codevision/demo/my-todo-app/` | 예제 프로젝트 (화면의 "예제 프로젝트 열기"와 테스트에 사용) |
| `tests/` | pytest 테스트, `tests/fixtures/sample-graph.json`은 화면 개발용 예시 그래프 |

## 3. 분석기 인터페이스

각 분석기 모듈(`codevision.lang.*`)은 다음을 제공한다.

```python
EXTENSIONS: tuple[str, ...]                                   # 소문자, 점 포함 (".py")
def analyze(rel_path: str, source: str) -> FileFacts
def resolve(imp: ImportRef, importer: str, index: ProjectIndex) -> list[str]
def project_entries(index: ProjectIndex) -> dict[str, str]    # rel_path -> 한국어 이유

# 선택 (있으면 graph가 쓴다)
def project_links(index: ProjectIndex) -> dict[str, list[str]]  # 파일 -> 코드의 import가 아닌 방법으로 불러오는 파일
                                                                # (index.html -> app.js, app.css)
```

- 등록부(`lang/__init__.py`)는 분석기를 처음 쓸 때 한 번만, 잠금을 걸고 불러온다. 서버가 여러 요청을 동시에 분석해도 반쯤 채워진 목록을 보는 일이 없다.
- `analyze`는 예외를 던지지 않는다. 문법 오류면 `parse_error=True`로 할 수 있는 만큼만 채운다.
  - Python 분석기는 긴 `elif` 사슬, 한 단어짜리 줄이 아주 많은 docstring, 긴 문자열 더하기(`"a" + "b" + …`)를 재귀 대신 반복문으로 읽는다. 그래서 Python으로는 문제없는 이런 파일을 문법 오류로 세지 않는다.
  - 그래도 구문 트리(AST)를 따라가다 어느 최상위 문장에서 `RecursionError`가 나면 그 문장만 건너뛰고, 나머지 문장에서 찾은 것은 그대로 둔다 (`parse_error`는 False).
- `resolve`는 프로젝트 안의 파일 경로 목록을 돌려준다. 외부 패키지(react, fastapi 등)는 빈 목록이다.
  `from routes import todos, users`처럼 한 import가 여러 파일을 가리킬 수 있다.
- `project_entries`는 파일 하나만 봐서는 모르는 시작점을 찾는다 (package.json, index.html, pyproject.toml 등).
- 모든 경로는 프로젝트 기준 POSIX 경로다 (`frontend/src/api.js`). 루트 폴더는 `""`.

### FileFacts 필드 (model.py)

| 필드 | 뜻 |
|---|---|
| `imports` | `ImportRef(spec, names, kind, level, line)`. kind: import, from, require, dynamic, reexport, side-effect |
| `symbols` | 패널 "주요 함수"에 보일 최상위 함수·클래스·컴포넌트. `signature`는 화면에 그대로 보이는 글자 (`addTodo(title)`, `class Todo`) |
| `http_calls` | 화면 쪽 요청 (fetch, axios 등). `path`는 `normalize_http_path()` 결과 |
| `http_routes` | 서버 쪽 주소 정의 (FastAPI, Flask, Express, `http.server` 핸들러의 경로 표 등). `HttpRoute(method, path, handler, line, base)`. `path`도 정규화. `base`는 라우트가 이 파일에서 속한 맨 바깥 라우터(Blueprint, APIRouter) 자신의 prefix를 정규화한 것이고(없으면 `""`), 다른 파일의 Flask `url_prefix`가 이 앞부분을 대신한다 |
| `http_mounts` | 다른 파일의 라우터를 주소 앞부분(prefix) 아래에 붙이는 곳. `HttpMount(prefix, target, line, base, replace, base_own)`. Express/Koa `app.use("/api", router)`, Hono `app.route`, Fastify `register(..., {prefix})`, FastAPI `include_router(..., prefix=)`, Flask `register_blueprint(..., url_prefix=)`. `base`는 대상을 붙인 쪽 라우터가 이 파일에서 응답하는 prefix (앱에 붙였으면 `""`). `replace`는 Flask `url_prefix`라서 대상 Blueprint 자신의 prefix(라우트의 `base`)를 대신하면 True. `base_own`은 `base`의 앞부분 중 맨 바깥 라우터 자신의 prefix로, 이 파일을 다른 파일이 Flask `url_prefix`로 붙이면 이 부분만 그 prefix로 바뀐다. 붙인 쪽 라우터가 이 파일에서 두 곳 이상에 붙어 있으면(`/v1`과 `/v2`) 그 자리마다 `HttpMount`가 하나씩 있다. `base`, `replace`, `base_own`은 Python 분석기만 채우고, JavaScript 분석기의 마운트는 늘 기본값(`""`, False)이다. `prefix`, `base`, `base_own`은 쓴 글자 그대로이고(모르는 부분은 `${}`) 붙일 때 정규화한다 |
| `frameworks` | import에서 찾은 프레임워크·라이브러리의 표준 이름 (아래 표) |
| `is_entry`, `entry_reason` | 파일 안의 시작점 단서 (`if __name__ == "__main__"`, `app = FastAPI()`, `createRoot(...).render`, `app.listen`) |
| `module_doc` | 모듈 docstring이나 맨 위 주석에서 요약으로 쓸 첫 문장. 줄바꿈으로 나뉜 문장은 이어 붙인다 (5장 요약 참고) |

### frameworks 표준 이름

- 화면: `react`, `vue`, `svelte`, `next`, `angular`
- 서버: `fastapi`, `flask`, `django`, `starlette`, `aiohttp`, `express`, `koa`, `fastify`, `nestjs`, `hono`, `http.server` (점이 들어간 채로 쓰는 유일한 이름)
- DB: `sqlite`, `sqlalchemy`, `postgres`, `mysql`, `mongodb`, `prisma`, `sequelize`, `typeorm`, `supabase`, `firebase`, `redis`
- 그 밖: 필요하면 소문자 패키지 이름을 그대로 쓴다 (`pandas`, `streamlit` 등)

## 4. 그래프 JSON (서버 → 화면)

키 이름은 승인된 목업(`design/mockup.html`)의 데이터와 같다. 예시: `tests/fixtures/sample-graph.json`.

```jsonc
{
  "version": 1,
  "source": {"kind": "local", "path": "C:\\...\\my-todo-app"},
  "name": "my-todo-app",                // 분석한 폴더 이름
  "root": ".",                          // 루트 노드 id
  "focus": "backend/routes/todos.py",   // 처음 선택할 파일 (연결이 가장 많은 파일)
  "stats": {"folders": 5, "files": 15, "entries": 2, "orphans": 1, "skipped": 0},
  "hidden": ["node_modules", ".venv"],  // 실제로 있어서 숨긴 폴더 이름
  "areas": {"ui": "화면 코드", "sv": "서버 코드", "ot": "문서·설정"},  // 범례 이름. 웹 서버가 없으면 sv는 "코드"
  "stack": [{"area": "ui", "label": "화면", "tech": "React"}, {"area": "sv", "label": "서버", "tech": "FastAPI"}, {"area": "sv", "label": "DB", "tech": "SQLite"}],
  "ai": {"provider": "none", "available": false, "message": "AI 설명이 아직 연결되지 않았어요."},
  "warnings": [],                       // 화면에 작게 보여 줄 한국어 안내
  "truncated": false,                   // 파일이 너무 많아 일부만 분석했으면 true
  "generated": 1763480000.0,
  "nodes": {
    ".": {"n": "my-todo-app", "p": "my-todo-app/", "t": "d", "a": "ot", "s": "…", "d": "…", "k": ["backend", "frontend", "helpers_old.py", "README.md"]},
    "frontend/src/api.js": {
      "n": "api.js", "p": "frontend/src/api.js", "t": "f", "a": "ui", "l": 24,
      "s": "서버 요청 함수 모음", "d": "…",
      "f": [["fetchTodos()", "할 일 목록을 서버에서 가져와요."]],
      "u": ["backend/routes/todos.py"], "b": ["frontend/src/components/TodoItem.jsx", "frontend/src/components/TodoList.jsx"],
      "h": ["backend/routes/todos.py"], "lang": "javascript"
    }
  }
}
```

노드 필드:

| 키 | 뜻 | 비고 |
|---|---|---|
| `n` | 이름 | |
| `p` | 화면에 보이는 경로 | 루트 `이름/`, 폴더 `경로/`, 파일 `경로`. 드라이브 루트(`C:\`)는 `n`과 그래프의 `name`이 `C:`, `p`가 `C:/` |
| `t` | `d` 폴더, `f` 파일 | |
| `a` | 영역 `ui` / `sv` / `ot` | 색: ui #534AB7, sv #0F6E56, ot #5F5E5A |
| `k` | 자식 id 목록 | 폴더만. 폴더 먼저, 그다음 파일. 각각 대소문자 무시 가나다/알파벳순 |
| `l` | 줄 수 | 파일만 |
| `s` | 한 줄 요약 | 비어 있지 않게 |
| `d` | 설명 문장 | 비어 있지 않게 |
| `f` | 주요 함수 `[[표시, 설명], …]` | 최대 8개. 설명은 비어 있을 수 있음 |
| `u` | 이 파일이 쓰는 파일 | import 연결 + HTTP 연결 |
| `b` | 이 파일을 쓰는 파일 | `u`의 반대 방향 |
| `h` | HTTP로 이어진 파일 | `u`/`b`에도 들어 있고, 화면에서 " · HTTP" 표시 |
| `e` | 1이면 시작점 | |
| `o` | 1이면 정리 후보 | 색 #BA7517 |
| `i` | 아이콘 이름 (Tabler 이름) | 없으면 폴더는 ti-folder, 파일은 ti-file-code. 새 이름을 쓰려면 `web/icons.css`에 그 아이콘을 추가해야 한다 |
| `lang` | python / javascript / typescript | 코드 파일만 |

id는 프로젝트 기준 POSIX 경로이고 루트만 `"."`이다. 파일 이름에 HTML 문자가 들어 있을 수 있으므로 화면은 모든 글자를 이스케이프한다.

추가 약속:

- 파일 노드에는 `f`, `u`, `b`, `h`가 항상 있다 (빈 목록일 수 있음). `e`, `o`, `i`, `lang`은 해당될 때만 있다.
- `source.kind`는 `local`(로컬 폴더), `demo`(예제 프로젝트, path는 빈 문자열), 나중에 `github`.
- 바이너리 파일(이미지 등)은 크기와 상관없이 목록에만 들어가고 `skipped`에 세지 않는다. `skipped`는 1MB가 넘어 분석하지 못한 텍스트 파일 수다. 바이너리·건너뛴 파일의 `l`은 0이다.
- docstring에서 가져온 글(모듈 docstring, 모듈 docstring이 없는 파일에서 쓰는 대표 클래스·함수의 docstring, 주요 함수 설명)은 번역하지 않고 쓰므로, 영어 docstring은 AI 설명이 붙기 전까지 영어로 보인다. 모듈 docstring은 역할 추측보다 먼저라서, 영어 모듈 docstring이 있는 `exceptions.py`에는 "오류 종류 정의" 대신 그 영어 문장이 보인다.

## 5. 규칙

- **영역**: `.py` → sv. `.jsx`/`.tsx`/`.vue`/`.svelte`/`.html`/`.css`/`.scss` → ui. `.js`/`.ts`는 화면 프레임워크를 쓰거나 화면 패키지(package.json에 react·vue 등) 안에 있거나 웹 페이지가 `<script src>`로 불러오면 ui, 서버 프레임워크를 쓰거나 서버 패키지 안에 있으면 sv, 판단이 안 되면 프로젝트에 화면 프레임워크가 있을 때 ui, 아니면 sv. 설정 파일(`*.config.js`, `package.json`, `tsconfig.json`, `requirements.txt`, `pyproject.toml`, `Dockerfile` 등)과 문서·데이터는 ot. 폴더는 자손 파일 중 ot가 아닌 영역의 다수결, 없으면 ot. 루트는 ot.
- **연결(u, b)**: import 말고도 다음을 연결로 친다.
  - 웹 페이지가 불러오는 파일 (`<script src>`, `<link href>`, 분석기의 `project_links()`)
  - Python에서 이름 글자로 불러오는 모듈: 같은 파일에 글자로 적힌 `import_module(...)`/`__import__(...)` 이름, 모듈 경로 표(Django `INSTALLED_APPS`, `ROOT_URLCONF`, pygments 같은 이름 표. dict의 키는 제외), `DJANGO_SETTINGS_MODULE`, `"polls.apps.PollsConfig"`처럼 끝에 클래스 이름이 붙은 경로, `import_module(f".unicode{v}", __package__)`처럼 앞부분이 정해진 이름(그 폴더의 맞는 파일들)
  - 분석하는 폴더의 이름은 최상위 패키지 이름으로도 쓴다 (`pip` 폴더를 열었을 때 `from pip._internal import …`). 일반 탐색이 먼저다.
  - `import __main__`은 어떤 파일도 가리키지 않는다. `if TYPE_CHECKING:` 아래 import는 프레임워크로 세지 않는다.
- **시작점(e)**: FileFacts.is_entry 또는 project_entries()에 있는 파일. 단, `if __name__ == "__main__"` 블록만 있고 다른(테스트가 아닌) 코드가 불러 쓰는 모듈은 시작점이 아니다. 그런 파일의 d에는 "직접 실행할 수도 있어요."가 붙는다. `__main__.py`, 서버 앱, project_entries의 근거는 그대로 시작점이다.
- **정리 후보(o)**: 코드 파일(python/javascript/typescript)인데 다른 파일이 쓰지 않고(b가 비어 있음), 시작점이 아니고, 다음에 해당하지 않는 파일.
  - 테스트: `test_*.py`, `*_test.py`, `tests.py`, `test.py`, `*.test.*`, `*.spec.*`, `tests/` 아래
  - 설정: `*.config.*`, `setup.py`, `conftest.py`, `manage.py`
  - `__init__.py`, `.d.ts`
  - `migrations/`, `alembic/` 아래 (프레임워크가 알아서 불러옴)
  - 이름을 import 문으로 쓸 수 없는 `.py` (`__pip-runner__.py`처럼 `-`가 들어간 이름. 경로로 실행되거나 불러와진다). 이런 파일은 docstring 요약을 그대로 쓰고, 다른 파일과 이어져 있지 않고 시작점도 아니면 d에 "파일 이름 때문에 import 문으로는 불러올 수 없어서, 다른 코드가 파일 경로로 불러오거나 직접 실행하는 파일일 거예요."가 붙는다.
- **HTTP 연결**: 화면 파일의 http_calls와 서버 파일의 http_routes를 `model.http_paths_match()`와 `http_methods_match()`로 맞춘다. 맞으면 호출한 파일의 `u`, 서버 파일의 `b`, 양쪽의 `h`에 넣는다.
  - 맞추기 전에 `http_mounts`의 prefix를 붙여 둔다. 연결된 마운트(app → api 라우터 → todos)는 9단계까지 이어 붙인다. 그래서 라우트 파일의 설명과 Python 라우트 힌트(docstring이 없는 주요 함수의 설명 자리에 보이는 `GET · /api/todos`)에도 전체 주소가 보인다.
  - FastAPI는 prefix를 앞에 이어 붙인다. `APIRouter(prefix="/todos")`를 `include_router(..., prefix="/api")`로 붙이면 `/api/todos`다. 자기 prefix가 있는 라우터에 붙인 라우터는 그 prefix도 받는다 (`APIRouter(prefix="/api")`에 붙인 todos 라우터는 `/api/todos`).
  - Flask는 `register_blueprint(..., url_prefix=)`의 `url_prefix`가 Blueprint 자신의 `url_prefix`(라우트의 `base`)를 대신한다. `Blueprint(url_prefix="/todos")`를 `url_prefix="/api"`로 붙이면 `/api/todos`가 아니라 `/api`다. `url_prefix`가 없거나 `None`이면 Blueprint 자신의 것을 쓴다. Blueprint를 다른 Blueprint에 붙이면 바깥 Blueprint의 prefix 뒤에 이어진다 (`api_bp.register_blueprint(todos_bp)` → `/api/todos`).
  - 우선순위 낮은 문제 수정(2026-11-20 완료)으로 바뀐 결과: 그 전에는 다른 라우터에 붙인 라우터가 바깥 라우터의 prefix를 받지 못했고(`APIRouter(prefix="/api")`에 붙인 todos 라우터와 `api_bp.register_blueprint(todos_bp)` 모두 `/todos`), Flask `url_prefix`는 Blueprint 자신의 prefix 앞에 이어 붙였다(`/api/todos`).
  - Python에서는 같은 파일에서 만든 라우터에 붙이면 prefix가 없어도 마운트로 기록한다. 세 단계 사슬(`root_bp → api_bp → todos_bp`, 앞의 둘이 한 파일)을 다른 파일이 `url_prefix="/v1"`로 붙이면 맨 바깥 `root_bp` 자신의 prefix(`base_own`)만 바뀌고 가운데 Blueprint의 몫은 남는다 (`/v1/api/todos`).
  - 한 라우터를 여러 곳에 붙이면(`/v1`과 `/v2`) 주소마다 라우트가 생기고, 힌트도 주소를 모두 보여 준다 (`GET · /v1/todos, /v2/todos`). 힌트에는 주소를 8개까지 쓰고, 한 라우터나 파일이 받는 prefix도 8개까지만 센다. 다른 파일에서 붙인 prefix 가운데 하나라도 실행할 때만 정해지면(`${}`) 힌트는 라우트 파일 안에서 정한 주소 그대로다.
  - Python에서 prefix 없이 앱에 붙인 것(`include_router(router)`, `register_blueprint(bp)`)은 같은 파일에서 같은 라우터를 다른 곳에도 붙였을 때만 마운트로 기록한다. 그러면 쓴 그대로의 주소도 함께 응답한다 (`GET · /v1/todos, /todos`). 혼자 있으면 주소가 바뀌지 않으므로 기록하지 않는다.
  - 다른 파일에서 붙인 마운트는 라우터가 아니라 파일 단위로 계산한다. 한 파일의 라우터 둘을 다른 파일에서 서로 다른 prefix로 붙이면(`todos`는 `/todos`, `users`는 `/users`) 그 파일의 모든 라우트가 두 prefix를 다 받아서 `/users/items`처럼 없는 주소도 생긴다. prefix 없는 마운트의 짝도 같은 파일 안에서만 찾으므로, 같은 라우터를 한 파일에서는 `/v1`로, 다른 파일에서는 prefix 없이 붙였으면 쓴 그대로의 주소(`/todos`)는 빠진다.
  - JavaScript(Express 등)에서는 라우터를 같은 파일 안의 다른 라우터에 붙이면 바깥 라우터의 prefix를 받지 못한다. `api.use("/todos", todos)`와 `app.use("/api", api)`가 한 파일에 있으면 todos의 주소는 `/api/todos`가 아니라 `/todos`로 보인다. 호출 쪽 앞에 세그먼트가 더 있어도 맞추므로(아래) 연결은 대개 이어진다. 파일을 나눠 붙인 사슬은 이어 붙인다.
  - `/`와 파일처럼 보이는 경로(`/app.js`)는 주소로 치지 않는다 (정적 파일 표가 DELETE 주소와 잘못 이어지던 문제).
  - 호출 쪽 앞에 세그먼트가 더 있으면(`/api/todos/*` → `/todos/*`) 와일드카드가 아닌 세그먼트가 하나 이상 같아야 한다.
  - `api(url, method)`처럼 fetch를 감싼 작은 도우미 함수 호출도 요청으로 친다 (같은 파일 안에서 만든 도우미만, tree-sitter가 있을 때만. 다른 파일에서 불러온 도우미와, 도우미가 또 다른 도우미를 부르는 경우는 따라가지 않는다).
  - Python `http.server` 핸들러는 경로 표(dict)와 `self.path == …`, `.startswith(…)` 검사에서 주소를 읽는다.
- **focus**: 등급이 먼저다. 프로젝트 자기 코드 → 예제·데모·테스트 → 다른 프로젝트에서 가져온 코드(`_vendor`, `vendor`, `third_party` 등) → 연결이 없는 파일. 같은 등급에서는 `len(u)+len(b)`가 가장 큰 파일, 같으면 시작점, 그다음 경로순.
- **stack과 범례**: 한눈에 보기 줄(stack), 범례의 서버 이름, JS 영역 판단의 마지막 기준은 프로젝트 자기 코드에서만 모은다. 예제·데모·샘플·fixture·테스트 폴더(`example`, `demo`, `sample`, `fixtures` 등)의 프레임워크는 넣지 않는다.
- **요약(s, d)**: AI 없이 describe.py가 만든다. 파일은 module_doc, 역할(컴포넌트, 주소 처리, 데이터 모양, 설정, 문서, 테스트, 자동 생성 코드, 패키지 입구 파일 등)과 연결 수로, 폴더는 흔한 폴더 이름 사전(components → "화면 부품 모음" 등), 패키지 `__init__.py`의 docstring, 내용물 수로 만든다. 말투는 쉬운 해요체.
  - 라이선스·저작권 머리말, 모듈 이름만 되풀이하는 제목 줄, reST 지시문(`.. note::`)과 그 아래 들여 쓴 글, doctest(`>>>`), 필드 목록(`:param x:`, `:returns:`), URL만 있는 줄은 요약으로 쓰지 않는다. 줄바꿈으로 나뉜 첫 문장은 이어 붙인다. 소문자·숫자·괄호·따옴표·`` ` ``·역할 표시로 시작하는 줄(한글 줄 뒤의 한글 줄도)만 잇고, 대문자로 시작하는 줄과 목록 항목(`-`·`1.`로 시작하는 줄)은 새 문장으로 본다. 그래서 다음 줄이 대문자로 시작하면 문장이 중간에서 끊길 수 있다 ("JSON (JavaScript Object Notation) is a subset of."). 이어 붙일 때는 문장이 끝나는 곳까지만 읽고 줄마다 한 번만 보므로, 줄이 수만 개인 문단도 글 길이만큼만 걸린다.
  - Python docstring의 reST 역할 표시는 Sphinx가 보여 주는 글자로 바꾼다: `` :class:`.HTTPConnectionPool` `` → `HTTPConnectionPool`, `` :func:`~a.b.run` `` → `run`, `` :class:`Foo <a.Foo>` `` → `Foo`, `` :class:`!Foo` `` → `Foo`, `` :pep:`8` `` → `PEP 8`. `~`, 앞의 `.`, `!`는 코드 이름을 가리키는 역할(`class`, `func`, `meth`, `mod` 등)에서만 지우므로 `` :file:`~/.bashrc` ``는 `~/.bashrc` 그대로다. 역할 바로 뒤의 `\`는 다음 글자를 붙이기만 한다(`` :class:`Option`\s `` → `Options`). 역할로 시작하는 줄은 필드 목록이 아니라 앞 문장이 이어지는 줄로 보고 이어 붙인다("…, such as" + `` :class:`.HTTPConnectionPool` and … ``). 이런 표시는 문장의 앞 4,000자에서만 찾는다 (요약은 300자까지만 쓴다).
  - docstring 글은 다듬어서 쓴다 (모듈·폴더·루트·README, 대표 클래스·함수, 주요 함수 설명 모두). 뒤에 올 목록만 가리키는 열 단어 미만의 머리말("Spinners are from:", "Usage:", "Run the following command:", "이 모듈은 다음 기능을 제공해요:")은 버리고 다음 규칙으로 넘어간다. "…, such as:"처럼 끝나면 그 앞의 말만 남기고("For PsySH console output"), 그 밖에는 끝의 ":"나 "::"만 뗀다.
  - 분석기가 예시 앞에서 끊은 끝말("such as", "e.g.", "including", "and", "the" 등)은 지우고, 지운 뒤 세 단어가 안 되면 쓰지 않는다. Sphinx 참조 `~rich.console.Console`은 Sphinx처럼 `Console`로 쓰고, reST 링크의 주소 부분(`<https://…>`)은 뺀다.
  - README에서 "Install it with:"처럼 뒤를 가리키기만 하는 줄은 그 아래 첫 블록(들여 쓴 줄, 코드 블록, 목록)과 함께 건너뛴다.
  - 모듈 docstring이 없는 코드 파일은 정해진 역할(시작점, 테스트, 주소 처리, 데이터 모양, 화면 조각, 서버 요청, DB 연결, 도우미, 설정값, 패키지 입구 등)을 먼저 본다. 그것도 아니면 오류 파일 → 역할 폴더 → 파일 이름 → 대표 클래스·함수의 docstring → 다른 곳에서 가져온 코드 → 약한 폴더 역할 순으로 단서를 찾고, 다 없을 때만 "Python 코드"처럼 일반 문장을 쓴다.
  - 오류 파일은 오류 클래스(이름이 `Error`·`Exception`·`Warning`·`Exc`로 끝나는 클래스)가 든 `errors.py`·`exceptions.py` 같은 파일이나, 오류 클래스만 두 개 이상 있는 파일이다 ("오류 종류 정의"). 뒤의 경우는 경고 클래스(`…Warning`)만 있으면 아니다. 역할 폴더는 폴더 이름 사전과 짝인 `FILE_ROLES` 표에서 가장 가까운 폴더로 정한다 (`routes/` → "주소별 요청 처리", `vcs/` → "버전 관리 도구 연결"). `commands/` 바로 아래 파일은 "install 명령"처럼 그 명령 이름이고, `base.py`·`utils.py`처럼 명령이 아닌 이름은 빠진다. 서버 쪽 파일에는 `components`, `styles` 같은 화면 전용 이름을 쓰지 않는다. 파일 이름 단서는 몇몇 역할 폴더와 같은 이름이다 (`auth.py`, `views.py`, `services.py`, `cli.py` 등. `core.py`, `tasks.py`는 아니다).
  - 대표 클래스·함수는 파일 이름과 같은 공개 이름(`align.py` → `Align`), 파일 이름으로 시작하는 이름 중 하나뿐인 클래스(`install.py` → `InstallCommand`), 아니면 하나뿐인 공개 이름이다 (옆에 오류 클래스 하나는 괜찮다). 오류 클래스는 대표가 될 수 없다. 그 docstring을 쓰면 d에 "이 파일의 대표 클래스인 Console에 적힌 설명이에요."가 붙는다. 가져온 코드는 `vendor`, `_vendor`, `third_party` 같은 폴더 아래 파일로, "rich에서 가져온 코드"처럼 그 아래 패키지 이름을 쓴다. 약한 폴더 역할(`core`, `internal`, `features`, `modules`, 서버 쪽의 `server`·`backend`, 화면 쪽의 `client`·`frontend`·`web`·`ui`)은 바로 위 폴더만 본다.
  - 그림 파일(`.svg` 포함)은 다른 파일이 쓰면 "화면에 쓰이는 그림이나 아이콘이에요."이고, 아니면 놓인 곳을 따른다. `screenshots` 아래는 "프로그램 화면을 찍어 둔 그림이에요.", `design`·`mockup`·`wireframe` 폴더 아래는 "디자인 자료로 넣어 둔 그림이에요.", `docs` 아래는 "설명 문서와 함께 둔 그림이에요.", 테스트 폴더(`tests` 등)나 `fixtures`·`mocks` 아래는 "테스트할 때 쓰는 그림이에요.", 그 밖에 `assets`·`static`·`public`·`images`·`img`·`icons` 아래는 화면 그림 문장, 나머지는 "그림 파일이에요."다.
  - 그림 폴더: `screenshots`는 "화면 캡처 모음", `design`·`mockup`·`wireframe`(복수형 포함)은 "디자인 자료 모음"이다 (이름 목록은 `describe.NOT_CODE_FOLDERS`). 다만 서버 코드 영역의 폴더, 그리고 안에 든 코드·스타일·웹 페이지 파일을 폴더 밖의 파일이 불러 쓰는 폴더(`App.tsx`가 불러오는 `src/design/Button.tsx`)는 코드 폴더로 보고 "화면 코드 모음"처럼 영역 이름을 쓴다. 폴더 밖에서 쓰는 코드 파일의 수는 `graph._used_outside()`가 아래 폴더까지 세어 `describe_folder(used_outside=)`로 넘긴다. 폴더 안에서만 쓰는 코드(시안 `index.html`이 불러오는 자기 `app.js`)는 세지 않는다. 그런 코드 폴더 안의 그림 파일도 다른 파일이 쓰지 않으면 여전히 "디자인 자료로 넣어 둔 그림이에요."다. `docs`·테스트·`design`·`screenshots` 같은 폴더 아래의 `images`·`img`·`icons` 폴더는 d가 "설명 문서와 함께 둔 그림이 모여 있어요."처럼 놓인 곳을 말한다.
  - 이름 사전에 없는 폴더라도 바로 아래 파일이 모두 그림이고 그 아래 어디에도 코드가 없으면 "그림 모음"이고, d는 "그림 파일이 모여 있어요."로 시작한다 (`photos/`. 그림 수는 graph가 `describe_folder(images=)`로 넘긴다). 그림과 문서·설정 파일이 섞여 있으면 그대로 "문서·설정 모음"이다.
  - 숫자는 모두 천 단위 쉼표를 쓴다 ("파일 3,642개", "함수나 클래스 1,234개").
  - 주요 함수(f)는 공개 이름 → 비공개 클래스와 설명이 있는 도우미 → 나머지 비공개 순이고, 묶음 안에서는 소스 순서다.
- **무시 폴더**: `.git`, `node_modules`, `.venv`, `venv`, `env`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `dist`, `build`, `.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.cache`, `.parcel-cache`, `coverage`, `.idea`, `.vscode`, `*.egg-info`, 그리고 루트 `.gitignore`의 단순 패턴. 단 `build`, `dist`, `env`, `venv`, `coverage`는 `__init__.py`가 있으면 Python 패키지이므로 숨기지 않는다. `.gitignore`의 잘못된 패턴은 건너뛴다.
- **크기 제한**: 파일 5,000개까지 (넘으면 truncated), 파일 하나 1MB 넘으면 분석하지 않고 목록에만 넣는다 (skipped). 바이너리(이미지 등, NUL 바이트가 있는 파일 포함)는 목록에만 넣는다.
  - 탐색은 5,000개에서 멈춘다. 찾았지만 들여다보지 못한 폴더는 "파일이 너무 많아서 이 폴더 안은 다 보지 못했어요."라고 쓴다.
  - 항목이 지나치게 많은 폴더는 일부만 본다. 잘린 이유(파일 수 한도, 너무 많은 항목)에 맞는 경고를 하나만 보여 준다.
  - 폴더 깊이는 64단계까지. 열 수 없는 폴더와 파일("열 수 없는 파일", 자물쇠 아이콘)은 경고로 알린다. UTF-8로 쓸 수 없는 파일 이름은 건너뛴다.
  - 경고 문장의 개수는 모두 천 단위 쉼표를 쓴다 ("1MB가 넘는 파일 1,234개는 내용을 분석하지 않았어요.").
- **파일 읽기**: 파일 내용은 `ProjectIndex.read_text()`로 읽는다. 한 번에 `limit`바이트(기본 2,000,000)까지만 읽고, 인덱스마다 버퍼 하나를 계속 다시 쓴다 (파일마다 큰 버퍼를 새로 만들지 않는다). 글자는 UTF-8(맨 앞의 BOM은 뺀다)로 읽고, 안 되면 cp949, 그래도 안 되면 깨진 글자만 바꾼 UTF-8로 읽는다. 스캐너가 건너뛴(skipped) 파일과 바이너리는 열지 않고 `None`을 돌려준다. 열 수 없는 파일과 앞 8,192바이트에 NUL 바이트가 있는 파일(UTF-16 등)도 `None`이다. 그래서 분석기의 `project_entries`·`project_links`도 그 내용을 보지 않는다.
  - 코드 파일은 `graph.build()` 1단계(분석)에서 `cache=False`로 읽고 캐시에 남기지 않는다. 분석이 끝난 글은 1단계가 끝날 때 한꺼번에 버리되, 모아 두는 양은 16MB(`graph.TEXT_HOLD`)까지이고 그 뒤의 글은 바로 버린다. 파일마다 바로 버리면 메모리를 돌려주고 다시 받느라 빌드가 1%쯤 느려지고, 다 모아 두면 큰 코드 파일이 많을 때 메모리가 수백 MB까지 는다. DB가 연결 주소나 설정에만 보이는 라이브러리(SQLAlchemy, Django 등)를 쓰는 프로젝트 자기 코드에서 DB 이름(`sqlite:///todo.db`, `django.db.backends.postgresql`)을 찾는 일도 이때 같이 하므로, 그것 때문에 코드 파일을 다시 읽지 않는다.
  - 줄 수나 문서 제목만 필요한 파일(분석기가 없는 파일, 줄 수가 0으로 나온 파일, `.md`·`.markdown` 문서)은 5단계(노드)에서 `cache=False`로 읽고 버린다.
  - 분석기의 도우미 함수가 읽는 파일(`package.json`, HTML 페이지, README, 실행 명령 파일 등)은 캐시에 남긴다. `package.json`, HTML 페이지, README는 여러 단계가 다시 읽는다.

## 6. 서버 API

- `127.0.0.1`에만 연다. 기본 포트 8765, 사용 중이면 빈 포트를 쓴다.
- 실행할 때마다 임의 토큰을 만든다. `index.html`의 `{{CV_TOKEN}}`을 토큰으로 바꿔서 `<meta name="cv-token" content="…">`로 전달한다. 화면은 모든 `/api/` 요청에 `X-CV-Token` 헤더를 붙인다.
- Host 헤더는 정확히 하나여야 하고 `127.0.0.1:포트` 또는 `localhost:포트`(대소문자 무시)여야 한다. 아니면 403 (DNS 리바인딩 방지). `/api/`에 토큰이 없거나 틀리면 403 (`hmac.compare_digest`로 비교).
- CSP: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'`. 아이콘까지 앱 안에서 제공하므로 다른 사이트에는 아무것도 요청하지 않는다. index.html에는 인라인 `<script>`를 쓰지 않는다.

| 요청 | 응답 |
|---|---|
| `GET /` | index.html (토큰 삽입) |
| `GET /app.js`, `/app.css`, `/icons.css` | 정적 파일 (web 폴더의 정해진 파일만) |
| `GET /api/analyze?path=<절대 경로>` | 그래프 JSON. 오류는 4xx `{"error": "한국어 메시지"}` |
| `GET /api/analyze?demo=1` | 예제 프로젝트 그래프. `source`는 `{"kind": "demo", "path": ""}`, 최근 목록에 넣지 않음 |
| `POST /api/pick-folder` | `{"path": "C:\\…"}` 또는 취소 시 `{"path": null}` (tkinter 폴더 선택 창) |
| `GET /api/recent` | `{"items": [{"path", "name", "at"}]}` 최근 10개 |
| `GET /api/ai/status` | `{"provider": "none", "available": false, "message": "…"}` |
| `POST /api/explain` | 뼈대. 공급자가 없으면 `{"status": "not_configured", "message": "…"}` |

사용자 데이터 폴더: `%LOCALAPPDATA%\CodeVision` (다른 OS는 `~/.codevision`). 환경 변수 `CODEVISION_HOME`이 있으면 그 폴더를 쓴다 (테스트용). 경로를 정하는 함수는 `codevision.explain.data_dir()`다.

추가 약속:

- 모든 응답(오류 포함)에 `Cache-Control: no-store`(index.html도 실행할 때마다 토큰이 바뀌므로), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`, CSP를 붙인다. `Server` 헤더는 `CodeVision`만 쓴다 (Python 버전을 알리지 않음).
- 오류 응답은 모두 한국어 JSON이다. http.server가 먼저 거절하는 요청(400, 414, 431, 501, 505)도 같다.
  - GET/POST가 바뀐 요청은 405와 `Allow` 헤더. 토큰 검사가 먼저라서 토큰 없이는 어떤 메서드가 되는지도 알 수 없다. 그 밖의 메서드(PUT, DELETE, OPTIONS 등)는 501.
  - 요청 본문: `Content-Length`가 숫자가 아니거나 여러 값이 다르면 400, `Transfer-Encoding`은 411, 1MB(`MAX_BODY`)가 넘으면 읽지 않고 413, 덜 오면 400.
  - 60초 동안 아무것도 보내지 않는 연결은 끊는다.
  - `path`는 절대 경로만 받는다. 비었거나, 4,096자(`MAX_PATH_CHARS`)가 넘거나, NUL이 들어 있거나, 상대 경로(`C:` 포함)면 400. UNC(`\\server\share`)와 `\\?\` 경로는 받는다.
  - 분석 중 예외는 500과 정해진 한국어 문장(`ANALYSIS_FAILED`)만 돌려준다. 예외 내용과 traceback은 응답에 넣지 않고 서버 실행 기록(stderr)에만 남긴다.
- 분석은 요청마다 스레드에서 돈다 (`ThreadingHTTPServer`, daemon 스레드). 오래 걸리는 분석이 다른 요청을 막지 않는다.
- 콘솔 출력: 표준 출력이 터미널이 아니면(파일·파이프로 받을 때) UTF-8로 쓴다. 포트가 사용 중이면 "{요청 포트}번 포트가 사용 중이라 {실제 포트}번으로 열었어요"를 먼저 쓴다.
- `recent.json`과 `/api/recent`의 `at`은 epoch 초(float)로, 그래프의 `generated`와 같은 형식이다.

## 7. AI 설명 뼈대

공급자(로컬 AI 또는 상용 LLM)는 아직 정하지 않았다. 지금은 인터페이스와 캐시만 있다.

- `explain/base.py`: `Explainer` 인터페이스 (`name`, `available()`, `explain_file(ctx)`, `explain_folder(ctx)`)와 입력·출력 데이터 클래스
- `explain/providers.py`: `get_explainer(config)`. 지금은 `NoneExplainer`(설명 안 함)만 있다. "local"과 "cloud" 자리는 표시만 해 둔다.
- `explain/cache.py`: SQLite 캐시. 키 = 공급자·모델·프롬프트 버전·파일 blob SHA-1 (git과 같은 방식이라 GitHub 파일과도 맞는다)
- `explain/prompts.py`: 공급자와 무관한 한국어 프롬프트 초안 (출력 형식 `{"s", "d", "f": {표시: 설명}}`)
- `explain/__init__.py`: `enrich(graph, explainer, cache, read_source)`. 공급자가 없으면 그래프를 그대로 두고 `graph["ai"]`만 채운다.
- `read_source(rel_path)`는 텍스트가 아니라 원본 바이트(또는 None)를 돌려준다. blob SHA를 정확한 바이트로 계산해야 git·GitHub와 맞기 때문이다. 글자로 바꾸는 일은 `enrich()`가 한다.
- 캐시 키에는 종류(`file` 또는 `folder`)도 들어간다. 폴더 설명의 키는 폴더 정보와 자식 요약으로 만든 SHA라서, 자식 요약이 바뀌면 다시 만든다.
- 공급자를 붙일 때 주의: `.env*` 파일처럼 비밀이 들어 있을 수 있는 파일의 내용은 절대 AI에 보내지 않는다. 트리에는 보이지만 설명 요청에서는 뺀다.

## 7-1. 화면 배치 (트리)

- 아코디언: 깊이마다 폴더 한 갈래만 펼친다.
- 보이는 줄(루트의 자식, 펼친 폴더들의 자식)만 배치를 계산한다. 위에서부터 한 형제 묶음씩 넣어 보고, 들어가는 동안은 위에서 아래로 내려가는 트리로 그린다.
- 크기는 기본 크기로 먼저 해 보고, 작은 크기가 더 많은 줄을 트리로 그릴 수 있을 때만 작은 크기를 쓴다. 그래서 폴더를 펼치다 보면 동그라미 크기가 조금 바뀔 수 있다.
- 한 줄에 다 들어가지 않는 형제 묶음은 여러 줄로 나눈다 (줄바꿈 트리). 파일은 위쪽 줄(최대 3줄)에, 폴더는 모두 마지막 줄에 둔다. 줄은 최대 4줄이고, 줄마다 가로선이 있고 왼쪽 세로선이 줄들을 잇는다. 그림 폭이 560px보다 좁으면(휴대폰) 줄바꿈하지 않는다.
- 줄바꿈으로도 안 되는 첫 묶음부터 그 아래는 부모 밑에 매달린 들여쓰기 목록으로 그리고, 그 위의 줄은 트리로 유지한다 (혼합 배치).
  - 부모가 너무 오른쪽에 있어서 목록 글자가 들어갈 자리(약 140px 또는 이름·배지 폭)가 없으면, 목록을 필요한 만큼만 왼쪽으로 옮긴다. 부모의 선은 반 줄 높이에서 꺾여 목록의 세로선으로 이어진다.
- 루트의 자식부터 안 들어가면(휴대폰 등) 전체를 들여쓰기 목록으로 그린다. 480px 미만에서는 작은 크기를 쓴다.
- 키보드: 트리 부분에서는 ←→ 형제(줄바꿈된 묶음은 줄 순서대로), ↑ 부모, ↓ 첫 자식. 목록 부분에서는 ↑↓ 목록 순서(첫 항목에서 ↑는 부모, 마지막 항목에서 ↓는 멈춤), ← 부모, → 첫 자식.
- 패널: 1024px 이상에서는 그림 옆에 붙어서 스크롤해도 보인다 (페이지 최대 폭 1480px). 그보다 좁으면 그림 아래에 있고, 파일을 누르면 패널이 보이도록 스크롤한다. 패널 안의 파일 칩을 누르면 패널이 화면에서 있던 자리를 지킨다 (옆 패널일 때는 새로 고른 파일이 보이게 한다). 처음 열 때 고른 파일이 화면 밖이면 보이게 스크롤한다.
- 패널 칩: 같은 목록에 이름이 같은 파일이 있으면 부모 폴더 이름을 앞에 붙인다 (최상위 파일은 프로젝트 이름). 전체 경로는 툴팁으로 보이고, 칩은 보이는 이름순이다.
- 패널 글: 설명(`d`)이 한 줄 요약(`s`)으로 시작하면(끝의 "…"나 "..."는 빼고 비교) 굵은 요약 줄은 넣지 않는다. 주요 함수는 항목마다 이름 아래에 설명을 조금 들여 쓰고, 설명이 없으면 이름만 둔다.
- 개수: 제목 줄("폴더 · 파일 · 시작점 · 정리 후보"), 트리의 배지("연결 1,200개", "1,200개 항목"), 패널의 항목 수·줄 수는 모두 천 단위 쉼표를 쓴다.
- AI 칩: 안내 문구가 있으면 버튼이다. 누르면(Enter, Space 포함) 머리줄 아래에 `#cv-aim` 한 줄이 열리고 닫힌다.
- 경로 표시: 분석할 폴더의 경로(머리줄의 출처 줄, 시작 화면의 최근 목록, 오류 화면의 메시지 속 경로와 경로 줄)는 모두 `pathHtml()`로 그린다. `\`나 `/` 바로 뒤에서만 줄을 바꾸고, 한 조각이 줄보다 길 때만 그 안에서 끊는다 (조각마다 `display:inline-block`, 조각 사이에 `<wbr>`). 조각 앞의 공백은 조각 밖에 두어서 일반 글처럼 보이고 복사된다 (inline-block 맨 앞의 공백은 브라우저가 지운다). 조각이 따로 상자라서 페이지 안 찾기(`window.find`)는 `\`·`/`를 사이에 둔 글을 찾지 못하고 조각 하나 안의 글만 찾는다 (Ctrl+F 찾기 막대로는 확인하지 않았다).
- 머리줄: 출처 줄의 "로컬 폴더 · 경로"는 한 덩어리 글이라 경로가 "로컬 폴더 ·" 바로 뒤에 이어진다. 숨긴 폴더는 앞의 두 이름만 쓰고, 그보다 많을 때만 " 등"을 붙인다 ("__pycache__ 숨김", "__pycache__, dist 등 숨김"). 숨긴 이름 전체는 툴팁으로 보인다.
- 글자 줄바꿈: 한국어 문장 칸(시작 화면 안내, 오류 메시지, 경고, 한눈에 보기, 범례, 패널의 요약·설명·주요 함수 설명 등)은 낱말 안에서 줄을 바꾸지 않는다(`word-break: keep-all`). 띄어쓰기 없이 줄보다 긴 글만 아무 데서나 끊는다. 경로와 파일 이름 칸(출처 줄, 최근 목록, 오류 화면의 경로 줄, 패널의 경로·이름·파일 칩 등)에는 이 규칙을 쓰지 않는다. 오류 메시지 속 경로(`.er-p`)는 메시지 칸의 `keep-all`을 물려받지만, 조각 사이에서 줄을 바꾸는 것은 같아서 줄보다 긴 조각 안에서만 차이가 난다.
- 화면이 바뀔 때 포커스: 프로젝트를 열면 프로젝트 제목, "다른 폴더"를 누르면 "폴더 선택…" 버튼, 오류 화면이면 "다시 시도"로 옮긴다. 처음 화면을 불러올 때는 옮기지 않는다.
- 시작 화면: 최근 목록의 버튼은 접근성 이름(`aria-label`)을 "이름 경로" 한 덩어리로 받는다. 조각으로 나뉜 경로에서 이름을 만들게 두면 Chromium이 조각 사이를 띄어서 "C:\ Users\ …"처럼 만들기 때문이다.
- 오류 화면: 메시지가 연 경로를 이미 말하면(서버의 "폴더를 찾을 수 없어요: C:\…") 아래의 경로 줄은 넣지 않고, 메시지 속 경로만 경로 표시로 그린다. 경로 줄은 메시지가 경로를 말하지 않을 때만 보인다.
- 잘림 경고는 서버가 보낸 한 줄만 보여 준다. 그래프가 잘렸는데 경고가 없을 때만 화면이 일반 문장을 넣는다.
- `design/mockup.html`은 승인된 디자인 원본이다. 앱은 실제 프로젝트에 맞추느라 그 뒤에 다음을 더하거나 바꿨다.
  - 배치: 줄바꿈 트리, 혼합 배치, 꺾인 선, 옆 패널.
  - 패널과 글자: 천 단위 쉼표를 쓰는 개수(시안 코드는 숫자를 그대로 붙인다), 이름 아래에 들여 쓴 주요 함수 설명(시안은 이름 옆에 이어 쓴다), 설명과 겹치면 빠지는 굵은 요약 줄(시안은 늘 보인다), 한 덩어리로 이어지고 경로를 `\`·`/` 뒤에서만 끊는 출처 줄(시안은 조각마다 따로 놓인다), 숨긴 폴더가 두 개 이하면 " 등"을 빼는 숨김 표시(시안은 늘 "… 등 숨김"), 낱말을 끊지 않는 한국어 줄바꿈.
  - 시안에 없는 화면: `mockup.html`에는 시작 화면(최근 목록 포함)과 오류 화면이 없다. 그래서 그 화면의 경로 표시(최근 목록 `.rc-p`, 오류 메시지 속 `.er-p`, 경로 줄 `.er-path`)와 최근 목록 버튼의 `aria-label`도 앱에만 있다.

## 8. 예제 프로젝트 기대 결과 (`src/codevision/demo/my-todo-app`)

- 폴더 5개 (`backend`, `backend/routes`, `frontend`, `frontend/src`, `frontend/src/components`), 파일 15개
- 연결 (u) 12개:
  - `frontend/index.html` → `frontend/src/main.jsx` (`<script src>`)
  - `frontend/src/main.jsx` → `frontend/src/App.jsx`
  - `frontend/src/App.jsx` → `frontend/src/components/TodoList.jsx`
  - `frontend/src/components/TodoList.jsx` → `frontend/src/api.js`, `frontend/src/components/TodoItem.jsx`
  - `frontend/src/components/TodoItem.jsx` → `frontend/src/api.js`
  - `frontend/src/api.js` → `backend/routes/todos.py` (HTTP, 양쪽 h)
  - `backend/main.py` → `backend/database.py`, `backend/routes/todos.py`
  - `backend/routes/todos.py` → `backend/database.py`, `backend/models.py`
  - `backend/models.py` → `backend/database.py`
- 시작점: `frontend/src/main.jsx` (index.html에서 불러옴, createRoot), `backend/main.py` (FastAPI 앱 생성)
- 정리 후보: `helpers_old.py`
- stack: 화면 React, 서버 FastAPI, DB SQLite
- focus: `backend/routes/todos.py`

## 9. 테스트

```
.venv\Scripts\python -m pytest -q
```

`pyproject.toml`의 pytest 설정이 `src`를 import 경로에 넣는다.
