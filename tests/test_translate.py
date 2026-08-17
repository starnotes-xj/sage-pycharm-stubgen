import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage_pycharm_stubgen.translate import (
    TranslationCache,
    _group_entries_by_marker,
    _request_baidu_batch,
    _restore_code_blocks,
    apply_translations,
    iter_english_docstrings,
    translate_texts,
)


class TranslateTextsTests(unittest.TestCase):
    def test_batch_translation_collects_results(self) -> None:
        def fake_batch(texts):
            return {t: f"中文:{t[:12]}" for t in texts}

        with patch("sage_pycharm_stubgen.translate._request_google_batch", side_effect=fake_batch):
            translated, failed = translate_texts(["doc one", "doc two", "doc three"], pause=0)
        self.assertEqual(failed, 0)
        self.assertEqual(len(translated), 3)
        self.assertTrue(all("中文" in v for v in translated.values()))

    def test_failed_batch_falls_back_to_singles(self) -> None:
        calls = []

        def flaky(texts):
            calls.append(len(texts))
            if len(texts) > 1:
                return {}
            return {texts[0]: "译文"}

        with patch("sage_pycharm_stubgen.translate._request_google_batch", side_effect=flaky):
            translated, failed = translate_texts(["a", "b"], batch_size=2, pause=0)
        self.assertEqual(translated, {"a": "译文", "b": "译文"})
        self.assertEqual(failed, 0)
        self.assertIn(2, calls)  # a batch was attempted

    def test_network_errors_count_as_failed(self) -> None:
        def broken(texts):
            raise OSError("offline")

        with patch("sage_pycharm_stubgen.translate._request_google_batch", side_effect=broken):
            translated, failed = translate_texts(["a"], attempts=2, pause=0)
        self.assertEqual(translated, {})
        self.assertEqual(failed, 1)

    def test_baidu_backend_requires_credentials(self) -> None:
        with self.assertRaises(ValueError):
            translate_texts(["a"], backend="baidu", pause=0)

    def test_baidu_backend_translates_one_request_per_text(self) -> None:
        def fake_baidu(texts, appid, **kwargs):
            return {t: f"中文:{t}" for t in texts}

        with patch("sage_pycharm_stubgen.translate._request_baidu_batch", side_effect=fake_baidu):
            translated, failed = translate_texts(
                ["a", "b"], backend="baidu", appid="id", api_key="k", pause=0
            )
        self.assertEqual(failed, 0)
        self.assertEqual(set(translated), {"a", "b"})

    def test_group_entries_by_marker_splits_groups(self) -> None:
        entries = [
            ("line 1", "译文一A"),
            ("line 2", "译文一B"),
            ("QXZ73M", "QXZ73M"),
            ("other line", "译文二"),
        ]
        self.assertEqual(
            _group_entries_by_marker(entries),
            ["译文一A\n译文一B", "译文二"],
        )

    def test_restore_code_blocks_keeps_doctest_lines(self) -> None:
        src = (
            "An element in a Clifford algebra.\n"
            "\n"
            "TESTS::\n"
            "\n"
            "    sage: Q = QuadraticForm(ZZ, 3, [1, 2, 3])\n"
            "    sage: TestSuite(elt).run()\n"
        )
        dst = (
            "克利福德代数中的一个元素。\n"
            "\n"
            "测试::\n"
            "\n"
            "    圣人： Q = QuadraticForm(ZZ, 3, [1, 2, 3])\n"
            "    鼠尾草：TestSuite(elt).run()\n"
        )
        restored = _restore_code_blocks(src, dst)
        self.assertIn("TESTS::", restored)
        self.assertIn("sage: Q = QuadraticForm", restored)
        self.assertIn("sage: TestSuite", restored)
        self.assertNotIn("圣人", restored)
        self.assertIn("克利福德代数中的一个元素。", restored)

    def test_restore_code_blocks_keeps_latex_lines(self) -> None:
        src = "Math display:\n\nx_1 \\wedge x_2 \\mapsto y\n"
        dst = "数学显示：\n\nx_1 \\楔形 x_2 \\映射到 y\n"
        restored = _restore_code_blocks(src, dst)
        self.assertIn("\\wedge", restored)
        self.assertNotIn("\\楔形", restored)
        self.assertIn("数学显示：", restored)

    def test_restore_code_blocks_mismatch_keeps_translation(self) -> None:
        self.assertEqual(_restore_code_blocks("A\nB", "甲"), "甲")

    def test_restore_code_blocks_unaligned_restores_block_by_position(self) -> None:
        src = (
            "Summary.\n"
            "\n"
            "TESTS::\n"
            "\n"
            "    sage: Q = QuadraticForm(ZZ, 3, [1, 2, 3])\n"
            "    sage: TestSuite(elt).run()\n"
        )
        dst = (
            "摘要。\n"
            "\n"
            "测试::\n"
            "\n"
            "    圣人： Q = QuadraticForm(ZZ, 3, [1, 2, 3])\n"
            "    鼠尾草：TestSuite(elt).run()\n"
            "摘要。"  # the model dropped a blank line — counts differ
        )
        restored = _restore_code_blocks(src, dst)
        self.assertIn("TESTS::", restored)
        self.assertIn("sage: Q = QuadraticForm", restored)
        self.assertIn("sage: TestSuite(elt).run()", restored)
        self.assertNotIn("圣人", restored)
        self.assertNotIn("鼠尾草", restored)
        self.assertIn("摘要。", restored)

    def test_baidu_batch_marker_grouping_maps_pack(self) -> None:
        entries = [
            ("first", "译文一"),
            ("QXZ73M", "QXZ73M"),
            ("second", "译文二"),
        ]

        def fake_entries(text, appid, **kwargs):
            return entries

        with patch(
            "sage_pycharm_stubgen.translate._baidu_translate_entries",
            side_effect=fake_entries,
        ):
            result = _request_baidu_batch(
                ["doc one", "doc two"], "id", api_key="k", pause=0, workers=2
            )
        self.assertEqual(result, {"doc one": "译文一", "doc two": "译文二"})

    def test_baidu_batch_marker_break_falls_back_to_singles(self) -> None:
        def fake_entries(text, appid, **kwargs):
            # The marker lines were dropped by the model.
            return [("merged", "全部混在一起")]

        def fake_segmented(text, appid, **kwargs):
            return f"单条:{text[:6]}"

        with (
            patch(
                "sage_pycharm_stubgen.translate._baidu_translate_entries",
                side_effect=fake_entries,
            ),
            patch(
                "sage_pycharm_stubgen.translate._baidu_translate_segmented",
                side_effect=fake_segmented,
            ),
        ):
            result = _request_baidu_batch(
                ["doc one", "doc two"], "id", api_key="k", pause=0
            )
        self.assertEqual(result, {"doc one": "单条:doc on", "doc two": "单条:doc tw"})


