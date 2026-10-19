"""Apply the reviewed subset of prune-candidates.json to _TYPE_LAYER.

Deletion is never automatic for good reason: candidates whose only
"upstream coverage" is our own installed sibling stub (origin=pyi,
circular evidence until the manifest guard fix) and candidates where the
match is a bare generic against a parameterized curated type (precision
loss, e.g. ``list[Integer]`` -> ``list``) must stay curated.

This script therefore removes exactly the candidates that are BOTH:

- origin == "source"  — the installed tree's own .py/.pyx declares it
- curated == matched_declaration  — byte-identical annotation

Every removal must hit exactly one line inside the right module block of
_TYPE_LAYER or the script aborts without writing.

Usage: python scripts/apply_type_prune.py
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "src" / "sage_pycharm_stubgen" / "supplemental_docs.py"
CANDIDATES = REPO / "prune-candidates.json"

MODULE_HEADER = re.compile(r"^    '([a-z0-9_.]+)': \{$")
ENTRY_LINE = re.compile(r"^        '(?P<qual>[^']+)': \{.*\},$")


def main() -> int:
    candidates = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    selected = [
        c
        for c in candidates
        if c["origin"] == "source" and c["curated"] == c["matched_declaration"]
    ]
    rejected = len(candidates) - len(selected)
    print(f"candidates: {len(candidates)}, selected: {len(selected)}, kept: {rejected}")

    targets: dict[tuple[str, str], str] = {
        (c["module"], c["qualname"]): c["curated"] for c in selected
    }

    lines = TARGET.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    current_module: str | None = None
    in_type_layer = False
    removed: list[tuple[str, str]] = []
    for line in lines:
        if line.startswith("_TYPE_LAYER"):
            in_type_layer = True
        elif in_type_layer and line.startswith("}"):
            in_type_layer = False  # end of the dict literal
        stripped = line.rstrip("\r\n")
        header = MODULE_HEADER.match(stripped) if in_type_layer else None
        if header:
            current_module = header.group(1)
            out.append(line)
            continue
        entry = ENTRY_LINE.match(stripped) if in_type_layer else None
        if entry and current_module:
            key = (current_module, entry.group("qual"))
            if key in targets:
                expected = targets[key]
                if expected not in stripped:
                    print(
                        f"ABORT: {key} line does not contain {expected!r}: {stripped}",
                        file=sys.stderr,
                    )
                    return 1
                removed.append(key)
                continue  # drop the line
        out.append(line)

    missing = sorted(set(targets) - set(removed))
    if missing:
        for key in missing:
            print(f"ABORT: entry not found exactly once: {key}", file=sys.stderr)
        return 1
    if len(removed) != len(set(removed)):
        print("ABORT: duplicate removals recorded", file=sys.stderr)
        return 1

    TARGET.write_text("".join(out), encoding="utf-8")
    print(f"removed {len(removed)} entries from {TARGET.name}")
    kept_json = [c for c in candidates if (c["module"], c["qualname"]) not in set(removed)]
    CANDIDATES.write_text(
        json.dumps(kept_json, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"prune-candidates.json updated: {len(kept_json)} entries remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
