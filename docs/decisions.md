# Decision log

Append-only. Each entry records what was decided, why, what was rejected, and
what would justify revisiting. The point is that nobody has to relitigate a
settled question, and that when someone does reopen one, they start from the
reasoning rather than from scratch.

Add new entries at the top. Never edit a past entry; supersede it with a new one
that names the entry it replaces.

---

## 2026-08-31 · Session 6

### D-029 · The exhaustive budget search stays, and its cost is published

**Decided.** `--budget` on `ansi`, `md`, and `html` keeps `exhaustive_prefixes=True`.
Selection is non-monotone — completing a multi-unit claim removes that claim's drop record
and can shrink the output — so a binary search alone can miss a valid larger projection.
The exhaustive pass is what makes "at most N bytes, and here is everything omitted" a
guarantee rather than an approximation. It is not traded away for speed.

**Measured** on `src/autotldr` (1750 units, 654 relations) on 2026-08-31: unbudgeted `md`
renders in 0.40 s and unbudgeted `json` in 1.31 s. Under a budget, `json` takes 5.1 s
because it binary-searches, while `md` takes 784 s because it probes every ranked prefix.
The hero collection (135 units) stays inside a few seconds, so the cost only bites on
large collections.

**Limits and revisit trigger.** Revisit when someone budgets a human shape over a
thousand-unit collection and the wait is the reason they stop using the tool. The fix is a
cheaper search that still proves completeness — for example bounding the probe set to the
claim-completion boundaries plus a binary search between them — not dropping the guarantee.
Until then the number is documented rather than discovered.

### D-028 · Warnings fail the build, and the one tolerated exception is named

**Decided.** `pyproject.toml` sets `filterwarnings = ["error", ...]`, so any warning raised
anywhere in the suite is a build failure. Exactly one message-specific `ignore` is allowed,
and it is documented at the filter: netCDF4 1.7.4 ships `_netCDF4.abi3.so`, a stable-ABI
wheel whose Cython type check reads `numpy.ndarray.__basicsize__` (96) and compares it with
`sizeof(PyArrayObject)` as numpy 2 declares it in the public header, where the struct is
opaque and therefore 16 bytes. Runtime-larger-than-header is Cython's benign warn case;
netCDF4 reaches arrays only through the public numpy C-API and never allocates the struct.

**Why.** The previous baseline reported "904 passed, 1 warning" and treated that warning as
an open question across three sessions. It was not diagnosable from a standalone import: it
surfaces only when netCDF4 is imported through the dynamically loaded benchmark modules,
which is why it appeared exclusively in the full-suite run. Promoting warnings to errors
converts "no warnings observed" from an observation into an enforced contract and makes the
next unexplained warning fail immediately instead of accumulating.

**Rejected.** Pinning a different netCDF4 is not a fix: abi3 wheels are the direction of
travel and the warning is structural to that build, not to a version. A blanket
`ignore::RuntimeWarning` was rejected for the obvious reason.

### D-027 · PDF byte reproducibility is a within-process contract

**Decided.** Rendering one fixed `Extraction` to ANSI, Markdown, HTML, JSON, or JSONL is
byte-identical, in one process and across processes. `render_pdf` is byte-identical within
one process only. `pymupdf.Story` lays identical HTML out differently in different
processes; a 59-page projection was measured at five distinct outputs across eight fresh
interpreters with the same input bytes. Page count, text, links, and the complete
`drop-v1` omission inventory are stable across all of them. AutoTLDR states the narrower
contract rather than the one it cannot keep.

**Found by.** The model-off proof compared repeated renders. Two separate causes were
present. The first was AutoTLDR's: MuPDF emits coordinates that are exactly zero in the
source geometry as values around 1e-6, and that accumulated noise differed between two
layouts inside one process — 635 differing numeric tokens in a 59-page document, every one
below 2.3e-6. `share._snap_content_stream_noise` now rewrites every page content stream,
snapping magnitudes under 1e-4 PDF units — roughly 35 nanometres at 72 dpi, four orders
below the thinnest rule the renderer draws — to an exact zero. That restored in-process
determinism at a scale where the previous two-page fixture never exercised it.

**Limits.** The second cause is upstream and was reproduced with `pymupdf.Story` alone, no
AutoTLDR code involved. It is not worked around by CSS: an explicit `width`, removing
`page-break-inside`, and the coordinate snap were each measured and none of them removed
it. Revisit when MuPDF's Story layout becomes reproducible, or if the PDF path stops going
through Story.

**Also decided.** `h2::before { content: attr(data-index) }` was replaced with real markup.
Story implements no `attr()`, so every PDF section heading read `attrWhat matters` instead
of its number. Generated content that a PDF must carry has to exist in the markup.

