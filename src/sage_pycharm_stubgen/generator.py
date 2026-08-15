from __future__ import annotations

import fnmatch
import importlib
import inspect
import json
import keyword
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from .factory_inference import FactoryInference, factory_return_map, infer_factory_returns


DEFAULT_PATTERNS = ("**/*.pyx",)


def _glob_matches(path: str, pattern: str) -> bool:
    """Match ``**`` with the usual zero-or-more-directories semantics."""
    candidates = {pattern}
    pending = [pattern]
    while pending:
        candidate = pending.pop()
        if candidate.startswith("**/"):
            shortened = candidate[3:]
            if shortened not in candidates:
                candidates.add(shortened)
                pending.append(shortened)
        if "/**/" in candidate:
            shortened = candidate.replace("/**/", "/", 1)
            if shortened not in candidates:
                candidates.add(shortened)
                pending.append(shortened)
    return any(fnmatch.fnmatch(path, candidate) for candidate in candidates)


@dataclass(frozen=True)
class Failure:
    source: str
    error: str


@dataclass
class GenerationSummary:
    sage_version: str
    sage_package: str
    output_root: str
    discovered: int = 0
    generated: int = 0
    failed: int = 0
    enhanced: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    factory_inferred: list[str] = field(default_factory=list)
    factory_unresolved: list[str] = field(default_factory=list)
    dynamic_unresolved: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    def write(self, path: Path) -> None:
        payload = asdict(self)
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def detect_sage_package() -> tuple[Path, str]:
    try:
        import sage
        from sage.version import version as sage_version
    except ImportError as exc:
        raise RuntimeError(
            "SageMath is not importable. Run this command with Sage's Python, for "
            "example: sage -python -m sage_pycharm_stubgen"
        ) from exc

    sage_file = getattr(sage, "__file__", None)
    if not sage_file:
        raise RuntimeError("The imported sage package has no filesystem location")
    package = Path(sage_file).resolve().parent
    if not (package / "all.py").is_file():
        raise RuntimeError(f"The detected Sage package looks incomplete: {package}")
    return package, str(sage_version)


def discover_sources(
    sage_package: Path,
    patterns: Iterable[str] = DEFAULT_PATTERNS,
    excludes: Iterable[str] = (),
) -> list[Path]:
    normalized_patterns = tuple(patterns) or DEFAULT_PATTERNS
    normalized_excludes = tuple(excludes)
    matches: list[Path] = []

    for source in sage_package.rglob("*.pyx"):
        relative = source.relative_to(sage_package).as_posix()
        if not any(_glob_matches(relative, pattern) for pattern in normalized_patterns):
            continue
        if any(_glob_matches(relative, pattern) for pattern in normalized_excludes):
            continue
        matches.append(source)

    return sorted(set(matches))


