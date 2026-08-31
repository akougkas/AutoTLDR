from __future__ import annotations

import json
from dataclasses import replace

import pytest

from autotldr.fusion import (
    CONTRADICTION_SIGNAL,
    IDENTIFIER_SIGNAL,
    LITERAL_SIGNAL,
    STRUCTURAL_SIGNAL,
    analyze,
    fuse,
)
from autotldr.unit import (
    Extraction,
    Gap,
    GapKind,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Role,
    Unit,
)


def _unit(
    source: str,
    content: str,
    *,
    ref: str,
    modality: Modality = Modality.PROSE,
    structure: tuple[str, ...] = (),
    role: Role = Role.UNKNOWN,
    salience: float = 0.5,
    meta: dict | None = None,
) -> Unit:
    return Unit(
        source=source,
        modality=modality,
        content=content,
        origin=Origin(source, ref),
        structure=structure,
        role=role,
        salience=salience,
        meta=meta or {},
    )


def _extraction(source: str, *units: Unit, kind: str = "text") -> Extraction:
    return Extraction(source=source, kind=kind, units=list(units))


def _reference(source: str, target: str, *, ref_kind: str = "path") -> Unit:
    return _unit(
        source,
        target,
        ref="line:2",
        modality=Modality.REFERENCE,
        meta={"target": target, "ref_kind": ref_kind},
    )


def _heading(source: str, title: str = "Overview") -> Unit:
    return _unit(
        source,
        title,
        ref="line:1",
        structure=(title,),
        salience=0.9,
        meta={"heading": True, "heading_level": 1},
    )


def _table(source: str, fields: list[tuple[str, list[str]]]) -> Extraction:
    root = _unit(
        source,
        f"CSV table with {len(fields)} columns",
        ref="table:",
        modality=Modality.SCHEMA,
        salience=0.9,
        meta={"table_summary": True, "columns": len(fields), "rows": 4},
    )
    columns = [
        _unit(
            source,
            f"Column {name}",
            ref=f"column:{index}",
            modality=Modality.SCHEMA,
            structure=(name,),
            meta={"column": index, "name": name, "types": types},
        )
        for index, (name, types) in enumerate(fields, start=1)
    ]
    return _extraction(source, root, *columns, kind="csv")


def _json_record_array(
    source: str, fields: list[tuple[str, list[str]]]
) -> Extraction:
    root = _unit(
        source,
        "$: array",
        ref="pointer:",
        modality=Modality.SCHEMA,
        meta={"schema_path": "$", "types": ["array"]},
    )
    item = _unit(
        source,
        "$/*: object",
        ref="pointer:",
        modality=Modality.SCHEMA,
        structure=("$/*",),
        meta={"schema_path": "$/*", "types": ["object"]},
    )
    children = [
        _unit(
            source,
            f"$/*/{name}: {types[0]}",
            ref=f"pointer:/{index}/{name}",
            modality=Modality.SCHEMA,
            structure=("$/*", f"$/*/{name}"),
            meta={"schema_path": f"$/*/{name}", "types": types},
        )
        for index, (name, types) in enumerate(fields)
    ]
    return _extraction(source, root, item, *children, kind="json")


def _jsonl_record_stream(
    source: str, fields: list[tuple[str, list[str]]]
) -> Extraction:
    root = _unit(
        source,
        "$: object",
        ref="lines:1-3#pointer:",
        modality=Modality.SCHEMA,
        meta={"schema_path": "$", "types": ["object"]},
    )
    children = [
        _unit(
            source,
            f"$/{name}: {types[0]}",
            ref=f"lines:1-3#pointer:/{name}",
            modality=Modality.SCHEMA,
            structure=(f"$/{name}",),
            meta={"schema_path": f"$/{name}", "types": types},
        )
        for name, types in fields
    ]
    return _extraction(source, root, *children, kind="jsonl")


def _payload(evidence: str, signal: str) -> dict:
    prefix = f"fusion.{signal} "
    assert evidence.startswith(prefix)
    return json.loads(evidence[len(prefix) :])


