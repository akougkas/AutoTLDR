"""Tests for native bounded metadata extraction of FITS astronomy data files.

Primary authoritative specifications:
- NASA / IAU FITS Standard Version 4.0 (2018):
  https://fits.gsfc.nasa.gov/standard40/fits_standard40aa.pdf
- NASA FITS Standard Overview and Definitions:
  https://fits.gsfc.nasa.gov/fits_standard.html
"""

import os
import sys
from pathlib import Path
import pytest

from autotldr.unit import Modality, Role
import autotldr.extract.astronomy as astronomy
from autotldr.extract.astronomy import (
    extract_astronomy,
    extract_fits,
    detect_astronomy_kind,
    InvalidFitsData,
    _scrub_error_message,
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
        escaped = value.replace("'", "''")
        val_str = f"'{escaped:<8}'"
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
        _make_fits_card("PCOUNT", 0),
        _make_fits_card("GCOUNT", 1),
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
        _make_fits_card("PCOUNT", 0),
        _make_fits_card("GCOUNT", 1),
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


def test_header_only_random_groups_requiring_payload_fails(tmp_path):
    """Random groups primary requiring payload fails on header-only file."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 16),
        _make_fits_card("NAXIS", 1),
        _make_fits_card("NAXIS1", 0),
        _make_fits_card("GROUPS", True),
        _make_fits_card("PCOUNT", 5),
        _make_fits_card("GCOUNT", 10),
    ]
    # (16//8) * 10 * (5 + 0) = 100 bytes payload -> padded to 2880 bytes
    hdr = _build_fits_header(cards)
    p = tmp_path / "random_groups_truncated.fits"
    p.write_bytes(hdr)

    with pytest.raises(InvalidFitsData, match="payload truncated"):
        extract_fits(p)


def test_valid_padded_random_groups_fixture_passes(tmp_path):
    """Valid random groups primary with padded payload passes and extracts correctly."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 16),
        _make_fits_card("NAXIS", 2),
        _make_fits_card("NAXIS1", 0),
        _make_fits_card("NAXIS2", 4),
        _make_fits_card("GROUPS", True),
        _make_fits_card("PCOUNT", 2),
        _make_fits_card("GCOUNT", 5),
        _make_fits_card("TELESCOP", "VLA"),
    ]
    # (16//8) * 5 * (2 + 4) = 2 * 5 * 6 = 60 bytes -> padded to 2880 bytes
    hdr = _build_fits_header(cards)
    data = b"\x00" * 2880
    p = tmp_path / "random_groups_valid.fits"
    p.write_bytes(hdr + data)

    extraction = extract_fits(p)
    assert extraction.kind == "fits"
    primary_units = [u for u in extraction.units if u.origin.ref == "hdu:0"]
    assert len(primary_units) == 1
    u = primary_units[0]
    assert u.meta["type"] == "RANDOM_GROUPS"
    assert u.meta["groups"] is True
    assert u.meta["gcount"] == 5
    assert u.meta["pcount"] == 2
    assert u.meta["telescope"] == "VLA"


def test_pcount_bitpix_boundary_equation_difference(tmp_path):
    """Boundary where wrong equation needs 2880 but correct equation needs 5760 bytes."""
    # Primary HDU: 0-dimensional header (2880 bytes, 0 data)
    prim_cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("EXTEND", True),
    ]
    prim_hdr = _build_fits_header(prim_cards)

    # Extension HDU:
    # BITPIX = 16 (2 bytes/val)
    # NAXIS = 1, NAXIS1 = 500 (prod = 500)
    # PCOUNT = 1000, GCOUNT = 1
    # Buggy equation: 1 * (2 * 500 + 1000) = 2000 bytes -> padded = 2880 bytes.
    # Correct standard equation: 2 * 1 * (1000 + 500) = 3000 bytes -> padded = 5760 bytes.
    ext_cards = [
        _make_fits_card("XTENSION", "IMAGE"),
        _make_fits_card("BITPIX", 16),
        _make_fits_card("NAXIS", 1),
        _make_fits_card("NAXIS1", 500),
        _make_fits_card("PCOUNT", 1000),
        _make_fits_card("GCOUNT", 1),
    ]
    ext_hdr = _build_fits_header(ext_cards)

    # Provide only 2880 bytes of extension data payload (total file: 2880 + 2880 + 2880 = 8640)
    # Under buggy equation, 8640 would pass; under standard equation, 11520 is required.
    ext_data_2880 = b"\x00" * 2880
    p = tmp_path / "pcount_boundary.fits"
    p.write_bytes(prim_hdr + ext_hdr + ext_data_2880)

    with pytest.raises(InvalidFitsData, match="payload truncated"):
        extract_fits(p)


