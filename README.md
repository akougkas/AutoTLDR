<p align="center">
  <img src="assets/autotldr_logo_transparent.png" alt="AutoTLDR" width="180" />
</p>

<h1 align="center">AutoTLDR</h1>

<p align="center"><strong>Point it at anything. Get back what it means, in the shape you asked for.</strong></p>

---

`file` tells you what something is. `pandoc` converts format A into format B.
AutoTLDR tells you what something *means*, and renders that meaning into whatever
shape the caller needs.

```bash
autotldr report.pdf                              # local path, ANSI to stdout
autotldr https://docs.example.com/guide --out md # HTTP(S) URL, Markdown
printf '# Notes\nhello\n' | autotldr - --type md --out jsonl
autotldr model.xlsx --out json                   # the formula graph, not the cells
autotldr paper.md results.csv analysis.ipynb     # explicit sources, fused
autotldr ./research --out html -o brief.html     # bounded collection acquisition
autotldr ./research --out pdf -o brief.pdf       # shareable, linked PDF
autotldr watch ./inbox --once                    # per-file TLDRs plus folder roll-up
```

It is a Unix tool. It reads a path, a URL, or stdin. It writes to stdout or a
file. It exits non-zero and says why. Coding agents get it for free, because
agents call bash.

> **Status: pre-alpha, complete thin MVP.** The Stage 1–8 vertical slice is
> implemented: one representation, measured role and fusion policies, Tiers 0–3,
> bounded collection acquisition, strict grounded local synthesis, watch mode,
> six core output shapes, and local agent surfaces. On 2026-08-31 the live
> Borealis proof ran 14 mixed inputs through one ZBook-local Ornith-1.5-35B-A3B
> instance and accepted three fully cited claims with no fallback, writing all
> six shapes plus a hash manifest from that single synthesis; 63 independent
> checks over the saved artifacts passed. Independent zero-CPU-spill residency
> certification remains unavailable and is reported as such, not inferred.
> Nothing here is stable. See [`docs/spec-v1.md`](docs/spec-v1.md) for what is
> being built and in what order.

## What makes it different

**It extracts semantics, not bytes.** "Summarize" means something different for
every format, and treating them all as text is why generic retrieval fails on
most of them:

| You point it at | It gives you back |
| --- | --- |
| A spreadsheet | The formula dependency graph. Inputs, assumptions, derived values, circular references, and the number somebody hardcoded into a formula in row 400 |
| A dataset | Schema, units, distributions, gaps. Never the values |
| A paper | Addressable units and structure, with unproved semantic roles left `unknown` |
| A repo | Architecture, entry points, the hazards |
| A folder of all four | A cited semantic TLDR of how they relate and where they disagree |

**Every claim is addressable.** No output sentence exists without a pointer back
into its source: `page:7#span:3`, `Sheet2!C14`, `src/auth.py:88`, `line:120-134`.
A summary you can verify is a summary you can trust.

**Absence is a finding.** If a source documents no rationale, AutoTLDR says so
rather than inventing one. "No file in this folder documents why the threshold is
0.7" is useful output.

**The competition is the toolchain.** Repomix, crawl4ai, Context7, Docling and
Whisper are good at turning things into clean text, and AutoTLDR is designed to
treat them as input adapters. The work left after they run is the product.

## The implemented MVP

The CLI accepts a local path, directory/repository/archive, `-` for stdin, or an
HTTP(S) URL; `--crawl` turns one documentation URL into a bounded same-origin
collection. Two or more explicitly named sources are fused as one collection.
It writes ANSI (the default), Markdown, self-contained HTML, linked PDF, JSON,
or JSONL to stdout or a file and keeps every extracted claim tied to an
addressable origin.
Human output includes inline citations by default. `--no-cite` moves those
references into stable IDs and a source map, while structured output always
retains origins.

`--budget N` is a hard limit over the complete rendered UTF-8 byte stream under
the named `utf8-byte-v1` estimator: framing, escaping, citations, ANSI bytes,
manifest data, omission records, and the final newline all count. `--out pdf`
applies the same selection and the same complete omission inventory but counts
raw PDF bytes under the separately named `binary-byte-v1` estimator, and the
output carries that counter name. Every omitted unit or relation is identified
in the output. If even the required envelope cannot fit, AutoTLDR emits no
partial stdout and exits with the budget status. `Unit.tokens` is a cheap
diagnostic estimate only and never drives this limit.

