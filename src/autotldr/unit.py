"""The intermediate representation.

Every input format normalizes into these types and every output renders from
them. Adding an input costs one extractor and zero renderers; adding an output
costs one renderer and zero extractors.

This module is on the CLI's cold-start path, so it imports only from the stdlib
and stays cheap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Modality(StrEnum):
    """What kind of thing a unit is, independent of the file it came from."""

    PROSE = "prose"
    CODE = "code"
    TABLE = "table"
    RECORD = "record"
    SCHEMA = "schema"
    EQUATION = "equation"
    REFERENCE = "reference"
    SOURCE = "source"


class Role(StrEnum):
    """What the unit is *doing*.

    Stage 2 retained only roles that met the preregistered recoverability gate
    in at least one evaluated arm. Extractors that cannot determine one of
    these roles must emit UNKNOWN rather than guessing.
    """

    UNKNOWN = "unknown"
    DEFINITION = "definition"
    PROCEDURE = "procedure"
    CAVEAT = "caveat"
    EXAMPLE = "example"
    DECISION = "decision"
    ASSUMPTION = "assumption"
    LIMITATION = "limitation"


class RelationKind(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    IMPLEMENTS = "implements"
    DERIVES_FROM = "derives-from"
    EXEMPLIFIES = "exemplifies"
    DESCRIBES = "describes"
    PRODUCED_BY = "produced-by"
    REFERENCES = "references"
    # Fusion signals 2 and 3 prove a symmetric correspondence, not a
    # directional semantic claim such as ``describes`` or ``implements``.
    CORRESPONDS = "corresponds"


@dataclass(frozen=True, slots=True)
class Origin:
    """An addressable back-pointer into a source.

    ``ref`` is format-specific by design and is both machine-parseable and
    readable: ``line:120-134``, ``page:7#span:3``, ``Sheet2!C14``, ``para:44``,
    ``cell:12#output:0``, ``/run3/pressure``.

    ``char_span`` carries exact offsets into the source's extracted text where
    the extractor knows them. It is what makes a citation verifiable rather than
    merely plausible: a claim can be checked by re-reading the span.
    """

    source: str
    ref: str
    char_span: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("origin source must not be empty")
        if not self.ref.strip():
            raise ValueError("origin ref must not be empty")
        _require_utf8(self.source, "origin source")
        _require_utf8(self.ref, "origin ref")
        if self.char_span is not None:
            start, end = self.char_span
            if start < 0 or end < start:
                raise ValueError("origin char_span must be a non-negative half-open span")

    @property
    def scheme(self) -> str:
        """The addressing scheme, taken from the ref's prefix.

        Falls back to ``opaque`` for refs that carry no ``scheme:`` prefix, such
        as spreadsheet A1 notation and HDF5 paths, which are already canonical
        in their own world.
        """
        head, sep, _ = self.ref.partition(":")
        if not sep or "!" in head or head.startswith("/"):
            return "opaque"
        return head

    def __str__(self) -> str:
        return f"{self.source}#{self.ref}"


class GapKind(StrEnum):
    """Why an addressable absence finding exists."""

    EXTRACTION = "extraction"
    UNRESOLVED_REFERENCE = "unresolved-reference"
    ORPHAN = "orphan"


class Gap(str):
    """An absence or limitation finding with its own source address.

    ``Gap`` deliberately subclasses :class:`str` so the Stage 1 extractor API
    remains pleasant (and existing callers can still search gap text), while
    making provenance impossible to lose at the representation boundary.
    Whole-source findings use ``ref='source'``; extractors should provide a
    narrower native ref whenever the finding is tied to a known cell, line,
    paragraph, or other addressable object.
    """

    origin: Origin
    kind: GapKind

    def __new__(
        cls,
        content: str,
        origin: Origin,
        kind: GapKind | str = GapKind.EXTRACTION,
    ) -> "Gap":
        if not content.strip():
            raise ValueError("gap content must not be empty")
        _require_utf8(content, "gap content")
        value = str.__new__(cls, content)
        value.origin = origin
        value.kind = GapKind(kind)
        return value

    @property
    def content(self) -> str:
        return str(self)


class _GapList(list[Gap]):
    """List that upgrades legacy string appends to source-addressed gaps."""

    def __init__(self, source: str, values=()) -> None:
        self.source = source
        super().__init__()
        self.extend(values)

    def _coerce(self, value: str | Gap) -> Gap:
        if isinstance(value, Gap):
            return value
        return Gap(str(value), Origin(self.source, "source"))

    def append(self, value: str | Gap) -> None:
        super().append(self._coerce(value))

    def extend(self, values) -> None:
        super().extend(self._coerce(value) for value in values)

    def insert(self, index: int, value: str | Gap) -> None:
        super().insert(index, self._coerce(value))

    def __setitem__(self, index, value) -> None:
        if isinstance(index, slice):
            super().__setitem__(index, [self._coerce(item) for item in value])
        else:
            super().__setitem__(index, self._coerce(value))


def _estimate_tokens(text: str) -> int:
    """Return the cheap, diagnostic ``char4-floor-v1`` estimate.

    This is deliberately not a real tokenizer and is never the renderer's
    compliance counter.  Exact ``--budget`` enforcement happens over the full
    canonical wire payload under the named ``utf8-byte-v1`` counter; this field
    remains useful only as lightweight unit metadata.
    """
    return max(1, len(text) // 4)


def _require_utf8(value: str, label: str) -> None:
    """Reject strings that cannot enter the canonical UTF-8 wire format."""

    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{label} contains an unpaired Unicode surrogate at character "
            f"{exc.start}"
        ) from exc


@dataclass(frozen=True, slots=True)
class Unit:
    """One addressable piece of meaning."""

    source: str
    modality: Modality
    content: str
    origin: Origin
    role: Role = Role.UNKNOWN
    structure: tuple[str, ...] = ()
    salience: float = 0.5
    confidence: float = 1.0
    tokens: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Content-addressed identity, stable across runs.

        Keyed on origin as well as content so that two files asserting the same
        sentence remain distinguishable, which fusion depends on when it reports
        agreement or contradiction between sources.
        """
        # v2 includes modality because two semantic units can legitimately
        # carry the same text at the same native address (for example an HTML
        # paragraph whose entire body is also an outbound reference).  A
        # 128-bit digest also makes accidental identity aliasing negligible for
        # large collections; renderers still reject any duplicate IDs they do
        # encounter rather than guessing which endpoint a relation means.
        h = hashlib.blake2b(digest_size=16)
        span = (
            "-"
            if self.origin.char_span is None
            else f"{self.origin.char_span[0]}:{self.origin.char_span[1]}"
        )
        for field in (
            self.source,
            self.origin.ref,
            span,
            str(self.modality),
            self.content,
        ):
            encoded = field.encode("utf-8")
            h.update(len(encoded).to_bytes(8, "big"))
            h.update(encoded)
        return h.hexdigest()

    def __post_init__(self) -> None:
        if self.source != self.origin.source:
            raise ValueError("unit source must match origin source")
        if not self.content.strip():
            raise ValueError("unit content must not be empty")
        _require_utf8(self.content, "unit content")
        for part in self.structure:
            _require_utf8(part, "unit structure")
        if not 0.0 <= self.salience <= 1.0:
            raise ValueError("unit salience must be between 0 and 1")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("unit confidence must be between 0 and 1")
        if not self.tokens:
            object.__setattr__(self, "tokens", _estimate_tokens(self.content))
        elif self.tokens < 0:
            raise ValueError("unit tokens must not be negative")


