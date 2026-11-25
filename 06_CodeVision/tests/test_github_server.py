"""Server tests for GitHub (/api/github/* and /api/analyze?github=), against tests/fake_github.py.

The real analyzer runs on the snapshots (tiny repos), the login lives in conftest's in-memory token store, and the
AI tests use tests/fake_ollama.py. Nothing here reaches GitHub, the internet or the Credential Manager.

- the page's contract: every JSON field app.js reads from status, login, repos, branches, resolve, open, job, logout,
  analyze and recent, and the error codes it meets with their extra fields (install_url, retry_after);
- every reply carries the security headers (the Client checks each one); bodies must be JSON objects;
- /api/analyze?github=: the exact source block, notes first in the warnings, the repo's display name, no data-folder
  path, a recent item of kind github, offline; the private-snapshot rule (only the login that downloaded it);
- AI explanations on a snapshot through its 'github:' project id; the AI side lets go of private snapshots on logout;
- logout and an account switch: private snapshots, private recent items and the stored login go, public ones stay;
- no token, device code or download address in replies, logs, stderr, thread hooks, files or AI prompts.
"""
import http.client
import importlib
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from codevision import server as cv_server
from codevision.explain import messages as AM
from codevision.github import auth, messages as GM, snapshot
from codevision.github.base import project_id
from codevision.github.extract import extract_stream
from fake_github import LFS_POINTER, Member, submodule_layout

UID = 583231  # the fake's octocat
HELLO = {"README.md": "# Hello-World\n",
         "app.py": "def greet(name):\n    return 'hi ' + name\n\n\nprint(greet('world'))\n",
         "lib/greet.js": "export function greet(name) {\n  return `hi ${name}`;\n}\n"}
DIARY = {"diary.py": "def write(text):\n    return text\n", "notes.py": "def note():\n    return 1\n"}
STATUS_KEYS = ["logged_in", "login", "store", "note", "fake", "install_url", "settings_url", "flow", "job"]
FLOW_KEYS = {"state", "user_code", "verification_uri", "expires_in", "message", "code"}
JOB_KEYS = ["job", "state", "phase", "owner", "repo", "branch", "sha", "private", "read", "total", "files",
            "message", "code", "note"]
DONE_KEYS = ["job", "state", "owner", "repo", "branch", "sha", "private", "cached", "offline", "message", "note"]
REPO_KEYS = ["owner", "repo", "full_name", "private", "default_branch", "pushed_at", "archived", "size_kb", "too_big"]
RESOLVE_KEYS = ["owner", "repo", "private", "default_branch", "branch", "note", "branches", "truncated", "size_kb",
                "too_big"]
MISSING = {"error": GM.SNAPSHOT_MISSING, "code": "snapshot_missing"}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": cv_server.CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class Client:
    """One running CodeVision server. Every reply is kept (raw) for the leak checks, and every reply is checked
    for JSON and the security headers."""

    def __init__(self, httpd, thread, fake, home):
        self.httpd, self.thread, self.fake, self.home = httpd, thread, fake, home
        self.replies: list[bytes] = []
        self.closed = False

    @property
    def svc(self):
        return self.httpd.github

    @property
    def ai(self):
        return self.httpd.ai

    def call(self, method, path, data=None, *, token=True, timeout=30):
        headers = {"X-CV-Token": self.httpd.token} if token else {}
        body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        self.replies.append(raw)
        assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
        for name, value in SECURITY_HEADERS.items():
            assert resp.headers[name] == value, (path, name)
        assert "Access-Control-Allow-Origin" not in resp.headers
        return resp.status, json.loads(raw.decode("utf-8"))

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.call("POST", path, {} if data is None else data, **kw)

    def close(self):
        if not self.closed:
            self.closed = True
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(5)


@pytest.fixture
def servers(fake_github, tmp_path, monkeypatch):
    """Start CodeVision servers on one scratch data folder, pointed at the fake GitHub (closed after the test)."""
    monkeypatch.setattr(auth, "RECHECK_WAIT", 0.0)
    home = tmp_path / "home"
    monkeypatch.setenv("CODEVISION_HOME", str(home))
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>{{CV_TOKEN}}", encoding="utf-8")
    made = []

    def start() -> Client:
        httpd = cv_server.create_server(port=0, web_dir=web)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        client = Client(httpd, thread, fake_github, home)
        made.append(client)
        return client

    yield start
    fake_github.release()
    for client in made:
        client.close()


@pytest.fixture
def cv(servers):
    return servers()


