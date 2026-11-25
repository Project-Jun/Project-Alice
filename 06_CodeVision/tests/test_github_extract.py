"""github/extract.py: the streaming tar extractor. Every archive is built here, in memory."""
import errno
import gzip
import inspect
import io
import json
import ntpath
import os
import tarfile
import threading
import time

import pytest

from codevision import graph as graph_mod
from codevision import scan as scan_mod
from codevision.github import extract
from codevision.github.extract import (
    ArchiveTooBig, BadArchive, Cancelled, DiskFull, DownloadStalled, Limits, TooManyFiles, extract_stream,
    windows_unsafe,
)

TOP = "octo-hello-abc1234"
SHA = "abc1234" + "d" * 33
MiB = 1024 * 1024
KINDS = {"sym": tarfile.SYMTYPE, "lnk": tarfile.LNKTYPE, "chr": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
LFS = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"4" * 64 + b"\nsize 123456789\n"


# --- building archives ------------------------------------------------------------------------------------

def _add(tf, name, data, mode=None):
    info = tarfile.TarInfo(name)
    info.mtime = 1_764_000_000
    if data is None:  # a folder
        info.type, info.mode = tarfile.DIRTYPE, 0o775
        tf.addfile(info)
    elif isinstance(data, tuple):  # (kind, link target): links and special files
        info.type, info.linkname = KINDS[data[0]], data[1]
        tf.addfile(info)
    else:
        info.size, info.mode = len(data), 0o644 if mode is None else mode
        tf.addfile(info, io.BytesIO(data))


def tar_bytes(entries, *, top=TOP, comment=SHA, raw=(), end=True) -> bytes:
    """A tar as git archive writes it: a global pax comment, the top folder, then the entries
    (name, bytes | None for a folder | (kind, target)). raw entries get no top folder."""
    buf = io.BytesIO()
    tf = tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": comment} if comment else {})
    if top:
        _add(tf, top, None)
    for name, data, *mode in entries:
        _add(tf, f"{top}/{name}" if top else name, data, *mode)
    for name, data in raw:
        _add(tf, name, data)
    offset = tf.offset
    tf.close()
    data = buf.getvalue()
    return data if end else data[:offset]


def tarball(entries, **kw) -> bytes:
    return gzip.compress(tar_bytes(entries, **kw), compresslevel=6, mtime=0)


def git_order(files: dict) -> list:
    """Entries in git archive's order: depth first, every folder before its contents, siblings sorted by
    their bytes with '/' after a folder's name. A key ending in '/' is an (empty) folder."""
    tree: dict = {}
    for path, data in files.items():
        parts = path.rstrip("/").split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if path.endswith("/"):
            node.setdefault(parts[-1], {})
        else:
            node[parts[-1]] = data
    out = []

    def walk(node, prefix):
        def key(item):
            return item[0].encode("utf-8", "surrogateescape") + (b"/" if isinstance(item[1], dict) else b"")
        for name, value in sorted(node.items(), key=key):
            if isinstance(value, dict):
                out.append((prefix + name, None))
                walk(value, prefix + name + "/")
            else:
                out.append((prefix + name, value))

    walk(tree, "")
    return out


class Stream:
    """A response double: hands out data in chunks; can fail at a byte offset or call a hook per read."""

    def __init__(self, data: bytes, *, chunk=None, fail_at=None, error=None, on_read=None):
        self.data, self.pos, self.chunk, self.fail_at, self.error, self.on_read = data, 0, chunk, fail_at, error, on_read
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        if self.on_read:
            self.on_read(self)
        if self.fail_at is not None and self.pos >= self.fail_at:
            raise self.error
        n = len(self.data) - self.pos if n is None or n < 0 else n
        if self.chunk:
            n = min(n, self.chunk)
        if self.fail_at is not None:
            n = min(n, self.fail_at - self.pos)
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


class Clock:
    def __init__(self, step=0.0):
        self.t, self.step = 1000.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def run(tmp_path, data, *, stream=None, name="Hello", **kw):
    dest = tmp_path / name
    result = extract_stream(stream or io.BytesIO(data), dest, kw.pop("limits", None), **kw)
    return result, dest


def files_in(root) -> dict:
    out = {}
    for folder, _dirs, names in os.walk(root):
        for n in names:
            path = os.path.join(folder, n)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root).replace(os.sep, "/")] = fh.read()
    return out


def dirs_in(root) -> set:
    return {os.path.relpath(os.path.join(folder, d), root).replace(os.sep, "/")
            for folder, dirs, _names in os.walk(root) for d in dirs}


def write(root, files):
    for rel, content in files.items():
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


# --- the normal case ----------------------------------------------------------------------------------------

