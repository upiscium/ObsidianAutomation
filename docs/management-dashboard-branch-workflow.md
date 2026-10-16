# Management Dashboard integration branch policy

- **Status:** Active development / not released
- **Integration branch:** `feature/298-management-dashboard`
- **Tracker:** [Epic #298](https://github.com/upiscium/ObsidianAutomation/issues/298)
- **Existing source PRs:** [ADR PR #307](https://github.com/upiscium/ObsidianAutomation/pull/307), [read-only Dashboard PR #310](https://github.com/upiscium/ObsidianAutomation/pull/310).
- **Decision:** The Dashboard and its supporting one-host configuration, credentials, Vault mappings, Snapshot/Publisher migration, and control functions are developed and validated together in one long-lived **feature branch**, then merged into `main` only after final acceptance. Opening the final PR early as a Draft is permitted; merging into `main` is not.

## Branch workflow

1. Treat `feature/298-management-dashboard` as the **only integration target** for #298 work, including #299 (ADR), #300 (Status/Control), #301 (Connections), #302 (Nextcloud Standard/Strict), #303 (Vault folder bindings), #304 (Web Dashboard), #305 (Snapshot/Publisher), #306 (single LXC migration), and #308 (integration feasibility).
2. Implement each substantive change in a short-lived feature branch **forked from this integration branch**, and open a reviewable PR whose **base is `feature/298-management-dashboard`**, not `main`. Keep intermediate changes traceable by issue and test receipts.
3. Merge accepted intermediate PRs **into the integration branch** only after relevant CI, correctness and security gates. It is permissible to leave unaccepted changes in Draft PRs; never treat a code commit or Green CI as human approval for live effects.
4. Keep the integration branch current with reviewed, relevant `main` changes using safe merge commits or carefully reviewed backports. Avoid unreviewed force pushes or rewrites of evidence and do not overwrite concurrent work.
5. Preserve `main` as the production/release line. **No #298 partial PR is merged directly into `main`.** In particular, do not independently merge the source documentation PR #307 or MVP PR #310.
6. Keep the integration-to-main PR in **Draft** while anything is incomplete or any blocker remains; do not enable auto-merge.

## Final merge acceptance

The final transition out of Draft and merge into `main` requires:

- A reviewed, accepted ADR on credential-mode guarantees, loginless private-network ingress and the one-LXC design;
- README and runtime docs explicitly disclose the **single active service-execution LXC per Vault** support boundary and that concurrent multi-instance operations on the same Vault are **unsupported and not guaranteed**; cross-host distributed fencing is out of #300 MVP scope, but within-LXC update/worker/control locks remain mandatory;
- All requested operator workflows present: service status and controlled pause/resume, Nextcloud/AI/Gitea/GitHub Connections, Vault selection, Snapshot and local publication, one-host install/recovery;
- Representative end-to-end tests including one-Nextcloud-account Standard, remotely isolated Strict, CAS/ETag/no unintended deletion, secret confidentiality, shared lock and no duplicate canonical writer;
- Independently qualified Security and Correctness reviews on the **final** integration SHA, CI Green, and explicit user merge approval;
- A separate human-approved **production cutover**, which is not implied even by an accepted code merge. No existing LXC/account or private Vault is automatically migrated or destroyed.

## Provenance of the initial branch

The integration branch initially combines the **exact existing** code/UI/tests from PR #310 and the **exact existing** ADR and #308 experimental notes from PR #307. These are retained as historical source PRs; closing their duplicate main-targeted PRs is administrative, not a code deletion or an acceptance decision.

**Current implementation is experimental and observation-only.** The dashboard prototype is bound to loopback and has no mutating route. Do not represent it as a complete operational control plane.
