"""github/client.py and the conftest network guards, always against tests/fake_github.py or tiny local servers.

- the guards: no GitHub name resolves and no connection leaves this computer, in any test;
- CODEVISION_GITHUB_FAKE: only plain http to loopback with a port, anything else turns GitHub off;
- fixed headers; Authorization only to the API (and codeload's one-time retry for a private repo);
- no automatic redirects: one same-host 301/307/308, and the tarball 302 only to codeload;
- Link pagination, rate limits, error mapping, deadlines, downloads cut short;
- nothing secret in logs, stderr or exception texts.
"""
import contextlib
import http.server
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error

import pytest

import conftest
from codevision.github import messages as M
from codevision.github.base import (API_VERSION, USER_AGENT, GitHubError, Hosts, Secret, fake_base,
                                    hosts_from_env)
from codevision.github.client import GitHubClient, _rate_kind, _Raw, next_link
from codevision.github.store import TokenStore
from codevision.github.auth import TokenManager

SHA = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"


# --- helpers --------------------------------------------------------------------------------------------------

@contextlib.contextmanager
def tiny(route):
    """A loopback server: route(method, path, headers) -> (status, headers, body). Yields (base url, calls)."""
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _any(self):
            calls.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
            status, headers, body = route(self.command, self.path, self.headers)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _any

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def make_client(fake, *, login=True):
    """A client for the fake, logged in as octocat (without running the device flow) unless login=False."""
    hosts = Hosts.for_fake(fake.url)
    tokens = TokenManager(TokenStore(fake=True), sleep=lambda s: None)
    client = GitHubClient(hosts, tokens)
    tokens.bind(client)
    if login:
        tokens.accept(fake.issue())
        tokens.set_user("octocat", 583231)
    return client


def api_calls(fake, needle=""):
    return [c for c in fake.calls("/api") if needle in c["path"]]


# --- the conftest guards (self-tests) ----------------------------------------------------------------------------

@pytest.mark.parametrize("address", [("192.0.2.1", 443), ("8.8.8.8", 53), ("2001:db8::1", 443),
                                     ("api.github.com", 443), ("example.com", 80)])
def test_no_connection_leaves_this_computer(address):
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        with pytest.raises(ConnectionRefusedError):
            sock.connect(address)
        with pytest.raises(ConnectionRefusedError):
            sock.connect_ex(address)


@pytest.mark.parametrize("name", ["github.com", "api.github.com", "codeload.github.com", "API.GitHub.com.",
                                  "objects.githubusercontent.com", "github.githubassets.com", "octocat.github.io"])
def test_github_names_never_resolve(name):
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(name, 443)
    with pytest.raises(socket.gaierror):
        socket.gethostbyname(name)


def test_loopback_still_works_and_github_points_at_a_dead_port():
    assert conftest.is_local_address(("127.0.0.1", 1)) and conftest.is_local_address(("::1", 1))
    assert os.environ["CODEVISION_GITHUB_FAKE"] == conftest.DEAD_GITHUB_URL
    assert os.environ["CODEVISION_KEYRING"] == "memory"
    hosts = hosts_from_env()
    assert hosts.fake and not hosts.off and hosts.api == conftest.DEAD_GITHUB_URL + "/api"
    started = time.monotonic()
    with pytest.raises(GitHubError) as caught:
        GitHubClient(hosts).get("/zen")
    assert caught.value.code == "network" and caught.value.message == M.NETWORK
    assert time.monotonic() - started < 1.5  # a refused loopback connection is reported at once


def test_the_real_hosts_are_unreachable_from_tests():
    with pytest.raises(GitHubError) as caught:
        GitHubClient(Hosts.real()).get("/zen")
    assert caught.value.code == "network"


def test_the_data_folder_is_the_sessions_scratch_folder_never_the_real_one(monkeypatch):
    from codevision import explain, server

    assert os.environ["CODEVISION_HOME"] == str(conftest.SESSION_HOME) and explain.data_dir() == conftest.SESSION_HOME
    assert not conftest.is_real_data_dir(conftest.SESSION_HOME)
    if not os.environ.get("LOCALAPPDATA"):
        pytest.skip("no %LOCALAPPDATA% here")
    monkeypatch.delenv("CODEVISION_HOME")  # a test that forgot its own home, and builds a server
    assert conftest.is_real_data_dir(os.path.join(os.environ["LOCALAPPDATA"], "CodeVision", "repos"))
    with pytest.raises(RuntimeError, match="real CodeVision data folder"):
        server.create_server(port=0)
    del conftest.REAL_DATA_CALLS[:]  # this test's own call: the guard did its job


