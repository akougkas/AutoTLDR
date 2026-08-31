"""Semantic contracts for the dependency-free Tier 1 fast paths."""

from __future__ import annotations

import zipfile

import pytest

from autotldr.extract import html as html_module
from autotldr.router import UnsupportedFormat, extract
from autotldr.unit import Modality, Role


def test_local_html_removes_chrome_and_keeps_element_provenance(tmp_path):
    page = tmp_path / "guide.html"
    page.write_text(
        """<!doctype html><html><body>
<nav><img src="logo.png">Menu that is not the document.</nav>
<main><h1 id="start">Relay guide</h1>
<p>Retry the relay exactly twice. <a href="details.html">Details</a></p></main>
</body></html>""",
        encoding="utf-8",
    )

    result = extract(page)

    assert result.kind == "html"
    assert any(unit.content == "Relay guide" for unit in result.units)
    assert any("exactly twice" in unit.content for unit in result.units)
    assert not any("Menu that" in unit.content for unit in result.units)
    assert all(unit.origin.source == str(page) for unit in result.units)
    assert all(unit.origin.ref.startswith("line:") for unit in result.units)
    refs = [unit for unit in result.units if unit.modality is Modality.REFERENCE]
    assert refs and refs[0].meta["target"] == "details.html"
    assert all(unit.role is Role.UNKNOWN for unit in result.units)


def test_short_linked_html_survives_optional_main_content_filter(monkeypatch):
    source = (
        "<main><h1>Guide</h1><p>Alpha serves reports. Read the "
        '<a href="detail.html">capacity detail</a>.</p></main>'
    )
    monkeypatch.setattr(
        html_module,
        "_trafilatura_text",
        lambda _text: ("Guide", "trafilatura"),
    )

    result = html_module.extract_html(source, source="guide.html")

    assert any(unit.content == "Alpha serves reports. Read the capacity detail." for unit in result.units)
    reference = next(
        unit for unit in result.units if unit.modality is Modality.REFERENCE
    )
    assert reference.meta["target"] == "detail.html"
    assert reference.origin.source == "guide.html"
    assert result.meta["addressable_blocks"] == 2
    assert result.meta["emitted_blocks"] == 2
    assert result.meta["trafilatura_mapped_blocks"] == 1
    assert result.meta["linked_blocks_retained"] == 1
    assert not result.gaps


def test_latex_recovers_sections_equations_labels_and_citations(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(
        r"""\documentclass{article}
\begin{document}
\section{Method}
We follow the relay protocol in \cite{relay2026}; see results.csv and
https://doi.org/10.5555/external. The scalar 1.7 is not a path.

\begin{equation}
y = 2x
\label{eq:relay}
\end{equation}
See Equation~\ref{eq:relay}.
\newcommand{\relay}{R}
\end{document}
""",
        encoding="utf-8",
    )

    result = extract(source)

    assert result.kind == "latex"
    assert any(unit.meta.get("heading") and unit.content == "Method" for unit in result.units)
    equations = [unit for unit in result.units if unit.modality is Modality.EQUATION]
    assert len(equations) == 1
    assert equations[0].meta["labels"] == ["eq:relay"]
    targets = {
        unit.meta["target"]
        for unit in result.units
        if unit.modality is Modality.REFERENCE
    }
    assert {
        "relay2026",
        "eq:relay",
        "results.csv",
        "https://doi.org/10.5555/external",
    } <= targets
    assert "1.7" not in targets
    assert not any(target.startswith("org/") for target in targets)
    assert all(unit.origin.ref.startswith("line:") for unit in result.units)
    assert any("not expanded" in gap for gap in result.gaps)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)


def test_latex_recovers_compact_section_prose_and_equation_boundaries(tmp_path):
    source = tmp_path / "compact.tex"
    source.write_text(
        r"\section{Alpha {model}} Alpha serves reports."
        r"\begin{equation}x=42\label{eq:answer}\end{equation}"
        r" Continue safely.",
        encoding="utf-8",
    )

    result = extract(source)

    semantic = [
        unit for unit in result.units if unit.modality is not Modality.REFERENCE
    ]
    assert [unit.content for unit in semantic] == [
        "Alpha {model}",
        "Alpha serves reports.",
        r"\begin{equation}x=42\label{eq:answer}\end{equation}",
        "Continue safely.",
    ]
    assert [unit.modality for unit in semantic] == [
        Modality.PROSE,
        Modality.PROSE,
        Modality.EQUATION,
        Modality.PROSE,
    ]
    assert semantic[0].meta["heading"] is True
    assert semantic[2].meta["labels"] == ["eq:answer"]
    assert all(unit.structure == ("Alpha {model}",) for unit in semantic)
    assert all(unit.origin.ref == "line:1" for unit in semantic)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)


def test_epub_follows_spine_order_and_uses_item_origins(tmp_path):
    book = tmp_path / "relay.epub"
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    package = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Relay Book</dc:title></metadata>
 <manifest>
  <item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>
  <item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine><itemref idref="first"/><itemref idref="second"/></spine>
</package>"""
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/book.opf", package)
        archive.writestr(
            "OEBPS/first.xhtml",
            "<html><body><h1 id='one'>First</h1><p>Open the relay.</p></body></html>",
        )
        archive.writestr(
            "OEBPS/second.xhtml",
            "<html><body><h1 id='two'>Second</h1><p>Close the relay.</p></body></html>",
        )

    result = extract(book)

    assert result.kind == "epub"
    content = [unit.content for unit in result.units]
    assert content.index("First") < content.index("Second")
    assert result.meta["title"] == "Relay Book"
    assert result.meta["spine_items"] == 2
    assert all(unit.origin.source == str(book) for unit in result.units)
    assert all(unit.origin.ref.startswith("item:") for unit in result.units)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)


def test_directory_is_named_as_deferred_tier_two(tmp_path):
    with pytest.raises(UnsupportedFormat) as raised:
        extract(tmp_path)

    message = str(raised.value)
    assert "directory" in message
    assert "tier 2" in message
