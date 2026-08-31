# AutoTLDR Product Matrix

Point it at anything. Get back what it means, in the shape you asked for.

This document began as the broad capability map that replaced the six prior plans and
the CLIO-framed analysis archived under `archive/`. It remains the product menu and
long-range roadmap; it is not the implementation contract.

> **Status and authority.** [`spec-v1.md`](spec-v1.md) defines the authoritative v1
> contract, and [`decisions.md`](decisions.md) supersedes both documents when a later
> decision is recorded. The complete thin Stage 1–8 MVP is implemented: locked Tier
> 0/1 extraction, bounded Tier 2 collections, the required Tier 3 adapters, measured
> fusion, strict grounded synthesis, watch, HTML/PDF, and local agent surfaces. The
> functional localhost inference proof is distinct from independent physical
> no-CPU-spill certification, which current telemetry cannot provide. The tier, output, and
> distribution tables below describe capability direction unless a current stage says
> otherwise.

---

## Part 0: The product

```
autotldr <source> [--ask "question"] [--budget N] [--out FORMAT] [--depth LEVEL]
```

One binary. Point it at a file, a folder, a URL, a dataset, a recording. It figures out
what the thing is, extracts what the thing *means*, and renders that into the shape the
caller needs. A human gets a page they can read. An agent gets a bundle it can cite. A
pipeline gets JSONL it can pipe.

### The positioning

`file` tells you what something is. `pandoc` converts format A to format B.
**AutoTLDR tells you what something means, and renders that meaning into any shape.**

It is a Unix tool. It reads from a path, a URL, or stdin. It writes to stdout or a file.
It exits non-zero when it fails and says why. It composes:

```bash
autotldr paper.pdf                            # human reads it, in the terminal
autotldr paper.pdf --out html > brief.html    # human shares it
autotldr ./repo --ask "how does auth work"    # task-conditioned
autotldr *.xlsx --out jsonl | jq '.units[]'   # pipeline
autotldr meeting.m4a --out md --brief         # one paragraph
```

Coding agents get this for free, because agents call bash. That is the entire
integration story for the largest agent audience. Root-scoped MCP exists for agents that
cannot shell out; A2A waits for a real client and server need.

### Who it is for, in order

1. **A developer or researcher at a terminal** who has a thing and needs to know what is
   in it. This is the primary user and every design decision serves them first.
2. **A coding agent shelling out**, which is the same interface with `--out json`.
3. **A non-shell agent** over root-scoped MCP, which is a thin surface over the same core.
4. **A pipeline**, which is JSONL and exit codes.

Design for (1) and the rest follow. Design for (3) first and you get a protocol nobody
uses, which is what the original 2025 plan did.

---

## Part 1: The pivot that makes this tractable

The prior plans treated crawl4ai, Repomix, Context7, gitingest, and DeepWiki as
competitors and spent pages on differentiation. That was the wrong frame and it made the
project unbuildable, because every one of those tools is better at its narrow job than a
new project will be in year one.

**They are input adapters.** AutoTLDR shells out to them, takes their output as raw
material, and does the part none of them do.

| Tool | What it is good at | Its role here |
| --- | --- | --- |
| Repomix | Fast, git-aware repo flattening with secret stripping | Repo adapter |
| gitingest | URL-swap remote repo fetch | Remote repo adapter |
| crawl4ai | Robust JS-aware site crawling | Web adapter |
| Context7 | Curated, version-resolved library docs | Docs adapter, when it has the library |
| MarkItDown / pandoc | Format conversion to markdown | Document adapter |
| Docling | PDF layout and table structure | PDF adapter |
| Whisper | Speech to text | Audio adapter |
| DuckDB | Instant query over tabular files | Data adapter |

None of them produce a **semantic representation**. They produce bytes in a friendlier
encoding. Repomix hands you 400k tokens of concatenated source. crawl4ai hands you clean
markdown of 200 pages. Whisper hands you a wall of transcript with no paragraph breaks.

The work that is left over after all of them have run is the entire product.

---

## Part 2: The architecture bet

The naive design for N input formats and M output formats is N×M converters. That is
the trap that kills format-universal tools.

