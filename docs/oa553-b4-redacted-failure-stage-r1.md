# #553 / B4 — bounded redacted failure-stage Receipt (R1, source-only)

**Scope:** The existing DailyJob in the single-app source records a finite
failure stage/code in its own SQLite Run receipt and exposes those two optional
strings through the existing GET endpoints. No Run Once, schedule change,
Provider API request, deployment or Vault write is introduced by this change.
No new task is activated; the pre-existing B4 schedule is unchanged.

## Failure diagnostic contract

- The pair is written only for failed or publish_failed. It is never arbitrary
  exception text; a static allowlist controls writing and reading. Out-of-vocabulary or disallowed SQLite pairs are projected as null;
  this does not authenticate an allowlisted pair written by a privileged actor.
- source_admission / not_configured: Daily source is not configured.
- source_admission / source_unavailable: unclassified source admission error.
- source_pull / pull_failed: Nextcloud mirror pull did not report success;
  it does NOT reveal whether auth, network, timeout or content caused it.
- project_selection / no_watched_projects: B4 source found no watched GitHub
  Projects; this is not proof the Vault has no Projects.
- source_verification / source_changed: source changed during operation.
- collection_preview / collection_or_preview_failed: GitHub observations,
  source-route processing or local Preview/CAS generation failed; the job
  does not infer the specific substage within the existing combined collector.
- runner / unexpected_exception: generic unexpected runner error.
- preview_validation / invalid_preview_receipt: Preview contract rejected
  returned data, including unverifiable publication/write claims.
- publication / outcome_unconfirmed: publisher raised after being invoked.
  This is NOT evidence of no remote effect. Never blindly retry.
- publication / invalid_publication_receipt: publisher returned an output
  rejected by the existing strict publication validator. Remote effect
  may still be uncertain.

An old failed Run is not retroactively diagnosed. Its new columns are NULL
after the additive, idempotent local SQLite migration. Overall status,
verified Preview hashes, publication outcome, Run ID, trigger and configured
scheduler remain unchanged. Interrupted running/queued Runs continue to be
reconciled as unconfirmed; no automatic retry is introduced.

All codes are selected from static code, never from exception text, HTTP
content, token values, paths, Nextcloud notes, GitHub bodies, or rclone
output. Existing API route, authentication and origin rules are unchanged.
The diagnostic fields are observations, never authorization. The strict
B4 workflow-control read adapter accepts the two new optional fields, but
still projects effect authority as unknown and never relays private details.

## Limits and approval boundaries

- No new endpoint or WebUI action. GET does not trigger collection.
- No real Provider I/O was performed while building or testing R1. Synthetic
  source/collector/publisher fixtures exercise disposable state only.
- The historical B4 scheduled failure on 2026-11-09 remains cause UNKNOWN;
  this change improves future receipts only.
- Publication, Python 3.13 service-user staging, Deploy, schedule activation,
  canonical Writer/Core Promotion, real Provider I/O, manual Run Once and
  production upgrade each require separate owner approval.
