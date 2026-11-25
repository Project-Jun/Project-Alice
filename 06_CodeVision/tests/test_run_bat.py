"""run.bat's install marker (decisions.md, M5), in a scratch copy whose Python calls go to a stub batch file:
no pip, no server and no window, only cmd's own if/else, fc, del and copy.

- a first install (no venv, or no marker) must succeed, else run.bat stops;
- a changed requirements.txt installs again; when that fails, it warns, keeps the old marker and starts anyway;
- an up-to-date marker skips pip;
- a venv made again (its python.exe was gone) installs even when an old marker still matches.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="run.bat is for Windows")

ROOT = Path(__file__).resolve().parent.parent
STUB = (
    "@echo off\r\n"
    "if \"%2\"==\"pip\" (\r\n"
    "    echo PIP_CALLED>> \"%~dp0calls.txt\"\r\n"
    "    exit /b %PIP_RESULT%\r\n"
    ")\r\n"
    "if \"%2\"==\"codevision\" (\r\n"
    "    echo STARTED>> \"%~dp0calls.txt\"\r\n"
    "    exit /b 0\r\n"
    ")\r\n"
    "exit /b 3\r\n"
)


def prepare(work: Path, marker: bytes | None, venv: bool = True) -> Path:
    text = (ROOT / "run.bat").read_bytes().decode("utf-8")
    for old, new in [('".venv\\Scripts\\python.exe" -m', 'call "%~dp0fakepy.bat" -m'),
                     ("py -3 -m venv .venv 2> nul",
                      'mkdir .venv\\Scripts 2> nul & type nul > ".venv\\Scripts\\python.exe"'),
                     ("\r\npause\r\n", "\r\n")]:
        assert old in text
        text = text.replace(old, new)
    work.mkdir()
    (work / "run.bat").write_bytes(text.encode("utf-8"))
    (work / "fakepy.bat").write_bytes(STUB.encode("ascii"))
    shutil.copy(ROOT / "requirements.txt", work / "requirements.txt")
    if venv:
        (work / ".venv" / "Scripts").mkdir(parents=True)
        (work / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    if marker is not None:
        (work / ".venv").mkdir(exist_ok=True)
        (work / ".venv" / "codevision-ready.txt").write_bytes(marker)
    return work


def run(work: Path, pip_result: int):
    env = dict(os.environ, PIP_RESULT=str(pip_result))
    proc = subprocess.run(["cmd", "/d", "/c", str(work / "run.bat")], cwd=work, env=env, capture_output=True,
                          timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                          stdin=subprocess.DEVNULL)
    calls = (work / "calls.txt").read_text().split() if (work / "calls.txt").exists() else []
    marker = work / ".venv" / "codevision-ready.txt"
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), calls, marker.read_bytes() if marker.exists() else None


REQ = (ROOT / "requirements.txt").read_bytes()


@pytest.mark.parametrize("name, marker, venv, pip, code, calls, kept", [
    ("first install fails", None, False, 1, 1, ["PIP_CALLED"], None),
    ("no marker fails", None, True, 1, 1, ["PIP_CALLED"], None),
    ("first install works", None, True, 0, 0, ["PIP_CALLED", "STARTED"], REQ),
    ("update fails: starts anyway", b"ok\r\n", True, 1, 0, ["PIP_CALLED", "STARTED"], b"ok\r\n"),
    ("update works", b"ok\r\n", True, 0, 0, ["PIP_CALLED", "STARTED"], REQ),
    ("up to date: no pip", REQ, True, 1, 0, ["STARTED"], REQ),
    # python.exe was gone (deleted, quarantined) but the marker matched: the new venv still gets its packages
    ("venv made again fails", REQ, False, 1, 1, ["PIP_CALLED"], None),
    ("venv made again works", REQ, False, 0, 0, ["PIP_CALLED", "STARTED"], REQ),
])
def test_the_install_marker(tmp_path, name, marker, venv, pip, code, calls, kept):
    work = prepare(tmp_path / "app", marker, venv)
    got_code, out, got_calls, got_marker = run(work, pip)
    assert (got_code, got_calls, got_marker) == (code, calls, kept), out
    if code:
        assert "설치하지 못했어요" in out
    elif pip and calls[0] == "PIP_CALLED":
        assert "그대로 켜요" in out