### D-026 · Canonical ordering may not depend on the private snapshot path

**Decided.** An extractor may not rank anything by `Unit.id`. Extraction runs against an
immutable private snapshot whose directory name changes on every invocation, so the IDs an
extractor computes are temporary; the router rewrites them to logical IDs afterwards.
Ordering is ranked by canonical unit position — the index of each endpoint in the
`(origin.ref, modality, content)`-sorted unit list — which survives that rewrite unchanged.

**Found by.** Two identical `router.extract` calls on the same file returned the same
relation set in different orders. It reached the product: two runs of the Borealis
collection produced HTML of identical length and different bytes. `extract/tier3.py` (all
five Parquet/SQLite/DuckDB/HDF5/NetCDF adapters) and `extract/astronomy.py` sorted
relations by raw `src`/`dst`; `columnar_interchange.py` and `scientific_arrays.py` already
used the position index and were already correct. `tests/test_determinism.py` pins the
invariant for both fixed adapters and asserts the position-ranked property directly.

**Constraint that falls out.** Any future extractor that needs a stable order must derive
it from logical, source-independent facts — origin ref, modality, content, native position
— never from a content hash computed over a path the caller never sees.

## 2026-08-30 · Session 5

### D-025 · Resolve by exact local path, load by model key, verify the resident

**Decided.** The guarded LM Studio lifecycle resolves a requested artifact against an
exact unprefixed `deviceIdentifier: null` catalog path while ZBook is the verified
preferred device. Current `lms load` does not accept that path as its positional selector;
it accepts `modelKey`. The runner therefore loads the resolved row's model key with
`--yes` inside the same preference transaction, then accepts the operation only when the
fresh process row is the sole local LLM, has the owned identifier and requested settings,
and matches the captured local fingerprint. Incumbent restoration uses the same mechanism.

Passing a linked/colon path, resolving an ambiguous short key, trusting `localhost` alone,
or accepting a served-model identity different from the configured instance all fail
closed. A catalog path remains the authorization identity; the model key is only the CLI
selector required after locality has been fixed and verified.

### D-024 · PDF has a binary counter; frozen human-budget audits remain immutable

**Decided.** D-015's `utf8-byte-v1` name applies only to canonical UTF-8 text output.
PDF reuses the same atomic selection and full `drop-v1` omission inventory but reports
`binary-byte-v1` and counts the complete PDF bytes. Deterministic padding is inside the
valid file before `startxref`/the terminal `%%EOF`; long omission records are fragmented
only at safe layout boundaries so pagination cannot silently clip fields.

Correcting the Stage 5 evaluator to parse the real D-015 human wire made four frozen
v2 Markdown/ANSI budget cells honestly infeasible. `policy-v2.json` is hash-frozen and is
not retuned. Its own `render_budget_scope.product_acceptance_target: false` means those
cells remain reported audit evidence rather than silently becoming a synthesis-candidate
eligibility gate. A future policy may deliberately set that field true with feasible
budgets; the frozen policy is never rewritten after seeing results.

### D-023 · LM Studio compatibility is exact and hidden reasoning has no authority

**Decided.** The strict OpenAI-compatible response parser accepts one qualified LM Studio
profile: the exact observed speculative-decoding counters, empty tool-call field,
`reasoning_content`, nullable logprobs, and reasoning-token usage detail. Hidden reasoning
is never parsed as a claim; only its byte count and SHA-256 are recorded before it is
discarded. Arbitrary provider extras still fail closed.

The response's served-model identity must equal the configured model or one explicitly
frozen alias. This is a security boundary under LM Link: a local alias that disappears
must not let a Dynamo substitution become an accepted AutoTLDR run.

### D-022 · Software completion and physical residency certification are separate

**Decided.** This supersedes only the part of D-020 that made unavailable hardware
telemetry a gate on completing the software stages. The Stage 1–8 MVP may be called
functionally complete when the mixed collection is acquired, fused, strictly synthesized,
and rendered end to end through the numeric-loopback transport with exact model/run
records. It may not claim independently certified zero CPU spill unless a process-level
attestor proves actual layer placement before and after inference.

The guarded lifecycle still requires LM Studio's 100% GPU estimate, `--gpu max`, exclusive
local residency, exact `deviceIdentifier: null` identity, owned-row cleanup, and unchanged
linked peers. If an actual-residency attestor is unavailable, the certification path fails
closed and the limitation is named; configuration evidence is not relabeled as physical
proof. The successful Ornith Borealis run is a functional local inference proof, not a
fabricated hardware attestation.

### D-021 · Freeze expansion and ship one thin Stage 1–8 vertical slice

