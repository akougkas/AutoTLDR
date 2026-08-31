"""AutoTLDR: point it at anything, get back what it means.

Nothing heavy is imported here. The package root is on the cold-start path of
every invocation, including ``autotldr --help``, and the startup contract in
tests/test_startup.py enforces that.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ._version import __version__

if TYPE_CHECKING:
    from .api import AutoTLDRResult
else:
    # Keep runtime introspection safe without importing the public pipeline on
    # every CLI start. The concrete result type remains available from
    # ``autotldr.api`` once a pipeline function is actually called.
    AutoTLDRResult = Any


def acquire(
    sources: Sequence[str | Path] | str | Path,
    *,
    input_type: str | None = None,
    crawl: bool = False,
    stdin: str | bytes | None = None,
    registry: Any | None = None,
    acquirer: str | None = None,
) -> Any:
    """Lazily acquire/fuse sources through :mod:`autotldr.api`."""

    from .api import acquire as _acquire

    return _acquire(
        sources,
        input_type=input_type,
        crawl=crawl,
        stdin=stdin,
        registry=registry,
        acquirer=acquirer,
    )


def summarize(
    sources: Sequence[str | Path] | str | Path,
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
    acquirer: str | None = None,
) -> AutoTLDRResult:
    """Lazily run AutoTLDR's composable public pipeline."""

    from .api import summarize as _summarize

    return _summarize(
        sources,
        input_type=input_type,
        crawl=crawl,
        stdin=stdin,
        synthesis_config=synthesis_config,
        client=client,
        output=output,
        budget=budget,
        cite=cite,
        color=color,
        registry=registry,
        acquirer=acquirer,
    )


def summarize_product(
    sources: Sequence[str | Path] | str | Path,
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
    acquirer: str | None = None,
    client: Any | None = None,
) -> AutoTLDRResult:
    """Lazily run the configured first-user product pipeline."""

    from .api import summarize_product as _summarize_product

    return _summarize_product(
        sources,
        detail=detail,
        mode=mode,
        allow_evidence_fallback=allow_evidence_fallback,
        use_config=use_config,
        product_config=product_config,
        input_type=input_type,
        crawl=crawl,
        stdin=stdin,
        output=output,
        budget=budget,
        cite=cite,
        color=color,
        registry=registry,
        acquirer=acquirer,
        client=client,
    )


__all__ = ["__version__", "acquire", "summarize", "summarize_product"]
