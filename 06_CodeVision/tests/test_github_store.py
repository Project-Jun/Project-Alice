"""github/store.py: the token store, against an in-memory backend and a fake win32cred (never the real vault).

- one generic credential 'CodeVision/github' / 'oauth-tokens', persisted for this machine, overwritten in place;
- a leftover keyring-style 'oauth-tokens@CodeVision/github' copy is removed;
- the 2,560-byte guard keeps only the refresh token; anything else that cannot be stored means memory + a note;
- an old login or one for another app counts as logged out and is deleted;
- memory only under CODEVISION_KEYRING=memory, in fake mode and under CODEVISION_HOME (unless =os);
- tokens are Secret objects: never printed, serialized, pickled or copied.
"""
import copy
import json
import logging
import os
import pickle
import time

import pytest

import conftest
from codevision.github import messages as M
from codevision.github import store
from codevision.github.base import CLIENT_ID, Secret
from codevision.github.store import (COMPOUND, MAX_BLOB_BYTES, SERVICE, USERNAME, MemoryBackend, Tokens,
                                     TokenStore, WinCredBackend)

ACCESS = "ghu_" + "A" * 36
REFRESH = "ghr_" + "R" * 76


class FakeWinCred:
    """A stand-in for win32ctypes.pywin32.win32cred: CredRead / CredWrite / CredDelete on a dict, with the
    real limits (a blob of at most 2,560 bytes of UTF-16) and the real error numbers."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_SESSION = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    CRED_PERSIST_ENTERPRISE = 3

    class error(Exception):
        def __init__(self, winerror, funcname, strerror):
            super().__init__(winerror, funcname, strerror)
            self.winerror, self.funcname, self.strerror = winerror, funcname, strerror

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}  # function name -> winerror it raises

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise self.error(self.fail[name], name, "fake failure")

    def CredRead(self, TargetName, Type, Flags=0):
        self._maybe_fail("CredRead")
        item = self.items.get(TargetName.lower())
        if item is None:
            raise self.error(1168, "CredRead", "Element not found.")
        return dict(item, CredentialBlob=item["CredentialBlob"].encode("utf-16-le"))

    def CredWrite(self, Credential, Flags=0):
        self._maybe_fail("CredWrite")
        if len(Credential["CredentialBlob"].encode("utf-16-le")) > 2560:
            raise self.error(1783, "CredWrite", "The stub received bad data.")
        self.items[Credential["TargetName"].lower()] = dict(Credential)

    def CredDelete(self, TargetName, Type, Flags=0):
        self._maybe_fail("CredDelete")
        if self.items.pop(TargetName.lower(), None) is None:
            raise self.error(1168, "CredDelete", "Element not found.")


def tokens(access=ACCESS, refresh=REFRESH, *, now=None, login="octocat", client_id=CLIENT_ID, access_in=28800,
           refresh_in=15897600):
    now = time.time() if now is None else now
    return Tokens(Secret(access) if access else None, now + access_in if access else None,
                  Secret(refresh) if refresh else None, now + refresh_in if refresh else None,
                  login, 583231 if login else 0, client_id, now)


def wincred_store():
    cred = FakeWinCred()
    return cred, TokenStore(backend=WinCredBackend(cred))


# --- the entry ---------------------------------------------------------------------------------------------------

def test_a_login_round_trips_through_the_memory_store(memory_keyring):
    s = TokenStore()
    assert s.load() is None and s.kind == "memory" and s.note == M.STORE_MEMORY
    s.save(tokens())
    loaded = s.load()
    assert loaded.access == Secret(ACCESS) and loaded.refresh == Secret(REFRESH) and loaded.login == "octocat"
    assert list(memory_keyring.entries()) == [(SERVICE, USERNAME)] == [("CodeVision/github", "oauth-tokens")]
    assert s.clear() is True and s.load() is None and memory_keyring.entries() == {}


def test_the_blob_is_compact_json_in_a_fixed_shape():
    t = tokens(now=1795560000.0)
    blob = t.to_json()
    assert " " not in blob and blob.isascii()
    data = json.loads(blob)
    assert list(data) == ["v", "client_id", "access_token", "access_expires_at", "refresh_token",
                          "refresh_expires_at", "login", "user_id", "saved_at"]
    assert data["v"] == 1 and data["client_id"] == CLIENT_ID and data["access_expires_at"] == 1795588800.0
    assert Tokens.from_json(blob) == t
    assert ACCESS not in repr(t) and REFRESH not in repr(t) and "Secret(***)" in repr(t)


def test_windows_writes_one_credential_in_place_for_this_machine_only():
    cred, s = wincred_store()
    s.save(tokens())
    s.save(tokens(access="ghu_" + "B" * 36))
    assert list(cred.items) == ["codevision/github"]
    item = cred.items["codevision/github"]
    assert (item["TargetName"], item["UserName"], item["Type"], item["Persist"]) == (
        "CodeVision/github", "oauth-tokens", 1, 2)  # CRED_PERSIST_LOCAL_MACHINE: never roams
    assert cred.calls.count("CredWrite") == 2 and "CredRead" not in cred.calls  # overwritten, never read first
    assert s.load().access == Secret("ghu_" + "B" * 36) and s.kind == "keyring" and s.note == ""


def test_a_leftover_keyring_copy_is_removed():
    cred, s = wincred_store()
    cred.items[COMPOUND.lower()] = {"TargetName": COMPOUND, "UserName": USERNAME, "CredentialBlob": "old"}
    s.save(tokens())
    assert list(cred.items) == ["codevision/github"]


def test_logout_deletes_both_names_and_a_missing_entry_is_fine():
    cred, s = wincred_store()
    s.save(tokens())
    cred.items[COMPOUND.lower()] = {"TargetName": COMPOUND, "UserName": USERNAME, "CredentialBlob": "old"}
    assert s.clear() is True and cred.items == {}
    assert s.clear() is True


def test_an_entry_under_another_user_name_is_not_ours():
    cred, s = wincred_store()
    cred.items["codevision/github"] = {"TargetName": SERVICE, "UserName": "someone-else",
                                       "CredentialBlob": tokens().to_json()}
    assert s.load() is None


# --- the size guard ------------------------------------------------------------------------------------------------

def test_a_blob_over_2560_bytes_keeps_only_the_refresh_token():
    cred, s = wincred_store()
    long_access = "ghu_" + "L" * 1500  # a future, longer token format
    t = tokens(access=long_access)
    assert len(t.to_json().encode("utf-16-le")) > MAX_BLOB_BYTES
    s.save(t)
    stored = json.loads(cred.items["codevision/github"]["CredentialBlob"])
    assert "access_token" not in stored and stored["refresh_token"] == REFRESH
    assert s.load().access == Secret(long_access)  # this run still has it in memory
    again = TokenStore(backend=WinCredBackend(cred)).load()  # the next run has only the refresh token
    assert again.access is None and again.refresh == Secret(REFRESH) and again.login == "octocat"


def test_a_login_too_big_even_without_the_access_token_stays_in_memory(caplog):
    cred, s = wincred_store()
    s.save(tokens(access="ghu_" + "L" * 1500, refresh="ghr_" + "M" * 1500))
    assert cred.items == {} and s.kind == "memory" and s.note == M.STORE_MEMORY
    assert s.load().refresh == Secret("ghr_" + "M" * 1500)
    assert "L" * 100 not in caplog.text and "M" * 100 not in caplog.text


# --- when the OS store fails ---------------------------------------------------------------------------------------

def test_a_failing_write_means_memory_for_this_run_with_a_note(caplog):
    caplog.set_level(logging.DEBUG)
    cred, s = wincred_store()
    cred.fail["CredWrite"] = 5  # access denied
    s.save(tokens())
    assert s.kind == "memory" and s.note == M.STORE_MEMORY
    assert s.load().access == Secret(ACCESS)
    assert "winerror 5" in caplog.text and "error" in caplog.text
    assert ACCESS not in caplog.text and REFRESH not in caplog.text


def test_a_failing_read_means_memory(caplog):
    cred, s = wincred_store()
    cred.fail["CredRead"] = 1312  # no logon session
    assert s.load() is None and s.kind == "memory"
    assert "winerror 1312" in caplog.text


def test_a_failing_delete_is_reported_but_memory_is_cleared():
    cred, s = wincred_store()
    s.save(tokens())
    cred.fail["CredDelete"] = 5
    assert s.clear() is False  # the page then asks the user to delete 'CodeVision/github' by hand
    assert M.LOGOUT_STORE_FAILED.count("CodeVision/github") == 1


@pytest.mark.skipif(os.name != "nt", reason="the Windows store")
def test_without_win32ctypes_the_login_stays_in_memory(monkeypatch):
    store.use_backend(None)
    monkeypatch.setenv("CODEVISION_KEYRING", "os")
    monkeypatch.delenv("CODEVISION_HOME", raising=False)

    def missing():
        raise ImportError("No module named 'win32ctypes'")

    monkeypatch.setattr(store, "_load_win32cred", missing)
    s = TokenStore()
    assert s.kind == "memory" and s.note == M.STORE_MEMORY
    s.save(tokens())
    assert s.load().refresh == Secret(REFRESH)


# --- old logins ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("old", [
    tokens(client_id="Iv1.someotherapp"),
    tokens(now=time.time() - 16_000_000),          # both tokens long expired
    tokens(access=None, now=time.time() - 16_000_000),
])
def test_an_old_login_or_one_for_another_app_counts_as_logged_out(memory_keyring, old):
    memory_keyring.set_password(SERVICE, USERNAME, old.to_json())
    s = TokenStore()
    assert s.load() is None and s.stale is True
    assert memory_keyring.entries() == {}  # and it is deleted


@pytest.mark.parametrize("blob", ["not json", "[]", '{"v": 2}', '{"v": 1, "client_id": "x"}',
                                  '{"v": 1, "client_id": "x", "access_token": "short"}'])
def test_an_unreadable_entry_is_removed(memory_keyring, blob):
    memory_keyring.set_password(SERVICE, USERNAME, blob)
    s = TokenStore()
    assert s.load() is None and s.stale is False and memory_keyring.entries() == {}


def test_clear_if_keeps_a_pair_someone_else_saved_meanwhile(memory_keyring):
    s = TokenStore()
    ours, theirs = tokens(), tokens(access="ghu_" + "N" * 36, refresh="ghr_" + "N" * 76)
    s.save(theirs)
    assert s.clear_if(ours) is False and s.load().refresh == theirs.refresh
    assert s.clear_if(theirs) is True and memory_keyring.entries() == {}


# --- which store --------------------------------------------------------------------------------------------------------

def test_codevision_keyring_memory_never_calls_win32cred(monkeypatch):
    store.use_backend(None)
    monkeypatch.setenv("CODEVISION_KEYRING", "memory")
    s = TokenStore()
    s.save(tokens())
    assert s.load().access == Secret(ACCESS) and s.clear()
    assert s.kind == "memory" and conftest.REAL_VAULT_CALLS == []
    assert store.choose_backend() is store.memory_backend()


def test_the_win32cred_guard_is_in_place():
    pytest.importorskip("win32ctypes")
    from win32ctypes.pywin32 import win32cred

    with pytest.raises(conftest.RealVaultBlocked):
        win32cred.CredRead(TargetName=SERVICE, Type=1)
    assert conftest.REAL_VAULT_CALLS == ["CredRead"]
    del conftest.REAL_VAULT_CALLS[:]  # the call was this test's own


def test_fake_mode_always_uses_memory(monkeypatch):
    store.use_backend(None)
    monkeypatch.setenv("CODEVISION_KEYRING", "os")
    monkeypatch.delenv("CODEVISION_HOME", raising=False)
    s = TokenStore(fake=True)
    assert s.kind == "memory" and s.note == M.FAKE_MODE
    assert store.choose_backend(fake=True) is store.memory_backend()


def test_codevision_home_means_memory_unless_keyring_os(monkeypatch, tmp_path):
    store.use_backend(None)
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    monkeypatch.delenv("CODEVISION_KEYRING")
    assert store.choose_backend() is store.memory_backend()
    if os.name == "nt":
        monkeypatch.setenv("CODEVISION_KEYRING", "os")
        assert isinstance(store.choose_backend(), WinCredBackend)  # made, but never called here


def test_the_store_never_writes_a_file(monkeypatch, tmp_path):
    store.use_backend(None)
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    s = TokenStore()
    s.save(tokens())
    s.load()
    s.clear()
    assert list(tmp_path.iterdir()) == []


def test_the_store_is_chosen_lazily():
    s = TokenStore()
    assert s._backend is None  # constructing it reads nothing
    s.kind
    assert s._backend is not None


# --- Secret ---------------------------------------------------------------------------------------------------------------

def test_a_secret_never_shows_or_serializes_itself():
    secret = Secret(ACCESS)
    assert repr(secret) == str(secret) == f"{secret}" == "Secret(***)"
    assert secret == Secret(ACCESS) and secret != Secret(REFRESH) and secret.reveal() == ACCESS
    with pytest.raises(TypeError):
        json.dumps({"token": secret})
    for clone in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            clone(secret)
    with pytest.raises(TypeError):
        hash(secret)
    with pytest.raises(TypeError):
        Secret(b"bytes")
    assert ACCESS not in "%s %r" % (secret, secret)


def test_the_memory_backend_is_not_a_keyring_backend():
    import keyring.backend

    assert not isinstance(MemoryBackend(), keyring.backend.KeyringBackend)
    assert MemoryBackend not in keyring.backend.KeyringBackend._classes