def test_literal_relative_path_resolves_to_deterministic_source_anchor() -> None:
    readme = "/corpus/README.md"
    results = "/corpus/data/results.csv"
    reference = _reference(readme, "data/results.csv")
    result_anchor = _heading(results, "Benchmark results")

    signals = analyze(
        [_extraction(readme, _heading(readme), reference), _extraction(results, result_anchor)]
    )

    assert len(signals.literal) == 1
    match = signals.literal[0]
    assert match.src == reference.id
    assert match.dst == result_anchor.id
    assert match.relation_kind == "references"
    evidence = _payload(match.evidence, LITERAL_SIGNAL)
    assert evidence["raw_target"] == "data/results.csv"
    assert evidence["target_source"] == results
    assert evidence["target_anchor_policy"] == "representative-source-anchor-v1"
    assert not signals.unresolved


def test_literal_basename_ambiguity_abstains_and_reports_candidates() -> None:
    readme = "/corpus/README.md"
    one = "/corpus/first/results.csv"
    two = "/corpus/second/results.csv"

    signals = analyze(
        [
            _extraction(readme, _heading(readme), _reference(readme, "results.csv")),
            _extraction(one, _heading(one)),
            _extraction(two, _heading(two)),
        ]
    )

    assert not signals.literal
    assert len(signals.unresolved) == 1
    unresolved = signals.unresolved[0]
    assert unresolved.reason == "ambiguous-target"
    assert unresolved.candidates == (one, two)


def test_external_url_is_not_a_collection_gap() -> None:
    source = "/corpus/README.md"
    reference = _reference(
        source, "https://external.example/paper", ref_kind="url"
    )

    signals = analyze([_extraction(source, _heading(source), reference)])

    assert not signals.literal
    assert not signals.unresolved
    assert any(
        trace.signal == LITERAL_SIGNAL
        and trace.reason == "external-reference"
        for trace in signals.traces
    )


def test_url_identity_is_safe_and_fragment_insensitive_but_query_sensitive() -> None:
    source = "https://notes.example/index"
    target = "https://example.test/a/guide?edition=2"
    reference = _reference(
        source,
        "https://EXAMPLE.test:443/a/tmp/../guide?edition=2#section",
        ref_kind="url",
    )
    wrong_query = "https://example.test/a/guide?edition=3"

    signals = analyze(
        [
            _extraction(source, _heading(source), reference),
            _extraction(target, _heading(target)),
            _extraction(wrong_query, _heading(wrong_query)),
        ]
    )

    assert len(signals.literal) == 1
    assert signals.literal[0].dst_source == target


def test_url_identity_preserves_encoded_reserved_slash_distinction() -> None:
    source = "https://notes.example/index"
    encoded = "https://example.test/a%2Fb"
    decoded = "https://example.test/a/b"
    reference = _reference(source, encoded, ref_kind="url")

    signals = analyze(
        [
            _extraction(source, _heading(source), reference),
            _extraction(decoded, _heading(decoded)),
        ]
    )

    assert not signals.literal
    assert not signals.unresolved


def test_same_source_literal_is_resolved_without_cross_edge_or_gap() -> None:
    source = "/corpus/paper.tex"
    definition = _unit(
        source,
        "\\begin{equation} x=1 \\label{eq:x} \\end{equation}",
        ref="line:1",
        modality=Modality.EQUATION,
        meta={"labels": ["eq:x"]},
    )
    reference = _reference(source, "eq:x", ref_kind="label")

    signals = analyze([_extraction(source, definition, reference, kind="latex")])

    assert not signals.literal
    assert not signals.unresolved
    assert any(
        trace.status == "resolved"
        and trace.reason == "intra-source-resolution"
        for trace in signals.traces
    )


def test_same_source_label_definition_wins_over_cross_source_collision() -> None:
    source = "/corpus/paper.tex"
    other = "/corpus/appendix.tex"
    reference = _reference(source, "eq:x", ref_kind="label")
    local = _unit(
        source,
        "\\label{eq:x}",
        ref="line:1",
        modality=Modality.EQUATION,
        meta={"labels": ["eq:x"]},
    )
    collision = _unit(
        other,
        "\\label{eq:x}",
        ref="line:1",
        modality=Modality.EQUATION,
        meta={"labels": ["eq:x"]},
    )

    signals = analyze(
        [_extraction(source, local, reference), _extraction(other, collision)]
    )

    assert not signals.literal
    assert not signals.unresolved
    assert any(trace.reason == "intra-source-resolution" for trace in signals.traces)


