"""Semantic contracts for the dependency-free Tier-1 native extractors."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from autotldr.extract.docx import InvalidDocx, extract as extract_docx
from autotldr.extract.notebook import InvalidNotebook, extract as extract_notebook
from autotldr.unit import Modality, RelationKind, Role

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _write_docx(path: Path, document: str, comments: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        )
        package.writestr("word/document.xml", document)
        if comments is not None:
            package.writestr("word/comments.xml", comments)
    return path


def _assert_addressable_unknown(result, path: Path) -> None:
    assert result.units
    ids = {unit.id for unit in result.units}
    assert len(ids) == len(result.units)
    assert all(unit.source == str(path) for unit in result.units)
    assert all(unit.origin.source == str(path) and unit.origin.ref for unit in result.units)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)
    assert all(relation.src in ids and relation.dst in ids for relation in result.relations)


def test_docx_preserves_heading_paragraph_table_comment_and_revisions(tmp_path):
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Evaluation</w:t></w:r>
    </w:p>
    <w:p>
      <w:commentRangeStart w:id="7"/>
      <w:r><w:t xml:space="preserve">Run </w:t></w:r>
      <w:del w:id="1" w:author="Editor"><w:r><w:delText>ten</w:delText></w:r></w:del>
      <w:ins w:id="2" w:author="Editor"><w:r><w:t>forty</w:t></w:r></w:ins>
      <w:r><w:t xml:space="preserve"> trials.</w:t></w:r>
      <w:commentRangeEnd w:id="7"/>
      <w:r><w:commentReference w:id="7"/></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Metric</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Latency</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>12 ms</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:altChunk r:id="external1"/>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    comments = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:comments xmlns:w="{W}">
  <w:comment w:id="7" w:author="Reviewer" w:initials="RV" w:date="2026-08-30T12:00:00Z">
    <w:p><w:r><w:t>Document why forty trials are sufficient.</w:t></w:r></w:p>
  </w:comment>
</w:comments>
"""
    path = _write_docx(tmp_path / "evaluation.docx", document, comments)

    result = extract_docx(path)

    _assert_addressable_unknown(result, path)
    assert result.kind == "docx"
    assert result.meta == {
        "paragraphs": 2,
        "tables": 1,
        "table_rows": 2,
        "comments": 1,
        "tracked_revisions": 2,
        "external_content_blocks": 1,
    }

    by_ref = {unit.origin.ref: unit for unit in result.units}
    heading = by_ref["para:1"]
    paragraph = by_ref["para:2"]
    header = by_ref["table:1#row:1"]
    row = by_ref["table:1#row:2"]
    comment = by_ref["comment:7"]

    assert heading.content == "Evaluation"
    assert heading.meta["heading_level"] == 1
    assert heading.structure == ("Evaluation",)
    assert paragraph.content == "Run forty trials."
    assert "ten" not in paragraph.content
    assert paragraph.meta["tracked_insertions"] == ["forty"]
    assert paragraph.meta["tracked_deletions"] == ["ten"]
    assert paragraph.meta["comment_ids"] == ["7"]
    assert paragraph.structure == ("Evaluation",)

    assert header.modality is Modality.RECORD
    assert header.content == "Metric | Value"
    assert header.meta["header_candidate"] is True
    assert row.content == "Latency | 12 ms"
    assert row.meta["cells"] == ["Latency", "12 ms"]
    assert row.structure == ("Evaluation", "Table 1")

    assert comment.content == "Document why forty trials are sufficient."
    assert comment.meta["author"] == "Reviewer"
    assert comment.meta["anchors"] == [paragraph.id]
    assert any(
        relation.src == comment.id
        and relation.dst == paragraph.id
        and relation.kind is RelationKind.DESCRIBES
        and "comment 7" in relation.evidence
        for relation in result.relations
    )
    described = {
        relation.dst
        for relation in result.relations
        if relation.src == heading.id and relation.kind is RelationKind.DESCRIBES
    }
    assert {paragraph.id, header.id, row.id} <= described
    assert any("tracked revision" in gap for gap in result.gaps)
    assert any("externally linked content" in gap for gap in result.gaps)

    # Native addresses and content make IDs repeatable without package metadata.
    assert [unit.id for unit in extract_docx(path).units] == [unit.id for unit in result.units]


