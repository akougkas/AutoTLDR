# Frozen role corpus

This document records the provenance and limits of the Stage 2 scored set. It
describes the corpus as frozen; it is not a model-result report.

## Identity and composition

The corpus contains 200 exact units emitted by AutoTLDR's production extractors
at rules commit `54b2158a37e7dd42392494fbadf031e11d952289`. It draws 40
units from each of five formats and four independently pinned documents per
format, for 20 source groups total.

| Format | Units | Source groups | Source family |
| --- | ---: | ---: | --- |
| Markdown | 40 | 4 | Kubernetes Enhancement Proposals, Apache-2.0 |
| reStructuredText | 40 | 4 | Python Enhancement Proposals, public domain/CC0 |
| plain text | 40 | 4 | RFC 8259, 8446, 9000, and 9110; IETF Trust terms |
| PDF | 40 | 4 | PLOS ONE research articles, CC BY 4.0 |
| XLSX | 40 | 4 | PLOS ONE supplementary workbooks, CC BY 4.0 |

Exact titles, immutable download URIs, attribution statements, licenses, and
source-file SHA-256 digests are in `sources.jsonl`.

The set is stratified to give every taxonomy role enough support for a
per-role diagnostic. It is intentionally neither class-balanced nor sampled to
represent the prevalence of roles in production documents.

| Gold role | Units | Distinct source groups |
| --- | ---: | ---: |
| `unknown` | 25 | 17 |
| `claim` | 17 | 13 |
| `definition` | 15 | 11 |
| `procedure` | 24 | 11 |
| `parameter` | 15 | 10 |
| `caveat` | 15 | 10 |
| `result` | 25 | 8 |
| `example` | 16 | 12 |
| `decision` | 15 | 8 |
| `assumption` | 17 | 5 |
| `limitation` | 16 | 10 |

Every role exceeds the preregistered minimum of three independent source
groups. Support remains small enough that the final analysis must report raw
support with per-role precision, recall, and F1 rather than hiding uncertainty
inside an aggregate score.

## Frozen hashes

The validator reports these canonical corpus-contract hashes:

| File | SHA-256 |
| --- | --- |
| `items.jsonl` | `e16b869d95b01f61b5fa01642583a3d02fdaee655caf3e95fea995aef8745dc8` |
| `labels.jsonl` | `f9496760807da643f547faa54aa0dc8af362f8247bc94a5af06ea64bdcae5e27` |
| `sources.jsonl` | `75ac99d83ab56145e0060d5cbe4f552e775417f75eac467728ca7c8cc0a3e761` |
| `policy.json` | `f486dff85886e0a397b130328647ae077eb8db8deb05c24999ebbd2b507118e7` |

The separately auditable annotation record has SHA-256
`51b0b3ae5c0b41e576dd3ef93e135eb36b0c87aa9900de15ba1231cd6a4142e2`.
It is not accepted as endpoint input and is not included in the model payload.

## Exact-unit construction

Source selection used real technical and scientific documents, never the
synthetic fixtures in `tests/`. Each source file was pinned by URI and digest.
The builder then:

1. verifies all 20 local source binaries against `sources.jsonl`;
2. runs `autotldr.extract.text`, `autotldr.extract.pdf`, or
   `autotldr.extract.xlsx`, as appropriate;
3. requires each selected origin reference and content to resolve to exactly
   one emitted production `Unit`;
4. verifies any recorded character span and frozen rules role; and
5. writes the unit's content, structure, modality, evidence, origin, and
   attribution without manually splitting or rewriting it.

This matters because mixed-role units expose a segmentation problem. Turning
one into a cleaner hand-edited sentence would measure an easier task than the
one the product actually presents to a tagger.

Eight first-pass units were replaced to improve role support. Every replacement
came from the same pinned source group as the discarded unit, remained an exact
production-extractor unit, and was independently confirmed by both blind
reviewers before arm scoring. The replacement map is explicit in
`build_corpus.py`; discarded origins and adjudications remain in
`annotations.jsonl`.

## Annotation protocol and agreement

An initial selector proposed a label and rationale. Two independent reviewers
then labeled every item without seeing model-arm predictions. Disagreements
were adjudicated separately. The eight replacements were also reviewed blind
by two reviewers, who agreed unanimously on their target roles. No rules,
local-model, or frontier prediction was consulted while selecting, reviewing,
adjudicating, or replacing units.

On the final 200 units:

- reviewer A and reviewer B agreed on 183/200 labels (91.5%);
- multiclass Cohen's kappa between the reviewers was 0.906;
- reviewer A matched the adjudicated label on 195/200 units (97.5%);
- reviewer B matched it on 188/200 units (94.0%);
- selector and both reviewers were unanimous on 153/200 units (76.5%); and
- both reviewers recorded high confidence on 185/200 units (92.5%).

These figures measure internal consistency, not human ground-truth quality.
The selectors, both reviewers, and adjudicator were AI agents, not human domain
annotators. They may share training data, model-family behavior, prompting
conventions, and the same interpretation of the taxonomy. In particular, a
frontier arm from the same or a closely related foundation-model family can
agree with these labels for circular reasons. Public source documents may also
have appeared in model pretraining. The resulting per-role scores are suitable
for a Stage 2 engineering gate, but the labels and frontier ceiling remain
provisional until a human domain-expert audit confirms them.

## Label isolation

The stored `items.jsonl` rows retain source provenance and `rule_role` so the
rules baseline and origins can be audited. The model runner does not serialize
those rows directly. It constructs a new request containing only:

```text
format, modality, content, structure, evidence
```

The runner has no labels argument. It excludes ids, source ids/groups, origins,
attribution, frozen rules roles, selection rationales, review notes,
adjudication, and gold labels. `labels.jsonl` is joined by id only during corpus
validation and post-run reporting. Tests assert this boundary and reject nested
gold or annotation fields.

## Rebuild and validate

The frozen records validate offline:

```bash
uv run python benchmarks/roles/evaluate.py validate
```

The output must report 200 items, 40 for each format, the role and source-group
counts above, one rules commit, and the four canonical hashes above.

Rebuilding requires the pinned source binaries plus selection/review artifacts
under the gitignored `.agent/scratch/roles` working directory:

```bash
uv run python benchmarks/roles/build_corpus.py
uv run python benchmarks/roles/evaluate.py validate
```

The rules arm is intentionally historical. D-013 shrank the live taxonomy after
these artifacts were frozen, so reproducing `rule_role` byte-for-byte requires a
checkout of rules commit `54b2158a37e7dd42392494fbadf031e11d952289` (plus the
scratch sources) before running the builder. The frozen JSONL files can still be
validated, scored, and reported from the current tree; the evaluator carries an
explicit frozen copy of the eleven evaluated role strings rather than importing
the smaller live enum.

`build_corpus.py` refuses source-hash drift, missing or duplicate reviewer
coverage, non-exact extractor matches, changed frozen rules roles, duplicate
origins, or replacements that cross source groups. The source binaries are not
vendored; `sources.jsonl` is the durable retrieval and licensing manifest.
