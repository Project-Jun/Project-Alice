"""github/snapshot.py: the snapshot cache under <data_dir>/repos. Archives are built here, in memory."""
import gzip
import hashlib
import io
import json
import os
import tarfile
import threading

import pytest

from codevision.github import extract, snapshot
from codevision.github.extract import extract_stream
from codevision.github.snapshot import NOTES, SnapshotStore, leaf_name, notes, repo_key

SHA1, SHA2, SHA3, SHA4 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)
NOW = 1_800_000_000.0


class Clock:
    """Wall-clock double: sleep() moves it on, so retry loops end without real waiting."""

    def __init__(self):
        self.t = NOW
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def tarball(files: dict, sha: str, top="octo-hello-x") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": sha}) as tf:
        for name in [top] + sorted({f"{top}/{p.rsplit('/', 1)[0]}" for p in files if "/" in p}):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for rel, data in files.items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), mtime=0)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return SnapshotStore(lambda: tmp_path / "home", clock=clock, sleep=clock.sleep)


def make(store, owner="Octo", repo="Hello", sha=SHA1, *, private=False, user_id=None, branch="main",
         files=None, default_branch="main"):
    stg = store.staging(owner, repo, sha)
    data = tarball(files or {"README.md": b"# hello\n", "src/app.py": b"print(1)\n"}, sha)
    result = extract_stream(io.BytesIO(data), stg.dest, final_root=stg.final_root, expect_commit=sha)
    return store.publish(stg, result, private=private, user_id=user_id, branch=branch, default_branch=default_branch)


def repos(tmp_path):
    return tmp_path / "home" / "repos"


# --- layout and lookup --------------------------------------------------------------------------------------------

def test_repo_key_is_16_hex_of_sha256_of_the_lowercase_names():
    assert repo_key("Octo", "Hello") == hashlib.sha256(b"octo/hello").hexdigest()[:16]
    assert repo_key("OCTO", "hello") == repo_key("octo", "HELLO")


def test_publish_lays_out_the_short_path_and_find_returns_it(store, tmp_path):
    snap = make(store, branch="main")
    folder = repos(tmp_path) / repo_key("octo", "hello") / SHA1[:12]
    assert snap.root == folder / "Hello" and snap.folder == folder
    assert (snap.root / "README.md").read_bytes() == b"# hello\n"
    assert (snap.owner, snap.repo, snap.sha, snap.private, snap.display) == ("Octo", "Hello", SHA1, False, "Hello")
    assert (snap.branch, snap.created, snap.opened, snap.size) == ("main", NOW, NOW, len(b"# hello\nprint(1)\n"))
    found = store.find("octo", "HELLO", SHA1)
    assert found is not None and found.root == snap.root and found.notes == []
    assert sorted(os.listdir(repos(tmp_path))) == [".partial", repo_key("octo", "hello")]
    assert os.listdir(repos(tmp_path) / ".partial") == []
    assert sorted(os.listdir(folder.parent)) == [SHA1[:12], "refs.json"]
    assert sorted(os.listdir(folder)) == ["Hello", "snapshot.json"]


def test_snapshot_json_holds_the_agreed_fields_and_no_machine_path(store, tmp_path):
    snap = make(store, private=True, user_id=42, branch="feature/x")
    text = (snap.folder / "snapshot.json").read_text(encoding="utf-8")
    meta = json.loads(text)
    for key in ("owner", "repo", "sha", "private", "user_id", "branch", "created", "opened"):
        assert key in meta
    assert (meta["private"], meta["user_id"], meta["branch"]) == (True, 42, "feature/x")
    assert str(tmp_path) not in text and str(tmp_path).replace("\\", "\\\\") not in text
    assert meta["skipped"]["bad_name"] == 0 and "examples" in meta


def test_find_refuses_other_commits_broken_files_and_a_missing_leaf(store, tmp_path):
    snap = make(store)
    assert store.find("Octo", "Hello", SHA2) is None
    assert store.find("Octo", "Other", SHA1) is None
    assert store.find("Octo", "Hello", "not-a-sha") is None and store.find("../x", "Hello", SHA1) is None
    meta_path = snap.folder / "snapshot.json"
    good = meta_path.read_text(encoding="utf-8")
    meta_path.write_text("{broken", encoding="utf-8")
    assert store.find("Octo", "Hello", SHA1) is None
    meta_path.write_text(good.replace(SHA1, SHA1[:12] + "f" * 28), encoding="utf-8")  # another full sha
    assert store.find("Octo", "Hello", SHA1) is None
    meta_path.write_text(good, encoding="utf-8")
    (snap.root / "README.md").unlink()
    (snap.root / "src" / "app.py").unlink()
    (snap.root / "src").rmdir()
    snap.root.rmdir()
    assert store.find("Octo", "Hello", SHA1) is None


