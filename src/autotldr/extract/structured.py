"""Tier-0 structured data and configuration formats.

These inputs are already structured, so their meaning is their schema rather
than a prose rendering of every value.  JSON, JSONL, and TOML are induced into
path-level schema units; XML becomes an element/attribute schema; CSV and TSV
become bounded-memory column profiles.  No record or row is emitted verbatim.

Everything except YAML uses the standard library. YAML is deliberately routed
through a lazy optional PyYAML import rather than a JSON-shaped subset
pretending to support the format.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

SUPPORTED_SUFFIXES = frozenset(
    {
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".csv",
        ".tsv",
    }
)

_MAX_DISTINCT = 256
_MAX_SHOWN_VALUES = 8
_MAX_EVIDENCE_REFS = 8
_MAX_SCHEMA_PATHS = 10_000
_MAX_TRAVERSAL_VALUES = 1_000_000
_MAX_NESTING_DEPTH = 128
_MAX_ALIAS_VISITS = 64
_MAX_DELIMITED_COLUMNS = 4096
_INTEGER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)\Z")
_NUMBER = re.compile(
    r"[+-]?(?:(?:0|[1-9][0-9]*)\.[0-9]+|(?:0|[1-9][0-9]*)(?:[eE][+-]?[0-9]+)|"
    r"(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][+-]?[0-9]+))\Z"
)


class InvalidStructuredData(ValueError):
    """A recognized structured input whose bytes do not form that format."""

    def __init__(self, path: Path, kind: str, detail: str) -> None:
        self.path = path
        self.kind = kind
        self.detail = detail
        super().__init__(f"{path.name}: invalid {kind}: {detail}")


class _DuplicateJsonKey(ValueError):
    pass


class _InvalidJsonConstant(ValueError):
    pass


class _TraversalLimit(ValueError):
    pass


class _InvalidUnicodeScalar(ValueError):
    pass


@dataclass(slots=True)
class _TraversalState:
    """Bound recursive induction and reject cyclic/amplified alias graphs."""

    visits: int = 0
    active_containers: set[int] = field(default_factory=set)
    container_visits: Counter[int] = field(default_factory=Counter)

    def enter(self, value: Any, path: tuple[str, ...]) -> int | None:
        if len(path) > _MAX_NESTING_DEPTH:
            raise _TraversalLimit(
                f"nesting exceeds {_MAX_NESTING_DEPTH} schema levels"
            )
        self.visits += 1
        if self.visits > _MAX_TRAVERSAL_VALUES:
            raise _TraversalLimit(
                f"value traversal exceeds {_MAX_TRAVERSAL_VALUES} observations"
            )

        if not isinstance(value, (dict, list, tuple)):
            return None
        identity = id(value)
        if identity in self.active_containers:
            raise _TraversalLimit("recursive alias cycle")
        self.container_visits[identity] += 1
        if self.container_visits[identity] > _MAX_ALIAS_VISITS:
            raise _TraversalLimit(
                f"alias expansion visits one container more than "
                f"{_MAX_ALIAS_VISITS} times"
            )
        self.active_containers.add(identity)
        return identity

    def leave(self, identity: int | None) -> None:
        if identity is not None:
            self.active_containers.discard(identity)


@dataclass(slots=True)
class _ScalarStats:
    """Bounded-memory scalar statistics shared by every schema inducer."""

    seen: int = 0
    nulls: int = 0
    types: Counter[str] = field(default_factory=Counter)
    numeric_min: int | float | Decimal | None = None
    numeric_max: int | float | Decimal | None = None
    numeric_sum: Decimal = field(default_factory=lambda: Decimal(0))
    numeric_count: int = 0
    string_min_length: int | None = None
    string_max_length: int | None = None
    distinct: dict[str, str] = field(default_factory=dict)
    distinct_overflow: bool = False

    def observe(self, value: Any, *, type_name: str | None = None) -> None:
        self.seen += 1
        name = type_name or _type_name(value)
        self.types[name] += 1

        if value is None:
            self.nulls += 1
            return

        if name in {"integer", "number"} and not isinstance(value, bool):
            self._observe_number(value)
        elif name in {"string", "date", "time", "datetime"}:
            length = len(str(value))
            self.string_min_length = (
                length if self.string_min_length is None else min(self.string_min_length, length)
            )
            self.string_max_length = (
                length if self.string_max_length is None else max(self.string_max_length, length)
            )

        if name not in {"object", "array", "element"}:
            self._remember_distinct(value)

    def _observe_number(self, value: int | float | Decimal) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            return
        self.numeric_min = value if self.numeric_min is None else min(self.numeric_min, value)
        self.numeric_max = value if self.numeric_max is None else max(self.numeric_max, value)
        try:
            self.numeric_sum += Decimal(str(value))
            self.numeric_count += 1
        except InvalidOperation:
            pass

    def _remember_distinct(self, value: Any) -> None:
        key = _canonical_scalar(value)
        if key in self.distinct or self.distinct_overflow:
            return
        if len(self.distinct) >= _MAX_DISTINCT:
            self.distinct_overflow = True
            return
        self.distinct[key] = _display_scalar(value)


@dataclass(slots=True)
class _SchemaNode:
    path: tuple[str, ...]
    visits: int = 0
    stats: _ScalarStats = field(default_factory=_ScalarStats)
    array_length_min: int | None = None
    array_length_max: int | None = None
    evidence_refs: list[str] = field(default_factory=list)
    evidence_line_min: int | None = None
    evidence_line_max: int | None = None

    def add_ref(self, ref: str, line: int | None) -> None:
        if ref not in self.evidence_refs and len(self.evidence_refs) < _MAX_EVIDENCE_REFS:
            self.evidence_refs.append(ref)
        if line is not None:
            self.evidence_line_min = (
                line if self.evidence_line_min is None else min(self.evidence_line_min, line)
            )
            self.evidence_line_max = (
                line if self.evidence_line_max is None else max(self.evidence_line_max, line)
            )

    def add_line_range(self, start: int, end: int) -> None:
        self.evidence_line_min = (
            start
            if self.evidence_line_min is None
            else min(self.evidence_line_min, start)
        )
        self.evidence_line_max = (
            end if self.evidence_line_max is None else max(self.evidence_line_max, end)
        )

    def add_array_length(self, length: int) -> None:
        self.array_length_min = (
            length if self.array_length_min is None else min(self.array_length_min, length)
        )
        self.array_length_max = (
            length if self.array_length_max is None else max(self.array_length_max, length)
        )


def extract(path: Path) -> Extraction:
    """Dispatch a recognized structured suffix to its native stdlib parser."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _extract_json(path)
    if suffix in {".jsonl", ".ndjson"}:
        return _extract_jsonl(path)
    if suffix in {".yaml", ".yml"}:
        return _extract_yaml(path)
    if suffix == ".toml":
        return _extract_toml(path)
    if suffix == ".xml":
        return _extract_xml(path)
    if suffix == ".csv":
        return _extract_delimited(path, delimiter=",", kind="csv")
    if suffix == ".tsv":
        return _extract_delimited(path, delimiter="\t", kind="tsv")
    raise ValueError(f"structured extractor does not handle {suffix or 'extensionless input'}")


