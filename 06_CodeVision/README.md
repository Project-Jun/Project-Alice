# CodeVision

바이브 코딩으로 만든 프로젝트의 폴더와 파일을 트리 도식으로 보여 주고, 파일 사이의 연결과 역할을 쉬운 말로 알려 주는 로컬 도구예요.

## 실행하기

`run.bat`을 더블클릭하면 돼요. 처음 실행할 때는 필요한 패키지를 설치하느라 1~2분 걸려요. 브라우저가 열리면 분석할 폴더나 GitHub 저장소를 고르세요.

`requirements.txt`가 바뀌면 다음 실행 때 한 번 더 설치해요(이번에 GitHub 로그인 정보를 저장하는 `keyring`이 더해졌어요). 그때 인터넷이 안 되면 "새 기능에 필요한 프로그램을 설치하지 못했어요. 지금은 그대로 켜요."가 나오고 그대로 켜져요. 다음 실행 때 다시 설치하고, 그동안은 GitHub 로그인을 저장하지 못해서 CodeVision을 끄면 로그인이 풀려요.

명령어로 실행하려면:

```
.venv\Scripts\python -m codevision                 # 시작 화면에서 폴더 고르기
.venv\Scripts\python -m codevision "C:\경로\프로젝트"   # 바로 분석
```

끌 때는 실행 창에서 `Ctrl+C`를 누르세요.

## 지금 할 수 있는 것 (1~3단계)

- 로컬 폴더의 구조를 위에서 아래로 내려가는 원형 트리로 보여 줘요. `node_modules`, `.venv` 같은 폴더는 숨겨요. 항목이 많은 폴더는 여러 줄로 나눠 그리고, 휴대폰처럼 좁은 화면에서는 들여쓰기 목록으로 보여 줘요.
- GitHub 저장소도 열 수 있어요. GitHub에 로그인해서 내 저장소를 고르거나, 공개 저장소는 로그인 없이 주소를 넣어 열어요. 고른 브랜치를 이 PC에 받아서 로컬 폴더와 같은 트리로 보여 줘요 (아래 "GitHub 저장소 열기").
- Python, JavaScript, TypeScript 파일과 Vue·Svelte 파일(`.vue`, `.svelte` 안의 `<script>` 코드)의 import를 분석해서 "이 파일이 쓰는 파일"과 "이 파일을 쓰는 파일"을 보여 줘요. HTML 페이지가 불러오는 스크립트와 스타일도 연결로 보여 줘요.
- 실행할 때 이름을 정해서 불러오는 모듈도 이름의 앞부분이 코드에 적혀 있으면(`import.meta.glob("./pages/*.vue")`, `` import(`./locales/${lang}.json`) ``, `require("./plugins/" + name)`, `import_module(f"pkg.plugins.{name}")`) 그 이름에 맞는 파일들과 이어요. 한 곳에서 맞는 파일은 50개까지만 잇고, 넘으면 경고로 알려 줘요.
- Nuxt, SvelteKit, Next.js가 알아서 불러오는 파일(페이지, 레이아웃, Nuxt의 `components/` 같은 폴더, SvelteKit의 `+page.svelte`)은 정리 후보로 치지 않고, 앱이 처음 띄우는 화면(`app.vue`, 맨 위 `+layout.svelte`)은 시작점으로 보여 줘요.
- 화면 코드의 서버 요청(`fetch`, `axios`)과 서버 코드의 주소(FastAPI, Flask, Express, Koa, Hono, Fastify, Python `http.server`)를 이어서 HTTP 연결도 보여 줘요. `app.use("/api", router)`처럼 주소 앞부분을 붙이는 방식도 알아보고, 라우터 안에 다시 붙인 라우터(FastAPI `include_router`, Flask `register_blueprint`, Express·Koa의 `use`, Hono의 `route`, Fastify의 `register`)도 전체 주소를 계산해요. 한 라우터를 여러 주소에 붙였으면 그 주소들을 함께 보여 주고, 한 파일에 든 라우터 여러 개를 따로 붙였으면(`include_router(routes.todos_router)`, `import { todos, users } from "./routes"`) 라우터마다 제 주소만 받아요.
- 다른 파일에서 만든 요청 도우미로 보내는 요청도 서버 주소와 이어요. `fetch`를 감싼 함수(`api("/todos")`, `request("/todos", "POST")`)와 `axios.create({ baseURL: "/api" })`로 만든 클라이언트(`http.get("/todos")`)가 앞에 붙이는 주소까지 계산하고, `index.js`에서 다시 내보낸 도우미도 따라가요.
- Fastify 라우트를 파일마다 나눠 둔 플러그인(`module.exports = async function (fastify) { fastify.get(...) }`)은 fastify를 import하지 않아도 주소를 알아보고, 다른 파일의 `fastify.register(require("./routes/todos"), { prefix: "/api" })`가 붙인 주소까지 계산해요. 프로젝트가 Fastify를 쓸 때만 주소로 쳐요.
- 시작점(처음 실행되는 파일)과 정리 후보(아무도 쓰지 않는 파일)를 표시해요.
- 파일마다 주요 함수 목록과 규칙 기반 요약을 보여 줘요. 파일 맨 위에 설명 글(docstring이나 주석)이 없으면 폴더 이름, 파일 이름, 대표 클래스에 적힌 설명 같은 단서로 역할을 짐작해요.
- AI 설명을 켜면 이 컴퓨터의 로컬 AI(Ollama)가 코드 파일과 폴더마다 쉬운 한국어 설명을 써 줘요. 고른 항목을 하나씩, 또는 "전체 설명 만들기"로 한꺼번에 만들고, 만든 설명은 저장해 두었다가 다시 보여 줘요 (아래 "AI 설명 쓰기"). GitHub에서 받은 저장소도 같아요.
- 로컬 폴더는 처음 설치가 끝나면 인터넷 연결 없이 열려요. 인터넷은 GitHub 카드에서 로그인하거나, 저장소 목록을 보거나, GitHub 저장소를 열 때만 써요. 분석한 내용과 코드는 다른 곳으로 보내지 않아요.