def test_extracts_a_normal_repository_and_strips_the_top_folder(tmp_path):
    files = {"README.md": b"# hello\n", "src/app.py": b"print('hi')\n", "docs/guide.md": b"guide\n"}
    data = tarball(git_order(files))
    res, dest = run(tmp_path, data)
    assert files_in(dest) == files
    assert dirs_in(dest) == {"src", "docs"}
    assert res.commit == SHA and res.top == TOP
    assert (res.files, res.folders, res.written) == (3, 2, sum(map(len, files.values())))
    assert res.read == len(data) and res.unpacked >= len(tar_bytes(git_order(files))) - 10240
    assert sum(res.skipped.values()) == 0
    assert sorted(os.listdir(tmp_path)) == ["Hello"]


def test_a_second_top_folder_is_skipped(tmp_path):
    data = tarball([("README.md", b"r")], raw=[("other-top/evil.txt", b"x"), ("other-top", None)])
    res, dest = run(tmp_path, data)
    assert files_in(dest) == {"README.md": b"r"}
    assert res.skipped["other_top"] == 2


def test_an_archive_must_start_with_its_top_folder(tmp_path):
    for first in ("../escape.txt", "/abs.txt", "README.md"):
        data = tarball([], top=None, raw=[(first, b"x"), (f"{TOP}/a.txt", b"a")])
        with pytest.raises(BadArchive) as err:
            run(tmp_path, data, name=f"t{len(os.listdir(tmp_path))}")
        assert err.value.reason == "corrupt"


# --- hostile entries ------------------------------------------------------------------------------------------

HOSTILE_NAMES = [
    "CON", "con.txt", "nul", "NUL.md", "aux.c", "COM1", "com1.log", "LPT1.txt", "PRN", "CONIN$", "COM\u00b9",
    "nul .txt", "trail.", "space ", "lt<.txt", "gt>.txt", 'q".txt', "pipe|.txt", "ques?.txt", "star*.txt",
    "colon:.txt", "ads.txt:stream", "ctrl\x01.txt", "back\\slash.txt", "a/../../escape.txt", "dot/./c.txt",
    "sub/aux/x.txt", "dirdot./x.txt", "dirsp /y.txt", "double//slash.txt",
]


def test_hostile_names_are_skipped_with_a_reason_and_nothing_is_made(tmp_path):
    res, dest = run(tmp_path, tarball([(name, b"x") for name in HOSTILE_NAMES]))
    assert os.listdir(dest) == []  # not even a folder for 'a/../../escape.txt' or 'sub/aux/x.txt'
    assert res.skipped["bad_name"] == len(HOSTILE_NAMES)
    assert res.files == 0 and sorted(os.listdir(tmp_path)) == ["Hello"]
    assert len(res.examples["bad_name"]) == extract.EXAMPLES
    assert "\x01" not in "".join(res.examples["bad_name"])


def test_traversal_absolute_and_drive_names_never_leave_the_folder(tmp_path):
    raw = [("../escape.txt", b"x"), ("/abs.txt", b"x"), ("C:/abs.txt", b"x"), ("C:rel.txt", b"x"),
           ("D:rel.txt", b"x"), ("\\\\server\\share\\x.txt", b"x"), (f"{TOP}/..\\..\\bs-escape.txt", b"x"),
           (f"{TOP}/a/../../b.txt", b"x")]
    res, dest = run(tmp_path, tarball([("ok.txt", b"ok")], raw=raw))
    assert files_in(dest) == {"ok.txt": b"ok"}
    assert res.skipped["other_top"] == 6 and res.skipped["bad_name"] == 2
    assert sorted(os.listdir(tmp_path)) == ["Hello"] and not os.path.exists(tmp_path.parent / "escape.txt")


def test_links_devices_and_fifos_are_never_created(tmp_path):
    entries = [("link-out", ("sym", "../../outside.txt")), ("link-abs", ("sym", "C:/Windows/win.ini")),
               ("link-in", ("sym", "README.md")), ("hard", ("lnk", f"{TOP}/README.md")), ("dev", ("chr", "")),
               ("fifo", ("fifo", "")), ("README.md", b"r")]
    res, dest = run(tmp_path, tarball(entries))
    assert files_in(dest) == {"README.md": b"r"}
    assert res.skipped["links"] == 6


def test_a_name_that_is_not_utf8_is_a_bad_name_shown_safely(tmp_path):
    res, dest = run(tmp_path, tarball([("bad\udcff.txt", b"x"), ("ok.txt", b"ok")]))
    assert files_in(dest) == {"ok.txt": b"ok"}
    assert res.skipped["bad_name"] == 1
    shown = res.examples["bad_name"][0]
    assert shown == "bad\ufffd.txt"
    json.dumps(res.to_dict(), ensure_ascii=False).encode("utf-8")  # storable and showable


