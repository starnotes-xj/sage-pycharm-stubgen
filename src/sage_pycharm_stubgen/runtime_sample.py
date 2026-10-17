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

- arbitrary NAMED methods are never sampled BY DEFAULT — a probe of no-arg
  methods on mixed Sage objects segfaulted a compiled module (unwrapped
  ``sig_on``); an opt-in ``sample_methods`` tier probes only own-class
  public methods with no required parameters, inside the same subprocess
  isolation;
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

#: Same-instance ``a % a`` reaches unwrapped compiled code on some matrix
#: families (``Matrix_integer_dense % Matrix_integer_dense`` segfaults the
#: interpreter — probed 2026-10 on Sage 10.9); the scalar-side pairs raise
#: clean TypeErrors everywhere, so the self-pair is simply never attempted
#: for these operators.
_NO_SELF_OPERAND_OPS = frozenset({"__mod__"})

# Per-parent receiver recipes: (class_name, expression) evaluated with the
# Sage global namespace plus ``cls`` bound to the stub-resolved class.  The
# result is accepted only when the runtime type matches the class name (or is
# an instance of it — operator dunders are inherited, so a concrete-subclass
# receiver exercises a base-class declaration; dynamic ``_with_category``
# suffixes are tolerated).  Expressions were verified live against Sage 10.9;
# a 2026-10 recipe audit also fixed entries whose construction could never
# satisfy the isinstance gate (dense vector for a sparse class, GF(p)
# IntegerMod element for FiniteFieldElement, a ring *element* expression for
# a ring class).
RECEIVER_RECIPES: list[tuple[str, str]] = [
    ("Integer", "cls(6)"),
    ("Rational", "cls(22, 7)"),
    ("RealNumber", "cls('1.5')"),
    ("ComplexNumber", "cls(1, 2)"),
    ("EllipticCurvePoint", "EllipticCurve(GF(11), [1, 1]).random_point()"),
    ("EllipticCurvePoint_finite_field", "EllipticCurve(GF(11), [1, 1]).random_point()"),
    ("EllipticCurvePoint_field", "EllipticCurve(GF(11), [1, 1]).random_point()"),
    ("ProjectiveSpace_point_finite_field", "ProjectiveSpace(GF(11), 2).point([2, 1])"),
    # GF(p) elements are IntegerMod_int, NOT FiniteFieldElement instances —
    # only the extension-field implementations satisfy the base-class gate.
    ("FiniteFieldElement", "GF(4).an_element()"),
    ("FiniteField_givaroElement", "GF(3**3, 'a', impl='givaro').an_element()"),
    ("FiniteField_ntl_gf2eElement", "GF(2**6, 'a', impl='ntl').an_element()"),
    ("FiniteFieldElement_pari_ffelt", "GF(2**4, 'a', impl='pari_ffelt').an_element()"),
    ("IntegerMod_int", "Zmod(11)(5)"),
    # number fields
    ("NumberFieldElement", "NumberField(PolynomialRing(QQ, 'x').gen() ** 2 + 1, 'a').an_element()"),
    ("NumberFieldElement_absolute", "NumberField(PolynomialRing(QQ, 'x').gen() ** 2 + 1, 'a').an_element()"),
    ("NumberFieldElement_quadratic", "QuadraticField(2, 'a').an_element()"),
    ("NumberFieldElement_gaussian", "QuadraticField(-1, 'i').an_element()"),
    ("NumberFieldElement_relative", "NumberField(PolynomialRing(QQ, 'x').gen() ** 2 + 1, 'a').extension(PolynomialRing(QQ, 'x').gen() ** 3 + 2, 'b').an_element()"),
    # polynomial / series families
    ("Polynomial_rational_flint", "PolynomialRing(QQ, 'x').gen()"),
    ("Polynomial_integer_dense_flint", "PolynomialRing(ZZ, 'x').gen()"),
    ("Polynomial_zmod_flint", "PolynomialRing(Zmod(11), 'x').gen()"),
    ("MPolynomial_libsingular", "PolynomialRing(QQ, 2, 'x').gens()[0]"),
    ("MPolynomialRing_libsingular", "PolynomialRing(GF(11), 2, 'x')"),
    ("LaurentPolynomial_univariate", "LaurentPolynomialRing(QQ, 'x').gen()"),
    ("PowerSeries_poly", "PowerSeriesRing(QQ, 'x').gen()"),
    ("FractionFieldElement_1poly_field", "FractionField(PolynomialRing(QQ, 'x')).an_element()"),
    # p-adics
    ("pAdicCappedRelativeElement", "Qp(5).an_element()"),
    ("pAdicFixedModElement", "Zp(5, 5, 'fixed-mod').an_element()"),
    ("pAdicCappedAbsoluteElement", "Zp(5, 5, 'capped-abs').an_element()"),
    # base rings (operators on QQ/ZZ/RR return the ring itself)
    ("RationalField", "QQ"),
    ("IntegerRing_class", "ZZ"),
    ("RealField_class", "RealField(53)"),
    # matrices
    ("Matrix_integer_dense", "matrix(2, 2, [1, 0, 0, 1])"),
    ("Matrix_rational_dense", "matrix(QQ, 2, 2, [1, 2, 3, 4])"),
    ("Matrix_rational_sparse", "matrix(QQ, 2, 2, [1, 2, 3, 4], sparse=True)"),
    ("Matrix_symbolic_dense", "matrix(SR, 2, 2, [SR.var('a'), 1, 1, 1])"),
    ("Matrix_generic_dense", "matrix(FractionField(PolynomialRing(QQ, 'x')), 2, 2, [1, 0, 0, 1])"),
    ("Matrix_mod2_dense", "matrix(GF(2), 2, 2, [1, 0, 0, 1])"),
    ("Matrix_modn_dense_float", "matrix(GF(11), 2, 2, [1, 0, 0, 1])"),
    # vectors
    ("Vector_rational_dense", "vector(QQ, [1, 2])"),
    ("Vector_integer_dense", "vector(ZZ, [1, 2])"),
    ("Vector_mod2_dense", "vector(GF(2), [1, 0])"),
    ("Vector_modn_dense", "vector(GF(11), [1, 0])"),
    ("Vector_symbolic_dense", "vector(SR, [SR.var('a'), 1])"),
    ("FreeModuleElement_generic_dense", "vector(QQ, [1, 2])"),
    ("FreeModuleElement_generic_sparse", "vector(QQ, {0: 1, 3: 2})"),
    # symbolic
    ("Expression", "SR.var('x') + 1"),
    # combinatorics
    ("Permutation", "Permutation([2, 1])"),
    ("Partition", "Partition([3, 1])"),
    ("Tableau", "Tableau([[1, 2], [3]])"),
    ("StandardTableau", "StandardTableau([[1, 2], [3]])"),
    ("SkewPartition", "SkewPartition([[3, 1], [1]])"),
    ("Composition", "Composition([2, 1])"),
    # graphs and geometry
    ("DiGraph", "DiGraph({1: [2]})"),
    ("Graph", "Graph({1: [2]})"),
    ("SchemeMorphism_point_projective_field", "ProjectiveSpace(QQ, 2).point([1, 2, 1])"),
]

