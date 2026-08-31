"""Native Tier-0 structured extraction contracts.

The tests call the extractor directly until the Stage 3 router wires these
suffixes.  They assert semantics and provenance, not merely that parsing did
not raise: structured inputs become schemas and profiles, never row dumps.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from autotldr.extract.structured import InvalidStructuredData, extract
from autotldr.unit import Modality, Role


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _assert_representation(result, path: Path) -> None:
    assert result.source == str(path)
    assert result.units
    ids = {unit.id for unit in result.units}
    assert len(ids) == len(result.units)
    assert all(unit.source == str(path) for unit in result.units)
    assert all(unit.origin.source == str(path) and unit.origin.ref for unit in result.units)
    assert all(unit.modality is Modality.SCHEMA for unit in result.units)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)
    assert all(relation.src in ids and relation.dst in ids for relation in result.relations)


def test_importing_structured_extractor_does_not_import_optional_yaml_parser():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import autotldr.extract.structured; "
            "print('yaml' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_json_induces_one_aggregated_schema_with_json_pointer_origins(tmp_path):
    path = _write(
        tmp_path / "runs.json",
        """{
  "version": 1,
  "runs": [
    {"status": "ok", "latency_ms": 10},
    {"status": "failed", "latency_ms": 20},
    {"status": "ok", "latency_ms": 15}
  ],
  "a/b~c": true
}
""",
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.kind == "json"
    assert result.meta["top_level"] == "object"
    by_schema = {unit.meta["schema_path"]: unit for unit in result.units}
    assert by_schema["$/runs/*/latency_ms"].meta["observations"] == 3
    assert "range 10 to 20" in by_schema["$/runs/*/latency_ms"].content
    assert by_schema["$/runs/*/status"].meta["distinct"] == 2
    assert by_schema["$/a~1b~0c"].origin.ref == "pointer:/a~1b~0c"
    # Array members are aggregated under one wildcard schema, not emitted as
    # three copies of each record field.
    assert len([key for key in by_schema if key == "$/runs/*/status"]) == 1
    rendered = "\n".join(unit.content for unit in result.units)
    assert '{"status": "ok", "latency_ms": 10}' not in rendered


def test_jsonl_profiles_across_records_and_reports_optional_fields(tmp_path):
    path = _write(
        tmp_path / "events.jsonl",
        '{"event":"start","attempt":1}\n\n'
        '{"event":"finish","attempt":2,"elapsed":2.5}\n'
        '{"event":"finish","attempt":3,"elapsed":3.5}\n',
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.kind == "jsonl"
    assert result.meta["records"] == 3
    assert result.meta["blank_lines"] == 1
    elapsed = next(unit for unit in result.units if unit.meta["schema_path"] == "$/elapsed")
    assert elapsed.meta["presence"] == pytest.approx(2 / 3)
    assert elapsed.origin.ref == "lines:3-4#pointer:/elapsed"
    assert "present in 2 of 3" in elapsed.content
    assert any("blank line" in gap for gap in result.gaps)


def test_empty_jsonl_reports_absence_instead_of_inventing_a_schema(tmp_path):
    path = _write(tmp_path / "empty.jsonl", "\n\n")

    result = extract(path)

    assert result.units == []
    assert result.meta["records"] == 0
    assert any("no records" in gap for gap in result.gaps)


def test_yaml_uses_safe_parser_schema_and_line_range_origins(tmp_path):
    path = _write(
        tmp_path / "workers.yaml",
        """service: queue
workers:
  - name: alpha
    retries: 2
  - name: beta
    retries: 4
    enabled: true
""",
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.kind == "yaml"
    by_schema = {unit.meta["schema_path"]: unit for unit in result.units}
    retries = by_schema["$/workers/*/retries"]
    assert retries.meta["observations"] == 2
    assert retries.meta["numeric"]["min"] == 2
    assert retries.meta["numeric"]["max"] == 4
    assert retries.origin.ref.startswith("lines:")
    assert retries.origin.ref.endswith("#path:$/workers/*/retries")
    enabled = by_schema["$/workers/*/enabled"]
    assert enabled.meta["presence"] == 0.5
    assert result.meta["documents"] == 1


def test_yaml_merge_keys_allow_explicit_overrides(tmp_path):
    path = _write(
        tmp_path / "merge.yaml",
        """defaults: &defaults
  retries: 2
  enabled: true
worker:
  <<: *defaults
  retries: 4
""",
    )

    result = extract(path)

    by_schema = {unit.meta["schema_path"]: unit for unit in result.units}
    assert by_schema["$/worker/retries"].meta["values"] == ["4"]
    assert "$/worker/enabled" in by_schema


def test_toml_emits_native_table_and_key_origins(tmp_path):
    path = _write(
        tmp_path / "service.toml",
        """title = "worker"
enabled = true

[server]
host = "127.0.0.1"
port = 8080
timeouts = [1.5, 2.0, 3.0]
""",
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.kind == "toml"
    by_schema = {unit.meta["schema_path"]: unit for unit in result.units}
    assert by_schema["server"].origin.ref == "table:server"
    assert by_schema["server.port"].origin.ref == "key:server.port"
    assert by_schema["server.port"].meta["types"] == ["integer"]
    assert by_schema["server.timeouts.*"].meta["observations"] == 3
    assert result.meta["keys"] >= 5


def test_xml_induces_repeated_element_attribute_and_text_shape(tmp_path):
    path = _write(
        tmp_path / "catalog.xml",
        """<?xml version="1.0"?>
