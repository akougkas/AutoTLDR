# AutoTLDR Product Matrix

Point it at anything. Get back what it means, in the shape you asked for.

This document replaces the six prior plans and the CLIO-framed analysis, both archived
under `archive/`. It is written from a clean slate: no assumed ecosystem, no incumbent
codebase, no obligation to any prior architecture.

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
autotldr *.xlsx --out jsonl | jq '.claims[]'  # pipeline
autotldr meeting.m4a --out md --brief         # one paragraph
```

Coding agents get this for free, because agents call bash. That is the entire
integration story for the largest agent audience. MCP and A2A exist for agents that
cannot.

### Who it is for, in order

1. **A developer or researcher at a terminal** who has a thing and needs to know what is
   in it. This is the primary user and every design decision serves them first.
2. **A coding agent shelling out**, which is the same interface with `--out json`.
3. **A non-shell agent** over MCP or A2A, which is a thin surface over the same core.
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
| `role` | **The differentiator.** claim, definition, procedure, parameter, caveat, result, example, decision, assumption, limitation |
| `origin` | Addressable back-pointer: `page:12`, `Sheet2!C4:C40`, `src/a.py:88`, `00:14:32`, `/group/dataset` |
| `structure` | Position in the source's own hierarchy: outline, tree, sheet, chapter, scene |
| `relations` | Supports, contradicts, implements, derives-from, exemplifies |
| `salience` | Why this survived selection |
| `confidence` | How sure the extractor is, per modality |
| `tokens` | Cost to include it |

The `role` field is the bet within the bet. A tool that knows a sentence is a *caveat*
rather than a *claim* can build an output no keyword-based tool can. Every downstream
feature depends on roles being extractable and reliable.

### The invariant contract

Three rules that hold for every modality, and are the product's actual promise:

1. **Every claim is addressable.** No output sentence exists without an `origin` that
   points back into the source. A claim that cannot be grounded is dropped, not softened.
2. **Absence is reported.** If a source documents no rationale, the output says so
   instead of inventing it. "This spreadsheet has no documentation for its assumptions"
   is a finding.
3. **The budget is honored exactly.** `--budget 4000` returns at most 4000 tokens and
   says what it dropped and why.

---

## Part 3: Input surface

Fifty-odd formats, tiered by **extraction difficulty**, not by popularity. The tiers are
the build order.

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

### Formats

| `--out` | Consumer | Shape | Notes |
| --- | --- | --- | --- |
| `ansi` (default) | Human, terminal | Progressive, syntax-aware, folded sections | Streams as it works. Never dumps a wall |
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
| `--budget N` | Hard token ceiling. Reports what was dropped |
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
| **MCP server** | Non-shell agents. Use the 2026-07-28 Tasks extension for Tier 4–6 jobs so a long build returns a durable handle instead of timing out | Low | After the CLI is real |
| **A2A agent card** | Agent-to-agent ecosystems, 150+ orgs on the LF spec | Low | After MCP |
| **Agent Skill** (`SKILL.md`) | Claude Code, Codex, Cursor, ~40 tools reading the same file | Trivial | Alongside MCP. It is a wrapper that teaches an agent when to shell out |
| **Hosted API** | Users who cannot run a 7B VLM or Whisper locally | High | Only if Tier 4–5 demand is proven |
| Browser extension | Clip any page | Med | Later, if ever |
| GUI | Non-terminal users | High | Not this product |

The Agent Skill is worth being precise about. It carries *instructions*, not execution:
it tells an agent that AutoTLDR exists, when to reach for it, and what flags to pass.
The execution is the CLI. That pairing is why the bash-first design covers the agent
market almost for free.

---

## Part 6: The hard problems, stated honestly

**1. The IR is the bet and it must be validated across dissimilar modalities early.**
Building ten text-shaped extractors teaches nothing, because they all fit any schema.
Build a PDF, a spreadsheet, and an audio file in the first month. If one representation
holds a claim from a paper, a formula dependency from a model, and a decision from a
meeting, the design is sound. If it does not, better to know in week four.

**2. Role extraction is unproven at this scale.** Distinguishing a claim from a caveat
from an assumption is easy for a strong model and unreliable for a 7B local one. The
whole `role` field, and everything built on it, depends on this working. It needs an
eval before it needs an implementation.

**3. Tier 4 and 5 break the latency contract.** `autotldr paper.pdf` should feel like
`cat`. A one-hour video cannot. Two different interaction models are required: the
synchronous one for Tiers 0–3, and a job with a durable handle for Tiers 4–6. Deciding
this at the CLI-design level rather than retrofitting it later is cheap now and
expensive in six months.

**4. Local inference is the cost wall, not a feature.** Tiers 0–3 need no model at all
for structure, and a small one for roles. Tiers 4–5 need a VLM and ASR that many users
cannot run. This is the natural free-versus-paid boundary, and it falls out of physics
rather than from an arbitrary paywall.

**5. Some semantics require domain knowledge.** A climate model spreadsheet and a
financial model spreadsheet have different meanings for the same formula graph. This
needs a plugin point for domain packs. It should exist as a seam from the beginning even
if it stays empty for a year.

**6. Quality will vary enormously by modality, and hiding that would be fatal.** A tool
that is excellent on markdown and mediocre on scanned PDFs, and says so per result, is
trustworthy. One that presents both with equal confidence is not.

---

## Part 7: Build order

Each stage is independently useful and shippable. Nothing here is throwaway.

| Stage | Scope | Proves | Weeks |
| --- | --- | --- | --- |
| **0. Spike the IR** | Three deliberately dissimilar inputs: a PDF paper, an XLSX model, a WAV meeting. Extraction only, no renderers. | The representation survives contact with dissimilar modalities. Kill or redesign here | 2 |
| **1. The Unix tool** | Tier 0 and 1 complete. `--out ansi/md/json/jsonl`. Budget and cite. Exit codes. | It feels like a real tool and someone uses it twice | 3 |
| **2. Make it shareable** | `--out html` and `pdf`. Claim-to-source linking. | The output is something people send to each other | 2 |
| **3. Collections** | Tier 2. Selection under budget. `--ask`. Adapters for Repomix, crawl4ai, gitingest. | Selection works when the corpus never fits | 3 |
| **4. Data** | Tier 3. Formula graph, schema profiling, scientific formats. | The most underserved tier, and the strongest demo | 3 |
| **5. Agents** | MCP with Tasks, `SKILL.md`, A2A card. `bundle` output. | Agents adopt it without a bespoke integration | 2 |
| **6. Heavy modalities** | Tiers 4 and 5. Async jobs, confidence reporting. | The vision, and the paid tier's justification | 6+ |

Stage 0 is the one that must not be skipped. Every prior plan skipped it and specified
the architecture instead.

---

## Part 8: The open question

Everything above describes a category. Categories do not get adopted; specific wins do.
The one decision I cannot make from the material is **which single capability makes
someone install this and tell a colleague**.

Four candidates, each of which changes the build order:

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

My read, stated once: **(1)**. It is the only one where the semantics are fully
recoverable without a model, the demo is immediate and visual, the pain is universal
among analysts and scientists, and no competitor is close. It also validates the IR
against the least text-shaped thing in Tier 0–3, which is exactly what Stage 0 needs.

That is a recommendation, not a decision.
