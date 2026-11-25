"""github/repos.py: what the user typed, repo and branch lists and head commits, against tests/fake_github.py.

- parse_repo_input: owner/repo, any github.com page of a repo (/tree/ and /blob/ give branch hints), .git,
  SSH forms; refuses other hosts, userinfo, bad names and too much text; never touches the network;
- installations, then their repos: several installations, dedup, newest first, caps, suspended ones skipped;
- repo info with GitHub's case, a renamed repo, other accounts' repos (other_owner), SSO and app permissions;
- branches: the default first, names git does not allow dropped; head_sha with If-None-Match, 409, 422.
"""
import time

import pytest

from codevision.github import messages as M
from codevision.github.auth import TokenManager
from codevision.github.base import GitHubError, Hosts
from codevision.github.client import GitHubClient
from codevision.github.repos import (TOO_BIG_KB, RepoRef, head_sha, installation_notes, list_branches,
                                     list_installations, list_repos, parse_repo_input, pick_branch, repo_info,
                                     whoami)
from codevision.github.store import TokenStore
from fake_github import fake_sha


def client_for(fake, login=True):
    tokens = TokenManager(TokenStore(fake=True), sleep=lambda s: None)
    client = GitHubClient(Hosts.for_fake(fake.url), tokens)
    tokens.bind(client)
    if login:
        tokens.accept(fake.issue())
        tokens.set_user("octocat", 583231)
    return client


def iso(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


# --- what the user typed -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, owner, repo, hint", [
    ("octocat/Hello-World", "octocat", "Hello-World", ""),
    ('  "octocat/Hello-World"  ', "octocat", "Hello-World", ""),
    ("<https://github.com/octocat/Hello-World>", "octocat", "Hello-World", ""),
    ("github.com/octocat/Hello-World", "octocat", "Hello-World", ""),
    ("www.github.com/octocat/Hello-World/", "octocat", "Hello-World", ""),
    ("https://github.com/octocat/Hello-World", "octocat", "Hello-World", ""),
    ("https://github.com/octocat/Hello-World.git", "octocat", "Hello-World", ""),
    ("https://github.com/octocat/Hello-World/", "octocat", "Hello-World", ""),
    ("http://www.github.com/octocat/Hello-World", "octocat", "Hello-World", ""),
    ("HTTPS://GitHub.COM/octocat/Hello-World", "octocat", "Hello-World", ""),
    ("https://github.com:443/octocat/Hello-World", "octocat", "Hello-World", ""),
    ("https://github.com/octocat/Hello-World/tree/feature/a/src", "octocat", "Hello-World", "feature/a/src"),
    ("https://github.com/octocat/Hello-World/blob/main/README.md", "octocat", "Hello-World", "main/README.md"),
    ("https://github.com/octocat/Hello-World/tree/feat%2Fx", "octocat", "Hello-World", "feat/x"),
    ("https://github.com/octocat/Hello-World/issues/3", "octocat", "Hello-World", ""),
    ("https://github.com/octocat/Hello-World/commit/" + "a" * 40, "octocat", "Hello-World", ""),
    ("https://github.com/octocat/Hello-World/pulls?q=is%3Aopen#top", "octocat", "Hello-World", ""),
    ("https://github.com/octocat/Hello-World?tab=readme-ov-file#readme", "octocat", "Hello-World", ""),
    ("octocat/Hello-World/tree/dev", "octocat", "Hello-World", "dev"),
    ("git@github.com:octocat/Hello-World.git", "octocat", "Hello-World", ""),
    ("git@github.com:octocat/Hello-World", "octocat", "Hello-World", ""),
    ("ssh://git@github.com/octocat/Hello-World.git", "octocat", "Hello-World", ""),
    ("ssh://git@ssh.github.com:443/octocat/Hello-World.git", "octocat", "Hello-World", ""),
    ("git://github.com/octocat/Hello-World.git", "octocat", "Hello-World", ""),
    ("octo_cat-emu/repo.name_1", "octo_cat-emu", "repo.name_1", ""),
])
def test_accepted_repo_addresses(text, owner, repo, hint):
    assert parse_repo_input(text) == RepoRef(owner, repo, hint)