def test_explicit_source_anchor_is_preferred_for_literal_target() -> None:
    source = "/corpus/README.md"
    target = "/corpus/results.csv"
    reference = _reference(source, "results.csv")
    heading = _heading(target, "Results")
    anchor = _unit(
        target,
        "results.csv source",
        ref="source",
        modality=Modality.SOURCE,
        meta={"source_anchor": True},
    )

    signals = analyze(
        [_extraction(source, _heading(source), reference), _extraction(target, heading, anchor)]
    )

    assert signals.literal[0].dst == anchor.id


def test_unresolved_internal_label_is_addressed_to_reference_origin() -> None:
    source = "/corpus/paper.tex"
    reference = _reference(source, "missing-equation", ref_kind="label")

    signals = analyze([_extraction(source, reference, kind="latex")])

    assert len(signals.unresolved) == 1
    assert signals.unresolved[0].origin == reference.origin
    assert signals.unresolved[0].reason == "target-not-in-collection"


def test_identifier_signal_links_native_anchors_after_frozen_normalization() -> None:
    code_source = "/corpus/measure.py"
    data_source = "/corpus/results.csv"
    code = _unit(
        code_source,
        "def measure_throughput():",
        ref="line:1",
        modality=Modality.CODE,
        meta={"symbol": "measure_throughput", "signature": True},
    )
    column = _unit(
        data_source,
        'Column "tput_mbps"',
        ref="column:1",
        modality=Modality.SCHEMA,
        structure=("tput_mbps",),
        meta={"name": "tput_mbps", "column": 1, "types": ["number"]},
    )

    signals = analyze([_extraction(code_source, code), _extraction(data_source, column)])

    assert len(signals.identifier) == 1
    match = signals.identifier[0]
    assert match.relation_kind == "corresponds"
    evidence = _payload(match.evidence, IDENTIFIER_SIGNAL)
    assert match.details == evidence
    assert evidence["canonical"] == ["throughput"]
    assert "frozen-alias-v1" in evidence["normalizations"]
    assert "drop-action-prefix-v1" in evidence["normalizations"]
    assert "drop-measurement-suffix-v1" in evidence["normalizations"]


def test_identifier_compound_entity_key_and_write_prefix_close_sources() -> None:
    code_source = "/corpus/producer.py"
    data_source = "/corpus/results.csv"
    config_source = "/corpus/config.json"
    prose_source = "/corpus/README.md"
    code = _unit(
        code_source,
        "def write_run_id(run_id: str):",
        ref="line:1",
        modality=Modality.CODE,
        meta={"symbol": "write_run_id", "signature": True},
    )
    column = _unit(
        data_source,
        'Column "run_id"',
        ref="column:1",
        modality=Modality.SCHEMA,
        meta={"name": "run_id", "column": 1, "types": ["string"]},
    )
    field = _unit(
        config_source,
        "$/runs/*/run_id: string",
        ref="pointer:/runs/0/run_id",
        modality=Modality.SCHEMA,
        meta={"schema_path": "$/runs/*/run_id", "types": ["string"]},
    )
    prose = _unit(
        prose_source,
        "The addressable identifier is `run_id`.",
        ref="line:1",
    )

    signals = analyze(
        [
            _extraction(code_source, code, kind="python"),
            _extraction(data_source, column, kind="csv"),
            _extraction(config_source, field, kind="json"),
            _extraction(prose_source, prose, kind="markdown"),
        ]
    )

    assert len(signals.identifier) == 3
    assert {tuple(item.details["canonical"]) for item in signals.identifier} == {
        ("run", "id")
    }
    assert any(
        "drop-action-prefix-v1" in item.details["normalizations"]
        for item in signals.identifier
    )


