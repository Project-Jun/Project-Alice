"""Shared test setup.

No test ever reaches a real Ollama, a real GitHub, the internet or the real Credential Manager, or starts the
real program:
- CODEVISION_OLLAMA_URL points at a loopback port that is reserved but never listens,
  so "Ollama" looks stopped unless a test points it at a fake (tests/fake_ollama.py);
- CODEVISION_OLLAMA_EXE points at a file that does not exist, so Ollama looks missing
  unless a test points it at the fake's serve mode;
- connecting to port 11434 (the real Ollama's port) and running an "ollama" program both
  raise at once in this process. Child processes get the two variables above.
- CODEVISION_GITHUB_FAKE points at another dead loopback port, so "GitHub" is unreachable unless a test points
  it at tests/fake_github.py (the fake_github fixture); CODEVISION_KEYRING=memory and PYTHON_KEYRING_BACKEND=
  keyring.backends.fail.Keyring keep logins in memory, in child processes too;
- looking up a GitHub name, or connecting to any address that is not this computer, raises at once;
- win32cred's CredRead, CredWrite, CredDelete and CredEnumerate (keyring's Windows backend uses the same
  module) are stubs that raise, and a test that reaches them fails;
- every test gets a fresh in-memory token store (the autouse memory_keyring fixture);
- CODEVISION_HOME points at a scratch folder for the whole session (removed at the end), so a server or a
  snapshot store built without a home of its own never sweeps, prunes or writes the real data folder
  (%LOCALAPPDATA%\\CodeVision); a test whose data folder still resolves there fails (the autouse
  real_data_guard fixture).
"""
import atexit
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
DEMO = PROJECT / "src" / "codevision" / "demo" / "my-todo-app"
SAMPLE_GRAPH = PROJECT / "tests" / "fixtures" / "sample-graph.json"
FAKE_OLLAMA = PROJECT / "tests" / "fake_ollama.py"
FAKE_GITHUB = PROJECT / "tests" / "fake_github.py"
REAL_OLLAMA_PORT = 11434

if str(PROJECT / "tests") not in sys.path:
    sys.path.insert(0, str(PROJECT / "tests"))  # so "import fake_ollama" works everywhere

# A port that refuses every connection for the whole session: bound, never listening.
_dead = socket.socket()
_dead.bind(("127.0.0.1", 0))
DEAD_OLLAMA_URL = f"http://127.0.0.1:{_dead.getsockname()[1]}"
os.environ["CODEVISION_OLLAMA_URL"] = DEAD_OLLAMA_URL
os.environ["CODEVISION_OLLAMA_EXE"] = str(PROJECT / "tests" / "no-such-ollama.exe")

# The same for GitHub: a test that forgets the fake gets "connection refused", never GitHub.
_dead_github = socket.socket()
_dead_github.bind(("127.0.0.1", 0))
DEAD_GITHUB_URL = f"http://127.0.0.1:{_dead_github.getsockname()[1]}"
os.environ["CODEVISION_GITHUB_FAKE"] = DEAD_GITHUB_URL
os.environ["CODEVISION_KEYRING"] = "memory"
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
os.environ.pop("CODEVISION_GITHUB_REFRESH_SOON", None)

# The real data folders, as explain.data_dir() would find them without the line after this (the user's own
# CODEVISION_HOME, %LOCALAPPDATA%\CodeVision, ~/.codevision), then the session's scratch folder in their place.
REAL_DATA_DIRS = [Path(p) for p in (os.environ.get("CODEVISION_HOME"),
                                    os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"],
                                                                                   "CodeVision"),
                                    os.path.join(os.path.expanduser("~"), ".codevision")) if p]
SESSION_HOME = Path(tempfile.mkdtemp(prefix="codevision-tests-"))
os.environ["CODEVISION_HOME"] = str(SESSION_HOME)
atexit.register(shutil.rmtree, SESSION_HOME, True)


def is_real_data_dir(path) -> bool:
    """True for a real data folder or anything inside one."""
    try:
        here = os.path.normcase(os.path.realpath(os.fspath(path)))
    except (TypeError, ValueError, OSError):
        return False
    for real in REAL_DATA_DIRS:
        top = os.path.normcase(os.path.realpath(real))
        if here == top or here.startswith(top.rstrip("\\/") + os.sep):
            return True
    return False

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_popen_init = subprocess.Popen.__init__
_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex

GITHUB_NAMES = ("github.com", "api.github.com", "codeload.github.com",
                "githubusercontent.com", "githubassets.com", "github.io")
GITHUB_SUFFIXES = (".github.com", ".githubusercontent.com", ".githubassets.com", ".github.io")


def _real_ollama_address(address) -> bool:
    return (isinstance(address, tuple) and len(address) >= 2 and address[1] == REAL_OLLAMA_PORT
            and str(address[0]).lower() in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "::"))


def is_github_name(host) -> bool:
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip().rstrip(".").lower()
    return name in GITHUB_NAMES or name.endswith(GITHUB_SUFFIXES)


def is_local_address(address) -> bool:
    """True for loopback (and the unspecified address, which means this computer); a name only as localhost."""
    if not isinstance(address, tuple) or len(address) < 2:
        return True  # AF_UNIX paths and the like
    host = address[0]
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    host = str(host).split("%", 1)[0].strip("[]").lower()
    if host in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # any other name: never resolved behind the guards' back
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or ip.is_unspecified or bool(mapped and mapped.is_loopback)


