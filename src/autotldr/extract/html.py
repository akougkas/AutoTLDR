"""Provenance-preserving HTML extraction for local pages and URLs.

The fast path intentionally uses the stdlib parser.  It removes obvious chrome
(``nav``, ``header``, ``footer``, scripts, forms) but keeps each surviving block
mapped to an existing element ID or a deterministic element address.  Cleaned
text that cannot be mapped back to the page is not emitted.
"""

from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..unit import Extraction, Modality, Origin, Role, Unit

_BLOCKS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "pre",
    "blockquote",
    "dt",
    "dd",
    "figcaption",
    "caption",
    # These containers normally own nested blocks, but direct text inside them
    # is still main content and must not disappear merely because it lacks a
    # paragraph wrapper.
    "main",
    "article",
    "div",
}
_SKIP = {
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "form",
    "aside",
    "template",
    "svg",
    "canvas",
}
_SKIP_ROLES = {
    "navigation",
    "banner",
    "contentinfo",
    "complementary",
    "dialog",
    "search",
}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def extract(path: Path) -> Extraction:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path.name}: HTML input is not strict UTF-8 at byte {exc.start}"
        ) from exc
    return extract_html(text, source=str(path))


def extract_html(
    text: str,
    *,
    source: str,
    requested_url: str | None = None,
    content_type: str | None = None,
) -> Extraction:
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{source}: HTML contains an unpaired Unicode surrogate at "
            f"character {exc.start}"
        ) from exc

    parser = _Blocks(source, text)
    parser.feed(text)
    parser.close()

    result = Extraction(source=source, kind="url" if _is_url(source) else "html")
    cleaned, cleaner = _trafilatura_text(text)
    cleaned_normalized = _normalize(cleaned) if cleaned else ""
    normalized_blocks = [
        block.code_payload(text) if block.is_code else _normalize(block.text)
        for block in parser.blocks
    ]
    mapped = {
        index
        for index, body in enumerate(normalized_blocks)
        if cleaned_normalized and body and body in cleaned_normalized
    }
    # Trafilatura is an optional main-content oracle, never a source of claims.
    # Only exact whitespace-normalized native DOM blocks may survive its filter.
    # If no block maps, retain the native result rather than emitting its
    # unaddressable cleaned rewrite or returning an empty success.
    use_cleaner_filter = bool(cleaned_normalized and mapped)
    id_counts = Counter(
        block.element_id for block in parser.blocks if block.element_id
    )
    headings: list[tuple[int, str]] = []
    emitted_blocks = 0
    linked_blocks_retained = 0
    for index, block in enumerate(parser.blocks):
        body = normalized_blocks[index]
        if block.is_code and block.nested_markup and not block.nested_code:
            result.add_gap(
                "code block contains nested HTML markup, so no exact payload "
                "claim was emitted",
                ref=_origin_ref(
                    source,
                    block,
                    duplicate_id=id_counts[block.element_id] > 1,
                ),
            )
            continue
        if block.is_code and block.nested_code:
            # A nested <code> frame carries the exact payload and its own
            # source span; emitting the wrapping <pre> as well would duplicate
            # it and would include the wrapper markup in the content.
            continue
        if not body:
            continue

        is_heading = block.tag.startswith("h") and block.tag[1:].isdigit()
        if use_cleaner_filter and index not in mapped and not (is_heading or block.is_code):
            # Native addressable links are both source meaning and the bounded
            # documentation crawler's only traversal signal.  Optional main-
            # content cleanup may discard a short paragraph while retaining a
            # nearby heading; silently following that hint would lose the
            # paragraph and make its link unreachable.  Obvious navigation
            # chrome has already been excluded structurally by _SKIP/_SKIP_ROLES.
            if not block.links:
                continue
            linked_blocks_retained += 1

        if is_heading:
            level = int(block.tag[1:])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, body))
            structure = tuple(title for _, title in headings)
            salience = max(0.45, 1.0 - level * 0.1)
            meta = {
                "heading": True,
                "heading_level": level,
                "element": block.tag,
                "element_id": block.element_id or None,
                "definition_cue": True,
            }
        else:
            structure = tuple(title for _, title in headings)
            salience = 0.55
            meta = {
                "element": block.tag,
                "element_id": block.element_id or None,
                "links": block.links or None,
            }

        ref = _origin_ref(source, block, duplicate_id=id_counts[block.element_id] > 1)
        result.units.append(
            Unit(
                source=source,
                modality=Modality.CODE if block.is_code else Modality.PROSE,
                content=body,
                origin=Origin(source, ref, block.char_span),
                role=Role.UNKNOWN,
                structure=structure,
                salience=salience,
                meta=meta,
            )
        )
        emitted_blocks += 1

        seen_links: set[str] = set()
        for target, label in block.links:
            absolute = urljoin(source, target) if _is_url(source) else target
            if absolute in seen_links:
                continue
            seen_links.add(absolute)
            result.units.append(
                Unit(
                    source=source,
                    modality=Modality.REFERENCE,
                    content=absolute,
                    origin=Origin(source, ref),
                    role=Role.UNKNOWN,
                    structure=structure,
                    salience=0.3,
                    meta={
                        "target": absolute,
                        "label": label or None,
                        "ref_kind": "url" if _is_url(absolute) else "path",
                    },
                )
            )

    if not result.units:
        result.gaps.append("no addressable main-content blocks found in HTML")
    result.meta.update(
        {
            "requested_url": requested_url,
            "final_url": source if _is_url(source) else None,
            "content_type": content_type,
            "elements_seen": parser.element_count,
            "addressable_blocks": len(parser.blocks),
            "emitted_blocks": emitted_blocks,
            "main_content_filter": cleaner,
            "trafilatura_mapped_blocks": len(mapped) if cleaner == "trafilatura" else None,
            "linked_blocks_retained": linked_blocks_retained,
        }
    )
    return result