아직 안 되는 것:

- 이름이 코드에 전혀 적혀 있지 않고 입력이나 설정에서 오는 모듈(`require(name)`, `import_module(config["module"])`)은 실행해 보기 전에는 무엇인지 알 수 없어서 잇지 않아요.
- import 없이 태그 이름만으로 쓰는 화면 부품(`unplugin-vue-components` 같은 자동 불러오기)은 연결이 보이지 않고 정리 후보로 보일 수 있어요. Nuxt의 `components/` 폴더 안 부품은 정리 후보로 치지 않지만, 연결은 보이지 않아요.
- 영어 docstring의 한국어 번역: AI 설명을 끄면 영어 그대로 보여요. 켜면 AI가 쓴 한국어 설명이 보이지만, 아직 AI 설명을 만들지 않은 파일과 AI가 비워 둔 칸(함수 설명이나 요약)은 영어 그대로예요.
- 요청 도우미 함수는 JavaScript 분석 도구(tree-sitter)가 있을 때만 알아봐요. tree-sitter는 `run.bat`이 함께 설치해요. 도우미가 또 다른 도우미를 불러서 보내는 요청(`const get = (path) => api(path)`)은 따라가지 않아요.
- Fastify의 `@fastify/autoload`처럼 폴더째 불러오는 플러그인은 폴더 이름으로 붙는 주소 앞부분이 빠진 채로 보이고, 정리 후보로 보일 수 있어요.
- fastify를 import하지 않는 플러그인 파일에서 첫 매개변수 이름이 `fastify`, `instance`, `app` 같은 이름이 아니고(`async (f) => { … }`) 처리 함수를 이름으로만 넘긴 라우트(`f.get("/items", getItems)`)만 있으면 주소로 알아보지 못해요. 처리 함수를 그 자리에 쓴 라우트(`f.get("/items", async () => …)`)가 하나라도 있으면 알아봐요.
- JavaScript 서버에서 함수가 만들어 돌려주는 라우터(`app.use("/api", makeRouter())`), 주소 목록이나 정규식으로 붙인 라우터, 변수로 된 미들웨어 뒤에 주소 없이 붙인 라우터(`api.use(auth, sub)`): 붙인 주소 앞부분이 빠진 채로 보여요.

