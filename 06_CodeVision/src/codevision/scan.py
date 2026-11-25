"""Folder scanner: walks a project, applies the ignore rules and builds a ProjectIndex.

Rules (docs/architecture.md section 5): default ignored folders, simple patterns
from the root .gitignore, a file count limit (truncated), a size limit per file
(skipped) and binary detection (listed but never analyzed).

The walk is breadth-first and stops as soon as the limit is reached, so pointing
it at a huge folder (a whole drive) only costs the part that is shown.
"""
from __future__ import annotations

import itertools
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .model import ProjectIndex, atomic, join_stars

IGNORED_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".nuxt", ".svelte-kit",
    ".turbo", ".cache", ".parcel-cache", "coverage", ".idea", ".vscode",
})
IGNORED_DIR_SUFFIXES = (".egg-info",)
# Ignored names that are also ordinary Python package names (pip/_internal/operations/build,
# the standard library's venv). A folder with an __init__.py is source code, not output.
PACKAGE_NAMES = frozenset(name for name in IGNORED_DIRS if name.isidentifier())
MAX_DEPTH = 64  # folders below this depth are not walked (deep chains are never real source trees)
MIN_ENTRIES = 20_000  # entries read from one folder at least (more when max_files is larger)
GITIGNORE_LIMIT = 256_000  # bytes of .gitignore that are read
GITIGNORE_RULES = 2_000  # at most this many patterns are used
GITIGNORE_PATTERN_MAX = 4_096  # a longer pattern is skipped: no real path is that long

# .svg is an image but plain text, so it is not in BINARY_EXTS.
IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".avif", ".tif", ".tiff",
    ".heic", ".psd", ".svg",
})
FONT_EXTS = frozenset({".woff", ".woff2", ".ttf", ".otf", ".eot"})
MEDIA_EXTS = frozenset({".mp3", ".wav", ".ogg", ".flac", ".m4a", ".mp4", ".mov", ".avi", ".webm", ".mkv"})
ARCHIVE_EXTS = frozenset({".zip", ".gz", ".tgz", ".tar", ".rar", ".7z", ".bz2", ".xz"})
DOCUMENT_EXTS = frozenset({".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".hwp", ".hwpx"})
DATABASE_EXTS = frozenset({".db", ".sqlite", ".sqlite3"})
COMPILED_EXTS = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar", ".pyc", ".pyo", ".pyd",
    ".o", ".a", ".lib", ".wasm", ".pkl", ".npy", ".npz", ".h5", ".parquet", ".onnx",
    ".pt", ".ckpt", ".lockb",
})
BINARY_EXTS = (
    (IMAGE_EXTS - {".svg"}) | FONT_EXTS | MEDIA_EXTS | ARCHIVE_EXTS | DOCUMENT_EXTS
    | DATABASE_EXTS | COMPILED_EXTS
)
# Text by extension: no need to open the file during the walk to look for NUL bytes.
# (A text file that still holds NUL bytes, such as UTF-16, is caught when it is read.)
TEXT_EXTS = frozenset({
    ".py", ".pyi", ".pyw", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".styl", ".svg",
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".conf", ".xml", ".properties", ".lock", ".sql", ".csv", ".tsv", ".sh", ".bash", ".bat", ".cmd", ".ps1",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c", ".cpp", ".h", ".hpp", ".swift", ".dart", ".lua",
})
TEXT_NAMES = frozenset({
    "license", "licence", "readme", "makefile", "dockerfile", "procfile", "authors", "changelog",
    "contributing", "notice", "copying", "gemfile", "rakefile", "vagrantfile", "jenkinsfile",
})


@dataclass
class ScanResult:
    index: ProjectIndex
    hidden: list[str] = field(default_factory=list)  # default-ignored folder names that existed
    skipped: list[str] = field(default_factory=list)  # text files over max_bytes
    binary: list[str] = field(default_factory=list)  # binary files (by extension or NUL byte)
    truncated: bool = False  # part of the folder was left out: file_limit or crowded
    unreadable: list[str] = field(default_factory=list)  # folders that could not be opened ("" is the root)
    too_deep: bool = False  # some folders were below MAX_DEPTH and not walked
    cut_off: list[str] = field(default_factory=list)  # folders whose contents the file limit left out
    file_limit: bool = False  # stopped at max_files
    crowded: list[str] = field(default_factory=list)  # folders with more entries than max_entries ("" is the root)
    max_entries: int = 0  # entries read from one folder at most


