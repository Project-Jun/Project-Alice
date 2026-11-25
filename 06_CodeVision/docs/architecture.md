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

GitHub 저장소는 이 흐름 앞에 한 단계가 더 있다. 서버가 브랜치의 커밋 하나를 사용자 데이터 폴더의 스냅샷 폴더로 받아 두고(`/api/github/open`과 내려받기 작업), 화면이 `/api/analyze?github=`로 그 폴더를 위의 흐름 그대로 분석하게 한다. 분석과 AI 설명은 GitHub에 묻지 않는다 (6-1).

## 2. 파일과 담당

| 파일 | 하는 일 |
|---|---|
| `src/codevision/model.py` | 공용 데이터 클래스(FileFacts 등), ProjectIndex, HTTP 경로 정규화·매칭, 별이 든 이름 패턴을 되짚기 없이 맞추는 정규식(`join_stars`, `atomic`) |
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
| `src/codevision/github/` | GitHub 연동 (6-1). `__init__.py`는 아래 모듈의 공용 이름을 둔다 (`GitHubService`, `Snapshot`, `SnapshotStore`는 처음 쓸 때 불러온다. 테스트 가드가 이 패키지를 늘 불러도 가볍다) |
| `src/codevision/github/service.py` | `GitHubService`(서버마다 하나): 아래 모듈을 묶고, 상태 블록, 60초 캐시, 저장소 열기, 내려받기 작업, 로그아웃과 계정 바꾸기, 비공개 스냅샷 규칙 |
| `src/codevision/github/base.py` | 상수(Client ID, 주소, API 버전, 환경 변수 이름), `GitHubError`(계약 코드와 HTTP 상태), `Secret`(메모리의 토큰), `Hosts`(진짜 GitHub, 가짜 GitHub, 끔), 이름 검사, AI 프로젝트 id |
| `src/codevision/github/client.py` | `GitHubClient`: GitHub에 보내는 모든 HTTP(`urllib`). HTTPS, 정해 둔 리다이렉트만, 로그인 붙이기와 다시 보내기, 요청 한도, 오류 코드. `Download`: 받는 중인 tarball |
| `src/codevision/github/store.py` | `TokenStore`: 로그인 정보 저장 (Windows 자격 증명 관리자, 다른 OS의 keyring, 아니면 메모리) |
| `src/codevision/github/auth.py` | `TokenManager`(토큰의 수명과 갱신), `DeviceLogin`(기기 코드 로그인과 그 확인 스레드) |
| `src/codevision/github/repos.py` | 붙여 넣은 입력 읽기, 설치·저장소·브랜치 목록, 저장소 정보, 브랜치의 최신 커밋 |
| `src/codevision/github/extract.py` | 우리 스트리밍 압축 풀기 `extract_stream()`: Windows에서 위험한 이름, 링크, 스캐너가 숨기는 폴더, LFS·바이너리, 한도 |
| `src/codevision/github/snapshot.py` | `SnapshotStore`: 스냅샷 폴더 배치, 오프라인 찾기, 올리기(publish), `refs.json`, 정리, 스냅샷의 경고 문장 |
| `src/codevision/github/messages.py` | GitHub 쪽이 화면에 보여 주는 한국어 문장 모음 |
| `src/codevision/server.py` | 로컬 웹 서버 (표준 라이브러리 `http.server`). `/api/ai/*`는 `explain.AIService`가, `/api/github/*`는 `github.GitHubService`가 처리한다. GitHub 스냅샷의 그래프(`build_github_graph`)와 최근 목록(`recent.json`)도 여기 있다 |
| `src/codevision/__main__.py` | 실행 진입점 `python -m codevision` |
| `src/codevision/web/` | 화면: `index.html`, `app.js`(트리, 옆 패널, AI 패널, 시작 화면의 GitHub 카드), `app.css`, `icons.css`(아이콘. 인터넷 없이도 보이도록 앱 안에 들어 있음) |
| `src/codevision/demo/my-todo-app/` | 예제 프로젝트 (화면의 "예제 프로젝트 열기"와 테스트에 사용) |
| `tests/` | pytest 테스트, `tests/fixtures/sample-graph.json`은 화면 개발용 예시 그래프 (AI 상태 블록과 `x` 예시 포함) |
| `tests/fake_ollama.py` | 테스트와 화면 확인에 쓰는 가짜 Ollama 서버. 모델 없이 Ollama 0.32와 같은 모양으로, 프롬프트에서 만든 해요체 답을 준다. 테스트가 불러 쓰고, `python tests/fake_ollama.py --port N`으로 따로 띄울 수도 있다 |
| `tests/fake_github.py` | 테스트와 화면 확인에 쓰는 가짜 GitHub 서버. loopback 서버 하나가 github.com(기기 코드 로그인), api.github.com, codeload.github.com을 흉내 낸다 (9장). 테스트가 불러 쓰고, `python tests/fake_github.py --port N --demo`로 따로 띄울 수도 있다 |

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
- `source.kind`는 `local`(로컬 폴더), `demo`(예제 프로젝트, path는 빈 문자열), `github`(받아 둔 GitHub 스냅샷, 6-1)다. 로컬 폴더와 예제의 `source`는 `kind`와 `path` 두 키뿐이다.
- GitHub 스냅샷의 `source`: `{"kind": "github", "path": "", "owner": "octocat", "repo": "Hello-World", "branch": "main", "sha": "<커밋 40자리>", "private": false, "url": "https://github.com/octocat/Hello-World", "project": "github:octocat/hello-world@<커밋 40자리>"}`.
  - `owner`, `repo`는 GitHub의 이름 그대로(대소문자 포함)다. `branch`는 연 브랜치로 이름표일 뿐이고, 스냅샷은 `sha`가 고른다 (비었으면 받을 때의 브랜치). `url`은 검사한 이름으로 만든 저장소 페이지다 (붙여 넣은 글로 만들지 않는다. 가짜 GitHub에서는 그 페이지). `project`는 AI 요청에 쓰는 프로젝트 id다 (6장).
  - `path`는 예제처럼 `""`이다. 그래서 스냅샷 폴더의 경로는 화면, 주소, 오류 화면, `recent.json`에 가지 않는다.
  - 그래프의 `name`과 루트 노드의 `n`, `p`는 저장소 이름이다 (Windows가 쓸 수 없는 이름이라 스냅샷 폴더가 `repo`인 때도). `warnings`는 스냅샷이 건너뛴 것의 경고(6-1)가 분석기의 경고보다 먼저다.
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
    - 패턴과 정규식은 분석하는 저장소의 글이라, 맞추는 데 시간이 들지 않게 다룬다 (2026-11-25). glob과 실행할 때 만드는 이름(Python의 `import_module(f"pkg.{a}x{b}")`도)은 별 사이의 고정된 조각을 되짚지 않는다 (`model.join_stars`. 그래서 `[…]`도 `/`에는 맞지 않는다). `{a,b}`가 든 칸에는 별이 하나까지이고, `**` 칸이 둘 이상이면 그 사이의 칸이 점으로 시작하지 않아야 한다 (아니면 잇지 않는다). `require.context`의 정규식은 열린 반복(`*`, `+`, `{n,m}`)이 하나(맨 앞이 `^`이면 둘)까지이고, 반복 안의 반복과 `|`, 되돌아보기(``)가 없고, 한 자리에서 고를 길이 16가지까지일 때만 쓰고 (`js_lang._regex_is_cheap`), 300자가 넘는 `./경로`에는 쓰지 않는다. `/(a|aa)+$/`나 `/a*a*a*a*b/`는 긴 이름 하나에 몇 분이 걸렸다.
    - Python: `import_module()`·`__import__()`의 이름을 f-string, `+`, `%`, `.format()`이나 그런 값을 담은 변수로 만들고 앞에 패키지를 적었으면(`f"pkg.plugins.{name}"` → `pkg.plugins.*`) 그 패키지에서 맞는 모듈(`x.py`, `x/__init__.py`. 패키지 자신과 `__main__`은 빼고)이다. `*` 뒤에 이름이 더 있으면(`pkg.*.handler`) 하위 패키지(`__init__.py`가 없는 폴더도)로 내려간다. 상대 이름은 `__package__`(패키지의 `__init__.py`에서는 `__name__`도)를 기준으로 한다: `import_module(f".unicode{v}", __package__)` → 그 패키지의 `unicode*`, `f"{__package__}.plugins.{name}"` → `plugins.*`.
    - 알 수 없는 이름은 잇지 않는다: 적힌 부분이 없는 이름(`require(name)`, `import_module(config["module"])`), 패키지만 적힌 이름(`` import(`./${name}`) ``, `import_module("." + name, __package__)`), 다른 패키지의 모듈(`` import(`lodash/${fn}`) ``), 이 분석기가 읽지 못하거나 시간이 들 수 있는 정규식(위). 프로젝트 폴더 밖의 파일은 가리키지 않는다.
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
- **무시 폴더**: `.git`, `node_modules`, `.venv`, `venv`, `env`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `dist`, `build`, `.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.cache`, `.parcel-cache`, `coverage`, `.idea`, `.vscode`, `*.egg-info`, 그리고 루트 `.gitignore`의 단순 패턴. 단 이름이 Python 식별자인 것(`build`, `dist`, `env`, `venv`, `coverage`, `node_modules`, `__pycache__`. `scan.PACKAGE_NAMES`)은 `__init__.py`가 있으면 Python 패키지이므로 숨기지 않는다. GitHub 스냅샷을 풀 때도 같은 규칙을 쓴다 (6-1). `.gitignore`의 잘못된 패턴과 4,096자가 넘는 패턴은 건너뛴다. 패턴은 git처럼 맞춘다: `*`·`?`·`[…]`는 `/`를 넘지 않고, 폴더 한 칸 전체인 `**`는 폴더 여러 개(맨 끝의 `a/**`는 그 안의 모든 것)이고, 그 밖의 `**`는 `*`와 같다. 별 사이의 고정된 조각은 처음 맞는 자리에서 다시 되짚지 않는다 (`model.join_stars`. `**` 칸 뒤의 칸들도 같다). 그래서 다른 사람 저장소의 `*a*a*a…*b` 같은 패턴도 시간이 들지 않는다. 그대로 정규식으로 바꾸면 긴 이름 하나에 몇 분이 걸렸고, Python의 `re`는 그동안 GIL을 쥐고 있어서 서버 전체가 멈췄다 (2026-11-25 수정).
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
- CSP: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'`. 아이콘까지 앱 안에서 제공하므로 화면은 다른 사이트에 아무것도 요청하지 않는다 (GitHub에는 서버만 연결한다, 6-1). `base-uri 'none'`과 `form-action 'none'`(2026-11-25 추가)은 남의 저장소 이름이나 브랜치 이름에 섞여 들어온 마크업이 링크의 기준 주소를 바꾸거나 폼을 다른 곳으로 보내지 못하게 한다. index.html에는 인라인 `<script>`를 쓰지 않는다.

| 요청 | 응답 |
|---|---|
| `GET /` | index.html (토큰 삽입) |
| `GET /app.js`, `/app.css`, `/icons.css` | 정적 파일 (web 폴더의 정해진 파일만) |
| `GET /api/analyze?path=<절대 경로>` | 그래프 JSON. 오류는 4xx `{"error": "한국어 메시지"}` |
| `GET /api/analyze?demo=1` | 예제 프로젝트 그래프. `source`는 `{"kind": "demo", "path": ""}`, 최근 목록에 넣지 않음 |
| `GET /api/analyze?github=<owner>%2F<repo>&branch=<브랜치>&sha=<커밋 40자리>` | 받아 둔 GitHub 스냅샷의 그래프 (6-1). GitHub에는 묻지 않는다. `github`가 있으면(값이 비어도) `path`·`demo`보다 먼저다 |
| `POST /api/pick-folder` | `{"path": "C:\\…"}` 또는 취소 시 `{"path": null}` (tkinter 폴더 선택 창) |
| `GET /api/recent` | `{"items": [...]}` 새것부터 10개 (폴더와 GitHub 저장소를 합쳐). 폴더는 `{"path", "name", "at"}`, GitHub 저장소는 `{"kind": "github", "owner", "repo", "branch", "private", "name", "path": "", "at"}` (6-1) |
| `GET /api/ai/status` | AI 상태 블록 (4장 `ai`). 부를 때마다 Ollama에 새로 묻는다 (7장) |
| `POST /api/ai/settings` | `{"enabled": bool, "model": str}`(`model`은 없어도 됨)을 저장하고 200 AI 상태 블록. 모델 이름이 잘못됐거나, Ollama가 켜져 있고 설치 목록에 없는 모델이면 400 `{"error"}` |
| `POST /api/ai/start` | `{}`. 설치된 Ollama를 켜고 답할 때까지(최대 20초) 기다린 뒤 200 AI 상태 블록. 이미 켜져 있으면 그대로 200 |
| `POST /api/ai/explain` | `{"project": 화면이 분석한 프로젝트, "node": 노드 id}`. `project`는 로컬 폴더면 그 경로(그래프의 `source.path`), 예제면 `"demo"`, GitHub 스냅샷이면 `source.project`(`"github:<owner 소문자>/<repo 소문자>@<sha>"`)다. 코드 파일(`t`가 `f`이고 `lang`이 있음)과 폴더(루트 `.` 포함)만 설명한다. 200 `{"node": id, "x": {"s", "d", "f"}, "cached": bool}` |
| `POST /api/ai/explain-all` | `{"project": …}`. 202 작업 블록. 같은 프로젝트의 작업이 이미 돌고 있으면 200과 그 작업 블록 |
| `GET /api/ai/job?id=<id>&since=<k>` | 200 작업 블록. `results`에는 앞의 k개 뒤에 끝난 것만 있다 |
| `POST /api/ai/job/cancel` | `{"job": id}`. 200 작업 블록. 작업은 지금 만드는 항목까지만 하고 멈춘다 |
| `/api/github/*` | GitHub 로그인, 저장소 목록, 열기, 내려받기 작업 (6-1의 표) |

