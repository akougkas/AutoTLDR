#!/usr/bin/env python3
"""Stage 2 role-tagging benchmark.

The benchmark deliberately lives outside :mod:`autotldr`.  Model clients,
corpus validation, and reporting must never enter the CLI's cold-start import
graph.  Everything here uses the standard library plus AutoTLDR's canonical
``Modality`` enum.  The evaluated role strings are frozen here because the
point of Stage 2 is to decide which members survive in the live ``Role`` enum.

The strongest boundary in this module is also the simplest: model execution
accepts an items file and has no labels argument.  Gold labels are loaded only
by ``validate`` and ``report``.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from autotldr.unit import Modality


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

DEFAULT_ITEMS = HERE / "items.jsonl"
DEFAULT_LABELS = HERE / "labels.jsonl"
DEFAULT_SOURCES = HERE / "sources.jsonl"
DEFAULT_PROMPT = HERE / "prompt.md"
DEFAULT_POLICY = HERE / "policy.json"
DEFAULT_PREDICTIONS = HERE / "predictions"
DEFAULT_PILOT_ITEMS = HERE / "pilot" / "items.jsonl"
DEFAULT_PILOT_LABELS = HERE / "pilot" / "labels.jsonl"
DEFAULT_PILOT_POLICY = HERE / "pilot_policy.json"
DEFAULT_PILOT_JSON = HERE / "pilot" / "report.json"
DEFAULT_PILOT_MARKDOWN = HERE / "pilot" / "report.md"

UNKNOWN_ROLE = "unknown"
# Historical Stage 2 evaluation taxonomy. Do not derive this tuple from the
# live Role enum: shrinking that enum must not invalidate the frozen corpus,
# predictions, or report. Validation and reporting reproduce from those frozen
# files; rebuilding the corpus's historical rule output requires the pinned
# pre-shrink rules commit 54b2158a37e7dd42392494fbadf031e11d952289.
ROLE_VALUES = (
    UNKNOWN_ROLE,
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
ROLE_SET = frozenset(ROLE_VALUES)
NAMED_ROLE_VALUES = tuple(role for role in ROLE_VALUES if role != UNKNOWN_ROLE)
MODALITY_SET = frozenset(modality.value for modality in Modality)
LOCAL_MODEL_ARMS = frozenset({"local", "ornith"})

# This is the complete payload boundary.  Adding a field is an evaluation
# design change and should be made in policy.json, prompt.md, and tests together.
MODEL_INPUT_FIELDS = ("format", "modality", "content", "structure", "evidence")

ROLE_RESPONSE_FORMAT: JsonObject = {
    "type": "json_schema",
    "json_schema": {
        "name": "role_prediction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": list(ROLE_VALUES)},
            },
            "required": ["role"],
            "additionalProperties": False,
        },
    },
}

REQUIRED_ITEM_FIELDS = frozenset(
    {
        "id",
        "source_id",
        "source_group",
        "format",
        "modality",
        "content",
        "structure",
        "evidence",
        "origin",
        "rule_role",
        "rule_commit",
    }
)
REQUIRED_SOURCE_FIELDS = frozenset(
    {"source_id", "source_group", "format", "title", "uri", "license", "sha256"}
)
FORBIDDEN_ITEM_FIELDS = frozenset(
    {
        "label",
        "labels",
        "gold",
        "gold_role",
        "annotation",
        "annotations",
        "adjudicated_role",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

JsonObject = dict[str, Any]
Transport = Callable[[str, JsonObject, Mapping[str, str], float], JsonObject]


class BenchmarkError(ValueError):
    """A corpus, prediction, configuration, or response is invalid."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise BenchmarkError(f"cannot hash {path}: {exc}") from exc


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def load_json(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"{path} must contain one JSON object")
    return value


