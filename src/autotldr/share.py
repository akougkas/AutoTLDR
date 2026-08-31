"""Shareable, paginated output built from the core semantic projection.

The HTML renderer lives in :mod:`autotldr.render` because it is a UTF-8 text
shape.  PDF is deliberately a separate bytes API: it feeds that exact HTML to
PyMuPDF Story, then adds deterministic pagination and URI annotations while
reusing the core renderer's one selection and budget policy.

PyMuPDF remains a point-of-use optional import.  Importing this module must be
as cheap as importing the text renderers.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from .errors import MissingOptionalDependency
from .render import RenderOptions, _build_html, _render_budgeted
from .unit import Extraction

_A4_MARGIN_X = 54
_A4_MARGIN_TOP = 54
_A4_CONTENT_BOTTOM = 770
_FOOTER_TOP = 790
_FOOTER_BOTTOM = 812
_PDF_SIZE_QUANTUM = 256
_PDF_SIZE_HEADROOM = 256


@dataclass(frozen=True, slots=True)
class _LinkPosition:
    page_number: int
    element_id: str
    href: str
    rect: tuple[float, float, float, float]


def _load_pymupdf() -> Any:
    try:
        import pymupdf
    except ModuleNotFoundError as exc:
        if exc.name != "pymupdf":
            raise
        raise MissingOptionalDependency(
            feature="PDF output",
            dependency="pymupdf",
            extra="pdf",
            detail="PyMuPDF is required for shareable PDF output",
        ) from exc
    return pymupdf


def render_pdf(
    result: Extraction,
    *,
    budget: int | None = None,
    cite: bool = True,
) -> bytes:
    """Render one deterministic A4 PDF under an exact complete-byte ceiling.

    Selection is the same claim-first, salience-ranked prefix used by every
    core renderer.  Units remain atomic and the PDF carries the full inline
    D-015 omission inventory.  A ceiling that cannot hold any valid projection
    raises :class:`autotldr.render.BudgetTooSmall`; bytes are never truncated.
    """

    options = RenderOptions(output="pdf", cite=cite, color=False, indent=2)
    payload = _render_budgeted(
        result,
        output="pdf",
        budget=budget,
        options=options,
        builder=_build_pdf,
        exhaustive_prefixes=True,
    )
    if not isinstance(payload, bytes):  # pragma: no cover - binary API boundary
        raise TypeError("PDF renderer returned text")
    return payload


def _build_pdf(bundle: Any, options: RenderOptions) -> bytes:
    # PDF uses the same selection policy, but its complete wire is binary and
    # must not be mislabeled as D-015's UTF-8 text counter.
    bundle.selection["counter"] = "binary-byte-v1"
    html = _build_html(bundle, options)
    embedded_used = int(bundle.selection["used"])
    embedded_available = int(bundle.selection["available"])
    minimum_size = embedded_used
    if bundle.selection["requested"] is None:
        minimum_size = max(minimum_size, embedded_available)
    return _story_pdf(html, minimum_size=minimum_size)


def _story_pdf(html: str, *, minimum_size: int = 0) -> bytes:
    """Lay out the exact HTML string and return reproducible PDF bytes."""

    pymupdf = _load_pymupdf()
    output = io.BytesIO()
    writer = pymupdf.DocumentWriter(output)
    page_box = pymupdf.paper_rect("a4")
    content_box = pymupdf.Rect(
        _A4_MARGIN_X,
        _A4_MARGIN_TOP,
        page_box.width - _A4_MARGIN_X,
        _A4_CONTENT_BOTTOM,
    )
    positions: list[_LinkPosition] = []

    def page_rect(_rect_number: int, _filled: tuple[float, float, float, float]):
        return page_box, content_box, None

    def record_position(position: Any) -> None:
        href = getattr(position, "href", None)
        if (
            not isinstance(href, str)
            or not href
            or href.startswith("#")
            or not (int(getattr(position, "open_close", 0)) & 1)
        ):
            return
        rect = pymupdf.Rect(getattr(position, "rect"))
        if rect.width <= 0 or rect.height <= 0:
            return
        raw_id = getattr(position, "id", None)
        positions.append(
            _LinkPosition(
                page_number=int(getattr(position, "page_num")),
                element_id=raw_id if isinstance(raw_id, str) else "",
                href=href,
                rect=(rect.x0, rect.y0, rect.x1, rect.y1),
            )
        )

    story = pymupdf.Story(html=html)
    try:
        story.write(writer, page_rect, positionfn=record_position)
    finally:
        writer.close()

    with pymupdf.open(stream=output.getvalue(), filetype="pdf") as document:
        page_total = document.page_count
        for page_index, page in enumerate(document):
            footer = pymupdf.Rect(
                _A4_MARGIN_X,
                _FOOTER_TOP,
                page.rect.width - _A4_MARGIN_X,
                _FOOTER_BOTTOM,
            )
            remaining = page.insert_textbox(
                footer,
                f"Page {page_index + 1} of {page_total}",
                fontname="helv",
                fontsize=9,
                color=(0.35, 0.4, 0.4),
                align=pymupdf.TEXT_ALIGN_CENTER,
                overlay=True,
            )
            if remaining < 0:  # pragma: no cover - fixed geometry invariant
                raise AssertionError("PDF page number did not fit its footer box")

        for position in _merged_link_positions(positions, pymupdf):
            page_index = position.page_number - 1
            if not 0 <= page_index < document.page_count:
                raise AssertionError("Story returned a link on an unknown PDF page")
            page = document[page_index]
            rect = pymupdf.Rect(position.rect) & page.rect
            if rect.width <= 0 or rect.height <= 0:
                continue
            page.insert_link(
                {
                    "kind": pymupdf.LINK_URI,
                    "from": rect,
                    "uri": position.href,
                }
            )

        _snap_content_stream_noise(document)

        document.set_metadata({})
        if hasattr(document, "del_xml_metadata"):
            document.del_xml_metadata()
        payload = document.tobytes(
            garbage=4,
            clean=True,
            deflate=True,
            deflate_fonts=True,
            no_new_id=True,
        )
    return _stabilize_pdf_size(payload, minimum_size=minimum_size)


def _snap_content_stream_noise(document: Any) -> None:
    """Collapse sub-micron layout noise so identical input yields identical bytes.

    MuPDF lays a Story out in floating point and accumulates rounding error
    along the way.  Coordinates that are exactly zero in the source geometry are
    emitted as values around 1e-6, and the accumulated noise is not reproducible
    between two layouts of the same HTML.  Every observed divergence in a
    59-page projection was one of these: 635 differing tokens, all below 2.3e-6.

    Snapping magnitudes under 1e-4 PDF units — roughly 35 nanometres at 72 dpi,
    four orders below the thinnest rule the renderer draws — to an exact zero
    removes the whole class without moving anything a reader could see.
    """

    # Every content stream is rewritten, not only the ones that carried noise:
    # an updated stream and an untouched one reach the writer through different
    # compression paths, so rewriting conditionally would trade one source of
    # byte drift for another.
    for page in document:
        for xref in page.get_contents():
            document.update_stream(
                xref, _canonical_content_stream(document.xref_stream(xref))
            )


# A PDF real whose magnitude is below 1e-4: at least four zeros after the point,
# and not preceded by a digit or another point, so 12.00001 is left alone.
_NEAR_ZERO_REAL = re.compile(rb"(?<![0-9.])[-+]?0*\.0{4,}[0-9]*")


def _canonical_content_stream(payload: bytes) -> bytes:
    """Rewrite near-zero reals outside string literals to an exact ``0``.

    Literal ``(...)`` and hex ``<...>`` strings are copied verbatim: they carry
    glyph and text bytes that must never be reinterpreted as numeric tokens.
    """

    out = bytearray()
    index = 0
    length = len(payload)
    while index < length:
        char = payload[index : index + 1]
        if char == b"(":
            out += char
            index += 1
            depth = 1
            while index < length and depth:
                current = payload[index : index + 1]
                if current == b"\\":
                    out += payload[index : index + 2]
                    index += 2
                    continue
                if current == b"(":
                    depth += 1
                elif current == b")":
                    depth -= 1
                out += current
                index += 1
            continue
        if char == b"<":
            if payload[index : index + 2] == b"<<":
                out += b"<<"
                index += 2
                continue
            end = payload.find(b">", index)
            if end < 0:
                out += payload[index:]
                break
            out += payload[index : end + 1]
            index = end + 1
            continue
        end = length
        for delimiter in (b"(", b"<"):
            position = payload.find(delimiter, index)
            if position >= 0:
                end = min(end, position)
        out += _NEAR_ZERO_REAL.sub(b"0", payload[index:end])
        index = end
    return bytes(out)


def _stabilize_pdf_size(payload: bytes, *, minimum_size: int = 0) -> bytes:
    """Add counted trailing PDF whitespace so byte fixed points always exist.

    Compressed content can change by a few bytes when the embedded ``used`` or
    ``available`` decimal changes, yielding a cycle with no self-referential
    byte-count fixed point.  PDF permits comments and whitespace after EOF.
    Reserving a small deterministic quantum absorbs those compression deltas;
    the padding remains part of the complete counted output.
    """

    target = (
        (max(len(payload) + _PDF_SIZE_HEADROOM, minimum_size) + _PDF_SIZE_QUANTUM - 1)
        // _PDF_SIZE_QUANTUM
        * _PDF_SIZE_QUANTUM
    )
    startxref = payload.rfind(b"startxref")
    eof = payload.rfind(b"%%EOF")
    if startxref < 0 or eof < startxref or payload[eof:].rstrip() != b"%%EOF":
        raise ValueError("generated PDF lacks one terminal startxref/%%EOF trailer")
    marker = b"% AutoTLDR deterministic byte padding "
    missing = target - len(payload)
    if missing < len(marker) + 1:  # pragma: no cover - headroom exceeds marker
        target += _PDF_SIZE_QUANTUM
        missing = target - len(payload)
    padding = marker + (b"." * (missing - len(marker) - 1)) + b"\n"
    # Keep %%EOF terminal. Comments before startxref are conforming PDF
    # whitespace and do not alter the xref byte offset recorded in the trailer.
    return payload[:startxref] + padding + payload[startxref:]


def _merged_link_positions(
    positions: list[_LinkPosition], pymupdf: Any
) -> list[_LinkPosition]:
    """Coalesce Story's word/space callbacks into line-level annotations."""

    rows: dict[tuple[int, str, str, float, float], Any] = {}
    for position in positions:
        if not position.element_id:
            continue
        rect = pymupdf.Rect(position.rect)
        key = (
            position.page_number,
            position.element_id,
            position.href,
            round(rect.y0, 2),
            round(rect.y1, 2),
        )
        previous = rows.get(key)
        rows[key] = rect if previous is None else previous | rect
    merged = [
        _LinkPosition(
            page_number=key[0],
            element_id=key[1],
            href=key[2],
            rect=(rect.x0, rect.y0, rect.x1, rect.y1),
        )
        for key, rect in rows.items()
    ]
    return sorted(
        merged,
        key=lambda item: (
            item.page_number,
            item.rect[1],
            item.rect[0],
            item.element_id,
            item.href,
        ),
    )
