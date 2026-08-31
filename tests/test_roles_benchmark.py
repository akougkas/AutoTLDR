"""Offline contract tests for the Stage 2 role-tagging benchmark."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest


EVALUATOR_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "roles" / "evaluate.py"
SPEC = importlib.util.spec_from_file_location("autotldr_roles_evaluate", EVALUATOR_PATH)
assert SPEC and SPEC.loader
evaluate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluate
SPEC.loader.exec_module(evaluate)


def test_evaluated_roles_are_frozen_independently_of_live_taxonomy():
    assert evaluate.UNKNOWN_ROLE == "unknown"
    assert evaluate.ROLE_VALUES == (
        "unknown",
        "claim",
        "definition",
        "procedure",
        "parameter",
        "caveat",
        "result",
        "example",
        "decision",
        "assumption",
        "limitation",
    )


def _write_jsonl(path: Path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _policy() -> dict:
    return json.loads(evaluate.DEFAULT_POLICY.read_text(encoding="utf-8"))


def _report_policy(**overrides) -> dict:
    recoverability = {
        "minimum_support": 1,
        "minimum_source_groups": 1,
        "minimum_precision": 0.8,
        "minimum_recall": 0.7,
        "rules_local_f1_close_delta": 0.05,
        "material_f1_gain": 0.1,
    }
    recoverability.update(overrides)
    return {"recoverability": recoverability}


def _make_valid_corpus(tmp_path: Path):
    """Build a policy-valid synthetic corpus strictly inside pytest's temp dir."""
    policy = _policy()
    role_schedule: list[str] = []
    for role in evaluate.ROLE_VALUES:
        role_schedule.extend(
            [role] * policy["corpus"]["expected_role_counts"][role]
        )
    assert len(role_schedule) == 200

    formats = ["markdown", "rst", "text", "pdf", "xlsx"]
    within_format: Counter[str] = Counter()
    items = []
    labels = []
    source_by_id = {}
    frozen_roles = list(evaluate.ROLE_VALUES)
    for index, gold_role in enumerate(role_schedule):
        format_name = formats[index % len(formats)]
        format_index = within_format[format_name]
        within_format[format_name] += 1
        source_number = format_index // 5
        source_id = f"source-{format_name}-{source_number:02d}"
        source_group = f"group-{source_id}"
        source_by_id.setdefault(
            source_id,
            {
                "source_id": source_id,
                "source_group": source_group,
                "format": format_name,
                "title": f"Real-looking test source {source_id}",
                "uri": f"https://example.invalid/{source_id}",
                "license": "test-only synthetic data",
                "sha256": hashlib.sha256(source_id.encode()).hexdigest(),
            },
        )
        item_id = f"role-{index:04d}"
        items.append(
            {
                "id": item_id,
                "source_id": source_id,
                "source_group": source_group,
                "format": format_name,
                "modality": "record" if format_name == "xlsx" else "prose",
                "content": f"Benchmark content {index}",
                "structure": [f"Section {source_number}"],
                "evidence": {"heading": False, "ordinal": index},
                "origin": {"ref": f"line:{index + 1}", "char_span": [index, index + 1]},
                "rule_role": frozen_roles[(index + 1) % len(frozen_roles)],
                "rule_commit": "a" * 40,
            }
        )
        labels.append({"id": item_id, "role": gold_role})

    paths = {
        "items": tmp_path / "items.jsonl",
        "labels": tmp_path / "labels.jsonl",
        "sources": tmp_path / "sources.jsonl",
        "policy": evaluate.DEFAULT_POLICY,
        "prompt": evaluate.DEFAULT_PROMPT,
    }
    _write_jsonl(paths["items"], items)
    _write_jsonl(paths["labels"], labels)
    _write_jsonl(paths["sources"], source_by_id.values())
    return paths, items, labels, list(source_by_id.values())


