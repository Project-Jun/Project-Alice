"""The AI settings in <data dir>/config.json and choosing the provider they name.

    {"ai": {"provider": "local", "enabled": false, "model": "", "base_url": "http://127.0.0.1:11434"}}

- "local" is Ollama on this computer (ollama.py), "none" turns AI explanations off,
  "cloud" (a commercial LLM service) is reserved and not implemented.
- Without a config file the provider is "local" and AI explanations are off.
- model "" means "choose for me": the first installed model of prompts.RECOMMENDED_MODELS,
  else the first installed model.
- base_url may only name this computer (127.0.0.1, localhost, ::1). The environment
  variable CODEVISION_OLLAMA_URL replaces it (for tests and development).
- The file is written atomically (a temporary file, then a replace), and keys this
  code does not know are kept.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from . import messages as M
from .base import Explainer, data_dir
from .ollama import DEFAULT_BASE_URL, URL_ENV, OllamaExplainer, check_base_url

DEFAULT_AI = {"provider": "local", "enabled": False, "model": "", "base_url": DEFAULT_BASE_URL}
PLANNED_PROVIDERS = {"cloud": M.CLOUD}

log = logging.getLogger(__name__)
_write_lock = threading.RLock()  # update_ai() holds it around its read and its write_config()


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


@dataclass
class AISettings:
    provider: str   # "local", "none", "cloud" or an unknown name
    enabled: bool   # the user turned AI explanations on
    model: str      # "" lets CodeVision choose
    base_url: str   # normalized when allowed, else as written
    url_error: str  # the Korean reason when base_url is not allowed, else ""


def config_path() -> Path:
    return data_dir() / "config.json"


def default_config() -> dict:
    return {"ai": dict(DEFAULT_AI)}


def _normalized_ai(ai) -> dict:
    ai = dict(ai) if isinstance(ai, dict) else {}
    provider = ai.get("provider")
    ai["provider"] = provider.strip().lower() if isinstance(provider, str) and provider.strip() else "local"
    ai["enabled"] = ai.get("enabled") is True
    ai["model"] = ai["model"].strip() if isinstance(ai.get("model"), str) else ""
    ai["base_url"] = ai["base_url"].strip() if isinstance(ai.get("base_url"), str) and ai["base_url"].strip() \
        else DEFAULT_BASE_URL
    return ai


def read_config() -> dict:
    """The whole config file with "ai" filled in; the defaults when it is missing or broken."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default_config()
    except (OSError, ValueError) as exc:
        log.warning("설정 파일을 읽지 못해서 기본 설정을 써요: %s (%s)", path, exc)
        return default_config()
    if not isinstance(data, dict):
        return default_config()
    data["ai"] = _normalized_ai(data.get("ai"))
    return data


def write_config(config: dict) -> None:
    """Write config.json atomically (a temporary file next to it, then os.replace). Raises OSError."""
    target = config_path()
    with _write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


def update_ai(**changes) -> dict:
    """Change some of provider, enabled, model, base_url and save; returns the new config."""
    with _write_lock:
        config = read_config()
        ai = config["ai"]
        for key, value in changes.items():
            if key not in DEFAULT_AI:
                raise ValueError(f"unknown AI setting: {key}")
            ai[key] = value
        config["ai"] = _normalized_ai(ai)
        write_config(config)
    return config


def ai_settings(config: dict | None = None) -> AISettings:
    """The AI settings in effect: config.json, with CODEVISION_OLLAMA_URL applied and base_url checked."""
    ai = _normalized_ai((config if config is not None else read_config()).get("ai"))
    raw_url = os.environ.get(URL_ENV) or ai["base_url"]
    try:
        base_url, url_error = check_base_url(raw_url), ""
    except ValueError as exc:
        base_url, url_error = raw_url, str(exc)
    return AISettings(provider=ai["provider"], enabled=ai["enabled"], model=ai["model"], base_url=base_url,
                      url_error=url_error)


def get_explainer(config: dict | None = None) -> Explainer:
    """The explainer config["ai"]["provider"] names. Raises ValueError for an unknown provider or a
    base_url that is not on this computer, NotImplementedError for "cloud"."""
    settings = ai_settings(config)
    if settings.provider == "none":
        return NoneExplainer()
    if settings.provider == "local":
        if settings.url_error:
            raise ValueError(settings.url_error)
        return OllamaExplainer(settings.base_url, settings.model)
    if settings.provider in PLANNED_PROVIDERS:
        raise NotImplementedError(PLANNED_PROVIDERS[settings.provider] + ' 설정의 provider를 "local"이나 "none"으로 바꿔 주세요.')
    raise ValueError(f'알 수 없는 AI 공급자예요: {settings.provider}. "local", "none", "cloud" 중 하나를 써 주세요.')
