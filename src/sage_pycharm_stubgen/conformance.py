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
import difflib
import importlib
import io
import re
import warnings
from dataclasses import dataclass, field
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
    origin: str | None = None  # "source" | "pyi" | "overload" | "factory" | None

    def as_json(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "qualname": self.qualname,
            "status": self.status,
            "curated": self.curated,
            "annotation": self.annotation,
            "origin": self.origin,
        }


def normalize_annotation(text: str) -> str:
    """Canonical rendering of an annotation so equivalent spellings compare equal.

    Cython C-level spellings of Python types (``Py_ssize_t``, ``bint``) map
    to their Python names; bare generic origins (``list``) are canonicalized
    with ``Any`` parameters so they compare against parameterized forms.
    """
    stripped = text.strip().strip("'\"")
    try:
        node = ast.parse(stripped, mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return re.sub(r"\s+", "", stripped)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return normalize_annotation(node.value)
    try:
        rendered = re.sub(r"\s+", "", ast.unparse(node))
    except (ValueError, RecursionError):
        return re.sub(r"\s+", "", stripped)
    mapped = _C_LEVEL_ALIASES.get(rendered, rendered)
    mapped = _BARE_GENERIC_WITH_ANY.get(mapped, mapped)
    # Module-qualified spellings of the same type (``numpy.ndarray`` in
    # curated vs ``ndarray`` in a stub that imports it; ``builtins.list``
    # qualifying a shadowed name — sometimes applied twice) never
    # distinguish declarations, so strip repeatedly until stable.
    previous = None
    while previous != mapped:
        previous = mapped
        mapped = re.sub(r"\bbuiltins\.(\w+)", r"\1", mapped)
        mapped = re.sub(r"\bnumpy\.(\w+)", r"\1", mapped)
    return mapped


#: Cython-only spellings whose Python meaning is the plain builtin.
_C_LEVEL_ALIASES = {
    "Py_ssize_t": "int",
    "ssize_t": "int",
    "size_t": "int",
    "bint": "bool",
}

#: A bare generic origin declared by upstream (``-> list``) is compatible
#: with any parameterization of the same origin — it is the same claim with
#: less precision, not a disagreement.
_BARE_GENERIC_WITH_ANY = {
    origin: f"{origin}[Any]"
    for origin in ("list", "set", "dict", "frozenset", "Sequence", "Iterable", "Iterator", "Collection")
}


def _declarations_match(declared: str, curated: str) -> bool:
    """True when the source declaration and the curated fix AGREE.

    Same canonical spelling, or the same generic origin with ``Any``
    parameters on one side only: upstream's bare ``-> list`` (Cython cannot
    spell parameters) with a curated ``list[Integer]`` is a precision
    difference, not a disagreement.  Both sides parameterized with
    different members (``list[str]`` vs ``list[Integer]``) still conflict.
    """
    left, right = normalize_annotation(declared), normalize_annotation(curated)
    if left == right:
        return True
    for a, b in ((left, right), (right, left)):
        m_a = re.fullmatch(r"(\w+)\[Any\]", a)
        m_b = re.fullmatch(r"(\w+)\[.+\]", b)
        if m_a and m_b and m_a.group(1) == m_b.group(1):
            return True
    return False


def find_source_file(package_root: Path, module: str) -> Path | None:
    """The shipped source (``.py`` or ``.pyx``) for a sage module, if present.

    Accepts both root conventions: the Python package root
    (``.../site-packages``) and the Sage package directory itself
    (``.../site-packages/sage``, what :func:`detect_sage_package` returns)
    — in the latter case the module's leading ``sage`` segment is not
    duplicated.
    """
    relative = Path(*module.split("."))
    candidates = [relative]
    if relative.parts and relative.parts[0] == package_root.name:
        candidates.append(Path(*relative.parts[1:]))
    for candidate in candidates:
        for suffix in (".py", ".pyx"):
            path = package_root / candidate.with_suffix(suffix)
            if path.is_file():
                return path
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
            # ``cdef`` declarations are C-level only — local variables inside
            # method bodies (``cdef Py_ssize_t row``) and C-only functions
            # never describe the Python API, and a local whose NAME collides
            # with a real method used to overwrite that method's annotation.
            if def_match.group("kind") == "cdef":
                index += 1
                continue
            rest = re.sub(r"^(?:inline|api|nogil)\s+", "", def_match.group("rest"))
            head = rest.split("(", 1)[0]
            # No ``(`` before any ``=`` → attribute/initializer, not a def.
            if "(" not in rest or "=" in head:
                index += 1
                continue
            name, return_type = _parse_cython_signature(rest)
            # Cython 3 also supports the arrow syntax on plain ``def``
            # methods (e.g. ``def f(x) -> X:``); the helper only parses the
            # Cython-style ``cpdef X f(...)`` form.
            if return_type is None and "->" in rest:
                arrow = rest.rsplit("->", 1)[1].split(":", 1)[0].strip()
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


# ---------------------------------------------------------------------------
# Upstream-coverage perception: overload stacks, sibling .pyi stubs, and
# factory-instance ``__call__`` class annotations.  Each produces
# ``qualname -> [(annotation, origin)]`` variants; a curated entry counts as
# covered when ANY variant matches (an overload's typed path is exactly how
# the polymorphic helpers were upstreamed).
# ---------------------------------------------------------------------------

def _is_overload(node: ast.FunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "overload":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "overload":
            return True
    return False


def _callable_return(annotation: ast.expr) -> str | None:
    """The ``X`` in a ``Callable[..., X]`` annotation, if that is the shape."""
    if isinstance(annotation, ast.Subscript):
        value = annotation.value
        name = value.id if isinstance(value, ast.Name) else (
            value.attr if isinstance(value, ast.Attribute) else None)
        if name == "Callable":
            args = annotation.slice
            elts = args.elts if isinstance(args, ast.Tuple) else [args]
            if len(elts) == 2:
                try:
                    return ast.unparse(elts[1])
                except Exception:
                    return None
    return None


def python_annotation_variants(
    source: Path, origin_prefix: str = "source"
) -> dict[str, list[tuple[str, str]]]:
    """``qualname -> [(annotation, origin)]`` for an ast-parseable file.

    Handles plain ``def`` returns, ``@overload`` stacks (each overload stub
    contributes its own variant, origin ``<prefix>-overload``), and the
    factory pattern: a module-level ``name = SomeClass(...)`` whose class
    declares a class-level ``__call__: Callable[..., X]`` contributes the
    ``X`` variant for ``name`` (origin ``<prefix>-factory``).  Used for both
    ``.py`` sources and ``.pyi`` stubs.
    """
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError):
        return {}
    parents = _parents(tree)
    found: dict[str, list[tuple[str, str]]] = {}

    def add(qualname: str, annotation: str, origin: str) -> None:
        found.setdefault(qualname, []).append((annotation, origin))

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.returns is not None:
            prefix = _qualified_prefix(node, parents)
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            origin = f"{origin_prefix}-overload" if _is_overload(node) else origin_prefix
            add(qualname, ast.unparse(node.returns), origin)

    # Module-level aliases (``Euler_Phi = euler_phi``, ``prime_factors =
    # prime_divisors``): the alias carries the target's declaration.
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        if not isinstance(stmt.value, ast.Name):
            continue
        alias, target = stmt.targets[0].id, stmt.value.id
        if alias == target or alias in found:
            continue
        for pairs in (found.get(target),):
            if pairs:
                found[alias] = [
                    (a, f"{o}-alias" if not o.endswith("-alias") else o)
                    for a, o in pairs
                ]
                break

    # Factory instances: module-level ``GF = FiniteFieldFactory(...)`` whose
    # class carries ``__call__: Callable[..., X]`` — the annotation form of
    # merged upstream PRs like sagemath/sage#42670.
    classes: dict[str, ast.ClassDef] = {
        n.name: n for n in tree.body if isinstance(n, ast.ClassDef)
    }
    imported_classes: dict[str, str] = {}  # local name -> class name
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                imported_classes[alias.asname or alias.name] = alias.name
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                continue
            call = stmt.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            cls_name = call.func.id
            cls = classes.get(cls_name)
            if cls is None and cls_name in imported_classes:
                # The class lives in another module of this package; look it
                # up there so the pattern also resolves across re-exports.
                cls = _find_class_in_module_dir(source, cls_name)
            if cls is None:
                continue
            for body in cls.body:
                if (
                    isinstance(body, ast.AnnAssign)
                    and isinstance(body.target, ast.Name)
                    and body.target.id == "__call__"
                ):
                    ret = _callable_return(body.annotation)
                    if ret:
                        add(target.id, ret, f"{origin_prefix}-factory")
                elif (
                    isinstance(body, ast.FunctionDef)
                    and body.name == "__call__"
                    and body.returns is not None
                ):
                    # The #43009 form: @overload-decorated forwarding
                    # __call__ methods on the factory class — each overload
                    # contributes a variant for the instance's calls.
                    origin = (
                        f"{origin_prefix}-factory-overload"
                        if _is_overload(body)
                        else f"{origin_prefix}-factory"
                    )
                    add(target.id, ast.unparse(body.returns), origin)
    return found


def _find_class_in_module_dir(source: Path, class_name: str) -> ast.ClassDef | None:
    """Best-effort class lookup among the sibling modules of ``source``."""
    try:
        for path in sorted(source.parent.glob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, SyntaxError):
                continue
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    return node
    except OSError:
        pass
    return None


def pyx_annotation_variants(source: Path) -> dict[str, list[tuple[str, str]]]:
    """Single-variant view of the Cython line-walk (origin ``source``)."""
    return {
        qualname: [(annotation, "source")]
        for qualname, annotation in source_annotations(source).items()
    }


def _stubgen_owned_stubs(module_file: Path) -> frozenset[Path]:
    """``.pyi`` paths this toolchain itself installed beside the source.

    Those stubs carry the curated fixes — matching against them is circular
    (the curated table agreeing with itself).  The in-place install manifest
    lists every file we own; stubs NOT in it are assumed to ship with Sage.

    The manifest sits at the installed Sage package root, which is an
    arbitrary number of levels above the module file (``sage/a/b/mod.py``
    needs three hops) — so every ancestor is checked, nearest first.  Both
    manifest generations are honored: ``files`` (list) and ``file_hashes``
    (dict, format 2+).
    """
    for directory in module_file.parents:
        manifest = directory / ".sage-pycharm-stubgen-in-place.json"
        if manifest.is_file():
            try:
                import json

                payload = json.loads(manifest.read_text(encoding="utf-8"))
                owned = list(payload.get("files", []))
                hashes = payload.get("file_hashes")
                if isinstance(hashes, dict):
                    owned.extend(hashes)
                return frozenset(
                    (directory / Path(raw)).resolve() for raw in owned
                )
            except (OSError, ValueError):
                return frozenset()
    return frozenset()


def _stub_candidates(module_file: Path, extra_roots: list[Path]) -> list[Path]:
    """``.pyi`` paths declaring this module: its own tree plus extra roots.

    The module's own tree contributes the sibling stub directly — UNLESS
    the in-place install manifest lists it (that stub is our own curated
    output; circular evidence).  Each extra root (e.g. a develop checkout —
    a *different* tree, never an ancestor) is resolved by the module's
    package-relative path under both root conventions (with and without the
    leading ``sage`` segment).
    """
    candidates: list[Path] = []
    owned = _stubgen_owned_stubs(module_file)
    sibling = module_file.with_suffix(".pyi")
    if sibling.is_file() and sibling.resolve() not in owned:
        candidates.append(sibling)
    try:
        relative = module_file.relative_to(module_file.parent.parent).with_suffix(".pyi")
    except ValueError:
        return candidates
    for root in extra_roots:
        for cand in (root / relative, root / "sage" / relative):
            if cand.is_file() and cand not in candidates:
                candidates.append(cand)
    return candidates


def _extra_tree_variants(
    module: str, qualname: str, extra_roots: list[Path]
) -> list[tuple[str, str]]:
    """Variants declared in an extra tree (a develop checkout preview).

    Considers both the ``.py`` source (including its overload stacks and
    factory ``__call__`` class annotations — merged upstream PRs) and any
    ``.pyi`` stub, under both root conventions.
    """
    parts = module.split(".")
    variants: list[tuple[str, str]] = []
    for root in extra_roots:
        for relative in (Path(*parts), Path(*parts[1:]) if parts[:1] == ["sage"] else None):
            if relative is None:
                continue
            for suffix, origin in ((".py", "tree"), (".pyi", "pyi")):
                path = root / relative.with_suffix(suffix)
                if not path.is_file():
                    continue
                pairs = python_annotation_variants(path, origin).get(qualname, [])
                variants.extend(pairs)
    return variants


def resolve_declaration(
    module: str,
    qualname: str,
    package_root: Path,
    extra_roots: list[Path] | None = None,
) -> list[tuple[str, str]]:
    """Every (annotation, origin) variant declared upstream for a symbol.

    Lookup order: the module's own source (``.py``/``.pyx``), the factory
    pattern in that source, then sibling ``.pyi`` stubs in the package tree
    and everything (source, overloads, factories, stubs) in each extra root
    (e.g. a develop checkout, previewing the post-merge state).
    """
    extra_roots = extra_roots or []
    variants: list[tuple[str, str]] = []
    source = find_source_file(package_root, module)
    if source is not None:
        if source.suffix == ".py":
            variants.extend(
                (a, o)
                for q, pairs in python_annotation_variants(source).items()
                if q == qualname
                for a, o in pairs
            )
        else:
            variants.extend(pyx_annotation_variants(source).get(qualname, []))
        for stub in _stub_candidates(source, extra_roots):
            variants.extend(python_annotation_variants(stub, "pyi").get(qualname, []))
    if extra_roots:
        variants.extend(_extra_tree_variants(module, qualname, extra_roots))
    # De-duplicate identical (annotation, origin) pairs, keep order.
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for pair in variants:
        if pair not in seen:
            seen.add(pair)
            unique.append(pair)
    return unique


def run_conformance(
    package_root: Path,
    curated: dict[str, dict[str, Any]] | None = None,
    extra_roots: list[Path] | None = None,
) -> list[ConformanceFinding]:
    """Compare every curated ``return`` fix against the upstream declaration.

    Coverage perception includes ``@overload`` stacks, sibling ``.pyi``
    stubs (in the package tree and any ``extra_roots``), and
    factory-instance ``__call__`` class annotations — matching findings
    carry the matching ``origin``.
    """
    curated = SUPPLEMENTAL_DOCS if curated is None else curated
    findings: list[ConformanceFinding] = []
    for module, entries in curated.items():
        for qualname, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            curated_return = entry.get("return")
            if not curated_return:
                continue
            variants = resolve_declaration(module, qualname, package_root, extra_roots)
            real = [(a, o) for a, o in variants if a and a != "Any"]
            if not real:
                findings.append(
                    ConformanceFinding(module, qualname, "unannotated", curated_return)
                )
                continue
            for declared, origin in real:
                if _declarations_match(declared, curated_return):
                    findings.append(
                        ConformanceFinding(
                            module, qualname, "ok", curated_return, declared, origin
                        )
                    )
                    break
            else:
                # A curated UNION is covered when every member matches some
                # upstream variant — exactly how the polymorphic helpers
                # were annotated upstream (one @overload per path).
                members = split_union(curated_return)
                if members and all(
                    any(_declarations_match(a, member) for a, _ in real)
                    for member in members
                ):
                    matched = next(
                        (o for a, o in real if _declarations_match(a, members[0])),
                        real[0][1],
                    )
                    findings.append(
                        ConformanceFinding(
                            module, qualname, "ok", curated_return, curated_return, matched
                        )
                    )
                    continue
                findings.append(
                    ConformanceFinding(
                        module, qualname, "conflict", curated_return, real[0][0], real[0][1]
                    )
                )
    findings.sort(key=lambda f: (f.status != "conflict", f.module, f.qualname))
    return findings


def prune_candidates(
    findings: list[ConformanceFinding],
) -> list[dict[str, Any]]:
    """Curated ``return`` entries made redundant by upstream coverage.

    Every ``ok`` finding with a non-None origin (matched in source, a
    ``.pyi`` stub, an overload variant, or a factory ``__call__``
    annotation) is a pruning candidate; the human reviews the list and
    deletes the entries.  ``unannotated`` and ``conflict`` findings never
    qualify.
    """
    return [
        {
            "module": f.module,
            "qualname": f.qualname,
            "curated": f.curated,
            "matched_declaration": f.annotation,
            "origin": f.origin,
        }
        for f in findings
        if f.status == "ok" and f.origin
    ]


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


@dataclass
class UpstreamExportResult:
    """Outcome of generating upstream-ready annotation patches."""

    patches: list[str] = field(default_factory=list)  # written patch paths
    annotated: int = 0  # annotations added across patches
    imports_added: int = 0
    skipped_compiled: list[str] = field(default_factory=list)  # .pyx-only modules
    skipped_annotated: list[str] = field(default_factory=list)  # source already declares
    skipped_conflict: list[str] = field(default_factory=list)  # needs human decision
    skipped_not_found: list[str] = field(default_factory=list)


#: Dunders an upstream stub legitimately declares.  The rest of the ``__``
#: namespace is implementation detail and stays out of PR material.
_EXPORT_DUNDER_ALLOWLIST = frozenset({
    "__all__",
    "__init__", "__call__", "__getitem__", "__setitem__", "__delitem__",
    "__iter__", "__len__", "__contains__", "__bool__", "__eq__", "__ne__",
    "__hash__", "__repr__", "__str__", "__enter__", "__exit__",
    "__aenter__", "__aexit__", "__neg__", "__pos__", "__abs__",
    "__invert__", "__add__", "__radd__", "__iadd__", "__sub__", "__rsub__",
    "__isub__", "__mul__", "__rmul__", "__imul__", "__truediv__",
    "__rtruediv__", "__itruediv__", "__floordiv__", "__rfloordiv__",
    "__ifloordiv__", "__mod__", "__rmod__", "__imod__", "__pow__",
    "__rpow__", "__ipow__", "__divmod__", "__rdivmod__", "__lshift__",
    "__rshift__", "__and__", "__rand__", "__or__", "__ror__", "__xor__",
    "__rxor__", "__matmul__", "__rmatmul__", "__int__", "__float__",
    "__index__",
})

#: Builtin *typing* names that must be qualified when the module binds the
#: same name to a non-class object (an alias/assign): as an annotation the
#: builtin collection is virtually always what the source meant.
_BUILTIN_TYPING_NAMES = ("list", "dict", "set", "frozenset", "tuple", "type")


def _exportable_name(name: str) -> bool:
    return not name.startswith("_") or name in _EXPORT_DUNDER_ALLOWLIST


def _stmt_names(stmt: ast.stmt) -> set[str]:
    """Top-level names a module statement binds."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    if isinstance(stmt, ast.Assign):
        return {t.id for t in stmt.targets if isinstance(t, ast.Name)}
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return {stmt.target.id}
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return set()


def _strip_docstrings(tree: ast.Module) -> None:
    """Remove every bare string-literal statement (upstream stubs stay lean).

    Docstrings usually sit first in a body, but stub enrichment also appends
    them after the import block — and a string expression is never behavior,
    so all of them go.  Bodies left empty get ``...``.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        kept = [
            stmt
            for stmt in node.body
            if not (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            )
        ]
        if not kept:
            kept = [ast.Expr(value=ast.Constant(value=Ellipsis))]
        node.body = kept


def _filter_private(tree: ast.Module) -> None:
    """Drop private members from the module and every class body.

    A class whose public members were all filtered keeps a ``...`` body —
    the class itself stays (it is importable API) but its private surface
    does not.
    """

    def filtered(body: list[ast.stmt]) -> list[ast.stmt]:
        kept: list[ast.stmt] = []
        for stmt in body:
            names = _stmt_names(stmt)
            if names and not any(_exportable_name(name) for name in names):
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                aliases = [
                    alias
                    for alias in stmt.names
                    if _exportable_name(alias.asname or alias.name.split(".")[0])
                ]
                if not aliases:
                    continue
                stmt = ast.ImportFrom(stmt.module, aliases, stmt.level) if isinstance(
                    stmt, ast.ImportFrom
                ) else ast.Import(aliases)
            if isinstance(stmt, ast.ClassDef):
                stmt.body = filtered(stmt.body) or [
                    ast.Expr(value=ast.Constant(value=Ellipsis))
                ]
            kept.append(stmt)
        return kept

    tree.body = filtered(tree.body)


def _drop_module_state_assigns(tree: ast.Module) -> int:
    """Drop module-level placeholder assignments from PR material.

    Converter placeholders (``set_integer_from_gen = None``,
    ``initialized = False``) describe the module's C globals, not its API —
    upstream they would mis-type real functions as ``None``.  Only CONSTANT
    bindings go: function/class aliases and ``TypeVar`` calls are public
    API and stay (an alias may even shadow a builtin, which the
    qualification pass then handles).
    """
    placeholder_types = (type(None), bool, int, float, str, bytes)
    kept: list[ast.stmt] = []
    dropped = 0
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) and isinstance(
            stmt.value.value, placeholder_types
        ):
            targets = {t.id for t in stmt.targets if isinstance(t, ast.Name)}
            if not targets & {"__all__"}:
                dropped += 1
                continue
        kept.append(stmt)
    tree.body = kept
    return dropped


