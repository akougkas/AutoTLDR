"""Native bounded metadata extraction for NumPy NPY (v1-v3) and NPZ archives.

Primary authoritative specifications:
- NumPy NPY Format Specification (Versions 1.0, 2.0, 3.0):
  https://numpy.org/doc/stable/reference/generated/numpy.lib.format.html
- NumPy Array Interface Specification:
  https://numpy.org/doc/stable/reference/arrays.interface.html
- PKWARE .ZIP Application Note Specification:
  https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT

Invariants enforced:
- Every emitted Unit, Relation, and Gap is strictly addressable with exact origins.
- Emits Role.UNKNOWN exclusively.
- Never emits raw array values, cell data, or unbudgeted binary payloads.
- Never loads arrays or calls allow_pickle; object dtypes emit a named safety gap.
- Pure stdlib parsing for headers; lazy numpy dtype helper only for checked itemsize calculation.
- NPZ enforces strict security guards: rejects encrypted entries, duplicate members,
  path traversal (../), directory members, drive prefixes, zero-compressed bombs,
  and decompression bomb ratios.
- All file sizes, member counts, dimensions, payload extents, and header bytes are strictly bounded.
- Scrubs parser exceptions to prevent private paths or binary buffer leaks.
"""

from __future__ import annotations

import ast
import hashlib
import json
import posixpath
import stat
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import quote

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

# ---------------------------------------------------------------------------
# Strict limits & bounds
# ---------------------------------------------------------------------------
_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB
_MAX_NPZ_MEMBERS = 1024
_MAX_NPZ_TOTAL_UNCOMPRESSED = 1024 * 1024 * 1024  # 1 GB
_MAX_DECOMPRESSION_RATIO = 100.0
_MAX_HEADER_BYTES = 64 * 1024  # 64 KB
_MAX_STRUCTURED_FIELDS = 512
_MAX_STRUCTURED_DEPTH = 8
_MAX_DIMENSIONS = 32
_MAX_TEXT_CHARS = 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_NPY_MAGIC = b"\x93NUMPY"


class InvalidScientificArrayData(ValueError):
    """A recognized NPY or NPZ file is corrupt, unsafe, empty, or exceeds bounds."""

    tier = 3

    def __init__(self, path: Path | str, kind: str, detail: str) -> None:
        self.path = Path(path)
        self.kind = kind
        self.detail = _bounded_text(detail, 200)
        super().__init__(f"{self.path.name}: invalid {kind}: {self.detail}")


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
    return text[: max(0, limit - 1)] + "â¦"


def _scrub_error_message(exc: BaseException, phase: str, filename: str) -> str:
    """Produce a bounded, stable, scrubbed error description without private paths or binary buffer dumps."""
    cls_name = exc.__class__.__name__
    raw = str(exc).splitlines()[0] if str(exc) else ""
    parts = []
    for token in raw.split():
        if "/" in token or "\\" in token:
            token = Path(token).name if "/" in token else token
        parts.append(token)
    cleaned = " ".join(parts)
    bounded = _bounded_text(cleaned, 160)
    return f"{phase} failed ({cls_name}): {bounded}"