## GitHub 저장소 열기

GitHub 저장소의 한 브랜치를 이 PC에 받아서 로컬 폴더처럼 트리로 보여 줘요. CodeVision은 저장소를 읽기만 해요. GitHub에서 아무것도 바꾸거나 올리지 않아요.

### 처음 로그인하기

1. 시작 화면의 GitHub 카드에서 "GitHub로 로그인"을 눌러요.
2. CodeVision이 로그인 코드(예: `WDJB-MJHT`)를 보여 주고, 코드를 복사한 뒤 GitHub의 코드 넣기 페이지(github.com/login/device)를 새 탭으로 열어요. 창이 열리지 않았으면 "GitHub 열기"를, 복사가 안 되었으면 "코드 복사"를 눌러요. 코드 아래에 남은 시간(보통 15분)이 보여요.
3. GitHub 페이지에 코드를 넣고 **Continue**를 눌러요. GitHub에 로그인하라고 나오면 먼저 로그인해요.
4. 다음 화면에서 **Authorize CodeVision-Project-Jun**을 눌러요.
5. CodeVision 탭으로 돌아오면 저절로 이어져요. 허락한 뒤 GitHub 탭에 '404' 페이지가 보일 수 있는데, 로그인은 이미 끝났으니 그 탭은 닫으면 돼요.
6. CodeVision GitHub 앱을 아직 설치하지 않았으면 "GitHub 앱 설치하기"가 보여요. GitHub 웹사이트에서 CodeVision이 읽을 저장소를 고르는 단계예요. PC에 설치하는 프로그램이 아니에요. 설치 화면에서 **Only select repositories**를 고르고 CodeVision으로 볼 저장소만 고른 뒤 **Install**(또는 **Install & Authorize**)을 눌러요. 그다음 CodeVision으로 돌아와 "다시 불러오기"를 눌러요.

> **주의:** CodeVision 화면이 방금 보여 준 코드만 넣으세요. 메일·메시지·다른 사이트가 알려 준 코드는 넣지 마세요. 다른 사람이 보낸 코드를 넣고 허락하면, 그 사람이 내 저장소를 읽을 수 있게 돼요.

**Only select repositories**를 권하는 까닭: "All repositories"로 설치하면 CodeVision 앱이 내 계정의 모든 저장소(비공개 포함)를 읽을 수 있어요. 볼 저장소만 골라 두면, 혹시 로그인이 다른 사람 손에 넘어가도 고르지 않은 저장소는 안전해요. 저장소는 나중에도 더하거나 뺄 수 있어요. GitHub 카드의 "GitHub 앱 설정 열기"를 누르거나, GitHub의 **Settings** → **Applications** → **Installed GitHub Apps**에서 CodeVision-Project-Jun의 **Configure**를 눌러요.

### 저장소 열기

- 로그인하면 GitHub 카드가 시작 화면 맨 위로 오고, CodeVision 앱이 읽을 수 있는 저장소가 최근에 고친 것부터 보여요. "저장소 찾기"로 이름을 걸러요.
- 저장소를 누르고 브랜치를 고른 뒤(기본 브랜치에는 "(기본)"이 붙어요) "열기"를 눌러요. 처음 받는 커밋이면 받는 동안 진행 막대가 보이고, "멈추기"로 멈출 수 있어요. 한 번에 저장소 하나만 받아요.
- 다시 열면(새로 고침, 최근 목록, 시작 화면) CodeVision이 GitHub에 새 커밋이 있는지 물어요. 없으면 받아 둔 것을 그대로 쓰고("새 커밋이 없어서 받아 둔 것을 썼어요."), 있으면 새 커밋을 받아요.
- 인터넷이 안 되거나 GitHub가 답하지 않으면, 그 브랜치를 전에 받아 둔 내용을 보여 주고 어느 커밋인지 알려 줘요.
- 전에 연 브랜치가 GitHub에서 없어졌으면 기본 브랜치를 열고, 프로젝트 화면 위쪽에 그렇다고 알려 줘요.
- 연 저장소는 "최근에 연 프로젝트"에 남아요. 비공개 저장소에는 자물쇠가 붙어요.
- 프로젝트 화면 맨 위에는 "GitHub · 계정 이름/저장소 이름 · 브랜치"가 보여요. 다른 저장소나 폴더를 열려면 "다른 프로젝트"를 눌러요.

