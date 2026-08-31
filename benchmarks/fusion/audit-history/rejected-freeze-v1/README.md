# Rejected Stage 4 candidate freeze v1

This directory preserves the first pre-score freeze and both independent blind
audits. No scored prediction was produced against this freeze.

The candidate was rejected on 2026-08-30 before decisive scoring. The audits
found unadjudicated raw-source truth (two notebook links, one orphan-origin
missing reference, and scalar-name identifier coexistence), ambiguous
greenhouse and parcel adversaries, non-isolated namespace/derived hard
negatives, and upstream extraction artifacts. Stage 3 was corrected and the
corpus was superseded rather than tuned after a held-out result.

Files:

- `freeze.json`: exact rejected freeze record and hashes.
- `policy.json`: the rejected preregistered policy whose hash is bound above.
- `blind_a.md`: source-first audit A and blinding attestation.
- `blind_b.md`: source-first audit B and blinding attestation.
- `dev-diagnostic/`: label-isolated development predictions and report produced
  against the rejected freeze; no held-out source or scored prediction was used.

The corrected freeze must receive its own delta blind review before the
one-shot scored run.
