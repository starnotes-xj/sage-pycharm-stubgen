#!/usr/bin/env python3
"""Generate a deterministic, versioned Sage API index from .pyi/.py sources."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
GENERATOR_VERSION = "sage-api-index-py/0.1"
INVENTORY_SCHEMA_VERSION = 1
RETURN_EVIDENCE_MANIFEST_SCHEMA_VERSION = 1
RETURN_EVIDENCE_KIND = "TRUSTED_MANIFEST"
KIND_ORDER = {name: index for index, name in enumerate(("MODULE", "CLASS", "FUNCTION", "METHOD", "PROPERTY", "CONSTANT", "ALIAS"))}
CONFIDENCE_RANK = {"UNKNOWN": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4}

@dataclass(frozen=True)
class SourceRef:
    kind: str
    locator: str
    digest: str
    def json(self) -> dict[str, str]:
        return {"kind": self.kind, "locator": self.locator, "digest": self.digest}

@dataclass
class RawSymbol:
    qualified_name: str
    kind: str
    source: SourceRef
    signatures: list[dict[str, Any]] = field(default_factory=list)
    declaration_role: str = "ORDINARY"
    value_type: dict[str, Any] | None = None
    parents: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    documentation: dict[str, Any] | None = None
    dynamicity: str = "STATIC"
    confidence: str = "UNKNOWN"
    def key(self) -> tuple[str, str]:
        return self.qualified_name, self.kind

@dataclass(frozen=True)
class SourceSpec:
    root: Path
    kind: str
    locator: str
    module_prefix: str = ""

class AstExtractor:
    def __init__(self, source: SourceSpec) -> None:
        self.source_root = source.root.resolve()
        self.source_kind = source.kind
        self.source_locator = source.locator.strip("/")
        self.module_prefix = source.module_prefix.strip(".")

    def extract_path(self, path: Path) -> list[RawSymbol]:
        path = path.resolve()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path), type_comments=True)
        module = self.module_name(path)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        relative = path.relative_to(self.source_root).as_posix()
        locator = "/".join(part for part in (self.source_locator, relative) if part)
        source = SourceRef(self.source_kind, locator, digest)
        imports = self.collect_imports(tree)
        imports.update({node.name: f"{module}.{node.name}" for node in tree.body if isinstance(node, ast.ClassDef)})
        type_parameters = self.collect_type_parameters(tree, imports)
        roles = declaration_roles(tree.body, imports)
        symbols: list[RawSymbol] = [RawSymbol(module, "MODULE", source, confidence="HIGH")]
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(self.function_symbol(node, module, None, imports, source, "FUNCTION", roles.get(id(node)), type_parameters))
            else:
                symbols.extend(self.extract_top_level(node, module, imports, source, type_parameters))
        return sorted(symbols, key=lambda item: (item.qualified_name, KIND_ORDER[item.kind], item.source.locator))

    def module_name(self, path: Path) -> str:
        parts = list(path.relative_to(self.source_root).parts)
        filename = Path(parts.pop())
        if filename.stem == "__init__":
            value = ".".join(parts)
        else:
            value = ".".join(parts + [filename.stem])
        if self.module_prefix and value and not (value == self.module_prefix or value.startswith(self.module_prefix + ".")):
            value = self.module_prefix + "." + value
        return value or self.module_prefix or "__root__"

    @staticmethod
    def collect_imports(tree: ast.Module) -> dict[str, str]:
        imports: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    if alias.name != "*":
                        imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = alias.name
        return imports

    def extract_top_level(self, node: ast.AST, module: str, imports: dict[str, str], source: SourceRef, type_parameters: dict[str, dict[str, Any]] | None = None) -> list[RawSymbol]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return self.extract_aliases(node, module, source)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return [self.function_symbol(node, module, None, imports, source, "FUNCTION")]
        if isinstance(node, ast.ClassDef):
            return self.class_symbols(node, module, imports, source, type_parameters)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [RawSymbol(f"{module}.{node.target.id}", "CONSTANT", source_at(source, node.lineno), value_type=type_ref(node.annotation, imports), documentation=doc(node), confidence="MEDIUM")]
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name) and target.id != "__all__"]
            return [RawSymbol(f"{module}.{name}", "CONSTANT", source_at(source, node.lineno), confidence="LOW") for name in names]
        return []

    @staticmethod
    def extract_aliases(node: ast.AST, module: str, source: SourceRef) -> list[RawSymbol]:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            return []
        return [RawSymbol(f"{module}.{alias.asname or alias.name}", "ALIAS", source_at(source, node.lineno), aliases=[f"{node.module}.{alias.name}"], confidence="HIGH") for alias in node.names if alias.name != "*"]

    @staticmethod
    def collect_type_parameters(tree: ast.Module, imports: dict[str, str]) -> dict[str, dict[str, Any]]:
        declarations: dict[str, dict[str, Any]] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            constructor = annotation_text(value.func, imports)
            if constructor not in {"TypeVar", "typing.TypeVar", "typing_extensions.TypeVar"}:
                continue
            kind = "TYPE_VARIABLE"
            declaration: dict[str, Any] = {"name": node.targets[0].id, "kind": kind}
            named = {
                keyword.arg: keyword.value
                for keyword in value.keywords
                if keyword.arg is not None
            }
            if "bound" in named:
                bound = type_ref(named["bound"], imports)
                if bound is not None:
                    declaration["bound"] = bound
            constraints = [type_ref(argument, imports) for argument in value.args[1:]]
            declaration["constraints"] = [item for item in constraints if item is not None]
            declarations[node.targets[0].id] = declaration
        return declarations

    def class_symbols(self, node: ast.ClassDef, module: str, imports: dict[str, str], source: SourceRef, type_parameters: dict[str, dict[str, Any]] | None = None) -> list[RawSymbol]:
        qualified = f"{module}.{node.name}"
        result = [RawSymbol(qualified, "CLASS", source_at(source, node.lineno), parents=[annotation_text(base, imports) for base in node.bases if annotation_text(base, imports)], documentation=doc(node), confidence="HIGH")]
        roles = declaration_roles(node.body, imports)
        declared_members = {
            member.name: self.function_symbol(
                member,
                module,
                node.name,
                imports,
                source,
                "PROPERTY" if any(isinstance(item, ast.Name) and item.id == "property" for item in member.decorator_list) else "METHOD",
                roles.get(id(member)),
                type_parameters or {},
            )
            for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for member in node.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                result.append(declared_members[member.name])
            elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                result.append(RawSymbol(f"{qualified}.{member.target.id}", "PROPERTY", source_at(source, member.lineno), value_type=type_ref(member.annotation, imports), documentation=doc(member), confidence="MEDIUM"))
            elif isinstance(member, ast.Assign) and isinstance(member.value, ast.Name):
                target = declared_members.get(member.value.id)
                if target is None:
                    continue
                for assigned in member.targets:
                    if not isinstance(assigned, ast.Name):
                        continue
                    result.append(RawSymbol(
                        f"{qualified}.{assigned.id}",
                        target.kind,
                        source_at(source, member.lineno),
                        signatures=target.signatures,
                        declaration_role=target.declaration_role,
                        aliases=[target.qualified_name],
                        documentation=target.documentation,
                        dynamicity=target.dynamicity,
                        confidence=target.confidence,
                    ))
        return result

    def function_symbol(self, node: ast.FunctionDef | ast.AsyncFunctionDef, module: str, owner: str | None, imports: dict[str, str], source: SourceRef, kind: str, role: str | None = None, type_parameters: dict[str, dict[str, Any]] | None = None) -> RawSymbol:
        qualified = ".".join(part for part in (module, owner, node.name) if part)
        signature = signature_for(node, imports, owner is not None, type_parameters or {})
        declaration_role_value = role or declaration_role(node, imports)
        return RawSymbol(qualified, kind, source_at(source, node.lineno), signatures=[signature], declaration_role=declaration_role_value, documentation=doc(node), confidence="HIGH")

def source_at(source: SourceRef, line: int) -> SourceRef:
    return SourceRef(source.kind, f"{source.locator}:{line}", source.digest)

def doc(node: ast.AST) -> dict[str, Any] | None:
    if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    value = ast.get_docstring(node, clean=False)
    if not value:
        return None
    lines = value.strip().splitlines()
    return {"summary": lines[0].strip() if lines else None, "body": value.strip(), "examples": [line.strip() for line in lines if line.strip().startswith(">>>")]}

def annotation_text(node: ast.AST | None, imports: dict[str, str]) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = annotation_text(node.value, imports)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        base = annotation_text(node.value, imports) or ""
        values = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        return f"{base}[{', '.join(filter(None, (annotation_text(item, imports) for item in values)))}]"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{annotation_text(node.left, imports)} | {annotation_text(node.right, imports)}"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return imports.get(node.value, node.value)
    try:
        return ast.unparse(node)
    except Exception:
        return None

def type_ref(node: ast.AST | None, imports: dict[str, str]) -> dict[str, Any] | None:
    expression = annotation_text(node, imports)
    if not expression:
        return None
    if expression == "Any" or expression.endswith(".Any"):
        return {"state": "DYNAMIC", "expression": None}
    return {"state": "KNOWN", "expression": expression}

def default_text(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None

def decorator_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return decorator_name(node.func)
    return None

def declaration_role(node: ast.FunctionDef | ast.AsyncFunctionDef, imports: dict[str, str]) -> str:
    overload_names = {"overload", "typing.overload", "typing_extensions.overload"}
    return "OVERLOAD" if any((imports.get(name) or name) in overload_names for name in (decorator_name(item) or "" for item in node.decorator_list)) else "ORDINARY"

def declaration_roles(nodes: list[ast.stmt], imports: dict[str, str]) -> dict[int, str]:
    roles: dict[int, str] = {}
    overload_names = {
        node.name
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and declaration_role(node, imports) == "OVERLOAD"
    }
    seen_overload: set[str] = set()
    for node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        role = declaration_role(node, imports)
        if role == "OVERLOAD":
            seen_overload.add(node.name)
        elif node.name in overload_names and node.name in seen_overload:
            role = "IMPLEMENTATION"
        roles[id(node)] = role
    return roles


def signature_for(node: ast.FunctionDef | ast.AsyncFunctionDef, imports: dict[str, str], is_method: bool = False, module_type_parameters: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    positional_only_names = {argument.arg for argument in args.posonlyargs}
    parameters = [parameter_for(argument, default, imports, False, False, positional_only=argument.arg in positional_only_names) for argument, default in zip(positional, defaults) if not (is_method and argument.arg in {"self", "cls"})]
    if args.vararg:
        parameters.append(parameter_for(args.vararg, None, imports, False, True))
    parameters.extend(parameter_for(argument, default, imports, True, False) for argument, default in zip(args.kwonlyargs, args.kw_defaults))
    if args.kwarg:
        parameters.append(parameter_for(args.kwarg, None, imports, True, True))
    type_parameters = explicit_type_parameters(node, imports, module_type_parameters or {})
    return {
        "parameters": parameters,
        "returnType": type_ref(node.returns, imports) or {"state": "UNKNOWN", "expression": None},
        **({"typeParameters": type_parameters} if type_parameters else {}),
    }

def parameter_for(argument: ast.arg, default: ast.AST | None, imports: dict[str, str], keyword_only: bool, variadic: bool, positional_only: bool = False) -> dict[str, Any]:
    parameter = {"name": argument.arg, "type": type_ref(argument.annotation, imports) or {"state": "UNKNOWN", "expression": None}, "defaultValue": default_text(default), "optional": default is not None, "keywordOnly": keyword_only, "variadic": variadic}
    if positional_only:
        parameter["positionalOnly"] = True
    return parameter


def explicit_type_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef, imports: dict[str, str], module_type_parameters: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Emit only declarations explicitly present in the function signature."""
    declarations: list[dict[str, Any]] = []
    annotation_nodes = [argument.annotation for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
    annotation_nodes.append(node.returns)
    explicit_names: set[str] = set()
    declared = module_type_parameters or {}

    def referenced_names(annotation: ast.AST | None) -> set[str]:
        if annotation is None:
            return set()
        return {
            item.id
            for item in ast.walk(annotation)
            if isinstance(item, ast.Name) and item.id in declared
        }

    for annotation in annotation_nodes:
        explicit_names.update(referenced_names(annotation))
    for annotation in annotation_nodes:
        if isinstance(annotation, ast.Name) and annotation.id in {"Self", "typing_Self"}:
            explicit_names.add("Self")
        elif isinstance(annotation, ast.Attribute) and annotation.attr == "Self":
            explicit_names.add("Self")
    if "Self" in explicit_names:
        declarations.append({"name": "Self", "kind": "SELF"})
    for name in declared:
        if name in explicit_names:
            declarations.append(declared[name])
    return declarations


def signature_key(signature: dict[str, Any]) -> str:
    return json.dumps(signature, sort_keys=True, separators=(",", ":"))

def canonical_signature_shape(signature: dict[str, Any]) -> dict[str, Any]:
    return {
        "parameters": [
            {key: parameter.get(key) for key in ("name", "type", "defaultValue", "optional", "keywordOnly", "variadic", "positionalOnly")}
            for parameter in signature.get("parameters", [])
        ],
        "typeParameters": signature.get("typeParameters", []),
    }

def signature_shape_digest(signature: dict[str, Any]) -> str:
    payload = json.dumps(canonical_signature_shape(signature), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def source_digests_digest(source_digest_map: dict[str, str]) -> str:
    payload = json.dumps(dict(sorted(source_digest_map.items())), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def parse_return_evidence_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("return evidence manifest must be a JSON object")
    if value.get("schemaVersion") != RETURN_EVIDENCE_MANIFEST_SCHEMA_VERSION:
        raise ValueError("return evidence manifest has unsupported schemaVersion")
    for field in ("artifactId", "sageVersion", "pythonVersion", "sourceDigestsDigest"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"return evidence manifest requires nonblank {field}")
    if not is_sha256(value["sourceDigestsDigest"]):
        raise ValueError("return evidence manifest sourceDigestsDigest is invalid")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ValueError("return evidence manifest entries must be an array")
    return value

def validate_return_evidence_manifest(manifest: dict[str, Any], index: dict[str, Any]) -> None:
    if manifest["sageVersion"] != index.get("sageVersion") or manifest["pythonVersion"] != index.get("pythonVersion"):
        raise ValueError("return evidence manifest version does not match index")
    if manifest["artifactId"] != index.get("artifactId"):
        raise ValueError("return evidence manifest artifactId does not match index")
    if manifest["sourceDigestsDigest"] != source_digests_digest(index.get("sourceDigests", {})):
        raise ValueError("return evidence manifest sourceDigestsDigest does not match index")
    index_entries = {(entry.get("qualifiedName"), entry.get("kind")): entry for entry in index.get("entries", [])}
    seen: set[tuple[str, str, str]] = set()
    for position, item in enumerate(manifest["entries"]):
        path = f"entries[{position}]"
        if not isinstance(item, dict):
            raise ValueError(f"return evidence manifest {path} must be an object")
        qualified_name, kind, digest = item.get("qualifiedName"), item.get("kind"), item.get("signatureDigest")
        if not isinstance(qualified_name, str) or not qualified_name.strip() or kind not in KIND_ORDER or not isinstance(digest, str) or not is_sha256(digest):
            raise ValueError(f"return evidence manifest {path} identity is invalid")
        key = (qualified_name, kind, digest)
        if key in seen:
            raise ValueError(f"return evidence manifest {path} is duplicated")
        seen.add(key)
        entry = index_entries.get((qualified_name, kind))
        if entry is None:
            raise ValueError(f"return evidence manifest {path} targets an unknown entry")
        if kind != "METHOD":
            raise ValueError(f"return evidence manifest {path} target kind must be METHOD")
        if entry.get("dynamicity") == "DYNAMIC":
            raise ValueError(f"return evidence manifest {path} targets a dynamic entry")
        matches = [signature for signature in entry.get("signatures", []) if signature_shape_digest(signature) == digest]
        if len(matches) != 1:
            raise ValueError(f"return evidence manifest {path} signatureDigest does not match exactly one signature")
        return_type = item.get("returnType")
        if not isinstance(return_type, dict) or return_type.get("state") != "KNOWN" or not isinstance(return_type.get("expression"), str) or not return_type["expression"].strip():
            raise ValueError(f"return evidence manifest {path}.returnType must be KNOWN with an expression")
        base_return = matches[0].get("returnType", {})
        if base_return.get("state") == "DYNAMIC" or (base_return.get("state") == "KNOWN" and base_return.get("expression") != return_type["expression"]):
            raise ValueError(f"return evidence manifest {path} contradicts indexed return metadata")
        source = item.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("locator"), str) or not source["locator"].strip() or not is_sha256(source.get("digest", "")):
            raise ValueError(f"return evidence manifest {path}.source is invalid")

def attach_trusted_return_evidence(index: dict[str, Any], manifest: dict[str, Any], manifest_path: Path) -> None:
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    entries = {(entry["qualifiedName"], entry["kind"]): entry for entry in index["entries"]}
    for item in manifest["entries"]:
        entry = entries[(item["qualifiedName"], item["kind"])]
        signature = next(signature for signature in entry["signatures"] if signature_shape_digest(signature) == item["signatureDigest"])
        evidence = {
            "kind": RETURN_EVIDENCE_KIND,
            "returnType": item["returnType"],
            "source": {"kind": "SIGNATURE", "locator": item["source"]["locator"], "digest": manifest_digest},
        }
        signature["trustedReturnEvidence"] = signature.get("trustedReturnEvidence", []) + [evidence]

def normalize(raw_symbols: Iterable[RawSymbol], sage_version: str, python_version: str, metadata: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:

    raw_list = list(raw_symbols)
    grouped: dict[tuple[str, str], list[RawSymbol]] = {}
    imported_aliases: dict[str, set[str]] = {}
    for symbol in raw_list:
        grouped.setdefault(symbol.key(), []).append(symbol)
        if symbol.kind == "ALIAS":
            for target in symbol.aliases:
                imported_aliases.setdefault(target, set()).add(symbol.qualified_name)
    entries: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda item: (item[0], KIND_ORDER[item[1]])):
        candidates = sorted(grouped[key], key=lambda item: (item.source.kind, item.source.locator))
        signatures, conflict = merge_candidate_signatures(candidates)
        if len(candidates) > 1:
            diagnostic = {"kind": "CONFLICT" if conflict else "DUPLICATE", "qualifiedName": key[0], "message": "Conflicting declarations were merged as dynamic" if conflict else "Duplicate declarations were merged", "sources": [candidate.source.json() for candidate in candidates]}
            if conflict:
                diagnostic.update(conflict_metadata(candidates, signatures))
            diagnostics.append(diagnostic)
        if conflict:
            signatures = [{"parameters": [], "returnType": {"state": "DYNAMIC", "expression": None}}]
        dynamicity = "DYNAMIC" if conflict or any(item.dynamicity == "DYNAMIC" for item in candidates) else ("UNKNOWN" if any(item.dynamicity == "UNKNOWN" for item in candidates) else "STATIC")
        confidence = max((item.confidence for item in candidates), key=lambda item: CONFIDENCE_RANK[item])
        sources = []
        seen_sources: set[tuple[str, str]] = set()
        for candidate in candidates:
            marker = (candidate.source.kind, candidate.source.locator)
            if marker not in seen_sources:
                seen_sources.add(marker)
                sources.append(candidate.source.json())
        aliases = {alias for item in candidates for alias in item.aliases}
        aliases.update(imported_aliases.get(key[0], set()))
        entry: dict[str, Any] = {"qualifiedName": key[0], "kind": key[1], "dynamicity": dynamicity, "confidence": confidence, "parents": sorted({parent for item in candidates for parent in item.parents}), "protocols": sorted({protocol for item in candidates for protocol in item.protocols}), "aliases": sorted(aliases), "sources": sources, "signatures": signatures}
        values = [item.value_type for item in candidates if item.value_type is not None]
        if values:
            entry["valueType"] = values[0]
        documents = [item.documentation for item in candidates if item.documentation is not None]
        if documents:
            entry["documentation"] = documents[0]
        entries.append(entry)
    index = {"schemaVersion": SCHEMA_VERSION, "sageVersion": sage_version, "pythonVersion": python_version, "generatorVersion": GENERATOR_VERSION, "sourceDigests": source_digests(entries), "entries": entries}
    if metadata:
        if metadata.get("artifactId"):
            index["artifactId"] = metadata["artifactId"]
        index = {**metadata, **index}
        index["sources"] = []
        for source in metadata.get("sourceSpecs", []):
            source_files = [f"{source['locator'].rstrip('/')}/{path}" for path in source.get("files", [])]
            source_data = {"kind": source["kind"], "locator": source["locator"], "fileCount": source.get("fileCount", len(source_files)), "files": source_files}
            if source.get("treeDigest"):
                source_data["treeDigest"] = source["treeDigest"]
            index["sources"].append(source_data)
    return index, sorted(diagnostics, key=lambda item: (item["qualifiedName"], item["kind"]))

def conflict_metadata(candidates: list[RawSymbol], signatures: list[dict[str, Any]]) -> dict[str, Any]:
    digests = sorted({candidate.source.digest for candidate in candidates})
    return {
        "sourceDigest": digests[0] if len(digests) == 1 else None,
        "sourceDigests": digests,
        "declarationCount": len(candidates),
        "distinctSignatureCount": len({signature_key(signature) for signature in signatures}),
        "signatureKeys": sorted(signature_key(signature) for signature in signatures),
    }

def conflicting_signatures(signatures: list[dict[str, Any]]) -> bool:
    groups: dict[tuple[str, ...], set[str]] = {}
    for signature in signatures:
        names = tuple(parameter["name"] for parameter in signature.get("parameters", []))
        groups.setdefault(names, set()).add(json.dumps(signature.get("returnType", {}), sort_keys=True))
    return any(len(values) > 1 for values in groups.values())

def same_call_shape(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def shape(signature: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {key: parameter.get(key) for key in ("name", "defaultValue", "optional", "keywordOnly", "variadic", "positionalOnly")}
            for parameter in signature.get("parameters", [])
        ]
    return shape(left) == shape(right)

def signature_is_untyped_implementation(signature: dict[str, Any], known_signatures: list[dict[str, Any]]) -> bool:
    return (
        signature.get("returnType", {}).get("state") == "UNKNOWN"
        and not any(parameter.get("type", {}).get("state") == "KNOWN" for parameter in signature.get("parameters", []))
        and any(same_call_shape(signature, known) for known in known_signatures)
    )

def merge_candidate_signatures(candidates: list[RawSymbol]) -> tuple[list[dict[str, Any]], bool]:
    """Expose overload declarations and discard their paired implementation."""
    if not candidates:
        return [], False
    ordered = sorted(candidates, key=lambda item: source_line(item.source.locator))
    typed = [item for item in ordered if item.declaration_role == "OVERLOAD"]
    if typed:
        return unique_signatures(typed), False
    signatures = unique_signatures(candidates)
    signatures = [signature for signature in signatures if not signature_is_untyped_implementation(signature, signatures)]
    if not signatures:
        signatures = unique_signatures(candidates)
    return signatures, conflicting_signatures(signatures)

def unique_signatures(candidates: list[RawSymbol]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: source_line(item.source.locator)):
        for signature in candidate.signatures:
            key = signature_key(signature)
            if key not in seen:
                seen.add(key)
                result.append(signature)
    return result

def source_line(locator: str) -> int:
    suffix = locator.rsplit(":", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0

def source_digests(entries: list[dict[str, Any]]) -> dict[str, str]:
    result = {}
    for entry in entries:
        for source in entry["sources"]:
            result[source["locator"].split(":")[0]] = source["digest"]
    return dict(sorted(result.items()))

def source_file_locator(locator: str) -> str:
    base, separator, line = locator.rpartition(":")
    return base if separator and line.isdigit() else locator

def inventory_digest(identities: list[dict[str, str]]) -> str:
    payload = json.dumps(identities, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def build_inventory(raw_symbols: list[RawSymbol], sage_version: str, python_version: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    identity_keys = sorted({item.key() for item in raw_symbols}, key=lambda item: (item[0], KIND_ORDER[item[1]]))
    identities = [{"qualifiedName": qualified_name, "kind": kind} for qualified_name, kind in identity_keys]
    source_digest_map: dict[str, str] = {}
    for item in raw_symbols:
        source_digest_map[source_file_locator(item.source.locator)] = item.source.digest
    inventory: dict[str, Any] = {
        "schemaVersion": INVENTORY_SCHEMA_VERSION,
        "basis": "RAW_AST_DECLARATIONS",
        "sageVersion": sage_version,
        "pythonVersion": python_version,
        "generatorVersion": GENERATOR_VERSION,
        "rawDeclarationCount": len(raw_symbols),
        "identityCount": len(identities),
        "identities": identities,
        "identityDigest": inventory_digest(identities),
        "sourceFileCount": len(source_digest_map),
        "sourceDigests": dict(sorted(source_digest_map.items())),
    }
    if metadata and metadata.get("artifactId"):
        inventory["artifactId"] = metadata["artifactId"]
    return inventory

def validate_inventory(inventory: dict[str, Any]) -> None:
    if not isinstance(inventory, dict) or inventory.get("schemaVersion") != INVENTORY_SCHEMA_VERSION:
        raise ValueError("generated API inventory has unsupported schemaVersion")
    if inventory.get("basis") != "RAW_AST_DECLARATIONS":
        raise ValueError("generated API inventory basis is invalid")
    for field in ("sageVersion", "pythonVersion", "generatorVersion"):
        if not isinstance(inventory.get(field), str) or not inventory[field].strip():
            raise ValueError(f"generated API inventory requires nonblank {field}")
    identities = inventory.get("identities")
    if not isinstance(identities, list):
        raise ValueError("generated API inventory identities must be an array")
    previous: tuple[str, int] | None = None
    for position, item in enumerate(identities):
        if not isinstance(item, dict) or not isinstance(item.get("qualifiedName"), str) or not item["qualifiedName"].strip() or item.get("kind") not in KIND_ORDER:
            raise ValueError(f"generated API inventory identity {position} is invalid")
        key = (item["qualifiedName"], KIND_ORDER[item["kind"]])
        if previous is not None and key <= previous:
            raise ValueError("generated API inventory identities are not strictly sorted")
        previous = key
    if inventory.get("identityCount") != len(identities) or inventory.get("identityDigest") != inventory_digest(identities):
        raise ValueError("generated API inventory identity digest is invalid")
    for field in ("rawDeclarationCount", "sourceFileCount"):
        if type(inventory.get(field)) is not int or inventory[field] < 0:
            raise ValueError(f"generated API inventory {field} is invalid")
    if inventory["rawDeclarationCount"] < inventory["identityCount"]:
        raise ValueError("generated API inventory rawDeclarationCount is less than identityCount")
    source_digests_value = inventory.get("sourceDigests")
    if not isinstance(source_digests_value, dict) or inventory["sourceFileCount"] != len(source_digests_value):
        raise ValueError("generated API inventory sourceDigests are invalid")
    for locator, digest in source_digests_value.items():
        if not isinstance(locator, str) or not locator.strip() or not is_sha256(digest):
            raise ValueError("generated API inventory sourceDigests contain an invalid digest")

def validate_index(index: dict[str, Any]) -> None:
    if not isinstance(index, dict):
        raise ValueError("generated index must be a JSON object")
    if type(index.get("schemaVersion")) is not int or index["schemaVersion"] != SCHEMA_VERSION:
        raise ValueError("generated index has unsupported schemaVersion")
    for field in ("sageVersion", "pythonVersion", "generatorVersion"):
        if not isinstance(index.get(field), str) or not index[field].strip():
            raise ValueError(f"generated index requires nonblank {field}")
    source_digests_value = index.get("sourceDigests", {})
    if not isinstance(source_digests_value, dict):
        raise ValueError("generated index sourceDigests must be an object")
    for locator, digest in source_digests_value.items():
        if not isinstance(locator, str) or not locator.strip() or not isinstance(digest, str) or not is_sha256(digest):
            raise ValueError("generated index sourceDigests must map locators to SHA-256 digests")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ValueError("generated index entries must be an array")
    seen: set[tuple[str, str]] = set()
    for entry_index, entry in enumerate(entries):
        validate_entry(entry, f"entries[{entry_index}]")
        key = (entry["qualifiedName"], entry["kind"])
        if key in seen:
            raise ValueError(f"generated index contains duplicate entry {key[0]} ({key[1]})")
        seen.add(key)

def validate_entry(entry: Any, path: str) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"{path} must be an object")
    for field in ("qualifiedName", "kind", "dynamicity", "confidence"):
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise ValueError(f"{path}.{field} must be a nonblank string")
    if entry["kind"] not in KIND_ORDER:
        raise ValueError(f"{path}.kind is not a known Sage symbol kind")
    if entry["dynamicity"] not in {"STATIC", "DYNAMIC", "UNKNOWN"}:
        raise ValueError(f"{path}.dynamicity is invalid")
    if entry["confidence"] not in CONFIDENCE_RANK:
        raise ValueError(f"{path}.confidence is invalid")
    for field in ("parents", "protocols", "aliases"):
        if not isinstance(entry.get(field), list) or not all(isinstance(item, str) and item.strip() for item in entry[field]):
            raise ValueError(f"{path}.{field} must contain nonblank strings")
    signatures = entry.get("signatures")
    if not isinstance(signatures, list):
        raise ValueError(f"{path}.signatures must be an array")
    for signature_index, signature in enumerate(signatures):
        validate_signature(signature, f"{path}.signatures[{signature_index}]")
    sources = entry.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"{path}.sources must be a nonempty array")
    for source_index, source in enumerate(sources):
        if not isinstance(source, dict) or source.get("kind") not in {"RUNTIME", "STUB", "SIGNATURE", "DOCUMENTATION", "USER_STUB", "PROBE"} or not isinstance(source.get("locator"), str) or not source["locator"].strip():
            raise ValueError(f"{path}.sources[{source_index}] is invalid")
        if "digest" in source and (not isinstance(source["digest"], str) or not is_sha256(source["digest"])):
            raise ValueError(f"{path}.sources[{source_index}].digest is invalid")
    if "valueType" in entry:
        validate_type_ref(entry["valueType"], f"{path}.valueType")
    if "documentation" in entry and not isinstance(entry["documentation"], dict):
        raise ValueError(f"{path}.documentation must be an object")

def validate_signature(signature: Any, path: str) -> None:
    if not isinstance(signature, dict) or not isinstance(signature.get("parameters"), list) or "returnType" not in signature:
        raise ValueError(f"{path} is invalid")
    for parameter_index, parameter in enumerate(signature["parameters"]):
        if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str) or not parameter["name"].strip() or not isinstance(parameter.get("type"), dict):
            raise ValueError(f"{path}.parameters[{parameter_index}] is invalid")
        validate_type_ref(parameter["type"], f"{path}.parameters[{parameter_index}].type")
        for boolean_field in ("optional", "keywordOnly", "variadic", "positionalOnly"):
            if boolean_field in parameter and type(parameter[boolean_field]) is not bool:
                raise ValueError(f"{path}.parameters[{parameter_index}].{boolean_field} must be boolean")
        if "defaultValue" in parameter and parameter["defaultValue"] is not None and not isinstance(parameter["defaultValue"], str):
            raise ValueError(f"{path}.parameters[{parameter_index}].defaultValue must be a string or null")
    validate_type_ref(signature["returnType"], f"{path}.returnType")
    type_parameters = signature.get("typeParameters", [])
    if not isinstance(type_parameters, list):
        raise ValueError(f"{path}.typeParameters must be an array")
    seen_type_parameters: set[str] = set()
    for index, parameter in enumerate(type_parameters):
        parameter_path = f"{path}.typeParameters[{index}]"
        if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str) or not parameter["name"].strip():
            raise ValueError(f"{parameter_path} is invalid")
        if parameter["name"] in seen_type_parameters:
            raise ValueError(f"{parameter_path}.name is duplicated")
        seen_type_parameters.add(parameter["name"])
        if parameter.get("kind", "TYPE_VARIABLE") not in {"TYPE_VARIABLE", "SELF", "PARAM_SPEC"}:
            raise ValueError(f"{parameter_path}.kind is invalid")
        if "bound" in parameter:
            validate_type_ref(parameter["bound"], f"{parameter_path}.bound")
        constraints = parameter.get("constraints", [])
        if not isinstance(constraints, list):
            raise ValueError(f"{parameter_path}.constraints must be an array")
        for constraint_index, constraint in enumerate(constraints):
            validate_type_ref(constraint, f"{parameter_path}.constraints[{constraint_index}]")
        if "bound" in parameter and constraints:
            raise ValueError(f"{parameter_path} cannot contain both bound and constraints")

def validate_type_ref(value: Any, path: str) -> None:
    if not isinstance(value, dict) or value.get("state") not in {"KNOWN", "UNKNOWN", "DYNAMIC"}:
        raise ValueError(f"{path} is an invalid type reference")
    expression = value.get("expression")
    if expression is not None and (not isinstance(expression, str) or not expression.strip()):
        raise ValueError(f"{path}.expression must be a nonblank string or null")
    if value["state"] == "KNOWN" and not isinstance(expression, str):
        raise ValueError(f"{path} KNOWN type requires an expression")

def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)

def expected_symbols(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("expected manifest must be a JSON array")
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("qualifiedName"), str) or not isinstance(item.get("kind"), str):
            raise ValueError("expected manifest entries require qualifiedName and kind")
        result.append({"qualifiedName": item["qualifiedName"], "kind": item["kind"]})
    return sorted({(item["qualifiedName"], item["kind"]): item for item in result}.values(), key=lambda item: (item["qualifiedName"], KIND_ORDER.get(item["kind"], 99)))

def coverage(index: dict[str, Any], diagnostics: list[dict[str, Any]], expected: list[dict[str, str]]) -> dict[str, Any]:
    entries = {(item["qualifiedName"], item["kind"]): item for item in index["entries"]}
    aliases = {}
    for key, entry in entries.items():
        for alias in entry.get("aliases", []):
            aliases.setdefault((alias, entry["kind"]), key)
    resolved = {(item["qualifiedName"], item["kind"]): ((item["qualifiedName"], item["kind"]) if (item["qualifiedName"], item["kind"]) in entries else aliases.get((item["qualifiedName"], item["kind"]))) for item in expected}
    covered = [item for item in expected if resolved[(item["qualifiedName"], item["kind"])] is not None]
    missing = [item for item in expected if resolved[(item["qualifiedName"], item["kind"])] is None]
    without_signature = [item for item in covered if resolved[(item["qualifiedName"], item["kind"])] and item["kind"] in {"FUNCTION", "METHOD", "CLASS"} and not entries[resolved[(item["qualifiedName"], item["kind"])]].get("signatures")]
    dynamic = [item for item in covered if resolved[(item["qualifiedName"], item["kind"])] and (entries[resolved[(item["qualifiedName"], item["kind"])]]["dynamicity"] != "STATIC" or any(signature.get("returnType", {}).get("state") == "DYNAMIC" for signature in entries[resolved[(item["qualifiedName"], item["kind"])]].get("signatures", [])))]
    return {"expected": expected, "covered": covered, "missing": missing, "withoutSignature": without_signature, "dynamic": dynamic, "conflicts": [item for item in diagnostics if item["kind"] == "CONFLICT"], "expectedCount": len(expected), "coveredCount": len(covered), "coverageRatio": 1.0 if not expected else len(covered) / len(expected), "isComplete": not missing and not any(item["kind"] == "CONFLICT" for item in diagnostics), "diagnostics": diagnostics}

def gate_failures(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures = []
    if report["missing"] and not args.allow_missing:
        failures.append(f"missing={len(report['missing'])}")
    if report["conflicts"] and not args.allow_conflicts:
        failures.append(f"conflicts={len(report['conflicts'])}")
    if args.min_coverage is not None and report["coverageRatio"] < args.min_coverage:
        failures.append(f"coverage={report['coverageRatio']:.6f}<min={args.min_coverage:.6f}")
    return failures

def diff_indexes(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    before = {(item["qualifiedName"], item["kind"]): item for item in previous.get("entries", [])}
    after = {(item["qualifiedName"], item["kind"]): item for item in current.get("entries", [])}
    item = lambda key: {"qualifiedName": key[0], "kind": key[1]}
    added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    return {"added": [item(key) for key in added], "removed": [item(key) for key in removed], "changed": [item(key) for key in changed], "addedCount": len(added), "removedCount": len(removed), "changedCount": len(changed)}

def discover(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.suffix in {".pyi", ".py"} and path.is_file())

def tree_metadata(spec: SourceSpec, paths: list[Path]) -> dict[str, Any]:
    files = []
    for path in paths:
        relative = path.relative_to(spec.root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({"path": relative, "digest": digest})
    files.sort(key=lambda item: item["path"])
    payload = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return {"fileCount": len(files), "files": [item["path"] for item in files], "treeDigest": hashlib.sha256(payload.encode("utf-8")).hexdigest()}

def validate_provenance(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("source manifest provenance must be a nonempty object")
    if value.get("kind") not in {"FIXTURE", "RUNTIME", "STUBGEN"}:
        raise ValueError("source manifest provenance.kind is invalid")
    for field in ("generator", "source"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"source manifest provenance.{field} must be a nonblank string")

def parse_source_manifest(path: Path, source_base: Path | None = None) -> tuple[list[SourceSpec], dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        items = value
        metadata = {}
    elif isinstance(value, dict):
        required = ("artifactId", "sageVersion", "pythonVersion", "provenance", "sources")
        missing = [field for field in required if field not in value]
        if missing:
            raise ValueError(f"source manifest requires {', '.join(missing)}")
        for field in ("artifactId", "sageVersion", "pythonVersion"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError(f"source manifest {field} must be a nonblank string")
        validate_provenance(value["provenance"])
        items = value["sources"]
        metadata = {"artifactId": value["artifactId"], "sageVersion": value["sageVersion"], "pythonVersion": value["pythonVersion"], "provenance": value["provenance"], "sourceManifest": path.name, "sourceSpecs": []}
    else:
        raise ValueError("source manifest must be a JSON array or artifact object")
    if not isinstance(items, list) or not items:
        raise ValueError("source manifest sources must be a nonempty JSON array")
    specs = []
    base = (source_base or path.parent).resolve()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"source manifest entry {index} must be an object")
        root = item.get("root")
        kind = item.get("kind")
        locator = item.get("locator")
        module_prefix = item.get("modulePrefix", "")
        valid_kinds = {"FIXTURE", "RUNTIME", "STUBGEN", "STUB", "SIGNATURE", "DOCUMENTATION", "USER_STUB", "PROBE"}
        if not isinstance(root, str) or not root.strip() or not isinstance(kind, str) or kind not in valid_kinds or not isinstance(locator, str) or not locator.strip() or not isinstance(module_prefix, str):
            raise ValueError(f"source manifest entry {index} requires root, valid kind, locator, and optional modulePrefix")
        source_root = Path(root)
        if not source_root.is_absolute():
            source_root = base / source_root
        source_root = source_root.resolve()
        if not source_root.is_dir():
            raise ValueError(f"source manifest root does not exist: {source_root}")
        declared_digest = item.get("treeDigest")
        if declared_digest is not None and (not isinstance(declared_digest, str) or not is_sha256(declared_digest)):
            raise ValueError(f"source manifest entry {index}.treeDigest is invalid")
        spec = SourceSpec(source_root, "STUB" if kind in {"FIXTURE", "STUBGEN"} else kind, locator, module_prefix)
        actual_metadata = tree_metadata(spec, discover(source_root))
        actual = actual_metadata["treeDigest"]
        if declared_digest is not None and actual != declared_digest:
            raise ValueError(f"source manifest entry {index} tree digest mismatch: expected {declared_digest}, got {actual}")
        if metadata:
            metadata["sourceSpecs"].append({"kind": kind, "extractorKind": spec.kind, "locator": locator, **actual_metadata})
        specs.append(spec)
    return specs, metadata

def validate_source_contract(index: dict[str, Any], metadata: dict[str, Any]) -> None:
    expected = metadata.get("sourceSpecs", [])
    actual = index.get("sources", [])
    expected_locators = [source.get("locator") for source in expected]
    if len(expected_locators) != len(set(expected_locators)):
        raise ValueError("source manifest locators must be unique")
    if len(expected) != len(actual):
        raise ValueError("generated index sources do not match manifest source count")
    for position, (manifest_source, index_source) in enumerate(zip(expected, actual)):
        for field in ("kind", "locator"):
            if index_source.get(field) != manifest_source.get(field):
                raise ValueError(f"generated index source {position} {field} does not match manifest")
        expected_files = [f"{manifest_source['locator'].rstrip('/')}/{path}" for path in manifest_source.get("files", [])]
        if index_source.get("fileCount") != manifest_source.get("fileCount") or index_source.get("files") != expected_files:
            raise ValueError(f"generated index source {position} file metadata does not match scanned source")
        if manifest_source.get("treeDigest") and index_source.get("treeDigest") != manifest_source["treeDigest"]:
            raise ValueError(f"generated index source {position} treeDigest does not match manifest")
        expected_file_locators = set(expected_files)
        entry_sources = [
            source
            for entry in index.get("entries", [])
            for source in entry.get("sources", [])
            if source["kind"] == manifest_source.get("extractorKind")
            and source["locator"].split(":")[0] in expected_file_locators
        ]
        if not entry_sources:
            raise ValueError(f"source manifest entry {position} has no extracted entries")
        digest_keys = {source["locator"].split(":")[0] for source in entry_sources}
        if not digest_keys.issubset(index.get("sourceDigests", {})):
            raise ValueError(f"source manifest entry {position} is missing source digests")
    entry_sources = [source for entry in index.get("entries", []) for source in entry.get("sources", [])]
    for source in entry_sources:
        source_locator = source["locator"].split(":")[0]
        candidates = [manifest_source for manifest_source in expected if source["kind"] == manifest_source.get("extractorKind")]
        if not any(source_locator in {f"{candidate['locator'].rstrip('/')}/{path}" for path in candidate.get("files", [])} for candidate in candidates):
            if any(source["locator"].startswith(candidate["locator"].rstrip("/") + "/") for candidate in candidates):
                raise ValueError(f"entry source locator {source['locator']} does not match a scanned source file")
            raise ValueError(f"entry source locator {source['locator']} does not belong to a manifest source")
    if index.get("sourceDigests") != source_digests(index.get("entries", [])):
        raise ValueError("generated index sourceDigests do not match entry sources")

def build(args: argparse.Namespace) -> int:
    if args.source_manifest is None and args.source_root is None:
        raise ValueError("either --source-root or --source-manifest is required")
    if args.source_manifest:
        specs, metadata = parse_source_manifest(args.source_manifest, args.source_base)
    else:
        specs, metadata = [SourceSpec(args.source_root, "STUB", args.source_locator, args.module_prefix)], {}
    paths = [(spec, path) for spec in specs for path in discover(spec.root)]
    if not paths:
        raise ValueError("no .pyi/.py sources found in configured source roots")
    raw = [symbol for spec, path in paths for symbol in AstExtractor(spec).extract_path(path)]
    sage_version = metadata.get("sageVersion", args.sage_version)
    python_version = metadata.get("pythonVersion", args.python_version)
    inventory = build_inventory(raw, sage_version, python_version, metadata)
    index, diagnostics = normalize(raw, sage_version, python_version, metadata)
    if metadata:
        validate_source_contract(index, metadata)
    if args.return_evidence_manifest:
        manifest = parse_return_evidence_manifest(args.return_evidence_manifest)
        validate_return_evidence_manifest(manifest, index)
        attach_trusted_return_evidence(index, manifest, args.return_evidence_manifest)
    validate_index(index)
    validate_inventory(inventory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Compact form: the index ships into IDE sidecars behind a hard byte cap
    # that protects startup; pretty-printing alone added ~39% over payload.
    args.output.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if args.inventory_output:
        args.inventory_output.parent.mkdir(parents=True, exist_ok=True)
        args.inventory_output.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.raw_output:
        args.raw_output.parent.mkdir(parents=True, exist_ok=True)
        args.raw_output.write_text(json.dumps([{"qualifiedName": item.qualified_name, "kind": item.kind, "sources": [item.source.json()], "signatures": item.signatures, "parents": item.parents, "aliases": item.aliases, "dynamicity": item.dynamicity, "confidence": item.confidence} for item in raw], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report = coverage(index, diagnostics, expected_symbols(args.expected))
    if args.coverage_output:
        args.coverage_output.parent.mkdir(parents=True, exist_ok=True)
        args.coverage_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.previous and args.diff_output:
        previous = json.loads(args.previous.read_text(encoding="utf-8"))
        args.diff_output.parent.mkdir(parents=True, exist_ok=True)
        args.diff_output.write_text(json.dumps(diff_indexes(previous, index), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {"sources": len(paths), "rawSymbols": len(raw), "entries": len(index["entries"]), "diagnostics": len(diagnostics), "coverage": report["coverageRatio"], "missing": len(report["missing"]), "inventoryEntries": inventory["identityCount"], "inventoryDigest": inventory["identityDigest"]}
    failures = gate_failures(report, args)
    if failures:
        summary["gateFailures"] = failures
        print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
        return 3
    print(json.dumps(summary, ensure_ascii=False))
    return 0

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source-root", type=Path)
    result.add_argument("--module-prefix", default="")
    result.add_argument("--source-manifest", type=Path)
    result.add_argument("--source-base", type=Path)
    result.add_argument("--source-locator", default="runtime-export")
    result.add_argument("--return-evidence-manifest", type=Path)
    result.add_argument("--sage-version", required=True)
    result.add_argument("--python-version", required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--raw-output", type=Path)
    result.add_argument("--inventory-output", type=Path)
    result.add_argument("--expected", type=Path)
    result.add_argument("--coverage-output", type=Path)
    result.add_argument("--previous", type=Path)
    result.add_argument("--diff-output", type=Path)
    result.add_argument("--min-coverage", type=float)
    result.add_argument("--allow-missing", action="store_true")
    result.add_argument("--allow-conflicts", action="store_true")
    return result

if __name__ == "__main__":
    try:
        raise SystemExit(build(parser().parse_args()))
    except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as error:
        print(f"sage-api-index: error: {error}", file=sys.stderr)
        raise SystemExit(2)
