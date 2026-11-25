"""What the user typed, the repos and branches a login can see, and a branch's head commit.

Pasted addresses are parsed, never fetched or logged; names that come back from GitHub are validated before
they are used anywhere.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass

from . import messages as M
from .base import GitHubError, valid_branch, valid_owner, valid_repo, valid_sha
from .client import SHA_ACCEPT, GitHubClient

MAX_INPUT = 2000
TOO_BIG_KB = 2_000_000  # repos above this size_kb are refused before any download
MAX_REPOS = 1000
MAX_BRANCHES = 1000
MAX_INSTALLATIONS = 100

WEB_HOSTS = ("github.com", "www.github.com")
SSH_HOSTS = ("github.com", "ssh.github.com")
# First path parts of github.com pages that are not "owner/repo" (GitHub reserves these names).
RESERVED_OWNERS = frozenset({
    "about", "account", "apps", "codespaces", "collections", "contact", "customer-stories", "dashboard",
    "enterprise", "enterprises", "events", "explore", "features", "gist", "gists", "issues", "join", "login",
    "logout", "marketplace", "new", "notifications", "organizations", "orgs", "pricing", "pulls", "search",
    "security", "sessions", "settings", "signup", "site", "sponsors", "stars", "topics", "trending", "watching",
})
_PAIRS = {'"': '"', "'": "'", "`": "`", "<": ">", "(": ")", "“": "”", "‘": "’"}
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoRef:
    """What the user typed: owner and repo as typed, and the /tree/ or /blob/ rest ('' when none)."""

    owner: str
    repo: str
    ref_hint: str = ""


@dataclass(frozen=True)
class RepoInfo:
    """GET /repos/{owner}/{repo}, with GitHub's canonical case."""

    owner: str
    repo: str
    private: bool
    default_branch: str
    size_kb: int
    archived: bool

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def too_big(self) -> bool:
        return self.size_kb > TOO_BIG_KB


@dataclass(frozen=True)
class Installation:
    """One installation of the CodeVision app that the login can see."""

    id: int
    account: str
    account_type: str  # "User" | "Organization"
    suspended: bool
    selection: str  # "all" | "selected"


def _q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


# --- what the user typed -------------------------------------------------------------------------------

def _bad(message: str = M.BAD_INPUT) -> GitHubError:
    return GitHubError("bad_input", message)


def _from_path(path: str, *, hints: bool) -> RepoRef:
    path = path.split("#", 1)[0].split("?", 1)[0]
    segments = [s for s in path.strip("/").split("/")] if path.strip("/") else []
    if len(segments) < 2 or "" in segments[:2]:
        raise _bad()
    owner, repo = urllib.parse.unquote(segments[0]), urllib.parse.unquote(segments[1])
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not valid_owner(owner) or not valid_repo(repo) or owner.lower() in RESERVED_OWNERS:
        raise _bad()
    hint = ""
    if hints and len(segments) >= 4 and segments[2] in ("tree", "blob"):
        hint = urllib.parse.unquote("/".join(segments[3:]))
    return RepoRef(owner, repo, hint)


def parse_repo_input(text) -> RepoRef:
    """owner/repo, github.com/owner/repo, http(s)://(www.)github.com/owner/repo/… (any page of the repo:
    /tree/ and /blob/ give a branch hint, other suffixes are ignored, .git and a trailing slash too),
    git@github.com:owner/repo(.git) and ssh://git@github.com/owner/repo(.git). Query and fragment are ignored;
    spaces and quotes around the text are trimmed. Raises GitHubError bad_input or not_github."""
    if not isinstance(text, str) or len(text) > MAX_INPUT:
        raise _bad()
    s = text.strip()
    while len(s) >= 2 and s[0] in _PAIRS and s[-1] == _PAIRS[s[0]]:
        s = s[1:-1].strip()
    if not s:
        raise _bad(M.EMPTY_INPUT)
    if any(ch.isspace() or not ch.isprintable() for ch in s):
        raise _bad()
    if s.lower().startswith("git@"):  # scp-like SSH: git@github.com:owner/repo.git
        host, sep, path = s[4:].partition(":")
        if not sep:
            raise _bad()
        if host.lower() not in SSH_HOSTS:
            raise GitHubError("not_github", M.NOT_GITHUB)
        return _from_path(path, hints=False)
    if "://" in s:
        try:
            parts = urllib.parse.urlsplit(s)
            port = parts.port
        except ValueError:
            raise _bad() from None
        scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
        if scheme in ("http", "https"):
            if parts.username is not None or parts.password is not None or "@" in parts.netloc:
                raise _bad()  # user:password@ is never accepted
            if host not in WEB_HOSTS or port not in (None, 80, 443):
                raise GitHubError("not_github", M.NOT_GITHUB)
            return _from_path(parts.path, hints=True)
        if scheme in ("ssh", "git+ssh", "git"):
            if parts.password is not None or parts.username not in (None, "git"):
                raise _bad()
            if host not in SSH_HOSTS or port not in (None, 22, 443):
                raise GitHubError("not_github", M.NOT_GITHUB)
            return _from_path(parts.path, hints=False)
        raise GitHubError("not_github", M.NOT_GITHUB)
    first, _, rest = s.partition("/")
    if "." in first or ":" in first or "@" in first:  # a host name in front: github.com/owner/repo
        host = first.lower()
        if host not in WEB_HOSTS:
            raise GitHubError("not_github", M.NOT_GITHUB)
        return _from_path(rest, hints=True)
    return _from_path(s, hints=True)


