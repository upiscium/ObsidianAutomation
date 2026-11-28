# Knowledge (B1) restoration — the simple design ("B1-lite")

Status: **phases 1 and 2 implemented** (#616: manual CLI without Vault access; phase 2: opt-in `--publish`, below); later phases are proposals (tracking issue #610). Owner direction:
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
  models as listed by `GET /v1/models`; on 2026-11-27: `qwen3.8-27b` (input 122,880 / output 8,192
  tokens), `qwen3.8-flash-next-coder` and `qwen3.6-35b-a3b-short` (input only 12,288)). The existing
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

- **Approval is a button in the proposal note**, not a manual move and not a
  new mechanism. ObsidianCore already has the "Knowledgeに昇格" (promote to
  Knowledge) action: it moves a note to `11-Knowledge/`, swaps the managed
  metadata embed for `knowledge-meta`, sets the Knowledge front matter and
  refuses to overwrite. Core PR upiscium/ObsidianCore#247 adds
  `type: knowledge-proposal` to its promotable types with a
  `knowledge-proposal-meta` embed that carries the same button. Approving is
  therefore one click on the proposal itself, and the Automation side needs no
  approval code, artifact or executor.
- **Proposal folder `04-AI/Proposals/`** (flat), replacing the ten-stage layout.
- **First source: imported ChatGPT / Claude conversations**; Vault notes later.

## Proposal note contract (shared with ObsidianCore)

```text
---
type: knowledge-proposal
status: proposed
category: <knowledge policy value>
maturity: draft
source_type: <knowledge policy value>
---

```meta-bind-embed
[[knowledge-proposal-meta]]
```

<generated note body>

<!-- oa-proposal: {"v":1,"scope":...,"source_sha256":...,"context_sha256":...,"generation_sha256":...,"model":...} -->
```

Only the five Knowledge-policy front matter keys are used (no extra keys), so a
promoted note stays valid under `knowledge-note-v0`. Provenance lives in the
trailing HTML comment. Generated text is refused if it contains anything that
would execute in Obsidian (Meta Bind / Dataview / Templater blocks, inline
`BUTTON[` / `INPUT[` / `VIEW[` / `JS[` controls, `obsidian://` or
`javascript:` links, script-like tags); plain code fences are allowed.

## Phase 2: write a proposal to the Vault (opt-in CLI)

```sh
python -m obsidian_automation.knowledge_proposal propose --publish \
  --state /var/lib/obsidian-snapshot --zip export.zip --provider claude --index 3 \
  --ai-root <private artifact dir> --base-url https://tsumgi.arc.upiscium.dev/v1 --model <model>
```

Run it as the service user (it reads the shared saved connection). Without
`--publish` the CLI behaves exactly as in phase 1 (`--out`, no Vault access);
`--publish` and `--out` cannot be combined.

- **Create-only.** One `PUT` with `If-None-Match: *` to
  `04-AI/Proposals/<name>.md`; parents are created and verified as DAV
  collections. An existing file with different bytes is never touched
  (`proposal_exists`); identical bytes report `already_present` with no write.
- **Fixed folder, enforced in code,** and only `*.md` directly in it. The note
  must be a `knowledge-proposal`, carry the managed embed exactly once, and
  contain no other executable Obsidian content.
- **Same authority as Daily:** the shared connection plus the pinned-root guard
  (`vault_root_refused` stops before any request). No new account.
- **Readback:** success (`created`, or `recovered` after a lost response) is
  reported only after a `GET` returns the exact bytes. A lost response with
  nothing written is `outcome_unconfirmed` (`vault_write: "unknown"`), never
  success. The ETag is not required (Apache answers a fresh write with a weak
  one unless `FileETag Digest` is set); nothing is updated afterwards.
- **Idempotent:** a private journal (`<state>/knowledge-proposals/journal.sqlite3`)
  keyed by source digest + model + pipeline revision + prompt stops the same input
  being generated and written twice; a failed attempt is not recorded and can be
  retried. Exit codes: 0 on `proposed` / `already_proposed`, 1 on any error.
- **Not wired to any schedule or panel** (phase 3). The first write to the
  production Vault needs its own approval.

## Phase 3: panel and opt-in schedule (`--knowledge-ui`)

Start the app with `--knowledge-ui --knowledge-llm-url <OpenAI-compatible base URL>
--knowledge-model <name>` (optionally `--knowledge-api-key-file`). Without the flag
the routes return 503 and the panel never appears. The LLM endpoint and key are
start-up arguments, not panel fields.

- **Panel "知識提案（ローカルLLM）".** Lists the conversations already imported
  through the chat-import panel (provider, message count, size, first day, state;
  never titles or text) and runs **one proposal at a time** (the local LLM handles
  one request at a time). Routes: `GET /api/v1/knowledge`, `POST
  /api/v1/knowledge/conversations` (load the list), `POST /api/v1/knowledge/runs`
  (`{archive, index}`, manual), `POST /api/v1/knowledge/settings`
  (`{enabled}`, the schedule switch); exact Origin and one-use CSRF as elsewhere.
- **Same engine as the CLI.** A run is `generate_and_publish` (create-only into
  `04-AI/Proposals/`, exact-byte readback, pinned-root guard) and shares the CLI's
  journal, so a conversation proposed by either is never generated twice. The
  states are `new`, `proposed`, `failed`, `unconfirmed`, `empty` and `too_large`.
- **Long conversations are proposed part by part.** A conversation above the model's
  input limit (`--knowledge-max-source-bytes`) is cut at message boundaries (an
  over-long message at line boundaries) into at most 8 parts; each part is its own
  source with its own journal key, run and proposal note, and starts with a
  `[長い会話の一部 i/n]` line. The schedule takes parts in order, under the same
  daily and pending limits. A conversation needing more than 8 parts stays
  `too_large`. Part number and count are shown in the panel; no text is.
- **Opt-in schedule, brakes first** (all start-up arguments):
  `--knowledge-interval-seconds` (default and minimum 3600),
  `--knowledge-max-per-day` (default 3, per Japan-time day) and
  `--knowledge-max-pending` (default 10). It is off until switched on in the panel
  (with a confirmation that names the limits), never catches up after an outage, and
  each slot makes at most one proposal: the **oldest new conversation** (by first
  message day). A slot is skipped, with a recorded reason, when a run is active, the
  daily cap is reached, the number of `*.md` files waiting in `04-AI/Proposals/`
  (read from the Vault before each scheduled run) is at or above the limit, or that
  count cannot be read (fail closed). A conversation whose run failed or is
  unconfirmed is skipped until a person retries it by hand.
- **Manual runs ignore the daily cap and the pending limit** (a person chose the
  conversation); they still need the root guard and the create-only rules.
- A run left `queued`/`running` by a restart becomes `unconfirmed` and is never
  replayed. Run records hold fixed codes only (no exception text, paths or note text).
- The first write to the production Vault needs its own approval; the schedule stays
  off until the owner switches it on.

- **Model choice (measured 2026-11-27, local files only).** `qwen3.8-27b` wrote natural Japanese
  proposals for a 10.7 KB, a 57 KB and a 129 KB conversation in 44 s, 66 s and 106 s;
  `qwen3.6-35b-a3b-short` was also Japanese but cannot take more than about 16 KiB;
  the coder models answered in English. TSUMGI rejects `response_format`, so run with
  `--knowledge-no-structured-output`. A conversation the panel has proposed (any model) is
  not proposed again by the schedule, so changing the model does not create duplicates.
