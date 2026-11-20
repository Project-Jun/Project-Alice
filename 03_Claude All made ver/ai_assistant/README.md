# 🤖 AI 개인 비서 앱

PC에서 실행되는 개인 AI 비서 데스크톱 애플리케이션입니다.

## ✨ 주요 기능

| 기능 | 설명 |
|------|------|
| 💬 AI 대화 | Claude, OpenAI GPT, Ollama(로컬) 지원 |
| 📎 파일 요약 | PDF, Word, Excel, PowerPoint, TXT 읽기/요약 |
| 📝 메모 관리 | 로컬 SQLite 기반 메모 저장/검색/편집 |
| 📅 일정 관리 | 일정 추가/조회/삭제 |
| 🎙️ 음성 입출력 | Whisper STT + Edge TTS (한국어 지원) |
| 🌐 웹 검색 | DuckDuckGo 무료 검색 연동 |
| 📧 이메일 | Gmail 읽기/작성/AI 초안 작성 |

---

## 🚀 빠른 시작

### 1단계: Python 설치
- Python **3.10 이상** 필요
- 다운로드: https://www.python.org/downloads/

### 2단계: 실행
```
run.bat 더블클릭
```
또는 직접 실행:
```bash
pip install -r requirements.txt
python main.py
```

### 3단계: API 키 설정
`config.yaml` 파일을 메모장으로 열어 API 키를 입력하세요:

```yaml
llm:
  provider: "claude"   # 사용할 AI 선택
  
  claude:
    api_key: "sk-ant-..."   # Claude API 키
  
  openai:
    api_key: "sk-..."       # OpenAI API 키
```

---

## 🔑 API 키 발급

### Claude (Anthropic)
1. https://console.anthropic.com 접속
2. API Keys → Create Key

### OpenAI
1. https://platform.openai.com 접속
2. API Keys → Create new secret key

### Ollama (로컬, 무료)
1. https://ollama.ai 에서 설치
2. 터미널에서: `ollama pull llama3`
3. config.yaml에서 `provider: "ollama"` 설정

---

## 📧 이메일 설정 (Gmail)

Gmail 앱 비밀번호 사용:
1. Google 계정 → 보안 → 2단계 인증 활성화
2. 앱 비밀번호 생성 (메일 앱 선택)
3. config.yaml에 입력:
```yaml
email:
  smtp_user: "your@gmail.com"
  smtp_password: "xxxx xxxx xxxx xxxx"  # 앱 비밀번호 16자리
```

---

## 🎙️ 음성 기능 설정

음성 기능은 별도 설치가 필요합니다:
```bash
pip install openai-whisper pyaudio edge-tts pygame
```

> **Windows에서 PyAudio 설치 오류 시:**
> ```bash
> pip install pipwin
> pipwin install pyaudio
> ```

처음 음성 입력 시 Whisper 모델이 자동으로 다운로드됩니다 (약 140MB~1.5GB).  
`config.yaml`의 `whisper_model`을 조정하세요:
- `tiny`: 빠름, 정확도 낮음
- `base`: 권장 (기본값)
- `small`: 균형
- `medium`: 정확도 높음
- `large`: 가장 정확, 느림

---

## 📁 프로젝트 구조

```
ai_assistant/
├── main.py              # 진입점
├── config.yaml          # 설정 파일 (API 키 등)
├── requirements.txt     # 패키지 목록
├── run.bat              # Windows 실행 스크립트
├── core/
│   ├── llm_manager.py   # AI 연결 (Claude/OpenAI/Ollama)
│   ├── voice.py         # 음성 입출력
│   ├── file_handler.py  # 파일 읽기/요약
│   ├── database.py      # 메모/일정 (SQLite)
│   ├── web_search.py    # 웹 검색
│   └── email_manager.py # 이메일 관리
├── ui/
│   └── main_window.py   # PyQt6 메인 UI
└── data/
    └── assistant.db     # 로컬 데이터 (자동 생성)
```

---

## 💡 사용 팁

- **Ctrl + Enter**: 메시지 전송
- **AI 엔진 전환**: 좌측 드롭다운에서 즉시 변경 가능
- **파일 요약**: 좌측 "📎 파일 읽기" → 파일 선택 → "전송"
- **메모 AI 요약**: 메모 탭 → 내용 작성 → "🤖 AI 요약" 버튼
- **이메일 초안**: 이메일 탭 → "🤖 AI로 이메일 초안 작성"