def _make_balanced_pilot(tmp_path: Path):
    items = []
    labels = []
    for role in evaluate.ROLE_VALUES:
        for role_offset in range(3):
            item_id = f"pilot-{role}-{role_offset}"
            items.append(
                {
                    "id": item_id,
                    "format": "markdown",
                    "modality": "prose",
                    "content": f"Selection-pilot content for {role} {role_offset}",
                    "structure": ["Synthetic test section"],
                    "evidence": {"ordinal": role_offset},
                }
            )
            labels.append({"id": item_id, "role": role})

    paths = {
        "items": tmp_path / "pilot-items.jsonl",
        "labels": tmp_path / "pilot-labels.jsonl",
        "policy": tmp_path / "pilot-policy.json",
        "json_output": tmp_path / "pilot-report.json",
        "markdown_output": tmp_path / "pilot-report.md",
    }
    _write_jsonl(paths["items"], items)
    _write_jsonl(paths["labels"], labels)
    _write_pilot_policy(paths)
    return paths, items, labels


def _write_pilot_policy(paths, *, tie_order=None) -> None:
    policy = {
        "schema": 1,
        "pilot": {
            "items": 33,
            "roles": 11,
            "items_per_role": 3,
            "items_sha256": evaluate.sha256_file(paths["items"]),
            "labels_sha256": evaluate.sha256_file(paths["labels"]),
        },
        "runtime_gate": {
            "required_ok_predictions": 33,
            "invalid_or_error_predictions_allowed": 0,
        },
        "selection_rule": {
            "minimum_total_correct_advantage": 4,
            "maximum_roles_worse_than_incumbent": 2,
            "known_complete_artifact_size_order": tie_order
            or ["Smaller", "Larger", "Incumbent"],
            "unknown_artifact_tie": "retain-incumbent",
        },
    }
    paths["policy"].write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")


def _pilot_predictions(labels, correct_by_role, *, status_by_id=None):
    seen: Counter[str] = Counter()
    status_by_id = status_by_id or {}
    predictions = []
    for label in labels:
        item_id = label["id"]
        gold_role = label["role"]
        is_correct = seen[gold_role] < correct_by_role[gold_role]
        seen[gold_role] += 1
        role_index = evaluate.ROLE_VALUES.index(gold_role)
        predicted_role = (
            gold_role
            if is_correct
            else evaluate.ROLE_VALUES[(role_index + 1) % len(evaluate.ROLE_VALUES)]
        )
        status = status_by_id.get(item_id, "ok")
        if status != "ok":
            predicted_role = "unknown"
        predictions.append({"id": item_id, "role": predicted_role, "status": status})
    return predictions


def _write_candidate(path: Path, labels, correct_by_role, *, status_by_id=None):
    _write_jsonl(
        path,
        _pilot_predictions(
            labels, correct_by_role, status_by_id=status_by_id
        ),
    )


def test_validate_enforces_the_frozen_corpus_contract(tmp_path):
    paths, _, _, _ = _make_valid_corpus(tmp_path)

    summary = evaluate.validate_corpus(
        paths["items"], paths["labels"], paths["sources"], paths["policy"]
    )

    assert summary["status"] == "valid"
    assert summary["items"] == 200
    assert summary["formats"] == {
        "markdown": 40,
        "pdf": 40,
        "rst": 40,
        "text": 40,
        "xlsx": 40,
    }
    expected = _policy()["corpus"]["expected_role_counts"]
    assert summary["roles"] == {
        role: expected[role] for role in evaluate.ROLE_VALUES
    }
    assert summary["roles"]["result"] == 25
    assert summary["roles"]["unknown"] == 25
    assert all(len(value) == 64 for value in summary["hashes"].values())


def test_validate_rejects_any_count_other_than_200(tmp_path):
    paths, items, _, _ = _make_valid_corpus(tmp_path)
    _write_jsonl(paths["items"], items[:-1])

    with pytest.raises(evaluate.BenchmarkError, match="exactly 200"):
        evaluate.validate_corpus(
            paths["items"], paths["labels"], paths["sources"], paths["policy"]
        )


def test_validate_rejects_wrong_role_distribution(tmp_path):
    paths, _, labels, _ = _make_valid_corpus(tmp_path)
    labels[0]["role"] = "claim"
    _write_jsonl(paths["labels"], labels)

    with pytest.raises(evaluate.BenchmarkError, match="role distribution"):
        evaluate.validate_corpus(
            paths["items"], paths["labels"], paths["sources"], paths["policy"]
        )


def test_validate_rejects_gold_hidden_anywhere_in_an_item(tmp_path):
    paths, items, _, _ = _make_valid_corpus(tmp_path)
    items[0]["evidence"]["annotation"] = {"gold_role": "claim"}
    _write_jsonl(paths["items"], items)

    with pytest.raises(evaluate.BenchmarkError, match="forbidden gold field"):
        evaluate.validate_corpus(
            paths["items"], paths["labels"], paths["sources"], paths["policy"]
        )


