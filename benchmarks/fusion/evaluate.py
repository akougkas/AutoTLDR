#!/usr/bin/env python3
"""Validate, run, and report the frozen Stage 4 fusion benchmark.

``run`` is deliberately label-isolated: it accepts no labels argument and
never loads a labels file.  It serializes :class:`MatchCandidate.details` and
typed unresolved records directly; it never parses ``Relation.evidence``.
Gold labels enter only through ``validate-labels`` and ``report``.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import importlib.util
import itertools
import json
import math
import os
import re
import shutil
import tempfile
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urldefrag


HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "policy.json"
FREEZE_PATH = HERE / "freeze.json"
REVIEW_CLEARANCE_PATH = HERE / "reviews" / "clearance.json"
REQUIRED_BLIND_REVIEWS = (
    "reviews/blind_a.md",
    "reviews/blind_b.md",
)
PREDICTIONS = HERE / "predictions"
REPORT_JSON = HERE / "report.json"
REPORT_MD = HERE / "report.md"
EVALUATOR_VERSION = "stage4-fusion-evaluator-v2"
TASKS = ("literal", "identifier", "structural", "contradiction", "orphan", "unresolved")
SIGNAL_TO_TASK = {
    "literal-v1": "literal",
    "identifier-v1": "identifier",
    "structural-v1": "structural",
    "contradiction-v1": "contradiction",
}

_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_LOWER = re.compile(r"([a-z0-9])([A-Z])")
_ALPHA_DIGIT = re.compile(r"([A-Za-z])([0-9])|([0-9])([A-Za-z])")
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_ACTION_PREFIXES = frozenset(
    {
        "get",
        "set",
        "make",
        "build",
        "create",
        "compute",
        "calculate",
        "measure",
        "load",
        "save",
        "write",
    }
)
_MEASUREMENT_SUFFIXES = frozenset(
    {"ms", "sec", "secs", "mbps", "gbps", "hz", "kb", "mb", "gb"}
)
_IDENTIFIER_ALIASES = {"tput": "throughput"}
_PATH_KINDS = frozenset(
    {"path", "include", "literalinclude", "image", "figure", "explicit-target"}
)
_TABLE_SOURCE_KINDS = frozenset({"csv", "tsv", "xlsx"})
_RECORD_SOURCE_KINDS = frozenset(
    {"json", "jsonl", "notebook", "toml", "xml", "yaml"}
)
_UNCLASSIFIED_SUBTYPE = "__unclassified__"


class EvaluationError(ValueError):
    """A label, prediction, hash, or score artifact is invalid."""


def _load_build_module():
    path = HERE / "build_corpus.py"
    spec = importlib.util.spec_from_file_location("autotldr_fusion_build", path)
    if spec is None or spec.loader is None:
        raise EvaluationError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_json_bytes(dict(row)) for row in rows)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(payload)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return _sha256(path.read_bytes())
    except OSError as exc:
        raise EvaluationError(f"cannot hash {path}: {exc}") from exc


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"{path} must contain one object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvaluationError(f"cannot read {path}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"{path}:{line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise EvaluationError(f"{path}:{line_no}: row must be an object")
        rows.append(row)
    return rows


def _collections(split: str) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    rows = _load_jsonl(HERE / split / "collections.jsonl")
    index: dict[str, set[str]] = {}
    for row in rows:
        collection_id = row.get("id")
        sources = row.get("sources")
        if not isinstance(collection_id, str) or not isinstance(sources, list):
            raise EvaluationError(f"invalid {split} collection row")
        index[collection_id] = set(sources)
    return rows, index


def _normalize_identifier(value: str, *, conceptual: bool) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", value)
    if normalized.isascii():
        split = _CAMEL_ACRONYM.sub(r"\1 \2", normalized)
        split = _CAMEL_LOWER.sub(r"\1 \2", split)
        split = _ALPHA_DIGIT.sub(
            lambda match: " ".join(part for part in match.groups() if part), split
        )
        tokens = [token.casefold() for token in _TOKEN.findall(split)]
    else:
        tokens = [token for token in re.findall(r"[^\W_]+", normalized, re.UNICODE)]
    if conceptual:
        tokens = [_IDENTIFIER_ALIASES.get(token, token) for token in tokens]
        if len(tokens) > 1 and tokens[0] in _ACTION_PREFIXES:
            tokens = tokens[1:]
        if len(tokens) > 1 and tokens[-1] in _MEASUREMENT_SUFFIXES:
            tokens = tokens[:-1]
    return tuple(tokens)


def _canonical_number(value: str) -> str:
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise EvaluationError(f"invalid numeric label value {value!r}") from exc
    if not number.is_finite():
        raise EvaluationError(f"non-finite numeric label value {value!r}")
    if number == 0:
        number = Decimal(0)
    return format(number.normalize(), "f")


def _normalize_target(value: str) -> str:
    target, _fragment = urldefrag(value.strip())
    if "://" in target:
        return target
    normalized = os.path.normpath(target).replace(os.sep, "/")
    return normalized.removeprefix("./")


def _kind(value: str) -> str:
    lowered = value.casefold()
    return "path" if lowered in _PATH_KINDS else lowered


def _pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def _canonical_field(value: str) -> tuple[str, ...]:
    """Return the delimiter-independent canonical form of one schema field."""

    return _normalize_identifier(value, conceptual=False)


def _label_expansion(label: Mapping[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    task = str(label["task"])
    collection = str(label["collection"])
    sources = [str(item) for item in label["sources"]]
    fact = label["fact"]
    if not isinstance(fact, dict):
        raise EvaluationError(f"label {label['id']} fact must be an object")
    subtype = str(label.get("subtype") or "unspecified")
    if task == "literal":
        if len(sources) != 2:
            # Negative unresolved/ambiguous references need not name a target source.
            return []
        key = (
            task,
            collection,
            sources[0],
            sources[1],
            _kind(str(fact.get("kind", "path"))),
            _normalize_target(str(fact["target"])),
        )
        return [(key, subtype)]
    if task == "identifier":
        canonical = _normalize_identifier(str(fact["canonical"]), conceptual=True)
        prose = {str(item) for item in fact.get("prose_sources", [])}
        expanded = []
        for left, right in itertools.combinations(sorted(sources), 2):
            if left in prose and right in prose:
                pair_subtype = "prose-prose"
            elif left in prose or right in prose:
                pair_subtype = "explicit-prose-native"
            else:
                pair_subtype = "native-native"
            expanded.append(((task, collection, canonical, left, right), pair_subtype))
        return expanded
    if task == "structural":
        if len(sources) != 2:
            raise EvaluationError(f"structural label {label['id']} needs two sources")
        fields = tuple(sorted(_canonical_field(str(field)) for field in fact.get("fields", [])))
        left, right = _pair(*sources)
        return [((task, collection, left, right, fields), subtype)]
    if task == "contradiction":
        if len(sources) != 2:
            raise EvaluationError(f"contradiction label {label['id']} needs two sources")
        if not {"key", "left", "right"} <= fact.keys():
            if label.get("positive") is False:
                return []
            raise EvaluationError(
                f"positive contradiction label {label['id']} lacks key/left/right"
            )
        key = _normalize_identifier(str(fact["key"]), conceptual=False)
        values = []
        for side in (fact["left"], fact["right"]):
            value_type = str(side["type"])
            raw = str(side["value"])
            canonical = _canonical_number(raw) if value_type in {"integer", "number"} else raw
            values.append(("number" if value_type in {"integer", "number"} else value_type, canonical))
        left, right = _pair(*sources)
        return [((task, collection, left, right, key, tuple(sorted(values))), subtype)]
    if task == "orphan":
        if len(sources) != 1:
            raise EvaluationError(f"orphan label {label['id']} needs one source")
        return [((task, collection, sources[0]), subtype)]
    if task == "unresolved":
        if len(sources) != 1:
            # Resolved-local negative records may carry the target source too.
            if label.get("positive") is False:
                sources = sources[:1]
            else:
                raise EvaluationError(f"unresolved label {label['id']} needs one source")
        return [
            (
                (
                    task,
                    collection,
                    sources[0],
                    _kind(str(fact.get("kind", "path"))),
                    _normalize_target(str(fact["target"])),
                ),
                subtype,
            )
        ]
    raise EvaluationError(f"label {label['id']} has unknown task {task!r}")


def _identifier_provenance_error(label: Mapping[str, Any]) -> str | None:
    if label.get("task") != "identifier" or label.get("positive") is not True:
        return None
    fact = label.get("fact")
    sources = label.get("sources")
    if not isinstance(fact, dict) or not isinstance(sources, list):
        return "invalid identifier fact or sources"
    prose_sources = fact.get("prose_sources")
    if not isinstance(prose_sources, list):
        return "fact.prose_sources must be an explicit list"
    if not all(isinstance(item, str) and item in sources for item in prose_sources):
        return "fact.prose_sources must contain only declared source names"
    if len(prose_sources) != len(set(prose_sources)):
        return "fact.prose_sources must be unique"
    return None


def validate_labels(split: str = "scored") -> dict[str, Any]:
    policy = _load_json(POLICY_PATH)
    labels_path = HERE / split / "labels.jsonl"
    labels = _load_jsonl(labels_path)
    _rows, collections_index = _collections(split)
    ids: set[str] = set()
    positive_keys: dict[tuple[Any, ...], tuple[str, str]] = {}
    negative_keys: set[tuple[Any, ...]] = set()
    hard_classes: dict[str, set[str]] = collections.defaultdict(set)
    support: collections.Counter[str] = collections.Counter()
    groups: dict[str, set[str]] = collections.defaultdict(set)
    subtype_support: collections.Counter[tuple[str, str]] = collections.Counter()
    warnings: list[str] = []
    for offset, label in enumerate(labels, start=1):
        required = {"id", "collection", "task", "positive", "sources", "fact", "subtype"}
        missing = required - label.keys()
        if missing:
            raise EvaluationError(
                f"{labels_path}:{offset}: missing {', '.join(sorted(missing))}"
            )
        label_id = label["id"]
        collection_id = label["collection"]
        task = label["task"]
        if not isinstance(label_id, str) or not label_id or label_id in ids:
            raise EvaluationError(f"{labels_path}:{offset}: duplicate/invalid id {label_id!r}")
        ids.add(label_id)
        if collection_id not in collections_index:
            raise EvaluationError(f"label {label_id}: unknown collection {collection_id!r}")
        if task not in TASKS:
            raise EvaluationError(f"label {label_id}: unknown task {task!r}")
        if type(label["positive"]) is not bool:
            raise EvaluationError(f"label {label_id}: positive must be boolean")
        sources = label["sources"]
        if not isinstance(sources, list) or not sources or not all(
            isinstance(item, str) and item in collections_index[collection_id]
            for item in sources
        ):
            raise EvaluationError(f"label {label_id}: invalid source membership")
        provenance_error = _identifier_provenance_error(label)
        if provenance_error:
            message = f"label {label_id}: {provenance_error}"
            if split == "scored":
                raise EvaluationError(message)
            warnings.append(message)
        expanded = _label_expansion(label)
        if label["positive"]:
            if not expanded:
                raise EvaluationError(f"positive label {label_id} expands to no facts")
            for key, subtype in expanded:
                if key in positive_keys:
                    raise EvaluationError(
                        f"labels {positive_keys[key][0]} and {label_id} duplicate one positive fact"
                    )
                positive_keys[key] = (label_id, subtype)
                support[task] += 1
                groups[task].add(collection_id)
                subtype_support[(task, subtype)] += 1
        else:
            hard_class = label.get("hard_negative_class")
            if task != "orphan" and (not isinstance(hard_class, str) or not hard_class):
                raise EvaluationError(f"negative label {label_id}: missing hard_negative_class")
            if isinstance(hard_class, str):
                hard_classes[task].add(hard_class)
            negative_keys.update(key for key, _subtype in expanded)
    collision = set(positive_keys) & negative_keys
    if collision:
        raise EvaluationError(f"positive and negative truth collide: {next(iter(collision))!r}")

    if split == "scored":
        expected = policy["corpus"]["expected_positive_support"]
        if dict(sorted(support.items())) != dict(sorted(expected.items())):
            raise EvaluationError(
                f"positive support differs: expected {expected}, got {dict(support)}"
            )
        for task, gate in policy["gates"].items():
            if support[task] < gate["minimum_support"]:
                raise EvaluationError(f"{task}: support below preregistered minimum")
            if len(groups[task]) < gate["minimum_groups"]:
                raise EvaluationError(f"{task}: source groups below preregistered minimum")
        for task, required_classes in policy["hard_negative_classes"].items():
            missing = set(required_classes) - hard_classes[task]
            if missing:
                raise EvaluationError(
                    f"{task}: missing hard-negative classes {sorted(missing)}"
                )
        annotations = _load_jsonl(HERE / split / "annotations.jsonl")
        if not annotations or any(row.get("predictions_seen") is not False for row in annotations):
            raise EvaluationError("scored annotations must state predictions_seen=false")

    return {
        "status": "valid",
        "split": split,
        "label_rows": len(labels),
        "positive_support": dict(sorted(support.items())),
        "source_groups": {task: len(groups[task]) for task in TASKS},
        "subtype_support": {
            f"{task}:{subtype}": count
            for (task, subtype), count in sorted(subtype_support.items())
        },
        "hard_negative_classes": {
            task: sorted(hard_classes[task]) for task in sorted(hard_classes)
        },
        "labels_sha256": _file_sha256(labels_path),
        "warnings": warnings,
    }


def _source_map(extractions: Sequence[Any], relatives: Sequence[str]) -> dict[str, str]:
    if len(extractions) != len(relatives):
        raise EvaluationError("source map cardinality mismatch")
    return {extraction.source: relative for extraction, relative in zip(extractions, relatives, strict=True)}


def _replace_sources(value: Any, source_map: Mapping[str, str], ids: set[str]) -> Any:
    if isinstance(value, str):
        if value in ids:
            return value
        if value in source_map:
            return source_map[value]
        for source, relative in source_map.items():
            prefix = source + "#"
            if value.startswith(prefix):
                return relative + "#" + value[len(prefix):]
        return value
    if isinstance(value, dict):
        return {key: _replace_sources(item, source_map, ids) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_replace_sources(item, source_map, ids) for item in value]
    return value


def _candidate_row(candidate: Any, source_map: Mapping[str, str], ids: set[str]) -> dict[str, Any]:
    if candidate.src not in ids or candidate.dst not in ids:
        raise EvaluationError(
            f"candidate {candidate.signal} has dangling endpoint {candidate.src}->{candidate.dst}"
        )
    return {
        "signal": candidate.signal,
        "task": SIGNAL_TO_TASK[candidate.signal],
        "relation_kind": candidate.relation_kind,
        "src": candidate.src,
        "dst": candidate.dst,
        "src_source": source_map[candidate.src_source],
        "dst_source": source_map[candidate.dst_source],
        "src_origin": _replace_sources(candidate.src_origin, source_map, ids),
        "dst_origin": _replace_sources(candidate.dst_origin, source_map, ids),
        "confidence": candidate.confidence,
        "details": _replace_sources(candidate.details, source_map, ids),
    }


def _signal_payload(signals: Any, source_map: Mapping[str, str], ids: set[str]) -> dict[str, Any]:
    candidates = {
        "literal": [_candidate_row(item, source_map, ids) for item in signals.literal],
        "identifier": [_candidate_row(item, source_map, ids) for item in signals.identifier],
        "structural": [_candidate_row(item, source_map, ids) for item in signals.structural],
        "contradiction": [_candidate_row(item, source_map, ids) for item in signals.contradictions],
    }
    unresolved = [
        {
            "reference_id": item.reference_id,
            "source": source_map[item.source],
            "origin": {
                "source": source_map[item.origin.source],
                "ref": item.origin.ref,
                "char_span": list(item.origin.char_span) if item.origin.char_span is not None else None,
            },
            "ref_kind": item.ref_kind,
            "raw_target": item.raw_target,
            "normalized_target": _replace_sources(item.normalized_target, source_map, ids),
            "reason": item.reason,
            "candidates": [_replace_sources(value, source_map, ids) for value in item.candidates],
        }
        for item in signals.unresolved
    ]
    connected = {
        source_map[item.src_source]
        for item in signals.accepted
        for _source in (0,)
    } | {
        source_map[item.dst_source]
        for item in signals.accepted
        for _source in (0,)
    }
    all_sources = set(source_map.values())
    return {
        "candidates": candidates,
        "unresolved": unresolved,
        "orphans": sorted(all_sources - connected),
    }


def _unknown_extractions(extractions: Sequence[Any]) -> list[Any]:
    from autotldr.unit import Extraction, Role

    return [
        Extraction(
            source=item.source,
            kind=item.kind,
            units=[dataclasses.replace(unit, role=Role.UNKNOWN) for unit in item.units],
            relations=list(item.relations),
            gaps=list(item.gaps),
            meta=dict(item.meta),
            summary_claims=list(item.summary_claims),
        )
        for item in extractions
    ]


def _semantic_key_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove runtime ids/origins while retaining every scored structured fact."""

    candidates: dict[str, list[dict[str, Any]]] = {}
    for task, rows in payload["candidates"].items():
        candidates[task] = sorted(
            [
                {
                    "src_source": row["src_source"],
                    "dst_source": row["dst_source"],
                    "relation_kind": row["relation_kind"],
                    "details": {
                        key: value
                        for key, value in row["details"].items()
                        if key not in {"target_anchor", "left", "right"}
                    }
                    | (
                        {
                            "left": {
                                key: value
                                for key, value in row["details"].get("left", {}).items()
                                if key not in {"unit_id", "origin", "source"}
                            },
                            "right": {
                                key: value
                                for key, value in row["details"].get("right", {}).items()
                                if key not in {"unit_id", "origin", "source"}
                            },
                        }
                        if "left" in row["details"] and "right" in row["details"]
                        else {}
                    ),
                }
                for row in rows
            ],
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        )
    unresolved = sorted(
        [
            {
                "source": row["source"],
                "ref_kind": row["ref_kind"],
                "raw_target": row["raw_target"],
                "reason": row["reason"],
                "candidates": row["candidates"],
            }
            for row in payload["unresolved"]
        ],
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )
    return {"candidates": candidates, "unresolved": unresolved, "orphans": payload["orphans"]}


