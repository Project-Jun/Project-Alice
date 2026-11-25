"""GitHubService: one per CodeVision server. Signing in, the repo lists, and opening a GitHub branch as a local
snapshot folder that the existing analysis and AI explanations read like any other folder.

It owns the token store, the token manager, the device-flow login, the HTTP client, the snapshot store, the
60-second caches (repo list, installations, repo info, branch lists) and the download jobs.

- Thread-safe. Constructing it does no I/O. The start-up work runs once, in the daemon thread
  'codevision-github-startup', on the first call (or on start()): the snapshot sweep (old partial downloads,
  leftovers of deletes, broken folders, the global cap), then the account check for a saved login.
- Locks are held only while state is swapped: never across a network call, a refresh or a download.
  status() never asks GitHub and never waits for a refresh.
- Every public method raises only GitHubError (a fixed Korean text, our HTTP status, .reply() for the body).
- One download at a time per server, in the daemon thread 'codevision-github-download'. The job ids are
  'g' + 12 hex characters, and the 10 newest finished jobs are kept. close() stops everything through Events
  and never joins.
- open() always asks GitHub for the branch's head commit (If-None-Match carries the known commit, so an
  unchanged branch costs a 304), then reuses a snapshot of that commit, or falls back to the last snapshot of the
  branch when GitHub cannot be asked, or checks the size and the free disk space and starts a download job.
- Private snapshots open only for the GitHub user id that downloaded them (snapshot.json keeps it).
- Logout drops the login, cancels a waiting login and private downloads, clears every cache, tells the server
  through on_private_gone (so it can evict AI projects and cancel AI jobs on those snapshots), then deletes the
  private snapshots. A login whose user id differs from the one stored with private snapshots is an account
  switch: those snapshots go the same way.

on_private_gone(project_ids: frozenset[str], reason: str) is the server's hook. project_ids are the AI project
ids ('github:<owner_lc>/<repo_lc>@<sha>') of the private snapshots about to be deleted (or, on a logout with
delete_private=False, about to become unreadable); reason is 'logout' (every logout, the set may be empty) or
'switch' (an account switch that found another login's private snapshots). It is called before the folders are
deleted, from the thread doing the logout or the login, with no lock of this service held. It should return
quickly; whatever it raises is logged by type and ignored.

Logs carry only operation names, our error codes, counts and exception type names: never URLs, names of
private repos, tokens or codes.
"""
from __future__ import annotations

import collections
import copy
import functools
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import messages as M
from .auth import DeviceLogin, TokenManager
from .base import (GitHubError, Hosts, hosts_from_env, project_id, valid_branch, valid_owner, valid_repo,
                   valid_sha)
from .client import GitHubClient
from .extract import (ArchiveTooBig, BadArchive, Cancelled, DiskFull, DownloadStalled, ExtractError, GiB, Limits,
                      MiB, TooManyFiles, extract_stream, is_disk_full)
from .repos import (RepoInfo, head_sha, installation_notes, list_branches, list_installations, list_repos,
                    parse_repo_input, pick_branch, repo_info, whoami)
from .snapshot import Snapshot, SnapshotStore
from .store import TokenStore

CACHE_SECONDS = 60.0        # repo list, installations, repo info and branch lists are kept this long
MAX_CACHE = 500             # cache entries at most (expired ones go first)
MAX_FINISHED_JOBS = 10      # finished download jobs kept for /api/github/job
FREE_NEEDED = 1 * GiB       # free disk space a download needs
FLOW_MAX_AGE = 300.0        # an ended login flow stays in the status block this long
MAX_JOB_ID = 64
JOB_THREAD = "codevision-github-download"
STARTUP_THREAD = "codevision-github-startup"
# GitHub could not be asked: open() then shows the last snapshot of the branch, offline.
OFFLINE_CODES = frozenset({"network", "timeout", "tls", "server_error", "rate_limited"})
REASONS = ("logout", "switch")  # what on_private_gone is told

log = logging.getLogger(__name__)


# --- small helpers ------------------------------------------------------------------------------------------

