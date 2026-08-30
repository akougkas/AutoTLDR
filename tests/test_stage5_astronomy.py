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

    primary_units = [u for u in extraction.units if u.origin.ref == "primary"]
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


def test_extract_multi_extension_fits(tmp_path):
    # 1. Primary HDU (no data, NAXIS=0)
    prim_cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("EXTEND", True),
        _make_fits_card("TELESCOP", "JWST"),
        _make_fits_card("INSTRUME", "NIRCam"),
    ]
    prim_hdr = _build_fits_header(prim_cards)

    # 2. Extension 1: BINTABLE
    ext1_cards = [
        _make_fits_card("XTENSION", "BINTABLE"),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 2),
        _make_fits_card("NAXIS1", 16),
        _make_fits_card("NAXIS2", 10),
        _make_fits_card("PCOUNT", 0),
        _make_fits_card("GCOUNT", 1),
        _make_fits_card("TFIELDS", 3),
        _make_fits_card("EXTNAME", "SOURCES"),
        _make_fits_card("TTYPE1", "STAR_ID"),
        _make_fits_card("TFORM1", "1J"),
        _make_fits_card("TTYPE2", "FLUX"),
        _make_fits_card("TFORM2", "1E"),
        _make_fits_card("TUNIT2", "count/s"),
        _make_fits_card("TTYPE3", "QUALITY"),
        _make_fits_card("TFORM3", "1I"),
    ]
    ext1_hdr = _build_fits_header(ext1_cards)
    ext1_data_len = 16 * 10
    ext1_data_pad = 2880 - (ext1_data_len % 2880)
    ext1_data = b"\x00" * (ext1_data_len + ext1_data_pad)

    # 3. Extension 2: IMAGE
    ext2_cards = [
        _make_fits_card("XTENSION", "IMAGE"),
        _make_fits_card("BITPIX", -32),
        _make_fits_card("NAXIS", 2),
        _make_fits_card("NAXIS1", 20),
        _make_fits_card("NAXIS2", 20),
        _make_fits_card("EXTNAME", "SCI"),
    ]
    ext2_hdr = _build_fits_header(ext2_cards)
    ext2_data_len = 20 * 20 * 4
    ext2_data_pad = 2880 - (ext2_data_len % 2880)
    ext2_data = b"\x00" * (ext2_data_len + ext2_data_pad)

    p = tmp_path / "jwst_dataset.fits"
    p.write_bytes(prim_hdr + ext1_hdr + ext1_data + ext2_hdr + ext2_data)

    extraction = extract_astronomy(p)
    assert len(extraction.units) >= 6

    # Verify primary and extension relations
    prim_unit = [u for u in extraction.units if u.origin.ref == "primary"][0]
    ext_units = [u for u in extraction.units if u.origin.ref in {"ext:SOURCES", "ext:SCI"}]
    assert len(ext_units) == 2

    # Verify column units
    col_units = [u for u in extraction.units if "col:" in u.origin.ref]
    assert len(col_units) == 3
    col_names = [u.meta["name"] for u in col_units]
    assert set(col_names) == {"STAR_ID", "FLUX", "QUALITY"}

    # Relations check
    table_unit = [u for u in extraction.units if u.origin.ref == "ext:SOURCES"][0]
    for c in col_units:
        assert any(
            r.src == table_unit.id and r.dst == c.id and r.kind == RelationKind.DESCRIBES
            for r in extraction.relations
        )


def test_fits_payload_skip_checked_arithmetic(tmp_path):
    """Ensure parser skips data payload using arithmetic without loading large payload."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 32),
        _make_fits_card("NAXIS", 2),
        _make_fits_card("NAXIS1", 1000),
        _make_fits_card("NAXIS2", 1000),  # 4 MB payload
        _make_fits_card("TELESCOP", "VLT"),
    ]
    hdr = _build_fits_header(cards)
    data_len = 1000 * 1000 * 4
    data_pad = 2880 - (data_len % 2880)
    data = b"\x00" * (data_len + data_pad)

    p = tmp_path / "large_image.fits"
    p.write_bytes(hdr + data)

    extraction = extract_fits(p)
    assert extraction.units[0].meta["shape"] == [1000, 1000]


def test_missing_observation_cards_gap(tmp_path):
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
    ]
    hdr = _build_fits_header(cards)
    p = tmp_path / "no_obs.fits"
    p.write_bytes(hdr)

    extraction = extract_fits(p)
    assert any("TELESCOP or INSTRUME" in str(g) for g in extraction.gaps)


def test_truncated_fits_rejected(tmp_path):
    p = tmp_path / "truncated.fits"
    p.write_bytes(b"SIMPLE  =                    T / Standard FITS" + b" " * 100)
    with pytest.raises(InvalidFitsData, match="multiple of 2880"):
        extract_fits(p)


def test_empty_fits_rejected(tmp_path):
    empty = tmp_path / "empty.fits"
    empty.write_bytes(b"")
    with pytest.raises(InvalidFitsData, match="empty"):
        extract_fits(empty)


def test_determinism(tmp_path):
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("TELESCOP", "TEST"),
    ]
    p = tmp_path / "det.fits"
    p.write_bytes(_build_fits_header(cards))

    ext1 = extract_fits(p)
    ext2 = extract_fits(p)
    assert [u.id for u in ext1.units] == [u.id for u in ext2.units]
