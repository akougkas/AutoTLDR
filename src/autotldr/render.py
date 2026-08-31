"""Budgeted text renderers for the invoke and shareable-output surfaces.

Every core text shape consumes the same :class:`~autotldr.bundle.Bundle`.
Budget compliance happens here, at the serialization boundary: citations,
metadata, escaping, the drop report, ANSI controls, and the final newline all
cost space and therefore all count.

There is no universal model token count without naming a model tokenizer.  v1's
portable, dependency-free counter is ``utf8-byte-v1``: one portable token is one
byte of the canonical UTF-8 payload.  It is conservative for ordinary byte-BPE
tokenizers and, unlike the old character heuristic, exact and independently
verifiable.  ``Unit.tokens`` remains a cheap diagnostic estimate only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from .bundle import Bundle, Finding, project
from .unit import Extraction, GapKind, GroundedStatement, Origin, Relation, Unit

SCHEMA_VERSION = 2
COUNTER_NAME = "utf8-byte-v1"
COUNTER_SCOPE = "complete-output"
EXTENSION_RENDER_ENVELOPE_SCHEMA = "autotldr-extension-render-envelope-v1"
HUMAN_DROP_RECORD_WIRE = "drop-v1"

_Rendered = str | bytes
_BudgetBuilder = Callable[[Bundle, "RenderOptions"], _Rendered]


class BudgetTooSmall(ValueError):
    """The requested ceiling cannot hold a valid addressable envelope."""

    def __init__(
        self,
        limit: int,
        required: int,
        output: str,
        counter: str = COUNTER_NAME,
    ) -> None:
        self.limit = limit
        self.required = required
        self.output = output
        self.counter = counter
        unit = "portable tokens" if counter == COUNTER_NAME else "bytes"
        super().__init__(
            f"budget {limit} is too small for {output}; the minimum valid "
            f"addressable output needs {required} {unit} ({counter})"
        )


def count_portable_tokens(text: str) -> int:
    """Count the canonical UTF-8 payload exactly."""

    return len(text.encode("utf-8"))


def _payload_size(payload: _Rendered) -> int:
    """Count one complete text or binary serialization exactly."""

    if isinstance(payload, str):
        return count_portable_tokens(payload)
    if isinstance(payload, bytes):
        return len(payload)
    raise TypeError("renderer builder must return str or bytes")


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Stable builder options shared by core and explicit render extensions."""

    output: str
    cite: bool
    color: bool
    indent: int
    extension_capabilities: dict[str, object] | None = None
    extension_renderer: dict[str, object] | None = None


# Kept for source compatibility with existing internal tests and adapters that
# adopted the Stage 3 name before the v1 extension contract was published.
_RenderOptions = RenderOptions


def validate_extension_registry(registry: object) -> None:
    """Reject renderer claims that overlap an implemented core shape."""

    from .extensions import ExtensionCollisionError, ExtensionRegistry

    if not isinstance(registry, ExtensionRegistry):
        raise TypeError("registry must be an ExtensionRegistry")
    core_names = {"ansi", "html", "json", "jsonl", "markdown", "md", "pdf"}
    core_suffixes = {
        ".ansi",
        ".html",
        ".htm",
        ".json",
        ".jsonl",
        ".markdown",
        ".md",
        ".ndjson",
        ".pdf",
        ".txt",
    }
    core_media = {
        "application/json",
        "application/jsonl",
        "application/x-jsonlines",
        "application/x-ndjson",
        "application/pdf",
        "text/html",
        "text/markdown",
        "text/plain",
        "text/x-jsonl",
    }
    for spec in registry.renderers:
        if overlap := sorted({spec.name, *spec.aliases} & core_names):
            raise ExtensionCollisionError(
                f"renderer {spec.name!r} collides with implemented core "
                f"name {overlap[0]!r}"
            )
        if overlap := sorted(set(spec.suffixes) & core_suffixes):
            raise ExtensionCollisionError(
                f"renderer {spec.name!r} collides with implemented core "
                f"suffix {overlap[0]!r}"
            )
        if overlap := sorted(set(spec.media_types) & core_media):
            raise ExtensionCollisionError(
                f"renderer {spec.name!r} collides with implemented core "
                f"media type {overlap[0]!r}"
            )


def render(
    result: Extraction,
    *,
    output: str = "ansi",
    budget: int | None = None,
    cite: bool = True,
    color: bool = False,
    indent: int = 2,
    registry: object | None = None,
) -> str:
    """Render ``result`` with a complete-output portable-token ceiling.

    Units are selected by a salience-ranked prefix, but emitted in source order.
    A unit is atomic; it is never sliced merely to make a limit.  The induced
    relation graph contains no dangling endpoints.  Every omitted unit and
    relation is concretely identified inside the ceiling; if that mandatory
    envelope cannot fit, :class:`BudgetTooSmall` is raised.
    """

    builder: _BudgetBuilder | None = _BUILDERS.get(output)
    canonical_output = output
    renderer_manifest: dict[str, object] | None = None
    capabilities: dict[str, object] | None = None
    if registry is not None:
        validate_extension_registry(registry)
        capabilities = registry.capability_manifest()  # type: ignore[attr-defined]
    if builder is None:
        if registry is None:
            raise ValueError(f"unknown output format: {output}")
        try:
            spec = registry.get_renderer(output)  # type: ignore[attr-defined]
        except LookupError:
            raise ValueError(f"unknown output format: {output}") from None
        if cite and not spec.supports_citations:
            raise ValueError(
                f"extension renderer {spec.name!r} does not support citations; "
                "use --no-cite"
            )
        if color and not spec.supports_color:
            raise ValueError(
                f"extension renderer {spec.name!r} does not support color"
            )
        resolved = registry.resolve_renderer(spec)  # type: ignore[attr-defined]
        builder = _extension_builder(spec, resolved)
        canonical_output = spec.name
        renderer_manifest = spec.as_manifest()
    options = RenderOptions(
        canonical_output,
        cite,
        color,
        indent,
        extension_capabilities=capabilities,
        extension_renderer=renderer_manifest,
    )
    rendered = _render_budgeted(
        result,
        output=canonical_output,
        budget=budget,
        options=options,
        builder=builder,
        exhaustive_prefixes=canonical_output in {"ansi", "md", "html"},
    )
    if not isinstance(rendered, str):  # pragma: no cover - core text boundary
        raise TypeError(f"text renderer {canonical_output!r} returned bytes")
    return rendered