class _Block:
    __slots__ = (
        "tag",
        "line",
        "number",
        "element_id",
        "parts",
        "links",
        "active_link",
        "content_start",
        "content_end",
        "first_data",
        "nested_markup",
        "nested_code",
    )

    def __init__(
        self,
        tag: str,
        line: int,
        number: int,
        element_id: str,
        content_start: int,
    ) -> None:
        self.tag = tag
        self.line = line
        self.number = number
        self.element_id = element_id
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.active_link: tuple[str, list[str]] | None = None
        self.content_start = content_start
        self.content_end: int | None = None
        self.first_data: int | None = None
        self.nested_markup = False
        self.nested_code = False

    @property
    def text(self) -> str:
        return "".join(self.parts)

    @property
    def is_code(self) -> bool:
        return self.tag in {"pre", "code"}

    @property
    def char_span(self) -> tuple[int, int] | None:
        if self.content_end is None:
            return None
        return (self.content_start, self.content_end)

    def code_payload(self, source_text: str) -> str:
        if self.char_span is None or self.nested_markup:
            return ""
        start, end = self.char_span
        return source_text[start:end]


class _Blocks(HTMLParser):
    """Collect addressable native blocks without flattening their children.

    Frames remain on a stack while nested blocks are parsed.  Text following a
    child therefore returns to its owning ``main``/``article``/``div`` frame
    instead of disappearing, while child text is not copied into the parent.
    """

    def __init__(self, source: str, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.text = text
        self.blocks: list[_Block] = []
        self.frames: list[_Block] = []
        self.skip_depth = 0
        self.element_count = 0
        self._line_starts = [0]
        position = 0
        for raw in text.splitlines(keepends=True):
            position += len(raw)
            self._line_starts.append(position)

    @property
    def current(self) -> _Block | None:
        return self.frames[-1] if self.frames else None

    def _offset(self) -> int:
        line, column = self.getpos()
        index = max(0, min(line - 1, len(self._line_starts) - 1))
        return min(len(self.text), self._line_starts[index] + column)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        self.element_count += 1
        if self.skip_depth:
            if tag not in _VOID:
                self.skip_depth += 1
            return
        attrs_dict = dict(attrs)
        role = str(attrs_dict.get("role") or "").casefold()
        hidden = "hidden" in attrs_dict or str(
            attrs_dict.get("aria-hidden") or ""
        ).casefold() == "true"
        if tag in _SKIP or role in _SKIP_ROLES or hidden:
            if tag not in _VOID:
                self.skip_depth = 1
            return

        current = self.current
        code_in_pre = tag == "code" and current is not None and current.tag == "pre"
        if current is not None and current.tag == "pre" and tag not in _VOID:
            current.nested_markup = True
            current.nested_code = code_in_pre

        if tag in _BLOCKS or code_in_pre:
            # HTML implicitly closes a paragraph when another block begins.
            if current is not None and current.tag == "p" and tag in _BLOCKS:
                self._finish_frame(len(self.frames) - 1, self._offset())
            raw = self.get_starttag_text() or ""
            self.frames.append(_Block(
                tag,
                self.getpos()[0],
                self.element_count,
                attrs_dict.get("id", ""),
                self._offset() + len(raw),
            ))
        elif tag == "br" and current is not None:
            current.parts.append("\n")
        elif tag == "a" and current is not None and attrs_dict.get("href"):
            current.active_link = (attrs_dict["href"], [])

    def handle_startendtag(self, tag: str, attrs) -> None:
        # A self-closing element has no payload and cannot become a useful
        # semantic block.  Preserve only line breaks on the current frame.
        self.element_count += 1
        if not self.skip_depth and tag.lower() == "br" and self.current is not None:
            self.current.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "a":
            for frame in reversed(self.frames):
                if frame.active_link:
                    target, label = frame.active_link
                    frame.links.append((target, "".join(label).strip()))
                    frame.active_link = None
                    break
            return

        matching = next(
            (index for index in range(len(self.frames) - 1, -1, -1)
             if self.frames[index].tag == tag),
            None,
        )
        if matching is not None:
            end = self._offset()
            while len(self.frames) > matching:
                self._finish_frame(len(self.frames) - 1, end)

    def handle_data(self, data: str) -> None:
        current = self.current
        if self.skip_depth or current is None:
            return
        current.parts.append(data)
        if data.strip() and current.first_data is None:
            current.first_data = self._offset()
        if current.active_link is not None:
            current.active_link[1].append(data)

    def close(self) -> None:
        super().close()
        while self.frames:
            self._finish_frame(len(self.frames) - 1, len(self.text))
        self.blocks.sort(
            key=lambda block: (
                block.first_data
                if block.first_data is not None
                else block.content_start,
                block.number,
            )
        )

    def _finish_frame(self, index: int, end: int) -> None:
        frame = self.frames.pop(index)
        frame.content_end = max(frame.content_start, end)
        payload = frame.code_payload(self.text) if frame.is_code else frame.text
        # Keep nested-markup code frames long enough for the caller to emit a
        # source-addressed gap explaining why an exact claim was declined.
        if payload.strip() or (frame.is_code and frame.nested_markup):
            self.blocks.append(frame)


def _origin_ref(source: str, block: _Block, *, duplicate_id: bool = False) -> str:
    # Duplicate DOM IDs do not address one unique element.  Fall back to the
    # deterministic parser ordinal for every duplicate rather than giving two
    # claims the same ambiguous origin and therefore the same stable ID.
    fragment = (
        block.element_id
        if block.element_id and not duplicate_id
        else f"element-{block.number}"
    )
    if _is_url(source):
        base = source.split("#", 1)[0]
        return f"{base}#{fragment}"
    return f"line:{block.line}#element:{block.number}"


def _is_url(value: str) -> bool:
    return urlsplit(value).scheme in {"http", "https"}


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _trafilatura_text(text: str) -> tuple[str | None, str]:
    """Return optional cleaned text as a filter, never as emitted content."""

    try:
        import trafilatura
    except ModuleNotFoundError as exc:  # pragma: no cover - install-dependent
        if exc.name != "trafilatura":
            raise
        return None, "native-dom"

    try:
        cleaned = trafilatura.extract(
            text,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
    except Exception:
        # The native DOM path remains complete and addressable when an optional
        # quality adapter rejects malformed-but-parseable HTML.
        return None, "native-dom-trafilatura-fallback"
    return (cleaned if isinstance(cleaned, str) and cleaned.strip() else None), "trafilatura"
