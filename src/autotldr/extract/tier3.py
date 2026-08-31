"""Native, bounded extraction for the remaining locked Tier 3 formats.

Tier 3 inputs are data-shaped.  Their meaning is their schema, declared
relationships, storage shape, metadata, and bounded profiles--not a rendering
of their rows or array payloads.  This module therefore never emits raw table
rows, HDF5 dataset values, or NetCDF variable values.

The module itself is deliberately stdlib-only.  Each optional parser is
imported inside the one adapter that needs it; SQLite uses the stdlib driver in
read-only immutable mode.  This keeps the Tier 0 import graph cold.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

# These limits are part of the extractor contract, not performance hints.  A
# caller may lower them in a controlled test, but adapters never silently walk
# an unbounded object graph or return an unbounded schema.
_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_UNITS = 2_048
_MAX_RELATIONS = 4_096
_MAX_GAPS = 256
_MAX_OBJECTS = 2_048
_MAX_DEPTH = 32
_MAX_ATTRIBUTES = 512
_MAX_ATTRIBUTE_BYTES = 16 * 1024
_MAX_TEXT_CHARS = 1_024
_MAX_COLUMNS_PER_TABLE = 512
_MAX_STATS_COLUMNS_PER_TABLE = 64
_MAX_SAMPLE_ROWS = 256
_MAX_PARQUET_ROW_GROUPS = 256
_MAX_PARQUET_METADATA_BYTES = 64 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024

# Populated only after the lazy NetCDF adapter imports its optional runtime.
# Holding the resolved C function avoids reopening the extension library for
# every attribute while keeping the Tier 0 import graph stdlib-only.
_NETCDF_INQ_ATT = None

KIND_ALIASES: dict[str, str] = {
    "parquet": "parquet",
    "pq": "parquet",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "db3": "sqlite",
    "s3db": "sqlite",
    "duckdb": "duckdb",
    "hdf5": "hdf5",
    "hdf": "hdf5",
    "h5": "hdf5",
    "he5": "hdf5",
    "netcdf": "netcdf",
    "netcdf4": "netcdf",
    "nc": "netcdf",
    "nc4": "netcdf",
}

_SUFFIX_KINDS: dict[str, str] = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".db3": "sqlite",
    ".s3db": "sqlite",
    ".duckdb": "duckdb",
    ".hdf5": "hdf5",
    ".hdf": "hdf5",
    ".h5": "hdf5",
    ".he5": "hdf5",
    ".nc": "netcdf",
    ".nc4": "netcdf",
    ".netcdf": "netcdf",
}

_DISPLAY_KIND = {
    "parquet": "Parquet",
    "sqlite": "SQLite",
    "duckdb": "DuckDB",
    "hdf5": "HDF5",
    "netcdf": "NetCDF",
}


class InvalidTier3Data(ValueError):
    """A recognized Tier 3 file is corrupt, unsafe, empty, or over a bound."""

    tier = 3

    def __init__(self, path: Path, kind: str, detail: str) -> None:
        self.path = path
        self.kind = _DISPLAY_KIND.get(kind, kind)
        self.detail = detail
        super().__init__(f"{path.name}: invalid {self.kind}: {detail}")


class UnsupportedTier3Subtype(ValueError):
    """A parser opened the container but its subtype is outside the v1 lock."""

    tier = 3

    def __init__(self, path: Path, kind: str, subtype: str) -> None:
        self.path = path
        self.kind = _DISPLAY_KIND.get(kind, kind)
        self.subtype = subtype
        super().__init__(
            f"{path.name}: unsupported {self.kind} subtype {subtype!r} (tier 3)"
        )


@dataclass(frozen=True, slots=True)
class _ReadContext:
    path: Path
    identity: tuple[int, int, int, int]
    result: Extraction


class _Emitter:
    """Bound the representation as well as the native traversal."""

    __slots__ = (
        "result",
        "suppressed_units",
        "suppressed_relations",
        "suppressed_gaps",
        "unit_ids",
    )

    def __init__(self, result: Extraction) -> None:
        self.result = result
        self.suppressed_units = 0
        self.suppressed_relations = 0
        self.suppressed_gaps = 0
        self.unit_ids: set[str] = set()

    def unit(self, unit: Unit) -> bool:
        if len(self.result.units) >= _MAX_UNITS:
            self.suppressed_units += 1
            return False
        self.result.units.append(unit)
        self.unit_ids.add(unit.id)
        return True

    def relation(self, relation: Relation) -> bool:
        if (
            len(self.result.relations) >= _MAX_RELATIONS
            or relation.src not in self.unit_ids
            or relation.dst not in self.unit_ids
        ):
            self.suppressed_relations += 1
            return False
        self.result.relations.append(relation)
        return True

    def gap(self, content: str, *, ref: str = "source") -> None:
        # Reserve the last slot for the combined truncation finding.
        if len(self.result.gaps) >= _MAX_GAPS - 1:
            self.suppressed_gaps += 1
            return
        self.result.add_gap(content, ref=ref)

    def finish(self) -> None:
        truncated = {
            "units": self.suppressed_units,
            "relations": self.suppressed_relations,
            "gaps": self.suppressed_gaps,
        }
        if any(truncated.values()):
            parts = [f"{count} {name}" for name, count in truncated.items() if count]
            self.result.add_gap(
                "representation bounds suppressed " + ", ".join(parts),
                ref="source",
            )
            self.result.meta["truncated"] = truncated


def extract(path: Path, *, kind: str | None = None) -> Extraction:
    """Dispatch a Tier 3 path by an explicit kind or an unambiguous suffix.

    ``.db`` is intentionally absent: both SQLite and DuckDB commonly use it,
    so router integration must select one from the native file signature or an
    explicit input kind rather than guessing from that suffix.
    """

    path = Path(path)
    if kind is None:
        canonical = _SUFFIX_KINDS.get(path.suffix.casefold())
    else:
        canonical = KIND_ALIASES.get(kind.casefold().lstrip("."))
    if canonical is None:
        label = kind if kind is not None else (path.suffix or "extensionless input")
        raise ValueError(
            f"Tier 3 extractor does not handle {label!r}; choose one of "
            + ", ".join(sorted(KIND_ALIASES))
        )
    return {
        "parquet": extract_parquet,
        "sqlite": extract_sqlite,
        "duckdb": extract_duckdb,
        "hdf5": extract_hdf5,
        "netcdf": extract_netcdf,
    }[canonical](path)


# ---------------------------------------------------------------------------
# Common file and representation helpers
# ---------------------------------------------------------------------------


def _begin(path: Path, kind: str) -> tuple[_ReadContext, _Emitter]:
    path = Path(path)
    try:
        info = path.stat()
    except OSError as exc:
        raise InvalidTier3Data(
            path, kind, f"stat failed ({exc.__class__.__name__})"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise InvalidTier3Data(path, kind, "input is not a regular file")
    if info.st_size == 0:
        raise InvalidTier3Data(path, kind, "file is empty")
    if info.st_size > _MAX_FILE_BYTES:
        raise InvalidTier3Data(
            path,
            kind,
            f"file is {info.st_size} bytes; limit is {_MAX_FILE_BYTES} bytes",
        )

    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                byte_count += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise InvalidTier3Data(
            path, kind, f"read failed ({exc.__class__.__name__})"
        ) from exc

    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if byte_count != info.st_size or _identity(path) != identity:
        raise InvalidTier3Data(path, kind, "source changed while it was fingerprinted")

    source = str(path)
    result = Extraction(source=source, kind=kind)
    result.meta.update(
        {
            "inputs": [
                {
                    "source": source,
                    "kind": kind,
                    "tier": 3,
                    "bytes": byte_count,
                    "sha256": digest.hexdigest(),
                }
            ],
            "extractor": {
                "name": "native-tier3-v1",
                "bounds": {
                    "file_bytes": _MAX_FILE_BYTES,
                    "objects": _MAX_OBJECTS,
                    "depth": _MAX_DEPTH,
                    "units": _MAX_UNITS,
                    "relations": _MAX_RELATIONS,
                    "sample_rows": _MAX_SAMPLE_ROWS,
                },
            },
        }
    )
    context = _ReadContext(path=path, identity=identity, result=result)
    return context, _Emitter(result)


def _finish(context: _ReadContext, emitter: _Emitter) -> Extraction:
    if _identity(context.path) != context.identity:
        raise InvalidTier3Data(
            context.path,
            context.result.kind,
            "source changed while it was being extracted",
        )
    emitter.finish()
    result = context.result
    # Ordering must not depend on the physical path this extractor was handed.
    # Unit IDs are derived from the immutable snapshot's temporary directory and
    # are rewritten to logical IDs by the router afterwards, so sorting by them
    # would leave the list in an order that varies from run to run. Rank by the
    # canonical unit position instead, which survives that rewrite unchanged.
    result.units.sort(
        key=lambda unit: (unit.origin.ref, str(unit.modality), unit.content)
    )
    unit_order = {unit.id: index for index, unit in enumerate(result.units)}
    result.relations.sort(
        key=lambda relation: (
            unit_order.get(relation.src, 0),
            unit_order.get(relation.dst, 0),
            str(relation.kind),
            relation.evidence,
        )
    )
    result.gaps.sort(key=lambda gap: (gap.origin.ref, str(gap.kind), gap.content))
    ids = {unit.id for unit in result.units}
    dangling = [
        relation
        for relation in result.relations
        if relation.src not in ids or relation.dst not in ids
    ]
    if dangling:
        raise AssertionError("Tier 3 extractor emitted a dangling relation")
    result.meta["counts"] = {
        "units": len(result.units),
        "relations": len(result.relations),
        "gaps": len(result.gaps),
    }
    return result


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _component(value: object) -> str:
    return quote(str(value), safe="-._~")


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _bounded_catalog_names(values: Iterable[object], limit: int) -> tuple[list[str], bool]:
    """Read at most ``limit + 1`` names from a deterministic native catalog.

    HDF5 and NetCDF expose path/catalog mappings whose iteration order is part
    of the stored file.  Sorting an entire mapping before applying our object
    limit defeats that limit for hostile files.  Keep only a bounded prefix,
    then sort that prefix so emitted order is canonical without an unbounded
    intermediate allocation.
    """

    if limit < 0:
        raise ValueError("catalog-name limit must be non-negative")
    selected = [str(value) for value in islice(values, limit + 1)]
    truncated = len(selected) > limit
    if truncated:
        del selected[limit:]
    selected.sort()
    return selected, truncated


def _safe_scalar(value: object) -> object:
    """Return JSON-safe scalar metadata without expanding an array value."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": len(value)}
    shape = getattr(value, "shape", None)
    if shape is not None:
        try:
            normalized_shape = tuple(int(part) for part in shape)
        except (TypeError, ValueError):
            normalized_shape = ()
        if normalized_shape:
            return {
                "array": True,
                "shape": list(normalized_shape),
                "dtype": str(getattr(value, "dtype", type(value).__name__)),
            }
        item = getattr(value, "item", None)
        if callable(item):
            try:
                return _safe_scalar(item())
            except (TypeError, ValueError, OverflowError):
                pass
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return _bounded_text(isoformat())
        except (TypeError, ValueError):
            pass
    return _bounded_text(value)


