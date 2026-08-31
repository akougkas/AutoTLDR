# Changelog

AutoTLDR is pre-alpha. Nothing is stable yet, and this record names product-level changes
rather than promising semantic-version compatibility.

## Unreleased

No changes yet.

## 0.1.0a1 — first-user alpha candidate — 2026-08-31

- Ordinary CLI, watch, product Python API, and MCP runs use one configured local-model
  prose policy; deterministic representation is an explicit evidence mode.
- Added `brief`, `standard`, and `deep` product profiles that jointly control bounded model
  evidence, claim allowance, generation allowance, and human evidence presentation. Claim
  allowances are ceilings rather than verbosity targets.
- Added strict layered TOML configuration, local model discovery, setup, configuration
  introspection, runtime-derived format reporting, and a completion-probing doctor.
- Separated OpenAI catalog rows from active LM Studio instances so setup and invocation
  cannot trigger implicit model loading.
- Qualified no-reasoning synthesis for LM Studio and reject responses unless the runtime
  reports an empty reasoning channel and zero reasoning tokens.
- Removed the unused setup endpoint choice: the alpha runtime address is fixed and recorded,
  not presented as a setting that users can meaningfully change.
- Added bounded 60/90/120-second synthesis deadlines for brief/standard/deep while keeping
  provider timeout controls out of ordinary onboarding.
- Withheld uncitable findings from product claim input while preserving them as gaps, and
  added audited fail-closed dispositions for four measured authority errors: unsupported
  signature behavior, measurement units, composed number-unit quantities, and structured
  identifiers absent from cited content.
- Reorganized human output around cited claims, source outcomes, supporting native
  evidence, relationships, gaps, references, and an explicit presentation/selection audit.
- Added the public `summarize_product()` API.
- Root-scoped MCP now defaults to the same local prose pipeline and returns the actual JSON
  artifact as structured content. The nonexistent A2A endpoint advertisement was removed.
- The wheel contains a version-matched Agent Skill, installable with
  `autotldr integrations skill --install DIRECTORY`.
- Release builds exclude benchmark corpora, tests, working census drafts, and agent
  endpoint placeholders. CI runs the complete warnings-as-errors suite and audits both
  distributions.
- Added an independent non-hero Tier 3 corpus builder, a four-job product acceptance
  procedure, a claim-quality rubric, and a preregistered five-user validation plan.
- Added a deterministic checksummed private-alpha bundle with a required support contact,
  participant-facing wheel installation guide, privacy-safe session template, and strict
  evaluator for the preregistered five-user release gate.

## 0.1.0.dev0 — thin Stage 1–8 engineering slice

- Implemented one addressable representation, Tiers 0–3 native extraction, bounded Tier 2
  acquisition, measured role and fusion policies, strict local synthesis, watch mode, six
  output shapes, MCP Tasks, and an Agent Skill.
- Completed the 2026-08-31 Borealis functional local-inference proof. Independent zero-CPU-
  spill residency certification remained unavailable and was not inferred.
