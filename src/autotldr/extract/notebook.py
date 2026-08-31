"""Native Jupyter notebook extraction.

Notebook semantics live in the pairing between a cell and its outputs.  This
extractor keeps that pairing explicit: cells and textual outputs are separate,
addressable units connected by ``PRODUCED_BY`` relations.  Binary image output
is never represented as if it had been read; image-only outputs become gaps.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..unit import Extraction, Modality, Origin, Relation, RelationKind, Role, Unit

_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_TEXT_MIME_PRIORITY = (
    "text/markdown",
    "text/plain",
    "text/html",
    "application/json",
)


class InvalidNotebook(ValueError):
    """A recognized IPYNB path whose JSON or notebook shape is invalid."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.kind = "Jupyter notebook"
        self.detail = detail
        super().__init__(f"{path.name}: invalid Jupyter notebook: {detail}")


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class _InvalidConstant(ValueError):
    pass


def extract(path: Path) -> Extraction:
    """Parse one ``.ipynb`` file and preserve native cell/output addresses."""

    notebook = _load(path)
    cells = notebook.get("cells")
    metadata = notebook.get("metadata", {})
    nbformat = notebook.get("nbformat")
    nbformat_minor = notebook.get("nbformat_minor", 0)

    if not isinstance(cells, list):
        raise InvalidNotebook(path, "top-level 'cells' must be an array")
    if not isinstance(metadata, dict):
        raise InvalidNotebook(path, "top-level 'metadata' must be an object")
    if not isinstance(nbformat, int) or isinstance(nbformat, bool):
        raise InvalidNotebook(path, "top-level 'nbformat' must be an integer")
    if not isinstance(nbformat_minor, int) or isinstance(nbformat_minor, bool):
        raise InvalidNotebook(path, "top-level 'nbformat_minor' must be an integer")

    result = Extraction(source=str(path), kind="notebook")
    state = _NotebookState(result, _language(metadata))

    for number, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            raise InvalidNotebook(path, f"cell {number} must be an object")
        state.add_cell(number, cell)

    if state.empty_cells:
        result.add_gap(
            f"{state.empty_cells} empty cell(s) carry no source content"
        )
    if not result.units:
        result.add_gap("empty notebook: no addressable cell or textual output content")

    kernelspec = metadata.get("kernelspec", {})
    language_info = metadata.get("language_info", {})
    result.meta.update(
        {
            "nbformat": nbformat,
            "nbformat_minor": nbformat_minor,
            "cells": len(cells),
            "code_cells": state.code_cells,
            "markdown_cells": state.markdown_cells,
            "raw_cells": state.raw_cells,
            "empty_cells": state.empty_cells,
            "outputs": state.outputs,
            "textual_outputs": state.textual_outputs,
            "image_only_outputs": state.image_only_outputs,
            "kernel": kernelspec.get("name") if isinstance(kernelspec, dict) else None,
            "language": state.language,
            "language_version": (
                language_info.get("version") if isinstance(language_info, dict) else None
            ),
        }
    )
    return result


