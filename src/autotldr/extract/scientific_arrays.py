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
- Never loads arrays or calls allow_pickle; object dtypes emit an honest named safety gap.
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
import os
import stat
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
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
_MAX_SUBARRAY_ELEMENTS = 10000
_MAX_MEMBER_NAME_CHARS = 255
_MAX_TEXT_CHARS = 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_STREAM_DRAIN_CHUNK = 64 * 1024
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
    file_obj: BinaryIO
    identity: tuple[int, int, int, int]
    initial_digest: str
    result: Extraction


class _CountingStream:
    """Wrapper around binary stream that tracks total bytes read without storing them."""

    __slots__ = ("_stream", "bytes_read")

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.bytes_read = 0

    def read(self, n: int = -1) -> bytes:
        chunk = self._stream.read(n)
        self.bytes_read += len(chunk)
        return chunk


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _safe_error_message(phase: str, exc: BaseException) -> str:
    """Produce a bounded, stable description without leaking paths, header values, or binary reprs."""
    cls_name = exc.__class__.__name__
    return f"{phase} failed ({cls_name})"


# ---------------------------------------------------------------------------
# Structural Descriptor Preflight & Checked Dtype Construction
# ---------------------------------------------------------------------------
def _validate_descr_preflight(descr: Any, depth: int = 0) -> int:
    """Stdlib structural preflight for dtype descriptors.

    Recursively checks nesting depth, field counts, subarray shapes, and data types.
    Accepts string typestrings and structured field lists with non-empty (name, dtype)
    or titled ((title, name), dtype) entries.
    Returns the total number of fields encountered.
    Raises ValueError if the descriptor violates structural safety bounds.
    """
    if depth > _MAX_STRUCTURED_DEPTH:
        raise ValueError(f"structured dtype nesting depth exceeds maximum {_MAX_STRUCTURED_DEPTH}")

    if isinstance(descr, str):
        if not descr or len(descr) > 128:
            raise ValueError("dtype typestring must be non-empty and bounded")
        return 0

    if isinstance(descr, (list, tuple)):
        field_count = len(descr)
        if field_count > _MAX_STRUCTURED_FIELDS:
            raise ValueError(f"structured field count {field_count} exceeds maximum {_MAX_STRUCTURED_FIELDS}")

        total_fields = field_count
        for item in descr:
            if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
                raise ValueError("structured field entry must have exactly 2 or 3 elements")

            ident = item[0]
            if isinstance(ident, str):
                if not ident:
                    raise ValueError("structured field name must not be empty")
                if len(ident) > 128:
                    raise ValueError("structured field name exceeds maximum length")
            elif (
                isinstance(ident, (tuple, list))
                and len(ident) == 2
                and isinstance(ident[0], str)
                and isinstance(ident[1], str)
            ):
                if not ident[1]:
                    raise ValueError("structured field name must not be empty")
                if len(ident[0]) > 128 or len(ident[1]) > 128:
                    raise ValueError("structured field title/name exceeds maximum length")
            else:
                raise ValueError("structured field identity must be a string or (title, name) pair of strings")

            sub_dtype = item[1]
            total_fields += _validate_descr_preflight(sub_dtype, depth + 1)
            if total_fields > _MAX_STRUCTURED_FIELDS:
                raise ValueError(f"total structured field count exceeds maximum {_MAX_STRUCTURED_FIELDS}")

            if len(item) == 3:
                sub_shape = item[2]
                if type(sub_shape) is int:
                    if sub_shape < 0 or sub_shape > _MAX_SUBARRAY_ELEMENTS:
                        raise ValueError("structured field subarray dimension out of bounds")
                elif isinstance(sub_shape, (list, tuple)):
                    if len(sub_shape) > _MAX_DIMENSIONS:
                        raise ValueError("structured field subarray dimensionality exceeds maximum")
                    prod = 1
                    for d in sub_shape:
                        if type(d) is not int or d < 0:
                            raise ValueError("structured field subarray dimension must be non-negative integer")
                        prod *= d
                    if prod > _MAX_SUBARRAY_ELEMENTS:
                        raise ValueError("structured field subarray total element count out of bounds")
                else:
                    raise ValueError("structured field subarray shape must be int or tuple")
        return total_fields

    raise ValueError(f"invalid dtype descriptor type: {type(descr).__name__}")


