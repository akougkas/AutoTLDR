# Corrected-v2 development-label correction

Date: 2026-08-30

Scope: development split only. No scored prediction or scored report existed,
was run, or was inspected during this correction. Scored fixtures, extraction
snapshots, annotations, and labels were not edited.

The first development diagnostic against corrected freeze v2 emitted this
literal relation:

```text
relay_service: analysis.ipynb -> events.jsonl (path, target events.jsonl)
```

The relation was initially reported as the sole literal false positive. Direct
source-first adjudication showed that it is corpus truth: cell 1 of
`dev/fixtures/relay_service/analysis.ipynb` says “Inspect message_id and
ack_latency_ms from events.jsonl,” and frozen extraction unit 1 is a path
reference whose target is `events.jsonl`. The target is a member of the same
collection. This satisfies the frozen literal rule independently of the
prediction, so omitting the edge from development gold was a label error.

The correction adds `dev-relay-lit-004` with endpoints `analysis.ipynb` and
`events.jsonl`. It also marks the development annotation honestly as
prediction-seen. This is acceptable for the visible tuning split and is not
evidence about the held-out split.

Pre-correction bindings and diagnostic artifacts:

| Artifact | SHA-256 |
| --- | --- |
| freeze | `f175d5bbf18be69d14bd4a18bff7444b67a3987c539e2844fa1bea976592abfd` |
| dev labels | `4b277cffed81efb64a1d9a2d364f4a9ed57c9b7b81c19d3a9345a605edd87ffd` |
| dev predictions | `5d0933fb968500e5eadcd40d92b47c8a673a51271c73196e7d015df4d1183a67` |
| dev manifest | `9f67571c5a1a01e20afe44b00d5079169bf4a35bb6f96259b157533adef0128f` |
| dev JSON report | `649c1ead38c3fbfa6ba1ec69fe4ef42e9c774029b0bb4018eebcd602ceee1e40` |
| dev Markdown report | `8c4c9ddbfbf26625f36db5615d29244d65f2ec180ddf6dcf3a93af6d5a996bdb` |

Those four diagnostic artifacts are preserved verbatim under
`audit-history/corrected-freeze-v2-pre-dev-label-correction/`. New post-
correction freeze hashes and diagnostic results are recorded in the benchmark
README.

## Refreeze reproducibility incident

The first refreeze attempt changed only the held-out `capacity.xlsx` source
bytes. Investigation stopped before any new dev diagnostic and before any
scored run. Two unzipped generations differed only at
`docProps/core.xml`'s `dcterms:modified`: openpyxl overwrote that internal
property at save time even though the outer ZIP member times were already
fixed.

The originally validated corrected-v2 binary was recovered exactly. Its
internal modified value is `2026-08-30T21:16:58Z`, its SHA-256 is
`26a34562a4ad10492995bd9165f60ac3b89327f270e09b6ad0e6bcd854050afa`,
and it restores the prior scored source-tree hash
`5e4f8c1f3132395f40527949dc7b5410b3ade5f208c5401d70bca26cc9ac5db6`.
The builder now canonicalizes that internal field, and validation independently
requires two regenerated XLSX files and the checked-in fixture to be
byte-identical. Every scored binding after the final refreeze is exactly the
same as before the dev-label correction.

## Post-correction bindings and dev diagnostic

| Artifact | SHA-256 |
| --- | --- |
| freeze | `7cb67edf77156688c25aeb41d7018ddda2426dc7c6a3c572fcfb31f8354094fa` |
| dev labels | `502b81148e3b339b9da3d599751635270c0ab1923d61f44ff03c0a4d2890101c` |
| dev annotations | `48c29a6e4f4ce24564c64098348e8d6f15a74984b2030b665992ad870e1786bb` |
| dev predictions | `5310019404e96cfad0db6be950756381ec7b6494c9f325e157d4db4b99306720` |
| production fusion implementation | `830d42e7efcdf3fd20beac11733acf9276c0484af810e439dd70122de8dc8420` |
| evaluator with scored-review clearance guard | `331d14c93d9d9d0195121b1a8a4d1cc0a6d48064fb89c74aacb4d85272cca402` |
| dev manifest | `e959c5db4d23ddcbd814e103c50ecbcdc03b8b6139a22019b39b313b12c3473c` |
| dev JSON report | `022442610941e5e066c52d370f619b82a566b5f43aa3c8204f27cc3eeaaeff12` |
| dev Markdown report | `2f59b27d0d2ff871277d70f12ea5ac618c68e7d6e31a54a864b970f178d62c76` |

The corrected diagnostic has TP/support 7/7 literal, 56/56 identifier,
2/2 structural, 2/2 contradiction, 2/2 orphan, and 2/2 unresolved, with zero
false positives, false negatives, or duplicate predictions. All five
robustness checks pass. It is a visible development result and cannot determine
held-out eligibility.

At this checkpoint `reviews/clearance.json` is absent, so the evaluator's
scored-review clearance guard rejects a held-out run before inference. The two
delta reviews remain pending; no scored artifact exists.