# ---------------------------------------------------------------------------
# JSON, JSONL, and TOML schema induction
# ---------------------------------------------------------------------------


def _extract_json(path: Path) -> Extraction:
    text = _read_text(path, "JSON")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _InvalidJsonConstant,
        RecursionError,
    ) as exc:
        detail = _json_error_detail(exc)
        raise InvalidStructuredData(path, "JSON", detail) from exc

    nodes: dict[tuple[str, ...], _SchemaNode] = {}
    try:
        _observe_schema(nodes, (), (), value, _json_ref, line=None)
    except (RecursionError, _TraversalLimit, _InvalidUnicodeScalar) as exc:
        detail = "nesting is too deep" if isinstance(exc, RecursionError) else str(exc)
        raise InvalidStructuredData(path, "JSON", detail) from exc

    result = _schema_extraction(path, "json", nodes, _json_origin, _json_path_label)
    result.meta.update(
        {
            "bytes": len(text.encode("utf-8")),
            "top_level": _type_name(value),
            "schema_paths": len(nodes),
        }
    )
    _schema_gaps(result, nodes, "document", _json_path_label)
    return result


def _extract_jsonl(path: Path) -> Extraction:
    text = _read_text(path, "JSONL")
    nodes: dict[tuple[str, ...], _SchemaNode] = {}
    traversal = _TraversalState()
    records = 0
    blank_lines = 0

    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            blank_lines += 1
            continue
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (
            json.JSONDecodeError,
            _DuplicateJsonKey,
            _InvalidJsonConstant,
            RecursionError,
        ) as exc:
            detail = _json_error_detail(exc)
            raise InvalidStructuredData(
                path, "JSONL", f"line {line_no}: {detail}"
            ) from exc
        records += 1
        try:
            _observe_schema(
                nodes,
                (),
                (),
                value,
                lambda concrete, n=line_no: _jsonl_ref(n, concrete),
                line=line_no,
                traversal=traversal,
            )
        except (RecursionError, _TraversalLimit, _InvalidUnicodeScalar) as exc:
            detail = "nesting is too deep" if isinstance(exc, RecursionError) else str(exc)
            raise InvalidStructuredData(
                path, "JSONL", f"line {line_no}: {detail}"
            ) from exc

    result = _schema_extraction(path, "jsonl", nodes, _jsonl_origin, _json_path_label)
    result.meta.update(
        {
            "bytes": len(text.encode("utf-8")),
            "lines": len(text.splitlines()),
            "records": records,
            "blank_lines": blank_lines,
            "schema_paths": len(nodes),
        }
    )
    if not records:
        result.gaps.append("no records: the JSONL stream carries no schema")
    elif blank_lines:
        result.gaps.append(f"{blank_lines} blank line(s) were skipped")
    _schema_gaps(result, nodes, "stream", _json_path_label)
    return result


