# CodeVision 구조와 모듈 계약

이 문서는 모듈 사이의 약속(계약)이다. 모듈을 고칠 때 이 문서와 `src/codevision/model.py`가 기준이다.

## 1. 흐름

```
폴더 경로
  → scan.scan()            파일·폴더 목록 (무시 규칙 적용) → ProjectIndex
  → lang.<언어>.analyze()  파일마다 FileFacts (import, 함수, HTTP, 시작점 단서)
  → lang.<언어>.resolve()  import → 프로젝트 안의 파일 경로
  → graph.build()          연결(u/b/h), 시작점, 정리 후보, 영역, 요약 → 그래프 JSON
  → AIService.annotate()   graph["ai"](AI 상태), AI가 켜져 있으면 캐시에 있는 AI 설명(x)과
                           AI에 보내지 않는 파일 표시(xs). 모델은 부르지 않는다
  → server                 /api/analyze 로 JSON 전달
  → web/app.js             트리 도식과 패널 그리기
```

AI 설명은 이 흐름 밖에서 만든다. 화면이 `/api/ai/explain`(고른 항목 하나)이나 `/api/ai/explain-all`(전체 설명)로 요청하면, 서버의 AI 작업 스레드가 이 컴퓨터의 Ollama에 묻고 답을 캐시에 넣는다 (6장, 7장).

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
| `src/codevision/explain/` | 이 컴퓨터의 로컬 AI(Ollama)로 만드는 AI 설명 (7장). `__init__.py`는 아래 모듈의 공용 이름과 `status()`, `MAX_SOURCE_BYTES`를 둔다 |
| `src/codevision/explain/service.py` | `AIService`(서버마다 하나): AI 상태, 설정 저장, Ollama 켜기, 모델을 부르는 작업 스레드 하나, 전체 설명 작업, `/api/analyze`에 캐시된 설명(`x`)과 AI에 보내지 않는 파일 표시(`xs`)를 붙이는 `annotate()` |
| `src/codevision/explain/ollama.py` | 공급자 `local`: 이 컴퓨터 주소만 부르는 Ollama 클라이언트(`urllib`), `OllamaExplainer`, 설치된 Ollama 찾기와 켜기(`ollama serve`) |
| `src/codevision/explain/safety.py` | 비밀 파일 거르기, 보내는 글 속의 비밀 값 가리기, 노드 id를 분석한 폴더 안의 파일로만 읽기 |
| `src/codevision/explain/prompts.py` | 프롬프트, 답 형식(JSON schema), 답 읽기와 다듬기, 추천 모델 순서, 모델 옵션 |
| `src/codevision/explain/cache.py` | AI 설명 SQLite 캐시 (`cache.sqlite`) |
| `src/codevision/explain/providers.py` | AI 설정 파일(`config.json`) 읽고 쓰기, 공급자 고르기, `NoneExplainer` |
| `src/codevision/explain/base.py` | `Explainer` 인터페이스, 입력·출력 데이터 클래스(`FileContext`, `FolderContext`, `Explanation`), `AIError`, `data_dir()` |
| `src/codevision/explain/messages.py` | AI 쪽이 화면에 보여 주는 한국어 문장 모음 (서버와 화면이 같은 말을 쓴다) |
| `src/codevision/server.py` | 로컬 웹 서버 (표준 라이브러리 `http.server`). `/api/ai/*`는 `explain.AIService`가 처리한다 |
| `src/codevision/__main__.py` | 실행 진입점 `python -m codevision` |
| `src/codevision/web/` | 화면: `index.html`, `app.js`(트리, 옆 패널, AI 패널), `app.css`, `icons.css`(아이콘. 인터넷 없이도 보이도록 앱 안에 들어 있음) |
| `src/codevision/demo/my-todo-app/` | 예제 프로젝트 (화면의 "예제 프로젝트 열기"와 테스트에 사용) |
| `tests/` | pytest 테스트, `tests/fixtures/sample-graph.json`은 화면 개발용 예시 그래프 (AI 상태 블록과 `x` 예시 포함) |
| `tests/fake_ollama.py` | 테스트와 화면 확인에 쓰는 가짜 Ollama 서버. 모델 없이 Ollama 0.32와 같은 모양으로, 프롬프트에서 만든 해요체 답을 준다. 테스트가 불러 쓰고, `python tests/fake_ollama.py --port N`으로 따로 띄울 수도 있다 |

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
def project_loaded(index: ProjectIndex) -> dict[str, str]      # rel_path -> 한국어 문장. 프레임워크가 import 없이
                                                                # 알아서 불러오는 파일 (Nuxt pages/, +page.svelte)
