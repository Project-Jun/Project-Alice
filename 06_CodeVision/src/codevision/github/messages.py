"""Korean texts the GitHub side sends: plain 해요체, short, no jargon.

They are all here so the server and the screen say the same things; the page shows them as they arrive.
Visible text says "계정 이름/저장소 이름", not owner/repo. Names that come from GitHub (branches, file
names) go through _name() so a hostile name stays short and printable; the page still escapes everything.
"""
from __future__ import annotations

# --- errors by code ------------------------------------------------------------------------------
NETWORK = "GitHub에 연결하지 못했어요. 인터넷 연결을 확인한 뒤 다시 해 주세요."
TIMEOUT = "GitHub가 너무 오래 답하지 않아서 멈췄어요. 잠시 뒤에 다시 해 주세요."
TLS = ("GitHub와 안전하게 연결하지 못했어요. 회사나 학교 네트워크라면 보안 프로그램의 인증서 설정을 확인해 주세요. "
       "카페·호텔 와이파이라면 브라우저에서 먼저 와이파이 로그인을 해 주세요.")
RATE_SECONDARY = "GitHub에 요청을 너무 자주 보냈어요. 1분쯤 뒤에 다시 해 주세요."
SERVER_ERROR = "GitHub에 문제가 있어서 답을 받지 못했어요. 잠시 뒤에 다시 해 주세요."
BAD_REPLY = "GitHub의 답을 읽지 못했어요. 잠시 뒤에 다시 해 주세요."
INTERRUPTED = "받는 도중에 연결이 끊겼어요. 다시 해 주세요."
OUTDATED = "GitHub가 이 버전의 CodeVision 요청을 더 받지 않아요. CodeVision을 최신 버전으로 바꿔 주세요."

LOGIN_NEEDED = "먼저 GitHub로 로그인해 주세요."
LOGIN_EXPIRED = "GitHub 로그인이 풀렸어요. 다시 로그인해 주세요."
LOGIN_TOO_OLD = "로그인한 지 오래되어서 다시 로그인해야 해요."
PRIVATE_LOGIN = "비공개 저장소예요. 먼저 GitHub로 로그인해 주세요."
ALREADY_LOGGED_IN = "이미 GitHub에 로그인되어 있어요. 다른 계정으로 바꾸려면 먼저 로그아웃해 주세요."

NOT_FOUND_ANON = "저장소를 찾지 못했어요. 주소를 확인해 주세요. 비공개 저장소라면 먼저 GitHub로 로그인해 주세요."
NOT_FOUND_USER = ("저장소를 찾지 못했어요. 주소를 확인하거나, 비공개 저장소라면 GitHub 앱 설정에서 "
                  "그 저장소를 골라 주세요.")
BRANCH_NOT_FOUND = "브랜치를 찾지 못했어요: {branch}"
BRANCH_MISSING = "브랜치를 찾지 못했어요."
EMPTY_REPO = "빈 저장소라서 보여 줄 파일이 없어요."
TOO_BIG = ("저장소가 너무 커서 받지 않았어요. 이 PC에 내려받은 저장소 폴더(git clone)가 있으면 "
           "'폴더 선택…'으로 열어 주세요.")
TOO_MANY_FILES = ("파일이 너무 많은 저장소라서 멈췄어요. 이 PC에 내려받은 저장소 폴더(git clone)가 있으면 "
                  "'폴더 선택…'으로 열어 주세요.")
