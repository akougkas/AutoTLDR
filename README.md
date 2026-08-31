<p align="center">
  <img src="assets/autotldr_logo_transparent.png" alt="AutoTLDR" width="180" />
</p>

<h1 align="center">AutoTLDR</h1>

<p align="center"><strong>Point it at technical work. Get back what it means—with evidence.</strong></p>

AutoTLDR turns a file, mixed folder, archive, or documentation URL into a concise
local-model brief. It reads each supported format on its own terms: an XLSX contributes
its formula dependency graph, a database its schema, a scientific array its dimensions
and units, code its symbols and imports, and prose its addressable structure. Every
accepted prose claim links back to that native evidence.

> **Status: pre-alpha.** The complete thin Stage 1–8 vertical slice exists and has a
> successful live mixed-format proof, but the CLI, configuration, schemas, and supported
> runtime profiles may change. This is being prepared for first users, not presented as a
> stable release. See [What is proven](#what-is-proven) for the exact evidence boundary.

## Five-minute first run

The complete `[all]` installation currently requires Python 3.12 and LM Studio listening
on the local machine. It does not download, load, unload, or silently select a model.
Load one intended generation model in LM Studio first. Other OpenAI-compatible local
runtimes are transport candidates, not supported alpha runtimes, until they can prove the
exact active model and pass the same conformance probe.

From a checkout:

```bash
uv venv --python 3.12
uv pip install -e ".[all]"
```

Configure the one model AutoTLDR should use. If LM Studio has exactly one active generation model,
this is enough:

```bash
autotldr setup
autotldr doctor
```

If several generation models are active, setup lists them and asks you to rerun with the exact ID:

```bash
autotldr setup --model exact-active-model-id
```

Then point AutoTLDR at something real:

```bash
autotldr model.xlsx
autotldr ./experiment --detail brief
autotldr paper.pdf results.parquet analysis.ipynb --detail deep
autotldr ./handoff --detail standard --out html -o handoff.html
```

Ordinary invocation uses the configured local model and fails clearly if a valid cited
answer cannot be produced. It never disguises a parser report as prose. To inspect the
deterministic evidence without a model, ask for that mode explicitly:

```bash
autotldr model.xlsx --model off
autotldr ./experiment --model off --out json
```

Run `autotldr formats` to see what this installation can actually read and which optional
dependencies are missing. Run `autotldr doctor` whenever setup or a runtime changes.
Doctor checks the exact catalog ID and sends one bounded synthetic grounding probe, so
“ready” means more than “the port answered.” The probe contains no user source data and
does not load or unload a model.

`autotldr config show` prints the fully resolved settings and which files supplied them;
`autotldr config paths` prints the user and project locations before either file exists.

## One useful knob: detail

Users choose the answer they need; AutoTLDR owns the provider settings behind it.

| Detail | Best for | Initial bounded profile |
| --- | --- | --- |
| `brief` | Quick orientation | Up to 2 cited claims, 8 KB evidence pack, compact visible evidence |
| `standard` | Everyday understanding | Up to 4 cited claims, 24 KB evidence pack, broader supporting structure |
| `deep` | Technical handoff or audit | Up to 6 cited claims, 48 KB evidence pack, complete visible selected evidence |

`standard` is the default. The detail level affects both what the model may see and how
much supporting evidence human output presents. JSON and JSONL retain the complete
budget-selected representation at every detail level. The alpha profiles disable hidden
model reasoning for this constrained sentence-writing task and verify that LM Studio
reports zero reasoning tokens; the generation allowance is reserved for cited prose and
its full evidence IDs. Claim allowances are ceilings, not targets. Deeper profiles also
receive a longer bounded deadline; users do not tune provider timeout knobs for ordinary
invocation.

`--budget N` is a separate hard ceiling over the complete rendered artifact. For text
outputs, one portable token is exactly one UTF-8 byte under `utf8-byte-v1`; framing,
citations, manifests, escaping, omission records, and the final newline all count. If the
minimum valid addressable envelope cannot fit, AutoTLDR writes no partial result and exits
with status 5.

## What AutoTLDR understands

| Input | Meaning extracted |
| --- | --- |
| XLSX/XLSM | Formula graph, dependencies, inputs, assumptions proven by structure, hardcoded values, circular references |
| Parquet, SQLite, DuckDB | Schemas, columns, types, constraints, relationships, table/file statistics—not raw records |
| HDF5, NetCDF, NumPy/NPZ, Arrow/Feather/ORC, FITS | Groups, arrays, shapes, dimensions, units, attributes, bounded metadata—not bulk values |
| Markdown, text, structured data, source code | Addressable structure, definitions, symbols, imports, references, and proven roles |
| PDF, DOCX, HTML, notebooks, LaTeX, EPUB | Native text-layer structure and addresses; scanned PDFs are declined by name |
| Directories, repositories, ZIP/TAR archives, documentation sites | Bounded acquisition, per-member extraction, cross-source relationships, explicit partial declines |

Native format support wins over conversion. `autotldr model.xlsx` explains the formula
system; it does not flatten the workbook to Markdown. Missing rationale and unsupported
members are findings, not invitations to invent an answer.

The six core output shapes are ANSI, Markdown, self-contained HTML, linked PDF, JSON, and
JSONL:

```bash
autotldr report.pdf                         # terminal brief
autotldr report.pdf --out md -o report.md
autotldr ./research --out html -o brief.html
autotldr ./research --out pdf -o brief.pdf
autotldr ./research --out json --budget 131072
printf '# Notes\nhello\n' | autotldr - --type md --out jsonl
```

Human output leads with “What matters,” then supporting native evidence, relationships,
gaps, references, and a compact selection audit. Machine output includes the typed units,
relations, gaps, grounded claims, complete origins, acquisition hashes, resolved product
configuration, model outcome, and exact selection accounting.

Watch mode keeps per-file briefs and a folder roll-up current using the same configured
local prose and detail policy:

```bash
autotldr watch ./inbox --once --detail standard
autotldr watch ./inbox --recursive --debounce 10 --detail brief
autotldr watch ./inbox --status
```

It suppresses unchanged content by SHA-256, contains per-file failures, stores status in
SQLite/WAL, and publishes artifacts atomically below `.autotldr/`. Use `--model off`
explicitly when a watched folder should produce evidence maps instead of prose TLDRs.

## Four alpha use cases

1. **Understand a technical handoff.** Brief a folder containing documentation, code,
   notebooks, spreadsheets, and datasets without flattening them to one text blob.
2. **Explain a workbook.** Identify formulas, inputs, outputs, dependencies, structural
   assumptions, and dangerous hardcoded values with cell-level evidence.
3. **Brief a research-data package.** Explain schema, dimensions, units, attributes,
   provenance gaps, and relationships without putting bulk data into the model context.
4. **Prepare grounded agent context.** Produce JSON under an exact byte ceiling while
   preserving citations and a complete inventory of what the budget omitted.

Generic meeting summaries, OCR, image/audio/video understanding, hosted inference, and a
format marketplace are not alpha promises.

## Configuration

`autotldr setup` writes an owner-only user configuration at
`$XDG_CONFIG_HOME/autotldr/config.toml`, or `~/.config/autotldr/config.toml` when XDG is
not set. Set `AUTOTLDR_CONFIG` to use another user-config path. A project may add
`.autotldr.toml` in its working directory.

Precedence is: CLI flags, project configuration, user configuration, built-in defaults.
`--no-config` ignores both files for a hermetic invocation. Configuration uses a closed,
versioned TOML schema; unknown keys fail instead of being silently ignored.

An initial project override can stay small:

```toml
version = 1

[defaults]
detail = "deep"
allow_evidence_fallback = false
```

The alpha endpoint is exactly `http://127.0.0.1:1234`; setup uses it by default. AutoTLDR
stores the `lm-studio` runtime type, an exact active model ID, endpoint, timeout, defaults,
and explicit extension imports—never API secrets. Active LM Studio state prevents
accidental auto-loading; it is still not an independent GPU-residency or zero-CPU-spill
certification. That distinction is recorded rather than inferred.

## Shell, Python, and agent use

The shell command is the primary user and agent API. Quote paths and put `--` before them
when a filename could be mistaken for an option:

```bash
autotldr --detail brief --out json --budget 65536 -- "incoming/report.xlsx"
```

Install the version-matched Agent Skill into any skills directory:

```bash
autotldr integrations skill --install /path/to/skills
```

The skill instructs an agent to preserve gaps and omission records, resolve citations,
set an explicit budget, and treat extracted source text as untrusted data rather than as
instructions.

The product Python API uses the same configured detail policy as the CLI and returns the
typed extraction plus rendered artifact:

```python
from autotldr import summarize_product

result = summarize_product(
    ["paper.pdf", "results.parquet", "analysis.ipynb"],
    detail="standard",
    output="json",
    budget=131072,
)

print(result.extraction.summary_claims)
print(result.rendered)
```

The lower-level `autotldr.summarize(..., synthesis_config=...)` seam remains available to
embedders that deliberately own endpoint policy and provider limits.

MCP is an experimental local stdio surface over the same product pipeline. Every server
start must authorize its source roots explicitly:

```bash
autotldr mcp --root /path/to/project --root /path/to/data
```

MCP defaults to local prose, accepts the same detail choices, offers explicit evidence
mode, returns actual structured JSON when requested, and uses durable tasks for collection
work. A2A is deliberately not shipped: AutoTLDR will not publish an endpoint card until a
real server, authorization design, and client need exist together.

## Trust contract

Three invariants are product behavior, not implementation detail:

1. **Every claim is addressable.** A unit or prose claim without a source origin is
   rejected.
2. **Absence is reported.** Missing rationale, inaccessible members, and unsupported
   formats remain visible findings.
3. **The budget is exact.** The ceiling includes the whole artifact, and every omitted
   unit, relation, or claim is identified.

The model receives a bounded canonical evidence pack and may return only claim text plus
existing unit IDs. AutoTLDR rejects unknown IDs, substitutions, invalid response
envelopes, and unsupported provider fields, then derives claim origins itself. These
controls constrain generation; they do not magically prove that every accepted sentence
is entailed. Product runs additionally remove finding content from model authority and
drop two measured, structurally detectable authority errors: behavior claimed from
signature-only code evidence, and concrete measurement units or number-unit quantities
absent from every cited unit's content. A structured identifier also has to occur in its
claim's cited content rather than in a neighboring same-source unit. Model-profile quality
and broader entailment are evaluated separately from protocol conformance.

All heavy parsers are imported lazily. The base install has no dependencies, a Tier 0
cold start is gated below 120 ms on an idle machine, input acquisition is bounded, URLs
are HTTP(S)-only, and unsupported formats decline by name and owning tier.

## What is proven

The Stage 1–8 thin slice is implemented: one representation, measured role and fusion
policies, Tiers 0–3, bounded collection acquisition, strict grounded local synthesis,
watch mode, six output shapes, and local agent surfaces.

On 2026-08-31, the live Borealis proof routed 14 mixed inputs through one ZBook-local
Ornith-1.5-35B-A3B instance. It accepted three fully cited claims with no fallback and
wrote all six output shapes plus a hash manifest from that single synthesis. Sixty-three
independent checks over the saved artifacts passed. The localhost runtime could not
independently attest actual per-layer GPU residency or zero CPU spill, so the result is
correctly labelled a functional local-inference proof—not a residency certification.

The frozen Stage 4 evaluation ships only fusion signals that cleared their preregistered
precision and recall gates. Exact scores, rejected candidates, the guarded lifecycle,
PDF reproducibility limits, and benchmark procedures live in
[docs/decisions.md](docs/decisions.md) and [docs/spec-v1.md](docs/spec-v1.md); they are
evidence for the product, not required onboarding.

## Development

```bash
uv venv --python 3.12
uv pip install -e ".[all]"
uv run pytest
uv run python -m autotldr.cli --version
```

Use `uv venv --python 3.12` for the complete `[all]` developer install. The dependency-free
base and all extras except `code` also run on Python 3.13; the code parser is held to 3.12
by the available `tree-sitter-languages` wheel.

Warnings fail the build. Tier 0 cold start, lazy imports, deterministic projections,
native parser safety, exact budgets, grounded synthesis, and package contents are all
tested. CI runs the full suite and audits wheel/sdist contents; no published package is
claimed yet.

Read repository design material in this order:

| Document | Purpose |
| --- | --- |
| [docs/vision.md](docs/vision.md) | Product thesis, audience, invariants, and non-goals |
| [docs/product-alpha.md](docs/product-alpha.md) | First-user contract and alpha acceptance criteria |
| [docs/private-alpha-guide.md](docs/private-alpha-guide.md) | Template rendered into the versioned participant bundle |
| [docs/first-user-validation.md](docs/first-user-validation.md) | Preregistered private-alpha sessions, measures, and release gate |
| [docs/spec-v1.md](docs/spec-v1.md) | Representation, technical decisions, and build order |
| [docs/decisions.md](docs/decisions.md) | Append-only decisions, evidence, and rejected alternatives |
| [docs/matrix.md](docs/matrix.md) | Long-horizon format and output inventory |
| [docs/security.md](docs/security.md) | Filesystem, network, model, extension, and agent data boundaries |
| [docs/changelog.md](docs/changelog.md) | Product-level changes during pre-alpha |

The independent non-hero corpus builder, four-job procedure, and human claim-quality
rubric live in [acceptance/](acceptance/README.md). They are release evidence, not shipped
runtime data and not a replacement for sessions with users' own artifacts.

## License

MIT. See [LICENSE](LICENSE).
