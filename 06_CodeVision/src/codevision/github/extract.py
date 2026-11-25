"""Our own streaming extractor for a GitHub tarball: someone else's archive, checked entry by entry.

tarfile.extract and extractall are never called. The venv's Python 3.14.3 lacks the 2026 tar
filter fixes, and the 'data' filter misses the Windows traps (device names, NTFS streams through
':', trailing dots and spaces, case and 8.3 aliases that silently overwrite each other).
extract_stream() reads the archive once, front to back, and writes only what it checked itself:

- The gzip layer is ours (zlib checks the gzip trailer, so a cut download is noticed). tarfile
  only reads the plain tar stream ('r|'), never seeking, with stream=True on 3.13+.
- The top folder ("owner-repo-sha7/") is stripped. Entries under any other top folder are skipped.
- Every path component must pass windows_unsafe(). Path lengths are checked against the FINAL
  snapshot path (final_root), not the staging folder: when long paths are off, a file may take at
  most 259 and a folder at most 247 UTF-16 units.
- Only regular files and folders are written: never links, hard links, devices, FIFOs or sparse
  files. Folder entries become folders even when empty (git archive writes a submodule as one).
- A file is created with open(path, "xb"), so it never replaces another one. A name that differs
  from an earlier one only by case is a collision, and the folders of such names merge, as in a
  Windows clone.
- The scanner's ignore rules (scan.py) apply while extracting, so a committed node_modules or .venv
  is neither written nor counted: IGNORED_DIRS and *.egg-info, the PACKAGE_NAMES rule (a build/ or
  dist/ folder that holds an __init__.py is source code), files named .git, and the root .gitignore
  once it has arrived. An ignored folder itself is still created, empty, so graph.hidden lists it as
  it would for a clone.
- What is written for a file, decided the way scan.py decides: a Git LFS pointer becomes an empty
  file; a binary by extension (scan.BINARY_EXTS) an empty placeholder; a binary found by its NUL
  bytes a one-byte placeholder holding a NUL, so the scanner still calls it binary. None of a binary
  file's content is ever written. A text file is cut after its first 1,000,001 bytes, so the scanner
  lists it as over 1 MB, the same as for a clone.
- Caps: compressed bytes, written bytes, files written (after the ignored folders), tar entries,
  unpacked bytes, a stall timeout and a ceiling, plus cancel and progress.

The package rule needs to know whether build/__init__.py exists before build/'s other files are
written. git archive lists every folder's entries depth first, in git's sorted tree order, so the
answer is known as soon as an entry that sorts after "__init__.py" (or one outside the folder)
arrives. Until then the folder's entries are held in memory, within their own small cap.

Failures that stop the job raise an ExtractError subclass; per-file problems are counted in
Result.skipped (and a few names kept in Result.examples) for the Korean notes in snapshot.py.
Exception messages are fixed English texts: never a name, a path or a URL.
"""
from __future__ import annotations

import errno
import http.client
import os
import sys
import tarfile
import time
import unicodedata
import zlib
from dataclasses import dataclass, field

from .. import scan

MiB = 1024 * 1024
GiB = 1024 * MiB

TEXT_CAP = 1_000_001   # bytes written of a text file: the scanner's max_bytes (1,000,000) plus one
HEAD_BYTES = 8192      # the scanner and read_text look for NUL bytes in this many bytes
LFS_MAX = 1024         # a Git LFS pointer file is smaller than this
LFS_SIGNATURES = (b"version https://git-lfs.github.com/spec/", b"version https://hawser.github.com/spec/")
LFS_OID = b"\noid sha256:"
FILE_PATH_MAX = 259    # UTF-16 units of a file path when long paths are off (MAX_PATH less the NUL)
DIR_PATH_MAX = 247     # ... of a folder path (CreateDirectory leaves room for an 8.3 name)
LONG_PATH_MAX = 1000   # either, with long paths on: other tools still choke on longer paths
COMPONENT_MAX = 255    # UTF-16 units of one name (NTFS)
EXAMPLES = 20          # names kept per kind of skip, for the notes and snapshot.json
CHUNK = 64 * 1024      # bytes read from the response at a time
GZIP_MAGIC = b"\x1f\x8b"
PROGRESS_SECONDS = 0.25
GITMODULES_LIMIT = 64 * 1024
GITIGNORE_RULES = 200  # the root .gitignore is applied while extracting only up to this many rules
INIT_NAME = "__init__.py"
INIT_KEY = INIT_NAME.encode()

