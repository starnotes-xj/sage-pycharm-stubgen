from __future__ import annotations

import json
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path

from sage_pycharm_stubgen.cli import build_parser
from sage_pycharm_stubgen.generator import (
    audit_all_stub_imports,
    discover_sources,
    enhance_factory_instances,
    enhance_factorization_stub,
    enhance_finite_field_stub,
    enhance_instance_annotations,
    enhance_integer_mod_stub,
    enhance_integer_stub,
    enhance_lazy_imports,
    enhance_method_aliases,
    enhance_parent_chain,
    enhance_parent_getitem,
    render_sage_all_stub,
    render_runtime_module_stub,
)
from sage_pycharm_stubgen.installer import (
    MANIFEST_NAME,
    install_stub_package,
    uninstall_stub_package,
)
from sage_pycharm_stubgen.lint import lint_generated_stubs
from sage_pycharm_stubgen.merge import merge_generated_stubs


class GeneratorTests(unittest.TestCase):
    def test_cli_exposes_install_uninstall_and_preparse_modes(self) -> None:
        parser = build_parser()
        options = {
            option
            for action in parser._actions
            for option in action.option_strings
        }

        self.assertIn("--install", options)
        self.assertIn("--uninstall", options)
        self.assertNotIn("--install-in-place", options)
        self.assertNotIn("--uninstall-in-place", options)
        self.assertEqual(
            parser.parse_args(["--install"]).output,
            Path(sys.prefix) / "sage_typings",
        )
        self.assertIsNone(parser.parse_args(["--install"]).command)

    def test_discover_sources_honors_patterns_and_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "rings" / "tests").mkdir(parents=True)
            (root / "rings" / "integer_mod.pyx").write_text("", encoding="utf-8")
            (root / "rings" / "tests" / "helper.pyx").write_text("", encoding="utf-8")
            (root / "graphs.pyx").write_text("", encoding="utf-8")

            result = discover_sources(
                root,
                patterns=("rings/**/*.pyx",),
                excludes=("**/tests/**",),
            )

            self.assertEqual(result, [root / "rings" / "integer_mod.pyx"])

    def test_integer_mod_enhancement_adds_factory_return_types(self) -> None:
        content = """\
mod = Mod
class IntegerMod_abstract:
    def sqrt(self, extend=True, all=False): ...
def Mod(n, m, parent=None): ...
def IntegerMod(parent, value): ...
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "integer_mod.pyi"
            stub.write_text(content, encoding="utf-8")

            changed = enhance_integer_mod_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("def Mod(n, m, parent=None) -> IntegerMod_abstract:", result)
        self.assertIn("def IntegerMod(parent, value) -> IntegerMod_abstract:", result)
        self.assertIn("def mod(n, m, parent=None) -> IntegerMod_abstract: ...", result)
        self.assertNotIn("mod = Mod", result)

    def test_parent_chain_reconnects_dropped_base_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            structure = root / "sage" / "structure"
            structure.mkdir(parents=True)
            parent_old = structure / "parent_old.pyi"
            parent_base = structure / "parent_base.pyi"
            parent_old.write_text("class Parent:\n    def old(self): ...\n", encoding="utf-8")
            parent_base.write_text("class ParentWithBase:\n    pass\n", encoding="utf-8")
            rings = root / "sage" / "rings"
            rings.mkdir(parents=True)
            real_mpfr = rings / "real_mpfr.pyi"
            real_mpfr.write_text("class RealField_class:\n    pass\n", encoding="utf-8")

            changed = enhance_parent_chain(root)

            self.assertTrue(changed)
            old_text = parent_old.read_text(encoding="utf-8")
            self.assertIn(
                "from sage.structure.parent import Parent as _Parent", old_text
            )
            self.assertIn("class Parent(_Parent):", old_text)
            base_text = parent_base.read_text(encoding="utf-8")
            self.assertIn(
                "from sage.structure.parent_old import Parent as _ParentOld", base_text
            )
            self.assertIn("class ParentWithBase(_ParentOld):", base_text)
            real_text = real_mpfr.read_text(encoding="utf-8")
            self.assertIn("from sage.structure.parent import Parent", real_text)
            self.assertIn("class RealField_class(Parent):", real_text)
            # RR(1) needs Parent.__call__ to be reachable: the bridge must not
            # rely on a module-level import later than the class head.
            compile(real_text, "real_mpfr.pyi", "exec")

    def test_finite_field_stub_declares_characteristic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "finite_field_base.pyi"
            stub.write_text(
                "from sage.rings.ring import Field\nfrom typing import Any\n\n\n"
                "class FiniteField(Field):\n    def __iter__(self):\n        pass\n",
                encoding="utf-8",
            )

            changed = enhance_finite_field_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("def characteristic(self) -> Integer: ...", result)
        self.assertIn("from sage.rings.integer import Integer", result)
        # The enhancer must not introduce a second `Any` import.
        self.assertEqual(result.count("from typing import Any"), 1)
        # The typed __iter__ keeps the IDE's iteration-element type at the
        # element union instead of falling back to FiniteField itself (which
        # mis-types `for x in F` loops and flags `F.<a> = GF(...)` sugar).
        self.assertIn("from typing import Iterator", result)
        self.assertIn(
            "def __iter__(self) -> Iterator[FiniteField_givaroElement | "
            "FiniteField_ntl_gf2eElement | FiniteFieldElement_pari_ffelt | IntegerMod_int]:",
            result,
        )
        compile(result, "finite_field_base.pyi", "exec")

    def test_parent_getitem_accepts_generator_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            structure = root / "sage" / "structure"
            structure.mkdir(parents=True)
            parent = structure / "parent.pyi"
            parent.write_text(
                "class Parent[ElementT](CategoryObject):\n"
                "    def __getitem__(self, n: int | slice) -> ElementT: ...\n",
                encoding="utf-8",
            )

            changed = enhance_parent_getitem(root)

            self.assertTrue(changed)
            self.assertEqual(
                parent.read_text(encoding="utf-8"),
                "class Parent[ElementT](CategoryObject):\n"
                "    def __getitem__(self, n: Any) -> ElementT: ...\n",
            )

    def test_lazy_imports_resolved_when_target_is_declared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "sage" / "rings" / "finite_rings"
            target.mkdir(parents=True)
            (target / "finite_field_constructor.pyi").write_text(
                "def GF(*args: Any, **kwargs: Any) -> FiniteField: ...\n",
                encoding="utf-8",
            )
            graphs = root / "sage" / "graphs"
            graphs.mkdir(parents=True)
            stub = graphs / "strongly_regular_db.pyi"
            stub.write_text(
                "GF = LazyImport('sage.rings.finite_rings.finite_field_constructor', 'GF')\n"
                "Matrix = LazyImport('sage.matrix.constructor', 'Matrix')\n"
                "to_hex = LazyImport('matplotlib.colors', 'to_hex')\n",
                encoding="utf-8",
            )

            changed = enhance_lazy_imports(root)
            content = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn(
            "from sage.rings.finite_rings.finite_field_constructor import GF as GF",
            content,
        )
        # Target module has no generated stub -> keep the assignment.
        self.assertIn("Matrix = LazyImport('sage.matrix.constructor', 'Matrix')", content)
        # Non-sage target module -> keep the assignment.
        self.assertIn("to_hex = LazyImport('matplotlib.colors', 'to_hex')", content)

    def test_prune_stale_outputs_removes_only_pyi(self) -> None:
        from sage_pycharm_stubgen.generator import _prune_stale_outputs

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            deep = root / "sage" / "sub"
            deep.mkdir(parents=True)
            (deep / "old.pyi").write_text("class Old: ...\n", encoding="utf-8")
            (root / "all.pyi").write_text("", encoding="utf-8")
            keep = root / "generation-report.json"
            keep.write_text("{}", encoding="utf-8")

            pruned = _prune_stale_outputs(root)

            self.assertEqual(pruned, 2)
            self.assertFalse((deep / "old.pyi").exists())
            self.assertFalse((root / "all.pyi").exists())
            self.assertTrue(keep.exists())

    def test_method_alias_shadowing_ancestor_is_left_unpromoted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "sage" / "demo"
            target.mkdir(parents=True)
            (root / "sage" / "__init__.pyi").write_text("", encoding="utf-8")
            (target / "__init__.pyi").write_text("", encoding="utf-8")
            (target / "base.pyi").write_text(
                "class Base:\n    def is_zero(self) -> bool: ...\n",
                encoding="utf-8",
            )
            stub = target / "leaf.pyi"
            stub.write_text(
                "from sage.demo.base import Base\n\n\n"
                "class Leaf(Base):\n"
                "    def helper(self): ...\n"
                "    is_zero = helper\n"
                "    fresh = helper\n",
                encoding="utf-8",
            )

            from sage_pycharm_stubgen.runtime_audit import build_stub_hierarchy

            changed = enhance_method_aliases(root, hierarchy=build_stub_hierarchy(root))
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)  # `fresh` has no ancestor declaration: promoted
        self.assertIn("def fresh(self): ...", result)
        # is_zero is a def on the stub base: promoting the annotation-less copy
        # would shadow the ancestor's precise `-> bool` with nothing, so the
        # alias assignment stays as-is and MRO keeps the member callable.
        self.assertIn("is_zero = helper", result)
        self.assertNotIn("def is_zero(self): ...", result)

    def test_method_alias_precise_copy_promotes_despite_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "sage" / "demo"
            target.mkdir(parents=True)
            (root / "sage" / "__init__.pyi").write_text("", encoding="utf-8")
            (target / "__init__.pyi").write_text("", encoding="utf-8")
            (target / "base.pyi").write_text(
                "class Base:\n    def is_zero(self) -> bool: ...\n",
                encoding="utf-8",
            )
            stub = target / "leaf.pyi"
            stub.write_text(
                "from sage.demo.base import Base\n\n\n"
                "class Leaf(Base):\n"
                "    def helper(self) -> int: ...\n"
                "    is_zero = helper\n",
                encoding="utf-8",
            )

            from sage_pycharm_stubgen.runtime_audit import build_stub_hierarchy

            changed = enhance_method_aliases(root, hierarchy=build_stub_hierarchy(root))
            result = stub.read_text(encoding="utf-8")

        # The copied signature is PRECISE (`-> int`): promoting carries real
        # information and equals the ancestor's runtime semantics, so the
        # promotion goes through despite the base declaration.
        self.assertTrue(changed)
        self.assertIn("def is_zero(self) -> int: ...", result)

    def test_method_aliases_promoted_to_defs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "sage" / "demo"
            target.mkdir(parents=True)
            stub = target / "demo.pyi"
            stub.write_text(
                "class Widget:\n"
                "    def cardinality(self, algorithm=None) -> int: ...\n"
                "    order = cardinality\n"
                "    def size(self) -> int: ...\n"
                "    num_edges = size\n"
                "    class_attr = 3\n"
                "    class Inner:\n"
                "        def length(self) -> int: ...\n"
                "        n = length\n",
                encoding="utf-8",
            )

            changed = enhance_method_aliases(root)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn(
            "def order(self, algorithm=None) -> int: ...",
            result,
        )
        self.assertIn("def num_edges(self) -> int: ...", result)
        self.assertNotIn("order = cardinality", result)
        self.assertNotIn("num_edges = size", result)
        # Nested classes keep their deeper indentation.
        self.assertIn("        def n(self) -> int: ...", result)
        self.assertNotIn("        n = length", result)
        # Non-alias assignments are untouched.
        self.assertIn("class_attr = 3", result)
        import ast

        ast.parse(result)

    def test_factory_instances_reexported_from_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sage_dir = root / "sage"
            sage_dir.mkdir()
            (sage_dir / "all.pyi").write_text(
                "def GF(*args: Any, **kwargs: Any) -> _FactoryReturn_GF: ...\n"
                "def FiniteField(*args: Any, **kwargs: Any) -> _FactoryReturn_FiniteField: ...\n",
                encoding="utf-8",
            )
            rings = sage_dir / "rings" / "finite_rings"
            rings.mkdir(parents=True)
            stub = rings / "finite_field_constructor.pyi"
            stub.write_text(
                "FiniteField = FiniteFieldFactory('FiniteField')\n"
                "def ordinary(x): ...\n",
                encoding="utf-8",
            )

            changed = enhance_factory_instances(root)
            content = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("from sage.all import FiniteField as FiniteField", content)
        # Non-factory statements are untouched.
        self.assertIn("def ordinary(x): ...", content)

    def test_factory_instances_skipped_without_all_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sage_dir = root / "sage"
            sage_dir.mkdir()
            (sage_dir / "all.pyi").write_text(
                "def GF(*args: Any, **kwargs: Any) -> _FactoryReturn_GF: ...\n",
                encoding="utf-8",
            )
            rings = sage_dir / "rings" / "finite_rings"
            rings.mkdir(parents=True)
            stub = rings / "finite_field_constructor.pyi"
            stub.write_text(
                "FiniteField = FiniteFieldFactory('FiniteField')\n",
                encoding="utf-8",
            )

            changed = enhance_factory_instances(root)
            content = stub.read_text(encoding="utf-8")

        self.assertFalse(changed)
        self.assertIn("FiniteField = FiniteFieldFactory", content)

    def test_integer_stub_gains_arithmetic_dunders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "integer.pyi"
            stub.write_text(
                "from sage.rings.integer import Integer\n\n\n"
                "class Integer(EuclideanDomainElement):\n"
                "    pass\n",
                encoding="utf-8",
            )

            changed = enhance_integer_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("class Integer(EuclideanDomainElement):", result)
        self.assertIn(
            "def __pow__(self, exp: Any, mod: Any = None) -> Any: ...", result
        )
        # Scalar multiplication mirrors Sage coercion: Integer * Integer (and
        # int) stay Integer, any other operand keeps its own type.
        self.assertIn("def __mul__(self, other: Integer) -> Integer: ...", result)
        self.assertIn("def __mul__(self, other: int) -> Integer: ...", result)
        self.assertIn("def __mul__(self, other: T) -> T: ...", result)
        self.assertEqual(result.count("def __mul__("), 3)
        self.assertIn("from typing import Any, TypeVar, overload", result)
        self.assertIn('T = TypeVar("T")', result)
        import ast

        ast.parse(result)  # the enhanced stub must stay valid Python

    def test_integer_stub_drops_converter_untyped_mul(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "integer.pyi"
            stub.write_text(
                "from sage.rings.integer import Integer\n\n\n"
                "class Integer(EuclideanDomainElement):\n"
                "    def __mul__(left, right):\n"
                '        """TESTS::\n\n            sage: 3 * 2\n            6\n        """\n'
                "    def _mul_(self, right):\n"
                "        ...\n",
                encoding="utf-8",
            )

            changed = enhance_integer_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertNotIn("def __mul__(left, right)", result)
        self.assertIn("def _mul_(self, right):", result)
        self.assertIn("def __mul__(self, other: T) -> T: ...", result)
        import ast

        ast.parse(result)

    def test_factorization_stub_gains_iter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "factorization.pyi"
            stub.write_text(
                "from typing import Any\n"
                "class Factorization(SageObject):\n"
                "    def __getitem__(self, i): ...\n"
                "    def __len__(self): ...\n",
                encoding="utf-8",
            )

            changed = enhance_factorization_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn(
            "def __iter__(self) -> Iterator[tuple[Any, Any]]: ...", result
        )
        self.assertIn("from typing import Iterator", result)
        import ast

        ast.parse(result)

    def test_factorization_stub_iter_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "factorization.pyi"
            stub.write_text(
                "from typing import Any, Iterator\n"
                "class Factorization(SageObject):\n"
                "    def __iter__(self) -> Iterator[tuple[Any, Any]]: ...\n",
                encoding="utf-8",
            )

            self.assertFalse(enhance_factorization_stub(stub))

    def test_instance_annotations_annotate_same_file_class_singletons(self) -> None:
        # PyCharm does not evaluate bare assignments inside .pyi stubs, so the
        # `from <module> import ZZ as ZZ` re-export stays untyped and every
        # chained result collapses; the same-file singleton must carry an
        # explicit annotation for the type to flow.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stub = root / "integer_ring.pyi"
            stub.write_text(
                "from sage.rings.integer import Integer\n"
                "class IntegerRing_class:\n"
                "    def __call__(self, x=None) -> Integer: ...\n"
                "ZZ = IntegerRing_class()\n"
                "Z = ZZ\n"
                "QQ = RationalField()\n"
                "flag = None\n",
                encoding="utf-8",
            )
            other = root / "words.pyi"
            other.write_text("class WordClass:\n    pass\n", encoding="utf-8")

            self.assertTrue(enhance_instance_annotations(root))
            result = stub.read_text(encoding="utf-8")

        self.assertIn("ZZ: IntegerRing_class = IntegerRing_class()", result)
        self.assertNotRegex(result, r"(?m)^Z:")  # non-call RHS untouched
        self.assertNotRegex(result, r"(?m)^QQ:")  # func is not a same-file class
        self.assertNotRegex(result, r"(?m)^flag:")  # non-call assignment untouched
        import ast

        ast.parse(result)

        # Idempotent: the annotated form is an AnnAssign, not an Assign.
        self.assertFalse(enhance_instance_annotations(root))

    def test_instance_annotations_skip_self_named_singletons(self) -> None:
        # `Foo: Foo = Foo()` would make the annotation ambiguous with the
        # variable binding, so self-named singletons stay untouched.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stub = root / "words.pyi"
            stub.write_text(
                "class Word:\n    pass\nWord = Word()\n",
                encoding="utf-8",
            )

            self.assertFalse(enhance_instance_annotations(root))
            self.assertIn("Word = Word()", stub.read_text(encoding="utf-8"))

    def test_instance_annotations_use_runtime_class_for_factory_rhs(self) -> None:
        # `RR = RealField()` — the RHS names a factory def, but the runtime
        # module binds RR to a RealField_class instance whose class lives in
        # the same stub; the runtime MRO supplies the annotation class.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stub = root / "real_mpfr.pyi"
            stub.write_text(
                "class RealField_class:\n    pass\n"
                "def RealField(prec=53): ...\n"
                "RR = RealField()\n",
                encoding="utf-8",
            )

            class RealField_class:
                pass

            RealField_class.__module__ = "fake_real_mpfr"
            fake_module = types.ModuleType("fake_real_mpfr")
            fake_module.__dict__["RR"] = RealField_class()
            sys.modules["fake_real_mpfr"] = fake_module
            self.addCleanup(sys.modules.pop, "fake_real_mpfr", None)
            # The enhancer derives the dotted module from the file path, so
            # stage the stub under the matching directory name.
            staged = root / "fake_real_mpfr.pyi"
            stub.rename(staged)

            self.assertTrue(enhance_instance_annotations(root))
            result = staged.read_text(encoding="utf-8")

        self.assertIn("RR: RealField_class = RealField()", result)
        import ast

        ast.parse(result)

    def test_finite_field_stub_gains_characteristic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "finite_field_base.pyi"
            stub.write_text(
                "from sage.rings.ring import Field\n"
                "class FiniteField(Field):\n    pass\n",
                encoding="utf-8",
            )

            changed = enhance_finite_field_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn(
            "from sage.rings.integer import Integer", result
        )
        self.assertIn("class FiniteField(Field):", result)
        self.assertIn("def characteristic(self) -> Integer: ...", result)
        import ast

        ast.parse(result)  # the enhanced stub must stay valid Python

    def test_sage_all_stub_uses_explicit_imports_and_value_types(self) -> None:
        stub = render_sage_all_stub({"sqrt": math.sqrt, "pi": math.pi, "_hidden": 1})

        self.assertIn("from math import sqrt as sqrt", stub)
        self.assertIn("from builtins import float as _Type_pi", stub)
        self.assertIn("pi: _Type_pi", stub)
        self.assertNotIn("_hidden", stub)

    def test_sage_all_stub_keeps_type_alias_for_foreign_singletons(self) -> None:
        # math.pi's home module (builtins) does not bind `pi`, so the
        # annotation-alias shape must stay.
        stub = render_sage_all_stub({"pi": math.pi})

        self.assertIn("from builtins import float as _Type_pi", stub)
        self.assertIn("pi: _Type_pi", stub)

    def test_sage_all_stub_singletons_stay_attribute_form(self) -> None:
        # REGRESSION (2026-10-19 outage): the `_Type_*` attribute shape is
        # load-bearing for the plugin's resolve chain.  A same-name home
        # binding (ZZ lives in sage.rings.integer_ring) must NOT become a
        # from-import re-export — that resolves the name to the import
        # ELEMENT, the one target PyCharm cannot type, and every chained
        # result (q = ZZ(35) / ZZ(3)) degraded to the namespace fallback.
        home = types.ModuleType("sage.rings.integer_ring")

        class IntegerRing_class:
            pass

        IntegerRing_class.__module__ = "sage.rings.integer_ring"
        ring = IntegerRing_class()
        home.IntegerRing_class = IntegerRing_class
        home.ZZ = ring
        sys.modules["sage.rings.integer_ring"] = home
        self.addCleanup(sys.modules.pop, "sage.rings.integer_ring", None)

        stub = render_sage_all_stub({"ZZ": ring})

        self.assertIn(
            "from sage.rings.integer_ring import IntegerRing_class as _Type_ZZ", stub
        )
        self.assertIn("ZZ: _Type_ZZ", stub)
        self.assertNotIn("import ZZ as ZZ", stub)
        import ast

        ast.parse(stub)

    def test_annotation_falls_back_to_declared_mro_base(self) -> None:
        # The symbolic constant `e` is a runtime-only `E` subclass of the
        # declared `Expression`; with the tree's declared-class index the
        # annotation must target the declared base instead of dangling.
        home = types.ModuleType("sage.symbolic.expression")

        class Expression:
            pass

        class E(Expression):
            pass

        for cls in (Expression, E):
            cls.__module__ = "sage.symbolic.expression"
        home.Expression = Expression
        home.E = E
        sys.modules["sage.symbolic.expression"] = home
        self.addCleanup(sys.modules.pop, "sage.symbolic.expression", None)

        declared = {("sage.symbolic.expression", "Expression")}
        stub = render_sage_all_stub({"e": E()}, declared_classes=declared)

        self.assertIn(
            "from sage.symbolic.expression import Expression as _Type_e", stub
        )
        self.assertNotIn("import E as _Type_e", stub)

    def test_audit_all_stub_imports_detects_dangling_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "sage").mkdir()
            (root / "sage" / "rings").mkdir()
            (root / "sage" / "rings" / "integer_ring.pyi").write_text(
                "class IntegerRing_class:\n    pass\n", encoding="utf-8"
            )
            (root / "sage" / "all.pyi").write_text(
                "from sage.rings.integer_ring import IntegerRing_class as _Type_ZZ\n"
                "from sage.rings.rational_field import RationalField as _Type_QQ\n"
                "ZZ: _Type_ZZ\n"
                "QQ: _Type_QQ\n",
                encoding="utf-8",
            )

            findings = audit_all_stub_imports(root)

        self.assertEqual(len(findings), 1)
        self.assertIn("RationalField", findings[0])
        self.assertIn("_Type_QQ", findings[0])

    def test_audit_all_stub_imports_flags_with_category(self) -> None:
        # Even if a *_with_category class were someday declared, importing a
        # runtime category wrapper is always a finding (2026-10-19 QQ case).
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "sage").mkdir()
            (root / "sage" / "rings").mkdir()
            (root / "sage" / "rings" / "rational_field.pyi").write_text(
                "class RationalField_with_category:\n    pass\n", encoding="utf-8"
            )
            (root / "sage" / "all.pyi").write_text(
                "from sage.rings.rational_field import RationalField_with_category as _Type_QQ\n"
                "QQ: _Type_QQ\n",
                encoding="utf-8",
            )

            findings = audit_all_stub_imports(root)

        self.assertEqual(len(findings), 1)
        self.assertIn("runtime-only", findings[0])

    def test_audit_all_stub_imports_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "sage").mkdir()
            (root / "sage" / "rings").mkdir()
            (root / "sage" / "rings" / "integer_ring.pyi").write_text(
                "class IntegerRing_class:\n    pass\n", encoding="utf-8"
            )
            (root / "sage" / "all.pyi").write_text(
                "from sage.rings.integer_ring import IntegerRing_class as _Type_ZZ\n"
                "from sage.rings.integer_ring import IntegerRing_class as _FactoryReturn_IntegerRing\n"
                "ZZ: _Type_ZZ\n",
                encoding="utf-8",
            )

            self.assertEqual(audit_all_stub_imports(root), [])

    def test_audit_all_stub_imports_requires_all_stub(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(audit_all_stub_imports(Path(temp_dir)), [])

    def test_sage_all_stub_normalizes_with_category_singletons(self) -> None:
        # The category machinery wraps QQ in RationalField_with_category —
        # a RUNTIME-ONLY class the stubs never declare; the annotation must
        # target the declared plain base instead of dangling.
        home = types.ModuleType("fake_rational_field")

        class RationalField:
            pass

        class RationalField_with_category(RationalField):
            pass

        for cls in (RationalField, RationalField_with_category):
            cls.__module__ = "fake_rational_field"
        home.RationalField = RationalField
        home.RationalField_with_category = RationalField_with_category
        sys.modules["fake_rational_field"] = home
        self.addCleanup(sys.modules.pop, "fake_rational_field", None)

        stub = render_sage_all_stub({"QQ": RationalField_with_category()})

        self.assertIn(
            "from fake_rational_field import RationalField as _Type_QQ", stub
        )
        self.assertIn("QQ: _Type_QQ", stub)
        self.assertNotIn("RationalField_with_category", stub)

    def test_runtime_fallback_uses_extension_signatures(self) -> None:
        module = types.ModuleType("example")

        def public(value, count=1):
            return value

        setattr(module, "public", public)
        setattr(module, "answer", 42)
        setattr(module, "_private", public)

        stub = render_runtime_module_stub(module)

        self.assertIn("def public(value, count=1) -> Any: ...", stub)
        self.assertIn("answer: Any", stub)
        self.assertNotIn("_private", stub)

    def test_sage_all_stub_can_replace_factory_with_typed_declaration(self) -> None:
        def factory(number):
            return number

        stub = render_sage_all_stub({"Mod": factory}, {"Mod": "builtins.int"})

        self.assertIn("from builtins import int as _FactoryReturn_Mod", stub)
        self.assertIn("def Mod(number) -> _FactoryReturn_Mod: ...", stub)

    def test_factory_return_annotation_is_replaced(self) -> None:
        def factory(number) -> str:
            return str(number)

        stub = render_sage_all_stub({"Factory": factory}, {"Factory": "builtins.int"})

        self.assertIn("def Factory(number) -> _FactoryReturn_Factory: ...", stub)
        self.assertNotIn("-> str ->", stub)

    def test_integer_stub_declares_truediv_overloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "integer.pyi"
            stub.write_text(
                "from sage.rings.integer import Integer\n\n\n"
                "class Integer(EuclideanDomainElement):\n"
                "    pass\n",
                encoding="utf-8",
            )
            changed = enhance_integer_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        # ZZ(5) / QQ(2, 3) and friends divide into a Rational; anything
        # else keeps the permissive catch-all.
        self.assertIn(
            "def __truediv__(self, other: Integer | int | Rational) -> Rational: ...",
            result,
        )
        self.assertIn("def __truediv__(self, other: Any) -> Any: ...", result)
        self.assertIn("from sage.rings.rational import Rational", result)
        import ast

        ast.parse(result)

    def test_integer_stub_inserts_typing_import_without_anchor(self) -> None:
        # Pre-0.9.0 the enhancer anchored the typing import on the
        # `from sage.rings.integer import Integer` line and inserted
        # nothing when that line was absent, leaving the overload set
        # referencing undefined names.
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "integer.pyi"
            stub.write_text(
                "class Integer(EuclideanDomainElement):\n    pass\n",
                encoding="utf-8",
            )
            changed = enhance_integer_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("from typing import Any, TypeVar, overload", result)
        self.assertIn('T = TypeVar("T")', result)
        import ast

        ast.parse(result)

    def test_merge_removes_converter_duplicates_after_integer_enhance(self) -> None:
        # Regression for the installed 0.8.4 artifact: __mul__/__pow__ were
        # each declared twice and the plain def shadowed the overload set,
        # collapsing ZZ(5) * QQ(...) to Integer.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stub = root / "sage" / "rings" / "integer.pyi"
            stub.parent.mkdir(parents=True)
            stub.write_text(
                "from sage.rings.integer import Integer\n\n\n"
                "class Integer(EuclideanDomainElement):\n"
                "    def gcd(self, other): ...\n"
                "    def __pow__(left, right, modulus=None): ...\n"
                "    def __mul__(left, right): ...\n"
                "    def sqrt(self, all=False): ...\n",
                encoding="utf-8",
            )
            self.assertTrue(enhance_integer_stub(stub))
            merge_generated_stubs(root)
            errors, _warnings = lint_generated_stubs(root, check_curated=False)
            self.assertEqual(errors, [])
            result = stub.read_text(encoding="utf-8")

        self.assertEqual(result.count("def __mul__("), 3)
        self.assertEqual(result.count("def __pow__("), 1)
        self.assertEqual(result.count("def __add__("), 1)
        self.assertEqual(result.count("def __truediv__("), 2)
        import ast

        ast.parse(result)

    def test_installer_rejects_invalid_stub_before_touching_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "generated" / "sage"
            sage_package = root / "site-packages" / "sage"
            output.mkdir(parents=True)
            sage_package.mkdir(parents=True)
            (sage_package / "all.py").write_text("answer = 42\n", encoding="utf-8")
            (output / "broken.pyi").write_text(
                "def broken( -> int: ...\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "invalid stub"):
                install_stub_package(root / "generated", sage_package, "10.9")

            self.assertFalse((sage_package / "broken.pyi").exists())
            self.assertFalse((sage_package / MANIFEST_NAME).exists())

    def test_installer_updates_only_owned_stub_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "generated" / "sage"
            sage_package = root / "site-packages" / "sage"
            (output / "misc").mkdir(parents=True)
            (sage_package / "misc").mkdir(parents=True)
            (sage_package / "all.py").write_text("answer = 42\n", encoding="utf-8")
            (sage_package / "misc" / "persist.py").write_text(
                "def load(path): return path\n", encoding="utf-8"
            )
            (output / "all.pyi").write_text("def Mod(n, m): ...\n", encoding="utf-8")
            (output / "misc" / "persist.pyi").write_text(
                "def load(path: str): ...\n", encoding="utf-8"
            )

            first = install_stub_package(root / "generated", sage_package, "10.9")
            foreign = sage_package / "foreign.pyi"
            foreign.write_text("foreign: int\n", encoding="utf-8")

            self.assertTrue((sage_package / "all.pyi").is_file())
            self.assertTrue((sage_package / "misc" / "persist.pyi").is_file())
            self.assertTrue(first.manifest.is_file())

            (output / "misc" / "persist.pyi").unlink()
            (output / "misc" / "new.pyi").write_text(
                "new_value: int\n", encoding="utf-8"
            )
            second = install_stub_package(root / "generated", sage_package, "10.10")

            self.assertEqual(second.removed_stale_files, 1)
            self.assertFalse((sage_package / "misc" / "persist.pyi").exists())
            self.assertTrue((sage_package / "misc" / "persist.py").is_file())
            self.assertTrue((sage_package / "misc" / "new.pyi").is_file())

            removed = uninstall_stub_package(sage_package)

            self.assertGreater(removed.removed_files, 0)
            self.assertTrue((sage_package / "all.py").is_file())
            self.assertTrue((sage_package / "misc" / "persist.py").is_file())
            self.assertTrue(foreign.is_file())
            self.assertFalse((sage_package / "all.pyi").exists())
            self.assertFalse((sage_package / MANIFEST_NAME).exists())

    def test_installer_preserves_unmanaged_stub(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "generated" / "sage"
            sage_package = root / "site-packages" / "sage"
            output.mkdir(parents=True)
            sage_package.mkdir(parents=True)
            (sage_package / "all.py").write_text("answer = 42\n", encoding="utf-8")
            (sage_package / "all.pyi").write_text(
                "user_owned: int\n", encoding="utf-8"
            )
            (output / "all.pyi").write_text("generated: int\n", encoding="utf-8")

            result = install_stub_package(root / "generated", sage_package, "10.9")

            self.assertEqual(result.preserved_existing_files, 1)
            self.assertEqual(
                (sage_package / "all.pyi").read_text(encoding="utf-8"),
                "user_owned: int\n",
            )

    def test_installer_takes_over_unmanaged_stub_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "generated" / "sage"
            sage_package = root / "site-packages" / "sage"
            output.mkdir(parents=True)
            sage_package.mkdir(parents=True)
            (sage_package / "all.py").write_text("answer = 42\n", encoding="utf-8")
            (sage_package / "all.pyi").write_text(
                "stale_foreign: int\n", encoding="utf-8"
            )
            (output / "all.pyi").write_text("generated: int\n", encoding="utf-8")

            result = install_stub_package(
                root / "generated", sage_package, "10.9", overwrite_unowned=True
            )

            self.assertEqual(result.preserved_existing_files, 0)
            self.assertEqual(result.taken_over_files, 1)
            self.assertEqual(
                (sage_package / "all.pyi").read_text(encoding="utf-8"),
                "generated: int\n",
            )
            self.assertEqual(
                (sage_package / "all.pyi.sps-bak").read_text(encoding="utf-8"),
                "stale_foreign: int\n",
            )

            removed = uninstall_stub_package(sage_package)

            self.assertEqual(removed.restored_backups, 1)
            self.assertEqual(
                (sage_package / "all.pyi").read_text(encoding="utf-8"),
                "stale_foreign: int\n",
            )
            self.assertFalse((sage_package / "all.pyi.sps-bak").exists())


if __name__ == "__main__":
    unittest.main()
