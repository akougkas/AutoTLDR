#!/usr/bin/env python3
"""Build and validate the frozen Stage 4 fusion corpus.

The fixtures are real files and every extraction passes through the production
router.  Frozen extraction records deliberately replace checkout-specific
absolute sources and v2 ids with stable logical sources and unit indexes.  Raw
bytes, native origins, content, metadata, and relation topology remain exact.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping


HERE = Path(__file__).resolve().parent
POLICY = HERE / "policy.json"
FREEZE = HERE / "freeze.json"
SPLITS = ("dev", "scored")
FIXED_ZIP_TIME = (2026, 8, 30, 0, 0, 0)
# openpyxl overwrites ``properties.modified`` at save time.  Preserve the
# exact value in the first accepted corrected-v2 fixture so rebuilding the
# binary cannot silently move the frozen held-out source-tree hash.
FROZEN_CORE_MODIFIED = b"2026-08-30T21:16:58Z"
CORE_MODIFIED_PATTERN = re.compile(
    rb"(<dcterms:modified\b[^>]*>)[^<]*(</dcterms:modified>)"
)


class CorpusError(ValueError):
    """A fixture, manifest, extraction, or frozen hash is invalid."""


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        text = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return (text + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_json_bytes(dict(row)) for row in rows)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(payload)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CorpusError(f"{path} must contain one JSON object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CorpusError(f"cannot read {path}: {exc}") from exc
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorpusError(f"{path}:{line_no}: {exc}") from exc
        if not isinstance(value, dict):
            raise CorpusError(f"{path}:{line_no}: each row must be an object")
        rows.append(value)
    return rows


def _build_capacity_xlsx(path: Path) -> None:
    """Generate the sole binary fixture and canonicalize its ZIP envelope."""

    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CorpusError("openpyxl is required to build capacity.xlsx") from exc

    workbook = openpyxl.Workbook()
    workbook.properties.creator = "AutoTLDR Stage 4 benchmark"
    workbook.properties.created = dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
    workbook.properties.modified = dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
    sheet = workbook.active
    sheet.title = "Capacity"
    sheet["A1"] = "Input"
    sheet["B1"] = "Value"
    sheet["A2"] = "node_count"
    sheet["B2"] = 8
    sheet["A3"] = "per_node_mbps"
    sheet["B3"] = 450
    sheet["A4"] = "overhead_factor"
    sheet["B4"] = 0.92
    sheet["A5"] = "safety_margin_pct"
    sheet["B5"] = 10
    sheet["A7"] = "raw_capacity_mbps"
    sheet["B7"] = "=B2*B3"
    sheet["A8"] = "effective_capacity_mbps"
    sheet["B8"] = "=B7*B4*(1-B5/100)"

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="autotldr-fusion-xlsx-") as raw_dir:
        raw = Path(raw_dir) / "capacity.raw.xlsx"
        workbook.save(raw)
        workbook.close()
        with zipfile.ZipFile(raw, "r") as source:
            members = []
            for name in sorted(source.namelist()):
                payload = source.read(name)
                if name == "docProps/core.xml":
                    payload, replacements = CORE_MODIFIED_PATTERN.subn(
                        rb"\g<1>" + FROZEN_CORE_MODIFIED + rb"\g<2>", payload
                    )
                    if replacements != 1:
                        raise CorpusError(
                            "capacity.xlsx core properties have no unique modified timestamp"
                        )
                members.append((name, payload))

    temporary = path.with_suffix(".xlsx.tmp")
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as target:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            target.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.replace(temporary, path)


def build_fixtures() -> None:
    _build_capacity_xlsx(HERE / "scored" / "fixtures" / "capacity" / "capacity.xlsx")


def _collections(split: str) -> list[dict[str, Any]]:
    path = HERE / split / "collections.jsonl"
    rows = _load_jsonl(path)
    ids: set[str] = set()
    for offset, row in enumerate(rows, start=1):
        expected = {"id", "sources", "split"}
        if set(row) != expected:
            raise CorpusError(
                f"{path}:{offset}: fields must be exactly {sorted(expected)}"
            )
        collection_id = row["id"]
        sources = row["sources"]
        if not isinstance(collection_id, str) or not collection_id:
            raise CorpusError(f"{path}:{offset}: invalid collection id")
        if collection_id in ids:
            raise CorpusError(f"{path}: duplicate collection id {collection_id!r}")
        ids.add(collection_id)
        if row["split"] != split:
            raise CorpusError(f"{path}:{offset}: split must be {split!r}")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(item, str) and item for item in sources
        ):
            raise CorpusError(f"{path}:{offset}: sources must be non-empty strings")
        if sources != sorted(sources) or len(sources) != len(set(sources)):
            raise CorpusError(
                f"{path}:{offset}: sources must be unique and sorted"
            )
        root = HERE / split / "fixtures" / collection_id
        declared = {Path(item).as_posix() for item in sources}
        actual = {
            item.relative_to(root).as_posix()
            for item in root.rglob("*")
            if item.is_file()
        }
        if declared != actual:
            raise CorpusError(
                f"{collection_id}: declared/actual source mismatch; "
                f"missing={sorted(declared - actual)}, extra={sorted(actual - declared)}"
            )
    return rows


def _tree_digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for logical, payload in sorted(files):
        name = logical.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _stable_value(value: Any, id_map: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return id_map.get(value, value)
    if isinstance(value, dict):
        return {
            str(_stable_value(key, id_map)): _stable_value(item, id_map)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item, id_map) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_stable_value(item, id_map) for item in value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _snapshot_extraction(result: Any, logical_source: str) -> dict[str, Any]:
    unit_index = {unit.id: index for index, unit in enumerate(result.units)}
    if len(unit_index) != len(result.units):
        raise CorpusError(f"{logical_source}: duplicate production unit ids")
    id_map = {unit_id: f"unit:{index}" for unit_id, index in unit_index.items()}
    units = []
    for index, unit in enumerate(result.units):
        units.append(
            {
                "index": index,
                "modality": str(unit.modality),
                "role": str(unit.role),
                "content": unit.content,
                "content_sha256": _sha256(unit.content.encode("utf-8")),
                "origin": {
                    "source": logical_source,
                    "ref": unit.origin.ref,
                    "char_span": list(unit.origin.char_span)
                    if unit.origin.char_span is not None
                    else None,
                },
                "structure": list(unit.structure),
                "salience": unit.salience,
                "confidence": unit.confidence,
                "tokens": unit.tokens,
                "meta": _stable_value(unit.meta, id_map),
            }
        )
    relations = []
    for relation in result.relations:
        if relation.src not in unit_index or relation.dst not in unit_index:
            raise CorpusError(f"{logical_source}: production relation is dangling")
        relations.append(
            {
                "src_index": unit_index[relation.src],
                "dst_index": unit_index[relation.dst],
                "kind": str(relation.kind),
                "evidence": relation.evidence,
                "confidence": relation.confidence,
            }
        )
    gaps = [
        {
            "content": str(gap),
            "origin": {
                "source": logical_source,
                "ref": gap.origin.ref,
                "char_span": list(gap.origin.char_span)
                if gap.origin.char_span is not None
                else None,
            },
        }
        for gap in result.gaps
    ]
    meta = {
        key: item
        for key, item in result.meta.items()
        if key not in {"inputs", "timings"}
    }
    return {
        "logical_source": logical_source,
        "kind": result.kind,
        "units": units,
        "relations": relations,
        "gaps": gaps,
        "meta": _stable_value(meta, id_map),
    }


def build_split(split: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    from autotldr.router import extract

    collections = _collections(split)
    source_rows: list[dict[str, Any]] = []
    extraction_rows: list[dict[str, Any]] = []
    tree_files: list[tuple[str, bytes]] = []
    for collection in collections:
        collection_id = collection["id"]
        root = HERE / split / "fixtures" / collection_id
        for relative in collection["sources"]:
            path = root / relative
            payload = path.read_bytes()
            logical = f"fusion://{split}/{collection_id}/{relative}"
            tree_files.append((f"{collection_id}/{relative}", payload))
            result = extract(path)
            manifest_inputs = result.meta.get("inputs")
            if not isinstance(manifest_inputs, list) or len(manifest_inputs) != 1:
                raise CorpusError(f"{path}: production router did not attach one input")
            acquired = manifest_inputs[0]
            expected_hash = _sha256(payload)
            if acquired.get("sha256") != expected_hash or acquired.get("bytes") != len(payload):
                raise CorpusError(f"{path}: production input manifest does not match bytes")
            source_rows.append(
                {
                    "collection": collection_id,
                    "source": relative,
                    "logical_source": logical,
                    "kind": acquired.get("kind", result.kind),
                    "tier": acquired.get("tier"),
                    "bytes": len(payload),
                    "sha256": expected_hash,
                    "license": "Synthetic benchmark fixture; CC0-1.0",
                    "attribution": "Authored for the AutoTLDR Stage 4 diagnostic corpus",
                }
            )
            snapshot = _snapshot_extraction(result, logical)
            snapshot["collection"] = collection_id
            snapshot["source"] = relative
            extraction_rows.append(snapshot)
    return source_rows, extraction_rows, _tree_digest(tree_files)


def freeze() -> dict[str, Any]:
    prediction_artifacts = sorted(
        str(path.relative_to(HERE))
        for path in (HERE / "predictions").glob("*")
        if path.is_file()
    )
    report_paths = [HERE / "report.json", HERE / "report.md"]
    for split in SPLITS:
        report_paths.extend((HERE / split / "report.json", HERE / split / "report.md"))
    report_artifacts = [
        str(path.relative_to(HERE)) for path in report_paths if path.exists()
    ]
    if prediction_artifacts or report_artifacts:
        raise CorpusError(
            "refusing to freeze after active prediction/report artifacts exist: "
            + ", ".join((*prediction_artifacts, *report_artifacts))
        )
    build_fixtures()
    split_records: dict[str, Any] = {}
    for split in SPLITS:
        source_rows, extraction_rows, tree_hash = build_split(split)
        source_payload = _jsonl_bytes(source_rows)
        extraction_payload = _jsonl_bytes(extraction_rows)
        source_path = HERE / split / "sources.jsonl"
        extraction_path = HERE / split / "extractions.jsonl"
        _atomic_write(source_path, source_payload)
        _atomic_write(extraction_path, extraction_payload)
        split_records[split] = {
            "collections": len(_collections(split)),
            "sources": len(source_rows),
            "source_tree_sha256": tree_hash,
            "collections_sha256": _file_sha256(HERE / split / "collections.jsonl"),
            "sources_sha256": _sha256(source_payload),
            "extractions_sha256": _sha256(extraction_payload),
            "labels_sha256": _file_sha256(HERE / split / "labels.jsonl"),
        }
        annotations = HERE / split / "annotations.jsonl"
        if annotations.exists():
            split_records[split]["annotations_sha256"] = _file_sha256(annotations)
    policy = _load_json(POLICY)
    record = {
        "schema": 2,
        "version": policy.get("version"),
        "frozen_at": "2026-08-30",
        "frozen_before_predictions": True,
        "supersedes": "audit-history/rejected-freeze-v1/freeze.json",
        "delta_blind_review_required_before_scored_run": True,
        "policy_sha256": _file_sha256(POLICY),
        "splits": split_records,
    }
    _atomic_write(FREEZE, _json_bytes(record, pretty=True))
    return record


def _require_snapshot_binding(
    name: str,
    canonical_payload: bytes,
    checked_in_payload: bytes,
    frozen_hash: Any,
) -> str:
    """Require canonical, checked-in, and frozen snapshot hashes to agree."""

    canonical_hash = _sha256(canonical_payload)
    checked_in_hash = _sha256(checked_in_payload)
    if checked_in_hash != canonical_hash:
        raise CorpusError(
            f"{name} checked-in snapshot differs from the canonical production extraction: "
            f"checked-in {checked_in_hash}, canonical {canonical_hash}"
        )
    if frozen_hash != canonical_hash:
        raise CorpusError(
            f"{name} frozen hash differs: expected {frozen_hash!r}, "
            f"canonical/checked-in {canonical_hash!r}"
        )
    return canonical_hash


def _snapshot_guard_self_test() -> None:
    """Regression-check both stale-file and stale-freeze rejection paths."""

    canonical = b"canonical\n"
    try:
        _require_snapshot_binding(
            "self-test", canonical, b"stale\n", _sha256(canonical)
        )
    except CorpusError:
        pass
    else:
        raise CorpusError("snapshot guard self-test accepted stale checked-in bytes")
    try:
        _require_snapshot_binding(
            "self-test", canonical, canonical, _sha256(b"stale\n")
        )
    except CorpusError:
        pass
    else:
        raise CorpusError("snapshot guard self-test accepted a stale frozen hash")


def _xlsx_builder_self_test() -> None:
    """Require two builds and the checked-in binary fixture to be byte-exact."""

    with tempfile.TemporaryDirectory(prefix="autotldr-fusion-xlsx-self-test-") as raw:
        root = Path(raw)
        first = root / "first.xlsx"
        second = root / "second.xlsx"
        _build_capacity_xlsx(first)
        _build_capacity_xlsx(second)
        first_payload = first.read_bytes()
        if first_payload != second.read_bytes():
            raise CorpusError("capacity.xlsx builder is not byte-deterministic")
        checked_in = HERE / "scored" / "fixtures" / "capacity" / "capacity.xlsx"
        if first_payload != checked_in.read_bytes():
            raise CorpusError(
                "checked-in capacity.xlsx differs from its deterministic builder output"
            )


def validate() -> dict[str, Any]:
    _snapshot_guard_self_test()
    _xlsx_builder_self_test()
    frozen = _load_json(FREEZE)
    policy = _load_json(POLICY)
    if frozen.get("schema") != 2 or frozen.get("version") != policy.get("version"):
        raise CorpusError("freeze.json schema/version differs from policy.json")
    if frozen.get("frozen_before_predictions") is not True:
        raise CorpusError("freeze.json must attest frozen_before_predictions=true")
    if frozen.get("policy_sha256") != _file_sha256(POLICY):
        raise CorpusError("policy.json differs from the frozen hash")
    split_summaries: dict[str, Any] = {}
    for split in SPLITS:
        source_rows, extraction_rows, tree_hash = build_split(split)
        expected = frozen.get("splits", {}).get(split)
        if not isinstance(expected, dict):
            raise CorpusError(f"freeze.json has no {split} split")
        source_payload = _jsonl_bytes(source_rows)
        extraction_payload = _jsonl_bytes(extraction_rows)
        source_snapshot = HERE / split / "sources.jsonl"
        extraction_snapshot = HERE / split / "extractions.jsonl"
        sources_hash = _require_snapshot_binding(
            f"{split}/sources.jsonl",
            source_payload,
            source_snapshot.read_bytes(),
            expected.get("sources_sha256"),
        )
        extractions_hash = _require_snapshot_binding(
            f"{split}/extractions.jsonl",
            extraction_payload,
            extraction_snapshot.read_bytes(),
            expected.get("extractions_sha256"),
        )
        actual = {
            "collections": len(_collections(split)),
            "sources": len(source_rows),
            "source_tree_sha256": tree_hash,
            "collections_sha256": _file_sha256(HERE / split / "collections.jsonl"),
            "sources_sha256": sources_hash,
            "extractions_sha256": extractions_hash,
            "labels_sha256": _file_sha256(HERE / split / "labels.jsonl"),
        }
        annotations = HERE / split / "annotations.jsonl"
        if annotations.exists():
            actual["annotations_sha256"] = _file_sha256(annotations)
        for field, value in actual.items():
            if expected.get(field) != value:
                raise CorpusError(
                    f"{split} {field} differs: expected {expected.get(field)!r}, got {value!r}"
                )
        split_summaries[split] = {
            "collections": actual["collections"],
            "sources": actual["sources"],
            "units": sum(len(row["units"]) for row in extraction_rows),
            "relations": sum(len(row["relations"]) for row in extraction_rows),
            "gaps": sum(len(row["gaps"]) for row in extraction_rows),
            "source_tree_sha256": tree_hash,
        }
    return {"status": "valid", "splits": split_summaries}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build-fixtures", "freeze", "validate"))
    args = parser.parse_args(argv)
    try:
        if args.command == "build-fixtures":
            build_fixtures()
            result: dict[str, Any] = {"status": "built"}
        elif args.command == "freeze":
            result = freeze()
        else:
            result = validate()
    except CorpusError as exc:
        parser.exit(1, f"fusion corpus error: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
