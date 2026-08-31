"""Tests for native bounded metadata extraction of Apache Arrow IPC, Feather, and ORC.

Primary authoritative specifications:
- Apache Arrow IPC File Format:
  https://arrow.apache.org/docs/format/Columnar.html#ipc-file-format
- Apache Arrow IPC Streaming Format:
  https://arrow.apache.org/docs/format/Columnar.html#ipc-streaming-format
- Apache Arrow Feather Format:
  https://arrow.apache.org/docs/python/feather.html
- Apache ORC File Format Specification:
  https://orc.apache.org/specification/
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import subprocess
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from autotldr.extract.columnar_interchange import (
    InvalidColumnarData,
    _begin_and_bind,
    _verify_and_finish,
    detect_columnar_kind,
    extract_arrow_file,
    extract_arrow_stream,
    extract_columnar_interchange,
    extract_feather,
    extract_orc,
)
from autotldr.unit import Modality, RelationKind, Role

CANARY_SECRET = "CANARY_SECRET_PAYLOAD_VAL_998877"


@pytest.fixture
def sample_tables(tmp_path: Path):
    import pyarrow as pa
    import pyarrow.feather as feather
    import pyarrow.ipc as ipc
    import pyarrow.orc as orc

    schema = pa.schema(
        [
            pa.field("id", pa.int64(), nullable=False),
            pa.field("name", pa.string(), nullable=True, metadata={"doc": "User name"}),
            pa.field("score", pa.float64(), nullable=True),
            pa.field("active", pa.bool_(), nullable=False),
        ],
        metadata={"app": "autotldr-test", "version": "1.0"},
    )

    data = [
        pa.array([1, 2, 3], type=pa.int64()),
        pa.array(["Alice", "Bob", CANARY_SECRET], type=pa.string()),
        pa.array([95.5, 88.0, 72.3], type=pa.float64()),
        pa.array([True, True, False], type=pa.bool_()),
    ]
    batch = pa.RecordBatch.from_arrays(data, schema=schema)
    table = pa.Table.from_batches([batch])

    # 1. Arrow file
    p_arrow_file = tmp_path / "sample.arrow"
    with pa.OSFile(str(p_arrow_file), "wb") as sink:
        with ipc.new_file(sink, schema) as writer:
            writer.write_batch(batch)

    # 2. Arrow stream
    p_arrow_stream = tmp_path / "sample.arrows"
    with pa.OSFile(str(p_arrow_stream), "wb") as sink:
        with ipc.new_stream(sink, schema) as writer:
            writer.write_batch(batch)

    # 3. Feather v2
    p_feather = tmp_path / "sample.feather"
    feather.write_feather(table, str(p_feather), version=2)

    # 4. ORC
    p_orc = tmp_path / "sample.orc"
    orc.write_table(table, str(p_orc))

    return {
        "arrow_file": p_arrow_file,
        "arrow_stream": p_arrow_stream,
        "feather": p_feather,
        "orc": p_orc,
    }


def _count_open_fds() -> int | None:
    if os.path.exists("/proc/self/fd"):
        return len(os.listdir("/proc/self/fd"))
    return None


def test_lazy_imports():
    """Importing columnar_interchange must not import pyarrow at module level."""
    cmd = [
        sys.executable,
        "-c",
        "import sys; import autotldr.extract.columnar_interchange; "
        "assert 'pyarrow' not in sys.modules, 'pyarrow was eagerly imported!'",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Lazy import failed: {res.stderr}"


def test_extract_arrow_file(sample_tables):
    path = sample_tables["arrow_file"]
    assert detect_columnar_kind(path) == "arrow-file"

    extraction = extract_arrow_file(path)
    assert extraction.kind == "arrow-file"
    assert extraction.source == str(path)

    # Exact byte count and sha256
    file_bytes = path.read_bytes()
    assert extraction.meta["inputs"][0]["bytes"] == len(file_bytes)
    assert extraction.meta["inputs"][0]["sha256"] == hashlib.sha256(file_bytes).hexdigest()

    # Table unit
    table_units = [u for u in extraction.units if u.origin.ref == "schema:table"]
    assert len(table_units) == 1
    t_unit = table_units[0]
    assert t_unit.modality == Modality.SCHEMA
    assert t_unit.role == Role.UNKNOWN
    assert "4 fields" in t_unit.content
    assert t_unit.meta["field_count"] == 4
    assert t_unit.meta["batch_count"] == 1
    assert t_unit.meta["metadata"]["app"] == "autotldr-test"

    # Field units
    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 4
    for f_unit in field_units:
        assert f_unit.role == Role.UNKNOWN
        assert f_unit.modality == Modality.SCHEMA
        assert any(
            r.src == t_unit.id and r.dst == f_unit.id and r.kind == RelationKind.DESCRIBES
            for r in extraction.relations
        )


def test_extract_arrow_stream(sample_tables):
    path = sample_tables["arrow_stream"]
    assert detect_columnar_kind(path) == "arrow-stream"

    extraction = extract_arrow_stream(path)
    assert extraction.kind == "arrow-stream"
    assert extraction.source == str(path)

    # Exact byte count and sha256
    file_bytes = path.read_bytes()
    assert extraction.meta["inputs"][0]["bytes"] == len(file_bytes)
    assert extraction.meta["inputs"][0]["sha256"] == hashlib.sha256(file_bytes).hexdigest()

    table_units = [u for u in extraction.units if u.origin.ref == "schema:table"]
    assert len(table_units) == 1
    assert table_units[0].meta["field_count"] == 4

    # Explicit unvalidated-tail gap on schema:table
    assert any(
        "later stream payload integrity and batch count were not validated" in g.content
        and g.origin.ref == "schema:table"
        for g in extraction.gaps
    )


def test_stream_late_corruption(tmp_path: Path):
    """Corrupting payload after schema in stream still extracts schema and reports unvalidated tail gap."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    schema = pa.schema([pa.field("col", pa.int32())])
    batch = pa.RecordBatch.from_arrays([pa.array([10, 20, 30])], schema=schema)

    sink = io.BytesIO()
    with ipc.new_stream(sink, schema) as writer:
        writer.write_batch(batch)
    valid_bytes = sink.getvalue()

    # Arrow stream framing: continuation 0xFFFFFFFF (4 bytes), then 4 bytes message length
    _, msg_len = struct.unpack("<II", valid_bytes[:8])
    schema_end = 8 + msg_len

    # Corrupt tail bytes starting immediately after schema message
    corrupt_bytes = valid_bytes[:schema_end] + b"\xff" * (len(valid_bytes) - schema_end)
    p_corrupt_stream = tmp_path / "corrupt_tail.arrows"
    p_corrupt_stream.write_bytes(corrupt_bytes)

    # Schema extraction succeeds and flags the unvalidated tail
    extraction = extract_arrow_stream(p_corrupt_stream)
    assert len(extraction.units) == 2  # table + col
    assert any(
        "later stream payload integrity and batch count were not validated" in g.content
        for g in extraction.gaps
    )


