"""Runtime audit pass: reconcile generated stubs against the live Sage runtime.

Source-scanning generation (stubgen-pyx for Cython, AST conversion for pure
Python) systematically misses members that live in parent modules, members the
converter drops, and members only visible after a category join.  A measured
gap on 10.9: ``EllipticCurve_finite_field`` exposes 208 public members at
runtime while its stub declared 32; the point class 56 vs 8.

This pass imports every generated module in the live Sage interpreter, walks
each stub class, and appends declarations for public runtime members the stub
does not declare yet — on the class itself or anywhere in its stub-declared
base chain (0.9.7: MRO-aware; copying an inherited member onto every subclass
both bloated the stubs and let the flattened ``Any`` shadow a curated precise
return on an ancestor).

Signatures come from three tiers, first hit wins:

1. ``inspect.signature`` — real parameters for plain Python methods and
   Cython methods built with inspectable text signatures;
2. recovery — Sage's own ``sage_getdef`` docstring parsing, embedded-signature
   or ``sig:`` docstring blocks, and the defining ``.pyx`` source on disk;
3. ``(*args, **kwargs)`` placeholder — only when every tier fails.

Return types stay conservatively ``Any`` — precise returns remain the job of
the curated channel, because sampling real return values would require calling
user-visible methods with unknown side effects.

Declarations are appended at the END of each class body (flattened onto the
concrete class), so completion works in any IDE without MRO support.  The
audit runs after enrichment/merge/annotation-fix and before the lint gate;
every rewritten file is re-parsed and compiled, and a failure reverts that
file entirely (fail-closed, like the merge pass).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import re
import sys
from collections.abc import Callable
from pathlib import Path

from .docstring_enrich import _insert_imports, stub_module_name

_FALLBACK_DEF = "def {name}(self, *args, **kwargs) -> Any: ..."


@dataclasses.dataclass
class RuntimeAuditSummary:
    files_touched: int = 0
    methods_added: int = 0
    properties_added: int = 0
    attributes_added: int = 0
    fallback_signatures: int = 0
    signatures_recovered: int = 0
    inherited_skipped: int = 0
    modules_skipped: list[str] = dataclasses.field(default_factory=list)
    classes_skipped: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    @property
    def members_added(self) -> int:
        return self.methods_added + self.properties_added + self.attributes_added


# --------------------------------------------------------------------------
# stub-side class graph (MRO-aware skipping)
# --------------------------------------------------------------------------

class _StubHierarchy:
    """Class graph read from the generated stubs themselves.

    A runtime ``dir()`` on a subclass already lists inherited members; adding
    them onto the subclass stub is unnecessary when the ancestor stub chain
    declares them, because both PyCharm and the plugin index resolve members
    through the stub base edges.  Copying anyway let the flattened ``-> Any``
    shadow a curated precise return declared on an ancestor.
    """

    def __init__(self) -> None:
        self._declared: dict[tuple[str, str], set[str]] = {}
        self._bases: dict[tuple[str, str], list[tuple[str | None, str]]] = {}
        self._memo: dict[tuple[tuple[str, str], str], bool] = {}

    @classmethod
    def build(cls, stub_root: Path) -> _StubHierarchy:
        hierarchy = cls()
        for stub in sorted(stub_root.rglob("*.pyi")):
            module = _hierarchy_module_name(stub_root, stub)
            if module is None:
                continue
            try:
                tree = ast.parse(stub.read_text(encoding="utf-8"))
            except (SyntaxError, ValueError):
                continue
            imports = _module_import_map(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                key = (module, node.name)
                hierarchy._declared[key] = _declared_member_names(node)
                hierarchy._bases[key] = [
                    resolved
                    for resolved in (
                        _resolve_base(base, module, imports)
                        for base in node.bases
                    )
                    if resolved is not None
                ]
        return hierarchy

    def has_visible(self, module: str, classname: str, member: str) -> bool:
        """True when ``member`` is declared by ``classname`` or any stub base.

        Only the queried start key is memoized: caching intermediate nodes
        would be wrong on diamonds, where a sibling branch's declaration is
        not visible from every visited ancestor.
        """
        memo = self._memo
        start = ((module, classname), member)
        cached = memo.get(start)
        if cached is not None:
            return cached

        seen: set[tuple[str, str]] = set()
        pending = [(module, classname)]
        found = False
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            if member in self._declared.get(key, ()):
                found = True
                break
            for base_module, base_name in self._bases.get(key, ()):
                pending.append((base_module or module, base_name))
        memo[start] = found
        return found


def _hierarchy_module_name(stub_root: Path, stub: Path) -> str | None:
    relative = stub.relative_to(stub_root)
    parts = list(relative.with_suffix("").parts)
    if not parts or parts[0] != "sage":
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1] or ["sage"]
    return ".".join(parts)


def _module_import_map(tree: ast.AST) -> dict[str, str]:
    """Map locally-visible names to their dotted origin for base resolution."""
    mapping: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                mapping[local] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    mapping[alias.asname] = alias.name
                else:
                    mapping[alias.name.split(".")[0]] = alias.name
    return mapping


def _resolve_base(
    base: ast.expr,
    module: str,
    imports: dict[str, str],
) -> tuple[str | None, str] | None:
    dotted: str
    if isinstance(base, ast.Name):
        dotted = base.id
    elif isinstance(base, ast.Attribute):
        parts: list[str] = []
        node: ast.expr = base
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        dotted = ".".join(reversed(parts))
    else:
        return None
    head, _, tail = dotted.partition(".")
    origin = imports.get(head)
    if origin:
        full = origin + ("." + tail if tail else "")
        owner, _, name = full.rpartition(".")
        return (owner or None), name
    # Same-module class (or an unresolvable name — treated as a leaf edge,
    # which keeps the member copy on the safe side).
    return module, dotted.split(".")[0]


# --------------------------------------------------------------------------
# signature recovery (tier 2: docstrings and .pyx sources)
# --------------------------------------------------------------------------

def _extract_argument_list(text: str, name: str) -> str | None:
    """Pull the argument list out of a signature-like text fragment.

    Accepts ``def name(...)``, ``Class.name(...)``, bare ``(self, x)`` and
    Sage ``sig:`` block bodies; multi-line signatures are joined.
    """
    marker = f"{name}("
    position = text.find(marker)
    if position < 0:
        position = text.find("(")
        if position < 0:
            return None
    start = text.find("(", position)
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index].strip()
    return None


def _docstring_signature(target: object, name: str) -> str | None:
    doc = getattr(target, "__doc__", None)
    if not doc or not isinstance(doc, str):
        return None
    candidates: list[str] = []
    lines = doc.splitlines()
    for index, line in enumerate(lines):
        if line.strip() in {"sig:", "sig::"} and index + 1 < len(lines):
            candidates.append(lines[index + 1])
    if lines:
        candidates.append(lines[0])
        candidates.append(doc)
    for candidate in candidates:
        if not candidate:
            continue
        stripped = candidate.strip()
        if stripped.startswith("def "):
            stripped = stripped[4:]
        stripped = stripped.strip()
        # allow a dotted receiver prefix: `Class.name(self, x)` / `name(self, x)`
        if re.fullmatch(r"[A-Za-z_][\w.]*", stripped.split("(")[0].strip() or "x"):
            args = _extract_argument_list(stripped, name)
            if args is not None:
                return args
    return None


def _sage_getdef_signature(target: object) -> str | None:
    """Tier-2 recovery through Sage's own Cython docstring parser."""
    try:
        from sage.misc.sageinspect import sage_getdef
    except Exception:  # noqa: BLE001 - sage is optional in tests
        return None
    try:
        text = sage_getdef(target)
    except Exception:  # noqa: BLE001 - probe must never kill the audit
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.startswith("def "):
        cleaned = cleaned[4:]
    return cleaned.strip() or None