def pick_branch(ref_hint: str, names, default: str) -> tuple[str, str]:
    """(branch, note): the longest prefix of the /tree/ hint that is a real branch, else the default branch,
    with a note when a hint was given but matched nothing."""
    if not ref_hint:
        return default, ""
    known = set(names)
    parts = ref_hint.strip("/").split("/")
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i])
        if candidate in known:
            return candidate, ""
    return default, M.branch_default(default)


# --- what a login can see -----------------------------------------------------------------------------------

def list_installations(client: GitHubClient) -> list[Installation]:
    """GET /user/installations: the app's installations this login can see (needs a login)."""
    items, _ = client.paginate("/user/installations", auth="require", item_key="installations",
                               max_items=MAX_INSTALLATIONS, op="installations")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        inst_id, account = item.get("id"), item.get("account")
        login = account.get("login") if isinstance(account, dict) else None
        if isinstance(inst_id, bool) or not isinstance(inst_id, int) or inst_id <= 0 or not valid_owner(login):
            continue
        kind = account.get("type") if isinstance(account.get("type"), str) else "User"
        selection = item.get("repository_selection") if item.get("repository_selection") in ("all", "selected") else "all"
        out.append(Installation(inst_id, login, kind, item.get("suspended_at") is not None, selection))
    return out


def installation_notes(installations) -> list[str]:
    """Korean notes about installations whose repos are not listed (suspended ones)."""
    return [M.install_suspended(i.account) for i in installations if i.suspended][:3]


def repo_entry(item) -> dict | None:
    """One repo of a list, as the page gets it, or None when GitHub's item is not usable."""
    if not isinstance(item, dict):
        return None
    owner_obj = item.get("owner")
    owner = owner_obj.get("login") if isinstance(owner_obj, dict) else None
    name = item.get("name")
    if not (valid_owner(owner) and valid_repo(name)):
        full = item.get("full_name")
        owner, _, name = full.partition("/") if isinstance(full, str) else ("", "", "")
        if not (valid_owner(owner) and valid_repo(name)):
            return None
    size = item.get("size")
    size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
    branch = item.get("default_branch")
    pushed = item.get("pushed_at")
    return {"owner": owner, "repo": name, "full_name": f"{owner}/{name}", "private": item.get("private") is True,
            "default_branch": branch if valid_branch(branch) else "",
            "pushed_at": pushed if isinstance(pushed, str) and _ISO.fullmatch(pushed) else "",
            "archived": item.get("archived") is True, "size_kb": size, "too_big": size > TOO_BIG_KB}


def list_repos(client: GitHubClient, max_repos: int = MAX_REPOS, *, installations=None) -> tuple[list[dict], bool, bool]:
    """(repos, installed, truncated): every installation's repositories (suspended installations skipped),
    deduplicated by full_name, newest pushed_at first, then by full_name. installed is False when the app is
    not installed anywhere this login can see."""
    insts = list_installations(client) if installations is None else list(installations)
    found: dict[str, dict] = {}
    truncated = False
    for inst in insts:
        if inst.suspended:
            continue
        items, more = client.paginate(f"/user/installations/{inst.id}/repositories", auth="require",
                                      item_key="repositories", max_items=max_repos, op="repositories")
        truncated = truncated or more
        for item in items:
            entry = repo_entry(item)
            if entry is not None:
                found.setdefault(entry["full_name"].lower(), entry)
    repos = sorted(found.values(), key=lambda r: r["full_name"].lower())
    repos.sort(key=lambda r: r["pushed_at"], reverse=True)  # stable: names stay sorted within one time
    if len(repos) > max_repos:
        repos, truncated = repos[:max_repos], True
    return repos, bool(insts), truncated


# --- one repo ------------------------------------------------------------------------------------------------

