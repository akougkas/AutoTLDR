"""Tests for native bounded metadata extraction of NumPy NPY (v1-v3) and NPZ archives.

Primary authoritative specifications:
- NumPy NPY Format Specification (Versions 1.0, 2.0, 3.0):
  https://numpy.org/doc/stable/reference/generated/numpy.lib.format.html
- NumPy Array Interface Specification:
  https://numpy.org/doc/stable/reference/arrays.interface.html
- PKWARE .ZIP Application Note Specification:
  https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT
"""

from __future__ import annotations

import io
import os
import struct
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch
import pytest

from autotldr.unit import Modality, RelationKind, Role
from autotldr.extract import scientific_arrays
from autotldr.extract.scientific_arrays import (
    extract_scientific_array,
    extract_npy,
    extract_npz,
    detect_scientific_array_kind,
    InvalidScientificArrayData,
)


def _make_npy_bytes(
    shape: tuple[int, ...],
    descr: object,
    fortran_order: bool = False,
    version: tuple[int, int] = (1, 0),
    payload: bytes = b"",
    pad_char: str = " ",
    terminate_newline: bool = True,
    extra_keys: dict | None = None,
    raw_dict_str: str | None = None,
) -> bytes:
    """Helper to synthesize byte-exact NPY files with controlled header properties."""
    v_maj, v_min = version
    prefix_len = 10 if v_maj == 1 else 12

    if raw_dict_str is not None:
        dict_str = raw_dict_str
    else:
        d = {"descr": descr, "fortran_order": fortran_order, "shape": shape}
        if extra_keys:
            d.update(extra_keys)
        dict_str = repr(d)

    # Calculate padding to ensure (prefix_len + hlen) % 64 == 0
    current = prefix_len + len(dict_str) + (1 if terminate_newline else 0)
    pad_len = (64 - (current % 64)) % 64

    header_str = dict_str + (pad_char * pad_len)
    if terminate_newline:
        header_str += "\n"

    encoding = "utf-8" if v_maj == 3 else "latin1"
    header_bytes = header_str.encode(encoding)
    hlen = len(header_bytes)

    if v_maj == 1:
        prefix = b"\x93NUMPY\x01" + bytes([v_min]) + struct.pack("<H", hlen)
    else:
        prefix = b"\x93NUMPY" + bytes([v_maj, v_min]) + struct.pack("<I", hlen)

    return prefix + header_bytes + payload