def test_model_message_uses_only_the_explicit_whitelist():
    item = {
        "id": "DO-NOT-LEAK-ID",
        "source_id": "DO-NOT-LEAK-SOURCE",
        "source_group": "DO-NOT-LEAK-GROUP",
        "format": "pdf",
        "modality": "prose",
        "content": "Visible content",
        "structure": ["Visible section"],
        "evidence": {"caption": False},
        "origin": {"ref": "DO-NOT-LEAK-ORIGIN"},
        "rule_role": "decision",
        "rule_commit": "DO-NOT-LEAK-COMMIT",
        "gold": "DO-NOT-LEAK-GOLD",
    }

    rendered = evaluate.render_model_user_message(item)
    payload = json.loads(rendered)

    assert tuple(payload) == tuple(sorted(evaluate.MODEL_INPUT_FIELDS))
    assert set(payload) == set(evaluate.MODEL_INPUT_FIELDS)
    assert "DO-NOT-LEAK" not in rendered
    assert "rule_role" not in rendered
    assert "gold" not in rendered


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '```json\n{"role":"claim"}\n```',
        '{"role":"claim","confidence":1}',
        '{"role":"CLAIM"}',
        '[{"role":"claim"}]',
        '{"label":"claim"}',
    ],
)
def test_model_role_parser_is_strict(response):
    with pytest.raises(evaluate.BenchmarkError):
        evaluate.parse_model_role(response)


@pytest.mark.parametrize("role", evaluate.ROLE_VALUES)
def test_model_role_parser_accepts_each_exact_role(role):
    assert evaluate.parse_model_role(json.dumps({"role": role})) == role


def test_run_rules_exports_only_the_frozen_extractor_prediction(tmp_path):
    paths, items, _, _ = _make_valid_corpus(tmp_path)
    output = tmp_path / "predictions" / "rules.jsonl"

    predictions, manifest = evaluate.run_rules(
        paths["items"], output, paths["policy"], enforce_corpus=True
    )

    assert [row["role"] for row in predictions] == [item["rule_role"] for item in items]
    assert all(row["status"] == "ok" for row in predictions)
    assert manifest["rule_commit"] == "a" * 40
    assert manifest["counts"] == {"ok": 200, "invalid": 0, "error": 0}
    assert output.exists()
    manifest_path = output.with_suffix(".manifest.json")
    assert manifest_path.exists()
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["hashes"]["predictions_sha256"] == evaluate.sha256_file(output)


def test_run_model_is_label_blind_and_maps_invalid_or_errors_to_unknown(tmp_path):
    paths, items, _, _ = _make_valid_corpus(tmp_path)
    # The model runner has no labels parameter and remains runnable when labels
    # do not exist. This is the mechanical leakage boundary, not a convention.
    paths["labels"].unlink()
    output = tmp_path / "predictions" / "local.jsonl"
    seen_payloads = []

    def fake_transport(url, payload, headers, timeout):
        seen_payloads.append((url, payload, headers, timeout))
        call = len(seen_payloads)
        if call == 1:
            return {"choices": [{"message": {"content": "not-json"}}]}
        if call == 2:
            raise evaluate.BenchmarkError("endpoint returned HTTP 503")
        return {"choices": [{"message": {"content": '{"role":"claim"}'}}]}

    predictions, manifest = evaluate.run_model(
        arm="local",
        items_path=paths["items"],
        output_path=output,
        base_url="http://model.invalid/v1",
        model="four-billion-parameters",
        api_key="secret-key",
        prompt_path=paths["prompt"],
        policy_path=paths["policy"],
        transport=fake_transport,
        enforce_corpus=True,
    )

    assert len(seen_payloads) == len(items) == 200
    assert predictions[0]["status"] == "invalid"
    assert predictions[0]["role"] == "unknown"
    assert predictions[1]["status"] == "error"
    assert predictions[1]["role"] == "unknown"
    assert predictions[2]["status"] == "ok"
    assert predictions[2]["role"] == "claim"
    assert manifest["counts"] == {"ok": 198, "invalid": 1, "error": 1}
    assert manifest["model"] == "four-billion-parameters"
    assert "secret-key" not in json.dumps(manifest)

    url, request, headers, timeout = seen_payloads[0]
    assert url == "http://model.invalid/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret-key"
    assert timeout == 60.0
    assert request["temperature"] == 0
    assert request["seed"] == 20260830
    assert request["max_tokens"] == 32
    assert request["response_format"] == evaluate.ROLE_RESPONSE_FORMAT
    assert request["reasoning_effort"] == "none"
    user_payload = json.loads(request["messages"][1]["content"])
    assert set(user_payload) == set(evaluate.MODEL_INPUT_FIELDS)
    assert items[0]["id"] not in request["messages"][1]["content"]
    assert items[0]["rule_commit"] not in request["messages"][1]["content"]


