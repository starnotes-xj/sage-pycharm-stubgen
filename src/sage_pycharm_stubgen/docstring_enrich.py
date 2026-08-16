"""Enrich generated ``.pyi`` stubs with docstrings and precise return types.

PyCharm's Quick Documentation (Ctrl+Q) reads the docstring *body* of a stub
function.  stubgen-pyx v0.2.18 does not produce useful ones:

- it drops docstrings entirely for many functions (e.g. ``_first_ngens``);
- when it keeps a docstring, it emits it as a *separate string statement
  after* the ``def ...: ...`` line, which PyCharm does not associate with
  the function at all;
- ``-> Any`` is used even where the Cython source declares a return type.

This module repairs those stubs from three sources, in priority order:

1. :mod:`sage_pycharm_stubgen.supplemental_docs` -- curated Chinese
   docstrings and verified return types for CTF-critical APIs; entries may
   also carry a ``declare`` signature to re-add members that stubgen-pyx
   dropped (such as private names skipped by ``include_private=False``);
2. docstrings extracted from the installed ``.pyx`` sources (a line-based
   extractor that understands ``def``/``cpdef``/``cdef`` and class nesting,
   unlike stubgen-pyx it never needs to fully parse the Cython grammar);
3. runtime reflection -- import the module in the live Sage environment and
   read ``inspect.getdoc``, which also recovers *inherited* docstrings.

The merge rewrites each stub function so that the docstring becomes the
function body (``def f() -> T:`` followed by an indented docstring), which
is the layout PyCharm's stub indexer expects.  Return annotations are
upgraded from ``Any`` when a curated override or a Cython-declared return
type is available.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .supplemental_docs import SUPPLEMENTAL_DOCS

_BUILTIN_NAMES = frozenset(
    {
        "Any",
        "None",
        "True",
        "False",
        "bool",
        "bytearray",
        "bytes",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "object",
        "set",
        "str",
        "tuple",
        "type",
    }
)

# Cython scalar return types expressed as stub annotations.  Cython class
# types are kept as-is and validated against the stub's imports later.
_CYTHON_RETURN_TYPE_MAP = {
    "bint": "bool",
    "int": "int",
    "long": "int",
    "float": "float",
    "double": "float",
    "str": "str",
    "bytes": "bytes",
    "void": "None",
    "object": "Any",
    "tuple": "tuple[Any, ...]",
    "list": "list[Any]",
    "dict": "dict[Any, Any]",
    "set": "set[Any]",
}


@dataclass
class SourceDocstring:
    """A docstring and optional Cython return type found in a ``.pyx``."""

    qualname: str
    literal: str  # raw literal text including quotes, valid Python syntax
    return_type: str | None = None  # Cython-declared type mapped to a stub annotation


@dataclass
class EnrichmentSummary:
    modules_processed: int = 0
    docstrings_attached: int = 0
    docstrings_moved: int = 0
    return_types_added: int = 0
    curated_applied: int = 0
    runtime_docs_added: int = 0
    declarations_added: int = 0
    runtime_import_failures: list[str] = field(default_factory=list)
    curated_unmatched: list[str] = field(default_factory=list)
    curated_stubless: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Source extraction
# --------------------------------------------------------------------------

_DEF_RE = re.compile(r"^(?P<indent>\s*)(?P<kind>cpdef|cdef|def)\s+(?P<rest>.+)$")
_CLASS_RE = re.compile(r"^(?P<indent>\s*)cdef\s+class\s+(\w+)\b.*$")
_STRING_START_RE = re.compile(
    r'^(?P<indent>\s*)(?P<prefix>[rubfRUBF]{0,2})("""|\'\'\'|"|\')'
)


