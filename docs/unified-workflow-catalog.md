# Five-workflow catalog for the fresh single-WebUI MVI (#298)

This source-only slice separates **five product workflows** from the five
legacy systemd services already shown by the admin dashboard. The presence of
source code or a systemd unit is **not evidence that the workflow is connected
to the new one-user scheduler, configured, or producing verified Vault effects**.

## Current product integration map

| Workflow | Existing source / separate runtime | New WebUI connection |
| --- | --- | --- |
| B1: AI-derived/review notes | Pre-review and AI pipeline; existing unit observation | Pending; legacy health ≠ shared-Job acceptance |
| B2: Vault Snapshot → private Gitea | `snapshot_app`, `snapshot_job`, `snapshot_setup` | Connected **only** when SnapshotJob is actually injected into the server |
| B3: Project/automation data → Vault | Project watcher, Sync/Writer/Core Promotion | Pending; no new configured writer or Vault mutation |
| B4: GitHub progress → Daily Note | Deterministic progress source/preview; #444 evolves independently | Pending; do not use a Draft preview PR as live delivery evidence |
| B5: ChatGPT Export intake | Parser/Importer and External Human Review (#411) | Pending; manual import should not require a timer |

The old `GET /api/v1/status` retains its unchanged **legacy systemd** five
service schema. It is intentionally separate from the new product-facing
`GET /api/v1/workflows`:

- `version=1`, `authority=observation_only`, exactly five fixed IDs in
  B1 → B2 → B3 → B4 → B5 order.
- `integration=pending` means the legacy/existing backend was **not** attached
  to this new WebUI Job API. It does **not** mean stopped, not installed or
  failed. For other workflows the configuration and schedule are
  `not_observed`; B5's schedule is `not_applicable`.
- `integration=not_attached` for B2 means this particular Web server did not
  receive a SnapshotJob; it does **not** claim the backup cannot run elsewhere.
- `integration=connected` for B2 means only that a SnapshotJob was attached.
  Its `configuration` separately distinguishes `missing` from `configured`;
  `schedule` reports effective On/Off, while a bounded `last_result` comes
  from its SQLite Job record. It does not prove real Nextcloud/Gitea E2E.
- `integration=unavailable` indicates an invalid/unreadable B2 observation.
  Never keep showing previous connected state as fresh.
- `control_available` means the **existing** B2 Snapshot settings section
  can be used when configured. This catalog adds **no new POST** or permission.

The endpoint must not query GitHub, Nextcloud, Gitea or systemd; it reads the
already injected in-process B2 job only. It exposes no credential data, paths,
remote URLs, run IDs or Git refs. The browser inserts all text via DOM
`textContent` and removes stale cards on an HTTP/schema failure. Host/Origin
restrictions and loopback binding remain unchanged.

## Next implementation sequence

1. Keep current B2 connected and verify its real Fresh-LXC deployment and
   private test Vault/Gitea restore separately in P3-C.
2. Accept the independently developed deterministic B4 `Run Once` preview
   contract (#444) before plugging it into the same scheduler/receipt API.
   Preview completion must not be presented as a Daily Note publication.
3. Adapt B3's existing Writer/Project Sync into the same one-user model,
   preserving CAS and no-unapproved-overwrite semantics.
4. Expose B1 Pre-review status/queue as its own workflow; do not infer success
   from a healthy AI Pipeline or submit a Job until configured.
5. Expose B5 as an explicit import operation; no timer by default, and
   `awaiting_human_review` remains distinct from a completed note.

The **`obsidian-control`** Debian 13 LXC is reserved for the *whole*
single-WebUI product; a standalone permanent B2 service is not the final
installation. This PR does not install anything, migrate an old LXC, start
workers, manage provider credentials or affect a canonical Vault.