def test_identifier_conflicting_qualified_namespaces_abstain() -> None:
    bakery_source = "/corpus/bakery.json"
    dispatch_source = "/corpus/dispatch.yaml"
    bakery = _unit(
        bakery_source,
        "$/bakery/parcel_id: string",
        ref="pointer:/bakery/parcel_id",
        modality=Modality.SCHEMA,
        meta={"schema_path": "$/bakery/parcel_id", "types": ["string"]},
    )
    dispatch = _unit(
        dispatch_source,
        "$/dispatch/parcel_id: string",
        ref="path:dispatch.parcel_id",
        modality=Modality.SCHEMA,
        meta={"schema_path": "$/dispatch/parcel_id", "types": ["string"]},
    )

    signals = analyze(
        [
            _extraction(bakery_source, bakery, kind="json"),
            _extraction(dispatch_source, dispatch, kind="yaml"),
        ]
    )

    assert not signals.identifier
    assert any(
        trace.signal == IDENTIFIER_SIGNAL
        and trace.reason == "conflicting-qualified-namespaces"
        for trace in signals.traces
    )


@pytest.mark.parametrize("identifier", ["id", "name", "status", "value", "main", "run"])
def test_common_identifiers_never_create_links(identifier: str) -> None:
    left_source = f"/left/{identifier}.py"
    right_source = f"/right/{identifier}.csv"
    left = _unit(
        left_source,
        f"def {identifier}():",
        ref="line:1",
        modality=Modality.CODE,
        meta={"symbol": identifier},
    )
    right = _unit(
        right_source,
        f"Column {identifier}",
        ref="column:1",
        modality=Modality.SCHEMA,
        meta={"name": identifier, "column": 1},
    )

    assert not analyze([_extraction(left_source, left), _extraction(right_source, right)]).identifier


def test_identifier_matching_does_not_use_substrings() -> None:
    left_source = "/corpus/a.py"
    right_source = "/corpus/b.md"
    symbol = _unit(
        left_source,
        "def grid_index():",
        ref="line:1",
        modality=Modality.CODE,
        meta={"symbol": "grid_index"},
    )
    prose = _unit(right_source, "The id is retained.", ref="line:1")

    assert not analyze([_extraction(left_source, symbol), _extraction(right_source, prose)]).identifier


def test_structural_signal_accepts_native_schema_correspondence() -> None:
    fields = [
        ("device_id", ["string"]),
        ("latency_ms", ["number"]),
        ("throughput_mbps", ["number"]),
        ("packet_loss_rate", ["number"]),
    ]
    left = _table("/corpus/raw.csv", fields)
    right = _table(
        "/corpus/derived.csv",
        fields[:3],
    )

    signals = analyze([left, right])

    assert len(signals.structural) == 1
    match = signals.structural[0]
    assert match.relation_kind == "corresponds"
    evidence = _payload(match.evidence, STRUCTURAL_SIGNAL)
    assert evidence["jaccard"] == {"numerator": 3, "denominator": 4}


def test_structural_same_width_different_schema_abstains() -> None:
    left = _table(
        "/corpus/a.csv",
        [("alpha_count", ["number"]), ("beta_rate", ["number"]), ("gamma_code", ["string"])],
    )
    right = _table(
        "/corpus/b.csv",
        [("region_name", ["string"]), ("owner_email", ["string"]), ("created_date", ["date"])],
    )

    assert not analyze([left, right]).structural


def test_structural_signal_matches_csv_to_native_json_record_schema() -> None:
    fields = [
        ("device_id", ["string"]),
        ("latency_ms", ["number"]),
        ("throughput_mbps", ["number"]),
    ]

    signals = analyze(
        [_table("/corpus/results.csv", fields), _json_record_array("/corpus/results.json", fields)]
    )

    assert len(signals.structural) == 1
    evidence = signals.structural[0].details
    assert evidence["left_family"] == evidence["right_family"] == "table"
    assert evidence["jaccard"] == {"numerator": 3, "denominator": 3}


def test_structural_signal_treats_jsonl_root_as_a_record_stream() -> None:
    fields = [
        ("message_id", ["string"]),
        ("delivery_attempts", ["integer"]),
        ("ack_latency_ms", ["number"]),
    ]

    signals = analyze(
        [
            _jsonl_record_stream("/corpus/events.jsonl", fields),
            _json_record_array("/corpus/examples.json", fields),
        ]
    )

    assert len(signals.structural) == 1
    assert signals.structural[0].details["left_family"] == "table"
    assert signals.structural[0].details["right_family"] == "table"