def test_extract_feather_v2(sample_tables):
    path = sample_tables["feather"]
    assert detect_columnar_kind(path) == "feather"

    extraction = extract_feather(path)
    assert extraction.kind == "feather"
    assert extraction.source == str(path)

    # Exact byte count and sha256
    file_bytes = path.read_bytes()
    assert extraction.meta["inputs"][0]["bytes"] == len(file_bytes)
    assert extraction.meta["inputs"][0]["sha256"] == hashlib.sha256(file_bytes).hexdigest()

    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 4
    for u in extraction.units:
        assert u.role == Role.UNKNOWN


def test_feather_v2_routing_convention(sample_tables, tmp_path: Path):
    """Feather v2 is physically Arrow IPC; .feather suffix routes as feather, other suffix routes as arrow-file."""
    valid_feather_bytes = sample_tables["feather"].read_bytes()

    p_arrow = tmp_path / "routed_as_arrow.arrow"
    p_arrow.write_bytes(valid_feather_bytes)
    assert detect_columnar_kind(p_arrow) == "arrow-file"

    p_feather = tmp_path / "routed_as_feather.feather"
    p_feather.write_bytes(valid_feather_bytes)
    assert detect_columnar_kind(p_feather) == "feather"

    p_dat = tmp_path / "routed_as_arrow_default.dat"
    p_dat.write_bytes(valid_feather_bytes)
    assert detect_columnar_kind(p_dat) == "arrow-file"