**Decided.** The MVP is one composable pipeline: acquire, normalize, fuse, optionally
synthesize grounded claims, then render. CLI, Python API, watch, HTML/PDF, MCP Tasks, the
Agent Skill, and A2A metadata must wrap that pipeline rather than fork it. Every one of the
eight planned stages gets the smallest complete production-shaped implementation; format
universe expansion, more model shopping, embeddings, hosted inference, and additional
output shapes are deferred.

This slice is complete only when a mixed Tier 0–3 collection can produce human and machine
artifacts, watch can update the same result, and agents can request it without receiving a
second semantics implementation. This decision closes the sprint but does not declare the
pre-alpha interfaces stable.

## 2026-08-30 · Session 4

### D-020 · The first synthesis demo is ZBook-local; OpenAI-compatible names the wire

**Decided.** This narrows D-019's phrase “local or OpenAI-compatible” for the
first complete demo. The Stage 5 synthesis evaluation and hero run use only the
user's ZBook-local LM Studio endpoint at `http://127.0.0.1:1234`. “OpenAI-
compatible” describes that localhost wire protocol; it is not permission to
call a hosted or remote model. A protocol-neutral client seam may make a future
configured remote backend possible, but remote inference is outside this gate
and requires separate user authorization.

The existing lifecycle remains binding: verify ZBook locality despite LM Link,
require 100% GPU offload, keep one AutoTLDR-owned generation model resident,
evaluate candidates sequentially, unload only the owned local row, verify its
absence, restore preference/incumbent state, and leave every linked/Dynamo row
unchanged. This does not select a model in advance; it prevents transport
generality from broadening the explicitly authorized infrastructure scope.

### D-019 · The first complete demo is Tiers 0–3 plus grounded local synthesis

**Decided.** A deterministic fusion dump is not the complete AutoTLDR demo. The
first demo that may be described as the product must exercise every locked v1
input tier—Tier 0, Tier 1, Tier 2 collection acquisition, and all Tier 3 native
data adapters—and produce an actual concise “too long; didn’t read” collection
synthesis through a configured local or OpenAI-compatible model. Stage 5 now
owns that integrated gate: directory/repository/archive/doc-site acquisition,
the remaining Tier 3 adapters, and grounded synthesis over the Stage 1–4
representation. This extends the earlier Stage 5 row, which named only Tiers 2
and 3.

The deterministic Stage 4 bundle remains the model-off fallback and the source
of truth. A synthesis model consumes a separately budgeted canonical evidence
pack only after extraction and measured fusion. It returns strict structured
claims containing existing evidence unit IDs; AutoTLDR rejects unknown IDs,
derives origins itself, constructs `GroundedStatement`s, and sends those claims
through the existing exact complete-output budgeter. The model may not invent
relations, contradictions, gaps, origins, or roles. An invalid response never
becomes repaired free-form prose. Every run records the exact model identifier,
task, endpoint class, settings, input/output hashes, timing, and outcome in the
manifest. Projection preserves those records rather than resetting
`models: []`.

For local inference, model parameter count is not a product eligibility limit.
The selected model must be measured on the synthesis task; Ornith's Stage 2
role-tagging result does not establish synthesis quality, and model marketing
does not substitute for an eval. The authorized lifecycle is ZBook-only at
`http://127.0.0.1:1234`: one AutoTLDR-owned generation model resident at a time,
100% GPU offload, sequential estimate/load/run/unload, exact local-row and
`deviceIdentifier: null` verification, LM Link preference restoration, and no
mutation of a linked/Dynamo row. No embedding model is needed for this demo.
The optional second residency slot remains disabled until semantic-link value
is separately measured.

**Demo gate.** A bounded mixed collection must include representative Tier 0
and Tier 1 documents, a real Tier 2 container, XLSX plus Parquet, SQLite,
DuckDB, HDF5, and NetCDF Tier 3 inputs, and planted cross-source evidence. It
passes only if the summary is useful and fully cited, every summary evidence ID
and relation endpoint resolves, cited origins equal evidence origins, no
unsupported claim or raw data payload leaks, the complete output honors its
byte ceiling and drop inventory, every input/decline and model is manifested,
the generation model stays fully on GPU, linked peers are unchanged, and the
owned model is absent after cleanup. Until that gate passes, call the current
artifact a fusion prototype, not a complete AutoTLDR demo.

### D-018 · Stage 4 ships only the fusion signals that passed separately

**Decided.** Stage 4 is complete as a measured, model-free fusion substrate over
two or more explicitly named acquired sources. The CLI accepts repeated source
arguments and shell-expanded globs; direct directory, repository, archive, and
doc-site collection acquisition remains Stage 5. One-source invocation retains
the Stage 3 behavior. Fusion preserves every input unit, relation, extraction
gap, and exact input manifest; adds one source-manifest anchor per input; and
emits grounded collection statements through all four existing renderers under
the same exact budget and omission contract. Every model record remains empty
for this stage.