def test_structural_generic_three_column_schema_abstains() -> None:
    fields = [("id", ["integer"]), ("status", ["string"]), ("value", ["number"])]
    signals = analyze([_table("/corpus/a.csv", fields), _table("/corpus/b.csv", fields)])

    assert not signals.structural
    assert any(
        trace.signal == STRUCTURAL_SIGNAL
        and trace.reason == "insufficient-discriminative-fields"
        for trace in signals.traces
    )


def test_structural_incompatible_types_abstain() -> None:
    left = _table(
        "/corpus/a.csv",
        [("device_code", ["string"]), ("latency_millis", ["number"]), ("packet_count", ["integer"])],
    )
    right = _table(
        "/corpus/b.csv",
        [("device_code", ["number"]), ("latency_millis", ["string"]), ("packet_count", ["string"])],
    )

    assert not analyze([left, right]).structural


def test_strict_prose_scalar_conflict_emits_contradiction() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    left = _unit(left_source, "retry_limit = 3", ref="line:1")
    right = _unit(right_source, "retry_limit = 4", ref="line:1")

    signals = analyze([_extraction(left_source, left), _extraction(right_source, right)])

    assert len(signals.contradictions) == 1
    match = signals.contradictions[0]
    assert match.relation_kind == "contradicts"
    evidence = _payload(match.evidence, CONTRADICTION_SIGNAL)
    assert evidence["canonical_key"] == ["retry", "limit"]
    assert {evidence["left"]["canonical_value"], evidence["right"]["canonical_value"]} == {"3", "4"}


def test_numeric_spelling_three_and_three_point_zero_are_equal() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    signals = analyze(
        [
            _extraction(left_source, _unit(left_source, "retry_limit = 3", ref="line:1")),
            _extraction(right_source, _unit(right_source, "retry_limit = 3.0", ref="line:1")),
        ]
    )

    assert not signals.contradictions
    assert any(
        trace.signal == CONTRADICTION_SIGNAL
        and trace.reason == "canonically-equal-values"
        for trace in signals.traces
    )


def test_multiple_values_within_one_source_suppress_cross_source_contradiction() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    signals = analyze(
        [
            _extraction(
                left_source,
                _unit(left_source, "retry_limit = 3", ref="line:1"),
                _unit(left_source, "retry_limit = 4", ref="line:2"),
            ),
            _extraction(right_source, _unit(right_source, "retry_limit = 5", ref="line:1")),
        ]
    )

    assert not signals.contradictions
    assert any(
        trace.reason == "multiple-values-within-source" for trace in signals.traces
    )


def test_bare_or_weak_fact_keys_abstain_even_when_sources_are_linked() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    left = _extraction(
        left_source,
        _heading(left_source),
        _reference(left_source, "b.md"),
        _unit(left_source, "n = 40", ref="line:3"),
    )
    right = _extraction(
        right_source,
        _heading(right_source),
        _unit(right_source, "n = 38", ref="line:2"),
    )

    signals = analyze([left, right])

    assert signals.literal
    assert not signals.contradictions
    assert any(trace.reason == "unqualified-fact-key" for trace in signals.traces)


def test_scalar_values_with_different_units_abstain() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    signals = analyze(
        [
            _extraction(left_source, _unit(left_source, "retry_limit = 3 sec", ref="line:1")),
            _extraction(right_source, _unit(right_source, "retry_limit = 4 ms", ref="line:1")),
        ]
    )

    assert not signals.contradictions


def test_ranges_inequalities_code_and_examples_are_not_scalar_facts() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    units = [
        _unit(left_source, "retry_limit >= 3", ref="line:1"),
        _unit(left_source, "retry_limit = 3..5", ref="line:2"),
        _unit(
            left_source,
            "retry_limit = 3",
            ref="line:3",
            modality=Modality.CODE,
        ),
        _unit(
            left_source,
            "retry_limit = 3",
            ref="line:4",
            meta={"example_cue": True},
        ),
    ]
    right = _unit(right_source, "retry_limit = 4", ref="line:1")

    assert not analyze([_extraction(left_source, *units), _extraction(right_source, right)]).contradictions


