# Corrected Stage 4 fusion freeze: delta blind review B

Date: 2026-08-30  
Reviewer: B  
Verdict: **FIT for the scored Stage 4 engineering run**

## Freeze reviewed

This review is bound to the stable corrected freeze, not the rejected first
candidate:

| artifact | SHA-256 |
| --- | --- |
| `freeze.json` | `7cb67edf77156688c25aeb41d7018ddda2426dc7c6a3c572fcfb31f8354094fa` |
| `policy.json` | `b015d48e048b895d0002ad56f60a4ee4bd3bbc02229af886827671be37737ff3` |
| scored source tree | `5e4f8c1f3132395f40527949dc7b5410b3ade5f208c5401d70bca26cc9ac5db6` |
| scored collections manifest | `b284fdf5073a6e5d9685d7b7cde1537af9997d471802a0beefc4bd08ed6b89b8` |
| scored sources manifest | `86933d81723b9a4c5195851a2d245d7e1afbee059fb477b7ed19dfea442414cd` |
| scored extraction snapshot | `f83b4dd639ed5a4d24721d43f524370cc25854349f27cd3abed2791e84978cc5` |

The freeze manifest declares `frozen_before_predictions=true` and requires the
delta blind reviews before a scored run. It supersedes
`audit-history/rejected-freeze-v1/freeze.json`. The scored label and annotation
hashes recorded opaquely in the manifest are unchanged at `4a208074...` and
`e9454ce7...`; I did not open either artifact.

## Method

I independently read the corrected policy, all 34 scored raw sources, all 34
frozen scored extractions, and the collection/source/freeze manifests. I also
examined the raw dev fixtures and dev extraction snapshot solely to assess
split-template leakage. The rejected-v1 audit was used only as a checklist of
previously identified blockers.

Truth was reconstructed source-first under the policy's current operational
definitions. A source fact was counted only when the frozen extraction makes
its evidence addressable. Identifier support uses the declared per-identifier
source-pair closure: one identifier present in *n* accepted sources contributes
`n * (n - 1) / 2` pairs. Identifier and contradiction signals are nonexclusive,
as schema-2 policy now states explicitly.

## Exact truth reconstruction

| collection | literal | identifier | structural | strict contradiction | orphan | unresolved |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| battery | 4 | 21 | 1 | 2 | 1 | 1 |
| capacity | 3 | 21 | 1 | 2 | 1 | 1 |
| greenhouse | 4 | 42 | 3 | 2 | 1 | 2 |
| parcel_dispatch | 3 | 20 | 1 | 2 | 2 | 2 |
| telescope | 6 | 47 | 3 | 2 | 1 | 2 |
| water_billing | 3 | 20 | 1 | 2 | 1 | 1 |
| **total** | **23** | **171** | **10** | **12** | **7** | **9** |
| policy support | **23** | **171** | **10** | **12** | **7** | **9** |

Every role total and every collection group reconstructs exactly without using
gold labels.

The three changed totals are independently explainable:

- Literal rises from 21 to 23 because source-wide eligibility adds the
  addressable notebook references `battery/analysis.ipynb -> cycles.csv` and
  `telescope/reduce.ipynb -> frames.csv`.
- Identifier rises from 157 to 171: the original 157 shared-field closure pairs
  remain, all 12 strict-contradiction fact-name pairs also count as identifier
  correspondences, battery's equal-decimal `nominal_voltage_v` pair adds one,
  and capacity's declared/derived `effective_capacity_mbps` pair adds one.
- Unresolved rises from 8 to 9 because greenhouse's explicit missing
  `moisture-readings.csv` is now correctly counted even though its source is
  otherwise orphaned.

Structural support is also internally checkable by subtype: seven
table-to-record pairs across five collections and three record-to-record pairs
across greenhouse, telescope, and water billing produce the exact total of ten.

## Delta blocker closure

