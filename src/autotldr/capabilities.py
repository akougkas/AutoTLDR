"""Runtime-derived capability inventory for onboarding and agent negotiation."""

from __future__ import annotations

import importlib.util
from collections import defaultdict
from typing import Any


_DEPENDENCY_BY_KIND = {
    "yaml": ("yaml", "structured"),
    "pdf": ("pymupdf", "pdf"),
    "xlsx": ("openpyxl", "office"),
    "parquet": ("pyarrow", "data"),
    "duckdb": ("duckdb", "data"),
    "hdf5": ("h5py", "data"),
    "netcdf": ("netCDF4", "data"),
    "numpy": ("numpy", "data"),
    "arrow": ("pyarrow", "data"),
    "arrow-stream": ("pyarrow", "data"),
    "feather": ("pyarrow", "data"),
    "orc": ("pyarrow", "data"),
}


def runtime_capabilities() -> dict[str, Any]:
    """Return one canonical inventory derived from the live router."""

    from . import router

    grouped: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    for suffix, handler in router._BY_SUFFIX.items():
        status = (
            "declined"
            if suffix in router._UNAVAILABLE_SOURCE_SUFFIXES
            else _handler_status(handler.kind)
        )
        grouped[(handler.kind, handler.tier, status)].append(suffix)

    formats: list[dict[str, Any]] = []
    for (kind, tier, status), suffixes in sorted(
        grouped.items(), key=lambda item: (item[0][1], item[0][0], item[0][2])
    ):
        dependency = _DEPENDENCY_BY_KIND.get(kind)
        formats.append(
            {
                "kind": kind,
                "tier": tier,
                "status": status,
                "suffixes": sorted(suffixes),
                "install": (
                    None
                    if dependency is None or status != "missing-dependency"
                    else f"autotldr[{dependency[1]}]"
                ),
            }
        )

    declined = [
        {"suffix": suffix, "kind": kind, "tier": tier, "status": "declined"}
        for suffix, (kind, tier) in sorted(router._DEFERRED.items())
    ]
    pdf_available = (
        importlib.util.find_spec("pymupdf") is not None
        or importlib.util.find_spec("fitz") is not None
    )
    output_capabilities = [
        {"name": name, "status": "available", "install": None}
        for name in ("ansi", "md", "html", "json", "jsonl")
    ]
    output_capabilities.insert(
        3,
        {
            "name": "pdf",
            "status": "available" if pdf_available else "missing-dependency",
            "install": None if pdf_available else "autotldr[pdf]",
        },
    )
    return {
        "schema": "autotldr-runtime-capabilities-v1",
        "inputs": formats,
        "collections": ["directory", "git-worktree", "zip", "tar", "doc-site"],
        "outputs": ["ansi", "md", "html", "pdf", "json", "jsonl"],
        "output_capabilities": output_capabilities,
        "detail_levels": ["brief", "standard", "deep"],
        "declined": declined,
    }


def _handler_status(kind: str) -> str:
    dependency = _DEPENDENCY_BY_KIND.get(kind)
    if dependency is None:
        return "available"
    return (
        "available"
        if importlib.util.find_spec(dependency[0]) is not None
        else "missing-dependency"
    )


__all__ = ["runtime_capabilities"]
