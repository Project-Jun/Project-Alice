"""All HTTP to GitHub, with urllib.

- HTTPS only, to github.com (the device flow), api.github.com (REST) and codeload.github.com (archives),
  with fixed headers. In fake mode: plain http to tests/fake_github.py on loopback, never through a proxy.
- Per-operation socket timeouts plus total deadlines.
- No automatic redirects. Only one same-host 301/307/308 on the API (a renamed repo) and the tarball 302 to
  codeload are followed, by hand, after checking where they point.
- Authorization goes only to the API, added with add_unredirected_header at the last moment; codeload gets it
  only on the documented one-time retry for a private repo.
- Errors and rate limits become GitHubError, raised "from None" with fixed Korean texts.
- Logs carry only the operation name, HTTP status, our error code and exception type names: never URLs
  (Location, Link, codeload), headers, bodies, codes or tokens.
"""
from __future__ import annotations

import _socket
import functools
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import messages as M
from .base import (API_VERSION, CODELOAD_URL, USER_AGENT, GitHubError, Hosts, Secret, valid_owner, valid_repo,
                   valid_sha)

DEFAULT_TIMEOUT = 15.0       # seconds for each socket operation of an API or OAuth request
FORM_TIMEOUT = 15.0
STALL_TIMEOUT = 60.0         # a download that sends nothing for this long is stopped
DOWNLOAD_CEILING = 30 * 60   # and no download runs longer than this
PAGE_SIZE = 100
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_FORM_BYTES = 64 * 1024
CHUNK = 64 * 1024
JSON_ACCEPT = "application/vnd.github+json"
SHA_ACCEPT = "application/vnd.github.sha"
RATE_CAP = 3600              # a rate-limit block never lasts longer than this
_CODELOAD_HOST = urllib.parse.urlsplit(CODELOAD_URL).hostname
_TCP_NOSYNRETRIES = 9        # Windows: report a refused loopback connection at once instead of after ~2 s

log = logging.getLogger(__name__)


# --- loopback-only HTTP for fake mode (the same rules as explain/ollama.py) ---------------------------

def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(str(address).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class _LoopbackConnection(http.client.HTTPConnection):
    """An HTTP connection that only ever reaches a loopback address, whatever the name resolves to."""

    def connect(self) -> None:
        last: OSError = ConnectionRefusedError("no loopback address")
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else None
        for family, socktype, proto, _, address in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
            if not _is_loopback(address[0]):
                last = ConnectionRefusedError("not a loopback address")
                continue
            sock = socket.socket(family, socktype, proto)
            try:
                if os.name == "nt":
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, _TCP_NOSYNRETRIES, 1)
                    except OSError:
                        pass
                sock.settimeout(timeout)
                sock.connect(address)
                if not _is_loopback(sock.getpeername()[0]):
                    raise ConnectionRefusedError("not a loopback address")
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                sock.close()
                last = exc
                continue
            self.sock = sock
            return
        raise last


class _LoopbackHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_LoopbackConnection, req)


def _tls_context() -> ssl.SSLContext:
    """The default verified context (TLS 1.2+, the Windows root store), with key logging turned off."""
    ctx = ssl.create_default_context()
    try:
        ctx.keylog_filename = None  # SSLKEYLOGFILE never captures our TLS keys
    except (AttributeError, ValueError, OSError):
        pass
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _build_opener(hosts: Hosts) -> urllib.request.OpenerDirector:
    """No redirect handler, and only the scheme the hosts use: https for GitHub, loopback http for the fake."""
    opener = urllib.request.OpenerDirector()
    if hosts.fake:
        handlers = [_LoopbackHandler()]  # no ProxyHandler at all: loopback never goes through a proxy
    else:
        # The environment and Windows proxy settings are kept; with CONNECT the token stays inside TLS.
        handlers = [urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(context=_tls_context())]
    handlers += [urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(),
                 urllib.request.UnknownHandler()]
    for handler in handlers:
        opener.add_handler(handler)
    return opener


# --- replies ----------------------------------------------------------------------------------------------

@dataclass
class Reply:
    """A successful API reply: status (200-299, or 304), data (parsed JSON, text, or None for 304), headers
    (case-insensitive). Never log the headers: they can hold Link URLs."""

    status: int
    data: object
    headers: object


@dataclass
class _Raw:
    status: int
    headers: object
    body: bytes