def _github_errors_only(method):
    """Public methods raise only GitHubError: anything else is logged by type and given a fixed text."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except GitHubError:
            raise
        except OSError as exc:
            code = "disk_full" if is_disk_full(exc) else "disk"
            log.warning("GitHub %s failed: %s (%s)", method.__name__, type(exc).__name__, code)
            raise GitHubError(code, M.for_code(code)) from None
        except Exception as exc:
            log.error("GitHub %s failed: %s (internal)", method.__name__, type(exc).__name__)
            raise GitHubError("bad_reply", M.BAD_REPLY) from None

    return wrapper


def _sha_of(branches, branch: str) -> str | None:
    """The commit refs.json last recorded for a branch, or None."""
    ref = branches.get(branch) if isinstance(branches, dict) else None
    sha = ref.get("sha") if isinstance(ref, dict) else None
    return sha if valid_sha(sha) else None


def _sorted_branches(items, default: str) -> list[dict]:
    """The default branch first, then by name (as list_branches sorts when it knows the default)."""
    return sorted((dict(b) for b in items), key=lambda b: (b["name"] != default, b["name"].lower(), b["name"]))


def _size_text(n: int) -> str:
    """A size limit for the screen: '300MB', '8GB'."""
    if n >= GiB and n % GiB == 0:
        return f"{n // GiB}GB"
    if n >= MiB:
        return f"{n // MiB}MB"
    if n >= 1024:
        return f"{n // 1024}KB"
    return f"{n}B"


def _recordable(branch: str) -> bool:
    """refs.json keeps only printable branch names (git allows a few that are not, such as zero-width spaces)."""
    return isinstance(branch, str) and 0 < len(branch) <= 255 and branch.isprintable()


def _extract_error(exc: ExtractError) -> GitHubError:
    """The job's failure for an extraction that stopped (Cancelled is handled before this)."""
    if isinstance(exc, ArchiveTooBig):
        return GitHubError("too_big", M.TOO_BIG_STREAM(_size_text(exc.limit)))
    if isinstance(exc, TooManyFiles):
        return GitHubError("too_many_files", M.TOO_MANY_FILES)
    if isinstance(exc, DownloadStalled):
        return GitHubError("timeout", M.TIMEOUT)
    if isinstance(exc, DiskFull):
        return GitHubError("disk_full", M.DISK_FULL)
    if isinstance(exc, BadArchive):
        code = {"truncated": "interrupted", "commit_mismatch": "commit_mismatch"}.get(exc.reason, "bad_reply")
        return GitHubError(code, M.for_code(code))
    return GitHubError("bad_reply", M.BAD_REPLY)


def _job_failure(exc: Exception) -> tuple[str, str, str]:
    """(state, message, code) of a job that stopped with `exc`, logged by type and code only."""
    if isinstance(exc, GitHubError):
        log.info("GitHub download failed (%s)", exc.code)
        return "failed", exc.message, exc.code
    if isinstance(exc, ExtractError):
        err = _extract_error(exc)
        detail = getattr(exc, "reason", "") or getattr(exc, "what", "")
        log.warning("GitHub download stopped: %s %s (%s)", type(exc).__name__, detail, err.code)
        return "failed", err.message, err.code
    code = "disk_full" if is_disk_full(exc) else "disk"  # OSError (staging, publish) or ValueError
    log.warning("GitHub download could not be saved: %s (%s)", type(exc).__name__, code)
    return "failed", M.for_code(code), code


# --- the login's user id ------------------------------------------------------------------------------------

class _ServiceTokens(TokenManager):
    """The token manager, telling the service whenever a login's GitHub user id becomes known: after a device-flow
    login (in the login thread, before the flow says done) and when the name is fetched later (ensure_user)."""

    def __init__(self, store: TokenStore, *, on_user: Callable[[int], None], **kwargs):
        super().__init__(store, **kwargs)
        self._on_user = on_user

    def set_user(self, login: str, user_id: int) -> None:
        super().set_user(login, user_id)
        self._on_user(user_id)


# --- download jobs --------------------------------------------------------------------------------------------

@dataclass
class _Job:
    id: str
    owner: str             # GitHub's spelling
    repo: str
    branch: str            # the branch it was opened for (a label: sha picks the snapshot)
    sha: str
    private: bool
    auth: str              # 'prefer' with a login, else 'none'
    user_id: int | None    # the login that owns a private download
    default_branch: str
    note: str = ""         # a branch_gone sentence when a gone branch fell back to the default one
    also: list = field(default_factory=list)  # other branches that asked for the same commit
    state: str = "running"  # running | done | cancelled | failed
    phase: str = "download"  # download | finish
    read: int = 0          # compressed bytes so far
    total: int = 0         # Content-Length, or 0 when unknown
    files: int = 0         # files written so far
    message: str = M.JOB_DOWNLOADING
    code: str = ""
    why: str = ""          # why it was cancelled: user | logout | closing
    hidden: bool = False   # a private job after a logout or an account switch: its block names no repo
    ended: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event)
    exited: threading.Event = field(default_factory=threading.Event)  # the worker has cleaned up and ended
    download: object = None  # the client.Download being read, while there is one

    @property
    def key(self) -> tuple[str, str, str]:
        return self.owner.lower(), self.repo.lower(), self.sha

    def block(self, branch: str | None = None, note: str | None = None) -> dict:
        """The job block of /api/github/open, /job and /job/cancel (and status.job)."""
        out = {"job": self.id, "state": self.state, "phase": self.phase, "owner": self.owner, "repo": self.repo,
               "branch": self.branch if branch is None else branch, "sha": self.sha, "private": self.private,
               "read": self.read, "total": self.total, "files": self.files, "message": self.message,
               "code": self.code, "note": self.note if note is None else note}
        if self.hidden:
            out.update(owner="", repo="", branch="", sha="", note="")
        return out


# --- the service ----------------------------------------------------------------------------------------------

