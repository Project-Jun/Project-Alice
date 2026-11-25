#!/usr/bin/env python3
"""A fake GitHub for CodeVision tests and screen checks. No real GitHub, no internet, no real tokens.

One loopback server stands in for three hosts, under three prefixes that match github.base.Hosts in fake mode:
    /web       github.com: the device flow (/login/device/code, /login/oauth/access_token) and tiny HTML
               pages for typing the code, installing the app and revoking it
    /api       api.github.com: /user, /user/installations, /repos/... (branches, commits/heads, tarball)
    /codeload  codeload.github.com: the tarballs, built in memory with make_tarball()
It answers the way GitHub does: OAuth errors come as HTTP 200 with an "error" field (form-encoded unless the
request asks for JSON), every refresh rotates both tokens, private repos are 404 without a login, a renamed repo
answers 301, the tarball is a 302 to codeload (with a ?token= for private repos). Only the standard library is
used, and it only ever listens on a loopback address (never on the user's ports 8799, 8800 and 11434).

From a test (the fake_github fixture in conftest.py does this and points CODEVISION_GITHUB_FAKE at it):

    from fake_github import FakeGitHub, make_tarball, HOSTILE
    with FakeGitHub(interval=0.05) as fake:
        fake.add_repo("octocat", "Hello-World", {"README.md": "# hi"})
        fake.approve()                       # the next device-flow poll succeeds
        ...  fake.url, fake.calls(), fake.issued_tokens(), fake.hold(), fake.release()

Run it on its own for the browser checks (stop it with Ctrl+C or POST /__fake/stop):

    python tests/fake_github.py --port 8810 [--demo] [--approve-after 3] [--interval 2]
    set CODEVISION_GITHUB_FAKE=http://127.0.0.1:8810

Control endpoints (JSON; only reachable from this computer):
    GET /__fake/calls      every request so far: [{"method", "path", "query", "headers", "form", "at"}]
    POST /__fake/reset     forget calls and failure settings
    POST /__fake/set       {"fail": "500", "fail_count": 1, "rate": "primary", "chunked": true, ...} (see set())
    POST /__fake/approve   {"login": "octocat"}; /__fake/deny; /__fake/expire
    POST /__fake/push      {"owner", "repo", "branch"}: a new commit on that branch
    POST /__fake/delete_branch {"owner", "repo", "branch"}: that branch is gone
    POST /__fake/script    {"steps": ["slow_down", "pending", ...]}: answers for the next device-flow polls
    POST /__fake/expire_tokens, /__fake/revoke, /__fake/hold {"target"}, /__fake/release, /__fake/stop

Fail modes (set(fail=...), for "web", "api", "codeload" or "all" requests, fail_count of them or all):
"500" answers HTTP 500, "badjson" answers 200 with broken JSON, "slow" waits `slow` seconds first, "drop" closes
the connection without a reply, "trickle" sends the reply one byte at a time, "cut" (codeload) closes the
tarball halfway. set(chunked=True) sends tarballs without Content-Length.
"""
from __future__ import annotations

import argparse
import functools
import gzip
import hashlib
import html
import io
import json
import os
import secrets
import socket
import socketserver
import string
import sys
import tarfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIENT_ID = "Iv23liGLsLW4iMJ8LI4g"
APP_SLUG = "codevision-project-jun"
APP_NAME = "CodeVision-Project-Jun"
DEFAULT_LOGIN = "octocat"
DEFAULT_USER_ID = 583231
ACCESS_TTL = 28800
REFRESH_TTL = 15897600
API_VERSIONS = ("2022-11-28", "2026-03-10")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
RESERVED_PORTS = (8799, 8800, 11434)  # the user's own servers and Ollama
FAIL_MODES = ("none", "500", "badjson", "slow", "drop", "trickle", "cut")
FAIL_TARGETS = ("all", "web", "api", "codeload")
RATE_MODES = (None, "primary", "secondary", "secondary403")
MAX_BODY = 1_000_000
MTIME = 1795560000  # 2026-11-25: every tar member's time, so tarballs are the same bytes every run
_ALNUM = string.ascii_letters + string.digits
_CODE_CHARS = "BCDFGHJKLMNPQRSTVWXZ0123456789"


def _rand(n: int, chars: str = _ALNUM) -> str:
    return "".join(secrets.choice(chars) for _ in range(n))


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_sha(*parts) -> str:
    """A stable 40-hex commit id for a name."""
    return hashlib.sha1(":".join(str(p) for p in parts).encode("utf-8", "surrogatepass")).hexdigest()


# --- tarballs ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Member:
    """One special tar member. name is inside the top folder unless raw (then it is the whole archive name).

    kind: file | dir | symlink | hardlink | chr | blk | fifo. link: the target (for a hardlink a path inside the
    top folder, unless raw). size: the header size when it differs from len(data) (only that many bytes are
    stored). zeros: a file of this many zero bytes, streamed while the archive is built (a compression bomb).
    """

    name: str
    kind: str = "file"
    data: bytes = b""
    link: str = ""
    raw: bool = False
    size: int | None = None
    zeros: int = 0


class _Zeros(io.RawIOBase):
    def __init__(self, n: int):
        self.left = n

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        n = min(len(buf), self.left)
        buf[:n] = bytes(n)
        self.left -= n
        return n


_KINDS = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
          "chr": tarfile.CHRTYPE, "blk": tarfile.BLKTYPE, "fifo": tarfile.FIFOTYPE}


def _git_order(paths) -> list[tuple[str, bool]]:
    """(path, is_dir) for every file and folder, in the order git archive writes them: each folder right before
    its contents, names in byte order with folders compared as "name/"."""
    root: dict = {}
    for path in paths:
        is_dir = path.endswith("/")
        parts = path.strip("/").split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        leaf = parts[-1] + ("/" if is_dir else "")
        if is_dir:
            node.setdefault(leaf, {})
        else:
            node[leaf] = None
    out: list[tuple[str, bool]] = []

    def walk(node: dict, prefix: str) -> None:
        for name in sorted(node, key=lambda n: n.encode("utf-8", "surrogateescape")):
            if name.endswith("/"):
                out.append((prefix + name[:-1], True))
                walk(node[name], prefix + name)
            else:
                out.append((prefix + name, False))

    walk(root, "")
    return out


def _as_bytes(data) -> bytes:
    if data is None:
        return b""
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def make_tarball(files=None, top: str = "octocat-Hello-World-0000000", comment: str | None = None, extra=(), *,
                 compresslevel: int = 6) -> bytes:
    """A gzip tarball shaped like GitHub's (git archive): a pax global header whose 'comment' is the commit id,
    the top folder first, then every folder right before its contents.

    files: {path: bytes | str} (or (path, data) pairs) inside the top folder; a path ending in "/" is an empty
    folder (a submodule looks like that). extra: Member objects, written after the files (see HOSTILE)."""
    items = list(files.items()) if isinstance(files, dict) else list(files or [])
    data_of = {path.rstrip("/") if path.endswith("/") else path: data for path, data in items}
    buf = io.BytesIO()
    gz = gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=compresslevel, mtime=0)
    pax = {"comment": comment} if comment else None
    with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT, pax_headers=pax) as tf:
        tf.addfile(_info(top + "/", tarfile.DIRTYPE))
        for path, is_dir in _git_order(path for path, _ in items):
            if is_dir:
                tf.addfile(_info(f"{top}/{path}/", tarfile.DIRTYPE))
            else:
                body = _as_bytes(data_of.get(path))
                tf.addfile(_info(f"{top}/{path}", tarfile.REGTYPE, len(body)), io.BytesIO(body))
        for member in extra:
            _add_member(tf, member, top)
    gz.close()
    return buf.getvalue()