# ---------------------------------------------------------------------------
# Test Lazy Imports
# ---------------------------------------------------------------------------
def test_lazy_imports():
    """Importing scientific_arrays must not import numpy at module level."""
    import subprocess
    cmd = [
        sys.executable,
        "-c",
        "import sys; import autotldr.extract.scientific_arrays; assert 'numpy' not in sys.modules, 'numpy was eagerly imported!'",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Lazy import failed: {res.stderr}"


# ---------------------------------------------------------------------------
# Positive Coverage: Real library-produced NPY and NPZ fixtures
# ---------------------------------------------------------------------------
def test_extract_npy_real_fixtures(tmp_path):
    """Positive tests using real library-produced np.save fixtures."""
    import numpy as np

    # 1. 2D Float64 array
    p1 = tmp_path / "real_f8.npy"
    arr1 = np.ones((100, 20), dtype="<f8")
    np.save(p1, arr1)

    assert detect_scientific_array_kind(p1) == "npy"
    ext1 = extract_npy(p1)
    assert ext1.kind == "npy"
    u1 = ext1.units[0]
    assert u1.meta["shape"] == [100, 20]
    assert u1.meta["ndim"] == 2
    assert u1.meta["dtype"] == "<f8"
    assert u1.meta["fortran_order"] is False
    assert u1.role == Role.UNKNOWN
    assert u1.origin.ref == "array"

    # 2. 1D Int32 Fortran-ordered array
    p2 = tmp_path / "real_fortran.npy"
    arr2 = np.asfortranarray(np.arange(500, dtype="<i4").reshape(25, 20))
    np.save(p2, arr2)
    ext2 = extract_npy(p2)
    u2 = ext2.units[0]
    assert u2.meta["shape"] == [25, 20]
    assert u2.meta["fortran_order"] is True
    assert u2.meta["dtype"] == "<i4"
    assert u2.role == Role.UNKNOWN

    # 3. 3D Uint8 array
    p3 = tmp_path / "real_u1.npy"
    arr3 = np.zeros((10, 10, 10), dtype="|u1")
    np.save(p3, arr3)
    ext3 = extract_npy(p3)
    u3 = ext3.units[0]
    assert u3.meta["shape"] == [10, 10, 10]
    assert u3.meta["dtype"] == "|u1"
    assert u3.role == Role.UNKNOWN

    # 4. Scalar (0D) array
    p4 = tmp_path / "real_scalar.npy"
    arr4 = np.array(42.5, dtype="<f8")
    np.save(p4, arr4)
    ext4 = extract_npy(p4)
    u4 = ext4.units[0]
    assert u4.meta["shape"] == []
    assert u4.meta["ndim"] == 0
    assert u4.meta["dtype"] == "<f8"

    # 5. Zero-length array
    p5 = tmp_path / "real_zero_length.npy"
    arr5 = np.zeros((0, 50), dtype="<f4")
    np.save(p5, arr5)
    ext5 = extract_npy(p5)
    u5 = ext5.units[0]
    assert u5.meta["shape"] == [0, 50]
    assert u5.meta["ndim"] == 2


def test_extract_npz_real_fixtures(tmp_path):
    """Positive test for NPZ archive using real np.savez."""
    import numpy as np

    p = tmp_path / "bundle.npz"
    np.savez(p, arr_a=np.zeros((20, 30), dtype="<f8"), arr_b=np.ones(100, dtype="<i2"))

    assert detect_scientific_array_kind(p) == "npz"
    extraction = extract_npz(p)
    assert extraction.kind == "npz"

    archive_units = [u for u in extraction.units if u.origin.ref == "archive"]
    assert len(archive_units) == 1
    assert archive_units[0].meta["member_count"] == 2
    assert archive_units[0].role == Role.UNKNOWN

    member_units = [u for u in extraction.units if u.origin.ref.startswith("member:")]
    assert len(member_units) == 2
    member_names = [u.meta["name"] for u in member_units]
    assert set(member_names) == {"arr_a.npy", "arr_b.npy"}
    for u in member_units:
        assert u.role == Role.UNKNOWN
        assert u.modality == Modality.SCHEMA


# ---------------------------------------------------------------------------
# Defect 1: Exact NPY Grammar & Dtype Validation
# ---------------------------------------------------------------------------
def test_npy_valid_versions_v1_v2_v3(tmp_path):
    """Valid headers for v1.0, v2.0, and v3.0 must parse cleanly."""
    for ver in ((1, 0), (2, 0), (3, 0)):
        payload = b"\x00" * 80  # 10 * 8 bytes
        data = _make_npy_bytes((10,), "<f8", fortran_order=False, version=ver, payload=payload)
        p = tmp_path / f"valid_v{ver[0]}_{ver[1]}.npy"
        p.write_bytes(data)

        ext = extract_npy(p)
        assert ext.units[0].meta["version"] == f"{ver[0]}.{ver[1]}"
        assert ext.units[0].meta["shape"] == [10]


def test_npy_reject_unknown_minor_or_major_version(tmp_path):
    """Unknown minor version (e.g. 1.1, 2.1) or major version (e.g. 4.0) must be rejected."""
    for bad_ver in ((1, 1), (2, 1), (3, 1), (4, 0), (0, 9)):
        data = _make_npy_bytes((5,), "<i4", version=bad_ver, payload=b"\x00" * 20)
        p = tmp_path / "bad_ver.npy"
        p.write_bytes(data)
        with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
            extract_npy(p)


def test_npy_reject_missing_newline(tmp_path):
    """Header not terminating in newline must be rejected."""
    data = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20, terminate_newline=False)
    p = tmp_path / "no_newline.npy"
    p.write_bytes(data)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p)


