"""Choosing an AI provider from the user's config.

Only "none" exists today. "local" (a model running on this computer) and
"cloud" (a commercial LLM service) are reserved names until one is chosen.
"""
from __future__ import annotations

import json
import logging

from .base import Explainer, data_dir

PLANNED_PROVIDERS = {
    "local": "내 컴퓨터에서 돌리는 AI(로컬 AI) 연결은 아직 준비 중이에요.",
    "cloud": "인터넷 AI 서비스 연결은 아직 준비 중이에요.",
}

log = logging.getLogger(__name__)


class NoneExplainer(Explainer):
    """No AI: the graph keeps the rule-based summaries from describe.py."""

    name = "none"
    model = ""

    def available(self) -> bool:
        return False

    def explain_file(self, ctx):
        return None

    def explain_folder(self, ctx):
        return None


def default_config() -> dict:
    return {"ai": {"provider": "none"}}


def read_config() -> dict:
    """Read <data dir>/config.json; fall back to the defaults when missing or broken."""
    path = data_dir() / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default_config()
    except (OSError, ValueError) as exc:
        log.warning("설정 파일을 읽지 못해서 기본 설정을 써요: %s (%s)", path, exc)
        return default_config()
    if not isinstance(data, dict):
        return default_config()
    if not isinstance(data.get("ai"), dict):
        data["ai"] = default_config()["ai"]
    return data


def get_explainer(config: dict | None = None) -> Explainer:
    """Build the explainer named in config["ai"]["provider"]."""
    if config is None:
        config = read_config()
    ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
    provider = str(ai.get("provider") or "none").strip().lower()
    if provider == "none":
        return NoneExplainer()
    if provider in PLANNED_PROVIDERS:
        raise NotImplementedError(PLANNED_PROVIDERS[provider] + ' 설정의 provider를 "none"으로 바꿔 주세요.')
    raise ValueError(f'알 수 없는 AI 공급자예요: {provider}. "none", "local", "cloud" 중 하나를 써 주세요.')
