"""github/service.py: GitHubService against tests/fake_github.py (the fake_github fixture) and a scratch data folder.

- the status block: the agreed fields, no GitHub request, never waiting for a refresh; constructing does no I/O;
- the device-flow login, cancel, a second login refused;
- repos, branches and resolve, with the 60-second caches and the private_login / other_owner wording;
- open: reuse versus download and the two reuse sentences, the job block and its progress, cancel, busy and
  re-attach, the offline and rate-limit fallbacks, private repos (the login and its user id), the size and disk
  checks, download failures as codes, a branch that is gone;
- logout and the account switch: caches cleared, private downloads cancelled, private snapshots deleted, the
  server's hook told;
- the start-up sweep and the global cap; close never joins; no token, code or download address anywhere.
"""
import io
import json
import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

import codevision.github as github
from codevision.github import auth, messages as M, service, snapshot
from codevision.github.base import GitHubError, Hosts, project_id
from codevision.github.extract import Limits, extract_stream
from codevision.github.service import STARTUP_THREAD, GitHubService

UID = 583231  # the fake's octocat
HELLO = {"README.md": "# Hello-World\n", "app.py": "print('hi')\n", "lib/greet.js": "export const x = 1;\n"}
STATUS_KEYS = ["logged_in", "login", "store", "note", "fake", "install_url", "settings_url", "flow", "job"]
JOB_KEYS = ["job", "state", "phase", "owner", "repo", "branch", "sha", "private", "read", "total", "files",
            "message", "code", "note"]
DONE_KEYS = ["job", "state", "owner", "repo", "branch", "sha", "private", "cached", "offline", "message", "note"]
RESOLVE_KEYS = ["owner", "repo", "private", "default_branch", "branch", "note", "branches", "truncated", "size_kb",
                "too_big"]


class Clock:
    """A monotonic clock the test moves on by hand (the 60-second caches)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def new(fake_github, tmp_path, monkeypatch):
    """Services on one scratch data folder, pointed at the fake (closed after the test)."""
    monkeypatch.setattr(auth, "RECHECK_WAIT", 0.0)
    made = []

    def factory(**kwargs):
        gone, in_use = [], set()
        svc = GitHubService(lambda: tmp_path / "home", in_use=lambda: set(in_use),
                            on_private_gone=lambda ids, why: gone.append((ids, why)), **kwargs)
        g = SimpleNamespace(svc=svc, fake=fake_github, home=tmp_path / "home", gone=gone, in_use=in_use)
        made.append(g)
        return g

    yield factory
    fake_github.release()
    for g in made:
        g.svc.close()


@pytest.fixture
def gh(new):
    return new()


def wait(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def finish(g, block, timeout=15.0):
    """The job's last block (a done block from open comes back as it is)."""
    if block["state"] != "running":
        return block
    return wait(lambda: (lambda b: b if b["state"] != "running" else None)(g.svc.job(block["job"])), timeout)


def opened(g, owner, repo, branch=""):
    status, block = g.svc.open(owner, repo, branch)
    return status, finish(g, block)


def sign_in(g, login="octocat") -> int:
    """Logged in as if the device flow had just finished (without running it)."""
    user = g.fake.add_user(login)
    g.svc.tokens.accept(g.fake.issue(login))
    g.svc.tokens.set_user(login, user["id"])
    return user["id"]


def flow_state(g):
    return (g.svc.status()["flow"] or {}).get("state")


def downloads(fake):
    return fake.calls("/codeload/")


def partial_empty(g) -> bool:
    folder = g.home / "repos" / ".partial"
    return not folder.exists() or not any(folder.iterdir())


def worker(g, job_id):
    return g.svc._jobs[job_id]


def reading(g, job_id):
    """Wait until the worker is blocked reading the held archive, so stopping it needs Download.abort()."""
    job = worker(g, job_id)
    wait(lambda: job.download is not None and g.fake.calls("/codeload/"))
    time.sleep(0.05)


def plant_private(g, owner, repo, user_id):
    """A private snapshot that `user_id` downloaded earlier (as if in another run)."""
    g.fake.add_repo(owner, repo, {"notes.py": "x = 1\n"}, private=True)
    sha = g.fake.head(owner, repo)
    st = g.svc.snapshots.staging(owner, repo, sha)
    result = extract_stream(io.BytesIO(g.fake.tarball(owner, repo, sha)), st.dest, final_root=st.final_root,
                            expect_commit=sha)
    return g.svc.snapshots.publish(st, result, private=True, user_id=user_id, branch="main")


def quietly(call):
    try:
        call()
    except GitHubError:
        pass


# --- construction, status, login ------------------------------------------------------------------------------

def test_the_package_exports_the_service_lazily():
    assert github.GitHubService is GitHubService
    assert github.Snapshot is snapshot.Snapshot and github.SnapshotStore is snapshot.SnapshotStore
    assert {"GitHubService", "Snapshot", "SnapshotStore", "GitHubError", "project_id", "parse_project",
            "valid_owner", "valid_repo", "valid_branch", "valid_sha", "messages"} <= set(github.__all__)


def test_constructing_the_service_does_no_io(fake_github, memory_keyring):
    def data_dir():
        raise AssertionError("the data folder is read on first use only")

    svc = GitHubService(data_dir)
    try:
        assert svc.hosts.fake and svc.hosts.api == fake_github.url + "/api"  # CODEVISION_GITHUB_FAKE
        assert not svc._started and not svc.ready.is_set() and svc.store._backend is None
        assert fake_github.calls() == [] and memory_keyring.writes == 0
    finally:
        svc.close()
    assert svc.ready.is_set()  # closed before any call: the start-up work will never run


