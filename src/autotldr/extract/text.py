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
_PATH_REF = re.compile(r"(?<![\w/])((?:\.{1,2}/)?[\w.\-]+(?:/[\w.\-]+)*\.[A-Za-z0-9]{1,6})(?![\w/])")

_DEFINITION = re.compile(r"^\s*(?:[-*]\s+)?\*\*([^*]+)\*\*\s*[:—-]\s+\S")
_CAVEAT_CUE = re.compile(
    r"\b(?:note that|caveat|caution|warning|however|but only if|be aware|"
    r"do not|don't|never|must not|limitation)\b",
    re.IGNORECASE,
)


def extract(path: Path) -> Extraction:
    text = path.read_text(encoding="utf-8", errors="replace")
    source = str(path)
    is_markdown = path.suffix.lower() in {".md", ".markdown"}

    lines = text.splitlines()
    line_offsets = _line_offsets(text, lines)

    result = Extraction(source=source, kind="markdown" if is_markdown else "text")
    heading_stack: list[tuple[int, str]] = []
    section_unit_by_path: dict[tuple[str, ...], str] = {}

    for block in _blocks(lines, is_markdown):
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
                role=Role.DEFINITION if len(structure) > 1 else Role.UNKNOWN,
                salience=max(0.4, 1.0 - 0.1 * level),
                meta={"heading_level": level},
            )
            result.units.append(unit)
            section_unit_by_path[structure] = unit.id
            continue

        body = "\n".join(block.lines).strip()
        if not body:
            continue

        if block.kind != "code":
            body = _unwrap(body)

        if block.kind == "code":
            unit = _unit(
                source,
                Modality.CODE,
                body,
                block,
                line_offsets,
                structure=structure,
                role=Role.EXAMPLE,
                salience=0.7,
                meta={"language": block.info or None},
            )
        else:
            unit = _unit(
                source,
                Modality.PROSE,
                body,
                block,
                line_offsets,
                structure=structure,
                role=_prose_role(body),
                salience=0.5,
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
    __slots__ = ("kind", "lines", "start", "end", "heading", "info")

    def __init__(
        self,
        kind: str,
        lines: list[str],
        start: int,
        end: int,
        heading: tuple[int, str] | None = None,
        info: str = "",
    ) -> None:
        self.kind = kind
        self.lines = lines
        self.start = start  # 1-indexed, inclusive
        self.end = end  # 1-indexed, inclusive
        self.heading = heading
        self.info = info


def _blocks(lines: list[str], is_markdown: bool):
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
                buf_start = idx
                buf = []
            elif marker == fence_marker:
                yield _Block("code", buf, buf_start, idx, info=fence_info)
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
        # An unterminated fence still carries content worth keeping.
        yield _Block("code", buf, buf_start, len(lines), info=fence_info)
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


def _line_offsets(text: str, lines: list[str]) -> list[int]:
    """Character offset of the start of each 1-indexed line.

    Index 0 is unused so that ``offsets[n]`` is line ``n``, which keeps the
    char-span arithmetic readable at the call sites.
    """
    offsets = [0, 0]
    pos = 0
    newline = 1 if "\r\n" not in text else 2
    for line in lines:
        pos += len(line) + newline
        offsets.append(pos)
    return offsets


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
        origin=Origin(source, _ref(block.start, block.end), _span(offsets, block.start, block.end)),
        role=role,
        structure=structure,
        salience=salience,
        meta=meta or {},
    )


# --------------------------------------------------------------------------- #
# Roles and references
# --------------------------------------------------------------------------- #


def _prose_role(body: str) -> Role:
    """Rules-only role assignment.

    Stage 2's eval decides whether this is enough, whether a small model beats
    it, and how much of the taxonomy is reliably recoverable at all. Until then
    the honest default is UNKNOWN, and only high-precision cues promote a unit
    out of it.
    """
    if _DEFINITION.match(body):
        return Role.DEFINITION
    if _CAVEAT_CUE.search(body):
        return Role.CAVEAT
    stripped = body.lstrip()
    if stripped[:2] in {"1.", "1)"} or stripped.startswith(("- ", "* ")):
        return Role.PROCEDURE
    return Role.UNKNOWN


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
    seen: set[str] = set()
    units: list[Unit] = []

    def add(target: str, label: str, kind: str) -> None:
        target = target.strip()
        if not target or target.startswith("#") or target in seen:
            return
        seen.add(target)
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

    for m in _MD_LINK.finditer(body):
        target = m.group(2)
        add(target, m.group(1), "url" if target.startswith(("http://", "https://")) else "path")
    for m in _BARE_URL.finditer(body):
        add(m.group(1), "", "url")
    for m in _PATH_REF.finditer(body):
        add(m.group(1), "", "path")

    return units