BLOCKED_WINERRORS = frozenset({5, 32, 225, 226})  # access denied, in use, virus found, virus removed
DISK_FULL_WINERRORS = frozenset({39, 112})        # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_DISK_FULL_ERRNOS = frozenset(e for e in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if e is not None)

# Result.skipped counts, and the kinds that keep a few names in Result.examples
SKIP_KEYS = ("lfs", "binary", "capped", "submodules", "bad_name", "too_long", "collision", "blocked",
             "links", "other_top", "ignored")
EXAMPLE_KEYS = ("bad_name", "too_long", "collision", "blocked")


# --- errors that stop the job (the service maps them to GitHubError codes) ----------------------------------

class ExtractError(Exception):
    """The extraction stopped. The staging folder holds a partial tree and must be thrown away."""


class ArchiveTooBig(ExtractError):
    """what: 'compressed' (the download), 'written' (bytes put on disk) or 'unpacked' (the tar stream)."""

    def __init__(self, what: str, limit: int):
        super().__init__(f"the archive is larger than the {what} limit")
        self.what, self.limit = what, limit


class TooManyFiles(ExtractError):
    """what: 'files' (files written, after the ignored folders) or 'entries' (tar entries read)."""

    def __init__(self, what: str, limit: int):
        super().__init__(f"the archive has more {what} than the limit")
        self.what, self.limit = what, limit


class Cancelled(ExtractError):
    def __init__(self):
        super().__init__("the download was cancelled")


class DownloadStalled(ExtractError):
    """what: 'stall' (no bytes for stall_seconds: a read timed out) or 'ceiling' (max_seconds in total)."""

    def __init__(self, what: str, seconds: float):
        super().__init__(f"the download stopped ({what})")
        self.what, self.seconds = what, seconds


class BadArchive(ExtractError):
    """reason:
    - 'truncated': the stream ended or the connection broke before the archive did (an interrupted download);
    - 'empty': nothing arrived at all;
    - 'not_gzip': the reply is not a gzip stream;
    - 'corrupt': a broken gzip or tar stream (bad checksum, a broken header, data after the end);
    - 'commit_mismatch': the archive's pax comment is not the expected commit.
    detail: the type name of the error behind it, for the log (never its message)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"bad archive ({reason})")
        self.reason, self.detail = reason, detail


class DiskFull(ExtractError):
    def __init__(self):
        super().__init__("the disk is full")


def is_disk_full(exc: BaseException) -> bool:
    """ENOSPC (or EDQUOT), or WinError 112/39."""
    if not isinstance(exc, OSError):
        return False
    return getattr(exc, "winerror", None) in DISK_FULL_WINERRORS or exc.errno in _DISK_FULL_ERRNOS


# --- Windows names and path lengths --------------------------------------------------------------------------

_BAD_CHARS = frozenset('<>:"/\\|?*') | frozenset(map(chr, range(32)))
_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                           *(f"COM{c}" for c in "0123456789¹²³"),
                           *(f"LPT{c}" for c in "0123456789¹²³")})


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, as Windows counts path lengths (a character above U+FFFF is two)."""
    if text.isascii():
        return len(text)
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def windows_unsafe(component: str) -> str | None:
    """Why one path component cannot be created safely on Windows, or None when it can.

    ntpath.isreserved's rules, written out here for Python 3.10+ (and never by calling it: in 3.14.3
    isreserved('a:b') is False, because 'a:' reads as a drive), plus COM0/LPT0, the 255-unit name
    limit and a UTF-8 check. Reasons: 'empty' ('', '.', '..'), 'encoding' (a lone surrogate),
    'trailing' (a dot or space at the end), 'char' (< > : " / \\ | ? * or a control character),
    'device' (CON, NUL.txt, 'nul .txt', COM1, LPT¹, CONIN$ ...), 'long' (over 255 UTF-16 units).
    """
    if component in ("", ".", ".."):
        return "empty"
    if not component.isascii():
        try:
            component.encode("utf-8")
        except UnicodeEncodeError:
            return "encoding"
    if component[-1] in ". ":
        return "trailing"
    if not _BAD_CHARS.isdisjoint(component):
        return "char"
    if component.partition(".")[0].rstrip(" ").upper() in _DEVICE_NAMES:
        return "device"
    if utf16_len(component) > COMPONENT_MAX:
        return "long"
    return None


def long_paths_enabled() -> bool:
    """HKLM SYSTEM/CurrentControlSet/Control/FileSystem LongPathsEnabled is 1. Always True off Windows."""
    if os.name != "nt":
        return True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except (OSError, ImportError):
        return False
    return value == 1


