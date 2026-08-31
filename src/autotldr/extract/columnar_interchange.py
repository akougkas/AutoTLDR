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
- Never calls read_table, read_feather, get_batch, read_all, read_stripe, or any materializing API.
- All heavy dependencies (pyarrow) are imported lazily inside the extractor.
- Enforces strict bounds on file sizes, field counts, batch/stripe counts, and metadata bytes.
- Scrubs parser exceptions to prevent private paths or binary buffer leaks.
- Single-descriptor same-byte lifecycle verification with post-extraction digest, fstat, and inode checks.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping
from urllib.parse import quote

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

# ---------------------------------------------------------------------------
# Strict limits & bounds
# ---------------------------------------------------------------------------
_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB
_MAX_FIELDS = 1024
_MAX_METADATA_KEYS = 256
_MAX_METADATA_VALUE_BYTES = 4096
_MAX_TEXT_CHARS = 1024
_MAX_NAME_CHARS = 256
_MAX_BATCHES = 65536
_MAX_STRIPES = 65536
_HASH_CHUNK_BYTES = 1024 * 1024


class InvalidColumnarData(ValueError):
    """A recognized columnar interchange file is corrupt, unsafe, empty, or exceeds bounds."""

    tier = 3

    def __init__(self, path: Path | str, kind: str, detail: str) -> None:
        self.path = Path(path)
        self.kind = kind
        self.detail = _bounded_text(detail, 200)
        # Scrub full local directory path from the message string
        super().__init__(f"{self.path.name}: invalid {kind}: {self.detail}")


@dataclass(frozen=True, slots=True)
class _ReadContext:
    path: Path
    file_obj: BinaryIO
    identity: tuple[int, int, int, int]
    initial_digest: str
    result: Extraction


def _get_arrow_ipc() -> tuple[Any, Any]:
    """Lazy import pyarrow.ipc and pyarrow.lib with deterministic named failure."""
    try:
        import pyarrow.ipc as ipc
        import pyarrow.lib as pa_lib

        return ipc, pa_lib
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Arrow IPC support requires pyarrow; install it with: pip install pyarrow"
        ) from exc


def _get_arrow_orc() -> tuple[Any, Any]:
    """Lazy import pyarrow.orc and pyarrow.lib with deterministic named failure."""
    try:
        import pyarrow.lib as pa_lib
        import pyarrow.orc as orc

        return orc, pa_lib
    except ModuleNotFoundError as exc:
        raise ImportError(
            "ORC support requires pyarrow; install it with: pip install pyarrow"
        ) from exc


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _safe_error_message(phase: str, exc: BaseException) -> str:
    """Produce a bounded, stable description without leaking paths, schema values, or binary reprs."""
    cls_name = exc.__class__.__name__
    return f"{phase} failed ({cls_name})"