| rejected-v1 blocker | corrected evidence | result |
| --- | --- | --- |
| Narrative-only literal eligibility silently excluded notebook references. | `truth_scope` is source-first and format-neutral; both notebook filenames are emitted as path-reference units. | **Closed.** |
| Greenhouse's substring case also contained a negated exact `readings.csv`, while the longer missing path was omitted from unresolved truth. | The fixture now says only that eyepiece records go to the explicitly missing `moisture-readings.csv`; no standalone real filename remains. The policy counts unresolved paths from orphan sources. | **Closed.** The long name is simultaneously a literal substring negative and a positive unresolved gap, without ambiguity. |
| Parcel `draft.json` did not establish its foreign domain strongly enough. | `domain_notice` says bakery/sourdough planning only and names the colliding fields as bakery concepts; the extraction exposes that notice plus `bakery_namespace`. | **Closed.** |
| `derived-vs-declared` had no actual same-name candidate pair. | `overview.md` declares `effective_capacity_mbps = 3000`; the XLSX extraction exposes a same-named unit with `derived=true` and its formula. | **Closed.** It is an identifier positive and contradiction negative. |
| Namespace-collision classes were not isolated. | `draft.json` has `bakery_namespace.routing_epoch` and `.audit_window_s`; `policy.toml` has `dispatch_namespace` leaves with the same spellings and comparable unequal scalars. | **Closed.** Qualified namespaces make both identifier and contradiction negatives explicit. |
| Telescope's external DOI survived only inside prose. | `methods.tex` now emits `https://doi.org/10.5555/example.external` as its own URL reference unit. | **Closed.** The external-DOI negative is active at candidate generation. |
| Blank rows became phantom nullable records, and battery decimals became path references. | Affected CSV/TSV snapshots report the correct data-row counts with `malformed_rows=0`; blank lines are reported only as skipped gaps. `meters.jsonl` has three records. Battery exposes no numeric path-reference units. | **Closed.** |
| Identifier/contradiction coexistence and reference polarity were unstated. | `signal_coexistence`, `literal`, `identifier`, `contradiction`, `unresolved`, `external_identity`, and `orphan` now have explicit operational definitions. | **Closed.** The new counts follow those rules exactly. |

## Collection audit

### Battery

Expected positives:

- Literal (4): `report.md` resolves `analysis.ipynb`, `cycles.csv`, and
  `limits.toml`; `analysis.ipynb` independently resolves `cycles.csv`.
- Identifier (21): `cycle_index`, `capacity_mah`, and
  `internal_resistance_mohm` each span notebook, CSV, TOML record schema, and
  report prose, contributing 18 closure pairs. `cutoff_voltage_v`,
  `rest_period_min`, and `nominal_voltage_v` each add the report/TOML pair.
- Structural (1): the CSV table corresponds to TOML `cycle_examples` on the
  same three discriminative fields.
- Strict contradiction (2): `cutoff_voltage_v` 2.80 versus 2.70 and
  `rest_period_min` 30 versus 45.
- Orphan (1): `todo.txt` is a bicycle-route note with no accepted cross-source
  relation.
- Unresolved (1): `thermal-appendix.md` has no collection target.

Hard negatives are realistic and separable: the manufacturer URL is external;
3.60 and 3.6 are exactly equal under decimal normalization; “approximately
2.8” is not an exact scalar declaration; and the bicycle note supplies common
and one-token prose overlap without a native battery schema. The former decimal
path artifacts are absent from the snapshot. The CSV now contains exactly three
non-null records; its terminal blank line is only a reported skipped-row gap.

### Capacity

Expected positives:

- Literal (3): `overview.md` resolves `capacity.xlsx`, `export.csv`, and
  `schema.xml`.
- Identifier (21): `node_count`, `per_node_mbps`, and `overhead_factor` each
  span XLSX, CSV, XML, and overview prose for 18 closure pairs;
  `safety_margin_pct`, `rack_limit`, and `effective_capacity_mbps` add one pair
  each.
- Structural (1): CSV and repeated XML `/capacity/record` containers carry the
  same three logical fields.
- Strict contradiction (2): `safety_margin_pct` 12 versus 10 and `rack_limit`
  16 versus 20.
- Orphan (1): `unused.md` is explicitly a wedding-seating domain.
- Unresolved (1): `assumptions.md` is absent.