def test_run_model_accepts_minimal_label_blind_pilot_items(tmp_path):
    item = {
        "id": "pilot-0001",
        "format": "markdown",
        "modality": "prose",
        "content": "The service must reject expired credentials.",
        "structure": ["Security requirements"],
        "evidence": {"heading": False},
    }
    items_path = tmp_path / "pilot-items.jsonl"
    output = tmp_path / "predictions" / "pilot.jsonl"
    _write_jsonl(items_path, [item])
    seen_requests = []

    def fake_transport(url, payload, headers, timeout):
        seen_requests.append(payload)
        return {"choices": [{"message": {"content": '{"role":"claim"}'}}]}

    predictions, manifest = evaluate.run_model(
        arm="local",
        items_path=items_path,
        output_path=output,
        base_url="http://model.invalid/v1",
        model="pilot-model",
        prompt_path=evaluate.DEFAULT_PROMPT,
        policy_path=evaluate.DEFAULT_POLICY,
        transport=fake_transport,
        enforce_corpus=False,
    )

    assert len(predictions) == 1
    assert predictions[0]["id"] == "pilot-0001"
    assert predictions[0]["role"] == "claim"
    assert predictions[0]["status"] == "ok"
    assert predictions[0]["latency_ms"] >= 0
    assert manifest["items"] == 1
    assert manifest["counts"] == {"ok": 1, "invalid": 0, "error": 0}
    assert len(seen_requests) == 1
    model_payload = json.loads(seen_requests[0]["messages"][1]["content"])
    assert set(model_payload) == set(evaluate.MODEL_INPUT_FIELDS)
    assert "id" not in model_payload


def test_run_model_rejects_gold_role_in_minimal_pilot_items(tmp_path):
    item = {
        "id": "pilot-0001",
        "format": "markdown",
        "modality": "prose",
        "content": "The service must reject expired credentials.",
        "structure": ["Security requirements"],
        "evidence": {"heading": False},
        "gold_role": "claim",
    }
    items_path = tmp_path / "pilot-items.jsonl"
    _write_jsonl(items_path, [item])

    with pytest.raises(evaluate.BenchmarkError, match=r"extra=\['gold_role'\]"):
        evaluate.run_model(
            arm="local",
            items_path=items_path,
            output_path=tmp_path / "predictions" / "pilot.jsonl",
            base_url="http://model.invalid/v1",
            model="pilot-model",
            prompt_path=evaluate.DEFAULT_PROMPT,
            policy_path=evaluate.DEFAULT_POLICY,
            transport=lambda *args: pytest.fail("invalid pilot item reached transport"),
            enforce_corpus=False,
        )