def path_limits(long_paths: bool | None = None) -> tuple[int, int]:
    """(file, folder) path limits in UTF-16 units: (259, 247) when long paths are off, else (1000, 1000)."""
    if long_paths is None:
        long_paths = long_paths_enabled()
    return (LONG_PATH_MAX, LONG_PATH_MAX) if long_paths else (FILE_PATH_MAX, DIR_PATH_MAX)


def shown_name(rel: str, limit: int = 80) -> str:
    """A repo-relative name as it may be stored as JSON and shown: control and format characters and lone
    surrogates become U+FFFD, and a long name keeps its end."""
    if not (rel.isascii() and rel.isprintable()):
        rel = "".join("�" if (0xD800 <= ord(ch) <= 0xDFFF or unicodedata.category(ch) in ("Cc", "Cf"))
                      else ch for ch in rel)
    return rel if len(rel) <= limit else "…" + rel[-(limit - 1):]


# --- limits and result ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Limits:
    max_compressed: int = 300 * MiB    # bytes downloaded
    max_written: int = 500 * MiB       # bytes put on disk
    max_files: int = 50_000            # files written, after the ignored folders are skipped
    max_entries: int = 1_000_000       # tar entries read, skipped ones included (a guard against odd archives)
    max_unpacked: int = 8 * GiB        # tar bytes after gunzip (a guard against gzip bombs)
    text_cap: int = TEXT_CAP           # bytes written of one text file
    max_held: int = 5_000              # entries held while a build/, dist/ ... folder may still be a package
    max_held_bytes: int = 32 * MiB     # ... and their bytes
    stall_seconds: float = 60.0        # the caller opens the response with this socket timeout
    max_seconds: float = 30 * 60.0     # the ceiling for the whole download
    long_paths: bool | None = None     # None: ask long_paths_enabled()


def _counts() -> dict[str, int]:
    return dict.fromkeys(SKIP_KEYS, 0)


def _example_lists() -> dict[str, list[str]]:
    return {key: [] for key in EXAMPLE_KEYS}


@dataclass
class Result:
    commit: str | None = None   # the archive's pax 'comment' (git archive's commit id), None when missing
    top: str | None = None      # the top folder that was stripped
    files: int = 0              # files written, placeholders included
    folders: int = 0            # folders created
    written: int = 0            # bytes written
    read: int = 0               # compressed bytes read
    unpacked: int = 0           # tar bytes after gunzip
    entries: int = 0            # tar entries seen
    skipped: dict[str, int] = field(default_factory=_counts)               # see SKIP_KEYS
    examples: dict[str, list[str]] = field(default_factory=_example_lists)  # first EXAMPLES names per kind

    def to_dict(self) -> dict:
        """The fields snapshot.json keeps."""
        return {"commit": self.commit, "files": self.files, "folders": self.folders, "written": self.written,
                "read": self.read, "unpacked": self.unpacked, "entries": self.entries,
                "skipped": dict(self.skipped), "examples": {k: list(v) for k, v in self.examples.items()}}


# --- reading the response ----------------------------------------------------------------------------------