@pytest.mark.parametrize("name, reason", [
    ("", "empty"), (".", "empty"), ("..", "empty"), ("trail.", "trailing"), ("space ", "trailing"),
    ("a:b", "char"), ("ads.txt:stream", "char"), ("q?.txt", "char"), ("back\\slash", "char"), ("tab\there", "char"),
    ("CON", "device"), ("con.txt", "device"), ("nul .txt", "device"), ("COM\u00b9", "device"), ("LPT0", "device"),
    ("CONOUT$", "device"), ("x" * 256, "long"), ("\udc80bad", "encoding"),
])
def test_windows_unsafe_reasons(name, reason):
    assert windows_unsafe(name) == reason


@pytest.mark.parametrize("name", [
    "README.md", "console.log", "auxiliary.c", "COM10", "nul_device.txt", "café.txt", "한글.md", "😀.txt",
    " leading-space.txt", "x" * 255, "😀" * 127,
])
def test_windows_safe_names(name):
    assert windows_unsafe(name) is None


def test_windows_unsafe_never_asks_ntpath_isreserved(monkeypatch):
    def refuse(*_a):
        raise AssertionError("isreserved reads 'a:' as a drive")
    for module in (ntpath, os.path):
        if hasattr(module, "isreserved"):
            monkeypatch.setattr(module, "isreserved", refuse)
    assert windows_unsafe("a:b") == "char" and windows_unsafe("nul.txt") == "device"


# --- collisions ----------------------------------------------------------------------------------------------

def test_a_case_collision_keeps_the_first_file(tmp_path):
    res, dest = run(tmp_path, tarball([("README.md", b"upper\n"), ("readme.md", b"lower\n")]))
    assert files_in(dest) == {"README.md": b"upper\n"}
    assert res.skipped["collision"] == 1 and res.examples["collision"] == ["readme.md"]


def test_folders_that_differ_by_case_merge_like_a_windows_clone(tmp_path):
    entries = [("Docs", None), ("Docs/a.txt", b"a"), ("docs", None), ("docs/b.txt", b"b"), ("docs/A.TXT", b"dup")]
    res, dest = run(tmp_path, tarball(entries))
    assert files_in(dest) == {"Docs/a.txt": b"a", "Docs/b.txt": b"b"}
    assert res.skipped["collision"] == 1 and res.examples["collision"] == ["Docs/A.TXT"]


def test_a_file_and_a_folder_with_one_name_collide(tmp_path):
    entries = [("a", b"file"), ("a/b.txt", b"x"), ("x", None), ("x/y.txt", b"y"), ("X", b"file")]
    res, dest = run(tmp_path, tarball(entries))
    assert files_in(dest) == {"a": b"file", "x/y.txt": b"y"}
    assert res.skipped["collision"] == 2


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are a Windows feature")
def test_an_8_3_short_name_alias_is_a_collision(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "longfilename.txt").write_bytes(b"x")
    if not (probe / "LONGFI~1.TXT").exists():
        pytest.skip("8.3 names are off on this volume")
    res, dest = run(tmp_path, tarball([("longfilename.txt", b"long"), ("LONGFI~1.TXT", b"alias")]))
    assert files_in(dest) == {"longfilename.txt": b"long"}
    assert res.skipped["collision"] == 1


# --- what is written ---------------------------------------------------------------------------------------

def test_binaries_by_extension_become_empty_placeholders(tmp_path):
    entries = [("logo.png", PNG), ("font.woff2", b"wOF2" * 100), ("app.exe", b"MZ" + b"\0" * 500),
               ("data.zip", b"PK\x03\x04" * 50)]
    res, dest = run(tmp_path, tarball(entries))
    assert files_in(dest) == {"logo.png": b"", "font.woff2": b"", "app.exe": b"", "data.zip": b""}
    assert res.skipped["binary"] == 4 and res.written == 0


def test_a_nul_binary_keeps_one_nul_byte_so_the_scanner_still_calls_it_binary(tmp_path):
    res, dest = run(tmp_path, tarball([("blob.dat", b"MZ\0\0" + b"payload" * 100), (".DS_Store", b"\0\0\0\x01Bud1")]))
    assert files_in(dest) == {"blob.dat": b"\0", ".DS_Store": b"\0"}
    assert res.skipped["binary"] == 2
    assert scan_mod.scan(dest).binary == ["blob.dat"]  # .DS_Store is text by name; read_text sees the NUL


def test_lfs_pointers_become_empty_files(tmp_path):
    hawser = b"version https://hawser.github.com/spec/v1\noid sha256:" + b"5" * 64 + b"\nsize 9\n"
    entries = [("model.bin", LFS), ("data.csv", LFS), ("old.psd", hawser), ("notes.md", b"version 1\n"),
               ("almost.txt", b"version https://git-lfs.github.com/spec/v1\nno oid here\n")]
    res, dest = run(tmp_path, tarball(entries))
    got = files_in(dest)
    assert got["model.bin"] == got["data.csv"] == got["old.psd"] == b""
    assert got["notes.md"] == b"version 1\n" and got["almost.txt"].startswith(b"version")
    assert res.skipped["lfs"] == 3