def _guarded_connect(self, address):
    if _real_ollama_address(address):
        raise ConnectionRefusedError("tests never talk to the real Ollama (port 11434)")
    if not is_local_address(address):
        raise ConnectionRefusedError("tests never connect to other computers")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if _real_ollama_address(address):
        raise ConnectionRefusedError("tests never talk to the real Ollama (port 11434)")
    if not is_local_address(address):
        raise ConnectionRefusedError("tests never connect to other computers")
    return _real_connect_ex(self, address)


def _guarded_popen_init(self, args, *a, **kw):
    program = args if isinstance(args, (str, bytes, os.PathLike)) else (args[0] if args else "")
    name = os.path.basename(os.fsdecode(program)).lower()
    if name in ("ollama", "ollama.exe", "ollama app.exe") or (isinstance(args, str) and "ollama app" in args.lower()):
        raise RuntimeError(f"tests never start the real Ollama: {args!r}")
    return _real_popen_init(self, args, *a, **kw)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if is_github_name(host):
        raise socket.gaierror(socket.EAI_NONAME, "tests never look up GitHub")
    return _real_getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host):
    if is_github_name(host):
        raise socket.gaierror(socket.EAI_NONAME, "tests never look up GitHub")
    return _real_gethostbyname(host)


def _guarded_gethostbyname_ex(host):
    if is_github_name(host):
        raise socket.gaierror(socket.EAI_NONAME, "tests never look up GitHub")
    return _real_gethostbyname_ex(host)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
subprocess.Popen.__init__ = _guarded_popen_init
socket.getaddrinfo = _guarded_getaddrinfo
socket.gethostbyname = _guarded_gethostbyname
socket.gethostbyname_ex = _guarded_gethostbyname_ex


# --- the real Windows Credential Manager is never touched -------------------------------------------------

REAL_VAULT_CALLS: list[str] = []  # stub calls during the current test (the memory_keyring fixture checks it)


class RealVaultBlocked(RuntimeError):
    """Tests never read or write the real Windows Credential Manager."""


def _vault_stub(name: str):
    def stub(*args, **kwargs):
        REAL_VAULT_CALLS.append(name)
        raise RealVaultBlocked(f"tests never call win32cred.{name}")

    stub.__name__ = name
    return stub


try:
    from win32ctypes.pywin32 import win32cred as _win32cred  # keyring's Windows backend uses this module too
except ImportError:  # not Windows, or pywin32-ctypes missing
    _win32cred = None
if _win32cred is not None:
    for _name in ("CredRead", "CredWrite", "CredDelete", "CredEnumerate"):
        setattr(_win32cred, _name, _vault_stub(_name))


@pytest.fixture(autouse=True)
def memory_keyring():
    """A fresh in-memory token store for this test (yields the backend: entries(), writes, fail). A test that
    reaches the real Credential Manager's functions fails."""
    from codevision.github import store

    backend = store.MemoryBackend()
    store.use_backend(backend)
    store.memory_backend().clear()
    del REAL_VAULT_CALLS[:]
    try:
        yield backend
    finally:
        store.use_backend(None)
        store.memory_backend().clear()
    reached = list(REAL_VAULT_CALLS)
    del REAL_VAULT_CALLS[:]
    if reached:
        pytest.fail(f"the test reached the real Credential Manager: {reached}")


REAL_DATA_CALLS: list[str] = []  # data folders under a real one asked for during the current test


@pytest.fixture(autouse=True)
def real_data_guard(monkeypatch):
    """explain.data_dir(), wherever it is used (the server's home, the AI cache and settings, the recent list,
    the GitHub snapshots), refuses a real data folder: a test that unsets CODEVISION_HOME and then builds a
    server fails instead of sweeping %LOCALAPPDATA%\\CodeVision."""
    from codevision import explain
    from codevision.explain import base, cache, providers

    real = base.data_dir

    def guarded():
        path = real()
        if is_real_data_dir(path):
            REAL_DATA_CALLS.append(str(path))
            raise RuntimeError("tests never use the real CodeVision data folder")
        return path

    for module in (base, cache, providers, explain):
        monkeypatch.setattr(module, "data_dir", guarded)
    del REAL_DATA_CALLS[:]
    yield
    reached = list(REAL_DATA_CALLS)
    del REAL_DATA_CALLS[:]
    if reached:
        pytest.fail(f"the test reached the real data folder: {reached}")


@pytest.fixture
def demo_root() -> Path:
    return DEMO


@pytest.fixture
def fake_ollama(monkeypatch):
    """A running fake Ollama (qwen2.5:7b and gemma3:4b) that CodeVision is pointed at."""
    from fake_ollama import FakeOllama

    fake = FakeOllama(models=["qwen2.5:7b", "gemma3:4b"]).start()
    monkeypatch.setenv("CODEVISION_OLLAMA_URL", fake.url)
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def fake_github(monkeypatch):
    """A running fake GitHub that CodeVision is pointed at (CODEVISION_GITHUB_FAKE), with device-flow polls
    every 0.05 s so logins finish quickly. Add repos with fake_github.add_repo(...)."""
    from fake_github import FakeGitHub

    fake = FakeGitHub(interval=0.05, slow_step=0.05).start()
    monkeypatch.setenv("CODEVISION_GITHUB_FAKE", fake.url)
    try:
        yield fake
    finally:
        fake.stop()