def _is_object_dtype_recursive(descr: Any, depth: int = 0) -> bool:
    """Check if descr structurally represents or contains Python object dtype ('|O', 'O', 'object')."""
    if depth > _MAX_STRUCTURED_DEPTH:
        return False
    if isinstance(descr, str):
        cleaned = descr.strip()
        return cleaned in ("|O", "O", ">O", "<O", "=O", "object", "|O8", "<O8", ">O8") or cleaned.endswith("O")
    if isinstance(descr, (list, tuple)):
        for item in descr:
            if isinstance(item, (list, tuple)) and len(item) in (2, 3):
                if _is_object_dtype_recursive(item[1], depth + 1):
                    return True
    return False


def _get_dtype_info(descr: Any) -> tuple[int, bool]:
    """Return (itemsize_in_bytes, has_object_flag) using lazy numpy dtype constructor.

    Preflights structure in stdlib first, then validates the exact parsed descriptor with numpy.dtype.
    Strictly catches TypeError/ValueError from numpy.dtype.
    Does not catch MemoryError, AssertionError, or programmer defects.
    """
    _validate_descr_preflight(descr)

    try:
        import numpy as np
        dt = np.dtype(descr)
        return int(dt.itemsize), bool(dt.hasobject)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid dtype descriptor") from exc


# ---------------------------------------------------------------------------
# Exact NPY Grammar Parser (v1.0, v2.0, v3.0)
# ---------------------------------------------------------------------------
def _parse_npy_header_bytes(stream: BinaryIO, max_read: int = _MAX_HEADER_BYTES) -> tuple[dict[str, Any], int, int, int]:
    """Parse NPY header from open binary stream without loading payload.

    Enforces exact NPY specification grammar:
    - Accepted versions: strictly (1, 0), (2, 0), (3, 0)
    - Prefix + header length divisible by 64
    - Header must end in '\\n' with only ASCII space (' ', 0x20) padding before '\\n'
    - Dictionary literal with exactly keys: 'descr', 'fortran_order', 'shape'
    - 'shape' must be a tuple (not list) of non-bool non-negative ints
    - 'fortran_order' must be a bool

    Returns (header_dict, major_version, minor_version, total_header_bytes).
    """
    magic = stream.read(6)
    if magic != _NPY_MAGIC:
        raise ValueError("invalid NPY magic signature")

    version_bytes = stream.read(2)
    if len(version_bytes) < 2:
        raise ValueError("truncated NPY version bytes")
    v_maj, v_min = struct.unpack("BB", version_bytes)

    if (v_maj, v_min) not in ((1, 0), (2, 0), (3, 0)):
        raise ValueError(f"unsupported NPY version {v_maj}.{v_min}")

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

    total_hdr_len = prefix_len + hlen
    if total_hdr_len % 64 != 0:
        raise ValueError(f"misaligned NPY header length: total {total_hdr_len} bytes is not divisible by 64")

    if hlen > max_read:
        raise ValueError(f"NPY header length {hlen} exceeds limit {max_read}")

    header_bytes = stream.read(hlen)
    if len(header_bytes) < hlen:
        raise ValueError("truncated NPY header data")

    if not header_bytes.endswith(b"\n"):
        raise ValueError("NPY header must terminate with newline")

    encoding = "utf-8" if v_maj == 3 else "latin1"
    try:
        header_str = header_bytes.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid NPY header encoding ({encoding})") from exc

    # Validate padding: everything between dict closing brace and trailing newline must be ASCII spaces (0x20)
    body = header_str[:-1]  # drop trailing '\n'
    last_brace = body.rfind("}")
    if last_brace == -1:
        raise ValueError("malformed NPY header dict syntax: missing closing brace")

    padding = body[last_brace + 1:]
    if any(c != " " for c in padding):
        raise ValueError("NPY header padding must contain only ASCII spaces")

    try:
        parsed = ast.literal_eval(body)
    except (SyntaxError, ValueError) as exc:
        raise ValueError("malformed NPY header dict syntax") from exc

    if not isinstance(parsed, dict):
        raise ValueError("NPY header is not a dictionary literal")

    # Enforce exactly the required keys
    expected_keys = {"descr", "fortran_order", "shape"}
    if set(parsed.keys()) != expected_keys:
        raise ValueError(f"NPY header keys mismatch: expected exactly {expected_keys}, got {set(parsed.keys())}")

    # Strict shape validation: must be tuple (not list)
    shape = parsed["shape"]
    if not isinstance(shape, tuple):
        raise ValueError(f"NPY shape must be a tuple, got {type(shape).__name__}")
    if len(shape) > _MAX_DIMENSIONS:
        raise ValueError(f"NPY dimensionality {len(shape)} exceeds maximum {_MAX_DIMENSIONS}")
    for dim in shape:
        if type(dim) is not int or dim < 0:
            raise ValueError(f"NPY shape dimensions must be non-negative integers, got {dim!r}")

    fortran_order = parsed["fortran_order"]
    if type(fortran_order) is not bool:
        raise ValueError(f"NPY fortran_order must be a boolean, got {type(fortran_order).__name__}")

    return parsed, v_maj, v_min, total_hdr_len


