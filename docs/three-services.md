# Three-service cutdown — implementation and production gate

**Status:** source-only implementation on an isolated branch. **No deployment,
Vault mutation, credential rotation, GitHub Core push, or existing timer
disablement is authorized by this document.**

## What remains

- **Snapshot:** Existing `SnapshotJob`, `SnapshotAdapter`, persisted
  `snapshot.sqlite3`, nonroot Gitea credential and verified readback.
- **GitHub Daily:** Existing `DailyJob`, deterministic Preview, bounded
  source capture/verification, CAS/conditional section-only Daily writer.
  `--daily-publish` is still required for actual Daily publication. New runs
  bind the exact private Nextcloud source generation; legacy/unbound rows
  are historical, never treated as verified results of a new Vault source.
  HTTP responses expose only a current-binding Boolean, not credential hashes.
- **Core Promotion:** New `CorePromotionJob`, source binding, OFF-by-default
  durable SQLite schedule, explicit private checkpoint bootstrap and existing
  managed `run_promotion_cycle`. The old write transport retains policy SHA,
  content-addressed plan/receipt, conditional ETags, and live-drift detection.
- **Management:** One HTTP server with precisely three workflow routes,
  one-use CSRF, strict Host/Origin rules, bounded requests, private bind only,
  fixed Nextcloud/Gitea/GitHub connection endpoints and no generic executor.

## Removed from the active profile

AI Knowledge and Review, ChatGPT Export/External Source, GitHub Project Writer
and B3 observer, Vault Formatter, old root Operator/systemd console, phase
scheduler, arbitrary-workflow support and their WebUI cards/routes. The Daily
GitHub **read-only collector** remains because Daily needs source evidence.
The existing **Gitea Public Publisher** remains an external Snapshot→Core
projection dependency; there is no independent publisher management panel.

Retained internal utilities are not automatically active jobs. Their presence
must not be mistaken for authority to run abandoned workflows.


## Restored as manual tools (#610)

