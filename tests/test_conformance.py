import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.conformance import (
    check_actual_type,
    doctest_lines,
    find_source_file,
    normalize_annotation,
    run_conformance,
    run_runtime_checks,
    source_annotations,
)


class SourceAnnotationsTests(unittest.TestCase):
    def test_python_source_collects_def_and_method_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sage" / "demo" / "mod.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "def plain(x):\n"
                "    pass\n"
                "def typed(x: int) -> str:\n"
                "    pass\n"
                "class Holder:\n"
                "    def method(self, a) -> tuple[int, int]:\n"
                "        pass\n",
                encoding="utf-8",
            )
            annotations = source_annotations(source)
            self.assertEqual(annotations, {"typed": "str", "Holder.method": "tuple[int, int]"})

    def test_pyx_source_collects_def_and_cpdef_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sage" / "demo" / "mod.pyx"
            source.parent.mkdir(parents=True)
            source.write_text(
                "def plain(x):\n"
                "    pass\n"
                "def typed(x) -> str:\n"
                "    pass\n"
                "cpdef int ctyped(x):\n"
                "    pass\n"
                "cdef class Box:\n"
                "    def method(self) -> object:\n"
                "        pass\n",
                encoding="utf-8",
            )
            annotations = source_annotations(source)
            self.assertEqual(
                annotations,
                {"typed": "str", "ctyped": "int", "Box.method": "object"},
            )

    def test_find_source_file_prefers_py_then_pyx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sage" / "a").mkdir(parents=True)
            (root / "sage" / "a" / "both.pyx").write_text("", encoding="utf-8")
            self.assertEqual(find_source_file(root, "sage.a.both").suffix, ".pyx")
            (root / "sage" / "a" / "both.py").write_text("", encoding="utf-8")
            self.assertEqual(find_source_file(root, "sage.a.both").suffix, ".py")
            self.assertIsNone(find_source_file(root, "sage.a.missing"))


class NormalizeTests(unittest.TestCase):
    def test_normalize_ignores_whitespace_and_quotes(self) -> None:
        self.assertEqual(normalize_annotation("A | B"), normalize_annotation("  A|B "))
        self.assertEqual(normalize_annotation("'X'"), normalize_annotation("X"))
        self.assertEqual(
            normalize_annotation("tuple[int, int]"), normalize_annotation(" tuple[ int , int ] ")
        )


class ConformanceTests(unittest.TestCase):
    def _make_root(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / "sage" / "demo").mkdir(parents=True)
        return root

    def test_statuses_unannotated_ok_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_root(tmp)
            (root / "sage" / "demo" / "mod.py").write_text(
                "def untyped(x):\n"
                "    pass\n"
                "def matches(x) -> FiniteField:\n"
                "    pass\n"
                "def disagrees(x) -> str:\n"
                "    pass\n",
                encoding="utf-8",
            )
            curated = {
                "sage.demo.mod": {
                    "untyped": {"return": "int"},
                    "matches": {"return": "FiniteField"},
                    "disagrees": {"return": "int"},
                }
            }
            findings = run_conformance(root, curated=curated)
            by_qualname = {f.qualname: f for f in findings}
            self.assertEqual(by_qualname["untyped"].status, "unannotated")
            self.assertEqual(by_qualname["matches"].status, "ok")
            self.assertEqual(by_qualname["disagrees"].status, "conflict")

    def test_missing_source_counts_as_unannotated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_root(tmp)
            curated = {"sage.demo.ghost": {"f": {"return": "int"}}}
            findings = run_conformance(root, curated=curated)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].status, "unannotated")