DYNAMIC_SUFFIX = "_with_category"
_BATCH_SIZE = 125


def _needs_arguments(signature: inspect.Signature) -> bool:
    """True when a BOUND call of ``signature`` still requires arguments.

    The leading positional slot without a default is the bound ``self`` /
    ``cls``; every remaining parameter must carry a default or be variadic
    for a no-argument probe to be well-defined.
    """
    parameters = list(signature.parameters.values())
    if (
        parameters
        and parameters[0].kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        and parameters[0].default is parameters[0].empty
    ):
        parameters = parameters[1:]
    return any(
        param.default is param.empty
        and param.kind
        not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
        for param in parameters
    )


def receiver_for(cls: type, globalns: dict) -> object | None:
    """Construct a receiver whose runtime type matches (or subclasses) ``cls``.

    Subclass instances are valid receivers: operator dunders are inherited,
    so an ``EllipticCurvePoint_finite_field`` instance exercises the base
    ``EllipticCurvePoint_field`` operator declarations.
    """
    matches = lambda obj: isinstance(obj, cls) or type(obj).__name__ in (
        cls.__name__,
        cls.__name__ + DYNAMIC_SUFFIX,
    )
    expected = cls.__name__
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
    sample_methods: bool = False,
) -> dict[str, dict[str, dict]]:
    """In-process sampler used inside the isolated child process.

    ``candidates`` are ``{"module": ..., "class": ...}`` dicts.  Returns
    observations keyed ``"<module>.<Class>" -> {"<op>": {"expression": ...,
    "module": ...}}``.  When ``out_path`` is given the results are rewritten
    after EVERY candidate, so a segfault or timeout kill still leaves the
    parent the partial observations of this batch.

    ``sample_methods`` (opt-in, off by default) additionally probes the
    class's OWN public named methods — but only those whose ``inspect``
    signature has no required parameter besides the bound ``self``/``cls``
    slot (``is_square()``, ``norm()``, …).  The probe reuses every operator
    guard: double-run type stability, NotImplemented/None/foreign-module
    rejection, and the batch subprocess timeout as the only hang guard (a
    single wedged call costs its batch, whose earlier observations the
    incremental out-file writes already salvaged).  Named methods carry a
    real segfault risk that the operator-only protocol avoids, which is why
    this tier is opt-in.
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

        def record(name: str, info: dict) -> None:
            observed[name] = info
            if out_path is not None:
                out_path.write_text(
                    json.dumps(results, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

        scalar = 6
        for op_name, apply in SAMPLE_OPS.items():
            # Operand variants: same-instance first, then a small scalar on
            # either side (point * 5 / 5 * point arithmetic — point * point
            # is a TypeError for most Sage structures).  The first
            # double-run-stable observation wins for this operator.
            operand_pairs = [(receiver, receiver), (receiver, scalar), (scalar, receiver)]
            if op_name in _NO_SELF_OPERAND_OPS:
                operand_pairs = operand_pairs[1:]
            info = None
            for left, right in operand_pairs:
                try:
                    first = apply(left, right)
                    second = apply(left, right)
                except Exception:
                    continue
                if type(first) is type(second):
                    info = type_expression(first)
                    if info:
                        break
            if info:
                record(op_name, info)
        for op_name, apply in SAMPLE_UNARY_OPS.items():
            try:
                first = apply(receiver)
                second = apply(receiver)
            except Exception:
                continue
            info = type_expression(first) if type(first) is type(second) else None
            if info:
                record(op_name, info)
        if sample_methods:
            for name, member in list(vars(cls).items()):
                if name.startswith("_") or name in observed or not callable(member):
                    continue
                try:
                    signature = inspect.signature(member)
                except (TypeError, ValueError):
                    continue
                if _needs_arguments(signature):
                    continue  # required parameters: no safe probe exists
                try:
                    bound = getattr(receiver, name)
                    first = bound()
                    second = bound()
                except Exception:
                    continue
                if type(first) is type(second):
                    info = type_expression(first)
                    if info:
                        record(name, info)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--sample-methods",
        action="store_true",
        help="Also probe own-class public methods with no required parameters",
    )
    args = parser.parse_args(argv)

    # A poisoned class can balloon to tens of GB before hanging; cap the
    # child's address space so the kernel OOM killer never takes the whole
    # WSL VM down.  MemoryError from an over-eager sample is caught upstream.
    try:
        import resource

        cap = 12 * 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except (ImportError, ValueError, OSError):
        pass
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    results = sample_candidates(
        candidates, out_path=args.out, sample_methods=args.sample_methods
    )
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
    sample_methods: bool = False,
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
            child_argv = [
                sys.executable,
                "-m",
                "sage_pycharm_stubgen.runtime_sample",
                "--candidates",
                str(candidates_file),
                "--out",
                str(output_file),
            ]
            if sample_methods:
                child_argv.append("--sample-methods")
            try:
                completed = subprocess.run(
                    child_argv,
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
