# Decision log

Append-only. Each entry records what was decided, why, what was rejected, and
what would justify revisiting. The point is that nobody has to relitigate a
settled question, and that when someone does reopen one, they start from the
reasoning rather than from scratch.

Add new entries at the top. Never edit a past entry; supersede it with a new one
that names the entry it replaces.

---

## 2026-08-30 · Session 1

The project had six planning documents written across four models between October
2025 and August 2026, and zero lines of implementation. This session archived all
of them, rebuilt the product framing from scratch, and shipped the first working
code. Everything below was decided in that session.

### D-012 · Prose is unwrapped; code is byte-exact

**Decided.** Hard line breaks inside a prose paragraph are joined. List items,
quotes, and table rows keep their breaks. Code blocks are never touched.

**Why.** A wrap point is not meaning. Leaving it in means `"power\nanalysis"`
never matches "power analysis", which silently breaks every phrase and identifier
match that folder fusion depends on. The same class of problem is why the PDF
extractor de-hyphenates across line breaks.

**Found by** a failing test during the Stage 1 spike, not by planning.

### D-011 · Cold start is a test, not an aspiration

**Decided.** Under 120ms to first output for a Tier 0 file, enforced in the test
suite. A parser entering the CLI's import graph fails the build.

**Why.** Python's import cost is the one thing that can make this feel unlike a
Unix tool, and it degrades silently: someone adds a convenient top-level import,
every invocation gets slower, nobody notices for a month.

**Measured** at 74ms best, 80ms median on first implementation.

### D-010 · Extractors precede the role eval

**Decided.** Build order is representation spike, then role-tagging eval, then
everything else. Supersedes an earlier draft that put the eval first.

**Why.** A dependency inversion: you cannot evaluate role tagging until
extractors exist to produce spans to label.

### D-009 · Role tagging is gated by an eval before implementation

**Decided.** Rules-only role assignment ships first. A 200-span labeled eval
across five formats, scored against rules-only, a 4B local model, and a frontier
ceiling, decides what happens next. Extractors emit `UNKNOWN` rather than
guessing.

**Why.** The `role` field is what makes the representation more than chunked
text, and a 3B local model may simply not be reliable at distinguishing a claim
from a caveat from an assumption. If rules land close to the 4B, v1 ships fully
offline. If neither approaches the ceiling, the taxonomy shrinks to what is
actually recoverable and the product claim shrinks with it.

**Open.** This is the largest unresolved risk in the project.

### D-008 · Extraction routes; it never converts

**Decided.** Each format goes to the extractor that understands it natively.
XLSX through `openpyxl` on the formula layer, never through a markdown converter.
Fast path by default; `--deep` opts into the slow accurate extractor.

**Why.** No single extractor wins across formats, and accuracy gaps above 20% on
complex layouts are normal. More importantly, conversion destroys exactly the
structure that carries the meaning: a spreadsheet's formula graph, a DOCX's
comments, a notebook's output pairing.

### D-007 · SQLite is the store; DuckDB is a transient compute engine

**Decided.** SQLite with WAL holds units, relations, folder state, the job queue,
and the cache. FTS5 for lexical, sqlite-vec for vectors, recursive CTEs for the
relation graph. DuckDB is spun up to profile a data file and closed. It is never
a persistent store.

**Why.** DuckDB reads CSV, Parquet, JSON, and Excel directly off disk with zero
ingestion, which turns Tier 3 profiling into a query rather than a parse-and-load
pipeline. Keeping it transient also means its single-writer limitation never
bites once the watch daemon and the CLI are both live.

**Rejected: SurrealDB.** Architecturally the most elegant option, and the one
explicitly evaluated. 3.0 went GA February 2026 with one engine covering
document, graph, relational, full-text, and vector in a single transaction, and
its live queries fit the watch daemon well. Four reasons against for v1: no
zero-ingestion file reading, which is worth more here than multi-model
unification; vector indexing described by SurrealDB itself as a foundation for
future specialized indexes rather than a shipped one; no independent embedded-mode
benchmarks, with nearly all technical detail tracing to their own material and
funding openly earmarked for hardening reliability; and an engine built to also
run distributed clusters is the wrong shape for a 120ms CLI.

**Revisit when** the watch daemon rather than the CLI becomes the primary entry
point. Live queries and a unified model then beat hand-rolling that on SQLite.
The storage layer stays behind one interface so it remains a swap.