The corrected derived-versus-declared case is strong. XLSX
`effective_capacity_mbps` is addressable as a named formula with
`derived=true`, while overview's 3000 is explicitly called a planning
declaration. They correspond by identifier but cannot form a constant-constant
contradiction. XML's multi-valued `node_count` range versus scalar XLSX/CSV
observations supplies range/profile negatives. The “about sixteen” sentence is
an approximation. `unused.md` explicitly disambiguates its same-spelled
`node_count`, and generic names remain common-token negatives. The CSV snapshot
now has one real row, zero malformed rows, and no synthetic nullability.

### Greenhouse

Expected positives:

- Literal (4): `protocol.md` resolves `controller.py`, `readings.csv`,
  `readings.jsonl`, and `layout.json`.
- Identifier (42): `sample_id`, `soil_moisture_pct`, `valve_duty_pct`, and
  `zone_temperature_c` each span five sources—Python, JSON, CSV, JSONL, and
  protocol prose—for 40 closure pairs. `sample_interval_s` and
  `watering_window_min` add the JSON/protocol pairs.
- Structural (3): JSON `readings`, CSV, and JSONL are a three-source record
  component and therefore contribute all three pairs.
- Strict contradiction (2): `sample_interval_s` 15 versus 20 and
  `watering_window_min` 20 versus 25.
- Orphan (1): `old-notes.txt` remains an eyepiece-inventory orphan because a
  gap does not connect a source.
- Unresolved (2): `calibration.md` and `moisture-readings.csv` are both explicit
  missing paths.

The critical corrected sentence is now unambiguous: `moisture-readings.csv` is
an absent local reference and is not an exact identity for `readings.csv`.
There is no negated exact real-path unit. This cleanly exercises nonexclusive
truth across signal families: unresolved positive, literal substring negative,
and orphan source. `statistics` is an external import; the vendor URL is
external and has both query and fragment; “about 18 minutes” is approximate;
and eyepiece moisture/common fields are one-token and generic distractors.

### Parcel dispatch

Expected positives:

- Literal (3): `runbook.rst` resolves `dispatch.py`,
  `current/parcels.tsv`, and `policy.toml`.
- Identifier (20): `parcel_id`, `route_bucket`, and `handoff_delay_ms` each
  span live TSV, Python, TOML examples, and runbook prose for 18 closure pairs;
  `retry_window_s` and `max_batch_parcels` add the policy/runbook pairs.
- Structural (1): the live TSV and TOML `parcel_examples` share the three-field
  dispatch record schema.
- Strict contradiction (2): `retry_window_s` 45 versus 60 and
  `max_batch_parcels` 100 versus 120.
- Orphans (2): `archive/parcels.tsv` and the explicitly bakery-only
  `draft.json` have no accepted positive cross-source relations.
- Unresolved (2): `handoff-map.csv` is missing; unqualified `parcels.tsv` is
  ambiguous between two real collection basenames.

The bakery-domain correction is sufficient in both source and extraction.
`domain_notice` remains visibly “Bakery sourdough planning only” even in the
bounded schema summary, and the `bakery_namespace` path is retained. Thus its
same-spelled `parcel_id` and `route_bucket` are clear different-domain
identifier negatives rather than malformed live-dispatch positives.

Namespace collision is now directly testable: bakery and dispatch namespaces
both contain `routing_epoch` and `audit_window_s`, but their qualified concepts
are different. Unequal values must not become contradictions and identical leaf
spellings must not become identifier links. The archive/live TSVs are a clean
same-width/different-fields structural negative. The bakery draft adds generic
fields, low overlap, and incompatible field types. That incompatible-type case
is multi-factor rather than a one-variable control—the intended live TSV/TOML
pair itself represents `route_bucket` as inferred integer versus quoted
string—but the negative source is semantically unambiguous because domain and
field-set evidence agree. `handoff_timeout_s` versus `handoff_timeout_ms` is a
clear unit/name mismatch; `pathlib` and the carrier URL are external.

### Telescope

Expected positives:

- Literal (6): `methods.tex` resolves `reduce.ipynb`, `frames.csv`,
  `frames.jsonl`, `instrument.yaml`, and appendix label `tab:frames`;
  `reduce.ipynb` independently resolves `frames.csv`.
