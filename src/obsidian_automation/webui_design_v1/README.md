# Isolated design preview — #481

Implementation run `OA298-W1-UI481-20261028-R1` was originally based on
`f691fa79400ce06e41ffa4376a7a08837620e02c` on branch
`task/oa298-ui-481-20261027`. That hash is the **historical development base**,
not the current commit. Consult [Draft PR #488](https://github.com/upiscium/ObsidianAutomation/pull/488)
for the current published source revision. Source-only; **not registered,
mounted, merged or connected**. The uploaded `Page 1.zip` was not available
here. Layout follows the textual interpretation in #481 / the execution packet.

## Preview

Open `index.html` locally in an installed browser. Classic deferred scripts need
no bundler, package install or application server. If the browser blocks local
scripts, use a separately authorized local static preview; do not weaken CSP or
mount this into the production dashboard. No preview server is started by this
component. The responsive grid is 1 column below 640px, 2 at 640px, 3 at 1024px.

Overview, service detail and grouped Connections are the normative views.
Use the top-right menu, Settings / details links and Preview state selector.
The selector exposes current-catalog-shaped mocks, a strict #480 v1 read-only DTO fixture, synthetic lifecycle examples,
loading, unavailable and stale states. Optional AI/Vault groups are presentation
placeholders, not a Setup Wizard replacement or new backend setting contract.

All interactions are inert:

- No network/storage API exists in the shipped JS. CSP also denies connections
  and form submissions. Do not enter real credentials; mock fields clear secrets
  on validation, navigation and page exit. A normal password input only masks
  newly typed text; this is not a browser-memory erasure guarantee.
- B1 Run once / Enable and B3 Sync now change only in-memory illustrations.
  They never emit commands, request IDs, success receipts or verified effects.
- The **#480 v1 read-only DTO** scenario has no workflow buttons, file picker,
  schedule setter or outgoing dashboard control links. Even connected observations
  and eligible action guidance never enable a control. `simulate` explicitly
  refuses read-only projections. No admission or receipt parser is implemented.
- B5 is manual ZIP Import, no schedule toggle or Next run. File selection reads
  the local filename **only** to check the `.zip` suffix. Neither the filename
  nor file contents are displayed, uploaded or persisted; the contents are never
  read or inspected. No claim of ZIP validation, admission or actual import is made.
- B2/B4 expose **navigation links only** to the existing root dashboard anchors
  `/#snapshot-controls` and `/#github-daily`. They leave this preview, do not
  submit an action and do not imply control availability. With `file:` preview
  these root-relative links cannot reach the dashboard; they are integration
  affordances, not mock substitute controls. Existing routes/panels are untouched.
- Connection checks are local format checks only; Save is disabled. No false
  “saved” or “connected” confirmation is produced. Blank replacement secrets are
  permitted; service dependencies are not globally required.

## Adapter boundary / integration handoff

`model.js` exports `globalThis.ObsidianDesign`. The unchanged `fromCatalog(payload)` accepts the
**existing** observation-only GET `/api/v1/workflows` version 1, not a proposed
#480 DTO. It requires exactly B1–B5, known state enumerations, boolean
`control_available`, and B5 `not_applicable`. It copies no arbitrary API label,
note, URL, secret, extra field, or control permission. Values render through DOM
`textContent`; navigation destinations come from fixed local constants.

Catalog v1 has no run timestamps, next-run timestamps, independent execution
field or verified previous outcome while running. These remain Unknown. Its
queued/running result can label current execution, but is never success. Daily
`preview_ready` explicitly does not prove Vault publication. An unavailable
adapter discards apparent success/configuration/schedule fields. No systemd
observations are used. Global health is a distinct summary, always Unknown in
this preview, not a sixth backend or an all-green service count.

`scenario("demonstration")` adds **internal, explicitly synthetic view fields**
to illustrate timestamp, Idle, Running, Enabled/Disabled and outcome layout.
These fields are not a versioned wire contract or evidence about live services.
`app.js` consumes only these local scenarios; it never fetches the GET itself.

### Strict synthetic #480 v1 projection

`fromControlCatalog(payload)` is a **separate** parsed-object boundary for the
exact `schemaVersion: "webui-workflow-control/v1"`, `authority: "observation_only"`
Catalog in `docs/webui-workflow-control-contract-v1.md` (revision 2). It is not a
transport, server-side shared validator, command parser, or proof of admission.
It validates exactly five workflows in B1–B5 order; strict own data fields,
versions, enums, fixed reasons, opaque handles, finite nonnegative Unix seconds,
schedule/execution/attempt/effect coherence, product effect kinds, and the complete
disjoint action/reason partition. Duplicate/reordered/unknown IDs or actions,
extra fields and malformed input reject the whole catalog, not a partial success.
No arbitrary display extension fields are accepted by this bounded UI candidate.
This API takes **already parsed DTO objects**, not raw JSON; duplicate JSON-key
rejection at a future transport/parser remains an integrator obligation.

The returned view is constructed from local product identities and allowlisted
fields. Server labels are type/length checked but discarded; arbitrary secret-like
label text, revisions, request handles, action reasons and action guidance are not
projected. Unknown status/reason text rejects with a fixed error instead of being
echoed. All projected actions are empty, regardless of incoming connected eligibility.
Unavailable observations clear stale configuration, schedule, execution and history
to neutral unknown (while B5 retains manual-only mode). Reads never infer authority.

`mockControlCatalog()` is a fresh synthetic fixture, not a provider observation.
Its connected B2/B4 *fixture states* are visibly synthetic, not live attachment
claims. B4 running/unknown latest attempt is shown prominently **alongside** an
older historical Daily preview success, explicitly not Vault publication or current
health. Newer failure/unknown never becomes historical success. Effect kind,
verification and changed/no-change/unknown remain separate. Epoch zero stays a
valid timestamp, not a fabricated absence; numbers display explicitly as synthetic
Unix seconds UTC (no live clock or invented start time). B5 has no next run or
timer. B1/B3 schedule stays unknown; no engine or cadence policy is chosen.

`controlReader()` is an isolated in-memory request-generation guard with no I/O.
`begin()` clears prior evidence; only the newest loading generation can settle
once. New malformed/failed observations clear all cards to unavailable; late old
success **or** failure cannot overwrite newer state. `snapshot()` returns detached
data. `scenario("control-v1")` feeds this guard a local fixture synchronously.
No actual polling/async transport is installed; the later integrator must carry
this guard through rendering and failure handling rather than trust revisions.

Future live reads, shared sanitization, capability/admission enforcement, strict
duplicate-key JSON parsing, durable request binding/lookup-only ambiguous outcomes,
CSRF/origin checks, connection validation and command/result retrieval require
separate integration review. Observation/action guidance is never an admission
token. Staging and execution admissions/receipts remain separate; this candidate
creates neither. Scheduler engine/bounds and B5 production upload transport/limits
remain human-owned decisions. No shared router, registration, B2/B4 backend panels,
#444 Daily Writer, #411 External Source, settings, service or runtime code changed.

## Dependency-free verification

From the repository root, with **already installed** tools:

```sh
bun tests/webui_design_v1.test.cjs
# Or: node tests/webui_design_v1.test.cjs
python -m unittest discover -s tests -p test_webui_design_v1.py -v
# Or existing pytest environment: python -m pytest -q tests/test_webui_design_v1.py
```

The JS suite executes actual model, rendering and event handlers with a minimal
DOM fixture and throwing network/storage stubs. The Python wrapper uses stdlib
HTMLParser and launches an installed Node/Bun; it skips runtime execution only
when neither exists. It imports no application/backend modules. DOM fixtures
are not a browser layout/accessibility audit. Browser screenshots, actual viewport
overflow, contrast/rendering and assistive-technology checks still require an
installed authorized browser and independent review; no dependency installation
is required or authorized by this component.

The suite retains the original **17** checks and adds **9** bounded v1 checks
(26 total), including the normative synthetic catalog extracted from the checked-in
contract, malformed/secret-like inputs, duplicate IDs/actions, invalid timestamps,
manual B5, unresolved B1/B3 schedules, read-only controls, stale generations,
independent latest/history effects and fixture detachment. The two Python checks
remain a stdlib template check and the installed-runtime wrapper; no runtime is
downloaded when absent. Final execution evidence and SHA256 manifest are reported
in the run handoff rather than embedded as self-referential file hashes here.

Next gate: independent source correctness/security review, then separately owned
browser layout/accessibility acceptance. Shared integration and any live rollout
are not authorized by this source candidate.
