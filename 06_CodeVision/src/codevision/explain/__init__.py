"""AI explanations for the graph (a skeleton: no AI provider is connected yet).

The server only needs ``enrich()`` and ``status()``. With the default "none"
provider the graph keeps the rule-based texts from describe.py and only
graph["ai"] is filled in.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Callable

from .base import Explainer, Explanation, FileContext, FolderContext, data_dir
from .cache import ExplanationCache, blob_sha
from .prompts import PROMPT_VERSION
from .providers import NoneExplainer, get_explainer, read_config

__all__ = [
    "Explainer", "Explanation", "FileContext", "FolderContext", "data_dir",
    "ExplanationCache", "blob_sha", "PROMPT_VERSION",
    "NoneExplainer", "get_explainer", "read_config",
    "NOT_CONNECTED", "status", "enrich",
]

NOT_CONNECTED = "AI 설명이 아직 연결되지 않았어요."
MAX_SOURCE_BYTES = 1_000_000  # same limit as the analyzers

log = logging.getLogger(__name__)


def status(explainer: Explainer) -> dict:
    """The graph["ai"] block, also returned by /api/ai/status."""
    if explainer.available():
        return {"provider": explainer.name, "available": True, "message": "AI 설명을 쓸 수 있어요."}
    return {"provider": explainer.name, "available": False, "message": NOT_CONNECTED}


def enrich(graph: dict, explainer: Explainer, cache: ExplanationCache | None,
           read_source: Callable[[str], bytes | None]) -> dict:
    """Replace rule-based s/d/f texts with AI explanations where the explainer gives one.

    read_source(rel_path) returns the file's raw bytes (or None). Code files are
    explained first, then folders from the deepest up, so a folder sees the
    new summaries of what it contains. Returns the same graph object.
    """
    graph["ai"] = status(explainer)
    if not graph["ai"]["available"]:
        return graph

    nodes = graph.get("nodes") or {}
    failed = 0

    for node_id, node in nodes.items():
        if node.get("t") != "f" or not node.get("lang"):
            continue
        data = read_source(node_id)
        if data is None or len(data) > MAX_SOURCE_BYTES:
            continue
        ctx = FileContext(
            rel_path=node_id,
            language=node["lang"],
            source=_decode(data),
            summary_hint=node.get("s", ""),
            symbols=[item[0] for item in node.get("f") or []],
            uses=list(node.get("u") or []),
            used_by=list(node.get("b") or []),
        )
        try:
            result = _cached(explainer, cache, "file", blob_sha(data), lambda: explainer.explain_file(ctx))
        except Exception:
            log.exception("AI explanation failed for %s", node_id)
            failed += 1
            continue
        if result is not None:
            _apply(node, result)

    folders = [i for i, n in nodes.items() if n.get("t") == "d"]
    for node_id in sorted(folders, key=_depth, reverse=True):
        node = nodes[node_id]
        children = [(nodes[k].get("n", k), nodes[k].get("s", "")) for k in node.get("k") or [] if k in nodes]
        ctx = FolderContext(
            rel_path="" if node_id == "." else node_id,
            name=node.get("n", ""),
            summary_hint=node.get("s", ""),
            children=children,
        )
        key = blob_sha(json.dumps(asdict(ctx), ensure_ascii=False, sort_keys=True).encode("utf-8"))
        try:
            result = _cached(explainer, cache, "folder", key, lambda: explainer.explain_folder(ctx))
        except Exception:
            log.exception("AI explanation failed for folder %s", node_id)
            failed += 1
            continue
        if result is not None:
            _apply(node, result)

    if failed:
        graph["ai"]["message"] = f"AI 설명을 {failed}곳에서 만들지 못해서, 그곳은 기본 설명을 보여 줘요."
    return graph


def _cached(explainer: Explainer, cache: ExplanationCache | None, kind: str, sha: str, call):
    key = (explainer.name, explainer.model, PROMPT_VERSION, sha)
    if cache is not None:
        hit = cache.get(*key, kind=kind)
        if hit is not None:
            return hit
    result = call()
    if result is not None and cache is not None:
        cache.put(*key, result, kind=kind)
    return result


def _apply(node: dict, exp: Explanation) -> None:
    if exp.s:
        node["s"] = exp.s
    if exp.d:
        node["d"] = exp.d
    if exp.f and node.get("f"):
        node["f"] = [[sig, exp.f.get(sig) or desc] for sig, desc in node["f"]]


def _depth(node_id: str) -> int:
    return 0 if node_id == "." else node_id.count("/") + 1


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