# --- which GitHub ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, base", [
    ("http://127.0.0.1:5000", "http://127.0.0.1:5000"),
    ("  http://localhost:5000/  ", "http://localhost:5000"),
    ("http://[::1]:5000", "http://[::1]:5000"),
    ("HTTP://127.0.0.1:5000", "http://127.0.0.1:5000"),
])
def test_the_fake_address_must_be_loopback_http_with_a_port(value, base):
    assert fake_base(value) == base
    assert Hosts.for_fake(base).codeload == base + "/codeload"


@pytest.mark.parametrize("value", ["https://127.0.0.1:5000", "http://127.0.0.1", "http://10.0.0.1:5000",
                                   "http://api.github.com:80", "http://127.0.0.1:5000/api", "http://u:p@127.0.0.1:5000",
                                   "http://127.0.0.1:5000?x=1", "http://127.0.0.1:0", "http://127.0.0.1:99999",
                                   "ftp://127.0.0.1:21", "127.0.0.1:5000", "http://127.0.0.1:5000#x",
                                   "", "   "])  # set but empty: a child process with the variable blanked
def test_a_wrong_fake_address_turns_github_off(monkeypatch, value):
    assert fake_base(value) is None
    monkeypatch.setenv("CODEVISION_GITHUB_FAKE", value)
    hosts = hosts_from_env()
    assert hosts.off and hosts.fake and hosts.api == "" and hosts.verify_url == ""
    with pytest.raises(GitHubError) as caught:
        GitHubClient(hosts).post_form("/login/device/code", {"client_id": "x"})
    assert (caught.value.code, caught.value.message, caught.value.status) == ("login_config", M.FAKE_OFF, 500)


def test_without_the_variable_the_real_hosts_are_used(monkeypatch):
    monkeypatch.delenv("CODEVISION_GITHUB_FAKE")
    hosts = hosts_from_env()
    assert hosts == Hosts.real() and not hosts.fake
    assert (hosts.verify_url, hosts.install_url, hosts.settings_url) == (
        "https://github.com/login/device", "https://github.com/apps/codevision-project-jun/installations/new",
        "https://github.com/settings/apps/authorizations")
    assert hosts.repo_url("octocat", "Hello-World") == "https://github.com/octocat/Hello-World"


# --- headers and where Authorization goes -----------------------------------------------------------------------

def test_api_requests_carry_the_fixed_headers_and_no_token_unless_asked(fake_github):
    fake_github.add_repo("octocat", "Hello-World", {"README.md": "# hi"})
    client = make_client(fake_github)
    client.get("/repos/octocat/Hello-World")
    (call,) = api_calls(fake_github)
    assert call["headers"]["user-agent"] == USER_AGENT == "CodeVision/0.1.0"
    assert call["headers"]["accept"] == "application/vnd.github+json"
    assert call["headers"]["x-github-api-version"] == API_VERSION == "2026-03-10"
    assert "authorization" not in call["headers"]
    client.get("/repos/octocat/Hello-World", auth="prefer")
    assert api_calls(fake_github)[-1]["headers"]["authorization"].startswith("Bearer ghu_")