class RuntimeCheckTests(unittest.TestCase):
    def test_doctest_lines_extracts_sage_and_python_prompts(self) -> None:
        doc = (
            "说明文本\n\n"
            "示例::\n\n"
            "    sage: F = GF(29)\n"
            "    sage: F.order()\n"
            "    29\n"
            "    sage: plain line\n"
            "    >>> F.order()\n"
        )
        self.assertEqual(
            doctest_lines(doc),
            ["F = GF(29)", "F.order()", "plain line", "F.order()"],
        )

    def test_check_actual_type_exact_subclass_union_generic(self) -> None:
        class Base:
            pass

        class Sub(Base):
            pass

        env = {"Base": Base, "Sub": Sub}
        self.assertTrue(check_actual_type("Base", Sub(), [], env)[0])
        self.assertTrue(check_actual_type("Sub", Sub(), [], env)[0])
        self.assertFalse(check_actual_type("Sub", Base(), [], env)[0])
        # union accepts any member; Any always passes
        self.assertTrue(check_actual_type("Base | int", Sub(), [], env)[0])
        self.assertTrue(check_actual_type("Any", Sub(), [], env)[0])
        # generic origin plus element types
        self.assertTrue(check_actual_type("list[Sub]", [Sub(), Sub()], [], env)[0])
        self.assertFalse(check_actual_type("list[Sub]", [Base()], [], env)[0])
        self.assertFalse(check_actual_type("list[Sub]", (Sub(),), [], env)[0])
        self.assertTrue(check_actual_type("tuple[Any, ...]", (Sub(), 3), [], env)[0])
        self.assertTrue(check_actual_type("Sequence[Sub]", [Sub()], [], env)[0])
        # nested generic and union inside a generic stay intact
        self.assertTrue(
            check_actual_type("list[tuple[int, int]]", [(1, 2)], [], env)[0]
        )
        self.assertTrue(
            check_actual_type("list[Sub | int]", [Sub(), 5], [], env)[0]
        )
        self.assertFalse(
            check_actual_type("list[Sub | int]", [Sub(), "x"], [], env)[0]
        )

    def test_split_union_keeps_bracketed_pipes(self) -> None:
        from sage_pycharm_stubgen.conformance import split_union

        self.assertEqual(split_union("A | B"), ["A", "B"])
        self.assertEqual(split_union("list[Integer | int]"), ["list[Integer | int]"])
        self.assertEqual(
            split_union("A | tuple[int, int] | C"), ["A", "tuple[int, int]", "C"]
        )

    def test_check_actual_type_resolves_aliased_imports(self) -> None:
        class Field:
            pass

        env = {"Field": Field}
        imports = ["from sage.demo.mod import Field as _FieldBase"]
        self.assertTrue(check_actual_type("_FieldBase", Field(), imports, env)[0])

    def test_run_runtime_checks_probes_examples_and_reports_mismatch(self) -> None:
        class FieldPoint:
            pass

        def lift_x(x, all=False, extend=False):
            return FieldPoint()

        def lift_x_wide(x, all=False, extend=False):
            return FieldPoint()

        env = {"FieldPoint": FieldPoint, "lift_x": lift_x, "lift_x_wide": lift_x_wide}
        curated = {
            "sage.demo.curves": {
                "EllipticCurve_generic.lift_x": {
                    "doc": (
                        "示例::\n\n"
                        "    sage: Q = lift_x(5)\n"
                        "    sage: Q.xy()\n"
                        "    (5, 1)\n"
                    ),
                    "return": "FieldPoint",
                },
                "EllipticCurve_generic.lift_x_wide": {
                    "doc": (
                        "示例::\n\n"
                        "    sage: Q = lift_x_wide(5)\n"
                        "    sage: Q.xy()\n"
                        "    (5, 1)\n"
                    ),
                    "return": "int",  # deliberately wrong
                },
                "EllipticCurve_generic.no_example": {
                    "doc": "没有示例。",
                    "return": "FieldPoint",
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            findings = run_runtime_checks(Path(tmp), curated=curated, namespace=env)

        by_qualname = {f.qualname: f for f in findings}
        self.assertEqual(by_qualname["EllipticCurve_generic.lift_x"].status, "ok")
        self.assertEqual(
            by_qualname["EllipticCurve_generic.lift_x_wide"].status, "mismatch"
        )
        self.assertIn("int", by_qualname["EllipticCurve_generic.lift_x_wide"].actual)
        self.assertEqual(
            by_qualname["EllipticCurve_generic.no_example"].status, "skipped"
        )


if __name__ == "__main__":
    unittest.main()
