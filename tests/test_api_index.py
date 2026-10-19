import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.api_index.generate import (
    AstExtractor,
    SourceRef,
    RawSymbol,
    SourceSpec,
    normalize,
)

SOURCE = SourceRef("STUB", "runtime-export/sage/all.pyi", "0" * 64)


def _symbol(qualified_name, kind, **kwargs):
    return RawSymbol(qualified_name, kind, SOURCE, **kwargs)


class AliasValueTypePropagationTests(unittest.TestCase):
    def test_alias_inherits_target_value_type(self) -> None:
        # all.pyi `from sage.rings.integer_ring import ZZ as ZZ` produces an
        # ALIAS entry; the home module's annotated singleton carries the
        # declared class.  The plugin types the namespace constant through
        # valueType, so the alias entry must inherit it.
        raw = [
            _symbol(
                "sage.rings.integer_ring.ZZ",
                "CONSTANT",
                value_type={"state": "KNOWN", "expression": "sage.rings.integer_ring.IntegerRing_class"},
                confidence="MEDIUM",
            ),
            _symbol(
                "sage.all.ZZ",
                "ALIAS",
                aliases=["sage.rings.integer_ring.ZZ"],
                confidence="HIGH",
            ),
        ]

        index, _diagnostics = normalize(raw, "10.9", "3.13")

        entries = {entry["qualifiedName"]: entry for entry in index["entries"]}
        self.assertEqual(
            entries["sage.all.ZZ"]["valueType"],
            {"state": "KNOWN", "expression": "sage.rings.integer_ring.IntegerRing_class"},
        )
        self.assertEqual(entries["sage.all.ZZ"]["kind"], "ALIAS")

    def test_alias_without_typed_target_stays_untyped(self) -> None:
        raw = [
            _symbol("sage.rings.orphan.NAME", "CONSTANT", confidence="LOW"),
            _symbol("sage.all.NAME", "ALIAS", aliases=["sage.rings.orphan.NAME"], confidence="HIGH"),
        ]

        index, _diagnostics = normalize(raw, "10.9", "3.13")

        entries = {entry["qualifiedName"]: entry for entry in index["entries"]}
        self.assertNotIn("valueType", entries["sage.all.NAME"])

    def test_alias_own_value_type_is_not_overwritten(self) -> None:
        own = {"state": "KNOWN", "expression": "builtins.int"}
        raw = [
            _symbol(
                "sage.rings.integer_ring.ZZ",
                "CONSTANT",
                value_type={"state": "KNOWN", "expression": "sage.rings.integer_ring.IntegerRing_class"},
                confidence="MEDIUM",
            ),
            _symbol(
                "sage.all.ZZ",
                "ALIAS",
                aliases=["sage.rings.integer_ring.ZZ"],
                value_type=own,
                confidence="HIGH",
            ),
        ]

        index, _diagnostics = normalize(raw, "10.9", "3.13")

        entries = {entry["qualifiedName"]: entry for entry in index["entries"]}
        self.assertEqual(entries["sage.all.ZZ"]["valueType"], own)


class SingletonSidecarShapeTests(unittest.TestCase):
    """The sidecar data form the plugin's type provider consumes.

    REGRESSION (2026-10-19 outage): a singleton must surface as a CONSTANT
    entry carrying a valueType that names a stub-declared class — that is
    what `SageTypeProvider` lowers to the callable class instance.  If the
    all.pyi declaration stops being an annotated attribute (e.g. becomes a
    from-import re-export), the entry degrades to an untyped ALIAS and every
    `ZZ(35) / ZZ(3)`-style chain loses its type in PyCharm.
    """

    def test_singleton_attribute_yields_constant_with_declared_value_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sage_dir = root / "sage"
            sage_dir.mkdir()
            (sage_dir / "all.pyi").write_text(
                "from sage.rings.integer_ring import IntegerRing_class as _Type_ZZ\n"
                "\n"
                "ZZ: _Type_ZZ\n"
                "\n"
                "__all__: list[str] = ['ZZ']\n",
                encoding="utf-8",
            )
            spec = SourceSpec(root, "STUB", "runtime-export", "")
            raw = AstExtractor(spec).extract_path(sage_dir / "all.pyi")

        index, _diagnostics = normalize(raw, "10.9", "3.13")
        entries = {entry["qualifiedName"]: entry for entry in index["entries"]}
        entry = entries["sage.all.ZZ"]
        self.assertEqual(entry["kind"], "CONSTANT")
        self.assertEqual(
            entry["valueType"],
            {"state": "KNOWN", "expression": "sage.rings.integer_ring.IntegerRing_class"},
        )

    def test_import_reexport_singleton_degrades_to_untyped_alias(self) -> None:
        # Documents the failure mode the attribute form protects against: the
        # re-export shape produces an ALIAS whose own valueType is empty (only
        # the normalize() propagation may fill it from the target entry).
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sage_dir = root / "sage"
            sage_dir.mkdir()
            (sage_dir / "all.pyi").write_text(
                "from sage.rings.integer_ring import ZZ as ZZ\n",
                encoding="utf-8",
            )
            spec = SourceSpec(root, "STUB", "runtime-export", "")
            raw = AstExtractor(spec).extract_path(sage_dir / "all.pyi")

        index, _diagnostics = normalize(raw, "10.9", "3.13")
        entries = {entry["qualifiedName"]: entry for entry in index["entries"]}
        self.assertEqual(entries["sage.all.ZZ"]["kind"], "ALIAS")
        self.assertNotIn("valueType", entries["sage.all.ZZ"])


if __name__ == "__main__":
    unittest.main()