def test_npy_reject_non_space_padding(tmp_path):
    """Padding characters other than ASCII space (e.g. tabs, nulls, chars) must be rejected."""
    for bad_pad in ("\t", "\x00", "\r", "X"):
        data = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20, pad_char=bad_pad)
        p = tmp_path / "bad_pad.npy"
        p.write_bytes(data)
        with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
            extract_npy(p)


def test_npy_reject_misalignment(tmp_path):
    """Header length where prefix+header is not divisible by 64 must be rejected."""
    dict_str = "{'descr': '<i4', 'fortran_order': False, 'shape': (5,)}\n"
    header_bytes = dict_str.encode("latin1")
    hlen = len(header_bytes)
    prefix = b"\x93NUMPY\x01\x00" + struct.pack("<H", hlen)
    data = prefix + header_bytes + b"\x00" * 20

    p = tmp_path / "misaligned.npy"
    p.write_bytes(data)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p)


def test_npy_reject_extra_and_missing_keys(tmp_path):
    """Extra keys or missing required keys in the header dictionary must be rejected."""
    # Extra key
    data_extra = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20, extra_keys={"extra": 123})
    p_extra = tmp_path / "extra_key.npy"
    p_extra.write_bytes(data_extra)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p_extra)

    # Missing key
    raw_missing = "{'descr': '<i4', 'shape': (5,)}"
    data_missing = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20, raw_dict_str=raw_missing)
    p_missing = tmp_path / "missing_key.npy"
    p_missing.write_bytes(data_missing)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p_missing)


def test_npy_reject_list_shape_and_invalid_dims(tmp_path):
    """Shape must be a tuple, not a list; dimensions must be non-bool nonnegative ints."""
    # List shape
    raw_list = "{'descr': '<i4', 'fortran_order': False, 'shape': [5, 10]}"
    data_list = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 200, raw_dict_str=raw_list)
    p_list = tmp_path / "list_shape.npy"
    p_list.write_bytes(data_list)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p_list)

    # Bool in shape
    raw_bool = "{'descr': '<i4', 'fortran_order': False, 'shape': (True, 5)}"
    data_bool = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20, raw_dict_str=raw_bool)
    p_bool = tmp_path / "bool_shape.npy"
    p_bool.write_bytes(data_bool)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p_bool)

    # Negative dim
    raw_neg = "{'descr': '<i4', 'fortran_order': False, 'shape': (-5, 2)}"
    data_neg = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20, raw_dict_str=raw_neg)
    p_neg = tmp_path / "neg_shape.npy"
    p_neg.write_bytes(data_neg)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p_neg)


def test_npy_reject_non_bool_fortran_order(tmp_path):
    """fortran_order must be strictly bool."""
    raw_int = "{'descr': '<i4', 'fortran_order': 1, 'shape': (5,)}"
    data_int = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20, raw_dict_str=raw_int)
    p_int = tmp_path / "int_fortran.npy"
    p_int.write_bytes(data_int)
    with pytest.raises(InvalidScientificArrayData, match="npy header parse failed"):
        extract_npy(p_int)


def test_dtype_reject_duplicate_field_names(tmp_path):
    """NumPy rejects duplicate field names; AutoTLDR must not rewrite evidence and must decline stably."""
    data = _make_npy_bytes((10,), [("x", "<i4"), ("x", "<f8")], payload=b"\x00" * (10 * 12))
    p = tmp_path / "dup_fields.npy"
    p.write_bytes(data)

    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p)