Features removed in the three-service cutdown are being restored one at a
time (tracking issue #610).

**Chat-history import (ChatGPT and Claude).** The export tools (inspect,
normalize, private intake, source context, evidence candidates) are back as
standalone, standard-library modules (`python -m
obsidian_automation.chatgpt_export_inspect`, `..._intake --provider
{chatgpt,claude}`). They never touch the Vault, WebDAV or the network; see the
`docs/chatgpt-export-*.md` contracts.

* **Claude** (claude.ai "Export data") arrives as a manifest plus one ZIP per
  category: `conversations-NNN.zip`, `projects-*`, `memories-*`, `frames-*`,
  `claude_code_cloud-*` and `light_metadata-*` (account data; never imported).
  Only `conversations-NNN.zip` (a single `conversations.json`) is a transcript
  source; import each `conversations` part separately. The download links in
  the manifest need the signed-in claude.ai session and expire after 24
  hours, so download the files in a browser. Support covers inspect,
  normalize and intake (parser contract `claude-linear-v0`) and was verified
  against a real 2026-11 export (21 conversations):
  * the current branch is the chain of `parent_message_uuid` links from the
    last message (the list also holds edited/regenerated alternatives, which
    are counted as off-branch, not imported);
  * the top-level `text` is the final reply and wins over `content` blocks
    (which also hold interim narration between tool calls); `thinking`,
    `tool_use`, `tool_result`, `injected_prompt_block`, `image` and `document`
    blocks are never retained; messages with no visible text are counted as
    omitted;
  * without parent links the list is treated as one linear branch and reported
    as unverified. Source-context and evidence-candidate layers remain
    ChatGPT-only until the AI/Knowledge feature (B1) is restored.
* **A scope is bound to one provider** (`private-chatgpt`, `private-claude`);
  reading a ZIP with the wrong provider is rejected and recorded, never
  half-imported.
* **Import panel (opt-in).** Start the app with `--chat-import-ui` to add a
  collapsible "チャット履歴の取り込み" section: upload one export ZIP (limit
  2 GiB, one at a time), automatic provider detection, inventory preview and
  commit. Routes: `GET /api/v1/chat-import`, `POST
  /api/v1/chat-import/upload` (`application/zip`, exact Origin + one-use CSRF,
  30 s idle / 1 h total deadline) and `POST /api/v1/chat-import/inventory`.
  Data lives in a `0700` `chat-import/` directory under the service state,
  outside the Vault. Every response is count-only (no titles, ids or text).
  It writes nothing to the Vault, queues no Generation job and has no
  schedule. Without the flag the routes return 503 and the page never names
  either provider.

**Knowledge proposals (B1-lite, phase 1: manual CLI, no Vault).**
`python -m obsidian_automation.knowledge_proposal list|propose` turns one
imported conversation (ChatGPT or Claude export ZIP, one conversation per
run) into a single proposal note using the existing OpenAI-compatible
generator against the configured endpoint (the local TSUMGI API). The note is
written to a local directory only (`--out`), created exclusively (an existing
file is never overwritten), never to the Vault. It carries
`type: knowledge-proposal`, `status: proposed`, the metadata embed
`knowledge-proposal-meta` and a machine-readable provenance comment; generated
content that would execute in Obsidian (Meta Bind, Dataview/Templater blocks,
`BUTTON[` / `INPUT[` inline controls, `obsidian://` and `javascript:` links,
script tags) is refused before anything is written. Approval is the existing
"Knowledgeに昇格" button in ObsidianCore (upiscium/ObsidianCore#247), which moves
the note to `11-Knowledge/`. A conversation larger than 128 KiB is refused
(`source_too_large`). Design: `docs/knowledge-lite-design.md`.

**AI/Knowledge (B1), step 1: library only.** `context_selection`,
`knowledge_validator`, `retrieval_coverage` and `retrieval_eval` are back
(contract: `docs/retrieval-eval-v0.md`). They are in-process helpers with no
service, route, schedule, network or Vault access. The orchestration layer
(pre-review jobs, workers, human projection, production update), the review
intake and `knowledge_review` are still removed and come back in later steps
(tracking issue #610).

## App control contract

| API | Behavior |
| --- | --- |
| `GET /api/v1/{snapshot,core-promotion,daily}` | Per-job persisted settings, last attempt and result |
| `POST /api/v1/{name}/settings` | Set enabled/interval; refuses invalid or unconfigured |
| `POST /api/v1/{name}/runs` | Durable request-ID admission; HTTP 202 is **not** effect success |
| `GET /api/v1/{name}/runs/<run_id>` | Query exact result of the attempt |
| `GET /api/v1/core-promotion/configuration` | Read-only readiness/policy SHA |
| `POST /api/v1/core-promotion/bootstrap` | Explicit previously reflected Core baseline; no Vault PUT |
| `GET /api/v1/session` | One-use CSRF token |
| `GET/POST /api/v1/*/connections` | Fixed credential setup with bounded validation |

Routes for Formatter, B3, AI, ChatGPT Import, old systemd operations, and
`/api/v1/workflows` are **not mounted**. All mutable routes require an Origin
matching the bound HTTP Host and a one-use CSRF token. No arbitrary host path,
unit name, command or script is accepted.

## Core Promotion safety

1. The internal publisher policy bytes must equal the bundled reviewed
   `public-export.toml`; invalid/missing policy is not silently recreated
   after a mismatch.
2. Core Promotion starts **OFF**; a missing checkpoint, mismatched policy,
   missing Nextcloud connection, mismatched private Vault-target seal or
   changed source fingerprint refuses enabling. The durable target seal
   includes the server, account, selected Vault folder and enrolled root pin.
3. Checkpoint bootstrap must use a complete 40-character ancestor commit
   whose subject matches the generated Gitea public projection, and which is
   independently confirmed reflected in the actual canonical Vault. The
   source code cannot prove this last fact automatically.
4. Vault-target changes performed through the connection UI create a
   persistent quarantine marker **before** the new connection is saved.
   Switching back to the prior target does not clear it. The old checkpoint
   must be independently reconciled and explicitly reauthorized; deleting
   the marker to bypass validation is prohibited. Credential-only changes
   for the same target do not invalidate the sealed Vault identity.
5. Every run reuses the existing exact policy/plan/conditional WebDAV transport.
   The `CorePromotionJob` is an admission/status wrapper, not a replacement
   for conflict checks or canonical readback.
6. On crashes, lost responses or an unclassified exception, result is
   **indeterminate**. No automatic replay or new request ID is allowed until
   the operator has reconciled the private checkpoint, receipt and Vault state.
7. Separate Snapshot and Daily jobs remain operational if Promotion is
   unavailable or fails. Do not treat a public Core Git SHA as proof that
   Live Vault has been updated.

## Production migration acceptance — not yet executed

The current `obsidian-control` unit is `obsidian-snapshot.service`,
running from a versioned release under `/opt/obsidian-snapshot/releases/`
as nonroot `obsidian-snapshot` with persisted
`/var/lib/obsidian-snapshot`. Its **currently installed command includes**
`--vault-root-ui --vault-bindings-ui`; those options must be removed from
the new ExecStart, or the new binary would reject the command at startup.

Before any cutover, in this order:

1. Record exact running binary/source SHA, current B2/B4 status/settings, the
   ongoing run IDs and their terminal receipts. Inspect root/mapping pins and
   the selected Vault root without exposing secrets.
2. Verify no legacy Core Promotion canonical writer is still enabled or able
   to restart. Stopping an LXC once is not proof of credential revocation.
3. Preserve existing state/SQLite credentials, policy, immutable receipts,
   publisher access and full old release path. **Never purge live state.**
4. Build and stage the new source in an isolated release; run new component
   and negative-route tests. Do not modify the live unit yet.
5. Qualify Core Promotion's exact policy and generated baseline against the
   actual Vault, using read-only checks. Only then bootstrap its **new private**
   checkpoint and explicitly enable the schedule if approved.
6. Update the service unit's ExecStart on an approved cutover, preserving
   `--state`, bind address, and `--daily-publish` only if the existing Daily
   Writer authorization is still valid. Omit the two retired Vault UI flags.
7. On restart, verify B2/B4 schedules, Run Once admission, eventual persisted
   result and actual Gitea/GitHub/Nextcloud readback, then Core Promotion
   initial no-op/changed-policy scenarios and conflict refusal. Test desktop
   and Android WebUI. A mock-only or `202` response does not pass E2E.
8. On failure, disable newly admitted jobs, reconcile any indeterminate
   remote effect first, and restore the previous source/ExecStart without
   deleting newer canonical Vault data. Do not operate both old and new
   canonical writers concurrently.

The old separate Gitea Publisher/Runner may be required for the
`ObsidianVault` → GitHub `ObsidianCore` projection. Keep it outside the
three-panel UI but **do not stop it as an incidental consequence** of the
code purge.

## Deferred

No cross-Vault mapping editor, generated Daily/Monthly/Recurring/Subscription
jobs, AI enhancement, full-Vault Formatter or new infrastructure. They may be
reintroduced only as independently scoped requirements.

## Vault Settings (three-panel UI extension, #598)

The common **Vault管理設定** section is not a fourth managed workflow.
It restores the previously reviewed verified WebDAV root-selection flow
(candidate enumeration, Preview, explicit activation, one-step rollback,
trusted account enrollment) and the previously reviewed restricted
`daily`/`project` mapping flow (System stays fixed at `98-System` until Core Promotion supports another target) (Preview, save, reset,
one-step rollback). Root credentials are never returned. Raw Vault contents
are not read for this UI, and none of the configuration actions creates,
moves, deletes or overwrites a Vault note.

A new single-app fence serializes configuration changes with Snapshot,
GitHub Daily and Core Promotion, including the Promotion effect gate and
indeterminate result admission. Any root/System mapping target change
durably quarantines the existing Promotion checkpoint before the
configuration's pending marker is released. Any change pauses all three
schedules. Changes to root or System mapping require explicit Core
requalification; do not delete the quarantine marker as a shortcut.
Configuration Preview is not a confirmed canonical Vault edit.

This extension remains source-only until reviewed and separately deployed.

## One-field settings and individual configured markers

The common Settings UI contains **one save control and one explicit status for
each Nextcloud (4), Gitea (4), GitHub (1) value**. The Daily/Project Vault
folder mappings likewise have distinct Preview and Save actions. The System
mapping is displayed as fixed at `98-System` until Core Promotion supports
other destinations. An existing legacy nondefault System mapping is marked
incompatible and blocks individual mapping changes until an explicit reset.
A stale/quarantined Core checkpoint is likewise never shown with a green
success checkmark. Vault root/pin and Core bootstrap show their own status.

New setup values can be saved one at a time. Until a complete provider
configuration is present, values are held only in a bounded private
`connection-field-drafts.json` (0600 regular file, no symlink reads). Draft
status is **saved but unverified**, not a green checkmark or an active source.
When a provider has all required inputs, the existing full-schema validation,
read-only credential probe, target-identity quarantine, single-writer Job
fence, atomic private credential store, and schedule-pausing gates are used.
If another provider prerequisite becomes ready *after* a complete draft was
saved, an explicit **Verify saved settings** action applies it without
requiring any secret to be reentered.

`GET /api/v1/{nextcloud,snapshot,daily}/connections` includes
per-field `verified | saved | stale | missing` codes, a
`ready_to_verify` Boolean and a `values` dictionary restricted to
**nonsecret** fields. The saved (or staged) WebDAV URL, Vault folder,
Nextcloud username, Gitea Git URL, username and branch are displayed in
their corresponding inputs. Tokens and passwords are never returned, not
even encrypted or partially revealed. A field whose secret is configured
(or staged) gets only a **constant eight-circle placeholder** in the
password input, while the input's actual `value` remains empty; an empty
password field cannot overwrite the saved secret with its visual mask.
A field not configured gets no fake mask. Refresh never overwrites an
unsaved edited input. `verified` means the configured provider passed
the normal save-time probe; it is not a claim of continuous live health.
A stale GitHub token after Nextcloud source rotation is not shown as verified.
New fixed `POST .../connections/field` endpoints accept only
`{field,value}` with the existing CSRF/Origin checks and strict field
allowlists. Fixed `POST .../connections/verify` requires `{}` and only
commits a complete private draft. The legacy all-fields-at-once setup routes
remain for existing clients.

A configuration update never silently activates a schedule, invents a
fourth workflow, or publishes a Vault note. The operator can see
`✓ 保存済み`, `◌ 一時保存（未検証）`, `● 変更中`, or `未設定` on
each independent field. Secret inputs remain blank after refresh.

## Phase-locked Scheduler V1 (GitHub Issue #533)

The existing **single embedded scheduler** calls the three independently
persisted Job tick hooks; no additional timer, worker, LXC or privileged
systemd action is added.

The optional anchored schedule is explicitly entered in each existing
Snapshot/Core Promotion/GitHub Daily panel: a supported JST `HH:mm` anchor,
an integer interval in seconds and enabled/disabled. Each individual value
shows a persisted/unsaved marker, but the interdependent anchor and interval
are committed together after one full schedule Preview. Preview computes
the next **three** `Asia/Tokyo` wall-clock slots *without editing state*.
Only minute-granular intervals dividing 86400 are accepted, with the existing
minimum per Job (Snapshot/Promotion: 60s; GitHub Daily: 3600s).
Thus `12:00 + 3600s` at JST 12:37 first fires at 13:00, not at 13:37.
DST transitions do not apply because V1 is explicitly JST-only.

The SQLite migrations only add versioned phase columns; existing settings,
enabled states, pending intervals, run IDs and receipts remain untouched until
an operator explicitly saves an anchored value. Save requires a fresh matching
Preview in the WebUI and a matching `expected_revision` in the settings API.
Once anchored, old-form HTTP updates are refused; only internal trusted Job
pauses may carry the old schema, and these preserve the prior anchor.

At every scheduled tick, the next due is projected onto the original phase,
not calculated as `now + interval`. More than five seconds past a slot or an
overlapping Job is a **skipped slot**, never a catch-up burst or an unconfirmed
effect replay. Core Promotion's existing unresolved effect gate remains
enforced. The manual Run Once path is independent.

V1 does **not enable live timers**. Synthetic checks cover exact wall-clock
JST and cross-midnight slots, invalid values, stale revisions, persistence,
legacy SQLite upgrade, interrupted/busy Jobs and missing CSRF.

## Snapshot run retention (opt-in)

Each Snapshot attempt keeps a mirror, a bare fetch and a worktree under
`state/runs/<run_id>` (about 215 MB for the current Vault; hourly runs add about
5 GB a day), and nothing removed them. Retention is **off by default**.

* **Service:** start with `--keep-runs N` (N >= 2) to delete whole run
  directories older than the newest N runs, once after each finished run. The
  result is printed to the journal (`snapshot_retention deleted=… failed=…`) and
  never changes a run's outcome.
* **Never deleted:** queued / running / unconfirmed runs and the most recent
  successful run, however old. Only directories named by a 32-hex run id that
  also have a `runs` row are candidates; other entries are never touched. A
  candidate must be a plain directory owned by the service user directly inside
  `state/runs` (symlinks are refused). The `runs` table rows are kept.
* **Dry run first:** `python -m obsidian_automation.snapshot_retention --state
  <state> --keep N [--list]` prints what would be deleted and changes nothing;
  add `--apply` to delete. Run it as the service user. Enabling `--keep-runs` on
  a host with many old runs deletes all of them on the next run, so check the
  dry run first.
