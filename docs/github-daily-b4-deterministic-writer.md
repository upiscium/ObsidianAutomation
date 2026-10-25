# B4 deterministic Daily Note Writer — source integration (#444 / #298)

The B4 single-user application can now connect its **source-verified, no-LLM**
GitHub progress Preview to the existing Project Progress WebDAV Writer. This is
a source-only opt-in integration. It does not authorize or perform a deployment
to \`obsidian-control\`, and does not change the existing production LXC.

## Execution path

1. The existing **one** Snapshot app scheduler calls the persistent
   \`DailyJob.tick()\` hook (or an operator uses the existing B4 Run Now API).
   The source collector selects the **previous JST calendar date**.
2. B4 fetches watched GitHub Issue/PR evidence through the existing
   read-only authenticated client and stores immutable original
   evidence/receipt/capture bytes in the private Standard CAS.
3. \`run_once\` re-admits that CAS, builds a deterministic Japanese digest, and
   publishes create-only local Markdown/JSON Preview artifacts.
4. **Only when the whole app was explicitly launched with
   \`--state <private-state> --daily-publish\`**, the B4 Job invokes
   \`publish_verified_daily\` to re-read the exact Standard CAS. The preview
   digest SHA, capture SHA and JST date must agree before any WebDAV request.
5. The Writer targets only
   \`00-DailyNote/YYYY/MM/YYYY-MM-DD.md\` relative to the selected Nextcloud
   Vault folder. The Daily Note must already exist and have
   \`type: daily-review\` and exactly one visible \`# Project Progress\` H1.
   Other headings, frontmatter and existing note content are preserved.
6. The existing WebDAV transport performs a GET, requires a **strong ETag**,
   and PUTs only with \`If-Match\`. A 412 is an explicit conflict, not
   silently retried. An ambiguous PUT result is recovered only by fresh GET
   of *exact desired bytes*. A no-op update performs no PUT.
7. The B4 Job persists and displays a typed result. No per-day user approval,
   LLM dependency, separate service, or second timer is introduced.

## States and recovery

- \`preview_ready\`: source/digest/Preview verified, **no canonical Vault write**
  attempted. This is the default mode without the explicit startup flag.
- \`published\`: canonical Daily bytes observed exactly as desired, with
  \`publication_outcome=applied\`, \`recovered\` or \`already_desired\`. The
  latter denotes a repeat with no remote write.
- \`publish_failed\`: verified local Preview survives, but the remote write
  failed, was refused or could not be proven. This is **not** success.
- \`failed\`: source/Preview itself failed. \`unconfirmed\` is retained for
  interrupted workers on restart; a crash is not silently replayed.

The existing DailyJob SQLite state accepts prior Preview-only run records. The
new optional \`publication_outcome\` column is added without re-running old
jobs. Run IDs and typed results are available through existing B4 status and
receipt GET APIs. The B1–B5 catalog also reports actual B4 state.

## Activation boundary

The existing fresh LXC installer/systemd unit does **not** pass
\`--daily-publish\`; it remains Preview-only. Before enabling the Writer on a
real system, separately authorize deployment, a write-capable Nextcloud app
password, existing Daily Note template and the target's real ETag behavior.
Start the app with the explicit flag only after acceptance. Its daily
schedule remains **Off until enabled through existing B4 settings**.

One configured owner/scheduler is sufficient for unattended operation; a new
root Worker, duplicate Timer, separate account or Daily approval step is not
needed. Longer downtime/backfill policy, first-live-host E2E and operational
retry handling for missing Daily Notes are separately checked before GA.

The synthetic acceptance tests use fake GitHub and WebDAV transports and
assert Preview evidence binding, section preservation, strong ETag CAS,
HTTP 412, weak/missing ETag, missing Daily, byte-exact post-GET, ambiguous PUT
recovery, idempotent no-op, optional persistent Job status, source change and
nonpublishing default. These are **not** evidence of real provider success.