def _validate_payload_extent(
    total_available_bytes: int,
    total_header_bytes: int,
    shape: tuple[int, ...],
    itemsize: int,
    has_object: bool,
) -> None:
    """Verify exact fixed-width payload extent for non-object arrays or presence for object arrays."""
    actual_payload = total_available_bytes - total_header_bytes
    if actual_payload < 0:
        raise ValueError("truncated array: file shorter than header")

    if has_object:
        # Object arrays require at least 1 payload byte (pickle header/stream), even for 0-element arrays
        if actual_payload < 1:
            raise ValueError("truncated object array: payload missing")
        return

    total_elements = 1
    for dim in shape:
        total_elements *= dim

    expected_payload = total_elements * itemsize
    if actual_payload < expected_payload:
        raise ValueError(
            f"truncated array payload: expected {expected_payload} bytes, found {actual_payload} bytes"
        )
    if actual_payload > expected_payload:
        raise ValueError(
            f"trailing array payload: expected {expected_payload} bytes, found {actual_payload} bytes"
        )


# ---------------------------------------------------------------------------
# NPZ Member Name & ZIP Security Validation
# ---------------------------------------------------------------------------
def _validate_npz_member_name(name: str) -> None:
    """Validate member filename against path traversal, NUL bytes, backslashes, and malformed paths."""
    if not name:
        raise ValueError("empty member name")
    if len(name) > _MAX_MEMBER_NAME_CHARS:
        raise ValueError(f"member name length {len(name)} exceeds limit {_MAX_MEMBER_NAME_CHARS}")
    if chr(0) in name:
        raise ValueError(f"NUL byte in member name {name!r}")
    if "\\" in name:
        raise ValueError(f"backslash in member name {name!r}")
    if name.startswith("/"):
        raise ValueError(f"absolute path in member name {name!r}")
    if name.endswith("/"):
        raise ValueError(f"directory member {name!r} is unsupported in NPZ")
    if "//" in name:
        raise ValueError(f"empty path segment in member name {name!r}")

    first_segment = name.split("/")[0]
    if ":" in first_segment:
        raise ValueError(f"drive-like prefix in member name {name!r}")

    segments = name.split("/")
    if any(seg in (".", "..") for seg in segments):
        raise ValueError(f"path traversal or dot component in member name {name!r}")

    if not name.endswith(".npy"):
        raise ValueError(f"non-NPY member {name!r} in NPZ archive")


