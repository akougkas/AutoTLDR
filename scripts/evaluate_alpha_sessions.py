#!/usr/bin/env python3
"""Validate five privacy-safe first-user records and evaluate the alpha gate."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "autotldr-first-user-session-v1"
REPORT_SCHEMA = "autotldr-first-user-gate-v1"
COHORT = Counter({"developer": 2, "research-data": 2, "spreadsheet": 1})
ACCEPTABLE_OUTCOMES = {"valid-cited-tldr", "actionable-decline"}
OUTCOMES = ACCEPTABLE_OUTCOMES | {
    "empty-success",
    "silent-fallback",
    "unactionable-failure",
    "no-result",
}
CATEGORIES = {
    "none",
    "onboarding",
    "extraction",
    "selection",
    "synthesis",
    "presentation",
    "authority",
    "performance",
    "missing-use-case",
}
SESSION_KEYS = {
    "schema",
    "participant_id",
    "cohort",
    "artifact_kind",
    "source_content_recorded",
    "doctor_green",
    "intervention_count",
    "install_to_green_doctor_seconds",
    "doctor_to_useful_outcome_seconds",
    "outcome",
    "claim_judgments",
    "citation_samples",
    "understands_gaps",
    "understands_detail",
    "authority_surprise",
    "would_reuse",
    "first_missing_capability",
}
JUDGMENT_KEYS = {"useful", "incorrect", "unsupported", "redundant"}


class SessionDataError(ValueError):
    """One session record is not safe or complete enough to evaluate."""


def _plain_string(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SessionDataError(f"{label} must be a non-empty unpadded string")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise SessionDataError(f"{label} is too long or contains a control character")
    return value


def _boolean(value: Any, *, label: str) -> bool:
    if type(value) is not bool:
        raise SessionDataError(f"{label} must be true or false")
    return value


def _nonnegative_int(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise SessionDataError(f"{label} must be a non-negative integer")
    return value


def _positive_seconds(value: Any, *, label: str, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if type(value) not in {int, float} or value <= 0 or value > 86_400:
        raise SessionDataError(f"{label} must be a positive number of seconds")
    return float(value)


def validate_session(raw: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SessionDataError(f"{source}: session must be a JSON object")
    if set(raw) != SESSION_KEYS:
        missing = sorted(SESSION_KEYS - set(raw))
        unknown = sorted(set(raw) - SESSION_KEYS)
        raise SessionDataError(
            f"{source}: session fields differ; missing={missing}, unknown={unknown}"
        )
    if raw["schema"] != SCHEMA:
        raise SessionDataError(f"{source}: unsupported schema {raw['schema']!r}")
    participant_id = _plain_string(
        raw["participant_id"], label=f"{source}.participant_id", maximum=64
    )
    cohort = raw["cohort"]
    if cohort not in COHORT:
        raise SessionDataError(f"{source}.cohort must be one of {sorted(COHORT)}")
    artifact_kind = _plain_string(
        raw["artifact_kind"], label=f"{source}.artifact_kind", maximum=80
    )
    if _boolean(
        raw["source_content_recorded"],
        label=f"{source}.source_content_recorded",
    ):
        raise SessionDataError(
            f"{source}: source_content_recorded must remain false; store no source "
            "path, excerpt, or artifact in the session record"
        )
    doctor_green = _boolean(raw["doctor_green"], label=f"{source}.doctor_green")
    intervention_count = _nonnegative_int(
        raw["intervention_count"], label=f"{source}.intervention_count"
    )
    install_seconds = _positive_seconds(
        raw["install_to_green_doctor_seconds"],
        label=f"{source}.install_to_green_doctor_seconds",
        nullable=not doctor_green,
    )
    useful_seconds = _positive_seconds(
        raw["doctor_to_useful_outcome_seconds"],
        label=f"{source}.doctor_to_useful_outcome_seconds",
        nullable=True,
    )
    outcome = raw["outcome"]
    if outcome not in OUTCOMES:
        raise SessionDataError(f"{source}.outcome must be one of {sorted(OUTCOMES)}")

    judgments = raw["claim_judgments"]
    if not isinstance(judgments, dict) or set(judgments) != JUDGMENT_KEYS:
        raise SessionDataError(
            f"{source}.claim_judgments must contain exactly {sorted(JUDGMENT_KEYS)}"
        )
    normalized_judgments = {
        key: _nonnegative_int(
            judgments[key], label=f"{source}.claim_judgments.{key}"
        )
        for key in sorted(JUDGMENT_KEYS)
    }

    samples = raw["citation_samples"]
    if not isinstance(samples, list):
        raise SessionDataError(f"{source}.citation_samples must be a list")
    normalized_samples = []
    for index, sample in enumerate(samples):
        label = f"{source}.citation_samples[{index}]"
        if not isinstance(sample, dict) or set(sample) != {"resolves", "entails"}:
            raise SessionDataError(f"{label} must contain only resolves and entails")
        normalized_samples.append(
            {
                "resolves": _boolean(sample["resolves"], label=f"{label}.resolves"),
                "entails": _boolean(sample["entails"], label=f"{label}.entails"),
            }
        )

    missing = raw["first_missing_capability"]
    if not isinstance(missing, dict) or set(missing) != {"category", "note"}:
        raise SessionDataError(
            f"{source}.first_missing_capability must contain category and note"
        )
    if missing["category"] not in CATEGORIES:
        raise SessionDataError(
            f"{source}.first_missing_capability.category must be one of "
            f"{sorted(CATEGORIES)}"
        )
    note = missing["note"]
    if not isinstance(note, str) or len(note) > 500 or any(
        ord(character) < 32 and character not in "\n\t" for character in note
    ):
        raise SessionDataError(
            f"{source}.first_missing_capability.note must be at most 500 safe characters"
        )

    return {
        "participant_id": participant_id,
        "cohort": cohort,
        "artifact_kind": artifact_kind,
        "doctor_green": doctor_green,
        "intervention_count": intervention_count,
        "install_to_green_doctor_seconds": install_seconds,
        "doctor_to_useful_outcome_seconds": useful_seconds,
        "outcome": outcome,
        "claim_judgments": normalized_judgments,
        "citation_samples": normalized_samples,
        "understands_gaps": _boolean(
            raw["understands_gaps"], label=f"{source}.understands_gaps"
        ),
        "understands_detail": _boolean(
            raw["understands_detail"], label=f"{source}.understands_detail"
        ),
        "authority_surprise": _boolean(
            raw["authority_surprise"], label=f"{source}.authority_surprise"
        ),
        "would_reuse": _boolean(raw["would_reuse"], label=f"{source}.would_reuse"),
        "first_missing_capability": {
            "category": missing["category"],
            "note": note,
        },
    }


def _criterion(*, passed: bool, observed: Any, required: str) -> dict[str, Any]:
    return {"passed": passed, "observed": observed, "required": required}


def evaluate(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    sessions = list(records)
    if len(sessions) != 5:
        raise SessionDataError(f"expected exactly five sessions, received {len(sessions)}")
    participant_ids = [item["participant_id"] for item in sessions]
    if len(set(participant_ids)) != len(participant_ids):
        raise SessionDataError("participant_id values must be unique")
    cohort = Counter(item["cohort"] for item in sessions)
    if cohort != COHORT:
        raise SessionDataError(
            f"cohort must be 2 developer, 2 research-data, 1 spreadsheet; got {dict(cohort)}"
        )

    no_intervention = sum(
        item["doctor_green"] and item["intervention_count"] == 0 for item in sessions
    )
    acceptable_outcomes = sum(
        item["outcome"] in ACCEPTABLE_OUTCOMES for item in sessions
    )
    samples = [sample for item in sessions for sample in item["citation_samples"]]
    sessions_with_sample = sum(bool(item["citation_samples"]) for item in sessions)
    resolving = sum(sample["resolves"] for sample in samples)
    entailing = sum(sample["entails"] for sample in samples)
    comprehension = sum(
        item["understands_gaps"] and item["understands_detail"] for item in sessions
    )
    useful_times = [
        item["doctor_to_useful_outcome_seconds"]
        for item in sessions
        if item["doctor_to_useful_outcome_seconds"] is not None
    ]
    median_seconds = statistics.median(useful_times) if useful_times else None
    surprises = sum(item["authority_surprise"] for item in sessions)
    reuse = sum(item["would_reuse"] for item in sessions)
    entailment_ratio = entailing / len(samples) if samples else 0.0

    criteria = {
        "green_doctor_without_intervention": _criterion(
            passed=no_intervention >= 4,
            observed=no_intervention,
            required=">= 4 of 5",
        ),
        "valid_tldr_or_actionable_decline": _criterion(
            passed=acceptable_outcomes == 5,
            observed=acceptable_outcomes,
            required="5 of 5",
        ),
        "sampled_citations_resolve": _criterion(
            passed=sessions_with_sample == 5 and resolving == len(samples),
            observed={
                "sessions_with_sample": sessions_with_sample,
                "resolving": resolving,
                "sampled": len(samples),
            },
            required="at least one per session and all resolve",
        ),
        "sampled_claim_entailment": _criterion(
            passed=entailment_ratio >= 0.90,
            observed={
                "entailing": entailing,
                "sampled": len(samples),
                "ratio": round(entailment_ratio, 6),
            },
            required=">= 0.90",
        ),
        "understands_gaps_and_detail": _criterion(
            passed=comprehension >= 4,
            observed=comprehension,
            required=">= 4 of 5",
        ),
        "median_time_to_useful_outcome_seconds": _criterion(
            passed=len(useful_times) == 5 and median_seconds is not None and median_seconds < 300,
            observed={"sessions_timed": len(useful_times), "median": median_seconds},
            required="all 5 timed and median < 300",
        ),
        "no_authority_surprise": _criterion(
            passed=surprises == 0,
            observed=surprises,
            required="0 of 5",
        ),
        "would_reuse": _criterion(
            passed=reuse >= 3,
            observed=reuse,
            required=">= 3 of 5",
        ),
    }
    return {
        "schema": REPORT_SCHEMA,
        "passed": all(item["passed"] for item in criteria.values()),
        "session_count": len(sessions),
        "cohort": dict(sorted(cohort.items())),
        "criteria": criteria,
        "classification_counts": dict(
            sorted(
                Counter(
                    item["first_missing_capability"]["category"] for item in sessions
                ).items()
            )
        ),
        "claim_judgment_totals": {
            key: sum(item["claim_judgments"][key] for item in sessions)
            for key in sorted(JUDGMENT_KEYS)
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate exactly five AutoTLDR private-alpha session records."
    )
    parser.add_argument("sessions", nargs="+", type=Path, metavar="SESSION.json")
    parser.add_argument("--json", action="store_true", help="emit only canonical JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        sessions = []
        for path in args.sessions:
            raw = json.loads(path.read_text(encoding="utf-8"))
            sessions.append(validate_session(raw, source=str(path)))
        report = evaluate(sessions)
    except (OSError, UnicodeError, json.JSONDecodeError, SessionDataError) as exc:
        print(f"autotldr alpha gate: {exc}", file=sys.stderr)
        return 2

    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.json:
        print(canonical)
    else:
        print("AutoTLDR private-alpha gate: " + ("PASS" if report["passed"] else "FAIL"))
        for name, criterion in report["criteria"].items():
            status = "pass" if criterion["passed"] else "FAIL"
            print(
                f"  {status:4}  {name}: {criterion['observed']} "
                f"(required {criterion['required']})"
            )
        print("report-json: " + canonical)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
