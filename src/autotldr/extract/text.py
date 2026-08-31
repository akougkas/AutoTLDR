"""Markdown and plain text.

Tier 0: the structure is already in the file, so nothing here needs a model. The
heading hierarchy gives ``structure``, line ranges give verifiable origins, and
outbound links and file paths become REFERENCE units, which is fusion signal 1
and the cheapest way to discover that two files in a folder are related.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_FENCE = re.compile(r"^\s*(```|~~~)(.*)$")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BARE_URL = re.compile(r"(?<![(\w])(https?://[^\s<>\"')]+)")
# A path-ish token with a known-ish extension. Deliberately conservative: a false
# reference is worse than a missed one, because fusion reports references as
# observed rather than inferred.
_PATH_REF = re.compile(
    r"(?<![\w/])((?:\.{1,2}/)?[\w.\-]+(?:/[\w.\-]+)*"
    r"\.[A-Za-z][A-Za-z0-9]{0,7})(?![\w/])"
)

_DEFINITION = re.compile(r"^\s*(?:[-*]\s+)?\*\*([^*]+)\*\*\s*[:—-]\s+\S")
_CAVEAT_CUE = re.compile(
    r"\b(?:note that|caveat|caution|warning|however|but only if|be aware|"
    r"do not|don't|never|must not|limitation)\b",
    re.IGNORECASE,
)


def extract(path: Path) -> Extraction:
    data = path.read_bytes()
    try:
        # Decode the bytes directly.  TextIO's universal-newline translation
        # would make a CRLF citation point into a different string than the
        # source that was actually read.
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path.name}: text input is not strict UTF-8 at byte {exc.start}"
        ) from exc
    kind = "markdown" if path.suffix.lower() in {".md", ".markdown"} else "text"
    return extract_text(text, source=str(path), kind=kind)


def extract_text(text: str, *, source: str, kind: str = "text") -> Extraction:
    """Extract already-acquired text while preserving its logical source.

    stdin and HTTP acquisition must not leak a temporary filename into origins
    or stable unit IDs.  They enter through this function; path invocation keeps
    the small wrapper above.
    """

    is_markdown = kind == "markdown"

    lines, line_offsets = _source_lines(text)

    result = Extraction(source=source, kind=kind)
    heading_stack: list[tuple[int, str]] = []
    section_unit_by_path: dict[tuple[str, ...], str] = {}

    for block in _blocks(lines, is_markdown, line_offsets, len(text)):
        structure = tuple(title for _, title in heading_stack)

        if block.kind == "heading":
            level, title = block.heading  # type: ignore[misc]
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            structure = tuple(t for _, t in heading_stack)
            unit = _unit(
                source,
                Modality.PROSE,
                title,
                block,
                line_offsets,
                structure=structure,
                role=Role.UNKNOWN,
                salience=max(0.4, 1.0 - 0.1 * level),
                meta={
                    "heading": True,
                    "heading_level": level,
                    "definition_cue": len(structure) > 1,
                },
            )
            result.units.append(unit)
            section_unit_by_path[structure] = unit.id
            continue

        if block.kind == "code":
            # D-012's exact-code promise means exactly the characters between
            # the opening fence's line ending and the closing fence's first
            # character.  Consequently a newline immediately before the
            # closing fence is retained, while no newline is synthesized at
            # EOF for an unterminated fence.
            assert block.char_span is not None
            body = text[slice(*block.char_span)]
            if not body.strip():
                continue
        else:
            body = "\n".join(block.lines).strip()
            if not body:
                continue
            body = _unwrap(body)

        if block.kind == "code":
            unit = _unit(
                source,
                Modality.CODE,
                body,
                block,
                line_offsets,
                structure=structure,
                role=Role.UNKNOWN,
                salience=0.7,
                meta={"language": block.info or None, "example_cue": True},
            )
        else:
            unit = _unit(
                source,
                Modality.PROSE,
                body,
                block,
                line_offsets,
                structure=structure,
                role=Role.UNKNOWN,
                salience=0.5,
                meta=_prose_cues(body),
            )

        result.units.append(unit)

        if parent := section_unit_by_path.get(structure):
            result.relations.append(
                Relation(
                    src=parent,
                    dst=unit.id,
                    kind=RelationKind.DESCRIBES,
                    evidence="section containment",
                )
            )

        result.units.extend(
            _references(source, body, block, line_offsets, structure)
        )

    if is_markdown and not any(u.meta.get("heading_level") for u in result.units):
        result.gaps.append("no headings: the document carries no explicit structure")
    if not result.units:
        result.gaps.append("empty or unreadable")

    result.meta.update(
        {
            "lines": len(lines),
            "bytes": len(text.encode("utf-8")),
        }
    )
    return result


# --------------------------------------------------------------------------- #
# Block segmentation
# --------------------------------------------------------------------------- #


class _Block:
    __slots__ = (
        "kind",
        "lines",
        "start",
        "end",
        "heading",
        "info",
        "char_span",
    )

    def __init__(
        self,
        kind: str,
        lines: list[str],
        start: int,
        end: int,
        heading: tuple[int, str] | None = None,
        info: str = "",
        char_span: tuple[int, int] | None = None,
    ) -> None:
        self.kind = kind
        self.lines = lines
        self.start = start  # 1-indexed, inclusive
        self.end = end  # 1-indexed, inclusive
        self.heading = heading
        self.info = info
        self.char_span = char_span


def _blocks(
    lines: list[str],
    is_markdown: bool,
    offsets: list[int],
    text_length: int,
):
    """Split into headings, fenced code, and paragraphs.

    Fences are tracked explicitly so that a ``#`` inside a shell snippet is never
    mistaken for a heading, which is the classic way naive markdown splitters
    corrupt a document's structure.
    """
    buf: list[str] = []
    buf_start = 0
    in_fence = False
    fence_marker = ""
    fence_info = ""
    fence_content_start = 0
    fence_content_line = 0

    def flush(end: int):
        nonlocal buf, buf_start
        if buf and any(line.strip() for line in buf):
            yield _Block("paragraph", buf, buf_start, end)
        buf = []

    for idx, line in enumerate(lines, start=1):
        if is_markdown and (m := _FENCE.match(line)):
            marker = m.group(1)
            if not in_fence:
                yield from flush(idx - 1)
                in_fence = True
                fence_marker = marker
                fence_info = m.group(2).strip()
                fence_content_line = idx + 1
                fence_content_start = (
                    offsets[idx + 1] if idx + 1 < len(offsets) else text_length
                )
                buf_start = fence_content_line
                buf = []
            elif marker == fence_marker and not m.group(2).strip():
                yield _Block(
                    "code",
                    buf,
                    fence_content_line,
                    max(fence_content_line, idx - 1),
                    info=fence_info,
                    char_span=(fence_content_start, offsets[idx]),
                )
                in_fence = False
                buf = []
            else:
                buf.append(line)
            continue

        if in_fence:
            if not buf:
                buf_start = idx
            buf.append(line)
            continue

        if is_markdown and (m := _HEADING.match(line)):
            yield from flush(idx - 1)
            yield _Block("heading", [line], idx, idx, heading=(len(m.group(1)), m.group(2)))
            continue

        if not line.strip():
            yield from flush(idx - 1)
            continue

        if not buf:
            buf_start = idx
        buf.append(line)

    if in_fence and buf:
        # An unterminated fence still carries content worth keeping, through
        # the real EOF and without manufacturing a terminal newline.
        yield _Block(
            "code",
            buf,
            fence_content_line,
            len(lines),
            info=fence_info,
            char_span=(fence_content_start, text_length),
        )
    else:
        yield from flush(len(lines))


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|>\s|\|)")


def _unwrap(body: str) -> str:
    """Join lines that a hard wrap split, keep the breaks that mean something.

    A paragraph wrapped at 80 columns is one paragraph, and the wrap points are
    not part of its meaning. Leaving them in breaks every phrase and identifier
    match that fusion depends on: ``power\\nanalysis`` does not match "power
    analysis", and neither does a column name split across a line.

    List items, quotes, and table rows keep their breaks, because there the line
    boundary *is* structure. Code never reaches this function.
    """
    lines = body.split("\n")
    if len(lines) < 2:
        return body

    out: list[str] = [lines[0]]
    for line in lines[1:]:
        prev = out[-1]
        if _LIST_ITEM.match(line) or _LIST_ITEM.match(prev) or not prev.strip():
            out.append(line)
        else:
            out[-1] = f"{prev.rstrip()} {line.lstrip()}"
    return "\n".join(out)


_LINE_ENDINGS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _source_lines(text: str) -> tuple[list[str], list[int]]:
    """Return logical lines and their exact character boundaries.

    ``str.splitlines(keepends=True)`` understands mixed CRLF/LF and the other
    Unicode line boundaries.  ``offsets[n]`` is the start of 1-indexed line
    ``n`` and the final sentinel is the real EOF, even when there is no final
    newline.  This makes every returned span safe to slice without clamping.
    """

    raw_lines = text.splitlines(keepends=True)
    lines: list[str] = []
    offsets = [0, 0]
    pos = 0
    for raw in raw_lines:
        line = raw
        if line.endswith("\r\n"):
            line = line[:-2]
        elif line and line[-1] in _LINE_ENDINGS:
            line = line[:-1]
        lines.append(line)
        pos += len(raw)
        offsets.append(pos)
    return lines, offsets


def _span(offsets: list[int], start: int, end: int) -> tuple[int, int]:
    lo = offsets[start] if start < len(offsets) else offsets[-1]
    hi = offsets[end + 1] if end + 1 < len(offsets) else offsets[-1]
    return (lo, max(lo, hi))


def _ref(start: int, end: int) -> str:
    return f"line:{start}" if start == end else f"line:{start}-{end}"


def _unit(
    source: str,
    modality: Modality,
    content: str,
    block: _Block,
    offsets: list[int],
    *,
    structure: tuple[str, ...],
    role: Role,
    salience: float,
    meta: dict | None = None,
) -> Unit:
    return Unit(
        source=source,
        modality=modality,
        content=content,
        origin=Origin(
            source,
            _ref(block.start, block.end),
            block.char_span or _span(offsets, block.start, block.end),
        ),
        role=role,
        structure=structure,
        salience=salience,
        meta=meta or {},
    )


# --------------------------------------------------------------------------- #
# Roles and references
# --------------------------------------------------------------------------- #


def _prose_cues(body: str) -> dict[str, bool]:
    """Observed rule cues retained without promoting unverified roles."""
    stripped = body.lstrip()
    return {
        "definition_cue": bool(_DEFINITION.match(body)),
        "caveat_cue": bool(_CAVEAT_CUE.search(body)),
        "procedure_cue": stripped[:2] in {"1.", "1)"}
        or stripped.startswith(("- ", "* ")),
    }


def _references(
    source: str,
    body: str,
    block: _Block,
    offsets: list[int],
    structure: tuple[str, ...],
) -> list[Unit]:
    """Outbound references: fusion signal 1, exact and free.

    A markdown link, a bare URL, or a path-shaped token naming another file is
    the strongest and cheapest evidence that two things in a folder belong
    together. These are emitted as units so fusion can resolve them against the
    other files it saw, without re-parsing anything.
    """
    units: list[Unit] = []
    for target, label, kind in reference_specs(body):
        units.append(
            Unit(
                source=source,
                modality=Modality.REFERENCE,
                content=target,
                origin=Origin(
                    source,
                    _ref(block.start, block.end),
                    _span(offsets, block.start, block.end),
                ),
                role=Role.UNKNOWN,
                structure=structure,
                salience=0.3,
                meta={"target": target, "label": label or None, "ref_kind": kind},
            )
        )

    return units


def reference_specs(body: str) -> list[tuple[str, str, str]]:
    """Return conservative outbound-reference facts without assigning origins.

    Native container extractors such as notebooks and LaTeX already own the
    correct cell/line address.  They reuse this lexical contract and attach
    their own origin rather than flattening through the text extractor.
    Numeric decimals are deliberately not path-shaped: the extension must
    begin with an ASCII letter.
    """

    seen: set[str] = set()
    references: list[tuple[str, str, str]] = []
    occupied: list[tuple[int, int]] = []

    def add(target: str, label: str, kind: str) -> None:
        target = target.strip()
        if kind == "url":
            # Sentence punctuation is not part of a bare URL. Markdown link
            # targets arrive without it, so this only normalizes prose URLs.
            target = target.rstrip(".,;:")
        if not target or target.startswith("#") or target in seen:
            return
        seen.add(target)
        references.append((target, label, kind))

    for m in _MD_LINK.finditer(body):
        target = m.group(2)
        occupied.append(m.span(2))
        add(target, m.group(1), "url" if target.startswith(("http://", "https://")) else "path")
    for m in _BARE_URL.finditer(body):
        occupied.append(m.span(1))
        add(m.group(1), "", "url")
    for m in _PATH_REF.finditer(body):
        start, end = m.span(1)
        if any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied):
            continue
        add(m.group(1), "", "path")

    return references