def _extract_collection(split: str, collection: Mapping[str, Any], root: Path | None = None):
    from autotldr.router import extract

    collection_id = collection["id"]
    fixture_root = root or (HERE / split / "fixtures" / collection_id)
    relatives = list(collection["sources"])
    extractions = [extract(fixture_root / relative) for relative in relatives]
    return relatives, extractions


def _run_collection(split: str, collection: Mapping[str, Any]) -> dict[str, Any]:
    from autotldr.fusion import analyze

    relatives, extractions = _extract_collection(split, collection)
    mapping = _source_map(extractions, relatives)
    ids = {unit.id for extraction in extractions for unit in extraction.units}
    if len(ids) != sum(len(item.units) for item in extractions):
        raise EvaluationError(f"{collection['id']}: duplicate unit ids across sources")
    base = _signal_payload(analyze(extractions), mapping, ids)
    repeat = _signal_payload(analyze(extractions), mapping, ids)
    reversed_extractions = list(reversed(extractions))
    reversed_mapping = {item.source: mapping[item.source] for item in reversed_extractions}
    permuted = _signal_payload(analyze(reversed_extractions), reversed_mapping, ids)
    unknown = _unknown_extractions(extractions)
    unknown_payload = _signal_payload(analyze(unknown), mapping, ids)
    base_semantic = _semantic_key_payload(base)

    with tempfile.TemporaryDirectory(prefix="autotldr-fusion-relocate-") as temp_name:
        relocated_root = Path(temp_name) / str(collection["id"])
        original_root = HERE / split / "fixtures" / str(collection["id"])
        shutil.copytree(original_root, relocated_root)
        relocated_relatives, relocated = _extract_collection(split, collection, relocated_root)
        relocated_map = _source_map(relocated, relocated_relatives)
        relocated_ids = {unit.id for item in relocated for unit in item.units}
        relocated_payload = _signal_payload(analyze(relocated), relocated_map, relocated_ids)

    checks = {
        "deterministic_repeat": base == repeat,
        "input_permutation_equal": base == permuted,
        "all_unknown_equal": base == unknown_payload,
        "root_relocation_equal": base_semantic == _semantic_key_payload(relocated_payload),
        "full_existing_unit_ids": True,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise EvaluationError(f"{collection['id']}: robustness checks failed: {failed}")
    return {"collection": collection["id"], **base, "checks": checks}


def _ensure_run_target(
    split: str,
    output: Path,
    *,
    force: bool,
    canonical_scored_output: Path | None = None,
    scored_artifacts: Sequence[Path] | None = None,
) -> None:
    """Enforce a single canonical held-out run while keeping dev iterative."""

    if split != "scored":
        if output.exists() and not force:
            raise EvaluationError(f"refusing to overwrite {output}; use --force explicitly")
        return

    canonical = canonical_scored_output or (PREDICTIONS / "scored.jsonl")
    if force:
        raise EvaluationError("--force is dev-only; the scored run is immutable")
    if output.resolve() != canonical.resolve():
        raise EvaluationError(f"scored predictions must use the canonical output {canonical}")
    if scored_artifacts is None:
        discovered = set(PREDICTIONS.glob("scored*"))
        discovered.update((HERE / "scored").glob("report.*"))
        discovered.update(
            {canonical, canonical.with_suffix(".manifest.json"), REPORT_JSON, REPORT_MD}
        )
        artifacts = sorted(discovered)
    else:
        artifacts = list(scored_artifacts)
    existing = [path for path in artifacts if path.exists()]
    if existing:
        raise EvaluationError(
            "scored run is one-shot and an artifact already exists: "
            + ", ".join(str(path) for path in existing)
        )


def _validate_scored_review_clearance(
    *,
    clearance_path: Path = REVIEW_CLEARANCE_PATH,
    freeze_path: Path = FREEZE_PATH,
    benchmark_root: Path = HERE,
) -> dict[str, Any]:
    """Require two hash-bound blind reviews before held-out prediction.

    The freeze deliberately records that a corrected-corpus delta review is
    required.  A prose review file alone is mutable and therefore insufficient
    as a machine gate.  The clearance record binds both independent reviews to
    the exact freeze and every scored artifact that matters to interpretation.
    """

    if not clearance_path.exists():
        raise EvaluationError(
            "scored run is locked pending hash-bound blind-review clearance at "
            f"{clearance_path}"
        )
    freeze = _load_json(freeze_path)
    clearance = _load_json(clearance_path)
    scored = freeze.get("splits", {}).get("scored")
    if not isinstance(scored, dict):
        raise EvaluationError("freeze has no scored split for review clearance")

    expected = {
        "schema": 1,
        "status": "accepted",
        "reviewed_freeze_sha256": _file_sha256(freeze_path),
        "policy_sha256": freeze.get("policy_sha256"),
        "scored_source_tree_sha256": scored.get("source_tree_sha256"),
        "scored_sources_sha256": scored.get("sources_sha256"),
        "scored_extractions_sha256": scored.get("extractions_sha256"),
        "scored_labels_sha256": scored.get("labels_sha256"),
        "scored_annotations_sha256": scored.get("annotations_sha256"),
        "scored_predictions_absent_at_clearance": True,
    }
    mismatches = _binding_mismatches(clearance, expected)
    if mismatches:
        raise EvaluationError(
            "blind-review clearance is not bound to this frozen scored corpus: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )

    reviews = clearance.get("reviews")
    if not isinstance(reviews, list):
        raise EvaluationError("blind-review clearance reviews must be a list")
    by_path: dict[str, Mapping[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict) or not isinstance(review.get("path"), str):
            raise EvaluationError("blind-review clearance has a malformed review row")
        path = str(review["path"])
        if path in by_path:
            raise EvaluationError(f"blind-review clearance duplicates {path}")
        by_path[path] = review
    if set(by_path) != set(REQUIRED_BLIND_REVIEWS):
        raise EvaluationError(
            "blind-review clearance must bind exactly: "
            + ", ".join(REQUIRED_BLIND_REVIEWS)
        )
    for relative in REQUIRED_BLIND_REVIEWS:
        review = by_path[relative]
        path = benchmark_root / relative
        if review.get("verdict") != "FIT":
            raise EvaluationError(f"blind review {relative} did not return FIT")
        if not path.is_file():
            raise EvaluationError(f"blind review is missing: {path}")
        if review.get("sha256") != _file_sha256(path):
            raise EvaluationError(f"blind review hash differs: {relative}")
    return clearance


def run_predictions(split: str, output: Path, *, force: bool = False) -> dict[str, Any]:
    evaluator_self_test()
    _ensure_run_target(split, output, force=force)
    if split == "scored":
        _validate_scored_review_clearance()
    build = _load_build_module()
    try:
        build.validate()
    except Exception as exc:
        raise EvaluationError(f"frozen corpus validation failed before run: {exc}") from exc
    collection_rows, _index = _collections(split)
    rows = [_run_collection(split, collection) for collection in collection_rows]
    payload = _jsonl_bytes(rows)
    _atomic_write(output, payload)
    freeze = _load_json(FREEZE_PATH)
    fusion_path = HERE.parents[1] / "src" / "autotldr" / "fusion.py"
    manifest = {
        "schema": 2,
        "evaluator_version": EVALUATOR_VERSION,
        "split": split,
        "status": "complete",
        "collections": len(rows),
        "predictions_sha256": _sha256(payload),
        "policy_sha256": freeze["policy_sha256"],
        "source_tree_sha256": freeze["splits"][split]["source_tree_sha256"],
        "extractions_sha256": freeze["splits"][split]["extractions_sha256"],
        "labels_sha256_recorded_but_not_loaded": freeze["splits"][split]["labels_sha256"],
        "implementation_sha256": _file_sha256(fusion_path),
        "evaluator_sha256": _file_sha256(Path(__file__)),
        "structured_trace_only": True,
        "models": [],
        "robustness": {
            name: all(row["checks"][name] for row in rows)
            for name in rows[0]["checks"]
        } if rows else {},
    }
    manifest_path = output.with_suffix(".manifest.json")
    _atomic_write(manifest_path, _json_bytes(manifest, pretty=True))
    return manifest


@dataclasses.dataclass(frozen=True, slots=True)
class _ScoredPrediction:
    """One canonical prediction plus its independently observable subtype(s)."""

    key: tuple[Any, ...]
    subtypes: frozenset[str]
    subtype_basis: str


def _literal_prediction_subtypes(detail: Mapping[str, Any]) -> frozenset[str]:
    ref_kind = _kind(str(detail.get("ref_kind", "")))
    resolution = str(detail.get("resolution", "")).casefold()
    if ref_kind == "label" or resolution == "explicit-label":
        return frozenset({"label-key"})
    if ref_kind == "citation" or resolution == "citation-definition":
        return frozenset({"citation-key"})
    if ref_kind in {"url", "doi"} or resolution in {
        "doi-source-identity",
        "url-identity",
    }:
        return frozenset({"url-source-identity"})
    if ref_kind == "path" or resolution in {
        "path-identity",
        "relative-import",
        "unique-basename",
    }:
        return frozenset({"local-path"})
    return frozenset({_UNCLASSIFIED_SUBTYPE})


def _identifier_prediction_subtypes(
    left_states: set[bool], right_states: set[bool]
) -> frozenset[str]:
    if not left_states or not right_states:
        return frozenset({_UNCLASSIFIED_SUBTYPE})
    subtypes: set[str] = set()
    for left_native, right_native in itertools.product(left_states, right_states):
        if left_native and right_native:
            subtypes.add("native-native")
        elif left_native or right_native:
            subtypes.add("explicit-prose-native")
        else:
            # This is an unambiguous out-of-scope subtype, not missing
            # attribution. It cannot make an allowed native subtype look good.
            subtypes.add("prose-prose")
    return frozenset(subtypes)


def _schema_classes(source_kind: str, family: str) -> frozenset[str]:
    """Classify a native source as a table or record schema without gold."""

    kind = source_kind.casefold()
    if kind in _TABLE_SOURCE_KINDS:
        return frozenset({"table"})
    if kind in _RECORD_SOURCE_KINDS:
        # JSONL is a record stream even when the matched container itself is
        # row-shaped. The benchmark taxonomy is about native source schemas,
        # not an implementation's internal container name.
        return frozenset({"record"})
    normalized_family = family.casefold()
    if normalized_family == "record":
        return frozenset({"record"})
    if normalized_family == "table":
        return frozenset({"table"})
    return frozenset({_UNCLASSIFIED_SUBTYPE})


def _structural_prediction_subtypes(
    row: Mapping[str, Any], kinds: Mapping[tuple[str, str], str], collection_id: str
) -> frozenset[str]:
    detail = row["details"]
    left_classes = _schema_classes(
        kinds.get((collection_id, str(row["src_source"])), ""),
        str(detail.get("left_family", "")),
    )
    right_classes = _schema_classes(
        kinds.get((collection_id, str(row["dst_source"])), ""),
        str(detail.get("right_family", "")),
    )
    if _UNCLASSIFIED_SUBTYPE in left_classes or _UNCLASSIFIED_SUBTYPE in right_classes:
        return frozenset({_UNCLASSIFIED_SUBTYPE})
    subtypes: set[str] = set()
    for left_class, right_class in itertools.product(left_classes, right_classes):
        if {left_class, right_class} == {"table", "record"}:
            subtypes.add("table-record-schema")
        elif left_class == right_class == "record":
            subtypes.add("record-schema-record-schema")
        else:
            subtypes.add("table-schema-table-schema")
    return frozenset(subtypes)


def _contradiction_prediction_subtypes(detail: Mapping[str, Any]) -> frozenset[str]:
    bases = {
        str(side.get("basis", "")).casefold()
        for side in (detail.get("left", {}), detail.get("right", {}))
    }
    if bases and bases <= {"native-structured-constant", "strict-prose-assignment"}:
        return frozenset({"constant-constant"})
    if bases and any("declared-count" in basis for basis in bases) and any(
        "observed-count" in basis for basis in bases
    ):
        return frozenset({"declared-count-observed-count"})
    return frozenset({_UNCLASSIFIED_SUBTYPE})


def _unresolved_prediction_subtypes(row: Mapping[str, Any]) -> frozenset[str]:
    ref_kind = _kind(str(row.get("ref_kind", "")))
    reason = str(row.get("reason", "")).casefold()
    if ref_kind == "path" and reason == "ambiguous-target":
        return frozenset({"ambiguous-local-path"})
    if ref_kind == "path":
        return frozenset({"local-path"})
    if ref_kind == "label":
        return frozenset({"label-key"})
    if ref_kind in {"citation", "doi"}:
        return frozenset({"citation-key"})
    return frozenset({_UNCLASSIFIED_SUBTYPE})


def _prediction_facts(
    rows: Sequence[Mapping[str, Any]],
    kinds: Mapping[tuple[str, str], str],
) -> dict[str, list[_ScoredPrediction]]:
    facts: dict[str, list[_ScoredPrediction]] = collections.defaultdict(list)
    for collection_row in rows:
        collection_id = str(collection_row["collection"])
        candidates = collection_row["candidates"]
        for row in candidates["literal"]:
            detail = row["details"]
            facts["literal"].append(
                _ScoredPrediction(
                    key=(
                    "literal",
                    collection_id,
                    row["src_source"],
                    row["dst_source"],
                    _kind(str(detail.get("ref_kind", "path"))),
                    _normalize_target(str(detail.get("raw_target", ""))),
                    ),
                    subtypes=_literal_prediction_subtypes(detail),
                    subtype_basis="ref_kind+resolution",
                )
            )

        # Identifier topology is intentionally scored by per-concept connected
        # components, not by an arbitrary star-versus-clique edge policy.
        by_identifier: dict[tuple[str, ...], list[tuple[str, str]]] = collections.defaultdict(list)
        native_states: dict[tuple[str, ...], dict[str, set[bool]]] = collections.defaultdict(
            lambda: collections.defaultdict(set)
        )
        for row in candidates["identifier"]:
            detail = row["details"]
            canonical = tuple(
                token
                for item in detail.get("canonical", [])
                for token in _normalize_identifier(str(item), conceptual=True)
            )
            by_identifier[canonical].append((row["src_source"], row["dst_source"]))
            if type(detail.get("left_native")) is bool:
                native_states[canonical][str(row["src_source"])].add(detail["left_native"])
            if type(detail.get("right_native")) is bool:
                native_states[canonical][str(row["dst_source"])].add(detail["right_native"])
        for canonical, edges in by_identifier.items():
            adjacency: dict[str, set[str]] = collections.defaultdict(set)
            for left, right in edges:
                adjacency[left].add(right)
                adjacency[right].add(left)
            seen: set[str] = set()
            for source in sorted(adjacency):
                if source in seen:
                    continue
                stack = [source]
                component: set[str] = set()
                while stack:
                    current = stack.pop()
                    if current in component:
                        continue
                    component.add(current)
                    stack.extend(adjacency[current] - component)
                seen.update(component)
                for left, right in itertools.combinations(sorted(component), 2):
                    facts["identifier"].append(
                        _ScoredPrediction(
                            key=("identifier", collection_id, canonical, left, right),
                            subtypes=_identifier_prediction_subtypes(
                                native_states[canonical][left],
                                native_states[canonical][right],
                            ),
                            subtype_basis="left_native+right_native",
                        )
                    )

        for row in candidates["structural"]:
            detail = row["details"]
            shared = tuple(
                sorted(
                    _canonical_field(str(item))
                    for item in detail.get("discriminative_shared", [])
                )
            )
            left, right = _pair(row["src_source"], row["dst_source"])
            facts["structural"].append(
                _ScoredPrediction(
                    key=("structural", collection_id, left, right, shared),
                    subtypes=_structural_prediction_subtypes(row, kinds, collection_id),
                    subtype_basis="source_kind+container_family",
                )
            )
        for row in candidates["contradiction"]:
            detail = row["details"]
            canonical_key = tuple(
                token
                for item in detail.get("canonical_key", [])
                for token in _normalize_identifier(str(item), conceptual=False)
            )
            values = tuple(
                sorted(
                    (
                        str(side.get("value_type", "")),
                        str(side.get("canonical_value", "")),
                    )
                    for side in (detail.get("left", {}), detail.get("right", {}))
                )
            )
            left, right = _pair(row["src_source"], row["dst_source"])
            facts["contradiction"].append(
                _ScoredPrediction(
                    key=(
                        "contradiction",
                        collection_id,
                        left,
                        right,
                        canonical_key,
                        values,
                    ),
                    subtypes=_contradiction_prediction_subtypes(detail),
                    subtype_basis="left_basis+right_basis",
                )
            )
        for source in collection_row["orphans"]:
            facts["orphan"].append(
                _ScoredPrediction(
                    key=("orphan", collection_id, source),
                    subtypes=frozenset({"no-accepted-relation"}),
                    subtype_basis="absence-of-accepted-cross-source-relation",
                )
            )
        for row in collection_row["unresolved"]:
            facts["unresolved"].append(
                _ScoredPrediction(
                    key=(
                        "unresolved",
                        collection_id,
                        row["source"],
                        _kind(str(row["ref_kind"])),
                        _normalize_target(str(row["raw_target"])),
                    ),
                    subtypes=_unresolved_prediction_subtypes(row),
                    subtype_basis="ref_kind+reason",
                )
            )
    return facts


def _prediction_keys(
    rows: Sequence[Mapping[str, Any]],
    kinds: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, list[tuple[Any, ...]]]:
    return {
        task: [fact.key for fact in task_facts]
        for task, task_facts in _prediction_facts(rows, kinds or {}).items()
    }


def _wilson(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = proportion + z * z / (2 * total)
    margin = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    )
    return [round((centre - margin) / denominator, 6), round((centre + margin) / denominator, 6)]


def _jsonable_key(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable_key(item) for item in value]
    return value


def _stable_keys(values: Iterable[tuple[Any, ...]]) -> list[Any]:
    serialized = [_jsonable_key(value) for value in values]
    return sorted(
        serialized,
        key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True),
    )


def _metrics(
    gold: set[tuple[Any, ...]],
    predicted: Sequence[tuple[Any, ...]],
) -> dict[str, Any]:
    counts = collections.Counter(predicted)
    predicted_set = set(counts)
    tp = len(gold & predicted_set)
    duplicate_fp = sum(max(0, count - 1) for count in counts.values())
    fp = len(predicted_set - gold) + duplicate_fp
    fn = len(gold - predicted_set)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "duplicate_fp": duplicate_fp,
        "support": len(gold),
        "predictions": len(predicted),
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "precision_wilson_95": _wilson(tp, tp + fp),
        "recall_wilson_95": _wilson(tp, tp + fn),
        "false_positive_keys": _stable_keys(predicted_set - gold),
        "false_negative_keys": _stable_keys(gold - predicted_set),
        "duplicate_prediction_keys": [
            {"key": _jsonable_key(key), "extra_count": count - 1}
            for key, count in sorted(
                (
                    (key, count)
                    for key, count in counts.items()
                    if count > 1
                ),
                key=lambda item: json.dumps(
                    _jsonable_key(item[0]), ensure_ascii=False, sort_keys=True
                ),
            )
        ],
    }


def _source_kind_index(split: str) -> dict[tuple[str, str], str]:
    return {
        (row["collection"], row["source"]): row["kind"]
        for row in _load_jsonl(HERE / split / "sources.jsonl")
    }


def _key_sources(key: tuple[Any, ...]) -> tuple[str, ...]:
    task = key[0]
    if task == "literal":
        return (key[2], key[3])
    if task == "identifier":
        return (key[3], key[4])
    if task in {"structural", "contradiction"}:
        return (key[2], key[3])
    return (key[2],)


def _binding_mismatches(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        field: {"expected": expected_value, "actual": actual.get(field)}
        for field, expected_value in expected.items()
        if actual.get(field) != expected_value
    }


def _expected_manifest_binding(split: str) -> dict[str, Any]:
    frozen = _load_json(FREEZE_PATH)
    fusion_path = HERE.parents[1] / "src" / "autotldr" / "fusion.py"
    collections_rows, _index = _collections(split)
    return {
        "schema": 2,
        "evaluator_version": EVALUATOR_VERSION,
        "split": split,
        "status": "complete",
        "collections": len(collections_rows),
        "policy_sha256": frozen["policy_sha256"],
        "source_tree_sha256": frozen["splits"][split]["source_tree_sha256"],
        "extractions_sha256": frozen["splits"][split]["extractions_sha256"],
        "labels_sha256_recorded_but_not_loaded": frozen["splits"][split]["labels_sha256"],
        "implementation_sha256": _file_sha256(fusion_path),
        "evaluator_sha256": _file_sha256(Path(__file__)),
        "structured_trace_only": True,
        "models": [],
    }


def _validate_prediction_manifest(
    manifest: Mapping[str, Any], predictions_path: Path, split: str
) -> None:
    if manifest.get("predictions_sha256") != _file_sha256(predictions_path):
        raise EvaluationError("prediction manifest hash does not match predictions")
    mismatches = _binding_mismatches(manifest, _expected_manifest_binding(split))
    if mismatches:
        raise EvaluationError(
            "prediction manifest is not bound to this split/corpus/policy/code: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )
    robustness = manifest.get("robustness")
    required_checks = {
        "all_unknown_equal",
        "deterministic_repeat",
        "full_existing_unit_ids",
        "input_permutation_equal",
        "root_relocation_equal",
    }
    if not isinstance(robustness, dict) or any(
        robustness.get(check) is not True for check in required_checks
    ):
        raise EvaluationError("prediction manifest lacks passing robustness checks")


def _report_paths(split: str) -> tuple[Path, Path]:
    if split == "scored":
        return REPORT_JSON, REPORT_MD
    return HERE / split / "report.json", HERE / split / "report.md"


def evaluator_self_test() -> dict[str, Any]:
    """Exercise scorer invariants without reading held-out predictions or gold."""

    literal_rows = [
        {
            "collection": "self-test",
            "candidates": {
                "literal": [
                    {
                        "src_source": "guide.md",
                        "dst_source": target,
                        "details": {
                            "ref_kind": "path",
                            "raw_target": target,
                            "resolution": "path-identity",
                        },
                    }
                    for target in ("truth.csv", "false-positive.csv")
                ],
                "identifier": [],
                "structural": [],
                "contradiction": [],
            },
            "orphans": [],
            "unresolved": [],
        }
    ]
    literal_facts = _prediction_facts(literal_rows, {})["literal"]
    literal_gold = {literal_facts[0].key}
    local_path_predictions = [
        fact.key for fact in literal_facts if "local-path" in fact.subtypes
    ]
    subtype_metrics = _metrics(literal_gold, local_path_predictions)
    if subtype_metrics["precision"] != 0.5 or subtype_metrics["fp"] != 1:
        raise EvaluationError(
            "self-test failed: an unlabeled subtype FP did not lower precision"
        )

    structural_label = {
        "id": "self-structural",
        "collection": "self-test",
        "task": "structural",
        "positive": True,
        "sources": ["rows.csv", "schema.json"],
        "subtype": "table-record-schema",
        "fact": {"fields": ["ack_latency_ms", "message_id", "retry_count"]},
    }
    structural_rows = [
        {
            "collection": "self-test",
            "candidates": {
                "literal": [],
                "identifier": [],
                "structural": [
                    {
                        "src_source": "rows.csv",
                        "dst_source": "schema.json",
                        "details": {
                            "discriminative_shared": [
                                "ack.latency.ms",
                                "message.id",
                                "retry.count",
                            ],
                            "left_family": "table",
                            "right_family": "record",
                        },
                    }
                ],
                "contradiction": [],
            },
            "orphans": [],
            "unresolved": [],
        }
    ]
    structural_gold = _label_expansion(structural_label)[0][0]
    structural_fact = _prediction_facts(
        structural_rows,
        {
            ("self-test", "rows.csv"): "csv",
            ("self-test", "schema.json"): "json",
        },
    )["structural"][0]
    if structural_fact.key != structural_gold:
        raise EvaluationError(
            "self-test failed: dot and underscore structural fields are not canonical-equivalent"
        )
    if structural_fact.subtypes != frozenset({"table-record-schema"}):
        raise EvaluationError("self-test failed: structural subtype attribution differs")

    split_mismatch = _binding_mismatches(
        {"split": "dev", "policy_sha256": "policy-a"},
        {"split": "scored", "policy_sha256": "policy-a"},
    )
    hash_mismatch = _binding_mismatches(
        {"split": "dev", "policy_sha256": "policy-a"},
        {"split": "dev", "policy_sha256": "policy-b"},
    )
    if set(split_mismatch) != {"split"} or set(hash_mismatch) != {"policy_sha256"}:
        raise EvaluationError("self-test failed: manifest binding mismatch was not rejected")

    with tempfile.TemporaryDirectory(prefix="autotldr-fusion-evaluator-self-test-") as temp:
        root = Path(temp)
        canonical = root / "scored.jsonl"
        artifacts = [
            canonical,
            canonical.with_suffix(".manifest.json"),
            root / "report.json",
            root / "report.md",
        ]
        _ensure_run_target(
            "scored",
            canonical,
            force=False,
            canonical_scored_output=canonical,
            scored_artifacts=artifacts,
        )
        try:
            _ensure_run_target(
                "scored",
                canonical,
                force=True,
                canonical_scored_output=canonical,
                scored_artifacts=artifacts,
            )
        except EvaluationError:
            pass
        else:
            raise EvaluationError("self-test failed: scored --force was accepted")
        _atomic_write(artifacts[1], b"{}\n")
        try:
            _ensure_run_target(
                "scored",
                canonical,
                force=False,
                canonical_scored_output=canonical,
                scored_artifacts=artifacts,
            )
        except EvaluationError:
            pass
        else:
            raise EvaluationError("self-test failed: scored artifact overwrite was accepted")

        review_paths = [root / relative for relative in REQUIRED_BLIND_REVIEWS]
        for index, review_path in enumerate(review_paths, start=1):
            _atomic_write(review_path, f"blind review {index}: FIT\n".encode("utf-8"))
        freeze_path = root / "freeze.json"
        freeze_value = {
            "policy_sha256": "policy-a",
            "splits": {
                "scored": {
                    "source_tree_sha256": "tree-a",
                    "sources_sha256": "sources-a",
                    "extractions_sha256": "extractions-a",
                    "labels_sha256": "labels-a",
                    "annotations_sha256": "annotations-a",
                }
            },
        }
        _atomic_write(freeze_path, _json_bytes(freeze_value, pretty=True))
        clearance_path = root / "reviews" / "clearance.json"
        clearance_value = {
            "schema": 1,
            "status": "accepted",
            "reviewed_freeze_sha256": _file_sha256(freeze_path),
            "policy_sha256": "policy-a",
            "scored_source_tree_sha256": "tree-a",
            "scored_sources_sha256": "sources-a",
            "scored_extractions_sha256": "extractions-a",
            "scored_labels_sha256": "labels-a",
            "scored_annotations_sha256": "annotations-a",
            "scored_predictions_absent_at_clearance": True,
            "reviews": [
                {
                    "path": relative,
                    "sha256": _file_sha256(root / relative),
                    "verdict": "FIT",
                }
                for relative in REQUIRED_BLIND_REVIEWS
            ],
        }
        _atomic_write(clearance_path, _json_bytes(clearance_value, pretty=True))
        _validate_scored_review_clearance(
            clearance_path=clearance_path,
            freeze_path=freeze_path,
            benchmark_root=root,
        )
        _atomic_write(review_paths[0], b"tampered\n")
        try:
            _validate_scored_review_clearance(
                clearance_path=clearance_path,
                freeze_path=freeze_path,
                benchmark_root=root,
            )
        except EvaluationError:
            pass
        else:
            raise EvaluationError(
                "self-test failed: tampered blind review passed clearance"
            )

    return {
        "status": "valid",
        "subtype_false_positive_lowers_precision": True,
        "structural_delimiter_canonicalization": True,
        "manifest_split_and_hash_binding": True,
        "scored_one_shot_guard": True,
        "scored_review_clearance_guard": True,
    }


def report(predictions_path: Path, split: str = "scored") -> dict[str, Any]:
    evaluator_self_test()
    build = _load_build_module()
    try:
        corpus_validation = build.validate()
    except Exception as exc:
        raise EvaluationError(f"frozen corpus validation failed: {exc}") from exc
    label_validation = validate_labels(split)
    labels = _load_jsonl(HERE / split / "labels.jsonl")
    prediction_rows = _load_jsonl(predictions_path)
    manifest = _load_json(predictions_path.with_suffix(".manifest.json"))
    _validate_prediction_manifest(manifest, predictions_path, split)
    expected_collections = [row["id"] for row in _collections(split)[0]]
    actual_collections = [row.get("collection") for row in prediction_rows]
    if actual_collections != expected_collections:
        raise EvaluationError(
            f"prediction collections differ for {split}: "
            f"expected {expected_collections}, got {actual_collections}"
        )

    gold_by_task: dict[str, set[tuple[Any, ...]]] = collections.defaultdict(set)
    subtype_by_key: dict[tuple[Any, ...], str] = {}
    groups_by_task: dict[str, set[str]] = collections.defaultdict(set)
    negative_by_key: dict[tuple[Any, ...], str] = {}
    gold_subtype_complete: dict[str, bool] = {task: True for task in TASKS}
    for label in labels:
        expanded = _label_expansion(label)
        if label["positive"]:
            if _identifier_provenance_error(label):
                gold_subtype_complete["identifier"] = False
            for key, subtype in expanded:
                gold_by_task[label["task"]].add(key)
                subtype_by_key[key] = subtype
                groups_by_task[label["task"]].add(label["collection"])
        else:
            hard_class = label.get("hard_negative_class")
            if isinstance(hard_class, str):
                for key, _subtype in expanded:
                    negative_by_key[key] = hard_class

    policy = _load_json(POLICY_PATH)
    kinds = _source_kind_index(split)
    prediction_facts = _prediction_facts(prediction_rows, kinds)
    predicted_by_task: dict[str, list[tuple[Any, ...]]] = collections.defaultdict(list)
    for task, facts in prediction_facts.items():
        predicted_by_task[task] = [fact.key for fact in facts]
    task_reports: dict[str, Any] = {}
    for task in TASKS:
        task_metrics = _metrics(gold_by_task[task], predicted_by_task[task])
        gate = policy["gates"][task]
        task_metrics["groups"] = len(groups_by_task[task])
        task_metrics["gate"] = gate
        meets_gate = bool(
            task_metrics["support"] >= gate["minimum_support"]
            and task_metrics["groups"] >= gate["minimum_groups"]
            and task_metrics["precision"] is not None
            and task_metrics["precision"] >= gate["minimum_precision"]
            and task_metrics["recall"] is not None
            and task_metrics["recall"] >= gate["minimum_recall"]
        )
        task_metrics["passes"] = meets_gate if split == "scored" else None
        task_metrics["diagnostic_would_pass_scored_gate"] = meets_gate
        predicted_set = set(predicted_by_task[task])
        hard_hits: collections.Counter[str] = collections.Counter()
        for key in predicted_set:
            if key in negative_by_key:
                hard_hits[negative_by_key[key]] += 1
        task_metrics["hard_negative_false_positives"] = dict(sorted(hard_hits.items()))

        task_facts = prediction_facts[task]
        unclassified = [
            fact for fact in task_facts if _UNCLASSIFIED_SUBTYPE in fact.subtypes
        ]
        assignment_counts: collections.Counter[str] = collections.Counter(
            subtype for fact in task_facts for subtype in fact.subtypes
        )
        task_metrics["subtype_attribution"] = {
            "complete": not unclassified and gold_subtype_complete[task],
            "prediction_complete": not unclassified,
            "gold_complete": gold_subtype_complete[task],
            "ambiguous_predictions": sum(len(fact.subtypes) > 1 for fact in task_facts),
            "assignment_counts": dict(sorted(assignment_counts.items())),
            "unclassified_prediction_keys": _stable_keys(fact.key for fact in unclassified),
            "basis": sorted({fact.subtype_basis for fact in task_facts}),
        }
        subtype_reports: dict[str, Any] = {}
        subtypes = sorted(
            {subtype_by_key[key] for key in gold_by_task[task]}
            | {
                subtype
                for fact in task_facts
                for subtype in fact.subtypes
                if subtype != _UNCLASSIFIED_SUBTYPE
            }
        )
        for subtype in subtypes:
            sub_gold = {key for key in gold_by_task[task] if subtype_by_key[key] == subtype}
            # Subtype assignment is derived only from structured prediction
            # facts. Therefore an unlabeled candidate assigned here is an FP.
            sub_pred = [fact.key for fact in task_facts if subtype in fact.subtypes]
            subtype_reports[subtype] = _metrics(sub_gold, sub_pred)
            subtype_reports[subtype]["groups"] = len({key[1] for key in sub_gold})
            subtype_reports[subtype]["eligible"] = (
                not unclassified and gold_subtype_complete[task]
            )
        task_metrics["subtypes"] = subtype_reports

        format_slices: dict[str, dict[str, Any]] = {}
        format_names: set[str] = set()
        for key in gold_by_task[task] | predicted_set:
            source_kinds = sorted(kinds.get((key[1], source), "unknown") for source in _key_sources(key))
            format_names.add("<->".join(source_kinds))
        for format_name in sorted(format_names):
            def belongs(key: tuple[Any, ...]) -> bool:
                return "<->".join(
                    sorted(kinds.get((key[1], source), "unknown") for source in _key_sources(key))
                ) == format_name
            format_slices[format_name] = _metrics(
                {key for key in gold_by_task[task] if belongs(key)},
                [key for key in predicted_by_task[task] if belongs(key)],
            )
        task_metrics["format_pair_slices"] = format_slices

        if split != "scored":
            disposition = {"status": "diagnostic-only", "subtypes": []}
        elif task_metrics["passes"]:
            disposition = {"status": "ship-complete", "subtypes": []}
        else:
            shrink_policy = policy.get("subtypes", {}).get(task)
            passing_subtypes: list[str] = []
            if isinstance(shrink_policy, dict):
                for subtype in shrink_policy["allowed"]:
                    metrics = subtype_reports.get(subtype)
                    if not metrics:
                        continue
                    if (
                        metrics["eligible"]
                        and task_metrics["subtype_attribution"]["complete"]
                        and metrics["support"] >= shrink_policy["minimum_support"]
                        and metrics["groups"] >= shrink_policy["minimum_groups"]
                        and metrics["precision"] is not None
                        and metrics["precision"] >= gate["minimum_precision"]
                        and metrics["recall"] is not None
                        and metrics["recall"] >= gate["minimum_recall"]
                    ):
                        passing_subtypes.append(subtype)
            disposition = (
                {"status": "ship-preregistered-subtype", "subtypes": passing_subtypes}
                if passing_subtypes
                else {"status": "disable", "subtypes": []}
            )
        task_metrics["disposition"] = disposition
        task_reports[task] = task_metrics

    result = {
        "schema": 2,
        "benchmark": "stage4-model-free-fusion-v1",
        "split": split,
        "corpus_validation": corpus_validation,
        "label_validation": label_validation,
        "prediction_manifest": manifest,
        "signals": task_reports,
        "aggregate_is_gate": False,
        "models": [],
    }
    report_json, report_md = _report_paths(split)
    _atomic_write(report_json, _json_bytes(result, pretty=True))
    _atomic_write(report_md, _render_markdown(result).encode("utf-8"))
    return result


def _score(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _render_markdown(report_value: Mapping[str, Any]) -> str:
    split = str(report_value["split"])
    lines = [
        f"# Stage 4 model-free fusion report ({split})",
        "",
        (
            "The gate is per signal. Aggregate accuracy is deliberately not used."
            if split == "scored"
            else "This development report is diagnostic only; scored gates are not applied."
        ),
        "",
        "| Signal | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Disposition |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for task in TASKS:
        metrics = report_value["signals"][task]
        lines.append(
            f"| `{task}` | {metrics['support']} | {metrics['groups']} | "
            f"{metrics['tp']} | {metrics['fp']} | {metrics['fn']} | "
            f"{_score(metrics['precision'])} | {_score(metrics['recall'])} | "
            f"{_score(metrics['f1'])} | {metrics['disposition']['status']} |"
        )
    lines.extend(["", "## Signal details", ""])
    for task in TASKS:
        metrics = report_value["signals"][task]
        gate_status = (
            "not evaluated on dev"
            if metrics["passes"] is None
            else ("yes" if metrics["passes"] else "no")
        )
        lines.extend(
            [
                f"### {task}",
                "",
                f"Gate passed: **{gate_status}**. "
                f"Precision Wilson 95%: `{metrics['precision_wilson_95']}`; "
                f"recall Wilson 95%: `{metrics['recall_wilson_95']}`.",
                "",
                "Error inventory: "
                f"{len(metrics['false_positive_keys'])} unique FP key(s), "
                f"{metrics['duplicate_fp']} duplicate FP occurrence(s), and "
                f"{len(metrics['false_negative_keys'])} FN key(s). "
                "The stable serialized keys are in `report.json`.",
                "",
                f"Hard-negative false positives: "
                f"`{json.dumps(metrics['hard_negative_false_positives'], sort_keys=True)}`.",
                "",
                "Subtype attribution: "
                f"`{json.dumps(metrics['subtype_attribution'], sort_keys=True)}`.",
                "",
            ]
        )
        if metrics["subtypes"]:
            lines.extend(
                [
                    "| Subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Eligible |",
                    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
                ]
            )
            for subtype, values in sorted(metrics["subtypes"].items()):
                lines.append(
                    f"| `{subtype}` | {values['support']} | {values['groups']} | "
                    f"{values['tp']} | {values['fp']} | {values['fn']} | "
                    f"{_score(values['precision'])} | {_score(values['recall'])} | "
                    f"{_score(values['f1'])} | "
                    f"{'yes' if values['eligible'] else 'no'} |"
                )
            lines.append("")
    lines.extend(
        [
            "## Limitations",
            "",
            "This is a synthetic diagnostic corpus over real production extractors. "
            "It is not an estimate of production prevalence. Labels were frozen before "
            "predictions, but the semantic annotations have not received a human domain-expert audit.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-labels")
    validate_parser.add_argument("--split", choices=("dev", "scored"), default="scored")
    subparsers.add_parser("validate")
    subparsers.add_parser("self-test")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--split", choices=("dev", "scored"), default="dev")
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--force", action="store_true")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--split", choices=("dev", "scored"), default="scored")
    report_parser.add_argument("--predictions", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-labels":
            result = validate_labels(args.split)
        elif args.command == "validate":
            build = _load_build_module()
            result = {
                "corpus": build.validate(),
                "labels": validate_labels("scored"),
                "evaluator": evaluator_self_test(),
            }
        elif args.command == "self-test":
            result = evaluator_self_test()
        elif args.command == "run":
            output = args.output or (PREDICTIONS / f"{args.split}.jsonl")
            result = run_predictions(args.split, output, force=args.force)
        else:
            predictions = args.predictions or (PREDICTIONS / f"{args.split}.jsonl")
            result = report(predictions, args.split)
    except (EvaluationError, ValueError, OSError) as exc:
        parser.exit(1, f"fusion evaluation error: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
