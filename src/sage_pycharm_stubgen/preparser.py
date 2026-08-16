from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .factory_inference import EXPLICIT_FACTORY_NAMES


BACKUP_SUFFIX = ".preparse-backup"


@dataclass(frozen=True)
class PreparseResult:
    path: Path
    changed: bool
    backup: Path | None = None
    error: str | None = None

SAGE_ALL_IMPORT = "from sage.all import *\n"

# Marks files converted in place so that re-runs report them Clean instead of
# re-wrapping the already converted literals.  Sage's preparser is not
# idempotent on its own output: a second run turns `_sage_const_2 = Integer(2)`
# into `_sage_const_2 = Integer(_sage_const_2)` plus a duplicate declaration.
CONVERSION_MARKER = "# Converted by sage-pycharm-stubgen. Remove this line to re-convert Sage syntax."

# Names that only make sense as sage.all exports.  Files that use them without
# an import rely on Sage's implicit namespace injection, which only .sage
# files get from the sage command.  Shares the factory list used by the
# generator and adds common function-style entry points plus attribute-style
# usages such as ``SR.var(...)``.
_SAGE_HINTS = frozenset(EXPLICIT_FACTORY_NAMES) | frozenset(
    {
        "AA",
        "CC",
        "ComplexField",
        "Euler_phi",
        "FunctionField",
        "Integer",
        "QQ",
        "QQbar",
        "Qp",
        "Qq",
        "QuadraticField",
        "RR",
        "Rational",
        "RealField",
        "RealNumber",
        "SR",
        "SymmetricGroup",
        "ZZ",
        "discrete_log",
        "divisors",
        "factor",
        "gcd",
        "is_prime",
        "kronecker",
        "lcm",
        "moebius",
        "next_prime",
        "random_prime",
        "show",
        "solve",
        "var",
    }
)

_SAGE_HINT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])("
    + "|".join(sorted(map(re.escape, _SAGE_HINTS)))
    + r")\s*(\(|\.)"
)

_CODING_COOKIE = re.compile(r"^#.*coding[:=]")


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


def _insert_at_top(text: str, block: str) -> str:
    """Insert ``block`` after any shebang and encoding-cookie lines."""
    lines = text.splitlines(keepends=True)
    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1
        if len(lines) > 1 and _CODING_COOKIE.match(lines[1]):
            insert_at = 2
    elif lines and _CODING_COOKIE.match(lines[0]):
        insert_at = 1
    lines.insert(insert_at, block)
    return "".join(lines)


def _ensure_sage_import(converted: str) -> str:
    """Insert ``from sage.all import *`` when Sage symbols are used without it.

    The Sage command injects the sage.all namespace before running ``.sage``
    files, so they often omit the import.  A ``.py`` file has no such
    injection; without the import it fails at runtime and static analysis
    reports every Sage name as unresolved.
    """
    if re.search(r"^from sage\.all import", converted, re.MULTILINE):
        return converted
    if "_sage_const_" in converted or _SAGE_HINT_PATTERN.search(converted):
        return _insert_at_top(converted, SAGE_ALL_IMPORT)
    return converted


def _prepend_marker(text: str) -> str:
    """Place the conversion marker directly above the sage.all import."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("from sage.all import"):
            lines.insert(index, CONVERSION_MARKER + "\n")
            return "".join(lines)
    return _insert_at_top(text, CONVERSION_MARKER + "\n")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _atomic_replace(path: Path, contents: str) -> None:
    """Write ``contents`` to ``path`` without leaving a partial file."""
    handle, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
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
    force: bool = False,
) -> PreparseResult:
    """Convert one file with Sage's preparser.

    In-place mode (default) rewrites the file atomically, keeps a
    ``.preparse-backup`` copy of the first original version, and stamps the
    file with a conversion marker so re-runs report it Clean.  ``check_only``
    reports whether conversion is needed without writing anything, and
    ``output_dir`` writes converted copies instead of touching the original.
    """
    source = path.resolve()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        return PreparseResult(source, changed=False, error=str(exc))
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        original = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        return PreparseResult(source, changed=False, error=str(exc))

    if not force and CONVERSION_MARKER in original:
        return PreparseResult(source, changed=False)

    try:
        converted = preparse_source(original)
    except (ImportError, SyntaxError, OSError, RuntimeError) as exc:
        return PreparseResult(source, changed=False, error=str(exc))
    converted = _ensure_sage_import(converted)

    changed = _normalize_newlines(converted) != _normalize_newlines(original)
    if check_only or not changed:
        return PreparseResult(source, changed=changed)

    converted = _prepend_marker(converted)
    if "\r\n" in original:
        converted = converted.replace("\r\n", "\n").replace("\n", "\r\n")
    payload = ("\ufeff" + converted) if has_bom else converted

    if output_dir is not None:
        destination = (output_dir / source.name).resolve()
        if destination == source:
            return PreparseResult(
                source,
                changed=False,
                error=(
                    "output directory resolves to the source file itself; "
                    "refusing to overwrite without a backup"
                ),
            )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(payload, encoding="utf-8", newline="")
        except OSError as exc:
            return PreparseResult(destination, changed=False, error=str(exc))
        return PreparseResult(destination, changed=True)

    backup_path = source.with_name(source.name + BACKUP_SUFFIX)
    try:
        if backup and not backup_path.exists():
            shutil.copy2(source, backup_path)
        _atomic_replace(source, payload)
    except OSError as exc:
        return PreparseResult(source, changed=False, error=str(exc))
    return PreparseResult(source, changed=True, backup=backup_path if backup else None)
