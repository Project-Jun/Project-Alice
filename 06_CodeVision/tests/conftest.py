"""Shared test setup.

No test ever reaches a real Ollama or starts the real program:
- CODEVISION_OLLAMA_URL points at a loopback port that is reserved but never listens,
  so "Ollama" looks stopped unless a test points it at a fake (tests/fake_ollama.py);
- CODEVISION_OLLAMA_EXE points at a file that does not exist, so Ollama looks missing
  unless a test points it at the fake's serve mode;
- connecting to port 11434 (the real Ollama's port) and running an "ollama" program both
  raise at once in this process. Child processes get the two variables above.
"""
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
DEMO = PROJECT / "src" / "codevision" / "demo" / "my-todo-app"
SAMPLE_GRAPH = PROJECT / "tests" / "fixtures" / "sample-graph.json"
FAKE_OLLAMA = PROJECT / "tests" / "fake_ollama.py"
REAL_OLLAMA_PORT = 11434

if str(PROJECT / "tests") not in sys.path:
    sys.path.insert(0, str(PROJECT / "tests"))  # so "import fake_ollama" works everywhere

# A port that refuses every connection for the whole session: bound, never listening.
_dead = socket.socket()
_dead.bind(("127.0.0.1", 0))
DEAD_OLLAMA_URL = f"http://127.0.0.1:{_dead.getsockname()[1]}"
os.environ["CODEVISION_OLLAMA_URL"] = DEAD_OLLAMA_URL
os.environ["CODEVISION_OLLAMA_EXE"] = str(PROJECT / "tests" / "no-such-ollama.exe")

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_popen_init = subprocess.Popen.__init__


def _real_ollama_address(address) -> bool:
    return (isinstance(address, tuple) and len(address) >= 2 and address[1] == REAL_OLLAMA_PORT
            and str(address[0]).lower() in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "::"))


def _guarded_connect(self, address):
    if _real_ollama_address(address):
        raise ConnectionRefusedError("tests never talk to the real Ollama (port 11434)")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if _real_ollama_address(address):
        raise ConnectionRefusedError("tests never talk to the real Ollama (port 11434)")
    return _real_connect_ex(self, address)


def _guarded_popen_init(self, args, *a, **kw):
    program = args if isinstance(args, (str, bytes, os.PathLike)) else (args[0] if args else "")
    name = os.path.basename(os.fsdecode(program)).lower()
    if name in ("ollama", "ollama.exe", "ollama app.exe") or (isinstance(args, str) and "ollama app" in args.lower()):
        raise RuntimeError(f"tests never start the real Ollama: {args!r}")
    return _real_popen_init(self, args, *a, **kw)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
subprocess.Popen.__init__ = _guarded_popen_init


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