def test_score_pilot_selects_challenger_at_the_frozen_boundary(tmp_path):
    paths, _, labels = _make_balanced_pilot(tmp_path)
    incumbent_path = tmp_path / "incumbent.jsonl"
    challenger_path = tmp_path / "challenger.jsonl"
    incumbent_correct = {role: 1 for role in evaluate.ROLE_VALUES}
    challenger_correct = {
        role: 2 if index < 4 else 1
        for index, role in enumerate(evaluate.ROLE_VALUES)
    }
    _write_candidate(incumbent_path, labels, incumbent_correct)
    _write_candidate(challenger_path, labels, challenger_correct)

    report = evaluate.score_pilot_from_files(
        items_path=paths["items"],
        labels_path=paths["labels"],
        candidate_paths={
            "Incumbent": incumbent_path,
            "Smaller": challenger_path,
        },
        incumbent="Incumbent",
        policy_path=paths["policy"],
        json_output=paths["json_output"],
        markdown_output=paths["markdown_output"],
    )

    challenger = report["candidates"]["Smaller"]
    assert report["selection"]["selected_candidate"] == "Smaller"
    assert report["selection"]["retained_incumbent"] is False
    assert report["candidates"]["Incumbent"]["exact_correct"] == 11
    assert challenger["exact_correct"] == 15
    assert set(challenger["per_role_correct"]) == set(evaluate.ROLE_VALUES)
    assert all(0 <= count <= 3 for count in challenger["per_role_correct"].values())
    assert challenger["statuses"] == {"ok": 33, "invalid": 0, "error": 0}
    assert challenger["switch_conditions"]["total_correct_advantage"] == {
        "actual": 4,
        "minimum": 4,
        "passed": True,
    }
    assert challenger["switch_conditions"]["roles_worse_than_incumbent"][
        "actual"
    ] == 0
    assert challenger["qualifies_to_replace_incumbent"] is True
    assert json.loads(paths["json_output"].read_text(encoding="utf-8")) == report
    markdown = paths["markdown_output"].read_text(encoding="utf-8")
    assert "It is not role-recoverability evidence" in markdown
    assert "| Smaller | 15/33 |" in markdown
    assert "## Per-role exact-correct counts" in markdown


def test_score_pilot_enforces_worse_role_and_runtime_conditions(tmp_path):
    paths, _, labels = _make_balanced_pilot(tmp_path)
    incumbent_path = tmp_path / "incumbent.jsonl"
    worse_roles_path = tmp_path / "worse-roles.jsonl"
    runtime_failure_path = tmp_path / "runtime-failure.jsonl"
    incumbent_correct = {role: 2 for role in evaluate.ROLE_VALUES}
    worse_roles_correct = {
        role: 1 if index < 3 else 3
        for index, role in enumerate(evaluate.ROLE_VALUES)
    }
    runtime_failure_correct = {role: 3 for role in evaluate.ROLE_VALUES}
    failed_id = labels[2]["id"]
    _write_candidate(incumbent_path, labels, incumbent_correct)
    _write_candidate(worse_roles_path, labels, worse_roles_correct)
    _write_candidate(
        runtime_failure_path,
        labels,
        runtime_failure_correct,
        status_by_id={failed_id: "invalid"},
    )

    report = evaluate.score_pilot_from_files(
        items_path=paths["items"],
        labels_path=paths["labels"],
        candidate_paths={
            "Incumbent": incumbent_path,
            "WorseRoles": worse_roles_path,
            "RuntimeFailure": runtime_failure_path,
        },
        incumbent="Incumbent",
        policy_path=paths["policy"],
        json_output=paths["json_output"],
        markdown_output=paths["markdown_output"],
    )

    worse = report["candidates"]["WorseRoles"]
    assert worse["exact_correct"] - report["candidates"]["Incumbent"][
        "exact_correct"
    ] == 5
    assert worse["switch_conditions"]["roles_worse_than_incumbent"][
        "actual"
    ] == 3
    assert worse["qualifies_to_replace_incumbent"] is False
    runtime = report["candidates"]["RuntimeFailure"]
    assert runtime["statuses"] == {"ok": 32, "invalid": 1, "error": 0}
    assert runtime["switch_conditions"]["runtime_gate"]["passed"] is False
    assert runtime["qualifies_to_replace_incumbent"] is False
    assert report["selection"]["selected_candidate"] == "Incumbent"