**The design is N → 1 → M.** Every input normalizes into one intermediate
representation. Every output renders from it. Adding an input format costs one extractor
and zero renderers. Adding an output format costs one renderer and zero extractors.

```
  50 input formats          ONE representation           10 output shapes
  ────────────────          ──────────────────           ────────────────
  pdf, xlsx, repo,   ───▶   Semantic Units         ───▶  html, md, pdf,
  wav, h5, mp4, ...          + structure                  json, jsonl,
                             + provenance                 ansi, bundle
                             + relations
```

**The intermediate representation is the whole company.** Adapters are replaceable;
renderers are cosmetic. If the IR is right, AutoTLDR outlives every tool it wraps. If
the IR is wrong, nothing else matters.

### What a Semantic Unit has to carry

| Field | Why it exists |
| --- | --- |
| `content` | The text or structured payload |
| `modality` | prose, code, table, figure, equation, utterance, record, schema |
| `role` | Measured live v1 vocabulary: definition, procedure, caveat, example, decision, assumption, limitation, plus mandatory unknown |
| `origin` | Addressable back-pointer: `page:12`, `Sheet2!C4:C40`, `src/a.py:88`, `00:14:32`, `/group/dataset` |
| `structure` | Position in the source's own hierarchy: outline, tree, sheet, chapter, scene |
| `relations` | Supports, contradicts, implements, derives-from, exemplifies |
| `salience` | Why this survived selection |
| `confidence` | How sure the extractor is, per modality |
| `tokens` | Cost to include it |

Stage 2 measured this field rather than assuming it. Seven named tags survived:
`definition`, `procedure`, `caveat`, `example`, `decision`, `assumption`, and
`limitation`, plus the mandatory `unknown` fallback. `claim`, `parameter`, and `result`
passed no evaluated arm and are not live v1 roles. Reliability is backend-scoped:
deterministic rules may prove `assumption`; the selected local enrichment backend adds
`procedure`; a configured frontier backend may add the six roles it passed. Everything
else stays `unknown`, and the manifest records which backend ran.

### The invariant contract

Three rules that hold for every modality, and are the product's actual promise:

1. **Every claim is addressable.** No output sentence exists without an `origin` that
   points back into the source. A claim that cannot be grounded is dropped, not softened.
2. **Absence is reported.** If a source documents no rationale, the output says so
   instead of inventing it. "This spreadsheet has no documentation for its assumptions"
   is a finding.
3. **The budget is honored exactly.** `--budget 4000` returns at most 4000 named
   portable tokens and says exactly what it dropped and why. Stage 3 defines one
   `utf8-byte-v1` portable token as one byte of the complete canonical UTF-8 output.

---

## Part 3: Input surface

Fifty-odd formats, tiered by **extraction difficulty**, not by popularity. This is a
capability menu, not a statement that every row has an adapter. The authoritative
eight-stage build order is in Part 7. In these tables, “Local?” describes feasibility,
not current implementation status.

The column that matters is the second one. **"Summarize" means something different for
every modality**, and that is precisely why generic RAG fails on most of this list.

### Tier 0 — Text-shaped

Deterministic parsing. No model needed for structure. Instant, offline, exact.

| Format | What the TLDR actually is | Extraction | Local? | Hard? |
| --- | --- | --- | --- | --- |
| Plain text | Topic structure, claims, entities | Segmentation + role tagging | Yes | Low |
| Markdown, rST, AsciiDoc | Outline, definitions, procedures, code blocks | Native structure + role tagging | Yes | Low |
| Source code | API surface, invariants, side effects, entry points | tree-sitter symbols + signatures | Yes | Low |
| JSON, JSONL | **Inferred schema**, cardinality, enums, what varies vs what is constant | Structural induction over samples | Yes | Low |
| YAML, TOML, INI | Config surface, defaults, required vs optional | Schema induction + comment mining | Yes | Low |
| XML | Schema, namespaces, document type, repeated structures | Structural induction | Yes | Low |
| CSV, TSV | Column semantics, types, units, ranges, nulls, outliers | Profiling, never row content | Yes | Low |
| Log files | Event vocabulary, error clusters, timeline, anomalies | Template mining (Drain-style) | Yes | Med |
| Diff, patch | What changed semantically, not textually; blast radius | AST-diff over hunks | Yes | Med |
| SQL schema | Entities, relationships, cardinality, constraints | DDL parse to ER graph | Yes | Low |