def _other_owner(exc: GitHubError, owner: str, client: GitHubClient, accounts) -> GitHubError:
    """A not_found while logged in, for an owner that is neither the login nor an installation account,
    becomes other_owner: CodeVision's app cannot read that account's repos."""
    if exc.code != "not_found" or not client.logged_in():
        return exc
    if accounts is None:
        try:
            accounts = [i.account for i in list_installations(client)]
        except GitHubError:
            return exc
    known = {a.lower() for a in accounts if isinstance(a, str)} | {client.login().lower()}
    if owner.lower() in known:
        return exc
    return GitHubError("other_owner", M.other_owner(owner), gh_status=exc.gh_status,
                       install_url=client.hosts.install_url)


def repo_info(client: GitHubClient, owner: str, repo: str, auth: str, *, accounts=None) -> RepoInfo:
    """GET /repos/{owner}/{repo} with GitHub's canonical case. A renamed repo's 301 is followed. accounts:
    the installation accounts when known (a not_found for another account becomes other_owner)."""
    if not (valid_owner(owner) and valid_repo(repo)):
        raise _bad()
    try:
        data = client.get(f"/repos/{_q(owner)}/{_q(repo)}", auth=auth, op="repo").data
    except GitHubError as exc:
        raise _other_owner(exc, owner, client, accounts) from None
    entry = repo_entry(data)
    if entry is None:
        log.warning("GitHub repo reply is not usable (bad_reply)")
        raise GitHubError("bad_reply", M.BAD_REPLY)
    return RepoInfo(entry["owner"], entry["repo"], entry["private"], entry["default_branch"], entry["size_kb"],
                    entry["archived"])


def list_branches(client: GitHubClient, owner: str, repo: str, auth: str, max_branches: int = MAX_BRANCHES, *,
                  default: str = "") -> tuple[list[dict], bool]:
    """([{name, sha}], truncated): the default branch first, then by name. Names that are not valid git
    branch names and items without a commit sha are left out."""
    if not (valid_owner(owner) and valid_repo(repo)):
        raise _bad()
    items, truncated = client.paginate(f"/repos/{_q(owner)}/{_q(repo)}/branches", auth=auth,
                                       max_items=max_branches, op="branches")
    seen: dict[str, str] = {}
    dropped = 0
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        commit = item.get("commit") if isinstance(item, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if valid_branch(name) and valid_sha(sha):
            seen.setdefault(name, sha)
        else:
            dropped += 1
    if dropped:
        log.info("GitHub listed %d branch(es) whose names cannot be used; they are left out", dropped)
    branches = [{"name": n, "sha": s} for n, s in seen.items()]
    branches.sort(key=lambda b: (b["name"] != default, b["name"].lower(), b["name"]))
    return branches, truncated


def head_sha(client: GitHubClient, owner: str, repo: str, branch: str, auth: str, known: str | None = None) -> str:
    """The 40-character head commit of a branch: GET /repos/{o}/{r}/commits/heads/{branch} with the sha media
    type, and If-None-Match "<known>" when a sha is known (a 304 then means unchanged). 404 not_found,
    409 empty_repo, 422 branch_not_found; when the ref form fails, the branch list is asked instead."""
    if not (valid_owner(owner) and valid_repo(repo)):
        raise _bad()
    if not valid_branch(branch):
        raise GitHubError("branch_not_found", M.branch_not_found(branch))
    path = f"/repos/{_q(owner)}/{_q(repo)}/commits/heads/{urllib.parse.quote(branch, safe='/')}"
    etag = f'"{known}"' if valid_sha(known) else None
    try:
        reply = client.get(path, auth=auth, accept=SHA_ACCEPT, etag=etag, parse="text", op="head",
                           errors={409: "empty_repo", 422: "branch_not_found"})
    except GitHubError as exc:
        if exc.code != "branch_not_found":
            raise
        for item in list_branches(client, owner, repo, auth)[0]:  # an unusual name the ref form cannot reach
            if item["name"] == branch:
                return item["sha"]
        raise GitHubError("branch_not_found", M.branch_not_found(branch), gh_status=exc.gh_status) from None
    if reply.status == 304:
        return known
    sha = str(reply.data or "").strip()
    if not valid_sha(sha):
        log.warning("GitHub head reply is not a commit sha (bad_reply)")
        raise GitHubError("bad_reply", M.BAD_REPLY)
    return sha


def whoami(client: GitHubClient) -> tuple[str, int]:
    """(login, user id) of the logged-in user: GET /user."""
    data = client.get("/user", auth="require", op="user").data
    login, uid = (data.get("login"), data.get("id")) if isinstance(data, dict) else (None, None)
    if not valid_owner(login) or isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        log.warning("GitHub user reply is not usable (bad_reply)")
        raise GitHubError("bad_reply", M.BAD_REPLY)
    return login, uid
