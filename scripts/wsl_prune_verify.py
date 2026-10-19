"""Post-prune verification inside the WSL sage env (read-only re: install).

1. Re-run conformance with the fixed manifest-ownership guard: the 21
   self-matched ``origin=pyi`` candidates must disappear (our own
   installed stubs are circular evidence), leaving only the 3 loose
   source matches; also print the identity of any conflict finding.
2. Regenerate ONLY the modules that lost curated entries (plus the two
   new factory modules) into a temp tree and assert, for every pruned
   qualname, that the output stub still declares the exact expected
   return annotation via the upstream channel alone.

Usage: PYTHONPATH=src python scripts/wsl_prune_verify.py
"""

import json
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

PRUNED = json.loads((REPO / ".pruned-43.json").read_text(encoding="utf-8"))
NEW_FACTORIES = [
    ("sage.rings.integer_ring", "IntegerRing", "IntegerRing_class"),
    ("sage.rings.real_mpfr", "RealField", "RealField_class"),
]


def find_def_return(text: str, name: str) -> str | None:
    """Return annotation of ``def name(...)`` in stub text, None-shaped as ''."""
    match = re.search(rf"\bdef {re.escape(name)}\(", text)
    if match is None:
        return None
    depth = 0
    index = match.end() - 1
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                rest = text[index + 1 : text.find(":", index)]
                arrow = re.match(r"\s*->\s*(.+?)\s*$", rest or "", re.DOTALL)
                return arrow.group(1) if arrow else ""
        index += 1
    return None


def main() -> int:
    from sage_pycharm_stubgen.conformance import (
        find_source_file,
        prune_candidates,
        run_conformance,
    )
    from sage_pycharm_stubgen.generator import generate

    import sage

    pkg = Path(sage.__file__).parent

    print("== conformance with fixed ownership guard ==")
    findings = run_conformance(pkg)
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.status] = counts.get(finding.status, 0) + 1
    print("findings:", counts)
    for finding in findings:
        if finding.status == "conflict":
            print(
                f"  conflict: {finding.module}.{finding.qualname}"
                f" curated={finding.curated!r} upstream={finding.annotation!r}"
                f" origin={finding.origin}"
            )
    candidates = prune_candidates(findings)
    print(f"prune candidates now: {len(candidates)}")
    for cand in candidates:
        print(
            f"  {cand['module']}.{cand['qualname']}"
            f" curated={cand['curated']!r} matched={cand['matched_declaration']!r}"
            f" origin={cand['origin']}"
        )

    modules = sorted({entry["module"] for entry in PRUNED} | {m for m, _, _ in NEW_FACTORIES})
    patterns = []
    for module in modules:
        src = find_source_file(pkg, module)
        if src is None:
            print(f"PANIC: source not found for {module}")
            return 1
        patterns.append(src.relative_to(pkg).as_posix())
    print(f"\n== targeted regen of {len(patterns)} modules ==")
    for pattern in patterns:
        print("  ", pattern)

    with tempfile.TemporaryDirectory(prefix="sps-verify-") as tmp:
        summary = generate(
            Path(tmp),
            sage_package=pkg,
            patterns=patterns,
            include_py=True,
            generate_all=False,
            use_runtime_docs=False,
            sample_methods=False,
        )
        print(f"regen: discovered={summary.discovered} failed={summary.failed}")

        failures = 0
        checks = [
            (entry["module"], entry["qualname"].split(".")[-1], entry["return"], entry["qualname"])
            for entry in PRUNED
        ] + [(m, q, r, q) for m, q, r in NEW_FACTORIES]
        for module, func, expected, qualname in checks:
            src = find_source_file(pkg, module)
            stub = Path(tmp) / "sage" / src.relative_to(pkg).with_suffix(".pyi")
            if not stub.is_file():
                print(f"  MISSING STUB: {stub.name} ({qualname})")
                failures += 1
                continue
            actual = find_def_return(stub.read_text(encoding="utf-8"), func)
            if actual == expected:
                continue
            failures += 1
            print(
                f"  REGRESSION: {module}.{qualname}"
                f" expected -> {expected!r} got {actual!r} ({stub.name})"
            )
        total = len(checks)
        print(f"\nannotation checks: {total - failures}/{total} ok")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