def _pyx_signature(cls: type, name: str) -> str | None:
    """Tier-2 recovery from the defining Cython source file on disk."""
    for ancestor in cls.__mro__:
        if name in vars(ancestor):
            defining = ancestor
            break
    else:
        return None
    module_name = getattr(defining, "__module__", "")
    if not module_name.startswith("sage."):
        return None
    try:
        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
    except Exception:  # noqa: BLE001
        return None
    if not module_file:
        return None
    pyx = Path(module_file).with_suffix(".pyx")
    if not pyx.is_file():
        return None
    try:
        text = pyx.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    class_name = (getattr(defining, "__qualname__", "") or "").split(".")[0]
    if class_name:
        class_match = re.search(
            rf"^\s*(?:cdef\s+)?(?:api\s+)?class\s+{re.escape(class_name)}\b",
            text,
            re.MULTILINE,
        )
        if class_match is None:
            return None
        search_text = text[class_match.start():]
        next_class = re.search(r"^\s*(?:cdef\s+)?class\s+", search_text[10:], re.MULTILINE)
        if next_class is not None:
            search_text = search_text[: 10 + next_class.start()]
    else:
        search_text = text
    member_match = re.search(rf"^\s*def\s+{re.escape(name)}\s*\(", search_text, re.MULTILINE)
    if member_match is None:
        return None
    args = _extract_argument_list(search_text[member_match.start():], name)
    if args is None:
        return None
    return args