class GitHubService:
    """Everything GitHub for one server (see the module docstring).

    data_dir() gives the data folder (read on every use; snapshots live in <data_dir>/repos). in_use() gives the
    analysis roots that cleanup must not delete (AIService.roots_in_use). hosts: real GitHub, the fake, or GitHub
    turned off (default: hosts_from_env()). on_private_gone: the server's hook (module docstring). limits: the
    extraction caps (tests make them small). clock: for the 60-second caches and job times.
    """

    def __init__(self, data_dir: Callable[[], Path], in_use: Callable[[], Iterable] = set,
                 hosts: Hosts | None = None, *, on_private_gone: Callable[[frozenset, str], None] | None = None,
                 limits: Limits | None = None, clock: Callable[[], float] = time.monotonic):
        self.hosts = hosts if hosts is not None else hosts_from_env()
        self.store = TokenStore(fake=self.hosts.fake)
        self.tokens = _ServiceTokens(self.store, on_user=self._user_known)
        self.client = GitHubClient(self.hosts, self.tokens)
        self.tokens.bind(self.client)
        self.device = DeviceLogin(self.client, self.tokens)
        self.snapshots = SnapshotStore(data_dir, in_use)
        self.limits = limits if limits is not None else Limits()
        self._on_private_gone = on_private_gone
        self._clock = clock
        self._lock = threading.RLock()
        self._cache: dict = {}
        self._jobs: collections.OrderedDict[str, _Job] = collections.OrderedDict()
        self._closing = False
        self._started = False
        self.ready = threading.Event()  # set once the start-up work has finished (or will never run)

    # --- start-up -------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Begin the one-time start-up work now, in its daemon thread (the first call of any other method does it
        too). Never blocks."""
        self._ensure_started()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started or self._closing:
                return
            self._started = True
        try:
            threading.Thread(target=self._startup, name=STARTUP_THREAD, daemon=True).start()
        except RuntimeError:  # the interpreter is shutting down
            self.ready.set()

    def _startup(self) -> None:
        try:
            try:
                done = self.snapshots.sweep()
                if any(done.values()):
                    log.info("GitHub start-up cleanup: partial %d, trash %d, broken %d, capped %d, failed %d",
                             done["partial"], done["trash"], done["broken"], done["capped"], done["failed"])
            except Exception as exc:
                log.warning("GitHub start-up cleanup failed: %s (sweep)", type(exc).__name__)
            if self.tokens.logged_in():  # a saved login: its private snapshots of another account go
                self._user_known(self.tokens.user_id())
        except BaseException as exc:  # never a traceback with values on stderr: the type name only
            log.error("GitHub start-up thread stopped: %s (startup_crash)", type(exc).__name__)
        finally:
            self.ready.set()

    # --- status and login -------------------------------------------------------------------------------------

    @_github_errors_only
    def status(self) -> dict:
        """The status block. It never asks GitHub and never waits for a refresh."""
        self._ensure_started()
        return self._status()

    def _status(self) -> dict:
        logged_in = self.tokens.logged_in()
        flow = self.device.view(max_age=FLOW_MAX_AGE)
        if flow is not None and flow.get("state") == "done" and not logged_in:
            flow = None  # that login has ended since (a logout, or GitHub refused a refresh)
        with self._lock:
            running = self._running()
            job = running.block() if running is not None else None
        return {"logged_in": logged_in, "login": self.tokens.login() if logged_in else "",
                "store": self.store.kind, "note": self._note(), "fake": self.hosts.fake,
                "install_url": self.hosts.install_url, "settings_url": self.hosts.settings_url,
                "flow": flow, "job": job}

    def _note(self) -> str:
        if self.hosts.off:
            return M.FAKE_OFF
        return self.tokens.note() or self.store.note

    @_github_errors_only
    def login_start(self) -> dict:
        """Start a device-flow login (or re-attach to the one still waiting): the status block with the flow.
        Refused while logged in (already_logged_in): the user logs out first."""
        self._ensure_started()
        if self._closing:
            raise GitHubError("closing", M.CLOSING)
        self.device.start()
        return self._status()

    @_github_errors_only
    def login_cancel(self) -> dict:
        """Stop a waiting login: the status block (an ended flow comes back as it is)."""
        self._ensure_started()
        self.device.cancel()
        return self._status()

    @_github_errors_only
    def logout(self, delete_private: bool = True) -> dict:
        """Log out here: the status block plus removed {snapshots, failed} and the Korean message.

        It never fails because of the credential store: the tokens leave memory anyway, and the message then asks
        the user to delete 'CodeVision/github' in Credential Manager."""
        self._ensure_started()
        stored = self.tokens.forget()
        self.device.cancel()
        self._clear_caches()
        with self._lock:
            for job in list(self._jobs.values()):
                if job.state == "running" and job.private:
                    self._cancel(job, "logout")
            self._hide_private_jobs(None)
        self._tell_private_gone([s for s in self._all_snapshots() if s.private], "logout")
        removed = self._delete_private(None) if delete_private else {"snapshots": 0, "failed": 0}
        if not stored:
            message = M.LOGOUT_STORE_FAILED
        elif removed["failed"]:
            message = M.LOGOUT_PARTIAL
        else:
            message = M.LOGOUT_DONE
        out = self._status()
        out["removed"] = removed
        out["message"] = message
        return out

    # --- the account-switch rule ------------------------------------------------------------------------------------

    def _user_known(self, user_id: int) -> None:
        """A login's user id is known (a finished login, a name fetched later, or a saved login at start-up).
        Private snapshots of any other user id are an account switch: they go as on logout. Never raises."""
        try:
            if self.tokens.logged_in() and self.tokens.user_id() == user_id:
                self._account_check(user_id)
        except Exception as exc:
            log.warning("GitHub account check failed: %s (account_check)", type(exc).__name__)

    def _account_check(self, user_id: int) -> None:
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            return
        with self._lock:
            self._hide_private_jobs(user_id)
        others = [s for s in self._all_snapshots() if s.private and s.user_id != user_id]
        if not others:
            return
        log.info("GitHub login is another account; %d private snapshot(s) of the other account go (account_switch)",
                 len(others))
        self._clear_caches()
        with self._lock:
            for job in list(self._jobs.values()):
                if job.state == "running" and job.private and job.user_id != user_id:
                    self._cancel(job, "logout")
            self._hide_private_jobs(user_id)
        self._tell_private_gone(others, "switch")
        self._delete_private(user_id)

    def _hide_private_jobs(self, keep_user_id: int | None) -> None:
        """Finished private jobs (of every login, or of every login but keep_user_id) stop naming their repo: a
        logout leaves no private trace, and a job block is one. The lock is held."""
        for job in self._jobs.values():
            if job.private and job.state != "running" and (keep_user_id is None or job.user_id != keep_user_id):
                job.hidden = True

    def _tell_private_gone(self, snaps: list[Snapshot], reason: str) -> None:
        hook = self._on_private_gone
        if hook is None:
            return
        ids = set()
        for snap in snaps:
            try:
                ids.add(project_id(snap.owner, snap.repo, snap.sha))
            except ValueError:
                continue
        try:
            hook(frozenset(ids), reason)
        except Exception as exc:
            log.warning("the server could not let go of private GitHub projects: %s (private_gone)",
                        type(exc).__name__)

    def _all_snapshots(self) -> list[Snapshot]:
        try:
            return self.snapshots.snapshots()
        except OSError as exc:
            log.warning("GitHub snapshots could not be listed: %s (disk)", type(exc).__name__)
            return []

    def _delete_private(self, keep_user_id: int | None) -> dict:
        try:
            done = self.snapshots.delete_private(keep_user_id=keep_user_id)
        except OSError as exc:
            log.warning("GitHub private snapshots could not be deleted: %s (disk)", type(exc).__name__)
            return {"snapshots": 0, "failed": 1}
        return {"snapshots": int(done.get("snapshots", 0)), "failed": int(done.get("failed", 0))}

    # --- repos, branches, resolve -------------------------------------------------------------------------------------

    @_github_errors_only
    def repos(self, refresh: bool = False) -> dict:
        """{installed, login, repos, truncated, note}: the repos of every installation this login can see (kept
        for 60 s; refresh=True asks again). note: suspended installations, and the hint that an organization's
        owner may have to approve an install when nothing is listed. Needs a login (login_needed)."""
        self._ensure_started()
        if not self.tokens.logged_in():
            raise GitHubError("login_needed", self.tokens.login_needed_message())
        who = self._who(True)
        if not refresh:
            cached = self._get(("repos", who))
            if cached is not None:
                return copy.deepcopy(cached)
        insts = self._installations(who, refresh=refresh)
        items, installed, truncated = list_repos(self.client, installations=insts)
        login = self.tokens.ensure_user()
        notes = installation_notes(insts)
        if not installed or not items:
            notes.append(M.ORG_HINT)
        out = {"installed": installed, "login": login, "repos": items, "truncated": truncated,
               "note": " ".join(notes)}
        expires = self._clock() + CACHE_SECONDS
        self._put(("repos", who), copy.deepcopy(out), expires)
        for item in items:  # open() takes the repo info from the list instead of asking again
            info = RepoInfo(item["owner"], item["repo"], item["private"], item["default_branch"], item["size_kb"],
                            item["archived"])
            self._put(("info", who, item["owner"].lower(), item["repo"].lower()), info, expires)
        return out

    def _installations(self, who: str, refresh: bool = False) -> list:
        if not refresh:
            cached = self._get(("insts", who))
            if cached is not None:
                return list(cached)
        insts = list_installations(self.client)
        self._put(("insts", who), tuple(insts))
        return insts

    def _cached_accounts(self, who: str) -> list[str] | None:
        insts = self._get(("insts", who))
        return None if insts is None else [i.account for i in insts]

    @_github_errors_only
    def branches(self, owner, repo) -> dict:
        """{owner, repo, default_branch, branches: [{name, sha}], truncated}: the default branch first, then by
        name (kept for 60 s). default_branch is '' until the repo info is known."""
        self._ensure_started()
        self._check_names(owner, repo)
        logged_in = self.tokens.logged_in()
        refs = self.snapshots.refs(owner, repo)
        if not logged_in and refs.get("private") is True:
            raise GitHubError("private_login", M.PRIVATE_LOGIN)
        who = self._who(logged_in)
        items, truncated = self._branch_list(owner, repo, self._auth(logged_in), who, logged_in)
        info = self._get(("info", who, owner.lower(), repo.lower()))
        default = info.default_branch if info is not None else (refs.get("default_branch") or "")
        return {"owner": info.owner if info is not None else owner, "repo": info.repo if info is not None else repo,
                "default_branch": default, "branches": _sorted_branches(items, default), "truncated": truncated}

    def _branch_list(self, owner: str, repo: str, auth: str, who: str, logged_in: bool) -> tuple[list, bool]:
        key = ("branches", who, owner.lower(), repo.lower())
        cached = self._get(key)
        if cached is not None:
            items, truncated = cached
            return copy.deepcopy(items), truncated
        try:
            items, truncated = list_branches(self.client, owner, repo, auth)
        except GitHubError as exc:
            raise self._refine(exc, owner, repo, logged_in) from None
        self._put(key, (copy.deepcopy(items), truncated))
        return items, truncated

    @_github_errors_only
    def resolve(self, text) -> dict:
        """What the user typed, checked with GitHub (two requests: the repo, its branches):
        {owner, repo, private, default_branch, branch, note, branches, truncated, size_kb, too_big}."""
        self._ensure_started()
        ref = parse_repo_input(text)
        logged_in = self.tokens.logged_in()
        auth, who = self._auth(logged_in), self._who(logged_in)
        info = self._repo_info(ref.owner, ref.repo, auth, logged_in)
        items, truncated = self._branch_list(info.owner, info.repo, auth, who, logged_in)
        branch, note = pick_branch(ref.ref_hint, [b["name"] for b in items], info.default_branch)
        return {"owner": info.owner, "repo": info.repo, "private": info.private,
                "default_branch": info.default_branch, "branch": branch, "note": note,
                "branches": _sorted_branches(items, info.default_branch), "truncated": truncated,
                "size_kb": info.size_kb, "too_big": info.too_big}

    def _repo_info(self, owner: str, repo: str, auth: str, logged_in: bool) -> RepoInfo:
        who = self._who(logged_in)
        key = ("info", who, owner.lower(), repo.lower())
        info = self._get(key)
        if info is not None:
            return info
        try:
            info = repo_info(self.client, owner, repo, auth,
                             accounts=self._cached_accounts(who) if logged_in else None)
        except GitHubError as exc:
            raise self._refine(exc, owner, repo, logged_in) from None
        self._put(key, info)
        if (info.owner.lower(), info.repo.lower()) != (owner.lower(), repo.lower()):  # renamed: both names
            self._put(("info", who, info.owner.lower(), info.repo.lower()), info)
        return info

    def _refine(self, exc: GitHubError, owner: str, repo: str, logged_in: bool) -> GitHubError:
        """A not_found said more exactly: private_login for a repo known to be private while logged out;
        other_owner, while logged in, for an owner that is neither the login nor an installation account."""
        if exc.code != "not_found":
            return exc
        if not logged_in:
            if self.snapshots.refs(owner, repo).get("private") is True:
                return GitHubError("private_login", M.PRIVATE_LOGIN, gh_status=exc.gh_status)
            return exc
        who = self._who(True)
        accounts = self._cached_accounts(who)
        if accounts is None:
            try:
                accounts = [i.account for i in self._installations(who)]
            except GitHubError:
                return exc
        known = {a.lower() for a in accounts if isinstance(a, str)} | {self.tokens.login().lower()}
        known.discard("")
        if owner.lower() in known:
            return exc
        return GitHubError("other_owner", M.other_owner(owner), gh_status=exc.gh_status,
                           install_url=self.hosts.install_url)

    # --- opening a branch -----------------------------------------------------------------------------------------

    @_github_errors_only
    def open(self, owner, repo, branch="") -> tuple[int, dict]:
        """Open a branch ('' is the default branch): (200, done block) when a snapshot of its head commit is here
        (or GitHub cannot be asked and the last one is used, offline), (202, job block) for a new download,
        (200, job block) when that commit is already downloading."""
        self._ensure_started()
        if self._closing:
            raise GitHubError("closing", M.CLOSING)
        branch = "" if branch is None else branch
        if not (valid_owner(owner) and valid_repo(repo)) or not isinstance(branch, str) \
                or (branch and not valid_branch(branch)):
            raise GitHubError("bad_request", M.BAD_REQUEST)
        logged_in = self.tokens.logged_in()
        auth, who = self._auth(logged_in), self._who(logged_in)
        refs = self.snapshots.refs(owner, repo)
        known = refs.get("branches") or {}
        info = self._get(("info", who, owner.lower(), repo.lower()))
        # 1. a repo known to be private needs a login: said without asking GitHub
        if not logged_in and (refs.get("private") is True or (info is not None and info.private)):
            raise GitHubError("private_login", M.PRIVATE_LOGIN)
        asked, note = branch, ""
        # 2. the branch, and its head commit: always asked, with the known commit as If-None-Match
        if not branch:
            if info is None:
                try:
                    info = self._repo_info(owner, repo, auth, logged_in)
                except GitHubError as exc:
                    fallback = refs.get("default_branch")
                    reply = self._offline(owner, repo, fallback, _sha_of(known, fallback), exc, logged_in) \
                        if exc.code in OFFLINE_CODES and fallback else None
                    if reply is None:
                        raise
                    return 200, reply
            branch = info.default_branch
            if not branch:
                raise GitHubError("branch_not_found", M.BRANCH_MISSING)
        try:
            sha, offline = self._head(owner, repo, branch, auth, known, logged_in)
        except GitHubError as exc:
            if not (exc.code == "branch_not_found" and asked and asked in known):
                raise self._refine(exc, owner, repo, logged_in) from None
            # a branch opened before (a recent entry) is gone: its default branch, with a note
            if info is None:
                info = self._repo_info(owner, repo, auth, logged_in)
            if not info.default_branch or info.default_branch == asked:
                raise
            note = M.branch_gone(asked, info.default_branch)
            branch = info.default_branch
            try:
                sha, offline = self._head(owner, repo, branch, auth, known, logged_in)
            except GitHubError as again:
                raise self._refine(again, owner, repo, logged_in) from None
        if offline is not None:
            offline["note"] = note
            return 200, offline
        # 3. reuse: a snapshot of that commit is here (under GitHub's spelling when known: a renamed repo)
        name = (info.owner, info.repo) if info is not None else (owner, repo)
        uid = self._uid(logged_in)
        snap = self.snapshots.find(*name, sha)
        if snap is not None and snap.private and logged_in and uid is None:
            uid = self._ensure_uid()
        if snap is not None and snap.allowed(uid):
            message = M.REUSED if _sha_of(known, branch) == sha else M.REUSED_FIRST
            self._reused(snap, branch, info, refs)
            return 200, self._done(snap, branch, message, note=note)
        # 4. that commit is downloading already (re-attach), or another download runs (busy)
        attached = self._attach((name[0].lower(), name[1].lower(), sha), branch, note)
        if attached is not None:
            return 200, attached
        # 5. before a download: the repo info, its size, the login it needs, the free disk space
        if info is None:
            info = self._repo_info(owner, repo, auth, logged_in)
        if info.too_big:
            raise GitHubError("too_big", M.TOO_BIG)
        if info.private and not logged_in:
            raise GitHubError("private_login", M.PRIVATE_LOGIN)
        uid = self._ensure_uid() if info.private else None
        self._check_disk()
        return self._start_job(info, sha, branch, auth, uid, note)

    def _head(self, owner: str, repo: str, branch: str, auth: str, known: dict,
              logged_in: bool) -> tuple[str | None, dict | None]:
        """(sha, None) from GitHub, or (None, the offline done block) when GitHub cannot be asked and the last
        snapshot of this branch is here."""
        last = _sha_of(known, branch)
        try:
            return head_sha(self.client, owner, repo, branch, auth, known=last), None
        except GitHubError as exc:
            if exc.code in OFFLINE_CODES and last:
                reply = self._offline(owner, repo, branch, last, exc, logged_in)
                if reply is not None:
                    return None, reply
            raise

    def _offline(self, owner: str, repo: str, branch: str, sha: str | None, exc: GitHubError,
                 logged_in: bool) -> dict | None:
        if not valid_sha(sha):
            return None
        snap = self.snapshots.find(owner, repo, sha)
        if snap is None:
            return None
        if not snap.allowed(self._uid(logged_in)):
            if snap.private and not logged_in:
                raise GitHubError("private_login", M.PRIVATE_LOGIN)
            return None
        log.info("GitHub could not be asked for the head commit (%s); the last snapshot is shown (offline)",
                 exc.code)
        self._opened_quietly(snap)
        message = M.RATE_OFFLINE(sha) if exc.code == "rate_limited" else M.OFFLINE(sha)
        return self._done(snap, branch, message, offline=True)

    def _reused(self, snap: Snapshot, branch: str, info: RepoInfo | None, refs: dict) -> None:
        default = info.default_branch if info is not None else refs.get("default_branch")
        if _recordable(branch):
            try:
                self.snapshots.record(snap.owner, snap.repo, branch=branch, sha=snap.sha, private=snap.private,
                                      default_branch=default or None)
            except (OSError, ValueError) as exc:
                log.warning("GitHub branch could not be recorded: %s (disk)", type(exc).__name__)
        self._opened_quietly(snap)  # protected from pruning while the page analyzes it

    @staticmethod
    def _done(snap: Snapshot, branch: str, message: str, *, offline: bool = False, note: str = "") -> dict:
        return {"job": "", "state": "done", "owner": snap.owner, "repo": snap.repo, "branch": branch,
                "sha": snap.sha, "private": snap.private, "cached": True, "offline": offline, "message": message,
                "note": note}

    def _attach(self, key: tuple, branch: str, note: str) -> dict | None:
        with self._lock:
            running = self._running()
            if running is None:
                return None
            if running.key != key:
                raise GitHubError("busy", M.BUSY)
            if branch != running.branch and branch not in running.also:
                running.also.append(branch)
            return running.block(branch=branch, note=note)

    def _check_disk(self) -> None:
        try:
            free = self.snapshots.free_bytes()
        except OSError as exc:
            code = "disk_full" if is_disk_full(exc) else "disk"
            log.warning("GitHub snapshot folder cannot be used: %s (%s)", type(exc).__name__, code)
            raise GitHubError(code, M.for_code(code)) from None
        if free < FREE_NEEDED:
            log.info("less than 1 GiB is free for a GitHub download (disk_full)")
            raise GitHubError("disk_full", M.DISK_FULL)

    def _start_job(self, info: RepoInfo, sha: str, branch: str, auth: str, uid: int | None,
                   note: str) -> tuple[int, dict]:
        job = _Job(id="g" + secrets.token_hex(6), owner=info.owner, repo=info.repo, branch=branch, sha=sha,
                   private=info.private, auth=auth, user_id=uid, default_branch=info.default_branch, note=note)
        with self._lock:
            if self._closing:
                raise GitHubError("closing", M.CLOSING)
            running = self._running()
            if running is not None:  # another open() started one meanwhile
                if running.key != job.key:
                    raise GitHubError("busy", M.BUSY)
                if branch != running.branch and branch not in running.also:
                    running.also.append(branch)
                return 200, running.block(branch=branch, note=note)
            while job.id in self._jobs:
                job.id = "g" + secrets.token_hex(6)
            self._jobs[job.id] = job
            try:
                threading.Thread(target=self._run_job, args=(job,), name=JOB_THREAD, daemon=True).start()
            except RuntimeError:  # no new threads: the interpreter is shutting down
                del self._jobs[job.id]
                raise GitHubError("closing", M.CLOSING) from None
            return 202, job.block()

    # --- the download worker ---------------------------------------------------------------------------------------

    def _run_job(self, job: _Job) -> None:
        try:
            self._download(job)
        except BaseException as exc:  # never a traceback with values on stderr: the type name only
            log.error("GitHub download thread stopped: %s (download_crash)", type(exc).__name__)
            self._end_job(job, "failed", M.DISK, "disk")
        finally:
            job.exited.set()

    def _download(self, job: _Job) -> None:
        """open_tarball -> extract_stream into staging (the pax comment is checked against the commit) ->
        publish (it records the branch, prunes this repo and enforces the global cap)."""
        limits = self.limits
        dl = staging = None
        outcome = ("failed", M.DISK, "disk")
        try:
            dl = self.client.open_tarball(job.owner, job.repo, job.sha, auth=job.auth, stall=limits.stall_seconds,
                                          ceiling=limits.max_seconds)
            with self._lock:
                job.download = dl
                job.total = dl.length or 0
            if job.cancel.is_set():
                raise Cancelled()
            if dl.length is not None and dl.length > limits.max_compressed:
                raise ArchiveTooBig("compressed", limits.max_compressed)
            staging = self.snapshots.staging(job.owner, job.repo, job.sha)
            result = extract_stream(dl, staging.dest, limits, cancel=job.cancel,
                                    progress=lambda read, files: self._progress(job, read, files),
                                    final_root=staging.final_root, expect_commit=job.sha)
            dl.close()
            with self._lock:
                job.download = None
                if job.state == "running":
                    job.phase, job.message = "finish", M.JOB_FINISHING
                    job.read, job.files = result.read, result.files
            if job.cancel.is_set():
                raise Cancelled()
            snap = self.snapshots.publish(staging, result, private=job.private,
                                          user_id=job.user_id if job.private else None,
                                          branch=job.branch if _recordable(job.branch) else "",
                                          default_branch=job.default_branch or None)
            staging = None
            for other in list(job.also):
                if _recordable(other):
                    try:
                        self.snapshots.record(snap.owner, snap.repo, branch=other, sha=snap.sha,
                                              private=snap.private, default_branch=job.default_branch or None)
                    except (OSError, ValueError) as exc:
                        log.warning("GitHub branch could not be recorded: %s (disk)", type(exc).__name__)
            if job.cancel.is_set() and job.why == "logout":
                self.snapshots.delete(snap)  # logged out while it was being published: no private copy stays,
                self.snapshots.forget_private_refs(snap.owner, snap.repo)  # nor the refs.json that names it
                raise Cancelled()
            outcome = ("done", M.JOB_DONE, "")
        except Cancelled:
            outcome = ("cancelled", M.JOB_CANCELLED, "")
        except (GitHubError, ExtractError, OSError, ValueError) as exc:
            # once the job is cancelled, whatever the aborted download raised is only the cancel
            outcome = ("cancelled", M.JOB_CANCELLED, "") if job.cancel.is_set() else _job_failure(exc)
        finally:
            if dl is not None:
                dl.close()
            if staging is not None:
                self.snapshots.discard(staging)
            self._end_job(job, *outcome)

    def _progress(self, job: _Job, read: int, files: int) -> None:
        with self._lock:
            if job.state == "running":
                job.read, job.files = read, files

    def _end_job(self, job: _Job, state: str, message: str, code: str = "") -> None:
        with self._lock:
            job.download = None
            if job.state == "running":
                job.state, job.message, job.code = state, message, code
                if state == "done":
                    job.phase = "finish"
            if not job.ended:
                job.ended = self._clock()
            self._prune_jobs()

    def _cancel(self, job: _Job, why: str) -> None:
        """Stop a running job (the lock is held): the block says cancelled at once, and the worker, woken by
        abort(), closes the download and deletes its staging folder."""
        job.why = why
        if why == "closing":
            job.state, job.message, job.code = "cancelled", M.CLOSING, "closing"
        else:
            job.state, job.message, job.code = "cancelled", M.JOB_CANCELLED, ""
        job.ended = self._clock()
        job.cancel.set()
        dl = job.download
        if dl is not None:
            dl.abort()  # never blocks: wakes the read in the worker
        self._prune_jobs()

    def _running(self) -> _Job | None:
        return next((j for j in self._jobs.values() if j.state == "running"), None)

    def _prune_jobs(self) -> None:
        finished = [j for j in self._jobs.values() if j.state != "running"]
        if len(finished) <= MAX_FINISHED_JOBS:
            return
        finished.sort(key=lambda j: j.ended)
        for old in finished[:len(finished) - MAX_FINISHED_JOBS]:
            del self._jobs[old.id]

    # --- jobs ----------------------------------------------------------------------------------------------------

    @_github_errors_only
    def job(self, job_id) -> dict:
        """The job block (bad_request for a bad id, job_not_found when it is not kept)."""
        with self._lock:
            return self._find_job(job_id).block()

    @_github_errors_only
    def cancel_job(self, job_id) -> dict:
        """Stop a running job: the block, state cancelled. A finished job comes back unchanged."""
        with self._lock:
            job = self._find_job(job_id)
            if job.state == "running":
                self._cancel(job, "user")
            return job.block()

    def _find_job(self, job_id) -> _Job:
        if not isinstance(job_id, str) or not 1 <= len(job_id) <= MAX_JOB_ID:
            raise GitHubError("bad_request", M.BAD_REQUEST)
        job = self._jobs.get(job_id)
        if job is None:
            raise GitHubError("job_not_found", M.JOB_NOT_FOUND)
        return job

    # --- snapshots, offline ------------------------------------------------------------------------------------------

    @_github_errors_only
    def snapshot(self, owner, repo, sha) -> Snapshot | None:
        """The snapshot of this commit, offline (for /api/analyze and the AI project id), or None. A private one
        is given only to the login whose user id downloaded it."""
        self._ensure_started()
        if not (valid_owner(owner) and valid_repo(repo) and valid_sha(sha)):
            return None
        snap = self.snapshots.find(owner, repo, sha)
        if snap is None:
            return None
        if snap.private and not snap.allowed(self._uid(self.tokens.logged_in())):
            return None
        return snap

    def opened(self, snapshot: Snapshot) -> None:
        """Note that a snapshot was just shown (the global cap deletes the least recently opened first)."""
        self._opened_quietly(snapshot)

    def _opened_quietly(self, snap: Snapshot) -> None:
        try:
            self.snapshots.opened(snap.owner, snap.repo, snap.sha)
        except Exception as exc:
            log.warning("GitHub snapshot open time not recorded: %s (disk)", type(exc).__name__)

    # --- closing -----------------------------------------------------------------------------------------------------

    def close(self) -> None:
        """Server close: refuse new work, cancel a running download (state cancelled, code closing) and stop the
        login poller, all through Events. Never joins a thread and never raises."""
        try:
            with self._lock:
                self._closing = True
                if not self._started:
                    self.ready.set()
                for job in list(self._jobs.values()):
                    if job.state == "running":
                        self._cancel(job, "closing")
            self.device.close()
            self.client.close()
        except Exception as exc:
            log.warning("GitHub close: %s (close)", type(exc).__name__)

    # --- inside ----------------------------------------------------------------------------------------------------

    def _check_names(self, owner, repo) -> None:
        if not (valid_owner(owner) and valid_repo(repo)):
            raise GitHubError("bad_request", M.BAD_REQUEST)

    @staticmethod
    def _auth(logged_in: bool) -> str:
        return "prefer" if logged_in else "none"

    def _who(self, logged_in: bool) -> str:
        """Whose view a cache entry is: 'anon', or the login's user id (private data never crosses logins)."""
        return f"user:{self.tokens.user_id()}" if logged_in else "anon"

    def _uid(self, logged_in: bool) -> int | None:
        uid = self.tokens.user_id() if logged_in else 0
        return uid if uid > 0 else None

    def _ensure_uid(self) -> int:
        """The login's user id, asking GitHub (GET /user) when it is not known yet. Raises GitHubError."""
        uid = self.tokens.user_id()
        if uid > 0:
            return uid
        login, uid = whoami(self.client)
        self.tokens.set_user(login, uid)
        return uid

    def _get(self, key):
        with self._lock:
            item = self._cache.get(key)
            if item is None:
                return None
            expires, value = item
            if self._clock() >= expires:
                del self._cache[key]
                return None
            return value

    def _put(self, key, value, expires: float | None = None) -> None:
        with self._lock:
            now = self._clock()
            self._cache[key] = (now + CACHE_SECONDS if expires is None else expires, value)
            if len(self._cache) > MAX_CACHE:
                for old in [k for k, (until, _) in self._cache.items() if until <= now]:
                    del self._cache[old]
                while len(self._cache) > MAX_CACHE:
                    del self._cache[next(iter(self._cache))]

    def _clear_caches(self) -> None:
        with self._lock:
            self._cache.clear()


__all__ = ["GitHubService", "CACHE_SECONDS", "FREE_NEEDED", "MAX_FINISHED_JOBS", "JOB_THREAD", "STARTUP_THREAD",
           "OFFLINE_CODES", "REASONS"]