def _render_budgeted(
    result: Extraction,
    *,
    output: str,
    budget: int | None,
    options: RenderOptions,
    builder: _BudgetBuilder,
    exhaustive_prefixes: bool = False,
) -> _Rendered:
    """Apply the one canonical selection policy to text or binary builders."""

    if budget is not None and budget <= 0:
        raise ValueError("budget must be a positive integer")
    _validate_result(result)

    all_indexes = set(range(len(result.units)))
    available, unlimited_text = _settle_available(
        result,
        all_indexes,
        options,
        builder,
    )
    if budget is None:
        return unlimited_text

    # A complete projection has no omission inventory and can therefore be
    # smaller than an empty projection that must name every omitted item.
    complete_text = _settle_used(
        result,
        all_indexes,
        options,
        builder,
        requested=budget,
        available=available,
    )
    if _payload_size(complete_text) <= budget:
        return complete_text

    ranked_indexes = _ranked_unit_indexes(result)
    if exhaustive_prefixes:
        best: tuple[int, _Rendered] | None = None
        prefix_sets: list[set[int]] = []
        for prefix_size in range(len(ranked_indexes)):
            selected = set(ranked_indexes[:prefix_size])
            prefix_sets.append(selected)
            candidate = _settle_used(
                result,
                selected,
                options,
                builder,
                requested=budget,
                available=available,
            )
            if _payload_size(candidate) <= budget:
                best = (prefix_size, candidate)
        if best is not None:
            return best[1]
        required = min(
            _retryable_required(result, selected, options, builder, available)
            for selected in [*prefix_sets, all_indexes]
        )
        counter = "binary-byte-v1" if output == "pdf" else COUNTER_NAME
        raise BudgetTooSmall(budget, required, output, counter)

    empty_text = _settle_used(
        result,
        set(),
        options,
        builder,
        requested=budget,
        available=available,
    )
    one_indexes = set(ranked_indexes[:1])
    one_text = (
        _settle_used(
            result,
            one_indexes,
            options,
            builder,
            requested=budget,
            available=available,
        )
        if one_indexes
        else empty_text
    )

    # Completing a multi-unit claim can remove a comparatively large concrete
    # omission record at the same moment the claim becomes renderable.  That is
    # an intentional non-monotone boundary, so test every claim-completion
    # prefix explicitly rather than assuming the one-unit probe represents it.
    ranked_position = {
        result.units[index].id: position
        for position, index in enumerate(ranked_indexes)
    }
    claim_boundaries = sorted(
        {
            max(ranked_position[unit_id] for unit_id in statement.evidence_unit_ids)
            + 1
            for statement in result.summary_claims
        }
    )
    special_best: tuple[int, str] | None = None
    for boundary in claim_boundaries:
        if boundary in {0, 1, len(ranked_indexes)}:
            continue
        candidate = _settle_used(
            result,
            set(ranked_indexes[:boundary]),
            options,
            builder,
            requested=budget,
            available=available,
        )
        if _payload_size(candidate) <= budget:
            special_best = (boundary, candidate)

    empty_fits = _payload_size(empty_text) <= budget
    one_fits = _payload_size(one_text) <= budget
    if not empty_fits and not one_fits and special_best is None:
        boundary_sets = [set(), all_indexes]
        if one_indexes:
            boundary_sets.append(one_indexes)
        required = min(
            _retryable_required(result, selected, options, builder, available)
            for selected in boundary_sets
        )
        counter = "binary-byte-v1" if output == "pdf" else COUNTER_NAME
        raise BudgetTooSmall(budget, required, output, counter)

    if not one_fits:
        # The empty projection is the only fitting structural boundary.  Every
        # ordinary non-empty prefix is at least as rich as the tested one-unit
        # prefix; an explicitly tested claim-completion boundary may still fit.
        return special_best[1] if special_best is not None else empty_text

    # Concrete drop records make a selected unit richer than its omitted
    # record.  Prefix size is monotone after the two explicitly tested human
    # discontinuities: zero units removes the empty-state sentence, while all
    # units removes the drop section.  Binary search caps complete
    # serializations at O(log n), rather than rebuilding once per unit.
    low = 1
    high = max(0, len(ranked_indexes) - 1)
    best = one_text
    best_size = 1
    if special_best is not None and special_best[0] > best_size:
        best_size, best = special_best
    while low < high:
        middle = (low + high + 1) // 2
        selected = set(ranked_indexes[:middle])
        candidate = _settle_used(
            result,
            selected,
            options,
            builder,
            requested=budget,
            available=available,
        )
        if _payload_size(candidate) <= budget:
            low = middle
            if middle > best_size:
                best_size = middle
                best = candidate
        else:
            high = middle - 1

    used = _payload_size(best)
    if used > budget:  # pragma: no cover - the assertion is the design boundary
        raise AssertionError(f"renderer emitted {used} tokens against {budget}")
    return best


def _ranked_unit_indexes(result: Extraction) -> list[int]:
    """Return the shared claim-first, salience-ranked unit prefix order."""

    claim_evidence_priority: dict[str, tuple[int, int]] = {}
    for statement_index, statement in enumerate(result.summary_claims):
        for evidence_index, unit_id in enumerate(statement.evidence_unit_ids):
            claim_evidence_priority.setdefault(
                unit_id,
                (statement_index, evidence_index),
            )
    ranked = sorted(
        enumerate(result.units),
        key=lambda item: (
            0 if item[1].id in claim_evidence_priority else 1,
            *claim_evidence_priority.get(item[1].id, (len(result.summary_claims), 0)),
            -item[1].salience,
            -item[1].confidence,
            item[0],
        ),
    )
    return [index for index, _unit in ranked]


def _extension_builder(spec: object, resolved: Callable[..., object]):
    """Wrap one cosmetic extension payload in the core-owned wire envelope.

    Extension code never serializes the authoritative result, manifest, or
    selection report.  It receives a defensive copy of the selected semantic
    projection and may return only a UTF-8 text payload.  The core then places
    that payload beside its canonical machine projection so provenance,
    evidence closure, capabilities, and concrete omission records cannot be
    suppressed by a renderer.

    The payload view deliberately has an empty ``selection`` mapping.  Exact
    ``used``/``available`` values are fixed-point outputs, not renderer inputs;
    allowing payload bytes to depend on them would make the byte equation
    renderer-controlled.  Calling twice and retaining one result per selected
    projection makes nondeterminism a named conformance failure rather than an
    unstable budget result.
    """

    payloads: dict[tuple[object, ...], str] = {}

    def invoke_payload(bundle: Bundle, options: RenderOptions) -> str:
        import copy

        from .extensions import (
            ExtensionConformanceError,
            validate_renderer_output,
        )

        def invoke_once() -> str:
            # Build the renderer view field-by-field so the mandatory, potentially
            # large drop inventory is never copied across the extension boundary
            # even transiently.
            payload_bundle = Bundle(
                subject=bundle.subject,
                kind=bundle.kind,
                summary=bundle.summary,
                summary_claims=copy.deepcopy(bundle.summary_claims),
                units=copy.deepcopy(bundle.units),
                relations=copy.deepcopy(bundle.relations),
                gaps=copy.deepcopy(bundle.gaps),
                manifest=copy.deepcopy(bundle.manifest),
                selection={},
            )
            payload_options = copy.deepcopy(options)
            try:
                value = resolved(payload_bundle, payload_options)
            except Exception:
                raise ExtensionConformanceError(
                    f"extension renderer {spec.name!r} failed"
                ) from None
            return validate_renderer_output(value)

        first = invoke_once()
        second = invoke_once()
        if first != second:
            raise ExtensionConformanceError(
                f"extension renderer {spec.name!r} is nondeterministic for one "
                "selected projection"
            )

        key = (
            bundle.subject,
            bundle.kind,
            tuple(unit.id for unit in bundle.units),
            tuple(
                (relation.src, relation.dst, str(relation.kind), relation.evidence)
                for relation in bundle.relations
            ),
            tuple(statement.id for statement in bundle.summary_claims),
            tuple(finding.id for finding in bundle.gaps),
            options.output,
            options.cite,
            options.color,
            options.indent,
        )
        previous = payloads.setdefault(key, first)
        if previous != first:
            raise ExtensionConformanceError(
                f"extension renderer {spec.name!r} changed payload for one "
                "selected projection"
            )
        return first

    def build(bundle: Bundle, options: RenderOptions) -> str:
        import json

        payload = invoke_payload(bundle, options)
        envelope = {
            "schema": EXTENSION_RENDER_ENVELOPE_SCHEMA,
            "format": {
                "name": options.output,
                "renderer": options.extension_renderer,
            },
            "payload": payload,
            "core": _json_payload(bundle),
        }
        return (
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        )

    return build