def test_the_status_block_asks_github_nothing(gh):
    st = gh.svc.status()
    assert list(st) == STATUS_KEYS
    assert st == {"logged_in": False, "login": "", "store": "memory", "note": M.FAKE_MODE, "fake": True,
                  "install_url": gh.fake.url + "/web/apps/codevision-project-jun/installations/new",
                  "settings_url": gh.fake.url + "/web/settings/apps/authorizations", "flow": None, "job": None}
    json.dumps(st)
    assert gh.svc.ready.wait(10)
    assert gh.fake.calls() == []


def test_github_turned_off_says_so_everywhere(tmp_path):
    svc = GitHubService(lambda: tmp_path / "home", hosts=Hosts.disabled())
    try:
        st = svc.status()
        assert (st["note"], st["install_url"], st["settings_url"]) == (M.FAKE_OFF, "", "")
        for call in (svc.login_start, lambda: svc.resolve("octocat/Hello-World"),
                     lambda: svc.open("octocat", "Hello-World", "")):
            with pytest.raises(GitHubError) as caught:
                call()
            assert (caught.value.code, caught.value.message) == ("login_config", M.FAKE_OFF)
    finally:
        svc.close()


def test_the_device_flow_login_shows_in_the_status(gh):
    st = gh.svc.login_start()
    flow = st["flow"]
    assert (st["logged_in"], flow["state"], flow["message"], flow["code"]) == (False, "waiting", M.WAITING, "")
    assert flow["verification_uri"] == gh.fake.url + "/web/login/device" and len(flow["user_code"]) == 9
    assert gh.svc.login_start()["flow"]["user_code"] == flow["user_code"]  # a reloaded page re-attaches
    assert len(gh.fake.calls("/web/login/device/code")) == 1
    gh.fake.approve()
    wait(lambda: flow_state(gh) == "done")
    st = gh.svc.status()
    assert (st["logged_in"], st["login"], st["flow"]["message"]) == (True, "octocat", M.DONE)
    assert "user_code" not in st["flow"] and gh.svc.tokens.user_id() == UID
    with pytest.raises(GitHubError) as caught:
        gh.svc.login_start()
    assert (caught.value.code, caught.value.status, caught.value.message) == \
        ("already_logged_in", 409, M.ALREADY_LOGGED_IN)
    assert gh.svc.logout()["flow"] is None  # a finished login is not shown once logged out


def test_cancelling_a_login(gh):
    gh.svc.login_start()
    st = gh.svc.login_cancel()
    assert st["logged_in"] is False
    assert st["flow"] == {"state": "cancelled", "message": M.CANCELLED, "code": ""}


def test_status_never_waits_for_a_refresh(gh):
    gh.fake.set(access_ttl=60)  # less than 5 minutes left: the next request refreshes first
    sign_in(gh)
    gh.fake.hold("web")
    busy = threading.Thread(target=quietly, args=(gh.svc.repos,), daemon=True)
    busy.start()
    assert gh.fake.wait_for_calls("/web/login/oauth/access_token", 1, 10)
    started = time.monotonic()
    st = gh.svc.status()
    assert time.monotonic() - started < 0.5
    assert (st["logged_in"], st["login"]) == (True, "octocat")
    gh.fake.release()
    busy.join(10)


# --- repos, branches, resolve -----------------------------------------------------------------------------------

def test_the_repo_list_needs_a_login(gh):
    with pytest.raises(GitHubError) as caught:
        gh.svc.repos()
    assert (caught.value.code, caught.value.status, caught.value.message) == ("login_needed", 401, M.LOGIN_NEEDED)
    assert gh.fake.calls() == []


def test_the_repo_list_is_kept_for_60_seconds(new):
    clock = Clock()
    g = new(clock=clock)
    g.fake.add_repo("octocat", "Hello-World", HELLO, pushed_at="2026-11-20T00:00:00Z")
    g.fake.add_repo("octocat", "secret-diary", {"a.py": "x = 1\n"}, private=True, pushed_at="2026-11-24T00:00:00Z")
    sign_in(g)
    first = g.svc.repos()
    assert list(first) == ["installed", "login", "repos", "truncated", "note"]
    assert (first["installed"], first["login"], first["truncated"], first["note"]) == (True, "octocat", False, "")
    assert [r["full_name"] for r in first["repos"]] == ["octocat/secret-diary", "octocat/Hello-World"]
    assert first["repos"][0]["private"] is True and first["repos"][1]["default_branch"] == "main"
    asked = len(g.fake.calls("/api"))
    assert g.svc.repos() == first and len(g.fake.calls("/api")) == asked
    clock.t += 61
    assert g.svc.repos() == first and len(g.fake.calls("/api")) > asked
    asked = len(g.fake.calls("/api"))
    g.svc.repos(refresh=True)
    assert len(g.fake.calls("/api")) > asked


def test_no_installation_lists_nothing_with_the_org_hint(gh):
    gh.fake.set_installed(False)
    sign_in(gh)
    out = gh.svc.repos()
    assert (out["installed"], out["repos"], out["truncated"], out["note"]) == (False, [], False, M.ORG_HINT)


def test_a_suspended_installation_is_named_in_the_note(gh):
    gh.fake.add_org("paused-org")
    gh.fake.add_installation("paused-org", account_type="Organization", suspended=True, members=["octocat"])
    gh.fake.add_repo("paused-org", "app", {"main.py": "x = 1\n"}, private=True, collaborators=["octocat"])
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    sign_in(gh)
    out = gh.svc.repos()
    assert [r["full_name"] for r in out["repos"]] == ["octocat/Hello-World"]
    assert out["note"] == M.install_suspended("paused-org")


