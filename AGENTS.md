# AGENTS.md

Orientation for any agent working in this repository.

## What this is

AutoTLDR is a Unix tool that understands semantics. Point it at a file, a folder,
or a URL and it returns what the thing *means*, rendered into the shape the
caller asked for. `autotldr model.xlsx` returns the formula dependency graph, not
a grid of numbers.

Pre-alpha. Stage 1 of 8 is complete.

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
- **Cold start is under 120ms** for a tier 0 file, enforced as a test.
- **Native format beats conversion.** XLSX goes to `openpyxl` on the formula
  layer, never through a markdown converter. The formula graph is the meaning.
- **Decline by name.** An unsupported format reports which format it was and
  which tier owns it. Never return an empty success.
- **Extractors emit `Role.UNKNOWN`** rather than guessing. Role reliability is
  unmeasured until Stage 2.
- Local imports use relative paths within the package. Tests use `pytest`.

## Commands

```bash
uv venv && uv pip install -e ".[all]"
uv run pytest                      # 27 tests
uv run python -m autotldr.cli FILE # try it
```

Run the suite before committing.

## Session state

`.agent/` is gitignored working state for cross-session coordination. Read
`.agent/HANDOFF.md` on arrival; rewrite it, `state.md`, and append to `log/`
before you leave. Anything that will still be true in a month belongs in `docs/`
instead.