def enhance_integer_mod_stub(path: Path) -> bool:
    """Add the return types needed for ``Mod(...).sqrt`` completion."""
    if not path.is_file():
        return False

    original = path.read_text(encoding="utf-8")
    content = original
    content = re.sub(
        r"^def Mod\(([^\n]*)\)(?:\s*->\s*[^:]+)?:",
        r"def Mod(\1) -> IntegerMod_abstract:",
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r"^def IntegerMod\(([^\n]*)\)(?:\s*->\s*[^:]+)?:",
        r"def IntegerMod(\1) -> IntegerMod_abstract:",
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(r"^mod\s*=\s*Mod\s*\n", "", content, flags=re.MULTILINE)

    if "def mod(" not in content:
        content = content.rstrip() + (
            "\n\n"
            "def mod(n, m, parent=None) -> IntegerMod_abstract: ...\n"
        )

    if content == original:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _lazy_import_target(value: Any) -> tuple[str, str] | None:
    cls = type(value)
    if cls.__module__ != "sage.misc.lazy_import" or cls.__name__ != "LazyImport":
        return None
    # Do not use getattr(value, "_module"): LazyImport.__getattr__ would resolve
    # and execute the import.  Sage exposes this debugging helper specifically
    # for reading its Cython fields without resolving the target.
    try:
        from sage.misc.lazy_import import attributes

        metadata = attributes(value)
    except (ImportError, TypeError, ValueError):
        return None
    module_name = metadata.get("_module")
    source_name = metadata.get("_name")
    if not isinstance(module_name, str) or not isinstance(source_name, str):
        return None
    if not source_name.isidentifier() or not all(
        part.isidentifier() for part in module_name.split(".")
    ):
        return None
    return module_name, source_name


def _import_line(name: str, value: Any) -> str | None:
    lazy_target = _lazy_import_target(value)
    if lazy_target:
        module_name, source_name = lazy_target
        if source_name.isidentifier():
            return f"from {module_name} import {source_name} as {name}"
    value_type = type(value)
    if (
        value_type.__module__ == "sage.misc.lazy_import"
        and value_type.__name__ == "LazyImport"
    ):
        return None

    if isinstance(value, ModuleType):
        module_name = value.__name__
        if module_name and all(part.isidentifier() for part in module_name.split(".")):
            return f"import {module_name} as {name}"

    module_name = getattr(value, "__module__", None)
    source_name = getattr(value, "__name__", None)
    if (
        isinstance(module_name, str)
        and isinstance(source_name, str)
        and module_name != "sage.all"
        and all(part.isidentifier() for part in module_name.split("."))
        and source_name.isidentifier()
    ):
        return f"from {module_name} import {source_name} as {name}"
    return None


def _annotation_lines(name: str, value: Any) -> list[str]:
    value_type = type(value)
    if (
        value_type.__module__ == "sage.misc.lazy_import"
        and value_type.__name__ == "LazyImport"
    ):
        return [f"{name}: Any"]
    module_name = getattr(value_type, "__module__", None)
    type_name = getattr(value_type, "__name__", None)
    alias = f"_Type_{name}"
    if (
        isinstance(module_name, str)
        and isinstance(type_name, str)
        and all(part.isidentifier() for part in module_name.split("."))
        and type_name.isidentifier()
    ):
        return [
            f"from {module_name} import {type_name} as {alias}",
            f"{name}: {alias}",
        ]
    return [f"{name}: Any"]


def _qualified_import(reference: str, alias: str) -> str | None:
    module_name, separator, type_name = reference.rpartition(".")
    if not separator or not module_name or not type_name.isidentifier():
        return None
    if not all(part.isidentifier() for part in module_name.split(".")):
        return None
    return f"from {module_name} import {type_name} as {alias}"


def _factory_declaration(name: str, value: Any, return_type: str) -> tuple[str, str] | None:
    alias = f"_FactoryReturn_{name}"
    import_line = _qualified_import(return_type, alias)
    if import_line is None:
        return None
    try:
        callable_signature = inspect.signature(value)
        # A few Sage factories expose a Python return annotation.  The
        # inferred concrete return type must replace it, not be appended to
        # it (``(...) -> OldType -> NewType`` is invalid Python syntax).
        signature = str(
            callable_signature.replace(
                return_annotation=inspect.Signature.empty
            )
        )
        compile(f"def _probe{signature}: ...\n", "<signature>", "exec")
    except (TypeError, ValueError, SyntaxError):
        signature = "(*args: Any, **kwargs: Any)"
    return import_line, f"def {name}{signature} -> {alias}: ..."


def render_sage_all_stub(
    namespace: Mapping[str, Any], factory_returns: Mapping[str, str] | None = None
) -> str:
    factory_returns = factory_returns or {}
    imports: list[str] = []
    annotations: list[str] = []
    declarations: list[str] = []
    public_names: list[str] = []

    for name in sorted(namespace):
        if name.startswith("_") or not name.isidentifier() or keyword.iskeyword(name):
            continue
        public_names.append(name)
        value = namespace[name]
        if name in factory_returns:
            declaration = _factory_declaration(name, value, factory_returns[name])
            if declaration is not None:
                imports.append(declaration[0])
                declarations.append(declaration[1])
                continue
        line = _import_line(name, value)
        if line is not None:
            imports.append(line)
        else:
            annotation_lines = _annotation_lines(name, value)
            imports.extend(annotation_lines[:-1])
            annotations.append(annotation_lines[-1])

    unique_imports = list(dict.fromkeys(imports))
    body = [
        "# Generated by sage-pycharm-stubgen. Do not edit by hand.",
        "from typing import Any",
        "",
        *unique_imports,
    ]
    if annotations:
        body.extend(["", *annotations])
    if declarations:
        body.extend(["", *declarations])
    body.extend(["", f"__all__: list[str] = {public_names!r}", ""])
    return "\n".join(body)


def generate_sage_all_stub(
    output_root: Path,
) -> tuple[Path, list[FactoryInference], list[str], list[str]]:
    import sage.all as sage_all

    target = output_root / "sage" / "all.pyi"
    target.parent.mkdir(parents=True, exist_ok=True)
    inferences, unresolved = infer_factory_returns(dict(vars(sage_all)))
    factory_returns = factory_return_map(inferences)
    content = render_sage_all_stub(dict(vars(sage_all)), factory_returns)
    target.write_text(content, encoding="utf-8")
    dynamic_unresolved = [
        match.group(1)
        for line in content.splitlines()
        if (match := re.fullmatch(r"([A-Za-z_]\w*): Any", line))
    ]
    (target.parent / "all_cmdline.pyi").write_text(
        "from sage.all import *\n", encoding="utf-8"
    )
    init_stub = target.parent / "__init__.pyi"
    if not init_stub.exists():
        init_stub.write_text("from sage.all import *\n", encoding="utf-8")
    (target.parent / "py.typed").touch()
    target.with_name("factory-inference.json").write_text(
        json.dumps(
            {
                "inferred": [asdict(item) for item in inferences],
                "unresolved": unresolved,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target, inferences, unresolved, dynamic_unresolved


def _safe_signature(value: Any) -> str:
    try:
        signature = str(inspect.signature(value))
        compile(f"def _probe{signature}: ...\n", "<signature>", "exec")
        return signature
    except (TypeError, ValueError, SyntaxError):
        return "(*args: Any, **kwargs: Any)"


def render_runtime_module_stub(module: ModuleType) -> str:
    """Create a conservative fallback stub from an imported extension module."""
    lines = [
        "# Runtime-introspection fallback generated by sage-pycharm-stubgen.",
        "from typing import Any",
        "",
    ]
    for name, value in sorted(vars(module).items()):
        if name.startswith("_") or not name.isidentifier() or keyword.iskeyword(name):
            continue
        if inspect.isclass(value):
            lines.append(f"class {name}: ...")
        elif inspect.isroutine(value) or (
            callable(value) and isinstance(getattr(value, "__name__", None), str)
        ):
            lines.append(f"def {name}{_safe_signature(value)} -> Any: ...")
        else:
            lines.append(f"{name}: Any")
    lines.append("")
    return "\n".join(lines)


def runtime_fallback(source: Path, sage_package: Path, target: Path) -> bool:
    try:
        relative = source.relative_to(sage_package).with_suffix("")
        module_name = "sage." + ".".join(relative.parts)
        module = importlib.import_module(module_name)
        content = render_runtime_module_stub(module)
        compile(content, str(target), "exec")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True
    except (ImportError, OSError, RuntimeError, SyntaxError, ValueError):
        return False


def generate(
    output_root: Path,
    *,
    sage_package: Path | None = None,
    patterns: Iterable[str] = DEFAULT_PATTERNS,
    excludes: Iterable[str] = (),
    include_private: bool = False,
    generate_all: bool = True,
    verbose: bool = False,
) -> GenerationSummary:
    detected_package, sage_version = detect_sage_package()
    package = (sage_package or detected_package).resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    from stubgen_pyx import StubgenPyx
    from stubgen_pyx.config import StubgenPyxConfig

    sources = discover_sources(package, patterns, excludes)
    summary = GenerationSummary(
        sage_version=sage_version,
        sage_package=str(package),
        output_root=str(output_root),
        discovered=len(sources),
    )
    def make_converter(*, include_docstrings: bool, pxd_to_stubs: bool) -> Any:
        return StubgenPyx(
            StubgenPyxConfig(
                continue_on_error=True,
                include_private=include_private,
                include_docstrings=include_docstrings,
                pxd_to_stubs=pxd_to_stubs,
                verbose=verbose,
            )
        )

    converters = [
        ("source", make_converter(include_docstrings=True, pxd_to_stubs=True)),
        ("without-docstrings", make_converter(include_docstrings=False, pxd_to_stubs=True)),
        ("without-pxd", make_converter(include_docstrings=True, pxd_to_stubs=False)),
        (
            "without-docstrings-or-pxd",
            make_converter(include_docstrings=False, pxd_to_stubs=False),
        ),
    ]

    for index, source in enumerate(sources, start=1):
        relative = source.relative_to(package).with_suffix(".pyi")
        target = output_root / "sage" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        result = converters[0][1].convert_single_file(source, target)
        used_fallback: str | None = None
        if not result.success:
            for fallback_name, fallback_converter in converters[1:]:
                retry = fallback_converter.convert_single_file(source, target)
                if retry.success and target.is_file():
                    result = retry
                    used_fallback = fallback_name
                    break
        if not result.success and runtime_fallback(source, package, target):
            used_fallback = "runtime-introspection"

        if target.is_file() and (result.success or used_fallback is not None):
            summary.generated += 1
            if used_fallback is not None:
                summary.fallbacks.append(
                    f"{source.relative_to(package).as_posix()}: {used_fallback}"
                )
        elif result.success:
            # A Cython __init__ file can be deliberately skipped by stubgen-pyx.
            continue
        else:
            summary.failed += 1
            summary.failures.append(Failure(str(source), str(result.error)))
        if verbose:
            state = "ok" if target.is_file() else "failed"
            print(f"[{index}/{len(sources)}] {state}: {relative}", file=sys.stderr)

    integer_mod_stub = (
        output_root / "sage" / "rings" / "finite_rings" / "integer_mod.pyi"
    )
    if enhance_integer_mod_stub(integer_mod_stub):
        summary.enhanced.append(
            "sage.rings.finite_rings.integer_mod: Mod/IntegerMod/mod return types"
        )

    if generate_all:
        (
            all_stub,
            factory_inferred,
            factory_unresolved,
            dynamic_unresolved,
        ) = generate_sage_all_stub(output_root)
        summary.enhanced.append(str(all_stub.relative_to(output_root)))
        summary.factory_inferred = [
            f"{item.name} -> {item.return_type}" for item in factory_inferred
        ]
        summary.factory_unresolved = factory_unresolved
        summary.dynamic_unresolved = dynamic_unresolved

    summary.write(output_root / "generation-report.json")
    return summary
