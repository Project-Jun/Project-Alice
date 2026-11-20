"""Analyzer registry.

Every analyzer module exposes:

    EXTENSIONS: tuple[str, ...]          lowercase, with the dot
    analyze(rel_path, source) -> FileFacts
    resolve(imp, importer, index) -> list[str]     project files the import points to
    project_entries(index) -> dict[str, str]       rel_path -> Korean reason

and may expose:

    project_links(index) -> dict[str, list[str]]   file -> project files it loads that are not
                                                   imports in code (index.html -> app.js, app.css)

Modules load lazily so that one broken analyzer does not stop the others. Loading happens
once, under a lock: the server analyzes in several threads at once (two tabs opened right
after a start), and a thread must never see a half-filled list.
"""
from __future__ import annotations

import importlib
import logging
import threading

_MODULES = ("codevision.lang.python_lang", "codevision.lang.js_lang")
_loaded: list | None = None  # set once, to the complete list
_errors: dict[str, str] = {}
_lock = threading.Lock()


def analyzers() -> list:
    global _loaded
    found = _loaded
    if found is not None:  # the common case: already loaded, no lock needed
        return found
    with _lock:
        if _loaded is None:  # another thread may have loaded them while this one waited
            mods, errors = [], {}
            for name in _MODULES:
                try:
                    mods.append(importlib.import_module(name))
                except Exception as exc:  # keep going without this language
                    errors[name] = str(exc)
                    logging.getLogger(__name__).warning("analyzer %s unavailable: %s", name, exc)
            _errors.update(errors)
            _loaded = mods  # published only when complete
        return _loaded


def load_errors() -> dict[str, str]:
    analyzers()  # waits for a load that another thread has started
    with _lock:
        return dict(_errors)


def analyzer_for(rel_path: str):
    name = rel_path.rsplit("/", 1)[-1].lower()
    for module in analyzers():
        if any(name.endswith(ext) for ext in module.EXTENSIONS):
            return module
    return None
