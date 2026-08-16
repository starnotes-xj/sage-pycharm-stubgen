import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage_pycharm_stubgen.translate import (
    TranslationCache,
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
