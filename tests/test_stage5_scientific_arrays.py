"""Tests for native bounded metadata extraction of NumPy NPY (v1-v3) and NPZ archives.

Primary authoritative specifications:
- NumPy NPY Format Specification (Versions 1.0, 2.0, 3.0):
  https://numpy.org/doc/stable/reference/generated/numpy.lib.format.html
- NumPy Array Interface Specification:
  https://numpy.org/doc/stable/reference/arrays.interface.html
- PKWARE .ZIP Application Note Specification:
  https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT
"""

import io
import struct
import sys
import tempfile
import zipfile
from pathlib import Path
import pytest

from autotldr.unit import Modality, RelationKind, Role
from autotldr.extract.scientific_arrays import (
    extract_scientific_array,
    extract_npy,
    extract_npz,
    detect_scientific_array_kind,
    InvalidScientificArrayData,
)


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

    # 2. 1D Int32 Fortran-ordered array
    p2 = tmp_path / "real_fortran.npy"
    arr2 = np.asfortranarray(np.arange(500, dtype="<i4").reshape(25, 20))
    np.save(p2, arr2)
    ext2 = extract_npy(p2)
    u2 = ext2.units[0]
    assert u2.meta["shape"] == [25, 20]
    assert u2.meta["fortran_order"] is True
    assert u2.meta["dtype"] == "<i4"

    # 3. 3D Uint8 array
    p3 = tmp_path / "real_u1.npy"
    arr3 = np.zeros((10, 10, 10), dtype="|u1")
    np.save(p3, arr3)
    ext3 = extract_npy(p3)
    u3 = ext3.units[0]
    assert u3.meta["shape"] == [10, 10, 10]
    assert u3.meta["dtype"] == "|u1"


def test_extract_npy_structured_dtype(tmp_path):
    """Positive test for structured dtype NPY array."""
    import numpy as np

    dt = np.dtype([("time", "<f8"), ("pos", "<f4", (3,)), ("flags", "|u1")])
    arr = np.zeros(50, dtype=dt)
    p = tmp_path / "test_struct.npy"
    np.save(p, arr)

    extraction = extract_npy(p)
    array_unit = [u for u in extraction.units if u.origin.ref == "array"][0]

    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 3
    field_names = [u.meta["name"] for u in field_units]
    assert set(field_names) == {"time", "pos", "flags"}

    for f in field_units:
        assert any(
            r.src == array_unit.id and r.dst == f.id and r.kind == RelationKind.DESCRIBES
            for r in extraction.relations
        )


def test_payload_extent_validation_truncation_rejected(tmp_path):
    """Truncated array payload must decline with a typed error."""
    import struct
    d = {"descr": "<f8", "fortran_order": False, "shape": (100, 200)}
    hdr_str = str(d) + "\n"
    pad = 16 - ((10 + len(hdr_str)) % 16)
    hdr_str = str(d) + " " * pad + "\n"
    hdr_bytes = hdr_str.encode("latin1")
    raw_header = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(hdr_bytes)) + hdr_bytes

    p = tmp_path / "truncated_payload.npy"
    # Declares 100*200*8 = 160,000 bytes, but only provides 100 bytes
    p.write_bytes(raw_header + b"\x00" * 100)

    with pytest.raises(InvalidScientificArrayData, match="truncated array payload"):
        extract_npy(p)


def test_nested_object_dtype_safety_gap(tmp_path):
    """Nested structured object dtype must be detected and emit a safety gap without unpickling."""
    import numpy as np

    dt = np.dtype([("id", "<i4"), ("meta", [("obj", "O")])])
    arr = np.empty(5, dtype=dt)
    p = tmp_path / "nested_obj.npy"
    # np.save with allow_pickle=True to write fixture
    np.save(p, arr, allow_pickle=True)

    extraction = extract_npy(p)
    assert any("pickled Python objects" in str(g) for g in extraction.gaps)


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

    member_units = [u for u in extraction.units if u.origin.ref.startswith("member:")]
    assert len(member_units) == 2
    member_names = [u.meta["name"] for u in member_units]
    assert set(member_names) == {"arr_a.npy", "arr_b.npy"}


