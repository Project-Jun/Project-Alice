"""The token store: where the GitHub login lives between runs. Never plain text, never a file.

- Windows: one generic credential in Windows Credential Manager, written with win32ctypes' win32cred directly
  (keyring's own dependency): TargetName 'CodeVision/github', UserName 'oauth-tokens', CRED_PERSIST_LOCAL_MACHINE
  (it never roams with a domain profile). One CredWrite overwrites in place, so there is never a gap and never
  a second copy; a leftover keyring-style 'oauth-tokens@CodeVision/github' copy is removed. keyring itself, its
  config file and PYTHON_KEYRING_BACKEND are never consulted on Windows.
- Other systems: an allowlisted OS keyring backend (macOS Keychain, Secret Service, libsecret, KWallet), made
  directly rather than through keyring's detection.
- Memory only, for the rest of this run, when CODEVISION_KEYRING=memory, in fake GitHub mode, whenever
  CODEVISION_HOME is set (a test or scratch run) unless CODEVISION_KEYRING=os, when no OS store can be used,
  or when an OS store call fails. Nothing is then written anywhere.

The value is one compact JSON blob; win32cred stores it as UTF-16, at most 2,560 bytes. A longer blob drops the
access token (it stays in memory for this run) and keeps only the refresh token. Only exception type names and
Windows error numbers are logged, never values.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, replace

from . import messages as M
from .base import CLIENT_ID, HOME_ENV, KEYRING_ENV, Secret, valid_owner, valid_token

SERVICE = "CodeVision/github"
USERNAME = "oauth-tokens"
COMPOUND = f"{USERNAME}@{SERVICE}"  # where keyring's WinVault backend would have moved an older copy
MAX_BLOB_BYTES = 2560  # CRED_MAX_CREDENTIAL_BLOB_SIZE: the blob is UTF-16, so 1,280 ASCII characters
COMMENT = "CodeVision GitHub login"
_NOT_FOUND = 1168  # ERROR_NOT_FOUND from CredRead / CredDelete

log = logging.getLogger(__name__)


# --- the login itself ----------------------------------------------------------------------------------

def _secret(value) -> Secret | None:
    if value is None:
        return None
    if not valid_token(value):
        raise ValueError("not a token")
    return Secret(value)


def _time(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value != value:
        raise ValueError("not a time")
    return float(value)


@dataclass
class Tokens:
    """One GitHub login: the 8-hour access token and the 6-month refresh token, as Secret objects."""

    access: Secret | None
    access_expires_at: float | None
    refresh: Secret | None
    refresh_expires_at: float | None
    login: str = ""
    user_id: int = 0
    client_id: str = CLIENT_ID
    saved_at: float = 0.0

    def access_ok(self, now: float, min_left: float = 0.0) -> bool:
        return self.access is not None and (self.access_expires_at is None or self.access_expires_at - now > min_left)

    def refresh_ok(self, now: float) -> bool:
        return self.refresh is not None and (self.refresh_expires_at is None or self.refresh_expires_at > now)

    def usable(self, now: float) -> bool:
        return self.access_ok(now) or self.refresh_ok(now)

    def to_json(self, include_access: bool = True) -> str:
        data: dict = {"v": 1, "client_id": self.client_id}
        if include_access and self.access is not None:
            data["access_token"] = self.access.reveal()
            data["access_expires_at"] = self.access_expires_at
        if self.refresh is not None:
            data["refresh_token"] = self.refresh.reveal()
            data["refresh_expires_at"] = self.refresh_expires_at
        data.update(login=self.login, user_id=self.user_id, saved_at=self.saved_at)
        return json.dumps(data, separators=(",", ":"), ensure_ascii=True)

    @classmethod
    def from_json(cls, text: str) -> Tokens:
        """Raises ValueError for anything that is not a CodeVision login blob."""
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("client_id"), str):
            raise ValueError("not a CodeVision login")
        access, refresh = _secret(data.get("access_token")), _secret(data.get("refresh_token"))
        if access is None and refresh is None:
            raise ValueError("no token")
        login, user_id = data.get("login"), data.get("user_id")
        return cls(access=access, access_expires_at=_time(data.get("access_expires_at")),
                   refresh=refresh, refresh_expires_at=_time(data.get("refresh_expires_at")),
                   login=login if valid_owner(login) else "",
                   user_id=user_id if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0 else 0,
                   client_id=data["client_id"], saved_at=_time(data.get("saved_at")) or 0.0)


def same_pair(a: Tokens | None, b: Tokens | None) -> bool:
    """True when both hold the same refresh token (and the same access token, when both have one)."""
    if a is None or b is None:
        return a is b
    if (a.refresh is None) != (b.refresh is None) or (a.refresh is not None and a.refresh != b.refresh):
        return False
    if a.access is not None and b.access is not None and a.access != b.access:
        return False
    return True


def _blob_size(blob: str) -> int:
    return len(blob.encode("utf-16-le", "surrogatepass"))


def _winerror(exc) -> int | None:
    value = getattr(exc, "winerror", None)
    return value if isinstance(value, int) else None


def _describe(exc) -> str:
    code = _winerror(exc)
    return f"{type(exc).__name__}, winerror {code}" if code is not None else type(exc).__name__


# --- backends: anything with get_password / set_password / delete_password -----------------------------------

class MemoryBackend:
    """Keeps entries in this process only. Deliberately a plain class, not a keyring backend, so keyring never
    registers it (its ChainerBackend would otherwise pair it with the real Credential Manager)."""

    kind = "memory"

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], str] = {}
        self.fail: set[str] = set()  # tests: "get", "set" and/or "delete" then raise
        self.writes = 0

    def _maybe_fail(self, what: str) -> None:
        if what in self.fail:
            raise RuntimeError(f"the test backend fails on {what}")

    def get_password(self, service: str, username: str) -> str | None:
        self._maybe_fail("get")
        with self._lock:
            return self._items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._maybe_fail("set")
        if not isinstance(password, str):
            raise TypeError("a password is a str")
        with self._lock:
            self._items[(service, username)] = password
            self.writes += 1

    def delete_password(self, service: str, username: str) -> None:
        self._maybe_fail("delete")
        with self._lock:
            self._items.pop((service, username), None)

    def entries(self) -> dict[tuple[str, str], str]:
        with self._lock:
            return dict(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def _load_win32cred():
    """win32ctypes' win32cred (keyring's own Windows dependency). Raises ImportError when it is missing."""
    from win32ctypes.pywin32 import win32cred

    return win32cred


class WinCredBackend:
    """Windows Credential Manager through win32cred: CredRead, CredWrite (overwrites in place) and CredDelete
    on one generic credential, persisted for this machine only."""

    kind = "keyring"

    def __init__(self, win32cred=None):
        self._cred = win32cred if win32cred is not None else _load_win32cred()
        self._generic = self._cred.CRED_TYPE_GENERIC
        self._persist = self._cred.CRED_PERSIST_LOCAL_MACHINE

    def get_password(self, service: str, username: str) -> str | None:
        try:
            item = self._cred.CredRead(TargetName=service, Type=self._generic)
        except Exception as exc:
            if _winerror(exc) == _NOT_FOUND:
                return None
            raise
        if not item or item.get("UserName") != username:
            return None
        blob = item.get("CredentialBlob") or b""
        if isinstance(blob, str):
            return blob
        try:
            return bytes(blob).decode("utf-16-le")
        except UnicodeDecodeError:
            return bytes(blob).decode("utf-8")

    def set_password(self, service: str, username: str, password: str) -> None:
        self._cred.CredWrite({"Type": self._generic, "TargetName": service, "UserName": username,
                              "CredentialBlob": password, "Comment": COMMENT, "Persist": self._persist}, 0)
        try:  # a copy keyring's WinVault backend would have left behind
            self._delete(f"{username}@{service}")
        except Exception as exc:
            log.info("an old copy of the GitHub login could not be removed (%s)", _describe(exc))

    def delete_password(self, service: str, username: str) -> None:
        self._delete(service)
        self._delete(f"{username}@{service}")

    def _delete(self, target: str) -> None:
        try:
            self._cred.CredDelete(TargetName=target, Type=self._generic)
        except Exception as exc:
            if _winerror(exc) != _NOT_FOUND:
                raise


_OS_BACKENDS = (
    ("keyring.backends.macOS", "Keyring"),
    ("keyring.backends.SecretService", "Keyring"),
    ("keyring.backends.libsecret", "Keyring"),
    ("keyring.backends.kwallet", "DBusKeyring"),
    ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
)


class OSKeyringBackend:
    """An allowlisted keyring backend on macOS and Linux, made directly (keyring's detection, its config file
    and PYTHON_KEYRING_BACKEND could otherwise pick a plaintext or chained backend)."""

    kind = "keyring"

    def __init__(self):
        import importlib

        self._kr = None
        for module_name, class_name in _OS_BACKENDS:
            try:
                cls = getattr(importlib.import_module(module_name), class_name)
                if cls.priority < 1:  # the property raises when the backend cannot work here
                    continue
                self._kr = cls()
                break
            except Exception:
                continue
        if self._kr is None:
            raise RuntimeError("no OS keyring")

    def get_password(self, service: str, username: str) -> str | None:
        return self._kr.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._kr.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            self._kr.delete_password(service, username)
        except PasswordDeleteError:
            pass


_MEMORY = MemoryBackend()  # the process-wide memory store (CODEVISION_KEYRING=memory, fake mode, CODEVISION_HOME)
_override = None


def use_backend(backend) -> None:
    """Test hook: every TokenStore made from now on uses `backend` (None: choose as usual)."""
    global _override
    _override = backend


def memory_backend() -> MemoryBackend:
    """The process-wide memory store."""
    return _MEMORY


def choose_backend(fake: bool = False):
    """The backend a new TokenStore uses, in this order: the use_backend override; memory for
    CODEVISION_KEYRING=memory, fake mode, or CODEVISION_HOME without CODEVISION_KEYRING=os; the OS store;
    memory when the OS store cannot be used."""
    if _override is not None:
        return _override
    mode = (os.environ.get(KEYRING_ENV) or "").strip().lower()
    if mode == "memory" or fake:
        return _MEMORY
    if (os.environ.get(HOME_ENV) or "").strip() and mode != "os":
        return _MEMORY
    try:
        return WinCredBackend() if os.name == "nt" else OSKeyringBackend()
    except Exception as exc:
        log.warning("the OS credential store cannot be used (%s); the GitHub login stays in memory", _describe(exc))
        return MemoryBackend()


# --- the store -------------------------------------------------------------------------------------------------

class TokenStore:
    """load / save / clear for the one login entry. Thread-safe: one lock covers every store call (all local;
    none of them touches the network). The backend is chosen on first use, never at server start."""

    def __init__(self, fake: bool = False, backend=None):
        self.fake = bool(fake)
        self._fixed = backend
        self._backend = None
        self._os_backend = None  # the OS store we fell back from, still cleared on logout
        self._lock = threading.Lock()
        self._stale = False
        self._held: tuple | None = None  # (refresh, access, access_expires_at) the OS entry had no room for

    def _pick(self):
        if self._backend is None:
            self._backend = self._fixed if self._fixed is not None else choose_backend(self.fake)
        return self._backend

    def _fall_back(self, exc, what: str) -> None:
        log.warning("the credential store could not %s the GitHub login (%s); it stays in memory for this run",
                    what, _describe(exc))
        if getattr(self._backend, "kind", "keyring") != "memory":
            self._os_backend = self._backend
        self._backend = MemoryBackend()

    @property
    def kind(self) -> str:
        """'keyring' (an OS store) or 'memory'."""
        with self._lock:
            return "memory" if getattr(self._pick(), "kind", "keyring") == "memory" else "keyring"

    @property
    def note(self) -> str:
        """The status note about where the login lives ('' for an OS store)."""
        if self.fake:
            return M.FAKE_MODE
        return M.STORE_MEMORY if self.kind == "memory" else ""

    @property
    def stale(self) -> bool:
        """True when the last load() found a login that was too old or for another app (and removed it)."""
        return self._stale

    def load(self) -> Tokens | None:
        with self._lock:
            self._stale = False
            backend = self._pick()
            try:
                blob = backend.get_password(SERVICE, USERNAME)
            except Exception as exc:
                self._fall_back(exc, "read")
                return None
            if blob is None:
                return None
            try:
                tokens = Tokens.from_json(blob)
            except (ValueError, TypeError, RecursionError):
                log.warning("the saved GitHub login could not be read; it is removed")
                self._delete_quietly(backend)
                return None
            if tokens.client_id != CLIENT_ID or not tokens.usable(time.time()):
                log.info("the saved GitHub login is too old or belongs to another app; it is removed")
                self._stale = True
                self._delete_quietly(backend)
                return None
            held = self._held
            if tokens.access is None and held is not None and tokens.refresh is not None and tokens.refresh == held[0]:
                tokens = replace(tokens, access=held[1], access_expires_at=held[2])
            return tokens

    def save(self, tokens: Tokens) -> None:
        """Save the login (refreshed tokens are saved before they are used: a refresh token works once)."""
        with self._lock:
            backend = self._pick()
            blob = tokens.to_json()
            self._held = None
            if getattr(backend, "kind", "keyring") != "memory" and _blob_size(blob) > MAX_BLOB_BYTES:
                short = tokens.to_json(include_access=False)
                if tokens.refresh is not None and _blob_size(short) <= MAX_BLOB_BYTES:
                    log.info("the GitHub login is too large for one credential; the access token stays in memory")
                    blob = short
                    self._held = (tokens.refresh, tokens.access, tokens.access_expires_at)
                else:
                    log.warning("the GitHub login is too large for the credential store; it stays in memory")
                    self._delete_quietly(backend)
                    self._os_backend = backend
                    self._backend = backend = MemoryBackend()
            try:
                backend.set_password(SERVICE, USERNAME, blob)
            except Exception as exc:
                self._fall_back(exc, "save")
                self._delete_quietly(self._os_backend)
                self._backend.set_password(SERVICE, USERNAME, tokens.to_json())

    def clear(self) -> bool:
        """Delete the login. False when the OS store could not delete it (the page then asks the user to)."""
        with self._lock:
            self._held = None
            self._stale = False
            ok = True
            for backend in (self._pick(), self._os_backend):
                if backend is None:
                    continue
                try:
                    backend.delete_password(SERVICE, USERNAME)
                except Exception as exc:
                    log.warning("the GitHub login could not be deleted from the credential store (%s)", _describe(exc))
                    ok = False
            return ok

    def clear_if(self, expected: Tokens) -> bool:
        """Delete the login only while it still holds `expected`'s tokens: another CodeVision may have just
        saved a newer pair. True when the entry is gone afterwards."""
        with self._lock:
            backend = self._pick()
            try:
                blob = backend.get_password(SERVICE, USERNAME)
            except Exception as exc:
                log.warning("the credential store could not be read (%s)", _describe(exc))
                return False
            if blob is not None:
                try:
                    current = Tokens.from_json(blob)
                except (ValueError, TypeError, RecursionError):
                    current = None
                if current is not None and not same_pair(current, expected):
                    return False
            self._held = None
            try:
                backend.delete_password(SERVICE, USERNAME)
            except Exception as exc:
                log.warning("the GitHub login could not be deleted from the credential store (%s)", _describe(exc))
                return False
            return True

    def _delete_quietly(self, backend) -> None:
        if backend is None:
            return
        try:
            backend.delete_password(SERVICE, USERNAME)
        except Exception as exc:
            log.info("the GitHub login could not be deleted (%s)", _describe(exc))
