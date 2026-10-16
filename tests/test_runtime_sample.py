import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.runtime_sample import (
    collect_candidates,
    run_batches,
    type_expression,
)


class TypeExpressionTests(unittest.TestCase):
    def test_not_implemented_and_none_are_rejected(self) -> None:
        self.assertIsNone(type_expression(NotImplemented))
        self.assertIsNone(type_expression(None))

    def test_builtin_and_sage_types_map(self) -> None:
        builtin_type = type(5)
        self.assertEqual(
            type_expression(builtin_type(5) if False else 5),
            {"expression": "int", "module": ""},
        )

        class Fake:
            pass

        Fake.__module__ = "sage.rings.integer"
        fake = Fake()
        self.assertEqual(
            type_expression(fake),
            {"expression": "Fake", "module": "sage.rings.integer"},
        )

    def test_dynamic_category_suffix_is_stripped(self) -> None:
        class EllipticCurvePoint_field_with_category:
            pass

        EllipticCurvePoint_field_with_category.__module__ = "sage.schemes.elliptic_curves.ell_point"
        value = EllipticCurvePoint_field_with_category()
        info = type_expression(value)
        self.assertEqual(info["expression"], "EllipticCurvePoint_field")
        self.assertEqual(
            info["module"],
            "sage.schemes.elliptic_curves",
        )

    def test_foreign_modules_are_rejected(self) -> None:
        class NotSage:
            pass

        self.assertIsNone(type_expression(NotSage()))


class BatchRunnerTests(unittest.TestCase):
    def test_collect_candidates_walks_sage_stub_tree(self) -> None:
        import tempfile

        from sage_pycharm_stubgen.runtime_sample import collect_candidates as collect

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            demo = root / "sage" / "demo"
            demo.mkdir(parents=True)
            (demo / "point.pyi").write_text(
                "class Point:\n    def xy(self): ...\n", encoding="utf-8"
            )
            (demo / "__init__.pyi").write_text("", encoding="utf-8")

            candidates = collect(root)

        self.assertEqual(
            candidates,
            [{"module": "sage.demo.point", "class": "Point"}],
        )

    def test_run_batches_samples_live_integer(self) -> None:
        """Integration: the isolated child samples live Sage operators."""
        try:
            import sage.all  # noqa: F401
        except ImportError:
            self.skipTest("live Sage not available")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rings = root / "sage" / "rings"
            rings.mkdir(parents=True)
            (rings / "integer.pyi").write_text(
                "class Integer:\n    def __mul__(self, other): ...\n",
                encoding="utf-8",
            )
            observed, crashed = run_batches(root, batch_size=10, timeout=300.0)

        self.assertEqual(crashed, [])
        mul = observed.get("sage.rings.integer.Integer", {}).get("__mul__")
        self.assertEqual(mul and mul["expression"], "Integer")


if __name__ == "__main__":
    unittest.main()