def test_dtype_titled_fields_positive(tmp_path):
    """Titled structured fields (title, name) from real np.save must parse with title in metadata and name in origin."""
    import numpy as np

    dt = np.dtype([(("Temperature in Celsius", "temp"), "<f8"), (("Sensor ID", "sensor_id"), "<i4")])
    arr = np.zeros(20, dtype=dt)
    p = tmp_path / "titled.npy"
    np.save(p, arr)

    extraction = extract_npy(p)
    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 2

    # Field 0
    f0 = field_units[0]
    assert f0.origin.ref == "field:0:temp"
    assert f0.meta["name"] == "temp"
    assert f0.meta["title"] == "Temperature in Celsius"
    assert f0.meta["index"] == 0
    assert f0.role == Role.UNKNOWN

    # Field 1
    f1 = field_units[1]
    assert f1.origin.ref == "field:1:sensor_id"
    assert f1.meta["name"] == "sensor_id"
    assert f1.meta["title"] == "Sensor ID"
    assert f1.meta["index"] == 1


def test_dtype_reject_empty_field_name(tmp_path):
    """Empty raw structured field name must be rejected to prevent misstating NumPy field identities."""
    # 1. Direct empty name
    data_empty_name = _make_npy_bytes((1,), [("", "<i4")], payload=b"\x00" * 4)
    p_empty = tmp_path / "empty_name.npy"
    p_empty.write_bytes(data_empty_name)
    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p_empty)

    # 2. Titled pair with empty name
    data_empty_titled_name = _make_npy_bytes((1,), [(("Title", ""), "<i4")], payload=b"\x00" * 4)
    p_empty_titled = tmp_path / "empty_titled_name.npy"
    p_empty_titled.write_bytes(data_empty_titled_name)
    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p_empty_titled)

    # 3. Empty title with valid name is accepted
    data_empty_title = _make_npy_bytes((1,), [(("", "valid_name"), "<i4")], payload=b"\x00" * 4)
    p_empty_title = tmp_path / "empty_title.npy"
    p_empty_title.write_bytes(data_empty_title)
    ext = extract_npy(p_empty_title)
    assert ext.units[1].meta["name"] == "valid_name"


def test_dtype_structured_preflight_rejections(tmp_path):
    """Preflight rejects malformed field entry lengths, bad field identities, and out-of-bounds resources."""
    # Field tuple with 1 element (< 2)
    data_1elem = _make_npy_bytes((1,), [("field_only",)], payload=b"\x00" * 4)
    p_1elem = tmp_path / "1elem.npy"
    p_1elem.write_bytes(data_1elem)
    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p_1elem)

    # Field tuple with 4 elements (> 3)
    data_4elem = _make_npy_bytes((1,), [("name", "i4", (2,), "extra")], payload=b"\x00" * 8)
    p_4elem = tmp_path / "4elem.npy"
    p_4elem.write_bytes(data_4elem)
    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p_4elem)

    # Field identity with non-string (e.g. 123)
    data_bad_id = _make_npy_bytes((1,), [(123, "i4")], payload=b"\x00" * 4)
    p_bad_id = tmp_path / "bad_id.npy"
    p_bad_id.write_bytes(data_bad_id)
    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p_bad_id)

    # Excessive nesting depth (> 8)
    deep_descr: object = "i4"
    for i in range(12):
        deep_descr = [(f"f_{i}", deep_descr)]
    data_deep = _make_npy_bytes((1,), deep_descr, payload=b"\x00" * 4)
    p_deep = tmp_path / "deep_dtype.npy"
    p_deep.write_bytes(data_deep)
    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p_deep)

    # Excessive field count (> 512)
    many_fields = [(f"f_{i}", "i4") for i in range(600)]
    data_many = _make_npy_bytes((1,), many_fields, payload=b"\x00" * 2400)
    p_many = tmp_path / "many_fields.npy"
    p_many.write_bytes(data_many)
    with pytest.raises(InvalidScientificArrayData, match="dtype validation failed"):
        extract_npy(p_many)


