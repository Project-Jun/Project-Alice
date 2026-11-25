import os
import time

import pytest

from codevision import scan as scan_mod
from codevision.scan import scan


def write(root, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def test_default_ignored_folders_are_hidden(tmp_path):
    write(tmp_path, {
        "src/app.py": "print(1)\n",
        "node_modules/react/index.js": "x",
        "web/node_modules/vue/index.js": "x",
        ".venv/lib/site.py": "x",
        "__pycache__/app.cpython-314.pyc": b"\x00\x01",
        "pkg.egg-info/PKG-INFO": "x",
        ".git/HEAD": "ref: refs/heads/main\n",
        "dist/bundle.js": "x",
        "web/index.js": "x",
        ".github/workflows/ci.yml": "on: push\n",
        ".env.example": "API_KEY=\n",
    })
    result = scan(tmp_path)
    assert result.index.files == {"src/app.py", "web/index.js", ".github/workflows/ci.yml", ".env.example"}
    assert result.index.dirs == {"", "src", "web", ".github", ".github/workflows"}
    assert result.hidden == ["__pycache__", "dist", ".git", "node_modules", "pkg.egg-info", ".venv"]
    assert result.truncated is False


def test_gitignore_simple_patterns(tmp_path):
    write(tmp_path, {
        ".gitignore": "\n".join([
            "# comment",
            "secret.txt",
            "*.log",
            "!keep.log",
            "out/",
            "/root_only.txt",
            "**/gen",
            "docs/private",
            "cache-*/",
            "",
        ]),
        "main.py": "x",
        "secret.txt": "x",
        "sub/secret.txt": "x",
        "app.log": "x",
        "keep.log": "x",
        "out/x.txt": "x",
        "sub/out": "a file, not a folder",
        "root_only.txt": "x",
        "sub/root_only.txt": "x",
        "gen/y.js": "x",
        "a/b/gen/x.js": "x",
        "docs/private/p.md": "x",
        "docs/public.md": "x",
        "sub/docs/private/q.md": "x",
        "cache-1/z.txt": "x",
    })
    result = scan(tmp_path)
    assert result.index.files == {
        ".gitignore", "main.py", "keep.log", "sub/out", "sub/root_only.txt", "docs/public.md",
        "sub/docs/private/q.md",
    }
    assert "out" not in result.index.dirs and "a/b" in result.index.dirs
    assert result.hidden == []  # .gitignore matches are not listed as hidden


def test_binary_and_oversized_files_stay_listed(tmp_path):
    write(tmp_path, {
        "logo.png": "not really an image",
        "blob.dat": b"abc\x00def",
        "big.txt": "x" * 200,
        "big.png": b"\x89PNG" + b"\x00" * 300,
        "small.txt": "hello",
        "icon.svg": "<svg></svg>",
    })
    result = scan(tmp_path, max_bytes=100)
    assert result.index.files == {"logo.png", "blob.dat", "big.txt", "big.png", "small.txt", "icon.svg"}
    assert result.binary == ["big.png", "blob.dat", "logo.png"]
    assert result.skipped == ["big.txt"]


def test_truncation_keeps_shallow_files_first(tmp_path):
    write(tmp_path, {name: "x" for name in ("a.txt", "b.txt", "c.txt", "sub/d.txt", "sub/e.txt")})
    result = scan(tmp_path, max_files=3)
    assert result.truncated is True
    assert result.file_limit is True and result.crowded == []  # why: the file limit
    assert result.index.files == {"a.txt", "b.txt", "c.txt"}

    exact = scan(tmp_path, max_files=5)
    assert exact.truncated is False and exact.file_limit is False
    assert len(exact.index.files) == 5


def test_a_folder_with_too_many_entries_is_reported_as_crowded(tmp_path, monkeypatch):
    # .gitignore hides the logs, so only 2 files are shown: the file limit is not why the scan stopped short
    write(tmp_path, {".gitignore": "*.log\n", "main.py": "x", **{f"logs/{i:03d}.log": "" for i in range(30)}})
    monkeypatch.setattr(scan_mod, "MIN_ENTRIES", 10)
    result = scan(tmp_path, max_files=5)  # at most max(10, 4 * 5) = 20 entries read from one folder
    assert result.truncated is True
    assert result.crowded == ["logs"] and result.file_limit is False and result.max_entries == 20
    assert result.index.files == {".gitignore", "main.py"}


def test_scan_is_deterministic(tmp_path):
    write(tmp_path, {f"d{i}/f{j}.py": "x" for i in range(3) for j in range(3)})
    first, second = scan(tmp_path, max_files=4), scan(tmp_path, max_files=4)
    assert first.index.files == second.index.files
    assert first.index.files == {"d0/f0.py", "d0/f1.py", "d0/f2.py", "d1/f0.py"}


def test_links_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    write(outside, {"secret.py": "x"})
    root = tmp_path / "proj"
    write(root, {"main.py": "x"})
    made = False
    try:
        os.symlink(outside, root / "linked", target_is_directory=True)
        made = True
    except (OSError, NotImplementedError):
        if os.name == "nt":
            try:
                import _winapi
                _winapi.CreateJunction(str(outside), str(root / "linked"))
                made = True
            except (ImportError, OSError):
                pass
    if not made:
        pytest.skip("cannot create a symlink or junction here")
    result = scan(root)
    assert result.index.files == {"main.py"}
    assert "linked" not in result.index.dirs


def test_permission_errors_do_not_crash(tmp_path, monkeypatch):
    write(tmp_path, {"ok/a.py": "x", "locked/b.py": "x", "c.py": "x"})
    real_scandir = os.scandir

    def fake_scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(scan_mod.os, "scandir", fake_scandir)
    result = scan(tmp_path)
    assert result.index.files == {"ok/a.py", "c.py"}
    assert "locked" in result.index.dirs
    assert result.unreadable == ["locked"]


def test_unreadable_root_gives_an_empty_index(tmp_path, monkeypatch):
    write(tmp_path, {"a.py": "x"})

    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(scan_mod.os, "scandir", denied)
    result = scan(tmp_path)
    assert result.index.files == frozenset() and result.unreadable == [""]


def test_python_packages_named_like_output_folders_are_kept(tmp_path):
    # pip/_internal/operations/build and the standard library's venv are source code
    write(tmp_path, {
        "pkg/build/__init__.py": "", "pkg/build/wheel.py": "x",
        "venv/__init__.py": "", "venv/core.py": "x",
        "build/lib/pkg/__init__.py": "", "dist/app.js": "x", "env/pyvenv.cfg": "x",
    })
    result = scan(tmp_path)
    assert result.index.files == {"pkg/build/__init__.py", "pkg/build/wheel.py", "venv/__init__.py", "venv/core.py"}
    assert result.hidden == ["build", "dist", "env"]


def test_bad_gitignore_patterns_are_skipped(tmp_path):
    write(tmp_path, {".gitignore": "[z-a].txt\n*.log\n", "a.log": "x", "b.txt": "x"})
    assert scan(tmp_path).index.files == {".gitignore", "b.txt"}


@pytest.mark.parametrize("pattern, path, hit", [
    ("*a*b", "xaxb", True), ("*a*b", "ab", True), ("*a*b", "ba", False), ("*a*a*b", "aab", True),
    ("*a*a*b", "abab", True), ("*a*a*b", "ab", False), ("a*?b*c", "axbc", True), ("a*?b*c", "abc", False),
    ("*.py[co]", "x.pyc", True), ("*.py[!co]", "x.pyc", False), ("x*y*z", "x/y/z", False),
    ("/src/*a*b", "src/xaxb", True), ("/src/*a*b", "src/x/ab", False), ("/a*/b*/c", "ax/by/c", True),
    ("**/gen", "gen", True), ("**/gen", "a/b/gen", True), ("**/gen", "agen", False),
    ("a/**/b", "a/b", True), ("a/**/b", "a/x/y/b", True), ("a/**/b", "ab", False),
    ("a/**", "a/x", True), ("a/**", "a/x/y", True), ("a/**", "a", False),
    ("**/a/**/b/**/c", "x/a/y/b/c", True), ("**/a/**/b/**/c", "b/a/c", False), ("**/a/**/b/**/c", "a/a/b/b/c", True),
    ("**/a/**", "x/a/y", True), ("**/a/**", "x/a", False), ("/**", "anything/at/all", True),
    ("a**", "abc", True), ("/a**", "ab/c", False),  # "**" inside a part is a star, as in git
])
def test_gitignore_globs_match_like_git(pattern, path, hit):
    rules = scan_mod.parse_gitignore(pattern + "\n")
    name = path.rsplit("/", 1)[-1]
    assert scan_mod._ignored(rules, path, name, False) is hit


def test_a_hostile_gitignore_pattern_costs_no_time(tmp_path):
    # Written out as a plain regex, "*a*a...*b" tries every split of a long name between its stars:
    # 90 s for one 40-character name, holding the GIL, which stopped the whole server
    stars = "*a" * 12 + "*b"
    deep = "/".join(["a"] * 40)
    write(tmp_path, {
        ".gitignore": "\n".join([stars, "/deep/" + stars, "**/" + "/".join(["**", "a"] * 15) + "/b", ""]),
        "a" * 60 + ".md": "x", "deep/" + "a" * 120: "x", deep + "/c.txt": "x", "kept.b": "x",
        "a" * 12 + "b": "x", "deep/x" + "a" * 12 + "b": "x", deep + "/b": "x",
    })
    started = time.monotonic()
    result = scan(tmp_path)
    assert time.monotonic() - started < 1
    assert result.index.files == {".gitignore", "a" * 60 + ".md", "deep/" + "a" * 120, deep + "/c.txt", "kept.b"}
    rules = scan_mod.parse_gitignore(stars + "\n")
    started = time.monotonic()
    assert scan_mod._ignored(rules, "a" * 250 + "b", "a" * 250 + "b", False) is True
    assert scan_mod._ignored(rules, "a" * 250, "a" * 250, False) is False
    assert time.monotonic() - started < 1


def test_a_gitignore_pattern_longer_than_any_path_is_skipped():
    assert scan_mod.parse_gitignore("*a" * 3000 + "\n*.log\n")[0].regex.pattern == "[^/]*\\.log"


def test_walk_stops_at_the_limit(tmp_path, monkeypatch):
    files = {f"top{i}.txt": "x" for i in range(3)}
    for i in range(40):
        files.update({f"sub{i:02d}/f.txt": "x", f"sub{i:02d}/deeper/g.txt": "x"})
    write(tmp_path, files)
    opened = []
    real_scandir = os.scandir

    def counting(path):
        opened.append(path)
        return real_scandir(path)

    monkeypatch.setattr(scan_mod.os, "scandir", counting)
    result = scan(tmp_path, max_files=4)
    assert result.truncated is True
    assert result.index.files == {"top0.txt", "top1.txt", "top2.txt", "sub00/f.txt"}
    assert len(opened) == 3  # the root, sub00 and sub01, where the 5th file showed up: nothing more
    # folders that were found but not looked through are kept and marked, so they are not shown as empty
    assert {"sub01", "sub39", "sub00/deeper", "sub01/deeper"} <= set(result.cut_off)
    assert set(result.cut_off) <= result.index.dirs
    assert "sub00" not in result.cut_off  # looked through completely


def test_too_deep_folders_are_not_walked(tmp_path, monkeypatch):
    write(tmp_path, {"a/b/c/d/e/deep.py": "x", "a/top.py": "x"})
    monkeypatch.setattr(scan_mod, "MAX_DEPTH", 3)
    result = scan(tmp_path)
    assert result.too_deep is True
    assert result.index.files == {"a/top.py"}
    assert "a/b/c" in result.index.dirs and "a/b/c/d" not in result.index.dirs


def test_known_text_files_are_not_opened_while_scanning(tmp_path, monkeypatch):
    write(tmp_path, {"app.py": "x", "README.md": "x", "LICENSE": "x", ".env": "x", "blob.dat": b"a\x00b"})
    opened = []
    real_open = open

    def recording(path, *args, **kwargs):
        opened.append(os.path.basename(str(path)))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(scan_mod, "open", recording, raising=False)
    result = scan(tmp_path)
    assert result.binary == ["blob.dat"]
    assert [name for name in opened if name != ".gitignore"] == ["blob.dat"]  # the 4 text files are not opened


def test_names_that_cannot_be_shown_are_skipped(tmp_path):
    try:
        (tmp_path / "bad\udcff.txt").write_text("x", encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        pytest.skip("this file system cannot hold that name")
    write(tmp_path, {"ok.txt": "x"})
    assert scan(tmp_path).index.files == {"ok.txt"}


def test_missing_root_raises(tmp_path):
    with pytest.raises(NotADirectoryError):
        scan(tmp_path / "nope")


def test_index_never_reads_skipped_or_binary_files(tmp_path):
    # a huge installer script must not be loaded just to look for a server command in it
    write(tmp_path, {"run.sh": "uvicorn main:app\n" + "echo hi\n" * 50, "main.py": "app = 1\n",
                     "logo.png": b"\x89PNG\x00\x00", "notes.txt": "hello world"})
    result = scan(tmp_path, max_bytes=100)
    index = result.index
    assert result.skipped == ["run.sh"] and result.binary == ["logo.png"]
    assert index.read_text("run.sh") is None and index.read_text("logo.png") is None
    assert index.read_text("notes.txt", limit=5, cache=False) == "hello"  # at most `limit` bytes
    from codevision.lang import python_lang

    assert python_lang.project_entries(index) == {}  # run.sh was too big to read, so main.py is not named


def test_read_text_works_on_scanned_index(tmp_path):
    write(tmp_path, {"note.md": "안녕하세요\n"})
    result = scan(str(tmp_path))
    assert result.index.read_text("note.md") == "안녕하세요\n"


@pytest.mark.parametrize("raw", [
    b"\xef\xbb\xbfprint('hi')\n", b"\xef\xbb\xbf", b"\xef\xbb", b"\xef\xbb\xbf\xef\xbb\xbfx\n",  # BOMs
    "한글 = 1\n".encode("cp949"), b"\xef\xbb\xbf" + "한글".encode("cp949"),  # not UTF-8
    b"x = '\xff\xfe'\n", "가나다".encode("utf-8")[:-1], b"",  # neither: UTF-8 with stand-ins
])
def test_read_text_decodes_like_utf8_sig_then_cp949(tmp_path, raw):
    # UTF-8 (a BOM dropped), else cp949, else UTF-8 with stand-ins; the BOM is skipped by hand,
    # which reads faster than the "utf-8-sig" codec, so the answer must stay the same
    write(tmp_path, {"f.py": raw})
    expected = None
    for enc in ("utf-8-sig", "cp949"):
        try:
            expected = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if expected is None:
        expected = raw.decode("utf-8", "replace")
    assert scan(tmp_path).index.read_text("f.py", cache=False) == expected
