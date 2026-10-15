"""Final merge pass: every stub member is declared exactly once.

Multiple independent writers decorate a generated stub — the stubgen-pyx
converter, curated ``declare`` entries (appended at the end of a class
body), the ``enhance_*`` patchers (inserted at the top), and the method
alias promoter.  Each knows only its own declarations, so the same member
can reach the final file twice; a plain ``def`` after an ``@overload``
group silently shadows the whole overload set (``ZZ(5) * QQ(...)`` then
types as ``Integer``).

This module is the single reconciliation point that runs after every
writer.  It never invents declarations — it only picks one winner per
name and deletes the losers:

- an ``@overload`` group wins over any plain ``def`` or assignment with
  the same name (the group is kept atomically, all of it);
- among plain ``def`` duplicates, the best-annotated one wins (return
  annotation, then annotated parameters); ties prefer the later
  declaration, which matches Python's shadowing semantics;
- a ``def`` wins over a same-named assignment (the assignment renders
  the member as a plain variable in PyCharm);
- duplicated assignments keep the last one; duplicated classes keep the
  later declaration (shadowing semantics).

A multi-target assignment (``GF = FiniteField = X``) that collides on
only ONE of its names is trimmed to the surviving targets instead of
being deleted, so no unrelated binding is lost as collateral.

Deletions are computed on the AST and applied bottom-up; the result must
re-parse or the file is left untouched (fail closed).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MergeResult:
    changed_files: int = 0
    removed_declarations: int = 0


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Name) and decorator.id == "overload"
        for decorator in node.decorator_list
    )


def _statement_start(node: ast.stmt) -> int:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node.decorator_list:
            return min(decorator.lineno for decorator in node.decorator_list)
    return node.lineno


def _bound_name(node: ast.stmt) -> list[str]:
    """Top-level names a statement declares in its enclosing body."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [
            target.id for target in node.targets if isinstance(target, ast.Name)
        ]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _def_score(node: ast.FunctionDef | ast.AsyncFunctionDef, order: int) -> tuple:
    arguments = node.args
    parameters = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
    ]
    annotated = sum(1 for parameter in parameters if parameter.annotation is not None)
    return (node.returns is not None, annotated, order)


def _plan_body(
    body: list[ast.stmt],
) -> tuple[set[int], dict[int, set[str]]]:
    """Statements to remove and per-statement names to trim for one body.

    Keys are ``id(statement)``.  A statement whose bound names are ALL
    losers is removed; a multi-target assignment losing only some names is
    reported for trimming.
    """
    by_name: dict[str, list[tuple[int, ast.stmt]]] = {}
    for order, statement in enumerate(body):
        for name in _bound_name(statement):
            by_name.setdefault(name, []).append((order, statement))

    remove: set[int] = set()
    trim: dict[int, set[str]] = {}
    for name, statements in by_name.items():
        if len(statements) <= 1:
            continue
        defs = [
            (order, statement)
            for order, statement in statements
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if defs:
            overloads = [item for item in defs if _is_overload(item[1])]
            if overloads:
                winners = {id(statement) for _order, statement in overloads}
            else:
                winner = max(defs, key=lambda item: _def_score(item[1], item[0]))
                winners = {id(winner[1])}
            losers = statements
        else:
            # Assignments (or classes) only: the last binding wins.
            winners = {id(statements[-1][1])}
            losers = statements[:-1]
        for _order, statement in losers:
            if id(statement) in winners:
                continue
            names = set(_bound_name(statement))
            if names - {name}:
                trim.setdefault(id(statement), set()).add(name)
            else:
                remove.add(id(statement))
    return remove, trim


def _trim_assignment_line(line: str, statement: ast.Assign, dropped: set[str]) -> str | None:
    """The assignment line with the dropped chain targets removed.

    ``GF = FiniteField = X`` losing ``GF`` becomes ``FiniteField = X`` —
    the surviving text is taken verbatim from the first kept target's
    column, so the value and the rest of the chain are preserved exactly.
    Returns None when the statement spans several lines (left untouched)
    or no target survives (the caller removes the statement instead).
    """
    if statement.lineno != statement.end_lineno:
        return None
    kept = [
        target
        for target in statement.targets
        if isinstance(target, ast.Name) and target.id not in dropped
    ]
    if not kept:
        return None
    return line[kept[0].col_offset :].rstrip()


def _collect_bodies(tree: ast.AST) -> list[list[ast.stmt]]:
    bodies: list[list[ast.stmt]] = [tree.body]  # type: ignore[attr-defined]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bodies.append(node.body)
    return bodies


def merge_source(text: str) -> tuple[str, int]:
    """Deduplicate declarations in one stub's source; returns (text, removed)."""
    tree = ast.parse(text)
    spans: list[tuple[int, int]] = []
    replacements: dict[int, str] = {}
    changed = 0
    for body in _collect_bodies(tree):
        remove, trim = _plan_body(body)
        for statement in body:
            if id(statement) in remove:
                spans.append(
                    (_statement_start(statement), statement.end_lineno or statement.lineno)
                )
                changed += 1
            elif id(statement) in trim:
                dropped = trim[id(statement)]
                if not isinstance(statement, ast.Assign):
                    continue  # defensive: trim only applies to Assign
                line = text.splitlines()[statement.lineno - 1]
                replacement = _trim_assignment_line(line, statement, dropped)
                if replacement is None:
                    spans.append(
                        (
                            _statement_start(statement),
                            statement.end_lineno or statement.lineno,
                        )
                    )
                else:
                    replacements[statement.lineno] = replacement
                changed += 1
    if not changed:
        return text, 0
    # A redundant statement nested inside another redundant statement (a
    # duplicate class whose members are also duplicated) must not be
    # deleted twice: keep only the outermost span.
    outer: list[tuple[int, int]] = []
    for start, end in spans:
        if any(
            other_start <= start and end <= other_end and (other_start, other_end) != (start, end)
            for other_start, other_end in spans
        ):
            continue
        outer.append((start, end))
    lines = text.splitlines()
    for lineno, replacement in replacements.items():
        lines[lineno - 1] = replacement
    for start, end in sorted(outer, reverse=True):
        del lines[start - 1 : end]
    merged = "\n".join(lines) + "\n"
    ast.parse(merged)  # fail closed: a non-parsing result is never written
    return merged, changed


def merge_stub_file(path: Path) -> int:
    """Rewrite one ``.pyi`` with duplicate declarations removed."""
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return 0
    newline = "\r\n" if "\r\n" in original else "\n"
    try:
        merged, removed = merge_source(original)
    except (SyntaxError, ValueError, RecursionError):
        return 0
    if removed == 0 or merged == original:
        return 0
    normalized = merged.replace("\n", newline) if newline != "\n" else merged
    path.write_text(normalized, encoding="utf-8")
    return removed


def merge_generated_stubs(output_root: Path) -> MergeResult:
    """Run the merge pass over every generated stub under ``output_root/sage``."""
    root = output_root / "sage"
    if not root.is_dir():
        return MergeResult()
    changed = 0
    removed_total = 0
    for stub in sorted(root.rglob("*.pyi")):
        removed = merge_stub_file(stub)
        if removed:
            changed += 1
            removed_total += removed
    return MergeResult(changed, removed_total)