def test_extract_orc(sample_tables):
    path = sample_tables["orc"]
    assert detect_columnar_kind(path) == "orc"

    extraction = extract_orc(path)
    assert extraction.kind == "orc"
    assert extraction.source == str(path)

    # Exact byte count and sha256
    file_bytes = path.read_bytes()
    assert extraction.meta["inputs"][0]["bytes"] == len(file_bytes)
    assert extraction.meta["inputs"][0]["sha256"] == hashlib.sha256(file_bytes).hexdigest()

    table_units = [u for u in extraction.units if u.origin.ref == "schema:table"]
    assert len(table_units) == 1
    assert table_units[0].meta["field_count"] == 4
    assert table_units[0].meta["row_count"] == 3
    assert table_units[0].meta["stripe_count"] == 1


def test_unified_entry_point(sample_tables):
    for kind, path in sample_tables.items():
        extraction = extract_columnar_interchange(path)
        assert len(extraction.units) >= 5
        assert len(extraction.relations) >= 4


def test_never_emits_raw_values_and_forbidden_apis_prevented(sample_tables):
    """Canaries must be absent and forbidden materializer APIs must never be invoked."""
    import pyarrow.feather as feather
    import pyarrow.ipc as ipc
    import pyarrow.orc as orc

    def forbidden(*args, **kwargs):
        raise RuntimeError("FORBIDDEN MATERIALIZATION API CALLED")

    with (
        patch.object(feather, "read_table", forbidden),
        patch.object(feather, "read_feather", forbidden),
        patch.object(ipc.RecordBatchFileReader, "read_all", forbidden),
        patch.object(ipc.RecordBatchFileReader, "get_batch", forbidden),
        patch.object(ipc.RecordBatchFileReader, "get_record_batch", forbidden),
        patch.object(ipc.RecordBatchFileReader, "read_pandas", forbidden),
        patch.object(ipc.RecordBatchStreamReader, "read_all", forbidden),
        patch.object(ipc.RecordBatchStreamReader, "read_next_batch", forbidden),
        patch.object(orc.ORCFile, "read", forbidden),
        patch.object(orc.ORCFile, "read_stripe", forbidden),
    ):
        for kind, path in sample_tables.items():
            extraction = extract_columnar_interchange(path)

            # Assert canary is absent in units
            for u in extraction.units:
                assert CANARY_SECRET not in u.content
                assert CANARY_SECRET not in u.origin.ref
                assert CANARY_SECRET not in str(u.structure)
                assert CANARY_SECRET not in str(u.meta)

            # Assert canary is absent in relations and gaps
            for r in extraction.relations:
                assert CANARY_SECRET not in str(r.evidence)
            for g in extraction.gaps:
                assert CANARY_SECRET not in g.content
                assert CANARY_SECRET not in g.origin.ref