def _extract_yaml(path: Path) -> Extraction:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - install-dependent
        if exc.name != "yaml":
            raise
        raise ImportError(
            "YAML support requires PyYAML; install it with: "
            "pip install 'autotldr[structured]'"
        ) from exc

    text = _read_text(path, "YAML")
    loader = _unique_yaml_loader(yaml)
    try:
        values = list(yaml.load_all(text, Loader=loader))
        syntax_nodes = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f"line {mark.line + 1}, column {mark.column + 1}: "
            if mark is not None
            else ""
        )
        detail = getattr(exc, "problem", None) or str(exc)
        raise InvalidStructuredData(path, "YAML", location + detail) from exc

    nodes: dict[tuple[str, ...], _SchemaNode] = {}
    traversal = _TraversalState()
    try:
        for document, value in enumerate(values, start=1):
            _observe_schema(
                nodes,
                (),
                (),
                value,
                lambda concrete, n=document: _yaml_ref(n, concrete),
                line=None,
                traversal=traversal,
            )

        for value, syntax_node in zip(values, syntax_nodes, strict=False):
            if syntax_node is not None:
                _mark_yaml_schema(yaml, nodes, (), syntax_node, value)
    except (RecursionError, _TraversalLimit, _InvalidUnicodeScalar) as exc:
        detail = "nesting is too deep" if isinstance(exc, RecursionError) else str(exc)
        raise InvalidStructuredData(path, "YAML", detail) from exc

    result = _schema_extraction(
        path, "yaml", nodes, _yaml_origin, _yaml_path_label
    )
    result.meta.update(
        {
            "bytes": len(text.encode("utf-8")),
            "documents": len(values),
            "schema_paths": len(nodes),
            "top_level_types": sorted(nodes.get((), _SchemaNode(())).stats.types),
        }
    )
    if not values:
        result.gaps.append("no documents: the YAML input carries no schema")
    _schema_gaps(result, nodes, "YAML input", _yaml_path_label)
    return result