class TranslationCacheTests(unittest.TestCase):
    def test_roundtrip_and_merge_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = TranslationCache(root / "translations.json")
            cache.data["k1"] = "v1"
            cache.save()

            reloaded = TranslationCache(root / "translations.json")
            self.assertEqual(reloaded.data, {"k1": "v1"})

            bundled = root / "bundled.json"
            bundled.write_text(
                json.dumps({"translations": {"k1": "older", "k2": "v2"}}),
                encoding="utf-8",
            )
            reloaded.merge(bundled)
            self.assertEqual(reloaded.data["k1"], "v1")  # user cache wins
            self.assertEqual(reloaded.data["k2"], "v2")  # bundled fills gaps


class ApplyTranslationsTests(unittest.TestCase):
    def test_apply_rewrites_english_docstring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / "sample.pyi"
            stub.write_text(
                'class Element:\n'
                '    def inverse_mod(self, I):\n'
                '        """Return x such that self*x == 1 mod I."""\n'
                '        ...\n',
                encoding="utf-8",
            )
            cache = {
                "Return x such that self*x == 1 mod I.": "返回满足 self*x ≡ 1 (mod I) 的 x。"
            }
            applied = apply_translations(root, cache)
            self.assertEqual(applied, 1)
            content = stub.read_text(encoding="utf-8")
            self.assertIn("返回满足 self*x", content)
            self.assertNotIn("Return x such that", content)
            compile(content, "sample.pyi", "exec")

    def test_iter_english_docstrings_skips_chinese_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stub = root / "sample.pyi"
            stub.write_text(
                'def f1(): ...\n'
                'def f2():\n'
                '    """Return the value."""\n'
                '    ...\n'
                'def f3():\n'
                '    """返回中文说明。"""\n'
                '    ...\n',
                encoding="utf-8",
            )
            docs = [doc for _, doc in iter_english_docstrings(root)]
            self.assertEqual(docs, ["Return the value."])
