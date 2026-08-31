"""Process-level checks for bytes and status that in-process capture can hide."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_stdout_is_canonical_utf8_even_when_pythonioencoding_disagrees(tmp_path):
    source = tmp_path / "unicode.md"
    source.write_text("# Café 🧪\n\nA naïve but addressable claim.\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-16"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autotldr.cli",
            str(source),
            "--out",
            "json",
            "--budget",
            "4096",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert not completed.stdout.startswith((b"\xff\xfe", b"\xfe\xff"))
    payload = json.loads(completed.stdout.decode("utf-8"))
    selection = payload["manifest"]["selection"]
    assert selection["used"] == len(completed.stdout)
    assert len(completed.stdout) <= selection["requested"]
    assert any("Café" in unit["content"] for unit in payload["units"])


def test_explicit_type_routes_a_mislabeled_local_source(tmp_path):
    source = tmp_path / "relay.data"
    source.write_text(
        "export interface Relay { start(id: string): void; }\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autotldr.cli",
            str(source),
            "--type",
            "typescript",
            "--out",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    payload = json.loads(completed.stdout)
    assert payload["kind"] == "source"
    assert payload["manifest"]["inputs"][0]["kind"] == "source"
    assert any(
        unit.get("meta", {}).get("symbol_kind") == "interface"
        for unit in payload["units"]
    )
    assert all(unit["origin"]["source"] == str(source) for unit in payload["units"])


def test_explicit_language_hint_parses_stdin_without_a_temp_origin(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autotldr.cli",
            "-",
            "--type",
            "javascript",
            "--out",
            "json",
        ],
        input=b"export function start(id) { return id; }\n",
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    payload = json.loads(completed.stdout)
    assert payload["subject"] == "<stdin>"
    assert payload["manifest"]["inputs"][0]["source"] == "<stdin>"
    assert payload["units"]
    assert all(unit["origin"]["source"] == "<stdin>" for unit in payload["units"])
    assert all("tmp" not in unit["origin"]["source"] for unit in payload["units"])
