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
    discover_sources,
    enhance_finite_field_stub,
    enhance_integer_mod_stub,
    enhance_integer_stub,
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
                "from sage.rings.ring import Field\n\n\nclass FiniteField(Field):\n    pass\n",
                encoding="utf-8",
            )

            changed = enhance_finite_field_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("def characteristic(self) -> Integer: ...", result)
        self.assertIn("from sage.rings.integer import Integer", result)
        self.assertNotIn("from typing import Any", result)
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

    def test_integer_stub_gains_arithmetic_dunders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stub = Path(temp_dir) / "integer.pyi"
            stub.write_text("class Integer(EuclideanDomainElement):\n    pass\n", encoding="utf-8")

            changed = enhance_integer_stub(stub)
            result = stub.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn("class Integer(EuclideanDomainElement):", result)
        self.assertIn(
            "def __pow__(self, exp: Any, mod: Any = None) -> Any: ...", result
        )
        self.assertIn("def __mul__(self, other: Any) -> Integer: ...", result)
        import ast

        ast.parse(result)  # the enhanced stub must stay valid Python

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
