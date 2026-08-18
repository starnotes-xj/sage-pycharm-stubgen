"""Render a pure-Python module (``.py``) into a ``.pyi`` stub.

The whole ``sage`` tree ships two kinds of modules:

- compiled Cython modules (``*.pyx`` sources) — stubbed via ``stubgen_pyx``;
- pure-Python modules (``*.py``) — **not stubbed at all** by the historical
  pipeline, so every function defined in them (``prime_divisors`` in
  ``sage/arith/misc.py``, most of combinatorics/graphs, ...) has no return
  type and every element chain through them (``for q in prime_divisors(p)``
  → ``q.nbits()``) is untyped.

This module closes that gap: parse the ``.py`` with :mod:`ast`, keep the
module structure verbatim (imports, assignments, ``__all__``, module-level
control flow), strip every function/method BODY down to its docstring (or
``...``), and re-emit with :func:`ast.unparse`.  The output is a complete,
valid ``.pyi`` — PyCharm prefers it over the ``.py``, so the stub MUST keep
every public def/class; nothing is dropped except bodies.  The result then
flows through the same enrichment pass (curated return types + translated
docstrings) as the compiled-module stubs.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_MAX_LINE_LENGTH = 88


def _is_main_guard(node: ast.If) -> bool:
    """True for `if __name__ == "__main__":` (also `!=` with a leading `else`)."""
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    left, ops, comparators = test.left, test.ops, test.comparators
    if len(ops) != 1 or len(comparators) != 1:
        return False
    name = left
    if not isinstance(name, ast.Name) or name.id != "__name__":
        return False
    if isinstance(comparators[0], ast.Constant) and isinstance(comparators[0].value, str):
        return comparators[0].value == "__main__"
    return False


def _docstring_expr(node: ast.AST) -> ast.Expr | None:
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
        return node.body[0]
    return None


def _strip_body(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> ast.AST:
    """Keep the signature; replace the body with the docstring or `...`."""
    doc = _docstring_expr(node)
    node.body = [doc] if doc is not None else [ast.Expr(ast.Constant(Ellipsis))]
    return node


class _PureStubRenderer(ast.NodeTransformer):
    """Module-level pass: strip function bodies; recurse into classes."""

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self.generic_visit(node)
        kept: list[ast.stmt] = []
        for child in node.body:
            if isinstance(child, ast.If) and _is_main_guard(child):
                continue
            kept.append(child)
        node.body = kept
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        # Nested function definitions only occur inside other functions,
        # whose bodies are stripped — never reached in practice.
        return _strip_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return _strip_body(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        doc = _docstring_expr(node)
        new_body: list[ast.stmt] = []
        if doc is not None:
            new_body.append(doc)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                new_body.append(_strip_body(child))
            elif isinstance(child, ast.ClassDef):
                # Nested classes are rare at this level; keep them but strip
                # their method bodies too.
                new_body.append(self.visit_ClassDef(child))
            elif isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
                # Additional string literals inside the class body: harmless,
                # keep them only if they are the leading docstring (already
                # handled); drop stray ones to keep the stub clean.
                continue
            else:
                new_body.append(child)
        if not new_body:
            new_body = [ast.Expr(ast.Constant(Ellipsis))]
        node.body = new_body
        return node


def _unparse_lines(node: ast.AST) -> str:
    return ast.unparse(node)


def render_py_stub(source: Path) -> str:
    """Render ``source`` (a pure-Python module) into ``.pyi`` text."""
    text = source.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(source))
    except SyntaxError as exc:
        raise RuntimeError(f"cannot parse {source}: {exc}") from exc

    renderer = _PureStubRenderer()
    tree = renderer.visit(tree)
    ast.fix_missing_locations(tree)
    try:
        rendered = ast.unparse(tree)
    except Exception as exc:  # pragma: no cover - unparse is total in practice
        raise RuntimeError(f"cannot unparse {source}: {exc}") from exc
    return rendered + "\n"


def is_stub_candidate(relative: str) -> bool:
    """Deprecated: subpackage all.py files ARE stubbed (star-import shims).

    Kept for backwards compatibility with callers; always returns True so
    the caller's own exclude globs decide.  Only the TOP-LEVEL all.py is
    skipped, through the ``all.py`` pattern in ``DEFAULT_PY_EXCLUDES``.
    """
    return True


if __name__ == "__main__":  # quick manual check: python py_renderer.py some_module.py
    for arg in sys.argv[1:]:
        path = Path(arg)
        out = render_py_stub(path)
        compile(out, str(path) + "i", "exec")
        print(f"OK {path}: {len(out)} bytes")
