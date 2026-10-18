"""One-off v1 -> v2 migration for docs_zh.json (kept for provenance).

Two normalizations are applied while migrating:
- trailing ``陷阱:``-style sections that sit AFTER the ``示例::`` block
  (a batch-append artifact) are moved back before the example, matching the
  house layout used by the other ~800 entries;
- block-separator blank lines are re-inserted by the renderer, so the
  normalized blob is the round-trip fixed point of parse/render.

Safety gate: every normalized blob must round-trip byte-identically, and the
final rendered view must equal the normalized v1 map exactly — otherwise
nothing is written.

Usage: python scripts/migrate_docs_v2.py
"""
from __future__ import annotations

import difflib
import json
import sys

sys.path.insert(0, "src")

from sage_pycharm_stubgen import docs_layer

MARKER = docs_layer.EXAMPLE_MARKER


def normalize_after_example(blob: str) -> tuple[str, bool]:
    """Move col-0 ``X:`` sections that follow the example block before it."""
    marker_at = blob.find("\n" + MARKER)
    if marker_at >= 0:
        head, tail = blob[: marker_at + 1], blob[marker_at + 1 + len(MARKER) :]
    elif blob.startswith(MARKER):
        head, tail = "", blob[len(MARKER) :]
    else:
        return blob, False
    lines = tail.split("\n")  # lines[0] is the marker line's remainder
    split = None
    for index in range(1, len(lines)):
        stripped = lines[index].rstrip()
        if (
            stripped
            and not stripped[0].isspace()
            and not stripped.startswith("- ")
            and stripped.endswith(":")
        ):
            split = index
            break
    if split is None:
        return blob, False
    example = "\n".join(lines[1:split]).strip("\n")
    trailing = "\n".join(lines[split:]).strip("\n")
    rebuilt = (
        head.rstrip("\n") + "\n\n" + trailing + "\n\n" + MARKER + "\n\n" + example
    )
    return rebuilt, True


def main() -> int:
    payload = json.loads(docs_layer.DATA_FILE.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        print(f"expected v1 file, found version {payload.get('version')!r}")
        return 1
    docs: dict[str, dict[str, str]] = payload["docs"]

    headings: dict[str, int] = {}
    mixed: list[str] = []
    reordered: list[str] = []
    mismatches: list[tuple[str, str, str]] = []
    entries: dict[str, dict[str, dict]] = {}
    normalized_docs: dict[str, dict[str, str]] = {}
    for module, symbols in docs.items():
        for symbol, blob in symbols.items():
            key = f"{module}.{symbol}"
            new_blob, moved = normalize_after_example(blob)
            if moved:
                reordered.append(key)
            normalized_docs.setdefault(module, {})[symbol] = new_blob
            entry = docs_layer.parse_blob(new_blob)
            rendered = docs_layer.render_entry(entry)
            if rendered != new_blob:
                mismatches.append((key, new_blob, rendered))
            for section in entry.get("sections", []):
                headings[section["heading"]] = headings.get(section["heading"], 0) + 1
                if section.get("body") and section.get("items"):
                    mixed.append(key)
            entries.setdefault(module, {})[symbol] = entry

    total = sum(len(v) for v in docs.values())
    print(f"entries: {total}")
    print(f"post-example sections reordered: {len(reordered)} (e.g. {reordered[:3]})")
    print(f"headings: {sorted(headings.items(), key=lambda kv: -kv[1])}")
    print(f"mixed body+items sections: {len(mixed)} {mixed[:5]}")
    print(f"round-trip mismatches: {len(mismatches)}")
    for key, blob, rendered in mismatches[:3]:
        diff = "\n".join(
            difflib.unified_diff(
                blob.split("\n"), rendered.split("\n"), "v1", "v2-rendered", lineterm=""
            )
        )
        print(f"--- {key}\n{diff}")
    if mismatches:
        print("ABORT: fix parser or corpus before migrating")
        return 1

    for module, symbols in entries.items():
        for symbol, entry in symbols.items():
            docs_layer.set_entry(module, symbol, entry)
    written = docs_layer.save_docs()
    docs_layer.reload()
    problems = docs_layer.validate()
    identical = docs_layer.load_docs() == normalized_docs
    print(f"written: {written}")
    print(f"validate problems: {len(problems)}")
    print(f"rendered view identical to normalized v1: {identical}")
    if problems or not identical:
        for problem in problems[:10]:
            print("  ", problem)
        return 1
    print("stats:", {k: v for k, v in docs_layer.stats().items() if k != "per_module"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
