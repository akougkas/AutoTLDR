"""Semantic, human-adjudicated closure tests for synthesis benchmark v2."""

from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVALUATE_PATH = ROOT / "benchmarks" / "synthesis" / "evaluate.py"
BUILD_HERO_PATH = ROOT / "benchmarks" / "synthesis" / "build_hero.py"
RAW_ONLY_SQLITE_SENTINEL = "AUTOTLDR_RAW_ONLY_SQLITE_20260830_7F3C91"
RAW_ONLY_CANARIES = {
    "capacity.xlsx": "AUTOTLDR_RAW_ONLY_XLSX_20260830_5A1D2C",
    "measurements.parquet": "AUTOTLDR_RAW_ONLY_PARQUET_20260830_8E4B17",
    "safety.sqlite": RAW_ONLY_SQLITE_SENTINEL,
    "analytics.duckdb": "AUTOTLDR_RAW_ONLY_DUCKDB_20260830_C2A845",
    "experiments.h5": "AUTOTLDR_RAW_ONLY_HDF5_20260830_D6E913",
    "forecast.nc": "AUTOTLDR_RAW_ONLY_NETCDF_20260830_B4F720",
}
CANARY_ID_BY_FILE = {
    "capacity.xlsx": "xlsx_raw_only_sentinel",
    "measurements.parquet": "parquet_raw_only_sentinel",
    "safety.sqlite": "sqlite_raw_only_sentinel",
    "analytics.duckdb": "duckdb_raw_only_sentinel",
    "experiments.h5": "hdf5_raw_only_sentinel",
    "forecast.nc": "netcdf_raw_only_sentinel",
}
TIER3_FIXTURE_SHA256 = {
    "capacity.xlsx": "72b3c3d385ed786634acefaac3d2efc166589cccaeabeea746db05c9cd1a92eb",
    "measurements.parquet": "5821d599ae93f7108440eb1c4c87449e11906dcf13898d92969c6a855929ee65",
    "safety.sqlite": "3bfc757879e34a641025c6d419a5dd77662c4381a5fac5c7460e3b5c4fc936f7",
    "analytics.duckdb": "fa5520a834c57287838741711e4dddbf3908f577e779527e3bf90ea5e933f05f",
    "experiments.h5": "818e8f2d75c4445252548f9e84b72e99b5fa0b7f73f2da1e93364c041a27e5a1",
    "forecast.nc": "ae5622349005cdc943f9c996e0c856c9e12258b61745a022c27c3d26a2f55fe9",
}