def _extract_string_literal(
    lines: list[str], start: int
) -> tuple[str, int] | None:
    """Collect the literal beginning on ``lines[start]``.

    Returns ``(literal, end_index)`` -- the literal text including quotes
    and the index of the last line it occupies.  The raw text (including
    any ``r`` prefix) is preserved verbatim; ``None`` is returned when the
    line does not start a complete, evaluable Python string.
    """
    match = _STRING_START_RE.match(lines[start])
    if match is None:
        return None
    quote = match.group(3)
    text = lines[start][match.end() :]
    if len(quote) == 3:
        closing = text.find(quote)
        if closing >= 0:
            if closing != len(text) - len(quote):
                return None  # code after the closing quotes -- not a docstring
            literal = lines[start].strip()
            end = start
        else:
            for end in range(start + 1, len(lines)):
                if quote in lines[end]:
                    break
            else:
                return None
            literal = "\n".join(lines[start : end + 1]).strip()
    else:
        if quote not in text[1:]:
            return None
        literal = lines[start].strip()
        end = start
    try:
        ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return None
    return literal, end


def _parse_cython_signature(rest: str) -> tuple[str, str | None]:
    """Split a Cython ``def``/``cpdef``/``cdef`` header into name and type.

    ``def f(...)`` has no declared type; ``cpdef tuple f(...)`` and
    ``cdef int f(...)`` declare the return type in the first token.
    """
    head = rest.split("(", 1)[0]
    tokens = head.split()
    if len(tokens) >= 2:
        declared = tokens[0]
        if re.fullmatch(r"\w+(?:\.\w+)*", declared):
            return tokens[-1], _CYTHON_RETURN_TYPE_MAP.get(declared, declared)
    return (tokens[-1], None) if tokens else ("", None)


def _skip_blank_and_comments(lines: list[str], index: int) -> int:
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            return index
        index += 1
    return index


def extract_docstrings(source: Path) -> dict[str, SourceDocstring]:
    """Extract docstrings from a Cython ``.pyx`` file.

    Returns a mapping from qualified names (``Class.method`` or ``func``)
    to :class:`SourceDocstring`.  The literal text is kept verbatim (raw
    strings stay raw), so re-embedding it into a stub cannot corrupt
    escapes.  String regions are skipped entirely, so ``cdef class`` or
    ``def`` lines that only appear inside docstring examples cannot
    disturb the class stack.
    """
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}

    found: dict[str, SourceDocstring] = {}
    class_stack: list[tuple[int, str]] = []  # (indent, class name)
    index = 0
    while index < len(lines):
        line = lines[index]
        class_match = _CLASS_RE.match(line)
        if class_match:
            indent = len(class_match.group(1))
            name = class_match.group(2)
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            class_stack.append((indent, name))
            body = _skip_blank_and_comments(lines, index + 1)
            if body < len(lines):
                extracted = _extract_string_literal(lines, body)
                if extracted is not None:
                    literal, end = extracted
                    qualname = ".".join(item[1] for item in class_stack)
                    found[qualname] = SourceDocstring(qualname=qualname, literal=literal)
                    index = end
            index += 1
            continue

        def_match = _DEF_RE.match(line)
        if def_match:
            indent = len(def_match.group("indent"))
            kind = def_match.group("kind")
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            name, return_type = _parse_cython_signature(def_match.group("rest"))
            if not name.isidentifier():
                index += 1
                continue
            qualname = (
                ".".join(item[1] for item in class_stack) + "." + name
                if class_stack
                else name
            )
            body = _skip_blank_and_comments(lines, index + 1)
            if body < len(lines):
                extracted = _extract_string_literal(lines, body)
                if extracted is not None:
                    literal, end = extracted
                    found[qualname] = SourceDocstring(
                        qualname=qualname,
                        literal=literal,
                        return_type=return_type if kind in ("cpdef", "cdef") else None,
                    )
                    index = end
            index += 1
            continue
        index += 1
    return found


# --------------------------------------------------------------------------
# Runtime reflection
# --------------------------------------------------------------------------