def test_duplicate_field_names(tmp_path: Path):
    """Arrow schemas with duplicate field names must produce unique Unit IDs and origin refs."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    dup_schema = pa.schema(
        [
            pa.field("metric", pa.int64()),
            pa.field("metric", pa.float64()),
            pa.field("metric", pa.string()),
        ]
    )
    p = tmp_path / "dup.arrow"
    with pa.OSFile(str(p), "wb") as sink:
        with ipc.new_file(sink, dup_schema) as writer:
            pass

    extraction = extract_arrow_file(p)
    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 3
    unit_ids = [u.id for u in field_units]
    assert len(set(unit_ids)) == 3, "Duplicate field names must have unique Unit IDs"

    refs = [u.origin.ref for u in field_units]
    assert refs == ["field:0:metric", "field:1:metric", "field:2:metric"]

    table_unit = [u for u in extraction.units if u.origin.ref == "schema:table"][0]
    assert len(extraction.relations) == 3
    for f_unit in field_units:
        assert any(r.src == table_unit.id and r.dst == f_unit.id for r in extraction.relations)


def test_feather_v1_declined(tmp_path: Path):
    """Real Feather v1 files must be declined with a typed unsupported subtype message."""
    import pyarrow as pa
    import pyarrow.feather as feather

    p = tmp_path / "v1.feather"
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        try:
            table = pa.Table.from_arrays([pa.array([1, 2, 3])], names=["col"])
            feather.write_feather(table, str(p), version=1)
        except (ValueError, NotImplementedError, AttributeError):
            p.write_bytes(b"FEA1" + b"\x00" * 100 + b"FEA1")

    assert detect_columnar_kind(p) == "feather-v1"

    with pytest.raises(
        InvalidColumnarData, match="Feather v1 requires table array materialization"
    ):
        extract_columnar_interchange(p)

    with pytest.raises(
        InvalidColumnarData, match="Feather v1 requires table array materialization"
    ):
        extract_feather(p)


def test_feather_v1_prefix_only_corrupt(tmp_path: Path):
    """Feather v1 prefix without closing framing fails closed, not as a valid v1 decline."""
    p = tmp_path / "fake_v1_prefix_only.feather"
    p.write_bytes(b"FEA1" + b"\x00" * 50)

    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p)

    with pytest.raises(InvalidColumnarData, match="missing Feather framing"):
        extract_feather(p)


def test_missing_optional_dependency_isolated(tmp_path: Path, sample_tables):
    """Missing PyArrow raises named ImportError without UnboundLocalError across detector and all extractors."""
    blocker_script = """
import sys
from pathlib import Path

class BlockPyArrow:
    def find_spec(self, fullname, path, target=None):
        if fullname == 'pyarrow' or fullname.startswith('pyarrow.'):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None

# Unload existing pyarrow modules
for k in list(sys.modules.keys()):
    if k == 'pyarrow' or k.startswith('pyarrow.'):
        del sys.modules[k]

sys.meta_path.insert(0, BlockPyArrow())

from autotldr.extract.columnar_interchange import (
    detect_columnar_kind,
    extract_arrow_file,
    extract_arrow_stream,
    extract_feather,
    extract_orc,
    InvalidColumnarData,
)

arrow_file = Path(sys.argv[1])
arrow_stream = Path(sys.argv[2])
feather_file = Path(sys.argv[3])
orc_file = Path(sys.argv[4])
random_file = Path(sys.argv[5])

# 1. extract_arrow_file -> named ImportError
try:
    extract_arrow_file(arrow_file)
    assert False, "expected ImportError"
except ImportError as e:
    assert "Arrow IPC support requires pyarrow" in str(e), f"Unexpected message: {e}"

# 2. extract_arrow_stream -> named ImportError
try:
    extract_arrow_stream(arrow_stream)
    assert False, "expected ImportError"
except ImportError as e:
    assert "Arrow IPC support requires pyarrow" in str(e), f"Unexpected message: {e}"

# 3. extract_feather -> named ImportError
try:
    extract_feather(feather_file)
    assert False, "expected ImportError"
except ImportError as e:
    assert "Arrow IPC support requires pyarrow" in str(e), f"Unexpected message: {e}"

# 4. extract_orc -> named ImportError
try:
    extract_orc(orc_file)
    assert False, "expected ImportError"
except ImportError as e:
    assert "ORC support requires pyarrow" in str(e), f"Unexpected message: {e}"

# 5. detect_columnar_kind on plausible continuation-prefixed stream -> named ImportError
try:
    detect_columnar_kind(arrow_stream)
    assert False, "expected ImportError on continuation-prefixed stream"
except ImportError as e:
    assert "Arrow IPC support requires pyarrow" in str(e), f"Unexpected message: {e}"

# 6. detect_columnar_kind on unknown random bytes -> fails closed with InvalidColumnarData
try:
    detect_columnar_kind(random_file)
    assert False, "expected InvalidColumnarData"
except InvalidColumnarData as e:
    assert "failed closed" in str(e), f"Unexpected message: {e}"

