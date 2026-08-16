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
import hashlib
import json
import os
import random
import re
import time
import urllib.error
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


_BAIDU_MAX_BYTES = 6000


def _baidu_translate_one(text: str, appid: str, secret: str, timeout: float = 20.0) -> str:
    """Translate a single text through the Baidu general translation API."""
    salt = str(random.randint(10000, 99999))
    sign = hashlib.md5(f"{appid}{text}{salt}{secret}".encode("utf-8")).hexdigest()
    data = urllib.parse.urlencode(
        {"q": text, "from": "en", "to": "zh", "appid": appid, "salt": salt, "sign": sign}
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://fanyi-api.baidu.com/api/trans/vip/translate",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error_code"):
        raise RuntimeError(
            f"Baidu translate error {payload.get('error_code')}: {payload.get('error_msg')}"
        )
    parts = [item["dst"] for item in payload.get("trans_result", [])]
    return "".join(parts).strip()


def _baidu_translate_segmented(
    text: str, appid: str, secret: str, timeout: float = 20.0
) -> str:
    """Translate ``text``, splitting overlong inputs at the 6000-byte limit."""
    if len(text.encode("utf-8")) <= _BAIDU_MAX_BYTES:
        return _baidu_translate_one(text, appid, secret, timeout)
    segments: list[str] = []
    remaining = text
    while remaining:
        cut = _BAIDU_MAX_BYTES
        while cut > 0:
            candidate = remaining[:cut]
            if len(candidate.encode("utf-8")) <= _BAIDU_MAX_BYTES:
                break
            cut -= 256
        if cut <= 0:
            return ""
        segments.append(candidate)
        remaining = remaining[cut:]
    return "".join(
        _baidu_translate_one(segment, appid, secret, timeout) for segment in segments
    )


def _request_baidu_batch(
    texts: list[str], appid: str, secret: str, timeout: float = 20.0
) -> dict[str, str]:
    """Translate each text individually through the Baidu API (1 QPS tier)."""
    result: dict[str, str] = {}
    for text in texts:
        try:
            translated = _baidu_translate_segmented(text, appid, secret, timeout)
        except Exception:
            continue
        if translated:
            result[text] = translated
    return result


def _backoff_sleep(pause: float, attempt: int, error: BaseException | None = None) -> None:
    """Sleep before a retry; HTTP 429 (rate limit) backs off much longer."""
    if isinstance(error, urllib.error.HTTPError) and error.code == 429:
        time.sleep(30.0 + 10.0 * attempt + random.uniform(0, 5.0))
    else:
        time.sleep(pause * (2**attempt))


def _try_translate(
    texts: list[str],
    attempts: int,
    pause: float,
    request_fn,
) -> tuple[dict[str, str], BaseException | None]:
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return request_fn(texts), None
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            last_error = exc
            _backoff_sleep(pause, attempt, exc)
    return {}, last_error


def translate_texts(
    texts: list[str],
    batch_size: int = 8,
    pause: float = 2.0,
    attempts: int = 4,
    backend: str = "google",
    appid: str | None = None,
    secret: str | None = None,
) -> tuple[dict[str, str], int]:
    """Translate ``texts``; returns ``(translated, failed_count)``.

    ``backend="google"`` uses the free Google endpoint with batched
    requests; ``backend="baidu"`` uses the Baidu general translation API
    (``appid``/``secret``, one request per text — the standard tier is 1
    QPS, so keep the caller's pacing gentle).  Transient errors are retried
    with backoff, and callers resume from the persistent cache after any
    interruption.
    """
    if backend == "baidu":
        if not appid or not secret:
            raise ValueError("Baidu backend requires BAIDU_APPID and BAIDU_SECRET")
        request_fn = lambda texts: _request_baidu_batch(texts, appid, secret)
        effective_batch = 1
    else:
        request_fn = _request_google_batch
        effective_batch = batch_size

    translated: dict[str, str] = {}
    pending = [t for t in dict.fromkeys(texts) if t not in translated and t.strip()]
    for start in range(0, len(pending), effective_batch):
        chunk = pending[start : start + effective_batch]
        result, _ = _try_translate(chunk, attempts, pause, request_fn)
        translated.update(result)
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
