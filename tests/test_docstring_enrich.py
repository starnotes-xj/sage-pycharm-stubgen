from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.docstring_enrich import (
    EnrichmentSummary,
    RuntimeDocProvider,
    enrich_stub_file,
    extract_docstrings,
    stub_module_name,
)


class ExtractDocstringsTests(unittest.TestCase):
    def test_def_cpdef_and_cdef_docstrings(self) -> None:
        source = """\
def plain(self, n):
    \"\"\"Plain def docstring.\"\"\"

cpdef tuple first_ngens(self, n):
    r\"\"\"
    Returns the first ``n`` generators.

    EXAMPLES::

        sage: R.<x> = PolynomialRing(QQ)
        sage: R._first_ngens(1)
        ('x',)
    \"\"\"

cdef int count(self):
    'Single-quoted cdef docstring.'

def undocumented(self):
    return 1
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.pyx"
            path.write_text(source, encoding="utf-8")
            docs = extract_docstrings(path)

        self.assertIn("plain", docs)
        self.assertIn("first_ngens", docs)
        self.assertIn("count", docs)
        self.assertNotIn("undocumented", docs)
        self.assertEqual(docs["first_ngens"].return_type, "tuple[Any, ...]")
        self.assertEqual(docs["count"].return_type, "int")
        self.assertIsNone(docs["plain"].return_type)
        self.assertIn("('x',)", docs["first_ngens"].literal)

    def test_class_methods_and_class_docstring(self) -> None:
        source = """\
