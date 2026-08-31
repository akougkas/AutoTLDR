"""Native, dependency-free reStructuredText extraction.

rST carries structure in adornment styles, indentation, directives, and literal
blocks rather than Markdown markers.  This parser recognizes that conservative
subset directly and keeps every emitted unit tied to exact source lines.  It is
not a renderer and does not attempt to interpret arbitrary roles or execute
directives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

_ADORNMENT_CHARS = set("= -`:'\"~^_*+#<>") - {" "}
_DIRECTIVE = re.compile(
    r"^(?P<indent>\s*)\.\.\s+(?P<name>[A-Za-z][\w-]*)::(?:\s*(?P<argument>.*))?$"
)
_TARGET = re.compile(r"^\s*\.\.\s+_[^:]+:\s*(?P<target>\S.*?)\s*$")
_LIST = re.compile(r"^(?P<indent>\s*)(?P<marker>(?:[-+*]|\d+[.)]|[A-Za-z][.)]))\s+\S")
_LINE_BLOCK = re.compile(r"^\s*\|(?:\s|$)")
_INLINE_LINK = re.compile(r"`[^`<>]*<(?P<target>[^<>\s]+)>`_+")
_BARE_URL = re.compile(r"(?<![(\w])(https?://[^\s<>\"')]+)")
_CODE_DIRECTIVES = {"code", "code-block", "sourcecode"}
_REFERENCE_DIRECTIVES = {"include", "image", "figure", "literalinclude"}


class InvalidRst(ValueError):
    """A recognized rST source whose bytes cannot support exact claims."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.kind = "reStructuredText"
        self.detail = detail
        super().__init__(f"{path.name}: invalid reStructuredText: {detail}")


def extract(path: Path) -> Extraction:
    data = path.read_bytes()
    try:
        # Decode the bytes directly so universal-newline translation cannot
        # invalidate character spans or silently turn CRLF code into LF code.
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InvalidRst(
            path,
            f"input is not strict UTF-8 at byte {exc.start}",
        ) from exc
    return extract_text(text, source=str(path))


