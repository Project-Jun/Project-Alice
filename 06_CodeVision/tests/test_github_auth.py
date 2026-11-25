"""github/auth.py: the device-flow login and the token lifetime, against tests/fake_github.py.

- the code request: client_id only (no scope, no client secret), JSON asked for;
- every answer GitHub can give while polling, slow_down, network trouble and the local deadline;
- one flow per server: start twice gives the same flow, cancel, close never waits, no login while logged in;
- refreshes: 5 minutes early, saved before use, one at a time, once after a 401;
- a refused refresh re-reads the store, then logs out with the normal sentence; a network error keeps the login;
- status() never waits for GitHub.
"""
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from codevision.github import messages as M
from codevision.github.auth import DeviceLogin, TokenManager
from codevision.github.base import CLIENT_ID, GitHubError, Hosts, Secret
from codevision.github.client import GitHubClient
from codevision.github.store import SERVICE, USERNAME, Tokens, TokenStore


@pytest.fixture
def gh(fake_github):
    hosts = Hosts.for_fake(fake_github.url)
    store = TokenStore(fake=True)
    tokens = TokenManager(store, sleep=lambda s: None)
    client = GitHubClient(hosts, tokens)
    tokens.bind(client)
    login = DeviceLogin(client, tokens)
    yield SimpleNamespace(fake=fake_github, hosts=hosts, store=store, tokens=tokens, client=client, login=login)
    login.close()


