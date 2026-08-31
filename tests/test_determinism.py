"""Byte-level reproducibility invariants for extraction and shareable output.

Two regressions live here.

The first is an ordering leak. Extractors run against an immutable private
snapshot whose directory name changes on every invocation, so unit IDs computed
during extraction are temporary; the router rewrites them to logical IDs
afterwards. Any extractor that ranked its relations by those temporary IDs left
the final list in an order that varied from run to run even though the relation
set never changed.

The second is PDF byte drift. MuPDF lays a Story out in floating point and
emits coordinates that should be exactly zero as values around 1e-6, and that
accumulated noise differs between two layouts inside one process. Snapping
sub-micron magnitudes to an exact zero removes the whole class.

The cross-process half of PDF reproducibility is out of AutoTLDR's hands and is
deliberately not asserted here: ``pymupdf.Story`` lays the same HTML out
differently in different processes. See ``docs/decisions.md`` D-026.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autotldr import router
from autotldr.render import render
from autotldr.share import _canonical_content_stream, render_pdf
from autotldr.unit import Extraction, Modality, Origin, Role, Unit

TEXT_SHAPES = ("ansi", "md", "html", "json", "jsonl")


def _relation_order(result: Extraction) -> list[tuple[str, str, str, str]]:
    return [
        (relation.src, relation.dst, str(relation.kind), relation.evidence)
        for relation in result.relations
    ]


def _sqlite_fixture(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE stations ("
            "station_id TEXT PRIMARY KEY, label TEXT, reservoir_temp_c REAL, "
            "pressure_kpa REAL, commissioned TEXT)"
        )
        connection.execute(
            "CREATE TABLE readings ("
            "reading_id INTEGER PRIMARY KEY, station_id TEXT REFERENCES "
            "stations(station_id), observed_at TEXT, value REAL)"
        )
        connection.execute("CREATE VIEW hot AS SELECT * FROM stations")
        connection.commit()
    finally:
        connection.close()
    return path


def _fits_fixture(path: Path) -> Path:
    """A primary HDU plus a three-column BINTABLE, so relations exist."""

    def number(keyword: str, value: str) -> str:
        return f"{keyword:<8}= {value:>20}".ljust(80)

    def text(keyword: str, value: str) -> str:
        return f"{keyword:<8}= {value:<20}".ljust(80)

    primary = "".join(
        [
            number("SIMPLE", "T"),
            number("BITPIX", "8"),
            number("NAXIS", "0"),
            "END".ljust(80),
        ]
    ).ljust(2880)
    extension = "".join(
        [
            text("XTENSION", "'BINTABLE'"),
            number("BITPIX", "8"),
            number("NAXIS", "2"),
            number("NAXIS1", "12"),
            number("NAXIS2", "2"),
            number("PCOUNT", "0"),
            number("GCOUNT", "1"),
            number("TFIELDS", "3"),
            text("TTYPE1", "'STATION '"),
            text("TFORM1", "'1E      '"),
            text("TUNIT1", "'K       '"),
            text("TTYPE2", "'TEMP    '"),
            text("TFORM2", "'1E      '"),
            text("TTYPE3", "'PRESSURE'"),
            text("TFORM3", "'1E      '"),
            "END".ljust(80),
        ]
    )
    extension = extension.ljust(((len(extension) + 2879) // 2880) * 2880)
    path.write_bytes(
        primary.encode("ascii") + extension.encode("ascii") + bytes(2880)
    )
    return path


def _multipage_extraction() -> Extraction:
    """A projection long enough to span many PDF pages and many rule borders."""

    units = []
    for index in range(90):
        ref = f"line:{index + 1}"
        units.append(
            Unit(
                source="reservoir.md",
                modality=Modality.PROSE,
                content=(
                    f"Paragraph {index:02d} records the reservoir control loop, its "
                    "operating envelope, and the calibration inputs it consumes "
                    "before the controller is allowed to actuate anything."
                ),
                origin=Origin("reservoir.md", ref),
                role=Role.UNKNOWN,
                salience=0.4 + (index % 7) / 100,
            )
        )
    return Extraction(source="reservoir.md", kind="markdown", units=units)


@pytest.mark.parametrize("builder", [_sqlite_fixture, _fits_fixture])
def test_extraction_order_does_not_depend_on_the_private_snapshot_path(
    tmp_path: Path, builder
) -> None:
    """Two identical extractions must agree on relation order, not just content.

    The router hands each run a differently named snapshot directory, so an
    extractor that sorted by the temporary unit IDs would order its relations
    differently every time while still emitting the same relation set.
    """

    suffix = ".sqlite" if builder is _sqlite_fixture else ".fits"
    source = builder(tmp_path / f"fixture{suffix}")

    first = router.extract(source)
    second = router.extract(source)

    assert [unit.id for unit in first.units] == [unit.id for unit in second.units]
    assert _relation_order(first) == _relation_order(second)
    assert first.relations, "fixture must exercise at least one relation"
    assert [str(gap) for gap in first.gaps] == [str(gap) for gap in second.gaps]


def test_extraction_relations_are_ranked_by_canonical_unit_position(
    tmp_path: Path,
) -> None:
    """Relation order must be a function of the logical units, not of raw IDs."""

    result = router.extract(_sqlite_fixture(tmp_path / "fixture.sqlite"))
    position = {unit.id: index for index, unit in enumerate(result.units)}

    observed = [
        (position[relation.src], position[relation.dst], str(relation.kind))
        for relation in result.relations
    ]
    assert observed == sorted(observed)


@pytest.mark.parametrize("output", TEXT_SHAPES)
def test_text_shapes_are_byte_identical_for_one_extraction(output: str) -> None:
    result = _multipage_extraction()

    assert render(result, output=output) == render(result, output=output)


def test_large_pdf_projection_is_byte_identical_within_one_process() -> None:
    """A many-page PDF must not drift on re-render.

    A two-page fixture never accumulated enough layout noise to diverge, so the
    original determinism test passed while real collections did not.
    """

    pytest.importorskip("pymupdf")
    result = _multipage_extraction()

    first = render_pdf(result)
    second = render_pdf(result)

    assert first == second
    with __import__("pymupdf").open(stream=first, filetype="pdf") as document:
        assert document.page_count >= 4


def test_content_stream_canonicalization_only_touches_near_zero_reals() -> None:
    """The snap must not disturb real geometry, text, or string literals."""

    payload = (
        b"q 1 0 0 -1 0 842 cm .000018533138 -18 447 1 re f "
        b"12.00001 5.5 m 0.5 1.5 l S "
        b"BT [<0053004600440050>] TJ ET (literal .00001 text) Tj "
        b"<< /Name /Value >> .9843137 .972549 .9372549 rg"
    )
    canonical = _canonical_content_stream(payload)

    assert b"0 -18 447 1 re f" in canonical
    assert b"12.00001 5.5 m 0.5 1.5 l S" in canonical
    assert b"<0053004600440050>" in canonical
    assert b"(literal .00001 text)" in canonical
    assert b".9843137 .972549 .9372549 rg" in canonical
    assert b".000018533138" not in canonical


def test_content_stream_canonicalization_is_idempotent() -> None:
    payload = b"q .0000012 0 447 1 re f -.000004 2 m Q"
    once = _canonical_content_stream(payload)

    assert once == b"q 0 0 447 1 re f 0 2 m Q"
    assert _canonical_content_stream(once) == once
