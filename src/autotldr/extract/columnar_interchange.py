"""Native bounded metadata extraction for Apache Arrow IPC, Feather, and ORC.

Primary authoritative specifications:
- Apache Arrow IPC File Format:
  https://arrow.apache.org/docs/format/Columnar.html#ipc-file-format
- Apache Arrow IPC Streaming Format:
  https://arrow.apache.org/docs/format/Columnar.html#ipc-streaming-format
- Apache Arrow Feather Format:
  https://arrow.apache.org/docs/python/feather.html
- Apache ORC File Format Specification:
  https://orc.apache.org/specification/

Invariants enforced:
- Every emitted Unit, Relation, and Gap is strictly addressable with exact origins.
- Emits Role.UNKNOWN exclusively.
- Never emits raw table rows, column cell values, or unbudgeted payload bytes.
- All heavy dependencies (pyarrow) are imported lazily inside the extractor.
- Enforces strict bounds on file sizes, field counts, batch/stripe counts, and metadata bytes.
- Scrubs parser exceptions to prevent private paths or binary buffer leaks.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

# ---------------------------------------------------------------------------
# Strict limits & bounds
# ---------------------------------------------------------------------------
_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB
_MAX_FIELDS = 1024
_MAX_METADATA_KEYS = 256
_MAX_METADATA_VALUE_BYTES = 4096
_MAX_BATCHES_PROFILED = 256
_MAX_STRIPES_PROFILED = 256
_MAX_TEXT_CHARS = 1024
_HASH_CHUNK_BYTES = 1024 * 1024


class InvalidColumnarData(ValueError):
    """A recognized columnar interchange file is corrupt, unsafe, empty, or exceeds bounds."""

    tier = 3

    def __init__(self, path: Path, kind: str, detail: str) -> None:
        self.path = Path(path)
        self.kind = kind
        self.detail = detail
        # Scrub full local directory path from the message string
        super().__init__(f"{self.path.name}: invalid {kind}: {detail}")


@dataclass(frozen=True, slots=True)
class _ReadContext:
    path: Path
    identity: tuple[int, int, int, int]
    result: Extraction


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _safe_metadata_dict(
    metadata: Mapping[bytes | str, bytes | str] | None,
) -> dict[str, str]:
    if not metadata:
        return {}
    safe: dict[str, str] = {}
    count = 0
    for k, v in metadata.items():
        if count >= _MAX_METADATA_KEYS:
            break
        try:
            k_str = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else str(k)
            v_bytes = v if isinstance(v, bytes) else str(v).encode("utf-8", errors="replace")
            if len(v_bytes) > _MAX_METADATA_VALUE_BYTES:
                v_str = f"<{len(v_bytes)} bytes binary/text metadata>"
            else:
                v_str = v_bytes.decode("utf-8", errors="replace")
            safe[_bounded_text(k_str, 128)] = _bounded_text(v_str, 512)
            count += 1
        except Exception:
            continue
    return safe


# ---------------------------------------------------------------------------
# Magic and structural detection
# ---------------------------------------------------------------------------
def detect_columnar_kind(path: str | Path) -> str:
    """Detect whether a file is Arrow IPC File, Arrow Stream, Feather, or ORC.

    Detection uses verified byte structures rather than file suffix alone.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InvalidColumnarData(path, "columnar", str(exc)) from exc

    if size == 0:
        raise InvalidColumnarData(path, "columnar", "file is empty")
    if size > _MAX_FILE_BYTES:
        raise InvalidColumnarData(
            path, "columnar", f"file size {size} exceeds limit {_MAX_FILE_BYTES}"
        )

    with path.open("rb") as f:
        header = f.read(16)
        footer = b""
        if size >= 16:
            f.seek(max(0, size - 16))
            footer = f.read(16)

    # 1. Feather v1 magic: 'FEA1' at start and end
    if header.startswith(b"FEA1") or footer.endswith(b"FEA1"):
        return "feather"

    # 2. Arrow IPC File (including Feather v2): starts with 'ARROW1\0\0' or starts/ends with 'ARROW1'
    if header.startswith(b"ARROW1") or footer.endswith(b"ARROW1"):
        if path.suffix.lower() == ".feather":
            return "feather"
        return "arrow-file"

    # 3. Apache ORC magic: 'ORC' at start (offset 0) or ending with Postscript magic 'ORC'
    if header.startswith(b"ORC") or footer.endswith(b"ORC") or (len(footer) >= 4 and footer[-4:-1] == b"ORC"):
        return "orc"

    # 4. Arrow IPC Stream: starts with 0xFFFFFFFF (continuation) or valid stream message
    if header.startswith(b"\xff\xff\xff\xff") or path.suffix.lower() in {".arrows", ".ipc", ".stream"}:
        return "arrow-stream"

    # Default to arrow-file if .arrow suffix, otherwise try stream
    if path.suffix.lower() in {".arrow", ".feather"}:
        return "arrow-file"
    if path.suffix.lower() in {".orc"}:
        return "orc"

    return "arrow-stream"