# ---------------------------------------------------------------------------
# Magic and structural detection
# ---------------------------------------------------------------------------
def detect_scientific_array_kind(path: str | Path) -> str:
    """Detect whether a file is NumPy NPY or NPZ archive.

    Fails closed on unknown or spoofed bytes.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InvalidScientificArrayData(path, "array", f"stat error: {_bounded_text(str(exc), 80)}") from exc

    if size == 0:
        raise InvalidScientificArrayData(path, "array", "file is empty")
    if size > _MAX_FILE_BYTES:
        raise InvalidScientificArrayData(
            path, "array", f"file size {size} exceeds limit {_MAX_FILE_BYTES}"
        )

    with path.open("rb") as f:
        header = f.read(16)

    # 1. Strong NPY magic: ÂNUMPY
    if header.startswith(_NPY_MAGIC):
        return "npy"

    # 2. NPZ is a ZIP archive containing .npy members
    if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(path, mode="r") as zf:
                # Check that at least one member exists or is valid ZIP
                infolist = zf.infolist()
                if any(m.filename.endswith(".npy") for m in infolist) or path.suffix.lower() == ".npz":
                    return "npz"
        except Exception:
            pass

    raise InvalidScientificArrayData(
        path, "array", "unrecognized or unsupported scientific array signature (failed closed)"
    )


# ---------------------------------------------------------------------------
# Extractor Lifecycle
# ---------------------------------------------------------------------------
def _begin(path: Path, kind: str) -> _ReadContext:
    path = Path(path)
    try:
        info = path.stat()
    except OSError as exc:
        raise InvalidScientificArrayData(path, kind, f"stat error: {_bounded_text(str(exc), 80)}") from exc

    if not stat.S_ISREG(info.st_mode):
        raise InvalidScientificArrayData(path, kind, "input is not a regular file")
    if info.st_size == 0:
        raise InvalidScientificArrayData(path, kind, "file is empty")
    if info.st_size > _MAX_FILE_BYTES:
        raise InvalidScientificArrayData(
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
        raise InvalidScientificArrayData(path, kind, f"read error: {_bounded_text(str(exc), 80)}") from exc

    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if byte_count != info.st_size or _identity(path) != identity:
        raise InvalidScientificArrayData(
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
                "name": "scientific-arrays-v1",
                "bounds": {
                    "file_bytes": _MAX_FILE_BYTES,
                    "header_bytes": _MAX_HEADER_BYTES,
                    "structured_fields": _MAX_STRUCTURED_FIELDS,
                    "structured_depth": _MAX_STRUCTURED_DEPTH,
                    "npz_members": _MAX_NPZ_MEMBERS,
                },
            },
        }
    )
    return _ReadContext(path=path, identity=identity, result=result)


def _finish(context: _ReadContext) -> Extraction:
    if _identity(context.path) != context.identity:
        raise InvalidScientificArrayData(
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
        raise AssertionError("Scientific array extractor emitted a dangling relation")

    result.meta["counts"] = {
        "units": len(result.units),
        "relations": len(result.relations),
        "gaps": len(result.gaps),
    }
    return result


# ---------------------------------------------------------------------------
# Bounded Header Parsing (NPY v1, v2, v3) & Dtype Validation
# ---------------------------------------------------------------------------
def _is_object_dtype_recursive(descr: Any, depth: int = 0) -> bool:
    """Check if descr represents or contains Python object dtype ('|O')."""
    if depth > _MAX_STRUCTURED_DEPTH:
        return False
    if isinstance(descr, str):
        return descr.endswith("O") or descr == "O" or descr == "|O"
    if isinstance(descr, (list, tuple)):
        for item in descr:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                if _is_object_dtype_recursive(item[1], depth + 1):
                    return True
    return False


def _get_dtype_info(descr: Any) -> tuple[int, bool]:
    """Return (itemsize_in_bytes, has_object_flag) using lazy numpy dtype constructor."""
    try:
        import numpy as np
        dt = np.dtype(descr)
        return int(dt.itemsize), bool(dt.hasobject)
    except Exception:
        # Fallback for standard primitive types without numpy
        if isinstance(descr, str):
            if descr in ("|b1", "|u1", "|i1"):
                return 1, False
            if descr in ("<i2", ">i2", "<u2", ">u2"):
                return 2, False
            if descr in ("<i4", ">i4", "<u4", ">u4", "<f4", ">f4"):
                return 4, False
            if descr in ("<i8", ">i8", "<u8", ">u8", "<f8", ">f8"):
                return 8, False
            if descr in ("|O", "O"):
                return 8, True
        return 1, _is_object_dtype_recursive(descr)


def _parse_npy_header_bytes(stream, max_read: int = _MAX_HEADER_BYTES) -> tuple[dict[str, Any], int, int, int]:
    """Parse NPY header from open binary stream without loading payload.

    Returns (header_dict, major_version, minor_version, total_header_bytes).
    """
    magic = stream.read(6)
    if magic != _NPY_MAGIC:
        raise ValueError(f"invalid NPY magic signature {magic!r}")

    version_bytes = stream.read(2)
    if len(version_bytes) < 2:
        raise ValueError("truncated NPY version bytes")
    v_maj, v_min = struct.unpack("BB", version_bytes)

    if v_maj not in (1, 2, 3):
        raise ValueError(f"unsupported NPY major version {v_maj}")

    if v_maj == 1:
        hlen_bytes = stream.read(2)
        if len(hlen_bytes) < 2:
            raise ValueError("truncated NPY v1 header length")
        hlen = struct.unpack("<H", hlen_bytes)[0]
        prefix_len = 10
    else:  # v2, v3
        hlen_bytes = stream.read(4)
        if len(hlen_bytes) < 4:
            raise ValueError(f"truncated NPY v{v_maj} header length")
        hlen = struct.unpack("<I", hlen_bytes)[0]
        prefix_len = 12

    if hlen > max_read:
        raise ValueError(f"NPY header length {hlen} exceeds limit {max_read}")

    header_bytes = stream.read(hlen)
    if len(header_bytes) < hlen:
        raise ValueError("truncated NPY header data")

    encoding = "utf-8" if v_maj == 3 else "latin1"
    try:
        header_str = header_bytes.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid NPY header encoding ({encoding})") from exc

    try:
        parsed = ast.literal_eval(header_str.strip())
    except (SyntaxError, ValueError) as exc:
        raise ValueError("malformed NPY header dict syntax") from exc

    if not isinstance(parsed, dict):
        raise ValueError("NPY header is not a dictionary literal")

    for req_key in ("descr", "fortran_order", "shape"):
        if req_key not in parsed:
            raise ValueError(f"missing required key {req_key!r} in NPY header")

    # Strict type validations
    shape = parsed["shape"]
    if not isinstance(shape, (tuple, list)):
        raise ValueError(f"NPY shape must be a tuple/list, got {type(shape).__name__}")
    if len(shape) > _MAX_DIMENSIONS:
        raise ValueError(f"NPY dimensionality {len(shape)} exceeds maximum {_MAX_DIMENSIONS}")
    for dim in shape:
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 0:
            raise ValueError(f"NPY shape dimensions must be non-negative integers, got {dim!r}")

    fortran_order = parsed["fortran_order"]
    if not isinstance(fortran_order, bool):
        raise ValueError(f"NPY fortran_order must be a boolean, got {type(fortran_order).__name__}")

    return parsed, v_maj, v_min, prefix_len + hlen


def _validate_payload_extent(
    total_available_bytes: int,
    total_header_bytes: int,
    shape: tuple[int, ...] | list[int],
    itemsize: int,
) -> None:
    """Verify that the actual data payload extent is sufficient for the declared shape and dtype."""
    total_elements = 1
    for dim in shape:
        total_elements *= dim

    expected_payload = total_elements * itemsize
    actual_payload = total_available_bytes - total_header_bytes

    if actual_payload < expected_payload:
        raise ValueError(
            f"truncated array payload: expected at least {expected_payload} bytes for shape {list(shape)}, found {actual_payload} bytes"
        )


# ---------------------------------------------------------------------------
# NPY Extractor
# ---------------------------------------------------------------------------
def extract_npy(path: str | Path) -> Extraction:
    """Extract array metadata from a NumPy NPY file."""
    path = Path(path)
    context = _begin(path, "npy")
    file_size = context.path.stat().st_size

    try:
        with context.path.open("rb") as f:
            header, v_maj, v_min, total_hdr_len = _parse_npy_header_bytes(f)
    except Exception as exc:
        scrubbed = _scrub_error_message(exc, "npy header parse", context.path.name)
        raise InvalidScientificArrayData(context.path, "npy", scrubbed) from exc

    shape = header["shape"]
    descr = header["descr"]
    fortran_order = bool(header["fortran_order"])
    ndim = len(shape)

    itemsize, has_object = _get_dtype_info(descr)

    # Validate payload extent
    try:
        _validate_payload_extent(file_size, total_hdr_len, shape, itemsize)
    except ValueError as exc:
        scrubbed = _scrub_error_message(exc, "payload validation", context.path.name)
        raise InvalidScientificArrayData(context.path, "npy", scrubbed) from exc

    source = str(context.path)

    # 1. Main Array Unit
    content_parts = [
        f"NumPy NPY v{v_maj}.{v_min}",
        f"shape={list(shape)}",
        f"ndim={ndim}",
        f"order={'Fortran (F)' if fortran_order else 'C'}",
    ]
    if isinstance(descr, str):
        content_parts.append(f"dtype={descr}")
    else:
        content_parts.append(f"dtype=structured({len(descr)} fields)")

    array_content = ", ".join(content_parts)
    array_unit = Unit(
        source=source,
        modality=Modality.SCHEMA,
        content=array_content,
        origin=Origin(source, "array"),
        role=Role.UNKNOWN,
        structure=("array", context.path.name),
        salience=0.9,
        meta={
            "version": f"{v_maj}.{v_min}",
            "shape": list(shape),
            "ndim": ndim,
            "fortran_order": fortran_order,
            "dtype": str(descr) if isinstance(descr, str) else "structured",
            "header_bytes": total_hdr_len,
        },
    )
    context.result.units.append(array_unit)

    # 2. Check for object dtype security risk
    if has_object or _is_object_dtype_recursive(descr):
        context.result.add_gap(
            "Object dtype ('|O') contains pickled Python objects; payload inspection refused for security",
            ref="array",
        )

    # 3. Structured fields (if structured dtype)
    if isinstance(descr, list):
        for idx, field_tuple in enumerate(descr):
            if idx >= _MAX_STRUCTURED_FIELDS:
                context.result.add_gap(
                    f"Structured field count exceeds limit ({len(descr)} > {_MAX_STRUCTURED_FIELDS}); truncated",
                    ref="array",
                )
                break
            if isinstance(field_tuple, (list, tuple)) and len(field_tuple) >= 2:
                f_name = str(field_tuple[0])
                f_type = str(field_tuple[1])
                f_shape = list(field_tuple[2]) if len(field_tuple) > 2 else []
                field_unit = Unit(
                    source=source,
                    modality=Modality.SCHEMA,
                    content=f"Field {idx} ({f_name}): dtype={f_type}" + (f", shape={f_shape}" if f_shape else ""),
                    origin=Origin(source, f"field:{idx}:{quote(f_name, safe='-._~')}"),
                    role=Role.UNKNOWN,
                    structure=("array", context.path.name, f"{idx}:{f_name}"),
                    salience=0.7,
                    meta={
                        "index": idx,
                        "name": f_name,
                        "dtype": f_type,
                        "shape": f_shape,
                    },
                )
                context.result.units.append(field_unit)
                context.result.relations.append(
                    Relation(
                        src=array_unit.id,
                        dst=field_unit.id,
                        kind=RelationKind.DESCRIBES,
                        evidence="structured-field",
                    )
                )

    return _finish(context)


# ---------------------------------------------------------------------------
# NPZ Extractor with Canonical Posix Path & Security Hardening
# ---------------------------------------------------------------------------
def _validate_npz_member_name(name: str) -> PurePosixPath:
    if chr(0) in name:
        raise ValueError(f"NUL byte in member name {name!r}")
    if "\\" in name:
        raise ValueError(f"backslash in member name {name!r}")
    if name.startswith("/"):
        raise ValueError(f"absolute path in member name {name!r}")
    if name.endswith("/"):
        raise ValueError(f"directory member {name!r} is unsupported in NPZ")
    if ":" in name.split("/")[0]:
        raise ValueError(f"drive-like prefix in member name {name!r}")

    p = PurePosixPath(name)
    if p.is_absolute() or any(part in (".", "..") for part in p.parts):
        raise ValueError(f"path traversal or dot component in member name {name!r}")
    return p


def extract_npz(path: str | Path) -> Extraction:
    """Extract member inventory and array metadata from a NumPy NPZ archive."""
    path = Path(path)
    context = _begin(path, "npz")
    source = str(context.path)

    try:
        zf = zipfile.ZipFile(context.path, mode="r")
    except Exception as exc:
        scrubbed = _scrub_error_message(exc, "npz zip open", context.path.name)
        raise InvalidScientificArrayData(context.path, "npz", scrubbed) from exc

    with zf:
        infolist = zf.infolist()
        total_members = len(infolist)
        if total_members == 0:
            raise InvalidScientificArrayData(context.path, "npz", "NPZ archive is empty")

        if total_members > _MAX_NPZ_MEMBERS:
            raise InvalidScientificArrayData(
                context.path,
                "npz",
                f"member count {total_members} exceeds limit {_MAX_NPZ_MEMBERS}",
            )

        # 1. Main Archive Unit
        archive_content = f"NumPy NPZ Archive: {total_members} member arrays"
        archive_unit = Unit(
            source=source,
            modality=Modality.RECORD,
            content=archive_content,
            origin=Origin(source, "archive"),
            role=Role.UNKNOWN,
            structure=("archive", context.path.name),
            salience=0.9,
            meta={"member_count": total_members},
        )
        context.result.units.append(archive_unit)

        seen_names: set[str] = set()
        total_uncompressed = 0

        for member in infolist:
            fname = member.filename

            # Hardened name check
            try:
                _validate_npz_member_name(fname)
            except ValueError as exc:
                raise InvalidScientificArrayData(
                    context.path, "npz", f"security violation on member: {_bounded_text(str(exc), 100)}"
                ) from exc

            # Security: check encrypted flag
            if member.flag_bits & 0x1:
                raise InvalidScientificArrayData(
                    context.path, "npz", f"encrypted member {fname!r} is unsupported"
                )

            # Security: duplicate entries
            if fname in seen_names:
                raise InvalidScientificArrayData(
                    context.path, "npz", f"duplicate archive member {fname!r}"
                )
            seen_names.add(fname)

            # Security: uncompressed size tracking
            total_uncompressed += member.file_size
            if total_uncompressed > _MAX_NPZ_TOTAL_UNCOMPRESSED:
                raise InvalidScientificArrayData(
                    context.path,
                    "npz",
                    f"uncompressed size {total_uncompressed} exceeds limit {_MAX_NPZ_TOTAL_UNCOMPRESSED}",
                )

            # Security: zero-compressed non-empty bomb
            if member.file_size > 0 and member.compress_size == 0:
                raise InvalidScientificArrayData(
                    context.path, "npz", f"zero-compressed non-empty member bomb: {fname!r}"
                )

            # Decompression ratio check
            if member.compress_size > 0:
                ratio = member.file_size / member.compress_size
                if ratio > _MAX_DECOMPRESSION_RATIO and member.file_size > 64 * 1024:
                    raise InvalidScientificArrayData(
                        context.path,
                        "npz",
                        f"suspicious decompression ratio {ratio:.1f} on {fname!r}",
                    )

            # Read only NPY header from member stream without loading payload
            member_ref = f"member:{quote(fname, safe='-._~')}"
            try:
                with zf.open(member, mode="r") as member_stream:
                    hdr, v_maj, v_min, total_hdr_len = _parse_npy_header_bytes(member_stream)
                    m_shape = hdr["shape"]
                    m_descr = hdr["descr"]
                    m_fortran = bool(hdr["fortran_order"])
                    m_itemsize, m_has_obj = _get_dtype_info(m_descr)

                    # Validate member payload extent
                    _validate_payload_extent(member.file_size, total_hdr_len, m_shape, m_itemsize)
            except Exception as exc:
                context.result.add_gap(
                    f"Member {fname!r} is not a valid NPY array: {_bounded_text(str(exc), 120)}",
                    ref=member_ref,
                )
                continue

            m_ndim = len(m_shape)
            member_desc = [
                f"Array {fname}",
                f"shape={list(m_shape)}",
                f"ndim={m_ndim}",
                f"dtype={m_descr if isinstance(m_descr, str) else 'structured'}",
                f"order={'F' if m_fortran else 'C'}",
            ]

            member_unit = Unit(
                source=source,
                modality=Modality.SCHEMA,
                content=", ".join(member_desc),
                origin=Origin(source, member_ref),
                role=Role.UNKNOWN,
                structure=("archive", context.path.name, fname),
                salience=0.8,
                meta={
                    "name": fname,
                    "shape": list(m_shape),
                    "ndim": m_ndim,
                    "dtype": str(m_descr) if isinstance(m_descr, str) else "structured",
                    "fortran_order": m_fortran,
                    "uncompressed_bytes": member.file_size,
                },
            )
            context.result.units.append(member_unit)
            context.result.relations.append(
                Relation(
                    src=archive_unit.id,
                    dst=member_unit.id,
                    kind=RelationKind.DESCRIBES,
                    evidence="archive-member",
                )
            )

            # Safety gap for object dtype inside member
            if m_has_obj or _is_object_dtype_recursive(m_descr):
                context.result.add_gap(
                    f"Member {fname!r} has object dtype ('|O') with pickled objects; inspection refused",
                    ref=member_ref,
                )

    return _finish(context)


def extract_scientific_array(path: str | Path) -> Extraction:
    """Unified entry point for NPY and NPZ scientific arrays."""
    path = Path(path)
    kind = detect_scientific_array_kind(path)
    if kind == "npz":
        return extract_npz(path)
    return extract_npy(path)
