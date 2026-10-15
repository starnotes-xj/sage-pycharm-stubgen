import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.lint import lint_generated_stubs, lint_source


class LintTests(unittest.TestCase):
    def test_overload_shadow_is_an_error(self) -> None:
        source = (
            "from typing import overload\n\n\n"
            "class C:\n"
            "    @overload\n"
            "    def __mul__(self, other: int) -> int: ...\n"
            "    @overload\n"
            "    def __mul__(self, other: str) -> str: ...\n"
            "    def __mul__(self, other): ...\n"
        )
        findings = lint_source(source, "sage/x.pyi")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "overload-shadow")
        self.assertEqual(findings[0].severity, "error")

    def test_duplicate_plain_member_is_an_error(self) -> None:
        source = "class C:\n    def f(self): ...\n    def f(self): ...\n"
        findings = lint_source(source, "sage/x.pyi")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "duplicate-member")
        self.assertEqual(findings[0].severity, "error")

    def test_member_shadowed_by_assignment_is_an_error(self) -> None:
        source = "class C:\n    def f(self) -> int: ...\n    f = _g\n"
        findings = lint_source(source, "sage/x.pyi")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "member-shadowed-by-assignment")

    def test_unresolved_annotation_is_a_warning(self) -> None:
        source = "class C:\n    def f(self) -> Mystery: ...\n"
        findings = lint_source(source, "sage/x.pyi")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unresolved-annotation")
        self.assertEqual(findings[0].severity, "warning")
        self.assertIn("Mystery", findings[0].detail)

    def test_typevar_and_imports_count_as_known(self) -> None:
        source = (
            "from typing import Any, TypeVar, overload\n"
            "from sage.rings.integer import Integer\n"
            'T = TypeVar("T")\n\n\n'
            "class C:\n"
            "    @overload\n"
            "    def __mul__(self, other: Integer) -> Integer: ...\n"
            "    @overload\n"
            "    def __mul__(self, other: T) -> T: ...\n"
        )
        self.assertEqual(lint_source(source, "sage/x.pyi"), [])

    def test_unparsable_stub_is_an_error(self) -> None:
        findings = lint_source("class C:\n  def f(:\n", "sage/x.pyi")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unparsable")
        self.assertEqual(findings[0].severity, "error")

    def test_lint_generated_stubs_splits_errors_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sage = root / "sage"
            sage.mkdir()
            (sage / "bad.pyi").write_text(
                "class C:\n    def f(self): ...\n    def f(self): ...\n",
                encoding="utf-8",
            )
            (sage / "warn.pyi").write_text(
                "class D:\n    def g(self) -> Mystery: ...\n",
                encoding="utf-8",
            )
            # check_curated=False: the real curated table would otherwise
            # add its coverage warnings to this synthetic tree.
            errors, warnings = lint_generated_stubs(root, check_curated=False)
            self.assertEqual([f.kind for f in errors], ["duplicate-member"])
            self.assertEqual([f.kind for f in warnings], ["unresolved-annotation"])


if __name__ == "__main__":
    unittest.main()