def _validate_result(result: Extraction) -> None:
    """Fail closed when IDs cannot define one unambiguous induced graph."""

    seen: dict[str, int] = {}
    for index, unit in enumerate(result.units):
        unit_id = unit.id
        if unit_id in seen:
            raise ValueError(
                f"duplicate unit id {unit_id} at indexes {seen[unit_id]} and "
                f"{index}; addressable rendering requires unique full IDs"
            )
        seen[unit_id] = index

    for relation in result.relations:
        missing = [
            endpoint
            for endpoint in (relation.src, relation.dst)
            if endpoint not in seen
        ]
        if missing:
            raise ValueError(
                f"relation {relation.src}->{relation.dst} has unresolved "
                f"endpoint(s): {', '.join(missing)}"
            )

    statement_ids: dict[str, int] = {}
    units_by_id = {unit.id: unit for unit in result.units}
    for index, statement in enumerate(result.summary_claims):
        if not isinstance(statement, GroundedStatement):
            raise ValueError(
                f"summary claim at index {index} is not a GroundedStatement"
            )
        if statement.id in statement_ids:
            raise ValueError(
                f"duplicate grounded statement id {statement.id} at indexes "
                f"{statement_ids[statement.id]} and {index}"
            )
        statement_ids[statement.id] = index
        missing = [
            unit_id
            for unit_id in statement.evidence_unit_ids
            if unit_id not in units_by_id
        ]
        if missing:
            raise ValueError(
                f"grounded statement {statement.id} has unresolved evidence "
                f"unit id(s): {', '.join(missing)}"
            )

        cited_origins = set(statement.origins)
        evidence_origins = {
            units_by_id[unit_id].origin
            for unit_id in statement.evidence_unit_ids
        }
        if cited_origins != evidence_origins:
            raise ValueError(
                f"grounded statement {statement.id} origins must exactly match "
                "its evidence unit origins"
            )


def _retryable_required(
    result: Extraction,
    selected: set[int],
    options: RenderOptions,
    builder: _BudgetBuilder,
    available: int,
) -> int:
    """Return a ceiling guaranteed to fit this exact projection on retry.

    The decimal spelling of ``requested`` is part of the payload.  Merely
    reporting the byte count produced with ``requested=1`` can therefore be a
    few bytes short on retry.  Iterate that dependency to a fixed point.
    """

    required = 1
    for _ in range(20):
        payload = _settle_used(
            result,
            selected,
            options,
            builder,
            requested=required,
            available=available,
        )
        measured = _payload_size(payload)
        if measured <= required:
            return required
        required = measured
    raise AssertionError("minimum portable-token limit did not converge")


def to_ansi(
    result: Extraction,
    *,
    budget: int | None = None,
    cite: bool = True,
    color: bool = False,
) -> str:
    return render(result, output="ansi", budget=budget, cite=cite, color=color)


def to_markdown(
    result: Extraction,
    *,
    budget: int | None = None,
    cite: bool = True,
) -> str:
    return render(result, output="md", budget=budget, cite=cite)


def to_json(
    result: Extraction,
    *,
    indent: int = 2,
    budget: int | None = None,
) -> str:
    # Origins are structural data, not presentation chrome, so machine shapes
    # intentionally have no cite=False path.
    return render(result, output="json", budget=budget, indent=indent)


def to_jsonl(result: Extraction, *, budget: int | None = None) -> str:
    return render(result, output="jsonl", budget=budget)


def to_html(
    result: Extraction,
    *,
    budget: int | None = None,
    cite: bool = True,
) -> str:
    return render(result, output="html", budget=budget, cite=cite)


# ---------------------------------------------------------------------------
# Selection accounting
# ---------------------------------------------------------------------------


def _settle_available(
    result: Extraction,
    selected: set[int],
    options: RenderOptions,
    builder: _BudgetBuilder | None = None,
) -> tuple[int, _Rendered]:
    """Find the fixed point where unlimited ``used == available == bytes``."""

    available = 0
    for _ in range(20):
        payload = _settle_used(
            result,
            selected,
            options,
            builder,
            requested=None,
            available=available,
        )
        measured = _payload_size(payload)
        if measured == available:
            return measured, payload
        available = measured
    raise AssertionError("portable-token available count did not converge")


def _settle_used(
    result: Extraction,
    selected: set[int],
    options: RenderOptions,
    builder: _BudgetBuilder | None = None,
    *,
    requested: int | None,
    available: int,
) -> _Rendered:
    """Render until the serialized ``used`` field equals the real byte count."""

    used = 0
    for _ in range(20):
        bundle = _prepare_bundle(
            result,
            selected,
            requested=requested,
            used=used,
            available=available,
        )
        if options.extension_capabilities is not None:
            bundle.manifest.setdefault(
                "extensions",
                {
                    "schema": "autotldr-explicit-extension-run-v1",
                    "requested": None,
                    "capabilities": options.extension_capabilities,
                },
            )
        if options.extension_renderer is not None:
            bundle.manifest["extension_renderer"] = options.extension_renderer
        selected_builder = builder or _BUILDERS[options.output]
        payload = selected_builder(bundle, options)
        measured = _payload_size(payload)
        if measured == used:
            return payload
        used = measured
    raise AssertionError("portable-token used count did not converge")


