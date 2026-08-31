# Stage 4 fusion benchmark

This directory contains the model-free cross-source fusion benchmark for
AutoTLDR Stage 4. It measures each signal independently; there is deliberately
no aggregate accuracy gate. The decision is which signal, or preregistered
signal subtype, is safe enough to ship.

The benchmark connects the earlier stages instead of bypassing them:

- Stage 1/3 production routing extracts every real fixture in its native
  format. The checked-in extraction snapshots are audit aids, not substitute
  inputs to the implementation under test.
- Stage 2 role labels are erased and the run must remain bit-for-bit equal.
  Fusion therefore cannot inherit backend-specific role guarantees.
- Stage 3 input manifests bind every source to its actual bytes, format, and
  collection-relative identity.
- Stage 4 consumes those extractions and emits endpoint-exact, structured
  literal, identifier, structural, contradiction, unresolved, and orphan facts.

No model is loaded by this benchmark. Both manifests and reports record
`models: []`.

## Current status

The corrected v2 corpus was frozen before the held-out prediction and passes
all corpus, label, evaluator, deterministic-binary, and three-way snapshot
validations.
Two independent source-first delta reviews returned `FIT`, their hashes were
bound into an accepted clearance, and one immutable held-out run was completed
on 2026-08-30. The run was label-isolated, model-free (`models: []`), and
passed deterministic-repeat, input-permutation, all-`Role.UNKNOWN`, checkout-
relocation, and endpoint-existence checks. Aggregate accuracy is not a gate.

The final per-signal result and frozen production disposition are:

| Signal | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Overall gate | Frozen disposition |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| literal | 23 | 6 | 22 | 0 | 1 | 1.000000 | 0.956522 | 0.977778 | pass | `ship-complete` |
| identifier | 171 | 6 | 142 | 25 | 29 | 0.850299 | 0.830409 | 0.840237 | fail | `ship-preregistered-subtype`: `native-native` |
| structural | 10 | 6 | 8 | 0 | 2 | 1.000000 | 0.800000 | 0.888889 | pass | `ship-complete` |
| contradiction | 12 | 6 | 8 | 0 | 4 | 1.000000 | 0.666667 | 0.800000 | fail | `disable` |
| orphan | 7 | 6 | 4 | 0 | 3 | 1.000000 | 0.571429 | 0.727273 | fail | `disable` |
| unresolved | 9 | 6 | 9 | 1 | 0 | 0.900000 | 1.000000 | 0.947368 | fail | `ship-preregistered-subtype`: `local-path` |

The partial dispositions come only from subtypes frozen in advance:

| Signal subtype | Support | Groups | TP | FP | FN | Precision | Recall | F1 | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| identifier `explicit-prose-native` | 96 | 6 | 79 | 12 | 17 | 0.868132 | 0.822917 | 0.844920 | do not ship |
| identifier `native-native` | 63 | 6 | 54 | 6 | 9 | 0.900000 | 0.857143 | 0.878049 | ship |
| identifier `prose-prose` | 12 | 2 | 9 | 7 | 3 | 0.562500 | 0.750000 | 0.642857 | do not ship |
| unresolved `local-path` | 6 | 5 | 6 | 0 | 0 | 1.000000 | 1.000000 | 1.000000 | ship |

Other unresolved subtypes had only one positive each and do not independently
satisfy the preregistered support/group gates: ambiguous local path was 1/1,
citation key was 1/1, and label key had one true positive plus one false
positive. Literal's only miss was the single
`label-key` edge; all 22 local-path edges were exact. Structural recovery was
3/3 for record-schema/record-schema and 5/7 for table/record-schema. The only
contradiction subtype, constant/constant, recovered 8/12 and therefore missed
its recall gate. Orphan recovery found 4/7. There were no duplicate predictions,
unclassified subtype predictions, or hard-negative false positives.

Identifier support is exhaustive under the non-exclusive rule: the original
157 central-schema source pairs, 13 same-domain contradiction/equality scalar
name pairs, and the newly instantiated same-name derived-output/declaration
pair. That last pair is a valid identifier correspondence while remaining a
hard negative for contradiction.

Corrected freeze hashes:

| Artifact | SHA-256 |
| --- | --- |
| freeze record | `7cb67edf77156688c25aeb41d7018ddda2426dc7c6a3c572fcfb31f8354094fa` |
| policy | `b015d48e048b895d0002ad56f60a4ee4bd3bbc02229af886827671be37737ff3` |
| dev source tree | `667fb23e3b5033116921107cffb07d83d25f3ffad9982b1b0684af49a25f7602` |
| dev sources snapshot | `fffbc09efdbeb1914487d7f6a8afb2c9e2feda23b2d56583cf46058c67c9afda` |
| dev extractions snapshot | `7c01a5834fbef4df810dcb515a525ff227726b905aff719575715f5c572ebf18` |
| dev labels | `502b81148e3b339b9da3d599751635270c0ab1923d61f44ff03c0a4d2890101c` |
| dev annotations | `48c29a6e4f4ce24564c64098348e8d6f15a74984b2030b665992ad870e1786bb` |
| scored source tree | `5e4f8c1f3132395f40527949dc7b5410b3ade5f208c5401d70bca26cc9ac5db6` |
| scored sources snapshot | `86933d81723b9a4c5195851a2d245d7e1afbee059fb477b7ed19dfea442414cd` |
| scored extractions snapshot | `f83b4dd639ed5a4d24721d43f524370cc25854349f27cd3abed2791e84978cc5` |
| scored labels | `4a208074cdad1e6ec60fc39b778916eedaf8aad4c1be85f7f55cb755a5a9ae9c` |
| scored annotations | `e9454ce7e875de3305fe4f4c3455483d7b70c9d9094c52bedfc9d6b6fcb4dad7` |

### Blind clearance and immutable scored artifacts

Both reviewers reconstructed the frozen support independently, without
inspecting labels, predictions, evaluator output, or the fusion implementation,
and returned `FIT`. Clearance attests that scored predictions were absent at
that point.

| Artifact | SHA-256 |
| --- | --- |
| blind review A | `f10fbad9d7a60fd963b212248df86d4ad8d6a58cdf37e06e6f54f5e3f935d8aa` |
| blind review B | `40107bbeef53656aec57fb07dc31dd136d4910e51f47e1b04491a02118e533c4` |
| accepted clearance | `499445c53592adb339d06bce84c4ac61566053a01c4cf613571fc3b1459d31b4` |
| scored predictions | `c9fd530184b14219cbc6f409380dd0a1e6ac45c12efda445c90ef5a184d17ac9` |
| scored prediction manifest | `5068c02faa8522d71c4576eac4da05ee979355c385870ef9870c909aa4f31b97` |
| scored JSON report | `0b73c0544e1e8276a63c6d8c1b07ef9f2f532b9029de90d0217d5cbef5bc6604` |
| scored Markdown report | `7ef261caac77081d4eb1f1ce898ba9d019d36e9273019c575663c2bccb0eca93` |

The scored manifest binds evaluator
`331d14c93d9d9d0195121b1a8a4d1cc0a6d48064fb89c74aacb4d85272cca402`
and evaluated fusion implementation
`830d42e7efcdf3fd20beac11733acf9276c0484af810e439dd70122de8dc8420`.
The exact evaluated source is preserved as
[`audit-history/scored-v2-evaluated/fusion-830d42e7.py`](audit-history/scored-v2-evaluated/fusion-830d42e7.py),
and its archive README has SHA-256
`5b91624d6a3ad879b76eceabb1cbc7f73e241d2976ebb576e5611b282c433136`.

### Evaluated `analyze()` versus live `fuse()`

The immutable scored run measured the raw `analyze()` candidate surface in the
archived `830d42e7…` source. After scoring, the live fusion file changed to SHA-
256 `25989cc411d69cc547d7a24aa1138fa4ba373415727bcc428b612a6fc7ffc2bf`.
No matcher or raw candidate rule was tuned: `analyze()` and `run_signals`
remain the transparent diagnostic surface. The production `fuse()` path now
mechanically applies the report's frozen dispositions:

- emit every literal and structural candidate;
- emit identifier candidates only when both endpoints are native;
- emit no contradiction or orphan finding;
- emit unresolved references only for non-ambiguous local paths;
- record evaluated-disposition metadata and raw-before-filter counts in the
  collection manifest.

The live file intentionally does not reproduce the scored implementation hash.
The scored prediction and report were not rerun, and reporting them against the
post-disposition source is rejected by implementation-hash binding. Any future
matcher change or new performance claim requires a new held-out source group
under the frozen policy; the corrected-v2 scored artifacts remain immutable.

### Pre-score development diagnostic

The corrected-v2 development diagnostic was model-free and had no errors on
the two visible source groups:

| Signal | Support | TP | FP | FN | Precision | Recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| literal | 7 | 7 | 0 | 0 | 1.000 | 1.000 |
| identifier | 56 | 56 | 0 | 0 | 1.000 | 1.000 |
| structural | 2 | 2 | 0 | 0 | 1.000 | 1.000 |
| contradiction | 2 | 2 | 0 | 0 | 1.000 | 1.000 |
| orphan | 2 | 2 | 0 | 0 | 1.000 | 1.000 |
| unresolved | 2 | 2 | 0 | 0 | 1.000 | 1.000 |

