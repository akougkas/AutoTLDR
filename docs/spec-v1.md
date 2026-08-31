# AutoTLDR v1 — Technical Specification

**Scope target locked:** the mixed folder, text-only, Tiers 0 through 3. No images, no
audio, no video. The v1 target is to point AutoTLDR at a file or folder and get back one
fused context bundle.

**Implementation status:** the complete thin Stage 1–8 MVP is implemented. One public
pipeline acquires files, folders, archives, stdin, and URLs; normalizes every supported
input into the same addressable representation; applies the measured role/fusion policy;
optionally accepts strictly ID-grounded local-model claims; and renders six output shapes.
Watch, MCP, the Agent Skill, and A2A metadata wrap that pipeline. The software gate is
complete; independent physical proof of zero CPU spill remains unavailable from the
current LM Studio/process telemetry and is reported separately rather than invented.

This document makes the stack decisions and the representation concrete. It supersedes
nothing in [`matrix.md`](matrix.md); the matrix is the menu, this is what we cook
first.

---

## Part 1: What v1 is

Two execution modes over one engine.

### Mode 1 — Invoke

```bash
autotldr report.pdf                                  # one local path
printf '# Notes\nhello\n' | autotldr - --type md    # stdin
autotldr https://docs.example.com/guide --out md     # one HTTP(S) URL
autotldr paper.pdf analysis.ipynb results.xlsx       # explicit collection
```

Synchronous. Feels like `cat` for Tier 0 and remains bounded for Tier 1. Writes to stdout
by default.

#### Stage 3–5 invoke contract — implemented

Stage 3 processes one acquired source. Stage 4 fuses two or more sources. Stage 5 adds
bounded directory/repository/archive/doc-site acquisition, the locked Tier 3 adapters,
and the public grounded-synthesis seam. Ordinary CLI invoke remains deterministic and
model-off; callers opt into synthesis through the Python API or the guarded demo path.

| Concern | Implemented contract |
| --- | --- |
| Input | One or more local paths, directory/repository/archive collections, `-` for stdin, or HTTP(S) URLs. `--crawl` performs a bounded same-origin documentation crawl. `--type` supplies an explicit format only for one stdin or deliberately mislabeled local source |
| Output | `ansi` by default, plus `md`, self-contained `html`, linked `pdf`, `json`, and `jsonl`; text or binary-safe output to stdout or `-o` / `--output` |
| Citations | Human output cites exact origins inline by default. `--no-cite` uses stable IDs plus a source map instead. Structured output always retains origins |
| Budget | `--budget N` is a hard ceiling over the complete rendered UTF-8 byte stream under the named `utf8-byte-v1` estimator. Framing, escaping, citations, ANSI bytes, the manifest, omission records, and the final newline all count |
| Omissions | Units and relations are atomic. Every omitted unit and relation is identified concretely in the selection report; an impossible required envelope produces no partial stdout |
| Machine manifest | JSON and JSONL record input source/kind/tier/byte count/SHA-256, acquisition and extraction timings, AutoTLDR and representation versions, model and role backend, estimator and ID schemes, and selection accounting |
| Exit status | `0` success; `1` runtime or extraction error; `2` invalid CLI usage; `3` unsupported format or tier; `4` input not found; `5` budget cannot be satisfied |

`Unit.tokens` does not enforce `--budget`. It is a cheap `char4-floor-v1` diagnostic for
inspection and ranking; only the final `utf8-byte-v1` rendered-stream measurement decides
whether output fits.

### Mode 2 — Watch

```bash
autotldr watch ./inbox --once             # deterministic foreground pass
autotldr watch ./inbox --status           # persisted file/folder state
autotldr watch ./inbox --recursive --debounce 10
```

A foreground polling service monitors one folder. Drop a file in and an artifact appears
under `.autotldr/files/`; the fused `.autotldr/FOLDER.tldr.md` roll-up is rebuilt after a
debounce. SQLite/WAL stores status, SHA-256 suppresses unchanged content, writes are atomic,
and one failing file is contained and reported without stalling siblings.

**This is what the "auto" means.** The invoke mode is a tool. The watch mode is a service
that turns a directory into a self-summarizing corpus, and it is the differentiated half.