사용자 데이터 폴더: `%LOCALAPPDATA%\CodeVision` (다른 OS는 `~/.codevision`). 환경 변수 `CODEVISION_HOME`이 있으면 그 폴더를 쓴다 (테스트·연습 실행용. 이때 GitHub 로그인은 메모리에만 둔다, 6-1). 경로를 정하는 함수는 `codevision.explain.data_dir()`다. 그 안에는 AI 설정 `config.json`과 AI 설명 캐시 `cache.sqlite`(7장), 최근 목록 `recent.json`, GitHub에서 받은 스냅샷 `repos/`(6-1)가 있다. GitHub 쪽(스냅샷과 GitHub 최근 항목)은 서버를 만들 때의 폴더(`server.home`)를 끝까지 쓴다. GitHub 로그인 정보는 이 폴더가 아니라 OS 자격 증명 저장소에 있다.

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
- `github:`로 시작하는 `project`는 오프라인으로 `GitHubService.snapshot()`에서 찾는다 (`server.project_from_text`. 비공개 스냅샷 규칙이 같다, 6-1). 모양이 틀리면 400 `{"error": "GitHub 저장소 정보를 읽을 수 없어요. 저장소를 다시 열어 주세요."}`, 그 스냅샷이 없거나 이 로그인의 것이 아니면 404 `{"error": "받아 둔 저장소를 찾지 못했어요. 저장소를 다시 열어 주세요.", "code": "not_found"}`다. 상대 경로(`relative/folder`)와 `owner/repo`만 쓴 글은 전처럼 폴더 경로로 읽어 400이다.
- 예전 뼈대의 `POST /api/explain`은 없어졌다. 다른 없는 주소처럼 404 `{"error": "찾는 주소가 없어요."}`다.
- 작업 블록: `{"job": id, "state": "running" | "done" | "cancelled" | "failed", "done": int, "total": int, "errors": int, "current": 노드 id 또는 "", "message": 한국어, "results": [[노드 id, x], …], "next": int}`. 다음 확인은 `since=next`로 한다. `total`에는 비밀 파일을 넣지 않는다.
- AI 오류는 `{"error": 한국어, "code": …}`이고, 코드마다 HTTP 상태가 정해져 있다 (`explain.AIError.STATUS`. 표 아래의 `failed` 두 경우, 409와 500만 다르다).

| HTTP | `code` | 언제 |
|---|---|---|
| 409 | `off` | AI 설명이 꺼져 있다 |
| 409 | `not_running` | Ollama가 꺼져 있거나, 답하다가 꺼졌거나, 모델 목록을 주지 않는다. 주소가 이 컴퓨터가 아닐 때도 이 코드다 |
| 409 | `no_model` | 설치된 모델이 없거나, 고른 모델이 설치되어 있지 않다 |
| 409 | `busy` | 기다리는 요청이 이미 6개다. CodeVision이 꺼지는 중일 때도 이 코드다 |
| 404 | `not_found` | 그래프에 없는 노드, 링크·정션을 지나는 노드, 없어진 프로젝트 폴더나 GitHub 스냅샷, 읽지 못한 파일, 모르는 작업 id, GitHub 로그아웃·계정 바꾸기로 닫힌 프로젝트의 기다리던 요청 (`PROJECT_CLOSED`) |
| 422 | `not_code` | 코드 파일도 폴더도 아니다 |
| 422 | `secret` | 비밀이 들어 있을 수 있는 파일이다 (7장) |
| 422 | `too_big` | `explain.MAX_SOURCE_BYTES`(1,000,000바이트)보다 크다 |
| 504 | `timeout` | 모델이 제때 답하지 않는다 (한 번의 답에 120초, 요청 하나는 차례를 기다리는 시간까지 180초) |
| 502 | `bad_answer` | 모델의 답을 읽지 못했거나, 다듬고 나니 남은 글이 없다 |
| 502 | `failed` | Ollama의 오류(HTTP 500, 메모리 부족 등), 끊긴 연결, 다시 분석하다 난 오류 |

- `POST /api/ai/start`의 오류: Ollama를 찾지 못하면 409 `missing`, 켜지 못했거나 `ollama serve`가 답하기 전에 끝나면 502 `failed`, 20초 안에 답하지 않으면 504 `timeout`, 주소가 이 컴퓨터가 아니면 409 `failed`(주소 안내 문장).
- `POST /api/ai/settings`가 설정 파일을 저장하지 못하면 500 `failed`다.
- 서버를 닫으면(`server_close`) GitHub 쪽을 먼저 닫고(6-1), 기다리던 AI 요청은 바로 409 `busy`로 답을 받는다.
- 환경 변수(테스트·개발용): `CODEVISION_OLLAMA_URL`은 설정의 Ollama 주소를 바꾼다 (이 컴퓨터 주소만). `CODEVISION_OLLAMA_EXE`는 "Ollama 켜기"가 실행할 프로그램을 바꾼다 (`serve` 인수와 함께 실행하고, `.py` 파일은 이 Python으로 실행한다). GitHub 쪽의 환경 변수는 6-1.

## 6-1. GitHub 연동

GitHub 저장소의 브랜치 하나를 커밋 하나의 스냅샷 폴더로 받아 두고, 그 폴더를 로컬 폴더처럼 분석한다 (3단계, 2026-11-25. 결정과 한도 표는 `docs/decisions.md` "GitHub 연동"). GitHub와는 이 PC의 서버만 이야기하고, 화면은 127.0.0.1에만 요청한다. 그래서 CSP의 `connect-src 'self'`는 그대로다. 분석(`/api/analyze?github=`)과 AI 설명은 GitHub에 묻지 않고 받아 둔 스냅샷만 읽는다.

### 모듈과 흐름

- `github.GitHubService`(`service.py`)가 서버마다 하나 있고(`server.github`), 다음을 모두 갖는다. 만들 때는 디스크와 네트워크를 건드리지 않는다 (로그인 저장소와 프록시 설정도 처음 쓸 때 고른다).
  - `TokenStore`(로그인 정보 저장), `TokenManager`(토큰과 갱신), `DeviceLogin`(기기 코드 로그인).
  - `GitHubClient`(GitHub에 보내는 모든 HTTP). `repos.py`의 함수들이 이것으로 목록과 최신 커밋을 묻는다.
  - `SnapshotStore`(받아 둔 스냅샷)와 `extract_stream`(내려받는 압축 파일 풀기).
  - 60초 캐시(저장소 목록, 설치, 저장소 정보, 브랜치 목록)와 내려받기 작업.
- 서버(`server.py`)의 `/api/github/*` 경로 10개는 서비스의 메서드를 부르고, `GitHubError`를 그 상태 코드와 `exc.reply()`로 돌려준다. `/api/analyze?github=`, 최근 목록의 GitHub 항목, AI 프로젝트 id `github:…`도 서버가 맡는다 (아래 "분석, AI, 최근 목록").
- 서버는 AI 쪽(`server.ai`) 다음에 GitHub 쪽을 만들고 `start()`를 부른다. 데몬 스레드 `codevision-github-startup`이 스냅샷 정리(sweep)와 저장된 로그인의 계정 확인을 한다 (네트워크는 쓰지 않는다). `server_close`는 GitHub 쪽을 AI 쪽보다 먼저 닫는다: 새 일을 받지 않고, 받던 저장소는 멈추고(`cancelled`, code `closing`), 기다리던 로그인은 `cancelled`로 끝내고 그 스레드를 깨운다. 스레드는 기다리지(join) 않는다.
- 공개 메서드는 모두 스레드에 안전하고 `GitHubError`만 던진다 (정해진 한국어 문장, 우리 HTTP 상태). 잠금은 상태를 바꾸는 동안만 쥐고, 네트워크, 토큰 갱신, 내려받기를 기다리는 동안에는 쥐지 않는다. 그래서 `status()`는 GitHub에 묻지도, 갱신을 기다리지도 않는다.

```
GitHub 카드: 로그인 → 저장소와 브랜치 고르기, 또는 공개 저장소 주소 (POST /api/github/resolve)
  → POST /api/github/open          브랜치의 최신 커밋을 묻는다 (아는 커밋이면 If-None-Match, 같으면 304)
      → 받아 둔 커밋               200 done 블록 (cached)
      → GitHub에 닿지 않음         200 done 블록 (offline): 그 브랜치의 마지막 스냅샷
      → 새 커밋                    202 작업 블록 → GET /api/github/job (1초마다)
          작업 스레드: GET …/tarball/{sha} → 302 → codeload → extract_stream(.partial) → publish
  → GET /api/analyze?github=<owner>/<repo>&branch=<branch>&sha=<sha>   스냅샷을 오프라인으로 분석 (1장)
```

### 로그인 (기기 코드, `auth.DeviceLogin`)

- client secret은 어디에도 없다. 공개값인 Client ID `Iv23liGLsLW4iMJ8LI4g`만 보낸다.
- "GitHub로 로그인"을 누르면 화면이 `POST /api/github/login`을 보내고, 서버는 `POST https://github.com/login/device/code`(본문은 `client_id`뿐이다. scope 없음, `Accept: application/json`)로 코드를 받는다.
  - 답을 검사한다: `device_code`는 1~200자의 보이는 ASCII, `user_code`는 `[A-Z0-9][A-Z0-9-]{3,15}`, `expires_in`은 60~1800초(없으면 900), `interval`은 1~60초(없으면 5)로 맞춘다. 쓸 수 없는 답은 `login_config`다.
  - 코드를 넣을 주소는 늘 정해진 `https://github.com/login/device`다 (가짜 GitHub에서는 그 페이지). 답의 `verification_uri`는 쓰지 않는다.
  - `device_code`는 서버 메모리에만(`Secret`) 있다. 화면은 `user_code`, 코드를 넣을 주소, 남은 시간, 상태와 문장만 받는다.
- 데몬 스레드 `codevision-github-login`이 `interval`마다 `POST https://github.com/login/oauth/access_token`(`grant_type=urn:ietf:params:oauth:grant-type:device_code`)으로 묻는다. GitHub는 기다리는 동안에도 HTTP 200에 `error`를 담아 답하므로, 상태 코드가 아니라 `error`로 정한다.
  - `authorization_pending`: 계속 묻는다. `slow_down`: 답의 `interval`(1~120초 정수)을, 없으면 지금 간격 + 5초를 쓴다.
  - `expired_token`·`token_expired`(GitHub 문서가 두 이름을 쓴다)와, 이 PC에서 센 시간이 다 지난 것: `expired`. `access_denied`: `denied`. `incorrect_device_code`·`bad_verification_code`: `failed`, code `login_code`. `unverified_user_email`: `failed`, `unverified_email`. 그 밖의 오류(`device_flow_disabled`, 모르는 코드): `failed`, `login_config`. 로그에는 GitHub의 오류 코드만 남긴다.
  - GitHub에 묻지 못하면(`network`, `timeout`, `tls`, `server_error`, `bad_reply`, `rate_limited`, `interrupted`) 간격을 두 배씩(60초까지) 늘려 계속 묻고, 세 번 잇달아 실패하면 문장이 "GitHub에 잠시 연결하지 못했어요. 계속 확인하고 있어요."가 된다.
  - 토큰이 오면 `finishing`: 토큰을 검사해 저장하고(`TokenManager.accept`), `GET /user`로 로그인 이름과 사용자 id를 받아 더한다 (실패하면 이름 없이 두고 저장소 목록을 볼 때 다시 묻는다). 계정 바꾸기 확인(아래)도 이때 한다. 그다음 `done`이다. `finishing` 동안 다시 로그인을 시작하면 새 코드를 받지 않고 그 흐름을 돌려준다 (그 코드를 허락하면 방금 저장한 토큰을 바꿔 버린다).
- 흐름 블록: `{"state", "user_code", "verification_uri", "expires_in", "message", "code"}`. `state`는 `waiting`·`finishing`·`done`·`denied`·`expired`·`failed`·`cancelled`다. `user_code`·`verification_uri`·`expires_in`(남은 초)은 `waiting`일 때만 있고, `code`는 `failed`일 때만 `login_code`·`login_config`·`unverified_email` 가운데 하나다(아니면 `""`). 끝난 흐름은 5분 동안 상태 블록에 남고, `done`은 로그아웃하면 바로 빠진다.
- 로그인 흐름은 서버에 하나다. 기다리는 흐름이 있으면 `login`은 그 흐름을 돌려준다 (새로 고친 화면이 다시 붙는다). 끝난 뒤에 다시 하면 새 코드를 받는다. 이미 로그인해 있으면 409 `already_logged_in`이다 (먼저 로그아웃한다). `login/cancel`은 기다리는 흐름을 `cancelled`로 끝내고 `device_code`를 버린다.