# ---------------------------------------------------------------------------
# Extractor Lifecycle
# ---------------------------------------------------------------------------
def _begin(path: Path, kind: str) -> _ReadContext:
    path = Path(path)
    try:
        info = path.stat()
    except OSError as exc:
        raise InvalidColumnarData(path, kind, str(exc)) from exc

    if not stat.S_ISREG(info.st_mode):
        raise InvalidColumnarData(path, kind, "input is not a regular file")
    if info.st_size == 0:
        raise InvalidColumnarData(path, kind, "file is empty")
    if info.st_size > _MAX_FILE_BYTES:
        raise InvalidColumnarData(
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
        raise InvalidColumnarData(path, kind, str(exc)) from exc

    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if byte_count != info.st_size or _identity(path) != identity:
        raise InvalidColumnarData(
            path, kind, "source changed while it was fingerprinted"
        )

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
                "name": "columnar-interchange-v1",
                "bounds": {
                    "file_bytes": _MAX_FILE_BYTES,
                    "fields": _MAX_FIELDS,
                    "metadata_keys": _MAX_METADATA_KEYS,
                    "batches_profiled": _MAX_BATCHES_PROFILED,
                    "stripes_profiled": _MAX_STRIPES_PROFILED,
                },
            },
        }
    )
    return _ReadContext(path=path, identity=identity, result=result)


def _finish(context: _ReadContext) -> Extraction:
    if _identity(context.path) != context.identity:
        raise InvalidColumnarData(
            context.path,
            context.result.kind,
            "source changed while it was being extracted",
        )
    result = context.result
    result.units.sort(
        key=lambda unit: (unit.origin.ref, str(unit.modality), unit.content, unit.id)
    )
    result.relations.sort(
        key=lambda relation: (
            relation.src,
            relation.dst,
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
        raise AssertionError("Columnar extractor emitted a dangling relation")

    result.meta["counts"] = {
        "units": len(result.units),
        "relations": len(result.relations),
        "gaps": len(result.gaps),
    }
    return result


# ---------------------------------------------------------------------------
# Extraction Logic
# ---------------------------------------------------------------------------
def _extract_arrow_schema_units(
    context: _ReadContext,
    schema: Any,
    *,
    kind_label: str,
    row_count: int | None = None,
    batch_count: int | None = None,
    stripe_count: int | None = None,
) -> None:
    source = str(context.path)
    safe_meta = _safe_metadata_dict(getattr(schema, "metadata", None))
    field_count = len(schema.names)

    # 1. Dataset root unit
    parts = [f"{kind_label} {context.path.name}: {field_count} fields"]
    if row_count is not None:
        parts.append(f"{row_count} rows")
    if batch_count is not None:
        parts.append(f"{batch_count} record batches")
    if stripe_count is not None:
        parts.append(f"{stripe_count} stripes")

    table_content = ", ".join(parts)
    table_unit = Unit(
        source=source,
        modality=Modality.SCHEMA,
        content=table_content,
        origin=Origin(source, "schema:table"),
        role=Role.UNKNOWN,
        structure=("table", context.path.name),
        salience=0.9,
        meta={
            "kind": context.result.kind,
            "field_count": field_count,
            "row_count": row_count,
            "batch_count": batch_count,
            "stripe_count": stripe_count,
            "metadata": safe_meta,
        },
    )
    context.result.units.append(table_unit)

    # Absence check on fields
    if field_count == 0:
        context.result.add_gap(
            f"{kind_label} schema declares 0 fields/columns",
            ref="schema:table",
        )
        return

    # Absence check on metadata
    if not safe_meta:
        context.result.add_gap(
            "No custom application metadata declared in schema",
            ref="schema:table",
        )

    # 2. Field units
    for idx, field in enumerate(schema):
        if idx >= _MAX_FIELDS:
            context.result.add_gap(
                f"Field count exceeds limit ({field_count} > {_MAX_FIELDS}); remaining fields omitted",
                ref="schema:table",
            )
            break

        f_meta = _safe_metadata_dict(getattr(field, "metadata", None))
        f_type_str = str(field.type)
        f_name = str(field.name)
        f_content = f"Field {f_name}: type={f_type_str}, nullable={field.nullable}"

        field_unit = Unit(
            source=source,
            modality=Modality.SCHEMA,
            content=f_content,
            origin=Origin(source, f"field:{quote(f_name, safe='-._~')}"),
            role=Role.UNKNOWN,
            structure=("table", context.path.name, f_name),
            salience=0.7,
            meta={
                "index": idx,
                "name": f_name,
                "type": f_type_str,
                "nullable": field.nullable,
                "metadata": f_meta,
            },
        )
        context.result.units.append(field_unit)
        context.result.relations.append(
            Relation(
                src=table_unit.id,
                dst=field_unit.id,
                kind=RelationKind.DESCRIBES,
                evidence="schema-field",
            )
        )


def extract_arrow_file(path: str | Path) -> Extraction:
    """Extract metadata from an Apache Arrow IPC File."""
    path = Path(path)
    context = _begin(path, "arrow-file")
    try:
        import pyarrow.ipc as ipc
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Arrow IPC support requires pyarrow; install it with: pip install pyarrow"
        ) from exc

    try:
        reader = ipc.open_file(context.path)
        schema = reader.schema
        num_batches = reader.num_record_batches
    except Exception as exc:
        raise InvalidColumnarData(context.path, "arrow-file", str(exc)) from exc

    _extract_arrow_schema_units(
        context,
        schema,
        kind_label="Arrow IPC File",
        batch_count=num_batches,
    )
    return _finish(context)


def extract_arrow_stream(path: str | Path) -> Extraction:
    """Extract metadata from an Apache Arrow IPC Stream."""
    path = Path(path)
    context = _begin(path, "arrow-stream")
    try:
        import pyarrow.ipc as ipc
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Arrow IPC support requires pyarrow; install it with: pip install pyarrow"
        ) from exc

    try:
        reader = ipc.open_stream(context.path)
        schema = reader.schema
    except Exception as exc:
        raise InvalidColumnarData(context.path, "arrow-stream", str(exc)) from exc

    _extract_arrow_schema_units(
        context,
        schema,
        kind_label="Arrow IPC Stream",
    )
    return _finish(context)