def test_branches_put_the_default_first_and_use_githubs_spelling(gh):
    gh.fake.add_repo("Hubot", "Tools", HELLO, branches={"main": None, "alpha": None, "Zeta": None},
                     default_branch="main")
    first = gh.svc.branches("hubot", "tools")  # the repo info is not known yet
    assert (first["owner"], first["repo"], first["default_branch"]) == ("hubot", "tools", "")
    assert [b["name"] for b in first["branches"]] == ["alpha", "main", "Zeta"]
    gh.svc.resolve("hubot/tools")
    out = gh.svc.branches("hubot", "tools")
    assert list(out) == ["owner", "repo", "default_branch", "branches", "truncated"]
    assert (out["owner"], out["repo"], out["default_branch"], out["truncated"]) == ("Hubot", "Tools", "main", False)
    assert [b["name"] for b in out["branches"]] == ["main", "alpha", "Zeta"]
    assert all(len(b["sha"]) == 40 for b in out["branches"])
    assert len([c for c in gh.fake.calls("/api") if c["path"].endswith("/branches")]) == 1  # kept for 60 s


def test_resolve_takes_the_branch_from_the_address(gh):
    gh.fake.add_repo("hubot", "Tools", HELLO, branches={"main": None, "feature/login": None})
    out = gh.svc.resolve("https://github.com/HUBOT/tools/blob/feature/login/src/app.py?plain=1")
    assert list(out) == RESOLVE_KEYS
    assert (out["owner"], out["repo"], out["private"], out["default_branch"], out["branch"], out["note"]) == \
        ("hubot", "Tools", False, "main", "feature/login", "")
    assert [b["name"] for b in out["branches"]] == ["main", "feature/login"]
    assert (out["size_kb"] >= 0, out["too_big"], out["truncated"]) == (True, False, False)
    out = gh.svc.resolve("hubot/tools/tree/no-such-branch")
    assert (out["branch"], out["note"]) == ("main", M.branch_default("main"))


@pytest.mark.parametrize("text, code", [("", "bad_input"), ("not a repo", "bad_input"),
                                        ("https://gitlab.com/a/b", "not_github")])
def test_resolve_refuses_what_is_not_a_github_repo(gh, text, code):
    with pytest.raises(GitHubError) as caught:
        gh.svc.resolve(text)
    assert (caught.value.code, caught.value.status) == (code, 400)
    assert gh.fake.calls() == []


def test_not_found_is_worded_for_the_login(gh):
    with pytest.raises(GitHubError) as caught:
        gh.svc.resolve("octocat/nothing-here")
    assert (caught.value.code, caught.value.status, caught.value.message) == ("not_found", 404, M.NOT_FOUND_ANON)
    sign_in(gh)
    with pytest.raises(GitHubError) as caught:
        gh.svc.resolve("octocat/nothing-here")
    assert (caught.value.code, caught.value.message) == ("not_found", M.NOT_FOUND_USER)


def test_a_repo_of_an_account_the_app_cannot_read_is_other_owner(gh):
    gh.fake.add_org("other-org")
    gh.fake.add_repo("other-org", "private-tool", {"tool.py": "x = 1\n"}, private=True)
    sign_in(gh)
    for call in (lambda: gh.svc.resolve("other-org/private-tool"),
                 lambda: gh.svc.open("other-org", "private-tool", "main")):
        with pytest.raises(GitHubError) as caught:
            call()
        assert (caught.value.code, caught.value.status, caught.value.message) == \
            ("other_owner", 404, M.other_owner("other-org"))
        assert caught.value.reply()["install_url"] == gh.svc.hosts.install_url


# --- open: reuse and download ---------------------------------------------------------------------------------------

def test_open_downloads_once_then_reuses_the_snapshot(gh):
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    sha = gh.fake.head("octocat", "Hello-World")
    status, block = gh.svc.open("octocat", "hello-world", "")
    assert status == 202 and list(block) == JOB_KEYS
    assert block["job"].startswith("g") and len(block["job"]) == 13 and block["state"] == "running"
    assert (block["owner"], block["repo"], block["branch"], block["sha"], block["private"]) == \
        ("octocat", "Hello-World", "main", sha, False)
    done = finish(gh, block)
    assert (done["state"], done["phase"], done["message"], done["code"], done["note"]) == \
        ("done", "finish", M.JOB_DONE, "", "")
    assert done["files"] == 3 and done["read"] == done["total"] == len(gh.fake.tarball("octocat", "Hello-World", sha))
    snap = gh.svc.snapshot("octocat", "Hello-World", sha)
    assert snap.root.name == "Hello-World" and (snap.root / "README.md").read_text(encoding="utf-8") == HELLO["README.md"]
    status, again = gh.svc.open("octocat", "Hello-World", "main")
    assert status == 200 and list(again) == DONE_KEYS
    assert again == {"job": "", "state": "done", "owner": "octocat", "repo": "Hello-World", "branch": "main",
                     "sha": sha, "private": False, "cached": True, "offline": False, "message": M.REUSED, "note": ""}
    assert len(downloads(gh.fake)) == 1
    heads = [c for c in gh.fake.calls("/api/repos/") if "/commits/heads/" in c["path"]]
    assert len(heads) == 2 and "if-none-match" not in heads[0]["headers"]
    assert heads[1]["headers"]["if-none-match"] == f'"{sha}"'  # always asked: a 304 when nothing changed