def _recovered_argument_lists(target: object, name: str, cls: type):
    """Yield argument-list candidates from the recovery tiers, in order.

    The consumer validates each candidate by parsing the rendered def; an
    unparseable candidate from an early tier must not hide a good one from a
    later tier, so every tier runs.
    """
    tiers: list[Callable[[], str | None]] = [
        lambda: _extract_argument_list(_sage_getdef_signature(target) or "", name),
        lambda: _docstring_signature(target, name),
        lambda: _pyx_signature(cls, name),
    ]
    for tier in tiers:
        try:
            args = tier()
        except Exception:  # noqa: BLE001 - recovery is best-effort by contract
            continue
        if args:
            yield args


# --------------------------------------------------------------------------
# member rendering
# --------------------------------------------------------------------------

def _class_indent(class_node: ast.ClassDef) -> str:
    body = class_node.body
    if not body:
        return "    "
    return " " * body[0].col_offset


def _declared_member_names(class_node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _signature_parameters(attr: object) -> inspect.Signature | None:
    try:
        return inspect.signature(attr)  # type: ignore[arg-type]
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return None


def _strip_annotations(
    signature: inspect.Signature,
) -> list[inspect.Parameter]:
    return [
        parameter.replace(annotation=inspect.Parameter.empty)
        for parameter in signature.parameters.values()
    ]


def _normalize_first_parameter(
    parameters: list[inspect.Parameter],
    default_name: str,
) -> list[inspect.Parameter]:
    """Make the receiver explicit: keep a leading self/cls, otherwise add one.

    Plain functions accessed on their class already carry ``self``; Cython
    ``method_descriptor`` signatures usually do too (embedsignature), but
    bound classmethods and slots do not.  A wrong guess only costs a slightly
    off parameter hint — never a wrong member.
    """
    if parameters and parameters[0].name in {"self", "cls"}:
        return parameters
    receiver = inspect.Parameter(default_name, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return [receiver, *parameters]


def _render_def(name: str, parameters: list[inspect.Parameter]) -> str:
    signature = inspect.Signature(parameters)
    return f"def {name}{signature} -> Any: ..."


def _render_member(
    name: str,
    cls: type,
) -> tuple[list[str], str, str]:
    """Declaration lines for one missing member.

    Returns ``(lines, kind, quality)`` where kind is one of
    ``method``/``property``/``attribute`` and quality one of
    ``inspect``/``recovered``/``fallback``.
    """
    try:
        static = inspect.getattr_static(cls, name)
    except (AttributeError, TypeError, RuntimeError):
        static = None

    if isinstance(static, property):
        return ["@property", f"def {name}(self) -> Any: ..."], "property", "inspect"

    if static is not None and not callable(static) and not isinstance(
        static, (staticmethod, classmethod)
    ):
        return [f"{name}: Any = ..."], "attribute", "inspect"

    target: object = static
    receiver_name = "self"
    if isinstance(static, staticmethod):
        target = static.__func__
        receiver_name = ""
    elif isinstance(static, classmethod):
        target = static.__func__
        receiver_name = "cls"

    signature = _signature_parameters(target)
    if signature is not None:
        parameters = _strip_annotations(signature)
        if receiver_name:
            parameters = _normalize_first_parameter(parameters, receiver_name)
        try:
            line = _render_def(name, parameters)
            ast.parse(line)
            return [line], "method", "inspect"
        except (SyntaxError, ValueError, TypeError):
            pass

    for args in _recovered_argument_lists(target, name, cls):
        line = f"def {name}({args}) -> Any: ..."
        try:
            ast.parse(line)
            return [line], "method", "recovered"
        except (SyntaxError, ValueError):
            continue

    return [_FALLBACK_DEF.format(name=name)], "method", "fallback"


def _uses_any(text: str) -> bool:
    return "Any" in text


def _has_typing_any_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                if alias.name == "Any":
                    return True
    return False


def audit_stub_text(
    stub_text: str,
    runtime_module: object,
    *,
    module_name: str = "",
    hierarchy: _StubHierarchy | None = None,
) -> tuple[str, RuntimeAuditSummary]:
    """Append missing public runtime members to every class in one stub.

    Returns the (possibly unchanged) new text and a per-file summary.  A
    re-parse or compile failure reverts to the input text and records the
    error — the caller never receives a half-audited file.
    """
    summary = RuntimeAuditSummary()
    tree = ast.parse(stub_text)

    edits: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.body or node.body[0].lineno == node.lineno:
            summary.classes_skipped.append(node.name)
            continue
        runtime_cls = getattr(runtime_module, node.name, None)
        if runtime_cls is None or not inspect.isclass(runtime_cls):
            summary.classes_skipped.append(node.name)
            continue
        declared = _declared_member_names(node)
        indent = _class_indent(node)
        new_lines: list[str] = []
        for name in sorted(n for n in dir(runtime_cls) if not n.startswith("_")):
            if name in declared:
                continue
            if hierarchy is not None and hierarchy.has_visible(module_name, node.name, name):
                summary.inherited_skipped += 1
                continue
            try:
                lines, kind, quality = _render_member(name, runtime_cls)
            except Exception:  # noqa: BLE001 - a hostile __dir__/descriptor must not kill the audit
                continue
            if kind == "method":
                summary.methods_added += 1
            elif kind == "property":
                summary.properties_added += 1
            else:
                summary.attributes_added += 1
            if quality == "fallback":
                summary.fallback_signatures += 1
            elif quality == "recovered":
                summary.signatures_recovered += 1
            new_lines.extend(indent + line for line in lines)
        if new_lines:
            insert_at = node.body[-1].end_lineno
            edits.append((insert_at, new_lines))

    if not edits:
        return stub_text, summary

    lines = stub_text.splitlines(keepends=True)
    for insert_at, new_lines in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[insert_at:insert_at] = [line if line.endswith("\n") else line + "\n" for line in new_lines]

    new_text = "".join(lines)
    try:
        new_tree = ast.parse(new_text)
        compile(new_text, "<stub>", "exec")
    except (SyntaxError, ValueError) as exc:
        summary.errors.append(f"revert: {exc}")
        return stub_text, summary

    if _uses_any(new_text) and not _has_typing_any_import(new_tree):
        _insert_imports(lines, new_tree, ["from typing import Any\n"])
        new_text = "".join(lines)
        try:
            ast.parse(new_text)
            compile(new_text, "<stub>", "exec")
        except (SyntaxError, ValueError) as exc:
            summary.errors.append(f"revert after import insert: {exc}")
            return stub_text, summary

    return new_text, summary


def audit_stub_tree(
    stub_root: Path,
    *,
    importer: Callable[[str], object] = importlib.import_module,
) -> RuntimeAuditSummary:
    """Audit every generated stub under ``stub_root`` against the live runtime.

    ``importer`` is injectable so tests can run against fabricated modules.
    Modules that fail to import (optional dependencies) are skipped and
    reported, never fatal.
    """
    summary = RuntimeAuditSummary()
    hierarchy = _StubHierarchy.build(stub_root)
    # Some Sage modules parse sys.argv at import time and would mistake our
    # own CLI flags for sage CLI arguments, aborting the audit.  Hide them
    # for the duration; argv is restored no matter how the loop exits.
    saved_argv = list(sys.argv)
    sys.argv = [saved_argv[0] if saved_argv else "<sage-pycharm-stubgen>"]
    try:
        _audit_modules(stub_root, importer, summary, hierarchy)
    finally:
        sys.argv = saved_argv
    return summary


def _audit_modules(
    stub_root: Path,
    importer: Callable[[str], object],
    summary: RuntimeAuditSummary,
    hierarchy: _StubHierarchy,
) -> None:
    for stub in sorted(stub_root.rglob("*.pyi")):
        module_name = stub_module_name(stub_root, stub)
        if module_name is None:
            continue
        # sage.cli.__main__ and friends EXECUTE their CLI at import time;
        # with a sanitized argv that means an interactive REPL.  A stub for
        # a __main__ module is static documentation only — never import it.
        if module_name.rpartition(".")[2] == "__main__":
            continue
        try:
            runtime_module = importer(module_name)
        except Exception:  # noqa: BLE001 - optional-department imports are expected to fail
            summary.modules_skipped.append(module_name)
            continue

        original = stub.read_text(encoding="utf-8")
        new_text, file_summary = audit_stub_text(
            original,
            runtime_module,
            module_name=module_name,
            hierarchy=hierarchy,
        )
        summary.methods_added += file_summary.methods_added
        summary.properties_added += file_summary.properties_added
        summary.attributes_added += file_summary.attributes_added
        summary.fallback_signatures += file_summary.fallback_signatures
        summary.signatures_recovered += file_summary.signatures_recovered
        summary.inherited_skipped += file_summary.inherited_skipped
        summary.classes_skipped.extend(f"{module_name}.{n}" for n in file_summary.classes_skipped)
        summary.errors.extend(f"{module_name}: {e}" for e in file_summary.errors)
        if new_text != original:
            stub.write_text(new_text, encoding="utf-8")
            summary.files_touched += 1