def test_a_text_file_is_cut_after_1000001_bytes_and_shows_as_over_1mb(tmp_path):
    big = b"a,b\n" * 275_000  # 1,100,000 bytes
    res, dest = run(tmp_path, tarball([("big.csv", big), ("small.csv", b"a,b\n")]))
    assert (dest / "big.csv").read_bytes() == big[:1_000_001]
    assert res.skipped["capped"] == 1 and res.written == 1_000_001 + 4
    assert scan_mod.scan(dest).skipped == ["big.csv"]


def test_a_big_text_kind_file_with_nul_bytes_stays_over_1mb(tmp_path):
    utf16 = "x".encode("utf-16") * 600_000  # 2,400,000 bytes, NUL bytes everywhere, .txt is text by name
    res, dest = run(tmp_path, tarball([("log.txt", utf16)]))
    assert (dest / "log.txt").stat().st_size == 1_000_001
    assert scan_mod.scan(dest).skipped == ["log.txt"] and res.skipped["capped"] == 1


def test_the_text_cap_is_the_scanners_limit_plus_one():
    assert extract.TEXT_CAP == inspect.signature(scan_mod.scan).parameters["max_bytes"].default + 1
    assert Limits().text_cap == extract.TEXT_CAP


def test_modes_from_the_archive_are_not_applied(tmp_path):
    res, dest = run(tmp_path, tarball([("setuid.sh", b"#!/bin/sh\n", 0o4755), ("noperm.txt", b"x\n", 0o000)]))
    assert files_in(dest) == {"setuid.sh": b"#!/bin/sh\n", "noperm.txt": b"x\n"}
    assert os.access(dest / "noperm.txt", os.W_OK)


def test_empty_folder_entries_are_created(tmp_path):
    res, dest = run(tmp_path, tarball(git_order({"empty/": None, "a/b/": None, "x.txt": b"x"})))
    assert dirs_in(dest) == {"empty", "a", "a/b"} and res.folders == 3


def test_submodules_stay_empty_folders_and_are_counted(tmp_path):
    gitmodules = (b'[submodule "sub"]\n\tpath = libs/sub\n\turl = https://example.invalid/sub.git\n'
                  b'[submodule "gone"]\n\tpath = "vendor/gone"\n')
    data = tarball(git_order({".gitmodules": gitmodules, "libs/sub/": None, "libs/keep.txt": b"k"}))
    res, dest = run(tmp_path, data)
    assert (dest / "libs" / "sub").is_dir() and os.listdir(dest / "libs" / "sub") == []
    assert res.skipped["submodules"] == 1


# --- the scanner's ignore rules ----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def node_modules_60k() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": SHA}) as tf:
        _add(tf, TOP, None)
        _add(tf, f"{TOP}/node_modules", None)
        for i in range(600):
            pkg = f"{TOP}/node_modules/pkg{i:03d}"
            _add(tf, pkg, None)
            for j in range(100):
                _add(tf, f"{pkg}/f{j:02d}.js", b"")
        _add(tf, f"{TOP}/package.json", b"{}")
        _add(tf, f"{TOP}/src", None)
        _add(tf, f"{TOP}/src/index.js", b"x")
    return gzip.compress(buf.getvalue(), compresslevel=1, mtime=0)


def test_a_committed_node_modules_of_60000_files_is_neither_written_nor_counted(tmp_path, node_modules_60k):
    res, dest = run(tmp_path, node_modules_60k)  # the default 50,000-file cap
    assert files_in(dest) == {"package.json": b"{}", "src/index.js": b"x"}
    assert (dest / "node_modules").is_dir() and os.listdir(dest / "node_modules") == []
    assert res.files == 2 and res.skipped["ignored"] == 60_600
    assert scan_mod.scan(dest).hidden == ["node_modules"]


def test_entries_that_sort_before_init_py_are_held_then_dropped(tmp_path):
    files = {"node_modules/.package-lock.json": b"{}", "node_modules/@babel/core/index.js": b"x",
             "node_modules/@types/node/index.d.ts": b"x", "node_modules/Upper/index.js": b"x",
             "node_modules/react/index.js": b"x", "index.js": b"x"}
    res, dest = run(tmp_path, tarball(git_order(files)))
    assert files_in(dest) == {"index.js": b"x"} and os.listdir(dest / "node_modules") == []
    res, dest = run(tmp_path, tarball(git_order(files)), name="capped", limits=Limits(max_held=3))
    assert files_in(dest) == {"index.js": b"x"} and os.listdir(dest / "node_modules") == []


