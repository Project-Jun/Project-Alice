"""AI explanations with a local AI (Ollama on this computer).

The server owns one AIService (service.py). It answers GET /api/ai/status, saves
the settings, starts the installed Ollama, explains one node or a whole project in
its worker thread, and annotate()s /api/analyze: graph["ai"] plus, while AI is on,
node["x"] for explanations already in the cache and node["xs"] for code files the AI
never gets. /api/analyze never calls the model.

- base.py: Explainer, FileContext, FolderContext, Explanation, AIError, data_dir()
- providers.py: config.json (provider, enabled, model, base_url), NoneExplainer
- ollama.py: the "local" provider (urllib, loopback only), finding and starting Ollama
- safety.py: secret files, masking secrets, node ids that never leave the folder
- cache.py: SQLite cache keyed by provider, model, prompt version, kind and blob SHA-1
- prompts.py: the prompts, the answer schema, and reading and tidying the answers
- messages.py: every Korean text the AI side shows
"""
from __future__ import annotations

from .base import AIError, Explainer, Explanation, FileContext, FolderContext, data_dir
from .cache import ExplanationCache, blob_sha
from .ollama import DEFAULT_BASE_URL, OllamaClient, OllamaExplainer, check_base_url, find_ollama
from .prompts import PROMPT_VERSION
from .providers import AISettings, NoneExplainer, ai_settings, get_explainer, read_config, update_ai, write_config
from .safety import is_secret_file, mask_secrets
from .service import AIService, compute_status, make_x

MAX_SOURCE_BYTES = 1_000_000  # larger files are not sent (code "too_big"); the analyzers have the same limit

__all__ = [
    "AIError", "Explainer", "Explanation", "FileContext", "FolderContext", "data_dir",
    "ExplanationCache", "blob_sha", "PROMPT_VERSION", "MAX_SOURCE_BYTES",
    "DEFAULT_BASE_URL", "OllamaClient", "OllamaExplainer", "check_base_url", "find_ollama",
    "AISettings", "NoneExplainer", "ai_settings", "get_explainer", "read_config", "update_ai", "write_config",
    "is_secret_file", "mask_secrets",
    "AIService", "compute_status", "make_x", "status",
]


def status() -> dict:
    """The status block (the same as GET /api/ai/status), asking Ollama now."""
    return compute_status()