def wait_end(login, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = login.view()
        if view is not None and view["state"] not in ("waiting", "finishing"):
            return view
        time.sleep(0.01)
    raise AssertionError(f"the login did not end: {login.view()}")


def logged_in(gh, **fake_settings):
    if fake_settings:
        gh.fake.set(**fake_settings)
    gh.tokens.accept(gh.fake.issue())
    gh.tokens.set_user("octocat", 583231)


def refreshes(fake):
    return [c for c in fake.calls("/web") if c["form"].get("grant_type") == "refresh_token"]


def polls(fake):
    return [c for c in fake.calls("/web") if c["form"].get("grant_type", "").endswith("device_code")]


# --- the code -------------------------------------------------------------------------------------------------

def test_the_code_request_sends_only_the_client_id(gh):
    view = gh.login.start()
    (call,) = gh.fake.calls("/web/login/device/code")
    assert call["form"] == {"client_id": CLIENT_ID}  # no scope, no client secret
    assert call["headers"]["accept"] == "application/json"
    assert list(view) == ["state", "user_code", "verification_uri", "expires_in", "message", "code"]
    assert view["state"] == "waiting" and view["message"] == M.WAITING and view["code"] == ""
    assert view["verification_uri"] == gh.fake.url + "/web/login/device" == gh.hosts.verify_url
    assert 899 <= view["expires_in"] <= 900 and len(view["user_code"]) == 9


def test_the_device_code_stays_on_the_server(gh):
    view = gh.login.start()
    device_code = [s for s in gh.fake.issued_secrets() if len(s) == 40][0]
    assert device_code not in repr(view) and device_code not in repr(gh.login._flow.__dict__)


def test_the_verification_address_is_never_taken_from_the_reply(gh, monkeypatch):
    real = gh.client.post_form

    def lying(path, fields, timeout=15, *, op="oauth"):
        reply = real(path, fields, timeout, op=op)
        if path.endswith("/device/code"):
            reply["verification_uri"] = "https://evil.example/login/device"
        return reply

    monkeypatch.setattr(gh.client, "post_form", lying)
    assert gh.login.start()["verification_uri"] == gh.fake.url + "/web/login/device"


@pytest.mark.parametrize("change", [{"user_code": "abcd-efgh"}, {"user_code": "<script>"}, {"device_code": ""},
                                    {"device_code": "x" * 201}, {"error": "device_flow_disabled"}])
def test_an_odd_code_reply_is_login_config(gh, monkeypatch, change):
    real = gh.client.post_form
    monkeypatch.setattr(gh.client, "post_form", lambda *a, **k: dict(real(*a, **k), **change))
    with pytest.raises(GitHubError) as caught:
        gh.login.start()
    assert (caught.value.code, caught.value.status, caught.value.message) == ("login_config", 500, M.LOGIN_CONFIG)
    assert gh.login.view() is None


def test_device_flow_disabled_is_login_config(gh):
    gh.fake.set(device_flow_disabled=True)
    with pytest.raises(GitHubError) as caught:
        gh.login.start()
    assert caught.value.code == "login_config"


@pytest.mark.parametrize("fail, code", [("drop", "network"), ("500", "server_error"), ("badjson", "bad_reply")])
def test_github_unreachable_at_start(gh, fail, code):
    gh.fake.set(fail=fail, fail_on="web")
    with pytest.raises(GitHubError) as caught:
        gh.login.start()
    assert caught.value.code == code


# --- polling ----------------------------------------------------------------------------------------------------

def test_pending_then_success_saves_the_login_with_its_name(gh, memory_keyring):
    gh.fake.script(["pending", "pending", "ok"])
    gh.login.start()
    view = wait_end(gh.login)
    assert view == {"state": "done", "message": M.DONE, "code": ""}
    assert gh.tokens.logged_in() and gh.tokens.login() == "octocat" and gh.tokens.user_id() == 583231
    assert len(polls(gh.fake)) == 3
    (blob,) = memory_keyring.entries().values()
    assert '"login":"octocat"' in blob and gh.fake.issued_tokens()[0] in blob


def test_slow_down_uses_the_interval_github_sends(gh):
    gh.fake.script(["slow_down", "ok"])  # the fake adds slow_step (0.05 s) to the interval
    gh.login.start()
    wait_end(gh.login)
    first, second = polls(gh.fake)
    assert second["at"] - first["at"] >= 0.095


def test_slow_down_without_an_interval_adds_five_seconds(gh):
    assert gh.login._slower({"error": "slow_down"}, 5.0) == 10.0
    assert gh.login._slower({"error": "slow_down", "interval": 7}, 5.0) == 7.0
    assert gh.login._slower({"error": "slow_down", "interval": 500}, 5.0) == 10.0
    assert gh.login._slower({"error": "slow_down", "interval": True}, 5.0) == 10.0


@pytest.mark.parametrize("step, state, message, code", [
    ("deny", "denied", M.DENIED, ""),
    ("expired", "expired", M.EXPIRED, ""),
    ("token_expired", "expired", M.EXPIRED, ""),
    ("incorrect", "failed", M.LOGIN_CODE, "login_code"),
    ("bad_code", "failed", M.LOGIN_CODE, "login_code"),
    ("unverified", "failed", M.UNVERIFIED_EMAIL, "unverified_email"),
    ("disabled", "failed", M.LOGIN_CONFIG, "login_config"),
    ("unknown", "failed", M.LOGIN_CONFIG, "login_config"),
    ("bad_token", "failed", M.LOGIN_CONFIG, "login_config"),
])
def test_every_ending(gh, step, state, message, code):
    gh.fake.script([step])
    gh.login.start()
    assert wait_end(gh.login) == {"state": state, "message": message, "code": code}
    assert not gh.tokens.logged_in()
    time.sleep(0.15)
    assert len(polls(gh.fake)) == 1  # nothing more is asked after an ending


def test_network_trouble_keeps_polling_and_says_so(gh):
    gh.login.start()
    gh.fake.set(fail="500", fail_on="web")
    deadline = time.monotonic() + 5
    while gh.login.view()["message"] != M.POLL_TROUBLE:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert gh.login.view()["state"] == "waiting"
    gh.fake.set(fail="none")
    gh.fake.approve()
    assert wait_end(gh.login)["state"] == "done"


def test_the_local_deadline_ends_the_flow(gh):
    gh.fake.set(expires_in=0.3)
    gh.login.start()
    view = wait_end(gh.login)
    assert view == {"state": "expired", "message": M.EXPIRED, "code": ""}


def test_a_view_after_the_deadline_is_expired_even_if_the_poller_sleeps(gh):
    gh.fake.set(expires_in=0.3, interval=30)
    gh.login.start()
    time.sleep(0.35)
    assert gh.login.view()["state"] == "expired"


# --- one flow per server -------------------------------------------------------------------------------------------

def test_starting_again_returns_the_same_waiting_flow(gh):
    first = gh.login.start()
    second = gh.login.start()
    assert first["user_code"] == second["user_code"]
    assert len(gh.fake.calls("/web/login/device/code")) == 1


def test_concurrent_starts_ask_github_once(gh):
    views = []
    threads = [threading.Thread(target=lambda: views.append(gh.login.start())) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len({v["user_code"] for v in views}) == 1 and len(views) == 6
    assert len(gh.fake.calls("/web/login/device/code")) == 1


def test_cancel_stops_the_flow_and_drops_the_code(gh):
    gh.login.start()
    view = gh.login.cancel()
    assert view == {"state": "cancelled", "message": M.CANCELLED, "code": ""}
    assert gh.login._flow.device_code is None
    seen = len(polls(gh.fake))
    time.sleep(0.2)
    assert len(polls(gh.fake)) == seen
    assert gh.login.cancel() == view  # an ended flow comes back unchanged


def test_after_an_end_a_new_start_gets_a_new_code(gh):
    first = gh.login.start()
    gh.login.cancel()
    second = gh.login.start()
    assert second["state"] == "waiting" and second["user_code"] != first["user_code"]
    assert len(gh.fake.calls("/web/login/device/code")) == 2


def test_close_returns_at_once_and_refuses_new_logins(gh):
    gh.login.start()
    started = time.monotonic()
    gh.login.close()
    assert time.monotonic() - started < 0.1
    with pytest.raises(GitHubError) as caught:
        gh.login.start()
    assert caught.value.code == "closing"
    time.sleep(0.15)
    assert not [t for t in threading.enumerate() if t.name == "codevision-github-login" and t.is_alive()]


def test_no_login_while_logged_in(gh):
    logged_in(gh)
    with pytest.raises(GitHubError) as caught:
        gh.login.start()
    assert (caught.value.code, caught.value.status, caught.value.message) == (
        "already_logged_in", 409, M.ALREADY_LOGGED_IN)
    assert gh.fake.calls("/web/login/device/code") == []


def test_no_second_code_while_a_login_is_finishing(gh, monkeypatch):
    # between the approval and the saved tokens, logged_in() is still False: a second start used to get a new
    # code, and approving that one would have replaced the tokens of the first
    held, go = threading.Event(), threading.Event()
    real = gh.tokens.accept

    def slow(reply):
        held.set()
        go.wait(5)
        return real(reply)

    monkeypatch.setattr(gh.tokens, "accept", slow)
    gh.fake.approve()
    gh.login.start()
    assert held.wait(5)
    try:
        assert gh.login.start()["state"] == "finishing"
        assert len(gh.fake.calls("/web/login/device/code")) == 1
    finally:
        go.set()
    assert wait_end(gh.login)["state"] == "done"


def test_when_the_name_cannot_be_fetched_the_tokens_are_kept(gh):
    gh.fake.set(fail="500", fail_on="api")
    gh.fake.approve()
    gh.login.start()
    assert wait_end(gh.login)["state"] == "done"
    assert gh.tokens.logged_in() and gh.tokens.login() == ""
    gh.fake.set(fail="none")
    assert gh.tokens.ensure_user() == "octocat" and gh.tokens.user_id() == 583231


def test_the_login_thread_never_prints_a_traceback(gh, monkeypatch, capfd, caplog):
    def broken(reply):
        raise RuntimeError("ghu_secret_in_an_exception_text")

    hooked = []
    monkeypatch.setattr(threading, "excepthook", hooked.append)
    monkeypatch.setattr(gh.tokens, "accept", broken)
    gh.fake.approve()
    gh.login.start()
    assert wait_end(gh.login) == {"state": "failed", "message": M.LOGIN_CONFIG, "code": "login_config"}
    out, err = capfd.readouterr()
    assert "Traceback" not in err and "ghu_secret" not in err + out + caplog.text
    assert "RuntimeError" in caplog.text and "login_crash" in caplog.text
    assert hooked == []  # the thread's own catch-all handled it; threading.excepthook never ran


def test_secrets_from_a_whole_login_never_reach_logs_or_stderr(gh, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    gh.fake.script(["pending", "slow_down", "ok"])
    gh.login.start()
    assert wait_end(gh.login)["state"] == "done"
    gh.fake.expire_tokens()
    gh.client.get("/user", auth="require")  # a 401 and a refresh
    out, err = capfd.readouterr()
    text = caplog.text + out + err
    for secret in gh.fake.issued_secrets():
        assert secret not in text
    assert len(gh.fake.issued_secrets()) == 5  # the device code and two token pairs


# --- refreshing ---------------------------------------------------------------------------------------------------------

def test_a_token_is_refreshed_five_minutes_before_it_expires(gh):
    logged_in(gh, access_ttl=200)  # under the 300-second margin
    gh.fake.set(access_ttl=28800)  # the refreshed token lasts the usual 8 hours
    first = gh.tokens._tokens.access
    token = gh.tokens.access()
    assert token is not None and token != first and len(refreshes(gh.fake)) == 1
    assert gh.tokens.access() == token and len(refreshes(gh.fake)) == 1


def test_the_new_pair_is_saved_before_it_is_used(gh, memory_keyring):
    logged_in(gh, access_ttl=200)
    gh.fake.set(access_ttl=28800)
    token = gh.tokens.access()
    (blob,) = memory_keyring.entries().values()
    assert token.reveal() in blob and gh.tokens.login() == "octocat"  # the name stays with the new pair
    old_refresh = gh.fake.issued_tokens()[1]
    assert old_refresh not in blob


def test_two_threads_cause_one_refresh(gh):
    logged_in(gh, access_ttl=200)
    gh.fake.set(access_ttl=28800, delay=0.2)  # the refresh takes a while
    got = []
    threads = [threading.Thread(target=lambda: got.append(gh.tokens.access())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(got) == 8 and len({t.reveal() for t in got}) == 1
    assert len(refreshes(gh.fake)) == 1


def test_status_never_waits_for_a_refresh(gh):
    logged_in(gh, access_ttl=200)
    gh.fake.hold("web")
    worker = threading.Thread(target=gh.tokens.access, daemon=True)
    worker.start()
    assert gh.fake.wait_for_calls("/web/login/oauth/access_token")
    started = time.monotonic()
    assert gh.tokens.logged_in() and gh.tokens.login() == "octocat" and gh.tokens.note() == ""
    assert time.monotonic() - started < 0.2
    gh.fake.release()
    worker.join(5)


def test_refresh_soon_makes_a_minute_old_token_count_as_expiring(gh, monkeypatch):
    now = [time.time()]
    tokens = TokenManager(gh.store, lambda: gh.client, clock=lambda: now[0], sleep=lambda s: None)
    gh.client.tokens = tokens
    tokens.accept(gh.fake.issue())
    monkeypatch.setenv("CODEVISION_GITHUB_REFRESH_SOON", "1")
    first = tokens.access()
    assert len(refreshes(gh.fake)) == 0  # a fresh token is used as it is
    now[0] += 61
    second = tokens.access()
    assert second != first and len(refreshes(gh.fake)) == 1
    monkeypatch.delenv("CODEVISION_GITHUB_REFRESH_SOON")
    now[0] += 61
    assert tokens.access() == second and len(refreshes(gh.fake)) == 1


def test_a_401_refreshes_once_and_the_request_is_retried_once(gh):
    logged_in(gh)
    gh.fake.expire_tokens()  # as if 8 hours had passed
    assert gh.client.get("/user", auth="require").data["login"] == "octocat"
    assert len(refreshes(gh.fake)) == 1
    first, second = gh.fake.calls("/api/user")
    assert first["headers"]["authorization"] != second["headers"]["authorization"]


def test_a_refused_refresh_rereads_the_store_then_logs_out(gh, memory_keyring, caplog):
    logged_in(gh)
    gh.fake.expire_tokens()
    gh.fake.set(refresh_error="bad_refresh_token")
    reads = []
    real_load = gh.store.load
    gh.store.load = lambda: (reads.append(1), real_load())[1]
    with pytest.raises(GitHubError) as caught:
        gh.client.get("/user", auth="require")
    assert (caught.value.code, caught.value.status, caught.value.message) == ("login_needed", 401, M.LOGIN_EXPIRED)
    assert len(reads) >= 2  # once after the 401, once after the refusal
    assert memory_keyring.entries() == {} and not gh.tokens.logged_in()
    assert gh.tokens.note() == M.LOGIN_EXPIRED and gh.tokens.login_needed_message() == M.LOGIN_EXPIRED
    assert "refresh_rejected (bad_refresh_token)" in caplog.text


def test_a_refusal_adopts_a_pair_another_codevision_just_saved(gh, memory_keyring):
    logged_in(gh, access_ttl=200)
    newer = gh.fake.issue()  # another CodeVision process refreshed meanwhile

    def other_process_saves(seconds):
        other = TokenManager(TokenStore(fake=True))
        other.accept(newer)

    tokens = TokenManager(gh.store, lambda: gh.client, sleep=other_process_saves)
    gh.client.tokens = tokens
    gh.fake.set(refresh_error="bad_refresh_token", refresh_error_count=1)
    token = tokens.access()
    assert token == Secret(newer["access_token"]) and tokens.logged_in()
    assert newer["refresh_token"] in memory_keyring.entries()[(SERVICE, USERNAME)]


@pytest.mark.parametrize("error", ["unsupported_grant_type", "incorrect_client_credentials", "something_new"])
def test_every_refusal_is_a_normal_logout(gh, memory_keyring, error):
    logged_in(gh, access_ttl=200)
    gh.fake.set(refresh_error=error)
    assert gh.tokens.access() is None
    assert memory_keyring.entries() == {} and gh.tokens.note() == M.LOGIN_EXPIRED
    with pytest.raises(GitHubError) as caught:
        gh.client.get("/user", auth="require")
    assert caught.value.code == "login_needed" and caught.value.message == M.LOGIN_EXPIRED


@pytest.mark.parametrize("fail, code", [("drop", "network"), ("500", "server_error"), ("slow", "timeout")])
def test_github_unreachable_during_a_refresh_keeps_the_login(gh, memory_keyring, monkeypatch, fail, code):
    logged_in(gh, access_ttl=200)
    blob = dict(memory_keyring.entries())
    gh.fake.set(fail=fail, fail_on="web", slow=3)
    monkeypatch.setattr("codevision.github.auth.OAUTH_TIMEOUT", 0.3)
    with pytest.raises(GitHubError) as caught:
        gh.tokens.access()
    assert caught.value.code == code
    assert gh.tokens.logged_in() and memory_keyring.entries() == blob


def test_a_stored_login_past_its_refresh_expiry_is_too_old(gh, memory_keyring):
    old = Tokens(Secret("ghu_" + "o" * 36), time.time() - 10, Secret("ghr_" + "o" * 76), time.time() - 5,
                 "octocat", 583231, CLIENT_ID, time.time() - 16_000_000)
    memory_keyring.set_password(SERVICE, USERNAME, old.to_json())
    tokens = TokenManager(TokenStore(fake=True))
    assert not tokens.logged_in() and tokens.note() == M.LOGIN_TOO_OLD
    assert tokens.login_needed_message() == M.LOGIN_TOO_OLD and memory_keyring.entries() == {}


def test_forget_clears_memory_and_the_store(gh, memory_keyring):
    logged_in(gh)
    assert gh.tokens.forget() is True
    assert not gh.tokens.logged_in() and gh.tokens.login() == "" and memory_keyring.entries() == {}
    assert gh.tokens.note() == ""  # a logout the user asked for needs no note


@pytest.mark.parametrize("reply", [
    {"access_token": "ghu_" + "a" * 36, "token_type": "mac"},
    {"access_token": "short", "token_type": "bearer"},
    {"access_token": "ghu_" + "a" * 36 + " x", "token_type": "bearer"},
    {"token_type": "bearer"},
    {"access_token": "ghu_" + "a" * 36, "token_type": "bearer", "refresh_token": 5},
])
def test_odd_token_replies_are_refused(gh, reply):
    with pytest.raises(GitHubError) as caught:
        gh.tokens.accept(reply)
    assert caught.value.code == "login_config" and not gh.tokens.logged_in()


def test_tokens_are_opaque_strings_of_any_shape(gh):
    stateless = "ghu_123_" + "eyJ" + "x" * 500  # a future, longer format
    tokens = gh.tokens.accept({"access_token": stateless, "token_type": "Bearer", "expires_in": 28800})
    assert tokens.access == Secret(stateless) and tokens.refresh is None and tokens.refresh_expires_at is None