def test_a_committed_venv_is_hidden_and_does_not_count(tmp_path):
    files = {f".venv/Lib/site-packages/pkg{i}.py": b"x" for i in range(50)}
    files.update({"venv/Include/x.h": b"x", "venv/Lib/site-packages/y.py": b"y", "venv/pyvenv.cfg": b"home",
                  "app.py": b"print(1)\n", "requirements.txt": b"flask\n"})
    res, dest = run(tmp_path, tarball(git_order(files)), limits=Limits(max_files=2))
    assert files_in(dest) == {"app.py": b"print(1)\n", "requirements.txt": b"flask\n"}
    assert os.listdir(dest / ".venv") == [] and os.listdir(dest / "venv") == []
    assert scan_mod.scan(dest).hidden == [".venv", "venv"]


def test_a_build_folder_that_is_a_package_is_kept(tmp_path):
    files = {"build/README.md": b"notes\n", "build/__init__.py": b"", "build/tool.py": b"import os\n",
             "src/pip/_internal/operations/build/__init__.py": b"", "src/pip/_internal/operations/build/wheel.py": b"w\n",
             "main.py": b"import build.tool\n"}
    res, dest = run(tmp_path, tarball(git_order(files)))
    assert files_in(dest) == files
    assert scan_mod.scan(dest).hidden == []


def test_a_build_folder_without_init_py_is_hidden_and_empty(tmp_path):
    files = {"build/Makefile": b"all:\n", "build/out.js": b"x", "dist/bundle.js": b"x", "pkg.egg-info/PKG-INFO": b"x",
             "web/dist/app.js": b"x", "web/index.js": b"x"}
    res, dest = run(tmp_path, tarball(git_order(files)))
    assert files_in(dest) == {"web/index.js": b"x"}
    assert {"build", "dist", "pkg.egg-info", "web/dist"} <= dirs_in(dest)
    assert scan_mod.scan(dest).hidden == ["build", "dist", "pkg.egg-info"]
    assert res.files == 1 and res.skipped["ignored"] == 5


def test_package_folders_inside_a_package_are_decided_on_their_own(tmp_path):
    files = {"build/Notes.md": b"n", "build/__init__.py": b"", "build/dist/Main.py": b"m",
             "build/dist/__init__.py": b"", "build/env/x.py": b"x", "build/env/zz.txt": b"z"}
    res, dest = run(tmp_path, tarball(git_order(files)))
    assert files_in(dest) == {"build/Notes.md": b"n", "build/__init__.py": b"", "build/dist/Main.py": b"m",
                              "build/dist/__init__.py": b""}
    assert os.listdir(dest / "build" / "env") == []


def test_dot_git_folders_and_files_are_not_written(tmp_path):
    files = {".git/HEAD": b"ref: refs/heads/main\n", ".git/config": b"[core]\n", "sub/.git": b"gitdir: ../x\n",
             "sub/a.txt": b"a"}
    res, dest = run(tmp_path, tarball(git_order(files)))
    assert files_in(dest) == {"sub/a.txt": b"a"} and os.listdir(dest / ".git") == []


def test_the_root_gitignore_applies_to_what_follows_it(tmp_path):
    files = {".gitignore": b"out/\n*.log\n!keep.log\n", "app.log": b"x", "keep.log": b"k", "out/x.txt": b"x",
             "src/a.log": b"x", "src/b.py": b"b"}
    res, dest = run(tmp_path, tarball(git_order(files)))
    assert files_in(dest) == {".gitignore": files[".gitignore"], "keep.log": b"k", "src/b.py": b"b"}
    assert "out" not in dirs_in(dest) and res.skipped["ignored"] == 3


def test_a_hostile_root_gitignore_does_not_stall_the_extraction(tmp_path):
    # "*a*a...*b" against a long name used to take minutes in the regex engine, with the GIL held
    files = {".gitignore": ("*a" * 13 + "*b\n").encode(), "a" * 60 + ".md": b"x", "a" * 13 + "b": b"x"}
    started = time.monotonic()
    res, dest = run(tmp_path, tarball(git_order(files)))
    assert time.monotonic() - started < 2
    assert set(files_in(dest)) == {".gitignore", "a" * 60 + ".md"} and res.skipped["ignored"] == 1


