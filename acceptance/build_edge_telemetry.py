#!/usr/bin/env python3
"""Build the small independent Tier 3 alpha acceptance package."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path


README = """# Edge telemetry handoff

This package records a capacity experiment for an edge telemetry service. A run is
accepted only when throughput stays above 2,800 Mbps and error rate stays below 1 percent.

`capacity.xlsx` contains the planning assumptions and formula chain.
`results.parquet` contains the bounded result schema and file statistics.
`experiments.sqlite3` records run ownership and the acceptance decision.
`forecast.nc` records the expected throughput over a three-hour horizon.

The package does not document how the overhead factor or safety margin was chosen.
"""


def _write_workbook(path: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Capacity"
    for row in (
        ("metric", "value"),
        ("node_count", 8),
        ("per_node_mbps", 450),
        ("overhead_factor", 0.92),
        ("safety_margin_pct", 10),
        ("", ""),
        ("raw_capacity_mbps", "=B2*B3"),
        ("effective_capacity_mbps", "=B7*B4*(1-B5/100)"),
    ):
        sheet.append(row)
    workbook.save(path)


def _write_parquet(path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        (
            pa.field("run_id", pa.int64(), nullable=False),
            pa.field("throughput_mbps", pa.float64(), nullable=False),
            pa.field("error_rate_pct", pa.float64(), nullable=False),
            pa.field("accepted", pa.bool_(), nullable=False),
        ),
        metadata={
            b"dataset": b"edge telemetry capacity trials",
            b"throughput_unit": b"Mbps",
            b"error_rate_unit": b"percent",
        },
    )
    table = pa.Table.from_arrays(
        (
            pa.array((101, 102, 103), type=pa.int64()),
            pa.array((3012.0, 2876.0, 2740.0), type=pa.float64()),
            pa.array((0.4, 0.7, 1.2), type=pa.float64()),
            pa.array((True, True, False), type=pa.bool_()),
        ),
        schema=schema,
    )
    pq.write_table(table, path)


def _write_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE experiments (
                experiment_id INTEGER PRIMARY KEY,
                owner TEXT NOT NULL,
                purpose TEXT NOT NULL
            );
            CREATE TABLE runs (
                run_id INTEGER PRIMARY KEY,
                experiment_id INTEGER NOT NULL REFERENCES experiments(experiment_id),
                accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
                note TEXT
            );
            CREATE INDEX runs_experiment_idx ON runs(experiment_id);
            INSERT INTO experiments VALUES
                (1, 'platform-team', 'Validate edge telemetry capacity');
            INSERT INTO runs VALUES
                (101, 1, 1, 'passes throughput and error-rate gates'),
                (102, 1, 1, 'passes throughput and error-rate gates'),
                (103, 1, 0, 'misses both gates');
            """
        )


def _write_netcdf(path: Path) -> None:
    import netCDF4

    with netCDF4.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.title = "Edge telemetry throughput forecast"
        dataset.source = "capacity planning model"
        dataset.createDimension("time", 3)
        time = dataset.createVariable("time", "i4", ("time",))
        time.units = "hours since 2026-08-31 00:00:00 UTC"
        throughput = dataset.createVariable("throughput", "f4", ("time",))
        throughput.units = "Mbps"
        throughput.long_name = "forecast effective throughput"
        time[:] = (0, 1, 2)
        throughput[:] = (2980.8, 2910.0, 2840.5)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: build_edge_telemetry.py NEW_OUTPUT_DIRECTORY")
    root = Path(argv[0])
    if root.exists():
        raise SystemExit(f"refusing to overwrite existing path: {root}")
    manifest_path = root.with_name(f"{root.name}.manifest.json")
    if manifest_path.exists():
        raise SystemExit(f"refusing to overwrite existing path: {manifest_path}")
    root.mkdir(parents=True)

    (root / "README.md").write_text(README, encoding="utf-8", newline="\n")
    _write_workbook(root / "capacity.xlsx")
    _write_parquet(root / "results.parquet")
    _write_sqlite(root / "experiments.sqlite3")
    _write_netcdf(root / "forecast.nc")

    kinds = {
        "README.md": "markdown",
        "capacity.xlsx": "xlsx",
        "experiments.sqlite3": "sqlite",
        "forecast.nc": "netcdf",
        "results.parquet": "parquet",
    }
    manifest = {
        "schema": "autotldr-edge-telemetry-corpus-v1",
        "files": [
            {
                "name": name,
                "bytes": (root / name).stat().st_size,
                "kind": kinds[name],
                "sha256": _sha256(root / name),
            }
            for name in sorted(kinds)
        ],
        "expected_native_meaning": [
            "acceptance thresholds",
            "XLSX formula dependency chain",
            "SQLite primary and foreign keys",
            "Parquet schema and units",
            "NetCDF dimensions, variable units, and attributes",
            "absence of documented overhead and safety-margin rationale",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(root)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