def _qualify_shadowed_builtins(tree: ast.Module) -> int:
    """Qualify builtin collection annotations shadowed by non-class bindings.

    A module-level ``list = some_function`` makes a stub annotation ``list``
    resolve to the function; the collection is what was meant, so rewrite
    those annotation names to ``builtins.<name>``.  Shadowing via an actual
    class declaration is left alone — there the annotation means the class.
    """
    class_names = {
        stmt.name for stmt in tree.body if isinstance(stmt, ast.ClassDef)
    }
    bound = set()
    for stmt in tree.body:
        bound |= _stmt_names(stmt)
    shadowed = {
        name
        for name in _BUILTIN_TYPING_NAMES
        if name in bound and name not in class_names
    }
    if not shadowed:
        return 0

    def qualify(node: ast.expr) -> ast.expr:
        if isinstance(node, ast.Name) and node.id in shadowed:
            return ast.Attribute(value=ast.Name(id="builtins"), attr=node.id)
        return node

    changed = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                node.returns = qualify(node.returns)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            node.annotation = qualify(node.annotation)
        if isinstance(node, ast.arguments):
            for arg in [*node.args, *node.kwonlyargs, *node.posonlyargs]:
                if arg.annotation is not None:
                    arg.annotation = qualify(arg.annotation)
    # ast.walk does not re-yield rewritten parents; count via a second pass.
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
                if child.value.id == "builtins" and child.attr in shadowed:
                    changed += 1
    return changed


