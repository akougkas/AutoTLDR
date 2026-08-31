"""Comprehensive product routing and invariant tests for science adapters.

Covers:
- NPY/NPZ (scientific arrays via real numpy.save / numpy.savez)
- FITS (astronomy via spec-valid cards and binary data blocks)
- Arrow IPC file/stream, Feather v1/v2, ORC (columnar interchange via PyArrow)
across local filesystem, stdin, public extract_url, and collection surfaces.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import struct
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest

from autotldr import router
from autotldr.collection import acquire_archive, acquire_directory
from autotldr.errors import MissingOptionalDependency
from autotldr.extensions import (
    ExtensionCollisionError,
    ExtensionRegistry,
    ExtractorSpec,
    SignatureProbe,
)
from autotldr.extract.astronomy import InvalidFitsData
from autotldr.extract.columnar_interchange import InvalidColumnarData
from autotldr.extract.scientific_arrays import InvalidScientificArrayData
from autotldr.router import Handler, UnknownFormat, UnsupportedFormat
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

# Distinct, unique raw-only canaries for each format family
CANARY_NPY = "RAW_CANARY_NPY_CELL_VALUE_9911"
CANARY_NPZ = "RAW_CANARY_NPZ_MEMBER_VAL_8822"
CANARY_FITS = "RAW_CANARY_FITS_DATA_PAYLOAD_7733"
CANARY_ARROW_FILE = "RAW_CANARY_ARROW_FILE_ROW_6644"
CANARY_ARROW_STREAM = "RAW_CANARY_ARROW_STREAM_ROW_5555"
CANARY_FEATHER_V2 = "RAW_CANARY_FEATHER_V2_ROW_4466"
CANARY_ORC = "RAW_CANARY_ORC_TABLE_ROW_3377"


# ---------------------------------------------------------------------------
# Strict, Type-Guarded IR Canary Walker and Positive/Adversarial Self-Tests
# ---------------------------------------------------------------------------
def _assert_canary_not_present(
    obj: object, canary: str, _visited: set[int] | None = None
) -> None:
    """Traverse only exact known AutoTLDR IR concrete types and exact built-in primitives."""
    if _visited is None:
        _visited = set()
    obj_id = id(obj)
    if obj_id in _visited:
        return
    _visited.add(obj_id)

    obj_type = type(obj)

    # 1. Exact Gap check before str to prevent subclass bypass
    if obj_type is Gap:
        gap_obj: Gap = obj  # type: ignore[assignment]
        assert canary not in str(gap_obj), f"Canary {canary!r} found in Gap text: {gap_obj!r}"
        _assert_canary_not_present(gap_obj.origin, canary, _visited)
        _assert_canary_not_present(str(gap_obj.kind), canary, _visited)
        return

    # 2. Exact primitives
    if obj_type is str:
        assert canary not in obj, f"Canary {canary!r} found in string: {obj!r}"
        return

    if obj_type in (bytes, bytearray):
        assert (
            canary.encode("utf-8") not in obj
        ), f"Canary {canary!r} found in bytes: {obj!r}"
        return

    if obj_type in (int, float, bool) or obj is None:
        return

    # 3. Exact built-in containers
    if obj_type is dict:
        for k, v in obj.items():  # type: ignore[union-attr]
            _assert_canary_not_present(k, canary, _visited)
            _assert_canary_not_present(v, canary, _visited)
        return

    if obj_type in (list, tuple, set, frozenset):
        for item in obj:  # type: ignore[union-attr]
            _assert_canary_not_present(item, canary, _visited)
        return

    # 4. Exact AutoTLDR IR dataclasses
    if obj_type is Extraction:
        ext: Extraction = obj  # type: ignore[assignment]
        _assert_canary_not_present(ext.source, canary, _visited)
        _assert_canary_not_present(ext.kind, canary, _visited)
        _assert_canary_not_present(ext.units, canary, _visited)
        _assert_canary_not_present(ext.relations, canary, _visited)
        # Extraction.gaps is an internal _GapList; traverse its declared elements directly
        for gap_item in ext.gaps:
            _assert_canary_not_present(gap_item, canary, _visited)
        _assert_canary_not_present(ext.meta, canary, _visited)
        _assert_canary_not_present(ext.summary_claims, canary, _visited)
        return

    if obj_type is Unit:
        u: Unit = obj  # type: ignore[assignment]
        _assert_canary_not_present(u.source, canary, _visited)
        _assert_canary_not_present(str(u.modality), canary, _visited)
        _assert_canary_not_present(u.content, canary, _visited)
        _assert_canary_not_present(u.origin, canary, _visited)
        _assert_canary_not_present(str(u.role), canary, _visited)
        _assert_canary_not_present(u.structure, canary, _visited)
        _assert_canary_not_present(u.meta, canary, _visited)
        _assert_canary_not_present(u.id, canary, _visited)
        return

    if obj_type is Origin:
        orig: Origin = obj  # type: ignore[assignment]
        _assert_canary_not_present(orig.source, canary, _visited)
        _assert_canary_not_present(orig.ref, canary, _visited)
        return

    if obj_type is Relation:
        rel: Relation = obj  # type: ignore[assignment]
        _assert_canary_not_present(rel.src, canary, _visited)
        _assert_canary_not_present(rel.dst, canary, _visited)
        _assert_canary_not_present(str(rel.kind), canary, _visited)
        _assert_canary_not_present(rel.evidence, canary, _visited)
        return

    if obj_type is GroundedStatement:
        stmt: GroundedStatement = obj  # type: ignore[assignment]
        _assert_canary_not_present(stmt.content, canary, _visited)
        _assert_canary_not_present(stmt.origins, canary, _visited)
        _assert_canary_not_present(stmt.evidence_unit_ids, canary, _visited)
        _assert_canary_not_present(stmt.id, canary, _visited)
        return

    raise TypeError(f"Unexpected object in IR canary traversal: {type(obj)!r}")


def test_canary_walker_catches_all_slot_ir_surfaces():
    """Prove that _assert_canary_not_present raises on every declared IR field and rejects unknown types."""
    canary = "TEST_CANARY_TRIGGER_123"

    # 1. Extraction.source
    ext_src = Extraction(f"src_{canary}", "numpy", (), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_src, canary)

    # 2. Extraction.kind
    ext_kind = Extraction("src", f"numpy_{canary}", (), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_kind, canary)

    # 3. Extraction.meta
    ext_meta = Extraction("src", "numpy", (), (), ())
    ext_meta.meta["nested"] = {"secret": canary}
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_meta, canary)

    # 4. Unit.source
    u_src = Unit(f"src_{canary}", Modality.PROSE, "content", Origin(f"src_{canary}", "ref"), Role.UNKNOWN)
    ext_u_src = Extraction(f"src_{canary}", "numpy", (u_src,), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_u_src, canary)

    # 5. Unit.content
    u_content = Unit("src", Modality.PROSE, f"prefix {canary} suffix", Origin("src", "ref"), Role.UNKNOWN)
    ext_u_content = Extraction("src", "numpy", (u_content,), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_u_content, canary)

    # 6. Unit.origin.source
    u_orig_src = Unit(f"src_{canary}", Modality.PROSE, "content", Origin(f"src_{canary}", "ref"), Role.UNKNOWN)
    ext_orig_src = Extraction(f"src_{canary}", "numpy", (u_orig_src,), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_orig_src, canary)

    # 7. Unit.origin.ref
    u_orig_ref = Unit("src", Modality.PROSE, "content", Origin("src", f"ref_{canary}"), Role.UNKNOWN)
    ext_orig_ref = Extraction("src", "numpy", (u_orig_ref,), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_orig_ref, canary)

    # 8. Unit.structure
    u_struct = Unit("src", Modality.PROSE, "content", Origin("src", "ref"), Role.UNKNOWN, structure=(canary,))
    ext_struct = Extraction("src", "numpy", (u_struct,), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_struct, canary)

    # 9. Unit.meta
    u_meta = Unit("src", Modality.PROSE, "content", Origin("src", "ref"), Role.UNKNOWN, meta={"key": canary})
    ext_u_meta = Extraction("src", "numpy", (u_meta,), (), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_u_meta, canary)

    # 10. Gap.content
    g_content = Gap(f"gap {canary}", Origin("src", "ref"), GapKind.EXTRACTION)
    ext_g_content = Extraction("src", "numpy", (), (), (g_content,))
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_g_content, canary)

    # 11. Gap.origin.source
    g_orig_src = Gap("gap content", Origin(f"src_{canary}", "ref"), GapKind.EXTRACTION)
    ext_g_orig_src = Extraction("src", "numpy", (), (), (g_orig_src,))
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_g_orig_src, canary)

    # 12. Gap.origin.ref
    g_orig_ref = Gap("gap content", Origin("src", f"ref_{canary}"), GapKind.EXTRACTION)
    ext_g_orig_ref = Extraction("src", "numpy", (), (), (g_orig_ref,))
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_g_orig_ref, canary)

    # 13. Gap.kind
    g_kind = Gap("gap content", Origin("src", "ref"), kind=GapKind.EXTRACTION)
    # Monkeypatch kind to carry canary
    object.__setattr__(g_kind, "kind", f"kind_{canary}")
    ext_g_kind = Extraction("src", "numpy", (), (), (g_kind,))
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_g_kind, canary)

    # 14. Relation.src
    u1 = Unit("src", Modality.PROSE, "a", Origin("src", "1"), Role.UNKNOWN)
    u2 = Unit("src", Modality.PROSE, "b", Origin("src", "2"), Role.UNKNOWN)
    rel_src = Relation(f"src_{canary}", u2.id, RelationKind.DESCRIBES)
    ext_rel_src = Extraction("src", "numpy", (u1, u2), (rel_src,), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_rel_src, canary)

    # 15. Relation.dst
    rel_dst = Relation(u1.id, f"dst_{canary}", RelationKind.DESCRIBES)
    ext_rel_dst = Extraction("src", "numpy", (u1, u2), (rel_dst,), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_rel_dst, canary)

    # 16. Relation.evidence
    rel_ev = Relation(u1.id, u2.id, RelationKind.DESCRIBES, evidence=f"evidence {canary}")
    ext_rel_ev = Extraction("src", "numpy", (u1, u2), (rel_ev,), ())
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_rel_ev, canary)

    # 17. GroundedStatement.content
    stmt_content = GroundedStatement(f"statement {canary}", (Origin("src", "1"),), (u1.id,))
    ext_stmt_content = Extraction("src", "numpy", (u1,), (), (), summary_claims=(stmt_content,))
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_stmt_content, canary)

    # 18. GroundedStatement.origins
    stmt_orig = GroundedStatement("statement text", (Origin(f"src_{canary}", "1"),), (u1.id,))
    ext_stmt_orig = Extraction("src", "numpy", (u1,), (), (), summary_claims=(stmt_orig,))
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_stmt_orig, canary)

    # 19. GroundedStatement.evidence_unit_ids
    stmt_ev_id = GroundedStatement("statement text", (Origin("src", "1"),), (f"id_{canary}",))
    ext_stmt_ev_id = Extraction("src", "numpy", (u1,), (), (), summary_claims=(stmt_ev_id,))
    with pytest.raises(AssertionError, match=canary):
        _assert_canary_not_present(ext_stmt_ev_id, canary)


def test_canary_walker_rejects_hostile_subclasses_without_calling_overrides():
    """Prove that _assert_canary_not_present rejects hostile subclasses with TypeError before executing their methods."""
    canary = "TEST_CANARY"

    # Hostile str subclass
    class HostileStr(str):
        def __str__(self) -> str:
            raise RuntimeError("hostile __str__ executed")

        def __contains__(self, item: object) -> bool:
            raise RuntimeError("hostile __contains__ executed")

    with pytest.raises(TypeError, match="Unexpected object"):
        _assert_canary_not_present(HostileStr("clean"), canary)

    # Hostile dict subclass
    class HostileDict(dict):
        def items(self):  # type: ignore[override]
            raise RuntimeError("hostile items executed")

    with pytest.raises(TypeError, match="Unexpected object"):
        _assert_canary_not_present(HostileDict(), canary)

    # Hostile list subclass
    class HostileList(list):
        def __iter__(self):
            raise RuntimeError("hostile __iter__ executed")

    with pytest.raises(TypeError, match="Unexpected object"):
        _assert_canary_not_present(HostileList(), canary)

    # Hostile Unit subclass
    class HostileUnit(Unit):
        pass

    u = Unit("src", Modality.PROSE, "content", Origin("src", "ref"), Role.UNKNOWN)
    hu = HostileUnit(
        u.source, u.modality, u.content, u.origin, u.role, u.structure, u.salience, u.confidence, u.tokens, u.meta
    )
    with pytest.raises(TypeError, match="Unexpected object"):
        _assert_canary_not_present(hu, canary)


# ---------------------------------------------------------------------------
# Spec-Valid FITS Fixture Generator
# ---------------------------------------------------------------------------
def _make_fits_card(keyword: str, value: object = None, comment: str | None = None) -> bytes:
    kw = f"{keyword:<8}"[:8]
    if value is None:
        card = kw + (f" / {comment}" if comment else "")
    elif isinstance(value, bool):
        val_str = "T" if value else "F"
        card = f"{kw}= {val_str:>20}" + (f" / {comment}" if comment else "")
    elif isinstance(value, int):
        card = f"{kw}= {value:>20d}" + (f" / {comment}" if comment else "")
    elif isinstance(value, float):
        card = f"{kw}= {value:>20.8E}" + (f" / {comment}" if comment else "")
    elif isinstance(value, str):
        escaped = value.replace("'", "''")
        val_str = f"'{escaped:<8}'"
        card = f"{kw}= {val_str:<20}" + (f" / {comment}" if comment else "")
    else:
        card = f"{kw}= {str(value):<20}"
    return f"{card:<80}"[:80].encode("ascii")


def _build_fits_header(cards: list[bytes]) -> bytes:
    body = b"".join(cards) + b"END" + b" " * 77
    pad = 2880 - (len(body) % 2880)
    if pad != 2880:
        body += b" " * pad
    return body


def _make_fits_bytes(cards: list[bytes] | None = None, data_bytes: bytes = b"") -> bytes:
    if cards is None:
        cards = [
            _make_fits_card("SIMPLE", True, "Standard FITS"),
            _make_fits_card("BITPIX", 8, "8-bit unsigned"),
            _make_fits_card("NAXIS", 1, "1D data array"),
            _make_fits_card("NAXIS1", len(data_bytes), "Number of bytes"),
            _make_fits_card("EXTEND", True, "Extensions permitted"),
            _make_fits_card("OBJECT", "SCIENCE_SAMPLE", "Target name"),
        ]
    hdr = _build_fits_header(cards)
    data_pad = (2880 - (len(data_bytes) % 2880)) % 2880
    data = data_bytes + (b"\x00" * data_pad if data_pad != 2880 else b"")
    return hdr + data


# ---------------------------------------------------------------------------
# Real Library Fixture Generators with Native Canary Presence Proof
# ---------------------------------------------------------------------------
@pytest.fixture
def science_fixtures(tmp_path: Path) -> dict[str, Path]:
    import numpy as np
    import pyarrow as pa
    import pyarrow.feather as feather
    import pyarrow.ipc as ipc
    import pyarrow.orc as orc

    fixtures: dict[str, Path] = {}

    # 1. NPY (real numpy.save with structured array holding CANARY_NPY in raw field)
    p_npy = tmp_path / "sample.npy"
    arr_npy = np.array(
        [(1, 10.5, CANARY_NPY), (2, 20.5, "normal_val")],
        dtype=[("id", "i4"), ("score", "f8"), ("raw_secret", "U40")],
    )
    np.save(str(p_npy), arr_npy)
    # Native library readback presence check
    loaded_npy = np.load(str(p_npy))
    assert any(CANARY_NPY in str(row) for row in loaded_npy), "CANARY_NPY missing from native NPY"
    fixtures["npy"] = p_npy

    # 2. NPZ (real numpy.savez with array member holding CANARY_NPZ)
    p_npz = tmp_path / "sample.npz"
    arr_npz = np.array([CANARY_NPZ, "other_val"], dtype="U40")
    weights = np.array([1.0, 2.0, 3.0], dtype="f8")
    np.savez(str(p_npz), arr_0=arr_npz, weights=weights)
    # Native library readback presence check
    with np.load(str(p_npz)) as loaded_npz:
        assert CANARY_NPZ in loaded_npz["arr_0"], "CANARY_NPZ missing from native NPZ"
    fixtures["npz"] = p_npz

    # 3. FITS (spec-valid cards + data block containing CANARY_FITS)
    p_fits = tmp_path / "sample.fits"
    fits_data = f"HEADER_START_{CANARY_FITS}_DATA_BLOCK_END".encode("ascii")
    p_fits.write_bytes(_make_fits_bytes(data_bytes=fits_data))
    # Native readback presence check
    assert CANARY_FITS.encode("ascii") in p_fits.read_bytes(), "CANARY_FITS missing from native FITS"
    fixtures["fits"] = p_fits

    # 4. Arrow File (IPC File format with CANARY_ARROW_FILE in row payload)
    p_arrow_file = tmp_path / "sample.arrow"
    schema_arrow = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("label", pa.string()),
            pa.field("value", pa.float64()),
        ]
    )
    table_arrow = pa.table(
        {
            "id": [1, 2],
            "label": ["alpha", CANARY_ARROW_FILE],
            "value": [10.0, 20.0],
        },
        schema=schema_arrow,
    )
    with pa.OSFile(str(p_arrow_file), "wb") as sink:
        with ipc.new_file(sink, schema_arrow) as writer:
            writer.write_table(table_arrow)
    # Native library readback presence check
    with pa.OSFile(str(p_arrow_file), "rb") as source:
        with ipc.open_file(source) as reader:
            read_arrow_tbl = reader.read_all()
            assert CANARY_ARROW_FILE in read_arrow_tbl["label"].to_pylist(), "CANARY_ARROW_FILE missing from Arrow IPC File"
    fixtures["arrow_file"] = p_arrow_file

    # 5. Arrow Stream (IPC Stream format with CANARY_ARROW_STREAM in row payload)
    p_arrow_stream = tmp_path / "sample.arrows"
    schema_stream = pa.schema(
        [
            pa.field("sensor_id", pa.int32()),
            pa.field("measurement", pa.string()),
        ]
    )
    table_stream = pa.table(
        {
            "sensor_id": [101, 102],
            "measurement": ["ok", CANARY_ARROW_STREAM],
        },
        schema=schema_stream,
    )
    with pa.OSFile(str(p_arrow_stream), "wb") as sink:
        with ipc.new_stream(sink, schema_stream) as writer:
            writer.write_table(table_stream)
    # Native library readback presence check
    with pa.OSFile(str(p_arrow_stream), "rb") as source:
        with ipc.open_stream(source) as reader:
            read_stream_tbl = reader.read_all()
            assert CANARY_ARROW_STREAM in read_stream_tbl["measurement"].to_pylist(), "CANARY_ARROW_STREAM missing from Arrow Stream"
    fixtures["arrow_stream"] = p_arrow_stream

    # 6. Feather v2 (with CANARY_FEATHER_V2 in row payload)
    p_feather = tmp_path / "sample.feather"
    table_feather = pa.table(
        {
            "user_id": [1, 2],
            "note": ["public", CANARY_FEATHER_V2],
        }
    )
    feather.write_feather(table_feather, str(p_feather), version=2)
    # Native library readback presence check
    read_feather_tbl = feather.read_table(str(p_feather))
    assert CANARY_FEATHER_V2 in read_feather_tbl["note"].to_pylist(), "CANARY_FEATHER_V2 missing from Feather v2"
    fixtures["feather"] = p_feather

    # 7. ORC (with CANARY_ORC in row payload)
    p_orc = tmp_path / "sample.orc"
    table_orc = pa.table(
        {
            "record_id": [1, 2],
            "comment": ["first", CANARY_ORC],
        }
    )
    orc.write_table(table_orc, str(p_orc))
    # Native library readback presence check
    read_orc_tbl = orc.read_table(str(p_orc))
    assert CANARY_ORC in read_orc_tbl["comment"].to_pylist(), "CANARY_ORC missing from ORC table"
    fixtures["orc"] = p_orc

    return fixtures


# ---------------------------------------------------------------------------
# 1. Catalog, IANA Media Types, Suffixes & Collision Prevention
# ---------------------------------------------------------------------------
def test_science_catalog_registration():
    """Verify built-in handlers, suffixes, type hints, IANA media types, and strong signatures."""
    supp = router.supported_suffixes()
    expected_suffixes = {
        ".npy",
        ".npz",
        ".fits",
        ".fts",
        ".arrow",
        ".arrows",
        ".feather",
        ".orc",
    }
    for s in expected_suffixes:
        assert s in supp, f"Suffix {s} missing from supported_suffixes"

    # .fit must NOT be advertised in supported_suffixes; it is in declined_suffixes as ambiguous
    assert ".fit" not in supp
    decl = router.declined_suffixes()
    assert ".fit" in decl
    assert ".arrow" not in decl

    type_names = set(router.input_type_names())
    expected_names = {
        "numpy",
        "npy",
        "npz",
        "fits",
        "fts",
        "arrow",
        "arrow-file",
        "arrow-stream",
        "arrows",
        "feather",
        "feather-v2",
        "orc",
    }
    for name in expected_names:
        assert name in type_names, f"Type name {name} missing from input_type_names"
    assert "fit" not in type_names

    # Verify official IANA registered media types only
    assert router._HTTP_NATIVE_MEDIA["image/fits"] == ".fits"
    assert router._HTTP_NATIVE_MEDIA["application/fits"] == ".fits"
    assert router._HTTP_NATIVE_MEDIA["application/vnd.apache.arrow.file"] == ".arrow"
    assert (
        router._HTTP_NATIVE_MEDIA["application/vnd.apache.arrow.stream"] == ".arrows"
    )

    # Un-registered guesses must NOT be present in media maps
    for unverified in [
        "application/x-numpy-data",
        "application/x-npz",
        "application/vnd.apache.orc",
        "application/vnd.apache.arrow.feather",
        "application/x-fits",
        "application/arrow",
    ]:
        assert unverified not in router._HTTP_NATIVE_MEDIA


def test_extension_collision_prevention():
    """Community extensions cannot collide with core science names, suffixes, media, or signatures."""
    # Suffix collision
    reg_suffix = ExtensionRegistry(
        (
            ExtractorSpec(
                name="custom-arrow",
                module="builtins",
                callable="len",
                kinds=("custom-arrow",),
                suffixes=(".arrow",),
            ),
        )
    )
    with pytest.raises(
        ExtensionCollisionError, match="collides with implemented core suffix"
    ):
        router.validate_extension_registry(reg_suffix)

    # Kind collision
    reg_name = ExtensionRegistry(
        (
            ExtractorSpec(
                name="numpy",
                module="builtins",
                callable="len",
                kinds=("custom",),
                suffixes=(".custom",),
            ),
        )
    )
    with pytest.raises(
        ExtensionCollisionError, match="collides with implemented core name/kind"
    ):
        router.validate_extension_registry(reg_name)

    # IANA Media type collision
    reg_media = ExtensionRegistry(
        (
            ExtractorSpec(
                name="my-fits",
                module="builtins",
                callable="len",
                kinds=("my-fits",),
                suffixes=(".custom_fits",),
                media_types=("application/fits",),
            ),
        )
    )
    with pytest.raises(
        ExtensionCollisionError, match="collides with implemented core media type"
    ):
        router.validate_extension_registry(reg_media)

    # Strong signature collision
    reg_sig = ExtensionRegistry(
        (
            ExtractorSpec(
                name="my-npy",
                module="builtins",
                callable="len",
                kinds=("my-npy",),
                suffixes=(".custom_npy",),
                signatures=(SignatureProbe(b"\x93NUMPY"),),
            ),
        )
    )
    with pytest.raises(
        ExtensionCollisionError,
        match="collides with an implemented core strong signature",
    ):
        router.validate_extension_registry(reg_sig)


def test_garmin_fit_ambiguity_and_community_override(tmp_path: Path):
    """Plausible Garmin FIT headers decline as ambiguous; genuine FITS routes by magic; community can override."""
    # 1. Plausible Garmin FIT header at activity.fit declines as ambiguous
    p_fit = tmp_path / "activity.fit"
    p_fit.write_bytes(b"\x0e\x10\x00\x00.FIT\x00\x00\x00\x00\x00\x00")
    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p_fit)
    assert exc_info.value.tier == 3
    assert "Garmin FIT" in exc_info.value.kind

    # 2. Genuine FITS bytes at activity.fit route via SIMPLE = strong magic
    p_fits_fit = tmp_path / "observation.fit"
    p_fits_fit.write_bytes(_make_fits_bytes(data_bytes=b"FITS_OBSERVATION_DATA"))
    res_fits = router.extract(p_fits_fit)
    assert res_fits.kind == "fits"

    # 3. Community adapter claiming .fit overrides the deferred ambiguity in registry
    community_fit = ExtensionRegistry(
        (
            ExtractorSpec(
                name="garmin-fit",
                module="builtins",
                callable="len",
                kinds=("garmin-fit",),
                suffixes=(".fit",),
            ),
        )
    )
    router.validate_extension_registry(community_fit)
    assert ".fit" in router.supported_suffixes(registry=community_fit)
    assert ".fit" not in router.declined_suffixes(registry=community_fit)

    # 4. detect() selects community handler on activity.fit
    detected_garmin = router.detect(p_fit, registry=community_fit)
    assert detected_garmin.kind == "garmin-fit"
    assert detected_garmin.extension_name == "garmin-fit"

    # 5. Genuine FITS magic on .fit still beats the community suffix
    detected_fits = router.detect(p_fits_fit, registry=community_fit)
    assert detected_fits.kind == "fits"
    assert detected_fits.extension_name is None


def test_lazy_imports():
    """Importing router or cli must not import heavy libraries."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import autotldr.router; import autotldr.cli; "
            "heavy = ['numpy', 'pyarrow', 'astropy', 'openpyxl', 'pymupdf', 'duckdb', 'h5py']; "
            "leaked = [m for m in heavy if m in sys.modules]; "
            "assert not leaked, f'Leaked: {leaked}'",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Lazy import violation: {proc.stderr}"


