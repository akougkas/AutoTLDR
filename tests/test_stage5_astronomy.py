"""Tests for native bounded metadata extraction of FITS astronomy data files.

Primary authoritative specifications:
- NASA / IAU FITS Standard Version 4.0 (2018):
  https://fits.gsfc.nasa.gov/standard40/fits_standard40aa.pdf
- NASA FITS Standard Overview and Definitions:
  https://fits.gsfc.nasa.gov/fits_standard.html
"""

import os
import sys
import tempfile
from pathlib import Path
import pytest

from autotldr.unit import Modality, RelationKind, Role
from autotldr.extract.astronomy import (
    extract_astronomy,
    extract_fits,
    detect_astronomy_kind,
    InvalidFitsData,
)


def _make_fits_card(keyword: str, value: object = None, comment: str | None = None) -> bytes:
    kw = f"{keyword:<8}"[:8]
    if value is None:
        card = kw + (f" / {comment}" if comment else "")
    elif isinstance(value, bool):
        val_str = "T" if value else "F"
        card = f"{kw}= {val_str:>20}" + (f" / {comment}" if comment else "")
    elif isinstance(value, int):
        card = f"{kw}= {value:>20d}" + (f" / {comment}" if comment else "")
    elif isinstance(value, float):
        card = f"{kw}= {value:>20.8E}" + (f" / {comment}" if comment else "")
    elif isinstance(value, str):
        val_str = f"'{value:<8}'"
        card = f"{kw}= {val_str:<20}" + (f" / {comment}" if comment else "")
    else:
        card = f"{kw}= {str(value):<20}"
    return f"{card:<80}"[:80].encode("ascii")


def _build_fits_header(cards: list[bytes]) -> bytes:
    body = b"".join(cards) + b"END" + b" " * 77
    pad = 2880 - (len(body) % 2880)
    if pad != 2880:
        body += b" " * pad
    return body


def test_lazy_imports():
    """Importing astronomy must not import astropy or heavy libs at module level."""
    import subprocess
    cmd = [
        sys.executable,
        "-c",
        "import sys; import autotldr.extract.astronomy; assert 'astropy' not in sys.modules, 'astropy was eagerly imported!'",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Lazy import failed: {res.stderr}"


def test_extract_primary_fits(tmp_path):
    cards = [
        _make_fits_card("SIMPLE", True, "Standard FITS"),
        _make_fits_card("BITPIX", 16, "16-bit integers"),
        _make_fits_card("NAXIS", 2, "2D image"),
        _make_fits_card("NAXIS1", 100, "X dimension"),
        _make_fits_card("NAXIS2", 50, "Y dimension"),
        _make_fits_card("EXTEND", True, "Extensions may follow"),
        _make_fits_card("TELESCOP", "HST", "Hubble Space Telescope"),
        _make_fits_card("INSTRUME", "WFC3", "Wide Field Camera 3"),
        _make_fits_card("OBJECT", "M31", "Target object"),
        _make_fits_card("DATE-OBS", "2026-08-30", "Observation UTC date"),
        _make_fits_card("EXPTIME", 1200.0, "Exposure time in seconds"),
        _make_fits_card("CTYPE1", "RA---TAN", "WCS axis 1"),
        _make_fits_card("CRVAL1", 10.684, "Reference RA"),
        _make_fits_card("CRPIX1", 50.5, "Reference X pixel"),
    ]
    hdr = _build_fits_header(cards)
    data_size = 100 * 50 * 2  # 10000 bytes
    data_pad = 2880 - (data_size % 2880)
    data = b"\x00" * (data_size + data_pad)

    p = tmp_path / "observation.fits"
    p.write_bytes(hdr + data)

    assert detect_astronomy_kind(p) == "fits"
    extraction = extract_fits(p)
    assert extraction.kind == "fits"
    assert extraction.source == str(p)

    primary_units = [u for u in extraction.units if u.origin.ref == "hdu:0"]
    assert len(primary_units) == 1
    u = primary_units[0]
    assert u.modality == Modality.SCHEMA
    assert u.role == Role.UNKNOWN
    assert u.meta["telescope"] == "HST"
    assert u.meta["instrument"] == "WFC3"
    assert u.meta["object"] == "M31"
    assert u.meta["shape"] == [100, 50]
    assert u.meta["bitpix"] == 16
    assert u.meta["wcs"]["CTYPE1"] == "RA---TAN"


def test_quoted_string_with_slash_and_doubled_quote(tmp_path):
    """Quoted string containing slash and doubled quote must be parsed without corruption."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("TELESCOP", "ESO/VLT / 8.2m", "Telescope with slash in name"),
        _make_fits_card("OBSERVER", "O''Neil, J.", "Observer with doubled quote"),
    ]
    p = tmp_path / "quoted.fits"
    p.write_bytes(_build_fits_header(cards))

    extraction = extract_fits(p)
    u = extraction.units[0]
    assert u.meta["telescope"] == "ESO/VLT / 8.2m"


def test_duplicate_extname_and_column_names(tmp_path):
    """Duplicate EXTNAMEs and duplicate column names must not collide in Unit IDs or refs."""
    prim_cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
    ]
    prim_hdr = _build_fits_header(prim_cards)

    # Extension 1 with EXTNAME='DATA' and duplicate columns 'FLUX', 'FLUX'
    ext1_cards = [
        _make_fits_card("XTENSION", "BINTABLE"),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 2),
        _make_fits_card("NAXIS1", 8),
        _make_fits_card("NAXIS2", 5),
        _make_fits_card("TFIELDS", 2),
        _make_fits_card("EXTNAME", "DATA"),
        _make_fits_card("TTYPE1", "FLUX"),
        _make_fits_card("TFORM1", "1E"),
        _make_fits_card("TTYPE2", "FLUX"),
        _make_fits_card("TFORM2", "1E"),
    ]
    ext1_hdr = _build_fits_header(ext1_cards)
    data1 = b"\x00" * 2880

    # Extension 2 ALSO with EXTNAME='DATA'
    ext2_cards = [
        _make_fits_card("XTENSION", "IMAGE"),
        _make_fits_card("BITPIX", 16),
        _make_fits_card("NAXIS", 2),
        _make_fits_card("NAXIS1", 10),
        _make_fits_card("NAXIS2", 10),
        _make_fits_card("EXTNAME", "DATA"),
    ]
    ext2_hdr = _build_fits_header(ext2_cards)
    data2 = b"\x00" * 2880

    p = tmp_path / "dup_ext.fits"
    p.write_bytes(prim_hdr + ext1_hdr + data1 + ext2_hdr + data2)

    extraction = extract_fits(p)
    unit_ids = [u.id for u in extraction.units]
    assert len(unit_ids) == len(set(unit_ids)), "All emitted Unit IDs must be strictly unique"

    refs = [u.origin.ref for u in extraction.units]
    assert "hdu:1" in refs
    assert "hdu:2" in refs
    assert "hdu:1#col:1:FLUX" in refs
    assert "hdu:1#col:2:FLUX" in refs


def test_payload_truncation_rejected(tmp_path):
    """FITS header declaring 100x100 32-bit payload but ending at header must decline."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 32),
        _make_fits_card("NAXIS", 2),
        _make_fits_card("NAXIS1", 100),
        _make_fits_card("NAXIS2", 100),
    ]
    p = tmp_path / "header_only_truncated.fits"
    # Declares 100*100*4 = 40,000 bytes data payload, but writes only header
    p.write_bytes(_build_fits_header(cards))

    with pytest.raises(InvalidFitsData, match="payload truncated"):
        extract_fits(p)