def test_ignored_entries_do_not_count_toward_the_file_cap(tmp_path):
    files = {f"node_modules/lib{i}/index.js": b"x" for i in range(20)}
    files.update({"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})
    res, dest = run(tmp_path, tarball(git_order(files)), limits=Limits(max_files=3))
    assert res.files == 3
    files["d.txt"] = b"d"
    with pytest.raises(TooManyFiles) as err:
        run(tmp_path, tarball(git_order(files)), name="four", limits=Limits(max_files=3))
    assert (err.value.what, err.value.limit) == ("files", 3)


def test_a_snapshot_graph_equals_the_graph_of_a_plain_clone(tmp_path):
    files = {
        "README.md": b"# Hello\n\nA small app.\n",
        "main.py": b"from hello.util import helper\n\nif __name__ == '__main__':\n    print(helper())\n",
        "hello/__init__.py": b"", "hello/util.py": b"def helper():\n    return 1\n",
        "web/index.js": b"import { x } from './app.js'\nconsole.log(x)\n", "web/app.js": b"export const x = 1\n",
        "assets/logo.png": PNG, "blob.dat": b"ab\0cd" * 10, "utf16.txt": "hello".encode("utf-16"),
        "big.csv": b"a,b\n" * 300_000, ".DS_Store": b"\0\0\0\x01Bud1" * 10,
        "node_modules/react/index.js": b"module.exports = 1\n", ".venv/Lib/site.py": b"x = 1\n",
        "build/README.md": b"b\n", "build/__init__.py": b"", "build/tool.py": b"import os\n",
        "dist/bundle.js": b"x", "pkg.egg-info/PKG-INFO": b"x", "docs/guide.md": b"# Guide\n",
        ".gitignore": b"output/\n*.log\n", "output/result.txt": b"x", "debug.log": b"x",
    }
    clone = tmp_path / "clone" / "hello-repo"
    write(clone, files)
    (tmp_path / "snap").mkdir()
    res, snap = run(tmp_path / "snap", tarball(git_order(files)), name="hello-repo")
    assert not (snap / "output").exists() and not (snap / "debug.log").exists()

    def comparable(g):
        return {k: v for k, v in g.items() if k not in ("source", "generated")}

    assert comparable(graph_mod.build(str(snap))) == comparable(graph_mod.build(str(clone)))


# --- caps --------------------------------------------------------------------------------------------------------

def test_the_written_cap_stops_the_job(tmp_path):
    with pytest.raises(ArchiveTooBig) as err:
        run(tmp_path, tarball([("a.txt", b"x" * 20)]), limits=Limits(max_written=10))
    assert (err.value.what, err.value.limit) == ("written", 10)


def test_the_compressed_cap_stops_the_job(tmp_path):
    data = tarball([(f"f{i}.txt", os.urandom(512).hex().encode()) for i in range(20)])
    with pytest.raises(ArchiveTooBig) as err:
        run(tmp_path, data, limits=Limits(max_compressed=4096))
    assert err.value.what == "compressed"


def test_the_unpacked_cap_stops_a_gzip_bomb_even_in_an_ignored_folder(tmp_path):
    raw = tar_bytes([("node_modules", None), ("node_modules/zeros.bin", b"\0" * (20 * MiB))])
    data = gzip.compress(raw, compresslevel=9, mtime=0)
    assert len(data) < 100_000
    with pytest.raises(ArchiveTooBig) as err:
        run(tmp_path, data, limits=Limits(max_unpacked=4 * MiB))
    assert err.value.what == "unpacked"


def test_the_entry_cap_stops_the_job(tmp_path):
    with pytest.raises(TooManyFiles) as err:
        run(tmp_path, tarball([(f"node_modules/f{i}", b"") for i in range(10)]), limits=Limits(max_entries=5))
    assert err.value.what == "entries"


# --- the stream: cancel, stalls, progress, broken downloads -------------------------------------------------

def big_archive():
    return tarball([(f"f{i:03d}.txt", os.urandom(1000).hex().encode()) for i in range(100)])


def test_cancel_before_the_start_and_during_the_download(tmp_path):
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        run(tmp_path, big_archive(), cancel=ev)
    ev = threading.Event()
    stream = Stream(big_archive(), chunk=4096, on_read=lambda s: ev.set() if s.pos > 20_000 else None)
    with pytest.raises(Cancelled):
        run(tmp_path, b"", stream=stream, cancel=ev, name="second")
    assert stream.pos < len(stream.data)


def test_a_response_closed_to_cancel_gives_cancelled(tmp_path):
    ev = threading.Event()  # the service sets cancel, then closes the response under the blocked read
    stream = Stream(big_archive(), fail_at=0, error=ValueError("I/O operation on closed file"),
                    on_read=lambda s: ev.set())
    with pytest.raises(Cancelled):
        run(tmp_path, b"", stream=stream, cancel=ev)


def test_the_clients_own_errors_pass_through_unless_the_job_was_cancelled(tmp_path):
    class ClientError(Exception):  # like GitHubError('interrupted') from client.Download
        pass

    stream = Stream(big_archive(), fail_at=5000, error=ClientError("interrupted"))
    with pytest.raises(ClientError):
        run(tmp_path, b"", stream=stream)
    ev = threading.Event()  # Download.abort() wakes the blocked read with the same error
    stream = Stream(big_archive(), fail_at=5000, error=ClientError("interrupted"),
                    on_read=lambda s: ev.set() if s.pos >= 5000 else None)
    with pytest.raises(Cancelled):
        run(tmp_path, b"", stream=stream, cancel=ev, name="cancelled")


def test_a_read_timeout_is_a_stall(tmp_path):
    stream = Stream(big_archive(), fail_at=3000, error=TimeoutError("timed out"))
    with pytest.raises(DownloadStalled) as err:
        run(tmp_path, b"", stream=stream)
    assert err.value.what == "stall"


def test_the_ceiling_stops_a_download_that_never_ends(tmp_path):
    stream = Stream(big_archive(), chunk=1024)
    with pytest.raises(DownloadStalled) as err:
        run(tmp_path, b"", stream=stream, clock=Clock(step=100.0), limits=Limits(max_seconds=1000))
    assert err.value.what == "ceiling"


def test_progress_reports_bytes_and_files_and_the_final_totals(tmp_path):
    data = big_archive()
    calls = []
    res, _ = run(tmp_path, b"", stream=Stream(data, chunk=8192), clock=Clock(step=1.0),
                 progress=lambda read, files: calls.append((read, files)))
    assert len(calls) > 3 and calls == sorted(calls)
    assert calls[-1] == (len(data), 100) == (res.read, res.files)
    quiet = []
    run(tmp_path, data, name="quiet", clock=Clock(step=0.0), progress=lambda *a: quiet.append(a))
    assert quiet == [(len(data), 100)]  # at most every 0.25 s, plus the final call


def test_a_response_that_trickles_one_byte_at_a_time_still_works(tmp_path):
    files = {"README.md": b"# hello\n", "src/app.py": b"print(1)\n" * 50}
    data = tarball(git_order(files))
    stream = Stream(data, chunk=1)
    res, dest = run(tmp_path, b"", stream=stream)
    assert files_in(dest) == files and res.read == len(data) and stream.reads >= len(data)


def test_a_cut_download_is_truncated(tmp_path):
    data = big_archive()
    with pytest.raises(BadArchive) as err:
        run(tmp_path, data[: len(data) // 2])
    assert err.value.reason == "truncated"


def test_a_connection_reset_mid_stream_is_truncated(tmp_path):
    stream = Stream(big_archive(), fail_at=50_000, error=ConnectionResetError(10054, "reset"))
    with pytest.raises(BadArchive) as err:
        run(tmp_path, b"", stream=stream)
    assert (err.value.reason, err.value.detail) == ("truncated", "ConnectionResetError")
    assert "reset" not in str(err.value)


@pytest.mark.parametrize("data, reason", [
    (b"<html><body>Bad gateway</body></html>", "not_gzip"),
    (b"", "empty"),
    (tar_bytes([("a.txt", b"a")]), "not_gzip"),
])
def test_a_reply_that_is_not_a_gzip_archive(tmp_path, data, reason):
    with pytest.raises(BadArchive) as err:
        run(tmp_path, data)
    assert err.value.reason == reason


def test_a_bad_gzip_checksum_is_corrupt(tmp_path):
    data = bytearray(tarball([("a.txt", b"a" * 1000)]))
    data[-8] ^= 0xFF  # the CRC-32 in the gzip trailer
    with pytest.raises(BadArchive) as err:
        run(tmp_path, bytes(data))
    assert err.value.reason == "corrupt"


def test_a_tar_without_its_end_blocks_is_corrupt(tmp_path):
    data = gzip.compress(tar_bytes([("a.txt", b"a"), ("b.txt", b"b")], end=False), mtime=0)
    with pytest.raises(BadArchive) as err:
        run(tmp_path, data)
    assert err.value.reason == "corrupt"


def test_data_after_the_end_of_the_archive_is_corrupt(tmp_path):
    data = gzip.compress(tar_bytes([("a.txt", b"a")]) + b"hidden" * 10_000, mtime=0)
    with pytest.raises(BadArchive) as err:
        run(tmp_path, data)
    assert err.value.reason == "corrupt"


def test_the_pax_comment_is_kept_and_checked(tmp_path):
    with pytest.raises(BadArchive) as err:
        run(tmp_path, tarball([("a.txt", b"a")], comment="f" * 40), expect_commit=SHA)
    assert err.value.reason == "commit_mismatch" and os.listdir(tmp_path / "Hello") == []
    res, _ = run(tmp_path, tarball([("a.txt", b"a")], comment=None), name="nocomment", expect_commit=SHA)
    assert res.commit is None and res.files == 1  # a missing comment is accepted (and recorded)


def test_dest_must_be_a_new_folder(tmp_path):
    (tmp_path / "Hello").mkdir()
    with pytest.raises(FileExistsError):
        run(tmp_path, tarball([("a.txt", b"a")]))


# --- Windows path limits on the final path ---------------------------------------------------------------------

def test_long_paths_off_limit_files_to_259_and_folders_to_247_units(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "long_paths_enabled", lambda: False)
    final_root = "C:\\" + "r" * 97  # 100 units: where the snapshot will live, longer than the staging folder
    d_ok, d_long, f_ok, f_long = "d" * 146, "e" * 147, "f" * 158, "g" * 159
    entries = [(d_ok, None), (f"{d_ok}/abcdefghijk", b"1"), (f"{d_ok}/abcdefghijkl", b"2"), (d_long, None),
               (f"{d_long}/x", b"3"), (f_ok, b"4"), (f_long, b"5")]
    res, dest = run(tmp_path, tarball(entries), final_root=final_root)
    assert files_in(dest) == {f"{d_ok}/abcdefghijk": b"1", f_ok: b"4"}
    assert dirs_in(dest) == {d_ok}
    assert res.skipped["too_long"] == 3


def test_path_limits_count_utf16_units(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "long_paths_enabled", lambda: False)
    final_root = "C:\\" + "r" * 237  # 240 units
    three, four = "😀" * 3, "😀" * 4  # 6 and 8 units: 247 fits, 249 does not (len() would say 245)
    entries = [(three, None), (f"{three}/a.txt", b"a"), (four, None), (f"{four}/b.txt", b"b")]
    res, dest = run(tmp_path, tarball(entries), final_root=final_root)
    assert files_in(dest) == {f"{three}/a.txt": b"a"} and res.skipped["too_long"] == 1
    assert extract.utf16_len(four) == 8 and windows_unsafe("😀" * 128) == "long"


def test_long_paths_on_allow_up_to_1000_units(tmp_path):
    final_root = "C:\\" + "r" * 986  # 989 units
    entries = [("abcdef.txt", b"ok"), ("abcdefg.txt", b"long")]  # 989 + 1 + 10 = 1000; 1001
    res, dest = run(tmp_path, tarball(entries), final_root=final_root, limits=Limits(long_paths=True))
    assert files_in(dest) == {"abcdef.txt": b"ok"} and res.skipped["too_long"] == 1
    assert extract.path_limits(True) == (1000, 1000) and extract.path_limits(False) == (259, 247)


def test_a_name_over_255_units_is_too_long(tmp_path):
    res, dest = run(tmp_path, tarball([("n" * 256, b"x"), ("ok.txt", b"ok")]))
    assert files_in(dest) == {"ok.txt": b"ok"} and res.skipped["too_long"] == 1


# --- per-file write errors -------------------------------------------------------------------------------------

def failing_create(monkeypatch, names, error):
    real = extract._create

    def create(path):
        if os.path.basename(path) in names:
            raise error
        return real(path)

    monkeypatch.setattr(extract, "_create", create)


class Virus(PermissionError):
    winerror = 225


class InUse(PermissionError):
    winerror = 32


class Denied(PermissionError):
    winerror = 5


class BadWinName(OSError):
    winerror = 123


class WinDiskFull(OSError):
    winerror = 112


@pytest.mark.parametrize("error", [Virus(13, "virus"), InUse(13, "in use"), Denied(13, "denied")])
def test_a_file_the_antivirus_blocks_is_skipped_and_the_rest_continues(tmp_path, monkeypatch, error):
    failing_create(monkeypatch, {"eicar.com.txt"}, error)
    res, dest = run(tmp_path, tarball([("a.txt", b"a"), ("eicar.com.txt", b"X5O!"), ("z.txt", b"z")]))
    assert files_in(dest) == {"a.txt": b"a", "z.txt": b"z"}
    assert res.skipped["blocked"] == 1 and res.examples["blocked"] == ["eicar.com.txt"]


@pytest.mark.parametrize("error", [OSError(errno.ENOSPC, "No space left on device"), WinDiskFull(28, "full")])
def test_a_full_disk_stops_the_job(tmp_path, monkeypatch, error):
    failing_create(monkeypatch, {"b.txt"}, error)
    with pytest.raises(DiskFull):
        run(tmp_path, tarball([("a.txt", b"a"), ("b.txt", b"b"), ("c.txt", b"c")]))
    assert extract.is_disk_full(error) and not extract.is_disk_full(Virus(13, "virus"))


def test_any_other_error_on_one_file_is_a_bad_name_and_extraction_continues(tmp_path, monkeypatch):
    failing_create(monkeypatch, {"odd.txt"}, BadWinName(22, "invalid name"))
    res, dest = run(tmp_path, tarball([("odd.txt", b"o"), ("ok.txt", b"k")]))
    assert files_in(dest) == {"ok.txt": b"k"} and res.skipped["bad_name"] == 1


def test_examples_are_capped_and_safe_to_store(tmp_path):
    names = [f"bad{i}\x07?.txt" for i in range(30)] + ["\u202eevil.txt:x"]
    res, _ = run(tmp_path, tarball([(n, b"x") for n in names]))
    assert res.skipped["bad_name"] == 31 and len(res.examples["bad_name"]) == extract.EXAMPLES
    text = json.dumps(res.to_dict(), ensure_ascii=False)
    assert "\x07" not in text and "\u202e" not in text
    assert set(res.to_dict()) >= {"commit", "files", "written", "read", "skipped", "examples"}