class RuntimeDocProvider:
    """Import Sage modules and collect their live docstrings.

    Importing every module takes minutes, so each module is imported at most
    once, only the docstrings actually wanted by a stub are looked up, and
    failures are recorded instead of raised.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, str]] = {}
        self._failures: list[str] = []
        self._ready = False

    def _ensure_sage_ready(self) -> None:
        """Import ``sage.all`` once to resolve module import cycles.

        Some modules (``sage.rings.finite_rings.element_base`` among them)
        hit a partially initialized ``sage.rings.integer_ring`` when they
        are imported before the core rings; importing ``sage.all`` first
        settles the initialization order for everything that follows.
        """
        if self._ready:
            return
        try:
            import sage.all  # noqa: F401
        except Exception:  # noqa: BLE001 - proceed; failures are recorded per module
            pass
        self._ready = True

    def module_docs(self, module_name: str, wanted: set[str]) -> dict[str, str]:
        """Docstrings for ``module_name`` keyed by in-module qualified name."""
        if module_name in self._cache:
            return self._cache[module_name]
        self._ensure_sage_ready()
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - record and continue
            self._failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
            self._cache[module_name] = {}
            return {}

        docs: dict[str, str] = {}
        for key in wanted:
            parts = key.split(".")
            if len(parts) == 1:
                name = parts[0]
                try:
                    value = inspect.getattr_static(module, name)
                    doc = inspect.getdoc(value) if callable(value) else None
                except Exception:  # noqa: BLE001 - a broken member must not abort the module
                    continue
                if isinstance(value, property) or doc is None:
                    continue
            elif len(parts) == 2:
                class_name, method_name = parts
                try:
                    cls = inspect.getattr_static(module, class_name)
                except AttributeError:
                    continue
                if not inspect.isclass(cls):
                    continue
                method = None
                try:
                    method = inspect.getattr_static(cls, method_name)
                except AttributeError:
                    pass
                if method is None:
                    # Inherited members need a resolved lookup.
                    try:
                        method = getattr(cls, method_name)
                    except Exception:  # noqa: BLE001 - sage dynamic attrs can raise
                        continue
                try:
                    doc = inspect.getdoc(method) if callable(method) else None
                except Exception:  # noqa: BLE001 - Cython __doc__ access can raise
                    continue
                if isinstance(method, property) or doc is None:
                    continue
            else:
                continue  # nested classes are not covered by runtime docs
            docs[key] = doc
        self._cache[module_name] = docs
        return docs

    @property
    def failures(self) -> list[str]:
        return self._failures


# --------------------------------------------------------------------------
# Stub merging
# --------------------------------------------------------------------------

_DEF_HEAD_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(")


@dataclass
class _DefLine:
    """Positions of the interesting pieces of one ``def`` line."""

    indent: str
    close_paren: int  # index just past the ``)`` closing the parameter list
    arrow_start: int | None
    arrow_end: int | None
    colon: int
    body: str


def _split_def_line(line: str) -> _DefLine | None:
    """Locate the parameter list end, return arrow, colon and body.

    Parameter annotations and defaults may contain colons and ``->``
    (``Callable[[int], int]``, lambdas), so a plain regex split is not
    reliable; scan for the matching closing parenthesis instead.  The scan
    is string-aware so quotes inside defaults cannot terminate it early.
    Returns ``None`` (fail closed) when the line cannot be split reliably.
    """
    match = _DEF_HEAD_RE.match(line)
    if match is None:
        return None
    indent = match.group(1)
    depth = 0
    quote = ""  # current string delimiter ('' for none, ' or ")
    escaped = False
    index = match.end() - 1  # the '('
    while index < len(line):
        char = line[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    else:
        return None  # unbalanced line
    tail_match = re.match(
        r"^(?P<arrow>\s*->\s*[^:\n]+)?(?P<colon>:)(?P<body>[^\n]*)$",
        line[index + 1 :],
    )
    if tail_match is None:
        return None
    arrow_start = arrow_end = None
    if tail_match.group("arrow") is not None:
        arrow_start = index + 1 + tail_match.start("arrow")
        arrow_end = index + 1 + tail_match.end("arrow")
    return _DefLine(
        indent=indent,
        close_paren=index + 1,
        arrow_start=arrow_start,
        arrow_end=arrow_end,
        colon=index + 1 + tail_match.start("colon"),
        body=tail_match.group("body"),
    )


def _is_ellipsis_body(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and node.value.value is Ellipsis
    )


def _is_string_body(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _annotation_is_upgradable(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return True
    return isinstance(annotation, ast.Name) and annotation.id == "Any"


def _valid_annotation(annotation: str) -> bool:
    try:
        ast.parse(f"def _probe() -> {annotation}: ...\n")
    except SyntaxError:
        return False
    return True


def _annotation_needs_import(annotation: str, imported: set[str]) -> str | None:
    """Return an identifier in *annotation* neither builtin nor imported."""
    for node in ast.walk(ast.parse(f"def _probe() -> {annotation}: ...\n")):
        if isinstance(node, ast.Name):
            if node.id not in _BUILTIN_NAMES and node.id not in imported:
                return node.id
    return None


def _local_names(tree: ast.AST) -> set[str]:
    """Names declared in the stub itself (classes and functions).

    An annotation may legally reference them without an import — e.g. a
    method returning its own class — so the import check must not reject
    them.
    """
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }


def _collect_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _quote(doc: str) -> str:
    """Turn evaluated doc text into a stub-legal string literal.

    Multi-line text becomes a raw triple-quoted literal so LaTeX escapes
    (``\\frac`` and friends) survive verbatim; text that starts or ends
    with a double quote (or contains ``\"\"\"``) falls back to ``repr``,
    which escapes every backslash.
    """
    if "\n" not in doc:
        return repr(doc)
    if '"""' in doc or doc.startswith('"') or doc.endswith('"') or doc.endswith("\\"):
        return repr(doc)
    return 'r"""' + doc + '"""'


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST | None]:
    parents: dict[ast.AST, ast.AST | None] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    parents[tree] = None
    return parents


