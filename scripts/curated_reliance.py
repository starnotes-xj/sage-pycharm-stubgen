"""Curated reliance accounting + operator-coverage intersection.

For every curated entry that targets an arithmetic operator dunder, classify:

- OBSERVED_COVERED_EXACT  — the live-runtime sampler derives the same type;
                            the curated type layer is redundant (retirable)
- OBSERVED_COVERED_DIFF   — sampler derives a different type; needs review
- NOT_CONSTRUCTIBLE       — no receiver recipe produced an instance; curated
                            stays as the only precision source
- NOT_OPERATOR            — non-operator entry (out of sampler scope)

Usage: python scripts/curated_reliance.py [site-packages-dir]
"""

import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sage_pycharm_stubgen.runtime_sample import (  # noqa: E402
    SAMPLE_OPS,
    SAMPLE_UNARY_OPS,
    receiver_for,
)

import sage.all as sage_all  # noqa: F401,E402

ALL_OPS = {**SAMPLE_OPS, **SAMPLE_UNARY_OPS}


def entry_target(module: str, qualname: str):
    parts = qualname.split(".")
    if len(parts) < 2:
        return None
    return module, parts[-2], parts[-1]


def is_operator_member(member: str) -> bool:
    return member in ALL_OPS


def type_set(expression: str) -> set[str]:
    return {part.strip() for part in expression.split("|") if part.strip()}


def main(site_packages: Path):
    from sage_pycharm_stubgen.supplemental_docs import SUPPLEMENTAL_DOCS

    operator_entries = []
    for module, entries in SUPPLEMENTAL_DOCS.items():
        for qualname, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            target = entry_target(module, qualname)
            if target is None:
                continue
            module, owner, member = target
            if not is_operator_member(member):
                continue
            operator_entries.append((module, owner, member, qualname, entry))

    print(f"curated operator entries: {len(operator_entries)}")

    covered_exact = []
    covered_diff = []
    not_constructible = []
    receiver_cache: dict[str, object | None] = {}

    for module, owner, member, qualname, entry in operator_entries:
        try:
            runtime_module = importlib.import_module(module)
        except Exception:
            not_constructible.append((qualname, "module import failed"))
            continue
        cls = getattr(runtime_module, owner, None)
        if not isinstance(cls, type):
            not_constructible.append((qualname, "owner class missing"))
            continue
        cache_key = f"{module}.{owner}"
        if cache_key in receiver_cache:
            receiver = receiver_cache[cache_key]
        else:
            receiver = receiver_for(cls, dict(vars(sage_all)))
            receiver_cache[cache_key] = receiver
        if receiver is None:
            not_constructible.append((qualname, "no receiver"))
            continue
        apply_binary = ALL_OPS.get(member)
        # Mirror the sampler's operand variants: same-instance first, then a
        # small scalar on either side (point * 5 arithmetic).
        operand_pairs = (
            [(receiver, receiver), (receiver, 6), (6, receiver)]
            if member in SAMPLE_OPS
            else [(receiver,)]
        )
        observed = None
        stable = False
        for operands in operand_pairs:
            try:
                first = apply_binary(*operands)
                second = apply_binary(*operands)
            except Exception:
                continue
            if type(first) is type(second):
                observed = type(first)
                stable = True
                break
        if not stable:
            not_constructible.append((qualname, "no stable observation"))
            continue
        observed_mod = getattr(observed, "__module__", "")
        if observed_mod != "builtins" and not observed_mod.startswith("sage."):
            not_constructible.append((qualname, "foreign type"))
            continue
        observed_name = getattr(observed, "__name__", "")
        curated_return = str(entry.get("return") or "")
        if not curated_return:
            # declare entries carry the type inside the signature text
            declare = str(entry.get("declare") or "")
            if "->" in declare:
                curated_return = declare.rsplit("->", 1)[1].split(":")[0].strip()
        curated_types = type_set(curated_return) if curated_return else set()
        if observed_name in curated_types:
            covered_exact.append((qualname, observed_name, curated_return))
        else:
            covered_diff.append((qualname, observed_name, curated_return))

    print(f"OBSERVED_COVERED_EXACT (retirable): {len(covered_exact)}")
    for q, observed, curated in covered_exact[:10]:
        print(f"  {q}: observed={observed} curated={curated}")
    print(f"OBSERVED_COVERED_DIFF (review): {len(covered_diff)}")
    for q, observed, curated in covered_diff[:10]:
        print(f"  {q}: observed={observed} curated={curated}")
    print(f"NOT_CONSTRUCTIBLE (curated stays): {len(not_constructible)}")
    for item in not_constructible[:10]:
        print("  ", item)


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    main(root)