class Download:
    """A tarball being streamed from codeload. read(n) gives raw gzip bytes and raises GitHubError:
    interrupted when the connection breaks or ends early, timeout when no bytes come for the stall timeout
    or the whole download passes its ceiling. abort() wakes a read blocked in another thread; close() frees
    the connection. The download URL is never kept here."""

    def __init__(self, resp, *, ceiling: float | None = DOWNLOAD_CEILING):
        self._resp = resp
        self.length = _content_length(resp.headers)
        self.read_bytes = 0
        self._deadline = time.monotonic() + ceiling if ceiling else None
        self._aborted = False
        self.closed = False

    @property
    def stream(self) -> Download:
        return self

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        """At most n bytes, after at most one network read (so a short result is normal; b'' is the end).
        read() or read(-1) reads everything that is left."""
        if self._aborted or self.closed:
            raise GitHubError("interrupted", M.INTERRUPTED)
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise GitHubError("timeout", M.TIMEOUT)
        try:
            data = self._resp.read() if n is None or n < 0 else _read1(self._resp, n)
        except Exception as exc:  # the stream broke: never the URL, only the type name
            if self._aborted:
                raise GitHubError("interrupted", M.INTERRUPTED) from None
            code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "interrupted"
            log.info("GitHub download stopped: %s (%s)", type(exc).__name__, code)
            raise GitHubError(code, M.for_code(code)) from None
        if not data and n != 0 and getattr(self._resp, "length", None):
            log.info("GitHub download ended before its promised length (interrupted)")
            raise GitHubError("interrupted", M.INTERRUPTED)  # the connection closed mid-stream
        self.read_bytes += len(data)
        return data

    def abort(self) -> None:
        """Make a read blocked in another thread return at once (it raises interrupted). Never blocks.

        shutdown() wakes the reader on Linux and macOS; on Windows only closing the socket handle does, so the
        handle itself is closed too (urllib has already dropped its own reference; the response holds the last)."""
        self._aborted = True
        sock = getattr(getattr(getattr(self._resp, "fp", None), "raw", None), "_sock", None)
        if isinstance(sock, socket.socket):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                _socket.socket.close(sock)
            except OSError:
                pass

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            _close(self._resp)

    def __enter__(self) -> Download:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _close(resp) -> None:
    try:
        resp.close()
    except Exception:
        pass


def _read1(resp, n: int) -> bytes:
    """Up to n bytes with at most one read from the network: read(n) would wait for all n bytes, so a reply
    trickling in byte by byte would never reach the deadline checks between reads."""
    read1 = getattr(resp, "read1", None)
    return read1(n) if callable(read1) else resp.read(n)


def _content_length(headers) -> int | None:
    value = (headers.get("Content-Length") or "").strip() if headers is not None else ""
    if value.isascii() and value.isdigit() and len(value) <= 15:
        return int(value)
    return None


def _int_header(headers, name: str) -> int | None:
    value = (headers.get(name) or "").strip()
    if value.isascii() and value.isdigit() and len(value) <= 12:
        return int(value)
    return None


def _gh_message(body: bytes) -> str:
    """GitHub's "message" field, lowercased, only for matching (it is never logged or shown)."""
    try:
        data = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return message.lower()[:500] if isinstance(message, str) else ""


def _rate_kind(raw: _Raw, message: str) -> str | None:
    if raw.status not in (403, 429):
        return None
    if (raw.headers.get("x-ratelimit-remaining") or "").strip() == "0":
        return "primary"
    if raw.headers.get("retry-after") is not None:
        return "secondary"
    if "secondary rate limit" in message or "abuse detection" in message:
        return "secondary"
    if "rate limit" in message:
        return "primary"
    return None


_LINK = re.compile(r"<([^>]*)>([^,<]*)")


def next_link(header) -> str | None:
    """The rel="next" URL of a Link header, exactly as given (it is checked before it is followed)."""
    if not header:
        return None
    for m in _LINK.finditer(str(header)):
        for param in m.group(2).split(";"):
            name, _, value = param.strip().partition("=")
            if name.strip().lower() == "rel" and "next" in value.strip().strip('"').lower().split():
                return m.group(1).strip()
    return None


def _q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def _clean_url(url) -> bool:
    return isinstance(url, str) and 0 < len(url) <= 8000 and not any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url)


