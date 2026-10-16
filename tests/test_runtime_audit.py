import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from sage_pycharm_stubgen.runtime_audit import (
    RuntimeAuditSummary,
    audit_stub_text,
    audit_stub_tree,
)


def _runtime(**members) -> types.SimpleNamespace:
    return types.SimpleNamespace(**members)


class RuntimeAuditTests(unittest.TestCase):
    def _audit(self, stub_text: str, namespace: dict) -> tuple[str, RuntimeAuditSummary]:
        return audit_stub_text(stub_text, _runtime(**namespace))

    def test_real_signature_is_appended_inside_the_class(self) -> None:
        class Integer:
            def gcd(self, right):
                return right

        stub = "class Integer:\n    def is_square(self) -> bool: ...\n"
        content, summary = self._audit(stub, {"Integer": Integer})

        self.assertIn("def gcd(self, right) -> Any: ...", content)
        self.assertEqual(summary.methods_added, 1)
        self.assertEqual(summary.files_touched, 0)  # tree-level counter, not per-file
        compile(content, "sample.pyi", "exec")

    def test_defaults_and_kwonly_survive(self) -> None:
        class Element:
            def nth_root(self, n, truncate_mode=0):
                return n

            def order(self, *, algorithm=None):
                return 1

        stub = "class Element:\n    pass\n".replace("    pass\n", "    def x(self): ...\n")
        content, _ = self._audit(stub, {"Element": Element})

        self.assertIn("def nth_root(self, n, truncate_mode=0) -> Any: ...", content)
        self.assertIn("def order(self, *, algorithm=None) -> Any: ...", content)

    def test_no_signature_falls_back_to_args_kwargs(self) -> None:
        from sage_pycharm_stubgen import runtime_audit as ra

        class Holder:
            expand = map  # builtin without an inspectable signature

        stub = "class Holder:\n    def known(self): ...\n"
        # Shield the recovery tiers so the placeholder path is what's tested;
        # under a live sage, sage_getdef genuinely recovers `(*args, **kwds)`
        # for `map` (covered by the recovery tests).
        with mock.patch.object(ra, "_recovered_argument_lists", return_value=iter([])):
            content, summary = self._audit(stub, {"Holder": Holder})

        self.assertIn("def expand(self, *args, **kwargs) -> Any: ...", content)
        self.assertEqual(summary.fallback_signatures, 1)

    def test_property_and_plain_attribute_kinds(self) -> None:
        class Parent:
            def __init__(self) -> None:
                self._cat = "rings"

            @property
            def category(self):
                return self._cat

            base_ring = "ZZ"

        stub = "class Parent:\n    def gen(self): ...\n"
        content, summary = self._audit(stub, {"Parent": Parent})

        self.assertIn("@property", content)
        self.assertIn("def category(self) -> Any: ...", content)
        self.assertIn("base_ring: Any = ...", content)
        self.assertEqual(summary.properties_added, 1)
        self.assertEqual(summary.attributes_added, 1)

    def test_already_declared_members_are_not_duplicated(self) -> None:
        class Integer:
            def gcd(self, right):
                return right

        stub = "class Integer:\n    def gcd(self, right) -> Integer: ...\n"
        content, summary = self._audit(stub, {"Integer": Integer})

        self.assertEqual(content, stub)
        self.assertEqual(summary.methods_added, 0)

    def test_typing_any_import_is_inserted_once(self) -> None:
        class Demo:
            def value(self):
                return 1

        stub = "from sage.other import Thing\n\n\nclass Demo:\n    def known(self): ...\n"
        content, _ = self._audit(stub, {"Demo": Demo})

        self.assertEqual(content.count("from typing import Any"), 1)
        self.assertLess(
            content.index("from typing import Any"),
            content.index("class Demo"),
        )

    def test_class_missing_from_runtime_is_skipped(self) -> None:
        stub = "class Ghost:\n    def known(self): ...\n"
        content, summary = self._audit(stub, {})

        self.assertEqual(content, stub)
        self.assertEqual(summary.classes_skipped, ["Ghost"])

    def test_inline_class_without_runtime_resolution_stays_untouched(self) -> None:
        # An inline class the runtime cannot resolve is left exactly as-is;
        # resolvable inline classes are expanded instead (see the expansion test).
        class Other:
            def known(self):
                return 1

        stub = "class Ghost: ...\n\nclass Other:\n    def known(self): ...\n"
        content, summary = self._audit(stub, {"Other": Other})

        self.assertEqual(content, stub)
        self.assertEqual(sorted(summary.classes_skipped), ["Ghost"])

    def test_unparseable_result_reverts_the_file(self) -> None:
        class Demo:
            def value(self):
                return 1

        stub = "class Demo:\n    def known(self): ...\n"
        with mock.patch(
            "sage_pycharm_stubgen.runtime_audit._render_member",
            return_value=(["def broken(: ..."], "method", False),
        ):
            content, summary = self._audit(stub, {"Demo": Demo})

        self.assertEqual(content, stub)
        self.assertTrue(any("revert" in e for e in summary.errors))

    def test_tree_audit_walks_files_and_reports_unimportable_modules(self) -> None:
        class Demo:
            def value(self):
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_dir = root / "sage" / "demo"
            module_dir.mkdir(parents=True)
            (module_dir / "mod.pyi").write_text(
                "class Demo:\n    def known(self): ...\n", encoding="utf-8"
            )
            (module_dir / "__init__.pyi").write_text("", encoding="utf-8")

            def importer(name: str):
                if name == "sage.demo.mod":
                    return _runtime(Demo=Demo)
                raise ImportError(name)

            summary = audit_stub_tree(root, importer=importer)

            self.assertEqual(summary.files_touched, 1)
            self.assertEqual(summary.methods_added, 1)
            self.assertEqual(summary.modules_skipped, [])
            rewritten = (module_dir / "mod.pyi").read_text(encoding="utf-8")
            self.assertIn("def value(self) -> Any: ...", rewritten)


    def test_docstring_signature_recovery(self) -> None:
        from sage_pycharm_stubgen import runtime_audit as ra

        self.assertEqual(
            ra._extract_argument_list("def nbits(self, x=None):", "nbits"),
            "self, x=None",
        )
        self.assertEqual(
            ra._extract_argument_list("Integer.gcd(self, right)", "gcd"),
            "self, right",
        )
        sig_target = types.SimpleNamespace(
            __doc__="sig:\n    def m(self, x=2)\n\n    Description."
        )
        self.assertEqual(ra._docstring_signature(sig_target, "m"), "self, x=2")
        embed = types.SimpleNamespace(__doc__="m(self, y=1)\n\nDescription.")
        self.assertEqual(ra._docstring_signature(embed, "m"), "self, y=1")
        prose = types.SimpleNamespace(__doc__="Return the bits of the value.")
        self.assertIsNone(ra._docstring_signature(prose, "bits"))

    def test_render_member_recovers_docstring_signature(self) -> None:
        from sage_pycharm_stubgen import runtime_audit as ra

        class Holder:
            __call__ = map  # uninspectable callable

        holder = Holder()
        holder.__doc__ = "compute(self, x, y=2)\n\nLong description."

        class Runtime:
            compute = holder

        stub = "class Runtime:\n    def known(self): ...\n"
        with mock.patch.object(ra, "_sage_getdef_signature", return_value=None):
            content, summary = audit_stub_text(stub, _runtime(Runtime=Runtime))

        self.assertIn("def compute(self, x, y=2) -> Any: ...", content)
        self.assertEqual(summary.signatures_recovered, 1)
        self.assertEqual(summary.fallback_signatures, 0)
        compile(content, "sample.pyi", "exec")

    def test_mro_aware_skip_and_missing_base_edge(self) -> None:
        class Base:
            def inherited(self, x):
                return x

        class Leaf(Base):
            def own(self):
                return 1

        class Orphan(Base):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "sage" / "demo"
            pkg.mkdir(parents=True)
            (root / "sage" / "__init__.pyi").write_text("", encoding="utf-8")
            (pkg / "__init__.pyi").write_text("", encoding="utf-8")
            (pkg / "base.pyi").write_text(
                "class Base:\n    def inherited(self, x) -> int: ...\n",
                encoding="utf-8",
            )
            (pkg / "leaf.pyi").write_text(
                "from sage.demo.base import Base\n\n\n"
                "class Leaf(Base):\n    def own(self): ...\n",
                encoding="utf-8",
            )
            (pkg / "orphan.pyi").write_text(
                "class Orphan:\n    def unrelated(self): ...\n",
                encoding="utf-8",
            )

            modules = {
                "sage.demo.base": _runtime(Base=Base),
                "sage.demo.leaf": _runtime(Leaf=Leaf),
                "sage.demo.orphan": _runtime(Orphan=Orphan),
            }

            def importer(name: str):
                return modules[name]

            summary = audit_stub_tree(root, importer=importer)

            leaf = (pkg / "leaf.pyi").read_text(encoding="utf-8")
            orphan = (pkg / "orphan.pyi").read_text(encoding="utf-8")
            # Leaf: `inherited` visible through the stub base edge — skipped.
            self.assertNotIn("def inherited", leaf)
            # Orphan: stub declares no base edge, so the flattened copy must
            # stay (removing it would lose completion entirely).
            self.assertIn("def inherited(self, x) -> Any: ...", orphan)
            self.assertEqual(summary.inherited_skipped, 1)

    def test_deep_scan_resolves_class_defined_but_not_exported(self) -> None:
        import sys as _sys

        from sage_pycharm_stubgen import runtime_audit as ra

        def value(self):
            return 1

        Klass = type("Klass", (), {"value": value, "__module__": "sage.demo.mod"})
        holder = types.ModuleType("sage.demo.holder")
        holder.Klass = Klass
        saved = _sys.modules.get("sage.demo.holder")
        _sys.modules["sage.demo.holder"] = holder
        ra._deep_class_index = None
        try:
            stub = "class Klass:\n    def known(self): ...\n"
            content, summary = audit_stub_text(
                stub, types.SimpleNamespace(), module_name="sage.demo.mod"
            )
        finally:
            if saved is None:
                _sys.modules.pop("sage.demo.holder", None)
            else:
                _sys.modules["sage.demo.holder"] = saved
            ra._deep_class_index = None

        self.assertIn("def value(self) -> Any: ...", content)
        self.assertEqual(summary.classes_resolved_deep, 1)
        self.assertEqual(summary.classes_skipped, [])

    def test_category_variant_resolves_to_base_class(self) -> None:
        class Base:
            def value(self):
                return 1

        stub = "class Base_with_category:\n    def known(self): ...\n"
        content, summary = audit_stub_text(
            stub, _runtime(Base=Base), module_name="sage.demo.cat"
        )

        self.assertIn("def value(self) -> Any: ...", content)
        self.assertEqual(summary.classes_skipped, [])

    def test_deep_scan_rejects_wrong_module_collision(self) -> None:
        import sys as _sys

        from sage_pycharm_stubgen import runtime_audit as ra

        impostor = type("Shared", (), {"__module__": "sage.demo.other"})
        holder = types.ModuleType("sage.demo.holder2")
        holder.Shared = impostor
        _sys.modules["sage.demo.holder2"] = holder
        ra._deep_class_index = None
        try:
            stub = "class Shared:\n    def known(self): ...\n"
            content, summary = audit_stub_text(
                stub, types.SimpleNamespace(), module_name="sage.demo.mod"
            )
        finally:
            _sys.modules.pop("sage.demo.holder2", None)
            ra._deep_class_index = None

        self.assertEqual(content, stub)
        self.assertEqual(summary.classes_skipped, ["Shared"])
        self.assertEqual(summary.classes_resolved_deep, 0)

    def test_nested_class_resolves_within_same_module_only(self) -> None:
        import sys as _sys

        from sage_pycharm_stubgen import runtime_audit as ra

        # Owner defined in the stub's own module: its nested Element resolves.
        def value(self):
            return 1

        Nested = type("Element", (), {"value": value, "__module__": "sage.demo.mod"})
        runtime_module = types.ModuleType("sage.demo.mod")
        Owner = type("Owner", (), {"Element": Nested, "__module__": "sage.demo.mod"})
        runtime_module.Owner = Owner
        # An imported owner from ANOTHER module must not satisfy the lookup:
        # its nested Element documents a foreign type.
        class ForeignOwner:
            pass

        ForeignOwner.__module__ = "sage.demo.other"
        ForeignElement = type("Element", (), {"__module__": "sage.demo.other"})
        runtime_module.ForeignOwner = ForeignOwner

        stub = "class Element:\n    def known(self): ...\n"
        content, summary = audit_stub_text(
            stub, runtime_module, module_name="sage.demo.mod"
        )

        self.assertIn("def value(self) -> Any: ...", content)
        self.assertEqual(summary.classes_skipped, [])

    def test_inline_class_expanded_and_audited_when_runtime_members_exist(self) -> None:
        class Inline:
            def value(self):
                return 1

        stub = "class Inline: ...\n\nclass Other:\n    def known(self): ...\n"
        content, summary = self._audit(stub, {"Inline": Inline, "Other": int})

        # Inline expanded into a real body with the audited member; Other
        # (runtime int) gains nothing addable beyond what a stub declares, so
        # it is simply skipped as before.
        self.assertIn("class Inline:\n    pass\n", content)
        self.assertIn("def value(self) -> Any: ...", content)
        self.assertEqual(summary.classes_expanded, 1)
        self.assertNotIn("Inline", summary.classes_skipped)
        compile(content, "sample.pyi", "exec")

    def test_main_module_is_never_imported(self) -> None:
        # sage.cli.__main__ executes its CLI at import time (an interactive
        # REPL under a sanitized argv); stubs for __main__ modules are static
        # documentation and must be skipped, even when the class would match.
        imported: list[str] = []

        def importer(name: str):
            imported.append(name)
            raise ImportError(name)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_dir = root / "sage" / "cli"
            cli_dir.mkdir(parents=True)
            (cli_dir / "__main__.pyi").write_text(
                "class Main:\n    def known(self): ...\n", encoding="utf-8"
            )
            summary = audit_stub_tree(root, importer=importer)

        self.assertEqual(imported, [])
        self.assertEqual(summary.methods_added, 0)

    def test_importers_see_sanitized_argv(self) -> None:
        import sys as _sys

        class Demo:
            def value(self):
                return 1

        seen: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_dir = root / "sage" / "demo"
            module_dir.mkdir(parents=True)
            (module_dir / "mod.pyi").write_text(
                "class Demo:\n    def known(self): ...\n", encoding="utf-8"
            )

            def importer(name: str):
                seen.append(list(_sys.argv))
                return _runtime(Demo=Demo)

            saved = list(_sys.argv)
            _sys.argv = [saved[0], "--install", "--runtime-audit"]
            try:
                summary = audit_stub_tree(root, importer=importer)
            finally:
                _sys.argv = saved

        self.assertEqual(summary.files_touched, 1)
        self.assertEqual(seen, [[saved[0]]])


if __name__ == "__main__":
    unittest.main()