@pytest.mark.parametrize("text, message", [
    ("", M.EMPTY_INPUT), ("   ", M.EMPTY_INPUT), ('""', M.EMPTY_INPUT),
    ("octocat", M.BAD_INPUT), ("octocat/", M.BAD_INPUT), ("/Hello-World", M.BAD_INPUT),
    ("octo cat/x", M.BAD_INPUT), ("octocat/Hello World", M.BAD_INPUT), ("octocat//Hello", M.BAD_INPUT),
    ("https://github.com/octocat", M.BAD_INPUT), ("https://github.com/settings/apps", M.BAD_INPUT),
    ("https://github.com/orgs/acme/repositories", M.BAD_INPUT),
    ("https://user:pass@github.com/octocat/Hello-World", M.BAD_INPUT),
    ("https://token@github.com/octocat/Hello-World", M.BAD_INPUT),
    ("ssh://root@github.com/octocat/Hello-World.git", M.BAD_INPUT),
    ("-octocat/x", M.BAD_INPUT), ("octocat/..", M.BAD_INPUT), ("octocat/re<po", M.BAD_INPUT),
    ("o" * 40 + "/x", M.BAD_INPUT), ("octocat/" + "r" * 101, M.BAD_INPUT), ("a" * 2001, M.BAD_INPUT),
    ("octocat/x\x00", M.BAD_INPUT), (None, M.BAD_INPUT),
])
def test_refused_inputs(text, message):
    with pytest.raises(GitHubError) as caught:
        parse_repo_input(text)
    assert (caught.value.code, caught.value.status, caught.value.message) == ("bad_input", 400, message)


def test_the_input_sentences_say_account_and_repo_names_in_plain_words():
    """Visible text says "계정 이름/저장소 이름", never owner/repo (lead decision M11)."""
    for text in (M.EMPTY_INPUT, M.BAD_INPUT):
        assert "계정 이름/저장소 이름" in text and "owner" not in text and "/repo" not in text


@pytest.mark.parametrize("text", ["https://gitlab.com/o/r", "gitlab.com/o/r", "git@gitlab.com:o/r.git",
                                  "https://github.com.evil.example/o/r", "ftp://github.com/o/r",
                                  "https://raw.githubusercontent.com/o/r/main/x", "https://github.com:8443/o/r",
                                  "https://gist.github.com/o/abc", "ssh://git@bitbucket.org/o/r.git"])
def test_other_hosts_are_not_github(text):
    with pytest.raises(GitHubError) as caught:
        parse_repo_input(text)
    assert (caught.value.code, caught.value.message) == ("not_github", M.NOT_GITHUB)


@pytest.mark.parametrize("hint, names, expected", [
    ("feature/a/src/app.py", ["main", "feature", "feature/a"], ("feature/a", "")),
    ("feature/src", ["main", "feature", "feature/a"], ("feature", "")),
    ("main", ["main"], ("main", "")),
    ("", ["main", "dev"], ("main", "")),
    ("gone/x", ["main"], ("main", M.branch_default("main"))),
    ("a" * 40, ["main"], ("main", M.branch_default("main"))),
])
def test_pick_branch_takes_the_longest_real_branch(hint, names, expected):
    assert pick_branch(hint, names, "main") == expected


# --- what a login can see ---------------------------------------------------------------------------------------------