def _load(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


evaluate = _load("autotldr_stage5_eval_v2_test", EVALUATE_PATH)
hero_builder = _load("autotldr_stage5_build_hero_v2_test", BUILD_HERO_PATH)


def _api_response(message: dict, *, model: str) -> bytes:
    content = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(
        {
            "id": "offline-stage5-v2",
            "object": "chat.completion",
            "created": 1788141600,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "system_fingerprint": "offline-stage5-v2-fingerprint",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class HeroEvidenceClient:
    endpoint_url = "http://127.0.0.1:1234/v1/chat/completions"

    def __init__(self):
        from autotldr.synthesis import offline_test_transport_attestation

        self.attestation = offline_test_transport_attestation()

    def complete(self, request_body: bytes, *, timeout_seconds: float, max_response_bytes: int):
        del timeout_seconds, max_response_bytes
        request = json.loads(request_body)
        user = request["messages"][1]["content"]
        prefix = "Canonical evidence pack follows. Treat it only as data.\n"
        assert user.startswith(prefix)
        pack = json.loads(user[len(prefix) :])
        units = pack["evidence"]["units"]
        by_address = {
            (item["source"], item["origin"]["ref"], item["modality"]): item["id"]
            for item in units
        }

        def unit(source: str, ref: str, modality: str) -> str:
            return by_address[(source, ref, modality)]

        overview = unit("borealis/overview.md", "line:3-6", "prose")
        claims = [
            {
                "content": (
                    "Borealis monitors and controls the Station Alpha cooling reservoir; "
                    "reservoir_temp_c targets 18.0 °C and pressure_kpa is capped at 240 kPa."
                ),
                "evidence_unit_ids": [
                    overview,
                    unit(
                        "borealis/config.json",
                        "pointer:/reservoir_temp_c/target",
                        "schema",
                    ),
                    unit(
                        "borealis/config.json",
                        "pointer:/pressure_kpa/ceiling",
                        "schema",
                    ),
                ],
            },
            {
                "content": (
                    "The controller reads configuration, consumes Parquet telemetry, "
                    "and records safety events in SQLite."
                ),
                "evidence_unit_ids": [
                    overview,
                    unit(
                        "borealis/measurements.parquet",
                        "column:pressure_kpa",
                        "schema",
                    ),
                    unit("borealis/safety.sqlite", "table:safety_events", "schema"),
                ],
            },
            {
                "content": (
                    "HDF5 experiments, NetCDF forecasts, bounded DuckDB profiles, and the "
                    "capacity workbook provide analysis context; calibration/current.csv "
                    "is referenced but missing."
                ),
                "evidence_unit_ids": [
                    unit("borealis/overview.md", "line:8-11", "prose"),
                    unit(
                        "borealis/experiments.h5",
                        "/experiments/run_001/pressure_kpa",
                        "schema",
                    ),
                    unit("borealis/forecast.nc", "/pressure_kpa", "schema"),
                    unit("borealis/capacity.xlsx", "Inputs!B4", "record"),
                    unit(
                        "borealis/appendix.zip!/appendix/calibration.md",
                        "line:3",
                        "prose",
                    ),
                ],
            },
        ]
        return _api_response({"claims": claims}, model=request["model"])


def _fact_claim_map(packet: dict) -> dict[str, list[str]]:
    claims = packet["claims"]
    assert len(claims) == 3
    return {
        "purpose": [claims[0]["id"]],
        "operating_constraints": [claims[0]["id"]],
        "data_flow": [claims[1]["id"]],
        "scientific_and_capacity_context": [claims[2]["id"]],
        "missing_calibration": [claims[2]["id"]],
    }


def _review(
    context: dict,
    reviewer_id: str,
    *,
    fact_overrides: dict[tuple[str, str], str] | None = None,
    summary_overrides: dict[tuple[str, str], bool] | None = None,
) -> dict:
    fact_overrides = fact_overrides or {}
    summary_overrides = summary_overrides or {}
    fact_rows = []
    claim_rows = []
    summary_rows = []
    for packet in context["packets"]["packets"]:
        supporting = _fact_claim_map(packet)
        for fact in context["truth"]["facts"]:
            key = (packet["packet_id"], fact["id"])
            entailment = fact_overrides.get(key, "entailed")
            fact_rows.append(
                {
                    "packet_id": packet["packet_id"],
                    "fact_id": fact["id"],
                    "supporting_claim_ids": (
                        supporting[fact["id"]] if entailment == "entailed" else []
                    ),
                    "entailment": entailment,
                    "rationale": "The cited text directly supports every named semantic slot.",
                }
            )
        for claim in packet["claims"]:
            detected_canaries = sorted(
                item["id"]
                for item in context["truth"]["payload_canaries"]
                if item["literal"] in claim["content"]
            )
            claim_rows.append(
                {
                    "packet_id": packet["packet_id"],
                    "claim_id": claim["id"],
                    "every_citation_relevant": True,
                    "atomic_claim": True,
                    "hard_negative_ids": [],
                    "payload_canary_ids": detected_canaries,
                    "other_raw_payload_leak": False,
                    "usefulness": "useful",
                    "rationale": "Every clause is supported and contributes to the TLDR.",
                }
            )
        summary = {
            "packet_id": packet["packet_id"],
            "coherent": True,
            "concise": True,
            "integrates_sources": True,
            "useful_over_inventory": True,
            "rationale": "The three claims form a concise cross-source explanation.",
        }
        for gate in (
            "coherent",
            "concise",
            "integrates_sources",
            "useful_over_inventory",
        ):
            summary[gate] = summary_overrides.get(
                (packet["packet_id"], gate), summary[gate]
            )
        summary_rows.append(summary)
    record = {
        "schema": evaluate.REVIEW_ARTIFACT_V2_SCHEMA,
        "benchmark": context["freeze"]["benchmark"],
        "freeze_sha256": context["freeze"]["freeze_sha256"],
        "packet_set_sha256": context["packets"]["packet_set_sha256"],
        "review_schema_sha256": context["freeze"]["review_schema_v2"]["sha256"],
        "reviewer_id": reviewer_id,
        "human_authored": True,
        "completed_at": "2026-08-30T18:00:00-05:00",
        "fact_rows": fact_rows,
        "claim_rows": claim_rows,
        "summary_rows": summary_rows,
    }
    return evaluate.finalize_review_artifact_v2(record)


def _refinalize(record: dict) -> dict:
    updated = copy.deepcopy(record)
    updated.pop("artifact_sha256", None)
    return evaluate.finalize_review_artifact_v2(updated)


def _raw_station_description(path: Path) -> str:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT description FROM stations WHERE station_id = ?",
            ("alpha",),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_raw_canaries_absent(payload: bytes) -> None:
    for literal in RAW_ONLY_CANARIES.values():
        assert literal.encode("utf-8") not in payload


@pytest.fixture(scope="module")
def context(tmp_path_factory):
    directory = tmp_path_factory.mktemp("stage5-v2")
    extraction = evaluate.build_hero_extraction()
    base_policy, _ = evaluate.load_policy()
    policy_v2, _ = evaluate.load_policy_v2()
    truth, _ = evaluate.load_truth_v2()
    review_schema, _ = evaluate.load_review_schema_v2()
    freeze = evaluate.build_freeze_record_v2(extraction=extraction)
    base_freeze_path = directory / "base-freeze.json"
    evaluate.write_freeze(base_freeze_path, freeze["base_freeze"])
    artifacts = []
    for candidate in evaluate.policy_candidates(base_policy):
        artifacts.append(
            evaluate.run_candidate(
                candidate["model_id"],
                directory / f"{candidate['slug']}.json",
                freeze_path=base_freeze_path,
                client_factory=lambda repeat, config: HeroEvidenceClient(),
                extraction=extraction,
            )
        )
    packets = evaluate.build_review_packets_v2(
        artifacts,
        freeze_v2=freeze,
        extraction=extraction,
        base_policy=base_policy,
        truth=truth,
    )
    result = {
        "directory": directory,
        "extraction": extraction,
        "base_policy": base_policy,
        "policy_v2": policy_v2,
        "truth": truth,
        "review_schema": review_schema,
        "freeze": freeze,
        "artifacts": artifacts,
        "packets": packets,
    }
    result["review_a"] = _review(result, "human-reviewer-a")
    result["review_b"] = _review(result, "human-reviewer-b")
    result["adjudication"] = evaluate.adjudicate_reviews_v2(
        result["review_a"],
        result["review_b"],
        packet_set=packets,
        freeze_v2=freeze,
        truth=truth,
        review_schema=review_schema,
        policy_v2=policy_v2,
    )
    return result


def test_v2_sidecars_are_hash_pinned_and_forbid_aggregate_accuracy():
    policy, policy_bytes = evaluate.load_policy_v2()
    truth, truth_bytes = evaluate.load_truth_v2()
    schema, schema_bytes = evaluate.load_review_schema_v2()

    assert evaluate._sha256(policy_bytes) == evaluate.FROZEN_POLICY_V2_SHA256
    assert evaluate._sha256(truth_bytes) == evaluate.FROZEN_TRUTH_V2_SHA256
    assert evaluate._sha256(schema_bytes) == evaluate.FROZEN_REVIEW_SCHEMA_V2_SHA256
    assert policy["eligibility"]["aggregate_accuracy_is_gate"] is False
    assert policy["required_endpoint_class"] == "openai-compatible-zbook-local"
    assert policy["render_budget_scope"] == {
        "classification": "benchmark-complete-audit-wire-v1",
        "includes_model_run_manifest": True,
        "product_acceptance_target": False,
        "review_required_before_product_lock": True,
    }
    assert len(truth["facts"]) == 5
    assert len(truth["hard_negatives"]) == 5
    assert len(truth["payload_canaries"]) == 6
    assert schema["model_generated_review_allowed"] is False


def test_final_tier3_fixtures_and_manifest_are_repeat_build_deterministic(
    tmp_path, monkeypatch
):
    hero_builder.validate()
    checked_payloads = {
        name: (evaluate.HERO_DIR / name).read_bytes() for name in RAW_ONLY_CANARIES
    }
    manifest_payload = evaluate.HERO_MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_payload)
    manifest_entries = {item["path"]: item for item in manifest["files"]}

    assert hero_builder.RAW_ONLY_SQLITE_SENTINEL == RAW_ONLY_SQLITE_SENTINEL
    assert hero_builder.read_raw_canaries() == RAW_ONLY_CANARIES
    assert evaluate._truth_canary_values(
        evaluate.load_truth_v2()[0], hero_dir=evaluate.HERO_DIR
    ) == {
        CANARY_ID_BY_FILE[name]: literal
        for name, literal in RAW_ONLY_CANARIES.items()
    }
    assert evaluate._sha256(manifest_payload) == evaluate.FROZEN_HERO_MANIFEST_SHA256
    for name, checked_payload in checked_payloads.items():
        assert evaluate._sha256(checked_payload) == TIER3_FIXTURE_SHA256[name]
        assert manifest_entries[name] == {
            "bytes": len(checked_payload),
            "path": name,
            "sha256": TIER3_FIXTURE_SHA256[name],
        }

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    monkeypatch.setattr(hero_builder, "HERO", first_root)
    hero_builder._build_locked_tier3()
    first_payloads = {
        name: (first_root / name).read_bytes() for name in RAW_ONLY_CANARIES
    }
    first_canaries = hero_builder.read_raw_canaries()
    first_manifest = hero_builder._manifest()
    monkeypatch.setattr(hero_builder, "HERO", second_root)
    hero_builder._build_locked_tier3()
    second_payloads = {
        name: (second_root / name).read_bytes() for name in RAW_ONLY_CANARIES
    }
    second_canaries = hero_builder.read_raw_canaries()
    second_manifest = hero_builder._manifest()

    assert first_payloads == second_payloads == checked_payloads
    assert {
        name: evaluate._sha256(payload) for name, payload in first_payloads.items()
    } == TIER3_FIXTURE_SHA256
    assert first_manifest == second_manifest
    assert first_canaries == second_canaries == RAW_ONLY_CANARIES


def test_v2_freeze_binds_addresses_sidecars_failures_and_is_write_once(context, tmp_path):
    freeze = context["freeze"]
    verified, observed_extraction = evaluate.verify_freeze_record_v2(
        freeze,
        extraction=context["extraction"],
    )
    assert verified == freeze
    assert observed_extraction is context["extraction"]
    assert freeze["frozen_before_model_outputs"] is True
    assert freeze["aggregate_accuracy_computed"] is False
    assert freeze["temporary_canary_ids"] == []
    assert freeze["live_freeze_eligible"] is True
    assert len(freeze["truth_binding"]["facts"]) == 5
    assert all(
        any(item["available_in_evidence_pack"] for item in fact["evidence_sets"])
        for fact in freeze["truth_binding"]["facts"]
    )
    assert freeze["failure_injections"]["result_sha256"]
    assert len(freeze["failure_injections"]["cases"]) == 11
    assert len(freeze["truth_binding"]["payload_canaries"]) == 6
    assert all(
        item["present_at_native_locator"]
        and item["absent_from_complete_extraction"]
        and item["absent_from_evidence_pack"]
        for item in freeze["truth_binding"]["payload_canaries"]
    )

    target = tmp_path / "freeze-v2.json"
    evaluate.write_freeze(target, freeze)
    with pytest.raises(evaluate.EvaluationError, match="overwrite"):
        evaluate.write_freeze(target, freeze)


def test_v2_live_freeze_has_one_final_canary_per_locked_tier3_source(context):
    canaries = context["truth"]["payload_canaries"]
    assert {item["status"] for item in canaries} == {"final"}
    assert {item["id"] for item in canaries} == set(CANARY_ID_BY_FILE.values())
    assert {
        item["source"].removeprefix("borealis/"): item["literal"]
        for item in canaries
    } == RAW_ONLY_CANARIES
    assert len({item["native_locator"] for item in canaries}) == 6
    assert context["freeze"]["temporary_canary_ids"] == []
    assert context["freeze"]["live_freeze_eligible"] is True


def test_final_tier3_canaries_are_raw_only_across_extraction_requests_and_renders(
    context,
):
    assert hero_builder.read_raw_canaries() == RAW_ONLY_CANARIES
    assert _raw_station_description(evaluate.HERO_DIR / "safety.sqlite") == (
        RAW_ONLY_SQLITE_SENTINEL
    )
    assert evaluate._truth_canary_values(
        context["truth"], hero_dir=evaluate.HERO_DIR
    ) == {
        CANARY_ID_BY_FILE[name]: literal
        for name, literal in RAW_ONLY_CANARIES.items()
    }

    extraction = context["extraction"]
    complete_record = evaluate.extraction_record(extraction)
    for surface in ("units", "relations", "gaps", "summary_claims"):
        _assert_raw_canaries_absent(evaluate._canonical_bytes(complete_record[surface]))
    _assert_raw_canaries_absent(evaluate._canonical_bytes(extraction.meta))
    _assert_raw_canaries_absent(evaluate._canonical_bytes(
        {"extraction": complete_record, "meta": extraction.meta}
    ))

    unit_by_address = {
        (unit.source, unit.origin.ref): unit for unit in extraction.units
    }
    useful_addresses = {
        "borealis/capacity.xlsx": "Inputs!B4",
        "borealis/measurements.parquet": "column:pressure_kpa",
        "borealis/safety.sqlite": "table:safety_events",
        "borealis/analytics.duckdb": "table:main.telemetry_profile",
        "borealis/experiments.h5": "/experiments/run_001/pressure_kpa",
        "borealis/forecast.nc": "/pressure_kpa",
    }
    useful_units = {
        source: unit_by_address[(source, ref)]
        for source, ref in useful_addresses.items()
    }
    assert len(useful_units) == 6
    assert useful_units["borealis/capacity.xlsx"].meta["input"] is True
    assert useful_units["borealis/measurements.parquet"].meta["values"] == 3
    assert useful_units["borealis/safety.sqlite"].meta["columns"] == 6
    assert useful_units["borealis/analytics.duckdb"].meta["estimated_rows"] == 1
    assert useful_units["borealis/experiments.h5"].meta["payload_read"] is False
    assert useful_units["borealis/forecast.nc"].meta["payload_read"] is False

    raw_profile_addresses = {
        "borealis/capacity.xlsx": "_raw_canary!A1:A1",
        "borealis/measurements.parquet": "column:raw_note",
        "borealis/safety.sqlite": "table:stations#column:description",
        "borealis/analytics.duckdb": "table:main.telemetry_profile#column:raw_note",
        "borealis/experiments.h5": "/experiments/run_001/raw_note",
        "borealis/forecast.nc": "/raw_note",
    }
    raw_profiles = {
        source: unit_by_address[(source, ref)]
        for source, ref in raw_profile_addresses.items()
    }
    assert raw_profiles["borealis/capacity.xlsx"].meta == {
        "sheet": "_raw_canary",
        "sheet_summary": True,
        "definition_cue": True,
        "rows": 1,
        "columns": 1,
        "populated": 1,
        "formulas": 0,
    }
    assert raw_profiles["borealis/measurements.parquet"].meta["values"] == 3
    assert raw_profiles["borealis/measurements.parquet"].meta["nulls"] == 2
    assert raw_profiles["borealis/measurements.parquet"].meta["min"] is None
    assert raw_profiles["borealis/measurements.parquet"].meta["max"] is None
    assert raw_profiles["borealis/safety.sqlite"].meta["sample_length_min"] == len(
        RAW_ONLY_CANARIES["safety.sqlite"]
    )
    assert raw_profiles["borealis/analytics.duckdb"].meta[
        "sample_length_min"
    ] == len(RAW_ONLY_CANARIES["analytics.duckdb"])
    assert raw_profiles["borealis/experiments.h5"].meta["payload_read"] is False
    assert raw_profiles["borealis/forecast.nc"].meta["payload_read"] is False
    _assert_raw_canaries_absent(
        evaluate._canonical_bytes(
            {
                source: {"content": unit.content, "meta": unit.meta}
                for source, unit in raw_profiles.items()
            }
        )
    )

    from autotldr.render import BudgetTooSmall, render
    from autotldr.synthesis import build_chat_request, build_evidence_pack

    pack = build_evidence_pack(
        extraction,
        budget_bytes=context["base_policy"]["evidence_pack"]["max_bytes"],
    )
    assert useful_units["borealis/safety.sqlite"].id in pack.unit_ids
    _assert_raw_canaries_absent(pack.to_bytes())
    selection = pack.selection_record()
    assert selection["dropped_unit_ids"] == [
        item["unit_id"] for item in selection["dropped_units"]
    ]
    for dropped in selection["dropped_units"]:
        assert set(dropped) == {
            "id",
            "canonical_index",
            "unit_id",
            "source",
            "origin",
            "modality",
            "role",
            "content_sha256",
            "content_bytes",
        }
        assert "content" not in dropped and "meta" not in dropped
        assert len(dropped["content_sha256"]) == 64
        assert dropped["content_bytes"] > 0
        assert not dropped["source"].startswith(("/", "file:"))
        assert "/home/" not in dropped["source"]
        assert set(dropped["origin"]) == {"source", "ref", "char_span"}
        assert dropped["origin"]["source"] == dropped["source"]
    _assert_raw_canaries_absent(evaluate._canonical_bytes(selection["dropped_units"]))
    for candidate in evaluate.policy_candidates(context["base_policy"]):
        config = evaluate.synthesis_config(
            context["base_policy"], candidate["model_id"]
        )
        _assert_raw_canaries_absent(build_chat_request(pack, config))

    for artifact in context["artifacts"]:
        _assert_raw_canaries_absent(evaluate._canonical_bytes(artifact))
        artifact_path = context["directory"] / f"{artifact['candidate']['slug']}.json"
        _assert_raw_canaries_absent(artifact_path.read_bytes())
        for repeat in artifact["repeats"]:
            request = evaluate._decode_bytes_record(
                repeat["request"], label="offline candidate request"
            )
            response = evaluate._decode_bytes_record(
                repeat["raw_response"], label="offline candidate response"
            )
            assert request is not None
            assert response is not None
            _assert_raw_canaries_absent(request)
            _assert_raw_canaries_absent(response)
            _assert_raw_canaries_absent(evaluate._canonical_bytes(repeat["claims"]))
            _assert_raw_canaries_absent(
                evaluate._canonical_bytes(repeat["model_manifest"])
            )

            rendered_extraction = evaluate._repeat_extraction_v2(extraction, repeat)
            _assert_raw_canaries_absent(evaluate._canonical_bytes(
                {
                    "extraction": evaluate.extraction_record(rendered_extraction),
                    "meta": rendered_extraction.meta,
                }
            ))
            expected_infeasible = {
                ("ansi", "minimum"),
                ("ansi", "compact"),
                ("md", "minimum"),
                ("md", "compact"),
            }
            observed_infeasible = set()
            for shape, budgets in context["policy_v2"]["render_budgets"].items():
                for level, budget in budgets.items():
                    try:
                        rendered = render(
                            rendered_extraction,
                            output=shape,
                            budget=budget,
                            cite=True,
                            color=False,
                        )
                    except BudgetTooSmall:
                        observed_infeasible.add((shape, level))
                        continue
                    _assert_raw_canaries_absent(rendered.encode("utf-8"))
            assert observed_infeasible == expected_infeasible


def test_truth_binding_resolves_addresses_and_rejects_non_canary(context):
    truth = copy.deepcopy(context["truth"])
    truth["payload_canaries"][0]["literal"] = "Borealis"
    selected = context["freeze"]["base_freeze"]["evidence_pack"]["selection"][
        "selected_unit_ids"
    ]
    from autotldr.synthesis import build_evidence_pack

    pack = build_evidence_pack(context["extraction"], budget_bytes=24_000)
    source_canary_values = evaluate._truth_canary_values(
        truth, hero_dir=evaluate.HERO_DIR
    )
    source_canary_values[truth["payload_canaries"][0]["id"]] = "Borealis"
    with pytest.raises(evaluate.EvaluationError, match="complete extraction"):
        evaluate.resolve_truth_bindings(
            context["extraction"],
            selected,
            truth,
            evidence_pack_bytes=pack.to_bytes(),
            source_payloads=evaluate._truth_source_payloads(
                truth, hero_dir=evaluate.HERO_DIR
            ),
            source_canary_values=source_canary_values,
        )


def test_failure_injections_are_normalized_deterministic_and_tamper_evident(context):
    frozen_cases = context["freeze"]["failure_injections"]["cases"]
    contract_cases = context["policy_v2"]["failure_injection"]["cases"]
    assert [item["id"] for item in frozen_cases] == [
        item["id"] for item in contract_cases
    ]
    for observed, expected in zip(frozen_cases, contract_cases, strict=True):
        assert len(observed["repeats"]) == 2
        for repeat in observed["repeats"]:
            assert repeat["outcome"] == expected["expected_outcome"]
            assert (
                repeat["validation_status"]
                == expected["expected_validation_status"]
            )
            assert repeat["error_class"] == expected["expected_error_class"]
            assert repeat["error_phase"] == expected["expected_error_phase"]
            assert repeat["error_code"] == expected["expected_error_code"]
            assert repeat["response_present"] is expected["response_present"]
            assert repeat["accepted_claim_count"] == 0
            assert repeat["fallback_used"] is False

    validated = evaluate.validate_failure_injection_results(
        context["freeze"]["failure_injections"],
        policy_v2=context["policy_v2"],
        preoutput_binding_sha256=context["freeze"]["preoutput_binding_sha256"],
    )
    assert validated["all_cases_valid"] is True
    assert len(validated["validated_cases"]) == 11

    tampered = copy.deepcopy(context["freeze"]["failure_injections"])
    tampered["cases"][0]["repeats"][0]["outcome"] = "success"
    core = dict(tampered)
    core.pop("result_sha256")
    tampered["result_sha256"] = evaluate._sha256(evaluate._canonical_bytes(core))
    with pytest.raises(evaluate.EvaluationError, match="wrong outcome"):
        evaluate.validate_failure_injection_results(
            tampered,
            policy_v2=context["policy_v2"],
            preoutput_binding_sha256=context["freeze"]["preoutput_binding_sha256"],
        )


def test_review_packets_are_deterministic_blind_and_contain_only_cited_evidence(context):
    second = evaluate.build_review_packets_v2(
        list(reversed(context["artifacts"])),
        freeze_v2=context["freeze"],
        extraction=context["extraction"],
        base_policy=context["base_policy"],
        truth=context["truth"],
    )
    assert second == context["packets"]
    serialized = json.dumps(second, sort_keys=True)
    for candidate in evaluate.policy_candidates(context["base_policy"]):
        assert candidate["name"] not in serialized
        assert candidate["model_id"] not in serialized
    for packet in second["packets"]:
        cited = {
            unit_id
            for claim in packet["claims"]
            for unit_id in claim["evidence_unit_ids"]
        }
        assert {item["id"] for item in packet["cited_evidence"]} == cited


def test_review_validation_requires_exact_fact_claim_and_summary_coverage(context):
    validated = evaluate.validate_review_artifact_v2(
        context["review_a"],
        packet_set=context["packets"],
        freeze_v2=context["freeze"],
        truth=context["truth"],
        review_schema=context["review_schema"],
    )
    assert validated["reviewer_id"] == "human-reviewer-a"

    incomplete = copy.deepcopy(context["review_a"])
    incomplete["fact_rows"].pop()
    core = dict(incomplete)
    core.pop("artifact_sha256")
    incomplete["artifact_sha256"] = evaluate._sha256(evaluate._canonical_bytes(core))
    with pytest.raises(evaluate.EvaluationError, match="do not cover"):
        evaluate.validate_review_artifact_v2(
            incomplete,
            packet_set=context["packets"],
            freeze_v2=context["freeze"],
            truth=context["truth"],
            review_schema=context["review_schema"],
        )


def test_two_review_disagreement_is_unresolved_until_a_third_human(context):
    with pytest.raises(evaluate.EvaluationError, match="must be independent"):
        evaluate.adjudicate_reviews_v2(
            context["review_a"],
            context["review_a"],
            packet_set=context["packets"],
            freeze_v2=context["freeze"],
            truth=context["truth"],
            review_schema=context["review_schema"],
            policy_v2=context["policy_v2"],
        )

    packet_id = context["packets"]["packets"][0]["packet_id"]
    dissent = _review(
        context,
        "human-reviewer-dissent",
        summary_overrides={(packet_id, "coherent"): False},
    )
    unresolved = evaluate.adjudicate_reviews_v2(
        context["review_a"],
        dissent,
        packet_set=context["packets"],
        freeze_v2=context["freeze"],
        truth=context["truth"],
        review_schema=context["review_schema"],
        policy_v2=context["policy_v2"],
    )
    assert unresolved["third_required"] is True
    assert unresolved["third_used"] is False
    assert unresolved["all_packets_resolved"] is False
    assert any(packet_id in item for item in unresolved["unresolved"])

    with pytest.raises(evaluate.EvaluationError, match="must be a third human"):
        evaluate.adjudicate_reviews_v2(
            context["review_a"],
            dissent,
            third_review=context["review_a"],
            packet_set=context["packets"],
            freeze_v2=context["freeze"],
            truth=context["truth"],
            review_schema=context["review_schema"],
            policy_v2=context["policy_v2"],
        )

    third = _review(context, "human-reviewer-third")
    resolved = evaluate.adjudicate_reviews_v2(
        context["review_a"],
        dissent,
        third_review=third,
        packet_set=context["packets"],
        freeze_v2=context["freeze"],
        truth=context["truth"],
        review_schema=context["review_schema"],
        policy_v2=context["policy_v2"],
    )
    assert resolved["third_used"] is True
    assert resolved["all_packets_resolved"] is True
    target = next(item for item in resolved["packets"] if item["packet_id"] == packet_id)
    assert target["summary"]["coherent"] is True


def test_render_audit_reports_frozen_human_budget_infeasibility_exactly(context):
    first_artifact = context["artifacts"][0]
    repeat = first_artifact["repeats"][0]
    extraction = evaluate._repeat_extraction_v2(context["extraction"], repeat)
    alias = context["freeze"]["blind_assignment"]["records"][0]["blind_alias"]
    packet = next(
        item
        for item in context["adjudication"]["packets"]
        if item["blind_candidate_id"] == alias and item["repeat"] == 1
    )
    audit = evaluate.audit_render_matrix(
        extraction,
        policy_v2=context["policy_v2"],
        fact_claim_ids={
            item["id"]: item["supporting_claim_ids"] for item in packet["facts"]
        },
    )
    assert audit["all_cells_passed"] is False
    assert len(audit["cells"]) == 12
    assert {(item["shape"], item["level"]) for item in audit["cells"]} == {
        (shape, level)
        for shape in ("ansi", "md", "json", "jsonl")
        for level in ("minimum", "compact", "complete")
    }
    failed = {
        (item["shape"], item["level"]): item
        for item in audit["cells"]
        if not item["passed"]
    }
    assert {
        key: item["errors"] for key, item in failed.items()
    } == {
        ("ansi", "minimum"): ["budget-too-small:required=40526"],
        ("ansi", "compact"): ["budget-too-small:required=40526"],
        ("md", "minimum"): ["budget-too-small:required=42096"],
        ("md", "compact"): ["budget-too-small:required=42096"],
    }
    assert all(item["used_bytes"] <= item["budget"] for item in audit["cells"])
    for cell in audit["cells"]:
        if not cell["passed"]:
            assert cell["wire_components"] is None
            assert cell["output_sha256"] is None
            assert cell["used_bytes"] == 0
            continue
        components = cell["wire_components"]
        assert components is not None
        assert components["total_bytes"] == cell["used_bytes"]
        assert components["fixed_envelope_bytes"] > 0
        assert components["semantic_evidence_bytes"] >= 0
        assert components["audit_record_bytes"] > 0
        assert sum(
            components[field]
            for field in (
                "fixed_envelope_bytes",
                "semantic_claim_bytes",
                "semantic_evidence_bytes",
                "audit_record_bytes",
            )
        ) == components["total_bytes"]
        if cell["retained_claim_ids"]:
            assert components["semantic_claim_bytes"] > 0


@pytest.mark.parametrize("shape", ["ansi", "md"])
def test_human_render_audit_verifies_observed_full_drop_records(shape):
    from autotldr.render import render
    from autotldr.unit import (
        Extraction,
        GroundedStatement,
        Modality,
        Origin,
        Relation,
        RelationKind,
        Unit,
    )

    kept = Unit(
        source="kept.md",
        modality=Modality.PROSE,
        content="compact evidence",
        origin=Origin("kept.md", "line:1", (0, 16)),
        salience=1.0,
    )
    unsafe_source = "源`\x1b\x7f\u202e🧪.md"
    dropped = Unit(
        source=unsafe_source,
        modality=Modality.PROSE,
        content="x" * 30_000,
        origin=Origin(unsafe_source, "line:`\x07\u2066é", (4, 44)),
        salience=0.0,
    )
    relation = Relation(
        kept.id,
        dropped.id,
        RelationKind.DERIVES_FROM,
        evidence="structural derivation",
    )
    statement = GroundedStatement(
        content="The retained fact is grounded in the omitted appendix.",
        origins=(kept.origin, dropped.origin),
        evidence_unit_ids=(kept.id, dropped.id),
    )
    extraction = Extraction(
        source="adversarial collection",
        kind="collection",
        units=[kept, dropped],
        relations=[relation],
        summary_claims=[statement],
    )
    budget = 8192
    text = render(extraction, output=shape, budget=budget)

    parsed, errors = evaluate._parse_human_render_audit(
        shape, text, extraction, budget
    )
    assert errors == []
    assert parsed["dropped_unit_ids"] == [dropped.id]
    assert parsed["dropped_relation_indexes"] == [0]
    assert parsed["dropped_statement_ids"] == [statement.id]

    prefix = "- drop-v1/" if shape == "ansi" else "  - drop-v1/"

    def inventory(rendered: str) -> dict[str, list[dict]]:
        records = {"units": [], "relations": [], "statements": []}
        plural = {"unit": "units", "relation": "relations", "statement": "statements"}
        for line in rendered.splitlines():
            if line.startswith(prefix):
                kind, payload = line.removeprefix(prefix).split(" ", 1)
                records[plural[kind]].append(json.loads(payload))
        return records

    unit_line = next(
        line for line in text.splitlines() if line.startswith(prefix + "unit ")
    )
    kind, raw_record = unit_line.removeprefix(prefix).split(" ", 1)
    forged_record = json.loads(raw_record)
    forged_record["origin"]["ref"] = "line:forged"
    forged_payload = evaluate._canonical_human_drop_json(forged_record)
    forged_line = f"{prefix}{kind} {forged_payload}"
    stale_digest_text = text.replace(unit_line, forged_line, 1)

    _parsed, stale_errors = evaluate._parse_human_render_audit(
        shape, stale_digest_text, extraction, budget
    )
    assert "human renderer unit drop records differ from extraction" in stale_errors
    assert "human renderer drop-set digest differs from wire inventory" in stale_errors

    old_digest = parsed["drop_digest"]
    forged_digest = evaluate._sha256(
        evaluate._canonical_bytes(inventory(stale_digest_text))
    )
    self_consistent_forgery = stale_digest_text.replace(
        old_digest, forged_digest, 1
    )
    _parsed, forged_errors = evaluate._parse_human_render_audit(
        shape, self_consistent_forgery, extraction, budget
    )
    assert "human renderer unit drop records differ from extraction" in forged_errors
    assert not any("digest" in error for error in forged_errors)


def test_v2_scores_each_fact_from_humans_and_never_from_regex_accuracy(context):
    score = evaluate.score_candidate_v2(
        context["artifacts"][0],
        freeze_v2=context["freeze"],
        extraction=context["extraction"],
        base_policy=context["base_policy"],
        policy_v2=context["policy_v2"],
        truth=context["truth"],
        adjudication=context["adjudication"],
        candidate_order=0,
    )
    assert score["eligible"] is True, score["failed_hard_gates"]
    assert score["aggregate_accuracy_computed"] is False
    assert score["lexical_prefilter_used_as_semantic_verdict"] is False
    assert all(item["entailed_all_repeats"] for item in score["per_fact"])
    assert all(
        item["entailed_repeat_count"] == item["evaluated_repeat_count"] == 2
        for item in score["per_fact"]
    )

    alias = context["freeze"]["blind_assignment"]["records"][0]["blind_alias"]
    target_packets = [
        item
        for item in context["packets"]["packets"]
        if item["blind_candidate_id"] == alias
    ]
    overrides = {
        (packet["packet_id"], "purpose"): "unsupported" for packet in target_packets
    }
    negative_a = _review(context, "human-negative-a", fact_overrides=overrides)
    negative_b = _review(context, "human-negative-b", fact_overrides=overrides)
    negative_adjudication = evaluate.adjudicate_reviews_v2(
        negative_a,
        negative_b,
        packet_set=context["packets"],
        freeze_v2=context["freeze"],
        truth=context["truth"],
        review_schema=context["review_schema"],
        policy_v2=context["policy_v2"],
    )
    negative_score = evaluate.score_candidate_v2(
        context["artifacts"][0],
        freeze_v2=context["freeze"],
        extraction=context["extraction"],
        base_policy=context["base_policy"],
        policy_v2=context["policy_v2"],
        truth=context["truth"],
        adjudication=negative_adjudication,
        candidate_order=0,
    )
    purpose = next(item for item in negative_score["per_fact"] if item["id"] == "purpose")
    assert purpose["entailed_all_repeats"] is False
    assert purpose["repeat_entailed"] == [False, False]
    assert purpose["entailed_repeat_count"] == 0
    assert purpose["evaluated_repeat_count"] == 2
    assert negative_score["eligible"] is False
    assert "repeat-1:fact:purpose" in negative_score["failed_hard_gates"]
    assert negative_score["lexical_prefilter_used_as_semantic_verdict"] is False


def test_hard_negative_and_non_canary_raw_payload_leak_are_separate_hard_gates(context):
    alias = context["freeze"]["blind_assignment"]["records"][0]["blind_alias"]
    packet = next(
        item
        for item in context["packets"]["packets"]
        if item["blind_candidate_id"] == alias and item["repeat"] == 1
    )
    reviews = []
    for source, reviewer_id in (
        (context["review_a"], "human-leak-a"),
        (context["review_b"], "human-leak-b"),
    ):
        review = copy.deepcopy(source)
        review["reviewer_id"] = reviewer_id
        row = next(
            item
            for item in review["claim_rows"]
            if item["packet_id"] == packet["packet_id"]
            and item["claim_id"] == packet["claims"][0]["id"]
        )
        row["hard_negative_ids"] = ["current_alert_or_exceedance"]
        row["other_raw_payload_leak"] = True
        reviews.append(_refinalize(review))
    adjudication = evaluate.adjudicate_reviews_v2(
        reviews[0],
        reviews[1],
        packet_set=context["packets"],
        freeze_v2=context["freeze"],
        truth=context["truth"],
        review_schema=context["review_schema"],
        policy_v2=context["policy_v2"],
    )
    score = evaluate.score_candidate_v2(
        context["artifacts"][0],
        freeze_v2=context["freeze"],
        extraction=context["extraction"],
        base_policy=context["base_policy"],
        policy_v2=context["policy_v2"],
        truth=context["truth"],
        adjudication=adjudication,
        candidate_order=0,
    )

    assert score["eligible"] is False
    assert "repeat-1:no_hard_negative" in score["failed_hard_gates"]
    assert "repeat-1:no_other_raw_payload_leak" in score["failed_hard_gates"]


def test_four_candidate_report_keeps_per_fact_vectors_and_does_not_invent_a_tie_winner(
    context,
):
    report = evaluate.score_candidates_v2(
        context["artifacts"],
        freeze_v2=context["freeze"],
        extraction=context["extraction"],
        base_policy=context["base_policy"],
        policy_v2=context["policy_v2"],
        truth=context["truth"],
        adjudication=context["adjudication"],
    )

    assert report["aggregate_accuracy_computed"] is False
    assert len(report["candidates"]) == 4
    assert report["eligible_candidate_count"] == 4
    assert report["selected_candidate"] is None
    assert report["eligible_candidates"] == [
        item["candidate"] for item in report["candidates"]
    ]
    assert report["selection_note"] == (
        "no winner is inferred when zero or multiple candidates clear every gate"
    )
    assert all(len(item["per_fact"]) == 5 for item in report["candidates"])
    assert all("aggregate_accuracy" not in item for item in report["candidates"])