class _Source:
    """The compressed response with the caps, the cancel Event and progress; hands tarfile the tar stream.

    It gunzips itself (zlib with a gzip header and trailer), so a stream that ends early is noticed:
    tarfile ends silently at a missing header.
    """

    def __init__(self, raw, limits: Limits, cancel, progress, clock):
        self._raw, self._limits, self._cancel, self._progress, self._clock = raw, limits, cancel, progress, clock
        self._started = clock()
        self._last_progress = self._started
        self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._magic_checked = False
        self._input = b""     # compressed bytes zlib has not taken yet
        self.read_bytes = 0   # compressed
        self.unpacked = 0
        self.files = 0        # kept up to date by the extractor, for progress

    def check(self) -> None:
        """Cancel and the ceiling: before every read and every tar entry."""
        if self._cancel is not None and self._cancel.is_set():
            raise Cancelled()
        if self._clock() - self._started > self._limits.max_seconds:
            raise DownloadStalled("ceiling", self._limits.max_seconds)

    def tick(self, force: bool = False) -> None:
        if self._progress is None:
            return
        now = self._clock()
        if force or now - self._last_progress >= PROGRESS_SECONDS:
            self._last_progress = now
            self._progress(self.read_bytes, self.files)

    def _pull(self) -> bytes:
        self.check()
        try:
            chunk = self._raw.read(CHUNK)
        except Exception as exc:
            if self._cancel is not None and self._cancel.is_set():  # the response was closed or aborted to cancel
                raise Cancelled() from None
            if isinstance(exc, TimeoutError):  # the socket timeout: no bytes for that long
                raise DownloadStalled("stall", self._limits.stall_seconds) from None
            if isinstance(exc, (OSError, http.client.HTTPException, ValueError, AttributeError)):
                raise BadArchive("truncated", type(exc).__name__) from None
            raise  # the client's own error (GitHubError 'interrupted' or 'timeout') is already worded
        if not chunk:
            if self._cancel is not None and self._cancel.is_set():
                raise Cancelled()
            raise BadArchive("truncated" if self.read_bytes else "empty")
        self.read_bytes += len(chunk)
        if self.read_bytes > self._limits.max_compressed:
            raise ArchiveTooBig("compressed", self._limits.max_compressed)
        self.tick()
        return bytes(chunk)

    def read(self, size: int = -1) -> bytes:
        """Up to `size` bytes of the tar stream; b'' only after the gzip trailer was checked."""
        if size == 0:
            return b""
        want = size if size and size > 0 else CHUNK
        if not self._magic_checked:
            while len(self._input) < 2:
                self._input += self._pull()
            if self._input[:2] != GZIP_MAGIC:  # an HTML error page, a plain tar, ...
                raise BadArchive("not_gzip")
            self._magic_checked = True
        while not self._z.eof:
            try:
                data = self._z.decompress(self._input, want)
            except zlib.error:
                raise BadArchive("corrupt", "zlib.error") from None
            self._input = self._z.unconsumed_tail
            if data:
                self.unpacked += len(data)
                if self.unpacked > self._limits.max_unpacked:
                    raise ArchiveTooBig("unpacked", self._limits.max_unpacked)
                return data
            if self._input:  # input left but nothing came out: never happens with zlib, but never loop on it
                raise BadArchive("corrupt", "zlib")
            if not self._z.eof:
                self._input = self._pull()
        return b""

    def drain(self) -> bool:
        """Read to the end of the gzip stream, so its trailer (CRC and size) is checked.
        True when everything after the tar's end was NUL padding."""
        clean = True
        while data := self.read(CHUNK):
            if data.strip(b"\0"):
                clean = False
        return clean


class _Entry(tarfile.TarInfo):
    """tarfile treats a missing, cut or broken header as the end of the archive. This notes the one clean
    end, the zero block, so anything else can be refused."""

    @classmethod
    def fromtarfile(cls, tf):
        try:
            return super().fromtarfile(tf)
        except tarfile.EOFHeaderError:
            tf._cv_clean_end = True
            raise


# --- what is written for one file ------------------------------------------------------------------------------

@dataclass
class _Payload:
    how: str            # 'text' | 'capped' | 'lfs' | 'binary'
    head: bytes         # the first bytes to write (all of them when rest is None)
    rest: object        # the tar member's file object for the remaining bytes, or None
    size: int           # bytes written in total


def _ext(name: str) -> str:
    dot = name.rfind(".")  # exactly as scan._classify
    return name[dot:].lower() if dot > 0 else ""


def _text_kind(name: str, ext: str) -> bool:
    """scan._classify reads no bytes of these: text by extension, a leading dot, or a known name."""
    return ext in scan.TEXT_EXTS or name.startswith(".") or name.lower() in scan.TEXT_NAMES


def _read_exact(f, n: int) -> bytes:
    parts, got = [], 0
    while got < n:
        data = f.read(n - got)
        if not data:
            break
        parts.append(data)
        got += len(data)
    return b"".join(parts)


def _is_lfs(data: bytes) -> bool:
    return data.startswith(LFS_SIGNATURES) and LFS_OID in data


def _read_payload(f, name: str, size: int, cap: int) -> _Payload:
    """Decide what to write the way the scanner will classify it, reading as little as needed."""
    small = None
    if size < LFS_MAX:
        small = _read_exact(f, size)
        if _is_lfs(small):
            return _Payload("lfs", b"", None, 0)
    ext = _ext(name)
    if ext in scan.BINARY_EXTS:  # the scanner decides by extension before reading
        return _Payload("binary", b"", None, 0)
    head = small if small is not None else _read_exact(f, min(size, HEAD_BYTES))
    # A NUL byte at the start: binary to the scanner (unknown kinds) or to read_text (text kinds), unless a
    # text kind is over the scanner's size limit, which it lists as over 1 MB without reading.
    if b"\0" in head[:HEAD_BYTES] and (not _text_kind(name, ext) or size < cap):
        return _Payload("binary", b"\0", None, 1)
    n = min(size, cap)
    head = head[:n]
    return _Payload("capped" if size > cap else "text", head, f if len(head) < n else None, n)


