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
autotldr report.pdf                          # read it, in the terminal
autotldr ./project                           # a whole folder, fused into one view
autotldr ./project --ask "what did we measure"
autotldr model.xlsx --out json               # the formula graph, not the cells
autotldr *.md --out jsonl | jq '.content'    # pipeline
```

It is a Unix tool. It reads a path, a URL, or stdin. It writes to stdout or a
file. It exits non-zero and says why. Coding agents get it for free, because
agents call bash.

> **Status: pre-alpha.** The representation is being validated against dissimilar
> formats before anything else is built. Nothing here is stable. See
> [`docs/spec-v1.md`](docs/spec-v1.md) for what is being built and in what order.

## What makes it different

**It extracts semantics, not bytes.** "Summarize" means something different for
every format, and treating them all as text is why generic retrieval fails on
most of them:

| You point it at | It gives you back |
| --- | --- |
| A spreadsheet | The formula dependency graph. Inputs, assumptions, derived values, circular references, and the number somebody hardcoded into a formula in row 400 |
| A dataset | Schema, units, distributions, gaps. Never the values |
| A paper | Claims, the evidence under them, the method, the limitations |
| A repo | Architecture, entry points, the hazards |
| A folder of all four | How they relate, and where they disagree |

**Every claim is addressable.** No output sentence exists without a pointer back
into its source: `page:7#span:3`, `Sheet2!C14`, `src/auth.py:88`, `line:120-134`.
A summary you can verify is a summary you can trust.

**Absence is a finding.** If a source documents no rationale, AutoTLDR says so
rather than inventing one. "No file in this folder documents why the threshold is
0.7" is useful output.

**The competition is the toolchain.** Repomix, crawl4ai, Context7, Docling and
Whisper are good at turning things into clean text, and AutoTLDR shells out to
them. The work that is left over after they have all run is the product.

## Install

Not published yet. From a checkout:

```bash
uv venv && uv pip install -e ".[all]"
```

The base install has zero dependencies. Each format's parser lives in an extra
and is lazy-imported at the point of use, so `autotldr notes.md` never pays for a
PDF parser it will not call. Cold start for a Tier 0 file is held under 120ms by
a test that fails the build if it regresses.

## Documentation

Read them in this order.

| Document | What it covers |
| --- | --- |
| [`docs/vision.md`](docs/vision.md) | The stable part. Product thesis, positioning, audience, the three invariants, and the non-goals |
| [`docs/matrix.md`](docs/matrix.md) | The full product matrix: 51 input formats across 7 tiers, 10 output shapes, distribution surfaces, and what "summarize" actually means for each format |
| [`docs/spec-v1.md`](docs/spec-v1.md) | v1 build spec: stack decisions and rationale, the representation, folder fusion, the watch daemon, and build order |
| [`docs/decisions.md`](docs/decisions.md) | Append-only decision log. What was chosen, why, what was rejected, and what would justify revisiting |

Six earlier planning documents are preserved untracked under `archive/`. They are
superseded and kept only as a record of what was tried.

## Status

Stage 1 of eight is complete: the representation is validated against three
deliberately dissimilar formats. Stage 2 is the role-tagging eval, which is the
largest open risk in the project. See
[`docs/spec-v1.md`](docs/spec-v1.md#part-7-build-order) for the full order.

```bash
uv run pytest          # 27 tests
```

## License

MIT. See [LICENSE](LICENSE).
