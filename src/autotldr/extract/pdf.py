"""PDFs with a text layer.

The fast path, and the one most PDFs take. Blocks come out of the page in
reading order with their coordinates, which is enough to recover paragraphs,
headings by relative font size, and per-page origins.

Scanned PDFs have no text layer. This module detects that and says so rather
than returning an empty result that looks like a successful read of an empty
document. Tier 4 owns OCR and v1 does not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

_CAPTION = re.compile(r"^\s*(?:figure|fig\.?|table|listing|algorithm)\s*\d+", re.IGNORECASE)
_HEADING_NUM = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S")
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
_URL = re.compile(r"https?://[^\s<>\"')]+")
_CAVEAT_CUE = re.compile(
    r"\b(?:however|limitation|caveat|we do not|does not|cannot|assume[sd]?|"
    r"future work|threat to validity)\b",
    re.IGNORECASE,
)
_RESULT_CUE = re.compile(
    r"\b(?:we (?:find|observe|measure|report)|results? show|improves? by|"
    r"outperforms?|achieves?|reduces? .{0,20}by)\b",
    re.IGNORECASE,
)


def extract(path: Path) -> Extraction:
    source = str(path)
    result = Extraction(source=source, kind="pdf")

    with pymupdf.open(path) as doc:
        sizes = _body_size(doc)
        section: tuple[str, ...] = ()
        section_unit: str | None = None
        empty_pages = 0

        for page_no, page in enumerate(doc, start=1):
            blocks = page.get_text("dict").get("blocks", [])
            text_blocks = [b for b in blocks if b.get("type") == 0]
            if not text_blocks:
                empty_pages += 1
                continue

            for span_no, block in enumerate(text_blocks):
                text, size = _flatten(block)
                text = text.strip()
                if not text or len(text) < 3:
                    continue

                ref = f"page:{page_no}#span:{span_no}"
                origin = Origin(source, ref)

                if _is_heading(text, size, sizes):
                    section = (text,) if size >= sizes.heading else section[:1] + (text,)
                    unit = Unit(
                        source=source,
                        modality=Modality.PROSE,
                        content=text,
                        origin=origin,
                        role=Role.DEFINITION,
                        structure=section,
                        salience=0.85,
                        meta={"page": page_no, "font_size": round(size, 1), "heading": True},
                    )
                    result.units.append(unit)
                    section_unit = unit.id
                    continue

                unit = Unit(
                    source=source,
                    modality=Modality.PROSE,
                    content=text,
                    origin=origin,
                    role=_role(text),
                    structure=section,
                    salience=0.65 if _CAPTION.match(text) else 0.5,
                    meta={
                        "page": page_no,
                        "font_size": round(size, 1),
                        "caption": bool(_CAPTION.match(text)),
                    },
                )
                result.units.append(unit)

                if section_unit:
                    result.relations.append(
                        Relation(
                            src=section_unit,
                            dst=unit.id,
                            kind=RelationKind.DESCRIBES,
                            evidence="section containment",
                        )
                    )

                result.units.extend(_references(source, text, origin, section))

        result.meta.update(
            {
                "pages": doc.page_count,
                "pages_without_text": empty_pages,
                "title": (doc.metadata or {}).get("title") or None,
            }
        )

    if not result.units:
        result.gaps.append(
            "no text layer: this is a scanned PDF and needs OCR, which is tier 4 and not in v1"
        )
    elif result.meta["pages_without_text"]:
        result.gaps.append(
            f"{result.meta['pages_without_text']} of {result.meta['pages']} pages "
            f"carry no text layer and were skipped"
        )

    return result


class _Sizes:
    __slots__ = ("body", "heading")

    def __init__(self, body: float, heading: float) -> None:
        self.body = body
        self.heading = heading


def _body_size(doc) -> _Sizes:
    """Establish the document's body text size.

    Headings are relative, not absolute: 12pt is a heading in a 9pt paper and
    body text in a 12pt report. Sampling the most common span size gives the
    baseline everything else is judged against.
    """
    counts: dict[float, int] = {}
    for page in doc.pages(0, min(doc.page_count, 8)):
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", ()):
                for span in line.get("spans", ()):
                    size = round(span.get("size", 0), 1)
                    counts[size] = counts.get(size, 0) + len(span.get("text", ""))
    if not counts:
        return _Sizes(10.0, 12.0)
    body = max(counts, key=lambda k: counts[k])
    return _Sizes(body, body * 1.15)


def _flatten(block) -> tuple[str, float]:
    """Join a block's spans into text, and report its dominant font size."""
    parts: list[str] = []
    weight: dict[float, int] = {}
    for line in block.get("lines", ()):
        for span in line.get("spans", ()):
            text = span.get("text", "")
            parts.append(text)
            size = round(span.get("size", 0), 1)
            weight[size] = weight.get(size, 0) + len(text)
        parts.append(" ")
    size = max(weight, key=lambda k: weight[k]) if weight else 0.0
    return _dehyphenate("".join(parts)), size


def _dehyphenate(text: str) -> str:
    """Rejoin words split across a line break.

    PDF extraction otherwise yields tokens like ``perfor- mance``, which corrupt
    every downstream identifier match that fusion depends on.
    """
    return re.sub(r"(\w)-\s+(\w)", r"\1\2", " ".join(text.split()))


def _is_heading(text: str, size: float, sizes: _Sizes) -> bool:
    if len(text) > 120 or text.endswith((".", ",", ";")):
        return False
    if size >= sizes.heading:
        return True
    return bool(_HEADING_NUM.match(text)) and len(text) < 80


def _role(text: str) -> Role:
    if _CAPTION.match(text):
        return Role.EXAMPLE
    if _CAVEAT_CUE.search(text):
        return Role.CAVEAT
    if _RESULT_CUE.search(text):
        return Role.RESULT
    return Role.UNKNOWN


def _references(source: str, text: str, origin: Origin, section: tuple[str, ...]) -> list[Unit]:
    """DOIs and URLs, which are fusion signal 1 for anything paper-shaped."""
    units: list[Unit] = []
    seen: set[str] = set()
    for pattern, kind in ((_DOI, "doi"), (_URL, "url")):
        for m in pattern.finditer(text):
            target = m.group(0).rstrip(".,);")
            if target in seen:
                continue
            seen.add(target)
            units.append(
                Unit(
                    source=source,
                    modality=Modality.REFERENCE,
                    content=target,
                    origin=origin,
                    structure=section,
                    salience=0.3,
                    meta={"target": target, "ref_kind": kind},
                )
            )
    return units
