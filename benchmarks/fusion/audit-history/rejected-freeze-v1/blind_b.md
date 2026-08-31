# Stage 4 frozen fusion corpus: blind review B

Date: 2026-08-30  
Reviewer: B  
Disposition: **useful engineering corpus, but not yet fit as a decisive frozen ship/disable gate without adjudication**

## Scope and method

I reviewed `policy.json`, `freeze.json`, the scored collection/source/extraction
manifests, and every one of the 34 raw scored fixtures. I treated the raw source
meaning as primary and used the frozen extraction manifest only to verify what
Stage 1 actually makes addressable to Stage 4.

The identifier counts below use the policy's source-pair closure interpretation:
for one normalized identifier present in *n* intended sources, the expected
support is `n * (n - 1) / 2`. That is the only interpretation of the fixtures
that exactly reconstructs the preregistered identifier support of 157.

My independent intended-positive reconstruction is:

| collection | literal | identifier | structural | strict contradiction | orphan | unresolved |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| battery | 3 | 18 | 1 | 2 | 1 | 1 |
| capacity | 3 | 18 | 1 | 2 | 1 | 1 |
| greenhouse | 4 | 40 | 3 | 2 | 1 | 1 |
| parcel_dispatch | 3 | 18 | 1 | 2 | 2 | 2 |
| telescope | 5 | 45 | 3 | 2 | 1 | 2 |
| water_billing | 3 | 18 | 1 | 2 | 1 | 1 |
| **total** | **21** | **157** | **10** | **12** | **7** | **8** |

Those totals reproduce `policy.json` exactly. That is evidence of deliberate
construction, but not by itself evidence that every boundary is semantically
unambiguous. The collection-by-collection review below distinguishes the clean
cases from the cases where reproducing the total requires an undocumented
choice.

## Battery

### Expected positives

- Literal (3): `report.md` explicitly links to `analysis.ipynb`, `cycles.csv`,
  and `limits.toml`.
- Identifier (18): each of `cycle_index`, `capacity_mah`, and
  `internal_resistance_mohm` is intentionally shared by `analysis.ipynb`,
  `cycles.csv`, `limits.toml`, and `report.md`; each four-source component has
  six source pairs.
- Structural (1): the three-column table in `cycles.csv` and the
  `cycle_examples` record schema in `limits.toml` express the same record.
- Strict contradiction (2): `cutoff_voltage_v` is 2.80 in `report.md` versus
  2.70 in `limits.toml`; `rest_period_min` is 30 versus 45.
- Orphan (1): `todo.txt` is a city-bicycle note and supplies no battery-cycle
  relation.
- Unresolved (1): `thermal-appendix.md` is an explicit missing local path.

### Hard negatives and clarity

- The manufacturer URL is plainly external.
- `nominal_voltage_v = 3.60` versus 3.6 is a clean equivalent-number negative,
  not a contradiction under decimal-exact numeric comparison.
- The “approximately 2.8 volts” sentence is a clean approximate-versus-scalar
  negative.
- `todo.txt` supplies common tokens and prose-only one-token overlaps (`name`,
  `status`, `value`, `timestamp`, and the ordinary word “battery”) in an
  explicitly different subject.

The raw semantics are strong. There are, however, two upstream extraction
artifacts. First, the frozen CSV extraction reports four rows and one malformed
row although the fixture contains three actual records; the terminal blank line
became a null/malformed observation. Second, the Markdown extraction emits
`2.80`, `3.60`, and `2.8` as `ref_kind=path` reference units. Those three numeric
tokens are unambiguously *not* unresolved paths, but this is an unpreregistered
negative-candidate class that can directly affect the zero-false-positive
unresolved gate.

## Capacity

### Expected positives

- Literal (3): `overview.md` links to `capacity.xlsx`, `export.csv`, and
  `schema.xml`.
- Identifier (18): `node_count`, `per_node_mbps`, and `overhead_factor` are
  shared by the XLSX inputs, CSV columns, XML record children, and explicit
  prose in `overview.md`; three identifiers times six four-source pairs.
- Structural (1): `export.csv` and the repeated `/capacity/record` XML schema
  have the same three fields.
- Strict contradiction (2): `safety_margin_pct` is 12 in `overview.md` versus
  10 in `capacity.xlsx`; `rack_limit` is 16 in `overview.md` versus 20 in
  `schema.xml`.
