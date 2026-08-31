"""Fixtures for the representation spike.

The three sources are built here rather than committed as binaries so that what
each one contains is readable, and so the assertions in test_representation.py
can point at specific known facts.
"""

from __future__ import annotations

import pytest

MARKDOWN = """\
# Throughput Study

Notes for the Q3 measurement run. Raw numbers live in [results](results.csv)
and the harness is `bench/measure.py`.

## Method

We ran each configuration 40 times and took the median.

1. Warm the page cache
2. Drop caches between repeats
3. Record with `--output-format=json`

**Median** — the middle value once the 40 runs are sorted.

Note that the 40-run count was chosen for wall-clock reasons, not from a power
analysis.

```python
def measure_throughput(path: str) -> float:
    return bytes_read(path) / elapsed(path)
```

## Results

See https://example.org/runs/2026-q3 for the full series.
"""


@pytest.fixture
def md_file(tmp_path):
    path = tmp_path / "study.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    return path


@pytest.fixture
def xlsx_file(tmp_path):
    """A small model with labeled inputs, derived cells, and one buried constant."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Inputs"
    ws["A2"] = "Node count"
    ws["B2"] = 8
    ws["A3"] = "Per-node throughput (MB/s)"
    ws["B3"] = 450
    ws["A4"] = "Overhead factor"
    ws["B4"] = 0.92

    ws["A6"] = "Derived"
    ws["A7"] = "Raw aggregate"
    ws["B7"] = "=B2*B3"
    ws["A8"] = "Effective aggregate"
    ws["B8"] = "=B7*B4"
    # The buried override: a magic number typed straight into a formula.
    ws["A9"] = "Reported figure"
    ws["B9"] = "=B8*0.87"

    sheet2 = wb.create_sheet("Summary")
    sheet2["A1"] = "Headline"
    sheet2["B1"] = "=Model!B9"

    path = tmp_path / "model.xlsx"
    wb.save(path)
    wb.close()
    return path


@pytest.fixture
def pdf_file(tmp_path):
    """A two-page document with a large heading and body text."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()

    page = doc.new_page()
    page.insert_text((72, 100), "Throughput Under Contention", fontsize=20)
    page.insert_text((72, 140), "1  Introduction", fontsize=13)
    page.insert_text(
        (72, 170),
        "We measure aggregate throughput across eight nodes.",
        fontsize=10,
    )
    page.insert_text(
        (72, 190),
        "However, the harness does not isolate network interference.",
        fontsize=10,
    )

    page2 = doc.new_page()
    page2.insert_text((72, 100), "2  Results", fontsize=13)
    page2.insert_text(
        (72, 130),
        "We observe a 12 percent improvement over the baseline.",
        fontsize=10,
    )
    page2.insert_text((72, 150), "See https://example.org/runs/2026-q3", fontsize=10)
    page2.insert_text((72, 170), "Figure 1: Throughput by node count", fontsize=10)

    path = tmp_path / "paper.pdf"
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def scanned_pdf(tmp_path):
    """A PDF with pages but no text layer, which v1 must decline honestly."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    return path