def extract_text(text: str, *, source: str) -> Extraction:
    index = _SourceIndex(text)
    lines = [line.rstrip("\r\n") for line in index.lines]
    result = Extraction(source=source, kind="rst")
    result.meta.update(
        {
            "lines": len(lines),
            "bytes": len(text.encode("utf-8")),
            "parser": "native-rst",
        }
    )

    headings, claimed_heading_lines = _headings(lines)
    style_levels: dict[tuple[str, str], int] = {}
    stack: list[tuple[int, str, str]] = []  # level, title, unit id
    seen_references: set[str] = set()
    i = 0

    def current_structure() -> tuple[str, ...]:
        return tuple(title for _, title, _ in stack)

    def append(
        unit: Unit,
        *,
        parent: str | None = None,
        evidence: str = "section containment",
    ) -> None:
        result.units.append(unit)
        parent_id = parent or (stack[-1][2] if stack else None)
        if parent_id and parent_id != unit.id:
            result.relations.append(
                Relation(
                    src=parent_id,
                    dst=unit.id,
                    kind=RelationKind.DESCRIBES,
                    evidence=evidence,
                )
            )

    while i < len(lines):
        if i in headings:
            heading = headings[i]
            level = style_levels.setdefault(heading.style, len(style_levels) + 1)
            while stack and stack[-1][0] >= level:
                stack.pop()
            structure = tuple((*current_structure(), heading.title))
            span = index.line_span(heading.start + 1, heading.end + 1)
            unit = Unit(
                source=source,
                modality=Modality.PROSE,
                content=heading.title,
                origin=_origin(source, heading.start + 1, heading.end + 1, span),
                role=Role.UNKNOWN,
                structure=structure,
                salience=max(0.45, 1.0 - level * 0.1),
                meta={
                    "heading": True,
                    "heading_level": level,
                    "adornment": heading.style[1],
                    "adornment_form": heading.style[0],
                    "definition_cue": level > 1,
                },
            )
            parent_id = stack[-1][2] if stack else None
            result.units.append(unit)
            if parent_id:
                result.relations.append(
                    Relation(
                        src=parent_id,
                        dst=unit.id,
                        kind=RelationKind.DESCRIBES,
                        evidence="rST section hierarchy",
                    )
                )
            stack.append((level, heading.title, unit.id))
            i = heading.end + 1
            continue

        if i in claimed_heading_lines:
            i += 1
            continue
        if not lines[i].strip():
            i += 1
            continue

        directive_match = _DIRECTIVE.match(lines[i])
        if directive_match:
            start = i
            end = _indented_block_end(lines, i + 1, len(directive_match.group("indent")))
            last = _last_nonblank(lines, start, end)
            name = directive_match.group("name").casefold()
            argument = (directive_match.group("argument") or "").strip()
            content = "\n".join(lines[start : last + 1]).strip()
            directive = Unit(
                source=source,
                modality=Modality.RECORD,
                content=content,
                origin=_origin(
                    source,
                    start + 1,
                    last + 1,
                    index.line_span(start + 1, last + 1),
                ),
                role=Role.UNKNOWN,
                structure=current_structure(),
                salience=0.6,
                meta={
                    "directive": name,
                    "argument": argument or None,
                },
            )
            append(directive)

            if name in _REFERENCE_DIRECTIVES and argument:
                _append_reference(
                    result,
                    source,
                    argument,
                    current_structure(),
                    _origin(
                        source,
                        start + 1,
                        start + 1,
                        index.line_span(start + 1, start + 1),
                    ),
                    seen_references,
                    ref_kind=name,
                )

            if name in _CODE_DIRECTIVES:
                body = _directive_code_range(lines, start + 1, end)
                if body is None:
                    result.add_gap(
                        f"{name} directive at line {start + 1} has no indented code body",
                        origin=_origin(
                            source,
                            start + 1,
                            start + 1,
                            index.line_span(start + 1, start + 1),
                        ),
                    )
                else:
                    body_start, body_end = body
                    code_span = index.line_span(body_start + 1, body_end + 1)
                    # Structural indentation and newline bytes are part of the
                    # claim.  The span and content must round-trip exactly.
                    code = text[code_span[0] : code_span[1]]
                    code_unit = Unit(
                        source=source,
                        modality=Modality.CODE,
                        content=code,
                        origin=_origin(
                            source,
                            body_start + 1,
                            body_end + 1,
                            code_span,
                        ),
                        role=Role.UNKNOWN,
                        structure=current_structure(),
                        salience=0.7,
                        meta={
                            "literal_block": True,
                            "directive": name,
                            "language": argument or None,
                        },
                    )
                    append(
                        code_unit,
                        parent=directive.id,
                        evidence="rST code directive body",
                    )
            else:
                _append_inline_references(
                    result,
                    source,
                    content,
                    current_structure(),
                    directive.origin,
                    seen_references,
                )
            i = max(end, start + 1)
            continue

        target_match = _TARGET.match(lines[i])
        if target_match:
            target = target_match.group("target")
            _append_reference(
                result,
                source,
                target,
                current_structure(),
                _origin(source, i + 1, i + 1, index.line_span(i + 1, i + 1)),
                seen_references,
                ref_kind="explicit-target",
            )
            i += 1
            continue

        if lines[i].lstrip().startswith(".."):
            # A comment is not source meaning.  Skip its indented continuation
            # rather than presenting internal author notes as document prose.
            i = max(_indented_block_end(lines, i + 1, _indent(lines[i])), i + 1)
            continue

        list_match = _LIST.match(lines[i])
        if list_match or _LINE_BLOCK.match(lines[i]):
            start = i
            end = _list_end(lines, i, len((list_match.group("indent") if list_match else "")))
            last = _last_nonblank(lines, start, end)
            content = "\n".join(lines[start : last + 1]).strip()
            origin = _origin(
                source,
                start + 1,
                last + 1,
                index.line_span(start + 1, last + 1),
            )
            unit = Unit(
                source=source,
                modality=Modality.PROSE,
                content=content,
                origin=origin,
                role=Role.UNKNOWN,
                structure=current_structure(),
                salience=0.55,
                meta={
                    "list": True,
                    "list_kind": "line-block" if not list_match else list_match.group("marker"),
                    "procedure_cue": bool(list_match),
                },
            )
            append(unit)
            _append_inline_references(
                result,
                source,
                content,
                current_structure(),
                origin,
                seen_references,
            )
            i = end
            continue

        if _adornment(lines[i]) is not None:
            result.add_gap(
                f"orphan section adornment at line {i + 1}",
                origin=_origin(
                    source,
                    i + 1,
                    i + 1,
                    index.line_span(i + 1, i + 1),
                ),
            )
            i += 1
            continue

        start = i
        i += 1
        while i < len(lines):
            if not lines[i].strip() or i in headings or i in claimed_heading_lines:
                break
            if _DIRECTIVE.match(lines[i]) or _TARGET.match(lines[i]):
                break
            if lines[i].lstrip().startswith(".."):
                break
            if _LIST.match(lines[i]) or _LINE_BLOCK.match(lines[i]):
                break
            if _adornment(lines[i]) is not None:
                break
            i += 1
        end = i
        paragraph_lines = lines[start:end]
        content = " ".join(line.strip() for line in paragraph_lines).strip()
        if not content:
            continue
        origin = _origin(
            source,
            start + 1,
            end,
            index.line_span(start + 1, end),
        )
        paragraph = Unit(
            source=source,
            modality=Modality.PROSE,
            content=content,
            origin=origin,
            role=Role.UNKNOWN,
            structure=current_structure(),
            salience=0.5,
            meta={"literal_introducer": content.rstrip().endswith("::")},
        )
        append(paragraph)
        _append_inline_references(
            result,
            source,
            content,
            current_structure(),
            origin,
            seen_references,
        )

        if content.rstrip().endswith("::"):
            body = _following_literal_range(lines, end, _indent(lines[start]))
            if body is not None:
                body_start, body_end = body
                code_span = index.line_span(body_start + 1, body_end + 1)
                code = text[code_span[0] : code_span[1]]
                literal = Unit(
                    source=source,
                    modality=Modality.CODE,
                    content=code,
                    origin=_origin(
                        source,
                        body_start + 1,
                        body_end + 1,
                        code_span,
                    ),
                    role=Role.UNKNOWN,
                    structure=current_structure(),
                    salience=0.65,
                    meta={"literal_block": True, "language": None},
                )
                append(
                    literal,
                    parent=paragraph.id,
                    evidence="rST literal introducer",
                )
                i = body_end + 1

    if not result.units:
        result.add_gap("empty or no addressable rST content")
    result.meta["headings"] = sum(
        1 for unit in result.units if unit.meta.get("heading") is True
    )
    return result


