"""Private-alpha artifacts must be installable, privacy-safe, and gateable."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


def _fake_distributions(root: Path, *, version: str = "0.1.1") -> Path:
    root.mkdir()
    wheel = root / f"autotldr-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"autotldr-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: autotldr\nVersion: {version}\n",
        )
    sdist = root / f"autotldr-{version}.tar.gz"
    payload = f"Metadata-Version: 2.4\nName: autotldr\nVersion: {version}\n".encode()
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo(f"autotldr-{version}/PKG-INFO")
        member.size = len(payload)
        member.mtime = 0
        archive.addfile(member, io.BytesIO(payload))
    return root


def _session(participant_id: str, cohort: str) -> dict[str, object]:
    return {
        "schema": "autotldr-first-user-session-v1",
        "participant_id": participant_id,
        "cohort": cohort,
        "artifact_kind": "mixed technical folder",
        "source_content_recorded": False,
        "doctor_green": True,
        "intervention_count": 0,
        "install_to_green_doctor_seconds": 120,
        "doctor_to_useful_outcome_seconds": 90,
        "outcome": "valid-cited-tldr",
        "claim_judgments": {
            "useful": 3,
            "incorrect": 0,
            "unsupported": 0,
            "redundant": 0,
        },
        "citation_samples": [{"resolves": True, "entails": True}],
        "understands_gaps": True,
        "understands_detail": True,
        "authority_surprise": False,
        "would_reuse": True,
        "first_missing_capability": {"category": "none", "note": ""},
    }


def _write_sessions(root: Path) -> list[Path]:
    cohorts = ["developer", "developer", "research-data", "research-data", "spreadsheet"]
    paths = []
    for index, cohort in enumerate(cohorts, start=1):
        path = root / f"session-{index}.json"
        path.write_text(
            json.dumps(_session(f"participant-{index}", cohort)),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def test_private_alpha_bundle_is_versioned_checksummed_and_reproducible(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    dist = _fake_distributions(tmp_path / "dist")
    script = repository / "scripts" / "build_alpha_bundle.py"
    support = "private GitHub security advisory"
    outputs = [tmp_path / "bundle-one", tmp_path / "bundle-two"]
    reports = []

    for output in outputs:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                str(dist),
                str(output),
                "--support",
                support,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        reports.append(json.loads(completed.stdout))

    first = outputs[0]
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "autotldr-private-alpha-bundle-v1"
    assert manifest["version"] == "0.1.1"
    assert manifest["support"] == support
    guide = (first / "README.md").read_text(encoding="utf-8")
    assert "AutoTLDR 0.1.1 private-alpha guide" in guide
    assert support in guide
    assert "{{" not in guide

    checksums = (first / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert checksums
    for line in checksums:
        expected, relative = line.split("  ", 1)
        payload = first / relative
        assert payload.is_file()
        assert hashlib.sha256(payload.read_bytes()).hexdigest() == expected

    assert reports[0]["archive_sha256"] == reports[1]["archive_sha256"]
    with zipfile.ZipFile(str(first) + ".zip") as archive:
        names = archive.namelist()
    assert names
    assert all(name.startswith("autotldr-0.1.1-private-alpha/") for name in names)

    refused = subprocess.run(
        [
            sys.executable,
            str(script),
            str(dist),
            str(first),
            "--support",
            support,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 1
    assert "refusing to overwrite" in refused.stderr


def test_alpha_session_gate_passes_only_the_preregistered_cohort(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "evaluate_alpha_sessions.py"
    sessions = _write_sessions(tmp_path)

    completed = subprocess.run(
        [sys.executable, str(script), "--json", *(str(path) for path in sessions)],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["schema"] == "autotldr-first-user-gate-v1"
    assert report["passed"] is True
    assert report["criteria"]["would_reuse"]["observed"] == 5

    failed_record = json.loads(sessions[0].read_text(encoding="utf-8"))
    failed_record.update(
        {
            "doctor_green": False,
            "intervention_count": 1,
            "install_to_green_doctor_seconds": None,
            "doctor_to_useful_outcome_seconds": None,
            "outcome": "empty-success",
            "citation_samples": [],
            "authority_surprise": True,
            "would_reuse": False,
        }
    )
    sessions[0].write_text(json.dumps(failed_record), encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, str(script), "--json", *(str(path) for path in sessions)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 1, failed.stderr
    failed_report = json.loads(failed.stdout)
    assert failed_report["passed"] is False
    assert failed_report["criteria"]["valid_tldr_or_actionable_decline"]["passed"] is False
    assert failed_report["criteria"]["sampled_citations_resolve"]["passed"] is False
    assert failed_report["criteria"]["no_authority_surprise"]["passed"] is False


def test_alpha_session_records_reject_source_content_fields(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "evaluate_alpha_sessions.py"
    sessions = _write_sessions(tmp_path)
    unsafe = json.loads(sessions[0].read_text(encoding="utf-8"))
    unsafe["source_path"] = "/private/research/data.parquet"
    sessions[0].write_text(json.dumps(unsafe), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(script), "--json", *(str(path) for path in sessions)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "unknown=['source_path']" in completed.stderr


def test_session_template_is_privacy_safe_and_schema_complete():
    repository = Path(__file__).resolve().parents[1]
    template = json.loads(
        (repository / "acceptance" / "session-template.json").read_text(encoding="utf-8")
    )
    assert template["schema"] == "autotldr-first-user-session-v1"
    assert template["source_content_recorded"] is False
    assert "source_path" not in template
    assert "source_content" not in template