The first candidate corpus was rejected before held-out prediction by two
independent source-first audits. The corrected freeze contains 6 scored
collections, 34 real routed fixtures, 222 units, 122 intra-source relations,
and targeted positive and hard-negative truth for six tasks. Two new blind
audits independently reconstructed every support count before a hash-bound
clearance unlocked one immutable scored run. Aggregate accuracy was not
computed or used.

| Signal/finding | Support | Precision | Recall | Frozen disposition |
| --- | ---: | ---: | ---: | --- |
| literal reference | 23 | 1.000 | 0.957 | ship complete |
| identifier correspondence | 171 | 0.850 | 0.830 | ship preregistered `native-native` subtype only (P=.900, R=.857, support 63, 6 groups) |
| structural correspondence | 10 | 1.000 | 0.800 | ship complete |
| strict scalar contradiction | 12 | 1.000 | 0.667 | disable; recall missed the frozen .70 gate |
| orphan absence | 7 | 1.000 | 0.571 | disable; recall missed the frozen .90 gate |
| unresolved reference | 9 | 0.900 | 1.000 | ship preregistered `local-path` subtype only (P=R=1.000, support 6, 5 groups) |

The raw `analyze()` surface remains available for transparent diagnostics and
future fresh-corpus evaluation. The user-facing `fuse()` path mechanically
filters that output to the table above: it emits all literal and structural
relations, native/native identifier candidates, and non-ambiguous local-path
gaps; it emits no contradiction or orphan finding. Disabled results are named
in the manifest, never reported as zero observed facts. The exact evaluated
source and immutable prediction/report hashes are preserved under
`benchmarks/fusion/audit-history/scored-v2-evaluated/`; the production filter
was applied after scoring without tuning or rerunning v2.

**Limits.** This is a synthetic engineering gate over production extractors,
not a production-prevalence estimate or human domain-expert gold standard. The
positive identifier set is exact-name-heavy, rare literal/unresolved subtypes
are thin, and fixture rhetoric is intentionally explicit. Stage 4's three
sentences are grounded engineering findings, not genuine model synthesis and
not yet the promised statement of what a folder is for. Signal 4 embeddings
remain deferred. Any matcher change requires a new held-out source group under
a frozen policy.

## 2026-08-30 · Session 3

### D-017 · Stage 3 is a complete, deterministic invoke contract

**Decided.** Stage 3 is complete. One synchronous command accepts a local path,
strict stdin, or an HTTP(S) URL; routes Tier 0 and Tier 1 inputs to native
extractors; and emits canonical UTF-8 `ansi`, `md`, `json`, or `jsonl`. Human
shapes can suppress inline citations, but machine shapes always retain origins.
The process exits `0` for success, `1` for a contained processing failure, `2`
for argparse usage, `3` for a named unsupported or unknown format, `4` for a
missing path, and `5` when the requested budget cannot hold a valid addressable
envelope. A downstream pipe closing early is success. Runtime errors do not
escape as tracebacks.

The default ANSI result is a deterministic complete projection, with color only
on a TTY and never when `NO_COLOR` is set. It is not yet a live progressive,
syntax-highlighting, folded UI. JSONL is line-oriented and pipe-friendly, but
selection finishes before bytes are written. This supersedes those three ANSI
adjectives and the live-streaming note in the broader capability matrix for the
v1 Stage 3 surface. Live progressive rendering may be added only if it retains
the same final budget and omission-accounting contract.

Every machine manifest records the acquired input's logical source, detected
kind and tier, byte length, SHA-256, acquisition and extraction timings,
representation/version data, ID schemes, the role backend, and `models: []`.
Path acquisition hashes the same stable snapshot the extractor reads, guarded
by pre/post device, inode, size, and nanosecond-mtime checks. URL routing gives
strong acquired-byte identity first priority, then specific response media;
only generic or absent media may borrow a supported suffix from the final URL
and then the requested URL. Non-HTTP(S) redirects are rejected before they are
followed, and acquisition may prefer a valid same-origin `/llms.txt`. stdout
and `-o` always receive the canonical UTF-8 bytes regardless of
`PYTHONIOENCODING`.

**Stage boundary.** Stage 3 fills `Bundle.summary` with an evidenced per-source
structural statement: counts of addressable units, relations, and reported
gaps. It does not pretend that this is the three-sentence semantic collection
statement described for the finished product. Cross-source synthesis,
contradictions, orphans, and the semantic collection statement remain Stage 4.
No role model, embedding model, or model runtime is required or invoked by
Stage 3.