def test_fail_closed_unknown_bytes_and_spoofed_suffix(tmp_path):
    """Unknown bytes and spoofed suffixes must fail closed."""
    p_fake_npy = tmp_path / "fake.npy"
    p_fake_npy.write_bytes(b"NOT_A_NUMPY_FILE_AT_ALL_1234567890")

    with pytest.raises(InvalidScientificArrayData, match="failed closed"):
        detect_scientific_array_kind(p_fake_npy)

    p_fake_npz = tmp_path / "fake.npz"
    p_fake_npz.write_bytes(b"JUST_RANDOM_GARBAGE_NOT_A_ZIP")

    with pytest.raises(InvalidScientificArrayData, match="failed closed"):
        detect_scientific_array_kind(p_fake_npz)


def test_npz_security_guards(tmp_path):
    """Test all NPZ security guards: path traversal, directory member, duplicate, backslash."""
    import struct
    d = {"descr": "<f4", "fortran_order": False, "shape": (5,)}
    hdr_str = str(d) + "\n"
    pad = 16 - ((10 + len(hdr_str)) % 16)
    hdr_str = str(d) + " " * pad + "\n"
    hdr_bytes = hdr_str.encode("latin1")
    valid_npy = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(hdr_bytes)) + hdr_bytes + b"\x00" * 20

    # 1. Path traversal
    p_trav = tmp_path / "trav.npz"
    with zipfile.ZipFile(p_trav, mode="w") as zf:
        zf.writestr("../escape.npy", valid_npy)
    with pytest.raises(InvalidScientificArrayData, match="security violation"):
        extract_npz(p_trav)

    # 2. Backslash
    p_bs = tmp_path / "bs.npz"
    with zipfile.ZipFile(p_bs, mode="w") as zf:
        zf.writestr("sub\\item.npy", valid_npy)
    with pytest.raises(InvalidScientificArrayData, match="security violation"):
        extract_npz(p_bs)

    # 3. Directory member
    p_dir = tmp_path / "dir.npz"
    with zipfile.ZipFile(p_dir, mode="w") as zf:
        zf.writestr("somedir/", b"")
    with pytest.raises(InvalidScientificArrayData, match="security violation"):
        extract_npz(p_dir)


def test_npz_bad_member_gap_policy(tmp_path):
    """A single corrupted member inside a valid archive produces an addressable gap without failing the archive."""
    import numpy as np

    p = tmp_path / "partial_corrupt.npz"
    with zipfile.ZipFile(p, mode="w") as zf:
        # Member 1 is valid NPY
        arr_bytes = io.BytesIO()
        np.save(arr_bytes, np.ones((10, 10), dtype="<f8"))
        zf.writestr("good.npy", arr_bytes.getvalue())
        # Member 2 is corrupt NPY bytes
        zf.writestr("bad.npy", b"CORRUPT_NPY_HEADER")

    extraction = extract_npz(p)
    # The valid member is extracted
    assert any(u.origin.ref == "member:good.npy" for u in extraction.units)
    # The bad member produces an addressable gap
    assert any(g.origin.ref == "member:bad.npy" for g in extraction.gaps)


def test_error_message_scrubbing(tmp_path):
    """Error messages must not leak private directory paths."""
    secret_dir = tmp_path / "top_secret_directory"
    secret_dir.mkdir()
    corrupt = secret_dir / "corrupt.npy"
    corrupt.write_bytes(b"\x93NUMPY\x01\x00INVALID_HEADER")

    with pytest.raises(InvalidScientificArrayData) as exc_info:
        extract_npy(corrupt)

    msg = str(exc_info.value)
    assert "top_secret_directory" not in msg
    assert "corrupt.npy:" in msg
