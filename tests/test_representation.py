"""Stage 1's gate: does one representation survive three dissimilar formats?

The three sources are chosen to be as unlike each other as v1's scope allows.
A markdown document is prose with an explicit outline. A spreadsheet is a graph
of typed cells with no prose at all. A PDF is prose with no structure except what
font sizes imply.

If the same Unit, Origin and Relation types hold all three without special-casing
at the consumer, the design is sound. If they do not, this is the cheapest
possible moment to find out.
"""

from __future__ import annotations

import json

import pytest

from autotldr.render import to_json, to_jsonl
from autotldr.router import extract
from autotldr.unit import (
    Extraction,
    GroundedStatement,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Role,
    Unit,
)


def test_role_taxonomy_is_the_stage_2_recoverable_subset():
    assert tuple(role.value for role in Role) == (
        "unknown",
        "definition",
        "procedure",
        "caveat",
        "example",
        "decision",
        "assumption",
        "limitation",
    )


class TestMarkdown:
    def test_recovers_heading_hierarchy(self, md_file):
        result = extract(md_file)
        structures = {u.structure for u in result.units if u.structure}
        assert ("Throughput Study", "Method") in structures
        assert ("Throughput Study", "Results") in structures

    def test_code_fence_is_code_not_prose(self, md_file):
        code = result_units(md_file, Modality.CODE)
        assert len(code) == 1
        assert "def measure_throughput" in code[0].content
        assert code[0].role is Role.UNKNOWN
        assert code[0].meta["example_cue"] is True

    def test_hash_inside_fence_is_not_a_heading(self, tmp_path):
        path = tmp_path / "f.md"
        path.write_text("# Real\n\n```sh\n# not a heading\necho hi\n```\n", encoding="utf-8")
        headings = [u for u in extract(path).units if u.meta.get("heading_level")]
        assert [u.content for u in headings] == ["Real"]

    def test_finds_outbound_references(self, md_file):
        targets = {u.meta["target"] for u in result_units(md_file, Modality.REFERENCE)}
        # Fusion signal 1: a link, a bare path, and a URL.
        assert "results.csv" in targets
        assert "bench/measure.py" in targets
        assert any(t.startswith("https://example.org") for t in targets)

    def test_decimal_literals_are_not_filenames(self, tmp_path):
        path = tmp_path / "measurements.md"
        path.write_text(
            "cutoff_voltage_v = 2.80\n\nRead limits.toml for the limit.\n",
            encoding="utf-8",
        )

        targets = {
            unit.meta["target"]
            for unit in result_units(path, Modality.REFERENCE)
        }

        assert targets == {"limits.toml"}

    def test_url_subpaths_are_not_second_local_references(self, tmp_path):
        path = tmp_path / "sources.md"
        path.write_text(
            "The DOI https://doi.org/10.5555/example.external is external.\n",
            encoding="utf-8",
        )

        references = result_units(path, Modality.REFERENCE)

        assert [(unit.meta["ref_kind"], unit.meta["target"]) for unit in references] == [
            ("url", "https://doi.org/10.5555/example.external")
        ]

    def test_caveat_cue_is_preserved_without_promoting_role(self, md_file):
        caveats = [u for u in extract(md_file).units if u.meta.get("caveat_cue")]
        assert any("power analysis" in u.content for u in caveats)
        assert all(u.role is Role.UNKNOWN for u in caveats)

    def test_hard_wrapped_paragraphs_are_unwrapped(self, md_file):
        # A wrap point is not meaning. Leaving it in breaks every phrase and
        # identifier match fusion depends on.
        units = extract(md_file).units
        assert any("power analysis" in u.content for u in units)
        assert any(u.meta.get("caveat_cue") and "power analysis" in u.content for u in units)

    def test_list_structure_survives_unwrapping(self, md_file):
        procedures = [u for u in extract(md_file).units if u.meta.get("procedure_cue")]
        assert procedures, "numbered list was not recognized"
        assert all(u.role is Role.UNKNOWN for u in procedures)
        assert procedures[0].content.count("\n") == 2, "list items were joined together"

    def test_headings_preserve_definition_cues_without_promoting_roles(self, md_file):
        headings = [u for u in extract(md_file).units if u.meta.get("heading")]
        assert headings
        assert any(u.meta.get("definition_cue") for u in headings)
        assert all(u.role is Role.UNKNOWN for u in headings)

    def test_definition_syntax_is_preserved_as_a_cue(self, md_file):
        definitions = [u for u in extract(md_file).units if u.meta.get("definition_cue")]
        assert any("middle value" in u.content for u in definitions)
        assert all(u.role is Role.UNKNOWN for u in definitions)

    def test_origin_char_span_round_trips_to_the_source(self, md_file):
        text = md_file.read_text(encoding="utf-8")
        for unit in extract(md_file).units:
            if unit.modality is Modality.REFERENCE:
                continue
            lo, hi = unit.origin.char_span
            excerpt = " ".join(text[lo:hi].split())
            # The whole point of char_span: a citation can be checked by
            # re-reading the source, not merely believed. Content is normalized
            # for prose, so the comparison normalizes the excerpt the same way.
            body = " ".join(unit.content.split())
            assert body in excerpt, f"{unit.origin.ref} does not contain its own content"