- Identifier (47): `frame_id`, `sky_level_adu`, and `focus_offset_um` each span
  appendix, CSV, JSONL, YAML, methods, and notebook for 45 closure pairs;
  `exposure_time_s` and `gain_e_per_adu` add the methods/YAML pairs.
- Structural (3): CSV, JSONL, and YAML `frames` are a three-source record
  component.
- Strict contradiction (2): `exposure_time_s` 30 versus 25 and
  `gain_e_per_adu` 1.7 versus 1.8.
- Orphan (1): `old-memo.txt` is explicitly an orchestra memo.
- Unresolved (2): `fig:future-flat` lacks a local label and `ortega2025` lacks a
  local citation declaration.

All four prose filenames in LaTeX, both label references, the citation key, and
the DOI are now individual reference units. The DOI's `doi.org` target is
therefore an active external-DOI negative rather than prose-only inert text. It
does not syntactically declare or resolve `ortega2025`. Differing frame-value
profiles are not contradictions; the approximate zero-focus sentence is not a
scalar assignment; and the orchestra's ordinary “Focus,” `id`, and `status`
remain clean one-token/common-name negatives. The CSV snapshot reports three
real rows, zero malformed rows, and no phantom nullable observation.

### Water billing

Expected positives:

- Literal (3): `spec.html` resolves `tariff.py`, `meters.jsonl`, and
  `tariff.json`.
- Identifier (20): `account_ref`, `meter_reading_l`, and `tariff_band` each span
  JSONL, JSON, Python, and HTML prose for 18 closure pairs;
  `grace_period_days` and `late_fee_pct` add the HTML/JSON pairs.
- Structural (1): JSONL records correspond to JSON `meters` records.
- Strict contradiction (2): `grace_period_days` 7 versus 10 and `late_fee_pct`
  2 versus 3.
- Orphan (1): `legacy.md` is a pottery-kiln ledger.
- Unresolved (1): `audit-notes.md` is absent.

The regulator URL is a clear external reference with a fragment. “About a
week” is descriptive approximation, not a second scalar. `legacy.md` explicitly
says its `account_ref` denotes a ceramics customer rather than a water meter,
making it the strongest prose-only/same-spelling-different-domain control in the
corpus. JSON/JSONL meter ranges are profiles rather than constants. The JSONL
snapshot has three records and reports its blank terminal line only as a skipped
gap.

## Hard-negative coverage

### Literal

| class | current evidence | assessment |
| --- | --- | --- |
| ambiguous-basename | parcel `parcels.tsv`, with real archive/current candidates | Active and unambiguous. |
| external-import | scored `statistics` and `pathlib` imports | Active as import-reference units. |
| external-url | explicit external URL units in Markdown, RST, HTML, and LaTeX | Active. |
| query-or-fragment | greenhouse query+fragment and water fragment URLs | Active. |
| substring-basename | missing `moisture-readings.csv` versus existing `readings.csv` | Active and now clean; it remains a legitimate unresolved positive. |

### Identifier

| class | current evidence | assessment |
| --- | --- | --- |
| common-token | `id`, `name`, `status`, `value`, `result`, `timestamp` distractors | Active across collections. |
| namespace-collision | bakery versus dispatch `routing_epoch` and `audit_window_s` | Active, qualified, and isolated from positive concepts. |
| one-token-overlap | eyepiece “Moisture,” orchestra “Focus,” archive `id` versus compound domain identifiers | Active. |
| prose-only-overlap | capacity `node_count`; water `account_ref` | Active with explicit foreign-domain prose. |
| same-spelling-different-domain | capacity, water, and corrected parcel examples | Active and unambiguous. |

### Structural

| class | current evidence | assessment |
| --- | --- | --- |
| generic-fields | archive/draft and prose distractor schemas | Active. |
| incompatible-types | bakery `parcel_id` integer and `route_bucket` boolean versus live meanings | Active but intentionally co-occurs with low overlap/domain evidence. |
| low-overlap | bakery draft versus live dispatch record | Active. |
| same-width-different-fields | archive and current three-column TSVs | Active and clean. |

