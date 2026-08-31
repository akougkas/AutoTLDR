#!/usr/bin/env python3
"""Generate the complete AutoTLDR MVP demonstration artifact set.

This script deliberately owns no model lifecycle. Run it only after the chosen
model instance has been verified as ZBook-local and resident through the
guarded lifecycle tooling. It performs one bounded evidence call against the
fixed localhost endpoint, then renders that single accepted grounded extraction
into all six MVP shapes without calling the model again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ARTIFACT_STEM = "autotldr-mvp"
MANIFEST_SCHEMA = "autotldr-mvp-demo-v2"

# Every shape the product supports. All six are rendered from one accepted
# Extraction; the model is never consulted a second time.
TEXT_SHAPES = ("ansi", "md", "html", "json", "jsonl")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire a mixed local collection, run one grounded local synthesis, "
            "and write ANSI, Markdown, HTML, PDF, JSON, and JSONL artifacts."
        )
    )
    parser.add_argument("source", type=Path, help="local mixed collection directory")
    parser.add_argument(
        "--model",
        required=True,
        help="exact already-loaded ZBook-local LM Studio model instance ID",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="directory that will receive the six demo artifacts and manifest",
    )
    parser.add_argument(
        "--evidence-budget",
        type=int,
        default=12_000,
        help="canonical model evidence ceiling in UTF-8 bytes (default: 12000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="localhost model request timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=4096,
        help="model completion ceiling, including hidden reasoning (default: 4096)",
    )
    return parser


def _origin_record(origin: Any) -> dict[str, Any]:
    return {
        "source": origin.source,
        "ref": origin.ref,
        "char_span": list(origin.char_span) if origin.char_span is not None else None,
    }


def generate(
    source: Path,
    *,
    model: str,
    output_dir: Path,
    evidence_budget: int = 12_000,
    timeout: float = 120.0,
    max_output_tokens: int = 4096,
    client: Any | None = None,
) -> dict[str, Any]:
    """Acquire, synthesize exactly once, render every shape, and record it all.

    Returns the manifest. ``client`` exists so the single-call and one-extraction
    invariants can be tested without a resident model; production leaves it None
    and the strict loopback transport is constructed inside ``synthesize``.
    """

    from autotldr.api import acquire
    from autotldr.render import render
    from autotldr.share import render_pdf
    from autotldr.synthesis import SynthesisConfig, synthesize

    extraction = acquire([str(source)])
    if extraction.kind != "collection":
        raise ValueError("the MVP demo source must route as a collection")

    config = SynthesisConfig(
        model=model,
        evidence_budget_bytes=evidence_budget,
        timeout_seconds=timeout,
        max_output_tokens=max_output_tokens,
        fallback_on_failure=False,
    )
    started = time.monotonic_ns()
    synthesized = synthesize(
        extraction, config, **({"client": client} if client is not None else {})
    )
    wall_ns = time.monotonic_ns() - started

    # One accepted Extraction feeds every renderer below. Nothing here may
    # re-enter the model seam.
    result = synthesized.extraction
    rendered: dict[str, str | bytes] = {
        shape: render(result, output=shape, cite=True) for shape in TEXT_SHAPES
    }
    rendered["pdf"] = render_pdf(result, cite=True)

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, object]] = []
    for suffix, value in rendered.items():
        payload = value if isinstance(value, bytes) else value.encode("utf-8")
        path = output_dir / f"{ARTIFACT_STEM}.{suffix}"
        path.write_bytes(payload)
        artifacts.append(
            {
                "format": suffix,
                "path": str(path),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    acquisitions = result.meta.get("collection_acquisitions") or [{}]
    model_run = synthesized.model_run
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "source": result.source,
        "kind": result.kind,
        "collection": {
            "path": str(Path(source).resolve()),
            "manifest_sha256": acquisitions[0].get("sha256"),
            "counts": acquisitions[0].get("counts"),
            "admitted_bytes": acquisitions[0].get("admitted_bytes"),
        },
        "inputs": list(result.meta.get("inputs", [])),
        "input_count": len(result.meta.get("inputs", [])),
        "units": len(result.units),
        "relations": len(result.relations),
        "gaps": len(result.gaps),
        "summary_claims": [
            {
                "id": statement.id,
                "content": statement.content,
                "evidence_unit_ids": list(statement.evidence_unit_ids),
                "origins": [_origin_record(origin) for origin in statement.origins],
            }
            for statement in result.summary_claims
        ],
        "synthesis": {
            "model": model_run.get("model"),
            "endpoint": model_run.get("endpoint"),
            "endpoint_class": model_run.get("endpoint_class"),
            "endpoint_policy": model_run.get("endpoint_policy"),
            "transport": model_run.get("transport"),
            "settings": model_run.get("settings"),
            "outcome": model_run.get("outcome"),
            "validation": model_run.get("validation"),
            "fallback": model_run.get("fallback"),
            "used_fallback": synthesized.used_fallback,
            "input": model_run.get("input"),
            "output": model_run.get("output"),
            "response_facts": model_run.get("response_facts"),
            "timing": model_run.get("timing"),
            "claim_count": model_run.get("claim_count"),
            "claim_ids": model_run.get("claim_ids"),
            "record_sha256": model_run.get("record_sha256"),
            "evidence_bytes": synthesized.evidence_pack.used_bytes,
            "evidence_sha256": synthesized.evidence_pack.sha256,
            "evidence_unit_ids": list(synthesized.evidence_pack.unit_ids),
            "model_calls": 1,
            "wall_ns": wall_ns,
        },
        "artifacts": artifacts,
    }
    manifest_payload = (
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, indent=2, default=str
        ).encode("utf-8")
        + b"\n"
    )
    (output_dir / "manifest.json").write_bytes(manifest_payload)
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.evidence_budget <= 0:
        raise SystemExit("--evidence-budget must be positive")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if not 1 <= args.max_output_tokens <= 4096:
        raise SystemExit("--max-output-tokens must be in 1..4096")

    from autotldr.synthesis import SynthesisRunError

    try:
        manifest = generate(
            args.source,
            model=args.model,
            output_dir=args.output_dir,
            evidence_budget=args.evidence_budget,
            timeout=args.timeout,
            max_output_tokens=args.max_output_tokens,
        )
    except SynthesisRunError as exc:
        outcome = exc.model_run.get("outcome", "unknown")
        validation = exc.model_run.get("validation")
        code = validation.get("error_code") if isinstance(validation, dict) else None
        detail = f" ({code})" if isinstance(code, str) and code else ""
        print(
            f"autotldr demo: grounded synthesis failed: {outcome}{detail}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"autotldr demo: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, indent=2, default=str
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
