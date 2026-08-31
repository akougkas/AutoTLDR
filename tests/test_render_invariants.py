"""Adversarial closure tests for Stage 3's four serialization boundaries."""

from __future__ import annotations

import hashlib
import json
import re

import pytest

import autotldr.render as render_module
from autotldr.render import BudgetTooSmall, render
from autotldr.unit import (
    Extraction,
    GroundedStatement,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Unit,
)


def _unit(
    number: int,
    content: str,
    *,
    salience: float = 0.5,
    source: str = "source.md",
) -> Unit:
    return Unit(
        source=source,
        modality=Modality.PROSE,
        content=content,
        origin=Origin(source, f"line:{number}"),
        salience=salience,
    )


def _selection(shape: str, text: str) -> dict:
    if shape == "json":
        return json.loads(text)["manifest"]["selection"]
    if shape == "jsonl":
        return json.loads(text.splitlines()[-1])["selection"]

    if shape == "ansi":
        match = re.search(r"(?m)^(\d+)/(?:\d+|unlimited) portable tokens", text)
    else:
        match = re.search(
            r"Portable tokens: \*\*(\d+) / (?:\d+|unlimited)\*\*", text
        )
    assert match is not None
    return {"used": int(match.group(1))}


@pytest.mark.parametrize("shape", ["ansi", "md"])
def test_a_fitting_complete_human_projection_beats_larger_drop_envelope(shape):
    first = _unit(1, "naïve café 🧪", salience=0.9, source="源.md")
    second = _unit(2, "a second compact fact", salience=0.4, source="源.md")
    result = Extraction(
        source="源.md",
        kind="markdown",
        units=[first, second],
        relations=[
            Relation(
                first.id,
                second.id,
                RelationKind.DESCRIBES,
                evidence="section containment",
            )
        ],
    )
    unlimited = render(result, output=shape)
    ceiling = len(unlimited.encode("utf-8"))

    bounded = render(result, output=shape, budget=ceiling)

    assert len(bounded.encode("utf-8")) <= ceiling
    assert first.content in bounded and second.content in bounded
    assert "dropped" in bounded.casefold() and "0 units" in bounded.casefold()


def test_one_unit_boundary_can_fit_even_when_empty_drop_envelope_cannot():
    units = [
        _unit(
            index + 1,
            f"{index:02d}" + ("x" * (18 if index == 0 else 498)),
            salience=1 - index / 20,
            source="s",
        )
        for index in range(8)
    ]
    result = Extraction(source="s", kind="text", units=units)
    options = render_module._RenderOptions("ansi", True, False, 2)
    available, _ = render_module._settle_available(
        result, set(range(len(units))), options
    )
    empty = render_module._settle_used(
        result, set(), options, requested=1000, available=available
    )
    one = render_module._settle_used(
        result, {0}, options, requested=1000, available=available
    )
    ceiling = len(one.encode("utf-8"))
    assert ceiling < len(empty.encode("utf-8")), "fixture must cover the boundary"

    bounded = render(result, output="ansi", budget=ceiling)

    assert len(bounded.encode("utf-8")) <= ceiling
    assert units[0].content in bounded
    assert units[1].content not in bounded


@pytest.mark.parametrize("shape", ["ansi", "md", "json", "jsonl"])
def test_budget_too_small_suggestion_is_retryable(shape):
    result = Extraction(
        source="retry.md",
        kind="markdown",
        units=[_unit(1, "one exact fact", source="retry.md")],
        gaps=["no rationale was documented"],
    )

    with pytest.raises(BudgetTooSmall) as raised:
        render(result, output=shape, budget=1)

    retried = render(result, output=shape, budget=raised.value.required)
    assert len(retried.encode("utf-8")) <= raised.value.required


@pytest.mark.parametrize(
    ("shape", "cite", "color"),
    [
        ("ansi", True, False),
        ("ansi", False, True),
        ("md", True, False),
        ("md", False, False),
        ("json", True, False),
        ("jsonl", True, False),
    ],
)
def test_fixed_point_counts_every_unicode_and_ansi_output_byte(shape, cite, color):
    first = _unit(1, "combining e\u0301 and emoji 🧪", salience=0.9, source="源.md")
    second = _unit(2, "another fact", source="源.md")
    result = Extraction(
        source="源.md",
        kind="markdown",
        units=[first, second],
        relations=[Relation(first.id, second.id, RelationKind.DESCRIBES, "edge 🧪")],
        gaps=["β has no documented rationale"],
    )
    unlimited = render(result, output=shape, cite=cite, color=color)
    budget = len(unlimited.encode("utf-8")) + 32

    bounded = render(
        result,
        output=shape,
        cite=cite,
        color=color,
        budget=budget,
    )
    actual = len(bounded.encode("utf-8"))

    assert actual <= budget
    assert _selection(shape, bounded)["used"] == actual
    assert "source with" in bounded