def test_repos_from_every_installation_newest_first(fake_github):
    fake_github.add_repo("octocat", "old", {"a": "x"}, pushed_at=iso(30))
    fake_github.add_repo("octocat", "new", {"a": "x"}, pushed_at=iso(1), private=True)
    fake_github.add_repo("octocat", "same-day-b", {"a": "x"}, pushed_at="2026-11-20T00:00:00Z")
    fake_github.add_repo("octocat", "same-day-a", {"a": "x"}, pushed_at="2026-11-20T00:00:00Z")
    fake_github.add_org("acme")
    fake_github.add_installation("acme", members=["octocat"])
    fake_github.add_repo("acme", "tool", {"a": "x"}, pushed_at=iso(2))
    fake_github.add_repo("someone", "theirs", {"a": "x"})  # no installation: never listed
    repos, installed, truncated = list_repos(client_for(fake_github))
    assert installed and not truncated
    assert [r["full_name"] for r in repos] == ["octocat/new", "acme/tool", "octocat/same-day-a",
                                               "octocat/same-day-b", "octocat/old"]
    assert repos[0] == {"owner": "octocat", "repo": "new", "full_name": "octocat/new", "private": True,
                        "default_branch": "main", "pushed_at": repos[0]["pushed_at"], "archived": False,
                        "size_kb": 1, "too_big": False}


def test_a_repo_in_two_installations_is_listed_once(fake_github):
    fake_github.add_org("acme")
    fake_github.add_installation("acme", members=["octocat"])
    fake_github.add_installation("acme", members=["octocat"], selection="selected", repos=["acme/tool"])
    fake_github.add_repo("acme", "tool", {"a": "x"})
    repos, _, _ = list_repos(client_for(fake_github))
    assert [r["full_name"] for r in repos] == ["acme/tool"]


def test_many_repos_are_paginated_and_capped(fake_github):
    for i in range(130):
        fake_github.add_repo("octocat", f"r{i:03d}", {"a": "x"}, pushed_at=iso(i))
    client = client_for(fake_github)
    repos, _, truncated = list_repos(client)
    assert len(repos) == 130 and not truncated and repos[0]["repo"] == "r000"
    repos, _, truncated = list_repos(client, max_repos=50)
    assert len(repos) == 50 and truncated


def test_size_and_archived_flags(fake_github):
    fake_github.add_repo("octocat", "huge", {"a": "x"}, size_kb=TOO_BIG_KB + 1)
    fake_github.add_repo("octocat", "edge", {"a": "x"}, size_kb=TOO_BIG_KB)
    fake_github.add_repo("octocat", "old", {"a": "x"}, archived=True)
    repos = {r["repo"]: r for r in list_repos(client_for(fake_github))[0]}
    assert repos["huge"]["too_big"] and not repos["edge"]["too_big"] and TOO_BIG_KB == 2_000_000
    assert repos["old"]["archived"] and not repos["huge"]["archived"]


def test_not_installed_means_an_empty_list(fake_github):
    fake_github.set_installed(False)
    fake_github.add_repo("octocat", "a", {"a": "x"})
    assert list_repos(client_for(fake_github)) == ([], False, False)


def test_a_suspended_installation_is_skipped_with_a_note(fake_github):
    fake_github.add_org("paused")
    fake_github.add_installation("paused", members=["octocat"], suspended=True)
    fake_github.add_repo("paused", "app", {"a": "x"})
    client = client_for(fake_github)
    installations = list_installations(client)
    assert [(i.account, i.account_type, i.suspended) for i in installations] == [
        ("octocat", "User", False), ("paused", "Organization", True)]
    repos, installed, _ = list_repos(client, installations=installations)
    assert repos == [] and installed
    assert installation_notes(installations) == [M.install_suspended("paused")]
    assert not [c for c in fake_github.calls("/api") if f"/{installations[1].id}/" in c["path"]]


def test_listing_needs_a_login(fake_github):
    with pytest.raises(GitHubError) as caught:
        list_repos(client_for(fake_github, login=False))
    assert caught.value.code == "login_needed"


def test_whoami(fake_github):
    assert whoami(client_for(fake_github)) == ("octocat", 583231)


# --- one repo ----------------------------------------------------------------------------------------------------------

