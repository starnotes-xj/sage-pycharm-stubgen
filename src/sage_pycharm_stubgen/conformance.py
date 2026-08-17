"""Conformance: cross-check the data layer against source-declared annotations.

The curated table (``supplemental_docs``) patches return types that static
analysis cannot discover.  As upstream Sage gains real annotations (the
factory-annotation PRs), those curated patches become *checkable claims*:
this module compares every curated ``return`` fix against the annotation
declared in the installed Sage source and reports:

- ``unannotated`` -- the source declares nothing; the curated fix is still
  load-bearing (nothing to do today),
- ``ok``         -- the curated fix matches the source declaration,
- ``conflict``   -- the two disagree; either the curated fix is stale/wrong
  (fix the data layer) or the upstream annotation is wrong (a candidate for
  the next upstream PR).

This is the "use types to verify the guesses" mode: every upstream
annotation that lands converts a guess into a checked claim, and every
conflict is a concrete, reviewable finding instead of silent drift.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .docstring_enrich import (
    _CLASS_RE,
    _DEF_RE,
    _parents,
    _parse_cython_signature,
    _qualified_prefix,
)
from .supplemental_docs import SUPPLEMENTAL_DOCS


@dataclass(frozen=True)
class ConformanceFinding:
    module: str
    qualname: str
    status: str  # "ok" | "conflict" | "unannotated"
    curated: str
    annotation: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "qualname": self.qualname,
            "status": self.status,
            "curated": self.curated,
            "annotation": self.annotation,
        }


def normalize_annotation(text: str) -> str:
    """Canonical rendering of an annotation so equivalent spellings compare equal."""
    try:
        node = ast.parse(text.strip(), mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return re.sub(r"\s+", "", text.strip().strip("'\""))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    try:
        return re.sub(r"\s+", "", ast.unparse(node))
    except (ValueError, RecursionError):
        return re.sub(r"\s+", "", text.strip())


def find_source_file(package_root: Path, module: str) -> Path | None:
    """The shipped source (``.py`` or ``.pyx``) for a sage module, if present."""
    relative = Path(*module.split("."))
    for suffix in (".py", ".pyx"):
        candidate = package_root / relative.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def source_annotations(source: Path) -> dict[str, str]:
    """Return ``qualname -> return annotation`` for one source file.

    Pure-Python files are parsed with ``ast`` (real ``-> X`` annotations);
    Cython ``.pyx`` files reuse the docstring extractor's regex machinery so
    ``def``/``cpdef``/``cdef`` signatures are all captured.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}

    if source.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return {}
        parents = _parents(tree)
        found: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.returns is not None:
                prefix = _qualified_prefix(node, parents)
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                found[qualname] = ast.unparse(node.returns)
        return found

    # Cython source: line-based walk mirroring extract_docstrings.
    lines = text.splitlines()
    found: dict[str, str] = {}
    class_stack: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        class_match = _CLASS_RE.match(line)
        if class_match:
            indent = len(class_match.group(1))
            name = class_match.group(2)
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            class_stack.append((indent, name))
            index += 1
            continue
        def_match = _DEF_RE.match(line)
        if def_match:
            indent = len(def_match.group("indent"))
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            name, return_type = _parse_cython_signature(def_match.group("rest"))
            # Cython 3 also supports the arrow syntax on plain ``def``
            # methods (e.g. ``def f(x) -> X:``); the helper only parses the
            # Cython-style ``cpdef X f(...)`` form.
            if return_type is None and "->" in def_match.group("rest"):
                arrow = def_match.group("rest").rsplit("->", 1)[1].split(":", 1)[0].strip()
                if arrow:
                    return_type = arrow
            if name.isidentifier() and return_type:
                qualname = (
                    ".".join(item[1] for item in class_stack) + "." + name
                    if class_stack
                    else name
                )
                found[qualname] = return_type
            index += 1
            continue
        index += 1
    return found


def count_source_annotations(package_root: Path) -> int:
    """Total annotated callables across the shipped sage source tree."""
    total = 0
    for suffix in ("*.py", "*.pyx"):
        for source in package_root.rglob(suffix):
            total += len(source_annotations(source))
    return total


def run_conformance(
    package_root: Path,
    curated: dict[str, dict[str, Any]] | None = None,
) -> list[ConformanceFinding]:
    """Compare every curated ``return`` fix against the source declaration."""
    curated = SUPPLEMENTAL_DOCS if curated is None else curated
    findings: list[ConformanceFinding] = []
    for module, entries in curated.items():
        source = find_source_file(package_root, module)
        annotations = source_annotations(source) if source is not None else {}
        for qualname, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            curated_return = entry.get("return")
            if not curated_return:
                continue
            declared = annotations.get(qualname)
            if not declared or declared == "Any":
                findings.append(
                    ConformanceFinding(module, qualname, "unannotated", curated_return)
                )
                continue
            status = (
                "ok"
                if normalize_annotation(declared) == normalize_annotation(curated_return)
                else "conflict"
            )
            findings.append(
                ConformanceFinding(module, qualname, status, curated_return, declared)
            )
    findings.sort(key=lambda f: (f.status != "conflict", f.module, f.qualname))
    return findings
