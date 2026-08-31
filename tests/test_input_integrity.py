"""Hostile-input regressions for Stage 3 acquisition and provenance."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autotldr.extract.html import extract_html
from autotldr.extract.structured import InvalidStructuredData
from autotldr.cli import EXIT_UNSUPPORTED, main
from autotldr.router import (
    UnsupportedFormat,
    declined_suffixes,
    detect,
    extract,
    extract_stdin,
    extract_url,
    input_type_names,
    supported_suffixes,
)
from autotldr.unit import Modality, RelationKind


def test_markdown_fence_payload_and_crlf_span_are_exact(tmp_path):
    source = (
        "# Guide\r\n\r\n"
        "```python\r\n"
        "  keep trailing spaces  \r\n"
        "```not-a-closing-fence\r\n"
        "print('still code')\r\n"
        "```\r\n"
        "Final line without newline"
    )
    path = tmp_path / "guide.md"
    path.write_bytes(source.encode("utf-8"))

    result = extract(path)
    code = next(unit for unit in result.units if unit.modality is Modality.CODE)

    assert code.content == (
        "  keep trailing spaces  \r\n"
        "```not-a-closing-fence\r\n"
        "print('still code')\r\n"
    )
    assert code.origin.char_span is not None
    assert source[slice(*code.origin.char_span)] == code.content
    final = next(unit for unit in result.units if unit.content == "Final line without newline")
    assert final.origin.char_span is not None
    assert source[slice(*final.origin.char_span)] == final.content


@pytest.mark.parametrize(("suffix", "kind"), [(".md", "text"), (".html", "HTML"), (".tex", "LaTeX")])
def test_text_derived_paths_refuse_invalid_utf8(tmp_path, suffix, kind):
    path = tmp_path / f"hostile{suffix}"
    path.write_bytes(b"valid prefix\xffinvented suffix")

    with pytest.raises(ValueError) as raised:
        extract(path)

    message = str(raised.value)
    assert path.name in message
    assert kind.casefold() in message.casefold()
    assert "byte" in message
    assert "replacement" not in message.casefold()


def test_stdin_refuses_invalid_utf8_without_leaking_a_temp_name():
    with pytest.raises(ValueError) as raised:
        extract_stdin(b"truth\xffclaim", kind="markdown")

    message = str(raised.value)
    assert "<stdin>" in message
    assert "byte 5" in message
    assert "/tmp/" not in message


def test_invalid_materialized_structured_input_names_stdin_not_tempfile():
    with pytest.raises(ValueError) as raised:
        extract_stdin(b'{"broken": }', kind="json")

    message = str(raised.value)
    assert "<stdin>" in message
    assert "/tmp/" not in message
    assert "tmp" not in message.casefold()


def test_html_keeps_direct_tail_and_exact_code_but_declines_markup_code():
    source = (
        "<main><h1>Heading</h1>important tail"
        "<pre>  exact  \n</pre>"
        "<pre><span>not exactly sliceable</span></pre></main>"
    )

    result = extract_html(source, source="guide.html")

    assert any(unit.content == "important tail" for unit in result.units)
    code = next(unit for unit in result.units if unit.modality is Modality.CODE)
    assert code.content == "  exact  \n"
    assert code.origin.char_span is not None
    assert source[slice(*code.origin.char_span)] == code.content
    markup_gap = next(gap for gap in result.gaps if "nested HTML markup" in gap)
    assert markup_gap.origin.source == "guide.html"
    assert markup_gap.origin.ref.startswith("line:")
    assert not any("not exactly sliceable" in unit.content for unit in result.units)


def test_duplicate_html_ids_fall_back_to_distinct_native_addresses():
    result = extract_html(
        "<main><p id='duplicate'>same</p><p id='duplicate'>same</p></main>",
        source="duplicate.html",
    )
    paragraphs = [unit for unit in result.units if unit.content == "same"]

    assert len(paragraphs) == 2
    assert len({unit.id for unit in paragraphs}) == 2
    assert {unit.origin.ref for unit in paragraphs} == {
        "line:1#element:2",
        "line:1#element:3",
    }


@pytest.mark.parametrize(
    ("suffix", "payload", "needle"),
    [
        (".json", '{"bad":"\\ud800"}', "unpaired Unicode surrogate"),
        (".jsonl", '{"bad":"\\udfff"}\n', "unpaired Unicode surrogate"),
        (".yaml", "root: &root [*root]\n", "recursive alias cycle"),
        (".xml", "<x>" * 140 + "</x>" * 140, "nesting exceeds"),
    ],
)
def test_structured_hostility_is_a_typed_decline(tmp_path, suffix, payload, needle):
    path = tmp_path / f"hostile{suffix}"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(InvalidStructuredData, match=needle):
        extract(path)


def test_csv_column_count_is_bounded_before_profile_allocation(tmp_path):
    path = tmp_path / "wide.csv"
    path.write_text(",".join(f"c{index}" for index in range(4097)), encoding="utf-8")

    with pytest.raises(InvalidStructuredData, match="4097 columns; limit is 4096"):
        extract(path)


def _epub_bytes(*, href: str = "chapter.xhtml", duplicate_spine: bool = True) -> bytes:
    container = """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>"""
    repeated = '<itemref idref="chapter"/>' if duplicate_spine else ""
    package = f"""<package xmlns="http://www.idpf.org/2007/opf">
