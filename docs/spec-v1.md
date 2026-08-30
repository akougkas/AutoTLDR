# AutoTLDR v1 — Technical Specification

**Scope locked:** the mixed folder, text-only, Tiers 0 through 3. No images, no audio, no
video. Point AutoTLDR at a file or a folder and get back one fused context bundle.

This document makes the stack decisions and the representation concrete. It supersedes
nothing in [`matrix.md`](matrix.md); the matrix is the menu, this is what we cook
first.

---

## Part 1: What v1 is

Two execution modes over one engine.

### Mode 1 — Invoke

```bash
autotldr report.pdf                       # one file
autotldr ./project                        # one folder, fused
autotldr ./project --ask "what did we measure"
autotldr https://docs.example.com/guide   # one URL
```

Synchronous. Feels like `cat` for Tier 0, a couple of seconds for Tier 1–3. Writes to
stdout by default.

### Mode 2 — Watch

```bash
autotldr watch ./inbox --out bundle       # register a smart folder
autotldr watch --status                   # what is registered, what is stale
autotldr watch --stop ./inbox
```

A background daemon monitors registered folders. Drop a file in, and an artifact appears
beside it without anyone asking. Re-fusion of the folder-level bundle happens on a debounce
so that dropping twelve files produces one rebuild, not twelve.

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
| 0 | txt, markdown, rST, source code, JSON, JSONL, YAML, TOML, XML, CSV, TSV |
| 1 | PDF with a text layer, DOCX, HTML and URLs, Jupyter notebooks, LaTeX, EPUB |
| 2 | Directory trees, git repos, archives, doc sites |
| 3 | XLSX, Parquet, SQLite, DuckDB files, HDF5, NetCDF |

Explicitly out of v1: scanned PDF, PPTX rendering, images, audio, video, binaries. When
AutoTLDR meets one, it says so by name and moves on rather than failing the run.

---

## Part 3: Stack decisions

### 3.1 Language: Python, distributed with uv

**Decision:** Python 3.12+, packaged for `uvx autotldr` and `uv tool install`.

The entire extraction ecosystem this product depends on is Python: MarkItDown, Docling,
openpyxl, pypdf, pymupdf, python-docx, nbformat, tree-sitter bindings, h5py, trafilatura.
Rust would give a faster cold start and a single static binary, and would cost most of
year one reimplementing extractors that already exist and are maintained.

**The cost, named:** Python startup is the one thing that can make this feel unlike a Unix
tool. The mitigation is not optional.

- Every format extractor is lazy-imported. `autotldr notes.md` must never import Docling,
  h5py, or a tokenizer.
- The CLI entry path imports argparse, pathlib, and sqlite3 and nothing else until the
  format router has decided what the input is.
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
| Source code | tree-sitter | Symbols and signatures, not text |
| JSON, YAML, TOML, XML | Structural induction over samples | Schema is the semantics |
| CSV, Parquet | DuckDB profiling | Zero-copy, statistics not rows |
| **XLSX** | **openpyxl on the formula layer** | **Never through a markdown converter. The formula graph is the meaning and conversion destroys it** |
| DOCX | python-docx | Native structure, tracked changes, comments |
| PDF, simple | pymupdf text layer | Fast path, most PDFs |
| PDF, table-heavy | Docling, opt-in via `--deep` | Accurate and slow. Earn the wait |
| HTML, URL | llms.txt probe, then trafilatura | Check for a served summary before crawling |
| Notebooks | nbformat | Cells, outputs, and their pairing |
| HDF5, NetCDF | h5py, netCDF4 | Structure and attributes, never values |

Two rules the router enforces:

1. **Native format beats conversion.** Routing an XLSX through a PDF-oriented pipeline
   throws away exactly the structure that carries the meaning. Same for DOCX comments and
   notebook outputs.
2. **The fast path is the default.** `--deep` opts into the slow, accurate extractor. A
   user who has not asked to wait does not wait.

### 3.5 Models

| Job | v1 approach |
| --- | --- |
| Structure extraction | **No model.** Parsers, all tiers |
| Schema and statistics | **No model.** DuckDB and profiling |
| Formula graph | **No model.** openpyxl dependency walk |
| Role tagging | Rules and heuristics first, small local model where rules fail |
| Embeddings for fusion | Local Qwen3-Embedding, small variant, ONNX |
| Folder-level synthesis | Local 3–4B, or a configured OpenAI-compatible endpoint |

