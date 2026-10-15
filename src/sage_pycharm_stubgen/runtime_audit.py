"""Runtime audit pass: reconcile generated stubs against the live Sage runtime.

Source-scanning generation (stubgen-pyx for Cython, AST conversion for pure
Python) systematically misses members that live in parent modules, members the
converter drops, and members only visible after a category join.  A measured
gap on 10.9: ``EllipticCurve_finite_field`` exposes 208 public members at
runtime while its stub declared 32; the point class 56 vs 8.

This pass imports every generated module in the live Sage interpreter, walks
each stub class, and appends declarations for public runtime members the stub
does not declare yet.  Signatures come from ``inspect`` (real parameters for
roughly nine of ten members; the rest fall back to ``(*args, **kwargs)``).
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
    modules_skipped: list[str] = dataclasses.field(default_factory=list)
    classes_skipped: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    @property
    def members_added(self) -> int:
        return self.methods_added + self.properties_added + self.attributes_added


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
) -> tuple[list[str], str, bool]:
    """Declaration lines for one missing member plus its kind.

    Returns ``(lines, kind, used_fallback)`` where kind is one of
    ``method``/``property``/``attribute``.
    """
    try:
        static = inspect.getattr_static(cls, name)
    except (AttributeError, TypeError, RuntimeError):
        static = None

    if isinstance(static, property):
        return ["@property", f"def {name}(self) -> Any: ..."], "property", False

    if static is not None and not callable(static) and not isinstance(
        static, (staticmethod, classmethod)
    ):
        return [f"{name}: Any = ..."], "attribute", False

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
            return [line], "method", False
        except (SyntaxError, ValueError, TypeError):
            pass

    fallback = _FALLBACK_DEF.format(name=name)
    return [fallback], "method", True


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
) -> tuple[str, RuntimeAuditSummary]:
    """Append missing public runtime members to every class in one stub.

    Returns the (possibly unchanged) new text and a per-file summary.  A
    re-parse or compile failure reverts to the input text and records the
    error — the caller never receives a half-audited file.
    """
    summary = RuntimeAuditSummary()
    tree = ast.parse(stub_text)

    edits: list[tuple[int, list[str], str, bool]] = []
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
            try:
                lines, kind, fallback = _render_member(name, runtime_cls)
            except Exception:  # noqa: BLE001 - a hostile __dir__/descriptor must not kill the audit
                continue
            if kind == "method":
                summary.methods_added += 1
            elif kind == "property":
                summary.properties_added += 1
            else:
                summary.attributes_added += 1
            if fallback:
                summary.fallback_signatures += 1
            new_lines.extend(indent + line for line in lines)
        if new_lines:
            insert_at = node.body[-1].end_lineno
            edits.append((insert_at, new_lines, node.name, False))

    if not edits:
        return stub_text, summary

    lines = stub_text.splitlines(keepends=True)
    for insert_at, new_lines, _name, _flag in sorted(edits, key=lambda e: e[0], reverse=True):
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
    # Some Sage modules parse sys.argv at import time and would mistake our
    # own CLI flags for sage CLI arguments, aborting the audit.  Hide them
    # for the duration; argv is restored no matter how the loop exits.
    saved_argv = list(sys.argv)
    sys.argv = [saved_argv[0] if saved_argv else "<sage-pycharm-stubgen>"]
    try:
        _audit_modules(stub_root, importer, summary)
    finally:
        sys.argv = saved_argv
    return summary


def _audit_modules(
    stub_root: Path,
    importer: Callable[[str], object],
    summary: RuntimeAuditSummary,
) -> None:
    for stub in sorted(stub_root.rglob("*.pyi")):
        module_name = stub_module_name(stub_root, stub)
        if module_name is None:
            continue
        try:
            runtime_module = importer(module_name)
        except Exception:  # noqa: BLE001 - optional-department imports are expected to fail
            summary.modules_skipped.append(module_name)
            continue

        original = stub.read_text(encoding="utf-8")
        new_text, file_summary = audit_stub_text(original, runtime_module)
        summary.methods_added += file_summary.methods_added
        summary.properties_added += file_summary.properties_added
        summary.attributes_added += file_summary.attributes_added
        summary.fallback_signatures += file_summary.fallback_signatures
        summary.classes_skipped.extend(f"{module_name}.{n}" for n in file_summary.classes_skipped)
        summary.errors.extend(f"{module_name}: {e}" for e in file_summary.errors)
        if new_text != original:
            stub.write_text(new_text, encoding="utf-8")
            summary.files_touched += 1
    return summary
