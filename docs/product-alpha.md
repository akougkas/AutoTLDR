# AutoTLDR alpha product contract

This is the first-user contract. It describes the product we are building, not the
incidental limits of the first two implementation days. The technical representation and
evaluation record remain in `spec-v1.md` and `decisions.md`.

## The promise

> Point AutoTLDR at a technical file, mixed folder, archive, or documentation URL and get
> a concise local-model TLDR whose claims link back to native evidence.

AutoTLDR understands each supported format on its own terms. A spreadsheet contributes
its formula graph, a database its schema and relationships, a scientific array its
dimensions and attributes, a notebook its cell/output graph, code its symbols and
imports, and prose its addressable structure. The local model writes over that evidence;
it does not replace extraction.

## The first user

The primary user is a developer, researcher, or data practitioner at a terminal who has a
technical artifact and needs to understand it quickly. Shell-capable coding agents use the
same command with a machine output. Non-shell agent protocols remain wrappers over the
same run contract.

The alpha leads with four jobs:

1. Understand a mixed technical handoff without flattening every format into text.
2. Explain a spreadsheet's formulas, assumptions, inputs, outputs, and dependencies.
3. Brief a research-data package using schema, dimensions, units, attributes, and gaps
   rather than raw rows or arrays.
4. Compile a cited context artifact for an agent under an exact byte ceiling.

Generic repo summaries, meetings, images, audio, video, hosted inference, and a format
marketplace are not alpha promises.

## The ordinary command

```bash
autotldr report.xlsx
autotldr ./experiment --detail brief
autotldr ./handoff --detail deep --out html -o handoff.html
autotldr ./handoff --out json --budget 131072
```

For file output, the recognized `.md`/`.markdown`, `.html`/`.htm`, `.pdf`, `.json`, and
`.jsonl` suffixes select that shape when `--out` is omitted. Stdout and unknown suffixes
remain ANSI, and an explicit `--out` always takes precedence.

The default is a cited prose TLDR produced by the configured local model. A successful
human result leads with what matters, then shows supporting native structure, gaps,
declines, and provenance at the requested detail. Machine output retains the complete
addressable representation and run manifest.

If no usable local model is configured, the command prints one short diagnosis and the
exact `autotldr setup` or `autotldr doctor` action to take. If synthesis fails validation,
the command fails rather than relabeling an IR report as a TLDR. A caller may explicitly
choose evidence fallback when continuity matters more than prose.

Watch mode uses that same prose/detail policy for changed-file artifacts and the folder
roll-up. It does not silently switch to deterministic output; `--model off` and explicit
evidence fallback retain their ordinary meanings.

## Detail, not model tuning

Users choose the answer shape. AutoTLDR owns the provider settings needed to produce it.

| Detail | User intent | Initial profile target |
| --- | --- | --- |
| `brief` | The few facts needed to orient quickly | Up to 2 prose claims, 8 KB of evidence, compact sources and gaps |
| `standard` | A useful everyday technical brief | Up to 4 prose claims, 24 KB of evidence, key native structures |
| `deep` | A handoff or audit that explains the system | Up to 6 prose claims, 48 KB of evidence, detailed gaps and supporting structures |

The targets are versioned product profiles, not promises about one model tokenizer. The
final `--budget` remains a hard ceiling over the complete rendered artifact. When detail
and budget conflict, the exact budget wins and the omission inventory says what was left
out. The LM Studio alpha profiles disable hidden reasoning for constrained sentence
writing and verify that the runtime reports zero reasoning tokens. Detail changes evidence,
claim, generation, and presentation allowances; it never grants hidden reasoning claim
authority. Claim allowances are ceilings, not targets: the product prompt explicitly asks
for fewer claims when additional prose would not add distinct supported meaning.
The profiles also own bounded 60/90/120-second deadlines for brief/standard/deep; the
configured model timeout is an upper cap, not another ordinary user-facing detail knob.

Temperature, evidence bytes, response schema, and raw generation-token knobs are not
public alpha settings. A later advanced profile must be justified by a measured model or
use-case need rather than exposing provider internals preemptively.

## Local model contract

`autotldr setup` discovers active generation instances in LM Studio and writes one explicit
named model profile. Ordinary invocation does not download a model, choose among multiple
candidates, or mutate runtime residency. The first alpha certifies LM Studio because its
local management API distinguishes downloaded catalog rows from active instances. Generic
OpenAI-compatible endpoints remain future transport candidates until a provider adapter can
prove active state and its observed response profile passes conformance.