def _qualified_prefix(node: ast.AST, parents: dict[ast.AST, ast.AST | None]) -> str:
    chain: list[str] = []
    parent = parents.get(node)
    while isinstance(parent, ast.ClassDef):
        chain.append(parent.name)
        parent = parents.get(parent)
    return ".".join(reversed(chain))


def _declared_qualnames(tree: ast.AST) -> set[str]:
    parents = _parents(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            prefix = _qualified_prefix(node, parents)
            names.add(f"{prefix}.{node.name}" if prefix else node.name)
    return names


def _insert_imports(lines: list[str], tree: ast.AST, new_imports: list[str]) -> None:
    """Insert *new_imports* after the last top-level import statement.

    When the file has no imports, the lines go after the module docstring
    (if any) so the docstring stays the first statement.
    """
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, node.end_lineno or node.lineno)
    if last == 0 and tree.body and _is_string_body(tree.body[0]):
        last = tree.body[0].end_lineno or tree.body[0].lineno
    existing = {line.strip() for line in lines}
    pending = [line for line in new_imports if line.strip() not in existing]
    if not pending:
        return
    for offset, line in enumerate(pending):
        lines.insert(last + offset, line)


def _dangling_docstring(
    node: ast.stmt, parents: dict[ast.AST, ast.AST | None]
) -> ast.Expr | None:
    """The misplaced string statement following ``node`` in its block.

    Only a string immediately following *node* and directly followed by
    another def/class (or the end of the block) counts as its docstring;
    anything else is left alone.  Callers must not invoke this for defs
    that already carry an in-body docstring.
    """
    parent = parents.get(node)
    if not isinstance(parent, (ast.Module, ast.ClassDef)):
        return None
    siblings = parent.body
    position = siblings.index(node)
    if position + 1 >= len(siblings):
        return None
    candidate = siblings[position + 1]
    if not _is_string_body(candidate):
        return None
    if position + 2 < len(siblings) and not isinstance(
        siblings[position + 2], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    ):
        return None
    return candidate


