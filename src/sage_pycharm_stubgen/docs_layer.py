"""The Chinese documentation layer: ``docs_zh.json`` and its tooling.

The layer started as a flat ``module -> symbol -> text`` map (v1).  Since the
texts follow one house layout — a summary paragraph, then ``参数:`` /
``返回:`` / ``陷阱:`` style bullet sections, then a ``示例::`` doctest block —
v2 stores that structure explicitly so tooling can consume sections
independently (per-section rendering in the IDE plugin, pitfall counting,
multi-frontend reuse).  The file layout is::

    {"version": 2,
     "docs": {"sage.arith.functions": {"lcm": {
         "summary": "最小公倍数。…",
         "sections": [{"heading": "陷阱", "items": ["与 gcd 归属不同模块…"]}],
         "example": "sage: from sage.arith.functions import lcm\n…"}}}}

Every field except ``summary`` is optional and omitted when empty.
:func:`load_docs` still returns the rendered full text per symbol, so every
existing consumer (``supplemental_docs``, ``docstring_enrich``,
``conformance``, ``installer``) is unchanged; structured access goes through
:func:`load_entries` / :func:`get_entry` / :func:`set_entry`.

``parse_blob`` and :func:`render_entry` are exact inverses on the house
format; ``scripts/migrate_docs_v2.py`` verified the round trip over the
whole corpus when the file moved from v1 to v2.

A missing or malformed file yields an empty layer (fail-open): the type
layer in ``supplemental_docs`` is unaffected and stub generation proceeds
without curated docs.
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
FORMAT_VERSION = 2

#: Marker line that starts the doctest example block.
EXAMPLE_MARKER = "示例::"

#: Fields an entry may carry; anything else is a validation error.
_ENTRY_FIELDS = frozenset({"summary", "sections", "example"})

_cache_entries: dict[str, dict[str, dict[str, Any]]] | None = None
_cache_docs: dict[str, dict[str, str]] | None = None


def load_entries() -> dict[str, dict[str, dict[str, Any]]]:
    """``module -> symbol -> structured entry``; cached until :func:`reload`."""
    global _cache_entries
    if _cache_entries is None:
        _cache_entries = _read_data_file()
    return _cache_entries


def load_docs() -> dict[str, dict[str, str]]:
    """``module -> symbol -> rendered full text``; cached until :func:`reload`.

    This is the shape every historical consumer has always seen.
    """
    global _cache_docs
    if _cache_docs is None:
        _cache_docs = {
            module: {symbol: render_entry(entry) for symbol, entry in symbols.items()}
            for module, symbols in load_entries().items()
        }
    return _cache_docs


def reload() -> dict[str, dict[str, str]]:
    """Drop the caches and re-read the data file (after external edits)."""
    global _cache_entries, _cache_docs
    _cache_entries = None
    _cache_docs = None
    return load_docs()


def _read_data_file() -> dict[str, dict[str, dict[str, Any]]]:
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


def _trim_blank_edges(lines: list[str]) -> None:
    """Drop leading/trailing blank lines in place (they are separators)."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    del lines[end:], lines[:start]


def parse_blob(text: str) -> dict[str, Any]:
    """Split one rendered document into its structured entry.

    Lines that start at column 0, are not bullets and end with ``:`` open a
    section; ``- `` lines inside a section become items, everything else in
    the section is its body.  The ``示例::`` marker switches to verbatim
    example capture until the end of the text.  Blank lines are separators
    between blocks and are not stored; :func:`render_entry` re-inserts one
    between every pair of blocks.
    """
    summary_lines: list[str] = []
    sections: list[dict[str, Any]] = []
    example_lines: list[str] | None = None
    current: dict[str, Any] | None = None
    for line in text.split("\n"):
        if example_lines is not None:
            example_lines.append(line)
            continue
        stripped = line.rstrip()
        is_bullet = line.startswith("- ")
        is_heading = (
            bool(line) and not line[0].isspace() and not is_bullet and stripped.endswith(":")
        )
        if is_heading:
            _trim_blank_edges(summary_lines if current is None else current["body"])
            if stripped == EXAMPLE_MARKER:
                example_lines = []
                current = None
                continue
            current = {"heading": stripped[:-1], "items": [], "body": []}
            sections.append(current)
            continue
        if current is None:
            summary_lines.append(line)
        elif is_bullet:
            current["items"].append(line[2:])
        else:
            current["body"].append(line)
    _trim_blank_edges(summary_lines if current is None else current["body"])

    entry: dict[str, Any] = {}
    summary = "\n".join(summary_lines)
    if summary:
        entry["summary"] = summary
    structured = []
    for section in sections:
        piece: dict[str, Any] = {"heading": section["heading"]}
        body = "\n".join(section["body"])
        if body:
            piece["body"] = body
        if section["items"]:
            piece["items"] = section["items"]
        structured.append(piece)
    if structured:
        entry["sections"] = structured
    if example_lines is not None:
        example = "\n".join(example_lines).strip("\n")
        if example:
            entry["example"] = example
    return entry


