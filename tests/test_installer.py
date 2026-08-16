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
