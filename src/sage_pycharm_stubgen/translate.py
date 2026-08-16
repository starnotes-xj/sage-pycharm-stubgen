"""Batch-translate the remaining English stub docstrings into Chinese.

Two-tier documentation model:

- **Curated layer** (``supplemental_docs.py``): hand-written Chinese docs with
  examples executed against Sage — the gold standard, applied during
  generation.
- **Translation layer** (this module): everything else gets a machine
  translation, applied from a persistent JSON cache so the work is done once
  and shared.

The cache is a plain data file (the same philosophy as the curated table):
a batch can run anywhere a translator endpoint is reachable, and the result
ships to every user without requiring network access.
"""

from __future__ import annotations

import ast
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# A separator Google reliably preserves; NUL never appears in docstrings.
_SEP = "\n\x00\n"

USER_CACHE_DIR = Path.home() / ".sage-pycharm-stubgen"


def _request_google_batch(texts: list[str], timeout: float = 20.0) -> dict[str, str]:
    joined = _SEP.join(texts)
    data = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": joined}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single",
        data=data,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    translated = "".join(segment[0] if segment[0] else "" for segment in payload[0])
    parts = translated.split(_SEP)
    if len(parts) != len(texts):
        return {}
    return {src: dst.strip() for src, dst in zip(texts, parts) if dst.strip()}


def translate_texts(
    texts: list[str],
    batch_size: int = 8,
    pause: float = 0.4,
    attempts: int = 3,
) -> tuple[dict[str, str], int]:
    """Translate ``texts``; returns ``(translated, failed_count)``.

    Batches of ``batch_size`` go out as single requests; texts whose batch
    round-trip breaks fall back to individual requests.  Transient errors are
    retried with exponential backoff.
    """
    translated: dict[str, str] = {}
    pending = [t for t in dict.fromkeys(texts) if t not in translated and t.strip()]
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        for attempt in range(attempts):
            try:
                result = _request_google_batch(chunk)
                if result:
                    translated.update(result)
                    break
            except Exception:
                pass
            time.sleep(pause * (2**attempt))
        leftover = [t for t in chunk if t not in translated]
        for single in leftover:
            for attempt in range(attempts):
                try:
                    result = _request_google_batch([single])
                    if result:
                        translated.update(result)
                        break
                except Exception:
                    pass
                time.sleep(pause * (2**attempt))
        time.sleep(pause)
    return translated, len(pending) - len(translated)


def iter_english_docstrings(stubs_root: Path):
    """Yield ``(stub_path, docstring)`` for docstrings that lack Chinese."""
    for stub in sorted(stubs_root.rglob("*.pyi")):
        try:
            text = stub.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            doc = ast.get_docstring(node, clean=False)
            if doc and doc.strip() and not CJK_RE.search(doc):
                yield stub, doc


def apply_translations(stubs_root: Path, cache: dict[str, str]) -> int:
    """Rewrite stub docstrings in place from the cache; returns applied count."""
    from .docstring_enrich import _quote

    applied = 0
    for stub, doc in iter_english_docstrings(stubs_root):
        translated = cache.get(doc)
        if not translated or not CJK_RE.search(translated):
            continue
        try:
            text = stub.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeError):
            continue
        lines = text.splitlines()
        edits: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) or not node.body:
                continue
            current = ast.get_docstring(node, clean=False)
            if current is None or current not in cache:
                continue
            replacement = cache[current]
            if not CJK_RE.search(replacement):
                continue
            first = node.body[0]
            if (
                not isinstance(first, ast.Expr)
                or not isinstance(first.value, ast.Constant)
                or not isinstance(first.value.value, str)
            ):
                continue
            indent = " " * first.col_offset
            literal = _quote(replacement)
            literal_lines = literal.splitlines()
            if len(literal_lines) > 1:
                literal = "\n".join(
                    indent + line if i > 0 else line for i, line in enumerate(literal_lines)
                )
            edits.append((first.lineno, first.end_lineno or first.lineno, indent + literal))
        for start, end, replacement in reversed(edits):
            lines[start - 1 : end] = [replacement]
        try:
            stub.write_text("\n".join(lines) + "\n", encoding="utf-8")
            applied += len(edits)
        except OSError:
            continue
    return applied


class TranslationCache:
    """Persistent source->Chinese mapping for machine translations."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("translations"), dict):
                self.data = payload["translations"]
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "translations": self.data}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def merge(self, bundled: Path) -> None:
        """Fill missing entries from a bundled cache file (lower priority)."""
        if not bundled.is_file():
            return
        try:
            payload = json.loads(bundled.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("translations"), dict):
                for key, value in payload["translations"].items():
                    self.data.setdefault(key, value)
        except (OSError, json.JSONDecodeError):
            return