### The v1 promise, in one sentence

> Point it at a folder of mixed text documents and it tells you what the collection is,
> how the pieces relate, and where every claim came from.

---

## Part 2: Input scope

Everything here is text-derivable and needs no vision or speech model.

| Tier | Formats in v1 |
| --- | --- |
| 0 | txt, markdown, rST, the published source-language inventory below, JSON, JSONL, YAML, TOML, XML, CSV, TSV |
| 1 | PDF with a text layer, DOCX, HTML and URLs, Jupyter notebooks, LaTeX, EPUB |
| 2 | Directory trees, git repos, archives, doc sites |
| 3 | XLSX, Parquet, SQLite, DuckDB files, HDF5, NetCDF |

Explicitly out of v1: scanned PDF, PPTX rendering, images, audio, video, binaries. When
AutoTLDR meets one, it says so by name and moves on rather than failing the run.

---

## Part 3: Stack decisions

### 3.1 Language: Python, distributed with uv

**Decision:** Python 3.12+, packaged for `uvx autotldr` and `uv tool install`.

The extraction ecosystem this product depends on is Python: MarkItDown, Docling, openpyxl,
pypdf, pymupdf, pinned tree-sitter bindings, h5py, and trafilatura, alongside stdlib OOXML,
JSON, and HTML adapters where a smaller native parser preserves the required structure.
Rust would give a faster cold start and a single static binary, and would cost most of year
one reimplementing extractors that already exist and are maintained.

**The cost, named:** Python startup is the one thing that can make this feel unlike a Unix
tool. The mitigation is not optional.

- Every format extractor is lazy-imported. `autotldr notes.md` must never import Docling,
  h5py, or a tokenizer.
- The CLI entry path imports only standard-library argument, path, and stream primitives
  until the format router has decided what the input is.
- Budget: **under 120ms** to first output for a Tier 0 file. This is a tested contract,
  not an aspiration. If it regresses, that is a build failure.

**Revisit when:** the extractor set is stable and startup is still the complaint. A Rust
core calling Python extractors as subprocesses is the migration path, and the CLI contract
in Part 4 is designed so that swap is invisible to callers.

### 3.2 Store: SQLite as the durable store, DuckDB as a compute engine

This is the decision I want to be most explicit about, because it is the one that is
expensive to reverse and the one you asked about by name.

**Decision:**

| Component | Role |
| --- | --- |
| **SQLite** with WAL | The durable store: semantic units, relations, folder state, job queue, content-addressed cache |
| **FTS5** | Lexical index over unit content, BM25 out of the box |
| **sqlite-vec** | Vector index over unit embeddings, in the same file |
| **Recursive CTEs** | The relation graph. Unit-to-unit, formula dependencies, import edges |
| **DuckDB** | Invoked transiently to profile a data file. Never a persistent store |

One durable file per project (`.autotldr/store.db`). Opens in microseconds. Concurrent
readers alongside the watch daemon's writer, which is exactly what WAL mode is for.

DuckDB earns its place for a specific reason: it reads CSV, Parquet, JSON, and Excel
**directly off disk with zero ingestion**. Tier 3 profiling becomes
`SELECT * FROM read_xlsx(...)` rather than a parse-and-load pipeline. It is spun up, asked
a question, and closed. Its single-writer limitation never bites because it is never the
store.

### 3.3 The SurrealDB verdict

You raised it specifically, so here is the real assessment rather than a dismissal.

**What is genuinely attractive.** SurrealDB 3.0 went GA in February 2026 with $44M raised
and one engine covering document, graph, relational, full-text, and vector through a single
query language and a single transaction. For a system whose representation is explicitly a
graph of typed units with both lexical and vector retrieval, that is the shape of the
problem. Its live-query feature is a natural fit for the watch daemon. Architecturally it is
the most elegant answer on the table.

**Why not for v1.**

1. **No zero-ingestion file reading.** Tiers 0 through 3 are mostly *files*, and DuckDB
   querying an XLSX in place is a structural advantage SurrealDB has no answer to. That
   advantage is worth more to this product than multi-model unification is.