### Contradiction

| class | current evidence | assessment |
| --- | --- | --- |
| approximate-vs-scalar | explicit approximate prose in every collection | Active. |
| derived-vs-declared | XLSX/overview `effective_capacity_mbps` | Active as a real same-name pair. |
| equivalent-number | battery 3.60 versus 3.6 | Active. |
| multi-valued-profile | differing record profiles, especially greenhouse and telescope | Active. |
| namespace-collision | unequal bakery/dispatch namespace scalars | Active and fully qualified. |
| range-vs-scalar | capacity XML ranges versus scalar XLSX/CSV observations | Active. |
| unit-name-mismatch | parcel `handoff_timeout_s` versus `handoff_timeout_ms` | Active. |

### Unresolved

| class | current evidence | assessment |
| --- | --- | --- |
| external-doi | telescope DOI URL reference unit | Active at extraction/candidate level. |
| external-import | greenhouse and parcel import units | Active. |
| external-url | external URL units in all six scored collections | Active. |
| resolved-local | all 23 literal positives, including notebook and LaTeX paths | Active across all six groups. |

Every preregistered hard-negative class is now genuinely represented. The
controls are synthetic and often deliberately explicit, but no class depends on
the previous ambiguous inference.

## Extraction stability and addressability

Read-only consistency checks against the frozen artifacts found:

- all 34 raw fixture hashes match their entries in `scored/sources.jsonl`;
- the 34-source collection manifest, sources manifest, and extraction snapshot
  have exactly the same collection/source set, with no duplicates or omissions;
- the snapshot contains 222 Units and 122 intra-source Relations;
- every Unit content hash recomputes correctly;
- every Unit index is sequential, every Relation endpoint is in range, and
  every Unit/gap origin names its owning logical source;
- all Units have a nonempty origin reference and a content hash;
- the only non-UNKNOWN deterministic roles are the four XLSX assumption inputs,
  consistent with the earlier role-taxonomy decision; schema-2 policy requires
  the scored robustness run to prove that replacing all roles with UNKNOWN does
  not change fusion predictions.

The source-to-extraction corrections are substantive, not cosmetic:

- notebook Markdown filenames are explicit path-reference Units;
- LaTeX plain paths and the DOI are explicit reference Units while label and
  citation handling remains addressable;
- five CSV/TSV sources now report their true data-row counts with zero malformed
  rows; skipped terminal blanks no longer add null types or records;
- the water JSONL record count remains three despite its reported skipped blank
  line;
- battery scalar decimals remain prose and are no longer path references;
- the capacity formula carries `label`, `formula`, and `derived=true` metadata;
- parcel's domain and qualified namespace paths survive extraction; and
- all strict scalar declarations needed for the 12 contradictions remain
  individually addressable.

This is sufficient corpus-side stability for the scored run. Deterministic
repeat, input-permutation equality, all-UNKNOWN equivalence, full existing Unit
IDs, and structured-trace-only scoring are runtime properties and must still be
enforced by the run; this blind audit did not execute forbidden fusion or
evaluator code.

## Policy measurability

Schema-2 policy resolves the earlier adjudication gaps:

- truth eligibility is source-wide and conditioned on extraction
  addressability, not narrative format;
- literal identity requires an explicit non-negated reference and exactly one
  defensible local target;
- identifier concepts include same-domain fields, symbols, declared scalars,
  and named derived outputs, while namespaces remain semantic context;
- identifier and contradiction may coexist on one grounded fact pair;
- contradiction is restricted to exact unequal comparable
  constant-versus-constant declarations with the same qualified concept;
- observations, profiles, ranges, formulas, approximations, equivalent decimals,
  namespaces, and units are expressly excluded from contradiction truth;
- unresolved includes missing or ambiguous local references from orphan sources;
  external identities do not become gaps merely because they are absent; and
- orphan status depends on accepted positive cross-source relations, not on the
  presence of a reported gap.

Those definitions reproduce all current counts and make the zero-false-positive
contradiction/orphan/unresolved gates adjudicable.