An eligible directly loaded LM Studio instance must expose the same exact identifier as its
catalog model key. A routed instance whose loaded ID differs from that key—including an LM
Link/Dynamo row—is excluded from setup and fails the pre-acquisition active-model check.
AutoTLDR does not invoke a route whose locality the provider inventory cannot attest.

The manifest records the exact endpoint class, served model identity, resolved detail
profile, evidence hash, response hash, validation result, and whether fallback was used.
Schema-valid citations constrain a model; they do not by themselves prove entailment. The
product evaluates supported model profiles on real tasks and states that evidence level
separately from transport conformance.

Findings are withheld from product claim input and remain visible as gaps because claims
cannot cite finding IDs. Narrow deterministic product dispositions also drop a behavioral
claim when it relies on signature-only code evidence and no cited non-signature unit states
that behavior, or a claim that names a recognized concrete measurement unit absent from
every cited unit's content. A number-unit quantity must likewise occur explicitly in cited
content; separate counts and units do not authorize a derived duration, range, or measure.
Likewise, a structured identifier named in prose must occur in the claim's cited content;
same-source proximity does not authorize it. Dropped model claims and named reasons are
recorded in the run audit. When cited prose explicitly distinguishes a same-named workbook
cell from a declaration, the cell's derived-formula predicate cannot be transferred to the
bare identifier. These measured guardrails do not replace human entailment evaluation.

Remote inference is off unless a later decision defines explicit authorization, privacy,
credentials, and data-boundary behavior.

## Evidence mode

The deterministic representation remains a public strength, but it is an explicit mode:

```bash
autotldr report.xlsx --model off --out json
autotldr ./experiment --model off --detail deep
```

Human evidence-mode output calls itself an evidence map, never a TLDR. It is useful for
audits, CI, debugging, unsupported model environments, and inspecting exactly what the
model was allowed to see.

## Configuration principles

- One local file remains zero-configuration after model setup.
- CLI flags override project configuration, which overrides user configuration, which
  overrides built-in defaults.
- `--no-config` produces a hermetic run.
- Project and user configuration use TOML from the standard library.
- Resolved configuration is validated before acquisition and committed into the manifest.
- Hard parser and container safety ceilings cannot be raised casually through preferences.
- Extensions remain explicit. Configuration may name imports; installed packages are
  never scanned or executed ambiently.
- Secrets are referenced through environment variables or a later credential provider,
  never stored directly in a project configuration.

## Presentation contract

Human output is organized for comprehension:

1. What matters: the cited prose TLDR and synthesis status.
2. Sources: acquired, declined, ignored, and missing inputs.
3. Native structure: formula, schema, outline, code, or data insights relevant to the
   requested detail.
4. Cross-source relationships, using readable labels rather than opaque IDs as the main
   visual language.
5. Gaps and limitations.
6. Selection, provenance, and full identifiers in a compact audit section.

JSON is the stable machine contract. JSONL is line-oriented but is not advertised as
progressive until acquisition and selection can safely stream. HTML is the shareable local
artifact. PDF preserves content and links but does not promise cross-process byte identity.

## Agent contract

Shell is the primary agent API. The installed Agent Skill teaches an agent to set a
budget, preserve gaps and omissions, resolve citations, and treat all source text as
untrusted data rather than instructions.

MCP is served only within explicitly configured filesystem roots. It exposes capability
discovery, structured results, and durable collection tasks over the same pipeline. No
A2A card ships; actual A2A deployment waits for a real client need, server, and matching
authorization design.

## Alpha acceptance

The reproducible engineering procedure is `acceptance/README.md`; the external-user gate
and preregistered measures are `docs/first-user-validation.md`. Invited participants
receive a version-bound, checksummed bundle whose guide names the support contact; they do
not install from a mutable checkout. Privacy-safe session records are evaluated by the
checked-in gate script rather than a retrospectively edited spreadsheet.

The alpha is ready for first users when:

- a clean Python 3.12 installation has one documented install path;
- `doctor`, `setup`, and runtime-derived format discovery make failures actionable;
- the ordinary command returns cited local-model prose for Tier 0–3 single files and mixed
  folders;
- `brief`, `standard`, and `deep` produce measurably different, bounded useful answers;
- the four lead jobs have real, non-hero acceptance corpora;
- wheel and source distribution contain only deliberate product files;
- human output is understandable without reading the IR specification;
- shell and root-scoped MCP agents cannot broaden source or model authority;
- the full suite, packaging checks, warnings-as-errors, determinism checks, and startup
  gate pass in CI; and
- first users beat a documented baseline on comprehension time or agent task quality at
  equal context budget.