DISK_FULL = "저장할 공간이 모자라서 멈췄어요. 디스크 공간을 비운 뒤 다시 해 주세요."
DISK = "받은 저장소를 저장하지 못했어요. 자세한 내용은 CodeVision 실행 기록에 남겼어요."
BUSY = "다른 저장소를 받고 있어요. 끝나거나 멈춘 뒤에 다시 해 주세요."
COMMIT_MISMATCH = "받은 내용이 고른 커밋과 달라서 쓰지 않았어요. 다시 해 주세요."
FORBIDDEN = "CodeVision 앱이 이 저장소를 읽을 권한이 없어요. GitHub 앱 설정을 확인해 주세요."
NOT_ALLOWED = "CodeVision GitHub 앱이 이 저장소를 읽도록 허락되지 않았어요. GitHub 앱 설정에서 저장소를 골라 주세요."
NOT_INSTALLED = NOT_ALLOWED
ORG_HINT = "조직 관리자가 허락해야 할 수 있어요."
SSO_REQUIRED = ("이 저장소의 조직은 조직 로그인(SSO)을 따로 해야 열 수 있어요. "
                "GitHub 웹사이트에서 그 조직의 SSO 로그인을 마친 뒤 다시 해 주세요.")
SUSPENDED = ("이 저장소의 계정에서 CodeVision GitHub 앱이 일시 중지되어 있어서 읽을 수 없어요. "
             "GitHub 앱 설정에서 다시 켜 주세요. 조직 계정이라면 조직 관리자가 다시 켤 수 있어요.")
OTHER_OWNER = ("이 저장소는 {owner} 계정(조직)의 것이라 지금 CodeVision 앱으로는 읽을 수 없어요. "
               "조직 저장소라면 조직 관리자가 허락해야 할 수 있어요.")
SNAPSHOT_MISSING = "받아 둔 저장소를 찾지 못했어요. 저장소를 다시 열어 주세요."
JOB_NOT_FOUND = "이 작업을 찾지 못했어요. 저장소를 다시 열어 주세요."
BAD_PARAMS = "GitHub 저장소 정보를 읽을 수 없어요. 저장소를 다시 열어 주세요."
ANALYSIS_FAILED = "저장소를 분석하다가 문제가 생겼어요. 자세한 내용은 CodeVision 실행 기록에 남겼어요."
CLOSING = "CodeVision이 꺼지고 있어서 멈췄어요."
BAD_REQUEST = "요청 내용을 읽을 수 없어요."

# --- what the user typed (POST /api/github/resolve) -------------------------------------------------
EMPTY_INPUT = "열 저장소의 주소나 계정 이름/저장소 이름을 넣어 주세요."
BAD_INPUT = "GitHub 저장소 주소나 계정 이름/저장소 이름 모양으로 넣어 주세요. 예: octocat/Hello-World"
NOT_GITHUB = "GitHub 저장소 주소만 열 수 있어요."
BRANCH_DEFAULT = "주소에 적힌 브랜치를 찾지 못해서 기본 브랜치({branch})를 골랐어요."
BRANCH_GONE = "전에 연 브랜치({branch})를 찾지 못해서 기본 브랜치({default})를 열었어요."

# --- the login flow ------------------------------------------------------------------------------------
WAITING = "GitHub에서 허락하기를 기다리고 있어요."
POLL_TROUBLE = "GitHub에 잠시 연결하지 못했어요. 계속 확인하고 있어요."
FINISHING = "로그인을 마무리하고 있어요."
DONE = "GitHub에 로그인했어요."
EXPIRED = "코드를 넣을 시간이 지났어요. 새 코드를 받아 주세요."
DENIED = "GitHub에서 로그인을 허락하지 않았어요. 다시 하려면 'GitHub로 로그인'을 눌러 주세요."
CANCELLED = "로그인을 멈췄어요."
LOGIN_CODE = "로그인 코드가 맞지 않아요. 새 코드를 받아 주세요."
UNVERIFIED_EMAIL = "GitHub 계정의 이메일 인증을 마친 뒤 다시 로그인해 주세요."
LOGIN_CONFIG = "CodeVision의 GitHub 로그인 설정에 문제가 있어요. CodeVision을 최신 버전으로 바꾼 뒤 다시 해 주세요."
FAKE_OFF = "테스트용 GitHub 주소(CODEVISION_GITHUB_FAKE)가 올바르지 않아서 GitHub 기능을 껐어요."
PHISHING = "CodeVision 화면이 방금 보여 준 코드만 넣으세요. 메일·메시지·다른 사이트가 알려 준 코드는 넣지 마세요."