def test_structured_constant_and_strict_prose_fact_compare() -> None:
    config_source = "/corpus/config.json"
    doc_source = "/corpus/README.md"
    structured = _unit(
        config_source,
        "$/retry_limit: integer; constant 3.",
        ref="pointer:/retry_limit",
        modality=Modality.SCHEMA,
        structure=("$/retry_limit",),
        meta={
            "schema_path": "$/retry_limit",
            "types": ["integer"],
            "values": ["3"],
            "numeric": {"min": 3, "max": 3, "mean": 3},
            "evidence_refs": ["pointer:/retry_limit"],
        },
    )
    prose = _unit(doc_source, "retry_limit = 4", ref="line:1")

    signals = analyze(
        [_extraction(config_source, structured, kind="json"), _extraction(doc_source, prose)]
    )

    assert len(signals.contradictions) == 1


def test_model_roles_do_not_change_any_signal_output() -> None:
    left_source = "/corpus/a.py"
    right_source = "/corpus/b.csv"
    left = _unit(
        left_source,
        "def calculate_latency():",
        ref="line:1",
        modality=Modality.CODE,
        role=Role.DEFINITION,
        meta={"symbol": "calculate_latency"},
    )
    right = _unit(
        right_source,
        "Column latency_ms",
        ref="column:1",
        modality=Modality.SCHEMA,
        role=Role.PROCEDURE,
        meta={"name": "latency_ms", "column": 1, "types": ["number"]},
    )
    enriched = [_extraction(left_source, left), _extraction(right_source, right)]
    erased = [
        _extraction(left_source, replace(left, role=Role.UNKNOWN)),
        _extraction(right_source, replace(right, role=Role.UNKNOWN)),
    ]

    assert analyze(enriched) == analyze(erased)


def test_signal_output_is_independent_of_extraction_permutation() -> None:
    one_source = "/corpus/one.md"
    two_source = "/corpus/two.csv"
    three_source = "/corpus/three.py"
    one = _extraction(
        one_source,
        _heading(one_source),
        _reference(one_source, "two.csv"),
        _unit(one_source, "retry_limit = 3", ref="line:3"),
    )
    two = _extraction(
        two_source,
        _heading(two_source),
        _unit(two_source, "retry_limit = 4", ref="line:2"),
    )
    three = _extraction(
        three_source,
        _unit(
            three_source,
            "def calculate_retry_limit():",
            ref="line:1",
            modality=Modality.CODE,
            meta={"symbol": "calculate_retry_limit"},
        ),
    )

    assert analyze([one, two, three]) == analyze([three, one, two])


def test_duplicate_logical_sources_fail_closed() -> None:
    source = "/corpus/a.md"
    with pytest.raises(ValueError, match="unique logical extraction sources"):
        analyze([_extraction(source, _heading(source)), _extraction(source, _heading(source))])


def test_duplicate_unit_ids_within_one_extraction_fail_closed() -> None:
    source = "/corpus/a.md"
    unit = _heading(source)
    with pytest.raises(ValueError, match="duplicate unit id"):
        analyze([_extraction(source, unit, unit)])


def test_fuse_requires_a_real_collection() -> None:
    source = "/corpus/one.md"
    with pytest.raises(ValueError, match="at least two"):
        fuse([_extraction(source, _heading(source))])


