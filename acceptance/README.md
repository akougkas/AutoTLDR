# AutoTLDR first-user acceptance

This directory makes the alpha product gate reproducible without touching the frozen
Stage 4/5 scores or the Borealis hero. It exercises the four jobs in
`docs/product-alpha.md` against deliberately non-hero inputs.

Acceptance is not a benchmark and must not become a prompt-tuning set. A run may expose a
defect; fix the general product contract, add an independent regression, and retain the
failed report. Do not edit the corpus until the observed defect has been classified.

## Sources

| Job | Source | Why it is independent |
| --- | --- | --- |
| Mixed technical handoff | `benchmarks/fusion/scored/fixtures/greenhouse/` | Existing frozen fusion fixture; not used by synthesis policy selection |
| Formula workbook | `benchmarks/fusion/scored/fixtures/capacity/capacity.xlsx` | Existing frozen fusion fixture with a native formula graph |
| Research-data package | Generated `edge-telemetry/` package | Separate Markdown/XLSX/SQLite/Parquet/NetCDF package built below |
| Bounded agent context | The generated package rendered as JSON | Same meaning, independently constrained complete-output projection |

The generator writes binary formats to an explicit new directory. It refuses to overwrite
an existing path and records hashes in an adjacent `.manifest.json`, outside the acquired
source directory.

```bash
acceptance_root=$(mktemp -d /tmp/autotldr-acceptance.XXXXXX)
uv run python acceptance/build_edge_telemetry.py "$acceptance_root/edge-telemetry"
```

## Model-off preflight

Preflight acquisition without consuming a model response:

```bash
uv run autotldr \
  "$acceptance_root/edge-telemetry" \
  --model off --detail deep --out json --budget 262144 \
  -o "$acceptance_root/evidence.json"
```

The evidence must contain all five sources, Tier 3 native units, formula and schema
relationships, explicit gaps, and no raw Parquet/NetCDF arrays or SQLite rows.

## Live product run

Use only an already-active, explicitly configured AutoTLDR-owned LM Studio generation
model. This procedure does not authorize loading, unloading, downloading, or invoking a
linked/Dynamo model.

```bash
uv run autotldr doctor

uv run autotldr \
  benchmarks/fusion/scored/fixtures/greenhouse \
  --detail standard --out md --budget 65536 \
  -o "$acceptance_root/greenhouse-standard.md"

for detail in brief standard deep; do
  uv run autotldr \
    benchmarks/fusion/scored/fixtures/capacity/capacity.xlsx \
    --detail "$detail" --out json --budget 262144 \
    -o "$acceptance_root/capacity-$detail.json"
done

uv run autotldr \
  "$acceptance_root/edge-telemetry" \
  --detail deep --out md --budget 65536 \
  -o "$acceptance_root/edge-telemetry-deep.md"

uv run autotldr \
  "$acceptance_root/edge-telemetry" \
  --detail standard --out json --budget 98304 \
  -o "$acceptance_root/edge-telemetry-96k.json"
```

Review each result against `rubric.json`. A passing process exit is necessary but not
sufficient: a human must inspect entailment, usefulness, and detail progression. The
online validator proves response structure, known evidence IDs, derived origins, exact
budget accounting, and declared runtime facts; it does not independently prove that the
claim wording is entailed by every citation.

## Budget refusal check

The research package intentionally has a provenance-rich representation. Record, but do
not hard-code, the minimum valid envelope reported for a smaller budget:

```bash
uv run autotldr \
  "$acceptance_root/edge-telemetry" \
  --detail standard --out json --budget 65536 \
  -o "$acceptance_root/must-not-exist.json"
```

If the command cannot fit a complete addressable result, it must exit with the budget
status, write no partial artifact, and name the measured minimum. A future compact agent
projection may lower that floor only while preserving resolvable citations, gaps, exact
omissions, and a model-run audit.

## Results

Session reports and generated binaries belong under `.agent/acceptance/` and remain
gitignored. Durable product conclusions belong in `docs/decisions.md`; never commit a
private user artifact or source content from an interview.

## External-user gate

Engineering acceptance authorizes invited observation, not public release. Give each
participant the rendered private-alpha bundle guide and follow
`docs/first-user-validation.md`. Copy `session-template.json` for each of the exact five
participants, store no source content in those records, and run
`scripts/evaluate_alpha_sessions.py` once the cohort is complete. Do not relax a failed
criterion after seeing the results.