def test_fail_closed_unknown_bytes_and_spoofed_suffix(tmp_path):
    """Spoofed suffix on unknown bytes must fail closed."""
    p_fake = tmp_path / "fake.fits"
    p_fake.write_bytes(b"NOT_A_FITS_HEADER_FILE_AT_ALL_1234" + b"\x00" * 3000)

    with pytest.raises(InvalidFitsData, match="failed closed"):
        detect_astronomy_kind(p_fake)


def test_missing_required_card_order(tmp_path):
    """FITS primary missing SIMPLE as first card must decline."""
    cards = [
        _make_fits_card("BITPIX", 8),
        _make_fits_card("SIMPLE", True),
        _make_fits_card("NAXIS", 0),
    ]
    p = tmp_path / "wrong_order.fits"
    p.write_bytes(_build_fits_header(cards))

    with pytest.raises(InvalidFitsData, match="must begin with 'SIMPLE'"):
        extract_fits(p)


def test_invalid_bitpix_rejected(tmp_path):
    """Invalid BITPIX value must decline."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 17),  # 17 is not in (8, 16, 32, 64, -32, -64)
        _make_fits_card("NAXIS", 0),
    ]
    p = tmp_path / "bad_bitpix.fits"
    p.write_bytes(_build_fits_header(cards))

    with pytest.raises(InvalidFitsData, match="invalid BITPIX"):
        extract_fits(p)


def test_error_message_scrubbing(tmp_path):
    """Error messages must not leak private directory paths."""
    secret_dir = tmp_path / "top_secret_astronomy_observatory"
    secret_dir.mkdir()
    p = secret_dir / "bad.fits"
    p.write_bytes(b"SIMPLE  =                    T" + b" " * 2854)

    with pytest.raises(InvalidFitsData) as exc_info:
        extract_fits(p)

    msg = str(exc_info.value)
    assert "top_secret_astronomy_observatory" not in msg
    assert "bad.fits:" in msg