def _extract_toml(path: Path) -> Extraction:
    try:
        data = path.read_bytes()
        value = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise InvalidStructuredData(
            path, "TOML", f"input is not UTF-8 at byte {exc.start}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise InvalidStructuredData(path, "TOML", str(exc)) from exc

    nodes: dict[tuple[str, ...], _SchemaNode] = {}
    try:
        _observe_schema(nodes, (), (), value, _toml_ref, line=None)
    except (RecursionError, _TraversalLimit, _InvalidUnicodeScalar) as exc:
        detail = "nesting is too deep" if isinstance(exc, RecursionError) else str(exc)
        raise InvalidStructuredData(path, "TOML", detail) from exc
    result = _schema_extraction(path, "toml", nodes, _toml_origin, _toml_path_label)
    result.meta.update(
        {
            "bytes": len(data),
            "schema_paths": len(nodes),
            "keys": sum(
                1
                for schema_path, node in nodes.items()
                if schema_path
                and schema_path[-1] != "*"
                and "object" not in node.stats.types
            ),
            "tables": sum(
                1
                for schema_path, node in nodes.items()
                if schema_path and "object" in node.stats.types
            ),
        }
    )
    if not value:
        result.gaps.append("no keys: the configuration is empty")
    _schema_gaps(result, nodes, "configuration", _toml_path_label)
    return result


def _observe_schema(
    nodes: dict[tuple[str, ...], _SchemaNode],
    schema_path: tuple[str, ...],
    concrete_path: tuple[str, ...],
    value: Any,
    ref_for: Callable[[tuple[str, ...]], str],
    *,
    line: int | None,
    traversal: _TraversalState | None = None,
) -> None:
    traversal = traversal or _TraversalState()
    identity = traversal.enter(value, schema_path)
    try:
        _ensure_utf8_scalar(value, schema_path)
        node = _schema_node(nodes, schema_path)
        node.visits += 1
        node.stats.observe(value)
        node.add_ref(ref_for(concrete_path), line)

        if isinstance(value, dict):
            for key, child in value.items():
                token = str(key)
                _ensure_utf8_text(token, schema_path + ("<key>",))
                _observe_schema(
                    nodes,
                    schema_path + (token,),
                    concrete_path + (token,),
                    child,
                    ref_for,
                    line=line,
                    traversal=traversal,
                )
        elif isinstance(value, (list, tuple)):
            node.add_array_length(len(value))
            for index, child in enumerate(value):
                _observe_schema(
                    nodes,
                    schema_path + ("*",),
                    concrete_path + (str(index),),
                    child,
                    ref_for,
                    line=line,
                    traversal=traversal,
                )
    finally:
        traversal.leave(identity)


def _schema_node(
    nodes: dict[tuple[str, ...], _SchemaNode], path: tuple[str, ...]
) -> _SchemaNode:
    node = nodes.get(path)
    if node is not None:
        return node
    if len(nodes) >= _MAX_SCHEMA_PATHS:
        raise _TraversalLimit(
            f"schema exceeds {_MAX_SCHEMA_PATHS} distinct paths"
        )
    node = _SchemaNode(path)
    nodes[path] = node
    return node


def _ensure_utf8_scalar(value: Any, path: tuple[str, ...]) -> None:
    if isinstance(value, str):
        _ensure_utf8_text(value, path)


def _ensure_utf8_text(value: str, path: tuple[str, ...]) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        label = _json_path_label(path)
        raise _InvalidUnicodeScalar(
            f"{label} contains an unpaired Unicode surrogate at character "
            f"{exc.start}"
        ) from exc


def _schema_extraction(
    path: Path,
    kind: str,
    nodes: dict[tuple[str, ...], _SchemaNode],
    origin_for: Callable[[_SchemaNode], str],
    label_for: Callable[[tuple[str, ...]], str],
) -> Extraction:
    source = str(path)
    result = Extraction(source=source, kind=kind)
    by_path: dict[tuple[str, ...], Unit] = {}

    for schema_path, node in nodes.items():
        parent = nodes.get(schema_path[:-1]) if schema_path else None
        unit = Unit(
            source=source,
            modality=Modality.SCHEMA,
            content=_describe_schema_node(node, parent, nodes, label_for),
            origin=Origin(source, origin_for(node)),
            role=Role.UNKNOWN,
            structure=tuple(label_for(schema_path[:index]) for index in range(1, len(schema_path) + 1)),
            salience=0.9 if not schema_path else 0.65,
            meta=_schema_meta(node, parent, label_for),
        )
        result.units.append(unit)
        by_path[schema_path] = unit

    for schema_path, unit in by_path.items():
        if not schema_path or schema_path[:-1] not in by_path:
            continue
        parent = by_path[schema_path[:-1]]
        result.relations.append(
            Relation(
                src=parent.id,
                dst=unit.id,
                kind=RelationKind.DESCRIBES,
                evidence=f"{unit.origin.ref} is structurally nested under {parent.origin.ref}",
            )
        )
    return result


def _describe_schema_node(
    node: _SchemaNode,
    parent: _SchemaNode | None,
    nodes: dict[tuple[str, ...], _SchemaNode],
    label_for: Callable[[tuple[str, ...]], str],
) -> str:
    label = label_for(node.path)
    parts = [f"{label}: {_type_summary(node.stats.types)}"]

    if parent is not None and node.visits < parent.visits:
        parts.append(f"present in {node.visits} of {parent.visits} parent value(s)")

    if "object" in node.stats.types:
        fields = sorted(
            child_path[-1]
            for child_path in nodes
            if len(child_path) == len(node.path) + 1
            and child_path[:-1] == node.path
            and child_path[-1] != "*"
        )
        if fields:
            parts.append("fields " + ", ".join(_quote_name(field) for field in fields))

    if "array" in node.stats.types and node.array_length_min is not None:
        parts.append(
            f"array length {_range_text(node.array_length_min, node.array_length_max)}"
        )

    parts.extend(_scalar_findings(node.stats))
    return "; ".join(parts) + "."


def _schema_meta(
    node: _SchemaNode,
    parent: _SchemaNode | None,
    label_for: Callable[[tuple[str, ...]], str],
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "schema_path": label_for(node.path),
        "observations": node.visits,
        "types": sorted(node.stats.types),
        "nullable": bool(node.stats.nulls),
        "evidence_refs": node.evidence_refs,
    }
    if parent is not None:
        meta["parent_observations"] = parent.visits
        meta["presence"] = round(node.visits / parent.visits, 6) if parent.visits else 0.0
    if node.array_length_min is not None:
        meta["array_length"] = {
            "min": node.array_length_min,
            "max": node.array_length_max,
        }
    meta.update(_scalar_meta(node.stats))
    return meta


def _schema_gaps(
    result: Extraction,
    nodes: dict[tuple[str, ...], _SchemaNode],
    subject: str,
    label_for: Callable[[tuple[str, ...]], str],
) -> None:
    root = nodes.get(())
    if root is not None and "object" in root.stats.types and len(nodes) == 1:
        result.gaps.append(f"no fields: the {subject}'s root object is empty")
    for path, node in nodes.items():
        if "array" in node.stats.types and node.array_length_max == 0:
            result.gaps.append(
                f"{label_for(path)} contains only empty arrays; its item schema is absent"
            )


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateJsonKey(f"duplicate object key {key!r}")
        out[key] = value
    return out


def _reject_json_constant(value: str) -> None:
    raise _InvalidJsonConstant(f"non-standard numeric constant {value}")


def _json_error_detail(exc: BaseException) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return str(exc)


def _json_ref(concrete_path: tuple[str, ...]) -> str:
    return "pointer:" + _json_pointer(concrete_path)


def _jsonl_ref(line: int, concrete_path: tuple[str, ...]) -> str:
    pointer = _json_pointer(concrete_path)
    return f"line:{line}" + (f"#pointer:{pointer}" if pointer else "")


def _json_origin(node: _SchemaNode) -> str:
    # A wildcard schema observation is grounded in the array containing all of
    # its samples, not falsely in whichever array item happened to appear first.
    concrete: list[str] = []
    for token in node.path:
        if token == "*":
            break
        concrete.append(token)
    return "pointer:" + _json_pointer(tuple(concrete))


def _jsonl_origin(node: _SchemaNode) -> str:
    if node.evidence_line_min is None or node.evidence_line_max is None:
        return "lines:"
    low, high = node.evidence_line_min, node.evidence_line_max
    ref = f"line:{low}" if low == high else f"lines:{low}-{high}"
    pointer = _json_pointer(node.path)
    return ref + (f"#pointer:{pointer}" if pointer else "")


def _json_pointer(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    return "/" + "/".join(token.replace("~", "~0").replace("/", "~1") for token in path)


def _json_path_label(path: tuple[str, ...]) -> str:
    return "$" + _json_pointer(path)


def _toml_ref(concrete_path: tuple[str, ...]) -> str:
    return "document:" if not concrete_path else "key:" + _toml_dotted(concrete_path)


def _toml_origin(node: _SchemaNode) -> str:
    if not node.path:
        return "document:"
    concrete = node.path[: node.path.index("*")] if "*" in node.path else node.path
    prefix = "table:" if "object" in node.stats.types else "key:"
    return prefix + _toml_dotted(concrete)


def _toml_path_label(path: tuple[str, ...]) -> str:
    return "root" if not path else _toml_dotted(path)


def _toml_dotted(path: tuple[str, ...]) -> str:
    return ".".join(_toml_key(token) for token in path)


def _toml_key(token: str) -> str:
    if token == "*":
        return "*"
    if re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return token
    return json.dumps(token, ensure_ascii=False)


def _unique_yaml_loader(yaml):
    """Return a safe loader that refuses ambiguous duplicate mapping keys."""

    class UniqueSafeLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        explicit_keys = set()
        for key_node, _ in node.value:
            # YAML merge keys intentionally overlap with explicit keys: the
            # explicit value is the override. Check only keys written in this
            # mapping, then let SafeLoader apply merge semantics normally.
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in explicit_keys
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            explicit_keys.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    UniqueSafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    return UniqueSafeLoader


def _mark_yaml_schema(yaml, nodes, path, syntax_node, value) -> None:
    schema_node = nodes.get(path)
    if schema_node is None:
        return

    start = syntax_node.start_mark.line + 1
    end = max(start, syntax_node.end_mark.line + 1)
    label = _yaml_path_label(path)
    line_ref = f"line:{start}" if start == end else f"lines:{start}-{end}"
    schema_node.add_ref(f"{line_ref}#path:{label}", None)
    schema_node.add_line_range(start, end)

    if isinstance(syntax_node, yaml.MappingNode) and isinstance(value, dict):
        loaded_items = list(value.items())
        for index, (_, child_node) in enumerate(syntax_node.value):
            if index >= len(loaded_items):
                break
            key, child_value = loaded_items[index]
            _mark_yaml_schema(
                yaml,
                nodes,
                path + (str(key),),
                child_node,
                child_value,
            )
    elif isinstance(syntax_node, yaml.SequenceNode) and isinstance(value, (list, tuple)):
        for child_node, child_value in zip(syntax_node.value, value, strict=False):
            _mark_yaml_schema(
                yaml,
                nodes,
                path + ("*",),
                child_node,
                child_value,
            )


def _yaml_ref(document: int, concrete_path: tuple[str, ...]) -> str:
    return f"document:{document}#path:{_yaml_path_label(concrete_path)}"


def _yaml_origin(node: _SchemaNode) -> str:
    if node.evidence_line_min is None or node.evidence_line_max is None:
        return "document:"
    low, high = node.evidence_line_min, node.evidence_line_max
    line_ref = f"line:{low}" if low == high else f"lines:{low}-{high}"
    return f"{line_ref}#path:{_yaml_path_label(node.path)}"


def _yaml_path_label(path: tuple[str, ...]) -> str:
    return "$" + _json_pointer(path)


# ---------------------------------------------------------------------------
# XML structural induction
# ---------------------------------------------------------------------------


def _extract_xml(path: Path) -> Extraction:
    try:
        data = path.read_bytes()
        iterator = ET.iterparse(io.BytesIO(data), events=("start", "start-ns"))
        namespaces: dict[str, str] = {}
        for event, value in iterator:
            if event == "start-ns":
                prefix, uri = value
                namespaces[prefix or ""] = uri
        root = iterator.root
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (0, 0))
        raise InvalidStructuredData(
            path, "XML", f"line {line}, column {column}: {exc}"
        ) from exc

    nodes: dict[tuple[str, ...], _SchemaNode] = {}
    try:
        elements = _observe_xml(nodes, root, (_xml_name(root.tag),))
    except (RecursionError, _TraversalLimit, _InvalidUnicodeScalar) as exc:
        detail = "nesting is too deep" if isinstance(exc, RecursionError) else str(exc)
        raise InvalidStructuredData(path, "XML", detail) from exc
    result = _xml_extraction(path, nodes)
    result.meta.update(
        {
            "bytes": len(data),
            "root": _xml_name(root.tag),
            "elements": elements,
            "namespaces": namespaces,
            "schema_paths": len(nodes),
        }
    )
    return result


def _observe_xml(
    nodes: dict[tuple[str, ...], _SchemaNode], element: ET.Element, path: tuple[str, ...]
) -> int:
    count = 0
    stack: list[tuple[ET.Element, tuple[str, ...]]] = [(element, path)]
    while stack:
        current, current_path = stack.pop()
        if len(current_path) > _MAX_NESTING_DEPTH:
            raise _TraversalLimit(
                f"nesting exceeds {_MAX_NESTING_DEPTH} schema levels"
            )
        count += 1
        if count > _MAX_TRAVERSAL_VALUES:
            raise _TraversalLimit(
                f"element traversal exceeds {_MAX_TRAVERSAL_VALUES} observations"
            )

        ref = _xml_xpath(current_path)
        node = _schema_node(nodes, current_path)
        node.visits += 1
        node.stats.observe("element", type_name="element")
        node.add_ref(ref, None)

        for raw_name, raw_value in current.attrib.items():
            attr_name = "@" + _xml_name(raw_name)
            _ensure_utf8_text(attr_name, current_path + ("<attribute>",))
            _ensure_utf8_text(raw_value, current_path + (attr_name,))
            attr_path = current_path + (attr_name,)
            attr = _schema_node(nodes, attr_path)
            attr.visits += 1
            value, kind = _coerce_text_scalar(raw_value)
            attr.stats.observe(value, type_name=kind)
            attr.add_ref(_xml_xpath(attr_path), None)

        direct_text = (current.text or "").strip()
        if direct_text:
            text_path = current_path + ("#text",)
            _ensure_utf8_text(direct_text, text_path)
            text_node = _schema_node(nodes, text_path)
            text_node.visits += 1
            value, kind = _coerce_text_scalar(direct_text)
            text_node.stats.observe(value, type_name=kind)
            text_node.add_ref(_xml_xpath(text_path), None)

        children = list(current)
        for child in reversed(children):
            child_path = current_path + (_xml_name(child.tag),)
            stack.append((child, child_path))
    return count


def _xml_extraction(path: Path, nodes: dict[tuple[str, ...], _SchemaNode]) -> Extraction:
    source = str(path)
    result = Extraction(source=source, kind="xml")
    by_path: dict[tuple[str, ...], Unit] = {}

    for schema_path, node in nodes.items():
        parent = nodes.get(schema_path[:-1])
        is_element = "element" in node.stats.types
        content = _describe_xml_node(node, parent, nodes)
        unit = Unit(
            source=source,
            modality=Modality.SCHEMA,
            content=content,
            origin=Origin(source, _xml_xpath(schema_path)),
            role=Role.UNKNOWN,
            structure=tuple(_xml_path_label(schema_path[:index]) for index in range(1, len(schema_path) + 1)),
            salience=0.85 if len(schema_path) == 1 else (0.65 if is_element else 0.55),
            meta={
                "schema_path": _xml_path_label(schema_path),
                "node_kind": "element" if is_element else ("text" if schema_path[-1] == "#text" else "attribute"),
                "observations": node.visits,
                "types": sorted(node.stats.types),
                "evidence_refs": node.evidence_refs,
                **_scalar_meta(node.stats),
            },
        )
        result.units.append(unit)
        by_path[schema_path] = unit

    for schema_path, unit in by_path.items():
        parent = by_path.get(schema_path[:-1])
        if parent is None:
            continue
        result.relations.append(
            Relation(
                src=parent.id,
                dst=unit.id,
                kind=RelationKind.DESCRIBES,
                evidence=f"{unit.origin.ref} is structurally nested under {parent.origin.ref}",
            )
        )
    return result


def _describe_xml_node(
    node: _SchemaNode,
    parent: _SchemaNode | None,
    nodes: dict[tuple[str, ...], _SchemaNode],
) -> str:
    label = _xml_path_label(node.path)
    if "element" not in node.stats.types:
        kind = "text" if node.path[-1] == "#text" else "attribute"
        parts = [f"{label}: {kind} {_type_summary(node.stats.types)}"]
        if parent is not None and node.visits < parent.visits:
            parts.append(f"present on {node.visits} of {parent.visits} parent element(s)")
        parts.extend(_scalar_findings(node.stats))
        return "; ".join(parts) + "."

    children = sorted(
        child[-1]
        for child in nodes
        if len(child) == len(node.path) + 1
        and child[:-1] == node.path
        and not child[-1].startswith(("@", "#"))
    )
    attributes = sorted(
        child[-1][1:]
        for child in nodes
        if len(child) == len(node.path) + 1
        and child[:-1] == node.path
        and child[-1].startswith("@")
    )
    parts = [f"{label}: element observed {node.visits} time(s)"]
    if attributes:
        parts.append("attributes " + ", ".join(_quote_name(name) for name in attributes))
    if children:
        parts.append("children " + ", ".join(_quote_name(name) for name in children))
    return "; ".join(parts) + "."


def _xml_name(name: str) -> str:
    return str(name)


def _xml_xpath(path: tuple[str, ...]) -> str:
    if not path:
        return "xpath:/"
    head = "/".join(path)
    if path[-1] == "#text":
        head = "/".join(path[:-1]) + "/text()"
    return "xpath:/" + head


def _xml_path_label(path: tuple[str, ...]) -> str:
    return "/" + "/".join(path)


# ---------------------------------------------------------------------------
# CSV and TSV column profiling
# ---------------------------------------------------------------------------


def _extract_delimited(path: Path, *, delimiter: str, kind: str) -> Extraction:
    source = str(path)
    result = Extraction(source=source, kind=kind)

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as sample_stream:
            sample = sample_stream.read(65536)
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream, delimiter=delimiter, strict=True)
            leading_blank_rows = 0
            first = next(rows, None)
            while first is not None and (
                not first or not any(cell.strip() for cell in first)
            ):
                leading_blank_rows += 1
                first = next(rows, None)
            if first is None:
                result.gaps.append(
                    f"empty {kind.upper()} input: no table schema is present"
                )
                result.meta.update(
                    {
                        "bytes": path.stat().st_size,
                        "rows": 0,
                        "columns": 0,
                        "delimiter": delimiter,
                        "blank_rows": leading_blank_rows,
                    }
                )
                return result

            has_header = _has_header(sample, first)
            width = len(first)
            if width > _MAX_DELIMITED_COLUMNS:
                raise InvalidStructuredData(
                    path,
                    kind.upper(),
                    f"table has {width} columns; limit is "
                    f"{_MAX_DELIMITED_COLUMNS}",
                )
            raw_headers = (
                first
                if has_header
                else [f"column_{index}" for index in range(1, width + 1)]
            )
            headers, header_gaps = _normalize_headers(raw_headers)
            result.gaps.extend(header_gaps)
            profiles = [_ScalarStats() for _ in range(width)]
            malformed_count = 0
            malformed_shown: list[int] = []
            data_rows = 0

            blank_rows = leading_blank_rows

            if not has_header:
                _profile_row(first, profiles)
                data_rows += 1
                result.gaps.append(
                    "no header row was inferred; generated positional column names"
                )

            for row in rows:
                if not row or not any(cell.strip() for cell in row):
                    blank_rows += 1
                    continue
                data_rows += 1
                if len(row) != width:
                    malformed_count += 1
                    if len(malformed_shown) < 8:
                        malformed_shown.append(rows.line_num)
                normalized = row[:width] + [""] * max(0, width - len(row))
                _profile_row(normalized, profiles)
    except UnicodeDecodeError as exc:
        raise InvalidStructuredData(
            path, kind.upper(), f"input is not UTF-8 at byte {exc.start}"
        ) from exc
    except csv.Error as exc:
        raise InvalidStructuredData(path, kind.upper(), str(exc)) from exc

    table_ref = "table:"
    table = Unit(
        source=source,
        modality=Modality.SCHEMA,
        content=(
            f"{kind.upper()} table: {width} column(s), {data_rows} data row(s); "
            f"columns {', '.join(_quote_name(name) for name in headers)}."
        ),
        origin=Origin(source, table_ref),
        role=Role.UNKNOWN,
        salience=0.9,
        meta={
            "table_summary": True,
            "rows": data_rows,
            "columns": width,
            "delimiter": delimiter,
            "header": has_header,
        },
    )
    result.units.append(table)

    for index, (name, profile) in enumerate(zip(headers, profiles, strict=True), start=1):
        ref = f"column:{index}"
        unit = Unit(
            source=source,
            modality=Modality.SCHEMA,
            content=_describe_column(name, profile, data_rows),
            origin=Origin(source, ref),
            role=Role.UNKNOWN,
            structure=(name,),
            salience=0.7,
            meta={
                "column": index,
                "name": name,
                "rows": data_rows,
                "non_null": profile.seen - profile.nulls,
                "nulls": profile.nulls,
                "types": sorted(profile.types),
                **_scalar_meta(profile),
            },
        )
        result.units.append(unit)
        result.relations.append(
            Relation(
                src=table.id,
                dst=unit.id,
                kind=RelationKind.DESCRIBES,
                evidence=f"{ref} is a declared column of {table_ref}",
            )
        )

    if not data_rows:
        result.gaps.append("header only: no data rows are available to infer column types")
    if blank_rows:
        result.gaps.append(f"{blank_rows} blank row(s) were skipped")
    if malformed_count:
        shown = ", ".join(str(line) for line in malformed_shown)
        more = (
            f" and {malformed_count - len(malformed_shown)} more"
            if malformed_count > len(malformed_shown)
            else ""
        )
        result.gaps.append(
            f"{malformed_count} row(s) have a different field count "
            f"(lines {shown}{more})"
        )

    result.meta.update(
        {
            "bytes": path.stat().st_size,
            "rows": data_rows,
            "columns": width,
            "delimiter": delimiter,
            "header": has_header,
            "malformed_rows": malformed_count,
            **({"blank_rows": blank_rows} if blank_rows else {}),
        }
    )
    return result


