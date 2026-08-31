"""The independent first-user corpus must stay buildable and semantically useful."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from autotldr.api import acquire


def test_edge_telemetry_acceptance_corpus_builds_without_payload_leakage(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    builder = repository / "acceptance" / "build_edge_telemetry.py"
    root = tmp_path / "edge-telemetry"

    completed = subprocess.run(
        [sys.executable, str(builder), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert str(root) in completed.stdout
    assert sorted(path.name for path in root.iterdir()) == [
        "README.md",
        "capacity.xlsx",
        "experiments.sqlite3",
        "forecast.nc",
        "results.parquet",
    ]
    manifest_path = tmp_path / "edge-telemetry.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "autotldr-edge-telemetry-corpus-v1"
    assert len(manifest["files"]) == 5

    extraction = acquire([root])
    assert extraction.kind == "collection"
    assert {item["kind"] for item in extraction.meta["inputs"]} == {
        "markdown",
        "netcdf",
        "parquet",
        "sqlite",
        "xlsx",
    }
    assert len(extraction.meta["inputs"]) == 5
    assert len(extraction.units) >= 40
    assert len(extraction.relations) >= 30
    assert len(extraction.gaps) >= 4

    evidence = "\n".join(unit.content for unit in extraction.units)
    assert "B7*B4*(1-B5/100)" in evidence
    assert "foreign key" in evidence.casefold()
    assert "throughput_unit" in evidence
    assert "payload values were not read" in evidence
    assert "platform-team" not in evidence
    assert "2980.8" not in evidence

    refused = subprocess.run(
        [sys.executable, str(builder), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "refusing to overwrite" in refused.stderr