def test_web_requests_are_forms_without_the_api_version_or_a_token(fake_github):
    client = make_client(fake_github)
    reply = client.post_form("/login/device/code", {"client_id": "Iv23liGLsLW4iMJ8LI4g"})
    assert reply["user_code"] and reply["device_code"]
    (call,) = fake_github.calls("/web")
    assert call["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert call["headers"]["accept"] == "application/json"
    assert "authorization" not in call["headers"] and "x-github-api-version" not in call["headers"]
    assert call["form"] == {"client_id": "Iv23liGLsLW4iMJ8LI4g"}


def test_authorization_goes_to_the_api_but_never_to_codeload(fake_github):
    fake_github.add_repo("octocat", "diary", {"a.md": "x"}, private=True)
    client = make_client(fake_github)
    with client.open_tarball("octocat", "diary", fake_github.head("octocat", "diary"), auth="require") as download:
        assert download.read(-1) == fake_github.tarball("octocat", "diary", fake_github.head("octocat", "diary"))
    (api,) = api_calls(fake_github, "/tarball/")
    (codeload,) = fake_github.calls("/codeload")
    assert api["headers"]["authorization"].startswith("Bearer ghu_")
    assert "authorization" not in codeload["headers"] and codeload["query"].startswith("token=")


def test_a_private_codeload_refusal_gets_one_retry_with_the_token(fake_github):
    fake_github.add_repo("octocat", "diary", {"a.md": "x"}, private=True)
    fake_github.set(codeload_auth="header")  # this codeload wants Authorization, not the ?token=
    client = make_client(fake_github)
    sha = fake_github.head("octocat", "diary")
    with client.open_tarball("octocat", "diary", sha, auth="require") as download:
        assert download.read(-1) == fake_github.tarball("octocat", "diary", sha)
    first, second = fake_github.calls("/codeload")
    assert "authorization" not in first["headers"] and second["headers"]["authorization"].startswith("Bearer ")


def test_a_public_tarball_never_sends_the_token_to_codeload_even_after_a_refusal(fake_github):
    fake_github.add_repo("octocat", "pub", {"a.md": "x"})
    client = make_client(fake_github, login=False)
    with pytest.raises(GitHubError) as caught:
        client.open_tarball("octocat", "pub", "0" * 40, auth="prefer")  # no such commit: codeload says 404
    assert caught.value.code == "not_found"
    assert len(fake_github.calls("/codeload")) == 1


# --- redirects ---------------------------------------------------------------------------------------------------

def test_one_same_host_301_is_followed_with_the_token_added_again(fake_github):
    fake_github.add_repo("octocat", "new-name", {"a.md": "x"}, renamed_from="octocat/old-name")
    client = make_client(fake_github)
    reply = client.get("/repos/octocat/old-name", auth="require")
    assert reply.data["full_name"] == "octocat/new-name"
    first, second = api_calls(fake_github)
    assert first["path"] == "/api/repos/octocat/old-name" and second["path"].startswith("/api/repositories/")
    assert first["headers"]["authorization"] == second["headers"]["authorization"]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_to_another_host_is_never_followed(status):
    with tiny(lambda m, p, h: (200, {}, b"{}")) as (other, other_calls):
        with tiny(lambda m, p, h: (status, {"Location": other + "/api/user"}, b"")) as (base, _):
            with pytest.raises(GitHubError) as caught:
                GitHubClient(Hosts.for_fake(base)).get("/user")
    assert caught.value.code == "bad_reply" and other_calls == []
    assert "127.0.0.1" not in str(caught.value) and "http" not in repr(caught.value)


def test_a_second_redirect_is_not_followed():
    def route(method, path, headers):
        return {"/api/a": (301, {"Location": "/api/b"}, b""), "/api/b": (301, {"Location": "/api/c"}, b"")}.get(
            path, (200, {}, b"{}"))

    with tiny(route) as (base, calls):
        with pytest.raises(GitHubError) as caught:
            GitHubClient(Hosts.for_fake(base)).get("/a")
    assert caught.value.code == "bad_reply"
    assert [c[1] for c in calls] == ["/api/a", "/api/b"]


@pytest.mark.parametrize("location, ok", [
    ("https://codeload.github.com/o/r/legacy.tar.gz/abc", True),
    ("https://codeload.github.com:443/o/r/legacy.tar.gz/abc?token=XYZ", True),
    ("http://codeload.github.com/o/r/legacy.tar.gz/abc", False),
    ("https://codeload.github.com:8443/o/r", False),
    ("https://user@codeload.github.com/o/r", False),
    ("https://user:pw@codeload.github.com/o/r", False),
    ("https://codeload.github.com.evil.example/o/r", False),
    ("https://evil.example/codeload.github.com/o/r", False),
    ("//evil.example/x", False),
    ("/repos/o/r/tarball", False),
    ("https://codeload.github.com/o/r\nX: y", False),
    ("", False),
])
def test_the_tarball_redirect_must_point_at_codeload(location, ok):
    client = GitHubClient(Hosts.real())
    target = client.codeload_target(location, "https://api.github.com/repos/o/r/tarball/" + SHA)
    assert (target is not None) is ok


def test_a_tarball_redirect_somewhere_else_is_refused():
    with tiny(lambda m, p, h: (200, {}, b"tarball")) as (other, other_calls):
        def route(method, path, headers):
            return 302, {"Location": other + "/codeload/o/r/legacy.tar.gz/" + SHA}, b""

        with tiny(route) as (base, _):
            with pytest.raises(GitHubError) as caught:
                GitHubClient(Hosts.for_fake(base)).open_tarball("o", "r", SHA, auth="none")
    assert caught.value.code == "bad_reply" and other_calls == []


# --- pagination ----------------------------------------------------------------------------------------------------

def test_paginate_follows_link_next_until_the_end(fake_github):
    fake_github.add_repo("octocat", "many", {"a": "x"}, branches={f"b{i:03d}": None for i in range(250)})
    items, truncated = make_client(fake_github).paginate("/repos/octocat/many/branches", auth="none")
    assert len(items) == 250 and not truncated
    assert [c["query"] for c in api_calls(fake_github)] == ["per_page=100", "per_page=100&page=2",
                                                             "per_page=100&page=3"]
    # like GitHub's, the next-page links name the repo by its id
    assert [c["path"].split("/")[2] for c in api_calls(fake_github)] == ["repos", "repositories", "repositories"]


def test_paginate_stops_at_max_items(fake_github):
    fake_github.add_repo("octocat", "many", {"a": "x"}, branches={f"b{i:03d}": None for i in range(250)})
    items, truncated = make_client(fake_github).paginate("/repos/octocat/many/branches", auth="none", max_items=150)
    assert len(items) == 150 and truncated
    assert len(api_calls(fake_github)) == 2


def test_paginate_refuses_a_next_page_on_another_host():
    with tiny(lambda m, p, h: (200, {}, b"[]")) as (other, other_calls):
        link = f'<{other}/api/items?page=2>; rel="next"'
        with tiny(lambda m, p, h: (200, {"Link": link}, b"[1, 2]")) as (base, _):
            with pytest.raises(GitHubError) as caught:
                GitHubClient(Hosts.for_fake(base)).paginate("/items", auth="none")
    assert caught.value.code == "bad_reply" and other_calls == []


def test_paginate_has_a_total_deadline(fake_github):
    fake_github.add_repo("octocat", "many", {"a": "x"}, branches={f"b{i:03d}": None for i in range(450)})
    fake_github.set(delay=0.3)
    with pytest.raises(GitHubError) as caught:
        make_client(fake_github).paginate("/repos/octocat/many/branches", auth="none", deadline=0.5)
    assert caught.value.code == "timeout"


@pytest.mark.parametrize("header, expected", [
    ('<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"',
     "https://api.github.com/x?page=2"),
    ('<https://api.github.com/x?page=9>; rel="last", <https://api.github.com/x?page=3>; rel="next"',
     "https://api.github.com/x?page=3"),
    ('<https://api.github.com/x?page=1>; rel="prev first"', None),
    ("", None),
])
def test_link_headers(header, expected):
    assert next_link(header) == expected


# --- rate limits -------------------------------------------------------------------------------------------------

def test_a_primary_rate_limit_blocks_that_mode_until_the_reset(fake_github):
    fake_github.add_repo("octocat", "Hello-World", {"a": "x"})
    client = make_client(fake_github, login=False)
    fake_github.set(rate="primary", rate_count=1, rate_reset=600)
    with pytest.raises(GitHubError) as caught:
        client.get("/repos/octocat/Hello-World", auth="prefer")
    assert caught.value.code == "rate_limited" and caught.value.status == 429
    assert 590 <= caught.value.reply()["retry_after"] <= 601
    assert caught.value.message == M.RATE_LIMITED(10, anonymous=True)
    seen = len(api_calls(fake_github))
    with pytest.raises(GitHubError) as again:
        client.get("/repos/octocat/Hello-World")
    assert again.value.code == "rate_limited" and len(api_calls(fake_github)) == seen  # GitHub is not asked
    assert client.rate_block("anon") > 500 and client.rate_block("user") == 0


def test_the_user_mode_is_not_blocked_by_an_anonymous_limit(fake_github):
    fake_github.add_repo("octocat", "Hello-World", {"a": "x"})
    client = make_client(fake_github)
    fake_github.set(rate="primary", rate_count=1)
    with pytest.raises(GitHubError):
        client.get("/repos/octocat/Hello-World")
    assert client.get("/repos/octocat/Hello-World", auth="require").status == 200


@pytest.mark.parametrize("rate, seconds", [("secondary", 60), ("secondary403", 60)])
def test_secondary_rate_limits(fake_github, rate, seconds):
    fake_github.add_repo("octocat", "Hello-World", {"a": "x"})
    fake_github.set(rate=rate, rate_count=1, rate_retry=seconds)
    with pytest.raises(GitHubError) as caught:
        make_client(fake_github).get("/repos/octocat/Hello-World", auth="require")
    assert caught.value.code == "rate_limited" and caught.value.message == M.RATE_SECONDARY
    assert caught.value.reply()["retry_after"] == seconds


def test_rate_limit_headers_are_read_in_any_case():
    from email.message import Message

    for name in ("X-RateLimit-Remaining", "x-ratelimit-remaining", "X-RATELIMIT-REMAINING"):
        headers = Message()
        headers[name] = "0"
        assert _rate_kind(_Raw(403, headers, b""), "") == "primary"
    headers = Message()
    headers["Retry-After"] = "30"
    assert _rate_kind(_Raw(429, headers, b""), "") == "secondary"
    assert _rate_kind(_Raw(403, Message(), b""), "resource not accessible by integration") is None


# --- errors ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fail, code", [("500", "server_error"), ("badjson", "bad_reply"), ("drop", "network")])
def test_failures_become_fixed_errors(fake_github, fail, code):
    fake_github.add_repo("octocat", "Hello-World", {"a": "x"})
    fake_github.set(fail=fail, fail_on="api")
    with pytest.raises(GitHubError) as caught:
        make_client(fake_github, login=False).get("/repos/octocat/Hello-World")
    assert caught.value.code == code and caught.value.message == M.for_code(code)


def test_a_stalled_reply_times_out(fake_github):
    fake_github.set(fail="slow", slow=5, fail_on="api")
    started = time.monotonic()
    with pytest.raises(GitHubError) as caught:
        make_client(fake_github, login=False).get("/user", timeout=0.3)
    assert caught.value.code == "timeout" and time.monotonic() - started < 3


def test_a_trickling_reply_hits_the_total_deadline(fake_github):
    fake_github.add_repo("octocat", "Hello-World", {"a": "x"})
    fake_github.set(fail="trickle", trickle=0.05, fail_on="api")
    started = time.monotonic()
    with pytest.raises(GitHubError) as caught:  # each byte comes in time, but the whole reply does not
        make_client(fake_github, login=False).get("/repos/octocat/Hello-World", timeout=2, total=0.6)
    assert caught.value.code == "timeout" and time.monotonic() - started < 2


def test_tls_problems_are_tls_errors():
    class Opener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))

    with pytest.raises(GitHubError) as caught:
        GitHubClient(Hosts.real(), opener=Opener()).get("/zen")
    assert caught.value.code == "tls" and caught.value.message == M.TLS
    # the café / hotel Wi-Fi sign-in page is the likeliest cause at home or on the road: the sentence ends with it
    assert M.TLS.endswith("카페·호텔 와이파이라면 브라우저에서 먼저 와이파이 로그인을 해 주세요.")