def test_a_first_open_that_finds_the_same_commit_says_so(gh):
    same = "d" * 40
    gh.fake.add_repo("octocat", "Hello-World", HELLO, branches={"main": same, "docs": same})
    opened(gh, "octocat", "Hello-World", "main")
    status, reply = gh.svc.open("octocat", "Hello-World", "docs")
    assert status == 200 and (reply["branch"], reply["sha"], reply["message"]) == ("docs", same, M.REUSED_FIRST)
    assert gh.svc.open("octocat", "Hello-World", "docs")[1]["message"] == M.REUSED
    assert len(downloads(gh.fake)) == 1


def test_a_renamed_repo_opens_under_its_new_name(gh):
    gh.fake.add_repo("octocat", "new-name", HELLO, renamed_from="octocat/old-name")
    status, done = opened(gh, "octocat", "old-name")
    assert status == 202 and (done["state"], done["owner"], done["repo"]) == ("done", "octocat", "new-name")
    status, again = gh.svc.open("octocat", "old-name", "")  # the old address finds the same snapshot
    assert status == 200 and (again["repo"], again["sha"], again["cached"]) == ("new-name", done["sha"], True)
    assert gh.svc.open("octocat", "new-name", "main")[1]["message"] == M.REUSED
    assert len(downloads(gh.fake)) == 1


def test_progress_is_reported_while_downloading(gh):
    gh.fake.add_repo("octocat", "big-files", {"data.txt": os.urandom(120_000).hex()})
    sha = gh.fake.head("octocat", "big-files")
    size = len(gh.fake.tarball("octocat", "big-files", sha))
    gh.fake.set(throttle=size)  # about a second for the whole archive
    status, block = gh.svc.open("octocat", "big-files", "")
    assert status == 202
    seen = wait(lambda: (lambda j: j if j and j["read"] > 0 else None)(gh.svc.status()["job"]))
    assert (seen["job"], seen["state"], seen["phase"], seen["total"]) == (block["job"], "running", "download", size)
    assert 0 < seen["read"] <= size and seen["message"] == M.JOB_DOWNLOADING
    done = finish(gh, block)
    assert (done["state"], done["read"], done["files"]) == ("done", size, 1)
    assert gh.svc.status()["job"] is None


def test_cancelling_a_download_stops_it_and_cleans_up(gh):
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    sha = gh.fake.head("octocat", "Hello-World")
    gh.fake.hold("codeload")
    _, block = gh.svc.open("octocat", "Hello-World", "")
    reading(gh, block["job"])
    out = gh.svc.cancel_job(block["job"])
    assert (out["state"], out["message"], out["code"]) == ("cancelled", M.JOB_CANCELLED, "")
    assert worker(gh, block["job"]).exited.wait(10)
    assert partial_empty(gh) and gh.svc.snapshots.snapshots() == []
    assert gh.svc.cancel_job(block["job"]) == out  # a finished job comes back unchanged
    gh.fake.release()
    assert gh.svc.snapshot("octocat", "Hello-World", sha) is None


def test_one_download_at_a_time_and_the_same_commit_reattaches(gh):
    same = "c" * 40
    gh.fake.add_repo("octocat", "Hello-World", HELLO, branches={"main": same, "docs": same})
    gh.fake.add_repo("octocat", "other", {"a.py": "x = 1\n"})
    gh.fake.hold("codeload")
    status, first = gh.svc.open("octocat", "Hello-World", "main")
    assert status == 202
    with pytest.raises(GitHubError) as caught:
        gh.svc.open("octocat", "other", "")
    assert (caught.value.code, caught.value.status, caught.value.message) == ("busy", 409, M.BUSY)
    status, again = gh.svc.open("octocat", "HELLO-WORLD", "docs")  # the same commit through another branch
    assert status == 200 and (again["job"], again["branch"], again["state"]) == (first["job"], "docs", "running")
    gh.fake.release()
    assert finish(gh, first)["state"] == "done"
    assert gh.svc.open("octocat", "Hello-World", "docs")[1]["message"] == M.REUSED  # both branches recorded
    assert len(downloads(gh.fake)) == 1
    assert gh.svc.open("octocat", "other", "")[0] == 202


@pytest.mark.parametrize("trouble, sentence", [({"fail": "drop", "fail_on": "api"}, M.OFFLINE),
                                               ({"rate": "primary"}, M.RATE_OFFLINE)])
def test_github_out_of_reach_shows_the_last_snapshot(gh, trouble, sentence):
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    _, done = opened(gh, "octocat", "Hello-World", "main")
    gh.fake.set(**trouble)
    for branch in ("main", ""):  # the default branch too
        status, reply = gh.svc.open("octocat", "Hello-World", branch)
        assert status == 200 and list(reply) == DONE_KEYS
        assert (reply["offline"], reply["cached"], reply["sha"], reply["branch"], reply["message"]) == \
            (True, True, done["sha"], "main", sentence(done["sha"]))
    assert len(downloads(gh.fake)) == 1


@pytest.mark.parametrize("trouble, code, status", [({"fail": "drop", "fail_on": "api"}, "network", 502),
                                                   ({"rate": "primary"}, "rate_limited", 429)])
def test_out_of_reach_without_a_snapshot_is_an_error(gh, trouble, code, status):
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    gh.fake.set(**trouble)
    with pytest.raises(GitHubError) as caught:
        gh.svc.open("octocat", "Hello-World", "main")
    assert (caught.value.code, caught.value.status) == (code, status)
    if code == "rate_limited":
        assert caught.value.reply()["retry_after"] > 0 and caught.value.message == M.rate_limited(600, True)


