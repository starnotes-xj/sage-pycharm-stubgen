from __future__ import annotations

import ast
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_NAME = ".sage-pycharm-stubgen-in-place.json"


@dataclass(frozen=True)
class InstallationResult:
    target_root: Path
    installed_files: int
    removed_stale_files: int
    preserved_existing_files: int
    manifest: Path


@dataclass(frozen=True)
class UninstallResult:
    target_root: Path
    removed_files: int


def _read_manifest(
    target: Path, manifest_name: str = MANIFEST_NAME
) -> dict[str, object]:
    manifest = target / manifest_name
    if not manifest.is_file():
        return {}
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid manifest payload: {manifest}")
    return payload


def _manifest_files(
    target: Path, manifest_name: str = MANIFEST_NAME
) -> list[Path]:
    manifest = target / manifest_name
    payload = _read_manifest(target, manifest_name)
    result: list[Path] = []
    for raw_path in payload.get("files", []):
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe path in {manifest}: {raw_path!r}")
        result.append(relative)
    return result


def _validated_generated_files(output_root: Path) -> tuple[Path, list[Path]]:
    source = output_root.resolve() / "sage"
    if not source.is_dir():
        raise RuntimeError(f"Generated sage stub directory does not exist: {source}")

    generated_files = sorted(path for path in source.rglob("*.pyi") if path.is_file())
    if not generated_files:
        raise RuntimeError(f"No .pyi files found under: {source}")

    for generated_file in generated_files:
        try:
            ast.parse(
                generated_file.read_text(encoding="utf-8"),
                filename=str(generated_file),
            )
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise RuntimeError(
                f"Refusing to install invalid stub: {generated_file}: {exc}"
            ) from exc
    return source, generated_files


def _validated_sage_package(sage_package: Path) -> Path:
    target = sage_package.resolve()
    if target.name != "sage" or not (target / "all.py").is_file():
        raise RuntimeError(f"Expected an installed Sage package directory, got: {target}")
    return target


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = path.parent
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def install_stub_package(
    output_root: Path,
    sage_package: Path,
    sage_version: str,
) -> InstallationResult:
    """Install generated stubs next to Sage's runtime modules.

    PyCharm's WSL interpreter indexing reliably sees ``sage/foo.pyi`` beside
    ``sage/foo.py`` or ``sage/foo.so``.  A manifest prevents the installer from
    overwriting or later removing files it does not own.
    """
    source, generated_files = _validated_generated_files(output_root)
    target = _validated_sage_package(sage_package)
    manifest = target / MANIFEST_NAME
    previously_owned = set(_manifest_files(target))

    pending: list[tuple[Path, Path]] = []
    preserved_existing = 0
    for source_file in generated_files:
        relative = source_file.relative_to(source)
        destination = target / relative
        if destination.exists() and relative not in previously_owned:
            preserved_existing += 1
            continue
        pending.append((source_file, relative))

    installed: set[Path] = set()
    for source_file, relative in pending:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
        installed.add(relative)

    marker_relative = Path("py.typed")
    marker = target / marker_relative
    if not marker.exists() or marker_relative in previously_owned:
        marker.write_text("", encoding="utf-8")
        installed.add(marker_relative)

    stale = previously_owned - installed
    removed_stale = 0
    for relative in sorted(stale, key=lambda item: len(item.parts), reverse=True):
        stale_file = target / relative
        if stale_file.is_file():
            stale_file.unlink()
            removed_stale += 1
            _remove_empty_parents(stale_file, target)

    payload = {
        "format": 1,
        "generator": "sage-pycharm-stubgen",
        "sage_version": sage_version,
        "sage_package": str(target),
        "generated_from": str(output_root.resolve()),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(path.as_posix() for path in installed),
    }
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return InstallationResult(
        target,
        len(installed),
        removed_stale,
        preserved_existing,
        manifest,
    )


def uninstall_stub_package(sage_package: Path) -> UninstallResult:
    target = _validated_sage_package(sage_package)
    manifest = target / MANIFEST_NAME
    if not manifest.is_file():
        return UninstallResult(target, 0)

    removed = 0
    for relative in sorted(
        _manifest_files(target),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        owned_file = target / relative
        if owned_file.is_file():
            owned_file.unlink()
            removed += 1
            _remove_empty_parents(owned_file, target)
    manifest.unlink()
    return UninstallResult(target, removed)