- Orphan (1): `unused.md` is a wedding-seating sketch.
- Unresolved (1): `assumptions.md` is an explicit missing local path.

### Hard negatives and clarity

- The office-documentation URL is plainly external.
- `unused.md` explicitly says its `node_count` denotes diagram nodes rather
  than compute capacity. This is a strong same-spelling/different-domain and
  prose-only-overlap negative; its generic `id`, `name`, `status`, and `value`
  tokens are also clean common-token negatives.
- “About sixteen” is a clean approximate-versus-scalar negative.
- The XML record profiles are multi-valued. In particular, XML `node_count`
  spans 8 to 12 while the spreadsheet and one-row CSV expose scalar 8. These
  are clean multi-valued-profile/range-versus-scalar non-contradictions.
- The two XLSX outputs are correctly marked `derived=true` in the frozen
  extraction.

The claimed `derived-vs-declared` hard-negative class is not genuinely exercised
here. `raw_capacity_mbps` and `effective_capacity_mbps` occur only as formulas;
there is no same-named declared scalar in another source with which either can
form a candidate contradiction. Merely having formulas next to declared inputs
tests “ignore derived cells,” but not a derived-versus-declared comparison.

As in battery, `export.csv` has one actual data row while the frozen extraction
reports two rows, one malformed/null, due to a terminal blank line. The intended
field identity remains clear, but Stage 4 sees a noisier and partly nullable
schema than the raw fixture meaning warrants.

## Greenhouse

### Expected positives

- Literal (4): `protocol.md` links to `controller.py`, `readings.csv`,
  `readings.jsonl`, and `layout.json`.
- Identifier (40): `sample_id`, `soil_moisture_pct`, `valve_duty_pct`, and
  `zone_temperature_c` are each shared by five intended sources—Python symbols,
  JSON schema, CSV schema, JSONL schema, and explicit protocol prose. Each
  identifier therefore contributes ten source pairs.
- Structural (3): `layout.json`'s `readings` records, `readings.csv`, and
  `readings.jsonl` form a three-source same-record component, hence three pairs.
- Strict contradiction (2): `sample_interval_s` is 15 in the protocol versus 20
  in the layout; `watering_window_min` is 20 versus 25.
- Intended orphan (1): `old-notes.txt` is an eyepiece inventory.
- Intended unresolved (1): `calibration.md` is an explicit missing local path.

### Hard negatives and clarity

- Python import `statistics` is plainly external.
- The vendor URL is plainly external and includes both query and fragment, so
  the external-url and query-or-fragment cases are active.
- The approximate 18-minute sentence is a clean approximate-versus-scalar
  negative.
- The eyepiece note provides common-token, one-token (`Moisture`), and
  prose-only distractors in a clearly different domain.

The intended substring-basename negative is **not cleanly annotatable**. The
fixture says:

> Eyepiece records are exported to moisture-readings.csv, which is not readings.csv.

There are two independent problems:

1. `moisture-readings.csv` is not a literal identity match for `readings.csv`,
   so it is a valid substring-basename negative for *literal linkage*. But it is
   also an explicit, local-looking path to an absent file, introduced by “are
   exported to.” Under the corpus's ordinary unresolved-path semantics, that is
   a genuine additional unresolved reference. Excluding it requires an
   undocumented rule that references from an unrelated-domain/orphan source do
   not count.
2. The same sentence contains `readings.csv` as an exact standalone path, and
   the frozen extractor emits it as its own path reference unit. Whether that
   exact but negated mention is a literal link to the real source is a relation-
   polarity decision not specified in `policy.json`. A source-identity reading
   says the text identifies the real source while denying equality; a dependency
   reading says the negation forbids a link.

Consequently, the preregistered totals of one greenhouse unresolved reference
and four literal links are obtainable only by choosing one side of two unstated
semantic rules. This fixture also makes the intended orphan status of
`old-notes.txt` depend on the same unstated polarity decision.

## Parcel dispatch

### Expected positives

- Literal (3): `runbook.rst` links to `dispatch.py`,
  `current/parcels.tsv`, and `policy.toml`.
- Identifier (18): `parcel_id`, `route_bucket`, and `handoff_delay_ms` are
  shared by the current TSV, Python symbols, TOML example records, and explicit
  runbook prose; three identifiers times six four-source pairs.
