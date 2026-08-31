"""Renderer-neutral bundle projection types.

Extraction answers "what did the source yield?".  Rendering answers the
separate question "which of those units fit this output shape and budget?".
The latter is necessarily renderer-aware because JSON escaping, Markdown
citations, and ANSI controls have different serialized costs.  This module
holds the common projected shape without importing a renderer or tokenizer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .unit import (
    Extraction,
    GapKind,
    GroundedStatement,
    Origin,
    Relation,
    Unit,
)


@dataclass(frozen=True, slots=True)
class Finding:
    """A source-scoped finding such as an absence or extraction gap."""

    content: str
    origin: Origin
    kind: GapKind = GapKind.EXTRACTION

    @property
    def id(self) -> str:
        digest = hashlib.blake2b(digest_size=16)
        span = (
            "-"
            if self.origin.char_span is None
            else f"{self.origin.char_span[0]}:{self.origin.char_span[1]}"
        )
        for field in (
            str(self.kind),
            self.origin.source,
            self.origin.ref,
            span,
            self.content,
        ):
            encoded = field.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()


@dataclass(slots=True)
class Bundle:
    """The selected representation consumed by one renderer."""

    subject: str
    kind: str
    summary: str = ""
    summary_claims: list[GroundedStatement] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    gaps: list[Finding] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    selection: dict[str, Any] = field(default_factory=dict)


def project(result: Extraction, selected_indexes: set[int]) -> Bundle:
    """Return the induced bundle for ``selected_indexes`` in source order.

    Relations survive only when both endpoints survive.  Units are atomic: a
    renderer may keep one with its origin or omit it, but never truncate a claim
    into content that no longer round-trips to the source.
    """

    units = [
        unit
        for index, unit in enumerate(result.units)
        if index in selected_indexes
    ]
    ids = {unit.id for unit in units}
    relations = [
        relation
        for relation in result.relations
        if relation.src in ids and relation.dst in ids
    ]
    gaps = [
        Finding(content=gap.content, origin=gap.origin, kind=gap.kind)
        for gap in result.gaps
    ]
    # A projected claim is addressable only when every evidence endpoint is in
    # the same projected bundle.  Keeping its origin strings while omitting an
    # evidence unit would create a machine-level dangling reference and make a
    # tight budget look more grounded than it is.
    summary_claims = [
        statement
        for statement in result.summary_claims
        if all(unit_id in ids for unit_id in statement.evidence_unit_ids)
    ]
    summary = (
        " ".join(claim.content for claim in summary_claims)
        if summary_claims
        else (
            f"{result.kind} source with {len(result.units)} addressable semantic "
            f"unit(s), {len(result.relations)} relation(s), and "
            f"{len(result.gaps)} reported gap(s)."
        )
    )
    from . import __version__

    raw_models = result.meta.get("models")
    models = list(raw_models) if isinstance(raw_models, list) else []
    raw_role_backend = result.meta.get("role_backend")
    role_backend = (
        raw_role_backend
        if isinstance(raw_role_backend, str) and raw_role_backend
        else "deterministic-rules-v1"
    )

    return Bundle(
        subject=result.source,
        kind=result.kind,
        # Stage 3 retains its deterministic structural count.  Stage 4 supplies
        # sentence-granular claims whose origins remain mandatory regardless of
        # which evidence units survive budget selection.
        summary=summary,
        summary_claims=summary_claims,
        units=units,
        relations=relations,
        gaps=gaps,
        manifest={
            **result.meta,
            # Extraction/fusion owns inference truth. Projection supplies the
            # deterministic defaults but must never erase an upstream model or
            # enrichment backend declaration.
            "models": models,
            "role_backend": role_backend,
            "unit_token_estimator": "char4-floor-v1",
            "unit_id_scheme": "blake2b-128-framed-origin-modality-content-v2",
            "finding_id_scheme": "blake2b-128-framed-kind-origin-content-v2",
            "statement_id_scheme": (
                "blake2b-128-framed-content-origins-evidence-v1"
            ),
            "versions": {
                "autotldr": __version__,
                "representation": 2,
            },
        },
    )
