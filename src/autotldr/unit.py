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


class Role(StrEnum):
    """What the unit is *doing*.

    This is the differentiating field and the one whose reliability is not yet
    established. Extractors that cannot determine a role must emit UNKNOWN
    rather than guessing; Stage 2's eval decides how much of this taxonomy
    survives contact with real documents.
    """

    UNKNOWN = "unknown"
    CLAIM = "claim"
    DEFINITION = "definition"
    PROCEDURE = "procedure"
    PARAMETER = "parameter"
    CAVEAT = "caveat"
    RESULT = "result"
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


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate.

    Deliberately not a real tokenizer. Importing one costs more than the entire
    cold-start budget, and every consumer of this number is making a budgeting
    decision that tolerates a few percent of error. Swap in a real count only
    where exactness is paid for.
    """
    return max(1, len(text) // 4)


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
        h = hashlib.blake2b(digest_size=8)
        h.update(self.source.encode())
        h.update(b"\0")
        h.update(self.origin.ref.encode())
        h.update(b"\0")
        h.update(self.content.encode())
        return h.hexdigest()

    def __post_init__(self) -> None:
        if not self.tokens:
            object.__setattr__(self, "tokens", _estimate_tokens(self.content))


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


@dataclass(slots=True)
class Extraction:
    """Everything one source yielded.

    ``gaps`` is a first-class result, not an error channel. "This spreadsheet
    documents no assumptions" is a finding worth emitting.
    """

    source: str
    kind: str
    units: list[Unit] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return sum(u.tokens for u in self.units)

    def by_modality(self, modality: Modality) -> list[Unit]:
        return [u for u in self.units if u.modality is modality]
