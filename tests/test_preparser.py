from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from sage_pycharm_stubgen.cli import build_parser, main
from sage_pycharm_stubgen.preparser import (
    BACKUP_SUFFIX,
    CONVERSION_MARKER,
    SAGE_ALL_IMPORT,
    _ensure_sage_import,
    _load_preparse_file,
    preparse_path,
    preparse_source,
)


def _fake_preparse(contents: str) -> str:
    return contents.replace(
        "R.<x> = GF(2)[]",
        "R = GF(Integer(2))['x']; (x,) = R._first_ngens(1)",
    )


def _fake_module(name: str, attrs: dict[str, object] | None = None) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in (attrs or {}).items():
        setattr(module, key, value)
    return module


def _converted_expected(body: str) -> str:
    return CONVERSION_MARKER + "\n" + SAGE_ALL_IMPORT + body


class PreparsePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_in_place_rewrite_creates_backup(self) -> None:
        source = self.root / "test.py"
        source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=_fake_preparse,
        ):
            result = preparse_path(source)

        self.assertTrue(result.changed)
        self.assertEqual(result.backup, source.with_name("test.py" + BACKUP_SUFFIX))
        self.assertEqual(
            source.read_text(encoding="utf-8"),
            _converted_expected("R = GF(Integer(2))['x']; (x,) = R._first_ngens(1)\n"),
        )
        self.assertEqual(
            result.backup.read_text(encoding="utf-8"), "R.<x> = GF(2)[]\n"
        )

    def test_second_run_reports_clean(self) -> None:
        source = self.root / "test.py"
        source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=_fake_preparse,
        ):
            first = preparse_path(source)
            second = preparse_path(source)

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)

    def test_force_bypasses_marker(self) -> None:
        source = self.root / "test.py"
        original = CONVERSION_MARKER + "\nanswer = 42\n"
        source.write_text(original, encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=lambda contents: contents,
        ):
            result = preparse_path(source, force=True)

        self.assertFalse(result.changed)
        self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_clean_file_is_left_untouched(self) -> None:
        source = self.root / "clean.py"
        source.write_text("answer = 42\n", encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=lambda contents: contents,
        ):
            result = preparse_path(source)

        self.assertFalse(result.changed)
        self.assertIsNone(result.backup)
        self.assertFalse(source.with_name("clean.py" + BACKUP_SUFFIX).exists())

    def test_existing_backup_keeps_the_first_original(self) -> None:
        source = self.root / "test.py"
        source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=_fake_preparse,
        ):
            preparse_path(source)
            source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")
            preparse_path(source)

        backup = source.with_name("test.py" + BACKUP_SUFFIX)
        self.assertEqual(backup.read_text(encoding="utf-8"), "R.<x> = GF(2)[]\n")

    def test_output_dir_leaves_original_untouched(self) -> None:
        source = self.root / "test.py"
        source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")
        output_dir = self.root / "converted"

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=_fake_preparse,
        ):
            result = preparse_path(source, output_dir=output_dir)

        self.assertTrue(result.changed)
        self.assertEqual(source.read_text(encoding="utf-8"), "R.<x> = GF(2)[]\n")
        self.assertEqual(
            (output_dir / "test.py").read_text(encoding="utf-8"),
            _converted_expected("R = GF(Integer(2))['x']; (x,) = R._first_ngens(1)\n"),
        )

    def test_output_dir_same_as_source_is_refused(self) -> None:
        source = self.root / "test.py"
        original = "R.<x> = GF(2)[]\n"
        source.write_text(original, encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=_fake_preparse,
        ):
            result = preparse_path(source, output_dir=source.parent)

        self.assertFalse(result.changed)
        self.assertIsNotNone(result.error)
        self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_check_only_writes_nothing(self) -> None:
        source = self.root / "test.py"
        source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=_fake_preparse,
        ):
            result = preparse_path(source, check_only=True)

        self.assertTrue(result.changed)
        self.assertEqual(source.read_text(encoding="utf-8"), "R.<x> = GF(2)[]\n")

    def test_missing_file_reports_error(self) -> None:
        result = preparse_path(self.root / "missing.py")

        self.assertFalse(result.changed)
        self.assertIsNotNone(result.error)

    def test_missing_sage_reports_error(self) -> None:
        source = self.root / "test.py"
        original = "R.<x> = GF(2)[]\n"
        source.write_text(original, encoding="utf-8")

        def missing_preparse(contents: str) -> str:
            raise ImportError("No module named 'sage'")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=missing_preparse,
        ):
            result = preparse_path(source)

        self.assertFalse(result.changed)
        self.assertIn("sage", result.error or "")
        self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_preparser_failure_reports_error_without_touching_file(self) -> None:
        source = self.root / "broken.py"
        source.write_text("def broken( -> int: ...\n", encoding="utf-8")

        def failing_preparse(contents: str) -> str:
            raise SyntaxError("invalid syntax")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=failing_preparse,
        ):
            result = preparse_path(source)

        self.assertFalse(result.changed)
        self.assertIn("invalid syntax", result.error or "")
        self.assertEqual(
            source.read_text(encoding="utf-8"), "def broken( -> int: ...\n"
        )

    def test_bom_is_preserved_at_the_top(self) -> None:
        source = self.root / "test.py"
        source.write_bytes("﻿R.<x> = GF(2)[]\n".encode("utf-8"))

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=_fake_preparse,
        ):
            result = preparse_path(source)

        self.assertTrue(result.changed)
        self.assertTrue(source.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(
            source.read_text(encoding="utf-8-sig"),
            _converted_expected("R = GF(Integer(2))['x']; (x,) = R._first_ngens(1)\n"),
        )

    def test_shebang_stays_first(self) -> None:
        source = self.root / "test.py"
        source.write_text("#!/usr/bin/env sage\nGF(2^8)\n", encoding="utf-8")

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=lambda contents: contents,
        ):
            result = preparse_path(source)

        self.assertTrue(result.changed)
        self.assertEqual(
            source.read_text(encoding="utf-8"),
            "#!/usr/bin/env sage\n" + CONVERSION_MARKER + "\n" + SAGE_ALL_IMPORT + "GF(2^8)\n",
        )

    def test_crlf_file_without_changes_is_not_rewritten(self) -> None:
        source = self.root / "test.py"
        original = "answer = 42\r\nmore = 1\r\n"
        source.write_bytes(original.encode("utf-8"))

        with mock.patch(
            "sage_pycharm_stubgen.preparser._load_preparse_file",
            return_value=lambda contents: contents,
        ):
            result = preparse_path(source)

        self.assertFalse(result.changed)
        self.assertEqual(source.read_bytes(), original.encode("utf-8"))


