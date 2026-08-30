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

import sys
import tempfile
from pathlib import Path
import pytest

from autotldr.unit import Modality, RelationKind, Role
from autotldr.extract.columnar_interchange import (
    extract_columnar_interchange,
    extract_arrow_file,
    extract_arrow_stream,
    extract_feather,
    extract_orc,
    detect_columnar_kind,
    InvalidColumnarData,
)


@pytest.fixture
def sample_tables(tmp_path):
    import pyarrow as pa
    import pyarrow.ipc as ipc
    import pyarrow.feather as feather
    import pyarrow.orc as orc

    schema = pa.schema([
        pa.field("id", pa.int64(), nullable=False),
        pa.field("name", pa.string(), nullable=True, metadata={"doc": "User name"}),
        pa.field("score", pa.float64(), nullable=True),
        pa.field("active", pa.bool_(), nullable=False),
    ], metadata={"app": "autotldr-test", "version": "1.0"})

    data = [
        pa.array([1, 2, 3], type=pa.int64()),
        pa.array(["Alice", "Bob", "Charlie"], type=pa.string()),
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

    # 3. Feather v2 (written via pyarrow.feather with version=2 or default)
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


def test_lazy_imports():
    """Importing columnar_interchange must not import pyarrow at module level."""
    import subprocess
    cmd = [
        sys.executable,
        "-c",
        "import sys; import autotldr.extract.columnar_interchange; assert 'pyarrow' not in sys.modules, 'pyarrow was eagerly imported!'",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Lazy import failed: {res.stderr}"


def test_extract_arrow_file(sample_tables):
    path = sample_tables["arrow_file"]
    assert detect_columnar_kind(path) == "arrow-file"

    extraction = extract_arrow_file(path)
    assert extraction.kind == "arrow-file"
    assert extraction.source == str(path)

    # Check table unit
    table_units = [u for u in extraction.units if u.origin.ref == "schema:table"]
    assert len(table_units) == 1
    t_unit = table_units[0]
    assert t_unit.modality == Modality.SCHEMA
    assert t_unit.role == Role.UNKNOWN
    assert "4 fields" in t_unit.content
    assert t_unit.meta["field_count"] == 4
    assert t_unit.meta["batch_count"] == 1
    assert t_unit.meta["metadata"]["app"] == "autotldr-test"

    # Check field units
    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 4
    field_names = [u.meta["name"] for u in field_units]
    assert set(field_names) == {"id", "name", "score", "active"}

    # Check relations
    for f_unit in field_units:
        assert any(
            r.src == t_unit.id and r.dst == f_unit.id and r.kind == RelationKind.DESCRIBES
            for r in extraction.relations
        )


def test_duplicate_field_names(tmp_path):
    """Arrow schemas with duplicate field names must produce unique Unit IDs and origin refs."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    dup_schema = pa.schema([
        pa.field("metric", pa.int64()),
        pa.field("metric", pa.float64()),
        pa.field("metric", pa.string()),
    ])
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


def test_feather_v1_declined(tmp_path):
    """Feather v1 files must be declined with a typed unsupported subtype message."""
    p = tmp_path / "v1.feather"
    p.write_bytes(b"FEA1" + b"\x00" * 100 + b"FEA1")

    with pytest.raises(InvalidColumnarData, match="Feather v1 requires table array materialization"):
        extract_columnar_interchange(p)


def test_fail_closed_unknown_and_spoofed_bytes(tmp_path):
    """Spoofed suffixes on unknown bytes must fail closed."""
    p_fake_arrow = tmp_path / "fake.arrow"
    p_fake_arrow.write_bytes(b"SPOOFED_UNKNOWN_BYTES_1234567890")

    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p_fake_arrow)

    p_fake_orc = tmp_path / "fake.orc"
    p_fake_orc.write_bytes(b"NOT_REALLY_ORC_HEADER_OR_FOOTER")

    with pytest.raises(InvalidColumnarData, match="failed closed"):
        detect_columnar_kind(p_fake_orc)


def test_error_message_scrubbing_no_leak(tmp_path):
    """Parser errors must not leak private directory paths in exception text."""
    secret_dir = tmp_path / "private_user_secret_data_directory"
    secret_dir.mkdir()
    corrupt_file = secret_dir / "corrupt.arrow"
    corrupt_file.write_bytes(b"ARROW1\x00\x00INVALID_HEADER_GARBAGE_PAYLOAD")

    with pytest.raises(InvalidColumnarData) as exc_info:
        extract_arrow_file(corrupt_file)

    msg = str(exc_info.value)
    assert "private_user_secret_data_directory" not in msg
    assert "corrupt.arrow:" in msg


def test_extract_arrow_stream(sample_tables):
    path = sample_tables["arrow_stream"]
    assert detect_columnar_kind(path) == "arrow-stream"

    extraction = extract_arrow_stream(path)
    assert extraction.kind == "arrow-stream"
    assert extraction.source == str(path)

    table_units = [u for u in extraction.units if u.origin.ref == "schema:table"]
    assert len(table_units) == 1
    assert table_units[0].meta["field_count"] == 4

    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 4


def test_extract_feather_v2(sample_tables):
    path = sample_tables["feather"]
    assert detect_columnar_kind(path) == "feather"

    extraction = extract_feather(path)
    assert extraction.kind == "feather"
    assert extraction.source == str(path)

    field_units = [u for u in extraction.units if u.origin.ref.startswith("field:")]
    assert len(field_units) == 4


def test_extract_orc(sample_tables):
    path = sample_tables["orc"]
    assert detect_columnar_kind(path) == "orc"

    extraction = extract_orc(path)
    assert extraction.kind == "orc"
    assert extraction.source == str(path)

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


def test_never_emits_raw_values(sample_tables):
    """Extraction must never contain cell contents or row payloads."""
    extraction = extract_columnar_interchange(sample_tables["arrow_file"])
    for unit in extraction.units:
        assert "Alice" not in unit.content
        assert "Bob" not in unit.content
        assert "Charlie" not in unit.content
        assert "95.5" not in unit.content


def test_empty_file_rejected(tmp_path):
    empty = tmp_path / "empty.arrow"
    empty.write_bytes(b"")
    with pytest.raises(InvalidColumnarData, match="empty"):
        extract_columnar_interchange(empty)


def test_corrupt_file_rejected(tmp_path):
    corrupt = tmp_path / "corrupt.arrow"
    corrupt.write_bytes(b"ARROW1\x00\x00INVALIDBYTESGARBAGE")
    with pytest.raises(InvalidColumnarData):
        extract_arrow_file(corrupt)


def test_empty_schema_gaps(tmp_path):
    import pyarrow as pa
    import pyarrow.ipc as ipc

    empty_schema = pa.schema([])
    p = tmp_path / "empty_schema.arrow"
    with pa.OSFile(str(p), "wb") as sink:
        with ipc.new_file(sink, empty_schema) as writer:
            pass

    extraction = extract_arrow_file(p)
    assert any("0 fields/columns" in str(g) for g in extraction.gaps)


def test_determinism(sample_tables):
    path = sample_tables["arrow_file"]
    ext1 = extract_arrow_file(path)
    ext2 = extract_arrow_file(path)
    assert [u.id for u in ext1.units] == [u.id for u in ext2.units]
    assert [r.src + r.dst for r in ext1.relations] == [r.src + r.dst for r in ext2.relations]
