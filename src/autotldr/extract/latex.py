"""Lightweight native extraction for LaTeX source.

The fast path preserves source truth rather than rendering: section commands,
theorem/equation environments, labels, and citations remain visible with exact
line ranges.  It deliberately does not expand user macros; their presence is
reported as a gap instead of pretending a regex parser saw the rendered paper.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..unit import Extraction, Modality, Origin, Role, Unit

_SECTION = re.compile(r"^\s*\\(part|chapter|section|subsection|subsubsection)\*?\{(.+?)\}\s*$")
_BEGIN = re.compile(r"^\s*\\begin\{([^}]+)\}(?:\[([^]]+)\])?")
_END = re.compile(r"^\s*\\end\{([^}]+)\}")
_LABEL = re.compile(r"\\label\{([^}]+)\}")
_CITE = re.compile(r"\\(?:cite|citep|citet|autocite)\{([^}]+)\}")
_REF = re.compile(r"\\(?:ref|eqref|autoref)\{([^}]+)\}")
_MACRO = re.compile(r"\\(?:newcommand|renewcommand|def)\b")

_LEVEL = {"part": 0, "chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4}
_EQUATIONS = {"equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*"}
_THEOREMS = {"theorem", "lemma", "proposition", "corollary", "definition", "proof"}


def extract(path: Path) -> Extraction:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path.name}: LaTeX input is not strict UTF-8 at byte {exc.start}"
        ) from exc
    source = str(path)
    lines, offsets = _source_lines(text)
    result = Extraction(source=source, kind="latex")
    headings: list[tuple[int, str]] = []
    paragraph: list[str] = []
    paragraph_start = 0
    index = 0

    def flush(end: int) -> None:
        nonlocal paragraph, paragraph_start
        body = " ".join(part.strip() for part in paragraph if part.strip()).strip()
        if body:
            _emit(
                result,
                source,
                body,
                paragraph_start,
                end,
                offsets,
                Modality.PROSE,
                tuple(title for _, title in headings),
                {"latex": True},
            )
        paragraph = []
        paragraph_start = 0

    while index < len(lines):
        line_no = index + 1
        raw = lines[index]
        clean = _strip_comment(raw).strip()

        if match := _SECTION.match(clean):
            flush(line_no - 1)
            command, title = match.groups()
            level = _LEVEL[command]
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            _emit(
                result,
                source,
                title,
                line_no,
                line_no,
                offsets,
                Modality.PROSE,
                tuple(value for _, value in headings),
                {
                    "heading": True,
                    "heading_level": level,
                    "command": command,
                    "definition_cue": True,
                },
                salience=max(0.5, 1.0 - level * 0.08),
            )
            index += 1
            continue

        begin = _BEGIN.match(clean)
        if begin and (begin.group(1) in _EQUATIONS or begin.group(1) in _THEOREMS):
            flush(line_no - 1)
            environment = begin.group(1)
            block = [clean]
            end = line_no
            index += 1
            while index < len(lines):
                block_line = _strip_comment(lines[index]).rstrip()
                block.append(block_line)
                end = index + 1
                index += 1
                if (closing := _END.match(block_line.strip())) and closing.group(1) == environment:
                    break
            body = "\n".join(block).strip()
            labels = _LABEL.findall(body)
            _emit(
                result,
                source,
                body,
                line_no,
                end,
                offsets,
                Modality.EQUATION if environment in _EQUATIONS else Modality.PROSE,
                tuple(value for _, value in headings),
                {
                    "environment": environment,
                    "title": begin.group(2) or None,
                    "labels": labels or None,
                    "theorem": environment in _THEOREMS,
                },
                salience=0.75,
            )
            _references(result, source, body, line_no, end, offsets, tuple(value for _, value in headings))
            continue

        if not clean:
            flush(line_no - 1)
        elif clean.startswith(("\\documentclass", "\\usepackage", "\\begin{document}", "\\end{document}")):
            flush(line_no - 1)
        else:
            if not paragraph:
                paragraph_start = line_no
            paragraph.append(clean)
        index += 1

    flush(len(lines))

    # References in ordinary paragraphs are emitted in a second pass so their
    # exact line scope remains discoverable even when prose was unwrapped.
    for line_no, raw in enumerate(lines, start=1):
        _references(
            result,
            source,
            _strip_comment(raw),
            line_no,
            line_no,
            offsets,
            (),
        )

    if _MACRO.search(text):
        result.gaps.append("custom LaTeX macros were preserved but not expanded")
    if not result.units:
        result.gaps.append("no addressable LaTeX structure or prose found")
    result.meta.update(
        {
            "lines": len(lines),
            "custom_macros": len(_MACRO.findall(text)),
            "labels": sorted(set(_LABEL.findall(text))),
        }
    )
    return result


def _emit(
    result: Extraction,
    source: str,
    content: str,
    start: int,
    end: int,
    offsets: list[int],
    modality: Modality,
    structure: tuple[str, ...],
    meta: dict,
    *,
    salience: float = 0.5,
) -> None:
    result.units.append(
        Unit(
            source=source,
            modality=modality,
            content=content,
            origin=Origin(source, _ref(start, end), _span(offsets, start, end)),
            role=Role.UNKNOWN,
            structure=structure,
            salience=salience,
            meta=meta,
        )
    )


def _references(
    result: Extraction,
    source: str,
    content: str,
    start: int,
    end: int,
    offsets: list[int],
    structure: tuple[str, ...],
) -> None:
    seen: set[tuple[str, str]] = set()
    references: list[tuple[str, str, str]] = []
    for pattern, kind in ((_CITE, "citation"), (_REF, "label")):
        for match in pattern.finditer(content):
            references.extend(
                (part.strip(), "", kind)
                for part in match.group(1).split(",")
            )

    from .text import reference_specs

    references.extend(reference_specs(content))
    for target, label, kind in references:
        key = (kind, target)
        if not target or key in seen:
            continue
        seen.add(key)
        result.units.append(
            Unit(
                source=source,
                modality=Modality.REFERENCE,
                content=target,
                origin=Origin(source, _ref(start, end), _span(offsets, start, end)),
                role=Role.UNKNOWN,
                structure=structure,
                salience=0.3,
                meta={
                    "target": target,
                    "label": label or None,
                    "ref_kind": kind,
                },
            )
        )


def _strip_comment(line: str) -> str:
    escaped = False
    for index, char in enumerate(line):
        if char == "%" and not escaped:
            return line[:index]
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return line


_LINE_ENDINGS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _source_lines(text: str) -> tuple[list[str], list[int]]:
    """Return lines and exact offsets without universal-newline rewriting."""

    lines: list[str] = []
    offsets = [0, 0]
    position = 0
    for raw in text.splitlines(keepends=True):
        line = raw
        if line.endswith("\r\n"):
            line = line[:-2]
        elif line and line[-1] in _LINE_ENDINGS:
            line = line[:-1]
        lines.append(line)
        position += len(raw)
        offsets.append(position)
    return lines, offsets


def _span(offsets: list[int], start: int, end: int) -> tuple[int, int]:
    low = offsets[start] if start < len(offsets) else offsets[-1]
    high = offsets[end + 1] if end + 1 < len(offsets) else offsets[-1]
    return (low, max(low, high))


def _ref(start: int, end: int) -> str:
    return f"line:{start}" if start == end else f"line:{start}-{end}"
