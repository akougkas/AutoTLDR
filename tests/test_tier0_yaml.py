"""YAML gets a real, lazy parser rather than an indented-JSON approximation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from autotldr.router import extract
from autotldr.unit import Role


def test_yaml_emits_schema_paths_with_parser_marks(tmp_path):
    path = tmp_path / "service.yaml"
    path.write_text(
        """service:
  port: 1234
  enabled: true
workers:
  - name: alpha
    retries: 2
  - name: beta
    retries: null
""",
        encoding="utf-8",
    )

    result = extract(path)
    by_path = {unit.meta["schema_path"]: unit for unit in result.units}

    assert result.kind == "yaml"
    assert {"$", "$/service/port", "$/workers/*/name", "$/workers/*/retries"} <= by_path.keys()
    assert by_path["$/service/port"].meta["types"] == ["integer"]
    assert by_path["$/workers/*/retries"].meta["types"] == ["integer", "null"]
    assert all("#path:$" in unit.origin.ref for unit in result.units)
    assert all(unit.role is Role.UNKNOWN for unit in result.units)
    assert all(relation.src and relation.dst for relation in result.relations)


def test_invalid_yaml_is_a_named_data_error(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("key: [unterminated\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid YAML.*line"):
        extract(path)


def test_markdown_path_does_not_import_yaml(md_file):
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; from pathlib import Path; from autotldr.router import extract; "
        f"extract(Path({str(md_file)!r})); print('yaml' in sys.modules)"
    )
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "False"
