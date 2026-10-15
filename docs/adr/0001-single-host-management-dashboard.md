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
             Nextcloud connection boundary
              (implementation undecided)
        ┌─────────────────┼─────────────────┐
    managed direct    one remote-I/O     scoped UDS DAV
    credentials       owner + local      Gateway/proxy
    to selected jobs  verified views     per-role policy
        └─────────────────┼─────────────────┘
                          │ HTTPS WebDAV
                       Nextcloud

Separate, separately scoped connectors: AI provider, Gitea, GitHub.
```

Single *host* does not mean a single *process* or single root-privileged service. Keep the Unix identities, filesystem ACLs, immutable artifact/provenance contracts, fixed service registry and systemd sandboxing.

A generic host-mode Gitea Runner MUST NOT be placed into the same privileged-data LXC to satisfy the one-host checkbox. Replace its ObsidianAutomation publication use with a narrowly scoped local publisher; other unrelated Gitea workflows may keep an optional external runner not required by this product.

## 4. Credential profiles and their honest guarantees

**Status: proposed, pending design/security acceptance.** #308 established authenticated direct rclone sync and a Nextcloud-backed read-only Unix DAV sidecar against a disposable Nextcloud 35.0.1 fixture; the [evidence](../spikes/308-nextcloud-authenticated-gateway-and-cas.md) also demonstrates a destructive generic-rclone conditional-upload failure. The operator-facing Standard/Strict choice (one account vs multiple remotely restricted accounts) remains separate from the internal transport. For **initial Standard v1**, prefer centrally configured credentials delivered to existing explicitly remote-facing service identities with as little new transport code as possible, while preserving typed direct HTTP CAS handlers. This has a larger credential-compromise blast radius than a Gateway or Strict; it is **not** security-equivalent. Reserve remote-I/O-owner/Unix DAV Gateway hardening for an independently justified risk or later phase, rather than making all users wait for a new proxy.


| Dimension | Standard (default proposal) | Strict (compatible option) |
| --- | --- | --- |
| Nextcloud user accounts | One **dedicated** service account, one user-managed connection | Several remotely restricted accounts/share bindings |
| Internal processes | Separate Unix identities; credential exposure/capabilities depend on selected adapter | Separate Unix identities and credentials/remote shares |
| Snapshot | Read-only by application/capability contract; shared remote account may still have writer rights | Remote read-only share/account |
| Canonical creation/update/delete | Existing path, CAS, provenance and review guards; selected adapter may add server-side checks | Remote Nextcloud permissions **and** local contract |
| Remote credential compromise | Broad access up to that account's Nextcloud permissions, regardless of adapter | Restricted by the compromised credential's remote share |
| Setup burden | Lower | Higher |
| Security equivalence | **Not equivalent to Strict** | Stronger external enforcement |

Standard MUST NOT silently reuse the human's primary Nextcloud password; prefer a dedicated application password/account. Provisioning brand-new Nextcloud server accounts is optional; v1 can take an operator-supplied app credential and does not require Nextcloud server administrator permission.

One account in Standard is an explicit tradeoff: the remote server cannot distinguish the individual authority of the services behind a shared credential. Treat a compromised credential holder (whether a direct consumer, central I/O owner or Gateway) as high-impact. There must be no documentation falsely advertising remotely enforced read-only Snapshot in this mode.

## 5. Remote-I/O authority contract and optional Gateway design (proposed)

All Standard implementations must preserve existing fixed effects, CAS/provenance validation, local isolation and explicit audits. They differ in **which processes hold the remote credential**:

- Managed direct credentials: only intentionally authorized remote-facing services can receive the shared credential through protected systemd secret delivery; the service's compromised credential grants the full shared remote account authority. Do not claim a local filter narrows remote permissions.
- Single remote-I/O owner: only the owner holds the credential; reads become verified local mirrors/role-limited views, while remote mutations are independently checked typed requests. Concurrency, stale-data and durable-stage contracts must remain intact.
- Scope-specific UDS Gateway (the optional design below): only the Gateway holds the credential. Service-to-Gateway requests travel over restricted local Unix sockets with peer identity verification, **not** arbitrary remote proxies.

Compile a fixed, reviewed capability matrix per service identity. The matrix must constrain all of: target Vault / canonical root / permitted relative path patterns / HTTP method / operation semantics, e.g.:

- Snapshot: `read/list`, never `write/update/delete`;
- AI context readers: permitted read roots, never remote mutation;
- projection creator: `create-if-absent` under `04-AI`;
- Review Intake: bounded read of Review projections;
- Knowledge canonical writer: exact proposed target, `If-None-Match: *` and independent read-back check;
- GitHub Project/Daily writer: bounded owned path/section, `If-Match` CAS and read-back check;
- cleanup: exact-case and auditable 04-AI set only, delete never generalized to a Vault root. New deterministic rejection cleanup (#309) uses a separately versioned, server-originated request/result, exact four fixed pre-review stage targets and immutable rejection evidence; existing Human Review cleanup remains a distinct six-stage authority.
- Core Promotion: reviewed include list, checkpoint and remote-live reconciliation semantics unchanged.

No privilege escalation via caller-supplied role/path, arbitrary HTTP verb, URL, redirect or header. Normalize paths exactly and reject `..`, encoded separators, ambiguities, case-fold collisions, symlinks where relevant, escaping of the selected Vault. Preserve conditional-write and exact-byte verification semantics. A Gateway reimplementation must preserve the existing writer, receipt, recovery and Human Review contracts unchanged. Separately, the new #309 server-originated deterministic-rejection cleanup requires its own four-stage exact-case binding, conditional DELETE and immutable receipts; it neither replaces nor broadens Human Review's existing six-stage cleanup authority. Ordinary deterministic Validation rejection does not require an Acknowledge request.

If a Gateway is selected and unavailable or its policy identity is stale, deny effects rather than passing a raw remote credential to the job. Do not use an unsafe fallback from a Gateway deployment to direct credentials without a new reviewed config change.

### 5.1. rclone integration evidence and remaining security gate

**Feasibility spike: [#308](https://github.com/upiscium/ObsidianAutomation/issues/308).** [Initial synthetic rclone Unix DAV fixture](../spikes/308-rclone-unix-webdav-fixture.md) demonstrated named-remote compatibility and found that the default 5-minute VFS directory cache can yield successful-but-stale sync. [Actual Nextcloud 35.0.1 authenticated fixture](../spikes/308-nextcloud-authenticated-gateway-and-cas.md) subsequently demonstrated direct rclone sync (0 differences / 3 files), read-only scoped Unix DAV proxy, update/delete convergence with cache disabled, and strong-ETag direct HTTP conditional writes. **Critical negative result:** in three synthetic cases `rclone copyto --header-upload 'If-None-Match: *'` received a 412 but then deleted the pre-existing remote file; the HTTP server log showed PUT 412 followed by DELETE 204. Consequently, generic rclone upload must **never** replace the dedicated typed conditional WebDAV writer, even if it appears to support conditional headers. Client-side `--filter-from` is not a remote authorization boundary. These tests establish functional read feasibility, not cross-UID security, large scale, or production readiness.

Existing `vault_mirror.py` runs `rclone sync` with a named remote and `rclone.conf`; AI and GitHub mirrors and the Snapshot path currently depend on separate rclone/WebDAV credentials. The above Unix-socket Gateway is a *proposed authority contract*, not a drop-in compatible backend for rclone.

Before Standard production acceptance, implement one chosen credential-distribution design and test under real distinct Unix service identities, including secret-file access, unauthorized remote reads/writes, rotation/rollback, and copy/move/delete safeguards. A read-only Unix DAV sidecar has been prototyped under one Unix identity, not qualified as multi-role enforcement. Under the proposed initial Standard delivery model, document plainly that any recipient can potentially perform all remote operations permitted by the shared Nextcloud account; role-specific `rclone` filters cannot prevent this. Do not silently copy a remote credential into LLM-facing processes.

The selected deployment must preserve named-remote filtering, **local-only** `--delete-after` / `--delete-excluded`, canonical I/O and mirror-read-view locks, Snapshot double-pull/check, remote freshness limitations, and untrusted-path handling. Keep remote writes in the existing independently checked conditional HTTP transport, not `rclone copyto` or CLI-level upload headers. Any Gateway implementation must verify the same typed CAS guarantees and readback, and must not use a failed upload's unconditional DELETE cleanup. If a Gateway raises more risk/maintenance cost than it reduces, the Standard direct/central-I/O model is acceptable only with its explicitly reviewed shared-credential blast radius; Strict remains the remote-ACL option.

## 6. Loginless management console

Loginless means **no separate GUI username/password prompt**, not unauthenticated exposure to the public Internet.

Initial default is `127.0.0.1` / Unix socket. Access from a phone should be provided via explicitly configured WireGuard/private ingress, with a restrictive network firewall/allowlist. Do not implicitly trust all LAN clients. Document that every device allowed through this boundary gains administrative UI access.

The web stack needs Host allowlist, Origin checks and CSRF mitigation for state-changing requests, protection against DNS rebinding and untrusted Vault/GitHub strings/XSS, tight input and response bounds, and audit logs with no credentials or Vault body dumps. Do not store tokens in browser local storage; secret fields are write-only.

The Web API stays non-root. A local operation broker exposes only a fixed typed action registry, never an arbitrary command, systemd unit name, filesystem path or shell expression.

## 7. Effect-aware service control

A timer's `active` / `enabled` is not the same as its oneshot service's current status. Expose schedule, last run, proven last success, freshness, queue/backpressure, missing config and provider unavailability separately; unknown must remain unknown.

| Action / event | Effect class | Default execution |
| --- | --- | --- |
| View state/last sanitized result | Observation | No canonical effect |
| Pause/resume schedule | Host scheduling | Does not kill active job by default; preserve intent |
| Trigger one existing service | Potentially remote-effectful | Existing service-specific authority/preflight; clearly describe effect |
| Replace secret / Vault mapping | Configuration | Validate and stage; no implicit remote modification |
| Activate staged production revision | Live deployment | Exact-target approval, #294 stage/activate gate |
| Deterministic Validation rejection cleanup | Audited bounded background DELETE | #309 requires a versioned, server-owned exact-case/generation request, canonical rejected Validation record + original published projection/result hashes, a four-path 04-AI allowlist, and Sync-only conditional DELETE after remote strong-ETag/bytes revalidation; no Human Acknowledge, never unconditional delete |
| Force stop active job | Risk of partial effect | Separate explicit guarded operation; not in MVP |

For automatic deterministic Validation rejection in #309, the server-side
cleanup producer and Sync transport MUST retain separately versioned immutable
request/result schemas, original projection bindings, and a strict fixed path
derivation. Verify the remote target's exact bytes/identity and a strong ETag
immediately before destructive effects, then use a conditional If-Match DELETE;
never assume a local lock alone protects a remotely replaced projection.
Unexpected Human edits, a changed ETag, a newer generation or missing binding
must fail closed without deleting another note. Lost DELETE responses, partial
cleanup, already-absent targets and replay/republication require bounded
reconciliation backed by immutable evidence and a durable non-resurrection
rule. A cleanup conflict becomes a visible operational incident in #300/#304,
not a hidden successful Validation rejection. These conditions are a future
acceptance contract, not proof that existing Human Review cleanup already
implements conditional remote deletion.

The registry must be single-source-of-truth for status, packaging, control and lifecycle. Respect durable lock/pending journal, exact SHA, running-work drain and no duplicate writer / parallel runner. Simple dashboard clicks do not waive the existing consent boundary.

### 7.1. User-facing Review vs operational incidents

ObsidianCore [#220](https://github.com/upiscium/ObsidianCore/issues/220) / Draft PR #221 is a **Human Review-only action inbox**. It does not provide a service-health signal; a queue with zero Reviews must never be interpreted as a healthy or even running pipeline. The Automation Management API (#300) and mobile Dashboard (#304) exclusively own operational incidents and remediation status.

- A durably confirmed `deterministic_reject` is an **expected non-promotion outcome**, not an operational failure or required Human Acknowledge. The bounded server-originated cleanup contract is #309.
- `blocked` and `retry_exhausted` are actionable incidents. A retryable generation failure is transient, distinct from retry budget exhaustion.
- Verified provider/transport unavailability, failed Nextcloud cleanup/consistency checks and recurring service errors also belong in the operations Dashboard; they must **not be silently deleted or mislabeled as normal Validator rejections**.
- `UNKNOWN` data, missing operational sources, stale last-success evidence and disabled timers must be distinguished from `OK`. A past successful systemd oneshot cannot prove present health or data freshness.
- Report bounded incident components, reason codes, affected counts, timestamps only when supported by trustworthy evidence, and explicit recovered/active distinctions; never raw credentials, logs, untrusted model text or arbitrary Vault paths. Read-only status pages do not perform remote/canonical writes.

This ownership boundary does not override #294 stage/activate or future service-control safety gates. Core Review-only production promotion is blocked until the operational incident replacement has independent acceptance and genuine failures remain discoverable.

## 8. Config and Vault bindings

Connections: Nextcloud, AI provider(s), Gitea and GitHub. Separate non-secret endpoint/account/repository/model identifiers from secret material. Secrets can be set/rotated/invalidated, never read back through the API. Prepare -> bounded no-write test -> atomic apply; failure must preserve the last accepted live config and emit a redacted error. Production config location, ACLs, and updater behavior must be compatible with `#253`/`#254`.

