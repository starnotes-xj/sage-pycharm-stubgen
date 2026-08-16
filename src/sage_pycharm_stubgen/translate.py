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

_BAIDU_LLM_ENDPOINT = "https://fanyi-api.baidu.com/ait/api/aiTextTranslate"

# Default translation instruction for the LLM endpoint: keep math notation,
# code identifiers and sage: doctest blocks verbatim; translate only the prose.
BAIDU_REFERENCE = (
    "这是 SageMath 的 API 技术文档：请保留数学符号、代码标识符、"
    "``...`` 与 :meth: 等交叉引用标记，以及 sage: 示例代码块原样不动，"
    "只把英文说明文字翻译成简体中文，采用技术文档风格。"
)

_BAIDU_SPLIT_MARK = "<<<SPLIT>>>"
_BAIDU_SPLIT_JOIN = "\n<<<SPLIT>>>\n"
_BAIDU_MAX_PACK = 5000

_BAIDU_SPLIT_REFERENCE = (
    f"这是多段待翻译文本，段与段之间以 {_BAIDU_SPLIT_MARK} 分隔。"
    "请逐段翻译，并原样保留分隔符。"
)


class BaiduRateError(RuntimeError):
    """Baidu rate-limit style errors (54003/59004) — back off much longer."""


def _baidu_translate_one(
    text: str,
    appid: str,
    *,
    secret: str | None = None,
    api_key: str | None = None,
    model_type: str = "llm",
    reference: str = BAIDU_REFERENCE,
    timeout: float = 20.0,
) -> str:
    """Translate a single text through the Baidu LLM text translation API."""
    payload: dict[str, object] = {
        "appid": appid,
        "from": "en",
        "to": "zh",
        "q": text,
        "model_type": model_type,
        "reference": reference,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        salt = str(random.randint(10000, 99999))
        payload["salt"] = salt
        payload["sign"] = hashlib.md5(f"{appid}{text}{salt}{secret}".encode("utf-8")).hexdigest()
    request = urllib.request.Request(
        _BAIDU_LLM_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("error_code"):
        code = str(result.get("error_code"))
        message = result.get("error_msg")
        if code in {"54003", "59004"}:
            raise BaiduRateError(f"Baidu rate limit {code}: {message}")
        raise RuntimeError(f"Baidu translate error {code}: {message}")
    parts = [item["dst"] for item in result.get("trans_result", [])]
    return "".join(parts).strip()


def _baidu_translate_segmented(
    text: str,
    appid: str,
    *,
    secret: str | None = None,
    api_key: str | None = None,
    model_type: str = "llm",
    reference: str = BAIDU_REFERENCE,
    timeout: float = 20.0,
) -> str:
    """Translate ``text``, splitting overlong inputs at the 6000-char limit."""
    kwargs = dict(secret=secret, api_key=api_key, model_type=model_type, reference=reference)
    if len(text) <= _BAIDU_MAX_BYTES:
        return _baidu_translate_one(text, appid, timeout=timeout, **kwargs)
    segments: list[str] = []
    remaining = text
    while remaining:
        cut = _BAIDU_MAX_BYTES
        while cut > 0 and len(remaining[:cut]) > _BAIDU_MAX_BYTES:
            cut -= 256
        if cut <= 0:
            return ""
        segments.append(remaining[:cut])
        remaining = remaining[cut:]
    return "".join(
        _baidu_translate_one(segment, appid, timeout=timeout, **kwargs)
        for segment in segments
    )


def _pack_texts(texts: list[str]) -> list[list[str]]:
    """Pack short texts into request groups under the per-request char limit."""
    packs: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        if len(text) > _BAIDU_MAX_PACK:
            if current:
                packs.append(current)
                current = []
                size = 0
            packs.append([text])  # overlong text goes alone (segmented later)
            continue
        if current and size + len(text) + len(_BAIDU_SPLIT_JOIN) > _BAIDU_MAX_PACK:
            packs.append(current)
            current = []
            size = 0
        current.append(text)
        size += len(text) + len(_BAIDU_SPLIT_JOIN)
    if current:
        packs.append(current)
    return packs


def _request_baidu_batch(
    texts: list[str],
    appid: str,
    secret: str | None = None,
    api_key: str | None = None,
    model_type: str = "llm",
    reference: str = BAIDU_REFERENCE,
    pause: float = 1.2,
    timeout: float = 20.0,
) -> dict[str, str]:
    """Translate texts through the Baidu LLM API, packing several per request."""
    common = dict(secret=secret, api_key=api_key, model_type=model_type)
    result: dict[str, str] = {}
    for pack in _pack_texts(texts):
        if len(pack) == 1:
            try:
                translated = _baidu_translate_segmented(
                    pack[0], appid, reference=reference, timeout=timeout, **common
                )
            except Exception:
                translated = ""
            if translated:
                result[pack[0]] = translated
        else:
            reference_batch = reference + " " + _BAIDU_SPLIT_REFERENCE
            try:
                joined = _baidu_translate_one(
                    _BAIDU_SPLIT_JOIN.join(pack),
                    appid,
                    reference=reference_batch,
                    timeout=timeout,
                    **common,
                )
                parts = joined.split(_BAIDU_SPLIT_MARK)
            except Exception:
                parts = []
            if len(parts) == len(pack):
                for src, dst in zip(pack, parts):
                    if dst.strip():
                        result[src] = dst.strip()
            else:
                # The split broke — fall back to per-text requests.
                for single in pack:
                    try:
                        translated = _baidu_translate_segmented(
                            single, appid, reference=reference, timeout=timeout, **common
                        )
                    except Exception:
                        continue
                    if translated:
                        result[single] = translated
        time.sleep(pause)
    return result


def _backoff_sleep(pause: float, attempt: int, error: BaseException | None = None) -> None:
    """Sleep before a retry; rate-limit errors back off much longer."""
    if isinstance(error, BaiduRateError) or (
        isinstance(error, urllib.error.HTTPError) and error.code == 429
    ):
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
    api_key: str | None = None,
    model_type: str = "llm",
    reference: str = BAIDU_REFERENCE,
) -> tuple[dict[str, str], int]:
    """Translate ``texts``; returns ``(translated, failed_count)``.

    ``backend="google"`` uses the free Google endpoint with batched
    requests; ``backend="baidu"`` uses the Baidu LLM text translation API
    (``appid`` plus either a Bearer ``api_key`` or the ``secret`` for sign
    auth).  Transient errors — including rate limits — are retried with
    backoff, and callers resume from the persistent cache after any
    interruption.
    """
    translated: dict[str, str] = {}
    pending = [t for t in dict.fromkeys(texts) if t not in translated and t.strip()]
    if backend == "baidu":
        if not appid:
            raise ValueError("Baidu backend requires an appid")
        if not api_key and not secret:
            raise ValueError("Baidu backend requires an api_key or a secret")
        request_fn = lambda texts: _request_baidu_batch(
            texts, appid, secret=secret, api_key=api_key,
            model_type=model_type, reference=reference, pause=pause,
        )
        # The Baidu path packs several texts per request and paces itself,
        # so hand it the whole pending set at once.
        effective_batch = len(pending) if pending else 1
    else:
        request_fn = _request_google_batch
        effective_batch = batch_size
    for start in range(0, len(pending), effective_batch):
        chunk = pending[start : start + effective_batch]
        result, _ = _try_translate(chunk, attempts, pause, request_fn)
        translated.update(result)
        if effective_batch > 1:
            # A batch whose round-trip broke (e.g. the separator was mangled)
            # falls back to individual requests.
            for single in [t for t in chunk if t not in translated]:
                result, _ = _try_translate([single], attempts, pause, request_fn)
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
