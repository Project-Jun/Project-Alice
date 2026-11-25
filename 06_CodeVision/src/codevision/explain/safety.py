"""What may go to the AI: secret files stay out, obvious secrets are masked, paths stay inside.

AI traffic never leaves this computer (the Ollama address is loopback only), but an
answer is shown on screen and kept in the cache, so secrets are kept out of prompts too.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PureWindowsPath

MASK = "[가린 값]"  # what the model sees in place of a masked secret

# --- secret files: never sent, refused with code "secret" ----------------------------

_SECRET_NAMES = {
    ".env", ".envrc", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pypirc", ".netrc", "_netrc", ".pgpass",
    ".htpasswd", ".git-credentials", ".dockercfg", ".s3cfg", ".boto", "credentials", "keystore", ".keystore",
    "terraform.tfstate", "terraform.tfstate.backup",
}
_SECRET_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "client_secret")
_SECRET_SUFFIXES = (".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".kdbx", ".p8",
                    ".tfvars", ".tfstate")
_SECRET_STEMS = {"secret", "secrets", "credential", "credentials", "client_secrets", "service_account",
                 "service-account", "serviceaccount"}
_SECRET_FOLDERS = {".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"}


def is_secret_file(rel_path: str) -> bool:
    """True for files that may hold secrets: .env*, private keys, credential files and the like."""
    parts = rel_path.replace("\\", "/").split("/")
    name = parts[-1].lower()
    if any(p.lower() in _SECRET_FOLDERS for p in parts[:-1]):
        return True
    if name in _SECRET_NAMES or name.startswith(_SECRET_PREFIXES) or name.endswith(_SECRET_SUFFIXES):
        return True
    return name.split(".", 1)[0] in _SECRET_STEMS


# --- masking: obvious secrets inside the text that is sent ---------------------------

# "( BLOCK|)" always takes part in the match, so \2 also matches when there is no " BLOCK"
# (a group that did not take part never matches its backreference). A block without its END
# line is masked to the end of the text.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]{0,40})PRIVATE KEY( BLOCK|)-----.*?(?:-----END \1PRIVATE KEY\2-----|\Z)", re.S)

# A token with dot-separated parts starts only where a run of [A-Za-z0-9_-] starts ((?<![\w-]) is \b
# that also refuses a "-" before it), so no run is read again from a later start: with \b alone, a
# crafted file of "-ghs_1_aaaaaaaa" (or "-eyJaaaaaaaa") repeated was read once per start, 41 s for 1 MB.
_TOKENS = re.compile("|".join([
    r"\bgh[pousr]_[A-Za-z0-9]{30,}",                         # GitHub tokens
    r"(?<![\w-])ghs_[0-9]+_[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # stateless installation tokens
    r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bglpat-[A-Za-z0-9_-]{20,}",                           # GitLab
    r"\bsk-[A-Za-z0-9_-]{20,}",                              # OpenAI, Anthropic and similar
    r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}",        # Stripe
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",                        # AWS access key id
    r"\bAIza[0-9A-Za-z_-]{35}",                              # Google API key
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",                      # Slack
    r"\bhf_[A-Za-z0-9]{30,}",                                # Hugging Face
    r"\bnpm_[A-Za-z0-9]{36}",
    r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",         # SendGrid
    r"(?<![\w-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT
    r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+",
    r"https://discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+",
]))
_URL_PASSWORD = re.compile(r"""(\b[A-Za-z][A-Za-z0-9+.-]{1,20}://[^\s:/@"'`<>]{1,64}:)([^\s@/"'`<>]{1,256})(@)""")
# ...?token=... or ...&token=... in a URL (download links such as codeload's carry one)
_URL_TOKEN = re.compile(r"([?&](?i:token)=)([A-Za-z0-9_.~%+/=-]{16,})")
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=-]{16,})")
# "...;Password=hunter2;" in a connection string: no spaces around "=", and the value (4 or more
# characters) ends at ";", a quote or the line end (so "password = input(...)" and "password=get(...)"
# stay as they are). See _mask_connection_secrets.
_CONN_KEY = re.compile(r"(?i)(?<![\w.])(?:AccountKey|SharedAccessKey|Password|Pwd)=")
_CONN_VALUE = re.compile(r"[^;\"'`\s()]*")
_CONN_END = re.compile(r";|[\"'`]|[ \t]*$", re.M)