def test_reordered_mandatory_cards_fail(tmp_path):
    """Reordered mandatory cards (e.g. SIMPLE, NAXIS, BITPIX) must fail."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("BITPIX", 8),
    ]
    p = tmp_path / "reordered_primary.fits"
    p.write_bytes(_build_fits_header(cards))

    with pytest.raises(InvalidFitsData, match="must have 'BITPIX' card at card index 1"):
        extract_fits(p)


def test_extension_missing_or_reordering_pcount_gcount_fails(tmp_path):
    """Extension missing or reordering PCOUNT/GCOUNT must fail."""
    prim_cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("EXTEND", True),
    ]
    prim_hdr = _build_fits_header(prim_cards)

    # Case A: Extension missing PCOUNT (has GCOUNT at pos 3+naxis)
    ext_cards_missing_pcount = [
        _make_fits_card("XTENSION", "IMAGE"),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("GCOUNT", 1),
    ]
    p_a = tmp_path / "ext_missing_pcount.fits"
    p_a.write_bytes(prim_hdr + _build_fits_header(ext_cards_missing_pcount))
    with pytest.raises(InvalidFitsData, match="missing mandatory 'PCOUNT' card"):
        extract_fits(p_a)

    # Case B: Extension missing GCOUNT
    ext_cards_missing_gcount = [
        _make_fits_card("XTENSION", "IMAGE"),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("PCOUNT", 0),
    ]
    p_b = tmp_path / "ext_missing_gcount.fits"
    p_b.write_bytes(prim_hdr + _build_fits_header(ext_cards_missing_gcount))
    with pytest.raises(InvalidFitsData, match="missing mandatory 'GCOUNT' card"):
        extract_fits(p_b)

    # Case C: Extension reordering GCOUNT before PCOUNT
    ext_cards_reordered = [
        _make_fits_card("XTENSION", "IMAGE"),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("GCOUNT", 1),
        _make_fits_card("PCOUNT", 0),
    ]
    p_c = tmp_path / "ext_reordered.fits"
    p_c.write_bytes(prim_hdr + _build_fits_header(ext_cards_reordered))
    with pytest.raises(InvalidFitsData, match="missing mandatory 'PCOUNT' card"):
        extract_fits(p_c)


def test_duplicate_mandatory_card_fails(tmp_path):
    """Duplicate mandatory keyword in header must fail."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("BITPIX", 16),  # Duplicate BITPIX
    ]
    p = tmp_path / "dup_bitpix.fits"
    p.write_bytes(_build_fits_header(cards))

    with pytest.raises(InvalidFitsData, match="duplicate mandatory keyword 'BITPIX'"):
        extract_fits(p)


def test_non_space_and_0xff_after_end_fails(tmp_path):
    """0xff and non-space characters after END card in header block must fail."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
    ]
    clean_hdr = _build_fits_header(cards)

    # Case A: 0xff in padding bytes after END
    bad_hdr_ff = bytearray(clean_hdr)
    bad_hdr_ff[-1] = 0xFF
    p_ff = tmp_path / "bad_ff.fits"
    p_ff.write_bytes(bad_hdr_ff)

    with pytest.raises(InvalidFitsData, match="contains non-space byte after END card"):
        extract_fits(p_ff)

    # Case B: Non-space ASCII byte in END card itself
    bad_hdr_end = bytearray(clean_hdr)
    end_pos = bad_hdr_end.find(b"END")
    bad_hdr_end[end_pos + 8] = ord("X")
    p_end = tmp_path / "bad_end_card.fits"
    p_end.write_bytes(bad_hdr_end)

    with pytest.raises(InvalidFitsData, match="contains non-space byte in END card"):
        extract_fits(p_end)


# ---------------------------------------------------------------------------
# Regressions for Reviewer Feedback (Defects 1-5)
# ---------------------------------------------------------------------------

def test_scrub_error_message_never_reflects_secret_fragments_or_paths():
    """_scrub_error_message must never return str(exc), source header values, or private paths."""
    # Test 1: Exception containing secret header value bytes / string
    exc1 = ValueError("Unclosed card with raw b'SECRET_HEADER_VALUE' content")
    res1 = _scrub_error_message(exc1, "fits block parse")
    assert "SECRET_HEADER_VALUE" not in res1
    assert res1 == "fits block parse failed (ValueError)"

    # Test 2: Exception containing POSIX private path
    exc2 = OSError("Failed reading /home/private/patient/scan.fits")
    res2 = _scrub_error_message(exc2, "fits block parse")
    assert "/home/private/patient" not in res2
    assert "patient" not in res2
    assert res2 == "fits block parse failed (OSError)"

    # Test 3: Exception containing Windows private path
    exc3 = ValueError("Error in C:\\private\\patient\\medical.fits card")
    res3 = _scrub_error_message(exc3, "fits block parse")
    assert "C:\\private\\patient" not in res3
    assert "patient" not in res3
    assert res3 == "fits block parse failed (ValueError)"


def test_internal_defects_propagate_unconverted(tmp_path, monkeypatch):
    """Internal bugs such as RuntimeError, TypeError, and ZeroDivisionError must propagate unconverted."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
    ]
    p = tmp_path / "internal_defect.fits"
    p.write_bytes(_build_fits_header(cards))

    # 1. Injected RuntimeError propagates
    def raise_runtime(f, path, result, file_size):
        raise RuntimeError("injected internal extractor defect")

    monkeypatch.setattr(astronomy, "_parse_fits_stream", raise_runtime)
    with pytest.raises(RuntimeError, match="injected internal extractor defect"):
        extract_fits(p)

    # 2. Injected TypeError propagates
    def raise_type(f, path, result, file_size):
        raise TypeError("injected programmer TypeError defect")

    monkeypatch.setattr(astronomy, "_parse_fits_stream", raise_type)
    with pytest.raises(TypeError, match="injected programmer TypeError defect"):
        extract_fits(p)

    # 3. Injected ZeroDivisionError propagates
    def raise_zero_div(f, path, result, file_size):
        raise ZeroDivisionError("injected programmer ZeroDivisionError defect")

    monkeypatch.setattr(astronomy, "_parse_fits_stream", raise_zero_div)
    with pytest.raises(ZeroDivisionError, match="injected programmer ZeroDivisionError defect"):
        extract_fits(p)


