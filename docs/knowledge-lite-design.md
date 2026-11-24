# Knowledge (B1) restoration — the simple design ("B1-lite")

Status: **proposal, not implemented** (tracking issue #610). Owner direction:
restore the AI/Knowledge feature, but keep it simple; a restored multi-identity
authority model costs too much to maintain.

## Principles

- One app, one WebDAV credential, **one folder the feature may write**.
- A human approves by moving a file in Obsidian. No approval artifacts, no
  executor, no receipts.
- Reuse what `main` already has (generators, evaluators, contracts, index,
  retrieval) and the helpers restored in #614; do not restore the old runtime.
- Every effect fails closed; nothing is reported as done without a readback.

## What the old design had, and what is kept

The removed feature ran on a dedicated "AI Writer" host with seven Linux
identities (sync, reader, embedder, generator, validator, evaluator, executor),
ACL-separated queues, a private ten-stage lifecycle under
`/var/lib/obsidian-ai/state`, ten projected stages under `04-AI/`, review and
execution artifacts, recovery and cleanup jobs and a production update/canary
toolchain (about 14,500 lines with its dependencies).

Kept as **invariants** (cheap, and they are what made the old design safe):

1. **Create-only.** A proposal is created with a conditional request
   (`If-None-Match: *`); an existing file is never overwritten, merged,
   renamed or deleted.
2. **Fixed write folder, enforced in code.** Only `04-AI/Proposals/` (a path
   guard, not an ACL). Nothing is written to `11-Knowledge/` or anywhere else.
3. **Input recursion boundary.** `04-AI/**` is never read as input.
4. **Validate before write.** A proposal that fails `knowledge_validator` or
   the note policy is not written.
5. **Marked and traceable.** Proposal front matter records
   `type: knowledge-proposal`, `status: proposed`, the source (provider scope
   and content digest), the recipe/model identity and the validation result.
6. **Idempotent.** A small private SQLite journal keyed by input digest +
   recipe digest prevents regenerating or re-writing the same proposal.

Dropped (not restored): the ten-stage lifecycle, per-role queues and
identities, human-facing lifecycle projection (with cleanup and recovery),
executor/transport roles, approval/receipt artifacts and the production
update/canary/acceptance tooling.

## Pipeline (v0, manual)

```text
selected conversation (private import inventory)   or, later, a Vault note
   -> context       context_bundle + context_selection (restored in #614)
   -> generate      openai_generator against the TSUMGI endpoint (existing; endpoint is configuration)
   -> validate      knowledge_validator (restored) + knowledge_note_policy / layout
   -> (evaluate)    existing evaluators, optional
   -> propose       one Markdown file, conditional create in 04-AI/Proposals/
   -> approve       the owner moves accepted notes to 11-Knowledge in Obsidian
```

Authority is the same credential Core Promotion and Daily already use; the
feature adds no new account.

## Reuse map

| Module | Plan |
| --- | --- |
| `context_selection`, `knowledge_validator`, `retrieval_coverage`, `retrieval_eval` | restored (#614) |
| `generator_contract`, `generation_artifact`, `evaluator_*`, `evaluation_artifact`, `openai_*`, `ollama_*`, `knowledge_index`, `knowledge_note_*`, `semantic_*`, `context_bundle`, `artifact_lifecycle` | already in `main`; reused as they are |
| `semantic_objective_generation`, `knowledge_review` | restore only if the pipeline needs them (decided in phase 1) |
| `ai_input_planner` (automatic source selection) | optional, phase 4; v0 selects the source by hand |
| `execution_orchestrator`, `production_orchestrator`, `human_projection*`, `human_recovery`, `pre_review_*`, `review_intake`, `semantic_production_acceptance`, `admin_ai_status` | not restored |

## Phases

1. **Pipeline library + manual CLI**, tested against local files and a fake
   WebDAV; no Vault access.
2. **Write proposals** to the Vault (opt-in flag, manual run, readback check).
3. **Optional panel/Job** in the single app (same opt-in pattern as the import
   panel); no schedule unless asked for.
4. **Automatic source selection** only if manual selection proves tedious.

Each phase is its own issue/PR, off by default, and deploys only with explicit
approval.

## Decided

- **LLM: the local TSUMGI API** (OpenAI-compatible, `https://tsumgi.arc.upiscium.dev/v1`,
  models `qwen3.6-35b-a3b` plus the `-fast` / `-short` variants). The existing
  `openai_compatible`, `openai_generator` and `openai_evaluator` already speak
  this protocol, accept a remote HTTPS endpoint, never inherit proxy settings
  (the context may hold private notes) and bound the response, so no new client
  is needed. The endpoint and model are configuration, not code.
- **Claude API is out of scope.** Its request format differs from the
  OpenAI-compatible one and would need a second client; it can be reconsidered
  later behind the same generator contract.
- Because local-model quality is still being evaluated and TSUMGI runs one
  request at a time, generation is **single-flight**, and the validator plus
  the human approval step are the quality gate: a weak proposal costs a
  review, never a Vault change.

## Decisions still needed from the owner

1. Approval by moving the file in Obsidian (recommended) versus a front-matter
   flag plus a publish step.
2. Proposal folder `04-AI/Proposals/` (flat, replaces the ten-stage layout).
3. First source: imported conversations (ChatGPT / Claude) first, Vault notes
   later.