- Structural (1): `current/parcels.tsv` and the TOML `parcel_examples` records
  have the same three field names.
- Strict contradiction (2): `retry_window_s` is 45 in the runbook versus 60 in
  policy; `max_batch_parcels` is 100 versus 120.
- Intended orphans (2): `archive/parcels.tsv` and `draft.json`.
- Unresolved (2): `handoff-map.csv` is absent, and the unqualified
  `parcels.tsv` is explicitly ambiguous between `archive/` and `current/`.

### Hard negatives and clarity

- The ambiguous-basename case is unusually strong: both colliding basenames
  exist and the prose calls out the ambiguity.
- Python import `pathlib` and the carrier API URL are clear external negatives.
- The archive and current TSVs are a clean same-width/different-fields
  structural negative (`id,name,status` versus the three live parcel fields).
- `handoff_timeout_s = 1` versus `handoff_timeout_ms = 1000` is a clear unit-name
  mismatch and must not be a strict contradiction.
- `draft.json` supplies generic fields, low field overlap, and sharply
  incompatible types for the two overlapping live names.

The domain status of `draft.json` is nevertheless too implicit for the number
of judgments it appears intended to carry. It lives inside `parcel_dispatch`,
is named `draft.json`, and natively declares `parcel_id` and `route_bucket`.
The value `name = "sourdough starter"` hints at a bakery domain, but unlike the
capacity and water distractors there is no sentence explicitly saying these
same-spelled fields have a different meaning. A malformed parcel draft is also
a reasonable reading. Under that reading, native-native identifier links for
`parcel_id` and `route_bucket` remain valid despite bad types, and the source is
not an orphan. The intended negative/orphan reading therefore relies on a
guess.

There is also a structural-control confound: the intended positive pair does
not have fully compatible inferred types. `route_bucket` is inferred as integer
in the TSV but is quoted string in TOML. Thus “incompatible types” cannot by
itself distinguish the draft negative from the intended structural positive;
the decision also depends on overlap and domain.

Both TSVs have a terminal blank line that the frozen extraction counts as a
malformed null row, adding another upstream nullable-schema artifact.

## Telescope

### Expected positives

- Literal (5): `methods.tex` names `reduce.ipynb`, `frames.csv`,
  `frames.jsonl`, and `instrument.yaml`, and `\ref{tab:frames}` resolves to the
  label declared in `appendix.tex`.
- Identifier (45): `frame_id`, `sky_level_adu`, and `focus_offset_um` are shared
  by six intended sources—appendix prose, CSV, JSONL, YAML, methods prose, and
  notebook—so each identifier contributes 15 source pairs.
- Structural (3): CSV, JSONL, and the YAML `frames` records form a three-source
  same-record component.
- Strict contradiction (2): `exposure_time_s` is 30 in methods versus 25 in
  instrument settings; `gain_e_per_adu` is 1.7 versus 1.8.
- Orphan (1): `old-memo.txt` is an orchestra memo.
- Unresolved (2): `fig:future-flat` has no local label declaration and
  `ortega2025` has no local citation-key declaration.

### Hard negatives and clarity

- “About zero micrometres” is a clean approximate-versus-scalar negative.
- The orchestra memo supplies clean common-token and one-token (`Focus`)
  distractors in an explicitly non-astronomical domain.
- The CSV/JSONL/YAML value profiles differ while their schemas agree; those
  are clear multi-valued-profile non-contradictions.
- The DOI is textually and explicitly external.

There is an extraction-layer coverage weakness. The LaTeX extractor materializes
the label and citation references, but none of the four plain local filenames
and not the DOI URL as `reference` units; they survive only inside one prose
unit. Therefore the local-path positives and external-DOI negative are active
only for implementations that rescan prose. A reference-unit-only implementation
cannot make the DOI mistake and cannot recover the four path links, so the DOI
hard-negative claim is not independently exercised at the native-reference
layer.

The external DOI does not itself declare or map the key `ortega2025`, so I read
that key as unresolved. If the intended adjudication treats the nearby DOI as
an external resolution of the citation key, that mapping rule also needs to be
stated; it is not syntactically present.

Finally, `frames.csv` has three actual records but the frozen extraction reports
four rows and one malformed/null row because of the terminal blank line.