2. **Vector indexing is still maturing.** The 3.0 indexing work is described by SurrealDB
   as a foundation for future specialized indexes, vector among them. Building the
   retrieval core on a component that is explicitly on the roadmap is avoidable risk.
3. **No independent embedded-mode benchmarks exist.** Nearly all published technical
   detail traces back to SurrealDB's own material, and the funding is openly earmarked for
   hardening reliability and performance. That is a candid signal, and it is the wrong
   place for a foundational bet in month one.
4. **Startup cost is unforgiving here.** A CLI that must answer in 120ms cannot afford an
   engine designed to also run distributed clusters.

**When to revisit, concretely.** If the watch daemon grows into a persistent multi-folder
service with live subscriptions and cross-project queries, SurrealDB's live queries and
unified model become a real advantage over hand-rolling that on SQLite. The storage layer
in Part 4 is kept behind one interface so this stays a swap rather than a rewrite. Revisit
at the point where the daemon, not the CLI, is the primary entry point.

**What I ruled out and why.** LanceDB is excellent at vectors and solves a problem we do not
have yet at v1 scale. Kùzu is the right call if graph traversal becomes the bottleneck, and
recursive CTEs over a few thousand units will not be that. Postgres with pgvector requires a
server, which is disqualifying for a Unix tool. Chroma and Qdrant are servers for the same
reason.

### 3.4 Extraction: a router, not a converter

The consensus in the current tooling literature is that no single extractor wins across
formats, and that accuracy gaps above 20% between parsers on complex layouts are normal.
So AutoTLDR routes.

| Input | Extractor | Why this one |
| --- | --- | --- |
| Markdown, txt, rST | Native parse | Structure is already there |
| Source code, restricted inventory | Python stdlib AST; lazy pinned native tree-sitter pack for the published reliable languages | Exact declarations and imports; every unconfigured or unreliable language routes to a named Tier 0 decline and is not counted as supported |
| JSON, YAML, TOML, XML | Structural induction over samples | Schema is the semantics |
| CSV, TSV | Native bounded stdlib profiling | Streaming column schema/statistics, never rows |
| Parquet | Lazy PyArrow metadata/statistics profiling | Schema and row-group statistics, never rows |
| **XLSX** | **openpyxl on the formula layer** | **Never through a markdown converter. The formula graph is the meaning and conversion destroys it** |
| DOCX | Native OOXML ZIP/XML with the stdlib | Native paragraphs, tables, comments, and tracked revisions without `python-docx` |
| PDF, simple | pymupdf text layer | Fast path, most PDFs |
| PDF, table-heavy | Docling, future opt-in via `--deep` | Accurate and slow. Earn the wait |
| HTML, URL | Native stdlib HTML parser with bounded HTTP(S) acquisition | Addressable document structure on the Stage 3 fast path |
| Notebooks | Native JSON with the stdlib | Cells, outputs, and their pairing without `nbformat` |
| HDF5, NetCDF | h5py, netCDF4 | Structure and attributes, never values |

“Source code” in Tier 0 is deliberately not an open-ended format claim. The published
reliable inventory is Python via the stdlib AST, then JS/JSX, TS/TSX, C/C++, Java, Rust,
Go, Ruby, PHP, Kotlin, C#, Bash, SQL, Scala, Perl (with parser evidence), R, Elixir,
Haskell, and Objective-C (with parser evidence) through the pinned native tree-sitter pack.
For Perl and Objective-C, “with evidence” means an error-free native declaration or import
node grounds the emitted unit; a parse without that evidence is declined rather than
treated as empty success.

Named Tier 0 declines include Lua, Swift, Zsh, Fish, Dart, Clojure/ClojureScript, F#, VB,
Groovy, Vue, Svelte, and Objective-C++. Any other unconfigured or unreliable language
follows the same named-decline path and is not part of the supported inventory.

Two routing rules govern v1:

1. **Native format beats conversion.** Routing an XLSX through a PDF-oriented pipeline
   throws away exactly the structure that carries the meaning. Same for DOCX comments and
   notebook outputs.
2. **The fast path is the default.** Stage 3 implements that path. A future `--deep`
   opts into the slow, accurate extractor; a user who has not asked to wait does not wait.

### 3.5 Models