def test_a_json_reply_over_5_mib_is_refused():
    big = b"[" + b"1," * (3 * 1024 * 1024) + b"1]"
    with tiny(lambda m, p, h: (200, {"Content-Type": "application/json"}, big)) as (base, _):
        with pytest.raises(GitHubError) as caught:
            GitHubClient(Hosts.for_fake(base)).get("/big")
    assert caught.value.code == "bad_reply"


@pytest.mark.parametrize("reply, code", [("410", "outdated"), ("400", "outdated")])
def test_a_refused_api_version_asks_for_a_newer_codevision(fake_github, reply, code):
    fake_github.set(api_reply=reply)
    with pytest.raises(GitHubError) as caught:
        make_client(fake_github, login=False).get("/zen")
    assert caught.value.code == code and caught.value.message == M.OUTDATED


@pytest.mark.parametrize("kind, code", [("sso", "sso_required"), ("not_allowed", "not_allowed"),
                                        ("suspended", "suspended")])
def test_403s_say_why(fake_github, kind, code):
    fake_github.add_org("acme")
    fake_github.add_installation("acme", members=["octocat"], suspended=kind == "suspended")
    fake_github.add_repo("acme", "tool", {"a": "x"}, private=True, sso=kind == "sso", not_allowed=kind == "not_allowed",
                         collaborators=["octocat"])
    with pytest.raises(GitHubError) as caught:
        make_client(fake_github).get("/repos/acme/tool", auth="prefer")
    assert caught.value.code == code and caught.value.status == {"sso_required": 403, "suspended": 403,
                                                                 "not_allowed": 409}[code]
    assert caught.value.message == M.for_code(code) and caught.value.gh_status == 403
    if code in ("not_allowed", "suspended"):
        assert caught.value.reply()["install_url"] == fake_github.url + "/web/apps/codevision-project-jun/installations/new"