def _load(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InvalidNotebook(
            path,
            f"not valid UTF-8 at byte {exc.start}",
        ) from exc
    except OSError as exc:
        raise InvalidNotebook(path, str(exc)) from exc

    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise InvalidNotebook(
            path,
            f"malformed JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
        ) from exc
    except _DuplicateKey as exc:
        raise InvalidNotebook(path, f"duplicate object key {exc.key!r}") from exc
    except _InvalidConstant as exc:
        raise InvalidNotebook(path, str(exc)) from exc
    except RecursionError as exc:
        raise InvalidNotebook(path, "JSON nesting is too deep") from exc

    _validate_unicode(value, path, "JSON")
    if not isinstance(value, dict):
        raise InvalidNotebook(path, "top level must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def _reject_constant(value: str) -> None:
    raise _InvalidConstant(f"non-standard JSON numeric constant {value}")


def _validate_unicode(value: Any, path: Path, where: str) -> None:
    """Reject JSON escapes that decode to values UTF-8 cannot represent."""

    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise InvalidNotebook(
                path,
                f"{where} contains an unpaired Unicode surrogate at character {exc.start}",
            ) from exc
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_unicode(item, path, f"{where}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_unicode(key, path, f"{where} object key")
            _validate_unicode(item, path, f"{where}.{key}")


@dataclass(slots=True)
class _NotebookState:
    result: Extraction
    language: str | None
    headings: list[tuple[int, str, str]] = field(default_factory=list)
    code_cells: int = 0
    markdown_cells: int = 0
    raw_cells: int = 0
    empty_cells: int = 0
    outputs: int = 0
    textual_outputs: int = 0
    image_only_outputs: int = 0

    @property
    def source(self) -> str:
        return self.result.source

    @property
    def structure(self) -> tuple[str, ...]:
        return tuple(title for _, title, _ in self.headings)

    @property
    def section_unit(self) -> str | None:
        return self.headings[-1][2] if self.headings else None

    def add_cell(self, number: int, cell: dict[str, Any]) -> None:
        cell_type = cell.get("cell_type")
        if cell_type not in {"markdown", "code", "raw"}:
            raise InvalidNotebook(
                Path(self.source),
                f"cell {number} has unsupported or missing cell_type {cell_type!r}",
            )
        metadata = cell.get("metadata", {})
        if not isinstance(metadata, dict):
            raise InvalidNotebook(Path(self.source), f"cell {number} metadata must be an object")
        content = _source_text(cell.get("source", ""), f"cell {number} source", Path(self.source))
        ref = f"cell:{number}"

        if cell_type == "markdown":
            self.markdown_cells += 1
            cell_unit = self._add_markdown(number, ref, content, cell, metadata)
        elif cell_type == "code":
            self.code_cells += 1
            # Keep an empty code cell when it owns outputs: the PRODUCED_BY
            # endpoint is real even though the input source is empty.
            outputs = cell.get("outputs", [])
            if not isinstance(outputs, list):
                raise InvalidNotebook(Path(self.source), f"cell {number} outputs must be an array")
            cell_unit = self._add_code(
                number,
                ref,
                content,
                cell,
                metadata,
                keep=bool(outputs),
            )
            self._add_outputs(number, outputs, cell_unit)
        else:
            self.raw_cells += 1
            cell_unit = self._add_raw(number, ref, content, metadata)

        if cell_type != "code" and "outputs" in cell:
            self.result.add_gap(
                f"cell {number} is {cell_type}, but carries a non-standard outputs field that was ignored",
                ref=ref,
            )

        attachments = cell.get("attachments")
        if attachments is not None:
            if not isinstance(attachments, dict):
                raise InvalidNotebook(Path(self.source), f"cell {number} attachments must be an object")
            image_attachments = sum(
                1
                for value in attachments.values()
                if isinstance(value, dict) and any(_is_image_mime(str(mime)) for mime in value)
            )
            if image_attachments:
                self.result.add_gap(
                    f"cell:{number} contains {image_attachments} image attachment(s); image understanding is out of v1",
                    ref=ref,
                )

        if cell_unit is None:
            self.empty_cells += 1

    def _add_markdown(
        self,
        number: int,
        ref: str,
        content: str,
        cell: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Unit | None:
        if not content.strip():
            return None

        heading = _markdown_heading(content)
        parent = self.section_unit
        if heading:
            level, title = heading
            while self.headings and self.headings[-1][0] >= level:
                self.headings.pop()
            parent = self.section_unit
            structure = self.structure + (title,)
            salience = max(0.45, 1.0 - level * 0.1)
        else:
            level = None
            title = ""
            structure = self.structure
            salience = 0.55

        unit = Unit(
            source=self.source,
            modality=Modality.PROSE,
            content=content.strip(),
            origin=Origin(self.source, ref),
            role=Role.UNKNOWN,
            structure=structure,
            salience=salience,
            meta={
                "cell": number,
                "cell_id": cell.get("id"),
                "cell_type": "markdown",
                "heading": heading is not None,
                "heading_level": level,
                "tags": _tags(metadata, number, Path(self.source)),
            },
        )
        self.result.units.append(unit)
        from .text import reference_specs

        for target, label, kind in reference_specs(content):
            self.result.units.append(
                Unit(
                    source=self.source,
                    modality=Modality.REFERENCE,
                    content=target,
                    origin=Origin(self.source, ref),
                    role=Role.UNKNOWN,
                    structure=structure,
                    salience=0.3,
                    meta={
                        "target": target,
                        "label": label or None,
                        "ref_kind": kind,
                        "cell": number,
                    },
                )
            )
        if parent:
            self._section_relation(parent, unit.id)
        if heading:
            self.headings.append((level, title, unit.id))  # type: ignore[arg-type]
        return unit

    def _add_code(
        self,
        number: int,
        ref: str,
        content: str,
        cell: dict[str, Any],
        metadata: dict[str, Any],
        *,
        keep: bool,
    ) -> Unit | None:
        if not content and not keep:
            return None
        unit = Unit(
            source=self.source,
            modality=Modality.CODE,
            content=content,
            origin=Origin(self.source, ref),
            role=Role.UNKNOWN,
            structure=self.structure,
            salience=0.7,
            meta={
                "cell": number,
                "cell_id": cell.get("id"),
                "cell_type": "code",
                "language": self.language,
                "execution_count": cell.get("execution_count"),
                "empty_source": not bool(content),
                "tags": _tags(metadata, number, Path(self.source)),
            },
        )
        self.result.units.append(unit)
        if self.section_unit:
            self._section_relation(self.section_unit, unit.id)
        return unit

    def _add_raw(
        self,
        number: int,
        ref: str,
        content: str,
        metadata: dict[str, Any],
    ) -> Unit | None:
        if not content.strip():
            return None
        unit = Unit(
            source=self.source,
            modality=Modality.PROSE,
            content=content,
            origin=Origin(self.source, ref),
            role=Role.UNKNOWN,
            structure=self.structure,
            salience=0.4,
            meta={
                "cell": number,
                "cell_type": "raw",
                "raw_mimetype": metadata.get("raw_mimetype"),
                "tags": _tags(metadata, number, Path(self.source)),
            },
        )
        self.result.units.append(unit)
        if self.section_unit:
            self._section_relation(self.section_unit, unit.id)
        return unit

    def _add_outputs(
        self,
        cell_number: int,
        outputs: list[Any],
        cell_unit: Unit | None,
    ) -> None:
        for output_number, output in enumerate(outputs):
            self.outputs += 1
            where = f"cell:{cell_number}#output:{output_number}"
            if not isinstance(output, dict):
                raise InvalidNotebook(Path(self.source), f"{where} must be an object")

            payload = _output_payload(Path(self.source), where, output)
            if payload is None:
                mime_types = _mime_types(output)
                if mime_types and all(_is_image_mime(mime) for mime in mime_types):
                    self.image_only_outputs += 1
                    self.result.add_gap(
                        f"{where} is image-only ({', '.join(mime_types)}); image understanding is out of v1",
                        ref=where,
                    )
                else:
                    shown = ", ".join(mime_types) if mime_types else "no textual payload"
                    self.result.add_gap(f"{where} was skipped: {shown}", ref=where)
                continue

            content, modality, meta = payload
            if not content:
                self.result.add_gap(
                    f"{where} has an empty textual payload",
                    ref=where,
                )
                continue

            unit = Unit(
                source=self.source,
                modality=modality,
                content=content,
                origin=Origin(self.source, where),
                role=Role.UNKNOWN,
                structure=self.structure,
                salience=0.55,
                meta={"cell": cell_number, "output": output_number, **meta},
            )
            self.result.units.append(unit)
            self.textual_outputs += 1
            if cell_unit is None:
                self.result.add_gap(
                    f"{where} has no addressable producing cell because its source was empty",
                    ref=where,
                )
            else:
                self.result.relations.append(
                    Relation(
                        src=unit.id,
                        dst=cell_unit.id,
                        kind=RelationKind.PRODUCED_BY,
                        evidence=f"notebook output array of cell:{cell_number}",
                    )
                )

    def _section_relation(self, section: str, child: str) -> None:
        self.result.relations.append(
            Relation(
                src=section,
                dst=child,
                kind=RelationKind.DESCRIBES,
                evidence="notebook Markdown heading containment",
            )
        )


def _source_text(value: Any, where: str, path: Path) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return "".join(value)
    raise InvalidNotebook(path, f"{where} must be a string or array of strings")


def _output_payload(
    path: Path,
    where: str,
    output: dict[str, Any],
) -> tuple[str, Modality, dict[str, Any]] | None:
    output_type = output.get("output_type")
    if output_type == "stream":
        content = _source_text(output.get("text", ""), f"{where} text", path)
        return content, Modality.RECORD, {
            "output_type": "stream",
            "stream": output.get("name"),
            "mime_type": "text/plain",
        }

    if output_type in {"display_data", "execute_result"}:
        data = output.get("data")
        if not isinstance(data, dict):
            raise InvalidNotebook(path, f"{where} data must be an object")
        mime_types = sorted(str(mime) for mime in data)
        for mime in _TEXT_MIME_PRIORITY:
            if mime not in data:
                continue
            value = data[mime]
            if mime == "application/json":
                content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                modality = Modality.RECORD
            else:
                content = _source_text(value, f"{where} {mime}", path)
                modality = Modality.CODE if mime == "text/html" else Modality.PROSE
            return content, modality, {
                "output_type": output_type,
                "mime_type": mime,
                "mime_types": mime_types,
                "omitted_mime_types": [item for item in mime_types if item != mime] or None,
                "execution_count": output.get("execution_count"),
            }
        return None

    if output_type == "error":
        traceback = output.get("traceback", [])
        if not isinstance(traceback, list) or not all(isinstance(line, str) for line in traceback):
            raise InvalidNotebook(path, f"{where} traceback must be an array of strings")
        name = output.get("ename")
        value = output.get("evalue")
        if name is not None and not isinstance(name, str):
            raise InvalidNotebook(path, f"{where} ename must be a string")
        if value is not None and not isinstance(value, str):
            raise InvalidNotebook(path, f"{where} evalue must be a string")
        headline = f"{name or 'Error'}: {value}".rstrip(": ")
        content = "\n".join([headline, *traceback]).strip()
        return content, Modality.RECORD, {
            "output_type": "error",
            "error_name": name,
            "error_value": value,
            "mime_type": "text/plain",
        }

    raise InvalidNotebook(path, f"{where} has unsupported or missing output_type {output_type!r}")


def _mime_types(output: dict[str, Any]) -> list[str]:
    data = output.get("data")
    if not isinstance(data, dict):
        return []
    return sorted(str(mime) for mime in data)


def _is_image_mime(value: str) -> bool:
    return value.startswith("image/")


def _markdown_heading(content: str) -> tuple[int, str] | None:
    for line in content.splitlines():
        if not line.strip():
            continue
        if match := _HEADING.match(line):
            return len(match.group(1)), match.group(2)
        return None
    return None


def _tags(metadata: dict[str, Any], number: int, path: Path) -> list[str] | None:
    tags = metadata.get("tags")
    if tags is None:
        return None
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise InvalidNotebook(path, f"cell {number} metadata.tags must be an array of strings")
    return tags or None


def _language(metadata: dict[str, Any]) -> str | None:
    language_info = metadata.get("language_info")
    if isinstance(language_info, dict) and isinstance(language_info.get("name"), str):
        return language_info["name"]
    kernelspec = metadata.get("kernelspec")
    if isinstance(kernelspec, dict) and isinstance(kernelspec.get("language"), str):
        return kernelspec["language"]
    return None