def _origin(parts: urllib.parse.SplitResult) -> tuple[str, str, int]:
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


# --- the client ------------------------------------------------------------------------------------------------

class GitHubClient:
    """HTTP to GitHub for one server. Thread-safe; it holds no lock across a network call.

    tokens: a TokenManager (auth.py) or None. auth='none' never sends a token; 'prefer' sends one when
    someone is logged in and retries once without it on a 403 that is not a rate limit, or on a 404;
    'require' needs a login (login_needed otherwise). A 401 refreshes the login once and retries once.
    """

    def __init__(self, hosts: Hosts, tokens=None, *, opener: urllib.request.OpenerDirector | None = None):
        self.hosts = hosts
        self.tokens = tokens
        self._opener = opener
        self._lock = threading.Lock()
        self._blocked: dict[str, tuple[float, bool]] = {}  # "anon" | "user" -> (until, secondary)
        self._closed = False
        self._api = urllib.parse.urlsplit(hosts.api) if hosts.api else None

    # --- small helpers ---

    def close(self) -> None:
        """Every later request fails at once with 'closing'. Requests in flight end on their own."""
        self._closed = True

    def logged_in(self) -> bool:
        return self.tokens is not None and self.tokens.logged_in()

    def login(self) -> str:
        return self.tokens.login() if self.tokens is not None else ""

    def _check_on(self) -> None:
        if self.hosts.off:
            raise GitHubError("login_config", M.FAKE_OFF)
        if self._closed:
            raise GitHubError("closing", M.CLOSING)

    def _get_opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:  # built on first use: ProxyHandler() reads the environment and the registry
            with self._lock:
                if self._opener is None:
                    self._opener = _build_opener(self.hosts)
        return self._opener

    def _api_url(self, path: str, params: dict | None = None) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
            raise ValueError("an API path starts with '/' and has no query")
        url = self.hosts.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _api_headers(self, accept: str) -> dict:
        return {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION}

    def same_api(self, url) -> str | None:
        """url when it is on the API host (same scheme, host and port, under the API path, no userinfo)."""
        if self._api is None or not _clean_url(url):
            return None
        try:
            parts = urllib.parse.urlsplit(url)
            origin = _origin(parts)
        except ValueError:
            return None
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None
        if origin != _origin(self._api):
            return None
        prefix = self._api.path.rstrip("/") + "/"
        if not (parts.path + "/").startswith(prefix) and parts.path != self._api.path:
            return None
        return url

    def codeload_target(self, location, base: str) -> str | None:
        """The tarball redirect target when it is codeload: https://codeload.github.com/... (port 443 or none,
        no userinfo), or the fake's /codeload/ address. None for anything else."""
        if not isinstance(location, str) or not location:
            return None
        try:
            url = urllib.parse.urljoin(base, location)
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        if not _clean_url(url) or parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None
        if self.hosts.fake:
            fake = urllib.parse.urlsplit(self.hosts.codeload)
            if _origin(parts) != _origin(fake) or not parts.path.startswith(fake.path.rstrip("/") + "/"):
                return None
            return url
        if parts.scheme.lower() != "https" or (parts.hostname or "").lower() != _CODELOAD_HOST:
            return None
        if port not in (None, 443):
            return None
        return url

    # --- rate limits ---

    def rate_block(self, mode: str) -> int:
        """Seconds left before calls in this mode ('anon' or 'user') may go to GitHub again (0: not blocked)."""
        with self._lock:
            until, _ = self._blocked.get(mode, (0.0, False))
        left = until - time.monotonic()
        return int(left + 0.999) if left > 0 else 0

    def _block(self, mode: str, raw: _Raw, kind: str) -> int:
        if kind == "primary":
            reset = _int_header(raw.headers, "x-ratelimit-reset")
            seconds = (reset - time.time()) if reset else 60.0
            retry = _int_header(raw.headers, "retry-after")
            if retry:
                seconds = max(seconds, retry)
        else:
            seconds = _int_header(raw.headers, "retry-after") or 60
        seconds = int(min(max(seconds, 1), RATE_CAP) + 0.999)
        with self._lock:
            self._blocked[mode] = (time.monotonic() + seconds, kind == "secondary")
        return seconds

    def _rate_error(self, mode: str, logged_in: bool) -> GitHubError | None:
        left = self.rate_block(mode)
        if not left:
            return None
        with self._lock:
            _, secondary = self._blocked.get(mode, (0.0, False))
        return GitHubError("rate_limited", M.rate_limited(left, not logged_in, secondary), retry_after=left)

    # --- one request ---

    def _failure(self, exc, op: str) -> GitHubError:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            code = "timeout"
        elif isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
            code = "tls"
        elif isinstance(exc, GitHubError):
            return exc
        elif isinstance(exc, ValueError) and not isinstance(exc, OSError):
            code = "bad_reply"  # a URL urllib cannot use
        else:
            code = "network"  # refused, reset, DNS, a broken reply line, ...
        log.info("GitHub %s failed: %s (%s)", op, type(exc).__name__, code)
        return GitHubError(code, M.for_code(code))

    def _open(self, url: str, *, op: str, method: str = "GET", data: bytes | None = None, headers: dict,
              token: Secret | None, timeout: float):
        """The response, or the HTTPError for a non-2xx status (both must be closed). Raises GitHubError."""
        self._check_on()
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            if token is not None:
                req.add_unredirected_header("Authorization", "Bearer " + token.reveal())
            return self._get_opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            raise self._failure(reason if isinstance(reason, BaseException) else exc, op) from None
        except GitHubError:
            raise
        except Exception as exc:
            raise self._failure(exc, op) from None

    def _read_body(self, resp, cap: int, deadline: float, op: str, *, strict: bool = True) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() > deadline:
                log.info("GitHub %s took longer than its deadline (timeout)", op)
                raise GitHubError("timeout", M.TIMEOUT)
            try:
                piece = _read1(resp, min(CHUNK, cap + 1 - size))
            except Exception as exc:
                raise self._failure(exc, op) from None
            if not piece:
                if strict and getattr(resp, "length", None):
                    log.info("GitHub %s reply was cut off (network)", op)
                    raise GitHubError("network", M.NETWORK)
                return b"".join(chunks)
            size += len(piece)
            if size > cap:
                if strict:
                    log.warning("GitHub %s reply was too large (bad_reply)", op)
                    raise GitHubError("bad_reply", M.BAD_REPLY)
                chunks.append(piece[:len(piece) - (size - cap)])
                return b"".join(chunks)
            chunks.append(piece)

    def _fetch(self, url: str, *, op: str, method: str = "GET", data: bytes | None = None, headers: dict,
               token: Secret | None = None, timeout: float = DEFAULT_TIMEOUT, cap: int = MAX_JSON_BYTES,
               total: float | None = None) -> _Raw:
        deadline = time.monotonic() + (total if total is not None else max(30.0, 3 * timeout))
        resp = self._open(url, op=op, method=method, data=data, headers=headers, token=token, timeout=timeout)
        try:
            if isinstance(resp, urllib.error.HTTPError):
                try:
                    body = self._read_body(resp, MAX_ERROR_BYTES, deadline, op, strict=False)
                except GitHubError:
                    body = b""
                return _Raw(resp.code, resp.headers, body)
            return _Raw(resp.status, resp.headers, self._read_body(resp, cap, deadline, op))
        finally:
            _close(resp)

    def _api_fetch(self, url: str, *, token: Secret | None, op: str, headers: dict, timeout: float, cap: int,
                   total: float, allow_302: bool = False) -> _Raw:
        """One API request, following at most one same-host 301/307/308 (Authorization is added again)."""
        raw = self._fetch(url, op=op, headers=headers, token=token, timeout=timeout, cap=cap, total=total)
        if raw.status in (301, 307, 308):
            target = None
            location = raw.headers.get("Location")
            if isinstance(location, str) and location:
                try:
                    target = self.same_api(urllib.parse.urljoin(url, location))
                except ValueError:
                    target = None
            if target is None:
                log.warning("GitHub %s redirected somewhere that is not followed (bad_reply)", op)
                raise GitHubError("bad_reply", M.BAD_REPLY, gh_status=raw.status)
            raw = self._fetch(target, op=op, headers=headers, token=token, timeout=timeout, cap=cap, total=total)
        if 300 <= raw.status < 400 and raw.status != 304 and not (allow_302 and raw.status == 302):
            log.warning("GitHub %s answered %s, which is not followed (bad_reply)", op, raw.status)
            raise GitHubError("bad_reply", M.BAD_REPLY, gh_status=raw.status)
        return raw

    def _error_for(self, raw: _Raw, *, op: str, mode: str, logged_in: bool,
                   errors: dict | None = None) -> GitHubError:
        message = _gh_message(raw.body)
        kind = _rate_kind(raw, message)
        if kind:
            seconds = self._block(mode, raw, kind)
            log.info("GitHub %s answered %s (rate_limited, %s)", op, raw.status, kind)
            return GitHubError("rate_limited", M.rate_limited(seconds, not logged_in, kind == "secondary"),
                               retry_after=seconds, gh_status=raw.status)
        status = raw.status
        code = (errors or {}).get(status)
        if code is None:
            if status == 401:
                code = "login_needed"
            elif status == 403:
                if raw.headers.get("X-GitHub-SSO"):
                    code = "sso_required"
                elif "suspended" in message:
                    code = "suspended"
                elif "not accessible by integration" in message:
                    code = "not_allowed"
                else:
                    code = "forbidden"
            elif status == 404:
                code = "not_found"
            elif status == 410 or (status == 400 and "version" in message):
                code = "outdated"
            elif status >= 500:
                code = "server_error"
            else:
                code = "bad_reply"
        level = logging.WARNING if code in ("server_error", "bad_reply", "outdated") else logging.INFO
        log.log(level, "GitHub %s answered %s (%s)", op, status, code)
        extra = {}
        if code in ("not_allowed", "not_installed", "suspended", "other_owner"):
            extra["install_url"] = self.hosts.install_url
        if code == "login_needed" and self.tokens is not None:
            text = self.tokens.login_needed_message()
        else:
            text = M.for_code(code, logged_in)
        return GitHubError(code, text, gh_status=status, **extra)

    # --- logins ---

    def _token_for(self, auth: str) -> Secret | None:
        if auth not in ("none", "prefer", "require"):
            raise ValueError("auth is 'none', 'prefer' or 'require'")
        if auth == "none":
            return None
        if self.tokens is None:
            if auth == "require":
                raise GitHubError("login_needed", M.LOGIN_NEEDED)
            return None
        token = self.tokens.access()  # may refresh first; a network error then is raised as it is
        if token is None and auth == "require":
            raise GitHubError("login_needed", self.tokens.login_needed_message())
        return token

    def _send(self, fetch, token: Secret | None, logged_in: bool) -> _Raw:
        blocked = self._rate_error("user" if token is not None else "anon", logged_in)
        if blocked is not None:
            raise blocked
        return fetch(token=token)

    def _with_login(self, fetch, auth: str, *, op: str, errors: dict | None = None) -> tuple[_Raw, Secret | None, bool]:
        """Send with the right login: refresh once after a 401, and for 'prefer' retry once without the
        token after a 404 or a 403 that is not a rate limit. Returns (raw, token used, logged_in)."""
        logged_in = self.logged_in()
        token = self._token_for(auth)
        raw = self._send(fetch, token, logged_in)
        if raw.status == 401 and token is not None:
            new = self.tokens.refreshed_after_401(token)
            retried = None
            if new is not None:
                retried = self._send(fetch, new, logged_in)
                if retried.status == 401:
                    self.tokens.rejected(new)
                    retried = None
            if retried is not None:
                raw, token = retried, new
            elif auth == "require":
                raise GitHubError("login_needed", self.tokens.login_needed_message(), gh_status=401)
            else:
                logged_in = self.logged_in()
                token = None
                raw = self._send(fetch, None, logged_in)
        if (auth == "prefer" and token is not None and raw.status in (403, 404)
                and _rate_kind(raw, _gh_message(raw.body)) is None):
            first = self._error_for(raw, op=op, mode="user", logged_in=logged_in, errors=errors)
            if self.rate_block("anon"):
                raise first
            anon = self._send(fetch, None, logged_in)
            if 200 <= anon.status < 400:
                return anon, None, logged_in
            if first.code in ("sso_required", "suspended", "not_allowed", "forbidden"):
                raise first
            raise self._error_for(anon, op=op, mode="anon", logged_in=logged_in, errors=errors)
        return raw, token, logged_in

    # --- the public calls ---

    def post_form(self, path: str, fields: dict, timeout: float = FORM_TIMEOUT, *, op: str = "oauth") -> dict:
        """POST a form to github.com (/login/device/code, /login/oauth/access_token) and return the reply object.

        GitHub answers 200 with an 'error' key while a login is pending, so a reply holding 'error' is returned
        as it is, whatever the status. A reply that is not a JSON object or is over 64 KiB is bad_reply."""
        self._check_on()
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("a web path starts with '/'")
        body = urllib.parse.urlencode(fields).encode("ascii")
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                   "User-Agent": USER_AGENT}
        raw = self._fetch(self.hosts.web + path, op=op, method="POST", data=body, headers=headers, timeout=timeout,
                          cap=MAX_FORM_BYTES, total=2 * timeout)
        data = _form_reply(raw)
        if 200 <= raw.status < 300:
            if data is None:
                log.warning("GitHub %s sent a reply that is not a JSON object (bad_reply)", op)
                raise GitHubError("bad_reply", M.BAD_REPLY)
            return data
        if raw.status < 500 and data is not None and isinstance(data.get("error"), str):
            return data
        # Anything else that is not a rate limit or a GitHub outage means the login setup itself is wrong.
        raise self._error_for(raw, op=op, mode="web", logged_in=False,
                              errors={400: "login_config", 401: "login_config", 404: "login_config",
                                      405: "login_config", 422: "login_config"})

    def get(self, path: str, *, auth: str = "none", accept: str = JSON_ACCEPT, params: dict | None = None,
            timeout: float = DEFAULT_TIMEOUT, etag: str | None = None, op: str = "api", parse: str = "json",
            errors: dict | None = None, total: float | None = None) -> Reply:
        """GET an API path. parse: 'json' (capped at 5 MiB), 'text' (64 KiB) or 'bytes'. etag: sent as
        If-None-Match, and a 304 comes back as Reply(304, None, headers). errors: {GitHub status: our code}
        for statuses this endpoint gives a special meaning (409 empty_repo, 422 branch_not_found).
        timeout: each socket operation; total: the whole request and reply (default max(30, 3 x timeout))."""
        return self._get_url(self._api_url(path, params), auth=auth, accept=accept, timeout=timeout, etag=etag,
                             op=op, parse=parse, errors=errors, total=total)

    def _get_url(self, url: str, *, auth: str, accept: str = JSON_ACCEPT, timeout: float = DEFAULT_TIMEOUT,
                 etag: str | None = None, op: str = "api", parse: str = "json", errors: dict | None = None,
                 total: float | None = None) -> Reply:
        self._check_on()
        headers = self._api_headers(accept)
        if etag:
            headers["If-None-Match"] = etag
        cap = MAX_JSON_BYTES if parse == "json" else MAX_TEXT_BYTES
        fetch = functools.partial(self._api_fetch, url, op=op, headers=headers, timeout=timeout, cap=cap,
                                  total=total if total is not None else max(30.0, 3 * timeout))
        raw, token, logged_in = self._with_login(fetch, auth, op=op, errors=errors)
        if raw.status == 304:
            return Reply(304, None, raw.headers)
        if 200 <= raw.status < 300:
            return Reply(raw.status, self._parse(raw.body, parse, op), raw.headers)
        raise self._error_for(raw, op=op, mode="user" if token is not None else "anon", logged_in=logged_in,
                              errors=errors)

    def _parse(self, body: bytes, parse: str, op: str):
        if parse == "bytes":
            return body
        if parse == "text":
            return body.decode("utf-8", "replace")
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, ValueError, RecursionError):
            data = None
        if not isinstance(data, (dict, list)):
            log.warning("GitHub %s sent a reply that is not JSON (bad_reply)", op)
            raise GitHubError("bad_reply", M.BAD_REPLY)
        return data

    def paginate(self, path: str, *, auth: str, params: dict | None = None, item_key: str | None = None,
                 max_items: int = 1000, deadline: float = 30.0, op: str = "list") -> tuple[list, bool]:
        """Every item of a paginated list (per_page=100), following Link rel=next exactly as given after
        checking its scheme, host and port. Returns (items, truncated): truncated when more than max_items
        exist. The whole walk must finish within `deadline` seconds (timeout otherwise)."""
        params = dict(params or {})
        params.setdefault("per_page", PAGE_SIZE)
        url = self._api_url(path, params)
        end = time.monotonic() + deadline
        max_pages = max(1, -(-max_items // PAGE_SIZE))
        items: list = []
        pages = 0
        while True:
            left = end - time.monotonic()
            if left <= 0:
                log.info("GitHub %s list took longer than its deadline (timeout)", op)
                raise GitHubError("timeout", M.TIMEOUT)
            reply = self._get_url(url, auth=auth, op=op, timeout=min(DEFAULT_TIMEOUT, left), total=left)
            data = reply.data
            page = data.get(item_key) if item_key and isinstance(data, dict) else (None if item_key else data)
            if not isinstance(page, list):
                log.warning("GitHub %s list is not a list (bad_reply)", op)
                raise GitHubError("bad_reply", M.BAD_REPLY)
            pages += 1
            room = max_items - len(items)
            items.extend(page[:room])
            nxt = next_link(reply.headers.get("Link"))
            if len(page) > room or (nxt is not None and (len(items) >= max_items or pages >= max_pages)):
                return items, True
            if nxt is None:
                return items, False
            target = self.same_api(nxt)
            if target is None:
                log.warning("GitHub %s list points somewhere else for its next page (bad_reply)", op)
                raise GitHubError("bad_reply", M.BAD_REPLY)
            url = target

    def open_tarball(self, owner: str, repo: str, sha: str, *, auth: str, timeout: float = 30.0,
                     stall: float = STALL_TIMEOUT, ceiling: float | None = DOWNLOAD_CEILING) -> Download:
        """Start downloading the tarball of one commit. GET /repos/{o}/{r}/tarball/{sha} must answer 302 to
        codeload (after at most one same-host 301/307/308); the Location is fetched without Authorization,
        and for a login a 401/403/404 there gets one retry with it, sent to codeload only. The Location is a
        temporary credential for private repos: it is never logged, kept or shown."""
        self._check_on()
        if not (valid_owner(owner) and valid_repo(repo) and valid_sha(sha)):
            raise ValueError("not a valid owner, repo or sha")
        url = self._api_url(f"/repos/{_q(owner)}/{_q(repo)}/tarball/{sha}")
        fetch = functools.partial(self._api_fetch, url, op="tarball", headers=self._api_headers(JSON_ACCEPT),
                                  timeout=timeout, cap=MAX_ERROR_BYTES, total=2 * timeout, allow_302=True)
        raw, token, logged_in = self._with_login(fetch, auth, op="tarball")
        if raw.status != 302:
            if 200 <= raw.status < 400:
                log.warning("GitHub tarball answered %s instead of a redirect (bad_reply)", raw.status)
                raise GitHubError("bad_reply", M.BAD_REPLY, gh_status=raw.status)
            raise self._error_for(raw, op="tarball", mode="user" if token is not None else "anon",
                                  logged_in=logged_in)
        target = self.codeload_target(raw.headers.get("Location"), url)
        if target is None:
            log.warning("GitHub tarball redirect does not point at codeload; not followed (bad_reply)")
            raise GitHubError("bad_reply", M.BAD_REPLY, gh_status=302)
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        resp = self._open(target, op="codeload", headers=headers, token=None, timeout=stall)
        if isinstance(resp, urllib.error.HTTPError) and resp.code in (401, 403, 404) and token is not None:
            _close(resp)
            resp = self._open(target, op="codeload", headers=headers, token=token, timeout=stall)
        if isinstance(resp, urllib.error.HTTPError) or getattr(resp, "status", 0) != 200:
            status = resp.code if isinstance(resp, urllib.error.HTTPError) else getattr(resp, "status", 0)
            _close(resp)
            code = ("not_found" if status == 404 else "server_error" if status >= 500
                    else "forbidden" if status in (401, 403) else "bad_reply")
            log.info("GitHub codeload answered %s (%s)", status, code)
            raise GitHubError(code, M.for_code(code, logged_in), gh_status=status)
        return Download(resp, ceiling=ceiling)


def _form_reply(raw: _Raw) -> dict | None:
    """A reply object from github.com: JSON, or a form-encoded body if Accept was not honoured."""
    try:
        text = raw.body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        data = json.loads(text) if text else None
    except (ValueError, RecursionError):
        data = None
        content_type = (raw.headers.get("Content-Type") or "").lower()
        if "x-www-form-urlencoded" in content_type:
            data = dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
    return data if isinstance(data, dict) else None