def test_fuse_adds_exact_source_anchors_relations_and_three_grounded_sentences() -> None:
    readme_source = "/corpus/README.md"
    results_source = "/corpus/results.csv"
    readme_heading = _heading(readme_source)
    reference = _reference(readme_source, "results.csv")
    results_heading = _heading(results_source, "Results")
    readme = _extraction(readme_source, readme_heading, reference)
    results = _extraction(results_source, results_heading)
    readme.meta.update(
        {
            "inputs": [
                {
                    "source": readme_source,
                    "kind": "markdown",
                    "tier": 0,
                    "bytes": 20,
                    "sha256": "a" * 64,
                }
            ],
            "timings": {"acquisition_ms": 1.25, "extraction_ms": 2.5},
        }
    )
    results.meta.update(
        {
            "inputs": [
                {
                    "source": results_source,
                    "kind": "csv",
                    "tier": 0,
                    "bytes": 30,
                    "sha256": "b" * 64,
                }
            ],
            "timings": {"acquisition_ms": 0.75, "extraction_ms": 1.5},
        }
    )

    fused = fuse([results, readme], subject="/corpus")

    assert fused.source == "/corpus"
    assert fused.kind == "collection"
    assert {readme_heading.id, reference.id, results_heading.id} <= {
        unit.id for unit in fused.units
    }
    anchors = [unit for unit in fused.units if unit.meta.get("source_anchor")]
    assert len(anchors) == 2
    assert all(unit.modality is Modality.SOURCE for unit in anchors)
    assert all(unit.role is Role.UNKNOWN for unit in anchors)
    assert {unit.origin for unit in anchors} == {
        Origin(readme_source, "source"),
        Origin(results_source, "source"),
    }
    literal = [
        relation
        for relation in fused.relations
        if relation.evidence.startswith("fusion.literal-v1 ")
    ]
    assert len(literal) == 1
    assert literal[0].src == reference.id
    assert literal[0].kind is RelationKind.REFERENCES
    assert literal[0].dst in {unit.id for unit in anchors}
    assert not [gap for gap in fused.gaps if gap.kind is GapKind.ORPHAN]
    assert len(fused.summary_claims) == 3
    unit_ids = {unit.id for unit in fused.units}
    assert all(
        set(statement.evidence_unit_ids) <= unit_ids
        and statement.origins
        and statement.content.endswith(".")
        for statement in fused.summary_claims
    )
    assert fused.meta["models"] == []
    assert [item["source"] for item in fused.meta["inputs"]] == [
        readme_source,
        results_source,
    ]
    assert fused.meta["timings"]["acquisition_ms"] == 2
    assert fused.meta["timings"]["extraction_ms"] == 4
    assert fused.meta["timings"]["fusion_ms"] >= 0


def test_fuse_preserves_original_relations_and_typed_extraction_gaps() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    parent = _heading(left_source)
    child = _unit(left_source, "Body", ref="line:2")
    original_relation = Relation(
        parent.id,
        child.id,
        RelationKind.DESCRIBES,
        "section containment",
    )
    original_gap = Gap(
        "no headings in appendix",
        Origin(left_source, "line:10"),
        GapKind.EXTRACTION,
    )
    left = Extraction(
        source=left_source,
        kind="markdown",
        units=[parent, child],
        relations=[original_relation],
        gaps=[original_gap],
    )
    right = _extraction(right_source, _heading(right_source))

    fused = fuse([left, right])

    assert original_relation in fused.relations
    assert original_gap in fused.gaps
    assert next(gap for gap in fused.gaps if str(gap) == str(original_gap)).kind is GapKind.EXTRACTION


def test_fuse_emits_measured_local_path_gaps_and_suppresses_unproved_orphans() -> None:
    one_source = "/corpus/one.md"
    two_source = "/corpus/two.md"
    three_source = "/corpus/three.md"
    reference = _reference(one_source, "missing.csv")

    fused = fuse(
        [
            _extraction(one_source, _heading(one_source), reference),
            _extraction(two_source, _heading(two_source)),
            _extraction(three_source, _heading(three_source)),
        ]
    )

    unresolved = [gap for gap in fused.gaps if gap.kind is GapKind.UNRESOLVED_REFERENCE]
    orphans = [gap for gap in fused.gaps if gap.kind is GapKind.ORPHAN]
    assert len(unresolved) == 1
    assert unresolved[0].origin == reference.origin
    assert "within this collection" in str(unresolved[0])
    assert orphans == []
    disposition = fused.meta["fusion"]["evaluated_dispositions"]
    assert disposition["signals"]["orphan-v1"] == {
        "status": "disable",
        "subtypes": [],
    }
    assert disposition["orphan_candidates_suppressed"] == 3
    summary = " ".join(statement.content for statement in fused.summary_claims)
    assert "contradiction and orphan" not in summary.casefold()


