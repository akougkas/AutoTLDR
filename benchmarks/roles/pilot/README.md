# Role-screening pilot

This is a label-blind, **unscored** pilot for choosing a local model and checking
strict response-JSON compliance. It is not part of the frozen 200-unit Stage 2
corpus and must never be merged into its scores.

The pilot has 33 exact production-extractor units: three for every value in the
11-role taxonomy. It uses five fresh source groups, one in each of `md`, `rst`,
`txt`, `pdf`, and `xlsx`; none occurs among the 20 scored source groups.

## Trust boundary

`items.jsonl` is the only model-side corpus file. Each row contains an `id` for
joining the response plus exactly the production request fields:

```text
format, modality, content, structure, evidence
```

The runner must omit `id` from the actual model message, matching the scored
benchmark's five-field whitelist. It must not open or join any of these files
until after candidate selection is final:

- `labels.jsonl` — gold role keyed by id
- `annotations.jsonl` — AI-agent annotation rationale
- `origins.jsonl` — source identity, exact origin, frozen rule role and commit
- `sources.jsonl` — immutable provenance, license, and SHA-256

The pilot is for candidate selection and JSON compliance only. Do not report its
accuracy as Stage 2 evidence, tune the frozen prompt against it after inspecting
predictions, or substitute its items into the scored corpus.

## Rebuild and verify

```bash
uv run python .agent/scratch/roles/pilot/build_pilot.py
```

The builder verifies source digests, disjoint source groups, exact production
unit resolution, unique addressable origins, all five formats, exactly three
items per role, JSON round trips, and the model-visible field boundary. It does
not read or write a prediction file. Its machine-readable result is
`validation.json`.

The prose contents are never resegmented or rewritten. Structural evidence is
copied from extractor metadata where available; `literal_block` is recorded as
source-level structural evidence for the RST and RFC examples, consistent with
the scored corpus convention.
