"""The locked Stage 3 Tier 0/1 inventory is routed, not merely documented."""

from __future__ import annotations

import zipfile

import pytest

from autotldr.router import UnsupportedFormat, detect, extract, supported_suffixes


@pytest.mark.parametrize(
    ("suffix", "kind"),
    [
        (".txt", "text"),
        (".md", "markdown"),
        (".rst", "rst"),
        (".py", "python"),
        (".js", "source"),
        (".json", "json"),
        (".jsonl", "jsonl"),
        (".yaml", "yaml"),
        (".toml", "toml"),
        (".xml", "xml"),
        (".csv", "csv"),
        (".tsv", "tsv"),
    ],
)
def test_locked_tier_zero_suffixes_have_native_routes(tmp_path, suffix, kind):
    path = tmp_path / f"input{suffix}"
    path.write_bytes(b"")

    handler = detect(path)

    assert handler.kind == kind
    assert handler.tier == 0
    assert suffix in supported_suffixes()


@pytest.mark.parametrize(
    ("suffix", "kind"),
    [
        (".pdf", "pdf"),
        (".docx", "docx"),
        (".html", "html"),
        (".ipynb", "notebook"),
        (".tex", "latex"),
        (".epub", "epub"),
    ],
)
def test_locked_tier_one_suffixes_have_native_routes(tmp_path, suffix, kind):
    path = tmp_path / f"input{suffix}"
    path.write_bytes(b"")

    handler = detect(path)

    assert handler.kind == kind
    assert handler.tier == 1
    assert suffix in supported_suffixes()


def test_extensionless_json_is_sniffed_and_rebased_to_real_source(tmp_path):
    path = tmp_path / "manifest"
    path.write_text('{"workers":[{"name":"alpha"}]}', encoding="utf-8")

    result = extract(path)

    assert result.kind == "json"
    assert result.units
    assert all(unit.source == str(path) for unit in result.units)
    assert all(unit.origin.source == str(path) for unit in result.units)
    ids = {unit.id for unit in result.units}
    assert all(relation.src in ids and relation.dst in ids for relation in result.relations)


def test_extensionless_zip_is_identified_by_native_container_members(tmp_path):
    docx = tmp_path / "document"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
    workbook = tmp_path / "workbook"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    archive_path = tmp_path / "archive"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("file.txt", "text")

    assert detect(docx).kind == "docx"
    assert detect(workbook).kind == "xlsx"
    with pytest.raises(UnsupportedFormat, match="archive input is tier 2"):
        detect(archive_path)


@pytest.mark.parametrize(
    ("suffix", "kind"),
    [
        (".xlsx", "xlsx"),
        (".parquet", "parquet"),
        (".sqlite", "sqlite"),
        (".duckdb", "duckdb"),
        (".h5", "hdf5"),
        (".nc", "netcdf"),
    ],
)
def test_locked_tier_three_suffixes_have_native_routes(tmp_path, suffix, kind):
    path = tmp_path / f"data{suffix}"
    path.write_bytes(b"")

    handler = detect(path)

    assert handler.kind == kind
    assert handler.tier == 3
    assert suffix in supported_suffixes()


def test_ambiguous_database_suffix_still_declines_without_native_signature(tmp_path):
    path = tmp_path / "ambiguous.db"
    path.write_bytes(b"not a database")

    with pytest.raises(UnsupportedFormat, match="database input is tier 3"):
        detect(path)
