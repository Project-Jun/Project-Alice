"""Signing in with the GitHub App device flow, and keeping the login alive (refreshes).

Who holds what: the page only sees the user code, the verification address, the flow state and the login
name. DeviceLogin keeps device_code (a Secret) in memory; TokenManager keeps the tokens as Secret objects and
in the store. There is no client secret anywhere: the device flow and refreshes run without one.

Locks are held only while state is swapped (and for the local store calls that go with a swap), never across a
network call, so status() never waits for GitHub. One refresh runs at a time: other threads that need one wait
for its result instead of sending their own (refresh tokens work once).
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace

from . import messages as M
from .base import CLIENT_ID, REFRESH_SOON_ENV, GitHubError, Secret, valid_owner, valid_token
from .store import Tokens, TokenStore, same_pair

REFRESH_MARGIN = 300.0   # refresh when less than this many seconds of the access token are left
SOON_AGE = 60.0          # CODEVISION_GITHUB_REFRESH_SOON=1: a token older than this counts as expiring
RECHECK_WAIT = 1.5       # after bad_refresh_token, wait this long and read the store again
FLIGHT_WAIT = 45.0       # a thread waiting for another thread's refresh gives up after this long
DEVICE_CODE_PATH = "/login/device/code"
TOKEN_PATH = "/login/oauth/access_token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_BACKOFF = 60.0       # the longest wait between polls while GitHub cannot be reached
OAUTH_TIMEOUT = 15.0     # seconds for each socket operation of a code request, poll or refresh
TROUBLE_AFTER = 3        # failed polls in a row before the flow says so
LOGIN_THREAD = "codevision-github-login"

# Errors that leave a login as it is: GitHub could not be asked, or its answer could not be read.
TRANSIENT = frozenset({"network", "timeout", "tls", "server_error", "bad_reply", "rate_limited", "interrupted"})

_USER_CODE = re.compile(r"[A-Z0-9][A-Z0-9-]{3,15}")
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,40}")

log = logging.getLogger(__name__)


def _code_for_log(value) -> str:
    """A GitHub OAuth error code, safe to log (anything else becomes 'unknown')."""
    return value if isinstance(value, str) and _ERROR_CODE.fullmatch(value) else "unknown"


def _seconds(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _user_from(data) -> tuple[str, int]:
    if not isinstance(data, dict):
        return "", 0
    login, uid = data.get("login"), data.get("id")
    if not valid_owner(login) or isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        return "", 0
    return login, uid


@dataclass
class _Flight:
    """One refresh in progress; other threads wait on `done` for its result."""

    done: threading.Event = field(default_factory=threading.Event)
    result: Secret | None = None
    error: GitHubError | None = None


class TokenManager:
    """The login's tokens and their lifetime: refresh 5 minutes before expiry or once after a 401, rotate the
    pair, save it before use, and log out when GitHub refuses a refresh (never on a network error).

    client_factory() gives the GitHubClient for refreshes and /user; bind() sets it after construction.
    """

    def __init__(self, store: TokenStore, client_factory=None, *, clock=time.time, sleep=time.sleep):
        self.store = store
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens: Tokens | None = None
        self._loaded = False
        self._flight: _Flight | None = None
        self._ended = ""  # why the login ended: "expired" (GitHub refused a refresh) or "too_old"

    def bind(self, client) -> None:
        self._client_factory = lambda: client

    def _client(self):
        if self._client_factory is None:
            raise GitHubError("login_config", M.LOGIN_CONFIG)
        return self._client_factory()

    # --- reading the state (never waits for GitHub) ---

    def _current(self) -> Tokens | None:
        with self._lock:
            if not self._loaded:  # read lazily, once per run (the store is local)
                self._loaded = True
                self._tokens = self.store.load()
                if self._tokens is None and self.store.stale:
                    self._ended = "too_old"
            return self._tokens

    def logged_in(self) -> bool:
        tokens = self._current()
        return tokens is not None and tokens.usable(self._clock())

    def login(self) -> str:
        tokens = self._current()
        return tokens.login if tokens is not None else ""

    def user_id(self) -> int:
        tokens = self._current()
        return tokens.user_id if tokens is not None else 0

    def note(self) -> str:
        """Why the user is logged out now, for the status line ('' when nothing happened)."""
        self._current()
        return {"expired": M.LOGIN_EXPIRED, "too_old": M.LOGIN_TOO_OLD}.get(self._ended, "")

    def login_needed_message(self) -> str:
        self._current()
        return {"expired": M.LOGIN_EXPIRED, "too_old": M.LOGIN_TOO_OLD}.get(self._ended, M.LOGIN_NEEDED)

    # --- tokens for requests ---

    def _expiring(self, tokens: Tokens, now: float, min_left: float) -> bool:
        if os.environ.get(REFRESH_SOON_ENV, "").strip() == "1" and now - tokens.saved_at >= SOON_AGE:
            return True
        return tokens.access_expires_at is not None and tokens.access_expires_at - now <= min_left

    def access(self, min_left: float = REFRESH_MARGIN) -> Secret | None:
        """A working access token, refreshed first when less than `min_left` seconds are left. None when
        nobody is logged in (or GitHub refused the refresh: the login is then cleared). A network error during
        a refresh keeps the tokens and is raised as GitHubError."""
        tokens = self._current()
        if tokens is None:
            return None
        now = self._clock()
        if tokens.access is not None and not self._expiring(tokens, now, min_left):
            return tokens.access
        if not tokens.refresh_ok(now):
            if tokens.access_ok(now):
                return tokens.access  # cannot be refreshed, but it still works for a while
            self._end(tokens, "too_old")
            return None
        return self._refresh(tokens)

    def refreshed_after_401(self, used: Secret) -> Secret | None:
        """After GitHub answered 401 to `used`: a token another thread or process already got, else one
        refresh. None means logged out."""
        with self._lock:
            tokens = self._tokens
        if tokens is None:
            return None
        now = self._clock()
        if tokens.access is not None and tokens.access != used and tokens.access_ok(now):
            return tokens.access
        newer = self.store.load()  # another CodeVision may have rotated the pair
        if newer is not None and newer.access is not None and newer.access != used and newer.access_ok(now):
            with self._lock:
                if self._tokens is tokens:
                    self._tokens = newer
            return newer.access
        if not tokens.refresh_ok(now):
            self._end(tokens, "expired")
            return None
        return self._refresh(tokens)

    def rejected(self, used: Secret) -> None:
        """GitHub answered 401 again to a fresh token: the login no longer works, so it is cleared."""
        with self._lock:
            tokens = self._tokens
        if tokens is not None and tokens.access is not None and tokens.access == used:
            log.warning("GitHub refused a fresh token (login_rejected); the login is cleared")
            self._end(tokens, "expired")

    def _end(self, tokens: Tokens, why: str) -> None:
        with self._lock:
            if self._tokens is not tokens:
                return
            self.store.clear_if(tokens)
            self._tokens = None
            self._ended = why

    # --- refreshing (one at a time) ---

    def _refresh(self, base: Tokens) -> Secret | None:
        with self._lock:
            current = self._tokens
            if current is None:
                return None
            if current is not base and current.access is not None and current.access_ok(self._clock(), 60):
                return current.access  # another thread refreshed meanwhile
            flight = self._flight
            leader = flight is None
            if leader:
                flight = self._flight = _Flight()
                base = current
        if not leader:
            if not flight.done.wait(FLIGHT_WAIT):
                raise GitHubError("timeout", M.TIMEOUT)
            if flight.error is not None:
                raise flight.error.copy()
            return flight.result
        try:
            flight.result = self._do_refresh(base)
        except GitHubError as exc:
            flight.error = exc
            raise
        except Exception:
            flight.error = GitHubError("network", M.NETWORK)
            raise
        finally:
            with self._lock:
                self._flight = None
            flight.done.set()
        return flight.result

    def _do_refresh(self, base: Tokens) -> Secret | None:
        client = self._client()
        try:
            reply = client.post_form(TOKEN_PATH, {"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                                  "refresh_token": base.refresh.reveal()}, OAUTH_TIMEOUT,
                                     op="refresh")
        except GitHubError as exc:
            if exc.code in TRANSIENT or exc.code == "closing":
                raise  # GitHub could not be asked: the tokens are kept
            reply = {"error": exc.code}
        # From here GitHub has answered, and anything but a new pair is a refusal.
        error = reply.get("error")
        tokens = None
        if error is None:
            try:
                tokens = self._tokens_from(reply, login=base.login, user_id=base.user_id)
            except GitHubError:
                error = "unusable_reply"
        if tokens is None:
            # Every refusal is a logout with the normal sentence; the log gets the distinct code.
            log.warning("GitHub refresh_rejected (%s)", _code_for_log(error))
            return self._after_refusal(base)
        with self._lock:
            current = self._tokens
            if current is not None and current.login and not tokens.login:
                tokens = replace(tokens, login=current.login, user_id=current.user_id)
            self.store.save(tokens)  # before use: the old pair stopped working
            self._tokens = tokens
            self._ended = ""
        return tokens.access

    def _after_refusal(self, base: Tokens) -> Secret | None:
        self._sleep(RECHECK_WAIT)  # another CodeVision may be saving a pair it just rotated
        newer = self.store.load()
        now = self._clock()
        if newer is not None and newer.usable(now) and not same_pair(newer, base):
            with self._lock:
                self._tokens = newer
                self._ended = ""
            return newer.access if newer.access_ok(now) else None
        with self._lock:
            self.store.clear_if(base)
            if self._tokens is None or same_pair(self._tokens, base):
                self._tokens = None
                self._ended = "expired"
        return None

    def _tokens_from(self, reply: dict, *, login: str = "", user_id: int = 0) -> Tokens:
        """Tokens from a token reply. Tokens are opaque strings (any prefix, 20-2,000 visible ASCII
        characters). Raises GitHubError login_config for a reply that is not a usable bearer token."""
        token_type = reply.get("token_type")
        access, refresh = reply.get("access_token"), reply.get("refresh_token")
        if (not isinstance(token_type, str) or token_type.lower() != "bearer" or not valid_token(access)
                or (refresh is not None and not valid_token(refresh))):
            raise GitHubError("login_config", M.LOGIN_CONFIG)
        now = self._clock()
        expires_in = _seconds(reply.get("expires_in"))
        refresh_in = _seconds(reply.get("refresh_token_expires_in"))
        return Tokens(access=Secret(access), access_expires_at=now + expires_in if expires_in else None,
                      refresh=Secret(refresh) if refresh is not None else None,
                      refresh_expires_at=now + refresh_in if (refresh is not None and refresh_in) else None,
                      login=login, user_id=user_id, client_id=CLIENT_ID, saved_at=now)

    # --- logging in and out ---

    def accept(self, reply: dict) -> Tokens:
        """Check a successful token reply and save it as the login (no name yet: see set_user)."""
        tokens = self._tokens_from(reply)
        with self._lock:
            self.store.save(tokens)
            self._tokens = tokens
            self._loaded = True
            self._ended = ""
        return tokens

    def set_user(self, login: str, user_id: int) -> None:
        """Add the GitHub login name and user id to the saved login."""
        if not valid_owner(login) or isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("not a GitHub login and user id")
        with self._lock:
            tokens = self._tokens
            if tokens is None:
                return
            tokens = replace(tokens, login=login, user_id=user_id)
            self.store.save(tokens)
            self._tokens = tokens

    def ensure_user(self) -> str:
        """The login name, asking GitHub (GET /user) when it is not known yet. '' when that fails."""
        tokens = self._current()
        if tokens is None:
            return ""
        if tokens.login:
            return tokens.login
        try:
            reply = self._client().get("/user", auth="require", op="user")
        except GitHubError as exc:
            log.info("GitHub user lookup failed (%s)", exc.code)
            return ""
        login, uid = _user_from(reply.data)
        if login:
            self.set_user(login, uid)
        return login

    def forget(self) -> bool:
        """Log out here: drop the tokens and delete the store entry. False when the OS store could not delete
        it (the tokens are dropped from memory anyway)."""
        with self._lock:
            self._tokens = None
            self._loaded = True
            self._ended = ""
            return self.store.clear()


# --- the device flow --------------------------------------------------------------------------------------------

class _Flow:
    def __init__(self, device_code: Secret, user_code: str, verification_uri: str, expires_in: float,
                 interval: float, now: float):
        self.device_code: Secret | None = device_code
        self.user_code = user_code
        self.verification_uri = verification_uri
        self.deadline = now + expires_in
        self.interval = interval
        self.state = "waiting"
        self.message = M.WAITING
        self.code = ""
        self.ended_at: float | None = None
        self.stop = threading.Event()


class DeviceLogin:
    """One device-flow login per server, polled in the daemon thread 'codevision-github-login'.

    start() asks GitHub for a code (or returns the flow that is still waiting), view() is the flow block for
    the status reply, cancel() stops a waiting flow, close() stops everything and never joins.
    Flow view: {state, user_code, verification_uri, expires_in, message, code}; user_code, verification_uri and
    expires_in only while waiting; code is set only for failed: login_code | login_config | unverified_email.
    """

    def __init__(self, client, tokens: TokenManager, *, clock=time.monotonic):
        self.client = client
        self.tokens = tokens
        self._clock = clock
        self._lock = threading.Lock()
        self._flow: _Flow | None = None
        self._starting: threading.Event | None = None
        self._closed = False

    # --- the page's calls ---

    def start(self) -> dict:
        """A waiting flow (the same one while it is still waiting, so a reloaded page re-attaches), or a new
        one. Refused while logged in: the user logs out first. Raises GitHubError when GitHub cannot be asked
        (network, timeout, tls, server_error, bad_reply, rate_limited) or the reply is unusable (login_config)."""
        while True:
            with self._lock:
                if self._closed:
                    raise GitHubError("closing", M.CLOSING)
                flow = self._flow
                if flow is not None and flow.state == "waiting" and self._clock() < flow.deadline:
                    return self._view(flow)
                if flow is not None and flow.state == "finishing":  # its tokens are being saved: no second code,
                    return self._view(flow)                         # which would replace them once approved
                starting = self._starting
                if starting is None:
                    starting = self._starting = threading.Event()
                    break
            starting.wait(30.0)  # another request is asking GitHub for a code; use its flow
        try:
            if self.tokens.logged_in():
                raise GitHubError("already_logged_in", M.ALREADY_LOGGED_IN)
            reply = self.client.post_form(DEVICE_CODE_PATH, {"client_id": CLIENT_ID}, OAUTH_TIMEOUT,
                                          op="device_code")
            flow = self._new_flow(reply)
        except BaseException:
            with self._lock:
                self._starting = None
            starting.set()
            raise
        with self._lock:
            self._starting = None
            if self._closed:
                starting.set()
                raise GitHubError("closing", M.CLOSING)
            old = self._flow
            if old is not None:
                old.stop.set()
            self._flow = flow
            threading.Thread(target=self._run, args=(flow,), name=LOGIN_THREAD, daemon=True).start()
            view = self._view(flow)
        starting.set()
        return view

    def view(self, max_age: float | None = None) -> dict | None:
        """The current flow block, or None (also for a flow that ended more than max_age seconds ago)."""
        with self._lock:
            flow = self._flow
            if flow is None:
                return None
            if flow.state == "waiting" and self._clock() >= flow.deadline:
                self._finish(flow, "expired", M.EXPIRED)  # the poller may still be asleep
            if max_age is not None and flow.ended_at is not None and self._clock() - flow.ended_at > max_age:
                return None
            return self._view(flow)

    def cancel(self) -> dict | None:
        """Stop a waiting flow (state cancelled; the device code is dropped). An ended flow comes back as is."""
        with self._lock:
            flow = self._flow
            if flow is None:
                return None
            if flow.state == "waiting":
                self._finish(flow, "cancelled", M.CANCELLED)
            return self._view(flow)

    def close(self) -> None:
        """Server close: wake the poller so it exits. Never joins."""
        with self._lock:
            self._closed = True
            flow = self._flow
            if flow is not None and flow.state == "waiting":
                self._finish(flow, "cancelled", M.CANCELLED)
            elif flow is not None:
                flow.stop.set()

    # --- inside ---

    def _view(self, flow: _Flow) -> dict:
        out: dict = {"state": flow.state}
        if flow.state == "waiting":
            out["user_code"] = flow.user_code
            out["verification_uri"] = flow.verification_uri
            out["expires_in"] = max(0, int(math.ceil(flow.deadline - self._clock())))
        out["message"] = flow.message
        out["code"] = flow.code
        return out

    def _finish(self, flow: _Flow, state: str, message: str, code: str = "") -> None:
        """End a flow (the caller holds the lock)."""
        flow.state = state
        flow.message = message
        flow.code = code
        flow.device_code = None
        flow.ended_at = self._clock()
        flow.stop.set()

    def _number(self, value, default: float, low: float, high: float, fake_low: float) -> float:
        """A number from GitHub's reply, kept in range. The fake may send fractions so tests run quickly."""
        fake = bool(getattr(getattr(self.client, "hosts", None), "fake", False))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return float(default)
        if isinstance(value, float) and not fake:
            value = int(value)
        return float(min(max(value, fake_low if fake else low), high))

    def _new_flow(self, reply: dict) -> _Flow:
        error = reply.get("error")
        device_code, user_code = reply.get("device_code"), reply.get("user_code")
        if error is not None or not isinstance(device_code, str) or not 1 <= len(device_code) <= 200 \
                or not device_code.isascii() or not device_code.isprintable() \
                or not isinstance(user_code, str) or not _USER_CODE.fullmatch(user_code):
            log.warning("GitHub refused to start a login (%s)", _code_for_log(error) if error else "bad_reply")
            raise GitHubError("login_config", M.LOGIN_CONFIG)
        expires_in = self._number(reply.get("expires_in"), 900, 60, 1800, 0.2)
        interval = self._number(reply.get("interval"), 5, 1, 60, 0.01)
        verification_uri = self.client.hosts.verify_url  # the known address, never one taken from the reply
        if reply.get("verification_uri") != verification_uri:
            log.info("GitHub named another verification address; the known one is shown")
        return _Flow(Secret(device_code), user_code, verification_uri, expires_in, interval, self._clock())

    def _run(self, flow: _Flow) -> None:
        try:
            self._poll(flow)
        except BaseException as exc:  # never a traceback with values on stderr: the type name only
            log.error("GitHub login thread stopped: %s (login_crash)", type(exc).__name__)
            with self._lock:
                if flow.state in ("waiting", "finishing"):
                    self._finish(flow, "failed", M.LOGIN_CONFIG, "login_config")

    def _slower(self, reply: dict, interval: float) -> float:
        """slow_down: the reply's new interval when it is sensible, else interval + 5 (what GitHub adds)."""
        new = reply.get("interval")
        fake = bool(getattr(self.client.hosts, "fake", False))
        if isinstance(new, int) and not isinstance(new, bool) and 1 <= new <= 120:
            return float(new)
        if fake and isinstance(new, float) and 0.01 <= new <= 120:
            return new
        return interval + 5.0

    def _poll(self, flow: _Flow) -> None:
        interval = flow.interval
        fails = 0
        while True:
            wait = interval if fails == 0 else min(MAX_BACKOFF, interval * (2 ** fails))
            left = flow.deadline - self._clock()
            if flow.stop.wait(max(0.0, min(wait, left))):
                return
            with self._lock:
                if flow.state != "waiting":
                    return
                if self._clock() >= flow.deadline:
                    self._finish(flow, "expired", M.EXPIRED)
                    return
                device_code = flow.device_code
            if device_code is None:
                return
            try:
                reply = self.client.post_form(TOKEN_PATH, {"client_id": CLIENT_ID,
                                                           "device_code": device_code.reveal(),
                                                           "grant_type": DEVICE_GRANT}, OAUTH_TIMEOUT,
                                              op="device_poll")
            except GitHubError as exc:
                if exc.code in TRANSIENT:
                    fails += 1
                    if fails >= TROUBLE_AFTER:
                        with self._lock:
                            if flow.state == "waiting":
                                flow.message = M.POLL_TROUBLE
                    continue
                with self._lock:
                    if exc.code == "closing":  # the server is shutting down: stop quietly
                        if flow.state == "waiting":
                            self._finish(flow, "cancelled", M.CANCELLED)
                    elif flow.state == "waiting":
                        self._finish(flow, "failed", M.LOGIN_CONFIG, "login_config")
                return
            if flow.stop.is_set():
                return  # cancelled while GitHub was being asked
            if fails:
                fails = 0
                with self._lock:
                    if flow.state == "waiting":
                        flow.message = M.WAITING
            error = reply.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = self._slower(reply, interval)
                continue
            ending = {
                "expired_token": ("expired", M.EXPIRED, ""), "token_expired": ("expired", M.EXPIRED, ""),
                "access_denied": ("denied", M.DENIED, ""),
                "incorrect_device_code": ("failed", M.LOGIN_CODE, "login_code"),
                "bad_verification_code": ("failed", M.LOGIN_CODE, "login_code"),
                "unverified_user_email": ("failed", M.UNVERIFIED_EMAIL, "unverified_email"),
            }.get(error) if isinstance(error, str) else None
            if ending is None and (error is not None or not reply.get("access_token")):
                log.warning("GitHub login stopped (%s)", _code_for_log(error))
                ending = ("failed", M.LOGIN_CONFIG, "login_config")
            if ending is not None:
                with self._lock:
                    if flow.state == "waiting":
                        self._finish(flow, *ending)
                return
            self._complete(flow, reply)
            return

    def _complete(self, flow: _Flow, reply: dict) -> None:
        with self._lock:
            if flow.state != "waiting":
                return
            flow.state = "finishing"
            flow.message = M.FINISHING
            flow.device_code = None
        try:
            self.tokens.accept(reply)
        except GitHubError as exc:
            log.warning("GitHub login reply could not be used (%s)", exc.code)
            with self._lock:
                self._finish(flow, "failed", M.LOGIN_CONFIG, "login_config")
            return
        try:  # the name; when this fails the tokens are kept, and the name is asked again later
            login, uid = _user_from(self.client.get("/user", auth="require", op="user").data)
            if login:
                self.tokens.set_user(login, uid)
        except GitHubError as exc:
            log.info("GitHub user lookup after login failed (%s)", exc.code)
        with self._lock:
            self._finish(flow, "done", M.DONE)