def test_score_pilot_uses_frozen_size_order_for_known_exact_tie(tmp_path):
    paths, _, labels = _make_balanced_pilot(tmp_path)
    incumbent_path = tmp_path / "incumbent.jsonl"
    smaller_path = tmp_path / "smaller.jsonl"
    larger_path = tmp_path / "larger.jsonl"
    incumbent_correct = {role: 1 for role in evaluate.ROLE_VALUES}
    tied_correct = {
        role: 2 if index < 4 else 1
        for index, role in enumerate(evaluate.ROLE_VALUES)
    }
    _write_candidate(incumbent_path, labels, incumbent_correct)
    _write_candidate(smaller_path, labels, tied_correct)
    _write_candidate(larger_path, labels, tied_correct)

    report = evaluate.score_pilot_from_files(
        items_path=paths["items"],
        labels_path=paths["labels"],
        candidate_paths={
            "Larger": larger_path,
            "Incumbent": incumbent_path,
            "Smaller": smaller_path,
        },
        incumbent="Incumbent",
        policy_path=paths["policy"],
        json_output=paths["json_output"],
        markdown_output=paths["markdown_output"],
    )

    assert report["selection"]["selected_candidate"] == "Smaller"
    assert "artifact size order" in report["selection"]["reason"]
    first_json = paths["json_output"].read_bytes()
    evaluate.score_pilot_from_files(
        items_path=paths["items"],
        labels_path=paths["labels"],
        candidate_paths={
            "Smaller": smaller_path,
            "Incumbent": incumbent_path,
            "Larger": larger_path,
        },
        incumbent="Incumbent",
        policy_path=paths["policy"],
        json_output=paths["json_output"],
        markdown_output=paths["markdown_output"],
    )
    assert paths["json_output"].read_bytes() == first_json


def test_score_pilot_retains_incumbent_for_unlisted_artifact_size_tie(tmp_path):
    paths, _, labels = _make_balanced_pilot(tmp_path)
    incumbent_path = tmp_path / "incumbent.jsonl"
    known_path = tmp_path / "known.jsonl"
    arbitrary_path = tmp_path / "arbitrary-zbook-model.jsonl"
    incumbent_correct = {role: 1 for role in evaluate.ROLE_VALUES}
    tied_correct = {
        role: 2 if index < 4 else 1
        for index, role in enumerate(evaluate.ROLE_VALUES)
    }
    _write_candidate(incumbent_path, labels, incumbent_correct)
    _write_candidate(known_path, labels, tied_correct)
    _write_candidate(arbitrary_path, labels, tied_correct)

    report = evaluate.score_pilot_from_files(
        items_path=paths["items"],
        labels_path=paths["labels"],
        candidate_paths={
            "Incumbent": incumbent_path,
            "Smaller": known_path,
            "Any-Other-ZBook-Candidate": arbitrary_path,
        },
        incumbent="Incumbent",
        policy_path=paths["policy"],
        json_output=paths["json_output"],
        markdown_output=paths["markdown_output"],
    )

    assert report["selection"]["selected_candidate"] == "Incumbent"
    assert report["selection"]["unresolved_artifact_size_tie"] == [
        "Any-Other-ZBook-Candidate",
        "Smaller",
    ]


def test_score_pilot_rejects_non_balanced_labels(tmp_path):
    paths, _, labels = _make_balanced_pilot(tmp_path)
    labels[0]["role"] = labels[3]["role"]
    _write_jsonl(paths["labels"], labels)
    _write_pilot_policy(paths)
    incumbent_path = tmp_path / "incumbent.jsonl"
    _write_candidate(
        incumbent_path,
        labels,
        {role: 1 for role in evaluate.ROLE_VALUES},
    )

    with pytest.raises(evaluate.BenchmarkError, match="every role exactly 3 times"):
        evaluate.score_pilot_from_files(
            items_path=paths["items"],
            labels_path=paths["labels"],
            candidate_paths={"Incumbent": incumbent_path},
            incumbent="Incumbent",
            policy_path=paths["policy"],
            json_output=paths["json_output"],
            markdown_output=paths["markdown_output"],
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows.pop(), "differ from items"),
        (lambda rows: rows[0].update(role="not-a-role"), "invalid role"),
        (lambda rows: rows[0].update(status="retried"), "invalid status"),
    ],
)
def test_score_pilot_validates_prediction_ids_roles_and_statuses(
    tmp_path, mutation, message
):
    paths, _, labels = _make_balanced_pilot(tmp_path)
    candidate_path = tmp_path / "candidate.jsonl"
    rows = _pilot_predictions(
        labels, {role: 1 for role in evaluate.ROLE_VALUES}
    )
    mutation(rows)
    _write_jsonl(candidate_path, rows)

    with pytest.raises(evaluate.BenchmarkError, match=message):
        evaluate.score_pilot_from_files(
            items_path=paths["items"],
            labels_path=paths["labels"],
            candidate_paths={"Incumbent": candidate_path},
            incumbent="Incumbent",
            policy_path=paths["policy"],
            json_output=paths["json_output"],
            markdown_output=paths["markdown_output"],
        )