def render_entry(entry: dict[str, Any]) -> str:
    """Render one structured entry back into the full document text."""
    parts: list[str] = []
    summary = entry.get("summary", "")
    if summary:
        parts.append(summary)
    for section in entry.get("sections", []):
        block = section["heading"] + ":"
        body = section.get("body", "")
        if body:
            block += "\n" + body
        for item in section.get("items", []):
            block += "\n- " + item
        parts.append(block)
    example = entry.get("example", "")
    if example:
        parts.append(EXAMPLE_MARKER + "\n\n" + example)
    return "\n\n".join(parts)


def get_doc(module: str, symbol: str) -> str | None:
    """The rendered Chinese doc for one symbol, if present."""
    entry = get_entry(module, symbol)
    return None if entry is None else render_entry(entry)


def set_doc(module: str, symbol: str, doc: str) -> None:
    """Set one symbol's document from full text; call save() after."""
    set_entry(module, symbol, parse_blob(doc))


def get_entry(module: str, symbol: str) -> dict[str, Any] | None:
    """The structured entry for one symbol, if present."""
    return load_entries().get(module, {}).get(symbol)


def set_entry(module: str, symbol: str, entry: dict[str, Any]) -> None:
    """Set (or replace) one structured entry in the in-memory layer."""
    load_entries().setdefault(module, {})[symbol] = entry


def remove_doc(module: str, symbol: str) -> bool:
    """Drop one doc from the in-memory layer; returns whether it existed."""
    symbols = load_entries().get(module)
    if not symbols or symbol not in symbols:
        return False
    del symbols[symbol]
    if not symbols:
        load_entries().pop(module, None)
    return True


def save_docs() -> Path:
    """Write the in-memory layer back to the data file, atomically.

    Keys are sorted for stable diffs; the version stamp travels with the
    payload so future layout changes stay detectable.
    """
    payload: dict[str, Any] = {"version": FORMAT_VERSION, "docs": load_entries()}
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
        problems.append(f"version {payload.get('version')!r} != expected {FORMAT_VERSION}")
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
        for symbol, entry in symbols.items():
            if not isinstance(symbol, str) or not symbol:
                problems.append(f"{module}: non-string symbol {symbol!r}")
            problems.extend(_validate_entry(module, symbol, entry))
    return problems


def _validate_entry(module: str, symbol: str, entry: Any) -> list[str]:
    where = f"{module}.{symbol}"
    if not isinstance(entry, dict):
        return [f"{where}: entry must be an object"]
    problems = []
    unknown = sorted(set(entry) - _ENTRY_FIELDS)
    if unknown:
        problems.append(f"{where}: unknown field(s) {', '.join(unknown)}")
    summary = entry.get("summary")
    if summary is not None and not isinstance(summary, str):
        problems.append(f"{where}: summary must be a string")
    example = entry.get("example")
    if example is not None and (not isinstance(example, str) or not example.strip()):
        problems.append(f"{where}: example must be non-empty text")
    sections = entry.get("sections", [])
    if not isinstance(sections, list):
        problems.append(f"{where}: sections must be a list")
        sections = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            problems.append(f"{where}: section[{index}] must be an object")
            continue
        heading = section.get("heading")
        if not isinstance(heading, str) or not heading:
            problems.append(f"{where}: section[{index}] heading must be non-empty")
        items = section.get("items", [])
        if not isinstance(items, list) or any(not isinstance(i, str) for i in items):
            problems.append(f"{where}: section[{index}] items must be a string list")
            items = []
        body = section.get("body")
        if body is not None and not isinstance(body, str):
            problems.append(f"{where}: section[{index}] body must be a string")
        if not items and not body:
            problems.append(f"{where}: section[{index}] ({heading!r}) has no content")
    has_content = bool(
        (summary and summary.strip()) or example or any(
            isinstance(s, dict) and (s.get("items") or s.get("body"))
            for s in sections
        )
    )
    if not has_content:
        problems.append(f"{where}: entry must be non-empty")
    return problems


def stats() -> dict[str, Any]:
    """Counts for tooling: totals plus the per-module distribution."""
    entries = load_entries()
    docs = load_docs()
    per_module = {module: len(symbols) for module, symbols in sorted(entries.items())}
    total = sum(per_module.values())
    pitfall_symbols = 0
    pitfall_items = 0
    for symbols in entries.values():
        for entry in symbols.values():
            for section in entry.get("sections", []):
                if section.get("heading") == "陷阱":
                    pitfall_symbols += 1
                    pitfall_items += len(section.get("items", []))
    return {
        "modules": len(entries),
        "symbols": total,
        "characters": sum(len(doc) for symbols in docs.values() for doc in symbols.values()),
        "pitfall_symbols": pitfall_symbols,
        "pitfall_items": pitfall_items,
        "per_module": per_module,
    }