class TestSpreadsheet:
    def test_reads_the_formula_graph_not_the_cells(self, xlsx_file):
        result = extract(xlsx_file)
        derives = [r for r in result.relations if r.kind is RelationKind.DERIVES_FROM]
        assert derives, "no dependency edges recovered"

        by_id = {u.id: u for u in result.units}
        edges = {
            (by_id[r.src].meta.get("cell"), by_id[r.dst].meta.get("cell"))
            for r in derives
        }
        assert ("Model!B7", "Model!B2") in edges
        assert ("Model!B8", "Model!B7") in edges

    def test_separates_inputs_from_derived_values(self, xlsx_file):
        units = extract(xlsx_file).units
        inputs = {u.meta["cell"] for u in units if u.meta.get("input")}
        derived = {u.meta["cell"] for u in units if u.meta.get("formula")}
        assert {"Model!B2", "Model!B3", "Model!B4"} <= inputs
        assert {"Model!B7", "Model!B8", "Model!B9"} <= derived
        assert not inputs & derived

        # RESULT did not survive Stage 2. Formula identity remains explicit
        # rather than being hidden in an unreliable role tag.
        formula_units = [u for u in units if u.meta.get("formula")]
        assert all(u.role is Role.UNKNOWN for u in formula_units)
        assert all(u.meta.get("derived") is True for u in formula_units)

    def test_labels_inputs_from_the_cell_to_their_left(self, xlsx_file):
        by_cell = {u.meta.get("cell"): u for u in extract(xlsx_file).units}
        assert by_cell["Model!B2"].meta["label"] == "Node count"
        assert by_cell["Model!B3"].meta["label"] == "Per-node throughput (MB/s)"

    def test_surfaces_the_number_buried_in_a_formula(self, xlsx_file):
        result = extract(xlsx_file)
        assert any("0.87" in gap for gap in result.gaps), (
            "the hardcoded override in B9 was not reported"
        )
        finding = next(gap for gap in result.gaps if "0.87" in gap)
        assert finding.origin.source == str(xlsx_file)
        assert finding.origin.ref == "Model!B9"

    def test_follows_cross_sheet_references(self, xlsx_file):
        result = extract(xlsx_file)
        by_id = {u.id: u for u in result.units}
        edges = {(by_id[r.src].meta.get("cell"), by_id[r.dst].meta.get("cell")) for r in result.relations}
        assert ("Summary!B1", "Model!B9") in edges

    def test_terminal_outputs_are_marked(self, xlsx_file):
        terminals = {
            u.meta["cell"] for u in extract(xlsx_file).units if u.meta.get("terminal")
        }
        assert "Summary!B1" in terminals
        assert "Model!B7" not in terminals

    def test_sheet_summaries_preserve_definition_cues(self, xlsx_file):
        summaries = [u for u in extract(xlsx_file).units if u.meta.get("sheet_summary")]
        assert {u.meta["sheet"] for u in summaries} == {"Model", "Summary"}
        assert all(u.meta.get("definition_cue") is True for u in summaries)
        assert all(u.role is Role.UNKNOWN for u in summaries)


class TestPdf:
    def test_headings_come_from_relative_font_size(self, pdf_file):
        headings = [u for u in extract(pdf_file).units if u.meta.get("heading")]
        assert "Throughput Under Contention" in {u.content for u in headings}
        assert all(u.meta.get("definition_cue") is True for u in headings)
        assert all(u.role is Role.UNKNOWN for u in headings)

    def test_units_carry_page_origins(self, pdf_file):
        refs = [u.origin.ref for u in extract(pdf_file).units]
        assert all(r.startswith("page:") for r in refs)
        assert any(r.startswith("page:2") for r in refs)

    def test_cue_words_are_preserved_without_promoting_roles(self, pdf_file):
        units = extract(pdf_file).units
        caveats = [u for u in units if u.meta.get("caveat_cue")]
        assert any("does not isolate" in u.content for u in caveats)
        assert all(u.role is Role.UNKNOWN for u in caveats)
        examples = [u for u in units if u.meta.get("example_cue")]
        assert any("Figure 1" in u.content for u in examples)
        assert all(u.role is Role.UNKNOWN for u in examples)
        findings = [u for u in units if u.meta.get("result_cue")]
        assert any("12 percent" in u.content for u in findings)
        assert all(u.role is Role.UNKNOWN for u in findings)

    def test_declines_a_scanned_pdf_by_name(self, scanned_pdf):
        result = extract(scanned_pdf)
        assert result.units == []
        assert any("scanned" in gap and "tier 4" in gap for gap in result.gaps)