Selection is non-monotone, because completing a multi-unit claim removes that
claim's drop record and can shrink the output. The human shapes therefore probe
every ranked prefix instead of binary-searching, which costs one full render per
unit. On a 1750-unit source tree a budgeted `--out md` took 784s where the same
budget on `--out json` took 5.1s. Collections of a few hundred units stay inside
a few seconds. See D-029 for why the guarantee is kept and the cost published.

JSON and JSONL include a machine manifest covering the acquired input and its
hash, timings, representation and tool versions, model and role-backend use,
estimator and ID schemes, fusion policy, and complete selection accounting.
Ordinary CLI invoke is deliberately useful and deterministic with every model
off. Grounded synthesis is a separate public API seam: a model sees only a
bounded canonical evidence pack and may return only claims citing existing unit
IDs. AutoTLDR validates those IDs and derives every origin itself. Invalid,
timed-out, or unavailable model output preserves the exact measured Stage 4
claims unless the caller explicitly requires synthesis.

Stage 4 measured each signal independently and ships only what cleared its
frozen gate:

| Signal or finding | Precision | Recall | Production disposition |
| --- | ---: | ---: | --- |
| Literal reference | 1.000 | 0.957 | Ships completely |
| Identifier correspondence | 0.850 | 0.830 | Ships only the preregistered `native-native` subtype (P=.900, R=.857) |
| Structural correspondence | 1.000 | 0.800 | Ships completely |
| Strict scalar contradiction | 1.000 | 0.667 | Disabled; missed the .70 recall gate |
| Orphan absence | 1.000 | 0.571 | Disabled; missed the .90 recall gate |
| Unresolved reference | 0.900 | 1.000 | Ships only the preregistered `local-path` subtype (P=R=1.000) |

The diagnostic analyzer retains every raw candidate, but the user-facing fusion
path filters disabled signals and names that policy in the manifest. Its three
collection sentences remain the safe model-off fallback; accepted model claims
are separately identified in the manifest.

Watch mode reuses the same public pipeline. It polls safely on local or network
filesystems, suppresses unchanged content by SHA-256, contains per-file errors,
stores status in SQLite/WAL, writes atomic per-file Markdown artifacts, and
maintains `.autotldr/FOLDER.tldr.md`:

```bash
autotldr watch ./inbox --once
autotldr watch ./inbox --status
autotldr watch ./inbox --recursive --debounce 10
```

For code and agent integrations, the composable API returns both the typed
extraction and its rendered output:

```python
from autotldr import summarize
from autotldr.synthesis import SynthesisConfig

result = summarize(
    ["research/"],
    synthesis_config=SynthesisConfig(
        model="ornith-1.5-35b-a3b",
        max_output_tokens=4096,
        fallback_on_failure=False,
    ),
    output="html",
)
print(result.extraction.summary_claims)
```

The model lifecycle remains caller-owned because LM Link can expose a Dynamo
row through the localhost catalog. The checked-in complete-demo runner therefore
requires an exact already-loaded, independently attested ZBook model and never
loads, unloads, downloads, or selects one itself:

```bash
uv run python examples/mvp_demo.py \
  benchmarks/synthesis/hero/borealis \
  --model autotldr-mvp-final \
  --output-dir .agent/demo/2026-08-31-live
```

That one call writes all six shapes — ANSI, Markdown, HTML, PDF, JSON, JSONL —
plus a hash/claim manifest, from one accepted synthesis result. The model is
called exactly once; `tests/test_mvp_demo.py` asserts both that count and that
every shape is a projection of the same accepted `Extraction`.

`--model` names an instance that must already be resident and verified. Getting
it there is the guarded lifecycle's job, and `examples/mvp_demo_lifecycle.py` is
the certification path. On a host with no process-level residency attestor it
refuses to run at all:

```text
AutoTLDR MVP lifecycle error: no ZBook actual-residency attestor is configured;
configuration-only GPU claims are not accepted
```

That refusal is the designed behaviour and is not weakened. The 2026-08-31 proof
therefore used the same guarded primitives — exact unprefixed catalog
resolution, a required `GPU Offload: 100%` estimate, `--gpu max`, exclusive
local residency, unload by exact owned identifier, and incumbent restoration —
and labelled its result a functional ZBook-local inference proof rather than a
certified one.