# name = "literal" (also name: "literal", "name": "literal", name: str = "literal", f(name="literal"),
# obj["name"] = "literal", and comparisons such as name == "literal"). A name only starts where a word
# starts, so a very long word costs one try, not one per letter.
_ASSIGN = re.compile(
    r"""(?<![\w$])([A-Za-z_$][\w$.\-]{0,63})"""
    r"""(["']?\]?\s{0,3}(?::\s{0,3}[A-Za-z_][\w.\[\]|<> ]{0,40}?\s{0,3})?"""
    r"""(?:===|!==|==|!=|=>|:=|\?\?=|\|\|=|=|:)\s{0,3})"""
    r"""([bBrRuUfF]{0,2})(["'`])([^"'`\r\n]{3,300})\4""")
# os.getenv("NAME", "default"), os.environ.get("NAME", "default"), env("NAME", default="..."), setdefault(...):
# a call's first two arguments (a list of names such as ["password", "token"] is not a call)
_DEFAULT = re.compile(
    r"""(\(\s{0,3})(["'])([A-Za-z_][\w.\-]{0,63})\2(\s{0,3},\s{0,3}(?:default\s{0,3}=\s{0,3})?)"""
    r"""([bBrRuUfF]{0,2})(["'`])([^"'`\r\n]{3,300})\6""")
# ... || "literal", ?? "literal", or "literal", else "literal": a fallback, masked when a secret's name comes
# before it on the line (process.env.JWT_SECRET || "dev-secret", os.getenv("DB_PASSWORD") or "hunter2")
_FALLBACK = re.compile(r"""((?:\|\||\?\?|\bor\b|\belse\b)\s{0,3}[bBrRuUfF]{0,2})(["'`])([^"'`\r\n]{3,300})\2""")
_PROSE = re.compile(r"""(["'`])[^"'`\r\n]*\s[^"'`\r\n]*\1""")  # quoted words with a space: text, not a name
_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_SECRETISH = re.compile(r"(?i)pass|secret|token|key|credential|pwd")  # a quick look before _secret_name
_SECRET_NAME_PARTS = ("password", "passwd", "passphrase", "secret", "token", "apikey", "accesskey", "authkey",
                      "privatekey", "credential", "sessionkey", "signingkey", "encryptionkey")
_PASS_END = re.compile(r"(?:^|[_.\-])(?:pass|pwd)$", re.I)  # DB_PASS, smtp-pwd
_PASS_CAMEL = re.compile(r"[a-z0-9](?:Pass|PASS|Pwd|PWD)$")  # userPass, dbPwd (not bypass)
_SECRET_LINE = re.compile(r"(?i)pass|secret|token|api.?key|access.?key|auth.?key|private.?key|credential|"
                          r"session.?key|signing.?key|encryption.?key|pwd")


def _secret_name(name: str) -> bool:
    plain = name.lower().replace("_", "").replace("-", "").replace(".", "").replace("$", "")
    return (plain in ("pwd", "pass") or any(part in plain for part in _SECRET_NAME_PARTS)
            or bool(_PASS_END.search(name) or _PASS_CAMEL.search(name)))


def _mask_assignment(m: re.Match) -> str:
    if not _secret_name(m.group(1)):
        return m.group(0)
    quote = m.group(4)
    return m.group(1) + m.group(2) + m.group(3) + quote + MASK + quote


def _mask_default(m: re.Match) -> str:
    if not _secret_name(m.group(3)):
        return m.group(0)
    quote = m.group(6)
    return m.group(1) + m.group(2) + m.group(3) + m.group(2) + m.group(4) + m.group(5) + quote + MASK + quote


def _mask_connection_secrets(text: str) -> str:
    """The values after AccountKey=, SharedAccessKey=, Password= and Pwd=, each value read once.

    The same as one re.sub with the lookahead (?=;|["'`]|[ \\t]*$) after the value, but that pattern
    read a long value again from every key inside it ("Password=Password=...(" took minutes for 1 MB,
    with the GIL held). A key inside a value ends where that value ends, so it fails the same way."""
    out, last, pos = [], 0, 0
    while (key := _CONN_KEY.search(text, pos)) is not None:
        end = _CONN_VALUE.match(text, key.end()).end()
        if end - key.end() >= 4 and _CONN_END.match(text, end):
            out += (text[last:key.end()], MASK)
            last = end
        pos = max(end, key.end())
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _first_secret_word(line: str) -> int:
    """Where the first name of a secret starts on the line (quoted text with spaces aside), or -1."""
    bare = _PROSE.sub(lambda m: " " * len(m.group(0)), line)
    for word in _WORD.finditer(bare):
        if _SECRETISH.search(word.group()) and _secret_name(word.group()):
            return word.start()
    return -1