def test_prefer_retries_a_public_repo_without_the_token(fake_github):
    fake_github.add_org("acme")
    fake_github.add_repo("acme", "open", {"a": "x"}, sso=True)  # the token gets 403 SSO; anonymous works
    reply = make_client(fake_github).get("/repos/acme/open", auth="prefer")
    assert reply.data["full_name"] == "acme/open"
    first, second = api_calls(fake_github)
    assert "authorization" in first["headers"] and "authorization" not in second["headers"]


def test_a_401_refreshes_once_and_retries_once(fake_github):
    client = make_client(fake_github)
    fake_github.expire_tokens()
    assert client.get("/user", auth="require").data["login"] == "octocat"
    refreshes = [c for c in fake_github.calls("/web") if c["form"].get("grant_type") == "refresh_token"]
    assert len(refreshes) == 1 and len(api_calls(fake_github, "/user")) == 2


def test_a_second_401_is_login_needed_never_a_loop(fake_github):
    rejected = []

    class Tokens:  # a login whose tokens GitHub never accepts
        def logged_in(self):
            return True

        def login(self):
            return "octocat"

        def access(self):
            return Secret("ghu_" + "a" * 36)

        def refreshed_after_401(self, used):
            return Secret("ghu_" + "b" * 36)

        def rejected(self, used):
            rejected.append(used)

        def login_needed_message(self):
            return M.LOGIN_EXPIRED

    client = GitHubClient(Hosts.for_fake(fake_github.url), Tokens())
    with pytest.raises(GitHubError) as caught:
        client.get("/user", auth="require")
    assert (caught.value.code, caught.value.message, caught.value.status) == ("login_needed", M.LOGIN_EXPIRED, 401)
    assert len(api_calls(fake_github, "/user")) == 2 and rejected == [Secret("ghu_" + "b" * 36)]


