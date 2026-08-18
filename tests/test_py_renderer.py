from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.generator import DEFAULT_PY_EXCLUDES, discover_sources
from sage_pycharm_stubgen.py_renderer import is_stub_candidate, render_py_stub

SAMPLE_MODULE = '''\
"""A pure-python demo module."""

import math
from sage.rings.integer import Integer

__all__ = ["prime_divisors"]

GOLDEN = 1.618


def prime_divisors(n):
    """Return the prime divisors of n."""
    return sorted({p for p in range(2, n) if n % p == 0})


class Demo:
    """A demo class."""

    CLASS_ATTR = 1

    def __init__(self, name: str) -> None:
        self.name = name

    def nbits(self) -> int:
        """The bit length."""
        return 7


if __name__ == "__main__":
    print(prime_divisors(12))
'''


class PyRendererTests(unittest.TestCase):
    def test_render_keeps_signatures_and_strips_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "demo.py"
            path.write_text(SAMPLE_MODULE, encoding="utf-8")
            stub = render_py_stub(path)

        # The stub must be valid Python and keep every public definition.
        compile(stub, "demo.pyi", "exec")
        self.assertIn('def prime_divisors(n):', stub)
        self.assertIn('"""Return the prime divisors of n."""', stub)
        self.assertIn("class Demo:", stub)
        self.assertIn("def __init__(self, name: str) -> None:", stub)
        self.assertIn("def nbits(self) -> int:", stub)
        self.assertIn("__all__", stub)
        # Bodies are stripped: the generator expression is gone.
        self.assertNotIn("sorted({p for p in range(2, n)", stub)
        self.assertNotIn("self.name = name", stub)
        # The `if __name__ == "__main__"` guard is dropped entirely.
        self.assertNotIn("__main__", stub)
        self.assertNotIn("print(prime_divisors(12))", stub)
        # The module docstring survives.
        self.assertIn("A pure-python demo module", stub)

    def test_render_uses_ellipsis_for_bodyless_defs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "m.py"
            path.write_text("def f(x):\n    pass\n", encoding="utf-8")
            stub = render_py_stub(path)
        compile(stub, "m.pyi", "exec")
        self.assertIn("def f(x):\n    ...", stub)

    def test_discover_sources_skips_only_top_level_all(self) -> None:
        # Subpackage all.py files are star-import shims and ARE stubbed; only
        # the top-level all.py (dedicated generated stub) is excluded.
        self.assertTrue(is_stub_candidate("all.py"))
        self.assertTrue(is_stub_candidate("arith/all.py"))
        self.assertTrue(is_stub_candidate("arith/misc.py"))

    def test_discover_sources_include_py_filters_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "all.py").write_text("", encoding="utf-8")
            (root / "all__sagemath_objects.py").write_text("", encoding="utf-8")
            (root / "mod.py").write_text("", encoding="utf-8")
            (root / "__init__.py").write_text("", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_x.py").write_text("", encoding="utf-8")
            compiled = root / "compiled.pyx"
            compiled.write_text("", encoding="utf-8")
            sub = root / "arith"
            sub.mkdir()
            (sub / "all.py").write_text("", encoding="utf-8")

            sources = discover_sources(root, include_py=True)
            names = {s.name for s in sources}

        self.assertIn("mod.py", names)
        self.assertIn("compiled.pyx", names)
        # Subpackage all.py is a star-import shim and IS stubbed.
        self.assertIn("all.py", {s.name for s in sources if s.parent.name == "arith"})
        self.assertNotIn("all.py", {s.name for s in sources if s.parent == root})
        self.assertNotIn("all__sagemath_objects.py", names)
        self.assertNotIn("__init__.py", names)
        self.assertNotIn("test_x.py", names)

    def test_default_py_excludes_cover_dynamic_dirs(self) -> None:
        for pattern in (
            "**/__init__.py",
            "**/tests/**",
            "**/distributions/**",
            "all.py",
        ):
            self.assertIn(pattern, DEFAULT_PY_EXCLUDES)


if __name__ == "__main__":
    unittest.main()