@dataclass
class _Rule:
    regex: re.Pattern
    negate: bool
    dir_only: bool
    anchored: bool  # match the relative path with a "/" after it ("a/b/") instead of the name


# Globs become regular expressions that never backtrack their way into minutes (see
# model.join_stars: one hostile .gitignore in a downloaded repo would otherwise stop the whole
# server). Within a path part, the piece after each inner star is taken at its first place. A "**"
# part is the same one level up: it stands for any number of folders, and the run of parts after
# it is taken at its first place.
# Within a part, "**" is an ordinary star, as in git; a "**" part matches zero or more folders,
# and a "**" at the end (a/**) everything inside. ?, * and [...] never match "/".


def _glob_parts(pattern: str) -> list[list]:
    """The path parts of a glob, each a list of tokens: an int for a run of stars, else a regular
    expression for exactly one character."""
    parts: list[list] = [[]]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "/":
            parts.append([])
        elif c == "*":
            stars = 1
            while i + 1 < n and pattern[i + 1] == "*":
                i += 1
                stars += 1
            parts[-1].append(stars)
        elif c == "?":
            parts[-1].append("[^/]")
        elif c == "[" and pattern.find("]", i + 2) != -1:
            j = pattern.find("]", i + 2)
            body = pattern[i + 1:j].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            parts[-1].append(f"(?!/)[{body}]")
            i = j
        else:
            parts[-1].append(re.escape(c))
        i += 1
    return parts


def _part_regex(tokens: list) -> str:
    pieces: list[str] = [""]  # the fixed pieces between the stars
    for token in tokens:
        if isinstance(token, int):
            pieces.append("")
        else:
            pieces[-1] += token
    return join_stars(pieces)


def _glob_regex(pattern: str, anchored: bool = False) -> re.Pattern:
    """The regular expression for a glob: fullmatch it against a name, or with `anchored` against a
    relative path with a "/" after it ("docs/private/")."""
    parts = _glob_parts(pattern)
    if not anchored:
        return re.compile("/".join(_part_regex(tokens) for tokens in parts), re.S)
    runs: list[list[str]] = [[]]  # the runs of parts between "**" parts
    for tokens in parts:
        if len(tokens) == 1 and isinstance(tokens[0], int) and tokens[0] > 1:
            runs.append([])
        else:
            runs[-1].append(_part_regex(tokens) + "/")
    out = "".join(runs[0])
    if len(runs) > 1:
        for run in runs[1:-1]:
            if run:  # "**/**" is one "**"
                out += atomic("(?:[^/]*/)*?" + "".join(run))
        out += ("(?:[^/]*/)*" + "".join(runs[-1])) if runs[-1] else "(?:[^/]*/)+"
    return re.compile(out, re.S)


def parse_gitignore(text: str) -> list[_Rule]:
    """Simple .gitignore patterns: names, *.ext, dir/, /anchored, **/ prefix, !negation.

    A pattern that cannot be turned into a regular expression (such as "[z-a]") is skipped, and so
    is one longer than GITIGNORE_PATTERN_MAX.
    """
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or len(line) > GITIGNORE_PATTERN_MAX:
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        elif line.startswith("\\"):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        if not line:
            continue
        try:
            regex = _glob_regex(line, anchored)
        except (re.error, OverflowError, RecursionError):
            continue
        rules.append(_Rule(regex, negate, dir_only, anchored))
        if len(rules) >= GITIGNORE_RULES:
            break
    return rules


def _ignored(rules: list[_Rule], rel: str, name: str, is_dir: bool) -> bool:
    hit = False
    path = rel + "/"
    for rule in rules:
        if rule.dir_only and not is_dir:
            continue
        if rule.regex.fullmatch(path if rule.anchored else name):
            hit = not rule.negate
    return hit


def _load_gitignore(root: Path) -> list[_Rule]:
    try:
        with open(root / ".gitignore", "rb") as fh:
            raw = fh.read(GITIGNORE_LIMIT)
    except OSError:  # missing, a folder, no permission
        return []
    return parse_gitignore(raw.decode("utf-8", errors="replace"))


def _classify(entry: os.DirEntry, max_bytes: int) -> str | None:
    """Return "binary", "skipped" or None for a normal text file."""
    name = entry.name
    dot = name.rfind(".")
    ext = name[dot:].lower() if dot > 0 else ""
    if ext in BINARY_EXTS:
        return "binary"
    try:
        size = entry.stat(follow_symlinks=False).st_size  # no extra system call on Windows
    except OSError:
        return None
    if not (ext in TEXT_EXTS or name.startswith(".") or name.lower() in TEXT_NAMES):
        try:  # unknown kind of file: look for NUL bytes at the start
            with open(entry.path, "rb") as fh:
                if b"\x00" in fh.read(8192):
                    return "binary"
        except OSError:
            return None
    return "skipped" if size > max_bytes else None