Versioned Vault bindings should associate logical roles (Daily, Idea, Project, Knowledge, AI review, System, Snapshot, publisher source) with an explicitly selected canonical Nextcloud Vault root and physical path. Per-service bindings must be representable, including different Vault roots where policy permits. Stage 1 may support choosing an existing root; Stage 2 supports remapping logical folders after schemas/filters/review identity are decoupled.

Changing a binding is **not** a blind move/rename of human content. Dry-run its effect, classify pending or currently executing jobs, validate origin and destination, then activate only after explicit consent. Retain provenance of completed stages and protect handwritten Daily/Knowledge/Review content from overwrite or deletion.

## 9. Snapshot and public publication in one host

Keep Snapshot's stable-mirror and deterministic Git snapshot proof, isolated working paths, verified pull and conditional push. In **initial Standard v1 with managed shared direct credentials**, Snapshot is read-only only at the intended application/role boundary: the Nextcloud credential itself may have write/delete authority. Do **not** claim remote read-only enforcement in this profile. An optional reviewed Gateway could strengthen local per-role enforcement but remains a separate unqualified implementation; only a restricted Nextcloud account/share such as Strict provides independent remote ACL enforcement. Document the shared-credential compromise blast radius explicitly.

Replace the generic host-mode Gitea Actions publisher with a dedicated local, non-interactive publisher using the existing `obsidian-public-publish` validation/commit logic and reviewed publishing credentials. Trigger only from verified new snapshot provenance (or bounded polling with equivalent proof); serialize publication. Preserve `repository_owned` files, clean-worktree assertions, no-change behavior, validation failures, optimistic Git push and network-retry semantics. Never allow arbitrary untrusted Gitea workflow steps inside the secrets-bearing LXC.

