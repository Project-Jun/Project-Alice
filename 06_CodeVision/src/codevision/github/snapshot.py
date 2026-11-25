"""The snapshot cache: downloaded commits under <data_dir>/repos, found offline, published atomically,
recorded and cleaned up, plus the Korean notes a snapshot adds to graph["warnings"].

Layout (short, so Windows path limits leave room for the repository's own paths):

    repos/
      .partial/<12 hex>/              a download in progress (staging): snapshot.json is written here
      .partial/<12 hex>/<Repo>/       ... and the tree is extracted here
      .delete/<12 hex>/               a snapshot being deleted: renamed here first, so it is gone at once
      .delete.json                    tombstones: snapshot folders a delete could not remove yet
      <16 hex>/refs.json              owner, repo, private, default branch, branch -> {sha, checked_at}
      <16 hex>/<sha12>/snapshot.json  owner, repo, sha, private, user_id, branch, created, opened, notes data
      <16 hex>/<sha12>/<Repo>/        the analyzed root (the leaf), named after the repo

<16 hex> is the start of sha256("owner_lc/repo_lc"): owner and repo live only in the JSON files. The leaf is
the canonical repo name, because the graph name, the root label and the Python root-package rule come from
it; a name Windows cannot use becomes 'repo', with the real name kept as the display name.

Every write is atomic (a temporary name with the pid and thread id, then os.replace), and readers trust only
a complete snapshot.json next to an existing leaf. Two CodeVision processes can share the folder: publishing
is one rename, a delete is a rename into .delete first, and the start-up sweep spares young .partial
folders. Every path CodeVision creates here is made of fixed names, hex and the checked leaf, and the staging
and .delete paths are never longer than the final ones.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .extract import EXAMPLE_KEYS, GiB, Result, path_limits, shown_name, utf16_len, windows_unsafe

log = logging.getLogger(__name__)

VERSION = 1
KEY_LEN = 16                 # hex characters of sha256(owner_lc/repo_lc)
SHA_LEN = 12                 # hex characters of the commit in the folder name
RAND_BYTES = 6               # .partial and .delete names: 12 hex characters
META = "snapshot.json"
REFS = "refs.json"
PARTIAL = ".partial"
TRASH = ".delete"
TOMBSTONES = ".delete.json"
FALLBACK_LEAF = "repo"
MAX_TOTAL_BYTES = 2 * GiB    # all snapshots together (bytes written) ...
MAX_SNAPSHOTS = 30           # ... and how many
RECENT_SECONDS = 600.0       # a snapshot opened (or published) this recently is never pruned
PARTIAL_MAX_AGE = 3600.0     # younger .partial folders may belong to another CodeVision process
PUBLISH_SECONDS = 30.0       # the publish rename is retried this long (antivirus scans the new files)
MOVE_SECONDS = 2.0           # renaming a snapshot into .delete is retried this long
DELETE_RETRY_SECONDS = 3.0   # retries inside one rmtree, in total
MAX_BRANCHES = 200           # branches remembered per repo in refs.json

_SHA = re.compile(r"[0-9a-f]{40}")
_KEY = re.compile(r"[0-9a-f]{%d}" % KEY_LEN)
_SHA12 = re.compile(r"[0-9a-f]{%d}" % SHA_LEN)
_TOMBSTONE = re.compile(r"[0-9a-f]{%d}/[0-9a-f]{%d}" % (KEY_LEN, SHA_LEN))

# Korean notes for graph["warnings"], in this order; the same sentences as messages.LFS, SUBMODULES, ...
NOTES = {
    "lfs": "LFS로 올린 파일 {n}개는 내용 없이 이름만 보여요.",
    "submodules": "하위 저장소(submodule) {n}개는 GitHub 압축 파일에 내용이 없어서 빈 폴더로 보여요.",
    "bad_name": "Windows에서 쓸 수 없는 이름의 파일 {n}개{names}는 받지 않았어요.",
    "too_long": "경로가 너무 길어서 Windows에 만들 수 없는 파일 {n}개는 받지 않았어요.",
    "collision": "대소문자만 다른 같은 이름의 파일 {n}개{names}는 하나만 받았어요.",
    "blocked": "보안 프로그램이 막은 파일 {n}개는 받지 않았어요.",
}


# --- names and notes ------------------------------------------------------------------------------------

def repo_key(owner: str, repo: str) -> str:
    """The repo's folder name: the first 16 hex of sha256('owner_lc/repo_lc')."""
    return hashlib.sha256(f"{owner.lower()}/{repo.lower()}".encode("utf-8")).hexdigest()[:KEY_LEN]


