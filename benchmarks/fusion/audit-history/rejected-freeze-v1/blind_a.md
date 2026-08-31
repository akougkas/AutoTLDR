# Stage 4 fusion evaluation: blind review A

Date: 2026-08-30

## Scope and review rules

This is an independent, source-first review of the frozen six-collection scored
corpus. I inspected the frozen policy and manifests, then reconciled the raw
fixtures against the extracted units. I treated raw source meaning as primary
and used extraction output only to identify representation artifacts that could
affect scoring.

The review uses the following interpretations:

- A literal local resolution requires an explicit, non-negated reference with
  exactly one defensible source in the same collection. An external URL, module,
  or bibliographic citation is not silently converted into a local edge.
- An ambiguous basename is unresolved even when one candidate is more recent or
  looks more likely. Recency is not evidence of identity.
- Identifier equivalence requires the same domain concept, not merely the same
  spelling. Generic names and exact spellings planted in an unrelated domain are
  hard negatives.
- A native-container correspondence requires two actual record/table containers
  describing the same entity shape. Shared generic fields alone are insufficient.
- A strict scalar contradiction requires the same named concept, comparable
  units, exact declarative values, and unequal values. Approximate prose,
  observed samples, thresholds, derived formulas, and differently named or
  differently scaled quantities are not contradictions.
- “No accepted relation” means no positive inter-source relation. A source may
  still contain an unresolved mention or an intentionally misleading token.

## Corpus-wide expected relation shape

The intended central schema groups, native-container pairs, strict
contradictions, and isolated sources can be reconstructed consistently from the
raw fixtures:

| Collection | Central identifier support | Native-container pairs | Strict contradictions | Sources with no accepted positive relation |
| --- | ---: | ---: | ---: | ---: |
| Battery | 18 | 1 | 2 | 1 |
| Capacity | 18 | 1 | 2 | 1 |
| Greenhouse | 40 | 3 | 2 | 1 |
| Parcel dispatch | 18 | 1 | 2 | 2 |
| Telescope | 45 | 3 | 2 | 1 |
| Water billing | 18 | 1 | 2 | 1 |
| **Total** | **157** | **10** | **12** | **7** |

Identifier support in this table is pairwise closure within each listed central
equivalence group. For example, one identifier present in four sources provides
six positive source pairs. These totals exactly explain the policy's frozen
support counts for identifier, structural, contradiction, and orphan relations.

The literal-reference total is less settled. The 21 frozen positives are exactly
explained by references originating in each collection's primary narrative
document: 3 + 3 + 4 + 3 + 5 + 3. Raw source truth also contains two clear,
non-negated notebook-to-data references, making 23 defensible local resolutions.
Those two additional cases are called out below. A “primary document only” rule
would explain 21, but that source-selection rule is not intrinsic to literal
reference semantics and should be explicit if it is intentional.

## Collection 1: battery

### Literal references

Expected local resolutions from raw source truth:

- `report.md` -> `analysis.ipynb`
- `report.md` -> `cycles.csv`
- `report.md` -> `limits.toml`
- `analysis.ipynb` -> `cycles.csv`

The first three explain the collection's apparent primary-document contribution
to the frozen support count. The fourth is equally literal in the notebook's
markdown (“Read cycles.csv ...”) and is not negated or ambiguous.

Expected unresolved or non-local references:

- `report.md` -> `thermal-appendix.md`: missing local target.
- `https://cells.example/specs/21700`: external URL, not a local edge.

The extracted prose also exposes `2.80`, `3.60`, and `2.8` as path/reference-like
tokens. In context they are voltage values, not filenames or references. They
must not become either literal positives or unresolved-reference positives.

### Canonical identifier groups

The central identifier groups are:

- `cycle.index`: `analysis.ipynb`, `cycles.csv`, `limits.toml`, `report.md`
- `capacity.mah`: `analysis.ipynb`, `cycles.csv`, `limits.toml`, `report.md`
- `internal.resistance.mohm`: `analysis.ipynb`, `cycles.csv`, `limits.toml`,
  `report.md`

