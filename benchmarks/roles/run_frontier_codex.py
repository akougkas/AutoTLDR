#!/usr/bin/env python3
"""Run the Stage 2 frontier arm through subscription-authenticated Codex CLI.

This adapter exists because a ChatGPT-authenticated Codex installation and an
OpenAI API key are different credentials.  It keeps the benchmark's important
boundaries intact despite using the CLI transport:

* it accepts items, the frozen prompt, and policy -- never gold labels;
* every item is sent to one fresh ``codex exec`` process, with no retry;
* every process starts in a newly-created, empty, non-repository directory;
* only :data:`evaluate.MODEL_INPUT_FIELDS` cross the model boundary; and
* an invalid response, observed tool call, or invocation failure is recorded
  as ``unknown`` rather than repaired.

The Codex CLI does not expose temperature, seed, or output-token controls.
Those frozen policy values are recorded as requested-but-unavailable in the
manifest instead of being falsely claimed as applied settings.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
EVALUATOR_PATH = HERE / "evaluate.py"


def _load_evaluator():
    """Load the adjacent standalone evaluator without making it a package."""
    module_name = "autotldr_roles_evaluate"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, EVALUATOR_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - static path
        raise RuntimeError(f"cannot load evaluator from {EVALUATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


evaluate = _load_evaluator()

DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_OUTPUT = evaluate.DEFAULT_PREDICTIONS / "frontier.jsonl"
DEFAULT_TIMEOUT = 120.0
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 16

JsonObject = dict[str, Any]
CommandRunner = Callable[
    [Sequence[str], str | None, Path, float], subprocess.CompletedProcess[str]
]
ProgressCallback = Callable[[int, int, str, str], None]

# Codex's event stream can contain reasoning and final-message items without
# violating this categorical benchmark.  Every executable/retrieval item is a
# protocol violation: the unit and prompt are already complete.
DISALLOWED_CODEX_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "tool_call",
        "dynamic_tool_call",
        "collab_tool_call",
        "computer_use",
        "image_generation",
    }
)

# Use the very same schema object as the generic OpenAI-compatible runner,
# copied through JSON so neither module can mutate the other's nested values.
OUTPUT_SCHEMA: JsonObject = json.loads(
    json.dumps(evaluate.ROLE_RESPONSE_FORMAT["json_schema"]["schema"])
)


def _default_command_runner(
    command: Sequence[str], stdin: str | None, cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=stdin,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_frozen_prompt(prompt_path: Path) -> str:
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise evaluate.BenchmarkError(f"cannot read prompt {prompt_path}: {exc}") from exc
    if not prompt:
        raise evaluate.BenchmarkError("prompt must not be empty")
    return prompt


def render_codex_stdin(system_prompt: str, item: Mapping[str, Any]) -> str:
    """Frame the exact generic-runner prompt and exact whitelisted item only.

    ``codex exec`` has one initial-instruction channel rather than separate
    system/user chat messages.  The two frozen message bodies are therefore
    joined by exactly one blank line; no adapter-specific instruction is added.
    """
    return f"{system_prompt}\n\n{evaluate.render_model_user_message(item)}\n"


def _schema_text() -> str:
    return json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _codex_command(
    *,
    codex_binary: str,
    model: str,
    work_dir: Path,
    schema_path: Path,
    response_path: Path,
) -> list[str]:
    return [
        codex_binary,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-C",
        str(work_dir),
        "--model",
        model,
        "--sandbox",
        "read-only",
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        'web_search="disabled"',
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(response_path),
        "--json",
        "--color",
        "never",
        "-",
    ]


def _has_tool_event(stdout: str) -> bool:
    """Return true for any observed executable/retrieval Codex item."""
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in DISALLOWED_CODEX_ITEM_TYPES:
            return True
        if isinstance(item_type, str) and (
            item_type.endswith("_tool_call") or item_type.endswith("_execution")
        ):
            return True
    return False


def _safe_cli_version(
    codex_binary: str, runner: CommandRunner, timeout: float
) -> str:
    """Read a bounded, credential-free CLI version string for provenance."""
    with tempfile.TemporaryDirectory(prefix="autotldr-codex-version-") as raw_dir:
        cwd = Path(raw_dir)
        try:
            proc = runner([codex_binary, "--version"], None, cwd, min(timeout, 10.0))
        except subprocess.TimeoutExpired:
            return "unavailable (timeout)"
        except OSError:
            return "unavailable (executable)"
        except Exception as exc:  # defensive boundary around injected runners
            return f"unavailable ({type(exc).__name__})"
    if proc.returncode != 0:
        return f"unavailable (exit {proc.returncode})"
    first_line = proc.stdout.strip().splitlines()[:1]
    if not first_line:
        return "unavailable (empty)"
    # A version should be tiny.  Bounding it also prevents an unusual binary
    # from smuggling arbitrary stdout into the manifest.
    return first_line[0][:200]


def _run_one(
    *,
    item: Mapping[str, Any],
    system_prompt: str,
    codex_binary: str,
    model: str,
    timeout: float,
    runner: CommandRunner,
) -> JsonObject:
    started = time.perf_counter()
    role = "unknown"
    status = "ok"
    error: str | None = None
    tool_violation = False

    with tempfile.TemporaryDirectory(prefix="autotldr-frontier-") as raw_root:
        invocation_root = Path(raw_root)
        work_dir = invocation_root / "work"
        work_dir.mkdir()
        schema_path = invocation_root / "role.schema.json"
        response_path = invocation_root / "response.json"
        schema_path.write_text(_schema_text(), encoding="utf-8")
        command = _codex_command(
            codex_binary=codex_binary,
            model=model,
            work_dir=work_dir,
            schema_path=schema_path,
            response_path=response_path,
        )
        try:
            proc = runner(
                command,
                render_codex_stdin(system_prompt, item),
                work_dir,
                timeout,
            )
            if proc.returncode != 0:
                status = "error"
                error = f"codex exited with status {proc.returncode}"
            elif _has_tool_event(proc.stdout):
                status = "invalid"
                error = "codex emitted a disallowed tool event"
                tool_violation = True
            else:
                try:
                    raw_response = response_path.read_text(encoding="utf-8")
                except OSError:
                    status = "error"
                    error = "codex wrote no final response"
                else:
                    try:
                        role = evaluate.parse_model_role(raw_response)
                    except evaluate.BenchmarkError as exc:
                        status = "invalid"
                        error = str(exc)
        except subprocess.TimeoutExpired:
            status = "error"
            error = "codex invocation timed out"
        except OSError:
            status = "error"
            error = "codex executable unavailable"
        except Exception as exc:  # defensive boundary around injected runners
            status = "error"
            error = f"codex invocation raised {type(exc).__name__}"

    if status != "ok":
        role = "unknown"
    prediction: JsonObject = {
        "id": item["id"],
        "role": role,
        "status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    if error is not None:
        prediction["error"] = error
    if tool_violation:
        prediction["tool_violation"] = True
    return prediction


def _validate_policy(policy: Mapping[str, Any]) -> JsonObject:
    prompt_policy = policy.get("prompt")
    if not isinstance(prompt_policy, dict):
        raise evaluate.BenchmarkError("policy must contain prompt settings")
    if prompt_policy.get("fields") != list(evaluate.MODEL_INPUT_FIELDS):
        raise evaluate.BenchmarkError(
            "policy prompt.fields must be exactly "
            f"{list(evaluate.MODEL_INPUT_FIELDS)!r}"
        )
    if prompt_policy.get("temperature") != 0:
        raise evaluate.BenchmarkError("the frozen benchmark requires temperature 0")
    seed = prompt_policy.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise evaluate.BenchmarkError("the frozen benchmark seed must be an integer")
    max_tokens = prompt_policy.get("max_tokens")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 1
    ):
        raise evaluate.BenchmarkError("max_tokens must be a positive integer")
    return prompt_policy


def run_frontier_codex(
    *,
    items_path: Path = evaluate.DEFAULT_ITEMS,
    output_path: Path = DEFAULT_OUTPUT,
    prompt_path: Path = evaluate.DEFAULT_PROMPT,
    policy_path: Path = evaluate.DEFAULT_POLICY,
    manifest_path: Path | None = None,
    model: str = DEFAULT_MODEL,
    codex_binary: str = "codex",
    timeout: float = DEFAULT_TIMEOUT,
    concurrency: int = DEFAULT_CONCURRENCY,
    runner: CommandRunner | None = None,
    progress: ProgressCallback | None = None,
    enforce_corpus: bool = True,
) -> tuple[list[JsonObject], JsonObject]:
    """Run one label-blind, independent Codex CLI request per corpus item."""
    if not isinstance(model, str) or not model.strip():
        raise evaluate.BenchmarkError("model must be non-empty")
    if not isinstance(codex_binary, str) or not codex_binary.strip():
        raise evaluate.BenchmarkError("codex binary must be non-empty")
    if timeout <= 0:
        raise evaluate.BenchmarkError("timeout must be positive")
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not 1 <= concurrency <= MAX_CONCURRENCY
    ):
        raise evaluate.BenchmarkError(
            f"concurrency must be an integer from 1 through {MAX_CONCURRENCY}"
        )

    items = evaluate.load_jsonl(items_path)
    policy = evaluate.load_json(policy_path)
    if enforce_corpus:
        evaluate.validate_items(items, policy)
    else:
        for offset, item in enumerate(items, start=1):
            evaluate._validate_item_shape(item, offset)
    prompt_policy = _validate_policy(policy)
    system_prompt = _read_frozen_prompt(prompt_path)
    command_runner = runner or _default_command_runner
    codex_version = _safe_cli_version(codex_binary, command_runner, timeout)

    predictions_by_index: dict[int, JsonObject] = {}
    progress_lock = threading.Lock()
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_index = {
            pool.submit(
                _run_one,
                item=item,
                system_prompt=system_prompt,
                codex_binary=codex_binary,
                model=model,
                timeout=timeout,
                runner=command_runner,
            ): index
            for index, item in enumerate(items)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            # _run_one contains the subprocess exception boundary.  This guard
            # covers programming errors without losing the complete arm file.
            try:
                prediction = future.result()
            except Exception as exc:  # pragma: no cover - defensive last resort
                prediction = {
                    "id": items[index]["id"],
                    "role": "unknown",
                    "status": "error",
                    "latency_ms": 0.0,
                    "error": f"frontier worker raised {type(exc).__name__}",
                }
            predictions_by_index[index] = prediction
            if progress is not None:
                with progress_lock:
                    completed += 1
                    progress(completed, len(items), prediction["id"], prediction["status"])

    predictions = [predictions_by_index[index] for index in range(len(items))]
    evaluate.write_jsonl(output_path, predictions)
    counts: collections.Counter[str] = collections.Counter(
        prediction["status"] for prediction in predictions
    )
    tool_violations = sum(
        prediction.get("tool_violation") is True for prediction in predictions
    )
    settings = {
        "transport": "codex-cli-chatgpt-subscription",
        "timeout_seconds": timeout,
        "concurrency": concurrency,
        "requests_per_item": 1,
        "retries": 0,
        "ephemeral": True,
        "working_directory": "fresh-empty-temporary-directory-per-item",
        "sandbox": "read-only",
        "user_config": "ignored",
        "exec_rules": "ignored",
        "git_repository_check": "skipped",
        "shell_environment_inheritance": "none",
        "web_search": "disabled",
        "structured_output": "strict-json-schema",
        "accepted_tool_events": 0,
        # Codex CLI does not expose these three request controls.  Preserve the
        # requested policy values while making the transport limitation plain.
        "policy_temperature": prompt_policy["temperature"],
        "policy_seed": prompt_policy["seed"],
        "policy_max_tokens": prompt_policy["max_tokens"],
        "sampling_controls": "not-exposed-by-codex-cli",
    }
    manifest: JsonObject = {
        "schema": 1,
        "arm": "frontier",
        "runner": "codex-cli-one-item-per-process",
        "created_at": evaluate._utc_now(),
        "items": len(items),
        "model": model,
        "codex_version": codex_version,
        "evaluator_commit": evaluate._git_commit(),
        "label_blind": True,
        "settings": settings,
        "hashes": {
            "items_sha256": evaluate.sha256_file(items_path),
            "prompt_sha256": evaluate.sha256_file(prompt_path),
            "policy_sha256": evaluate.sha256_file(policy_path),
            "output_schema_sha256": hashlib.sha256(
                _schema_text().encode("utf-8")
            ).hexdigest(),
            "predictions_sha256": evaluate.sha256_file(output_path),
            "sanitized_config_sha256": _sha256_json(
                {
                    "model": model,
                    "prompt_fields": evaluate.MODEL_INPUT_FIELDS,
                    "settings": settings,
                }
            ),
        },
        "counts": {name: counts[name] for name in ("ok", "invalid", "error")},
        "tool_violations": tool_violations,
    }
    evaluate.write_json(
        manifest_path or output_path.with_suffix(".manifest.json"), manifest
    )
    return predictions, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=Path, default=evaluate.DEFAULT_ITEMS)
    parser.add_argument("--prompt", type=Path, default=evaluate.DEFAULT_PROMPT)
    parser.add_argument("--policy", type=Path, default=evaluate.DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def show_progress(done: int, total: int, item_id: str, status: str) -> None:
        print(f"frontier {done}/{total} {item_id} {status}", file=sys.stderr, flush=True)

    try:
        _, manifest = run_frontier_codex(
            items_path=args.items,
            output_path=args.output,
            prompt_path=args.prompt,
            policy_path=args.policy,
            manifest_path=args.manifest,
            model=args.model,
            codex_binary=args.codex_binary,
            timeout=args.timeout,
            concurrency=args.concurrency,
            progress=show_progress,
            enforce_corpus=True,
        )
    except evaluate.BenchmarkError as exc:
        print(f"frontier benchmark: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