def test_repo_info_gives_githubs_case(fake_github):
    fake_github.add_repo("Octocat", "Hello-World", {"a": "x"}, default_branch="master", size_kb=7)
    info = repo_info(client_for(fake_github, login=False), "octocat", "hello-world", "prefer")
    assert (info.owner, info.repo, info.private, info.default_branch, info.size_kb, info.archived) == (
        "Octocat", "Hello-World", False, "master", 7, False)
    assert info.full_name == "Octocat/Hello-World" and not info.too_big


def test_a_renamed_repo_is_followed(fake_github):
    fake_github.add_repo("octocat", "new-name", {"a": "x"}, renamed_from="octocat/old-name")
    assert repo_info(client_for(fake_github), "octocat", "old-name", "prefer").repo == "new-name"


def test_a_private_repo_without_a_login_is_not_found(fake_github):
    fake_github.add_repo("octocat", "diary", {"a": "x"}, private=True)
    with pytest.raises(GitHubError) as caught:
        repo_info(client_for(fake_github, login=False), "octocat", "diary", "prefer")
    assert (caught.value.code, caught.value.message) == ("not_found", M.NOT_FOUND_ANON)
    assert repo_info(client_for(fake_github), "octocat", "diary", "prefer").private


def test_another_accounts_private_repo_says_whose_it_is(fake_github):
    fake_github.add_org("other-org")
    fake_github.add_repo("other-org", "tool", {"a": "x"}, private=True)
    with pytest.raises(GitHubError) as caught:
        repo_info(client_for(fake_github), "other-org", "tool", "prefer")
    assert caught.value.code == "other_owner" and caught.value.message == M.other_owner("other-org")
    assert "other-org" in caught.value.message and M.ORG_HINT in caught.value.message
    assert caught.value.reply()["install_url"].endswith("/installations/new") and caught.value.status == 404


def test_a_missing_repo_of_the_login_itself_is_plain_not_found(fake_github):
    with pytest.raises(GitHubError) as caught:
        repo_info(client_for(fake_github), "octocat", "nope", "prefer", accounts=["octocat"])
    assert (caught.value.code, caught.value.message) == ("not_found", M.NOT_FOUND_USER)


def test_a_public_repo_of_an_sso_org_opens_without_the_login(fake_github):
    fake_github.add_org("acme")
    fake_github.add_repo("acme", "open", {"a": "x"}, sso=True)
    fake_github.add_repo("acme", "closed", {"a": "x"}, sso=True, private=True)
    client = client_for(fake_github)
    assert repo_info(client, "acme", "open", "prefer").repo == "open"
    with pytest.raises(GitHubError) as caught:
        repo_info(client, "acme", "closed", "prefer")
    assert (caught.value.code, caught.value.message) == ("sso_required", M.SSO_REQUIRED)


# --- branches and head commits -----------------------------------------------------------------------------------------

def test_branches_default_first_then_by_name(fake_github):
    fake_github.add_repo("octocat", "r", {"a": "x"}, branches={"zeta": None, "Alpha": None, "main": None, "beta": None},
                         default_branch="main")
    branches, truncated = list_branches(client_for(fake_github), "octocat", "r", "prefer", default="main")
    assert [b["name"] for b in branches] == ["main", "Alpha", "beta", "zeta"] and not truncated
    assert branches[0]["sha"] == fake_github.head("octocat", "r", "main")


def test_many_branches_are_paginated_and_capped(fake_github):
    fake_github.add_repo("octocat", "r", {"a": "x"}, branches={f"b{i:03d}": None for i in range(230)})
    client = client_for(fake_github)
    branches, truncated = list_branches(client, "octocat", "r", "prefer")
    assert len(branches) == 230 and not truncated
    branches, truncated = list_branches(client, "octocat", "r", "prefer", max_branches=100)
    assert len(branches) == 100 and truncated


def test_names_git_does_not_allow_are_dropped_and_hostile_ones_kept(fake_github):
    fake_github.add_repo("octocat", "r", {"a": "x"}, branches={"main": None, '"><a href=//x>': None,
                                                               "<img/src/onerror=alert(1)>": None, "a..b": None})
    names = [b["name"] for b in list_branches(client_for(fake_github), "octocat", "r", "prefer", default="main")[0]]
    assert names == ["main", "<img/src/onerror=alert(1)>"]