### 공개 저장소는 로그인 없이

- GitHub 카드의 "공개 저장소 주소"(로그인했으면 "다른 공개 저장소 열기")에 `octocat/Hello-World`처럼 계정 이름/저장소 이름을 넣거나, 브라우저 주소를 그대로 붙여 넣어요. 저장소의 어느 페이지 주소든 되고, `https://github.com/octocat/Hello-World/tree/main`처럼 브랜치가 든 주소면 그 브랜치를 골라 줘요. `git@github.com:octocat/Hello-World.git` 같은 SSH 주소도 돼요.
- 로그인하지 않으면 GitHub가 한 시간에 60번까지만 요청을 받아요. 같은 인터넷(공유기)을 쓰는 다른 사람과 나눠 써요. 공개 저장소를 처음 열 때 3~4번, 다시 열 때 1번쯤 써요. 한도를 다 쓰면 몇 분 뒤에 다시 하라고 알려 주고, 로그인하면 한도가 훨씬 넉넉해요(한 시간에 5,000번).

### 무엇을 받고, 어디에 두나요

- 고른 브랜치의 지금 커밋 하나를 압축 파일로 받아요. 커밋 기록과 `.git` 폴더는 받지 않아요.
- 받은 저장소는 `%LOCALAPPDATA%\CodeVision\repos` 폴더에 둬요(보통 `C:\Users\<사용자 이름>\AppData\Local\CodeVision\repos`). 폴더 이름은 알아보기 어려운 글자예요. 저장소 이름은 그 안의 `snapshot.json`에 있어요.
- 다음 파일은 내용을 받지 않고 이름만 남겨요. 트리에는 그대로 보여요.
  - 그림, 글꼴, 압축 파일 같은 바이너리 파일
  - Git LFS로 올린 파일 (GitHub 압축 파일에는 내용이 없어요)
  - 하위 저장소(submodule)는 빈 폴더로 보여요
- `node_modules`, `.venv`처럼 CodeVision이 숨기는 폴더는 안의 파일을 저장하지 않고 빈 폴더만 만들어요.
- Windows에서 쓸 수 없는 이름, 너무 긴 경로, 대소문자만 다른 이름(하나만 받아요), 보안 프로그램이 막은 파일은 받지 않고, 프로젝트 화면 위쪽에 그 수를 알려 줘요.
- 1MB가 넘는 텍스트 파일은 앞부분만 받아요. 로컬 폴더에서처럼 분석하지 않고 목록에만 보여요.
- 너무 큰 저장소는 받지 않아요. GitHub가 알려 준 크기가 약 2GB를 넘거나, 받는 압축 파일이 300MB를, 저장할 내용이 500MB를, 파일이 50,000개를 넘으면 멈춰요. 60초 동안 아무것도 오지 않거나 30분이 지나도 멈춰요. 그런 저장소는 이 PC에 clone한 폴더가 있으면 "폴더 선택…"으로 열면 돼요.
- 받아 둔 저장소는 모두 합쳐 2GB, 30개까지 두고, 넘으면 가장 오래 열지 않은 것부터 저절로 지워요(지금 보고 있는 것과 10분 안에 연 것은 남겨요). 디스크에 1GB보다 적게 남았으면 받지 않아요.
- 받은 저장소는 clone과 조금 다를 수 있어요(위에서 이름만 남긴 파일, GitHub가 압축 파일에서 빼는 파일 등).

