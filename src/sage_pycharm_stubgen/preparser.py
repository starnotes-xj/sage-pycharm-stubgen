from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


BACKUP_SUFFIX = ".preparse-backup"

SAGE_ALL_IMPORT = "from sage.all import *\n"

# Names that only make sense as sage.all exports; used to detect files that
# rely on Sage's implicit namespace injection (which only .sage files get).
_SAGE_FACTORY_HINTS = re.compile(
    r"\b(GF|PolynomialRing|Integer|RealNumber|ComplexNumber|Rational|"
    r"ZZ|QQ|RR|CC|FiniteField|NumberField|matrix|vector|var|SR|Mod)\s*\("
)


def _ensure_sage_import(converted: str) -> str:
    """Insert ``from sage.all import *`` when Sage symbols are used without it.

    The Sage command injects the sage.all namespace before running ``.sage``
    files, so they often omit the import.  A ``.py`` file has no such
    injection; without the import it fails at runtime and static analysis
    reports every Sage name as unresolved.
    """
    if re.search(r"^from sage\.all import", converted, re.MULTILINE):
        return converted
    if "_sage_const_" in converted or _SAGE_FACTORY_HINTS.search(converted):
        return SAGE_ALL_IMPORT + converted
    return converted


@dataclass(frozen=True)
class PreparseResult:
    path: Path
    changed: bool
    backup: Path | None = None
    error: str | None = None


def _load_preparse_file():
    """Return Sage's whole-file preparser.

    Sage 10.9 moved the preparser from ``sage.misc.preparser`` to
    ``sage.repl.preparse``; try the new location first and fall back to the
    old one for older Sage releases.
    """
    try:
        from sage.repl.preparse import preparse_file  # Sage >= 10.9
    except ImportError:
        from sage.misc.preparser import preparse_file  # Sage < 10.9
    return preparse_file


def preparse_source(contents: str) -> str:
    """Convert Sage preparser syntax in ``contents`` to plain Python.

    Handles ``R.<x> = GF(2)[]`` generator declarations, ``^`` as power,
    ``e^(-1)``, and Sage numeric literals, exactly as Sage itself would.
    """
    return _load_preparse_file()(contents)


def _atomic_replace(path: Path, contents: str) -> None:
    """Write ``contents`` to ``path`` without leaving a partial file."""
    handle, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(contents)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def preparse_path(
    path: Path,
    *,
    check_only: bool = False,
    backup: bool = True,
    output_dir: Path | None = None,
) -> PreparseResult:
    """Convert one file with Sage's preparser.

    In-place mode (default) rewrites the file atomically and keeps a
    ``.preparse-backup`` copy of the first original version.  ``check_only``
    reports whether conversion is needed without writing anything, and
    ``output_dir`` writes converted copies instead of touching the original.
    """
    source = path.resolve()
    try:
        original = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return PreparseResult(source, changed=False, error=str(exc))
    try:
        converted = _ensure_sage_import(preparse_source(original))
    except (SyntaxError, OSError, RuntimeError) as exc:
        return PreparseResult(source, changed=False, error=str(exc))

    changed = converted != original
    if check_only or not changed:
        return PreparseResult(source, changed=changed)

    if output_dir is not None:
        destination = (output_dir / source.name).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(converted, encoding="utf-8")
        return PreparseResult(destination, changed=True)

    backup_path = source.with_name(source.name + BACKUP_SUFFIX)
    if backup and not backup_path.exists():
        shutil.copy2(source, backup_path)
    _atomic_replace(source, converted)
    return PreparseResult(source, changed=True, backup=backup_path if backup else None)