def _safe_metadata_dict(
    metadata: Mapping[Any, Any] | None,
    origin_ref: str,
    context: _ReadContext,
) -> dict[str, str]:
    if not metadata:
        return {}

    def _raw_key_sort(k: Any) -> tuple[int, bytes]:
        if isinstance(k, bytes):
            return (0, k)
        if isinstance(k, str):
            return (1, k.encode("utf-8", errors="surrogatepass"))
        raise TypeError(f"Unexpected metadata key type: {type(k).__name__}")

    sorted_items = sorted(metadata.items(), key=lambda kv: _raw_key_sort(kv[0]))
    total_keys = len(sorted_items)

    if total_keys > _MAX_METADATA_KEYS:
        suppressed = total_keys - _MAX_METADATA_KEYS
        context.result.add_gap(
            f"Metadata key count exceeds limit ({total_keys} > {_MAX_METADATA_KEYS}); {suppressed} excess keys omitted",
            ref=origin_ref,
        )
        sorted_items = sorted_items[:_MAX_METADATA_KEYS]

    safe: dict[str, str] = {}
    for idx, (k, v) in enumerate(sorted_items):
        # 1. Process key
        if isinstance(k, bytes):
            try:
                k_decoded = k.decode("utf-8")
                k_key = _bounded_text(k_decoded, _MAX_NAME_CHARS)
                if len(k_decoded) > _MAX_NAME_CHARS:
                    context.result.add_gap(
                        f"Metadata key {k_key!r} exceeds length limit ({len(k_decoded)} > {_MAX_NAME_CHARS}) and was truncated",
                        ref=origin_ref,
                    )
            except UnicodeDecodeError:
                k_digest = hashlib.sha256(k).hexdigest()
                k_key = f"binary_key_{idx}_{k_digest[:8]}"
                context.result.add_gap(
                    f"Metadata key at index {idx} contains non-UTF-8 binary data ({len(k)} bytes, sha256={k_digest}); indexed as {k_key!r}",
                    ref=origin_ref,
                )
        elif isinstance(k, str):
            k_key = _bounded_text(k, _MAX_NAME_CHARS)
            if len(k) > _MAX_NAME_CHARS:
                context.result.add_gap(
                    f"Metadata key {k_key!r} exceeds length limit ({len(k)} > {_MAX_NAME_CHARS}) and was truncated",
                    ref=origin_ref,
                )
        else:
            raise TypeError(f"Unexpected metadata key type: {type(k).__name__}")

        # Duplicate key normalization check
        if k_key in safe:
            k_orig = k_key
            k_key = f"{k_orig}#{idx}"
            context.result.add_gap(
                f"Metadata key normalized to duplicate {k_orig!r}; stored as index-qualified key {k_key!r}",
                ref=origin_ref,
            )

        # 2. Process value
        if isinstance(v, bytes):
            v_len = len(v)
            if v_len > _MAX_METADATA_VALUE_BYTES:
                v_digest = hashlib.sha256(v).hexdigest()
                v_str = f"<{v_len} bytes, sha256={v_digest}>"
                context.result.add_gap(
                    f"Metadata key {k_key!r} value exceeds {_MAX_METADATA_VALUE_BYTES} bytes ({v_len} bytes); summarized as sha256 digest",
                    ref=origin_ref,
                )
            else:
                try:
                    v_decoded = v.decode("utf-8")
                    v_str = _bounded_text(v_decoded, _MAX_TEXT_CHARS)
                    if len(v_decoded) > _MAX_TEXT_CHARS:
                        context.result.add_gap(
                            f"Metadata key {k_key!r} value text exceeds {_MAX_TEXT_CHARS} chars and was truncated",
                            ref=origin_ref,
                        )
                except UnicodeDecodeError:
                    v_digest = hashlib.sha256(v).hexdigest()
                    v_str = f"<{v_len} bytes non-UTF8 binary, sha256={v_digest}>"
                    context.result.add_gap(
                        f"Metadata key {k_key!r} value contains non-UTF-8 binary data ({v_len} bytes); summarized as sha256 digest",
                        ref=origin_ref,
                    )
        elif isinstance(v, str):
            v_bytes = v.encode("utf-8", errors="surrogatepass")
            v_len = len(v_bytes)
            if v_len > _MAX_METADATA_VALUE_BYTES:
                v_digest = hashlib.sha256(v_bytes).hexdigest()
                v_str = f"<{v_len} bytes, sha256={v_digest}>"
                context.result.add_gap(
                    f"Metadata key {k_key!r} value exceeds {_MAX_METADATA_VALUE_BYTES} bytes ({v_len} bytes); summarized as sha256 digest",
                    ref=origin_ref,
                )
            else:
                v_str = _bounded_text(v, _MAX_TEXT_CHARS)
                if len(v) > _MAX_TEXT_CHARS:
                    context.result.add_gap(
                        f"Metadata key {k_key!r} value text exceeds {_MAX_TEXT_CHARS} chars and was truncated",
                        ref=origin_ref,
                    )
        else:
            raise TypeError(f"Unexpected metadata value type: {type(v).__name__}")

        safe[k_key] = v_str

    return safe