# ---------------------------------------------------------------------------
# Defect 2: Object Arrays Honest Integrity Statement & Non-Empty Payload
# ---------------------------------------------------------------------------
def test_object_dtype_honest_gap_and_canary_protection(tmp_path):
    """Object arrays require at least 1 payload byte, emit honest gap, never leak canaries."""
    import numpy as np

    canary = "SECRET_CANARY_VALUE_XYZ_987"

    # 1. Real non-empty object array
    p_real = tmp_path / "real_obj.npy"
    arr_real = np.array([{"key": canary}, 12345], dtype=object)
    np.save(p_real, arr_real, allow_pickle=True)

    ext_real = extract_npy(p_real)
    expected_gap_text = (
        "Object dtype ('|O') contains pickled Python objects; "
        "payload inspection was refused and inner pickle integrity/extent was not validated"
    )
    assert any(expected_gap_text == str(g) for g in ext_real.gaps)

    for u in ext_real.units:
        assert canary not in u.content
        assert canary not in str(u.meta)
    for g in ext_real.gaps:
        assert canary not in str(g)
    for r in ext_real.relations:
        assert canary not in r.evidence

    # 2. Real zero-length object array (has pickle payload written by numpy)
    p_zero = tmp_path / "real_zero_obj.npy"
    arr_zero = np.empty((0, 5), dtype=object)
    np.save(p_zero, arr_zero, allow_pickle=True)
    ext_zero = extract_npy(p_zero)
    assert any(expected_gap_text == str(g) for g in ext_zero.gaps)
    assert ext_zero.units[0].meta["shape"] == [0, 5]

    # 3. Hand-built object array with 0 payload bytes must decline as truncated
    data_0_payload = _make_npy_bytes((0,), "|O", payload=b"")
    p_0_payload = tmp_path / "zero_payload_obj.npy"
    p_0_payload.write_bytes(data_0_payload)
    with pytest.raises(InvalidScientificArrayData, match="payload validation failed"):
        extract_npy(p_0_payload)


# ---------------------------------------------------------------------------
# Defect 3: NPZ Member Envelope Draining & Late CRC Verification
# ---------------------------------------------------------------------------
def test_npz_late_payload_bitflip_fails_archive_crc(tmp_path):
    """A bitflip in stored member payload beyond the NPY header must fail whole NPZ on CRC check."""
    import numpy as np

    p = tmp_path / "corrupt_payload.npz"
    # Create valid ZIP_STORED npz archive with 10,000 float64 elements (80 KB payload)
    arr = np.arange(10000, dtype="<f8")
    np.savez(p, big_array=arr)

    raw_bytes = bytearray(p.read_bytes())
    # Locate member local data and flip a byte deep in the payload (far past the NPY header)
    header_magic_offset = raw_bytes.find(b"\x93NUMPY")
    assert header_magic_offset > 0
    flip_offset = header_magic_offset + 500  # Deep inside the array data
    raw_bytes[flip_offset] ^= 0xFF
    p.write_bytes(raw_bytes)

    with pytest.raises(InvalidScientificArrayData, match="npz member read failed"):
        extract_npz(p)


def test_npz_intact_envelope_invalid_npy_member_gap(tmp_path):
    """An intact ZIP member whose inner NPY bytes are corrupt produces an addressable gap without failing archive."""
    valid_npy = _make_npy_bytes((5,), "<i4", payload=b"\x00" * 20)
    corrupt_npy_header = b"\x93NUMPY\x01\x00INVALID_HEADER_DATA"

    p = tmp_path / "gap_test.npz"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("valid.npy", valid_npy)
        zf.writestr("corrupt.npy", corrupt_npy_header)

    ext = extract_npz(p)
    assert any(u.origin.ref == "member:valid.npy" for u in ext.units)
    gaps = [g for g in ext.gaps if g.origin.ref == "member:corrupt.npy"]
    assert len(gaps) == 1
    assert "Member 'corrupt.npy' is not a valid NPY array" in str(gaps[0])
    assert "INVALID_HEADER_DATA" not in str(gaps[0])