def load_jsonl(path: Path) -> list[JsonObject]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BenchmarkError(f"cannot read {path}: {exc}") from exc

    rows: list[JsonObject] = []
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise BenchmarkError(f"{path}:{line_no}: each JSONL row must be an object")
        rows.append(value)
    return rows


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: tempfile.NamedTemporaryFile[str] | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        )
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except OSError as exc:
        if handle is not None:
            try:
                Path(handle.name).unlink(missing_ok=True)
            except OSError:
                pass
        raise BenchmarkError(f"cannot write {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    text = "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    _atomic_write_text(path, text)


def _index_unique(rows: Sequence[JsonObject], key: str, source_name: str) -> dict[str, JsonObject]:
    result: dict[str, JsonObject] = {}
    for offset, row in enumerate(rows, start=1):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            raise BenchmarkError(f"{source_name} row {offset} has no non-empty {key!r}")
        if value in result:
            raise BenchmarkError(f"{source_name} contains duplicate {key} {value!r}")
        result[value] = row
    return result


def _ensure_no_gold_fields(value: Any, location: str = "item") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_ITEM_FIELDS:
                raise BenchmarkError(f"{location} contains forbidden gold field {key!r}")
            _ensure_no_gold_fields(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _ensure_no_gold_fields(child, f"{location}[{index}]")


def _validate_item_shape(item: JsonObject, offset: int) -> None:
    missing = REQUIRED_ITEM_FIELDS - item.keys()
    if missing:
        raise BenchmarkError(f"item row {offset} is missing fields: {', '.join(sorted(missing))}")
    _ensure_no_gold_fields(item, f"item row {offset}")

    for field in ("id", "source_id", "source_group", "format", "content", "rule_commit"):
        if not isinstance(item[field], str) or not item[field].strip():
            raise BenchmarkError(f"item row {offset} field {field!r} must be a non-empty string")
    if item["modality"] not in MODALITY_SET:
        raise BenchmarkError(
            f"item {item['id']!r} has invalid modality {item['modality']!r}"
        )
    if item["rule_role"] not in ROLE_SET:
        raise BenchmarkError(
            f"item {item['id']!r} has invalid frozen rule_role {item['rule_role']!r}"
        )
    if not isinstance(item["structure"], list) or not all(
        isinstance(part, str) for part in item["structure"]
    ):
        raise BenchmarkError(f"item {item['id']!r} structure must be a list of strings")
    if not isinstance(item["evidence"], dict):
        raise BenchmarkError(f"item {item['id']!r} evidence must be an object")
    origin = item["origin"]
    if not isinstance(origin, dict) or not isinstance(origin.get("ref"), str) or not origin["ref"]:
        raise BenchmarkError(f"item {item['id']!r} origin must contain a non-empty ref")
    if "char_span" in origin and origin["char_span"] is not None:
        span = origin["char_span"]
        if (
            not isinstance(span, list)
            or len(span) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in span)
            or span[0] < 0
            or span[1] < span[0]
        ):
            raise BenchmarkError(
                f"item {item['id']!r} origin.char_span must be null or [start, end]"
            )


def _validate_pilot_item_shape(item: JsonObject, offset: int) -> None:
    """Validate the minimal, label-blind shape used for model selection.

    A pilot item intentionally omits source identity, origin, and frozen rule
    output.  Requiring *exactly* ``id`` plus the five model-visible fields makes
    the clean-room boundary inspectable and prevents metadata from becoming an
    accidental model-selection feature.
    """
    expected = frozenset({"id", *MODEL_INPUT_FIELDS})
    if set(item) != expected:
        missing = sorted(expected - item.keys())
        extra = sorted(item.keys() - expected)
        raise BenchmarkError(
            f"pilot item row {offset} must contain exactly id plus the five "
            f"model fields; missing={missing}, extra={extra}"
        )
    _ensure_no_gold_fields(item, f"pilot item row {offset}")
    for field in ("id", "format", "content"):
        if not isinstance(item[field], str) or not item[field].strip():
            raise BenchmarkError(
                f"pilot item row {offset} field {field!r} must be a non-empty string"
            )
    if item["modality"] not in MODALITY_SET:
        raise BenchmarkError(
            f"pilot item {item['id']!r} has invalid modality {item['modality']!r}"
        )
    if not isinstance(item["structure"], list) or not all(
        isinstance(part, str) for part in item["structure"]
    ):
        raise BenchmarkError(
            f"pilot item {item['id']!r} structure must be a list of strings"
        )
    if not isinstance(item["evidence"], dict):
        raise BenchmarkError(f"pilot item {item['id']!r} evidence must be an object")


def _corpus_policy(policy: JsonObject) -> JsonObject:
    corpus = policy.get("corpus")
    if not isinstance(corpus, dict):
        raise BenchmarkError("policy.json must contain a corpus object")
    return corpus


def _recoverability_policy(policy: JsonObject) -> JsonObject:
    recoverability = policy.get("recoverability")
    if not isinstance(recoverability, dict):
        raise BenchmarkError("policy.json must contain a recoverability object")

    integer_fields = ("minimum_support", "minimum_source_groups")
    rate_fields = (
        "minimum_precision",
        "minimum_recall",
        "rules_local_f1_close_delta",
        "material_f1_gain",
    )
    required = integer_fields + rate_fields
    missing = [field for field in required if field not in recoverability]
    if missing:
        raise BenchmarkError(
            "policy recoverability is missing required fields: " + ", ".join(missing)
        )

    settings: JsonObject = {}
    for field in integer_fields:
        value = recoverability[field]
        if type(value) is not int or value <= 0:
            raise BenchmarkError(
                f"policy recoverability field {field!r} must be a positive integer"
            )
        settings[field] = value
    for field in rate_fields:
        value = recoverability[field]
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise BenchmarkError(
                f"policy recoverability field {field!r} must be a finite number "
                "from 0 through 1"
            )
        settings[field] = float(value)
    return settings


def validate_items(items: Sequence[JsonObject], policy: JsonObject) -> dict[str, Any]:
    corpus = _corpus_policy(policy)
    expected_items = corpus.get("expected_items")
    expected_format_count = corpus.get("expected_format_count")
    items_per_format = corpus.get("items_per_format")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (expected_items, expected_format_count, items_per_format)
    ):
        raise BenchmarkError(
            "policy corpus counts must be positive integers: expected_items, "
            "expected_format_count, items_per_format"
        )
    if len(items) != expected_items:
        raise BenchmarkError(f"expected exactly {expected_items} items, found {len(items)}")

    item_by_id = _index_unique(items, "id", "items")
    origins: set[tuple[str, str]] = set()
    format_counts: collections.Counter[str] = collections.Counter()
    rule_commits: set[str] = set()
    for offset, item in enumerate(items, start=1):
        _validate_item_shape(item, offset)
        format_counts[item["format"]] += 1
        rule_commits.add(item["rule_commit"])
        origin_key = (item["source_id"], item["origin"]["ref"])
        if origin_key in origins:
            raise BenchmarkError(
                f"duplicate addressable origin {origin_key[0]}#{origin_key[1]}"
            )
        origins.add(origin_key)

    if len(format_counts) != expected_format_count:
        raise BenchmarkError(
            f"expected exactly {expected_format_count} formats, found "
            f"{len(format_counts)}: {dict(sorted(format_counts.items()))}"
        )
    wrong_formats = {
        name: count for name, count in format_counts.items() if count != items_per_format
    }
    if wrong_formats:
        raise BenchmarkError(
            f"every format must have exactly {items_per_format} items; found {wrong_formats}"
        )
    if len(rule_commits) != 1:
        raise BenchmarkError(
            f"all frozen rule predictions must name one commit; found {sorted(rule_commits)}"
        )

    return {
        "count": len(item_by_id),
        "formats": dict(sorted(format_counts.items())),
        "rule_commit": next(iter(rule_commits)),
    }


