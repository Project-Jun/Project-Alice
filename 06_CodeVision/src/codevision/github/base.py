"""Constants, the error type, the token wrapper, host selection and input validators.

Nothing here touches the network, and neither keyring nor urllib is imported at module level.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass

from codevision import __version__

# --- the GitHub App (public values: the device flow and refreshes need no client secret) ---------
CLIENT_ID = "Iv23liGLsLW4iMJ8LI4g"
APP_SLUG = "codevision-project-jun"
WEB_URL = "https://github.com"
API_URL = "https://api.github.com"
CODELOAD_URL = "https://codeload.github.com"
INSTALL_URL = f"{WEB_URL}/apps/{APP_SLUG}/installations/new"
VERIFY_URL = f"{WEB_URL}/login/device"
AUTH_SETTINGS_URL = f"{WEB_URL}/settings/apps/authorizations"
API_VERSION = "2026-03-10"
USER_AGENT = "CodeVision/" + __version__

# --- environment switches -----------------------------------------------------------------------
FAKE_ENV = "CODEVISION_GITHUB_FAKE"            # http://127.0.0.1:<port> of tests/fake_github.py
KEYRING_ENV = "CODEVISION_KEYRING"             # "memory", or "os" to use the OS store under CODEVISION_HOME
REFRESH_SOON_ENV = "CODEVISION_GITHUB_REFRESH_SOON"  # "1": an access token older than a minute counts as expiring
HOME_ENV = "CODEVISION_HOME"

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")

log = logging.getLogger(__name__)


# --- the error the GitHub side raises ------------------------------------------------------------

class GitHubError(Exception):
    """A failure the screen can show: a contract code, our HTTP status and one Korean sentence.

    The message is always a fixed text from messages.py: never a URL, a header or a reply body.
    `extra` goes into the reply (retry_after, install_url); `gh_status` is GitHub's own HTTP status,
    for callers only, and never part of the reply.
    """

    STATUS = {
        "bad_request": 400, "bad_input": 400, "not_github": 400,
        "login_needed": 401, "private_login": 401,
        "forbidden": 403, "sso_required": 403, "suspended": 403,
        "not_found": 404, "branch_not_found": 404, "snapshot_missing": 404, "job_not_found": 404,
        "other_owner": 404,
        "busy": 409, "not_installed": 409, "not_allowed": 409, "already_logged_in": 409,
        "too_big": 422, "too_many_files": 422, "empty_repo": 422,
        "rate_limited": 429,
        "login_config": 500, "disk": 500, "disk_full": 500,
        "network": 502, "server_error": 502, "bad_reply": 502, "tls": 502, "interrupted": 502,
        "commit_mismatch": 502, "outdated": 502,
        "closing": 503,
        "timeout": 504,
    }

    def __init__(self, code: str, message: str, status: int | None = None, *, gh_status: int | None = None,
                 **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or self.STATUS.get(code, 500)
        self.gh_status = gh_status
        self.extra = {k: v for k, v in extra.items() if v is not None}

    def reply(self) -> dict:
        """The JSON body: {"error", "code", **extra}, or only {"error"} for bad_request."""
        if self.code == "bad_request":
            return {"error": self.message}
        return {"error": self.message, "code": self.code, **self.extra}

    def copy(self) -> GitHubError:
        """A fresh exception with the same contents (for handing one failure to several threads)."""
        return GitHubError(self.code, self.message, self.status, gh_status=self.gh_status, **self.extra)

    def __repr__(self) -> str:
        return f"GitHubError({self.code!r}, status={self.status})"


# --- tokens in memory ------------------------------------------------------------------------------

class Secret:
    """A token in memory. repr and str never show it; comparing is constant-time.

    It is deliberately not JSON-serializable and cannot be pickled or copied, so a token that slips into
    a reply, a log argument or a deep copy raises instead of leaking.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise TypeError("Secret takes a str")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "Secret(***)"

    def __eq__(self, other) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode("utf-8", "surrogatepass"),
                                   other._value.encode("utf-8", "surrogatepass"))

    __hash__ = None  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce_ex__(self, protocol):
        raise TypeError("a Secret is never pickled or copied")


