"""The cold-start contract.

Python's import cost is the one thing that can make AutoTLDR feel unlike a Unix
tool, and it degrades silently: someone adds a convenient top-level import, every
invocation gets 200ms slower, and nobody notices for a month.

So it is a test. If these fail, the build fails.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BUDGET_MS = 120.0
RUNS = 5

# Parsers, tokenizers, and analytics engines. None of these may be reachable
# from the CLI's import graph until a file that needs one is actually routed.
HEAVY = (
    "pymupdf",
    "fitz",
    "openpyxl",
    "duckdb",
    "pyarrow",
    "h5py",
    "netCDF4",
    "numpy",
    "pandas",
    "tiktoken",
    "tree_sitter",
    "tree_sitter_languages",
    "trafilatura",
    "lxml",
    "yaml",
    "transformers",
    "torch",
    "onnxruntime",
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )


def test_cli_module_pulls_in_nothing_heavy():
    """Importing the entry point must not drag in a single parser."""
    proc = _run(
        "import sys; import autotldr.cli; "
        f"bad=[m for m in {HEAVY!r} if m in sys.modules]; "
        "print(','.join(bad))"
    )
    assert proc.returncode == 0, proc.stderr
    leaked = proc.stdout.strip()
    assert not leaked, f"autotldr.cli imports heavy modules at module scope: {leaked}"


def test_router_pulls_in_nothing_heavy():
    """Detection must be free. Only dispatch may cost anything."""
    proc = _run(
        "import sys; from autotldr.router import detect; "
        f"bad=[m for m in {HEAVY!r} if m in sys.modules]; "
        "print(','.join(bad))"
    )
    assert proc.returncode == 0, proc.stderr
    leaked = proc.stdout.strip()
    assert not leaked, f"autotldr.router imports heavy modules at module scope: {leaked}"


def test_reading_markdown_does_not_load_a_pdf_parser(md_file):
    """The lazy-import promise, verified end to end on a real file."""
    proc = _run(
        "import sys; from autotldr.router import extract; "
        f"extract(__import__('pathlib').Path({str(md_file)!r})); "
        f"bad=[m for m in {HEAVY!r} if m in sys.modules]; "
        "print(','.join(bad))"
    )
    assert proc.returncode == 0, proc.stderr
    leaked = proc.stdout.strip()
    assert not leaked, f"a markdown read loaded: {leaked}"


def test_tier_zero_cold_start_is_under_budget(md_file):
    """End-to-end wall time for the cheapest real invocation.

    Best-of-N rather than mean: this measures the tool, and a scheduler hiccup on
    a loaded machine is not the tool. A regression that matters will fail every
    run, not one in five.
    """
    best = float("inf")
    for _ in range(RUNS):
        start = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-m", "autotldr.cli", str(md_file)],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        elapsed = (time.perf_counter() - start) * 1000
        assert proc.returncode == 0, proc.stderr
        best = min(best, elapsed)

    assert best < BUDGET_MS, (
        f"tier 0 cold start was {best:.0f}ms against a {BUDGET_MS:.0f}ms budget. "
        f"Something heavy entered the import graph."
    )