@dataclass(frozen=True, slots=True)
class Relation:
    """A typed edge between two units.

    Within a file these come from structure. Across files they are the fusion
    problem, and every cross-file relation carries the evidence that produced it
    so an inferred link is never mistaken for an observed one.
    """

    src: str
    dst: str
    kind: RelationKind
    evidence: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.src.strip() or not self.dst.strip():
            raise ValueError("relation endpoints must not be empty")
        _require_utf8(self.src, "relation source")
        _require_utf8(self.dst, "relation destination")
        _require_utf8(self.evidence, "relation evidence")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("relation confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class GroundedStatement:
    """One synthesized sentence with explicit, addressable evidence.

    A collection statement is derived rather than copied verbatim, so one
    source span cannot honestly stand in for it.  ``origins`` records every
    native address cited by the sentence, while ``evidence_unit_ids`` ties
    those addresses to existing semantic units in the unbudgeted extraction.
    Renderers retain the origins even when a referenced evidence unit does not
    survive selection.
    """

    content: str
    origins: tuple[Origin, ...]
    evidence_unit_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("grounded statement content must not be empty")
        _require_utf8(self.content, "grounded statement content")

        # Accept ordinary iterables at the API boundary but freeze their order
        # immediately so identity and rendering remain deterministic.
        object.__setattr__(self, "origins", tuple(self.origins))
        object.__setattr__(
            self, "evidence_unit_ids", tuple(self.evidence_unit_ids)
        )
        if not self.origins:
            raise ValueError("grounded statement must cite at least one origin")
        if len(set(self.origins)) != len(self.origins):
            raise ValueError("grounded statement origins must be unique")
        if not self.evidence_unit_ids:
            raise ValueError(
                "grounded statement must name at least one evidence unit id"
            )
        if len(set(self.evidence_unit_ids)) != len(self.evidence_unit_ids):
            raise ValueError(
                "grounded statement evidence unit ids must be unique"
            )
        for unit_id in self.evidence_unit_ids:
            if not unit_id.strip():
                raise ValueError(
                    "grounded statement evidence unit ids must not be empty"
                )
            _require_utf8(unit_id, "grounded statement evidence unit id")

    @property
    def id(self) -> str:
        """Stable, length-framed identity for source-map and wire use."""

        digest = hashlib.blake2b(digest_size=16)

        def frame(value: str) -> None:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)

        frame("grounded-statement-v1")
        frame(self.content)
        frame(str(len(self.origins)))
        for origin in self.origins:
            frame(origin.source)
            frame(origin.ref)
            frame(
                "-"
                if origin.char_span is None
                else f"{origin.char_span[0]}:{origin.char_span[1]}"
            )
        frame(str(len(self.evidence_unit_ids)))
        for unit_id in self.evidence_unit_ids:
            frame(unit_id)
        return digest.hexdigest()