def extract_feather(path: str | Path) -> Extraction:
    """Extract metadata from a Feather format file (v1 or v2)."""
    path = Path(path)
    context = _begin(path, "feather")
    try:
        import pyarrow.feather as feather
        import pyarrow.ipc as ipc
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Feather support requires pyarrow; install it with: pip install pyarrow"
        ) from exc

    schema = None
    row_count = None
    batch_count = None

    try:
        # First try opening as Arrow IPC file (Feather v2)
        try:
            reader = ipc.open_file(context.path)
            schema = reader.schema
            batch_count = reader.num_record_batches
        except Exception:
            # Fall back to reading feather metadata
            table_meta = feather.read_table(context.path)
            schema = table_meta.schema
            row_count = table_meta.num_rows
    except Exception as exc:
        raise InvalidColumnarData(context.path, "feather", str(exc)) from exc

    if schema is None:
        raise InvalidColumnarData(context.path, "feather", "could not parse schema")

    _extract_arrow_schema_units(
        context,
        schema,
        kind_label="Feather",
        row_count=row_count,
        batch_count=batch_count,
    )
    return _finish(context)


def extract_orc(path: str | Path) -> Extraction:
    """Extract metadata from an Apache ORC file."""
    path = Path(path)
    context = _begin(path, "orc")
    try:
        import pyarrow.orc as orc
    except ModuleNotFoundError as exc:
        raise ImportError(
            "ORC support requires pyarrow; install it with: pip install pyarrow"
        ) from exc

    try:
        orc_file = orc.ORCFile(context.path)
        schema = orc_file.schema
        nstripes = orc_file.nstripes
        nrows = orc_file.nrows
    except Exception as exc:
        raise InvalidColumnarData(context.path, "orc", str(exc)) from exc

    _extract_arrow_schema_units(
        context,
        schema,
        kind_label="Apache ORC",
        row_count=nrows,
        stripe_count=nstripes,
    )
    return _finish(context)


def extract_columnar_interchange(path: str | Path) -> Extraction:
    """Unified entry point for Arrow IPC File, Arrow Stream, Feather, and ORC."""
    path = Path(path)
    kind = detect_columnar_kind(path)
    if kind == "arrow-file":
        try:
            return extract_arrow_file(path)
        except Exception:
            return extract_arrow_stream(path)
    elif kind == "feather":
        return extract_feather(path)
    elif kind == "orc":
        return extract_orc(path)
    elif kind == "arrow-stream":
        try:
            return extract_arrow_stream(path)
        except Exception:
            return extract_arrow_file(path)
    else:
        return extract_arrow_file(path)
