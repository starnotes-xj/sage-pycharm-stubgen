from __future__ import annotations

import contextlib
import importlib
import inspect
import io
import keyword
import warnings
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FactoryInference:
    name: str
    return_type: str
    observed_types: tuple[str, ...] = ()


FactoryCall = tuple[tuple[Any, ...], dict[str, Any]]


# These names are intentionally conservative: they are callable Sage objects or
# constructors commonly used by Sage users and CTF challenges.
EXPLICIT_FACTORY_NAMES = frozenset(
    {
        "Mod",
        "mod",
        "GF",
        "Zmod",
        "Integers",
        "IntegerModRing",
        "PolynomialRing",
        "PowerSeriesRing",
        "LaurentPolynomialRing",
        "matrix",
        "vector",
        "MatrixSpace",
        "VectorSpace",
        "RealField",
        "ComplexField",
        "FiniteField",
        "NumberField",
        "CyclotomicField",
        "EllipticCurve",
        "Permutation",
        "Graph",
        "DiGraph",
    }
)

FACTORY_SUFFIXES = (
    "Ring",
    "Field",
    "Space",
    "Algebra",
    "Module",
    "Curve",
    "Polynomial",
    "Matrix",
    "Vector",
    "Graph",
    "Group",
    "Ideal",
)

UNSAFE_NAMES = frozenset(
    {
        "attach",
        "exit",
        "get_ipython",
        "help",
        "load",
        "open",
        "quit",
        "random",
        "save",
        "set_random_seed",
        "show",
        "system",
        "timeit",
        "view",
        "walltime",
        "write",
    }
)

# These are callable parent objects, not constructor factories.  Treating a
# single coercion call as their universal return type would be actively wrong.
NON_FACTORY_CALLABLES = frozenset({"InfinityRing", "UnsignedInfinityRing"})


def _is_lazy_import(value: Any) -> bool:
    value_type = type(value)
    return (
        value_type.__module__ == "sage.misc.lazy_import"
        and value_type.__name__ == "LazyImport"
    )


def is_factory_candidate(name: str, value: Any) -> bool:
    if (
        name.startswith("_")
        or not name.isidentifier()
        or keyword.iskeyword(name)
        or name in UNSAFE_NAMES
        or name in NON_FACTORY_CALLABLES
        or _is_lazy_import(value)
        or inspect.isclass(value)
        or inspect.ismodule(value)
        or not callable(value)
    ):
        return False

    if name in EXPLICIT_FACTORY_NAMES:
        return True
    return name[:1].isupper() and name.endswith(FACTORY_SUFFIXES)


def _class_reference(value_type: type[Any], *, require_exported: bool = True) -> str | None:
    module_name = getattr(value_type, "__module__", None)
    type_name = getattr(value_type, "__name__", None)
    if not isinstance(module_name, str) or not isinstance(type_name, str):
        return None
    if not type_name.isidentifier() or not all(
        part.isidentifier() for part in module_name.split(".")
    ):
        return None
    if require_exported:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, OSError):
            return None
        if getattr(module, type_name, None) is not value_type:
            return None
    return f"{module_name}.{type_name}"


def _observed_reference(value: Any) -> str:
    return _class_reference(type(value), require_exported=False) or repr(type(value))


def _common_return_reference(results: list[Any]) -> str | None:
    if not results:
        return None
    result_types = [type(result) for result in results]
    for candidate in inspect.getmro(result_types[0]):
        if candidate is object:
            continue
        if not all(candidate in inspect.getmro(other) for other in result_types[1:]):
            continue
        reference = _class_reference(candidate)
        if reference is not None:
            return reference
    return "builtins.object"


def _parameter_value(name: str, namespace: dict[str, Any]) -> Any:
    lower = name.lower()
    if lower in {"base", "base_ring", "ring", "parent", "r"}:
        return namespace.get("ZZ", 1)
    if lower in {"name", "names", "var", "variable", "variables"}:
        return "x"
    if lower in {"nrows", "ncols", "rows", "cols", "dimension", "dim"}:
        return 1
    if lower in {"prec", "precision", "bits", "digits"}:
        return 53
    if lower in {"modulus", "mod", "order", "degree", "p", "q", "size"}:
        return 29
    if lower in {"entries", "entry", "data", "elements"}:
        return [1]
    if lower in {"sparse", "proof", "check"}:
        return False
    return 1


def _call(*args: Any, **kwargs: Any) -> FactoryCall:
    return args, kwargs


