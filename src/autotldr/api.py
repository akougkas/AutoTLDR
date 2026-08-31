"""Composable public pipeline for people embedding AutoTLDR.

The CLI, watch mode, and agent surfaces all reduce to the same three operations:
acquire/fuse, optionally synthesize, then render.  This module keeps that path
small and typed without pulling any parser or model runtime into package import.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class AutoTLDRResult:
    """One completed pipeline run and its renderer-independent result."""

    extraction: Any
    rendered: str | bytes
    synthesis: Any | None = None


def acquire(
    sources: Sequence[str | Path],
    *,
    input_type: str | None = None,
    crawl: bool = False,
    stdin: str | bytes | None = None,
    registry: Any | None = None,
) -> Any:
    """Acquire and, when needed, fuse one or more sources.

    ``sources`` accepts local files, directories/repositories, supported
    archives, HTTP(S) URLs, and ``"-"`` for an explicitly supplied ``stdin``
    payload.  Tier 2 acquisition outcomes remain attached to the returned
    collection.  No synthesis or rendering occurs here.
    """

    from . import router

    normalized = tuple(str(source) for source in sources)
    if not normalized:
        raise ValueError("at least one source is required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate sources are not allowed in one collection")
    if normalized.count("-") > 1:
        raise ValueError("stdin source '-' may appear at most once")
    if "-" in normalized and stdin is None:
        raise ValueError("stdin source '-' requires the stdin payload")
    if "-" not in normalized and stdin is not None:
        raise ValueError("a stdin payload requires '-' in sources")
    if len(normalized) > 1 and input_type is not None:
        raise ValueError("input_type is valid only for exactly one source")
    if crawl and (
        len(normalized) != 1 or not _is_http_url(normalized[0])
    ):
        raise ValueError("crawl requires exactly one HTTP(S) source")
    if crawl and input_type is not None:
        raise ValueError("input_type cannot be combined with crawl")

    results: list[Any] = []
    acquisitions: list[Any] = []
    for source in normalized:
        if source == "-":
            result = (
                router.extract_stdin(stdin, kind=input_type)
                if registry is None
                else router.extract_stdin(stdin, kind=input_type, registry=registry)
            )
        elif router.is_url(source):
            if input_type is not None:
                raise ValueError(
                    "input_type is valid for stdin and local paths; HTTP(S) "
                    "responses use their native signature and media type"
                )
            if crawl:
                acquisition = _acquire_collection(source, registry=registry)
                acquisitions.append(acquisition)
                results.extend(acquisition.extractions)
                continue
            result = (
                router.extract_url(source)
                if registry is None
                else router.extract_url(source, registry=registry)
            )
        elif "://" in source:
            scheme = source.split(":", 1)[0] or "unknown"
            raise ValueError(
                f"unsupported URL scheme {scheme!r}; only HTTP(S) is accepted"
            )
        else:
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(path)
            if path.is_dir():
                if input_type is not None:
                    raise ValueError("input_type cannot coerce a directory collection")
                acquisition = _acquire_collection(path, registry=registry)
                acquisitions.append(acquisition)
                results.extend(acquisition.extractions)
                continue
            try:
                result = (
                    router.extract(path, kind=input_type, registry=registry)
                    if input_type is not None and registry is not None
                    else router.extract(path, kind=input_type)
                    if input_type is not None
                    else router.extract(path, registry=registry)
                    if registry is not None
                    else router.extract(path)
                )
            except router.UnsupportedFormat as exc:
                if input_type is None and exc.kind == "archive" and exc.tier == 2:
                    acquisition = _acquire_collection(path, registry=registry)
                    acquisitions.append(acquisition)
                    results.extend(acquisition.extractions)
                    continue
                raise
        results.append(result)

    collection_intent = bool(acquisitions) or len(normalized) > 1
    if not collection_intent and len(results) == 1:
        return results[0]
    subject = (
        acquisitions[0].source
        if len(normalized) == 1 and len(acquisitions) == 1
        else _collection_subject(normalized)
    )
    return assemble_collection(results, subject=subject, acquisitions=acquisitions)


def assemble_collection(
    extractions: Sequence[Any],
    *,
    subject: str,
    acquisitions: Sequence[Any] = (),
) -> Any:
    """Assemble zero, one, or many routed leaves using product semantics."""

    from .unit import Extraction

    leaves = tuple(extractions)
    if len(leaves) >= 2:
        from .fusion import fuse

        combined = fuse(leaves, subject=subject)
    elif len(leaves) == 1:
        leaf = leaves[0]
        combined = Extraction(
            source=subject,
            kind="collection",
            units=list(leaf.units),
            relations=list(leaf.relations),
            gaps=list(leaf.gaps),
            meta=dict(leaf.meta),
            summary_claims=list(leaf.summary_claims),
        )
        combined.meta["fusion"] = {
            "backend": "not-applicable-single-routed-member-v1",
            "input_count": 1,
        }
    else:
        combined = Extraction(source=subject, kind="collection")
        combined.meta.update(
            {
                "inputs": [],
                "models": [],
                "fusion": {
                    "backend": "not-applicable-no-routed-members-v1",
                    "input_count": 0,
                },
            }
        )

    if acquisitions:
        combined.meta["collection_acquisitions"] = [
            acquisition.manifest for acquisition in acquisitions
        ]
        for acquisition in acquisitions:
            combined.gaps.extend(acquisition.gaps)
    return combined


def summarize(
    sources: Sequence[str | Path],
    *,
    input_type: str | None = None,
    crawl: bool = False,
    stdin: str | bytes | None = None,
    synthesis_config: Any | None = None,
    client: Any | None = None,
    output: str = "ansi",
    budget: int | None = None,
    cite: bool = True,
    color: bool = False,
    registry: Any | None = None,
) -> AutoTLDRResult:
    """Run the complete composable pipeline and return text plus typed state.

    Supplying ``synthesis_config`` enables the existing grounded model seam;
    omitting it keeps the measured deterministic Stage 1–4/5 substrate.  Model
    lifecycle ownership remains the caller's responsibility—this function does
    not load, unload, or select a resident model.
    """

    extraction = acquire(
        sources,
        input_type=input_type,
        crawl=crawl,
        stdin=stdin,
        registry=registry,
    )
    synthesis_result = None
    if synthesis_config is not None:
        from .synthesis import synthesize

        if extraction.kind != "collection":
            raise ValueError(
                "grounded synthesis requires a fused collection of at least two "
                "routed sources; single-source extraction remains deterministic"
            )
        synthesis_result = synthesize(
            extraction,
            synthesis_config,
            **({"client": client} if client is not None else {}),
        )
        extraction = synthesis_result.extraction
    elif client is not None:
        raise ValueError("client requires synthesis_config")

    if output == "pdf":
        if color:
            raise ValueError("PDF output does not support terminal color")
        if registry is not None:
            from .render import validate_extension_registry

            validate_extension_registry(registry)
        from .share import render_pdf

        rendered = render_pdf(extraction, budget=budget, cite=cite)
    else:
        from .render import render

        rendered = render(
            extraction,
            output=output,
            budget=budget,
            cite=cite,
            color=color,
            registry=registry,
        )
    return AutoTLDRResult(extraction, rendered, synthesis_result)


def _acquire_collection(source: str | Path, *, registry: Any | None) -> Any:
    from .collection import acquire_collection

    return (
        acquire_collection(source)
        if registry is None
        else acquire_collection(source, registry=registry)
    )


def _is_http_url(value: str) -> bool:
    return value.casefold().startswith(("http://", "https://"))


def _collection_subject(sources: Sequence[str]) -> str:
    if any(source == "-" or "://" in source for source in sources):
        return "<collection>"

    import os

    parents = [str(Path(source).parent) for source in sources]
    try:
        common = os.path.commonpath(parents)
    except ValueError:
        return "<collection>"
    return common or "."


__all__ = ["AutoTLDRResult", "acquire", "assemble_collection", "summarize"]
