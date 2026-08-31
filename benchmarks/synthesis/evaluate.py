#!/usr/bin/env python3
"""Freeze, run, and score the Stage 5 grounded-synthesis benchmark.

This evaluator has three deliberately separate phases:

``freeze``
    Verify every hero fixture byte, run the production Tier 2 acquisition,
    fusion, and evidence-pack path, then bind the extraction, evidence pack,
    and candidate-specific chat request bodies before any model output exists.

``run-model``
    Rebuild and verify the complete freeze, call production ``synthesize``
    twice with fallback disabled, and preserve the exact request/response
    bytes, grounded claims, and complete production model-run manifests.

``score``
    Re-verify artifacts, report every required fact separately, apply the
    preregistered hard gates, and rank eligible candidates lexicographically.
    Aggregate accuracy is neither computed nor used.

The default endpoint is the frozen ZBook-local LM Studio wire.  This module
does not load or unload models; ``run_local_candidates.py`` owns that guarded
lifecycle and invokes ``run-model`` only while ZBook is the verified preferred
LM Link device.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence


HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "policy.json"
POLICY_V2_PATH = HERE / "policy-v2.json"
TRUTH_V2_PATH = HERE / "truth-v2.json"
REVIEW_SCHEMA_V2_PATH = HERE / "review-schema-v2.json"
HERO_DIR = HERE / "hero" / "borealis"
HERO_MANIFEST_PATH = HERE / "hero" / "manifest.json"
DEFAULT_FREEZE_PATH = HERE / "freeze.json"
DEFAULT_FREEZE_V2_PATH = HERE / "freeze-v2.json"
DEFAULT_OUTPUT_DIR = HERE / "outputs"
DEFAULT_REPORT_PATH = DEFAULT_OUTPUT_DIR / "report.json"
DEFAULT_REVIEW_PACKET_PATH = HERE / "review-packets-v2.json"
DEFAULT_ADJUDICATION_PATH = HERE / "adjudication-v2.json"
DEFAULT_REPORT_V2_PATH = DEFAULT_OUTPUT_DIR / "report-v2.json"

FROZEN_POLICY_SHA256 = (
    "0d513a748809ccf55ce21c499c1e6c055ac881613d47c30282d11b2f0b8c82f5"
)
FROZEN_HERO_MANIFEST_SHA256 = (
    "c3a45bebc13f89a5c5d5e92712157affe325c972f35966a0b83689910c76308a"
)
FROZEN_POLICY_V2_SHA256 = (
    "7f6601ec43d083a196d1d944921d4f0136707fbdb4aaa74b0886eb83595a17ee"
)
FROZEN_TRUTH_V2_SHA256 = (
    "7ba6db37e0bcff1535f34501bb46e755800c9876b96fb99f214597ab44500168"
)
FROZEN_REVIEW_SCHEMA_V2_SHA256 = (
    "04aa8636954dd8c5f29700aa0a10491082c3a84bf0354c7c574d7ca5fab22e1a"
)

FREEZE_SCHEMA = "autotldr-stage5-synthesis-freeze-v1"
CANDIDATE_ARTIFACT_SCHEMA = "autotldr-stage5-synthesis-candidate-v1"
REPORT_SCHEMA = "autotldr-stage5-synthesis-report-v1"
FREEZE_V2_SCHEMA = "autotldr-stage5-synthesis-freeze-v2"
REVIEW_PACKET_V2_SCHEMA = "autotldr-stage5-synthesis-review-packets-v2"
REVIEW_ARTIFACT_V2_SCHEMA = "autotldr-stage5-synthesis-human-review-v2"
ADJUDICATION_V2_SCHEMA = "autotldr-stage5-synthesis-adjudication-v2"
FAILURE_INJECTION_V2_SCHEMA = "autotldr-stage5-synthesis-failure-injections-v2"
RENDER_AUDIT_V2_SCHEMA = "autotldr-stage5-synthesis-render-audit-v2"
REPORT_V2_SCHEMA = "autotldr-stage5-synthesis-report-v2"
RUN_TIMEOUT_SECONDS = 120.0
MAX_RESPONSE_BYTES = 256 * 1024

REQUIRED_HERO_SOURCES = (
    "overview.md",
    "config.json",
    "controller.py",
    "operations.html",
    "pipeline.ipynb",
    "station.csv",
    "capacity.xlsx",
    "measurements.parquet",
    "safety.sqlite",
    "analytics.duckdb",
    "experiments.h5",
    "forecast.nc",
)
TIER3_SUFFIXES = (
    ".xlsx",
    ".xlsm",
    ".parquet",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".duckdb",
    ".h5",
    ".hdf5",
    ".nc",
)
LOCKED_TIER3_CANARY_SOURCES = frozenset(
    {
        "borealis/capacity.xlsx",
        "borealis/measurements.parquet",
        "borealis/safety.sqlite",
        "borealis/analytics.duckdb",
        "borealis/experiments.h5",
        "borealis/forecast.nc",
    }
)


class EvaluationError(RuntimeError):
    """The benchmark or one of its immutable audit bindings is invalid."""


class _CompletionClient(Protocol):
    def complete(
        self,
        request_body: bytes,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes: ...


ClientFactory = Callable[[int, Any], _CompletionClient]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EvaluationError(f"value is not canonical UTF-8 JSON: {exc}") from exc


def _strict_json_bytes(payload: bytes, *, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise EvaluationError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise EvaluationError(f"{label} contains non-JSON constant {value!r}")

    try:
        text = payload.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except EvaluationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"{label} is not strict UTF-8 JSON: {exc}") from exc


def _read_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise EvaluationError(f"cannot read {label} at {path}: {exc}") from exc
    value = _strict_json_bytes(payload, label=label)
    if not isinstance(value, dict):
        raise EvaluationError(f"{label} must contain one JSON object")
    return value, payload


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvaluationError(f"{label} must be a positive integer")
    return value


def _candidate_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not slug:
        raise EvaluationError(f"candidate name {name!r} has no safe slug")
    return slug


def _candidate_identifier(name: str) -> str:
    return f"autotldr-{_candidate_slug(name)}"


def load_policy(path: Path = POLICY_PATH) -> tuple[dict[str, Any], bytes]:
    """Read and validate the already-frozen Stage 5 policy."""

    policy, payload = _read_object(path, label="synthesis policy")
    if path.resolve() == POLICY_PATH.resolve() and _sha256(payload) != FROZEN_POLICY_SHA256:
        raise EvaluationError(
            "the frozen Stage 5 policy changed; create a new benchmark version "
            "instead of silently reusing this evaluator"
        )
    if policy.get("schema") != 1:
        raise EvaluationError("synthesis policy schema must be 1")
    if policy.get("benchmark") != "stage5-grounded-synthesis-v1":
        raise EvaluationError("unexpected synthesis benchmark name")
    if policy.get("frozen_before_model_outputs") is not True:
        raise EvaluationError("policy must state frozen_before_model_outputs=true")
    if policy.get("endpoint") != "http://127.0.0.1:1234/v1":
        raise EvaluationError("Stage 5 endpoint must be the frozen ZBook localhost URL")
    if policy.get("endpoint_policy") != "zbook-local-lmstudio-only":
        raise EvaluationError("Stage 5 endpoint policy is not ZBook-local-only")

    evidence = policy.get("evidence_pack")
    generation = policy.get("generation")
    response = policy.get("response")
    eligibility = policy.get("eligibility")
    selection = policy.get("selection")
    facts = policy.get("required_facts")
    candidates = policy.get("candidate_order")
    for label, value in (
        ("evidence_pack", evidence),
        ("generation", generation),
        ("response", response),
        ("eligibility", eligibility),
        ("selection", selection),
    ):
        if not isinstance(value, dict):
            raise EvaluationError(f"policy.{label} must be an object")
    if not isinstance(facts, list) or not facts:
        raise EvaluationError("policy.required_facts must be a non-empty array")
    if not isinstance(candidates, list) or len(candidates) != 4:
        raise EvaluationError("policy.candidate_order must contain exactly four candidates")

    _positive_int(evidence.get("max_bytes"), "policy.evidence_pack.max_bytes")
    _positive_int(evidence.get("max_units"), "policy.evidence_pack.max_units")
    _positive_int(
        evidence.get("minimum_sources_when_available"),
        "policy.evidence_pack.minimum_sources_when_available",
    )
    if evidence.get("raw_rows_or_arrays_allowed") is not False:
        raise EvaluationError("raw rows or arrays must remain forbidden")
    if generation.get("temperature") != 0.0:
        raise EvaluationError("benchmark generation temperature must remain 0.0")
    if generation.get("repeats") != 2 or generation.get("parallel") != 1:
        raise EvaluationError("benchmark requires exactly two sequential repeats")
    for key in ("max_tokens", "context_length"):
        _positive_int(generation.get(key), f"policy.generation.{key}")
    if not isinstance(generation.get("seed"), int) or isinstance(
        generation.get("seed"), bool
    ):
        raise EvaluationError("policy.generation.seed must be an integer")
    if selection.get("aggregate_accuracy_is_gate") is not False:
        raise EvaluationError("aggregate accuracy must not be a benchmark gate")

    fact_ids: list[str] = []
    for offset, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise EvaluationError(f"required fact {offset} must be an object")
        fact_id = fact.get("id")
        patterns = fact.get("content_regexes")
        if not isinstance(fact_id, str) or not fact_id:
            raise EvaluationError(f"required fact {offset} has no ID")
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(item, str) and item for item in patterns
        ):
            raise EvaluationError(f"required fact {fact_id!r} has invalid regexes")
        try:
            for pattern in patterns:
                re.compile(pattern)
        except re.error as exc:
            raise EvaluationError(
                f"required fact {fact_id!r} has an invalid regex: {exc}"
            ) from exc
        _positive_int(
            fact.get("minimum_cited_sources"),
            f"required fact {fact_id!r} minimum_cited_sources",
        )
        fact_ids.append(fact_id)
    if len(fact_ids) != len(set(fact_ids)):
        raise EvaluationError("required fact IDs must be unique")

    names: list[str] = []
    installed: list[str] = []
    model_ids: list[str] = []
    for offset, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise EvaluationError(f"candidate {offset} must be an object")
        name = candidate.get("name")
        model = candidate.get("installed_model")
        if not isinstance(name, str) or not name or not isinstance(model, str) or not model:
            raise EvaluationError(f"candidate {offset} must name an installed model")
        if ":" in model and not re.match(r"^[A-Za-z]:[\\/]", model):
            raise EvaluationError(f"candidate {name!r} has a linked/colon-prefixed model")
        names.append(name)
        installed.append(model)
        model_ids.append(_candidate_identifier(name))
    if (
        len(names) != len(set(names))
        or len(installed) != len(set(installed))
        or len(model_ids) != len(set(model_ids))
    ):
        raise EvaluationError(
            "candidate names, lifecycle identifiers, and installed models must be unique"
        )
    return policy, payload


def _require_exact_keys(value: Any, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationError(f"{label} must be an object")
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise EvaluationError(
            f"{label} fields differ from the frozen schema; missing={missing}, extra={extra}"
        )
    return value


def _nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise EvaluationError(f"{label} must be a non-empty, unpadded string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise EvaluationError(f"{label} must be valid UTF-8") from exc
    return value


def _validate_truth_locator(value: Any, *, label: str) -> dict[str, str]:
    locator = _require_exact_keys(
        value,
        {"source", "ref", "modality"},
        label=label,
    )
    return {
        key: _nonempty_string(locator[key], label=f"{label}.{key}")
        for key in ("source", "ref", "modality")
    }


def load_policy_v2(path: Path = POLICY_V2_PATH) -> tuple[dict[str, Any], bytes]:
    """Read and strictly validate the preregistered semantic gate policy."""

    policy, payload = _read_object(path, label="synthesis v2 policy")
    if (
        path.resolve() == POLICY_V2_PATH.resolve()
        and _sha256(payload) != FROZEN_POLICY_V2_SHA256
    ):
        raise EvaluationError(
            "the frozen Stage 5 v2 policy changed; create a new benchmark version"
        )
    _require_exact_keys(
        policy,
        {
            "schema",
            "benchmark",
            "base_benchmark",
            "base_policy_sha256",
            "frozen_before_model_outputs",
            "required_endpoint_class",
            "blindness",
            "review",
            "eligibility",
            "failure_injection",
            "render_budget_scope",
            "render_budgets",
            "render_semantic_requirements",
        },
        label="synthesis v2 policy",
    )
    if policy["schema"] != 2 or policy["benchmark"] != "stage5-grounded-synthesis-v2":
        raise EvaluationError("unexpected synthesis v2 policy identity")
    if policy["base_benchmark"] != "stage5-grounded-synthesis-v1":
        raise EvaluationError("synthesis v2 policy names the wrong base benchmark")
    if policy["base_policy_sha256"] != FROZEN_POLICY_SHA256:
        raise EvaluationError("synthesis v2 policy names the wrong base policy hash")
    if policy["frozen_before_model_outputs"] is not True:
        raise EvaluationError("synthesis v2 policy must be frozen before outputs")
    if policy["required_endpoint_class"] != "openai-compatible-zbook-local":
        raise EvaluationError("synthesis v2 requires the strict ZBook-local endpoint class")

    blindness = _require_exact_keys(
        policy["blindness"],
        {"algorithm", "alias_prefix", "alias_hex_characters"},
        label="policy_v2.blindness",
    )
    if blindness["algorithm"] != "sha256-preoutput-seed-candidate-v1":
        raise EvaluationError("unsupported v2 blindness algorithm")
    _nonempty_string(blindness["alias_prefix"], label="blindness.alias_prefix")
    alias_characters = _positive_int(
        blindness["alias_hex_characters"], "blindness.alias_hex_characters"
    )
    if alias_characters > 64:
        raise EvaluationError("blindness alias cannot exceed a SHA-256 digest")

    review = _require_exact_keys(
        policy["review"],
        {
            "initial_reviewers",
            "independent",
            "candidate_identity_hidden",
            "third_reviewer_on_gate_disagreement",
            "unresolved_is_ineligible",
            "model_as_judge_allowed",
        },
        label="policy_v2.review",
    )
    if review != {
        "initial_reviewers": 2,
        "independent": True,
        "candidate_identity_hidden": True,
        "third_reviewer_on_gate_disagreement": True,
        "unresolved_is_ineligible": True,
        "model_as_judge_allowed": False,
    }:
        raise EvaluationError("v2 requires two independent human reviewers and no model judge")

    eligibility = _require_exact_keys(
        policy["eligibility"],
        {
            "required_fact_ids_each_repeat",
            "minimum_relevant_sources_each_repeat",
            "minimum_relevant_tier3_sources_each_repeat",
            "accepted_claim_usefulness",
            "required_summary_gates",
            "aggregate_accuracy_is_gate",
        },
        label="policy_v2.eligibility",
    )
    facts = eligibility["required_fact_ids_each_repeat"]
    if not isinstance(facts, list) or not facts or not all(
        isinstance(item, str) and item for item in facts
    ) or len(facts) != len(set(facts)):
        raise EvaluationError("v2 required fact IDs must be a unique non-empty array")
    _positive_int(
        eligibility["minimum_relevant_sources_each_repeat"],
        "eligibility.minimum_relevant_sources_each_repeat",
    )
    _positive_int(
        eligibility["minimum_relevant_tier3_sources_each_repeat"],
        "eligibility.minimum_relevant_tier3_sources_each_repeat",
    )
    if eligibility["accepted_claim_usefulness"] != ["essential", "useful"]:
        raise EvaluationError("v2 accepted claim usefulness values changed")
    expected_summary_gates = [
        "coherent",
        "concise",
        "integrates_sources",
        "useful_over_inventory",
    ]
    if eligibility["required_summary_gates"] != expected_summary_gates:
        raise EvaluationError("v2 summary gates changed")
    if eligibility["aggregate_accuracy_is_gate"] is not False:
        raise EvaluationError("aggregate accuracy is forbidden in synthesis v2")

    failure = _require_exact_keys(
        policy["failure_injection"],
        {
            "repeats",
            "max_response_bytes",
            "clock_elapsed_ns",
            "accepted_claim_count",
            "fallback_used",
            "cases",
        },
        label="policy_v2.failure_injection",
    )
    if failure["repeats"] != 2:
        raise EvaluationError("failure injection requires exactly two repeats")
    _positive_int(failure["max_response_bytes"], "failure max_response_bytes")
    _positive_int(failure["clock_elapsed_ns"], "failure clock_elapsed_ns")
    if failure["accepted_claim_count"] != 0 or failure["fallback_used"] is not False:
        raise EvaluationError("failure injections must accept zero claims and use no fallback")
    cases = failure["cases"]
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("failure injection cases must be a non-empty array")
    case_ids: list[str] = []
    for index, case in enumerate(cases):
        item = _require_exact_keys(
            case,
            {
                "id",
                "expected_outcome",
                "expected_validation_status",
                "expected_error_class",
                "expected_error_phase",
                "expected_error_code",
                "response_present",
            },
            label=f"failure case {index}",
        )
        case_ids.append(_nonempty_string(item["id"], label=f"failure case {index}.id"))
        _nonempty_string(item["expected_outcome"], label=f"failure case {index}.outcome")
        if item["expected_validation_status"] not in {"rejected", "not-run"}:
            raise EvaluationError(
                f"failure case {index}.expected_validation_status is invalid"
            )
        _nonempty_string(
            item["expected_error_class"], label=f"failure case {index}.error_class"
        )
        _nonempty_string(
            item["expected_error_phase"], label=f"failure case {index}.error_phase"
        )
        _nonempty_string(
            item["expected_error_code"], label=f"failure case {index}.error_code"
        )
        if not isinstance(item["response_present"], bool):
            raise EvaluationError(f"failure case {index}.response_present must be boolean")
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationError("failure injection case IDs must be unique")

    render_scope = _require_exact_keys(
        policy["render_budget_scope"],
        {
            "classification",
            "includes_model_run_manifest",
            "product_acceptance_target",
            "review_required_before_product_lock",
        },
        label="policy_v2.render_budget_scope",
    )
    if render_scope != {
        "classification": "benchmark-complete-audit-wire-v1",
        "includes_model_run_manifest": True,
        "product_acceptance_target": False,
        "review_required_before_product_lock": True,
    }:
        raise EvaluationError(
            "v2 render ceilings are benchmark audit wires, not product targets"
        )

    budgets = policy["render_budgets"]
    if not isinstance(budgets, dict) or set(budgets) != {"ansi", "md", "json", "jsonl"}:
        raise EvaluationError("v2 must freeze all four core renderer budgets")
    for shape, levels in budgets.items():
        item = _require_exact_keys(
            levels, {"minimum", "compact", "complete"}, label=f"render budget {shape}"
        )
        values = [
            _positive_int(item[level], f"render budget {shape}.{level}")
            for level in ("minimum", "compact", "complete")
        ]
        if values != sorted(values) or len(set(values)) != 3:
            raise EvaluationError(f"render budget {shape} levels must strictly increase")
    semantic = policy["render_semantic_requirements"]
    if not isinstance(semantic, dict) or set(semantic) != {
        "minimum",
        "compact",
        "complete",
    }:
        raise EvaluationError("v2 render semantic requirements are incomplete")
    fact_set = set(facts)
    for level, required in semantic.items():
        if not isinstance(required, list) or len(required) != len(set(required)):
            raise EvaluationError(f"render requirement {level} must be a unique array")
        if not set(required).issubset(fact_set):
            raise EvaluationError(f"render requirement {level} names an unknown fact")
    if semantic["minimum"] or set(semantic["complete"]) != fact_set:
        raise EvaluationError("minimum is compliance-only and complete must require every fact")
    return policy, payload


def load_truth_v2(path: Path = TRUTH_V2_PATH) -> tuple[dict[str, Any], bytes]:
    """Read the human-authored proposition/evidence ledger without resolving IDs."""

    truth, payload = _read_object(path, label="synthesis v2 truth ledger")
    if path.resolve() == TRUTH_V2_PATH.resolve() and _sha256(payload) != FROZEN_TRUTH_V2_SHA256:
        raise EvaluationError("the frozen Stage 5 v2 truth ledger changed")
    _require_exact_keys(
        truth,
        {"schema", "benchmark", "hero_collection", "facts", "hard_negatives", "payload_canaries"},
        label="synthesis v2 truth ledger",
    )
    if truth["schema"] != 2 or truth["benchmark"] != "stage5-grounded-synthesis-v2":
        raise EvaluationError("unexpected synthesis v2 truth identity")
    _nonempty_string(truth["hero_collection"], label="truth.hero_collection")

    facts = truth["facts"]
    if not isinstance(facts, list) or not facts:
        raise EvaluationError("truth facts must be a non-empty array")
    fact_ids: list[str] = []
    for fact_index, fact in enumerate(facts):
        allowed_fields = {
            "id",
            "canonical_proposition",
            "critical",
            "semantic_slots",
            "acceptable_evidence_sets",
            "forbidden_overclaims",
        }
        if isinstance(fact, dict) and "required_findings" in fact:
            allowed_fields.add("required_findings")
        item = _require_exact_keys(fact, allowed_fields, label=f"truth fact {fact_index}")
        fact_id = _nonempty_string(item["id"], label=f"truth fact {fact_index}.id")
        fact_ids.append(fact_id)
        _nonempty_string(
            item["canonical_proposition"],
            label=f"truth fact {fact_id}.canonical_proposition",
        )
        if item["critical"] is not True:
            raise EvaluationError(f"truth fact {fact_id!r} must be a hard critical fact")
        slots = item["semantic_slots"]
        if not isinstance(slots, list) or not slots or not all(
            isinstance(value, str) and value for value in slots
        ) or len(slots) != len(set(slots)):
            raise EvaluationError(f"truth fact {fact_id!r} has invalid semantic slots")
        evidence_sets = item["acceptable_evidence_sets"]
        if not isinstance(evidence_sets, list) or not evidence_sets:
            raise EvaluationError(f"truth fact {fact_id!r} has no evidence sets")
        for set_index, evidence_set in enumerate(evidence_sets):
            if not isinstance(evidence_set, list) or not evidence_set:
                raise EvaluationError(
                    f"truth fact {fact_id!r} evidence set {set_index} is empty"
                )
            for locator_index, locator in enumerate(evidence_set):
                _validate_truth_locator(
                    locator,
                    label=f"truth fact {fact_id}.evidence[{set_index}][{locator_index}]",
                )
        overclaims = item["forbidden_overclaims"]
        if not isinstance(overclaims, list) or not all(
            isinstance(value, str) and value for value in overclaims
        ):
            raise EvaluationError(f"truth fact {fact_id!r} forbidden overclaims are invalid")
        for finding_index, finding in enumerate(item.get("required_findings", [])):
            record = _require_exact_keys(
                finding,
                {"source", "ref", "kind"},
                label=f"truth fact {fact_id}.finding[{finding_index}]",
            )
            for key in ("source", "ref", "kind"):
                _nonempty_string(record[key], label=f"truth finding {fact_id}.{key}")
    if len(fact_ids) != len(set(fact_ids)):
        raise EvaluationError("truth fact IDs must be unique")

    negatives = truth["hard_negatives"]
    if not isinstance(negatives, list) or not negatives:
        raise EvaluationError("truth hard negatives must be a non-empty array")
    negative_ids: list[str] = []
    for index, negative in enumerate(negatives):
        item = _require_exact_keys(
            negative,
            {"id", "proposition", "basis", "evidence"},
            label=f"hard negative {index}",
        )
        negative_ids.append(_nonempty_string(item["id"], label=f"hard negative {index}.id"))
        _nonempty_string(item["proposition"], label=f"hard negative {index}.proposition")
        if item["basis"] not in {"contradicted", "unsupported-by-evidence-pack"}:
            raise EvaluationError(f"hard negative {index} has an invalid basis")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise EvaluationError(f"hard negative {index} has no grounding evidence")
        for locator_index, locator in enumerate(evidence):
            _validate_truth_locator(
                locator, label=f"hard negative {index}.evidence[{locator_index}]"
            )
    if len(negative_ids) != len(set(negative_ids)):
        raise EvaluationError("hard negative IDs must be unique")

    canaries = truth["payload_canaries"]
    if not isinstance(canaries, list) or not canaries:
        raise EvaluationError("truth payload canaries must be a non-empty array")
    canary_ids: list[str] = []
    literals: list[str] = []
    canary_sources: list[str] = []
    native_locators: list[str] = []
    for index, canary in enumerate(canaries):
        item = _require_exact_keys(
            canary,
            {"id", "literal", "source", "native_locator", "status", "reason"},
            label=f"payload canary {index}",
        )
        canary_ids.append(_nonempty_string(item["id"], label=f"canary {index}.id"))
        literals.append(_nonempty_string(item["literal"], label=f"canary {index}.literal"))
        canary_sources.append(
            _nonempty_string(item["source"], label=f"canary {index}.source")
        )
        native_locators.append(
            _nonempty_string(
                item["native_locator"], label=f"canary {index}.native_locator"
            )
        )
        if item["status"] not in {"final", "temporary-test-only"}:
            raise EvaluationError(f"payload canary {index} has an invalid status")
        _nonempty_string(item["reason"], label=f"canary {index}.reason")
    if len(canary_ids) != len(set(canary_ids)) or len(literals) != len(set(literals)):
        raise EvaluationError("payload canary IDs and literals must be unique")
    if (
        len(canary_sources) != len(set(canary_sources))
        or set(canary_sources) != LOCKED_TIER3_CANARY_SOURCES
    ):
        raise EvaluationError(
            "truth must contain exactly one payload canary per locked Tier-3 source"
        )
    if len(native_locators) != len(set(native_locators)):
        raise EvaluationError("payload canary native locators must be unique")
    return truth, payload


def load_review_schema_v2(
    path: Path = REVIEW_SCHEMA_V2_PATH,
) -> tuple[dict[str, Any], bytes]:
    """Read the closed human-review artifact vocabulary."""

    schema, payload = _read_object(path, label="synthesis v2 review schema")
    if (
        path.resolve() == REVIEW_SCHEMA_V2_PATH.resolve()
        and _sha256(payload) != FROZEN_REVIEW_SCHEMA_V2_SHA256
    ):
        raise EvaluationError("the frozen Stage 5 v2 review schema changed")
    _require_exact_keys(
        schema,
        {
            "schema",
            "benchmark",
            "artifact_schema",
            "packet_schema",
            "entailment_values",
            "usefulness_values",
            "fact_row_fields",
            "claim_row_fields",
            "summary_row_fields",
            "rationale_min_characters",
            "rationale_max_characters",
            "reviewer_must_be_human",
            "model_generated_review_allowed",
        },
        label="synthesis v2 review schema",
    )
    if (
        schema["schema"] != 2
        or schema["benchmark"] != "stage5-grounded-synthesis-v2"
        or schema["artifact_schema"] != REVIEW_ARTIFACT_V2_SCHEMA
        or schema["packet_schema"] != REVIEW_PACKET_V2_SCHEMA
    ):
        raise EvaluationError("unexpected synthesis v2 review schema identity")
    if schema["entailment_values"] != [
        "entailed",
        "partial",
        "unsupported",
        "contradicted",
    ]:
        raise EvaluationError("review entailment vocabulary changed")
    if schema["usefulness_values"] != ["essential", "useful", "redundant", "noise"]:
        raise EvaluationError("review usefulness vocabulary changed")
    expected_fields = {
        "fact_row_fields": [
            "packet_id",
            "fact_id",
            "supporting_claim_ids",
            "entailment",
            "rationale",
        ],
        "claim_row_fields": [
            "packet_id",
            "claim_id",
            "every_citation_relevant",
            "atomic_claim",
            "hard_negative_ids",
            "payload_canary_ids",
            "other_raw_payload_leak",
            "usefulness",
            "rationale",
        ],
        "summary_row_fields": [
            "packet_id",
            "coherent",
            "concise",
            "integrates_sources",
            "useful_over_inventory",
            "rationale",
        ],
    }
    for key, expected in expected_fields.items():
        if schema[key] != expected:
            raise EvaluationError(f"review schema {key} changed")
    minimum = _positive_int(
        schema["rationale_min_characters"], "review rationale minimum"
    )
    maximum = _positive_int(
        schema["rationale_max_characters"], "review rationale maximum"
    )
    if minimum > maximum:
        raise EvaluationError("review rationale bounds are inverted")
    if schema["reviewer_must_be_human"] is not True or schema["model_generated_review_allowed"] is not False:
        raise EvaluationError("synthesis v2 reviews must be human-authored")
    return schema, payload


def policy_candidates(policy: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Return the exact candidate order with lifecycle model identifiers."""

    return tuple(
        {
            "name": item["name"],
            "slug": _candidate_slug(item["name"]),
            "installed_model": item["installed_model"],
            "model_id": _candidate_identifier(item["name"]),
        }
        for item in policy["candidate_order"]
    )


