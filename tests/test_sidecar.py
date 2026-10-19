import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sage_pycharm_stubgen.sidecar import (
    DOCS_BUCKET_COUNT,
    DOCS_DIRECTORY,
    _java_string_hash,
    build_sidecar_files,
    capacity_warning,
    install_sidecar,
    sidecar_target_dirs,
    split_documentation,
)

STUB_TREE = {
    "sage/__init__.pyi": "",
    "sage/demo/__init__.pyi": "",
    "sage/demo/point.pyi": (
        "class Point:\n"
        '    """Point 文档。"""\n'
        "    def __rmul__(self, n) -> Point: ...\n"
        "    def xy(self) -> tuple:\n"
        '        """返回坐标二元组。"""\n'
        "        ...\n"
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

        bucket_names = [f"sage-api-docs/{i:02x}.ndjson" for i in range(64)]
        self.assertEqual(
            sorted(files),
            sorted(
                (
                    "sage-api-index.json",
                    "sage-api-index-envelope.json",
                    "artifact-receipt.json",
                )
                + tuple(bucket_names)
            ),
        )

        index = json.loads(files["sage-api-index.json"])
        entries = index["entries"]
        entry_count = len(entries)

        # Slimming contract: documentation lives only in the buckets and the
        # per-source digests collapse into the top-level table — both fields
        # are optional to the plugin's schema-1 reader, so every released
        # plugin still loads this index.
        for entry in entries:
            self.assertNotIn("documentation", entry)
            for source in entry.get("sources", []):
                self.assertNotIn("digest", source)

        # Every generated document is findable in its hash bucket under the
        # plugin library's line format (KIND␟qualname␉{summary,body,examples}).
        moved = 0
        for entry in entries:
            key = f"{entry['kind']}\x1f{entry['qualifiedName']}"
            bucket = _java_string_hash(key) & (DOCS_BUCKET_COUNT - 1)
            prefix = f"{key}\t"
            hit = [
                line
                for line in files[f"sage-api-docs/{bucket:02x}.ndjson"].splitlines()
                if line.startswith(prefix)
            ]
            if entry["qualifiedName"] == "sage.demo.point.Point.xy":
                self.assertTrue(hit, "documented stub member must have a bucket record")
                payload = json.loads(hit[0][len(prefix):])
                self.assertEqual(sorted(payload), ["body", "examples", "summary"])
                moved += 1
        self.assertGreaterEqual(moved, 1)

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

        # documentation sidecar bookkeeping: envelope advertises the directory,
        # the receipt digests every bucket (including the empty ones), and the
        # recorded entry count equals the lines actually written
        bucket_lines = sum(
            len([line for line in text.splitlines() if line])
            for text in (files[name] for name in bucket_names)
        )
        self.assertEqual(envelope["documentationSidecar"]["directory"], DOCS_DIRECTORY)
        self.assertEqual(receipt["documentation"]["entries"], bucket_lines)
        self.assertEqual(sorted(receipt["documentation"]["sha256"]), sorted(bucket_names))
        for name in bucket_names:
            self.assertEqual(
                receipt["documentation"]["sha256"][name],
                hashlib.sha256(files[name].encode("utf-8")).hexdigest(),
            )

        # the acceptance-critical member made it into the index, under the
        # sage.-prefixed qualified name the plugin's owner-keyed member
        # lookup requires: entry.ownerName is derived by dropping the last
        # segment, so the prefix IS the owner key (module-prefix regression
        # guard)
        qualified = [e["qualifiedName"] for e in entries]
        self.assertIn("sage.demo.point.Point.__rmul__", qualified)
        self.assertIn("sage.demo.point.Point.xy", qualified)

    def test_install_sidecar_writes_versioned_directory(self) -> None:
        files = {
            "sage-api-index.json": "{}\n",
            "sage-api-index-envelope.json": "{}\n",
            "artifact-receipt.json": "{}\n",
            f"{DOCS_DIRECTORY}/2a.ndjson": "METHOD\x1fsage.demo.Point.xy\t{}\n",
        }
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            written = install_sidecar(files, "10.9", [base / "PyCharm2026.2"])

            destination = base / "PyCharm2026.2" / "sage-api" / "10.9"
            self.assertEqual(written, [destination])
            for name in files:
                self.assertTrue((destination / name).is_file())


class DocumentationSplitTests(unittest.TestCase):
    def test_java_string_hash_matches_jvm_semantics(self) -> None:
        # Hand-computed Java String.hashCode() vectors.
        self.assertEqual(_java_string_hash(""), 0)
        self.assertEqual(_java_string_hash("a"), 97)
        self.assertEqual(_java_string_hash("ab"), 31 * 97 + 98)
        # 32-bit wraparound: a long ASCII string must fold, not overflow.
        folded = 0
        for character in "sage.demo.point.Point.xy" * 40:
            folded = (folded * 31 + ord(character)) & 0xFFFFFFFF
        self.assertEqual(_java_string_hash("sage.demo.point.Point.xy" * 40), folded)
        # Bucket bits are the same under the signed two's-complement view
        # (Kotlin Int.and on a negative hashCode).
        unsigned = _java_string_hash("METHOD\x1fsage.all.QQ")
        signed = unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned
        self.assertEqual(unsigned & 63, signed & 63)

    def test_split_moves_docs_and_digests(self) -> None:
        index = {
            "entries": [
                {
                    "qualifiedName": "sage.demo.Point.xy",
                    "kind": "METHOD",
                    "documentation": {
                        "summary": "返回坐标。",
                        "body": "完整说明\n多行",
                        "examples": ["Point(1, 2).xy()"],
                    },
                    "sources": [
                        {"kind": "STUB", "locator": "runtime-export/sage/demo/point.pyi", "digest": "0" * 64}
                    ],
                },
                {
                    "qualifiedName": "sage.demo.Point",
                    "kind": "CLASS",
                    "sources": [{"kind": "STUB", "locator": "runtime-export/sage/demo/point.pyi", "digest": "1" * 64}],
                },
            ]
        }
        moved, files = split_documentation(index)

        self.assertEqual(moved, 1)
        self.assertNotIn("documentation", index["entries"][0])
        self.assertNotIn("digest", index["entries"][0]["sources"][0])
        self.assertEqual(index["entries"][0]["sources"][0]["locator"], "runtime-export/sage/demo/point.pyi")
        self.assertEqual(len(files), 64)

        key = "METHOD\x1fsage.demo.Point.xy"
        bucket = _java_string_hash(key) & (DOCS_BUCKET_COUNT - 1)
        bucket_text = files[f"{DOCS_DIRECTORY}/{bucket:02x}.ndjson"]
        self.assertEqual(bucket_text.count("\n"), 1)
        prefix = key + "\t"
        self.assertTrue(bucket_text.startswith(prefix))
        # The payload never contains raw newlines or tabs: one scan line per
        # record is the reader's core assumption.
        payload = bucket_text[len(prefix):].rstrip("\n")
        self.assertNotIn("\n", payload)
        self.assertNotIn("\t", payload)
        self.assertEqual(
            json.loads(payload),
            {"summary": "返回坐标。", "body": "完整说明\n多行", "examples": ["Point(1, 2).xy()"]},
        )
        # Empty buckets are present so reinstalls self-clean stale files.
        empty = [name for name, text in files.items() if not text]
        self.assertEqual(len(empty), 63)

    def test_sidecar_target_dirs_prefers_explicit(self) -> None:
        self.assertEqual(
            sidecar_target_dirs("some/dir"),
            [Path("some/dir")],
        )


class CapacityWarningTests(unittest.TestCase):
    def test_below_threshold_is_silent(self) -> None:
        # The installed 10.9 corpus (~175k entries of the 250k cap, 70%)
        # must not warn.
        self.assertIsNone(capacity_warning(174_859, 154 * 1024 * 1024))

    def test_entry_ratio_above_threshold_warns(self) -> None:
        warning = capacity_warning(220_000, 100 * 1024 * 1024)
        self.assertIsNotNone(warning)
        self.assertIn("88%", warning)
        self.assertIn("sanitize_release_index.py", warning)

    def test_byte_ratio_alone_triggers_the_warning(self) -> None:
        warning = capacity_warning(100_000, int(0.9 * 288 * 1024 * 1024))
        self.assertIsNotNone(warning)
        self.assertIn("bytes", warning)


class InstallEpochTests(unittest.TestCase):
    def test_epoch_payload_carries_stub_root_and_version(self) -> None:
        from sage_pycharm_stubgen.sidecar import build_install_epoch

        with tempfile.TemporaryDirectory() as temp:
            text = build_install_epoch(Path(temp) / "sage", "10.9", "abc123")
        payload = json.loads(text)
        self.assertEqual(payload["sageVersion"], "10.9")
        self.assertEqual(payload["stubgenCommit"], "abc123")
        self.assertEqual(payload["stubgenVersion"], "0.9.20")
        self.assertIn("stubRoot", payload)
        self.assertGreater(payload["epoch"], 1_700_000_000_000_000_000)

    def test_vfs_url_mapping_for_wsl_and_drive_mounts(self) -> None:
        from sage_pycharm_stubgen import sidecar
        from sage_pycharm_stubgen.sidecar import _stub_root_vfs_url

        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            self.assertEqual(
                _stub_root_vfs_url(Path("/home/starnotes/envs/sage/lib/site-packages/sage")),
                "file:////wsl.localhost/Ubuntu/home/starnotes/envs/sage/lib/site-packages/sage",
            )
            self.assertEqual(
                _stub_root_vfs_url(Path("/mnt/c/Users/x/proj")),
                "file:///C:/Users/x/proj",
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_stub_root_vfs_url(Path("/home/u/sage")))


if __name__ == "__main__":
    unittest.main()
