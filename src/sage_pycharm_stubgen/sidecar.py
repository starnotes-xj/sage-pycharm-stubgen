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
from typing import Any

from . import __version__
from .api_index import generate as index_generator

SIDECAR_FILES = (
    "sage-api-index.json",
    "sage-api-index-envelope.json",
    "artifact-receipt.json",
)

#: The plugin's SageApiIndexService refuses to load an index above these
#: bounds (MAX_INDEX_ENTRIES / MAX_EXTERNAL_INDEX_BYTES, 250_000 entries /
#: 288 MB as of plugin 1.8.29).  The installed 10.9 corpus sits at ~175k
#: entries (70%), which grows with every Sage release, so generation alerts
#: at 85% — leaving a real mitigation window (prune low-value entries with
#: the plugin repo's sanitize_release_index.py) before the cap bites.
SIDECAR_ENTRY_CAP = 250_000
SIDECAR_INDEX_BYTES_CAP = 288 * 1024 * 1024
SIDECAR_WARN_RATIO = 0.85

#: Documentation sidecar, byte-for-byte compatible with the plugin library's
#: ``SageApiDocumentationSidecar``: 64 hash buckets under ``sage-api-docs/``,
#: one decodable record per line ``KIND␟qualname␉{"summary","body","examples"}``
#: (␟ = U+001F field separator, ␉ = tab record separator).  The main index
#: keeps schemaVersion 1 — the plugin's reader treats per-entry
#: ``documentation`` and ``sources[].digest`` as optional, so a slimmed index
#: stays loadable by every released plugin while documentation moves to files
#: that are opened only for Quick Documentation.
DOCS_DIRECTORY = "sage-api-docs"
DOCS_BUCKET_COUNT = 64
DOCS_FIELD_SEPARATOR = "\x1f"
DOCS_RECORD_SEPARATOR = "\t"


def _java_string_hash(key: str) -> int:
    """Java ``String.hashCode()`` (32-bit two's complement, unsigned here).

    Bucket assignment must match ``SageApiDocumentationSidecar.resourceName``
    on the plugin side; only the low :data:`DOCS_BUCKET_COUNT` bits are used,
    which are identical for the signed and unsigned spelling.
    """
    value = 0
    for character in key:
        value = (value * 31 + ord(character)) & 0xFFFFFFFF
    return value


def _documentation_key(kind: str, qualified_name: str) -> str:
    return kind + DOCS_FIELD_SEPARATOR + qualified_name


def _documentation_line(kind: str, qualified_name: str, doc: dict[str, Any]) -> str:
    """One bucket record; payload matches ``writeDocumentation`` field order."""
    payload = json.dumps(
        {
            "summary": doc.get("summary"),
            "body": doc.get("body"),
            "examples": doc.get("examples", []),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        _documentation_key(kind, qualified_name)
        + DOCS_RECORD_SEPARATOR
        + payload
        + "\n"
    )


def split_documentation(
    index: dict[str, Any],
) -> tuple[int, dict[str, str]]:
    """Move entry documentation into bucket files; drop per-source digests.

    Mutates ``index`` in place: every entry loses its ``documentation`` (the
    Quick Documentation body — 61% of the Sage 10.9 index bytes) and every
    source loses its redundant ``digest`` string (the top-level
    ``sourceDigests`` map keeps one digest per module).  Both fields are
    optional to the plugin's schema-1 reader, so the slimmed index loads on
    every released plugin.  Returns ``(moved, files)`` where ``files`` maps
    ``sage-api-docs/<xx>.ndjson`` to bucket text (all buckets present, empty
    ones included, so reinstalls self-clean stale buckets).
    """
    buckets: dict[int, list[str]] = {}
    moved = 0
    for entry in index.get("entries", []):
        doc = entry.pop("documentation", None)
        if doc:
            key = _documentation_key(entry.get("kind", ""), entry.get("qualifiedName", ""))
            bucket = _java_string_hash(key) & (DOCS_BUCKET_COUNT - 1)
            buckets.setdefault(bucket, []).append(
                _documentation_line(entry.get("kind", ""), entry.get("qualifiedName", ""), doc)
            )
            moved += 1
        for source in entry.get("sources", []):
            if isinstance(source, dict):
                source.pop("digest", None)
    files = {
        f"{DOCS_DIRECTORY}/{bucket:02x}.ndjson": "".join(buckets.get(bucket, []))
        for bucket in range(DOCS_BUCKET_COUNT)
    }
    return moved, files


def capacity_warning(entry_count: int, index_bytes: int) -> str | None:
    """Warning text when the sidecar approaches the plugin's load caps.

    ``None`` while both ratios stay below the warning threshold — the
    normal path stays silent.
    """
    warnings: list[str] = []
    for label, value, cap in (
        ("entries", entry_count, SIDECAR_ENTRY_CAP),
        ("bytes", index_bytes, SIDECAR_INDEX_BYTES_CAP),
    ):
        ratio = value / cap
        if ratio >= SIDECAR_WARN_RATIO:
            warnings.append(
                f"index sidecar at {ratio:.0%} of the plugin {label} cap "
                f"({value}/{cap})"
            )
    if not warnings:
        return None
    return "; ".join(warnings) + (
        " — prune with sanitize_release_index.py (plugin repo) before the "
        "cap rejects the sidecar"
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
    # Slim before hashing: documentation moves to the bucket sidecar and the
    # per-source digests collapse into the top-level table.  The trio hashes
    # therefore describe the slimmed artifact actually installed.
    moved, doc_files = split_documentation(index)
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
        "documentationSidecar": {
            "directory": DOCS_DIRECTORY,
            "entries": moved,
        },
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
        "documentation": {
            "directory": DOCS_DIRECTORY,
            "entries": moved,
            "sha256": {
                name: _sha256_text(text) for name, text in sorted(doc_files.items())
            },
        },
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
    files = {
        "sage-api-index.json": index_text,
        "sage-api-index-envelope.json": dump(envelope),
        "artifact-receipt.json": dump(receipt),
    }
    files.update(doc_files)
    return files


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
    """Write the trio plus doc buckets into ``<dir>/sage-api/<sage_version>/``.

    Bucket names carry a ``sage-api-docs/`` prefix, so each destination
    parent is created on demand.
    """
    written: list[Path] = []
    for directory in target_dirs:
        destination = directory / "sage-api" / sage_version
        for name, text in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        written.append(destination)
    return written


def sidecar_target_dirs(explicit: str | None) -> list[Path]:
    """Resolve sidecar destinations: explicit dir wins, else auto-discovery."""
    if explicit:
        return [Path(explicit)]
    return discover_pycharm_config_dirs()