def leaf_name(repo: str) -> tuple[str, str]:
    """(folder, display): the canonical repo name, or 'repo' when Windows cannot use the name (CON, aux.js,
    'x.') or it would clash with snapshot.json; display is always the repo name."""
    if windows_unsafe(repo) or repo.lower().startswith(META):
        return FALLBACK_LEAF, repo
    return repo, repo


def _example_text(n: int, names) -> str:
    """'(a, b 등)': at most two names, with '등' when there are more than the ones shown."""
    shown = [shown_name(x, 60) for x in (names if isinstance(names, list) else [])[:2] if isinstance(x, str)]
    if not shown:
        return ""
    return "(" + ", ".join(shown) + (" 등" if n > len(shown) else "") + ")"


def notes(meta: dict) -> list[str]:
    """The Korean warnings for what extraction left out (snapshot.json 'skipped' and 'examples')."""
    skipped = meta.get("skipped") if isinstance(meta, dict) else None
    examples = meta.get("examples") if isinstance(meta, dict) else None
    skipped = skipped if isinstance(skipped, dict) else {}
    examples = examples if isinstance(examples, dict) else {}
    out = []
    for kind, text in NOTES.items():
        n = skipped.get(kind)
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            continue
        names = _example_text(n, examples.get(kind)) if kind in EXAMPLE_KEYS else ""
        out.append(text.format(n=f"{n:,}", names=names))
    return out


# --- data ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Staging:
    """A download in progress. Give dest and final_root to extract_stream, then publish() or discard()."""

    owner: str          # canonical case, as GitHub spells it
    repo: str
    sha: str
    root: Path          # repos/.partial/<12 hex>/
    dest: Path          # root/<leaf>: extract_stream creates it
    final_root: Path    # repos/<16 hex>/<sha12>/<leaf>: path lengths are checked against it
    leaf: str
    display: str


@dataclass
class Snapshot:
    owner: str
    repo: str
    sha: str
    private: bool
    root: Path                          # the analyzed folder (the leaf)
    display: str                        # the name to show: the repo name, also when the leaf is 'repo'
    notes: list[str] = field(default_factory=list)
    user_id: int | None = None          # the login that downloaded it; a private one is shown only to it
    branch: str = ""                    # the branch it was downloaded for (only a label)
    created: float = 0.0
    opened: float = 0.0
    size: int = 0                       # bytes written
    folder: Path | None = None          # repos/<16 hex>/<sha12>

    def allowed(self, user_id: int | None) -> bool:
        """A public snapshot opens for anyone; a private one only for the user_id that downloaded it."""
        return not self.private or (self.user_id is not None and user_id == self.user_id)


def _plain(name) -> bool:
    """An owner or repo name that can be hashed and stored (the validators in base.py are stricter)."""
    return (isinstance(name, str) and 0 < len(name) <= 100 and name.isascii() and name.isprintable()
            and not any(ch in name for ch in "/\\: "))


def _valid_sha(sha) -> bool:
    return isinstance(sha, str) and _SHA.fullmatch(sha) is not None


def _check(owner, repo, sha) -> None:
    if not (_plain(owner) and _plain(repo) and _valid_sha(sha)):
        raise ValueError("owner, repo or sha is not valid")


