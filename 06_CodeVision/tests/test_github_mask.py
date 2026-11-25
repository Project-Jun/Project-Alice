"""explain/safety.py masking for GitHub's newer token shapes, before repo files go to the local AI.

- stateless installation tokens 'ghs_<app id>_<JWT>' (the default since GitHub's 2026-10-02 changelog);
- the value of a ?token= / &token= query parameter, as codeload download links carry;
- the tokens tests/fake_github.py issues are masked too, and the older rules did not change.
"""
import base64
import json
import time

import pytest

from codevision.explain import safety
from codevision.explain.safety import MASK, mask_secrets


def b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


JWT = ".".join([b64url({"alg": "RS256", "typ": "JWT"}),
                b64url({"iss": "1234567", "iat": 1795560000, "exp": 1795563600, "installation_id": 98765432,
                        "repository_ids": list(range(40))}),
                "c2lnbmF0dXJl" * 30])
STATELESS = f"ghs_1234567_{JWT}"


def test_a_long_stateless_installation_token_is_masked():
    assert len(STATELESS) > 500
    for text in (STATELESS, f'GITHUB_TOKEN={STATELESS}', f'{{"token": "{STATELESS}"}}',
                 f"curl -H 'Authorization: token {STATELESS}' https://api.github.com/user"):
        masked = mask_secrets(text)
        assert STATELESS not in masked and JWT[:40] not in masked and MASK in masked


@pytest.mark.parametrize("url, kept", [
    ("https://codeload.github.com/o/r/legacy.tar.gz/abc?token=AABBCCDDEEFFGGHHIIJJKKLLMM", "?token="),
    ("https://example.com/download?id=7&token=Zm9vYmFyYmF6cXV4cXV1eA%3D%3D", "&token="),
    ("https://example.com/x?TOKEN=abcdefghijklmnop1234", "?TOKEN="),
])
def test_token_query_values_are_masked(url, kept):
    masked = mask_secrets(f'download("{url}")')
    assert url.split("token=" if "token=" in url else "TOKEN=")[1] not in masked
    assert kept + MASK in masked


@pytest.mark.parametrize("text", ["page?token=short", "the token=value pair", "a?tokens=AABBCCDDEEFFGGHHIIJJ",
                                  "url?tokenizer=AABBCCDDEEFFGGHHIIJJ", "ghs_ is the prefix of an installation token",
                                  "ghs_123_short.jwt.parts"])
def test_ordinary_text_stays_as_it_is(text):
    assert mask_secrets(text) == text


def test_the_tokens_the_fake_issues_are_masked(fake_github):
    reply = fake_github.issue()
    text = f"ACCESS = '{reply['access_token']}'\nrefresh: {reply['refresh_token']}\n"
    masked = mask_secrets(text)
    assert reply["access_token"] not in masked and reply["refresh_token"] not in masked


@pytest.mark.parametrize("text, secret", [
    ("token = 'ghp_" + "a" * 36 + "'", "ghp_" + "a" * 36),
    ("x ghu_" + "b" * 36, "ghu_" + "b" * 36),
    ("github_pat_" + "c" * 40, "github_pat_" + "c" * 40),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "eyJhbGciOiJIUzI1NiJ9"),
    ("postgres://app:hunter2secret@localhost/app", "hunter2secret"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
])
def test_the_older_rules_still_mask_as_before(text, secret):
    assert secret not in mask_secrets(text)


def test_the_older_token_rules_are_unchanged_byte_for_byte():
    old = [
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bglpat-[A-Za-z0-9_-]{20,}",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_-]{35}",
        r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
        r"\bhf_[A-Za-z0-9]{30,}",
        r"\bnpm_[A-Za-z0-9]{36}",
        r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
        # the JWT rule's start changed from \b to (?<![\w-]): see test_crafted_files_mask_quickly
        r"(?<![\w-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+",
        r"https://discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+",
    ]
    parts = safety._TOKENS.pattern.split("|(?<![\\w-])ghs_[0-9]+_")
    assert len(parts) == 2
    new_rule = "(?<![\\w-])ghs_[0-9]+_" + parts[1].split("|", 1)[0]
    assert safety._TOKENS.pattern == "|".join(old[:1] + [new_rule] + old[1:])


@pytest.mark.parametrize("unit", [
    "-ghs_1_aaaaaaaa",     # a stateless-token start inside one long run, again and again
    "-eyJaaaaaaaa",        # a JWT start, the same way
    "Password=",           # a connection-string key inside the value of the one before it
    "x;Pwd=" + "a" * 20,
])
def test_crafted_files_mask_quickly(unit):
    # Each of these took 40 s to many minutes for 1 MB before, with the GIL held: the whole server stopped
    text = unit * (1_000_000 // len(unit)) + "("
    started = time.monotonic()
    safety.mask_secrets(text)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("text, expected", [
    ('conn = "Server=x;Database=y;Password=hunter22;"', 'conn = "Server=x;Database=y;Password={};"'),
    ("AccountKey=abcd+efgh==;EndpointSuffix=core", "AccountKey={};EndpointSuffix=core"),
    ("pwd=abcd\nPWD=ab;\nSharedAccessKey=k3y-value 'x'\nSharedAccessKey=k3y-value",
     "pwd={}\nPWD=ab;\nSharedAccessKey=k3y-value 'x'\nSharedAccessKey={}"),
    ("Password=Password=abcd;", "Password={};"),
    ("Password=abcd(x);x.Password=abcd;", "Password=abcd(x);x.Password=abcd;"),
    ('"Pwd=abcd" Password=abc ', '"Pwd={}" Password=abc '),
])
def test_connection_string_secrets_are_masked_as_before(text, expected):
    assert mask_secrets(text) == expected.replace("{}", MASK)
