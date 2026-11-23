# CodeVision

바이브 코딩으로 만든 프로젝트의 폴더와 파일을 트리 도식으로 보여 주고, 파일 사이의 연결과 역할을 쉬운 말로 알려 주는 로컬 도구예요.

## 실행하기

`run.bat`을 더블클릭하면 돼요. 처음 실행할 때는 필요한 패키지를 설치하느라 1~2분 걸려요. 브라우저가 열리면 분석할 폴더를 고르세요.

명령어로 실행하려면:

```
.venv\Scripts\python -m codevision                 # 시작 화면에서 폴더 고르기
.venv\Scripts\python -m codevision "C:\경로\프로젝트"   # 바로 분석
```

끌 때는 실행 창에서 `Ctrl+C`를 누르세요.

## 지금 할 수 있는 것 (1·2단계)

- 로컬 폴더의 구조를 위에서 아래로 내려가는 원형 트리로 보여 줘요. `node_modules`, `.venv` 같은 폴더는 숨겨요. 항목이 많은 폴더는 여러 줄로 나눠 그리고, 휴대폰처럼 좁은 화면에서는 들여쓰기 목록으로 보여 줘요.
- Python, JavaScript, TypeScript 파일과 Vue·Svelte 파일(`.vue`, `.svelte` 안의 `<script>` 코드)의 import를 분석해서 "이 파일이 쓰는 파일"과 "이 파일을 쓰는 파일"을 보여 줘요. HTML 페이지가 불러오는 스크립트와 스타일도 연결로 보여 줘요.
- 실행할 때 이름을 정해서 불러오는 모듈도 이름의 앞부분이 코드에 적혀 있으면(`import.meta.glob("./pages/*.vue")`, `` import(`./locales/${lang}.json`) ``, `require("./plugins/" + name)`, `import_module(f"pkg.plugins.{name}")`) 그 이름에 맞는 파일들과 이어요. 한 곳에서 맞는 파일은 50개까지만 잇고, 넘으면 경고로 알려 줘요.
- Nuxt, SvelteKit, Next.js가 알아서 불러오는 파일(페이지, 레이아웃, Nuxt의 `components/` 같은 폴더, SvelteKit의 `+page.svelte`)은 정리 후보로 치지 않고, 앱이 처음 띄우는 화면(`app.vue`, 맨 위 `+layout.svelte`)은 시작점으로 보여 줘요.
- 화면 코드의 서버 요청(`fetch`, `axios`)과 서버 코드의 주소(FastAPI, Flask, Express, Koa, Hono, Fastify, Python `http.server`)를 이어서 HTTP 연결도 보여 줘요. `app.use("/api", router)`처럼 주소 앞부분을 붙이는 방식도 알아보고, 라우터 안에 다시 붙인 라우터(FastAPI `include_router`, Flask `register_blueprint`, Express·Koa의 `use`, Hono의 `route`, Fastify의 `register`)도 전체 주소를 계산해요. 한 라우터를 여러 주소에 붙였으면 그 주소들을 함께 보여 주고, 한 파일에 든 라우터 여러 개를 따로 붙였으면(`include_router(routes.todos_router)`, `import { todos, users } from "./routes"`) 라우터마다 제 주소만 받아요.
- 다른 파일에서 만든 요청 도우미로 보내는 요청도 서버 주소와 이어요. `fetch`를 감싼 함수(`api("/todos")`, `request("/todos", "POST")`)와 `axios.create({ baseURL: "/api" })`로 만든 클라이언트(`http.get("/todos")`)가 앞에 붙이는 주소까지 계산하고, `index.js`에서 다시 내보낸 도우미도 따라가요.
- Fastify 라우트를 파일마다 나눠 둔 플러그인(`module.exports = async function (fastify) { fastify.get(...) }`)은 fastify를 import하지 않아도 주소를 알아보고, 다른 파일의 `fastify.register(require("./routes/todos"), { prefix: "/api" })`가 붙인 주소까지 계산해요. 프로젝트가 Fastify를 쓸 때만 주소로 쳐요.
- 시작점(처음 실행되는 파일)과 정리 후보(아무도 쓰지 않는 파일)를 표시해요.
- 파일마다 주요 함수 목록과 규칙 기반 요약을 보여 줘요. 파일 맨 위에 설명 글(docstring이나 주석)이 없으면 폴더 이름, 파일 이름, 대표 클래스에 적힌 설명 같은 단서로 역할을 짐작해요.
- AI 설명을 켜면 이 컴퓨터의 로컬 AI(Ollama)가 코드 파일과 폴더마다 쉬운 한국어 설명을 써 줘요. 고른 항목을 하나씩, 또는 "전체 설명 만들기"로 한꺼번에 만들고, 만든 설명은 저장해 두었다가 다시 보여 줘요 (아래 "AI 설명 쓰기").
- 처음 설치가 끝나면 인터넷 연결 없이 동작하고, 분석한 내용을 다른 곳으로 보내지 않아요.