### Tier 1 — Document-shaped

Structure is recoverable but layout introduces ambiguity.

| Format | What the TLDR actually is | Extraction | Local? | Hard? |
| --- | --- | --- | --- | --- |
| PDF, born-digital | Claim → evidence → method → limitation; figures with captions; equations | Layout parse + role tagging | Yes | Med |
| DOCX | Outline, tracked changes, comments, the argument | OOXML parse | Yes | Low |
| HTML page, URL | Main content minus chrome; the page's actual assertion | Boilerplate removal + role tagging | Yes | Med |
| EPUB | Chapter arc, definitions, the through-line | Native structure | Yes | Low |
| Jupyter notebook | Narrative + what was computed + what the outputs showed | Cell graph, code and outputs paired | Yes | Med |
| LaTeX source | Structure, theorems, equations, citations, unrendered truth | Macro-aware parse | Yes | Med |
| Email, mbox | Thread arc, decisions, open questions, who owes what | Thread reconstruction | Yes | Med |
| Man pages, `--help` | Flags, defaults, invocation grammar, common recipes | Structured parse | Yes | Low |

### Tier 2 — Collection-shaped

The unit is the corpus, not the file. Selection under budget is mandatory because the
whole thing never fits.

**Current status:** implemented for bounded directory/repository traversal, ZIP/TAR
archives, and bounded same-origin documentation crawling. Failed members remain named
gaps and do not erase successful siblings.

| Source | What the TLDR actually is | Extraction | Local? | Hard? |
| --- | --- | --- | --- | --- |
| Git repo, local | Architecture, entry points, the golden path, hazards | Repomix + tree-sitter + selection | Yes | Med |
| Git repo, remote | Same, without cloning cost | gitingest or shallow clone | Yes | Med |
| Directory tree | What this pile of files collectively is | Per-file dispatch + roll-up | Yes | Med |
| Documentation site | Concept map, task recipes, version-pinned API truth | crawl4ai or llms.txt when present | Yes | High |
| Archive (zip, tar) | Manifest semantics, what this bundle is for | Recursive dispatch | Yes | Med |
| Package (npm, PyPI, crate) | Public API at the **installed version**, breaking changes | Registry + lockfile resolution | Yes | Med |
| Issue or PR thread | The decision, the dissent, the resolution, unresolved | Thread arc + role tagging | Yes | Med |
| Chat export | Decisions, owners, open loops | Turn segmentation + role tagging | Yes | High |

### Tier 3 — Data-shaped

**You never summarize the values. You summarize the structure, the statistics, and the
relationships.** This tier is where the largest capability gap in the market sits.

**Current status:** the locked v1 rows are implemented: XLSX/XLSM formula graphs,
Parquet metadata/statistics, SQLite and DuckDB schemas, and HDF5/NetCDF structure and
attributes. Additional metadata-only science adapters do not broaden the locked v1 gate.

| Format | What the TLDR actually is | Extraction | Local? | Hard? |
| --- | --- | --- | --- | --- |
| XLSX | **The formula dependency graph.** Inputs, assumptions, derived cells, outputs, circularity, hardcoded overrides | Formula graph extraction + provenance | Yes | High |
| Parquet, Arrow | Schema, distributions, cardinality, partitioning, nulls | Profiling via DuckDB | Yes | Low |
| SQLite, DB dump | ER model, hot tables, orphans, actual vs declared constraints | Schema + sampling | Yes | Med |
| HDF5 | Group hierarchy, dataset shapes, units, coordinate systems, attribute provenance | Structural walk + attribute mining | Yes | Med |
| NetCDF | Variables, dimensions, CF conventions, coverage, gaps | CF-aware walk | Yes | Med |
| Zarr | Chunking, compression, access-pattern implications | Store walk | Yes | Med |
| FITS | Instrument, observation parameters, WCS | Header semantics | Yes | Med |
| GeoJSON, shapefile | Extent, geometry types, attribute schema, projection | Geo profiling | Yes | Med |
| Time series | Regimes, seasonality, breakpoints, gaps, anomalies | Statistical decomposition | Yes | High |