def test_score_predictions_reports_per_role_confusion_and_abstention():
    gold = {"a": "claim", "b": "claim", "c": "caveat", "d": "unknown"}
    predicted = {"a": "claim", "b": "unknown", "c": "claim", "d": "unknown"}

    score = evaluate.score_predictions(gold, predicted)

    claim = score["per_role"]["claim"]
    assert claim == {
        "support": 2,
        "predicted": 2,
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
        "abstained": 1,
    }
    assert score["confusion"]["caveat"]["claim"] == 1
    assert score["confusion"]["unknown"]["unknown"] == 1
    assert score["unknown_predictions"] == 2
    assert score["coverage"] == 0.5


def test_report_contains_all_arms_roles_formats_and_confusions():
    items = [
        {"id": "a", "format": "pdf", "source_group": "paper-1"},
        {"id": "b", "format": "xlsx", "source_group": "book-1"},
    ]
    labels = [{"id": "a", "role": "claim"}, {"id": "b", "role": "result"}]
    predictions = {
        "rules": {
            "a": {"id": "a", "role": "unknown", "status": "ok"},
            "b": {"id": "b", "role": "result", "status": "ok"},
        },
        "local": {
            "a": {"id": "a", "role": "claim", "status": "ok"},
            "b": {"id": "b", "role": "result", "status": "ok"},
        },
        "frontier": {
            "a": {"id": "a", "role": "claim", "status": "ok"},
            "b": {"id": "b", "role": "claim", "status": "ok"},
        },
    }

    report = evaluate.build_report(
        items, labels, predictions, policy=_report_policy()
    )
    markdown = evaluate.render_markdown_report(report)

    assert report["schema"] == 2
    assert set(report["arms"]) == {"rules", "local", "frontier"}
    assert set(report["arms"]["rules"]["per_role"]) == set(evaluate.ROLE_VALUES)
    assert set(report["arms"]["rules"]["per_format"]) == {"pdf", "xlsx"}
    assert report["arms"]["local"]["per_role"]["claim"]["f1"] == 1.0
    assert report["arms"]["frontier"]["confusion"]["result"]["claim"] == 1
    assert report["arms"]["local"]["per_role"]["claim"]["recoverable"] is True
    assert report["arms"]["rules"]["per_role"]["claim"]["recoverable"] is False
    assert report["arms"]["frontier"]["per_role"]["claim"][
        "recoverability_checks"
    ] == {
        "support": True,
        "source_groups": True,
        "precision": False,
        "recall": True,
    }
    assert report["recoverability"]["passing_roles_by_arm"] == {
        "rules": ["result"],
        "local": ["claim", "result"],
        "frontier": [],
    }
    assert "Aggregate accuracy is intentionally omitted" in markdown
    assert "Passing roles by arm:" in markdown
    assert "- `local`: claim, result" in markdown
    assert "local gate" in markdown
    assert "PASS" in markdown
    assert "FAIL" in markdown
    assert "## Per-format scores" in markdown
    assert "## Confusion matrices" in markdown


def test_recoverability_requires_support_and_source_group_minima():
    items = [{"id": "a", "format": "pdf", "source_group": "paper-1"}]
    labels = [{"id": "a", "role": "claim"}]
    predictions = {
        "local": {"a": {"id": "a", "role": "claim", "status": "ok"}}
    }

    report = evaluate.build_report(
        items,
        labels,
        predictions,
        policy=_report_policy(minimum_support=2, minimum_source_groups=2),
    )

    metric = report["arms"]["local"]["per_role"]["claim"]
    assert metric["recoverability_checks"] == {
        "support": False,
        "source_groups": False,
        "precision": True,
        "recall": True,
    }
    assert metric["recoverable"] is False
    assert report["recoverability"]["passing_roles_by_arm"]["local"] == []