## Water billing

### Expected positives

- Literal (3): `spec.html` links to `tariff.py`, `meters.jsonl`, and
  `tariff.json`.
- Identifier (18): `account_ref`, `meter_reading_l`, and `tariff_band` are
  shared by JSONL, JSON record schema, Python symbols, and explicit HTML prose;
  three identifiers times six four-source pairs.
- Structural (1): the JSONL meter records and JSON `meters` records have the
  same three-field schema.
- Strict contradiction (2): `grace_period_days` is 7 in HTML versus 10 in JSON;
  `late_fee_pct` is 2 versus 3.
- Orphan (1): `legacy.md` is a pottery-kiln ledger.
- Unresolved (1): `audit-notes.md` is an explicit missing local path.

### Hard negatives and clarity

- The regulator URL is plainly external and includes a fragment.
- “About a week” is a clean approximate-versus-scalar negative relative to the
  exact seven-day assignment.
- `legacy.md` explicitly says its `account_ref` identifies a ceramics customer,
  not a water meter. This is the cleanest same-spelling/different-domain and
  prose-only-overlap example in the corpus; its other names are common-token
  negatives.
- Meter value ranges across JSON and JSONL are multi-valued profiles, not
  strict contradictory constants.

This is the cleanest collection. Its only notable extraction artifact is one
terminal blank JSONL line recorded as a skipped-line gap; unlike the affected
CSV/TSV files, it does not create a phantom schema row.

## Preregistered hard-negative coverage audit

### Literal

| class | evidence | verdict |
| --- | --- | --- |
| ambiguous-basename | parcel `parcels.tsv` with two real candidates | Strong and unambiguous. |
| external-import | greenhouse `statistics`; parcel `pathlib` | Strong and addressable as import reference units. |
| external-url | explicit external URLs in five collections plus telescope DOI prose | Strong for ordinary URLs. |
| query-or-fragment | greenhouse query+fragment URL; water fragment URL | Strong and unambiguous. |
| substring-basename | greenhouse `moisture-readings.csv` versus `readings.csv` | **Not clean**: the absent long path is itself a valid unresolved reference, and the exact basename also appears as a negated standalone reference. |

### Identifier

| class | evidence | verdict |
| --- | --- | --- |
| common-token | distractors repeatedly use `id`, `name`, `status`, `value`, `result`, and `timestamp` | Strong. |
| namespace-collision | generic `id`/`status` across parcel archive/draft are the nearest case | **Weak/not isolated**: no explicit namespace-bearing same name with a clear domain boundary; this overlaps common-token and different-domain cases. |
| one-token-overlap | ordinary “battery,” “moisture,” and “focus” prose versus compound domain identifiers | Present, but mostly confounded with prose-only distractors. |
| prose-only-overlap | capacity `node_count`; water `account_ref`; several ordinary-word distractors | Strong; capacity and water explicitly state the different meaning. |
| same-spelling-different-domain | capacity `node_count`; water `account_ref` | Strong and unambiguous. Parcel draft is not needed to establish this class. |

### Structural

| class | evidence | verdict |
| --- | --- | --- |
| generic-fields | parcel archive/draft and other distractors | Strong. |
| incompatible-types | parcel draft versus live parcel schemas | Present but confounded: the intended positive TSV/TOML pair also disagrees on `route_bucket` type. |
| low-overlap | parcel draft versus the live three-field record | Strong as a field-set negative. |
| same-width-different-fields | parcel archive versus current TSV | Strong and unambiguous. |

### Contradiction

| class | evidence | verdict |
| --- | --- | --- |
| approximate-vs-scalar | explicit “approximately/about” prose in all six collections | Strong. |
| derived-vs-declared | capacity XLSX formulas | **Not genuinely represented as a pair**: no same-named declared scalar exists outside either formula. |
| equivalent-number | battery 3.60 versus 3.6 | Strong and unambiguous. |
| multi-valued-profile | shared record fields with differing value profiles, especially greenhouse/telescope | Strong. |
| namespace-collision | parcel generic `status` values are the nearest candidate | **Weak/not isolated** and entangled with generic-field/different-domain judgment. |
| range-vs-scalar | capacity XML ranges versus XLSX/CSV scalar observations | Strong. |
| unit-name-mismatch | parcel `handoff_timeout_s` versus `handoff_timeout_ms` | Strong and unambiguous. |