def test_staging_and_trash_paths_are_never_longer_than_the_final_ones(store):
    stg = store.staging("Octo", "Hello", SHA1)
    assert stg.dest.parent == stg.root and stg.root.parent.name == ".partial"
    assert len(str(stg.dest)) < len(str(stg.final_root))
    final_part = f"{repo_key('octo', 'hello')}/{SHA1[:12]}"
    assert len(f".partial/{stg.root.name}") <= len(final_part) and len(".delete/" + "0" * 12) <= len(final_part)
    assert not stg.dest.exists()  # extract_stream creates it


@pytest.mark.parametrize("repo, leaf", [
    ("Hello", "Hello"), ("CON", "repo"), ("aux.js", "repo"), ("nul", "repo"), ("x.", "repo"),
    ("snapshot.json", "repo"), ("Snapshot.JSON.bak", "repo"), ("repo", "repo"), (".github", ".github"),
])
def test_leaf_name_keeps_the_repo_name_unless_windows_cannot_use_it(repo, leaf):
    assert leaf_name(repo) == (leaf, repo)


def test_a_reserved_repo_name_gets_a_safe_leaf_and_keeps_its_display_name(store):
    snap = make(store, repo="CON")
    assert snap.root.name == "repo" and snap.display == "CON"
    assert store.find("octo", "con", SHA1).display == "CON"