def _materialize(p: _Payload) -> _Payload:
    if p.rest is None:
        return p
    data = p.head + _read_exact(p.rest, p.size - len(p.head))
    return _Payload(p.how, data, None, len(data))


def _create(path: str):
    """Create a new file; never replaces one (case, 8.3 and dot/space aliases raise FileExistsError)."""
    return open(path, "xb")


def _write(path: str, p: _Payload) -> None:
    with _create(path) as out:
        if p.head:
            out.write(p.head)
        left = p.size - len(p.head)
        while left > 0:
            chunk = p.rest.read(min(CHUNK, left))
            if not chunk:
                raise BadArchive("truncated")
            out.write(chunk)
            left -= len(chunk)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _fold(name: str) -> str:
    """Case-insensitive key, close to how NTFS compares names: one-to-one upper case per character
    (str.upper alone would make 'ß' and 'SS' equal, which NTFS does not)."""
    if name.isascii():
        return name.upper()
    return "".join(up if len(up := ch.upper()) == 1 else ch for ch in name)


def _git_key(name: str, tree: bool) -> bytes:
    """How git sorts a tree entry: its name's bytes, with '/' after a folder's name."""
    try:
        raw = name.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raw = name.encode("utf-8", "surrogatepass")
    return raw + b"/" if tree else raw


def _ignored_name(name: str) -> bool:
    return name in scan.IGNORED_DIRS or name.endswith(scan.IGNORED_DIR_SUFFIXES)


def _first_unsafe(parts: list[str], start: int) -> str:
    """'bad_name' or 'too_long' for the first unsafe component from parts[start] on, '' when there is none
    before an ignored folder name (whatever is below that is never created)."""
    for part in parts[start:]:
        if _ignored_name(part):
            return ""
        why = windows_unsafe(part)
        if why:
            return "too_long" if why == "long" else "bad_name"
    return ""


def _is_init(name: str) -> bool:
    return os.path.normcase(name) == INIT_NAME  # scan._is_package: case-insensitive where the disk is


def _submodule_paths(text: str) -> list[str]:
    """The 'path = ...' values of .gitmodules, as repo-relative POSIX paths."""
    out = []
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip().lower() != "path":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        else:
            for mark in (" #", "\t#", " ;", "\t;"):
                value = value.split(mark, 1)[0]
        value = value.strip().strip("/")
        while value.startswith("./"):
            value = value[2:]
        if value and value not in out:
            out.append(value)
    return out


# --- the extractor ------------------------------------------------------------------------------------------

# Folder states. NORMAL: created, walked into. PENDING: an ignored name that may be a Python package
# (scan.PACKAGE_NAMES), created, its entries held until its __init__.py arrives or cannot come any more.
# HIDDEN: an ignored folder, created empty when possible, its entries skipped. GITIGNORED: matched by the
# root .gitignore, not created. BROKEN: could not be created; files under it are counted under its reason.
NORMAL, PENDING, HIDDEN, GITIGNORED, BROKEN = "normal", "pending", "hidden", "gitignored", "broken"


class _Dir:
    __slots__ = ("rel", "key", "state", "reason", "made", "held", "held_bytes")

    def __init__(self, rel: str, key: tuple, state: str, reason: str = "", made: bool = False):
        self.rel = rel          # repo-relative POSIX path, in the spelling that was created first
        self.key = key          # folded components
        self.state = state
        self.reason = reason
        self.made = made        # the folder exists on disk
        self.held: list[_Member] = []
        self.held_bytes = 0


class _Member:
    __slots__ = ("parts", "fparts", "kind", "size", "tf", "info", "payload")

    def __init__(self, parts: list[str], kind: str, size: int, tf, info):
        self.parts = parts                      # components under the top folder
        self.fparts = [_fold(p) for p in parts]
        self.kind = kind                        # 'dir' | 'file' | 'link'
        self.size = size
        self.tf, self.info = tf, info           # a live entry: read on demand
        self.payload: _Payload | None = None    # a held file: its bytes