**The honest position on role tagging.** Distinguishing a claim from a caveat from an
assumption is the load-bearing capability of the whole representation, and a 3B model may
simply not be reliable at it. Current guidance puts 1.5B–3B as the floor for extraction
work and Qwen3.5 4B as the CPU sweet spot, but that is general guidance, not evidence about
this task.

So: **role tagging gets an eval before it gets an implementation.** Two hundred hand-labeled
spans across five formats, scored against rules-only, a 4B local model, and a frontier model
as ceiling. If rules-only lands close to the 4B model, v1 ships with rules and stays fully
offline and instant. If neither approaches the ceiling, the role taxonomy needs to shrink to
whatever *is* reliably extractable, and the product claim shrinks with it. Finding that out
in week two is cheap.

Nothing in Tiers 0 through 3 requires a model to produce a *useful* result. That is the
reason this scope was the right pick: v1 degrades to a genuinely valuable tool even if every
model in it is turned off.

---

## Part 4: The representation

### 4.1 Semantic unit

```
Unit
  id            content hash, stable across runs
  source_id     which file this came from
  modality      prose | code | table | record | schema | equation | reference
  role          claim | definition | procedure | parameter | caveat | result
                | example | decision | assumption | limitation | unknown
  content       the text or structured payload
  origin        addressable back-pointer, see below
  structure     path in the source's own hierarchy
  salience      0..1, why this survived selection
  confidence    0..1, how sure the extractor is
  tokens        cost to include
```

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
  summary       what this collection is, in three sentences
  units         selected, budgeted, ordered
  relations     the graph among them
  gaps          what the sources never documented
  manifest      inputs, hashes, versions, timings, model use
```

`gaps` is a first-class field, not an afterthought. "No file in this folder documents why
the threshold is 0.7" is a finding, and emitting it is the second invariant from the matrix.

---

## Part 5: Fusion, the actual wedge

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
4. **Semantic similarity.** Embedding proximity between units across files, as the fallback
   for concepts that share no tokens. The only signal that costs a model, and the only one
   that produces false positives, so it carries the lowest confidence and is always
   reported as inferred.

Signals 1 through 3 run with no model at all. If the eval shows they carry most of the
value, v1 fusion is fully offline and deterministic, which would be a real product
advantage.

### 5.2 What fusion emits

- **A collection statement.** What this folder is for, grounded in its contents.
- **A relation graph.** Which file produced which, which claim rests on which data, which
  code implements which method.
- **Contradictions.** The paper says n=40, the CSV has 38 rows. This is the single most
  valuable output the tool can produce and it falls directly out of having both in one
  representation.
- **Orphans.** Files nothing else references. Often the stale draft nobody deleted.
- **Gaps.** Concepts referenced but never defined anywhere in the collection.

Contradiction detection is the demo. It is also the thing that requires no clever
summarization, only a shared representation and a comparison.

---

## Part 6: The watch daemon

```
autotldr watch ./inbox [--out bundle|md|html] [--debounce 30s] [--recursive]
```

| Concern | Decision |
| --- | --- |
| FS events | `watchdog`, cross-platform, with a polling fallback for network mounts |
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
| **1** | Representation spike. Three dissimilar inputs: a PDF paper, an XLSX model, a folder of markdown. Extraction and units only, no roles, no renderers | One representation holds all three, or it gets redesigned now |
| **2** | Role-tagging eval. 200 spans labeled from Stage 1 output, five formats, three arms: rules-only, 4B local, frontier ceiling | Decides whether the role taxonomy survives, and how much of it |
| **3** | Invoke mode, Tiers 0 and 1. `ansi`, `md`, `json`, `jsonl`. Budget, cite, exit codes. Startup contract enforced | It feels like a Unix tool |
| **4** | Fusion. Signals 1 through 3, collection statement, contradictions, orphans, gaps | The wedge works on a real folder |
| **5** | Tiers 2 and 3. Directory and repo, XLSX formula graph, data profiling | The underserved tier, and the strongest demo |
| **6** | Watch daemon | The "auto" is real |
| **7** | `html` and `pdf` output, claim-to-source linking | The output is shareable |
| **8** | MCP with the Tasks extension, `SKILL.md`, A2A card | Agents adopt it without bespoke work |

Stages 1 and 2 are roughly two weeks combined and they are the ones that decide whether the
rest is worth building. Every prior plan skipped both.

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
