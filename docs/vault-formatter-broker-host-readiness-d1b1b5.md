# #472 D1/B1/B5 — Host principal/IPC readiness and #298 profile compatibility (design candidate)

**Status: LOCAL DESIGN ONLY / UNWIRED / NOT APPROVED FOR DEPLOYMENT.** Based on `ObsidianAutomation/main@46af364619166a45a9d7173b444ca4170ea2fc7b` (merged B4 PR #569). This is a proposed acceptance contract, not a final product-profile decision, installed Broker, an API, a Writer permit, or permission for privileged tests. No Commit/Push/PR/Ready/Merge, account/group/ACL install, unit changes, credential read, Owner/Broker process startup, Provider/Vault/Nextcloud I/O, Auto apply, deployment or activation.

## Why another gate is necessary

B4 provided **real kernel cross-UID/GID `SO_PEERCRED` acceptance** under eight fresh unprivileged user/mount/network/PID namespaces with disposable Broker/Owner child processes. It established correct *local connection discrimination* against a wrong UID and a wrong primary GID with supplementary IPC access, and proved test-only fail-closed cleanup. It **did not** verify production host users, systemd sandboxing, trusted executable provenance / forwarded-file-descriptor origin, Broker-only credential custody or Provider egress, actual Provider finality, Owner-held stable effect flock or Writer authorization.

The currently controlling [#298](https://github.com/upiscium/ObsidianAutomation/issues/298) product contract prioritizes **one nonroot service user / one Nextcloud account / one management UI** for standard fresh 1-LXC MVI. Separate system users and strict provider-account isolation are **not required** for standard MVI. The specific proposed **strong-isolation D1 effect mechanism** in [`docs/vault-formatter-effect-admission-d1.md`](vault-formatter-effect-admission-d1.md) assigns distinct Worker, Owner and Broker OS UIDs and requires a Broker-exclusive credential/Provider dispatch path. The overarching [#472](https://github.com/upiscium/ObsidianAutomation/issues/472) acceptance criteria **do not universally mandate three Unix identities**. These are two **different assurance approaches**; treating #298's one-UID installation as if it satisfied this proposed D1 Broker mechanism would be incorrect.

**Proposed reconciliation, pending owner/product decision:** Leave #298's standard fresh-install flow and unrelated Snapshot/Daily/GitHub services unchanged. Introduce an *optional, separately authorized strict effect-execution profile* only for the #472 D1/B1 Broker-based recurring write path. **Do not silently change Standard into Strict, require Strict for all #298 users, or silently downgrade Strict into Standard.** If Strict is selected but its required evidence is absent, block that #472 recurring effect path. This proposal does not define any new default UI toggle, migration, installer or activation authority.

## Read-only observation on obsidian-control — 2026-11-08 JST

The authorized read-only device connection returned:

- Linux LXC `obsidian-control`; `obsidian-snapshot.service` is **active/running**, `User=obsidian-snapshot`, `Group=obsidian-snapshot`, `DynamicUser=no`. The named account has UID **999**, primary GID **991**.
- `systemctl show` for that **Snapshot** unit reports `PrivateNetwork=no`, `NoNewPrivileges=no`, `ProtectSystem=no`, `ProtectHome=no`. Those attributes describe this installed Snapshot service, **not** a nonexistent Broker unit, and they do not prove exclusive Broker credentials/egress.
- Name-filtered `getent passwd/group` found no explicitly named Formatter Owner/Broker/Worker principal; systemd list showed no separately named Broker service. A bounded `/run` socket search and known candidate directory checks found no Broker socket/lock. These are **negative observations within a bounded search**, not a proof that no differently named identity, socket, credential or external service exists anywhere.
- `obsidian-automation` is **offline in Desktop Commander** at this checkpoint; no claim is made about its host configuration or historical services. Do not change the active Snapshot process, its unit drop-ins, existing credentials, file permissions or timer.

Consequently **production Strict readiness is NOT ESTABLISHED**. An active Snapshot service is not evidence of a working Owner/Broker IPC security boundary. We should not launch B3 listener or B4 namespace test as if that were the production Broker, nor assign real Provider credentials to it.

## Mandatory gates — do not conflate evidence types

| Gate | Required proof to progress | Current disposition |
| --- | --- | --- |
| G0 profile decision | Human selects Standard product MVI vs optional Strict recurring-effect profile; explicitly documents assurance difference, compatibility and fail-closed non-downgrade | **UNDECIDED** (proposal only) |
| G1 host ownership | Separately authorized **fresh/test** LXC: three distinct nonroot application UIDs under the proposed Strict D1 model, actual process UID/primary GID, supplementary-group/ACL exposure, relevant privilege/capability boundaries, no inherited privileged handles; verify by authoritative evidence, not name regex. Trusted host root is not part of this nonroot isolation claim | **NOT ESTABLISHED** |
| G2 Broker listener | Production trusted ancestry (B3's sticky `/tmp` exception is disposable-test-only); Broker-owned 0710 IPC directory, 0600 single-link lock, initially absent 0660 group socket; reject stale path, symlink/hardlink/inode changes, failed cleanup; direct real Owner AF_UNIX `SO_PEERCRED` positive and **wrong-UID/wrong-primary-GID rejection after permitted connect with zero response bytes**, bounded accept/timeout | **B4 disposable userns PASS; production NOT ESTABLISHED** |
| G3 execution identity | Trusted operator-controlled executable path/inode/digest and launched service PID/UID/GID; FD ingress/forwarding policy, privilege drop, restart and handoff identity; `SO_PEERCRED` is a connected-peer check **not code provenance** | **NOT ESTABLISHED** |
| G4 credential and egress | Broker-only secret custody and active egress restrictions **enforced** against Worker/Owner; no alternate Worker/Owner credential access, arbitrary Provider PUT or unmediated Writer. Distinguish network-isolation policy from merely having a distinct UID | **NOT ESTABLISHED** |
| G5 effect-time transaction | Exact signed Owner Claim/immutable Plan/Provider account/target and credential generation epoch; Owner-wide durable admission gate and exclusive stable effect flock across dispatch + broker finality + Owner settlement | **NOT IMPLEMENTED/NOT ESTABLISHED** |
| G6 Provider finality | Broker-exclusive durable CAS/PUT/idempotency, provider-specific operation proof/readback and unknown-effect quarantine/restart/power-loss recovery; local `cancel_never_dispatched` receipt is **not** remote Provider no-effect proof | **NOT IMPLEMENTED/NOT ESTABLISHED** |
| G7 canary/activation | Separate Human approved canary on `98-System/**`, receipts, rollback and Snapshot/Gitea/Core eventual convergence; never auto-activate during source merge | **NOT AUTHORIZED** |

Passing G1–G3 is **necessary but insufficient** for G4–G7. A simple `uid != broker_uid` check, Unix socket mode, `systemctl active`, B4 test pass, source CI Green or a Python dataclass cannot issue a Writer capability, authorize Owner Journal settlement, or imply Provider outcome. This host-readiness contract also does **not** replace the separate #472 Off/Dry-run, idempotence, Formatter-only controls, error/status distinctions or publication-convergence acceptance criteria.

## B5 read-only next-step procedure (no OS mutation)

1. Preserve the exact host/read-only inventory and public policy references without collecting raw credentials, Vault paths/body, provider URLs or environment files. Treat absent/not observed separately from verified mismatch.
2. Align #298 and #472 with an explicit proposed **profile/assurance decision**, rather than making three service users mandatory across all Standard installs.
3. Propose source-only templates for a **disposable fresh LXC**: three nonroot identities only for the candidate Strict D1 model, IPC directory/group/lock/socket policy, executable provenance and restart rules. Independently review before requesting a separate approval for **any** privileged provisioning. Existing `obsidian-control` is not scratch disposable.
4. **Passive inventory gate:** separately authorize access to a designated fresh/test host and inspect principal/service metadata, group/ACL, executable identity and socket/lock state without creating IPC connections or changing filesystem, permissions, services or credentials.
5. **Active read-only IPC gate:** separately authorize the exact test-host listener/client lifecycle and bounded no-Provider `credential.observe` / `operation.finality` synthetic-only exchanges, real UID/GID and wrong UID/primary-GID denials after permitted socket connect. This is not authorized by passive inventory or provisioning approval alone.
6. **Lifecycle/fault gate:** separately authorize process startup/stop/restart, stale-socket and cleanup-failure injections under bounded disposable conditions. `SO_PEERCRED` does not authenticate executable provenance or a forwarded descriptor's original sender.
7. Defer credential custody/egress, Owner/Provider finality and any Writer/Vault effect (G4–G7) to distinct source, review and Human gates. If evidence remains absent, return **NOT_READY / UNVERIFIED**, not a transferable PASS/permit or fallback to writing as Standard.

## Acceptance for this design-only slice

- [ ] Explicitly preserve #298 Standard MVI's one-service-user goal without falsely claiming strict D1 Broker security.
- [ ] Keep Strict optional/proposed and impossible to activate implicitly; record that selecting it requires separate Human authorization.
- [ ] Disentangle userns B4 success, host principal/IPC acceptance, service/executable provenance, credential/egress containment, Owner/Provider finality and live writes.
- [ ] Host read-only observations disclose neither private Vault content nor raw credentials; bounded absence is not global proof.
- [ ] No runtime module, installer, systemd unit, config, API, or UI is changed in this design slice. No commit/publication unless separately approved.
- [ ] Independently review this document for conflicting product promises, missing safety boundaries, false claims and an accidentally implied deployment authority.

**Next distinct authorization after reviewed design:** decide whether to adopt the optional Strict profile and approve a concrete disposable-LXC provisioning/identity/IPC acceptance proposal; do not touch production services from a general `Next` continuation.
