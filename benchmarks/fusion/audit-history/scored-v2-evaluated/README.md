# Evaluated Stage 4 implementation snapshot

This directory preserves the exact production fusion source evaluated by the
one-shot corrected-v2 held-out run on 2026-08-30. It is evidence, not the live
shipping implementation.

| Artifact | SHA-256 |
| --- | --- |
| `fusion-830d42e7.py` | `830d42e7efcdf3fd20beac11733acf9276c0484af810e439dd70122de8dc8420` |
| scored predictions | `c9fd530184b14219cbc6f409380dd0a1e6ac45c12efda445c90ef5a184d17ac9` |
| prediction manifest | `5068c02faa8522d71c4576eac4da05ee979355c385870ef9870c909aa4f31b97` |
| JSON report | `0b73c0544e1e8276a63c6d8c1b07ef9f2f532b9029de90d0217d5cbef5bc6604` |
| Markdown report | `7ef261caac77081d4eb1f1ce898ba9d019d36e9273019c575663c2bccb0eca93` |

The evaluator measured the raw `analyze()` candidate surface. The live
`fuse()` path was then changed only to apply the report's mechanical frozen
dispositions; no matcher was tuned and the scored run was not repeated:

| Signal | Precision | Recall | Frozen disposition |
| --- | ---: | ---: | --- |
| literal | 1.000 | 0.957 | ship complete |
| identifier | 0.850 | 0.830 | ship `native-native` only |
| structural | 1.000 | 0.800 | ship complete |
| contradiction | 1.000 | 0.667 | disable |
| orphan | 1.000 | 0.571 | disable |
| unresolved | 0.900 | 1.000 | ship `local-path` only |

The immutable scored artifacts remain at the benchmark root. Re-running the
report against the post-disposition live source is intentionally rejected by
the implementation-hash binding. Re-evaluating changed match logic requires a
new held-out source group under the frozen policy.
