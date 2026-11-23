"""Korean texts the AI side shows on screen: plain 해요체, short, no jargon.

They are all here so the screen and the server speak the same way.
"""

# --- status: the AI chip and the AI panel (one sentence each) ------------------------
NONE = "AI 설명을 쓰지 않게 설정되어 있어요."
CLOUD = "인터넷 AI 서비스 연결은 아직 준비 중이에요."
OFF = "AI 설명이 꺼져 있어요."
BAD_URL = "Ollama 주소는 이 컴퓨터 안의 주소(127.0.0.1, localhost)만 쓸 수 있어요."
MISSING = "이 컴퓨터에서 Ollama를 찾지 못했어요."
STOPPED = "Ollama가 꺼져 있어서 AI 설명을 만들 수 없어요."
NO_MODELS = "Ollama에 설치된 AI 모델이 없어요."
NO_ANSWER = "Ollama가 제대로 답하지 않아요. 잠시 뒤에 다시 해 주세요."
MODEL_MISSING = "고른 AI 모델({model})이 설치되어 있지 않아요."
READY = "이 컴퓨터의 AI({model})로 설명을 만들어요."
ANNOTATE_FAILED = "저장해 둔 AI 설명을 불러오다가 문제가 생겨서 기본 설명을 보여 줘요."

# --- POST /api/ai/explain and explain-all errors ---------------------------------------
EXPLAIN_OFF = "AI 설명이 꺼져 있어요. 먼저 AI 설명을 켜 주세요."
NOT_RUNNING = "Ollama가 꺼져 있어서 설명을 만들 수 없어요."
NO_MODEL = "설명을 만들 AI 모델이 없어요. Ollama에 모델을 설치해 주세요."
NO_CHOSEN_MODEL = "고른 AI 모델이 설치되어 있지 않아요. 다른 모델을 골라 주세요."
BUSY = "지금 다른 설명을 여러 개 만들고 있어요. 잠시 뒤에 다시 해 주세요."
CLOSING = "CodeVision이 꺼지고 있어서 설명을 만들지 못했어요."
PROJECT_NOT_FOUND = "이 폴더를 찾을 수 없어요. 폴더를 다시 열어 주세요."
NODE_NOT_FOUND = "이 항목을 찾지 못했어요. 폴더를 다시 분석해 주세요."
FILE_UNREADABLE = "이 파일을 읽지 못했어요. 폴더를 다시 분석해 주세요."
NOT_CODE = "AI 설명은 코드 파일과 폴더에만 만들어요."
SECRET = "비밀 정보가 들어 있을 수 있는 파일이라 AI에 보내지 않아요."
TOO_BIG = "파일이 너무 커서 AI 설명을 만들지 않아요."
UNREADABLE = "이 파일을 읽을 수 없어서 AI 설명을 만들지 않아요."
TIMEOUT = "AI가 너무 오래 답하지 않아서 멈췄어요. 잠시 뒤에 다시 해 주세요."
BAD_ANSWER = "AI의 답을 읽지 못했어요. 다시 해 보거나 다른 모델을 골라 주세요."
FAILED = "AI 설명을 만들다가 문제가 생겼어요. 잠시 뒤에 다시 해 주세요."
ANALYSIS_FAILED = "폴더를 다시 분석하다가 문제가 생겼어요. 자세한 내용은 CodeVision 실행 기록에 남겼어요."

# node["xs"] in /api/analyze -> the note the side panel shows for that code file instead of asking the AI.
# The screen has the same texts (app.js XS_NOTE); tests/test_ai_contract.py keeps them the same.
MARK_NOTES = {"secret": SECRET, "too_big": TOO_BIG, "unreadable": UNREADABLE}

# --- requests ---------------------------------------------------------------------------
BAD_REQUEST = "요청 내용을 읽을 수 없어요."
BAD_MODEL = "모델 이름이 올바르지 않아요."
MODEL_NOT_INSTALLED = "설치되지 않은 모델이에요: {model}"
SETTINGS_NOT_SAVED = "AI 설정을 저장하지 못했어요."
JOB_NOT_FOUND = "이 작업을 찾지 못했어요. 전체 설명을 다시 시작해 주세요."

# --- POST /api/ai/start -------------------------------------------------------------------
START_MISSING = "이 컴퓨터에서 Ollama를 찾지 못했어요. Ollama를 설치한 뒤 다시 해 주세요."
START_FAILED = "Ollama를 켜지 못했어요."
START_TIMEOUT = "Ollama를 켰지만 20초 안에 답하지 않았어요. 잠시 뒤에 다시 해 주세요."

# --- explain-all jobs -----------------------------------------------------------------------
JOB_STARTED = "전체 설명을 만들기 시작했어요."
JOB_WORKING = "설명을 만들고 있어요."
JOB_WAITING = "다른 폴더의 설명을 먼저 만들고 있어요. 끝나면 이어서 할게요."
JOB_STOPPING = "지금 만들던 설명까지만 마치고 멈출게요."
JOB_CANCELLED = "멈췄어요. 그때까지 만든 설명 {n:,}개는 저장해 두었어요."
JOB_DONE = "전체 설명을 다 만들었어요."
JOB_DONE_ERRORS = "전체 설명을 다 만들었어요. {n:,}곳은 만들지 못해서 기본 설명을 보여 줘요."
JOB_OLLAMA_STOPPED = "Ollama가 꺼져서 멈췄어요. 그때까지 만든 설명 {n:,}개는 저장해 두었어요."
JOB_NO_MODEL = "AI 모델을 찾을 수 없어서 멈췄어요. 그때까지 만든 설명 {n:,}개는 저장해 두었어요."
JOB_TOO_MANY_ERRORS = "AI가 계속 설명을 만들지 못해서 멈췄어요. 그때까지 만든 설명 {n:,}개는 저장해 두었어요."
JOB_SETTINGS_CHANGED = "AI 설정이 바뀌어서 멈췄어요. 그때까지 만든 설명 {n:,}개는 저장해 두었어요."
JOB_CLOSED = "CodeVision이 꺼져서 멈췄어요."