def _prepare_bundle(
    result: Extraction,
    selected: set[int],
    *,
    requested: int | None,
    used: int,
    available: int,
) -> Bundle:
    bundle = project(result, selected)
    selected_ids = {unit.id for unit in bundle.units}
    dropped_units = [
        unit for index, unit in enumerate(result.units) if index not in selected
    ]
    records = [_drop_unit_record(unit) for unit in dropped_units]
    relation_records = [
        {
            # Relation has no first-class ID in the Stage 1 IR.  Its stable
            # source-order ordinal plus full endpoints and kind identifies the
            # exact edge, including duplicate parallel edges, without copying
            # the potentially large evidence payload into the drop inventory.
            "index": index,
            "src": relation.src,
            "dst": relation.dst,
            "kind": str(relation.kind),
            "reason": "budget",
        }
        for index, relation in enumerate(result.relations)
        if relation.src not in selected_ids or relation.dst not in selected_ids
    ]
    statement_records = [
        {
            "id": statement.id,
            "evidence_unit_ids": list(statement.evidence_unit_ids),
            "missing_evidence_unit_ids": [
                unit_id
                for unit_id in statement.evidence_unit_ids
                if unit_id not in selected_ids
            ],
            "origins": [_origin_to_dict(origin) for origin in statement.origins],
            "reason": "budget-evidence-omitted",
        }
        for statement in result.summary_claims
        if any(
            unit_id not in selected_ids
            for unit_id in statement.evidence_unit_ids
        )
    ]
    if records or relation_records or statement_records:
        import json

        canonical = json.dumps(
            {
                "units": records,
                "relations": relation_records,
                "statements": statement_records,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        canonical = b'{"relations":[],"statements":[],"units":[]}'
    bundle.selection = {
        "counter": COUNTER_NAME,
        "scope": COUNTER_SCOPE,
        "requested": requested,
        "used": used,
        "available": available,
        "selected_units": len(bundle.units),
        "selected_statements": len(bundle.summary_claims),
        "dropped": {
            "unit_count": len(records),
            "relation_count": len(relation_records),
            "statement_count": len(statement_records),
            "reason": (
                "budget"
                if records or relation_records or statement_records
                else None
            ),
            "digest": hashlib.sha256(canonical).hexdigest(),
            # Concrete identity records are mandatory output, never optional
            # detail.  A budget that cannot carry every record fails instead
            # of replacing omitted identities with a hash-only commitment.
            "reported": records,
            "unlisted": 0,
            "reported_relations": relation_records,
            "unlisted_relations": 0,
            "reported_statements": statement_records,
            "unlisted_statements": 0,
        },
    }
    return bundle


def _drop_unit_record(unit: Unit) -> dict[str, Any]:
    return {
        "id": unit.id,
        "origin": _origin_to_dict(unit.origin),
        "reason": "budget",
    }


def _human_drop_record(kind: str, record: dict[str, Any]) -> str:
    """Return one safe, deterministic human-renderer omission record.

    ``ensure_ascii`` makes terminal controls, bidirectional controls, and line
    separators inert while retaining exact JSON round-tripping.  Backticks are
    additionally written as a JSON Unicode escape so an adversarial source or
    native reference cannot open a Markdown code span.  Keys and separators
    are canonical, and the fixed ``drop-v1`` prefix versions the framing
    independently of the record objects committed by the drop-set digest.
    """

    import json

    payload = json.dumps(
        record,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).replace("`", r"\u0060").replace("\x7f", r"\u007f")
    return f"{HUMAN_DROP_RECORD_WIRE}/{kind} {payload}"


# ---------------------------------------------------------------------------
# Machine renderers
# ---------------------------------------------------------------------------


def unit_to_dict(unit: Unit) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": unit.id,
        "source": unit.source,
        "modality": str(unit.modality),
        "role": str(unit.role),
        "content": unit.content,
        "origin": _origin_to_dict(unit.origin),
        "tokens": unit.tokens,
        "salience": round(unit.salience, 3),
        "confidence": round(unit.confidence, 3),
    }
    if unit.structure:
        out["structure"] = list(unit.structure)
    if unit.meta:
        out["meta"] = _clean(unit.meta)
    return out


def _origin_to_dict(origin: Origin) -> dict[str, Any]:
    out: dict[str, Any] = {"source": origin.source, "ref": origin.ref}
    if origin.char_span is not None:
        out["char_span"] = list(origin.char_span)
    return out


def _relation_to_dict(relation: Relation) -> dict[str, Any]:
    return {
        "src": relation.src,
        "dst": relation.dst,
        "kind": str(relation.kind),
        "evidence": relation.evidence,
        "confidence": round(relation.confidence, 3),
    }


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "kind": str(finding.kind),
        "content": finding.content,
        "origin": _origin_to_dict(finding.origin),
    }


def _statement_to_dict(statement: GroundedStatement) -> dict[str, Any]:
    return {
        "id": statement.id,
        "content": statement.content,
        "origins": [_origin_to_dict(origin) for origin in statement.origins],
        "evidence_unit_ids": list(statement.evidence_unit_ids),
    }


def _json_payload(bundle: Bundle) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "subject": bundle.subject,
        "kind": bundle.kind,
        "summary": _bundle_summary(bundle),
        "summary_claims": [
            _statement_to_dict(statement)
            for statement in bundle.summary_claims
        ],
        "tokens": sum(unit.tokens for unit in bundle.units),
        "units": [unit_to_dict(unit) for unit in bundle.units],
        "relations": [_relation_to_dict(rel) for rel in bundle.relations],
        "gaps": [_finding_to_dict(gap) for gap in bundle.gaps],
        "manifest": {
            **_machine_manifest(bundle),
            "selection": bundle.selection,
        },
    }


def _build_json(bundle: Bundle, options: _RenderOptions) -> str:
    import json

    return (
        json.dumps(
            _json_payload(bundle),
            indent=options.indent,
            ensure_ascii=False,
            default=str,
        )
        + "\n"
    )