def _has_header(sample: str, first: list[str]) -> bool:
    if not first:
        return False
    try:
        sniffed = csv.Sniffer().has_header(sample)
    except csv.Error:
        sniffed = False
    if sniffed:
        return True

    # Sniffer votes against common mixed tables when a nullable numeric column
    # changes type in the sample. A first row made entirely of nonempty text is
    # the conventional CSV/TSV header shape; treating it as such is also safer
    # than promoting those names to anonymous data values. Truly headerless
    # numeric and mixed-type files still take the positional-column path.
    return all(
        cell.strip() and _coerce_text_scalar(cell)[1] == "string" for cell in first
    )


def _normalize_headers(raw_headers: list[str]) -> tuple[list[str], list[str]]:
    headers: list[str] = []
    gaps: list[str] = []
    counts: Counter[str] = Counter()
    for index, raw in enumerate(raw_headers, start=1):
        base = raw.strip()
        if not base:
            base = f"column_{index}"
            gaps.append(f"column {index} has a blank header; using {base!r}")
        counts[base] += 1
        name = base if counts[base] == 1 else f"{base} [{counts[base]}]"
        if counts[base] > 1:
            gaps.append(f"duplicate header {base!r} at column {index}; using {name!r}")
        headers.append(name)
    return headers, gaps