# ---------------------------------------------------------------------------
# Magic and structural detection
# ---------------------------------------------------------------------------
def detect_columnar_kind(path: str | Path) -> str:
    """Detect whether a file is Arrow IPC File, Arrow Stream, Feather v2, Feather v1, or ORC.

    Detection uses verified byte structures rather than file suffix alone.
    Fails closed on unknown bytes.
    """
    path = Path(path)
    try:
        st = path.stat()
    except OSError as exc:
        raise InvalidColumnarData(path, "columnar", _safe_error_message("stat", exc)) from exc

    if not stat.S_ISREG(st.st_mode):
        raise InvalidColumnarData(path, "columnar", "input is not a regular file")
    if st.st_size == 0:
        raise InvalidColumnarData(path, "columnar", "file is empty")
    if st.st_size > _MAX_FILE_BYTES:
        raise InvalidColumnarData(
            path, "columnar", f"file size {st.st_size} exceeds limit {_MAX_FILE_BYTES}"
        )

    size = st.st_size
    try:
        with path.open("rb") as f:
            header = f.read(16)
            footer = b""
            if size >= 16:
                f.seek(max(0, size - 16))
                footer = f.read(16)
            elif size >= 4:
                f.seek(max(0, size - 4))
                footer = f.read(4)
    except OSError as exc:
        raise InvalidColumnarData(path, "columnar", _safe_error_message("file read", exc)) from exc

    # 1. Feather v1 magic: BOTH starts with 'FEA1' AND ends with 'FEA1' (minimum size 8)
    if size >= 8 and header.startswith(b"FEA1") and footer.endswith(b"FEA1"):
        return "feather-v1"

    # 2. Arrow IPC File (including Feather v2): BOTH starts with 'ARROW1' AND ends with 'ARROW1'
    if size >= 12 and header.startswith(b"ARROW1") and footer.endswith(b"ARROW1"):
        if path.suffix.lower() == ".feather":
            return "feather"
        return "arrow-file"

    # 3. Apache ORC magic: 'ORC' at start (offset 0)
    if size >= 3 and header.startswith(b"ORC"):
        return "orc"

    # 4. Arrow IPC Stream:
    # A stream starting with the continuation word 0xFFFFFFFF is structurally prefixed as an Arrow stream.
    if header.startswith(b"\xff\xff\xff\xff"):
        # Plausible/structurally prefixed Arrow stream: requires pyarrow to validate
        ipc, pa_lib = _get_arrow_ipc()
        try:
            with path.open("rb") as stream_f:
                reader = ipc.open_stream(stream_f)
                schema = reader.schema
                if hasattr(reader, "close"):
                    reader.close()
                if schema is not None:
                    return "arrow-stream"
        except (
            pa_lib.ArrowInvalid,
            pa_lib.ArrowIOError,
            pa_lib.ArrowNotImplementedError,
            OSError,
            ValueError,
            EOFError,
        ):
            pass

    # Legacy stream candidate (pre-0.15 legacy stream without continuation marker)
    elif size >= 8:
        try:
            ipc, pa_lib = _get_arrow_ipc()
        except ImportError:
            # PyArrow is absent and bytes had no continuation marker: fail closed
            pass
        else:
            try:
                with path.open("rb") as stream_f:
                    reader = ipc.open_stream(stream_f)
                    schema = reader.schema
                    if hasattr(reader, "close"):
                        reader.close()
                    if schema is not None:
                        return "arrow-stream"
            except (
                pa_lib.ArrowInvalid,
                pa_lib.ArrowIOError,
                pa_lib.ArrowNotImplementedError,
                OSError,
                ValueError,
                EOFError,
            ):
                pass

    raise InvalidColumnarData(
        path,
        "columnar",
        "unrecognized or unsupported columnar interchange signature (failed closed)",
    )


# ---------------------------------------------------------------------------
# Extractor Lifecycle & Same-Byte Binding
# ---------------------------------------------------------------------------
def _begin_and_bind(path: Path | str, kind: str) -> _ReadContext:
    path = Path(path)
    try:
        file_obj = path.open("rb")
    except OSError as exc:
        raise InvalidColumnarData(path, kind, _safe_error_message("file open", exc)) from exc

    try:
        st = os.fstat(file_obj.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise InvalidColumnarData(path, kind, "input is not a regular file")
        if st.st_size == 0:
            raise InvalidColumnarData(path, kind, "file is empty")
        if st.st_size > _MAX_FILE_BYTES:
            raise InvalidColumnarData(
                path,
                kind,
                f"file is {st.st_size} bytes; limit is {_MAX_FILE_BYTES} bytes",
            )

        identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

        file_obj.seek(0)
        digest = hashlib.sha256()
        byte_count = 0
        while chunk := file_obj.read(_HASH_CHUNK_BYTES):
            byte_count += len(chunk)
            digest.update(chunk)

        if byte_count != st.st_size:
            raise InvalidColumnarData(path, kind, "source changed while it was fingerprinted")

        file_obj.seek(0)
    except BaseException:
        file_obj.close()
        raise

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
                    "metadata_value_bytes": _MAX_METADATA_VALUE_BYTES,
                    "text_chars": _MAX_TEXT_CHARS,
                    "name_chars": _MAX_NAME_CHARS,
                    "batches": _MAX_BATCHES,
                    "stripes": _MAX_STRIPES,
                },
            },
        }
    )
    return _ReadContext(
        path=path,
        file_obj=file_obj,
        identity=identity,
        initial_digest=digest.hexdigest(),
        result=result,
    )


