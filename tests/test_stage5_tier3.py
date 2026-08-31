"""Stage 5 native Tier 3 extraction contracts.

The optional parser tests generate real, tiny fixtures when their libraries are
installed.  Safety, laziness, SQLite, and dependency-failure behavior remain
fully testable in the base development environment.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

from autotldr.extract import tier3
from autotldr.unit import Modality, RelationKind, Role


def _sqlite_fixture(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        PRAGMA user_version=7;
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            temperature_c REAL,
            note TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        CREATE UNIQUE INDEX samples_project_temperature
            ON samples(project_id, temperature_c);
        INSERT INTO projects VALUES (1, 'alpha');
        INSERT INTO samples VALUES
            (10, 1, 12.5, 'ROW_PAYLOAD_MUST_NOT_ESCAPE'),
            (11, 1, 14.5, 'another private row');
        CREATE VIEW project_temperatures AS
            SELECT project_id, temperature_c FROM samples;
        """
    )
    connection.commit()
    connection.close()
    return path


def _wire_without_payload(result) -> str:
    payload = {
        "source": result.source,
        "kind": result.kind,
        "units": [
            {
                "id": unit.id,
                "modality": str(unit.modality),
                "role": str(unit.role),
                "content": unit.content,
                "origin": unit.origin.ref,
                "structure": unit.structure,
                "meta": unit.meta,
            }
            for unit in result.units
        ],
        "relations": [
            {
                "src": relation.src,
                "dst": relation.dst,
                "kind": str(relation.kind),
                "evidence": relation.evidence,
                "confidence": relation.confidence,
            }
            for relation in result.relations
        ],
        "gaps": [
            {
                "content": gap.content,
                "origin": gap.origin.ref,
                "kind": str(gap.kind),
            }
            for gap in result.gaps
        ],
        "meta": result.meta,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _assert_common(result, path: Path, kind: str) -> None:
    assert result.source == str(path)
    assert result.kind == kind
    assert result.units
    ids = {unit.id for unit in result.units}
    assert len(ids) == len(result.units)
    assert all(unit.source == str(path) for unit in result.units)
    assert all(unit.origin.source == str(path) and unit.origin.ref for unit in result.units)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)
    assert all(
        relation.src in ids and relation.dst in ids for relation in result.relations
    )
    manifest = result.meta["inputs"]
    assert manifest == [
        {
            "source": str(path),
            "kind": kind,
            "tier": 3,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    ]
    # All extractor metadata must be directly serializable by the canonical
    # JSON renderer; optional-library scalars must not leak through as objects.
    _wire_without_payload(result)


def test_importing_tier3_module_loads_no_optional_parser_or_numpy():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import autotldr.extract.tier3; "
            "names=('pyarrow','duckdb','h5py','netCDF4','numpy'); "
            "print(','.join(name for name in names if name in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


@pytest.mark.parametrize(
    ("function_name", "blocked_module", "message"),
    [
        ("extract_parquet", "pyarrow", "requires pyarrow"),
        ("extract_duckdb", "duckdb", "requires duckdb"),
        ("extract_hdf5", "h5py", "requires h5py"),
        ("extract_netcdf", "netCDF4", "requires netCDF4"),
    ],
)
def test_optional_dependency_failures_are_lazy_clear_and_closed(
    tmp_path, function_name, blocked_module, message
):
    path = tmp_path / f"input-{function_name}.bin"
    path.write_bytes(b"recognized nonempty fixture")
    script = f"""
import builtins
from pathlib import Path
from autotldr.extract import tier3
real_import = builtins.__import__
def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == {blocked_module!r} or name.startswith({blocked_module!r} + '.'):
        raise ModuleNotFoundError('blocked for test', name={blocked_module!r})
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = blocked
try:
    getattr(tier3, {function_name!r})(Path({str(path)!r}))
except ImportError as exc:
    print(str(exc))
else:
    raise SystemExit('adapter did not fail closed')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert message in completed.stdout


@pytest.mark.parametrize(
    "function_name",
    [
        "extract_parquet",
        "extract_sqlite",
        "extract_duckdb",
        "extract_hdf5",
        "extract_netcdf",
    ],
)
def test_empty_files_fail_before_optional_parsers_are_needed(tmp_path, function_name):
    path = tmp_path / function_name
    path.touch()

    with pytest.raises(tier3.InvalidTier3Data, match="file is empty"):
        getattr(tier3, function_name)(path)


@pytest.mark.parametrize(
    "function_name",
    [
        "extract_parquet",
        "extract_sqlite",
        "extract_duckdb",
        "extract_hdf5",
        "extract_netcdf",
    ],
)
def test_file_size_bound_fails_before_parser_import(
    tmp_path, monkeypatch, function_name
):
    path = tmp_path / function_name
    path.write_bytes(b"too large")
    monkeypatch.setattr(tier3, "_MAX_FILE_BYTES", 4)

    with pytest.raises(tier3.InvalidTier3Data, match="limit is 4 bytes"):
        getattr(tier3, function_name)(path)


def test_kind_aliases_are_explicit_and_ambiguous_db_suffix_is_not_guessed(tmp_path):
    path = _sqlite_fixture(tmp_path / "ambiguous.db")

    with pytest.raises(ValueError, match="does not handle"):
        tier3.extract(path)

    result = tier3.extract(path, kind="sqlite3")
    assert result.kind == "sqlite"
    assert tier3.KIND_ALIASES["h5"] == "hdf5"
    assert tier3.KIND_ALIASES["nc4"] == "netcdf"


def test_invalid_utf8_metadata_keeps_distinct_deterministic_native_identities():
    first = tier3._decode_metadata_bytes(b"\xff")
    second = tier3._decode_metadata_bytes(b"\xfe")

    assert first == (
        "binary:1:sha256:"
        "a8100ae6aa1940d0b663bb31cd466142ebbdbd5187131b92d93818987832eb89"
    )
    assert second == (
        "binary:1:sha256:"
        "aa687b58b0e73e2e383f8c500d75b591e188efe0168b3ffbcd3771caaa6dd4c7"
    )
    assert first != second
    assert "\ufffd" not in first + second
    assert tier3._decode_metadata_bytes("units") == "units"


def test_parser_error_detail_never_reflects_paths_sql_or_binary_snippets():
    private = RuntimeError(
        "/home/private/patient.sqlite: SELECT secret FROM records; b'raw-bytes'"
    )

    assert tier3._error_detail(private) == "parser reported RuntimeError"


def test_view_dependency_parser_preserves_qualified_and_quoted_identifiers():
    assert tier3._sql_dependencies(
        'SELECT * FROM "science"."runs" JOIN [sample values] ON true '
        "JOIN `local_table` ON true"
    ) == ["local_table", "sample values", "science.runs"]


def test_sqlite_emits_schema_profiles_keys_relationships_views_and_manifest(tmp_path):
    path = _sqlite_fixture(tmp_path / "study.sqlite")

    result = tier3.extract_sqlite(path)

    _assert_common(result, path, "sqlite")
    assert result.meta["user_version"] == 7
    assert result.meta["tables"] == 2
    assert result.meta["views"] == 1
    assert result.meta["foreign_keys"] == 1
    origins = {unit.origin.ref for unit in result.units}
    assert "database:" in origins
    assert "table:projects" in origins
    assert "table:samples#column:temperature_c" in origins
    assert "view:project_temperatures" in origins
    assert any("primary key" in unit.content.casefold() for unit in result.units)
    assert any("unique" in unit.content.casefold() for unit in result.units)
    assert any("foreign key" in unit.content.casefold() for unit in result.units)
    temperature = next(
        unit
        for unit in result.units
        if unit.origin.ref == "table:samples#column:temperature_c"
    )
    assert temperature.meta["sample_numeric_min"] == 12.5
    assert temperature.meta["sample_numeric_max"] == 14.5
    assert any(
        relation.kind is RelationKind.REFERENCES for relation in result.relations
    )
    assert any(
        relation.kind is RelationKind.DERIVES_FROM for relation in result.relations
    )
    assert any("unit" in gap.casefold() for gap in result.gaps)
    assert "ROW_PAYLOAD_MUST_NOT_ESCAPE" not in _wire_without_payload(result)


def test_sqlite_connection_is_read_only_immutable_and_source_is_unchanged(
    tmp_path, monkeypatch
):
    path = _sqlite_fixture(tmp_path / "immutable.sqlite")
    before = path.read_bytes()
    real_connect = sqlite3.connect
    calls: list[tuple[str, dict[str, object]]] = []

    def recording_connect(database, *args, **kwargs):
        calls.append((str(database), dict(kwargs)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    result = tier3.extract_sqlite(path)

    assert calls
    uri, kwargs = calls[0]
    assert "mode=ro" in uri and "immutable=1" in uri
    assert kwargs["uri"] is True
    assert kwargs["isolation_level"] is None
    assert path.read_bytes() == before
    assert result.meta["inputs"][0]["sha256"] == hashlib.sha256(before).hexdigest()


def test_sqlite_profiles_are_bounded_deterministic_and_do_not_emit_text_rows(
    tmp_path, monkeypatch
):
    path = _sqlite_fixture(tmp_path / "bounded.sqlite")
    monkeypatch.setattr(tier3, "_MAX_SAMPLE_ROWS", 1)

    first = tier3.extract_sqlite(path)
    second = tier3.extract_sqlite(path)

    sample_column = next(
        unit
        for unit in first.units
        if unit.origin.ref == "table:samples#column:note"
    )
    assert sample_column.meta["sampled_rows"] == 1
    assert sample_column.meta["sample_truncated"] is True
    assert sample_column.meta["sample_length_min"] == len("ROW_PAYLOAD_MUST_NOT_ESCAPE")
    assert "ROW_PAYLOAD_MUST_NOT_ESCAPE" not in _wire_without_payload(first)
    assert _wire_without_payload(first) == _wire_without_payload(second)


def test_sqlite_empty_catalog_is_an_addressed_absence_not_empty_success(tmp_path):
    path = tmp_path / "empty-schema.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("VACUUM")
    connection.close()

    result = tier3.extract_sqlite(path)

    _assert_common(result, path, "sqlite")
    assert [unit.origin.ref for unit in result.units] == ["database:"]
    assert any("no user tables or views" in gap for gap in result.gaps)
    assert all(gap.origin.ref for gap in result.gaps)


def test_sqlite_corruption_and_sidecars_fail_closed(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(tier3.InvalidTier3Data, match="signature"):
        tier3.extract_sqlite(corrupt)

    database = _sqlite_fixture(tmp_path / "with-wal.sqlite")
    database.with_name(database.name + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(tier3.InvalidTier3Data, match="checkpoint"):
        tier3.extract_sqlite(database)


def test_sqlite_object_count_bound_is_explicit(tmp_path, monkeypatch):
    path = _sqlite_fixture(tmp_path / "objects.sqlite")
    monkeypatch.setattr(tier3, "_MAX_OBJECTS", 2)

    result = tier3.extract_sqlite(path)

    assert result.meta["schema_objects"] > result.meta["tables"]
    assert any("only the first 2" in gap for gap in result.gaps)


def test_netcdf_unsupported_subtype_is_named_without_real_optional_library(
    tmp_path, monkeypatch
):
    path = tmp_path / "future.nc"
    path.write_bytes(b"nonempty synthetic container")

    class FakeDataset:
        data_model = "FUTURE_NETCDF"

        def close(self):
            pass

    module = types.ModuleType("netCDF4")
    module.Dataset = lambda *_args, **_kwargs: FakeDataset()
    monkeypatch.setitem(sys.modules, "netCDF4", module)

    with pytest.raises(
        tier3.UnsupportedTier3Subtype, match="FUTURE_NETCDF"
    ) as raised:
        tier3.extract_netcdf(path)
    assert raised.value.tier == 3
    assert raised.value.kind == "NetCDF"


def test_parquet_fixture_emits_footer_schema_and_bounded_stats(tmp_path):
    pyarrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    schema = pyarrow.schema(
        [
            pyarrow.field(
                "latency_ms",
                pyarrow.float64(),
                metadata={b"units": b"ms", b"description": b"request latency"},
            ),
            pyarrow.field("status", pyarrow.string()),
        ],
        metadata={b"title": b"bounded run profile"},
    )
    table = pyarrow.Table.from_arrays(
        [
            pyarrow.array([10.0, 20.0, None]),
            pyarrow.array(["ok", "PARQUET_ROW_PAYLOAD_MUST_NOT_ESCAPE", "failed"]),
        ],
        schema=schema,
    )
    path = tmp_path / "runs.parquet"
    parquet.write_table(table, path, row_group_size=2)

    result = tier3.extract_parquet(path)

    _assert_common(result, path, "parquet")
    assert result.meta["rows"] == 3
    assert result.meta["row_groups"] == 2
    latency = next(unit for unit in result.units if unit.origin.ref == "column:latency_ms")
    assert latency.meta["min"] == 10.0
    assert latency.meta["max"] == 20.0
    assert any(
        unit.origin.ref == "column:latency_ms#attribute:units"
        for unit in result.units
    )
    assert not any("declares no column units" in gap for gap in result.gaps)
    status = next(unit for unit in result.units if unit.origin.ref == "column:status")
    assert status.meta["range_suppressed"] is True
    assert "PARQUET_ROW_PAYLOAD_MUST_NOT_ESCAPE" not in _wire_without_payload(result)


def test_parquet_row_group_bound_and_corruption_are_explicit(tmp_path, monkeypatch):
    pyarrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "bounded.parquet"
    parquet.write_table(pyarrow.table({"value": [1, 2, 3]}), path, row_group_size=1)
    monkeypatch.setattr(tier3, "_MAX_PARQUET_ROW_GROUPS", 1)

    result = tier3.extract_parquet(path)
    assert result.meta["statistics_row_groups"] == 1
    assert any("first 1 of 3 row groups" in gap for gap in result.gaps)

    corrupt = tmp_path / "corrupt.parquet"
    corrupt.write_bytes(b"not parquet")
    with pytest.raises(tier3.InvalidTier3Data):
        tier3.extract_parquet(corrupt)


def test_duckdb_fixture_emits_schema_stats_keys_foreign_keys_and_view_dependencies(
    tmp_path,
):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "study.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, name VARCHAR UNIQUE)")
    connection.execute(
        "CREATE TABLE samples("
        "id INTEGER PRIMARY KEY, project_id INTEGER REFERENCES projects(id), "
        "value DOUBLE, note VARCHAR)"
    )
    connection.execute("INSERT INTO projects VALUES (1, 'alpha')")
    connection.execute(
        "INSERT INTO samples VALUES "
        "(1, 1, 3.5, 'DUCKDB_ROW_PAYLOAD_MUST_NOT_ESCAPE'), "
        "(2, 1, 4.5, 'private')"
    )
    connection.execute(
        "CREATE VIEW sample_values AS SELECT project_id, value FROM samples"
    )
    connection.close()

    result = tier3.extract_duckdb(path)

    _assert_common(result, path, "duckdb")
    assert result.meta["tables"] == 2
    assert result.meta["views"] == 1
    value = next(
        unit
        for unit in result.units
        if unit.origin.ref == "table:main.samples#column:value"
    )
    assert value.meta["sample_numeric_min"] == 3.5
    assert value.meta["sample_numeric_max"] == 4.5
    assert any("primary key" in unit.content.casefold() for unit in result.units)
    assert any("foreign key" in unit.content.casefold() for unit in result.units)
    assert any(
        relation.kind is RelationKind.REFERENCES for relation in result.relations
    )
    assert any(
        relation.kind is RelationKind.DERIVES_FROM for relation in result.relations
    )
    assert "DUCKDB_ROW_PAYLOAD_MUST_NOT_ESCAPE" not in _wire_without_payload(result)


def test_duckdb_corruption_fails_closed_when_parser_is_available(tmp_path):
    pytest.importorskip("duckdb")
    path = tmp_path / "corrupt.duckdb"
    path.write_bytes(b"not a DuckDB database")

    with pytest.raises(tier3.InvalidTier3Data):
        tier3.extract_duckdb(path)


def test_hdf5_fixture_emits_hierarchy_storage_attributes_and_links_without_values(
    tmp_path,
):
    h5py = pytest.importorskip("h5py")
    path = tmp_path / "run.h5"
    with h5py.File(path, "w") as handle:
        handle.attrs["title"] = "instrument run"
        run = handle.create_group("run3")
        pressure = run.create_dataset("pressure", data=[1.0, 2.0, 3.0])
        pressure.attrs["units"] = "kPa"
        pressure.attrs["long_name"] = "chamber pressure"
        pressure.attrs["valid_min"] = 0.0
        pressure.attrs["valid_max"] = 100.0
        run.create_dataset(
            "notes",
            data=[b"HDF5_DATASET_PAYLOAD_MUST_NOT_ESCAPE", b"private"],
        )
        handle["pressure_alias"] = h5py.SoftLink("/run3/pressure")

    result = tier3.extract_hdf5(path)

    _assert_common(result, path, "hdf5")
    origins = {unit.origin.ref for unit in result.units}
    assert "/" in origins
    assert "/run3" in origins
    assert "/run3/pressure" in origins
    assert "/run3/pressure#attribute:units" in origins
    pressure = next(unit for unit in result.units if unit.origin.ref == "/run3/pressure")
    assert pressure.meta["payload_read"] is False
    assert pressure.meta["shape"] == [3]
    assert any(
        relation.kind is RelationKind.REFERENCES for relation in result.relations
    )
    assert "HDF5_DATASET_PAYLOAD_MUST_NOT_ESCAPE" not in _wire_without_payload(result)


def test_hdf5_depth_object_bounds_and_corruption_are_explicit(tmp_path, monkeypatch):
    h5py = pytest.importorskip("h5py")
    path = tmp_path / "deep.h5"
    with h5py.File(path, "w") as handle:
        handle.create_group("a/b/c/d")
        handle["a"].create_dataset("one", data=[1])
        handle["a"].create_dataset("two", data=[2])
    monkeypatch.setattr(tier3, "_MAX_DEPTH", 2)
    monkeypatch.setattr(tier3, "_MAX_OBJECTS", 4)

    result = tier3.extract_hdf5(path)
    assert any("depth limit 2" in gap or "stopped at 4" in gap for gap in result.gaps)
    assert "/a/b/c" not in {unit.origin.ref for unit in result.units}

    corrupt = tmp_path / "corrupt.h5"
    corrupt.write_bytes(b"not HDF5")
    with pytest.raises(tier3.InvalidTier3Data):
        tier3.extract_hdf5(corrupt)


def test_hdf5_variable_length_attribute_is_omitted_before_read(tmp_path):
    h5py = pytest.importorskip("h5py")
    path = tmp_path / "vlen-attribute.h5"
    with h5py.File(path, "w") as handle:
        handle.attrs.create(
            "unsafe_vlen",
            "HDF5_VLEN_ATTRIBUTE_MUST_NOT_ESCAPE",
            dtype=h5py.string_dtype(encoding="utf-8"),
        )

    result = tier3.extract_hdf5(path)
    attribute = next(
        unit for unit in result.units if unit.origin.ref == "/#attribute:unsafe_vlen"
    )
    assert attribute.meta["value_omitted"] is True
    assert attribute.meta["omission_reason"] == "variable-length"
    assert "HDF5_VLEN_ATTRIBUTE_MUST_NOT_ESCAPE" not in _wire_without_payload(result)


def test_netcdf_fixture_emits_groups_dimensions_variables_units_and_no_values(tmp_path):
    netcdf = pytest.importorskip("netCDF4")
    path = tmp_path / "climate.nc"
    with netcdf.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.title = "bounded climate run"
        dataset.createDimension("time", 2)
        temperature = dataset.createVariable("temperature", "f4", ("time",))
        temperature.units = "K"
        temperature.long_name = "surface temperature"
        temperature.valid_min = 0.0
        temperature.valid_max = 400.0
        temperature[:] = [280.0, 285.0]
        note = dataset.createVariable("note", str, ("time",))
        note[0] = "NETCDF_VARIABLE_PAYLOAD_MUST_NOT_ESCAPE"
        note[1] = "private"
        station = dataset.createGroup("station")
        station.createDimension("sensor", 1)
        sensor = station.createVariable("sensor_id", "i4", ("sensor",))
        sensor[:] = [42]

    result = tier3.extract_netcdf(path)

    _assert_common(result, path, "netcdf")
    assert result.meta["data_model"] == "NETCDF4"
    origins = {unit.origin.ref for unit in result.units}
    assert "/#dimension:time" in origins
    assert "/temperature" in origins
    assert "/temperature#attribute:units" in origins
    temperature = next(unit for unit in result.units if unit.origin.ref == "/temperature")
    assert temperature.meta["payload_read"] is False
    assert any(
        relation.kind is RelationKind.REFERENCES for relation in result.relations
    )
    assert "NETCDF_VARIABLE_PAYLOAD_MUST_NOT_ESCAPE" not in _wire_without_payload(result)


def test_netcdf_variable_length_string_attribute_is_preflight_omitted(tmp_path):
    netcdf = pytest.importorskip("netCDF4")
    path = tmp_path / "vlen-attribute.nc"
    with netcdf.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.title = "ordinary bounded title"
        dataset.setncattr_string(
            "unsafe_vlen", ["NETCDF_VLEN_ATTRIBUTE_MUST_NOT_ESCAPE"]
        )

    result = tier3.extract_netcdf(path)
    title = next(
        unit for unit in result.units if unit.origin.ref == "/#attribute:title"
    )
    unsafe = next(
        unit for unit in result.units if unit.origin.ref == "/#attribute:unsafe_vlen"
    )
    assert title.meta["value"] == "ordinary bounded title"
    assert unsafe.meta["declared_type"] == "NC_STRING"
    assert unsafe.meta["value_omitted"] is True
    assert "NETCDF_VLEN_ATTRIBUTE_MUST_NOT_ESCAPE" not in _wire_without_payload(result)


def test_netcdf_depth_object_bounds_empty_and_corruption_are_explicit(
    tmp_path, monkeypatch
):
    netcdf = pytest.importorskip("netCDF4")
    deep = tmp_path / "deep.nc"
    with netcdf.Dataset(deep, "w", format="NETCDF4") as dataset:
        a = dataset.createGroup("a")
        b = a.createGroup("b")
        b.createGroup("c")
    monkeypatch.setattr(tier3, "_MAX_DEPTH", 2)

    bounded = tier3.extract_netcdf(deep)
    assert any("depth limit 2" in gap for gap in bounded.gaps)
    assert "/a/b/c" not in {unit.origin.ref for unit in bounded.units}

    empty = tmp_path / "empty.nc"
    with netcdf.Dataset(empty, "w", format="NETCDF4"):
        pass
    empty_result = tier3.extract_netcdf(empty)
    assert any(
        "no dimensions, variables, groups, or attributes" in gap
        for gap in empty_result.gaps
    )

    corrupt = tmp_path / "corrupt.nc"
    corrupt.write_bytes(b"not NetCDF")
    with pytest.raises(tier3.InvalidTier3Data):
        tier3.extract_netcdf(corrupt)


def test_catalog_name_bound_never_materializes_the_unbounded_tail():
    consumed: list[int] = []

    def names():
        for index in range(1000):
            consumed.append(index)
            yield f"name-{9 - index}" if index < 10 else f"tail-{index}"

    selected, truncated = tier3._bounded_catalog_names(names(), 3)

    assert selected == ["name-7", "name-8", "name-9"]
    assert truncated is True
    assert consumed == [0, 1, 2, 3]


def test_duckdb_auxiliary_catalog_queries_are_bounded(monkeypatch):
    monkeypatch.setattr(tier3, "_MAX_OBJECTS", 2)

    class Cursor:
        def fetchall(self):
            return []

    class Connection:
        def __init__(self):
            self.queries: list[str] = []

        def execute(self, query):
            self.queries.append(query)
            return Cursor()

    connection = Connection()
    assert tier3._duckdb_view_queries(connection) == {}
    assert tier3._duckdb_table_estimates(connection) == {}
    assert len(connection.queries) == 2
    assert all("LIMIT 3" in query for query in connection.queries)