@pytest.mark.parametrize(
    ("builder", "needle"),
    [
        (lambda path: path.write_bytes(b"not a zip"), "ZIP/OOXML"),
        (
            lambda path: zipfile.ZipFile(path, "w").close(),
            "missing required member word/document.xml",
        ),
        (
            lambda path: _write_docx(path, "<w:document xmlns:w='urn:word'><w:body>"),
            "malformed word/document.xml",
        ),
    ],
)
def test_invalid_docx_is_typed_and_names_the_broken_package_part(tmp_path, builder, needle):
    path = tmp_path / "broken.docx"
    builder(path)

    with pytest.raises(InvalidDocx) as raised:
        extract_docx(path)

    assert raised.value.path == path
    assert raised.value.kind == "DOCX"
    assert needle in str(raised.value)


def test_notebook_preserves_cells_text_outputs_and_production_edges(tmp_path):
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "language": "python"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "cells": [
            {
                "id": "intro",
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Experiment\n",
                    "Read results.csv and https://example.test/method; ",
                    "the scalar 2.80 is not a path.",
                ],
                "attachments": {"plot.png": {"image/png": "AAAA"}},
            },
            {
                "id": "measure",
                "cell_type": "code",
                "execution_count": 3,
                "metadata": {"tags": ["parameters"]},
                "source": ["samples = measure(40)\n", "median(samples)"],
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": ["running\n", "done\n"]},
                    {
                        "output_type": "execute_result",
                        "execution_count": 3,
                        "metadata": {},
                        "data": {"text/plain": ["12.0\n"], "image/png": "BBBB"},
                    },
                    {
                        "output_type": "error",
                        "ename": "ValueError",
                        "evalue": "bad sample",
                        "traceback": ["Traceback line", "ValueError: bad sample"],
                    },
                ],
            },
            {
                "id": "plot",
                "cell_type": "code",
                "execution_count": 4,
                "metadata": {},
                "source": "plot(samples)",
                "outputs": [
                    {
                        "output_type": "display_data",
                        "metadata": {},
                        "data": {"image/png": "CCCC"},
                    }
                ],
            },
            {
                "id": "raw",
                "cell_type": "raw",
                "metadata": {"raw_mimetype": "text/latex"},
                "source": "\\newcommand{\\samples}{40}",
            },
            {
                "id": "empty",
                "cell_type": "markdown",
                "metadata": {},
                "source": [],
            },
        ],
    }
    path = tmp_path / "experiment.ipynb"
    path.write_text(json.dumps(notebook), encoding="utf-8")

    result = extract_notebook(path)

    _assert_addressable_unknown(result, path)
    assert result.kind == "notebook"
    assert result.meta == {
        "nbformat": 4,
        "nbformat_minor": 5,
        "cells": 5,
        "code_cells": 2,
        "markdown_cells": 2,
        "raw_cells": 1,
        "empty_cells": 1,
        "outputs": 4,
        "textual_outputs": 3,
        "image_only_outputs": 1,
        "kernel": "python3",
        "language": "python",
        "language_version": "3.12",
    }

    by_ref = {
        unit.origin.ref: unit
        for unit in result.units
        if unit.modality is not Modality.REFERENCE
    }
    heading = by_ref["cell:1"]
    code = by_ref["cell:2"]
    stream = by_ref["cell:2#output:0"]
    value = by_ref["cell:2#output:1"]
    error = by_ref["cell:2#output:2"]
    plot_code = by_ref["cell:3"]
    raw = by_ref["cell:4"]

    assert heading.content.startswith("# Experiment")
    assert heading.meta["heading_level"] == 1
    assert code.modality is Modality.CODE
    assert code.content == "samples = measure(40)\nmedian(samples)"
    assert code.meta["execution_count"] == 3
    assert code.meta["tags"] == ["parameters"]
    assert code.structure == ("Experiment",)
    assert stream.modality is Modality.RECORD
    assert stream.content == "running\ndone\n"
    assert stream.meta["stream"] == "stdout"
    assert value.modality is Modality.PROSE
    assert value.content == "12.0\n"
    assert value.meta["mime_type"] == "text/plain"
    assert value.meta["omitted_mime_types"] == ["image/png"]
    assert error.content.startswith("ValueError: bad sample\nTraceback line")
    assert plot_code.content == "plot(samples)"
    assert raw.content == "\\newcommand{\\samples}{40}"
    assert raw.meta["raw_mimetype"] == "text/latex"

    produced_by = {
        relation.src: relation.dst
        for relation in result.relations
        if relation.kind is RelationKind.PRODUCED_BY
    }
    assert produced_by == {
        stream.id: code.id,
        value.id: code.id,
        error.id: code.id,
    }
    assert any("cell:3#output:0 is image-only" in gap for gap in result.gaps)
    assert any("cell:1 contains 1 image attachment" in gap for gap in result.gaps)
    assert any("1 empty cell" in gap for gap in result.gaps)
    assert "cell:3#output:0" not in by_ref
    references = [
        unit
        for unit in result.units
        if unit.modality is Modality.REFERENCE
    ]
    assert {unit.meta["target"] for unit in references} == {
        "results.csv",
        "https://example.test/method",
    }
    assert all(unit.origin.ref == "cell:1" for unit in references)
    assert [unit.id for unit in extract_notebook(path).units] == [unit.id for unit in result.units]


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ('{"nbformat":4,"cells":[}', "malformed JSON at line 1"),
        ("[]", "top level must be an object"),
        ('{"nbformat":4,"cells":[],"cells":[]}', "duplicate object key 'cells'"),
        (
            '{"nbformat":4,"metadata":{},"cells":[{"cell_type":"code","metadata":{},"source":7,"outputs":[]}]}',
            "cell 1 source must be a string or array of strings",
        ),
        (
            '{"nbformat":4,"metadata":{},"cells":[{"cell_type":"code","metadata":{},"source":"x","outputs":[{"output_type":"video"}]}]}',
            "unsupported or missing output_type 'video'",
        ),
        (
            '{"nbformat":4,"metadata":{},"cells":[{"cell_type":"code","metadata":{},"source":"\\ud800","outputs":[]}]}',
            "unpaired Unicode surrogate",
        ),
    ],
)
def test_invalid_notebook_is_typed_and_actionable(tmp_path, body, needle):
    path = tmp_path / "broken.ipynb"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(InvalidNotebook) as raised:
        extract_notebook(path)

    assert raised.value.path == path
    assert raised.value.kind == "Jupyter notebook"
    assert needle in str(raised.value)