def _validate_npz_zipinfo(member: zipfile.ZipInfo, seen_names: set[str]) -> None:
    """Validate ZipInfo attributes for security, encryption, symlinks, and duplicate names."""
    # Check orig_filename and enforce strict agreement with filename
    if hasattr(member, "orig_filename") and member.orig_filename:
        orig = member.orig_filename
        if isinstance(orig, bytes):
            if b"\x00" in orig:
                raise ValueError("NUL byte in orig_filename")
            if b"\\" in orig or b"//" in orig:
                raise ValueError("raw-unsafe bytes in orig_filename")
            try:
                orig_str = orig.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("orig_filename decoding failed") from exc
        else:
            orig_str = str(orig)
            if "\x00" in orig_str:
                raise ValueError("NUL byte in orig_filename")

        _validate_npz_member_name(orig_str)
        if orig_str != member.filename:
            raise ValueError(f"raw member name {orig_str!r} disagrees with normalized filename {member.filename!r}")

    # Check normalized filename
    _validate_npz_member_name(member.filename)

    # Check Unix symlinks / special files
    if member.external_attr > 0:
        mode = (member.external_attr >> 16) & 0o170000
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlink member {member.filename!r} is forbidden")
        if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISDIR(mode):
            raise ValueError(f"special file or directory member {member.filename!r} is forbidden")

    # Encryption flag
    if member.flag_bits & 0x1:
        raise ValueError(f"encrypted member {member.filename!r} is unsupported")

    # Duplicate names
    if member.filename in seen_names:
        raise ValueError(f"duplicate archive member {member.filename!r}")
    seen_names.add(member.filename)

    # Supported compression methods: STORED (0) and DEFLATED (8)
    if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ValueError(f"unsupported compression method {member.compress_type} on {member.filename!r}")

    # Zero-compressed non-empty bomb
    if member.file_size > 0 and member.compress_size == 0:
        raise ValueError(f"zero-compressed non-empty member bomb: {member.filename!r}")

    # Decompression ratio check
    if member.compress_size > 0:
        ratio = member.file_size / member.compress_size
        if ratio > _MAX_DECOMPRESSION_RATIO and member.file_size > 64 * 1024:
            raise ValueError(f"suspicious decompression ratio {ratio:.1f} on {member.filename!r}")


# ---------------------------------------------------------------------------
# Magic and Structural Detection (Content-based only)
# ---------------------------------------------------------------------------
def detect_scientific_array_kind(path: str | Path) -> str:
    """Detect whether a file is NumPy NPY or NPZ archive based strictly on content.

    Fails closed on unknown, generic ZIP, empty ZIP, or spoofed files.
    """
    path = Path(path)
    try:
        info = path.stat()
    except OSError as exc:
        raise InvalidScientificArrayData(path, "array", _safe_error_message("file stat", exc)) from exc

    if not stat.S_ISREG(info.st_mode):
        raise InvalidScientificArrayData(path, "array", "input is not a regular file")
    if info.st_size == 0:
        raise InvalidScientificArrayData(path, "array", "file is empty")
    if info.st_size > _MAX_FILE_BYTES:
        raise InvalidScientificArrayData(
            path, "array", f"file size {info.st_size} exceeds limit {_MAX_FILE_BYTES}"
        )

    try:
        with path.open("rb") as f:
            header = f.read(16)

            # 1. Strong NPY magic: \x93NUMPY
            if header.startswith(_NPY_MAGIC):
                return "npy"

            # 2. NPZ is a non-empty ZIP archive where ALL members are valid .npy members
            if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06"):
                f.seek(0)
                try:
                    with zipfile.ZipFile(f, mode="r") as zf:
                        infolist = zf.infolist()
                        if not infolist:
                            raise ValueError("empty ZIP archive is not NPZ")
                        if len(infolist) > _MAX_NPZ_MEMBERS:
                            raise ValueError("member count exceeds bounds")
                        seen: set[str] = set()
                        for m in infolist:
                            _validate_npz_zipinfo(m, seen)
                        return "npz"
                except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, struct.error, OSError):
                    pass
    except OSError as exc:
        raise InvalidScientificArrayData(path, "array", _safe_error_message("file open", exc)) from exc

    raise InvalidScientificArrayData(
        path, "array", "unrecognized or unsupported scientific array signature (failed closed)"
    )