# --- which GitHub to talk to ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Hosts:
    """Base URLs for github.com (web: the device flow), api.github.com and codeload.github.com.

    fake: the hosts are tests/fake_github.py on loopback (plain http, memory-only token store).
    off: CODEVISION_GITHUB_FAKE was set but invalid, so GitHub is turned off (every request: login_config).
    """

    web: str
    api: str
    codeload: str
    fake: bool = False
    off: bool = False

    @classmethod
    def real(cls) -> Hosts:
        return cls(WEB_URL, API_URL, CODELOAD_URL, fake=False)

    @classmethod
    def for_fake(cls, base: str) -> Hosts:
        """base: a checked loopback address such as "http://127.0.0.1:5000" (see fake_base)."""
        return cls(base + "/web", base + "/api", base + "/codeload", fake=True)

    @classmethod
    def disabled(cls) -> Hosts:
        return cls("", "", "", fake=True, off=True)

    @property
    def verify_url(self) -> str:
        """Where the user types the code: the constant https://github.com/login/device (or the fake's page)."""
        if self.off:
            return ""
        return self.web + "/login/device" if self.fake else VERIFY_URL

    @property
    def install_url(self) -> str:
        if self.off:
            return ""
        return f"{self.web}/apps/{APP_SLUG}/installations/new" if self.fake else INSTALL_URL

    @property
    def settings_url(self) -> str:
        if self.off:
            return ""
        return self.web + "/settings/apps/authorizations" if self.fake else AUTH_SETTINGS_URL

    def repo_url(self, owner: str, repo: str) -> str:
        """The repo's GitHub page, built only from validated names (never from pasted text)."""
        if not (valid_owner(owner) and valid_repo(repo)):
            raise ValueError("not a valid owner/repo")
        if self.off:
            return ""
        return f"{self.web if self.fake else WEB_URL}/{owner}/{repo}"


def fake_base(text: str) -> str | None:
    """The normalized fake address for a CODEVISION_GITHUB_FAKE value, or None when it is not plain http
    to 127.0.0.1, localhost or [::1] with a port and nothing after it (a lone trailing "/" is fine)."""
    import urllib.parse  # light, and only needed here

    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > 100:
        return None
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (parts.scheme.lower() != "http" or host not in LOOPBACK_NAMES or not port
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc):
        return None
    return f"http://{'[::1]' if host == '::1' else host}:{port}"


def hosts_from_env() -> Hosts:
    """Real GitHub, the fake named by CODEVISION_GITHUB_FAKE, or GitHub turned off when that variable is
    set but is not a loopback http address, even an empty one (never the real hosts by mistake)."""
    raw = os.environ.get(FAKE_ENV)
    if raw is None:
        return Hosts.real()
    base = fake_base(raw)
    if base is None:
        log.warning("%s is set but is not a loopback http address with a port; GitHub is turned off", FAKE_ENV)
        return Hosts.disabled()
    return Hosts.for_fake(base)


# --- validators --------------------------------------------------------------------------------------

_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")  # '_' too: Enterprise Managed User names
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")  # git: control characters, space, ~ ^ : ? * [ \
_TOKEN = re.compile(r"[\x21-\x7e]{20,2000}")  # tokens are opaque: any prefix, 20-2,000 visible ASCII characters


def valid_token(s) -> bool:
    """A token GitHub sent: treated as an opaque string, since GitHub keeps changing token formats."""
    return isinstance(s, str) and _TOKEN.fullmatch(s) is not None


def valid_owner(s) -> bool:
    return isinstance(s, str) and _OWNER.fullmatch(s) is not None


def valid_repo(s) -> bool:
    return isinstance(s, str) and _REPO.fullmatch(s) is not None and s not in (".", "..")


def valid_sha(s) -> bool:
    return isinstance(s, str) and _SHA.fullmatch(s) is not None


def valid_branch(s) -> bool:
    """git's ref-name rules, applied literally (git check-ref-format), plus 1-255 characters, no leading '-'
    and UTF-8. Non-ASCII spaces and characters such as < > " ' & are allowed, as git allows them."""
    if not isinstance(s, str) or not 1 <= len(s) <= 255:
        return False
    if _BRANCH_BAD.search(s) or s == "@" or ".." in s or "@{" in s or "//" in s:
        return False
    if s.startswith(("-", "/")) or s.endswith(("/", ".")):
        return False
    for part in s.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            return False
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def repo_dir(owner: str, repo: str) -> tuple[str, str]:
    """Owner and repo as folder keys: GitHub names ignore case."""
    return owner.lower(), repo.lower()


# --- the AI side's project id for a snapshot ---------------------------------------------------------

_PROJECT = re.compile(r"github:([^/@\s]{1,39})/([^/@\s]{1,100})@([0-9a-f]{40})")


def project_id(owner: str, repo: str, sha: str) -> str:
    """'github:<owner_lc>/<repo_lc>@<sha>'. Raises ValueError for names that are not valid."""
    if not (valid_owner(owner) and valid_repo(repo) and valid_sha(sha)):
        raise ValueError("not a valid owner, repo or sha")
    return f"github:{owner.lower()}/{repo.lower()}@{sha}"


def parse_project(text) -> tuple[str, str, str] | None:
    """(owner_lc, repo_lc, sha) for a 'github:' project id, or None. No network, no disk."""
    if not isinstance(text, str):
        return None
    m = _PROJECT.fullmatch(text.strip())
    if not m:
        return None
    owner, repo, sha = m.groups()
    if not (valid_owner(owner) and valid_repo(repo) and valid_sha(sha)):
        return None
    return owner.lower(), repo.lower(), sha
