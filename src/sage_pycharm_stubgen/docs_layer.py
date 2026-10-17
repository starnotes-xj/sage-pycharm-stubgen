"""The Chinese documentation layer: ``docs_zh.json`` and its tooling.

Historically the docs lived inside the ``SUPPLEMENTAL_DOCS`` dict in
``supplemental_docs.py`` together with the return-type knowledge.  The two
layers have opposite lifecycles — the return entries retire as Sage
upstream ships the annotations (``conformance --prune``), while the doc
layer only grows — so the docs now live in their own data file managed by
this module.

The file layout is::

    {"version": 1,
     "docs": {"sage.arith.misc": {"divisors": "返回整数 n 的全部正约数…"}}}

``supplemental_docs`` merges this file back into ``SUPPLEMENTAL_DOCS`` at
import time, so existing consumers are unchanged; new tooling should use
the APIs here (query, edit, validate, stats) instead of touching the
merged view.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

#: The data file, shipped beside this module (see pyproject package-data).
DATA_FILE = Path(__file__).with_name("docs_zh.json")

#: Bumped when the on-disk layout changes incompatibly.
FORMAT_VERSION = 1

_cache: dict[str, dict[str, str]] | None = None


def load_docs() -> dict[str, dict[str, str]]:
    """``module -> symbol -> Chinese doc``; cached until :func:`reload`.

    A missing or malformed file yields an empty layer (fail-open): the
    type layer in ``supplemental_docs`` is unaffected and stub generation
    proceeds without curated docs.
    """
    global _cache
    if _cache is None:
        _cache = _read_data_file()
    return _cache


def reload() -> dict[str, dict[str, str]]:
    """Drop the cache and re-read the data file (after external edits)."""
    global _cache
    _cache = None
    return load_docs()


def _read_data_file() -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("version") != FORMAT_VERSION:
        # Unknown future layout: treat as absent rather than guessing.
        return {}
    docs = payload.get("docs")
    if not isinstance(docs, dict):
        return {}
    return docs


def get_doc(module: str, symbol: str) -> str | None:
    """The curated Chinese doc for one symbol, if present."""
    return load_docs().get(module, {}).get(symbol)


def set_doc(module: str, symbol: str, doc: str) -> None:
    """Set (or replace) one doc in the in-memory layer; call save() after."""
    load_docs().setdefault(module, {})[symbol] = doc


def remove_doc(module: str, symbol: str) -> bool:
    """Drop one doc from the in-memory layer; returns whether it existed."""
    symbols = load_docs().get(module)
    if not symbols or symbol not in symbols:
        return False
    del symbols[symbol]
    if not symbols:
        load_docs().pop(module, None)
    return True


def save_docs() -> Path:
    """Write the in-memory layer back to the data file, atomically.

    Keys are sorted for stable diffs; the version stamp travels with the
    payload so future layout changes stay detectable.
    """
    payload: dict[str, Any] = {"version": FORMAT_VERSION, "docs": load_docs()}
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    directory = DATA_FILE.parent
    handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(temporary, DATA_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return DATA_FILE


def validate() -> list[str]:
    """Structural problems with the on-disk layer (empty when healthy)."""
    problems: list[str] = []
    try:
        payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"unreadable data file {DATA_FILE}: {exc}"]
    if not isinstance(payload, dict):
        return [f"{DATA_FILE}: top level must be an object"]
    if payload.get("version") != FORMAT_VERSION:
        problems.append(
            f"version {payload.get('version')!r} != expected {FORMAT_VERSION}"
        )
    docs = payload.get("docs")
    if not isinstance(docs, dict):
        return problems + [f"{DATA_FILE}: 'docs' must be an object"]
    for module, symbols in docs.items():
        if not isinstance(module, str) or not module:
            problems.append(f"non-string module key {module!r}")
            continue
        if not isinstance(symbols, dict):
            problems.append(f"{module}: symbol map must be an object")
            continue
        for symbol, doc in symbols.items():
            if not isinstance(symbol, str) or not symbol:
                problems.append(f"{module}: non-string symbol {symbol!r}")
            if not isinstance(doc, str) or not doc.strip():
                problems.append(f"{module}.{symbol}: doc must be non-empty text")
    return problems


def stats() -> dict[str, Any]:
    """Counts for tooling: totals plus the per-module distribution."""
    docs = load_docs()
    per_module = {module: len(symbols) for module, symbols in sorted(docs.items())}
    total = sum(per_module.values())
    characters = sum(len(doc) for symbols in docs.values() for doc in symbols.values())
    return {
        "modules": len(docs),
        "symbols": total,
        "characters": characters,
        "per_module": per_module,
    }
