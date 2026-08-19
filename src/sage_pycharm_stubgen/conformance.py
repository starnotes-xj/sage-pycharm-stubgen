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

``run_runtime_checks`` adds a second, stronger layer: it *executes* the
doctest examples of every curated ``return`` entry against the live Sage
namespace and verifies that the runtime type of the probed call actually
fits the declared annotation (issubclass / generic origin / element
types).  A declaration that names a base class while the runtime returns a
concrete subclass (``lift_x`` returning ``EllipticCurvePoint_field`` while
declared as ``EllipticCurvePoint``) is reported as a ``mismatch`` -- the
exact class of drift that silently hides member completion.
"""

from __future__ import annotations

import ast
import collections.abc as collections_abc
import contextlib
import importlib
import io
import re
import warnings
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


@dataclass(frozen=True)
class RuntimeFinding:
    """One curated ``return`` entry probed against the live Sage runtime."""

    module: str
    qualname: str
    status: str  # "ok" | "mismatch" | "skipped"
    declared: str
    actual: str
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "qualname": self.qualname,
            "status": self.status,
            "declared": self.declared,
            "actual": self.actual,
            "detail": self.detail,
        }


_BUILTIN_TYPES: dict[str, type[Any]] = {
    "bool": bool,
    "bytearray": bytearray,
    "bytes": bytes,
    "complex": complex,
    "dict": dict,
    "float": float,
    "frozenset": frozenset,
    "int": int,
    "list": list,
    "set": set,
    "str": str,
    "tuple": tuple,
}

#: Generic origins that accept any concrete Sequence/Iterable/Iterator.
_ABSTRACT_ORIGINS = {"Iterable", "Iterator", "Sequence", "Collection"}


def split_union(annotation: str) -> list[str]:
    """The top-level union members of a stub annotation.

    ``A | B`` -> ``[A, B]``; ``|`` inside ``[...]`` (``list[Integer | int]``)
    is not a top-level union and stays inside its member.
    """
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(annotation):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "|" and depth == 0:
            parts.append(annotation[start:index].strip())
            start = index + 1
    parts.append(annotation[start:].strip())
    return parts


def doctest_lines(doc: str) -> list[str]:
    """The executable ``sage:`` / ``>>>`` example lines of a curated doc."""
    lines: list[str] = []
    for raw in doc.splitlines():
        stripped = raw.strip()
        for prompt in ("sage:", ">>>"):
            if stripped.startswith(prompt):
                lines.append(stripped[len(prompt) :].strip())
                break
    return lines


def _observed_type(value: Any) -> str:
    """Human-readable runtime type of a probed value (elements for containers)."""
    cls = type(value)
    name = f"{cls.__module__}.{cls.__name__}"
    if cls in (list, tuple, set, frozenset) and value:
        samples = [type(item).__name__ for item in list(value)[:3]]
        return f"{name}[{', '.join(samples)}...]"
    return name


def _split_generic(member: str) -> tuple[str, str] | None:
    """Split ``Name[Args...]`` with bracket-balance-aware argument extraction."""
    match = re.match(r"(\w+)\[", member)
    if not match:
        return None
    depth = 0
    for index in range(match.end() - 1, len(member)):
        char = member[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return match.group(1), member[match.end() : index]
    return None


def _resolve_declared_member(
    member: str, imports: list[str], env: dict[str, Any]
) -> tuple[str, Any]:
    """Resolve one union member of a stub annotation to a runtime checkable.

    Returns ``("class", cls)``, ``("generic", (origin_name, element_annotation))``,
    ``("any", None)`` or ``("unknown", member)``.  Names already present in
    the probe environment win (testability); the curated ``imports`` lines
    are the fallback, which also handles aliases (``... as _FieldBase``).
    """
    generic = _split_generic(member)
    if generic:
        return ("generic", generic)
    if member in env and isinstance(env[member], type):
        return ("class", env[member])
    if member in _BUILTIN_TYPES:
        return ("class", _BUILTIN_TYPES[member])
    if member == "Any":
        return ("any", None)
    if member in _ABSTRACT_ORIGINS:
        return ("typing", member)
    if "." in member:
        # Qualified name (e.g. ``numpy.ndarray``): resolve through importlib.
        module_name, _, type_name = member.rpartition(".")
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ValueError):
            module = None
        if module is not None:
            resolved = getattr(module, type_name, None)
            if isinstance(resolved, type):
                return ("class", resolved)
    for line in imports:
        from_match = re.fullmatch(r"from ([\.\w]+) import (.+)", line)
        if not from_match:
            continue
        for alias in from_match.group(2).split(","):
            alias_match = re.fullmatch(r"(\w+)(?: as (\w+))?", alias.strip())
            if not alias_match:
                continue
            source_name, as_name = alias_match.group(1), alias_match.group(2)
            if as_name != member and (as_name is None and source_name != member):
                continue
            try:
                module = importlib.import_module(from_match.group(1))
                resolved = getattr(module, source_name)
            except (ImportError, AttributeError, ValueError):
                resolved = env.get(source_name)
            if isinstance(resolved, type):
                return ("class", resolved)
    return ("unknown", member)


def _split_top_level(annotation: str) -> list[str]:
    """Split on commas outside any ``[...]`` (nested generics stay intact)."""
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(annotation):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(annotation[start:index].strip())
            start = index + 1
    parts.append(annotation[start:].strip())
    return parts


def check_actual_type(
    declared: str, actual: Any, imports: list[str], env: dict[str, Any]
) -> tuple[bool, str]:
    """True when the runtime type of *actual* fits the stub annotation.

    ``A | B`` accepts either member; ``list[X]`` / ``tuple[X, ...]`` check
    the origin plus up to the first five element types (recursively);
    ``Sequence[X]``-style abstract origins accept list/tuple/set.
    """
    for member in split_union(declared):
        kind, value = _resolve_declared_member(member, imports, env)
        if kind == "any":
            return True, member
        if kind == "unknown":
            # Same-module class the imports did not name: accept when the
            # runtime type's MRO carries the declared name (base-class
            # declarations like ``Matrix`` for concrete matrix classes are
            # fine as long as the name is on the MRO).
            if any(cls.__name__ == member for cls in type(actual).__mro__):
                return True, member
            continue
        if kind == "generic":
            origin_name, element_annotation = value
            actual_cls = type(actual)
            if origin_name in _ABSTRACT_ORIGINS:
                origin_ok = isinstance(actual, getattr(collections_abc, origin_name, object))
            else:
                origin_ok = actual_cls.__name__ == origin_name
            if not origin_ok:
                continue
            elements = list(actual)
            element_annotations = [
                part for part in _split_top_level(element_annotation) if part != "..."
            ]
            if not element_annotations:
                element_annotations = ["Any"]
            for element in elements[:5]:
                if not any(
                    check_actual_type(part, element, imports, env)[0]
                    for part in element_annotations
                ):
                    return False, f"{member} element mismatch ({type(element).__name__})"
            return True, member
        if kind == "typing":
            if type(actual) in (list, tuple, set, frozenset):
                return True, member
            continue
        # kind == "class"
        if issubclass(type(actual), value):
            return True, member
    return False, f"actual {_observed_type(actual)}"


def _is_top_level_call(call: str, after_open: int, target: str) -> bool:
    """True when the call is the statement's top-level expression.

    *after_open* is the index just past the call's opening ``(``.  Excludes
    wrapped calls (``list(v.items())`` -- an unclosed ``(`` before the
    call) and chained/subscripted calls (``Primes(...).next(500)``,
    ``E.gens()[0]`` -- a ``.`` or ``[`` right after the call's closing
    paren), whose runtime result type is not the target's own.
    """
    before = call[: after_open - len(target) - 1]
    if before.count("(") > before.count(")"):
        return False
    depth = 0
    index = after_open
    while index < len(call):
        char = call[index]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return not call[index + 1 :].lstrip().startswith((".", "["))
            depth -= 1
        index += 1
    return True


def _target_call_index(calls: list[str], target: str) -> int | None:
    """Index of the example line that calls *target*.

    Priority: direct call (``target(...)``), assignment whose RHS is the
    call (``Q = target(...)``), method call (``obj.target(...)``), then any
    occurrence (so ``polygen(GF(2))``-style *argument* uses never shadow a
    real ``GF(...)`` call when one exists).  Wrapped or chained calls are
    skipped (their result type is not the target's own).
    """
    patterns = (
        re.compile(rf"^{re.escape(target)}\("),
        re.compile(rf"^[\w.]+ = {re.escape(target)}\("),
        re.compile(rf"\.{re.escape(target)}\("),
        re.compile(rf"{re.escape(target)}\("),
    )
    for pattern in patterns:
        for index, call in enumerate(calls):
            for match in pattern.finditer(call):
                if _is_top_level_call(call, match.end(), target):
                    return index
    return None


def run_runtime_checks(
    package_root: Path,
    curated: dict[str, dict[str, Any]] | None = None,
    namespace: dict[str, Any] | None = None,
) -> list[RuntimeFinding]:
    """Probe every curated ``return`` entry against the live Sage runtime.

    For each entry the doctest examples are executed in order (output
    silenced); the first example line that calls the target method/name is
    eval'ed and its result type is checked against the declared annotation.
    Entries whose examples cannot run are reported as ``skipped`` (never a
    false positive); a runtime type that does not fit the declaration is a
    ``mismatch`` -- typically a base-class declaration hiding a concrete
    subclass, which silently degrades member completion.
    """
    curated = SUPPLEMENTAL_DOCS if curated is None else curated
    if namespace is None:
        import sage.all as sage_all

        namespace = dict(vars(sage_all))

    findings: list[RuntimeFinding] = []
    for module, entries in curated.items():
        for qualname, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            declared = entry.get("return")
            if not declared:
                continue
            calls = doctest_lines(entry.get("doc") or "")
            target = qualname.split(".")[-1] if "." in qualname else qualname
            target_index = _target_call_index(calls, target)
            if target_index is None:
                findings.append(
                    RuntimeFinding(
                        module, qualname, "skipped", declared, "", "no matching example call"
                    )
                )
                continue
            env = dict(namespace)
            try:
                for call in calls[:target_index]:
                    exec(call, env)
                target_line = calls[target_index]
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ), warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    assignment = re.match(r"^([\w.]+)\s*=\s*", target_line)
                    if assignment:
                        exec(target_line, env)
                        result = env.get(assignment.group(1))
                    else:
                        result = eval(target_line, env)
            except Exception as exc:  # noqa: BLE001 - probe failures are skipped
                findings.append(
                    RuntimeFinding(
                        module,
                        qualname,
                        "skipped",
                        declared,
                        "",
                        f"probe failed: {type(exc).__name__}",
                    )
                )
                continue
            if result is None:
                findings.append(
                    RuntimeFinding(
                        module, qualname, "skipped", declared, "", "probe returned None"
                    )
                )
                continue
            ok, detail = check_actual_type(
                declared, result, entry.get("imports") or [], env
            )
            findings.append(
                RuntimeFinding(
                    module,
                    qualname,
                    "ok" if ok else "mismatch",
                    declared,
                    _observed_type(result),
                    detail,
                )
            )
    findings.sort(key=lambda f: (f.status != "mismatch", f.module, f.qualname))
    return findings
