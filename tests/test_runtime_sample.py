import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sage_pycharm_stubgen.runtime_sample import (
    RECEIVER_RECIPES,
    SAMPLE_OPS,
    _NO_SELF_OPERAND_OPS,
    _needs_arguments,
    collect_candidates,
    run_batches,
    type_expression,
)


class NeedsArgumentsTests(unittest.TestCase):
    def test_bound_zero_arg_methods_pass(self) -> None:
        import inspect

        def noarg(self): ...
        def defaulted(self, algorithm=None, *, proof=False): ...
        def variadic(self, *args, **kwargs): ...

        for fn in (noarg, defaulted, variadic):
            self.assertFalse(_needs_arguments(inspect.signature(fn)))

    def test_methods_with_required_arguments_are_rejected(self) -> None:
        import inspect

        def required(self, n): ...
        def kwonly_required(self, *, algorithm): ...

        for fn in (required, kwonly_required):
            self.assertTrue(_needs_arguments(inspect.signature(fn)))


class RecipeTableTests(unittest.TestCase):
    def test_recipe_names_are_unique_and_expressions_parse(self) -> None:
        names = [name for name, _ in RECEIVER_RECIPES]
        self.assertEqual(len(names), len(set(names)), "duplicate recipe class names")
        for name, expression in RECEIVER_RECIPES:
            self.assertTrue(name.isidentifier(), name)
            compile(expression, f"<recipe {name}>", "eval")

    def test_self_mod_pair_is_excluded_from_sampling(self) -> None:
        # ``a % a`` segfaults compiled matrix code; the protocol must keep
        # the operator itself (scalar pairs are clean) while dropping the
        # same-instance pair.
        self.assertIn("__mod__", SAMPLE_OPS)
        self.assertEqual(_NO_SELF_OPERAND_OPS, {"__mod__"})


class TypeExpressionTests(unittest.TestCase):
    def test_not_implemented_and_none_are_rejected(self) -> None:
        self.assertIsNone(type_expression(NotImplemented))
        self.assertIsNone(type_expression(None))

    def test_builtin_and_sage_types_map(self) -> None:
        builtin_type = type(5)
        self.assertEqual(
            type_expression(builtin_type(5) if False else 5),
            {"expression": "int", "module": ""},
        )

        class Fake:
            pass

        Fake.__module__ = "sage.rings.integer"
        fake = Fake()
        self.assertEqual(
            type_expression(fake),
            {"expression": "Fake", "module": "sage.rings.integer"},
        )

    def test_dynamic_category_suffix_is_stripped(self) -> None:
        class EllipticCurvePoint_field_with_category:
            pass

        EllipticCurvePoint_field_with_category.__module__ = "sage.schemes.elliptic_curves.ell_point"
        value = EllipticCurvePoint_field_with_category()
        info = type_expression(value)
        self.assertEqual(info["expression"], "EllipticCurvePoint_field")
        self.assertEqual(
            info["module"],
            "sage.schemes.elliptic_curves",
        )

    def test_foreign_modules_are_rejected(self) -> None:
        class NotSage:
            pass

        self.assertIsNone(type_expression(NotSage()))


class BatchRunnerTests(unittest.TestCase):
    def test_collect_candidates_walks_sage_stub_tree(self) -> None:
        import tempfile

        from sage_pycharm_stubgen.runtime_sample import collect_candidates as collect

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            demo = root / "sage" / "demo"
            demo.mkdir(parents=True)
            (demo / "point.pyi").write_text(
                "class Point:\n    def xy(self): ...\n", encoding="utf-8"
            )
            (demo / "__init__.pyi").write_text("", encoding="utf-8")

            candidates = collect(root)

        self.assertEqual(
            candidates,
            [{"module": "sage.demo.point", "class": "Point"}],
        )

    def test_run_batches_samples_live_integer(self) -> None:
        """Integration: the isolated child samples live Sage operators."""
        try:
            import sage.all  # noqa: F401
        except ImportError:
            self.skipTest("live Sage not available")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rings = root / "sage" / "rings"
            rings.mkdir(parents=True)
            (rings / "integer.pyi").write_text(
                "class Integer:\n    def __mul__(self, other): ...\n",
                encoding="utf-8",
            )
            observed, crashed = run_batches(root, batch_size=10, timeout=300.0)

        self.assertEqual(crashed, [])
        mul = observed.get("sage.rings.integer.Integer", {}).get("__mul__")
        self.assertEqual(mul and mul["expression"], "Integer")