def test_every_dropped_unit_and_relation_has_a_concrete_identity_record():
    kept = _unit(1, "the compact fact", salience=1.0)
    omitted = _unit(2, "x" * 20_000, salience=0.0)
    edge = Relation(
        kept.id,
        omitted.id,
        RelationKind.DESCRIBES,
        evidence="the compact fact heads the oversized appendix",
        confidence=0.875,
    )
    result = Extraction(
        source="source.md",
        kind="markdown",
        units=[kept, omitted],
        relations=[edge],
    )

    payload = json.loads(render(result, output="json", budget=4096))
    dropped = payload["manifest"]["selection"]["dropped"]

    assert payload["summary"] == (
        "markdown source with 2 addressable semantic unit(s), 1 relation(s), "
        "and 0 reported gap(s)."
    )
    assert payload["manifest"]["models"] == []
    assert [unit["id"] for unit in payload["units"]] == [kept.id]
    assert payload["relations"] == []
    assert dropped["unit_count"] == len(dropped["reported"]) == 1
    assert dropped["unlisted"] == 0
    assert dropped["reported"] == [
        {
            "id": omitted.id,
            "origin": {"source": "source.md", "ref": "line:2"},
            "reason": "budget",
        }
    ]
    assert dropped["relation_count"] == len(dropped["reported_relations"]) == 1
    assert dropped["unlisted_relations"] == 0
    assert dropped["reported_relations"] == [
        {
            "index": 0,
            "src": kept.id,
            "dst": omitted.id,
            "kind": "describes",
            "reason": "budget",
        }
    ]


@pytest.mark.parametrize("shape", ["ansi", "md"])
def test_human_drop_wire_carries_complete_safe_canonical_records(shape):
    kept = Unit(
        source="kept.md",
        modality=Modality.PROSE,
        content="the compact fact survives",
        origin=Origin("kept.md", "line:1", (0, 25)),
        salience=1.0,
    )
    unsafe_source = "源`\x1b\x7f\u202e🧪.md"
    unsafe_ref = "line:`\x07\u2066é"
    spanned = Unit(
        source=unsafe_source,
        modality=Modality.PROSE,
        content="x" * 20_000,
        origin=Origin(unsafe_source, unsafe_ref, (7, 99)),
        salience=0.1,
    )
    unspanned = Unit(
        source="plain.md",
        modality=Modality.PROSE,
        content="y" * 20_000,
        origin=Origin("plain.md", "line:3"),
        salience=0.0,
    )
    relation = Relation(
        kept.id,
        spanned.id,
        RelationKind.DERIVES_FROM,
        evidence="the large appendix derives the compact fact",
    )
    statement = GroundedStatement(
        content="The compact fact depends on both omitted appendices.",
        origins=(kept.origin, spanned.origin, unspanned.origin),
        evidence_unit_ids=(kept.id, spanned.id, unspanned.id),
    )
    result = Extraction(
        source="adversarial collection",
        kind="collection",
        units=[kept, spanned, unspanned],
        relations=[relation],
        summary_claims=[statement],
    )

    text = render(result, output=shape, budget=8192)
    prefix = "- drop-v1/" if shape == "ansi" else "  - `drop-v1/"
    records: dict[str, list[dict]] = {
        "unit": [],
        "relation": [],
        "statement": [],
    }
    raw_record_lines = []
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        kind, raw_payload = line.removeprefix(prefix).split(" ", 1)
        if shape == "md":
            assert raw_payload.endswith("`")
            raw_payload = raw_payload[:-1]
        value = json.loads(raw_payload)
        assert raw_payload == json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).replace("`", r"\u0060").replace("\x7f", r"\u007f")
        records[kind].append(value)
        raw_record_lines.append(f"drop-v1/{kind} {raw_payload}")

    expected = {
        "units": [
            {
                "id": spanned.id,
                "origin": {
                    "source": unsafe_source,
                    "ref": unsafe_ref,
                    "char_span": [7, 99],
                },
                "reason": "budget",
            },
            {
                "id": unspanned.id,
                "origin": {"source": "plain.md", "ref": "line:3"},
                "reason": "budget",
            },
        ],
        "relations": [
            {
                "index": 0,
                "src": kept.id,
                "dst": spanned.id,
                "kind": "derives-from",
                "reason": "budget",
            }
        ],
        "statements": [
            {
                "id": statement.id,
                "evidence_unit_ids": [kept.id, spanned.id, unspanned.id],
                "missing_evidence_unit_ids": [spanned.id, unspanned.id],
                "origins": [
                    {"source": "kept.md", "ref": "line:1", "char_span": [0, 25]},
                    {
                        "source": unsafe_source,
                        "ref": unsafe_ref,
                        "char_span": [7, 99],
                    },
                    {"source": "plain.md", "ref": "line:3"},
                ],
                "reason": "budget-evidence-omitted",
            }
        ],
    }
    observed = {
        "units": records["unit"],
        "relations": records["relation"],
        "statements": records["statement"],
    }

    assert observed == expected
    wire = "\n".join(raw_record_lines)
    assert "`" not in wire
    assert "\x1b" not in wire
    assert "\x7f" not in wire
    assert "\x07" not in wire
    assert "\u202e" not in wire
    assert "\u2066" not in wire
    assert "🧪" not in wire
    assert r"\u0060" in wire
    assert r"\u001b" in wire
    assert r"\u007f" in wire
    assert r"\u202e" in wire
    canonical = json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    assert digest in text


