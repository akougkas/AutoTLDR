"""Fully offline contracts for the subscription-backed frontier adapter."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path


RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "roles"
    / "run_frontier_codex.py"
)
SPEC = importlib.util.spec_from_file_location("autotldr_roles_frontier", RUNNER_PATH)
assert SPEC and SPEC.loader
frontier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = frontier
SPEC.loader.exec_module(frontier)


def _write_jsonl(path: Path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _item(index: int, content: str) -> dict:
    return {
        "id": f"HIDDEN-ID-{index}",
        "source_id": f"HIDDEN-SOURCE-{index}",
        "source_group": f"HIDDEN-GROUP-{index}",
        "format": "markdown",
        "modality": "prose",
        "content": content,
        "structure": ["Visible section"],
        "evidence": {"heading": False, "ordinal": index},
        "origin": {"ref": f"HIDDEN-ORIGIN-{index}"},
        "rule_role": "decision",
        "rule_commit": "HIDDEN-COMMIT-" + "a" * 40,
    }


class SuccessfulFakeRunner:
    """A thread-safe fake that writes Codex's output-last-message file."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.first_pair = threading.Barrier(2)
        self.item_calls = 0

    def __call__(self, command, stdin, cwd, timeout):
        command = list(command)
        if command == ["fake-codex", "--version"]:
            assert stdin is None
            assert not any(cwd.iterdir())
            return subprocess.CompletedProcess(
                command, 0, stdout="codex-cli 9.8.7\n", stderr=""
            )

        assert command[:2] == ["fake-codex", "exec"]
        assert not any(cwd.iterdir())
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.item_calls += 1
            ordinal = self.item_calls
            self.calls.append(
                {
                    "command": command,
                    "stdin": stdin,
                    "cwd": cwd,
                    "timeout": timeout,
                    "empty_at_start": True,
                }
            )
        if ordinal <= 2:
            self.first_pair.wait(timeout=2)
        # Complete out of order to prove the file remains in corpus order.
        if stdin and "Visible item 0" in stdin:
            time.sleep(0.02)
        response_path = Path(command[command.index("--output-last-message") + 1])
        response_path.write_text('{"role":"claim"}\n', encoding="utf-8")
        with self.lock:
            self.active -= 1
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"offline"}',
                '{"type":"item.completed","item":{"type":"reasoning"}}',
                '{"type":"item.completed","item":{"type":"agent_message"}}',
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_frontier_runner_is_one_item_per_process_isolated_and_label_blind(tmp_path):
    items = [_item(index, f"Visible item {index}") for index in range(3)]
    items_path = tmp_path / "items.jsonl"
    output_path = tmp_path / "predictions" / "frontier.jsonl"
    _write_jsonl(items_path, items)
    fake = SuccessfulFakeRunner()

    predictions, manifest = frontier.run_frontier_codex(
        items_path=items_path,
        output_path=output_path,
        model="frontier-model",
        codex_binary="fake-codex",
        timeout=17,
        concurrency=2,
        runner=fake,
        enforce_corpus=False,
    )

    assert fake.item_calls == len(items) == 3
    assert fake.max_active == 2
    assert len({call["cwd"] for call in fake.calls}) == len(items)
    assert all(call["empty_at_start"] for call in fake.calls)
    assert [row["id"] for row in predictions] == [item["id"] for item in items]
    assert all(row["role"] == "claim" and row["status"] == "ok" for row in predictions)

    frozen_prompt = frontier.evaluate.DEFAULT_PROMPT.read_text(encoding="utf-8").strip()
    for call, item in zip(
        sorted(fake.calls, key=lambda value: value["stdin"]),
        sorted(items, key=lambda value: value["content"]),
    ):
        command = call["command"]
        assert "--ephemeral" in command
        assert "--ignore-user-config" in command
        assert "--ignore-rules" in command
        assert "--skip-git-repo-check" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("--model") + 1] == "frontier-model"
        assert 'shell_environment_policy.inherit="none"' in command
        assert 'web_search="disabled"' in command
        assert command[-1] == "-"
        assert call["stdin"] == (
            frozen_prompt
            + "\n\n"
            + frontier.evaluate.render_model_user_message(item)
            + "\n"
        )
        assert item["id"] not in call["stdin"]
        assert item["origin"]["ref"] not in call["stdin"]
        assert item["rule_commit"] not in call["stdin"]
        schema_path = Path(command[command.index("--output-schema") + 1])
        # Temp paths are gone after the run, but their schema hash in the
        # manifest is checked against the frozen in-memory schema below.
        assert schema_path.name == "role.schema.json"

    assert manifest["model"] == "frontier-model"
    assert manifest["codex_version"] == "codex-cli 9.8.7"
    assert manifest["label_blind"] is True
    assert manifest["counts"] == {"ok": 3, "invalid": 0, "error": 0}
    assert manifest["tool_violations"] == 0
    assert manifest["settings"]["requests_per_item"] == 1
    assert manifest["settings"]["retries"] == 0
    assert manifest["settings"]["sampling_controls"] == "not-exposed-by-codex-cli"
    assert set(frontier.OUTPUT_SCHEMA["properties"]["role"]["enum"]) == set(
        frontier.evaluate.ROLE_VALUES
    )
    assert frontier.OUTPUT_SCHEMA == frontier.evaluate.ROLE_RESPONSE_FORMAT["json_schema"][
        "schema"
    ]
    persisted = json.loads(
        output_path.with_suffix(".manifest.json").read_text(encoding="utf-8")
    )
    assert persisted["hashes"]["predictions_sha256"] == frontier.evaluate.sha256_file(
        output_path
    )