def test_require_without_a_login_never_asks_github(fake_github):
    with pytest.raises(GitHubError) as caught:
        make_client(fake_github, login=False).get("/user", auth="require")
    assert (caught.value.code, caught.value.message) == ("login_needed", M.LOGIN_NEEDED)
    assert api_calls(fake_github) == []


def test_a_closed_client_refuses_new_requests(fake_github):
    client = make_client(fake_github, login=False)
    client.close()
    with pytest.raises(GitHubError) as caught:
        client.get("/zen")
    assert caught.value.code == "closing" and caught.value.status == 503 and fake_github.calls() == []


# --- downloads -------------------------------------------------------------------------------------------------------

def test_a_download_cut_midway_is_interrupted(fake_github):
    fake_github.add_repo("octocat", "big", {f"f{i}.txt": os.urandom(4096).hex() for i in range(40)})
    fake_github.set(fail="cut", fail_on="codeload")
    download = make_client(fake_github).open_tarball("octocat", "big", fake_github.head("octocat", "big"), auth="none")
    assert download.length == len(fake_github.tarball("octocat", "big", fake_github.head("octocat", "big")))
    with pytest.raises(GitHubError) as caught:
        while download.read(65536):
            pass
    assert caught.value.code == "interrupted" and caught.value.message == M.INTERRUPTED
    download.close()


def test_a_chunked_download_has_no_length_and_arrives_whole(fake_github):
    fake_github.add_repo("octocat", "pub", {f"f{i}.txt": os.urandom(2048).hex() for i in range(20)})
    fake_github.set(chunked=True)
    sha = fake_github.head("octocat", "pub")
    with make_client(fake_github).open_tarball("octocat", "pub", sha, auth="none") as download:
        assert download.length is None
        data = b"".join(iter(lambda: download.read(10000), b""))
    assert data == fake_github.tarball("octocat", "pub", sha)


def test_abort_wakes_a_read_blocked_in_another_thread(fake_github):
    fake_github.add_repo("octocat", "pub", {f"f{i}.txt": os.urandom(4096).hex() for i in range(40)})
    fake_github.hold("codeload")
    download = make_client(fake_github).open_tarball("octocat", "pub", fake_github.head("octocat", "pub"), auth="none")
    outcome = []

    def reader():
        try:
            while download.read(65536):
                pass
            outcome.append("eof")
        except GitHubError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive()  # waiting for bytes that the held fake does not send
    started = time.monotonic()
    download.abort()
    thread.join(5)
    assert outcome == ["interrupted"] and time.monotonic() - started < 2
    download.close()
    fake_github.release()


# --- nothing secret leaks ------------------------------------------------------------------------------------------------

