"""Whole-corpus coverage: every stub class vs its live runtime public members.

Missing = a public runtime member defined somewhere in the Sage MRO that is
neither declared by the stub class itself nor visible through its stub base
chain — i.e. completion genuinely cannot reach it.
"""

import ast
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sage_pycharm_stubgen.runtime_audit import (  # noqa: E402
    _StubHierarchy,
    _hierarchy_module_name,
    _declared_member_names,
    _resolve_runtime_class,
)

import sage.all  # noqa: F401,E402


def origin_module(cls, name):
    for k in cls.__mro__:
        if name in vars(k):
            m = getattr(k, "__module__", "")
            if m.startswith("sage."):
                return m
    return None


def main(stub_root: Path):
    hierarchy = _StubHierarchy.build(stub_root)
    total_runtime = 0
    total_missing = 0
    classes_checked = 0
    classes_with_runtime = 0
    offenders = []

    files = sorted(stub_root.rglob("*.pyi"))
    for index, stub in enumerate(files):
        module_name = _hierarchy_module_name(stub_root, stub)
        if module_name is None:
            continue
        if module_name.rpartition(".")[2] == "__main__":
            continue
        try:
            runtime_module = importlib.import_module(module_name)
        except Exception:
            continue
        try:
            tree = ast.parse(stub.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not node.body or node.body[0].lineno == node.lineno:
                continue
            classes_checked += 1
            runtime_cls, _ = _resolve_runtime_class(runtime_module, module_name, node.name)
            if runtime_cls is None:
                continue
            classes_with_runtime += 1
            declared = _declared_member_names(node)
            for name in dir(runtime_cls):
                if name.startswith("_"):
                    continue
                owner_mod = origin_module(runtime_cls, name)
                if owner_mod is None:
                    continue
                total_runtime += 1
                if name in declared or hierarchy.has_visible(module_name, node.name, name):
                    continue
                total_missing += 1
                if len(offenders) < 15:
                    offenders.append(f"{module_name}.{node.name}.{name}")

        if index % 400 == 0:
            print(f"[progress] {index}/{len(files)} files, {classes_checked} classes...", file=sys.stderr)

    pct = 100 * (total_runtime - total_missing) / max(total_runtime, 1)
    print(f"classes checked: {classes_checked} (with runtime: {classes_with_runtime})")
    print(f"runtime public members: {total_runtime} | missing: {total_missing} | coverage: {pct:.2f}%")
    for line in offenders:
        print("  missing:", line)


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    main(root)
