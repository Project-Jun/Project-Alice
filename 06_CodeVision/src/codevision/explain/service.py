"""Local AI explanations for one CodeVision server.

- status(): the block of GET /api/ai/status and graph["ai"]. /api/analyze reuses it for a
  few seconds and asks Ollama while the folder is being analyzed; GET /api/ai/status asks
  Ollama every time (status(fresh=True)), so an Ollama started or stopped outside
  CodeVision shows at once. Fresh requests that come together share one probe.
- annotate(): /api/analyze adds graph["ai"], and while AI explanations are on node["x"]
  from the cache and node["xs"] for code files the AI never gets (MARKS). It never
  calls the model.
- explain(), explain_all(), job(), cancel_job(): one worker thread makes every model
  call, one at a time. On-demand requests go ahead of explain-all items.
- Turning AI explanations off, or closing the server, lets Ollama free the memory of
  the models this server used (unload_models), so the GPU is free for other programs.
- A few recently analyzed projects (graph and root) stay in memory, least recently
  used out first, so requests know the node facts. Others are analyzed again, through
  build_key(key, root, demo) when the server gives one (a GitHub snapshot's key, so the
  rebuilt graph has the same root name and cache keys as the page's).
- roots_in_use(): the roots that the projects in memory, the waiting and running requests
  and the running jobs read, so the GitHub snapshot cleanup never deletes one of them.
- forget_projects(keys): the server's logout hook. Those projects leave memory, their jobs
  stop and their waiting requests are answered (not_found) at once.

node["x"] is {"s", "d", "f": [[signature, text], ...]}: f lists the node's own
signatures in the node's order, with the rule-based text where the AI gave none.
The rule-based s, d and f are never changed.

node["xs"] says why the AI never gets a code file: "secret" (a name that may hold
secrets), "too_big" (over MAX_SOURCE_BYTES) or "unreadable" (gone, locked, or through
a link). Such a node has no x; the screen leaves it out of the 전체 설명 estimate and
shows messages.MARK_NOTES for it instead of asking.
"""
from __future__ import annotations

import collections
import copy
import itertools
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import messages as M
from . import ollama, prompts, providers, safety
from .base import AIError, Explanation, FileContext, FolderContext
from .cache import ExplanationCache, blob_sha

PROVIDER = "local"        # the provider part of the cache key
STATUS_TTL = 3.0          # seconds a status block is reused
PROBE_CONNECT_TIMEOUT = 1.0
MAX_PROJECTS = 3          # analyzed projects kept in memory
MAX_PENDING = 6           # on-demand requests that may wait for the worker
MAX_JOBS = 10             # finished jobs kept for GET /api/ai/job
MAX_ERRORS_IN_A_ROW = 5   # a job stops after this many failed items in a row
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")
SKIPPED = ("secret", "not_code", "too_big", "not_found")  # job items that are passed over, not errors
MARKS = tuple(M.MARK_NOTES)  # the values of node["xs"]: "secret", "too_big", "unreadable"

log = logging.getLogger(__name__)


def _max_source_bytes() -> int:
    from codevision import explain  # read when used, so explain.MAX_SOURCE_BYTES can be changed
    return explain.MAX_SOURCE_BYTES


def _depth(node_id: str) -> int:
    return 0 if node_id == "." else node_id.count("/") + 1


