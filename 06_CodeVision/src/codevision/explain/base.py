"""Provider-neutral interface and data classes for AI explanations.

The "local" provider (Ollama on this computer) is in ollama.py. A provider only
has to implement ``Explainer``; caching, jobs and graph updates live elsewhere.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path


def data_dir() -> Path:
    """Per-user data folder: CODEVISION_HOME, else %LOCALAPPDATA%\\CodeVision, else ~/.codevision."""
    home = os.environ.get("CODEVISION_HOME")
    if home:
        return Path(home)
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        return Path(local) / "CodeVision"
    return Path.home() / ".codevision"


@dataclass
class FileContext:
    """Everything an explainer gets for one file."""

    rel_path: str  # project-relative POSIX path
    language: str  # python | javascript | typescript
    source: str
    summary_hint: str = ""  # rule-based one-line summary to improve on
    symbols: list[str] = field(default_factory=list)  # signatures shown in the panel, e.g. "addTodo(title)"
    uses: list[str] = field(default_factory=list)  # files this file uses
    used_by: list[str] = field(default_factory=list)  # files that use this file


@dataclass
class FolderContext:
    """Everything an explainer gets for one folder."""

    rel_path: str  # "" for the project root
    name: str
    summary_hint: str = ""
    children: list[tuple[str, str]] = field(default_factory=list)  # (child name, one-line summary)


@dataclass
class Explanation:
    """What an explainer returns; the keys match the graph node fields."""

    s: str = ""  # one-line summary
    d: str = ""  # a few plain sentences
    f: dict[str, str] = field(default_factory=dict)  # symbol signature -> one-line description

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Explanation:
        """Build from a cache row or a model answer; missing or odd values become empty."""
        f = data.get("f") if isinstance(data.get("f"), dict) else {}
        pairs = ((str(k), str(v or "").strip()) for k, v in f.items())
        return cls(
            s=str(data.get("s") or "").strip(),
            d=str(data.get("d") or "").strip(),
            f={k: v for k, v in pairs if v},
        )


class AIError(Exception):
    """A failure the screen can show: a contract code, its HTTP status and one Korean message."""

    STATUS = {
        "off": 409, "not_running": 409, "no_model": 409, "busy": 409, "missing": 409,
        "not_found": 404,
        "not_code": 422, "secret": 422, "too_big": 422,
        "timeout": 504,
        "bad_answer": 502, "failed": 502,
        "bad_request": 400,
    }

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or self.STATUS.get(code, 500)

    def reply(self) -> dict:
        """The JSON body: {"error": Korean, "code": ...} (plain {"error"} for a bad request)."""
        if self.code == "bad_request":
            return {"error": self.message}
        return {"error": self.message, "code": self.code}


class Explainer(ABC):
    """One AI provider. ``name`` and ``model`` are part of the cache key.

    explain_file() and explain_folder() return an Explanation, None to keep the
    rule-based text, or raise AIError with a contract code.
    """

    name: str = ""
    model: str = ""

    @abstractmethod
    def available(self) -> bool:
        """True when explanations can be made right now (configured, reachable)."""

    @abstractmethod
    def explain_file(self, ctx: FileContext) -> Explanation | None:
        """Explain one file, or return None to keep the rule-based text."""

    @abstractmethod
    def explain_folder(self, ctx: FolderContext) -> Explanation | None:
        """Explain one folder, or return None to keep the rule-based text."""