## 10. Migration, rollback and acceptance gates

1. **Read-only inventory** of existing LXC services, remote account/share scope, filesystem metadata, immutable state, Git refs, timers, config existence; report no credential values.
2. **Stage** exact reviewed SHA, new management/role services, the selected remote-I/O adapter configuration (Gateway policy only if Gateway is selected; otherwise reviewed direct/central-I/O credential bindings and service-identity allowlists), local publisher and private config without enabling writers; preserve old LXC roles as rollback candidates.
3. **Dry-run and verify** Vault root/bindings, Snapshot consistency, pending AI & Daily jobs, generated state, remote readback, Gitea/GitHub connectivity and invalid credentials.
4. **Quiesce old effects**: stop further scheduling, drain active work, verify locks and absence of in-flight canonical writers/publishers. Do not assume a process-count check alone proves idle.
5. **Copy durable state** under a quiesced boundary. Rebuild disposable mirrors only after ensuring no data/provenance loss. Re-run role permission/secret-readability smokes.
6. **Human-gated activate** the new one-host profile only after no duplicate authority, code revision, exact selected adapter/credential configuration identity, per-service secret exposure tests, and no-write gates pass. Validate Gateway policy identity only if the Gateway adapter is selected; a direct Standard profile is not required to invent a Gateway policy or claim Gateway isolation.
7. **Observe and accept** live snapshot, AI Review, GitHub Daily CAS, Core Promotion and publication, checking remote bytes and receipts. Keep old containers disabled but recoverable until explicit disposal approval.