The dotted names above denote canonical concepts. Their fixture spellings are
the corresponding snake-case names such as `cycle_index` and `capacity_mah`.

Repeated scalar fact names (`cutoff_voltage_v`, `rest_period_min`, and
`nominal_voltage_v`) are also semantically aligned between `report.md` and
`limits.toml`, but they serve the scalar fact/contradiction portion of this
fixture rather than the central schema-normalization group. Whether such fact
keys also receive identifier edges needs an explicit policy rule; counting them
would increase identifier support beyond 157.

### Native-container correspondence

The true correspondence is:

- `cycles.csv` table <-> `limits.toml` `cycle_examples` record collection

The exact discriminative fields are:

- `cycle_index`
- `capacity_mah`
- `internal_resistance_mohm`

The CSV's trailing blank line is not a fourth domain record and should not be
used as structural or count evidence.

### Strict contradictions and hard-negative non-conflicts

Strict contradictions:

- `cutoff_voltage_v`: `report.md` declares `2.80`; `limits.toml` declares
  `2.70`.
- `rest_period_min`: `report.md` declares `30`; `limits.toml` declares `45`.

Required non-conflicts:

- `nominal_voltage_v` is `3.60` versus `3.6`, which is numerically equal.
- “about 2.8” is approximate prose and must not create a second contradiction.
- Measured cycle values and the TOML example records are observations/examples,
  not competing declarations of the cut-off, rest period, or nominal voltage.
- The extractor's blank CSV row and resulting observed row count are artifacts,
  not declared-count contradictions.

### No-relation source and risks

`todo.txt` is an off-domain city-bicycle task list. Its generic `name`, `status`,
`value`, and `timestamp` language has no accepted identifier, structural, or
contradiction relation to the battery sources.

Primary label risks are the notebook's omitted literal edge, numeric prose being
misread as paths, and the trailing-blank-row artifact.

## Collection 2: capacity planning

### Literal references

Expected local resolutions:

- `overview.md` -> `capacity.xlsx`
- `overview.md` -> `export.csv`
- `overview.md` -> `schema.xml`

Expected unresolved or non-local references:

- `overview.md` -> `assumptions.md`: missing local target.
- The office/specification URL in `overview.md`: external URL, not a local edge.

### Canonical identifier groups

The central identifier groups are:

- `node.count`: `capacity.xlsx`, `export.csv`, `overview.md`, `schema.xml`
- `per.node.mbps`: `capacity.xlsx`, `export.csv`, `overview.md`, `schema.xml`
- `overhead.factor`: `capacity.xlsx`, `export.csv`, `overview.md`, `schema.xml`

The workbook's immediate-left labels confirm that its cells mean
`node_count`, `per_node_mbps`, and `overhead_factor`; these are not guesses based
only on cell position. The central exact values in the workbook/export are 8,
450, and 0.92. The XML contains records with those same semantic fields.

The exact token `node_count` in `unused.md` is not equivalent: it refers to
wedding seating in another domain. `safety_margin_pct` and `rack_limit` align
across the relevant sources as scalar fact concepts, but, as in the other
collections, including scalar fact-name edges in identifier scoring would
exceed the policy's 157 central-group support.

### Native-container correspondence

The true correspondence is:

- `export.csv` table <-> `schema.xml` `/capacity/record` collection

The exact discriminative logical leaf fields are:

- `node_count`
- `per_node_mbps`
- `overhead_factor`

XML wrapper nodes and `#text` extraction paths must not obscure that these leaf
fields describe the same native records. The CSV trailing blank line is not a
second data record.

### Strict contradictions and hard-negative non-conflicts

Strict contradictions:

- `safety_margin_pct`: `overview.md` declares `12`; `capacity.xlsx` declares
  `10`.
- `rack_limit`: `overview.md` declares `16`; `schema.xml` declares `20`.

Required non-conflicts:

- Workbook and CSV declarations of 8 nodes, 450 Mbps per node, and 0.92 overhead
  agree.
- The XML's multiple record values are a profile/data series, not mutually
  contradictory global constants.
- Derived workbook formula results are not new independent declarations of the
  input constants.
- “approximately sixteen” is not an additional exact scalar assertion.
- The wedding `node_count` in `unused.md` is a cross-domain collision, not a
  disagreement about capacity planning.

### No-relation source and risks

`unused.md` is an off-domain wedding-seating document and has no accepted
relation to this collection despite its planted `node_count` token.

Primary risks are XML leaf/wrapper representation, the workbook values being
extracted as record facts rather than ordinary prose/constants, the blank CSV
row, and the exact cross-domain identifier collision.

## Collection 3: greenhouse controller

### Literal references

Expected local resolutions:

- `protocol.md` -> `controller.py`
- `protocol.md` -> `readings.csv`
- `protocol.md` -> `readings.jsonl`
- `protocol.md` -> `layout.json`

Expected unresolved or non-local references:

- `protocol.md` -> `calibration.md`: missing local target.
- The vendor URL, including its query and fragment: external URL, not a local
  edge.
- `statistics` imported by `controller.py`: external/standard-library module,
  not a source in this collection.

`old-notes.txt` says that eyepiece records are exported to
`moisture-readings.csv`, “which is not readings.csv.” This sentence is a
deliberate hard case:

- `old-notes.txt` -> `readings.csv` must **not** resolve because the mention is
  explicitly negated.
- `moisture-readings.csv` is syntactically a positive filename mention but has
  no local target. A purely syntactic unresolved-reference policy would count
  it. An off-domain/adversarial-source exclusion would not. The frozen
  unresolved total of eight is explainable only when this missing filename is
  excluded, so that exclusion needs to be stated rather than inferred after
  scoring.

### Canonical identifier groups

The central identifier groups are:

- `sample.id`: `controller.py`, `layout.json`, `protocol.md`, `readings.csv`,
  `readings.jsonl`
- `soil.moisture.pct`: `controller.py`, `layout.json`, `protocol.md`,
  `readings.csv`, `readings.jsonl`
- `valve.duty.pct`: `controller.py`, `layout.json`, `protocol.md`,
  `readings.csv`, `readings.jsonl`
- `zone.temperature.c`: `controller.py`, `layout.json`, `protocol.md`,
  `readings.csv`, `readings.jsonl`

`sample_interval_s` and `watering_window_min` align as scalar declaration keys
between `protocol.md` and `layout.json`; they are the contradiction concepts,
not part of the central 40-pair identifier support.

The telescope-oriented `id`/`status` fields and filename language in
`old-notes.txt` are not greenhouse identifiers.

### Native-container correspondences

There are three true pairwise correspondences among:

- `layout.json` `readings` record collection
- `readings.csv` table
- `readings.jsonl` record stream

The exact discriminative fields for every pair are:

- `sample_id`
- `soil_moisture_pct`
- `valve_duty_pct`
- `zone_temperature_c`

Thus three native containers produce three positive structural pairs, not one
three-way annotation and not additional links to prose or function signatures.

### Strict contradictions and hard-negative non-conflicts

Strict contradictions:

- `sample_interval_s`: `protocol.md` declares `15`; `layout.json` declares
  `20`.
- `watering_window_min`: `protocol.md` declares `20`; `layout.json` declares
  `25`.

Required non-conflicts:

- “about 18” is approximate prose and must not become an exact third sample
  interval.
- Individual moisture, valve, and temperature readings are observations, not
  competing global configuration declarations.
- Differing values across records are a time/sample series, not pairwise
  contradictions.
- The telescope content in `old-notes.txt` is a namespace/domain hard negative.

### No-relation source and risks

`old-notes.txt` must have no accepted positive relation to the greenhouse
sources. Its negated exact basename is not a link. The status of its positive
but absent `moisture-readings.csv` mention is the largest label-policy ambiguity
in this collection: it can be a valid unresolved reference while the source
remains an orphan with respect to positive inter-source relations.