### 로그아웃

- GitHub 카드의 "로그아웃"을 누르고, "로그아웃할까요? 이 PC에 받아 둔 비공개 저장소도 함께 지워요."에서 "로그아웃"을 눌러요.
- 로그아웃하면 이 PC의 로그인 정보(Windows 자격 증명 관리자의 `CodeVision/github`), 받아 둔 비공개 저장소, 최근 목록의 비공개 저장소를 지워요. 공개 저장소와 이미 만든 AI 설명(비공개 저장소의 설명도)은 남아요.
- GitHub 쪽의 허락은 그대로 남아요. 완전히 끊으려면 로그아웃 뒤에 보이는 "GitHub 설정 열기"를 눌러(https://github.com/settings/apps/authorizations) **Authorized GitHub Apps**에서 CodeVision-Project-Jun의 **Revoke**를 눌러요. 앱 설치까지 지우려면 **Installed GitHub Apps**에서 **Configure**를 누르고 **Uninstall**을 눌러요.
- 다른 GitHub 계정으로 바꾸려면 먼저 로그아웃해요. 다른 계정으로 로그인하면 전 계정의 비공개 저장소는 이 PC에서 지워요.
- 로그인이 저절로 풀리기도 해요(GitHub에서 허락을 지웠을 때, 또는 약 6개월 동안 쓰지 않았을 때). 그러면 GitHub 카드에 다시 로그인하라는 문장이 보이고("GitHub 로그인이 풀렸어요. 다시 로그인해 주세요." 등), 비공개 저장소는 다시 로그인해야 열려요. 최근 목록의 비공개 저장소는 자물쇠를 단 채 남아요.
- 연 저장소는 새로 고침해도 다시 열리도록 브라우저 주소(`?github=계정%2F저장소&branch=…`)와 창 제목에 이름이 들어가요. 그래서 비공개 저장소의 이름도 브라우저 방문 기록에 남고, 브라우저 동기화를 켰다면 그 계정에도 올라가요. 로그아웃은 이 기록을 지우지 못하니, 필요하면 브라우저의 방문 기록에서 `127.0.0.1` 주소를 지워 주세요.

### CodeVision의 데이터를 모두 지우려면

1. CodeVision을 꺼요(실행 창에서 `Ctrl+C`).
2. 로그인 정보: 로그아웃했다면 이미 지워졌어요. 아니면 Windows 검색에서 "자격 증명 관리자"를 열고 **Windows 자격 증명** → **일반 자격 증명**에서 `CodeVision/github`를 펼쳐 **제거**를 눌러요.
3. 데이터 폴더: 파일 탐색기 주소창에 `%LOCALAPPDATA%`를 넣고 Enter를 누른 뒤, 거기 있는 `CodeVision` 폴더를 지워요. AI 설정(`config.json`), AI 설명(`cache.sqlite`), 최근 목록(`recent.json`), 받아 둔 저장소(`repos`)가 모두 이 안에 있어요.
4. GitHub 쪽의 허락과 앱 설치는 위 "로그아웃"처럼 GitHub 설정에서 지워요.

### 알아 둘 것

- 로그인 정보는 Windows 자격 증명 관리자에 저장해요. 같은 Windows 계정으로 실행되는 다른 프로그램은 이 정보를 읽을 수 있어요(자격 증명 관리자가 원래 그래요). 저장할 수 없으면 CodeVision을 끌 때까지만 기억하고, 파일에는 쓰지 않아요.
- CodeVision이 켜져 있는 동안에는 이 PC의 다른 프로그램이나 다른 Windows 사용자(사용자 전환으로 함께 로그인해 둔 계정 등)도 `http://127.0.0.1:포트` 주소로 CodeVision 화면을 열 수 있어요. GitHub에 로그인해 있으면 그 화면으로 내 저장소 목록을 보고 저장소를 받아 볼 수도 있어요. 여러 사람이 쓰는 PC에서는 다 쓴 뒤 CodeVision을 끄거나 로그아웃해 주세요.
- 지금 CodeVision GitHub 앱은 만든 사람의 GitHub 계정에만 설치할 수 있어요. 그래서 다른 계정이나 조직(organization)의 비공개 저장소는 읽을 수 없어요. 공개 저장소는 누구의 것이든 열 수 있어요.
- 회사나 학교 네트워크의 보안 프로그램이 HTTPS를 검사하면 GitHub에 연결하지 못할 수 있어요. 카페·호텔 와이파이라면 브라우저에서 먼저 와이파이 로그인을 해 주세요.

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

1. GitHub 연동(3단계) 다듬기: 프로젝트 화면의 "새 커밋 확인" 버튼과, 받아 둔 저장소를 화면에서 지우는 버튼 더하기
2. AI 설명 다듬기: 틀리는 설명이 줄도록 프롬프트를 더 손보기 (이 로컬 앱에서 인터넷 AI 서비스에 연결하는 기능은 자리만 있어요)
3. Claude·ChatGPT 스킬(4단계, GitHub 연동 다음): 전에 계획한 실행 파일(exe) 대신, Claude나 ChatGPT에서 지금과 같은 트리 화면을 HTML 파일 하나로 받기
   - Claude에 프로젝트를 주고 "이 프로젝트 구조 보여 줘"라고 하면 그 파일을 받아요. `run.bat`이나 Ollama를 켜지 않아도 브라우저로 열면 돼요.
   - Claude Code와 claude.ai부터 만들고, ChatGPT·Codex는 마지막에 시험해 보고 붙여요.
   - 스킬로 쓰면 코드가 이 컴퓨터 밖으로 나가요. Claude나 ChatGPT가 그 회사 서버에서 코드를 읽고, claude.ai와 ChatGPT에서는 프로젝트 압축 파일도 그 회사로 올라가요. 코드를 밖으로 보내고 싶지 않으면 지금처럼 이 로컬 앱을 쓰면 돼요.

자세한 결정 사항은 [docs/decisions.md](docs/decisions.md), 모듈 구조는 [docs/architecture.md](docs/architecture.md)에 있어요.

## 개발

```
.venv\Scripts\python -m pytest -q
```

테스트는 진짜 Ollama, 진짜 GitHub, 인터넷, Windows 자격 증명 관리자를 쓰지 않아요. AI 쪽은 가짜 Ollama(`tests/fake_ollama.py`), GitHub 쪽은 가짜 GitHub(`tests/fake_github.py`)와 메모리에만 두는 로그인 저장소로 확인해요.

화면을 가짜 GitHub로 보려면 `.venv\Scripts\python tests\fake_github.py --port 8810 --demo`를 켜 두고, 다른 명령 창에서 `set CODEVISION_GITHUB_FAKE=http://127.0.0.1:8810`과 `set CODEVISION_HOME=%TEMP%\codevision-try`를 한 뒤 `run.bat`을 실행해요. 로그인은 메모리에만 있고, 진짜 GitHub에는 연결하지 않아요.

| 폴더 | 내용 |
|---|---|
| `src/codevision/lang/` | 언어별 분석기 (Python, JS/TS) |
| `src/codevision/scan.py`, `graph.py`, `describe.py` | 폴더 탐색, 연결 그래프, 규칙 기반 요약 |
| `src/codevision/explain/` | AI 설명: 이 컴퓨터의 Ollama 연결, 프롬프트, 설명 저장(캐시), 비밀 정보 거르기 |
| `src/codevision/github/` | GitHub 연동: 기기 코드 로그인, 로그인 정보 저장, 저장소 목록, 저장소 받기와 안전하게 풀기, 받아 둔 저장소 관리 |
| `src/codevision/server.py` | 로컬 웹 서버 |
| `src/codevision/web/` | 화면 (아이콘 포함) |
| `src/codevision/demo/my-todo-app/` | 예제 프로젝트 |
| `design/` | 확정된 디자인 시안 (실제 앱은 옆 패널과 줄바꿈 트리가 더해졌고, 패널의 글자 배치도 조금 달라요) |