def test_a_branch_opened_before_that_is_gone_opens_the_default_branch(gh):
    gh.fake.add_repo("octocat", "Hello-World", HELLO, branches={"main": None, "feature/login": None})
    opened(gh, "octocat", "Hello-World", "feature/login")
    with gh.fake._lock:
        del gh.fake._repos["octocat/hello-world"].branches["feature/login"]
    note = M.branch_gone("feature/login", "main")
    status, block = gh.svc.open("octocat", "Hello-World", "feature/login")
    assert status == 202 and (block["branch"], block["note"]) == ("main", note)
    done = finish(gh, block)
    assert (done["state"], done["branch"], done["note"]) == ("done", "main", note)
    status, again = gh.svc.open("octocat", "Hello-World", "feature/login")
    assert status == 200 and (again["branch"], again["note"], again["message"]) == ("main", note, M.REUSED)
    with pytest.raises(GitHubError) as caught:  # a branch never opened here is simply not found
        gh.svc.open("octocat", "Hello-World", "never-opened")
    assert (caught.value.code, caught.value.message) == ("branch_not_found", M.branch_not_found("never-opened"))


def test_an_empty_repo_and_an_unknown_branch(gh):
    gh.fake.add_repo("octocat", "empty-repo", empty=True)
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    with pytest.raises(GitHubError) as caught:
        gh.svc.open("octocat", "empty-repo", "")
    assert (caught.value.code, caught.value.status, caught.value.message) == ("empty_repo", 422, M.EMPTY_REPO)
    with pytest.raises(GitHubError) as caught:
        gh.svc.open("octocat", "Hello-World", "nope")
    assert (caught.value.code, caught.value.status, caught.value.message) == \
        ("branch_not_found", 404, M.branch_not_found("nope"))
    assert downloads(gh.fake) == []


@pytest.mark.parametrize("args", [("../x", "Hello-World", ""), ("octocat", "..", ""), (None, "Hello-World", ""),
                                  ("octocat", "Hello-World", "bad..branch"), ("octocat", "Hello-World", "-x"),
                                  ("octocat", "Hello-World", 5)])
def test_open_refuses_bad_names_before_any_request(gh, args):
    with pytest.raises(GitHubError) as caught:
        gh.svc.open(*args)
    assert caught.value.status == 400 and caught.value.reply() == {"error": M.BAD_REQUEST}
    assert gh.fake.calls() == []


# --- open: checks before a download, and failures ---------------------------------------------------------------------

def test_a_repo_over_2_000_000_kb_is_refused_before_downloading(gh):
    gh.fake.add_repo("octocat", "too-big", {"README.md": "# huge\n"}, size_kb=2_000_001)
    with pytest.raises(GitHubError) as caught:
        gh.svc.open("octocat", "too-big", "main")
    assert (caught.value.code, caught.value.status, caught.value.message) == ("too_big", 422, M.TOO_BIG)
    assert downloads(gh.fake) == [] and not [c for c in gh.fake.calls("/api") if "/tarball/" in c["path"]]
    gh.fake.add_repo("octocat", "just-fits", {"README.md": "# ok\n"}, size_kb=2_000_000)
    assert gh.svc.open("octocat", "just-fits", "main")[0] == 202


def test_less_than_1_gib_free_is_disk_full(gh, monkeypatch):
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    monkeypatch.setattr(gh.svc.snapshots, "free_bytes", lambda: 1024 ** 3 - 1)
    with pytest.raises(GitHubError) as caught:
        gh.svc.open("octocat", "Hello-World", "")
    assert (caught.value.code, caught.value.status, caught.value.message) == ("disk_full", 500, M.DISK_FULL)
    assert downloads(gh.fake) == []


@pytest.mark.parametrize("chunked", [False, True])
def test_a_download_over_the_size_cap_stops(new, chunked):
    g = new(limits=Limits(max_compressed=4096))
    g.fake.add_repo("octocat", "big-files", {"data.txt": os.urandom(8192).hex()})
    g.fake.set(chunked=chunked)  # with and without Content-Length
    status, done = opened(g, "octocat", "big-files")
    assert status == 202
    assert (done["state"], done["code"], done["message"]) == ("failed", "too_big", M.TOO_BIG_STREAM("4KB"))
    assert g.svc.snapshots.snapshots() == [] and partial_empty(g)


@pytest.mark.parametrize("case, code", [("cut", "interrupted"), ("mismatch", "commit_mismatch"),
                                        ("files", "too_many_files"), ("stall", "timeout")])
def test_download_failures_become_codes(new, case, code):
    limits = {"files": Limits(max_files=2), "stall": Limits(stall_seconds=0.3)}.get(case)
    g = new(limits=limits) if limits else new()
    g.fake.add_repo("octocat", "Hello-World", HELLO, comment="f" * 40 if case == "mismatch" else None)
    if case == "cut":
        g.fake.set(fail="cut", fail_on="codeload")
    if case == "stall":
        g.fake.hold("codeload")  # half the archive, then nothing
    status, done = opened(g, "octocat", "Hello-World")
    assert status == 202 and (done["state"], done["code"], done["message"]) == ("failed", code, M.for_code(code))
    assert g.svc.snapshots.snapshots() == [] and partial_empty(g)


# --- private repos, logout and the account switch -------------------------------------------------------------------

def test_a_repo_known_to_be_private_needs_a_login_before_any_request(gh):
    gh.svc.snapshots.record("octocat", "secret-diary", branch="main", sha="a" * 40, private=True)
    for call in (lambda: gh.svc.open("octocat", "secret-diary", "main"),
                 lambda: gh.svc.open("octocat", "secret-diary", ""),
                 lambda: gh.svc.branches("octocat", "secret-diary")):
        with pytest.raises(GitHubError) as caught:
            call()
        assert (caught.value.code, caught.value.status, caught.value.message) == \
            ("private_login", 401, M.PRIVATE_LOGIN)
    assert gh.fake.calls() == []


