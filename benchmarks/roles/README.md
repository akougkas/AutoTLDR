# Stage 2 role-tagging benchmark

This benchmark decides which `Unit.role` values are recoverable at all. The
corpus is a deliberately stratified diagnostic set, not a balanced set and not
an estimate of production prevalence. Aggregate accuracy is therefore not a
gate. The durable result is the per-role precision/recall/F1 table and confusion
matrices for the frozen rules, one selected local model, and a frontier ceiling.

The scored corpus is frozen at 200 exact production-extractor units: 40 each
from Markdown, reStructuredText, plain text, PDF, and XLSX, drawn from 20 real
source groups. See [corpus.md](corpus.md) for its composition, hashes,
construction method, annotation agreement, and limitations.

## Result

Stage 2 closed by shrinking the taxonomy. At the frozen gate (support >=15,
source groups >=3, precision >=0.80, recall >=0.70), the passing roles were:

- rules: `assumption`;
- selected local Ornith: `procedure` plus correct recognition of the `unknown`
  fallback; and
- frontier ceiling: `definition`, `procedure`, `caveat`, `example`, `decision`,
  and `limitation`.

`claim`, `parameter`, and `result` passed no arm and were removed from the live
v1 `Role` enum. The remaining named roles are backend-scoped: a backend may emit
only roles it passed, and the deterministic production path otherwise emits
`unknown`. The generated [report.md](report.md) contains every per-role score,
format slice, arm-health count, and confusion matrix. D-013 records the durable
product decision and the AI-only annotation limitation.

## Files and trust boundaries

```text
benchmarks/roles/
  README.md              benchmark operation and interpretation
  corpus.md              frozen-corpus provenance and annotation methodology
  build_corpus.py        exact-unit corpus rebuilder and integrity checks
  evaluate.py            validator, rules/model runners, scorer, report renderer
  run_frontier_codex.py  isolated frontier runner
  prompt.md              frozen prompt shared verbatim by model arms
  policy.json            corpus contract, prompt settings, decision thresholds
  pilot_policy.json      local-model pilot candidates and frozen switch rule
  run_local_candidates.py
                         exclusive ZBook-local pilot lifecycle runner
  pilot/                 fresh, label-isolated model-selection corpus
  items.jsonl            units, origins, attribution, and frozen rules roles
  labels.jsonl           adjudicated labels, keyed by item id
  annotations.jsonl      selections, blind reviews, and adjudication notes
  sources.jsonl          pinned source provenance, licenses, and hashes
  predictions/           generated arm outputs and manifests
  report.json            generated machine-readable result
  report.md              generated per-role report
```

The model runner accepts an items path but no labels path. Although the item
record preserves provenance and the frozen rules result for auditability, an
endpoint request is rebuilt from this exact five-field whitelist:

```text
format, modality, content, structure, evidence
```

Item ids, source identity, origin, attribution, `rule_role`, annotation notes,
and gold labels never enter a model request. Gold labels are joined only by
`validate` and `report`. The optional `--pilot` mode accepts only an id plus the
same five input fields and rejects extra fields.

## Record schemas

Each `items.jsonl` row is an exact extracted unit with this general shape:

```json
{
  "id": "role-0001",
  "source_id": "source-001",
  "source_group": "canonical-document-family",
  "format": "pdf",
  "modality": "prose",
  "content": "The exact extracted Unit content.",
  "structure": ["Evaluation", "Limitations"],
  "evidence": {"heading": false, "caption": false},
  "origin": {"ref": "page:7#span:3", "char_span": null},
  "rule_role": "caveat",
  "rule_commit": "full-commit-hash",
  "attribution": {
    "title": "Document title",
    "uri": "https://example.invalid/document",
    "license": "redistribution or provenance statement"
  }
}
```

`rule_role` is the role emitted by the production extractor at the frozen rules
commit. It was captured independently of gold and must never be revised using
labels. All 200 items use one `rule_commit`.

Each `labels.jsonl` row is deliberately separate:

```json
{"id":"role-0001","role":"limitation"}
```

Review and adjudication records live in `annotations.jsonl`, never in items.
Each `sources.jsonl` row records `source_id`, `source_group`, format, title, URI,
license statement, and SHA-256 digest.

An item must remain an exact extracted `Unit`, not a manually resegmented
sentence. A mixed-role unit is a segmentation finding; it must not be silently
made easier for the model arms.

## Frozen corpus contract

Validation enforces:

- exactly 200 item/label pairs;
- exactly five formats with 40 items each;
- the exact per-role support declared in `policy.json`;
- at least three independent source groups for every gold role;
- unique ids and addressable `(source_id, origin.ref)` pairs;
- one frozen rules commit;
- complete source manifests with hashes and matching format/group metadata;
- no gold or annotation fields anywhere in an item; and
- the exact model-input whitelist in `policy.json`.

Gold `unknown` means that no named role applies with the available unit
evidence. It is not an annotator-confidence bucket. Unresolved disagreements
were adjudicated or replaced before any model-arm prediction was inspected.