# ---------------------------------------------------------------------------
# Extractor Lifecycle & Same-Byte Binding
# ---------------------------------------------------------------------------
def _begin_and_bind(path: Path, kind: str) -> _ReadContext:
    """Open the file descriptor, verify regular file bounds, fingerprint it, and bind the descriptor."""
    path = Path(path)
    try:
        file_obj = path.open("rb")
    except OSError as exc:
        raise InvalidScientificArrayData(path, kind, _safe_error_message("file open", exc)) from exc

    try:
        st = os.fstat(file_obj.fileno())
        if not stat.S_ISREG(st.st_mode):
            file_obj.close()
            raise InvalidScientificArrayData(path, kind, "input is not a regular file")
        if st.st_size == 0:
            file_obj.close()
            raise InvalidScientificArrayData(path, kind, "file is empty")
        if st.st_size > _MAX_FILE_BYTES:
            file_obj.close()
            raise InvalidScientificArrayData(
                path,
                kind,
                f"file is {st.st_size} bytes; limit is {_MAX_FILE_BYTES} bytes",
            )

        identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

        # Compute SHA-256 fingerprint through the open file descriptor
        file_obj.seek(0)
        digest = hashlib.sha256()
        byte_count = 0
        while chunk := file_obj.read(_HASH_CHUNK_BYTES):
            byte_count += len(chunk)
            digest.update(chunk)

        if byte_count != st.st_size:
            file_obj.close()
            raise InvalidScientificArrayData(path, kind, "source changed while it was fingerprinted")

        file_obj.seek(0)
    except Exception:
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
    return _ReadContext(
        path=path,
        file_obj=file_obj,
        identity=identity,
        initial_digest=digest.hexdigest(),
        result=result,
    )


def _verify_and_finish(context: _ReadContext) -> Extraction:
    """Post-rehash the open file descriptor, verify path identity, sort results, and close descriptor."""
    try:
        # 1. Post-rehash the descriptor and verify byte count matches bound identity
        context.file_obj.seek(0)
        post_digest = hashlib.sha256()
        rehash_count = 0
        while chunk := context.file_obj.read(_HASH_CHUNK_BYTES):
            rehash_count += len(chunk)
            post_digest.update(chunk)

        if rehash_count != context.identity[2]:
            raise InvalidScientificArrayData(
                context.path,
                context.result.kind,
                "source changed while it was being extracted",
            )

        if post_digest.hexdigest() != context.initial_digest:
            raise InvalidScientificArrayData(
                context.path,
                context.result.kind,
                "source changed while it was being extracted",
            )

        # 2. Check fstat identity on the descriptor
        st = os.fstat(context.file_obj.fileno())
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != context.identity:
            raise InvalidScientificArrayData(
                context.path,
                context.result.kind,
                "source changed while it was being extracted",
            )

        # 3. Check path identity on filesystem
        try:
            path_st = context.path.stat()
            if (path_st.st_dev, path_st.st_ino) != (st.st_dev, st.st_ino):
                raise InvalidScientificArrayData(
                    context.path,
                    context.result.kind,
                    "source pathname replaced while it was being extracted",
                )
        except OSError as exc:
            raise InvalidScientificArrayData(
                context.path,
                context.result.kind,
                _safe_error_message("source path stat", exc),
            ) from exc

    finally:
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
        raise AssertionError("Scientific array extractor emitted a dangling relation")

    result.meta["counts"] = {
        "units": len(result.units),
        "relations": len(result.relations),
        "gaps": len(result.gaps),
    }
    return result