def test_a_private_snapshot_opens_only_for_the_login_that_downloaded_it(gh):
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "print('dear diary')\n"}, private=True)
    sign_in(gh)
    status, done = opened(gh, "octocat", "secret-diary")
    assert status == 202 and (done["state"], done["private"]) == ("done", True)
    snap = gh.svc.snapshot("octocat", "secret-diary", done["sha"])
    assert snap is not None and snap.private and snap.user_id == UID
    other = plant_private(gh, "octocat", "their-notes", 4242)  # another login's private snapshot
    assert gh.svc.snapshot("octocat", "their-notes", other.sha) is None
    gh.svc.tokens.forget()  # the login ends without a logout: nothing is deleted, and nothing is handed out
    assert snap.root.is_dir() and gh.svc.snapshot("octocat", "secret-diary", done["sha"]) is None
    with pytest.raises(GitHubError) as caught:
        gh.svc.open("octocat", "secret-diary", "main")
    assert caught.value.code == "private_login"


def test_a_private_download_learns_the_logins_user_id_first(gh):
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "x = 1\n"}, private=True)
    gh.svc.tokens.accept(gh.fake.issue())  # logged in, but GET /user never answered
    assert gh.svc.tokens.user_id() == 0
    status, done = opened(gh, "octocat", "secret-diary")
    assert status == 202 and done["state"] == "done"
    assert gh.svc.snapshot("octocat", "secret-diary", done["sha"]).user_id == UID
    assert [c for c in gh.fake.calls("/api/user") if c["path"] == "/api/user"]


def test_a_logout_while_publishing_leaves_no_private_copy(gh, monkeypatch):
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "x = 1\n"}, private=True)
    sign_in(gh)
    real = gh.svc.snapshots.publish

    def late(*args, **kwargs):
        gh.svc.logout()  # it deletes the private snapshots just before this one is renamed into place
        return real(*args, **kwargs)

    monkeypatch.setattr(gh.svc.snapshots, "publish", late)
    _, block = gh.svc.open("octocat", "secret-diary", "")
    assert worker(gh, block["job"]).exited.wait(10)
    assert gh.svc.job(block["job"])["state"] == "cancelled"
    assert gh.svc.snapshots.snapshots() == [] and partial_empty(gh)
    # publish recorded the branch after the logout had deleted refs.json: that copy goes too
    assert gh.svc.snapshots.refs("octocat", "secret-diary") == {}
    left = [p for p in (gh.home / "repos").rglob("*") if p.is_file()]
    assert not [p for p in left if b"secret-diary" in p.read_bytes()]


def test_finished_private_jobs_stop_naming_their_repo_after_a_logout_or_an_account_switch(gh):
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "x = 1\n"}, private=True)
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    sign_in(gh)
    _, private = opened(gh, "octocat", "secret-diary")
    _, public = opened(gh, "octocat", "Hello-World")
    gh.svc.logout()
    hidden = gh.svc.job(private["job"])
    assert (hidden["state"], hidden["owner"], hidden["repo"], hidden["branch"], hidden["sha"]) == \
        ("done", "", "", "", "")
    assert gh.svc.job(public["job"])["repo"] == "Hello-World"  # a public job keeps its names
    sign_in(gh)
    _, again = opened(gh, "octocat", "secret-diary")
    assert gh.svc.job(again["job"])["repo"] == "secret-diary"  # the same account sees its own job
    sign_in(gh, "hubot")  # another account: octocat's job names nothing any more
    assert gh.svc.job(again["job"])["repo"] == "" and gh.svc.job(again["job"])["sha"] == ""


def test_another_account_logging_in_deletes_the_other_accounts_private_snapshots(gh):
    old = plant_private(gh, "octocat", "secret-diary", UID)
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    _, public = opened(gh, "octocat", "Hello-World")
    hubot = gh.fake.add_user("hubot")
    gh.svc.login_start()
    gh.fake.approve("hubot")
    wait(lambda: flow_state(gh) == "done")
    assert gh.svc.tokens.user_id() == hubot["id"]
    assert not old.folder.exists()  # gone before the flow said done
    assert gh.svc.snapshot("octocat", "Hello-World", public["sha"]) is not None
    assert gh.gone == [(frozenset({project_id("octocat", "secret-diary", old.sha)}), "switch")]
    mine = plant_private(gh, "hubot", "notes", hubot["id"])
    gh.svc.tokens.set_user("hubot", hubot["id"])  # the same account again: its own snapshots stay
    assert mine.folder.exists() and len(gh.gone) == 1


def test_a_saved_login_is_checked_against_private_snapshots_at_start_up(new):
    first = new()
    sign_in(first)  # saved in the (memory) credential store
    other = plant_private(first, "octocat", "secret-diary", 4242)
    mine = plant_private(first, "octocat", "my-notes", UID)
    second = new()  # the next run of CodeVision
    second.svc.status()
    assert second.svc.ready.wait(10)
    assert not other.folder.exists() and mine.folder.exists()
    assert second.gone == [(frozenset({project_id("octocat", "secret-diary", other.sha)}), "switch")]