def _candidate_calls(name: str, value: Any, namespace: dict[str, Any]) -> list[FactoryCall]:
    zz = namespace.get("ZZ", 1)
    qq = namespace.get("QQ", zz)
    explicit: dict[str, list[FactoryCall]] = {
        "Mod": [_call(5, 29), _call(5, 2**70 + 33)],
        "mod": [_call(5, 29), _call(5, 2**70 + 33)],
        "GF": [_call(29), _call(25, name="a")],
        "Zmod": [_call(29), _call(2**70 + 33)],
        "Integers": [_call(29), _call(2**70 + 33)],
        "IntegerModRing": [_call(29), _call(2**70 + 33)],
        "PolynomialRing": [_call(zz, "x"), _call(qq, names=("x", "y"))],
        "PowerSeriesRing": [_call(zz, "x")],
        "LaurentPolynomialRing": [_call(zz, "x")],
        "matrix": [_call(zz, 1, 1), _call([[1]])],
        "vector": [_call(zz, [1]), _call([1])],
        "MatrixSpace": [_call(zz, 1, 1)],
        "VectorSpace": [_call(qq, 1)],
        "RealField": [_call(), _call(100)],
        "ComplexField": [_call(), _call(100)],
        "FiniteField": [_call(29), _call(25, name="a")],
        "CyclotomicField": [_call(5)],
        "AdditiveAbelianGroup": [_call([2, 3])],
        "AffinePermutationGroup": [_call(["A", 2, 1])],
        "BrandtModule": [_call(5)],
        "DirichletGroup": [_call(5)],
        "EllipticCurve": [_call([0, 0, 1, -1, 0])],
        "FreeAlgebra": [_call(qq, 2, "x")],
        "FreeModule": [_call(zz, 2)],
        "FunctionField": [_call(qq, "x")],
        "Ideal": [_call(zz, 2)],
        "InfinitePolynomialRing": [_call(qq, "x")],
        "QuadraticField": [_call(2, "a")],
        "QuaternionAlgebra": [_call(qq, -1, -1)],
    }
    calls = list(explicit.get(name, ()))

    try:
        identity_matrix = namespace["identity_matrix"]
        polynomial_ring = namespace["PolynomialRing"]
        if name in {"FreeQuadraticModule", "InnerProductSpace", "QuadraticSpace"}:
            base = zz if name == "FreeQuadraticModule" else qq
            calls.append(_call(base, 2, identity_matrix(base, 2)))
        elif name == "CallableSymbolicExpressionRing":
            calls.append(_call((namespace["SR"].var("x"),)))
        elif name == "Curve":
            ring = polynomial_ring(qq, names=("x", "y"))
            x, y = ring.gens()
            calls.append(_call(x**2 + y**2 - 1))
        elif name in {"HyperellipticCurve", "NumberField", "PolynomialQuotientRing"}:
            ring = polynomial_ring(qq, "x")
            x = ring.gen()
            if name == "HyperellipticCurve":
                calls.append(_call(x**5 + x + 1))
            elif name == "NumberField":
                calls.append(_call(x**2 - 2, "a"))
            else:
                calls.append(_call(ring, x**2 + 1, "a"))
        elif name == "QuarticCurve":
            ring = polynomial_ring(qq, names=("x", "y", "z"))
            x, y, z = ring.gens()
            calls.append(_call(x**4 + y**4 + z**4))
        elif name == "ResidueField":
            number_field = namespace["QuadraticField"](5, "a")
            calls.append(_call(number_field.prime_above(3)))
        elif name == "PermutationGroup":
            calls.append(_call([namespace["Permutation"]([2, 1, 3])]))
        elif name == "TateAlgebra":
            calls.append(_call(namespace["Qp"](5), names=("x",)))
    except Exception:
        # A missing optional Sage component should leave this factory in the
        # explicit unresolved report rather than abort the complete run.
        pass

    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError):
        return calls

    positional: list[Any] = []
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.kind == parameter.KEYWORD_ONLY:
            continue
        if parameter.default is not parameter.empty:
            break
        positional.append(_parameter_value(parameter.name, namespace))
        if len(positional) > 4:
            return calls
    if positional:
        calls.append(_call(*positional))
    elif not calls:
        calls.append(_call())
    return calls


def _probe(value: Any, calls: list[FactoryCall]) -> list[Any]:
    results: list[Any] = []
    for args, kwargs in calls:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ), warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = value(*args, **kwargs)
            if result is not None and not inspect.ismodule(result):
                results.append(result)
        except Exception:
            continue
    return results


def infer_factory_returns(namespace: dict[str, Any]) -> tuple[list[FactoryInference], list[str]]:
    inferred: list[FactoryInference] = []
    unresolved: list[str] = []
    for name, value in sorted(namespace.items()):
        if not is_factory_candidate(name, value):
            continue
        results = _probe(value, _candidate_calls(name, value, namespace))
        return_type = _common_return_reference(results)
        if return_type is None:
            unresolved.append(name)
        else:
            observed = tuple(dict.fromkeys(_observed_reference(item) for item in results))
            inferred.append(FactoryInference(name, return_type, observed))
    return inferred, unresolved


def factory_return_map(inferences: list[FactoryInference]) -> dict[str, str]:
    return {item.name: item.return_type for item in inferences}