def test_no_token_url_or_code_reaches_the_logs_or_stderr(fake_github, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    fake_github.add_repo("octocat", "diary", {"a.md": "x"}, private=True)
    client = make_client(fake_github)
    client.post_form("/login/device/code", {"client_id": "Iv23liGLsLW4iMJ8LI4g"})
    sha = fake_github.head("octocat", "diary")
    fake_github.expire_tokens()  # a 401 and a refresh on the way
    with client.open_tarball("octocat", "diary", sha, auth="require") as download:
        download.read(-1)
    for fail in ("500", "badjson", "drop"):
        fake_github.set(fail=fail, fail_count=1)
        with pytest.raises(GitHubError) as caught:
            client.get("/repos/octocat/diary", auth="require")
        assert fake_github.url not in str(caught.value) and "http" not in repr(caught.value)
    out, err = capfd.readouterr()
    text = caplog.text + out + err
    secrets = fake_github.issued_secrets()
    assert len(secrets) >= 6  # tokens, the refreshed pair, the device code, the codeload token and address
    for secret in secrets:
        assert secret not in text
    assert "/codeload/" not in text and "token=" not in text and fake_github.url not in text


# --- the fake itself: tarballs, serve mode and its pages (other packages build on these) ---------------------------

def test_the_fake_tarball_is_shaped_like_githubs():
    import io
    import tarfile

    from fake_github import make_tarball

    data = make_tarball({"src/b.py": "b", "src/a/x.py": "x", "README.md": "# hi", "libs/sub/": None},
                        top="octocat-r-7fd1a60", comment=SHA)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tf:
        names = [(m.name, m.type) for m in tf]
        assert tf.pax_headers == {"comment": SHA}
    assert names[0] == ("octocat-r-7fd1a60", tarfile.DIRTYPE)
    assert [n for n, _ in names] == ["octocat-r-7fd1a60", "octocat-r-7fd1a60/README.md", "octocat-r-7fd1a60/libs",
                                     "octocat-r-7fd1a60/libs/sub", "octocat-r-7fd1a60/src", "octocat-r-7fd1a60/src/a",
                                     "octocat-r-7fd1a60/src/a/x.py", "octocat-r-7fd1a60/src/b.py"]


def test_the_fake_tarball_lists_entries_in_git_archive_order(tmp_path):
    # depth first, every folder right before its contents, siblings by their bytes with a folder's name read
    # as "name/" (so a.txt comes before a/): the extractor's package rule for build/ and dist/ relies on it
    import io
    import tarfile

    from codevision.github.extract import extract_stream
    from fake_github import make_tarball

    files = {"dist/x.js": "x", "build/tool.py": "t", "build/__init__.py": "", "build/README.md": "r",
             "a/x.py": "x", "a.txt": "t", "B.md": "b"}
    data = make_tarball(files, top="t", comment=SHA)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tf:
        names = [m.name for m in tf]
    assert names == ["t", "t/B.md", "t/a.txt", "t/a", "t/a/x.py", "t/build", "t/build/README.md",
                     "t/build/__init__.py", "t/build/tool.py", "t/dist", "t/dist/x.js"]
    extract_stream(io.BytesIO(data), tmp_path / "snap", expect_commit=SHA)
    kept = sorted(p.relative_to(tmp_path / "snap").as_posix() for p in (tmp_path / "snap").rglob("*") if p.is_file())
    assert kept == ["B.md", "a.txt", "a/x.py", "build/README.md", "build/__init__.py", "build/tool.py"]
    assert (tmp_path / "snap" / "dist").is_dir()  # held until its __init__.py could no longer come: hidden, empty


def test_the_fake_rate_headers_keep_one_reset_and_the_tarball_costs_nothing(fake_github):
    # as seen on real GitHub (2026-11-25): one reset time for the hour; the tarball redirect uses no core quota
    fake_github.add_repo("octocat", "Hello-World", {"a": "x"})
    client = make_client(fake_github, login=False)
    first = client.get("/repos/octocat/Hello-World", auth="none").headers
    sha = fake_github.head("octocat", "Hello-World")
    client.open_tarball("octocat", "Hello-World", sha, auth="none").close()
    second = client.get("/repos/octocat/Hello-World", auth="none").headers
    assert first["x-ratelimit-reset"] == second["x-ratelimit-reset"]
    assert (int(first["x-ratelimit-used"]), int(second["x-ratelimit-used"])) == (1, 2)


def test_the_hostile_set_survives_into_the_tarball():
    import io
    import tarfile

    from fake_github import HOSTILE, bomb, make_tarball

    # Level 1: tarfile's stream reader is slow on very compressible members (a level-6 bomb takes seconds).
    data = make_tarball({"README.md": "x"}, top="t", comment=SHA, extra=HOSTILE + (bomb(),), compresslevel=1)
    assert len(data) < 1_000_000  # the 50 MiB bomb compresses to almost nothing
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tf:
        members = {m.name: m for m in tf}
    for name in ("t/../escape.txt", "../outside.txt", "/abs.txt", "C:/abs.txt", "D:rel.txt", "t/CON", "t/COM\u00b9",
                 "t/ads.txt:stream", "t/back\\slash.txt", "t/ctl\x01.txt", "t/lone\udcff.txt", "t/space ", "t/trail.",
                 "t/caf\u00e9.txt", "t/cafe\u0301.txt", "t/LONGFI~1.TXT", "other-top-0000000/x.txt"):
        assert name in members, name
    assert members["t/link-out"].issym() and members["t/hard"].islnk() and members["t/hard"].linkname == "t/README.md"
    assert members["t/dev-null"].ischr() and members["t/pipe"].isfifo()
    assert members["t/longer-than-header.txt"].size == 5 and members["t/bomb.txt"].size == 50 * 1024 * 1024
    assert max(len(n) for n in members) > 260


def test_the_extractor_fixtures():
    import io
    import tarfile

    from fake_github import (CASE_COLLISION_FILES, RESERVED_NAME_FILES, committed_venv_files, long_name_files,
                             node_modules_files, node_modules_tarball, submodule_layout)

    assert len(node_modules_files(60_000)) == 60_000 and len(committed_venv_files()) > 250
    files, extra = submodule_layout()
    assert "[submodule" in files[".gitmodules"] and [m.kind for m in extra] == ["dir", "dir"]
    assert max(len(p) for p in long_name_files()) > 300 and "CON.txt" in RESERVED_NAME_FILES
    assert {p.lower() for p in CASE_COLLISION_FILES} != set(CASE_COLLISION_FILES)
    with tarfile.open(fileobj=io.BytesIO(node_modules_tarball(500)), mode="r|gz") as tf:
        assert sum(1 for m in tf if m.isfile()) == 502


def test_the_fake_device_pages_approve_a_login(fake_github):
    import urllib.request

    client = make_client(fake_github, login=False)
    flow = client.post_form("/login/device/code", {"client_id": "Iv23liGLsLW4iMJ8LI4g"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    page = opener.open(fake_github.url + "/web/login/device").read().decode()
    assert "Continue" in page
    form = f"user_code={flow['user_code']}".encode()
    page = opener.open(fake_github.url + "/web/login/device", data=form).read().decode()
    assert "Authorize CodeVision-Project-Jun" in page
    page = opener.open(fake_github.url + "/web/login/device/authorize",
                       data=form + b"&decision=authorize").read().decode()
    assert "all set" in page
    reply = client.post_form("/login/oauth/access_token", {
        "client_id": "Iv23liGLsLW4iMJ8LI4g", "device_code": flow["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    assert reply["access_token"].startswith("ghu_") and reply["token_type"] == "bearer"


def test_the_fake_refuses_a_client_secret_and_the_users_ports(fake_github):
    from fake_github import FakeGitHub

    reply = make_client(fake_github, login=False).post_form("/login/oauth/access_token", {
        "client_id": "Iv23liGLsLW4iMJ8LI4g", "client_secret": "x", "grant_type": "refresh_token"})
    assert reply["error"] == "client_secret_not_allowed"
    for port in (8799, 8800, 11434):
        with pytest.raises(ValueError):
            FakeGitHub(port)
    with pytest.raises(ValueError):
        FakeGitHub(host="0.0.0.0")


def test_the_fake_controls_delete_a_branch_and_script_the_device_flow(fake_github):
    """POST /__fake/delete_branch and /__fake/script, which the browser checks use (the gone-branch note, slow_down)."""
    import json
    import urllib.request

    fake_github.add_repo("octocat", "Hello-World", {"README.md": "# hi"}, branches={"main": None, "docs": None})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def ctl(name, body):
        req = urllib.request.Request(fake_github.url + "/__fake/" + name, data=json.dumps(body).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=10) as reply:
                return reply.status
        except urllib.error.HTTPError as exc:
            return exc.code

    assert ctl("delete_branch", {"owner": "octocat", "repo": "Hello-World", "branch": "docs"}) == 200
    client = make_client(fake_github, login=False)
    assert [b["name"] for b in client.paginate("/repos/octocat/Hello-World/branches", auth="none")[0]] == ["main"]
    assert ctl("delete_branch", {"owner": "octocat", "repo": "Hello-World", "branch": "main"}) == 400  # the default stays
    assert ctl("script", {"steps": "slow_down"}) == 400
    assert ctl("script", {"steps": ["slow_down", "bad_code"]}) == 200
    flow = client.post_form("/login/device/code", {"client_id": "Iv23liGLsLW4iMJ8LI4g"})
    grant = {"client_id": "Iv23liGLsLW4iMJ8LI4g", "device_code": flow["device_code"],
             "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
    first = client.post_form("/login/oauth/access_token", grant)
    assert first["error"] == "slow_down" and first["interval"] > flow["interval"]
    assert client.post_form("/login/oauth/access_token", grant)["error"] == "bad_verification_code"


def test_the_fake_serve_mode_with_demo_repos():
    import subprocess
    import sys
    import urllib.request

    proc = subprocess.Popen([sys.executable, str(conftest.FAKE_GITHUB), "--port", "0", "--demo", "--max-life", "60"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        line = proc.stdout.readline()
        assert line.startswith("fake github on http://127.0.0.1:"), line + proc.stderr.read()
        base = line.split()[3]
        client = GitHubClient(Hosts.for_fake(base))
        assert client.get("/repos/octocat/Hello-World").data["full_name"] == "octocat/Hello-World"
        with pytest.raises(GitHubError) as caught:
            client.get("/repos/octocat/empty-repo/commits/heads/main", errors={409: "empty_repo"})
        assert caught.value.code == "empty_repo"
        branches = client.paginate("/repos/octocat/hostile-names/branches", auth="none")[0]
        assert "<img/src/onerror=alert(1)>" in {b["name"] for b in branches}
        assert client.get("/repos/octocat/old-name").data["name"] == "new-name"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(urllib.request.Request(base + "/__fake/stop", data=b"{}"))
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.stderr.close()
