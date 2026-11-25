"""AIService additions for GitHub snapshots: roots_in_use(), forget_projects() and build_key, plus the neutral wording
now that a project can be a folder, the example or a GitHub repository. No model is asked here: the requests and
jobs are put in place by hand, the way the worker holds them. The server side is in test_github_server.py."""
import os
from pathlib import Path

import pytest

from codevision.explain import messages as M
from codevision.explain import service
from codevision.explain.base import AIError


def graph_for(root: Path) -> dict:
    return {"name": root.name, "root": ".", "nodes": {".": {"n": root.name, "p": root.name + "/", "t": "d", "k": []}}}


def norm(path: Path) -> Path:
    return Path(os.path.normcase(os.path.realpath(path)))


def task_for(project: service.Project, node: str = "a.py") -> service._Task:
    work = service._Work(node, "file", "0" * 40, None)
    return service._Task((project.key, node, "m", "file", "0" * 40), project, work, "m")


@pytest.fixture
def ai():
    return service.AIService(None, lambda root, demo: graph_for(root))


def test_roots_in_use_covers_projects_requests_and_jobs(ai, tmp_path):
    roots = {name: tmp_path / name for name in ("kept", "waiting", "asking", "job", "stopping", "done", "Mixed")}
    for root in roots.values():
        root.mkdir()
    ai.remember("kept", roots["kept"], graph_for(roots["kept"]))
    ai._pending.append(task_for(service.Project("waiting", roots["waiting"], {})))
    ai._running_task = task_for(service.Project("asking", roots["asking"], {}))
    jobs = {
        "job": service.Job("j1", service.Project("job", roots["job"], {}), "m", ["a.py"]),
        "stopping": service.Job("j2", service.Project("stopping", roots["stopping"], {}), "m", ["a.py"],
                                state="cancelled", current="a.py"),  # its item still ends in the worker
        "done": service.Job("j3", service.Project("done", roots["done"], {}), "m", ["a.py"], state="done"),
    }
    for job in jobs.values():
        ai._jobs[job.id] = job
    expected = {norm(roots[n]) for n in ("kept", "waiting", "asking", "job", "stopping")}
    if os.name == "nt":  # another spelling of the same folder is the same root there
        ai.remember("mixed", Path(str(roots["Mixed"]).upper()), {})
        expected.add(norm(roots["Mixed"]))
    assert ai.roots_in_use() == expected


def test_forget_projects_lets_go_of_only_those_projects(ai, tmp_path):
    gone, other = service.Project("github:o/gone@" + "a" * 40, tmp_path / "gone", {}), \
        service.Project("other", tmp_path / "other", {})
    for project in (gone, other):
        ai.remember(project.key, project.root, {})
    gone_job = service.Job("j1", gone, "m", ["a.py", "b.py"], results=[["a.py", {"s": "", "d": "", "f": []}]])
    other_job = service.Job("j2", other, "m", ["a.py"])
    ai._jobs.update({"j1": gone_job, "j2": other_job})
    waiting_gone, waiting_other, asking = task_for(gone), task_for(other), task_for(gone, "b.py")
    ai._pending.extend([waiting_gone, waiting_other])
    ai._running_task = asking

    assert ai.forget_projects(frozenset({gone.key})) == {"projects": 1, "jobs": 1, "tasks": 2}
    assert list(ai._projects) == ["other"]
    assert (gone_job.state, gone_job.cancel, gone_job.message) == \
        ("cancelled", True, M.JOB_PROJECT_CLOSED.format(n=1))
    assert other_job.state == "running"
    assert list(ai._pending) == [waiting_other] and not waiting_other.done.is_set()
    for task in (waiting_gone, asking):  # answered at once: nobody waits for the model on a closed project
        assert task.done.is_set()
        assert (task.error.code, task.error.status, task.error.message) == ("not_found", 404, M.PROJECT_CLOSED)
    assert ai.forget_projects(set()) == ai.forget_projects(None) == {"projects": 0, "jobs": 0, "tasks": 0}


def test_a_job_item_in_flight_on_a_forgotten_project_is_dropped(ai, tmp_path):
    project = service.Project("github:o/r@" + "b" * 40, tmp_path, {"nodes": {"a.py": {"t": "f", "f": []}}})
    job = service.Job("j1", project, "m", ["a.py"], current="a.py", pos=1)
    ai._jobs["j1"] = job
    ai.forget_projects({project.key})
    assert ai.roots_in_use() == {norm(tmp_path)}  # the worker may still read it until the item ends
    ai._prepare = lambda *args, **kwargs: ({"s": "late", "d": "", "f": []}, None)
    ai._do_job_item(job, "a.py")  # the answer comes back after the cancel
    assert (job.state, job.results, job.current) == ("cancelled", [], "")
    assert ai.roots_in_use() == set()


def test_build_key_rebuilds_a_project_and_its_errors_pass_through(tmp_path):
    built, keyed = [], []

    def build_key(key, root, demo):
        keyed.append((key, root, demo))
        if key == "gone":
            raise AIError("not_found", "받아 둔 저장소를 찾지 못했어요.")
        if key == "broken":
            raise RuntimeError("the analyzer broke")
        return {"name": "by key", "nodes": {}}

    ai = service.AIService(None, lambda root, demo: built.append(root) or graph_for(root), build_key=build_key)
    assert ai.project("github:o/r@" + "c" * 40, tmp_path).graph["name"] == "by key"
    assert keyed == [("github:o/r@" + "c" * 40, tmp_path, False)] and built == []
    with pytest.raises(AIError) as caught:
        ai.project("gone", tmp_path)
    assert (caught.value.code, caught.value.status, caught.value.message) == \
        ("not_found", 404, "받아 둔 저장소를 찾지 못했어요.")  # not turned into "failed"
    with pytest.raises(AIError) as caught:
        ai.project("broken", tmp_path)
    assert (caught.value.code, caught.value.message) == ("failed", M.ANALYSIS_FAILED)
    plain = service.AIService(None, lambda root, demo: built.append(root) or graph_for(root))
    assert plain.project("x", tmp_path).graph["name"] == tmp_path.name and built == [tmp_path]


def test_error_logs_name_no_folder_or_file_of_a_github_snapshot(tmp_path, caplog):
    snap = tmp_path / "repos" / "0123456789abcdef" / ("c" * 12) / "secret-diary"
    snap.mkdir(parents=True)

    def build_key(key, root, demo):
        raise RuntimeError("the analyzer broke")

    ai = service.AIService(None, lambda root, demo: graph_for(root), build_key=build_key)
    with pytest.raises(AIError):
        ai.project("github:octocat/secret-diary@" + "c" * 40, snap)
    with pytest.raises(AIError):
        ai.project("plain", tmp_path)  # a folder of the user's own is still named
    lines = [r.getMessage() for r in caplog.records]
    assert not [x for x in lines if "secret-diary" in x] and [x for x in lines if str(tmp_path) in x]
    assert service._log_name("github:o/r@" + "c" * 40, "notes/plan.md") == "(a GitHub snapshot)"


def test_texts_about_any_project_say_project_not_folder():
    for text in (M.NODE_NOT_FOUND, M.FILE_UNREADABLE, M.JOB_WAITING, M.ANALYSIS_FAILED, M.PROJECT_CLOSED,
                 M.JOB_PROJECT_CLOSED):
        assert "프로젝트" in text and "폴더" not in text, text
    assert M.JOB_PROJECT_CLOSED.format(n=1234) == "프로젝트가 닫혀서 멈췄어요. 그때까지 만든 설명 1,234개는 저장해 두었어요."