def test_nonterminal_keyword_starting_with_end_parsed_correctly(tmp_path):
    """Keywords starting with END such as ENDING or ENDGAME must not terminate header prematurely."""
    cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("ENDING", "STAGE_1", "Nonterminal keyword starting with END"),
        _make_fits_card("ENDGAME", 42, "Another nonterminal keyword"),
        _make_fits_card("TELESCOP", "VLT", "Should still be parsed"),
    ]
    p = tmp_path / "endgame.fits"
    p.write_bytes(_build_fits_header(cards))

    extraction = extract_fits(p)
    u = extraction.units[0]
    assert u.meta["telescope"] == "VLT"


def test_cards_per_hdu_boundary_limit_fails_closed(tmp_path, monkeypatch):
    """_MAX_CARDS_PER_HDU limit fails closed on limit+1 non-END cards."""
    # Test with monkeypatched small limit of 3 cards
    monkeypatch.setattr(astronomy, "_MAX_CARDS_PER_HDU", 3)

    # 1. Exactly 3 cards + END -> Passes
    pass_cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
    ]
    p_pass = tmp_path / "limit_pass.fits"
    p_pass.write_bytes(_build_fits_header(pass_cards))
    ext_pass = extract_fits(p_pass)
    assert len(ext_pass.units) == 1

    # 2. 4 cards (limit + 1) + END -> Fails closed
    fail_cards = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("COMMENT", "Extra non-end card exceeding limit"),
    ]
    p_fail = tmp_path / "limit_fail.fits"
    p_fail.write_bytes(_build_fits_header(fail_cards))
    with pytest.raises(InvalidFitsData, match="header card count exceeded limit"):
        extract_fits(p_fail)


def test_deterministic_same_size_mutation_during_extraction_fails(tmp_path, monkeypatch):
    """In-place inode rewrite to same size with restored mtime during extraction fails via rehash check."""
    cards1 = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("TELESCOP", "TELESCOPE_A"),
    ]
    cards2 = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("TELESCOP", "TELESCOPE_B"),
    ]
    hdr1 = _build_fits_header(cards1)
    hdr2 = _build_fits_header(cards2)
    assert len(hdr1) == len(hdr2) == 2880

    p = tmp_path / "mutation_target.fits"
    p.write_bytes(hdr1)
    st = p.stat()
    orig_ns = (st.st_atime_ns, st.st_mtime_ns)

    real_parse = astronomy._parse_fits_stream

    def mutating_parse(f, path, result, file_size):
        # Mutate the file in-place on the same open inode
        with path.open("r+b") as mut_f:
            mut_f.seek(0)
            mut_f.write(hdr2)
            mut_f.flush()
        # Restore exact nanosecond timestamps
        os.utime(path, ns=orig_ns)
        return real_parse(f, path, result, file_size)

    monkeypatch.setattr(astronomy, "_parse_fits_stream", mutating_parse)

    with pytest.raises(InvalidFitsData, match="source changed while it was being extracted"):
        extract_fits(p)


def test_deterministic_pathname_replacement_during_extraction_fails(tmp_path, monkeypatch):
    """Pathname replacement with os.replace during extraction fails via pathname consistency check."""
    cards1 = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("TELESCOP", "TELESCOPE_A"),
    ]
    cards2 = [
        _make_fits_card("SIMPLE", True),
        _make_fits_card("BITPIX", 8),
        _make_fits_card("NAXIS", 0),
        _make_fits_card("TELESCOP", "TELESCOPE_B"),
    ]
    hdr1 = _build_fits_header(cards1)
    hdr2 = _build_fits_header(cards2)

    p = tmp_path / "replace_target.fits"
    p.write_bytes(hdr1)
    st = p.stat()
    orig_ns = (st.st_atime_ns, st.st_mtime_ns)

    other = tmp_path / "other.fits"
    other.write_bytes(hdr2)
    os.utime(other, ns=orig_ns)

    real_parse = astronomy._parse_fits_stream

    def replacing_parse(f, path, result, file_size):
        os.replace(other, path)
        return real_parse(f, path, result, file_size)

    monkeypatch.setattr(astronomy, "_parse_fits_stream", replacing_parse)

    with pytest.raises(InvalidFitsData, match="source changed while it was being extracted"):
        extract_fits(p)