<manifest><item id="chapter" href="{href}" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="chapter"/>{repeated}</spine></package>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/book.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", "<html><body><p>Same chapter.</p></body></html>")
    return buffer.getvalue()


def test_duplicate_epub_spine_entries_keep_distinct_unit_ids(tmp_path):
    path = tmp_path / "duplicate.epub"
    path.write_bytes(_epub_bytes())

    result = extract(path)

    assert len(result.units) == 2
    assert len({unit.id for unit in result.units}) == 2
    assert "#spine:1#" in result.units[0].origin.ref
    assert "#spine:2#" in result.units[1].origin.ref


def test_epub_refuses_package_path_escape(tmp_path):
    path = tmp_path / "escape.epub"
    path.write_bytes(_epub_bytes(href="../../outside.xhtml", duplicate_spine=False))

    with pytest.raises(ValueError, match="unsafe archive member path"):
        extract(path)


def test_magic_beats_text_suffix_and_explicit_language_hint_is_concrete(tmp_path):
    pdf = tmp_path / "document.txt"
    pdf.write_bytes(b"%PDF-1.7\n")
    html = tmp_path / "document.json"
    html.write_text("<!doctype html><html><body>x</body></html>", encoding="utf-8")
    source = tmp_path / "program"
    source.write_text("export const answer = 42;", encoding="utf-8")

    assert detect(pdf).kind == "pdf"
    assert detect(html).kind == "html"
    assert detect(source, kind="javascript").module == "autotldr.extract.code"
    with pytest.raises(ValueError, match="unsupported explicit input type 'source'"):
        detect(source, kind="source")