class TestOneRepresentation:
    """The actual Stage 1 gate."""

    def test_all_three_produce_the_same_shape(self, md_file, xlsx_file, pdf_file):
        for path in (md_file, xlsx_file, pdf_file):
            result = extract(path)
            assert result.units, f"{path.name} produced nothing"
            for unit in result.units:
                assert isinstance(unit.modality, Modality)
                assert isinstance(unit.role, Role)
                assert unit.role in {Role.UNKNOWN, Role.ASSUMPTION}
                assert unit.origin.source == str(path)
                assert unit.origin.ref
                assert unit.tokens > 0
                assert 0.0 <= unit.salience <= 1.0

    def test_every_relation_endpoint_resolves(self, md_file, xlsx_file, pdf_file):
        for path in (md_file, xlsx_file, pdf_file):
            result = extract(path)
            ids = {u.id for u in result.units}
            for rel in result.relations:
                assert rel.src in ids, f"{path.name}: dangling relation source"
                assert rel.dst in ids, f"{path.name}: dangling relation target"

    def test_unit_ids_are_stable_across_runs(self, md_file, xlsx_file):
        for path in (md_file, xlsx_file):
            first = [u.id for u in extract(path).units]
            second = [u.id for u in extract(path).units]
            assert first == second
            assert len(set(first)) == len(first), f"{path.name}: duplicate unit ids"

    def test_semantically_distinct_units_at_one_origin_have_distinct_ids(self):
        source = "https://example.test/#only-link"
        origin = Origin(source, source)
        prose = Unit(source, Modality.PROSE, source, origin)
        reference = Unit(source, Modality.REFERENCE, source, origin)

        assert prose.id != reference.id

    def test_legacy_string_gaps_are_upgraded_to_addressed_findings(self):
        result = Extraction(source="notes.txt", kind="text")
        result.gaps.append("no headings were present")

        assert result.gaps[0].content == "no headings were present"
        assert result.gaps[0].origin == Origin("notes.txt", "source")

    def test_semantic_identity_fields_reject_whitespace_only_values(self):
        with pytest.raises(ValueError, match="origin ref"):
            Origin("notes.txt", " \t")
        with pytest.raises(ValueError, match="unit content"):
            Unit(
                "notes.txt",
                Modality.PROSE,
                " \n",
                Origin("notes.txt", "line:1"),
            )
        with pytest.raises(ValueError, match="extraction source and kind"):
            Extraction(source="notes.txt", kind="   ")
        with pytest.raises(ValueError, match="relation endpoints"):
            Relation(" ", "unit-id", RelationKind.DESCRIBES)
        with pytest.raises(ValueError, match="evidence unit ids"):
            GroundedStatement(
                content="Grounded claim.",
                origins=(Origin("notes.txt", "line:1"),),
                evidence_unit_ids=(" \t",),
            )

    def test_origin_schemes_differ_per_format(self, md_file, xlsx_file, pdf_file):
        schemes = {
            path.suffix: {u.origin.scheme for u in extract(path).units}
            for path in (md_file, xlsx_file, pdf_file)
        }
        # Format-specific addressing is the design, not an inconsistency.
        assert schemes[".md"] == {"line"}
        assert schemes[".pdf"] == {"page"}
        assert schemes[".xlsx"] == {"opaque"}  # A1 notation is already canonical

    def test_renders_to_json_and_jsonl_without_special_casing(self, md_file, xlsx_file, pdf_file):
        for path in (md_file, xlsx_file, pdf_file):
            result = extract(path)

            payload = json.loads(to_json(result))
            assert payload["schema"] == 2
            assert len(payload["units"]) == len(result.units)

            lines = to_jsonl(result).strip().splitlines()
            header = json.loads(lines[0])
            assert header["type"] == "header"
            manifest = json.loads(lines[-1])
            assert manifest["type"] == "manifest"
            assert len(lines) - 2 == len(result.units)
            for line in lines[1:-1]:
                assert json.loads(line)["type"] == "unit"


def result_units(path, modality):
    return [u for u in extract(path).units if u.modality is modality]