print("ALL_IMPORT_INTERCEPTIONS_PASSED")
"""
    p_random = tmp_path / "random_bytes.bin"
    p_random.write_bytes(b"RANDOM_UNKNOWN_NON_STREAM_DATA_12345")

    cmd = [
        sys.executable,
        "-c",
        blocker_script,
        str(sample_tables["arrow_file"]),
        str(sample_tables["arrow_stream"]),
        str(sample_tables["feather"]),
        str(sample_tables["orc"]),
        str(p_random),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Missing dependency interception failed: {res.stderr}\n{res.stdout}"
    assert "ALL_IMPORT_INTERCEPTIONS_PASSED" in res.stdout


@pytest.mark.parametrize(
    "extractor_func,key",
    [
        (extract_arrow_file, "arrow_file"),
        (extract_arrow_stream, "arrow_stream"),
        (extract_feather, "feather"),
        (extract_orc, "orc"),
    ],
)
def test_descriptor_closed_on_emission_failure(sample_tables, extractor_func, key):
    """Descriptor is closed and never leaked when IR emission fails."""
    path = sample_tables[key]

    fds_before = _count_open_fds()

    def crash_emission(*args, **kwargs):
        raise RuntimeError("Injected IR emission crash")

    with patch("autotldr.extract.columnar_interchange._extract_arrow_schema_units", crash_emission):
        with pytest.raises(RuntimeError, match="Injected IR emission crash"):
            extractor_func(path)

    fds_after = _count_open_fds()
    if fds_before is not None and fds_after is not None:
        assert fds_after == fds_before, f"Descriptor leak detected: {fds_after} != {fds_before}"


@pytest.mark.parametrize(
    "error_cls",
    [RuntimeError, TypeError, AssertionError, MemoryError],
)
def test_programmer_errors_escape_unchanged_from_parser_and_emission(sample_tables, error_cls):
    """RuntimeError, TypeError, AssertionError, MemoryError escape untouched from parser and emission."""
    path = sample_tables["arrow_file"]
    import pyarrow.ipc as ipc

    # 1. Error during parser call
    def crash_parser(*args, **kwargs):
        raise error_cls(f"Injected parser {error_cls.__name__}")

    with patch.object(ipc, "open_file", crash_parser):
        with pytest.raises(error_cls, match=f"Injected parser {error_cls.__name__}"):
            extract_arrow_file(path)

    # 2. Error during emission
    def crash_emission(*args, **kwargs):
        raise error_cls(f"Injected emission {error_cls.__name__}")

    with patch("autotldr.extract.columnar_interchange._extract_arrow_schema_units", crash_emission):
        with pytest.raises(error_cls, match=f"Injected emission {error_cls.__name__}"):
            extract_arrow_file(path)


@pytest.mark.parametrize(
    "error_cls",
    [RuntimeError, TypeError, AssertionError, MemoryError],
)
def test_programmer_errors_escape_unchanged_from_stream_probe(sample_tables, error_cls):
    """RuntimeError, TypeError, AssertionError, MemoryError escape untouched from detector stream probe."""
    path = sample_tables["arrow_stream"]
    import pyarrow.ipc as ipc

    def crash_probe(*args, **kwargs):
        raise error_cls(f"Injected probe {error_cls.__name__}")

    with patch.object(ipc, "open_stream", crash_probe):
        with pytest.raises(error_cls, match=f"Injected probe {error_cls.__name__}"):
            detect_columnar_kind(path)


def test_fail_closed_spoofed_inputs(tmp_path: Path):
    """Spoofed prefix-only, suffix-only, continuation-only, ORC-footer-only, and misleading suffix inputs fail closed."""
    # Prefix-only Arrow spoof (starts with ARROW1, does not end with ARROW1)
    p_prefix = tmp_path / "spoof_prefix.arrow"
    p_prefix.write_bytes(b"ARROW1" + b"\x00" * 50)
    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p_prefix)
    with pytest.raises(InvalidColumnarData, match="missing Arrow IPC file ARROW1 framing"):
        extract_arrow_file(p_prefix)

    # Suffix-only Arrow spoof (does not start with ARROW1, ends with ARROW1)
    p_suffix = tmp_path / "spoof_suffix.arrow"
    p_suffix.write_bytes(b"\x00" * 50 + b"ARROW1")
    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p_suffix)
    with pytest.raises(InvalidColumnarData, match="missing Arrow IPC file ARROW1 framing"):
        extract_arrow_file(p_suffix)

    # Continuation-only stream spoof (starts with 0xffffffff then garbage)
    p_cont = tmp_path / "spoof_cont.arrows"
    p_cont.write_bytes(b"\xff\xff\xff\xff\x00\x00GARBAGE_PAYLOAD")
    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p_cont)
    with pytest.raises(InvalidColumnarData):
        extract_arrow_stream(p_cont)

    # ORC footer-only spoof (no leading ORC)
    p_orc_footer = tmp_path / "spoof_orc_footer.orc"
    p_orc_footer.write_bytes(b"\x00" * 50 + b"ORC")
    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p_orc_footer)
    with pytest.raises(InvalidColumnarData, match="missing Apache ORC magic signature"):
        extract_orc(p_orc_footer)

    # Misleading suffix with random garbage
    p_bad_feather = tmp_path / "bad.feather"
    p_bad_feather.write_bytes(b"RANDOM_BYTES_NOT_COLUMNAR")
    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p_bad_feather)
    with pytest.raises(InvalidColumnarData):
        extract_feather(p_bad_feather)


def test_empty_and_corrupt_files_rejected(tmp_path: Path):
    """Empty and corrupt files across all subtypes must be rejected."""
    empty = tmp_path / "empty.arrow"
    empty.write_bytes(b"")
    with pytest.raises(InvalidColumnarData, match="empty"):
        detect_columnar_kind(empty)
    with pytest.raises(InvalidColumnarData, match="empty"):
        extract_arrow_file(empty)

    # Corrupt arrow file
    corrupt_arrow = tmp_path / "corrupt.arrow"
    corrupt_arrow.write_bytes(b"ARROW1" + b"\x00" * 20 + b"ARROW1")
    with pytest.raises(InvalidColumnarData):
        extract_arrow_file(corrupt_arrow)

    # Corrupt orc file
    corrupt_orc = tmp_path / "corrupt.orc"
    corrupt_orc.write_bytes(b"ORC" + b"\x00" * 20)
    with pytest.raises(InvalidColumnarData):
        extract_orc(corrupt_orc)


def test_empty_schema_and_absent_metadata_gaps(tmp_path: Path):
    """Empty schema and absent custom metadata must emit explicit gaps."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    empty_schema = pa.schema([])
    p = tmp_path / "empty_schema.arrow"
    with pa.OSFile(str(p), "wb") as sink:
        with ipc.new_file(sink, empty_schema) as writer:
            pass

    extraction = extract_arrow_file(p)
    assert any("0 fields/columns" in g.content for g in extraction.gaps)
    assert any("No custom application metadata declared in schema" in g.content for g in extraction.gaps)


