import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sage_pycharm_stubgen import docs_layer
from sage_pycharm_stubgen.docs_layer import (
    FORMAT_VERSION,
    get_doc,
    load_docs,
    reload,
    remove_doc,
    save_docs,
    set_doc,
    stats,
    validate,
)


class DocsLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        # Every test works on a scratch copy; the shipped file never moves.
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "docs_zh.json"
        self.data.write_text(
            json.dumps(
                {
                    "version": FORMAT_VERSION,
                    "docs": {
                        "sage.demo.mod": {
                            "alpha": "第一段文档",
                            "beta": "第二段文档",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        patcher = mock.patch.object(docs_layer, "DATA_FILE", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        reload()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_reads_and_caches(self) -> None:
        docs = load_docs()
        self.assertEqual(docs["sage.demo.mod"]["alpha"], "第一段文档")
        # Cached: an external edit stays invisible until reload().
        self.data.write_text("{}", encoding="utf-8")
        self.assertIn("sage.demo.mod", load_docs())
        reload()
        self.assertEqual(load_docs(), {})

    def test_get_doc_hit_and_miss(self) -> None:
        self.assertEqual(get_doc("sage.demo.mod", "beta"), "第二段文档")
        self.assertIsNone(get_doc("sage.demo.mod", "missing"))
        self.assertIsNone(get_doc("sage.absent.mod", "alpha"))

    def test_set_and_remove_roundtrip(self) -> None:
        set_doc("sage.demo.mod", "gamma", "新文档")
        self.assertEqual(get_doc("sage.demo.mod", "gamma"), "新文档")
        save_docs()
        reload()
        self.assertEqual(get_doc("sage.demo.mod", "gamma"), "新文档")
        self.assertTrue(remove_doc("sage.demo.mod", "gamma"))
        self.assertIsNone(get_doc("sage.demo.mod", "gamma"))
        self.assertFalse(remove_doc("sage.demo.mod", "gamma"))

    def test_save_is_atomic_and_sorted(self) -> None:
        set_doc("sage.demo.other", "zeta", "z 文档")
        set_doc("sage.demo.other", "eta", "e 文档")
        written = save_docs()
        self.assertEqual(written, self.data)
        payload = json.loads(self.data.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], FORMAT_VERSION)
        self.assertEqual(
            list(payload["docs"]["sage.demo.other"]), ["eta", "zeta"]
        )
        # No temp residue beside the data file.
        self.assertEqual(list(self.data.parent.glob("*.tmp")), [])

    def test_malformed_file_fails_open(self) -> None:
        self.data.write_text("{not json", encoding="utf-8")
        self.assertEqual(reload(), {})
        self.assertTrue(validate()[0].startswith("unreadable data file"))

    def test_unknown_version_fails_open(self) -> None:
        self.data.write_text(
            json.dumps({"version": 99, "docs": {"m": {"s": "d"}}}),
            encoding="utf-8",
        )
        self.assertEqual(reload(), {})

    def test_validate_reports_structural_problems(self) -> None:
        self.data.write_text(
            json.dumps(
                {"version": FORMAT_VERSION, "docs": {"m": {"s": "", "": "x", "t": 5}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        problems = validate()
        self.assertTrue(any("m.s" in p for p in problems))
        self.assertTrue(any("non-string symbol" in p for p in problems))
        self.assertTrue(any("m.t" in p for p in problems))

    def test_stats_counts_layers(self) -> None:
        numbers = stats()
        self.assertEqual(numbers["modules"], 1)
        self.assertEqual(numbers["symbols"], 2)
        self.assertEqual(numbers["characters"], len("第一段文档") + len("第二段文档"))
        self.assertEqual(numbers["per_module"], {"sage.demo.mod": 2})


class SupplementalDocsCompatTests(unittest.TestCase):
    def test_merged_view_exposes_both_layers(self) -> None:
        from sage_pycharm_stubgen.supplemental_docs import SUPPLEMENTAL_DOCS

        reload()  # drop any scratch cache from other test classes
        docs = load_docs()
        modules_with_docs = sum(
            1
            for entries in SUPPLEMENTAL_DOCS.values()
            for entry in entries.values()
            if isinstance(entry, dict) and "doc" in entry
        )
        self.assertEqual(modules_with_docs, sum(len(v) for v in docs.values()))
        # Type knowledge still travels through the merged view.
        returns = sum(
            1
            for entries in SUPPLEMENTAL_DOCS.values()
            for entry in entries.values()
            if isinstance(entry, dict) and entry.get("return")
        )
        self.assertGreater(returns, 500)
        # The merged view is a copy: mutating it never corrupts the layers.
        module = next(iter(docs))
        symbol = next(iter(docs[module]))
        SUPPLEMENTAL_DOCS[module][symbol]["doc"] = "MUTATED"
        self.assertNotEqual(get_doc(module, symbol), "MUTATED")


if __name__ == "__main__":
    unittest.main()