def test_path_manifest_hashes_the_exact_acquired_bytes(tmp_path):
    path = tmp_path / "fact.txt"
    payload = b"The relay retries exactly twice.\r\n"
    path.write_bytes(payload)

    result = extract(path)
    item = result.meta["inputs"][0]

    assert item == {
        "source": str(path),
        "kind": "text",
        "tier": 0,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert set(result.meta["timings"]) == {"acquisition_ms", "extraction_ms"}
    assert all(value >= 0 for value in result.meta["timings"].values())


def _docx_with_comment() -> bytes:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = f"""<w:document xmlns:w="{namespace}"><w:body><w:p>
<w:commentRangeStart w:id="1"/><w:r><w:t>Anchored fact.</w:t></w:r>
<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r>
</w:p></w:body></w:document>"""
    comments = f"""<w:comments xmlns:w="{namespace}"><w:comment w:id="1">
<w:p><w:r><w:t>Explain it.</w:t></w:r></w:p></w:comment></w:comments>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/comments.xml", comments)
    return buffer.getvalue()


def test_docx_stdin_rebase_rewrites_recursive_anchor_ids_and_gap_origins():
    payload = _docx_with_comment()

    result = extract_stdin(payload, kind="docx")
    units = {unit.origin.ref: unit for unit in result.units}
    paragraph = units["para:1"]
    comment = units["comment:1"]

    assert result.source == "<stdin>"
    assert comment.meta["anchors"] == [paragraph.id]
    assert any(
        relation.src == comment.id and relation.dst == paragraph.id
        for relation in result.relations
    )
    assert all(unit.source == "<stdin>" for unit in result.units)
    assert all(gap.origin.source == "<stdin>" for gap in result.gaps)
    assert "/tmp/" not in repr(result.meta)


@contextmanager
def _server(handler_type):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _LlmsHandler(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).paths.append(self.path)
        if self.path == "/llms.txt":
            body = b"# Canonical\nThe local relay retries twice.\n"
            content_type = "text/plain; charset=utf-8"
        else:
            body = b"<html><body><p>Fallback page must not win.</p></body></html>"
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        pass


def test_url_prefers_same_origin_llms_txt_and_hashes_those_bytes():
    _LlmsHandler.paths = []
    with _server(_LlmsHandler) as origin:
        result = extract_url(origin + "/guide")

    assert _LlmsHandler.paths == ["/llms.txt"]
    assert result.source == origin + "/llms.txt"
    assert result.meta["llms_txt"] == {
        "used": True,
        "url": origin + "/llms.txt",
    }
    assert any("retries twice" in unit.content for unit in result.units)
    assert not any("Fallback page" in unit.content for unit in result.units)
    item = result.meta["inputs"][0]
    expected = b"# Canonical\nThe local relay retries twice.\n"
    assert item["sha256"] == hashlib.sha256(expected).hexdigest()
    assert item["bytes"] == len(expected)


class _BadCharsetHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/llms.txt":
            self.send_response(404)
            self.end_headers()
            return
        body = b"<html><body><p>truth\xff</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=ascii")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        pass


def test_http_html_strictly_honors_declared_charset():
    with _server(_BadCharsetHandler) as origin:
        with pytest.raises(ValueError) as raised:
            extract_url(origin + "/guide")

    message = str(raised.value)
    assert origin + "/guide" in message
    assert "ascii" in message
    assert "byte" in message
    assert "replaced" not in message


class _FtpRedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", "ftp://127.0.0.1/never-opened")
        self.end_headers()

    def log_message(self, _format, *args):
        pass


def test_non_http_redirect_is_rejected_before_target_acquisition():
    with _server(_FtpRedirectHandler) as origin:
        with pytest.raises(ValueError) as raised:
            extract_url(origin + "/guide")

    message = str(raised.value)
    assert "redirect target" in message
    assert "ftp://" in message
    assert "only HTTP(S)" in message


class _RouteHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[str | None, bytes]] = {}

    def do_GET(self):  # noqa: N802
        route = type(self).routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type, body = route
        self.send_response(200)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *args):
        pass


def _remote_notebook_bytes() -> bytes:
    return json.dumps(
        {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"kernelspec": {"name": "python3"}},
            "cells": [
                {
                    "id": "run",
                    "cell_type": "code",
                    "metadata": {},
                    "execution_count": 1,
                    "source": ["print('ready')\n"],
                    "outputs": [
                        {
                            "output_type": "stream",
                            "name": "stdout",
                            "text": ["ready\n"],
                        }
                    ],
                }
            ],
        }
    ).encode("utf-8")


def test_http_native_text_media_and_generic_url_suffixes_reach_native_parsers():
    payloads = {
        "rst": (b"Relay\n=====\n\nUse the relay.\n", "text/x-rst", ".rst"),
        "yaml": (b"relay:\n  enabled: true\n", "application/yaml", ".yaml"),
        "toml": (b"[relay]\nenabled = true\n", "application/toml", ".toml"),
        "jsonl": (b'{"id":1}\n{"id":2}\n', "application/x-ndjson", ".jsonl"),
        "csv": (b"name,value\nalpha,1\n", "text/csv", ".csv"),
        "tsv": (b"name\tvalue\nalpha\t1\n", "text/tab-separated-values", ".tsv"),
        "latex": (b"\\section{Relay}\nUse the relay.\n", "application/x-latex", ".tex"),
        "notebook": (_remote_notebook_bytes(), "application/x-ipynb+json", ".ipynb"),
    }
    routes: dict[str, tuple[str | None, bytes]] = {}
    for kind, (body, media, suffix) in payloads.items():
        # Media identity must beat the deliberately misleading .txt suffix.
        routes[f"/media/{kind}.txt"] = (media + "; charset=utf-8", body)
        # A generic transport type may recover identity from the URL suffix.
        routes[f"/suffix/{kind}{suffix}"] = ("application/octet-stream", body)
    _RouteHandler.routes = routes

    with _server(_RouteHandler) as origin:
        for kind in payloads:
            for route_kind in ("media", "suffix"):
                suffix = ".txt" if route_kind == "media" else payloads[kind][2]
                result = extract_url(f"{origin}/{route_kind}/{kind}{suffix}")
                assert result.kind == kind
                assert result.units
                manifest = result.meta["inputs"][0]
                assert manifest["kind"] == kind
                assert manifest["source"] == result.source
                assert manifest["bytes"] == len(payloads[kind][0])
                assert manifest["sha256"] == hashlib.sha256(
                    payloads[kind][0]
                ).hexdigest()

                if kind in {"csv", "tsv"}:
                    assert any(
                        unit.modality is Modality.SCHEMA
                        for unit in result.units
                    )
                    assert result.relations
                if kind == "notebook":
                    assert any(
                        relation.kind is RelationKind.PRODUCED_BY
                        for relation in result.relations
                    )


def test_http_textual_native_media_honors_charset_but_hashes_wire_bytes():
    payload = "name,value\ncafé,1\n".encode("iso-8859-1")
    _RouteHandler.routes = {
        "/latin.csv": ("text/csv; charset=iso-8859-1", payload),
    }

    with _server(_RouteHandler) as origin:
        result = extract_url(origin + "/latin.csv")

    assert result.kind == "csv"
    assert any("café" in repr(unit.meta) for unit in result.units)
    manifest = result.meta["inputs"][0]
    assert manifest["bytes"] == len(payload)
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()


def test_http_strong_signature_precedes_misleading_media_identity():
    docx = _docx_with_comment()
    html = b"<!doctype html><html><body><p>Native HTML.</p></body></html>"
    _RouteHandler.routes = {
        "/document.pdf": ("application/pdf", docx),
        "/page.png": ("image/png", html),
    }

    with _server(_RouteHandler) as origin:
        document = extract_url(origin + "/document.pdf")
        page = extract_url(origin + "/page.png")

    assert document.kind == "docx"
    assert document.meta["inputs"][0]["kind"] == "docx"
    assert page.kind in {"html", "url"}
    assert any(unit.content == "Native HTML." for unit in page.units)
    assert page.meta["inputs"][0]["kind"] == "html"


def test_http_source_media_and_unknown_text_subtype_suffix_use_native_code():
    javascript = b"export function start(id) { return id; }\n"
    _RouteHandler.routes = {
        "/media/relay.data": ("text/javascript; charset=utf-8", javascript),
        "/suffix/relay.js": ("text/x-vendor-source; charset=utf-8", javascript),
    }

    with _server(_RouteHandler) as origin:
        by_media = extract_url(origin + "/media/relay.data")
        by_suffix = extract_url(origin + "/suffix/relay.js")

    for result in (by_media, by_suffix):
        assert result.kind == "source"
        assert result.meta["inputs"][0]["kind"] == "source"
        assert any(
            unit.meta.get("symbol_kind") == "function"
            and unit.meta.get("symbol") == "start"
            for unit in result.units
        )


def test_embedded_html_tag_in_source_never_overrides_native_code_routing(tmp_path):
    javascript = b'export function render() { return "<body>"; }\n'
    local = tmp_path / "render.js"
    local.write_bytes(javascript)
    _RouteHandler.routes = {
        "/render.data": ("text/javascript; charset=utf-8", javascript),
    }

    local_result = extract(local)
    stdin_result = extract_stdin(javascript, kind="javascript")
    with _server(_RouteHandler) as origin:
        remote_result = extract_url(origin + "/render.data")

    for result in (local_result, stdin_result, remote_result):
        assert result.kind == "source"
        assert result.units
        assert any(
            unit.meta.get("symbol_kind") == "function"
            and unit.meta.get("symbol") == "render"
            for unit in result.units
        )


def test_leading_jsx_component_and_html_root_keep_native_source_identity(tmp_path):
    sources = (
        b"<BodyComponent />;\nexport function render() { return 1; }\n",
        b"<html><body>layout</body></html>;\n",
    )

    for index, jsx in enumerate(sources):
        local = tmp_path / f"layout-{index}.jsx"
        local.write_bytes(jsx)
        _RouteHandler.routes = {
            f"/layout-{index}.data": ("text/jsx; charset=utf-8", jsx),
        }

        local_result = extract(local)
        stdin_result = extract_stdin(jsx, kind="jsx")
        with _server(_RouteHandler) as origin:
            remote_result = extract_url(origin + f"/layout-{index}.data")

        for result in (local_result, stdin_result, remote_result):
            assert result.kind == "source"
            assert result.meta.get("grammar") == "javascript"


@pytest.mark.parametrize(
    "prefix",
    [b"<bodyguard>", b"<htmlish>", b"<!doctype htmlish>"],
)
def test_html_identifier_prefixes_are_not_document_signatures(tmp_path, prefix):
    path = tmp_path / "prefix.txt"
    path.write_bytes(prefix + b" remains plain text\n")

    result = extract(path)

    assert result.kind == "text"


def test_png_url_and_stdin_are_named_tier_four_declines(monkeypatch, capsys):
    png = b"\x89PNG\r\n\x1a\n" + b"opaque-image-data"
    _RouteHandler.routes = {
        # The URL decline is driven by recognized media even without magic.
        "/asset": ("image/png", b"declared-image-payload"),
        # Strong magic independently beats a misleading native text media type.
        "/mislabel.csv": ("text/csv", png),
    }

    with _server(_RouteHandler) as origin:
        with pytest.raises(UnsupportedFormat) as url_decline:
            extract_url(origin + "/asset")
        with pytest.raises(UnsupportedFormat) as magic_decline:
            extract_url(origin + "/mislabel.csv")
        status = main([origin + "/asset"])
    captured = capsys.readouterr()

    assert url_decline.value.kind == "image"
    assert url_decline.value.tier == 4
    assert magic_decline.value.kind == "image"
    assert magic_decline.value.tier == 4
    assert status == EXIT_UNSUPPORTED
    assert captured.out == ""
    assert "image" in captured.err
    assert "tier 4" in captured.err

    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(png), encoding="utf-8"),
    )
    status = main(["-"])
    captured = capsys.readouterr()

    assert status == EXIT_UNSUPPORTED
    assert captured.out == ""
    assert "<stdin>" in captured.err
    assert "image" in captured.err
    assert "tier 4" in captured.err


def test_supported_suffixes_exclude_always_declined_native_grammar_gaps(tmp_path):
    unavailable = {
        ".clj",
        ".cljs",
        ".dart",
        ".fish",
        ".fs",
        ".fsx",
        ".groovy",
        ".lua",
        ".mm",
        ".svelte",
        ".swift",
        ".vb",
        ".vue",
        ".zsh",
    }

    assert unavailable.isdisjoint(supported_suffixes())
    assert unavailable <= declined_suffixes()
    assert "lua" in input_type_names()

    source = tmp_path / "relay.swift"
    source.write_text("struct Relay {}\n", encoding="utf-8")
    assert detect(source).kind == "source"
    with pytest.raises(UnsupportedFormat) as raised:
        extract(source)
    assert raised.value.kind == "Swift source"
    assert raised.value.tier == 0


def test_explicit_unavailable_source_hints_are_named_tier_zero_declines(
    monkeypatch,
    capsys,
):
    known_hints = {
        "swift",
        "zsh",
        "fish",
        "dart",
        "clojure",
        "clojurescript",
        "fsharp",
        "visual-basic",
        "groovy",
        "vue",
        "svelte",
        "lua",
        "objective-c++",
    }
    assert known_hints <= set(input_type_names())

    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(b"struct Relay {}\n"),
            encoding="utf-8",
        ),
    )
    status = main(["-", "--type", "swift"])
    captured = capsys.readouterr()

    assert status == EXIT_UNSUPPORTED
    assert captured.out == ""
    assert "Swift source" in captured.err
    assert "tier 0" in captured.err


def test_avif_ftyp_and_pptx_container_receive_precise_named_declines(tmp_path):
    avif = b"\x00\x00\x00\x14ftypavif\x00\x00\x00\x00avif"
    with pytest.raises(UnsupportedFormat) as avif_decline:
        extract_stdin(avif)
    assert avif_decline.value.kind == "image"
    assert avif_decline.value.tier == 4

    presentation = tmp_path / "slides.bin"
    with zipfile.ZipFile(presentation, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<p:presentation xmlns:p='urn:p'/>")
    with pytest.raises(UnsupportedFormat) as slides_decline:
        extract(presentation)
    assert slides_decline.value.kind == "slides"
    assert slides_decline.value.tier == 4

    with pytest.raises(UnsupportedFormat) as piped_slides_decline:
        extract_stdin(presentation.read_bytes())
    assert piped_slides_decline.value.kind == "slides"
    assert piped_slides_decline.value.tier == 4


def test_short_magic_like_text_is_not_promoted_to_a_binary_signature():
    result = extract_stdin(b"BM is an ordinary pair of initials in this sentence.\n")

    assert result.kind == "text"
    assert any("ordinary pair of initials" in unit.content for unit in result.units)