# ---------------------------------------------------------------------------
# Defect 4: Complete ZIP Security Matrix & Raw-NUL Branch Proof
# ---------------------------------------------------------------------------
def test_npz_raw_nul_guard_proven():
    """Verify that orig_filename with a raw NUL is caught by the NUL guard, not earlier suffix checks."""
    # 1. String orig_filename with raw NUL where normalized filename is a valid .npy
    z_str = zipfile.ZipInfo("safe.npy")
    z_str.orig_filename = "safe.npy\x00hidden"
    with pytest.raises(ValueError, match="NUL byte in orig_filename"):
        scientific_arrays._validate_npz_zipinfo(z_str, set())

    # 2. Bytes orig_filename with raw NUL where normalized filename is a valid .npy
    z_bytes = zipfile.ZipInfo("safe.npy")
    z_bytes.orig_filename = b"safe.npy\x00hidden"
    with pytest.raises(ValueError, match="NUL byte in orig_filename"):
        scientific_arrays._validate_npz_zipinfo(z_bytes, set())


def test_npz_member_name_length_bound_exact():
    """Verify member-name bound exactly at 255 chars (accepted) and 256 chars (rejected)."""
    # 255 chars (251 'a's + '.npy') -> must be accepted
    name_255 = ("a" * 251) + ".npy"
    assert len(name_255) == 255
    z_255 = zipfile.ZipInfo(name_255)
    scientific_arrays._validate_npz_zipinfo(z_255, set())

    # 256 chars (252 'a's + '.npy') -> must be rejected with exact bound error
    name_256 = ("a" * 252) + ".npy"
    assert len(name_256) == 256
    z_256 = zipfile.ZipInfo(name_256)
    with pytest.raises(ValueError, match="member name length 256 exceeds limit 255"):
        scientific_arrays._validate_npz_zipinfo(z_256, set())


def test_npz_security_matrix_guards():
    """Complete advertised ZIP security matrix guards tested deterministically."""
    # 1. orig_filename disagrees with filename
    z1 = zipfile.ZipInfo("normalized.npy")
    z1.orig_filename = "different.npy"
    with pytest.raises(ValueError, match="disagrees with normalized filename"):
        scientific_arrays._validate_npz_zipinfo(z1, set())

    # 2. Encrypted flag
    z2 = zipfile.ZipInfo("arr.npy")
    z2.flag_bits = 0x1
    with pytest.raises(ValueError, match="encrypted member"):
        scientific_arrays._validate_npz_zipinfo(z2, set())

    # 3. Zero-compressed non-empty bomb
    z3 = zipfile.ZipInfo("bomb.npy")
    z3.file_size = 1000
    z3.compress_size = 0
    with pytest.raises(ValueError, match="zero-compressed"):
        scientific_arrays._validate_npz_zipinfo(z3, set())

    # 4. Decompression ratio over threshold
    z4 = zipfile.ZipInfo("ratio.npy")
    z4.file_size = 10 * 1024 * 1024
    z4.compress_size = 1024
    with pytest.raises(ValueError, match="suspicious decompression ratio"):
        scientific_arrays._validate_npz_zipinfo(z4, set())

    # 5. Unsupported compression method
    z5 = zipfile.ZipInfo("bzip.npy")
    z5.compress_type = 12
    with pytest.raises(ValueError, match="unsupported compression method"):
        scientific_arrays._validate_npz_zipinfo(z5, set())

    # 6. Absolute POSIX path
    z6 = zipfile.ZipInfo("/etc/passwd.npy")
    with pytest.raises(ValueError, match="absolute path"):
        scientific_arrays._validate_npz_zipinfo(z6, set())

    # 7. Special files (char device, FIFO, socket)
    for mode_type in (0o020000, 0o010000, 0o140000):  # S_IFCHR, S_IFIFO, S_IFSOCK
        z7 = zipfile.ZipInfo("special.npy")
        z7.external_attr = mode_type << 16
        with pytest.raises(ValueError, match="special file"):
            scientific_arrays._validate_npz_zipinfo(z7, set())

    # 8. Symlink
    z8 = zipfile.ZipInfo("link.npy")
    z8.external_attr = 0o120777 << 16
    with pytest.raises(ValueError, match="symlink member"):
        scientific_arrays._validate_npz_zipinfo(z8, set())

    # 9. Duplicate names
    seen = set()
    z9_a = zipfile.ZipInfo("dup.npy")
    scientific_arrays._validate_npz_zipinfo(z9_a, seen)
    assert "dup.npy" in seen
    with pytest.raises(ValueError, match="duplicate archive member"):
        scientific_arrays._validate_npz_zipinfo(z9_a, seen)

    # 10. Raw-unsafe member names (backslash, traversal, double-slash, dot, drive, directory, non-.npy)
    for bad_name in ("sub\\a.npy", "../a.npy", "a/../b.npy", "a//b.npy", "a/./b.npy", "C:a.npy", "dir/", "a.txt"):
        z_bad = zipfile.ZipInfo(bad_name)
        with pytest.raises(ValueError):
            scientific_arrays._validate_npz_zipinfo(z_bad, set())


