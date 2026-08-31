"""Process-level Stage 4 fusion contract over the public CLI."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


def _write_collection(
    root: Path, *, oversized_orphan: bool = False
) -> tuple[Path, Path, Path]:
    root.mkdir(exist_ok=True)
    notes = root / "study.md"
    results = root / "results.csv"
    orphan = root / "old-notes.txt"
    notes.write_text(
        """\
# Relay Throughput Study

The measured table is [results.csv](results.csv).

The addressable metric is `throughput_mbps`.

Deployment also refers to missing-schema.json.
""",
        encoding="utf-8",
    )
    results.write_text(
        "node_id,throughput_mbps,retry_limit\n"
        "alpha,91,3\n"
        "beta,104,3\n",
        encoding="utf-8",
    )
    orphan.write_text(
        (
            "Unconnected historical note.\n\n"
            + ("oversized appendix " * 2500 if oversized_orphan else "Manual review only.")
            + "\n"
        ),
        encoding="utf-8",
    )
    return notes, results, orphan


def _invoke(*args: str | Path, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "autotldr.cli", *(str(arg) for arg in args)],
        input=stdin,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )


def _decode(completed: subprocess.CompletedProcess) -> tuple[str, str]:
    return (
        completed.stdout.decode("utf-8", errors="strict"),
        completed.stderr.decode("utf-8", errors="strict"),
    )


def _manifest(path: Path, kind: str) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "source": str(path),
        "kind": kind,
        "tier": 0,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _assert_grounded_claims(payload: dict) -> None:
    units = {unit["id"]: unit for unit in payload["units"]}
    claims = payload["summary_claims"]
    assert len(claims) == 3
    assert payload["summary"] == " ".join(claim["content"] for claim in claims)
    for claim in claims:
        assert claim["id"]
        assert claim["content"].endswith(".")
        assert claim["origins"]
        assert claim["evidence_unit_ids"]
        assert set(claim["evidence_unit_ids"]) <= set(units)
        evidence_origins = {
            (
                units[unit_id]["origin"]["source"],
                units[unit_id]["origin"]["ref"],
                tuple(units[unit_id]["origin"].get("char_span", ())),
            )
            for unit_id in claim["evidence_unit_ids"]
        }
        cited_origins = {
            (
                origin["source"],
                origin["ref"],
                tuple(origin.get("char_span", ())),
            )
            for origin in claim["origins"]
        }
        assert cited_origins == evidence_origins


def _assert_relation_endpoints(payload: dict) -> None:
    ids = {unit["id"] for unit in payload["units"]}
    assert all(
        relation["src"] in ids and relation["dst"] in ids
        for relation in payload["relations"]
    )


def _jsonl_payload(stdout: str) -> tuple[dict, list[dict], dict]:
    records = [json.loads(line) for line in stdout.splitlines()]
    assert records[0]["type"] == "header"
    assert records[-1]["type"] == "manifest"
    units = [record for record in records[1:-1] if record["type"] == "unit"]
    return records[0], units, records[-1]


def test_repeated_explicit_files_emit_one_exact_grounded_collection(tmp_path):
    sources = _write_collection(tmp_path / "study")

    completed = _invoke(*sources, "--out", "json")
    stdout, stderr = _decode(completed)
    assert completed.returncode == 0, stderr
    assert stderr == ""
    payload = json.loads(stdout)

    assert payload["schema"] == 2
    assert payload["kind"] == "collection"
    assert payload["subject"] == str(sources[0].parent)
    assert payload["manifest"]["models"] == []
    expected_manifests = sorted(
        (
            _manifest(sources[0], "markdown"),
            _manifest(sources[1], "csv"),
            _manifest(sources[2], "text"),
        ),
        key=lambda item: item["source"],
    )
    assert payload["manifest"]["inputs"] == expected_manifests

    anchors = [
        unit
        for unit in payload["units"]
        if unit.get("meta", {}).get("source_anchor") is True
    ]
    assert len(anchors) == len(sources)
    expected_by_source = {
        item["source"]: item for item in expected_manifests
    }
    for anchor in anchors:
        assert anchor["modality"] == "source"
        assert anchor["role"] == "unknown"
        assert anchor["origin"] == {
            "source": anchor["source"],
            "ref": "source",
        }
        assert anchor["meta"]["manifest"] == expected_by_source[anchor["source"]]
        assert anchor["content"].startswith("Source manifest: {")

    _assert_grounded_claims(payload)
    _assert_relation_endpoints(payload)
    assert any(
        relation["kind"] == "references"
        and "fusion.literal-v1" in relation["evidence"]
        for relation in payload["relations"]
    )
    assert {finding["kind"] for finding in payload["gaps"]} >= {
        "unresolved-reference",
    }
    assert not any(finding["kind"] == "orphan" for finding in payload["gaps"])
    assert payload["manifest"]["fusion"]["evaluated_dispositions"]["signals"][
        "orphan-v1"
    ]["status"] == "disable"


def test_cli_collection_is_canonical_under_argument_permutation(tmp_path):
    sources = _write_collection(tmp_path / "study")

    first = _invoke(*sources, "--out", "json")
    second = _invoke(*reversed(sources), "--out", "json")
    first_stdout, first_stderr = _decode(first)
    second_stdout, second_stderr = _decode(second)
    assert first.returncode == second.returncode == 0
    assert first_stderr == second_stderr == ""
    first_payload = json.loads(first_stdout)
    second_payload = json.loads(second_stdout)

    # Acquisition/extraction/fusion durations are the only permitted semantic
    # difference. ``used`` and ``available`` are exact byte counts of the
    # original payload and therefore inherit the removed durations' variable
    # decimal spelling; every other selection field remains deterministic.
    first_payload["manifest"].pop("timings")
    second_payload["manifest"].pop("timings")
    first_selection = first_payload["manifest"].pop("selection")
    second_selection = second_payload["manifest"].pop("selection")
    assert first_payload == second_payload
    first_selection.pop("used")
    first_selection.pop("available")
    second_selection.pop("used")
    second_selection.pop("available")
    assert first_selection == second_selection


def test_one_explicit_source_remains_the_stage_3_path(tmp_path):
    source, _results, _orphan = _write_collection(tmp_path / "study")

    completed = _invoke(source, "--out", "json")
    stdout, stderr = _decode(completed)
    assert completed.returncode == 0, stderr
    payload = json.loads(stdout)

    assert payload["schema"] == 2
    assert payload["subject"] == str(source)
    assert payload["kind"] == "markdown"
    assert payload["summary"].startswith("markdown source with")
    assert payload["summary_claims"] == []
    assert payload["manifest"]["inputs"] == [_manifest(source, "markdown")]
    assert payload["manifest"]["models"] == []
    assert "fusion" not in payload["manifest"]
    assert not any(
        unit.get("meta", {}).get("source_anchor")
        for unit in payload["units"]
    )


def test_multi_source_usage_errors_are_exit_2_and_emit_no_stdout(tmp_path):
    first, second, _orphan = _write_collection(tmp_path / "study")
    cases = (
        (
            (first, second, "--type", "markdown"),
            None,
            "--type is valid only when exactly one source is supplied",
        ),
        ((first, first), None, "duplicate sources are not allowed"),
        (("-", "-"), b"ignored stdin", "may appear at most once"),
    )

    for args, stdin, expected in cases:
        completed = _invoke(*args, stdin=stdin)
        stdout, stderr = _decode(completed)
        assert completed.returncode == 2
        assert stdout == ""
        assert "usage: autotldr" in stderr
        assert expected in stderr


def test_missing_later_source_is_fail_fast_with_no_partial_stdout(tmp_path):
    first, _second, _orphan = _write_collection(tmp_path / "study")
    missing = tmp_path / "study" / "does-not-exist.csv"

    completed = _invoke(first, missing, "--out", "json")
    stdout, stderr = _decode(completed)

    assert completed.returncode == 4
    assert stdout == ""
    assert str(missing) in stderr
    assert "no such file" in stderr


def test_direct_directory_is_acquired_as_a_tier_2_collection(tmp_path):
    directory = tmp_path / "study"
    _write_collection(directory)

    completed = _invoke(directory, "--out", "json")
    stdout, stderr = _decode(completed)

    assert completed.returncode == 0, stderr
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["kind"] == "collection"
    assert payload["subject"] == "study"
    acquisition = payload["manifest"]["collection_acquisitions"][0]
    assert acquisition["kind"] == "directory"
    assert acquisition["counts"]["extracted"] == 3
    _assert_grounded_claims(payload)
    _assert_relation_endpoints(payload)


@pytest.mark.parametrize("shape", ["ansi", "md", "json", "jsonl"])
def test_every_fused_shape_is_grounded_and_self_consistent(tmp_path, shape):
    sources = _write_collection(tmp_path / "study")

    completed = _invoke(*sources, "--out", shape)
    stdout, stderr = _decode(completed)
    assert completed.returncode == 0, stderr
    assert stderr == ""
    assert stdout.encode("utf-8") == completed.stdout

    if shape == "json":
        payload = json.loads(stdout)
        _assert_grounded_claims(payload)
        _assert_relation_endpoints(payload)
        assert payload["manifest"]["selection"]["used"] == len(completed.stdout)
    elif shape == "jsonl":
        header, units, manifest = _jsonl_payload(stdout)
        ids = {unit["id"] for unit in units}
        assert len(header["summary_claims"]) == 3
        assert all(
            set(claim["evidence_unit_ids"]) <= ids
            for claim in header["summary_claims"]
        )
        assert all(
            relation["src"] in ids and relation["dst"] in ids
            for relation in manifest["relations"]
        )
        assert manifest["selection"]["used"] == len(completed.stdout)
    else:
        assert "This " in stdout and "collection" in stdout
        assert "Gaps" in stdout
        assert "Orphans" not in stdout
        assert "missing-schema.json" in stdout
        if shape == "ansi":
            match = re.search(r"(?m)^(\d+)/unlimited portable tokens", stdout)
        else:
            match = re.search(
                r"Portable tokens: \*\*(\d+) / unlimited\*\*", stdout
            )
        assert match is not None
        assert int(match.group(1)) == len(completed.stdout)


@pytest.mark.parametrize("shape", ["ansi", "md", "json", "jsonl"])
def test_enforced_fused_budget_keeps_mandatory_claims_and_findings(tmp_path, shape):
    sources = _write_collection(
        tmp_path / "study", oversized_orphan=True
    )
    unlimited = _invoke(*sources, "--out", shape)
    unlimited_stdout, unlimited_stderr = _decode(unlimited)
    assert unlimited.returncode == 0, unlimited_stderr

    too_small = _invoke(*sources, "--out", shape, "--budget", "1")
    tiny_stdout, tiny_stderr = _decode(too_small)
    assert too_small.returncode == 5
    assert tiny_stdout == ""
    required_match = re.search(
        r"minimum valid addressable output needs (\d+) portable tokens",
        tiny_stderr,
    )
    assert required_match is not None
    required = int(required_match.group(1))
    assert required < len(unlimited.stdout)
    budget = (required + len(unlimited.stdout)) // 2

    bounded = _invoke(*sources, "--out", shape, "--budget", str(budget))
    stdout, stderr = _decode(bounded)
    assert bounded.returncode == 0, stderr
    assert stderr == ""
    assert len(bounded.stdout) <= budget
    assert len(bounded.stdout) < len(unlimited.stdout)

    if shape == "json":
        payload = json.loads(stdout)
        selection = payload["manifest"]["selection"]
        _assert_relation_endpoints(payload)
        assert len(payload["summary_claims"]) == 3
        assert {finding["kind"] for finding in payload["gaps"]} >= {
            "unresolved-reference",
        }
        assert not any(finding["kind"] == "orphan" for finding in payload["gaps"])
        assert selection["used"] == len(bounded.stdout)
        assert selection["requested"] == budget
        assert selection["dropped"]["unit_count"] > 0
    elif shape == "jsonl":
        header, units, manifest = _jsonl_payload(stdout)
        ids = {unit["id"] for unit in units}
        assert len(header["summary_claims"]) == 3
        assert {finding["kind"] for finding in manifest["gaps"]} >= {
            "unresolved-reference",
        }
        assert not any(finding["kind"] == "orphan" for finding in manifest["gaps"])
        assert all(
            relation["src"] in ids and relation["dst"] in ids
            for relation in manifest["relations"]
        )
        assert manifest["selection"]["used"] == len(bounded.stdout)
        assert manifest["selection"]["dropped"]["unit_count"] > 0
    else:
        assert "collection" in stdout
        assert "Orphans" not in stdout and "Gaps" in stdout
        assert "missing-schema.json" in stdout
        assert "dropped 0 units" not in stdout.casefold()
