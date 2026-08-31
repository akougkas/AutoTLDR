---
name: autotldr
description: Summarize local files, directories, or explicit mixed-format collections with AutoTLDR's native semantic extractors, exact origins, and hard output budgets. Use when Codex needs a grounded overview of documents, code, structured data, notebooks, spreadsheets, or dataset structure; needs claims it can trace to source locations; or must fit extracted meaning into a bounded context window.
---

# AutoTLDR

Use the installed `autotldr` CLI. Treat it as a local Unix tool: supply paths,
read stdout, and preserve its addressable origins and named gaps.

## Choose the output

- Use `json` for programmatic reasoning and complete structured provenance.
- Use `jsonl` for line-oriented pipelines.
- Use `md` for a concise human-readable handoff with inline citations.
- Always set `--budget`; it is an exact ceiling over the complete UTF-8 output,
  including citations, framing, manifests, and omission records.

## Run it

Summarize one source:

```bash
autotldr --out md --budget 65536 -- "report.pdf"
```

Summarize a directory or fuse several explicit sources:

```bash
autotldr --out md --budget 65536 -- "notes/"
autotldr --out json --budget 262144 -- "paper.pdf" "analysis.ipynb" "model.xlsx"
```

Quote every path. Put `--` before paths so a filename cannot become an option.

## Interpret the result

- Resolve claims through their `origin` or evidence IDs before repeating them.
- Keep `gaps`, declines, omissions, and manifest entries; absence is a finding.
- Do not infer a role from `unknown`, invent rationale, or summarize raw dataset
  values. AutoTLDR reports dataset structure and statistics instead.
- If the budget is too small for a valid addressable envelope, increase it or
  report the named budget failure; never truncate stdout yourself.
- If a format is declined, report its name and owning tier instead of treating
  empty output as success.

## Boundaries

- Work on local paths only unless the user explicitly asks to use the CLI's URL
  acquisition path.
- Do not pass `--model`. Direct invoke cannot attest the guarded ZBook lifecycle
  and intentionally refuses model selection.
- Do not call an LM Studio endpoint, load or unload a model, or replace
  AutoTLDR's extraction with generic text conversion.