class CrashRetryTests(unittest.TestCase):
    def test_crashed_batch_is_split_and_non_poison_classes_recovered(self) -> None:
        """A crashing batch must not cost the classes after the poison.

        ``subprocess.run`` is mocked: any chunk containing class ``Poison``
        exits non-zero, everything else writes a real observation file.
        """
        import unittest.mock

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rings = root / "sage" / "rings"
            rings.mkdir(parents=True)
            # Poison first: the batch splits [P,0,1,2]+[3..6], the left half
            # again [P,0]+[1,2] — the depth-2 cutoff gives up the [P,0]
            # chunk (one casualty next to the poison), everything else is
            # recovered by the retries.
            names = ["Poison"] + [f"Safe{i}" for i in range(7)]
            (rings / "mixed.pyi").write_text(
                "\n".join(f"class {name}:\n    pass\n" for name in names),
                encoding="utf-8",
            )

            def fake_run(argv, capture_output, timeout):  # noqa: ANN001
                candidates_file = Path(argv[argv.index("--candidates") + 1])
                out_file = Path(argv[argv.index("--out") + 1])
                chunk = json.loads(candidates_file.read_text(encoding="utf-8"))
                if any(item["class"] == "Poison" for item in chunk):
                    class Result:
                        returncode = -11

                    return Result()
                results = {
                    f"sage.rings.mixed.{item['class']}": {"__neg__": {"expression": "int", "module": ""}}
                    for item in chunk
                }
                out_file.write_text(json.dumps(results), encoding="utf-8")

                class Result:
                    returncode = 0

                return Result()

            with unittest.mock.patch(
                "subprocess.run", side_effect=fake_run
            ):
                observed, crashed = run_batches(
                    root, batch_size=8, timeout=1.0, sample_methods=False
                )

        # everything except the chunk the poison shared is recovered
        for index in range(1, 7):
            self.assertIn(f"sage.rings.mixed.Safe{index}", observed)
        # the poison's final chunk is the only one given up
        self.assertEqual(len(crashed), 1)


class ObservedCacheTests(unittest.TestCase):
    def test_second_run_serves_from_cache_without_spawning(self) -> None:
        import sys
        import types
        import unittest.mock

        fake_version = types.ModuleType("sage.version")
        fake_version.version = "10.9.test"
        sys.modules["sage.version"] = fake_version
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                rings = root / "sage" / "rings"
                rings.mkdir(parents=True)
                (rings / "integer.pyi").write_text(
                    "class Integer:\n    pass\n", encoding="utf-8"
                )
                spawns = []

                def fake_run(argv, capture_output, timeout):  # noqa: ANN001
                    spawns.append(1)
                    out_file = Path(argv[argv.index("--out") + 1])
                    out_file.write_text(
                        json.dumps(
                            {
                                "sage.rings.integer.Integer": {
                                    "__neg__": {"expression": "int", "module": ""}
                                }
                            }
                        ),
                        encoding="utf-8",
                    )

                    class Result:
                        returncode = 0

                    return Result()

                with unittest.mock.patch("subprocess.run", side_effect=fake_run):
                    first, _ = run_batches(root, batch_size=10, timeout=1.0)
                    self.assertEqual(len(spawns), 1)
                    spawns.clear()
                    second, _ = run_batches(root, batch_size=10, timeout=1.0)

                self.assertEqual(spawns, [])
                self.assertEqual(first, second)
        finally:
            sys.modules.pop("sage.version", None)


if __name__ == "__main__":
    unittest.main()