The spreadsheet row is the single most underserved item on this entire page. Nobody can
tell you what a forty-sheet financial or scientific model actually computes. The formula
graph is the semantics and it is fully recoverable without a model.

### Tier 4 — Visual

Needs a vision model. Slow, uncertain, and where confidence reporting becomes mandatory.

| Format | What the TLDR actually is | Extraction | Local? | Hard? |
| --- | --- | --- | --- | --- |
| Diagram, flowchart, architecture | **Nodes, edges, direction, labels.** A graph, not a caption | VLM to structured graph | Marginal | High |
| Chart, plot | The trend and the numbers behind it | VLM to data table | Marginal | High |
| Screenshot | What app, what state, what the user was doing | VLM + OCR | Yes | Med |
| Scanned PDF | Reading order, then Tier 1 | OCR + layout | Yes | High |
| PPTX, slides | The argument arc across slides, not bullet transcription | Native parse + per-slide role | Yes | Med |
| Photo | What is depicted, informational content only | VLM | Marginal | Med |
| Whiteboard photo | The diagram, recovered as a graph | OCR + VLM | No | V. High |
| UI mockup | Components, hierarchy, states, flow | VLM | Marginal | High |

### Tier 5 — Temporal

Long, expensive, and the reason async and background execution are not optional.

| Format | What the TLDR actually is | Extraction | Local? | Hard? |
| --- | --- | --- | --- | --- |
| Audio, meeting | **Decisions, owners, deadlines, open questions.** Not a transcript | ASR + diarization + role tagging | Yes | High |
| Audio, lecture or podcast | Argument structure, claims, references | ASR + segmentation | Yes | High |
| Video, talk | Argument plus what the slides showed | ASR + keyframe VLM | Marginal | V. High |
| Screencast, tutorial | The procedure performed, as steps | ASR + on-screen text + action detection | No | V. High |
| Screen recording | What was done and what changed | Frame diff + OCR | No | V. High |

### Tier 6 — Opaque

The honest tier. Partial answers, clearly labeled.

| Format | What the TLDR actually is | Extraction | Local? | Hard? |
| --- | --- | --- | --- | --- |
| Unknown binary | Format identification, structure inference, entropy map, strings | Magic bytes + heuristics | Yes | Med |
| Executable, library | Exported symbols, linked deps, build provenance | Symbol table walk | Yes | Med |
| Firmware, disk image | Partition and filesystem layout, embedded artifacts | Structural carve | Yes | High |
| Encrypted, protected | What it is and that it cannot be read | Refuse honestly, name the reason | Yes | Trivial |
| Proprietary format | Best-effort structure, explicit uncertainty | Heuristics + plugin hook | Varies | High |

---

## Part 4: Output surface

The same IR, rendered for whoever is asking.

**Current status:** core production output is `ansi`, `md`, self-contained `html`,
deterministic linked `pdf`, `json`, and `jsonl`. The remaining shapes are roadmap entries.

### Formats

| `--out` | Consumer | Shape | Notes |
| --- | --- | --- | --- |
| `ansi` (default) | Human, terminal | Compact deterministic sections, optional TTY color | Stage 3 writes a complete budgeted projection; live progressive folding is later work |
| `md` | Human editing, coding agent | Clean markdown with source anchors | The lingua franca |
| `html` | Human reading and sharing | Single self-contained file, navigable, links to source spans | The shareable artifact |
| `pdf` | Human archiving, citing | Paginated, page-numbered, references section | Via HTML render |
| `json` | Agent | Full bundle, schema'd, versioned | The complete IR projection |
| `jsonl` | Pipeline | One semantic unit per line | Pipes into `jq`, streamable |
| `bundle` | Agent, durable | Directory: `memory.md`, `reasoning.md`, `examples.md`, `manifest.json` | The three-dimensional shape |
| `xml` | Agent, prompt-embedded | CXML-style tagged blocks | For models that parse tags better |
| `mermaid` | Human, agent | Diagram of the structure found | Repo graph, formula graph, ER model |
| `quiet` | Script | Exit code plus one line | For test and CI use |