@dataclass(frozen=True, slots=True)
class _Heading:
    start: int
    end: int
    title: str
    style: tuple[str, str]


def _headings(lines: list[str]) -> tuple[dict[int, _Heading], set[int]]:
    headings: dict[int, _Heading] = {}
    claimed: set[int] = set()
    i = 0
    while i < len(lines):
        over = _adornment(lines[i])
        if (
            over is not None
            and i + 2 < len(lines)
            and lines[i + 1].strip()
            and not lines[i + 1].startswith((" ", "\t"))
            and _adornment(lines[i + 2]) == over
            and len(lines[i].strip()) >= len(lines[i + 1].strip())
        ):
            heading = _Heading(i, i + 2, lines[i + 1].strip(), ("overline", over))
            headings[i] = heading
            claimed.update(range(i, i + 3))
            i += 3
            continue

        if (
            lines[i].strip()
            and not lines[i].startswith((" ", "\t"))
            and i + 1 < len(lines)
            and (under := _adornment(lines[i + 1])) is not None
            and len(lines[i + 1].strip()) >= len(lines[i].strip())
        ):
            heading = _Heading(i, i + 1, lines[i].strip(), ("underline", under))
            headings[i] = heading
            claimed.update((i, i + 1))
            i += 2
            continue
        i += 1
    return headings, claimed