class EnsureSageImportTests(unittest.TestCase):
    def test_adds_import_when_sage_factory_is_used_without_it(self) -> None:
        converted = "F = GF(2**8)\n"

        result = _ensure_sage_import(converted)

        self.assertEqual(result, SAGE_ALL_IMPORT + converted)

    def test_adds_import_when_numeric_literals_were_extracted(self) -> None:
        converted = "_sage_const_2 = Integer(2)\nx = _sage_const_2\n"

        result = _ensure_sage_import(converted)

        self.assertEqual(result, SAGE_ALL_IMPORT + converted)

    def test_adds_import_for_function_style_entries(self) -> None:
        for snippet in ("solve(f, x)", "show(f)", "gcd(a, b)", "discrete_log(g, h)"):
            with self.subTest(snippet=snippet):
                result = _ensure_sage_import(snippet + "\n")
                self.assertEqual(result, SAGE_ALL_IMPORT + snippet + "\n")

    def test_adds_import_for_attribute_style_entries(self) -> None:
        result = _ensure_sage_import("y = SR.var('y')\n")

        self.assertEqual(result, SAGE_ALL_IMPORT + "y = SR.var('y')\n")

    def test_does_not_match_attribute_access_on_other_names(self) -> None:
        result = _ensure_sage_import("value = my_lib.SR.var('y')\n")

        self.assertEqual(result, "value = my_lib.SR.var('y')\n")

    def test_keeps_existing_star_import(self) -> None:
        converted = "from sage.all import *\nF = GF(2**8)\n"

        result = _ensure_sage_import(converted)

        self.assertEqual(result, converted)

    def test_keeps_existing_named_import(self) -> None:
        converted = "from sage.all import GF\nF = GF(2**8)\n"

        result = _ensure_sage_import(converted)

        self.assertEqual(result, converted)

    def test_leaves_plain_python_alone(self) -> None:
        converted = "answer = 42\n"

        result = _ensure_sage_import(converted)

        self.assertEqual(result, converted)


