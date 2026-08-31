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
import os
import stat
from pathlib import Path
from typing import Any
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


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _scrub_error_message(exc: BaseException, phase: str) -> str:
    """Produce a bounded, stable, scrubbed error description without private paths or binary buffer dumps."""
    cls_name = exc.__class__.__name__
    return f"{phase} failed ({cls_name})"


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
        raise InvalidFitsData(path, "astronomy", "stat error") from exc

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

    try:
        with path.open("rb") as f:
            header = f.read(30)
    except OSError as exc:
        raise InvalidFitsData(path, "astronomy", "read error") from exc

    # Standard FITS primary header starts with 'SIMPLE  = '
    if header.startswith(b"SIMPLE  ="):
        return "fits"

    raise InvalidFitsData(
        path, "astronomy", "unrecognized or unsupported astronomy format signature (failed closed)"
    )


# ---------------------------------------------------------------------------
# Strict 7-Bit ASCII Card Parsing Logic
# ---------------------------------------------------------------------------
def _parse_card_strict(card_bytes: bytes) -> tuple[str, Any, str]:
    """Parse an 80-byte strict 7-bit ASCII FITS card into (keyword, value, comment)."""
    if len(card_bytes) != 80:
        raise ValueError("card must be exactly 80 bytes")

    if any(b < 32 or b > 126 for b in card_bytes):
        raise ValueError("invalid non-printable or non-ASCII character in card")

    card_str = card_bytes.decode("ascii")

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
        accum: list[str] = []
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
            raise ValueError("unclosed single-quoted string")
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
    stream: Any, path: Path, hdu_index: int
) -> tuple[list[tuple[str, Any, str]], int]:
    """Read all 2880-byte header blocks for one HDU until 'END' card.

    Enforces that after 'END', every remaining byte in the 2880-byte block is ASCII space (0x20).
    Fails closed if non-END card count exceeds _MAX_CARDS_PER_HDU.
    Returns (ordered_cards_list, total_header_bytes).
    """
    ordered_cards: list[tuple[str, Any, str]] = []
    block_count = 0
    card_count = 0
    found_end = False

    while block_count < _MAX_HEADER_BLOCKS_PER_HDU:
        block = stream.read(_FITS_BLOCK_SIZE)
        if len(block) < _FITS_BLOCK_SIZE:
            if block_count == 0 and len(block) == 0:
                # Clean EOF between HDUs
                return [], 0
            raise InvalidFitsData(path, "fits", f"truncated FITS header block in HDU {hdu_index}")

        block_count += 1
        for card_idx in range(_CARDS_PER_BLOCK):
            card_offset = card_idx * _FITS_CARD_SIZE
            card_bytes = block[card_offset : card_offset + _FITS_CARD_SIZE]

            # In FITS 4.0, END keyword is exact 8 bytes 'END     ' with bytes 8..79 blank
            if card_bytes[:8] == b"END     ":
                # Validate END card: bytes 8..79 must be ASCII space 0x20
                if any(b != 0x20 for b in card_bytes[8:]):
                    raise InvalidFitsData(
                        path, "fits", f"HDU {hdu_index} header contains non-space byte in END card"
                    )
                # Validate rest of 2880 block: all bytes after END card must be ASCII space 0x20
                rest_of_block = block[card_offset + _FITS_CARD_SIZE :]
                if any(b != 0x20 for b in rest_of_block):
                    raise InvalidFitsData(
                        path, "fits", f"HDU {hdu_index} header contains non-space byte after END card"
                    )
                found_end = True
                break

            card_count += 1
            if card_count > _MAX_CARDS_PER_HDU:
                raise InvalidFitsData(
                    path, "fits", f"HDU {hdu_index} header card count exceeded limit {_MAX_CARDS_PER_HDU}"
                )

            kw, val, comm = _parse_card_strict(card_bytes)
            if kw or comm:
                ordered_cards.append((kw, val, comm))

        if found_end:
            break

    if not found_end:
        raise InvalidFitsData(
            path, "fits", f"HDU {hdu_index} header exceeded {_MAX_HEADER_BLOCKS_PER_HDU} blocks without END card"
        )

    return ordered_cards, block_count * _FITS_BLOCK_SIZE


