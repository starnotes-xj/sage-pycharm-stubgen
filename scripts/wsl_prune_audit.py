"""Read-only audit backing the _TYPE_LAYER prune decision (WSL only).

Answers, against the *installed* Sage tree that stubgen regenerates from:

1. Which sage version is installed, and where.
2. For every ``origin=pyi`` prune candidate: does a sibling ``.pyi`` exist,
   is it owned by our install manifest (circular evidence), and does it
   actually declare the matching annotation?
3. For every ``origin=source`` candidate: confirm the module's own
   ``.py``/``.pyx`` declares the matching annotation (should reproduce the
   conformance result, no extra roots involved).
4. Re-run ``run_conformance`` with NO extra roots and report how many of
   the 67 recorded candidates survive — anything that drops out was matched
   only through ``--pyi-root`` previews and must not be pruned.

Usage (from repo root, inside the WSL conda ``sage`` env):
    python scripts/wsl_prune_audit.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sage_pycharm_stubgen.conformance import (  # noqa: E402
    _stubgen_owned_stubs,
    find_source_file,
    python_annotation_variants,
    pyx_annotation_variants,
    resolve_declaration,
    run_conformance,
)

REPO = Path(__file__).resolve().parents[1]
CANDIDATES = json.loads((REPO / "prune-candidates.json").read_text(encoding="utf-8"))


def sage_package() -> Path:
    import sage  # noqa: F401

    return Path(sage.__file__).parent


def main() -> None:
    pkg = sage_package()
    from sage.env import SAGE_VERSION

    print(f"sage package: {pkg}")
    print(f"sage version: {SAGE_VERSION}")

    manifest = pkg / ".sage-pycharm-stubgen-in-place.json"
    print(f"manifest present: {manifest.is_file()}")

    print("\n== origin=pyi candidates: sibling .pyi forensics ==")
    for cand in CANDIDATES:
        if cand["origin"] != "pyi":
            continue
        module, qualname = cand["module"], cand["qualname"]
        src = find_source_file(pkg, module)
        if src is None:
            print(f"  {module}.{qualname}: SOURCE FILE NOT FOUND")
            continue
        sib = src.with_suffix(".pyi")
        if not sib.is_file():
            print(f"  {module}.{qualname}: NO sibling .pyi (matched via --pyi-root only)")
            continue
        is_owned = sib.resolve() in _stubgen_owned_stubs(src)
        variants = dict(python_annotation_variants(sib, "pyi")).get(qualname, [])
        matched = any(a == cand["curated"] for a, _ in variants)
        print(
            f"  {module}.{qualname}: sibling={sib.name}"
            f" owned_by_us={is_owned} declares_match={matched}"
            f" variants={ [a for a, _ in variants] }"
        )

    print("\n== origin=source candidates: own-source re-verification ==")
    bad = 0
    for cand in CANDIDATES:
        if cand["origin"] != "source":
            continue
        module, qualname = cand["module"], cand["qualname"]
        variants = resolve_declaration(module, qualname, pkg)
        if not any(
            a == cand["curated"] for a, _ in variants
        ):
            bad += 1
            print(f"  MISMATCH {module}.{qualname}: {variants}")
    print(f"  source-origin re-verified: {sum(1 for c in CANDIDATES if c['origin'] == 'source') - bad} ok, {bad} mismatched")

    print("\n== conformance re-run WITHOUT extra roots ==")
    findings = run_conformance(pkg)
    from sage_pycharm_stubgen.conformance import prune_candidates

    fresh = {
        (c["module"], c["qualname"], c["origin"])
        for c in prune_candidates(findings)
    }
    survivor = 0
    for cand in CANDIDATES:
        key = (cand["module"], cand["qualname"], cand["origin"])
        if key in fresh:
            survivor += 1
        else:
            print(f"  DROPPED without --pyi-root: {cand['module']}.{cand['qualname']} ({cand['origin']})")
    print(f"  survivors: {survivor}/{len(CANDIDATES)}")
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    print(f"  full findings: {counts}")


if __name__ == "__main__":
    main()
