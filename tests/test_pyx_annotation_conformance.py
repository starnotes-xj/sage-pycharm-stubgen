"""Parsing rules of the .pyx annotation conformance scan (scripts/).

The scan's value is its extraction precision: modifier tokens, attribute
declarations, local ``cdef`` variables with call initializers and plain
(non-``cdef``) classes must all be classified correctly or the preservation
ratio lies.  These tests pin each rule.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "pyx_annotation_conformance.py"
)
_spec = importlib.util.spec_from_file_location("pyx_annotation_conformance", _SCRIPT)
scan = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("pyx_annotation_conformance", scan)
_spec.loader.exec_module(scan)


def write_pyx(tmp: str, text: str) -> Path:
    source = Path(tmp) / "mod.pyx"
    source.write_text(text, encoding="utf-8")
    return source


class PyxSourceAnnotationsTests(unittest.TestCase):
    def test_arrow_and_cython_forms_are_captured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = write_pyx(
                tmp,
                "def typed(x) -> Integer:\n"
                "    pass\n"
                "cpdef tuple seq(t):\n"
                "    pass\n",
            )
            found = scan.pyx_source_annotations(source)
        self.assertEqual(found["typed"], ("Integer", "arrow", "Integer"))
        # ``cpdef tuple`` canonicalizes through the cython type map; the raw
        # token stays verbatim so equivalent stub spellings compare equal.
        self.assertEqual(found["seq"], ("tuple[Any, ...]", "cython", "tuple"))

    def test_modifier_tokens_are_not_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = write_pyx(
                tmp,
                "cpdef inline int twice(int x):\n"
                "    return 2 * x\n",
            )
            found = scan.pyx_source_annotations(source)
        self.assertEqual(found["twice"], ("int", "cython", "int"))

    def test_cdef_functions_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = write_pyx(
                tmp,
                "cdef int helper(int x):\n"
                "    return x\n"
                "cpdef int public_api(int x):\n"
                "    return helper(x)\n",
            )
            found = scan.pyx_source_annotations(source)
        self.assertNotIn("helper", found)
        self.assertIn("public_api", found)

    def test_attribute_and_local_variable_declarations_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = write_pyx(
                tmp,
                "cdef class Owner:\n"
                "    cdef FrozenBitset flags\n"
                "    cpdef Integer compute(self, Integer n):\n"
                "        cdef Integer r = <Integer> max(self.f(n) for n in range(3))\n"
                "        return r\n",
            )
            found = scan.pyx_source_annotations(source)
        self.assertNotIn("Owner.flags", found)
        self.assertNotIn("Owner.r", found)
        self.assertEqual(found["Owner.compute"], ("Integer", "cython", "Integer"))

    def test_plain_python_classes_scope_methods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = write_pyx(
                tmp,
                "class Plain:\n"
                "    def value(self) -> str:\n"
                "        return 'x'\n",
            )
            found = scan.pyx_source_annotations(source)
        self.assertEqual(found["Plain.value"], ("str", "arrow", "str"))

    def test_private_members_are_flagged_by_the_scan_only(self) -> None:
        # The extractor keeps private defs; privacy is a scan-level concern.
        with tempfile.TemporaryDirectory() as tmp:
            source = write_pyx(
                tmp,
                "cpdef int _hidden(int x):\n"
                "    return x\n",
            )
            found = scan.pyx_source_annotations(source)
        self.assertIn("_hidden", found)


class StubReturnsTests(unittest.TestCase):
    def test_class_scoped_returns_with_none_for_unannotated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "mod.pyi"
            stub.write_text(
                "def top(x) -> int: ...\n"
                "class C:\n"
                "    def typed(self) -> str: ...\n"
                "    def bare(self): ...\n",
                encoding="utf-8",
            )
            found = scan.stub_returns(stub)
        self.assertEqual(found["top"], "int")
        self.assertEqual(found["C.typed"], "str")
        self.assertIsNone(found["C.bare"])


if __name__ == "__main__":
    unittest.main()
