"""Stage 3's Unix-facing invoke contract.

These tests deliberately exercise the command boundary rather than individual
renderer helpers.  A caller should be able to depend on the same behavior
whether the engine behind it changes or not: stdin/path/URL acquisition,
machine-readable shapes, addressable citations, a hard complete-output budget,
and useful process status.

The Stage 3 counter is ``utf8-byte-v1``: a deliberately conservative,
dependency-free envelope whose scope is every byte written to stdout, including
serialization framing, citations, and the drop report itself.  Units remain
atomic.  The report is kept in the bundle manifest so structured projections
can prove their own accounting.
"""

from __future__ import annotations

import io
import hashlib
import json
import re
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autotldr import cli as cli_module
from autotldr.cli import (
    EXIT_ERROR,
    EXIT_BUDGET,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_UNSUPPORTED,
    main,
)
from autotldr.router import extract


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


SMALL_DOCUMENT = """\
# Relay Notes

The relay opens after the health check passes.

## Caveat

The fallback remains manual.
"""


def _write_document(tmp_path, text: str = SMALL_DOCUMENT):
    path = tmp_path / "relay.md"
    path.write_text(text, encoding="utf-8")
    return path


def _invoke(capsys, args: list[str], *, stdin: str | None = None):
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        status = main(args)
    finally:
        sys.stdin = old_stdin
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_ansi_is_the_default_human_shape(tmp_path, capsys):
    path = _write_document(tmp_path)

    default = _invoke(capsys, [str(path)])
    explicit = _invoke(capsys, [str(path), "--out", "ansi"])

    assert default == explicit
    status, stdout, stderr = default
    assert status == EXIT_OK
    assert stderr == ""
    assert "Relay Notes" in stdout
    assert "The relay opens after the health check passes." in stdout
    assert "#line:" in stdout, "citations are on by default"
    assert not stdout.lstrip().startswith(("{", "[")), "ANSI is a human view"


def test_markdown_is_clean_and_cited(tmp_path, capsys):
    path = _write_document(tmp_path)

    status, stdout, stderr = _invoke(capsys, [str(path), "--out", "md"])

    assert status == EXIT_OK
    assert stderr == ""
    assert stdout.lstrip().startswith("#")
    assert "Relay Notes" in stdout
    assert "The fallback remains manual." in stdout
    assert "#line:" in stdout
    assert "\x1b[" not in stdout, "Markdown must not contain terminal escapes"