@dataclass
class PyiExportResult:
    """Outcome of exporting upstream-ready .pyi stubs for compiled modules."""

    files: list[str] = field(default_factory=list)
    defs_exported: int = 0
    builtin_qualifications: int = 0
    probe_files: list[str] = field(default_factory=list)  # assert_type companions
    probe_assertions: int = 0
    skipped_no_stub: list[str] = field(default_factory=list)  # stub not installed
    skipped_not_pyx: list[str] = field(default_factory=list)  # .py modules patch in-source
    skipped_no_entries: list[str] = field(default_factory=list)  # curated module w/o source
    warnings: list[str] = field(default_factory=list)


def export_pyi_stubs(
    package_root: Path,
    out_dir: Path,
    curated: dict[str, dict[str, Any]] | None = None,
) -> PyiExportResult:
    """Write upstream-ready ``.pyi`` stubs for curated compiled modules.

    The compiled-module answer to :func:`export_upstream_patches`: source
    patches cannot annotate ``.pyx`` methods (recompile required), but Sage
    merges ``.pyi`` stubs beside the source (rational.pyi / integer.pyi
    precedent).  The INSTALLED stub tree already carries the curated
    precision — docstrings stripped, private members dropped, builtin
    shadowing qualified — so each curated ``.pyx`` module becomes a
    PR-ready stub file.  Output layout mirrors the package
    (``DIR/sage/...``); the result is review material, never applied to the
    installed tree.
    """
    curated = SUPPLEMENTAL_DOCS if curated is None else curated
    out_dir.mkdir(parents=True, exist_ok=True)
    result = PyiExportResult()

    for module, entries in sorted(curated.items()):
        if not any(isinstance(entry, dict) for entry in entries.values()):
            continue
        source = find_source_file(package_root, module)
        if source is None:
            result.skipped_no_entries.append(module)
            continue
        if source.suffix != ".pyx":
            result.skipped_not_pyx.append(module)
            continue
        stub = source.with_suffix(".pyi")
        if not stub.is_file():
            result.skipped_no_stub.append(module)
            continue
        try:
            tree = ast.parse(stub.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as exc:
            result.warnings.append(f"{module}: stub unparseable ({exc})")
            continue

        _strip_docstrings(tree)
        _filter_private(tree)
        _drop_module_state_assigns(tree)
        result.builtin_qualifications += _qualify_shadowed_builtins(tree)

        header = "from __future__ import annotations"
        text = header + "\n\n" + ast.unparse(tree) + "\n"
        try:
            ast.parse(text)
        except SyntaxError:
            result.warnings.append(f"{module}: cleaned stub does not re-parse")
            continue
        defs = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        result.defs_exported += defs
        target = out_dir / Path(*module.split(".")).with_suffix(".pyi")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        result.files.append(str(target))

        probe = _render_type_probe(module, tree)
        if probe is not None:
            probe_text, assertions = probe
            probe_target = out_dir / "type_checking_probes" / (
                module.replace(".", "_") + "_returns.py"
            )
            probe_target.parent.mkdir(parents=True, exist_ok=True)
            validated, dropped = _validate_probe_file(probe_target, probe_text)
            if dropped and dropped != [-1]:
                if "assert_type(" not in validated:
                    result.warnings.append(
                        f"{module}: probe dropped entirely (no runnable line)"
                    )
                    continue
                result.warnings.append(
                    f"{module}: dropped {len(dropped)} probe line(s) that "
                    "failed under the live runtime"
                )
                assertions -= len(dropped)
            probe_target.write_text(validated, encoding="utf-8")
            result.probe_files.append(str(probe_target))
            result.probe_assertions += assertions
    return result


def _builtin_words() -> frozenset[str]:
    import builtins as builtins_module

    return frozenset(name for name in dir(builtins_module) if not name.startswith("_"))


def _render_type_probe(
    module: str, tree: ast.Module
) -> tuple[str, int] | None:
    """The upstream-style ``assert_type`` companion for an exported stub.

    Sage's acceptance pattern for a new ``.pyi`` is stub + meson entry + a
    ``type_checking`` probe of ``assert_type`` lines (rational_returns.py
    precedent).  Receivers come from the sampler's verified recipe table;
    annotation names are imported from the modules the STUB imports them
    from (matching the hand-written precedent's plain ``Integer`` style).
    ``None`` when no class has both a recipe and a precisely-annotated
    named method.
    """
    from .runtime_sample import RECEIVER_RECIPES

    recipes = dict(RECEIVER_RECIPES)
    builtin_words = _builtin_words()
    # Annotation-name -> defining sage module, from the stub's own imports.
    origin: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module and stmt.module.startswith("sage."):
            for alias in stmt.names:
                if alias.name != "*":
                    origin[alias.asname or alias.name] = stmt.module
    needed_imports: dict[str, set[str]] = {}
    lines: list[str] = []
    assertions = 0

    def qualify(annotation: str) -> str:
        def repl(match: re.Match[str]) -> str:
            word = match.group(1)
            module_of = origin.get(word)
            if module_of and word not in builtin_words:
                needed_imports.setdefault(module_of, set()).add(word)
            return word

        return re.sub(r"\b(\w+)\b", repl, annotation)

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        recipe = recipes.get(node.name)
        if recipe is None:
            continue
        annotated = [
            stmt
            for stmt in node.body
            if isinstance(stmt, ast.FunctionDef)
            and not stmt.name.startswith("_")
            and stmt.returns is not None
            # ``Any`` (alone or inside a union) asserts nothing — probes
            # carry precise returns only.
            and not re.search(r"\bAny\b", ast.unparse(stmt.returns))
        ]
        if not annotated:
            continue
        var = "r_" + node.name
        lines.append(f"cls = {node.name}")
        lines.append(f"{var} = {recipe}")
        for stmt in annotated:
            lines.append(f"assert_type({var}.{stmt.name}(), {qualify(ast.unparse(stmt.returns))})")
            assertions += 1
    if not lines:
        return None
    import_lines = [
        f"from {module_of} import {', '.join(sorted(names))}"
        for module_of, names in sorted(needed_imports.items())
    ]
    body = [
        "# Generated by sage-pycharm-stubgen conformance --export-pyi.",
        "# Review before PR: move to src/sage/tests/type_checking/ and add the",
        "# .pyi to meson.build; receivers reuse the sampler's verified recipes.",
        "from typing import assert_type",
        "",
        "from sage.all import *  # noqa: F401,F403 - sage test convention",
        f"from {module} import *  # noqa: F401,F403 - stubbed classes",
        *import_lines,
        "",
        *lines,
        "",
    ]
    return "\n".join(body), assertions


def _validate_probe_file(path: Path, source: str) -> tuple[str, list[int]]:
    """Execute a rendered probe under the live Sage runtime, dropping the
    ``assert_type`` lines that fail (stub signatures can lose required
    parameters; a probe line that cannot run is noise, not evidence).

    Returns the (possibly reduced) probe source and the dropped line
    numbers (1-based, in the ORIGINAL source).  When Sage is not
    importable the probe is returned untouched — validation is best
    effort and the unit-test environment has no Sage.
    """
    try:
        import sage.all  # noqa: F401
    except ImportError:
        return source, []

    current = source
    dropped: list[int] = []
    for _ in range(200):
        try:
            exec(compile(current, str(path), "exec"), {})
            return current, dropped
        except BaseException as exc:  # noqa: BLE001 - drop the failing line
            import traceback

            # The exception usually originates deep inside compiled code;
            # the probe's own frame is the deepest one in THIS file.
            probe_frames = [
                frame
                for frame in traceback.extract_tb(exc.__traceback__)
                if frame.filename == str(path)
            ]
            if not probe_frames:
                return source, [-1]  # failure outside the probe: keep as-is
            lineno = probe_frames[-1].lineno
            lines = current.splitlines()
            if not 1 <= lineno <= len(lines) or "assert_type" not in lines[lineno - 1]:
                return current, dropped + [-1]
            dropped.append(lineno)
            del lines[lineno - 1]
            current = "\n".join(lines) + "\n"
    return current, dropped


def _module_known_names(tree: ast.Module) -> set[str]:
    """Names importable at module scope: imports plus top-level defs/classes."""
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(statement.name)
        elif isinstance(statement, ast.Assign):
            names.update(t.id for t in statement.targets if isinstance(t, ast.Name))
    return names


def _annotation_needed_names(annotation: str) -> set[str]:
    try:
        tree = ast.parse(f"def _probe() -> {annotation}: ...\n")
    except SyntaxError:
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def export_upstream_patches(
    package_root: Path,
    out_dir: Path,
    curated: dict[str, dict[str, Any]] | None = None,
) -> UpstreamExportResult:
    """Generate upstream-ready patches adding curated return annotations.

    Only ``unannotated`` targets are exported: the source declares no return
    type, the curated table carries one, and (per the caller's conformance
    run) it is runtime-verified.  ``conflict`` findings need a human decision
    and are skipped, as are ``.pyx`` modules (annotations there require a
    full recompile — upstream adds those itself).

    Each patch inserts ``-> ReturnType`` on the def line and, when the
    annotation references a name the module does not provide, a matching
    from-import marked with a TODO so the PR author can confirm it does not
    introduce a circular import.  Patches are PR material: they are meant
    for the SageMath upstream, never applied to the installed tree (where
    evaluated annotations could break imports and a Sage upgrade would
    silently discard them).
    """
    curated = SUPPLEMENTAL_DOCS if curated is None else curated
    out_dir.mkdir(parents=True, exist_ok=True)
    result = UpstreamExportResult()

    for module, entries in sorted(curated.items()):
        source = find_source_file(package_root, module)
        if source is None:
            result.skipped_not_found.extend(
                f"{module}.{name}" for name, entry in entries.items()
                if isinstance(entry, dict) and entry.get("return")
            )
            continue
        if source.suffix == ".pyx":
            result.skipped_compiled.extend(
                f"{module}.{name}" for name, entry in entries.items()
                if isinstance(entry, dict) and entry.get("return")
            )
            continue

        try:
            text = source.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, UnicodeError, SyntaxError):
            result.skipped_not_found.extend(
                f"{module}.{name}" for name, entry in entries.items()
                if isinstance(entry, dict) and entry.get("return")
            )
            continue

        annotations = source_annotations(source)
        known = _module_known_names(tree)
        lines = text.splitlines()
        # (lineno 1-based of def header end, new tail, import lines) per edit
        edits: dict[int, str] = {}
        import_lines: list[str] = []
        for qualname, entry in sorted(entries.items()):
            if not isinstance(entry, dict):
                continue
            return_type = entry.get("return")
            if not return_type:
                continue
            full = f"{module}.{qualname}"
            declared = annotations.get(qualname)
            if declared and _declarations_match(declared, return_type):
                result.skipped_annotated.append(full)
                continue
            if declared:  # conflict: human decision required
                result.skipped_conflict.append(full)
                continue

            node = _locate_function(tree, qualname)
            if node is None:
                result.skipped_not_found.append(full)
                continue
            # The signature (not the body) carries the annotation colon: it
            # ends on the line before the first body statement — which is
            # also correct for one-liner suites (`def f(): ...`).
            signature_end = (
                node.body[0].lineno if node.body else (node.end_lineno or node.lineno)
            ) - 1
            tail_line = lines[signature_end - 1]
            colon = tail_line.rfind(":")
            if colon < 0:
                result.skipped_not_found.append(full)
                continue
            new_tail = tail_line[:colon] + f" -> {return_type}" + tail_line[colon:]
            edits[signature_end] = new_tail
            result.annotated += 1

            needed = {
                name
                for name in _annotation_needed_names(return_type)
                if name not in known
            }
            for import_line in entry.get("imports", []):
                match = re.fullmatch(r"from ([.\w]+) import (.+)", import_line.strip())
                if not match:
                    continue
                for part in match.group(2).split(","):
                    alias = re.fullmatch(r"(\w+)(?: as (\w+))?", part.strip())
                    if not alias:
                        continue
                    visible = alias.group(2) or alias.group(1)
                    if visible in needed and import_line.strip() not in import_lines:
                        import_lines.append(
                            import_line.strip()
                            + "  # TODO(upstream): confirm no circular import"
                        )

        if not edits:
            continue
        new_lines = list(lines)
        for lineno in sorted(edits, reverse=True):
            new_lines[lineno - 1] = edits[lineno]
        if import_lines:
            result.imports_added += len(import_lines)
            last_import = 0
            for statement in tree.body:
                if isinstance(statement, (ast.Import, ast.ImportFrom)):
                    last_import = max(last_import, statement.end_lineno or statement.lineno)
            if last_import == 0:
                for statement in tree.body:
                    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                        last_import = statement.end_lineno or statement.lineno
                        break
            for offset, line in enumerate(import_lines, start=1):
                new_lines.insert(last_import + offset - 1, line)
        new_text = "\n".join(new_lines) + "\n"
        # The patched source must still parse.
        try:
            ast.parse(new_text)
        except SyntaxError:
            result.annotated -= len(edits)
            result.skipped_not_found.extend(f"{module}.{q}" for q in edits)
            continue

        diff = difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{source.relative_to(package_root).as_posix()}",
            tofile=f"b/{source.relative_to(package_root).as_posix()}",
        )
        patch_path = out_dir / (module.replace(".", "_") + ".patch")
        patch_path.write_text("".join(diff), encoding="utf-8")
        result.patches.append(str(patch_path))
    return result


def _locate_function(tree: ast.Module, qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The module-level or class-body function declared under *qualname*."""
    parts = qualname.split(".")
    scope: list[ast.stmt] = tree.body
    node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        match = None
        for statement in scope:
            if last and isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == part:
                match = statement
                break
            if not last and isinstance(statement, ast.ClassDef) and statement.name == part:
                match = statement
                break
        if match is None:
            return None
        if last:
            node = match
        else:
            scope = match.body  # type: ignore[assignment]
    return node


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
