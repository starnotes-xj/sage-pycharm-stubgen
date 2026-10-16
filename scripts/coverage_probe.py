"""Coverage probe: compare public runtime members of key classes against stubs."""

import ast
import importlib
import inspect


def _has_sig(cls, n):
    try:
        inspect.signature(getattr(cls, n))
        return True
    except Exception:
        return False


def stub_names(path, class_name):
    tree = ast.parse(open(path).read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    found.add(item.name)
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    found.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name):
                            found.add(t.id)
    return found


def origin(cls, name):
    for k in cls.__mro__:
        if name in vars(k):
            return k
    return None


CASES = [
    ("sage.rings.integer", "Integer", "sage/rings/integer.pyi"),
    ("sage.schemes.elliptic_curves.ell_point", "EllipticCurvePoint_finite_field", "sage/schemes/elliptic_curves/ell_point.pyi"),
    ("sage.schemes.elliptic_curves.ell_finite_field", "EllipticCurve_finite_field", "sage/schemes/elliptic_curves/ell_finite_field.pyi"),
    ("sage.rings.rational", "Rational", "sage/rings/rational.pyi"),
    ("sage.rings.finite_rings.element_givaro", "FiniteField_givaroElement", "sage/rings/finite_rings/element_givaro.pyi"),
    ("sage.rings.polynomial.polynomial_element", "Polynomial", "sage/rings/polynomial/polynomial_element.pyi"),
]

import sage.all  # noqa: F401,E402

for modname, clsname, stubrel in CASES:
    mod = importlib.import_module(modname)
    cls = getattr(mod, clsname)
    pub = [n for n in dir(cls) if not n.startswith("_")]
    sage_defined = []
    for n in pub:
        k = origin(cls, n)
        if k is None:
            continue
        m = getattr(k, "__module__", "")
        if m.startswith("sage."):
            sage_defined.append(n)
    stub = stub_names(stubrel, clsname)
    missing = sorted(set(sage_defined) - stub)
    pct = 100 * (len(sage_defined) - len(missing)) / max(len(sage_defined), 1)
    print(f"{clsname}: runtime={len(sage_defined)} stub={len(stub)} missing={len(missing)} coverage={pct:.1f}%")
    if missing:
        print("   missing:", missing[:12])