**Rollback limitation:** after new remote writes are committed, simply restoring old databases/re-enabling old writers may overwrite or replay effects. A safe switch-back must reconcile the new authoritative remote state first; otherwise stop effects and fix forward. Never promise automatic lossless rollback of an already mutating system.

## 11. Test / review matrix

- Fresh single-LXC install and cold restart; multiple Unix identities, no root web server.
- Browser-origin, CSRF, Host, DNS rebinding, XSS and unauthorized ingress tests.
- Secret masking/rotation, direct unauthorized file reads, leaked logs/receipts, SSRF and API input bounds.
- For every selected mode: per-role secret accessibility and remote compromise blast radius, CAS conflict, timeout, replay, delete confinement, readback mismatch; for optional Gateway also socket/root/method checks and stale-cache behavior. The #308 rclone 412→DELETE regression must never reach canonical transports.
- #309 verified remote bytes/strong-ETag If-Match delete, lost-response recovery, four-stage-only cleanup, no resurrection or cross-case deletion; no arbitrary path from a client.
- Existing immutable AI validation/review/executor/cleanup evidence and #309 verified automatic rejection cleanup (never arbitrary or Human-ack-driven DELETE), with no silent Human Review bypass.
- Daily manually edited section preservation, `If-Match` CAS, timestamp/bindings, no semantic recursion changes.
- Snapshot pull/check failure, Git publication race and idempotent no-op, no generic Runner requirement.
- Upgrade journal/timer restoration/#294 exact-target consent gate, #253 build hygiene, #254 non-root package import.
- Multi-LXC state migration dry-run and interrupted-resume recovery; no double active writer.
- Separate Security and Correctness review before approving the architecture and before any production cutover.