def _profile_row(row: list[str], profiles: list[_ScalarStats]) -> None:
    for profile, raw in zip(profiles, row, strict=True):
        value, kind = _coerce_text_scalar(raw)
        profile.observe(value, type_name=kind)


def _describe_column(name: str, profile: _ScalarStats, rows: int) -> str:
    parts = [f"Column {_quote_name(name)}: {_type_summary(profile.types)}"]
    parts.append(f"{profile.seen - profile.nulls} non-null and {profile.nulls} null of {rows} row(s)")
    parts.extend(_scalar_findings(profile))
    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Shared scalar and formatting helpers
# ---------------------------------------------------------------------------


def _read_text(path: Path, kind: str, *, encoding: str = "utf-8") -> str:
    try:
        with path.open("r", encoding=encoding, newline="") as stream:
            return stream.read()
    except UnicodeDecodeError as exc:
        raise InvalidStructuredData(
            path, kind, f"input is not {encoding.upper()} at byte {exc.start}"
        ) from exc


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, Decimal)):
        return "number"
    if isinstance(value, dt.datetime):
        return "datetime"
    if isinstance(value, dt.date):
        return "date"
    if isinstance(value, dt.time):
        return "time"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _coerce_text_scalar(raw: str) -> tuple[Any, str]:
    value = raw.strip()
    if not value or value.casefold() in {"null", "none"}:
        return None, "null"
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true", "boolean"
    if _INTEGER.fullmatch(value):
        try:
            return int(value), "integer"
        except ValueError:
            pass
    if _NUMBER.fullmatch(value):
        try:
            return Decimal(value), "number"
        except InvalidOperation:
            pass
    # ISO temporal values are structure, not arbitrary strings. Require the
    # familiar separators to avoid classifying identifiers as dates or times.
    if "T" in value or " " in value:
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")), "datetime"
        except ValueError:
            pass
    if "-" in value:
        try:
            return dt.date.fromisoformat(value), "date"
        except ValueError:
            pass
    if ":" in value:
        try:
            return dt.time.fromisoformat(value), "time"
        except ValueError:
            pass
    return value, "string"