All identifier subtype slices are also exact: explicit-prose/native 32/32,
native/native 21/21, and prose/prose 3/3. All five robustness checks pass.
The manifest binds production fusion
`830d42e7efcdf3fd20beac11733acf9276c0484af810e439dd70122de8dc8420`,
evaluator
`331d14c93d9d9d0195121b1a8a4d1cc0a6d48064fb89c74aacb4d85272cca402`,
and prediction bytes
`5310019404e96cfad0db6be950756381ec7b6494c9f325e157d4db4b99306720`.
Those results were diagnostic, not a gate decision: development has only two
source groups, while the preregistered gates require the held-out support and
group counts.

Pre-score development artifact hashes:

| Artifact | SHA-256 |
| --- | --- |
| prediction manifest | `e959c5db4d23ddcbd814e103c50ecbcdc03b8b6139a22019b39b313b12c3473c` |
| JSON report | `022442610941e5e066c52d370f619b82a566b5f43aa3c8204f27cc3eeaaeff12` |
| Markdown report | `2f59b27d0d2ff871277d70f12ea5ac618c68e7d6e31a54a864b970f178d62c76` |

The development literal support changed from six to seven after the first v2
diagnostic exposed an omitted notebook-origin source edge. The source-first
adjudication, prediction-seen annotation, pre-correction artifacts, refreeze
hashes, and a caught-and-restored XLSX reproducibility issue are recorded in
[`audit-history/corrected-freeze-v2-dev-label-correction.md`](audit-history/corrected-freeze-v2-dev-label-correction.md).

For historical context, the last development diagnostic against the rejected
v1 freeze was:

| Signal | Support | TP | FP | FN | Precision | Recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| literal | 6 | 6 | 0 | 0 | 1.000 | 1.000 |
| identifier | 54 | 48 | 2 | 6 | 0.960 | 0.889 |
| structural | 2 | 2 | 0 | 0 | 1.000 | 1.000 |
| contradiction | 2 | 2 | 0 | 0 | 1.000 | 1.000 |
| orphan | 2 | 2 | 0 | 0 | 1.000 | 1.000 |
| unresolved | 2 | 2 | 0 | 0 | 1.000 | 1.000 |

Those two identifier “false positives” were the evidence that triggered the
non-exclusive adjudication; they are positive identifier truth in v2. The old
dev labels also omitted pair-level prose provenance. The complete old artifacts
are preserved under `audit-history/rejected-freeze-v1/dev-diagnostic/`; this
table is not a v2 result.

### Rejected freeze history

The first candidate freeze, dated 2026-08-30, was rejected before any scored
prediction. Its hashes are retained here so the audit trail survives the
corrective refreeze:

| Artifact | SHA-256 |
| --- | --- |
| policy | `abf237e3370d51bb44c1416e5f3d6290fbd92c19f089c4f3b0cd093002c15747` |
| dev source tree | `667fb23e3b5033116921107cffb07d83d25f3ffad9982b1b0684af49a25f7602` |
| dev sources snapshot | `fffbc09efdbeb1914487d7f6a8afb2c9e2feda23b2d56583cf46058c67c9afda` |
| dev extractions snapshot | `dfe79b46a4194947dcf549f1ac8c6311e7b01b417702ef6b92ef450c610685ab` |
| dev labels | `1cc017b80e182f60c05b39947e8788d9c29c7e4420cf0ab01eb3ee3a3ba6dd80` |
| scored source tree | `99a730adb10e8fc65dd25528377b8701971343860a04cd1d37051660e5757631` |
| scored sources snapshot | `1694e73f1686e241c666cb67b5f0d446eff52cb028583c5049c1a5f5d21ef3ea` |
| scored extractions snapshot | `37eeb2b76652b272ad771a3d3b1d0dff3264863db741c97c5121aa3c5aca5f5c` |
| scored labels | `5a9c9956e9987979cfaf7129ab2f47eaa3d2368431a3f7e0af8594989d936b5a` |

The independent source-first audits are preserved in
[`audit-history/rejected-freeze-v1/blind_a.md`](audit-history/rejected-freeze-v1/blind_a.md)
and
[`audit-history/rejected-freeze-v1/blind_b.md`](audit-history/rejected-freeze-v1/blind_b.md).
They rejected decisive scoring
because literal source eligibility, negation/missingness, identifier relation
scope, several hard-negative instantiations, and extraction artifacts were not
yet cleanly adjudicated. Subsequent Stage 3 fixes intentionally changed the
extraction snapshots by materializing notebook/LaTeX references, suppressing
URL-overlapping path matches, rejecting decimal literals as paths, and ignoring
terminal blank delimited rows. The old freeze remains immutable history.

## Layout