def test_a_data_folder_too_deep_for_the_repo_name_falls_back_then_refuses(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(extract, "long_paths_enabled", lambda: False)
    tail = 1 + len("repos") + 1 + 16 + 1 + 12 + 1  # what follows the data folder, before the leaf
    room = 247 - tail
    home = tmp_path / ("d" * max(1, room - len(str(tmp_path)) - 1 - 20))  # 20 units left for the leaf
    st = SnapshotStore(lambda: home, clock=clock, sleep=clock.sleep)
    long_repo = "a" * 40
    stg = st.staging("Octo", long_repo, SHA1)
    assert stg.leaf == "repo" and stg.display == long_repo
    assert extract.utf16_len(str(stg.final_root)) <= 247
    deeper = SnapshotStore(lambda: home / ("e" * 30), clock=clock, sleep=clock.sleep)
    with pytest.raises(OSError):
        deeper.staging("Octo", long_repo, SHA1)


def test_bad_names_and_shas_are_refused(store):
    for owner, repo, sha in (("../x", "Hello", SHA1), ("Octo", "a/b", SHA1), ("Octo", "Hello", "abc"),
                             ("Octo", "Hello", "A" * 40), ("", "Hello", SHA1), ("Octo", "a\\b", SHA1)):
        with pytest.raises(ValueError):
            store.staging(owner, repo, sha)


def test_the_data_folder_is_read_on_every_call(tmp_path, clock):
    home = [tmp_path / "one"]
    st = SnapshotStore(lambda: home[0], clock=clock, sleep=clock.sleep)
    make(st)
    home[0] = tmp_path / "two"
    assert st.find("Octo", "Hello", SHA1) is None and st.repos_dir() == tmp_path / "two" / "repos"
    assert st.free_bytes() > 0


# --- publish ----------------------------------------------------------------------------------------------------

def test_publishing_the_same_commit_twice_keeps_the_first(store, tmp_path):
    first = make(store, files={"a.txt": b"first"})
    second = make(store, files={"a.txt": b"second"})
    assert second.root == first.root and (first.root / "a.txt").read_bytes() == b"first"
    assert os.listdir(repos(tmp_path) / ".partial") == []
    assert len(store.snapshots()) == 1


def test_two_threads_publishing_one_commit_both_get_it(store, tmp_path):
    stagings = [store.staging("Octo", "Hello", SHA1) for _ in range(2)]
    results = [extract_stream(io.BytesIO(tarball({"a.txt": b"x"}, SHA1)), s.dest, final_root=s.final_root)
               for s in stagings]
    barrier, out, errors = threading.Barrier(2), [], []

    def go(i):
        barrier.wait()
        try:
            out.append(store.publish(stagings[i], results[i], private=False, branch="main"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [] and len(out) == 2 and out[0].root == out[1].root
    assert len(store.snapshots()) == 1 and os.listdir(repos(tmp_path) / ".partial") == []


def test_publish_retries_while_the_antivirus_holds_the_new_files(store, clock, monkeypatch):
    real = os.replace
    fails = [5]

    def busy_replace(src, dst):
        if os.path.isdir(src) and fails[0]:
            fails[0] -= 1
            raise PermissionError(13, "in use")
        return real(src, dst)

    monkeypatch.setattr(snapshot.os, "replace", busy_replace)
    snap = make(store)
    assert fails[0] == 0 and snap.root.is_dir() and len(clock.sleeps) >= 5


def test_publish_gives_up_after_about_30_seconds(store, clock, monkeypatch, tmp_path):
    real = os.replace

    def always_busy(src, dst):
        if os.path.isdir(src) and ".partial" in str(src):
            raise PermissionError(13, "in use")
        return real(src, dst)

    monkeypatch.setattr(snapshot.os, "replace", always_busy)
    with pytest.raises(PermissionError):
        make(store)
    assert 30 <= sum(clock.sleeps) <= 40
    assert store.find("Octo", "Hello", SHA1) is None


def test_another_logins_private_copy_is_replaced(store):
    make(store, private=True, user_id=1, files={"a.txt": b"one"})
    snap = make(store, private=True, user_id=2, files={"a.txt": b"two"})
    assert snap.user_id == 2 and (snap.root / "a.txt").read_bytes() == b"two"
    assert len(store.snapshots()) == 1


def test_a_half_deleted_folder_in_the_way_is_replaced(store, tmp_path):
    folder = repos(tmp_path) / repo_key("octo", "hello") / SHA1[:12]
    (folder / "Hello").mkdir(parents=True)
    (folder / "Hello" / "left.txt").write_text("x")  # no snapshot.json: a delete stopped midway
    snap = make(store)
    assert (snap.root / "README.md").exists() and not (snap.root / "left.txt").exists()


def test_publish_refuses_a_user_id_it_could_not_read_back(store):
    stg = store.staging("Octo", "Hello", SHA1)
    result = extract_stream(io.BytesIO(tarball({"a.txt": b"x"}, SHA1)), stg.dest, final_root=stg.final_root)
    for bad in ("42", True, 4.2):
        with pytest.raises(ValueError):
            store.publish(stg, result, private=True, user_id=bad, branch="main")
    assert store.publish(stg, result, private=True, user_id=42, branch="main").user_id == 42


def test_discard_throws_a_download_away(store, tmp_path):
    stg = store.staging("Octo", "Hello", SHA1)
    extract_stream(io.BytesIO(tarball({"a.txt": b"x"}, SHA1)), stg.dest, final_root=stg.final_root)
    store.discard(stg)
    assert not stg.root.exists() and store.find("Octo", "Hello", SHA1) is None


# --- refs.json and opened times -----------------------------------------------------------------------------------

def test_record_keeps_branches_privacy_and_the_default_branch(store, tmp_path, clock):
    assert store.refs("Octo", "Hello") == {}
    store.record("Octo", "Hello", branch="main", sha=SHA1, private=True, default_branch="main")
    clock.t += 5
    refs = store.record("octo", "hello", branch="dev/한글", sha=SHA2, private=True)
    assert refs["branches"] == {"main": {"sha": SHA1, "checked_at": NOW}, "dev/한글": {"sha": SHA2, "checked_at": NOW + 5}}
    assert (refs["private"], refs["default_branch"]) == (True, "main")
    assert store.refs("OCTO", "HELLO") == refs
    folder = repos(tmp_path) / repo_key("octo", "hello")
    assert sorted(os.listdir(folder)) == ["refs.json"]  # no temporary files left behind
    (folder / "refs.json").write_text("[1, 2", encoding="utf-8")
    assert store.refs("Octo", "Hello") == {}


def test_record_refuses_bad_branches_and_shas(store):
    for branch, sha in (("", SHA1), ("x" * 256, SHA1), ("bad\udc80", SHA1), ("ctrl\x01", SHA1), ("main", "zz")):
        with pytest.raises(ValueError):
            store.record("Octo", "Hello", branch=branch, sha=sha, private=False)


def test_publish_records_the_branch(store):
    make(store, branch="main")
    assert store.refs("Octo", "Hello")["branches"]["main"]["sha"] == SHA1


def test_opened_updates_the_time(store, clock):
    make(store)
    clock.t += 1000
    store.opened("octo", "hello", SHA1)
    assert store.find("Octo", "Hello", SHA1).opened == NOW + 1000
    store.opened("octo", "hello", SHA2)  # not there: nothing happens


# --- cleanup ----------------------------------------------------------------------------------------------------

def test_prune_keeps_current_in_use_and_recently_opened_snapshots(store, clock):
    for sha in (SHA2, SHA3, SHA4):
        make(store, sha=sha, branch="main")  # each publish prunes, but the earlier ones are still recent
    assert len(store.snapshots()) == 3
    clock.t += 3600
    store.opened("Octo", "Hello", SHA4)
    in_use = store.find("Octo", "Hello", SHA3)
    store.record("Octo", "Hello", branch="main", sha=SHA1, private=False)  # main moved on
    store.record("Octo", "Hello", branch="dev", sha=SHA2, private=False)
    # SHA2 is dev's head, SHA3 is in use, SHA4 was opened just now
    assert store.prune_repo("Octo", "Hello", in_use=lambda: {in_use.root}) == 0
    clock.t += 3600
    assert store.prune_repo("Octo", "Hello") == 2
    assert store.find("Octo", "Hello", SHA2) is not None
    assert store.find("Octo", "Hello", SHA3) is None and store.find("Octo", "Hello", SHA4) is None


def test_prune_without_refs_and_when_in_use_fails_deletes_nothing(store, clock, tmp_path):
    make(store, sha=SHA1, branch="main")
    make(store, sha=SHA2, branch="main")
    clock.t += 3600
    (repos(tmp_path) / repo_key("octo", "hello") / "refs.json").unlink()
    assert store.prune_repo("Octo", "Hello") == 0

    def broken():
        raise RuntimeError("AI side busy")

    store.record("Octo", "Hello", branch="main", sha=SHA2, private=False)
    assert store.prune_repo("Octo", "Hello", in_use=broken) == 0
    assert store.enforce_cap(in_use=broken, max_count=0) == 0
    assert len(store.snapshots()) == 2


def test_the_global_cap_deletes_the_least_recently_opened_first(store, clock):
    for i, sha in enumerate((SHA1, SHA2, SHA3, SHA4)):
        make(store, repo=f"Repo{i}", sha=sha)
        clock.t += 100
    clock.t += 3600
    store.opened("Octo", "Repo0", SHA1)  # the oldest is now the most recently opened
    clock.t += 3600
    assert store.enforce_cap(max_count=2) == 2
    left = sorted(s.repo for s in store.snapshots())
    assert left == ["Repo0", "Repo3"]
    size = store.snapshots()[0].size
    assert store.enforce_cap(max_bytes=size) == 1 and [s.repo for s in store.snapshots()] == ["Repo0"]


def test_the_cap_spares_snapshots_in_use_or_opened_recently(store, clock):
    a = make(store, repo="A", sha=SHA1)
    make(store, repo="B", sha=SHA2)
    assert store.enforce_cap(max_count=0) == 0  # both were just published
    clock.t += 3600
    assert store.enforce_cap(in_use=lambda: [a.root], max_count=0) == 1
    assert [s.repo for s in store.snapshots()] == ["A"]


def test_publish_enforces_the_global_cap(store, clock, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOTS", 2)
    for i, sha in enumerate((SHA1, SHA2, SHA3)):
        make(store, repo=f"R{i}", sha=sha)
        clock.t += 3600
    assert sorted(s.repo for s in store.snapshots()) == ["R1", "R2"]


def test_the_sweep_removes_old_partial_folders_only(store, tmp_path, clock):
    old = store.staging("Octo", "Hello", SHA1)
    young = store.staging("Octo", "Hello", SHA2)
    (old.root / "x.txt").write_text("x")
    os.utime(old.root, (NOW - 7200, NOW - 7200))
    os.utime(young.root, (NOW - 60, NOW - 60))
    out = store.sweep()
    assert out["partial"] == 1 and not old.root.exists() and young.root.exists()


def test_the_sweep_clears_trash_tombstones_and_broken_folders(store, tmp_path):
    snap = make(store)
    trash = repos(tmp_path) / ".delete" / ("a" * 12)
    (trash / "deep").mkdir(parents=True)
    (trash / "deep" / "f.txt").write_text("x")
    broken = repos(tmp_path) / repo_key("x", "y") / ("b" * 12)
    (broken / "Y").mkdir(parents=True)
    tomb = repos(tmp_path) / repo_key("p", "q") / ("c" * 12)
    tomb.mkdir(parents=True)
    (repos(tmp_path) / ".delete.json").write_text(json.dumps({"v": 1, "paths": [
        f"{repo_key('p', 'q')}/{'c' * 12}", "../../outside", "C:/Windows"]}))
    out = store.sweep()
    assert not trash.exists() and not broken.exists() and not tomb.exists()
    assert not (repos(tmp_path) / ".delete.json").exists()
    assert out["trash"] == 1 and out["broken"] >= 1 and out["failed"] == 0
    assert store.find("Octo", "Hello", SHA1).root == snap.root


def test_a_delete_renames_into_the_trash_first(store, tmp_path, monkeypatch):
    snap = make(store)
    monkeypatch.setattr(snapshot, "_rmtree", lambda path, *a, **k: False)  # the antivirus holds every file
    assert store.delete(snap) is False
    assert not snap.folder.exists() and store.find("Octo", "Hello", SHA1) is None
    left = os.listdir(repos(tmp_path) / ".delete")
    assert len(left) == 1 and len(left[0]) == 12
    monkeypatch.undo()
    assert store.sweep()["trash"] == 1 and os.listdir(repos(tmp_path) / ".delete") == []


def test_a_delete_that_cannot_rename_removes_snapshot_json_first_and_leaves_a_tombstone(store, tmp_path, monkeypatch):
    snap = make(store)
    real_replace, real_rmtree = os.replace, snapshot._rmtree

    def no_trash(src, dst):
        if ".delete" in str(dst) and os.path.isdir(src):
            raise PermissionError(13, "a file is open")
        return real_replace(src, dst)

    monkeypatch.setattr(snapshot.os, "replace", no_trash)
    monkeypatch.setattr(snapshot, "_rmtree", lambda path, *a, **k: False)
    assert store.delete(snap) is False
    assert snap.folder.exists() and not (snap.folder / "snapshot.json").exists()
    assert store.find("Octo", "Hello", SHA1) is None
    tombs = json.loads((repos(tmp_path) / ".delete.json").read_text())["paths"]
    assert tombs == [f"{repo_key('octo', 'hello')}/{SHA1[:12]}"]
    monkeypatch.setattr(snapshot, "_rmtree", real_rmtree)
    store.sweep()
    assert not snap.folder.exists() and not (repos(tmp_path) / ".delete.json").exists()


def test_rmtree_retries_a_file_in_use_and_clears_read_only(tmp_path):
    folder = tmp_path / "t"
    (folder / "sub").mkdir(parents=True)
    ro = folder / "sub" / "ro.txt"
    ro.write_text("x")
    os.chmod(ro, 0o444)
    assert snapshot._rmtree(folder, sleep=lambda s: None) and not folder.exists()


def test_logout_deletes_private_snapshots_and_their_refs_and_keeps_public_ones(store, tmp_path, clock):
    public = make(store, owner="Octo", repo="Public", sha=SHA1, branch="main")
    mine = make(store, owner="Me", repo="Secret", sha=SHA2, private=True, user_id=1, branch="main")
    other = make(store, owner="Org", repo="Hidden", sha=SHA3, private=True, user_id=2, branch="dev")
    out = store.delete_private()
    assert out == {"snapshots": 2, "failed": 0}
    assert store.find("Octo", "Public", SHA1).root == public.root
    assert store.find("Me", "Secret", SHA2) is None and store.find("Org", "Hidden", SHA3) is None
    assert store.refs("Me", "Secret") == {} and store.refs("Org", "Hidden") == {}
    assert store.refs("Octo", "Public")["branches"]["main"]["sha"] == SHA1
    assert not mine.folder.parent.exists() and not other.folder.parent.exists()


def test_an_account_switch_deletes_only_the_other_logins_private_snapshots(store):
    make(store, owner="Me", repo="Secret", sha=SHA1, private=True, user_id=1)
    make(store, owner="Me", repo="Secret", sha=SHA2, private=True, user_id=2, branch="dev")
    make(store, owner="Org", repo="Hidden", sha=SHA3, private=True, user_id=1)
    out = store.delete_private(keep_user_id=2)
    assert out == {"snapshots": 2, "failed": 0}
    assert store.find("Me", "Secret", SHA2).user_id == 2 and store.find("Me", "Secret", SHA1) is None
    assert store.refs("Me", "Secret") != {} and store.refs("Org", "Hidden") == {}


def test_a_private_snapshot_opens_only_for_its_login(store):
    public = make(store, repo="P", sha=SHA1)
    private = make(store, repo="S", sha=SHA2, private=True, user_id=7)
    assert public.allowed(None) and public.allowed(3)
    assert private.allowed(7) and not private.allowed(None) and not private.allowed(8)
    assert not snapshot.Snapshot("o", "r", SHA1, True, private.root, "r", user_id=None).allowed(None)


# --- notes ------------------------------------------------------------------------------------------------------

def test_the_note_texts():
    assert NOTES["lfs"] == "LFS로 올린 파일 {n}개는 내용 없이 이름만 보여요."
    assert NOTES["submodules"] == "하위 저장소(submodule) {n}개는 GitHub 압축 파일에 내용이 없어서 빈 폴더로 보여요."
    assert NOTES["blocked"] == "보안 프로그램이 막은 파일 {n}개는 받지 않았어요."
    meta = {"skipped": {"lfs": 1234, "bad_name": 1, "too_long": 2, "collision": 3, "blocked": 1, "submodules": 2,
                        "binary": 50, "capped": 3, "links": 4, "other_top": 1, "ignored": 60000},
            "examples": {"bad_name": ["src/CON.js"], "collision": ["README.md", "docs/A.md", "x"]}}
    assert notes(meta) == [
        "LFS로 올린 파일 1,234개는 내용 없이 이름만 보여요.",
        "하위 저장소(submodule) 2개는 GitHub 압축 파일에 내용이 없어서 빈 폴더로 보여요.",
        "Windows에서 쓸 수 없는 이름의 파일 1개(src/CON.js)는 받지 않았어요.",
        "경로가 너무 길어서 Windows에 만들 수 없는 파일 2개는 받지 않았어요.",
        "대소문자만 다른 같은 이름의 파일 3개(README.md, docs/A.md 등)는 하나만 받았어요.",
        "보안 프로그램이 막은 파일 1개는 받지 않았어요.",
    ]


def test_notes_ignore_zero_missing_and_odd_values():
    assert notes({}) == [] and notes({"skipped": "x"}) == [] and notes(None) == []
    meta = {"skipped": {"lfs": 0, "bad_name": True, "too_long": -1, "collision": "3", "blocked": 2.5},
            "examples": {"bad_name": "not a list"}}
    assert notes(meta) == []
    two = notes({"skipped": {"bad_name": 2}, "examples": {"bad_name": ["a{n}", "b\x00"]}})
    assert two == ["Windows에서 쓸 수 없는 이름의 파일 2개(a{n}, b\ufffd)는 받지 않았어요."]
    none = notes({"skipped": {"collision": 5}, "examples": {}})
    assert none == ["대소문자만 다른 같은 이름의 파일 5개는 하나만 받았어요."]


def test_a_hostile_archive_gives_its_notes_on_the_snapshot(store):
    lfs = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"4" * 64 + b"\nsize 9\n"
    snap = make(store, files={"CON": b"x", "q?.txt": b"x", "trail.": b"x", "README.md": b"a", "readme.md": b"b",
                              "model.bin": lfs, "ok.txt": b"ok"})
    assert snap.notes == [
        "LFS로 올린 파일 1개는 내용 없이 이름만 보여요.",
        "Windows에서 쓸 수 없는 이름의 파일 3개(CON, q?.txt 등)는 받지 않았어요.",
        "대소문자만 다른 같은 이름의 파일 1개(readme.md)는 하나만 받았어요.",
    ]
    assert store.find("Octo", "Hello", SHA1).notes == snap.notes