def _type_summary(types: Counter[str]) -> str:
    names = set(types)
    if names <= {"integer", "number"} and names:
        return "number" if "number" in names else "integer"
    ordered = [
        name
        for name in (
            "object",
            "array",
            "element",
            "boolean",
            "integer",
            "number",
            "datetime",
            "date",
            "time",
            "string",
            "null",
        )
        if name in names
    ]
    ordered.extend(sorted(names - set(ordered)))
    return " or ".join(ordered) if ordered else "unknown"


def _scalar_findings(stats: _ScalarStats) -> list[str]:
    parts: list[str] = []
    if stats.numeric_count:
        parts.append(f"range {_range_text(stats.numeric_min, stats.numeric_max)}")
    if stats.string_min_length is not None:
        parts.append(
            "string length "
            + _range_text(stats.string_min_length, stats.string_max_length)
        )

    distinct = len(stats.distinct)
    if stats.distinct_overflow:
        parts.append(f"at least {distinct + 1} distinct values")
    elif distinct:
        parts.append(f"{distinct} distinct value(s)")
        shown = list(stats.distinct.values())
        if distinct <= _MAX_SHOWN_VALUES and sum(map(len, shown)) <= 160:
            prefix = "constant " if distinct == 1 else "values "
            parts.append(prefix + ", ".join(shown))
    return parts