def test_fuse_applies_scored_subtype_and_disabled_signal_dispositions() -> None:
    code_source = "/corpus/producer.py"
    prose_source = "/corpus/README.md"
    schema_source = "/corpus/config.json"
    code = _unit(
        code_source,
        "def retry_limit():",
        ref="line:1",
        modality=Modality.CODE,
        meta={"symbol": "retry_limit"},
    )
    prose = _unit(
        prose_source,
        "retry_limit = 4",
        ref="line:1",
    )
    schema = _unit(
        schema_source,
        "$/retry_limit: integer; constant 3.",
        ref="pointer:/retry_limit",
        modality=Modality.SCHEMA,
        meta={
            "schema_path": "$/retry_limit",
            "types": ["integer"],
            "values": ["3"],
            "numeric": {"min": 3, "max": 3, "mean": 3},
            "evidence_refs": ["pointer:/retry_limit"],
        },
    )

    raw = analyze(
        [
            _extraction(code_source, code, kind="python"),
            _extraction(prose_source, prose, kind="markdown"),
            _extraction(schema_source, schema, kind="json"),
        ]
    )
    fused = fuse(
        [
            _extraction(code_source, code, kind="python"),
            _extraction(prose_source, prose, kind="markdown"),
            _extraction(schema_source, schema, kind="json"),
        ]
    )

    assert raw.identifier and raw.contradictions
    emitted = [
        relation
        for relation in fused.relations
        if relation.evidence.startswith("fusion.")
    ]
    assert emitted
    assert all(
        relation.kind is RelationKind.CORRESPONDS
        and "identifier-v1" in relation.evidence
        for relation in emitted
    )
    assert not any(
        relation.kind is RelationKind.CONTRADICTS for relation in fused.relations
    )
    signal_meta = fused.meta["fusion"]["signals"]
    assert signal_meta[IDENTIFIER_SIGNAL]["disposition"] == {
        "status": "ship-preregistered-subtype",
        "subtypes": ["native-native"],
    }
    assert signal_meta[CONTRADICTION_SIGNAL]["accepted"] == 0
    assert signal_meta[CONTRADICTION_SIGNAL]["raw_before_disposition"] == 1


def test_fuse_resolves_extraction_level_literal_labels_to_source_anchor_only() -> None:
    citing_source = "/corpus/appendix.tex"
    defining_source = "/corpus/paper.tex"
    reference = _reference(citing_source, "eq:throughput", ref_kind="label")
    citing = _extraction(citing_source, reference, kind="latex")
    defining = _extraction(defining_source, _heading(defining_source), kind="latex")
    defining.meta["labels"] = ["eq:throughput"]

    fused = fuse([citing, defining])

    literal = [relation for relation in fused.relations if relation.kind is RelationKind.REFERENCES]
    assert len(literal) == 1
    target = next(unit for unit in fused.units if unit.id == literal[0].dst)
    assert target.modality is Modality.SOURCE
    assert target.meta["literal_labels"] == ["eq:throughput"]
    # Literal label definitions are not identifier anchors.
    assert fused.meta["fusion"]["signals"][IDENTIFIER_SIGNAL]["accepted"] == 0


def test_fuse_rejects_dangling_original_relation_before_assembly() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    left = _extraction(left_source, _heading(left_source))
    left.relations.append(
        Relation(left.units[0].id, "f" * 32, RelationKind.DESCRIBES, "broken")
    )

    with pytest.raises(ValueError, match="unresolved endpoint"):
        fuse([left, _extraction(right_source, _heading(right_source))])


def test_fuse_rejects_preexisting_relation_into_a_different_input() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    left_heading = _heading(left_source)
    right_heading = _heading(right_source)
    left = _extraction(left_source, left_heading)
    left.relations.append(
        Relation(
            left_heading.id,
            right_heading.id,
            RelationKind.REFERENCES,
            "caller supplied cross-input edge",
        )
    )

    with pytest.raises(ValueError, match="unresolved endpoint"):
        fuse([left, _extraction(right_source, right_heading)])


def test_fuse_is_canonical_under_input_permutation() -> None:
    left_source = "/corpus/a.md"
    right_source = "/corpus/b.md"
    left = _extraction(left_source, _heading(left_source), _reference(left_source, "b.md"))
    right = _extraction(right_source, _heading(right_source))

    first = fuse([left, right], subject="/corpus")
    second = fuse([right, left], subject="/corpus")
    first_fusion_ms = first.meta["timings"].pop("fusion_ms")
    second_fusion_ms = second.meta["timings"].pop("fusion_ms")
    assert first_fusion_ms >= 0 and second_fusion_ms >= 0
    assert first == second
