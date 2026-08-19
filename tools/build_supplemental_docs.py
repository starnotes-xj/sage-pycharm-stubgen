"""Regenerate ``supplemental_docs.py`` from a research-workflow output file.

Usage::

    python tools/build_supplemental_docs.py <workflow-output.json>

The input is the task-output JSON written by the ``sage-docs-research``
workflow; the output overwrites
``src/sage_pycharm_stubgen/supplemental_docs.py``.  Only entries marked
``verified`` are kept, duplicate (module, qualname) pairs are deduplicated
(preferring a concrete return annotation over ``Any``), and annotations are
validated so the generated module can never corrupt a stub.

``DECLARATIONS`` below is hand-maintained: it re-targets entries whose
stub declares the symbol in a different module, and adds ``declare``
signatures for members that stubgen-pyx drops entirely (the enrichment
pass appends them to the target class, so PyCharm can still resolve and
document them).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

MODULE_NAME_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
QUALNAME_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")

# (module, qualname) -> replacement module for entries whose stub declares
# the symbol elsewhere (base classes are the useful anchor).
REMAP: dict[tuple[str, str], str] = {
    ("sage.matrix.matrix2", "Matrix.kernel"): "sage.matrix.matrix0",
    ("sage.matrix.matrix2", "Matrix.nullity"): "sage.matrix.matrix0",
}

# (module, qualname) -> {declare, imports} for members stubgen-pyx drops
# (private names skipped by include_private=False, or methods implemented
# on concrete classes / categories that the factory return type cannot
# statically reach).
DECLARATIONS: dict[tuple[str, str], dict] = {
    ("sage.structure.category_object", "CategoryObject._first_ngens"): {
        "declare": "def _first_ngens(self, n: int) -> tuple[Any, ...]",
        "imports": ["from typing import Any"],
    },
    ("sage.rings.finite_rings.finite_field_base", "FiniteField.degree"): {
        "declare": "def degree(self) -> Integer",
        "imports": [],
    },
    ("sage.rings.finite_rings.finite_field_base", "FiniteField.is_finite"): {
        "declare": "def is_finite(self) -> bool",
        "imports": [],
    },
    ("sage.rings.finite_rings.finite_field_base", "FiniteField.primitive_element"): {
        "declare": (
            "def primitive_element(self) -> "
            "FiniteField_givaroElement | FiniteField_ntl_gf2eElement | "
            "FiniteFieldElement_pari_ffelt"
        ),
        "imports": [
            "from sage.rings.finite_rings.element_givaro import FiniteField_givaroElement",
            "from sage.rings.finite_rings.element_ntl_gf2e import FiniteField_ntl_gf2eElement",
            "from sage.rings.finite_rings.element_pari_ffelt import FiniteFieldElement_pari_ffelt",
        ],
    },
    ("sage.rings.finite_rings.finite_field_base", "FiniteField.zeta"): {
        "declare": "def zeta(self, n: Any = None) -> Any",
        "imports": ["from typing import Any"],
    },
    ("sage.rings.finite_rings.finite_field_base", "FiniteField.zeta_order"): {
        "declare": "def zeta_order(self) -> Integer",
        "imports": [],
    },
    # Module-level factory: stubgen-pyx renders `GF = FiniteField =
    # FiniteFieldFactory('FiniteField')` as a bare assignment with no callee
    # type, so `from sage.rings.finite_rings.finite_field_constructor import
    # GF` (directly or via a LazyImport re-export like strongly_regular_db.GF)
    # leaves `GF(p)` untyped.  Declare the factory; `_FieldBase` aliases the
    # base class because `FiniteField` inside this module is the factory
    # instance (kept by the chain-trim in _apply_declarations).
    ("sage.rings.finite_rings.finite_field_constructor", "GF"): {
        "declare": "def GF(*args: Any, **kwargs: Any) -> _FieldBase",
        "imports": [
            "from typing import Any",
            "from sage.rings.finite_rings.finite_field_base import FiniteField as _FieldBase",
        ],
    },
    # Module-level factory: stubgen-pyx renders `EllipticCurve =
    # EllipticCurveFactory('...')` as a bare assignment with no callee type,
    # so `from sage.schemes.elliptic_curves.constructor import EllipticCurve`
    # leaves `EllipticCurve(...)` untyped (same defect as finite_field_constructor.GF).
    ("sage.schemes.elliptic_curves.constructor", "EllipticCurve"): {
        "declare": "def EllipticCurve(*args: Any, **kwargs: Any) -> EllipticCurve_generic",
        "imports": [
            "from typing import Any",
            "from sage.schemes.elliptic_curves.ell_generic import EllipticCurve_generic",
        ],
    },
    # Scalar multiplication is implemented via the coercion action
    # `_acted_upon_`; binary-expression typing only consults `__mul__`, so
    # `P * n` (point first) needs an explicit dunder declaration to keep the
    # point type.  `n * P` stays typed by the left operand (Sage coercion
    # cannot be expressed statically on that side).
    ("sage.schemes.elliptic_curves.ell_point", "EllipticCurvePoint.__mul__"): {
        "declare": "def __mul__(self, n: Any) -> EllipticCurvePoint",
        "imports": ["from typing import Any"],
    },
    ("sage.rings.polynomial.polynomial_element", "Polynomial.quo_rem"): {
        "declare": "def quo_rem(self, other: Any) -> tuple[Any, Any]",
        "imports": ["from typing import Any"],
    },
    ("sage.matrix.matrix0", "Matrix.kernel"): {
        "declare": "def kernel(self, *args: Any, **kwargs: Any) -> FreeModule_generic",
        "imports": [
            "from typing import Any",
            "from sage.modules.free_module import FreeModule_generic",
        ],
    },
    ("sage.matrix.matrix0", "Matrix.nullity"): {
        "declare": "def nullity(self) -> Integer",
        "imports": ["from sage.rings.integer import Integer"],
    },
}


def _valid_annotation(annotation: str) -> bool:
    try:
        ast.parse(f"def _probe() -> {annotation}: ...\n")
    except SyntaxError:
        return False
    return True


def _is_concrete(annotation: str | None) -> bool:
    return bool(annotation) and annotation != "Any"


def load_entries(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload["result"]["results"]
    by_key: dict[tuple[str, str], dict] = {}
    for domain in results:
        for entry in domain["entries"]:
            if not entry.get("verified"):
                continue
            module = entry.get("module", "")
            qualname = entry.get("qualname", "")
            if not MODULE_NAME_RE.match(module) or not QUALNAME_RE.match(qualname):
                print(f"skip invalid key: {module} / {qualname}", file=sys.stderr)
                continue
            annotation = entry.get("return_annotation", "Any")
            if not _valid_annotation(annotation):
                print(
                    f"skip invalid annotation: {module} / {qualname} -> {annotation!r}",
                    file=sys.stderr,
                )
                annotation = "Any"
            doc = entry.get("zh_doc") or ""
            if not doc.strip():
                continue
            key = (module, qualname)
            previous = by_key.get(key)
            if previous is not None:
                # Prefer a concrete return annotation over Any/None.
                if _is_concrete(previous["return"]) or not _is_concrete(annotation):
                    continue
                by_key.pop(key)
            by_key[key] = {
                "module": module,
                "qualname": qualname,
                "doc": doc,
                "return": annotation,
                "imports": list(entry.get("imports") or []),
                "source": (
                    f"{entry['source_file']}:{entry['source_line']}"
                    if entry.get("source_file") and entry.get("source_line")
                    else ""
                ),
            }

    # Re-target entries whose stub declares the symbol in another module.
    for (module, qualname), target_module in REMAP.items():
        entry = by_key.pop((module, qualname), None)
        if entry is not None:
            entry["module"] = target_module
            by_key[(target_module, qualname)] = entry
    # Add declaration signatures for members stubgen-pyx drops.
    for (module, qualname), declaration in DECLARATIONS.items():
        entry = by_key.get((module, qualname))
        if entry is None:
            print(
                f"DECLARATIONS key not found in research data: {module}.{qualname}",
                file=sys.stderr,
            )
            continue
        entry["declare"] = declaration["declare"]
        entry["imports"] = list(
            dict.fromkeys(list(entry.get("imports", [])) + declaration.get("imports", []))
        )
    return list(by_key.values())


def render(entries: list[dict]) -> str:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["module"], []).append(entry)
    parts: list[str] = [
        '"""Curated docstrings and return types for CTF-critical Sage APIs.',
        "",
        "Auto-generated by tools/build_supplemental_docs.py from verified",
        "research against the installed Sage version -- do not edit by hand.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "SUPPLEMENTAL_DOCS: dict[str, dict[str, dict]] = {",
    ]
    for module in sorted(grouped):
        parts.append(f'    "{module}": {{')
        for entry in sorted(grouped[module], key=lambda item: item["qualname"]):
            parts.append(f'        "{entry["qualname"]}": {{')
            parts.append(f'            "doc": {entry["doc"]!r},')
            if _is_concrete(entry["return"]):
                parts.append(f'            "return": {entry["return"]!r},')
            if entry.get("declare"):
                parts.append(f'            "declare": {entry["declare"]!r},')
            if entry["imports"]:
                parts.append(f'            "imports": {entry["imports"]!r},')
            if entry["source"]:
                parts.append(f"            # source: {entry['source']}")
            parts.append("        },")
        parts.append("    },")
    parts.extend(["}", ""])
    return "\n".join(parts)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    input_path = Path(sys.argv[1])
    if not input_path.is_file():
        print(f"not found: {input_path}", file=sys.stderr)
        return 2
    entries = load_entries(input_path)
    output_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "sage_pycharm_stubgen"
        / "supplemental_docs.py"
    )
    output_path.write_text(render(entries), encoding="utf-8")
    print(f"wrote {len(entries)} entries to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