def _apply_edits(lines: list[str], edits: list[tuple[int, int, list[str]]]) -> None:
    """Apply line-numbered edits from the bottom of the file upwards.

    Edits sharing a range are coalesced: a replacement wins over a
    deletion, and the last replacement wins, so overlapping edits can
    never delete a line that another edit just rebuilt.
    """
    grouped: dict[tuple[int, int], list[str]] = {}
    for start, end, replacement in edits:
        grouped.setdefault((start, end), []).append(replacement)
    for (start, end), replacements in sorted(grouped.items(), reverse=True):
        chosen: list[str] | None = None
        for replacement in replacements:
            if replacement:
                chosen = replacement
        lines[start - 1 : end] = chosen if chosen is not None else []


def _strip_inline_comment(segment: str) -> str:
    """The segment with a trailing ``#`` comment removed."""
    return re.split(r"\s+#", segment)[0].strip()


class _Planner:
    """Computes edits for one stub file against all doc sources."""

    def __init__(
        self,
        lines: list[str],
        tree: ast.AST,
        parents: dict[ast.AST, ast.AST | None],
        curated: dict[str, dict[str, Any]],
        source_docs: dict[str, SourceDocstring],
        runtime_docs: dict[str, str],
        imported: set[str],
        summary: EnrichmentSummary,
    ) -> None:
        self.lines = lines
        self.tree = tree
        self.parents = parents
        self.curated = curated
        self.source_docs = source_docs
        self.runtime_docs = runtime_docs
        self.imported = imported
        self.summary = summary
        self.edits: list[tuple[int, int, list[str]]] = []
        self.extra_imports: list[str] = []

    # -- doc selection ---------------------------------------------------

    def _curated_entry(self, qualname: str) -> dict[str, Any] | None:
        return self.curated.get(qualname)

    def _source_doc(self, qualname: str) -> str | None:
        entry = self.source_docs.get(qualname)
        if entry is None:
            return None
        try:
            return ast.literal_eval(entry.literal)
        except (SyntaxError, ValueError):
            return None

    def _best_doc(self, qualname: str) -> str | None:
        entry = self._curated_entry(qualname)
        if entry and entry.get("doc"):
            self.summary.curated_applied += 1
            return entry["doc"]
        source_doc = self._source_doc(qualname)
        if source_doc is not None:
            return source_doc
        runtime_doc = self.runtime_docs.get(qualname)
        if runtime_doc:
            self.summary.runtime_docs_added += 1
            return runtime_doc
        return None

    def _best_return(self, qualname: str) -> str | None:
        """A curated or Cython-declared return type valid in this stub."""
        entry = self._curated_entry(qualname)
        candidate = None
        if entry and entry.get("return"):
            candidate = entry["return"]
        else:
            source_entry = self.source_docs.get(qualname)
            if (
                source_entry
                and source_entry.return_type
                and source_entry.return_type != "Any"
            ):
                candidate = source_entry.return_type
        if candidate is None or not _valid_annotation(candidate):
            return None
        # Names imported by the curated entry itself count as known; the
        # imports are inserted into the stub together with the annotation.
        # Names declared in the stub itself (a method returning its own
        # class) are known as well.
        known = self.imported | _local_names(self.tree)
        if entry:
            for line in entry.get("imports", []):
                match = re.match(r"^from\s+\S+\s+import\s+([^#]+)", line.strip())
                if match:
                    for part in match.group(1).split(","):
                        alias = part.strip().split(" as ")[-1].strip()
                        known = known | {alias}
        if _annotation_needs_import(candidate, known) is not None:
            return None
        if entry:
            self.extra_imports.extend(
                line for line in entry.get("imports", []) if line not in self.extra_imports
            )
        return candidate

    # -- edit computation --------------------------------------------------

    def plan_function(self, node: ast.FunctionDef, qualname: str) -> None:
        if any(
            isinstance(dec, ast.Name) and dec.id == "overload"
            for dec in node.decorator_list
        ):
            return  # overloads share one docstring on the implementation

        has_docstring = len(node.body) == 1 and _is_string_body(node.body[0])
        curated = self._curated_entry(qualname)
        # A def that already carries its docstring never "owns" a following
        # string -- that string may be a displaced class docstring, and
        # deleting or replacing it would destroy unrelated documentation.
        dangling = None if has_docstring else _dangling_docstring(node, self.parents)

        doc = None
        if curated and curated.get("doc"):
            doc = curated["doc"]
            self.summary.curated_applied += 1
        elif not has_docstring:
            doc = self._best_doc(qualname)
        if dangling is not None and doc is None:
            try:
                doc = ast.literal_eval(dangling.value)
                self.summary.docstrings_moved += 1
            except (SyntaxError, ValueError):
                doc = None

        new_return = None
        if _annotation_is_upgradable(node.returns):
            new_return = self._best_return(qualname)

        if doc is None and new_return is None and dangling is None:
            return

        def_line = self.lines[node.lineno - 1]
        parsed = _split_def_line(def_line)
        if parsed is None:
            return  # unconventional formatting; leave the def untouched

        # Remove old body lines (an in-body docstring being replaced by a
        # curated one, or a `...` on its own line below the def line).
        body_removals: list[tuple[int, int]] = []
        if has_docstring and doc is not None:
            string_node = node.body[0]
            if string_node.lineno > node.lineno:
                body_removals.append(
                    (string_node.lineno, string_node.end_lineno or string_node.lineno)
                )
        body_clean = _strip_inline_comment(parsed.body)
        if not body_clean and doc is not None:
            next_line_no = node.lineno + 1
            if next_line_no <= len(self.lines) and self.lines[
                next_line_no - 1
            ].strip() in ("...", "pass"):
                body_removals.append((next_line_no, next_line_no))
        if dangling is not None and doc is not None:
            # The dangling string only disappears when its text is reused
            # as the docstring; a doc chosen from another source leaves it
            # untouched (it may be a displaced class docstring).
            self.edits.append((dangling.lineno, dangling.end_lineno or dangling.lineno, []))

        # Rebuild the def line: cut the inline body, apply the return type.
        cut_at = parsed.colon + 1
        if body_clean in ("...", "pass"):
            cut_at = parsed.colon + 1 + parsed.body.index(body_clean)
        header = def_line[:cut_at].rstrip()
        if new_return is not None:
            if parsed.arrow_start is not None:
                header = (
                    def_line[: parsed.arrow_start]
                    + f" -> {new_return}"
                    + def_line[parsed.arrow_end : cut_at].rstrip()
                )
            else:
                header = (
                    def_line[: parsed.colon]
                    + f" -> {new_return}"
                    + def_line[parsed.colon : cut_at].rstrip()
                )
            self.summary.return_types_added += 1

        new_lines = [header]
        if doc is not None:
            new_lines.append(parsed.indent + "    " + _quote(doc))
            self.summary.docstrings_attached += 1
        elif not body_clean:
            new_lines.append(parsed.indent + "    ...")
        else:
            # The def carried an inline ``...`` / ``pass`` body; the header
            # rewrite cut it off, so keep it after the new header — a return
            # upgrade without a docstring must still produce a valid stub.
            new_lines[0] = f"{header} {body_clean}".rstrip()
        self.edits.extend((start, end, []) for start, end in body_removals)
        self.edits.append((node.lineno, node.lineno, new_lines))

    def plan_class(self, node: ast.ClassDef, qualname: str) -> None:
        if any(_is_string_body(item) for item in node.body[:1]):
            return  # class already documented
        dangling = _dangling_docstring(node, self.parents)
        doc = self._best_doc(qualname)
        if doc is None and dangling is None:
            return
        if dangling is not None and doc is None:
            try:
                doc = ast.literal_eval(dangling.value)
                self.summary.docstrings_moved += 1
            except (SyntaxError, ValueError):
                doc = None
        if dangling is not None and doc is not None:
            self.edits.append((dangling.lineno, dangling.end_lineno or dangling.lineno, []))
        if doc is None:
            return
        class_line = self.lines[node.lineno - 1]
        match = re.match(
            r"^(?P<indent>\s*)class\s+(?P<name>\w+)(?P<tail>[^:\n]*):(?P<body>[^\n]*)$",
            class_line,
        )
        if match is None:
            return
        indent = match.group("indent")
        # A one-line class body (`class X: ...`) must be cut so the
        # docstring can become the first statement of the suite.
        header = class_line[: match.start("body")].rstrip()
        self.edits.append(
            (node.lineno, node.lineno, [header, indent + "    " + _quote(doc)])
        )
        self.summary.docstrings_attached += 1

    def finish(self) -> None:
        _apply_edits(self.lines, self.edits)
        if self.extra_imports:
            try:
                _insert_imports(self.lines, self.tree, self.extra_imports)
            except Exception:  # noqa: BLE001 - imports are cosmetic; keep going
                pass