Other risks are substring/basename matching that ignores negation and treating
generic off-domain fields as identifiers.

## Collection 4: parcel dispatch

### Literal references

Expected local resolutions:

- `runbook.rst` -> `dispatch.py`
- `runbook.rst` -> `current/parcels.tsv`
- `runbook.rst` -> `policy.toml`

Expected ambiguous, unresolved, or non-local references:

- Bare `parcels.tsv` is ambiguous between `current/parcels.tsv` and
  `archive/parcels.tsv`; it must not resolve to either one.
- `handoff-map.csv` is missing.
- The carrier URL is external.
- `pathlib` imported by `dispatch.py` is an external/standard-library module,
  not a local source.

The ambiguity and the missing file account for two unresolved cases; choosing
the current file based on directory name or apparent freshness would leak a
domain preference into literal resolution.

### Canonical identifier groups

The central identifier groups are:

- `parcel.id`: `current/parcels.tsv`, `dispatch.py`, `policy.toml`,
  `runbook.rst`
- `route.bucket`: `current/parcels.tsv`, `dispatch.py`, `policy.toml`,
  `runbook.rst`
- `handoff.delay.ms`: `current/parcels.tsv`, `dispatch.py`, `policy.toml`,
  `runbook.rst`

The exact `parcel_id` and `route_bucket` spellings in `draft.json` describe
sourdough planning, not parcel dispatch. They are hard negatives even though
string normalization alone would make them look ideal. The generic `id`,
`name`, and `status` fields in `archive/parcels.tsv` are likewise insufficient.

`retry_interval_s` and `max_attempts` are aligned scalar declaration keys
between `runbook.rst` and `policy.toml`, but are reserved for strict
contradiction evaluation in the support accounting described above.

### Native-container correspondence

The true correspondence is:

- `current/parcels.tsv` table <-> `policy.toml` `parcel_examples` records

The exact discriminative fields are:

- `parcel_id`
- `route_bucket`
- `handoff_delay_ms`

This is a semantic correspondence despite `route_bucket` being represented as
an integer in the TSV and a string in the TOML examples. That type mismatch is
useful disagreement evidence, but a blanket “incompatible types means hard
negative” rule would incorrectly erase an otherwise exact three-field native
container match. The intended precedence between field-set agreement and type
compatibility should be explicit.

Neither `draft.json` nor `archive/parcels.tsv` is structurally equivalent to the
current parcel table or policy examples. Exact planted names in the sourdough
object do not establish entity identity.

### Strict contradictions and hard-negative non-conflicts

Strict contradictions:

- `retry_interval_s`: `runbook.rst` declares `45`; `policy.toml` declares `60`.
- `max_attempts`: `runbook.rst` declares `100`; `policy.toml` declares `120`.

Required non-conflicts:

- `handoff_timeout_s = 1` and `handoff_timeout_ms = 1000` use different
  identifiers and units; after unit conversion they agree, so they are not a
  strict contradiction.
- Observed/example handoff delays are not contradictions with a timeout.
- Per-record parcel values are not global declarations.
- Sourdough `parcel_id` and Boolean `route_bucket` values are cross-domain/type
  traps, not parcel-policy disagreements.
- Trailing blank TSV rows are extraction artifacts, not declared count facts.

### No-relation sources and risks

Both `archive/parcels.tsv` and `draft.json` have no accepted positive relation.
The archive file is merely one candidate for an intentionally ambiguous
basename; ambiguity does not create a link. The draft is an explicit
same-spelling/wrong-domain adversary.

Primary risks are accidental recency-based disambiguation, cross-domain exact
name matching, the native-container type mismatch, unit-aware non-conflict, and
blank-row counts.

## Collection 5: telescope reduction

### Literal references

Expected local resolutions from raw source truth:

- `methods.tex` -> `reduce.ipynb`
- `methods.tex` -> `frames.csv`
- `methods.tex` -> `frames.jsonl`
- `methods.tex` -> `instrument.yaml`
- `methods.tex` label `tab:frames` -> the definition in `appendix.tex`
- `reduce.ipynb` -> `frames.csv`

The first five exactly explain this collection's apparent contribution to the
21 primary-document positives. The sixth is a clear notebook markdown
instruction (“Load frames.csv ...”) and is the second defensible local edge not
accounted for by that total.

Expected unresolved or non-local references:

- `methods.tex` label `fig:future-flat`: no definition in the collection.
- Citation key `ortega2025`: no local bibliographic target.
- The DOI in `methods.tex`: external identifier/URL, not a local source edge.

Raw LaTeX contains the four filenames, but its extracted representation folds
them into prose rather than dedicated reference units. The notebook filename is
similarly present in markdown/prose rather than a reference unit. Literal truth
must not depend on whether a format-specific extractor happened to assign a
reference modality.

### Canonical identifier groups

The central identifier groups are:

- `frame.id`: `appendix.tex`, `frames.csv`, `frames.jsonl`, `instrument.yaml`,
  `methods.tex`, `reduce.ipynb`
- `sky.level.adu`: `appendix.tex`, `frames.csv`, `frames.jsonl`,
  `instrument.yaml`, `methods.tex`, `reduce.ipynb`
- `focus.offset.um`: `appendix.tex`, `frames.csv`, `frames.jsonl`,
  `instrument.yaml`, `methods.tex`, `reduce.ipynb`

`exposure_s` and `gain_e_per_adu` align as scalar declaration concepts between
`methods.tex` and `instrument.yaml`, but they are the contradiction keys rather
than members of the central 45-pair identifier support.

The orchestra-oriented `id`, `status`, and `focus` in `old-memo.txt` are not
telescope identifiers. In particular, generic `focus` must not be broadened to
the unit-qualified `focus_offset_um` merely because both are plausible words in
other contexts.

### Native-container correspondences

There are three true pairwise correspondences among:

- `frames.csv` table
- `frames.jsonl` record stream
- `instrument.yaml` frame records

The exact discriminative fields for every pair are:

- `frame_id`
- `sky_level_adu`
- `focus_offset_um`

The matching table in `appendix.tex`, notebook output, and prose in
`methods.tex` support identifier recovery but are not additional native
container pairs under the frozen native-native structural policy.

### Strict contradictions and hard-negative non-conflicts

Strict contradictions:

- `exposure_s`: `methods.tex` declares `30`; `instrument.yaml` declares `25`.
- `gain_e_per_adu`: `methods.tex` declares `1.7`; `instrument.yaml` declares
  `1.8`.

Required non-conflicts:

- Approximate focus language is not an exact declaration.
- Frame-level sky/focus values form observations, not contradictory global
  constants.
- Differing records and trailing blank CSV rows are not declaration conflicts.
- The off-domain orchestra memo is not a source of telescope disagreements.

### No-relation source and risks

`old-memo.txt` has no accepted positive relation to the collection.

Primary risks are format-dependent reference-unit creation for LaTeX and
notebook markdown, distinguishing a missing label from a missing citation,
keeping the DOI external, generic-domain collisions, and blank-row artifacts.

## Collection 6: water billing

### Literal references

Expected local resolutions:

- `spec.html` -> `tariff.py`
- `spec.html` -> `meters.jsonl`
- `spec.html` -> `tariff.json`

Expected unresolved or non-local references:

- `spec.html` -> `audit-notes.md`: missing local target.
- The regulator URL: external URL, not a local edge.

### Canonical identifier groups

The central identifier groups are:

- `account.ref`: `meters.jsonl`, `spec.html`, `tariff.json`, `tariff.py`
- `meter.reading.l`: `meters.jsonl`, `spec.html`, `tariff.json`, `tariff.py`
- `tariff.band`: `meters.jsonl`, `spec.html`, `tariff.json`, `tariff.py`

