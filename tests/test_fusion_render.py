"""Stage 4 representation and renderer integration invariants."""

from __future__ import annotations

import json
import re

import pytest

from autotldr.render import render
from autotldr.unit import (
    Extraction,
    Gap,
    GapKind,
    GroundedStatement,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Role,
    Unit,
)


def _unit(
    source: str,
    ref: str,
    content: str,
    *,
    modality: Modality = Modality.PROSE,
    salience: float = 0.5,
) -> Unit:
    return Unit(
        source=source,
        modality=modality,
        content=content,
        origin=Origin(source, ref),
        role=Role.UNKNOWN,
        salience=salience,
    )


def _collection() -> Extraction:
    paper_anchor = _unit(
        "paper.md",
        "source",
        "Source paper.md",
        modality=Modality.SOURCE,
        salience=1.0,
    )
    data_anchor = _unit(
        "results.csv",
        "source",
        "Source results.csv",
        modality=Modality.SOURCE,
        salience=1.0,
    )
    orphan_anchor = _unit(
        "old-notes.txt",
        "source",
        "Source old-notes.txt",
        modality=Modality.SOURCE,
        salience=0.8,
    )
    paper_fact = _unit(
        "paper.md",
        "line:8",
        "Throughput is recorded as throughput_mbps.",
        salience=0.9,
    )
    data_fact = _unit(
        "results.csv",
        "column:2",
        "throughput_mbps: number; range 91 to 104.",
        modality=Modality.SCHEMA,
        salience=0.9,
    )
    reference = _unit(
        "paper.md",
        "line:14",
        "missing-schema.json",
        modality=Modality.REFERENCE,
        salience=0.4,
    )

    first_claim = GroundedStatement(
        content="The collection connects the paper and results through throughput_mbps.",
        origins=(paper_fact.origin, data_fact.origin),
        evidence_unit_ids=(paper_fact.id, data_fact.id),
    )
    second_claim = GroundedStatement(
        content="One input remains unconnected by the measured fusion signals.",
        origins=(orphan_anchor.origin,),
        evidence_unit_ids=(orphan_anchor.id,),
    )

    return Extraction(
        source="<collection>",
        kind="collection",
        units=[
            paper_anchor,
            data_anchor,
            orphan_anchor,
            paper_fact,
            data_fact,
            reference,
        ],
        relations=[
            Relation(
                paper_fact.id,
                data_fact.id,
                RelationKind.CORRESPONDS,
                evidence=(
                    'fusion:identifier-v1 normalized="throughput_mbps" '
                    'left="throughput_mbps" right="throughput_mbps"'
                ),
                confidence=0.95,
            )
        ],
        gaps=[
            Gap(
                "missing-schema.json is referenced but absent from the collection.",
                reference.origin,
                GapKind.UNRESOLVED_REFERENCE,
            ),
            Gap(
                "No cross-source relation connects old-notes.txt.",
                orphan_anchor.origin,
                GapKind.ORPHAN,
            ),
        ],
        summary_claims=[first_claim, second_claim],
        meta={
            "inputs": [
                {"source": "paper.md", "kind": "markdown", "tier": 0},
                {"source": "results.csv", "kind": "csv", "tier": 0},
                {"source": "old-notes.txt", "kind": "text", "tier": 0},
            ]
        },
    )


def _reported_used(shape: str, text: str) -> int:
    if shape == "json":
        return json.loads(text)["manifest"]["selection"]["used"]
    if shape == "jsonl":
        return json.loads(text.splitlines()[-1])["selection"]["used"]
    if shape == "ansi":
        match = re.search(r"(?m)^(\d+)/unlimited portable tokens", text)
    else:
        match = re.search(
            r"Portable tokens: \*\*(\d+) / unlimited\*\*", text
        )
    assert match is not None
    return int(match.group(1))


def test_stage_4_additive_vocabulary_does_not_promote_roles():
    result = _collection()

    assert Modality.SOURCE.value == "source"
    assert RelationKind.CORRESPONDS.value == "corresponds"
    assert all(unit.role is Role.UNKNOWN for unit in result.units)


def test_grounded_statement_identity_and_constructor_invariants():
    unit = _unit("paper.md", "line:3", "An exact fact.")
    first = GroundedStatement(
        "A derived sentence.", (unit.origin,), (unit.id,)
    )
    second = GroundedStatement(
        "A derived sentence.", (unit.origin,), (unit.id,)
    )

    assert first.id == second.id
    assert len(first.id) == 32
    with pytest.raises(ValueError, match="at least one origin"):
        GroundedStatement("No origin.", (), (unit.id,))
    with pytest.raises(ValueError, match="evidence unit id"):
        GroundedStatement("No evidence.", (unit.origin,), ())
    with pytest.raises(ValueError, match="Unicode surrogate"):
        GroundedStatement("bad\ud800", (unit.origin,), (unit.id,))


