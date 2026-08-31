#!/usr/bin/env python3
"""Rebuild the frozen Stage 2 corpus from exact production-extractor units.

The source binaries are intentionally not vendored.  ``sources.jsonl`` records
their immutable URLs, licenses, and SHA-256 digests; this builder consumes the
locally fetched copies under ``.agent/scratch/roles`` and refuses a hash or
extraction mismatch.  Selection drafts retain annotation rationales but the
generated model-visible file never contains a label.

The initial selectors, two independent reviewers, and the adjudicator all ran
before any prediction file was inspected.  Eight mixed or imbalanced units are
replaced by exact units from the same pinned sources.  The replacement map is
explicit below because the discarded unit id is part of the frozen sampling
design; it must never be inferred from a target label after scoring.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRATCH = ROOT / ".agent" / "scratch" / "roles"
RULE_COMMIT = "54b2158a37e7dd42392494fbadf031e11d952289"
FORMAT_ORDER = {name: offset for offset, name in enumerate(("md", "rst", "txt", "pdf", "xlsx"))}

REVIEW_DIR = SCRATCH / "reviews"
SELECTION_DIR = SCRATCH / "selections"

# ``drop_id`` is deliberately preregistered and independent of the selection
# draft's suggested drop.  Two first-round suggestions would have removed
# scarce definitions; these alternatives preserve source/format counts while
# removing overrepresented adjudicated roles.  No prediction was consulted.
REPLACEMENT_PLAN = (
    {
        "path": "replacements_md.json",
        "kind": "many_top_level",
        "index": 0,
        "drop_id": "role-0039",
        "review_id": "new-md-1",
    },
    {
        "path": "replacements_md.json",
        "kind": "many_top_level",
        "index": 1,
        "drop_id": "role-0034",
        "review_id": "new-md-2",
    },
    {
        "path": "replacements_rst_txt.json",
        "kind": "many_nested",
        "index": 0,
        "drop_id": "role-0117",
        "review_id": "new-txt-1",
    },
    {
        "path": "replacements_rst_txt.json",
        "kind": "many_nested",
        "index": 1,
        "drop_id": "role-0113",
        "review_id": "new-txt-2",
    },
    {
        "path": "replacements_rst_round2.json",
        "kind": "single_nested",
        "drop_id": "role-0071",
        "review_id": "role-0071",
    },
    {
        "path": "replacements_md_round2.json",
        "kind": "many_nested_md",
        "index": 0,
        "drop_id": "role-0017",
        "review_id": "role-0017",
    },
    {
        "path": "replacements_md_round2.json",
        "kind": "many_nested_md",
        "index": 1,
        "drop_id": "role-0018",
        "review_id": "role-0018",
    },
    {
        "path": "replacements_md_round2.json",
        "kind": "many_nested_md",
        "index": 2,
        "drop_id": "role-0022",
        "review_id": "role-0022",
    },
)

SOURCE_PATHS = {
    "md-kep-4355": SCRATCH / "repos/enhancements/keps/sig-api-machinery/4355-coordinated-leader-election/README.md",
    "md-kep-1205": SCRATCH / "repos/enhancements/keps/sig-auth/1205-bound-service-account-tokens/README.md",
    "md-kep-2400": SCRATCH / "repos/enhancements/keps/sig-node/2400-node-swap/README.md",
    "md-kep-0753": SCRATCH / "repos/enhancements/keps/sig-node/753-sidecar-containers/README.md",
    "rst-pep-0440": SCRATCH / "repos/peps/peps/pep-0440.rst",
    "rst-pep-0517": SCRATCH / "repos/peps/peps/pep-0517.rst",
    "rst-pep-0518": SCRATCH / "repos/peps/peps/pep-0518.rst",
    "rst-pep-0621": SCRATCH / "repos/peps/peps/pep-0621.rst",
    "txt-rfc-8259": SCRATCH / "sources/txt/rfc8259.txt",
    "txt-rfc-8446": SCRATCH / "sources/txt/rfc8446.txt",
    "txt-rfc-9000": SCRATCH / "sources/txt/rfc9000.txt",
    "txt-rfc-9110": SCRATCH / "sources/txt/rfc9110.txt",
    "pdf-plos-0314813": SCRATCH / "sources/pdf/pone0314813.pdf",
    "pdf-plos-0326373": SCRATCH / "sources/pdf/pone0326373.pdf",
    "pdf-plos-0341664": SCRATCH / "sources/pdf/pone0341664.pdf",
    "pdf-plos-0354688": SCRATCH / "sources/pdf/pone0354688.pdf",
    "xlsx-plos-0350642-s001": SCRATCH / "sources/xlsx/pone0350642-s001.xlsx",
    "xlsx-plos-0352019-s001": SCRATCH / "sources/xlsx/pone0352019-s001.xlsx",
    "xlsx-plos-0352164-s001": SCRATCH / "sources/xlsx/pone0352164-s001.xlsx",
    "xlsx-plos-0352207-s009": SCRATCH / "sources/xlsx/pone0352207-s009.xlsx",
}

SOURCE_ALIASES = {
    **{f"pep-{number}": f"rst-pep-{number}" for number in ("0440", "0517", "0518", "0621")},
    **{f"rfc{number}": f"txt-rfc-{number}" for number in ("8259", "8446", "9000", "9110")},
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def index_unique(rows: Iterable[dict[str, Any]], key: str, source: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{source}: missing non-empty {key}")
        if value in result:
            raise ValueError(f"{source}: duplicate {key} {value}")
        result[value] = row
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_source_id(source_id: str) -> str:
    return SOURCE_ALIASES.get(source_id, source_id)


def extract_units(source_id: str):
    path = SOURCE_PATHS[source_id]
    if source_id.startswith("pdf-"):
        from autotldr.extract.pdf import extract
    elif source_id.startswith("xlsx-"):
        from autotldr.extract.xlsx import extract
    else:
        from autotldr.extract.text import extract
    return extract(path).units


def unit_record(unit, source_id: str, source: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    char_span = list(unit.origin.char_span) if unit.origin.char_span is not None else None
    return {
        "source_id": source_id,
        "source_group": source["source_group"],
        "format": source["format"],
        "modality": unit.modality.value,
        "content": unit.content,
        "structure": list(unit.structure),
        "evidence": evidence,
        "origin": {"ref": unit.origin.ref, "char_span": char_span},
        "rule_role": unit.role.value,
        "rule_commit": RULE_COMMIT,
        "attribution": {
            "title": source["title"],
            "uri": source["uri"],
            "license": source["license"],
        },
    }


def require_unit(source_id: str, draft: dict[str, Any], units) -> Any:
    ref = draft["origin"]["ref"] if isinstance(draft["origin"], dict) else draft["origin"]
    content = draft.get("content")
    candidates = [unit for unit in units if unit.origin.ref == ref]
    if content is not None:
        candidates = [unit for unit in candidates if unit.content == content]
    if len(candidates) != 1:
        raise ValueError(f"{source_id} {ref}: expected one exact extracted unit, found {len(candidates)}")
    unit = candidates[0]
    expected_rule = draft.get("rule_role")
    if expected_rule is not None and unit.role.value != expected_rule:
        raise ValueError(f"{source_id} {ref}: frozen rule changed {expected_rule} -> {unit.role.value}")
    expected_span = draft.get("origin", {}).get("char_span") if isinstance(draft.get("origin"), dict) else None
    actual_span = list(unit.origin.char_span) if unit.origin.char_span is not None else None
    if expected_span is not None and actual_span != expected_span:
        raise ValueError(f"{source_id} {ref}: character span changed")
    return unit


def prose_drafts() -> list[dict[str, Any]]:
    md_pdf = load_json(SCRATCH / "selections/md_pdf.json")
    rows: list[dict[str, Any]] = []
    for source in md_pdf["sources"]:
        for selection in source["selections"]:
            rows.append(
                {
                    "source_id": source["source_id"],
                    "origin": selection["origin"],
                    "content": selection["content"],
                    "rule_role": selection["rule_role"],
                    "evidence": selection["evidence"],
                    "gold_role": selection["gold_role"],
                    "rationale": selection["rationale"],
                }
            )

    rst_txt = load_json(SCRATCH / "selections/rst_txt.json")
    for selection in rst_txt["selections"]:
        rows.append(
            {
                "source_id": canonical_source_id(selection["source_id"]),
                "origin": selection["origin"],
                "content": selection["content"],
                "rule_role": selection["rule_role"],
                "evidence": selection["evidence"],
                "gold_role": selection["gold_role"],
                "rationale": selection["annotation_rationale"],
            }
        )
    return rows


def xlsx_drafts() -> list[dict[str, Any]]:
    spec = load_json(SCRATCH / "selections/xlsx.json")
    rows: list[dict[str, Any]] = []
    for source in spec["sources"]:
        for selection in source["selections"]:
            rows.append(
                {
                    "source_id": source["source_id"],
                    "origin": {"ref": selection["origin"], "char_span": None},
                    "gold_role": selection["role"],
                    "rationale": selection["rationale"],
                }
            )
    return rows


def xlsx_evidence(unit) -> dict[str, Any]:
    if unit.modality.value == "schema":
        return {"sheet_summary": True, "formula": False, "input": False}
    return {
        "sheet_summary": False,
        "formula": bool(unit.meta.get("formula")),
        "input": bool(unit.meta.get("input")),
        "documented": bool(unit.meta.get("label")),
    }


def base_annotations(drafts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    reviewer_a = index_unique(load_jsonl(REVIEW_DIR / "reviewer_a.jsonl"), "id", "reviewer A")
    reviewer_b = index_unique(load_jsonl(REVIEW_DIR / "reviewer_b.jsonl"), "id", "reviewer B")
    adjudicated = index_unique(
        load_jsonl(REVIEW_DIR / "adjudicated.jsonl"), "id", "adjudication"
    )
    expected_ids = {f"role-{offset:04d}" for offset in range(1, len(drafts) + 1)}
    for name, rows in (
        ("reviewer A", reviewer_a),
        ("reviewer B", reviewer_b),
        ("adjudication", adjudicated),
    ):
        if set(rows) != expected_ids:
            raise ValueError(f"{name}: ids do not exactly cover the base corpus")

    annotations: list[dict[str, Any]] = []
    labels: list[dict[str, str]] = []
    for offset, draft in enumerate(drafts, start=1):
        item_id = f"role-{offset:04d}"
        row_a = reviewer_a[item_id]
        row_b = reviewer_b[item_id]
        final = adjudicated[item_id]
        if final.get("selection_role") != draft["gold_role"]:
            raise ValueError(f"{item_id}: adjudication selection label does not match draft")
        if final.get("reviewer_a") != row_a.get("role"):
            raise ValueError(f"{item_id}: reviewer A role does not match adjudication input")
        if final.get("reviewer_b") != row_b.get("role"):
            raise ValueError(f"{item_id}: reviewer B role does not match adjudication input")
        role = final.get("adjudicated_role")
        if not isinstance(role, str) or not role:
            raise ValueError(f"{item_id}: no adjudicated role")
        labels.append({"id": item_id, "role": role})
        annotations.append(
            {
                "id": item_id,
                "selection": {
                    "role": draft["gold_role"],
                    "rationale": draft["rationale"],
                },
                "reviewer_a": {
                    "role": row_a["role"],
                    "confidence": row_a["confidence"],
                    "note": row_a["note"],
                },
                "reviewer_b": {
                    "role": row_b["role"],
                    "confidence": row_b["confidence"],
                    "note": row_b["note"],
                },
                "adjudication": {
                    "role": role,
                    "disposition": final["disposition"],
                    "note": final["note"],
                },
                "annotation_provenance": "independent AI-agent reviews and AI-agent adjudication; no model-arm predictions consulted",
            }
        )
    return annotations, labels


def replacement_payload(config: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    raw = load_json(SELECTION_DIR / config["path"])
    kind = config["kind"]
    if kind == "many_top_level":
        replacement = raw["replacements"][config["index"]]
        item = replacement
        role = replacement["target_gold_role"]
        rationale = replacement["rationale"]
    elif kind == "many_nested":
        replacement = raw["replacements"][config["index"]]
        item = replacement["item"]
        role = replacement["target_role"]
        rationale = replacement["annotation"]["rationale"]
    elif kind == "single_nested":
        replacement = raw["replacement"]
        item = replacement["item"]
        role = replacement["target_role"]
        rationale = replacement["annotation"]["rationale"]
    elif kind == "many_nested_md":
        replacement = raw["replacements"][config["index"]]
        item = replacement["item"]
        role = replacement["target_gold_role"]
        rationale = replacement["annotation"]["rationale"]
    else:  # pragma: no cover - constant is reviewed with the corpus
        raise ValueError(f"unknown replacement kind {kind}")
    return item, role, rationale


def replacement_reviews() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    first_a = load_jsonl(REVIEW_DIR / "replacements_a.jsonl")
    first_b = load_jsonl(REVIEW_DIR / "replacements_b.jsonl")
    round2_a = load_jsonl(REVIEW_DIR / "round2_blind.jsonl")
    round2_b = load_jsonl(REVIEW_DIR / "round2_blind_b.jsonl")
    return (
        index_unique(first_a + round2_a, "id", "replacement reviewer A"),
        index_unique(first_b + round2_b, "id", "replacement reviewer B"),
    )


def review_note(row: dict[str, Any]) -> str:
    note = row.get("note", row.get("rationale"))
    if not isinstance(note, str) or not note:
        raise ValueError(f"review {row.get('id', '<unknown>')}: no note or rationale")
    return note


def apply_replacements(
    items: list[dict[str, Any]],
    labels: list[dict[str, str]],
    annotations: list[dict[str, Any]],
    source_by_id: dict[str, dict[str, Any]],
    cache: dict[str, Any],
) -> None:
    item_index = {item["id"]: offset for offset, item in enumerate(items)}
    review_a, review_b = replacement_reviews()
    used_origins = {(item["source_id"], item["origin"]["ref"]) for item in items}
    expected_review_ids = {config["review_id"] for config in REPLACEMENT_PLAN}
    if set(review_a) != expected_review_ids or set(review_b) != expected_review_ids:
        raise ValueError("replacement reviews do not exactly cover the replacement plan")

    for config in REPLACEMENT_PLAN:
        drop_id = config["drop_id"]
        offset = item_index[drop_id]
        old_item = items[offset]
        old_annotation = annotations[offset]
        old_label = labels[offset]["role"]
        draft, target_role, rationale = replacement_payload(config)
        source_id = canonical_source_id(draft["source_id"])
        if source_id != old_item["source_id"]:
            raise ValueError(f"{drop_id}: replacement must preserve source group")
        if source_id not in cache:
            cache[source_id] = extract_units(source_id)
        units = cache[source_id]
        unit = require_unit(source_id, draft, units)
        evidence = draft.get("evidence") or xlsx_evidence(unit)
        new_item = unit_record(unit, source_id, source_by_id[source_id], evidence)
        new_item["id"] = drop_id
        origin = (source_id, new_item["origin"]["ref"])
        old_origin = (old_item["source_id"], old_item["origin"]["ref"])
        used_origins.remove(old_origin)
        if origin in used_origins:
            raise ValueError(f"{drop_id}: replacement origin is already selected")
        used_origins.add(origin)

        blind_id = config["review_id"]
        row_a = review_a[blind_id]
        row_b = review_b[blind_id]
        if row_a.get("role") != target_role or row_b.get("role") != target_role:
            raise ValueError(
                f"{drop_id}: replacement reviewers do not both confirm {target_role}"
            )
        items[offset] = new_item
        labels[offset] = {"id": drop_id, "role": target_role}
        annotations[offset] = {
            "id": drop_id,
            "selection": {"role": target_role, "rationale": rationale},
            "reviewer_a": {
                "role": row_a["role"],
                "confidence": row_a["confidence"],
                "note": review_note(row_a),
            },
            "reviewer_b": {
                "role": row_b["role"],
                "confidence": row_b["confidence"],
                "note": review_note(row_b),
            },
            "adjudication": {
                "role": target_role,
                "disposition": "replacement confirmed by both blind reviewers",
                "note": "Exact production unit selected before model-arm scoring.",
            },
            "replaces": {
                "source_id": old_item["source_id"],
                "origin": old_item["origin"],
                "adjudicated_role": old_label,
                "adjudication": old_annotation["adjudication"],
            },
            "annotation_provenance": "independent AI-agent reviews; no model-arm predictions consulted",
        }


def main() -> None:
    sources = load_jsonl(HERE / "sources.jsonl")
    source_by_id = {row["source_id"]: row for row in sources}
    if set(source_by_id) != set(SOURCE_PATHS):
        raise ValueError("source manifest and local source map differ")
    for source_id, path in SOURCE_PATHS.items():
        actual = sha256(path)
        expected = source_by_id[source_id]["sha256"]
        if actual != expected:
            raise ValueError(f"{source_id}: SHA-256 mismatch {actual} != {expected}")

    drafts = prose_drafts() + xlsx_drafts()
    drafts.sort(
        key=lambda row: (
            FORMAT_ORDER[source_by_id[row["source_id"]]["format"]],
            row["source_id"],
            row["origin"]["ref"],
        )
    )
    cache: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    annotations, labels = base_annotations(drafts)
    for offset, draft in enumerate(drafts, start=1):
        source_id = draft["source_id"]
        if source_id not in cache:
            cache[source_id] = extract_units(source_id)
        units = cache[source_id]
        unit = require_unit(source_id, draft, units)
        evidence = draft.get("evidence") or xlsx_evidence(unit)
        item = unit_record(unit, source_id, source_by_id[source_id], evidence)
        item_id = f"role-{offset:04d}"
        item["id"] = item_id
        items.append(item)

    apply_replacements(items, labels, annotations, source_by_id, cache)

    if len(items) != 200:
        raise ValueError(f"expected 200 selections, found {len(items)}")
    duplicate_origins = len(items) - len({(item["source_id"], item["origin"]["ref"]) for item in items})
    if duplicate_origins:
        raise ValueError(f"selected origins are not unique ({duplicate_origins} duplicates)")
    print("formats", dict(sorted(Counter(item["format"] for item in items).items())))
    print("roles", dict(sorted(Counter(label["role"] for label in labels).items())))
    write_jsonl(HERE / "items.jsonl", items)
    write_jsonl(HERE / "labels.jsonl", labels)
    write_jsonl(HERE / "annotations.jsonl", annotations)


if __name__ == "__main__":
    main()