### Unresolved

| class | evidence | verdict |
| --- | --- | --- |
| external-doi | telescope DOI in methods prose | Semantically clear, but only prose-resident after extraction; inert for native-reference-only candidate generation. |
| external-import | greenhouse and parcel imports | Strong. |
| external-url | explicit URL reference units in Markdown/RST/HTML | Strong. |
| resolved-local | all 21 intended literal positives | Strong across six groups. |

## Cross-cutting risks

1. **Relation exclusivity is undocumented.** The exact identifier total of 157
   counts only the fixture-declared shared field groups. It excludes 13 obvious
   same-name source pairs used for strict/equivalent/unit facts: three in
   battery and two in each other collection. If identifier linkage and
   contradiction are independent evidence types, names such as
   `cutoff_voltage_v` still form identifier links even when their values
   contradict. Reproducing 157 therefore implies an exclusive-role or
   precedence rule that `policy.json` does not state.
2. **Reference polarity/domain filtering is undocumented.** The greenhouse
   negated exact path and unrelated-domain missing path produce reasonable gold
   interpretations different from the preregistered totals.
3. **Two hard-negative claims are not genuinely isolated.**
   `derived-vs-declared` has no candidate pair, while identifier/contradiction
   `namespace-collision` has no clear namespace-controlled example.
4. **One hard-negative claim is extraction-inert on the obvious path.** The
   telescope external DOI is not a reference unit.
5. **The parcel draft is multiply ambiguous.** A single understated domain cue
   controls identifier negatives, structural negatives, and orphan status.
6. **Frozen Stage 1 artifacts leak into the Stage 4 task.** Five CSV/TSV
   fixtures gain phantom null/malformed rows from terminal blank lines, one
   JSONL fixture reports a skipped blank line, and battery decimal literals are
   emitted as path references. These are legitimate robustness stressors only
   if deliberately acknowledged; currently they are outside the
   preregistered hard-negative taxonomy.
7. **Template breadth exceeds semantic breadth.** Format coverage is good
   (notebook, CSV/TSV, TOML, XLSX, XML, JSON/JSONL, YAML, Python, Markdown, RST,
   LaTeX, HTML), but every collection follows nearly the same hub-document +
   shared-schema + two-constant-conflicts + distractor pattern. That is
   acceptable for the stated engineering gate, not evidence of production
   prevalence or general fusion quality.

## Corpus fitness verdict

The corpus is quantitatively coherent and valuable. Its intended positive
support reconstructs exactly; every role appears in every collection; literal,
identifier, structural, contradiction, orphan, and unresolved behaviors are
all exercised; and the fixtures connect Stage 4 to real Stage 1 representations
rather than bypassing extraction.

I would use it immediately as a diagnostic and implementation stress suite. I
would **not use the frozen scores alone for the decisive Stage 4
ship/preregistered-subtype/disable disposition** until the following are
independently adjudicated and written down:

1. whether contradiction-bearing fact names can also be identifier links;
2. whether negated exact path mentions link, and whether absent paths in an
   unrelated/orphan source count as unresolved;
3. whether `draft.json` is explicitly a different domain;
4. whether the claimed derived-versus-declared and namespace-collision negative
   classes need real replacement examples; and
5. whether prose-only DOI coverage and the frozen extraction artifacts are
   intentional parts of the gate.

If those decisions preserve the current gold without changing fixtures, the
policy needs operational definitions that make that gold reproducible. If they
change support or hard-negative coverage, this should be a corrected re-freeze,
not post-score tuning. Until then, a gate failure could reflect corpus
adjudication choices rather than a fusion capability failure, and a pass would
not substantiate every preregistered hard-negative claim.

## Blind attestation

For this review I did **not** open, read, search, parse, or otherwise inspect
`benchmarks/fusion/scored/labels.jsonl`, any `annotations.jsonl`, any content
under `predictions/` or `reports/`, or reviewer A's review. I did not call or
import `analyze`, `run_signals`, `fuse`, or evaluator run/report code. I did not
run model inference. I consulted only the allowed policy/freeze/manifests, raw
scored fixtures, and their frozen extraction records. I made no changes to the
corpus, labels, policy, production code, or tests; this review file is the sole
edit made for the audit.