def test_metadata_bounds_and_binary_and_non_utf8(tmp_path: Path):
    """Bounds, binary metadata, non-UTF8 keys/values, and duplicate normalization are handled deterministically."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    meta = {
        b"a_oversized": b"A" * 5000,
        b"b_non_utf8_key_\xff\xfe": b"binary_key_val",
        b"c_non_utf8_val": b"\x80\x81\x82\x83",
    }
    for i in range(270):
        meta[f"k_{i:03d}".encode("utf-8")] = f"v_{i}".encode("utf-8")

    schema = pa.schema([pa.field("col1", pa.int32())], metadata=meta)
    p = tmp_path / "meta_bounds.arrow"
    with pa.OSFile(str(p), "wb") as sink:
        with ipc.new_file(sink, schema) as writer:
            pass

    extraction = extract_arrow_file(p)
    gap_contents = [g.content for g in extraction.gaps]

    # Check excess metadata keys gap
    assert any("Metadata key count exceeds limit" in c and "omitted" in c for c in gap_contents)

    # Check oversized value gap
    assert any("value exceeds 4096 bytes" in c and "sha256" in c for c in gap_contents)

    # Check non-UTF8 binary key gap
    assert any("contains non-UTF-8 binary data" in c for c in gap_contents)

    # Check non-UTF8 binary value gap
    assert any("value contains non-UTF-8 binary data" in c for c in gap_contents)

    # Table unit metadata
    table_unit = [u for u in extraction.units if u.origin.ref == "schema:table"][0]
    safe_meta = table_unit.meta["metadata"]
    assert "a_oversized" in safe_meta
    assert safe_meta["a_oversized"].startswith("<5000 bytes, sha256=")


def test_excess_fields_bounds(tmp_path: Path):
    """Schemas with field count exceeding _MAX_FIELDS (1024) emit suppressed count gap."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    fields = [pa.field(f"f_{i}", pa.int32()) for i in range(1030)]
    schema = pa.schema(fields)
    p = tmp_path / "excess_fields.arrow"
    with pa.OSFile(str(p), "wb") as sink:
        with ipc.new_file(sink, schema) as writer:
            pass

    extraction = extract_arrow_file(p)
    gap_contents = [g.content for g in extraction.gaps]
    assert any("Field count exceeds limit (1030 > 1024); 6 remaining fields omitted" in c for c in gap_contents)
    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 1024