def test_logout_deletes_private_snapshots_and_clears_the_caches(gh, memory_keyring):
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "x = 1\n"}, private=True)
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    sign_in(gh)
    _, private = opened(gh, "octocat", "secret-diary")
    _, public = opened(gh, "octocat", "Hello-World")
    folder = gh.svc.snapshot("octocat", "secret-diary", private["sha"]).folder
    gh.svc.repos()
    assert memory_keyring.entries()
    out = gh.svc.logout()
    assert list(out) == STATUS_KEYS + ["removed", "message"]
    assert (out["logged_in"], out["login"], out["removed"], out["message"]) == \
        (False, "", {"snapshots": 1, "failed": 0}, M.LOGOUT_DONE)
    assert memory_keyring.entries() == {}
    assert not folder.exists() and gh.svc.snapshot("octocat", "Hello-World", public["sha"]) is not None
    assert gh.svc.snapshots.refs("octocat", "secret-diary") == {}
    assert gh.gone == [(frozenset({project_id("octocat", "secret-diary", private["sha"])}), "logout")]
    sign_in(gh)
    asked = len(gh.fake.calls("/api/user/installations"))
    gh.svc.repos()
    assert len(gh.fake.calls("/api/user/installations")) > asked  # the old list was not kept


def test_logout_can_keep_private_snapshots_but_never_hands_them_out(gh):
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "x = 1\n"}, private=True)
    sign_in(gh)
    _, done = opened(gh, "octocat", "secret-diary")
    out = gh.svc.logout(delete_private=False)
    assert out["removed"] == {"snapshots": 0, "failed": 0}
    assert gh.svc.snapshots.find("octocat", "secret-diary", done["sha"]) is not None
    assert gh.svc.snapshot("octocat", "secret-diary", done["sha"]) is None
    assert gh.gone == [(frozenset({project_id("octocat", "secret-diary", done["sha"])}), "logout")]


def test_logout_cancels_a_private_download_but_not_a_public_one(gh):
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "x = 1\n"}, private=True)
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    sign_in(gh)
    gh.fake.hold("codeload")
    _, job = gh.svc.open("octocat", "secret-diary", "")
    reading(gh, job["job"])
    gh.svc.logout()
    assert gh.svc.job(job["job"])["state"] == "cancelled"
    assert worker(gh, job["job"]).exited.wait(10) and partial_empty(gh)
    gh.fake.release()
    assert gh.svc.snapshots.snapshots() == []
    sign_in(gh)
    gh.fake.hold("codeload")
    _, job = gh.svc.open("octocat", "Hello-World", "")
    assert gh.fake.wait_for_calls("/codeload/", 2, 10)
    gh.svc.logout()
    assert gh.svc.job(job["job"])["state"] == "running"
    gh.fake.release()
    assert finish(gh, job)["state"] == "done"


def test_logout_says_what_could_not_be_deleted(gh, memory_keyring, monkeypatch):
    sign_in(gh)
    monkeypatch.setattr(gh.svc.snapshots, "delete_private", lambda keep_user_id=None: {"snapshots": 1, "failed": 2})
    out = gh.svc.logout()
    assert (out["removed"], out["message"]) == ({"snapshots": 1, "failed": 2}, M.LOGOUT_PARTIAL)
    sign_in(gh)
    memory_keyring.fail.add("delete")
    out = gh.svc.logout()
    assert (out["logged_in"], out["message"]) == (False, M.LOGOUT_STORE_FAILED)


def test_a_failing_or_missing_hook_never_breaks_a_logout(fake_github, tmp_path, caplog):
    def broken(ids, why):
        raise RuntimeError("the server's hook broke")

    for hook in (broken, None):
        svc = GitHubService(lambda: tmp_path / "home", on_private_gone=hook)
        try:
            svc.tokens.accept(fake_github.issue())
            assert svc.logout()["message"] == M.LOGOUT_DONE
        finally:
            svc.close()
    assert "RuntimeError" in caplog.text and "the server's hook broke" not in caplog.text


# --- cleanup, closing, leaks, jobs -------------------------------------------------------------------------------------

def test_a_new_commit_downloads_again_and_old_ones_go_unless_in_use(gh, monkeypatch):
    monkeypatch.setattr(snapshot, "RECENT_SECONDS", 0.0)
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    _, first = opened(gh, "octocat", "Hello-World")
    gh.in_use.add(gh.svc.snapshot("octocat", "Hello-World", first["sha"]).root)
    gh.fake.push("octocat", "Hello-World", "main", {"README.md": "# v2\n"})
    status, second = opened(gh, "octocat", "Hello-World", "main")
    assert status == 202 and second["state"] == "done" and second["sha"] != first["sha"]
    assert gh.svc.snapshot("octocat", "Hello-World", first["sha"]) is not None  # in use: kept
    gh.in_use.clear()
    gh.fake.push("octocat", "Hello-World", "main", {"README.md": "# v3\n"})
    _, third = opened(gh, "octocat", "Hello-World", "main")
    assert [s.sha for s in gh.svc.snapshots.snapshots()] == [third["sha"]]


def test_the_global_cap_holds_after_every_publish(gh, monkeypatch):
    monkeypatch.setattr(snapshot, "RECENT_SECONDS", 0.0)
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOTS", 2)
    for name in ("one", "two", "three"):
        gh.fake.add_repo("octocat", name, {"README.md": f"# {name}\n"})
        assert opened(gh, "octocat", name)[1]["state"] == "done"
    assert sorted(s.repo for s in gh.svc.snapshots.snapshots()) == ["three", "two"]