def test_projection_preserves_declared_model_and_role_backend_metadata():
    unit = _unit(1, "one grounded model input")
    model_record = {
        "task": "collection-synthesis",
        "model": "zbook-local/example",
        "input_sha256": "a" * 64,
        "output_sha256": "b" * 64,
    }
    result = Extraction(
        source="collection",
        kind="collection",
        units=[unit],
        meta={
            "models": [model_record],
            "role_backend": "configured-enrichment-v1",
        },
    )

    payload = json.loads(render(result, output="json"))

    assert payload["manifest"]["models"] == [model_record]
    assert payload["manifest"]["role_backend"] == "configured-enrichment-v1"


@pytest.mark.parametrize("shape", ["ansi", "md"])
def test_human_shapes_use_full_ids_and_render_retained_relations(shape):
    first = _unit(1, "parent fact", salience=0.9)
    second = _unit(2, "child fact", salience=0.8)
    evidence = "exact structural containment"
    result = Extraction(
        source="source.md",
        kind="markdown",
        units=[first, second],
        relations=[
            Relation(first.id, second.id, RelationKind.DESCRIBES, evidence=evidence)
        ],
    )

    text = render(result, output=shape, cite=False)

    assert first.id in text and second.id in text
    assert len(first.id) == len(second.id) == 32
    assert "Relations" in text
    assert "describes" in text
    assert evidence in text
    assert "dropped" in text.casefold() and "0 units" in text.casefold()


def test_ansi_escapes_untrusted_controls_but_keeps_generated_color():
    hostile = "trusted\x1b[2J\x1b]0;owned\x07\t\rnext\u2028line\u202eforge"
    unit = _unit(1, hostile)
    result = Extraction(
        source="source.md",
        kind="text",
        units=[unit],
        gaps=["gap\x1b[Hclaim"],
    )

    text = render(result, output="ansi", color=True)

    assert "\x1b[2J" not in text
    assert "\x1b]0;owned" not in text
    assert "\x1b[H" not in text
    assert "\t" not in text
    assert "\r" not in text
    assert "\u2028" not in text
    assert "\u202e" not in text
    assert "\\x1b[2J" in text
    assert "\\x07" in text
    assert "\\x09" in text
    assert "\\x0d" in text
    assert "\\u2028" in text
    assert "\\u202e" in text
    assert "\x1b[1;36m" in text, "renderer-owned ANSI color remains active"


def test_duplicate_unit_ids_and_dangling_relations_fail_closed():
    first = _unit(1, "same identity")
    duplicate = _unit(1, "same identity")

    with pytest.raises(ValueError, match="duplicate unit id"):
        render(
            Extraction(source="source.md", kind="markdown", units=[first, duplicate]),
            output="json",
        )

    with pytest.raises(ValueError, match="unresolved endpoint"):
        render(
            Extraction(
                source="source.md",
                kind="markdown",
                units=[first],
                relations=[
                    Relation(first.id, "0" * 32, RelationKind.DESCRIBES)
                ],
            ),
            output="json",
        )


def test_budget_selection_uses_logarithmically_many_full_serializations(monkeypatch):
    units = [
        _unit(
            index + 1,
            f"field_{index}: " + ("x" * 200),
            salience=1.0 - index / 1024,
            source="wide.json",
        )
        for index in range(512)
    ]
    result = Extraction(source="wide.json", kind="json", units=units)
    full_size = len(render(result, output="json").encode("utf-8"))
    with pytest.raises(BudgetTooSmall) as raised:
        render(result, output="json", budget=1)
    budget = (raised.value.required + full_size) // 2

    calls = 0
    original = render_module._BUILDERS["json"]

    def counted(bundle, options):
        nonlocal calls
        calls += 1
        return original(bundle, options)

    monkeypatch.setitem(render_module._BUILDERS, "json", counted)
    text = render(result, output="json", budget=budget)
    selection = json.loads(text)["manifest"]["selection"]

    assert 0 < selection["selected_units"] < len(units)
    assert calls < 80, f"selection rebuilt the full bundle {calls} times"
