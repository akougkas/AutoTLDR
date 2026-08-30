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


def _make_npy_bytes(version: int, descr: object, shape: tuple[int, ...], fortran_order: bool = False) -> bytes:
    """Helper to synthesize valid NPY v1, v2, or v3 bytes."""
    d = {"descr": descr, "fortran_order": fortran_order, "shape": shape}
    header_str = str(d) + "\n"
    if version == 1:
        pad_len = 16 - ((10 + len(header_str)) % 16)
        header_str = str(d) + " " * pad_len + "\n"
        header_bytes = header_str.encode("latin1")
        return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header_bytes)) + header_bytes
    elif version == 2:
        pad_len = 64 - ((12 + len(header_str)) % 64)
        header_str = str(d) + " " * pad_len + "\n"
        header_bytes = header_str.encode("latin1")
        return b"\x93NUMPY\x02\x00" + struct.pack("<I", len(header_bytes)) + header_bytes
    elif version == 3:
        pad_len = 64 - ((12 + len(header_str)) % 64)
        header_str = str(d) + " " * pad_len + "\n"
        header_bytes = header_str.encode("utf-8")
        return b"\x93NUMPY\x03\x00" + struct.pack("<I", len(header_bytes)) + header_bytes
    raise ValueError(f"unknown version {version}")


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


def test_extract_npy_v1(tmp_path):
    p = tmp_path / "test_v1.npy"
    p.write_bytes(_make_npy_bytes(1, "<f8", (100, 200), fortran_order=False) + b"\x00" * 100)

    assert detect_scientific_array_kind(p) == "npy"
    extraction = extract_npy(p)
    assert extraction.kind == "npy"
    assert extraction.source == str(p)

    array_units = [u for u in extraction.units if u.origin.ref == "array"]
    assert len(array_units) == 1
    u = array_units[0]
    assert u.meta["version"] == "1.0"
    assert u.meta["shape"] == [100, 200]
    assert u.meta["ndim"] == 2
    assert u.meta["fortran_order"] is False
    assert u.meta["dtype"] == "<f8"


def test_extract_npy_v2(tmp_path):
    p = tmp_path / "test_v2.npy"
    p.write_bytes(_make_npy_bytes(2, "<i4", (5000,), fortran_order=True) + b"\x00" * 100)

    extraction = extract_npy(p)
    u = extraction.units[0]
    assert u.meta["version"] == "2.0"
    assert u.meta["shape"] == [5000]
    assert u.meta["fortran_order"] is True
    assert u.meta["dtype"] == "<i4"


def test_extract_npy_v3(tmp_path):
    p = tmp_path / "test_v3.npy"
    p.write_bytes(_make_npy_bytes(3, "|u1", (10, 10, 10)) + b"\x00" * 100)

    extraction = extract_npy(p)
    u = extraction.units[0]
    assert u.meta["version"] == "3.0"
    assert u.meta["shape"] == [10, 10, 10]
    assert u.meta["dtype"] == "|u1"


def test_extract_npy_structured_dtype(tmp_path):
    descr = [("time", "<f8"), ("pos", "<f4", (3,)), ("flags", "|u1")]
    p = tmp_path / "test_struct.npy"
    p.write_bytes(_make_npy_bytes(1, descr, (50,)) + b"\x00" * 100)

    extraction = extract_npy(p)
    array_unit = [u for u in extraction.units if u.origin.ref == "array"][0]

    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 3
    field_names = [u.meta["name"] for u in field_units]
    assert set(field_names) == {"time", "pos", "flags"}

    # Check relations
    for f in field_units:
        assert any(
            r.src == array_unit.id and r.dst == f.id and r.kind == RelationKind.DESCRIBES
            for r in extraction.relations
        )


def test_object_dtype_safety_gap(tmp_path):
    """Arrays with object dtype must emit a safety gap without unpickling."""
    p = tmp_path / "test_object.npy"
    p.write_bytes(_make_npy_bytes(1, "|O", (10,)) + b"\x00" * 100)

    extraction = extract_npy(p)
    assert any("pickled Python objects" in str(g) for g in extraction.gaps)


def test_extract_npz(tmp_path):
    p = tmp_path / "bundle.npz"
    with zipfile.ZipFile(p, mode="w") as zf:
        zf.writestr("arr_a.npy", _make_npy_bytes(1, "<f8", (20, 30)))
        zf.writestr("arr_b.npy", _make_npy_bytes(1, "<i2", (100,)))

    assert detect_scientific_array_kind(p) == "npz"
    extraction = extract_npz(p)
    assert extraction.kind == "npz"

    # Check archive root unit
    archive_units = [u for u in extraction.units if u.origin.ref == "archive"]
    assert len(archive_units) == 1
    assert archive_units[0].meta["member_count"] == 2

    # Check member units
    member_units = [u for u in extraction.units if u.origin.ref.startswith("member:")]
    assert len(member_units) == 2
    member_names = [u.meta["name"] for u in member_units]
    assert set(member_names) == {"arr_a.npy", "arr_b.npy"}


def test_npz_path_traversal_rejected(tmp_path):
    p = tmp_path / "evil.npz"
    with zipfile.ZipFile(p, mode="w") as zf:
        zf.writestr("../escape.npy", _make_npy_bytes(1, "<f4", (5,)))

    with pytest.raises(InvalidScientificArrayData, match="traversal"):
        extract_npz(p)


def test_npz_duplicate_member_rejected(tmp_path):
    p = tmp_path / "dup.npz"
    with zipfile.ZipFile(p, mode="w") as zf:
        zf.writestr("a.npy", _make_npy_bytes(1, "<f4", (5,)))
        zf.writestr("a.npy", _make_npy_bytes(1, "<f4", (5,)))

    with pytest.raises(InvalidScientificArrayData, match="duplicate"):
        extract_npz(p)


def test_corrupt_npy_rejected(tmp_path):
    p = tmp_path / "corrupt.npy"
    p.write_bytes(b"\x93NUMPY\x01\x00INVALID_HEADER")
    with pytest.raises(InvalidScientificArrayData):
        extract_npy(p)


def test_empty_file_rejected(tmp_path):
    empty = tmp_path / "empty.npy"
    empty.write_bytes(b"")
    with pytest.raises(InvalidScientificArrayData, match="empty"):
        extract_scientific_array(empty)


def test_determinism(tmp_path):
    p = tmp_path / "det.npy"
    p.write_bytes(_make_npy_bytes(1, "<f8", (10, 10)) + b"\x00" * 100)

    ext1 = extract_scientific_array(p)
    ext2 = extract_scientific_array(p)
    assert [u.id for u in ext1.units] == [u.id for u in ext2.units]