def _apply_declarations(
    lines: list[str], tree: ast.AST, curated: dict[str, dict[str, Any]]
) -> tuple[int, list[str]]:
    """Append curated ``declare`` signatures to their target classes.

    Returns ``(inserted_count, extra_imports)``.  Declarations re-add
    members that stubgen-pyx dropped (for example private methods skipped
    by ``include_private=False``); they are appended at the end of the
    class body so an in-body class docstring is never displaced.
    """
    declared = _declared_qualnames(tree)
    pending_class: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    pending_module: list[tuple[str, dict[str, Any]]] = []
    for qualname, entry in curated.items():
        declaration = entry.get("declare")
        if not declaration or not isinstance(declaration, str):
            continue
        if qualname in declared:
            continue
        parts = qualname.split(".")
        if len(parts) == 2:
            pending_class.setdefault(parts[0], []).append((declaration, entry))
        elif len(parts) == 1:
            pending_module.append((declaration, entry))

    def normalize(declaration: str) -> str:
        """Ensure the declaration is a complete one-line def."""
        declaration = declaration.strip()
        if declaration.endswith("..."):
            return declaration
        if declaration.endswith(":"):
            return declaration + " ..."
        return declaration + ": ..."

    inserted = 0
    extra_imports: list[str] = []
    if pending_class:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for declaration, entry in pending_class.pop(node.name, []):
                if not node.body:
                    continue
                anchor = node.body[-1].end_lineno or node.body[-1].lineno
                indent = " " * (node.col_offset + 4)
                lines.insert(anchor, indent + normalize(declaration))
                extra_imports.extend(entry.get("imports", []))
                inserted += 1
    if pending_module:
        for declaration, entry in pending_module:
            lines.append(normalize(declaration))
            extra_imports.extend(entry.get("imports", []))
            inserted += 1
    return inserted, extra_imports


