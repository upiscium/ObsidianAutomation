# WebUI workflow control contract v1

## 1. Status, authority, and scope

- Contract ID: `webui-workflow-control/v1`; document revision: **2**, 2026-10-28.
- Run: `OA298-API480-W0-20261027-R1`, bounded continuation packet revision 2.
- Source-only P1 correction: `OA298-API480-P1-CORR-R1-20261028`, instruction
  revision 1, finding `OA298-API480-P1-001`; independent re-review still required.
- Source baseline: `c569f924da84c9adaa7d50107ee488ff894f2496`.
- Authority: [#480](https://github.com/upiscium/ObsidianAutomation/issues/480)
  AC-API-01..06 and its linked parent [#298](https://github.com/upiscium/ObsidianAutomation/issues/298),
  effective owner-approved 2026-10-24 UX-first MVI, as specified in the task packet.
- **Source-only contract draft for coordinator review.** DTO vocabulary and port
  semantics below are the v1 freeze candidate for independent development, not
  proof of approved deployment, attached adapters, or implemented endpoints.
  Scheduler choice remains human-owned and unresolved (section 10).
- This change implements **no** B1/B3/B5 runtime, shared router, migration,
  authentication system, provider call, timer, or production effect.

#480's acceptance text was read directly in the initial session. The continuation
does not retry the denied GitHub comments command. The parent AC-03/04/05/07
cross-check must be signed off by the coordinator against its independently read
#298 text; this document does not invent or quote unavailable parent AC wording.
Old wireframes are presentation references, not execution authority.

Normative **MUST** statements describe the proposed contract unless explicitly
labelled **existing**. A future implementation must pass the relevant acceptance
checks before advertising its connected actions. Merely implementing an interface
does not satisfy production acceptance.

## 2. Evidence inventory and current integration truth

All paths below are relative to the repository root. Reads and tests are not an
invitation to invoke their CLI entry points or live workers.

| Product | Existing source/interface | Existing common WebUI observation at baseline |
| --- | --- | --- |
| B1 AI-derived notes | `pre_review_job.py`: `parse_recipe`, `submit_job`, `job_status`, `stage_output`, attempts/retries/generations | `pending`; configuration/schedule not observed; no common control |
| B2 Vault Snapshot to private Gitea | `snapshot_job.py`: `settings/status/update/submit/run/tick`; `snapshot_adapter.py` verifies the destination ref | Connected only if `SnapshotJob` is explicitly attached; otherwise `not_attached` |
| B3 Project data into Vault | `github_project_watcher.py`, `github_project_watcher_runtime.py`, `github_project_overview_queue.py`, `github_project_status_queue.py`, `github_project_overview_worker.py`, `github_project_status_mutation.py` | `pending`; watcher/queue source or an old service being active is not attachment |
| B4 GitHub progress into Daily Note | `github_daily_job.py`: `DailyJob.settings/status/update/submit/run/tick`; optional publisher | Connected only if attached; without attachment the catalog says `pending` |
| B5 ChatGPT Export intake | `chatgpt_export_intake.py`: `publish_archive`, `inventory_ready`, `PublishReceipt`, `InventoryReceipt` | `pending`, manual-only `not_applicable` schedule; no upload route |

`unified_workflows.py:workflow_catalog` currently returns exactly `version`,
`authority: observation_only`, and five ordered `workflows` (`b1`..`b5`). Each
entry has `id,label,note,integration,configuration,schedule,last_result,control_available`.
Existing schedules use `on/off/not_observed/not_applicable`, not the new object
below. `last_result` is a last-run status, **not** a historical verified-outcome
field. Preserve that meaning. The catalog deliberately omits run IDs, refs,
private paths, payloads, and raw exception messages.

Source tests inspected: `test_unified_workflows.py`, `test_snapshot_job.py`,
`test_github_daily_job.py`, `test_github_daily_deterministic_writer.py`,
`test_pre_review_job.py`, `test_github_project_overview_integration.py`, and
`test_chatgpt_export_intake.py`. These contain synthetic-provider/local fixtures;
inspection alone is not an execution pass.

Important existing boundaries:

- B1 `submit_job` deduplicates by context + recipe, returns metadata including
  private recipe paths, and does not itself run a worker. `job_status` explicitly
  says `orchestration_metadata_only`. `awaiting_human_review` is not approval or
  canonical publication. Backend digests/paths must not be forwarded wholesale.
- B3 collection, enqueue, status mutation and overview publication are distinct.
  `apply_project_overview` uses conditional PUT and exact-byte GET verification;
  `already_desired/created/updated/recovered` are meaningful verified outcomes.
  `apply_project_status` similarly preserves strong-ETag CAS and verification.
  A queue file, CLI zero exit code, or watcher snapshot does not prove Vault effect.
- B4 `preview_ready` validates private nonpublishing artifacts; only `published`
  with validated publication evidence proves Daily effect. `publication_enabled`
  means a publisher is attached, not that any write succeeded. #444 internals
  remain separately owned and unchanged.
- B5 is experimental private local inventory, not an approved production
  importer. Even `inventory_ready(commit=False)` calls `_root`, `_connect`, and
  `_salt` and may initialize private state. It is **not** a side-effect-free GET
  implementation. Inventory commit queues zero LLM jobs and changes no Vault.

## 3. Versioning and transport separation

The existing `/api/v1/workflows`, `/api/v1/snapshot*`, and `/api/v1/daily*`
responses retain their current schemas and integer `version` meanings. **Do not
replace their payloads with this DTO, change their status codes, or reinterpret
`control_available` as a capability.** See section 7 for exact compatibility.

Proposed future routes, all **unimplemented** in this change:

| Method/path | Contract |
| --- | --- |
| GET `/api/v1/workflow-control` | `Catalog` containing exactly five ordered workflow observations |
| GET `/api/v1/workflow-control/{id}` | One `Workflow` observation; fixed allowlisted ID only |
| GET `/api/v1/workflow-control/{id}/requests/{requestId}` | Execution Admission/Receipt lookup, including after an ambiguous POST; not staging lookup |
| POST `/api/v1/workflow-control/b1/generations` | B1 `GenerateRequest`, not a shell or service operation |
| POST `/api/v1/workflow-control/b3/syncs` | B3 `ProjectSyncRequest` |
| POST `/api/v1/workflow-control/b5/imports` | B5 `InventoryImportRequest` |
| POST `/api/v1/workflow-control/{b1,b3}/schedule` | Typed schedule setting, only after scheduler decision and acceptance |

B2/B4 commands/settings keep their existing routes; their future projection
adapters can supply the new read DTO. No new generic `/execute`, arbitrary
`action` dispatcher, systemd unit selector, shell, argv, environment, UID or sudo
port exists. Upload staging is a separate bounded port (section 8); its HTTP
encoding/limits require B5/integrator review before a route is registered.

New DTOs use `schemaVersion: "webui-workflow-control/v1"` so they cannot be
mistaken for the existing integer-version catalog. An incompatible field, enum,
effect meaning, or action expansion requires v2 or an explicitly reviewed v1
revision before implementation; do not silently broaden authority. Additive
optional display fields can be ignored only if they cannot affect control.
Unknown versions/enums in actionable data fail closed. Backend DB schema
migrations stay adapter-owned, never run on GET, never erase receipt history,
and never replay indeterminate effects. Contract adoption does not authorize
any DB or scheduler migration.

## 4. Normative DTO schema

JSON object fields in these tables are required unless marked nullable. Exact
types matter: JSON booleans are not integers, timestamps must be finite, and
duplicate JSON keys must be rejected on new command inputs. All timestamps are
Unix seconds UTC (number >= 0, fractional seconds allowed), never local strings.
Adapters convert B1/B3 ISO UTC timestamps explicitly; UI localization is display
only. Absent/unknown timestamps use `null`, never zero as an invented success.

### Catalog and Workflow

`Catalog = {schemaVersion, authority, observedAt, workflows}`; authority is
`observation_only`, observedAt is a timestamp, workflows is exactly one entry per
ID in B1..B5 order. A global fetch failure is not an empty successful catalog.

| Workflow field | Type / values / invariant |
| --- | --- |
| `schemaVersion` | Literal `webui-workflow-control/v1` |
| `id` | `b1/b2/b3/b4/b5`; permanent product identities, not legacy service IDs |
| `label` | Server-owned display string, 1..160 characters; render as text |
| `adapterMode` | `connected/read_only/mock`; mock cannot touch runtime ports |
| `revision` | Opaque 32-lowercase-hex token for this workflow's admission-relevant state |
| `observedAt` | Timestamp for a coherent observation, not a permission expiry |
| `integration` | `connected/pending/not_attached/unavailable` |
| `configuration` | `{state, reason}`; state `configured/missing/invalid/not_observed`; reason fixed code or null |
| `schedule` | `{state, intervalSeconds, nextDueAt, reason}`; see below |
| `execution` | `{state, requestId, startedAt}`; state `idle/queued/running/unknown`; requestId/startedAt nullable |
| `lastAttempt` | Null or `{requestId, state, outcome, completedAt, effect}`; state `queued/running/completed`, outcome null or `success/failure/unknown` |
| `lastVerifiedOutcome` | Null or `{status, completedAt, effect}`; status `success/failure`; no request IDs or private evidence in catalog history |
| `lastAttemptAt` | Timestamp/null; newest admitted attempt creation time |
| `availableActions` | Array of distinct eligible action names from the product allowlist |
| `actionReasons` | Object mapping **every other action** in that product's allowlist to a fixed absence reason; disjoint from availableActions |

Integration is independent of configuration and execution. `connected` means a
validated adapter attachment and successful observation, not configured,
authorized, running, or successful. `pending` means no accepted common adapter;
`not_attached` means a known adapter is absent in this server instance;
`unavailable` means an attempted observation failed validation or could not be
obtained. Never turn a timeout into idle/off/failed or an old service into B1.

Revision is an equality-only concurrency token, not a timestamp or a capability.
Keep it stable across unchanged polling; change it atomically when configuration
generation, accepted input bindings, admission eligibility or schedule changes.
Observation and admission must compare against the same owner snapshot. A mock
revision cannot be submitted to a connected adapter. Discard stale fetches using
the client request-generation guard even if their revision looks well formed.

Schedule states: `enabled/disabled/not_applicable/unknown`.
`enabled` needs a configured accepted schedule, bounded integer interval and an
observed `nextDueAt` (nullable when no reliable due time is available, with
`due_unavailable`). `disabled` has null nextDueAt, and may retain an interval.
`unknown` has null nextDueAt and a reason; it is not disabled. For B1/B3 whose
schedule engine is undecided, use `unknown`, null interval/due and
`scheduler_decision_required`; do not invent a timer. B5 **always** uses
`not_applicable`, null interval/due, `manual_only`, even when unavailable.
`nextDueAt` is an eligibility estimate, not a promised start or successful effect.

Execution `idle` is only valid when no active attempt is positively known.
`queued` needs requestId and null startedAt; `running` needs requestId and may
have null startedAt if the backend never persisted it. `unknown` does not invent
a start or request ID. Existing B2/B4 records have no start-time column; their
creation time must not be relabelled as start time. Scheduling Off neither
cancels nor removes admitted work. Manual execution can be allowed while Off.

### Effect and verified-outcome semantics

`Effect = {kind, state, changed}`:

- kind: `none/review_artifact/snapshot_ref/project_vault/daily_preview/daily_vault/private_inventory`.
- state: `none/verified/unknown`; `changed` is boolean or null for verified
  effects, otherwise null. It describes change to that effect's destination,
  not necessarily the canonical Vault. Null means evidence proves readiness but
  does not establish whether destination content changed. Verified no-op uses
  `changed:false`; it is not a fresh write.
- `none/none/null` means no product effect occurred, positively established.
  An active or interrupted attempt uses its intended effect kind and
  `unknown/null`, never a premature no-effect claim.
- `verified` requires adapter-validated evidence bound to this attempt's input,
  configured destination/generation, expected effect, and durable result. It is
  not the browser's assertion. Private evidence remains server-side.

`lastAttempt` answers what happened to the newest admission. `lastVerifiedOutcome`
answers the newest completed attempt with a verified result **and** known effect
(`verified` or positively established `none`). It may be older than lastAttempt;
UI must show a newer failure/unknown prominently alongside it. If historical
verification is unavailable, return null, not an inferred older success. It
does not mean the source or current connection is still healthy. Never combine
evidence across connection generations as current verification.

| Existing result | New attempt/effect projection, only after validating its binding |
| --- | --- |
| B2 `saved` / `no_changes` | completed success, verified snapshot_ref, changed true / false; fixed SnapshotAdapter verifies remote ref at `verify` stage |
| B2 `failed` | completed failure; known none only if stage/evidence proves no destination effect; otherwise effect unknown |
| B2 `unconfirmed` | completed unknown, snapshot_ref unknown; no automatic replay |
| B4 `preview_ready` | completed success of **daily_preview**, changed null unless artifact-change evidence exists; never daily_vault success or a claim the Vault changed |
| B4 `published` | completed success daily_vault; `applied/recovered` changed true, `already_desired` false; validated publication binding required |
| B4 `publish_failed` | completed failure, daily_vault unknown unless separate evidence resolves it; preview remains available, never infer no remote effect from catch-all status |
| B4 `failed` / `unconfirmed` | completed failure / unknown; no verified publication implied |
| B1 queued/active stage | queued/running; completion of one generation stage does not prove a Review artifact |
| B1 `awaiting_human_review` | review_artifact success only after selected artifact chain is validated; not canonical apply, approval, or automatic downstream execution |
| B3 watcher/enqueued | still active or queue-only observation, not project_vault success; aggregate success requires all required Writer subeffects verified |
| B5 `inventory_only/inventory_review_required` | private_inventory success only after committed journal verification; review-required must remain visible in detail; never generation or Vault success |

B5 publication to ready is not inventory commit. `normalization_rejected` is a
failed import, not a successful inventory. `eligible_for_generation:false`
remains false; nothing here enables generation. No-change/replayed operations
must not increment counts or pretend to have written again.

### Action allowlists and fixed reasons

| ID | Complete v1 action universe | Conditions beyond a valid observation |
| --- | --- | --- |
| b1 | `generate`, `set_schedule` | Bound context/recipe/provider; no active conflict; schedule engine accepted for settings |
| b2 | `run_once`, `set_schedule` | Existing configured SnapshotJob routes; run requires admission lock |
| b3 | `sync_project`, `set_schedule` | Bound Project scope and accepted collection/Writer ports; engine accepted for settings |
| b4 | `run_once`, `set_schedule` | Configured DailyJob; publication separately indicated by per-run effects |
| b5 | `stage_archive`, `import_inventory` | Manual-only; stage_archive names the separate streaming port, not a JSON action; staging stays pending_integration until separately accepted; import needs bound inspected selection and private journal |

Reasons: `pending_integration/not_attached/observation_unavailable/read_only/mock_only/configuration_missing/configuration_invalid/busy/scheduler_decision_required/input_required/reconciliation_required/due_unavailable/manual_only/unsupported_action/stale_revision/authority_rejected`.
Reason null is allowed only where no reason is necessary; actionReasons never
contains null. For unavailable/malformed observations all actions are absent;
retain only validated server-owned identity/label and neutral unknown state.
`availableActions` is guidance, **not** an execution token. Admission rechecks
attachment, authority, configuration, input binding, locks and current revision.

## 5. Five-workflow JSON fixture

The following complete **synthetic mock** catalog exercises pending, connected,
unavailable, running, older verified success, and B5 manual-only states. It is
not a claimed response from the current `/api/v1/workflows` route. Mock actions
are deliberately disabled. IDs and times contain no private source data.

```json
{
  "schemaVersion": "webui-workflow-control/v1",
  "authority": "observation_only",
  "observedAt": 1793059200,
  "workflows": [
    {
      "schemaVersion": "webui-workflow-control/v1",
      "id": "b1", "label": "AI-derived notes", "adapterMode": "mock",
      "revision": "11111111111111111111111111111111", "observedAt": 1793059200,
      "integration": "pending", "configuration": {"state": "not_observed", "reason": "pending_integration"},
      "schedule": {"state": "unknown", "intervalSeconds": null, "nextDueAt": null, "reason": "scheduler_decision_required"},
      "execution": {"state": "unknown", "requestId": null, "startedAt": null},
      "lastAttempt": null, "lastVerifiedOutcome": null, "lastAttemptAt": null,
      "availableActions": [], "actionReasons": {"generate": "pending_integration", "set_schedule": "scheduler_decision_required"}
    },
    {
      "schemaVersion": "webui-workflow-control/v1",
      "id": "b2", "label": "Vault Snapshot to private Gitea", "adapterMode": "mock",
      "revision": "22222222222222222222222222222222", "observedAt": 1793059200,
      "integration": "connected", "configuration": {"state": "configured", "reason": null},
      "schedule": {"state": "disabled", "intervalSeconds": 300, "nextDueAt": null, "reason": null},
      "execution": {"state": "idle", "requestId": null, "startedAt": null},
      "lastAttempt": {"requestId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "state": "completed", "outcome": "success", "completedAt": 1793059100, "effect": {"kind": "snapshot_ref", "state": "verified", "changed": false}},
      "lastVerifiedOutcome": {"status": "success", "completedAt": 1793059100, "effect": {"kind": "snapshot_ref", "state": "verified", "changed": false}},
      "lastAttemptAt": 1793059000,
      "availableActions": [], "actionReasons": {"run_once": "mock_only", "set_schedule": "mock_only"}
    },
    {
      "schemaVersion": "webui-workflow-control/v1",
      "id": "b3", "label": "Project data into Vault", "adapterMode": "mock",
      "revision": "33333333333333333333333333333333", "observedAt": 1793059200,
      "integration": "unavailable", "configuration": {"state": "not_observed", "reason": "observation_unavailable"},
      "schedule": {"state": "unknown", "intervalSeconds": null, "nextDueAt": null, "reason": "observation_unavailable"},
      "execution": {"state": "unknown", "requestId": null, "startedAt": null},
      "lastAttempt": null, "lastVerifiedOutcome": null, "lastAttemptAt": null,
      "availableActions": [], "actionReasons": {"sync_project": "observation_unavailable", "set_schedule": "observation_unavailable"}
    },
    {
      "schemaVersion": "webui-workflow-control/v1",
      "id": "b4", "label": "GitHub progress into Daily Note", "adapterMode": "mock",
      "revision": "44444444444444444444444444444444", "observedAt": 1793059200,
      "integration": "connected", "configuration": {"state": "configured", "reason": null},
      "schedule": {"state": "enabled", "intervalSeconds": 86400, "nextDueAt": 1793145600, "reason": null},
      "execution": {"state": "running", "requestId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "startedAt": null},
      "lastAttempt": {"requestId": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "state": "running", "outcome": null, "completedAt": null, "effect": {"kind": "daily_vault", "state": "unknown", "changed": null}},
      "lastVerifiedOutcome": {"status": "success", "completedAt": 1792972800, "effect": {"kind": "daily_preview", "state": "verified", "changed": null}},
      "lastAttemptAt": 1793059190,
      "availableActions": [], "actionReasons": {"run_once": "mock_only", "set_schedule": "mock_only"}
    },
    {
      "schemaVersion": "webui-workflow-control/v1",
      "id": "b5", "label": "ChatGPT Export intake", "adapterMode": "mock",
      "revision": "55555555555555555555555555555555", "observedAt": 1793059200,
      "integration": "pending", "configuration": {"state": "not_observed", "reason": "pending_integration"},
      "schedule": {"state": "not_applicable", "intervalSeconds": null, "nextDueAt": null, "reason": "manual_only"},
      "execution": {"state": "unknown", "requestId": null, "startedAt": null},
      "lastAttempt": null, "lastVerifiedOutcome": null, "lastAttemptAt": null,
      "availableActions": [], "actionReasons": {"stage_archive": "pending_integration", "import_inventory": "pending_integration"}
    }
  ]
}
```

Additional valid connected scenarios (not current attachments): B1 can be
configured/idle with a verified review_artifact and generate eligible while
set_schedule remains scheduler_decision_required; B3 can be configured/idle
with verified project_vault and sync_project eligible under the same schedule
restriction; B5 can be configured/idle with verified private_inventory and,
only after separate staging acceptance, manual stage/import actions, but still
no timer. Read-only versions of these
scenarios have no availableActions and `read_only` reasons. A connected missing
configuration has no execution actions and `configuration_missing` reasons.

Example of **invalid untrusted input**, never a valid observation or command:

```json
{"schemaVersion":"webui-workflow-control/v9","id":"b5","schedule":{"state":"enabled","nextDueAt":1793145600},"availableActions":["run_once"],"argv":["arbitrary-program"]}
```

Reject it; do not auto-correct it into connected B5 or relay argv. Unknown source
status text, including strings that look like tokens, is never echoed. Normalize
an unrecognized terminal backend status to unknown outcome/effect, disable new
effectful actions pending reconciliation, and keep raw text out of browser JSON.

## 6. Typed commands, admission, retries, and receipts

### New command inputs (not legacy B2/B4 payloads)

All new **JSON command** requests in the table below have **exact** keys
`{schemaVersion,requestId,expectedRevision,action,input}`. B5 `StageArchiveRequest`
is an explicit typed **streaming-port exception**, not a JSON command: it has
only `{schemaVersion,requestId,expectedRevision}` and a separate bounded binary
stream argument (section 8). `stage_archive` is a catalog action name, never an
`action` discriminator in this common JSON shape.
requestId and expectedRevision are 32 lowercase hex. requestId is generated once
per operator intent, retained across transport retries, and scoped to the fixed
workflow. It is correlation, not authentication. `input` is a discriminated,
exact-key object; every `*Ref` is an opaque server-issued 32-hex handle resolved
to an immutable, current, allowlisted binding. It is not a path, URL or digest
provided as execution authority.

| Type / route | action | Exact input keys and authority limit |
| --- | --- | --- |
| GenerateRequest / b1 generations | `generate` | `contextRef,recipeRef`; private prepared context/recipe, no arbitrary model endpoint or credentials |
| ProjectSyncRequest / b3 syncs | `sync_project` | `bindingRef`; configured Project scope, never arbitrary repository or Vault path |
| InventoryImportRequest / b5 imports | `import_inventory` | `archiveRef,selectionRef`; inspected immutable staged upload + bound server-side scope/parser/member policy |
| ScheduleRequest / b1,b3 schedule | `set_schedule` | `enabled,intervalSeconds`; bool and bounded integer, policy range must be advertised/accepted by owner before admission |

Schedule policy bounds for B1/B3 are intentionally **not chosen** here. Until
the owner settles engine/cadence semantics, their schedule setter returns
`scheduler_decision_required` and never starts or enables a timer. `generate`
and `sync_project` can be independently tested with injected manual execution
ports; no scheduler selection is needed for read/mock/manual contracts.

```json
{"schemaVersion":"webui-workflow-control/v1","requestId":"cccccccccccccccccccccccccccccccc","expectedRevision":"55555555555555555555555555555555","action":"import_inventory","input":{"archiveRef":"dddddddddddddddddddddddddddddddd","selectionRef":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}}
```

### Admission state and errors

For execution commands only,
`Admission = {schemaVersion,workflowId,requestId,decision,receipt,error}`.
Staging MUST NOT return this generic Admission or an execution Receipt; it uses
`StagingAdmission` (section 8). Schedule settings use `ScheduleResult` below.
decision is `accepted/replayed/rejected/indeterminate`.
Accepted/replayed needs a persisted receipt and null error; rejected has null
receipt and a typed error; indeterminate has nullable receipt and error
`admission_unknown`. A rejection establishes **this request was not admitted**;
it is not a claim about all prior operations. A server must not say rejected if
it cannot prove whether admission committed.

`Error = {code,retry}`; fixed codes and retry modes:

| Code | Proposed HTTP | Retry mode |
| --- | --- | --- |
| `invalid_request`, `unsupported_version`, `unsupported_action` | 400 | `never` |
| `origin_rejected`, `csrf_rejected`, `authority_rejected` | 403 | `after_refresh` (fresh CSRF/authority check, not blind retry) |
| `request_not_found` | 404 | `lookup` after an ambiguous submission; absence alone is not permission to invent a new request |
| `request_conflict`, `stale_revision`, `busy`, `reconciliation_required` | 409 | `after_refresh`; conflicts require a corrected/new operator intent, never silent mutation |
| `configuration_missing`, `configuration_invalid`, `input_binding_invalid` | 422 | `after_refresh` |
| `pending_integration`, `not_attached`, `observation_unavailable`, `read_only`, `mock_only`, `scheduler_decision_required` | 503 | `after_refresh` |
| `admission_unknown` | 503 | `lookup` with same requestId; no automatic new ID or re-execution |

Authentication/CSRF middleware may return a minimal fixed error before parsing
requestId. Do not echo a malformed request for the sake of making an envelope.
Typed admission HTTP 202 for accepted/replayed does **not** prove queued state:
an asynchronously completed replay can already carry a completed receipt.

Deduplication MUST be durable and atomic with admission. Bind the key
`(workflowId, requestId)` to the canonical action + typed input + configuration
generation, storing the accepted revision. Same ID, same binding returns the
same run/receipt, never invokes twice; differing input/action is request_conflict.
After authority checks, lookup an already-bound ID before rejecting an otherwise
identical retry solely for an obsolete expectedRevision. For new IDs, stale
revision rejects before effects. Unknown/corrupt/lost ledger fails closed;
neither restart nor a TTL eviction can make an accepted ID executable again.
After configuration rotation, an identical old request lookup returns its old
receipt and old binding, never rebinds or executes under the new configuration.
These guarantees are at-most-one admission, **not** exactly-once remote effects.
Adapter content deduplication (B1 context/recipe, B5 archive/selector) remains
necessary but is not a replacement for command request-ID binding. Staging's
separate durable content/request binding is specified in section 8.

### Receipt

`Receipt = {schemaVersion,workflowId,requestId,runId,action,state,outcome,createdAt,startedAt,completedAt,effect,error}`.
runId is a new opaque 32-hex WebUI handle, not necessarily a backend job digest;
backend references stay private. state is `queued/running/completed`.
queued/running have null outcome/completedAt/error and unknown effect; queued
also has null startedAt. Completed has outcome `success/failure/unknown` and
completedAt. Success requires verified effect; failure needs a fixed execution
error (`execution_failed/input_rejected/verification_failed`); unknown needs
`effect_unconfirmed` and unknown effect. No free-form stderr/message field.

```json
{
  "schemaVersion":"webui-workflow-control/v1", "workflowId":"b5",
  "requestId":"cccccccccccccccccccccccccccccccc", "decision":"accepted", "error":null,
  "receipt":{
    "schemaVersion":"webui-workflow-control/v1", "workflowId":"b5",
    "requestId":"cccccccccccccccccccccccccccccccc", "runId":"ffffffffffffffffffffffffffffffff",
    "action":"import_inventory", "state":"queued", "outcome":null,
    "createdAt":1793059200, "startedAt":null, "completedAt":null,
    "effect":{"kind":"private_inventory","state":"unknown","changed":null}, "error":null
  }
}
```

```json
{
  "schemaVersion":"webui-workflow-control/v1", "workflowId":"b5",
  "requestId":"cccccccccccccccccccccccccccccccc", "runId":"ffffffffffffffffffffffffffffffff",
  "action":"import_inventory", "state":"completed", "outcome":"success",
  "createdAt":1793059200, "startedAt":1793059201, "completedAt":1793059203,
  "effect":{"kind":"private_inventory","state":"verified","changed":true}, "error":null
}
```

```json
{"schemaVersion":"webui-workflow-control/v1","workflowId":"b3","requestId":"cccccccccccccccccccccccccccccccc","decision":"rejected","receipt":null,"error":{"code":"stale_revision","retry":"after_refresh"}}
```

Execution transitions are queued -> running -> completed; interruption can
transition queued/running directly to completed unknown. GET never triggers
recovery execution. A timeout, worker loss, malformed result, or missing effect
verification cannot become success. Preserve the uncertain receipt; reconcile
using bound evidence, not a fresh request or automatic replay. B2/B4 currently
recover persisted queued/running records as `unconfirmed` without replay.
The new B1/B3/B5 boundary MUST prevent conflicting unresolved effects from being
automatically retried; do not falsely claim legacy B2/B4 globally prohibit new
manual intents after an unconfirmed attempt (they do not).

Settings admission is not execution admission. A successful schedule change
returns the observed schedule and a new revision, never a run/effect receipt.
Off does not cancel a queued/running attempt; On must not mean Run Once.
Duplicate desired settings must not reset next due gratuitously. The exact
new ScheduleResult is `{schemaVersion,workflowId,requestId,decision,schedule,revision,error}`;
accepted/replayed contains the persisted schedule/revision, rejected contains
null schedule/revision and typed error. Same-ID settings replay/conflict rules
are the same; never fabricate an Effect for setting a timer.

## 7. Existing B2/B4 HTTP compatibility (normative preservation)

These are **existing** routes, inspected in `admin_dashboard.py:Handler` and the
job tests. Query-string variants are not automatically equivalent route names.

| Route | Existing request/response |
| --- | --- |
| GET `/api/v1/snapshot` | 200 `{version:1,job:"vault_snapshot",settings,last_run}`; absent job 503 `snapshot_disabled` |
| GET `/api/v1/daily` | 200 `{version:1,job:"github_daily_preview",publication_enabled,settings,last_run}`; absent job 503 `daily_disabled` |
| GET `/api/v1/snapshot/runs/{run_id}` | Raw persisted run object; malformed/missing ID 404 **text** `Not found`; absent job 503 |
| GET `/api/v1/daily/runs/{run_id}` | Raw persisted run object; malformed/missing ID 404 JSON `{"error":"not_found"}`; absent job 503 |
| POST `/api/v1/snapshot/settings` | Exact `{enabled,interval_seconds,source_name:"nextcloud",destination_name:"gitea"}`; 200 settings |
| POST `/api/v1/daily/settings` | Exact `{enabled,interval_seconds}`; 200 settings |
| POST `/api/v1/{snapshot,daily}/runs` | Exact `{"request_id":"<32 lowercase hex>"}`; 202 `{version:1,outcome:"accepted" or "replayed",run_id}`; 409 `{version:1,outcome:"busy" or "unconfigured",run_id:null}` |
| GET/POST `/api/v1/snapshot/connections` | Redacted SetupStore status / bounded validated connection save; absent store 503 `setup_disabled` |
| GET/POST `/api/v1/daily/connections` | Redacted DailySetup status / exact validated GitHub connection save; absent setup 503 `daily_setup_disabled` |
| GET/POST `/api/v1/nextcloud/connections` | Redacted shared Nextcloud-only status / validated save; absent setup 503 `nextcloud_setup_disabled` |
| GET `/api/v1/workflows` | Existing integer-version observation-only catalog described in section 2; POST 405 |
| GET `/api/v1/operation-session` | Existing one-use CSRF session for typed POSTs; HEAD 405; issue failure 503 `preview_session_unavailable` |

There is **no GET settings endpoint** for these jobs; settings come from GET
status. Both jobs' run IDs/request IDs are `[a-f0-9]{32}`. Duplicate submission
returns the existing run even after completion; different internal trigger for
the same ID raises Invalid. HTTP clients cannot supply trigger (extra keys
reject), so scheduled/manual trigger conflicts cannot be injected through POST.

B2 settings include `version,configured,enabled,interval_seconds,next_due,source_name,destination_name,gitea_write`.
Interval is integer 60..86400, default 300, default Off; bool is not an integer.
B2 `update` itself does not check configured before recording enabled; admission
and tick still reject/skip unconfigured execution, and catalog displays off.
Preserve this distinction rather than claiming B2 rejects every unconfigured
settings save. B2 connection changes reset Off, next_due and write verification.

B4 settings include `enabled,interval_seconds,next_due,configured` (no nested
version). Interval is integer 3600..86400, default 86400, default Off. Enabling
while unconfigured rejects. Configured is dynamically checked; missing
connections can coexist with a persisted enabled flag. A new projection must
not present that as an effective enabled runnable schedule. Both jobs set next
due to clock + interval when newly enabled/interval changed; unchanged enabled
settings preserve due; Off sets due null. Busy ticks skip execution. B4 atomically
persists scheduled run + new due before worker launch; neither constructor runs
providers. Manual Run Once does not require schedule On.

B2 run keys: `run_id,request_id,trigger,status,created,completed,stage,remote_ref`.
B4 run keys: `run_id,request_id,trigger,status,created,completed,date_jst,capture_sha256,digest_sha256,original_event_count,rendered_entity_count,publication_outcome`.
These private-network typed detail APIs already expose their validated evidence
fields; do not silently remove them for compatibility. The **new catalog** must
not copy them. B4 job name remains `github_daily_preview` even when a publisher
is attached; renaming it breaks validated readers.

After the shared origin/CSRF guards, malformed payloads/settings are 400
`invalid_snapshot_request` / `invalid_daily_request`; unexpected errors are 503
`snapshot_unavailable` / `daily_unavailable`. Do not replace these with the new
typed Admission schema. Unknown POST paths return 405. GET Host/Origin failure
is 403 text Forbidden. Exact settings and run examples, for existing routes:

```json
{"enabled":false,"interval_seconds":300,"source_name":"nextcloud","destination_name":"gitea"}
```

```json
{"enabled":false,"interval_seconds":86400}
```

```json
{"request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

```json
{"version":1,"outcome":"replayed","run_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
```

Connection protections remain: probe before accepting changed credentials;
reject changes while relevant jobs are queued/running; never equate a read
probe with verified write success. Daily credential save commits Off before
replacement; shared Nextcloud changes coordinate B2/B4 and invalidate relevant
schedules. Failed probes must not launch work or expose credential values. No
B1/B3/B5 adapter may circumvent the existing shared connection owner.

## 8. Engine-agnostic adapter ports and independent ownership

The following are **design signatures**, not imports of a newly implemented
module. DTO names refer to sections 4/6 and the explicit staging exception below;
immutable typed inputs use their respective exact fields. Ports are injected by
the future composition root, never selected by browser-supplied Python/module/
service names.

```python
class WorkflowReadPort(Protocol):
    def observe(self) -> Workflow: ...
    # Execution admissions only; staging lookup is a separate design port.
    def lookup(self, request_id: RequestId) -> Admission: ...

class GenerationPort(Protocol):
    def generate(self, request: GenerateRequest) -> Admission: ...

class ProjectSyncPort(Protocol):
    def sync_project(self, request: ProjectSyncRequest) -> Admission: ...

class InventoryImportPort(Protocol):
    def import_inventory(self, request: InventoryImportRequest) -> Admission: ...

class SchedulePort(Protocol):
    def set_schedule(self, request: ScheduleRequest) -> ScheduleResult: ...

class PrivateArchiveStagingPort(Protocol):
    def stage_archive(self, request: StageArchiveRequest,
                      stream: BoundedBinaryStream) -> StagingAdmission: ...
    def lookup_staging(self, request_id: RequestId) -> StagingAdmission: ...
```

### B5 private staging: streaming-port exception (normative)

`StageArchiveRequest = {schemaVersion,requestId,expectedRevision}` has exactly
these fields, with the v1 literal and 32-lowercase-hex IDs/revision from section 6.
The port takes a separate `BoundedBinaryStream`; request metadata contains no
`action`, `input`, bytes, local path or remote URL. The following is request
metadata only, not an upload encoding or an executable HTTP request:

```json
{"schemaVersion":"webui-workflow-control/v1","requestId":"99999999999999999999999999999999","expectedRevision":"55555555555555555555555555555555"}
```

`StagingAdmission = {schemaVersion,requestId,decision,stagedArchive,error}` has
exactly these fields. schemaVersion is the v1 literal; requestId echoes the valid
staging ID; decision is `accepted/replayed/rejected/indeterminate`. `error` is
null or the exact `Error = {code,retry}` from section 6 (its HTTP column does not
register a staging route). There is no workflowId, runId, action, execution state
or Effect in this envelope.

| Decision | stagedArchive | error / evidence required |
| --- | --- | --- |
| `accepted` | Original durable StagedArchive | null; verified completed private publication **and** inspection, durably bound to the staging request |
| `replayed` | Exact original StagedArchive, unchanged | null; verified identical content/binding; no second publication or inspection effect |
| `rejected` | null | Fixed Error; positively established that this submission was not admitted; a conflict does not erase a prior accepted binding |
| `indeterminate` | null | Exactly `{code:"admission_unknown",retry:"lookup"}`; publication/inspection/binding cannot be established safely |

`StagedArchive = {schemaVersion,requestId,archiveRef,selectionRef,inspection}` is
the **staging receipt**, not an execution Receipt. It has exactly those fields;
schemaVersion/requestId match the envelope, references are opaque 32-hex handles,
and inspection is `inspected/schema_review_required`. The selectionRef MUST bind
the inspected immutable archive and server-owned scope/parser/selector. An
ambiguous selection rejects with `input_binding_invalid`, never successful
handles. Schema review required is private inspection status, not permission to
import unaccepted content. An HTTP 2xx, receipt of bytes, temporary file, or
unverified journal entry MUST NOT bind a successful staging receipt or expose
handles. Accepted/replayed requires verified, durable completed private archive
publication and inspection and its original receipt in the private journal.

The following synthetic results show a completed staging receipt, its exact
replay, changed-content conflict, and uncertainty for the same staging request:

```json
{"schemaVersion":"webui-workflow-control/v1","requestId":"99999999999999999999999999999999","decision":"accepted","stagedArchive":{"schemaVersion":"webui-workflow-control/v1","requestId":"99999999999999999999999999999999","archiveRef":"dddddddddddddddddddddddddddddddd","selectionRef":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","inspection":"inspected"},"error":null}
```

```json
{"schemaVersion":"webui-workflow-control/v1","requestId":"99999999999999999999999999999999","decision":"replayed","stagedArchive":{"schemaVersion":"webui-workflow-control/v1","requestId":"99999999999999999999999999999999","archiveRef":"dddddddddddddddddddddddddddddddd","selectionRef":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","inspection":"inspected"},"error":null}
```

```json
{"schemaVersion":"webui-workflow-control/v1","requestId":"99999999999999999999999999999999","decision":"rejected","stagedArchive":null,"error":{"code":"request_conflict","retry":"after_refresh"}}
```

```json
{"schemaVersion":"webui-workflow-control/v1","requestId":"99999999999999999999999999999999","decision":"indeterminate","stagedArchive":null,"error":{"code":"admission_unknown","retry":"lookup"}}
```

The private journal MUST atomically bind `(b5 staging, requestId)` to verified
upload content identity, immutable inspection/selection policy and configuration
generation, accepted revision and original staging receipt. Same ID with changed
content/binding rejects `request_conflict`, not replay; exact replay returns only
the original receipt, including after configuration rotation. After authority
checks, resolve an existing binding before rejecting an otherwise identical retry
solely for its old revision; new IDs require current revision before publication.
Concurrent duplicates must not publish twice. Malformed/extra/duplicate metadata
keys, invalid versions/IDs or unsafe streams fail closed before publication;
fixed errors apply without echoing malformed input. An unavailable/corrupt private
journal forbids new publication, receipt fabrication, reset or auto-bootstrap.
Use `observation_unavailable` only when nonadmission is established; if prior
publication/admission cannot be determined, return indeterminate admission_unknown.

On indeterminate state or a lost response, recovery is **lookup-only**, keyed to
the same requestId: no automatic new upload, new ID, publication or import.
`lookup_staging(request_id)` is a noninitializing **design port**, not an
already-mounted or newly specified HTTP route. It returns the same typed
StagingAdmission: verified completed readback is replayed with the original
receipt; unresolved state remains indeterminate. `request_not_found` with retry
`lookup` is permitted only for positively established absence and does not permit
automatic re-upload/new intent. Lookup/GET never repairs, publishes, initializes
a journal, or mutates state. Readback uses only attached bounded private evidence.

Staging handles are inputs to a **subsequent, distinct manual**
`import_inventory` JSON command with its own requestId and execution
Admission/Receipt (section 6). Staging is not queued inventory work, committed
private_inventory, generation or a Vault/LLM effect. Even an accepted inventory
import never authorizes generation or Vault writes.

Actual upload encoding/transport, resource sizes, timeouts, ZIP parser policy,
private deployment/permission acceptance and HTTP route registration remain
**open acceptance work**, not selected or accepted by this correction. A later
accepted transport must enforce bounded bytes, stream timeouts, archive entry,
uncompressed/ratio/JSON limits and private permissions before exposing handles;
this is not authorization to feed ZIP bytes to the existing bounded JSON reader.
Until separately accepted, `stage_archive` remains nonactionable: absent from
availableActions with `pending_integration` in actionReasons. No live upload port
or permission is granted here; B5 remains manual-only and read/mock fail closed.

Port implementation rules:

1. Constructors, observe and lookup must not schedule, enqueue, initialize DBs,
   rewrite files, invoke production CLI, poll live legacy services, or acquire
   credentials from user input. Inspect only attached bounded state snapshots.
   Absent stores yield not_attached/pending, not auto-bootstrap. Read-only B5
   uses a noninitializing journal reader, not inventory_ready.
2. Read/mock ports have no effectful dependency and return no executable
   availableActions. Mock fixtures may simulate states/receipts in memory with
   a visible mock label, never claim connected production health or authority.
   A connected adapter needs explicit construction and acceptance; source-file
   detection or old Broker/systemd status is insufficient.
3. The mutation adapter owns strict validation, request binding/durable ledger,
   worker admission, evidence validation and sanitization. A scheduler supplies
   only a due intent to that **same** admission owner, not a second bypass.
   No executable engine API leaks into browser DTOs. Timer invocations must
   coordinate manual admission/locks and preserve request identity.
4. B1 wraps prepared context/recipe submission and separately validated Review
   output. Do not expose arbitrary recipe creation/provider URLs through these
   commands. Runtime/provider mismatches, retry exhaustion and stopped-state
   requirements are preserved. Review approval/apply/regenerate/retry are **not**
   v1 action names: expose honest read state and request a separately reviewed
   typed extension before adding those authority-bearing operations.
5. B3 binds collection and required Writer receipts to one request and scope;
   enqueue is not apply. Preserve both Project status and overview CAS rules,
   server-owned paths, managed regions, binding validation and canonical I/O
   coordination. Partial or stale subreceipts cannot yield aggregate success.
6. B5 publish/inspection and inventory commit are separate manual stages. The
   existing inventory_ready scans ready archives in publication order; it does
   **not** accept a selected archive argument. The adapter must prove the
   requested bound archive is the eligible next item under the locked journal,
   or reject; it must not silently import an older/different queued archive.
   Existing tuple returns lack a WebUI request/run receipt and timestamps; the
   adapter needs its own durable correlation and readback. Preserve HMAC source
   identities, selector/parser scope binding, no deletion for absent entries,
   quarantine and unsupported-part counts. Never call generation or transport.

### File ownership and integration sequence

| Owner | Allowed responsibility in later scoped work | Must not edit/adopt |
| --- | --- | --- |
| API #480 | This document, optional isolated `tests/test_webui_workflow_contract.py` | Runtime/shared source |
| UI #481 | Presentation/assets and UI-owned tests, mock DTO consumption | Backend registration, credentials, admission, scheduler |
| B1 #482 | Independent B1 adapter/mock/test modules; proposed `webui_b1_*` prefix | Shared router/catalog, Review/apply authority extension without review |
| B3 #483 | Independent B3 adapter/mock/test modules; proposed `webui_b3_*` prefix | #444 Daily internals or shared registration |
| B5 #484 | Independent B5 adapter/mock/test modules; proposed `webui_b5_*` prefix | #411 External Source, automatic generation, scheduler |
| Single later integrator | Sole editor of `admin_dashboard.py`, `unified_workflows.py`, shared DTO validation/router/composition registration | Live rollout without separate approval |

Proposed prefixes are ownership guidance, not claims files already exist. Freeze
the DTO/ports after coordinator review; owners then implement bounded isolated
adapters against fixtures. Integrator validates exact adapter schemas/redaction,
registers read projections first, and only then enables accepted typed actions
under the existing guards. Retest existing B2/B4 routes and UI stale-response
behavior before shared E2E. Scheduler and upload acceptance decisions must be
recorded before enabling those affected actions; unrelated manual/read/mock
work can proceed. No leaf edits shared files in parallel. No live LXC effects.

## 9. Security, stale data, and negative acceptance matrix

Existing trust model is a private-network loginless WebUI with one nonroot
service user/Nextcloud account, not per-user Web authentication. **No auth** must
not be falsely translated into bearer-token success: approved ingress plus
Host/Origin/CSRF checks are still mandatory for mutations. CSRF is not identity.
If ingress or deployment authority cannot be established, effectful actions stay
unavailable. Do not add a new login/account/security apparatus in this task.

Existing Handler checks one exact bound Host, at most one matching Origin for
reads; writes require exactly one same-Host `http://` Origin and permit only
absent or same-origin/none Sec-Fetch-Site. One `X-Obsidian-CSRF` token is consumed
once, before body processing; acquire a fresh token for any authorized retry.
Do not accept wildcards, client-provided ingress headers as authority, or create
CORS bypasses. Existing `_operator_network_allowed` is specific to legacy
operator routes; do not claim it currently guards all B2/B4 routes. New workflow
admission must be integrated into the accepted private deployment boundary,
not use that legacy Broker to gain arbitrary service privileges.

Preserve `Cache-Control:no-store`, CSP, nosniff, frame denial and referrer policy.
Existing JSON body parsing enforces exact application/json, one bounded decimal
Content-Length, no Transfer-Encoding/Content-Encoding/Expect, and a total read
deadline. New commands additionally reject duplicate keys and nonfinite JSON;
do not claim the old json.loads already rejects duplicate keys. All browser
labels/reasons use textContent/DOM text, not innerHTML, raw JSON HTML or model
suggestions as action definitions. Sanitize by constructing allowlisted DTOs,
not by blacklisting substrings in raw backend dictionaries.

No credentials, passwords, auth headers, raw exception/stderr, filesystem
paths, URLs with userinfo, Git refs, model/source text, ZIP member names,
conversation IDs/titles, raw source digests or backend recipe output cross the
new catalog/receipt boundary. Opaque correlation handles and fixed product
effect kinds are allowed; input bindings resolve privately. Credential setup
is separately typed/write-only; never accept secrets in workflow commands.
Read probes are not write verification. Models, issue/JSON content and ZIP text
are untrusted data, never permissions, code, shell fragments or filesystem roots.

| Negative/check | Required behavior / evidence |
| --- | --- |
| Slow old success after newer fetch failed; old failure after newer success | Client request-generation guard: only newest outstanding fetch may change cards, counters, actions or error state; no stale re-enable |
| Newest invalid version, missing/duplicate workflow ID, malformed booleans, enum such as `__proto__` | Clear actionable state; show unavailable; use own-property enum validation, never inherited JS properties |
| Backend unknown status or exception containing secret sentinel | Unknown/unavailable DTO with fixed reason; sentinel absent; no executable guidance |
| Same request ID, concurrent duplicate or retry after timeout/restart | One durable binding/run; replay/lookup only, no second worker; altered input conflicts |
| New request against stale revision/changed credential generation | Reject stale_revision/input_binding_invalid before effects; refresh does not auto-submit |
| Missing/duplicate/mismatched Host or Origin, cross-site request, missing/expired/reused CSRF | 403 with fixed error; no admission/effects; no per-user login invented |
| Extra `argv`, service, path, URL, env, trigger, secret; bool interval; oversized/malformed/duplicate-key JSON | Reject before any runner call; old typed payload key sets unchanged |
| Busy workflow, missing config, unavailable adapter or unknown ledger | No new execution; settings cannot imply execution; read/mock never calls a runner |
| POST response lost after durable admission | Show indeterminate, lookup same ID/fresh CSRF if retrying; never manufacture new intent |
| Runner complete/HTTP 2xx but no bound verified effect | Not product success; unknown or failure, older verified outcome labelled historical |
| B4 preview or publish_failed | Preview explicitly nonpublishing; no invented canonical success or no-effect proof |
| B5 enable schedule, Run Once, automatic LLM/canonical write | Unsupported; manual-only invariant always holds |
| B5 corrupt journal/salt, unsafe permissions, out-of-order/changed parser/selector | Fail closed; no auto-reset, history erasure or processing different archive |
| Settings Off during run / initial GET or constructor | Run remains observable; no cancellation implied; GET/constructor starts zero work |

The existing browser stale-response tests in `test_unified_workflows.py` cover
both late success and failure. The new revision token is additional server-side
concurrency protection, not a replacement for that client generation guard.

## 10. Human-owned scheduler decision record (OPEN)

Observed baseline: SnapshotJob owns one scheduler thread calling its tick plus
hooks; DailyJob owns durable state/one worker but **no separate scheduler**.
This is existing B2/B4 behavior, not proof #298 selected the final B1..B5 engine.
No timer/service/runtime migration is selected or implemented by this document.

| Decision dimension | Embedded one-process scheduler + durable admission/worker | User-level systemd timers/services (one nonroot user) |
| --- | --- | --- |
| Ownership/startup | App owns tick loop; SQLite ledger/OS locks; WebUI and timer lifecycle coupled | User manager owns timer lifetime; fixed service entry point still needs application ledger/admission lock |
| Compatibility effort | Matches B2/B4 baseline; add bounded hooks only after acceptance | Needs explicit plan for current scheduler ownership; never leave embedded + timers active together |
| Due truth | Persisted interval/next_due in app; overdue means eligible, not executed | Timer next-elapse observation must be reconciled with persisted desired settings and admission receipt; timer active != effect success |
| Manual concurrency | Same admission owner can serialize manual and scheduled intents | Timer and HTTP process must share same durable binding/lock; systemd serialization alone is insufficient |
| Crash/unknown | Recover indeterminate receipts, no auto effect replay; app restart interrupts scheduling | Service restart/Persistent timer catch-up policy can accidentally re-admit uncertain work; explicit no-replay design required |
| Configuration | App validates bounded settings, tracks connection generations | Fixed allowlisted units and user manager scope; no arbitrary unit/command API, no root Broker fallback |
| Availability | WebUI/app loss suspends scheduler; one failure must not stop other job ticks | Timers can outlive WebUI; user-session/linger/package/permission assumptions require deployment acceptance |
| Shutdown/cancellation | Need bounded worker join and durable final state; Off is future admission only | Stop timer prevents future ticks, not proof in-flight service cancelled; reconcile durable state |
| Test burden | Injected clock/worker, crash-after-commit and hook isolation tests | Above plus user-manager availability, unit binding, activation, timer drift and dual-owner exclusion tests |

**Decision owner:** human owner of #298, with coordinator integration review.
**Status:** OPEN; no approval revision recorded. Alternatives: retain embedded
for this MVI, explicitly migrate to fixed user timers, or defer scheduling for
new workflows while shipping read/mock/manual adapters. No root timers, new
accounts, or generic service executor are implied alternatives.

**Recommendation for review, not selection:** preserve the working B2/B4 runtime
unchanged and freeze engine-neutral read/manual DTOs now. Defer B1/B3 schedule
admission until the owner records engine, per-workflow cadence/bounds, restart
and catch-up semantics, shutdown, and ownership/locking acceptance. If the owner
chooses timers, require a separate reviewed migration with one active admission
owner and preserved receipts. This bounded choice blocks schedule setters, not
the manual typed ports or honest status projection. B5 remains manual regardless.

Material contract proposals requiring coordinator review: separate new read
namespace; admission-relevant revision and durable request binding; verified
effect vs historical outcome separation; no GET initialization; B5 exact-archive
binding; explicit deferred scheduling/upload/Review authority expansion. These
are recorded design changes, not silently implemented architecture decisions.

## 11. Acceptance traceability and verification limits

| #480 criterion | Contract evidence / remaining gate |
| --- | --- |
| AC-API-01 | Sections 2/4/5: all products, states, strict types, mock JSON and invalid input; coordinator must finish parent #298 AC-03/04/05/07 text cross-check |
| AC-API-02 | Sections 6/8: execution Admission vs Receipt, separate streaming StagingAdmission/receipt, atomic duplicate binding, conflict/lookup-only uncertainty; future adapter race/crash tests required |
| AC-API-03 | Section 7 plus inspected existing tests: preserve exact routes/payloads/status/error differences and guards; integrator must run compatibility suite |
| AC-API-04 | Sections 4/5/8/9: read/mock isolation, unavailable fail-closed, observation never authority |
| AC-API-05 | Section 8: isolated owner modules then single shared integrator; no #444/#411/live work |
| AC-API-06 | Section 10: explicit alternatives, human-owned OPEN choice, no migration |

`tests/test_webui_workflow_contract.py`, if included, is an isolated stdlib-only
fixture/source-consistency check. It may parse these JSON blocks and Python AST;
it must not import runtime modules, open a server, initialize jobs/journals,
invoke providers, or claim full adapter/schema/production conformance. Actual
execution results belong in the handoff report. Existing runtime tests and
production E2E are **not** implicitly run by writing this contract.