# --- download jobs ---------------------------------------------------------------------------------------
JOB_CHECKING = "새 커밋이 있는지 확인하고 있어요."
JOB_DOWNLOADING = "저장소를 받고 있어요."
JOB_FINISHING = "받은 내용을 확인하고 있어요."
JOB_DONE = "저장소를 다 받았어요."
JOB_CANCELLED = "저장소 받기를 멈췄어요."
REUSED = "새 커밋이 없어서 받아 둔 것을 썼어요."   # a reopen with the same head commit
REUSED_FIRST = "이미 받아 둔 같은 커밋을 써요."     # a first open that finds an existing snapshot
_OFFLINE = "GitHub에 연결하지 못해서 전에 받아 둔 내용(커밋 {sha7})을 보여 줘요."
_RATE_OFFLINE = "GitHub 요청 한도를 다 써서 새 커밋은 확인하지 못했어요. 전에 받아 둔 내용(커밋 {sha7})을 보여 줘요."

# --- store and account notes -------------------------------------------------------------------------------
STORE_MEMORY = "로그인 정보를 이 PC에 안전하게 저장할 수 없어서, CodeVision을 끄면 다시 로그인해야 해요."
FAKE_MODE = "테스트용 가짜 GitHub에 연결되어 있어요. 실제 GitHub가 아니에요."
LOGOUT_DONE = "로그아웃했어요. GitHub에 남은 허락까지 없애려면 GitHub 설정에서 CodeVision을 지워 주세요."
LOGOUT_PARTIAL = "로그아웃했어요. 지우지 못한 파일은 다음에 CodeVision을 켤 때 지울게요."
LOGOUT_STORE_FAILED = ("로그아웃했지만 Windows 자격 증명 관리자에서 로그인 정보를 지우지 못했어요. "
                       "자격 증명 관리자에서 'CodeVision/github'를 직접 지워 주세요.")
INSTALL_SUSPENDED = "CodeVision GitHub 앱이 {account} 계정에서 일시 중지되어 있어서 그 계정의 저장소는 목록에 없어요."


# --- builders -----------------------------------------------------------------------------------------------