```

- 등록부(`lang/__init__.py`)는 분석기를 처음 쓸 때 한 번만, 잠금을 걸고 불러온다. 서버가 여러 요청을 동시에 분석해도 반쯤 채워진 목록을 보는 일이 없다.
- `analyze`는 예외를 던지지 않는다. 문법 오류면 `parse_error=True`로 할 수 있는 만큼만 채운다.
  - Python 분석기는 긴 `elif` 사슬, 한 단어짜리 줄이 아주 많은 docstring, 긴 문자열 더하기(`"a" + "b" + …`)를 재귀 대신 반복문으로 읽는다. 그래서 Python으로는 문제없는 이런 파일을 문법 오류로 세지 않는다.
  - 그래도 구문 트리(AST)를 따라가다 어느 최상위 문장에서 `RecursionError`가 나면 그 문장만 건너뛰고, 나머지 문장에서 찾은 것은 그대로 둔다 (`parse_error`는 False).
- `resolve`는 프로젝트 안의 파일 경로 목록을 돌려준다. 외부 패키지(react, fastapi 등)는 빈 목록이다.
  `from routes import todos, users`처럼 한 import가 여러 파일을 가리킬 수 있다.
- `project_entries`는 파일 하나만 봐서는 모르는 시작점을 찾는다 (package.json, index.html, pyproject.toml 등).
- `project_loaded`는 프레임워크가 정해진 이름과 자리만 보고 알아서 불러오는 파일과, 어떻게 불러오는지 말하는 한국어 문장이다 ("Nuxt가 화면에 쓰인 이름을 보고 알아서 불러와요."). 그중 앱이 처음 띄우는 화면은 `project_entries`에도 있다 (5장 시작점, 정리 후보).
- JavaScript 분석기는 `.vue`·`.svelte` 파일도 맡는다. 맨 바깥의 `<script>` 블록(Vue `<script>`·`<script setup>`, Svelte 인스턴스 스크립트와 `<script context="module">`·`<script module>`)만 코드로 읽고, 나머지는 줄바꿈만 남긴 채 빈칸으로 바꿔서 줄 번호가 원래 파일과 같다. 블록끼리는 이름을 함께 쓰므로 한 프로그램으로 읽는다. 블록을 찾는 스캐너는 HTML 주석, 따옴표 속 속성값, `<script>`·`<style>`의 내용, Vue의 사용자 정의 블록(`<i18n>`, `<docs>`)과 HTML이 아닌 템플릿(`<template lang="pug">`)의 내용, 템플릿 식(Vue `{{ }}`, Svelte `{ }`)을 건너뛰고 요소를 쌓아 가며 세므로, 그 안에 쓴 `<script` 글자나 `<svelte:head>` 안의 `<script>`는 블록이 아니다. 닫히지 않은 `{`가 많은 깨진 파일도 파일 길이의 몇 배만 읽는다. `lang="ts"`·`"tsx"`면 TypeScript로 읽고(`language`도 typescript), `lang="coffee"` 같은 다른 언어와 `type="application/ld+json"` 같은 데이터는 읽지 않는다. Vue의 맨 바깥 블록에 쓴 `src`(`<script src>`, `<template src>`, `<style src>`, `<i18n src>`)는 side-effect import다. 파일의 `frameworks`에는 늘 `vue`나 `svelte`가 들어간다. 정규식 대체는 줄 맨 앞의 선언을 찾으므로 블록마다 모든 줄에 공통인 들여쓰기를 떼고 읽는다.
- 모든 경로는 프로젝트 기준 POSIX 경로다 (`frontend/src/api.js`). 루트 폴더는 `""`.

### FileFacts 필드 (model.py)

| 필드 | 뜻 |
|---|---|
| `imports` | `ImportRef(spec, names, kind, level, line)`. kind: import, from, require, dynamic, reexport, side-effect, glob. 이름의 일부만 코드에 적혀 있으면 `spec`은 모르는 부분을 `*`로 쓴 패턴이고(`` import(`./locales/${lang}.json`) `` → `./locales/*.json`, `require("./plugins/" + name)` → `./plugins/*`, `import_module(f"pkg.plugins.{name}")` → `pkg.plugins.*`), 맞는 파일 모두를 가리킨다 (`model.is_pattern()`). 이런 import에는 `names`가 없다. glob은 코드가 패턴을 직접 쓴 것이다: Vite `import.meta.glob("./pages/*.vue")`의 `spec`은 첫 패턴이고 `names`는 같은 호출의 다른 패턴(`!`로 시작하면 빼는 패턴), webpack `require.context("./dir", true, /\.js$/)`와 `import.meta.webpackContext()`는 `./dir/**`(하위 폴더를 안 보면 `./dir/*`)이고 `names`에 정규식(`/\.js$/`)이 있다 |
| `symbols` | 패널 "주요 함수"에 보일 최상위 함수·클래스·컴포넌트. `signature`는 화면에 그대로 보이는 글자 (`addTodo(title)`, `class Todo`). `bases`는 클래스가 물려받는 클래스마다 이름의 마지막 부분이다 (`class SMTPSenderRefused(SMTPResponseException)` → `SMTPResponseException`, `class Refused(errors.SendError)` → `SendError`. `Generic[T]`는 `Generic`, 함수 호출로 만든 것은 빠진다). JavaScript는 `extends` 뒤의 클래스다 (`class NotFound extends errors.HttpError` → `HttpError`, `class Box<T> extends Base<T>` → `Base`. `extends mixin(Base)`처럼 호출로 만든 것과 `extends parts[0]`은 빠진다) |
| `http_calls` | 화면 쪽 요청 (fetch, axios 등). `path`는 `normalize_http_path()` 결과 |
| `http_routes` | 서버 쪽 주소 정의 (FastAPI, Flask, Express, `http.server` 핸들러의 경로 표 등). `HttpRoute(method, path, handler, line, base, router)`. `path`도 정규화. `base`는 라우트가 이 파일에서 속한 맨 바깥 라우터(Blueprint, APIRouter) 자신의 prefix를 정규화한 것이고(없으면 `""`), 다른 파일의 Flask `url_prefix`가 이 앞부분을 대신한다. `router`는 라우트가 이 파일에서 속한 라우터의 변수 이름이다(`@todos_router.get` → `todos_router`, `router.get` → `router`, Fastify 플러그인 함수는 그 함수를 내보낸 이름, `module.exports = async function …`이면 `default`). 이 파일의 다른 라우터에 붙인 라우터의 라우트는 맨 바깥 라우터의 이름을 갖고, 두 라우터 아래에 붙었으면 라우트도 둘이다. 모르면(파일 경로가 곧 주소인 라우트, `this.router.get`) `""` |
| `http_mounts` | 다른 파일의 라우터를 주소 앞부분(prefix) 아래에 붙이는 곳. `HttpMount(prefix, target, line, base, replace, base_own, router, owner)`. Express/Koa `app.use("/api", router)`, Hono `app.route`, Fastify `register(..., {prefix})`, FastAPI `include_router(..., prefix=)`, Flask `register_blueprint(..., url_prefix=)`. prefix 없이 앱에 붙인 것(`app.use(router)`, `include_router(router)`)도 기록한다 (5장 HTTP 연결). `base`는 대상을 붙인 쪽 라우터가 이 파일에서 응답하는 prefix (앱에 붙였으면 `""`). `replace`는 Flask `url_prefix`라서 대상 Blueprint 자신의 prefix(라우트의 `base`)를 대신하면 True. `base_own`은 `base`의 앞부분 중 맨 바깥 라우터 자신의 prefix로, 이 파일을 다른 파일이 Flask `url_prefix`로 붙이면 이 부분만 그 prefix로 바뀐다. 붙인 쪽 라우터가 이 파일에서 두 곳 이상에 붙어 있으면(`/v1`과 `/v2`) 그 자리마다 `HttpMount`가 하나씩 있다. `router`는 붙인 라우터가 `target` 모듈에서 갖는 이름이다: JavaScript는 내보낸 이름(`import { users as people }`과 `app.use("/p", people)` → `users`, 기본 import·`require()`·`import()` → `default`, `routes.todos`처럼 모듈에서 꺼내면 `todos`, 같은 파일의 함수가 만든 것(`makeRouter(db)`)은 `""`), Python은 import 뒤에 적은 이름들이다(`include_router(routes.todos_router)` → `todos_router`, `from routes import todos`와 `todos.router` → `todos.router`. 하위 모듈 todos의 `router`일 수도, routes의 라우터 `todos`일 수도 있어서 그래프가 고른다). `""`이면 모듈 전체다. `owner`는 대상을 붙인 이 파일의 맨 바깥 라우터 이름(라우트의 `router`와 같은 이름, 앱에 붙였으면 앱의 변수)이다. `replace`와 `base_own`은 Python 분석기만 채운다. JavaScript에는 Flask `url_prefix`처럼 자기 prefix를 대신하는 방식이 없으므로 JavaScript 분석기의 `replace`는 늘 False, `base_own`은 늘 `""`이다. `prefix`, `base`, `base_own`은 쓴 글자 그대로이고(모르는 부분은 `${}`) 붙일 때 정규화한다 |
| `exports` | 다른 파일이 이 파일에서 import해 쓰는 라우터·요청 도우미·Fastify 플러그인과, 다른 모듈에서 받아 그대로 내보내는 이름. `ExportName(name, local, source, original)`. `local`은 그 이름이 가리키는 이 파일의 변수다(`export { router as todos }` → `todos`가 `router`, `module.exports = router` → `default`가 `router`, `export default async (url) => …`처럼 내보내면서 쓴 값은 내보낸 이름 그대로 `default`). 받아 넘기는 이름은 `source`(그 모듈의 ImportRef)와 `original`(거기서의 이름)이다: `export { default as todos } from "./todos"`, `export * from "./api"`(`name`과 `original`이 `*`), `module.exports = { todos: require("./todos") }`, `import { api } from "./api"; export { api }`. JavaScript 분석기만 채운다(Python 모듈의 이름은 변수 이름 그대로다) |
| `http_helpers` | 다른 파일이 부르는 요청 도우미. `HttpHelper(name, base, url_at, method, method_at, method_default, options_at, client, line)`. fetch의 주소가 매개변수 하나(`url_at`번째)이고 그 앞에 `base`(`BASE + path`의 BASE 값, `/api${url}`의 `/api`, 실행할 때만 아는 값은 `${}`)가 붙는 함수다. method는 정해진 값이거나, `method_at`번째 인수(빠지면 그 매개변수의 기본값 `method_default`), `options_at`번째 인수인 fetch 옵션 객체의 `method`에서 온다. 객체에 쓴 함수(`export const api = { get(path) { … } }`)는 `name`이 `api.get`이다. `client`는 `axios.create({ baseURL })`로 만든 클라이언트로, `base`가 baseURL이고 `get`·`post`·`put`·`patch`·`delete`의 첫 인수가 주소다. 내보낸 것만 들어 있고, tree-sitter가 있을 때만 찾는다 |
| `helper_calls` | 다른 프로젝트 파일에서 import한 도우미를 부르는 곳. `HelperCall(target, name, member, args, options, line, guess, symbols)`. `target`은 그 import, `name`은 그 모듈에서의 이름(`import { api as send }` → `api`, 기본 import·`require()` → `default`, `import * as lib`와 `lib.request(…)` → `request`), `member`는 그 위에서 부른 메서드(`http.get(…)` → `get`). `args`는 인수마다의 주소 글자(모르는 부분은 `${}`, 글자가 아니면 None), `options`는 옵션 객체 인수가 정한 method(없으면 `GET`, 모르면 `ANY`)다. 주소처럼 보이는 인수가 있는 호출만 있다. `guess`는 이 파일이 혼자 읽어 둔 요청(`http.get("/todos")`을 base 주소 없이 읽은 것)이고, `symbols`는 이 호출을 요청 힌트로 보여 줄 함수 이름이다. tree-sitter가 있을 때만 찾는다 |
| `plugin_routes`, `plugin_mounts` | 서버 프레임워크를 import하지 않는 파일에서 내보낸 Fastify 플러그인 함수의 첫 매개변수로 만든 라우트와 마운트 (`module.exports = async function (fastify) { fastify.get(…) }`). 그래프가 프로젝트에서 Fastify를 쓰는 것을 확인했을 때만 `http_routes`, `http_mounts`에 넣는다 (5장) |
| `frameworks` | import에서 찾은 프레임워크·라이브러리의 표준 이름 (아래 표). 패턴 import는 세지 않는다. import가 없어도 `.vue`는 `vue`, `.svelte`는 `svelte`, `nuxt.config.*`는 `nuxt`, `next.config.*`는 `next`다 |
| `is_entry`, `entry_reason` | 파일 안의 시작점 단서 (`if __name__ == "__main__"`, `app = FastAPI()`, `createRoot(...).render`, `app.listen`) |
| `module_doc` | 모듈 docstring이나 맨 위 주석에서 요약으로 쓸 첫 문장. 줄바꿈으로 나뉜 문장은 이어 붙인다 (5장 요약 참고) |

### frameworks 표준 이름

- 화면: `react`, `vue`, `svelte`, `next`, `nuxt`, `sveltekit`, `angular`. `nuxt`는 `nuxt`·`#app`·`#imports`에서, `sveltekit`은 `@sveltejs/kit`과 그 어댑터(`@sveltejs/adapter-*`), `$app/…`에서 온다. 한눈에 보기 줄에는 이 순서로 "Vue, Nuxt", "Svelte, SvelteKit"처럼 보인다
- 서버: `fastapi`, `flask`, `django`, `starlette`, `aiohttp`, `express`, `koa`, `fastify`, `nestjs`, `hono`, `http.server` (점이 들어간 채로 쓰는 유일한 이름). `fastify`는 `fastify`, `fastify-plugin`, `@fastify/…`에서 온다
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
  "ai": {"provider": "local", "enabled": true, "available": true, "model": "qwen2.5:7b",  // AI 상태 블록 (아래)
         "models": [{"name": "qwen2.5:7b", "size": 4683087332, "params": "7.6B"}, {"name": "gemma3:4b", "size": 3338801804, "params": "4.3B"}],
         "recommended": ["qwen2.5:7b", "gemma3:4b"], "ollama": "running", "can_start": false,
         "message": "이 컴퓨터의 AI(qwen2.5:7b)로 설명을 만들어요."},
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
      "h": ["backend/routes/todos.py"], "lang": "javascript",
      "x": {"s": "서버와 주고받는 함수 모음", "d": "…", "f": [["fetchTodos()", "서버에서 할 일 목록을 가져와요."]]}  // AI 설명 (AI가 켜져 있을 때만)
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
| `x` | AI 설명 `{"s", "d", "f": [[표시, 설명], …]}` | AI가 켜져 있고, 지금 모델의 설명이 캐시에 있을 때만 (아래) |
| `xs` | AI에 보내지 않는 코드 파일의 까닭: `secret` / `too_big` / `unreadable` | AI가 켜져 있을 때 코드 파일에만. `x`와 함께 있지 않다 (아래) |

id는 프로젝트 기준 POSIX 경로이고 루트만 `"."`이다. 파일 이름에 HTML 문자가 들어 있을 수 있으므로 화면은 모든 글자를 이스케이프한다.

추가 약속:

- 파일 노드에는 `f`, `u`, `b`, `h`가 항상 있다 (빈 목록일 수 있음). `e`, `o`, `i`, `lang`, `x`, `xs`는 해당될 때만 있다 (`x`는 폴더에도 붙는다).
- `source.kind`는 `local`(로컬 폴더), `demo`(예제 프로젝트, path는 빈 문자열), 나중에 `github`.
- 바이너리 파일(이미지 등)은 크기와 상관없이 목록에만 들어가고 `skipped`에 세지 않는다. `skipped`는 1MB가 넘어 분석하지 못한 텍스트 파일 수다. 바이너리·건너뛴 파일의 `l`은 0이다.
- docstring에서 가져온 글(모듈 docstring, 모듈 docstring이 없는 파일에서 쓰는 대표 클래스·함수의 docstring, 주요 함수 설명)은 번역하지 않고 쓰므로, 영어 docstring은 AI 설명(`x`)이 없는 동안, 그리고 `x`에서 AI가 비워 둔 함수 설명과 `s`·`d`(다듬다가 비운 것 포함. 규칙 기반 글이 대신한다)에서 영어로 보인다. 모듈 docstring은 역할 추측보다 먼저라서, 영어 모듈 docstring이 있는 `exceptions.py`에는 "오류 종류 정의" 대신 그 영어 문장이 보인다.

AI 상태 블록 (`ai`. 6장의 `GET /api/ai/status`, `POST /api/ai/settings`, `POST /api/ai/start` 응답과 같다):

| 키 | 뜻 |
|---|---|
| `provider` | 설정의 공급자. `local`(이 컴퓨터의 Ollama)이 아니면 모두 `none`이다 (`none`, 아직 만들지 않은 `cloud`) |
| `enabled` | 사용자가 AI 설명을 켰는지. 공급자가 `local`일 때만 true |
| `available` | 켜져 있고, Ollama가 답하고, 고른 모델이 설치되어 있는지. 화면은 이것이 true일 때만 스스로 설명을 묻는다 |
| `model` | 쓰는 모델. 사용자가 고르지 않았으면 CodeVision이 고른 모델(7장), 아직 고르지 못했으면 `""` |
| `models` | 설치된 모델 `[{"name", "size"(바이트), "params"}]`. 추천 모델 먼저, 그다음 이름순. Ollama가 답하지 않으면 `[]` |
| `recommended` | 추천 순서 `prompts.RECOMMENDED_MODELS` |
| `ollama` | `running`, `stopped`(설치는 되어 있음), `missing`(찾지 못함) |
| `can_start` | 설치되어 있고, 꺼져 있고, 주소가 이 컴퓨터라서 `POST /api/ai/start`로 켤 수 있는지 |
| `message` | AI 칩과 AI 패널에 보일 한국어 한 문장 (`explain/messages.py`) |

AI 설명 (`x`, `xs`):

- `x`는 AI 설명이다. `/api/ai/explain`의 답과 작업 블록의 `results`에도 같은 모양으로 온다.
  - `/api/analyze`에서는 AI가 켜져 있을 때만, 지금 모델(`ai.model`)의 설명이 캐시에 있는 노드에만 붙는다. 분석은 모델을 부르지 않는다.
  - `x.f`는 노드의 `f`와 같은 표시를 같은 순서로 갖는다. AI가 설명하지 않은 함수는 규칙 기반 설명이 들어가고, AI가 비운 `s`·`d`도 규칙 기반 글로 채운다.
  - 규칙 기반 `s`, `d`, `f`는 바꾸지 않는다. 화면은 AI가 켜져 있으면 `x`를 보여 주고, 옆 패널의 "기본 설명 보기"를 누르면 규칙 기반 글을 보여 준다.
- `xs`는 AI가 받지 않는 코드 파일(`t`가 `f`이고 `lang`이 있는 파일)의 까닭이다. AI가 켜져 있을 때만, 모델과 상관없이(모델을 고르기 전에도) 붙는다. 그런 파일에는 `x`가 없다.
  - `secret`: 이름이 비밀 파일 규칙(7장)에 맞는다 (`secrets.py`, `config/credentials.js`). 이런 파일은 읽지도 않는다.
  - `too_big`: `explain.MAX_SOURCE_BYTES`(1,000,000바이트)보다 크다.
  - `unreadable`: 없어졌거나, 잠겼거나, 링크·정션을 지나야 닿는다.
  - 1MB가 넘는 파일은 분석기도 읽지 않아 `lang`이 없고 스캐너는 링크를 목록에 넣지 않으므로, `too_big`과 `unreadable`은 분석한 뒤에 파일이 커지거나, 잠기거나, 없어지거나, 링크·정션 뒤로 옮겨진 경우다.
  - 화면은 이런 파일의 설명을 묻지 않고 까닭 문장(`messages.MARK_NOTES`. app.js의 `XS_NOTE`와 같고, 테스트가 둘을 견준다)을 보여 주며, 전체 설명 만들기의 남은 수에서 뺀다.

## 5. 규칙

- **영역**: `.py` → sv. `.jsx`/`.tsx`/`.vue`/`.svelte`/`.html`/`.css`/`.scss` → ui. `.js`/`.ts`는 화면 프레임워크를 쓰거나 화면 패키지(package.json에 react·vue 등) 안에 있거나 웹 페이지가 `<script src>`로 불러오면 ui, 서버 프레임워크를 쓰거나 서버 패키지 안에 있으면 sv, 판단이 안 되면 프로젝트에 화면 프레임워크가 있을 때 ui, 아니면 sv. 설정 파일(`*.config.js`, `package.json`, `tsconfig.json`, `requirements.txt`, `pyproject.toml`, `Dockerfile` 등)과 문서·데이터는 ot. 폴더는 자손 파일 중 ot가 아닌 영역의 다수결, 없으면 ot. 루트는 ot.
- **연결(u, b)**: import 말고도 다음을 연결로 친다.
  - 웹 페이지가 불러오는 파일 (`<script src>`, `<link href>`, 분석기의 `project_links()`)
  - Python에서 이름 글자로 불러오는 모듈: 같은 파일에 글자로 적힌 `import_module(...)`/`__import__(...)` 이름, 모듈 경로 표(Django `INSTALLED_APPS`, `ROOT_URLCONF`, pygments 같은 이름 표. dict의 키는 제외), `DJANGO_SETTINGS_MODULE`, `"polls.apps.PollsConfig"`처럼 끝에 클래스 이름이 붙은 경로. 적힌 글자 몇 개를 도는 반복문에서 만든 이름(`for package in ("urllib3", "idna"): __import__("pip._vendor." + package)`)은 그 모듈들이다.
  - 이름의 일부만 적힌 모듈(패턴, 3장 `imports`)은 맞는 파일 모두와 잇는다.
    - JavaScript: Vite `import.meta.glob`은 Vite처럼 읽는다 (`*`는 한 폴더 안, `**`는 하위 폴더까지, `?`, `[abc]`, `{a,b}`. `!`로 시작하는 패턴은 빼고, 점으로 시작하는 이름은 패턴이 점으로 시작할 때만 맞는다). webpack `require.context`는 그 폴더(두 번째 인수가 `false`면 바로 아래만)의 파일 중 `./하위/이름` 경로에 정규식이 맞는 파일이다. 실행할 때 만드는 `import()`·`require()` 이름(템플릿 문자열의 `${}`, `+`로 이은 글자. 파일 맨 위의 문자열 상수는 그 값을 넣고, 모두 상수면 보통 import다)에서 `${}` 하나는 이름 한 칸이고(`/`를 넘지 않는다), 확장자를 쓰지 않은 끝은 번들러처럼 `.js`·`.ts`·`.vue` 등과 `폴더/index.*`도 본다 (`./plugins/*` → `plugins/a.js`, `plugins/c/index.js`). `.d.ts`는 빼고, `.js`로 쓴 이름은 같은 이름의 `.ts`·`.tsx`도 본다. 패턴의 폴더는 import처럼 찾는다: `./`·`../`는 그 파일에서, `/`는 패키지 폴더에서, `@/`·`$lib` 같은 별칭은 tsconfig와 기본 별칭으로.
    - Python: `import_module()`·`__import__()`의 이름을 f-string, `+`, `%`, `.format()`이나 그런 값을 담은 변수로 만들고 앞에 패키지를 적었으면(`f"pkg.plugins.{name}"` → `pkg.plugins.*`) 그 패키지에서 맞는 모듈(`x.py`, `x/__init__.py`. 패키지 자신과 `__main__`은 빼고)이다. `*` 뒤에 이름이 더 있으면(`pkg.*.handler`) 하위 패키지(`__init__.py`가 없는 폴더도)로 내려간다. 상대 이름은 `__package__`(패키지의 `__init__.py`에서는 `__name__`도)를 기준으로 한다: `import_module(f".unicode{v}", __package__)` → 그 패키지의 `unicode*`, `f"{__package__}.plugins.{name}"` → `plugins.*`.
    - 알 수 없는 이름은 잇지 않는다: 적힌 부분이 없는 이름(`require(name)`, `import_module(config["module"])`), 패키지만 적힌 이름(`` import(`./${name}`) ``, `import_module("." + name, __package__)`), 다른 패키지의 모듈(`` import(`lodash/${fn}`) ``), 이 분석기가 읽지 못하는 정규식. 프로젝트 폴더 밖의 파일은 가리키지 않는다.
    - 한 import(한 호출)가 잇는 파일은 50개(`graph.PATTERN_LIMIT`)까지다. 넘으면 경로순으로 앞의 50개만 잇고, 경고 "이름 규칙으로 여러 파일을 한꺼번에 불러오는 코드(`encodings/__init__.py`, `idlelib/idle_test/htest.py` 등)는 맞는 파일이 너무 많아서 50개까지만 연결했어요."를 보여 준다 (그런 파일은 두 개까지 쓰고, 더 있으면 "등"을 붙인다). 잇지 않은 나머지도 쓰이는 파일이라 정리 후보가 아니고, 다른 연결이 없으면 d에 "다른 파일이 이름 규칙으로 한꺼번에 불러오는 파일 중 하나예요. 맞는 파일이 너무 많아서 연결선은 일부만 그렸어요."가 붙는다.
    - 이름 패턴 연결(2026-11-21 완료)로 바뀐 결과: 그 전에는 `f".unicode{v}"`처럼 마지막 칸에 글자가 있는 이름만 이었고, `"pkg.plugins." + name`이나 JavaScript의 glob과 계산된 이름은 잇지 않았다. 예를 들어 base Lib의 `encodings/__init__.py`(코덱을 이름으로 불러온다), `turtledemo/__main__.py`(데모를 이름으로 불러온다. 그래서 데모 파일은 이제 시작점이 아니라 "직접 실행할 수도 있어요")가 새로 이어진다.
  - 분석하는 폴더의 이름은 최상위 패키지 이름으로도 쓴다 (`pip` 폴더를 열었을 때 `from pip._internal import …`). 일반 탐색이 먼저다.
  - `import __main__`은 어떤 파일도 가리키지 않는다. `if TYPE_CHECKING:` 아래 import는 프레임워크로 세지 않는다.
- **시작점(e)**: FileFacts.is_entry 또는 project_entries()에 있는 파일. 단, `if __name__ == "__main__"` 블록만 있고 다른(테스트가 아닌) 코드가 불러 쓰는 모듈은 시작점이 아니다. 그런 파일의 d에는 "직접 실행할 수도 있어요."가 붙는다. `__main__.py`, 서버 앱, project_entries의 근거는 그대로 시작점이다.
  - 프레임워크 앱은 처음 띄우는 화면이 시작점이다 (JavaScript 분석기의 project_entries, 이유 "Nuxt가 처음 띄우는 화면" 등): Nuxt는 `app.vue`(없으면 `pages/index.vue`), SvelteKit은 `routes/` 바로 아래의 `+layout.svelte`(없으면 `+page.svelte`), Next.js는 `app/layout.*`과 `pages/_app.*`(둘 다 없으면 `app/page.*`나 `pages/index.*`)다. 페이지 하나하나는 시작점이 아니라 프레임워크가 불러오는 파일이다 (정리 후보 참고). Vue·Svelte의 `App.vue`·`App.svelte`는 `main.js`가 import하므로 따로 정하지 않는다.
  - Nuxt와 Next.js 앱은 package.json의 `nuxt`·`next` 의존성이나 `nuxt.config.*`·`next.config.*`가 있는 폴더이고, Nuxt 4처럼 `app/app.vue`나 `app/pages/`가 있으면 Nuxt의 자리는 `app/` 아래다. SvelteKit은 파일 이름(`routes/` 아래의 `+page.svelte`, `+layout.ts`, `+server.ts` 등)만으로 알아본다.
- **정리 후보(o)**: 코드 파일(python/javascript/typescript)인데 다른 파일이 쓰지 않고(b가 비어 있음), 시작점이 아니고, 다음에 해당하지 않는 파일.
  - 테스트: `test_*.py`, `*_test.py`, `tests.py`, `test.py`, `*.test.*`, `*.spec.*`, `tests/` 아래
  - 설정: `*.config.*`, `setup.py`, `conftest.py`, `manage.py`
  - `__init__.py`, `.d.ts`
  - `migrations/`, `alembic/` 아래 (프레임워크가 알아서 불러옴)
  - 이름을 import 문으로 쓸 수 없는 `.py` (`__pip-runner__.py`처럼 `-`가 들어간 이름. 경로로 실행되거나 불러와진다). 이런 파일은 docstring 요약을 그대로 쓰고, 다른 파일과 이어져 있지 않고 시작점도 아니면 d에 "파일 이름 때문에 import 문으로는 불러올 수 없어서, 다른 코드가 파일 경로로 불러오거나 직접 실행하는 파일일 거예요."가 붙는다.
  - 프레임워크가 알아서 불러오는 파일 (분석기의 `project_loaded()`): Nuxt 앱의 `pages/`(주소마다), `layouts/`, `components/`, `composables/`, `utils/`, `plugins/`, `middleware/` 아래 코드와 `error.vue`, 서버 폴더 `server/`와 `modules/`; SvelteKit `routes/` 아래의 `+page`·`+layout`·`+error`·`+server` 파일(`.svelte`, `.ts`, `.server.ts`, `@` 붙은 이름 포함)과 `routes/` 옆의 `hooks.server.*`·`hooks.client.*`·`hooks.*`, `params/*`, `service-worker.*`; Next.js `pages/` 아래 코드와 `app/` 아래의 `page`·`layout`·`template`·`loading`·`error`·`global-error`·`not-found`·`forbidden`·`unauthorized`·`default`·`route` 파일, `middleware.*`·`instrumentation.*` (`src/` 아래에 둔 것도). 이런 파일의 d에는 역할 문장 뒤에 어떻게 불러오는지("Nuxt가 /todos/[id] 주소에 맞춰 알아서 불러와요.", "SvelteKit이 모든 페이지에 씌울 때 알아서 불러와요.")가 "다른 파일과 직접 이어져 있지 않아요." 대신 붙는다. 링크는 만들지 않는다.
  - 이름 패턴이 맞혔지만 50개 제한 때문에 잇지 않은 파일 (연결 참고)
- **HTTP 연결**: 화면 파일의 http_calls와 서버 파일의 http_routes를 `model.http_paths_match()`와 `http_methods_match()`로 맞춘다. 맞으면 호출한 파일의 `u`, 서버 파일의 `b`, 양쪽의 `h`에 넣는다.
  - 맞추기 전에 `http_mounts`의 prefix를 붙여 둔다. 연결된 마운트(app → api 라우터 → todos)는 9단계까지 이어 붙인다. 그래서 라우트 파일의 설명과 Python 라우트 힌트(docstring이 없는 주요 함수의 설명 자리에 보이는 `GET · /api/todos`)에도 전체 주소가 보인다. JavaScript 함수의 힌트는 그 함수가 보내는 요청의 주소(`GET · /upstream/list`)라서 prefix를 붙이지 않는다.
  - FastAPI는 prefix를 앞에 이어 붙인다. `APIRouter(prefix="/todos")`를 `include_router(..., prefix="/api")`로 붙이면 `/api/todos`다. 자기 prefix가 있는 라우터에 붙인 라우터는 그 prefix도 받는다 (`APIRouter(prefix="/api")`에 붙인 todos 라우터는 `/api/todos`).
  - Flask는 `register_blueprint(..., url_prefix=)`의 `url_prefix`가 Blueprint 자신의 `url_prefix`(라우트의 `base`)를 대신한다. `Blueprint(url_prefix="/todos")`를 `url_prefix="/api"`로 붙이면 `/api/todos`가 아니라 `/api`다. `url_prefix`가 없거나 `None`이면 Blueprint 자신의 것을 쓴다. Blueprint를 다른 Blueprint에 붙이면 바깥 Blueprint의 prefix 뒤에 이어진다 (`api_bp.register_blueprint(todos_bp)` → `/api/todos`).
  - 우선순위 낮은 문제 수정(2026-11-20 완료)으로 바뀐 결과: 그 전에는 다른 라우터에 붙인 라우터가 바깥 라우터의 prefix를 받지 못했고(`APIRouter(prefix="/api")`에 붙인 todos 라우터와 `api_bp.register_blueprint(todos_bp)` 모두 `/todos`), Flask `url_prefix`는 Blueprint 자신의 prefix 앞에 이어 붙였다(`/api/todos`).
  - 같은 파일에서 만든 라우터에 붙이면 prefix가 없어도 그 라우터 아래에 붙은 것이다 (Python `api_bp.register_blueprint(todos_bp)`, JavaScript `api.use(todos)`). 붙인 쪽 라우터가 그 파일에서 prefix 아래에 있으면 그 자리가 `base`가 되고(`app.use("/api", api)`면 `/api`), 이 파일을 다른 파일이 붙인 prefix도 이어 받는다 (`routes/index.js`의 `router.use(auth)`를 `app.js`가 `app.use("/api", routes)`로 붙이면 auth의 라우트는 `/api` 아래). 세 단계 사슬(`root_bp → api_bp → todos_bp`, 앞의 둘이 한 파일)을 다른 파일이 `url_prefix="/v1"`로 붙이면 맨 바깥 `root_bp` 자신의 prefix(`base_own`)만 바뀌고 가운데 Blueprint의 몫은 남는다 (`/v1/api/todos`).
  - 한 라우터를 여러 곳에 붙이면(`/v1`과 `/v2`) 주소마다 라우트가 생기고, 힌트도 주소를 모두 보여 준다 (`GET · /v1/todos, /v2/todos`). 힌트에는 주소를 8개까지 쓰고, 한 라우터나 파일이 받는 prefix도 8개까지만 센다. 다른 파일에서 붙인 prefix 가운데 하나라도 실행할 때만 정해지면(`${}`) 힌트는 라우트 파일 안에서 정한 주소 그대로다.
  - prefix 없이 앱에 붙인 것(Python `include_router(router)`, `register_blueprint(bp)`, JavaScript `app.use(router)`, `app.register(plugin)`)도 늘 마운트로 기록한다. 혼자 있으면 주소가 바뀌지 않지만, 같은 라우터를 이 파일이나 다른 파일에서 prefix 아래에도 붙였으면 쓴 그대로의 주소도 함께 응답한다 (`GET · /v1/todos, /todos`). 다만 모듈 전체를 가리키는 마운트(아래)는 같은 파일에 같은 대상을 prefix 아래에 붙인 마운트가 있거나, 붙인 쪽 라우터가 다른 파일에서 prefix 아래에 붙을 때만 센다. `app.use(requireAuth)`처럼 라우터가 아니라 미들웨어일 수도 있어서다.
  - 다른 파일에서 붙인 마운트는 라우터마다 계산한다. 마운트가 이름으로 가리키는 라우터(`HttpMount.router`)가 대상 파일에서 라우트나 마운트를 가진 라우터(라우트의 `router`, 마운트의 `owner`)이면 그 라우터의 라우트만 prefix를 받는다. 한 파일의 `todos_router`와 `users_router`를 `/todos`, `/users`로 따로 붙이면 `/todos/list`와 `/users/items`만 생긴다. Python은 import 뒤에 적은 이름 가운데 대상 파일의 라우터인 마지막 이름을 쓴다 (`todos.router`는 하위 모듈 todos의 `router`, routes의 라우터 `todos` 순으로 본다). JavaScript는 내보낸 이름을 대상 파일의 `exports`로 변수 이름에 잇고, 다른 모듈에서 받아 넘긴 이름(`export { default as todos } from "./todos"`, `module.exports = { todos: require("./todos") }`, `export * from …`)은 그 모듈까지 따라간다 (9단계까지, 이름마다 한 번만). 받아 넘긴 이름이 가리키는 모듈에서 라우터를 모르면 그 모듈 전체다. 이름이 없거나(모듈 전체) 대상 파일의 라우터가 아니면 예전처럼 그 파일의 모든 라우트가 prefix를 받는다. 다른 파일이 붙이는 라우터에 다시 붙인 마운트는 그 라우터(`owner`)가 받는 prefix만 이어 받고, 어느 마운트도 가리키지 않는 라우터의 라우트(같은 파일의 `@app.get("/health")`)는 쓴 그대로다.
  - JavaScript도 한 파일 안에서 라우터를 붙인 자리를 계산한다. 그 파일에서 만든 라우터의 라우트는 붙은 자리마다 전체 주소를 받고, 다른 파일에서 가져온 라우터를 그런 라우터에 붙이면 그 자리가 마운트의 `base`가 된다 (`api.use("/todos", todos)`와 `app.use("/api", api)`가 한 파일에 있으면 todos는 `/api/todos`). 알아보는 모양은 Express `express.Router()`·`Router()`의 `use`, Koa 라우터 자신의 prefix(`new Router({ prefix: "/api" })`)와 `api.use("/todos", todos.routes())`, Hono `new Hono().basePath("/api")`, `app.basePath("/v1")`, `app.route("/todos", todos)`, Fastify `register(plugin, { prefix })`로 붙인 같은 파일의 plugin 함수(그 함수의 첫 매개변수에 만든 라우트가 plugin의 라우트)다. `use`의 첫 인수가 함수 호출이나 그 자리에 쓴 함수면 주소 없이 붙인 것으로 보고 마지막 인수를 붙인다 (`api.use(cors(), sub)`, Koa `api.use(v1.routes(), v1.allowedMethods())`). 첫 인수가 변수(`api.use(auth, sub)`)면 주소인지 미들웨어인지 알 수 없어서 따라가지 않는다. 라우터는 변수 이름으로 찾는다. 같은 파일의 함수가 만들어 돌려주는 라우터(`app.use("/api", makeRouter())`), 주소 목록(`app.use(["/a", "/b"], r)`)과 정규식 주소(`app.use(/^\/api/, r)`), Koa `router.prefix()`와 `koa-mount`는 따라가지 않는다. Hono에서 이어 쓴 라우트(`new Hono().get(...).post(...)`)와 Fastify `route({ ... })`는 tree-sitter가 있을 때만 읽는다.
  - JavaScript 같은 파일 라우터 자리 계산(2026-11-20 완료)으로 바뀐 결과: 그 전에는 같은 파일 안의 다른 라우터에 붙인 라우터가 바깥 라우터의 prefix를 받지 못했다(위의 todos는 `/todos`). 호출 쪽 앞에 세그먼트가 더 있어도 맞추는 규칙(아래) 덕분에 `/api/todos` 호출은 이어졌지만, 없는 주소인 `/todos` 호출도 이어졌다. Koa 라우터 자신의 prefix, Hono `basePath()`, 같은 파일의 Fastify plugin 함수는 주소에 넣지 않았고, prefix 없이 붙인 것(`app.use(router)`, `api.use(sub)`, `api.use(cors(), sub)`, `router.use(auth)`)은 마운트로 치지 않았다. 다른 파일이 붙인 JavaScript 라우트 파일은 함수의 요청 힌트에도 prefix가 붙어 보였다(`GET · /api/todos/upstream/list`).
  - Fastify 플러그인을 파일마다 나눈 모양도 읽는다. 파일이 내보내는 함수(`module.exports = async function (fastify, opts) { … }`, `export default async function routes(fastify) { … }`, `export const todos = async (app) => { … }`, `module.exports = fp(async (fastify) => { … })`)가 첫 매개변수로 주소와 그 자리에 쓴 처리 함수가 있는 라우트(`fastify.get("/todos", async () => …)`)나 `route({ … })`, `register(…)`를 부르면 Fastify 플러그인이고, 그 매개변수로 만든 라우트와 마운트는 그 플러그인의 것이다 (라우트의 `router`가 플러그인을 내보낸 이름). 처리 함수를 이름으로 넘긴 라우트(`fastify.get("/items", getItems)`)는 매개변수 이름이 Fastify 인스턴스나 라우터 같을 때(`fastify`가 든 이름, `instance`, `app`, `server`, `…router`)만 근거가 된다. `cache.get("/key", load)`의 `load`는 무엇이든 될 수 있어서다. 매개변수 이름이 요청 클라이언트 같으면(`http`, `api`, `httpClient`) 플러그인이 아니다: `makeTodoApi(http) { http.get("/todos", options) }`는 전처럼 요청이다. 서버 프레임워크를 import하지 않는 파일에서는 이것이 유일한 근거라서 `plugin_routes`·`plugin_mounts`에 따로 두고, 프로젝트 자기 코드가 Fastify를 import하거나 Fastify 파일(Fastify를 import하는 파일, 또는 이렇게 인정된 플러그인 파일)이 그 파일을 붙일 때만 라우트로 친다. 다른 파일의 `fastify.register(require("./routes/todos"), { prefix: "/api" })`나 `register(import("./routes/users.mjs"), …)`는 플러그인의 라우트를 그 prefix 아래에 둔다. 내보내지 않는 함수, 첫 매개변수가 라우트를 만들지 않는 함수(`map.get("/key", 1)`)는 플러그인이 아니고, 첫 매개변수 이름이 `app`·`server`·`…router`인 함수의 라우트는 전처럼 늘 라우트다.
  - 라우터별 마운트·다른 파일의 요청 도우미·Fastify 플러그인 파일(2026-11-21 완료)로 바뀐 결과: 그 전에는 한 파일의 라우터 여럿을 따로 붙이면 모든 라우트가 모든 prefix를 받아서 없는 주소(`/todos/items`, `/users/list`)가 생겼고, 그 파일의 앱 라우트도 prefix를 받았다(`/api/health`). prefix 없는 마운트는 같은 파일에 짝이 있을 때만 기록해서, 같은 라우터를 다른 파일에서 `/v1` 아래와 prefix 없이 붙이면 `/todos`가 빠졌다. `routes/index.js`가 받아 넘긴 라우터를 붙인 prefix는 `index.js`에 붙어서 사라졌다. 다른 파일에서 import한 요청 도우미로 보낸 요청은 잇지 않았고(`http.get("/todos")`는 base 주소 없이 `/todos`로만 읽었다), fastify를 import하지 않는 플러그인 파일에는 라우트가 없었다. 회귀 비교(예제 프로젝트, 이 저장소, pip, site-packages, base Lib)의 그래프 JSON은 키 하나도 바뀌지 않았다.
  - 남은 한계: 요청 도우미와 그 호출은 tree-sitter가 있을 때만 읽는다 (라우터별 마운트와 Fastify 플러그인 파일은 정규식 대체로도 읽는다). 같은 파일에서 다른 라우터 아래에 붙인 안쪽 라우터를 다른 파일이 따로 붙이면 파일 전체의 마운트로 계산한다. Python 패키지의 `__init__.py`가 하위 모듈의 라우터를 받아 넘기는 것(`from .todos import router as todos_router`)은 따라가지 않는다. Fastify `@fastify/autoload`처럼 폴더째 불러오는 플러그인은 폴더 이름으로 붙는 prefix를 모르고, 그 파일들은 정리 후보로 보일 수 있다. 서버 프레임워크를 import하지 않는 파일에서 첫 매개변수 이름이 Fastify 인스턴스 같지 않고 처리 함수를 이름으로만 넘기는 플러그인(`async (f) => { f.get("/items", getItems) }`)은 플러그인으로 알아보지 못한다.
  - `/`와 파일처럼 보이는 경로(`/app.js`)는 주소로 치지 않는다 (정적 파일 표가 DELETE 주소와 잘못 이어지던 문제).
  - 호출 쪽 앞에 세그먼트가 더 있으면(`/api/todos/*` → `/todos/*`) 와일드카드가 아닌 세그먼트가 하나 이상 같아야 한다.
  - `api(url, method)`처럼 fetch를 감싼 요청 도우미 함수 호출도 요청으로 친다 (tree-sitter가 있을 때만). 같은 파일의 도우미는 분석기가 그 자리에서, 다른 파일에서 import한 도우미는 그래프가 잇는다. 그 import와 대상 파일의 `exports`(받아 넘긴 이름도 따라간다)로 `http_helpers`의 도우미를 찾고, `helper_calls`의 인수로 `model.helper_request()`가 주소와 method를 만든다. 그 요청은 부른 파일의 `http_calls`가 되어 서버 라우트와 맞춰지고, 그 호출을 담은 함수의 힌트(`GET · /api/todos`)가 된다.
    - 도우미는 fetch 주소가 매개변수 하나이고 그 앞에 base 주소만 있는 함수다: `(path, opts) => fetch(BASE + path, opts)`(method는 옵션 객체에서), `` request(url, method = "GET") { … fetch(`/api${url}`, { method }) } ``(method 인수, 빠지면 기본값), 내보내면서 쓴 함수(`export default (url) => fetch(url)`), 객체에 담아 내보낸 함수(`export const api = { get(path) { … } }`와 `api.get("/x")`). `axios.create({ baseURL: "/api" })`로 만든 클라이언트도 도우미다 (`http.get("/todos")` → `GET /api/todos`, 상대 주소에만 baseURL을 붙인다).
    - 부르는 쪽은 이름 import, 기본 import, `require()`, `import * as lib`와 `lib.request(…)`, 받아 넘긴 이름(`export { api } from "./api"`, `export * from "./request"`, `export { default as client } from "./http"`)을 모두 따라간다. 주소처럼 보이는 인수가 없는 호출(`format(date)`, `api(someUrl)`)은 요청이 아니다.
    - 도우미 자신의 `fetch(BASE + path)`는 요청이 아니다. 클라이언트처럼 보이는 이름(`http.get`)을 부른 파일이 base 주소 없이 읽어 둔 요청은 도우미를 찾으면 그 주소로 바뀌고, 못 찾으면 그대로다. `` `/todos/${id}` ``, `?path=${x}`처럼 매개변수가 id나 값이면 그 함수가 직접 보내는 요청이다. 도우미가 또 다른 도우미를 불러 보내는 요청은 따라가지 않는다.
    - 같은 파일의 도우미에 넘긴 method 인수도 주소 인수처럼 읽으므로, 파일 맨 위의 문자열 상수(`const POST = "POST"`)도 method가 된다.
  - Python `http.server` 핸들러는 경로 표(dict)와 `self.path == …`, `.startswith(…)` 검사에서 주소를 읽는다.
  - 파일 경로가 곧 주소인 라우트: Next.js `pages/api/x.js`(모든 메서드)와 `app/x/route.ts`(내보낸 `GET`·`POST` 등), SvelteKit `routes/x/+server.ts`(내보낸 메서드. `fallback`은 나머지 모든 메서드), Nuxt `server/api/x.get.ts`·`server/routes/x.ts`(파일 이름의 메서드, 없으면 모든 메서드. `defineEventHandler()`·`eventHandler()`를 쓰는 파일만이라서 다른 서버의 `server/api` 폴더는 해당하지 않는다). 괄호 폴더(route group)는 주소에 넣지 않고, `[[lang]]`은 `[lang]`처럼 읽는다.
  - Vue 파일과 Nuxt를 쓰는 파일에서는 `useFetch("/api/todos")`·`useLazyFetch()`도 요청이다 (React의 직접 만든 `useFetch` 훅은 아니다). Vue·Svelte 파일은 `<script>` 블록 안의 요청만 본다. 템플릿 식 안의 요청(`{#await fetch(…)}`)은 읽지 않는다.
- **focus**: 등급이 먼저다. 프로젝트 자기 코드 → 예제·데모·테스트 → 다른 프로젝트에서 가져온 코드(`_vendor`, `vendor`, `third_party` 등) → 연결이 없는 파일. 같은 등급에서는 `len(u)+len(b)`가 가장 큰 파일, 같으면 시작점, 그다음 경로순.
- **stack과 범례**: 한눈에 보기 줄(stack), 범례의 서버 이름, JS 영역 판단의 마지막 기준은 프로젝트 자기 코드에서만 모은다. 예제·데모·샘플·fixture·테스트 폴더(`example`, `demo`, `sample`, `fixtures` 등)의 프레임워크는 넣지 않는다.
- **요약(s, d)**: AI 없이 describe.py가 만든다. 파일은 module_doc, 역할(컴포넌트, 주소 처리, 데이터 모양, 설정, 문서, 테스트, 자동 생성 코드, 패키지 입구 파일 등)과 연결 수로, 폴더는 흔한 폴더 이름 사전(components → "화면 부품 모음" 등), 패키지 `__init__.py`의 docstring, 내용물 수로 만든다. 말투는 쉬운 해요체.
  - 라이선스·저작권 머리말, 모듈 이름만 되풀이하는 제목 줄, reST 지시문(`.. note::`)과 그 아래 들여 쓴 글, doctest(`>>>`), 필드 목록(`:param x:`, `:returns:`), URL만 있는 줄은 요약으로 쓰지 않는다. 줄바꿈으로 나뉜 첫 문장은 이어 붙인다. 소문자·숫자·괄호·따옴표·`` ` ``·역할 표시로 시작하는 줄(한글 줄 뒤의 한글 줄도)만 잇고, 대문자로 시작하는 줄과 목록 항목(`-`·`1.`로 시작하는 줄)은 새 문장으로 본다. 그래서 다음 줄이 대문자로 시작하면 문장이 중간에서 끊길 수 있다 ("JSON (JavaScript Object Notation) is a subset of."). 이어 붙일 때는 문장이 끝나는 곳까지만 읽고 줄마다 한 번만 보므로, 줄이 수만 개인 문단도 글 길이만큼만 걸린다.
  - Python docstring의 reST 역할 표시는 Sphinx가 보여 주는 글자로 바꾼다: `` :class:`.HTTPConnectionPool` `` → `HTTPConnectionPool`, `` :func:`~a.b.run` `` → `run`, `` :class:`Foo <a.Foo>` `` → `Foo`, `` :class:`!Foo` `` → `Foo`, `` :pep:`8` `` → `PEP 8`. `~`, 앞의 `.`, `!`는 코드 이름을 가리키는 역할(`class`, `func`, `meth`, `mod` 등)에서만 지우므로 `` :file:`~/.bashrc` ``는 `~/.bashrc` 그대로다. 역할 바로 뒤의 `\`는 다음 글자를 붙이기만 한다(`` :class:`Option`\s `` → `Options`). 역할로 시작하는 줄은 필드 목록이 아니라 앞 문장이 이어지는 줄로 보고 이어 붙인다("…, such as" + `` :class:`.HTTPConnectionPool` and … ``). 이런 표시는 문장의 앞 4,000자에서만 찾는다 (요약은 300자까지만 쓴다).
  - docstring 글은 다듬어서 쓴다 (모듈·폴더·루트·README, 대표 클래스·함수, 주요 함수 설명 모두). 뒤에 올 목록만 가리키는 열 단어 미만의 머리말("Spinners are from:", "Usage:", "Run the following command:", "이 모듈은 다음 기능을 제공해요:")은 버리고 다음 규칙으로 넘어간다. "…, such as:"처럼 끝나면 그 앞의 말만 남기고("For PsySH console output"), 그 밖에는 끝의 ":"나 "::"만 뗀다.
  - 분석기가 예시 앞에서 끊은 끝말("such as", "e.g.", "including", "and", "the" 등)은 지우고, 지운 뒤 세 단어가 안 되면 쓰지 않는다. Sphinx 참조 `~rich.console.Console`은 Sphinx처럼 `Console`로 쓰고, reST 링크의 주소 부분(`<https://…>`)은 뺀다.
  - README에서 "Install it with:"처럼 뒤를 가리키기만 하는 줄은 그 아래 첫 블록(들여 쓴 줄, 코드 블록, 목록)과 함께 건너뛴다.
  - 모듈 docstring이 없는 코드 파일은 정해진 역할(시작점, 테스트, 주소 처리, 데이터 모양, 화면 조각, 서버 요청, DB 연결, 도우미, 설정값, 패키지 입구 등)을 먼저 본다. 그것도 아니면 오류 파일 → 역할 폴더 → 파일 이름 → 대표 클래스·함수의 docstring → 다른 곳에서 가져온 코드 → 약한 폴더 역할 순으로 단서를 찾고, 다 없을 때만 "Python 코드"처럼 일반 문장을 쓴다.
  - Vue·Svelte 파일은 테스트가 아니면 늘 React 컴포넌트처럼 화면 조각이고, 컴포넌트 이름(파일 이름, `index.vue`면 폴더 이름)을 쓴다: "Vue 화면 조각", "화면의 한 부분(TodoItem)을 그리는 Vue 코드예요.". `App`이면 "화면 전체 틀", `pages/`·`views/`·`screens/` 아래나 SvelteKit `+page`면 "화면 페이지", `layouts/` 아래나 `+layout`이면 "화면 배치 틀", `+error`나 맨 위(Nuxt 4처럼 `app/` 바로 아래도) `error.vue`면 "오류 화면"이고, 이때는 뒤에 "Vue로 만들었어요."(Svelte 파일은 "Svelte로 …")가 붙는다. SvelteKit `+page.ts`·`+layout.server.ts` 등은 "페이지 데이터 준비", `hooks.server.ts`·`hooks.client.ts`는 "요청 중간 처리"다. `useTodos.ts` 같은 훅 이름이라도 `composables/` 아래이거나 Vue·Svelte를 쓰는 파일이면 React 훅이라고 하지 않는다.
  - 오류 파일은 오류 클래스(이름이 `Error`·`Exception`·`Warning`·`Exc`로 끝나는 클래스)가 든 `errors.py`·`exceptions.py` 같은 파일이나, 오류 클래스만 두 개 이상 있는 파일이다 ("오류 종류 정의"). 뒤의 경우는 경고 클래스(`…Warning`)만 있으면 아니다. 역할 폴더는 폴더 이름 사전과 짝인 `FILE_ROLES` 표에서 가장 가까운 폴더로 정한다 (`routes/` → "주소별 요청 처리", `vcs/` → "버전 관리 도구 연결"). `commands/` 바로 아래 파일은 "install 명령"처럼 그 명령 이름이고, `base.py`·`utils.py`처럼 명령이 아닌 이름은 빠진다. 서버 쪽 파일에는 `components`, `styles` 같은 화면 전용 이름을 쓰지 않는다. 파일 이름 단서는 몇몇 역할 폴더와 같은 이름이다 (`auth.py`, `views.py`, `services.py`, `cli.py` 등. `core.py`, `tasks.py`는 아니다).
  - 대표 클래스·함수는 파일 이름과 같은 공개 이름(`align.py` → `Align`), 파일 이름으로 시작하는 이름 중 하나뿐인 클래스(`install.py` → `InstallCommand`), 아니면 하나뿐인 공개 이름이다 (옆에 오류 클래스 하나는 괜찮다). 이름이 `Error`·`Exception`·`Warning`·`Exc`로 끝나는 클래스와 함수는 대표가 될 수 없다. 그 docstring을 쓰면 d에 "이 파일의 대표 클래스인 Console에 적힌 설명이에요."가 붙는다. 가져온 코드는 `vendor`, `_vendor`, `third_party` 같은 폴더 아래 파일로, "rich에서 가져온 코드"처럼 그 아래 패키지 이름을 쓴다. 약한 폴더 역할(`core`, `internal`, `features`, `modules`, 서버 쪽의 `server`·`backend`, 화면 쪽의 `client`·`frontend`·`web`·`ui`)은 바로 위 폴더만 본다.
  - 그림 파일(`.svg` 포함)은 다른 파일이 쓰면 "화면에 쓰이는 그림이나 아이콘이에요."이고, 아니면 놓인 곳을 따른다. `screenshots` 아래는 "프로그램 화면을 찍어 둔 그림이에요.", `design`·`mockup`·`wireframe` 폴더 아래는 "디자인 자료로 넣어 둔 그림이에요.", `docs` 아래는 "설명 문서와 함께 둔 그림이에요.", 테스트 폴더(`tests` 등)나 `fixtures`·`mocks` 아래는 "테스트할 때 쓰는 그림이에요.", 그 밖에 `assets`·`static`·`public`·`images`·`img`·`icons` 아래는 화면 그림 문장, 나머지는 "그림 파일이에요."다.
  - 그림 폴더: `screenshots`는 "화면 캡처 모음", `design`·`mockup`·`wireframe`(복수형 포함)은 "디자인 자료 모음"이다 (이름 목록은 `describe.NOT_CODE_FOLDERS`). 다만 서버 코드 영역의 폴더, 그리고 안에 든 코드·스타일·웹 페이지 파일을 폴더 밖의 파일이 불러 쓰는 폴더(`App.tsx`가 불러오는 `src/design/Button.tsx`)는 코드 폴더로 보고 "화면 코드 모음"처럼 영역 이름을 쓴다. 폴더 밖에서 쓰는 코드 파일의 수는 `graph._used_outside()`가 아래 폴더까지 세어 `describe_folder(used_outside=)`로 넘긴다. 폴더 안에서만 쓰는 코드(시안 `index.html`이 불러오는 자기 `app.js`)는 세지 않는다. 그런 코드 폴더 안의 그림 파일도 다른 파일이 쓰지 않으면 여전히 "디자인 자료로 넣어 둔 그림이에요."다. `docs`·테스트·`design`·`screenshots` 같은 폴더 아래의 `images`·`img`·`icons` 폴더는 d가 "설명 문서와 함께 둔 그림이 모여 있어요."처럼 놓인 곳을 말한다.
  - 이름 사전에 없는 폴더라도 바로 아래 파일이 모두 그림이고 그 아래 어디에도 코드가 없으면 "그림 모음"이고, d는 "그림 파일이 모여 있어요."로 시작한다 (`photos/`. 그림 수는 graph가 `describe_folder(images=)`로 넘긴다). 그림과 문서·설정 파일이 섞여 있으면 그대로 "문서·설정 모음"이다.
  - 숫자는 모두 천 단위 쉼표를 쓴다 ("파일 3,642개", "함수나 클래스 1,234개").
  - 주요 함수(f)는 공개 이름 → 공개 오류·경고 클래스 → 비공개 클래스와 설명이 있는 도우미 → 나머지 비공개 순이고, 묶음 안에서는 소스 순서다. 오류·경고 클래스는 이름이 `Error`·`Exception`·`Warning`으로 끝나는 클래스와, Python의 예외 클래스(`OSError`, `KeyboardInterrupt` 등)나 그런 이름의 클래스나 이 파일에서 앞서 정의한 오류 클래스를 물려받은 클래스(3장 `bases`. JavaScript는 `extends` 뒤의 클래스)다. 화면 조각(컴포넌트)은 이름이 그래도 오류 클래스가 아니다. 오류 파일 판단과 달리 `Exc`로 끝나는 이름은 세지 않는다 (pytest의 `RaisesExc`는 오류 클래스가 아니다). 그래서 오류 클래스를 맨 위에 정의하는 모듈도 본래의 이름을 먼저 보여 주고(smtplib.py는 `quoteaddr`, `quotedata`, `SMTP`, `LMTP` 다음에 오류 클래스 넷), 오류 클래스만 있는 모듈은 전처럼 소스 순서다. 오류 파일 판단과 대표 클래스 고르기는 아직 이름만 본다(`Exc` 포함). 그래서 이름이 그렇지 않은 오류 클래스(`class Timeout(OSError)`)만 든 `errors.py`는 오류 파일로 보지 않고, 그런 클래스가 대표 클래스가 될 수도 있다.
    - 오류 클래스 순서(2026-11-21 완료)로 바뀐 결과: 그 전에는 공개 이름을 소스 순서대로만 보여 줘서 smtplib.py의 f가 오류 클래스 8개뿐이었고, `SMTP`는 패널에도 AI가 받는 주요 함수 목록에도 없었다. `ftplib.py`(`error_reply` 등), `zipfile`(`BadZipFile`)처럼 이름으로는 모르는 오류 클래스도 물려받은 클래스로 알아본다.
    - JavaScript의 `extends`(2026-11-21 완료)로 바뀐 결과: 그 전에는 JavaScript 클래스를 이름만 봐서 `class NotFound extends HttpError`가 본래의 이름 사이에 남았고, 정규식 대체는 이름 없는 `export default class extends Error {}`를 `class extends`라는 클래스로 목록에 넣었다. 회귀 비교(예제 프로젝트, 이 저장소, pip, site-packages, base Lib, 정규식 대체 포함)의 그래프 JSON은 오류 클래스 순서만 바꾼 코드와 키 하나도 다르지 않았다 (그 안의 JavaScript에는 `extends`로 만든 클래스가 없다).
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
| `GET /api/ai/status` | AI 상태 블록 (4장 `ai`). 부를 때마다 Ollama에 새로 묻는다 (7장) |
| `POST /api/ai/settings` | `{"enabled": bool, "model": str}`(`model`은 없어도 됨)을 저장하고 200 AI 상태 블록. 모델 이름이 잘못됐거나, Ollama가 켜져 있고 설치 목록에 없는 모델이면 400 `{"error"}` |
| `POST /api/ai/start` | `{}`. 설치된 Ollama를 켜고 답할 때까지(최대 20초) 기다린 뒤 200 AI 상태 블록. 이미 켜져 있으면 그대로 200 |
| `POST /api/ai/explain` | `{"project": 화면이 분석한 폴더 경로(그래프의 source.path) 또는 "demo", "node": 노드 id}`. 코드 파일(`t`가 `f`이고 `lang`이 있음)과 폴더(루트 `.` 포함)만 설명한다. 200 `{"node": id, "x": {"s", "d", "f"}, "cached": bool}` |
| `POST /api/ai/explain-all` | `{"project": …}`. 202 작업 블록. 같은 프로젝트의 작업이 이미 돌고 있으면 200과 그 작업 블록 |
| `GET /api/ai/job?id=<id>&since=<k>` | 200 작업 블록. `results`에는 앞의 k개 뒤에 끝난 것만 있다 |
| `POST /api/ai/job/cancel` | `{"job": id}`. 200 작업 블록. 작업은 지금 만드는 항목까지만 하고 멈춘다 |

사용자 데이터 폴더: `%LOCALAPPDATA%\CodeVision` (다른 OS는 `~/.codevision`). 환경 변수 `CODEVISION_HOME`이 있으면 그 폴더를 쓴다 (테스트용). 경로를 정하는 함수는 `codevision.explain.data_dir()`다. AI 설정은 그 안의 `config.json`, AI 설명 캐시는 `cache.sqlite`다 (7장).

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

AI 설명 API (`/api/ai/*`, 7장):

- Host 검사, `X-CV-Token`, CSP, `MAX_BODY`, 405·501 규칙은 다른 `/api/` 요청과 같다. POST 본문은 JSON 객체다 (빈 본문은 `{}`). 읽을 수 없거나 너무 깊게 중첩된 JSON(`[[[[…`), 객체가 아닌 값, 빠지거나 잘못된 `project`·`node`·`job`·`id`·`since`는 400 `{"error"}`다.
- 예전 뼈대의 `POST /api/explain`은 없어졌다. 다른 없는 주소처럼 404 `{"error": "찾는 주소가 없어요."}`다.
- 작업 블록: `{"job": id, "state": "running" | "done" | "cancelled" | "failed", "done": int, "total": int, "errors": int, "current": 노드 id 또는 "", "message": 한국어, "results": [[노드 id, x], …], "next": int}`. 다음 확인은 `since=next`로 한다. `total`에는 비밀 파일을 넣지 않는다.
- AI 오류는 `{"error": 한국어, "code": …}`이고, 코드마다 HTTP 상태가 정해져 있다 (`explain.AIError.STATUS`. 표 아래의 `failed` 두 경우, 409와 500만 다르다).

| HTTP | `code` | 언제 |
|---|---|---|
| 409 | `off` | AI 설명이 꺼져 있다 |
| 409 | `not_running` | Ollama가 꺼져 있거나, 답하다가 꺼졌거나, 모델 목록을 주지 않는다. 주소가 이 컴퓨터가 아닐 때도 이 코드다 |
| 409 | `no_model` | 설치된 모델이 없거나, 고른 모델이 설치되어 있지 않다 |
| 409 | `busy` | 기다리는 요청이 이미 6개다. CodeVision이 꺼지는 중일 때도 이 코드다 |
| 404 | `not_found` | 그래프에 없는 노드, 링크·정션을 지나는 노드, 없어진 프로젝트 폴더, 읽지 못한 파일, 모르는 작업 id |
| 422 | `not_code` | 코드 파일도 폴더도 아니다 |
| 422 | `secret` | 비밀이 들어 있을 수 있는 파일이다 (7장) |
| 422 | `too_big` | `explain.MAX_SOURCE_BYTES`(1,000,000바이트)보다 크다 |
| 504 | `timeout` | 모델이 제때 답하지 않는다 (한 번의 답에 120초, 요청 하나는 차례를 기다리는 시간까지 180초) |
| 502 | `bad_answer` | 모델의 답을 읽지 못했거나, 다듬고 나니 남은 글이 없다 |
| 502 | `failed` | Ollama의 오류(HTTP 500, 메모리 부족 등), 끊긴 연결, 다시 분석하다 난 오류 |

- `POST /api/ai/start`의 오류: Ollama를 찾지 못하면 409 `missing`, 켜지 못했거나 `ollama serve`가 답하기 전에 끝나면 502 `failed`, 20초 안에 답하지 않으면 504 `timeout`, 주소가 이 컴퓨터가 아니면 409 `failed`(주소 안내 문장).
- `POST /api/ai/settings`가 설정 파일을 저장하지 못하면 500 `failed`다.
- 서버를 닫으면(`server_close`) 기다리던 AI 요청은 바로 409 `busy`로 답을 받는다.
- 환경 변수(테스트·개발용): `CODEVISION_OLLAMA_URL`은 설정의 Ollama 주소를 바꾼다 (이 컴퓨터 주소만). `CODEVISION_OLLAMA_EXE`는 "Ollama 켜기"가 실행할 프로그램을 바꾼다 (`serve` 인수와 함께 실행하고, `.py` 파일은 이 Python으로 실행한다).

## 7. AI 설명 (로컬 AI)

AI 설명은 이 컴퓨터에서 도는 Ollama로 만든다 (2026-11-20 결정, `docs/decisions.md`). 구조와 연결은 분석기가 정하고, AI는 설명 글만 쓴다. AI에 보내는 코드는 이 컴퓨터 밖으로 나가지 않는다.

### 공급자와 설정

- 설정은 사용자 데이터 폴더의 `config.json`이다 (`explain/providers.py`): `{"ai": {"provider": "local", "enabled": false, "model": "", "base_url": "http://127.0.0.1:11434"}}`.
  - 파일이 없거나 깨졌으면 기본값이다 (공급자 `local`, AI 설명 꺼짐). 쓸 때는 옆의 임시 파일에 쓴 뒤 바꿔 넣고(`os.replace`), 이 코드가 모르는 키는 그대로 둔다.
  - `provider`: `local`은 Ollama(`explain/ollama.py`의 `OllamaExplainer`), `none`은 AI 설명을 쓰지 않는다는 뜻이다. `cloud`(상용 LLM)는 자리만 있고 만들지 않았다 (상태 문장 "인터넷 AI 서비스 연결은 아직 준비 중이에요."). AI 패널에서 켜면 `provider`도 `local`이 된다.
  - `model`이 `""`이면 CodeVision이 고른다: `prompts.RECOMMENDED_MODELS`(`["qwen2.5:7b", "gemma3:4b"]`) 가운데 설치된 첫 모델, 없으면 설치된 모델 가운데 이름순 첫 모델(임베딩 모델은 뺀다). 그래서 기본 모델은 qwen2.5:7b다. 고른 모델은 AI 설명을 켤 때와 처음 설명을 만들 때 설정에 저장한다. 그래서 Ollama가 꺼져 있어도 그 모델로 만든 캐시 설명이 보인다.
  - 공급자는 `explain/base.py`의 `Explainer`(`name`, `model`, `available()`, `explain_file(ctx)`, `explain_folder(ctx)`)만 구현한다. 캐시, 작업, 그래프에 붙이기는 공급자와 상관없다.
- Ollama 주소는 이 컴퓨터의 주소만 된다 (`ollama.check_base_url`): `http`이고, 호스트가 `127.0.0.1`, `localhost`, `::1` 중 하나이고, 사용자 정보·경로·쿼리가 없어야 한다. 아니면 상태 문장이 "Ollama 주소는 이 컴퓨터 안의 주소(127.0.0.1, localhost)만 쓸 수 있어요."이고, `can_start`는 false, 설명 요청은 409 `not_running`이며, 어디에도 연결하지 않는다. 환경 변수 `CODEVISION_OLLAMA_URL`도 같은 검사를 받는다.
- 클라이언트(`OllamaClient`)는 표준 라이브러리 `urllib`만 쓴다 (SDK 없음).
  - 이름이 어느 주소로 풀리든, 연결할 주소와 연결된 상대 주소가 모두 loopback이어야 한다. 프록시 처리기를 두지 않아서 `HTTP_PROXY`가 있어도 프록시를 쓰지 않고, 리다이렉트는 따라가지 않는다.
  - 시간 한도: 연결 2초(상태 확인은 1초), 상태 확인의 답 3초, 설명 한 번 120초(`ANSWER_TIMEOUT`). Windows에서는 `TCP_NOSYNRETRIES`를 켜서 꺼진 Ollama를 바로 알아챈다 (약 4ms. 켜지 않으면 약 2초). 4,000,000바이트가 넘는 답은 `bad_answer`다.

### 상태 (`GET /api/ai/status`, `graph["ai"]`)

- `compute_status()`(`explain.status()`도 같다)는 Ollama의 `/api/tags`로 설치된 모델을 받는다. 연결하지 못하면 꺼짐(`stopped`, 프로그램을 못 찾으면 `missing`)이다. 무언가 답하지만 모델 목록을 주지 않으면(늦은 답, 오류) 켜짐으로 치되, 문장은 "Ollama가 제대로 답하지 않아요. 잠시 뒤에 다시 해 주세요."이고 설명 요청은 409 `not_running`이다.
- 문장은 이 순서로 고른다: 공급자가 `none`·`cloud` → AI 설명 꺼짐 → 주소가 이 컴퓨터가 아님 → Ollama 꺼짐·없음 → 모델 목록 없음 → 설치된 모델 없음 → 고른 모델이 설치되지 않음 → "이 컴퓨터의 AI(qwen2.5:7b)로 설명을 만들어요.". AI 쪽 한국어 문장은 모두 `explain/messages.py`에 있다.
- `GET /api/ai/status`는 부를 때마다 Ollama에 새로 묻는다 (`status(fresh=True)`. 몇 ms, Ollama가 멈춰 있으면 확인 한도 3초까지). 그래서 CodeVision 밖에서 켜거나 끈 Ollama가 바로 보인다. 확인은 한 번에 하나씩이고, 요청은 그 요청 뒤에 시작한 확인의 답만 쓴다. 그래서 확인 하나가 도는 동안 온 요청들은 모두 그다음 확인 하나로 답을 받는다. 멈춘 Ollama를 확인하는 중에 온 요청은 그 확인과 그다음 확인을 기다리므로 6초쯤 걸릴 수 있다. `/api/analyze`는 3초(`STATUS_TTL`) 안에 만든 상태를 다시 쓰고, 폴더를 분석하는 동안 뒤에서 Ollama에 묻는다. 설정을 바꾸면 그 전에 시작한 확인의 결과는 다시 쓰지 않는다.
- 설정(`POST /api/ai/settings`)은 모델 이름의 모양을 보고, Ollama가 켜져 있고 설치 목록이 있을 때만 설치된 모델인지 본다. Ollama가 꺼져 있거나 목록을 모르면 이름만 확인한다.

### Ollama 켜기 (`POST /api/ai/start`)

- 설치된 Ollama를 찾아 `ollama serve`를 실행한다. Windows에서는 `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`, `PATH`, `Program Files` 순으로 찾는다. 트레이 앱(`ollama app.exe`)은 실행하지 않는다: 창을 띄우고 스스로 업데이트할 수 있어서다. 아무것도 내려받지 않고 모델도 받지 않는다 (모델은 사용자가 `ollama pull`로 받는다).
- 창 없이 띄운다. Windows에서는 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`과 `SW_HIDE`로 띄우고, 입출력은 모두 버린다. 그래서 Ollama가 띄우는 모델 실행 프로그램(0.32에서는 `llama-server.exe`)도 숨은 콘솔을 같이 써서 창이 뜨지 않는다 (콘솔이 아예 없는 `DETACHED_PROCESS`로 띄우면 그 프로그램이 제 창을 열 수 있다).
- 설정의 주소에서 듣도록 `OLLAMA_HOST`를 그 주소로 정한다. 다만 주소가 기본값이고 사용자가 `OLLAMA_HOST`를 정해 두었으면 그 값을 그대로 둔다.
- 답할 때까지 20초(`START_WAIT`) 기다린다. 요청이 동시에 와도 한 번만 띄운다. CodeVision이 켠 Ollama는 CodeVision을 꺼도 계속 돈다 (사용자가 켠 Ollama와 같다). 대신 CodeVision이 쓴 모델은 내린다 (아래 "메모리 비우기").
- `CODEVISION_OLLAMA_EXE`가 있으면 그 프로그램을 띄운다. 테스트는 이것을 `tests/fake_ollama.py`(가짜 Ollama의 `serve` 모드)로 가리킨다.

### 모델 부르기

- 설명 하나는 Ollama `/api/chat` 요청 하나다: `stream` false, `format`은 답의 JSON schema(`prompts.answer_schema(ctx)`가 주는 것, 없으면 `prompts.ANSWER_SCHEMA`), `options`는 `prompts.MODEL_OPTIONS`, `keep_alive`는 `"10m"`, 메시지는 `SYSTEM_PROMPT`와 `file_prompt(ctx)` 또는 `folder_prompt(ctx)`다. 답은 `prompts.parse_response()`로 읽고 `prompts.postprocess()`로 다듬는다.
- 실패는 계약 코드로 바꾼다 (6장): 연결하지 못함 → `not_running`, 시간 초과 → `timeout`, 404 "model not found" → `no_model`, 그 밖의 HTTP 오류(500, 메모리 부족)와 끊긴 연결 → `failed`, 읽을 수 없는 답이나 다듬고 나니 빈 답 → `bad_answer`. `failed`나 `timeout`인데 그때 Ollama가 아예 답하지 않으면(꺼졌거나 죽었으면) `not_running`으로 바꾸고 상태를 다시 묻는다. 실패한 답은 캐시에 넣지 않는다.

### 작업 스레드와 전체 설명

- 서버마다 `AIService`가 하나 있고(`server.ai`), 모델은 그 작업 스레드 하나만 부른다. 그래서 요청은 하나씩 차례로 Ollama에 간다.
- 고른 항목의 설명(`/api/ai/explain`)은 전체 설명의 항목보다 먼저 한다. 같은 질문이 이미 가고 있으면 그 답을 함께 받는다. 기다리는 요청이 6개(`MAX_PENDING`)면 그다음은 409 `busy`다. 캐시에 있으면 모델을 부르지 않고 바로 답한다 (`cached` true). Ollama가 꺼져 있어도 캐시에 있는 설명은 준다.
- 전체 설명(`/api/ai/explain-all`)의 순서는 코드 파일 → 폴더(깊은 것부터) → 루트다.
  - 비밀 파일은 항목에 넣지 않는다. 너무 크거나 읽지 못하는 파일은 건너뛴다 (`done`에는 세고, `errors`와 `results`에는 넣지 않는다). 캐시에 있는 항목은 바로 끝난다.
  - 폴더를 설명할 때는 같은 작업에서 먼저 만들었거나 캐시에서 찾은 자식의 AI 요약을 쓴다. 만든 설명은 모두 캐시에 넣는다.
  - 작업이 여럿이면 가장 새 작업부터 한다 (사용자가 보고 있는 것). 기다리는 작업의 문장은 "다른 폴더의 설명을 먼저 만들고 있어요. 끝나면 이어서 할게요."다.
  - 멈추기(`/api/ai/job/cancel`)는 지금 만드는 항목까지만 하고 멈춘다 (만드는 항목이 없으면 바로 멈춘다).
  - 저절로 멈출 때: Ollama가 꺼지거나, 모델이 없어지거나, 5개(`MAX_ERRORS_IN_A_ROW`)가 잇달아 실패하면 `failed`다. 모델을 바꾸거나 AI 설명을 끄거나 서버를 닫으면 `cancelled`다. 어떻게 멈추든 그때까지 만든 설명은 남는다.
- 끝난 작업은 10개까지 기억한다. 분석한 프로젝트는 3개(`MAX_PROJECTS`)까지 메모리에 두고(가장 오래 안 쓴 것부터 뺀다), 그 밖의 프로젝트에 요청이 오면 다시 분석한다.

### 메모리 비우기

- AI 설명이 켜져 있는 동안 Ollama는 마지막 답 뒤 10분(`keep_alive`) 동안 모델을 메모리(GPU)에 둔다.
- AI 설명을 끄면, 하던 설명과 기다리던 요청이 끝난 뒤에도 꺼져 있을 때 이 서버가 쓴 모델 가운데 Ollama가 아직 들고 있는 모델을 내린다 (`/api/ps`로 보고 `/api/generate`에 `keep_alive` 0). 서버를 닫을 때도 같다 (Ollama에 보내는 요청마다 연결 1초, 답 2초 한도라서, 내릴 모델이 여럿이면 조금 더 걸린다). 다른 프로그램이 불러 둔 모델은 건드리지 않고, 꺼진 Ollama는 그냥 둔다.

### 캐시

- `cache.sqlite`(사용자 데이터 폴더, `explain/cache.py`). 키는 공급자(`local`)·모델·`PROMPT_VERSION`·종류(`file`/`folder`)·SHA다.
- 파일의 SHA는 원본 바이트의 git blob SHA-1이다 (git·GitHub와 같다). 내용이 바뀌면 키가 달라서 새로 만들고, 옛 내용의 설명은 남는다.
- 파일의 키에는 주요 함수 목록(`node.f`)이 들어가지 않는다. 그래서 CodeVision의 분석기가 바뀌어 같은 파일의 목록이 달라져도 전에 만든 설명을 그대로 쓴다. `x.f`는 지금 목록의 순서를 따르고, 예전 답에 없는 함수에는 규칙 기반 설명을 보여 준다. 분석기 변경으로 모델에 보내는 내용이 크게 달라져서 설명을 다시 만들게 하려면 `PROMPT_VERSION`을 올린다.
- 폴더의 SHA는 폴더의 정보(경로, 이름, 규칙 기반 요약)와 자식마다 이름과 요약(AI 요약이 있으면 그것, 없으면 규칙 기반 요약)으로 만든 JSON의 SHA다. 그래서 자식의 요약이 바뀌면 폴더의 키도 바뀐다. 자식보다 먼저 설명한 폴더는 자식에게 AI 요약이 생기면 그 설명이 더는 보이지 않고, 다시 고르거나 전체 설명을 하면 새로 만든다.
- 모델이나 `PROMPT_VERSION`이 다르면 다른 키다. 화면에는 지금 모델의 설명만 보이고, 모델을 되돌리면 옛 설명을 다시 쓴다.

### 분석 결과에 붙이기 (`annotate()`)

- `/api/analyze`는 그래프를 만든 뒤 `AIService.annotate()`로 `graph["ai"]`를 넣는다. AI가 켜져 있으면 코드 파일마다 한 번 읽어 SHA를 구하고, `node["xs"]`와 지금 모델의 캐시 설명 `node["x"]`를 붙인다 (4장). 폴더는 아래에서 위로, 전체 설명과 같은 방법으로 키를 만들어 찾고, 캐시는 깊이마다 한꺼번에 읽는다.
- 모델은 부르지 않는다. AI가 꺼져 있으면 파일을 하나도 읽지 않는다. 걸리는 시간은 pip(코드 파일 442개)에서 약 0.1초다 (그래프 만들기는 약 1초).
- 붙이다가 문제가 생기면 `x`와 `xs`를 모두 빼고, `graph["ai"]`의 문장은 "저장해 둔 AI 설명을 불러오다가 문제가 생겨서 기본 설명을 보여 줘요."다.

### 비밀과 경로 (`explain/safety.py`)

- 비밀이 들어 있을 수 있는 파일은 보내지 않는다 (422 `secret`, 읽지도 않는다). 트리에는 그대로 보인다.
  - 이름: `.env`로 시작하거나 끝나는 이름, `.envrc`, `.npmrc`, `.yarnrc`, `.pypirc`, `.netrc`, `.pgpass`, `.htpasswd`, `.git-credentials`, `terraform.tfstate` 등. 개인 키(`id_rsa*`, `id_ed25519*` 등)와 `client_secret*`.
  - 확장자: `.pem`, `.key`, `.p12`, `.pfx`, `.jks`, `.keystore`, `.ppk`, `.kdbx`, `.p8`, `.tfvars`, `.tfstate`.
  - 점 앞 이름이 `secret(s)`, `credential(s)`, `client_secrets`, `service_account` 등인 파일 (`secrets.py`, `config/credentials.js`. `secretary.js`는 아니다), 그리고 `.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`, `.docker` 폴더 아래의 파일.
- 보내는 글 속의 뚜렷한 비밀 값은 `[가린 값]`으로 바꾼다 (`mask_secrets`). 소스, 규칙 기반 요약 힌트, 주요 함수 표시(답을 받은 뒤 원래 표시로 되돌린다), 폴더 자식들의 요약에 모두 쓴다.
  - 개인 키 블록, 잘 알려진 토큰 형식(GitHub, GitLab, OpenAI·Anthropic `sk-`, Stripe, AWS, Google, Slack, Hugging Face, npm, SendGrid, JWT, Slack·Discord webhook), URL 속 비밀번호, `Bearer`·`Basic` 값, 연결 문자열의 `Password=`·`AccountKey=`.
  - 이름이 비밀처럼 보이는 변수(`password`, `secret`, `token`, `api_key`, `DB_PASS`, `dbPwd` 등)에 넣거나 견주는 글자: `API_KEY = "…"`, `obj["password"] = "…"`, `password == "…"`. `os.getenv("SECRET_KEY", "…")`·`os.environ.get(…)`의 기본값, 비밀 이름 뒤의 `||`·`??`·`or`·`else` 대체값도 가린다.
  - 그 밖의 글은 그대로다 (`["password", "token"]` 같은 이름 목록도).
- 노드 id는 분석한 폴더 안의 파일로만 바꾼다 (`ProjectFiles`). `..`, `.`, 빈 이름(`a//b`), 절대 경로, 드라이브, UNC, `\`, `:`(Windows), NUL을 거절한다 (이름 속의 빈칸은 괜찮다). 스캐너처럼 심볼릭 링크와 정션은 폴더 안을 가리켜도 따라가지 않는다. 연 파일이 확인한 파일과 같은지(`fstat`) 다시 보고, 일반 파일만 읽는다.
- 프롬프트는 파일 내용 속의 지시를 따르지 말라고 하고, 파일이 `<파일 내용>` 블록을 스스로 닫지 못하게 한다.

### 프롬프트 (`explain/prompts.py`)

- `PROMPT_VERSION`은 "6"이다. 프롬프트 글이나 다듬기를 바꾸면 버전을 올려서, 옛 방식으로 만든 캐시 설명을 다시 쓰지 않게 한다.
  - 3: 실제 qwen2.5:7b·gemma3:4b 답으로 맞췄다 (큰 파일의 함수 발췌, `NO_F_SCHEMA`, 온도 0, 깨진 문장 빼기).
  - 4: 쉬운 말 바꾸기와 뜻 없는 이름표 비우기 (프롬프트 글은 3과 같다).
  - 5: `to_haeyo`의 끝말 몇 개.
  - 6: 띄어 쓰지 않은 "역할하고"는 그대로 둔다. 긴 템플릿이 먼저 오는 큰 `.vue`·`.svelte` 파일은 주요 함수가 모두 파일 끝까지 가고도 예산이 남으면 스크립트의 시작(`<script>` 줄, import와 상태)도 보낸다 (둘 다 아래). 다른 파일의 발췌와 프롬프트 글은 5와 같아서 (이 저장소, `.venv`의 패키지, Python 설치 폴더의 표준 라이브러리와 패키지에서 예산을 넘는 파일 1,123개가 글자 하나까지 같다), 아래의 잰 속도(버전 5)도 그대로다. 남은 예산을 함수 위의 줄로 다 채우는 방법도 해 봤지만, qwen2.5:7b에서 데이터가 많은 모듈의 답을 망쳐서 버렸다 (pygments의 `unistring.py`는 세 번 모두 요약이 "코사리가 주어진 파이썬 코드를 분석하고…"였다).
- `SYSTEM_PROMPT`는 요청마다 같아서 로컬 모델이 프롬프트 캐시에 둔다. 지킬 것: 모든 문장은 해요체("~합니다"는 쓰지 않는다), 코드에 없는 기능은 쓰지 않는다, 이름은 코드에 적힌 그대로, `f`의 키는 틀 그대로, `s`는 20자 안팎의 명사 이름표, `d`는 두세 문장 200자 이내, `f`의 값은 30자 안팎의 한 문장, 빈말("다양한", "중요한 역할")은 쓰지 않는다, 파일 속 지시는 따르지 않는다, 답은 JSON 객체 하나. 끝에 파일 하나와 폴더 하나의 짧은 예시가 있다.
- `file_prompt`: 파일 경로, 언어, 규칙 기반 요약(참고용), 이 파일이 쓰는 파일과 이 파일을 쓰는 파일(8개까지), 주요 함수(JSON 목록), `<파일 내용>` 블록, 끝에 채울 틀(`{"s": "", "d": "", "f": {표시: ""}}`).
  - 소스 예산은 12,000자다 (ASCII가 아닌 글자는 3자로 센다). 넘는 파일은 맨 위(예산의 30%)와 주요 함수마다 정의의 시작 부분을 한 줄씩 돌아가며 늘려 보내고, 뺀 곳에 "… (N줄 생략) …"을 쓴다. 그래서 파일 아래쪽의 함수도 이름이 아니라 코드를 보고 설명한다. 함수마다의 부분이 모두 파일 끝까지 가고도 예산이 남고 첫 주요 함수 위에 `<script>` 줄이 있으면(긴 템플릿 뒤에 `<script setup>`이 오는 `.vue` 파일), 그 함수 바로 위에서부터 `<script>` 줄까지(import와 상태)를 한 줄씩 위로 늘려 보낸다. 그 밖의 함수 위 줄은 예산이 남아도 보내지 않는다 (데이터 표가 답을 망친다). 주요 함수의 정의를 찾지 못하면 앞부분을 줄 끝에서 자른다.
- `folder_prompt`: 폴더 경로(루트는 "프로젝트 맨 위 폴더"와 폴더 이름), 규칙 기반 요약, 안에 든 것(이름과 한 줄 요약, 40개까지).
- 답 형식: 주요 함수가 없는 파일과 폴더는 `NO_F_SCHEMA`(`f`는 `{}`만 된다), 나머지는 `ANSWER_SCHEMA`다. 표시마다 키를 정한 `per_file_schema(ctx)`도 있지만 보내지 않는다. 형식에 `pattern`은 쓰지 않는다 (Ollama 0.32.6이 문법을 읽지 못하거나 지키지 않는다).
- `MODEL_OPTIONS` = `{"temperature": 0, "num_ctx": 8192, "num_predict": 2048}`. 같은 파일에 같은 답이 오도록 온도는 0이고, `num_predict`는 끝없이 쓰는 답만 막는다.
- `parse_response`: 앞뒤에 다른 글이 있어도 `s`·`d`·`f` 글이 있는 첫 JSON 객체를 읽는다. `f`는 객체나 `[[표시, 설명], …]`다. 빈 답과 잘린 답은 None이다.
- `postprocess`: 없는 것을 더하지 않고 다듬기만 한다.
  - `f`의 키를 노드의 정확한 표시로 맞춘다 (같은 키, 그다음 괄호 앞 이름을 대소문자 구분, 그다음 무시). 맞는 것이 없는 키는 버리고, 폴더에는 `f`가 없다.
  - Markdown 표시, 목록 기호, 감싼 따옴표, 남는 빈칸을 지우고, 한글에 붙은 영어 앞에 빈칸을 넣는다 ("있게Cors" → "있게 Cors").
  - 해요체로 바꾼다 (`to_haeyo`): 뜻이 하나뿐인 합니다체·한다체 끝말, 잘못 쓴 말(되요 → 돼요, 받어요 → 받아요), 받침 뒤의 "예요"(→ "이에요"), 빠진 "을"("역할 해요" → "역할을 해요"). 띄어 쓰지 않은 "역할하고"는 "하고"가 조사일 수 있어서("사용자 역할하고 권한") 그대로 둔다.
  - 쉬운 말로 바꾼다 (`plain_words`): 디렉토리·디렉터리 → 폴더, 파싱 → 해석, 유틸리티 → 도우미, "다양한" → "여러"(같은 문장에 "여러"가 있으면 지운다).
  - 깨진 문장을 뺀다: 한자·일본 글자처럼 다른 문자가 든 문장, 코드 기호로 끝나는 마지막 문장.
  - 길이: `s`는 첫 문장(끝 마침표 없이) 30자, `d`는 세 문장 200자, `f`의 설명은 두 문장 100자까지다. 이름이나 경로만 되풀이한 `s`와, 프로젝트에 대해 아무 말이 없는 루트 폴더의 `s`("프로젝트 주 폴더")는 비운다.
  - 비운 글은 규칙 기반 글이 대신한다 (`service.make_x`).

### Ollama가 꺼져 있을 때

- 상태는 `stopped`(설치됨, `can_start` true) 또는 `missing`이고, `available`은 false, `models`는 `[]`다. AI 설명을 켜 두었으면 화면의 AI 칩은 주황이고, AI 패널에는 "Ollama 켜기"(켤 수 있을 때)나 설치 안내가 보이며, 옆 패널의 AI 줄은 상태 문장과 "AI 설정 열기"다 (7-1).
- 이미 만든 설명은 그대로 보인다: `/api/analyze`는 설정에 저장된 모델의 캐시 설명을 붙이고, `/api/ai/explain`도 캐시에 있으면 답한다. 새 설명과 전체 설명은 409 `not_running`이다.
- 설명하는 도중에 Ollama가 꺼지면 그 요청은 409 `not_running`이고 상태를 바로 다시 묻는다. 전체 설명은 "Ollama가 꺼져서 멈췄어요. 그때까지 만든 설명 N개는 저장해 두었어요."로 멈춘다 (`failed`).

### 잰 속도와 품질

2026-11-20과 2026-11-21에 RTX 5060(8GB), Ollama 0.32.6에서 모델이 모두 GPU에 올라간 상태로 잰 값이다. 따로 적지 않으면 qwen2.5:7b다.

- 첫 답: 컴퓨터를 켠 뒤 처음 모델을 불러올 때는 불러오기만 25\~29초, 첫 답까지 34\~57초였다 (2026-11-20, gemma3:4b는 25초·34초, qwen2.5:7b는 29초·57초). 모델 파일이 이미 메모리에 있으면 첫 답까지 4\~8초쯤이었다. 화면의 "처음 한 번은 … 30초쯤" 안내는 이 사이의 값이다.
- 그 뒤: 파일 하나에 1\~10초(보통 2\~5초, 주요 함수가 많을수록 길다), 폴더 하나에 1\~2.5초.
- 전체 설명: 예제 프로젝트 17개가 약 24초(2026-11-21에 다시 재어 23초), 이 저장소는 65개 가운데 설명이 없던 53개가 약 4분(2026-11-21, 프롬프트 버전 5. 나머지 12개는 이미 만든 설명이 캐시에 있었다)이었다. 2026-11-20에 프롬프트 버전 2로 잰 이 저장소 약 50개는 약 3분 반(213\~217초)이었다.
- 평가한 답에서 다듬은 뒤 남은 합니다체·한다체는 없었다. `x.f`는 늘 노드의 표시 그대로다.
- 내용은 틀릴 수 있다. 프롬프트를 맞출 때 쓰지 않은 새 묶음(표준 라이브러리, pip, 예제의 폴더 등)의 답 48개를 코드와 견주어 보니 18개에 코드와 다른 말이 하나 이상 있었다 (5개는 큰 잘못, 대부분 비공개 도우미 함수의 설명). 모델을 고를 때 견준 70개(설계 결정 문서의 8개)는 70개 중 49개가 프롬프트를 맞추는 데 쓴 항목이어서 틀린 답이 더 적었다. 온도가 0이어도 요청 순서가 다르면 답이 달라질 수 있고(48개 중 21개), 캐시는 처음 받은 답을 쓴다.

### 아직 정하지 않은 것

- 사용자가 `OLLAMA_HOST`를 `0.0.0.0`처럼 정해 두었으면 "Ollama 켜기"로 켠 Ollama도 그 주소에서 듣는다 (CodeVision은 127.0.0.1로만 말한다). 늘 설정의 주소를 넘길지는 정하지 않았다.
- CodeVision이 켠 Ollama를 CodeVision을 끌 때 함께 끌지 (지금은 모델만 내린다).
- `per_file_schema`를 보내면 `f`의 키가 더 정확해지지만, 백엔드 테스트 두 곳을 바꿔야 한다.

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
- AI 칩: 머리줄의 "AI 설명 꺼짐"·"AI 설명 켜짐"은 늘 버튼이다 (`aria-haspopup="dialog"`, `aria-expanded`, `aria-controls="cv-ai"`). 꺼져 있으면 회색, 쓸 수 있으면 파랑, 켰지만 쓸 수 없으면(Ollama가 꺼짐·없음, 모델 없음) 경고 아이콘과 주황이다. 전체 설명을 만드는 동안은 도는 아이콘과 "· 12/40"이 붙는다. 툴팁은 서버의 상태 문장이다.
- AI 패널(`#cv-ai`, `role="dialog"`): 칩을 누르면(Enter, Space 포함) 칩 아래에 360px 팝오버로 열리고, 패널 제목으로 포커스가 간다. 560px 이하에서는 화면 아래쪽 시트로 열린다 (뒤 가림막, `aria-modal`, Tab과 Shift+Tab은 시트 안에서만 돈다, 가림막을 누르면 닫히고 연 버튼(보통 칩)으로 포커스가 돌아간다).
  - 담긴 것: 서버의 상태 문장(`role="status"`), "Ollama 켜기"(AI를 켰고 `can_start`일 때. 누르면 "켜는 중…"과 20초쯤 걸릴 수 있다는 문장), 설치 안내 "ollama.com에서 Ollama를 받아 설치해 주세요."(Ollama가 없을 때), "AI 설명 쓰기" 스위치(`role="switch"`), 모델 목록(추천 모델 먼저, 크기와 "추천" 표시. Ollama가 없으면 숨긴다. 목록이 없으면 AI를 켰고 고른 모델이 있을 때만 그 모델 하나를 보여 준다. 목록이 없거나 전체 설명 중이면 고를 수 없다), 오류(`role="alert"`), "전체 설명 만들기".
  - "Ollama가 켜지면 설치된 다른 모델도 고를 수 있어요."는 Ollama가 꺼져 있을 때만 보인다 (켜져 있는데 목록이 비었으면 모델이 없다는 뜻이라서).
  - 열 때마다 `GET /api/ai/status`를 다시 읽는다. 서버가 그때 Ollama에 새로 물으므로 CodeVision 밖에서 켜거나 끈 Ollama가 바로 보인다.
  - Esc는 확인 질문이 떠 있으면 질문만 닫고, 아니면 패널을 닫고 연 버튼(칩, 또는 옆 패널의 "AI 설정 열기")으로 포커스를 돌린다. 넓은 화면의 팝오버는 바깥을 누르거나 포커스가 밖으로 나가면 닫힌다. 컨트롤이 사라지면(작업이 시작됨, Ollama가 켜짐) 포커스는 다음 쓸 만한 컨트롤로 간다.
- 전체 설명 만들기:
  - 누르기 전에 남은 수와 걸릴 시간을 알려 준다. 남은 수는 `x`도 `xs`도 없는 코드 파일과 폴더이고, 서버가 거절한 파일도 뺀다. 시간은 폴더 1초, 파일 2초 + 주요 함수마다 0.55초(8개까지)로 어림한다 (qwen2.5:7b로 잰 값. 기본 모델을 바꾸면 app.js의 `AI_DIR`, `AI_FILE`, `AI_FN`을 다시 재야 한다). 5분이 넘으면 "N개를 만들려면 약 M분 걸려요. … 시작할까요?"를 먼저 묻는다 (시작하기·취소).
  - 남은 것이 없으면 "모든 코드 파일과 폴더에 AI 설명이 있어요."이고, AI에 보내지 않는 파일이 있으면 "AI에 보내지 않는 파일 N개를 빼면 모두 AI 설명이 있어요."다. 이때 버튼은 누를 수 없다.
  - 진행 중에는 서버의 문장, 완료/전체, 진행 막대(`aria-valuenow`, `aria-valuemax`, `aria-valuetext`), "지금: 경로", 남은 시간, 실패한 항목 수, "멈추기"를 보여 준다. 멈추기는 지금 항목까지만 하고 멈추고, 그동안 "멈추는 중…"이다. 남은 시간은 아직 설명이 없는 항목의 어림값에, 첫 새 답이 온 뒤에 잰 속도를 곱한다 (캐시에서 바로 끝난 항목이 속도를 부풀리지 않게). 1분 안이면 "곧 끝나요."다.
  - "처음 한 번은 AI가 준비하느라 30초쯤 걸릴 수 있어요."는 그 작업이 첫 항목을 시작하고 3초가 지나도 새 답이 없을 때만 보인다. 다른 폴더의 작업을 기다리는 동안에는 서버의 문장만 보인다.
  - 1초마다 `GET /api/ai/job`(`since=next`)으로 확인하고, 결과는 1.5초에 한 번 이하로 묶어서 트리에 넣는다. 끝나면 서버의 마지막 문장이 남는다 (다 만들었으면 초록, 실패면 빨강). 새로 고침한 뒤나 다른 탭에서 시작한 작업은 "전체 설명 만들기"가 200으로 그 작업을 돌려줄 때 다시 따라간다.
- AI가 스스로 보내 온 설명(전체 설명의 묶음, 앞서 고른 항목의 답)은 트리 배치를 다시 계산하지 않고 그 자리의 글자만 바꾼다. 칸보다 긴 글은 말줄임(…)으로 잘리고, 전체 글은 툴팁과 패널에 있다. 배치는 사용자가 무언가를 누르거나 창 크기가 바뀔 때, AI를 켜고 끄거나 모델을 바꿀 때, 그리고 전체 설명 만들기가 끝날 때 한 번 다시 계산한다. 그래서 작업 중에는 원과 선이 움직이지 않는다. 다시 계산해도 고른 항목, 펼친 폴더, 스크롤 위치는 그대로다.
- AI가 켜져 있으면 트리 이름 아래 요약과 옆 패널의 글은 `node.x`를 쓴다 (주요 함수는 `node.f`의 표시와 순서 그대로, AI가 비운 설명은 규칙 기반 글). 옆 패널의 경로 아래 AI 줄은 다음 중 하나다.
  - "AI 설명" 표시와 "기본 설명 보기". 누르면 규칙 기반 글로 바뀌고 "AI 설명 보기"가 된다.
  - 기다리는 중: "AI가 설명을 쓰고 있어요…". 물은 지 3초가 지나면 "처음 한 번은 AI가 준비하느라 30초쯤 걸릴 수 있어요."가 붙는다. AI를 켜거나 모델을 바꾼 뒤 `/api/analyze`의 답을 기다리는 동안에는 아직 묻지 않아서 "이미 만들어 둔 설명을 확인하고 있어요…"다 (AI 패널의 어림값 자리도 같다). 다만 고른 항목을 지금 모델에 이미 묻고 있으면(묻는 사이에 AI를 껐다가 같은 모델로 다시 켰을 때) 그 항목의 줄은 "AI가 설명을 쓰고 있어요…"와 3초 뒤의 문장을 그대로 보여 준다.
  - 오류 문장과 "다시 시도". 전체 설명을 만드는 중에 받은 `busy`는 "전체 설명을 만드는 중이에요. 이 항목 차례가 오면 설명이 보여요."이고, 작업이 끝나면 다시 묻는다.
  - 서버가 거절한 파일(`not_code`, `secret`, `too_big`)의 문장. 그 화면에서는 다시 묻지 않는다.
  - AI가 받지 않는 코드 파일(`node.xs`): 정보 아이콘과 "비밀 정보가 들어 있을 수 있는 파일이라 AI에 보내지 않아요.", "파일이 너무 커서 AI 설명을 만들지 않아요.", "이 파일을 읽을 수 없어서 AI 설명을 만들지 않아요." 중 하나. 서버에 묻지 않고, 다시 시도도 없다.
  - Ollama가 꺼져 있거나 모델이 없을 때: 서버의 상태 문장과 "AI 설정 열기".
- 고른 코드 파일과 폴더만, 한 번에 하나씩 `/api/ai/explain`에 묻는다. 답이 오면 그때 고른 항목이 다음 차례다. 다른 항목을 고른 뒤에 온 답은 그 항목의 트리 이름에 쓰이고, 옆 패널은 지금 고른 항목 그대로다. 그 항목을 다시 고르면 패널에도 그 답이 보인다. 코드가 아닌 파일은 규칙 기반 글만 보인다.
- 화면 읽기 프로그램: `#cv-say`(`aria-live="polite"`)가 AI 패널이 닫혀 있어도 답, 오류, 전체 설명의 시작과 끝을 알린다. 화면에 보이는 답만 알린다: AI가 켜져 있고, 지금 모델의 답이고, 그 항목을 고르고 있을 때만 "AI 설명이 나왔어요."라고 한다. AI를 켜거나 모델을 바꾼 뒤 `/api/analyze`가 고른 항목에 없던 설명이나 새 `xs`를 가져와도 알린다 ("AI 설명이 나왔어요." 또는 위의 정보 문장). 다만 전에 받은 AI 설명이 남아 있는 항목은 화면이 그 설명을 계속 보여 주므로 새 `xs`가 생겨도 알리지 않고, 고른 항목을 지금 모델에 이미 묻고 있으면 그 물음의 답이 올 때 한 번만 알린다.
- AI가 꺼져 있으면 트리와 패널은 AI가 없던 때와 같고, 화면이 스스로 AI 요청을 보내지 않는다 (AI 패널을 열 때 상태만 묻는다). AI를 켜거나 모델을 바꾸면 `/api/analyze`를 한 번 더 불러 그 모델로 이미 만든 `x`와 `xs`를 함께 반영한다 (모델은 부르지 않는다). 그 답이 올 때까지는 고른 항목의 설명도 묻지 않고, 답이 온 뒤에 아직 설명이 필요하면 묻는다. 모델이 바뀌면 그 전 모델의 `x`는 지운다. AI를 끈 사이에 모델이 바뀌면, 그 전에 부른 `/api/analyze`의 답은 `xs`만 쓰고 `x`는 버린다 (그 전 모델의 설명이다). 전체 설명 중에 AI를 끄면 화면이 먼저 멈추기를 보낸다.
- 경로 표시: 분석할 폴더의 경로(머리줄의 출처 줄, 시작 화면의 최근 목록, 오류 화면의 메시지 속 경로와 경로 줄)는 모두 `pathHtml()`로 그린다. `\`나 `/` 바로 뒤에서만 줄을 바꾸고, 한 조각이 줄보다 길 때만 그 안에서 끊는다 (조각마다 `display:inline-block`, 조각 사이에 `<wbr>`). 조각 앞의 공백은 조각 밖에 두어서 일반 글처럼 보이고 복사된다 (inline-block 맨 앞의 공백은 브라우저가 지운다). 조각이 따로 상자라서 페이지 안 찾기(`window.find`)는 `\`·`/`를 사이에 둔 글을 찾지 못하고 조각 하나 안의 글만 찾는다 (Ctrl+F 찾기 막대로는 확인하지 않았다).
- 머리줄: 출처 줄의 "로컬 폴더 · 경로"는 한 덩어리 글이라 경로가 "로컬 폴더 ·" 바로 뒤에 이어진다. 숨긴 폴더는 앞의 두 이름만 쓰고, 그보다 많을 때만 " 등"을 붙인다 ("__pycache__ 숨김", "__pycache__, dist 등 숨김"). 숨긴 이름 전체는 툴팁으로 보인다.
- 글자 줄바꿈: 한국어 문장 칸(시작 화면 안내, 오류 메시지, 경고, 한눈에 보기, 범례, 패널의 요약·설명·주요 함수 설명, 옆 패널의 AI 줄, AI 패널의 문장 등)은 낱말 안에서 줄을 바꾸지 않는다(`word-break: keep-all`). 띄어쓰기 없이 줄보다 긴 글만 아무 데서나 끊는다. 경로와 파일 이름 칸(출처 줄, 최근 목록, 오류 화면의 경로 줄, 패널의 경로·이름·파일 칩, AI 패널의 "지금:" 줄 등)에는 이 규칙을 쓰지 않는다. 오류 메시지 속 경로(`.er-p`)는 메시지 칸의 `keep-all`을 물려받지만, 조각 사이에서 줄을 바꾸는 것은 같아서 줄보다 긴 조각 안에서만 차이가 난다.
- 화면이 바뀔 때 포커스: 프로젝트를 열면 프로젝트 제목, "다른 폴더"를 누르면 "폴더 선택…" 버튼, 오류 화면이면 "다시 시도"로 옮긴다. 처음 화면을 불러올 때는 옮기지 않는다.
- 시작 화면: 최근 목록의 버튼은 접근성 이름(`aria-label`)을 "이름 경로" 한 덩어리로 받는다. 조각으로 나뉜 경로에서 이름을 만들게 두면 Chromium이 조각 사이를 띄어서 "C:\ Users\ …"처럼 만들기 때문이다.
- 오류 화면: 메시지가 연 경로를 이미 말하면(서버의 "폴더를 찾을 수 없어요: C:\…") 아래의 경로 줄은 넣지 않고, 메시지 속 경로만 경로 표시로 그린다. 경로 줄은 메시지가 경로를 말하지 않을 때만 보인다.
- 잘림 경고는 서버가 보낸 한 줄만 보여 준다. 그래프가 잘렸는데 경고가 없을 때만 화면이 일반 문장을 넣는다.
- `design/mockup.html`은 승인된 디자인 원본이다. 앱은 실제 프로젝트에 맞추느라 그 뒤에 다음을 더하거나 바꿨다.
  - 배치: 줄바꿈 트리, 혼합 배치, 꺾인 선, 옆 패널.
  - 패널과 글자: 천 단위 쉼표를 쓰는 개수(시안 코드는 숫자를 그대로 붙인다), 이름 아래에 들여 쓴 주요 함수 설명(시안은 이름 옆에 이어 쓴다), 설명과 겹치면 빠지는 굵은 요약 줄(시안은 늘 보인다), 한 덩어리로 이어지고 경로를 `\`·`/` 뒤에서만 끊는 출처 줄(시안은 조각마다 따로 놓인다), 숨긴 폴더가 두 개 이하면 " 등"을 빼는 숨김 표시(시안은 늘 "… 등 숨김"), 낱말을 끊지 않는 한국어 줄바꿈.
  - 시안에 없는 화면: `mockup.html`에는 시작 화면(최근 목록 포함)과 오류 화면이 없다. 그래서 그 화면의 경로 표시(최근 목록 `.rc-p`, 오류 메시지 속 `.er-p`, 경로 줄 `.er-path`)와 최근 목록 버튼의 `aria-label`도 앱에만 있다.
  - AI: 머리줄의 AI 칩과 AI 패널, 옆 패널의 AI 줄, 트리 이름 아래의 AI 요약은 시안에 없고 앱에만 있다.

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

테스트는 진짜 Ollama를 쓰지 않는다. `tests/conftest.py`가 `CODEVISION_OLLAMA_URL`을 아무것도 듣지 않는 포트로, `CODEVISION_OLLAMA_EXE`를 없는 파일로 정하고, 11434번 포트(진짜 Ollama) 연결과 `ollama` 프로그램 실행을 막는다. 모델의 답은 `tests/fake_ollama.py`가 만든다 (`fake_ollama` 픽스처). 설정이나 캐시를 쓰는 테스트는 `CODEVISION_HOME`을 pytest 임시 폴더로 정해서 사용자의 `config.json`과 `cache.sqlite`를 건드리지 않는다.
