# #304 R1/B — Workflow-scoped Setup Guide (source-only)

Status: uncommitted/unpushed source-only candidate. Not deployed.

## Product intent

ObsidianAutomation should not require Gitea solely to use GitHub
Daily Progress (B4), or GitHub solely to use Vault Snapshot (B2).
R1/A supplied a fixed three-step guide under Global System Settings.
R1/B adds a **temporary client-only choice** to show relevant steps.

| Workflow choice | Displayed setup steps |
| --- | --- |
| Both (default) | Nextcloud, Gitea Snapshot, GitHub Daily |
| Vault Snapshot only | Nextcloud, Gitea Snapshot |
| GitHub Daily only | Nextcloud, GitHub Daily |

The selection is *not* stored or an enable/run control. Reload resets
to Both. All existing connection forms, original save/validation APIs
and write-only credential behavior are unchanged and remain accessible.

## Status, stale data, and safety

The manual connection-status button uses only the relevant subset of
the three existing **fixed credential-free same-origin GET-only** routes.
Snapshot-only never requests GitHub status; Daily-only never requests
Gitea status. No new endpoint is created. The unselected slots are
populated internally with unknown/null observations. Results are
strictly validated as before, but only selected rows appear in the
guide and summary. The count refers to saved configuration
observations, not currently healthy connectivity, full Job readiness,
successful Vault output or proof of any B1/B3/B5 effect.

Selecting another workflow resets to Step 1, invalidates displayed
observations and aborts an in-flight status request. Keyboard focus stays
on the workflow selector so arrow-key selection can continue; explicit
Previous/Next actions may focus the new step heading. A response from
the old selection cannot repopulate the new workflow view. The strict
allowlist rejects unknown values and prototype-property names.
Prev/Next and opening an existing form are local DOM-only actions.

## Source footprint

1. Existing admin_dashboard.py HTML: accessible workflow selector and
   prominent explanation that this is display-only.
2. Existing setup_readiness.js: immutable both/snapshot/daily mapping
   to vetted existing connections, scoped navigation/status, and stale
   response prevention.
3. Existing status_manager.css: dark, responsive select with 44px touch
   target.
4. Existing Node/Python tests: exact scope, missing unrelated provider,
   no automatic status reads, no writes, explicit GET-only status,
   stale observation invalidation, and preserved secret boundaries.
5. This document.

The candidate requires exact-source tests and isolated browser preview,
then a separate Commit/Push/Draft PR owner gate. Ready, Merge and
production UI-only rollout each require independent explicit approval.

## Remaining dependencies

Safe removal of repeated Nextcloud credentials in the Gitea Snapshot
form requires the #301 backend save contract, not a browser-side cache.
AI Provider and Vault mapping are #301/#303. The full Setup Wizard,
B1/B3/B5 admission and fresh-LXC MVI remain unfinished under #304/#298.

Owner PC/phone acceptance of the already-live #531 simplified menu
(#530) and #539 R1/A guide is still pending; a simple Next is not
that acceptance.
