import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.installer import (
    CURATED_DOCS_NAME,
    MANIFEST_NAME,
    _current_version,
    _version_tuple,
    install_stub_package,
    uninstall_stub_package,
)


def _make_fixtures() -> tuple[Path, Path, Path]:
    tmp = Path(tempfile.mkdtemp())
    output_root = tmp / "sage_typings"
    source = output_root / "sage"
    (source / "rings").mkdir(parents=True)
    (source / "rings" / "sample.pyi").write_text(
        "class Sample: ...\n", encoding="utf-8"
    )
    sage_package = tmp / "site_packages" / "sage"
    sage_package.mkdir(parents=True)
    (sage_package / "all.py").write_text("", encoding="utf-8")
    return tmp, output_root, sage_package


class InstallerTests(unittest.TestCase):
    def test_install_records_version_and_ships_curated_docs(self) -> None:
        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        result = install_stub_package(output_root, sage_package, "10.9")

        manifest = json.loads(
            (sage_package / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["generator_version"], _current_version())
        self.assertIn("rings/sample.pyi", manifest["files"])

        curated = sage_package / CURATED_DOCS_NAME
        self.assertTrue(curated.is_file())
        curated_data = json.loads(curated.read_text(encoding="utf-8"))
        self.assertIsInstance(curated_data, dict)
        self.assertGreater(len(curated_data), 0)

        uninstall = uninstall_stub_package(sage_package)
        self.assertGreaterEqual(uninstall.removed_files, 3)  # pyi + marker + curated
        self.assertFalse(curated.exists())
        self.assertFalse((sage_package / MANIFEST_NAME).exists())

    def test_downgrade_install_is_refused_without_force(self) -> None:
        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (sage_package / MANIFEST_NAME).write_text(
            json.dumps({"generator_version": "99.0.0", "files": []}),
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError) as ctx:
            install_stub_package(output_root, sage_package, "10.9")
        self.assertIn("newer version", str(ctx.exception))

        # force bypasses the guard
        result = install_stub_package(
            output_root, sage_package, "10.9", force=True
        )
        self.assertGreater(result.installed_files, 0)

    def test_shrunken_tree_install_is_refused_without_force(self) -> None:
        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rings = output_root / "sage" / "rings"
        for index in range(10):
            (rings / f"full{index}.pyi").write_text(
                "class Full: ...\n", encoding="utf-8"
            )
        install_stub_package(output_root, sage_package, "10.9")

        # A later run that lost most of the previously owned stubs (e.g. a
        # py-only regen after a full pyx+py run) must not silently amputate
        # the installed tree.
        for pyi in rings.glob("full*.pyi"):
            pyi.unlink()
        for index in range(3):
            (rings / f"kept{index}.pyi").write_text(
                "class Kept: ...\n", encoding="utf-8"
            )
        with self.assertRaises(RuntimeError) as ctx:
            install_stub_package(output_root, sage_package, "10.9")
        self.assertIn("missing", str(ctx.exception))
        self.assertTrue((sage_package / "rings" / "full0.pyi").exists())

        result = install_stub_package(
            output_root, sage_package, "10.9", force=True
        )
        self.assertGreater(result.installed_files, 0)
        self.assertFalse((sage_package / "rings" / "full0.pyi").exists())

    def test_small_tree_growth_is_allowed(self) -> None:
        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (output_root / "sage" / "extra.pyi").write_text(
            "class Extra: ...\n", encoding="utf-8"
        )
        install_stub_package(output_root, sage_package, "10.9")

        (output_root / "sage" / "extra.pyi").unlink()
        result = install_stub_package(output_root, sage_package, "10.9")
        self.assertGreater(result.installed_files, 0)
        self.assertFalse((sage_package / "extra.pyi").exists())

    def test_legacy_manifest_without_version_is_upgraded(self) -> None:
        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (sage_package / MANIFEST_NAME).write_text(
            json.dumps({"files": []}), encoding="utf-8"
        )
        result = install_stub_package(output_root, sage_package, "10.9")
        self.assertGreater(result.installed_files, 0)

    def test_version_tuple_ordering(self) -> None:
        self.assertLess(_version_tuple("0.6.1"), _version_tuple("0.7.0"))
        self.assertLess(_version_tuple("0.0.0"), _version_tuple("0.7.0"))
        self.assertEqual(_version_tuple("0.7.0"), _version_tuple("0.7.0"))
        self.assertLess(_version_tuple("0.7.0"), _version_tuple("99.0.0"))

    def test_manifest_records_hashes_and_verify_passes(self) -> None:
        from sage_pycharm_stubgen.installer import verify_stub_package

        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        install_stub_package(output_root, sage_package, "10.9")

        manifest = json.loads(
            (sage_package / MANIFEST_NAME).read_text(encoding="utf-8")
        )
        self.assertIn("rings/sample.pyi", manifest["file_hashes"])
        self.assertEqual(len(manifest["file_hashes"]["rings/sample.pyi"]), 64)

        result = verify_stub_package(sage_package)
        self.assertTrue(result.clean)
        self.assertGreaterEqual(result.checked_files, 1)

    def test_verify_detects_modified_missing_and_unowned(self) -> None:
        from sage_pycharm_stubgen.installer import verify_stub_package

        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        install_stub_package(output_root, sage_package, "10.9")

        sample = sage_package / "rings" / "sample.pyi"
        sample.write_text("class Sample: ...\n# tampered\n", encoding="utf-8")
        (sage_package / "foreign.pyi").write_text(
            "class Foreign: ...\n", encoding="utf-8"
        )
        result = verify_stub_package(sage_package)
        self.assertIn("rings/sample.pyi", result.modified)
        self.assertIn("foreign.pyi", result.unowned)
        self.assertFalse(result.clean)

        sample.unlink()
        result = verify_stub_package(sage_package)
        self.assertIn("rings/sample.pyi", result.missing)

    def test_verify_requires_manifest(self) -> None:
        from sage_pycharm_stubgen.installer import verify_stub_package

        tmp, output_root, sage_package = _make_fixtures()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with self.assertRaises(RuntimeError):
            verify_stub_package(sage_package)

    def test_package_version_matches_pyproject(self) -> None:
        import tomllib

        from sage_pycharm_stubgen import __version__

        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        self.assertEqual(__version__, project["project"]["version"])
