from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .generator import DEFAULT_PATTERNS, detect_sage_package, generate
from .installer import install_stub_package, uninstall_stub_package


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-generate PyCharm-friendly .pyi files from an installed SageMath "
            "package. Run with Sage's Python interpreter."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(sys.prefix) / "sage_typings",
        help="Build directory for generated stubs (default: <current environment>/sage_typings)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Optional path to the installed sage package; normally auto-detected",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        dest="patterns",
        help='Relative glob to include; repeatable (default: "**/*.pyx")',
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Relative glob to exclude; repeatable",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="Include private Cython functions in generated stubs",
    )
    parser.add_argument(
        "--no-all-stub",
        action="store_true",
        help="Do not generate explicit exports for sage.all",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help=(
            "Install generated .pyi files beside the current Sage runtime modules; "
            "recommended for PyCharm WSL interpreters (strict checks are automatic)"
        ),
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove only .pyi files previously installed into Sage by this tool",
    )
    parser.add_argument(
        "--strict-factories",
        action="store_true",
        help="Fail instead of installing when a detected dynamic factory is unresolved",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Refuse installation if a module fails, a dynamic factory is unresolved, "
            "or a public sage.all export falls back to Any"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.install and args.uninstall:
        parser.error("--install and --uninstall cannot be used together")

    if args.uninstall:
        sage_package, sage_version = detect_sage_package()
        result = uninstall_stub_package(sage_package)
        print(f"SageMath: {sage_version}")
        print(f"Removed: {result.removed_files}")
        print(f"Stub package: {result.target_root}")
        return 0

    summary = generate(
        args.output,
        sage_package=args.source,
        patterns=args.patterns or DEFAULT_PATTERNS,
        excludes=args.exclude,
        include_private=args.include_private,
        generate_all=not args.no_all_stub,
        verbose=args.verbose,
    )
    print(f"SageMath: {summary.sage_version}")
    print(f"Discovered: {summary.discovered}")
    print(f"Generated: {summary.generated}")
    print(f"Failed: {summary.failed}")
    print(f"Output: {summary.output_root}")
    print(f"Report: {Path(summary.output_root) / 'generation-report.json'}")
    if args.strict_factories and summary.factory_unresolved:
        print(
            "Strict factory audit failed: "
            + ", ".join(summary.factory_unresolved),
            file=sys.stderr,
        )
        return 2
    if (args.strict or args.install) and (
        summary.failed or summary.factory_unresolved or summary.dynamic_unresolved
    ):
        details: list[str] = []
        if summary.failed:
            details.append(f"failed modules={summary.failed}")
        if summary.factory_unresolved:
            details.append("factories=" + ", ".join(summary.factory_unresolved))
        if summary.dynamic_unresolved:
            details.append("sage.all Any=" + ", ".join(summary.dynamic_unresolved))
        print("Strict audit failed: " + "; ".join(details), file=sys.stderr)
        return 2
    if args.install:
        if summary.failed:
            print(
                f"Refusing to install because {summary.failed} module(s) failed",
                file=sys.stderr,
            )
            return 2
        result = install_stub_package(
            Path(summary.output_root), Path(summary.sage_package), summary.sage_version
        )
        print(f"Installed: {result.installed_files}")
        if hasattr(result, "preserved_existing_files"):
            print(f"Preserved existing stubs: {result.preserved_existing_files}")
        print(f"Stub target: {result.target_root}")
        print(f"Manifest: {result.manifest}")
    return 1 if summary.failed == summary.discovered and summary.discovered else 0