def test_extract_npz_security_guard_integration(tmp_path):
    """Integration test: extract_npz fails archive when envelope contains unsafe member zipinfo."""
    p = tmp_path / "valid.npz"
    valid_npy = _make_npy_bytes((2,), "<i4", payload=b"\x00" * 8)
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("ok.npy", valid_npy)

    bad_zinfo = zipfile.ZipInfo("enc.npy")
    bad_zinfo.flag_bits = 0x1

    with patch("zipfile.ZipFile.infolist", return_value=[bad_zinfo]):
        with pytest.raises(InvalidScientificArrayData, match="npz member security check failed"):
            extract_npz(p)


# ---------------------------------------------------------------------------
# Detect-Time Open Race & Privacy
# ---------------------------------------------------------------------------
def test_detect_open_race_privacy(tmp_path):
    """A failing open during detection must emit a safe typed decline without leaking parent directory."""
    secret_dir = tmp_path / "classified_subfolder_77"
    secret_dir.mkdir()
    p = secret_dir / "race.npy"
    p.write_bytes(b"\x93NUMPY\x01\x00\x00\x00\x00\x00")

    def failing_open(*args, **kwargs):
        raise PermissionError(f"[Errno 13] Permission denied: '{p}'")

    with patch("pathlib.Path.open", side_effect=failing_open):
        with pytest.raises(InvalidScientificArrayData) as exc_info:
            detect_scientific_array_kind(p)

    msg = str(exc_info.value)
    assert "classified_subfolder_77" not in msg
    assert "race.npy:" in msg
    assert "file open failed (PermissionError)" in msg


# ---------------------------------------------------------------------------
# Defect 5: Hash Binding Independent of Mtime
# ---------------------------------------------------------------------------
def test_same_size_mutation_with_restored_mtime_rejected(tmp_path):
    """In-place same-size mutation with nanosecond-exact mtime restoration must be rejected by SHA-256."""
    valid_data = _make_npy_bytes((10,), "<f8", payload=b"\x00" * 80)
    p = tmp_path / "tamper_mtime.npy"
    p.write_bytes(valid_data)

    orig_stat = p.stat()
    orig_atime_ns = orig_stat.st_atime_ns
    orig_mtime_ns = orig_stat.st_mtime_ns

    original_parse = scientific_arrays._parse_npy_header_bytes

    def tampered_parse_restore_mtime(stream, max_read=64 * 1024):
        # Tamper with file in-place, keeping exact same size (80 bytes payload of 0xFF instead of 0x00)
        tampered_data = _make_npy_bytes((10,), "<f8", payload=b"\xFF" * 80)
        with open(p, "r+b") as f_tamper:
            f_tamper.write(tampered_data)

        # Restore nanosecond mtime and atime
        os.utime(p, ns=(orig_atime_ns, orig_mtime_ns))
        # Verify mtime is restored
        assert p.stat().st_mtime_ns == orig_mtime_ns

        return original_parse(stream, max_read)

    with patch("autotldr.extract.scientific_arrays._parse_npy_header_bytes", side_effect=tampered_parse_restore_mtime):
        with pytest.raises(InvalidScientificArrayData, match="source changed while it was being extracted"):
            extract_npy(p)