### Modifiers, orthogonal to format

| Flag | Effect |
| --- | --- |
| `--ask "<question>"` | Task-conditions the selection. The pack answers *this*, not everything |
| `--budget N` | Hard complete-output `utf8-byte-v1` ceiling. Concretely inventories every dropped unit and relation |
| `--brief` / `--depth 1..5` | One paragraph through exhaustive |
| `--dimensions memory,reasoning,examples` | Which slices to emit |
| `--cite` / `--no-cite` | Span citations on every claim, on by default |
| `--diff <prior>` | What changed since the last run against this source |
| `--focus <subpath>` | Narrow to a sheet, directory, chapter, or time range |
| `--confidence-floor F` | Drop units the extractor is unsure about |
| `--watch` | Re-run on source change |

### The presentation problem

The user-facing half of the product is not the extraction. It is that a person opens the
HTML and immediately knows the three things that matter, and can drill from any claim to
the exact page, cell, line, or timestamp it came from. **Every rendered claim is a link
back into the source.** That single property is what makes a summary trustworthy rather
than a thing you have to go verify by hand.

For a spreadsheet that means clicking an assumption and landing on `Sheet2!C14`. For a
meeting it means clicking a decision and hearing the fourteen seconds where it was made.

---

## Part 5: Distribution surface

| Surface | Who it serves | Cost | When |
| --- | --- | --- | --- |
| **CLI** | Humans, coding agents via bash, pipelines | Core | Day one. This is the product |
| **Library** (Python import) | Embedders, notebook users | Low | Day one, falls out of the CLI |
| **MCP server** | Non-shell agents. Root-scoped stdio plus the 2026-07-28 Tasks extension gives long collection jobs a durable handle instead of timing out | Low | Alpha, after the CLI is real |
| **A2A service and card** | Agent-to-agent ecosystems | Medium | Only with a real client need, server, and authorization design |
| **Agent Skill** (`SKILL.md`) | Claude Code, Codex, Cursor, ~40 tools reading the same file | Trivial | Alongside MCP. It is a wrapper that teaches an agent when to shell out |
| **Hosted API** | Users whose hardware cannot run the measured generation, vision, or speech workload locally | High | Only if demand is proven |
| Browser extension | Clip any page | Med | Later, if ever |
| GUI | Non-terminal users | High | Not this product |

The Agent Skill is worth being precise about. It carries *instructions*, not execution:
it tells an agent that AutoTLDR exists, when to reach for it, and what flags to pass.
The execution is the CLI. That pairing is why the bash-first design covers the agent
market almost for free.

---

## Part 6: The hard problems, stated honestly

**1. The IR is the bet and each new modality must validate it again.** Stage 1 already
proved that one representation can hold dissimilar PDF, XLSX, and folder-derived units.
That is evidence for the representation, not permission to flatten future visual,
temporal, or scientific formats into text and call them supported.

**2. Role extraction is measured, backend-scoped, and deliberately smaller than the
original menu.** Stage 2 retained seven named tags plus `unknown`; `claim`, `parameter`,
and `result` were removed. The remaining problem is not choosing a nominal model size,
but preserving each backend's measured guarantees and leaving unsupported units
`unknown`.

**3. Tier 4 and 5 break the latency contract.** `autotldr paper.pdf` should feel like
`cat`. A one-hour video cannot. Two different interaction models are required: the
synchronous one for Tiers 0–3, and a job with a durable handle for Tiers 4–6. Deciding
this at the CLI-design level rather than retrofitting it later is cheap now and
expensive in six months.

**4. Local inference is a measured workload, not a parameter-count product tier.**
Tiers 0–3 need no model for deterministic extraction, but D-019 requires a configured
ZBook-local generation model for the first complete grounded TLDR. OpenAI-compatible
names the localhost wire protocol, not a hosted endpoint.
There is no parameter-count eligibility ceiling: the selected model must pass the actual
synthesis task and fit the user's hardware. Grounded synthesis is implemented as a
strict opt-in seam over existing evidence IDs. Vision and speech workloads remain later
and hardware-dependent.