def _name(text, limit: int = 60) -> str:
    """A name from GitHub, made safe to put in a sentence: printable, at most `limit` characters."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(text))
    text = text.encode("utf-8", "replace").decode("utf-8")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _minutes(seconds) -> int:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 60
    return max(1, -(-seconds // 60))


def RATE_LIMITED(minutes, anonymous: bool) -> str:  # noqa: N802 (named like the other texts)
    """The primary rate limit: wait {n} minutes; anonymous users are told that logging in helps."""
    n = max(1, int(minutes))
    if anonymous:
        return f"GitHub 요청 한도를 다 썼어요. {n}분쯤 뒤에 다시 해 주세요. 로그인하면 한도가 훨씬 넉넉해요."
    return f"GitHub 요청 한도를 다 썼어요. {n}분쯤 뒤에 다시 해 주세요."


def rate_limited(seconds, anonymous: bool, secondary: bool = False) -> str:
    """The sentence for a rate limit that lasts `seconds` more."""
    if secondary:
        n = _minutes(seconds)
        return RATE_SECONDARY if n <= 1 else f"GitHub에 요청을 너무 자주 보냈어요. {n}분쯤 뒤에 다시 해 주세요."
    return RATE_LIMITED(_minutes(seconds), anonymous)


def TOO_BIG_STREAM(limit: str) -> str:  # noqa: N802
    return (f"저장소가 CodeVision이 받을 수 있는 크기({limit})보다 커서 멈췄어요. 이 PC에 내려받은 저장소 폴더"
            "(git clone)가 있으면 '폴더 선택…'으로 열어 주세요.")


def branch_not_found(branch) -> str:
    return BRANCH_NOT_FOUND.format(branch=_name(branch, 100))


def branch_default(branch) -> str:
    return BRANCH_DEFAULT.format(branch=_name(branch, 100))


def branch_gone(branch, default) -> str:
    return BRANCH_GONE.format(branch=_name(branch, 100), default=_name(default, 100))


def other_owner(owner) -> str:
    return OTHER_OWNER.format(owner=_name(owner, 40))


def install_suspended(account) -> str:
    return INSTALL_SUSPENDED.format(account=_name(account, 40))


def OFFLINE(sha: str) -> str:  # noqa: N802
    """The offline-reuse note; sha may be the full commit or its first 7 characters."""
    return _OFFLINE.format(sha7=str(sha)[:7])


def RATE_OFFLINE(sha: str) -> str:  # noqa: N802
    return _RATE_OFFLINE.format(sha7=str(sha)[:7])


offline, rate_offline = OFFLINE, RATE_OFFLINE


# --- notes for graph["warnings"] (snapshot notes; the page shows them like the analyzer's warnings) ----------

def _examples(n: int, names) -> str:
    """'(a, b 등)': at most two names, with '등' when there are more than the ones shown."""
    shown = [_name(x) for x in list(names or [])[:2]]
    if not shown:
        return ""
    return "(" + ", ".join(shown) + (" 등" if n > len(shown) else "") + ")"


def LFS(n: int) -> str:  # noqa: N802
    return f"LFS로 올린 파일 {n:,}개는 내용 없이 이름만 보여요."


def BAD_NAMES(n: int, names=()) -> str:  # noqa: N802
    return f"Windows에서 쓸 수 없는 이름의 파일 {n:,}개{_examples(n, names)}는 받지 않았어요."


def TOO_LONG(n: int) -> str:  # noqa: N802
    return f"경로가 너무 길어서 Windows에 만들 수 없는 파일 {n:,}개는 받지 않았어요."


def COLLISIONS(n: int, names=()) -> str:  # noqa: N802
    return f"대소문자만 다른 같은 이름의 파일 {n:,}개{_examples(n, names)}는 하나만 받았어요."


def SUBMODULES(n: int) -> str:  # noqa: N802
    return f"하위 저장소(submodule) {n:,}개는 GitHub 압축 파일에 내용이 없어서 빈 폴더로 보여요."


def BLOCKED(n: int) -> str:  # noqa: N802
    return f"보안 프로그램이 막은 파일 {n:,}개는 받지 않았어요."


# --- error text by code (for codes whose sentence does not depend on more than the login) ----------------------

_BY_CODE = {
    "network": NETWORK, "timeout": TIMEOUT, "tls": TLS, "server_error": SERVER_ERROR, "bad_reply": BAD_REPLY,
    "interrupted": INTERRUPTED, "outdated": OUTDATED, "login_needed": LOGIN_NEEDED, "private_login": PRIVATE_LOGIN,
    "empty_repo": EMPTY_REPO, "too_big": TOO_BIG, "too_many_files": TOO_MANY_FILES, "disk_full": DISK_FULL,
    "disk": DISK, "busy": BUSY, "commit_mismatch": COMMIT_MISMATCH, "forbidden": FORBIDDEN,
    "not_allowed": NOT_ALLOWED, "not_installed": NOT_INSTALLED, "sso_required": SSO_REQUIRED,
    "suspended": SUSPENDED, "snapshot_missing": SNAPSHOT_MISSING, "job_not_found": JOB_NOT_FOUND,
    "closing": CLOSING, "login_config": LOGIN_CONFIG, "bad_request": BAD_REQUEST, "bad_input": BAD_INPUT,
    "not_github": NOT_GITHUB, "already_logged_in": ALREADY_LOGGED_IN, "branch_not_found": BRANCH_MISSING,
    "other_owner": NOT_FOUND_USER,
}


def for_code(code: str, logged_in: bool = False) -> str:
    """The usual sentence for an error code (not_found depends on whether someone is logged in)."""
    if code == "not_found":
        return NOT_FOUND_USER if logged_in else NOT_FOUND_ANON
    return _BY_CODE.get(code, SERVER_ERROR)