def test_pathname_replacement_rejected(tmp_path):
    """Replacing the file on disk during extraction must be rejected."""
    valid_data = _make_npy_bytes((10,), "<f8", payload=b"\x00" * 80)
    p = tmp_path / "replace.npy"
    p.write_bytes(valid_data)

    original_parse = scientific_arrays._parse_npy_header_bytes

    def replaced_parse(stream, max_read=64 * 1024):
        p.unlink()
        p.write_bytes(valid_data)
        return original_parse(stream, max_read)

    with patch("autotldr.extract.scientific_arrays._parse_npy_header_bytes", side_effect=replaced_parse):
        with pytest.raises(InvalidScientificArrayData, match="source pathname replaced while it was being extracted"):
            extract_npy(p)


# ---------------------------------------------------------------------------
# Defect 6: Error Privacy & Exception Discipline
# ---------------------------------------------------------------------------
def test_error_message_privacy(tmp_path):
    """Error messages must not reflect private directory paths, header reprs, or binary buffers."""
    secret_dir = tmp_path / "top_secret_directory_name"
    secret_dir.mkdir()
    corrupt = secret_dir / "corrupt.npy"
    corrupt.write_bytes(b"\x93NUMPY\x01\x00\x00\x00\x00\x00")

    with pytest.raises(InvalidScientificArrayData) as exc_info:
        extract_npy(corrupt)

    msg = str(exc_info.value)
    assert "top_secret_directory_name" not in msg
    assert "corrupt.npy:" in msg
    assert "b'\\x93NUMPY" not in msg


def test_programmer_exceptions_propagate(tmp_path):
    """Injected fatal/programmer exceptions must propagate uninhibited."""
    valid_data = _make_npy_bytes((5,), "<f8", payload=b"\x00" * 40)
    p = tmp_path / "propagate.npy"
    p.write_bytes(valid_data)

    for exc_cls in (RuntimeError, TypeError, AssertionError, ZeroDivisionError, MemoryError):
        with patch("autotldr.extract.scientific_arrays._parse_npy_header_bytes", side_effect=exc_cls("injected")):
            with pytest.raises(exc_cls):
                extract_npy(p)


# ---------------------------------------------------------------------------
# Invariants: Addressability and Role.UNKNOWN
# ---------------------------------------------------------------------------
def test_role_invariants_and_addressability(tmp_path):
    """All emitted units and gaps must have Role.UNKNOWN and exact addressable origins."""
    import numpy as np

    p = tmp_path / "invariants.npz"
    dt = np.dtype([("a", "<i4"), ("b", "<f8", (2,))])
    np.savez(p, arr1=np.zeros(10, dtype=dt), arr2=np.ones((5, 5), dtype="<f4"))

    ext = extract_scientific_array(p)
    assert ext.kind == "npz"

    for u in ext.units:
        assert u.role == Role.UNKNOWN
        assert u.origin.source == str(p)
        assert u.origin.ref

    for r in ext.relations:
        assert r.confidence == 1.0

    for g in ext.gaps:
        assert g.origin.source == str(p)
        assert g.origin.ref