def verify_hero(
    hero_dir: Path = HERO_DIR,
    manifest_path: Path = HERO_MANIFEST_PATH,
) -> dict[str, Any]:
    """Verify every manifest entry and reject extra files or symlinks."""

    manifest, payload = _read_object(manifest_path, label="hero manifest")
    if (
        manifest_path.resolve() == HERO_MANIFEST_PATH.resolve()
        and _sha256(payload) != FROZEN_HERO_MANIFEST_SHA256
    ):
        raise EvaluationError("the frozen Stage 5 hero manifest changed")
    if manifest.get("schema") != 1 or manifest.get("collection") != "borealis-stage5-hero-v1":
        raise EvaluationError("unexpected Stage 5 hero manifest identity")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise EvaluationError("hero manifest files must be a non-empty array")

    expected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise EvaluationError("each hero manifest file needs path/bytes/sha256")
        name = entry.get("path")
        if not isinstance(name, str) or not name:
            raise EvaluationError("hero manifest file path must be non-empty")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or str(pure) != name:
            raise EvaluationError(f"unsafe hero manifest path {name!r}")
        if name in expected:
            raise EvaluationError(f"duplicate hero manifest path {name!r}")
        _positive_int(entry.get("bytes"), f"hero file {name!r} byte count")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise EvaluationError(f"hero file {name!r} has an invalid SHA-256")
        expected[name] = dict(entry)
    if list(expected) != sorted(expected):
        raise EvaluationError("hero manifest paths must be in deterministic order")

    if not hero_dir.is_dir() or hero_dir.is_symlink():
        raise EvaluationError("hero collection root must be a real directory")
    actual: dict[str, Path] = {}
    for path in sorted(hero_dir.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise EvaluationError(f"hero collection contains symlink {path}")
        if path.is_file():
            relative = path.relative_to(hero_dir).as_posix()
            actual[relative] = path
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise EvaluationError(
            f"hero files differ from manifest; missing={missing}, extra={extra}"
        )
    for name, entry in expected.items():
        try:
            file_payload = actual[name].read_bytes()
        except OSError as exc:
            raise EvaluationError(f"cannot read hero member {name!r}: {exc}") from exc
        if len(file_payload) != entry["bytes"] or _sha256(file_payload) != entry["sha256"]:
            raise EvaluationError(f"hero member {name!r} differs from its manifest")
    return {
        "manifest_sha256": _sha256(payload),
        "manifest_bytes": len(payload),
        "collection": manifest["collection"],
        "file_count": len(expected),
        "files": [dict(expected[name]) for name in sorted(expected)],
    }


def _origin_record(origin: Any) -> dict[str, Any]:
    return {
        "source": origin.source,
        "ref": origin.ref,
        "char_span": list(origin.char_span) if origin.char_span is not None else None,
    }


def _claim_record(claim: Any) -> dict[str, Any]:
    return {
        "id": claim.id,
        "content": claim.content,
        "evidence_unit_ids": list(claim.evidence_unit_ids),
        "origins": [_origin_record(origin) for origin in claim.origins],
    }


def extraction_record(extraction: Any) -> dict[str, Any]:
    """Canonical semantic extraction record, excluding volatile timings."""

    units = sorted(extraction.units, key=lambda unit: unit.id)
    relations = sorted(
        extraction.relations,
        key=lambda relation: (
            relation.src,
            relation.dst,
            str(relation.kind),
            relation.evidence,
            relation.confidence,
        ),
    )
    gaps = sorted(
        extraction.gaps,
        key=lambda gap: (
            str(gap.kind),
            gap.origin.source,
            gap.origin.ref,
            gap.origin.char_span or (-1, -1),
            gap.content,
        ),
    )
    claims = sorted(extraction.summary_claims, key=lambda claim: claim.id)
    return {
        "source": extraction.source,
        "kind": extraction.kind,
        "units": [
            {
                "id": unit.id,
                "source": unit.source,
                "modality": str(unit.modality),
                "role": str(unit.role),
                "content": unit.content,
                "origin": _origin_record(unit.origin),
                "structure": list(unit.structure),
                "salience": unit.salience,
                "confidence": unit.confidence,
                "tokens": unit.tokens,
                "meta": unit.meta,
            }
            for unit in units
        ],
        "relations": [
            {
                "src": relation.src,
                "dst": relation.dst,
                "kind": str(relation.kind),
                "evidence": relation.evidence,
                "confidence": relation.confidence,
            }
            for relation in relations
        ],
        "gaps": [
            {
                "kind": str(gap.kind),
                "content": gap.content,
                "origin": _origin_record(gap.origin),
            }
            for gap in gaps
        ],
        "summary_claims": [_claim_record(claim) for claim in claims],
    }


def acquisition_binding(extraction: Any) -> dict[str, Any]:
    """Validate and bind the complete Tier 2 acquisition manifest.

    The semantic extraction record intentionally excludes general ``meta``.
    Without this explicit binding, changes in member admission, declines,
    limits, or adapter capability manifests would not invalidate the freeze
    as long as the surviving semantic units happened to stay identical.
    """

    meta = getattr(extraction, "meta", None)
    manifest = meta.get("acquisition") if isinstance(meta, Mapping) else None
    if not isinstance(manifest, Mapping):
        raise EvaluationError(
            "benchmark collection extraction has no Tier 2 acquisition manifest"
        )
    complete = dict(manifest)
    claimed_digest = complete.pop("sha256", None)
    if not isinstance(claimed_digest, str) or len(claimed_digest) != 64:
        raise EvaluationError("acquisition manifest has no valid self-hash")
    if claimed_digest != _sha256(_canonical_bytes(complete)):
        raise EvaluationError("acquisition manifest self-hash is invalid")
    canonical = _canonical_bytes(dict(manifest))
    members = manifest.get("members")
    counts = manifest.get("counts")
    if not isinstance(members, list) or not isinstance(counts, Mapping):
        raise EvaluationError("acquisition manifest members/counts are invalid")
    return {
        "schema": manifest.get("schema"),
        "source": manifest.get("source"),
        "kind": manifest.get("kind"),
        "manifest_sha256": claimed_digest,
        "complete_manifest_sha256": _sha256(canonical),
        "manifest_bytes": len(canonical),
        "member_count": len(members),
        "counts": dict(counts),
        "admitted_bytes": manifest.get("admitted_bytes"),
    }


def build_hero_extraction(hero_dir: Path = HERO_DIR) -> Any:
    """Run production collection acquisition and measured fusion for the hero."""

    from autotldr.collection import acquire_collection
    from autotldr.fusion import fuse
    from autotldr.unit import Extraction

    acquisition = acquire_collection(hero_dir, logical_source=hero_dir.name)
    members = tuple(acquisition.extractions)
    if len(members) < 2:
        raise EvaluationError(
            f"hero acquisition yielded {len(members)} valid members; fusion needs at least two"
        )
    extraction = fuse(members, subject=acquisition.source)
    meta = dict(extraction.meta)
    meta["acquisition"] = acquisition.manifest
    result = Extraction(
        source=extraction.source,
        kind=extraction.kind,
        units=list(extraction.units),
        relations=list(extraction.relations),
        gaps=[*extraction.gaps, *acquisition.gaps],
        meta=meta,
        summary_claims=list(extraction.summary_claims),
    )
    source_names = {unit.source for unit in result.units}
    missing = [
        name
        for name in REQUIRED_HERO_SOURCES
        if not any(source == name or source.endswith("/" + name) for source in source_names)
    ]
    if missing:
        raise EvaluationError(
            "hero production extraction is missing required sources: " + ", ".join(missing)
        )
    return result


def synthesis_config(policy: Mapping[str, Any], model_id: str) -> Any:
    """Build the exact production configuration used for every repeat."""

    from autotldr.synthesis import EndpointPolicy, SynthesisConfig

    generation = policy["generation"]
    evidence = policy["evidence_pack"]
    return SynthesisConfig(
        model=model_id,
        endpoint=policy["endpoint"],
        endpoint_policy=EndpointPolicy(localhost_only=True, allowed_schemes=("http",)),
        evidence_budget_bytes=evidence["max_bytes"],
        timeout_seconds=RUN_TIMEOUT_SECONDS,
        max_output_tokens=generation["max_tokens"],
        max_response_bytes=MAX_RESPONSE_BYTES,
        temperature=generation["temperature"],
        seed=generation["seed"],
        fallback_on_failure=False,
    )


def _evidence_fact_coverage(pack: Any, policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Prove the frozen pack contains lexical support for every scored fact.

    This is a pre-output recoverability check, not a model score.  A fact whose
    preregistered patterns are absent from the evidence supplied to the model
    cannot be honestly recovered and must stop the freeze rather than become a
    benchmark trap.
    """

    evidence: list[tuple[str, str]] = [
        (unit.source, unit.content)
        for unit in pack.units
        if unit.modality not in {"source", "reference"}
    ]
    for finding in pack.findings:
        evidence.append((finding.origin.source, finding.content))

    coverage: list[dict[str, Any]] = []
    for fact in policy["required_facts"]:
        patterns: list[dict[str, Any]] = []
        for pattern in fact["content_regexes"]:
            regex = re.compile(pattern)
            matched_sources = sorted(
                {source for source, content in evidence if regex.search(content)}
            )
            patterns.append(
                {
                    "regex": pattern,
                    "matched": bool(matched_sources),
                    "matched_sources": matched_sources,
                }
            )
        coverage.append(
            {
                "id": fact["id"],
                "patterns": patterns,
                "recoverable_from_pack": all(item["matched"] for item in patterns),
            }
        )
    return coverage


def _validate_evidence_policy(
    extraction: Any, pack: Any, policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    evidence = policy["evidence_pack"]
    if pack.used_bytes > evidence["max_bytes"]:
        raise EvaluationError("production evidence pack exceeded its frozen byte ceiling")
    if len(pack.units) > evidence["max_units"]:
        raise EvaluationError(
            f"production evidence pack selected {len(pack.units)} units; "
            f"frozen maximum is {evidence['max_units']}"
        )
    available_sources = {unit.source for unit in extraction.units}
    selected_sources = {unit.source for unit in pack.units}
    expected_minimum = min(
        evidence["minimum_sources_when_available"], len(available_sources)
    )
    if len(selected_sources) < expected_minimum:
        raise EvaluationError(
            f"evidence pack covers {len(selected_sources)} sources; "
            f"frozen minimum is {expected_minimum}"
        )
    coverage = _evidence_fact_coverage(pack, policy)
    missing_facts = [
        item["id"] for item in coverage if not item["recoverable_from_pack"]
    ]
    if missing_facts:
        raise EvaluationError(
            "evidence pack lacks preregistered lexical support for required facts: "
            + ", ".join(missing_facts)
        )
    return coverage


def _freeze_digest(record_without_digest: Mapping[str, Any]) -> str:
    return _sha256(_canonical_bytes(record_without_digest))


def build_freeze_record(
    *,
    extraction: Any | None = None,
    policy_path: Path = POLICY_PATH,
    hero_dir: Path = HERO_DIR,
    hero_manifest_path: Path = HERO_MANIFEST_PATH,
) -> dict[str, Any]:
    """Build the immutable pre-output binding without writing it."""

    policy, policy_bytes = load_policy(policy_path)
    hero = verify_hero(hero_dir, hero_manifest_path)
    active_extraction = extraction or build_hero_extraction(hero_dir)
    if active_extraction.kind != "collection":
        raise EvaluationError("benchmark synthesis input must be a collection Extraction")

    from autotldr.synthesis import build_chat_request, build_evidence_pack

    pack = build_evidence_pack(
        active_extraction,
        budget_bytes=policy["evidence_pack"]["max_bytes"],
    )
    fact_coverage = _validate_evidence_policy(active_extraction, pack, policy)
    acquisition = acquisition_binding(active_extraction)
    extraction_bytes = _canonical_bytes(extraction_record(active_extraction))
    pack_bytes = pack.to_bytes()
    requests: list[dict[str, Any]] = []
    for candidate in policy_candidates(policy):
        config = synthesis_config(policy, candidate["model_id"])
        request = build_chat_request(pack, config)
        requests.append(
            {
                "candidate": candidate["name"],
                "model_id": candidate["model_id"],
                "sha256": _sha256(request),
                "bytes": len(request),
            }
        )

    core: dict[str, Any] = {
        "schema": FREEZE_SCHEMA,
        "benchmark": policy["benchmark"],
        "frozen_before_model_outputs": True,
        "policy": {
            "sha256": _sha256(policy_bytes),
            "bytes": len(policy_bytes),
        },
        "hero": hero,
        "acquisition": acquisition,
        "extraction": {
            "sha256": _sha256(extraction_bytes),
            "bytes": len(extraction_bytes),
            "source": active_extraction.source,
            "kind": active_extraction.kind,
            "unit_count": len(active_extraction.units),
            "relation_count": len(active_extraction.relations),
            "gap_count": len(active_extraction.gaps),
            "prior_claim_count": len(active_extraction.summary_claims),
        },
        "evidence_pack": {
            "sha256": _sha256(pack_bytes),
            "bytes": len(pack_bytes),
            "selection": pack.selection_record(),
            "distinct_sources": sorted({unit.source for unit in pack.units}),
            "required_fact_coverage": fact_coverage,
        },
        "generation": {
            "repeats": policy["generation"]["repeats"],
            "context_length": policy["generation"]["context_length"],
            "parallel": policy["generation"]["parallel"],
            "timeout_seconds": RUN_TIMEOUT_SECONDS,
            "max_response_bytes": MAX_RESPONSE_BYTES,
        },
        "requests": requests,
    }
    core["freeze_sha256"] = _freeze_digest(core)
    return core


def verify_freeze_record(
    freeze: Mapping[str, Any],
    *,
    extraction: Any | None = None,
    policy_path: Path = POLICY_PATH,
    hero_dir: Path = HERO_DIR,
    hero_manifest_path: Path = HERO_MANIFEST_PATH,
) -> tuple[dict[str, Any], Any]:
    """Rebuild every binding and return the verified freeze and extraction."""

    if not isinstance(freeze, Mapping) or freeze.get("schema") != FREEZE_SCHEMA:
        raise EvaluationError("invalid Stage 5 synthesis freeze schema")
    observed = dict(freeze)
    digest = observed.pop("freeze_sha256", None)
    if not isinstance(digest, str) or digest != _freeze_digest(observed):
        raise EvaluationError("synthesis freeze self-hash is invalid")
    active_extraction = extraction or build_hero_extraction(hero_dir)
    expected = build_freeze_record(
        extraction=active_extraction,
        policy_path=policy_path,
        hero_dir=hero_dir,
        hero_manifest_path=hero_manifest_path,
    )
    if _canonical_bytes(dict(freeze)) != _canonical_bytes(expected):
        raise EvaluationError(
            "current hero/policy/extraction/evidence/request bindings differ from freeze"
        )
    return expected, active_extraction


def read_freeze(path: Path = DEFAULT_FREEZE_PATH) -> dict[str, Any]:
    value, _ = _read_object(path, label="synthesis freeze")
    return value


def _atomic_write_new(path: Path, payload: bytes) -> None:
    """Publish a write-once artifact without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: tempfile.NamedTemporaryFile[bytes] | None = None
    temporary_name: str | None = None
    try:
        temporary = tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        )
        temporary_name = temporary.name
        with temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError as exc:
            raise EvaluationError(f"refusing to overwrite audit artifact {path}") from exc
        except OSError as exc:
            raise EvaluationError(f"cannot publish audit artifact {path}: {exc}") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def write_freeze(path: Path, record: Mapping[str, Any]) -> None:
    _atomic_write_new(path, json.dumps(record, indent=2, ensure_ascii=False).encode() + b"\n")


def require_no_model_outputs(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    """Refuse a nominal pre-output freeze after the audit directory has data."""

    if not output_dir.exists():
        return
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise EvaluationError(f"candidate output path is not a real directory: {output_dir}")
    existing = sorted(
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    )
    if existing:
        raise EvaluationError(
            "refusing to create a pre-output freeze after candidate output exists: "
            + ", ".join(existing)
        )


class RecordingClient:
    """Preserve exact transport bytes around a production completion client."""

    def __init__(self, wrapped: _CompletionClient) -> None:
        self.wrapped = wrapped
        declared_endpoint = getattr(wrapped, "endpoint_url", None)
        if declared_endpoint is not None:
            self.endpoint_url = declared_endpoint
        declared_attestation = getattr(wrapped, "attestation", None)
        if declared_attestation is not None:
            self.attestation = declared_attestation
        self.request_body: bytes | None = None
        self.response_body: bytes | None = None
        self.error: BaseException | None = None

    def complete(
        self,
        request_body: bytes,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        if self.request_body is not None:
            raise EvaluationError("one RecordingClient cannot issue multiple requests")
        self.request_body = bytes(request_body)
        try:
            response = self.wrapped.complete(
                request_body,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
        except BaseException as exc:
            self.error = exc
            raise
        if not isinstance(response, bytes):
            return response  # production synthesis records this as a client error
        self.response_body = bytes(response)
        return response


def _bytes_record(payload: bytes | None) -> dict[str, Any]:
    if payload is None:
        return {
            "present": False,
            "bytes": 0,
            "sha256": None,
            "base64": None,
            "utf8": None,
        }
    try:
        utf8 = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        utf8 = None
    return {
        "present": True,
        "bytes": len(payload),
        "sha256": _sha256(payload),
        "base64": base64.b64encode(payload).decode("ascii"),
        "utf8": utf8,
    }


def _candidate_for_model(policy: Mapping[str, Any], model_id: str) -> dict[str, str]:
    matches = [item for item in policy_candidates(policy) if item["model_id"] == model_id]
    if len(matches) != 1:
        allowed = [item["model_id"] for item in policy_candidates(policy)]
        raise EvaluationError(
            f"model ID {model_id!r} is not one of the frozen lifecycle IDs: {allowed}"
        )
    return matches[0]


def _request_binding(freeze: Mapping[str, Any], model_id: str) -> Mapping[str, Any]:
    requests = freeze.get("requests")
    if not isinstance(requests, list):
        raise EvaluationError("freeze requests binding is invalid")
    matches = [item for item in requests if isinstance(item, dict) and item.get("model_id") == model_id]
    if len(matches) != 1:
        raise EvaluationError(f"freeze has no unique request binding for {model_id!r}")
    return matches[0]


def run_candidate(
    model_id: str,
    output_path: Path,
    *,
    freeze_path: Path = DEFAULT_FREEZE_PATH,
    client_factory: ClientFactory | None = None,
    extraction: Any | None = None,
    policy_path: Path = POLICY_PATH,
    hero_dir: Path = HERO_DIR,
    hero_manifest_path: Path = HERO_MANIFEST_PATH,
) -> dict[str, Any]:
    """Run exactly two no-fallback repeats and publish one audited artifact."""

    if output_path.exists():
        raise EvaluationError(f"refusing to overwrite candidate artifact {output_path}")
    freeze = read_freeze(freeze_path)
    verified_freeze, active_extraction = verify_freeze_record(
        freeze,
        extraction=extraction,
        policy_path=policy_path,
        hero_dir=hero_dir,
        hero_manifest_path=hero_manifest_path,
    )
    policy, policy_bytes = load_policy(policy_path)
    candidate = _candidate_for_model(policy, model_id)
    request_binding = _request_binding(verified_freeze, model_id)
    config = synthesis_config(policy, model_id)

    from autotldr.synthesis import (
        OpenAICompatibleClient,
        SynthesisRunError,
        synthesize,
    )

    repeats: list[dict[str, Any]] = []
    repeat_count = policy["generation"]["repeats"]
    for repeat_index in range(repeat_count):
        wrapped = (
            client_factory(repeat_index, config)
            if client_factory is not None
            else OpenAICompatibleClient(config.endpoint, policy=config.endpoint_policy)
        )
        recorder = RecordingClient(wrapped)
        result = None
        model_run: Mapping[str, Any]
        error_record: dict[str, Any] | None = None
        try:
            result = synthesize(
                active_extraction,
                config,
                client=recorder,
            )
            model_run = result.model_run
        except SynthesisRunError as exc:
            model_run = exc.model_run
            error_record = {
                "class": exc.__class__.__name__,
                "message": str(exc),
            }

        if recorder.request_body is None:
            raise EvaluationError(f"repeat {repeat_index + 1} issued no model request")
        if (
            _sha256(recorder.request_body) != request_binding.get("sha256")
            or len(recorder.request_body) != request_binding.get("bytes")
        ):
            raise EvaluationError(
                f"repeat {repeat_index + 1} request differs from the pre-output freeze"
            )
        if model_run.get("input", {}).get("sha256") != request_binding.get("sha256"):
            raise EvaluationError(
                f"repeat {repeat_index + 1} model manifest has the wrong request hash"
            )
        claims = (
            [_claim_record(item) for item in result.extraction.summary_claims]
            if result is not None
            else []
        )
        repeats.append(
            {
                "repeat": repeat_index + 1,
                "request": _bytes_record(recorder.request_body),
                "raw_response": _bytes_record(recorder.response_body),
                "transport_error": (
                    {
                        "class": recorder.error.__class__.__name__,
                        "message": str(recorder.error),
                    }
                    if recorder.error is not None
                    else None
                ),
                "run_error": error_record,
                "claims": claims,
                "model_manifest": dict(model_run),
            }
        )

    artifact: dict[str, Any] = {
        "schema": CANDIDATE_ARTIFACT_SCHEMA,
        "benchmark": policy["benchmark"],
        "freeze_sha256": verified_freeze["freeze_sha256"],
        "policy_sha256": _sha256(policy_bytes),
        "hero_manifest_sha256": verified_freeze["hero"]["manifest_sha256"],
        "extraction_sha256": verified_freeze["extraction"]["sha256"],
        "evidence_pack_sha256": verified_freeze["evidence_pack"]["sha256"],
        "request_sha256": request_binding["sha256"],
        "candidate": candidate,
        "repeats": repeats,
    }
    artifact["artifact_sha256"] = _sha256(_canonical_bytes(artifact))
    _atomic_write_new(
        output_path,
        json.dumps(artifact, indent=2, ensure_ascii=False).encode("utf-8") + b"\n",
    )
    return artifact


def _decode_bytes_record(record: Any, *, label: str) -> bytes | None:
    if not isinstance(record, dict):
        raise EvaluationError(f"{label} byte record must be an object")
    present = record.get("present")
    encoded = record.get("base64")
    if present is False:
        if record.get("bytes") != 0 or record.get("sha256") is not None or encoded is not None:
            raise EvaluationError(f"{label} absent byte record is inconsistent")
        return None
    if present is not True or not isinstance(encoded, str):
        raise EvaluationError(f"{label} present byte record is invalid")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise EvaluationError(f"{label} contains invalid base64") from exc
    if len(payload) != record.get("bytes") or _sha256(payload) != record.get("sha256"):
        raise EvaluationError(f"{label} byte count or hash is invalid")
    expected_utf8 = record.get("utf8")
    try:
        observed_utf8 = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        observed_utf8 = None
    if expected_utf8 != observed_utf8:
        raise EvaluationError(f"{label} UTF-8 audit copy is inconsistent")
    return payload


def _source_is_tier3(source: str) -> bool:
    lowered = source.casefold()
    return any(lowered.endswith(suffix) for suffix in TIER3_SUFFIXES)


def _score_fact(
    fact: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    source_by_id: Mapping[str, str],
    support_text_by_id: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    pattern_records: list[dict[str, Any]] = []
    supporting_claim_indexes: set[int] = set()
    for pattern in fact["content_regexes"]:
        regex = re.compile(pattern)
        matching = [
            index
            for index, claim in enumerate(claims)
            if regex.search(claim["content"]) is not None
        ]
        cited_ids = {
            unit_id
            for index in matching
            for unit_id in claims[index]["evidence_unit_ids"]
        }
        evidence_matches = sorted(
            unit_id
            for unit_id in cited_ids
            if any(regex.search(text) for text in support_text_by_id.get(unit_id, ()))
        )
        supporting_claim_indexes.update(matching)
        pattern_records.append(
            {
                "regex": pattern,
                "matched": bool(matching),
                "claim_indexes": [index + 1 for index in matching],
                "supported_by_cited_evidence": bool(evidence_matches),
                "supporting_evidence_unit_ids": evidence_matches,
            }
        )
    cited_sources = sorted(
        {
            source_by_id[unit_id]
            for index in supporting_claim_indexes
            for unit_id in claims[index]["evidence_unit_ids"]
        }
    )
    content_recovered = all(item["matched"] for item in pattern_records)
    evidence_supported = all(
        item["supported_by_cited_evidence"] for item in pattern_records
    )
    citation_gate = len(cited_sources) >= fact["minimum_cited_sources"]
    return {
        "id": fact["id"],
        "description": fact["description"],
        "regexes": pattern_records,
        "content_recovered": content_recovered,
        "evidence_supported": evidence_supported,
        "cited_sources": cited_sources,
        "minimum_cited_sources": fact["minimum_cited_sources"],
        "citation_gate": citation_gate,
        "recovered": content_recovered and evidence_supported and citation_gate,
    }


def _require_model_manifest(
    manifest: Any,
    *,
    model_id: str,
    freeze: Mapping[str, Any],
    policy: Mapping[str, Any],
    request_body: bytes,
    response_body: bytes | None,
    claims: Sequence[Mapping[str, Any]],
    expected_role_backend: str,
) -> None:
    if not isinstance(claims, list) or not all(
        isinstance(claim, Mapping) for claim in claims
    ):
        raise EvaluationError("candidate claims must be an array of objects")
    item = _require_exact_keys(
        manifest,
        {
            "schema",
            "task",
            "endpoint_class",
            "endpoint",
            "transport",
            "endpoint_policy",
            "model",
            "settings",
            "input",
            "output",
            "response_facts",
            "timing",
            "outcome",
            "validation",
            "fallback",
            "claim_count",
            "claim_ids",
            "record_sha256",
        },
        label="model manifest",
    )
    core = dict(item)
    record_sha256 = core.pop("record_sha256")
    if not isinstance(record_sha256, str) or record_sha256 != _sha256(
        _canonical_bytes(core)
    ):
        raise EvaluationError("model manifest record self-hash is invalid")

    from autotldr.synthesis import MODEL_RUN_SCHEMA, SYNTHESIS_TASK

    if item["schema"] != MODEL_RUN_SCHEMA:
        raise EvaluationError("model manifest schema is invalid")
    if item["task"] != SYNTHESIS_TASK or item["model"] != model_id:
        raise EvaluationError("model manifest task/model identity is invalid")
    if item["endpoint_class"] != "openai-compatible-zbook-local":
        raise EvaluationError("model manifest endpoint class is not strict ZBook-local")
    if item["endpoint"] != "http://127.0.0.1:1234/v1/chat/completions":
        raise EvaluationError("model manifest endpoint is not the exact ZBook URL")

    transport = _require_exact_keys(
        item["transport"],
        {
            "endpoint_url",
            "endpoint_class",
            "implementation",
            "proxy_policy",
            "redirect_policy",
            "peer_requirement",
            "deadline_policy",
        },
        label="model transport attestation",
    )
    direct_transport = {
        "endpoint_url": item["endpoint"],
        "endpoint_class": item["endpoint_class"],
        "implementation": "direct-loopback-http1-v1",
        "proxy_policy": "no-proxy-code-path",
        "redirect_policy": "reject-non-200-no-follow",
        "peer_requirement": "127.0.0.1",
        "deadline_policy": "absolute-monotonic-per-operation-v1",
    }
    offline_transport = {
        "endpoint_url": item["endpoint"],
        "endpoint_class": item["endpoint_class"],
        "implementation": "offline-injected-test-v1",
        "proxy_policy": "caller-attested-disabled",
        "redirect_policy": "caller-attested-disabled",
        "peer_requirement": "127.0.0.1",
        "deadline_policy": "caller-attested-absolute-monotonic",
    }
    if transport != direct_transport and transport != offline_transport:
        raise EvaluationError("model transport attestation is not a frozen implementation")

    endpoint_policy = _require_exact_keys(
        item["endpoint_policy"],
        {"localhost_only", "allowed_schemes", "strict_zbook_local"},
        label="model endpoint policy",
    )
    if endpoint_policy != {
        "localhost_only": True,
        "allowed_schemes": ["http"],
        "strict_zbook_local": True,
    }:
        raise EvaluationError("model endpoint policy is not strict ZBook-local")

    settings = _require_exact_keys(
        item["settings"],
        {
            "allowed_response_model_aliases",
            "evidence_budget_bytes",
            "timeout_seconds",
            "max_output_tokens",
            "max_response_bytes",
            "temperature",
            "seed",
            "response_format",
            "fallback_on_failure",
        },
        label="model settings",
    )
    aliases = settings["allowed_response_model_aliases"]
    if not isinstance(aliases, list) or not all(
        isinstance(value, str) and value for value in aliases
    ) or len(aliases) != len(set(aliases)):
        raise EvaluationError("model response aliases are invalid")
    config = synthesis_config(policy, model_id)
    expected_settings = {
        "allowed_response_model_aliases": list(
            config.allowed_response_model_aliases
        ),
        "evidence_budget_bytes": config.evidence_budget_bytes,
        "timeout_seconds": config.timeout_seconds,
        "max_output_tokens": config.max_output_tokens,
        "max_response_bytes": config.max_response_bytes,
        "temperature": config.temperature,
        "seed": config.seed,
        "response_format": "strict-json-schema-v1",
        "fallback_on_failure": False,
    }
    if settings != expected_settings:
        raise EvaluationError("model settings differ from the frozen candidate config")

    input_record = _require_exact_keys(
        item["input"],
        {
            "sha256",
            "bytes",
            "role_backend",
            "evidence_pack_sha256",
            "evidence_pack_bytes",
            "evidence_selection",
        },
        label="model input record",
    )
    request_binding = _request_binding(freeze, model_id)
    if (
        input_record["sha256"] != request_binding["sha256"]
        or input_record["bytes"] != request_binding["bytes"]
        or _sha256(request_body) != request_binding["sha256"]
        or len(request_body) != request_binding["bytes"]
    ):
        raise EvaluationError("model manifest request hash differs from freeze")
    if (
        input_record["role_backend"] != expected_role_backend
        or input_record["evidence_pack_sha256"]
        != freeze["evidence_pack"]["sha256"]
        or input_record["evidence_pack_bytes"] != freeze["evidence_pack"]["bytes"]
    ):
        raise EvaluationError("model manifest evidence hash differs from freeze")
    if input_record["evidence_selection"] != freeze["evidence_pack"]["selection"]:
        raise EvaluationError("model manifest evidence selection differs from freeze")

    output = _require_exact_keys(
        item["output"],
        {"sha256", "bytes", "message_sha256", "message_bytes"},
        label="model output record",
    )
    expected_response_hash = _sha256(response_body) if response_body is not None else None
    expected_response_bytes = len(response_body) if response_body is not None else 0
    if (
        output["sha256"] != expected_response_hash
        or output["bytes"] != expected_response_bytes
    ):
        raise EvaluationError("model manifest response audit differs from raw response")

    facts = item["response_facts"]
    if facts is not None:
        response_facts = _require_exact_keys(
            facts,
            {
                "response_id",
                "created",
                "served_model",
                "finish_reason",
                "system_fingerprint",
                "usage",
            },
            label="model response facts",
        )
        usage = _require_exact_keys(
            response_facts["usage"],
            {"prompt_tokens", "completion_tokens", "total_tokens"},
            label="model response usage",
        )
        if response_body is None:
            raise EvaluationError("model response facts exist without a raw response")
        envelope = _strict_json_bytes(response_body, label="model raw response")
        expected_envelope_fields = {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
        }
        if not isinstance(envelope, dict) or frozenset(envelope) not in {
            frozenset(expected_envelope_fields),
            frozenset(expected_envelope_fields | {"system_fingerprint"}),
        }:
            raise EvaluationError("model raw response has a non-strict envelope")
        try:
            choice = envelope["choices"][0]
            expected_facts = {
                "response_id": envelope["id"],
                "created": envelope["created"],
                "served_model": envelope["model"],
                "finish_reason": choice["finish_reason"],
                "system_fingerprint": envelope.get("system_fingerprint"),
                "usage": envelope["usage"],
            }
        except (KeyError, IndexError, TypeError) as exc:
            raise EvaluationError("model raw response cannot support its response facts") from exc
        if response_facts != expected_facts:
            raise EvaluationError("model response facts differ from the raw envelope")
        if (
            not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in usage.values())
            or usage["total_tokens"]
            != usage["prompt_tokens"] + usage["completion_tokens"]
        ):
            raise EvaluationError("model response usage is invalid")
        if usage["completion_tokens"] > settings["max_output_tokens"]:
            raise EvaluationError("model response usage exceeds the output-token limit")
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise EvaluationError("model response facts lack a textual message")
        message_bytes = message["content"].encode("utf-8", errors="strict")
        if (
            output["message_sha256"] != _sha256(message_bytes)
            or output["message_bytes"] != len(message_bytes)
        ):
            raise EvaluationError("model manifest message audit differs from response")
    elif output["message_sha256"] is not None or output["message_bytes"] != 0:
        raise EvaluationError("model manifest has message bytes without response facts")

    timing = _require_exact_keys(
        item["timing"], {"elapsed_ns", "elapsed_ms"}, label="model timing"
    )
    if (
        not isinstance(timing["elapsed_ns"], int)
        or isinstance(timing["elapsed_ns"], bool)
        or timing["elapsed_ns"] < 0
        or timing["elapsed_ms"] != round(timing["elapsed_ns"] / 1_000_000, 3)
    ):
        raise EvaluationError("model timing is invalid")

    validation = _require_exact_keys(
        item["validation"],
        {"status", "phase", "error_class", "error_code"},
        label="model validation record",
    )
    if validation["status"] not in {"accepted", "rejected", "not-run"}:
        raise EvaluationError("model validation status is invalid")
    claim_ids = item["claim_ids"]
    expected_claim_ids = [claim.get("id") for claim in claims]
    if (
        not isinstance(item["claim_count"], int)
        or isinstance(item["claim_count"], bool)
        or item["claim_count"] != len(claims)
        or not isinstance(claim_ids, list)
        or claim_ids != expected_claim_ids
        or any(not isinstance(value, str) or not value for value in claim_ids)
    ):
        raise EvaluationError("model manifest claim inventory differs from artifact")
    fallback = item["fallback"]
    if item["outcome"] == "success":
        if _require_exact_keys(
            fallback, {"used", "reason"}, label="model success fallback record"
        ) != {"used": False, "reason": None}:
            raise EvaluationError("successful model manifest has an invalid fallback record")
        if validation != {
            "status": "accepted",
            "phase": "response-validation",
            "error_class": None,
            "error_code": None,
        } or facts is None:
            raise EvaluationError("successful model manifest was not strictly validated")
    else:
        failure_fallback = _require_exact_keys(
            fallback,
            {"used", "reason", "deterministic"},
            label="model failure fallback record",
        )
        if not isinstance(failure_fallback["used"], bool) or not isinstance(
            failure_fallback["deterministic"], bool
        ):
            raise EvaluationError("model failure fallback booleans are invalid")
        if (
            item["claim_count"] != 0
            or item["claim_ids"] != []
            or validation["status"] == "accepted"
            or not isinstance(validation["phase"], str)
            or not isinstance(validation["error_class"], str)
            or not isinstance(validation["error_code"], str)
            or failure_fallback["reason"]
            != item["outcome"].removeprefix("error-")
            or failure_fallback["deterministic"] is not False
        ):
            raise EvaluationError("failed model manifest has inconsistent audit fields")
    if item["fallback"]["used"] is not False:
        raise EvaluationError("benchmark model manifest used a forbidden fallback")


def _verify_artifact_identity(
    artifact: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    candidate: Mapping[str, str],
) -> None:
    if artifact.get("schema") != CANDIDATE_ARTIFACT_SCHEMA:
        raise EvaluationError(f"candidate {candidate['name']!r} has invalid artifact schema")
    value = dict(artifact)
    digest = value.pop("artifact_sha256", None)
    if not isinstance(digest, str) or digest != _sha256(_canonical_bytes(value)):
        raise EvaluationError(f"candidate {candidate['name']!r} artifact self-hash is invalid")
    bindings = {
        "freeze_sha256": freeze["freeze_sha256"],
        "policy_sha256": freeze["policy"]["sha256"],
        "hero_manifest_sha256": freeze["hero"]["manifest_sha256"],
        "extraction_sha256": freeze["extraction"]["sha256"],
        "evidence_pack_sha256": freeze["evidence_pack"]["sha256"],
        "request_sha256": _request_binding(freeze, candidate["model_id"])["sha256"],
    }
    for key, expected in bindings.items():
        if artifact.get(key) != expected:
            raise EvaluationError(
                f"candidate {candidate['name']!r} artifact has wrong {key}"
            )
    if artifact.get("candidate") != dict(candidate):
        raise EvaluationError(f"candidate {candidate['name']!r} identity differs from policy")


def score_candidate(
    artifact: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    extraction: Any,
    policy: Mapping[str, Any],
    candidate_order: int,
) -> dict[str, Any]:
    """Score one candidate using only preregistered per-fact and hard gates."""

    candidate = policy_candidates(policy)[candidate_order]
    _verify_artifact_identity(artifact, freeze=freeze, candidate=candidate)
    repeats = artifact.get("repeats")
    expected_repeats = policy["generation"]["repeats"]
    if not isinstance(repeats, list) or len(repeats) != expected_repeats:
        raise EvaluationError(
            f"candidate {candidate['name']!r} must contain {expected_repeats} repeats"
        )

    unit_by_id = {unit.id: unit for unit in extraction.units}
    if len(unit_by_id) != len(extraction.units):
        raise EvaluationError("benchmark extraction has duplicate unit IDs")
    allowed_ids = set(freeze["evidence_pack"]["selection"]["selected_unit_ids"])
    response_policy = policy["response"]
    eligibility = policy["eligibility"]
    repeat_scores: list[dict[str, Any]] = []
    all_failed_gates: list[str] = []

    from autotldr.unit import GroundedStatement

    for expected_index, repeat in enumerate(repeats, start=1):
        if not isinstance(repeat, dict) or repeat.get("repeat") != expected_index:
            raise EvaluationError(
                f"candidate {candidate['name']!r} repeat ordering is invalid"
            )
        request_body = _decode_bytes_record(
            repeat.get("request"), label=f"{candidate['name']} repeat {expected_index} request"
        )
        response_body = _decode_bytes_record(
            repeat.get("raw_response"),
            label=f"{candidate['name']} repeat {expected_index} response",
        )
        assert request_body is not None
        request_binding = _request_binding(freeze, candidate["model_id"])
        if _sha256(request_body) != request_binding["sha256"]:
            raise EvaluationError("candidate request bytes differ from freeze")
        manifest = repeat.get("model_manifest")
        _require_model_manifest(
            manifest,
            model_id=candidate["model_id"],
            freeze=freeze,
            policy=policy,
            request_body=request_body,
            response_body=response_body,
            claims=repeat.get("claims", []),
            expected_role_backend=extraction.meta.get(
                "role_backend", "deterministic-rules-v1"
            ),
        )

        runtime_ok = (
            repeat.get("run_error") is None
            and repeat.get("transport_error") is None
            and manifest.get("outcome") == "success"
        )
        claims = repeat.get("claims")
        if not isinstance(claims, list):
            raise EvaluationError("repeat claims must be an array")
        provenance_errors: list[str] = []
        seen_claim_ids: set[str] = set()
        normalized_claims: list[dict[str, Any]] = []
        for claim_index, claim in enumerate(claims, start=1):
            if not isinstance(claim, dict):
                provenance_errors.append(f"claim {claim_index} is not an object")
                continue
            content = claim.get("content")
            ids = claim.get("evidence_unit_ids")
            origins = claim.get("origins")
            if not isinstance(content, str) or not isinstance(ids, list) or not all(
                isinstance(item, str) for item in ids
            ):
                provenance_errors.append(f"claim {claim_index} has invalid content/IDs")
                continue
            if not ids or len(ids) != len(set(ids)):
                provenance_errors.append(f"claim {claim_index} has empty/duplicate IDs")
                continue
            unknown = sorted(set(ids) - allowed_ids)
            if unknown:
                provenance_errors.append(
                    f"claim {claim_index} cites IDs outside the frozen pack: {unknown}"
                )
                continue
            expected_origins: list[dict[str, Any]] = []
            origin_objects: list[Any] = []
            seen_origins: set[Any] = set()
            for unit_id in ids:
                origin = unit_by_id[unit_id].origin
                if origin not in seen_origins:
                    seen_origins.add(origin)
                    origin_objects.append(origin)
                    expected_origins.append(_origin_record(origin))
            if origins != expected_origins:
                provenance_errors.append(f"claim {claim_index} origins do not equal cited units")
                continue
            try:
                rebuilt = GroundedStatement(content, tuple(origin_objects), tuple(ids))
            except ValueError as exc:
                provenance_errors.append(f"claim {claim_index} is invalid: {exc}")
                continue
            if claim.get("id") != rebuilt.id or rebuilt.id in seen_claim_ids:
                provenance_errors.append(f"claim {claim_index} ID is invalid or duplicate")
                continue
            seen_claim_ids.add(rebuilt.id)
            normalized_claims.append(
                {"content": content, "evidence_unit_ids": list(ids), "id": rebuilt.id}
            )
        manifest_claim_ids = manifest.get("claim_ids")
        if manifest_claim_ids != [claim["id"] for claim in normalized_claims]:
            provenance_errors.append("model manifest claim IDs differ from audited claims")
        if manifest.get("claim_count") != len(normalized_claims):
            provenance_errors.append("model manifest claim count differs from audited claims")

        schema_and_provenance_valid = (
            runtime_ok
            and manifest.get("validation", {}).get("status") == "accepted"
            and not provenance_errors
        )
        per_claim_characters = [len(claim["content"]) for claim in normalized_claims]
        total_characters = sum(per_claim_characters)
        response_limits_ok = (
            response_policy["minimum_claims"]
            <= len(normalized_claims)
            <= response_policy["maximum_claims"]
            and all(
                length <= response_policy["maximum_claim_characters"]
                for length in per_claim_characters
            )
            and total_characters <= response_policy["maximum_total_claim_characters"]
        )
        source_by_id = {unit_id: unit_by_id[unit_id].source for unit_id in allowed_ids}
        support_text_by_id: dict[str, list[str]] = {
            unit_id: (
                []
                if str(unit_by_id[unit_id].modality) in {"source", "reference"}
                else [unit_by_id[unit_id].content]
            )
            for unit_id in allowed_ids
        }
        ids_by_origin: dict[Any, list[str]] = {}
        for unit_id in allowed_ids:
            ids_by_origin.setdefault(unit_by_id[unit_id].origin, []).append(unit_id)
        for gap in extraction.gaps:
            for unit_id in ids_by_origin.get(gap.origin, ()):
                support_text_by_id[unit_id].append(gap.content)
        fact_scores = [
            _score_fact(
                fact,
                normalized_claims,
                source_by_id,
                support_text_by_id,
            )
            for fact in policy["required_facts"]
        ]
        recovered_fact_ids = [item["id"] for item in fact_scores if item["recovered"]]
        cited_sources = sorted(
            {
                source_by_id[unit_id]
                for claim in normalized_claims
                for unit_id in claim["evidence_unit_ids"]
            }
        )
        tier3_sources = [source for source in cited_sources if _source_is_tier3(source)]
        required_ids_ok = set(eligibility["required_fact_ids_each_repeat"]).issubset(
            recovered_fact_ids
        )
        fact_count_ok = (
            len(recovered_fact_ids) >= eligibility["minimum_required_facts_each_repeat"]
        )
        source_count_ok = (
            len(cited_sources) >= eligibility["minimum_distinct_cited_sources_each_repeat"]
        )
        tier3_count_ok = (
            len(tier3_sources)
            >= eligibility["minimum_distinct_cited_tier3_sources_each_repeat"]
        )

        gates = {
            "runtime_ok": runtime_ok,
            "schema_and_provenance_valid": schema_and_provenance_valid,
            "response_limits_ok": response_limits_ok,
            "minimum_required_facts": fact_count_ok,
            "required_fact_ids": required_ids_ok,
            "minimum_distinct_cited_sources": source_count_ok,
            "minimum_distinct_cited_tier3_sources": tier3_count_ok,
        }
        failed = [name for name, passed in gates.items() if not passed]
        all_failed_gates.extend(
            f"repeat-{expected_index}:{name}" for name in failed
        )
        repeat_scores.append(
            {
                "repeat": expected_index,
                "facts": fact_scores,
                "recovered_fact_ids": recovered_fact_ids,
                "cited_sources": cited_sources,
                "cited_tier3_sources": tier3_sources,
                "claim_characters": per_claim_characters,
                "total_claim_characters": total_characters,
                "latency_ms": manifest.get("timing", {}).get("elapsed_ms"),
                "provenance_errors": provenance_errors,
                "hard_gates": gates,
                "failed_hard_gates": failed,
            }
        )

    per_fact = []
    for fact in policy["required_facts"]:
        outcomes = [
            next(item for item in repeat["facts"] if item["id"] == fact["id"])[
                "recovered"
            ]
            for repeat in repeat_scores
        ]
        per_fact.append(
            {
                "id": fact["id"],
                "repeat_recovered": outcomes,
                "recovered_all_repeats": all(outcomes),
            }
        )
    latencies = [
        float(repeat["latency_ms"])
        for repeat in repeat_scores
        if isinstance(repeat["latency_ms"], (int, float))
        and not isinstance(repeat["latency_ms"], bool)
    ]
    if len(latencies) != expected_repeats:
        all_failed_gates.append("candidate:complete-latency-records")
    metrics = {
        "failed_hard_gates": len(all_failed_gates),
        "minimum_repeat_required_facts": min(
            len(repeat["recovered_fact_ids"]) for repeat in repeat_scores
        ),
        "minimum_repeat_distinct_cited_sources": min(
            len(repeat["cited_sources"]) for repeat in repeat_scores
        ),
        "total_claim_characters": sum(
            repeat["total_claim_characters"] for repeat in repeat_scores
        ),
        "median_latency_ms": statistics.median(latencies) if latencies else None,
    }
    return {
        "candidate": candidate,
        "eligible": not all_failed_gates,
        "failed_hard_gates": all_failed_gates,
        "per_fact": per_fact,
        "repeats": repeat_scores,
        "selection_metrics": metrics,
        "candidate_order": candidate_order,
    }


def _selection_key(score: Mapping[str, Any]) -> tuple[Any, ...]:
    metrics = score["selection_metrics"]
    latency = metrics["median_latency_ms"]
    return (
        metrics["failed_hard_gates"],
        -metrics["minimum_repeat_required_facts"],
        -metrics["minimum_repeat_distinct_cited_sources"],
        metrics["total_claim_characters"],
        float("inf") if latency is None else latency,
        score["candidate_order"],
    )


def score_directory(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    freeze_path: Path = DEFAULT_FREEZE_PATH,
    extraction: Any | None = None,
    policy_path: Path = POLICY_PATH,
    hero_dir: Path = HERO_DIR,
    hero_manifest_path: Path = HERO_MANIFEST_PATH,
) -> dict[str, Any]:
    """Score the exact four-candidate set and select only among eligible runs."""

    freeze = read_freeze(freeze_path)
    verified_freeze, active_extraction = verify_freeze_record(
        freeze,
        extraction=extraction,
        policy_path=policy_path,
        hero_dir=hero_dir,
        hero_manifest_path=hero_manifest_path,
    )
    policy, _ = load_policy(policy_path)
    scores: list[dict[str, Any]] = []
    for order, candidate in enumerate(policy_candidates(policy)):
        path = output_dir / f"{candidate['slug']}.json"
        artifact, _ = _read_object(path, label=f"candidate artifact {candidate['name']}")
        scores.append(
            score_candidate(
                artifact,
                freeze=verified_freeze,
                extraction=active_extraction,
                policy=policy,
                candidate_order=order,
            )
        )
    ranking = sorted(scores, key=_selection_key)
    eligible = [item for item in ranking if item["eligible"]]
    selected = eligible[0]["candidate"] if eligible else None
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "benchmark": policy["benchmark"],
        "freeze_sha256": verified_freeze["freeze_sha256"],
        "aggregate_accuracy_computed": False,
        "selection_policy": policy["selection"],
        "candidates": scores,
        "ranking": [item["candidate"]["name"] for item in ranking],
        "selected_candidate": selected,
        "eligible_candidate_count": len(eligible),
    }
    report["report_sha256"] = _sha256(_canonical_bytes(report))
    return report


# ---------------------------------------------------------------------------
# Stage 5 grounded-synthesis benchmark v2
# ---------------------------------------------------------------------------


def _resolved_unit_locator(unit: Any, *, selected: bool) -> dict[str, Any]:
    return {
        "source": unit.source,
        "ref": unit.origin.ref,
        "modality": str(unit.modality),
        "resolved_unit_id": unit.id,
        "content_sha256": _sha256(unit.content.encode("utf-8", errors="strict")),
        "selected_in_evidence_pack": selected,
    }


def _resolve_unit_locator(
    locator: Mapping[str, Any],
    *,
    units: Sequence[Any],
    selected_ids: set[str],
    label: str,
) -> dict[str, Any]:
    matches = [
        unit
        for unit in units
        if unit.source == locator.get("source")
        and unit.origin.ref == locator.get("ref")
        and str(unit.modality) == locator.get("modality")
    ]
    if len(matches) != 1:
        raise EvaluationError(
            f"{label} must resolve to exactly one extraction unit; found {len(matches)}"
        )
    unit = matches[0]
    return _resolved_unit_locator(unit, selected=unit.id in selected_ids)


def resolve_truth_bindings(
    extraction: Any,
    selected_unit_ids: Sequence[str],
    truth: Mapping[str, Any],
    *,
    evidence_pack_bytes: bytes,
    source_payloads: Mapping[str, bytes],
    source_canary_values: Mapping[str, str],
) -> dict[str, Any]:
    """Resolve human source addresses against the exact frozen extraction.

    Source/ref/modality is the authored label.  The generated unit ID and
    content digest are consequences that enter the freeze; neither can be
    silently copied from a stale benchmark document.
    """

    units = tuple(getattr(extraction, "units", ()))
    unit_ids = [unit.id for unit in units]
    if len(unit_ids) != len(set(unit_ids)):
        raise EvaluationError("truth binding extraction contains duplicate unit IDs")
    selected_ids = set(selected_unit_ids)
    if len(selected_ids) != len(tuple(selected_unit_ids)):
        raise EvaluationError("truth binding selected unit IDs contain duplicates")
    unknown_selected = sorted(selected_ids - set(unit_ids))
    if unknown_selected:
        raise EvaluationError(
            "truth binding evidence selection names unknown units: "
            + ", ".join(unknown_selected)
        )

    fact_bindings: list[dict[str, Any]] = []
    for fact in truth["facts"]:
        evidence_sets: list[dict[str, Any]] = []
        for set_index, evidence_set in enumerate(fact["acceptable_evidence_sets"]):
            resolved = [
                _resolve_unit_locator(
                    locator,
                    units=units,
                    selected_ids=selected_ids,
                    label=f"truth fact {fact['id']} evidence set {set_index}",
                )
                for locator in evidence_set
            ]
            evidence_sets.append(
                {
                    "index": set_index,
                    "available_in_evidence_pack": all(
                        item["selected_in_evidence_pack"] for item in resolved
                    ),
                    "units": resolved,
                }
            )
        if not any(item["available_in_evidence_pack"] for item in evidence_sets):
            raise EvaluationError(
                f"truth fact {fact['id']!r} has no complete evidence set in the frozen pack"
            )
        finding_bindings: list[dict[str, Any]] = []
        for finding in fact.get("required_findings", []):
            matches = [
                gap
                for gap in getattr(extraction, "gaps", ())
                if gap.origin.source == finding["source"]
                and gap.origin.ref == finding["ref"]
                and str(gap.kind) == finding["kind"]
            ]
            if len(matches) != 1:
                raise EvaluationError(
                    f"truth fact {fact['id']!r} required finding must resolve once; "
                    f"found {len(matches)}"
                )
            gap = matches[0]
            finding_bindings.append(
                {
                    "source": gap.origin.source,
                    "ref": gap.origin.ref,
                    "kind": str(gap.kind),
                    "content_sha256": _sha256(
                        gap.content.encode("utf-8", errors="strict")
                    ),
                }
            )
        fact_bindings.append(
            {
                "id": fact["id"],
                "evidence_sets": evidence_sets,
                "required_findings": finding_bindings,
            }
        )

    negative_bindings: list[dict[str, Any]] = []
    for negative in truth["hard_negatives"]:
        negative_bindings.append(
            {
                "id": negative["id"],
                "basis": negative["basis"],
                "evidence": [
                    _resolve_unit_locator(
                        locator,
                        units=units,
                        selected_ids=selected_ids,
                        label=f"hard negative {negative['id']}",
                    )
                    for locator in negative["evidence"]
                ],
            }
        )

    canary_bindings: list[dict[str, Any]] = []
    complete_extraction_bytes = _canonical_bytes(
        {
            "extraction": extraction_record(extraction),
            "meta": getattr(extraction, "meta", {}),
        }
    )
    for canary in truth["payload_canaries"]:
        literal = canary["literal"].encode("utf-8", errors="strict")
        source_payload = source_payloads.get(canary["source"])
        observed_value = source_canary_values.get(canary["id"])
        if not isinstance(source_payload, bytes) or observed_value != canary["literal"]:
            raise EvaluationError(
                f"payload canary {canary['id']!r} is not present at its native locator"
            )
        absent_from_extraction = literal not in complete_extraction_bytes
        absent_from_pack = literal not in evidence_pack_bytes
        if not absent_from_extraction or not absent_from_pack:
            raise EvaluationError(
                f"payload canary {canary['id']!r} occurs in the complete extraction "
                "or model evidence pack"
            )
        canary_bindings.append(
            {
                "id": canary["id"],
                "status": canary["status"],
                "native_locator": canary["native_locator"],
                "literal_sha256": _sha256(literal),
                "literal_bytes": len(literal),
                "source_payload_sha256": _sha256(source_payload),
                "present_at_native_locator": True,
                "literal_visible_in_container_bytes": literal in source_payload,
                "absent_from_complete_extraction": True,
                "absent_from_evidence_pack": True,
            }
        )

    core: dict[str, Any] = {
        "schema": "autotldr-stage5-synthesis-truth-binding-v2",
        "facts": fact_bindings,
        "hard_negatives": negative_bindings,
        "payload_canaries": canary_bindings,
    }
    core["binding_sha256"] = _sha256(_canonical_bytes(core))
    return core


def _truth_direct_source_path(source: str, *, hero_dir: Path) -> Path:
    prefix = hero_dir.name + "/"
    if not source.startswith(prefix) or "!/" in source:
        raise EvaluationError(
            f"payload canary source {source!r} is outside the direct hero members"
        )
    relative = PurePosixPath(source[len(prefix) :])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != source[len(prefix) :]
    ):
        raise EvaluationError(f"payload canary source {source!r} is unsafe")
    path = hero_dir.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise EvaluationError(f"payload canary source {source!r} is not a real file")
    return path


def _truth_source_payloads(
    truth: Mapping[str, Any], *, hero_dir: Path
) -> dict[str, bytes]:
    """Read only the explicitly bound raw hero sources used by canaries."""

    payloads: dict[str, bytes] = {}
    for canary in truth["payload_canaries"]:
        source = canary["source"]
        path = _truth_direct_source_path(source, hero_dir=hero_dir)
        try:
            payloads[source] = path.read_bytes()
        except OSError as exc:
            raise EvaluationError(f"cannot read payload canary source {source!r}: {exc}") from exc
    return payloads


def _truth_canary_values(
    truth: Mapping[str, Any], *, hero_dir: Path
) -> dict[str, str]:
    """Read every fixture canary through its frozen native address."""

    values: dict[str, str] = {}
    for canary in truth["payload_canaries"]:
        source = canary["source"]
        path = _truth_direct_source_path(source, hero_dir=hero_dir)
        locator = canary["native_locator"]
        if path.name == "capacity.xlsx" and locator == "sheet:_raw_canary#cell:A1":
            import openpyxl

            workbook = openpyxl.load_workbook(path, data_only=True, read_only=False)
            try:
                sheet = workbook["_raw_canary"]
                if sheet.sheet_state != "veryHidden":
                    raise EvaluationError("XLSX canary worksheet is not veryHidden")
                value = sheet["A1"].value
            finally:
                workbook.close()
        elif path.name == "measurements.parquet" and locator == "column:raw_note#row:0":
            import pyarrow.parquet as pq

            table = pq.read_table(path, columns=["raw_note"])
            value = table.column("raw_note")[0].as_py()
        elif (
            path.name == "safety.sqlite"
            and locator == "table:stations#pk:alpha#column:description"
        ):
            import sqlite3

            uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as connection:
                row = connection.execute(
                    "SELECT description FROM stations WHERE station_id = ?",
                    ("alpha",),
                ).fetchone()
            value = row[0] if row is not None else None
        elif (
            path.name == "analytics.duckdb"
            and locator == "table:main.telemetry_profile#pk:alpha#column:raw_note"
        ):
            import duckdb

            connection = duckdb.connect(database=str(path), read_only=True)
            try:
                row = connection.execute(
                    "SELECT raw_note FROM telemetry_profile WHERE station_id = ?",
                    ["alpha"],
                ).fetchone()
            finally:
                connection.close()
            value = row[0] if row is not None else None
        elif (
            path.name == "experiments.h5"
            and locator == "/experiments/run_001/raw_note#scalar"
        ):
            import h5py

            with h5py.File(path, "r") as handle:
                value = bytes(handle["/experiments/run_001/raw_note"][()]).decode(
                    "ascii"
                )
        elif path.name == "forecast.nc" and locator == "/raw_note#chars":
            import netCDF4

            dataset = netCDF4.Dataset(path, mode="r")
            try:
                value = dataset.variables["raw_note"][:].tobytes().decode("ascii")
            finally:
                dataset.close()
        else:
            raise EvaluationError(
                f"payload canary {canary['id']!r} has an unsupported native locator"
            )
        if not isinstance(value, str):
            raise EvaluationError(
                f"payload canary {canary['id']!r} native value is not text"
            )
        values[canary["id"]] = value
    return values


def _failure_http_response(message_content: str) -> bytes:
    return _canonical_bytes(
        {
            "id": "autotldr-failure-injection",
            "object": "chat.completion",
            "created": 1788141600,
            "model": "autotldr-stage5-failure-injection",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": message_content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }
    )


def _failure_payload(case_id: str, *, evidence_id: str, maximum: int) -> bytes | BaseException:
    from autotldr.synthesis import (
        MAX_CLAIM_BYTES,
        SynthesisClientError,
        SynthesisTimeoutError,
    )

    if case_id == "timeout":
        return SynthesisTimeoutError("injected deterministic timeout")
    if case_id == "connection_failure":
        return SynthesisClientError("injected deterministic connection failure")
    if case_id == "non_utf8":
        return b"\xff"
    if case_id == "oversized_response":
        return b"x" * (maximum + 1)
    if case_id == "malformed_json":
        content = "{"
    elif case_id == "duplicate_json_key":
        content = '{"claims":[],"claims":[]}'
    elif case_id == "unknown_id":
        content = json.dumps(
            {
                "claims": [
                    {
                        "content": "Unsupported injected claim.",
                        "evidence_unit_ids": ["not-in-the-evidence-pack"],
                    }
                ]
            },
            separators=(",", ":"),
        )
    elif case_id == "empty_ids":
        content = json.dumps(
            {
                "claims": [
                    {
                        "content": "Injected claim without evidence.",
                        "evidence_unit_ids": [],
                    }
                ]
            },
            separators=(",", ":"),
        )
    elif case_id == "duplicate_ids":
        content = json.dumps(
            {
                "claims": [
                    {
                        "content": "Injected duplicate-evidence claim.",
                        "evidence_unit_ids": [evidence_id, evidence_id],
                    }
                ]
            },
            separators=(",", ":"),
        )
    elif case_id == "extra_fields":
        content = json.dumps(
            {
                "claims": [
                    {
                        "content": "Injected extra-field claim.",
                        "evidence_unit_ids": [evidence_id],
                        "origin": "model-supplied-origin",
                    }
                ]
            },
            separators=(",", ":"),
        )
    elif case_id == "overlong_claim":
        content = json.dumps(
            {
                "claims": [
                    {
                        "content": "x" * (MAX_CLAIM_BYTES + 1),
                        "evidence_unit_ids": [evidence_id],
                    }
                ]
            },
            separators=(",", ":"),
        )
    else:
        raise EvaluationError(f"no deterministic failure injection for {case_id!r}")
    return _failure_http_response(content)


class _InjectedFailureClient:
    endpoint_url = "http://127.0.0.1:1234/v1/chat/completions"

    def __init__(self, payload: bytes | BaseException) -> None:
        from autotldr.synthesis import offline_test_transport_attestation

        self.payload = payload
        self.attestation = offline_test_transport_attestation()

    def complete(
        self,
        request_body: bytes,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> bytes:
        del request_body, timeout_seconds, max_response_bytes
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def validate_failure_injection_results(
    result: Mapping[str, Any],
    *,
    policy_v2: Mapping[str, Any],
    preoutput_binding_sha256: str,
) -> dict[str, Any]:
    """Validate exact failure cases and repeat determinism against the policy."""

    _require_exact_keys(
        result,
        {
            "schema",
            "benchmark",
            "preoutput_binding_sha256",
            "contract_sha256",
            "cases",
            "result_sha256",
        },
        label="failure injection result",
    )
    core = dict(result)
    digest = core.pop("result_sha256", None)
    if not isinstance(digest, str) or digest != _sha256(_canonical_bytes(core)):
        raise EvaluationError("failure injection result self-hash is invalid")
    if (
        result["schema"] != FAILURE_INJECTION_V2_SCHEMA
        or result["benchmark"] != policy_v2["benchmark"]
        or result["preoutput_binding_sha256"] != preoutput_binding_sha256
    ):
        raise EvaluationError("failure injection result identity is invalid")
    contract = policy_v2["failure_injection"]
    expected_contract_hash = _sha256(_canonical_bytes(contract))
    if result["contract_sha256"] != expected_contract_hash:
        raise EvaluationError("failure injection contract hash is invalid")
    cases = result["cases"]
    if not isinstance(cases, list) or len(cases) != len(contract["cases"]):
        raise EvaluationError("failure injection result has the wrong case count")
    validated: list[dict[str, Any]] = []
    for expected, observed in zip(contract["cases"], cases, strict=True):
        item = _require_exact_keys(
            observed, {"id", "repeats"}, label=f"failure result {expected['id']}"
        )
        if item["id"] != expected["id"]:
            raise EvaluationError("failure injection result order or ID changed")
        repeats = item["repeats"]
        if not isinstance(repeats, list) or len(repeats) != contract["repeats"]:
            raise EvaluationError(f"failure case {expected['id']!r} has wrong repeats")
        normalized: list[dict[str, Any]] = []
        for repeat_index, repeat in enumerate(repeats, start=1):
            record = _require_exact_keys(
                repeat,
                {
                    "repeat",
                    "outcome",
                    "validation_status",
                    "error_class",
                    "error_phase",
                    "error_code",
                    "accepted_claim_count",
                    "fallback_used",
                    "request_sha256",
                    "response_present",
                    "response_bytes",
                    "response_sha256",
                },
                label=f"failure {expected['id']} repeat {repeat_index}",
            )
            if record["repeat"] != repeat_index:
                raise EvaluationError("failure injection repeat order changed")
            if record["outcome"] != expected["expected_outcome"]:
                raise EvaluationError(f"failure {expected['id']!r} has wrong outcome")
            if record["validation_status"] != expected["expected_validation_status"]:
                raise EvaluationError(
                    f"failure {expected['id']!r} has wrong validation status"
                )
            if record["error_class"] != expected["expected_error_class"]:
                raise EvaluationError(f"failure {expected['id']!r} has wrong error class")
            if record["error_phase"] != expected["expected_error_phase"]:
                raise EvaluationError(f"failure {expected['id']!r} has wrong error phase")
            if record["error_code"] != expected["expected_error_code"]:
                raise EvaluationError(f"failure {expected['id']!r} has wrong error code")
            if record["accepted_claim_count"] != 0 or record["fallback_used"] is not False:
                raise EvaluationError(f"failure {expected['id']!r} accepted claims or fallback")
            if not isinstance(record["request_sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", record["request_sha256"]
            ):
                raise EvaluationError(f"failure {expected['id']!r} lacks request binding")
            if record["response_present"] is not expected["response_present"]:
                raise EvaluationError(f"failure {expected['id']!r} response presence changed")
            if record["response_present"]:
                if (
                    not isinstance(record["response_bytes"], int)
                    or isinstance(record["response_bytes"], bool)
                    or record["response_bytes"] < 1
                    or not isinstance(record["response_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["response_sha256"])
                ):
                    raise EvaluationError(f"failure {expected['id']!r} response audit is invalid")
            elif record["response_bytes"] != 0 or record["response_sha256"] is not None:
                raise EvaluationError(f"failure {expected['id']!r} absent response is inconsistent")
            normalized.append({key: value for key, value in record.items() if key != "repeat"})
        if normalized[0] != normalized[1]:
            raise EvaluationError(f"failure {expected['id']!r} is not deterministic")
        validated.append({"id": expected["id"], "deterministic": True})
    return {
        "result_sha256": digest,
        "validated_cases": validated,
        "all_cases_valid": True,
    }


def run_failure_injections(
    extraction: Any,
    *,
    base_policy: Mapping[str, Any],
    policy_v2: Mapping[str, Any],
    preoutput_binding_sha256: str,
) -> dict[str, Any]:
    """Exercise production synthesis with local deterministic failure clients."""

    from autotldr.synthesis import (
        EndpointPolicy,
        SynthesisConfig,
        SynthesisRunError,
        build_evidence_pack,
        synthesize,
    )

    failure = policy_v2["failure_injection"]
    pack = build_evidence_pack(
        extraction, budget_bytes=base_policy["evidence_pack"]["max_bytes"]
    )
    if not pack.unit_ids:
        raise EvaluationError("failure injection needs one frozen evidence ID")
    config = SynthesisConfig(
        model="autotldr-stage5-failure-injection",
        endpoint=base_policy["endpoint"],
        endpoint_policy=EndpointPolicy(localhost_only=True, allowed_schemes=("http",)),
        evidence_budget_bytes=base_policy["evidence_pack"]["max_bytes"],
        timeout_seconds=RUN_TIMEOUT_SECONDS,
        max_output_tokens=base_policy["generation"]["max_tokens"],
        max_response_bytes=failure["max_response_bytes"],
        temperature=base_policy["generation"]["temperature"],
        seed=base_policy["generation"]["seed"],
        fallback_on_failure=False,
    )
    case_records: list[dict[str, Any]] = []
    for case in failure["cases"]:
        repeats: list[dict[str, Any]] = []
        for repeat_index in range(1, failure["repeats"] + 1):
            payload = _failure_payload(
                case["id"],
                evidence_id=pack.unit_ids[0],
                maximum=failure["max_response_bytes"],
            )
            ticks = iter((0, failure["clock_elapsed_ns"]))
            try:
                synthesize(
                    extraction,
                    config,
                    client=_InjectedFailureClient(payload),
                    clock_ns=lambda: next(ticks),
                )
            except SynthesisRunError as exc:
                manifest = exc.model_run
            else:  # pragma: no cover - every frozen injection must fail closed
                raise EvaluationError(f"failure injection {case['id']!r} was accepted")
            output = manifest["output"]
            response_bytes = output["bytes"]
            repeats.append(
                {
                    "repeat": repeat_index,
                    "outcome": manifest["outcome"],
                    "validation_status": manifest["validation"]["status"],
                    "error_class": manifest["validation"]["error_class"],
                    "error_phase": manifest["validation"]["phase"],
                    "error_code": manifest["validation"]["error_code"],
                    "accepted_claim_count": manifest["claim_count"],
                    "fallback_used": manifest["fallback"]["used"],
                    "request_sha256": manifest["input"]["sha256"],
                    "response_present": response_bytes > 0,
                    "response_bytes": response_bytes,
                    "response_sha256": manifest["output"]["sha256"],
                }
            )
        case_records.append({"id": case["id"], "repeats": repeats})
    core: dict[str, Any] = {
        "schema": FAILURE_INJECTION_V2_SCHEMA,
        "benchmark": policy_v2["benchmark"],
        "preoutput_binding_sha256": preoutput_binding_sha256,
        "contract_sha256": _sha256(_canonical_bytes(failure)),
        "cases": case_records,
    }
    core["result_sha256"] = _sha256(_canonical_bytes(core))
    validate_failure_injection_results(
        core,
        policy_v2=policy_v2,
        preoutput_binding_sha256=preoutput_binding_sha256,
    )
    return core


def _build_blind_assignment(
    candidates: Sequence[Mapping[str, str]],
    *,
    policy_v2: Mapping[str, Any],
    preoutput_binding_sha256: str,
) -> dict[str, Any]:
    blindness = policy_v2["blindness"]
    records: list[dict[str, str]] = []
    aliases: set[str] = set()
    for candidate in candidates:
        material = (
            preoutput_binding_sha256
            + "\x00"
            + candidate["name"]
            + "\x00"
            + candidate["model_id"]
        ).encode("utf-8", errors="strict")
        alias = blindness["alias_prefix"] + _sha256(material)[
            : blindness["alias_hex_characters"]
        ]
        if alias in aliases:
            raise EvaluationError("blind candidate aliases collided")
        aliases.add(alias)
        records.append(
            {
                "candidate": candidate["name"],
                "model_id": candidate["model_id"],
                "blind_alias": alias,
            }
        )
    core: dict[str, Any] = {
        "algorithm": blindness["algorithm"],
        "preoutput_binding_sha256": preoutput_binding_sha256,
        "records": records,
    }
    core["assignment_sha256"] = _sha256(_canonical_bytes(core))
    return core


def _sidecar_binding(
    payload: bytes, *, canonical_value: Any | None = None
) -> dict[str, Any]:
    result = {"sha256": _sha256(payload), "bytes": len(payload)}
    if canonical_value is not None:
        result["canonical_sha256"] = _sha256(_canonical_bytes(canonical_value))
    return result


def _require_canonical_sidecar(
    value: Any, binding: Mapping[str, Any], *, label: str
) -> None:
    expected = binding.get("canonical_sha256")
    observed = _sha256(_canonical_bytes(value))
    if not isinstance(expected, str) or observed != expected:
        raise EvaluationError(f"{label} differs from the object bound by the v2 freeze")


def build_freeze_record_v2(
    *,
    extraction: Any | None = None,
    policy_path: Path = POLICY_PATH,
    policy_v2_path: Path = POLICY_V2_PATH,
    truth_v2_path: Path = TRUTH_V2_PATH,
    review_schema_v2_path: Path = REVIEW_SCHEMA_V2_PATH,
    hero_dir: Path = HERO_DIR,
    hero_manifest_path: Path = HERO_MANIFEST_PATH,
    allow_temporary_canaries: bool = False,
) -> dict[str, Any]:
    """Build the complete semantic/adjudication freeze before model output."""

    base_policy, _ = load_policy(policy_path)
    policy_v2, policy_v2_bytes = load_policy_v2(policy_v2_path)
    truth, truth_bytes = load_truth_v2(truth_v2_path)
    review_schema, review_schema_bytes = load_review_schema_v2(
        review_schema_v2_path
    )
    temporary_canary_ids = [
        canary["id"]
        for canary in truth["payload_canaries"]
        if canary["status"] == "temporary-test-only"
    ]
    if temporary_canary_ids and not allow_temporary_canaries:
        joined = ", ".join(repr(value) for value in temporary_canary_ids)
        raise EvaluationError(
            "temporary test-only payload canary prevents a live freeze: "
            f"{joined}; replace it with a final fixture sentinel first"
        )
    if policy_v2["base_policy_sha256"] != FROZEN_POLICY_SHA256 and (
        policy_path.resolve() == POLICY_PATH.resolve()
    ):
        raise EvaluationError("v2 policy does not bind the active v1 policy")
    fact_ids = [fact["id"] for fact in truth["facts"]]
    if fact_ids != policy_v2["eligibility"]["required_fact_ids_each_repeat"]:
        raise EvaluationError("v2 policy and truth fact order differ")
    base_fact_ids = [fact["id"] for fact in base_policy["required_facts"]]
    if fact_ids != base_fact_ids:
        raise EvaluationError("v2 truth facts differ from the frozen request fact inventory")

    active_extraction = extraction or build_hero_extraction(hero_dir)
    base_freeze = build_freeze_record(
        extraction=active_extraction,
        policy_path=policy_path,
        hero_dir=hero_dir,
        hero_manifest_path=hero_manifest_path,
    )
    if truth["hero_collection"] != base_freeze["hero"]["collection"]:
        raise EvaluationError("v2 truth ledger names the wrong hero collection")

    from autotldr.synthesis import build_evidence_pack

    pack = build_evidence_pack(
        active_extraction,
        budget_bytes=base_policy["evidence_pack"]["max_bytes"],
    )
    truth_binding = resolve_truth_bindings(
        active_extraction,
        pack.unit_ids,
        truth,
        evidence_pack_bytes=pack.to_bytes(),
        source_payloads=_truth_source_payloads(truth, hero_dir=hero_dir),
        source_canary_values=_truth_canary_values(truth, hero_dir=hero_dir),
    )
    evaluator_bytes = Path(__file__).read_bytes()
    preoutput_core = {
        "base_freeze_sha256": base_freeze["freeze_sha256"],
        "base_policy_canonical_sha256": _sha256(_canonical_bytes(base_policy)),
        "policy_v2_sha256": _sha256(policy_v2_bytes),
        "truth_v2_sha256": _sha256(truth_bytes),
        "review_schema_v2_sha256": _sha256(review_schema_bytes),
        "truth_binding_sha256": truth_binding["binding_sha256"],
        "evaluator_sha256": _sha256(evaluator_bytes),
    }
    preoutput_binding_sha256 = _sha256(_canonical_bytes(preoutput_core))
    candidates = policy_candidates(base_policy)
    blind_assignment = _build_blind_assignment(
        candidates,
        policy_v2=policy_v2,
        preoutput_binding_sha256=preoutput_binding_sha256,
    )
    failures = run_failure_injections(
        active_extraction,
        base_policy=base_policy,
        policy_v2=policy_v2,
        preoutput_binding_sha256=preoutput_binding_sha256,
    )
    render_core: dict[str, Any] = {
        "budgets": policy_v2["render_budgets"],
        "semantic_requirements": policy_v2["render_semantic_requirements"],
    }
    render_core["binding_sha256"] = _sha256(_canonical_bytes(render_core))

    core: dict[str, Any] = {
        "schema": FREEZE_V2_SCHEMA,
        "benchmark": policy_v2["benchmark"],
        "frozen_before_model_outputs": True,
        "base_freeze": base_freeze,
        "policy_v2": _sidecar_binding(
            policy_v2_bytes, canonical_value=policy_v2
        ),
        "truth_v2": _sidecar_binding(truth_bytes, canonical_value=truth),
        "review_schema_v2": _sidecar_binding(
            review_schema_bytes, canonical_value=review_schema
        ),
        "evaluator": _sidecar_binding(evaluator_bytes),
        "preoutput_core": preoutput_core,
        "preoutput_binding_sha256": preoutput_binding_sha256,
        "truth_binding": truth_binding,
        "blind_assignment": blind_assignment,
        "failure_injections": failures,
        "render_matrix": render_core,
        "temporary_canary_ids": temporary_canary_ids,
        "live_freeze_eligible": not temporary_canary_ids,
        "aggregate_accuracy_computed": False,
    }
    core["freeze_sha256"] = _freeze_digest(core)
    return core


def verify_freeze_record_v2(
    freeze: Mapping[str, Any],
    *,
    extraction: Any | None = None,
    policy_path: Path = POLICY_PATH,
    policy_v2_path: Path = POLICY_V2_PATH,
    truth_v2_path: Path = TRUTH_V2_PATH,
    review_schema_v2_path: Path = REVIEW_SCHEMA_V2_PATH,
    hero_dir: Path = HERO_DIR,
    hero_manifest_path: Path = HERO_MANIFEST_PATH,
    allow_temporary_canaries: bool = False,
) -> tuple[dict[str, Any], Any]:
    """Rebuild every v2 binding, including deterministic failure behavior."""

    if not isinstance(freeze, Mapping) or freeze.get("schema") != FREEZE_V2_SCHEMA:
        raise EvaluationError("invalid Stage 5 synthesis v2 freeze schema")
    observed = dict(freeze)
    digest = observed.pop("freeze_sha256", None)
    if not isinstance(digest, str) or digest != _freeze_digest(observed):
        raise EvaluationError("synthesis v2 freeze self-hash is invalid")
    active_extraction = extraction or build_hero_extraction(hero_dir)
    expected = build_freeze_record_v2(
        extraction=active_extraction,
        policy_path=policy_path,
        policy_v2_path=policy_v2_path,
        truth_v2_path=truth_v2_path,
        review_schema_v2_path=review_schema_v2_path,
        hero_dir=hero_dir,
        hero_manifest_path=hero_manifest_path,
        allow_temporary_canaries=allow_temporary_canaries,
    )
    if _canonical_bytes(dict(freeze)) != _canonical_bytes(expected):
        raise EvaluationError(
            "current extraction, sidecars, truth bindings, failure behavior, or "
            "review plan differs from the v2 freeze"
        )
    return expected, active_extraction


def read_freeze_v2(path: Path) -> dict[str, Any]:
    value, _ = _read_object(path, label="synthesis v2 freeze")
    return value


def _validate_v2_freeze_digest(freeze: Mapping[str, Any]) -> None:
    if freeze.get("schema") != FREEZE_V2_SCHEMA:
        raise EvaluationError("review input is not a synthesis v2 freeze")
    core = dict(freeze)
    digest = core.pop("freeze_sha256", None)
    if not isinstance(digest, str) or digest != _freeze_digest(core):
        raise EvaluationError("review input v2 freeze self-hash is invalid")
    temporary_ids = freeze.get("temporary_canary_ids")
    if (
        not isinstance(temporary_ids, list)
        or not all(isinstance(value, str) and value for value in temporary_ids)
        or len(temporary_ids) != len(set(temporary_ids))
    ):
        raise EvaluationError("review input has invalid temporary canary IDs")
    bound_temporary_ids = [
        item.get("id")
        for item in freeze.get("truth_binding", {}).get("payload_canaries", [])
        if item.get("status") == "temporary-test-only"
    ]
    if temporary_ids != bound_temporary_ids:
        raise EvaluationError("review input temporary canary binding is inconsistent")
    if freeze.get("live_freeze_eligible") is not (not temporary_ids):
        raise EvaluationError("review input live-freeze eligibility is inconsistent")
    current_evaluator = _sha256(Path(__file__).read_bytes())
    if freeze.get("evaluator", {}).get("sha256") != current_evaluator:
        raise EvaluationError("the active evaluator differs from the v2 freeze")
    for label, path, key in (
        ("v2 policy", POLICY_V2_PATH, "policy_v2"),
        ("truth ledger", TRUTH_V2_PATH, "truth_v2"),
        ("review schema", REVIEW_SCHEMA_V2_PATH, "review_schema_v2"),
    ):
        try:
            digest = _sha256(path.read_bytes())
        except OSError as exc:
            raise EvaluationError(f"cannot read frozen {label}: {exc}") from exc
        if freeze.get(key, {}).get("sha256") != digest:
            raise EvaluationError(f"the active {label} bytes differ from the v2 freeze")


def _require_base_policy_binding(
    base_policy: Mapping[str, Any], freeze_v2: Mapping[str, Any]
) -> None:
    expected = freeze_v2.get("preoutput_core", {}).get(
        "base_policy_canonical_sha256"
    )
    if expected != _sha256(_canonical_bytes(base_policy)):
        raise EvaluationError("base synthesis policy differs from the v2 freeze")


def _require_extraction_binding(extraction: Any, freeze_v2: Mapping[str, Any]) -> None:
    base = freeze_v2["base_freeze"]
    observed = _sha256(_canonical_bytes(extraction_record(extraction)))
    if observed != base["extraction"]["sha256"]:
        raise EvaluationError("active extraction differs from the v2 freeze")
    if acquisition_binding(extraction) != base["acquisition"]:
        raise EvaluationError("active acquisition manifest differs from the v2 freeze")


def _blind_record_by_candidate(
    freeze_v2: Mapping[str, Any],
) -> dict[str, Mapping[str, str]]:
    records = freeze_v2.get("blind_assignment", {}).get("records")
    if not isinstance(records, list):
        raise EvaluationError("v2 freeze blind assignment is invalid")
    result: dict[str, Mapping[str, str]] = {}
    aliases: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise EvaluationError("v2 freeze blind assignment row is invalid")
        candidate = record.get("candidate")
        alias = record.get("blind_alias")
        if not isinstance(candidate, str) or not isinstance(alias, str):
            raise EvaluationError("v2 freeze blind assignment identity is invalid")
        if candidate in result or alias in aliases:
            raise EvaluationError("v2 freeze blind assignment contains duplicates")
        result[candidate] = record
        aliases.add(alias)
    return result


def build_review_packets_v2(
    artifacts: Sequence[Mapping[str, Any]],
    *,
    freeze_v2: Mapping[str, Any],
    extraction: Any,
    base_policy: Mapping[str, Any],
    truth: Mapping[str, Any],
) -> dict[str, Any]:
    """Build deterministic, candidate-blind claim/evidence review packets."""

    _validate_v2_freeze_digest(freeze_v2)
    _require_base_policy_binding(base_policy, freeze_v2)
    _require_extraction_binding(extraction, freeze_v2)
    _require_canonical_sidecar(truth, freeze_v2["truth_v2"], label="truth ledger")
    candidates = policy_candidates(base_policy)
    expected_names = {item["name"] for item in candidates}
    artifact_by_name: dict[str, Mapping[str, Any]] = {}
    for artifact in artifacts:
        candidate = artifact.get("candidate") if isinstance(artifact, Mapping) else None
        name = candidate.get("name") if isinstance(candidate, Mapping) else None
        if not isinstance(name, str) or name not in expected_names or name in artifact_by_name:
            raise EvaluationError("review artifacts do not contain each candidate exactly once")
        artifact_by_name[name] = artifact
    if set(artifact_by_name) != expected_names:
        raise EvaluationError("review artifacts omit a frozen candidate")

    blind_by_candidate = _blind_record_by_candidate(freeze_v2)
    unit_by_id = {unit.id: unit for unit in extraction.units}
    if len(unit_by_id) != len(extraction.units):
        raise EvaluationError("review packet extraction has duplicate unit IDs")
    gaps_by_origin: dict[Any, list[Any]] = {}
    for gap in extraction.gaps:
        gaps_by_origin.setdefault(gap.origin, []).append(gap)
    packets: list[dict[str, Any]] = []
    for order, candidate in enumerate(candidates):
        artifact = artifact_by_name[candidate["name"]]
        # Reuse v1's strict byte, manifest, schema, claim-ID, and provenance
        # checks, but never its lexical fact verdict as a v2 semantic label.
        score_candidate(
            artifact,
            freeze=freeze_v2["base_freeze"],
            extraction=extraction,
            policy=base_policy,
            candidate_order=order,
        )
        alias = blind_by_candidate[candidate["name"]]["blind_alias"]
        for repeat in artifact["repeats"]:
            claims = [
                {
                    "id": claim["id"],
                    "content": claim["content"],
                    "evidence_unit_ids": list(claim["evidence_unit_ids"]),
                }
                for claim in repeat["claims"]
            ]
            cited_ids: list[str] = []
            for claim in claims:
                for unit_id in claim["evidence_unit_ids"]:
                    if unit_id not in cited_ids:
                        cited_ids.append(unit_id)
            evidence: list[dict[str, Any]] = []
            cited_findings: list[dict[str, Any]] = []
            seen_findings: set[tuple[str, str, str, str]] = set()
            for unit_id in cited_ids:
                if unit_id not in unit_by_id:
                    raise EvaluationError("review packet claim cites an unknown extraction unit")
                unit = unit_by_id[unit_id]
                evidence.append(
                    {
                        "id": unit.id,
                        "source": unit.source,
                        "ref": unit.origin.ref,
                        "modality": str(unit.modality),
                        "content": unit.content,
                    }
                )
                for gap in gaps_by_origin.get(unit.origin, ()):
                    key = (
                        str(gap.kind),
                        gap.origin.source,
                        gap.origin.ref,
                        gap.content,
                    )
                    if key not in seen_findings:
                        seen_findings.add(key)
                        cited_findings.append(
                            {
                                "kind": str(gap.kind),
                                "source": gap.origin.source,
                                "ref": gap.origin.ref,
                                "content": gap.content,
                                "evidence_unit_ids": [unit.id],
                            }
                        )
            packet_seed = {
                "freeze_sha256": freeze_v2["freeze_sha256"],
                "blind_candidate_id": alias,
                "repeat": repeat["repeat"],
                "artifact_sha256": artifact["artifact_sha256"],
                "claims": claims,
                "cited_evidence": evidence,
            }
            packet_id = "packet-" + _sha256(_canonical_bytes(packet_seed))[:20]
            packets.append(
                {
                    "packet_id": packet_id,
                    "blind_candidate_id": alias,
                    "repeat": repeat["repeat"],
                    "claims": claims,
                    "cited_evidence": evidence,
                    "cited_findings": cited_findings,
                    "facts": [
                        {
                            "id": fact["id"],
                            "canonical_proposition": fact["canonical_proposition"],
                            "semantic_slots": list(fact["semantic_slots"]),
                            "forbidden_overclaims": list(fact["forbidden_overclaims"]),
                        }
                        for fact in truth["facts"]
                    ],
                    "hard_negatives": [
                        {
                            "id": item["id"],
                            "proposition": item["proposition"],
                            "basis": item["basis"],
                        }
                        for item in truth["hard_negatives"]
                    ],
                    "payload_canaries": [
                        {
                            "id": item["id"],
                            "reason": item["reason"],
                        }
                        for item in truth["payload_canaries"]
                    ],
                }
            )
    packets.sort(key=lambda item: (item["blind_candidate_id"], item["repeat"]))
    packet_ids = [item["packet_id"] for item in packets]
    if len(packet_ids) != len(set(packet_ids)):
        raise EvaluationError("deterministic review packet IDs collided")
    core: dict[str, Any] = {
        "schema": REVIEW_PACKET_V2_SCHEMA,
        "benchmark": freeze_v2["benchmark"],
        "freeze_sha256": freeze_v2["freeze_sha256"],
        "truth_binding_sha256": freeze_v2["truth_binding"]["binding_sha256"],
        "review_schema_sha256": freeze_v2["review_schema_v2"]["sha256"],
        "blind_assignment_sha256": freeze_v2["blind_assignment"][
            "assignment_sha256"
        ],
        "packets": packets,
    }
    core["packet_set_sha256"] = _sha256(_canonical_bytes(core))
    return core


def _validate_packet_set_v2(
    packet_set: Mapping[str, Any], *, freeze_v2: Mapping[str, Any]
) -> None:
    _require_exact_keys(
        packet_set,
        {
            "schema",
            "benchmark",
            "freeze_sha256",
            "truth_binding_sha256",
            "review_schema_sha256",
            "blind_assignment_sha256",
            "packets",
            "packet_set_sha256",
        },
        label="review packet set",
    )
    core = dict(packet_set)
    digest = core.pop("packet_set_sha256", None)
    if not isinstance(digest, str) or digest != _sha256(_canonical_bytes(core)):
        raise EvaluationError("review packet set self-hash is invalid")
    expected = {
        "schema": REVIEW_PACKET_V2_SCHEMA,
        "benchmark": freeze_v2["benchmark"],
        "freeze_sha256": freeze_v2["freeze_sha256"],
        "truth_binding_sha256": freeze_v2["truth_binding"]["binding_sha256"],
        "review_schema_sha256": freeze_v2["review_schema_v2"]["sha256"],
        "blind_assignment_sha256": freeze_v2["blind_assignment"][
            "assignment_sha256"
        ],
    }
    for key, value in expected.items():
        if packet_set.get(key) != value:
            raise EvaluationError(f"review packet set has wrong {key}")
    packets = packet_set["packets"]
    if not isinstance(packets, list) or not packets:
        raise EvaluationError("review packet set has no packets")
    ids = [packet.get("packet_id") if isinstance(packet, dict) else None for packet in packets]
    if not all(isinstance(item, str) and item for item in ids) or len(ids) != len(set(ids)):
        raise EvaluationError("review packet IDs are invalid or duplicated")


def finalize_review_artifact_v2(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical self-hashed review record for write-once publication."""

    if "artifact_sha256" in record:
        raise EvaluationError("review artifact is already finalized")
    result = dict(record)
    result["artifact_sha256"] = _sha256(_canonical_bytes(result))
    return result


def write_review_artifact_v2(path: Path, review: Mapping[str, Any]) -> None:
    _atomic_write_new(
        path,
        json.dumps(review, indent=2, ensure_ascii=False).encode("utf-8") + b"\n",
    )


def _review_rationale(value: Any, schema: Mapping[str, Any], *, label: str) -> str:
    text = _nonempty_string(value, label=label)
    if not schema["rationale_min_characters"] <= len(text) <= schema[
        "rationale_max_characters"
    ]:
        raise EvaluationError(f"{label} is outside the frozen character bounds")
    return text


def validate_review_artifact_v2(
    review: Mapping[str, Any],
    *,
    packet_set: Mapping[str, Any],
    freeze_v2: Mapping[str, Any],
    truth: Mapping[str, Any],
    review_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Require exact packet/fact/claim coverage from one independent human."""

    _validate_packet_set_v2(packet_set, freeze_v2=freeze_v2)
    _require_canonical_sidecar(truth, freeze_v2["truth_v2"], label="truth ledger")
    _require_canonical_sidecar(
        review_schema,
        freeze_v2["review_schema_v2"],
        label="review schema",
    )
    _require_exact_keys(
        review,
        {
            "schema",
            "benchmark",
            "freeze_sha256",
            "packet_set_sha256",
            "review_schema_sha256",
            "reviewer_id",
            "human_authored",
            "completed_at",
            "fact_rows",
            "claim_rows",
            "summary_rows",
            "artifact_sha256",
        },
        label="human review artifact",
    )
    core = dict(review)
    digest = core.pop("artifact_sha256", None)
    if not isinstance(digest, str) or digest != _sha256(_canonical_bytes(core)):
        raise EvaluationError("human review artifact self-hash is invalid")
    expected_bindings = {
        "schema": REVIEW_ARTIFACT_V2_SCHEMA,
        "benchmark": freeze_v2["benchmark"],
        "freeze_sha256": freeze_v2["freeze_sha256"],
        "packet_set_sha256": packet_set["packet_set_sha256"],
        "review_schema_sha256": freeze_v2["review_schema_v2"]["sha256"],
    }
    for key, expected in expected_bindings.items():
        if review.get(key) != expected:
            raise EvaluationError(f"human review artifact has wrong {key}")
    reviewer_id = _nonempty_string(review["reviewer_id"], label="reviewer_id")
    if review["human_authored"] is not True:
        raise EvaluationError("synthesis v2 review must be human-authored")
    _nonempty_string(review["completed_at"], label="review.completed_at")

    packet_by_id = {packet["packet_id"]: packet for packet in packet_set["packets"]}
    fact_ids = [fact["id"] for fact in truth["facts"]]
    negative_ids = {item["id"] for item in truth["hard_negatives"]}
    canary_by_id = {item["id"]: item for item in truth["payload_canaries"]}
    expected_fact_keys = {
        (packet_id, fact_id)
        for packet_id in packet_by_id
        for fact_id in fact_ids
    }
    observed_fact_keys: set[tuple[str, str]] = set()
    normalized_facts: dict[tuple[str, str], dict[str, Any]] = {}
    fact_rows = review["fact_rows"]
    if not isinstance(fact_rows, list):
        raise EvaluationError("review fact_rows must be an array")
    for index, row in enumerate(fact_rows):
        item = _require_exact_keys(
            row, set(review_schema["fact_row_fields"]), label=f"review fact row {index}"
        )
        key = (item["packet_id"], item["fact_id"])
        if key not in expected_fact_keys or key in observed_fact_keys:
            raise EvaluationError("review fact rows have unknown or duplicate coverage")
        packet = packet_by_id[key[0]]
        claim_ids = {claim["id"] for claim in packet["claims"]}
        supporting = item["supporting_claim_ids"]
        if not isinstance(supporting, list) or not all(
            isinstance(value, str) for value in supporting
        ) or len(supporting) != len(set(supporting)) or not set(supporting).issubset(claim_ids):
            raise EvaluationError("review fact supporting claim IDs are invalid")
        if item["entailment"] not in review_schema["entailment_values"]:
            raise EvaluationError("review fact entailment value is invalid")
        if item["entailment"] == "entailed" and not supporting:
            raise EvaluationError("an entailed fact must name a supporting claim")
        if item["entailment"] != "entailed" and supporting:
            raise EvaluationError("a non-entailed fact cannot name supporting claims")
        _review_rationale(item["rationale"], review_schema, label="review fact rationale")
        observed_fact_keys.add(key)
        normalized_facts[key] = dict(item)
    if observed_fact_keys != expected_fact_keys:
        raise EvaluationError("review fact rows do not cover every packet and fact")

    expected_claim_keys = {
        (packet_id, claim["id"])
        for packet_id, packet in packet_by_id.items()
        for claim in packet["claims"]
    }
    observed_claim_keys: set[tuple[str, str]] = set()
    normalized_claims: dict[tuple[str, str], dict[str, Any]] = {}
    claim_rows = review["claim_rows"]
    if not isinstance(claim_rows, list):
        raise EvaluationError("review claim_rows must be an array")
    for index, row in enumerate(claim_rows):
        item = _require_exact_keys(
            row, set(review_schema["claim_row_fields"]), label=f"review claim row {index}"
        )
        key = (item["packet_id"], item["claim_id"])
        if key not in expected_claim_keys or key in observed_claim_keys:
            raise EvaluationError("review claim rows have unknown or duplicate coverage")
        if not isinstance(item["every_citation_relevant"], bool) or not isinstance(
            item["atomic_claim"], bool
        ):
            raise EvaluationError("review claim gates must be booleans")
        if not isinstance(item["other_raw_payload_leak"], bool):
            raise EvaluationError("review claim raw-payload leak gate must be boolean")
        for field, allowed in (
            ("hard_negative_ids", negative_ids),
            ("payload_canary_ids", set(canary_by_id)),
        ):
            values = item[field]
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ) or len(values) != len(set(values)) or not set(values).issubset(allowed):
                raise EvaluationError(f"review claim {field} is invalid")
        packet = packet_by_id[key[0]]
        claim = next(value for value in packet["claims"] if value["id"] == key[1])
        detected_canaries = sorted(
            canary_id
            for canary_id, canary in canary_by_id.items()
            if canary["literal"] in claim["content"]
        )
        if item["payload_canary_ids"] != detected_canaries:
            raise EvaluationError("review payload canaries differ from literal detection")
        if item["usefulness"] not in review_schema["usefulness_values"]:
            raise EvaluationError("review claim usefulness value is invalid")
        _review_rationale(item["rationale"], review_schema, label="review claim rationale")
        observed_claim_keys.add(key)
        normalized_claims[key] = dict(item)
    if observed_claim_keys != expected_claim_keys:
        raise EvaluationError("review claim rows do not cover every claim")

    expected_summary_keys = set(packet_by_id)
    observed_summary_keys: set[str] = set()
    normalized_summaries: dict[str, dict[str, Any]] = {}
    summary_rows = review["summary_rows"]
    if not isinstance(summary_rows, list):
        raise EvaluationError("review summary_rows must be an array")
    for index, row in enumerate(summary_rows):
        item = _require_exact_keys(
            row,
            set(review_schema["summary_row_fields"]),
            label=f"review summary row {index}",
        )
        packet_id = item["packet_id"]
        if packet_id not in expected_summary_keys or packet_id in observed_summary_keys:
            raise EvaluationError("review summary rows have unknown or duplicate coverage")
        for gate in ("coherent", "concise", "integrates_sources", "useful_over_inventory"):
            if not isinstance(item[gate], bool):
                raise EvaluationError(f"review summary {gate} must be boolean")
        _review_rationale(item["rationale"], review_schema, label="review summary rationale")
        observed_summary_keys.add(packet_id)
        normalized_summaries[packet_id] = dict(item)
    if observed_summary_keys != expected_summary_keys:
        raise EvaluationError("review summary rows do not cover every packet")
    return {
        "reviewer_id": reviewer_id,
        "artifact_sha256": digest,
        "facts": normalized_facts,
        "claims": normalized_claims,
        "summaries": normalized_summaries,
    }


def _adjudicate_boolean(
    first: bool,
    second: bool,
    third: bool | None,
    *,
    field: str,
    disagreements: list[str],
    unresolved: list[str],
) -> bool | None:
    if first == second:
        return first
    disagreements.append(field)
    if third is None:
        unresolved.append(field)
        return None
    return (int(first) + int(second) + int(third)) >= 2


def adjudicate_reviews_v2(
    first_review: Mapping[str, Any],
    second_review: Mapping[str, Any],
    *,
    packet_set: Mapping[str, Any],
    freeze_v2: Mapping[str, Any],
    truth: Mapping[str, Any],
    review_schema: Mapping[str, Any],
    policy_v2: Mapping[str, Any],
    third_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Adjudicate only gate-affecting differences; unresolved means ineligible."""

    _require_canonical_sidecar(
        policy_v2, freeze_v2["policy_v2"], label="v2 policy"
    )
    first = validate_review_artifact_v2(
        first_review,
        packet_set=packet_set,
        freeze_v2=freeze_v2,
        truth=truth,
        review_schema=review_schema,
    )
    second = validate_review_artifact_v2(
        second_review,
        packet_set=packet_set,
        freeze_v2=freeze_v2,
        truth=truth,
        review_schema=review_schema,
    )
    if first["reviewer_id"] == second["reviewer_id"]:
        raise EvaluationError("the two initial synthesis reviewers must be independent")
    third = None
    if third_review is not None:
        third = validate_review_artifact_v2(
            third_review,
            packet_set=packet_set,
            freeze_v2=freeze_v2,
            truth=truth,
            review_schema=review_schema,
        )
        if third["reviewer_id"] in {first["reviewer_id"], second["reviewer_id"]}:
            raise EvaluationError("the synthesis adjudicator must be a third human")

    fact_ids = [fact["id"] for fact in truth["facts"]]
    negative_ids = [item["id"] for item in truth["hard_negatives"]]
    canary_ids = [item["id"] for item in truth["payload_canaries"]]
    accepted_usefulness = set(
        policy_v2["eligibility"]["accepted_claim_usefulness"]
    )
    summary_gates = policy_v2["eligibility"]["required_summary_gates"]
    disagreements: list[str] = []
    unresolved: list[str] = []
    packet_results: list[dict[str, Any]] = []

    for packet in packet_set["packets"]:
        packet_id = packet["packet_id"]
        claim_ids = [claim["id"] for claim in packet["claims"]]
        fact_results: list[dict[str, Any]] = []
        for fact_id in fact_ids:
            key = (packet_id, fact_id)
            first_row = first["facts"][key]
            second_row = second["facts"][key]
            third_row = third["facts"][key] if third is not None else None
            entailed = _adjudicate_boolean(
                first_row["entailment"] == "entailed",
                second_row["entailment"] == "entailed",
                (
                    third_row["entailment"] == "entailed"
                    if third_row is not None
                    else None
                ),
                field=f"{packet_id}:fact:{fact_id}:entailed",
                disagreements=disagreements,
                unresolved=unresolved,
            )
            supporting: list[str] = []
            for claim_id in claim_ids:
                supported = _adjudicate_boolean(
                    claim_id in first_row["supporting_claim_ids"],
                    claim_id in second_row["supporting_claim_ids"],
                    (
                        claim_id in third_row["supporting_claim_ids"]
                        if third_row is not None
                        else None
                    ),
                    field=f"{packet_id}:fact:{fact_id}:claim:{claim_id}",
                    disagreements=disagreements,
                    unresolved=unresolved,
                )
                if supported is True:
                    supporting.append(claim_id)
            if entailed is True and not supporting:
                marker = f"{packet_id}:fact:{fact_id}:no-consensus-supporting-claim"
                if marker not in unresolved:
                    unresolved.append(marker)
            fact_results.append(
                {
                    "id": fact_id,
                    "entailed": entailed,
                    "supporting_claim_ids": supporting,
                }
            )

        claim_results: list[dict[str, Any]] = []
        for claim_id in claim_ids:
            key = (packet_id, claim_id)
            first_row = first["claims"][key]
            second_row = second["claims"][key]
            third_row = third["claims"][key] if third is not None else None
            relevant = _adjudicate_boolean(
                first_row["every_citation_relevant"],
                second_row["every_citation_relevant"],
                third_row["every_citation_relevant"] if third_row else None,
                field=f"{packet_id}:claim:{claim_id}:citations-relevant",
                disagreements=disagreements,
                unresolved=unresolved,
            )
            atomic = _adjudicate_boolean(
                first_row["atomic_claim"],
                second_row["atomic_claim"],
                third_row["atomic_claim"] if third_row else None,
                field=f"{packet_id}:claim:{claim_id}:atomic",
                disagreements=disagreements,
                unresolved=unresolved,
            )
            useful = _adjudicate_boolean(
                first_row["usefulness"] in accepted_usefulness,
                second_row["usefulness"] in accepted_usefulness,
                (
                    third_row["usefulness"] in accepted_usefulness
                    if third_row
                    else None
                ),
                field=f"{packet_id}:claim:{claim_id}:useful",
                disagreements=disagreements,
                unresolved=unresolved,
            )
            hard_negatives: list[str] = []
            for negative_id in negative_ids:
                detected = _adjudicate_boolean(
                    negative_id in first_row["hard_negative_ids"],
                    negative_id in second_row["hard_negative_ids"],
                    (
                        negative_id in third_row["hard_negative_ids"]
                        if third_row
                        else None
                    ),
                    field=f"{packet_id}:claim:{claim_id}:negative:{negative_id}",
                    disagreements=disagreements,
                    unresolved=unresolved,
                )
                if detected is True:
                    hard_negatives.append(negative_id)
            payload_canaries: list[str] = []
            for canary_id in canary_ids:
                detected = _adjudicate_boolean(
                    canary_id in first_row["payload_canary_ids"],
                    canary_id in second_row["payload_canary_ids"],
                    (
                        canary_id in third_row["payload_canary_ids"]
                        if third_row
                        else None
                    ),
                    field=f"{packet_id}:claim:{claim_id}:canary:{canary_id}",
                    disagreements=disagreements,
                    unresolved=unresolved,
                )
                if detected is True:
                    payload_canaries.append(canary_id)
            other_raw_payload_leak = _adjudicate_boolean(
                first_row["other_raw_payload_leak"],
                second_row["other_raw_payload_leak"],
                third_row["other_raw_payload_leak"] if third_row else None,
                field=f"{packet_id}:claim:{claim_id}:other-raw-payload-leak",
                disagreements=disagreements,
                unresolved=unresolved,
            )
            claim_results.append(
                {
                    "id": claim_id,
                    "every_citation_relevant": relevant,
                    "atomic": atomic,
                    "useful": useful,
                    "hard_negative_ids": hard_negatives,
                    "payload_canary_ids": payload_canaries,
                    "other_raw_payload_leak": other_raw_payload_leak,
                }
            )

        first_summary = first["summaries"][packet_id]
        second_summary = second["summaries"][packet_id]
        third_summary = third["summaries"][packet_id] if third is not None else None
        summary_result: dict[str, bool | None] = {}
        for gate in summary_gates:
            summary_result[gate] = _adjudicate_boolean(
                first_summary[gate],
                second_summary[gate],
                third_summary[gate] if third_summary else None,
                field=f"{packet_id}:summary:{gate}",
                disagreements=disagreements,
                unresolved=unresolved,
            )
        packet_unresolved = [item for item in unresolved if item.startswith(packet_id + ":")]
        review_eligible = (
            bool(claim_results)
            and not packet_unresolved
            and all(item["entailed"] is True for item in fact_results)
            and all(
                item["every_citation_relevant"] is True
                and item["atomic"] is True
                and item["useful"] is True
                and not item["hard_negative_ids"]
                and not item["payload_canary_ids"]
                and item["other_raw_payload_leak"] is False
                for item in claim_results
            )
            and all(value is True for value in summary_result.values())
        )
        packet_results.append(
            {
                "packet_id": packet_id,
                "blind_candidate_id": packet["blind_candidate_id"],
                "repeat": packet["repeat"],
                "facts": fact_results,
                "claims": claim_results,
                "summary": summary_result,
                "unresolved": packet_unresolved,
                "review_eligible": review_eligible,
            }
        )

    third_required = bool(disagreements)
    if third is not None and not third_required:
        raise EvaluationError("a third review was supplied without a gate disagreement")
    core: dict[str, Any] = {
        "schema": ADJUDICATION_V2_SCHEMA,
        "benchmark": freeze_v2["benchmark"],
        "freeze_sha256": freeze_v2["freeze_sha256"],
        "packet_set_sha256": packet_set["packet_set_sha256"],
        "initial_review_sha256": [
            first["artifact_sha256"],
            second["artifact_sha256"],
        ],
        "third_review_sha256": third["artifact_sha256"] if third else None,
        "third_required": third_required,
        "third_used": third is not None,
        "disagreements": sorted(set(disagreements)),
        "unresolved": sorted(set(unresolved)),
        "packets": packet_results,
        "all_packets_resolved": not unresolved,
        "aggregate_accuracy_computed": False,
    }
    core["adjudication_sha256"] = _sha256(_canonical_bytes(core))
    return core


def _render_origin_record(origin: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"source": origin.source, "ref": origin.ref}
    if origin.char_span is not None:
        result["char_span"] = list(origin.char_span)
    return result


def _canonical_human_drop_json(value: Any) -> str:
    """Mirror the renderer's safe, canonical ``drop-v1`` JSON encoding."""

    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).replace("`", r"\u0060").replace("\x7f", r"\u007f")
    except (TypeError, ValueError) as exc:
        raise EvaluationError(
            f"human drop record is not canonical JSON: {exc}"
        ) from exc


def _expected_drop_records(
    extraction: Any,
    *,
    dropped_unit_ids: Sequence[Any],
    dropped_relation_indexes: Sequence[Any],
    dropped_statement_ids: Sequence[Any],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    unit_by_id = {unit.id: unit for unit in extraction.units}
    statement_by_id = {statement.id: statement for statement in extraction.summary_claims}

    valid_unit_ids = [value for value in dropped_unit_ids if isinstance(value, str)]
    if len(valid_unit_ids) != len(dropped_unit_ids):
        errors.append("dropped unit IDs must be strings")
    dropped_units = set(valid_unit_ids)
    if len(dropped_units) != len(valid_unit_ids):
        errors.append("duplicate dropped unit IDs")
    if unknown := sorted(dropped_units - set(unit_by_id)):
        errors.append(f"unknown dropped unit IDs: {unknown}")

    valid_relation_indexes = [
        value
        for value in dropped_relation_indexes
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if len(valid_relation_indexes) != len(dropped_relation_indexes):
        errors.append("dropped relation indexes must be integers")
    dropped_relations = set(valid_relation_indexes)
    if len(dropped_relations) != len(valid_relation_indexes):
        errors.append("duplicate dropped relation indexes")
    if any(
        not 0 <= index < len(extraction.relations)
        for index in dropped_relations
    ):
        errors.append("invalid dropped relation index")

    valid_statement_ids = [
        value for value in dropped_statement_ids if isinstance(value, str)
    ]
    if len(valid_statement_ids) != len(dropped_statement_ids):
        errors.append("dropped statement IDs must be strings")
    dropped_statements = set(valid_statement_ids)
    if len(dropped_statements) != len(valid_statement_ids):
        errors.append("duplicate dropped statement IDs")
    if unknown := sorted(dropped_statements - set(statement_by_id)):
        errors.append(f"unknown dropped statement IDs: {unknown}")

    unit_records = [
        {
            "id": unit.id,
            "origin": _render_origin_record(unit.origin),
            "reason": "budget",
        }
        for unit in extraction.units
        if unit.id in dropped_units
    ]
    relation_records = [
        {
            "index": index,
            "src": relation.src,
            "dst": relation.dst,
            "kind": str(relation.kind),
            "reason": "budget",
        }
        for index, relation in enumerate(extraction.relations)
        if index in dropped_relations
    ]
    statement_records = [
        {
            "id": statement.id,
            "evidence_unit_ids": list(statement.evidence_unit_ids),
            "missing_evidence_unit_ids": [
                unit_id
                for unit_id in statement.evidence_unit_ids
                if unit_id in dropped_units
            ],
            "origins": [_render_origin_record(origin) for origin in statement.origins],
            "reason": "budget-evidence-omitted",
        }
        for statement in extraction.summary_claims
        if statement.id in dropped_statements
    ]
    return {
        "units": unit_records,
        "relations": relation_records,
        "statements": statement_records,
    }, errors


def _parse_human_render_audit(
    shape: str, text: str, extraction: Any, budget: int
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if shape == "ansi":
        audit_marker = "\nSelection\n"
        record_prefix = "- drop-v1/"
        usage = re.search(
            r"(?m)^(\d+)/(\d+) portable tokens \(utf8-byte-v1, complete-output\); "
            r"available (\d+)$",
            text[text.rfind(audit_marker) + 1 :] if audit_marker in text else "",
        )
        counts = re.search(
            r"(?m)^dropped (\d+) units and (\d+) relations and (\d+) summary claims$",
            text[text.rfind(audit_marker) + 1 :] if audit_marker in text else "",
        )
    else:
        audit_marker = "\n## Selection\n"
        record_prefix = "  - drop-v1/"
        usage = re.search(
            r"Portable tokens: \*\*(\d+) / (\d+)\*\* "
            r"\(`utf8-byte-v1`, `complete-output`; unlimited form (\d+)\)",
            text[text.rfind(audit_marker) + 1 :] if audit_marker in text else "",
        )
        counts = re.search(
            r"Dropped for budget: \*\*(\d+) units\*\*, \*\*(\d+) relations\*\*, "
            r"\*\*(\d+) summary claims\*\*",
            text[text.rfind(audit_marker) + 1 :] if audit_marker in text else "",
        )
    if usage is None or counts is None:
        return {}, ["human renderer lacks a complete parseable selection report"]

    audit_text = text[text.rfind(audit_marker) + 1 :]
    digest_pattern = (
        r"(?m)^drop-set sha256 ([0-9a-f]{64})$"
        if shape == "ansi"
        else r"(?m)^- Drop-set SHA-256: `([0-9a-f]{64})`$"
    )
    digest_match = re.search(digest_pattern, audit_text)
    records: dict[str, list[dict[str, Any]]] = {
        "unit": [],
        "relation": [],
        "statement": [],
    }
    for line_number, line in enumerate(audit_text.splitlines(), start=1):
        if not line.startswith(record_prefix):
            continue
        kind_and_payload = line.removeprefix(record_prefix)
        kind, separator, raw_payload = kind_and_payload.partition(" ")
        if not separator or kind not in records:
            errors.append(
                f"human renderer has invalid drop-v1 framing on audit line {line_number}"
            )
            continue
        try:
            payload_bytes = raw_payload.encode("utf-8", errors="strict")
            value = _strict_json_bytes(
                payload_bytes,
                label=f"human {kind} drop record on audit line {line_number}",
            )
        except (EvaluationError, UnicodeEncodeError) as exc:
            errors.append(str(exc))
            continue
        if not isinstance(value, dict):
            errors.append(
                f"human {kind} drop record on audit line {line_number} must be an object"
            )
            continue
        if raw_payload != _canonical_human_drop_json(value):
            errors.append(
                f"human {kind} drop record on audit line {line_number} is not "
                "canonical drop-v1 JSON"
            )
        records[kind].append(value)

    used, requested, available = map(int, usage.groups())
    if requested != budget:
        errors.append("human renderer reported the wrong requested budget")
    declared_unit_count, declared_relation_count, declared_statement_count = map(
        int, counts.groups()
    )
    unit_records = records["unit"]
    relation_records = records["relation"]
    statement_records = records["statement"]
    if len(unit_records) != declared_unit_count:
        errors.append("human renderer unit drop inventory count differs")
    if len(relation_records) != declared_relation_count:
        errors.append("human renderer relation drop inventory count differs")
    if len(statement_records) != declared_statement_count:
        errors.append("human renderer statement drop inventory count differs")

    unit_ids = [item.get("id") for item in unit_records]
    relation_indexes = [item.get("index") for item in relation_records]
    statement_ids = [item.get("id") for item in statement_records]
    expected, identity_errors = _expected_drop_records(
        extraction,
        dropped_unit_ids=unit_ids,
        dropped_relation_indexes=relation_indexes,
        dropped_statement_ids=statement_ids,
    )
    errors.extend(identity_errors)
    observed_records = {
        "units": unit_records,
        "relations": relation_records,
        "statements": statement_records,
    }
    if unit_records != expected["units"]:
        errors.append("human renderer unit drop records differ from extraction")
    if relation_records != expected["relations"]:
        errors.append("human renderer relation drop records differ from extraction")
    if statement_records != expected["statements"]:
        errors.append("human renderer statement drop records differ from extraction")

    observed_records_digest = _sha256(_canonical_bytes(observed_records))
    observed_digest = digest_match.group(1) if digest_match is not None else None
    if observed_digest is None and any(
        (declared_unit_count, declared_relation_count, declared_statement_count)
    ):
        errors.append("human renderer omitted the non-empty drop-set digest")
    elif observed_digest is not None and observed_digest != observed_records_digest:
        errors.append("human renderer drop-set digest differs from wire inventory")
    return {
        "used": used,
        "requested": requested,
        "available": available,
        "dropped_unit_ids": unit_ids,
        "dropped_relation_indexes": relation_indexes,
        "dropped_statement_ids": statement_ids,
        "drop_digest": observed_digest or observed_records_digest,
    }, errors


def _parse_machine_render_audit(
    shape: str, text: str, extraction: Any, budget: int
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    encoded = text.encode("utf-8", errors="strict")
    if shape == "json":
        payload = _strict_json_bytes(encoded, label="v2 JSON render")
        if not isinstance(payload, dict):
            return {}, ["JSON renderer did not return an object"]
        claims = payload.get("summary_claims")
        units = payload.get("units")
        manifest = payload.get("manifest")
        selection = manifest.get("selection") if isinstance(manifest, dict) else None
        relations = payload.get("relations")
    else:
        rows = [
            _strict_json_bytes(line.encode("utf-8"), label=f"v2 JSONL line {index}")
            for index, line in enumerate(text.splitlines(), start=1)
        ]
        if len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
            return {}, ["JSONL renderer did not return object records"]
        if rows[0].get("type") != "header" or rows[-1].get("type") != "manifest":
            errors.append("JSONL renderer framing is invalid")
        claims = rows[0].get("summary_claims")
        units = [row for row in rows[1:-1] if row.get("type") == "unit"]
        selection = rows[-1].get("selection")
        relations = rows[-1].get("relations")
    if not isinstance(claims, list) or not isinstance(units, list):
        return {}, [*errors, "machine renderer lacks claims or units"]
    if not isinstance(selection, dict) or not isinstance(relations, list):
        return {}, [*errors, "machine renderer lacks selection or relations"]
    if selection.get("counter") != "utf8-byte-v1" or selection.get("scope") != "complete-output":
        errors.append("machine renderer counter or scope changed")
    if selection.get("requested") != budget:
        errors.append("machine renderer reported the wrong requested budget")
    dropped = selection.get("dropped")
    if not isinstance(dropped, dict):
        return {}, [*errors, "machine renderer lacks a drop inventory"]
    unit_records = dropped.get("reported")
    relation_records = dropped.get("reported_relations")
    statement_records = dropped.get("reported_statements")
    if not all(isinstance(value, list) for value in (unit_records, relation_records, statement_records)):
        return {}, [*errors, "machine renderer drop inventory arrays are invalid"]
    if dropped.get("unlisted") != 0 or dropped.get("unlisted_relations") != 0 or dropped.get("unlisted_statements") != 0:
        errors.append("machine renderer has unlisted dropped identities")
    if dropped.get("unit_count") != len(unit_records):
        errors.append("machine renderer unit drop count differs")
    if dropped.get("relation_count") != len(relation_records):
        errors.append("machine renderer relation drop count differs")
    if dropped.get("statement_count") != len(statement_records):
        errors.append("machine renderer statement drop count differs")
    unit_ids = [item.get("id") for item in unit_records if isinstance(item, dict)]
    relation_indexes = [
        item.get("index") for item in relation_records if isinstance(item, dict)
    ]
    statement_ids = [
        item.get("id") for item in statement_records if isinstance(item, dict)
    ]
    expected, identity_errors = _expected_drop_records(
        extraction,
        dropped_unit_ids=unit_ids,
        dropped_relation_indexes=relation_indexes,
        dropped_statement_ids=statement_ids,
    )
    errors.extend(identity_errors)
    if unit_records != expected["units"]:
        errors.append("machine renderer unit drop records differ from extraction")
    if relation_records != expected["relations"]:
        errors.append("machine renderer relation drop records differ from extraction")
    if statement_records != expected["statements"]:
        errors.append("machine renderer statement drop records differ from extraction")
    expected_digest = _sha256(_canonical_bytes(expected))
    if dropped.get("digest") != expected_digest:
        errors.append("machine renderer drop-set digest differs")
    selected_unit_ids = [
        item.get("id") for item in units if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    retained_claim_ids = [
        item.get("id")
        for item in claims
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    selected_set = set(selected_unit_ids)
    for relation in relations:
        if not isinstance(relation, dict) or not {
            relation.get("src"),
            relation.get("dst"),
        }.issubset(selected_set):
            errors.append("machine renderer retained a dangling relation")
            break
    return {
        "used": selection.get("used"),
        "requested": selection.get("requested"),
        "available": selection.get("available"),
        "dropped_unit_ids": unit_ids,
        "dropped_relation_indexes": relation_indexes,
        "dropped_statement_ids": statement_ids,
        "drop_digest": dropped.get("digest"),
        "selected_unit_ids": selected_unit_ids,
        "retained_claim_ids": retained_claim_ids,
    }, errors


def _render_wire_components(
    shape: str,
    text: str,
    *,
    retained_claim_count: int,
) -> dict[str, int]:
    """Measure disjoint wire components whose byte counts sum to the output.

    ``fixed_envelope_bytes`` is format framing plus identity fields only;
    ``semantic_claim_bytes`` is the incremental grounded-claim serialization;
    ``semantic_evidence_bytes`` is summaries, units, relations, and gaps; and
    ``audit_record_bytes`` is selection/accounting plus extraction/run
    manifests.  The decomposition is diagnostic benchmark evidence, not a
    product output-budget target.
    """

    if retained_claim_count < 0:
        raise EvaluationError("render wire claim count cannot be negative")
    encoded = text.encode("utf-8", errors="strict")

    def rendered_json(value: Any, *, indent: int | None = None) -> bytes:
        return (
            json.dumps(
                value,
                indent=indent,
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        ).encode("utf-8", errors="strict")

    if shape == "json":
        payload = _strict_json_bytes(encoded, label="v2 JSON wire measurement")
        item = _require_exact_keys(
            payload,
            {
                "schema",
                "subject",
                "kind",
                "summary",
                "summary_claims",
                "tokens",
                "units",
                "relations",
                "gaps",
                "manifest",
            },
            label="v2 JSON wire payload",
        )
        if rendered_json(item, indent=2) != encoded:
            raise EvaluationError("JSON renderer wire cannot be reproduced exactly")
        baseline = dict(item)
        baseline.update(
            {
                "summary": "",
                "summary_claims": [],
                "tokens": 0,
                "units": [],
                "relations": [],
                "gaps": [],
                "manifest": {},
            }
        )
        fixed = len(rendered_json(baseline, indent=2))
        claim_variant = dict(baseline)
        claim_variant["summary_claims"] = item["summary_claims"]
        claims = len(rendered_json(claim_variant, indent=2)) - fixed
        evidence_variant = dict(baseline)
        for field in ("summary", "tokens", "units", "relations", "gaps"):
            evidence_variant[field] = item[field]
        evidence = len(rendered_json(evidence_variant, indent=2)) - fixed
        audit_variant = dict(baseline)
        audit_variant["manifest"] = item["manifest"]
        audit = len(rendered_json(audit_variant, indent=2)) - fixed
    elif shape == "jsonl":
        rows = [
            _strict_json_bytes(line.encode("utf-8"), label=f"v2 JSONL wire row {index}")
            for index, line in enumerate(text.splitlines(), start=1)
        ]
        if len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
            raise EvaluationError("JSONL wire measurement lacks object framing")

        def line_bytes(value: Mapping[str, Any]) -> bytes:
            return json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8", errors="strict") + b"\n"

        if b"".join(line_bytes(row) for row in rows) != encoded:
            raise EvaluationError("JSONL renderer wire cannot be reproduced exactly")
        header = rows[0]
        footer = rows[-1]
        header_base = dict(header)
        header_base.update({"summary": "", "summary_claims": [], "units": 0})
        footer_base = dict(footer)
        footer_base.update(
            {"selection": {}, "relations": [], "gaps": [], "manifest": {}}
        )
        fixed = len(line_bytes(header_base)) + len(line_bytes(footer_base))
        header_claims = dict(header_base)
        header_claims["summary_claims"] = header["summary_claims"]
        claims = len(line_bytes(header_claims)) - len(line_bytes(header_base))
        header_evidence = dict(header_base)
        header_evidence.update(
            {"summary": header["summary"], "units": header["units"]}
        )
        footer_evidence = dict(footer_base)
        footer_evidence.update(
            {"relations": footer["relations"], "gaps": footer["gaps"]}
        )
        evidence = (
            len(line_bytes(header_evidence))
            - len(line_bytes(header_base))
            + sum(len(line_bytes(row)) for row in rows[1:-1])
            + len(line_bytes(footer_evidence))
            - len(line_bytes(footer_base))
        )
        footer_audit = dict(footer_base)
        footer_audit.update(
            {"selection": footer["selection"], "manifest": footer["manifest"]}
        )
        audit = len(line_bytes(footer_audit)) - len(line_bytes(footer_base))
    else:
        lines = text.splitlines(keepends=True)
        if not lines:
            raise EvaluationError("human renderer wire measurement is empty")
        if shape == "ansi":
            audit_marker = "Selection\n"
            fixed_line_count = 1
            claim_line_count = retained_claim_count
        elif shape == "md":
            audit_marker = "## Selection\n"
            fixed_line_count = 2
            claim_line_count = 2 * retained_claim_count
        else:
            raise EvaluationError(f"unknown render wire shape {shape!r}")
        try:
            audit_index = lines.index(audit_marker)
        except ValueError as exc:
            raise EvaluationError(
                f"{shape} renderer wire lacks its selection audit marker"
            ) from exc
        claim_start = fixed_line_count
        claim_end = claim_start + claim_line_count
        if claim_end > audit_index:
            raise EvaluationError(f"{shape} renderer claim segment overlaps audit")
        fixed = len("".join(lines[:fixed_line_count]).encode("utf-8"))
        claims = len("".join(lines[claim_start:claim_end]).encode("utf-8"))
        audit = len("".join(lines[audit_index:]).encode("utf-8"))
        evidence = len(encoded) - fixed - claims - audit

    components = {
        "total_bytes": len(encoded),
        "fixed_envelope_bytes": fixed,
        "semantic_claim_bytes": claims,
        "semantic_evidence_bytes": evidence,
        "audit_record_bytes": audit,
    }
    if any(value < 0 for value in components.values()) or sum(
        components[field]
        for field in (
            "fixed_envelope_bytes",
            "semantic_claim_bytes",
            "semantic_evidence_bytes",
            "audit_record_bytes",
        )
    ) != components["total_bytes"]:
        raise EvaluationError("render wire component accounting is not exact")
    return components


def audit_render_matrix(
    extraction: Any,
    *,
    policy_v2: Mapping[str, Any],
    fact_claim_ids: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Audit all four production renderers at three preregistered ceilings."""

    from autotldr.render import BudgetTooSmall, render

    statement_by_id = {statement.id: statement for statement in extraction.summary_claims}
    if len(statement_by_id) != len(extraction.summary_claims):
        raise EvaluationError("render audit extraction has duplicate statement IDs")
    expected_facts = set(policy_v2["eligibility"]["required_fact_ids_each_repeat"])
    if set(fact_claim_ids) != expected_facts:
        raise EvaluationError("render audit fact/claim map differs from the frozen facts")
    for fact_id, claim_ids in fact_claim_ids.items():
        if (
            not isinstance(claim_ids, Sequence)
            or isinstance(claim_ids, (str, bytes))
            or len(claim_ids) != len(set(claim_ids))
            or not set(claim_ids).issubset(statement_by_id)
        ):
            raise EvaluationError(f"render audit fact {fact_id!r} has invalid claim IDs")

    cells: list[dict[str, Any]] = []
    for shape in ("ansi", "md", "json", "jsonl"):
        for level in ("minimum", "compact", "complete"):
            budget = policy_v2["render_budgets"][shape][level]
            errors: list[str] = []
            try:
                text = render(
                    extraction,
                    output=shape,
                    budget=budget,
                    cite=True,
                    color=False,
                )
            except BudgetTooSmall as exc:
                cells.append(
                    {
                        "shape": shape,
                        "level": level,
                        "budget": budget,
                        "output_sha256": None,
                        "used_bytes": 0,
                        "retained_claim_ids": [],
                        "retained_fact_ids": [],
                        "required_fact_ids": policy_v2["render_semantic_requirements"][level],
                        "wire_components": None,
                        "errors": [f"budget-too-small:required={exc.required}"],
                        "passed": False,
                    }
                )
                continue
            encoded = text.encode("utf-8", errors="strict")
            if not text.endswith("\n"):
                errors.append("renderer output lacks the final newline")
            if len(encoded) > budget:
                errors.append("renderer exceeded the exact byte ceiling")
            if shape in {"json", "jsonl"}:
                parsed, parse_errors = _parse_machine_render_audit(
                    shape, text, extraction, budget
                )
            else:
                parsed, parse_errors = _parse_human_render_audit(
                    shape, text, extraction, budget
                )
            errors.extend(parse_errors)
            used = parsed.get("used")
            if used != len(encoded):
                errors.append("renderer reported used bytes different from exact UTF-8 bytes")
            dropped_units = set(parsed.get("dropped_unit_ids", []))
            dropped_statements = set(parsed.get("dropped_statement_ids", []))
            if shape in {"ansi", "md"}:
                retained_claim_ids = [
                    statement.id
                    for statement in extraction.summary_claims
                    if statement.id not in dropped_statements
                ]
                selected_ids = {
                    unit.id for unit in extraction.units if unit.id not in dropped_units
                }
                dropped_relation_indexes = set(
                    parsed.get("dropped_relation_indexes", [])
                )
                for index, relation in enumerate(extraction.relations):
                    if index not in dropped_relation_indexes and not {
                        relation.src,
                        relation.dst,
                    }.issubset(selected_ids):
                        errors.append("human renderer retained a dangling relation")
                        break
            else:
                retained_claim_ids = parsed.get("retained_claim_ids", [])
                selected_ids = set(parsed.get("selected_unit_ids", []))
            for claim_id in retained_claim_ids:
                statement = statement_by_id.get(claim_id)
                if statement is None:
                    errors.append("renderer retained an unknown summary claim")
                    continue
                if not set(statement.evidence_unit_ids).issubset(selected_ids):
                    errors.append(f"renderer retained claim {claim_id} without all evidence")
            retained_claim_set = set(retained_claim_ids)
            retained_facts = sorted(
                fact_id
                for fact_id, claim_ids in fact_claim_ids.items()
                if retained_claim_set.intersection(claim_ids)
            )
            required_facts = policy_v2["render_semantic_requirements"][level]
            missing_required = sorted(set(required_facts) - set(retained_facts))
            if missing_required:
                errors.append(
                    "required semantic facts were dropped: " + ", ".join(missing_required)
                )
            try:
                wire_components = _render_wire_components(
                    shape,
                    text,
                    retained_claim_count=len(retained_claim_ids),
                )
            except EvaluationError as exc:
                wire_components = None
                errors.append(str(exc))
            cells.append(
                {
                    "shape": shape,
                    "level": level,
                    "budget": budget,
                    "output_sha256": _sha256(encoded),
                    "used_bytes": len(encoded),
                    "retained_claim_ids": retained_claim_ids,
                    "retained_fact_ids": retained_facts,
                    "required_fact_ids": list(required_facts),
                    "wire_components": wire_components,
                    "errors": errors,
                    "passed": not errors,
                }
            )
    core: dict[str, Any] = {
        "schema": RENDER_AUDIT_V2_SCHEMA,
        "counter": "utf8-byte-v1",
        "scope": "complete-output",
        "cells": cells,
        "all_cells_passed": all(cell["passed"] for cell in cells),
    }
    core["audit_sha256"] = _sha256(_canonical_bytes(core))
    return core


def _validate_adjudication_v2(
    adjudication: Mapping[str, Any], *, freeze_v2: Mapping[str, Any]
) -> None:
    _require_exact_keys(
        adjudication,
        {
            "schema",
            "benchmark",
            "freeze_sha256",
            "packet_set_sha256",
            "initial_review_sha256",
            "third_review_sha256",
            "third_required",
            "third_used",
            "disagreements",
            "unresolved",
            "packets",
            "all_packets_resolved",
            "aggregate_accuracy_computed",
            "adjudication_sha256",
        },
        label="synthesis v2 adjudication",
    )
    core = dict(adjudication)
    digest = core.pop("adjudication_sha256", None)
    if not isinstance(digest, str) or digest != _sha256(_canonical_bytes(core)):
        raise EvaluationError("synthesis v2 adjudication self-hash is invalid")
    if (
        adjudication["schema"] != ADJUDICATION_V2_SCHEMA
        or adjudication["benchmark"] != freeze_v2["benchmark"]
        or adjudication["freeze_sha256"] != freeze_v2["freeze_sha256"]
        or adjudication["aggregate_accuracy_computed"] is not False
    ):
        raise EvaluationError("synthesis v2 adjudication identity is invalid")
    if adjudication["all_packets_resolved"] is not (not adjudication["unresolved"]):
        raise EvaluationError("synthesis v2 adjudication resolution flag is inconsistent")


def _repeat_extraction_v2(
    extraction: Any, repeat: Mapping[str, Any]
) -> Any:
    from autotldr.unit import Extraction, GroundedStatement

    unit_by_id = {unit.id: unit for unit in extraction.units}
    statements: list[Any] = []
    for claim in repeat["claims"]:
        origins: list[Any] = []
        for unit_id in claim["evidence_unit_ids"]:
            origin = unit_by_id[unit_id].origin
            if origin not in origins:
                origins.append(origin)
        statement = GroundedStatement(
            claim["content"], tuple(origins), tuple(claim["evidence_unit_ids"])
        )
        if statement.id != claim["id"]:
            raise EvaluationError("v2 repeat claim ID differs when reconstructed")
        statements.append(statement)
    meta = dict(extraction.meta)
    existing_models = meta.get("models")
    models = list(existing_models) if isinstance(existing_models, list) else []
    models.append(dict(repeat["model_manifest"]))
    meta["models"] = models
    return Extraction(
        source=extraction.source,
        kind=extraction.kind,
        units=list(extraction.units),
        relations=list(extraction.relations),
        gaps=list(extraction.gaps),
        meta=meta,
        summary_claims=statements,
    )


def score_candidate_v2(
    artifact: Mapping[str, Any],
    *,
    freeze_v2: Mapping[str, Any],
    extraction: Any,
    base_policy: Mapping[str, Any],
    policy_v2: Mapping[str, Any],
    truth: Mapping[str, Any],
    adjudication: Mapping[str, Any],
    candidate_order: int,
) -> dict[str, Any]:
    """Apply semantic human gates per fact; v1 regexes are never a verdict."""

    _validate_v2_freeze_digest(freeze_v2)
    _require_base_policy_binding(base_policy, freeze_v2)
    _require_extraction_binding(extraction, freeze_v2)
    _require_canonical_sidecar(
        policy_v2, freeze_v2["policy_v2"], label="v2 policy"
    )
    _require_canonical_sidecar(truth, freeze_v2["truth_v2"], label="truth ledger")
    _validate_adjudication_v2(adjudication, freeze_v2=freeze_v2)
    failure_validation = validate_failure_injection_results(
        freeze_v2["failure_injections"],
        policy_v2=policy_v2,
        preoutput_binding_sha256=freeze_v2["preoutput_binding_sha256"],
    )
    mechanical = score_candidate(
        artifact,
        freeze=freeze_v2["base_freeze"],
        extraction=extraction,
        policy=base_policy,
        candidate_order=candidate_order,
    )
    candidate = policy_candidates(base_policy)[candidate_order]
    if artifact["candidate"] != candidate:
        raise EvaluationError("v2 candidate order differs from the artifact")
    blind_by_candidate = _blind_record_by_candidate(freeze_v2)
    alias = blind_by_candidate[candidate["name"]]["blind_alias"]
    adjudicated_packets = {
        (item["blind_candidate_id"], item["repeat"]): item
        for item in adjudication["packets"]
    }
    expected_repeats = base_policy["generation"]["repeats"]
    repeat_scores: list[dict[str, Any]] = []
    failed_gates: list[str] = []
    fact_ids = policy_v2["eligibility"]["required_fact_ids_each_repeat"]
    canary_by_id = {item["id"]: item for item in truth["payload_canaries"]}

    for index, (repeat, mechanical_repeat) in enumerate(
        zip(artifact["repeats"], mechanical["repeats"], strict=True), start=1
    ):
        packet = adjudicated_packets.get((alias, index))
        if packet is None:
            raise EvaluationError("v2 adjudication omits a candidate repeat")
        claim_ids = [claim["id"] for claim in repeat["claims"]]
        if [item["id"] for item in packet["claims"]] != claim_ids:
            raise EvaluationError("v2 adjudication claim IDs differ from candidate artifact")
        fact_by_id = {item["id"]: item for item in packet["facts"]}
        if set(fact_by_id) != set(fact_ids):
            raise EvaluationError("v2 adjudication fact IDs differ from policy")
        fact_claim_ids = {
            fact_id: list(fact_by_id[fact_id]["supporting_claim_ids"])
            for fact_id in fact_ids
        }
        automated_canaries = sorted(
            {
                canary_id
                for claim in repeat["claims"]
                for canary_id, canary in canary_by_id.items()
                if canary["literal"] in claim["content"]
            }
        )
        reviewed_canaries = sorted(
            {
                canary_id
                for claim in packet["claims"]
                for canary_id in claim["payload_canary_ids"]
            }
        )
        if automated_canaries != reviewed_canaries:
            raise EvaluationError("v2 adjudication payload canaries differ from exact scan")
        hard_negative_ids = sorted(
            {
                negative_id
                for claim in packet["claims"]
                for negative_id in claim["hard_negative_ids"]
            }
        )
        other_raw_payload_leak = any(
            claim["other_raw_payload_leak"] is not False for claim in packet["claims"]
        )
        cited_sources = sorted(
            {
                unit.source
                for claim in repeat["claims"]
                for unit_id in claim["evidence_unit_ids"]
                for unit in extraction.units
                if unit.id == unit_id
            }
        )
        tier3_sources = [source for source in cited_sources if _source_is_tier3(source)]
        synthesized_extraction = _repeat_extraction_v2(extraction, repeat)
        render_audit = audit_render_matrix(
            synthesized_extraction,
            policy_v2=policy_v2,
            fact_claim_ids=fact_claim_ids,
        )
        render_scope = policy_v2["render_budget_scope"]
        render_budget_gate = (
            render_audit["all_cells_passed"]
            if render_scope["product_acceptance_target"] is True
            else True
        )
        mechanical_gates = {
            key: mechanical_repeat["hard_gates"][key]
            for key in ("runtime_ok", "schema_and_provenance_valid", "response_limits_ok")
        }
        fact_gates = {
            fact_id: fact_by_id[fact_id]["entailed"] is True
            and bool(fact_claim_ids[fact_id])
            for fact_id in fact_ids
        }
        claim_review_ok = bool(packet["claims"]) and all(
            claim["every_citation_relevant"] is True
            and claim["atomic"] is True
            and claim["useful"] is True
            and not claim["hard_negative_ids"]
            and not claim["payload_canary_ids"]
            and claim["other_raw_payload_leak"] is False
            for claim in packet["claims"]
        )
        summary_review_ok = all(
            packet["summary"].get(gate) is True
            for gate in policy_v2["eligibility"]["required_summary_gates"]
        )
        gates: dict[str, bool] = {
            **mechanical_gates,
            **{f"fact:{key}": value for key, value in fact_gates.items()},
            "review_resolved": not packet["unresolved"],
            "claim_entailment_and_relevance": claim_review_ok,
            "summary_useful_and_coherent": summary_review_ok,
            "no_hard_negative": not hard_negative_ids,
            "no_payload_canary": not automated_canaries,
            "no_other_raw_payload_leak": not other_raw_payload_leak,
            "minimum_relevant_sources": len(cited_sources)
            >= policy_v2["eligibility"]["minimum_relevant_sources_each_repeat"],
            "minimum_relevant_tier3_sources": len(tier3_sources)
            >= policy_v2["eligibility"]["minimum_relevant_tier3_sources_each_repeat"],
            # The hash-frozen v2 policy explicitly classifies this matrix as a
            # benchmark audit rather than a product-acceptance target.  A later
            # renderer correctness fix made four old human ceilings
            # infeasible because their now-complete omission inventories do
            # not fit.  Preserve and report those failed cells, but do not turn
            # an audit-only, pre-product budget guess into a synthesis-quality
            # verdict.  A future policy that sets product_acceptance_target
            # true makes every cell a hard gate again.
            "render_budget_policy": render_budget_gate,
        }
        failed = [name for name, passed in gates.items() if not passed]
        failed_gates.extend(f"repeat-{index}:{name}" for name in failed)
        repeat_scores.append(
            {
                "repeat": index,
                "facts": [
                    {
                        "id": fact_id,
                        "entailed": fact_gates[fact_id],
                        "supporting_claim_ids": fact_claim_ids[fact_id],
                    }
                    for fact_id in fact_ids
                ],
                "cited_sources": cited_sources,
                "cited_tier3_sources": tier3_sources,
                "hard_negative_ids": hard_negative_ids,
                "payload_canary_ids": automated_canaries,
                "other_raw_payload_leak": other_raw_payload_leak,
                "render_audit": render_audit,
                "hard_gates": gates,
                "failed_hard_gates": failed,
            }
        )
    if len(repeat_scores) != expected_repeats:
        raise EvaluationError("v2 candidate has the wrong repeat count")
    if "candidate:complete-latency-records" in mechanical["failed_hard_gates"]:
        failed_gates.append("candidate:complete-latency-records")
    per_fact: list[dict[str, Any]] = []
    for fact_id in fact_ids:
        repeat_entailed = [
            next(item for item in repeat["facts"] if item["id"] == fact_id)[
                "entailed"
            ]
            for repeat in repeat_scores
        ]
        if len(repeat_entailed) != expected_repeats:
            raise EvaluationError("v2 per-fact denominator differs from frozen repeats")
        per_fact.append(
            {
                "id": fact_id,
                "repeat_entailed": repeat_entailed,
                "entailed_repeat_count": sum(repeat_entailed),
                "evaluated_repeat_count": expected_repeats,
                "entailed_all_repeats": all(repeat_entailed),
            }
        )
    return {
        "candidate": candidate,
        "blind_candidate_id": alias,
        "eligible": not failed_gates,
        "failed_hard_gates": failed_gates,
        "per_fact": per_fact,
        "repeats": repeat_scores,
        "failure_injection_validation": failure_validation,
        "aggregate_accuracy_computed": False,
        "lexical_prefilter_used_as_semantic_verdict": False,
    }


def score_candidates_v2(
    artifacts: Sequence[Mapping[str, Any]],
    *,
    freeze_v2: Mapping[str, Any],
    extraction: Any,
    base_policy: Mapping[str, Any],
    policy_v2: Mapping[str, Any],
    truth: Mapping[str, Any],
    adjudication: Mapping[str, Any],
) -> dict[str, Any]:
    """Report every candidate/fact without collapsing semantic accuracy."""

    artifact_by_name = {
        artifact.get("candidate", {}).get("name"): artifact for artifact in artifacts
    }
    candidates = policy_candidates(base_policy)
    if set(artifact_by_name) != {item["name"] for item in candidates}:
        raise EvaluationError("v2 scoring needs the exact frozen candidate set")
    scores = [
        score_candidate_v2(
            artifact_by_name[candidate["name"]],
            freeze_v2=freeze_v2,
            extraction=extraction,
            base_policy=base_policy,
            policy_v2=policy_v2,
            truth=truth,
            adjudication=adjudication,
            candidate_order=order,
        )
        for order, candidate in enumerate(candidates)
    ]
    eligible = [score["candidate"] for score in scores if score["eligible"]]
    report: dict[str, Any] = {
        "schema": REPORT_V2_SCHEMA,
        "benchmark": freeze_v2["benchmark"],
        "freeze_sha256": freeze_v2["freeze_sha256"],
        "adjudication_sha256": adjudication["adjudication_sha256"],
        "candidates": scores,
        "eligible_candidates": eligible,
        "eligible_candidate_count": len(eligible),
        "aggregate_accuracy_computed": False,
        "selected_candidate": eligible[0] if len(eligible) == 1 else None,
        "selection_note": (
            "exactly one candidate cleared every per-fact hard gate"
            if len(eligible) == 1
            else "no winner is inferred when zero or multiple candidates clear every gate"
        ),
    }
    report["report_sha256"] = _sha256(_canonical_bytes(report))
    return report


def _read_candidate_artifacts_v2(
    output_dir: Path, base_policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for candidate in policy_candidates(base_policy):
        artifact, _ = _read_object(
            output_dir / f"{candidate['slug']}.json",
            label=f"candidate artifact {candidate['name']}",
        )
        artifacts.append(artifact)
    return artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze", help="create the write-once pre-output freeze")
    freeze.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_PATH)
    freeze.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="candidate audit directory that must still be empty",
    )

    verify = subparsers.add_parser("verify", help="rebuild and verify the freeze")
    verify.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_PATH)

    run = subparsers.add_parser("run-model", help="run two audited repeats for one loaded model")
    run.add_argument("--model", required=True, help="exact AutoTLDR lifecycle identifier")
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_PATH)

    score = subparsers.add_parser("score", help="score all four candidate artifacts")
    score.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    score.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_PATH)
    score.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)

    freeze_v2 = subparsers.add_parser(
        "freeze-v2",
        help="bind truth, review, failure, and render policy before model output",
    )
    freeze_v2.add_argument("--base-freeze", type=Path, default=DEFAULT_FREEZE_PATH)
    freeze_v2.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_V2_PATH)
    freeze_v2.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)

    verify_v2 = subparsers.add_parser(
        "verify-v2", help="rebuild and verify every synthesis v2 binding"
    )
    verify_v2.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_V2_PATH)

    packets_v2 = subparsers.add_parser(
        "make-review-packets-v2", help="create blinded human review packets"
    )
    packets_v2.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_V2_PATH)
    packets_v2.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    packets_v2.add_argument(
        "--packets", type=Path, default=DEFAULT_REVIEW_PACKET_PATH
    )

    adjudicate_v2 = subparsers.add_parser(
        "adjudicate-v2", help="combine two independent reviews and an optional third"
    )
    adjudicate_v2.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_V2_PATH)
    adjudicate_v2.add_argument(
        "--packets", type=Path, default=DEFAULT_REVIEW_PACKET_PATH
    )
    adjudicate_v2.add_argument("--review-a", type=Path, required=True)
    adjudicate_v2.add_argument("--review-b", type=Path, required=True)
    adjudicate_v2.add_argument("--review-third", type=Path)
    adjudicate_v2.add_argument(
        "--output", type=Path, default=DEFAULT_ADJUDICATION_PATH
    )

    score_v2 = subparsers.add_parser(
        "score-v2", help="apply per-fact human and renderer hard gates"
    )
    score_v2.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE_V2_PATH)
    score_v2.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    score_v2.add_argument(
        "--adjudication", type=Path, default=DEFAULT_ADJUDICATION_PATH
    )
    score_v2.add_argument("--report", type=Path, default=DEFAULT_REPORT_V2_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "freeze":
            require_no_model_outputs(args.output_dir)
            record = build_freeze_record()
            write_freeze(args.freeze, record)
            print(record["freeze_sha256"])
        elif args.command == "verify":
            record, _ = verify_freeze_record(read_freeze(args.freeze))
            print(record["freeze_sha256"])
        elif args.command == "run-model":
            artifact = run_candidate(args.model, args.output, freeze_path=args.freeze)
            print(artifact["artifact_sha256"])
        elif args.command == "score":
            report = score_directory(args.output_dir, freeze_path=args.freeze)
            _atomic_write_new(
                args.report,
                json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8") + b"\n",
            )
            print(json.dumps(
                {
                    "report_sha256": report["report_sha256"],
                    "selected_candidate": report["selected_candidate"],
                    "eligible_candidate_count": report["eligible_candidate_count"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif args.command == "freeze-v2":
            require_no_model_outputs(args.output_dir)
            base_freeze = read_freeze(args.base_freeze)
            record = build_freeze_record_v2()
            if _canonical_bytes(base_freeze) != _canonical_bytes(record["base_freeze"]):
                raise EvaluationError(
                    "the existing v1 freeze differs from the base embedded by v2"
                )
            write_freeze(args.freeze, record)
            print(record["freeze_sha256"])
        elif args.command == "verify-v2":
            record, _ = verify_freeze_record_v2(read_freeze_v2(args.freeze))
            print(record["freeze_sha256"])
        elif args.command == "make-review-packets-v2":
            freeze_v2_record, extraction = verify_freeze_record_v2(
                read_freeze_v2(args.freeze)
            )
            base_policy, _ = load_policy()
            truth, _ = load_truth_v2()
            artifacts = _read_candidate_artifacts_v2(args.output_dir, base_policy)
            packets = build_review_packets_v2(
                artifacts,
                freeze_v2=freeze_v2_record,
                extraction=extraction,
                base_policy=base_policy,
                truth=truth,
            )
            _atomic_write_new(
                args.packets,
                json.dumps(packets, indent=2, ensure_ascii=False).encode("utf-8") + b"\n",
            )
            print(packets["packet_set_sha256"])
        elif args.command == "adjudicate-v2":
            freeze_v2_record, _ = verify_freeze_record_v2(
                read_freeze_v2(args.freeze)
            )
            packet_set, _ = _read_object(args.packets, label="v2 review packet set")
            review_a, _ = _read_object(args.review_a, label="v2 first review")
            review_b, _ = _read_object(args.review_b, label="v2 second review")
            review_third = (
                _read_object(args.review_third, label="v2 third review")[0]
                if args.review_third is not None
                else None
            )
            policy_v2, _ = load_policy_v2()
            truth, _ = load_truth_v2()
            review_schema, _ = load_review_schema_v2()
            result = adjudicate_reviews_v2(
                review_a,
                review_b,
                third_review=review_third,
                packet_set=packet_set,
                freeze_v2=freeze_v2_record,
                truth=truth,
                review_schema=review_schema,
                policy_v2=policy_v2,
            )
            _atomic_write_new(
                args.output,
                json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8") + b"\n",
            )
            print(result["adjudication_sha256"])
        elif args.command == "score-v2":
            freeze_v2_record, extraction = verify_freeze_record_v2(
                read_freeze_v2(args.freeze)
            )
            base_policy, _ = load_policy()
            policy_v2, _ = load_policy_v2()
            truth, _ = load_truth_v2()
            artifacts = _read_candidate_artifacts_v2(args.output_dir, base_policy)
            adjudication, _ = _read_object(
                args.adjudication, label="v2 adjudication"
            )
            report = score_candidates_v2(
                artifacts,
                freeze_v2=freeze_v2_record,
                extraction=extraction,
                base_policy=base_policy,
                policy_v2=policy_v2,
                truth=truth,
                adjudication=adjudication,
            )
            _atomic_write_new(
                args.report,
                json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8") + b"\n",
            )
            print(
                json.dumps(
                    {
                        "report_sha256": report["report_sha256"],
                        "selected_candidate": report["selected_candidate"],
                        "eligible_candidate_count": report["eligible_candidate_count"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:  # pragma: no cover - argparse owns the command vocabulary
            raise AssertionError(args.command)
    except EvaluationError as exc:
        print(f"Stage 5 synthesis evaluation error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