def _build_jsonl(bundle: Bundle, _options: _RenderOptions) -> str:
    import json

    lines = [
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "type": "header",
                "subject": bundle.subject,
                "kind": bundle.kind,
                "summary": _bundle_summary(bundle),
                "summary_claims": [
                    _statement_to_dict(statement)
                    for statement in bundle.summary_claims
                ],
                "units": len(bundle.units),
            },
            ensure_ascii=False,
            default=str,
        )
    ]
    lines.extend(
        json.dumps(
            {"schema": SCHEMA_VERSION, "type": "unit", **unit_to_dict(unit)},
            ensure_ascii=False,
            default=str,
        )
        for unit in bundle.units
    )
    lines.append(
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "type": "manifest",
                # Kept at the record top level so a streaming consumer can
                # inspect accounting without understanding extraction metadata.
                "selection": bundle.selection,
                "relations": [
                    _relation_to_dict(relation) for relation in bundle.relations
                ],
                "gaps": [_finding_to_dict(gap) for gap in bundle.gaps],
                "manifest": _machine_manifest(bundle),
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Human renderers
# ---------------------------------------------------------------------------


_HTML_CSS = """\
* { box-sizing: border-box; }
html { background: #f3efe5; color: #1c2525; font-family: Charter, "Bitstream Charter", "Palatino Linotype", serif; }
body { margin: 0; line-height: 1.55; }
a { color: #315f55; text-decoration: underline; }
a:hover, a:focus { color: #b84a2b; }
.skip-link { position: absolute; left: -9999px; }
.skip-link:focus { left: 16px; top: 16px; background: #fffdf7; padding: 8px; }
.masthead, main, .document-footer { max-width: 1050px; margin: 0 auto; padding-left: 20px; padding-right: 20px; }
.masthead { padding-top: 50px; padding-bottom: 28px; border-bottom: 4px solid #1c2525; }
.kicker { color: #b84a2b; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 11px; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; }
h1 { max-width: 760px; margin: 8px 0 12px; font-size: 52px; line-height: .94; letter-spacing: -2px; }
.dek { max-width: 760px; margin: 0; color: #596565; font-size: 17px; }
.status-rail { width: 100%; margin-top: 24px; }
.status-card { min-height: 58px; margin-bottom: 7px; padding: 11px 13px; border-top: 2px solid #b8b7aa; background: #fbf8ef; }
.status-card strong { display: block; margin-top: 4px; font-size: 15px; }
.status-label { color: #596565; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 10px; font-weight: bold; letter-spacing: 1px; text-transform: uppercase; }
.status-success { border-color: #315f55; background: #dfe9e4; }
.status-fallback, .status-warning { border-color: #b84a2b; background: #f1ddd3; }
main { padding-top: 22px; padding-bottom: 55px; }
section { margin-top: 24px; }
h2 { margin: 28px 0 14px; padding-bottom: 6px; border-bottom: 1px solid #b8b7aa; font-size: 23px; line-height: 1.1; }
h2 .h2-index { margin-right: 6px; color: #b84a2b; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 10px; letter-spacing: 1px; }
h3 { margin: 0; font-size: 1.08rem; line-height: 1.25; }
.claims, .units, .relations, .findings, .references, .drop-records { margin: 0; padding: 0; list-style: none; }
.claim { margin-bottom: 16px; padding: 17px 18px 15px 22px; border-left: 4px solid #b84a2b; background: #fffdf7; page-break-inside: avoid; }
.claim-primary { color: #1c2525; font-size: 19px; font-weight: bold; text-decoration: none; }
.claim-primary::after { content: " ↗"; color: #b84a2b; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 11px; }
.evidence-row, .citation-row, .drop-links { margin-top: 10px; }
.evidence-link, .origin-link, .reference-link { margin-right: 9px; word-wrap: break-word; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 10px; line-height: 1.4; }
.evidence-link { display: inline-block; margin-bottom: 4px; padding: 3px 6px; border: 1px solid #adc1bb; background: #dfe9e4; text-decoration: none; }
.unit { width: 100%; padding-top: 17px; padding-bottom: 17px; border-top: 1px solid #b8b7aa; page-break-inside: avoid; }
.unit-meta { margin-bottom: 9px; color: #596565; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 10px; line-height: 1.55; word-wrap: break-word; }
.unit-copy { width: 100%; }
.unit-meta code, .record-code, .relation code { font-family: "Cascadia Mono", Menlo, Consolas, monospace; }
.unit-body { margin: 10px 0; white-space: pre-wrap; word-wrap: break-word; }
pre.unit-body { padding: 12px; border-left: 3px solid #315f55; background: #e8e8df; white-space: pre-wrap; }
.relation, .finding, .reference { padding: 10px 0; border-top: 1px solid #b8b7aa; page-break-inside: avoid; }
.relation-kind, .finding-kind { color: #b84a2b; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 10px; font-weight: bold; letter-spacing: 1px; text-transform: uppercase; }
.model-note, .empty { color: #596565; font-style: italic; }
.selection { padding: 0; }
.selection-grid { margin: 0; padding: 16px 18px; border: 1px solid #b8b7aa; background: #e8e4d9; }
.selection-grid div { margin: 0 0 10px; padding-top: 6px; border-top: 1px solid #b8b7aa; }
.selection-grid dt { color: #596565; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 9px; font-weight: bold; letter-spacing: 1px; text-transform: uppercase; }
.selection-grid dd { margin: 3px 0 0; font-family: "Cascadia Mono", Menlo, Consolas, monospace; word-wrap: break-word; }
.drop-records { margin-top: 16px; }
.drop-record { margin: 9px 0 0; }
.drop-record-fragment { margin: 0; }
.record-code { display: inline; color: #25302f; font-size: 9px; line-height: 1.45; white-space: pre-wrap; word-wrap: break-word; }
.document-footer { padding-top: 16px; padding-bottom: 36px; border-top: 2px solid #1c2525; color: #596565; font-family: "Cascadia Mono", Menlo, Consolas, monospace; font-size: 10px; line-height: 1.4; }
"""


def _html_safe(value: object, *, attribute: bool = False) -> str:
    """Escape untrusted text and render controls visibly in HTML."""

    import html

    safe = _ansi_safe(str(value))
    if attribute:
        safe = safe.replace("\n", r"\n")
    return html.escape(safe, quote=attribute)


def _html_origin_link(
    origin: Origin,
    *,
    label: str | None = None,
    anchor_id: str | None = None,
) -> str:
    target = _html_safe(_origin_target(origin), attribute=True)
    visible = _html_safe(label if label is not None else _origin_label(origin))
    id_attribute = (
        f' id="{_html_safe(anchor_id, attribute=True)}"' if anchor_id else ""
    )
    return f'<a{id_attribute} class="origin-link" href="{target}">{visible}</a>'


def _origin_from_record(value: object) -> Origin | None:
    if not isinstance(value, dict):
        return None
    source = value.get("source")
    ref = value.get("ref")
    span = value.get("char_span")
    if not isinstance(source, str) or not isinstance(ref, str):
        return None
    resolved_span: tuple[int, int] | None = None
    if (
        isinstance(span, list)
        and len(span) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in span)
    ):
        resolved_span = (span[0], span[1])
    try:
        return Origin(source, ref, resolved_span)
    except ValueError:  # pragma: no cover - records originate in validated IR
        return None


def _drop_record_origins(kind: str, record: dict[str, Any]) -> list[Origin]:
    values: list[object]
    if kind == "unit":
        values = [record.get("origin")]
    elif kind == "statement":
        raw = record.get("origins")
        values = list(raw) if isinstance(raw, list) else []
    else:
        values = []
    return [origin for value in values if (origin := _origin_from_record(value))]


def _model_presentation(bundle: Bundle) -> tuple[str, str, str]:
    models = bundle.manifest.get("models")
    records = models if isinstance(models, list) else []
    latest = next((item for item in reversed(records) if isinstance(item, dict)), None)
    if latest is None:
        return (
            "Model-free",
            "Deterministic representation; no synthesis model was invoked.",
            "neutral",
        )
    model = latest.get("model")
    model_label = model if isinstance(model, str) and model else "recorded backend"
    outcome = latest.get("outcome")
    outcome_label = outcome if isinstance(outcome, str) and outcome else "recorded"
    fallback = latest.get("fallback")
    fallback_used = (
        isinstance(fallback, dict) and fallback.get("used") is True
    ) or outcome_label.startswith("fallback-")
    if fallback_used:
        return "Grounded fallback", f"{model_label} · {outcome_label}", "fallback"
    if outcome_label == "success":
        return "Model synthesis", f"{model_label} · accepted", "success"
    return "Model run", f"{model_label} · {outcome_label}", "warning"