def test_report_from_files_applies_the_supplied_recoverability_policy(tmp_path):
    items = [{"id": "a", "format": "pdf", "source_group": "paper-1"}]
    labels = [{"id": "a", "role": "claim"}]
    prediction_rows = {
        "rules": [{"id": "a", "role": "unknown", "status": "ok"}],
        "local": [{"id": "a", "role": "claim", "status": "ok"}],
        "frontier": [{"id": "a", "role": "claim", "status": "ok"}],
    }
    items_path = tmp_path / "items.jsonl"
    labels_path = tmp_path / "labels.jsonl"
    policy_path = tmp_path / "policy.json"
    prompt_path = tmp_path / "prompt.md"
    output_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    _write_jsonl(items_path, items)
    _write_jsonl(labels_path, labels)
    prediction_paths = {}
    for arm, rows in prediction_rows.items():
        prediction_paths[arm] = tmp_path / f"{arm}.jsonl"
        _write_jsonl(prediction_paths[arm], rows)
    policy_path.write_text(
        json.dumps(_report_policy(), sort_keys=True) + "\n", encoding="utf-8"
    )
    prompt_path.write_text("Synthetic prompt.\n", encoding="utf-8")

    report = evaluate.report_from_files(
        items_path=items_path,
        labels_path=labels_path,
        rules_path=prediction_paths["rules"],
        local_path=prediction_paths["local"],
        frontier_path=prediction_paths["frontier"],
        policy_path=policy_path,
        prompt_path=prompt_path,
        json_output=output_path,
        markdown_output=markdown_path,
    )

    assert report["recoverability"]["passing_roles_by_arm"] == {
        "rules": [],
        "local": ["claim"],
        "frontier": ["claim"],
    }
    saved_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved_report["arms"]["local"]["per_role"]["claim"]["recoverable"] is True
    assert "- `rules`: none" in markdown_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "field",
    [
        "minimum_support",
        "minimum_source_groups",
        "minimum_precision",
        "minimum_recall",
        "rules_local_f1_close_delta",
        "material_f1_gain",
    ],
)
def test_report_rejects_missing_recoverability_fields(field):
    policy = _report_policy()
    del policy["recoverability"][field]

    with pytest.raises(evaluate.BenchmarkError, match="missing required fields"):
        evaluate.build_report([], [], {}, policy=policy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_support", 0),
        ("minimum_support", 1.0),
        ("minimum_source_groups", True),
        ("minimum_source_groups", -1),
        ("minimum_precision", -0.01),
        ("minimum_precision", "0.8"),
        ("minimum_recall", 1.01),
        ("minimum_recall", False),
        ("rules_local_f1_close_delta", float("inf")),
        ("rules_local_f1_close_delta", "0.05"),
        ("material_f1_gain", float("nan")),
        ("material_f1_gain", 1.1),
    ],
)
def test_report_rejects_invalid_recoverability_field_types_and_ranges(field, value):
    with pytest.raises(evaluate.BenchmarkError, match=repr(field)):
        evaluate.build_report([], [], {}, policy=_report_policy(**{field: value}))


@pytest.mark.parametrize("policy", [{}, {"recoverability": []}])
def test_report_requires_a_recoverability_policy_object(policy):
    with pytest.raises(evaluate.BenchmarkError, match="recoverability object"):
        evaluate.build_report([], [], {}, policy=policy)


def test_default_parser_exposes_the_required_subcommands():
    parser = evaluate.build_parser()
    subparser_action = next(
        action
        for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    )
    assert set(subparser_action.choices) == {
        "validate",
        "run-rules",
        "run-model",
        "score-pilot",
        "report",
    }


def test_model_subcommand_has_no_labels_argument():
    parser = evaluate.build_parser()
    args = parser.parse_args(
        [
            "run-model",
            "--arm",
            "local",
            "--base-url",
            "http://example.invalid/v1",
            "--model",
            "model",
        ]
    )
    assert not hasattr(args, "labels")


def test_model_subcommand_exposes_pilot_flag():
    parser = evaluate.build_parser()
    args = parser.parse_args(
        [
            "run-model",
            "--arm",
            "local",
            "--base-url",
            "http://example.invalid/v1",
            "--model",
            "model",
            "--pilot",
        ]
    )

    assert args.pilot is True


def test_score_pilot_subcommand_accepts_repeated_arbitrary_candidates():
    parser = evaluate.build_parser()
    args = parser.parse_args(
        [
            "score-pilot",
            "--candidate",
            "Incumbent=/tmp/incumbent.jsonl",
            "--candidate",
            "Any-ZBook-Model=/tmp/challenger.jsonl",
            "--incumbent",
            "Incumbent",
        ]
    )

    assert evaluate._parse_candidate_specs(args.candidate) == {
        "Incumbent": Path("/tmp/incumbent.jsonl"),
        "Any-ZBook-Model": Path("/tmp/challenger.jsonl"),
    }