class _Extractor:
    def __init__(self, dest: str, final_root: str, limits: Limits, source: _Source, result: Result):
        self.dest, self.limits, self.src, self.res = dest, limits, source, result
        self.base = utf16_len(final_root.rstrip("\\/")) + 1  # the final root and one separator
        self.file_max, self.dir_max = path_limits(limits.long_paths)
        self.dirs: dict[tuple, _Dir] = {}
        self.files: set[tuple] = set()
        self.pending: list[_Dir] = []   # never nested: entries under a pending folder are held unread
        self.held = 0
        self.held_bytes = 0
        self.rules: list = []           # the root .gitignore, from the moment it was written
        self.gitmodules: list[str] = []

    # -- entries --

    def entry(self, info: tarfile.TarInfo, tf: tarfile.TarFile, parts: list[str]) -> None:
        if info.isdir():
            kind = "dir"
        elif info.isreg() and not info.issparse():
            kind = "file"
        else:
            kind = "link"  # symlinks, hard links, devices, FIFOs, sparse and unknown types: never written
        self.handle(_Member(parts, kind, info.size, tf, info))

    def handle(self, m: _Member) -> None:
        if self.pending:
            self._order(m)
        where, d = self._place(m)
        if where == "skip":
            if not d:  # under an ignored folder: neither written nor counted, like the scanner hides it
                self.res.skipped["ignored"] += 1
            elif m.kind == "file":  # under a folder that could not be created: counted under its reason
                self._skip(d, "/".join(m.parts))
        elif where == "hold":
            if m.kind == "file" and len(m.parts) == len(d.key) + 1 and _is_init(m.parts[-1]):
                self._init_arrived(d, m)
            else:
                self._hold(d, m)
        elif m.kind == "dir":
            key = tuple(m.fparts)
            if key not in self.dirs:
                self._new_dir(m.parts[-1], d, key)
        elif m.kind == "file":
            self._file(m, d)
        else:
            self.res.skipped["links"] += 1

    def _place(self, m: _Member):
        """Walk the folders above the entry: ('go', parent or None), ('hold', the pending folder),
        or ('skip', reason): '' for an ignored folder, else the reason a broken folder gives its files."""
        parent = None
        checked = False
        for i in range(1, len(m.parts)):
            key = tuple(m.fparts[:i])
            d = self.dirs.get(key)
            if d is None:
                if not checked:  # no folder is made for a path that cannot be created anyway ("a/../x")
                    why = _first_unsafe(m.parts, i - 1)
                    if why:
                        return "skip", why
                    checked = True
                d = self._new_dir(m.parts[i - 1], parent, key)
            if d.state == NORMAL:
                parent = d
            elif d.state == PENDING:
                return "hold", d
            elif d.state == BROKEN:
                return "skip", d.reason
            else:
                return "skip", ""
        return "go", parent

    def _new_dir(self, name: str, parent: _Dir | None, key: tuple) -> _Dir:
        rel = f"{parent.rel}/{name}" if parent else name
        if _ignored_name(name):
            # scan.py hides it (unless it is a package), but lists its name: keep the folder, empty
            made = windows_unsafe(name) is None and key not in self.files and self._mkdir(rel) is None
            d = _Dir(rel, key, PENDING if made and name in scan.PACKAGE_NAMES else HIDDEN, made=made)
            if d.state == PENDING:
                self.pending.append(d)
        elif self.rules and scan._ignored(self.rules, rel, name, True):
            d = _Dir(rel, key, GITIGNORED)
        else:
            why = windows_unsafe(name)
            if why:
                reason = "too_long" if why == "long" else "bad_name"
            elif key in self.files:
                reason = "collision"
            else:
                reason = self._mkdir(rel)
            d = _Dir(rel, key, BROKEN, reason) if reason else _Dir(rel, key, NORMAL, made=True)
        self.dirs[key] = d
        return d

    def _mkdir(self, rel: str) -> str | None:
        if self.base + utf16_len(rel) > self.dir_max:
            return "too_long"
        try:
            os.mkdir(os.path.join(self.dest, rel.replace("/", os.sep)))
        except FileExistsError:  # an alias of something already there (an 8.3 name)
            return "collision"
        except OSError as exc:
            return self._os_reason(exc)
        self.res.folders += 1
        return None

    def _file(self, m: _Member, parent: _Dir | None) -> bool:
        """Write one file. False when it was skipped (counted) or ignored."""
        name = m.parts[-1]
        rel = f"{parent.rel}/{name}" if parent else name
        if name == ".git" or (self.rules and scan._ignored(self.rules, rel, name, False)):
            self.res.skipped["ignored"] += 1
            return False
        why = windows_unsafe(name)
        if why:
            return self._skip("too_long" if why == "long" else "bad_name", rel)
        key = tuple(m.fparts)
        if key in self.files or (key in self.dirs and self.dirs[key].made):
            return self._skip("collision", rel)
        if self.base + utf16_len(rel) > self.file_max:
            return self._skip("too_long", rel)
        if self.res.files >= self.limits.max_files:
            raise TooManyFiles("files", self.limits.max_files)
        p = m.payload or self._payload(m)
        if self.res.written + p.size > self.limits.max_written:
            raise ArchiveTooBig("written", self.limits.max_written)
        special = parent is None and (os.path.normcase(name) == ".gitignore" or name == ".gitmodules")
        if special:
            p = _materialize(p)
        path = os.path.join(self.dest, rel.replace("/", os.sep))
        try:
            _write(path, p)
        except FileExistsError:
            return self._skip("collision", rel)
        except OSError as exc:
            reason = self._os_reason(exc)  # raises DiskFull
            _remove(path)
            return self._skip(reason, rel)
        self.files.add(key)
        self.res.files += 1
        self.res.written += p.size
        self.src.files = self.res.files
        if p.how != "text":
            self.res.skipped[p.how] += 1
        if special and p.how in ("text", "capped"):
            if name == ".gitmodules":
                self.gitmodules = _submodule_paths(p.head[:GITMODULES_LIMIT].decode("utf-8", "replace"))
            else:  # what scan._load_gitignore will read from the disk
                rules = scan.parse_gitignore(p.head[:scan.GITIGNORE_LIMIT].decode("utf-8", errors="replace"))
                # Only a shortcut (the scanner applies them anyway), so a huge rule list is not worth its cost.
                self.rules = rules if len(rules) <= GITIGNORE_RULES else []
        return True

    def _payload(self, m: _Member) -> _Payload:
        return _read_payload(m.tf.extractfile(m.info), m.parts[-1], m.size, self.limits.text_cap)

    # -- the package rule --

    def _order(self, m: _Member) -> None:
        """git archive lists a folder's entries depth first, in git's sorted tree order. Once an entry outside
        a pending folder, or one inside it that sorts after __init__.py, has arrived, its __init__.py
        cannot come any more: the folder is not a package."""
        for d in list(self.pending):
            depth = len(d.key)
            if len(m.fparts) < depth or tuple(m.fparts[:depth]) != d.key:
                self._drop(d)
            elif len(m.parts) > depth:
                tree = len(m.parts) > depth + 1 or m.kind == "dir"
                if _git_key(m.parts[depth], tree) > INIT_KEY:
                    self._drop(d)

    def _init_arrived(self, d: _Dir, m: _Member) -> None:
        self.pending.remove(d)
        d.state = NORMAL
        if self._file(m, d):  # written: the folder is a package, like scan._is_package will find
            self._release(d)
        else:
            d.state = PENDING
            self.pending.append(d)

    def _hold(self, d: _Dir, m: _Member) -> None:
        if m.kind == "file" and m.payload is None:
            m.payload = _materialize(self._payload(m))  # the stream moves on: read it now
        size = len(m.payload.head) if m.payload is not None else 0
        if self.held + 1 > self.limits.max_held or self.held_bytes + size > self.limits.max_held_bytes:
            self._drop(d)  # too much waiting on one __init__.py: treat it as not a package
            self.res.skipped["ignored"] += 1
            return
        m.tf = m.info = None
        d.held.append(m)
        d.held_bytes += size
        self.held += 1
        self.held_bytes += size

    def _release(self, d: _Dir) -> None:
        held, d.held = d.held, []
        self.held -= len(held)
        self.held_bytes -= d.held_bytes
        d.held_bytes = 0
        for m in held:
            self.handle(m)

    def _drop(self, d: _Dir) -> None:
        self.pending.remove(d)
        d.state = HIDDEN
        self.res.skipped["ignored"] += len(d.held)
        self.held -= len(d.held)
        self.held_bytes -= d.held_bytes
        d.held, d.held_bytes = [], 0

    # -- bookkeeping --

    def _skip(self, reason: str, rel: str) -> bool:
        self.res.skipped[reason] += 1
        names = self.res.examples.get(reason)
        if names is not None and len(names) < EXAMPLES:
            names.append(shown_name(rel))
        return False

    @staticmethod
    def _os_reason(exc: OSError) -> str:
        """One file or folder failed: 'blocked' (antivirus, a lock, access denied) or 'bad_name' (anything
        else, such as a name Windows refuses). A full disk stops the job."""
        if is_disk_full(exc):
            raise DiskFull() from None
        winerror = getattr(exc, "winerror", None)
        if winerror in BLOCKED_WINERRORS or (winerror is None and exc.errno in (errno.EACCES, errno.EPERM)):
            return "blocked"
        return "bad_name"

    def finish(self) -> None:
        for d in list(self.pending):  # the archive ended before their __init__.py
            self._drop(d)
        found = set()
        for path in self.gitmodules:
            d = self.dirs.get(tuple(_fold(p) for p in path.split("/")))
            if d is None or d.state != NORMAL or d.key in found:
                continue
            try:
                if not os.listdir(os.path.join(self.dest, d.rel.replace("/", os.sep))):
                    found.add(d.key)
            except OSError:
                pass
        self.res.skipped["submodules"] = len(found)


