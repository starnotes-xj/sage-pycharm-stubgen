import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.conformance import (
    find_source_file,
    normalize_annotation,
    run_conformance,
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


if __name__ == "__main__":
    unittest.main()