cdef class CategoryObject:
    \"\"\"Base class docstring.\"\"\"

    def variable_names(self):
        \"\"\"Return the variable names.\"\"\"

    cpdef bint is_defined(self):
        \"\"\"Check.\"\"\"
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.pyx"
            path.write_text(source, encoding="utf-8")
            docs = extract_docstrings(path)

        self.assertIn("CategoryObject", docs)
        self.assertIn("CategoryObject.variable_names", docs)
        self.assertEqual(
            docs["CategoryObject.is_defined"].return_type, "bool"
        )

    def test_decorated_def_and_comments_before_docstring(self) -> None:
        source = """\
@cached_method
def cached_thing(self):
    # a comment must not hide the docstring
    \"\"\"Cached docstring.\"\"\"
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.pyx"
            path.write_text(source, encoding="utf-8")
            docs = extract_docstrings(path)

        self.assertIn("cached_thing", docs)


class EnrichStubFileTests(unittest.TestCase):
    def _enrich(self, stub_text: str, source_text: str | None = None, curated=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stub = root / "sample.pyi"
            stub.write_text(stub_text, encoding="utf-8")
            source = None
            if source_text is not None:
                source = root / "sample.pyx"
                source.write_text(source_text, encoding="utf-8")
            summary = EnrichmentSummary()
            enrich_stub_file(
                stub,
                source,
                curated or {},
                None,
                "sage.sample",
                summary,
            )
            return stub.read_text(encoding="utf-8"), summary

    def test_source_docstring_becomes_function_body(self) -> None:
        stub = "from typing import Any\n\n\nclass CategoryObject:\n    def _first_ngens(self, n: int) -> Any: ...\n"
        source = (
            "cdef class CategoryObject:\n"
            "    def _first_ngens(self, n):\n"
            '        """Used by the preparser for ``R.<x> = ...``."""\n'
        )
        content, summary = self._enrich(stub, source)

        self.assertIn("Used by the preparser", content)
        self.assertIn('def _first_ngens(self, n: int) -> Any:', content)
        self.assertEqual(summary.docstrings_attached, 1)
        compile(content, "sample.pyi", "exec")

    def test_dangling_docstring_is_moved_into_body(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "def f(self) -> Any: ...\n"
            '"""A misplaced docstring."""\n'
        )
        content, summary = self._enrich(stub, None)

        self.assertIn('def f(self) -> Any:', content)
        self.assertIn("A misplaced docstring.", content)
        self.assertNotIn('def f(self) -> Any: ...\n"""', content)
        self.assertEqual(summary.docstrings_moved, 1)
        self.assertEqual(summary.docstrings_attached, 1)
        # The result must still be a valid stub.
        compile(content, "sample.pyi", "exec")

    def test_curated_doc_and_return_override(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "class CategoryObject:\n"
            "    def _first_ngens(self, n: int) -> Any: ...\n"
        )
        curated = {
            "CategoryObject._first_ngens": {
                "doc": "返回前 n 个生成元的名称元组。\\n\\n返回:\\ntuple -- 生成元名称",
                "return": "tuple[Any, ...]",
                "imports": [],
            }
        }
        content, summary = self._enrich(stub, None, curated)

        self.assertIn("-> tuple[Any, ...]:", content)
        self.assertIn("返回前 n 个生成元", content)
        self.assertEqual(summary.curated_applied, 1)
        self.assertEqual(summary.return_types_added, 1)
        compile(content, "sample.pyi", "exec")

    def test_curated_element_union_return_with_imports(self) -> None:
        stub = (
            "from sage.rings.ring import Field\n\n\n"
            "class FiniteField(Field):\n"
            "    def from_integer(self, n, reverse=False): ...\n"
        )
        curated = {
            "FiniteField.from_integer": {
                "doc": "把整数 n 解释为域元素。\\n\\n返回:\\n域元素 -- 对应元素",
                "return": (
                    "FiniteField_givaroElement | FiniteField_ntl_gf2eElement "
                    "| FiniteFieldElement_pari_ffelt"
                ),
                "imports": [
                    "from sage.rings.finite_rings.element_givaro import FiniteField_givaroElement",
                    "from sage.rings.finite_rings.element_ntl_gf2e import FiniteField_ntl_gf2eElement",
                    "from sage.rings.finite_rings.element_pari_ffelt import FiniteFieldElement_pari_ffelt",
                ],
            }
        }
        content, summary = self._enrich(stub, None, curated)

        self.assertIn(
            "def from_integer(self, n, reverse=False) -> "
            "FiniteField_givaroElement | FiniteField_ntl_gf2eElement | FiniteFieldElement_pari_ffelt:",
            content,
        )
        self.assertIn(
            "from sage.rings.finite_rings.element_givaro import FiniteField_givaroElement",
            content,
        )
        self.assertEqual(summary.return_types_added, 1)
        compile(content, "sample.pyi", "exec")

    def test_cython_return_type_fills_any(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "def count(self) -> Any: ...\n"
        )
        source = (
            "cdef int count(self):\n"
            '    """Count things."""\n'
        )
        content, summary = self._enrich(stub, source)

        self.assertIn("def count(self) -> int:", content)
        self.assertEqual(summary.return_types_added, 1)
        compile(content, "sample.pyi", "exec")

    def test_unknown_class_annotation_is_not_injected(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "def build(self) -> Any: ...\n"
        )
        source = (
            "cdef Polynomial build(self):\n"
            '    """Build a polynomial."""\n'
        )
        content, summary = self._enrich(stub, source)

        # Polynomial is not imported in the stub, so Any must stay.
        self.assertIn("def build(self) -> Any:", content)
        self.assertEqual(summary.return_types_added, 0)

    def test_existing_in_body_docstring_is_kept_without_curated(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "def f(self) -> Any:\n"
            '    """Already documented."""\n'
        )
        content, summary = self._enrich(stub, None)

        self.assertIn('"""Already documented."""', content)
        self.assertEqual(summary.docstrings_attached, 0)
        self.assertEqual(summary.docstrings_moved, 0)

    def test_curated_replaces_existing_docstring(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "def f(self) -> Any:\n"
            '    """Old English docstring."""\n'
        )
        curated = {"f": {"doc": "新的中文文档。", "return": "int", "imports": []}}
        content, summary = self._enrich(stub, None, curated)

        self.assertIn("新的中文文档。", content)
        self.assertNotIn("Old English docstring", content)
        self.assertIn("def f(self) -> int:", content)
        compile(content, "sample.pyi", "exec")

    def test_curated_imports_are_inserted(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "class FiniteField:\n"
            "    def characteristic(self) -> Any: ...\n"
        )
        curated = {
            "FiniteField.characteristic": {
                "doc": "返回域的特征。",
                "return": "Integer",
                "imports": ["from sage.rings.integer import Integer"],
            }
        }
        content, summary = self._enrich(stub, None, curated)

        self.assertIn("from sage.rings.integer import Integer", content)
        self.assertIn("-> Integer:", content)
        compile(content, "sample.pyi", "exec")

    def test_multiline_docstring_with_backslashes_survives(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "def f(self) -> Any: ...\n"
        )
        source = (
            "def f(self):\n"
            '    """Line one.\n'
            "\n"
            "    LaTeX like \\\\frac{a}{b} and \\\\ZZ stay verbatim.\n"
            '    """\n'
        )
        content, summary = self._enrich(stub, source)

        self.assertIn("\\frac{a}{b}", content)
        self.assertIn("\\ZZ", content)
        compile(content, "sample.pyi", "exec")

    def test_overload_declarations_are_skipped(self) -> None:
        stub = (
            "from typing import Any, overload\n\n\n"
            "@overload\n"
            "def f(self, x: int) -> int: ...\n"
            "@overload\n"
            "def f(self, x: str) -> str: ...\n"
        )
        content, summary = self._enrich(stub, None)

        self.assertEqual(summary.docstrings_attached, 0)
        self.assertEqual(content, stub)

    def test_inline_docstring_replaced_by_curated_keeps_def_intact(self) -> None:
        # Regression: same-line edits (def-line rewrite + body removal)
        # previously corrupted the def line and the one below it.
        stub = (
            'from typing import Any\n\n\n'
            'def f() -> Any: """Old doc."""\n'
            "\n"
            "def g() -> Any: ...\n"
        )
        curated = {"f": {"doc": "新的中文文档。", "return": "int", "imports": []}}
        content, summary = self._enrich(stub, None, curated)

        compile(content, "sample.pyi", "exec")
        self.assertIn("def f() -> int:", content)
        self.assertIn("新的中文文档。", content)
        self.assertNotIn("Old doc.", content)
        self.assertIn("def g() -> Any: ...", content)

    def test_def_line_with_string_default_containing_paren(self) -> None:
        # Regression: the paren scan used to stop inside the string default.
        stub = (
            'from typing import Any\n\n\n'
            'def f(x: str = "):") -> Any: ...\n'
        )
        curated = {"f": {"doc": "doc", "return": "int", "imports": []}}
        content, summary = self._enrich(stub, None, curated)

        compile(content, "sample.pyi", "exec")
        self.assertIn('def f(x: str = "):") -> int:', content)

    def test_doc_ending_with_quote_falls_back_to_repr(self) -> None:
        stub = (
            'from typing import Any\n\n\n'
            "def f() -> Any: ...\n"
        )
        curated = {"f": {"doc": '第一行\n结尾有引号"', "return": None, "imports": []}}
        content, summary = self._enrich(stub, None, curated)

        compile(content, "sample.pyi", "exec")
        self.assertIn('结尾有引号"', content)

    def test_class_with_inline_ellipsis_body_gets_docstring(self) -> None:
        # Regression: plan_class used to append the docstring after
        # `class X: ...`, producing an invalid stub.
        stub = (
            "from typing import Any\n\n\n"
            "class Rational: ...\n"
        )
        source = (
            "cdef class Rational:\n"
            '    """A rational number class."""\n'
        )
        content, summary = self._enrich(stub, source)

        compile(content, "sample.pyi", "exec")
        self.assertIn("class Rational:", content)
        self.assertIn("A rational number class.", content)
        self.assertNotIn("class Rational: ...", content)

    def test_declare_appends_missing_member_to_class(self) -> None:
        stub = (
            "from typing import Any\n\n\n"
            "class CategoryObject(SageObject):\n"
            '    """Class docstring stays first."""\n'
            "\n"
            "    def variable_names(self) -> tuple[str, ...]: ...\n"
        )
        curated = {
            "CategoryObject._first_ngens": {
                "doc": "返回前 n 个生成元。",
                "return": "tuple[Any, ...]",
                "declare": "def _first_ngens(self, n: int) -> tuple[Any, ...]",
                "imports": [],
            }
        }
        content, summary = self._enrich(stub, None, curated)

        compile(content, "sample.pyi", "exec")
        self.assertIn("def _first_ngens(self, n: int) -> tuple[Any, ...]:", content)
        self.assertIn("返回前 n 个生成元。", content)
        # The class docstring must remain the first statement of the suite.
        self.assertLess(content.index('"""Class docstring stays first."""'), content.index("def _first_ngens"))
        self.assertEqual(summary.declarations_added, 1)

    def test_def_with_own_docstring_does_not_steal_following_string(self) -> None:
        # Regression: a def carrying its docstring used to delete it and
        # adopt the following sibling string (often a class docstring).
        stub = (
            "from typing import Any\n\n\n"
            "class A:\n"
            "    def m1(self) -> Any:\n"
            '        """Real doc of m1."""\n'
            '    """Class A docstring."""\n'
        )
        content, summary = self._enrich(stub, None)

        self.assertIn('"""Real doc of m1."""', content)
        self.assertIn('"""Class A docstring."""', content)
        self.assertEqual(summary.docstrings_attached, 0)

    def test_inline_comment_after_ellipsis_body(self) -> None:
        # Regression: `...  # comment` used to defeat the body cut and
        # produce an indented docstring after a completed def line.
        stub = (
            "from typing import Any\n\n\n"
            "def f() -> Any: ...  # noqa\n"
        )
        source = (
            "def f():\n"
            '    """Doc for f."""\n'
        )
        content, summary = self._enrich(stub, source)

        compile(content, "sample.pyi", "exec")
        self.assertIn("def f() -> Any:", content)
        self.assertIn("Doc for f.", content)

    def test_insert_imports_after_module_docstring(self) -> None:
        # Regression: with no existing imports, new import lines were
        # inserted before the module docstring.
        stub = (
            '"""Module docstring."""\n'
            "\n"
            "class A:\n"
            "    def m(self) -> Any: ...\n"
        )
        curated = {
            "A.m": {
                "doc": "doc",
                "return": "Integer",
                "imports": ["from sage.rings.integer import Integer"],
            }
        }
        content, summary = self._enrich(stub, None, curated)

        self.assertLess(content.index('"""Module docstring."""'), content.index("from sage.rings.integer import Integer"))
        compile(content, "sample.pyi", "exec")


class RuntimeDocProviderTests(unittest.TestCase):
    def test_module_docs_lookup(self) -> None:
        provider = RuntimeDocProvider()
        docs = provider.module_docs("builtins", {"len"})
        self.assertIn("len", docs)
        self.assertIsInstance(docs["len"], str)

    def test_missing_module_is_recorded(self) -> None:
        provider = RuntimeDocProvider()
        docs = provider.module_docs("sage.no_such_module_xyz", {"f"})
        self.assertEqual(docs, {})
        self.assertEqual(len(provider.failures), 1)
        self.assertTrue(provider.failures[0].startswith("sage.no_such_module_xyz:"))


class StubModuleNameTests(unittest.TestCase):
    def test_stub_module_name(self) -> None:
        root = Path("/out")
        stub = Path("/out/sage/structure/parent.pyi")
        self.assertEqual(stub_module_name(root, stub), "sage.structure.parent")

    def test_non_module_files_are_none(self) -> None:
        root = Path("/out")
        for name in ("all.pyi", "all_cmdline.pyi", "__init__.pyi"):
            self.assertIsNone(
                stub_module_name(root, Path(f"/out/sage/{name}"))
            )


if __name__ == "__main__":
    unittest.main()