**Why.** A Unix contract must be observable at the wire and process boundaries,
not inferred from a renderer's intent. Deterministic acquisition and explicit
exit classes make the command safe to compose. Deferring synthesis keeps a
model-free invocation useful without inventing meaning that the fusion stage
has not grounded.

### D-016 · Stage 3 adapters preserve native bytes and decline weak parses

**Decided.** Every Stage 3 claim comes from a native structural address or an
exact decoded source slice. Local prose and source inputs require strict UTF-8;
HTTP text formats that permit charset negotiation use the declared charset
strictly; formats with their own mandated encoding rules remain strict under
those rules. No extractor may create a claim with replacement characters.
Markdown, rST, LaTeX, source code, notebook cells,
and HTML code preserve meaningful whitespace and exact character spans. Invalid
encoding, unsafe or over-limit packages, ambiguous suffixes, and parser results
that cannot support exact origins fail or become source-addressed gaps instead
of weak claims.

Python uses the standard-library AST. Reliable non-Python languages lazy-load
the offline, pinned `tree-sitter-languages==1.10.2` pack with its compatible
`tree-sitter` ABI. A language without a trustworthy grammar is declined by name;
there is no regex fallback. In particular, Lua is declined because the pinned
pack rejects ordinary valid functions in fresh-parser probes, and Objective-C,
Perl, and their ambiguous suffixes require positive content evidence. The
supported and explicitly declined suffixes are kept as centralized router/code
maps and regression-tested.

DOCX is read directly as bounded OOXML with the standard library, and notebooks
as bounded native JSON. This supersedes the implementation-library rows in the
original v1 spec that named `python-docx` and `nbformat`; the semantic contract
(paragraphs, comments/revisions, cells, outputs, and pairings) is unchanged.
Direct parsing exposes the package-native addresses needed for provenance,
keeps the base install smaller, and avoids library normalization of exact cell
or revision content. EPUB and DOCX enforce member, total-size, ratio, path,
encryption, duplicate-name, and structural-depth limits. HTML uses its native
DOM for every emitted origin; optional Trafilatura output may filter native
blocks but may never introduce rewritten, unaddressable text.

**Rejected.** Lossy format conversion; decoding with replacement; regex symbol
extraction when a syntax tree is unavailable; trusting a filename over strong
PDF/ZIP/HTML signatures; flattening nested HTML code into text that does not
round-trip; and accepting unbounded ZIP/XML/YAML/CSV structures. A missed input
is visible and repairable. A plausible claim with a false origin is not.

### D-015 · The portable budget is the complete canonical UTF-8 byte stream

**Decided.** `--budget N` names the `utf8-byte-v1` counter: one portable token
is one byte of the exact canonical UTF-8 payload. The counter's scope is the
complete output, including framing, summary, units, relations, gaps, citations,
escaping, ANSI controls, manifest, selection report, and final newline. Every
shape reports the counter, scope, requested ceiling, actual bytes used, and the
unlimited form's available bytes. This makes compliance independently
recountable and conservative for ordinary byte-based model tokenizers without
silently binding the CLI to one model.

Selection keeps a salience-ranked prefix of atomic units and its induced
relation graph. Every omitted unit is listed by full ID and full origin. Every
omitted relation is listed by source-order index, full endpoints, and kind. A
SHA-256 commits to the same concrete drop set as supplemental integrity data,
never as a replacement for the inventory. If even an empty selected set plus
all mandatory findings and omission records does not fit, the command emits no
stdout, exits `5`, and reports the exact retryable minimum.

`Unit.tokens` remains the dependency-free `char4-floor-v1` diagnostic estimate;
it is not used to prove renderer compliance. There is no model-independent
meaning for “exact LLM tokens” without naming a tokenizer, and importing a
default tokenizer would violate both that fact and the cold-start contract.

**Rejected.** The character-divided-by-four estimate as a hard ceiling; counting
only selected unit bodies while ignoring output overhead; truncating a unit;
hash-only or count-only drop reports; shortened identifiers; and silently
emitting a structurally invalid envelope when the requested budget is too
small. A future explicit tokenizer mode may add a separately named counter, but
must count the complete serialized payload and preserve this failure behavior.

### D-014 · Semantic identities are full, origin-complete 128-bit digests

**Decided.** Unit identity v2 is a length-framed 128-bit BLAKE2b digest of the
logical source, native origin reference, complete character span, modality, and
content. Finding identity v1 uses the same framing over source, native
reference, complete span, and finding content. Renderers expose full IDs,
reject duplicate unit IDs, and reject relations with unresolved endpoints.
Budget omission records never abbreviate an identity.

**Why.** The original short content/origin digest omitted the full span and
modality. That allowed two legitimate semantic units at the same native HTML
address, such as prose and an outbound reference with identical visible text,
to alias. Truncated IDs also made collision risk needlessly material once
folder fusion combines many sources. Length framing prevents concatenation
ambiguity, modality distinguishes different semantic projections, and the full
span distinguishes repeated same-line syntax nodes.

