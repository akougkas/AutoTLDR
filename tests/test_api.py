"""Public acquire → synthesize → render pipeline contracts."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import autotldr
from autotldr.api import AutoTLDRResult, acquire, summarize
from autotldr.router import extract
from autotldr.synthesis import (
    SynthesisConfig,
    SynthesisTimeoutError,
    offline_test_transport_attestation,
)


class _Client:
    def __init__(self, response: bytes | BaseException) -> None:
        self.response = response
        self.attestation = offline_test_transport_attestation()

    def complete(
        self,
        request_body: bytes,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _response(model: str, content: str, evidence_id: str) -> bytes:
    claims = json.dumps(
        {
            "claims": [
                {"content": content, "evidence_unit_ids": [evidence_id]}
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return json.dumps(
        {
            "id": "offline-api-response",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": claims},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_single_file_acquire_is_the_native_router_path(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("# Purpose\n\nThe worker validates signed jobs.\n")

    direct = extract(source)
    public = acquire([source])

    assert public.kind == direct.kind
    assert public.source == direct.source
    assert public.units == direct.units
    assert public.relations == direct.relations
    assert public.gaps == direct.gaps
    assert public.meta["inputs"] == direct.meta["inputs"]

    lazy_public = autotldr.acquire([source])
    assert lazy_public.units == direct.units


def test_multiple_sources_use_the_production_fusion_path(tmp_path):
    guide = tmp_path / "guide.md"
    guide.write_text("# Worker\n\nSee settings.json for worker_limit.\n")
    settings = tmp_path / "settings.json"
    settings.write_text('{"worker_limit": 8}\n')

    result = acquire([guide, settings])

    assert result.kind == "collection"
    assert len(result.meta["inputs"]) == 2
    assert {item["source"] for item in result.meta["inputs"]} == {
        str(guide),
        str(settings),
    }
    unit_ids = {unit.id for unit in result.units}
    assert all({relation.src, relation.dst} <= unit_ids for relation in result.relations)


def test_directory_acquisition_retains_tier2_manifest_and_partial_declines(tmp_path):
    (tmp_path / "good.md").write_text("# Good\n\nAddressable content.\n")
    (tmp_path / "unknown.zzz").write_bytes(b"\x00\x01unsupported")

    result = acquire([tmp_path])

    assert result.kind == "collection"
    assert result.meta["collection_acquisitions"]
    manifest = result.meta["collection_acquisitions"][0]
    assert manifest["source"] == tmp_path.name
    assert manifest["members"][0]["source"] == f"{tmp_path.name}/good.md"
    assert any("unknown.zzz" in gap for gap in result.gaps)
    assert any(
        unit.source == f"{tmp_path.name}/good.md" for unit in result.units
    )


def test_stdin_payload_is_explicit_and_never_read_implicitly():
    with pytest.raises(ValueError, match="requires the stdin payload"):
        acquire(["-"], input_type="markdown")
    with pytest.raises(ValueError, match="requires '-' in sources"):
        acquire(["notes.md"], stdin=b"secret")

    result = acquire(
        ["-"],
        stdin=b"# Stream\n\nBounded input.\n",
        input_type="markdown",
    )
    assert result.source == "<stdin>"
    assert all(unit.origin.source == "<stdin>" for unit in result.units)


def test_summarize_composes_grounded_synthesis_and_exact_render(tmp_path):
    source = tmp_path / "service.md"
    settings = tmp_path / "settings.json"
    source.write_text("# Service\n\nThe service validates signed jobs.\n")
    settings.write_text('{"job_policy": "signed"}\n')
    base = acquire([source, settings])
    model = "zbook-local/test-model"
    response = _response(model, "The service validates signed jobs.", base.units[-1].id)

    result = summarize(
        [source, settings],
        synthesis_config=SynthesisConfig(model=model, evidence_budget_bytes=8_000),
        client=_Client(response),
        output="md",
        budget=12_000,
    )

    assert isinstance(result, AutoTLDRResult)
    assert result.synthesis is not None
    assert result.synthesis.used_fallback is False
    assert len(result.rendered.encode()) <= 12_000
    statement = result.extraction.summary_claims[0]
    unit_by_id = {unit.id: unit for unit in result.extraction.units}
    assert statement.origins == tuple(
        dict.fromkeys(unit_by_id[item].origin for item in statement.evidence_unit_ids)
    )


def test_summarize_preserves_stage4_fallback_and_reports_model_outcome(tmp_path):
    left = tmp_path / "left.md"
    right = tmp_path / "right.json"
    left.write_text("# Run\n\nSee right.json for threshold.\n")
    right.write_text('{"threshold": 0.7}\n')
    deterministic = acquire([left, right])

    result = summarize(
        [left, right],
        synthesis_config=SynthesisConfig(
            model="zbook-local/test-model",
            evidence_budget_bytes=8_000,
        ),
        client=_Client(SynthesisTimeoutError("offline")),
        output="json",
    )

    assert result.synthesis.used_fallback is True
    assert result.extraction.summary_claims == deterministic.summary_claims
    assert result.extraction.meta["models"][-1]["outcome"] == "fallback-timeout"


def test_importing_public_api_keeps_heavy_parsers_and_runtimes_lazy():
    script = """
import sys
import autotldr.api
blocked = {'numpy', 'pyarrow', 'openpyxl', 'fitz', 'duckdb', 'h5py', 'netCDF4'}
seen = sorted(name for name in blocked if name in sys.modules)
print(','.join(seen))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "\n"