def test_same_size_in_place_rewrite_rejected(sample_tables):
    """Same-size in-place file mutation with nanosecond timestamps restored is rejected."""
    path = sample_tables["arrow_file"]
    st = path.stat()
    orig_atime = st.st_atime_ns
    orig_mtime = st.st_mtime_ns

    context = _begin_and_bind(path, "arrow-file")

    # In-place modify exactly 1 byte in the file without changing size, restore timestamps
    with open(path, "r+b") as f:
        f.seek(8)
        f.write(b"\xaa")
    os.utime(path, ns=(orig_atime, orig_mtime))

    with pytest.raises(InvalidColumnarData, match="source changed while it was being extracted"):
        _verify_and_finish(context)


def test_pathname_replacement_rejected(tmp_path: Path, sample_tables):
    """Pathname replaced with a new inode during extraction is rejected."""
    path = sample_tables["arrow_file"]
    context = _begin_and_bind(path, "arrow-file")

    # Move original file and replace with a newly created file at same path
    backup = tmp_path / "backup.arrow"
    os.rename(path, backup)
    path.write_bytes(backup.read_bytes())

    with pytest.raises(InvalidColumnarData, match="source pathname replaced while it was being extracted"):
        _verify_and_finish(context)


def test_error_message_scrubbing_no_leak(tmp_path: Path):
    """Parser errors must not leak private directory paths, schemas, or binary buffers."""
    secret_dir = tmp_path / "private_user_secret_data_directory"
    secret_dir.mkdir()
    corrupt_file = secret_dir / "corrupt.arrow"
    corrupt_file.write_bytes(b"ARROW1\x00\x00INVALID_HEADER_GARBAGE_PAYLOAD\x00\x00ARROW1")

    with pytest.raises(InvalidColumnarData) as exc_info:
        extract_arrow_file(corrupt_file)

    msg = str(exc_info.value)
    assert "private_user_secret_data_directory" not in msg
    assert "corrupt.arrow:" in msg
    assert "parse failed" in msg


def test_invariants_and_determinism(sample_tables):
    """All units have Role.UNKNOWN, non-empty origins, endpoint closure, and repeated extractions are identical."""
    path = sample_tables["arrow_file"]

    ext1 = extract_arrow_file(path)
    ext2 = extract_arrow_file(path)

    # Origins non-empty and Role.UNKNOWN
    for u in ext1.units:
        assert u.origin.ref
        assert u.origin.source == str(path)
        assert u.role == Role.UNKNOWN

    for g in ext1.gaps:
        assert g.origin.ref
        assert g.origin.source == str(path)

    # Determinism
    assert [u.id for u in ext1.units] == [u.id for u in ext2.units]
    assert [u.content for u in ext1.units] == [u.content for u in ext2.units]
    assert [r.src + r.dst + r.evidence for r in ext1.relations] == [r.src + r.dst + r.evidence for r in ext2.relations]
    assert [g.content for g in ext1.gaps] == [g.content for g in ext2.gaps]
    assert ext1.meta == ext2.meta