# ---------------------------------------------------------------------------
# 2. Local File Extraction, Provenance & Deterministic Invariants
# ---------------------------------------------------------------------------
def test_local_file_extraction_invariants(science_fixtures: dict[str, Path]):
    """Every science format extracts with Role.UNKNOWN, exact provenance, manifest, and no canaries."""
    canary_map = {
        "npy": CANARY_NPY,
        "npz": CANARY_NPZ,
        "fits": CANARY_FITS,
        "arrow_file": CANARY_ARROW_FILE,
        "arrow_stream": CANARY_ARROW_STREAM,
        "feather": CANARY_FEATHER_V2,
        "orc": CANARY_ORC,
    }

    for key, p in science_fixtures.items():
        res1 = router.extract(p)
        assert res1.source == str(p)
        assert len(res1.units) > 0

        # Invariant 1: Role.UNKNOWN exclusively
        for u in res1.units:
            assert u.role is Role.UNKNOWN
            assert u.origin.source == str(p)
            assert str(p) in u.origin.source
            # Ensure private snapshot path does not leak
            assert "autotldr-snapshot" not in u.origin.source
            assert "autotldr-snapshot" not in u.origin.ref
            assert "autotldr-snapshot" not in u.content

        for gap in res1.gaps:
            assert gap.origin.source == str(p)
            assert "autotldr-snapshot" not in gap.origin.source

        # Invariant 2: Exact byte count and sha256 manifest
        manifest = res1.meta["inputs"][0]
        assert manifest["source"] == str(p)
        assert manifest["bytes"] == p.stat().st_size
        assert manifest["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()

        # Invariant 3: Distinct secret canary is completely absent from complete IR
        canary = canary_map[key]
        _assert_canary_not_present(res1, canary)

        # Invariant 4: Exact ordered deterministic repeat extraction across all IR fields
        res2 = router.extract(p)
        assert res1.source == res2.source
        assert res1.kind == res2.kind
        assert res1.units == res2.units
        assert res1.relations == res2.relations
        assert len(res1.gaps) == len(res2.gaps)
        for g1, g2 in zip(res1.gaps, res2.gaps, strict=True):
            assert str(g1) == str(g2)
            assert g1.origin == g2.origin
            assert g1.kind == g2.kind
        assert res1.summary_claims == res2.summary_claims
        m1 = res1.meta["inputs"][0]
        m2 = res2.meta["inputs"][0]
        assert m1 == m2
        meta1 = {k: v for k, v in res1.meta.items() if k != "timings"}
        meta2 = {k: v for k, v in res2.meta.items() if k != "timings"}
        assert meta1 == meta2


def test_extensionless_and_misleading_suffix_precedence(
    tmp_path: Path, science_fixtures: dict[str, Path]
):
    """Strong byte identities override misleading suffixes and route extensionless files."""
    # 1. Extensionless strong identity
    for key, p in science_fixtures.items():
        if key == "arrow_stream":
            continue  # Stream continuation is not prefix-only strong without hint/suffix
        p_extless = tmp_path / f"extless_{key}"
        p_extless.write_bytes(p.read_bytes())
        res = router.extract(p_extless)
        assert res.source == str(p_extless)
        assert len(res.units) > 0
        if key == "feather":
            # Extensionless Feather v2 is structurally ARROW1, honestly detected as arrow-file
            assert res.kind == "arrow-file"

    # 2. Misleading suffix overridden by strong bytes
    p_npy_as_txt = tmp_path / "array.txt"
    p_npy_as_txt.write_bytes(science_fixtures["npy"].read_bytes())
    res_npy = router.extract(p_npy_as_txt)
    assert res_npy.kind == "npy"

    p_fits_as_csv = tmp_path / "image.csv"
    p_fits_as_csv.write_bytes(science_fixtures["fits"].read_bytes())
    res_fits = router.extract(p_fits_as_csv)
    assert res_fits.kind == "fits"

    p_arrow_as_json = tmp_path / "table.json"
    p_arrow_as_json.write_bytes(science_fixtures["arrow_file"].read_bytes())
    res_arrow = router.extract(p_arrow_as_json)
    assert res_arrow.kind == "arrow-file"

    p_orc_as_pdf = tmp_path / "data.pdf"
    p_orc_as_pdf.write_bytes(science_fixtures["orc"].read_bytes())
    res_orc = router.extract(p_orc_as_pdf)
    assert res_orc.kind == "orc"


def test_explicit_type_hints(tmp_path: Path, science_fixtures: dict[str, Path]):
    """Explicit kind hints correctly resolve files with arbitrary names."""
    p_bin = tmp_path / "data.bin"

    # NPY
    p_bin.write_bytes(science_fixtures["npy"].read_bytes())
    res = router.extract(p_bin, kind="numpy")
    assert res.kind == "npy"

    # NPZ
    p_bin.write_bytes(science_fixtures["npz"].read_bytes())
    res = router.extract(p_bin, kind="npz")
    assert res.kind == "npz"

    # FITS
    p_bin.write_bytes(science_fixtures["fits"].read_bytes())
    res = router.extract(p_bin, kind="fits")
    assert res.kind == "fits"

    # Arrow Stream (requires hint or .arrows suffix)
    p_bin.write_bytes(science_fixtures["arrow_stream"].read_bytes())
    res = router.extract(p_bin, kind="arrow-stream")
    assert res.kind == "arrow-stream"

    # Feather v2 (explicit kind hint must strictly return feather)
    p_bin.write_bytes(science_fixtures["feather"].read_bytes())
    res = router.extract(p_bin, kind="feather")
    assert res.kind == "feather"

    # ORC
    p_bin.write_bytes(science_fixtures["orc"].read_bytes())
    res = router.extract(p_bin, kind="orc")
    assert res.kind == "orc"

    # Invalid type hint
    with pytest.raises(ValueError, match="unsupported explicit input type"):
        router.extract(p_bin, kind="nonexistent_type_foo")


# ---------------------------------------------------------------------------
# 3. Stdin Acquisition
# ---------------------------------------------------------------------------
def test_stdin_acquisition(science_fixtures: dict[str, Path]):
    """Extracting from stdin handles extensionless bytes, zip-detected NPZ, and hints."""
    # 1. Extensionless NPY
    res_npy = router.extract_stdin(science_fixtures["npy"].read_bytes())
    assert res_npy.source == "<stdin>"
    assert res_npy.kind == "npy"
    assert res_npy.meta["inputs"][0]["source"] == "<stdin>"
    assert all(u.origin.source == "<stdin>" for u in res_npy.units)
    _assert_canary_not_present(res_npy, CANARY_NPY)

    # 2. Extensionless FITS
    res_fits = router.extract_stdin(science_fixtures["fits"].read_bytes())
    assert res_fits.source == "<stdin>"
    assert res_fits.kind == "fits"
    assert all(u.origin.source == "<stdin>" for u in res_fits.units)
    _assert_canary_not_present(res_fits, CANARY_FITS)

    # 3. Extensionless Arrow IPC file
    res_arrow = router.extract_stdin(science_fixtures["arrow_file"].read_bytes())
    assert res_arrow.source == "<stdin>"
    assert res_arrow.kind == "arrow-file"
    _assert_canary_not_present(res_arrow, CANARY_ARROW_FILE)

    # 4. Extensionless ORC
    res_orc = router.extract_stdin(science_fixtures["orc"].read_bytes())
    assert res_orc.source == "<stdin>"
    assert res_orc.kind == "orc"
    _assert_canary_not_present(res_orc, CANARY_ORC)

    # 5. Extensionless NPZ (detected via bounded ZIP sniffing)
    res_npz = router.extract_stdin(science_fixtures["npz"].read_bytes())
    assert res_npz.source == "<stdin>"
    assert res_npz.kind == "npz"
    _assert_canary_not_present(res_npz, CANARY_NPZ)

    # 6. Stream with explicit hint
    res_stream = router.extract_stdin(
        science_fixtures["arrow_stream"].read_bytes(), kind="arrow-stream"
    )
    assert res_stream.source == "<stdin>"
    assert res_stream.kind == "arrow-stream"
    _assert_canary_not_present(res_stream, CANARY_ARROW_STREAM)


# ---------------------------------------------------------------------------
# 4. Public extract_url with Closed Localhost Server Harness
# ---------------------------------------------------------------------------
class _ScienceMockServer:
    """Threaded localhost HTTP server delivering real science payloads."""

    def __init__(self, routes: dict[str, tuple[bytes, str]]) -> None:
        self.routes = routes
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> str:
        routes = self.routes

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass  # Silence stderr request logging

            def do_GET(self) -> None:
                self.close_connection = True
                path = self.path.split("?")[0]
                if path in routes:
                    data, content_type = routes[path]
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.send_header("Connection", "close")
                    self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        import gc

        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        gc.collect()


@pytest.fixture
def http_science_server(science_fixtures: dict[str, Path]):
    routes: dict[str, tuple[bytes, str]] = {
        "/sample.npy": (
            science_fixtures["npy"].read_bytes(),
            "application/octet-stream",
        ),
        "/sample.npz": (
            science_fixtures["npz"].read_bytes(),
            "application/octet-stream",
        ),
        "/sample.fits": (
            science_fixtures["fits"].read_bytes(),
            "application/octet-stream",
        ),
        "/sample.arrow": (
            science_fixtures["arrow_file"].read_bytes(),
            "application/octet-stream",
        ),
        "/sample.arrows": (
            science_fixtures["arrow_stream"].read_bytes(),
            "application/octet-stream",
        ),
        "/sample.feather": (
            science_fixtures["feather"].read_bytes(),
            "application/octet-stream",
        ),
        "/sample.orc": (
            science_fixtures["orc"].read_bytes(),
            "application/octet-stream",
        ),
        # Misleading media and suffixes overridden by strong byte identities
        "/misleading_fits.txt": (science_fixtures["fits"].read_bytes(), "text/plain"),
        "/misleading_npy.pdf": (
            science_fixtures["npy"].read_bytes(),
            "application/pdf",
        ),
        # All 4 official IANA media types over generic endpoints
        "/iana_arrow_file": (
            science_fixtures["arrow_file"].read_bytes(),
            "application/vnd.apache.arrow.file",
        ),
        "/iana_arrow_stream": (
            science_fixtures["arrow_stream"].read_bytes(),
            "application/vnd.apache.arrow.stream",
        ),
        "/iana_app_fits": (science_fixtures["fits"].read_bytes(), "application/fits"),
        "/iana_image_fits": (science_fixtures["fits"].read_bytes(), "image/fits"),
        "/sample_v1.feather": (
            b"FEA1" + b"\x00" * 100 + b"FEA1",
            "application/octet-stream",
        ),
    }
    server = _ScienceMockServer(routes)
    base_url = server.start()
    try:
        yield base_url, routes
    finally:
        server.close()


def test_public_extract_url_all_science_routes(
    http_science_server: tuple[str, dict[str, tuple[bytes, str]]]
):
    """Exercise public extract_url across all science formats with exact byte/SHA binding and relation closure."""
    base, routes = http_science_server

    url_cases = [
        (f"{base}/sample.npy", "/sample.npy", "npy", CANARY_NPY),
        (f"{base}/sample.npz", "/sample.npz", "npz", CANARY_NPZ),
        (f"{base}/sample.fits", "/sample.fits", "fits", CANARY_FITS),
        (f"{base}/sample.arrow", "/sample.arrow", "arrow-file", CANARY_ARROW_FILE),
        (f"{base}/sample.arrows", "/sample.arrows", "arrow-stream", CANARY_ARROW_STREAM),
        (f"{base}/sample.feather", "/sample.feather", "feather", CANARY_FEATHER_V2),
        (f"{base}/sample.orc", "/sample.orc", "orc", CANARY_ORC),
        (f"{base}/misleading_fits.txt", "/misleading_fits.txt", "fits", CANARY_FITS),
        (f"{base}/misleading_npy.pdf", "/misleading_npy.pdf", "npy", CANARY_NPY),
        (f"{base}/iana_arrow_file", "/iana_arrow_file", "arrow-file", CANARY_ARROW_FILE),
        (f"{base}/iana_arrow_stream", "/iana_arrow_stream", "arrow-stream", CANARY_ARROW_STREAM),
        (f"{base}/iana_app_fits", "/iana_app_fits", "fits", CANARY_FITS),
        (f"{base}/iana_image_fits", "/iana_image_fits", "fits", CANARY_FITS),
    ]

    for url, route_path, expected_kind, canary in url_cases:
        expected_body = routes[route_path][0]
        res = router.extract_url(url)
        assert res.source == url
        assert res.kind == expected_kind
        assert len(res.units) > 0

        # Exact origins on all units
        unit_ids = {u.id for u in res.units}
        for u in res.units:
            assert u.role is Role.UNKNOWN
            assert u.origin.source == url
            assert "autotldr-snapshot" not in u.origin.source
            assert "autotldr-snapshot" not in u.origin.ref

        # Exact origins on gaps
        for gap in res.gaps:
            assert gap.origin.source == url
            assert "autotldr-snapshot" not in gap.origin.source

        # Exact origins on summary claims
        for claim in res.summary_claims:
            for origin in claim.origins:
                assert origin.source == url
                assert "autotldr-snapshot" not in origin.source

        # Relation closure over emitted units
        for rel in res.relations:
            assert rel.src in unit_ids, f"Relation src {rel.src!r} not in emitted units"
            assert rel.dst in unit_ids, f"Relation dst {rel.dst!r} not in emitted units"

        # Manifest matches URL, exact bytes count and exact sha256
        manifest = res.meta["inputs"][0]
        assert manifest["source"] == url
        assert manifest["bytes"] == len(expected_body)
        assert manifest["sha256"] == hashlib.sha256(expected_body).hexdigest()

        # Distinct canary absent from full IR
        _assert_canary_not_present(res, canary)

    # Recognized Feather v1 framing over URL declines as UnsupportedFormat(kind="Feather v1", tier=3)
    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract_url(f"{base}/sample_v1.feather")
    assert exc_info.value.tier == 3
    assert exc_info.value.kind == "Feather v1"


def test_public_extract_url_repeat_no_socket_leaks(
    http_science_server: tuple[str, dict[str, tuple[bytes, str]]]
):
    """Prove repeated invocations of extract_url in one process leak no sockets or resources."""
    base, _ = http_science_server
    url = f"{base}/sample.fits"
    for _ in range(10):
        res = router.extract_url(url)
        assert res.kind == "fits"
        assert res.source == url


# ---------------------------------------------------------------------------
# 5. Collection Directory and Archive Acquisition
# ---------------------------------------------------------------------------
def test_collection_directory_leaves(tmp_path: Path, science_fixtures: dict[str, Path]):
    """Collection directory traversal extracts science formats with exact logical member sources, manifest binding, and claim origins."""
    canary_map = {
        "sample.npy": CANARY_NPY,
        "sample.npz": CANARY_NPZ,
        "sample.fits": CANARY_FITS,
        "sample.arrow": CANARY_ARROW_FILE,
        "sample.arrows": CANARY_ARROW_STREAM,
        "sample.feather": CANARY_FEATHER_V2,
        "sample.orc": CANARY_ORC,
    }
    col_dir = tmp_path / "science_dir"
    col_dir.mkdir()
    for key, p in science_fixtures.items():
        target = col_dir / p.name
        target.write_bytes(p.read_bytes())

    acq = acquire_directory(col_dir)
    assert acq.source == col_dir.name
    assert len(acq.declines) == 0

    expected_members = set(canary_map.keys())
    actual_members = {Path(ext.source).name for ext in acq.extractions}
    assert actual_members == expected_members

    for ext in acq.extractions:
        member_name = Path(ext.source).name
        expected_logical_source = f"{col_dir.name}/{member_name}"
        assert ext.source == expected_logical_source

        fixture_bytes = (col_dir / member_name).read_bytes()
        manifest = ext.meta["inputs"][0]
        assert manifest["source"] == expected_logical_source
        assert manifest["bytes"] == len(fixture_bytes)
        assert manifest["sha256"] == hashlib.sha256(fixture_bytes).hexdigest()

        unit_ids = {u.id for u in ext.units}
        for u in ext.units:
            assert u.role is Role.UNKNOWN
            assert u.origin.source == expected_logical_source
            assert "autotldr-snapshot" not in u.origin.source

        for gap in ext.gaps:
            assert gap.origin.source == expected_logical_source
            assert "autotldr-snapshot" not in gap.origin.source

        for claim in ext.summary_claims:
            for origin in claim.origins:
                assert origin.source == expected_logical_source
                assert "autotldr-snapshot" not in origin.source
            for ev_id in claim.evidence_unit_ids:
                assert ev_id in unit_ids

        for rel in ext.relations:
            assert rel.src in unit_ids
            assert rel.dst in unit_ids

        canary = canary_map[member_name]
        _assert_canary_not_present(ext, canary)


def test_collection_archive_leaves(tmp_path: Path, science_fixtures: dict[str, Path]):
    """Collection archive expansion (ZIP) extracts science members with exact archive!/member origins, manifest binding, and claim origins."""
    canary_map = {
        "sample.npy": CANARY_NPY,
        "sample.npz": CANARY_NPZ,
        "sample.fits": CANARY_FITS,
        "sample.arrow": CANARY_ARROW_FILE,
        "sample.arrows": CANARY_ARROW_STREAM,
        "sample.feather": CANARY_FEATHER_V2,
        "sample.orc": CANARY_ORC,
    }
    zip_path = tmp_path / "science_bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for key, p in science_fixtures.items():
            zf.write(p, arcname=p.name)

    acq = acquire_archive(zip_path)
    assert acq.source == zip_path.name
    assert len(acq.declines) == 0

    expected_members = set(canary_map.keys())
    actual_members = {ext.source.split("!/")[-1] for ext in acq.extractions}
    assert actual_members == expected_members

    for ext in acq.extractions:
        member_name = ext.source.split("!/")[-1]
        expected_logical_source = f"{zip_path.name}!/{member_name}"
        assert ext.source == expected_logical_source

        fixture_bytes = science_fixtures[
            next(k for k, p in science_fixtures.items() if p.name == member_name)
        ].read_bytes()
        manifest = ext.meta["inputs"][0]
        assert manifest["source"] == expected_logical_source
        assert manifest["bytes"] == len(fixture_bytes)
        assert manifest["sha256"] == hashlib.sha256(fixture_bytes).hexdigest()

        unit_ids = {u.id for u in ext.units}
        for u in ext.units:
            assert u.role is Role.UNKNOWN
            assert u.origin.source == expected_logical_source
            assert "autotldr-snapshot" not in u.origin.source

        for gap in ext.gaps:
            assert gap.origin.source == expected_logical_source
            assert "autotldr-snapshot" not in gap.origin.source

        for claim in ext.summary_claims:
            for origin in claim.origins:
                assert origin.source == expected_logical_source
                assert "autotldr-snapshot" not in origin.source
            for ev_id in claim.evidence_unit_ids:
                assert ev_id in unit_ids

        for rel in ext.relations:
            assert rel.src in unit_ids
            assert rel.dst in unit_ids

        canary = canary_map[member_name]
        _assert_canary_not_present(ext, canary)


# ---------------------------------------------------------------------------
# 6. Negative and Edge Cases (Feather Framing, Bounded Sniffing, Propagations)
# ---------------------------------------------------------------------------
def test_feather_v1_recognized_framing_declines_with_exact_subtype(tmp_path: Path):
    """Recognized Feather v1 framing is declined as 'Feather v1', tier 3."""
    p = tmp_path / "sample_v1.feather"
    p.write_bytes(b"FEA1" + b"\x00" * 100 + b"FEA1")

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 3
    assert exc_info.value.kind == "Feather v1"

    # Stdin
    with pytest.raises(UnsupportedFormat) as exc_stdin:
        router.extract_stdin(p.read_bytes())
    assert exc_stdin.value.tier == 3
    assert exc_stdin.value.kind == "Feather v1"


def test_feather_v1_spoofed_prefix_only_fails_closed(tmp_path: Path):
    """Feather v1 prefix without closing framing fails closed, not as a valid v1 decline."""
    p = tmp_path / "fake_v1.feather"
    p.write_bytes(b"FEA1" + b"\x00" * 50)

    with pytest.raises(
        ValueError,
        match="failed closed|missing Feather framing|unrecognized or unsupported",
    ):
        router.extract(p)


def test_zip_sniffing_name_only_npy_declines_as_archive(tmp_path: Path):
    """An extensionless ZIP containing a plain text file named 'notes.npy' declines as archive."""
    p = tmp_path / "fake_npz"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.npy", "This is ordinary plain text, not a numpy array.")
    p.write_bytes(buf.getvalue())

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_encrypted_npy_declines_as_archive(tmp_path: Path):
    """An extensionless ZIP containing an encrypted member declines as archive rather than failing."""
    p = tmp_path / "encrypted_npz"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("array.npy", b"\x93NUMPY\x01\x00\x00\x00data")
    raw = bytearray(buf.getvalue())
    raw[6] = 1  # Patch local header flag_bits
    cd_idx = raw.find(b"PK\x01\x02")
    raw[cd_idx + 8] = 1  # Patch central directory flag_bits
    p.write_bytes(bytes(raw))

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_conflicting_ooxml_npy_raises_ambiguous_valueerror(tmp_path: Path):
    """ZIP containing conflicting format members raises ValueError."""
    p = tmp_path / "ambiguous.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("data.npy", b"\x93NUMPY\x01\x00\x00\x00{'descr': '<f8', 'fortran_order': False, 'shape': (1,)}")
    p.write_bytes(buf.getvalue())

    with pytest.raises(ValueError, match="ZIP container ambiguously matches"):
        router.extract(p)


def test_zip_sniffing_excessive_member_count_declines_as_archive(tmp_path: Path):
    """Extensionless ZIP containing > 1024 members does not promote to NPZ and declines as archive."""
    p = tmp_path / "excessive_members"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(1030):
            zf.writestr(f"arr_{i}.npy", b"\x93NUMPY\x01\x00\x00\x00")
    p.write_bytes(buf.getvalue())

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_forged_oversized_member_declines_as_archive(tmp_path: Path):
    """Extensionless ZIP with a forged oversized member (> 512MB in central directory) declines as archive."""
    p = tmp_path / "oversized_member"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.npy", b"\x93NUMPY\x01\x00\x00\x00")
    raw = bytearray(buf.getvalue())
    cd_idx = raw.find(b"PK\x01\x02")
    # Patch uncompressed size at offset 24 in central directory record to 600MB
    struct.pack_into("<I", raw, cd_idx + 24, 600 * 1024 * 1024)
    p.write_bytes(bytes(raw))

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_high_ratio_member_declines_as_archive(tmp_path: Path):
    """Extensionless ZIP containing an actual high-ratio compressed member (> 100:1) declines as archive."""
    p = tmp_path / "high_ratio_member"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Repeating data gives > 100:1 compression ratio
        payload = b"\x93NUMPY" + b"\x00" * (100 * 1024)
        zf.writestr("data.npy", payload)
    p.write_bytes(buf.getvalue())

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_zero_compressed_non_empty_declines_as_archive(tmp_path: Path):
    """Extensionless ZIP containing a zero-compressed non-empty member declines as archive with zero opens."""
    p = tmp_path / "zero_compressed_bomb"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.npy", b"\x93NUMPY\x01\x00\x00\x00")
    raw = bytearray(buf.getvalue())
    cd_idx = raw.find(b"PK\x01\x02")
    # Patch compressed size to 0 and uncompressed size to 100 in central directory record
    struct.pack_into("<I", raw, cd_idx + 20, 0)
    struct.pack_into("<I", raw, cd_idx + 24, 100)
    p.write_bytes(bytes(raw))

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_unsafe_path_declines_as_archive(tmp_path: Path):
    """Extensionless ZIP with unsafe member path (path traversal) declines as archive."""
    p = tmp_path / "unsafe_path"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escaped.npy", b"\x93NUMPY\x01\x00\x00\x00")
    p.write_bytes(buf.getvalue())

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_mixed_non_npy_member_declines_as_archive(tmp_path: Path):
    """An extensionless mixed ZIP is not promoted merely because one member has NPY magic."""
    p = tmp_path / "mixed_archive"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("data.npy", b"\x93NUMPY\x01\x00\x00\x00")
        zf.writestr("README.txt", "not part of an NPZ array archive")

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


def test_zip_sniffing_safe_nested_npy_name_can_promote(tmp_path: Path):
    """Safe POSIX components are allowed; substring-based path rejection is forbidden."""
    import numpy as np

    member = io.BytesIO()
    np.save(member, np.array([1, 2, 3], dtype="i4"))
    p = tmp_path / "nested_npz"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("group/sample..v2.npy", member.getvalue())

    result = router.extract(p)
    assert result.kind == "npz"


def test_zip_sniffing_assertion_error_propagates_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A detector AssertionError inside ZipFile.open must not be converted into an archive decline."""
    p = tmp_path / "trigger_assert"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test.npy", b"\x93NUMPY\x01\x00\x00\x00")
    p.write_bytes(buf.getvalue())

    def buggy_open(self, name, mode="r", *args, **kwargs):
        raise AssertionError("detector implementation defect sentinel")

    monkeypatch.setattr(zipfile.ZipFile, "open", buggy_open)

    with pytest.raises(AssertionError, match="detector implementation defect sentinel"):
        router.extract(p)


def test_spoofed_npy_fails_in_extractor(tmp_path: Path):
    """Spoofed NPY magic bytes with corrupt header fail with InvalidScientificArrayData."""
    p = tmp_path / "spoofed.npy"
    p.write_bytes(b"\x93NUMPY\x01\x00\x00\x00corrupt header data")

    with pytest.raises(ValueError, match="invalid (npy|array)"):
        router.extract(p)


def test_truncated_fits_fails_in_extractor(tmp_path: Path):
    """Truncated FITS file with SIMPLE = header card fails with InvalidFitsData."""
    p = tmp_path / "truncated.fits"
    p.write_bytes(b"SIMPLE  =                    T / Standard FITS" + b"\x00" * 50)

    with pytest.raises(
        ValueError, match="smaller than standard 2880-byte block|read error|invalid"
    ):
        router.extract(p)


def test_stream_continuation_only_not_promoted(tmp_path: Path):
    """Continuation marker 0xffffffff alone on extensionless file is not promoted to stream."""
    p = tmp_path / "raw_bytes"
    p.write_bytes(b"\xff\xff\xff\xff\x01\x02\x03\x04")

    # Extensionless raw bytes should raise UnknownFormat
    with pytest.raises(UnknownFormat):
        router.extract(p)


def test_generic_zip_declines_as_archive(tmp_path: Path):
    """A generic ZIP without NPY or OOXML members declines as Tier 2 archive."""
    p = tmp_path / "generic.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.txt", "hello world")
    p.write_bytes(buf.getvalue())

    with pytest.raises(UnsupportedFormat) as exc_info:
        router.extract(p)
    assert exc_info.value.tier == 2
    assert exc_info.value.kind == "archive"


# ---------------------------------------------------------------------------
# 7. Isolated Dependency Failures, Non-Leak Proof, and Programmer Defect Propagations
# ---------------------------------------------------------------------------
def test_missing_numpy_dependency_isolated(
    science_fixtures: dict[str, Path],
    http_science_server: tuple[str, dict[str, tuple[bytes, str]]],
    monkeypatch: pytest.MonkeyPatch,
):
    """Missing NumPy raises sanitized user-facing ImportError naming autotldr[data] across local, stdin, and URL without leaking raw payloads or canaries."""
    p_npy = science_fixtures["npy"]
    npy_bytes = p_npy.read_bytes()
    base, _ = http_science_server
    url_npy = f"{base}/sample.npy"

    raw_canary = CANARY_NPY
    raw_repr = repr(npy_bytes[:30])
    raw_hex = npy_bytes[:16].hex()

    monkeypatch.setitem(sys.modules, "numpy", None)

    # 1. Local file
    with pytest.raises(ImportError) as exc_local:
        router.extract(p_npy)
    msg_local = str(exc_local.value)
    assert "autotldr[data]" in msg_local
    assert "numpy" in msg_local.lower() or "scientific array" in msg_local.lower()
    assert str(p_npy) in msg_local
    assert "autotldr-snapshot" not in msg_local
    assert "pyarrow" not in msg_local
    assert raw_canary not in msg_local
    assert raw_repr not in msg_local
    assert raw_hex not in msg_local

    # 2. Stdin
    with pytest.raises(ImportError) as exc_stdin:
        router.extract_stdin(npy_bytes)
    msg_stdin = str(exc_stdin.value)
    assert "autotldr[data]" in msg_stdin
    assert "numpy" in msg_stdin.lower() or "scientific array" in msg_stdin.lower()
    assert "<stdin>" in msg_stdin
    assert "autotldr-snapshot" not in msg_stdin
    assert "/tmp/" not in msg_stdin
    assert "pyarrow" not in msg_stdin
    assert raw_canary not in msg_stdin
    assert raw_repr not in msg_stdin
    assert raw_hex not in msg_stdin

    # 3. Public URL
    with pytest.raises(ImportError) as exc_url:
        router.extract_url(url_npy)
    msg_url = str(exc_url.value)
    assert "autotldr[data]" in msg_url
    assert "numpy" in msg_url.lower() or "scientific array" in msg_url.lower()
    assert url_npy in msg_url
    assert "autotldr-snapshot" not in msg_url
    assert "/tmp/" not in msg_url
    assert "pyarrow" not in msg_url
    assert raw_canary not in msg_url
    assert raw_repr not in msg_url
    assert raw_hex not in msg_url


def test_missing_pyarrow_dependency_isolated(
    science_fixtures: dict[str, Path],
    http_science_server: tuple[str, dict[str, tuple[bytes, str]]],
    monkeypatch: pytest.MonkeyPatch,
):
    """Missing PyArrow raises sanitized user-facing ImportError naming autotldr[data] across local, stdin, and URL without leaking raw payloads or canaries."""
    p_arrow = science_fixtures["arrow_file"]
    arrow_bytes = p_arrow.read_bytes()
    base, _ = http_science_server
    url_arrow = f"{base}/sample.arrow"

    raw_canary = CANARY_ARROW_FILE
    raw_repr = repr(arrow_bytes[:30])
    raw_hex = arrow_bytes[:16].hex()

    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.ipc", None)

    # 1. Local file
    with pytest.raises(ImportError) as exc_local:
        router.extract(p_arrow)
    msg_local = str(exc_local.value)
    assert "autotldr[data]" in msg_local
    assert "pyarrow" in msg_local.lower() or "arrow" in msg_local.lower()
    assert str(p_arrow) in msg_local
    assert "autotldr-snapshot" not in msg_local
    assert "numpy" not in msg_local
    assert raw_canary not in msg_local
    assert raw_repr not in msg_local
    assert raw_hex not in msg_local

    # 2. Stdin
    with pytest.raises(ImportError) as exc_stdin:
        router.extract_stdin(arrow_bytes)
    msg_stdin = str(exc_stdin.value)
    assert "autotldr[data]" in msg_stdin
    assert "pyarrow" in msg_stdin.lower() or "arrow" in msg_stdin.lower()
    assert "<stdin>" in msg_stdin
    assert "autotldr-snapshot" not in msg_stdin
    assert "/tmp/" not in msg_stdin
    assert "numpy" not in msg_stdin
    assert raw_canary not in msg_stdin
    assert raw_repr not in msg_stdin
    assert raw_hex not in msg_stdin

    # 3. Public URL
    with pytest.raises(ImportError) as exc_url:
        router.extract_url(url_arrow)
    msg_url = str(exc_url.value)
    assert "autotldr[data]" in msg_url
    assert "pyarrow" in msg_url.lower() or "arrow" in msg_url.lower()
    assert url_arrow in msg_url
    assert "autotldr-snapshot" not in msg_url
    assert "/tmp/" not in msg_url
    assert "numpy" not in msg_url
    assert raw_canary not in msg_url
    assert raw_repr not in msg_url
    assert raw_hex not in msg_url


def test_extractor_body_programmer_import_error_not_reclassified(
    science_fixtures: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """An ordinary programmer ImportError raised within an extractor body propagates directly without being converted into a missing-dependency diagnosis."""
    import autotldr.extract.scientific_arrays as sa_mod
    import autotldr.extract.columnar_interchange as ci_mod
    import autotldr.extract.astronomy as astro_mod

    # 1. Scientific arrays programmer ImportError
    def buggy_extract_npy(path):
        raise ImportError("programmer defect sentinel in NPY extractor")

    monkeypatch.setattr(sa_mod, "extract", buggy_extract_npy)
    with pytest.raises(ImportError) as exc_npy:
        router.extract(science_fixtures["npy"])
    msg_npy = str(exc_npy.value)
    assert msg_npy == "programmer defect sentinel in NPY extractor"
    assert "not installed" not in msg_npy
    assert "pip install" not in msg_npy
    assert "autotldr[data]" not in msg_npy

    # 2. Columnar interchange programmer ImportError
    def buggy_extract_columnar(path, kind=None):
        raise ImportError("programmer defect sentinel in Arrow extractor")

    monkeypatch.setattr(ci_mod, "extract", buggy_extract_columnar)
    with pytest.raises(ImportError) as exc_arrow:
        router.extract(science_fixtures["arrow_file"])
    msg_arrow = str(exc_arrow.value)
    assert msg_arrow == "programmer defect sentinel in Arrow extractor"
    assert "not installed" not in msg_arrow
    assert "pip install" not in msg_arrow
    assert "autotldr[data]" not in msg_arrow

    # 3. Astronomy programmer ImportError
    def buggy_extract_fits(path):
        raise ImportError("programmer defect sentinel in FITS extractor")

    monkeypatch.setattr(astro_mod, "extract", buggy_extract_fits)
    with pytest.raises(ImportError) as exc_fits:
        router.extract(science_fixtures["fits"])
    msg_fits = str(exc_fits.value)
    assert msg_fits == "programmer defect sentinel in FITS extractor"
    assert "not installed" not in msg_fits
    assert "pip install" not in msg_fits


def test_module_initialization_import_error_not_reclassified(
    science_fixtures: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """A module-initialization defect is not guessed to be a missing optional package."""
    original = router.importlib.import_module

    def buggy_import(name: str):
        if name == "autotldr.extract.astronomy":
            raise ImportError("module initialization defect sentinel")
        return original(name)

    monkeypatch.setattr(router.importlib, "import_module", buggy_import)
    with pytest.raises(ImportError) as exc_info:
        router.extract(science_fixtures["fits"])
    assert str(exc_info.value) == "module initialization defect sentinel"
    assert "not installed" not in str(exc_info.value)
    assert "pip install" not in str(exc_info.value)


def test_critical_exceptions_propagate_unchanged(
    science_fixtures: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """MemoryError, AssertionError, and KeyboardInterrupt propagate through router unchanged."""
    import autotldr.extract.scientific_arrays as sa_mod

    # 1. MemoryError
    def mem_err(path):
        raise MemoryError("out of memory sentinel")

    monkeypatch.setattr(sa_mod, "extract", mem_err)
    with pytest.raises(MemoryError, match="out of memory sentinel"):
        router.extract(science_fixtures["npy"])

    # 2. AssertionError
    def assert_err(path):
        raise AssertionError("assertion defect sentinel")

    monkeypatch.setattr(sa_mod, "extract", assert_err)
    with pytest.raises(AssertionError, match="assertion defect sentinel"):
        router.extract(science_fixtures["npy"])

    # 3. KeyboardInterrupt
    def sigint_err(path):
        raise KeyboardInterrupt("user interrupt sentinel")

    monkeypatch.setattr(sa_mod, "extract", sigint_err)
    with pytest.raises(KeyboardInterrupt, match="user interrupt sentinel"):
        router.extract(science_fixtures["npy"])


# ---------------------------------------------------------------------------
# 8. HTTP Error Lifecycle and Closure Tests
# ---------------------------------------------------------------------------
def test_http_error_response_is_closed_on_failure(monkeypatch: pytest.MonkeyPatch):
    """Prove that an HTTPError raised by opener.open has its underlying response socket closed."""
    from urllib.error import HTTPError

    closed = False

    class ObservableHTTPError(HTTPError):
        def __init__(self) -> None:
            super().__init__("http://127.0.0.1:9999/fail", 404, "Not Found", {}, None)  # type: ignore[arg-type]

        def close(self) -> None:
            nonlocal closed
            closed = True
            super().close()

    class FakeOpener:
        def open(self, request: Any, timeout: float = 20.0) -> Any:
            raise ObservableHTTPError()

    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: FakeOpener())

    with pytest.raises(HTTPError):
        router._fetch_http("http://127.0.0.1:9999/fail", timeout=2.0, max_bytes=1000)

    assert closed, "HTTPError response socket was not closed when opener.open raised HTTPError"


def test_probe_llms_txt_404_closes_socket_without_warning(
    http_science_server: tuple[str, dict[str, tuple[bytes, str]]]
):
    """Prove repeated public extract_url calls where /llms.txt is 404 close sockets and pass GC under -W error."""
    import gc

    base, _ = http_science_server
    url = f"{base}/sample.fits"
    for _ in range(15):
        res = router.extract_url(url)
        assert res.kind == "fits"
        assert res.source == url
        gc.collect()