<catalog xmlns:m="urn:metrics">
  <item id="a"><m:latency>10</m:latency><status>ok</status></item>
  <item id="b"><m:latency>20</m:latency><status>failed</status></item>
</catalog>
""",
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.kind == "xml"
    by_schema = {unit.meta["schema_path"]: unit for unit in result.units}
    item = by_schema["/catalog/item"]
    assert item.meta["observations"] == 2
    assert item.origin.ref == "xpath:/catalog/item"
    assert by_schema["/catalog/item/@id"].meta["distinct"] == 2
    latency = by_schema["/catalog/item/{urn:metrics}latency/#text"]
    assert latency.meta["numeric"]["min"] == 10
    assert latency.meta["numeric"]["max"] == 20
    assert result.meta["namespaces"] == {"m": "urn:metrics"}
    assert result.meta["elements"] == 7


@pytest.mark.parametrize(
    ("suffix", "delimiter"),
    [(".csv", ","), (".tsv", "\t")],
)
def test_delimited_inputs_profile_columns_and_never_dump_rows(tmp_path, suffix, delimiter):
    path = _write(
        tmp_path / f"measurements{suffix}",
        delimiter.join(("run", "latency_ms", "status"))
        + "\n"
        + delimiter.join(("alpha", "10", "ok"))
        + "\n"
        + delimiter.join(("beta", "20", "failed"))
        + "\n"
        + delimiter.join(("gamma", "", "ok"))
        + "\n",
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.meta == {
        "bytes": len(path.read_bytes()),
        "rows": 3,
        "columns": 3,
        "delimiter": delimiter,
        "header": True,
        "malformed_rows": 0,
    }
    table = next(unit for unit in result.units if unit.meta.get("table_summary"))
    columns = [unit for unit in result.units if "column" in unit.meta]
    assert len(columns) == 3
    assert len(result.relations) == 3
    latency = next(unit for unit in columns if unit.meta["name"] == "latency_ms")
    assert latency.origin.ref == "column:2"
    assert latency.meta["nulls"] == 1
    assert latency.meta["numeric"]["min"] == 10
    assert latency.meta["numeric"]["max"] == 20
    rendered = table.content + "\n" + "\n".join(unit.content for unit in columns)
    assert f"alpha{delimiter}10{delimiter}ok" not in rendered
    assert f"beta{delimiter}20{delimiter}failed" not in rendered


def test_delimited_shape_problems_are_explicit_gaps(tmp_path):
    path = _write(
        tmp_path / "ragged.csv",
        "name,value,value\nalpha,1\nbeta,2,3,extra\n",
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.meta["malformed_rows"] == 2
    assert any("duplicate header" in gap for gap in result.gaps)
    assert any("different field count" in gap for gap in result.gaps)


def test_delimited_blank_records_are_reported_but_not_profiled(tmp_path):
    path = _write(
        tmp_path / "readings.csv",
        "sensor_id,temperature_c\nalpha,21.5\n\n beta,22.0\n\n",
    )

    result = extract(path)

    _assert_representation(result, path)
    assert result.meta["rows"] == 2
    assert result.meta["blank_rows"] == 2
    assert result.meta["malformed_rows"] == 0
    assert any("2 blank row" in gap for gap in result.gaps)
    assert all(unit.meta.get("nulls", 0) == 0 for unit in result.units)


def test_headerless_numeric_csv_uses_positional_columns_and_says_so(tmp_path):
    path = _write(tmp_path / "coordinates.csv", "1,2.5\n2,3.5\n")

    result = extract(path)

    _assert_representation(result, path)
    assert result.meta["header"] is False
    assert result.meta["rows"] == 2
    columns = [unit for unit in result.units if "column" in unit.meta]
    assert [unit.meta["name"] for unit in columns] == ["column_1", "column_2"]
    assert any("positional column names" in gap for gap in result.gaps)


def test_blank_delimited_input_reports_no_schema(tmp_path):
    path = _write(tmp_path / "blank.tsv", "\n\t\n")

    result = extract(path)

    assert result.units == []
    assert result.meta["rows"] == 0
    assert result.meta["columns"] == 0
    assert any("no table schema" in gap for gap in result.gaps)


@pytest.mark.parametrize(
    ("name", "body", "kind", "needle"),
    [
        ("bad.json", '{"a": 1,}', "JSON", "line 1"),
        ("duplicate.json", '{"a": 1, "a": 2}', "JSON", "duplicate object key"),
        ("constant.json", '{"value": NaN}', "JSON", "non-standard numeric constant"),
        ("bad.jsonl", '{"ok":1}\n{"bad":}\n', "JSONL", "line 2"),
        ("duplicate.yaml", "a: 1\na: 2\n", "YAML", "duplicate key"),
        ("bad.toml", "a = [1,\n", "TOML", "invalid TOML"),
        ("bad.xml", "<root><open></root>", "XML", "line 1"),
    ],
)
def test_invalid_structured_data_is_typed_and_actionable(tmp_path, name, body, kind, needle):
    path = _write(tmp_path / name, body)

    with pytest.raises(InvalidStructuredData) as raised:
        extract(path)

    assert raised.value.path == path
    assert raised.value.kind == kind
    assert needle in str(raised.value)


def test_dispatch_rejects_a_suffix_it_does_not_own(tmp_path):
    path = _write(tmp_path / "config.ini", "key=value\n")

    with pytest.raises(ValueError, match="does not handle .ini"):
        extract(path)