The exact `account_ref` spelling in `legacy.md` belongs to pottery accounting,
not utility billing, and must remain a hard negative.

`grace_period_days` and `late_fee_pct` align as scalar declaration concepts
between `spec.html` and `tariff.json`; they are used by the contradiction axis
rather than the central 18-pair identifier support.

### Native-container correspondence

The true correspondence is:

- `meters.jsonl` record stream <-> `tariff.json` `meters` record collection

The exact discriminative fields are:

- `account_ref`
- `meter_reading_l`
- `tariff_band`

The Python signatures and HTML prose establish identifier use but are not
native record containers for structural pair scoring.

### Strict contradictions and hard-negative non-conflicts

Strict contradictions:

- `grace_period_days`: `spec.html` declares `7`; `tariff.json` declares `10`.
- `late_fee_pct`: `spec.html` declares `2`; `tariff.json` declares `3`.

The HTML declarations appear inside a `<pre>` block and are extracted with code
modality. They remain exact declarations in source meaning; a contradiction
implementation restricted to prose/constants would miss them for a modality
reason unrelated to correctness.

Required non-conflicts:

- “about a week” is approximate and must not add a grace-period contradiction.
- Meter records and tariff profiles are observations/configured examples, not
  competing global declarations.
- Threshold logic inside `tariff.py` is procedural behavior, not an exact
  restatement of either scalar declaration unless the same constant is actually
  declared.
- Pottery `account_ref` in `legacy.md` is a domain collision, not a billing
  conflict.

### No-relation source and risks

`legacy.md` has no accepted positive relation despite its exact planted
`account_ref` spelling.

Primary risks are declaration semantics hidden behind HTML code modality and
exact same-name/wrong-domain matching.

## Ambiguity and label-risk findings

### 1. Literal support needs an explicit source-eligibility rule

Raw truth provides at least 23 unambiguous local resolutions. The frozen count
of 21 is reproduced only by accepting references from the six primary narrative
documents while excluding:

- `analysis.ipynb` -> `cycles.csv`
- `reduce.ipynb` -> `frames.csv`

A dedicated-reference-unit-only convention does not resolve the issue: several
valid LaTeX filename references are also represented only as prose. The corpus
therefore needs one explicit convention: either literal references are judged
from raw source meaning, in which case the two notebook edges belong in a future
version, or eligibility is restricted by source/annotation scope, in which case
that restriction must be written into policy and the metric named accordingly.

Because policy and labels are frozen, this should be handled through a versioned
corpus/policy supersession if correction is necessary, not through post-result
tuning.

### 2. The greenhouse adversary mixes negation, missingness, and orphanhood

`old-notes.txt` contains both a negated exact existing basename and a positive
missing basename. The existing `readings.csv` must not resolve. The absent
`moisture-readings.csv` is a defensible unresolved mention under ordinary
literal semantics. Excluding the entire off-domain source is also defensible as
a benchmark design choice, but only if the policy states that unresolved
references are scoped to relation-eligible sources. Orphanhood alone does not
logically exclude an unresolved mention, because unresolved mentions create no
positive inter-source edge.

### 3. Identifier scoring mostly tests cross-modality recovery, not normalization

The 157-pair total is internally coherent, but the central fields use the same
snake-case names across nearly every source. This is strong coverage for
recovering identifiers from prose, code, tables, spreadsheets, XML, TOML, YAML,
JSON, and notebooks. It is weak coverage for harder canonicalization such as
camelCase versus snake_case, abbreviations, true aliases, or renamed concepts.
Claims about “identifier normalization” should be limited accordingly.

There is also an implicit distinction between central schema identifiers and
repeated scalar declaration keys. That distinction explains the support count,
but it should be documented so two semantically equivalent fact keys are not
arbitrarily treated as non-equivalent on the identifier axis.

### 4. Structural gold is coherent but representation-sensitive

The ten native-container pairs are well motivated and have exact discriminative
field sets. Three representation details can nevertheless create false
negatives:

- XML wrapper/`#text` paths versus logical leaf fields in capacity planning.
- A `route_bucket` type mismatch in the otherwise exact parcel container pair.
- Native container versus rendered table/notebook-output boundaries in the
  telescope collection.

The policy should make precedence clear where field-set similarity and type
compatibility disagree.

### 5. Contradiction gold is strong, but modality and subtype coverage are narrow

All twelve strict contradiction pairs are source-grounded and have useful
hard-negative neighbors: approximate values, numerically equal formatting,
unit-equivalent values, sample/profile values, and cross-domain collisions.
However, exact declarations can surface as spreadsheet record facts, XML text
leaves, or HTML code blocks. Gating by extractor modality instead of declaration
semantics would measure formatting accidents.

The corpus clearly covers constant-versus-constant contradictions. It does not
provide a clean, source-truth example of a declared-count-versus-observed-count
contradiction; trailing blank-row extraction artifacts must not be repurposed as
one. Any claim that the frozen score validates that subtype would be too broad.

### 6. Extraction artifacts must stay outside gold semantics

Observed artifacts include numeric values emitted as path-like references and
trailing blank delimited rows counted as malformed or additional records. They
are valuable robustness probes, but labels should follow source meaning. They
must not silently create unresolved references, structural records, or
declared-count contradictions.

### 7. Relation-type coverage is not exhaustive

The corpus has external URLs and a DOI, but no clear positive local
URL-to-source-identity case. It has a positive LaTeX label resolution and a
missing citation, but little breadth for label/citation syntax. The frozen
results can gate the represented cases; they should not be treated as evidence
that every allowed literal subtype is solved.

## Fitness verdict

**Verdict: conditionally fit as a synthetic engineering gate, not yet fit as an
unqualified measurement of fusion quality.**

The corpus is thoughtfully adversarial and unusually good at testing the
project's real failure modes: same-name/wrong-domain identifiers, ambiguous
basenames, negation, approximate versus exact facts, equal values with different
formatting, units, native container boundaries, and sources that should remain
unconnected. Its identifier (157), structural (10), contradiction (12), orphan
(7), and intended unresolved (8) support can all be reconstructed independently
from raw fixtures. That makes it useful for a frozen Stage 4 go/no-go gate.

Before interpreting scores, the review owner should audit the frozen labels and
policy against three source-truth questions without looking at model outputs:

1. Are notebook-origin literal references eligible? If not, where is the
   primary-document-only rule stated?
2. Is `moisture-readings.csv` an unresolved mention even though it occurs in an
   off-domain orphan source?
3. Are scalar declaration-key equivalences intentionally excluded from the
   identifier relation, and are representation/type rules explicit for XML,
   spreadsheets, HTML `<pre>`, and the parcel structural pair?

If the frozen annotations already encode consistent, documented answers, the
corpus is suitable for the engineering decision it was designed to support. If
they do not, publish a versioned corrected corpus and rerun all arms unchanged.
Do not repair labels or thresholds in response to predictions. Even after those
questions are resolved, this remains a small synthetic diagnostic set, not a
prevalence estimate or evidence of broad real-world generalization.

## Blinding attestation

For this review I inspected only the allowed frozen policy/freeze metadata,
scored collection/source/extraction manifests, raw scored fixtures, and raw XLSX
ZIP/XML content needed to verify workbook labels and values.

I did **not**:

- open, read, search, or otherwise inspect
  `benchmarks/fusion/scored/labels.jsonl`;
- open, read, search, or otherwise inspect any `annotations.jsonl`;
- open, read, search, or otherwise inspect anything under any `predictions/`
  directory;
- open, read, search, or otherwise inspect anything under any `reports/`
  directory;
- call or import `analyze`, `run_signals`, or `fuse`;
- run or import the evaluator, or invoke any evaluator run/report action;
- inspect corpus-builder or evaluator implementation as a proxy for the hidden
  annotations; or
- edit the corpus, labels, policy, production code, or tests.

This review file is the only file I created or edited.
