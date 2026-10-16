"""Observed-return sampling: derive precise operator return types live.

The coverage layer (every public member present with a real signature) is
fully automatic; the remaining hand-maintained part of the curated table is
PRECISE RETURN TYPES.  A large, stable subset of those are arithmetic
operator chains (``Integer * Integer -> Integer``, finite-field element
arithmetic, polynomial arithmetic), and operators can be sampled safely:
construct one instance of the class, apply the binary operator with itself
(or the unary form) twice, and record the type only when both runs agree and
are not ``NotImplemented``.

Safety design (learned from probes):

- arbitrary NAMED methods are never sampled — a probe of no-arg methods on
  mixed Sage objects segfaulted a compiled module (unwrapped ``sig_on``);
- sampling runs in SUBPROCESS batches, so a crashed compiled module costs
  one batch, not the audit;
- classes in interactive/plotting/external-CAS modules are skipped;
- an instance is only used when its runtime type name matches the target
  class (dynamic ``_with_category`` subclasses are mapped back to their
  base name).

Receiver construction: a small table of per-parent recipes (``cls(6)``,
``PolynomialRing(QQ, 'x').gen()``, ``EllipticCurve(GF(11), [1, 1])
.random_point()``) plus generic attempts (``cls()``, ``an_element()``,
``zero()``, ``one()``).  Recipes are per-CLASS and version-stable — an
order of magnitude smaller and more durable than per-method return
annotations.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

SAMPLE_OPS = {
    "__add__": lambda a, b: a + b,
    "__sub__": lambda a, b: a - b,
    "__mul__": lambda a, b: a * b,
    "__truediv__": lambda a, b: a / b,
    "__floordiv__": lambda a, b: a // b,
    "__mod__": lambda a, b: a % b,
    "__pow__": lambda a, b: a ** b,
}
SAMPLE_UNARY_OPS = {
    "__neg__": lambda a: -a,
    "__invert__": lambda a: ~a,
}

# Per-parent receiver recipes: (class_name, expression) evaluated with the
# Sage global namespace plus ``cls`` bound to the stub-resolved class.  The
# result is accepted only when ``type(obj).__name__`` matches the class name
# (with the dynamic ``_with_category`` suffix tolerated).
RECEIVER_RECIPES: list[tuple[str, str]] = [
    ("Integer", "cls(6)"),
    ("Rational", "cls(22, 7)"),
    ("RealNumber", "cls('1.5')"),
    ("ComplexNumber", "cls(1, 2)"),
    ("EllipticCurvePoint_finite_field", "EllipticCurve(GF(11), [1, 1]).random_point()"),
    ("EllipticCurvePoint_field", "EllipticCurve(GF(11), [1, 1]).random_point()"),
    ("ProjectiveSpace_point_finite_field", "ProjectiveSpace(GF(11), 2).point([2, 1])"),
    ("FiniteFieldElement", "GF(11).an_element()"),
    ("FiniteField_givaroElement", "GF(3**3, 'a', impl='givaro').an_element()"),
    ("FiniteField_ntl_gf2eElement", "GF(2**6, 'a', impl='ntl').an_element()"),
    ("FiniteField_pari_ffeltElement", "GF(11).an_element()"),
    ("Polynomial_rational_flint", "PolynomialRing(QQ, 'x').gen()"),
    ("Polynomial_generic_dense", "PolynomialRing(QQ, 'x').gen()"),
    ("MPolynomial_ring_libsingular", "PolynomialRing(GF(11), 2, 'x').gens()[0]"),
    ("Matrix_integer_rsa", "matrix(2, 2, [1, 0, 0, 1])"),
    ("Matrix_mod2_dense", "matrix(GF(2), 2, 2, [1, 0, 0, 1])"),
    ("Matrix_modn_dense_float", "matrix(GF(11), 2, 2, [1, 0, 0, 1])"),
    ("Vector_rational_dense", "vector(QQ, [1, 2])"),
    ("FreeModuleElement_generic_dense", "vector(QQ, [1, 2])"),
    ("Permutation", "Permutation([2, 1])"),
    ("Partition", "Partition([3, 1])"),
    ("DiGraph", "DiGraph({1: [2]})"),
    ("Graph", "Graph({1: [2]})"),
]

DYNAMIC_SUFFIX = "_with_category"
_BATCH_SIZE = 125


def receiver_for(cls: type, globalns: dict) -> object | None:
    """Construct a receiver whose runtime type matches ``cls``."""
    expected = cls.__name__
    matches = lambda obj: type(obj).__name__ in (expected, expected + DYNAMIC_SUFFIX)
    for target_name, expression in RECEIVER_RECIPES:
        if target_name != expected:
            continue
        try:
            obj = eval(expression, dict(globalns), {"cls": cls})  # noqa: S307 - curated expressions only
            if obj is not None and matches(obj):
                return obj
        except Exception:
            continue
    for factory in ("an_element", "zero", "one"):
        fn = getattr(cls, factory, None)
        if fn is None:
            continue
        try:
            obj = fn()
            if obj is not None and matches(obj):
                return obj
        except Exception:
            continue
    try:
        obj = cls()
        if obj is not None and matches(obj):
            return obj
    except Exception:
        pass
    return None


def type_expression(value: object) -> dict | None:
    """Map an observed value to a stub-usable type reference (or None)."""
    if value is NotImplemented or value is None:
        return None
    cls = type(value)
    module = getattr(cls, "__module__", "")
    name = getattr(cls, "__name__", "")
    if module == "builtins":
        return {"expression": name, "module": ""}
    if name.endswith(DYNAMIC_SUFFIX):
        name = name[: -len(DYNAMIC_SUFFIX)]
        module = module.rpartition(".")[0]
    if module.startswith("sage.") and name:
        return {"expression": name, "module": module}
    return None


def sample_candidates(
    candidates: list[dict[str, str]],
    out_path: Path | None = None,
) -> dict[str, dict[str, dict]]:
    """In-process sampler used inside the isolated child process.

    ``candidates`` are ``{"module": ..., "class": ...}`` dicts.  Returns
    observations keyed ``"<module>.<Class>" -> {"<op>": {"expression": ...,
    "module": ...}}``.  When ``out_path`` is given the results are rewritten
    after EVERY candidate, so a segfault or timeout kill still leaves the
    parent the partial observations of this batch.
    """
    import ast
    import importlib

    import sage.all  # noqa: F401 - evaluation namespace for receiver recipes

    globalns = dict(vars(sys.modules["sage.all"]))
    results: dict[str, dict[str, dict]] = {}
    for candidate in candidates:
        module_name = candidate["module"]
        class_name = candidate["class"]
        if module_name.startswith(("sage.interfaces", "sage.repl", "sage.plot")):
            continue
        try:
            runtime_module = importlib.import_module(module_name)
            cls = getattr(runtime_module, class_name, None)
            if not inspect.isclass(cls):
                continue
        except Exception:
            continue
        receiver = receiver_for(cls, globalns)
        if receiver is None:
            continue
        key = f"{module_name}.{class_name}"
        observed = results.setdefault(key, {})
        for op_name, apply in SAMPLE_OPS.items():
            try:
                first = apply(receiver, receiver)
                second = apply(receiver, receiver)
            except Exception:
                continue
            info = type_expression(first) if type(first) is type(second) else None
            if info:
                observed[op_name] = info
        for op_name, apply in SAMPLE_UNARY_OPS.items():
            try:
                first = apply(receiver)
                second = apply(receiver)
            except Exception:
                continue
            info = type_expression(first) if type(first) is type(second) else None
            if info:
                observed[op_name] = info
                if out_path is not None:
                    out_path.write_text(
                        json.dumps(results, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    results = sample_candidates(candidates, out_path=args.out)
    args.out.write_text(
        json.dumps(results, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def collect_candidates(stub_root: Path) -> list[dict[str, str]]:
    """Every class declared in the stub tree, as sampler candidates."""
    import ast

    candidates: list[dict[str, str]] = []
    for stub in sorted(stub_root.rglob("*.pyi")):
        # Derive from the root-relative path: absolute stub paths would make
        # parts[0] the temp/home directory instead of "sage".
        relative = stub.relative_to(stub_root).with_suffix("")
        parts = list(relative.parts)
        if not parts or parts[0] != "sage":
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts[-1] == "__main__":
            continue
        module = ".".join(parts)
        if module.startswith(("sage.interfaces", "sage.repl", "sage.plot")):
            continue
        try:
            tree = ast.parse(stub.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                candidates.append({"module": module, "class": node.name})
    return candidates


def run_batches(
    stub_root: Path,
    *,
    batch_size: int = _BATCH_SIZE,
    timeout: float = 600.0,
) -> tuple[dict[str, dict[str, dict]], list[int]]:
    """Sample all candidate classes in isolated subprocess batches.

    A batch whose child crashes (segfaulting compiled module) is skipped and
    reported by index; every other batch proceeds.
    """
    import subprocess
    import tempfile

    candidates = collect_candidates(stub_root)
    merged: dict[str, dict[str, dict]] = {}
    crashed: list[int] = []
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        for index in range(0, len(candidates), batch_size):
            batch = candidates[index : index + batch_size]
            candidates_file = temp_dir / f"candidates-{index}.json"
            output_file = temp_dir / f"observed-{index}.json"
            candidates_file.write_text(
                json.dumps(batch, ensure_ascii=False), encoding="utf-8"
            )
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "sage_pycharm_stubgen.runtime_sample",
                        "--candidates",
                        str(candidates_file),
                        "--out",
                        str(output_file),
                    ],
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                # A poisonous class can hang the child; the child rewrites the
                # out file after every observation, so salvage what it saw.
                crashed.append(index)
            else:
                if completed.returncode != 0:
                    crashed.append(index)
            if output_file.is_file():
                for key, observed in json.loads(
                    output_file.read_text(encoding="utf-8")
                ).items():
                    merged.setdefault(key, {}).update(observed)
    return merged, crashed
