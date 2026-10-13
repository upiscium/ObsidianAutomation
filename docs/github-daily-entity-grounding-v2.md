# GitHub Daily v2 — singleton Issue/PR Grounding (experimental)

- **Issue:** [#330](https://github.com/upiscium/ObsidianAutomation/issues/330)
- **Parent:** [#324](https://github.com/upiscium/ObsidianAutomation/issues/324)
- **Dependency:** [#326 / Draft PR #327](https://github.com/upiscium/ObsidianAutomation/pull/327), which in turn depends on [#325](https://github.com/upiscium/ObsidianAutomation/pull/325)
- **Source-route authority:** [#328 / Draft PR #329](https://github.com/upiscium/ObsidianAutomation/pull/329)
- **Status:** Nonpublishing **experimental primitive**. NOT wired to Ollama, Scheduler, Renderer, Daily Writer or Nextcloud

## Purpose

The accepted Daily output consists only of substantive **Issue/PR progress**
and **verified same-day PR merges**, grouped by repository. The v1 model
summarizes by repository and kind; its single claims may cite more than one
Issue or PR. The v2 entity-scoped context (#326) introduces a deterministic
one-entity candidate shape, but the candidates are still model text, not
evidence-backed final claims.

This slice uses existing **validated original Evidence bytes** to construct a
bounded Grounding model input containing **exactly one candidate** and its
1–8 actual cited original GitHub events. No alternative Issue number,
repository, URL, merge/deployment state, source ref or publication permission
may be chosen by the model.

```text
Original v1 Evidence bytes (SHA verified, immutable)
   + #327 deterministic Issue/PR Context and candidate response
   |  normalize_entity_candidates(original Evidence mandatory)
   v
SingletonEntityGroundContext v2
   exact Evidence SHA / partial Context SHA / candidate response SHA
   exact entity / candidate SHA / original event IDs and original bytes
   |   model may return ONLY verdict + reason
   v
EntityGroundingAssessment v2
   source candidate ID and original source identity inherited,
   verdict supported|unsupported, bounded reason SHA-256 only,
   original model response SHA-256 (never raw explanation prose)
   PUBLICATION_AUTHORIZED = FALSE
   REQUIRES_HUMAN_QUALITY_REVIEW = TRUE
```

## Contracts

- All input contexts MUST be regenerated against the original exact Evidence
  bytes before model-bound Grounding context generation, **and again** before
  accepting any returned model verdict. Self-consistent hash text or forged
  in-memory objects do not suffice.
- The candidate identity is computed by #327 from the actual source-ref
  integers, one entity, original event IDs and parent Context SHA. One
  candidate produces exactly one Grounding Context. An empty candidate
  response produces **zero contexts**, but it **never** proves no progress.
- Input serializations are versioned, sorted-key canonical JSON with bounded
  source count and byte budgets (256 KiB max Grounding input, 8 KiB reply).
  Unexpected model fields, duplicate JSON keys, bad encoding, wrong verdict
  or multiline (including Unicode line/paragraph separators), invisible
  formatting/control characters or oversized reasons fail closed.
- Grounding model schema permits **only** `verdict` and `reason`. A
  `supported` response is a **model advisory assessment**, not proof that a
  genuinely material issue advanced or that its status is merged/deployed.
  The raw free-text reason is validated and immediately reduced to a SHA-256
  digest; it is **not copied into the normalized assessment**. A model could
  fabricate an Issue number, URL or merge assertion in its explanatory text,
  so that free text must never reach the user-visible Daily or an operations
  API. The typed verdict and original source bindings are the only carried
  information.
  Unsupported candidates cannot become user-facing progress by themselves.
- The response retains exact Grounding context SHA and original candidate
  SHA; identical original inputs and verdict bytes yield identical results.
  Any foreign Context/Issue/PR, changed original Evidence, swapped batch,
  altered candidate source-ref, forged in-memory candidate or model-specified
  identity is rejected before the response is trusted.
- No repository-wide prose, standalone unbound commits or old Issue/PR
  conversation comments are attached. #327 quarantines `/issues/N`
  discussion and PR commits when v1 does not attest the GitHub collector
  API route. Even if an LLM classifies those as progress, its output **cannot
  remove that quarantine**. Integration with #329 requires a separate
  independently reviewed, producer-sealed source-route receipt.
- Existing immutable 2026-10-07 Canary and v1 Daily Summary/Projection/CAS
  schemas are unchanged; `# Project Progress` Writer remains a distinct
  human-accepted effect boundary. No CLI/service calls this module in
  production.

## What is deliberately missing

This layer does **not** invoke a real Grounding model, attest its
model/prompt/options provenance, seal its response in a private CAS,
establish human semantic materiality (#290), merge multiple supported
candidates into one *Japanese* sentence per Issue/PR (#283), or produce a
v2 Daily Projection. Those need independent typed contracts, end-to-end
evidence/source closure, full-day cost and omission qualification, and
fresh-date previews before separately approved production CAS + Timer
rollout.

## Test design

Synthetic immutable GitHub Evidence with exact Issue/PR number identity,
strict dynamic v2 candidate response, one/two source citations, singleton
supported/unsupported outcomes, cross-entity/context/batch drift, duplicate
model keys, attempts to inject merge/URL/authority, malformed/oversized
responses, empty candidate case, and deterministic replay. All tests are
local, mock-free at the typed function level and have **zero network or
filesystem effects** beyond pytest's normal cache activity. A green test
or model verdict does not approve or simulate live Writer acceptance.
