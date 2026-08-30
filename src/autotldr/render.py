"""Renderers.

Every renderer reads the same Extraction and writes one shape. That is the
payoff of the N-to-1-to-M design: a new output format lands here and touches no
extractor.

v1 ships json and jsonl. The ansi, markdown, html, pdf, bundle, and mermaid
renderers land in later stages against this same interface.
"""

from __future__ import annotations

import json
from typing import Any

from .unit import Extraction, Unit

SCHEMA_VERSION = 1


def unit_to_dict(unit: Unit) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": unit.id,
        "source": unit.source,
        "modality": str(unit.modality),
        "role": str(unit.role),
        "content": unit.content,
        "origin": {"source": unit.origin.source, "ref": unit.origin.ref},
        "tokens": unit.tokens,
        "salience": round(unit.salience, 3),
        "confidence": round(unit.confidence, 3),
    }
    if unit.origin.char_span is not None:
        out["origin"]["char_span"] = list(unit.origin.char_span)
    if unit.structure:
        out["structure"] = list(unit.structure)
    if unit.meta:
        out["meta"] = _clean(unit.meta)
    return out


def to_json(result: Extraction, *, indent: int = 2) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "subject": result.source,
        "kind": result.kind,
        "tokens": result.tokens,
        "units": [unit_to_dict(u) for u in result.units],
        "relations": [
            {
                "src": r.src,
                "dst": r.dst,
                "kind": str(r.kind),
                "evidence": r.evidence,
                "confidence": round(r.confidence, 3),
            }
            for r in result.relations
        ],
        "gaps": result.gaps,
        "manifest": _clean(result.meta),
    }
    return json.dumps(payload, indent=indent, ensure_ascii=False, default=str) + "\n"


def to_jsonl(result: Extraction) -> str:
    """One unit per line, for piping into jq.

    A leading header line carries the subject and gaps so a consumer reading the
    stream never has to go back to the file to learn what it was looking at.
    """
    lines = [
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "type": "header",
                "subject": result.source,
                "kind": result.kind,
                "units": len(result.units),
                "gaps": result.gaps,
            },
            ensure_ascii=False,
            default=str,
        )
    ]
    lines.extend(
        json.dumps({"type": "unit", **unit_to_dict(u)}, ensure_ascii=False, default=str)
        for u in result.units
    )
    return "\n".join(lines) + "\n"


def _clean(meta: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so output stays readable at a glance."""
    return {k: v for k, v in meta.items() if v is not None and v != [] and v != {}}