def test_json_and_jsonl_keep_structured_claims_and_typed_absences():
    result = _collection()
    expected_summary = " ".join(
        claim.content for claim in result.summary_claims
    )

    payload = json.loads(render(result, output="json"))
    assert payload["schema"] == 2
    assert payload["summary"] == expected_summary
    assert payload["summary_claims"] == [
        {
            "id": claim.id,
            "content": claim.content,
            "origins": [
                {"source": origin.source, "ref": origin.ref}
                for origin in claim.origins
            ],
            "evidence_unit_ids": list(claim.evidence_unit_ids),
        }
        for claim in result.summary_claims
    ]
    assert {gap["kind"] for gap in payload["gaps"]} == {
        "unresolved-reference",
        "orphan",
    }
    orphan = next(gap for gap in payload["gaps"] if gap["kind"] == "orphan")
    assert orphan["origin"] == {
        "source": "old-notes.txt",
        "ref": "source",
    }
    assert payload["relations"][0]["kind"] == "corresponds"
    assert payload["manifest"]["versions"]["representation"] == 2

    records = [json.loads(line) for line in render(result, output="jsonl").splitlines()]
    assert records[0]["schema"] == 2
    assert records[0]["summary"] == expected_summary
    assert records[0]["summary_claims"] == payload["summary_claims"]
    assert records[-1]["gaps"] == payload["gaps"]


@pytest.mark.parametrize("shape", ["md", "ansi"])
def test_human_shapes_cite_every_claim_and_separate_orphans(shape):
    result = _collection()
    text = render(result, output=shape, cite=True)

    for statement in result.summary_claims:
        assert statement.content in text
        for origin in statement.origins:
            assert f"{origin.source}#{origin.ref}" in text
    assert "Orphans" in text
    assert "Gaps" in text
    assert "No cross-source relation connects old-notes.txt." in text
    assert "missing-schema.json is referenced but absent" in text


@pytest.mark.parametrize("shape", ["md", "ansi"])
def test_no_cite_gives_summary_claims_and_findings_source_map_keys(shape):
    result = _collection()
    text = render(result, output=shape, cite=False)

    for statement in result.summary_claims:
        assert f"statement-{statement.id}" in text
        for origin in statement.origins:
            assert f"{origin.source}#{origin.ref}" in text
    for gap in result.gaps:
        # Bundle finding IDs include the finding kind, so recover them from the
        # machine view rather than duplicating that projection implementation.
        payload = json.loads(render(result, output="json"))
        finding = next(item for item in payload["gaps"] if item["content"] == gap.content)
        assert f"gap-{finding['id']}" in text


@pytest.mark.parametrize("shape", ["ansi", "md", "json", "jsonl"])
def test_grounded_claims_and_typed_findings_participate_in_fixed_point(shape):
    text = render(_collection(), output=shape)

    assert _reported_used(shape, text) == len(text.encode("utf-8"))


def test_budget_drop_keeps_mandatory_claim_and_orphan_but_drops_edge_atomically():
    anchor = _unit(
        "paper.md",
        "source",
        "Source paper.md",
        modality=Modality.SOURCE,
        salience=1.0,
    )
    oversized = _unit(
        "results.csv",
        "column:1",
        "oversized: " + ("x" * 20_000),
        modality=Modality.SCHEMA,
        salience=0.0,
    )
    statement = GroundedStatement(
        "The collection contains one addressable paper source.",
        (anchor.origin,),
        (anchor.id,),
    )
    result = Extraction(
        source="<collection>",
        kind="collection",
        units=[anchor, oversized],
        relations=[
            Relation(
                anchor.id,
                oversized.id,
                RelationKind.CORRESPONDS,
                "fusion:structural-v1 field=oversized",
            )
        ],
        gaps=[
            Gap(
                "No cross-source relation connects paper.md after selection.",
                anchor.origin,
                GapKind.ORPHAN,
            )
        ],
        summary_claims=[statement],
    )

    payload = json.loads(render(result, output="json", budget=4096))
    selection = payload["manifest"]["selection"]

    assert [unit["id"] for unit in payload["units"]] == [anchor.id]
    assert payload["relations"] == []
    assert payload["summary_claims"][0]["id"] == statement.id
    assert payload["gaps"][0]["kind"] == "orphan"
    assert selection["dropped"]["relation_count"] == 1
    assert selection["dropped"]["reported_relations"] == [
        {
            "index": 0,
            "src": anchor.id,
            "dst": oversized.id,
            "kind": "corresponds",
            "reason": "budget",
        }
    ]
    assert selection["used"] == len(
        json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    ) + 1


def test_summary_evidence_must_resolve_and_match_cited_origins():
    unit = _unit("paper.md", "line:1", "One fact.")
    missing = GroundedStatement(
        "A bad sentence.", (unit.origin,), ("0" * 32,)
    )
    with pytest.raises(ValueError, match="unresolved evidence"):
        render(
            Extraction(
                source="<collection>",
                kind="collection",
                units=[unit],
                summary_claims=[missing],
            ),
            output="json",
        )

    other_origin = Origin("other.md", "line:2")
    mismatched = GroundedStatement(
        "Another bad sentence.", (other_origin,), (unit.id,)
    )
    with pytest.raises(ValueError, match="origins must exactly match"):
        render(
            Extraction(
                source="<collection>",
                kind="collection",
                units=[unit],
                summary_claims=[mismatched],
            ),
            output="json",
        )


def test_single_source_fallback_summary_and_legacy_string_gap_remain_supported():
    unit = _unit("notes.md", "line:1", "A source claim.")
    result = Extraction(
        source="notes.md",
        kind="markdown",
        units=[unit],
        gaps=["no heading was present"],
    )
    payload = json.loads(render(result, output="json"))

    assert payload["summary"] == (
        "markdown source with 1 addressable semantic unit(s), 0 relation(s), "
        "and 1 reported gap(s)."
    )
    assert payload["summary_claims"] == []
    assert payload["gaps"][0]["kind"] == "extraction"
    assert result.gaps[0] == "no heading was present"