def validate_corpus(
    items_path: Path = DEFAULT_ITEMS,
    labels_path: Path = DEFAULT_LABELS,
    sources_path: Path = DEFAULT_SOURCES,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    """Validate the frozen corpus and return a machine-readable summary."""
    policy = load_json(policy_path)
    items = load_jsonl(items_path)
    labels = load_jsonl(labels_path)
    sources = load_jsonl(sources_path)
    item_summary = validate_items(items, policy)

    item_by_id = _index_unique(items, "id", "items")
    label_by_id = _index_unique(labels, "id", "labels")
    source_by_id = _index_unique(sources, "source_id", "sources")
    if set(item_by_id) != set(label_by_id):
        missing = sorted(set(item_by_id) - set(label_by_id))
        extra = sorted(set(label_by_id) - set(item_by_id))
        raise BenchmarkError(
            f"item/label ids differ; missing labels={missing[:5]}, extra labels={extra[:5]}"
        )

    role_counts: collections.Counter[str] = collections.Counter()
    role_groups: dict[str, set[str]] = {role: set() for role in ROLE_VALUES}
    for offset, label in enumerate(labels, start=1):
        role = label.get("role")
        if role not in ROLE_SET:
            raise BenchmarkError(f"label row {offset} has invalid role {role!r}")
        item = item_by_id[label["id"]]
        role_counts[role] += 1
        role_groups[role].add(item["source_group"])

    expected_roles = _corpus_policy(policy).get("expected_role_counts")
    if not isinstance(expected_roles, dict) or set(expected_roles) != ROLE_SET:
        raise BenchmarkError(
            "policy expected_role_counts must contain every Role value exactly once"
        )
    invalid_support = {
        role: support
        for role, support in expected_roles.items()
        if not isinstance(support, int) or isinstance(support, bool) or support < 0
    }
    if invalid_support:
        raise BenchmarkError(f"invalid expected role support: {invalid_support}")
    if dict(role_counts) != {role: expected_roles[role] for role in role_counts} or any(
        role_counts[role] != expected_roles[role] for role in ROLE_VALUES
    ):
        raise BenchmarkError(
            "gold role distribution does not match policy; "
            f"expected={expected_roles}, found={dict(role_counts)}"
        )

    minimum_groups = _corpus_policy(policy).get("minimum_source_groups_per_role", 1)
    if not isinstance(minimum_groups, int) or isinstance(minimum_groups, bool) or minimum_groups < 1:
        raise BenchmarkError("minimum_source_groups_per_role must be a positive integer")
    undercovered = {
        role: len(groups) for role, groups in role_groups.items() if len(groups) < minimum_groups
    }
    if undercovered:
        raise BenchmarkError(
            f"roles must span at least {minimum_groups} source groups; found {undercovered}"
        )

    for offset, source in enumerate(sources, start=1):
        missing = REQUIRED_SOURCE_FIELDS - source.keys()
        if missing:
            raise BenchmarkError(
                f"source row {offset} is missing fields: {', '.join(sorted(missing))}"
            )
        for field in ("source_id", "source_group", "format", "title", "uri", "license"):
            if not isinstance(source[field], str) or not source[field].strip():
                raise BenchmarkError(
                    f"source {source.get('source_id', offset)!r} field {field!r} "
                    "must be a non-empty string"
                )
        if not isinstance(source["sha256"], str) or not SHA256_RE.fullmatch(source["sha256"]):
            raise BenchmarkError(
                f"source {source['source_id']!r} sha256 must be 64 lowercase hex characters"
            )

    missing_sources = sorted({item["source_id"] for item in items} - set(source_by_id))
    if missing_sources:
        raise BenchmarkError(f"items reference absent sources: {missing_sources[:5]}")
    for item in items:
        source = source_by_id[item["source_id"]]
        if item["format"] != source["format"]:
            raise BenchmarkError(
                f"item {item['id']!r} format does not match source {item['source_id']!r}"
            )
        if item["source_group"] != source["source_group"]:
            raise BenchmarkError(
                f"item {item['id']!r} source_group does not match source manifest"
            )

    prompt_policy = policy.get("prompt")
    if not isinstance(prompt_policy, dict) or prompt_policy.get("fields") != list(MODEL_INPUT_FIELDS):
        raise BenchmarkError(
            f"policy prompt.fields must be exactly {list(MODEL_INPUT_FIELDS)!r}"
        )

    return {
        "schema": 1,
        "status": "valid",
        "items": item_summary["count"],
        "formats": item_summary["formats"],
        "roles": {role: role_counts[role] for role in ROLE_VALUES},
        "sources": len(source_by_id),
        "source_groups_by_role": {
            role: len(role_groups[role]) for role in ROLE_VALUES
        },
        "rule_commit": item_summary["rule_commit"],
        "hashes": {
            "items_sha256": sha256_file(items_path),
            "labels_sha256": sha256_file(labels_path),
            "sources_sha256": sha256_file(sources_path),
            "policy_sha256": sha256_file(policy_path),
        },
    }


def build_model_input(item: Mapping[str, Any]) -> JsonObject:
    """Return the *only* item fields an endpoint may receive.

    A JSON round trip both copies nested values and prevents a custom mapping
    from exposing data during later serialization.
    """
    missing = [field for field in MODEL_INPUT_FIELDS if field not in item]
    if missing:
        raise BenchmarkError(f"model item is missing whitelist fields: {missing}")
    visible = {field: item[field] for field in MODEL_INPUT_FIELDS}
    return json.loads(json.dumps(visible, ensure_ascii=False))


def render_model_user_message(item: Mapping[str, Any]) -> str:
    return json.dumps(
        build_model_input(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def parse_model_role(content: str) -> str:
    """Parse the deliberately tiny output contract without repairs or coercion."""
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BenchmarkError("response is not a JSON object") from exc
    if not isinstance(value, dict) or set(value) != {"role"}:
        raise BenchmarkError("response must contain exactly one key named 'role'")
    role = value["role"]
    if not isinstance(role, str) or role not in ROLE_SET:
        raise BenchmarkError(f"response role is outside the taxonomy: {role!r}")
    return role


def _extract_chat_content(response: JsonObject) -> str:
    try:
        choices = response["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BenchmarkError("response lacks choices[0].message.content") from exc
    if not isinstance(content, str):
        raise BenchmarkError("response message content is not a string")
    return content


def _default_transport(
    url: str,
    payload: JsonObject,
    headers: Mapping[str, str],
    timeout: float,
) -> JsonObject:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise BenchmarkError(f"endpoint returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise BenchmarkError(f"endpoint transport failed: {type(exc.reason).__name__}") from exc
    except TimeoutError as exc:
        raise BenchmarkError("endpoint request timed out") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchmarkError("endpoint returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise BenchmarkError("endpoint response must be a JSON object")
    return value


def _git_commit() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = proc.stdout.strip()
    return commit if proc.returncode == 0 and commit else None


def _manifest_path(output_path: Path) -> Path:
    return output_path.with_suffix(".manifest.json")


def run_rules(
    items_path: Path = DEFAULT_ITEMS,
    output_path: Path = DEFAULT_PREDICTIONS / "rules.jsonl",
    policy_path: Path = DEFAULT_POLICY,
    manifest_path: Path | None = None,
    *,
    enforce_corpus: bool = True,
) -> tuple[list[JsonObject], JsonObject]:
    """Export the extractor's already-frozen prediction as the rules arm."""
    items = load_jsonl(items_path)
    policy = load_json(policy_path)
    if enforce_corpus:
        item_summary = validate_items(items, policy)
    else:
        for offset, item in enumerate(items, start=1):
            _validate_item_shape(item, offset)
        item_summary = {
            "rule_commit": sorted({item["rule_commit"] for item in items})[0]
            if items
            else None
        }

    predictions = [
        {"id": item["id"], "role": item["rule_role"], "status": "ok"}
        for item in items
    ]
    write_jsonl(output_path, predictions)
    manifest = {
        "schema": 1,
        "arm": "rules",
        "runner": "frozen-extractor-role",
        "created_at": _utc_now(),
        "items": len(items),
        "rule_commit": item_summary.get("rule_commit"),
        "evaluator_commit": _git_commit(),
        "hashes": {
            "items_sha256": sha256_file(items_path),
            "policy_sha256": sha256_file(policy_path),
            "predictions_sha256": sha256_file(output_path),
        },
        "counts": {"ok": len(predictions), "invalid": 0, "error": 0},
    }
    write_json(manifest_path or _manifest_path(output_path), manifest)
    return predictions, manifest


def _chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def run_model(
    *,
    arm: str,
    items_path: Path = DEFAULT_ITEMS,
    output_path: Path,
    base_url: str,
    model: str,
    api_key: str | None = None,
    prompt_path: Path = DEFAULT_PROMPT,
    policy_path: Path = DEFAULT_POLICY,
    manifest_path: Path | None = None,
    timeout: float = 60.0,
    max_tokens: int | None = None,
    concurrency: int = 1,
    transport: Transport | None = None,
    enforce_corpus: bool = True,
) -> tuple[list[JsonObject], JsonObject]:
    """Run one OpenAI-compatible chat-completions arm.

    Invalid protocol/model output is recorded as ``UNKNOWN``.  No labels path is
    accepted by this function or its CLI subcommand.
    """
    if not arm or not model or not base_url:
        raise BenchmarkError("arm, model, and base_url must be non-empty")
    if timeout <= 0:
        raise BenchmarkError("timeout must be positive")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= 16:
        raise BenchmarkError("concurrency must be an integer from 1 through 16")

    items = load_jsonl(items_path)
    policy = load_json(policy_path)
    if enforce_corpus:
        validate_items(items, policy)
    else:
        for offset, item in enumerate(items, start=1):
            _validate_pilot_item_shape(item, offset)

    try:
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BenchmarkError(f"cannot read prompt {prompt_path}: {exc}") from exc
    if not system_prompt:
        raise BenchmarkError("prompt must not be empty")

    prompt_policy = policy.get("prompt")
    if not isinstance(prompt_policy, dict):
        raise BenchmarkError("policy must contain prompt settings")
    if prompt_policy.get("fields") != list(MODEL_INPUT_FIELDS):
        raise BenchmarkError(
            f"policy prompt.fields must be exactly {list(MODEL_INPUT_FIELDS)!r}"
        )
    temperature = prompt_policy.get("temperature", 0)
    seed = prompt_policy.get("seed", 20260830)
    token_limit = max_tokens if max_tokens is not None else prompt_policy.get("max_tokens", 32)
    if temperature != 0:
        raise BenchmarkError("the frozen benchmark requires temperature 0")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise BenchmarkError("the frozen benchmark seed must be an integer")
    if not isinstance(token_limit, int) or isinstance(token_limit, bool) or token_limit < 1:
        raise BenchmarkError("max_tokens must be a positive integer")

    sender = transport or _default_transport
    url = _chat_url(base_url)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def predict_one(item: JsonObject) -> JsonObject:
        request_payload: JsonObject = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": render_model_user_message(item)},
            ],
            "temperature": 0,
            "seed": seed,
            "max_tokens": token_limit,
            "response_format": ROLE_RESPONSE_FORMAT,
        }
        if arm in LOCAL_MODEL_ARMS:
            # LM Studio's OpenAI-compatible endpoint ignores Qwen's usual
            # chat_template_kwargs switch, but its reasoning_effort control is
            # verified to suppress the separate reasoning stream.
            request_payload["reasoning_effort"] = "none"
        started = time.perf_counter()
        status = "ok"
        error: str | None = None
        role = UNKNOWN_ROLE
        try:
            response = sender(url, request_payload, headers, timeout)
            role = parse_model_role(_extract_chat_content(response))
        except BenchmarkError as exc:
            message = str(exc)
            status = "error" if message.startswith("endpoint ") else "invalid"
            error = message
        except Exception as exc:  # defensive boundary around injected transports
            status = "error"
            error = f"transport raised {type(exc).__name__}"
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        prediction: JsonObject = {
            "id": item["id"],
            "role": role,
            "status": status,
            "latency_ms": elapsed_ms,
        }
        if error:
            prediction["error"] = error
        return prediction

    if concurrency == 1:
        predictions = [predict_one(item) for item in items]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            # Executor.map preserves corpus order while scheduling requests in
            # parallel at the transport layer.
            predictions = list(pool.map(predict_one, items))
    counts = collections.Counter(prediction["status"] for prediction in predictions)

    write_jsonl(output_path, predictions)
    parsed_base = urllib.parse.urlsplit(base_url)
    endpoint_kind = (
        f"{parsed_base.scheme}://{parsed_base.hostname}" if parsed_base.scheme and parsed_base.hostname else "configured"
    )
    manifest = {
        "schema": 1,
        "arm": arm,
        "runner": "openai-compatible-chat-completions",
        "created_at": _utc_now(),
        "items": len(items),
        "model": model,
        "endpoint": endpoint_kind,
        "evaluator_commit": _git_commit(),
        "settings": {
            "temperature": 0,
            "seed": seed,
            "max_tokens": token_limit,
            "timeout_seconds": timeout,
            "concurrency": concurrency,
            "requests_per_item": 1,
            "structured_output": "strict-json-schema",
            "thinking": "disabled-via-reasoning-effort" if arm in LOCAL_MODEL_ARMS else "provider-default",
        },
        "hashes": {
            "items_sha256": sha256_file(items_path),
            "prompt_sha256": sha256_file(prompt_path),
            "policy_sha256": sha256_file(policy_path),
            "predictions_sha256": sha256_file(output_path),
            "sanitized_config_sha256": _sha256_json(
                {
                    "arm": arm,
                    "model": model,
                    "temperature": 0,
                    "seed": seed,
                    "max_tokens": token_limit,
                    "prompt_fields": MODEL_INPUT_FIELDS,
                }
            ),
        },
        "counts": {name: counts[name] for name in ("ok", "invalid", "error")},
    }
    write_json(manifest_path or _manifest_path(output_path), manifest)
    return predictions, manifest


def _load_predictions(path: Path, expected_ids: set[str]) -> dict[str, JsonObject]:
    rows = load_jsonl(path)
    by_id = _index_unique(rows, "id", f"predictions {path}")
    if set(by_id) != expected_ids:
        missing = sorted(expected_ids - set(by_id))
        extra = sorted(set(by_id) - expected_ids)
        raise BenchmarkError(
            f"prediction ids in {path} differ from items; missing={missing[:5]}, extra={extra[:5]}"
        )
    for item_id, row in by_id.items():
        if row.get("role") not in ROLE_SET:
            raise BenchmarkError(f"prediction {item_id!r} has invalid role {row.get('role')!r}")
        if row.get("status") not in {"ok", "invalid", "error"}:
            raise BenchmarkError(
                f"prediction {item_id!r} has invalid status {row.get('status')!r}"
            )
        if row["status"] != "ok" and row["role"] != UNKNOWN_ROLE:
            raise BenchmarkError(
                f"failed prediction {item_id!r} must be recorded as unknown"
            )
    return by_id


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score_predictions(
    gold: Mapping[str, str],
    predicted: Mapping[str, str],
) -> dict[str, Any]:
    if set(gold) != set(predicted):
        raise BenchmarkError("gold and predicted id sets must match before scoring")

    confusion = {
        actual: {guess: 0 for guess in ROLE_VALUES} for actual in ROLE_VALUES
    }
    for item_id, actual in gold.items():
        guess = predicted[item_id]
        if actual not in ROLE_SET or guess not in ROLE_SET:
            raise BenchmarkError(f"invalid role while scoring item {item_id!r}")
        confusion[actual][guess] += 1

    per_role: dict[str, Any] = {}
    for role in ROLE_VALUES:
        tp = confusion[role][role]
        support = sum(confusion[role].values())
        predicted_count = sum(confusion[actual][role] for actual in ROLE_VALUES)
        fp = predicted_count - tp
        fn = support - tp
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        f1 = _safe_ratio(2 * precision * recall, precision + recall)
        per_role[role] = {
            "support": support,
            "predicted": predicted_count,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "abstained": confusion[role][UNKNOWN_ROLE]
            if role != UNKNOWN_ROLE
            else 0,
        }

    unknown_predictions = sum(
        confusion[actual][UNKNOWN_ROLE] for actual in ROLE_VALUES
    )
    return {
        "items": len(gold),
        "unknown_predictions": unknown_predictions,
        "coverage": _safe_ratio(len(gold) - unknown_predictions, len(gold)),
        "per_role": per_role,
        "confusion": confusion,
    }


def _pilot_policy_settings(
    policy: JsonObject,
    *,
    items_path: Path,
    labels_path: Path,
) -> JsonObject:
    pilot = policy.get("pilot")
    runtime_gate = policy.get("runtime_gate")
    selection_rule = policy.get("selection_rule")
    if not all(
        isinstance(section, dict)
        for section in (pilot, runtime_gate, selection_rule)
    ):
        raise BenchmarkError(
            "pilot policy must contain pilot, runtime_gate, and selection_rule objects"
        )

    expected_contract = {
        "items": 33,
        "roles": len(ROLE_VALUES),
        "items_per_role": 3,
    }
    for field, expected in expected_contract.items():
        if pilot.get(field) != expected:
            raise BenchmarkError(
                f"pilot policy {field!r} must be frozen at {expected}, "
                f"not {pilot.get(field)!r}"
            )

    expected_hashes = {
        "items_sha256": (pilot.get("items_sha256"), items_path),
        "labels_sha256": (pilot.get("labels_sha256"), labels_path),
    }
    for field, (expected, path) in expected_hashes.items():
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise BenchmarkError(f"pilot policy {field!r} must be a SHA-256 digest")
        actual = sha256_file(path)
        if actual != expected:
            raise BenchmarkError(
                f"{field.removesuffix('_sha256')} does not match frozen pilot policy hash"
            )

    required_ok = runtime_gate.get("required_ok_predictions")
    failures_allowed = runtime_gate.get("invalid_or_error_predictions_allowed")
    if required_ok != 33 or failures_allowed != 0:
        raise BenchmarkError("pilot runtime gate must be frozen at 33 ok and 0 failures")

    minimum_advantage = selection_rule.get("minimum_total_correct_advantage")
    maximum_roles_worse = selection_rule.get(
        "maximum_roles_worse_than_incumbent"
    )
    if minimum_advantage != 4 or maximum_roles_worse != 2:
        raise BenchmarkError(
            "pilot switch thresholds must be frozen at +4 correct and at most 2 worse roles"
        )
    tie_order = selection_rule.get("known_complete_artifact_size_order")
    if (
        not isinstance(tie_order, list)
        or not tie_order
        or not all(isinstance(name, str) and name for name in tie_order)
        or len(tie_order) != len(set(tie_order))
    ):
        raise BenchmarkError(
            "pilot policy must contain a unique known artifact-size tie order"
        )
    if selection_rule.get("unknown_artifact_tie") != "retain-incumbent":
        raise BenchmarkError(
            "pilot policy must retain the incumbent for an unknown artifact-size tie"
        )
    return {
        "items": 33,
        "items_per_role": 3,
        "required_statuses": {"ok": 33, "invalid": 0, "error": 0},
        "minimum_total_correct_advantage": 4,
        "maximum_roles_worse_than_incumbent": 2,
        "known_artifact_tie_order": list(tie_order),
    }


def _load_pilot_gold(
    items_path: Path,
    labels_path: Path,
    policy: JsonObject,
) -> tuple[dict[str, JsonObject], dict[str, str]]:
    items = load_jsonl(items_path)
    if len(items) != 33:
        raise BenchmarkError(f"pilot items must contain exactly 33 rows, found {len(items)}")
    for offset, item in enumerate(items, start=1):
        _validate_pilot_item_shape(item, offset)
    item_by_id = _index_unique(items, "id", "pilot items")

    labels = load_jsonl(labels_path)
    for offset, label in enumerate(labels, start=1):
        if set(label) != {"id", "role"}:
            raise BenchmarkError(
                f"pilot label row {offset} must contain exactly id and role"
            )
    label_by_id = _index_unique(labels, "id", "pilot labels")
    if set(label_by_id) != set(item_by_id):
        missing = sorted(set(item_by_id) - set(label_by_id))
        extra = sorted(set(label_by_id) - set(item_by_id))
        raise BenchmarkError(
            "pilot item and label ids differ; "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )

    gold = {item_id: label_by_id[item_id].get("role") for item_id in item_by_id}
    if any(role not in ROLE_SET for role in gold.values()):
        raise BenchmarkError("pilot labels contain a role outside the taxonomy")
    support = collections.Counter(gold.values())
    expected_support = {role: policy["items_per_role"] for role in ROLE_VALUES}
    actual_support = {role: support[role] for role in ROLE_VALUES}
    if actual_support != expected_support:
        raise BenchmarkError(
            "pilot labels must contain every role exactly 3 times; "
            f"found {actual_support}"
        )
    return item_by_id, gold


def _parse_candidate_specs(specs: Sequence[str]) -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    for spec in specs:
        name, separator, raw_path = spec.partition("=")
        name = name.strip()
        raw_path = raw_path.strip()
        if not separator or not name or not raw_path:
            raise BenchmarkError(
                f"candidate {spec!r} must use the non-empty NAME=PATH form"
            )
        if name in candidates:
            raise BenchmarkError(f"duplicate pilot candidate name {name!r}")
        candidates[name] = Path(raw_path)
    if not candidates:
        raise BenchmarkError("at least one pilot candidate is required")
    return candidates


def build_pilot_selection_report(
    *,
    gold: Mapping[str, str],
    predictions: Mapping[str, Mapping[str, JsonObject]],
    incumbent: str,
    settings: Mapping[str, Any],
) -> JsonObject:
    """Score a balanced selection pilot without claiming role recoverability."""
    if incumbent not in predictions:
        raise BenchmarkError(f"incumbent {incumbent!r} is not one of the candidates")
    if set(gold) == set() or len(gold) != settings["items"]:
        raise BenchmarkError("pilot gold must contain the frozen 33 ids")

    required_statuses = settings["required_statuses"]
    candidate_reports: dict[str, JsonObject] = {}
    for name in sorted(predictions):
        rows = predictions[name]
        if set(rows) != set(gold):
            raise BenchmarkError(f"candidate {name!r} does not predict every pilot id")
        per_role_correct = {role: 0 for role in ROLE_VALUES}
        for item_id, gold_role in gold.items():
            prediction = rows[item_id]
            if prediction["status"] == "ok" and prediction["role"] == gold_role:
                per_role_correct[gold_role] += 1
        statuses_counter = collections.Counter(row["status"] for row in rows.values())
        statuses = {
            status: statuses_counter[status] for status in ("ok", "invalid", "error")
        }
        exact_correct = sum(per_role_correct.values())
        candidate_reports[name] = {
            "exact_correct": exact_correct,
            "per_role_correct": per_role_correct,
            "roles_three_of_three": sum(
                count == settings["items_per_role"]
                for count in per_role_correct.values()
            ),
            "roles_at_least_two_of_three": sum(
                count >= 2 for count in per_role_correct.values()
            ),
            "statuses": statuses,
            "runtime_gate_passed": statuses == required_statuses,
        }

    incumbent_report = candidate_reports[incumbent]
    qualifying: list[str] = []
    for name, candidate in candidate_reports.items():
        if name == incumbent:
            candidate["switch_conditions"] = None
            candidate["qualifies_to_replace_incumbent"] = False
            continue
        advantage = candidate["exact_correct"] - incumbent_report["exact_correct"]
        worse_roles = [
            role
            for role in ROLE_VALUES
            if candidate["per_role_correct"][role]
            < incumbent_report["per_role_correct"][role]
        ]
        conditions = {
            "total_correct_advantage": {
                "actual": advantage,
                "minimum": settings["minimum_total_correct_advantage"],
                "passed": advantage >= settings["minimum_total_correct_advantage"],
            },
            "roles_worse_than_incumbent": {
                "actual": len(worse_roles),
                "roles": worse_roles,
                "maximum": settings["maximum_roles_worse_than_incumbent"],
                "passed": len(worse_roles)
                <= settings["maximum_roles_worse_than_incumbent"],
            },
            "runtime_gate": {
                "actual": candidate["statuses"],
                "required": required_statuses,
                "passed": candidate["runtime_gate_passed"],
            },
        }
        qualifies = all(condition["passed"] for condition in conditions.values())
        candidate["switch_conditions"] = conditions
        candidate["qualifies_to_replace_incumbent"] = qualifies
        if qualifies:
            qualifying.append(name)

    selected = incumbent
    reason = "no challenger cleared every frozen switch condition"
    unresolved_tie: list[str] = []
    if qualifying:
        ranked_metrics = {
            name: (
                candidate_reports[name]["exact_correct"]
                - incumbent_report["exact_correct"],
                candidate_reports[name]["roles_three_of_three"],
                candidate_reports[name]["roles_at_least_two_of_three"],
            )
            for name in qualifying
        }
        best_metrics = max(ranked_metrics.values())
        leaders = sorted(
            name for name, metrics in ranked_metrics.items() if metrics == best_metrics
        )
        if len(leaders) == 1:
            selected = leaders[0]
            reason = "challenger cleared every switch condition and won the frozen ranking"
        else:
            tie_order = settings["known_artifact_tie_order"]
            tie_position = {name: index for index, name in enumerate(tie_order)}
            if all(name in tie_position for name in leaders):
                selected = min(leaders, key=tie_position.__getitem__)
                reason = "tied challengers resolved by frozen complete-artifact size order"
            else:
                unresolved_tie = leaders
                reason = (
                    "otherwise tied challenger has no frozen artifact-size order; "
                    "incumbent retained"
                )

    return {
        "schema": 1,
        "purpose": "local-model-selection-pilot",
        "interpretation": (
            "This pilot selects one local arm. It is not evidence that any role "
            "is recoverable; only the frozen 200-unit evaluation answers that question."
        ),
        "pilot": {
            "items": settings["items"],
            "roles": len(ROLE_VALUES),
            "items_per_role": settings["items_per_role"],
        },
        "incumbent": incumbent,
        "candidates": candidate_reports,
        "selection": {
            "selected_candidate": selected,
            "retained_incumbent": selected == incumbent,
            "qualifying_challengers": sorted(qualifying),
            "unresolved_artifact_size_tie": unresolved_tie,
            "reason": reason,
        },
        "frozen_rule": {
            "minimum_total_correct_advantage": settings[
                "minimum_total_correct_advantage"
            ],
            "maximum_roles_worse_than_incumbent": settings[
                "maximum_roles_worse_than_incumbent"
            ],
            "required_statuses": required_statuses,
            "known_artifact_tie_order": settings["known_artifact_tie_order"],
            "unknown_artifact_tie": "retain-incumbent",
        },
    }


def render_pilot_selection_markdown(report: Mapping[str, Any]) -> str:
    incumbent = report["incumbent"]
    candidate_names = [incumbent] + sorted(
        name for name in report["candidates"] if name != incumbent
    )
    selected = report["selection"]["selected_candidate"]
    lines = [
        "# Local role-model selection pilot",
        "",
        "This pilot selects one local arm. **It is not role-recoverability evidence.**",
        "",
        f"Selected candidate: **{selected}**.",
        "",
        f"Decision: {report['selection']['reason']}.",
        "",
        "## Candidate summary",
        "",
        "| Candidate | Exact correct | 3/3 roles | >=2/3 roles | OK | Invalid | Error | Runtime gate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for name in candidate_names:
        candidate = report["candidates"][name]
        statuses = candidate["statuses"]
        lines.append(
            f"| {name} | {candidate['exact_correct']}/33 | "
            f"{candidate['roles_three_of_three']} | "
            f"{candidate['roles_at_least_two_of_three']} | "
            f"{statuses['ok']} | {statuses['invalid']} | {statuses['error']} | "
            f"{'pass' if candidate['runtime_gate_passed'] else 'fail'} |"
        )

    lines.extend(["", "## Per-role exact-correct counts", ""])
    lines.append("| Role | " + " | ".join(candidate_names) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in candidate_names) + " |")
    for role in ROLE_VALUES:
        values = [
            f"{report['candidates'][name]['per_role_correct'][role]}/3"
            for name in candidate_names
        ]
        lines.append(f"| {role} | " + " | ".join(values) + " |")

    lines.extend(["", "## Switch-condition evaluation", ""])
    lines.append(
        "| Challenger | Correct advantage (>=4) | Worse roles (<=2) | Runtime 33/0/0 | Qualifies |"
    )
    lines.append("| --- | ---: | ---: | --- | --- |")
    for name in candidate_names:
        if name == incumbent:
            continue
        candidate = report["candidates"][name]
        conditions = candidate["switch_conditions"]
        lines.append(
            f"| {name} | {conditions['total_correct_advantage']['actual']} | "
            f"{conditions['roles_worse_than_incumbent']['actual']} | "
            f"{'pass' if conditions['runtime_gate']['passed'] else 'fail'} | "
            f"{'yes' if candidate['qualifies_to_replace_incumbent'] else 'no'} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def score_pilot_from_files(
    *,
    items_path: Path = DEFAULT_PILOT_ITEMS,
    labels_path: Path = DEFAULT_PILOT_LABELS,
    candidate_paths: Mapping[str, Path],
    incumbent: str,
    policy_path: Path = DEFAULT_PILOT_POLICY,
    json_output: Path = DEFAULT_PILOT_JSON,
    markdown_output: Path = DEFAULT_PILOT_MARKDOWN,
) -> JsonObject:
    policy = load_json(policy_path)
    settings = _pilot_policy_settings(
        policy, items_path=items_path, labels_path=labels_path
    )
    item_by_id, gold = _load_pilot_gold(items_path, labels_path, settings)
    predictions = {
        name: _load_predictions(path, set(item_by_id))
        for name, path in sorted(candidate_paths.items())
    }
    report = build_pilot_selection_report(
        gold=gold,
        predictions=predictions,
        incumbent=incumbent,
        settings=settings,
    )
    report["hashes"] = {
        "items_sha256": sha256_file(items_path),
        "labels_sha256": sha256_file(labels_path),
        "policy_sha256": sha256_file(policy_path),
        "predictions": {
            name: sha256_file(path) for name, path in sorted(candidate_paths.items())
        },
    }
    write_json(json_output, report)
    _atomic_write_text(markdown_output, render_pilot_selection_markdown(report))
    return report


def build_report(
    items: Sequence[JsonObject],
    labels: Sequence[JsonObject],
    predictions: Mapping[str, Mapping[str, JsonObject]],
    *,
    policy: JsonObject,
) -> JsonObject:
    recoverability_policy = _recoverability_policy(policy)
    item_by_id = _index_unique(items, "id", "items")
    label_by_id = _index_unique(labels, "id", "labels")
    if set(item_by_id) != set(label_by_id):
        raise BenchmarkError("items and labels must have identical ids")
    gold = {item_id: label_by_id[item_id].get("role") for item_id in item_by_id}
    if any(role not in ROLE_SET for role in gold.values()):
        raise BenchmarkError("labels contain a role outside the taxonomy")

    formats = sorted({item["format"] for item in items})
    arms: dict[str, Any] = {}
    for arm, rows in predictions.items():
        if set(rows) != set(item_by_id):
            raise BenchmarkError(f"arm {arm!r} does not predict every item exactly once")
        predicted = {item_id: rows[item_id]["role"] for item_id in item_by_id}
        arm_report = score_predictions(gold, predicted)
        arm_report["statuses"] = dict(
            sorted(collections.Counter(row["status"] for row in rows.values()).items())
        )
        per_format: dict[str, Any] = {}
        for format_name in formats:
            ids = {
                item_id
                for item_id, item in item_by_id.items()
                if item["format"] == format_name
            }
            per_format[format_name] = score_predictions(
                {item_id: gold[item_id] for item_id in ids},
                {item_id: predicted[item_id] for item_id in ids},
            )
        arm_report["per_format"] = per_format
        arms[arm] = arm_report

    role_source_groups = {
        role: len(
            {
                item_by_id[item_id]["source_group"]
                for item_id, gold_role in gold.items()
                if gold_role == role
            }
        )
        for role in ROLE_VALUES
    }

    passing_roles_by_arm: dict[str, list[str]] = {}
    for arm, arm_report in arms.items():
        passing_roles: list[str] = []
        for role in ROLE_VALUES:
            metric = arm_report["per_role"][role]
            checks = {
                "support": metric["support"]
                >= recoverability_policy["minimum_support"],
                "source_groups": role_source_groups[role]
                >= recoverability_policy["minimum_source_groups"],
                "precision": metric["precision"]
                >= recoverability_policy["minimum_precision"],
                "recall": metric["recall"]
                >= recoverability_policy["minimum_recall"],
            }
            metric["recoverability_checks"] = checks
            metric["recoverable"] = all(checks.values())
            if metric["recoverable"]:
                passing_roles.append(role)
        passing_roles_by_arm[arm] = passing_roles

    return {
        "schema": 2,
        "generated_at": _utc_now(),
        "recoverability": {
            "policy": recoverability_policy,
            "passing_roles_by_arm": passing_roles_by_arm,
        },
        "corpus": {
            "items": len(items),
            "formats": dict(
                sorted(collections.Counter(item["format"] for item in items).items())
            ),
            "gold_support": dict(sorted(collections.Counter(gold.values()).items())),
            "source_groups_by_role": role_source_groups,
        },
        "arms": arms,
    }


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def render_markdown_report(report: Mapping[str, Any]) -> str:
    arms = list(report["arms"])
    recoverability = report["recoverability"]
    gate = recoverability["policy"]
    lines = [
        "# Stage 2 role-tagging evaluation",
        "",
        "The gate is role-by-role recoverability. Aggregate accuracy is intentionally omitted.",
        "",
        f"Items: {report['corpus']['items']}. Formats: "
        + ", ".join(
            f"{name}={count}" for name, count in report["corpus"]["formats"].items()
        )
        + ".",
        "",
        "Gate: "
        f"support >= {gate['minimum_support']}; "
        f"source groups >= {gate['minimum_source_groups']}; "
        f"precision >= {_fmt(gate['minimum_precision'])}; "
        f"recall >= {_fmt(gate['minimum_recall'])}.",
        "",
        "Passing roles by arm:",
        "",
    ]
    for arm in arms:
        passing = recoverability["passing_roles_by_arm"][arm]
        lines.append(f"- `{arm}`: " + (", ".join(passing) if passing else "none"))
    lines.extend(["", "## Per-role scores", ""])
    header = ["Role", "Support", "Source groups"]
    for arm in arms:
        header.extend(
            [f"{arm} P", f"{arm} R", f"{arm} F1", f"{arm} abstain", f"{arm} gate"]
        )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] + ["---:"] * (len(header) - 1)) + " |")
    for role in ROLE_VALUES:
        first_arm = report["arms"][arms[0]]["per_role"][role]
        row = [
            role,
            str(first_arm["support"]),
            str(report["corpus"]["source_groups_by_role"][role]),
        ]
        for arm in arms:
            metric = report["arms"][arm]["per_role"][role]
            row.extend(
                [
                    _fmt(metric["precision"]),
                    _fmt(metric["recall"]),
                    _fmt(metric["f1"]),
                    str(metric["abstained"]),
                    "PASS" if metric["recoverable"] else "FAIL",
                ]
            )
        lines.append("| " + " | ".join(row) + " |")

    lines.extend(["", "## Arm health", ""])
    lines.append("| Arm | Coverage | OK | Invalid | Error |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for arm in arms:
        arm_data = report["arms"][arm]
        statuses = arm_data["statuses"]
        lines.append(
            f"| {arm} | {_fmt(arm_data['coverage'])} | {statuses.get('ok', 0)} | "
            f"{statuses.get('invalid', 0)} | {statuses.get('error', 0)} |"
        )

    lines.extend(["", "## Per-format scores", ""])
    for format_name in report["corpus"]["formats"]:
        lines.extend([f"### {format_name}", ""])
        format_header = ["Role", "Support"]
        for arm in arms:
            format_header.extend([f"{arm} P", f"{arm} R", f"{arm} F1"])
        lines.append("| " + " | ".join(format_header) + " |")
        lines.append(
            "| " + " | ".join(["---"] + ["---:"] * (len(format_header) - 1)) + " |"
        )
        for role in ROLE_VALUES:
            support = report["arms"][arms[0]]["per_format"][format_name]["per_role"][role][
                "support"
            ]
            row = [role, str(support)]
            for arm in arms:
                metric = report["arms"][arm]["per_format"][format_name]["per_role"][role]
                row.extend(
                    [_fmt(metric["precision"]), _fmt(metric["recall"]), _fmt(metric["f1"])]
                )
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    lines.extend(["## Confusion matrices", ""])
    for arm in arms:
        lines.extend([f"### {arm}", ""])
        lines.append("Gold rows; prediction columns.")
        lines.append("")
        lines.append("| Gold \\ Pred | " + " | ".join(ROLE_VALUES) + " |")
        lines.append("| --- | " + " | ".join("---:" for _ in ROLE_VALUES) + " |")
        confusion = report["arms"][arm]["confusion"]
        for actual in ROLE_VALUES:
            lines.append(
                f"| {actual} | "
                + " | ".join(str(confusion[actual][guess]) for guess in ROLE_VALUES)
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_from_files(
    *,
    items_path: Path = DEFAULT_ITEMS,
    labels_path: Path = DEFAULT_LABELS,
    rules_path: Path = DEFAULT_PREDICTIONS / "rules.jsonl",
    local_path: Path = DEFAULT_PREDICTIONS / "local.jsonl",
    frontier_path: Path = DEFAULT_PREDICTIONS / "frontier.jsonl",
    ornith_path: Path | None = None,
    json_output: Path = HERE / "report.json",
    markdown_output: Path = HERE / "report.md",
    policy_path: Path = DEFAULT_POLICY,
    prompt_path: Path = DEFAULT_PROMPT,
) -> JsonObject:
    policy = load_json(policy_path)
    items = load_jsonl(items_path)
    labels = load_jsonl(labels_path)
    item_ids = set(_index_unique(items, "id", "items"))
    prediction_paths = {
        "rules": rules_path,
        "local": local_path,
        "frontier": frontier_path,
    }
    if ornith_path is not None:
        prediction_paths["exploratory-ornith-35b-a3b"] = ornith_path
    predictions = {
        arm: _load_predictions(path, item_ids) for arm, path in prediction_paths.items()
    }
    report = build_report(items, labels, predictions, policy=policy)
    report["hashes"] = {
        "items_sha256": sha256_file(items_path),
        "labels_sha256": sha256_file(labels_path),
        "policy_sha256": sha256_file(policy_path),
        "prompt_sha256": sha256_file(prompt_path),
        "predictions": {
            arm: sha256_file(path) for arm, path in prediction_paths.items()
        },
    }
    report["evaluator_commit"] = _git_commit()
    write_json(json_output, report)
    _atomic_write_text(markdown_output, render_markdown_report(report))
    return report


def _resolve_model_config(args: argparse.Namespace) -> tuple[str, str, str | None, str | None]:
    configured: JsonObject = {}
    if args.config:
        config = load_json(args.config)
        if "api_key" in config:
            raise BenchmarkError("model config must reference an api_key_env, never contain a key")
        arms = config.get("arms", config)
        if not isinstance(arms, dict) or not isinstance(arms.get(args.arm, {}), dict):
            raise BenchmarkError(f"config has no object for arm {args.arm!r}")
        configured = arms.get(args.arm, {})
        if "api_key" in configured:
            raise BenchmarkError("model config must reference an api_key_env, never contain a key")

    prefix = f"AUTOTLDR_ROLE_{args.arm.upper().replace('-', '_')}"
    base_url = args.base_url or configured.get("base_url") or os.environ.get(f"{prefix}_BASE_URL")
    model = args.model or configured.get("model") or os.environ.get(f"{prefix}_MODEL")
    api_key_env = args.api_key_env or configured.get("api_key_env")

    if not base_url and args.arm in LOCAL_MODEL_ARMS and os.environ.get("OLLAMA_HOST"):
        base_url = os.environ["OLLAMA_HOST"].rstrip("/") + "/v1"
    if not base_url and args.arm == "frontier":
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key_env and args.arm == "frontier":
        api_key_env = "OPENAI_API_KEY"

    direct_key_name = f"{prefix}_API_KEY"
    api_key = os.environ.get(api_key_env) if api_key_env else os.environ.get(direct_key_name)
    if not isinstance(base_url, str) or not base_url:
        raise BenchmarkError(f"no base URL configured for arm {args.arm!r}")
    if not isinstance(model, str) or not model:
        raise BenchmarkError(f"no model configured for arm {args.arm!r}")
    return base_url, model, api_key, api_key_env or (direct_key_name if api_key else None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate corpus and labels")
    validate_parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS)
    validate_parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    validate_parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    validate_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)

    rules_parser = subparsers.add_parser("run-rules", help="export frozen extractor roles")
    rules_parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS)
    rules_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    rules_parser.add_argument(
        "--output", type=Path, default=DEFAULT_PREDICTIONS / "rules.jsonl"
    )
    rules_parser.add_argument("--manifest", type=Path)

    model_parser = subparsers.add_parser(
        "run-model", help="run an OpenAI-compatible role-classification arm"
    )
    model_parser.add_argument(
        "--arm", choices=("local", "ornith", "frontier"), required=True
    )
    model_parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS)
    model_parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    model_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    model_parser.add_argument("--config", type=Path)
    model_parser.add_argument("--base-url")
    model_parser.add_argument("--model")
    model_parser.add_argument("--api-key-env")
    model_parser.add_argument("--timeout", type=float, default=60.0)
    model_parser.add_argument("--max-tokens", type=int)
    model_parser.add_argument("--concurrency", type=int, default=4)
    model_parser.add_argument("--output", type=Path)
    model_parser.add_argument("--manifest", type=Path)
    model_parser.add_argument(
        "--pilot",
        action="store_true",
        help="accept minimal id-plus-five-field pilot items instead of the frozen 200",
    )

    pilot_score_parser = subparsers.add_parser(
        "score-pilot", help="score the frozen local-model selection pilot"
    )
    pilot_score_parser.add_argument("--items", type=Path, default=DEFAULT_PILOT_ITEMS)
    pilot_score_parser.add_argument("--labels", type=Path, default=DEFAULT_PILOT_LABELS)
    pilot_score_parser.add_argument("--policy", type=Path, default=DEFAULT_PILOT_POLICY)
    pilot_score_parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="candidate name and prediction JSONL; repeat for every candidate",
    )
    pilot_score_parser.add_argument("--incumbent", required=True)
    pilot_score_parser.add_argument(
        "--json-output", type=Path, default=DEFAULT_PILOT_JSON
    )
    pilot_score_parser.add_argument(
        "--markdown-output", type=Path, default=DEFAULT_PILOT_MARKDOWN
    )

    report_parser = subparsers.add_parser("report", help="score all three arms")
    report_parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS)
    report_parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    report_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    report_parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    report_parser.add_argument(
        "--rules", type=Path, default=DEFAULT_PREDICTIONS / "rules.jsonl"
    )
    report_parser.add_argument(
        "--local", type=Path, default=DEFAULT_PREDICTIONS / "local.jsonl"
    )
    report_parser.add_argument(
        "--frontier", type=Path, default=DEFAULT_PREDICTIONS / "frontier.jsonl"
    )
    report_parser.add_argument(
        "--ornith",
        type=Path,
        help="optional exploratory local-model predictions (outside the required three-arm gate)",
    )
    report_parser.add_argument("--json-output", type=Path, default=HERE / "report.json")
    report_parser.add_argument(
        "--markdown-output", type=Path, default=HERE / "report.md"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_corpus(args.items, args.labels, args.sources, args.policy)
        elif args.command == "run-rules":
            _, result = run_rules(
                args.items, args.output, args.policy, args.manifest, enforce_corpus=True
            )
        elif args.command == "run-model":
            base_url, model, api_key, api_key_env = _resolve_model_config(args)
            output = args.output or DEFAULT_PREDICTIONS / f"{args.arm}.jsonl"
            _, result = run_model(
                arm=args.arm,
                items_path=args.items,
                output_path=output,
                base_url=base_url,
                model=model,
                api_key=api_key,
                prompt_path=args.prompt,
                policy_path=args.policy,
                manifest_path=args.manifest,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                concurrency=args.concurrency,
                enforce_corpus=not args.pilot,
            )
            result["api_key_env"] = api_key_env
        elif args.command == "score-pilot":
            result = score_pilot_from_files(
                items_path=args.items,
                labels_path=args.labels,
                candidate_paths=_parse_candidate_specs(args.candidate),
                incumbent=args.incumbent,
                policy_path=args.policy,
                json_output=args.json_output,
                markdown_output=args.markdown_output,
            )
        elif args.command == "report":
            result = report_from_files(
                items_path=args.items,
                labels_path=args.labels,
                rules_path=args.rules,
                local_path=args.local,
                frontier_path=args.frontier,
                ornith_path=args.ornith,
                json_output=args.json_output,
                markdown_output=args.markdown_output,
                policy_path=args.policy,
                prompt_path=args.prompt,
            )
        else:  # pragma: no cover - argparse makes this unreachable
            raise BenchmarkError(f"unknown command {args.command!r}")
    except BenchmarkError as exc:
        print(f"role benchmark: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