def test_notebook_code_and_html_claims_preserve_exact_native_text(tmp_path):
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"language_info": {"name": "python"}},
        "cells": [
            {
                "id": "exact",
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "source": ["  café = 1  \r\n", "print(café)\r\n"],
                "outputs": [
                    {
                        "output_type": "display_data",
                        "metadata": {},
                        "data": {"text/html": ["  <pre>café</pre>  \r\n"]},
                    }
                ],
            },
            {
                "id": "raw",
                "cell_type": "raw",
                "metadata": {"raw_mimetype": "text/x-python"},
                "source": "  keep = True  \r\n",
            },
        ],
    }
    path = tmp_path / "exact.ipynb"
    path.write_text(json.dumps(notebook, ensure_ascii=False), encoding="utf-8")

    result = extract_notebook(path)
    by_ref = {unit.origin.ref: unit for unit in result.units}

    code = by_ref["cell:1"]
    html = by_ref["cell:1#output:0"]
    raw = by_ref["cell:2"]
    assert code.modality is Modality.CODE
    assert code.content == "  café = 1  \r\nprint(café)\r\n"
    assert code.origin.char_span is None
    assert html.modality is Modality.CODE
    assert html.content == "  <pre>café</pre>  \r\n"
    assert html.origin.char_span is None
    assert raw.content == "  keep = True  \r\n"


def test_notebook_rejects_non_utf8_bytes_before_emitting_claims(tmp_path):
    path = tmp_path / "broken.ipynb"
    path.write_bytes(b'{"nbformat":4,"metadata":{},"cells":[]}' + bytes([0xFF]))

    with pytest.raises(InvalidNotebook) as raised:
        extract_notebook(path)

    assert raised.value.path == path
    assert raised.value.kind == "Jupyter notebook"
    assert "not valid UTF-8" in str(raised.value)
    assert "byte 39" in str(raised.value)