def _is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)  # Windows junctions, Python 3.12+
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _is_package(path: str) -> bool:
    try:
        return os.path.isfile(os.path.join(path, "__init__.py"))
    except (OSError, ValueError):
        return False


def _showable(name: str) -> bool:
    """False for names that cannot be shown as UTF-8 (undecodable bytes, lone surrogates)."""
    if name.isascii():
        return True
    try:
        name.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def scan(root_abs: str | Path, max_files: int = 5000, max_bytes: int = 1_000_000) -> ScanResult:
    """Walk the folder breadth-first in sorted order and build the project index.

    The walk stops at max_files files, so a huge folder (a whole drive) costs only the
    part that is shown. Folders found by then but not looked through are kept and listed
    in `cut_off`. Folders that cannot be opened are kept and listed in `unreadable`.
    """
    root = Path(root_abs)
    if not root.is_dir():
        raise NotADirectoryError(f"폴더를 찾을 수 없어요: {root}")
    rules = _load_gitignore(root)
    files: list[str] = []
    dirs: list[str] = []
    hidden: set[str] = set()
    skipped: list[str] = []
    binary: list[str] = []
    unreadable: list[str] = []
    cut_off: list[str] = []
    crowded: list[str] = []
    file_limit = too_deep = limit_hit = False
    max_entries = max(MIN_ENTRIES, 4 * max_files)  # entries read from one folder
    extra_dirs = 200  # folders opened after the limit, only to learn whether more files exist
    queue: deque[tuple[str, int]] = deque([("", 0)])

    while queue:
        rel_dir, depth = queue.popleft()
        full = os.path.join(root, rel_dir) if rel_dir else str(root)
        if len(files) >= max_files:
            extra_dirs -= 1
            if extra_dirs < 0:
                file_limit = True  # cannot tell cheaply; more files are very likely
                rest = [rel_dir] + [rel for rel, _ in queue]
                cut_off.extend(rest)
                dirs.extend(rest)
                break
        try:
            with os.scandir(full) as it:
                entries = list(itertools.islice(it, max_entries + 1))
        except OSError:  # permission denied, vanished folder, path too long, ...
            if rel_dir:
                dirs.append(rel_dir)
            unreadable.append(rel_dir)
            continue
        if len(entries) > max_entries:  # a folder with an enormous number of entries
            entries = entries[:max_entries]
            crowded.append(rel_dir)
        entries.sort(key=lambda e: e.name)
        subdirs: list[tuple[str, int]] = []
        for entry in entries:
            name = entry.name
            if not _showable(name) or _is_link(entry):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = not is_dir and entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if name in IGNORED_DIRS or name.endswith(IGNORED_DIR_SUFFIXES):
                    if not (name in PACKAGE_NAMES and _is_package(entry.path)):
                        hidden.add(name)
                        continue
                if _ignored(rules, rel, name, True):
                    continue
                if depth >= MAX_DEPTH:
                    too_deep = True
                else:
                    subdirs.append((rel, depth + 1))
            elif is_file and name != ".git" and not limit_hit and not _ignored(rules, rel, name, False):
                if len(files) >= max_files:
                    limit_hit = True  # keep going through this folder for its subfolders only
                    continue
                files.append(rel)
                kind = _classify(entry, max_bytes)
                if kind == "binary":
                    binary.append(rel)
                elif kind == "skipped":
                    skipped.append(rel)
        if rel_dir:
            dirs.append(rel_dir)
        if limit_hit:
            file_limit = True
            rest = [rel for rel, _ in subdirs] + [rel for rel, _ in queue]
            cut_off.append(rel_dir)
            cut_off.extend(rest)
            dirs.extend(rest)
            break
        queue.extend(subdirs)

    return ScanResult(
        index=ProjectIndex(root, files, dirs, skipped=skipped, binary=binary),  # never read either
        hidden=sorted(hidden, key=lambda s: (s.lstrip(".").lower(), s)),
        skipped=sorted(skipped),
        binary=sorted(binary),
        truncated=file_limit or bool(crowded),
        unreadable=sorted(unreadable),
        too_deep=too_deep,
        cut_off=sorted(cut_off),
        file_limit=file_limit,
        crowded=sorted(crowded),
        max_entries=max_entries,
    )
