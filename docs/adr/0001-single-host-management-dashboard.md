# ADR-0001: UX-first single-host management plane for ObsidianAutomation

- **Status:** Proposed (not accepted; no production permission)
- **Date:** 2026-10-11
- **Parent issue:** [#298](https://github.com/upiscium/ObsidianAutomation/issues/298)
- **Design review:** [#299](https://github.com/upiscium/ObsidianAutomation/issues/299)
- **Baseline:** `main` at `ecb7e8235d6c4b5854f6c045d69655e2104f3366`

## 1. Problem

ObsidianAutomation automates Vault snapshots, public projection/promotion, AI generation/review, and GitHub Project/Daily flows, but an operator currently has to manage many Nextcloud account/share bindings, private configuration files and services across multiple Linux trust domains. There is no dedicated administration GUI for service control, credentials and Vault-path selection.

These were originally deliberate restrictions, not accidental cruft. Nevertheless, OS and remote-authority boundaries should not automatically become the user's setup burden.

**Product objective:** one supported, independently deployable ObsidianAutomation Linux host/unprivileged LXC, with a responsive, loginless, private-network management dashboard and one Nextcloud service account in the default Standard profile. No second LXC or generic Gitea Actions runner may be a dependency for ObsidianAutomation's baseline workflows. Existing Nextcloud, Gitea, GitHub and AI provider servers remain external.

## 2. Existing verified boundaries

- `docs/production-lxc-consolidation.md`: writer-side `obsidian-automation`, separately isolated `gitea-runner` and read-only `obsidian-snapshot-taker`. These are *documented deployment domains*, not a guarantee that each is presently active.
- `docs/installation.md` and `docs/ai-production-authority.md`: canonical writer, Review Intake reader, bounded `04-AI` projection cleanup are separate remote Nextcloud authorities; local processing stages use Unix identities and POSIX ACLs.
- `tools/host_runtime_lifecycle.py`: five managed recurring timers (AI pull, pre-review, GitHub Sync, GitHub Daily Progress, Core Promotion). `docs/host-runtime-lifecycle.md` contains an older count of four; runtime registry and documentation must converge.
- `src/obsidian_automation/pre_review_status.py`, `mirror_status.py`: useful non-secret read models already exist; many services are `Type=oneshot`, so `inactive` by itself is not unhealthy.
- `examples/gitea/public-projection.yml`: current public publication is a Gitea Actions host-runner workflow.
- `src/obsidian_automation/vault_snapshot.py` and `docs/live-vault-snapshot.md`: deterministic snapshot helper has no network effects itself.
- `00-DailyNote`, `05-Idea`, `04-AI`, `10-Project`, `11-Knowledge`, `98-System` are referenced in configuration, source validations, status pipelines, and/or schemas. A GUI folder picker alone cannot safely change physical layouts.

These details must be revalidated against the eventual implementation head and active deployment before cutover.

## 3. Proposed runtime topology

```text
user browser / phone
   │ private ingress: loopback/WireGuard or comparably restricted gateway
   ▼
Management Web UI + unprivileged Management API (no generic shell/root access)
   ├── read-only Status aggregator / service registry / metadata
   ├── Connections & Vault mapping configuration / validated staging
   └── local privileged-operation broker (fixed actions only; OS peer identity)
                          │
           same Linux host / one LXC
   ┌──────────────────────┼────────────────────────────────────────┐
   │ systemd role services│   Snapshot (read-only capability)      │
   │ AI / GitHub / Core    │   local Public Publisher (no generic  │
   │ Unix identities/ACLs │      host-mode Actions runner)         │
   └──────────────────────┼────────────────────────────────────────┘
                          │
                  Vault Gateway
            (only role with Standard
             Nextcloud remote secret)
                          │ HTTPS WebDAV
                       Nextcloud

Separate, separately scoped connectors: AI provider, Gitea, GitHub.
```

Single *host* does not mean a single *process* or single root-privileged service. Keep the Unix identities, filesystem ACLs, immutable artifact/provenance contracts, fixed service registry and systemd sandboxing.

A generic host-mode Gitea Runner MUST NOT be placed into the same privileged-data LXC to satisfy the one-host checkbox. Replace its ObsidianAutomation publication use with a narrowly scoped local publisher; other unrelated Gitea workflows may keep an optional external runner not required by this product.

## 4. Credential profiles and their honest guarantees

**Decision is provisional.** The Standard/Strict product choice (one operator-managed Nextcloud account vs multiple remotely restricted accounts) does **not** by itself choose the internal implementation of Standard. A Standard implementation using a central DAV Gateway and a simpler Standard implementation using service-scoped `systemd LoadCredential=` delivery must be independently compared in #308. The latter is less invasive to existing `rclone.conf` clients but grants each recipient the broad Nextcloud credential; it may still be preferable if the accepted personal-use threat model prioritizes maintenance simplicity over compromise isolation. Neither is equal to Strict's server-side account ACLs.


| Dimension | Standard (default proposal) | Strict (compatible option) |
| --- | --- | --- |
| Nextcloud user accounts | One **dedicated** service account, one configured credential to Gateway | Several remotely restricted accounts/share bindings |
| Internal processes | Separate Unix identities and Gateway capability checks | Separate Unix identities and credentials/remote shares |
| Snapshot | Remote reads through read-only Gateway capability | Remote read-only share/account |
| Canonical creation/update/delete | Gateway enforces per-role path/verb/CAS rules | Remote Nextcloud permissions **and** local contract |
| Gateway/remote credential compromise | Broad access up to that account's Nextcloud permissions | Restricted by the compromised credential's remote share |
| Setup burden | Lower | Higher |
| Security equivalence | **Not equivalent to Strict** | Stronger external enforcement |

Standard MUST NOT silently reuse the human's primary Nextcloud password; prefer a dedicated application password/account. Provisioning brand-new Nextcloud server accounts is optional; v1 can take an operator-supplied app credential and does not require Nextcloud server administrator permission.

One account in Standard is an explicit tradeoff: the remote server cannot distinguish the individual authority of the services behind a shared credential. Treat Gateway bugs/compromise as high-impact. There must be no documentation falsely advertising remotely enforced read-only Snapshot in this mode.

## 5. Vault Gateway contract (proposed)

Only the Gateway may read the Standard Nextcloud credential. Service-to-Gateway requests travel over restricted local Unix sockets with peer identity verification, **not** arbitrary remote proxies.

Compile a fixed, reviewed capability matrix per service identity. The matrix must constrain all of: target Vault / canonical root / permitted relative path patterns / HTTP method / operation semantics, e.g.:

- Snapshot: `read/list`, never `write/update/delete`;
- AI context readers: permitted read roots, never remote mutation;
- projection creator: `create-if-absent` under `04-AI`;
- Review Intake: bounded read of Review projections;
- Knowledge canonical writer: exact proposed target, `If-None-Match: *` and independent read-back check;
- GitHub Project/Daily writer: bounded owned path/section, `If-Match` CAS and read-back check;
- cleanup: exact-case and auditable `04-AI` set only, delete never generalized to a Vault root;
- Core Promotion: reviewed include list, checkpoint and remote-live reconciliation semantics unchanged.

No privilege escalation via caller-supplied role/path, arbitrary HTTP verb, URL, redirect or header. Normalize paths exactly and reject `..`, encoded separators, ambiguities, case-fold collisions, symlinks where relevant, escaping of the selected Vault. Preserve conditional-write and exact-byte verification semantics. A Gateway reimplementation must pass the existing writer, receipt, recovery and Human Review contracts, including #287.

If Gateway unavailable or its policy identity is stale, deny effects rather than passing a raw remote credential to the job.

### 5.1. rclone integration feasibility gate (not solved by this ADR)

**Blocking spike: [#308](https://github.com/upiscium/ObsidianAutomation/issues/308).** An initial disposable proof is recorded in [rclone Unix WebDAV fixture results](../spikes/308-rclone-unix-webdav-fixture.md): rclone v1.75.1 synchronized a filtered local fixture through a Unix-domain DAV socket, but the default VFS directory cache gave a stale successful sync after an upstream change; disabling that cache resolved the local reproduction. This is not a real Nextcloud or per-service security test.

Existing `vault_mirror.py` runs `rclone sync` with a named remote and `rclone.conf`; AI and GitHub mirrors and the Snapshot path currently depend on separate rclone/WebDAV credentials. The above Unix-socket Gateway is a *proposed authority contract*, not a drop-in compatible backend for rclone.

Before claiming Standard single-credential support, prototype at least a Gateway-owned mirror pull, a scoped local DAV interface (with actual caller authentication, not an unauthenticated loopback listener), and a constrained Gateway-launched rclone child. Model who can read the remote credential and who can write each local mirror in each option. In particular, passing the secret to a subprocess is still credential exposure, even if the credential is short-lived.

The selected adapter must preserve named-remote filtering behavior, local-only `--delete-after` / `--delete-excluded`, canonical I/O and mirror-read-view locks, Snapshot double-pull/check, remote freshness limitations, and untrusted-path handling. Test no remote writes and matching local manifests on a disposable DAV fixture before integrating with service users. If the adapter is not viable or increases risk excessively, do **not** fake Gateway isolation; retain multiple remotely scoped accounts behind a one-screen GUI instead and revise this ADR.

## 6. Loginless management console

Loginless means **no separate GUI username/password prompt**, not unauthenticated exposure to the public Internet.

Initial default is `127.0.0.1` / Unix socket. Access from a phone should be provided via explicitly configured WireGuard/private ingress, with a restrictive network firewall/allowlist. Do not implicitly trust all LAN clients. Document that every device allowed through this boundary gains administrative UI access.

The web stack needs Host allowlist, Origin checks and CSRF mitigation for state-changing requests, protection against DNS rebinding and untrusted Vault/GitHub strings/XSS, tight input and response bounds, and audit logs with no credentials or Vault body dumps. Do not store tokens in browser local storage; secret fields are write-only.

The Web API stays non-root. A local operation broker exposes only a fixed typed action registry, never an arbitrary command, systemd unit name, filesystem path or shell expression.

## 7. Effect-aware service control

A timer's `active` / `enabled` is not the same as its oneshot service's current status. Expose schedule, last run, proven last success, freshness, queue/backpressure, missing config and provider unavailability separately; unknown must remain unknown.

| Operator action | Effect class | Default execution |
| --- | --- | --- |
| View state/last sanitized result | Observation | No canonical effect |
| Pause/resume schedule | Host scheduling | Does not kill active job by default; preserve intent |
| Trigger one existing service | Potentially remote-effectful | Existing service-specific authority/preflight; clearly describe effect |
| Replace secret / Vault mapping | Configuration | Validate and stage; no implicit remote modification |
| Activate staged production revision | Live deployment | Exact-target approval, #294 stage/activate gate |
| Acknowledge rejected Validation | Human disposition | #287 bound request / audited cleanup, not unconditional deletion |
| Force stop active job | Risk of partial effect | Separate explicit guarded operation; not in MVP |

The registry must be single-source-of-truth for status, packaging, control and lifecycle. Respect durable lock/pending journal, exact SHA, running-work drain and no duplicate writer / parallel runner. Simple dashboard clicks do not waive the existing consent boundary.

## 8. Config and Vault bindings

Connections: Nextcloud, AI provider(s), Gitea and GitHub. Separate non-secret endpoint/account/repository/model identifiers from secret material. Secrets can be set/rotated/invalidated, never read back through the API. Prepare -> bounded no-write test -> atomic apply; failure must preserve the last accepted live config and emit a redacted error. Production config location, ACLs, and updater behavior must be compatible with `#253`/`#254`.

Versioned Vault bindings should associate logical roles (Daily, Idea, Project, Knowledge, AI review, System, Snapshot, publisher source) with an explicitly selected canonical Nextcloud Vault root and physical path. Per-service bindings must be representable, including different Vault roots where policy permits. Stage 1 may support choosing an existing root; Stage 2 supports remapping logical folders after schemas/filters/review identity are decoupled.

Changing a binding is **not** a blind move/rename of human content. Dry-run its effect, classify pending or currently executing jobs, validate origin and destination, then activate only after explicit consent. Retain provenance of completed stages and protect handwritten Daily/Knowledge/Review content from overwrite or deletion.

## 9. Snapshot and public publication in one host

Keep Snapshot's stable-mirror and deterministic Git snapshot proof, isolated working paths, verified pull and conditional push. Snapshot's remote read-only *capability* is enforced by Gateway in Standard and by the remote account additionally in Strict.

Replace the generic host-mode Gitea Actions publisher with a dedicated local, non-interactive publisher using the existing `obsidian-public-publish` validation/commit logic and reviewed publishing credentials. Trigger only from verified new snapshot provenance (or bounded polling with equivalent proof); serialize publication. Preserve `repository_owned` files, clean-worktree assertions, no-change behavior, validation failures, optimistic Git push and network-retry semantics. Never allow arbitrary untrusted Gitea workflow steps inside the secrets-bearing LXC.

## 10. Migration, rollback and acceptance gates

1. **Read-only inventory** of existing LXC services, remote account/share scope, filesystem metadata, immutable state, Git refs, timers, config existence; report no credential values.
2. **Stage** exact reviewed SHA, new management/role services, Gateway policy, local publisher and private config without enabling writers; preserve old LXC roles as rollback candidates.
3. **Dry-run and verify** Vault root/bindings, Snapshot consistency, pending AI & Daily jobs, generated state, remote readback, Gitea/GitHub connectivity and invalid credentials.
4. **Quiesce old effects**: stop further scheduling, drain active work, verify locks and absence of in-flight canonical writers/publishers. Do not assume a process-count check alone proves idle.
5. **Copy durable state** under a quiesced boundary. Rebuild disposable mirrors only after ensuring no data/provenance loss. Re-run role permission/secret-readability smokes.
6. **Human-gated activate** the new one-host profile only after no duplicate authority, code revision, policy identity, and no-write gates pass.
7. **Observe and accept** live snapshot, AI Review, GitHub Daily CAS, Core Promotion and publication, checking remote bytes and receipts. Keep old containers disabled but recoverable until explicit disposal approval.

**Rollback limitation:** after new remote writes are committed, simply restoring old databases/re-enabling old writers may overwrite or replay effects. A safe switch-back must reconcile the new authoritative remote state first; otherwise stop effects and fix forward. Never promise automatic lossless rollback of an already mutating system.

## 11. Test / review matrix

- Fresh single-LXC install and cold restart; multiple Unix identities, no root web server.
- Browser-origin, CSRF, Host, DNS rebinding, XSS and unauthorized ingress tests.
- Secret masking/rotation, direct unauthorized file reads, leaked logs/receipts, SSRF and API input bounds.
- Gateway per-role capability denial, path ambiguity, CAS conflict, timeout, replay, delete confinement, readback mismatch.
- Existing immutable AI validation/review/executor/cleanup evidence, #287, and no silent Human Review bypass.
- Daily manually edited section preservation, `If-Match` CAS, timestamp/bindings, no semantic recursion changes.
- Snapshot pull/check failure, Git publication race and idempotent no-op, no generic Runner requirement.
- Upgrade journal/timer restoration/#294 exact-target consent gate, #253 build hygiene, #254 non-root package import.
- Multi-LXC state migration dry-run and interrupted-resume recovery; no double active writer.
- Separate Security and Correctness review before approving the architecture and before any production cutover.

## 12. Decision points and rollout order

Pending approval:
1. Accept the Standard product goal (one operator-managed Nextcloud account) with weaker **remote** account isolation, keeping Strict as an explicit choice; choose **direct per-service credentials vs Gateway** only after #308's evidence and threat comparison.
2. Approve Runnerless local Publisher for the *ObsidianAutomation-only* publication path, not for arbitrary Gitea workflows.
3. Confirm authorized phone/network ingress; do not interpret "loginless" as public binding.
4. Decide whether force-stopping active effects is even necessary; safest MVP is schedule pause + controlled drain.

Implementation feasibility gate: [#308](https://github.com/upiscium/ObsidianAutomation/issues/308) rclone/Nextcloud adapter.

Implementation issues: [#300](https://github.com/upiscium/ObsidianAutomation/issues/300) Management API; [#301](https://github.com/upiscium/ObsidianAutomation/issues/301) Connections; [#302](https://github.com/upiscium/ObsidianAutomation/issues/302) Gateway; [#303](https://github.com/upiscium/ObsidianAutomation/issues/303) Vault mapping; [#304](https://github.com/upiscium/ObsidianAutomation/issues/304) Dashboard; [#305](https://github.com/upiscium/ObsidianAutomation/issues/305) Snapshot/Publisher; [#306](https://github.com/upiscium/ObsidianAutomation/issues/306) migration.

**This ADR proposes, but does not authorize, software deployment, credentials migration, LXC removal, Nextcloud writes or public publishing.**