def _check_branch(branch) -> None:
    if not isinstance(branch, str) or not 0 < len(branch) <= 255 or not branch.isprintable():
        raise ValueError("branch is not valid")
    branch.encode("utf-8")  # a lone surrogate raises UnicodeEncodeError, a ValueError


def _number(value, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
        return float(value)
    return default


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _read_json(path: Path):
    """The parsed file, or None. A file being replaced at that moment (Windows sharing violation) is read again,
    so a good snapshot is never taken for a broken one."""
    for attempt in range(4):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except PermissionError:
            if attempt == 3:
                return None
            time.sleep(0.05)
        except (OSError, ValueError):
            return None
    return None


def _replace(src, dst, sleep, seconds: float = 2.0) -> None:
    """os.replace, retried briefly while a reader or the antivirus holds the target."""
    waited, delay = 0.0, 0.02
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if waited >= seconds:
                raise
            sleep(delay)
            waited += delay
            delay = min(delay * 2, 0.5)


def _write_json(path: Path, data: dict, sleep=time.sleep) -> None:
    """Write a JSON file atomically: a temporary name with the pid and thread id, then os.replace."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        # ensure_ascii: the file stays valid UTF-8 whatever a name holds
        tmp.write_text(json.dumps(data, ensure_ascii=True, indent=1), encoding="utf-8")
        _replace(tmp, path, sleep)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _rmtree(path: Path, sleep=time.sleep, budget: float = DELETE_RETRY_SECONDS) -> bool:
    """Delete a folder tree, clearing read-only flags and retrying files that are in use (the antivirus, the
    AI worker) within a small time budget. True when nothing is left. Never follows links or junctions."""
    left = [budget]

    def retry(func, p, exc):
        if isinstance(exc, FileNotFoundError) or func not in (os.unlink, os.remove, os.rmdir):
            return  # only a delete is worth retrying (rmtree also reports os.open, os.scandir, os.lstat)
        delay = 0.05
        while True:
            try:
                os.chmod(p, stat.S_IWRITE)
            except OSError:
                pass
            try:
                func(p)
                return
            except FileNotFoundError:
                return
            except OSError:
                if left[0] <= 0:
                    return  # rmtree moves on; what is left is reported by the caller
            sleep(delay)
            left[0] -= delay
            delay = min(delay * 2, 0.5)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:  # pragma: no cover - Python 3.10 and 3.11
            shutil.rmtree(path, onerror=lambda func, p, info: retry(func, p, info[1]))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("snapshot folder not fully deleted (%s)", type(exc).__name__)
    return not os.path.lexists(path)


def _children(folder: Path, pattern: re.Pattern | None = None) -> list[Path]:
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    out = []
    for entry in entries:
        try:
            if entry.is_symlink() or (getattr(entry, "is_junction", None) and entry.is_junction()):
                continue  # cleanup never follows a link or a junction someone put here
            if entry.is_dir(follow_symlinks=False) and (pattern is None or pattern.fullmatch(entry.name)):
                out.append(Path(entry.path))
        except OSError:
            continue
    return out


# --- the store -------------------------------------------------------------------------------------------

class SnapshotStore:
    """The snapshot cache. Thread-safe; constructing it touches no disk.

    data_dir is read on every call (CODEVISION_HOME can change in tests). in_use gives the analysis roots that
    must not be deleted (AIService.roots_in_use): prune and the global cap skip them, logout does not.
    """

    def __init__(self, data_dir: Callable[[], Path], in_use: Callable[[], Iterable] | None = None, *,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self._data_dir = data_dir
        self._in_use = in_use
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()

    # -- paths --

    def repos_dir(self) -> Path:
        return Path(os.path.abspath(os.fspath(self._data_dir()))) / "repos"

    def _folder(self, owner: str, repo: str, sha: str) -> Path:
        return self.repos_dir() / repo_key(owner, repo) / sha[:SHA_LEN]

    def free_bytes(self) -> int:
        """Free space on the repos folder's drive (the caller wants 1 GiB before a download)."""
        repos = self.repos_dir()
        repos.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(repos).free

    # -- reading --

    def _load(self, folder: Path, require_leaf: bool = True) -> Snapshot | None:
        meta = _read_json(folder / META)
        if not isinstance(meta, dict) or meta.get("v") != VERSION:
            return None
        owner, repo, sha, leaf = meta.get("owner"), meta.get("repo"), meta.get("sha"), meta.get("leaf")
        if not (_plain(owner) and _plain(repo) and _valid_sha(sha) and isinstance(leaf, str)
                and windows_unsafe(leaf) is None):
            return None
        if folder.name != sha[:SHA_LEN] or folder.parent.name != repo_key(owner, repo):
            return None
        private, user_id = meta.get("private"), meta.get("user_id")
        if not isinstance(private, bool) or (user_id is not None and (not isinstance(user_id, int)
                                                                      or isinstance(user_id, bool))):
            return None
        root = folder / leaf
        if require_leaf and not root.is_dir():
            return None
        display = meta.get("display") if isinstance(meta.get("display"), str) and meta.get("display") else repo
        branch = meta.get("branch") if isinstance(meta.get("branch"), str) else ""
        created = _number(meta.get("created"))
        return Snapshot(owner, repo, sha, private, root, display, notes(meta), user_id=user_id, branch=branch,
                        created=created, opened=_number(meta.get("opened"), created),
                        size=_count(meta.get("written")), folder=folder)

    def find(self, owner: str, repo: str, sha: str) -> Snapshot | None:
        """The snapshot of this commit, offline: a complete snapshot.json with the same 40-character sha
        and an existing leaf. Owner and repo compare without case. The caller checks allowed(user_id)."""
        if not (_plain(owner) and _plain(repo) and _valid_sha(sha)):
            return None
        snap = self._load(self._folder(owner, repo, sha))
        if snap is None or snap.sha != sha or snap.owner.lower() != owner.lower() or snap.repo.lower() != repo.lower():
            return None
        return snap

    def snapshots(self) -> list[Snapshot]:
        """Every complete snapshot."""
        out = []
        for key_dir in _children(self.repos_dir(), _KEY):
            for folder in _children(key_dir, _SHA12):
                snap = self._load(folder)
                if snap is not None:
                    out.append(snap)
        return out

    def _repo_snapshots(self, owner: str, repo: str) -> list[Snapshot]:
        out = []
        for folder in _children(self.repos_dir() / repo_key(owner, repo), _SHA12):
            snap = self._load(folder)
            if snap is not None and snap.owner.lower() == owner.lower() and snap.repo.lower() == repo.lower():
                out.append(snap)
        return out

    # -- download and publish --

    def staging(self, owner: str, repo: str, sha: str) -> Staging:
        """A new, empty repos/.partial/<12 hex>/ for one download. Raises ValueError for bad names and
        OSError (ENAMETOOLONG) when even the short leaf would be too long a folder path."""
        _check(owner, repo, sha)
        repos = self.repos_dir()
        leaf, display = leaf_name(repo)
        final_root = repos / repo_key(owner, repo) / sha[:SHA_LEN] / leaf
        dir_max = path_limits()[1]
        if utf16_len(str(final_root)) > dir_max and leaf != FALLBACK_LEAF:
            leaf = FALLBACK_LEAF
            final_root = final_root.with_name(leaf)
        if utf16_len(str(final_root)) > dir_max:
            raise OSError(errno.ENAMETOOLONG, "the data folder's path is too long for a snapshot")
        partial = repos / PARTIAL
        partial.mkdir(parents=True, exist_ok=True)
        root = partial / secrets.token_hex(RAND_BYTES)
        root.mkdir()
        return Staging(owner, repo, sha, root, root / leaf, final_root, leaf, display)

    def discard(self, staging: Staging) -> None:
        """Throw a download away (cancelled or failed). What cannot be deleted now goes at the start-up
        sweep, an hour later."""
        if not _rmtree(staging.root, self._sleep):
            log.warning("a cancelled download could not be fully deleted yet")

    def publish(self, staging: Staging, result: Result, *, private: bool, user_id: int | None = None,
                branch: str = "", default_branch: str | None = None) -> Snapshot:
        """Make a finished download the snapshot of its commit, then do what follows every publish: record
        branch -> sha in refs.json, prune this repo's old commits and enforce the global cap.

        - snapshot.json is written into the staging folder, then the folder is renamed into place in one
          os.replace (same volume, atomic), retried with backoff for up to 30 s while files are in use.
        - If the same commit is already there (another job or process), that one is kept and ours deleted,
          unless it was a private copy for another login, which ours replaces.
        Raises OSError (the caller discards the staging folder) and ValueError for bad names.
        """
        if branch:
            _check_branch(branch)
        if user_id is not None and (not isinstance(user_id, int) or isinstance(user_id, bool)):
            raise ValueError("user_id is not valid")
        if not (isinstance(default_branch, str) and default_branch):
            default_branch = None
        now = self._clock()
        meta = {"v": VERSION, "owner": staging.owner, "repo": staging.repo, "sha": staging.sha,
                "private": bool(private), "user_id": user_id, "branch": branch, "default_branch": default_branch,
                "leaf": staging.leaf, "display": staging.display, "created": now, "opened": now,
                **result.to_dict()}
        _write_json(staging.root / META, meta, self._sleep)
        snap = self._move_into_place(staging, meta)
        keep = {_norm(snap.root)}
        steps = [("record", lambda: self.record(staging.owner, staging.repo, branch=branch, sha=staging.sha,
                                                private=private, default_branch=default_branch))] if branch else []
        steps += [("prune", lambda: self.prune_repo(staging.owner, staging.repo, keep=keep)),
                  ("cap", lambda: self.enforce_cap(keep=keep))]
        for name, step in steps:  # the snapshot is published: these only tidy up
            try:
                step()
            except (OSError, ValueError) as exc:
                log.warning("after publishing a snapshot, %s failed (%s)", name, type(exc).__name__)
        return snap

    def _move_into_place(self, staging: Staging, meta: dict) -> Snapshot:
        target = self._folder(staging.owner, staging.repo, staging.sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        deadline = self._clock() + PUBLISH_SECONDS
        delay = 0.05
        while True:
            with self._lock:
                existing = self._load(target) if os.path.lexists(target) else None
                if (existing is not None and existing.sha == staging.sha and existing.private == meta["private"]
                        and (not existing.private or existing.user_id == meta["user_id"])):
                    break  # the same commit is already there: keep it, ours goes
                if os.path.lexists(target):
                    self._delete_folder(target)  # half-deleted leftovers, or another login's private copy
                if not os.path.lexists(target):
                    try:
                        os.replace(staging.root, target)
                    except OSError as exc:
                        # in use (antivirus scanning the new files), or the target appeared meanwhile
                        busy = isinstance(exc, PermissionError) or exc.errno in (errno.EEXIST, errno.ENOTEMPTY)
                        if not busy or self._clock() >= deadline:
                            raise
                    else:
                        snap = self._load(target)
                        if snap is None:
                            raise OSError(errno.EIO, "the published snapshot cannot be read")
                        return snap
                elif self._clock() >= deadline:
                    raise OSError(errno.EEXIST, "an old snapshot folder is in the way")
            self._sleep(delay)
            delay = min(delay * 2, 2.0)
        self.discard(staging)
        return existing

    # -- refs.json --

    def _refs_path(self, owner: str, repo: str) -> Path:
        return self.repos_dir() / repo_key(owner, repo) / REFS

    def _read_refs(self, owner: str, repo: str) -> dict:
        data = _read_json(self._refs_path(owner, repo))
        if not isinstance(data, dict) or data.get("v") != VERSION:
            return {}
        if not (_plain(data.get("owner")) and _plain(data.get("repo"))
                and data["owner"].lower() == owner.lower() and data["repo"].lower() == repo.lower()):
            return {}
        branches = {}
        raw = data.get("branches")
        for name, ref in (raw.items() if isinstance(raw, dict) else ()):
            if isinstance(name, str) and name and isinstance(ref, dict) and _valid_sha(ref.get("sha")):
                branches[name] = {"sha": ref["sha"], "checked_at": _number(ref.get("checked_at"))}
        default = data.get("default_branch")
        return {"v": VERSION, "owner": data["owner"], "repo": data["repo"],
                "private": data.get("private") if isinstance(data.get("private"), bool) else None,
                "default_branch": default if isinstance(default, str) and default else None,
                "branches": branches}

    def refs(self, owner: str, repo: str) -> dict:
        """{'v', 'owner', 'repo', 'private', 'default_branch', 'branches': {name: {'sha', 'checked_at'}}},
        or {} when nothing is recorded (or the file is broken)."""
        if not (_plain(owner) and _plain(repo)):
            return {}
        return self._read_refs(owner, repo)

    def record(self, owner: str, repo: str, *, branch: str, sha: str, private: bool,
               default_branch: str | None = None) -> dict:
        """Remember branch -> sha (and the repo's privacy and default branch). Atomic. Returns the refs."""
        _check(owner, repo, sha)
        _check_branch(branch)
        with self._lock:
            refs = self._read_refs(owner, repo) or {"v": VERSION, "default_branch": None, "branches": {}}
            refs.update(owner=owner, repo=repo, private=bool(private))
            if default_branch:
                refs["default_branch"] = default_branch
            branches = refs["branches"]
            branches.pop(branch, None)
            branches[branch] = {"sha": sha, "checked_at": self._clock()}
            if len(branches) > MAX_BRANCHES:
                for name in sorted(branches, key=lambda b: branches[b]["checked_at"])[:len(branches) - MAX_BRANCHES]:
                    del branches[name]
            path = self._refs_path(owner, repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(path, refs, self._sleep)
            return json.loads(json.dumps(refs))

    def opened(self, owner: str, repo: str, sha: str) -> None:
        """Note that the snapshot was just opened (the global cap deletes the least recently opened first)."""
        if not (_plain(owner) and _plain(repo) and _valid_sha(sha)):
            return
        with self._lock:
            folder = self._folder(owner, repo, sha)
            meta = _read_json(folder / META)
            if not isinstance(meta, dict) or meta.get("sha") != sha or not (folder / META).is_file():
                return
            meta["opened"] = self._clock()
            try:
                _write_json(folder / META, meta, self._sleep)
            except OSError as exc:  # deleted meanwhile, or held by the antivirus: only the order of pruning
                log.warning("could not note that a snapshot was opened (%s)", type(exc).__name__)

    # -- cleanup --

    def _protected(self, in_use, keep: Iterable) -> set[str] | None:
        """Normalized roots that must stay, or None when in_use cannot tell (then nothing is deleted)."""
        source = in_use if in_use is not None else self._in_use
        try:
            roots = source() if callable(source) else (source or ())
            out = {_norm(p) for p in roots}
        except Exception as exc:  # noqa: BLE001 - whatever the AI side raises, deleting is not safe then
            log.warning("could not tell which snapshots are in use (%s)", type(exc).__name__)
            return None
        return out | {_norm(p) for p in keep}

    def _busy(self, snap: Snapshot, protected: set[str]) -> bool:
        return _norm(snap.root) in protected or self._clock() - max(snap.opened, snap.created) < RECENT_SECONDS

    def prune_repo(self, owner: str, repo: str, in_use=None, keep: Iterable = ()) -> int:
        """Delete this repo's snapshots that are no branch's current sha in refs.json, except the ones in use
        or opened in the last 10 minutes. Without refs.json nothing is deleted. Returns how many went."""
        refs = self.refs(owner, repo)
        if not refs.get("branches"):
            return 0
        current = {ref["sha"] for ref in refs["branches"].values()}
        protected = self._protected(in_use, keep)
        if protected is None:
            return 0
        gone = 0
        for snap in self._repo_snapshots(owner, repo):
            if snap.sha not in current and not self._busy(snap, protected):
                self._delete_folder(snap.folder)
                gone += 1
        return gone

    def enforce_cap(self, in_use=None, keep: Iterable = (), *, max_bytes: int | None = None,
                    max_count: int | None = None) -> int:
        """Keep all snapshots together within max_bytes (bytes written, default 2 GiB) and max_count (30),
        deleting the least recently opened first, never one in use or opened in the last 10 minutes.
        Returns how many went."""
        max_bytes = MAX_TOTAL_BYTES if max_bytes is None else max_bytes
        max_count = MAX_SNAPSHOTS if max_count is None else max_count
        snaps = self.snapshots()
        total, count = sum(s.size for s in snaps), len(snaps)
        if total <= max_bytes and count <= max_count:
            return 0
        protected = self._protected(in_use, keep)
        if protected is None:
            return 0
        gone = 0
        for snap in sorted(snaps, key=lambda s: (max(s.opened, s.created), s.sha)):
            if total <= max_bytes and count <= max_count:
                break
            if self._busy(snap, protected):
                continue
            self._delete_folder(snap.folder)
            total -= snap.size
            count -= 1
            gone += 1
        return gone

    def sweep(self, in_use=None) -> dict:
        """At start-up: old .partial folders, leftovers in .delete, tombstones, snapshot folders without a
        readable snapshot.json, empty repo folders, then the global cap.
        Returns {'partial', 'trash', 'broken', 'capped', 'failed'}."""
        out = dict.fromkeys(("partial", "trash", "broken", "capped", "failed"), 0)
        repos = self.repos_dir()
        if not repos.is_dir():
            return out
        now = self._clock()
        for folder in _children(repos / PARTIAL):
            try:
                age = now - folder.stat().st_mtime
            except OSError:
                continue
            if age > PARTIAL_MAX_AGE:
                out["partial" if _rmtree(folder, self._sleep) else "failed"] += 1
        for folder in _children(repos / TRASH):
            out["trash" if _rmtree(folder, self._sleep) else "failed"] += 1
        out["failed"] += self._retry_tombstones()
        for key_dir in _children(repos, _KEY):
            for folder in _children(key_dir, _SHA12):
                if self._load(folder, require_leaf=True) is None:
                    if self._delete_folder(folder):
                        out["broken"] += 1
                    else:
                        out["failed"] += 1
            try:
                key_dir.rmdir()  # only when it is empty
            except OSError:
                pass
        out["capped"] = self.enforce_cap(in_use)
        return out

    def delete_private(self, keep_user_id: int | None = None) -> dict:
        """Logout (keep_user_id None): delete every private snapshot and the refs.json of private repos, even
        ones in use. Account switch (keep_user_id = the new login): delete the private snapshots of every
        other login. Snapshot folders without a readable snapshot.json go too: they may hold private files.
        Returns {'snapshots': deleted, 'failed': left on disk for the next start}."""
        out = {"snapshots": 0, "failed": 0}
        repos = self.repos_dir()
        if not repos.is_dir():
            return out
        for key_dir in _children(repos, _KEY):
            kept_private = False
            for folder in _children(key_dir, _SHA12):
                snap = self._load(folder, require_leaf=False)
                if snap is not None and not snap.private:
                    continue
                if snap is not None and keep_user_id is not None and snap.user_id == keep_user_id:
                    kept_private = True
                    continue
                ok = self._delete_folder(folder, MOVE_SECONDS)
                if snap is not None:
                    out["snapshots" if ok else "failed"] += 1
                elif not ok:
                    out["failed"] += 1
            refs = _read_json(key_dir / REFS)
            if isinstance(refs, dict) and refs.get("private") is not False and not kept_private:
                try:
                    (key_dir / REFS).unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.warning("could not delete a private repo's refs.json (%s)", type(exc).__name__)
            try:
                key_dir.rmdir()
            except OSError:
                pass
        return out

    def delete(self, snapshot: Snapshot) -> bool:
        """Delete one snapshot now (rename into .delete, then delete). True when nothing is left."""
        if snapshot.folder is None:
            return False
        return self._delete_folder(snapshot.folder, MOVE_SECONDS)

    def forget_private_refs(self, owner: str, repo: str) -> None:
        """Delete the repo's refs.json when it says private and no private snapshot of the repo is left: a
        logout that came while a private download was being published, which recorded the branch after
        delete_private() had run, must not leave the private repo's name behind."""
        if not (_plain(owner) and _plain(repo)):
            return
        with self._lock:
            if any(snap.private for snap in self._repo_snapshots(owner, repo)):
                return
            path = self._refs_path(owner, repo)
            refs = _read_json(path)
            if isinstance(refs, dict) and refs.get("private") is not False:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.warning("could not delete a private repo's refs.json (%s)", type(exc).__name__)
            try:
                path.parent.rmdir()  # only when it is empty
            except OSError:
                pass

    def _delete_folder(self, folder: Path, seconds: float = 0.5) -> bool:
        """Rename the folder into repos/.delete/<12 hex> (atomic, so readers never see half of it), then delete
        it. When the rename keeps failing (a file in it is open), snapshot.json goes first, the rest is
        deleted in place, and what is left becomes a tombstone for the next start. True when nothing is left."""
        trash_root = self.repos_dir() / TRASH
        try:
            trash_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        trash = trash_root / secrets.token_hex(RAND_BYTES)
        waited, delay = 0.0, 0.05
        while True:
            try:
                os.replace(folder, trash)
                break
            except FileNotFoundError:
                return not os.path.lexists(folder)
            except OSError:
                if waited >= seconds:
                    trash = None
                    break
                self._sleep(delay)
                waited += delay
                delay = min(delay * 2, 0.5)
        if trash is not None:
            return _rmtree(trash, self._sleep)  # leftovers stay in .delete for the sweep
        try:
            (folder / META).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete a snapshot's snapshot.json (%s)", type(exc).__name__)
        if _rmtree(folder, self._sleep):
            return True
        self._add_tombstone(folder)
        return False

    # -- tombstones --

    def _tombstones(self) -> list[str]:
        data = _read_json(self.repos_dir() / TOMBSTONES)
        paths = data.get("paths") if isinstance(data, dict) else None
        return [p for p in (paths if isinstance(paths, list) else []) if isinstance(p, str) and _TOMBSTONE.fullmatch(p)]

    def _save_tombstones(self, paths: list[str]) -> None:
        path = self.repos_dir() / TOMBSTONES
        if paths:
            _write_json(path, {"v": VERSION, "paths": sorted(set(paths))}, self._sleep)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _add_tombstone(self, folder: Path) -> None:
        try:
            rel = folder.relative_to(self.repos_dir()).as_posix()
        except ValueError:
            return
        if not _TOMBSTONE.fullmatch(rel):
            return
        with self._lock:
            try:
                self._save_tombstones(self._tombstones() + [rel])
            except OSError as exc:
                log.warning("could not write a tombstone (%s)", type(exc).__name__)

    def _retry_tombstones(self) -> int:
        """Delete what earlier deletes left; returns how many are still there."""
        with self._lock:
            left = []
            for rel in self._tombstones():
                folder = self.repos_dir() / rel
                if os.path.lexists(folder) and not _rmtree(folder, self._sleep):
                    left.append(rel)
            try:
                self._save_tombstones(left)
            except OSError as exc:
                log.warning("could not update the tombstones (%s)", type(exc).__name__)
            return len(left)


__all__ = [
    "NOTES", "Snapshot", "SnapshotStore", "Staging", "leaf_name", "notes", "repo_key",
    "MAX_TOTAL_BYTES", "MAX_SNAPSHOTS", "RECENT_SECONDS", "PARTIAL_MAX_AGE", "PUBLISH_SECONDS",
]