아직 안 되는 것:

- 이름이 코드에 전혀 적혀 있지 않고 입력이나 설정에서 오는 모듈(`require(name)`, `import_module(config["module"])`)은 실행해 보기 전에는 무엇인지 알 수 없어서 잇지 않아요.
- import 없이 태그 이름만으로 쓰는 화면 부품(`unplugin-vue-components` 같은 자동 불러오기)은 연결이 보이지 않고 정리 후보로 보일 수 있어요. Nuxt의 `components/` 폴더 안 부품은 정리 후보로 치지 않지만, 연결은 보이지 않아요.
- 영어 docstring의 한국어 번역: AI 설명을 끄면 영어 그대로 보여요. 켜면 AI가 쓴 한국어 설명이 보이지만, 아직 AI 설명을 만들지 않은 파일과 AI가 비워 둔 칸(함수 설명이나 요약)은 영어 그대로예요.
- 요청 도우미 함수는 JavaScript 분석 도구(tree-sitter)가 있을 때만 알아봐요. tree-sitter는 `run.bat`이 함께 설치해요. 도우미가 또 다른 도우미를 불러서 보내는 요청(`const get = (path) => api(path)`)은 따라가지 않아요.
- Fastify의 `@fastify/autoload`처럼 폴더째 불러오는 플러그인은 폴더 이름으로 붙는 주소 앞부분이 빠진 채로 보이고, 정리 후보로 보일 수 있어요.
- fastify를 import하지 않는 플러그인 파일에서 첫 매개변수 이름이 `fastify`, `instance`, `app` 같은 이름이 아니고(`async (f) => { … }`) 처리 함수를 이름으로만 넘긴 라우트(`f.get("/items", getItems)`)만 있으면 주소로 알아보지 못해요. 처리 함수를 그 자리에 쓴 라우트(`f.get("/items", async () => …)`)가 하나라도 있으면 알아봐요.
- JavaScript 서버에서 함수가 만들어 돌려주는 라우터(`app.use("/api", makeRouter())`), 주소 목록이나 정규식으로 붙인 라우터, 변수로 된 미들웨어 뒤에 주소 없이 붙인 라우터(`api.use(auth, sub)`): 붙인 주소 앞부분이 빠진 채로 보여요.

## AI 설명 쓰기

AI 설명은 이 컴퓨터에서 도는 로컬 AI(Ollama)로 만들어요. 처음에는 꺼져 있고, 꺼져 있어도 규칙 기반 설명은 그대로 보여요.

