"""Focused closure tests for Stage 7's self-contained shareable outputs."""

from __future__ import annotations

import builtins
import json
import re
import subprocess
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

import autotldr.render as render_module
import autotldr.share as share_module
from autotldr.errors import MissingOptionalDependency
from autotldr.extensions import (
    ExtensionCollisionError,
    ExtensionRegistry,
    RendererSpec,
)
from autotldr.render import BudgetTooSmall, RenderOptions, render
from autotldr.share import render_pdf
from autotldr.unit import (
    Extraction,
    Gap,
    GroundedStatement,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Unit,
)


class _HTMLProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.hrefs: list[str] = []
        self.ids: list[str] = []
        self.data: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append(tag)
        values = dict(attrs)
        if isinstance(values.get("href"), str):
            self.hrefs.append(values["href"])
        if isinstance(values.get("id"), str):
            self.ids.append(values["id"])

    def handle_data(self, data: str) -> None:
        self.data.append(data)


def _share_result(*, oversized: bool = False, fallback: bool = False) -> Extraction:
    hostile_source = "field`\x1b\x7f\u202e🧪<script>.md"
    first = Unit(
        source=hostile_source,
        modality=Modality.PROSE,
        content='Measured <script>alert("x")</script> & bounded\x07\u2066.',
        origin=Origin(hostile_source, "line:`2", (4, 47)),
        salience=1.0,
    )
    remote_source = "https://example.test/report?q=one&lang=en"
    second = Unit(
        source=remote_source,
        modality=Modality.CODE,
        content="def measured():\n    return '<safe>'" + ("x" * 30_000 if oversized else ""),
        origin=Origin(remote_source, "section:méthod", (0, 35)),
        salience=0.1,
        meta={"language": "python"},
    )
    statement = GroundedStatement(
        content="The measured result is bounded by the referenced method.",
        origins=(first.origin, second.origin),
        evidence_unit_ids=(first.id, second.id),
    )
    model = {
        "task": "collection-synthesis",
        "model": "zbook-local/<model>",
        "outcome": "fallback-timeout" if fallback else "success",
        "fallback": {
            "used": fallback,
            "reason": "timeout" if fallback else None,
        },
    }
    return Extraction(
        source="shareable <brief>",
        kind="collection",
        units=[first, second],
        relations=[
            Relation(first.id, second.id, RelationKind.DERIVES_FROM, "native <edge>")
        ],
        gaps=[Gap("No <unsafe> rationale was documented.", first.origin)],
        meta={"models": [model]},
        summary_claims=[statement],
    )


def test_html_is_self_contained_control_safe_semantic_and_fully_linked() -> None:
    result = _share_result()
    text = render(result, output="html")
    lowered = text.casefold()
    probe = _HTMLProbe()
    probe.feed(text)

    assert text.startswith("<!doctype html>\n<html lang=\"en\">")
    assert text.endswith("</html>\n")
    assert '<meta charset="utf-8">' in text
    assert probe.tags.count("style") == 1
    assert not {"script", "link", "img", "iframe", "object"}.intersection(probe.tags)
    assert "@import" not in lowered
    assert "url(" not in lowered
    assert "color-mix(" not in lowered
    assert "\x1b" not in text
    assert "\x7f" not in text
    assert "\u202e" not in text
    assert "\u2066" not in text
    assert '<script>alert("x")</script>' not in text
    assert "&lt;script&gt;alert" in text
    assert "\\x1b" in text and "\\u202e" in text
    assert {"header", "main", "section", "article", "footer"} <= set(probe.tags)
    assert {"summary", "units", "relations", "findings", "references", "selection"} <= set(probe.ids)

    expected_targets = {
        render_module._origin_target(unit.origin) for unit in result.units
    }
    expected_targets.update(
        render_module._origin_target(origin)
        for statement in result.summary_claims
        for origin in statement.origins
    )
    assert expected_targets <= set(probe.hrefs)
    assert all(target.startswith(("https://", "field%60")) for target in expected_targets)
    assert all(
        value.startswith("link-")
        for value in probe.ids
        if value.startswith("link-")
    )
    for statement in result.summary_claims:
        assert f"statement-{statement.id}" in probe.ids
        for unit_id in statement.evidence_unit_ids:
            assert f'data-unit-id="{unit_id}"' in text
    assert "Model synthesis" in text
    assert "zbook-local/&lt;model&gt; · accepted" in text


def test_html_reports_grounded_model_fallback_compactly() -> None:
    text = render(_share_result(fallback=True), output="html")

    assert "Grounded fallback" in text
    assert "fallback-timeout" in text
    assert "status-fallback" in text