# ---------------------------------------------------------------------------
# NPY Extractor
# ---------------------------------------------------------------------------
def extract_npy(path: str | Path) -> Extraction:
    """Extract array metadata from a NumPy NPY file."""
    path = Path(path)
    context = _begin_and_bind(path, "npy")
    file_size = context.identity[2]

    try:
        try:
            header, v_maj, v_min, total_hdr_len = _parse_npy_header_bytes(context.file_obj)
        except (ValueError, UnicodeDecodeError, struct.error, SyntaxError, OSError) as exc:
            detail = _safe_error_message("npy header parse", exc)
            raise InvalidScientificArrayData(context.path, "npy", detail) from exc

        shape = header["shape"]
        descr = header["descr"]
        fortran_order = bool(header["fortran_order"])
        ndim = len(shape)

        try:
            itemsize, has_object = _get_dtype_info(descr)
        except ValueError as exc:
            detail = _safe_error_message("dtype validation", exc)
            raise InvalidScientificArrayData(context.path, "npy", detail) from exc

        try:
            _validate_payload_extent(file_size, total_hdr_len, shape, itemsize, has_object)
        except ValueError as exc:
            detail = _safe_error_message("payload validation", exc)
            raise InvalidScientificArrayData(context.path, "npy", detail) from exc

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

        # 2. Honest object dtype safety/integrity gap
        if has_object or _is_object_dtype_recursive(descr):
            context.result.add_gap(
                "Object dtype ('|O') contains pickled Python objects; "
                "payload inspection was refused and inner pickle integrity/extent was not validated",
                ref="array",
            )

        # 3. Structured fields (if structured dtype)
        if isinstance(descr, list):
            for idx, field_tuple in enumerate(descr):
                if isinstance(field_tuple, (list, tuple)) and len(field_tuple) in (2, 3):
                    ident = field_tuple[0]
                    if isinstance(ident, (tuple, list)) and len(ident) == 2:
                        title: str | None = str(ident[0])
                        f_name = str(ident[1])
                    else:
                        title = None
                        f_name = str(ident)

                    f_type = str(field_tuple[1])
                    if len(field_tuple) == 3:
                        raw_shape = field_tuple[2]
                        if isinstance(raw_shape, (list, tuple)):
                            f_shape = list(raw_shape)
                        elif type(raw_shape) is int:
                            f_shape = [raw_shape]
                        else:
                            f_shape = []
                    else:
                        f_shape = []

                    field_meta: dict[str, Any] = {
                        "index": idx,
                        "name": f_name,
                        "dtype": f_type,
                        "shape": f_shape,
                    }
                    if title is not None:
                        field_meta["title"] = title

                    field_unit = Unit(
                        source=source,
                        modality=Modality.SCHEMA,
                        content=f"Field {idx} ({f_name}): dtype={f_type}" + (f", shape={f_shape}" if f_shape else ""),
                        origin=Origin(source, f"field:{idx}:{quote(f_name, safe='-._~')}"),
                        role=Role.UNKNOWN,
                        structure=("array", context.path.name, f"{idx}:{f_name}"),
                        salience=0.7,
                        meta=field_meta,
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

        return _verify_and_finish(context)
    except Exception:
        context.file_obj.close()
        raise


# ---------------------------------------------------------------------------
# NPZ Extractor with Full Envelope Drain and CRC Validation
# ---------------------------------------------------------------------------
def extract_npz(path: str | Path) -> Extraction:
    """Extract member inventory and array metadata from a NumPy NPZ archive."""
    path = Path(path)
    context = _begin_and_bind(path, "npz")
    source = str(context.path)

    try:
        try:
            zf = zipfile.ZipFile(context.file_obj, mode="r")
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, struct.error, OSError) as exc:
            detail = _safe_error_message("npz zip open", exc)
            raise InvalidScientificArrayData(context.path, "npz", detail) from exc

        with zf:
            try:
                infolist = zf.infolist()
            except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, struct.error, OSError) as exc:
                detail = _safe_error_message("npz central directory read", exc)
                raise InvalidScientificArrayData(context.path, "npz", detail) from exc

            total_members = len(infolist)
            if total_members == 0:
                raise InvalidScientificArrayData(context.path, "npz", "NPZ archive is empty")

            if total_members > _MAX_NPZ_MEMBERS:
                raise InvalidScientificArrayData(
                    context.path,
                    "npz",
                    f"member count {total_members} exceeds limit {_MAX_NPZ_MEMBERS}",
                )

            # Upfront envelope validation on all member metadata
            seen_names: set[str] = set()
            total_uncompressed = 0
            for member in infolist:
                try:
                    _validate_npz_zipinfo(member, seen_names)
                except ValueError as exc:
                    detail = _safe_error_message("npz member security check", exc)
                    raise InvalidScientificArrayData(context.path, "npz", detail) from exc

                total_uncompressed += member.file_size
                if total_uncompressed > _MAX_NPZ_TOTAL_UNCOMPRESSED:
                    raise InvalidScientificArrayData(
                        context.path,
                        "npz",
                        f"uncompressed size {total_uncompressed} exceeds limit {_MAX_NPZ_TOTAL_UNCOMPRESSED}",
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

            valid_member_units = 0

            for member in infolist:
                fname = member.filename
                member_ref = f"member:{quote(fname, safe='-._~')}"

                npy_error: ValueError | None = None
                parsed_hdr_data: tuple[dict[str, Any], int, int, tuple[int, ...], Any, bool, int, bool] | None = None

                # Open ZipExtFile, attempt NPY header parse, then drain stream to EOF to verify CRC & envelope
                try:
                    with zf.open(member, mode="r") as member_stream:
                        counting_stream = _CountingStream(member_stream)
                        try:
                            hdr, v_maj, v_min, total_hdr_len = _parse_npy_header_bytes(counting_stream)
                            m_shape = hdr["shape"]
                            m_descr = hdr["descr"]
                            m_fortran = bool(hdr["fortran_order"])
                            m_itemsize, m_has_obj = _get_dtype_info(m_descr)

                            _validate_payload_extent(
                                member.file_size, total_hdr_len, m_shape, m_itemsize, m_has_obj
                            )
                            parsed_hdr_data = (
                                hdr, v_maj, v_min, m_shape, m_descr, m_fortran, m_itemsize, m_has_obj
                            )
                        except ValueError as exc:
                            npy_error = exc

                        # Drain all remaining member bytes to EOF to force ZipExtFile to verify CRC
                        while chunk := counting_stream.read(_STREAM_DRAIN_CHUNK):
                            pass

                        if counting_stream.bytes_read != member.file_size:
                            raise zipfile.BadZipFile(
                                f"decompressed size mismatch: expected {member.file_size}, got {counting_stream.bytes_read}"
                            )

                except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError) as exc:
                    # Envelope / decompression / CRC failure on any member fails the entire archive
                    detail = _safe_error_message("npz member read", exc)
                    raise InvalidScientificArrayData(context.path, "npz", detail) from exc

                # If envelope is intact but NPY structure/header/dtype/extent was invalid, emit addressable member gap
                if npy_error is not None or parsed_hdr_data is None:
                    gap_detail = _safe_error_message("member npy format", npy_error or ValueError("invalid member"))
                    context.result.add_gap(
                        f"Member {fname!r} is not a valid NPY array: {gap_detail}",
                        ref=member_ref,
                    )
                    continue

                hdr, v_maj, v_min, m_shape, m_descr, m_fortran, m_itemsize, m_has_obj = parsed_hdr_data
                valid_member_units += 1
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

                # Honest safety gap for object dtype inside member
                if m_has_obj or _is_object_dtype_recursive(m_descr):
                    context.result.add_gap(
                        f"Member {fname!r} has object dtype ('|O') with pickled objects; "
                        f"payload inspection was refused and inner pickle integrity/extent was not validated",
                        ref=member_ref,
                    )

            if valid_member_units == 0:
                context.result.add_gap(
                    f"NPZ archive contains no valid NPY array members ({total_members} invalid members)",
                    ref="archive",
                )

        return _verify_and_finish(context)
    except Exception:
        context.file_obj.close()
        raise


def extract_scientific_array(path: str | Path) -> Extraction:
    """Unified entry point for NPY and NPZ scientific arrays."""
    path = Path(path)
    kind = detect_scientific_array_kind(path)
    if kind == "npz":
        return extract_npz(path)
    return extract_npy(path)
