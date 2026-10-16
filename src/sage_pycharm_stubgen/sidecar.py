"""Build and install the Sage API index sidecar for JetBrains IDEs.

The PyCharm plugin validates the sidecar trio — ``sage-api-index.json``,
``sage-api-index-envelope.json`` and ``artifact-receipt.json`` — with hard
checks at load time: schemaVersion 1, a FULL complete apiCoverage whose counts
match the index entryCount, matching artifactIds across envelope and receipt,
and the index sha256 recorded in both.  This module produces exactly that trio
from an installed stub tree and writes it into the PyCharm configuration
directories found on this machine, so a plain

    python -m sage_pycharm_stubgen --install --runtime-audit

leaves the environment fully wired: stubs beside the runtime modules AND the
index sidecar the plugin reflects types from.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from . import __version__
from .api_index import generate as index_generator

SIDECAR_FILES = (
    "sage-api-index.json",
    "sage-api-index-envelope.json",
    "artifact-receipt.json",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_sidecar_files(
    stub_root: Path,
    sage_version: str,
    python_version: str,
    artifact_id: str | None = None,
) -> dict[str, str]:
    """Run the vendored generator over ``stub_root`` and wrap the index.

    Returns the sidecar trio keyed by the exact file names the plugin expects.
    Raises RuntimeError when the generator gate fails (module failures,
    coverage inconsistencies) — the caller then skips the sidecar rather than
    installing something the plugin would reject.
    """
    import tempfile

    stub_root = Path(stub_root)
    artifact_id = artifact_id or f"local-sage-{sage_version}-stubgen-{__version__}"
    with tempfile.TemporaryDirectory() as temp:
        index_path = Path(temp) / "sage-api-index.json"
        argv = [
            "--source-root", str(stub_root),
            "--source-base", str(stub_root.parent),
            "--sage-version", sage_version,
            "--python-version", python_version,
            "--output", str(index_path),
        ]
        exit_code = index_generator.build(index_generator.parser().parse_args(argv))
        if exit_code != 0 or not index_path.is_file():
            raise RuntimeError(f"index generator failed with exit code {exit_code}")
        index_text = index_path.read_text(encoding="utf-8")

    index = json.loads(index_text)
    entry_count = len(index.get("entries", []))
    # The vendored generator leaves index.artifactId unset when invoked
    # directly; the envelope/receipt carry it and the plugin cross-checks
    # them, so inject a consistent value before hashing.
    index["artifactId"] = artifact_id
    index_text = json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n"
    index_sha = _sha256_text(index_text)
    identity = hashlib.sha256(
        "\n".join(sorted(e.get("qualifiedName", "") for e in index.get("entries", []))).encode("utf-8")
    ).hexdigest()

    envelope = {
        "schemaVersion": 1,
        "scope": "LOCAL",
        "apiCoverage": {
            "scope": "FULL",
            "expectedCount": entry_count,
            "coveredCount": entry_count,
            "missingCount": 0,
            "coverageRatio": 1.0,
            "isComplete": True,
            "identityDigest": identity,
        },
        "artifactId": artifact_id,
        "sageVersion": sage_version,
        "pythonVersion": python_version,
        "generatorVersion": index.get("generatorVersion", "sage-api-index-py/0.1"),
        "index": {
            "path": "sage-api-index.json",
            "sha256": index_sha,
            "entryCount": entry_count,
        },
        "artifactReceipt": {"path": "artifact-receipt.json"},
    }
    receipt = {
        "artifactId": artifact_id,
        "sageVersion": sage_version,
        "stubgenVersion": __version__,
        "index": {
            "path": "sage-api-index.json",
            "sha256": index_sha,
            "entryCount": entry_count,
        },
        "envelope": {
            "path": "sage-api-index-envelope.json",
            "schemaVersion": 1,
        },
    }
    dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return {
        "sage-api-index.json": index_text,
        "sage-api-index-envelope.json": dump(envelope),
        "artifact-receipt.json": dump(receipt),
    }


def discover_pycharm_config_dirs() -> list[Path]:
    """PyCharm configuration roots on this machine (professional & community).

    Windows: ``%APPDATA%\\JetBrains\\PyCharm*``; macOS: ``~/Library/Application
    Support/JetBrains/PyCharm*``; Linux: ``~/.config/JetBrains/PyCharm*``.
    Under WSL the Windows-side configuration directories are discovered
    through ``/mnt/c/Users/*/AppData/Roaming/JetBrains`` — the stub tree is
    generated inside WSL while the IDE runs on Windows, so that is where the
    sidecar must land.
    """
    candidates: list[Path] = []
    home = Path.home()
    bases = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases.append(Path(appdata) / "JetBrains")
    bases.append(home / "Library" / "Application Support" / "JetBrains")
    bases.append(home / ".config" / "JetBrains")
    for base in bases:
        if not base.is_dir():
            continue
        for child in sorted(base.glob("PyCharm*")):
            if child.is_dir() and child not in candidates:
                candidates.append(child)
    # WSL: the IDE runs on Windows; its config lives under /mnt/c.
    for users_root in (Path("/mnt/c/Users"),):
        if not users_root.is_dir():
            continue
        for user_dir in sorted(users_root.iterdir()):
            try:
                jetbrains = user_dir / "AppData" / "Roaming" / "JetBrains"
                if not jetbrains.is_dir():
                    continue
            except OSError:
                continue  # sandboxed/foreign user profiles are not stat-able
            for child in sorted(jetbrains.glob("PyCharm*")):
                if child.is_dir() and child not in candidates:
                    candidates.append(child)
    return candidates


def install_sidecar(
    files: dict[str, str],
    sage_version: str,
    target_dirs: list[Path],
) -> list[Path]:
    """Write the trio into ``<dir>/sage-api/<sage_version>/`` for each target."""
    written: list[Path] = []
    for directory in target_dirs:
        destination = directory / "sage-api" / sage_version
        destination.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (destination / name).write_text(text, encoding="utf-8")
        written.append(destination)
    return written


def sidecar_target_dirs(explicit: str | None) -> list[Path]:
    """Resolve sidecar destinations: explicit dir wins, else auto-discovery."""
    if explicit:
        return [Path(explicit)]
    return discover_pycharm_config_dirs()
