"""GitHub: sign in with the GitHub App device flow and open GitHub repositories as local snapshots.

Rules:
- read-only: CodeVision never changes anything on GitHub; the app asks only for Contents and Metadata (read);
- server-side only: only the local Python server talks to GitHub, the page talks to 127.0.0.1 only;
- no client secret anywhere: the device flow and refreshes run without one (the Client ID is public);
- tokens live only in the OS credential store (Windows Credential Manager) and in memory as Secret objects:
  never in files, logs, URLs, exception texts or the page.

- base.py: constants, GitHubError, Secret, Hosts, validators, project ids
- messages.py: every Korean text the GitHub side sends
- client.py: HTTPS to github.com, api.github.com and codeload.github.com (urllib, no automatic redirects)
- store.py: the token store (Credential Manager through win32cred, memory fallback)
- auth.py: the device-flow login (DeviceLogin) and the token lifetime (TokenManager)
- repos.py: what the user typed, repo and branch lists, a branch's head commit
- extract.py, snapshot.py, service.py: the tarball extractor, the snapshot cache and GitHubService

Tests point CODEVISION_GITHUB_FAKE at tests/fake_github.py (loopback http only); CODEVISION_KEYRING=memory
keeps the login in memory.
"""
from __future__ import annotations

from . import messages
from .auth import DeviceLogin, TokenManager
from .base import (API_VERSION, APP_SLUG, AUTH_SETTINGS_URL, CLIENT_ID, FAKE_ENV, INSTALL_URL, KEYRING_ENV,
                   REFRESH_SOON_ENV, USER_AGENT, VERIFY_URL, GitHubError, Hosts, Secret, fake_base, hosts_from_env,
                   parse_project, project_id, repo_dir, valid_branch, valid_owner, valid_repo, valid_sha,
                   valid_token)
from .client import Download, GitHubClient, Reply
from .repos import (Installation, RepoInfo, RepoRef, head_sha, installation_notes, list_branches,
                    list_installations, list_repos, parse_repo_input, pick_branch, repo_info, whoami)
from .store import MemoryBackend, Tokens, TokenStore, use_backend

__all__ = [
    "messages",
    "API_VERSION", "APP_SLUG", "AUTH_SETTINGS_URL", "CLIENT_ID", "FAKE_ENV", "INSTALL_URL", "KEYRING_ENV",
    "REFRESH_SOON_ENV", "USER_AGENT", "VERIFY_URL",
    "GitHubError", "Hosts", "Secret", "fake_base", "hosts_from_env", "parse_project", "project_id", "repo_dir",
    "valid_branch", "valid_owner", "valid_repo", "valid_sha", "valid_token",
    "Download", "GitHubClient", "Reply",
    "Installation", "RepoInfo", "RepoRef", "head_sha", "installation_notes", "list_branches",
    "list_installations", "list_repos", "parse_repo_input", "pick_branch", "repo_info", "whoami",
    "MemoryBackend", "Tokens", "TokenStore", "use_backend",
    "DeviceLogin", "TokenManager",
    "GitHubService", "Snapshot", "SnapshotStore",
]

# GitHubService (service.py), Snapshot and SnapshotStore (snapshot.py) load on first use, so importing this
# package (the test guards do, for every test) stays light and never depends on the download side.
_LAZY = {"GitHubService": "service", "Snapshot": "snapshot", "SnapshotStore": "snapshot"}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
