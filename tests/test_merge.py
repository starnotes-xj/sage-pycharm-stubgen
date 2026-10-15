import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.merge import merge_generated_stubs, merge_source, merge_stub_file


class MergeTests(unittest.TestCase):
    def test_overload_group_beats_plain_def_and_assignment(self) -> None:
        source = (
            "from typing import overload\n"
            "alias = lambda *a: None\n\n\n"
            "class C:\n"
            "    @overload\n"
            "    def __mul__(self, other: int) -> int: ...\n"
            "    @overload\n"
            "    def __mul__(self, other: str) -> str: ...\n"
            "    def __mul__(self, other): ...\n"
            "    __mul__ = alias\n"
        )
        merged, removed = merge_source(source)
        self.assertEqual(removed, 2)
        self.assertEqual(merged.count("def __mul__("), 2)  # only the overloads
        self.assertNotIn("__mul__ = alias", merged)
        import ast

        ast.parse(merged)

    def test_best_annotated_plain_def_wins(self) -> None:
        source = (
            "class C:\n"
            "    def f(self, other): ...\n"
            "    def f(self, other: int) -> int: ...\n"
        )
        merged, removed = merge_source(source)
        self.assertEqual(removed, 1)
        self.assertIn("def f(self, other: int) -> int: ...", merged)
        self.assertNotIn("def f(self, other): ...", merged)

    def test_tie_prefers_the_later_declaration(self) -> None:
        source = (
            "class C:\n"
            "    def f(self) -> int: ...\n"
            "    def f(self) -> str: ...\n"
        )
        merged, removed = merge_source(source)
        self.assertEqual(removed, 1)
        self.assertIn("def f(self) -> str: ...", merged)

    def test_def_beats_assignment(self) -> None:
        source = "class C:\n    def f(self) -> int: ...\n    f = _g\n"
        merged, removed = merge_source(source)
        self.assertEqual(removed, 1)
        self.assertIn("def f(self) -> int: ...", merged)
        self.assertNotIn("f = _g", merged)

    def test_last_assignment_wins(self) -> None:
        merged, removed = merge_source("x = 1\nx = 2\n")
        self.assertEqual(removed, 1)
        self.assertIn("x = 2", merged)
        self.assertNotIn("x = 1", merged)

    def test_chain_assignment_loses_only_the_colliding_name(self) -> None:
        source = "class C:\n    def GF(self) -> int: ...\n    GF = FiniteField = _x\n"
        merged, removed = merge_source(source)
        self.assertEqual(removed, 1)
        self.assertIn("def GF(self) -> int: ...", merged)
        self.assertIn("FiniteField = _x", merged)
        self.assertNotIn("GF = FiniteField", merged)

    def test_duplicate_nested_class_keeps_members_intact(self) -> None:
        source = (
            "class Outer:\n"
            "    class Inner:\n"
            "        def a(self): ...\n"
            "        def a(self): ...\n"
            "    class Inner:\n"
            "        def b(self): ...\n"
        )
        merged, removed = merge_source(source)
        self.assertEqual(removed, 2)  # the first Inner plus its duplicate member
        self.assertNotIn("def a(", merged)
        self.assertIn("def b(", merged)
        import ast

        ast.parse(merged)

    def test_unparsable_input_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "broken.pyi"
            stub.write_text("class C:\n  def f(:\n", encoding="utf-8")
            self.assertEqual(merge_stub_file(stub), 0)
            self.assertEqual(stub.read_text(encoding="utf-8"), "class C:\n  def f(:\n")

    def test_merge_generated_stubs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sage"
            (root / "rings").mkdir(parents=True)
            (root / "rings" / "dup.pyi").write_text(
                "class C:\n    def f(self): ...\n    def f(self) -> int: ...\n",
                encoding="utf-8",
            )
            (root / "rings" / "clean.pyi").write_text(
                "class D:\n    pass\n", encoding="utf-8"
            )
            result = merge_generated_stubs(Path(temp_dir))
            self.assertEqual(result.changed_files, 1)
            self.assertEqual(result.removed_declarations, 1)
            self.assertIn(
                "def f(self) -> int: ...",
                (root / "rings" / "dup.pyi").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