def wait(check, timeout=15.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


def status(cv) -> dict:
    code, st = cv.get("/api/github/status")
    assert code == 200
    return st


def sign_in(cv, login="octocat") -> int:
    """Logged in as if the device flow had just finished (the flow itself is tested through the routes)."""
    user = cv.fake.add_user(login)
    cv.svc.tokens.accept(cv.fake.issue(login))
    cv.svc.tokens.set_user(login, user["id"])
    return user["id"]


def log_in_through_the_page(cv, login="octocat") -> dict:
    code, st = cv.post("/api/github/login", {})
    assert code == 200 and st["flow"]["state"] == "waiting"
    cv.fake.approve(login)
    return wait(lambda: (lambda s: s if s["logged_in"] and (s["flow"] or {}).get("state") == "done" else None)(
        status(cv)))


def open_repo(cv, owner, repo, branch=""):
    """POST /api/github/open, then the job until it ends: (status of the open, the last block)."""
    code, block = cv.post("/api/github/open", {"owner": owner, "repo": repo, "branch": branch})
    if code in (200, 202) and block.get("state") == "running":
        job_id = block["job"]
        block = wait(lambda: (lambda b: b if b["state"] != "running" else None)(
            cv.get(f"/api/github/job?id={job_id}")[1]))
    return code, block


def analyze_url(owner, repo, branch, sha) -> str:
    """The address the page builds (app.js analyzeUrl)."""
    return (f"/api/analyze?github={quote(owner + '/' + repo, safe='')}&branch={quote(branch, safe='')}"
            f"&sha={quote(sha, safe='')}")


def downloads(fake) -> list:
    return fake.calls("/codeload/")


def plant_private(cv, owner, repo, user_id):
    """A private snapshot that `user_id` downloaded earlier (another account, another run)."""
    cv.fake.add_repo(owner, repo, {"notes.py": "x = 1\n"}, private=True)
    sha = cv.fake.head(owner, repo)
    st = cv.svc.snapshots.staging(owner, repo, sha)
    result = extract_stream(io.BytesIO(cv.fake.tarball(owner, repo, sha)), st.dest, final_root=st.final_root,
                            expect_commit=sha)
    return cv.svc.snapshots.publish(st, result, private=True, user_id=user_id, branch="main")


def reading(cv, job_id):
    """Wait until the download worker is blocked reading the held archive."""
    job = cv.svc._jobs[job_id]
    wait(lambda: job.download is not None and downloads(cv.fake))
    time.sleep(0.05)


def recent(cv) -> list[dict]:
    code, data = cv.get("/api/recent")
    assert code == 200
    return data["items"]


# --- status and login ---------------------------------------------------------------------------------------------

def test_the_status_block_is_the_pages_contract(cv):
    st = status(cv)
    assert list(st) == STATUS_KEYS
    assert st == {"logged_in": False, "login": "", "store": "memory", "note": GM.FAKE_MODE, "fake": True,
                  "install_url": cv.fake.url + "/web/apps/codevision-project-jun/installations/new",
                  "settings_url": cv.fake.url + "/web/settings/apps/authorizations", "flow": None, "job": None}
    assert cv.svc.ready.wait(10)  # the start-up cleanup ran when the server started
    assert cv.fake.calls() == []  # the status never asks GitHub


def test_logging_in_with_the_device_flow_through_the_routes(cv, servers):
    code, st = cv.post("/api/github/login", {})
    flow = st["flow"]
    assert code == 200 and list(st) == STATUS_KEYS and set(flow) == FLOW_KEYS
    assert (st["logged_in"], flow["state"], flow["message"], flow["code"]) == (False, "waiting", GM.WAITING, "")
    assert len(flow["user_code"]) == 9 and flow["user_code"][4] == "-"  # split on '-' for the screen reader
    assert flow["verification_uri"] == cv.fake.url + "/web/login/device"
    assert isinstance(flow["expires_in"], int) and 0 < flow["expires_in"] <= 900
    assert not any(s in json.dumps(st) for s in cv.fake.issued_secrets())  # the device code stays on the server
    code, again = cv.post("/api/github/login", {})  # a reloaded page re-attaches to the same code
    assert code == 200 and again["flow"]["user_code"] == flow["user_code"]
    assert len(cv.fake.calls("/web/login/device/code")) == 1
    cv.fake.approve()
    st = wait(lambda: (lambda s: s if (s["flow"] or {}).get("state") == "done" else None)(status(cv)))
    assert (st["logged_in"], st["login"], st["flow"]["message"], st["flow"]["code"]) == \
        (True, "octocat", GM.DONE, "")
    assert "user_code" not in st["flow"] and "expires_in" not in st["flow"]
    assert cv.post("/api/github/login", {}) == (409, {"error": GM.ALREADY_LOGGED_IN, "code": "already_logged_in"})
    later = servers()  # the next run of CodeVision on this PC: logged in once is enough
    assert (status(later)["logged_in"], status(later)["login"]) == (True, "octocat")


def test_cancelling_a_login(cv):
    assert cv.post("/api/github/login", {})[0] == 200
    code, st = cv.post("/api/github/login/cancel", {})
    assert code == 200 and st["logged_in"] is False
    assert st["flow"] == {"state": "cancelled", "message": GM.CANCELLED, "code": ""}
    assert status(cv)["flow"]["state"] == "cancelled"


@pytest.mark.parametrize("path", ["/api/github/login", "/api/github/login/cancel", "/api/github/logout",
                                  "/api/github/resolve", "/api/github/open", "/api/github/job/cancel"])
def test_github_posts_need_a_json_object(cv, path):
    for body in (b"[]", b'"x"', b"nope", b"\xff\xfe", b"[" * 5000):
        assert cv.call("POST", path, body) == (400, {"error": GM.BAD_REQUEST}), body[:10]
    assert cv.fake.calls() == [] and status(cv)["flow"] is None


# --- repos, branches, resolve ----------------------------------------------------------------------------------

def test_the_repo_list_needs_a_login_and_is_kept_for_a_minute(cv):
    assert cv.get("/api/github/repos") == (401, {"error": GM.LOGIN_NEEDED, "code": "login_needed"})
    cv.fake.add_repo("octocat", "Hello-World", HELLO, pushed_at="2026-11-20T00:00:00Z")
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True, pushed_at="2026-11-24T00:00:00Z")
    sign_in(cv)
    code, out = cv.get("/api/github/repos")
    assert code == 200 and list(out) == ["installed", "login", "repos", "truncated", "note"]
    assert (out["installed"], out["login"], out["truncated"], out["note"]) == (True, "octocat", False, "")
    assert [r["full_name"] for r in out["repos"]] == ["octocat/secret-diary", "octocat/Hello-World"]  # newest push
    assert all(list(r) == REPO_KEYS for r in out["repos"])
    assert (out["repos"][0]["private"], out["repos"][1]["default_branch"], out["repos"][1]["too_big"]) == \
        (True, "main", False)
    asked = len(cv.fake.calls("/api"))
    assert cv.get("/api/github/repos") == (200, out) and len(cv.fake.calls("/api")) == asked
    assert cv.get("/api/github/repos?refresh=1")[0] == 200 and len(cv.fake.calls("/api")) > asked


def test_no_installation_lists_nothing_with_the_org_hint(cv):
    cv.fake.set_installed(False)
    sign_in(cv)
    code, out = cv.get("/api/github/repos")
    assert code == 200 and (out["installed"], out["repos"], out["note"]) == (False, [], GM.ORG_HINT)


def test_branches_and_their_errors(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO, branches={"main": None, "feature/login": None, "docs": None})
    cv.post("/api/github/resolve", {"text": "octocat/Hello-World"})  # the repo info: the default branch is known
    code, out = cv.get("/api/github/branches?owner=octocat&repo=Hello-World")
    assert code == 200 and list(out) == ["owner", "repo", "default_branch", "branches", "truncated"]
    assert (out["owner"], out["repo"], out["default_branch"], out["truncated"]) == \
        ("octocat", "Hello-World", "main", False)
    assert [b["name"] for b in out["branches"]] == ["main", "docs", "feature/login"]
    assert all(set(b) == {"name", "sha"} and len(b["sha"]) == 40 for b in out["branches"])
    for query in ("owner=..&repo=x", "owner=octocat", "repo=Hello-World", "owner=octocat&repo=..", ""):
        assert cv.get("/api/github/branches?" + query) == (400, {"error": GM.BAD_REQUEST}), query
    assert cv.get("/api/github/branches?owner=octocat&repo=nothing-here") == \
        (404, {"error": GM.NOT_FOUND_ANON, "code": "not_found"})


