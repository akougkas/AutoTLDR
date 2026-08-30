"""Native bounded metadata extraction for FITS astronomy data files.

Primary authoritative specifications:
- NASA / IAU FITS Standard Version 4.0 (2018):
  https://fits.gsfc.nasa.gov/standard40/fits_standard40aa.pdf
- NASA FITS Standard Overview and Definitions:
  https://fits.gsfc.nasa.gov/fits_standard.html

Invariants enforced:
- Every emitted Unit, Relation, and Gap is strictly addressable with exact origins.
- Emits Role.UNKNOWN exclusively.
- Never decodes or emits raw pixel payloads, image buffers, or table data rows.
- Pure standard library implementation using strict 7-bit ASCII 2880-byte block parsing.
- Validates required card order, valid BITPIX, bounded NAXIS/dimensions, and checked payload arithmetic.
- Proves payload extent before seeking; truncated payloads decline with typed errors.
- Disambiguates duplicate EXTNAME and duplicate column names with deterministic index-qualified refs.
- Enforces strict bounds on file sizes, HDU counts, cards per HDU, and table columns.
- Scrubs parser exceptions to prevent private paths or binary buffer leaks.
"""

from __future__ import annotations

import hashlib
import json
import os
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
_MAX_HDUS = 512
_MAX_HEADER_BLOCKS_PER_HDU = 128
_MAX_CARDS_PER_HDU = 4096
_MAX_COLUMNS_PER_TABLE = 1024
_MAX_DIMENSIONS = 32
_MAX_TEXT_CHARS = 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_FITS_BLOCK_SIZE = 2880
_FITS_CARD_SIZE = 80
_CARDS_PER_BLOCK = 36
_VALID_BITPIX = {8, 16, 32, 64, -32, -64}