def test_html_budget_counts_complete_utf8_and_keeps_full_drop_records() -> None:
    result = _share_result(oversized=True)
    with pytest.raises(BudgetTooSmall) as raised:
        render(result, output="html", budget=1)

    text = render(result, output="html", budget=raised.value.required)
    used = int(re.search(r'data-used="(\d+)"', text).group(1))
    records = []
    for match in re.finditer(
        r'<div class="drop-record" data-drop-kind="(unit|relation|statement)">(.*?)</div>',
        text,
        re.DOTALL,
    ):
        visible = unescape(re.sub(r"<[^>]+>", "", match.group(2)))
        visible = visible.replace("\n", "").strip()
        prefix = f"drop-v1/{match.group(1)} "
        assert visible.startswith(prefix)
        records.append((match.group(1), json.loads(visible[len(prefix) :])))

    assert used == len(text.encode("utf-8")) == raised.value.required
    assert 'data-requested="' + str(raised.value.required) + '"' in text
    assert records
    unit_record = next(
        record
        for kind, record in records
        if kind == "unit" and record.get("id") == result.units[1].id
    )
    assert set(unit_record) == {"id", "origin", "reason"}
    assert unit_record["origin"] == {
        "source": result.units[1].origin.source,
        "ref": result.units[1].origin.ref,
        "char_span": [0, 35],
    }
    assert unit_record["reason"] == "budget"


@pytest.mark.parametrize(
    "renderer",
    [
        RendererSpec("html", "example.output", "render"),
        RendererSpec("web-page", "example.output", "render", suffixes=(".html",)),
        RendererSpec(
            "web-media",
            "example.output",
            "render",
            media_types=("text/html",),
        ),
    ],
)
def test_html_core_keys_are_reserved_from_extensions(renderer: RendererSpec) -> None:
    with pytest.raises(ExtensionCollisionError, match="collides"):
        render_module.validate_extension_registry(ExtensionRegistry((renderer,)))


def test_pdf_uses_exact_generated_html_as_story_input(monkeypatch) -> None:
    result = _share_result()
    options = RenderOptions("pdf", True, False, 2)
    bundle = render_module._prepare_bundle(
        result,
        {0, 1},
        requested=None,
        used=123,
        available=456,
    )
    observed: list[str] = []

    def capture(html: str, *, minimum_size: int = 0) -> bytes:
        assert minimum_size == 456
        observed.append(html)
        return b"%PDF-exact-html"

    monkeypatch.setattr(share_module, "_story_pdf", capture)

    assert share_module._build_pdf(bundle, options) == b"%PDF-exact-html"
    assert observed == [render_module._build_html(bundle, options)]


def test_pdf_is_a4_paginated_linked_and_byte_deterministic() -> None:
    pymupdf = pytest.importorskip("pymupdf")
    result = _share_result()

    first = render_pdf(result)
    second = render_pdf(result)

    assert first.startswith(b"%PDF-")
    assert first == second
    assert first.rstrip().endswith(b"%%EOF")
    assert first.rfind(b"AutoTLDR deterministic byte padding") < first.rfind(
        b"startxref"
    )
    with pymupdf.open(stream=first, filetype="pdf") as document:
        assert document.page_count >= 2
        assert all(
            (round(page.rect.width), round(page.rect.height)) == (595, 842)
            for page in document
        )
        for page_number, page in enumerate(document, start=1):
            assert f"Page {page_number} of {document.page_count}" in page.get_text()
        text = "\n".join(page.get_text() for page in document)
        assert "References" in text
        links = [link for page in document for link in page.get_links()]
        remote_target = render_module._origin_target(result.units[1].origin)
        assert any(link.get("uri") == remote_target for link in links)
        assert document.metadata == {
            "format": "PDF 1.7",
            "title": "",
            "author": "",
            "subject": "",
            "keywords": "",
            "creator": "",
            "producer": "",
            "creationDate": "",
            "modDate": "",
            "trapped": "",
            "encryption": None,
        }


