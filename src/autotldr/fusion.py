"""Deterministic, model-free cross-source fusion signals.

This module deliberately stops before bundle projection.  It accepts already
extracted sources and returns transparent match candidates plus abstention
traces.  Callers decide which evaluated candidates become graph relations.

The implementation never consults :class:`~autotldr.unit.Role`.  Stage 4 must
remain useful when every unit is ``unknown`` and must not smuggle Stage 2's
backend-scoped role guarantees into deterministic fusion.
"""

from __future__ import annotations

import json
import math
import os
import posixpath
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence
from urllib.parse import quote, urldefrag, urlsplit, urlunsplit

from .unit import (
    Extraction,
    Gap,
    GapKind,
    GroundedStatement,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Role,
    Unit,
)

LITERAL_SIGNAL = "literal-v1"
IDENTIFIER_SIGNAL = "identifier-v1"
STRUCTURAL_SIGNAL = "structural-v1"
CONTRADICTION_SIGNAL = "contradiction-v1"

# Frozen Stage 4 scored disposition. ``analyze`` remains the transparent raw
# diagnostic surface used by the immutable report; ``fuse`` emits only the
# signals/subtypes that passed the preregistered engineering gate.  Changing a
# matcher now requires a new scored source group rather than a v2 rerun.
STAGE4_EVALUATED_IMPLEMENTATION_SHA256 = (
    "830d42e7efcdf3fd20beac11733acf9276c0484af810e439dd70122de8dc8420"
)
STAGE4_SCORED_PREDICTIONS_SHA256 = (
    "c9fd530184b14219cbc6f409380dd0a1e6ac45c12efda445c90ef5a184d17ac9"
)
STAGE4_SHIPPING_DISPOSITIONS = {
    LITERAL_SIGNAL: {"status": "ship-complete", "subtypes": ()},
    IDENTIFIER_SIGNAL: {
        "status": "ship-preregistered-subtype",
        "subtypes": ("native-native",),
    },
    STRUCTURAL_SIGNAL: {"status": "ship-complete", "subtypes": ()},
    CONTRADICTION_SIGNAL: {"status": "disable", "subtypes": ()},
    "orphan-v1": {"status": "disable", "subtypes": ()},
    "unresolved-v1": {
        "status": "ship-preregistered-subtype",
        "subtypes": ("local-path",),
    },
}

_MAX_IDENTIFIER_CHARS = 128
_MAX_UNIT_TOKENS = 4096
_MAX_CONTAINER_FIELDS = 256
_MAX_PAIR_CANDIDATES = 50_000

_URL_SCHEMES = frozenset({"http", "https"})
_PATH_REFERENCE_KINDS = frozenset(
    {
        "path",
        "include",
        "literalinclude",
        "image",
        "figure",
        "explicit-target",
    }
)
_IMPORT_REFERENCE_KINDS = frozenset({"import", "from-import"})
_LABEL_REFERENCE_KINDS = frozenset({"label"})
_CITATION_REFERENCE_KINDS = frozenset({"citation"})

_COMMON_IDENTIFIER_TOKENS = frozenset(
    {
        "id",
        "name",
        "key",
        "value",
        "values",
        "data",
        "datum",
        "item",
        "items",
        "type",
        "kind",
        "status",
        "result",
        "results",
        "model",
        "models",
        "config",
        "configuration",
        "setting",
        "settings",
        "option",
        "options",
        "object",
        "record",
        "row",
        "rows",
        "column",
        "columns",
        "field",
        "fields",
        "table",
        "test",
        "tests",
        "example",
        "sample",
        "file",
        "files",
        "document",
        "docs",
        "main",
        "run",
        "load",
        "save",
        "create",
        "make",
        "get",
        "set",
        "build",
        "compute",
        "calculate",
        "measure",
        "client",
        "server",
        "request",
        "response",
        "input",
        "output",
        "string",
        "integer",
        "number",
        "boolean",
        "true",
        "false",
        "null",
    }
)
_STRUCTURAL_GENERIC_TOKENS = _COMMON_IDENTIFIER_TOKENS | frozenset(
    {"time", "date", "timestamp", "created", "updated"}
)
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
_IDENTIFIER_NAMESPACE_WRAPPERS = frozenset(
    {
        "column",
        "columns",
        "config",
        "configuration",
        "data",
        "document",
        "item",
        "items",
        "properties",
        "property",
        "record",
        "records",
        "root",
        "row",
        "rows",
        "setting",
        "settings",
        "value",
        "values",
    }
)

