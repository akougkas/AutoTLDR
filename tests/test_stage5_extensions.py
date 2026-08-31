"""Stage 5's explicit, composable adapter-registry contract."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from autotldr.extensions import (
    ACQUISITION_CALL_CONTRACT,
    EXTENSION_API_VERSION,
    RENDERER_BUILDER_CONTRACT,
    AcquisitionSpec,
    ExtensionCollisionError,
    ExtensionConformanceError,
    ExtensionLoadError,
    ExtensionRegistry,
    ExtensionValidationError,
    ExtractorSpec,
    RendererSpec,
    SignatureProbe,
    load_extension,
    validate_acquisition_output,
    validate_extraction_output,
    validate_renderer_callable,
    validate_renderer_output,
)
from autotldr.collection import (
    CollectionAcquisition,
    DeclineKind,
    MemberDecline,
)
from autotldr.unit import (
    Extraction,
    Gap,
    GroundedStatement,
    Modality,
    Origin,
    Relation,
    RelationKind,
    Role,
    Unit,
)


def _valid_extraction(
    source: str = "sample.wfx", *, kind: str = "weird"
) -> Extraction:
    first = Unit(
        source,
        Modality.PROSE,
        "First addressable fact.",
        Origin(source, "line:1", (0, 23)),
        structure=("Facts",),
        salience=0.9,
        meta={"ordinal": 1},
    )
    second = Unit(
        source,
        Modality.REFERENCE,
        "Second addressable fact.",
        Origin(source, "line:2", (24, 48)),
        salience=0.7,
        meta={"ordinal": 2},
    )
    statement = GroundedStatement(
        "The two facts are linked.",
        (first.origin, second.origin),
        (first.id, second.id),
    )
    return Extraction(
        source=source,
        kind=kind,
        units=[first, second],
        relations=[
            Relation(
                first.id,
                second.id,
                RelationKind.REFERENCES,
                evidence="Native reference edge.",
                confidence=0.95,
            )
        ],
        gaps=[Gap("One section was unavailable.", Origin(source, "section:3"))],
        meta={
            "counts": {
                "units": 2,
                "relations": 1,
                "gaps": 1,
                "summary_claims": 1,
            },
            "inputs": [
                {
                    "source": source,
                    "kind": kind,
                    "bytes": 48,
                    "sha256": "a" * 64,
                    "tier": 6,
                }
            ],
        },
        summary_claims=[statement],
    )


def _valid_acquisition(*, with_digest: bool = False) -> CollectionAcquisition:
    leaf = _valid_extraction("virtual/member.wfx")
    decline = MemberDecline(
        DeclineKind.UNSUPPORTED,
        "Binary member is outside the adapter contract.",
        Origin("virtual/member.bin", "source"),
        detected_kind="binary",
        tier=6,
        details={"policy": "text-only"},
    )
    members = [
        {
            "status": "extracted",
            "source": leaf.source,
            "kind": leaf.kind,
            "units": len(leaf.units),
            "relations": len(leaf.relations),
            "gaps": len(leaf.gaps),
            "order": 0,
        },
        {**decline.as_manifest(), "order": 1},
    ]
    manifest: dict[str, object] = {
        "schema": 1,
        "source": "virtual",
        "kind": "virtual-collection",
        "container": {
            "source": "virtual",
            "kind": "virtual-collection",
        },
        "members": members,
        "counts": {
            "extracted": 1,
            "declined": 1,
            "ignored": 0,
            "records": 2,
        },
        "admitted_bytes": 48,
    }
    if with_digest:
        canonical = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        manifest["sha256"] = hashlib.sha256(canonical).hexdigest()
    return CollectionAcquisition(
        source="virtual",
        kind="virtual-collection",
        extractions=(leaf,),
        declines=(decline,),
        manifest=manifest,
    )


def _extractor(
    name: str,
    *,
    kinds: tuple[str, ...] | None = None,
    aliases: tuple[str, ...] = (),
    suffixes: tuple[str, ...] = (),
    media_types: tuple[str, ...] = (),
    signatures: tuple[SignatureProbe, ...] = (),
    module: str = "example.adapters",
    callable_name: str = "extract",
) -> ExtractorSpec:
    return ExtractorSpec(
        name=name,
        module=module,
        callable=callable_name,
        kinds=kinds or (name,),
        aliases=aliases,
        suffixes=suffixes,
        media_types=media_types,
        signatures=signatures,
    )


def test_specs_are_immutable_validated_and_canonical() -> None:
    spec = ExtractorSpec(
        name=" WEIRD-FORMAT ",
        module="community.weird",
        callable="Adapter.extract",
        kinds=("Record", "Weird"),
        aliases=("WFX",),
        suffixes=(".WFX", ".weird"),
        media_types=("Application/X-WEIRD",),
        signatures=(SignatureProbe(b"WFX\x00", offset=4),),
        tier=6,
        extra="WEIRD",
    )

    assert spec.name == "weird-format"
    assert spec.kinds == ("record", "weird")
    assert spec.suffixes == (".weird", ".wfx")
    assert spec.media_types == ("application/x-weird",)
    assert spec.extra == "weird"
    assert spec.call_contract == "source-to-extraction-v1"
    with pytest.raises(FrozenInstanceError):
        spec.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "not a name"}, "name"),
        ({"module": ".relative"}, "module"),
        ({"callable": "bad:name"}, "callable"),
        ({"kinds": ()}, "kinds"),
        ({"suffixes": ("pdf",)}, "suffix"),
        ({"media_types": ("text/plain; charset=utf-8",)}, "media type"),
        ({"tier": True}, "tier"),
        ({"contract_version": EXTENSION_API_VERSION + 1}, "contract_version"),
    ],
)
def test_extractor_validation_fails_closed(kwargs: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "name": "weird",
        "module": "community.weird",
        "callable": "extract",
        "kinds": ("weird",),
    }
    values.update(kwargs)
    with pytest.raises(ExtensionValidationError, match=message):
        ExtractorSpec(**values)  # type: ignore[arg-type]


def test_signature_probe_supports_tail_offsets_and_masks() -> None:
    tail = SignatureProbe(b"PAR1", offset=-4)
    masked = SignatureProbe(b"\xa0\x00", mask=b"\xf0\x00")

    assert tail.matches(b"PAR1bodyPAR1")
    assert not tail.matches(b"PAR1bodyNOPE")
    assert masked.matches(b"\xaf\xff")
    assert not masked.matches(b"\x9f\xff")
    assert not tail.matches(b"x")

    with pytest.raises(ExtensionValidationError, match="same length"):
        SignatureProbe(b"AB", mask=b"\xff")
    with pytest.raises(ExtensionValidationError, match="information bit"):
        SignatureProbe(b"AB", mask=b"\x00\x00")


@pytest.mark.parametrize("namespace", ["name", "suffix", "media", "signature"])
def test_extractor_collisions_are_rejected_atomically(namespace: str) -> None:
    first = _extractor(
        "first",
        kinds=("first-kind",),
        aliases=("shared",) if namespace == "name" else (),
        suffixes=(".shared",) if namespace == "suffix" else (),
        media_types=("application/x-shared",) if namespace == "media" else (),
        signatures=(SignatureProbe(b"SHARED"),)
        if namespace == "signature"
        else (),
    )
    second = _extractor(
        "second",
        kinds=("second-kind",),
        aliases=("shared",) if namespace == "name" else (),
        suffixes=(".shared",) if namespace == "suffix" else (),
        media_types=("application/x-shared",) if namespace == "media" else (),
        signatures=(SignatureProbe(b"SHARED"),)
        if namespace == "signature"
        else (),
    )
    registry = ExtensionRegistry()

    with pytest.raises(ExtensionCollisionError):
        registry.register_many((first, second))

    assert len(registry) == 0


def test_acquisition_and_renderer_names_have_separate_collision_domains() -> None:
    acquisition = AcquisitionSpec(
        name="archive",
        module="community.archive",
        callable="acquire",
        kinds=("archive",),
        aliases=("packed",),
    )
    renderer = RendererSpec(
        name="archive",
        module="community.render",
        callable="build",
        aliases=("arc",),
        suffixes=(".arc",),
        media_types=("text/x-archive-summary",),
    )
    registry = ExtensionRegistry((renderer, acquisition))

    assert registry.get_acquisition("packed") is acquisition
    assert registry.get_renderer("arc") is renderer
    assert acquisition.call_contract == ACQUISITION_CALL_CONTRACT

    with pytest.raises(ExtensionCollisionError):
        registry.register(
            AcquisitionSpec(
                name="other-acquirer",
                module="community.other",
                callable="acquire",
                kinds=("packed",),
            )
        )
    with pytest.raises(ExtensionCollisionError):
        registry.register(
            RendererSpec(
                name="other-renderer",
                module="community.other",
                callable="build",
                suffixes=(".arc",),
            )
        )


def test_registry_order_and_manifest_are_registration_order_independent() -> None:
    alpha = _extractor(
        "alpha", suffixes=(".a",), media_types=("application/x-a",)
    )
    zeta = _extractor("zeta", suffixes=(".z",))
    renderer = RendererSpec(
        name="mdx",
        module="community.mdx",
        callable="build",
        aliases=("markdown-extra",),
        suffixes=(".mdx",),
        media_types=("text/mdx",),
        supports_citations=True,
        supports_color=False,
    )
    left = ExtensionRegistry((zeta, renderer, alpha))
    right = ExtensionRegistry((alpha, zeta, renderer))

    assert [spec.name for spec in left.extractors] == ["alpha", "zeta"]
    assert left.snapshot() == right.snapshot()
    assert json.dumps(left.capability_manifest(), sort_keys=True) == json.dumps(
        right.capability_manifest(), sort_keys=True
    )

    manifest = left.capability_manifest()
    assert manifest["counts"] == {
        "extractors": 2,
        "acquisitions": 0,
        "renderers": 1,
    }
    renderer_meta = manifest["renderers"][0]  # type: ignore[index]
    assert renderer_meta["builder_contract"] == RENDERER_BUILDER_CONTRACT
    assert renderer_meta["supports_citations"] is True
    assert renderer_meta["supports_color"] is False


def test_lookup_normalizes_suffix_media_parameters_and_strong_bytes() -> None:
    signature = SignatureProbe(b"PAR1", offset=-4)
    parquet = _extractor(
        "parquet",
        suffixes=(".parquet",),
        media_types=("application/vnd.apache.parquet",),
        signatures=(signature,),
    )
    registry = ExtensionRegistry((parquet,))

    assert registry.extractor_for_suffix("PARQUET") is parquet
    assert (
        registry.extractor_for_media_type(
            "Application/Vnd.Apache.Parquet; charset=binary"
        )
        is parquet
    )
    assert registry.extractor_for_bytes(b"PAR1dataPAR1") is parquet
    assert registry.extractor_for_bytes(b"unknown") is None


def test_callable_resolution_is_lazy_and_validates_core_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "autotldr_test_lazy_adapter"
    marker = tmp_path / "imported"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('yes')\n"
        "def extract(source, *, kind=None):\n"
        "    return source\n"
        "def bad(source, required):\n"
        "    return source\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    good = _extractor("lazy", module=module_name, callable_name="extract")
    registry = ExtensionRegistry((good,))

    assert not marker.exists()
    resolved = registry.resolve_extractor("lazy")
    assert marker.read_text(encoding="utf-8") == "yes"
    assert resolved("source") == "source"
    assert registry.resolve_extractor(good) is resolved

    bad = _extractor("bad", module=module_name, callable_name="bad")
    registry.register(bad)
    with pytest.raises(ExtensionConformanceError, match="core call shape"):
        registry.resolve_extractor("bad")


def test_explicit_third_party_factory_loads_metadata_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "autotldr_test_extension_package"
    marker = tmp_path / "factory-ran"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        "from autotldr.extensions import ExtractorSpec, RendererSpec\n"
        "def register_formats():\n"
        f"    Path({str(marker)!r}).write_text('yes')\n"
        "    return [\n"
        "      ExtractorSpec(name='wfx', module='builtins', callable='len', "
        "kinds=('weird',), suffixes=('.wfx',), tier=9),\n"
        "      RendererSpec(name='wire', module='builtins', callable='format', "
        "aliases=('wfx-wire',), suffixes=('.wire',)),\n"
        "    ]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = ExtensionRegistry()

    assert not marker.exists()
    loaded = load_extension(f"{module_name}:register_formats", registry)

    assert marker.read_text(encoding="utf-8") == "yes"
    assert [(type(spec).__name__, spec.name) for spec in loaded] == [
        ("ExtractorSpec", "wfx"),
        ("RendererSpec", "wire"),
    ]
    assert registry.extractor_for_suffix(".wfx") is loaded[0]
    assert registry.get_renderer("wfx-wire") is loaded[1]


@pytest.mark.parametrize("phase", ["import", "factory"])
def test_third_party_failure_messages_scrub_exception_details(
    phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "DO-NOT-LEAK-token-/private/path"
    module_name = f"autotldr_test_failure_{phase}"
    if phase == "import":
        body = f"raise RuntimeError({secret!r})\n"
    else:
        body = (
            "def autotldr_extension():\n"
            f"    raise RuntimeError({secret!r})\n"
        )
    (tmp_path / f"{module_name}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ExtensionLoadError) as raised:
        load_extension(module_name, ExtensionRegistry())

    assert secret not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_factory_registration_is_atomic_when_its_last_spec_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "autotldr_test_colliding_factory"
    (tmp_path / f"{module_name}.py").write_text(
        "from autotldr.extensions import ExtractorSpec\n"
        "def autotldr_extension():\n"
        "    return [\n"
        "      ExtractorSpec(name='new', module='builtins', callable='len', "
        "kinds=('new',), suffixes=('.new',)),\n"
        "      ExtractorSpec(name='collision', module='builtins', callable='len', "
        "kinds=('collision',), suffixes=('.owned',)),\n"
        "    ]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    incumbent = _extractor("owned", suffixes=(".owned",))
    registry = ExtensionRegistry((incumbent,))

    with pytest.raises(ExtensionCollisionError):
        load_extension(module_name, registry)

    assert registry.extractor_for_suffix(".new") is None
    assert registry.extractors == (incumbent,)


def test_extraction_conformance_accepts_complete_grounded_ir() -> None:
    result = _valid_extraction()

    assert validate_extraction_output(result) is result


def test_extraction_conformance_accepts_documented_collection_sources() -> None:
    child = Unit(
        "root/child.md",
        Modality.PROSE,
        "Child evidence.",
        Origin("root/child.md", "line:1"),
    )
    result = Extraction(source="root", kind="collection", units=[child])

    assert validate_extraction_output(result) is result


def test_extraction_conformance_rejects_subclasses_and_container_subclasses() -> None:
    class DerivedExtraction(Extraction):
        pass

    class DerivedList(list):
        pass

    class DerivedDict(dict):
        pass

    with pytest.raises(ExtensionConformanceError, match="concrete.*Extraction"):
        validate_extraction_output(
            DerivedExtraction("sample.wfx", "weird", gaps=["declined"])
        )

    units_subclass = _valid_extraction()
    units_subclass.units = DerivedList(units_subclass.units)
    with pytest.raises(ExtensionConformanceError, match="units.*concrete list"):
        validate_extraction_output(units_subclass)

    metadata_subclass = _valid_extraction()
    metadata_subclass.meta = DerivedDict(metadata_subclass.meta)
    with pytest.raises(ExtensionConformanceError, match="metadata.*concrete"):
        validate_extraction_output(metadata_subclass)


def test_extraction_conformance_rejects_every_nested_core_subclass() -> None:
    class DerivedOrigin(Origin):
        pass

    class DerivedUnit(Unit):
        pass

    class DerivedRelation(Relation):
        pass

    class DerivedGap(Gap):
        pass

    class DerivedStatement(GroundedStatement):
        pass

    origin_result = _valid_extraction()
    object.__setattr__(
        origin_result.units[0],
        "origin",
        DerivedOrigin("sample.wfx", "line:1", (0, 23)),
    )
    with pytest.raises(ExtensionConformanceError, match="concrete Origin"):
        validate_extraction_output(origin_result)

    unit_result = _valid_extraction()
    original_unit = unit_result.units[0]
    unit_result.units[0] = DerivedUnit(
        original_unit.source,
        original_unit.modality,
        original_unit.content,
        original_unit.origin,
        original_unit.role,
        original_unit.structure,
        original_unit.salience,
        original_unit.confidence,
        original_unit.tokens,
        original_unit.meta,
    )
    with pytest.raises(ExtensionConformanceError, match="concrete Unit"):
        validate_extraction_output(unit_result)

    relation_result = _valid_extraction()
    original_relation = relation_result.relations[0]
    relation_result.relations[0] = DerivedRelation(
        original_relation.src,
        original_relation.dst,
        original_relation.kind,
        original_relation.evidence,
        original_relation.confidence,
    )
    with pytest.raises(ExtensionConformanceError, match="concrete Relation"):
        validate_extraction_output(relation_result)

    gap_result = _valid_extraction()
    original_gap = gap_result.gaps[0]
    list.__setitem__(
        gap_result.gaps,
        0,
        DerivedGap(original_gap.content, original_gap.origin, original_gap.kind),
    )
    with pytest.raises(ExtensionConformanceError, match="concrete Gap"):
        validate_extraction_output(gap_result)

    statement_result = _valid_extraction()
    original_statement = statement_result.summary_claims[0]
    statement_result.summary_claims[0] = DerivedStatement(
        original_statement.content,
        original_statement.origins,
        original_statement.evidence_unit_ids,
    )
    with pytest.raises(ExtensionConformanceError, match="GroundedStatement"):
        validate_extraction_output(statement_result)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda result: object.__setattr__(result.units[0], "modality", "prose"), "modality"),
        (lambda result: object.__setattr__(result.units[0], "role", "unknown"), "role"),
        (
            lambda result: object.__setattr__(
                result.units[0], "role", Role.PROCEDURE
            ),
            "role must be unknown",
        ),
        (
            lambda result: object.__setattr__(
                result.relations[0], "kind", "references"
            ),
            "relation kind",
        ),
        (lambda result: setattr(result.gaps[0], "kind", "extraction"), "gap kind"),
    ],
)
def test_extraction_conformance_rejects_forged_enums_and_named_roles(
    mutation, message: str
) -> None:
    result = _valid_extraction()
    mutation(result)

    with pytest.raises(ExtensionConformanceError, match=message):
        validate_extraction_output(result)


def test_extraction_conformance_rejects_cross_source_units_and_gaps() -> None:
    cross_source_unit = _valid_extraction()
    foreign = Unit(
        "other.wfx",
        Modality.PROSE,
        "Foreign fact.",
        Origin("other.wfx", "line:1"),
    )
    cross_source_unit.units = [foreign]
    cross_source_unit.relations = []
    cross_source_unit.summary_claims = []
    cross_source_unit.meta.pop("counts")
    with pytest.raises(ExtensionConformanceError, match="unit source"):
        validate_extraction_output(cross_source_unit)

    cross_source_gap = _valid_extraction()
    cross_source_gap.gaps[0] = Gap(
        "Foreign gap.", Origin("other.wfx", "source")
    )
    with pytest.raises(ExtensionConformanceError, match="gap source"):
        validate_extraction_output(cross_source_gap)


def test_extraction_conformance_rejects_duplicate_and_dangling_graph_ids() -> None:
    duplicate = _valid_extraction()
    duplicate.units[1] = duplicate.units[0]
    duplicate.relations = []
    duplicate.summary_claims = []
    duplicate.meta.pop("counts")
    with pytest.raises(ExtensionConformanceError, match="unit ids.*unique"):
        validate_extraction_output(duplicate)

    dangling = _valid_extraction()
    object.__setattr__(dangling.relations[0], "dst", "0" * 32)
    with pytest.raises(ExtensionConformanceError, match="dangling endpoint"):
        validate_extraction_output(dangling)


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("evidence", 7, "relation evidence"),
        ("confidence", float("nan"), "relation confidence"),
        ("confidence", 1.5, "between 0 and 1"),
    ],
)
def test_extraction_conformance_rejects_bad_relation_fields(
    field: str, bad_value: object, message: str
) -> None:
    result = _valid_extraction()
    object.__setattr__(result.relations[0], field, bad_value)

    with pytest.raises(ExtensionConformanceError, match=message):
        validate_extraction_output(result)


def test_extraction_conformance_rejects_untyped_gap_items() -> None:
    result = _valid_extraction()
    list.__setitem__(result.gaps, 0, "unaddressed gap")

    with pytest.raises(ExtensionConformanceError, match="concrete Gap"):
        validate_extraction_output(result)


def test_extraction_conformance_rejects_bad_statement_evidence_and_origins() -> None:
    unknown = _valid_extraction()
    object.__setattr__(
        unknown.summary_claims[0], "evidence_unit_ids", ("0" * 32,)
    )
    with pytest.raises(ExtensionConformanceError, match="unknown evidence"):
        validate_extraction_output(unknown)

    mismatched = _valid_extraction()
    object.__setattr__(
        mismatched.summary_claims[0],
        "origins",
        (Origin("other.wfx", "line:9"),),
    )
    with pytest.raises(ExtensionConformanceError, match="exactly match"):
        validate_extraction_output(mismatched)

    wrong_container = _valid_extraction()
    object.__setattr__(
        wrong_container.summary_claims[0],
        "evidence_unit_ids",
        list(wrong_container.summary_claims[0].evidence_unit_ids),
    )
    with pytest.raises(ExtensionConformanceError, match="concrete tuple"):
        validate_extraction_output(wrong_container)


def test_extraction_conformance_rejects_duplicate_statement_ids() -> None:
    result = _valid_extraction()
    result.summary_claims.append(result.summary_claims[0])
    result.meta.pop("counts")

    with pytest.raises(ExtensionConformanceError, match="statement ids.*unique"):
        validate_extraction_output(result)


def test_extraction_conformance_rejects_noncanonical_json_without_coercion() -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise AssertionError("hostile repr must not run")

        def __str__(self) -> str:
            raise AssertionError("hostile str must not run")

    cycle: list[object] = []
    cycle.append(cycle)
    invalid_values = (
        float("nan"),
        float("inf"),
        {1: "integer key"},
        ("tuple would be coerced",),
        cycle,
        Hostile(),
        "bad surrogate \ud800",
    )

    for invalid in invalid_values:
        result = _valid_extraction()
        result.meta = {"invalid": invalid}
        with pytest.raises(ExtensionConformanceError) as raised:
            validate_extraction_output(result)
        assert len(str(raised.value)) < 180


def test_extraction_conformance_checks_optional_count_and_input_claims() -> None:
    bad_count = _valid_extraction()
    bad_count.meta["counts"]["units"] = 99
    with pytest.raises(ExtensionConformanceError, match="counts claim"):
        validate_extraction_output(bad_count)

    bad_source = _valid_extraction()
    bad_source.meta["inputs"][0]["source"] = "other.wfx"
    with pytest.raises(ExtensionConformanceError, match="source must match"):
        validate_extraction_output(bad_source)

    bad_digest = _valid_extraction()
    bad_digest.meta["inputs"][0]["sha256"] = "NOT-A-DIGEST"
    with pytest.raises(ExtensionConformanceError, match="SHA-256"):
        validate_extraction_output(bad_digest)


def test_acquisition_conformance_accepts_recursive_records_and_digest() -> None:
    acquisition = _valid_acquisition(with_digest=True)

    assert validate_acquisition_output(acquisition) is acquisition


def test_acquisition_conformance_accepts_exact_member_source_manifest() -> None:
    leaf = _valid_extraction("virtual/member.wfx")
    acquisition = CollectionAcquisition(
        "virtual",
        "virtual-collection",
        (leaf,),
        (),
        {
            "source": "virtual",
            "kind": "virtual-collection",
            "members": [leaf.source],
        },
    )

    assert validate_acquisition_output(acquisition) is acquisition


def test_acquisition_conformance_rejects_subclasses_and_bad_containers() -> None:
    class DerivedAcquisition(CollectionAcquisition):
        pass

    valid = _valid_acquisition()
    derived = DerivedAcquisition(
        valid.source,
        valid.kind,
        valid.extractions,
        valid.declines,
        valid.manifest,
    )
    with pytest.raises(ExtensionConformanceError, match="concrete.*Collection"):
        validate_acquisition_output(derived)

    wrong_container = _valid_acquisition()
    object.__setattr__(wrong_container, "extractions", list(wrong_container.extractions))
    with pytest.raises(ExtensionConformanceError, match="concrete tuple"):
        validate_acquisition_output(wrong_container)


def test_acquisition_conformance_recursively_rejects_invalid_leaves() -> None:
    acquisition = _valid_acquisition()
    object.__setattr__(acquisition.extractions[0].units[0], "modality", "prose")

    with pytest.raises(ExtensionConformanceError, match="unit modality"):
        validate_acquisition_output(acquisition)


def test_acquisition_conformance_rejects_unsorted_and_duplicate_sources() -> None:
    first = _valid_extraction("virtual/a.wfx")
    second = _valid_extraction("virtual/b.wfx")

    unsorted = _valid_acquisition()
    object.__setattr__(unsorted, "extractions", (second, first))
    with pytest.raises(ExtensionConformanceError, match="sources must be sorted"):
        validate_acquisition_output(unsorted)

    duplicate = _valid_acquisition()
    object.__setattr__(duplicate, "extractions", (first, first))
    with pytest.raises(ExtensionConformanceError, match="sources must be unique"):
        validate_acquisition_output(duplicate)


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("kind", "unsupported", "decline kind"),
        ("origin", "virtual/member.bin#source", "decline origin"),
        ("tier", True, "decline tier"),
        ("detected_kind", "", "detected kind"),
        ("details", {1: "bad key"}, "non-string dictionary key"),
    ],
)
def test_acquisition_conformance_rejects_malformed_declines(
    field: str, bad_value: object, message: str
) -> None:
    acquisition = _valid_acquisition()
    object.__setattr__(acquisition.declines[0], field, bad_value)

    with pytest.raises(ExtensionConformanceError, match=message):
        validate_acquisition_output(acquisition)


def test_acquisition_conformance_rejects_decline_subclasses() -> None:
    class DerivedDecline(MemberDecline):
        pass

    acquisition = _valid_acquisition()
    original = acquisition.declines[0]
    derived = DerivedDecline(
        original.kind,
        original.content,
        original.origin,
        original.detected_kind,
        original.tier,
        original.details,
    )
    object.__setattr__(acquisition, "declines", (derived,))

    with pytest.raises(ExtensionConformanceError, match="concrete MemberDecline"):
        validate_acquisition_output(acquisition)


def test_acquisition_conformance_rejects_noncanonical_manifest_json() -> None:
    class DerivedDict(dict):
        pass

    subclassed = _valid_acquisition()
    object.__setattr__(subclassed, "manifest", DerivedDict(subclassed.manifest))
    with pytest.raises(ExtensionConformanceError, match="manifest.*concrete"):
        validate_acquisition_output(subclassed)

    cycle: list[object] = []
    cycle.append(cycle)
    for invalid in (float("nan"), {1: "bad key"}, ("coerced tuple",), cycle):
        acquisition = _valid_acquisition()
        acquisition.manifest["invalid"] = invalid
        with pytest.raises(ExtensionConformanceError):
            validate_acquisition_output(acquisition)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda manifest: manifest.__setitem__("source", "other"),
            "manifest source",
        ),
        (
            lambda manifest: manifest["counts"].__setitem__("extracted", 99),
            "counts claim",
        ),
        (
            lambda manifest: manifest["members"][0].__setitem__("units", 99),
            "extracted record",
        ),
        (
            lambda manifest: manifest["members"].pop(),
            "omits a declined",
        ),
    ],
)
def test_acquisition_conformance_checks_optional_manifest_claims(
    mutation, message: str
) -> None:
    acquisition = _valid_acquisition()
    mutation(acquisition.manifest)

    with pytest.raises(ExtensionConformanceError, match=message):
        validate_acquisition_output(acquisition)


def test_acquisition_conformance_rejects_unknown_member_and_bad_digest() -> None:
    leaf = _valid_extraction("virtual/member.wfx")
    unknown = CollectionAcquisition(
        "virtual",
        "virtual-collection",
        (leaf,),
        (),
        {"members": ["virtual/unknown.wfx"]},
    )
    with pytest.raises(ExtensionConformanceError, match="member sources"):
        validate_acquisition_output(unknown)

    bad_digest = _valid_acquisition(with_digest=True)
    bad_digest.manifest["sha256"] = "0" * 64
    with pytest.raises(ExtensionConformanceError, match="does not match"):
        validate_acquisition_output(bad_digest)


def test_renderer_conformance_preserves_core_budget_boundary() -> None:
    def valid_builder(bundle, options):
        return f"{bundle}:{options}"

    def invalid_builder(bundle):
        return str(bundle)

    assert validate_renderer_callable(valid_builder) is valid_builder
    assert validate_renderer_output("canonical \N{SNOWMAN}\n") == "canonical ☃\n"
    with pytest.raises(ExtensionConformanceError, match="core call shape"):
        validate_renderer_callable(invalid_builder)
    with pytest.raises(ExtensionConformanceError, match="not bytes"):
        validate_renderer_output(b"not canonical text")
    with pytest.raises(ExtensionConformanceError, match="UTF-8"):
        validate_renderer_output("bad surrogate: \ud800")


def test_importing_registry_runs_no_adapter_or_cli_discovery() -> None:
    heavy_or_surfaces = (
        "autotldr.cli",
        "autotldr.router",
        "autotldr.render",
        "autotldr.collection",
        "autotldr.synthesis",
        "pymupdf",
        "openpyxl",
        "duckdb",
        "pyarrow",
        "h5py",
        "netCDF4",
        "numpy",
        "torch",
    )
    code = (
        "import sys; import autotldr.extensions; "
        f"print(','.join(name for name in {heavy_or_surfaces!r} "
        "if name in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""
