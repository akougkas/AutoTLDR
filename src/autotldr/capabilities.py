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
    """Return one canonical inventory derived from the live public routes.

    ``declined`` describes inputs that the public acquisition surface cannot
    handle.  ``single_file_declines`` keeps the narrower router truth visible:
    an archive is not a leaf format, even though ZIP and TAR-family paths are
    supported collection roots.  Keeping those scopes separate prevents a
    capable archive collection from being advertised as globally unsupported.
    """

    from . import router
    from .collection import _ARCHIVE_SUFFIXES

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

    archive_suffixes = set(_ARCHIVE_SUFFIXES)
    # Path.suffix represents ``.tar.gz`` as ``.gz``.  Generic gzip is not a
    # supported collection, so only the exact deferred suffixes that name a
    # public ZIP/TAR-family root are removed from the global decline list.
    collection_deferred_suffixes = {
        suffix for suffix in router._DEFERRED if suffix in archive_suffixes
    }
    single_file_declines = []
    for suffix, (kind, tier) in sorted(router._DEFERRED.items()):
        item: dict[str, Any] = {
            "suffix": suffix,
            "kind": kind,
            "tier": tier,
            "status": "declined",
            "scope": "single-file-extraction",
        }
        if suffix in collection_deferred_suffixes:
            item["available_via"] = "collection-acquisition"
        single_file_declines.append(item)
    declined = [
        {
            "suffix": item["suffix"],
            "kind": item["kind"],
            "tier": item["tier"],
            "status": item["status"],
        }
        for item in single_file_declines
        if item["suffix"] not in collection_deferred_suffixes
    ]
    collection_capabilities = [
        {
            "name": "directory",
            "status": "available",
            "suffixes": [],
            "schemes": [],
        },
        {
            "name": "git-worktree",
            "status": "available",
            "suffixes": [],
            "schemes": [],
        },
        {
            "name": "zip",
            "status": "available",
            "suffixes": [".zip"],
            "schemes": [],
        },
        {
            "name": "tar",
            "status": "available",
            "suffixes": [
                suffix for suffix in _ARCHIVE_SUFFIXES if suffix != ".zip"
            ],
            "schemes": [],
        },
        {
            "name": "doc-site",
            "status": "available",
            "suffixes": [],
            "schemes": ["http", "https"],
        },
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
        # Keep the compact name list for v1 consumers; the structured rows are
        # authoritative for status, suffix, and scheme negotiation.
        "collections": [item["name"] for item in collection_capabilities],
        "collection_capabilities": collection_capabilities,
        "outputs": ["ansi", "md", "html", "pdf", "json", "jsonl"],
        "output_capabilities": output_capabilities,
        "detail_levels": ["brief", "standard", "deep"],
        "declined": declined,
        "single_file_declines": single_file_declines,
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
