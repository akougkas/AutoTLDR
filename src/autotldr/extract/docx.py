"""Native DOCX extraction through the OOXML package.

DOCX is a ZIP container whose semantic structure already lives in
``word/document.xml``.  Reading that structure directly preserves headings,
paragraphs, tables, comments, and tracked revisions without importing
``python-docx`` or flattening the document through a converter.

The extractor intentionally uses only the standard library.  Origins use the
document's own address space: ``para:N``, ``table:N#row:M``, and ``comment:N``.
"""

from __future__ import annotations

import re
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

_DOCUMENT_XML = "word/document.xml"
_COMMENTS_XML = "word/comments.xml"
_MAX_XML_BYTES = 64 * 1024 * 1024
_MAX_PACKAGE_MEMBERS = 10_000
_MAX_PACKAGE_UNCOMPRESSED = 256 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_MAX_XML_ELEMENTS = 1_000_000
_MAX_XML_DEPTH = 128
_HEADING_STYLE = re.compile(r"^heading[ _-]*([1-9])$", re.IGNORECASE)
_INSERTIONS = frozenset({"ins", "moveTo"})
_DELETIONS = frozenset({"del", "moveFrom"})


class InvalidDocx(ValueError):
    """A recognized DOCX path whose package is missing or malformed."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.kind = "DOCX"
        self.detail = detail
        super().__init__(f"{path.name}: invalid DOCX: {detail}")


def extract(path: Path) -> Extraction:
    """Extract one DOCX package without converting it to an intermediate form."""

    document_data: bytes
    comments_data: bytes | None
    try:
        with zipfile.ZipFile(path) as package:
            _validate_package(path, package)
            document_data = _read_member(package, _DOCUMENT_XML, required=True)
            comments_data = _read_member(package, _COMMENTS_XML, required=False)
    except InvalidDocx:
        raise
    except _MissingMember as exc:
        raise InvalidDocx(path, f"OOXML package is missing required member {exc.name}") from exc
    except _OversizedMember as exc:
        raise InvalidDocx(
            path,
            f"{exc.name} is {exc.size} bytes; XML member limit is {_MAX_XML_BYTES} bytes",
        ) from exc
    except zipfile.BadZipFile as exc:
        raise InvalidDocx(path, "the file is not a valid ZIP/OOXML package") from exc
    except (OSError, RuntimeError) as exc:
        raise InvalidDocx(path, str(exc)) from exc

    document = _parse_xml(path, _DOCUMENT_XML, document_data)
    if _local(document.tag) != "document":
        raise InvalidDocx(path, f"{_DOCUMENT_XML} has root {_local(document.tag)!r}, expected 'document'")
    body = next((child for child in document if _local(child.tag) == "body"), None)
    if body is None:
        raise InvalidDocx(path, f"{_DOCUMENT_XML} has no document body")

    result = Extraction(source=str(path), kind="docx")
    state = _DocumentState(result)
    state.alt_chunks = sum(1 for element in body.iter() if _local(element.tag) == "altChunk")

    for block in _body_blocks(body):
        kind = _local(block.tag)
        if kind == "p":
            state.add_paragraph(block)
        elif kind == "tbl":
            state.add_table(block)

    if comments_data is not None:
        comments = _parse_xml(path, _COMMENTS_XML, comments_data)
        if _local(comments.tag) != "comments":
            raise InvalidDocx(path, f"{_COMMENTS_XML} has root {_local(comments.tag)!r}, expected 'comments'")
        state.add_comments(comments)
    elif state.comment_anchors:
        result.gaps.append(
            "comment anchors are present, but word/comments.xml is missing from the DOCX package"
        )

    if state.revisions:
        result.gaps.append(
            f"{state.revisions} tracked revision(s) are present; insertions are included "
            "in current paragraph text and deletions are preserved only in unit metadata"
        )
    if state.alt_chunks:
        result.gaps.append(
            f"{state.alt_chunks} externally linked content block(s) were not embedded in "
            "word/document.xml and could not be extracted"
        )
    if not result.units:
        result.gaps.append("empty document: no addressable paragraphs, table rows, or comments")

    result.meta.update(
        {
            "paragraphs": state.paragraphs,
            "tables": state.tables,
            "table_rows": state.table_rows,
            "comments": state.comments,
            "tracked_revisions": state.revisions,
            "external_content_blocks": state.alt_chunks,
        }
    )
    return result


def _read_member(
    package: zipfile.ZipFile,
    name: str,
    *,
    required: bool,
) -> bytes | None:
    try:
        info = package.getinfo(name)
    except KeyError:
        if required:
            # The caller supplies the path in the final typed error.  Raising a
            # private sentinel here keeps all ZIP handling in one try block.
            raise _MissingMember(name)
        return None

    if info.file_size > _MAX_XML_BYTES:
        raise _OversizedMember(name, info.file_size)
    with package.open(info) as stream:
        data = stream.read(_MAX_XML_BYTES + 1)
    if len(data) > _MAX_XML_BYTES:
        raise _OversizedMember(name, len(data))
    return data


def _validate_package(path: Path, package: zipfile.ZipFile) -> None:
    infos = package.infolist()
    if len(infos) > _MAX_PACKAGE_MEMBERS:
        raise InvalidDocx(
            path,
            f"OOXML package has {len(infos)} members; limit is "
            f"{_MAX_PACKAGE_MEMBERS}",
        )
    names: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        normalized = posixpath.normpath(name)
        if (
            not name
            or "\x00" in name
            or "\\" in name
            or name.startswith("/")
            or normalized in {".", ".."}
            or normalized.startswith("../")
        ):
            raise InvalidDocx(path, f"OOXML package has unsafe member path {name!r}")
        if normalized in names:
            raise InvalidDocx(
                path, f"OOXML package has duplicate member {normalized!r}"
            )
        names.add(normalized)
        if info.flag_bits & 0x1:
            raise InvalidDocx(path, f"OOXML member {name!r} is encrypted")
        total += info.file_size
        if total > _MAX_PACKAGE_UNCOMPRESSED:
            raise InvalidDocx(
                path,
                "OOXML package declared uncompressed size exceeds "
                f"{_MAX_PACKAGE_UNCOMPRESSED} bytes",
            )
        if (
            info.file_size > 1024 * 1024
            and info.file_size
            > max(1, info.compress_size) * _MAX_COMPRESSION_RATIO
        ):
            raise InvalidDocx(
                path,
                f"OOXML member {name!r} exceeds the "
                f"{_MAX_COMPRESSION_RATIO}:1 compression-ratio limit",
            )


class _MissingMember(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


class _OversizedMember(Exception):
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size
        super().__init__(name)


def _parse_xml(path: Path, member: str, data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (None, None))
        where = f" at line {line}, column {column}" if line is not None else ""
        raise InvalidDocx(path, f"malformed {member}{where}: {exc}") from exc
    _validate_xml_tree(path, member, root)
    return root


def _validate_xml_tree(path: Path, member: str, root: ET.Element) -> None:
    count = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > _MAX_XML_ELEMENTS:
            raise InvalidDocx(
                path,
                f"{member} exceeds {_MAX_XML_ELEMENTS} XML elements",
            )
        if depth > _MAX_XML_DEPTH:
            raise InvalidDocx(
                path,
                f"{member} nesting exceeds {_MAX_XML_DEPTH} XML levels",
            )
        stack.extend((child, depth + 1) for child in reversed(element))


@dataclass(slots=True)
class _DocumentState:
    result: Extraction
    paragraphs: int = 0
    tables: int = 0
    table_rows: int = 0
    comments: int = 0
    revisions: int = 0
    alt_chunks: int = 0
    headings: list[tuple[int, str, str]] = field(default_factory=list)
    comment_anchors: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )

    @property
    def source(self) -> str:
        return self.result.source

    @property
    def structure(self) -> tuple[str, ...]:
        return tuple(title for _, title, _ in self.headings)

    @property
    def section_unit(self) -> str | None:
        return self.headings[-1][2] if self.headings else None

    def add_paragraph(self, paragraph: ET.Element) -> None:
        self.paragraphs += 1
        content = _current_text(paragraph)
        insertions = _revision_text(paragraph, _INSERTIONS)
        deletions = _revision_text(paragraph, _DELETIONS)
        self.revisions += len(insertions) + len(deletions)
        comment_ids = _comment_ids(paragraph)

        if not content:
            # Keep native numbering stable even when an empty paragraph is not
            # useful enough to become a semantic unit.
            return

        style, heading_level = _paragraph_style(paragraph)
        ref = f"para:{self.paragraphs}"
        meta = {
            "paragraph": self.paragraphs,
            "style": style or None,
            "heading": heading_level is not None,
            "heading_level": heading_level,
            "tracked_insertions": insertions or None,
            "tracked_deletions": deletions or None,
            "comment_ids": sorted(comment_ids, key=_natural_id) or None,
        }

        if heading_level is not None:
            while self.headings and self.headings[-1][0] >= heading_level:
                self.headings.pop()
            parent = self.section_unit
            structure = self.structure + (content,)
            unit = Unit(
                source=self.source,
                modality=Modality.PROSE,
                content=content,
                origin=Origin(self.source, ref),
                role=Role.UNKNOWN,
                structure=structure,
                salience=max(0.45, 1.0 - heading_level * 0.1),
                meta=meta,
            )
            self.result.units.append(unit)
            if parent:
                self._section_relation(parent, unit.id)
            self.headings.append((heading_level, content, unit.id))
        else:
            unit = Unit(
                source=self.source,
                modality=Modality.PROSE,
                content=content,
                origin=Origin(self.source, ref),
                role=Role.UNKNOWN,
                structure=self.structure,
                salience=0.5,
                meta=meta,
            )
            self.result.units.append(unit)
            if self.section_unit:
                self._section_relation(self.section_unit, unit.id)

        self._anchor_comments(comment_ids, unit.id)

    def add_table(self, table: ET.Element) -> None:
        self.tables += 1
        rows = [child for child in table if _local(child.tag) == "tr"]
        for row_number, row in enumerate(rows, start=1):
            self.table_rows += 1
            cells: list[str] = []
            comment_ids: set[str] = set()
            insertions: list[str] = []
            deletions: list[str] = []
            for cell in (child for child in row if _local(child.tag) == "tc"):
                paragraphs = [element for element in cell.iter() if _local(element.tag) == "p"]
                cell_parts = [_current_text(paragraph) for paragraph in paragraphs]
                cells.append("\n".join(part for part in cell_parts if part))
                for paragraph in paragraphs:
                    comment_ids.update(_comment_ids(paragraph))
                    insertions.extend(_revision_text(paragraph, _INSERTIONS))
                    deletions.extend(_revision_text(paragraph, _DELETIONS))

            self.revisions += len(insertions) + len(deletions)
            if not any(cells):
                continue

            ref = f"table:{self.tables}#row:{row_number}"
            unit = Unit(
                source=self.source,
                modality=Modality.RECORD,
                content=" | ".join(cells),
                origin=Origin(self.source, ref),
                role=Role.UNKNOWN,
                structure=self.structure + (f"Table {self.tables}",),
                salience=0.6 if row_number == 1 else 0.5,
                meta={
                    "table": self.tables,
                    "row": row_number,
                    "cells": cells,
                    "columns": len(cells),
                    "header_candidate": row_number == 1,
                    "tracked_insertions": insertions or None,
                    "tracked_deletions": deletions or None,
                    "comment_ids": sorted(comment_ids, key=_natural_id) or None,
                },
            )
            self.result.units.append(unit)
            if self.section_unit:
                self._section_relation(self.section_unit, unit.id)
            self._anchor_comments(comment_ids, unit.id)

    def add_comments(self, comments: ET.Element) -> None:
        seen: set[str] = set()
        for ordinal, comment in enumerate(
            (element for element in comments if _local(element.tag) == "comment"),
            start=1,
        ):
            comment_id = _attr(comment, "id") or str(ordinal)
            if comment_id in seen:
                raise InvalidDocx(Path(self.source), f"duplicate comment id {comment_id!r}")
            seen.add(comment_id)
            content = "\n".join(
                text
                for paragraph in comment.iter()
                if _local(paragraph.tag) == "p" and (text := _current_text(paragraph))
            )
            if not content:
                self.result.gaps.append(f"comment {comment_id} is empty")
                continue

            anchors = self.comment_anchors.get(comment_id, [])
            by_id = {unit.id: unit for unit in self.result.units}
            anchor_unit = by_id.get(anchors[0]) if anchors else None
            unit = Unit(
                source=self.source,
                modality=Modality.PROSE,
                content=content,
                origin=Origin(self.source, f"comment:{comment_id}"),
                role=Role.UNKNOWN,
                structure=anchor_unit.structure if anchor_unit else ("Comments",),
                salience=0.55,
                meta={
                    "comment": comment_id,
                    "author": _attr(comment, "author") or None,
                    "date": _attr(comment, "date") or None,
                    "initials": _attr(comment, "initials") or None,
                    "anchors": anchors or None,
                },
            )
            self.result.units.append(unit)
            self.comments += 1

            if not anchors:
                self.result.gaps.append(
                    f"comment {comment_id} has no addressable anchor in the document body"
                )
            for target in anchors:
                self.result.relations.append(
                    Relation(
                        src=unit.id,
                        dst=target,
                        kind=RelationKind.DESCRIBES,
                        evidence=f"OOXML comment {comment_id} range",
                    )
                )

    def _anchor_comments(self, comment_ids: set[str], unit_id: str) -> None:
        for comment_id in comment_ids:
            if unit_id not in self.comment_anchors[comment_id]:
                self.comment_anchors[comment_id].append(unit_id)

    def _section_relation(self, section: str, child: str) -> None:
        self.result.relations.append(
            Relation(
                src=section,
                dst=child,
                kind=RelationKind.DESCRIBES,
                evidence="DOCX heading containment",
            )
        )


def _body_blocks(body: ET.Element):
    """Yield paragraphs and tables in document order without entering tables."""

    for child in body:
        kind = _local(child.tag)
        if kind in {"p", "tbl"}:
            yield child
        elif kind not in {"sectPr", "altChunk"}:
            yield from _body_blocks(child)


def _paragraph_style(paragraph: ET.Element) -> tuple[str, int | None]:
    properties = next(
        (child for child in paragraph if _local(child.tag) == "pPr"),
        None,
    )
    if properties is None:
        return "", None

    style = ""
    outline: int | None = None
    for element in properties:
        kind = _local(element.tag)
        if kind == "pStyle":
            style = _attr(element, "val")
        elif kind == "outlineLvl":
            try:
                outline = int(_attr(element, "val")) + 1
            except (TypeError, ValueError):
                pass

    match = _HEADING_STYLE.match(style.strip())
    if match:
        return style, int(match.group(1))
    if outline is not None and 1 <= outline <= 9:
        return style, outline
    return style, None


def _current_text(element: ET.Element) -> str:
    parts: list[str] = []

    def visit(node: ET.Element, deleted: bool = False) -> None:
        kind = _local(node.tag)
        deleted = deleted or kind in _DELETIONS
        if kind in {"t", "delText"}:
            if not deleted and node.text:
                parts.append(node.text)
            return
        if not deleted and kind == "tab":
            parts.append("\t")
        elif not deleted and kind in {"br", "cr"}:
            parts.append("\n")
        elif not deleted and kind == "noBreakHyphen":
            parts.append("-")
        elif not deleted and kind == "softHyphen":
            parts.append("\u00ad")
        for child in node:
            visit(child, deleted)

    visit(element)
    return _clean_text("".join(parts))


def _revision_text(element: ET.Element, kinds: frozenset[str]) -> list[str]:
    fragments: list[str] = []
    for revision in element.iter():
        if _local(revision.tag) not in kinds:
            continue
        parts: list[str] = []
        for child in revision.iter():
            kind = _local(child.tag)
            if kind in {"t", "delText"} and child.text:
                parts.append(child.text)
            elif kind == "tab":
                parts.append("\t")
            elif kind in {"br", "cr"}:
                parts.append("\n")
        if text := _clean_text("".join(parts)):
            fragments.append(text)
    return fragments


def _comment_ids(element: ET.Element) -> set[str]:
    return {
        comment_id
        for child in element.iter()
        if _local(child.tag) in {"commentRangeStart", "commentReference"}
        and (comment_id := _attr(child, "id"))
    }


def _clean_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _attr(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _natural_id(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)
