"""Product-level lint for the generated stub tree.

The installer only validates that a stub parses.  That is not enough:
the failure modes that actually reach the IDE are structural — a plain
``def`` after an ``@overload`` group shadows the overload set (``ZZ(5) *
QQ(...)`` then types as ``Integer``), duplicate members fight each
other, a same-named assignment recolours a method as a plain variable,
and curated entries that never landed are exactly how "some function
has no completion" slips through a green test run.

Every check runs on the FINAL artifact (after all enhance/enrich/merge
passes), not on the inputs.  Findings are errors (install must refuse)
or warnings (reported in the generation report).

Error kinds:

- ``overload-shadow``         -- an ``@overload`` group is followed by a
                                 plain def with the same name;
- ``duplicate-member``        -- two or more plain defs declare the same
                                 member;
- ``member-shadowed-by-assignment`` -- a def and an assignment declare
                                 the same name.

Warning kinds:

- ``unresolved-annotation``   -- an annotation references a name that is
                                 neither imported nor declared in the
                                 stub (the ``Integer`` overload set with
                                 a missing typing import is the known
                                 case);
- ``curated-unmatched``       -- a curated entry whose stub exists but
                                 does not declare the symbol;
- ``curated-stubless``        -- curated entries for modules that
                                 produce no stub (informational).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .docstring_enrich import (
    _BUILTIN_NAMES,
    _collect_imports,
    _local_names,
    _unmatched_curated_entries,
)


@dataclass(frozen=True)
class StubLintFinding:
    file: str  # path relative to the output root, posix style
    qualname: str
    kind: str
    severity: str  # "error" | "warning"
    detail: str

    def as_json(self) -> dict[str, str]:
        return {
            "file": self.file,
            "qualname": self.qualname,
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        return f"{self.severity}: {self.kind} {self.qualname} ({self.file}): {self.detail}"


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Name) and decorator.id == "overload"
        for decorator in node.decorator_list
    )


def _module_bound_names(tree: ast.AST) -> set[str]:
    """Every name bound at module level, assignments included.

    ``_local_names`` only covers defs/classes; ``T = TypeVar("T")`` must
    count as declared or every ``-> T`` annotation would be a finding.
    """
    names = _local_names(tree)
    for statement in tree.body:  # type: ignore[attr-defined]
        if isinstance(statement, ast.Assign):
            names.update(
                target.id for target in statement.targets if isinstance(target, ast.Name)
            )
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names.add(statement.target.id)
    return names


def _annotation_names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    return {
        item.id for item in ast.walk(node) if isinstance(item, ast.Name)
    }


def lint_source(text: str, relative: str) -> list[StubLintFinding]:
    """Lint one stub's source; *relative* names the file in findings."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [
            StubLintFinding(relative, "<module>", "unparsable", "error", str(exc))
        ]
    findings: list[StubLintFinding] = []
    known = _collect_imports(tree) | _module_bound_names(tree) | _BUILTIN_NAMES

    bodies: list[tuple[str, list[ast.stmt]]] = [("", tree.body)]  # type: ignore[attr-defined]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            prefix = node.name
            findings.extend(_annotation_gaps(node, prefix, relative, known))
            bodies.append((node.name, node.body))
    findings.extend(_annotation_gaps(tree, "", relative, known))  # type: ignore[arg-type]

    for owner, body in bodies:
        qualname_prefix = f"{owner}." if owner else ""
        defs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        assigned: dict[str, ast.stmt] = {}
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.setdefault(statement.name, []).append(statement)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        assigned.setdefault(target.id, statement)
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                assigned.setdefault(statement.target.id, statement)
        for name, statements in defs.items():
            qualname = f"{qualname_prefix}{name}"
            overloads = [item for item in statements if _is_overload(item)]
            plain = [item for item in statements if not _is_overload(item)]
            if overloads and plain:
                findings.append(
                    StubLintFinding(
                        relative,
                        qualname,
                        "overload-shadow",
                        "error",
                        f"{len(overloads)} overload(s) shadowed by {len(plain)} plain def(s)",
                    )
                )
            elif len(plain) > 1:
                findings.append(
                    StubLintFinding(
                        relative,
                        qualname,
                        "duplicate-member",
                        "error",
                        f"declared {len(plain)} times without @overload",
                    )
                )
            if statements and name in assigned:
                findings.append(
                    StubLintFinding(
                        relative,
                        qualname,
                        "member-shadowed-by-assignment",
                        "error",
                        "a def and an assignment declare the same name",
                    )
                )
    return findings


def _annotation_gaps(
    node: ast.ClassDef | ast.Module, prefix: str, relative: str, known: set[str]
) -> list[StubLintFinding]:
    """Report annotations referencing names the stub neither imports nor declares."""
    findings: list[StubLintFinding] = []
    body = node.body
    functions = [
        statement
        for statement in body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for function in functions:
        qualname = f"{prefix}.{function.name}" if prefix else function.name
        referenced = set(_annotation_names(function.returns))
        arguments = function.args
        for parameter in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            referenced |= _annotation_names(parameter.annotation)
        unresolved = sorted(name for name in referenced if name not in known)
        if unresolved:
            findings.append(
                StubLintFinding(
                    relative,
                    qualname,
                    "unresolved-annotation",
                    "warning",
                    "names neither imported nor declared: " + ", ".join(unresolved),
                )
            )
    return findings


def lint_generated_stubs(
    output_root: Path, check_curated: bool = True
) -> tuple[list[StubLintFinding], list[StubLintFinding]]:
    """Lint the final stub tree; returns (errors, warnings).

    ``check_curated`` controls whether curated-table coverage
    (``curated-unmatched`` / ``curated-stubless``) is included; tests
    against synthetic trees turn it off so the real curated table does
    not leak its coverage warnings into the findings.
    """
    root = output_root / "sage"
    errors: list[StubLintFinding] = []
    warnings: list[StubLintFinding] = []
    if root.is_dir():
        for stub in sorted(root.rglob("*.pyi")):
            relative = stub.relative_to(output_root).as_posix()
            try:
                text = stub.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for finding in lint_source(text, relative):
                (errors if finding.severity == "error" else warnings).append(finding)
    if check_curated:
        unmatched, stubless = _unmatched_curated_entries(output_root)
        warnings.extend(
            StubLintFinding(
                "sage_pycharm_stubgen/supplemental_docs.py",
                entry,
                "curated-unmatched",
                "warning",
                "curated entry did not land in the generated stub",
            )
            for entry in unmatched
        )
        warnings.extend(
            StubLintFinding(
                "sage_pycharm_stubgen/supplemental_docs.py",
                module,
                "curated-stubless",
                "warning",
                "curated module produces no stub file (pure-Python module)",
            )
            for module in stubless
        )
    return errors, warnings