**Compatibility.** IDs are stable for the same logical input and extraction,
but deliberately change from the Stage 1 scheme. Stage 2's scored artifacts and
hashes are frozen historical evidence; their validator does not rebuild IDs
against the live representation. Exact reconstruction still uses the pinned
pre-shrink commit recorded in D-013. Revisit the scheme only through an explicit
representation-version migration, never by quietly changing digest inputs.

## 2026-08-30 · Session 2

### D-013 · The role taxonomy shrinks, and role reliability is backend-scoped

**Decided.** Stage 2 is complete and closes the open gate in D-009. The live v1
taxonomy is now `definition`, `procedure`, `caveat`, `example`, `decision`,
`assumption`, and `limitation`, plus the mandatory `unknown` fallback. `claim`,
`parameter`, and `result` are removed because no evaluated arm recovered them at
the preregistered threshold. This supersedes D-009's eleven-role taxonomy and
its assumed 4B local arm.

A backend may emit a named role only when that backend passed the role's gate.
The deterministic fast path may emit `assumption`; otherwise it emits `unknown`.
The selected local enrichment model may additionally emit `procedure`. A
configured frontier-class enrichment endpoint may emit `definition`,
`procedure`, `caveat`, `example`, `decision`, and `limitation`. `unknown` is a
fallback outcome, not a product-level semantic claim. The bundle manifest must
record which enrichment backend ran so downstream consumers never mistake a
backend-dependent role for a universal extractor guarantee.

**Evidence.** The frozen diagnostic corpus contains 200 exact production-
extractor units: 40 each from Markdown, reStructuredText, plain text, PDF, and
XLSX, across 20 real source groups. The preregistered per-role gate was support
at least 15, at least three source groups, precision at least 0.80, and recall at
least 0.70. Aggregate accuracy was deliberately not used.

| Gold role | Rules P/R/F1 | Local P/R/F1 | Frontier P/R/F1 | Passing arm |
| --- | ---: | ---: | ---: | --- |
| `unknown` | .123/.600/.204 | .862/1.000/.926 | 1.000/.680/.810 | local fallback |
| `claim` | .000/.000/.000 | .327/.941/.485 | .630/1.000/.773 | none — removed |
| `definition` | .000/.000/.000 | .737/.933/.824 | .812/.867/.839 | frontier |
| `procedure` | .571/.167/.258 | .857/.750/.800 | .960/1.000/.980 | local, frontier |
| `parameter` | .000/.000/.000 | .400/.400/.400 | .588/.667/.625 | none — removed |
| `caveat` | .455/.667/.541 | .722/.867/.788 | 1.000/.933/.966 | frontier |
| `result` | 1.000/.680/.810 | .613/.760/.679 | .561/.920/.697 | none — removed |
| `example` | .600/.188/.286 | 1.000/.312/.476 | 1.000/.812/.897 | frontier |
| `decision` | .000/.000/.000 | 1.000/.467/.636 | 1.000/.867/.929 | frontier |
| `assumption` | 1.000/.941/.970 | 1.000/.059/.111 | 1.000/.118/.211 | rules |
| `limitation` | .000/.000/.000 | 1.000/.312/.476 | .933/.875/.903 | frontier |

The rules arm therefore keeps one named role. The local arm closes one real gap
(`procedure`) but does not approach the frontier across the taxonomy. The
frontier ceiling shows that six other distinctions are coherent, while also
showing that a larger model is not automatically better: it loses the
structurally proven spreadsheet `assumption` role. v1 uses backend routing, not
one universal classifier.

**Local model selection.** Parameter count is not an eligibility limit. A
separate, label-isolated 33-item pilot compared the already-loaded
Ornith-1.5-35B-A3B with Granite 4.2 3B Q8, Granite 4.2 8B Q4_K_M,
MiniCPM-V 4.6 F16, and Gemma 4 26B-A4B QAT. Every candidate produced 33 valid
strict-schema responses. Ornith scored 15/33; Gemma led it by two, which did not
meet the frozen four-item switch margin, so Ornith remained the formal local
arm. The other candidates scored 4, 8, and 8 respectively. Pilot totals select
a model; they are not role-recoverability evidence.

Local lifecycle is an operational constraint, not a model-size policy: one
ZBook-local LLM resident at a time, zero embeddings for this task, 100% GPU
offload, sequential load/run/unload, and `deviceIdentifier: null` verified after
every load. LM Link's preferred device is explicitly switched to the ZBook for
each estimate, load, and unload, then restored and verified before inference.
Linked hosts are never loaded, invoked, or unloaded.