| Exit | Meaning |
| ---: | --- |
| `0` | Success |
| `1` | Runtime or extraction error |
| `2` | Invalid CLI usage |
| `3` | Unsupported format or tier, declined by name |
| `4` | Input not found |
| `5` | The requested budget cannot be satisfied |

## Install

Not published yet. From a checkout:

```bash
uv venv && uv pip install -e ".[all]"
```

The base install has zero dependencies. Each format's parser lives in an extra
and is lazy-imported at the point of use, so `autotldr notes.md` never pays for a
PDF parser it will not call. Cold start for a Tier 0 file is held under 120ms by
a test that fails the build if it regresses; it measures the best of repeated
runs, so a heavily loaded machine can exceed it.

The `code` extra, and therefore `[all]`, requires **Python 3.12**:
`tree-sitter-languages==1.10.2` publishes no wheel for 3.13 or later. Every
other extra — `data`, `office`, `pdf`, `structured`, `web` — installs and runs
on 3.13, so `pip install 'autotldr[data,office,pdf,structured,web]'` is the
newer-Python path until that pin can move.

## Documentation

Read them in this order.

| Document | What it covers |
| --- | --- |
| [`docs/vision.md`](docs/vision.md) | The stable part. Product thesis, positioning, audience, the three invariants, and the non-goals |
| [`docs/matrix.md`](docs/matrix.md) · [html](docs/matrix.html) | The full product matrix: 53 input formats across 7 tiers, 10 output shapes, distribution surfaces, and what "summarize" actually means for each format |
| [`docs/spec-v1.md`](docs/spec-v1.md) · [html](docs/spec-v1.html) | v1 build spec: stack decisions and rationale, the representation, folder fusion, the watch daemon, and build order |
| [`docs/decisions.md`](docs/decisions.md) | Append-only decision log. What was chosen, why, what was rejected, and what would justify revisiting |

The two `.html` pages are hand-maintained companions to their Markdown, readable
by opening the file. The matrix page filters its 53 formats by tier and by
whether they run fully offline. Markdown stays canonical; if the two disagree,
the Markdown wins.

Earlier planning documents, the original README, and vendored reference material
are preserved untracked under `archive/`. They are superseded and kept only as a
record of what was tried.

## Status

All eight MVP stages have a working vertical slice. The locked text-derived
format boundary is explicit:

| Tier | Current implementation |
| --- | --- |
| **0** | Complete for the locked v1 text, source-code, and structured-data inventory |
| **1** | Complete for text-layer PDF, DOCX, HTML/one URL, notebooks, LaTeX, and EPUB |
| **2** | Bounded directory/repository, ZIP/TAR archive, and same-origin documentation-site acquisition with partial named declines |
| **3** | XLSX/XLSM formula graphs; Parquet, SQLite, DuckDB, HDF5, NetCDF; metadata-only NumPy/NPZ, Arrow/Feather/ORC, and FITS science adapters |

The software and functional Stage 5 gate is complete: the real Borealis fixture
routes Markdown, JSON, Python, HTML, notebook, CSV, XLSX, Parquet, SQLite,
DuckDB, HDF5, NetCDF, and a ZIP member into 135 units, 105 relations, 15 explicit
gaps, and one strict local synthesis. The localhost LM Studio and local process
APIs still cannot independently attest actual per-layer GPU residency or prove
zero CPU spill. The certification wrapper therefore fails closed rather than
accept a configuration-only GPU claim, and the successful run is labelled a
functional ZBook-local inference proof. AutoTLDR does not fabricate a hardware
guarantee the runtime cannot expose.

Two reproducibility limits are worth knowing before you rely on byte identity.
Rendering one acquired `Extraction` to ANSI, Markdown, HTML, JSON, or JSONL is
byte-identical, in one process and across processes. `--out pdf` is
byte-identical within one process only, because `pymupdf.Story` lays identical
HTML out differently in different processes; page count, text, links, and the
complete omission inventory are stable regardless. And JSON and JSONL carry a
`manifest.timings` block of wall-clock milliseconds, so two runs over the same
bytes differ there by design.

Stage 8 provides `autotldr mcp` / `autotldr-mcp`, a concise skill under
`integrations/skills/autotldr`, and a static loopback A2A card. The MCP server is
stdio-only, local-path-only, model-off, lazy, and uses the Tasks extension for
collection work. It wraps the same public API rather than implementing a second
summarizer.

See
[`docs/spec-v1.md`](docs/spec-v1.md#part-7-build-order) for the full order.

```bash
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