def _scalar_meta(stats: _ScalarStats) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if stats.numeric_count:
        meta["numeric"] = {
            "min": _plain_scalar(stats.numeric_min),
            "max": _plain_scalar(stats.numeric_max),
            "mean": _plain_scalar(stats.numeric_sum / stats.numeric_count),
        }
    if stats.string_min_length is not None:
        meta["string_length"] = {
            "min": stats.string_min_length,
            "max": stats.string_max_length,
        }
    if stats.distinct_overflow:
        meta["distinct_at_least"] = len(stats.distinct) + 1
    else:
        meta["distinct"] = len(stats.distinct)
    if stats.distinct and not stats.distinct_overflow and len(stats.distinct) <= _MAX_SHOWN_VALUES:
        meta["values"] = list(stats.distinct.values())
    return meta


def _canonical_scalar(value: Any) -> str:
    if isinstance(value, Decimal):
        return "decimal:" + str(value)
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return type(value).__name__ + ":" + value.isoformat()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return repr(value)


def _display_scalar(value: Any) -> str:
    if isinstance(value, str):
        shown = value if len(value) <= 60 else value[:57] + "…"
        return json.dumps(shown, ensure_ascii=False)
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, (dt.date, dt.time, dt.datetime)):
        return value.isoformat()
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _plain_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _format_decimal(value)
    return value


def _format_decimal(value: Decimal) -> str:
    if not value.is_finite():
        return str(value)
    normalized = value.normalize()
    return format(normalized, "f")


def _range_text(low: Any, high: Any) -> str:
    low_text = _display_scalar(low)
    high_text = _display_scalar(high)
    return low_text if low == high else f"{low_text} to {high_text}"


def _quote_name(name: str) -> str:
    return json.dumps(name, ensure_ascii=False)
