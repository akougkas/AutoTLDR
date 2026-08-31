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
            extraction = assemble_collection(
                (extraction,),
                subject=extraction.source,
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


def apply_product_synthesis(
    extraction: Any,
    *,
    detail: str | None = None,
    mode: str = "prose",
    allow_evidence_fallback: bool | None = None,
    use_config: bool = True,
    product_config: Any | None = None,
    client: Any | None = None,
) -> tuple[Any, Any | None]:
    """Apply the first-user synthesis contract to one acquired representation.

    This is the shared policy seam for the CLI and agent surfaces.  ``mode='prose'``
    requires the explicitly configured loopback model; ``mode='evidence'`` records a
    clearly labelled model-off run.  It never owns model loading or residency.
    """

    from .product import DETAIL_PROFILES, load_product_config

    if mode not in {"prose", "evidence"}:
        raise ValueError("mode must be prose or evidence")
    config = product_config or load_product_config(use_config=use_config)
    detail_name = detail or config.detail
    if detail_name not in DETAIL_PROFILES:
        raise ValueError("detail must be brief, standard, or deep")
    if allow_evidence_fallback is not None and not isinstance(
        allow_evidence_fallback, bool
    ):
        raise TypeError("allow_evidence_fallback must be a boolean or None")
    allow_fallback = (
        config.allow_evidence_fallback
        if allow_evidence_fallback is None
        else allow_evidence_fallback
    )
    detail_profile = DETAIL_PROFILES[detail_name]
    resolved = config.as_manifest()
    resolved["detail"] = detail_profile.as_manifest()
    resolved["allow_evidence_fallback"] = allow_fallback

    if mode == "evidence":
        extraction.meta["product"] = {
            "schema": "autotldr-product-run-v1",
            "mode": "evidence",
            "detail": detail_profile.as_manifest(),
            "resolved_config": resolved,
        }
        return extraction, None

    from .product import require_active_model, require_configured_model
    from .synthesis import EndpointPolicy, SynthesisConfig, synthesize

    model = require_configured_model(config)
    require_active_model(model)
    wrapped_single = extraction.kind != "collection"
    synthesis_input = (
        assemble_collection((extraction,), subject=extraction.source)
        if wrapped_single
        else extraction
    )
    synthesis_result = synthesize(
        synthesis_input,
        SynthesisConfig(
            model=model.model,
            endpoint=model.endpoint,
            endpoint_policy=EndpointPolicy(),
            evidence_budget_bytes=detail_profile.evidence_budget_bytes,
            timeout_seconds=min(
                float(model.timeout_seconds), detail_profile.timeout_seconds
            ),
            max_output_tokens=detail_profile.max_output_tokens,
            max_claims=detail_profile.max_claims,
            reasoning_effort=detail_profile.reasoning_effort,
            product_detail=detail_profile.name,
            include_findings=False,
            fallback_on_failure=allow_fallback,
        ),
        **({"client": client} if client is not None else {}),
    )
    completed = synthesis_result.extraction
    if wrapped_single:
        # The collection wrapper satisfies the synthesis proof boundary; it is not the
        # source's public type. Restore the native kind for callers and renderers.
        from dataclasses import replace

        from .unit import Extraction

        completed = Extraction(
            source=extraction.source,
            kind=extraction.kind,
            units=list(completed.units),
            relations=list(completed.relations),
            gaps=list(completed.gaps),
            meta=dict(completed.meta),
            summary_claims=list(completed.summary_claims),
        )
        try:
            synthesis_result = replace(synthesis_result, extraction=completed)
        except TypeError:  # lightweight compatible result used by embedders/tests
            try:
                synthesis_result.extraction = completed
            except (AttributeError, TypeError):
                pass
    completed.meta["product"] = {
        "schema": "autotldr-product-run-v1",
        "mode": "evidence-fallback" if synthesis_result.used_fallback else "prose",
        "detail": detail_profile.as_manifest(),
        "resolved_config": resolved,
    }
    return completed, synthesis_result


def summarize_product(
    sources: Sequence[str | Path],
    *,
    detail: str | None = None,
    mode: str = "prose",
    allow_evidence_fallback: bool | None = None,
    use_config: bool = True,
    product_config: Any | None = None,
    input_type: str | None = None,
    crawl: bool = False,
    stdin: str | bytes | None = None,
    output: str = "ansi",
    budget: int | None = None,
    cite: bool = True,
    color: bool = False,
    registry: Any | None = None,
    client: Any | None = None,
) -> AutoTLDRResult:
    """Run acquisition, the product synthesis policy, and one output projection."""

    from .product import load_product_config

    resolved_config = product_config or load_product_config(use_config=use_config)
    if mode == "prose":
        from .product import require_active_model, require_configured_model

        require_active_model(require_configured_model(resolved_config))
    if registry is not None and resolved_config.extensions:
        raise ValueError(
            "an explicit registry cannot be combined with configured extension imports"
        )
    if registry is None and resolved_config.extensions:
        registry = _load_product_extensions(resolved_config.extensions)
    extraction = acquire(
        sources,
        input_type=input_type,
        crawl=crawl,
        stdin=stdin,
        registry=registry,
    )
    extraction, synthesis_result = apply_product_synthesis(
        extraction,
        detail=detail,
        mode=mode,
        allow_evidence_fallback=allow_evidence_fallback,
        use_config=use_config,
        product_config=resolved_config,
        client=client,
    )
    if output == "pdf":
        if color:
            raise ValueError("PDF output does not support terminal color")
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


def _load_product_extensions(references: Sequence[str]) -> Any:
    """Load only imports named by resolved product configuration."""

    from . import router
    from .collection import validate_extension_registry as validate_acquisitions
    from .extensions import ExtensionRegistry, load_extension
    from .render import validate_extension_registry as validate_renderers

    registry = ExtensionRegistry()
    for reference in references:
        load_extension(reference, registry)
    router.validate_extension_registry(registry)
    validate_acquisitions(registry)
    validate_renderers(registry)
    return registry


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


__all__ = [
    "AutoTLDRResult",
    "acquire",
    "apply_product_synthesis",
    "assemble_collection",
    "summarize",
    "summarize_product",
]