def _mask_line(line: str) -> str:
    """The secret values on a line that names a secret: assignments, comparisons, defaults, fallbacks."""
    line = _ASSIGN.sub(_mask_assignment, line)
    line = _DEFAULT.sub(_mask_default, line)
    if _FALLBACK.search(line):
        start = _first_secret_word(line)  # one pass over the line, so a minified file stays quick
        if start >= 0:
            line = _FALLBACK.sub(lambda m: m.group(1) + m.group(2) + MASK + m.group(2) if m.start() > start
                                 else m.group(0), line)
    return line


def mask_secrets(text: str) -> str:
    """Replace private key blocks, well-known token formats, ?token= values and passwords in URLs, and
    secret-looking values ("API_KEY = '...'", "password: '...'", 'os.getenv("SECRET_KEY", "...")',
    'process.env.JWT_SECRET || "..."', 'password == "..."') with MASK. Everything else stays as it is."""
    if not text:
        return text
    text = _PRIVATE_KEY.sub(MASK, text)
    text = _TOKENS.sub(MASK, text)
    text = _URL_TOKEN.sub(lambda m: m.group(1) + MASK, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + MASK + m.group(3), text)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = _mask_connection_secrets(text)
    if _SECRET_LINE.search(text):  # only lines that name a secret are searched for values
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if _SECRET_LINE.search(line):
                lines[i] = _mask_line(line)
        text = "".join(lines)
    return text


# --- node ids to paths: never outside the analyzed folder ----------------------------

class UnsafePath(ValueError):
    """A node id that could name something outside the project folder."""


class TooBig(ValueError):
    """A file larger than the size limit."""


def _parts(rel) -> list[str]:
    """The path parts of a node id. Raises UnsafePath for "..", ".", empty parts, absolute paths,
    drive letters, UNC paths, backslashes and colons (Windows) and NUL."""
    if not isinstance(rel, str) or "\x00" in rel or rel.startswith(("/", "\\")):
        raise UnsafePath(rel)
    if os.name == "nt" and ("\\" in rel or ":" in rel or PureWindowsPath(rel).drive):
        raise UnsafePath(rel)
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or os.path.isabs(rel):
        raise UnsafePath(rel)
    return parts


def _is_link(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)  # a junction


class ProjectFiles:
    """Files and folders of one analyzed project, by node id, never outside it.

    Like the scanner, links and junctions are never followed: a node id whose path goes through
    one is refused, wherever it points. Folder checks are remembered for the life of the object,
    so make one per request or batch.
    """

    def __init__(self, root: str | Path):
        self.base = os.path.realpath(str(root))
        self._checked: dict[str, os.stat_result | None] = {}

    def path(self, rel: str) -> Path:
        """The path of node `rel` (the project folder itself for "" and "."). Raises UnsafePath."""
        if rel in ("", "."):
            return Path(self.base)
        parts = _parts(rel)
        current = self.base
        for i in range(len(parts)):
            current = os.path.join(current, parts[i])
            key = "/".join(parts[:i + 1])
            if key not in self._checked:
                try:
                    st = os.lstat(current)
                except OSError:  # missing or unreadable: nothing to follow, reading fails later
                    st = None
                if st is not None and _is_link(st):
                    raise UnsafePath(rel)
                if st is not None:
                    self._checked[key] = st
        return Path(current)

    def read(self, rel: str, limit: int) -> bytes:
        """The raw bytes of file node `rel`, at most `limit` bytes. Raises UnsafePath, TooBig or OSError."""
        path = self.path(rel)
        checked = self._checked.get(rel)
        if checked is None or not stat.S_ISREG(checked.st_mode):
            raise FileNotFoundError(rel)
        with open(path, "rb") as fh:
            opened = os.fstat(fh.fileno())
            if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino) or not stat.S_ISREG(opened.st_mode):
                raise UnsafePath(rel)  # replaced after the check
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise TooBig(rel)
        return data


def safe_path(root: str | Path, rel: str) -> Path:
    """The path of node `rel` under `root`; raises UnsafePath for anything that could leave it."""
    return ProjectFiles(root).path(rel)


def read_file(root: str | Path, rel: str, limit: int) -> bytes:
    """The raw bytes of file node `rel` under `root`, at most `limit` bytes. Raises UnsafePath, TooBig or OSError."""
    return ProjectFiles(root).read(rel, limit)