The 200 scored units are a frozen zero-shot set. Prompt and client development
uses a separate pilot drawn from different source groups. Viewing scored-set
results and then tuning a prompt, rules, or model-selection policy contaminates
the evaluation.

## Build and validation

The source binaries and review working files are intentionally not vendored.
Given the pinned copies under `.agent/scratch/roles`, the builder verifies every
source hash, re-runs the production extractors, requires a unique exact origin
and content match, and regenerates the three corpus JSONL files:

```bash
uv run python benchmarks/roles/build_corpus.py
uv run python benchmarks/roles/evaluate.py validate
```

A clean checkout can audit the frozen records and fetch source copies from the
immutable URIs in `sources.jsonl`; reproducing the annotation assembly also
requires the untracked review records described in [corpus.md](corpus.md).

Export the frozen rules arm before endpoint calls:

```bash
uv run python benchmarks/roles/evaluate.py run-rules
```

## Model arms

The local runner targets an OpenAI-compatible `POST /v1/chat/completions`
endpoint. Pin the selected model explicitly; the benchmark does not prescribe a
parameter count or architecture class.

When LM Studio exposes linked hosts, a catalog row is not proof that a load is
local. `run_local_candidates.py` is the guarded path for a sequential pilot. It
derives the ZBook id from `lms link status --json`, temporarily makes that device
preferred for each estimate/load/unload, requires `GPU Offload: 100%`, verifies
the resident has `deviceIdentifier: null`, restores the original preference
before inference, and leaves at most one local LLM resident. It never acts on a
linked row. If a non-AutoTLDR model is already resident, its exact identifier
must be authorized with `--incumbent` so its settings can be snapshotted and
restored.

```bash
uv run python benchmarks/roles/run_local_candidates.py \
  --lms-executable /path/to/zbook/lms.exe \
  --candidate 'Candidate name=exact/local/catalog/path.gguf' \
  --incumbent EXACT_PREEXISTING_LOCAL_IDENTIFIER
```

Use `--dry-run` to inspect the invariant-bearing command plan without touching
LM Studio. The CLI also accepts the task-specific `AUTOTLDR_LMS_CLI` environment
variable instead of `--lms-executable`.

```bash
export AUTOTLDR_ROLE_LOCAL_BASE_URL=http://127.0.0.1:1234/v1
export AUTOTLDR_ROLE_LOCAL_MODEL=PINNED_LOCAL_MODEL_ID

uv run python benchmarks/roles/evaluate.py run-model --arm local
```

The isolated frontier adapter starts a fresh, tool-disabled Codex invocation
for every item and applies the same response schema and prompt:

```bash
uv run python benchmarks/roles/run_frontier_codex.py \
  --model PINNED_FRONTIER_MODEL_ID
```

An optional JSON configuration can supply endpoint metadata without secrets:

```json
{
  "arms": {
    "local": {
      "base_url": "http://127.0.0.1:1234/v1",
      "model": "PINNED_LOCAL_MODEL_ID"
    },
    "frontier": {
      "base_url": "https://api.example.invalid/v1",
      "model": "PINNED_FRONTIER_MODEL_ID",
      "api_key_env": "FRONTIER_API_KEY"
    }
  }
}
```

Pass it with `run-model --arm NAME --config PATH`. Direct CLI options override
configuration, which overrides environment fallbacks. API keys are read from
the environment and never written to a manifest.

Every scored item is sent once with temperature zero, seed `20260830`, and the
same prompt. A strict response is exactly one JSON object such as
`{"role":"result"}`. Invalid JSON, extra keys, prose, or an out-of-taxonomy
value is recorded and scored as `unknown`; it is never repaired with a retry.

Generate the report only after all three prediction files are frozen:

```bash
uv run python benchmarks/roles/evaluate.py report
```

## Outputs and interpretation

Each prediction file has a companion `.manifest.json` recording corpus, prompt,
policy, and output hashes; the exact model id and inference settings; endpoint
class; evaluator commit; and status counts. Credentials are excluded.

`report` emits:

- precision, recall, F1, support, and abstentions for every role and arm;
- a confusion matrix for every arm;
- the same role metrics sliced by format;
- invalid/error counts and non-`unknown` coverage; and
- hashes for the corpus, prompt, policy, and predictions.

Interpret roles individually. The thresholds in `policy.json` are
pre-registered decision aids, not a substitute for the annotation limitations
in [corpus.md](corpus.md). A low frontier score with credible label agreement
points toward merging, shrinking, or resegmenting a role. A material
local-over-rules gain supports an opt-in enrichment pass. Rules within the
declared per-role delta preserve the offline fast path.

## Tests

The default suite is fully offline:

```bash
uv run pytest tests/test_roles_benchmark.py tests/test_frontier_codex.py
uv run pytest
```

HTTP behavior is tested with an injected transport. No test contacts a live
model endpoint, downloads a corpus source, or imports benchmark code from the
AutoTLDR CLI path.