def _display_scalar(value: object) -> str:
    normalized = value if isinstance(value, (dict, list)) else _safe_scalar(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _safe_attribute_value(name: str, value: object) -> object:
    """Preserve at most two declared range endpoints, never dataset arrays."""

    if name.casefold() in _STATISTIC_ATTRIBUTES:
        shape = getattr(value, "shape", None)
        size = getattr(value, "size", None)
        if shape is not None and size is not None and 0 < int(size) <= 2:
            to_list = getattr(value, "tolist", None)
            if callable(to_list):
                raw = to_list()
                values = raw if isinstance(raw, list) else [raw]
                return [_safe_scalar(item) for item in values]
        if isinstance(value, (list, tuple)) and 0 < len(value) <= 2:
            return [_safe_scalar(item) for item in value]
    return _safe_scalar(value)


def _schema_unit(
    source: str,
    *,
    content: str,
    ref: str,
    structure: tuple[str, ...] = (),
    salience: float = 0.7,
    meta: dict[str, Any] | None = None,
    modality: Modality = Modality.SCHEMA,
) -> Unit:
    return Unit(
        source=source,
        modality=modality,
        content=content,
        origin=Origin(source, ref),
        role=Role.UNKNOWN,
        structure=structure,
        salience=salience,
        meta=meta or {},
    )


def _describe_relation(parent: Unit, child: Unit, evidence: str) -> Relation:
    return Relation(
        src=parent.id,
        dst=child.id,
        kind=RelationKind.DESCRIBES,
        evidence=evidence,
    )


def _error_detail(exc: BaseException) -> str:
    """Return stable parser context without reflecting untrusted exception text.

    Optional parsers commonly include absolute paths, SQL fragments, metadata
    values, or byte snippets in ``str(exc)``.  Call sites already identify the
    object and parser phase, so the exception class is the only safe additional
    diagnostic to expose in an extraction or named decline.
    """

    return f"parser reported {exc.__class__.__name__}"


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------


def extract_parquet(path: Path) -> Extraction:
    """Read Parquet footer/schema metadata and row-group statistics only."""

    context, emitter = _begin(Path(path), "parquet")
    try:
        import pyarrow.parquet as parquet
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
        if exc.name not in {"pyarrow", "pyarrow.parquet"}:
            raise
        raise ImportError(
            "Parquet support requires pyarrow; install it with: pip install pyarrow"
        ) from exc

    _validate_parquet_envelope(context.path)

    try:
        parquet_file = parquet.ParquetFile(context.path)
        metadata = parquet_file.metadata
        schema = parquet_file.schema
        arrow_field_metadata = _parquet_arrow_field_metadata(parquet_file)
        num_columns = int(metadata.num_columns)
        num_row_groups = int(metadata.num_row_groups)
        num_rows = int(metadata.num_rows)
    except Exception as exc:
        raise InvalidTier3Data(context.path, "parquet", _error_detail(exc)) from exc

    source = context.result.source
    table = _schema_unit(
        source,
        content=(
            f"Parquet file: {num_rows} row(s), {num_columns} column(s), "
            f"{num_row_groups} row group(s)."
        ),
        ref="parquet:file",
        salience=0.95,
        meta={
            "file_summary": True,
            "rows": num_rows,
            "columns": num_columns,
            "row_groups": num_row_groups,
            "created_by": _bounded_text(metadata.created_by) if metadata.created_by else None,
            "format_version": str(metadata.format_version),
        },
    )
    emitter.unit(table)

    if num_rows == 0:
        emitter.gap("Parquet file has a schema but contains no rows", ref="parquet:file")
    if num_columns == 0:
        emitter.gap("Parquet file declares no columns", ref="parquet:file")

    column_limit = min(num_columns, _MAX_COLUMNS_PER_TABLE, _MAX_OBJECTS)
    if num_columns > column_limit:
        emitter.gap(
            f"Parquet schema has {num_columns} columns; only the first "
            f"{column_limit} in native schema order were emitted",
            ref="parquet:file",
        )
    row_group_limit = min(num_row_groups, _MAX_PARQUET_ROW_GROUPS)
    if num_row_groups > row_group_limit:
        emitter.gap(
            f"Parquet statistics cover the first {row_group_limit} of "
            f"{num_row_groups} row groups",
            ref="parquet:file",
        )

    missing_stats: list[str] = []
    column_units: dict[str, Unit] = {}
    metadata_names: list[str] = []
    for index in range(column_limit):
        try:
            declared = schema.column(index)
            path_in_schema = str(getattr(declared, "path", None) or declared.name)
            physical = str(declared.physical_type)
            logical = str(declared.logical_type)
            stats = _parquet_column_stats(
                metadata,
                index=index,
                row_group_limit=row_group_limit,
                allow_range=_parquet_range_is_semantic(physical, logical),
            )
        except Exception as exc:
            raise InvalidTier3Data(
                context.path,
                "parquet",
                f"column {index} metadata is invalid: {_error_detail(exc)}",
            ) from exc
        if stats["row_groups_with_statistics"] == 0 and num_rows:
            missing_stats.append(path_in_schema)
        findings = [
            f"Parquet column {path_in_schema!r}: physical type {physical}",
            f"logical type {logical}",
            f"{stats['values']} encoded value(s)",
            f"{stats['nulls']} declared null(s)"
            if stats["nulls"] is not None
            else "null count is not recorded",
        ]
        if stats["min"] is not None or stats["max"] is not None:
            findings.append(
                f"metadata range {_display_scalar(stats['min'])} to "
                f"{_display_scalar(stats['max'])}"
            )
        findings.append(
            f"{stats['compressed_bytes']} compressed byte(s), "
            f"{stats['uncompressed_bytes']} uncompressed byte(s)"
        )
        unit = _schema_unit(
            source,
            content="; ".join(findings) + ".",
            ref=f"column:{_component(path_in_schema)}",
            structure=tuple(part for part in path_in_schema.split(".") if part),
            meta={
                "column_index": index,
                "path": path_in_schema,
                "physical_type": physical,
                "logical_type": logical,
                **stats,
            },
        )
        if emitter.unit(unit):
            column_units[path_in_schema] = unit
            emitter.relation(
                _describe_relation(
                    table,
                    unit,
                    f"column {path_in_schema!r} is declared in the Parquet schema",
                )
            )

    attribute_count = 0
    for field_path in sorted(arrow_field_metadata):
        owner = column_units.get(field_path)
        if owner is None:
            continue
        for raw_name, raw_value in sorted(
            arrow_field_metadata[field_path].items(),
            key=lambda item: _decode_metadata_bytes(item[0]),
        ):
            if attribute_count >= _MAX_ATTRIBUTES:
                break
            name = _decode_metadata_bytes(raw_name)
            value = _decode_metadata_bytes(raw_value)
            value_size = (
                len(raw_value) if isinstance(raw_value, bytes) else len(str(raw_value))
            )
            metadata_names.append(name.casefold())
            attribute_count += 1
            shown = _bounded_text(value, _MAX_TEXT_CHARS)
            attribute = _schema_unit(
                source,
                content=(
                    f"Parquet field metadata {name!r} on {field_path!r}: {shown!r}."
                    if value_size <= _MAX_ATTRIBUTE_BYTES
                    else f"Parquet field metadata {name!r} on {field_path!r}: "
                    f"{value_size} bytes; value omitted."
                ),
                ref=(
                    f"column:{_component(field_path)}"
                    f"#attribute:{_component(name)}"
                ),
                structure=tuple(part for part in field_path.split(".") if part)
                + ("attributes",),
                salience=0.65 if name.casefold() in _SEMANTIC_ATTRIBUTES else 0.45,
                meta={
                    "object_type": "attribute",
                    "owner": field_path,
                    "name": name,
                    "bytes": value_size,
                    **({"value": shown} if value_size <= _MAX_ATTRIBUTE_BYTES else {}),
                },
            )
            if emitter.unit(attribute):
                emitter.relation(
                    _describe_relation(
                        owner,
                        attribute,
                        f"field metadata {name!r} on {field_path!r}",
                    )
                )
        if attribute_count >= _MAX_ATTRIBUTES:
            break

    raw_metadata = metadata.metadata or {}
    metadata_items = sorted(
        raw_metadata.items(), key=lambda item: bytes(item[0]) if item[0] else b""
    )
    remaining_attributes = max(0, _MAX_ATTRIBUTES - attribute_count)
    if len(metadata_items) > remaining_attributes:
        emitter.gap(
            f"Parquet field/file metadata exceeds the {_MAX_ATTRIBUTES}-attribute "
            "bound; later entries were not emitted",
            ref="parquet:file",
        )
    for raw_name, raw_value in metadata_items[:remaining_attributes]:
        name = _decode_metadata_bytes(raw_name)
        metadata_names.append(name.casefold())
        value_size = len(raw_value) if isinstance(raw_value, bytes) else len(str(raw_value))
        value = _decode_metadata_bytes(raw_value)
        shown = _bounded_text(value, _MAX_TEXT_CHARS)
        unit = _schema_unit(
            source,
            content=(
                f"Parquet metadata {name!r}: {shown!r}."
                if value_size <= _MAX_ATTRIBUTE_BYTES
                else f"Parquet metadata {name!r}: {value_size} bytes; value omitted."
            ),
            ref=f"metadata:{_component(name)}",
            structure=("metadata",),
            salience=0.45,
            meta={
                "attribute": name,
                "bytes": value_size,
                **({"value": shown} if value_size <= _MAX_ATTRIBUTE_BYTES else {}),
            },
        )
        if emitter.unit(unit):
            emitter.relation(
                _describe_relation(table, unit, f"file metadata key {name!r}")
            )

    folded_metadata = " ".join(metadata_names)
    if not any(token in folded_metadata for token in ("description", "title", "comment")):
        emitter.gap(
            "Parquet metadata contains no title, description, or comment",
            ref="parquet:file",
        )
    if "unit" not in folded_metadata:
        emitter.gap(
            "Parquet metadata declares no column units",
            ref="parquet:file",
        )
    emitter.gap(
        "Parquet has no standardized primary-key or foreign-key metadata",
        ref="parquet:file",
    )
    if missing_stats:
        shown = ", ".join(repr(name) for name in missing_stats[:8])
        more = len(missing_stats) - min(len(missing_stats), 8)
        emitter.gap(
            f"no footer statistics for {shown}"
            + (f" and {more} more column(s)" if more else ""),
            ref="parquet:file",
        )

    context.result.meta.update(
        {
            "rows": num_rows,
            "columns": num_columns,
            "row_groups": num_row_groups,
            "statistics_row_groups": row_group_limit,
            "metadata_entries": len(metadata_items),
            "field_metadata_entries": sum(
                len(values) for values in arrow_field_metadata.values()
            ),
        }
    )
    return _finish(context, emitter)


def _parquet_column_stats(
    metadata,
    *,
    index: int,
    row_group_limit: int,
    allow_range: bool,
) -> dict[str, Any]:
    values = 0
    nulls: int | None = 0
    compressed = 0
    uncompressed = 0
    minima: list[object] = []
    maxima: list[object] = []
    with_stats = 0
    encodings: set[str] = set()
    compressions: set[str] = set()
    for row_group_index in range(row_group_limit):
        column = metadata.row_group(row_group_index).column(index)
        values += int(column.num_values)
        compressed += int(column.total_compressed_size)
        uncompressed += int(column.total_uncompressed_size)
        compressions.add(str(column.compression))
        encodings.update(str(item) for item in column.encodings)
        statistics = column.statistics
        if statistics is None:
            nulls = None
            continue
        with_stats += 1
        count = statistics.null_count
        if count is None:
            nulls = None
        elif nulls is not None:
            nulls += int(count)
        # String/binary min/max values are row payload, not structural
        # statistics.  Numeric, boolean, and temporal ranges are semantic and
        # bounded, so only those enter the representation.
        if allow_range and bool(getattr(statistics, "has_min_max", False)):
            minima.append(statistics.min)
            maxima.append(statistics.max)
    minimum = _safe_min(minima)
    maximum = _safe_max(maxima)
    return {
        "values": values,
        "nulls": nulls,
        "min": _safe_scalar(minimum) if minimum is not None else None,
        "max": _safe_scalar(maximum) if maximum is not None else None,
        "compressed_bytes": compressed,
        "uncompressed_bytes": uncompressed,
        "compression": sorted(compressions),
        "encodings": sorted(encodings),
        "row_groups_profiled": row_group_limit,
        "row_groups_with_statistics": with_stats,
        "range_suppressed": not allow_range,
    }


def _parquet_range_is_semantic(physical: str, logical: str) -> bool:
    if physical.upper() in {"BOOLEAN", "INT32", "INT64", "FLOAT", "DOUBLE"}:
        return True
    folded = logical.casefold()
    return any(token in folded for token in ("date", "time", "decimal")) and not any(
        token in folded for token in ("string", "json", "bson", "uuid")
    )


def _safe_min(values: Iterable[object]) -> object | None:
    materialized = list(values)
    if not materialized:
        return None
    try:
        return min(materialized)
    except (TypeError, ValueError):
        return min((_bounded_text(value) for value in materialized), default=None)


def _safe_max(values: Iterable[object]) -> object | None:
    materialized = list(values)
    if not materialized:
        return None
    try:
        return max(materialized)
    except (TypeError, ValueError):
        return max((_bounded_text(value) for value in materialized), default=None)


def _decode_metadata_bytes(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            # Replacement decoding is lossy: distinct native keys can become
            # the same text and therefore the same Origin.  Preserve a stable,
            # non-reversible native identity without emitting arbitrary bytes.
            digest = hashlib.sha256(value).hexdigest()
            return f"binary:{len(value)}:sha256:{digest}"
    return str(value)


def _parquet_arrow_field_metadata(parquet_file) -> dict[str, Mapping[object, object]]:
    """Return bounded field annotations without importing Arrow at module scope."""

    try:
        schema = parquet_file.schema_arrow
    except Exception:
        return {}
    found: dict[str, Mapping[object, object]] = {}

    def visit(field, prefix: tuple[str, ...]) -> None:
        path = prefix + (str(field.name),)
        metadata = getattr(field, "metadata", None)
        if metadata:
            found[".".join(path)] = metadata
        field_type = getattr(field, "type", None)
        count = int(getattr(field_type, "num_fields", 0) or 0)
        if len(path) >= _MAX_DEPTH:
            return
        for index in range(min(count, _MAX_COLUMNS_PER_TABLE)):
            visit(field_type.field(index), path)

    for field in schema:
        if len(found) >= _MAX_ATTRIBUTES:
            break
        visit(field, ())
    return found


def _validate_parquet_envelope(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            leading = stream.read(4)
            stream.seek(-8, 2)
            trailer = stream.read(8)
    except (OSError, ValueError) as exc:
        raise InvalidTier3Data(path, "parquet", "file is too short for a footer") from exc
    magic = trailer[4:]
    if leading != b"PAR1" or magic != b"PAR1":
        if magic == b"PARE":
            raise UnsupportedTier3Subtype(path, "parquet", "encrypted footer")
        raise InvalidTier3Data(path, "parquet", "native PAR1 envelope is missing")
    footer_bytes = int.from_bytes(trailer[:4], "little", signed=False)
    size = path.stat().st_size
    if footer_bytes > _MAX_PARQUET_METADATA_BYTES:
        raise InvalidTier3Data(
            path,
            "parquet",
            f"footer is {footer_bytes} bytes; metadata limit is "
            f"{_MAX_PARQUET_METADATA_BYTES} bytes",
        )
    if footer_bytes + 12 > size:
        raise InvalidTier3Data(path, "parquet", "footer length exceeds file size")


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def extract_sqlite(path: Path) -> Extraction:
    """Inspect SQLite through a read-only immutable stdlib connection."""

    context, emitter = _begin(Path(path), "sqlite")
    _require_prefix(context.path, b"SQLite format 3\x00", "sqlite")
    for suffix in ("-wal", "-journal"):
        sidecar = context.path.with_name(context.path.name + suffix)
        try:
            sidecar_size = sidecar.stat().st_size
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InvalidTier3Data(
                context.path,
                "sqlite",
                f"sidecar inspection failed ({exc.__class__.__name__})",
            ) from exc
        if sidecar_size:
            raise InvalidTier3Data(
                context.path,
                "sqlite",
                f"non-empty {sidecar.name} sidecar cannot be represented by an "
                "immutable single-file manifest; checkpoint it first",
            )

    import sqlite3

    uri = context.path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = None
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=0.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        _extract_sqlite_connection(context, emitter, connection)
    except sqlite3.Error as exc:
        raise InvalidTier3Data(context.path, "sqlite", _error_detail(exc)) from exc
    finally:
        if connection is not None:
            connection.close()
    return _finish(context, emitter)


def _extract_sqlite_connection(context: _ReadContext, emitter: _Emitter, connection) -> None:
    source = context.result.source
    pragma = {
        "schema_version": int(connection.execute("PRAGMA schema_version").fetchone()[0]),
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
        "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
        "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
    }
    total_objects = int(
        connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ).fetchone()[0]
    )
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 "
        "WHEN 'index' THEN 2 ELSE 3 END, name COLLATE BINARY "
        f"LIMIT {_MAX_OBJECTS + 1}"
    ).fetchall()
    if len(rows) > _MAX_OBJECTS:
        rows = rows[:_MAX_OBJECTS]
        emitter.gap(
            f"SQLite schema has {total_objects} objects; only the first "
            f"{_MAX_OBJECTS} in deterministic schema order were inspected",
            ref="database:",
        )

    table_rows = [row for row in rows if row[0] in {"table", "view"}]
    summary = _schema_unit(
        source,
        content=(
            f"SQLite database: {len([r for r in table_rows if r[0] == 'table'])} "
            f"table(s), {len([r for r in table_rows if r[0] == 'view'])} view(s), "
            f"{total_objects} user schema object(s)."
        ),
        ref="database:",
        salience=0.95,
        meta={"database_summary": True, "schema_objects": total_objects, **pragma},
    )
    emitter.unit(summary)
    if not table_rows:
        emitter.gap(
            "SQLite database contains no user tables or views",
            ref="database:",
        )

    object_units: dict[tuple[str, str], Unit] = {}
    column_units: dict[tuple[str, str], Unit] = {}
    table_info: dict[str, dict[str, Any]] = {}
    documentation_seen = False
    units_seen = False
    tables_without_keys: list[str] = []
    empty_tables: list[str] = []

    for object_type, name, _table_name, sql in table_rows:
        name = str(name)
        sql_text = _bounded_text(sql or "", _MAX_TEXT_CHARS)
        documentation_seen |= bool(re.search(r"(?:--|/\*)", sql or ""))
        columns = connection.execute(
            f"PRAGMA table_xinfo({_sqlite_string(name)})"
        ).fetchall()
        if len(columns) > _MAX_COLUMNS_PER_TABLE:
            emitter.gap(
                f"{object_type} {name!r} has {len(columns)} columns; only the first "
                f"{_MAX_COLUMNS_PER_TABLE} were emitted",
                ref=f"{object_type}:{_component(name)}",
            )
        columns = columns[:_MAX_COLUMNS_PER_TABLE]
        primary = [
            (int(row[5]), str(row[1])) for row in columns if int(row[5] or 0) > 0
        ]
        primary.sort()
        without_rowid = "WITHOUT ROWID" in (sql or "").upper()
        virtual = (sql or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")
        ref = f"{object_type}:{_component(name)}"
        description = (
            f"SQLite {object_type} {name!r}: {len(columns)} column(s)"
            + (f", primary key ({', '.join(col for _, col in primary)})" if primary else "")
            + "."
        )
        unit = _schema_unit(
            source,
            content=description,
            ref=ref,
            structure=(name,),
            salience=0.9 if object_type == "table" else 0.75,
            meta={
                "object_type": object_type,
                "name": name,
                "columns": len(columns),
                "primary_key": [column for _, column in primary],
                "without_rowid": without_rowid,
                "virtual": virtual,
                **({"query": sql_text} if object_type == "view" and sql_text else {}),
            },
        )
        if emitter.unit(unit):
            object_units[(object_type, name.casefold())] = unit
            emitter.relation(
                _describe_relation(summary, unit, f"{object_type} {name!r} in sqlite_schema")
            )
        table_info[name.casefold()] = {
            "name": name,
            "type": object_type,
            "unit": unit if unit.id in {u.id for u in context.result.units} else None,
            "columns": columns,
            "primary": [column for _, column in primary],
            "without_rowid": without_rowid,
            "virtual": virtual,
            "sql": sql or "",
        }

        order_clause = _sqlite_sample_order(without_rowid, [column for _, column in primary])
        sampled_any = False
        for position, row in enumerate(columns):
            _cid, column_name, declared_type, not_null, default, pk_position, hidden = row
            column_name = str(column_name)
            declared_type = str(declared_type or "")
            units_seen |= bool(
                re.search(r"(?:^|_)(?:unit|units)$", column_name, re.IGNORECASE)
            )
            stats: dict[str, Any] = {}
            if (
                object_type == "table"
                and not virtual
                and position < _MAX_STATS_COLUMNS_PER_TABLE
            ):
                stats = _sqlite_column_stats(
                    connection,
                    table=name,
                    column=column_name,
                    order_clause=order_clause,
                )
                sampled_any |= bool(stats["sampled_rows"])
            content = [
                f"SQLite column {name!r}.{column_name!r}: "
                f"declared type {declared_type or 'unspecified'}",
                "not null" if not_null else "nullable",
            ]
            if pk_position:
                content.append(f"primary-key position {pk_position}")
            if default is not None:
                content.append(f"default {_bounded_text(default, 160)!r}")
            if stats:
                content.append(_sample_stats_text(stats))
            column_ref = f"table:{_component(name)}#column:{_component(column_name)}"
            column_unit = _schema_unit(
                source,
                content="; ".join(content) + ".",
                ref=column_ref,
                structure=(name, column_name),
                meta={
                    "table": name,
                    "column": column_name,
                    "ordinal": int(_cid),
                    "declared_type": declared_type or None,
                    "nullable": not bool(not_null),
                    "default": _bounded_text(default, 160) if default is not None else None,
                    "primary_key_position": int(pk_position or 0),
                    "hidden": int(hidden or 0),
                    **stats,
                },
            )
            if emitter.unit(column_unit):
                column_units[(name.casefold(), column_name.casefold())] = column_unit
                if table_info[name.casefold()]["unit"] is not None:
                    emitter.relation(
                        _describe_relation(
                            unit,
                            column_unit,
                            f"column {column_name!r} declared by {object_type} {name!r}",
                        )
                    )
        if (
            object_type == "table"
            and not virtual
            and columns
            and not sampled_any
        ):
            empty_tables.append(name)
        if object_type == "table" and len(columns) > _MAX_STATS_COLUMNS_PER_TABLE:
            emitter.gap(
                f"bounded profiles cover the first {_MAX_STATS_COLUMNS_PER_TABLE} "
                f"of {len(columns)} columns in table {name!r}",
                ref=ref,
            )

    foreign_key_count = 0
    for folded_name in sorted(table_info):
        info = table_info[folded_name]
        if info["type"] != "table":
            continue
        table_name = info["name"]
        table_unit = info["unit"]
        key_count = _emit_sqlite_keys(
            connection,
            emitter,
            source,
            table_name,
            table_unit,
            column_units,
            info["primary"],
        )
        if not key_count:
            tables_without_keys.append(table_name)
        foreign_key_count += _emit_sqlite_foreign_keys(
            connection,
            emitter,
            source,
            table_name,
            table_unit,
            column_units,
            table_info,
        )

    for folded_name in sorted(table_info):
        info = table_info[folded_name]
        if info["type"] != "view" or info["unit"] is None:
            continue
        for dependency in _sql_dependencies(info["sql"]):
            target_name = dependency.rsplit(".", 1)[-1]
            target = table_info.get(target_name.casefold())
            if target is None or target["unit"] is None:
                continue
            emitter.relation(
                Relation(
                    src=info["unit"].id,
                    dst=target["unit"].id,
                    kind=RelationKind.DERIVES_FROM,
                    evidence=f"view query names {dependency!r} after FROM or JOIN",
                    confidence=0.95,
                )
            )

    if not documentation_seen and table_rows:
        emitter.gap(
            "SQLite schema contains no stored table or column documentation comments",
            ref="database:",
        )
    if not units_seen and any(info["columns"] for info in table_info.values()):
        emitter.gap(
            "SQLite schema declares no explicit column-unit metadata",
            ref="database:",
        )
    if tables_without_keys:
        shown = ", ".join(repr(name) for name in tables_without_keys[:8])
        more = len(tables_without_keys) - min(8, len(tables_without_keys))
        emitter.gap(
            f"no primary or unique key is declared for {shown}"
            + (f" and {more} more table(s)" if more else ""),
            ref="database:",
        )
    if not foreign_key_count and any(info["type"] == "table" for info in table_info.values()):
        emitter.gap(
            "SQLite schema declares no foreign-key relationships",
            ref="database:",
        )
    for table_name in empty_tables:
        emitter.gap(
            f"SQLite table {table_name!r} contains no rows",
            ref=f"table:{_component(table_name)}",
        )

    context.result.meta.update(
        {
            "schema_objects": total_objects,
            "tables": sum(info["type"] == "table" for info in table_info.values()),
            "views": sum(info["type"] == "view" for info in table_info.values()),
            "foreign_keys": foreign_key_count,
            **pragma,
        }
    )


def _sqlite_column_stats(
    connection, *, table: str, column: str, order_clause: str
) -> dict[str, Any]:
    quoted_column = _quote_identifier(column)
    rows = connection.execute(
        "SELECT "
        f"typeof({quoted_column}), "
        f"CASE WHEN typeof({quoted_column}) IN ('integer','real') "
        f"THEN {quoted_column} ELSE NULL END, "
        f"CASE WHEN {quoted_column} IS NULL THEN NULL ELSE length({quoted_column}) END "
        f"FROM {_quote_identifier(table)}{order_clause} "
        f"LIMIT {_MAX_SAMPLE_ROWS + 1}"
    ).fetchall()
    truncated = len(rows) > _MAX_SAMPLE_ROWS
    rows = rows[:_MAX_SAMPLE_ROWS]
    types = Counter(str(row[0]) for row in rows)
    numeric = [row[1] for row in rows if row[1] is not None]
    lengths = [int(row[2]) for row in rows if row[2] is not None]
    return {
        "sampled_rows": len(rows),
        "sample_truncated": truncated,
        "sample_types": dict(sorted(types.items())),
        "sample_nulls": types.get("null", 0),
        "sample_numeric_min": _safe_scalar(min(numeric)) if numeric else None,
        "sample_numeric_max": _safe_scalar(max(numeric)) if numeric else None,
        "sample_length_min": min(lengths) if lengths else None,
        "sample_length_max": max(lengths) if lengths else None,
    }


def _sample_stats_text(stats: Mapping[str, Any]) -> str:
    parts = [f"sampled {stats['sampled_rows']} row(s)"]
    types = stats.get("sample_types") or {}
    if types:
        parts.append(
            "observed types "
            + ", ".join(f"{name}={count}" for name, count in sorted(types.items()))
        )
    if stats.get("sample_numeric_min") is not None:
        parts.append(
            f"numeric range {_display_scalar(stats['sample_numeric_min'])} to "
            f"{_display_scalar(stats['sample_numeric_max'])}"
        )
    if stats.get("sample_length_min") is not None:
        parts.append(
            f"encoded length {stats['sample_length_min']} to "
            f"{stats['sample_length_max']}"
        )
    if stats.get("sample_truncated"):
        parts.append(f"profile capped at {_MAX_SAMPLE_ROWS} rows")
    return ", ".join(parts)


def _sqlite_sample_order(without_rowid: bool, primary: list[str]) -> str:
    if without_rowid and primary:
        return " ORDER BY " + ", ".join(_quote_identifier(name) for name in primary)
    if not without_rowid:
        return " ORDER BY rowid"
    return ""


def _emit_sqlite_keys(
    connection,
    emitter: _Emitter,
    source: str,
    table: str,
    table_unit: Unit | None,
    column_units: Mapping[tuple[str, str], Unit],
    primary: list[str],
) -> int:
    keys: list[tuple[str, str, list[str], bool]] = []
    if primary:
        keys.append(("primary", "PRIMARY KEY", primary, False))
    indexes = connection.execute(
        f"PRAGMA index_list({_sqlite_string(table)})"
    ).fetchall()
    for row in sorted(indexes, key=lambda item: str(item[1])):
        _seq, name, unique, origin, partial = row[:5]
        if not unique or origin == "pk":
            continue
        columns = [
            str(item[2])
            for item in connection.execute(
                f"PRAGMA index_info({_sqlite_string(str(name))})"
            ).fetchall()
            if item[2] is not None
        ]
        keys.append((str(name), "UNIQUE", columns, bool(partial)))

    emitted = 0
    for key_name, key_type, columns, partial in keys:
        ref = f"table:{_component(table)}#key:{_component(key_name)}"
        key_unit = _schema_unit(
            source,
            content=(
                f"SQLite {key_type.lower()} on {table!r}: "
                f"({', '.join(columns)})"
                + ("; partial index" if partial else "")
                + "."
            ),
            ref=ref,
            structure=(table, "keys"),
            salience=0.8,
            meta={
                "table": table,
                "key_name": key_name,
                "key_type": key_type,
                "columns": columns,
                "partial": partial,
            },
        )
        if not emitter.unit(key_unit):
            continue
        emitted += 1
        if table_unit is not None:
            emitter.relation(
                _describe_relation(table_unit, key_unit, f"declared {key_type.lower()}")
            )
        for column in columns:
            column_unit = column_units.get((table.casefold(), column.casefold()))
            if column_unit is not None:
                emitter.relation(
                    _describe_relation(
                        key_unit,
                        column_unit,
                        f"{key_type.lower()} includes column {column!r}",
                    )
                )
    return emitted


def _emit_sqlite_foreign_keys(
    connection,
    emitter: _Emitter,
    source: str,
    table: str,
    table_unit: Unit | None,
    column_units: Mapping[tuple[str, str], Unit],
    table_info: Mapping[str, Mapping[str, Any]],
) -> int:
    rows = connection.execute(
        f"PRAGMA foreign_key_list({_sqlite_string(table)})"
    ).fetchall()
    grouped: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        grouped[int(row[0])].append(row)
    emitted = 0
    for foreign_id in sorted(grouped):
        mappings = sorted(grouped[foreign_id], key=lambda row: int(row[1]))
        target_table = str(mappings[0][2])
        target_info = table_info.get(target_table.casefold())
        pairs: list[tuple[str, str]] = []
        for row in mappings:
            source_column = str(row[3])
            target_column = str(row[4] or "")
            if not target_column and target_info is not None:
                primary = list(target_info.get("primary") or [])
                sequence = int(row[1])
                if sequence < len(primary):
                    target_column = primary[sequence]
            pairs.append((source_column, target_column or "<target primary key>"))
        ref = f"table:{_component(table)}#foreign-key:{foreign_id}"
        fk_unit = _schema_unit(
            source,
            content=(
                f"SQLite foreign key on {table!r}: "
                + ", ".join(f"{src} -> {target_table}.{dst}" for src, dst in pairs)
                + f"; on update {mappings[0][5]}, on delete {mappings[0][6]}."
            ),
            ref=ref,
            structure=(table, "foreign keys"),
            salience=0.85,
            meta={
                "table": table,
                "foreign_key_id": foreign_id,
                "target_table": target_table,
                "columns": [list(pair) for pair in pairs],
                "on_update": str(mappings[0][5]),
                "on_delete": str(mappings[0][6]),
                "match": str(mappings[0][7]),
            },
        )
        if not emitter.unit(fk_unit):
            continue
        emitted += 1
        if table_unit is not None:
            emitter.relation(
                _describe_relation(table_unit, fk_unit, "declared foreign key")
            )
        for source_column, target_column in pairs:
            source_unit = column_units.get((table.casefold(), source_column.casefold()))
            target_unit = column_units.get(
                (target_table.casefold(), target_column.casefold())
            )
            if source_unit is not None and target_unit is not None:
                emitter.relation(
                    Relation(
                        src=source_unit.id,
                        dst=target_unit.id,
                        kind=RelationKind.REFERENCES,
                        evidence=(
                            f"SQLite foreign key {foreign_id}: {table}.{source_column} "
                            f"references {target_table}.{target_column}"
                        ),
                    )
                )
    return emitted


def _require_prefix(path: Path, prefix: bytes, kind: str) -> None:
    try:
        with path.open("rb") as stream:
            actual = stream.read(len(prefix))
    except OSError as exc:
        raise InvalidTier3Data(
            path, kind, f"signature read failed ({exc.__class__.__name__})"
        ) from exc
    if actual != prefix:
        raise InvalidTier3Data(path, kind, "native file signature is missing")


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_SQL_IDENTIFIER_PATTERN = (
    r'(?:"(?:[^"]|"")+"|`[^`]+`|\[[^]]+\]|[A-Za-z_][A-Za-z0-9_$]*)'
)
_SQL_DEPENDENCY = re.compile(
    rf"\b(?:FROM|JOIN)\s+(?P<first>{_SQL_IDENTIFIER_PATTERN})"
    rf"(?:\s*\.\s*(?P<second>{_SQL_IDENTIFIER_PATTERN}))?",
    re.IGNORECASE,
)


def _sql_dependencies(sql: str) -> list[str]:
    found: set[str] = set()
    for match in _SQL_DEPENDENCY.finditer(sql or ""):
        first = _unquote_sql_identifier(match.group("first"))
        second = match.group("second")
        found.add(
            first
            if second is None
            else f"{first}.{_unquote_sql_identifier(second)}"
        )
    return sorted(found, key=lambda value: (value.casefold(), value))


def _unquote_sql_identifier(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('""', '"')
    if len(value) >= 2 and value[0] == value[-1] == "`":
        return value[1:-1].replace("``", "`")
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        return value[1:-1].replace("]]", "]")
    return value


# DuckDB, HDF5, and NetCDF adapters follow below.  They intentionally share
# the same file context, emitter, scalar, and SQL-origin contracts.


def extract_duckdb(path: Path) -> Extraction:
    """Inspect a DuckDB database through a lazy, read-only connection."""

    context, emitter = _begin(Path(path), "duckdb")
    sidecar = context.path.with_name(context.path.name + ".wal")
    try:
        sidecar_size = sidecar.stat().st_size
    except FileNotFoundError:
        sidecar_size = 0
    except OSError as exc:
        raise InvalidTier3Data(
            context.path,
            "duckdb",
            f"sidecar inspection failed ({exc.__class__.__name__})",
        ) from exc
    if sidecar_size:
        raise InvalidTier3Data(
            context.path,
            "duckdb",
            f"non-empty {sidecar.name} sidecar is outside the single-file manifest; "
            "checkpoint the database first",
        )

    try:
        import duckdb
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
        if exc.name != "duckdb":
            raise
        raise ImportError(
            "DuckDB database support requires duckdb; install it with: pip install duckdb"
        ) from exc

    connection = None
    try:
        connection = duckdb.connect(database=str(context.path), read_only=True)
        try:
            connection.execute("SET enable_external_access=false")
        except Exception:
            # Older DuckDB releases do not expose this setting.  We never run
            # view queries or extension-backed table functions regardless.
            pass
        _extract_duckdb_connection(context, emitter, connection)
    except (InvalidTier3Data, UnsupportedTier3Subtype):
        raise
    except Exception as exc:
        raise InvalidTier3Data(context.path, "duckdb", _error_detail(exc)) from exc
    finally:
        if connection is not None:
            connection.close()
    return _finish(context, emitter)


def _extract_duckdb_connection(context: _ReadContext, emitter: _Emitter, connection) -> None:
    source = context.result.source
    version_row = connection.execute("SELECT version()").fetchone()
    version = str(version_row[0]) if version_row else "unknown"
    total_objects = int(
        connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')"
        ).fetchone()[0]
    )
    object_rows = connection.execute(
        "SELECT table_schema, table_name, table_type "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
        "ORDER BY table_schema, table_name "
        f"LIMIT {_MAX_OBJECTS + 1}"
    ).fetchall()
    if len(object_rows) > _MAX_OBJECTS:
        object_rows = object_rows[:_MAX_OBJECTS]
        emitter.gap(
            f"DuckDB catalog has {total_objects} tables/views; only the first "
            f"{_MAX_OBJECTS} in catalog order were inspected",
            ref="database:",
        )

    summary = _schema_unit(
        source,
        content=(
            f"DuckDB database: {sum(str(row[2]).upper() == 'BASE TABLE' for row in object_rows)} "
            f"table(s), {sum(str(row[2]).upper() == 'VIEW' for row in object_rows)} "
            f"view(s), engine {version}."
        ),
        ref="database:",
        salience=0.95,
        meta={
            "database_summary": True,
            "version": version,
            "catalog_objects": total_objects,
        },
    )
    emitter.unit(summary)
    if not object_rows:
        emitter.gap("DuckDB database contains no user tables or views", ref="database:")

    view_queries = _duckdb_view_queries(connection)
    estimates = _duckdb_table_estimates(connection)
    object_comments, column_comments = _duckdb_comments(connection)
    object_units: dict[tuple[str, str], Unit] = {}
    column_units: dict[tuple[str, str, str], Unit] = {}
    object_info: dict[tuple[str, str], dict[str, Any]] = {}
    tables_without_keys: set[tuple[str, str]] = set()
    empty_tables: list[tuple[str, str]] = []
    documentation_seen = False
    units_seen = False
    emitted_attributes = 0

    for raw_schema, raw_name, raw_type in object_rows:
        schema_name = str(raw_schema)
        name = str(raw_name)
        table_type = str(raw_type).upper()
        object_type = "view" if table_type == "VIEW" else "table"
        key = (schema_name.casefold(), name.casefold())
        columns = connection.execute(
            "SELECT column_name, data_type, is_nullable, column_default, ordinal_position "
            "FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position "
            f"LIMIT {_MAX_COLUMNS_PER_TABLE + 1}",
            [schema_name, name],
        ).fetchall()
        if len(columns) > _MAX_COLUMNS_PER_TABLE:
            columns = columns[:_MAX_COLUMNS_PER_TABLE]
            emitter.gap(
                f"DuckDB {object_type} {schema_name}.{name} has more than "
                f"{_MAX_COLUMNS_PER_TABLE} columns; later columns were not emitted",
                ref=_duckdb_object_ref(object_type, schema_name, name),
            )

        query = view_queries.get(key, "")
        object_comment = object_comments.get(key)
        documentation_seen |= bool(object_comment) or bool(
            re.search(r"(?:--|/\*)", query)
        )
        estimated_rows = estimates.get(key)
        ref = _duckdb_object_ref(object_type, schema_name, name)
        content = (
            f"DuckDB {object_type} {schema_name}.{name}: {len(columns)} column(s)"
            + (
                f", approximately {estimated_rows} row(s)"
                if estimated_rows is not None and object_type == "table"
                else ""
            )
            + "."
        )
        unit = _schema_unit(
            source,
            content=content,
            ref=ref,
            structure=(schema_name, name),
            salience=0.9 if object_type == "table" else 0.75,
            meta={
                "object_type": object_type,
                "schema": schema_name,
                "name": name,
                "columns": len(columns),
                "estimated_rows": estimated_rows,
                "comment": object_comment,
                **({"query": _bounded_text(query)} if query else {}),
            },
        )
        if emitter.unit(unit):
            object_units[key] = unit
            emitter.relation(
                _describe_relation(
                    summary,
                    unit,
                    f"{object_type} {schema_name}.{name} in the DuckDB catalog",
                )
            )
            if object_comment and emitted_attributes < _MAX_ATTRIBUTES:
                emitted_attributes += 1
                _emit_sql_comment_attribute(
                    emitter,
                    source,
                    owner=unit,
                    owner_ref=ref,
                    comment=object_comment,
                    structure=(schema_name, name, "attributes"),
                )
        object_info[key] = {
            "schema": schema_name,
            "name": name,
            "type": object_type,
            "unit": object_units.get(key),
            "query": query,
            "columns": columns,
        }

        sample_rows = 0
        sample_truncated = False
        if object_type == "table" and columns:
            sample_rows, sample_truncated = _duckdb_sample_size(
                connection, schema_name, name
            )
            if sample_rows == 0:
                empty_tables.append((schema_name, name))

        for position, column_row in enumerate(columns):
            column_name, data_type, is_nullable, default, ordinal = column_row
            column_name = str(column_name)
            data_type = str(data_type)
            column_comment = column_comments.get(
                (key[0], key[1], column_name.casefold())
            )
            documentation_seen |= bool(column_comment)
            units_seen |= bool(
                re.search(r"(?:^|_)(?:unit|units)$", column_name, re.IGNORECASE)
            )
            stats: dict[str, Any] = {}
            if object_type == "table" and position < _MAX_STATS_COLUMNS_PER_TABLE:
                try:
                    stats = _duckdb_column_stats(
                        connection,
                        schema=schema_name,
                        table=name,
                        column=column_name,
                        data_type=data_type,
                        sampled_rows=sample_rows,
                        sample_truncated=sample_truncated,
                    )
                except Exception as exc:
                    emitter.gap(
                        f"bounded profile failed for DuckDB column "
                        f"{schema_name}.{name}.{column_name}: {_error_detail(exc)}",
                        ref=_duckdb_column_ref(schema_name, name, column_name),
                    )
            parts = [
                f"DuckDB column {schema_name}.{name}.{column_name}: type {data_type}",
                "nullable" if str(is_nullable).upper() == "YES" else "not null",
            ]
            if default is not None:
                parts.append(f"default {_bounded_text(default, 160)!r}")
            if stats:
                parts.append(_sample_stats_text(stats))
            column_unit = _schema_unit(
                source,
                content="; ".join(parts) + ".",
                ref=_duckdb_column_ref(schema_name, name, column_name),
                structure=(schema_name, name, column_name),
                meta={
                    "schema": schema_name,
                    "table": name,
                    "column": column_name,
                    "ordinal": int(ordinal),
                    "declared_type": data_type,
                    "nullable": str(is_nullable).upper() == "YES",
                    "default": _bounded_text(default, 160) if default is not None else None,
                    "comment": column_comment,
                    **stats,
                },
            )
            if emitter.unit(column_unit):
                column_units[(key[0], key[1], column_name.casefold())] = column_unit
                if key in object_units:
                    emitter.relation(
                        _describe_relation(
                            unit,
                            column_unit,
                            f"column {column_name!r} declared by {schema_name}.{name}",
                        )
                    )
                if column_comment and emitted_attributes < _MAX_ATTRIBUTES:
                    emitted_attributes += 1
                    _emit_sql_comment_attribute(
                        emitter,
                        source,
                        owner=column_unit,
                        owner_ref=_duckdb_column_ref(
                            schema_name, name, column_name
                        ),
                        comment=column_comment,
                        structure=(schema_name, name, column_name, "attributes"),
                    )
        if object_type == "table" and len(columns) > _MAX_STATS_COLUMNS_PER_TABLE:
            emitter.gap(
                f"bounded profiles cover the first {_MAX_STATS_COLUMNS_PER_TABLE} "
                f"of {len(columns)} columns in {schema_name}.{name}",
                ref=ref,
            )
        if object_type == "table":
            tables_without_keys.add(key)

    foreign_keys = _emit_duckdb_constraints(
        connection,
        emitter,
        source,
        object_info,
        object_units,
        column_units,
        tables_without_keys,
    )
    for key in sorted(object_info):
        info = object_info[key]
        if info["type"] != "view" or key not in object_units:
            continue
        for dependency in _sql_dependencies(info["query"]):
            if "." in dependency:
                dependency_schema, dependency_table = dependency.rsplit(".", 1)
                exact = (
                    dependency_schema.casefold(),
                    dependency_table.casefold(),
                )
                candidates = [exact] if exact in object_units else []
            else:
                local = (key[0], dependency.casefold())
                if local in object_units:
                    candidates = [local]
                else:
                    candidates = [
                        target_key
                        for target_key in object_units
                        if target_key[1] == dependency.casefold()
                    ]
            if len(candidates) != 1:
                continue
            target = object_units[candidates[0]]
            emitter.relation(
                Relation(
                    src=object_units[key].id,
                    dst=target.id,
                    kind=RelationKind.DERIVES_FROM,
                    evidence=f"view query names {dependency!r} after FROM or JOIN",
                    confidence=0.95,
                )
            )

    if not documentation_seen and object_rows:
        emitter.gap(
            "DuckDB catalog contains no stored table or column documentation comments",
            ref="database:",
        )
    if not units_seen and any(info["columns"] for info in object_info.values()):
        emitter.gap(
            "DuckDB schema declares no explicit column-unit metadata",
            ref="database:",
        )
    if tables_without_keys:
        labels = [
            f"{object_info[key]['schema']}.{object_info[key]['name']}"
            for key in sorted(tables_without_keys)
        ]
        shown = ", ".join(repr(label) for label in labels[:8])
        more = len(labels) - min(8, len(labels))
        emitter.gap(
            f"no primary or unique key is declared for {shown}"
            + (f" and {more} more table(s)" if more else ""),
            ref="database:",
        )
    if not foreign_keys and any(
        info["type"] == "table" for info in object_info.values()
    ):
        emitter.gap(
            "DuckDB schema declares no foreign-key relationships",
            ref="database:",
        )
    for schema_name, table_name in empty_tables:
        emitter.gap(
            f"DuckDB table {schema_name}.{table_name} contains no rows",
            ref=_duckdb_object_ref("table", schema_name, table_name),
        )

    context.result.meta.update(
        {
            "version": version,
            "catalog_objects": total_objects,
            "tables": sum(info["type"] == "table" for info in object_info.values()),
            "views": sum(info["type"] == "view" for info in object_info.values()),
            "foreign_keys": foreign_keys,
        }
    )


def _duckdb_view_queries(connection) -> dict[tuple[str, str], str]:
    try:
        cursor = connection.execute(
            "SELECT schema_name, view_name, sql FROM duckdb_views() "
            "WHERE NOT internal ORDER BY schema_name, view_name "
            f"LIMIT {_MAX_OBJECTS + 1}"
        )
        rows = cursor.fetchall()
    except Exception:
        return {}
    return {
        (str(schema).casefold(), str(name).casefold()): str(sql or "")
        for schema, name, sql in rows[:_MAX_OBJECTS]
    }


def _duckdb_table_estimates(connection) -> dict[tuple[str, str], int]:
    try:
        rows = connection.execute(
            "SELECT schema_name, table_name, estimated_size FROM duckdb_tables() "
            "WHERE NOT internal ORDER BY schema_name, table_name "
            f"LIMIT {_MAX_OBJECTS + 1}"
        ).fetchall()
    except Exception:
        return {}
    estimates: dict[tuple[str, str], int] = {}
    for schema, name, estimate in rows[:_MAX_OBJECTS]:
        if estimate is not None:
            estimates[(str(schema).casefold(), str(name).casefold())] = int(estimate)
    return estimates


def _duckdb_comments(
    connection,
) -> tuple[
    dict[tuple[str, str], str],
    dict[tuple[str, str, str], str],
]:
    object_comments: dict[tuple[str, str], str] = {}
    column_comments: dict[tuple[str, str, str], str] = {}
    try:
        rows = connection.execute(
            "SELECT schema_name, table_name, comment FROM duckdb_tables() "
            "WHERE NOT internal AND comment IS NOT NULL "
            "ORDER BY schema_name, table_name "
            f"LIMIT {_MAX_ATTRIBUTES}"
        ).fetchall()
        object_comments.update(
            {
                (str(schema).casefold(), str(table).casefold()): _bounded_text(comment)
                for schema, table, comment in rows
                if str(comment).strip()
            }
        )
    except Exception:
        pass
    try:
        rows = connection.execute(
            "SELECT schema_name, view_name, comment FROM duckdb_views() "
            "WHERE NOT internal AND comment IS NOT NULL "
            "ORDER BY schema_name, view_name "
            f"LIMIT {_MAX_ATTRIBUTES}"
        ).fetchall()
        object_comments.update(
            {
                (str(schema).casefold(), str(view).casefold()): _bounded_text(comment)
                for schema, view, comment in rows
                if str(comment).strip()
            }
        )
    except Exception:
        pass
    try:
        rows = connection.execute(
            "SELECT schema_name, table_name, column_name, comment "
            "FROM duckdb_columns() "
            "WHERE NOT internal AND comment IS NOT NULL "
            "ORDER BY schema_name, table_name, column_name "
            f"LIMIT {_MAX_ATTRIBUTES}"
        ).fetchall()
        column_comments.update(
            {
                (
                    str(schema).casefold(),
                    str(table).casefold(),
                    str(column).casefold(),
                ): _bounded_text(comment)
                for schema, table, column, comment in rows
                if str(comment).strip()
            }
        )
    except Exception:
        pass
    return object_comments, column_comments


def _emit_sql_comment_attribute(
    emitter: _Emitter,
    source: str,
    *,
    owner: Unit,
    owner_ref: str,
    comment: str,
    structure: tuple[str, ...],
) -> None:
    unit = _schema_unit(
        source,
        content=f"Documentation on {owner_ref}: {_bounded_text(comment)!r}.",
        ref=f"{owner_ref}#attribute:comment",
        structure=structure,
        salience=0.65,
        meta={
            "object_type": "attribute",
            "owner": owner_ref,
            "name": "comment",
            "value": _bounded_text(comment),
        },
    )
    if emitter.unit(unit):
        emitter.relation(
            _describe_relation(owner, unit, f"stored comment on {owner_ref}")
        )


def _duckdb_sample_size(connection, schema: str, table: str) -> tuple[int, bool]:
    qualified = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    count = int(
        connection.execute(
            f"SELECT count(*) FROM (SELECT 1 FROM {qualified} "
            f"LIMIT {_MAX_SAMPLE_ROWS + 1}) AS autotldr_sample"
        ).fetchone()[0]
    )
    return min(count, _MAX_SAMPLE_ROWS), count > _MAX_SAMPLE_ROWS


def _duckdb_column_stats(
    connection,
    *,
    schema: str,
    table: str,
    column: str,
    data_type: str,
    sampled_rows: int,
    sample_truncated: bool,
) -> dict[str, Any]:
    qualified = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    quoted = _quote_identifier(column)
    sample = (
        f"(SELECT {quoted} AS value FROM {qualified} "
        f"LIMIT {_MAX_SAMPLE_ROWS}) AS autotldr_sample"
    )
    upper_type = data_type.upper()
    if _duckdb_numeric_type(upper_type) or _duckdb_temporal_type(upper_type):
        row = connection.execute(
            "SELECT count(value), count(DISTINCT value), min(value), max(value) "
            f"FROM {sample}"
        ).fetchone()
        non_null, distinct, minimum, maximum = row
        return {
            "sampled_rows": sampled_rows,
            "sample_truncated": sample_truncated,
            "sample_types": {data_type: sampled_rows},
            "sample_nulls": sampled_rows - int(non_null),
            "sample_distinct": int(distinct),
            "sample_numeric_min": _safe_scalar(minimum),
            "sample_numeric_max": _safe_scalar(maximum),
            "sample_length_min": None,
            "sample_length_max": None,
        }
    if _duckdb_text_type(upper_type):
        row = connection.execute(
            "SELECT count(value), count(DISTINCT value), "
            "min(length(value)), max(length(value)) "
            f"FROM {sample}"
        ).fetchone()
        non_null, distinct, minimum, maximum = row
        return {
            "sampled_rows": sampled_rows,
            "sample_truncated": sample_truncated,
            "sample_types": {data_type: sampled_rows},
            "sample_nulls": sampled_rows - int(non_null),
            "sample_distinct": int(distinct),
            "sample_numeric_min": None,
            "sample_numeric_max": None,
            "sample_length_min": int(minimum) if minimum is not None else None,
            "sample_length_max": int(maximum) if maximum is not None else None,
        }
    non_null = int(
        connection.execute(f"SELECT count(value) FROM {sample}").fetchone()[0]
    )
    return {
        "sampled_rows": sampled_rows,
        "sample_truncated": sample_truncated,
        "sample_types": {data_type: sampled_rows},
        "sample_nulls": sampled_rows - non_null,
        "sample_numeric_min": None,
        "sample_numeric_max": None,
        "sample_length_min": None,
        "sample_length_max": None,
    }


def _duckdb_numeric_type(data_type: str) -> bool:
    return bool(
        re.match(
            r"^(?:U?HUGEINT|U?BIGINT|U?INTEGER|U?SMALLINT|U?TINYINT|"
            r"DECIMAL|NUMERIC|REAL|FLOAT|DOUBLE)",
            data_type,
        )
    )


def _duckdb_temporal_type(data_type: str) -> bool:
    return data_type.startswith(("DATE", "TIME", "TIMESTAMP", "INTERVAL"))


def _duckdb_text_type(data_type: str) -> bool:
    return data_type.startswith(("VARCHAR", "CHAR", "TEXT"))


def _emit_duckdb_constraints(
    connection,
    emitter: _Emitter,
    source: str,
    object_info: Mapping[tuple[str, str], Mapping[str, Any]],
    object_units: Mapping[tuple[str, str], Unit],
    column_units: Mapping[tuple[str, str, str], Unit],
    tables_without_keys: set[tuple[str, str]],
) -> int:
    try:
        cursor = connection.execute(
            f"SELECT * FROM duckdb_constraints() LIMIT {_MAX_OBJECTS + 1}"
        )
        names = [str(item[0]) for item in cursor.description]
        rows = cursor.fetchall()
    except Exception:
        emitter.gap(
            "DuckDB constraint catalog is unavailable in this engine version",
            ref="database:",
        )
        return 0
    if len(rows) > _MAX_OBJECTS:
        rows = rows[:_MAX_OBJECTS]
        emitter.gap(
            f"DuckDB has more than {_MAX_OBJECTS} constraints; later constraints "
            "were not emitted",
            ref="database:",
        )
    records = [dict(zip(names, row, strict=True)) for row in rows]
    records.sort(
        key=lambda record: (
            str(record.get("schema_name", "")).casefold(),
            str(record.get("table_name", "")).casefold(),
            int(record.get("constraint_index", 0) or 0),
        )
    )
    foreign_keys = 0
    for record in records:
        schema = str(record.get("schema_name", ""))
        table = str(record.get("table_name", ""))
        key = (schema.casefold(), table.casefold())
        if key not in object_info:
            continue
        constraint_type = str(record.get("constraint_type", "")).upper()
        if constraint_type not in {"PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK"}:
            continue
        index = int(record.get("constraint_index", 0) or 0)
        text = _bounded_text(record.get("constraint_text", ""), _MAX_TEXT_CHARS)
        raw_columns = record.get("constraint_column_names") or []
        columns = [str(item) for item in raw_columns]
        ref = (
            f"table:{_component(schema)}.{_component(table)}#constraint:{index}"
        )
        constraint_unit = _schema_unit(
            source,
            content=(
                f"DuckDB {constraint_type.lower()} on {schema}.{table}"
                + (f" ({', '.join(columns)})" if columns else "")
                + (f": {text}." if text else ".")
            ),
            ref=ref,
            structure=(schema, table, "constraints"),
            salience=0.82,
            meta={
                "schema": schema,
                "table": table,
                "constraint_index": index,
                "constraint_type": constraint_type,
                "columns": columns,
                "expression": text or None,
            },
        )
        if not emitter.unit(constraint_unit):
            continue
        table_unit = object_units.get(key)
        if table_unit is not None:
            emitter.relation(
                _describe_relation(
                    table_unit,
                    constraint_unit,
                    f"declared {constraint_type.lower()}",
                )
            )
        for column in columns:
            column_unit = column_units.get((key[0], key[1], column.casefold()))
            if column_unit is not None:
                emitter.relation(
                    _describe_relation(
                        constraint_unit,
                        column_unit,
                        f"constraint includes column {column!r}",
                    )
                )
        if constraint_type in {"PRIMARY KEY", "UNIQUE"}:
            tables_without_keys.discard(key)
        if constraint_type == "FOREIGN KEY":
            foreign_keys += 1
            target = _parse_duckdb_reference(text, default_schema=schema)
            if target is None:
                emitter.gap(
                    f"could not resolve the target columns for DuckDB foreign key "
                    f"{schema}.{table} constraint {index}",
                    ref=ref,
                )
                continue
            target_schema, target_table, target_columns = target
            for source_column, target_column in zip(
                columns, target_columns, strict=False
            ):
                source_unit = column_units.get(
                    (key[0], key[1], source_column.casefold())
                )
                target_unit = column_units.get(
                    (
                        target_schema.casefold(),
                        target_table.casefold(),
                        target_column.casefold(),
                    )
                )
                if source_unit is not None and target_unit is not None:
                    emitter.relation(
                        Relation(
                            src=source_unit.id,
                            dst=target_unit.id,
                            kind=RelationKind.REFERENCES,
                            evidence=(
                                f"DuckDB foreign key {index}: "
                                f"{schema}.{table}.{source_column} references "
                                f"{target_schema}.{target_table}.{target_column}"
                            ),
                        )
                    )
    return foreign_keys


_DUCKDB_REFERENCE = re.compile(
    r"\bREFERENCES\s+"
    r"(?:(?P<schema>[A-Za-z_][A-Za-z0-9_$]*|\"(?:[^\"]|\"\")+\")\s*\.\s*)?"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_$]*|\"(?:[^\"]|\"\")+\")\s*"
    r"\((?P<columns>[^)]*)\)",
    re.IGNORECASE,
)


def _parse_duckdb_reference(
    text: str, *, default_schema: str
) -> tuple[str, str, list[str]] | None:
    match = _DUCKDB_REFERENCE.search(text)
    if match is None:
        return None
    schema = _unquote_identifier(match.group("schema") or default_schema)
    table = _unquote_identifier(match.group("table"))
    columns = [
        _unquote_identifier(item.strip())
        for item in match.group("columns").split(",")
        if item.strip()
    ]
    return schema, table, columns


def _unquote_identifier(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _duckdb_object_ref(kind: str, schema: str, table: str) -> str:
    return f"{kind}:{_component(schema)}.{_component(table)}"


def _duckdb_column_ref(schema: str, table: str, column: str) -> str:
    return (
        f"table:{_component(schema)}.{_component(table)}"
        f"#column:{_component(column)}"
    )


def extract_hdf5(path: Path) -> Extraction:
    """Walk HDF5 groups, datasets, links, and bounded attributes without values."""

    context, emitter = _begin(Path(path), "hdf5")
    try:
        import h5py
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
        if exc.name != "h5py":
            raise
        raise ImportError(
            "HDF5 support requires h5py; install it with: pip install h5py"
        ) from exc

    try:
        with h5py.File(context.path, mode="r") as handle:
            _extract_hdf5_file(context, emitter, h5py, handle)
    except (InvalidTier3Data, UnsupportedTier3Subtype):
        raise
    except Exception as exc:
        raise InvalidTier3Data(context.path, "hdf5", _error_detail(exc)) from exc
    return _finish(context, emitter)


@dataclass(slots=True)
class _HdfState:
    objects: int = 0
    groups: int = 0
    datasets: int = 0
    links: int = 0
    datatypes: int = 0
    attributes: int = 0
    max_depth: int = 0
    object_limit_reported: bool = False
    attribute_limit_reported: bool = False
    documentation_seen: bool = False
    units_seen: bool = False


def _extract_hdf5_file(
    context: _ReadContext, emitter: _Emitter, h5py, handle
) -> None:
    source = context.result.source
    root = _schema_unit(
        source,
        content="HDF5 root group /.",
        ref="/",
        salience=0.95,
        meta={
            "object_type": "group",
            "path": "/",
            "file_driver": str(handle.driver),
            "userblock_bytes": int(handle.userblock_size),
        },
    )
    emitter.unit(root)
    state = _HdfState(objects=1, groups=1)
    identities: dict[object, Unit] = {_hdf5_identity(h5py, handle): root}
    pending_links: list[tuple[Unit, str]] = []
    object_units: dict[str, Unit] = {"/": root}
    missing_units: list[str] = []
    missing_docs: list[str] = []
    missing_stats: list[str] = []

    root_attribute_names = _emit_hdf5_attributes(
        emitter, source, handle, "/", root, state
    )
    state.documentation_seen |= _has_documentation(root_attribute_names)
    state.units_seen |= _has_units(root_attribute_names)

    def visit_group(group, group_path: str, group_unit: Unit, depth: int) -> bool:
        try:
            names, names_truncated = _bounded_catalog_names(
                group.keys(), max(0, _MAX_OBJECTS - state.objects)
            )
        except Exception as exc:
            raise InvalidTier3Data(
                context.path,
                "hdf5",
                f"cannot enumerate group {group_path}: {_error_detail(exc)}",
            ) from exc
        for name in names:
            if state.objects >= _MAX_OBJECTS:
                if not state.object_limit_reported:
                    emitter.gap(
                        f"HDF5 object traversal stopped at {_MAX_OBJECTS} objects",
                        ref=group_path,
                    )
                    state.object_limit_reported = True
                return False
            child_path = _hdf5_child_path(group_path, name)
            try:
                link = group.get(name, getlink=True)
            except Exception as exc:
                emitter.gap(
                    f"HDF5 link {child_path} could not be inspected: {_error_detail(exc)}",
                    ref=child_path,
                )
                continue
            state.objects += 1
            state.max_depth = max(state.max_depth, depth + 1)

            if isinstance(link, h5py.SoftLink):
                state.links += 1
                raw_target = str(link.path)
                target = (
                    posixpath.normpath(raw_target)
                    if raw_target.startswith("/")
                    else posixpath.normpath(
                        posixpath.join(posixpath.dirname(child_path), raw_target)
                    )
                )
                if not target.startswith("/"):
                    target = "/" + target
                link_unit = _schema_unit(
                    source,
                    content=f"HDF5 soft link {child_path} -> {target}.",
                    ref=child_path,
                    structure=_hdf5_structure(child_path),
                    salience=0.55,
                    modality=Modality.REFERENCE,
                    meta={
                        "link_type": "soft",
                        "target": target,
                        "ref_kind": "hdf5-path",
                    },
                )
                if emitter.unit(link_unit):
                    object_units[child_path] = link_unit
                    emitter.relation(
                        _describe_relation(group_unit, link_unit, "group contains soft link")
                    )
                    pending_links.append((link_unit, target))
                continue
            if isinstance(link, h5py.ExternalLink):
                state.links += 1
                filename = _bounded_text(link.filename, 512)
                target = str(link.path)
                link_unit = _schema_unit(
                    source,
                    content=(
                        f"HDF5 external link {child_path} -> {filename}:{target}; "
                        "external payload was not followed."
                    ),
                    ref=child_path,
                    structure=_hdf5_structure(child_path),
                    salience=0.6,
                    modality=Modality.REFERENCE,
                    meta={
                        "link_type": "external",
                        "external_file": filename,
                        "target": target,
                        "ref_kind": "external-hdf5-path",
                    },
                )
                if emitter.unit(link_unit):
                    object_units[child_path] = link_unit
                    emitter.relation(
                        _describe_relation(
                            group_unit, link_unit, "group contains external link"
                        )
                    )
                emitter.gap(
                    f"external HDF5 link {child_path} was recorded but not followed",
                    ref=child_path,
                )
                continue

            try:
                child = group[name]
            except Exception as exc:
                emitter.gap(
                    f"HDF5 object {child_path} could not be opened: {_error_detail(exc)}",
                    ref=child_path,
                )
                continue
            identity = _hdf5_identity(h5py, child)
            if identity in identities:
                state.links += 1
                canonical = identities[identity]
                alias = _schema_unit(
                    source,
                    content=(
                        f"HDF5 hard-link alias {child_path} refers to "
                        f"{canonical.origin.ref}."
                    ),
                    ref=child_path,
                    structure=_hdf5_structure(child_path),
                    salience=0.5,
                    modality=Modality.REFERENCE,
                    meta={
                        "link_type": "hard-alias",
                        "target": canonical.origin.ref,
                        "ref_kind": "hdf5-path",
                    },
                )
                if emitter.unit(alias):
                    object_units[child_path] = alias
                    emitter.relation(
                        _describe_relation(group_unit, alias, "group contains hard-link alias")
                    )
                    emitter.relation(
                        Relation(
                            src=alias.id,
                            dst=canonical.id,
                            kind=RelationKind.REFERENCES,
                            evidence="HDF5 hard link resolves to the same object address",
                        )
                    )
                continue

            if isinstance(child, h5py.Group):
                state.groups += 1
                unit = _schema_unit(
                    source,
                    content=f"HDF5 group {child_path}: {len(child)} direct member(s).",
                    ref=child_path,
                    structure=_hdf5_structure(child_path),
                    salience=0.75,
                    meta={
                        "object_type": "group",
                        "path": child_path,
                        "members": len(child),
                    },
                )
            elif isinstance(child, h5py.Dataset):
                state.datasets += 1
                unit = _hdf5_dataset_unit(source, child_path, child)
            elif isinstance(child, h5py.Datatype):
                state.datatypes += 1
                unit = _schema_unit(
                    source,
                    content=f"HDF5 named datatype {child_path}: {child.dtype}.",
                    ref=child_path,
                    structure=_hdf5_structure(child_path),
                    meta={
                        "object_type": "datatype",
                        "path": child_path,
                        "dtype": str(child.dtype),
                    },
                )
            else:
                emitter.gap(
                    f"unsupported HDF5 object subtype at {child_path}: "
                    f"{type(child).__name__}",
                    ref=child_path,
                )
                continue

            identities[identity] = unit
            if emitter.unit(unit):
                object_units[child_path] = unit
                emitter.relation(
                    _describe_relation(
                        group_unit, unit, f"group {group_path} contains {child_path}"
                    )
                )
            attribute_names = _emit_hdf5_attributes(
                emitter, source, child, child_path, unit, state
            )
            state.documentation_seen |= _has_documentation(attribute_names)
            state.units_seen |= _has_units(attribute_names)
            if isinstance(child, h5py.Dataset):
                if _hdf5_numeric(child) and not _has_units(attribute_names):
                    missing_units.append(child_path)
                if not _has_documentation(attribute_names):
                    missing_docs.append(child_path)
                if _hdf5_numeric(child) and not _has_declared_stats(attribute_names):
                    missing_stats.append(child_path)
            if isinstance(child, h5py.Group):
                if depth + 1 >= _MAX_DEPTH:
                    if len(child):
                        emitter.gap(
                            f"HDF5 depth limit {_MAX_DEPTH} stopped traversal below "
                            f"{child_path}",
                            ref=child_path,
                        )
                    continue
                if not visit_group(child, child_path, unit, depth + 1):
                    return False
        if names_truncated:
            if not state.object_limit_reported:
                emitter.gap(
                    f"HDF5 object traversal stopped at {_MAX_OBJECTS} objects",
                    ref=group_path,
                )
                state.object_limit_reported = True
            return False
        return True

    visit_group(handle, "/", root, 0)
    for link_unit, target in pending_links:
        target_unit = object_units.get(target)
        if target_unit is None:
            emitter.gap(
                f"HDF5 soft link {link_unit.origin.ref} targets unresolved path {target}",
                ref=link_unit.origin.ref,
            )
            continue
        emitter.relation(
            Relation(
                src=link_unit.id,
                dst=target_unit.id,
                kind=RelationKind.REFERENCES,
                evidence="HDF5 soft link target resolved inside the file",
            )
        )

    if state.objects == 1:
        emitter.gap("HDF5 file contains an empty root group", ref="/")
    _emit_missing_metadata_gap(
        emitter,
        "HDF5 numeric datasets declare no units for",
        missing_units,
        ref="/",
    )
    _emit_missing_metadata_gap(
        emitter,
        "HDF5 datasets contain no title/description/long_name for",
        missing_docs,
        ref="/",
    )
    _emit_missing_metadata_gap(
        emitter,
        "HDF5 numeric datasets declare no valid/actual range for",
        missing_stats,
        ref="/",
    )
    context.result.meta.update(
        {
            "objects": state.objects,
            "groups": state.groups,
            "datasets": state.datasets,
            "links": state.links,
            "named_datatypes": state.datatypes,
            "attributes": state.attributes,
            "max_depth": state.max_depth,
            "file_driver": str(handle.driver),
            "userblock_bytes": int(handle.userblock_size),
        }
    )


def _hdf5_dataset_unit(source: str, path: str, dataset) -> Unit:
    shape = tuple(int(part) for part in dataset.shape)
    maxshape = (
        [None if part is None else int(part) for part in dataset.maxshape]
        if dataset.maxshape is not None
        else None
    )
    chunks = (
        [int(part) for part in dataset.chunks] if dataset.chunks is not None else None
    )
    logical_bytes = int(getattr(dataset, "nbytes", 0))
    try:
        storage_bytes = int(dataset.id.get_storage_size())
    except Exception:
        storage_bytes = None
    elements = int(dataset.size)
    shape_text = "scalar" if not shape else " × ".join(str(part) for part in shape)
    storage = (
        f", {storage_bytes} stored byte(s)" if storage_bytes is not None else ""
    )
    return _schema_unit(
        source,
        content=(
            f"HDF5 dataset {path}: dtype {dataset.dtype}, shape {shape_text}, "
            f"{elements} element(s), {logical_bytes} logical byte(s){storage}; "
            f"payload values were not read."
        ),
        ref=path,
        structure=_hdf5_structure(path),
        salience=0.85,
        meta={
            "object_type": "dataset",
            "path": path,
            "dtype": str(dataset.dtype),
            "shape": list(shape),
            "maxshape": maxshape,
            "elements": elements,
            "logical_bytes": logical_bytes,
            "storage_bytes": storage_bytes,
            "chunks": chunks,
            "compression": str(dataset.compression) if dataset.compression else None,
            "compression_options": _safe_scalar(dataset.compression_opts),
            "shuffle": bool(dataset.shuffle),
            "fletcher32": bool(dataset.fletcher32),
            "scaleoffset": _safe_scalar(dataset.scaleoffset),
            "payload_read": False,
        },
    )


def _emit_hdf5_attributes(
    emitter: _Emitter,
    source: str,
    owner,
    owner_path: str,
    owner_unit: Unit,
    state: _HdfState,
) -> list[str]:
    try:
        names, names_truncated = _bounded_catalog_names(
            owner.attrs.keys(), max(0, _MAX_ATTRIBUTES - state.attributes)
        )
    except Exception as exc:
        emitter.gap(
            f"HDF5 attributes on {owner_path} could not be enumerated: "
            f"{_error_detail(exc)}",
            ref=owner_path,
        )
        return []
    if names_truncated and not state.attribute_limit_reported:
        emitter.gap(
            f"HDF5 attribute traversal stopped at {_MAX_ATTRIBUTES} attributes",
            ref=owner_path,
        )
        state.attribute_limit_reported = True
    emitted_names: list[str] = []
    for name in names:
        if state.attributes >= _MAX_ATTRIBUTES:
            if not state.attribute_limit_reported:
                emitter.gap(
                    f"HDF5 attribute traversal stopped at {_MAX_ATTRIBUTES} attributes",
                    ref=owner_path,
                )
                state.attribute_limit_reported = True
            break
        state.attributes += 1
        emitted_names.append(name)
        attr_id = None
        try:
            attr_id = owner.attrs.get_id(name)
            shape = tuple(int(part) for part in getattr(attr_id, "shape", ()) or ())
            raw_dtype = getattr(attr_id, "dtype", None)
            dtype = str(raw_dtype if raw_dtype is not None else "unknown")
            variable_length = str(getattr(raw_dtype, "kind", "")) == "O"
            storage_size = int(attr_id.get_storage_size())
        except Exception as exc:
            emitter.gap(
                f"HDF5 attribute {name!r} on {owner_path} could not be inspected: "
                f"{_error_detail(exc)}",
                ref=f"{owner_path}#attribute:{_component(name)}",
            )
            continue
        finally:
            if attr_id is not None:
                try:
                    attr_id.close()
                except Exception:
                    pass

        value: object | None = None
        # HDF5 variable-length strings/arrays store heap descriptors in the
        # attribute itself, so get_storage_size() does not bound the payload
        # h5py would allocate.  Refuse those values before indexing attrs.
        omitted = variable_length or storage_size > _MAX_ATTRIBUTE_BYTES
        if not omitted:
            try:
                value = owner.attrs[name]
            except Exception as exc:
                emitter.gap(
                    f"HDF5 attribute {name!r} on {owner_path} could not be read: "
                    f"{_error_detail(exc)}",
                    ref=f"{owner_path}#attribute:{_component(name)}",
                )
                omitted = True
        safe = None if omitted else _safe_attribute_value(name, value)
        if variable_length:
            description = "variable-length value omitted before materialization"
        elif omitted:
            description = f"{storage_size} bytes; value omitted by attribute bound"
        elif isinstance(safe, dict):
            description = json.dumps(safe, sort_keys=True)
        else:
            description = f"value {_display_scalar(safe)}"
        ref = f"{owner_path}#attribute:{_component(name)}"
        unit = _schema_unit(
            source,
            content=(
                f"HDF5 attribute {name!r} on {owner_path}: dtype {dtype}, "
                f"shape {list(shape)}; {description}."
            ),
            ref=ref,
            structure=_hdf5_structure(owner_path) + ("attributes",),
            salience=0.65 if name.casefold() in _SEMANTIC_ATTRIBUTES else 0.45,
            meta={
                "object_type": "attribute",
                "owner": owner_path,
                "name": name,
                "dtype": dtype,
                "shape": list(shape),
                "storage_bytes": storage_size,
                "value_omitted": omitted,
                **(
                    {"omission_reason": "variable-length"}
                    if variable_length
                    else {}
                ),
                **({"value": safe} if not omitted else {}),
            },
        )
        if emitter.unit(unit):
            emitter.relation(
                _describe_relation(owner_unit, unit, f"attribute {name!r} on {owner_path}")
            )
    return emitted_names


def _hdf5_identity(h5py, obj) -> object:
    try:
        return ("address", int(h5py.h5o.get_info(obj.id).addr))
    except Exception:
        return ("name", str(obj.name))


def _hdf5_child_path(parent: str, name: str) -> str:
    return "/" + name if parent == "/" else parent.rstrip("/") + "/" + name


def _hdf5_structure(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


def _hdf5_numeric(dataset) -> bool:
    return str(getattr(dataset.dtype, "kind", "")) in {"b", "i", "u", "f", "c"}


def extract_netcdf(path: Path) -> Extraction:
    """Walk NetCDF groups, dimensions, variables, and metadata without values."""

    context, emitter = _begin(Path(path), "netcdf")
    try:
        import netCDF4
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
        if exc.name != "netCDF4":
            raise
        raise ImportError(
            "NetCDF support requires netCDF4; install it with: pip install netCDF4"
        ) from exc

    dataset = None
    try:
        dataset = netCDF4.Dataset(str(context.path), mode="r")
        model = str(dataset.data_model)
        allowed = {
            "NETCDF3_CLASSIC",
            "NETCDF3_64BIT_OFFSET",
            "NETCDF3_64BIT_DATA",
            "NETCDF4",
            "NETCDF4_CLASSIC",
        }
        if model not in allowed:
            raise UnsupportedTier3Subtype(context.path, "netcdf", model)
        _extract_netcdf_dataset(context, emitter, dataset)
    except (InvalidTier3Data, UnsupportedTier3Subtype):
        raise
    except Exception as exc:
        raise InvalidTier3Data(context.path, "netcdf", _error_detail(exc)) from exc
    finally:
        if dataset is not None:
            dataset.close()
    return _finish(context, emitter)


@dataclass(slots=True)
class _NetcdfState:
    objects: int = 0
    groups: int = 0
    dimensions: int = 0
    variables: int = 0
    attributes: int = 0
    max_depth: int = 0
    object_limit_reported: bool = False
    attribute_limit_reported: bool = False


def _extract_netcdf_dataset(
    context: _ReadContext, emitter: _Emitter, dataset
) -> None:
    source = context.result.source
    model = str(dataset.data_model)
    disk_format = str(getattr(dataset, "disk_format", model))
    root = _schema_unit(
        source,
        content=f"NetCDF root group /: data model {model}, disk format {disk_format}.",
        ref="/",
        salience=0.95,
        meta={
            "object_type": "group",
            "path": "/",
            "data_model": model,
            "disk_format": disk_format,
        },
    )
    emitter.unit(root)
    state = _NetcdfState(objects=1, groups=1)
    dimension_units: dict[tuple[str, str], Unit] = {}
    object_units: dict[str, Unit] = {"/": root}
    missing_units: list[str] = []
    missing_docs: list[str] = []
    missing_stats: list[str] = []

    def visit_group(group, path: str, group_unit: Unit, depth: int) -> bool:
        attribute_names = _emit_netcdf_attributes(
            emitter, source, group, path, group_unit, state
        )
        del attribute_names

        dimension_names, dimensions_truncated = _bounded_catalog_names(
            group.dimensions, max(0, _MAX_OBJECTS - state.objects)
        )
        for name in dimension_names:
            if not _netcdf_object_allowed(state, emitter, path):
                return False
            dimension = group.dimensions[name]
            state.objects += 1
            state.dimensions += 1
            ref = f"{path}#dimension:{_component(name)}"
            unit = _schema_unit(
                source,
                content=(
                    f"NetCDF dimension {name!r} in {path}: length {len(dimension)}"
                    + (", unlimited" if dimension.isunlimited() else "")
                    + "."
                ),
                ref=ref,
                structure=_netcdf_structure(path) + (name,),
                salience=0.78,
                meta={
                    "object_type": "dimension",
                    "group": path,
                    "name": name,
                    "length": int(len(dimension)),
                    "unlimited": bool(dimension.isunlimited()),
                },
            )
            if emitter.unit(unit):
                dimension_units[(path, name.casefold())] = unit
                emitter.relation(
                    _describe_relation(
                        group_unit, unit, f"dimension {name!r} declared in group {path}"
                    )
                )

        if dimensions_truncated:
            _netcdf_object_allowed(state, emitter, path)
            return False

        variable_names, variables_truncated = _bounded_catalog_names(
            group.variables, max(0, _MAX_OBJECTS - state.objects)
        )
        for name in variable_names:
            if not _netcdf_object_allowed(state, emitter, path):
                return False
            variable = group.variables[name]
            state.objects += 1
            state.variables += 1
            variable_path = _netcdf_child_path(path, name)
            dtype = str(variable.dtype)
            shape = [int(part) for part in variable.shape]
            dimensions = [str(item) for item in variable.dimensions]
            try:
                chunking = variable.chunking()
            except Exception:
                chunking = None
            try:
                filters = variable.filters()
            except Exception:
                filters = None
            content = (
                f"NetCDF variable {variable_path}: dtype {dtype}, shape "
                f"{shape or 'scalar'}, dimensions {dimensions or 'none'}; "
                "payload values were not read."
            )
            variable_unit = _schema_unit(
                source,
                content=content,
                ref=variable_path,
                structure=_netcdf_structure(variable_path),
                salience=0.86,
                meta={
                    "object_type": "variable",
                    "path": variable_path,
                    "dtype": dtype,
                    "shape": shape,
                    "dimensions": dimensions,
                    "chunking": _safe_scalar(chunking),
                    "filters": _safe_mapping(filters),
                    "endian": str(variable.endian()),
                    "payload_read": False,
                },
            )
            if emitter.unit(variable_unit):
                object_units[variable_path] = variable_unit
                emitter.relation(
                    _describe_relation(
                        group_unit,
                        variable_unit,
                        f"variable {name!r} declared in group {path}",
                    )
                )
            for dimension_name in dimensions:
                dimension_unit = _resolve_netcdf_dimension(
                    dimension_units, path, dimension_name
                )
                if dimension_unit is not None and variable_path in object_units:
                    emitter.relation(
                        Relation(
                            src=variable_unit.id,
                            dst=dimension_unit.id,
                            kind=RelationKind.REFERENCES,
                            evidence=(
                                f"NetCDF variable {variable_path} uses dimension "
                                f"{dimension_name!r}"
                            ),
                        )
                    )
            emitted_attrs = _emit_netcdf_attributes(
                emitter, source, variable, variable_path, variable_unit, state
            )
            if _netcdf_numeric(variable) and not _has_units(emitted_attrs):
                missing_units.append(variable_path)
            if not _has_documentation(emitted_attrs):
                missing_docs.append(variable_path)
            if _netcdf_numeric(variable) and not _has_declared_stats(emitted_attrs):
                missing_stats.append(variable_path)

        if variables_truncated:
            _netcdf_object_allowed(state, emitter, path)
            return False

        group_names, groups_truncated = _bounded_catalog_names(
            group.groups, max(0, _MAX_OBJECTS - state.objects)
        )
        for name in group_names:
            if not _netcdf_object_allowed(state, emitter, path):
                return False
            child_path = _netcdf_child_path(path, name)
            child = group.groups[name]
            state.objects += 1
            state.groups += 1
            state.max_depth = max(state.max_depth, depth + 1)
            child_unit = _schema_unit(
                source,
                content=(
                    f"NetCDF group {child_path}: {len(child.dimensions)} local "
                    f"dimension(s), {len(child.variables)} variable(s), "
                    f"{len(child.groups)} child group(s)."
                ),
                ref=child_path,
                structure=_netcdf_structure(child_path),
                salience=0.76,
                meta={
                    "object_type": "group",
                    "path": child_path,
                    "dimensions": len(child.dimensions),
                    "variables": len(child.variables),
                    "groups": len(child.groups),
                },
            )
            if emitter.unit(child_unit):
                object_units[child_path] = child_unit
                emitter.relation(
                    _describe_relation(
                        group_unit, child_unit, f"group {path} contains {child_path}"
                    )
                )
            if depth + 1 >= _MAX_DEPTH:
                if child.dimensions or child.variables or child.groups:
                    emitter.gap(
                        f"NetCDF depth limit {_MAX_DEPTH} stopped traversal below "
                        f"{child_path}",
                        ref=child_path,
                    )
                continue
            if not visit_group(child, child_path, child_unit, depth + 1):
                return False
        if groups_truncated:
            _netcdf_object_allowed(state, emitter, path)
            return False
        return True

    visit_group(dataset, "/", root, 0)
    if state.objects == 1 and not dataset.ncattrs():
        emitter.gap(
            "NetCDF file contains no dimensions, variables, groups, or attributes",
            ref="/",
        )
    _emit_missing_metadata_gap(
        emitter,
        "NetCDF numeric variables declare no units for",
        missing_units,
        ref="/",
    )
    _emit_missing_metadata_gap(
        emitter,
        "NetCDF variables contain no standard_name/long_name/description for",
        missing_docs,
        ref="/",
    )
    _emit_missing_metadata_gap(
        emitter,
        "NetCDF numeric variables declare no valid/actual range for",
        missing_stats,
        ref="/",
    )
    context.result.meta.update(
        {
            "data_model": model,
            "disk_format": disk_format,
            "objects": state.objects,
            "groups": state.groups,
            "dimensions": state.dimensions,
            "variables": state.variables,
            "attributes": state.attributes,
            "max_depth": state.max_depth,
        }
    )


def _emit_netcdf_attributes(
    emitter: _Emitter,
    source: str,
    owner,
    owner_path: str,
    owner_unit: Unit,
    state: _NetcdfState,
) -> list[str]:
    try:
        names, names_truncated = _bounded_catalog_names(
            owner.ncattrs(), max(0, _MAX_ATTRIBUTES - state.attributes)
        )
    except Exception as exc:
        emitter.gap(
            f"NetCDF attributes on {owner_path} could not be enumerated: "
            f"{_error_detail(exc)}",
            ref=owner_path,
        )
        return []
    if names_truncated and not state.attribute_limit_reported:
        emitter.gap(
            f"NetCDF attribute traversal stopped at {_MAX_ATTRIBUTES} attributes",
            ref=owner_path,
        )
        state.attribute_limit_reported = True
    emitted_names: list[str] = []
    for name in names:
        if state.attributes >= _MAX_ATTRIBUTES:
            if not state.attribute_limit_reported:
                emitter.gap(
                    f"NetCDF attribute traversal stopped at {_MAX_ATTRIBUTES} attributes",
                    ref=owner_path,
                )
                state.attribute_limit_reported = True
            break
        state.attributes += 1
        emitted_names.append(name)
        declared_bytes, declared_type, safe_to_read = _netcdf_attribute_extent(
            owner, name
        )
        safe = None
        encoded_bytes = declared_bytes
        omitted = not safe_to_read
        if safe_to_read:
            try:
                value = owner.getncattr(name)
            except Exception as exc:
                emitter.gap(
                    f"NetCDF attribute {name!r} on {owner_path} could not be read: "
                    f"{_error_detail(exc)}",
                    ref=f"{owner_path}#attribute:{_component(name)}",
                )
                continue
            safe = _safe_attribute_value(name, value)
            encoded_bytes = len(
                json.dumps(safe, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            omitted = encoded_bytes > _MAX_ATTRIBUTE_BYTES
        ref = f"{owner_path}#attribute:{_component(name)}"
        unit = _schema_unit(
            source,
            content=(
                f"NetCDF attribute {name!r} on {owner_path}: "
                + (
                    (
                        f"declared type {declared_type}, "
                        + (
                            f"{declared_bytes} byte(s)"
                            if declared_bytes is not None
                            else "unbounded encoded extent"
                        )
                        + "; value omitted before materialization."
                    )
                    if omitted
                    else f"value {_display_scalar(safe)}."
                )
            ),
            ref=ref,
            structure=_netcdf_structure(owner_path) + ("attributes",),
            salience=0.65 if name.casefold() in _SEMANTIC_ATTRIBUTES else 0.45,
            meta={
                "object_type": "attribute",
                "owner": owner_path,
                "name": name,
                "declared_type": declared_type,
                "encoded_bytes": encoded_bytes,
                "value_omitted": omitted,
                **({"value": safe} if not omitted else {}),
            },
        )
        if emitter.unit(unit):
            emitter.relation(
                _describe_relation(owner_unit, unit, f"attribute {name!r} on {owner_path}")
            )
    return emitted_names


def _netcdf_attribute_extent(owner, name: str) -> tuple[int | None, str, bool]:
    """Preflight a NetCDF attribute without asking netCDF4 to allocate it.

    The public ``getncattr`` API materializes the complete value.  Resolve the
    already-loaded extension's ``nc_inq_att`` symbol to inspect primitive type
    and element count first.  NC_STRING and user-defined types are refused:
    their descriptor count does not bound variable-length heap payloads.
    """

    import ctypes

    global _NETCDF_INQ_ATT
    try:
        if _NETCDF_INQ_ATT is None:
            import netCDF4

            library = ctypes.CDLL(netCDF4._netCDF4.__file__)
            function = library.nc_inq_att
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            function.restype = ctypes.c_int
            _NETCDF_INQ_ATT = function

        # netCDF4 overloads __getattr__ to read global attributes and raises
        # KeyError for missing private names.  Bypass it for Cython fields.
        try:
            group = object.__getattribute__(owner, "_grp")
        except AttributeError:
            group = owner
        group_id = int(object.__getattribute__(group, "_grpid"))
        try:
            variable_id = int(object.__getattribute__(owner, "_varid"))
        except AttributeError:
            variable_id = -1
        attribute_type = ctypes.c_int()
        element_count = ctypes.c_size_t()
        status = _NETCDF_INQ_ATT(
            group_id,
            variable_id,
            name.encode("utf-8", errors="strict"),
            ctypes.byref(attribute_type),
            ctypes.byref(element_count),
        )
    except Exception:
        return None, "unknown", False
    if status != 0:
        return None, "unknown", False

    # netcdf.h primitive type IDs.  NC_STRING (12) is intentionally absent.
    widths = {
        1: ("NC_BYTE", 1),
        2: ("NC_CHAR", 1),
        3: ("NC_SHORT", 2),
        4: ("NC_INT", 4),
        5: ("NC_FLOAT", 4),
        6: ("NC_DOUBLE", 8),
        7: ("NC_UBYTE", 1),
        8: ("NC_USHORT", 2),
        9: ("NC_UINT", 4),
        10: ("NC_INT64", 8),
        11: ("NC_UINT64", 8),
    }
    if attribute_type.value == 12:
        return None, "NC_STRING", False
    descriptor = widths.get(attribute_type.value)
    if descriptor is None:
        return None, f"USER_TYPE_{attribute_type.value}", False
    type_name, width = descriptor
    declared_bytes = int(element_count.value) * width
    return declared_bytes, type_name, declared_bytes <= _MAX_ATTRIBUTE_BYTES


def _netcdf_object_allowed(
    state: _NetcdfState, emitter: _Emitter, ref: str
) -> bool:
    if state.objects < _MAX_OBJECTS:
        return True
    if not state.object_limit_reported:
        emitter.gap(
            f"NetCDF object traversal stopped at {_MAX_OBJECTS} objects",
            ref=ref,
        )
        state.object_limit_reported = True
    return False


def _netcdf_child_path(parent: str, name: str) -> str:
    return "/" + name if parent == "/" else parent.rstrip("/") + "/" + name


def _netcdf_structure(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.split("/") if part)


def _resolve_netcdf_dimension(
    dimensions: Mapping[tuple[str, str], Unit], path: str, name: str
) -> Unit | None:
    current = path
    while True:
        unit = dimensions.get((current, name.casefold()))
        if unit is not None:
            return unit
        if current == "/":
            return None
        parent = current.rpartition("/")[0]
        current = parent or "/"


def _netcdf_numeric(variable) -> bool:
    return str(getattr(variable.dtype, "kind", "")) in {"b", "i", "u", "f", "c"}


def _safe_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        str(key): _safe_scalar(item)
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    }


_SEMANTIC_ATTRIBUTES = frozenset(
    {
        "actual_range",
        "comment",
        "description",
        "long_name",
        "standard_name",
        "title",
        "unit",
        "units",
        "valid_max",
        "valid_min",
        "valid_range",
    }
)
_DOCUMENTATION_ATTRIBUTES = frozenset(
    {"comment", "description", "long_name", "standard_name", "title"}
)
_UNIT_ATTRIBUTES = frozenset({"unit", "units"})
_STATISTIC_ATTRIBUTES = frozenset(
    {"actual_range", "valid_max", "valid_min", "valid_range"}
)


def _folded_names(names: Iterable[str]) -> set[str]:
    return {str(name).casefold() for name in names}


def _has_documentation(names: Iterable[str]) -> bool:
    return bool(_folded_names(names) & _DOCUMENTATION_ATTRIBUTES)


def _has_units(names: Iterable[str]) -> bool:
    return bool(_folded_names(names) & _UNIT_ATTRIBUTES)


def _has_declared_stats(names: Iterable[str]) -> bool:
    return bool(_folded_names(names) & _STATISTIC_ATTRIBUTES)


def _emit_missing_metadata_gap(
    emitter: _Emitter, prefix: str, paths: list[str], *, ref: str
) -> None:
    if not paths:
        return
    ordered = sorted(set(paths))
    shown = ", ".join(ordered[:8])
    more = len(ordered) - min(8, len(ordered))
    emitter.gap(
        f"{prefix} {shown}" + (f" and {more} more" if more else ""),
        ref=ref,
    )