@pytest.mark.parametrize("text, branch, note", [
    ("octocat/hello-world", "main", ""),
    ("https://github.com/OCTOCAT/Hello-World/tree/feature/login/src/app.py", "feature/login", ""),
    ("https://github.com/octocat/Hello-World/blob/main/README.md?plain=1#L1", "main", ""),
    ("git@github.com:octocat/Hello-World.git", "main", ""),
    ("https://github.com/octocat/Hello-World/tree/no-such-branch", "main", GM.branch_default("main")),
])
def test_resolve_reads_what_people_paste(cv, text, branch, note):
    cv.fake.add_repo("octocat", "Hello-World", HELLO, branches={"main": None, "feature/login": None})
    code, out = cv.post("/api/github/resolve", {"text": text})
    assert code == 200 and list(out) == RESOLVE_KEYS
    assert (out["owner"], out["repo"], out["private"], out["default_branch"], out["branch"], out["note"]) == \
        ("octocat", "Hello-World", False, "main", branch, note)  # GitHub's own spelling
    assert ([b["name"] for b in out["branches"]], out["truncated"], out["too_big"]) == \
        (["main", "feature/login"], False, False)


@pytest.mark.parametrize("body, reply", [
    ({"text": ""}, {"error": GM.EMPTY_INPUT, "code": "bad_input"}),
    ({"text": "not a repo"}, {"error": GM.BAD_INPUT, "code": "bad_input"}),
    ({"text": 5}, {"error": GM.BAD_INPUT, "code": "bad_input"}),
    ({}, {"error": GM.BAD_INPUT, "code": "bad_input"}),
    ({"text": "https://gitlab.com/octocat/Hello-World"}, {"error": GM.NOT_GITHUB, "code": "not_github"}),
    ({"text": "https://user:pass@github.com/octocat/Hello-World"}, {"error": GM.BAD_INPUT, "code": "bad_input"}),
])
def test_resolve_refuses_what_is_not_a_github_repo(cv, body, reply):
    assert cv.post("/api/github/resolve", body) == (400, reply)
    assert cv.fake.calls() == []


# --- open, job, analyze ------------------------------------------------------------------------------------------