**Why shrink instead of merge.** No merged classes were preregistered, and
inventing them after seeing the confusion matrix would turn the scored set into
a prompt-design set. Removed labels do not erase meaning: formula/value metadata,
dependency relations, structural cues, exact content, and origins remain. They
are simply not promoted to a typed role that the evidence did not support.

**Limits and revisit trigger.** The two blind reviewers and adjudicator were AI
agents, not independent human domain experts. Their agreement was high (91.5%,
Cohen's kappa .906), but shared training data and public-source pretraining can
inflate agreement, especially with a related frontier arm. Treat this as the v1
engineering gate, not permanent ontology truth. Revisit a removed role only on
a fresh held-out corpus with a frozen policy when a human audit materially
changes labels, segmentation is deliberately redesigned, or a new local backend
clears the same per-role precision/recall gate. Exact reconstruction of this
historical corpus's rule outputs requires the pinned pre-shrink commit
`54b2158a37e7dd42392494fbadf031e11d952289`; validation and reporting operate on
the frozen artifacts without that checkout.

## 2026-08-30 · Session 1

The project had six planning documents written across four models between October
2025 and August 2026, and zero lines of implementation. This session archived all
of them, rebuilt the product framing from scratch, and shipped the first working
code. Everything below was decided in that session.

### D-012 · Prose is unwrapped; code is byte-exact

**Decided.** Hard line breaks inside a prose paragraph are joined. List items,
quotes, and table rows keep their breaks. Code blocks are never touched.

**Why.** A wrap point is not meaning. Leaving it in means `"power\nanalysis"`
never matches "power analysis", which silently breaks every phrase and identifier
match that folder fusion depends on. The same class of problem is why the PDF
extractor de-hyphenates across line breaks.

**Found by** a failing test during the Stage 1 spike, not by planning.

### D-011 · Cold start is a test, not an aspiration

**Decided.** Under 120ms to first output for a Tier 0 file, enforced in the test
suite. A parser entering the CLI's import graph fails the build.

**Why.** Python's import cost is the one thing that can make this feel unlike a
Unix tool, and it degrades silently: someone adds a convenient top-level import,
every invocation gets slower, nobody notices for a month.

**Measured** at 74ms best, 80ms median on first implementation.

### D-010 · Extractors precede the role eval

**Decided.** Build order is representation spike, then role-tagging eval, then
everything else. Supersedes an earlier draft that put the eval first.

**Why.** A dependency inversion: you cannot evaluate role tagging until
extractors exist to produce spans to label.

### D-009 · Role tagging is gated by an eval before implementation

**Decided.** Rules-only role assignment ships first. A 200-span labeled eval
across five formats, scored against rules-only, a 4B local model, and a frontier
ceiling, decides what happens next. Extractors emit `UNKNOWN` rather than
guessing.

**Why.** The `role` field is what makes the representation more than chunked
text, and a 3B local model may simply not be reliable at distinguishing a claim
from a caveat from an assumption. If rules land close to the 4B, v1 ships fully
offline. If neither approaches the ceiling, the taxonomy shrinks to what is
actually recoverable and the product claim shrinks with it.

**Open.** This is the largest unresolved risk in the project.

### D-008 · Extraction routes; it never converts

**Decided.** Each format goes to the extractor that understands it natively.
XLSX through `openpyxl` on the formula layer, never through a markdown converter.
Fast path by default; `--deep` opts into the slow accurate extractor.

**Why.** No single extractor wins across formats, and accuracy gaps above 20% on
complex layouts are normal. More importantly, conversion destroys exactly the
structure that carries the meaning: a spreadsheet's formula graph, a DOCX's
comments, a notebook's output pairing.

### D-007 · SQLite is the store; DuckDB is a transient compute engine

**Decided.** SQLite with WAL holds units, relations, folder state, the job queue,
and the cache. FTS5 for lexical, sqlite-vec for vectors, recursive CTEs for the
relation graph. DuckDB is spun up to profile a data file and closed. It is never
a persistent store.

**Why.** DuckDB reads CSV, Parquet, JSON, and Excel directly off disk with zero
ingestion, which turns Tier 3 profiling into a query rather than a parse-and-load
pipeline. Keeping it transient also means its single-writer limitation never
bites once the watch daemon and the CLI are both live.

**Rejected: SurrealDB.** Architecturally the most elegant option, and the one
explicitly evaluated. 3.0 went GA February 2026 with one engine covering
document, graph, relational, full-text, and vector in a single transaction, and
its live queries fit the watch daemon well. Four reasons against for v1: no
zero-ingestion file reading, which is worth more here than multi-model
unification; vector indexing described by SurrealDB itself as a foundation for
future specialized indexes rather than a shipped one; no independent embedded-mode
benchmarks, with nearly all technical detail tracing to their own material and
funding openly earmarked for hardening reliability; and an engine built to also
run distributed clusters is the wrong shape for a 120ms CLI.

**Revisit when** the watch daemon rather than the CLI becomes the primary entry
point. Live queries and a unified model then beat hand-rolling that on SQLite.
The storage layer stays behind one interface so it remains a swap.

**Also rejected.** LanceDB solves a vector-scale problem v1 does not have. Kùzu
is right if graph traversal becomes the bottleneck, and recursive CTEs over a few
thousand units will not be. Postgres with pgvector, Chroma, and Qdrant all need a
server, which disqualifies them for a Unix tool.

### D-006 · Python, distributed with uv

**Decided.** Python 3.12+, `uvx autotldr`. Base install has zero dependencies;
each format's parser lives in an extra and is lazy-imported at the point of use.

**Why.** The entire extraction ecosystem is Python: MarkItDown, Docling,
openpyxl, pypdf, pymupdf, python-docx, nbformat, tree-sitter bindings, h5py,
trafilatura. Rust gives a faster cold start and a static binary, and costs most of
year one reimplementing maintained extractors.

**Revisit when** the extractor set is stable and startup is still the complaint.
A Rust core calling Python extractors as subprocesses is the migration path, and
the CLI contract is designed so that swap is invisible to callers.

### D-005 · A watched folder emits per-file artifacts plus a folder roll-up

**Decided.** Each dropped file gets its own artifact; `FOLDER.tldr.md` holds the
fused collection view, rewritten on debounce.

**Why.** Useful at both granularities, and the per-file artifacts are nearly free
because they are already computed on the way to fusion. Versioned history is the
more interesting option and roughly double the work; it can be added later
without changing the on-disk contract.

### D-004 · The watch daemon ships in v1

**Decided.** Seven weeks to first release rather than five.

**Why.** "Auto" is in the name and it has to mean something. Invoke mode is a
tool and there are other tools; watch mode turns a directory into a
self-summarizing corpus, which is the differentiated half. The work does not
overlap with anything else on the critical path.

**Constraint that falls out.** The CLI must work with the daemon stopped,
uninstalled, or never started. A tool that requires a background service to
answer a question is not a Unix tool.

### D-003 · v1 is the mixed folder, text-only, Tiers 0 through 3

**Decided.** Point at a file or folder of text-derivable formats and get one
fused, cited bundle. No images, audio, or video.

**Why.** Cross-modal fusion is the thing genuinely nobody does, and per-file
summarization is a commodity. Restricting to text-derivable formats means nothing
in v1 requires a model to produce a useful result, so the tool degrades to
something valuable even with every model turned off.

**Rejected wedges.** Spreadsheet-only was the strongest single demo and needs no
model, but it is narrower than the folder. Meetings land in the slowest, most
model-dependent tier on day one. Version-pinned API answers sit closest to
Context7 and Ref, where the work is competing rather than flanking.

### D-002 · The incumbents are input adapters, not competitors

**Decided.** AutoTLDR shells out to Repomix, gitingest, crawl4ai, Context7,
Docling, MarkItDown, and Whisper, and treats their output as raw material.

**Why.** Every one of them is better at its narrow job than a new project will be
in year one. Treating them as competitors is what made the prior six plans
unbuildable. None of them produces a semantic representation, and that leftover
work is the entire product.

### D-001 · One representation, N inputs to M outputs

**Decided.** Every input normalizes into one intermediate representation; every
output renders from it. Adding an input costs one extractor and zero renderers.

**Why.** N formats times M shapes is N×M converters otherwise, which is the trap
that kills format-universal tools.

**Validated** by the Stage 1 spike against three deliberately dissimilar formats:
markdown (prose with an explicit outline), XLSX (a typed cell graph with no prose
at all), and PDF (prose with no structure but font sizes). Same `Unit`, `Origin`,
and `Relation` types held all three, relations resolved, IDs were stable, and both
renderers worked without special-casing any format.

### D-000 · The six prior plans are archived, not built on

**Decided.** `MASTER-PLAN-v4`, `GPT5-PLAN`, `GEMINI-PLAN`, three Haiku master
plans, the original `INITIAL-PLAN`, and a CLIO-ecosystem analysis all moved to a
gitignored `archive/`.

**Why.** Six restatements of a plan across ten months with no implementation is a
signal that planning had become the work. The strongest idea in the corpus was in
an informal ideas file rather than any master plan, and each successive plan
drifted further from it.

**Kept from them.** The three-dimensional context shape (memory, reasoning,
examples), attribution-first synthesis, budgeted selection, and the per-component
token-counted selector. All carried forward into `matrix.md`.