def _info(name: str, kind, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size if kind == tarfile.REGTYPE else 0
    info.mode = 0o775 if kind == tarfile.DIRTYPE else 0o664
    info.mtime = MTIME
    info.uname = info.gname = "root"
    return info


def _add_member(tf: tarfile.TarFile, member: Member, top: str) -> None:
    name = member.name if member.raw else f"{top}/{member.name}"
    kind = _KINDS[member.kind]
    info = _info(name + ("/" if member.kind == "dir" and not name.endswith("/") else ""), kind)
    if member.kind in ("symlink", "hardlink"):
        info.linkname = member.link if (member.raw or member.kind == "symlink") else f"{top}/{member.link}"
    if member.kind in ("chr", "blk"):
        info.devmajor, info.devminor = 1, 3
    if member.kind != "file":
        tf.addfile(info)
        return
    if member.zeros:
        info.size = member.zeros
        tf.addfile(info, _Zeros(member.zeros))
        return
    info.size = len(member.data) if member.size is None else member.size
    tf.addfile(info, io.BytesIO(member.data))


# File contents the extractor treats specially.
LFS_POINTER = (b"version https://git-lfs.github.com/spec/v1\n"
               b"oid sha256:4665a5ea423c2713d436b5ee50593a9640e0018c1550b5a0002f74190d6caea8\n"
               b"size 13426789\n")
LFS_POINTER_OLD = (b"version https://hawser.github.com/spec/v1\n"
                   b"oid sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824\nsize 5\n")
PNG_BYTES = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                          "1f15c4890000000d49444154789c6360000002000154a24f5f0000000049454e44ae426082")
BINARY_NUL = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + bytes(range(256)) * 64  # a NUL early on, unknown extension
GITMODULES = ('[submodule "libs/sub"]\n\tpath = libs/sub\n\turl = https://github.com/octocat/sub.git\n'
              '[submodule "vendor/only/inner"]\n\tpath = vendor/only/inner\n\turl = https://github.com/octocat/in.git\n')

# The hostile member set: names Windows cannot hold, ways out of the folder, links and special files,
# collisions by case, 8.3 name, Unicode form, and too-long paths. (The bomb is separate: bomb().)
HOSTILE = (
    Member("../escape.txt", data=b"escape"),            # top/../escape.txt
    Member("../outside.txt", raw=True, data=b"out"),     # outside the top folder altogether
    Member("/abs.txt", raw=True, data=b"abs"),
    Member("C:/abs.txt", raw=True, data=b"abs"),
    Member("D:rel.txt", raw=True, data=b"rel"),
    Member("CON", data=b"con"),
    Member("aux.txt", data=b"aux"),
    Member("nul.md", data=b"nul"),
    Member("COM1", data=b"com"),
    Member("COM\u00b9", data=b"com-sup"),
    Member("sub/aux/x.txt", data=b"aux folder"),
    Member("trail.", data=b"dot"),
    Member("space ", data=b"space"),
    Member("colon:.txt", data=b"colon"),
    Member("ads.txt:stream", data=b"ads"),
    Member("back\\slash.txt", data=b"backslash"),
    Member("q?.txt", data=b"question"),
    Member("ctl\x01.txt", data=b"control"),
    Member("lone\udcff.txt", data=b"surrogate"),
    Member("link-in", kind="symlink", link="README.md"),
    Member("link-out", kind="symlink", link="../../outside"),
    Member("link-abs", kind="symlink", link="/etc/passwd"),
    Member("hard", kind="hardlink", link="README.md"),
    Member("dev-null", kind="chr"),
    Member("pipe", kind="fifo"),
    Member("README.md", data=b"# first"),
    Member("readme.md", data=b"# second, same name but for case"),
    Member("Docs/a.md", data=b"a"),
    Member("docs/b.md", data=b"b"),
    Member("longfilename.txt", data=b"long"),
    Member("LONGFI~1.TXT", data=b"short alias"),
    Member("caf\u00e9.txt", data=b"nfc"),
    Member("cafe\u0301.txt", data=b"nfd"),
    Member("deep/" + "/".join(["d" * 50] * 5) + "/" + "f" * 40 + ".txt", data=b"300+ characters"),
    Member("other-top-0000000/x.txt", raw=True, data=b"a second top folder"),
    Member("longer-than-header.txt", data=b"0123456789ABCDEF", size=5),
)


def bomb(name: str = "bomb.txt", size: int = 50 * 1024 * 1024) -> Member:
    """A file of `size` zero bytes: about 50 KB compressed for the default 50 MiB."""
    return Member(name, zeros=size)


def node_modules_files(count: int = 60_000, root: str = "node_modules") -> dict[str, bytes]:
    """A committed node_modules folder with `count` small files in 50-file packages."""
    return {f"{root}/pkg{i // 50:04d}/lib/f{i:05d}.js": b"module.exports = 1;\n" for i in range(count)}


def committed_venv_files(root: str = ".venv") -> dict[str, bytes]:
    """A committed virtual environment (scan.py hides it): about 300 files."""
    files = {f"{root}/pyvenv.cfg": b"home = C:\\Python314\nversion = 3.14.3\n",
             f"{root}/Scripts/python.exe": BINARY_NUL}
    for i in range(300):
        files[f"{root}/Lib/site-packages/pkg{i // 30}/mod{i}.py"] = b"x = 1\n"
    return files


def submodule_layout() -> tuple[dict, tuple]:
    """(files, extra) for a repo with two submodules: .gitmodules plus the empty folder members git archive
    writes for them (one whose parent holds nothing else)."""
    files = {".gitmodules": GITMODULES, "README.md": "# with submodules\n", "libs/own.py": "x = 1\n"}
    return files, (Member("libs/sub", kind="dir"), Member("vendor/only/inner", kind="dir"))


def long_name_files() -> dict[str, bytes]:
    """Paths around Windows' limits: a 255-character name, a 256-character one, and deep paths."""
    return {"n" * 251 + ".txt": b"255", "m" * 252 + ".txt": b"256", "a/" * 60 + "deep.txt": b"deep",
            "/".join(["x" * 100] * 3) + "/f.txt": b"long"}


RESERVED_NAME_FILES = {"CON.txt": b"x", "prn": b"x", "Aux.c": b"x", "nul": b"x", "com9.log": b"x", "LPT1": b"x",
                       "CONIN$": b"x", "conout$.txt": b"x", "lpt\u00b2": b"x", "ok.txt": b"fine"}
CASE_COLLISION_FILES = {"README.md": b"one", "readme.md": b"two", "Src/App.py": b"a", "src/app.py": b"b",
                        "Makefile": b"m", "MAKEFILE": b"M"}


@functools.lru_cache(maxsize=4)
def node_modules_tarball(count: int = 60_000, top: str = "octocat-deps-0000000", comment: str | None = None) -> bytes:
    """A tarball with a `count`-file node_modules plus a little real code (built once per process)."""
    files = node_modules_files(count)
    files.update({"package.json": b'{"name": "deps"}\n', "src/index.js": b"console.log(1);\n"})
    return make_tarball(files, top=top, comment=comment, compresslevel=1)


# --- the fake's data -------------------------------------------------------------------------------------------

@dataclass
class _Repo:
    id: int
    owner: str
    name: str
    private: bool
    default_branch: str
    branches: dict  # name -> sha
    trees: dict     # sha -> {path: bytes}
    size_kb: int
    archived: bool
    pushed_at: str
    empty: bool = False
    hostile: bool = False
    sso: bool = False
    not_allowed: bool = False
    collaborators: set = field(default_factory=set)
    extra: tuple = ()
    comment: str | None = None  # a pax comment that is not the commit (commit_mismatch)
    description: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class _Installation:
    id: int
    account: str
    account_type: str
    suspended: bool = False
    selection: str = "all"
    repos: set = field(default_factory=set)    # lowercase full names when selection is "selected"
    members: set = field(default_factory=set)  # lowercase logins (besides the account itself) who see it