### 토큰과 갱신 (`auth.TokenManager`)

- 접근 토큰은 8시간, 갱신 토큰은 약 6개월 간다 (GitHub가 주는 `expires_in`, `refresh_token_expires_in`). 토큰은 모양을 가정하지 않는 글자로 다룬다 (어떤 접두사든, 보이는 ASCII 20~2,000자. GitHub가 토큰 모양을 바꾸고 있다).
- 남은 시간이 5분보다 적으면 쓰기 전에 갱신한다: `POST https://github.com/login/oauth/access_token`(`grant_type=refresh_token`, client secret 없음). 갱신은 두 토큰을 모두 바꾸므로(옛 쌍은 더 쓰지 못한다) 새 쌍을 쓰기 전에 먼저 저장한다. 한 프로세스에서 갱신은 한 번에 하나이고, 다른 스레드는 그 결과를 기다린다(45초까지).
- `api.github.com`이 401로 답하면, 다른 스레드가 이미 받은 토큰, 다른 CodeVision이 저장소에 넣은 새 토큰, 갱신 순으로 새 토큰을 구해서 한 번만 다시 보낸다. 새 토큰에도 401이면 로그인을 지운다. 되풀이하지 않는다.
- GitHub가 갱신을 거절하면(네트워크류가 아닌 모든 답) 1.5초 기다린 뒤 저장소를 다시 읽는다. 다른 CodeVision이 그사이 저장한 새 쌍이 있으면 그것을 쓰고, 없으면 저장소가 아직 옛 쌍일 때만 지우고 로그아웃한다. 화면에는 `login_needed`와 "GitHub 로그인이 풀렸어요. 다시 로그인해 주세요."가 가고, 로그에는 `refresh_rejected (<GitHub 오류 코드>)`가 남는다. GitHub에 묻지 못한 갱신은 토큰을 지키고 그 오류를 그대로 돌려준다.
- 저장된 로그인이 다른 앱의 것(Client ID가 다름)이거나 갱신 토큰까지 만료되었으면 읽을 때 지우고, 상태 문장은 "로그인한 지 오래되어서 다시 로그인해야 해요."다.
- `CODEVISION_GITHUB_REFRESH_SOON=1`(사용자의 실제 GitHub 확인용): 저장한 지 60초가 지난 토큰을 곧 만료될 것으로 친다. 로그인하고 1분 넘게 기다린 뒤 무엇이든 열면 갱신이 일어난다 (요청마다 갱신하지는 않는다).

### 로그인 정보 저장 (`store.TokenStore`)

- Windows: 자격 증명 관리자의 일반(generic) 자격 증명 하나다. `win32ctypes.pywin32.win32cred`(keyring이 함께 설치하는 pywin32-ctypes)의 `CredRead`, `CredWrite`, `CredDelete`를 직접 부른다: TargetName `CodeVision/github`, UserName `oauth-tokens`, Persist `CRED_PERSIST_LOCAL_MACHINE`(도메인 프로필과 함께 다른 PC로 옮겨 가지 않는다). `CredWrite` 한 번이 그 자리에서 덮어쓰므로 지우고 쓰는 사이의 빈틈이나 사본이 없고, keyring의 WinVault 백엔드가 남겼을 `oauth-tokens@CodeVision/github` 사본은 쓸 때와 지울 때 지운다. Windows에서는 keyring 자체, 그 설정 파일, `PYTHON_KEYRING_BACKEND`를 보지 않는다.
- 다른 OS: keyring의 OS 백엔드(macOS Keychain, Secret Service, libsecret, KWallet) 가운데 이 PC에서 되는 것을 직접 만든다. keyring의 자동 고르기는 쓰지 않는다 (설정에 따라 일반 파일이나 여러 백엔드를 잇는 백엔드를 고를 수 있다).
- 값은 압축한 JSON 하나다: `{"v": 1, "client_id", "access_token", "access_expires_at", "refresh_token", "refresh_expires_at", "login", "user_id", "saved_at"}`. 자격 증명은 UTF-16으로 2,560바이트까지라서, 넘으면 접근 토큰은 빼고(이번 실행 동안 메모리에 둔다) 갱신 토큰만 저장한다. 그래도 넘으면 메모리에만 둔다.
- 메모리에만 두는 때(CodeVision을 끄면 다시 로그인한다): `CODEVISION_KEYRING=memory`, 가짜 GitHub(`CODEVISION_GITHUB_FAKE`), `CODEVISION_HOME`이 있고 `CODEVISION_KEYRING=os`가 아닐 때(테스트와 연습 실행이 진짜 로그인을 읽지 않게), OS 저장소가 없거나(keyring을 아직 설치하지 않은 가상 환경) 그 호출이 실패할 때. 상태 블록의 `store`는 `memory`이고 문장은 "로그인 정보를 이 PC에 안전하게 저장할 수 없어서, CodeVision을 끄면 다시 로그인해야 해요."다 (가짜 GitHub에서는 "테스트용 가짜 GitHub에 연결되어 있어요. 실제 GitHub가 아니에요."). 일반 파일에는 쓰지 않는다. 실패해서 메모리로 바뀐 뒤에도 로그아웃은 OS 저장소의 항목을 지운다.
- 로그에는 예외 종류와 Windows 오류 번호만 남긴다.
- 메모리의 토큰은 `base.Secret`이다: repr과 str은 `Secret(***)`, 비교는 상수 시간이고, JSON으로 바꾸거나 복사·pickle하면 예외가 난다. 그래서 토큰이 응답이나 로그 인수에 섞이면 새지 않고 오류가 난다. `Authorization: Bearer` 머리글은 보내기 직전에 만든다.

### 클라이언트 (`client.GitHubClient`)

- HTTPS로 `github.com`(로그인), `api.github.com`(REST), `codeload.github.com`(압축 파일)에만 간다. 가짜 GitHub에서는 이 PC 주소의 http만 쓰고(연결한 상대가 loopback인지도 본다) 프록시를 거치지 않는다 (`base.Hosts`).
- 머리글: `Accept: application/vnd.github+json`, `User-Agent: CodeVision/0.1.0`, `X-GitHub-Api-Version: 2026-03-10`. 로그인 요청은 form 본문과 `Accept: application/json`이다.
- TLS: `ssl.create_default_context()`(TLS 1.2 이상, Windows 인증서 저장소)로 늘 확인하고, `SSLKEYLOGFILE`이 있어도 키를 남기지 않는다. 확인을 끄는 길은 없다.
- 프록시: Ollama 클라이언트와 달리 환경 변수와 Windows의 프록시 설정을 따른다 (urllib `ProxyHandler`). HTTPS는 CONNECT라서 토큰은 TLS 안에 있다.
- 리다이렉트: 저절로 따라가지 않는다. API의 같은 호스트 301·307·308 한 번(이름이 바뀐 저장소)만 `Authorization`을 다시 붙여 따라가고, tarball의 302는 `https://codeload.github.com/…`(포트 443이나 없음, 사용자 정보 없음)일 때만 따라간다. 목록의 다음 쪽(`Link`의 `rel="next"`)도 같은 API 주소인지 보고 따라간다.
- `Authorization`은 API에만, `add_unredirected_header`로 붙인다 (Python 3.14의 리다이렉트 처리는 보통 머리글을 다른 호스트로도 옮긴다). codeload에는 붙이지 않고, 로그인해 있는데 codeload가 401·403·404로 답하면 한 번만 붙여 다시 보낸다 (비공개 저장소를 위한 길).
- 시간 한도: 소켓 동작마다 15초, API 요청 하나는 답을 다 읽기까지 45초, 로그인 요청은 30초, 목록 전체(여러 쪽)는 30초다. 내려받기는 60초 동안 아무것도 오지 않으면 멈추고(`timeout`), 한 번 받기는 30분까지다.
- 크기 한도: JSON 답 5MiB. 오류 답과 로그인 답은 64KiB까지만 읽는다.
- 목록은 `per_page=100`이고, 저장소 1,000개, 브랜치 1,000개, 설치 100개까지 읽는다 (넘으면 `truncated`).
- 요청 한도: 403·429에 `x-ratelimit-remaining: 0`(또는 GitHub 문장에 rate limit)이면 기본 한도, `retry-after`(또는 secondary rate limit)면 짧은 한도다. 걸리면 그 방식(로그인 없이 `anon`, 로그인 `user`)의 요청은 풀릴 때까지(`x-ratelimit-reset`이나 `retry-after`. 길어야 1시간) GitHub에 보내지 않고 바로 429 `rate_limited`(`retry_after` 초를 더한다)다. 저절로 다시 보내는 반복은 없다. GitHub의 한도는 로그인 없이 IP 하나에 시간당 60번, 로그인하면 5,000번이다.
- 로그인 쓰기(`auth`): `none`은 토큰을 보내지 않고, `require`는 로그인이 있어야 하고(없으면 `login_needed`), `prefer`는 로그인해 있으면 보내되 403(요청 한도가 아님)이나 404면 토큰 없이 한 번 더 보낸다 (앱이 설치되지 않은 계정의 공개 저장소). 저장소 목록은 `require`, 브랜치·주소 확인·열기·내려받기는 로그인해 있으면 `prefer`, 아니면 `none`이다.
- GitHub의 답을 계약 코드로 바꾼다: 401 → `login_needed`. 403 → `X-GitHub-SSO` 머리글이 있으면 `sso_required`, GitHub 문장에 "suspended"가 있으면 `suspended`, "not accessible by integration"이면 `not_allowed`, 그 밖은 `forbidden`. 404 → `not_found`. 410, 또는 API 버전을 말하는 400 → `outdated`("CodeVision을 최신 버전으로 바꿔 주세요"). 5xx → `server_error`. 그 밖 → `bad_reply`. 연결 오류는 `network`, 시간 초과는 `timeout`, 인증서 오류는 `tls`다. 끝점마다 다른 뜻은 그 자리에서 정한다 (최신 커밋의 409 → `empty_repo`, 422 → `branch_not_found`. 로그인 요청은 `error`가 든 답을 흐름에 그대로 넘기고, 그 밖의 400·401·404·405·422 → `login_config`). 오류는 `from None`으로 던지고 정해진 한국어 문장만 담는다.
- 로그에는 동작 이름, HTTP 상태, 우리 오류 코드, 예외 종류만 쓴다. 주소(Location, Link, codeload), 머리글, 본문, 코드, 토큰은 쓰지 않는다.

### 입력, 목록, 최신 커밋 (`repos.py`)

- 공개 저장소 칸(`POST /api/github/resolve`)이 받는 것: `계정/저장소`, `github.com/계정/저장소`, `http(s)://(www.)github.com/계정/저장소/…`(저장소의 어느 페이지든. `/tree/`·`/blob/` 뒤는 브랜치 힌트이고, 다른 뒤쪽과 `.git`, 쿼리, 조각은 버린다), `git@github.com:계정/저장소(.git)`, `ssh://git@github.com/계정/저장소(.git)`. 앞뒤 빈칸과 감싼 따옴표·괄호는 지운다. `user:password@`가 든 주소와 GitHub 페이지 이름(`settings`, `orgs` 등)은 `bad_input`, 다른 호스트는 `not_github`다. 붙여 넣은 주소는 읽기만 하고, 그 주소로 요청하거나 로그에 쓰지 않는다.
  - 브랜치 힌트는 실제 브랜치 가운데 가장 긴 앞부분을 고른다 (`feature/login/src/app.py` → `feature/login`). 맞는 것이 없으면 기본 브랜치이고 note는 "주소에 적힌 브랜치를 찾지 못해서 기본 브랜치({branch})를 골랐어요."다.