@dataclass(slots=True)
class Extraction:
    """Everything one source or fused collection yielded.

    ``gaps`` is a first-class result, not an error channel. "This spreadsheet
    documents no assumptions" is a finding worth emitting.
    """

    source: str
    kind: str
    units: list[Unit] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    summary_claims: list[GroundedStatement] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.source or not self.kind.strip():
            raise ValueError("extraction source and kind must not be empty")
        _require_utf8(self.source, "extraction source")
        _require_utf8(self.kind, "extraction kind")
        if not isinstance(self.gaps, _GapList):
            self.gaps = _GapList(self.source, self.gaps)

    def add_gap(
        self,
        content: str,
        *,
        ref: str = "source",
        origin: Origin | None = None,
        kind: GapKind | str = GapKind.EXTRACTION,
    ) -> Gap:
        """Append and return an explicitly addressed absence finding."""

        resolved = origin or Origin(self.source, ref)
        if resolved.source != self.source and self.kind != "collection":
            raise ValueError("gap origin source must match extraction source")
        gap = Gap(content, resolved, kind)
        self.gaps.append(gap)
        return gap

    @property
    def tokens(self) -> int:
        return sum(u.tokens for u in self.units)

    def by_modality(self, modality: Modality) -> list[Unit]:
        return [u for u in self.units if u.modality is modality]