def decode_source(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def code_file(node) -> bool:
    return isinstance(node, dict) and node.get("t") == "f" and bool(node.get("lang"))


# --- models -----------------------------------------------------------------------------

def sort_models(models: list[dict]) -> list[dict]:
    """Recommended models first (prompts.RECOMMENDED_MODELS order), then the rest by name."""
    rank = {name: i for i, name in enumerate(prompts.RECOMMENDED_MODELS)}
    return sorted(models, key=lambda m: (rank.get(m["name"], len(rank)), m["name"].lower()))


def auto_model(names: list[str]) -> str:
    """The model to use when the user has not chosen one."""
    for wanted in prompts.RECOMMENDED_MODELS:
        if wanted in names:
            return wanted
        if ":" not in wanted and f"{wanted}:latest" in names:
            return f"{wanted}:latest"
    for name in names:
        if "embed" not in name.lower():  # embedding models cannot write text
            return name
    return ""


# --- the status block ---------------------------------------------------------------------

def compute_status(settings: providers.AISettings | None = None) -> dict:
    """The status block, asking Ollama now: {"provider", "enabled", "available", "model", "models",
    "recommended", "ollama", "can_start", "message"}."""
    s = settings or providers.ai_settings()
    install = ollama.find_ollama()
    running, models, listed = False, [], True
    if not s.url_error:
        try:
            models = ollama.OllamaClient(s.base_url, connect_timeout=PROBE_CONNECT_TIMEOUT).tags()
            running = True
        except ollama.OllamaUnreachable:
            running = False
        except ollama.OllamaError:  # something answers there, but gives no model list (too slow, an error)
            running, listed = True, False
    models = sort_models(models)
    names = [m["name"] for m in models]
    model = s.model or (auto_model(names) if running else "")
    local = s.provider == "local"
    enabled = local and s.enabled
    if s.provider in providers.PLANNED_PROVIDERS:
        message = providers.PLANNED_PROVIDERS[s.provider]
    elif not local:
        message = M.NONE
    elif not s.enabled:
        message = M.OFF
    elif s.url_error:
        message = s.url_error
    elif not running:
        message = M.STOPPED if install else M.MISSING
    elif not listed:
        message = M.NO_ANSWER  # not "no models": the list is unknown, not empty
    elif not names:
        message = M.NO_MODELS
    elif model not in names:
        message = M.MODEL_MISSING.format(model=model)
    else:
        message = M.READY.format(model=model)
    return {
        "provider": "local" if local else "none",
        "enabled": enabled,
        "available": bool(enabled and running and model and model in names),
        "model": model,
        "models": models,
        "recommended": list(prompts.RECOMMENDED_MODELS),
        "ollama": "running" if running else ("stopped" if install else "missing"),
        "can_start": bool(install is not None and not running and not s.url_error),
        "message": message,
    }


# --- node facts -> prompt contexts and x -----------------------------------------------------

def _bare(signature: str) -> str:
    name = signature.strip().strip("`").strip()
    for prefix in ("export default ", "export ", "async ", "def ", "function ", "class "):
        if name.startswith(prefix):
            name = name[len(prefix):].lstrip()
    return name.split("(", 1)[0].strip()


def _texts_for(signatures: list[str], answer: dict) -> dict[str, str]:
    """The AI text for each signature: the exact key first, then a key with the same bare name
    ("get_todos" for "get_todos()") when that name is unique on both sides."""
    out = {}
    for sig in signatures:
        text = answer.get(sig)
        if isinstance(text, str) and text.strip():
            out[sig] = text.strip()
    rest = [sig for sig in signatures if sig not in out]
    if not rest:
        return out
    ours = collections.Counter(_bare(sig) for sig in signatures)
    theirs = collections.Counter(_bare(key) for key in answer)
    loose = {_bare(key): text.strip() for key, text in answer.items()
             if key not in out and theirs[_bare(key)] == 1 and isinstance(text, str) and text.strip()}
    for sig in rest:
        bare = _bare(sig)
        if bare and ours[bare] == 1 and bare in loose:
            out[sig] = loose[bare]
    return out


def make_x(node: dict, exp: Explanation) -> dict:
    """node["x"] for an explanation: the node's own signatures, the rule text where the AI gave none."""
    rule_f = [item for item in node.get("f") or [] if isinstance(item, (list, tuple)) and item]
    signatures = [str(item[0]) for item in rule_f]
    texts = _texts_for(signatures, exp.f or {})
    f = [[sig, texts.get(sig) or (str(item[1]) if len(item) > 1 and item[1] is not None else "")]
         for sig, item in zip(signatures, rule_f)]
    return {"s": exp.s or str(node.get("s") or ""), "d": exp.d or str(node.get("d") or ""), "f": f}


def folder_context(nodes: dict, node_id: str, xs: dict) -> FolderContext:
    """A folder's prompt facts: each child with its AI summary when there is one, else its rule summary."""
    node = nodes[node_id]
    children = []
    for child_id in node.get("k") or []:
        child = nodes.get(child_id)
        if not isinstance(child, dict):
            continue
        summary = (xs.get(child_id) or child).get("s") or ""
        children.append((str(child.get("n") or child_id), str(summary)))
    return FolderContext(rel_path="" if node_id == "." else node_id, name=str(node.get("n") or ""),
                         summary_hint=str(node.get("s") or ""), children=children)


def folder_key(ctx: FolderContext) -> str:
    """A folder's cache key: made from its facts and children's summaries, so it changes with them."""
    return blob_sha(json.dumps(asdict(ctx), ensure_ascii=False, sort_keys=True).encode("utf-8"))


def file_context(node_id: str, node: dict, data: bytes) -> tuple[FileContext, dict]:
    """A file's prompt facts with obvious secrets masked, and {masked signature: real signature}."""
    signatures = [str(item[0]) for item in node.get("f") or [] if isinstance(item, (list, tuple)) and item]
    masked = [safety.mask_secrets(sig) for sig in signatures]
    ctx = FileContext(
        rel_path=node_id,
        language=str(node.get("lang") or ""),
        source=safety.mask_secrets(decode_source(data)),
        summary_hint=safety.mask_secrets(str(node.get("s") or "")),
        symbols=masked,
        uses=[str(u) for u in node.get("u") or []],
        used_by=[str(b) for b in node.get("b") or []],
    )
    return ctx, {m: s for m, s in zip(masked, signatures) if m != s}


def _masked_folder(ctx: FolderContext) -> FolderContext:
    return FolderContext(rel_path=ctx.rel_path, name=ctx.name, summary_hint=safety.mask_secrets(ctx.summary_hint),
                         children=[(name, safety.mask_secrets(summary)) for name, summary in ctx.children])


def code_shas(root: Path, nodes: dict, ids) -> tuple[dict[str, str], dict[str, str]]:
    """({node id: blob SHA}, {node id: mark}) for the code files among ids, one read each. The mark (node["xs"])
    says why the AI never gets the file: "secret" (its name; never read), "too_big" (over MAX_SOURCE_BYTES) or
    "unreadable" (gone, locked, or through a link)."""
    files = safety.ProjectFiles(root)
    limit = _max_source_bytes()
    shas: dict[str, str] = {}
    marks: dict[str, str] = {}
    for node_id in ids:
        if not code_file(nodes.get(node_id)):
            continue
        if safety.is_secret_file(node_id):
            marks[node_id] = "secret"
            continue
        try:
            shas[node_id] = blob_sha(files.read(node_id, limit))
        except safety.TooBig:
            marks[node_id] = "too_big"
        except (ValueError, OSError):  # UnsafePath (a link), gone, locked
            marks[node_id] = "unreadable"
    return shas, marks


def job_items(nodes: dict) -> list[str]:
    """Explain-all order: code files first, then folders deepest first, the root last."""
    files = [i for i, n in nodes.items() if code_file(n) and not safety.is_secret_file(i)]
    folders = [i for i, n in nodes.items() if isinstance(n, dict) and n.get("t") == "d" and i != "."]
    folders.sort(key=_depth, reverse=True)  # stable: same-depth folders keep the graph order
    return files + folders + (["."] if "." in nodes else [])


# --- state -------------------------------------------------------------------------------------

def _log_name(key: str, name):
    """How a log line names a project's folder or one of its files: never by name inside a GitHub snapshot,
    which may be a private repo."""
    return "(a GitHub snapshot)" if str(key).startswith("github:") else name


@dataclass
class Project:
    key: str
    root: Path
    graph: dict
    demo: bool = False

    @property
    def nodes(self) -> dict:
        return self.graph.get("nodes") or {}


@dataclass
class _Work:
    """One model call, ready to go."""

    node_id: str
    kind: str  # "file" or "folder"
    sha: str   # the cache key
    ctx: FileContext | FolderContext
    back: dict = field(default_factory=dict)  # masked signature -> the node's signature


class _Task:
    """An on-demand request waiting for the worker."""

    def __init__(self, key: tuple, project: Project, work: _Work, model: str):
        self.key = key
        self.project = project
        self.work = work
        self.model = model
        self.done = threading.Event()
        self.result: tuple[dict, bool] | None = None
        self.error: AIError | None = None
        self.waiters = 1  # requests still waiting; 0 means everyone gave up


@dataclass
class Job:
    id: str
    project: Project
    model: str
    items: list[str]
    state: str = "running"  # running | done | cancelled | failed
    done: int = 0
    errors: int = 0
    current: str = ""
    message: str = M.JOB_STARTED
    results: list = field(default_factory=list)  # [[node id, x], ...] in the order they were made
    xs: dict = field(default_factory=dict)       # node id -> x, for the folders' children
    pos: int = 0                                 # the next item to take
    cancel: bool = False
    stop_message: str = ""
    in_a_row: int = 0

    def block(self, since: int = 0) -> dict:
        since = max(0, min(since, len(self.results)))
        results = [[node_id, x] for node_id, x in self.results[since:]]
        return {"job": self.id, "state": self.state, "done": self.done, "total": len(self.items),
                "errors": self.errors, "current": self.current, "message": self.message,
                "results": results, "next": since + len(results)}


class AIService:
    """Everything AI for one server. build(root, demo) analyzes a project again when it is not in memory;
    build_key(key, root, demo), when given, does it instead and may raise AIError (a project that is gone)."""

    def __init__(self, cache: ExplanationCache, build: Callable[[Path, bool], dict],
                 build_key: Callable[[str, Path, bool], dict] | None = None):
        self.cache = cache
        self._build = build
        self._build_key = build_key
        self.request_wait = ollama.ANSWER_TIMEOUT + 60.0  # seconds an on-demand request waits for its answer
        self.answer_timeout = ollama.ANSWER_TIMEOUT
        self.start_wait = ollama.START_WAIT
        self.started_process = None  # the Ollama process start_ollama() launched, if any
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._probe_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._status: dict | None = None
        self._status_at = 0.0
        self._status_seq = 0   # the probe that made self._status (probes are numbered as they begin)
        self._probes = 0       # probes begun so far
        self._valid_from = 0   # blocks of probes numbered lower are not reused (invalidate)
        self._projects: collections.OrderedDict[str, Project] = collections.OrderedDict()
        self._pending: collections.deque[_Task] = collections.deque()
        self._running_task: _Task | None = None
        self._jobs: collections.OrderedDict[str, Job] = collections.OrderedDict()
        self._worker: threading.Thread | None = None
        self._closing = False
        self._used: set[str] = set()  # models this server asked; they are let go of when AI is turned off

    # --- status ---

    def _reusable(self, after: int | None = None) -> dict | None:
        """With the lock held: a copy of the last status block when it may be used again, else None.
        after None: it is less than STATUS_TTL seconds old. Else: its probe began after `after` probes had
        begun, that is after the caller asked."""
        if self._status is None or self._status_seq < self._valid_from:
            return None
        if after is None:
            ok = time.monotonic() - self._status_at < STATUS_TTL
        else:
            ok = self._status_seq > after
        return copy.deepcopy(self._status) if ok else None

    def _fresh_status(self) -> dict | None:
        with self._lock:
            return self._reusable()

    def status(self, fresh: bool = False) -> dict:
        """The status block. It is reused for STATUS_TTL seconds; fresh=True asks Ollama after this call
        instead. Requests that come together share one probe: a probe begun after a fresh caller asked
        answers for it too, so the fresh requests that arrive while one probe runs all share the next one."""
        with self._lock:
            after = self._probes if fresh else None
            block = self._reusable(after)
        if block is not None:
            return block
        with self._probe_lock:  # one probe at a time; the others use its answer
            with self._lock:
                block = self._reusable(after)
                if block is not None:
                    return block
                self._probes += 1
                seq = self._probes
            block = compute_status()
            with self._lock:
                self._status, self._status_at, self._status_seq = block, time.monotonic(), seq
            return copy.deepcopy(block)

    def prefetch_status(self) -> None:
        """Ask Ollama in the background (used while a folder is being analyzed)."""
        if self._fresh_status() is None:
            threading.Thread(target=self._quiet_status, name="codevision-ai-status", daemon=True).start()

    def _quiet_status(self) -> None:
        try:
            self.status()
        except Exception:
            log.exception("AI status failed")

    def invalidate(self) -> None:
        """Forget the status block. A probe already on its way saw the old state, so its block is not
        reused either (its own caller still gets it)."""
        with self._lock:
            self._status = None
            self._valid_from = self._probes + 1

    def fallback_status(self, message: str) -> dict:
        block = dict(self._fresh_status() or {
            "provider": "local", "enabled": False, "available": False, "model": "", "models": [],
            "recommended": list(prompts.RECOMMENDED_MODELS), "ollama": "stopped", "can_start": False})
        block["message"] = message
        return block

    # --- projects in memory ---

    def remember(self, key: str, root: Path, graph: dict, demo: bool = False) -> Project:
        project = Project(key, Path(root), graph, demo)
        with self._lock:
            self._projects[key] = project
            self._projects.move_to_end(key)
            while len(self._projects) > MAX_PROJECTS:
                self._projects.popitem(last=False)
        return project

    def project(self, key: str, root: Path, demo: bool = False) -> Project:
        with self._lock:
            project = self._projects.get(key)
            if project is not None:
                self._projects.move_to_end(key)
                return project
        if not Path(root).is_dir():
            raise AIError("not_found", M.PROJECT_NOT_FOUND)
        try:
            if self._build_key is not None:
                graph = self._build_key(key, Path(root), demo)
            else:
                graph = self._build(Path(root), demo)
        except AIError:
            raise  # build_key's own answer (a snapshot that is gone)
        except Exception:
            log.exception("analysis for AI explanations failed: %s", _log_name(key, root))
            raise AIError("failed", M.ANALYSIS_FAILED) from None  # 502, like every "failed" of explain
        return self.remember(key, root, graph, demo)

    def roots_in_use(self) -> set[Path]:
        """The roots AI explanations may still read (normcase of the real path): the projects in memory, the
        waiting and running on-demand requests, and the jobs still working (a cancelled job's current item
        too). The GitHub snapshot cleanup never deletes these."""
        with self._lock:
            roots = [p.root for p in self._projects.values()]
            roots += [t.project.root for t in self._pending]
            if self._running_task is not None:
                roots.append(self._running_task.project.root)
            roots += [j.project.root for j in self._jobs.values() if j.state == "running" or j.current]
        out = set()
        for root in roots:  # outside the lock: realpath may touch the disk
            try:
                out.add(Path(os.path.normcase(os.path.realpath(os.fspath(root)))))
            except (OSError, ValueError):
                out.add(Path(os.path.normcase(os.path.abspath(os.fspath(root)))))
        return out

    def forget_projects(self, keys) -> dict:
        """Let go of these projects (the server's hook when a GitHub logout or an account switch makes private
        snapshots unreadable): they leave memory, their running jobs stop at once (the item being made still
        ends in the worker, and its answer is dropped), and their waiting or running on-demand requests are
        answered not_found. Other projects are untouched. Returns {"projects", "jobs", "tasks"} counts."""
        keys = {k for k in (keys or ()) if isinstance(k, str)}
        out = {"projects": 0, "jobs": 0, "tasks": 0}
        if not keys:
            return out
        with self._lock:
            for key in [k for k in self._projects if k in keys]:
                del self._projects[key]
                out["projects"] += 1
            for job in self._jobs.values():
                if job.project.key in keys and job.state == "running":
                    job.cancel = True
                    job.state = "cancelled"
                    job.message = M.JOB_PROJECT_CLOSED.format(n=len(job.results))
                    out["jobs"] += 1
            kept = collections.deque()
            for task in self._pending:
                if task.project.key in keys:
                    task.error = AIError("not_found", M.PROJECT_CLOSED)
                    task.done.set()
                    out["tasks"] += 1
                else:
                    kept.append(task)
            self._pending = kept
            running = self._running_task
            if running is not None and running.project.key in keys and not running.done.is_set():
                running.error = AIError("not_found", M.PROJECT_CLOSED)  # the call ends in the worker; nobody waits
                running.done.set()
                out["tasks"] += 1
            self._wake.notify_all()
        return out

    # --- the cache ---

    def _cache_get(self, model: str, sha: str, kind: str) -> Explanation | None:
        if not model:
            return None
        return self.cache.get(PROVIDER, model, prompts.PROMPT_VERSION, sha, kind=kind)

    def _cache_put(self, model: str, work: _Work, exp: Explanation) -> None:
        self.cache.put(PROVIDER, model, prompts.PROMPT_VERSION, work.sha, exp, kind=work.kind)

    def _cached_xs(self, root: Path, nodes: dict, model: str, top: str | None = None,
                   shas: dict[str, str] | None = None) -> dict:
        """x for every node under `top` (all nodes when None) whose explanation is in the cache.
        Folders are filled bottom-up from their children's cached or rule summaries, the same way
        a job builds them, so their keys match. Cache rows are read in batches, one depth at a time.
        shas: the code files' SHAs (code_shas) when the caller has them already."""
        if not model:
            return {}
        if top in (None, "."):
            ids = list(nodes)
        else:
            ids = [i for i in nodes if i == top or i.startswith(top + "/")]
        version = prompts.PROMPT_VERSION
        if shas is None:
            shas = code_shas(root, nodes, ids)[0]
        else:
            wanted = set(ids)
            shas = {node_id: sha for node_id, sha in shas.items() if node_id in wanted}
        xs: dict = {}
        found = self.cache.get_many(PROVIDER, model, version, shas.values(), kind="file") if shas else {}
        for node_id, sha in shas.items():
            if sha in found:
                xs[node_id] = make_x(nodes[node_id], found[sha])
        by_depth: dict[int, list[str]] = collections.defaultdict(list)
        for node_id in ids:
            if isinstance(nodes[node_id], dict) and nodes[node_id].get("t") == "d":
                by_depth[_depth(node_id)].append(node_id)
        for depth in sorted(by_depth, reverse=True):  # a folder's key needs its child folders' results
            keys = {node_id: folder_key(folder_context(nodes, node_id, xs)) for node_id in by_depth[depth]}
            found = self.cache.get_many(PROVIDER, model, version, keys.values(), kind="folder")
            for node_id, key in keys.items():
                if key in found:
                    xs[node_id] = make_x(nodes[node_id], found[key])
        return xs

    def annotate(self, graph: dict, root: Path, status: dict | None = None) -> dict:
        """graph["ai"]; while AI is on also node["xs"] for code files the AI never gets (whatever the model)
        and node["x"] for cached explanations of the chosen model. Never calls the model, and reads no file
        while AI is off."""
        block = status if status is not None else self.status()
        graph["ai"] = block
        if not block.get("enabled"):
            return graph
        nodes = graph.get("nodes") or {}
        shas, marks = code_shas(Path(root), nodes, list(nodes))
        for node_id, mark in marks.items():
            nodes[node_id]["xs"] = mark
        for node_id, x in self._cached_xs(Path(root), nodes, block.get("model") or "", shas=shas).items():
            nodes[node_id]["x"] = x
        return graph

    # --- preparing one node ---

    def _prepare(self, project: Project, node_id: str, model: str, xs: dict | None = None):
        """(cached x, None) or (None, work) for one node. Raises AIError (not_found, secret, not_code, too_big)."""
        nodes = project.nodes
        node = nodes.get(node_id) if isinstance(node_id, str) else None
        if not isinstance(node, dict):
            raise AIError("not_found", M.NODE_NOT_FOUND)
        if node.get("t") == "f":
            if safety.is_secret_file(node_id):
                raise AIError("secret", M.SECRET)
            if not node.get("lang"):
                raise AIError("not_code", M.NOT_CODE)
            try:
                data = safety.ProjectFiles(project.root).read(node_id, _max_source_bytes())
            except safety.TooBig:
                raise AIError("too_big", M.TOO_BIG) from None
            except safety.UnsafePath:
                raise AIError("not_found", M.NODE_NOT_FOUND) from None
            except OSError:
                raise AIError("not_found", M.FILE_UNREADABLE) from None
            sha = blob_sha(data)
            hit = self._cache_get(model, sha, "file")
            if hit is not None:
                return make_x(node, hit), None
            ctx, back = file_context(node_id, node, data)
            return None, _Work(node_id, "file", sha, ctx, back)
        if node.get("t") == "d":
            try:
                safety.ProjectFiles(project.root).path(node_id)
            except safety.UnsafePath:
                raise AIError("not_found", M.NODE_NOT_FOUND) from None
            if xs is None:
                xs = self._cached_xs(project.root, nodes, model, top=node_id)
                if node_id in xs:
                    return xs[node_id], None
            ctx = folder_context(nodes, node_id, xs)
            sha = folder_key(ctx)
            hit = self._cache_get(model, sha, "folder")
            if hit is not None:
                return make_x(node, hit), None
            return None, _Work(node_id, "folder", sha, ctx)
        raise AIError("not_code", M.NOT_CODE)

    def _ask(self, model: str, work: _Work) -> Explanation:
        """One model call (worker thread only). Raises AIError."""
        settings = providers.ai_settings()
        if settings.url_error:
            raise AIError("not_running", settings.url_error)
        explainer = ollama.OllamaExplainer(settings.base_url, model, answer_timeout=self.answer_timeout)
        with self._lock:
            self._used.add(model)
        if work.kind == "file":
            exp = explainer.explain_file(work.ctx)
        else:
            exp = explainer.explain_folder(_masked_folder(work.ctx))
        if work.back:
            exp = Explanation(s=exp.s, d=exp.d, f={work.back.get(k, k): v for k, v in exp.f.items()})
        return exp

    def _ollama_up(self) -> bool:
        settings = providers.ai_settings()
        if settings.url_error:
            return False
        return ollama.OllamaClient(settings.base_url, connect_timeout=PROBE_CONNECT_TIMEOUT).is_up()

    def _ollama_gone(self) -> bool:
        """True when nothing answers at Ollama's address any more (it stopped or crashed); a slow
        Ollama is not gone."""
        settings = providers.ai_settings()
        if settings.url_error:
            return True
        try:
            ollama.OllamaClient(settings.base_url, connect_timeout=PROBE_CONNECT_TIMEOUT).version()
        except (ollama.OllamaUnreachable, ollama.OllamaDisconnected):  # refused, or closed with no reply
            return True
        except ollama.OllamaError:
            return False
        return False

    def _keep_model(self, model: str) -> None:
        """Save an automatic model choice the first time it is used, so its cached texts still show
        while Ollama is off (the status cannot choose a model then)."""
        try:
            if model and not providers.ai_settings().model:
                providers.update_ai(model=model)
        except OSError:
            log.warning("saving the model choice failed", exc_info=True)

    @staticmethod
    def _check_ready(block: dict) -> None:
        if not block["enabled"]:
            raise AIError("off", M.EXPLAIN_OFF)
        settings = providers.ai_settings()
        if settings.url_error:
            raise AIError("not_running", settings.url_error)
        if block["ollama"] != "running":
            raise AIError("not_running", M.NOT_RUNNING)
        if block["message"] == M.NO_ANSWER:  # it answers, but not with its model list
            raise AIError("not_running", M.NO_ANSWER)
        names = [m["name"] for m in block["models"]]
        if not block["model"] or not names:
            raise AIError("no_model", M.NO_MODEL)
        if block["model"] not in names:
            raise AIError("no_model", M.NO_CHOSEN_MODEL)

    # --- on-demand ---

    def explain(self, key: str, root: Path, demo: bool, node_id: str) -> dict:
        """POST /api/ai/explain: {"node", "x", "cached"}. Raises AIError."""
        block = self.status()
        if not block["enabled"]:
            raise AIError("off", M.EXPLAIN_OFF)
        project = self.project(key, root, demo)
        model = block["model"]
        cached, work = self._prepare(project, node_id, model)
        if cached is not None:
            return {"node": node_id, "x": cached, "cached": True}
        self._check_ready(block)
        self._keep_model(model)
        task = self._submit(key, project, work, model)
        if not task.done.wait(self.request_wait):
            with self._lock:
                task.waiters -= 1
            raise AIError("timeout", M.TIMEOUT)
        if task.error is not None:
            raise task.error
        x, was_cached = task.result
        return {"node": node_id, "x": x, "cached": was_cached}

    def _submit(self, key: str, project: Project, work: _Work, model: str) -> _Task:
        task_key = (key, work.node_id, model, work.kind, work.sha)
        with self._lock:
            if self._closing:
                raise AIError("busy", M.CLOSING)
            running = [self._running_task] if self._running_task is not None else []
            for task in itertools.chain(running, self._pending):
                if task.key == task_key and not task.done.is_set():
                    task.waiters += 1  # the same question is already on its way
                    return task
            if len([t for t in self._pending if t.waiters > 0]) >= MAX_PENDING:
                raise AIError("busy", M.BUSY)
            task = _Task(task_key, project, work, model)
            self._pending.append(task)
            self._ensure_worker()
            self._wake.notify_all()
            return task

    def pending(self) -> int:
        """On-demand requests waiting for the worker (for tests and progress checks)."""
        with self._lock:
            return len([t for t in self._pending if t.waiters > 0])

    # --- explain-all jobs ---

    def explain_all(self, key: str, root: Path, demo: bool) -> tuple[int, dict]:
        """POST /api/ai/explain-all: (202, new job) or (200, the running job of this project)."""
        block = self.status()
        self._check_ready(block)
        project = self.project(key, root, demo)
        self._keep_model(block["model"])
        with self._lock:
            if self._closing:
                raise AIError("busy", M.CLOSING)
            for job in self._jobs.values():
                if job.project.key == key and job.state == "running" and not job.cancel:
                    return 200, job.block()
            job = Job(id="j" + secrets.token_hex(6), project=project, model=block["model"],
                      items=job_items(project.nodes))
            if not job.items:
                job.state, job.message = "done", M.JOB_DONE
            self._jobs[job.id] = job
            finished = [j for j in self._jobs.values() if j.state != "running"]
            for old in finished[:max(0, len(finished) - MAX_JOBS)]:
                del self._jobs[old.id]
            self._ensure_worker()
            self._wake.notify_all()
            return 202, job.block()

    def job(self, job_id: str, since: int = 0) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise AIError("not_found", M.JOB_NOT_FOUND)
            return job.block(since)

    def cancel_job(self, job_id: str, since: int = 0) -> dict:
        """Stop a job after the item it is working on."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise AIError("not_found", M.JOB_NOT_FOUND)
            self._stop(job, M.JOB_CANCELLED)
            return job.block(since)

    def _stop(self, job: Job, message: str) -> None:
        """With the lock held: stop now when idle, else after the current item."""
        if job.state != "running":
            return
        if job.current:
            job.cancel = True
            job.stop_message = message
            job.message = M.JOB_STOPPING
        else:
            job.state = "cancelled"
            job.message = message.format(n=len(job.results))

    def _stop_jobs(self, message: str) -> None:
        with self._lock:
            for job in self._jobs.values():
                self._stop(job, message)

    # --- the worker thread ---

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="codevision-ai", daemon=True)
            self._worker.start()

    def _next_job(self) -> Job | None:
        for job in reversed(self._jobs.values()):  # the newest job first: it is what the user looks at
            if job.state == "running" and not job.cancel and job.pos < len(job.items):
                return job
        return None

    def _run(self) -> None:
        while True:
            task = job = node_id = None
            with self._lock:
                while True:
                    if self._closing:
                        return
                    while self._pending and self._pending[0].waiters <= 0:  # everyone gave up on it
                        self._pending.popleft().done.set()
                    if self._pending:
                        task = self._pending.popleft()
                        self._running_task = task
                        break
                    job = self._next_job()
                    if job is not None:
                        node_id = job.items[job.pos]
                        job.pos += 1
                        job.current = node_id
                        job.message = M.JOB_WORKING
                        for other in self._jobs.values():
                            if other is not job and other.state == "running" and not other.cancel:
                                other.message = M.JOB_WAITING
                        break
                    self._wake.wait()
            try:
                if task is not None:
                    self._do_task(task)
                else:
                    self._do_job_item(job, node_id)
            except Exception:  # never let the worker die
                log.exception("AI worker failed")

    def _do_task(self, task: _Task) -> None:
        try:
            node = task.project.nodes[task.work.node_id]
            hit = self._cache_get(task.model, task.work.sha, task.work.kind)
            if hit is not None:
                task.result = (make_x(node, hit), True)
            else:
                exp = self._ask(task.model, task.work)
                self._cache_put(task.model, task.work, exp)
                task.result = (make_x(node, exp), False)
        except AIError as exc:
            if exc.code in ("failed", "timeout") and self._ollama_gone():
                exc = AIError("not_running", M.NOT_RUNNING)  # Ollama ended while it was answering
            task.error = exc
            if exc.code == "not_running":
                self.invalidate()
        except Exception:
            log.exception("AI explanation failed: %s", _log_name(task.project.key, task.work.node_id))
            task.error = AIError("failed", M.FAILED)
        finally:
            with self._lock:
                self._running_task = None
            task.done.set()

    def _do_job_item(self, job: Job, node_id: str) -> None:
        x, error = None, None
        try:
            with self._lock:
                xs = dict(job.xs)
            folder = isinstance(job.project.nodes.get(node_id), dict) and job.project.nodes[node_id].get("t") == "d"
            cached, work = self._prepare(job.project, node_id, job.model, xs=xs if folder else None)
            if cached is not None:
                x = cached
            else:
                exp = self._ask(job.model, work)
                self._cache_put(job.model, work, exp)
                x = make_x(job.project.nodes[node_id], exp)
        except AIError as exc:
            error = exc
        except Exception:
            log.exception("AI explanation failed: %s", _log_name(job.project.key, node_id))
            error = AIError("failed", M.FAILED)
        stop = ""
        if error is not None:
            if error.code == "not_running" or (error.code in ("failed", "timeout") and not self._ollama_up()):
                stop = M.JOB_OLLAMA_STOPPED
                self.invalidate()
            elif error.code == "no_model":
                stop = M.JOB_NO_MODEL
        with self._lock:
            job.current = ""
            if job.state != "running":
                return
            if stop:
                job.state = "failed"
                job.message = stop.format(n=len(job.results))
                return
            job.done += 1
            if x is not None:
                job.results.append([node_id, x])
                job.xs[node_id] = x
                job.in_a_row = 0
            elif error.code not in SKIPPED:
                job.errors += 1
                job.in_a_row += 1
                if job.in_a_row >= MAX_ERRORS_IN_A_ROW:
                    job.state = "failed"
                    job.message = M.JOB_TOO_MANY_ERRORS.format(n=len(job.results))
                    return
            if job.cancel:
                job.state = "cancelled"
                job.message = (job.stop_message or M.JOB_CANCELLED).format(n=len(job.results))
            elif job.pos >= len(job.items):
                job.state = "done"
                job.message = M.JOB_DONE_ERRORS.format(n=job.errors) if job.errors else M.JOB_DONE

    # --- settings and starting Ollama ---

    def update_settings(self, body) -> dict:
        """POST /api/ai/settings {"enabled": bool, "model": str (optional)}. Raises AIError (bad_request: 400)."""
        if not isinstance(body, dict) or not ({"enabled", "model"} & body.keys()):
            raise AIError("bad_request", M.BAD_REQUEST)
        if "enabled" in body and not isinstance(body["enabled"], bool):
            raise AIError("bad_request", M.BAD_REQUEST)
        model = body.get("model")
        if "model" in body:
            if not isinstance(model, str):
                raise AIError("bad_request", M.BAD_MODEL)
            model = model.strip()
            if model and not MODEL_NAME.fullmatch(model):
                raise AIError("bad_request", M.BAD_MODEL)
        before = self.status(fresh=True)
        installed = [m["name"] for m in before["models"]]
        if model and before["ollama"] == "running" and installed and model not in installed:  # [] says nothing
            raise AIError("bad_request", M.MODEL_NOT_INSTALLED.format(model=model))
        changes: dict = {}
        if "enabled" in body:
            changes["enabled"] = body["enabled"]
            if body["enabled"]:
                changes["provider"] = "local"
        if "model" in body:
            changes["model"] = model
        elif body.get("enabled") and not providers.ai_settings().model and before["model"]:
            changes["model"] = before["model"]  # keep the automatic choice, so cached texts show while Ollama is off
        try:
            providers.update_ai(**changes)
        except OSError:
            log.exception("saving the AI settings failed")
            raise AIError("failed", M.SETTINGS_NOT_SAVED, status=500) from None
        self.invalidate()
        after = self.status(fresh=True)
        if not after["enabled"] or after["model"] != before["model"]:
            self._stop_jobs(M.JOB_SETTINGS_CHANGED)
        if before["enabled"] and not after["enabled"]:  # the GPU memory goes back to games and other programs
            threading.Thread(target=self._unload_when_idle, name="codevision-ai-unload", daemon=True).start()
        return after

    def start_ollama(self) -> dict:
        """POST /api/ai/start: start the installed Ollama and wait until it answers."""
        with self._start_lock:
            block = self.status(fresh=True)
            if block["ollama"] == "running":
                return block
            settings = providers.ai_settings()
            if settings.url_error:
                raise AIError("failed", settings.url_error, status=409)
            install = ollama.find_ollama()
            if install is None:
                raise AIError("missing", M.START_MISSING)
            try:
                process = ollama.launch(install, settings.base_url)
            except (OSError, ValueError):
                log.exception("starting Ollama failed: %s", install.command)
                raise AIError("failed", M.START_FAILED) from None
            self.started_process = process
            client = ollama.OllamaClient(settings.base_url, connect_timeout=PROBE_CONNECT_TIMEOUT)
            serve = install.command[-1] == "serve"
            deadline = time.monotonic() + self.start_wait
            while not client.is_up(timeout=2.0):
                if serve and process.poll() is not None and not client.is_up(timeout=2.0):
                    raise AIError("failed", M.START_FAILED)  # "ollama serve" ended without answering
                if time.monotonic() >= deadline:
                    raise AIError("timeout", M.START_TIMEOUT)
                time.sleep(0.25)
            self.invalidate()
            return self.status(fresh=True)

    # --- letting go of the model ---

    def _busy(self) -> bool:
        """True while the worker makes a model call or has one waiting."""
        with self._lock:
            return (self._running_task is not None or any(t.waiters > 0 for t in self._pending)
                    or any(job.current for job in self._jobs.values()))

    def unload_models(self, timeout: float = 3.0) -> list[str]:
        """Ask Ollama to free the memory of the models this server used and Ollama still holds (best
        effort: an Ollama that is off or does not answer is left alone). Returns the models let go of."""
        with self._lock:
            used = sorted(self._used)
        if not used:
            return []
        settings = providers.ai_settings()
        if settings.url_error:
            return []
        client = ollama.OllamaClient(settings.base_url, connect_timeout=PROBE_CONNECT_TIMEOUT)
        try:
            held = set(client.loaded(timeout=timeout))
        except ollama.OllamaError:
            return []
        done = []
        for model in used:
            if model not in held:
                continue
            try:
                client.unload(model, timeout=timeout)
                done.append(model)
            except ollama.OllamaError as exc:
                log.warning("letting go of the model %s failed: %s", model, exc)
        return done

    def _unload_when_idle(self) -> None:
        """After AI explanations are turned off: let the call in progress finish, then free the memory."""
        try:
            deadline = time.monotonic() + self.answer_timeout + 10.0
            while self._busy() and not self._closing and time.monotonic() < deadline:
                time.sleep(0.25)
            if not providers.ai_settings().enabled:  # unless AI was turned on again meanwhile
                self.unload_models()
        except Exception:
            log.exception("letting go of the AI model failed")

    # --- shutting down ---

    def close(self) -> None:
        """Stop the worker after its current call; waiting requests get an answer at once. The models
        this server used are let go of, so closing CodeVision frees the GPU memory."""
        with self._lock:
            self._closing = True
            for task in self._pending:
                task.error = AIError("busy", M.CLOSING)
                task.done.set()
            self._pending.clear()
            for job in self._jobs.values():
                if job.state == "running":
                    job.state = "cancelled"
                    job.current = ""
                    job.message = M.JOB_CLOSED
            self._wake.notify_all()
        try:
            self.unload_models(timeout=2.0)
        except Exception:
            log.debug("letting go of the AI model on close failed", exc_info=True)
