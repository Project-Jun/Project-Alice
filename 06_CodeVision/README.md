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

## 지금 할 수 있는 것 (1단계)

- 로컬 폴더의 구조를 위에서 아래로 내려가는 원형 트리로 보여 줘요. `node_modules`, `.venv` 같은 폴더는 숨겨요. 항목이 많은 폴더는 여러 줄로 나눠 그리고, 휴대폰처럼 좁은 화면에서는 들여쓰기 목록으로 보여 줘요.
- Python, JavaScript, TypeScript 파일의 import를 분석해서 "이 파일이 쓰는 파일"과 "이 파일을 쓰는 파일"을 보여 줘요. HTML 페이지가 불러오는 스크립트와 스타일도 연결로 보여 줘요.
- 화면 코드의 서버 요청(`fetch`, `axios`)과 서버 코드의 주소(FastAPI, Flask, Express, Koa, Hono, Fastify, Python `http.server`)를 이어서 HTTP 연결도 보여 줘요. `app.use("/api", router)`처럼 주소 앞부분을 붙이는 방식도 알아보고, 라우터 안에 다시 붙인 라우터(FastAPI `include_router`, Flask `register_blueprint`)도 전체 주소를 계산해요. 한 라우터를 여러 주소에 붙였으면 그 주소들을 함께 보여 줘요.
- 시작점(처음 실행되는 파일)과 정리 후보(아무도 쓰지 않는 파일)를 표시해요.
- 파일마다 주요 함수 목록과 규칙 기반 요약을 보여 줘요. 파일 맨 위에 설명 글(docstring이나 주석)이 없으면 폴더 이름, 파일 이름, 대표 클래스에 적힌 설명 같은 단서로 역할을 짐작해요. AI 설명은 공급자를 정한 뒤 붙여요.
- 처음 설치가 끝나면 인터넷 연결 없이 동작하고, 분석한 내용을 다른 곳으로 보내지 않아요.

아직 안 되는 것:

- `.vue`·`.svelte` 파일 안의 코드 분석
- 실행할 때 이름을 계산해서 불러오는 모듈
- 영어 docstring의 한국어 번역. 지금은 영어 그대로 보여요 (AI 설명을 붙이면 해결돼요).
- 다른 파일에서 만든 요청 도우미 함수(`fetch`를 감싼 `api(url)` 같은 함수)로 보내는 서버 요청. 같은 파일 안의 도우미는 JavaScript 분석 도구(tree-sitter)가 있을 때만 알아봐요. tree-sitter는 `run.bat`이 함께 설치해요.
- 한 파일에 든 라우터 여러 개를 다른 파일에서 서로 다른 주소에 붙인 경우: 그 파일의 주소가 실제보다 많이 보일 수 있어요.
- Express 같은 JavaScript 서버에서 한 파일 안의 라우터에 다시 붙인 라우터: 바깥 라우터의 주소 앞부분이 빠진 채로 보여요 (연결은 대개 이어져요).

## 앞으로

1. AI 설명: 로컬 AI 또는 상용 LLM 중 하나를 골라 연결 (뼈대는 준비됨)
2. GitHub 연동: 내 GitHub 계정의 저장소를 바로 열기
3. 실행 파일: Python 없이 쓸 수 있는 exe

자세한 결정 사항은 [docs/decisions.md](docs/decisions.md), 모듈 구조는 [docs/architecture.md](docs/architecture.md)에 있어요.

## 개발

```
.venv\Scripts\python -m pytest -q
```

| 폴더 | 내용 |
|---|---|
| `src/codevision/lang/` | 언어별 분석기 (Python, JS/TS) |
| `src/codevision/scan.py`, `graph.py`, `describe.py` | 폴더 탐색, 연결 그래프, 규칙 기반 요약 |
| `src/codevision/explain/` | AI 설명 뼈대 (공급자 미정) |
| `src/codevision/server.py` | 로컬 웹 서버 |
| `src/codevision/web/` | 화면 (아이콘 포함) |
| `src/codevision/demo/my-todo-app/` | 예제 프로젝트 |
| `design/` | 확정된 디자인 시안 (실제 앱은 옆 패널과 줄바꿈 트리가 더해졌고, 패널의 글자 배치도 조금 달라요) |