def test_head_sha_and_the_free_304(fake_github):
    fake_github.add_repo("octocat", "r", {"a": "x"}, branches={"main": None, "<img/src/onerror=alert(1)>": None})
    client = client_for(fake_github)
    sha = head_sha(client, "octocat", "r", "main", "prefer")
    assert sha == fake_github.head("octocat", "r", "main")
    assert head_sha(client, "octocat", "r", "main", "prefer", known=sha) == sha
    last = fake_github.calls("/api/repos/octocat/r/commits")[-1]
    assert last["headers"]["if-none-match"] == f'"{sha}"'
    assert last["headers"]["accept"] == "application/vnd.github.sha"
    odd = head_sha(client, "octocat", "r", "<img/src/onerror=alert(1)>", "prefer")
    assert odd == fake_github.head("octocat", "r", "<img/src/onerror=alert(1)>")


def test_a_new_commit_is_seen_despite_the_known_sha(fake_github):
    fake_github.add_repo("octocat", "r", {"a": "x"})
    client = client_for(fake_github)
    old = head_sha(client, "octocat", "r", "main", "prefer")
    new = fake_github.push("octocat", "r", "main", {"a": "y"})
    assert head_sha(client, "octocat", "r", "main", "prefer", known=old) == new != old


def test_head_sha_errors(fake_github):
    fake_github.add_repo("octocat", "empty", empty=True)
    fake_github.add_repo("octocat", "r", {"a": "x"})
    client = client_for(fake_github, login=False)
    with pytest.raises(GitHubError) as caught:
        head_sha(client, "octocat", "empty", "main", "prefer")
    assert (caught.value.code, caught.value.status, caught.value.message) == ("empty_repo", 422, M.EMPTY_REPO)
    with pytest.raises(GitHubError) as caught:
        head_sha(client, "octocat", "r", "gone", "prefer")
    assert (caught.value.code, caught.value.status) == ("branch_not_found", 404)
    assert caught.value.message == M.branch_not_found("gone") == "브랜치를 찾지 못했어요: gone"
    assert fake_github.calls("/api/repos/octocat/r/branches")  # the branch list was asked before giving up
    with pytest.raises(GitHubError) as caught:
        head_sha(client, "octocat", "nope", "main", "prefer")
    assert caught.value.code == "not_found"


def test_a_branch_the_ref_form_cannot_reach_is_found_in_the_list(fake_github, monkeypatch):
    fake_github.add_repo("octocat", "r", {"a": "x"}, branches={"main": None, "odd": None})
    client = client_for(fake_github)
    real_get = client.get

    def picky(path, **kwargs):
        if "/commits/heads/odd" in path:
            raise GitHubError("branch_not_found", M.BRANCH_MISSING)
        return real_get(path, **kwargs)

    monkeypatch.setattr(client, "get", picky)
    assert head_sha(client, "octocat", "r", "odd", "prefer") == fake_github.head("octocat", "r", "odd")


def test_an_invalid_branch_never_reaches_github(fake_github):
    with pytest.raises(GitHubError) as caught:
        head_sha(client_for(fake_github), "octocat", "r", "a b", "prefer")
    assert caught.value.code == "branch_not_found" and fake_github.calls("/api") == []


def test_a_head_reply_that_is_not_a_sha_is_bad_reply(fake_github, monkeypatch):
    fake_github.add_repo("octocat", "r", {"a": "x"})
    client = client_for(fake_github)
    monkeypatch.setattr(client, "get", lambda path, **kw: type("R", (), {"status": 200, "data": "<html>"})())
    with pytest.raises(GitHubError) as caught:
        head_sha(client, "octocat", "r", "main", "prefer")
    assert caught.value.code == "bad_reply"
    assert fake_sha("x") != fake_sha("y")