## 12. Decision points and rollout order

Pending approval:
1. Decide whether to accept the proposed Standard v1 **single operator-managed connection / centrally delivered credentials** despite weaker **remote** isolation; keep Strict as an explicit choice. The #308 fixture supports basic direct and Gateway read viability but does **not** qualify a production deployment. Do not mandate Gateway before its additional enforcement value justifies the cost.
2. Approve Runnerless local Publisher for the *ObsidianAutomation-only* publication path, not for arbitrary Gitea workflows.
3. Confirm authorized phone/network ingress; do not interpret "loginless" as public binding.
4. Decide whether force-stopping active effects is even necessary; safest MVP is schedule pause + controlled drain.

Implementation feasibility gate: [#308](https://github.com/upiscium/ObsidianAutomation/issues/308) rclone/Nextcloud adapter.

Implementation issues: [#300](https://github.com/upiscium/ObsidianAutomation/issues/300) Management API; [#301](https://github.com/upiscium/ObsidianAutomation/issues/301) Connections; [#302](https://github.com/upiscium/ObsidianAutomation/issues/302) Standard/Strict Nextcloud credential and transport choice (Gateway optional); [#303](https://github.com/upiscium/ObsidianAutomation/issues/303) Vault mapping; [#304](https://github.com/upiscium/ObsidianAutomation/issues/304) Dashboard; [#305](https://github.com/upiscium/ObsidianAutomation/issues/305) Snapshot/Publisher; [#306](https://github.com/upiscium/ObsidianAutomation/issues/306) migration.

**This ADR proposes, but does not authorize, software deployment, credentials migration, LXC removal, Nextcloud writes or public publishing.**
