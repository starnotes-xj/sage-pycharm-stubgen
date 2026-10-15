from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_NAME = ".sage-pycharm-stubgen-in-place.json"
CURATED_DOCS_NAME = ".sage-pycharm-stubgen-curated-docs.json"


def _current_version() -> str:
    # The version of the CODE being executed, not of whatever dist happens to
    # be pip-installed: running the main branch from PYTHONPATH shadows an old
    # installed release, and the manifest must record the writer's version.
    from . import __version__

    if __version__:
        return __version__
    try:
        return importlib.metadata.version("sage-pycharm-stubgen")
    except importlib.metadata.PackageNotFoundError:
        return "0"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Numeric version key; non-numeric parts (``dev``, ``rc``) are ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


@dataclass(frozen=True)
class InstallationResult:
    target_root: Path
    installed_files: int
    removed_stale_files: int
    preserved_existing_files: int
    taken_over_files: int
    manifest: Path
    preserved_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallResult:
    target_root: Path
    removed_files: int
    restored_backups: int


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of re-hashing an installed stub package against its manifest."""

    manifest: Path
    checked_files: int
    missing: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    unowned: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.modified or self.unowned)


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


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
    overwrite_unowned: bool = False,
    force: bool = False,
    generator_commit: str | None = None,
) -> InstallationResult:
    """Install generated stubs next to Sage's runtime modules.

    PyCharm's WSL interpreter indexing reliably sees ``sage/foo.pyi`` beside
    ``sage/foo.py`` or ``sage/foo.so``.  A manifest prevents the installer from
    overwriting or later removing files it does not own; with
    ``overwrite_unowned=True`` a pre-existing unowned ``.pyi`` on the same
    path is taken over, after being backed up to ``<name>.pyi.sps-bak`` so an
    uninstall can restore it.

    The manifest records the generator version AND the SHA-256 of every
    installed file, so ``verify`` can answer which artifact is actually
    installed.  Installing with an OLDER tool version than the one that
    produced the current stubs is refused (unless ``force`` is set): the
    newer tool's documentation enrichment (curated Chinese docs, return
    annotations) would be silently lost otherwise.
    """
    source, generated_files = _validated_generated_files(output_root)
    target = _validated_sage_package(sage_package)
    manifest = target / MANIFEST_NAME
    previously_owned = set(_manifest_files(target))
    existing = _read_manifest(target)
    installed_version = str(existing.get("generator_version") or "0")
    if (
        not force
        and _version_tuple(_current_version()) < _version_tuple(installed_version)
    ):
        raise RuntimeError(
            "The installed stubs were generated by a newer version "
            f"({installed_version}) of sage-pycharm-stubgen than this one "
            f"({_current_version()}).  Overwriting them would lose the newer "
            "tool's documentation enrichment (curated Chinese docs and return "
            "annotations).  Upgrade the tool (pip install -U "
            "sage-pycharm-stubgen) or pass --force."
        )

    pending: list[tuple[Path, Path]] = []
    preserved_existing = 0
    preserved_paths: list[str] = []
    taken_over = 0
    backups: dict[str, str] = {}
    for source_file in generated_files:
        relative = source_file.relative_to(source)
        destination = target / relative
        if destination.exists() and relative not in previously_owned:
            if not overwrite_unowned:
                preserved_existing += 1
                preserved_paths.append(relative.as_posix())
                continue
            backup = destination.with_name(destination.name + ".sps-bak")
            shutil.copy2(destination, backup)
            backups[relative.as_posix()] = backup.name
            taken_over += 1
        pending.append((source_file, relative))

    installed: set[Path] = set()
    file_hashes: dict[str, str] = {}
    for source_file, relative in pending:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
        installed.add(relative)
        digest = _sha256_file(source_file)
        if digest:
            file_hashes[relative.as_posix()] = digest

    marker_relative = Path("py.typed")
    marker = target / marker_relative
    if not marker.exists() or marker_relative in previously_owned:
        marker.write_text("", encoding="utf-8")
        installed.add(marker_relative)
    marker_digest = _sha256_file(marker)
    if marker_digest:
        file_hashes[marker_relative.as_posix()] = marker_digest

    # Ship the curated Chinese docs next to the installed stubs so the
    # documentation data survives in the environment even if the stubs are
    # regenerated by other tooling (audit and recovery artifact).
    curated_relative = Path(CURATED_DOCS_NAME)
    try:
        from .supplemental_docs import SUPPLEMENTAL_DOCS

        curated_payload = json.dumps(SUPPLEMENTAL_DOCS, ensure_ascii=False, indent=2)
        (target / curated_relative).write_text(
            curated_payload + "\n", encoding="utf-8"
        )
        installed.add(curated_relative)
        curated_digest = _sha256_file(target / curated_relative)
        if curated_digest:
            file_hashes[curated_relative.as_posix()] = curated_digest
    except Exception:  # pragma: no cover - the data module is always shipped
        pass

    stale = previously_owned - installed
    removed_stale = 0
    for relative in sorted(stale, key=lambda item: len(item.parts), reverse=True):
        stale_file = target / relative
        if stale_file.is_file():
            stale_file.unlink()
            removed_stale += 1
            _remove_empty_parents(stale_file, target)

    payload = {
        "format": 2,
        "generator": "sage-pycharm-stubgen",
        "generator_version": _current_version(),
        "generator_commit": generator_commit,
        "sage_version": sage_version,
        "sage_package": str(target),
        "generated_from": str(output_root.resolve()),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(path.as_posix() for path in installed),
        "file_hashes": file_hashes,
        "backups": backups,
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
        taken_over,
        manifest,
        tuple(preserved_paths),
    )


def verify_stub_package(sage_package: Path) -> VerificationResult:
    """Recompute hashes of the installed stubs against the manifest.

    Reports files that disappeared, files whose content no longer matches
    the recorded hash, and ``.pyi`` files inside the Sage package that the
    manifest does not own (leftovers from an untracked earlier install —
    the state that previously made 'which version is installed'
    unanswerable).  Manifests written before format 2 carry no hashes; for
    those only presence and ownership are checked.
    """
    target = _validated_sage_package(sage_package)
    manifest = target / MANIFEST_NAME
    if not manifest.is_file():
        raise RuntimeError(
            f"No installation manifest at {manifest}: this Sage package has "
            "no sage-pycharm-stubgen installation to verify"
        )
    payload = _read_manifest(target)
    hashes = payload.get("file_hashes")
    if not isinstance(hashes, dict):
        hashes = {}
    owned: set[str] = set()
    missing: list[str] = []
    modified: list[str] = []
    for relative in _manifest_files(target):
        owned.add(relative.as_posix())
        installed_file = target / relative
        if not installed_file.is_file():
            missing.append(relative.as_posix())
            continue
        expected = hashes.get(relative.as_posix())
        if not isinstance(expected, str) or not expected:
            continue  # legacy manifest: presence-only check
        if _sha256_file(installed_file) != expected:
            modified.append(relative.as_posix())
    unowned = sorted(
        installed.relative_to(target).as_posix()
        for installed in target.rglob("*.pyi")
        if installed.is_file() and installed.relative_to(target).as_posix() not in owned
    )
    return VerificationResult(manifest, len(owned), missing, modified, unowned)


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
    restored = 0
    payload = _read_manifest(target)
    backups = payload.get("backups", {})
    if isinstance(backups, dict):
        for relative, backup_name in backups.items():
            if not isinstance(relative, str) or not isinstance(backup_name, str):
                continue
            owned_file = target / Path(relative)
            backup = owned_file.with_name(backup_name)
            if backup.is_file() and not owned_file.exists():
                shutil.move(str(backup), str(owned_file))
                restored += 1
    manifest.unlink()
    return UninstallResult(target, removed, restored)