1. [ollama.com](https://ollama.com)에서 Ollama를 받아 설치해요.
2. Ollama를 켜요. 설치하면 보통 저절로 켜져 있어요. 꺼져 있으면 시작 메뉴에서 Ollama를 실행해요.
3. AI 모델을 받아요. 이건 직접 해야 해요(CodeVision은 모델을 받지 않아요). 명령 창(PowerShell)에서 `ollama pull qwen2.5:7b`를 실행하면 약 4.7GB를 받아요. 더 작은 `gemma3:4b`(약 3.3GB)도 쓸 수 있지만, 평가에서 `qwen2.5:7b`가 코드와 다른 말을 덜 해서 둘 다 있으면 `qwen2.5:7b`를 먼저 골라요.
4. CodeVision에서 프로젝트를 열고, 머리줄의 "AI 설명 꺼짐"을 눌러 AI 패널을 연 뒤 "AI 설명 쓰기"를 켜요. Ollama가 꺼져 있으면 패널의 "Ollama 켜기"로 켤 수 있어요(창 없이 켜져요). 모델도 패널에서 바꿀 수 있어요.

그다음에는 코드 파일이나 폴더를 누르면 AI가 그 설명을 만들어요. 옆 패널의 "기본 설명 보기"를 누르면 규칙 기반 설명을 볼 수 있어요. AI 패널의 "전체 설명 만들기"는 아직 설명이 없는 코드 파일과 폴더를 한꺼번에 만들어요. 걸릴 시간을 먼저 알려 주고, 중간에 멈출 수 있어요.

- 걸리는 시간(RTX 5060 8GB에서 qwen2.5:7b로 잰 값이에요. 컴퓨터마다 달라요): 컴퓨터를 켜고 처음 답할 때는 모델을 불러오느라 30초\~1분쯤 걸려요. 그 뒤로는 파일 하나에 1\~10초(보통 2\~5초), 폴더 하나에 1\~2.5초쯤이에요. 전체 설명은 예제 프로젝트(17개)가 약 24초, 이 저장소는 아직 설명이 없던 53개가 약 4분 걸렸어요.
- 코드는 이 컴퓨터 밖으로 나가지 않아요. `.env`나 개인 키처럼 비밀 정보가 들어 있을 수 있는 파일은 AI에 보내지 않고, 코드 속 비밀번호나 토큰처럼 보이는 값은 가려서 보내요.
- AI 설명을 끄거나 CodeVision을 끄면, CodeVision이 쓰던 AI 모델을 그래픽카드 메모리에서 내려요. CodeVision이 켠 Ollama는 CodeVision을 꺼도 켜져 있어요.
- AI 설명도 틀릴 때가 있어요. 평가에서는 답 셋 중 하나꼴로 코드와 다른 말이 섞여 있었어요(주로 작은 도우미 함수의 설명). 중요한 내용은 코드에서 한 번 더 확인해 주세요.

## 앞으로

1. AI 설명 다듬기: 틀리는 설명이 줄도록 프롬프트를 더 손보기 (인터넷 AI 서비스 연결은 자리만 있어요)
2. GitHub 연동: 내 GitHub 계정의 저장소를 바로 열기
3. 실행 파일: Python 없이 쓸 수 있는 exe

자세한 결정 사항은 [docs/decisions.md](docs/decisions.md), 모듈 구조는 [docs/architecture.md](docs/architecture.md)에 있어요.

## 개발

```
.venv\Scripts\python -m pytest -q
```

테스트는 진짜 Ollama를 쓰지 않아요. AI 쪽은 가짜 Ollama(`tests/fake_ollama.py`)로 확인해요.

| 폴더 | 내용 |
|---|---|
| `src/codevision/lang/` | 언어별 분석기 (Python, JS/TS) |
| `src/codevision/scan.py`, `graph.py`, `describe.py` | 폴더 탐색, 연결 그래프, 규칙 기반 요약 |
| `src/codevision/explain/` | AI 설명: 이 컴퓨터의 Ollama 연결, 프롬프트, 설명 저장(캐시), 비밀 정보 거르기 |
| `src/codevision/server.py` | 로컬 웹 서버 |
| `src/codevision/web/` | 화면 (아이콘 포함) |
| `src/codevision/demo/my-todo-app/` | 예제 프로젝트 |
| `design/` | 확정된 디자인 시안 (실제 앱은 옆 패널과 줄바꿈 트리가 더해졌고, 패널의 글자 배치도 조금 달라요) |
