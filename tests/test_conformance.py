import json
import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.conformance import (
    check_actual_type,
    doctest_lines,
    export_upstream_patches,
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

    def test_pyx_local_cdef_declarations_do_not_pollute(self) -> None:
        # Real regressions behind the 2026-10 conflict batch: a local
        # ``cdef Py_ssize_t row`` inside a method body used to be recorded
        # as ``Matrix.row -> Py_ssize_t``, and a local whose name collided
        # with a real method (``cdef ZZ_c content``) OVERWROTE that
        # method's genuine annotation.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sage" / "demo" / "matrix1.pyx"
            source.parent.mkdir(parents=True)
            source.write_text(
                "cdef class Matrix:\n"
                "    cpdef Integer content(self):\n"
                "        return Integer(1)\n"
                "    def row(self, i, from_list=False):\n"
                "        cdef Py_ssize_t row\n"
                "        return row\n"
                "    cdef int _c_only(self):\n"
                "        return 0\n"
                "    cpdef inline int polished(self):\n"
                "        return 1\n",
                encoding="utf-8",
            )
            annotations = source_annotations(source)
            self.assertEqual(annotations.get("Matrix.content"), "Integer")
            self.assertNotIn("Matrix.row", annotations)
            self.assertNotIn("Matrix._c_only", annotations)
            self.assertEqual(annotations.get("Matrix.polished"), "int")

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

    def test_normalize_maps_c_level_spellings_and_bare_generics(self) -> None:
        from sage_pycharm_stubgen.conformance import _declarations_match

        self.assertEqual(normalize_annotation("Py_ssize_t"), "int")
        self.assertEqual(normalize_annotation("bint"), "bool")
        self.assertEqual(normalize_annotation("list"), "list[Any]")
        self.assertEqual(normalize_annotation("Sequence"), "Sequence[Any]")
        # Precision differences are agreement, not conflict...
        self.assertTrue(_declarations_match("list", "list[Any]"))
        self.assertTrue(_declarations_match("list", "list[Integer]"))
        self.assertTrue(_declarations_match("Sequence", "Sequence[Any]"))
        self.assertTrue(_declarations_match("Py_ssize_t", "int"))
        # ...but concrete parameter disagreements still conflict.
        self.assertFalse(_declarations_match("list[str]", "list[Integer]"))
        self.assertFalse(_declarations_match("tuple[Any]", "list[Any]"))


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


class UpstreamExportTests(unittest.TestCase):
    def _make_package(self, tmp: str) -> Path:
        root = Path(tmp)
        source = root / "sage" / "demo" / "mod.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            '"""Demo."""\n'
            "from sage.rings.integer import Integer\n"
            "\n"
            "\n"
            "def divisors(n):\n"
            "    return [Integer(1), Integer(n)]\n"
            "\n"
            "\n"
            "class Helper:\n"
            "    def sqrt(self):\n"
            "        return Integer(2)\n",
            encoding="utf-8",
        )
        compiled = root / "sage" / "demo" / "fast.pyx"
        compiled.write_text(
            "def hidden(n):\n    return n\n", encoding="utf-8"
        )
        return root

    def test_export_annotates_unannotated_and_skips_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_package(tmp)
            out = Path(tmp) / "patches"
            curated = {
                "sage.demo.mod": {
                    "divisors": {
                        "return": "list[Integer]",
                        "imports": ["from sage.rings.integer import Integer"],
                    },
                    "Helper.sqrt": {"return": "Integer"},
                    # already declared upstream-equivalent -> skipped
                    "declared": {"return": "Integer"},
                },
                "sage.demo.fast": {"hidden": {"return": "Integer"}},
                "sage.demo.missing": {"gone": {"return": "Integer"}},
            }
            # add an already-annotated def for the "declared" entry
            source = root / "sage" / "demo" / "mod.py"
            source.write_text(
                source.read_text(encoding="utf-8")
                + "\n\ndef declared(n) -> Integer:\n    return n\n",
                encoding="utf-8",
            )
            result = export_upstream_patches(root, out, curated)

            self.assertEqual(result.annotated, 2)
            self.assertEqual(len(result.patches), 1)
            patch = Path(result.patches[0]).read_text(encoding="utf-8")
            self.assertIn("-> list[Integer]", patch)
            self.assertIn("-> Integer", patch)
            # Integer is already imported in the module; no import TODO added
            self.assertNotIn("TODO(upstream)", patch)
            self.assertIn("sage.demo.fast.hidden", result.skipped_compiled)
            self.assertIn("sage.demo.mod.declared", result.skipped_annotated)
            self.assertIn("sage.demo.missing.gone", result.skipped_not_found)
            # The exporter itself refuses to write a patch whose patched
            # source does not parse (fail-closed), so reaching here with a
            # patch already proves syntactic validity.
            self.assertTrue(patch.startswith("---"))
            self.assertTrue(patch.splitlines()[1].startswith("+++"))

    def test_export_adds_todo_import_for_missing_annotation_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_package(tmp)
            out = Path(tmp) / "patches"
            curated = {
                "sage.demo.mod": {
                    "divisors": {
                        "return": "list[Rational]",
                        "imports": ["from sage.rings.rational import Rational"],
                    },
                },
            }
            result = export_upstream_patches(root, out, curated)
            self.assertEqual(result.annotated, 1)
            self.assertEqual(result.imports_added, 1)
            patch = Path(result.patches[0]).read_text(encoding="utf-8")
            self.assertIn(
                "from sage.rings.rational import Rational  # TODO(upstream)",
                patch,
            )


class ExportPyiTests(unittest.TestCase):
    def _make_package(self, tmp: str) -> tuple[Path, dict]:
        root = Path(tmp)
        module_dir = root / "sage" / "demo"
        module_dir.mkdir(parents=True)
        (module_dir / "compiled.pyx").write_text(
            "cdef class Box:\n    cpdef Integer public_thing(self):\n        pass\n",
            encoding="utf-8",
        )
        (module_dir / "pure.py").write_text("def f(x):\n    pass\n", encoding="utf-8")
        (module_dir / "compiled.pyi").write_text(
            "# Generated by sage-pycharm-stubgen. Do not edit by hand.\n"
            "from typing import Any, TypeVar\n"
            "from sage.rings.integer import Integer\n"
            "\n"
            '"""\u6a21\u5757\u6587\u6863\u5b57\u7b26\u4e32"""\n'
            "\n"
            "def helper(x: list) -> list:\n"
            '    """\u4e2d\u6587\u6587\u6863"""\n'
            "\n"
            "# a function alias shadowing the builtin collection\n"
            "list = helper\n"
            "\n"
            "# converter placeholders for C globals: mis-typed upstream\n"
            "set_helper_gen = None\n"
            "initialized = False\n"
            "T2 = TypeVar('T2')\n"
            "__all__ = ['Box']\n"
            "\n"
            "class Box:\n"
            '    """Box \u6587\u6863"""\n'
            "    def public_thing(self) -> Integer: ...\n"
            "    def _private_thing(self) -> Any: ...\n"
            "    def __add__(self, other) -> Box: ...\n"
            "    def __create_key__(self) -> Any: ...\n"
            "\n"
            "class OnlyPrivate:\n"
            "    def _hidden(self) -> Any: ...\n",
            encoding="utf-8",
        )
        curated = {
            "sage.demo.compiled": {"Box.public_thing": {"return": "Integer"}},
            "sage.demo.pure": {"f": {"return": "int"}},
            "sage.demo.nostub": {"g": {"return": "int"}},
        }
        return root, curated

    def test_export_pyi_strips_docs_filters_private_qualifies_builtins(self) -> None:
        from sage_pycharm_stubgen.conformance import export_pyi_stubs

        with tempfile.TemporaryDirectory() as tmp:
            root, curated = self._make_package(tmp)
            out = Path(tmp) / "export"
            result = export_pyi_stubs(root, out, curated)

            self.assertEqual(len(result.files), 1)
            text = Path(result.files[0]).read_text(encoding="utf-8")
            # upstream header, no generated-marker comment
            self.assertTrue(text.startswith("from __future__ import annotations"))
            self.assertNotIn("sage-pycharm-stubgen", text)
            # docstrings (module/function/class, incl. Chinese) are gone
            self.assertNotIn("\u6587\u6863", text)
            self.assertNotIn('"""', text)
            # private members and non-allowlisted dunders dropped
            self.assertNotIn("_private_thing", text)
            self.assertNotIn("__create_key__", text)
            # public API and arithmetic dunders kept, curated return applied
            self.assertIn("def public_thing(self) -> Integer", text)
            self.assertIn("def __add__(self, other)", text)
            # ``list`` is shadowed by an assign → annotations qualify
            self.assertIn("builtins.list", text)
            self.assertGreater(result.builtin_qualifications, 0)
            # converter placeholder state assigns dropped; TypeVar/__all__ stay
            self.assertNotIn("set_helper_gen", text)
            self.assertNotIn("initialized", text)
            self.assertIn("T2 = TypeVar('T2')", text)
            self.assertIn("__all__", text)
            # a class whose members are all private keeps an empty-but-valid body
            self.assertRegex(text, r"class OnlyPrivate:\s*\.\.\.")

    def test_export_pyi_skips_pure_and_missing_stubs(self) -> None:
        from sage_pycharm_stubgen.conformance import export_pyi_stubs

        with tempfile.TemporaryDirectory() as tmp:
            root, curated = self._make_package(tmp)
            out = Path(tmp) / "export"
            result = export_pyi_stubs(root, out, curated)
            self.assertIn("sage.demo.pure", result.skipped_not_pyx)
            self.assertIn("sage.demo.nostub", result.skipped_no_entries)

    def test_export_pyi_emits_assert_type_probe_companion(self) -> None:
        import unittest.mock

        from sage_pycharm_stubgen.conformance import export_pyi_stubs
        from sage_pycharm_stubgen import runtime_sample

        with tempfile.TemporaryDirectory() as tmp:
            root, curated = self._make_package(tmp)
            out = Path(tmp) / "export"
            with unittest.mock.patch.object(
                runtime_sample,
                "RECEIVER_RECIPES",
                [("Box", "cls(6)")],
            ):
                result = export_pyi_stubs(root, out, curated)

            self.assertEqual(len(result.probe_files), 1)
            probe = Path(result.probe_files[0]).read_text(encoding="utf-8")
            self.assertIn("from sage.all import *", probe)
            self.assertIn("from sage.demo.compiled import *", probe)
            # annotation imports come from the stub's own import table
            self.assertIn("from sage.rings.integer import Integer", probe)
            # receiver binding follows the recipe; annotations stay plain
            self.assertIn("r_Box = cls(6)", probe)
            self.assertIn("assert_type(r_Box.public_thing(), Integer)", probe)
            self.assertNotIn("_m.", probe)
            self.assertGreater(result.probe_assertions, 0)


if __name__ == "__main__":
    unittest.main()


class PerceptionTests(unittest.TestCase):
    """Upstream-coverage perception: overload stacks, .pyi siblings, factories."""

    def _tree(self, tmp: str):
        root = Path(tmp) / "sage"
        pkg = root / "demo"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        return root, pkg

    def test_overload_stack_contributes_each_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, pkg = self._tree(tmp)
            source = pkg / "mod.py"
            source.write_text(
                "from typing import overload\n"
                "\n"
                "@overload\n"
                "def poly(a: int, b: int) -> int: ...\n"
                "@overload\n"
                "def poly(a: object, b: object) -> tuple: ...\n"
                "def poly(a, b):\n"
                "    return a + b\n",
                encoding="utf-8",
            )
            from sage_pycharm_stubgen.conformance import python_annotation_variants

            variants = python_annotation_variants(source)["poly"]
            self.assertIn(("int", "source-overload"), variants)
            self.assertIn(("tuple", "source-overload"), variants)

    def test_pyi_sibling_resolves_pyx_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, pkg = self._tree(tmp)
            (pkg / "compiled.pyx").write_text(
                "def order(self):\n    return 5\n", encoding="utf-8"
            )
            (pkg / "compiled.pyi").write_text(
                "class Thing:\n    def order(self) -> int: ...\n", encoding="utf-8"
            )
            from sage_pycharm_stubgen.conformance import resolve_declaration

            variants = resolve_declaration(
                "sage.demo.compiled", "Thing.order", root
            )
            self.assertEqual(variants, [("int", "pyi")])

    def test_manifest_owned_sibling_stub_is_circular_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, pkg = self._tree(tmp)
            nested = pkg / "sub"
            nested.mkdir()
            (nested / "compiled.pyx").write_text(
                "def order(self):\n    return 5\n", encoding="utf-8"
            )
            (nested / "compiled.pyi").write_text(
                "class Thing:\n    def order(self) -> int: ...\n", encoding="utf-8"
            )
            from sage_pycharm_stubgen.conformance import resolve_declaration

            # Without a manifest the sibling is a genuine upstream stub.
            variants = resolve_declaration(
                "sage.demo.sub.compiled", "Thing.order", root
            )
            self.assertEqual(variants, [("int", "pyi")])

            # An in-place install manifest at the package root owns that
            # stub no matter how deep the module sits: matching a stub we
            # installed ourselves is the curated table agreeing with
            # itself, so it must not count as upstream coverage.  Format 2
            # manifests list files under "file_hashes".
            (root / ".sage-pycharm-stubgen-in-place.json").write_text(
                json.dumps({"file_hashes": {"demo/sub/compiled.pyi": "0" * 64}}),
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_declaration("sage.demo.sub.compiled", "Thing.order", root),
                [],
            )

            # Legacy format-1 manifests list plain paths under "files".
            (root / ".sage-pycharm-stubgen-in-place.json").write_text(
                json.dumps({"files": ["demo/sub/compiled.pyi"]}), encoding="utf-8"
            )
            self.assertEqual(
                resolve_declaration("sage.demo.sub.compiled", "Thing.order", root),
                [],
            )

    def test_pyi_root_previews_open_pr_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, pkg = self._tree(tmp)
            (pkg / "compiled.pyx").write_text(
                "def gcd(self, right):\n    pass\n", encoding="utf-8"
            )
            extra = Path(tmp) / "checkout" / "sage" / "demo"
            extra.mkdir(parents=True)
            (extra / "compiled.pyi").write_text(
                "class Thing:\n    def gcd(self, right) -> int: ...\n",
                encoding="utf-8",
            )
            from sage_pycharm_stubgen.conformance import resolve_declaration

            variants = resolve_declaration(
                "sage.demo.compiled", "Thing.gcd", root, [extra.parents[1]]
            )
            self.assertEqual(variants, [("int", "pyi")])

    def test_factory_instance_call_class_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, pkg = self._tree(tmp)
            source = pkg / "mod.py"
            source.write_text(
                "from typing import Callable\n"
                "\n"
                "class Factory:\n"
                "    __call__: Callable[..., int]\n"
                "\n"
                "GF = Factory()\n",
                encoding="utf-8",
            )
            from sage_pycharm_stubgen.conformance import python_annotation_variants

            variants = python_annotation_variants(source)["GF"]
            self.assertEqual(variants, [("int", "source-factory")])

    def test_run_conformance_reports_origin_and_prune_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, pkg = self._tree(tmp)
            (pkg / "mod.py").write_text(
                "from typing import Callable, overload\n"
                "\n"
                "class Factory:\n"
                "    __call__: Callable[..., int]\n"
                "\n"
                "GF = Factory()\n"
                "\n"
                "@overload\n"
                "def poly(a: int, b: int) -> int: ...\n"
                "def poly(a, b):\n"
                "    return a + b\n"
                "\n"
                "def untouched(x):\n"
                "    return x\n",
                encoding="utf-8",
            )
            curated = {
                "sage.demo.mod": {
                    "GF": {"return": "int"},
                    "poly": {"return": "int"},
                    "untouched": {"return": "int"},
                }
            }
            findings = {f.qualname: f for f in run_conformance(root, curated)}
            self.assertEqual(findings["GF"].status, "ok")
            self.assertEqual(findings["GF"].origin, "source-factory")
            self.assertEqual(findings["poly"].status, "ok")
            self.assertEqual(findings["poly"].origin, "source-overload")
            self.assertEqual(findings["untouched"].status, "unannotated")
            self.assertIsNone(findings["untouched"].origin)

            from sage_pycharm_stubgen.conformance import prune_candidates

            candidates = prune_candidates(list(findings.values()))
            names = sorted(c["qualname"] for c in candidates)
            self.assertEqual(names, ["GF", "poly"])
            by_name = {c["qualname"]: c for c in candidates}
            self.assertEqual(by_name["GF"]["origin"], "source-factory")
            self.assertEqual(by_name["poly"]["matched_declaration"], "int")
