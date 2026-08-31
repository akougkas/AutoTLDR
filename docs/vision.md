# Vision

The stable part. What AutoTLDR is, who it is for, and what it refuses to become.
Capabilities move; this should not. When something here changes, it is a product
pivot and belongs in [`decisions.md`](decisions.md) with a date on it.

---

## The thesis

> `file` tells you what something is. `pandoc` converts format A into format B.
> **AutoTLDR tells you what something means, and renders that meaning into any
> shape.**

Every tool in this space stops at bytes in a friendlier encoding. Repomix hands
you 400k tokens of concatenated source. crawl4ai hands you clean markdown of two
hundred pages. Whisper hands you a wall of transcript with no paragraph breaks.
Each is good at its job and none of them tell you what the thing *means*.

The work that is left over after all of them have run is the product.

## Product placement

**AutoTLDR is a Unix tool that happens to understand semantics.** It reads a
path, a URL, or stdin. It writes to stdout or a file. It exits non-zero and says
why. It composes with pipes.

That framing settles most design arguments before they start. A person at a
terminal is the primary user. Coding agents are served by the same interface for
free, because agents call bash. MCP and A2A are thin surfaces over the same core,
for agents that cannot shell out.

Design for the person and the rest follow. Design for the protocol first and you
get something nobody uses.

## Who it is for, in order

1. **A developer or researcher at a terminal** who has a thing and needs to know
   what is in it. Every design decision serves them first.
2. **A coding agent shelling out.** The same interface with `--out json`.
3. **A non-shell agent** over MCP or A2A. A wrapper, never a second
   implementation.
4. **A pipeline.** JSONL and exit codes.

## What makes it different

### Semantics, not bytes

"Summarize" means something different for every format, and treating them all as
text is why generic retrieval fails on most of them.

| Point it at | It gives back |
| --- | --- |
| A spreadsheet | The formula dependency graph. Inputs, assumptions, derived values, circular references, the number hardcoded into a formula in row 400 |
| A dataset | Schema, units, distributions, gaps. Never the values |
| A paper | Addressable units and structure; typed procedures, caveats, and limitations only when an evaluated enrichment backend supports them |
| A repo | Architecture, entry points, hazards |
| A meeting | Decisions, owners, and deadlines when the configured enrichment backend can recover them. Never an ungrounded rewrite of the transcript |
| A folder of several | How they relate, and where they disagree |

The per-modality semantic contract is the defensible part. It is documented in
[`matrix.md`](matrix.md), one row per format.

### The competition is the toolchain

Repomix, gitingest, crawl4ai, Context7, Docling, MarkItDown, and Whisper are
**input adapters**. AutoTLDR shells out to them and does the part none of them
do. This is not a rhetorical dodge; it is what makes the project buildable in a
year rather than five.

### One representation

N input formats and M output shapes is N×M converters if you get the
architecture wrong. Everything normalizes into one intermediate representation
and everything renders from it. A new input costs one extractor and zero
renderers. A new output costs one renderer and zero extractors.

**The representation is the whole company.** Adapters are replaceable and
renderers are cosmetic. If it is right, AutoTLDR outlives every tool it wraps.

### Role labels are measured claims

A unit is useful even when its role is `unknown`: it still has exact content,
structure, modality, relations, and an addressable origin. v1 does not pretend
that every semantic distinction can be tagged universally. Stage 2 retained
seven named roles (`definition`, `procedure`, `caveat`, `example`, `decision`,
`assumption`, and `limitation`) because at least one preregistered backend
recovered each; it removed `claim`, `parameter`, and `result` because none did.

Reliability is backend-scoped. The instant deterministic path proves
`assumption`; opt-in local enrichment additionally proves `procedure`; a
configured frontier-class endpoint can prove six named prose roles. Anything a
running backend did not demonstrate remains `unknown`. The product would rather
preserve an addressable untyped unit than attach a confident-looking label that
the evidence does not support.

## The three invariants

These hold for every modality and every output. They are the actual promise, and
breaking one is a bug regardless of how good the output looks.

1. **Every claim is addressable.** No output sentence exists without an origin
   pointing back into the source: `page:7#span:3`, `Sheet2!C14`,
   `src/auth.py:88`, `line:120-134`. A claim that cannot be grounded is dropped,
   not softened. A summary you can verify is a summary you can trust.

2. **Absence is reported.** If a source documents no rationale, the output says
   so instead of inventing one. "No file in this folder documents why the
   threshold is 0.7" is a finding, not a failure. Fabricating design rationale is
   the exact failure this product exists to prevent.

3. **The budget is honored exactly.** `--budget 4000` returns at most 4000 tokens
   and states what it dropped and why.

## Non-goals

Naming these is how the project stays finishable.

- **Not a session-context compactor.** The 2026 evidence is that with prompt
  caching, keeping conversation history often beats summarizing it. AutoTLDR
  operates on external corpora, not on an agent's own transcript.
- **Not a repo summarizer.** Repomix, gitingest, and DeepWiki own that. A static
  "what is this repo" bundle is a commodity.
- **Not a RAG framework.** It emits artifacts. What you do with them is yours.
- **Not a GUI.** A terminal tool with a shareable HTML output, not an app.
- **Not a chat interface.** It answers a question you pass as a flag and exits.
- **Not a hosted service, yet.** Local-first is the default and the free tier is
  the whole tool. A hosted API is justified only by modalities users genuinely
  cannot run locally, and only once demand for those is proven.

## What success looks like

Not stars, not installs, not demo views. Those measure attention.

**The metric is whether a pack beats the alternative.** An agent given an
AutoTLDR bundle should produce better output at fewer tokens than the same agent
given filesystem access and a grep. A person given the HTML should understand the
folder faster than opening the files. Both are measurable, and neither is assumed.

If it cannot beat grep, it should not exist. Finding that out early is worth more
than any feature on the roadmap.