def test_open_download_and_analyze_end_to_end(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    sha = cv.fake.head("octocat", "Hello-World")
    code, block = cv.post("/api/github/open", {"owner": "OCTOCAT", "repo": "hello-world", "branch": ""})
    assert code == 202 and list(block) == JOB_KEYS
    assert re.fullmatch(r"g[0-9a-f]{12}", block["job"]) and block["state"] == "running"
    assert (block["owner"], block["repo"], block["branch"], block["sha"], block["private"]) == \
        ("octocat", "Hello-World", "main", sha, False)  # GitHub's spelling and the default branch
    done = wait(lambda: (lambda b: b if b["state"] != "running" else None)(
        cv.get(f"/api/github/job?id={block['job']}")[1]))
    assert list(done) == JOB_KEYS
    assert (done["state"], done["phase"], done["message"], done["code"], done["note"]) == \
        ("done", "finish", GM.JOB_DONE, "", "")
    assert done["files"] == 3 and done["read"] == done["total"] > 0
    asked = len(cv.fake.calls())

    code, graph = cv.get(analyze_url("octocat", "Hello-World", "main", sha))
    assert code == 200
    assert graph["source"] == {"kind": "github", "path": "", "owner": "octocat", "repo": "Hello-World",
                               "branch": "main", "sha": sha, "private": False,
                               "url": cv.fake.url + "/web/octocat/Hello-World",
                               "project": f"github:octocat/hello-world@{sha}"}
    assert graph["name"] == "Hello-World" and graph["nodes"]["."]["n"] == "Hello-World"
    assert {"README.md", "app.py", "lib", "lib/greet.js"} <= set(graph["nodes"])
    assert isinstance(graph["warnings"], list) and graph["ai"]["enabled"] is False
    assert len(cv.fake.calls()) == asked  # analyze works offline: GitHub is never asked
    raw = cv.replies[-1].decode("utf-8")
    snap = cv.svc.snapshot("octocat", "Hello-World", sha)
    for folder in (cv.home, snap.root):  # no machine path reaches the page
        assert str(folder) not in raw and json.dumps(str(folder))[1:-1] not in raw
    items = recent(cv)
    assert len(items) == 1 and isinstance(items[0].pop("at"), float)
    assert items == [{"kind": "github", "owner": "octocat", "repo": "Hello-World", "branch": "main",
                      "private": False, "name": "Hello-World", "path": ""}]
    on_disk = (cv.home / "recent.json").read_text(encoding="utf-8")
    assert str(cv.home) not in on_disk and json.dumps(str(cv.home))[1:-1] not in on_disk
    code, again = cv.get(analyze_url("OCTOCAT", "HELLO-WORLD", "main", sha))  # names in another case
    assert code == 200 and (again["source"]["owner"], again["source"]["repo"]) == ("octocat", "Hello-World")
    assert len(recent(cv)) == 1


def test_reopening_the_same_commit_downloads_nothing(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    _, done = open_repo(cv, "octocat", "Hello-World")
    code, again = cv.post("/api/github/open", {"owner": "octocat", "repo": "Hello-World", "branch": "main"})
    assert code == 200 and list(again) == DONE_KEYS
    assert again == {"job": "", "state": "done", "owner": "octocat", "repo": "Hello-World", "branch": "main",
                     "sha": done["sha"], "private": False, "cached": True, "offline": False,
                     "message": GM.REUSED, "note": ""}
    assert len(downloads(cv.fake)) == 1


def test_a_new_commit_downloads_again_and_old_snapshots_go_unless_the_ai_side_uses_them(cv, monkeypatch):
    monkeypatch.setattr(snapshot, "RECENT_SECONDS", 0.0)  # snapshots opened in the last 10 minutes always stay
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    _, first = open_repo(cv, "octocat", "Hello-World")
    new = cv.fake.push("octocat", "Hello-World", "main", {"README.md": "# v2\n", "app.py": "x = 2\n"})
    code, second = open_repo(cv, "octocat", "Hello-World", "main")
    assert code == 202 and (second["state"], second["sha"]) == ("done", new) and len(downloads(cv.fake)) == 2
    assert cv.svc.snapshot("octocat", "Hello-World", first["sha"]) is None  # pruned: no branch points at it
    assert cv.get(analyze_url("octocat", "Hello-World", "main", new))[0] == 200  # the AI side now holds it
    cv.fake.push("octocat", "Hello-World", "main", {"README.md": "# v3\n"})
    _, third = open_repo(cv, "octocat", "Hello-World", "main")
    assert third["state"] == "done"
    assert cv.svc.snapshot("octocat", "Hello-World", new) is not None  # in use (AIService.roots_in_use): kept


@pytest.mark.parametrize("query", [
    "github=octocat%2FHello-World&branch=main",                      # no sha
    "github=octocat%2FHello-World&branch=main&sha=ABC",              # not 40 hex
    "github=octocat&branch=main&sha=" + "a" * 40,                    # no owner/repo
    "github=..%2Fx&branch=main&sha=" + "a" * 40,
    "github=octocat%2F..&branch=main&sha=" + "a" * 40,
    "github=octocat%2FHello-World&branch=bad..branch&sha=" + "a" * 40,
    "github=&branch=main&sha=" + "a" * 40,                           # blank: still a GitHub request
])
def test_analyze_refuses_bad_github_parameters(cv, query):
    assert cv.get("/api/analyze?" + query) == (400, {"error": GM.BAD_PARAMS})


def test_a_gone_snapshot_is_snapshot_missing(cv):
    assert cv.get(analyze_url("octocat", "Hello-World", "main", "b" * 40)) == (404, MISSING)
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    _, done = open_repo(cv, "octocat", "Hello-World")
    snap = cv.svc.snapshot("octocat", "Hello-World", done["sha"])
    assert cv.svc.snapshots.delete(snap)
    assert cv.get(analyze_url("octocat", "Hello-World", "main", done["sha"])) == (404, MISSING)
    assert recent(cv) == []


def test_snapshot_notes_come_first_in_the_warnings(cv):
    files, extra = submodule_layout()
    files = dict(files, **{"assets/design.psd": LFS_POINTER, "src/main.py": "print('hi')\n",
                           "src/broken.py": "def oops(:\n    pass\n"})  # the analyzer warns about this one
    extra = extra + (Member("CON", data=b"device"), Member("q?.txt", data=b"question"))
    cv.fake.add_repo("octocat", "noted", files, extra=extra)
    _, done = open_repo(cv, "octocat", "noted")
    snap = cv.svc.snapshot("octocat", "noted", done["sha"])
    assert GM.LFS(1) in snap.notes and GM.SUBMODULES(2) in snap.notes
    assert any(n.startswith("Windows에서 쓸 수 없는 이름의 파일 2개") for n in snap.notes)
    code, graph = cv.get(analyze_url("octocat", "noted", "main", done["sha"]))
    analyzer = importlib.import_module("codevision.graph").build(str(snap.root))["warnings"]
    assert analyzer, "the test needs a warning of the analyzer's own to tell the order"
    assert code == 200 and graph["warnings"] == snap.notes + analyzer  # the page puts open's message before them


def test_a_repo_named_like_a_windows_device_keeps_its_name(cv):
    cv.fake.add_repo("octocat", "CON", {"main.py": "print('con')\n"})
    _, done = open_repo(cv, "octocat", "CON")
    snap = cv.svc.snapshot("octocat", "CON", done["sha"])
    assert snap.root.name == "repo" and snap.display == "CON"  # Windows cannot have a folder named CON
    code, graph = cv.get(analyze_url("octocat", "CON", "main", done["sha"]))
    assert code == 200 and graph["name"] == "CON" and graph["source"]["repo"] == "CON"
    assert (graph["nodes"]["."]["n"], graph["nodes"]["."]["p"]) == ("CON", "CON/")
    key = graph["source"]["project"]
    cv.ai._projects.clear()  # gone from the AI side's memory: it analyzes the snapshot again the same way
    project = cv.ai.project(key, snap.root)
    assert (project.graph["name"], project.graph["nodes"]["."]["n"], project.graph["source"]["kind"]) == \
        ("CON", "CON", "github")


def test_a_snapshot_analysis_crash_is_a_korean_500(cv, monkeypatch, caplog):
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    sign_in(cv)
    _, done = open_repo(cv, "octocat", "secret-diary")
    graph_module = importlib.import_module("codevision.graph")

    def boom(root):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(graph_module, "build", boom)
    assert cv.get(analyze_url("octocat", "secret-diary", "main", done["sha"])) == \
        (500, {"error": cv_server.GH_ANALYSIS_FAILED})
    assert "저장소를 분석하다가" in cv_server.GH_ANALYSIS_FAILED
    failed = [r for r in caplog.records if r.name == "codevision.server" and r.levelno == logging.ERROR]
    assert failed and failed[0].exc_info[0] is RuntimeError  # the traceback goes to the log only
    assert "secret-diary" not in failed[0].getMessage()  # the line itself names no (private) repo
    assert recent(cv) == []  # a failed analysis is not remembered


def test_error_logs_never_name_a_private_repo(cv, monkeypatch, caplog):
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    sign_in(cv)
    _, done = open_repo(cv, "octocat", "secret-diary")
    from codevision.lang import python_lang

    def broken(rel, text):
        raise ValueError(f"cannot read {rel} of secret-diary")

    monkeypatch.setattr(python_lang, "analyze", broken)
    caplog.set_level(logging.DEBUG)
    assert cv.get(analyze_url("octocat", "secret-diary", "main", done["sha"]))[0] == 200  # the analyzer failed
    monkeypatch.setattr(cv.svc, "branches", lambda owner, repo: 1 / 0)  # a bug: an unexpected exception
    assert cv.get("/api/github/branches?owner=octocat&repo=secret-diary")[0] == 500
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("codevision")]
    assert [x for x in lines if "analyze failed" in x and "ValueError" in x]
    assert [x for x in lines if "request failed: GET /api/github/branches" in x]
    assert not [x for x in lines if "diary" in x or "notes.py" in x]


def test_github_out_of_reach_reopens_the_last_snapshot(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    _, done = open_repo(cv, "octocat", "Hello-World")
    cv.fake.set(fail="drop", fail_on="api")
    code, reply = cv.post("/api/github/open", {"owner": "octocat", "repo": "Hello-World", "branch": "main"})
    assert code == 200 and list(reply) == DONE_KEYS
    assert (reply["state"], reply["sha"], reply["cached"], reply["offline"], reply["message"]) == \
        ("done", done["sha"], True, True, GM.OFFLINE(done["sha"]))
    assert cv.get(analyze_url("octocat", "Hello-World", "main", done["sha"]))[0] == 200


# --- AI explanations on a snapshot ---------------------------------------------------------------------------------

def test_ai_explanations_on_a_snapshot(cv, fake_ollama):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    _, done = open_repo(cv, "octocat", "Hello-World")
    assert cv.post("/api/ai/settings", {"enabled": True})[0] == 200
    url = analyze_url("octocat", "Hello-World", "main", done["sha"])
    code, graph = cv.get(url)
    assert code == 200 and graph["ai"]["available"] is True
    project = graph["source"]["project"]
    code, reply = cv.post("/api/ai/explain", {"project": project, "node": "app.py"})
    assert code == 200 and (reply["node"], reply["cached"], set(reply["x"])) == ("app.py", False, {"s", "d", "f"})
    code, job = cv.post("/api/ai/explain-all", {"project": project})
    assert code == 202
    block = wait(lambda: (lambda b: b if b["state"] != "running" else None)(cv.get(f"/api/ai/job?id={job['job']}")[1]))
    assert block["state"] == "done" and block["errors"] == 0
    code, again = cv.get(url)  # what the page's refreshX asks: the cached explanations come with the graph
    assert code == 200 and again["nodes"]["app.py"]["x"] == reply["x"]
    cv.ai._projects.clear()  # gone from the AI side's memory: rebuilt from the snapshot, same cache keys
    assert cv.post("/api/ai/explain", {"project": project, "node": "app.py"}) == \
        (200, {"node": "app.py", "x": reply["x"], "cached": True})
    assert cv.ai._projects[project].graph["source"]["kind"] == "github"
    home = json.dumps(str(cv.home))[1:-1]
    assert fake_ollama.prompts() and not any(home in p or str(cv.home) in p for p in fake_ollama.prompts())


def test_ai_project_ids_for_github_are_checked(cv):
    gone = "github:octocat/hello-world@" + "b" * 40
    assert cv.post("/api/ai/explain", {"project": gone, "node": "app.py"}) == \
        (404, {"error": GM.SNAPSHOT_MISSING, "code": "not_found"})
    assert cv.post("/api/ai/explain-all", {"project": gone}) == \
        (404, {"error": GM.SNAPSHOT_MISSING, "code": "not_found"})
    for bad in ("github:../x@123", "github:octocat/Hello-World", "github:a/b@" + "A" * 40):
        assert cv.post("/api/ai/explain", {"project": bad, "node": "app.py"}) == (400, {"error": GM.BAD_PARAMS}), bad
    for relative in ("relative/folder", "octocat/Hello-World"):  # still folder paths, and not full ones
        code, data = cv.post("/api/ai/explain", {"project": relative, "node": "app.py"})
        assert code == 400 and "전체 경로" in data["error"], relative


# --- private snapshots, logout, account switch -----------------------------------------------------------------------

def test_a_private_snapshot_opens_only_for_its_login(cv):
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    sign_in(cv)
    _, done = open_repo(cv, "octocat", "secret-diary")
    assert (done["state"], done["private"]) == ("done", True)
    url = analyze_url("octocat", "secret-diary", "main", done["sha"])
    code, graph = cv.get(url)
    assert code == 200 and graph["source"]["private"] is True
    project = graph["source"]["project"]
    assert [(i["repo"], i["private"]) for i in recent(cv)] == [("secret-diary", True)]
    cv.svc.tokens.forget()  # the login ended without a logout (GitHub refused a refresh, say)
    assert cv.get(url) == (404, MISSING)
    assert cv.post("/api/ai/explain", {"project": project, "node": "diary.py"}) == \
        (404, {"error": GM.SNAPSHOT_MISSING, "code": "not_found"})
    assert [(i["repo"], i["private"]) for i in recent(cv)] == [("secret-diary", True)]  # kept, with a lock
    assert cv.svc.snapshots.find("octocat", "secret-diary", done["sha"]) is not None  # nothing was deleted
    sign_in(cv)
    other = plant_private(cv, "octocat", "their-notes", 4242)  # another account's download
    assert cv.get(analyze_url("octocat", "their-notes", "main", other.sha)) == (404, MISSING)
    assert cv.get(url)[0] == 200  # the right login again


def test_logout_deletes_private_data_and_keeps_public(cv, memory_keyring, tmp_path):
    folder = tmp_path / "my-app"
    folder.mkdir()
    (folder / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert cv.get("/api/analyze?path=" + quote(str(folder), safe=""))[0] == 200
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    sign_in(cv)
    keys = {}
    for repo in ("secret-diary", "Hello-World"):
        _, done = open_repo(cv, "octocat", repo)
        code, graph = cv.get(analyze_url("octocat", repo, "main", done["sha"]))
        assert code == 200
        keys[repo] = (graph["source"]["project"], done["sha"])
    private = cv.svc.snapshot("octocat", "secret-diary", keys["secret-diary"][1])
    assert memory_keyring.entries() and keys["secret-diary"][0] in cv.ai._projects
    code, out = cv.post("/api/github/logout", {"delete_private": True})
    assert code == 200 and list(out) == STATUS_KEYS + ["removed", "message"]
    assert (out["logged_in"], out["login"], out["message"]) == (False, "", GM.LOGOUT_DONE)
    assert out["removed"] == {"snapshots": 1, "recent": 1, "failed": 0}
    assert memory_keyring.entries() == {}
    assert [(i.get("kind"), i.get("repo") or i["path"]) for i in recent(cv)] == \
        [("github", "Hello-World"), (None, str(folder))]
    assert not private.folder.exists()
    assert keys["secret-diary"][0] not in cv.ai._projects and keys["Hello-World"][0] in cv.ai._projects
    assert cv.get(analyze_url("octocat", "secret-diary", "main", keys["secret-diary"][1])) == (404, MISSING)
    assert cv.get(analyze_url("octocat", "Hello-World", "main", keys["Hello-World"][1]))[0] == 200


def test_logout_stops_ai_work_on_private_snapshots(cv, fake_ollama):
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    sign_in(cv)
    _, done = open_repo(cv, "octocat", "secret-diary")
    assert cv.post("/api/ai/settings", {"enabled": True})[0] == 200
    project = cv.get(analyze_url("octocat", "secret-diary", "main", done["sha"]))[1]["source"]["project"]
    fake_ollama.hold()
    code, job = cv.post("/api/ai/explain-all", {"project": project})
    assert code == 202 and fake_ollama.wait_for_chats(1)  # the worker is in the middle of the first file
    waiting = {}
    asker = threading.Thread(target=lambda: waiting.setdefault(
        "reply", cv.post("/api/ai/explain", {"project": project, "node": "notes.py"})), daemon=True)
    asker.start()
    wait(lambda: cv.ai.pending() == 1)  # an on-demand request waits behind it
    assert cv.post("/api/github/logout", {"delete_private": True})[1]["removed"]["snapshots"] == 1
    asker.join(10)
    assert waiting["reply"] == (404, {"error": AM.PROJECT_CLOSED, "code": "not_found"})
    code, block = cv.get(f"/api/ai/job?id={job['job']}")
    assert (block["state"], block["message"]) == ("cancelled", AM.JOB_PROJECT_CLOSED.format(n=0))
    assert project not in cv.ai._projects
    fake_ollama.release()


def test_a_logout_during_an_analysis_last_step_leaves_no_private_trace(cv, monkeypatch):
    # The analysis had checked the snapshot again; a logout then used to finish before it put the private repo
    # on the recent list (and in the AI side's memory), so both stayed. Now the logout waits for that step.
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    sign_in(cv)
    _, done = open_repo(cv, "octocat", "secret-diary")
    real, logout = cv_server.add_recent_github, {}

    def late(*args, **kwargs):
        thread = threading.Thread(target=lambda: logout.setdefault("reply", cv.post("/api/github/logout", {})))
        thread.start()
        thread.join(0.5)  # long enough for a logout that does not wait
        logout["thread"] = thread
        return real(*args, **kwargs)

    monkeypatch.setattr(cv_server, "add_recent_github", late)
    code, graph = cv.get(analyze_url("octocat", "secret-diary", "main", done["sha"]))
    logout["thread"].join(15)
    assert code == 200 and logout["reply"][0] == 200 and logout["reply"][1]["removed"]["recent"] == 1
    assert recent(cv) == [] and graph["source"]["project"] not in cv.ai._projects


def test_logout_can_keep_private_files_but_never_shows_them(cv):
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    sign_in(cv)
    _, done = open_repo(cv, "octocat", "secret-diary")
    url = analyze_url("octocat", "secret-diary", "main", done["sha"])
    assert cv.get(url)[0] == 200
    assert cv.post("/api/github/logout", {"delete_private": "yes"}) == (400, {"error": GM.BAD_REQUEST})
    code, out = cv.post("/api/github/logout", {"delete_private": False})
    assert code == 200 and out["removed"] == {"snapshots": 0, "recent": 0, "failed": 0}
    assert cv.svc.snapshots.find("octocat", "secret-diary", done["sha"]) is not None
    assert cv.get(url) == (404, MISSING)
    assert [i["repo"] for i in recent(cv)] == ["secret-diary"]


def test_logout_when_nobody_is_logged_in(cv):
    code, out = cv.post("/api/github/logout", {})  # delete_private defaults to true
    assert code == 200 and out["logged_in"] is False and out["message"] == GM.LOGOUT_DONE
    assert out["removed"] == {"snapshots": 0, "recent": 0, "failed": 0}


def test_an_account_switch_takes_the_other_accounts_private_repos_away(cv):
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    sign_in(cv)
    keys = {}
    for repo in ("secret-diary", "Hello-World"):
        _, done = open_repo(cv, "octocat", repo)
        keys[repo] = cv.get(analyze_url("octocat", repo, "main", done["sha"]))[1]["source"]["project"]
    private = cv.svc.snapshots.snapshots()
    cv.svc.tokens.forget()  # octocat's login ended without a logout
    cv.fake.add_user("hubot")
    st = log_in_through_the_page(cv, "hubot")
    assert st["login"] == "hubot"
    assert [i["repo"] for i in recent(cv)] == ["Hello-World"]  # gone before the login said done
    assert [s.repo for s in cv.svc.snapshots.snapshots()] == ["Hello-World"]
    assert not [s for s in private if s.private and s.folder.exists()]
    assert keys["secret-diary"] not in cv.ai._projects and keys["Hello-World"] in cv.ai._projects


def test_the_github_side_keeps_to_the_servers_data_folder(cv, monkeypatch, tmp_path):
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    sign_in(cv)
    _, done = open_repo(cv, "octocat", "secret-diary")
    key = cv.get(analyze_url("octocat", "secret-diary", "main", done["sha"]))[1]["source"]["project"]
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("CODEVISION_HOME", str(elsewhere))  # changed after the server started
    cv.httpd._github_private_gone(frozenset({key}), "switch")  # what the login or start-up thread calls
    assert cv_server._read_recent(cv.home) == [] and key not in cv.ai._projects
    assert cv.svc.snapshots.repos_dir() == cv.home / "repos"
    assert not elsewhere.exists()


# --- jobs, busy, cancel, closing ------------------------------------------------------------------------------------

def test_one_download_at_a_time_cancel_and_the_status_job(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    cv.fake.add_repo("octocat", "other", {"a.py": "x = 1\n"})
    cv.fake.hold("codeload")
    code, first = cv.post("/api/github/open", {"owner": "octocat", "repo": "Hello-World", "branch": ""})
    assert code == 202
    st = status(cv)
    assert list(st["job"]) == JOB_KEYS and (st["job"]["job"], st["job"]["state"]) == (first["job"], "running")
    assert cv.post("/api/github/open", {"owner": "octocat", "repo": "other", "branch": ""}) == \
        (409, {"error": GM.BUSY, "code": "busy"})
    code, same = cv.post("/api/github/open", {"owner": "octocat", "repo": "Hello-World", "branch": "main"})
    assert code == 200 and (same["job"], same["state"]) == (first["job"], "running")  # the same commit re-attaches
    reading(cv, first["job"])
    code, out = cv.post("/api/github/job/cancel", {"job": first["job"]})
    assert code == 200 and list(out) == JOB_KEYS
    assert (out["state"], out["message"], out["code"]) == ("cancelled", GM.JOB_CANCELLED, "")
    assert cv.get(f"/api/github/job?id={first['job']}") == (200, out)
    assert cv.svc._jobs[first["job"]].exited.wait(10)
    assert status(cv)["job"] is None and cv.svc.snapshots.snapshots() == []
    cv.fake.release()


def test_job_routes_check_the_id(cv):
    for query in ("", "id=", "id=" + "g" * 65):
        assert cv.get("/api/github/job?" + query) == (400, {"error": GM.BAD_REQUEST}), query
    assert cv.get("/api/github/job?id=g0123456789ab") == (404, {"error": GM.JOB_NOT_FOUND, "code": "job_not_found"})
    for body in ({"job": 5}, {"job": ""}, {}):
        assert cv.post("/api/github/job/cancel", body) == (400, {"error": GM.BAD_REQUEST}), body
    assert cv.post("/api/github/job/cancel", {"job": "g0123456789ab"}) == \
        (404, {"error": GM.JOB_NOT_FOUND, "code": "job_not_found"})


def test_closing_the_server_never_waits_for_a_download(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    cv.fake.hold("codeload")
    _, block = cv.post("/api/github/open", {"owner": "octocat", "repo": "Hello-World", "branch": ""})
    reading(cv, block["job"])
    svc = cv.svc
    started = time.monotonic()
    cv.close()
    assert time.monotonic() - started < 3
    out = svc.job(block["job"])
    assert (out["state"], out["code"], out["message"]) == ("cancelled", "closing", GM.CLOSING)
    assert svc._jobs[block["job"]].exited.wait(10)


def test_server_close_closes_github_first(tmp_path, monkeypatch, fake_github):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    httpd = cv_server.create_server(port=0, web_dir=tmp_path)
    order = []
    gh_close, ai_close = httpd.github.close, httpd.ai.close
    monkeypatch.setattr(httpd.github, "close", lambda: (order.append("github"), gh_close()))
    monkeypatch.setattr(httpd.ai, "close", lambda: (order.append("ai"), ai_close()))
    httpd.server_close()
    assert order == ["github", "ai"]


# --- the recent list ---------------------------------------------------------------------------------------------

def test_old_recent_files_still_load(cv):
    good = {"kind": "github", "owner": "octocat", "repo": "Hello-World", "branch": "main", "private": False,
            "name": "Hello-World", "path": "", "at": 3.0}
    items = [
        {"path": "C:\\old\\app", "name": "app", "at": 1.0},                       # before GitHub: no kind
        {"kind": "local", "path": "D:\\work", "name": "work", "at": 2.0},
        good,
        {"kind": "github", "owner": "..", "repo": "x", "branch": "main"},           # broken items are left out
        {"kind": "github", "owner": "octocat", "repo": "r", "branch": "a..b"},
        {"kind": "github", "owner": "octocat", "repo": "r2", "private": "yes", "at": "soon", "name": "<b>x</b>"},
        {"kind": "gitlab", "path": "C:\\x"},                                       # a kind this version does not know
        "not an item", {"name": "no path"},
    ]
    cv.home.mkdir(parents=True, exist_ok=True)
    (cv.home / "recent.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    assert recent(cv) == [items[0], items[1], good,
                          {"kind": "github", "owner": "octocat", "repo": "r2", "branch": "", "private": False,
                           "name": "r2", "path": "", "at": 0.0}]
    (cv.home / "recent.json").write_text("[[[" * 10000, encoding="utf-8")  # broken: an empty list, not a crash
    assert recent(cv) == []


def test_recent_keeps_one_item_per_repo_and_shares_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEVISION_HOME", str(tmp_path))
    cv_server.add_recent_github("octocat", "Hello-World", "main", False)
    cv_server.add_recent(str(tmp_path / "a"))
    items = cv_server.add_recent_github("OCTOCAT", "hello-world", "docs", True)  # the same repo: newest wins
    assert [(i.get("kind"), i.get("branch")) for i in items] == [("github", "docs"), (None, None)]
    cv_server.add_recent_github("octocat", "public-one", "main", False)
    for i in range(12):
        cv_server.add_recent(str(tmp_path / f"p{i}"))
    items = cv_server.load_recent()
    assert len(items) == cv_server.MAX_RECENT and all(i.get("kind") is None for i in items)  # repos pushed out too
    cv_server.add_recent_github("octocat", "secret", "main", True)
    cv_server.add_recent_github("octocat", "open", "main", False)
    assert cv_server.remove_recent_github(private_only=True) == 1
    assert [i["repo"] for i in cv_server.load_recent() if i.get("kind") == "github"] == ["open"]
    assert cv_server.remove_recent_github(private_only=False) == 1
    assert not [i for i in cv_server.load_recent() if i.get("kind") == "github"]
    for bad in (("..", "x", "main"), ("octocat", "x", "a..b"), ("octocat", "x", None)):
        with pytest.raises(ValueError):
            cv_server.add_recent_github(*bad, False)
    assert not list(tmp_path.glob("recent.json.*"))  # no temporary file left behind


# --- errors the page meets ----------------------------------------------------------------------------------------

def _not_allowed(cv):
    cv.fake.add_repo("octocat", "locked", {"README.md": "# locked\n"}, private=True, not_allowed=True)
    sign_in(cv)
    return ("POST", "/api/github/resolve", {"text": "octocat/locked"}), 409, \
        {"error": GM.NOT_ALLOWED, "code": "not_allowed", "install_url": cv.svc.hosts.install_url}


def _sso(cv):
    cv.fake.add_org("acme-sso")
    cv.fake.add_installation("acme-sso", account_type="Organization", members=["octocat"])
    cv.fake.add_repo("acme-sso", "handbook", {"README.md": "# Handbook\n"}, private=True, sso=True,
                     collaborators=["octocat"])
    sign_in(cv)
    return ("POST", "/api/github/resolve", {"text": "acme-sso/handbook"}), 403, \
        {"error": GM.SSO_REQUIRED, "code": "sso_required"}


def _suspended(cv):
    cv.fake.add_org("paused-org")
    cv.fake.add_installation("paused-org", account_type="Organization", suspended=True, members=["octocat"])
    cv.fake.add_repo("paused-org", "app", {"main.py": "x = 1\n"}, private=True, collaborators=["octocat"])
    sign_in(cv)
    return ("POST", "/api/github/resolve", {"text": "paused-org/app"}), 403, \
        {"error": GM.SUSPENDED, "code": "suspended", "install_url": cv.svc.hosts.install_url}


def _other_owner(cv):
    cv.fake.add_org("other-org")
    cv.fake.add_repo("other-org", "private-tool", {"tool.py": "x = 1\n"}, private=True)
    sign_in(cv)
    return ("POST", "/api/github/open", {"owner": "other-org", "repo": "private-tool", "branch": "main"}), 404, \
        {"error": GM.other_owner("other-org"), "code": "other_owner", "install_url": cv.svc.hosts.install_url}


def _outdated(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    cv.fake.set(api_reply="410")
    return ("POST", "/api/github/resolve", {"text": "octocat/Hello-World"}), 502, \
        {"error": GM.OUTDATED, "code": "outdated"}


def _rate_limited(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    cv.fake.set(rate="primary")
    return ("POST", "/api/github/resolve", {"text": "octocat/Hello-World"}), 429, \
        {"error": GM.rate_limited(600, True), "code": "rate_limited"}


def _empty(cv):
    cv.fake.add_repo("octocat", "empty-repo", empty=True)
    return ("POST", "/api/github/open", {"owner": "octocat", "repo": "empty-repo", "branch": ""}), 422, \
        {"error": GM.EMPTY_REPO, "code": "empty_repo"}


def _too_big(cv):
    cv.fake.add_repo("octocat", "too-big", {"README.md": "# huge\n"}, size_kb=2_000_001)
    return ("POST", "/api/github/open", {"owner": "octocat", "repo": "too-big", "branch": "main"}), 422, \
        {"error": GM.TOO_BIG, "code": "too_big"}


def _no_branch(cv):
    cv.fake.add_repo("octocat", "Hello-World", HELLO)
    return ("POST", "/api/github/open", {"owner": "octocat", "repo": "Hello-World", "branch": "nope"}), 404, \
        {"error": GM.branch_not_found("nope"), "code": "branch_not_found"}


def _private_login(cv):
    cv.svc.snapshots.record("octocat", "secret-diary", branch="main", sha="a" * 40, private=True)
    return ("POST", "/api/github/open", {"owner": "octocat", "repo": "secret-diary", "branch": "main"}), 401, \
        {"error": GM.PRIVATE_LOGIN, "code": "private_login"}


def _not_found(cv):
    return ("POST", "/api/github/resolve", {"text": "octocat/nothing-here"}), 404, \
        {"error": GM.NOT_FOUND_ANON, "code": "not_found"}


def _bad_names(cv):
    return ("POST", "/api/github/open", {"owner": "../x", "repo": "Hello-World", "branch": ""}), 400, \
        {"error": GM.BAD_REQUEST}


@pytest.mark.parametrize("case", [_not_allowed, _sso, _suspended, _other_owner, _outdated, _rate_limited, _empty,
                                  _too_big, _no_branch, _private_login, _not_found, _bad_names],
                         ids=lambda f: f.__name__.strip("_"))
def test_errors_the_page_meets_carry_their_code_and_fields(cv, case):
    (method, path, data), code, body = case(cv)
    got_code, got = cv.call(method, path, data)
    retry = got.pop("retry_after", None)
    assert (got_code, got) == (code, body)
    if body.get("code") == "rate_limited":
        assert isinstance(retry, int) and retry > 0
    else:
        assert retry is None
    assert downloads(cv.fake) == []


# --- GitHub turned off, start-up, leaks ----------------------------------------------------------------------------

def test_github_turned_off_by_a_bad_fake_address(servers, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEVISION_GITHUB_FAKE", "https://127.0.0.1:1")  # set, but not a loopback http address
    cv = servers()
    st = status(cv)
    assert (st["logged_in"], st["note"], st["install_url"], st["settings_url"]) == (False, GM.FAKE_OFF, "", "")
    off = (500, {"error": GM.FAKE_OFF, "code": "login_config"})
    assert cv.post("/api/github/login", {}) == off
    assert cv.post("/api/github/resolve", {"text": "octocat/Hello-World"}) == off
    assert cv.post("/api/github/open", {"owner": "octocat", "repo": "Hello-World", "branch": ""}) == off
    folder = tmp_path / "still-works"
    folder.mkdir()
    (folder / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert cv.get("/api/analyze?path=" + quote(str(folder), safe=""))[0] == 200  # local folders are not affected


def test_the_start_up_cleanup_runs_when_the_server_starts(servers, tmp_path, fake_github):
    partial = tmp_path / "home" / "repos" / ".partial"
    old, young = partial / "abcdefabcdef", partial / "012345012345"
    old.mkdir(parents=True)
    young.mkdir()
    two_hours_ago = time.time() - 2 * 3600
    os.utime(old, (two_hours_ago, two_hours_ago))
    cv = servers()
    assert cv.svc.ready.wait(10)  # no GitHub call needed for it
    assert not old.exists() and young.exists()  # a young one may belong to another CodeVision
    assert fake_github.calls() == []


def test_no_token_code_or_download_address_leaks_anywhere(cv, fake_ollama, caplog, capfd, monkeypatch):
    caplog.set_level(logging.DEBUG)
    hooked = []
    monkeypatch.setattr(threading, "excepthook", lambda args: hooked.append(args.exc_type))
    cv.fake.add_repo("octocat", "secret-diary", DIARY, private=True)
    assert cv.post("/api/ai/settings", {"enabled": True})[0] == 200
    log_in_through_the_page(cv)
    cv.get("/api/github/repos")
    cv.get("/api/github/branches?owner=octocat&repo=secret-diary")
    cv.post("/api/github/resolve", {"text": "https://github.com/octocat/secret-diary"})
    _, done = open_repo(cv, "octocat", "secret-diary")
    assert done["state"] == "done"
    code, graph = cv.get(analyze_url("octocat", "secret-diary", "main", done["sha"]))
    assert code == 200
    assert cv.post("/api/ai/explain", {"project": graph["source"]["project"], "node": "diary.py"})[0] == 200
    cv.fake.expire_tokens()  # the next request is refused, the login is refreshed: both tokens rotate
    assert cv.get("/api/github/repos?refresh=1")[0] == 200
    assert cv.post("/api/github/open", {"owner": "octocat", "repo": "secret-diary", "branch": "main"})[0] == 200
    files = [p.read_bytes() for p in cv.home.rglob("*") if p.is_file()]
    assert cv.post("/api/github/logout", {"delete_private": True})[0] == 200
    files += [p.read_bytes() for p in cv.home.rglob("*") if p.is_file()]
    secrets = cv.fake.issued_secrets()
    assert len(secrets) >= 7  # two token pairs, the device code, the codeload token and the download address
    out, err = capfd.readouterr()
    logs = caplog.text
    replies = b"\n".join(cv.replies)
    prompts = fake_ollama.raw_requests()
    assert files and prompts
    for secret in secrets:
        assert secret.encode() not in replies
        assert secret not in out and secret not in err and secret not in logs and secret not in prompts
        assert not any(secret.encode() in data for data in files)
    assert hooked == []  # every daemon thread catches its own errors