class _Drop(Exception):
    """Close the connection without any reply."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:  # skip the reverse DNS lookup of HTTPServer.server_bind
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Server6(_Server):
    address_family = socket.AF_INET6


class FakeGitHub:
    """A fake github.com + api.github.com + codeload.github.com on 127.0.0.1. Thread-safe; every setting can
    change while it runs. interval and expires_in may be fractions of a second so tests run quickly."""

    def __init__(self, port: int = 0, *, installed: bool = True, interval: float = 5, expires_in: float = 900,
                 host: str = "127.0.0.1", login: str = DEFAULT_LOGIN, user_id: int = DEFAULT_USER_ID,
                 slow_step: float = 5, access_ttl: float = ACCESS_TTL, refresh_ttl: float = REFRESH_TTL,
                 approve_after: float | None = None, log_path: str | None = None):
        if host not in LOOPBACK_HOSTS:
            raise ValueError("the fake GitHub only listens on 127.0.0.1, localhost or ::1")
        if port in RESERVED_PORTS:
            raise ValueError(f"port {port} belongs to the user; pick another port")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = port
        self.interval = interval
        self.expires_in = expires_in
        self.slow_step = slow_step
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self.approve_after = approve_after
        self.log_path = log_path
        self.default_login = login
        self._lock = threading.RLock()
        self._users: dict[str, dict] = {}
        self._orgs: set[str] = set()
        self._installations: list[_Installation] = []
        self._repos: dict[str, _Repo] = {}
        self._by_id: dict[int, _Repo] = {}
        self._renames: dict[str, int] = {}
        self._next_id = 1296269
        self._flows: dict[str, dict] = {}
        self._decision: tuple | None = None
        self._script: list[str] = []
        self._access: dict[str, dict] = {}
        self._refresh: dict[str, dict] = {}
        self._codeload_tokens: dict[str, tuple[str, float]] = {}
        self._issued: list[str] = []
        self._secrets: list[str] = []
        self._calls: list[dict] = []
        self._used = {"anon": 0, "user": 0}
        self._window: dict[str, float] = {}  # when each mode's hour began: one reset time for all of it
        self._tarballs: dict[tuple[int, str], bytes] = {}
        self._gates = {name: threading.Event() for name in ("web", "api", "codeload")}
        for gate in self._gates.values():
            gate.set()
        self._stopped = threading.Event()
        self.httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self._defaults()
        self.add_user(login, user_id)
        self.installed = False
        if installed:
            self.set_installed(True)

    def _defaults(self) -> None:
        self.fail, self.fail_count, self.fail_on = "none", 0, "all"
        self.delay, self.slow, self.trickle = 0.0, 300.0, 0.05
        self.chunked, self.throttle = False, 0
        self.rate, self.rate_count, self.rate_reset, self.rate_retry = None, 0, 600, 60
        self.refresh_error, self.refresh_error_count = None, 0
        self.codeload_auth = "token"   # token | header | either: what a private codeload download accepts
        self.api_reply = None          # "410" or "400": every API call refuses the API version
        self.device_flow_disabled = False

    # --- life cycle ---

    def start(self) -> FakeGitHub:
        server_class = _Server6 if ":" in self.host else _Server
        self.httpd = server_class((self.host, self.port), _Handler)
        self.httpd.fake = self
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="fake-github", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        for gate in self._gates.values():
            gate.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def __enter__(self) -> FakeGitHub:
        return self.start() if self.httpd is None else self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    # --- people, installations and repos ---

    def add_user(self, login: str, user_id: int | None = None) -> dict:
        with self._lock:
            user = self._users.setdefault(login.lower(), {"login": login, "id": user_id or self._new_id()})
            return dict(user)

    def add_org(self, login: str) -> None:
        with self._lock:
            self._orgs.add(login.lower())

    def add_installation(self, account: str, *, account_type: str | None = None, suspended: bool = False,
                         selection: str = "all", repos=(), members=()) -> int:
        """An installation of the app on `account`. members: other logins who can see it (an org's members)."""
        with self._lock:
            kind = account_type or ("Organization" if account.lower() in self._orgs else "User")
            inst = _Installation(self._new_id(), account, kind, suspended, selection,
                                 {r.lower() for r in repos}, {m.lower() for m in members})
            self._installations.append(inst)
            return inst.id

    def set_installed(self, installed: bool) -> None:
        """Install or remove the app on the default user's own account."""
        with self._lock:
            own = [i for i in self._installations if i.account.lower() == self.default_login.lower()]
            if installed and not own:
                self.add_installation(self.default_login, account_type="User")
            if not installed:
                self._installations = [i for i in self._installations if i not in own]
            self.installed = installed

    def add_repo(self, owner: str, repo: str, files=None, *, private: bool = False, branches=None,
                 size_kb: int | None = None, renamed_from: str | None = None, empty: bool = False,
                 hostile: bool = False, default_branch: str | None = None, archived: bool = False,
                 pushed_at: str | None = None, sso: bool = False, not_allowed: bool = False, extra=(),
                 comment: str | None = None, collaborators=(), org: bool = False, description: str = "") -> dict:
        """Add a repo. branches: {name: sha or None (made up)}, default {"main": None}; every branch starts with
        `files`. hostile adds HOSTILE to its tarball; extra adds more Member objects. comment: a pax comment that
        differs from the commit. renamed_from: "old-owner/old-name", which then answers 301."""
        with self._lock:
            if org:
                self._orgs.add(owner.lower())
            files = {} if files is None else (dict(files) if isinstance(files, dict) else dict(files))
            files = {path: _as_bytes(data) for path, data in files.items()}
            names = dict(branches) if branches is not None else {(default_branch or "main"): None}
            if empty:
                names = {}
            heads = {name: sha or fake_sha(owner, repo, name, 1) for name, sha in names.items()}
            default = default_branch or (next(iter(heads)) if heads else "main")
            if size_kb is None:
                size_kb = max(1, sum(len(d) for d in files.values()) // 1024)
            entry = _Repo(self._new_id(), owner, repo, private, default, heads, {sha: files for sha in heads.values()},
                          size_kb, archived, pushed_at or _iso(time.time()), empty, hostile, sso, not_allowed,
                          {c.lower() for c in collaborators}, tuple(extra), comment, description)
            self._repos[entry.full_name.lower()] = entry
            self._by_id[entry.id] = entry
            if renamed_from:
                self._renames[renamed_from.lower()] = entry.id
            return {"id": entry.id, "owner": owner, "repo": repo, "full_name": entry.full_name,
                    "default_branch": default, "branches": dict(heads)}

    def push(self, owner: str, repo: str, branch: str | None = None, files=None) -> str:
        """A new commit on a branch (the default branch when None); files replace the tree when given."""
        with self._lock:
            entry = self._repos[f"{owner}/{repo}".lower()]
            name = branch or entry.default_branch
            old = entry.branches.get(name)
            sha = fake_sha(entry.full_name, name, time.time_ns(), len(entry.trees))
            tree = {p: _as_bytes(d) for p, d in files.items()} if files is not None else dict(entry.trees.get(old, {}))
            entry.trees[sha] = tree
            entry.branches[name] = sha
            entry.empty = False
            entry.pushed_at = _iso(time.time())
            return sha

    def delete_branch(self, owner: str, repo: str, branch: str) -> None:
        """The branch is gone (its commits stay, as on GitHub). The default branch cannot be deleted."""
        with self._lock:
            entry = self._repos[f"{owner}/{repo}".lower()]
            if branch == entry.default_branch:
                raise ValueError("the default branch cannot be deleted")
            del entry.branches[branch]

    def head(self, owner: str, repo: str, branch: str | None = None) -> str:
        with self._lock:
            entry = self._repos[f"{owner}/{repo}".lower()]
            return entry.branches[branch or entry.default_branch]

    def tarball(self, owner: str, repo: str, sha: str) -> bytes:
        """The exact bytes /codeload serves for this commit."""
        with self._lock:
            return self._tarball(self._repos[f"{owner}/{repo}".lower()], sha)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # --- the device flow ---

    def approve(self, login: str | None = None) -> None:
        """Device-flow polls succeed from now on (for every waiting flow and the next ones), as `login`."""
        with self._lock:
            login = login or self.default_login
            self.add_user(login)
            self._decision = ("approved", login)

    def deny(self) -> None:
        with self._lock:
            self._decision = ("denied",)

    def expire(self) -> None:
        with self._lock:
            self._decision = ("expired",)

    def script(self, steps) -> None:
        """Answers for the next polls, one step each: pending, slow_down, slow_down_bare, ok, deny, expired,
        token_expired, incorrect, bad_code, disabled, unverified, unknown, bad_token, 500, drop."""
        with self._lock:
            self._script = list(steps)

    def flows(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in f.items() if k != "device_code"} for f in self._flows.values()]

    # --- tokens ---

    def _issue(self, login: str) -> dict:
        access, refresh = "ghu_" + _rand(36), "ghr_" + _rand(76)
        now = time.time()
        self._access[access] = {"login": login, "expires_at": now + self.access_ttl, "refresh": refresh}
        self._refresh[refresh] = {"login": login, "expires_at": now + self.refresh_ttl, "access": access,
                                  "used": False}
        self._issued += [access, refresh]
        self._secrets += [access, refresh]
        return {"access_token": access, "expires_in": int(self.access_ttl), "refresh_token": refresh,
                "refresh_token_expires_in": int(self.refresh_ttl), "scope": "", "token_type": "bearer"}

    def issue(self, login: str | None = None) -> dict:
        """A token reply for `login` (the default user) as if a device-flow login had just finished: hand it to
        TokenManager.accept() to be logged in without running the flow."""
        with self._lock:
            login = login or self.default_login
            self.add_user(login)
            return self._issue(login)

    def issued_tokens(self) -> list[str]:
        """Every synthetic ghu_ / ghr_ token handed out (for the "never leaks" checks)."""
        with self._lock:
            return list(self._issued)

    def issued_secrets(self) -> list[str]:
        """issued_tokens() plus every device code, codeload ?token= value and private codeload address."""
        with self._lock:
            return list(self._secrets)

    def expire_tokens(self) -> None:
        """Every access token stops working, as if 8 hours had passed (refresh tokens still work)."""
        with self._lock:
            for item in self._access.values():
                item["expires_at"] = 0

    def revoke(self, login: str | None = None) -> None:
        """Every token of `login` (all logins when None) stops working, as after revoking the app on GitHub."""
        with self._lock:
            for table in (self._access, self._refresh):
                for item in table.values():
                    if login is None or item["login"].lower() == login.lower():
                        item["expires_at"] = 0

    def user_for(self, token: str | None) -> dict | None | bool:
        """The user of a live access token, None without a token, False for an unknown or expired one."""
        if not token:
            return None
        with self._lock:
            item = self._access.get(token)
            if item is None or item["expires_at"] <= time.time():
                return False
            return self._users.get(item["login"].lower())

    # --- test controls ---

    def set(self, **changes) -> None:
        """fail, fail_count, fail_on (all | web | api | codeload), delay, slow, trickle, chunked, throttle
        (codeload bytes per second), rate (primary | secondary | secondary403 | None), rate_count, rate_reset,
        rate_retry, refresh_error (a GitHub error code for refreshes), refresh_error_count, codeload_auth
        (token | header | either), api_reply ("410" | "400" | None), device_flow_disabled, interval,
        expires_in, slow_step, installed."""
        with self._lock:
            for key, value in changes.items():
                if key == "fail":
                    value = "none" if value is None else str(value)
                    if value not in FAIL_MODES:
                        raise ValueError(f"fail must be one of {', '.join(FAIL_MODES)}")
                    self.fail = value
                elif key == "fail_on":
                    if value not in FAIL_TARGETS:
                        raise ValueError(f"fail_on must be one of {', '.join(FAIL_TARGETS)}")
                    self.fail_on = value
                elif key == "rate":
                    if value not in RATE_MODES:
                        raise ValueError("rate must be primary, secondary, secondary403 or None")
                    self.rate = value
                elif key == "codeload_auth":
                    if value not in ("token", "header", "either"):
                        raise ValueError("codeload_auth must be token, header or either")
                    self.codeload_auth = value
                elif key == "installed":
                    self.set_installed(bool(value))
                elif key in ("fail_count", "rate_count", "refresh_error_count", "throttle"):
                    setattr(self, key, int(value))
                elif key in ("delay", "slow", "trickle", "rate_reset", "rate_retry", "interval", "expires_in",
                             "slow_step", "access_ttl", "refresh_ttl"):
                    setattr(self, key, float(value))
                elif key in ("chunked", "device_flow_disabled"):
                    setattr(self, key, bool(value))
                elif key in ("refresh_error", "api_reply"):
                    setattr(self, key, None if value is None else str(value))
                else:
                    raise ValueError(f"unknown setting: {key}")

    def hold(self, target: str = "codeload") -> None:
        """Make replies wait until release(): 'codeload' stops a tarball halfway, 'api' and 'web' wait
        before answering, 'all' does all three. Requests are still recorded on arrival."""
        for name in self._gates if target == "all" else (target,):
            self._gates[name].clear()

    def release(self, target: str = "all") -> None:
        for name in self._gates if target == "all" else (target,):
            self._gates[name].set()

    def reset(self) -> None:
        """Forget calls, failures and the device-flow decision; answer normally again (data stays)."""
        with self._lock:
            self._calls.clear()
            self._defaults()
            self._decision = None
            self._script = []
        self.release()

    def calls(self, prefix: str | None = None) -> list[dict]:
        """Requests so far: [{"method", "path", "query", "headers" (lowercase names), "form", "at"}]."""
        with self._lock:
            return [dict(c, headers=dict(c["headers"]), form=dict(c["form"])) for c in self._calls
                    if prefix is None or c["path"].startswith(prefix)]

    def wait_for_calls(self, prefix: str, count: int = 1, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.calls(prefix)) >= count:
                return True
            time.sleep(0.01)
        return False

    # --- used by the handler ---

    def _record(self, method: str, path: str, query: str, headers: dict, form: dict) -> None:
        entry = {"method": method, "path": path, "query": query, "headers": headers, "form": form, "at": time.time()}
        with self._lock:
            self._calls.append(entry)
        if self.log_path:
            safe = {k: v for k, v in entry.items() if k not in ("headers", "form")}
            safe["header_names"] = sorted(headers)
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(safe, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._stopped.wait(seconds)

    def _wait_gate(self, target: str) -> None:
        gate = self._gates[target]
        while not gate.wait(0.05):
            if self._stopped.is_set():
                raise _Drop()

    def _take_fail(self, target: str) -> str:
        with self._lock:
            mode = self.fail
            if mode == "none" or self.fail_on not in ("all", target) or (mode == "cut" and target != "codeload"):
                return "none"
            if self.fail_count > 0:
                self.fail_count -= 1
                if self.fail_count == 0:
                    self.fail = "none"
            return mode

    def _take_rate(self) -> str | None:
        with self._lock:
            kind = self.rate
            if kind and self.rate_count > 0:
                self.rate_count -= 1
                if self.rate_count == 0:
                    self.rate = None
            return kind

    def _visible_installations(self, user: dict) -> list[_Installation]:
        login = user["login"].lower()
        return [i for i in self._installations if i.account.lower() == login or login in i.members]

    def _covering(self, repo: _Repo, user: dict | None) -> _Installation | None:
        if user is None:
            return None
        for inst in self._visible_installations(user):
            if inst.account.lower() == repo.owner.lower() and (
                    inst.selection == "all" or repo.full_name.lower() in inst.repos):
                return inst
        return None

    def _readers(self, repo: _Repo, inst: _Installation | None) -> set[str]:
        return {repo.owner.lower()} | repo.collaborators | (inst.members if inst else set())

    def _blocked(self, repo: _Repo, user) -> tuple[int, dict, dict] | None:
        """(status, body, headers) when this user (None: no token) may not read the repo through the app."""
        if user:
            if repo.sso:
                return 403, {"message": "Resource protected by organization SAML enforcement. You must grant "
                                        "your OAuth token access to this organization."}, \
                    {"X-GitHub-SSO": f"required; url={self.url}/web/orgs/{repo.owner}/sso?authorization_request=1"}
            inst = self._covering(repo, user)
            if inst is not None and inst.suspended:
                return 403, {"message": "This installation has been suspended"}, {}
            if repo.not_allowed and (inst is not None or not repo.private):
                return 403, {"message": "Resource not accessible by integration"}, {}
            if repo.private and (inst is None or user["login"].lower() not in self._readers(repo, inst)):
                return 404, {"message": "Not Found"}, {}
            return None
        if repo.private:
            return 404, {"message": "Not Found"}, {}
        return None

    def _repo_by_name(self, owner: str, name: str) -> _Repo | None:
        return self._repos.get(f"{owner}/{name}".lower())

    def _resolve(self, repo: _Repo, ref: str) -> str | None:
        if ref.startswith("refs/heads/"):
            ref = ref[len("refs/heads/"):]
        elif ref.startswith("heads/"):
            ref = ref[len("heads/"):]
        if ref in repo.trees:
            return ref
        return repo.branches.get(ref)

    def _tarball(self, repo: _Repo, sha: str) -> bytes:
        key = (repo.id, sha)
        if key not in self._tarballs:
            extra = tuple(repo.extra) + (HOSTILE if repo.hostile else ())
            self._tarballs[key] = make_tarball(repo.trees.get(sha, {}), top=f"{repo.owner}-{repo.name}-{sha[:7]}",
                                               comment=repo.comment or sha, extra=extra, compresslevel=1)
        return self._tarballs[key]

    def _codeload_token(self, repo: _Repo) -> str:
        token = _rand(32, string.ascii_uppercase + string.digits)
        self._codeload_tokens[token] = (repo.full_name.lower(), time.time() + 300)
        self._secrets.append(token)
        return token

    def _repo_json(self, repo: _Repo) -> dict:
        owner_type = "Organization" if repo.owner.lower() in self._orgs else "User"
        owner_id = self._users.get(repo.owner.lower(), {}).get("id", 1000 + len(repo.owner))
        return {
            "id": repo.id, "node_id": f"R_{repo.id}", "name": repo.name, "full_name": repo.full_name,
            "private": repo.private, "visibility": "private" if repo.private else "public",
            "owner": {"login": repo.owner, "id": owner_id, "type": owner_type},
            "html_url": f"{self.url}/web/{repo.owner}/{repo.name}", "description": repo.description or None,
            "fork": False, "default_branch": repo.default_branch, "size": repo.size_kb, "archived": repo.archived,
            "disabled": False, "pushed_at": repo.pushed_at, "created_at": "2011-01-26T19:01:12Z",
            "updated_at": repo.pushed_at, "permissions": {"admin": False, "push": False, "pull": True},
        }


# --- the HTTP side ----------------------------------------------------------------------------------------------

_PAGE_CSS = ("body{font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em}"
             "input,button{font:inherit;padding:.3em .6em}code{font-size:1.4em;letter-spacing:.1em}")


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=en><meta charset=utf-8><title>{html.escape(title)}</title>"
            f"<style>{_PAGE_CSS}</style><h1>{html.escape(title)}</h1>{body}</html>").encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-github"
    sys_version = ""
    timeout = 30
    _mode = "none"

    def log_message(self, format, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("HEAD")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    @property
    def fake(self) -> FakeGitHub:
        return self.server.fake

    def _route(self, method: str) -> None:
        fake = self.fake
        split = urllib.parse.urlsplit(self.path)
        path, query = split.path, dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
        raw = b""
        length = self.headers.get("Content-Length")
        if length and length.isdigit() and int(length) <= MAX_BODY:
            raw = self.rfile.read(int(length))
        form: dict = {}
        if raw and "x-www-form-urlencoded" in (self.headers.get("Content-Type") or ""):
            form = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
        fake._record(method, path, split.query, {k.lower(): v for k, v in self.headers.items()}, form)
        try:
            if path.startswith("/__fake/"):
                return self._control(method, path, raw)
            target = ("web" if path == "/web" or path.startswith("/web/") else "api" if path.startswith("/api/")
                      else "codeload" if path.startswith("/codeload/") else None)
            if target is None:
                return self._json(404, {"message": "Not Found"})
            if target != "codeload":
                fake._wait_gate(target)
            self._mode = fake._take_fail(target)
            if self._mode == "drop":
                raise _Drop()
            if self._mode == "slow":
                fake._sleep(fake.slow)
            if fake.delay:
                fake._sleep(fake.delay)
            if fake.stopped:
                raise _Drop()
            if self._mode == "500":
                return self._json(500, {"message": "Server Error"})
            if target == "web":
                return self._web(method, path, form)
            if target == "api":
                return self._api(method, path, query)
            return self._codeload(method, path, query)
        except _Drop:
            self.close_connection = True
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    # --- replies ---

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        if self._mode == "trickle":
            for i in range(len(body)):
                if self.fake.stopped:
                    raise _Drop()
                self.wfile.write(body[i:i + 1])
                self.wfile.flush()
                self.fake._sleep(self.fake.trickle)
            return
        self.wfile.write(body)

    def _json(self, status: int, data, headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if self._mode == "badjson":
            body = b'{"message": "this reply is cut sho'
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _oauth(self, data: dict, status: int = 200) -> None:
        """OAuth replies: JSON when asked for, else form-encoded (GitHub's default)."""
        if "application/json" in (self.headers.get("Accept") or ""):
            return self._json(status, data)
        body = urllib.parse.urlencode(data).encode("ascii")
        if self._mode == "badjson":
            body = b"<html>not a reply</html>"
        self._send(status, body, "application/x-www-form-urlencoded; charset=utf-8")

    def _html(self, status: int, body: bytes) -> None:
        self._send(status, body, "text/html; charset=utf-8")

    def _redirect(self, status: int, location: str) -> None:
        self._send(status, b"", "text/html; charset=utf-8", {"Location": location})

    # --- github.com ---

    def _web(self, method: str, path: str, form: dict) -> None:
        fake = self.fake
        rel = path[len("/web"):] or "/"
        if rel == "/login/device/code" and method == "POST":
            return self._device_code(form)
        if rel == "/login/oauth/access_token" and method == "POST":
            if "client_secret" in form:
                return self._oauth({"error": "client_secret_not_allowed",
                                    "error_description": "CodeVision never sends a client secret."}, 400)
            if form.get("client_id") != CLIENT_ID:
                return self._oauth({"error": "incorrect_client_credentials"})
            grant = form.get("grant_type")
            if grant == "urn:ietf:params:oauth:grant-type:device_code":
                return self._device_grant(form)
            if grant == "refresh_token":
                return self._refresh_grant(form)
            return self._oauth({"error": "unsupported_grant_type"})
        if rel == "/login/device" and method == "GET":
            return self._html(200, _page("Device Activation", (
                "<p>Enter the code displayed on your device</p><form method=post action=''>"
                "<input name=user_code autocomplete=off autofocus> <button>Continue</button></form>")))
        if rel == "/login/device" and method == "POST":
            code = (form.get("user_code") or "").strip().upper()
            with fake._lock:
                flow = next((f for f in fake._flows.values() if f["user_code"] == code and f["state"] == "pending"), None)
            if flow is None:
                return self._html(200, _page("Device Activation", "<p>Incorrect code, or it has expired.</p>"
                                             "<p><a href=''>Try again</a></p>"))
            safe = html.escape(code)
            return self._html(200, _page(f"Authorize {APP_NAME}", (
                f"<p>{html.escape(APP_NAME)} would like permission to read your repositories.</p>"
                f"<form method=post action='device/authorize'><input type=hidden name=user_code value='{safe}'>"
                "<button name=decision value=authorize>Authorize " + html.escape(APP_NAME) + "</button> "
                "<button name=decision value=cancel>Cancel</button></form>")))
        if rel == "/login/device/authorize" and method == "POST":
            code = (form.get("user_code") or "").strip().upper()
            with fake._lock:
                flow = next((f for f in fake._flows.values() if f["user_code"] == code and f["state"] == "pending"), None)
                if flow is not None:
                    approved = form.get("decision") == "authorize"
                    flow["state"] = "approved" if approved else "denied"
                    flow["login"] = fake.default_login
            if flow is None:
                return self._html(200, _page("Device Activation", "<p>Incorrect code, or it has expired.</p>"))
            if flow["state"] == "approved":
                return self._html(200, _page("Congratulations, you're all set!",
                                             "<p>Your device is now connected. Go back to CodeVision.</p>"))
            return self._html(200, _page("Authorization cancelled", "<p>Nothing was shared.</p>"))
        if rel == f"/apps/{APP_SLUG}/installations/new":
            if method == "POST":
                fake.set_installed(form.get("action") != "uninstall")
            state = "installed" if fake.installed else "not installed"
            return self._html(200, _page(f"Install {APP_NAME}", (
                f"<p>The app is {state} on @{html.escape(fake.default_login)}.</p>"
                "<form method=post action=''><button name=action value=install>Install</button> "
                "<button name=action value=uninstall>Uninstall</button></form>")))
        if rel == "/settings/apps/authorizations":
            if method == "POST":
                fake.revoke(fake.default_login)
            return self._html(200, _page("Authorized GitHub Apps", (
                f"<p>{html.escape(APP_NAME)}</p><form method=post action=''><button>Revoke</button></form>")))
        parts = rel.strip("/").split("/")
        if method == "GET" and len(parts) == 2:
            with fake._lock:
                repo = fake._repo_by_name(*parts)
            if repo is not None and not repo.private:
                return self._html(200, _page(repo.full_name, "<p>A repository on the fake GitHub.</p>"))
        return self._html(404, _page("Not Found", "<p>This is not the web page you are looking for.</p>"))

    def _device_code(self, form: dict) -> None:
        fake = self.fake
        if "client_secret" in form:
            return self._oauth({"error": "client_secret_not_allowed"}, 400)
        if form.get("client_id") != CLIENT_ID:
            return self._oauth({"error": "incorrect_client_credentials"})
        if fake.device_flow_disabled:
            return self._oauth({"error": "device_flow_disabled"})
        with fake._lock:
            device_code = secrets.token_hex(20)
            user_code = _rand(4, _CODE_CHARS) + "-" + _rand(4, _CODE_CHARS)
            now = time.time()
            fake._flows[device_code] = {"device_code": device_code, "user_code": user_code, "created": now,
                                        "expires_at": now + fake.expires_in, "interval": fake.interval,
                                        "state": "pending", "login": None, "polls": 0}
            fake._secrets.append(device_code)
            reply = {"device_code": device_code, "user_code": user_code,
                     "verification_uri": f"{fake.url}/web/login/device", "expires_in": fake.expires_in,
                     "interval": fake.interval}
        self._oauth(reply)

    def _device_grant(self, form: dict) -> None:
        fake = self.fake
        reply = None
        with fake._lock:  # decide under the lock, answer after it (a slow client never holds the fake up)
            flow = fake._flows.get(form.get("device_code") or "")
            if flow is None:
                step = "incorrect"
            else:
                flow["polls"] += 1
                step = fake._script.pop(0) if fake._script else None
                now = time.time()
                if step is None:
                    if flow["state"] == "done":
                        step = "bad_code"
                    elif now > flow["expires_at"]:
                        step = "expired"
                    elif flow["state"] == "approved":
                        step = "ok"
                    elif flow["state"] == "denied":
                        step = "deny"
                    elif fake._decision is not None:
                        step = {"approved": "ok", "denied": "deny", "expired": "expired"}[fake._decision[0]]
                    elif fake.approve_after is not None and now - flow["created"] >= fake.approve_after:
                        step = "ok"
                    else:
                        step = "pending"
                if step == "ok":
                    login = flow.get("login") or (fake._decision[1] if fake._decision and len(fake._decision) > 1
                                                  else fake.default_login)
                    flow["state"] = "done"
                    reply = fake._issue(login)
                elif step == "slow_down":
                    flow["interval"] = flow["interval"] + fake.slow_step
                    reply = {"error": "slow_down", "interval": flow["interval"],
                             "error_description": "Too many requests have been made in the same timeframe."}
                elif step == "bad_token":
                    reply = {"access_token": "short", "token_type": "mac"}
        if reply is not None:
            return self._oauth(reply)
        if step == "500":
            return self._json(500, {"message": "Server Error"})
        if step == "drop":
            raise _Drop()
        error = {"pending": "authorization_pending", "slow_down_bare": "slow_down", "deny": "access_denied",
                 "expired": "expired_token", "token_expired": "token_expired", "incorrect": "incorrect_device_code",
                 "bad_code": "bad_verification_code", "disabled": "device_flow_disabled",
                 "unverified": "unverified_user_email", "unknown": "something_new"}.get(step, "authorization_pending")
        self._oauth({"error": error, "error_description": "fake", "error_uri": "https://docs.github.com/"})

    def _refresh_grant(self, form: dict) -> None:
        fake = self.fake
        with fake._lock:
            if fake.refresh_error:
                error = fake.refresh_error
                if fake.refresh_error_count > 0:
                    fake.refresh_error_count -= 1
                    if fake.refresh_error_count == 0:
                        fake.refresh_error = None
                reply = None if error == "500" else {"error": error, "error_description": "fake"}
            else:
                item = fake._refresh.get(form.get("refresh_token") or "")
                if item is None or item["used"] or item["expires_at"] <= time.time():
                    reply = {"error": "bad_refresh_token",
                             "error_description": "The refresh token passed is incorrect or expired."}
                else:
                    item["used"] = True  # every refresh rotates both tokens: the old pair stops working
                    old = fake._access.get(item["access"])
                    if old is not None:
                        old["expires_at"] = 0
                    reply = fake._issue(item["login"])
        if reply is None:
            return self._json(500, {"message": "Server Error"})
        self._oauth(reply)

    # --- api.github.com ---

    def _rate_headers(self, mode: str, count: bool = True) -> dict:
        """Like GitHub's: one reset time for the whole hour, and (count=False) the tarball redirect, which uses no
        core quota and shows a counter of its own."""
        fake = self.fake
        now = time.time()
        with fake._lock:
            start = fake._window.get(mode)
            if start is None or now >= start + 3600:
                start = fake._window[mode] = now
                fake._used[mode] = 0
            if count:
                fake._used[mode] += 1
            used = fake._used[mode] if count else 0
        limit = 5000 if mode == "user" else 60
        return {"x-ratelimit-limit": str(limit), "x-ratelimit-remaining": str(max(0, limit - used)),
                "x-ratelimit-reset": str(int(start) + 3600), "x-ratelimit-used": str(used),
                "x-ratelimit-resource": "core", "x-github-api-version-selected": API_VERSIONS[-1]}

    def _token(self) -> str | None:
        value = (self.headers.get("Authorization") or "").strip()
        scheme, _, token = value.partition(" ")
        if scheme.lower() in ("bearer", "token") and token.strip():
            return token.strip()
        return None

    def _api(self, method: str, path: str, query: dict) -> None:
        fake = self.fake
        token = self._token()
        mode = "user" if token else "anon"
        kind = fake._take_rate()
        if kind in ("primary",):
            return self._json(403, {"message": "API rate limit exceeded for 127.0.0.1.",
                                    "documentation_url": "https://docs.github.com/rest"},
                              {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "0", "x-ratelimit-used": "60",
                               "x-ratelimit-reset": str(int(time.time() + fake.rate_reset)),
                               "x-ratelimit-resource": "core"})
        if kind == "secondary":
            return self._json(429, {"message": "You have exceeded a secondary rate limit. Please wait a few "
                                               "minutes before you try again."},
                              {"retry-after": str(int(fake.rate_retry))})
        if kind == "secondary403":
            return self._json(403, {"message": "You have exceeded a secondary rate limit."})
        version = self.headers.get("X-GitHub-Api-Version")
        if fake.api_reply == "410":
            return self._json(410, {"message": "This API version is no longer supported."})
        if fake.api_reply == "400" or (version is not None and version not in API_VERSIONS):
            return self._json(400, {"message": f"Unsupported 'X-GitHub-Api-Version' header: {version or '?'}"})
        user = fake.user_for(token)
        if user is False:
            return self._json(401, {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"})
        rel = path[len("/api"):]
        parts = rel.strip("/").split("/")
        tarball = ((len(parts) >= 4 and parts[0] == "repos" and parts[3] == "tarball")
                   or (len(parts) >= 3 and parts[0] == "repositories" and parts[2] == "tarball"))
        headers = self._rate_headers(mode, count=not tarball)
        if method not in ("GET", "HEAD"):
            return self._json(404, {"message": "Not Found"}, headers)
        if rel == "/user":
            if user is None:
                return self._json(401, {"message": "Requires authentication"}, headers)
            return self._json(200, {"login": user["login"], "id": user["id"], "type": "User", "name": None}, headers)
        if rel == "/user/installations":
            if user is None:
                return self._json(401, {"message": "Requires authentication"}, headers)
            with fake._lock:
                items = [self._installation_json(i) for i in fake._visible_installations(user)]
            return self._paged(items, query, rel, headers, key="installations")
        if len(parts) == 4 and parts[:2] == ["user", "installations"] and parts[3] == "repositories":
            if user is None:
                return self._json(401, {"message": "Requires authentication"}, headers)
            with fake._lock:
                inst = next((i for i in fake._visible_installations(user) if str(i.id) == parts[2]), None)
                items = None
                if inst is not None and not inst.suspended:
                    repos = [r for r in sorted(fake._repos.values(), key=lambda r: r.id)
                             if r.owner.lower() == inst.account.lower()
                             and (inst.selection == "all" or r.full_name.lower() in inst.repos)
                             and (not r.private or user["login"].lower() in fake._readers(r, inst))]
                    items = [fake._repo_json(r) for r in repos]
            if inst is None:
                return self._json(404, {"message": "Not Found"}, headers)
            if items is None:
                return self._json(403, {"message": "This installation has been suspended"}, headers)
            return self._paged(items, query, rel, headers, key="repositories",
                               more={"repository_selection": inst.selection})
        repo, rest, moved_to = None, "", None
        with fake._lock:
            if len(parts) >= 2 and parts[0] == "repositories" and parts[1].isdigit():
                repo, rest = fake._by_id.get(int(parts[1])), "/".join(parts[2:])
            elif len(parts) >= 3 and parts[0] == "repos":
                owner, name = urllib.parse.unquote(parts[1]), urllib.parse.unquote(parts[2])
                repo, rest = fake._repo_by_name(owner, name), "/".join(parts[3:])
                if repo is None and f"{owner}/{name}".lower() in fake._renames:
                    moved_to = fake._renames[f"{owner}/{name}".lower()]
            blocked = fake._blocked(repo, user) if repo is not None else None
        if moved_to is not None:
            target = f"{fake.url}/api/repositories/{moved_to}" + (f"/{rest}" if rest else "")
            if "?" in self.path:
                target += "?" + self.path.split("?", 1)[1]
            return self._json(301, {"message": "Moved Permanently", "url": target}, dict(headers, Location=target))
        if repo is None:
            return self._json(404, {"message": "Not Found"}, headers)
        if blocked is not None:
            status, body, extra = blocked
            return self._json(status, body, dict(headers, **extra))
        return self._repo_route(repo, rest, query, headers)

    def _installation_json(self, inst: _Installation) -> dict:
        return {"id": inst.id, "account": {"login": inst.account, "type": inst.account_type},
                "repository_selection": inst.selection, "app_slug": APP_SLUG,
                "suspended_at": "2026-11-01T00:00:00Z" if inst.suspended else None,
                "permissions": {"contents": "read", "metadata": "read"}}

    def _paged(self, items: list, query: dict, rel: str, headers: dict, *, key: str | None = None,
               more: dict | None = None) -> None:
        try:
            per_page = max(1, min(100, int(query.get("per_page", "30"))))
            page = max(1, int(query.get("page", "1")))
        except ValueError:
            return self._json(422, {"message": "Invalid per_page or page"}, headers)
        last = max(1, -(-len(items) // per_page))
        chunk = items[(page - 1) * per_page:page * per_page]
        links = []
        base = f"{self.fake.url}/api{rel}"
        if page < last:
            links.append(f'<{base}?per_page={per_page}&page={page + 1}>; rel="next"')
            links.append(f'<{base}?per_page={per_page}&page={last}>; rel="last"')
        if page > 1:
            links.append(f'<{base}?per_page={per_page}&page=1>; rel="first"')
        out = dict(headers)
        if links:
            out["Link"] = ", ".join(links)
        data = chunk if key is None else dict({"total_count": len(items), key: chunk}, **(more or {}))
        self._json(200, data, out)

    def _repo_route(self, repo: _Repo, rest: str, query: dict, headers: dict) -> None:
        fake = self.fake
        if rest == "":
            with fake._lock:
                data = fake._repo_json(repo)
            return self._json(200, data, headers)
        if rest == "branches":
            with fake._lock:
                items = [{"name": name, "commit": {"sha": sha, "url": f"{fake.url}/api/repos/{repo.full_name}/commits/{sha}"},
                          "protected": False} for name, sha in sorted(repo.branches.items())]
            # the next-page links name the repo by its id, as GitHub's do
            return self._paged(items, query, f"/repositories/{repo.id}/branches", headers)
        if rest.startswith("commits/"):
            ref = urllib.parse.unquote(rest[len("commits/"):])
            with fake._lock:
                empty = repo.empty or not repo.branches
                sha = None if empty else fake._resolve(repo, ref)
            if empty:
                return self._json(409, {"message": "Git Repository is empty."}, headers)
            if sha is None:
                return self._json(422, {"message": f"No commit found for SHA: {ref}"}, headers)
            etag = f'"{sha}"'
            sent = (self.headers.get("If-None-Match") or "").strip()
            if sent in (etag, "W/" + etag):
                return self._send(304, b"", "text/plain", dict(headers, ETag=etag))
            if "application/vnd.github.sha" in (self.headers.get("Accept") or ""):
                return self._send(200, sha.encode("ascii"), "application/vnd.github.sha; charset=utf-8",
                                  dict(headers, ETag=etag))
            return self._json(200, {"sha": sha, "commit": {"message": "fake commit"}}, dict(headers, ETag=etag))
        if rest == "tarball" or rest.startswith("tarball/"):
            ref = urllib.parse.unquote(rest[len("tarball/"):]) if "/" in rest else repo.default_branch
            with fake._lock:
                location = f"{fake.url}/codeload/{repo.owner}/{repo.name}/legacy.tar.gz/{urllib.parse.quote(ref)}"
                if repo.private:
                    location += "?token=" + fake._codeload_token(repo)
                    fake._secrets.append(location)
            return self._send(302, b"", "text/html; charset=utf-8", dict(headers, Location=location))
        return self._json(404, {"message": "Not Found"}, headers)

    # --- codeload.github.com ---

    def _codeload(self, method: str, path: str, query: dict) -> None:
        fake = self.fake
        parts = path.split("/", 5)  # ['', 'codeload', owner, repo, 'legacy.tar.gz', ref]
        if len(parts) != 6 or parts[4] != "legacy.tar.gz":
            return self._send(404, b"404: Not Found", "text/plain; charset=utf-8")
        data = None
        with fake._lock:
            repo = fake._repo_by_name(parts[2], parts[3])
            sha = fake._resolve(repo, urllib.parse.unquote(parts[5])) if repo is not None else None
            allowed = repo is not None and sha is not None and not repo.empty
            if allowed and repo.private:
                entry = fake._codeload_tokens.get(query.get("token") or "")
                token_ok = entry is not None and entry[0] == repo.full_name.lower() and entry[1] > time.time()
                user = fake.user_for(self._token())
                header_ok = bool(user) and fake._blocked(repo, user) is None
                allowed = {"token": token_ok, "header": header_ok, "either": token_ok or header_ok}[fake.codeload_auth]
            if allowed:
                data = fake._tarball(repo, sha)
                top = f"{repo.owner}-{repo.name}-{sha[:7]}"
        if data is None:
            return self._send(404, b"404: Not Found", "text/plain; charset=utf-8")
        self._send_tarball(data, top)

    def _send_tarball(self, data: bytes, top: str) -> None:
        fake = self.fake
        chunked = fake.chunked
        if chunked:
            self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-gzip")
        self.send_header("Content-Disposition", f"attachment; filename={top}.tar.gz")
        self.send_header("ETag", '"' + hashlib.sha256(data).hexdigest() + '"')
        self.send_header("Vary", "Authorization,Accept-Encoding")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
        else:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        cut = len(data) // 2 if self._mode == "cut" else None
        first = min(64 * 1024, max(1, len(data) // 2))
        sent = 0
        while sent < len(data):
            if sent >= first:
                fake._wait_gate("codeload")
            size = first if sent == 0 else 64 * 1024
            if self._mode == "trickle":
                size = 512
            piece = data[sent:sent + size]
            if cut is not None and sent + len(piece) > cut:
                piece = piece[:cut - sent]
                self._write(piece, chunked)
                raise _Drop()  # the connection ends halfway
            self._write(piece, chunked)
            sent += len(piece)
            if fake.throttle:
                fake._sleep(len(piece) / fake.throttle)
            elif self._mode == "trickle":
                fake._sleep(fake.trickle)
            if fake.stopped:
                raise _Drop()
        if chunked:
            self.wfile.write(b"0\r\n\r\n")

    def _write(self, piece: bytes, chunked: bool) -> None:
        if not piece:
            return
        if chunked:
            self.wfile.write(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n")
        else:
            self.wfile.write(piece)
        self.wfile.flush()

    # --- control ---

    def _control(self, method: str, path: str, raw: bytes) -> None:
        fake = self.fake
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        name = path[len("/__fake/"):]
        if name == "calls" and method == "GET":
            return self._json(200, fake.calls())
        if method != "POST":
            return self._json(404, {"message": "Not Found"})
        try:
            if name == "reset":
                fake.reset()
            elif name == "set":
                fake.set(**body)
            elif name == "approve":
                fake.approve(body.get("login"))
            elif name == "deny":
                fake.deny()
            elif name == "expire":
                fake.expire()
            elif name == "push":
                return self._json(200, {"sha": fake.push(body["owner"], body["repo"], body.get("branch"))})
            elif name == "delete_branch":
                fake.delete_branch(body["owner"], body["repo"], body["branch"])
            elif name == "script":
                steps = body.get("steps")
                if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
                    raise ValueError("steps is a list of strings")
                fake.script(steps)
            elif name == "expire_tokens":
                fake.expire_tokens()
            elif name == "revoke":
                fake.revoke(body.get("login"))
            elif name == "hold":
                fake.hold(body.get("target", "codeload"))
            elif name == "release":
                fake.release(body.get("target", "all"))
            elif name == "stop":
                self._json(200, {"ok": True})
                threading.Thread(target=fake.stop, daemon=True).start()
                return None
            else:
                return self._json(404, {"message": "Not Found"})
        except (KeyError, TypeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, {"ok": True})


# --- demo data for the browser checks ---------------------------------------------------------------------------

def add_demo_repos(fake: FakeGitHub) -> None:
    """Repos for the screen checks: public, private, empty, renamed, an SSO org, a suspended installation,
    another account's private repo, a hostile-names repo, big files, committed dependencies, too big, many
    branches, and enough small repos to page the list."""
    day = 86400
    now = time.time()
    hello = {"README.md": "# Hello-World\n\nMy first repository on GitHub.\n",
             "index.html": "<!doctype html><title>Hello</title><script src=app.js></script>\n",
             "app.js": "import { greet } from './lib/greet.js';\ngreet('world');\n",
             "lib/greet.js": "export function greet(name) {\n  console.log(`Hello, ${name}!`);\n}\n",
             "server/app.py": "from flask import Flask\napp = Flask(__name__)\n\n@app.route('/')\ndef home():\n"
                              "    return 'hi'\n",
             "server/requirements.txt": "flask\n"}
    fake.add_repo("octocat", "Hello-World", hello, branches={"main": None, "feature/login": None, "docs": None},
                  pushed_at=_iso(now - 3 * day), description="My first repository")
    fake.add_repo("octocat", "secret-diary", {"README.md": "# Private notes\n", "diary.py": "print('dear diary')\n"},
                  private=True, pushed_at=_iso(now - 1 * day))
    fake.add_repo("octocat", "empty-repo", empty=True, pushed_at=_iso(now - 40 * day))
    fake.add_repo("octocat", "new-name", {"README.md": "# renamed from old-name\n"}, renamed_from="octocat/old-name",
                  pushed_at=_iso(now - 10 * day))
    fake.add_org("acme-sso")
    fake.add_installation("acme-sso", account_type="Organization", members=[fake.default_login])
    fake.add_repo("acme-sso", "handbook", {"README.md": "# Handbook\n"}, private=True, sso=True,
                  pushed_at=_iso(now - 5 * day), collaborators=[fake.default_login])
    fake.add_repo("acme-sso", "open-handbook", {"README.md": "# Open handbook\n"}, sso=True,
                  pushed_at=_iso(now - 6 * day))
    fake.add_org("paused-org")
    fake.add_installation("paused-org", account_type="Organization", suspended=True, members=[fake.default_login])
    fake.add_repo("paused-org", "app", {"main.py": "x = 1\n"}, private=True, collaborators=[fake.default_login])
    fake.add_org("other-org")
    fake.add_repo("other-org", "private-tool", {"tool.py": "x = 1\n"}, private=True)
    hostile_files = {"README.md": "# <b>hostile</b> names & \"quotes\"\n", "a&b.txt": "and\n", "o'neil.py": "x = 1\n",
                     "<script>alert(1)</script>.md": "x\n", "\"quoted\".js": "x\n", "x<y>z.txt": "x\n"}
    fake.add_repo("octocat", "hostile-names", hostile_files, hostile=True, pushed_at=_iso(now - 2 * day),
                  branches={"main": None, "<img/src/onerror=alert(1)>": None, '"><a href=//x>': None,
                            '"><b>bold</b>': None, "it's&amp;<i>": None}, default_branch="main")
    big = {"README.md": "# Big files\n", "logs/huge.log": ("2026-11-25 12:00:00 INFO a log line\n" * 60000),
           "media/clip.mp4": os.urandom(2 * 1024 * 1024), "model.bin": os.urandom(1024 * 1024),
           "src/main.py": "print('big')\n"}
    fake.add_repo("octocat", "big-files", big, pushed_at=_iso(now - 4 * day))
    deps = node_modules_files(3000)
    deps.update(committed_venv_files())
    sub_files, sub_extra = submodule_layout()
    deps.update(sub_files)
    deps.update({"package.json": '{"name": "committed-deps"}\n', "src/index.js": "require('./util');\n",
                 "src/util.js": "module.exports = {};\n", "dist/bundle.js": "!function(){}();\n",
                 "assets/design.psd": LFS_POINTER, "assets/logo.png": PNG_BYTES, "tools/blob.dat": BINARY_NUL})
    fake.add_repo("octocat", "committed-deps", deps, extra=sub_extra, pushed_at=_iso(now - 7 * day))
    fake.add_repo("octocat", "too-big", {"README.md": "# huge\n"}, size_kb=3_000_000, pushed_at=_iso(now - 8 * day))
    fake.add_repo("octocat", "locked", {"README.md": "# locked\n"}, private=True, not_allowed=True,
                  pushed_at=_iso(now - 9 * day))
    fake.add_repo("octocat", "many-branches", {"README.md": "# branches\n"},
                  branches={"main": None, **{f"feature/{i:03d}": None for i in range(150)}},
                  default_branch="main", pushed_at=_iso(now - 11 * day))
    fake.add_repo("octocat", "archived-tool", {"README.md": "# old\n"}, archived=True, pushed_at=_iso(now - 400 * day))
    for i in range(1, 13):
        fake.add_repo("octocat", f"demo-{i:02d}", {"README.md": f"# demo {i}\n", "main.py": f"print({i})\n"},
                      pushed_at=_iso(now - (12 + i) * day))


# --- command line -------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake_github.py", description="A fake GitHub for CodeVision tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--demo", action="store_true", help="add the demo repos for the browser checks")
    parser.add_argument("--approve-after", type=float, default=None,
                        help="approve every device-flow login this many seconds after it starts")
    parser.add_argument("--interval", type=float, default=5.0, help="device-flow poll interval in seconds")
    parser.add_argument("--expires-in", type=float, default=900.0, help="seconds a login code stays valid")
    parser.add_argument("--not-installed", action="store_true", help="start with the app not installed")
    parser.add_argument("--max-life", type=float, default=4 * 3600, help="stop by itself after this many seconds")
    parser.add_argument("--log", default=None, help="append every request (no header values) to this file")
    args = parser.parse_args(argv)
    try:
        fake = FakeGitHub(args.port, host=args.host, interval=args.interval, expires_in=args.expires_in,
                          installed=not args.not_installed, approve_after=args.approve_after, log_path=args.log)
        if args.demo:
            add_demo_repos(fake)
        fake.start()
    except (OSError, ValueError) as exc:
        print(f"fake_github: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    print(f"fake github on {fake.url} (set CODEVISION_GITHUB_FAKE={fake.url})", flush=True)
    deadline = time.monotonic() + args.max_life if args.max_life > 0 else None
    try:
        while not fake.stopped:
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
