"""Conformance scan: do .pyx source return annotations reach the stubs?

Sage upstream is gaining Python-level return annotations, including in Cython
sources (``def f(self) -> Integer:`` is legal .pyx syntax since Cython 3, and
the classic ``cpdef Integer f(self)`` form also names a Python-level return
type).  The curated ``declare`` table currently backstops those returns by
hand; if the converter already preserves them, every upstream .pyx annotation
(including merged upstream PRs) is adopted for free and the curated entries
retire naturally.

This scan answers that question empirically.  For every shipped ``.pyx`` it
collects source-declared returns (both the arrow and the Cython-style forms,
kept apart because the converter may treat them differently), then checks the
installed stub (``.pyi`` beside the source) for the same member:

- ``preserved``   -- stub declares the same (normalized) return;
- ``diverged``    -- stub declares a different return;
- ``stub_unannotated`` -- member exists in the stub without a usable return;
- ``stub_missing``-- the stub does not declare the member at all.

Run with Sage's Python (it only needs the stdlib, but the default source root
auto-detection imports sage)::

    sage -python scripts/pyx_annotation_conformance.py [--source SRC] [--json]

Exit code 0 always: this is a measurement, not a gate.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sage_pycharm_stubgen.conformance import normalize_annotation  # noqa: E402
from sage_pycharm_stubgen.docstring_enrich import (  # noqa: E402
    _CLASS_RE,
    _DEF_RE,
    _parse_cython_signature,
)

#: .pyx sources also declare plain Python classes (``class Foo:``); the
#: docstring extractor's ``cdef class`` regex alone mis-scopes their methods
#: to module level, which would flood the scan with false ``stub_missing``.
_PLAIN_CLASS_RE = re.compile(r"^(?P<indent>\s*)class\s+(\w+)\b.*:$")


def pyx_source_annotations(source: Path) -> dict[str, tuple[str, str, str]]:
    """``qualname -> (canonical, form, raw)`` return declaration per .pyx.

    ``form`` is ``"arrow"`` for ``def f() -> X:`` and ``"cython"`` for
    ``cpdef X f(...)`` — the two syntaxes the converter may handle
    differently, so the scan keeps them apart.  ``canonical`` is the
    mapped Python rendering (``dict`` → ``dict[Any, Any]``), ``raw`` the
    verbatim source token (``dict``).
    """
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}

    lines = text.splitlines()
    found: dict[str, tuple[str, str]] = {}
    class_stack: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        class_match = _CLASS_RE.match(line) or _PLAIN_CLASS_RE.match(line)
        if class_match:
            indent = len(class_match.group(1))
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            class_stack.append((indent, class_match.group(2)))
            index += 1
            continue
        def_match = _DEF_RE.match(line)
        if def_match:
            indent = len(def_match.group("indent"))
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            rest = def_match.group("rest")
            # ``cpdef inline tuple f(...)``: modifier tokens are not types.
            rest = re.sub(r"^(?:inline|api|nogil)\s+", "", rest)
            # ``cdef`` functions are C-level only (not callable from Python,
            # excluded from stubs by design); the annotation channel is the
            # Python-visible ``def``/``cpdef`` kinds.
            if def_match.group("kind") == "cdef":
                index += 1
                continue
            # Attribute declarations (``cdef FrozenBitset t``) and local
            # variable declarations inside method bodies (``cdef Integer r =
            # <Integer> max(...)`` — initializer calls carry parens too)
            # carry types but no callable signature.  A method header has
            # no ``=`` before its first ``(``.
            head = rest.split("(", 1)[0]
            if "(" not in rest or "=" in head:
                index += 1
                continue
            name, return_type = _parse_cython_signature(rest)
            form = "cython"
            raw = head.split()[0] if head.split() else ""
            if return_type is None and "->" in rest:
                arrow = rest.rsplit("->", 1)[1].split(":", 1)[0].strip()
                if arrow:
                    return_type, form, raw = arrow, "arrow", arrow
            if name.isidentifier() and return_type:
                qualname = (
                    ".".join(item[1] for item in class_stack) + "." + name
                    if class_stack
                    else name
                )
                found[qualname] = (return_type, form, raw)
            index += 1
            continue
        index += 1
    return found


_C_TYPES = {
    "void", "bint", "char", "short", "int", "long", "long long",
    "unsigned", "unsigned char", "unsigned short", "unsigned int",
    "unsigned long", "unsigned long long", "float", "double",
    "long double", "size_t", "Py_ssize_t", "object",
}


def stub_returns(stub: Path) -> dict[str, str | None]:
    """``qualname -> return annotation`` (None when unannotated) for a .pyi."""
    try:
        tree = ast.parse(stub.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return {}

    def walk_body(body, prefix: str) -> dict[str, str | None]:
        found: dict[str, str | None] = {}
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                found.update(
                    walk_body(stmt.body, f"{prefix}{stmt.name}." if not prefix else f"{prefix}{stmt.name}.")
                )
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotation = (
                    ast.unparse(stmt.returns) if stmt.returns is not None else None
                )
                found[f"{prefix}{stmt.name}"] = annotation
        return found

    return walk_body(tree.body, "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        help="Installed sage package root (default: auto-detected)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per finding on stdout",
    )
    args = parser.parse_args(argv)

    if args.source is None:
        from sage_pycharm_stubgen.generator import detect_sage_package

        source_root, _ = detect_sage_package()
    else:
        source_root = args.source

    findings: list[dict] = []
    counts: Counter = Counter()
    samples: dict[str, list[str]] = {}
    for pyx in sorted(source_root.rglob("*.pyx")):
        module = pyx.relative_to(source_root).with_suffix("").as_posix().replace("/", ".")
        declared = pyx_source_annotations(pyx)
        if not declared:
            continue
        stub = pyx.with_suffix(".pyi")
        stubs = stub_returns(stub) if stub.is_file() else {}
        for qualname, (annotation, form, raw) in declared.items():
            # C-only scalar returns (``cpdef long f()``) have no Python-level
            # meaning beyond int/float; they are not the upstream-annotation
            # channel this scan is about.
            if annotation in _C_TYPES:
                continue
            # Private members are excluded from the stubs by design
            # (--include-private defaults off); dropping them here keeps the
            # scan about the public annotation channel.
            if qualname.rpartition(".")[2].startswith("_"):
                counts[f"{form}/private_skipped"] += 1
                continue
            full = f"{module}.{qualname}"
            stub_annotation = stubs.get(qualname, "__missing__")
            if stub_annotation == "__missing__":
                status = "stub_missing"
            elif stub_annotation is None or normalize_annotation(stub_annotation) == "Any":
                status = "stub_unannotated"
            elif (
                normalize_annotation(stub_annotation) == normalize_annotation(annotation)
                # ``cpdef dict f()`` renders as bare ``dict`` while the
                # extractor canonicalizes to ``dict[Any, Any]`` — equivalent
                # declarations, the generic parameters are unknowable from
                # the Cython source either way.
                or normalize_annotation(stub_annotation) == normalize_annotation(raw)
            ):
                status = "preserved"
            else:
                status = "diverged"
            counts[f"{form}/{status}"] += 1
            if status != "preserved":
                samples.setdefault(f"{form}/{status}", []).append(
                    f"{full}: source={annotation!r} stub={stub_annotation!r}"
                )
                if args.json:
                    findings.append(
                        {
                            "module": module,
                            "qualname": qualname,
                            "form": form,
                            "status": status,
                            "source": annotation,
                            "stub": stub_annotation,
                        }
                    )

    print(f"Sage source root: {source_root}")
    total = sum(counts.values())
    public = total - counts.get("arrow/private_skipped", 0) - counts.get("cython/private_skipped", 0)
    preserved = sum(count for key, count in counts.items() if key.endswith("/preserved"))
    print(f".pyx source-declared returns checked: {total} (public: {public})")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    if public:
        print(f"preserved ratio (public members): {preserved / public:.1%}")
    for key in sorted(samples):
        print(f"-- samples {key}")
        for line in samples[key][:8]:
            print(f"   {line}")
    if args.json:
        for finding in findings:
            print(json.dumps(finding, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