def _comment(tf: tarfile.TarFile) -> str | None:
    value = tf.pax_headers.get("comment")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:100]


def extract_stream(fileobj, dest, limits: Limits | None = None, *, cancel=None, progress=None,
                   final_root=None, expect_commit: str | None = None, clock=time.monotonic) -> Result:
    """Extract a GitHub .tar.gz stream into dest, a new folder (its parent must exist).

    - fileobj: the response; read(n) only. The caller opens it with a socket timeout of
      limits.stall_seconds, and may close it from another thread after setting cancel.
    - final_root: where dest will live once published; path lengths are checked against it (default dest).
    - cancel: a threading.Event; progress(read_bytes, files) is called at most every 0.25 s and once at
      the end; expect_commit: when the archive names another commit, BadArchive('commit_mismatch') is
      raised at its first entry (a missing comment is accepted: see Result.commit).

    Raises ArchiveTooBig, TooManyFiles, Cancelled, DownloadStalled, BadArchive or DiskFull; an OSError
    when dest cannot be created. An error the response's own read() raises (client.Download's GitHubError
    'interrupted' or 'timeout') passes through unchanged, except that once cancel is set it is Cancelled.
    The caller deletes the staging folder after any of them.
    """
    limits = limits or Limits()
    dest = os.fspath(dest)
    result = Result()
    source = _Source(fileobj, limits, cancel, progress, clock)
    try:
        os.mkdir(dest)
    except OSError as exc:
        if is_disk_full(exc):
            raise DiskFull() from None
        raise
    root = os.fspath(final_root) if final_root is not None else os.path.abspath(dest)
    ex = _Extractor(dest, root, limits, source, result)
    options = {"stream": True} if sys.version_info >= (3, 13) else {}  # 3.13+: TarInfo objects are not kept
    try:
        with tarfile.open(fileobj=source, mode="r|", tarinfo=_Entry, **options) as tf:
            for info in tf:
                source.check()
                result.entries += 1
                if result.entries > limits.max_entries:
                    raise TooManyFiles("entries", limits.max_entries)
                top, sep, rest = info.name.partition("/")
                if result.top is None:
                    if windows_unsafe(top) or not (sep or info.isdir()):  # "../x", "/abs", a file first
                        raise BadArchive("corrupt", "top")
                    result.top = top
                    result.commit = _comment(tf)  # git archive's global pax header, read with the first entry
                    if expect_commit and result.commit and result.commit != expect_commit:
                        raise BadArchive("commit_mismatch")
                if top != result.top:
                    result.skipped["other_top"] += 1
                elif rest:
                    ex.entry(info, tf, rest.split("/"))
            if not getattr(tf, "_cv_clean_end", False):
                raise BadArchive("corrupt", "end")
        if not source.drain():
            raise BadArchive("corrupt", "trailing")
    except (tarfile.TarError, EOFError) as exc:
        raise BadArchive("corrupt", type(exc).__name__) from None
    ex.finish()
    result.read, result.unpacked = source.read_bytes, source.unpacked
    source.tick(force=True)
    return result


__all__ = [
    "ArchiveTooBig", "BadArchive", "Cancelled", "DiskFull", "DownloadStalled", "ExtractError", "TooManyFiles",
    "Limits", "Result", "extract_stream", "windows_unsafe", "long_paths_enabled", "path_limits", "utf16_len",
    "shown_name", "is_disk_full", "SKIP_KEYS", "EXAMPLE_KEYS", "TEXT_CAP", "FILE_PATH_MAX", "DIR_PATH_MAX",
    "LONG_PATH_MAX", "MiB", "GiB",
]