- `policy.json` preregisters per-signal support, group, precision, and recall
  gates; allowed shrink subtypes; hard-negative classes; and normalization.
- `freeze.json` binds policy, source trees, collections, source snapshots,
  extraction snapshots, labels, and annotations by SHA-256.
- `dev/` is the visible tuning split. Its report is written to
  `dev/report.json` and `dev/report.md`.
- `scored/` is the held-out split. Its canonical report paths are
  `report.json` and `report.md` at this directory root.
- `predictions/` stores label-isolated structured outputs and their manifests.
- `reviews/` contains the two completed independent delta reviews and the
  accepted, hash-bound clearance that unlocked the one-shot scored run.
- `audit-history/rejected-freeze-v1/` preserves the superseded freeze, its two
  independent audits, and its development diagnostic.
- `audit-history/corrected-freeze-v2-pre-dev-label-correction/` preserves the
  first v2 development diagnostic exactly as observed before source-first
  correction of its omitted literal label.
- `audit-history/scored-v2-evaluated/` preserves the exact fusion source
  measured by the immutable scored run and its artifact bindings.
- `build_corpus.py` builds the deterministic XLSX fixture, extracts all
  fixtures through the production router, freezes snapshots, and validates the
  three-way on-disk/canonical/frozen binding.
- `evaluate.py` validates labels, runs production fusion, performs robustness
  checks, canonicalizes predictions, attributes prediction subtypes without
  consulting gold, and produces reports.

## Reproduce the safe checks

Use the repository virtual environment so validation does not create or alter
dependency lock files:

```bash
.venv/bin/python benchmarks/fusion/build_corpus.py validate
.venv/bin/python benchmarks/fusion/evaluate.py self-test
.venv/bin/python benchmarks/fusion/evaluate.py validate-labels --split dev
.venv/bin/python benchmarks/fusion/evaluate.py validate-labels --split scored
.venv/bin/python benchmarks/fusion/evaluate.py validate
```

The scored run is complete. Do not delete, overwrite, regenerate, or reinterpret
its prediction and report as output from the live post-disposition source. The
evaluator enforces one-shot immutability: `--force` is dev-only, scored output
must use the canonical path, and the existing scored prediction/report
artifacts block another run. Development remains a visible diagnostic split,
but it cannot amend the completed held-out decision.

## What is scored

Each positive label expands into a canonical fact key:

- literal: collection, referencing source, resolved source, reference kind,
  and normalized target;
- identifier: collection, canonical concept, and every source pair in the
  recovered connected component;
- structural: collection, source pair, and a delimiter-independent tuple of
  canonical field-token tuples;
- contradiction: collection, source pair, canonical key, and exact canonical
  scalar values;
- orphan: collection and source;
- unresolved: collection, source, reference kind, and normalized target.

Duplicates are false positives. Wilson 95% intervals are descriptive. The
stable serialized inventories `false_positive_keys`, `false_negative_keys`,
and `duplicate_prediction_keys` in the JSON report make every error auditable.

Subtype precision is not derived by intersecting predictions with gold.
Instead, the evaluator classifies every prediction from its structured facts:

- literal uses reference kind and resolution;
- identifier uses the endpoint `left_native`/`right_native` evidence and
  propagates it through component closure;
- structural uses native source kinds plus container families;
- contradiction uses the two fact bases;
- unresolved uses reference kind plus abstention reason;
- orphan is the absence of any accepted cross-source relation.

An ambiguous prediction is charged to every plausible subtype. An
unclassifiable prediction makes subtype shipping ineligible. This prevents
unlabeled false positives from receiving mechanically perfect subtype
precision.

## Run isolation and binding

The `run` command has no labels parameter and never loads label content. It
records only the already-frozen label hash. It serializes structured candidate
details and typed unresolved facts directly and never parses human-readable
relation evidence.

Every collection must also pass:

- deterministic repeat equality;
- input-order permutation equality;
- all-`Role.UNKNOWN` equality;
- checkout-root relocation equality;
- exact existing unit-ID endpoints.

Before reporting, the evaluator verified the prediction bytes and bound the
manifest to the requested split, policy hash, source-tree hash, extraction
snapshot hash, label hash, current fusion implementation hash, evaluator hash,
and evaluator schema. A dev manifest cannot be scored against held-out gold.
For the completed scored run, “current fusion implementation” means the
archived `830d42e7…` source recorded in the manifest. The later live `fuse()`
projection is deliberately a different hash and does not alter the raw scored
measurement.

## Interpretation limits

The fixtures are synthetic diagnostic collections over real production
extractors. They test known decision boundaries and format interactions; they
do not estimate production prevalence. The frozen annotations have no human
domain-expert audit. Passing a signal says it met the preregistered engineering
gate on this corpus, not that arbitrary semantic fusion is solved.