class LoadPreparseFileTests(unittest.TestCase):
    def test_prefers_new_sage_location(self) -> None:
        sage = _fake_module("sage")
        sage.__path__ = []
        repl = _fake_module("sage.repl")
        repl.__path__ = []
        misc = _fake_module("sage.misc")
        misc.__path__ = []
        new_location = _fake_module(
            "sage.repl.preparse", {"preparse_file": lambda contents: "NEW"}
        )
        old_location = _fake_module(
            "sage.misc.preparser", {"preparse_file": lambda contents: "OLD"}
        )
        modules = {
            "sage": sage,
            "sage.repl": repl,
            "sage.repl.preparse": new_location,
            "sage.misc": misc,
            "sage.misc.preparser": old_location,
        }

        with mock.patch.dict(sys.modules, modules):
            self.assertIs(_load_preparse_file(), new_location.preparse_file)
            self.assertEqual(preparse_source("anything"), "NEW")

    def test_falls_back_to_old_sage_location(self) -> None:
        sage = _fake_module("sage")
        sage.__path__ = []
        misc = _fake_module("sage.misc")
        misc.__path__ = []
        old_location = _fake_module(
            "sage.misc.preparser", {"preparse_file": lambda contents: "OLD"}
        )
        modules = {
            "sage": sage,
            "sage.misc": misc,
            "sage.misc.preparser": old_location,
        }

        with mock.patch.dict(sys.modules, modules):
            self.assertIs(_load_preparse_file(), old_location.preparse_file)
            self.assertEqual(preparse_source("anything"), "OLD")


class PreparseCliTests(unittest.TestCase):
    def test_parser_exposes_preparse_subcommand(self) -> None:
        args = build_parser().parse_args(["preparse", "example.py", "--check"])

        self.assertEqual(args.command, "preparse")
        self.assertEqual(args.files, [Path("example.py")])
        self.assertTrue(args.check)
        self.assertFalse(args.no_backup)
        self.assertFalse(args.force)
        self.assertFalse(args.json)
        self.assertIsNone(args.output)

    def test_main_preparse_rewrites_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "test.py"
            source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")

            with mock.patch(
                "sage_pycharm_stubgen.preparser._load_preparse_file",
                return_value=_fake_preparse,
            ):
                exit_code = main(["preparse", str(source)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                source.read_text(encoding="utf-8"),
                _converted_expected(
                    "R = GF(Integer(2))['x']; (x,) = R._first_ngens(1)\n"
                ),
            )

    def test_main_preparse_check_returns_1_when_changes_needed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "test.py"
            original = "R.<x> = GF(2)[]\n"
            source.write_text(original, encoding="utf-8")

            with mock.patch(
                "sage_pycharm_stubgen.preparser._load_preparse_file",
                return_value=_fake_preparse,
            ):
                exit_code = main(["preparse", str(source), "--check"])

            self.assertEqual(exit_code, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_main_preparse_check_returns_0_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "test.py"
            source.write_text("answer = 42\n", encoding="utf-8")

            with mock.patch(
                "sage_pycharm_stubgen.preparser._load_preparse_file",
                return_value=lambda contents: contents,
            ):
                exit_code = main(["preparse", str(source), "--check"])

            self.assertEqual(exit_code, 0)

    def test_main_preparse_missing_file_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code = main(["preparse", str(Path(temp_dir) / "missing.py")])

            self.assertEqual(exit_code, 2)

    def test_main_preparse_json_output_is_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "test.py"
            source.write_text("R.<x> = GF(2)[]\n", encoding="utf-8")

            with mock.patch(
                "sage_pycharm_stubgen.preparser._load_preparse_file",
                return_value=_fake_preparse,
            ), mock.patch("sys.stdout") as stdout:
                exit_code = main(["preparse", str(source), "--json"])

            self.assertEqual(exit_code, 0)
            output = "".join(call.args[0] for call in stdout.write.call_args_list)
            payload = json.loads(output)
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["path"], str(source.resolve()))
            self.assertTrue(payload[0]["changed"])


if __name__ == "__main__":
    unittest.main()