def _adornment(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) < 3 or stripped[0] not in _ADORNMENT_CHARS:
        return None
    return stripped[0] if len(set(stripped)) == 1 else None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _indented_block_end(lines: list[str], start: int, parent_indent: int) -> int:
    i = start
    last_content = start
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if _indent(lines[i]) <= parent_indent:
            break
        last_content = i + 1
        i += 1
    return max(i, last_content)


def _last_nonblank(lines: list[str], start: int, end: int) -> int:
    end = min(end, len(lines))
    for i in range(end - 1, start - 1, -1):
        if lines[i].strip():
            return i
    return start


def _directive_code_range(
    lines: list[str], start: int, end: int
) -> tuple[int, int] | None:
    i = start
    while i < end:
        stripped = lines[i].strip()
        if not stripped or stripped.startswith(":"):
            i += 1
            continue
        break
    if i >= end or not lines[i].strip():
        return None
    last = _last_nonblank(lines, i, end)
    return i, last


def _following_literal_range(
    lines: list[str], start: int, parent_indent: int
) -> tuple[int, int] | None:
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or _indent(lines[i]) <= parent_indent:
        return None
    end = i + 1
    while end < len(lines):
        if lines[end].strip() and _indent(lines[end]) <= parent_indent:
            break
        end += 1
    return i, _last_nonblank(lines, i, end)


def _list_end(lines: list[str], start: int, base_indent: int) -> int:
    i = start + 1
    while i < len(lines):
        if not lines[i].strip():
            lookahead = i + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines) and (
                _LIST.match(lines[lookahead]) or _indent(lines[lookahead]) > base_indent
            ):
                i = lookahead
                continue
            break
        marker = _LIST.match(lines[i])
        if marker and len(marker.group("indent")) == base_indent:
            i += 1
            continue
        if _indent(lines[i]) > base_indent:
            i += 1
            continue
        break
    return i


class _SourceIndex:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.starts: list[int] = []
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line)
        self.end = len(text)

    def line_span(self, start: int, end: int) -> tuple[int, int]:
        if not self.starts:
            return 0, 0
        start = min(max(start, 1), len(self.starts))
        end = min(max(end, start), len(self.starts))
        lo = self.starts[start - 1]
        hi = self.starts[end] if end < len(self.starts) else self.end
        return lo, max(lo, hi)


def _ref(start: int, end: int) -> str:
    return f"line:{start}" if start == end else f"line:{start}-{end}"


def _origin(
    source: str,
    start: int,
    end: int,
    span: tuple[int, int],
) -> Origin:
    return Origin(source, _ref(start, end), span)


def _append_reference(
    result: Extraction,
    source: str,
    target: str,
    structure: tuple[str, ...],
    origin: Origin,
    seen: set[str],
    *,
    ref_kind: str,
) -> None:
    target = target.strip()
    if not target or target.startswith("#") or target in seen:
        return
    seen.add(target)
    result.units.append(
        Unit(
            source=source,
            modality=Modality.REFERENCE,
            content=target,
            origin=origin,
            role=Role.UNKNOWN,
            structure=structure,
            salience=0.3,
            meta={"target": target, "ref_kind": ref_kind},
        )
    )


def _append_inline_references(
    result: Extraction,
    source: str,
    content: str,
    structure: tuple[str, ...],
    origin: Origin,
    seen: set[str],
) -> None:
    for match in _INLINE_LINK.finditer(content):
        target = match.group("target")
        _append_reference(
            result,
            source,
            target,
            structure,
            origin,
            seen,
            ref_kind="url" if target.startswith(("http://", "https://")) else "path",
        )
    for match in _BARE_URL.finditer(content):
        _append_reference(
            result,
            source,
            match.group(1),
            structure,
            origin,
            seen,
            ref_kind="url",
        )