| Job | v1 approach |
| --- | --- |
| Structure extraction | **No model.** Parsers, all tiers |
| Schema and statistics | **No model.** DuckDB and profiling |
| Formula graph | **No model.** openpyxl dependency walk |
| Role tagging | Measured backend routing: rules prove `assumption`; opt-in local adds `procedure`; configured frontier enrichment adds six named roles |
| Embeddings for fusion | Deferred. The first complete demo uses none; an embedding resident is allowed only after a separate eval proves semantic-link value |
| Folder-level synthesis | Implemented public seam: the ZBook-local LM Studio endpoint, using its OpenAI-compatible wire, returns strict claims over existing evidence IDs; AutoTLDR derives origins and rejects unsupported IDs/model substitutions |

**Stage 2 settled the honest question on role tagging.** The frozen diagnostic set contains
200 exact extractor units across five formats and 20 real source groups. A role passed only
with support >=15, at least three source groups, precision >=0.80, and recall >=0.70.
Aggregate accuracy was not a gate. The full per-role table, format slices, and confusion
matrices live in [`benchmarks/roles/report.md`](../benchmarks/roles/report.md); D-013 records
the product decision.

- Deterministic rules passed `assumption` only.
- The selected ZBook-local Ornith-1.5-35B-A3B arm passed `procedure` and correctly
  recognized the `unknown` fallback.
- The frontier ceiling passed `definition`, `procedure`, `caveat`, `example`, `decision`,
  and `limitation`.
- `claim`, `parameter`, and `result` passed no arm and are removed from the v1 taxonomy.

The live vocabulary is therefore seven named roles plus `unknown`, but reliability is
backend-scoped. The fast path emits a named role only for a structurally proven
`assumption`; every other unit stays `unknown` unless an enabled enrichment backend passed
that specific role. Model use belongs in the bundle manifest so a downstream consumer can
tell which guarantee applies. A model's parameter count is not an eligibility ceiling.
Local evaluation instead enforces one ZBook-local resident at a time, 100% GPU offload,
and sequential load/run/unload without touching LM Link peers.

The labels were independently reviewed and adjudicated by AI agents, with 91.5% reviewer
agreement and Cohen's kappa .906. That is sufficient for the v1 engineering gate, not a
substitute for a later human domain-expert audit.

Extraction and measured fusion across Tiers 0 through 3 remain useful with every model
turned off; that deterministic representation is the fallback and source of truth. The
first demo described as the AutoTLDR product nevertheless requires a grounded model-written
TLDR. On ZBook, evaluation loads one AutoTLDR-owned generation model at a time, requires
100% GPU offload, runs candidates sequentially, and unloads the owned model after the run.

---

## Part 4: The representation

### 4.1 Semantic unit

```
Unit
  id            full origin/modality/content digest, stable across runs
  source_id     which file this came from
  modality      prose | code | table | record | schema | equation | reference
  role          definition | procedure | caveat | example | decision
                | assumption | limitation | unknown
  content       the text or structured payload
  origin        addressable back-pointer, see below
  structure     path in the source's own hierarchy
  salience      0..1, why this survived selection
  confidence    0..1, how sure the extractor is
  tokens        cheap diagnostic estimate; never --budget enforcement
```

`Unit.tokens` currently names the `char4-floor-v1` diagnostic estimate. Exact CLI budget
enforcement instead measures the complete rendered UTF-8 output with `utf8-byte-v1`, after
formatting, citations, manifests, omission accounting, and the final newline are present.

`origin` is the invariant that makes the whole thing trustworthy, and it is format-specific
by design:

| Source | origin |
| --- | --- |
| Markdown, txt | `line:120-134` |
| PDF | `page:7#span:3` |
| Source code | `src/auth.py:88` |
| XLSX | `Sheet2!C14` |
| DOCX | `para:44` |
| Notebook | `cell:12#output:0` |
| HDF5 | `/run3/pressure` |
| URL | `https://…#section-id` |

### 4.2 Relations

```
Relation
  from_unit, to_unit
  kind        supports | contradicts | implements | derives-from
              | exemplifies | describes | produced-by | references
              | corresponds
  evidence    what grounds this link
  confidence  0..1
```