Subtype evidence should still bound any claim made after scoring. Local-path
literal positives have 22 supports across six groups, while positive label
resolution has one support and citation/URL-source identity have none. Unresolved
has six ordinary local-path supports across five groups plus one ambiguous path,
one label, and one citation. Structural support is sufficiently balanced at
7 table-record and 3 record-record pairs. Rare literal/unresolved subtypes do not
meet the policy's three-support/three-group subtype floor and therefore cannot
independently justify a subtype shipment even if aggregate behavior looks good.
This is a declared scope boundary, not a count defect.

## Leakage and external-validity audit

I found no raw fixture containing gold relation IDs, expected support counts,
prediction traces, or evaluator outcomes. The extraction snapshot contains
source-derived content and metadata rather than score annotations.

There is strong *construction-template* overlap between dev and scored data:
hub documents say “shared fields/identifiers,” missing references are often
called pending or missing, external links are called external, approximate
values are explicitly qualified, and unrelated distractors state their foreign
domain. The two dev collections and six scored collections use different
domains and filenames, but the rhetorical pattern is recognizably the same.

That does not invalidate this freeze for its stated purpose. These statements
are source semantics that AutoTLDR should honor, and the policy explicitly calls
the corpus an engineering gate rather than a production-prevalence estimate.
It does mean a pass demonstrates the preregistered deterministic mechanisms on
this pattern family; it does not estimate generalization to naturally occurring,
implicit, noisy multi-document collections. No later stage should cite these
scores as production accuracy.

## Residual risks

1. The corpus is small and synthetic, and all six scored collections follow a
   similar hub + shared schema + two strict conflicts + distractor construction.
2. Structural incompatible-type coverage is semantically valid but multi-factor;
   it is not a pure type-only ablation because the bakery draft also differs in
   domain and field overlap.
3. Several allowed rare subtypes have one or zero positive examples and are
   intentionally ineligible for subtype-level shipment.
4. The extraction snapshots still report terminal blank lines as explicit gaps.
   They no longer corrupt schemas, but downstream code must keep extraction gaps
   distinct from fusion unresolved-reference gaps.
5. Corpus review cannot establish the policy's runtime robustness properties;
   those remain mandatory run-time assertions.

None of these residual risks changes an intended gold judgment, prevents exact
support measurement, or recreates a rejected-v1 blocker.

## Fitness verdict

**FIT.** The corrected freeze is suitable for the scored Stage 4 model-free
fusion engineering run.

The positive truths reconstruct exactly by role and collection, all evidence is
addressable through the frozen Stage 1 representation, every preregistered hard
negative is active, the previous ambiguous cases have explicit operational
answers, and source/extraction bindings are internally consistent. The scored
run can now answer the Stage 4 question—literal, identifier, structural,
contradiction, orphan, and unresolved recoverability separately—without relying
on an unmeasured field or an unreproducible annotation convention.

This verdict authorizes measurement, not post-score tuning. The result should be
reported per signal, collection group, format pair, and eligible subtype under
the frozen gates. Any changed implementation requires a new scored source group,
and unsupported rare subtypes must not be promoted by aggregate performance.

## Blinding attestation

This attestation applies to freeze
`7cb67edf77156688c25aeb41d7018ddda2426dc7c6a3c572fcfb31f8354094fa`.

For this delta audit I did **not** open, read, search, parse, hash directly, or
otherwise inspect current `labels.jsonl`, any `annotations.jsonl`, any content
under `predictions/` or `reports/`, evaluator implementation or output, reviewer
A's corrected review, or fusion production implementation. The label and
annotation prefixes above were read only as opaque bindings from `freeze.json`.
I did not import or call `analyze`, `run_signals`, `fuse`, or evaluator run/report
code, and I did not run model inference. I did not inspect dev predictions.

I consulted the rejected first freeze only as the supplied prior-blocker
checklist. Current judgments and counts were reconstructed independently from
the corrected policy, raw fixtures, extraction snapshots, and manifests. I
changed no corpus, label, annotation, policy, production, test, prediction,
report, or state artifact. This replacement blind-B review is the sole edit made
for the delta audit.