def test_pdf_budget_is_complete_exact_and_never_truncates() -> None:
    pymupdf = pytest.importorskip("pymupdf")
    result = _share_result()
    unlimited = render_pdf(result)
    bounded = render_pdf(result, budget=len(unlimited))

    assert len(bounded) <= len(unlimited)
    with pymupdf.open(stream=bounded, filetype="pdf") as document:
        text = "\n".join(page.get_text() for page in document)
    match = re.search(r"Portable bytes\s+(\d+) / (\d+)", text)
    assert match is not None
    assert int(match.group(1)) == len(bounded)
    assert int(match.group(2)) == len(unlimited)
    assert "binary-byte-v1" in text

    with pytest.raises(BudgetTooSmall) as raised:
        render_pdf(result, budget=1)
    assert raised.value.output == "pdf"
    assert raised.value.required > 1

    minimum = render_pdf(result, budget=raised.value.required)
    assert minimum.startswith(b"%PDF-")
    assert len(minimum) == raised.value.required
    with pymupdf.open(stream=minimum, filetype="pdf") as document:
        minimum_text = "".join(page.get_text().replace("\n", "") for page in document)
    assert "drop-v1/unit" in minimum_text
    assert "drop-v1/relation" in minimum_text
    assert "drop-v1/statement" in minimum_text
    assert all(unit.id in minimum_text for unit in result.units)
    bundle = render_module._prepare_bundle(
        result,
        set(),
        requested=raised.value.required,
        used=raised.value.required,
        available=len(unlimited),
    )
    dropped = bundle.selection["dropped"]
    for kind, records in (
        ("unit", dropped["reported"]),
        ("relation", dropped["reported_relations"]),
        ("statement", dropped["reported_statements"]),
    ):
        for record in records:
            # PDF layout may represent a line wrap as whitespace.  The
            # invariant is that every canonical field and value survives; the
            # HTML wire test above separately proves exact reconstruction.
            assert f"drop-v1/{kind}" in minimum_text
            pending: list[object] = [record]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        assert f'"{key}"' in minimum_text
                        pending.append(child)
                elif isinstance(value, list):
                    pending.extend(value)
                elif isinstance(value, str):
                    token = (
                        json.dumps(value, ensure_ascii=True)
                        .replace("`", r"\u0060")
                        .replace("\x7f", r"\u007f")
                    )
                    assert token[1:-1] in minimum_text
                elif value is not None:
                    assert json.dumps(value) in minimum_text


def test_pdf_budget_fixed_point_does_not_oscillate() -> None:
    import random
    import string

    pytest.importorskip("pymupdf")
    rng = random.Random(9)
    content = "".join(
        rng.choice(string.ascii_letters + " <>\n&") for _ in range(334)
    )
    result = Extraction(
        "f.md",
        "md",
        [Unit("f.md", Modality.PROSE, content, Origin("f.md", "line:10"))],
    )

    unlimited = render_pdf(result)
    bounded = render_pdf(result, budget=len(unlimited))

    assert len(bounded) <= len(unlimited)
    assert bounded.rstrip().endswith(b"%%EOF")


def test_missing_pdf_dependency_is_named_and_transitive_errors_are_not_masked(
    monkeypatch,
) -> None:
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "pymupdf":
            raise ModuleNotFoundError("blocked for test", name="pymupdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(MissingOptionalDependency) as raised:
        share_module._load_pymupdf()
    assert raised.value.feature == "PDF output"
    assert raised.value.dependency == "pymupdf"
    assert raised.value.extra == "pdf"
    assert "autotldr[pdf]" in str(raised.value)

    def broken(name, *args, **kwargs):
        if name == "pymupdf":
            raise ModuleNotFoundError("transitive", name="pymupdf._mupdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(ModuleNotFoundError, match="transitive"):
        share_module._load_pymupdf()


def test_importing_render_and_share_and_rendering_html_stays_pdf_lazy() -> None:
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        "from autotldr.render import render; "
        "import autotldr.share; "
        "from autotldr.unit import Extraction, Modality, Origin, Unit; "
        "u=Unit('x', Modality.PROSE, 'fact', Origin('x', 'line:1')); "
        "render(Extraction('x', 'text', [u]), output='html'); "
        "print(','.join(name for name in ('pymupdf','fitz') if name in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


def test_public_api_and_cli_share_the_binary_pdf_path(tmp_path) -> None:
    pytest.importorskip("pymupdf")
    source = tmp_path / "brief.md"
    source.write_text("# Purpose\n\nThe relay validates signed jobs.\n", encoding="utf-8")

    from autotldr.api import summarize

    api_result = summarize([source], output="pdf")
    assert isinstance(api_result.rendered, bytes)
    assert api_result.rendered.startswith(b"%PDF-")

    target = tmp_path / "brief.pdf"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autotldr.cli",
            str(source),
            "--out",
            "pdf",
            "--output",
            str(target),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == b""
    assert target.read_bytes().startswith(b"%PDF-")