**Also rejected.** LanceDB solves a vector-scale problem v1 does not have. Kùzu
is right if graph traversal becomes the bottleneck, and recursive CTEs over a few
thousand units will not be. Postgres with pgvector, Chroma, and Qdrant all need a
server, which disqualifies them for a Unix tool.

### D-006 · Python, distributed with uv

**Decided.** Python 3.12+, `uvx autotldr`. Base install has zero dependencies;
each format's parser lives in an extra and is lazy-imported at the point of use.

**Why.** The entire extraction ecosystem is Python: MarkItDown, Docling,
openpyxl, pypdf, pymupdf, python-docx, nbformat, tree-sitter bindings, h5py,
trafilatura. Rust gives a faster cold start and a static binary, and costs most of
year one reimplementing maintained extractors.

**Revisit when** the extractor set is stable and startup is still the complaint.
A Rust core calling Python extractors as subprocesses is the migration path, and
the CLI contract is designed so that swap is invisible to callers.

### D-005 · A watched folder emits per-file artifacts plus a folder roll-up

**Decided.** Each dropped file gets its own artifact; `FOLDER.tldr.md` holds the
fused collection view, rewritten on debounce.

**Why.** Useful at both granularities, and the per-file artifacts are nearly free
because they are already computed on the way to fusion. Versioned history is the
more interesting option and roughly double the work; it can be added later
without changing the on-disk contract.

### D-004 · The watch daemon ships in v1

**Decided.** Seven weeks to first release rather than five.

**Why.** "Auto" is in the name and it has to mean something. Invoke mode is a
tool and there are other tools; watch mode turns a directory into a
self-summarizing corpus, which is the differentiated half. The work does not
overlap with anything else on the critical path.

**Constraint that falls out.** The CLI must work with the daemon stopped,
uninstalled, or never started. A tool that requires a background service to
answer a question is not a Unix tool.

### D-003 · v1 is the mixed folder, text-only, Tiers 0 through 3

**Decided.** Point at a file or folder of text-derivable formats and get one
fused, cited bundle. No images, audio, or video.

**Why.** Cross-modal fusion is the thing genuinely nobody does, and per-file
summarization is a commodity. Restricting to text-derivable formats means nothing
in v1 requires a model to produce a useful result, so the tool degrades to
something valuable even with every model turned off.

**Rejected wedges.** Spreadsheet-only was the strongest single demo and needs no
model, but it is narrower than the folder. Meetings land in the slowest, most
model-dependent tier on day one. Version-pinned API answers sit closest to
Context7 and Ref, where the work is competing rather than flanking.

### D-002 · The incumbents are input adapters, not competitors

**Decided.** AutoTLDR shells out to Repomix, gitingest, crawl4ai, Context7,
Docling, MarkItDown, and Whisper, and treats their output as raw material.

**Why.** Every one of them is better at its narrow job than a new project will be
in year one. Treating them as competitors is what made the prior six plans
unbuildable. None of them produces a semantic representation, and that leftover
work is the entire product.

### D-001 · One representation, N inputs to M outputs

**Decided.** Every input normalizes into one intermediate representation; every
output renders from it. Adding an input costs one extractor and zero renderers.

**Why.** N formats times M shapes is N×M converters otherwise, which is the trap
that kills format-universal tools.

**Validated** by the Stage 1 spike against three deliberately dissimilar formats:
markdown (prose with an explicit outline), XLSX (a typed cell graph with no prose
at all), and PDF (prose with no structure but font sizes). Same `Unit`, `Origin`,
and `Relation` types held all three, relations resolved, IDs were stable, and both
renderers worked without special-casing any format.

### D-000 · The six prior plans are archived, not built on

**Decided.** `MASTER-PLAN-v4`, `GPT5-PLAN`, `GEMINI-PLAN`, three Haiku master
plans, the original `INITIAL-PLAN`, and a CLIO-ecosystem analysis all moved to a
gitignored `archive/`.

**Why.** Six restatements of a plan across ten months with no implementation is a
signal that planning had become the work. The strongest idea in the corpus was in
an informal ideas file rather than any master plan, and each successive plan
drifted further from it.

**Kept from them.** The three-dimensional context shape (memory, reasoning,
examples), attribution-first synthesis, budgeted selection, and the per-component
token-counted selector. All carried forward into `matrix.md`.