class InvalidFitsData(ValueError):
    """A recognized FITS file is corrupt, truncated, unsafe, empty, or exceeds bounds."""

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
    return text[: max(0, limit - 1)] + "…"


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
def detect_astronomy_kind(path: str | Path) -> str:
    """Detect whether a file is a FITS astronomy data file.

    Fails closed on unknown or spoofed bytes.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InvalidFitsData(path, "astronomy", f"stat error: {_bounded_text(str(exc), 80)}") from exc

    if size == 0:
        raise InvalidFitsData(path, "astronomy", "file is empty")
    if size < _FITS_BLOCK_SIZE:
        raise InvalidFitsData(
            path, "astronomy", f"file size {size} is smaller than standard 2880-byte block"
        )
    if size > _MAX_FILE_BYTES:
        raise InvalidFitsData(
            path, "astronomy", f"file size {size} exceeds limit {_MAX_FILE_BYTES}"
        )

    with path.open("rb") as f:
        header = f.read(30)

    # Standard FITS primary header starts with 'SIMPLE  = '
    if header.startswith(b"SIMPLE  ="):
        return "fits"

    raise InvalidFitsData(
        path, "astronomy", "unrecognized or unsupported astronomy format signature (failed closed)"
    )


# ---------------------------------------------------------------------------
# Extractor Lifecycle
# ---------------------------------------------------------------------------
def _begin(path: Path, kind: str) -> _ReadContext:
    path = Path(path)
    try:
        info = path.stat()
    except OSError as exc:
        raise InvalidFitsData(path, kind, f"stat error: {_bounded_text(str(exc), 80)}") from exc

    if not stat.S_ISREG(info.st_mode):
        raise InvalidFitsData(path, kind, "input is not a regular file")
    if info.st_size == 0:
        raise InvalidFitsData(path, kind, "file is empty")
    if info.st_size % _FITS_BLOCK_SIZE != 0:
        raise InvalidFitsData(
            path,
            kind,
            f"file size {info.st_size} is not a multiple of 2880 bytes",
        )
    if info.st_size > _MAX_FILE_BYTES:
        raise InvalidFitsData(
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
        raise InvalidFitsData(path, kind, f"read error: {_bounded_text(str(exc), 80)}") from exc

    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if byte_count != info.st_size or _identity(path) != identity:
        raise InvalidFitsData(
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
                "name": "astronomy-fits-v1",
                "bounds": {
                    "file_bytes": _MAX_FILE_BYTES,
                    "hdus": _MAX_HDUS,
                    "header_blocks_per_hdu": _MAX_HEADER_BLOCKS_PER_HDU,
                    "cards_per_hdu": _MAX_CARDS_PER_HDU,
                    "columns_per_table": _MAX_COLUMNS_PER_TABLE,
                },
            },
        }
    )
    return _ReadContext(path=path, identity=identity, result=result)


def _finish(context: _ReadContext) -> Extraction:
    if _identity(context.path) != context.identity:
        raise InvalidFitsData(
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
        raise AssertionError("FITS extractor emitted a dangling relation")

    result.meta["counts"] = {
        "units": len(result.units),
        "relations": len(result.relations),
        "gaps": len(result.gaps),
    }
    return result


# ---------------------------------------------------------------------------
# Strict 7-Bit ASCII Card Parsing Logic
# ---------------------------------------------------------------------------
def _parse_card_strict(card_bytes: bytes) -> tuple[str, Any, str]:
    """Parse an 80-byte strict 7-bit ASCII FITS card into (keyword, value, comment)."""
    if len(card_bytes) != 80:
        raise ValueError(f"card must be exactly 80 bytes, got {len(card_bytes)}")

    try:
        card_str = card_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"non-ASCII byte in card at offset {exc.start}") from exc

    kw = card_str[:8].strip().upper()
    if not kw:
        return "", None, ""

    if len(card_str) <= 8 or card_str[8:10] != "= ":
        comment = card_str[8:].strip()
        return kw, None, comment

    rest = card_str[10:]
    stripped_rest = rest.lstrip()

    # Check for single-quoted string value (which may contain slashes)
    if stripped_rest.startswith("'"):
        start_idx = rest.find("'")
        accum = []
        i = start_idx + 1
        closed = False
        while i < len(rest):
            if rest[i] == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    accum.append("'")
                    i += 2
                    continue
                else:
                    closed = True
                    i += 1
                    break
            else:
                accum.append(rest[i])
                i += 1
        if not closed:
            raise ValueError(f"unclosed single-quoted string for keyword {kw}")
        val = "".join(accum).rstrip()
        remainder = rest[i:]
        _, _, comment = remainder.partition("/")
        return kw, val, comment.strip()

    # Non-string value: partition at '/'
    val_part, _, comment_part = rest.partition("/")
    val_str = val_part.strip()
    comment = comment_part.strip()

    if not val_str:
        return kw, None, comment
    if val_str == "T":
        return kw, True, comment
    if val_str == "F":
        return kw, False, comment
    try:
        return kw, int(val_str), comment
    except ValueError:
        pass
    try:
        norm = val_str.replace("D", "E").replace("d", "e")
        return kw, float(norm), comment
    except ValueError:
        pass
    return kw, val_str, comment


# ---------------------------------------------------------------------------
# Ordered HDU Header Reader with Mandatory Validation
# ---------------------------------------------------------------------------
def _read_hdu_header(
    stream, hdu_index: int
) -> tuple[dict[str, Any], list[tuple[str, Any, str]], int]:
    """Read all 2880-byte header blocks for one HDU until 'END' card.

    Returns (cards_dict, ordered_cards_list, total_header_bytes).
    """
    cards: dict[str, Any] = {}
    ordered_cards: list[tuple[str, Any, str]] = []
    block_count = 0
    found_end = False

    while block_count < _MAX_HEADER_BLOCKS_PER_HDU:
        block = stream.read(_FITS_BLOCK_SIZE)
        if len(block) < _FITS_BLOCK_SIZE:
            if block_count == 0 and len(block) == 0:
                # Clean EOF between HDUs
                return {}, [], 0
            raise ValueError(f"truncated FITS header block in HDU {hdu_index}")

        block_count += 1
        for card_idx in range(_CARDS_PER_BLOCK):
            card_bytes = block[card_idx * _FITS_CARD_SIZE : (card_idx + 1) * _FITS_CARD_SIZE]
            kw, val, comm = _parse_card_strict(card_bytes)
            if kw == "END":
                found_end = True
                break
            if kw and len(ordered_cards) < _MAX_CARDS_PER_HDU:
                ordered_cards.append((kw, val, comm))
                if kw not in cards:
                    cards[kw] = val

        if found_end:
            break

    if not found_end:
        raise ValueError(
            f"HDU {hdu_index} header exceeded {_MAX_HEADER_BLOCKS_PER_HDU} blocks without END card"
        )

    return cards, ordered_cards, block_count * _FITS_BLOCK_SIZE


# ---------------------------------------------------------------------------
# Main FITS Extractor
# ---------------------------------------------------------------------------
def extract_fits(path: str | Path) -> Extraction:
    """Extract HDU inventories, schemas, and observation metadata from FITS files."""
    path = Path(path)
    context = _begin(path, "fits")
    source = str(context.path)
    file_size = context.path.stat().st_size

    try:
        with context.path.open("rb") as f:
            hdu_index = 0
            primary_unit = None

            while hdu_index < _MAX_HDUS:
                start_offset = f.tell()
                cards, ordered_cards, header_bytes = _read_hdu_header(f, hdu_index)
                if not cards and header_bytes == 0:
                    break

                # 1. Validate mandatory card ordering and types
                if hdu_index == 0:
                    if not ordered_cards or ordered_cards[0][0] != "SIMPLE":
                        raise ValueError("Primary HDU must begin with 'SIMPLE' card at card index 0")
                    simple_val = cards.get("SIMPLE")
                    if not isinstance(simple_val, bool):
                        raise ValueError(f"Primary HDU 'SIMPLE' card must be boolean T/F, got {simple_val!r}")
                    if simple_val is not True:
                        context.result.add_gap(
                            "Primary HDU declares SIMPLE=F (non-standard FITS conformant file)",
                            ref="hdu:0",
                        )
                else:
                    if not ordered_cards or ordered_cards[0][0] != "XTENSION":
                        raise ValueError(f"Extension HDU {hdu_index} must begin with 'XTENSION' card at card index 0")
                    xtension_val = cards.get("XTENSION")
                    if not isinstance(xtension_val, str) or not xtension_val.strip():
                        raise ValueError(f"Extension HDU {hdu_index} 'XTENSION' card must be a non-empty string")

                # Validate BITPIX
                if "BITPIX" not in cards:
                    raise ValueError(f"HDU {hdu_index} missing mandatory 'BITPIX' card")
                bitpix_raw = cards["BITPIX"]
                if isinstance(bitpix_raw, bool) or not isinstance(bitpix_raw, int) or bitpix_raw not in _VALID_BITPIX:
                    raise ValueError(f"HDU {hdu_index} invalid BITPIX {bitpix_raw!r}; must be one of {sorted(_VALID_BITPIX)}")
                bitpix = bitpix_raw

                # Validate NAXIS
                if "NAXIS" not in cards:
                    raise ValueError(f"HDU {hdu_index} missing mandatory 'NAXIS' card")
                naxis_raw = cards["NAXIS"]
                if isinstance(naxis_raw, bool) or not isinstance(naxis_raw, int) or naxis_raw < 0 or naxis_raw > _MAX_DIMENSIONS:
                    raise ValueError(f"HDU {hdu_index} invalid NAXIS {naxis_raw!r}; must be integer 0..{_MAX_DIMENSIONS}")
                naxis = naxis_raw

                # Validate NAXISn dimensions
                dims: list[int] = []
                for n in range(1, naxis + 1):
                    key = f"NAXIS{n}"
                    if key not in cards:
                        raise ValueError(f"HDU {hdu_index} missing mandatory '{key}' card for NAXIS={naxis}")
                    dim_val = cards[key]
                    if isinstance(dim_val, bool) or not isinstance(dim_val, int) or dim_val < 0:
                        raise ValueError(f"HDU {hdu_index} invalid {key} {dim_val!r}; must be non-negative integer")
                    dims.append(dim_val)

                # Validate PCOUNT and GCOUNT
                pcount_raw = cards.get("PCOUNT", 0)
                if isinstance(pcount_raw, bool) or not isinstance(pcount_raw, int) or pcount_raw < 0:
                    raise ValueError(f"HDU {hdu_index} invalid PCOUNT {pcount_raw!r}; must be non-negative integer")
                pcount = pcount_raw

                gcount_raw = cards.get("GCOUNT", 1)
                if isinstance(gcount_raw, bool) or not isinstance(gcount_raw, int) or gcount_raw < 1:
                    raise ValueError(f"HDU {hdu_index} invalid GCOUNT {gcount_raw!r}; must be positive integer >= 1")
                gcount = gcount_raw

                # 2. Checked Payload Arithmetic & Extent Validation
                payload_bytes = 0
                if naxis > 0 and all(d > 0 for d in dims):
                    bytes_per_val = abs(bitpix) // 8
                    prod = 1
                    for d in dims:
                        prod *= d
                    data_bytes = bytes_per_val * prod
                    payload_bytes = gcount * (data_bytes + pcount)

                padded_data_bytes = 0
                if payload_bytes > 0:
                    rem = payload_bytes % _FITS_BLOCK_SIZE
                    padded_data_bytes = payload_bytes if rem == 0 else payload_bytes + (_FITS_BLOCK_SIZE - rem)

                current_offset = f.tell()
                if current_offset + padded_data_bytes > file_size:
                    raise ValueError(
                        f"HDU {hdu_index} payload truncated: requires {padded_data_bytes} bytes, file ends at offset {file_size}"
                    )

                if padded_data_bytes > 0:
                    f.seek(padded_data_bytes, os.SEEK_CUR)

                # Extract observation & instrument metadata
                telescop = cards.get("TELESCOP")
                instrume = cards.get("INSTRUME")
                target_obj = cards.get("OBJECT")
                date_obs = cards.get("DATE-OBS")
                exptime = cards.get("EXPTIME")

                # Extract WCS descriptors
                wcs_info: dict[str, Any] = {}
                for wcs_k in ("CTYPE1", "CTYPE2", "CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CDELT1", "CDELT2", "RADESYS", "EQUINOX"):
                    if wcs_k in cards:
                        wcs_info[wcs_k] = cards[wcs_k]

                # 3. Emit Primary HDU Unit
                if hdu_index == 0:
                    content_parts = [f"FITS Primary HDU: {naxis}D image (BITPIX={bitpix})"]
                    if dims:
                        content_parts.append(f"shape={dims}")
                    if telescop:
                        content_parts.append(f"telescope={telescop}")
                    if instrume:
                        content_parts.append(f"instrument={instrume}")
                    if target_obj:
                        content_parts.append(f"object={target_obj}")
                    if exptime is not None:
                        content_parts.append(f"exptime={exptime}s")

                    primary_content = ", ".join(content_parts)
                    primary_unit = Unit(
                        source=source,
                        modality=Modality.SCHEMA,
                        content=primary_content,
                        origin=Origin(source, "hdu:0"),
                        role=Role.UNKNOWN,
                        structure=("fits", context.path.name, "hdu:0"),
                        salience=0.9,
                        meta={
                            "hdu_index": 0,
                            "type": "PRIMARY",
                            "bitpix": bitpix,
                            "naxis": naxis,
                            "shape": dims,
                            "telescope": str(telescop) if telescop else None,
                            "instrument": str(instrume) if instrume else None,
                            "object": str(target_obj) if target_obj else None,
                            "date_obs": str(date_obs) if date_obs else None,
                            "exptime": exptime,
                            "wcs": wcs_info,
                            "cards_count": len(cards),
                        },
                    )
                    context.result.units.append(primary_unit)

                    if not telescop and not instrume:
                        context.result.add_gap(
                            "Primary HDU contains no TELESCOP or INSTRUME observation cards",
                            ref="hdu:0",
                        )

                # 4. Emit Extension HDU Unit (Deterministic origin: ref=f"hdu:{i}")
                else:
                    xtension = str(cards.get("XTENSION", "UNKNOWN")).strip()
                    extname = str(cards.get("EXTNAME", "")).strip()
                    extver = cards.get("EXTVER")
                    tfields = int(cards.get("TFIELDS", 0)) if "TFIELDS" in cards else 0

                    ext_parts = [f"FITS HDU {hdu_index} ({xtension})"]
                    if extname:
                        ext_parts.append(f"extname={extname}")
                    if tfields > 0:
                        ext_parts.append(f"{tfields} columns")
                    elif dims:
                        ext_parts.append(f"shape={dims}")

                    ext_content = ", ".join(ext_parts)
                    ext_origin_ref = f"hdu:{hdu_index}"

                    ext_unit = Unit(
                        source=source,
                        modality=Modality.TABLE if "TABLE" in xtension else Modality.SCHEMA,
                        content=ext_content,
                        origin=Origin(source, ext_origin_ref),
                        role=Role.UNKNOWN,
                        structure=("fits", context.path.name, ext_origin_ref),
                        salience=0.8,
                        meta={
                            "hdu_index": hdu_index,
                            "xtension": xtension,
                            "extname": extname,
                            "extver": extver,
                            "bitpix": bitpix,
                            "naxis": naxis,
                            "shape": dims,
                            "tfields": tfields,
                            "cards_count": len(cards),
                        },
                    )
                    context.result.units.append(ext_unit)
                    if primary_unit:
                        context.result.relations.append(
                            Relation(
                                src=primary_unit.id,
                                dst=ext_unit.id,
                                kind=RelationKind.DESCRIBES,
                                evidence="fits-extension",
                            )
                        )

                    # Extract Table Columns (Deterministic origin: ref=f"hdu:{i}#col:{j}:{quote(name)}")
                    if tfields > 0:
                        for col_idx in range(1, tfields + 1):
                            if col_idx > _MAX_COLUMNS_PER_TABLE:
                                context.result.add_gap(
                                    f"Table column count exceeds limit ({tfields} > {_MAX_COLUMNS_PER_TABLE}); truncated",
                                    ref=ext_origin_ref,
                                )
                                break

                            col_name = str(cards.get(f"TTYPE{col_idx}", f"col_{col_idx}")).strip()
                            col_form = str(cards.get(f"TFORM{col_idx}", "")).strip()
                            col_unit = str(cards.get(f"TUNIT{col_idx}", "")).strip()

                            col_content = f"Column {col_idx} ({col_name}): format={col_form}" + (f", unit={col_unit}" if col_unit else "")
                            col_origin_ref = f"{ext_origin_ref}#col:{col_idx}:{quote(col_name, safe='-._~')}"

                            col_unit_obj = Unit(
                                source=source,
                                modality=Modality.SCHEMA,
                                content=col_content,
                                origin=Origin(source, col_origin_ref),
                                role=Role.UNKNOWN,
                                structure=("fits", context.path.name, ext_origin_ref, f"{col_idx}:{col_name}"),
                                salience=0.7,
                                meta={
                                    "column_index": col_idx,
                                    "name": col_name,
                                    "format": col_form,
                                    "unit": col_unit,
                                },
                            )
                            context.result.units.append(col_unit_obj)
                            context.result.relations.append(
                                Relation(
                                    src=ext_unit.id,
                                    dst=col_unit_obj.id,
                                    kind=RelationKind.DESCRIBES,
                                    evidence="table-column",
                                )
                            )

                hdu_index += 1

            if f.tell() < file_size and hdu_index >= _MAX_HDUS:
                context.result.add_gap(
                    f"HDU count reached maximum limit ({_MAX_HDUS}); remaining file data omitted",
                    ref=f"hdu:{_MAX_HDUS-1}",
                )

    except Exception as exc:
        scrubbed = _scrub_error_message(exc, "fits block parse", context.path.name)
        raise InvalidFitsData(context.path, "fits", scrubbed) from exc

    return _finish(context)


def extract_astronomy(path: str | Path) -> Extraction:
    """Unified entry point for astronomy data formats."""
    return extract_fits(path)