def _verify_and_finish(context: _ReadContext) -> Extraction:
    try:
        # 1. Post-rehash descriptor and check byte count and sha256
        context.file_obj.seek(0)
        post_digest = hashlib.sha256()
        rehash_count = 0
        while chunk := context.file_obj.read(_HASH_CHUNK_BYTES):
            rehash_count += len(chunk)
            post_digest.update(chunk)

        if rehash_count != context.identity[2] or post_digest.hexdigest() != context.initial_digest:
            raise InvalidColumnarData(
                context.path,
                context.result.kind,
                "source changed while it was being extracted",
            )

        # 2. Check fstat identity on the open descriptor
        st = os.fstat(context.file_obj.fileno())
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != context.identity:
            raise InvalidColumnarData(
                context.path,
                context.result.kind,
                "source changed while it was being extracted",
            )

        # 3. Check path identity on the filesystem
        try:
            path_st = context.path.stat()
            if (path_st.st_dev, path_st.st_ino) != (st.st_dev, st.st_ino):
                raise InvalidColumnarData(
                    context.path,
                    context.result.kind,
                    "source pathname replaced while it was being extracted",
                )
        except OSError as exc:
            raise InvalidColumnarData(
                context.path,
                context.result.kind,
                _safe_error_message("source path stat", exc),
            ) from exc
    finally:
        if not context.file_obj.closed:
            context.file_obj.close()

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
    is_stream: bool = False,
) -> None:
    source = str(context.path)
    safe_meta = _safe_metadata_dict(getattr(schema, "metadata", None), "schema:table", context)
    field_count = len(schema.names) if hasattr(schema, "names") else len(schema)

    # 1. Dataset root unit
    parts = [f"{kind_label} {context.path.name}: {field_count} fields"]
    if row_count is not None:
        parts.append(f"{row_count} rows")
    if batch_count is not None:
        parts.append(f"{batch_count} record batches")
        if batch_count > _MAX_BATCHES:
            suppressed = batch_count - _MAX_BATCHES
            context.result.add_gap(
                f"Record batch count exceeds limit ({batch_count} > {_MAX_BATCHES}); {suppressed} excess batches noted",
                ref="schema:table",
            )
    if stripe_count is not None:
        parts.append(f"{stripe_count} stripes")
        if stripe_count > _MAX_STRIPES:
            suppressed = stripe_count - _MAX_STRIPES
            context.result.add_gap(
                f"Stripe count exceeds limit ({stripe_count} > {_MAX_STRIPES}); {suppressed} excess stripes noted",
                ref="schema:table",
            )

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

    # Stream unvalidated tail gap
    if is_stream:
        context.result.add_gap(
            "Arrow IPC Stream later stream payload integrity and batch count were not validated (schema-only metadata extraction)",
            ref="schema:table",
        )

    # Absence check on fields
    if field_count == 0:
        context.result.add_gap(
            f"{kind_label} schema declares 0 fields/columns",
            ref="schema:table",
        )

    # Absence check on metadata
    if not safe_meta:
        context.result.add_gap(
            "No custom application metadata declared in schema",
            ref="schema:table",
        )

    if field_count == 0:
        return

    # Check field bounds
    if field_count > _MAX_FIELDS:
        suppressed_fields = field_count - _MAX_FIELDS
        context.result.add_gap(
            f"Field count exceeds limit ({field_count} > {_MAX_FIELDS}); {suppressed_fields} remaining fields omitted",
            ref="schema:table",
        )

    # 2. Field units (index-qualified to handle duplicate field names unambiguously)
    for idx, field in enumerate(schema):
        if idx >= _MAX_FIELDS:
            break

        f_name_raw = str(field.name)
        f_name = _bounded_text(f_name_raw, _MAX_NAME_CHARS)
        origin_ref = f"field:{idx}:{quote(f_name, safe='-._~')}"

        if len(f_name_raw) > _MAX_NAME_CHARS:
            context.result.add_gap(
                f"Field {idx} name exceeds length limit ({len(f_name_raw)} > {_MAX_NAME_CHARS}) and was truncated",
                ref=origin_ref,
            )

        f_meta = _safe_metadata_dict(getattr(field, "metadata", None), origin_ref, context)

        f_type_raw = str(field.type)
        f_type_str = _bounded_text(f_type_raw, _MAX_TEXT_CHARS)
        if len(f_type_raw) > _MAX_TEXT_CHARS:
            context.result.add_gap(
                f"Field {idx} ({f_name}) type representation exceeds length limit ({len(f_type_raw)} > {_MAX_TEXT_CHARS}) and was truncated",
                ref=origin_ref,
            )

        f_content = f"Field {idx} ({f_name}): type={f_type_str}, nullable={field.nullable}"

        field_unit = Unit(
            source=source,
            modality=Modality.SCHEMA,
            content=f_content,
            origin=Origin(source, origin_ref),
            role=Role.UNKNOWN,
            structure=("table", context.path.name, f"{idx}:{f_name}"),
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
    context = _begin_and_bind(path, "arrow-file")
    try:
        # Check framing through bound descriptor
        context.file_obj.seek(0)
        header = context.file_obj.read(16)
        size = context.identity[2]
        footer = b""
        if size >= 16:
            context.file_obj.seek(max(0, size - 16))
            footer = context.file_obj.read(16)
        context.file_obj.seek(0)

        if not (size >= 12 and header.startswith(b"ARROW1") and footer.endswith(b"ARROW1")):
            raise InvalidColumnarData(
                context.path,
                "arrow-file",
                "missing Arrow IPC file ARROW1 framing",
            )

        ipc, pa_lib = _get_arrow_ipc()

        try:
            reader = ipc.open_file(context.file_obj)
            schema = reader.schema
            num_batches = reader.num_record_batches
            if hasattr(reader, "close"):
                reader.close()
        except (
            pa_lib.ArrowInvalid,
            pa_lib.ArrowIOError,
            pa_lib.ArrowNotImplementedError,
            OSError,
            ValueError,
            EOFError,
        ) as exc:
            raise InvalidColumnarData(
                context.path, "arrow-file", _safe_error_message("arrow ipc file parse", exc)
            ) from exc

        _extract_arrow_schema_units(
            context,
            schema,
            kind_label="Arrow IPC File",
            batch_count=num_batches,
        )
        return _verify_and_finish(context)
    finally:
        if not context.file_obj.closed:
            context.file_obj.close()


def extract_arrow_stream(path: str | Path) -> Extraction:
    """Extract metadata from an Apache Arrow IPC Stream."""
    path = Path(path)
    context = _begin_and_bind(path, "arrow-stream")
    try:
        context.file_obj.seek(0)
        ipc, pa_lib = _get_arrow_ipc()

        try:
            reader = ipc.open_stream(context.file_obj)
            schema = reader.schema
            if hasattr(reader, "close"):
                reader.close()
        except (
            pa_lib.ArrowInvalid,
            pa_lib.ArrowIOError,
            pa_lib.ArrowNotImplementedError,
            OSError,
            ValueError,
            EOFError,
        ) as exc:
            raise InvalidColumnarData(
                context.path, "arrow-stream", _safe_error_message("arrow ipc stream parse", exc)
            ) from exc

        _extract_arrow_schema_units(
            context,
            schema,
            kind_label="Arrow IPC Stream",
            is_stream=True,
        )
        return _verify_and_finish(context)
    finally:
        if not context.file_obj.closed:
            context.file_obj.close()


def extract_feather(path: str | Path) -> Extraction:
    """Extract metadata from Feather format file (Feather v2 supported; v1 declined)."""
    path = Path(path)
    context = _begin_and_bind(path, "feather")
    try:
        # Check framing through bound descriptor
        context.file_obj.seek(0)
        header = context.file_obj.read(16)
        size = context.identity[2]
        footer = b""
        if size >= 16:
            context.file_obj.seek(max(0, size - 16))
            footer = context.file_obj.read(16)
        elif size >= 4:
            context.file_obj.seek(max(0, size - 4))
            footer = context.file_obj.read(4)
        context.file_obj.seek(0)

        if header.startswith(b"FEA1"):
            if size >= 8 and footer.endswith(b"FEA1"):
                raise InvalidColumnarData(
                    context.path,
                    "feather",
                    "unsupported subtype: Feather v1 requires table array materialization; only Feather v2 metadata inspection is supported",
                )
            raise InvalidColumnarData(
                context.path,
                "feather",
                "missing Feather framing (corrupt or unrecognized framing)",
            )

        if not (size >= 12 and header.startswith(b"ARROW1") and footer.endswith(b"ARROW1")):
            raise InvalidColumnarData(
                context.path,
                "feather",
                "missing Feather v2 / Arrow IPC file ARROW1 framing",
            )

        ipc, pa_lib = _get_arrow_ipc()

        try:
            reader = ipc.open_file(context.file_obj)
            schema = reader.schema
            num_batches = reader.num_record_batches
            if hasattr(reader, "close"):
                reader.close()
        except (
            pa_lib.ArrowInvalid,
            pa_lib.ArrowIOError,
            pa_lib.ArrowNotImplementedError,
            OSError,
            ValueError,
            EOFError,
        ) as exc:
            raise InvalidColumnarData(
                context.path, "feather", _safe_error_message("feather metadata parse", exc)
            ) from exc

        _extract_arrow_schema_units(
            context,
            schema,
            kind_label="Feather v2",
            batch_count=num_batches,
        )
        return _verify_and_finish(context)
    finally:
        if not context.file_obj.closed:
            context.file_obj.close()


def extract_orc(path: str | Path) -> Extraction:
    """Extract metadata from an Apache ORC file."""
    path = Path(path)
    context = _begin_and_bind(path, "orc")
    try:
        context.file_obj.seek(0)
        header = context.file_obj.read(16)
        size = context.identity[2]
        context.file_obj.seek(0)

        if not (size >= 3 and header.startswith(b"ORC")):
            raise InvalidColumnarData(
                context.path,
                "orc",
                "missing Apache ORC magic signature",
            )

        orc, pa_lib = _get_arrow_orc()

        try:
            orc_file = orc.ORCFile(context.file_obj)
            schema = orc_file.schema
            nstripes = orc_file.nstripes
            nrows = orc_file.nrows
            if hasattr(orc_file, "close"):
                orc_file.close()
        except (
            pa_lib.ArrowInvalid,
            pa_lib.ArrowIOError,
            pa_lib.ArrowNotImplementedError,
            OSError,
            ValueError,
            EOFError,
        ) as exc:
            raise InvalidColumnarData(
                context.path, "orc", _safe_error_message("orc metadata parse", exc)
            ) from exc

        _extract_arrow_schema_units(
            context,
            schema,
            kind_label="Apache ORC",
            row_count=nrows,
            stripe_count=nstripes,
        )
        return _verify_and_finish(context)
    finally:
        if not context.file_obj.closed:
            context.file_obj.close()


def extract_columnar_interchange(path: str | Path) -> Extraction:
    """Unified entry point for Arrow IPC File, Arrow Stream, Feather v2, and ORC."""
    path = Path(path)
    kind = detect_columnar_kind(path)
    if kind == "feather-v1":
        raise InvalidColumnarData(
            path,
            "feather",
            "unsupported subtype: Feather v1 requires table array materialization; only Feather v2 metadata inspection is supported",
        )
    elif kind == "arrow-file":
        return extract_arrow_file(path)
    elif kind == "feather":
        return extract_feather(path)
    elif kind == "orc":
        return extract_orc(path)
    elif kind == "arrow-stream":
        return extract_arrow_stream(path)
    else:
        raise InvalidColumnarData(path, "columnar", f"unsupported columnar kind {kind!r}")