def enrich_stub_file(
    stub: Path,
    source: Path | None,
    curated: dict[str, dict[str, Any]],
    runtime: RuntimeDocProvider | None,
    module_name: str,
    summary: EnrichmentSummary,
) -> None:
    """Rewrite one ``.pyi`` file in place with docstrings and return types."""
    try:
        original = stub.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return
    newline = "\r\n" if "\r\n" in original else "\n"
    try:
        tree = ast.parse(original)
    except SyntaxError:
        return
    lines = original.splitlines()

    inserted, declare_imports = _apply_declarations(lines, tree, curated)
    if inserted:
        summary.declarations_added += inserted
        try:
            tree = ast.parse("\n".join(lines))
        except SyntaxError:
            return

    parents = _parents(tree)
    imported = _collect_imports(tree)
    source_docs = extract_docstrings(source) if source is not None else {}
    wanted_runtime: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            prefix = _qualified_prefix(node, parents)
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            if qualname not in curated and qualname not in source_docs:
                if not (len(node.body) == 1 and _is_string_body(node.body[0])):
                    wanted_runtime.add(qualname)
        elif isinstance(node, ast.ClassDef) and not isinstance(
            parents.get(node), ast.ClassDef
        ):
            if node.name not in curated and node.name not in source_docs:
                if not any(_is_string_body(item) for item in node.body[:1]):
                    wanted_runtime.add(node.name)
    runtime_docs: dict[str, str] = {}
    if runtime is not None and wanted_runtime:
        runtime_docs = runtime.module_docs(module_name, wanted_runtime)

    planner = _Planner(
        lines, tree, parents, curated, source_docs, runtime_docs, imported, summary
    )
    planner.extra_imports.extend(declare_imports)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            prefix = _qualified_prefix(node, parents)
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            planner.plan_function(node, qualname)
        elif isinstance(node, ast.ClassDef) and not isinstance(
            parents.get(node), ast.ClassDef
        ):
            planner.plan_class(node, node.name)

    if not planner.edits and not planner.extra_imports:
        return
    planner.finish()
    stub.write_text(newline.join(lines) + newline, encoding="utf-8")
    summary.modules_processed += 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def stub_module_name(output_root: Path, stub: Path) -> str | None:
    """Module name for a generated stub, or None for non-module files."""
    relative = stub.relative_to(output_root)
    if relative.name in {"all.pyi", "all_cmdline.pyi", "__init__.pyi"}:
        return None
    parts = list(relative.with_suffix("").parts)
    if not parts or parts[0] != "sage":
        return None
    return ".".join(parts)