# ---------------------------------------------------------------------------
# Stream Parser & Extent Validation
# ---------------------------------------------------------------------------
def _parse_fits_stream(
    f: Any, path: Path, result: Extraction, file_size: int
) -> None:
    """Parse HDUs from an open FITS stream and validate structure and payload extents."""
    hdu_index = 0
    primary_unit: Unit | None = None
    source = result.source

    while hdu_index < _MAX_HDUS:
        ordered_cards, header_bytes = _read_hdu_header(f, path, hdu_index)
        if not ordered_cards and header_bytes == 0:
            break

        cards: dict[str, Any] = {}
        for kw, val, _ in ordered_cards:
            if kw and kw not in cards:
                cards[kw] = val

        # 1. Enforce FITS 4.0 Mandatory Ordered Prefixes and Reject Duplicates
        if hdu_index == 0:
            if not ordered_cards or ordered_cards[0][0] != "SIMPLE":
                raise InvalidFitsData(path, "fits", "primary HDU must begin with 'SIMPLE' card at card index 0")
            simple_val = ordered_cards[0][1]
            if type(simple_val) is not bool:
                raise InvalidFitsData(path, "fits", "primary HDU 'SIMPLE' card must be boolean T/F")
            if simple_val is not True:
                result.add_gap(
                    "Primary HDU declares SIMPLE=F (non-standard FITS conformant file)",
                    ref="hdu:0",
                )

            if len(ordered_cards) < 2 or ordered_cards[1][0] != "BITPIX":
                raise InvalidFitsData(path, "fits", "primary HDU must have 'BITPIX' card at card index 1")
            bitpix = ordered_cards[1][1]
            if type(bitpix) is not int or bitpix not in _VALID_BITPIX:
                raise InvalidFitsData(
                    path, "fits", f"primary HDU invalid BITPIX; must be one of {sorted(_VALID_BITPIX)}"
                )

            if len(ordered_cards) < 3 or ordered_cards[2][0] != "NAXIS":
                raise InvalidFitsData(path, "fits", "primary HDU must have 'NAXIS' card at card index 2")
            naxis = ordered_cards[2][1]
            if type(naxis) is not int or naxis < 0 or naxis > _MAX_DIMENSIONS:
                raise InvalidFitsData(
                    path, "fits", f"primary HDU invalid NAXIS; must be integer 0..{_MAX_DIMENSIONS}"
                )

            dims: list[int] = []
            for n in range(1, naxis + 1):
                card_pos = 2 + n
                key = f"NAXIS{n}"
                if len(ordered_cards) <= card_pos or ordered_cards[card_pos][0] != key:
                    raise InvalidFitsData(
                        path, "fits", f"primary HDU missing mandatory '{key}' card at card index {card_pos}"
                    )
                dim_val = ordered_cards[card_pos][1]
                if type(dim_val) is not int or dim_val < 0:
                    raise InvalidFitsData(
                        path, "fits", f"primary HDU invalid {key}; must be non-negative integer"
                    )
                dims.append(dim_val)

            # Check for Random Groups in Primary HDU
            next_pos = 3 + naxis
            is_random_groups = False
            if len(ordered_cards) > next_pos and ordered_cards[next_pos][0] == "GROUPS":
                groups_val = ordered_cards[next_pos][1]
                if type(groups_val) is not bool:
                    raise InvalidFitsData(path, "fits", "primary HDU 'GROUPS' card must be boolean")
                if groups_val is True:
                    is_random_groups = True

            if is_random_groups:
                if naxis < 1:
                    raise InvalidFitsData(path, "fits", "random groups structure requires NAXIS >= 1")
                if dims[0] != 0:
                    raise InvalidFitsData(path, "fits", "random groups structure requires NAXIS1=0")

                pcount_pos = next_pos + 1
                if len(ordered_cards) <= pcount_pos or ordered_cards[pcount_pos][0] != "PCOUNT":
                    raise InvalidFitsData(
                        path, "fits", "random groups structure requires 'PCOUNT' card following 'GROUPS'"
                    )
                pcount = ordered_cards[pcount_pos][1]
                if type(pcount) is not int or pcount < 0:
                    raise InvalidFitsData(
                        path, "fits", "random groups structure invalid PCOUNT; must be non-negative integer"
                    )

                gcount_pos = next_pos + 2
                if len(ordered_cards) <= gcount_pos or ordered_cards[gcount_pos][0] != "GCOUNT":
                    raise InvalidFitsData(
                        path, "fits", "random groups structure requires 'GCOUNT' card following 'PCOUNT'"
                    )
                gcount = ordered_cards[gcount_pos][1]
                if type(gcount) is not int or gcount < 1:
                    raise InvalidFitsData(
                        path, "fits", "random groups structure invalid GCOUNT; must be integer >= 1"
                    )

                mandatory_count = 6 + naxis
                mandatory_keys = {
                    "SIMPLE",
                    "BITPIX",
                    "NAXIS",
                    *(f"NAXIS{i}" for i in range(1, naxis + 1)),
                    "GROUPS",
                    "PCOUNT",
                    "GCOUNT",
                }
            else:
                pcount = 0
                gcount = 1
                mandatory_count = 3 + naxis
                mandatory_keys = {
                    "SIMPLE",
                    "BITPIX",
                    "NAXIS",
                    *(f"NAXIS{i}" for i in range(1, naxis + 1)),
                }

            # Reject duplicate mandatory cards in Primary HDU
            for idx, (k, _, _) in enumerate(ordered_cards):
                if idx >= mandatory_count and k in mandatory_keys:
                    raise InvalidFitsData(
                        path, "fits", f"primary HDU contains duplicate mandatory keyword '{k}'"
                    )
                if not is_random_groups and k == "GROUPS":
                    raise InvalidFitsData(path, "fits", "primary HDU contains unexpected 'GROUPS' keyword")

        else:
            # Extension HDU
            if not ordered_cards or ordered_cards[0][0] != "XTENSION":
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} must begin with 'XTENSION' card at card index 0"
                )
            xtension_val = ordered_cards[0][1]
            if type(xtension_val) is not str or not xtension_val.strip():
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} 'XTENSION' card must be a non-empty string"
                )

            if len(ordered_cards) < 2 or ordered_cards[1][0] != "BITPIX":
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} must have 'BITPIX' card at card index 1"
                )
            bitpix = ordered_cards[1][1]
            if type(bitpix) is not int or bitpix not in _VALID_BITPIX:
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} invalid BITPIX; must be one of {sorted(_VALID_BITPIX)}"
                )

            if len(ordered_cards) < 3 or ordered_cards[2][0] != "NAXIS":
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} must have 'NAXIS' card at card index 2"
                )
            naxis = ordered_cards[2][1]
            if type(naxis) is not int or naxis < 0 or naxis > _MAX_DIMENSIONS:
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} invalid NAXIS; must be integer 0..{_MAX_DIMENSIONS}"
                )

            dims = []
            for n in range(1, naxis + 1):
                card_pos = 2 + n
                key = f"NAXIS{n}"
                if len(ordered_cards) <= card_pos or ordered_cards[card_pos][0] != key:
                    raise InvalidFitsData(
                        path, "fits", f"extension HDU {hdu_index} missing mandatory '{key}' card at card index {card_pos}"
                    )
                dim_val = ordered_cards[card_pos][1]
                if type(dim_val) is not int or dim_val < 0:
                    raise InvalidFitsData(
                        path, "fits", f"extension HDU {hdu_index} invalid {key}; must be non-negative integer"
                    )
                dims.append(dim_val)

            pcount_pos = 3 + naxis
            if len(ordered_cards) <= pcount_pos or ordered_cards[pcount_pos][0] != "PCOUNT":
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} missing mandatory 'PCOUNT' card at card index {pcount_pos}"
                )
            pcount = ordered_cards[pcount_pos][1]
            if type(pcount) is not int or pcount < 0:
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} invalid PCOUNT; must be non-negative integer"
                )

            gcount_pos = 4 + naxis
            if len(ordered_cards) <= gcount_pos or ordered_cards[gcount_pos][0] != "GCOUNT":
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} missing mandatory 'GCOUNT' card at card index {gcount_pos}"
                )
            gcount = ordered_cards[gcount_pos][1]
            if type(gcount) is not int or gcount < 1:
                raise InvalidFitsData(
                    path, "fits", f"extension HDU {hdu_index} invalid GCOUNT; must be integer >= 1"
                )

            mandatory_count = 5 + naxis
            mandatory_keys = {
                "XTENSION",
                "BITPIX",
                "NAXIS",
                *(f"NAXIS{i}" for i in range(1, naxis + 1)),
                "PCOUNT",
                "GCOUNT",
            }
            # Reject duplicate mandatory cards in Extension HDU
            for idx, (k, _, _) in enumerate(ordered_cards):
                if idx >= mandatory_count and k in mandatory_keys:
                    raise InvalidFitsData(
                        path, "fits", f"extension HDU {hdu_index} contains duplicate mandatory keyword '{k}'"
                    )

        # 2. Correct and Bounded Data Extent Calculation
        bytes_per_val = abs(bitpix) // 8

        if hdu_index == 0:
            if is_random_groups:
                axis_prod = 0 if naxis == 1 else 1
                if naxis > 1:
                    for d in dims[1:]:
                        axis_prod *= d
                payload_bytes = bytes_per_val * gcount * (pcount + axis_prod)
            else:
                if naxis == 0:
                    payload_bytes = 0
                else:
                    prod = 1
                    for d in dims:
                        prod *= d
                    payload_bytes = bytes_per_val * prod
        else:
            if naxis == 0:
                axis_prod = 0
            else:
                axis_prod = 1
                for d in dims:
                    axis_prod *= d
            payload_bytes = bytes_per_val * gcount * (pcount + axis_prod)

        if payload_bytes > _MAX_FILE_BYTES:
            raise InvalidFitsData(path, "fits", f"HDU {hdu_index} payload size exceeds limit")

        padded_data_bytes = 0
        if payload_bytes > 0:
            rem = payload_bytes % _FITS_BLOCK_SIZE
            padded_data_bytes = (
                payload_bytes if rem == 0 else payload_bytes + (_FITS_BLOCK_SIZE - rem)
            )

        current_offset = f.tell()
        if current_offset + padded_data_bytes > file_size:
            raise InvalidFitsData(
                path,
                "fits",
                f"HDU {hdu_index} payload truncated: requires {padded_data_bytes} bytes, file ends at offset {file_size}",
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
        for wcs_k in (
            "CTYPE1",
            "CTYPE2",
            "CRVAL1",
            "CRVAL2",
            "CRPIX1",
            "CRPIX2",
            "CDELT1",
            "CDELT2",
            "RADESYS",
            "EQUINOX",
        ):
            if wcs_k in cards:
                wcs_info[wcs_k] = cards[wcs_k]

        # 3. Emit Primary HDU Unit
        if hdu_index == 0:
            if is_random_groups:
                content_parts = [
                    f"FITS Primary HDU: Random Groups ({gcount} groups, {pcount} parameters, BITPIX={bitpix})"
                ]
            else:
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
                structure=("fits", path.name, "hdu:0"),
                salience=0.9,
                meta={
                    "hdu_index": 0,
                    "type": "RANDOM_GROUPS" if is_random_groups else "PRIMARY",
                    "bitpix": bitpix,
                    "naxis": naxis,
                    "shape": dims,
                    "pcount": pcount,
                    "gcount": gcount,
                    "groups": is_random_groups,
                    "telescope": str(telescop) if telescop else None,
                    "instrument": str(instrume) if instrume else None,
                    "object": str(target_obj) if target_obj else None,
                    "date_obs": str(date_obs) if date_obs else None,
                    "exptime": exptime,
                    "wcs": wcs_info,
                    "cards_count": len(cards),
                },
            )
            result.units.append(primary_unit)

            if not telescop and not instrume:
                result.add_gap(
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
                structure=("fits", path.name, ext_origin_ref),
                salience=0.8,
                meta={
                    "hdu_index": hdu_index,
                    "xtension": xtension,
                    "extname": extname,
                    "extver": extver,
                    "bitpix": bitpix,
                    "naxis": naxis,
                    "shape": dims,
                    "pcount": pcount,
                    "gcount": gcount,
                    "tfields": tfields,
                    "cards_count": len(cards),
                },
            )
            result.units.append(ext_unit)
            if primary_unit:
                result.relations.append(
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
                        result.add_gap(
                            f"Table column count exceeds limit ({tfields} > {_MAX_COLUMNS_PER_TABLE}); truncated",
                            ref=ext_origin_ref,
                            kind="truncated-table-columns",
                        )
                        break

                    col_name = str(cards.get(f"TTYPE{col_idx}", f"col_{col_idx}")).strip()
                    col_form = str(cards.get(f"TFORM{col_idx}", "")).strip()
                    col_unit = str(cards.get(f"TUNIT{col_idx}", "")).strip()

                    col_content = (
                        f"Column {col_idx} ({col_name}): format={col_form}"
                        + (f", unit={col_unit}" if col_unit else "")
                    )
                    col_origin_ref = f"{ext_origin_ref}#col:{col_idx}:{quote(col_name, safe='-._~')}"

                    col_unit_obj = Unit(
                        source=source,
                        modality=Modality.SCHEMA,
                        content=col_content,
                        origin=Origin(source, col_origin_ref),
                        role=Role.UNKNOWN,
                        structure=(
                            "fits",
                            path.name,
                            ext_origin_ref,
                            f"{col_idx}:{col_name}",
                        ),
                        salience=0.7,
                        meta={
                            "column_index": col_idx,
                            "name": col_name,
                            "format": col_form,
                            "unit": col_unit,
                        },
                    )
                    result.units.append(col_unit_obj)
                    result.relations.append(
                        Relation(
                            src=ext_unit.id,
                            dst=col_unit_obj.id,
                            kind=RelationKind.DESCRIBES,
                            evidence="table-column",
                        )
                    )

        hdu_index += 1

    if f.tell() < file_size:
        if hdu_index >= _MAX_HDUS:
            result.add_gap(
                f"HDU count reached maximum limit ({_MAX_HDUS}); remaining file data omitted",
                ref=f"hdu:{_MAX_HDUS-1}",
                kind="fits-hdu-limit-exceeded",
            )
        else:
            raise InvalidFitsData(path, "fits", "unparsed trailing bytes after last HDU")


def _finalize_extraction(result: Extraction) -> None:
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
        raise AssertionError("FITS extractor emitted a dangling relation")

    result.meta["counts"] = {
        "units": len(result.units),
        "relations": len(result.relations),
        "gaps": len(result.gaps),
    }


# ---------------------------------------------------------------------------
# Main FITS Extractor
# ---------------------------------------------------------------------------
def extract_fits(path: str | Path) -> Extraction:
    """Extract HDU inventories, schemas, and observation metadata from FITS files."""
    path = Path(path)
    source = str(path)

    try:
        f = path.open("rb")
    except OSError as exc:
        raise InvalidFitsData(path, "fits", "open error") from exc

    with f:
        try:
            st_pre = os.fstat(f.fileno())
        except OSError as exc:
            raise InvalidFitsData(path, "fits", "stat error") from exc

        if not stat.S_ISREG(st_pre.st_mode):
            raise InvalidFitsData(path, "fits", "input is not a regular file")
        if st_pre.st_size == 0:
            raise InvalidFitsData(path, "fits", "file is empty")
        if st_pre.st_size % _FITS_BLOCK_SIZE != 0:
            raise InvalidFitsData(
                path,
                "fits",
                f"file size {st_pre.st_size} is not a multiple of 2880 bytes",
            )
        if st_pre.st_size > _MAX_FILE_BYTES:
            raise InvalidFitsData(
                path,
                "fits",
                f"file is {st_pre.st_size} bytes; limit is {_MAX_FILE_BYTES} bytes",
            )

        # 1. Initial fingerprint over this exact file descriptor
        f.seek(0)
        digest = hashlib.sha256()
        byte_count = 0
        while chunk := f.read(_HASH_CHUNK_BYTES):
            byte_count += len(chunk)
            digest.update(chunk)

        if byte_count != st_pre.st_size:
            raise InvalidFitsData(
                path, "fits", "source changed while it was fingerprinted"
            )

        manifest_sha256 = digest.hexdigest()
        f.seek(0)

        result = Extraction(source=source, kind="fits")
        result.meta.update(
            {
                "inputs": [
                    {
                        "source": source,
                        "kind": "fits",
                        "tier": 3,
                        "bytes": byte_count,
                        "sha256": manifest_sha256,
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

        # 2. Parse HDUs directly from f with narrowed expected exceptions
        file_size = st_pre.st_size
        try:
            _parse_fits_stream(f, path, result, file_size)
        except InvalidFitsData:
            raise
        except (ValueError, OSError, OverflowError, EOFError) as exc:
            scrubbed = _scrub_error_message(exc, "fits block parse")
            raise InvalidFitsData(path, "fits", scrubbed) from exc

        # 3. Post-parse rehash and fstat on the same file descriptor
        try:
            st_post = os.fstat(f.fileno())
        except OSError as exc:
            raise InvalidFitsData(path, "fits", "stat error") from exc

        if (
            st_pre.st_dev,
            st_pre.st_ino,
            st_pre.st_size,
            st_pre.st_mtime_ns,
        ) != (
            st_post.st_dev,
            st_post.st_ino,
            st_post.st_size,
            st_post.st_mtime_ns,
        ):
            raise InvalidFitsData(
                path, "fits", "source changed while it was being extracted"
            )

        f.seek(0)
        rehash_digest = hashlib.sha256()
        rehash_bytes = 0
        while chunk := f.read(_HASH_CHUNK_BYTES):
            rehash_bytes += len(chunk)
            rehash_digest.update(chunk)

        if (
            rehash_bytes != st_pre.st_size
            or rehash_digest.hexdigest() != manifest_sha256
        ):
            raise InvalidFitsData(
                path, "fits", "source changed while it was being extracted"
            )

        # 4. Check pathname snapshot consistency
        try:
            st_path = path.stat()
            if (
                st_path.st_dev,
                st_path.st_ino,
                st_path.st_size,
                st_path.st_mtime_ns,
            ) != (
                st_post.st_dev,
                st_post.st_ino,
                st_post.st_size,
                st_post.st_mtime_ns,
            ):
                raise InvalidFitsData(
                    path, "fits", "source changed while it was being extracted"
                )
        except OSError as exc:
            raise InvalidFitsData(path, "fits", "stat error") from exc

        _finalize_extraction(result)
        return result


def extract_astronomy(path: str | Path) -> Extraction:
    """Unified entry point for astronomy data formats."""
    return extract_fits(path)


def extract(path: str | Path) -> Extraction:
    """Extract metadata from a FITS astronomy data file."""
    return extract_astronomy(path)