- 이름 검사(`base.py`): 계정 `[A-Za-z0-9][A-Za-z0-9_-]{0,38}`(`_`는 Enterprise Managed User 이름에 있다), 저장소 `[A-Za-z0-9._-]{1,100}`(`.`, `..` 제외), 커밋 40자리 소문자 16진수. 브랜치는 git의 ref 규칙 그대로다: 1~255자, 제어 문자·빈칸·`~^:?*[\` 없음, `..`·`@{`·`//` 없음, `@` 하나만은 안 됨, `-`나 `/`로 시작하지 않고 `/`나 `.`로 끝나지 않음, 조각이 `.`로 시작하거나 `.lock`으로 끝나지 않음. 그래서 `<`, `>`, `"`, `'`, `&`가 든 브랜치 이름도 맞는 이름이다. 모든 경로에서 계정, 저장소, 브랜치, 커밋을 검사한다.
- 로그인한 사용자의 저장소 목록: `GET /user/installations`로 앱이 설치된 계정을 받고, 설치마다 `GET /user/installations/{id}/repositories`를 읽는다. 일시 중지된 설치는 건너뛰고 note에 "CodeVision GitHub 앱이 {account} 계정에서 일시 중지되어 있어서 그 계정의 저장소는 목록에 없어요."를 붙인다. 같은 저장소는 하나만 두고, 최근에 push한 것부터, 같으면 이름순이다. 설치가 하나도 없으면 `installed`가 false이고, 설치가 없거나 목록이 비었으면 note에 "조직 관리자가 허락해야 할 수 있어요."를 붙인다. 목록에서 받은 저장소 정보는 열기가 다시 묻지 않고 쓴다.
- 브랜치 목록: `GET /repos/{o}/{r}/branches`. 기본 브랜치 먼저, 그다음 이름순이고, git 규칙에 맞지 않는 이름과 커밋이 없는 항목은 뺀다.
- 저장소 정보: `GET /repos/{o}/{r}`로 GitHub의 이름(대소문자), 비공개 여부, 기본 브랜치, 크기(`size_kb`), 보관 여부를 받는다. 이름이 바뀐 저장소의 301은 따라간다. 로그인해 있는데 404이고 그 계정이 로그인한 계정도, 앱이 설치된 계정도 아니면 `other_owner`("이 저장소는 {owner} 계정(조직)의 것이라 지금 CodeVision 앱으로는 읽을 수 없어요. 조직 저장소라면 조직 관리자가 허락해야 할 수 있어요.")다. 로그인 없이 404인데 `refs.json`이 비공개라고 알면 `private_login`이다.
- 최신 커밋: `GET /repos/{o}/{r}/commits/heads/{branch}`(`Accept: application/vnd.github.sha`). 아는 커밋이 있으면 `If-None-Match: "<sha>"`를 보내고, 304면 그 커밋 그대로다 (GitHub 문서로는 로그인한 요청의 304는 요청 한도를 쓰지 않는다). 409는 `empty_repo`, 404는 `not_found`, 422는 브랜치 목록에서 그 이름을 한 번 더 찾은 뒤(ref 모양으로 닿지 않는 이름) `branch_not_found`다.
- 캐시: 저장소 목록, 설치, 저장소 정보, 브랜치 목록을 60초 동안 둔다 (500개까지). 키에 로그인한 사용자 id(또는 `anon`)가 들어가서 한 로그인의 답을 다른 로그인이 쓰지 않는다. `repos?refresh=1`은 새로 묻는다. 로그아웃과 계정 바꾸기는 캐시를 모두 지운다. 최신 커밋은 캐시하지 않는다.

### 열기 (`GitHubService.open`)

1. 계정, 저장소, 브랜치를 검사한다 (`bad_request`). 브랜치 `""`는 기본 브랜치다 (저장소 정보에서).
2. 로그인하지 않았는데 `refs.json`이나 캐시된 저장소 정보가 비공개라고 하면, GitHub에 묻지 않고 401 `private_login`이다.
3. 브랜치의 최신 커밋을 늘 묻는다 (아는 커밋은 If-None-Match). 전에 연 브랜치(`refs.json`에 있는 브랜치. 예를 들어 최근 목록에서 열 때)가 없어졌으면 기본 브랜치를 열고 note에 "전에 연 브랜치({branch})를 찾지 못해서 기본 브랜치({default})를 열었어요."를 넣는다.
4. GitHub에 묻지 못했고(`network`, `timeout`, `tls`, `server_error`, `rate_limited`) `refs.json`이 그 브랜치의 스냅샷을 알면: 200 done 블록, `offline` true, 문장 "GitHub에 연결하지 못해서 전에 받아 둔 내용(커밋 {sha7})을 보여 줘요."(요청 한도면 "GitHub 요청 한도를 다 써서 새 커밋은 확인하지 못했어요. …"). 비공개 스냅샷은 그것을 받은 로그인이어야 한다.
5. 그 커밋의 스냅샷이 있고 이 로그인이 열 수 있으면: 200 done 블록, `cached` true. 문장은 이 브랜치가 전에도 같은 커밋이었으면 "새 커밋이 없어서 받아 둔 것을 썼어요.", 아니면 "이미 받아 둔 같은 커밋을 써요."다. 저장소 정보를 알면 GitHub의 이름으로 찾는다.
6. 같은 커밋을 받고 있으면 그 작업에 다시 붙는다 (200 작업 블록). 다른 저장소를 받고 있으면 409 `busy`다. 한 서버에서 내려받기는 한 번에 하나다.
7. 받기 전에: 저장소 정보(60초 캐시나 저장소 목록에서), `size_kb`가 2,000,000을 넘으면 422 `too_big`, 비공개 저장소면 로그인과 그 사용자 id(모르면 `GET /user`), 데이터 폴더의 드라이브에 1GiB가 안 남았으면 500 `disk_full`.
8. 내려받기 작업을 만들고 202 작업 블록을 돌려준다.

- done 블록: `{"job": "", "state": "done", "owner", "repo", "branch", "sha", "private", "cached": true, "offline", "message", "note"}`. `owner`, `repo`는 GitHub의 이름이고, 화면은 이 이름과 `sha`로 분석을 부른다.
- 작업 블록: `{"job", "state", "phase", "owner", "repo", "branch", "sha", "private", "read", "total", "files", "message", "code", "note"}`. `state`는 `running`·`done`·`cancelled`·`failed`, `phase`는 `download`·`finish`, `read`는 받은 압축 바이트, `total`은 `Content-Length`(모르면 0), `files`는 쓴 파일 수, `code`는 실패 코드(아니면 `""`)다. 작업 id는 `g`와 16진수 12자리이고, 끝난 작업은 새것 10개까지 기억한다. 로그아웃 뒤(계정 바꾸기 뒤에는 다른 계정의 것)의 끝난 비공개 작업은 블록에 `owner`, `repo`, `branch`, `sha`, `note`를 `""`로 보낸다 (작업 블록도 비공개 저장소의 흔적이다).

### 내려받기 작업

- 데몬 스레드 `codevision-github-download`가 `GET /repos/{o}/{r}/tarball/{sha}`를 보낸다. 커밋으로 요청하므로 그사이 push가 와도 확인한 커밋을 받는다. codeload 주소(비공개 저장소는 5분짜리 임시 자격이 든 주소)는 로그에 쓰지도, 어디에 두지도, 화면에 보내지도 않는다. `Content-Length`가 300MiB를 넘으면 읽기 전에 `too_big`이다.
- 응답을 `extract_stream`으로 임시 폴더(`.partial`)에 풀고, 끝나면 publish한다 (아래). 압축 파일의 pax 주석(git archive가 넣는 커밋 id)이 요청한 커밋과 다르면 `commit_mismatch`다 (주석이 없으면 받는다).
- 실패 코드: 크기 한도 `too_big`("저장소가 CodeVision이 받을 수 있는 크기(300MB)보다 커서 멈췄어요. …". 괄호 안은 걸린 한도다: 압축 300MB, 쓰기 500MB, 푼 tar 8GB), 파일 수 `too_many_files`, 멈춤·전체 시간 `timeout`, 중간에 끊긴 받기 `interrupted`, 깨진 압축 파일 `bad_reply`, 다른 커밋 `commit_mismatch`, 디스크 `disk_full`·`disk`, 그리고 tarball 요청 자체의 GitHub 오류 코드. 뜻밖의 예외는 `disk`이고 로그에는 예외 종류만 남긴다.
- 멈추기(`POST /api/github/job/cancel`), 로그아웃(비공개 작업), 계정 바꾸기, 서버 닫기는 작업 블록을 바로 `cancelled`로 바꾸고 `Download.abort()`로 작업 스레드에서 막혀 있는 읽기를 깨운다 (Windows에서는 소켓 핸들을 닫아야 깨어난다). 작업 스레드는 뒤에서 받기를 닫고 임시 폴더를 지운다. 로그아웃이 publish하는 사이에 오면 publish한 비공개 스냅샷을 바로 지우고, 그사이에 적힌 `refs.json`도 그 저장소의 비공개 스냅샷이 남지 않았으면 지운다 (`SnapshotStore.forget_private_refs`).

### 압축 풀기 (`extract.extract_stream`)

- `tarfile.extract`·`extractall`은 쓰지 않는다 (가상 환경의 Python 3.14.3에는 2026년 tar 필터 수정이 없고, `data` 필터는 Windows의 함정을 모른다). 압축 파일을 앞에서부터 한 번 읽고, 스스로 검사한 것만 쓴다.
- gzip은 직접 푼다 (zlib가 끝의 CRC와 크기를 본다). 그래서 중간에 끊긴 받기, 깨진 CRC, tar 끝 표시가 없는 것, 끝 뒤의 데이터를 알아챈다 (tarfile만으로는 깨진 머리에서 말없이 끝난다). tarfile은 압축 없는 `r|` 스트림만 읽는다 (3.13부터 `stream=True`).
- 맨 위 폴더(`owner-repo-sha7/`)는 떼어 내고, 다른 맨 위 폴더 아래의 항목은 건너뛴다.
- 일반 파일과 폴더만 쓴다. 링크, 하드 링크, 장치, FIFO, sparse 파일은 쓰지 않고 말없이 센다 (스캐너도 링크를 목록에 넣지 않는다). 폴더 항목은 비어 있어도 폴더로 만든다 (git archive는 submodule을 빈 폴더 항목으로 쓴다). `.gitmodules`의 `path`로 빈 submodule 폴더를 세어 경고를 붙인다.
- 경로의 조각마다 `windows_unsafe()`를 거친다: 빈 이름·`.`·`..`, 끝의 점이나 빈칸, `< > : " / \ | ? *`와 제어 문자, 장치 이름(`CON`, `PRN`, `AUX`, `NUL`, `COM0`~`COM9`, `LPT0`~`LPT9`, `COM¹²³`, `LPT¹²³`, `CONIN$`, `CONOUT$`. 확장자가 붙거나 점 앞에 빈칸이 있어도), 255자(UTF-16)가 넘는 이름, UTF-8로 쓸 수 없는 이름. `ntpath.isreserved`는 부르지 않는다 (3.14.3에서 `isreserved('a:b')`는 `a:`를 드라이브로 읽어 False다). 맞지 않으면 그 파일은 `bad_name`이다.
- 경로 길이는 임시 폴더가 아니라 마지막 스냅샷 경로로 센다 (UTF-16 단위): 긴 경로(레지스트리의 LongPathsEnabled)가 꺼져 있으면 파일 259, 폴더 247, 켜져 있으면 둘 다 1,000까지다. 넘으면 `too_long`이다.
- 파일은 `open(path, "xb")`로 만들어 다른 파일을 덮어쓰지 않는다. 대소문자만 다른 이름은 처음 것만 쓰고(`collision`), 그런 폴더는 Windows clone처럼 합친다. 8.3 짧은 이름 같은 별명도 `FileExistsError`로 걸린다.
- 스캐너의 무시 규칙(5장. `scan.py`의 상수를 그대로 불러 쓴다)을 풀 때부터 쓴다: `IGNORED_DIRS`와 `*.egg-info` 아래는 쓰지도 세지도 않고 폴더만 빈 채로 만든다 (그래서 `graph.hidden`이 clone처럼 "node_modules 숨김"을 보여 준다). `build`·`dist`·`venv` 같은 이름(`scan.PACKAGE_NAMES`)은 그 폴더의 `__init__.py`가 오면 패키지로 보고 내용을 쓴다. git archive는 폴더마다 git의 정렬 순서로 쓰므로, `__init__.py`보다 뒤에 올 항목이나 그 폴더 밖의 항목이 오면 더 기다리지 않는다. 그동안의 항목은 메모리에 두고(5,000개, 32MiB까지), 넘으면 패키지가 아닌 것으로 본다. `.git`이라는 파일은 쓰지 않고, 루트 `.gitignore`가 온 뒤의 항목에는 그 규칙(200개까지)도 쓴다 (스캐너도 다시 적용하므로 지름길일 뿐이다).
- 무엇을 쓰나 (스캐너가 나누는 대로): Git LFS 포인터(1,024바이트 미만, `version https://git-lfs.github.com/spec/` 등과 `oid sha256:` 줄)는 빈 파일, `scan.BINARY_EXTS` 확장자는 빈 파일, 앞 8KiB에 NUL 바이트가 있는 파일은 NUL 1바이트(스캐너와 `read_text`가 바이너리로 본다. 1MB가 넘는 텍스트 종류는 빼고), 나머지는 앞 1,000,001바이트까지(넘는 파일은 스캐너가 "1MB가 넘는 파일"로 목록에만 넣는다). 바이너리의 내용은 쓰지 않는다.
- 파일 하나가 실패하면: 디스크가 가득 차면(ENOSPC, WinError 112·39) 작업을 멈추고 `disk_full`, WinError 5·32·225·226(접근 거부, 사용 중, 바이러스 발견·제거)이면 그 파일만 `blocked`, 그 밖의 OSError면 그 파일만 `bad_name`이고 계속한다.
- 한도(`extract.Limits`): 압축 300MiB, 쓰기 500MiB, 파일 50,000개(무시 폴더를 뺀 뒤), tar 항목 1,000,000개, 푼 tar 8GiB, 텍스트 1,000,001바이트, 멈춤 60초, 전체 30분. 멈추기 Event와 진행 알림(0.25초마다)이 있다.
- 결과(`Result`)의 건너뛴 수(`skipped`: `lfs`, `binary`, `capped`, `submodules`, `bad_name`, `too_long`, `collision`, `blocked`, `links`, `other_top`, `ignored`)와 이름 예(`bad_name`, `too_long`, `collision`, `blocked`마다 20개까지, 출력할 수 있게 고친 저장소 안 경로)는 `snapshot.json`에 남고, 경고 문장이 된다 (아래). 예외 문장은 정해진 영어이고 이름, 경로, 주소를 담지 않는다.

### 스냅샷 배치와 정리 (`snapshot.SnapshotStore`)

```
<데이터 폴더>/repos/
  .partial/<16진수 12자리>/                 받는 중 (임시): snapshot.json을 먼저 여기에 쓴다
  .partial/<16진수 12자리>/<Repo>/          ... 압축은 여기에 푼다
  .delete/<16진수 12자리>/                  지우는 중: 먼저 여기로 옮겨서 제자리에서는 바로 사라진다
  .delete.json                              아직 지우지 못한 스냅샷 폴더 (다음에 켤 때 다시)
  <16진수 16자리>/refs.json                 owner, repo, 비공개 여부, 기본 브랜치, 브랜치 → {sha, checked_at}
  <16진수 16자리>/<커밋 12자리>/snapshot.json   owner, repo, sha, 비공개 여부, user_id, branch, 시각, 건너뛴 수
  <16진수 16자리>/<커밋 12자리>/<Repo>/      분석하는 폴더 (leaf)
```

- `<16진수 16자리>`는 sha256("owner 소문자/repo 소문자")의 앞부분이다. 계정과 저장소 이름은 JSON 파일에만 있어서 경로가 짧고, 임시 폴더와 `.delete`의 경로는 마지막 경로보다 길지 않다. CodeVision이 만드는 경로 조각은 정해진 이름, 16진수, 검사한 leaf뿐이다.
- leaf는 GitHub의 저장소 이름이다: 그래프의 `name`, 루트 이름, 폴더 프롬프트의 이름, Python 맨 위 패키지 규칙이 이 이름에서 오기 때문이다 (커밋 이름으로 하면 import가 깨진다). Windows가 쓸 수 없는 이름(`CON`, `aux.js`)이나 `snapshot.json`으로 시작하는 이름, 데이터 폴더가 깊어 너무 긴 경로면 leaf는 `repo`이고 진짜 이름은 `display`로 남는다. `repo`로도 너무 길면 받지 않는다 (`disk`).
- `snapshot.json`: `{"v": 1, "owner", "repo", "sha", "private", "user_id", "branch", "default_branch", "leaf", "display", "created", "opened", "commit", "files", "folders", "written", "read", "unpacked", "entries", "skipped", "examples"}`. `refs.json`: `{"v": 1, "owner", "repo", "private", "default_branch", "branches": {이름: {"sha", "checked_at"}}}`(브랜치 200개까지). 둘 다 임시 이름(pid와 스레드 id)에 쓴 뒤 `os.replace`로 바꿔 넣는다.
- 찾기(`find`)는 오프라인이다: 같은 40자리 커밋의 온전한 `snapshot.json`과 leaf 폴더가 있어야 한다. 계정과 저장소는 대소문자를 무시하고 견준다. 로그인 확인은 부르는 쪽이 한다 (`Snapshot.allowed(user_id)`: 공개는 누구나, 비공개는 받은 사용자 id만. id가 없는 비공개 스냅샷은 아무도).
- publish: `snapshot.json`을 임시 폴더에 쓰고, 폴더를 한 번의 `os.replace`로 제자리에 옮긴다 (같은 드라이브라서 원자적이다. 백신이 새 파일을 검사하는 동안은 30초까지 다시 해 본다). 같은 커밋이 이미 있으면 그것을 두고 우리 것을 지운다 (다른 로그인의 비공개 사본이면 우리 것으로 바꾼다). 그다음 브랜치 → 커밋을 `refs.json`에 적고, 이 저장소를 정리하고, 전체 한도를 지킨다.
- 정리: 이 저장소에서 `refs.json`의 어느 브랜치의 지금 커밋도 아닌 스냅샷을 지운다 (`refs.json`이 없으면 지우지 않는다). 전체는 2GiB(쓴 바이트)와 30개 안으로 지키고, 가장 오래 열지 않은 것부터 지운다. 둘 다 쓰는 중인 스냅샷(AI 쪽 `roots_in_use()`, 7장)과 10분 안에 열거나 만든 것은 지우지 않고, 쓰는 중인지 알 수 없으면 아무것도 지우지 않는다. 분석할 때마다 연 시각을 적는다.
- 켤 때 (서버가 켜지면 시작 스레드에서): 1시간이 지난 `.partial`(더 새것은 다른 CodeVision의 것일 수 있다), `.delete`에 남은 것, `.delete.json`의 폴더, 읽을 수 있는 `snapshot.json`이나 leaf가 없는 스냅샷 폴더, 빈 저장소 폴더를 지우고 전체 한도를 지킨다.
- 지우기는 늘 먼저 `repos/.delete/<16진수 12자리>`로 이름을 바꾸고(읽는 쪽이 반쯤 지운 것을 보지 않는다) 지운다. 이름을 바꾸지 못하면(열린 파일) `snapshot.json`부터 지우고 나머지를 그 자리에서 지우며, 남은 것은 `.delete.json`에 적어 다음에 켤 때 지운다. 읽기 전용 표시를 풀고, 사용 중인 파일은 잠깐 다시 해 본다. 링크와 정션은 따라가지 않는다.
- CodeVision 둘이 한 데이터 폴더를 써도, publish는 이름 바꾸기 한 번이고, 지우기는 `.delete`로 옮기기부터이고, 켤 때의 정리는 1시간이 안 된 `.partial`을 남긴다. `refs.json` 고치기의 잠금은 한 프로세스 안에서만이다 (알려진 한계).

### 로그아웃과 계정 바꾸기

- `POST /api/github/logout`(`delete_private`는 없으면 true)은 이 순서다.
  1. 토큰을 메모리에서 버리고 자격 증명을 지운다 (`TokenManager.forget`).
  2. 기다리는 로그인을 멈춘다.
  3. 캐시를 모두 지운다.
  4. 받고 있던 비공개 저장소를 멈춘다.
  5. 서버의 훅 `on_private_gone(project_ids, "logout")`에 비공개 스냅샷의 프로젝트 id를 알린다 (지우기 전, 이 서비스의 잠금 없이). 서버는 AI 쪽의 `forget_projects`(7장)를 부른다: 그 프로젝트를 메모리에서 빼고, 그 전체 설명을 멈추고, 기다리던 설명 요청에 404로 답한다.
  6. 비공개 스냅샷과 비공개 저장소의 `refs.json`을 지운다 (쓰는 중이어도). 읽을 수 있는 `snapshot.json`이 없는 스냅샷 폴더도 비공개일 수 있어서 지운다. 공개 스냅샷은 남는다.
  7. 서버가 최근 목록에서 비공개 저장소를 뺀다.
  - 로그아웃 전체(1~7)와 분석의 마지막 단계(비공개 스냅샷을 다시 확인하고, AI 쪽에 두고, 최근 목록에 넣기)는 서버의 잠금(`private_recent`)으로 차례를 지킨다. 그래서 겹친 분석이 로그아웃 뒤에 비공개 항목을 다시 넣지 못한다. 계정 바꾸기의 최근 목록 정리도 같은 잠금을 쓴다.
  - 답은 상태 블록에 `"removed": {"snapshots", "recent", "failed"}`와 `"message"`를 더한 것이다. 문장은 "로그아웃했어요. GitHub에 남은 허락까지 없애려면 GitHub 설정에서 CodeVision을 지워 주세요."이고, 지우지 못한 파일이 있으면 "로그아웃했어요. 지우지 못한 파일은 다음에 CodeVision을 켤 때 지울게요.", 자격 증명을 지우지 못했으면(이것이 앞선다) "로그아웃했지만 Windows 자격 증명 관리자에서 로그인 정보를 지우지 못했어요. 자격 증명 관리자에서 'CodeVision/github'를 직접 지워 주세요."다. 자격 증명 저장소가 실패해도 로그아웃 자체는 실패하지 않는다.
  - `delete_private`가 false면 스냅샷과 최근 목록 항목은 남지만, 비공개 스냅샷은 같은 사용자 id로 다시 로그인할 때까지 열리지 않는다 (화면은 늘 true를 보낸다).
  - AI 설명 캐시(`cache.sqlite`)는 그대로다. GitHub의 허락도 지우지 않는다: client secret이 있어야 하는 토큰 지우기 API와, 앱 소유자에게 알림이 가고 되돌릴 수 없는 `POST /credentials/revoke`는 쓰지 않는다. 상태 블록의 `settings_url`(https://github.com/settings/apps/authorizations)로 안내한다.
- 로그인이 저절로 풀리면(갱신 거절, 오래됨) 스냅샷과 최근 목록 항목은 남는다. 비공개 항목은 자물쇠로 보이고, 열면 `private_login`이다.
- 계정 바꾸기: 로그인의 사용자 id가 정해질 때(로그인 스레드에서 `done` 전, 나중에 이름을 받을 때, 켤 때 저장된 로그인) 다른 사용자 id의 비공개 스냅샷이 있으면 로그아웃 때처럼 한다. 캐시를 지우고, 그 사용자의 비공개 작업을 멈추고, 훅을 부르고(reason `switch`: 서버는 AI 쪽을 정리하고 최근 목록의 비공개 항목을 모두 뺀다), 그 스냅샷을 지운다.
- 비공개 스냅샷은 `snapshot.json`의 `user_id`와 같은 로그인일 때만 열린다: `GitHubService.snapshot()`, `project_from_text`의 `github:` id, `/api/analyze?github=`(분석이 끝난 뒤 한 번 더 확인한다).

### API (`/api/github/*`)

Host 검사, `X-CV-Token`(404·405보다 먼저 검사해서 경로가 드러나지 않는다), CSP와 보안 머리글, `MAX_BODY`, 405·501 규칙은 다른 `/api/` 요청과 같다. POST 본문은 JSON 객체여야 한다 (아니면 400 `{"error": "요청 내용을 읽을 수 없어요."}`). 상태를 바꾸는 요청은 모두 POST라서, 다른 웹 페이지는 토큰 없이 로그인, 로그아웃, 내려받기를 하지 못한다.

| 요청 | 응답 |
|---|---|
| `GET /api/github/status` | 상태 블록 (아래). GitHub에 묻지 않고 갱신을 기다리지 않는다. 화면은 로그인하는 동안 2초마다 부른다 |
| `POST /api/github/login` | `{}`. 기기 코드 로그인을 시작하거나 기다리는 흐름에 다시 붙는다. 200 상태 블록 (`flow` 포함) |
| `POST /api/github/login/cancel` | `{}`. 200 상태 블록 |
| `POST /api/github/logout` | `{"delete_private": bool}`(없으면 true, bool이 아니면 400). 200 상태 블록 + `removed` + `message` |
| `GET /api/github/repos[?refresh=1]` | `{"installed", "login", "repos": [{"owner", "repo", "full_name", "private", "default_branch", "pushed_at", "archived", "size_kb", "too_big"}], "truncated", "note"}`. 로그인하지 않았으면 401 `login_needed` |
| `GET /api/github/branches?owner=&repo=` | `{"owner", "repo", "default_branch", "branches": [{"name", "sha"}], "truncated"}`. 기본 브랜치 먼저 |
| `POST /api/github/resolve` | `{"text"}`(붙여 넣은 글). `{"owner", "repo", "private", "default_branch", "branch", "note", "branches", "truncated", "size_kb", "too_big"}` |
| `POST /api/github/open` | `{"owner", "repo", "branch"}`. 200 done 블록, 202 새 작업 블록, 200 이미 받고 있는 같은 커밋의 작업 블록 |
| `GET /api/github/job?id=` | 200 작업 블록 |
| `POST /api/github/job/cancel` | `{"job"}`. 200 작업 블록 (끝난 작업은 그대로) |

- 상태 블록: `{"logged_in", "login", "store": "keyring" | "memory", "note", "fake", "install_url", "settings_url", "flow", "job"}`. `flow`는 흐름 블록이나 null, `job`은 받고 있는 작업 블록이나 null이다. `install_url`은 앱 설치 페이지(https://github.com/apps/codevision-project-jun/installations/new), `settings_url`은 허락 설정 페이지이고, 가짜 GitHub에서는 그 페이지다. `note`는 로그인이 풀린 까닭, 메모리 저장, 가짜 GitHub, GitHub를 끔 가운데 하나다 (없으면 `""`).
- 오류는 `{"error": 한국어, "code", …}`이고 (`bad_request`만 `{"error"}`), 코드마다 HTTP 상태가 정해져 있다 (`github.GitHubError.STATUS`). `rate_limited`에는 `retry_after`(초), `not_allowed`·`not_installed`·`suspended`·`other_owner`에는 `install_url`을 더한다. `not_installed`는 자리만 있고 지금은 만들지 않는다 (앱이 없으면 저장소 목록의 `installed`가 false다).

| HTTP | `code` |
|---|---|
| 400 | `bad_request`, `bad_input`, `not_github` |
| 401 | `login_needed`, `private_login` |
| 403 | `forbidden`, `sso_required`, `suspended` |
| 404 | `not_found`, `branch_not_found`, `snapshot_missing`, `job_not_found`, `other_owner` |
| 409 | `busy`, `not_installed`, `not_allowed`, `already_logged_in` |
| 422 | `too_big`, `too_many_files`, `empty_repo` |
| 429 | `rate_limited` |
| 500 | `login_config`, `disk`, `disk_full` |
| 502 | `network`, `server_error`, `bad_reply`, `tls`, `interrupted`, `commit_mismatch`, `outdated` |
| 503 | `closing` |
| 504 | `timeout` |

### 분석, AI, 최근 목록 (`server.py`)

- `GET /api/analyze?github=<owner>%2F<repo>&branch=<branch>&sha=<커밋 40자리>`: GitHub에는 묻지 않는다.
  - 이름, 브랜치, 커밋이 맞지 않으면 400 `{"error": "GitHub 저장소 정보를 읽을 수 없어요. 저장소를 다시 열어 주세요."}`. 스냅샷이 없거나 이 로그인의 것이 아니면 404 `{"error": "받아 둔 저장소를 찾지 못했어요. 저장소를 다시 열어 주세요.", "code": "snapshot_missing"}`이고, 화면은 열기를 한 번 더 한다. 분석 중 예외는 500 "저장소를 분석하다가 문제가 생겼어요. 자세한 내용은 CodeVision 실행 기록에 남겼어요."이고, 로그 문장에는 경로와 이름을 넣지 않는다 (비공개 저장소일 수 있다).
  - `build_github_graph(snap, branch)`: 스냅샷의 leaf를 `build_graph`로 분석하고, `source`(4장)를 넣고, leaf 이름이 저장소 이름과 다르면(`repo`) 그래프의 `name`과 루트 노드의 `n`, `p`를 저장소 이름으로 바꾸고, 스냅샷의 경고를 분석기의 경고 앞에 넣는다.
  - 그다음은 폴더와 같다: AI 상태와 캐시된 설명(`annotate`), `ai.remember(프로젝트 id, …)`, 최근 목록, 그리고 스냅샷을 연 시각(전체 한도는 가장 오래 열지 않은 것부터 지운다).
- 스냅샷의 경고 (이 순서로, 개수는 천 단위 쉼표): "LFS로 올린 파일 {n}개는 내용 없이 이름만 보여요.", "하위 저장소(submodule) {n}개는 GitHub 압축 파일에 내용이 없어서 빈 폴더로 보여요.", "Windows에서 쓸 수 없는 이름의 파일 {n}개(a, b 등)는 받지 않았어요.", "경로가 너무 길어서 Windows에 만들 수 없는 파일 {n}개는 받지 않았어요.", "대소문자만 다른 같은 이름의 파일 {n}개(a, b 등)는 하나만 받았어요.", "보안 프로그램이 막은 파일 {n}개는 받지 않았어요.". 이름은 남의 글이라 화면이 이스케이프한다.
- AI 프로젝트 id는 `github:<owner 소문자>/<repo 소문자>@<sha>`다 (`source.project`). `project_from_text`가 오프라인으로 `GitHubService.snapshot()`에서 찾으므로 비공개 규칙이 같다 (오류는 6장). AI 쪽이 다시 분석할 때도 `build_github_graph`를 쓴다 (7장 `build_key`). 캐시 키는 git blob SHA라서, 바뀌지 않은 파일의 설명은 커밋이 바뀌어도 다시 쓴다. AI 프롬프트에는 스냅샷 경로가 들어가지 않는다 (루트 폴더는 경로 `""`와 저장소 이름이다).
- 최근 목록(`recent.json`)의 GitHub 항목: `{"kind": "github", "owner", "repo", "branch", "private", "name": repo, "path": "", "at"}`. 저장소마다 하나(대소문자 무시)이고 가장 최근의 브랜치가 남는다. 폴더 항목과 함께 10개(`MAX_RECENT`)까지다. 망가진 GitHub 항목과 모르는 종류는 읽을 때 빼고, `kind`가 없는 옛 항목은 폴더다. `path`가 `""`라서 이 기능 전의 CodeVision도 이 파일을 읽다가 깨지지 않는다 (그 화면은 빈 경로를 건너뛴다). 임시 파일 이름에는 pid와 스레드 id가 들어간다.

### 보안 규칙

- 화면은 토큰을 받지 않는다. 받는 것은 `user_code`, 로그인 이름, 저장소·브랜치 정보, 작업 블록이다. `device_code`와 토큰은 서버 메모리(`Secret`)와 OS 저장소에만 있고, 그래프, `source`, `recent.json`, `config.json`, `snapshot.json`, `refs.json`, 주소, 쿼리, 로그, 예외 문장, index.html에는 없다.
- client secret이 없다. 로그인과 갱신 모두 client secret 없이 한다.
- CSP는 `connect-src 'self'`, `img-src 'self' data:` 그대로이고(아바타는 보여 주지 않는다), `base-uri 'none'; form-action 'none'`을 더했다. GitHub 페이지를 새 탭으로 여는 것은 CSP와 상관없는 이동이고(`noopener noreferrer`), `Referrer-Policy: no-referrer`가 이 PC의 주소를 숨긴다.
- 화면이 여는 GitHub 주소는 상태 블록과 오류 답에서만 오고, `https`일 때만(가짜 GitHub에서는 이 PC의 http도) 쓴다. 코드를 넣을 주소는 서버의 상수다. app.js에는 GitHub 주소를 쓰지 않는다.
- 이름은 남의 글이다. 화면은 계정, 저장소, 브랜치, 파일 이름과 경고를 모두 `textContent`나 `esc()`로 넣는다. 서버의 문장은 GitHub에서 온 이름을 출력할 수 있는 짧은 글로 줄여 넣는다 (`messages._name`).
- 주소의 `?github=`는 최근 목록에 있는 저장소만 바로 연다. 다른 저장소는 GitHub 카드의 "이 저장소를 열까요?"에서 한 번 눌러야 한다 (다른 웹사이트가 새 탭으로 이 주소를 열어 고른 저장소를 받고 분석하게 하지 못한다. 토큰과 Host 검사로는 막을 수 없는 경우다). 어떤 주소도 로그인이나 로그아웃을 하지 않는다.
- 스냅샷 폴더 이름은 검사한 이름의 해시, 커밋, 검사한 leaf로만 만든다. 분석하는 폴더는 leaf이고, `ProjectFiles`는 전처럼 링크와 정션을 거절한다. 스냅샷 경로는 서버 밖으로 나가지 않는다 (`source.path`는 `""`).
- 데몬 스레드(`codevision-github-login`, `codevision-github-download`, `codevision-github-startup`)는 모두 예외를 받아 예외 종류와 코드만 로그에 남긴다 (stderr에 값이 든 traceback이 나가지 않는다). 예외 문장은 주소나 Location으로 만들지 않는다.
- AI에 보내는 글의 비밀 가리기(7장)는 GitHub의 긴 설치 토큰과 `?token=` 값도 가린다.
- 로그는 비공개 저장소의 이름을 쓰지 않는다. 서버의 뜻밖의 오류와 요청 줄(debug)에는 주소의 쿼리를 쓰지 않는다 (`?github=`, `owner=`, 노드 id). 비공개 스냅샷을 분석하는 동안의 분석기 실패에는 파일 이름과 예외 문장 없이 예외 종류만 쓰고(`graph.private_names`), AI 쪽의 오류는 GitHub 프로젝트의 폴더와 파일을 이름으로 쓰지 않는다.
- 다른 사람 저장소의 글(`.gitignore`, 코드 속 glob·정규식, AI에 보낼 글)은 한 번의 맞추기가 몇 분씩 걸릴 수 있는 정규식으로 다루지 않는다 (5장, 7장). Python의 `re`는 맞추는 동안 GIL을 쥐고 있어서, 그런 저장소 하나로 서버 전체가 멈췄다.
- 테스트는 인터넷, 진짜 GitHub, 진짜 자격 증명 관리자에 닿지 않고, 만들어 낸 토큰과 코드, 내려받기 주소가 어디에도 새지 않는지 본다 (9장).

### 화면 (`app.js`의 `ghCtl`, `runGh`)

- 시작 화면의 GitHub 카드는 폴더 카드 아래에 있고, 로그인해 있으면 위로 온다 (휴대폰에서 저장소 목록이 안내 바로 아래에 온다. 폴더 카드에 포커스가 있으면 옮기지 않는다). 카드는 `GET /api/github/status`가 말하는 대로 그리고, 2초나 1초마다 다시 그려도 포커스를 옮기지 않는다.
  - 로그아웃: "GitHub로 로그인", "CodeVision은 고른 저장소를 읽기만 해요. 저장소를 바꾸거나 올리지 않아요.", 공개 저장소 주소 칸(예: octocat/Hello-World).
  - 로그인 중: 코드(크게. 화면 읽기 프로그램에는 한 글자씩), 남은 시간(확인할 때마다 서버 값으로 맞추고, 1분 남으면 한 번 알린다), "코드 복사", "GitHub 열기", "취소", 피싱 경고("CodeVision 화면이 방금 보여 준 코드만 넣으세요. 메일·메시지·다른 사이트가 알려 준 코드는 넣지 마세요."), 3단계 안내("열린 GitHub 창에 코드를 넣고 'Continue'를 눌러 주세요. GitHub에 로그인하라고 나오면 먼저 로그인해 주세요.", "다음 화면에서 'Authorize CodeVision-Project-Jun'을 눌러 주세요.", "허락한 뒤 이 화면으로 돌아오면 저절로 이어져요."). 코드를 받은 그 화면이 코드를 클립보드에 복사하고, 복사가 되었을 때만 코드 넣기 페이지를 새 탭으로 한 번 연다 (`noopener,noreferrer`). 브라우저가 둘 다 막을 수 있어서 두 버튼은 늘 있다. 새로 고친 화면이 처음 본 코드는 복사하지도 열지도 않는다. 상태는 2초마다(실패하면 10초까지 늘려) 묻고, 탭으로 돌아오면(`visibilitychange`) 바로 묻는다. 끝나면 결과 문장을 보여 준다 (시간이 지났거나 코드가 틀렸으면 버튼이 "새 코드 받기"가 되고, 실패는 오류 줄이다).
  - 로그인: "GitHub 계정: 이름"과 "로그아웃"(누르면 그 자리에서 "로그아웃할까요? 이 PC에 받아 둔 비공개 저장소도 함께 지워요."를 묻는다. Esc는 질문을 닫는다), "저장소 찾기", 목록(8개씩, "{n}개 더 보기". 줄마다 자물쇠, 공개·비공개, "업데이트 3일 전", 보관됨, "너무 커요". 너무 큰 저장소는 고를 수 없고 누르면 까닭을 말한다), "목록 새로 고침", "GitHub 앱 설정 열기"(`install_url`), 접힌 "다른 공개 저장소 열기"(공개 저장소 칸). 목록 답의 `note`(일시 중지된 설치, 조직 관리자 힌트)는 카드의 안내 줄로 서버 문장 그대로 보인다. 다른 탭에서 이미 로그인해서 "GitHub로 로그인"이 409 `already_logged_in`이면 오류 줄 대신 그 문장을 알리고 상태를 다시 물어 로그인한 화면으로 바뀐다.
  - 앱이 설치되지 않음: "CodeVision GitHub 앱이 아직 설치되지 않았어요. 앱을 설치하면서 열어 볼 저장소를 고를 수 있어요.", "GitHub 웹사이트에서 CodeVision이 읽을 저장소를 고르는 단계예요. PC에 설치하는 프로그램이 아니에요. 설치 화면에서 'Only select repositories'를 고르면 CodeVision은 고른 저장소만 읽을 수 있어요.", "GitHub 앱 설치하기", "다시 불러오기". 탭으로 돌아오면 목록을 한 번 새로 묻는다. 저장소가 0개일 때도 따로 안내한다. 조직 관리자 힌트는 화면이 따로 쓰지 않고 목록 답의 `note`로 보인다.
  - 브랜치 줄: "선택한 저장소", 브랜치 고르기(기본 브랜치 먼저 "(기본)", 최근 목록의 브랜치를 미리 고른다), "열기".
  - 서버가 이미 다른 저장소를 받고 있으면 "다른 저장소를 받고 있어요: …"와 "이어서 보기", "멈추기". `?github=`로 처음 보는 저장소가 오면 "이 저장소를 열까요?"와 "열기", "취소".
- 열기(`runGh`): `POST /api/github/open`, 작업이면 `GET /api/github/job`을 1초마다(실패하면 10초까지 늘려) 묻고, done이면 `/api/analyze?github=`를 부른다. 진행 블록은 단계 문장("새 커밋이 있는지 확인하고 있어요…", "저장소를 받고 있어요…", "받은 내용을 확인하고 있어요…", "분석 중이에요…", `role="status"`), 받은 크기와 파일 수, 진행 막대(크기를 모르면 저절로 움직이는 막대이고 `aria-valuenow`가 없다. 움직임 줄이기 설정에서는 멈춘 막대), "멈추기"(`data-keep`: 다른 컨트롤이 기다리는 동안에도 누를 수 있다)다. 분석이 `snapshot_missing`이면 열기를 한 번 더 한다. 열기의 cached·offline 문장과 `note`(전에 연 브랜치가 없어져 기본 브랜치를 열었다는 문장. 열기 답의 것, "이어서 보기"로 따라간 작업이면 작업 블록의 것)는 프로젝트 화면의 경고 맨 앞에 넣고 `#cv-say`로도 알린다. 멈추면 그 받기를 시작한 "열기" 버튼으로 포커스를 돌리고 주소의 쿼리를 지운다. 멈춘 작업은 그 블록의 문장("저장소 받기를 멈췄어요.", 서버가 꺼지면 "CodeVision이 꺼지고 있어서 멈췄어요.")을 상태 줄에 쓴다. 다른 탭의 받기를 "멈추기"로 멈출 때도 같다.
- 포커스 (카드 안): 로그인 코드가 나오면 코드, 로그인이 끝나면 저장소 찾기 칸, 저장소를 고르면 브랜치 고르기, 받기가 시작되면 "멈추기", 로그아웃 뒤와 로그인이 실패했을 때와 "로그인하러 가기"는 "GitHub로 로그인"이다.
- 주소: 연 저장소는 `?github=owner%2Frepo&branch=`로 남긴다 (커밋은 넣지 않는다). 새로 고침하면 최신 커밋을 다시 묻는다. 화면도 브랜치 이름을 서버와 같은 git 규칙으로 검사해서, git이 받지 않는 브랜치(빈칸 등)가 든 주소는 열지 않고 버린다.
- api() 시간 한도: status 10초, login 30초, login/cancel 10초, logout 30초, repos 60초, branches 30초, resolve 30초, open 60초, job·job/cancel 10초. 서버의 GitHub 시간 한도가 더 짧아서 서버의 문장이 먼저 온다.
- 375px에서도 "코드 복사", "GitHub 열기", "취소"가 한 줄에 들어간다. 320px과 375px에서 긴 이름으로도 가로 스크롤이 생기지 않는다.

### 환경 변수 (테스트·개발용)

- `CODEVISION_GITHUB_FAKE`: `http://127.0.0.1:<포트>`(또는 `localhost`, `[::1]`. 뒤에 경로 없음)면 그 주소의 가짜 GitHub(`tests/fake_github.py`)를 쓰고, 로그인은 메모리에만 둔다. 값이 있는데 이 모양이 아니면(https, 오타, 빈 값) 진짜 GitHub로 가지 않고 GitHub 기능을 끈다: 모든 요청이 500 `login_config`이고, 상태 문장은 "테스트용 GitHub 주소(CODEVISION_GITHUB_FAKE)가 올바르지 않아서 GitHub 기능을 껐어요."다.
- `CODEVISION_KEYRING`: `memory`면 로그인을 메모리에만 둔다. `os`면 `CODEVISION_HOME`이 있어도 OS 저장소를 쓴다.
- `CODEVISION_GITHUB_REFRESH_SOON=1`: 저장한 지 60초가 지난 토큰을 곧 만료될 것으로 친다 (갱신 확인용).

### 알려진 한계

- 실제 GitHub 확인 (2026-11-25, 사용자가 직접): client secret 없는 갱신이 되고, 비공개 저장소도 받아진다. 다만 codeload가 첫 시도에 답했는지, `Authorization`을 붙인 재시도로 답했는지는 기록하지 않아서 모른다. 허락한 뒤 GitHub 탭이 `/login/device/authorize`에서 404 페이지를 보여 주지만, 로그인은 이미 끝나 있다 (화면 안내와 README에 적었다).
- 실제 GitHub로 아직 보지 못한 것: SSO 조직과 일시 중지된 설치의 403 답 모양(가짜 GitHub는 문서대로 `X-GitHub-SSO` 머리글과 "suspended"가 든 문장을 쓴다), 이상한 문자가 든 브랜치의 `commits/heads/{branch}`.
- 스냅샷은 clone과 조금 다를 수 있다: git archive는 `export-ignore`, `export-subst`, `eol` 속성을 따르고, LFS 파일은 포인터뿐이고, 건너뛴 이름, 빈 바이너리, 잘린 텍스트가 있다. 그래서 몇몇 파일, 줄 수, blob SHA(와 캐시된 설명)가 clone과 다를 수 있다.
- 패키지 규칙은 git archive의 정렬 순서에 기댄다. 순서가 다르거나 `__init__.py` 앞에 항목이 5,000개(32MiB)보다 많으면 `build/`·`dist/` 같은 폴더를 패키지가 아닌 것으로 본다. 대소문자 충돌은 글자마다의 대문자로 견주므로 NTFS의 표와 조금 다를 수 있다 (`open(…, "xb")`가 뒤를 받친다).
- Windows의 이름과 긴 경로는 한 PC(Windows 11, NTFS, 긴 경로 켜짐, 8.3 이름 켜짐)에서만 시험했다. 긴 경로가 꺼진 PC는 CodeVision 자신의 검사만 시험했다.
- `Download.abort()`는 http.client 안쪽(`resp.fp.raw._sock`)으로 소켓을 닫는다. 그 구조가 바뀌면 멈추기가 60초 멈춤 한도까지 기다릴 수 있다.
- 아주 잘 압축되는 파일은 tarfile 스트림 읽기에 CPU를 몇 초 쓴다.
- 로그인하지 않으면 요청 한도가 IP 하나에 시간당 60번이고, 같은 네트워크(공유기)의 다른 사람과 나눠 쓴다. 공개 저장소는 처음 열 때 3~4번(주소 확인 2번, 최신 커밋 1번, tarball 1번), 다시 열 때 1번쯤 쓴다.
- `refs.json`의 비공개 표시는 낡을 수 있다: 비공개로 받은 저장소가 공개로 바뀌어도, 로그인이 풀린 동안에는 GitHub에 묻지 않고 `private_login`이다.
- 계정 바꾸기는 최근 목록의 비공개 항목을 모두 뺀다 (항목에 사용자 id가 없다). 스냅샷이 이미 정리되어 계정 바꾸기를 알아채지 못하면, 옛 계정의 비공개 항목(이름만)은 로그아웃할 때까지 남는다 (그 스냅샷은 열리지 않는다).
- 비공개 저장소도 주소(`?github=계정%2F저장소&branch=`)와 창 제목에 이름이 들어가서, 브라우저의 방문 기록(동기화를 켰으면 그 계정에도)에 남는다. 로그아웃은 이것을 지우지 못한다 (README에 적었다). 새로 고침으로 다시 열기를 위해 남겨 둔다.
- 비공개 저장소를 열 때 로그인의 사용자 id를 모르면 `GET /user`를 먼저 묻고, 실패하면 열기가 그 오류로 끝난다.
- 계정을 바꾸는 로그인은 다른 계정의 비공개 스냅샷을 다 지울 때까지 `finishing`에 머문다 (보통 몇 ms, 잠긴 파일이 있으면 몇 초). 로그인 흐름의 `finishing` 동안 상태 블록은 잠깐 `logged_in` true에 `login` `""`이다 (화면은 `done`을 기다린다).
- 멈추기, 로그아웃, 서버 닫기는 작업을 바로 `cancelled`로 바꾸고, 작업 스레드는 뒤에서 정리한다. 그래서 옛 작업 스레드가 끝나기 전에 새 받기가 시작될 수 있다.
- 요청 한도로 막힌 `user` 방식은 로그아웃해도 풀리지 않는다. 바로 다른 계정으로 로그인하면 남은 시간(길어야 1시간)을 기다린다.
- GitHub App은 지금 이 계정에만 설치할 수 있어서 다른 계정과 조직의 비공개 저장소는 읽지 못한다 (공개 저장소는 토큰 없이 연다). 앱을 공개로 바꾸면 GitHub는 앱 하나에 시간당 코드 입력 50번까지만 받는다.
- 회사의 TLS 검사 프록시나 urllib가 쓰지 못하는 PAC·WPAD 설정에서는 `tls`나 `network` 오류가 난다. 확인을 끄지 않으므로 그런 네트워크에서는 GitHub 기능을 쓸 수 없다.
- `?github=`로 켤 때 화면은 `/api/recent`만 보므로, 스냅샷은 있지만 최근 목록에 없는 저장소도 한 번 눌러야 연다.
- 미룬 것 (2026-11-25 결정):
  - 응답 전체의 ETag 캐시. 최신 커밋 확인의 `If-None-Match`(아는 커밋)만 쓴다.
  - Git Trees API로 받기 전에 크기 재기. 지금은 `size_kb`(전체 이력의 압축 크기라서 대강의 값)와 받는 동안의 한도뿐이라, `Content-Length` 없이 오는 큰 저장소는 300MiB를 받은 뒤에야 너무 크다고 안다.
  - 머리줄의 "새 커밋 확인" 버튼. 새로 고침이나 시작 화면에서 다시 열기가 새 커밋을 묻는다.
  - CodeVision 둘의 조율 (살아 있음 표시 파일, `refs.json` 잠금 파일, 두 서버 테스트). 지금은 한쪽의 정리가 다른 쪽이 설명하는 스냅샷을 지울 수 있고, `refs.json` 고치기가 서로를 덮을 수 있다.
  - 같은 PC의 다른 Windows 사용자를 막는 실행 비밀 값 (Jupyter의 `?k=` 같은 것). 지금은 이 PC의 어떤 프로그램이든 `http://127.0.0.1:<포트>/`에서 실행 토큰을 읽을 수 있고, GitHub에 로그인해 있으면 그 서버로 저장소 목록을 보고, 받고, 분석과 AI 설명을 읽을 수 있다 (README에 적었다).
  - "받아 둔 저장소 지우기" 버튼. 정리는 저절로 하고, 손으로 지우는 법은 README에 있다.
  - 법적 차단(HTTP 451)의 따로 된 문장. 지금은 일반 문장이다.

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
  - 이름이 어느 주소로 풀리든, 연결할 주소와 연결된 상대 주소가 모두 loopback이어야 한다. 프록시 처리기를 두지 않아서 `HTTP_PROXY`가 있어도 프록시를 쓰지 않고, 리다이렉트는 따라가지 않는다. 이 규칙은 Ollama 클라이언트의 것이다. GitHub 클라이언트는 환경과 Windows의 프록시 설정을 따르고, 정해 둔 리다이렉트만 손으로 따라간다 (6-1).
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
  - 작업이 여럿이면 가장 새 작업부터 한다 (사용자가 보고 있는 것). 기다리는 작업의 문장은 "다른 프로젝트의 설명을 먼저 만들고 있어요. 끝나면 이어서 할게요."다.
  - 멈추기(`/api/ai/job/cancel`)는 지금 만드는 항목까지만 하고 멈춘다 (만드는 항목이 없으면 바로 멈춘다).
  - 저절로 멈출 때: Ollama가 꺼지거나, 모델이 없어지거나, 5개(`MAX_ERRORS_IN_A_ROW`)가 잇달아 실패하면 `failed`다. 모델을 바꾸거나 AI 설명을 끄거나 서버를 닫거나, GitHub 로그아웃으로 그 프로젝트가 닫히면(아래 `forget_projects`) `cancelled`다. 어떻게 멈추든 그때까지 만든 설명은 남는다.
- 끝난 작업은 10개까지 기억한다. 분석한 프로젝트는 3개(`MAX_PROJECTS`)까지 메모리에 두고(가장 오래 안 쓴 것부터 뺀다), 그 밖의 프로젝트에 요청이 오면 다시 분석한다. 서버가 준 `build_key(key, root, demo)`로 다시 분석하므로, GitHub 스냅샷(`github:` 키)은 `build_github_graph`를 거쳐 루트 이름과 폴더 캐시 키가 화면의 그래프와 같다. 그사이 스냅샷이 없어졌으면 404 `not_found`다.
- `roots_in_use()`: AI 설명이 아직 읽을 수 있는 폴더들이다. 메모리의 프로젝트, 기다리거나 도는 설명 요청, 도는 작업(멈춘 작업이 아직 만드는 항목 포함). GitHub 스냅샷 정리는 이 폴더를 지우지 않는다 (6-1).
- `forget_projects(keys)`: GitHub 로그아웃과 계정 바꾸기의 훅이다 (6-1). 그 프로젝트를 메모리에서 빼고, 도는 전체 설명을 바로 `cancelled`("프로젝트가 닫혀서 멈췄어요. 그때까지 만든 설명 N개는 저장해 두었어요.")로 바꾸고(만들던 항목은 작업 스레드에서 끝나지만 그 답은 버린다), 기다리거나 도는 설명 요청에 404 `not_found`("프로젝트가 닫혀서 설명을 만들지 못했어요. 프로젝트를 다시 열어 주세요.")로 답한다. 다른 프로젝트는 그대로다.

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
  - GitHub의 긴 설치 토큰(`ghs_<숫자>_<JWT>`, GitHub changelog 2026-10-02)과 URL의 `?token=`·`&token=` 값(16자 이상. codeload의 내려받기 주소 같은 것)도 가린다 (2026-11-25 추가).
  - 긴 글도 한 번씩만 읽는다 (2026-11-25 수정): `ghs_` 토큰과 JWT는 `[A-Za-z0-9_-]` 덩어리의 맨 앞에서만 시작하고(`-` 바로 뒤에 붙은 토큰은 가리지 않는다), 연결 문자열의 값은 그 안에 든 다른 키(`Password=Password=…`)에서 다시 읽지 않는다 (`_mask_connection_secrets`. 답은 전과 같다). 그 전에는 이렇게 꾸민 1MB 파일 하나에 41초(`ghs_`), 54초(JWT), 몇 분(`Password=`)이 걸렸고, 그동안 서버 전체가 멈췄다.
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
  - "처음 한 번은 AI가 준비하느라 30초쯤 걸릴 수 있어요."는 그 작업이 첫 항목을 시작하고 3초가 지나도 새 답이 없을 때만 보인다. 다른 프로젝트의 작업을 기다리는 동안에는 서버의 문장만 보인다.
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
- 경로 표시: 분석할 폴더의 경로(머리줄의 출처 줄, 시작 화면의 최근 목록, 오류 화면의 메시지 속 경로와 경로 줄)는 모두 `pathHtml()`로 그린다. `\`나 `/` 바로 뒤에서만 줄을 바꾸고, 한 조각이 줄보다 길 때만 그 안에서 끊는다 (조각마다 `display:inline-block`, 조각 사이에 `<wbr>`). 조각 앞의 공백은 조각 밖에 두어서 일반 글처럼 보이고 복사된다 (inline-block 맨 앞의 공백은 브라우저가 지운다). 조각이 따로 상자라서 페이지 안 찾기(`window.find`)는 `\`·`/`를 사이에 둔 글을 찾지 못하고 조각 하나 안의 글만 찾는다 (Ctrl+F 찾기 막대로는 확인하지 않았다). 머리줄의 GitHub `owner/repo`도 `pathHtml()`로 그려 `/` 뒤에서만 끊고, 최근 목록과 오류 화면의 "GitHub · owner/repo · branch" 줄은 보통 글이다.
- 머리줄: 출처 줄의 "로컬 폴더 · 경로"는 한 덩어리 글이라 경로가 "로컬 폴더 ·" 바로 뒤에 이어진다. GitHub 저장소는 "GitHub · owner/repo · branch"이고(가운뎃점은 CSS가 그려서 화면 읽기 프로그램이 읽지 않는다), 브랜치는 아무 데서나 끊는다. 짧은 커밋(앞 7자리)은 브랜치의 툴팁("커밋 7fd1a60")과 화면 읽기 프로그램용 글에만 있고, 비공개 저장소는 끝에 자물쇠와 화면 읽기 프로그램용 "비공개 저장소"가 붙는다. 숨긴 폴더는 앞의 두 이름만 쓰고, 그보다 많을 때만 " 등"을 붙인다 ("__pycache__ 숨김", "__pycache__, dist 등 숨김"). 숨긴 이름 전체는 툴팁으로 보인다.
- 글자 줄바꿈: 한국어 문장 칸(시작 화면 안내, 오류 메시지, 경고, 한눈에 보기, 범례, 패널의 요약·설명·주요 함수 설명, 옆 패널의 AI 줄, AI 패널의 문장 등)은 낱말 안에서 줄을 바꾸지 않는다(`word-break: keep-all`). 띄어쓰기 없이 줄보다 긴 글만 아무 데서나 끊는다. 경로와 파일 이름 칸(출처 줄, 최근 목록, 오류 화면의 경로 줄, 패널의 경로·이름·파일 칩, AI 패널의 "지금:" 줄 등)에는 이 규칙을 쓰지 않는다. 오류 메시지 속 경로(`.er-p`)는 메시지 칸의 `keep-all`을 물려받지만, 조각 사이에서 줄을 바꾸는 것은 같아서 줄보다 긴 조각 안에서만 차이가 난다.
- 화면이 바뀔 때 포커스: 프로젝트를 열면 프로젝트 제목, "다른 프로젝트"(2026-11-25에 "다른 폴더"에서 바꿈. 오류 화면의 "처음 화면으로"도 같다)를 누르면 로컬 폴더와 예제에서는 "폴더 선택…" 버튼, GitHub 저장소에서는 GitHub 카드의 제목("GitHub 저장소"), 오류 화면이면 "다시 시도"로 옮긴다. 처음 화면을 불러올 때는 옮기지 않는다. GitHub 카드 안의 포커스는 6-1 "화면".
- 시작 화면: 안내 글은 "프로젝트 폴더나 GitHub 저장소를 고르면 …"이고, 폴더 카드와 GitHub 카드(6-1)가 있다. 최근 목록의 제목은 "최근에 연 프로젝트"이고 폴더와 GitHub 저장소가 함께 있다. 저장소 줄은 저장소 이름 아래에 "GitHub · owner/repo · branch"를 쓰고, 비공개 저장소는 자물쇠와 "비공개"를 붙인다(줄바꿈으로 나뉘지 않는다. 로그인이 풀려도 남는다). 최근 목록의 버튼은 접근성 이름(`aria-label`)을 "이름 경로"(저장소는 "이름 GitHub owner/repo branch 브랜치 비공개") 한 덩어리로 받는다. 조각으로 나뉜 경로에서 이름을 만들게 두면 Chromium이 조각 사이를 띄어서 "C:\ Users\ …"처럼 만들기 때문이다.
- 오류 화면: 메시지가 연 경로를 이미 말하면(서버의 "폴더를 찾을 수 없어요: C:\…") 아래의 경로 줄은 넣지 않고, 메시지 속 경로만 경로 표시로 그린다. 경로 줄은 메시지가 경로를 말하지 않을 때만 보인다. GitHub 저장소의 오류는 경로 줄 자리에 "GitHub · owner/repo · branch"를 쓰고, `login_needed`·`private_login`이면 "로그인하러 가기", `not_installed`·`not_allowed`·`suspended`·`other_owner`면 "GitHub 앱 설정 열기"(오류 답의 `install_url`)를 더한다.
- 잘림 경고는 서버가 보낸 한 줄만 보여 준다. 그래프가 잘렸는데 경고가 없을 때만 화면이 일반 문장을 넣는다.
- `design/mockup.html`은 승인된 디자인 원본이다. 앱은 실제 프로젝트에 맞추느라 그 뒤에 다음을 더하거나 바꿨다.
  - 배치: 줄바꿈 트리, 혼합 배치, 꺾인 선, 옆 패널.
  - 패널과 글자: 천 단위 쉼표를 쓰는 개수(시안 코드는 숫자를 그대로 붙인다), 이름 아래에 들여 쓴 주요 함수 설명(시안은 이름 옆에 이어 쓴다), 설명과 겹치면 빠지는 굵은 요약 줄(시안은 늘 보인다), 한 덩어리로 이어지고 경로를 `\`·`/` 뒤에서만 끊는 출처 줄(시안은 조각마다 따로 놓인다), 숨긴 폴더가 두 개 이하면 " 등"을 빼는 숨김 표시(시안은 늘 "… 등 숨김"), 낱말을 끊지 않는 한국어 줄바꿈.
  - 시안에 없는 화면: `mockup.html`에는 시작 화면(최근 목록 포함)과 오류 화면이 없다. 그래서 그 화면의 경로 표시(최근 목록 `.rc-p`, 오류 메시지 속 `.er-p`, 경로 줄 `.er-path`)와 최근 목록 버튼의 `aria-label`도 앱에만 있다.
  - AI: 머리줄의 AI 칩과 AI 패널, 옆 패널의 AI 줄, 트리 이름 아래의 AI 요약은 시안에 없고 앱에만 있다.
  - GitHub: 시작 화면의 GitHub 카드, 머리줄의 "GitHub · owner/repo · branch", 최근 목록의 저장소 줄은 시안에 없고 앱에만 있다 (6-1).

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

테스트는 진짜 Ollama를 쓰지 않는다. `tests/conftest.py`가 `CODEVISION_OLLAMA_URL`을 아무것도 듣지 않는 포트로, `CODEVISION_OLLAMA_EXE`를 없는 파일로 정하고, 11434번 포트(진짜 Ollama) 연결과 `ollama` 프로그램 실행을 막는다. 모델의 답은 `tests/fake_ollama.py`가 만든다 (`fake_ollama` 픽스처). 설정이나 캐시를 쓰는 테스트는 `CODEVISION_HOME`을 pytest 임시 폴더로 정해서 사용자의 `config.json`과 `cache.sqlite`를 건드리지 않는다. `conftest.py`는 처음부터 `CODEVISION_HOME`을 이번 실행만의 임시 폴더로 정하고(끝나면 지운다), 테스트 중에 데이터 폴더가 진짜 폴더(`%LOCALAPPDATA%\CodeVision`, `~/.codevision`, 사용자가 정해 둔 `CODEVISION_HOME`)로 풀리면 그 자리에서 예외를 내고 테스트를 실패시킨다 (autouse `real_data_guard`). 자기 폴더를 잊은 서버 테스트가 진짜 `repos`를 정리하거나 지우지 못하게 하기 위해서다.

테스트는 진짜 GitHub, 인터넷, 진짜 Windows 자격 증명 관리자에도 닿지 않는다. `tests/conftest.py`가:

- `CODEVISION_GITHUB_FAKE`를 아무것도 듣지 않는 다른 loopback 포트로 정한다. 가짜를 잊은 테스트는 GitHub가 아니라 연결 거절을 만난다. `CODEVISION_KEYRING=memory`와 `PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring`으로 자식 프로세스의 로그인도 메모리에 두고, `CODEVISION_GITHUB_REFRESH_SOON`은 지운다.
- GitHub 이름(`github.com`, `api.github.com`, `codeload.github.com`, `githubusercontent.com`, `githubassets.com`, `github.io`와 그 아래 이름)을 찾는 `getaddrinfo`·`gethostbyname`·`gethostbyname_ex`, 그리고 이 컴퓨터가 아닌 주소(이름은 `localhost`만 된다)로의 `connect`·`connect_ex`를 바로 실패시킨다.
- `win32cred`의 `CredRead`·`CredWrite`·`CredDelete`·`CredEnumerate`를 예외를 내는 대역으로 바꾼다. 테스트가 그것에 닿으면 그 테스트는 실패한다.
- 테스트마다 새 메모리 저장소를 준다 (autouse `memory_keyring` 픽스처).

`run.bat`은 임시 폴더의 사본으로 시험한다 (`tests/test_run_bat.py`, Windows만): Python 호출을 대역 배치 파일로 바꿔서 pip도 서버도 창도 없이 cmd의 `if`, `fc`, `del`, `copy`만 돈다. 처음 설치, 바뀐 `requirements.txt`, 맞는 표시 파일, 다시 만든 가상 환경을 본다.

GitHub의 답은 `tests/fake_github.py`가 만든다 (`fake_github` 픽스처: 로그인 확인 간격 0.05초, `CODEVISION_GITHUB_FAKE`를 그 주소로). loopback 서버 하나가 `/web`(github.com의 기기 코드 로그인과 코드 넣기·앱 설치·설정의 작은 HTML 페이지), `/api`(api.github.com), `/codeload`(codeload.github.com의 tarball)를 맡는다. GitHub처럼 OAuth 오류는 HTTP 200에 `error`로 답하고, 갱신마다 두 토큰을 모두 바꾸고, 로그인 없는 비공개 저장소는 404, 이름이 바뀐 저장소는 301, tarball은 codeload로 302(비공개는 `?token=`)다. tarball 안의 항목은 git archive의 순서(깊이 우선, 폴더가 그 내용 바로 앞, 같은 폴더 안은 바이트 순서이고 폴더 이름은 `이름/`로 견준다)다. 요청 한도 머리글은 실제 GitHub(2026-11-25 확인)처럼 한 시간 동안 같은 reset을 보내고, tarball의 302는 core 한도를 쓰지 않는다. 다음 쪽 링크는 GitHub처럼 저장소를 id로 부른다 (`/repositories/<id>/branches?page=`). 기기 코드 로그인의 모든 답(`slow_down`, 거절, 시간 지남 등), 요청 한도, SSO 403, 일시 중지된 설치, 끊기거나 느린 받기, 위험한 이름과 링크가 든 tarball, LFS 포인터, submodule, 6만 개짜리 `node_modules`를 흉내 낼 수 있다. 진짜 토큰 대신 만들어 낸 토큰을 주므로, 테스트는 그 토큰과 기기 코드, 내려받기 주소가 응답, 로그(caplog), stderr(capfd), 스레드 예외 훅, 파일, AI 프롬프트 어디에도 없는지 본다.

화면 확인용으로 따로 띄울 수도 있다: `.venv\Scripts\python tests\fake_github.py --port 8810 --demo`, 그리고 CodeVision을 켤 창에서 `set CODEVISION_GITHUB_FAKE=http://127.0.0.1:8810`과 연습용 `CODEVISION_HOME`(로그인은 메모리에만 있다). `--demo`는 `octocat`의 여러 저장소(비공개, 빈 저장소, 이름이 바뀐 저장소, 위험한 이름, 큰 파일, 커밋된 `node_modules`·`.venv`·submodule, 너무 큰 저장소, 허락되지 않은 저장소, 브랜치 151개 등)와 SSO 조직, 일시 중지된 설치, 앱이 없는 다른 계정의 저장소를 더한다. 코드 넣기 페이지에서 "Continue"와 "Authorize CodeVision-Project-Jun"을 누르면 로그인된다.