def _unmatched_curated_entries(output_root: Path) -> tuple[list[str], list[str]]:
    """Split curated entries into (unmatched, stubless).

    *stubless* are entries whose target module produces no stub file at all
    (pure-Python Sage modules -- PyCharm reads their source docstrings
    directly, so nothing is lost).  *unmatched* are entries whose stub
    exists but does not declare the symbol: a real gap.
    """
    unmatched: list[str] = []
    stubless: list[str] = []
    for module, entries in SUPPLEMENTAL_DOCS.items():
        parts = module.split(".")
        if len(parts) < 2 or parts[0] != "sage":
            continue
        stub = output_root.joinpath(*parts).with_suffix(".pyi")
        if not stub.is_file():
            stubless.append(module)
            continue
        try:
            tree = ast.parse(stub.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        declared = _declared_qualnames(tree)
        for qualname in entries:
            if qualname not in declared:
                unmatched.append(f"{module}.{qualname}")
    return sorted(unmatched), sorted(stubless)


def enrich_stubs(
    output_root: Path,
    *,
    sage_package: Path,
    use_runtime: bool = True,
) -> EnrichmentSummary:
    """Enrich every generated stub under ``output_root/sage``.

    ``sage_package`` must be the installed Sage package directory; the
    sibling ``.pyx`` sources provide the extracted docstrings.
    """
    summary = EnrichmentSummary()
    runtime = RuntimeDocProvider() if use_runtime else None
    for stub in sorted((output_root / "sage").rglob("*.pyi")):
        module_name = stub_module_name(output_root, stub)
        if module_name is None:
            continue
        relative = stub.relative_to(output_root / "sage").with_suffix(".pyx")
        source = sage_package / relative
        if not source.is_file():
            source = None
        curated = SUPPLEMENTAL_DOCS.get(module_name, {})
        enrich_stub_file(stub, source, curated, runtime, module_name, summary)
    if runtime is not None:
        summary.runtime_import_failures = runtime.failures
    summary.curated_unmatched, summary.curated_stubless = _unmatched_curated_entries(
        output_root
    )
    return summary