class FailureFakeRunner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.exec_inputs: list[str] = []

    def __call__(self, command, stdin, cwd, timeout):
        command = list(command)
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="codex-cli offline", stderr="")
        assert stdin is not None
        with self.lock:
            self.exec_inputs.append(stdin)
        if "malformed response" in stdin:
            response_path = Path(command[command.index("--output-last-message") + 1])
            response_path.write_text('{"role":"claim","extra":true}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if "nonzero exit" in stdin:
            return subprocess.CompletedProcess(command, 23, stdout="", stderr="SECRET")
        if "timeout response" in stdin:
            raise subprocess.TimeoutExpired(command, timeout)
        if "tool response" in stdin:
            response_path = Path(command[command.index("--output-last-message") + 1])
            response_path.write_text('{"role":"claim"}', encoding="utf-8")
            stdout = (
                '{"type":"item.started","item":'
                '{"type":"command_execution","command":"env"}}'
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        raise AssertionError("unexpected fake request")


def test_frontier_failures_are_unknown_without_retry_and_secrets_are_not_recorded(tmp_path):
    contents = [
        "malformed response",
        "nonzero exit",
        "timeout response",
        "tool response",
    ]
    items = [_item(index, content) for index, content in enumerate(contents)]
    items_path = tmp_path / "items.jsonl"
    output_path = tmp_path / "frontier.jsonl"
    _write_jsonl(items_path, items)
    fake = FailureFakeRunner()

    predictions, manifest = frontier.run_frontier_codex(
        items_path=items_path,
        output_path=output_path,
        codex_binary="fake-codex",
        runner=fake,
        concurrency=4,
        enforce_corpus=False,
    )

    assert len(fake.exec_inputs) == len(items)  # exactly once; there is no retry
    assert [row["status"] for row in predictions] == [
        "invalid",
        "error",
        "error",
        "invalid",
    ]
    assert all(row["role"] == "unknown" for row in predictions)
    assert predictions[3]["tool_violation"] is True
    assert manifest["counts"] == {"ok": 0, "invalid": 2, "error": 2}
    assert manifest["tool_violations"] == 1
    serialized = json.dumps({"predictions": predictions, "manifest": manifest})
    assert "SECRET" not in serialized


def test_frontier_cli_has_no_labels_argument_and_reproducible_defaults():
    args = frontier.build_parser().parse_args([])

    assert not hasattr(args, "labels")
    assert args.model == "gpt-5.6-sol"
    assert args.concurrency == 4
    assert args.timeout == 120.0