Relations within a file come from structure. Relations *across* files are the fusion
problem, and they are Part 5.

### 4.3 Bundle

What a run emits.

```
Bundle
  subject       what was pointed at
  summary       Stage 3 structural statement; Stage 4 measured fusion findings;
                Stage 5 grounded semantic synthesis
  summary_claims addressable statements with evidence unit IDs and derived origins
  units         selected, budgeted, ordered
  relations     the graph among them
  gaps          what the sources never documented
  manifest      inputs, hashes, timings, versions, backend and selection accounting
```

`gaps` is a first-class field, not an afterthought. "No file in this folder documents why
the threshold is 0.7" is a finding, and emitting it is the second invariant from the matrix.

---

## Part 5: Fusion, the actual wedge

**Stage 4 is implemented and measured.** Its core accepts already extracted sources, and
the CLI exposes it for two or more explicitly named inputs. It deliberately does not acquire
a directory or write the model-generated semantic collection statement; those are the
integrated Stage 5 gate.

Per-file summarization is a commodity. **Fusing a folder into one representation is what
nobody does**, and it is the reason this scope was chosen. Concretely, for a folder holding
a paper, a spreadsheet of results, and the code that produced them:

### 5.1 Four linking signals, cheapest first

1. **Literal reference.** A filename, path, URL, DOI, or citation key in one file naming
   another. Free, exact, high precision. Catches most real links.
2. **Identifier co-occurrence.** A column header `tput_mbps`, a symbol
   `measure_throughput()`, and a table caption "Throughput (Mb/s)". Normalized token
   matching over identifiers. Cheap, needs no model.
3. **Structural correspondence.** A results table whose column count and row labels match a
   dataset's schema. A figure caption numbered to match a notebook cell's output. Cheap,
   surprisingly strong.
4. **Semantic similarity.** Embedding proximity between units across files was proposed as
   a fallback for concepts that share no tokens. It is not implemented or resident in the
   first complete demo. It may enter only after a separate frozen eval proves value beyond
   the first three signals.

Signals 1 through 3 run with no model. The Stage 4 scored run evaluated every output class
separately; aggregate accuracy was neither computed nor used. The production path follows
the frozen dispositions exactly:

| Signal or finding | Support | Precision | Recall | Production disposition |
| --- | ---: | ---: | ---: | --- |
| Literal reference | 23 | 1.000 | 0.957 | Ship complete |
| Identifier correspondence | 171 | 0.850 | 0.830 | Ship only preregistered `native-native` (support 63, P=.900, R=.857) |
| Structural correspondence | 10 | 1.000 | 0.800 | Ship complete |
| Strict scalar contradiction | 12 | 1.000 | 0.667 | Disabled; missed the frozen .70 recall gate |
| Orphan absence | 7 | 1.000 | 0.571 | Disabled; missed the frozen .90 recall gate |
| Unresolved reference | 9 | 0.900 | 1.000 | Ship only preregistered `local-path` (support 6, P=R=1.000) |

The raw diagnostic analyzer is retained for new-corpus evaluation. The user-facing fusion
path emits only the rows and subtypes marked to ship, records every disabled signal in the
manifest, and never converts “disabled” into a claim that no contradiction or orphan
exists. D-018 and `benchmarks/fusion/report.md` hold the frozen details.

### 5.2 What fusion emits

- **Grounded engineering statements.** Stage 4 reports the collection size, the measured
  emitted link classes, and measured local-path unresolved findings with evidence IDs.
- **A conservative relation graph.** Literal references, native/native identifier
  correspondences, and compatible structural correspondences that passed their gates.
- **Measured gaps.** Non-ambiguous local paths referenced but unresolved. Other proposed
  unresolved subtypes remain outside the shipping claim.
- **No production contradiction or orphan claim yet.** Both detectors remain visible to
  diagnostics but failed their frozen recall gates and are filtered from `fuse()`.

Stage 5 adds the actual concise statement of what the collection means. The model receives
only a bounded canonical evidence pack, returns strict structured claims containing existing
unit IDs, and cannot create origins, roles, relations, contradictions, or gaps. AutoTLDR
validates IDs, derives `GroundedStatement` origins itself, and applies the existing exact
complete-output budget. An invalid model response falls back deterministically or fails
explicitly; it never becomes uncited repaired prose.

