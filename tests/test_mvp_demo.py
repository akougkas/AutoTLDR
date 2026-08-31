"""The MVP demo must call the model once and render every shape from that result.

The demo is the artifact a reader is asked to trust, so the two properties that
make it trustworthy are asserted directly: exactly one completion request
reaches the model seam, and all six output shapes are projections of the single
accepted ``Extraction`` rather than six independently generated answers.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import unicodedata
from pathlib import Path

import pytest

from autotldr.synthesis import offline_test_transport_attestation

ROOT = Path(__file__).resolve().parents[1]
DEMO_PATH = ROOT / "examples" / "mvp_demo.py"

_SPEC = importlib.util.spec_from_file_location("autotldr_mvp_demo", DEMO_PATH)
assert _SPEC is not None and _SPEC.loader is not None
mvp_demo = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mvp_demo
_SPEC.loader.exec_module(mvp_demo)

MODEL = "autotldr-test-instance"
SHAPES = ("ansi", "md", "html", "json", "jsonl", "pdf")


class _EvidenceAwareClient:
    """Answer with claims that cite real IDs taken from the request itself."""

    def __init__(self, *, claims: int = 2) -> None:
        self.attestation = offline_test_transport_attestation()
        self.calls = 0
        self.requests: list[bytes] = []
        self.claims = claims

    def complete(
        self,
        request_body: bytes,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        self.calls += 1
        self.requests.append(request_body)
        body = json.loads(request_body)
        user = body["messages"][1]["content"]
        pack = json.loads(user.split("\n", 1)[1])
        unit_ids = [unit["id"] for unit in pack["evidence"]["units"]]
        assert unit_ids, "evidence pack must carry addressable units"

        claims = [
            {
                "content": (
                    f"Claim {index} states one grounded fact about the collection "
                    "and cites the evidence it came from."
                ),
                "evidence_unit_ids": [unit_ids[index % len(unit_ids)]],
            }
            for index in range(min(self.claims, len(unit_ids)))
        ]
        content = json.dumps({"claims": claims}, ensure_ascii=False)
        envelope = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1_788_000_000,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
            },
        }
        return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def collection(tmp_path: Path) -> Path:
    root = tmp_path / "borealis-lite"
    root.mkdir()
    (root / "overview.md").write_text(
        "# Borealis\n\n"
        "Borealis monitors the Station Alpha cooling reservoir.\n"
        "The operating target for reservoir_temp_c is 18.0 degrees C.\n"
        "See calibration/current.csv for the calibration inputs.\n",
        encoding="utf-8",
    )
    (root / "config.json").write_text(
        json.dumps(
            {
                "station_id": "alpha",
                "reservoir_temp_c": 18.0,
                "pressure_kpa_ceiling": 240,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "station.csv").write_text(
        "station_id,reservoir_temp_c,pressure_kpa\nalpha,18.0,231\n", encoding="utf-8"
    )
    return root


def _run(collection: Path, tmp_path: Path, client: _EvidenceAwareClient) -> dict:
    return mvp_demo.generate(
        collection,
        model=MODEL,
        output_dir=tmp_path / "artifacts",
        evidence_budget=12_000,
        timeout=30.0,
        max_output_tokens=512,
        client=client,
    )


def test_demo_calls_the_model_exactly_once(collection: Path, tmp_path: Path) -> None:
    pytest.importorskip("pymupdf")
    client = _EvidenceAwareClient()

    manifest = _run(collection, tmp_path, client)

    assert client.calls == 1
    assert manifest["synthesis"]["model_calls"] == 1
    assert manifest["synthesis"]["outcome"] == "success"
    assert manifest["synthesis"]["used_fallback"] is False
    assert manifest["synthesis"]["validation"]["status"] == "accepted"
    assert manifest["synthesis"]["fallback"] == {"used": False, "reason": None}


def test_demo_writes_all_six_shapes_with_exact_byte_accounting(
    collection: Path, tmp_path: Path
) -> None:
    pytest.importorskip("pymupdf")
    client = _EvidenceAwareClient()

    manifest = _run(collection, tmp_path, client)

    assert {record["format"] for record in manifest["artifacts"]} == set(SHAPES)
    for record in manifest["artifacts"]:
        path = Path(record["path"])
        payload = path.read_bytes()
        assert path.stat().st_size == record["bytes"] == len(payload)
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
    assert (tmp_path / "artifacts" / "manifest.json").is_file()


def test_all_six_shapes_project_one_accepted_extraction(
    collection: Path, tmp_path: Path
) -> None:
    """Cross-artifact identity, not six independently generated answers."""

    pymupdf = pytest.importorskip("pymupdf")
    client = _EvidenceAwareClient()

    manifest = _run(collection, tmp_path, client)
    artifacts = tmp_path / "artifacts"

    document = json.loads((artifacts / "autotldr-mvp.json").read_text("utf-8"))
    records = [
        json.loads(line)
        for line in (artifacts / "autotldr-mvp.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    header = records[0]
    unit_records = [record for record in records if record["type"] == "unit"]

    assert [unit["id"] for unit in document["units"]] == [
        record["id"] for record in unit_records
    ]
    assert header["summary_claims"] == document["summary_claims"]

    claim_texts = [claim["content"] for claim in manifest["summary_claims"]]
    assert claim_texts, "the accepted synthesis must carry at least one claim"
    assert [claim["content"] for claim in document["summary_claims"]] == claim_texts

    ansi = (artifacts / "autotldr-mvp.ansi").read_text("utf-8")
    markdown = (artifacts / "autotldr-mvp.md").read_text("utf-8")
    html = (artifacts / "autotldr-mvp.html").read_text("utf-8")
    with pymupdf.open(artifacts / "autotldr-mvp.pdf") as pdf:
        pdf_text = "\n".join(page.get_text() for page in pdf)
    # A claim can straddle a page break, so the page footers have to come out
    # before the flowed text is compared. Extraction also returns the font's
    # `fi` ligature and turns a break after a slash into a space, so the
    # comparison is on the character sequence rather than on layout.
    pdf_flow = " ".join(re.sub(r"Page \d+ of \d+", " ", pdf_text).split())
    squashed = re.sub(r"\s+", "", unicodedata.normalize("NFKC", pdf_flow))

    for text in claim_texts:
        assert text in ansi
        assert text in markdown
        assert text in html
        assert re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)) in squashed

    # PyMuPDF Story implements no attr(), so CSS generated content that reads an
    # attribute renders the literal word instead of the value.
    assert "attrWhat matters" not in pdf_flow
    assert "01 What matters" in pdf_flow

    # The manifest's claims must resolve inside the machine artifact, and their
    # origins must be exactly the origins of the units they cite.
    units_by_id = {unit["id"]: unit for unit in document["units"]}
    for claim in manifest["summary_claims"]:
        assert claim["evidence_unit_ids"]
        derived: list[dict] = []
        for unit_id in claim["evidence_unit_ids"]:
            assert unit_id in units_by_id
            origin = units_by_id[unit_id]["origin"]
            record = {
                "source": origin["source"],
                "ref": origin["ref"],
                "char_span": origin.get("char_span"),
            }
            if record not in derived:
                derived.append(record)
        assert claim["origins"] == derived


def test_demo_records_provenance_for_every_acquired_input(
    collection: Path, tmp_path: Path
) -> None:
    pytest.importorskip("pymupdf")
    client = _EvidenceAwareClient()

    manifest = _run(collection, tmp_path, client)

    assert manifest["input_count"] == len(manifest["inputs"]) == 3
    for record in manifest["inputs"]:
        assert set(record) >= {"source", "kind", "bytes", "sha256"}
        name = record["source"].split("/")[-1]
        payload = (collection / name).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
        assert len(payload) == record["bytes"]
    assert manifest["collection"]["counts"]["declined"] == 0
    assert manifest["synthesis"]["evidence_unit_ids"]
    assert manifest["synthesis"]["record_sha256"]


def test_demo_reports_a_refused_run_without_writing_artifacts(
    collection: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rejected response must fail the demo rather than quietly fall back."""

    class _BadClient(_EvidenceAwareClient):
        def complete(self, request_body: bytes, **kwargs) -> bytes:
            self.calls += 1
            return json.dumps(
                {
                    "id": "chatcmpl-bad",
                    "object": "chat.completion",
                    "created": 1_788_000_000,
                    "model": "some-other-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "{}"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode("utf-8")

    from autotldr.synthesis import SynthesisRunError

    client = _BadClient()
    with pytest.raises(SynthesisRunError) as raised:
        _run(collection, tmp_path, client)

    assert client.calls == 1
    assert raised.value.model_run["outcome"] == "error-invalid-response"
    assert (
        raised.value.model_run["validation"]["error_code"] == "served-model-mismatch"
    )
    assert not (tmp_path / "artifacts").exists()