**5. Some semantics require domain knowledge.** A climate model spreadsheet and a
financial model spreadsheet have different meanings for the same formula graph. This
needs a plugin point for domain packs. It should exist as a seam from the beginning even
if it stays empty for a year.

**6. Quality will vary enormously by modality, and hiding that would be fatal.** A tool
that is excellent on markdown and mediocre on scanned PDFs, and says so per result, is
trustworthy. One that presents both with equal confidence is not.

---

## Part 7: Authoritative eight-stage build order

The stage sequence comes from `spec-v1.md` as superseded by D-018 through D-020. The broad
tier tables above do not reorder it.

| Stage | Scope | Current status / gate |
| --- | --- | --- |
| **1. Representation spike** | Dissimilar PDF, XLSX, and folder inputs; extraction and units before roles or renderers | **Complete.** One addressable representation held all three |
| **2. Role-tagging eval** | 200 exact units, five formats, rules/local/frontier arms, per-role gates | **Complete.** Seven named roles plus `unknown`; backend-scoped guarantees |
| **3. Invoke mode** | One Tier 0/1 source from path/stdin/HTTP(S); `ansi`, `md`, `json`, `jsonl`; citations, exact output budget, omissions, manifests, named exits | **Complete.** Bounded Unix pipeline |
| **4. Measured fusion** | Repeated explicitly named sources; model-free literal, identifier, and structural signals; grounded collection findings | **Complete.** D-018 ships literal and structural, native/native identifiers, and local-path unresolved gaps; failed contradiction/orphan signals remain disabled and named |
| **5. Integrated Tiers 0–3 plus grounded synthesis** | Direct directory/repository/archive/doc-site acquisition; required Tier 3 adapters; a separately budgeted evidence pack and strictly ID-grounded synthesis through ZBook-local LM Studio | **Complete software/functional slice.** Borealis is fully grounded: 14 mixed inputs, three accepted cited claims, no fallback, all six shapes from one synthesis. Physical no-spill attestation remains explicitly unproved by current telemetry |
| **6. Watch daemon** | Debounced watched-folder processing, stable artifacts, cache/store integration | **Complete.** Polling, SHA suppression, SQLite/WAL, atomic artifacts |
| **7. Shareable output** | Self-contained `html` and `pdf` with source linking | **Complete.** Exact budgets and omission inventory carry through both. PDF byte identity is a within-process contract (D-027) |
| **8. Agent surfaces** | Root-scoped MCP Tasks extension and installable `SKILL.md`; A2A deferred | **Complete alpha surface.** Same prose/detail policy as the CLI; no nonexistent endpoint advertised (D-031) |

---

## Part 8: Capability wedges

Everything above describes a category. Categories do not get adopted; specific wins do.
These are product angles inside the roadmap, not alternatives that change the settled
eight-stage build order. D-019 requires the first complete demo to integrate Tiers 0–3
and grounded synthesis rather than presenting any one wedge as the whole product.

Four candidate wedges:

1. **The spreadsheet nobody can read.** `autotldr model.xlsx` returns the formula
   dependency graph, the assumptions, the hardcoded overrides someone buried in row 400.
   Nothing on the market does this. Highest "holy shit" per unit of effort, and it needs
   no model at all.
2. **The meeting that becomes decisions.** `autotldr standup.m4a --brief` returns four
   decisions with owners and timestamps you can click. Crowded space, but everyone has
   the pain.
3. **The mixed folder.** A directory holding a paper, its dataset, and its code returns
   one bundle explaining how the three relate. Genuinely nobody does cross-modal, and it
   is the hardest to build.
4. **The version-pinned API answer.** `autotldr --ask "how do I stream responses"`
   against the version in your lockfile, not the docs site's latest. Narrow, deeply
   useful to the agent audience, and directly attacks a real bug source.

The spreadsheet remains the clearest individual demonstration: its structure is
recoverable without a model, the output is immediately visual, and XLSX already
validates the representation against a non-text-shaped source. It is now one required
part of the D-019 Stage 5 mixed-collection demo, not a substitute for Tier 2 acquisition,
the remaining Tier 3 adapters, or grounded synthesis.
