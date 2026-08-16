"""Merge new research output into ``supplemental_docs.py``.

Usage::

    python tools/merge_supplemental_docs.py <workflow-output.json> [more.json ...]

Each input is the task-output JSON written by a research workflow
(``{"result": {"results": [{"entries": [...]}, ...]}}``).  Verified entries
are merged into the existing curated data: new or conflicting ``doc``,
``return`` and ``imports`` values win, while hand-maintained fields such as
``declare`` that the research output does not carry are preserved.  The
module file is re-rendered in place.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_supplemental_docs as build  # noqa: E402

MODULE_NAME_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
QUALNAME_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")


def load_existing() -> dict[str, dict[str, dict]]:
    """Import the current SUPPLEMENTAL_DOCS from the package."""
    module_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "sage_pycharm_stubgen"
        / "supplemental_docs.py"
    )
    namespace: dict = {}
    exec(module_path.read_text(encoding="utf-8"), namespace)
    return namespace["SUPPLEMENTAL_DOCS"]


IMPORT_LINE_RE = re.compile(r"^(?:from\s+\S+\s+import\s+|import\s+)")


def collect_new_entries(paths: list[Path]) -> list[dict]:
    entries: dict[tuple[str, str], dict] = {}
    dropped_imports = 0
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        results = payload.get("result", {}).get("results") or payload.get("results", [])
        for domain in results:
            for entry in domain.get("entries", []):
                if not entry.get("verified"):
                    continue
                module = entry.get("module", "")
                qualname = entry.get("qualname", "")
                if not MODULE_NAME_RE.match(module) or not QUALNAME_RE.match(qualname):
                    print(f"skip invalid key: {module} / {qualname}", file=sys.stderr)
                    continue
                annotation = entry.get("return_annotation") or "Any"
                if not build._valid_annotation(annotation):
                    print(
                        f"skip invalid annotation: {module} / {qualname} -> {annotation!r}",
                        file=sys.stderr,
                    )
                    annotation = "Any"
                doc = entry.get("zh_doc") or ""
                if not doc.strip():
                    continue
                imports = [
                    line for line in entry.get("imports") or [] if IMPORT_LINE_RE.match(line)
                ]
                dropped_imports += len(entry.get("imports") or []) - len(imports)
                entries[(module, qualname)] = {
                    "module": module,
                    "qualname": qualname,
                    "doc": doc,
                    "return": annotation,
                    "imports": imports,
                    "source": "",
                }
    if dropped_imports:
        print(f"dropped {dropped_imports} malformed import line(s)", file=sys.stderr)
    return list(entries.values())


def merge(existing: dict[str, dict[str, dict]], new_entries: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], dict] = {}
    for module, module_entries in existing.items():
        for qualname, entry in module_entries.items():
            by_key[(module, qualname)] = {
                "module": module,
                "qualname": qualname,
                "doc": entry.get("doc", ""),
                "return": entry.get("return") or "Any",
                "imports": list(entry.get("imports") or []),
                "declare": entry.get("declare"),
                "source": entry.get("source", ""),
            }
    for entry in new_entries:
        key = (entry["module"], entry["qualname"])
        previous = by_key.get(key)
        if previous is None:
            previous = dict(entry, declare=None)
            by_key[key] = previous
            continue
        previous["doc"] = entry["doc"]
        if build._is_concrete(entry["return"]):
            previous["return"] = entry["return"]
        previous["imports"] = list(
            dict.fromkeys(previous["imports"] + entry["imports"])
        )
        # ``declare`` is hand-maintained and never overwritten by research.
    return list(by_key.values())


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    paths = [Path(arg) for arg in sys.argv[1:]]
    for path in paths:
        if not path.is_file():
            print(f"not found: {path}", file=sys.stderr)
            return 2
    existing = load_existing()
    new_entries = collect_new_entries(paths)
    merged = merge(existing, new_entries)
    output_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "sage_pycharm_stubgen"
        / "supplemental_docs.py"
    )
    output_path.write_text(build.render(merged), encoding="utf-8")
    print(f"merged {len(new_entries)} new entries; total {len(merged)}")
    # Validate the result.
    ast.parse(output_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