---

## Part 6: The watch daemon

```
autotldr watch ./inbox [--out bundle|md|html] [--debounce 30s] [--recursive]
```

| Concern | Decision |
| --- | --- |
| FS events | Stdlib polling, deterministic on local and network filesystems |
| Re-trigger storms | Content-hash comparison. A re-save with identical bytes does nothing |
| Batch drops | Debounce window, default 30s. Twelve files dropped produce one fusion |
| Where artifacts land | `.autotldr/` beside the folder by default, never scattered next to sources unless asked |
| Partial writes | Wait for size and mtime to settle before reading |
| Failure | Logged to the store and surfaced by `--status`. One bad file never stalls the folder |
| Daemon required? | **No.** Invoke mode never needs the daemon running |
| Contention | Daemon writes, CLI reads, WAL handles it |

The last two lines matter most. **The CLI must work with the daemon stopped, uninstalled,
or never started.** A tool that requires a background service to answer a question is not a
Unix tool.

---

## Part 7: Build order

Corrected from an earlier draft that put the role eval first. That was a dependency
inversion: you cannot evaluate role tagging until extractors exist to produce spans to
label. Extraction comes first, the eval second, and the eval gates everything that depends
on roles being reliable.

| Stage | Scope | Gate |
| --- | --- | --- |
| **1** | Representation spike. Three dissimilar inputs: a PDF paper, an XLSX model, a folder of markdown. Extraction and units only, no roles, no renderers | **Complete.** One addressable representation holds all three |
| **2** | Role-tagging eval. 200 exact units, five formats, three arms: rules-only, selected local, frontier ceiling | **Complete.** Seven named roles survive with backend-scoped guarantees; three were removed (D-013) |
| **3** | Invoke mode, Tiers 0 and 1. Path/stdin/HTTP(S); `ansi`, `md`, `json`, `jsonl`; citations, exact rendered-byte budget, omission inventory, manifests, named exits; startup contract enforced | **Complete.** It behaves as a bounded, addressable Unix pipeline |
| **4** | Model-free fusion over two or more explicit sources; separate evaluation of literal, identifier, structural, contradiction, orphan, and unresolved outputs | **Complete.** Only the signal/subtype surfaces that passed are emitted (D-018) |
| **5** | Complete-demo integration: Tier 2 directory/repo/archive/doc-site acquisition; all locked Tier 3 adapters; bounded evidence packing and grounded local-model TLDR synthesis | **Complete software/functional slice.** On 2026-08-31 Borealis ran 14 mixed inputs through one ZBook-local instance and accepted three cited claims with no fallback; 63 independent checks over the saved artifacts passed. Physical no-spill certification remains a separately named telemetry limitation and the certification wrapper fails closed |
| **6** | Watch daemon | **Complete.** Polling, debounce, SHA suppression, SQLite/WAL, atomic per-file and folder artifacts |
| **7** | `html` and `pdf` output, claim-to-source linking | **Complete.** Self-contained HTML and paginated PDF share the exact omission policy. Text shapes are byte-identical across processes; PDF byte identity holds within one process, because `pymupdf.Story` lays identical HTML out differently per process (D-027) |
| **8** | MCP with the Tasks extension, `SKILL.md`, A2A card | **Complete.** Thin local/model-off wrappers over the public API |

All eight stages have a thin working vertical slice. The measured Stage 4 engineering
statements remain the deterministic source of truth; accepted Stage 5 model claims are
separately manifested, and model failure never silently becomes uncited prose.

---

## Part 8: Decisions locked

**The watch daemon ships in v1.** Seven weeks to first release rather than five. The extra
work does not overlap with anything else on the critical path, and a release without it is
a worse version of tools that already exist. "Auto" is in the name and it has to mean
something.

**A watched folder produces a per-file artifact plus a folder roll-up.** Each dropped file
gets its own artifact, and `FOLDER.tldr.md` holds the fused collection view, rewritten on
debounce. The per-file artifacts are nearly free because they are already computed on the
way to fusion. Versioned history is deferred; it is the most interesting option and roughly
double the work, and it can be added later without changing the on-disk contract.