_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_LOWER = re.compile(r"([a-z0-9])([A-Z])")
_ALPHA_DIGIT = re.compile(r"([A-Za-z])([0-9])|([0-9])([A-Za-z])")
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_GENERATED_FIELD = re.compile(r"column_\d+|.+ \[\d+\]", re.IGNORECASE)
_DOI = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$", re.IGNORECASE)
_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_PROSE_FACT = re.compile(
    r"^\s*(?:[-*]\s+)?(?:`(?P<backtick>[^`]{1,64})`|"
    r"(?P<plain>[A-Za-z][A-Za-z0-9_.-]{0,63}))\s*"
    r"(?P<operator>=|:)\s*"
    r"(?P<value>true|false|[-+]?(?:\d+(?:\.\d*)?|\.\d+)|"
    r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
    r"(?:\s+(?P<unit>[A-Za-z%][A-Za-z0-9%/_-]{0,15}))?\s*[.;]?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """One accepted, endpoint-exact signal candidate.

    ``relation_kind`` is intentionally a string.  Literal and contradiction
    candidates map to existing relation kinds; identifier and structural
    candidates use the neutral ``corresponds`` spelling whose representation
    integration is owned by the caller.
    """

    signal: str
    relation_kind: str
    src: str
    dst: str
    src_source: str
    dst_source: str
    src_origin: str
    dst_origin: str
    confidence: float
    evidence: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MatchTrace:
    """An accepted or rejected rule decision suitable for eval diagnostics."""

    signal: str
    status: str
    reason: str
    unit_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UnresolvedReference:
    """A source-addressed literal reference that could not resolve safely."""

    reference_id: str
    source: str
    origin: Origin
    ref_kind: str
    raw_target: str
    normalized_target: str
    reason: str
    candidates: tuple[str, ...]
    evidence: str


@dataclass(frozen=True, slots=True)
class FusionSignals:
    """All deterministic Stage 4 signal outputs before graph integration."""

    literal: tuple[MatchCandidate, ...] = ()
    identifier: tuple[MatchCandidate, ...] = ()
    structural: tuple[MatchCandidate, ...] = ()
    contradictions: tuple[MatchCandidate, ...] = ()
    unresolved: tuple[UnresolvedReference, ...] = ()
    traces: tuple[MatchTrace, ...] = ()

    @property
    def accepted(self) -> tuple[MatchCandidate, ...]:
        return (*self.literal, *self.identifier, *self.structural, *self.contradictions)


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    extraction: Extraction
    anchor: Unit | None
    identities: tuple[str, ...]
    basename: str | None
    is_url: bool


@dataclass(frozen=True, slots=True)
class _IdentifierOccurrence:
    source: str
    unit: Unit
    raw: str
    canonical: tuple[str, ...]
    native: bool
    operations: tuple[str, ...]
    namespace: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _Container:
    source: str
    unit: Unit
    family: str
    path: str
    fields: tuple[tuple[str, tuple[str, ...]], ...]
    types: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def field_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.fields)

    @property
    def type_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.types)


@dataclass(frozen=True, slots=True)
class _Fact:
    source: str
    unit: Unit
    raw_key: str
    key: tuple[str, ...]
    raw_value: str
    value_type: str
    canonical_value: str
    unit_name: str | None
    qualified: bool
    basis: str


def analyze(extractions: Sequence[Extraction]) -> FusionSignals:
    """Run all raw deterministic Stage 4 signals over extracted sources.

    Source and candidate order is canonical, so permuting ``extractions`` does
    not alter the result. Duplicate logical sources fail closed. This raw
    surface is retained for audit and future evaluation; production callers
    should use :func:`fuse`, which applies the frozen scored dispositions.
    """

    ordered = _ordered_extractions(extractions)
    records = _source_records(ordered)

    literal, unresolved, literal_traces = _literal_matches(records)
    structural, structural_traces = _structural_matches(ordered)
    identifier, identifier_traces = _identifier_matches(ordered)

    related_pairs = {
        _source_pair(item.src_source, item.dst_source)
        for item in (*literal, *identifier, *structural)
    }
    contradictions, contradiction_traces = _contradiction_matches(
        ordered, related_pairs
    )

    traces = sorted(
        (*literal_traces, *identifier_traces, *structural_traces, *contradiction_traces),
        key=_trace_key,
    )
    return FusionSignals(
        literal=tuple(sorted(literal, key=_candidate_key)),
        identifier=tuple(sorted(identifier, key=_candidate_key)),
        structural=tuple(sorted(structural, key=_candidate_key)),
        contradictions=tuple(sorted(contradictions, key=_candidate_key)),
        unresolved=tuple(sorted(unresolved, key=_unresolved_key)),
        traces=tuple(traces),
    )


run_signals = analyze


def _apply_shipping_dispositions(signals: FusionSignals) -> FusionSignals:
    """Mechanically project raw candidates to the frozen shippable surface."""

    identifier = tuple(
        item
        for item in signals.identifier
        if item.details.get("left_native") is True
        and item.details.get("right_native") is True
    )
    unresolved = tuple(
        item
        for item in signals.unresolved
        if item.ref_kind in _PATH_REFERENCE_KINDS
        and item.reason != "ambiguous-target"
    )
    return FusionSignals(
        literal=signals.literal,
        identifier=identifier,
        structural=signals.structural,
        contradictions=(),
        unresolved=unresolved,
        traces=signals.traces,
    )


def fuse(
    extractions: Sequence[Extraction], *, subject: str = "<collection>"
) -> Extraction:
    """Assemble evaluated deterministic signals into one collection extraction.

    This is intentionally downstream of acquisition: callers provide at least
    two already-extracted inputs.  Original units, relations, gaps, and input
    manifests are preserved; one exact source-manifest anchor is added for each
    source so file-level literal references have an honest unit endpoint.
    """

    import time

    fusion_started = time.perf_counter()
    ordered = _ordered_extractions(extractions)
    if len(ordered) < 2:
        raise ValueError("fusion requires at least two unique input sources")
    if not subject.strip():
        raise ValueError("fusion subject must not be empty")

    for item in ordered:
        input_ids = {unit.id for unit in item.units}
        for relation in item.relations:
            missing = [
                endpoint
                for endpoint in (relation.src, relation.dst)
                if endpoint not in input_ids
            ]
            if missing:
                raise ValueError(
                    f"input relation {relation.src}->{relation.dst} has "
                    f"unresolved endpoint(s): {', '.join(missing)}"
                )

    augmented, anchors = _with_source_anchors(ordered)
    raw_signals = analyze(augmented)
    signals = _apply_shipping_dispositions(raw_signals)
    units = [unit for item in augmented for unit in item.units]
    unit_by_id = {unit.id: unit for unit in units}

    relations = [relation for item in ordered for relation in item.relations]
    for candidate in signals.accepted:
        relations.append(
            Relation(
                src=candidate.src,
                dst=candidate.dst,
                kind=RelationKind(candidate.relation_kind),
                evidence=candidate.evidence,
                confidence=candidate.confidence,
            )
        )

    gaps: list[Gap] = [gap for item in ordered for gap in item.gaps]
    for unresolved in signals.unresolved:
        gaps.append(
            Gap(
                _unresolved_gap_content(unresolved),
                unresolved.origin,
                GapKind.UNRESOLVED_REFERENCE,
            )
        )

    connected_sources = {
        source
        for candidate in signals.accepted
        for source in (candidate.src_source, candidate.dst_source)
        if candidate.src_source != candidate.dst_source
    }
    # Orphan recovery did not meet its frozen recall gate. Keep the mechanical
    # connectivity calculation local so no unmeasured absence finding escapes.
    _unconnected_sources = tuple(
        item.source for item in ordered if item.source not in connected_sources
    )
    orphan_sources: tuple[str, ...] = ()

    summary_claims = _collection_statements(
        ordered,
        anchors,
        signals,
        orphan_sources,
        unit_by_id,
    )
    manifest = _collection_manifest(
        ordered,
        signals,
        orphan_sources,
        raw_signals=raw_signals,
        suppressed_orphan_count=len(_unconnected_sources),
    )
    result = Extraction(
        source=subject,
        kind="collection",
        units=units,
        relations=relations,
        gaps=gaps,
        meta=manifest,
        summary_claims=summary_claims,
    )
    _validate_fused_result(result)
    result.meta["timings"]["fusion_ms"] = round(
        (time.perf_counter() - fusion_started) * 1000.0, 3
    )
    return result


def _with_source_anchors(
    extractions: Sequence[Extraction],
) -> tuple[tuple[Extraction, ...], tuple[Unit, ...]]:
    augmented: list[Extraction] = []
    anchors: list[Unit] = []
    for extraction in extractions:
        existing = [
            unit
            for unit in extraction.units
            if unit.meta.get("source_anchor") is True
        ]
        if len(existing) > 1:
            raise ValueError(
                f"source {extraction.source!r} has multiple source anchor units"
            )
        if existing:
            anchor = existing[0]
            if (
                anchor.modality is not Modality.SOURCE
                or anchor.origin != Origin(extraction.source, "source")
                or anchor.role is not Role.UNKNOWN
            ):
                raise ValueError(
                    f"source {extraction.source!r} has an invalid source anchor"
                )
            remaining = [unit for unit in extraction.units if unit.id != anchor.id]
        else:
            anchor = _source_anchor(extraction)
            remaining = list(extraction.units)
        anchors.append(anchor)
        augmented.append(
            Extraction(
                source=extraction.source,
                kind=extraction.kind,
                units=[anchor, *remaining],
                relations=list(extraction.relations),
                gaps=list(extraction.gaps),
                meta=dict(extraction.meta),
                summary_claims=list(extraction.summary_claims),
            )
        )
    return tuple(augmented), tuple(anchors)


def _source_anchor(extraction: Extraction) -> Unit:
    manifest = _source_manifest_fact(extraction)
    labels = extraction.meta.get("labels")
    literal_labels = sorted(
        {label for label in labels if isinstance(label, str) and label}
    ) if isinstance(labels, list) else []
    content = "Source manifest: " + json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return Unit(
        source=extraction.source,
        modality=Modality.SOURCE,
        content=content,
        origin=Origin(extraction.source, "source"),
        role=Role.UNKNOWN,
        salience=1.0,
        confidence=1.0,
        meta={
            "source_anchor": True,
            "source_kind": extraction.kind,
            "manifest": manifest,
            # Definitions are literal-resolution metadata. They intentionally
            # never enter _native_identifiers.
            "literal_labels": literal_labels or None,
        },
    )


def _source_manifest_fact(extraction: Extraction) -> dict[str, Any]:
    inputs = extraction.meta.get("inputs")
    if isinstance(inputs, list):
        exact = [
            item
            for item in inputs
            if isinstance(item, dict) and item.get("source") == extraction.source
        ]
        if len(exact) == 1:
            return _json_record(exact[0])
        if len(inputs) == 1 and isinstance(inputs[0], dict):
            return _json_record(inputs[0])
    # Library-created Extractions need an explicit fallback, not invented byte
    # counts or hashes. Production acquisitions carry the exact Stage 3 record.
    return {
        "source": extraction.source,
        "kind": extraction.kind,
        "manifest_available": False,
    }


def _collection_statements(
    inputs: Sequence[Extraction],
    anchors: Sequence[Unit],
    signals: FusionSignals,
    orphan_sources: tuple[str, ...],
    unit_by_id: dict[str, Unit],
) -> list[GroundedStatement]:
    anchor_ids = tuple(anchor.id for anchor in anchors)
    concepts: dict[tuple[str, ...], set[str]] = defaultdict(set)
    concept_evidence: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for candidate in signals.identifier:
        raw = candidate.details.get("canonical")
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            continue
        concept = tuple(raw)
        concepts[concept].update((candidate.src_source, candidate.dst_source))
        concept_evidence[concept].update((candidate.src, candidate.dst))
    ranked_concepts = sorted(
        concepts,
        key=lambda concept: (-len(concepts[concept]), concept),
    )[:2]
    if ranked_concepts:
        names = [_display_concept(concept) for concept in ranked_concepts]
        statement_one = (
            f"This {len(inputs)}-source collection has addressable material "
            f"connected by the shared identifier{'' if len(names) == 1 else 's'} "
            f"{_join_quoted(names)}."
        )
        statement_one_ids = tuple(
            sorted(
                {
                    unit_id
                    for concept in ranked_concepts
                    for unit_id in concept_evidence[concept]
                }
            )
        )
    else:
        kinds = len({item.kind for item in inputs})
        statement_one = (
            f"This collection contains {len(inputs)} addressable source(s) "
            f"spanning {kinds} native format(s)."
        )
        statement_one_ids = anchor_ids

    linkage = (*signals.literal, *signals.identifier, *signals.structural)
    statement_two = (
        "Deterministic literal, identifier, and structural signals produced "
        f"{len(linkage)} cross-source relation candidate(s) "
        f"({len(signals.literal)} literal, {len(signals.identifier)} identifier, "
        f"and {len(signals.structural)} structural)."
    )
    statement_two_ids = tuple(
        sorted({unit_id for item in linkage for unit_id in (item.src, item.dst)})
    ) or anchor_ids

    if signals.unresolved:
        statement_three = (
            f"The measured local-path policy found {len(signals.unresolved)} "
            "source-grounded reference(s) without a unique collection target."
        )
    else:
        statement_three = (
            "No unresolved local-path reference was recovered under the measured "
            f"policy for these {len(inputs)} source(s)."
        )
    unresolved_ids = {item.reference_id for item in signals.unresolved}
    orphan_ids = {anchor_by_source.id for anchor_by_source in anchors if anchor_by_source.source in orphan_sources}
    statement_three_ids = tuple(
        sorted(
            unresolved_ids
            | orphan_ids
            | {
                unit_id
                for item in signals.contradictions
                for unit_id in (item.src, item.dst)
            }
        )
    ) or anchor_ids

    return [
        _grounded_statement(statement_one, statement_one_ids, unit_by_id),
        _grounded_statement(statement_two, statement_two_ids, unit_by_id),
        _grounded_statement(statement_three, statement_three_ids, unit_by_id),
    ]


def _grounded_statement(
    content: str,
    evidence_ids: Sequence[str],
    unit_by_id: dict[str, Unit],
) -> GroundedStatement:
    ids = tuple(dict.fromkeys(evidence_ids))
    missing = [unit_id for unit_id in ids if unit_id not in unit_by_id]
    if missing:
        raise ValueError(
            "collection statement has unresolved evidence unit id(s): "
            + ", ".join(missing)
        )
    origins: list[Origin] = []
    seen_origins: set[Origin] = set()
    for unit_id in ids:
        origin = unit_by_id[unit_id].origin
        if origin not in seen_origins:
            origins.append(origin)
            seen_origins.add(origin)
    return GroundedStatement(content, tuple(origins), ids)


def _collection_manifest(
    inputs: Sequence[Extraction],
    signals: FusionSignals,
    orphan_sources: tuple[str, ...],
    *,
    raw_signals: FusionSignals,
    suppressed_orphan_count: int,
) -> dict[str, Any]:
    input_manifests: list[dict[str, Any]] = []
    per_source_timings: list[dict[str, Any]] = []
    aggregate_timings: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for item in inputs:
        raw_inputs = item.meta.get("inputs")
        if isinstance(raw_inputs, list) and raw_inputs:
            input_manifests.extend(
                _json_record(record)
                for record in raw_inputs
                if isinstance(record, dict)
            )
        else:
            input_manifests.append(_source_manifest_fact(item))
        raw_timings = item.meta.get("timings")
        timings: dict[str, Any] = {}
        if isinstance(raw_timings, dict):
            for key, value in sorted(raw_timings.items()):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    timings[key] = value
                    aggregate_timings[key] += Decimal(str(value))
        per_source_timings.append({"source": item.source, **timings})

    by_status: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for trace in signals.traces:
        by_status[trace.status] += 1
        by_reason[f"{trace.signal}:{trace.reason}"] += 1

    signal_groups = {
        LITERAL_SIGNAL: signals.literal,
        IDENTIFIER_SIGNAL: signals.identifier,
        STRUCTURAL_SIGNAL: signals.structural,
        CONTRADICTION_SIGNAL: signals.contradictions,
    }
    raw_signal_groups = {
        LITERAL_SIGNAL: raw_signals.literal,
        IDENTIFIER_SIGNAL: raw_signals.identifier,
        STRUCTURAL_SIGNAL: raw_signals.structural,
        CONTRADICTION_SIGNAL: raw_signals.contradictions,
    }
    policies: dict[str, dict[str, Any]] = {
        LITERAL_SIGNAL: {
            "acceptance": "unique exact or lexically normalized source identity",
            "ambiguous": "abstain",
        },
        IDENTIFIER_SIGNAL: {
            "anchor_required": True,
            "single_discriminative_token_min_chars": 6,
            "common_token_suppression": "fixed-v1",
        },
        STRUCTURAL_SIGNAL: {
            "minimum_discriminative_fields": 3,
            "minimum_jaccard": {"numerator": 3, "denominator": 4},
            "minimum_type_compatibility": {"numerator": 4, "denominator": 5},
        },
        CONTRADICTION_SIGNAL: {
            "explicit_scalar_only": True,
            "ambiguous_within_source": "abstain",
            "different_units": "abstain",
        },
    }
    candidates = {
        signal: [_candidate_record(item) for item in values]
        for signal, values in signal_groups.items()
    }
    unresolved = [
        {
            "reference_id": item.reference_id,
            "source": item.source,
            "origin": _origin_record(item.origin),
            "ref_kind": item.ref_kind,
            "raw_target": item.raw_target,
            "normalized_target": item.normalized_target,
            "reason": item.reason,
            "candidates": list(item.candidates),
        }
        for item in signals.unresolved
    ]
    return {
        "inputs": input_manifests,
        "timings": {
            **{
                key: _decimal_number(value)
                for key, value in sorted(aggregate_timings.items())
            },
            "per_source": per_source_timings,
        },
        "models": [],
        "fusion": {
            "backend": "deterministic-signals-v1",
            "signals": {
                signal: {
                    "version": signal,
                    "accepted": len(signal_groups[signal]),
                    "raw_before_disposition": len(raw_signal_groups[signal]),
                    "disposition": _disposition_record(signal),
                    "policy": policies[signal],
                }
                for signal in (
                    LITERAL_SIGNAL,
                    IDENTIFIER_SIGNAL,
                    STRUCTURAL_SIGNAL,
                    CONTRADICTION_SIGNAL,
                )
            },
            "candidates": candidates,
            "trace_counts": {
                "total": len(signals.traces),
                "by_status": dict(sorted(by_status.items())),
                "by_signal_and_reason": dict(sorted(by_reason.items())),
            },
            "unresolved_references": unresolved,
            "orphans": list(orphan_sources),
            "evaluated_dispositions": {
                "evaluated_implementation_sha256": STAGE4_EVALUATED_IMPLEMENTATION_SHA256,
                "scored_predictions_sha256": STAGE4_SCORED_PREDICTIONS_SHA256,
                "signals": {
                    name: _disposition_record(name)
                    for name in STAGE4_SHIPPING_DISPOSITIONS
                },
                "unresolved_raw_before_disposition": len(raw_signals.unresolved),
                "orphan_candidates_suppressed": suppressed_orphan_count,
            },
            "source_anchor_scheme": "manifest-facts-origin-source-v1",
        },
    }


def _disposition_record(name: str) -> dict[str, Any]:
    disposition = STAGE4_SHIPPING_DISPOSITIONS[name]
    return {
        "status": disposition["status"],
        "subtypes": list(disposition["subtypes"]),
    }


def _candidate_record(item: MatchCandidate) -> dict[str, Any]:
    return {
        "signal": item.signal,
        "relation_kind": item.relation_kind,
        "src": item.src,
        "dst": item.dst,
        "src_source": item.src_source,
        "dst_source": item.dst_source,
        "src_origin": item.src_origin,
        "dst_origin": item.dst_origin,
        "confidence": item.confidence,
        "details": _json_record(item.details),
    }


def _unresolved_gap_content(item: UnresolvedReference) -> str:
    target = json.dumps(item.raw_target, ensure_ascii=False)
    if item.reason == "ambiguous-target":
        return (
            f"Reference {target} is ambiguous within this collection; it "
            f"matches {len(item.candidates)} input sources."
        )
    if item.reason == "citation-definition-not-addressable":
        return (
            f"Citation key {target} has no addressable definition within this "
            "collection."
        )
    if item.reason == "target-source-has-no-addressable-unit":
        return (
            f"Reference {target} resolves to an input source with no "
            "addressable target unit."
        )
    return f"Reference {target} has no addressable target within this collection."


def _validate_fused_result(result: Extraction) -> None:
    ids: set[str] = set()
    for index, unit in enumerate(result.units):
        if unit.id in ids:
            raise ValueError(
                f"fused collection has duplicate unit id {unit.id} at index {index}"
            )
        ids.add(unit.id)
    for relation in result.relations:
        missing = [endpoint for endpoint in (relation.src, relation.dst) if endpoint not in ids]
        if missing:
            raise ValueError(
                f"fused relation {relation.src}->{relation.dst} has unresolved "
                f"endpoint(s): {', '.join(missing)}"
            )
    for statement in result.summary_claims:
        missing = [unit_id for unit_id in statement.evidence_unit_ids if unit_id not in ids]
        if missing:
            raise ValueError(
                f"grounded statement {statement.id} has unresolved evidence "
                f"unit id(s): {', '.join(missing)}"
            )


def _display_concept(concept: tuple[str, ...]) -> str:
    return "_".join(concept)


def _join_quoted(values: Sequence[str]) -> str:
    shown = [f"`{value}`" for value in values]
    if len(shown) == 1:
        return shown[0]
    return " and ".join(shown)


def _json_record(value: dict[str, Any]) -> dict[str, Any]:
    # Make metadata independent from caller-owned mutable dictionaries and
    # reject values that cannot enter the machine wire shapes.
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _origin_record(origin: Origin) -> dict[str, Any]:
    return {
        "source": origin.source,
        "ref": origin.ref,
        "char_span": list(origin.char_span) if origin.char_span is not None else None,
    }


def _decimal_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    return float(value)


def _ordered_extractions(extractions: Sequence[Extraction]) -> tuple[Extraction, ...]:
    ordered = tuple(sorted(extractions, key=lambda item: item.source))
    sources = [item.source for item in ordered]
    if len(sources) != len(set(sources)):
        raise ValueError("fusion requires unique logical extraction sources")

    ids: dict[str, tuple[str, int]] = {}
    for extraction in ordered:
        for index, unit in enumerate(extraction.units):
            previous = ids.get(unit.id)
            if previous is not None:
                raise ValueError(
                    f"duplicate unit id {unit.id} at "
                    f"{previous[0]} index {previous[1]} and "
                    f"{extraction.source} index {index}"
                )
            ids[unit.id] = (extraction.source, index)
    return ordered


def _source_records(extractions: Sequence[Extraction]) -> tuple[_SourceRecord, ...]:
    records: list[_SourceRecord] = []
    for extraction in extractions:
        source_url = _canonical_url(extraction.source)
        identities: set[str] = set()
        if source_url is not None:
            identities.add("url:" + source_url)
        elif _local_source(extraction.source) is not None:
            identities.add("path:" + _local_source(extraction.source))

        requested = extraction.meta.get("requested_url")
        final = extraction.meta.get("final_url")
        for value in (requested, final):
            if isinstance(value, str) and (canonical := _canonical_url(value)):
                identities.add("url:" + canonical)
        for item in extraction.meta.get("inputs", ()):  # Stage 3 manifest
            if not isinstance(item, dict) or not isinstance(item.get("source"), str):
                continue
            value = item["source"]
            if canonical := _canonical_url(value):
                identities.add("url:" + canonical)
            elif (local := _local_source(value)) is not None:
                identities.add("path:" + local)

        basename = None
        if source_url is None and (local := _local_source(extraction.source)):
            basename = os.path.basename(local)
        records.append(
            _SourceRecord(
                extraction=extraction,
                anchor=_target_anchor(extraction),
                identities=tuple(sorted(identities)),
                basename=basename,
                is_url=source_url is not None,
            )
        )
    return tuple(records)


def _target_anchor(extraction: Extraction) -> Unit | None:
    if not extraction.units:
        return None

    def key(unit: Unit) -> tuple[Any, ...]:
        source_anchor = unit.meta.get("source_anchor") is True
        root_heading = bool(unit.meta.get("heading")) and len(unit.structure) <= 1
        native_root = bool(
            unit.meta.get("table_summary")
            or unit.meta.get("sheet_summary")
            or (unit.modality is Modality.SCHEMA and not unit.structure)
        )
        return (
            0 if source_anchor else (1 if root_heading else (2 if native_root else 3)),
            len(unit.structure),
            -unit.salience,
            unit.origin.ref,
            unit.id,
        )

    return min(extraction.units, key=key)


def _literal_matches(
    records: Sequence[_SourceRecord],
) -> tuple[list[MatchCandidate], list[UnresolvedReference], list[MatchTrace]]:
    identity_index: dict[str, list[_SourceRecord]] = defaultdict(list)
    basename_index: dict[str, list[_SourceRecord]] = defaultdict(list)
    label_index: dict[str, list[tuple[_SourceRecord, Unit]]] = defaultdict(list)
    for record in records:
        for identity in record.identities:
            identity_index[identity].append(record)
        if record.basename:
            basename_index[record.basename].append(record)
        for unit in record.extraction.units:
            for label_field in ("labels", "literal_labels"):
                labels = unit.meta.get(label_field)
                if isinstance(labels, list):
                    for label in labels:
                        if isinstance(label, str) and label:
                            label_index[label].append((record, unit))
        extraction_labels = record.extraction.meta.get("labels")
        if record.anchor is not None and isinstance(extraction_labels, list):
            for label in extraction_labels:
                if isinstance(label, str) and label:
                    label_index[label].append((record, record.anchor))

    accepted: list[MatchCandidate] = []
    unresolved: list[UnresolvedReference] = []
    traces: list[MatchTrace] = []

    for record in records:
        references = sorted(
            (unit for unit in record.extraction.units if unit.modality is Modality.REFERENCE),
            key=lambda unit: (unit.origin.ref, unit.id),
        )
        for reference in references:
            raw_target = reference.meta.get("target", reference.content)
            ref_kind = str(reference.meta.get("ref_kind") or "unknown").casefold()
            if not isinstance(raw_target, str) or not raw_target.strip():
                traces.append(_reference_trace(reference, "rejected", "empty-target"))
                continue
            raw_target = raw_target.strip()

            matches: list[tuple[_SourceRecord, Unit | None, str, str, float]] = []
            normalized = raw_target
            report_missing = False

            if ref_kind in _LABEL_REFERENCE_KINDS:
                normalized = unicodedata.normalize("NFC", raw_target)
                for target_record, target_unit in label_index.get(normalized, ()):
                    matches.append((target_record, target_unit, "explicit-label", normalized, 1.0))
                report_missing = True
            elif ref_kind in _CITATION_REFERENCE_KINDS:
                normalized = unicodedata.normalize("NFC", raw_target)
                # Bibliography definitions need their own addressable units.
                report_missing = True
            elif ref_kind == "doi" or _looks_like_doi(raw_target):
                normalized = _canonical_doi(raw_target) or raw_target
                identity = "url:https://doi.org/" + normalized if _canonical_doi(raw_target) else ""
                for target_record in identity_index.get(identity, ()):
                    matches.append((target_record, None, "doi-source-identity", normalized, 1.0))
                # External DOIs are resolvable identities, not collection gaps.
            elif ref_kind == "url" or _canonical_url(raw_target) is not None:
                canonical = _canonical_url(raw_target)
                normalized = canonical or raw_target
                if canonical:
                    for target_record in identity_index.get("url:" + canonical, ()):
                        matches.append((target_record, None, "url-identity", canonical, 1.0))
            elif ref_kind in _IMPORT_REFERENCE_KINDS:
                normalized, import_identities = _relative_import_targets(
                    record.extraction.source, raw_target
                )
                for identity in import_identities:
                    for target_record in identity_index.get(identity, ()):
                        matches.append((target_record, None, "relative-import", normalized, 0.99))
                report_missing = raw_target.startswith(".")
            elif ref_kind in _PATH_REFERENCE_KINDS or _looks_like_local_path(raw_target):
                normalized, identities, basename = _path_targets(
                    record.extraction.source, raw_target
                )
                for identity in identities:
                    for target_record in identity_index.get(identity, ()):
                        matches.append((target_record, None, "path-identity", normalized, 1.0))
                if not matches and basename and raw_target == basename:
                    for target_record in basename_index.get(basename, ()):
                        matches.append((target_record, None, "unique-basename", normalized, 0.95))
                report_missing = True
            else:
                traces.append(
                    _reference_trace(
                        reference,
                        "rejected",
                        "unsupported-reference-kind",
                        {"ref_kind": ref_kind, "raw_target": raw_target},
                    )
                )
                continue

            # Remove duplicate aliases and same-source non-fusion resolutions.
            unique: dict[tuple[str, str], tuple[_SourceRecord, Unit | None, str, str, float]] = {}
            intra_source: dict[
                tuple[str, str], tuple[_SourceRecord, Unit | None, str, str, float]
            ] = {}
            for item in matches:
                target_record, target_unit, resolution, normalized_value, confidence = item
                if target_record.extraction.source == record.extraction.source:
                    intra_source[(
                        target_record.extraction.source,
                        target_unit.id if target_unit else "",
                    )] = item
                    continue
                unit_key = target_unit.id if target_unit else ""
                key = (target_record.extraction.source, unit_key)
                previous = unique.get(key)
                if previous is None or confidence > previous[4]:
                    unique[key] = item
            matches = sorted(
                unique.values(),
                key=lambda item: (
                    item[0].extraction.source,
                    item[1].id if item[1] else "",
                    item[2],
                ),
            )

            # Native labels are scoped to their defining document. A local
            # definition wins even when another source happens to reuse the
            # same spelling; cross-document label collision is not a link.
            if intra_source and (
                not matches or ref_kind in _LABEL_REFERENCE_KINDS
            ):
                traces.append(
                    _reference_trace(
                        reference,
                        "resolved",
                        "intra-source-resolution",
                        {
                            "ref_kind": ref_kind,
                            "raw_target": raw_target,
                            "normalized_target": normalized,
                            "targets": sorted(
                                {
                                    item[1].id if item[1] else item[0].extraction.source
                                    for item in intra_source.values()
                                }
                            ),
                        },
                    )
                )
                continue

            if len(matches) == 1:
                target_record, explicit_unit, resolution, normalized, confidence = matches[0]
                target = explicit_unit or target_record.anchor
                if target is None:
                    item = _unresolved(
                        reference,
                        ref_kind,
                        raw_target,
                        normalized,
                        "target-source-has-no-addressable-unit",
                        (target_record.extraction.source,),
                    )
                    unresolved.append(item)
                    traces.append(_unresolved_trace(item))
                    continue
                payload = {
                    "raw_target": raw_target,
                    "normalized_target": normalized,
                    "ref_kind": ref_kind,
                    "resolution": resolution,
                    "target_source": target_record.extraction.source,
                    "target_anchor": target.id,
                    "target_anchor_policy": (
                        "explicit-definition" if explicit_unit else "representative-source-anchor-v1"
                    ),
                }
                candidate = _candidate(
                    LITERAL_SIGNAL,
                    "references",
                    reference,
                    target,
                    confidence,
                    payload,
                )
                accepted.append(candidate)
                traces.append(_accepted_trace(candidate, "unique-resolution"))
                continue

            if len(matches) > 1:
                item = _unresolved(
                    reference,
                    ref_kind,
                    raw_target,
                    normalized,
                    "ambiguous-target",
                    tuple(sorted(match[0].extraction.source for match in matches)),
                )
                unresolved.append(item)
                traces.append(_unresolved_trace(item))
            elif report_missing:
                reason = (
                    "citation-definition-not-addressable"
                    if ref_kind in _CITATION_REFERENCE_KINDS
                    else "target-not-in-collection"
                )
                item = _unresolved(
                    reference,
                    ref_kind,
                    raw_target,
                    normalized,
                    reason,
                    (),
                )
                unresolved.append(item)
                traces.append(_unresolved_trace(item))
            else:
                traces.append(
                    _reference_trace(
                        reference,
                        "rejected",
                        "external-reference",
                        {
                            "ref_kind": ref_kind,
                            "raw_target": raw_target,
                            "normalized_target": normalized,
                        },
                    )
                )

    return accepted, unresolved, traces


def _identifier_matches(
    extractions: Sequence[Extraction],
) -> tuple[list[MatchCandidate], list[MatchTrace]]:
    units = sorted(
        (unit for extraction in extractions for unit in extraction.units),
        key=lambda unit: (unit.source, unit.origin.ref, unit.id),
    )
    tokenized: dict[str, tuple[str, ...]] = {}
    token_index: dict[str, set[str]] = defaultdict(set)
    unit_by_id = {unit.id: unit for unit in units}
    for unit in units:
        if unit.modality in {Modality.SOURCE, Modality.REFERENCE}:
            tokenized[unit.id] = ()
            continue
        tokens = _text_tokens(unit.content)
        if len(tokens) > _MAX_UNIT_TOKENS:
            tokens = tokens[:_MAX_UNIT_TOKENS]
        tokenized[unit.id] = tokens
        for token in set(tokens):
            token_index[token].add(unit.id)

    concepts: dict[tuple[str, ...], list[_IdentifierOccurrence]] = defaultdict(list)
    traces: list[MatchTrace] = []
    for unit in units:
        for raw in _native_identifiers(unit):
            canonical, operations = _normalize_identifier(raw, conceptual=True)
            if not _eligible_identifier(canonical):
                traces.append(
                    MatchTrace(
                        signal=IDENTIFIER_SIGNAL,
                        status="rejected",
                        reason="common-or-weak-identifier",
                        unit_ids=(unit.id,),
                        sources=(unit.source,),
                        detail=_evidence(
                            IDENTIFIER_SIGNAL,
                            {"raw": raw, "canonical": list(canonical)},
                        ),
                    )
                )
                continue
            concepts[canonical].append(
                _IdentifierOccurrence(
                    source=unit.source,
                    unit=unit,
                    raw=raw,
                    canonical=canonical,
                    native=True,
                    operations=operations,
                    namespace=_identifier_namespace(unit),
                )
            )

    accepted: list[MatchCandidate] = []
    source_count = max(1, len(extractions))
    for canonical, native_occurrences in sorted(concepts.items()):
        namespaces = {
            occurrence.namespace
            for occurrence in native_occurrences
            if occurrence.namespace is not None
        }
        if len(namespaces) > 1:
            traces.append(
                MatchTrace(
                    signal=IDENTIFIER_SIGNAL,
                    status="rejected",
                    reason="conflicting-qualified-namespaces",
                    unit_ids=tuple(
                        sorted({item.unit.id for item in native_occurrences})
                    ),
                    sources=tuple(
                        sorted({item.source for item in native_occurrences})
                    ),
                    detail=_evidence(
                        IDENTIFIER_SIGNAL,
                        {
                            "canonical": list(canonical),
                            "namespaces": [list(item) for item in sorted(namespaces)],
                        },
                    ),
                )
            )
            continue

        candidate_ids = set(token_index.get(canonical[0], ()))
        for token in canonical[1:]:
            candidate_ids &= token_index.get(token, set())

        occurrences = list(native_occurrences)
        native_ids = {item.unit.id for item in native_occurrences}
        for unit_id in sorted(candidate_ids):
            if unit_id in native_ids:
                continue
            tokens = tokenized[unit_id]
            if not _contains_sequence(tokens, canonical):
                continue
            unit = unit_by_id[unit_id]
            occurrences.append(
                _IdentifierOccurrence(
                    source=unit.source,
                    unit=unit,
                    raw=" ".join(canonical),
                    canonical=canonical,
                    native=False,
                    operations=("anchored-content-occurrence",),
                    namespace=None,
                )
            )

        by_source: dict[str, list[_IdentifierOccurrence]] = defaultdict(list)
        for occurrence in occurrences:
            by_source[occurrence.source].append(occurrence)
        if len(by_source) < 2:
            continue
        if len(by_source) > max(4, math.ceil(source_count * 0.75)):
            traces.append(
                MatchTrace(
                    signal=IDENTIFIER_SIGNAL,
                    status="rejected",
                    reason="collection-ubiquitous-identifier",
                    sources=tuple(sorted(by_source)),
                    detail=_evidence(
                        IDENTIFIER_SIGNAL,
                        {"canonical": list(canonical), "source_count": len(by_source)},
                    ),
                )
            )
            continue

        best = {
            source: min(items, key=_occurrence_key)
            for source, items in by_source.items()
        }
        anchor = min(best.values(), key=_occurrence_key)
        for source in sorted(best):
            other = best[source]
            if other.unit.id == anchor.unit.id:
                continue
            src, dst = _ordered_units(anchor.unit, other.unit)
            left = anchor if src.id == anchor.unit.id else other
            right = other if dst.id == other.unit.id else anchor
            operations = sorted(set((*left.operations, *right.operations)))
            transformed = any(
                operation != "ascii-casefold" for operation in operations
            )
            native_native = left.native and right.native
            confidence = 0.95 if native_native and not transformed else (
                0.90 if native_native else (0.82 if transformed else 0.86)
            )
            payload = {
                "canonical": list(canonical),
                "left_raw": left.raw,
                "right_raw": right.raw,
                "left_native": left.native,
                "right_native": right.native,
                "left_namespace": list(left.namespace) if left.namespace else None,
                "right_namespace": list(right.namespace) if right.namespace else None,
                "normalizations": operations,
            }
            candidate = _candidate(
                IDENTIFIER_SIGNAL,
                "corresponds",
                src,
                dst,
                confidence,
                payload,
            )
            accepted.append(candidate)
            traces.append(_accepted_trace(candidate, "anchored-identifier"))

    return accepted, traces


def _structural_matches(
    extractions: Sequence[Extraction],
) -> tuple[list[MatchCandidate], list[MatchTrace]]:
    containers = _containers(extractions)
    by_field: dict[str, list[int]] = defaultdict(list)
    for index, container in enumerate(containers):
        for field in container.field_map:
            by_field[field].append(index)

    overlap: dict[tuple[int, int], int] = defaultdict(int)
    pair_count = 0
    for indexes in by_field.values():
        unique = sorted(set(indexes))
        for left_pos, left in enumerate(unique):
            for right in unique[left_pos + 1 :]:
                if containers[left].source == containers[right].source:
                    continue
                pair = (left, right)
                if pair not in overlap:
                    pair_count += 1
                    if pair_count > _MAX_PAIR_CANDIDATES:
                        raise ValueError(
                            "structural fusion exceeds bounded candidate-pair limit"
                        )
                overlap[pair] += 1

    accepted: list[MatchCandidate] = []
    traces: list[MatchTrace] = []
    for (left_index, right_index), _count in sorted(overlap.items()):
        left = containers[left_index]
        right = containers[right_index]
        left_fields = set(left.field_map)
        right_fields = set(right.field_map)
        shared = sorted(left_fields & right_fields)
        discriminative = [
            field
            for field in shared
            if not _generic_field(left.field_map[field])
        ]
        union = sorted(left_fields | right_fields)
        required_shared = 3 if "table" in {left.family, right.family} else 4
        required_jaccard = Decimal("0.75") if "table" in {left.family, right.family} else Decimal("0.85")
        jaccard = Decimal(len(shared)) / Decimal(len(union)) if union else Decimal(0)

        typed = 0
        compatible = 0
        type_evidence: list[dict[str, Any]] = []
        for field in shared:
            left_types = set(left.type_map.get(field, ())) - {"null"}
            right_types = set(right.type_map.get(field, ())) - {"null"}
            if not left_types or not right_types:
                continue
            typed += 1
            is_compatible = _types_compatible(left_types, right_types)
            compatible += int(is_compatible)
            type_evidence.append(
                {
                    "field": field,
                    "left": sorted(left_types),
                    "right": sorted(right_types),
                    "compatible": is_compatible,
                }
            )
        type_ok = typed == 0 or compatible * 5 >= typed * 4
        family_ok = _families_compatible(left, right)
        accepted_pair = (
            family_ok
            and len(discriminative) >= required_shared
            and jaccard >= required_jaccard
            and type_ok
        )
        payload = {
            "left_path": left.path,
            "right_path": right.path,
            "left_family": left.family,
            "right_family": right.family,
            "shared": shared,
            "discriminative_shared": discriminative,
            "left_only": sorted(left_fields - right_fields),
            "right_only": sorted(right_fields - left_fields),
            "jaccard": {"numerator": len(shared), "denominator": len(union)},
            "type_compatible": {"numerator": compatible, "denominator": typed},
            "types": type_evidence,
        }
        if not accepted_pair:
            reason = (
                "incompatible-container-families"
                if not family_ok
                else (
                    "insufficient-discriminative-fields"
                    if len(discriminative) < required_shared
                    else (
                        "low-field-jaccard"
                        if jaccard < required_jaccard
                        else "incompatible-field-types"
                    )
                )
            )
            traces.append(
                MatchTrace(
                    signal=STRUCTURAL_SIGNAL,
                    status="rejected",
                    reason=reason,
                    unit_ids=(left.unit.id, right.unit.id),
                    sources=(left.source, right.source),
                    detail=_evidence(STRUCTURAL_SIGNAL, payload),
                )
            )
            continue

        src, dst = _ordered_units(left.unit, right.unit)
        confidence = min(0.98, float(Decimal("0.80") + jaccard * Decimal("0.18")))
        candidate = _candidate(
            STRUCTURAL_SIGNAL,
            "corresponds",
            src,
            dst,
            confidence,
            payload,
        )
        accepted.append(candidate)
        traces.append(_accepted_trace(candidate, "native-container-correspondence"))

    return accepted, traces


def _contradiction_matches(
    extractions: Sequence[Extraction],
    related_pairs: set[tuple[str, str]],
) -> tuple[list[MatchCandidate], list[MatchTrace]]:
    facts: list[_Fact] = []
    traces: list[MatchTrace] = []
    for extraction in extractions:
        for unit in sorted(extraction.units, key=lambda item: (item.origin.ref, item.id)):
            facts.extend(_unit_facts(unit))

    by_key_source: dict[tuple[str, ...], dict[str, list[_Fact]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for fact in facts:
        by_key_source[fact.key][fact.source].append(fact)

    accepted: list[MatchCandidate] = []
    for key, source_facts in sorted(by_key_source.items()):
        if len(key) < 2:
            traces.append(
                MatchTrace(
                    signal=CONTRADICTION_SIGNAL,
                    status="rejected",
                    reason="unqualified-fact-key",
                    unit_ids=tuple(
                        sorted(
                            {
                                fact.unit.id
                                for items in source_facts.values()
                                for fact in items
                            }
                        )
                    ),
                    sources=tuple(sorted(source_facts)),
                    detail=_evidence(
                        CONTRADICTION_SIGNAL,
                        {"canonical_key": list(key)},
                    ),
                )
            )
            continue
        resolved: dict[str, _Fact] = {}
        ambiguous_sources: set[str] = set()
        for source, items in sorted(source_facts.items()):
            values = {
                (item.value_type, item.canonical_value, item.unit_name)
                for item in items
            }
            if len(values) != 1:
                ambiguous_sources.add(source)
                traces.append(
                    MatchTrace(
                        signal=CONTRADICTION_SIGNAL,
                        status="rejected",
                        reason="multiple-values-within-source",
                        unit_ids=tuple(sorted({item.unit.id for item in items})),
                        sources=(source,),
                        detail=_evidence(
                            CONTRADICTION_SIGNAL,
                            {
                                "canonical_key": list(key),
                                "values": sorted(
                                    [list(value) for value in values],
                                    key=lambda value: tuple(str(item) for item in value),
                                ),
                            },
                        ),
                    )
                )
                continue
            resolved[source] = min(
                items, key=lambda item: (item.unit.origin.ref, item.unit.id, item.raw_value)
            )

        sources = sorted(resolved)
        for position, left_source in enumerate(sources):
            for right_source in sources[position + 1 :]:
                left = resolved[left_source]
                right = resolved[right_source]
                if not _comparable_facts(left, right):
                    continue
                if left.canonical_value == right.canonical_value:
                    traces.append(
                        MatchTrace(
                            signal=CONTRADICTION_SIGNAL,
                            status="rejected",
                            reason="canonically-equal-values",
                            unit_ids=(left.unit.id, right.unit.id),
                            sources=(left_source, right_source),
                            detail=_evidence(
                                CONTRADICTION_SIGNAL,
                                {
                                    "canonical_key": list(key),
                                    "left_raw": left.raw_value,
                                    "right_raw": right.raw_value,
                                    "canonical_value": left.canonical_value,
                                },
                            ),
                        )
                    )
                    continue
                pair = _source_pair(left_source, right_source)
                specific_key = len([token for token in key if token not in _COMMON_IDENTIFIER_TOKENS]) >= 2
                if pair not in related_pairs and not (
                    left.qualified and right.qualified and specific_key
                ):
                    traces.append(
                        MatchTrace(
                            signal=CONTRADICTION_SIGNAL,
                            status="rejected",
                            reason="unrelated-source-context",
                            unit_ids=(left.unit.id, right.unit.id),
                            sources=pair,
                            detail=_evidence(
                                CONTRADICTION_SIGNAL,
                                {"canonical_key": list(key)},
                            ),
                        )
                    )
                    continue

                src, dst = _ordered_units(left.unit, right.unit)
                first = left if src.id == left.unit.id else right
                second = right if dst.id == right.unit.id else left
                payload = {
                    "canonical_key": list(key),
                    "left": _fact_evidence(first),
                    "right": _fact_evidence(second),
                    "context": (
                        "accepted-cross-source-relation"
                        if pair in related_pairs
                        else "exact-qualified-specific-key"
                    ),
                }
                candidate = _candidate(
                    CONTRADICTION_SIGNAL,
                    "contradicts",
                    src,
                    dst,
                    1.0,
                    payload,
                )
                accepted.append(candidate)
                traces.append(_accepted_trace(candidate, "explicit-scalar-conflict"))

    return accepted, traces


def _containers(extractions: Sequence[Extraction]) -> list[_Container]:
    containers: list[_Container] = []
    for extraction in extractions:
        units = sorted(extraction.units, key=lambda item: (item.origin.ref, item.id))
        for unit in units:
            if unit.meta.get("table_summary") is True:
                children = [
                    child
                    for child in units
                    if isinstance(child.meta.get("name"), str)
                    and isinstance(child.meta.get("column"), int)
                ]
                container = _make_container(
                    extraction.source, unit, "table", "table:", children
                )
                if container:
                    containers.append(container)
                continue

            types = _string_tuple(unit.meta.get("types"))
            if not ({"object", "element"} & set(types)):
                continue
            path = str(unit.meta.get("schema_path") or unit.origin.ref)
            depth = len(unit.structure)
            children = [
                child
                for child in units
                if child.modality is Modality.SCHEMA
                and len(child.structure) == depth + 1
                and (
                    depth == 0
                    or child.structure[:depth] == unit.structure
                )
            ]
            # A JSONL root object is a profile over a stream of records, not a
            # singleton configuration object.  Treating it as a record table
            # lets it correspond to CSV or array-item schemas without opening
            # the unsafe generic object/table coercion rejected below.
            family = (
                "table"
                if "*" in path or (extraction.kind == "jsonl" and path == "$")
                else "object"
            )
            container = _make_container(
                extraction.source, unit, family, path, children
            )
            if container:
                containers.append(container)
    return sorted(
        containers,
        key=lambda item: (item.source, item.path, item.unit.origin.ref, item.unit.id),
    )


def _make_container(
    source: str,
    unit: Unit,
    family: str,
    path: str,
    children: Sequence[Unit],
) -> _Container | None:
    fields: dict[str, tuple[str, ...]] = {}
    types: dict[str, tuple[str, ...]] = {}
    for child in children[:_MAX_CONTAINER_FIELDS]:
        raw = _field_name(child)
        if not raw or _GENERATED_FIELD.fullmatch(raw.strip()):
            continue
        tokens, _operations = _normalize_identifier(raw, conceptual=False)
        if not tokens:
            continue
        canonical = ".".join(tokens)
        if canonical in fields:
            # A duplicate normalized field makes this container ambiguous.
            fields.pop(canonical, None)
            types.pop(canonical, None)
            continue
        fields[canonical] = tokens
        types[canonical] = _string_tuple(child.meta.get("types"))
    if len(fields) < 3:
        return None
    return _Container(
        source=source,
        unit=unit,
        family=family,
        path=path,
        fields=tuple(sorted(fields.items())),
        types=tuple(sorted(types.items())),
    )


def _unit_facts(unit: Unit) -> list[_Fact]:
    facts: list[_Fact] = []
    if unit.modality is Modality.PROSE and not unit.meta.get("example_cue"):
        for line in unit.content.splitlines():
            match = _PROSE_FACT.fullmatch(line)
            if not match:
                continue
            raw_key = match.group("backtick") or match.group("plain") or ""
            key = _fact_key(raw_key)
            parsed = _parse_raw_scalar(match.group("value"))
            if not key or parsed is None:
                continue
            value_type, canonical = parsed
            unit_name = match.group("unit")
            facts.append(
                _Fact(
                    source=unit.source,
                    unit=unit,
                    raw_key=raw_key,
                    key=key,
                    raw_value=match.group("value"),
                    value_type=value_type,
                    canonical_value=canonical,
                    unit_name=unit_name.casefold() if unit_name else None,
                    qualified=(len(key) >= 2 or "." in raw_key or "_" in raw_key or "-" in raw_key),
                    basis="strict-prose-assignment",
                )
            )

    structured = _structured_fact(unit)
    if structured is not None:
        facts.append(structured)
    return facts


def _structured_fact(unit: Unit) -> _Fact | None:
    if unit.modality is not Modality.SCHEMA:
        return None
    raw_key = unit.meta.get("schema_path") or unit.meta.get("name")
    if not isinstance(raw_key, str) or not raw_key:
        return None
    key = _fact_key(raw_key)
    if not key:
        return None
    types = set(_string_tuple(unit.meta.get("types"))) - {"null"}
    values = unit.meta.get("values")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return None
    raw_value = values[0]

    if types and types <= {"integer", "number"}:
        numeric = unit.meta.get("numeric")
        if not isinstance(numeric, dict) or numeric.get("min") != numeric.get("max"):
            return None
        parsed = _parse_number(numeric.get("min"))
        if parsed is None:
            return None
        value_type, canonical = "number", parsed
    elif types == {"boolean"}:
        lowered = raw_value.casefold()
        if lowered not in {"true", "false"}:
            return None
        value_type, canonical = "boolean", lowered
    elif types == {"string"}:
        try:
            decoded = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(decoded, str) or decoded.endswith("…"):
            return None
        value_type = "string"
        canonical = unicodedata.normalize("NFC", decoded)
    else:
        return None

    return _Fact(
        source=unit.source,
        unit=unit,
        raw_key=raw_key,
        key=key,
        raw_value=raw_value,
        value_type=value_type,
        canonical_value=canonical,
        unit_name=None,
        qualified=(len(key) >= 2 or raw_key.startswith(("$", "/"))),
        basis="native-structured-constant",
    )


def _native_identifiers(unit: Unit) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("symbol", "name"):
        value = unit.meta.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    path = unit.meta.get("schema_path")
    if isinstance(path, str) and path:
        if field := _last_path_segment(path):
            values.append(field)
    if unit.meta.get("documented") is True:
        label = unit.meta.get("label")
        if isinstance(label, str) and label:
            values.append(label)
    return tuple(dict.fromkeys(values))


def _identifier_namespace(unit: Unit) -> tuple[str, ...] | None:
    """Return an explicit native domain qualifier, never a guessed topic.

    Structured paths may carry a real namespace before their leaf field.  A
    leaf-only match must not connect two explicitly different domains, while
    format wrappers such as ``records`` or ``settings`` are not domains.  The
    result is intentionally conservative: unqualified code, tables, and prose
    remain eligible, but they cannot bridge two conflicting qualified paths.
    """

    path = unit.meta.get("schema_path")
    if not isinstance(path, str) or not path:
        return None
    raw_parts = [
        part.strip("$@'\"")
        for part in re.split(r"[/.[\]]+", path.rstrip("/."))
    ]
    parts = [
        part
        for part in raw_parts
        if part and part != "*" and not part.isdigit()
    ]
    if len(parts) < 2:
        return None
    for part in parts[:-1]:
        tokens, _operations = _normalize_identifier(part, conceptual=True)
        if tokens and not all(
            token in _IDENTIFIER_NAMESPACE_WRAPPERS for token in tokens
        ):
            return tokens
    return None


def _field_name(unit: Unit) -> str | None:
    name = unit.meta.get("name")
    if isinstance(name, str):
        return name
    path = unit.meta.get("schema_path")
    if isinstance(path, str):
        return _last_path_segment(path)
    return None


def _last_path_segment(path: str) -> str | None:
    stripped = path.rstrip("/.")
    if not stripped:
        return None
    parts = re.split(r"[/.[\]]+", stripped)
    meaningful = [part.strip("$@'") for part in parts if part.strip("$@'") and part != "*"]
    return meaningful[-1] if meaningful else None


def _normalize_identifier(
    value: str, *, conceptual: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if len(value) > _MAX_IDENTIFIER_CHARS:
        return (), ("over-length",)
    normalized = unicodedata.normalize("NFC", value)
    operations: list[str] = []
    if normalized.isascii():
        split = _CAMEL_ACRONYM.sub(r"\1 \2", normalized)
        split = _CAMEL_LOWER.sub(r"\1 \2", split)
        split = _ALPHA_DIGIT.sub(
            lambda match: " ".join(part for part in match.groups() if part), split
        )
        tokens = [token.casefold() for token in _TOKEN.findall(split)]
        operations.append("ascii-casefold")
    else:
        tokens = [token for token in re.findall(r"[^\W_]+", normalized, re.UNICODE)]
        operations.append("unicode-nfc-case-sensitive")

    if conceptual:
        aliased = [_IDENTIFIER_ALIASES.get(token, token) for token in tokens]
        if aliased != tokens:
            operations.append("frozen-alias-v1")
        tokens = aliased
        if len(tokens) > 1 and tokens[0] in _ACTION_PREFIXES:
            tokens = tokens[1:]
            operations.append("drop-action-prefix-v1")
        if len(tokens) > 1 and tokens[-1] in _MEASUREMENT_SUFFIXES:
            tokens = tokens[:-1]
            operations.append("drop-measurement-suffix-v1")
    return tuple(tokens), tuple(operations)


def _text_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", value)
    if normalized.isascii():
        split = _CAMEL_ACRONYM.sub(r"\1 \2", normalized)
        split = _CAMEL_LOWER.sub(r"\1 \2", split)
        return tuple(token.casefold() for token in _TOKEN.findall(split))
    return tuple(re.findall(r"[^\W_]+", normalized, re.UNICODE))


def _eligible_identifier(tokens: tuple[str, ...]) -> bool:
    meaningful = [token for token in tokens if token not in _COMMON_IDENTIFIER_TOKENS]
    if not meaningful:
        # A compound entity key such as ``run_id`` remains materially more
        # specific than either common token by itself.  Requiring an ``id``
        # suffix and a non-trivial qualifier preserves the single-token hard
        # negatives while recovering exact native/prose entity keys.
        return (
            len(tokens) >= 2
            and tokens[-1] == "id"
            and any(len(token) >= 3 for token in tokens[:-1])
        )
    if any(not token.isascii() for token in meaningful):
        return any(len(token) >= 4 for token in meaningful)
    if len(meaningful) == 1:
        return len(meaningful[0]) >= 6 and not meaningful[0].isdigit()
    return sum(len(token) >= 3 and not token.isdigit() for token in meaningful) >= 2


def _contains_sequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    size = len(needle)
    return any(haystack[index : index + size] == needle for index in range(len(haystack) - size + 1))


def _occurrence_key(item: _IdentifierOccurrence) -> tuple[Any, ...]:
    return (
        0 if item.native else 1,
        0 if item.unit.meta.get("heading") or item.unit.meta.get("caption") else 1,
        -item.unit.salience,
        item.source,
        item.unit.origin.ref,
        item.unit.id,
    )


def _generic_field(tokens: tuple[str, ...]) -> bool:
    meaningful = [token for token in tokens if token not in _STRUCTURAL_GENERIC_TOKENS]
    return not meaningful or all(len(token) < 3 or token.isdigit() for token in meaningful)


def _families_compatible(left: _Container, right: _Container) -> bool:
    if left.family == right.family:
        return True
    # A table may correspond to an array-item object, which is classified as a
    # table by _containers. No generic config-object/table coercion is allowed.
    return False


def _types_compatible(left: set[str], right: set[str]) -> bool:
    numeric = {"integer", "number"}
    if left <= numeric and right <= numeric:
        return True
    return bool(left & right)


def _fact_key(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = re.sub(r"^\$[./]?", "", normalized)
    normalized = normalized.replace("[*]", ".").replace("*", ".")
    tokens, _operations = _normalize_identifier(normalized, conceptual=False)
    return tuple(token for token in tokens if token not in {"pointer", "key", "document"})


def _parse_raw_scalar(value: str) -> tuple[str, str] | None:
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return "boolean", lowered
    if value.startswith(('"', "'")):
        try:
            if value.startswith('"'):
                decoded = json.loads(value)
            else:
                decoded = bytes(value[1:-1], "utf-8").decode("unicode_escape")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return "string", unicodedata.normalize("NFC", decoded)
    number = _parse_number(value)
    return ("number", number) if number is not None else None


def _parse_number(value: Any) -> str | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite():
        return None
    if number == 0:
        number = Decimal(0)
    normalized = number.normalize()
    return format(normalized, "f")


def _comparable_facts(left: _Fact, right: _Fact) -> bool:
    return (
        left.value_type == right.value_type
        and left.unit_name == right.unit_name
        and left.source != right.source
    )


def _fact_evidence(fact: _Fact) -> dict[str, Any]:
    return {
        "source": fact.source,
        "unit_id": fact.unit.id,
        "origin": str(fact.unit.origin),
        "raw_key": fact.raw_key,
        "raw_value": fact.raw_value,
        "value_type": fact.value_type,
        "canonical_value": fact.canonical_value,
        "unit": fact.unit_name,
        "basis": fact.basis,
    }


def _canonical_url(value: str) -> str | None:
    try:
        split = urlsplit(value)
    except ValueError:
        return None
    scheme = split.scheme.casefold()
    if scheme not in _URL_SCHEMES or not split.hostname:
        return None
    host = split.hostname.casefold()
    try:
        port = split.port
    except ValueError:
        return None
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    if split.username is not None or split.password is not None:
        return None
    path = _normalize_url_path(split.path or "/")
    return urlunsplit((scheme, host, path, split.query, ""))


def _normalize_url_path(path: str) -> str:
    # Decode only RFC 3986 unreserved characters, then normalize dot segments.
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    def normalize_escape(match: re.Match[str]) -> str:
        octet = int(match.group(1), 16)
        char = chr(octet)
        return char if char in unreserved else f"%{octet:02X}"

    decoded = _PERCENT_ESCAPE.sub(normalize_escape, path)
    safe = "/:@!$&'()*+,;=-._~%"
    recoded = quote(decoded, safe=safe)
    normalized = posixpath.normpath(recoded)
    if path.startswith("/") and not normalized.startswith("/"):
        normalized = "/" + normalized
    if path.endswith("/") and normalized != "/" and not normalized.endswith("/"):
        normalized += "/"
    return normalized or "/"


def _local_source(value: str) -> str | None:
    if not value or value.startswith("<") or "://" in value:
        return None
    return os.path.abspath(os.path.normpath(value))


def _path_targets(source: str, target: str) -> tuple[str, tuple[str, ...], str | None]:
    target_path, _fragment = urldefrag(target)
    source_path = _local_source(source)
    if source_path is None or not target_path or "://" in target_path:
        return target_path, (), None
    if os.path.isabs(target_path):
        normalized = os.path.abspath(os.path.normpath(target_path))
    else:
        normalized = os.path.abspath(
            os.path.normpath(os.path.join(os.path.dirname(source_path), target_path))
        )
    return normalized, ("path:" + normalized,), os.path.basename(target_path)


def _relative_import_targets(source: str, target: str) -> tuple[str, tuple[str, ...]]:
    if not target.startswith("."):
        return target, ()
    source_path = _local_source(source)
    if source_path is None:
        return target, ()
    level = len(target) - len(target.lstrip("."))
    module = target[level:].replace(".", os.sep)
    base = os.path.dirname(source_path)
    for _ in range(max(0, level - 1)):
        base = os.path.dirname(base)
    candidates = [os.path.join(base, module + ".py")]
    candidates.append(os.path.join(base, module, "__init__.py"))
    identities = tuple(
        "path:" + os.path.abspath(os.path.normpath(candidate))
        for candidate in candidates
    )
    return target, identities


def _looks_like_local_path(value: str) -> bool:
    if _canonical_url(value) is not None:
        return False
    head, _fragment = urldefrag(value)
    return bool(
        head.startswith(("./", "../", "/"))
        or re.search(r"(?:^|/)[^/]+\.[A-Za-z0-9]{1,8}$", head)
    )


def _canonical_doi(value: str) -> str | None:
    normalized = value.strip()
    normalized = re.sub(r"^doi:\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"^https?://(?:dx\.)?doi\.org/", "", normalized, flags=re.IGNORECASE
    )
    normalized = normalized.rstrip(".,;)")
    return normalized.casefold() if _DOI.fullmatch(normalized) else None


def _looks_like_doi(value: str) -> bool:
    return _canonical_doi(value) is not None


def _candidate(
    signal: str,
    relation_kind: str,
    src: Unit,
    dst: Unit,
    confidence: float,
    payload: dict[str, Any],
) -> MatchCandidate:
    return MatchCandidate(
        signal=signal,
        relation_kind=relation_kind,
        src=src.id,
        dst=dst.id,
        src_source=src.source,
        dst_source=dst.source,
        src_origin=str(src.origin),
        dst_origin=str(dst.origin),
        confidence=round(confidence, 6),
        evidence=_evidence(signal, payload),
        details=payload,
    )


def _evidence(signal: str, payload: dict[str, Any]) -> str:
    return f"fusion.{signal} " + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _unresolved(
    reference: Unit,
    ref_kind: str,
    raw_target: str,
    normalized_target: str,
    reason: str,
    candidates: tuple[str, ...],
) -> UnresolvedReference:
    payload = {
        "raw_target": raw_target,
        "normalized_target": normalized_target,
        "ref_kind": ref_kind,
        "reason": reason,
        "candidates": list(candidates),
    }
    return UnresolvedReference(
        reference_id=reference.id,
        source=reference.source,
        origin=reference.origin,
        ref_kind=ref_kind,
        raw_target=raw_target,
        normalized_target=normalized_target,
        reason=reason,
        candidates=candidates,
        evidence=_evidence(LITERAL_SIGNAL, payload),
    )


def _reference_trace(
    reference: Unit,
    status: str,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> MatchTrace:
    return MatchTrace(
        signal=LITERAL_SIGNAL,
        status=status,
        reason=reason,
        unit_ids=(reference.id,),
        sources=(reference.source,),
        detail=_evidence(LITERAL_SIGNAL, payload or {}),
    )


def _unresolved_trace(item: UnresolvedReference) -> MatchTrace:
    return MatchTrace(
        signal=LITERAL_SIGNAL,
        status="unresolved",
        reason=item.reason,
        unit_ids=(item.reference_id,),
        sources=(item.source, *item.candidates),
        detail=item.evidence,
    )


def _accepted_trace(candidate: MatchCandidate, reason: str) -> MatchTrace:
    return MatchTrace(
        signal=candidate.signal,
        status="accepted",
        reason=reason,
        unit_ids=(candidate.src, candidate.dst),
        sources=(candidate.src_source, candidate.dst_source),
        detail=candidate.evidence,
    )


def _ordered_units(left: Unit, right: Unit) -> tuple[Unit, Unit]:
    key = lambda unit: (unit.source, unit.origin.ref, unit.id)
    return (left, right) if key(left) <= key(right) else (right, left)


def _source_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted(str(item) for item in value if isinstance(item, str)))


def _candidate_key(item: MatchCandidate) -> tuple[Any, ...]:
    return (
        item.signal,
        item.src_source,
        item.dst_source,
        item.src,
        item.dst,
        item.relation_kind,
        item.evidence,
    )


def _trace_key(item: MatchTrace) -> tuple[Any, ...]:
    return (
        item.signal,
        item.status,
        item.reason,
        item.sources,
        item.unit_ids,
        item.detail,
    )


def _unresolved_key(item: UnresolvedReference) -> tuple[Any, ...]:
    return (
        item.source,
        item.origin.ref,
        item.reference_id,
        item.ref_kind,
        item.normalized_target,
        item.reason,
    )


__all__ = [
    "CONTRADICTION_SIGNAL",
    "FusionSignals",
    "IDENTIFIER_SIGNAL",
    "LITERAL_SIGNAL",
    "MatchCandidate",
    "MatchTrace",
    "STRUCTURAL_SIGNAL",
    "STAGE4_EVALUATED_IMPLEMENTATION_SHA256",
    "STAGE4_SCORED_PREDICTIONS_SHA256",
    "STAGE4_SHIPPING_DISPOSITIONS",
    "UnresolvedReference",
    "analyze",
    "fuse",
    "run_signals",
]