def test_the_start_up_sweep_runs_once_in_a_daemon_thread(gh, monkeypatch):
    partial = gh.home / "repos" / ".partial"
    old, young = partial / "abcdefabcdef", partial / "012345012345"
    old.mkdir(parents=True)
    young.mkdir()
    two_hours_ago = time.time() - 2 * 3600
    os.utime(old, (two_hours_ago, two_hours_ago))
    seen = []
    real = gh.svc.snapshots.sweep

    def spy(in_use=None):
        seen.append((threading.current_thread().name, threading.current_thread().daemon))
        return real(in_use)

    monkeypatch.setattr(gh.svc.snapshots, "sweep", spy)
    gh.svc.status()
    gh.svc.status()
    assert gh.svc.ready.wait(10)
    assert seen == [(STARTUP_THREAD, True)]
    assert not old.exists() and young.exists()  # a young one may belong to another CodeVision
    gh.svc.snapshot("octocat", "Hello-World", "a" * 40)
    assert len(seen) == 1


def test_close_never_waits_for_a_download(gh):
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    gh.fake.hold("codeload")
    _, block = gh.svc.open("octocat", "Hello-World", "")
    reading(gh, block["job"])
    started = time.monotonic()
    gh.svc.close()
    assert time.monotonic() - started < 1.0
    out = gh.svc.job(block["job"])
    assert (out["state"], out["code"], out["message"]) == ("cancelled", "closing", M.CLOSING)
    for call in (lambda: gh.svc.open("octocat", "Hello-World", ""), gh.svc.login_start):
        with pytest.raises(GitHubError) as caught:
            call()
        assert (caught.value.code, caught.value.status) == ("closing", 503)
    assert worker(gh, block["job"]).exited.wait(10) and partial_empty(gh)


def test_no_token_code_or_download_address_leaks(gh, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    gh.fake.add_repo("octocat", "secret-diary", {"diary.py": "print('dear diary')\n"}, private=True)
    replies = [gh.svc.status(), gh.svc.login_start()]
    gh.fake.approve()
    wait(lambda: flow_state(gh) == "done")
    replies += [gh.svc.status(), gh.svc.repos()]
    _, block = gh.svc.open("octocat", "secret-diary", "")
    replies += [block, finish(gh, block), gh.svc.resolve("octocat/secret-diary"),
                gh.svc.branches("octocat", "secret-diary"), gh.svc.open("octocat", "secret-diary", "main")[1]]
    files = [p.read_bytes() for p in gh.home.rglob("*") if p.is_file()]
    replies.append(gh.svc.logout())
    secrets = gh.fake.issued_secrets()
    assert len(secrets) >= 5 and files  # tokens, the device code, the codeload token and address
    text = json.dumps(replies, ensure_ascii=False)
    out, err = capfd.readouterr()
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for secret in secrets:
        assert secret not in text and secret not in out and secret not in err and secret not in logs
        assert not any(secret.encode() in data for data in files)


def test_a_crash_in_a_daemon_thread_logs_only_its_type(gh, monkeypatch, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    leak = "https://codeload.example/x.tar.gz?token=NOT-FOR-LOGS"

    def broken(*args, **kwargs):
        raise RuntimeError(leak)

    monkeypatch.setattr(gh.svc.snapshots, "sweep", broken)
    monkeypatch.setattr(service, "extract_stream", broken)
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    _, block = gh.svc.open("octocat", "Hello-World", "")
    assert worker(gh, block["job"]).exited.wait(10) and gh.svc.ready.wait(10)
    done = gh.svc.job(block["job"])
    assert (done["state"], done["code"], done["message"]) == ("failed", "disk", M.DISK)
    assert partial_empty(gh)
    out, err = capfd.readouterr()
    assert leak not in out + err + caplog.text and "Traceback" not in err
    assert "RuntimeError" in caplog.text and "download_crash" in caplog.text


def test_jobs_are_found_by_their_id(gh):
    for bad in ("", "g" * 65, None, 7):
        with pytest.raises(GitHubError) as caught:
            gh.svc.job(bad)
        assert caught.value.reply() == {"error": M.BAD_REQUEST}
    for call in (gh.svc.job, gh.svc.cancel_job):
        with pytest.raises(GitHubError) as caught:
            call("g0123456789ab")
        assert (caught.value.code, caught.value.status, caught.value.message) == \
            ("job_not_found", 404, M.JOB_NOT_FOUND)


def test_the_ten_newest_finished_jobs_are_kept(gh):
    ids = []
    for i in range(11):
        gh.fake.add_repo("octocat", f"demo-{i:02d}", {"README.md": f"# {i}\n"})
        status, block = gh.svc.open("octocat", f"demo-{i:02d}", "")
        assert status == 202
        ids.append(finish(gh, block)["job"])
    with pytest.raises(GitHubError) as caught:
        gh.svc.job(ids[0])
    assert caught.value.code == "job_not_found"
    assert [gh.svc.job(i)["state"] for i in ids[1:]] == ["done"] * 10


def test_snapshot_lookup_is_offline_and_opened_is_recorded(gh):
    gh.fake.add_repo("octocat", "Hello-World", HELLO)
    _, done = opened(gh, "octocat", "Hello-World")
    asked = len(gh.fake.calls())
    snap = gh.svc.snapshot("OCTOCAT", "hello-world", done["sha"])
    assert (snap.owner, snap.repo, snap.sha, snap.private, snap.display, snap.notes) == \
        ("octocat", "Hello-World", done["sha"], False, "Hello-World", [])
    for args in (("../x", "r", done["sha"]), ("octocat", "Hello-World", "nope"), ("octocat", "Hello-World", "b" * 40)):
        assert gh.svc.snapshot(*args) is None
    meta = snap.folder / "snapshot.json"
    before = json.loads(meta.read_text(encoding="utf-8"))["opened"]
    time.sleep(0.05)
    gh.svc.opened(snap)
    assert json.loads(meta.read_text(encoding="utf-8"))["opened"] > before
    assert len(gh.fake.calls()) == asked