def test_json_is_a_complete_addressable_projection(tmp_path, capsys):
    path = _write_document(tmp_path)

    status, stdout, stderr = _invoke(capsys, [str(path), "--out", "json"])
    payload = json.loads(stdout)

    assert status == EXIT_OK
    assert stderr == ""
    assert payload["schema"] == 2
    assert payload["subject"] == str(path)
    assert payload["kind"] == "markdown"
    assert payload["summary"].startswith("markdown source with")
    assert payload["units"]
    assert isinstance(payload["relations"], list)
    assert isinstance(payload["gaps"], list)
    assert isinstance(payload["manifest"], dict)
    manifest = payload["manifest"]
    assert manifest["inputs"] == [
        {
            "source": str(path),
            "kind": "markdown",
            "tier": 0,
            "bytes": len(path.read_bytes()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    ]
    assert set(manifest["timings"]) == {"acquisition_ms", "extraction_ms"}
    assert manifest["versions"]["autotldr"]
    assert manifest["models"] == []
    assert manifest["role_backend"] == "deterministic-rules-v1"
    for unit in payload["units"]:
        assert unit["id"]
        assert unit["content"]
        assert unit["origin"]["source"] == str(path)
        assert unit["origin"]["ref"].startswith("line:")
        assert unit["tokens"] > 0


def test_jsonl_has_one_addressable_unit_per_unit_record(tmp_path, capsys):
    path = _write_document(tmp_path)

    status, stdout, stderr = _invoke(capsys, [str(path), "--out", "jsonl"])
    records = [json.loads(line) for line in stdout.splitlines()]

    assert status == EXIT_OK
    assert stderr == ""
    assert records[0]["type"] == "header"
    assert records[0]["subject"] == str(path)
    assert records[-1]["type"] == "manifest"
    unit_records = records[1:-1]
    assert unit_records
    assert records[0]["units"] == len(unit_records)
    assert all(record["type"] == "unit" for record in unit_records)
    assert all(record["origin"]["ref"].startswith("line:") for record in unit_records)
    assert all("\n" not in json.dumps(record) for record in records)


@pytest.mark.parametrize("shape", ["ansi", "md"])
def test_no_cite_moves_human_provenance_to_a_source_map(tmp_path, capsys, shape):
    """Hiding inline spans must not make a rendered claim unaddressable."""
    path = _write_document(tmp_path)
    units = extract(path).units

    cited = _invoke(capsys, [str(path), "--out", shape, "--cite"])
    uncited = _invoke(capsys, [str(path), "--out", shape, "--no-cite"])

    assert cited[0] == uncited[0] == EXIT_OK
    assert cited[2] == uncited[2] == ""
    assert "Relay Notes" in cited[1] and "Relay Notes" in uncited[1]
    cited_text = ANSI_ESCAPE.sub("", cited[1])
    uncited_text = ANSI_ESCAPE.sub("", uncited[1])
    for unit in units:
        origin = str(unit.origin)
        assert f"[{origin}]" in cited_text
        assert f"[{origin}]" not in uncited_text, "inline span should be hidden"
        assert unit.id[:8] in uncited_text, "the claim must retain its short unit ID"
        assert origin in uncited_text, "the source appendix must resolve that ID"


def test_no_cite_never_strips_origins_from_structured_ir(tmp_path, capsys):
    """Presentation can hide anchors; the addressability invariant cannot."""
    path = _write_document(tmp_path)

    status, stdout, stderr = _invoke(
        capsys, [str(path), "--out", "json", "--no-cite"]
    )
    payload = json.loads(stdout)

    assert status == EXIT_OK
    assert stderr == ""
    assert payload["units"]
    assert all(unit["origin"]["ref"] for unit in payload["units"])


def _large_document(tmp_path):
    oversized = "BEGIN-ATOMIC-UNIT " + ("x" * 20_000) + " END-ATOMIC-UNIT"
    path = _write_document(
        tmp_path,
        f"""\
# Relay

The compact fact survives selection.

## Appendix

{oversized}
""",
    )
    return path, oversized


def _structured_budget_report(shape: str, stdout: str):
    if shape == "json":
        payload = json.loads(stdout)
        return payload["manifest"]["selection"], payload["units"]

    records = [json.loads(line) for line in stdout.splitlines()]
    assert records[0]["type"] == "header"
    assert records[-1]["type"] == "manifest"
    units = [record for record in records if record["type"] == "unit"]
    return records[-1]["selection"], units


@pytest.mark.parametrize("shape", ["json", "jsonl"])
def test_budget_covers_the_complete_parseable_structured_output(
    tmp_path, capsys, shape
):
    path, oversized = _large_document(tmp_path)
    # The complete envelope includes the required input hash/timing manifest
    # and a concrete identity record for every omission.  Six KiB leaves room
    # for the compact semantic units while still forcing the 20 KiB atomic
    # appendix to be dropped.
    requested = 6144

    status, stdout, stderr = _invoke(
        capsys, [str(path), "--out", shape, "--budget", str(requested)]
    )
    report, selected = _structured_budget_report(shape, stdout)
    used = len(stdout.encode("utf-8"))
    dropped = report["dropped"]

    assert status == EXIT_OK
    assert stderr == ""
    assert used <= requested
    assert report["counter"] == "utf8-byte-v1"
    assert report["scope"] == "complete-output"
    assert report["requested"] == requested
    assert report["used"] == used
    assert report["available"] > requested
    assert report["selected_units"] == len(selected)
    assert dropped["unit_count"] >= 1
    assert dropped["relation_count"] >= 0
    assert dropped["reason"] == "budget"
    assert dropped["digest"]
    assert dropped["unlisted"] + len(dropped["reported"]) == dropped["unit_count"]
    assert "The compact fact survives selection." in stdout
    assert oversized not in stdout, "a unit may be kept whole or dropped, never sliced"


@pytest.mark.parametrize("shape", ["ansi", "md"])
def test_human_budget_output_reports_omissions(tmp_path, capsys, shape):
    path, oversized = _large_document(tmp_path)
    requested = 4096

    status, stdout, stderr = _invoke(
        capsys, [str(path), "--out", shape, "--budget", str(requested)]
    )

    assert status == EXIT_OK
    assert stderr == ""
    assert len(stdout.encode("utf-8")) <= requested
    assert "dropped" in stdout.casefold()
    assert "budget" in stdout.casefold()
    assert oversized not in stdout


@pytest.mark.parametrize("shape", ["ansi", "md", "json", "jsonl"])
def test_impossible_budget_has_a_distinct_exit_and_never_emits_a_partial_shape(
    tmp_path, capsys, shape
):
    path = _write_document(tmp_path)

    status, stdout, stderr = _invoke(
        capsys, [str(path), "--out", shape, "--budget", "1"]
    )

    assert status == EXIT_BUDGET
    assert stdout == ""
    assert "budget" in stderr.casefold()
    assert "1" in stderr


def test_dash_reads_utf8_text_from_stdin_without_a_temporary_source(capsys):
    status, stdout, stderr = _invoke(
        capsys,
        ["-", "--out", "json"],
        stdin="Piped input\n\nA fact arriving through a pipe.\n",
    )
    payload = json.loads(stdout)

    assert status == EXIT_OK
    assert stderr == ""
    assert payload["subject"] == "<stdin>"
    assert payload["kind"] == "text"
    assert any("through a pipe" in unit["content"] for unit in payload["units"])
    assert all(unit["origin"]["source"] == "<stdin>" for unit in payload["units"])
    assert all(unit["origin"]["ref"].startswith("line:") for unit in payload["units"])


def test_stdin_confidently_sniffs_json_and_rebases_native_origins(capsys):
    status, stdout, stderr = _invoke(
        capsys,
        ["-", "--out", "json"],
        stdin='{"workers":[{"name":"alpha"},{"name":"beta"}]}',
    )
    payload = json.loads(stdout)

    assert status == EXIT_OK
    assert stderr == ""
    assert payload["subject"] == "<stdin>"
    assert payload["kind"] == "json"
    assert payload["units"]
    assert all(unit["origin"]["source"] == "<stdin>" for unit in payload["units"])
    ids = {unit["id"] for unit in payload["units"]}
    assert all(
        relation["src"] in ids and relation["dst"] in ids
        for relation in payload["relations"]
    )


@pytest.mark.parametrize("shape", ["json", "jsonl"])
def test_unlimited_machine_output_self_reports_its_exact_wire_size(
    tmp_path, capsys, shape
):
    path = _write_document(tmp_path)

    status, stdout, stderr = _invoke(capsys, [str(path), "--out", shape])
    if shape == "json":
        report = json.loads(stdout)["manifest"]["selection"]
    else:
        report = json.loads(stdout.splitlines()[-1])["selection"]

    assert status == EXIT_OK
    assert stderr == ""
    assert report["requested"] is None
    assert report["used"] == report["available"] == len(stdout.encode("utf-8"))


def test_output_file_is_canonical_utf8_and_leaves_stdout_clean(tmp_path, capsys):
    path = _write_document(tmp_path)
    destination = tmp_path / "relay.tldr.json"

    status, stdout, stderr = _invoke(
        capsys,
        [str(path), "--out", "json", "--output", str(destination)],
    )
    payload_bytes = destination.read_bytes()
    payload = json.loads(payload_bytes)

    assert status == EXIT_OK
    assert stdout == ""
    assert stderr == ""
    assert payload["manifest"]["selection"]["used"] == len(payload_bytes)


def test_non_http_url_scheme_is_named_instead_of_treated_as_a_path(capsys):
    status, stdout, stderr = _invoke(capsys, ["ftp://example.test/guide"])

    assert status == EXIT_ERROR
    assert stdout == ""
    assert "unsupported URL scheme" in stderr
    assert "ftp" in stderr


@contextmanager
def _html_server():
    document = b"""<!doctype html>
<html><head><title>Relay Guide</title></head><body>
<nav>Discard this navigation chrome.</nav>
<main><h1 id="relay">Relay Guide</h1>
<p>The local relay retries exactly twice.</p></main>
</body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, _format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/guide"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_url_fetches_tier_one_html_and_preserves_url_origins(capsys):
    with _html_server() as url:
        status, stdout, stderr = _invoke(capsys, [url, "--out", "json"])
    payload = json.loads(stdout)

    assert status == EXIT_OK
    assert stderr == ""
    assert payload["subject"] == url
    assert payload["kind"] in {"html", "url"}
    assert any("retries exactly twice" in unit["content"] for unit in payload["units"])
    assert not any("navigation chrome" in unit["content"] for unit in payload["units"])
    assert all(unit["origin"]["source"] == url for unit in payload["units"])
    assert all(unit["origin"]["ref"].startswith(f"{url}#") for unit in payload["units"])


def test_missing_path_has_a_named_status_and_no_stdout(tmp_path, capsys):
    missing = tmp_path / "not-here.md"

    status, stdout, stderr = _invoke(capsys, [str(missing)])

    assert status == EXIT_NOT_FOUND
    assert stdout == ""
    assert missing.name in stderr
    assert "no such file" in stderr.casefold()


def test_usage_errors_do_not_collide_with_runtime_declines(capsys):
    with pytest.raises(SystemExit) as caught:
        main([])
    capsys.readouterr()

    assert caught.value.code == 2
    assert EXIT_UNSUPPORTED != 2
    assert EXIT_NOT_FOUND != 2
    assert EXIT_BUDGET != 2


def test_known_unsupported_format_names_format_and_tier(tmp_path, capsys):
    image = tmp_path / "diagram.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    status, stdout, stderr = _invoke(capsys, [str(image)])

    assert status == EXIT_UNSUPPORTED
    assert stdout == ""
    assert image.name in stderr
    assert "image" in stderr.casefold()
    assert "tier 4" in stderr.casefold()


def test_unknown_format_is_not_an_empty_success(tmp_path, capsys):
    unknown = tmp_path / "payload.wat"
    unknown.write_bytes(b"\x00\x01\x02unknown")

    status, stdout, stderr = _invoke(capsys, [str(unknown)])

    assert status == EXIT_UNSUPPORTED
    assert stdout == ""
    assert unknown.name in stderr
    assert "unrecognized format" in stderr.casefold()


def test_io_failure_is_reported_without_a_traceback(tmp_path, capsys, monkeypatch):
    path = _write_document(tmp_path)

    def fail_read(_path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("autotldr.router.extract", fail_read)
    status, stdout, stderr = _invoke(capsys, [str(path)])

    assert status == EXIT_ERROR
    assert stdout == ""
    assert path.name in stderr
    assert "permission denied" in stderr.casefold()
    assert "traceback" not in stderr.casefold()


@pytest.mark.parametrize("budget", ["0", "-1"])
def test_nonpositive_budget_is_a_cli_usage_error(tmp_path, capsys, budget):
    path = _write_document(tmp_path)

    with pytest.raises(SystemExit) as raised:
        main([str(path), "--budget", budget])
    captured = capsys.readouterr()

    assert raised.value.code == 2
    assert captured.out == ""
    assert "--budget" in captured.err
    assert "positive" in captured.err.casefold()


def test_exit_categories_are_distinct():
    assert cli_module.EXIT_BUDGET == EXIT_BUDGET
    assert len(
        {EXIT_OK, EXIT_ERROR, EXIT_UNSUPPORTED, EXIT_NOT_FOUND, EXIT_BUDGET}
    ) == 5
    assert EXIT_OK == 0
    assert all(
        code > 0
        for code in (EXIT_ERROR, EXIT_UNSUPPORTED, EXIT_NOT_FOUND, EXIT_BUDGET)
    )