def _build_html(bundle: Bundle, options: _RenderOptions) -> str:
    """Build one self-contained, navigable UTF-8 HTML5 artifact."""

    unit_by_id = {unit.id: unit for unit in bundle.units}
    model_label, model_detail, model_state = _model_presentation(bundle)
    selection = bundle.selection
    dropped = selection["dropped"]
    requested = selection["requested"]
    ceiling = str(requested) if requested is not None else "unlimited"

    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>AutoTLDR — {_html_safe(bundle.subject)}</title>",
        '<meta name="generator" content="AutoTLDR">',
        "<style>",
        _HTML_CSS.rstrip(),
        "</style>",
        "</head>",
        "<body>",
        '<a class="skip-link" href="#main">Skip to semantic brief</a>',
        '<header class="masthead">',
        f'<div class="kicker">AutoTLDR · {_html_safe(bundle.kind)}</div>',
        f"<h1>{_html_safe(bundle.subject)}</h1>",
        '<p class="dek">A bounded semantic brief with addressable evidence and an explicit omission audit.</p>',
        '<div class="status-rail" aria-label="Artifact status">',
        f'<div class="status-card status-{model_state}"><span class="status-label">Synthesis</span><strong>{_html_safe(model_label)}</strong><span>{_html_safe(model_detail)}</span></div>',
        f'<div class="status-card"><span class="status-label">Selected</span><strong>{selection["selected_units"]} units · {selection["selected_statements"]} claims</strong><span>{dropped["unit_count"]} units omitted for budget</span></div>',
        f'<div class="status-card"><span class="status-label">Portable budget</span><strong>{selection["used"]} / {_html_safe(ceiling)} bytes</strong><span>{_html_safe(selection["counter"])} · {_html_safe(selection["scope"])}</span></div>',
        "</div>",
        "</header>",
        '<main id="main">',
        '<section id="summary" class="primary" aria-labelledby="summary-title">',
        '<h2 id="summary-title" data-index="01"><span class="h2-index">01</span> What matters</h2>',
    ]
    if bundle.summary_claims:
        lines.append('<ol class="claims">')
        for statement in bundle.summary_claims:
            primary = statement.origins[0]
            lines.extend(
                [
                    f'<li class="claim" id="statement-{statement.id}">',
                    f'<a id="link-statement-{statement.id}-primary" class="claim-primary" href="{_html_safe(_origin_target(primary), attribute=True)}">{_html_safe(statement.content)}</a>',
                    '<div class="evidence-row" aria-label="Evidence units">',
                ]
            )
            for evidence_index, unit_id in enumerate(statement.evidence_unit_ids):
                evidence = unit_by_id[unit_id]
                lines.append(
                    f'<a id="link-statement-{statement.id}-evidence-{evidence_index}" class="evidence-link" data-unit-id="{unit_id}" href="{_html_safe(_origin_target(evidence.origin), attribute=True)}">evidence {unit_id}</a>'
                )
            lines.append("</div>")
            if options.cite:
                lines.append('<div class="citation-row" aria-label="Claim origins">')
                lines.extend(
                    _html_origin_link(
                        origin,
                        anchor_id=f"link-statement-{statement.id}-origin-{origin_index}",
                    )
                    for origin_index, origin in enumerate(statement.origins)
                )
                lines.append("</div>")
            lines.append("</li>")
        lines.append("</ol>")
    else:
        lines.append(f'<p class="model-note">{_html_safe(_bundle_summary(bundle))}</p>')
    lines.extend(["</section>", '<section id="units" aria-labelledby="units-title">', '<h2 id="units-title" data-index="02"><span class="h2-index">02</span> Evidence ledger</h2>'])
    if not bundle.units:
        lines.append('<p class="empty">No semantic units fit this output.</p>')
    else:
        lines.append('<div class="units">')
        for unit in bundle.units:
            path = " › ".join(unit.structure) if unit.structure else str(unit.modality)
            target = _html_safe(_origin_target(unit.origin), attribute=True)
            body_tag = "pre" if str(unit.modality) == "code" else "p"
            lines.extend(
                [
                    f'<article class="unit role-{_html_safe(unit.role, attribute=True)}" id="unit-{unit.id}">',
                    '<div class="unit-meta">',
                    f'<span>{_html_safe(unit.modality)} · {_html_safe(unit.role)}</span><br>',
                    f'<code>{unit.id}</code><br>',
                    f'<span>confidence {unit.confidence:.3f}</span>',
                    "</div>",
                    '<div class="unit-copy">',
                    f'<h3><a id="link-unit-{unit.id}-heading" href="{target}">{_html_safe(path)}</a></h3>',
                    f'<{body_tag} class="unit-body">{_html_safe(unit.content)}</{body_tag}>',
                    _html_origin_link(
                        unit.origin,
                        anchor_id=f"link-unit-{unit.id}-origin",
                    ),
                    "</div>",
                    "</article>",
                ]
            )
        lines.append("</div>")
    lines.append("</section>")

    if bundle.relations:
        lines.extend(['<section id="relations" aria-labelledby="relations-title">', '<h2 id="relations-title" data-index="03"><span class="h2-index">03</span> Relations</h2>', '<ul class="relations">'])
        for relation in bundle.relations:
            evidence = f' — {_html_safe(relation.evidence)}' if relation.evidence else ""
            lines.append(
                '<li class="relation">'
                f'<a href="#unit-{relation.src}"><code>{relation.src}</code></a> '
                f'<span class="relation-kind">{_html_safe(relation.kind)}</span> '
                f'<a href="#unit-{relation.dst}"><code>{relation.dst}</code></a> '
                f'<span>· confidence {relation.confidence:.3f}{evidence}</span>'
                "</li>"
            )
        lines.extend(["</ul>", "</section>"])

    orphans = [gap for gap in bundle.gaps if gap.kind is GapKind.ORPHAN]
    gaps = [gap for gap in bundle.gaps if gap.kind is not GapKind.ORPHAN]
    if orphans or gaps:
        lines.extend(['<section id="findings" aria-labelledby="findings-title">', '<h2 id="findings-title" data-index="04"><span class="h2-index">04</span> Absence &amp; uncertainty</h2>', '<ul class="findings">'])
        for finding in [*orphans, *gaps]:
            lines.append(
                '<li class="finding">'
                f'<span class="finding-kind">{_html_safe(finding.kind)}</span> '
                f'{_html_safe(finding.content)} · {_html_origin_link(finding.origin, anchor_id=f"link-finding-{finding.id}")}'
                "</li>"
            )
        lines.extend(["</ul>", "</section>"])

    reference_origins: list[Origin] = []
    for statement in bundle.summary_claims:
        reference_origins.extend(statement.origins)
    reference_origins.extend(unit.origin for unit in bundle.units)
    reference_origins.extend(gap.origin for gap in bundle.gaps)
    for kind, records in (
        ("unit", dropped["reported"]),
        ("statement", dropped["reported_statements"]),
    ):
        for record in records:
            reference_origins.extend(_drop_record_origins(kind, record))
    references = list(dict.fromkeys(reference_origins))
    lines.extend(['<section id="references" aria-labelledby="references-title">', '<h2 id="references-title" data-index="05"><span class="h2-index">05</span> References</h2>'])
    if references:
        lines.append('<ol class="references">')
        for index, origin in enumerate(references, start=1):
            span = (
                f" · chars {origin.char_span[0]}–{origin.char_span[1]}"
                if origin.char_span is not None
                else ""
            )
            lines.append(
                f'<li class="reference" id="reference-{index}"><a id="link-reference-{index}" class="reference-link" href="{_html_safe(_origin_target(origin), attribute=True)}">{_html_safe(_origin_label(origin))}</a>{_html_safe(span)}</li>'
            )
        lines.append("</ol>")
    else:
        lines.append('<p class="empty">No retained source references.</p>')
    lines.append("</section>")

    lines.extend(
        [
            f'<section id="selection" class="selection" aria-labelledby="selection-title" data-used="{selection["used"]}" data-requested="{_html_safe(ceiling, attribute=True)}" data-available="{selection["available"]}">',
            '<h2 id="selection-title" data-index="06"><span class="h2-index">06</span> Selection audit</h2>',
            '<dl class="selection-grid">',
            f'<div><dt>Portable bytes</dt><dd><span data-selection-used>{selection["used"]}</span> / <span data-selection-requested>{_html_safe(ceiling)}</span></dd></div>',
            f'<div><dt>Unlimited form</dt><dd>{selection["available"]}</dd></div>',
            f'<div><dt>Dropped</dt><dd>{dropped["unit_count"]} units · {dropped["relation_count"]} relations · {dropped["statement_count"]} claims</dd></div>',
            f'<div><dt>Drop-set SHA-256</dt><dd>{dropped["digest"]}</dd></div>',
            "</dl>",
        ]
    )
    drop_groups = (
        ("unit", dropped["reported"]),
        ("relation", dropped["reported_relations"]),
        ("statement", dropped["reported_statements"]),
    )
    if any(records for _kind, records in drop_groups):
        lines.append('<div class="drop-records" aria-label="Complete omission inventory">')
        for kind, records in drop_groups:
            for record_index, record in enumerate(records):
                canonical = _human_drop_record(kind, record)
                # PyMuPDF Story treats a paragraph as an indivisible pagination
                # unit in some layouts.  A long canonical record could therefore
                # be clipped at a page boundary.  Short block fragments preserve
                # every character in order while giving Story safe break points.
                lines.append(
                    f'<div class="drop-record" data-drop-kind="{_html_safe(kind, attribute=True)}">'
                )
                for offset in range(0, len(canonical), 64):
                    fragment = canonical[offset : offset + 64]
                    lines.append(
                        '<p class="drop-record-fragment"><code class="record-code">'
                        f'{_html_safe(fragment)}</code></p>'
                    )
                lines.append("</div>")
                origins = _drop_record_origins(kind, record)
                if origins:
                    lines.append('<p class="drop-links">')
                    lines.extend(
                        _html_origin_link(
                            origin,
                            anchor_id=(
                                f"link-drop-{kind}-{record_index}-origin-{origin_index}"
                            ),
                        )
                        for origin_index, origin in enumerate(origins)
                    )
                    lines.append("</p>")
        lines.append("</div>")
    lines.extend(
        [
            "</section>",
            "</main>",
            '<footer class="document-footer">Self-contained AutoTLDR artifact · no scripts · no network assets</footer>',
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(lines) + "\n"


def _build_markdown(bundle: Bundle, options: _RenderOptions) -> str:
    lines = [
        f"# AutoTLDR: `{_escape_code(bundle.subject)}`",
        "",
    ]
    if bundle.summary_claims:
        for statement in bundle.summary_claims:
            if options.cite:
                citations = " ".join(
                    _markdown_citation(origin)
                    for origin in statement.origins
                )
                lines.extend([f"*{statement.content}* — {citations}", ""])
            else:
                lines.extend(
                    [
                        f"*{statement.content}* — Summary key: "
                        f"`statement-{statement.id}`",
                        "",
                    ]
                )
    else:
        lines.extend([f"*{_bundle_summary(bundle)}*", ""])
    lines.extend(["## Units", ""])
    if not bundle.units:
        lines.extend(["_No semantic units fit this output._", ""])
    for unit in bundle.units:
        key = unit.id
        path = " › ".join(unit.structure) if unit.structure else str(unit.modality)
        lines.extend([f"### {path} · `{key}`", ""])
        if str(unit.modality) == "code":
            language = str(unit.meta.get("language") or "")
            lines.extend([f"```{language}", unit.content, "```"])
        else:
            lines.append(unit.content)
        if options.cite:
            lines.append(f"\n{_markdown_citation(unit.origin)}")
        else:
            lines.append(f"\nOrigin key: `{key}`")
        lines.append("")

    if bundle.relations:
        lines.extend(["## Relations", ""])
        for relation in bundle.relations:
            evidence = " ".join(relation.evidence.split())
            suffix = f" — {evidence}" if evidence else ""
            lines.append(
                f"- `{relation.src}` **{relation.kind}** `{relation.dst}`"
                f" (confidence {relation.confidence:.3f}){suffix}"
            )
        lines.append("")

    gaps = [gap for gap in bundle.gaps if gap.kind is not GapKind.ORPHAN]
    orphans = [gap for gap in bundle.gaps if gap.kind is GapKind.ORPHAN]
    if orphans:
        lines.extend(["## Orphans", ""])
        for orphan in orphans:
            if options.cite:
                lines.append(
                    f"- {orphan.content} — "
                    f"{_markdown_citation(orphan.origin)}"
                )
            else:
                lines.append(f"- `gap-{orphan.id}` {orphan.content}")
        lines.append("")

    if gaps:
        lines.extend(["## Gaps", ""])
        for gap in gaps:
            if options.cite:
                lines.append(f"- {gap.content} — {_markdown_citation(gap.origin)}")
            else:
                lines.append(f"- `gap-{gap.id}` {gap.content}")
        lines.append("")

    if not options.cite:
        lines.extend(["## Source map", ""])
        for statement in bundle.summary_claims:
            origins = ", ".join(
                f"`{_origin_label(origin)}`"
                for origin in statement.origins
            )
            lines.append(f"- `statement-{statement.id}` → {origins}")
        for unit in bundle.units:
            lines.append(f"- `{unit.id}` → `{_origin_label(unit.origin)}`")
        for gap in bundle.gaps:
            lines.append(f"- `gap-{gap.id}` → `{_origin_label(gap.origin)}`")
        lines.append("")

    lines.extend(_markdown_selection(bundle.selection))
    return "\n".join(lines).rstrip() + "\n"


def _build_ansi(bundle: Bundle, options: _RenderOptions) -> str:
    def paint(code: str, value: str) -> str:
        safe = _ansi_safe(value)
        return f"\x1b[{code}m{safe}\x1b[0m" if options.color else safe

    lines = [paint("1;36", "AutoTLDR") + f"  {_ansi_safe(bundle.subject)}"]
    if bundle.summary_claims:
        for statement in bundle.summary_claims:
            marker = (
                "; ".join(_origin_label(origin) for origin in statement.origins)
                if options.cite
                else f"origin key statement-{statement.id}"
            )
            lines.append(paint("2", f"{statement.content} [{marker}]"))
    else:
        lines.append(paint("2", _bundle_summary(bundle)))
    lines.append("")
    if not bundle.units:
        lines.extend(["No semantic units fit this output.", ""])
    for unit in bundle.units:
        key = unit.id
        path = " › ".join(unit.structure) if unit.structure else str(unit.modality)
        lines.append(paint("1", f"{path}  {key}"))
        # Split only the one allowed line control.  ``str.splitlines`` also
        # consumes U+2028/U+2029 and several C0 controls, silently mutating an
        # otherwise atomic unit.
        lines.extend(_ansi_safe(unit.content).split("\n") or [""])
        if options.cite:
            lines.append(paint("2", f"[{_origin_label(unit.origin)}]"))
        else:
            lines.append(paint("2", f"[origin key {key}]"))
        lines.append("")

    if bundle.relations:
        lines.append(paint("1", "Relations"))
        for relation in bundle.relations:
            evidence = " ".join(_ansi_safe(relation.evidence).split())
            suffix = f"  {evidence}" if evidence else ""
            lines.append(
                _ansi_safe(
                    f"{relation.src}  {relation.kind}  {relation.dst}  "
                    f"confidence {relation.confidence:.3f}{suffix}"
                )
            )
        lines.append("")

    gaps = [gap for gap in bundle.gaps if gap.kind is not GapKind.ORPHAN]
    orphans = [gap for gap in bundle.gaps if gap.kind is GapKind.ORPHAN]
    if orphans:
        lines.append(paint("1;33", "Orphans"))
        for orphan in orphans:
            marker = (
                _origin_label(orphan.origin)
                if options.cite
                else f"origin key gap-{orphan.id}"
            )
            lines.append(_ansi_safe(f"- {orphan.content} [{marker}]"))
        lines.append("")

    if gaps:
        lines.append(paint("1;33", "Gaps"))
        for gap in gaps:
            marker = (
                _origin_label(gap.origin)
                if options.cite
                else f"origin key gap-{gap.id}"
            )
            lines.append(_ansi_safe(f"- {gap.content} [{marker}]"))
        lines.append("")

    if not options.cite:
        lines.append(paint("1", "Source map"))
        for statement in bundle.summary_claims:
            origins = "; ".join(
                _origin_label(origin) for origin in statement.origins
            )
            lines.append(
                _ansi_safe(f"statement-{statement.id}  {origins}")
            )
        for unit in bundle.units:
            lines.append(_ansi_safe(f"{unit.id}  {_origin_label(unit.origin)}"))
        for gap in bundle.gaps:
            lines.append(
                _ansi_safe(f"gap-{gap.id}  {_origin_label(gap.origin)}")
            )
        lines.append("")

    selection = bundle.selection
    requested = selection["requested"]
    ceiling = str(requested) if requested is not None else "unlimited"
    lines.append(paint("1", "Selection"))
    lines.append(
        f"{selection['used']}/{ceiling} portable tokens "
        f"({selection['counter']}, {selection['scope']}); "
        f"available {selection['available']}"
    )
    dropped = selection["dropped"]
    lines.append(
        f"dropped {dropped['unit_count']} units and "
        f"{dropped['relation_count']} relations and "
        f"{dropped['statement_count']} summary claims"
    )
    if (
        dropped["unit_count"]
        or dropped["relation_count"]
        or dropped["statement_count"]
    ):
        lines.append(f"drop-set sha256 {dropped['digest']}")
        for item in dropped["reported"]:
            lines.append(f"- {_human_drop_record('unit', item)}")
        for relation in dropped["reported_relations"]:
            lines.append(f"- {_human_drop_record('relation', relation)}")
        for statement in dropped["reported_statements"]:
            lines.append(f"- {_human_drop_record('statement', statement)}")
    return "\n".join(lines).rstrip() + "\n"


def _markdown_selection(selection: dict[str, Any]) -> list[str]:
    requested = selection["requested"]
    ceiling = str(requested) if requested is not None else "unlimited"
    dropped = selection["dropped"]
    lines = [
        "## Selection",
        "",
        f"- Portable tokens: **{selection['used']} / {ceiling}** "
        f"(`{selection['counter']}`, `{selection['scope']}`; "
        f"unlimited form {selection['available']})",
        f"- Selected units: **{selection['selected_units']}**",
        f"- Dropped for budget: **{dropped['unit_count']} units**, "
        f"**{dropped['relation_count']} relations**, "
        f"**{dropped['statement_count']} summary claims**",
    ]
    if (
        dropped["unit_count"]
        or dropped["relation_count"]
        or dropped["statement_count"]
    ):
        lines.append(f"- Drop-set SHA-256: `{dropped['digest']}`")
        for item in dropped["reported"]:
            lines.append(f"  - {_human_drop_record('unit', item)}")
        for relation in dropped["reported_relations"]:
            lines.append(f"  - {_human_drop_record('relation', relation)}")
        for statement in dropped["reported_statements"]:
            lines.append(f"  - {_human_drop_record('statement', statement)}")
    return lines


def _bundle_summary(bundle: Bundle) -> str:
    """Return the renderer-neutral collection statement unchanged."""

    return bundle.summary


_BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)


def _ansi_safe(value: str) -> str:
    """Render untrusted terminal controls visibly instead of executing them."""

    parts: list[str] = []
    for character in value:
        codepoint = ord(character)
        unsafe = (
            (codepoint < 0x20 and character != "\n")
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or character in {"\u2028", "\u2029"}
            or character in _BIDI_CONTROLS
        )
        if not unsafe:
            parts.append(character)
        elif codepoint <= 0xFF:
            parts.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            parts.append(f"\\u{codepoint:04x}")
        else:
            parts.append(f"\\U{codepoint:08x}")
    return "".join(parts)


def _origin_label(origin: Origin) -> str:
    return f"{origin.source}#{origin.ref}"


def _markdown_citation(origin: Origin) -> str:
    label = _origin_label(origin).replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({_origin_target(origin)})"


def _origin_target(origin: Origin) -> str:
    from urllib.parse import quote, urlsplit, urlunsplit

    source = origin.source
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"}:
        if origin.ref.startswith(("http://", "https://")):
            return origin.ref
        fragment = quote(origin.ref, safe="/:!#")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment))

    safe_source = quote(source, safe="/._-~")
    if origin.ref == "source":
        return safe_source
    if origin.ref.startswith("line:"):
        start = origin.ref.removeprefix("line:").split("-", 1)[0]
        return f"{safe_source}#L{start}"
    if origin.ref.startswith("page:"):
        page = origin.ref.removeprefix("page:").split("#", 1)[0]
        return f"{safe_source}#page={page}"
    return f"{safe_source}#{quote(origin.ref, safe='/:!')}"


def _escape_code(value: str) -> str:
    return value.replace("`", "\\`")


def _clean(meta: dict[str, Any]) -> dict[str, Any]:
    """Drop empty extraction metadata values while keeping booleans and zero."""

    return {
        key: value
        for key, value in meta.items()
        if value is not None and value != [] and value != {}
    }


def _machine_manifest(bundle: Bundle) -> dict[str, Any]:
    manifest = _clean(bundle.manifest)
    # An explicit empty list is evidence that no model ran, not visual clutter.
    # D-013 requires downstream callers to distinguish deterministic output
    # from an omitted/unknown enrichment record.
    manifest["models"] = list(bundle.manifest.get("models", ()))
    return manifest


_Builder = Callable[[Bundle, _RenderOptions], str]
_BUILDERS: dict[str, _Builder] = {
    "ansi": _build_ansi,
    "html": _build_html,
    "md": _build_markdown,
    "json": _build_json,
    "jsonl": _build_jsonl,
}
