# AGENTS.md

Orientation for any agent working in this repository.

## What this is

AutoTLDR is a Unix tool that understands semantics. Point it at a file, a folder,
or a URL and it returns what the thing *means*, rendered into the shape the
caller asked for. `autotldr model.xlsx` returns the formula dependency graph, not
a grid of numbers.

Pre-alpha. The thin Stage 1–8 MVP is complete. The same public pipeline now
acquires a file/folder/archive/URL, fuses its addressable representation,
optionally accepts strictly grounded local-model claims, and renders ANSI,
Markdown, HTML, PDF, JSON, or JSONL. Watch and agent surfaces wrap that pipeline;
they do not reimplement it.

The locked Tier 0/1 inventory, bounded Tier 2 directory/repository/archive/doc-
site acquisition, and the required Tier 3 XLSX, Parquet, SQLite, DuckDB, HDF5,
and NetCDF adapters are implemented. Additional science adapters are present,
but format-universe expansion is not the current sprint.

## Read in this order

1. `docs/vision.md` — the thesis, the three invariants, the non-goals
2. `docs/spec-v1.md` — stack decisions, the representation, build order
3. `docs/decisions.md` — settled decisions with rationale and rejected alternatives
4. `.agent/HANDOFF.md` — live state, untracked, and the next task

Check `docs/decisions.md` before proposing anything that sounds like a design
choice. It is probably settled, and the reasoning including what was rejected is
recorded. Supersede an entry with a dated one if you have a new reason; do not
quietly reverse it.

`archive/` holds six superseded planning documents. They are a record of what was
tried, not a foundation. Do not build on them.

## Invariants

These constrain code, not just prose. Breaking one is a bug regardless of how
good the output looks.

1. **Every claim is addressable.** No unit exists without an `Origin` pointing
   back into its source. A claim that cannot be grounded is dropped, not softened.
2. **Absence is reported.** `Extraction.gaps` is a result, not an error channel.
   "This spreadsheet documents no assumptions" is a finding. Never invent
   rationale a source does not contain.
3. **The budget is honored exactly.** A stated token ceiling is a ceiling, and
   what got dropped is reported.

## Conventions

- **Lazy imports, always.** Nothing heavy may be reachable from `autotldr.cli` at
  module scope. `tests/test_startup.py` fails the build if pymupdf, openpyxl,
  numpy, a tokenizer, or a runtime enters the import graph. Import inside the
  function that needs it.
- **Cold start is under 120ms** for a tier 0 file, enforced as a test. The test
  takes the best of repeated runs, so measure it on an otherwise idle machine.
- **Ordering never depends on the snapshot path.** Extraction runs against an
  immutable private snapshot whose directory name changes every invocation, and
  the router rewrites unit IDs to logical ones afterwards. Rank by canonical
  unit position, never by `Unit.id` (D-026).
- **Warnings fail the build.** `filterwarnings` is `error` plus one documented
  message-specific exception (D-028). A new warning is a defect, not noise.
- **Native format beats conversion.** XLSX goes to `openpyxl` on the formula
  layer, never through a markdown converter. The formula graph is the meaning.
- **Decline by name.** An unsupported format reports which format it was and
  which tier owns it. Never return an empty success.
- **Rules emit a named role only when Stage 2 proved it.** The deterministic
  extractor path may emit `Role.ASSUMPTION` for structurally proven spreadsheet
  inputs; everything else is `Role.UNKNOWN`. Local/frontier enrichment may emit
  only the backend-specific roles recorded in D-013.
- **Stage 4 dispositions are frozen.** Production fusion ships all literal and
  structural matches, the preregistered `native-native` identifier subtype, and
  the preregistered `local-path` unresolved-reference subtype. Strict scalar
  contradictions and orphan findings are disabled because they missed recall;
  the raw analyzer retains them only for diagnostics and fresh evaluation.
- **Model claims are constrained, not trusted.** Synthesis sees a bounded
  canonical evidence pack and may return only claim text plus existing unit IDs.
  AutoTLDR derives origins and rejects unknown IDs, model substitutions, invalid
  envelopes, and unsupported provider fields. The exact Stage 4 claims remain
  the model-off/failure source of truth.
- **Local model work is ZBook-only.** Evaluation uses
  `http://127.0.0.1:1234`, one AutoTLDR-owned generation model at a time, 100%
  GPU offload, and sequential estimate/load/run/unload. Never load, invoke, or
  unload a Dynamo/LM Link row. An embedding resident remains disabled until its
  value is separately measured.
- Local imports use relative paths within the package. Tests use `pytest`.

## Commands

```bash
uv venv --python 3.12 && uv pip install -e ".[all]"
uv run pytest
uv run python -m autotldr.cli FILE # try it
uv run python -m autotldr.cli DIR --out html -o brief.html
uv run python -m autotldr.cli watch DIR --once
uv run python -m autotldr.cli mcp --root .
```

Run the suite before committing.

## Session state

`.agent/` is gitignored working state for cross-session coordination. Read
`.agent/HANDOFF.md` on arrival; rewrite it, `state.md`, and append to `log/`
before you leave. Anything that will still be true in a month belongs in `docs/`
instead.
