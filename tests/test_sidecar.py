import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.sidecar import (
    build_sidecar_files,
    install_sidecar,
    sidecar_target_dirs,
)

STUB_TREE = {
    "sage/__init__.pyi": "",
    "sage/demo/__init__.pyi": "",
    "sage/demo/point.pyi": (
        "class Point:\n"
        "    def __rmul__(self, n) -> Point: ...\n"
        "    def xy(self) -> tuple: ...\n"
    ),
}


class SidecarTests(unittest.TestCase):
    def test_trio_satisfies_plugin_validation(self) -> None:
        """Mirror the plugin's SageApiIndexService sidecar checks exactly."""
        with tempfile.TemporaryDirectory() as temp:
            stub_root = Path(temp)
            for name, text in STUB_TREE.items():
                path = stub_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            files = build_sidecar_files(stub_root, "10.9", "3.13")

        self.assertEqual(sorted(files), sorted(
            ("sage-api-index.json", "sage-api-index-envelope.json", "artifact-receipt.json")
        ))

        index = json.loads(files["sage-api-index.json"])
        entries = index["entries"]
        entry_count = len(entries)

        # plugin: envelope JSON with an "index" object
        envelope = json.loads(files["sage-api-index-envelope.json"])
        self.assertEqual(envelope["schemaVersion"], 1)
        self.assertEqual(envelope["index"]["path"], "sage-api-index.json")
        # plugin: index sha256 must match the SHA256 regex AND the actual file
        self.assertEqual(
            envelope["index"]["sha256"],
            hashlib.sha256(files["sage-api-index.json"].encode("utf-8")).hexdigest(),
        )
        # plugin: FULL complete coverage with counts matching the entry count
        coverage = envelope["apiCoverage"]
        self.assertEqual(coverage["scope"], "FULL")
        self.assertTrue(coverage["isComplete"])
        self.assertEqual(coverage["coveredCount"], entry_count)
        self.assertEqual(coverage["expectedCount"], entry_count)
        self.assertTrue(coverage["identityDigest"])
        # plugin: non-blank artifactId
        self.assertTrue(envelope["artifactId"])

        # plugin: receipt artifactId must equal the envelope's, and the
        # receipt's index entry must name the product index file
        receipt = json.loads(files["artifact-receipt.json"])
        self.assertEqual(receipt["artifactId"], envelope["artifactId"])
        self.assertEqual(receipt["index"]["path"], "sage-api-index.json")
        self.assertEqual(receipt["index"]["entryCount"], entry_count)

        # the acceptance-critical member made it into the index
        qualified = [e["qualifiedName"] for e in entries]
        self.assertIn("sage.demo.point.Point.__rmul__", qualified)

    def test_install_sidecar_writes_versioned_directory(self) -> None:
        files = {
            "sage-api-index.json": "{}\n",
            "sage-api-index-envelope.json": "{}\n",
            "artifact-receipt.json": "{}\n",
        }
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            written = install_sidecar(files, "10.9", [base / "PyCharm2026.2"])

            destination = base / "PyCharm2026.2" / "sage-api" / "10.9"
            self.assertEqual(written, [destination])
            for name in files:
                self.assertTrue((destination / name).is_file())

    def test_sidecar_target_dirs_prefers_explicit(self) -> None:
        self.assertEqual(
            sidecar_target_dirs("some/dir"),
            [Path("some/dir")],
        )


if __name__ == "__main__":
    unittest.main()
