# #303 V1/B1 P1 — disposable Nextcloud environment readiness (design only)

**Status (2026-11-08 JST): SOURCE-ONLY DESIGN; REVIEWED DRAFT PR PUBLICATION AUTHORIZED. NOT AUTHORIZED TO PROVISION.**
Pinned source: `upiscium/ObsidianAutomation main@192aaf050cd7510b785e3b4c29138efa4715b0cf` after the source-only merge of PR #566. The 41-case V1/B1 matrix is already merged. This design and its inert synthetic manifest/tests were reviewed locally as R3; the owner separately authorized **Commit/Push/Draft PR only** for the exact three-file source slice. **No Ready/Merge/Deploy/activation, instance provisioning, account/fixture seeding, provider or credential I/O, cleanup or operational action is authorized.** Keep #303/#301/#304/#298 OPEN.

## Decision sought

Determine **whether a disposable, account-scoped Nextcloud instance can produce the evidence needed for a fail-closed Vault-root preview**. Neither deploying a test instance nor a green synthetic test grants authority to use a production Vault or switch roots. A/I/X/L/C/R remain `UNKNOWN` and provider integration `BLOCKED/NYI`.

## Separate authority stages

| Stage | Exact scope | Current authorization |
| --- | --- | --- |
| P1-P0 | Design, synthetic fixture/readiness validation, source-only | **YES** |
| P1-A | Provision **one** isolated disposable target and install pinned test-only Nextcloud/database | **NO** |
| P1-B | Create disposable accounts, directories, share/mount/symlink fixtures | **NO — distinct setup/write permission** |
| P2 | Bounded authenticated read-only `PROPFIND Depth:0/1` against **approved disposable origin and paths only** | **NO — distinct provider-read permission** |
| P3 | Test-only concurrent mutation, account rotation, cross-instance comparison, proxy-fault injection | **NO — individually scoped follow-up permissions** |
| B2/C/D | Real adapter, UI, root-switch activation, Writer/Job integration, deploy | **NO — outside this workstream** |

P1-A does not imply P1-B, and neither permits P2 or P3. A subsequent human approval must specify the **target** and the exact operations allowed. No automatic discovery, creation, cleanup or extension of scope is authorized by a fixture manifest. No current step touches obsidian-control or the existing Nextcloud environment.

## Proposed physical topology — contingent on owner approval

**Preferred:** a throwaway unprivileged VM dedicated to one Nextcloud test deployment on an owner-selected Proxmox host. The physical host is **UNSELECTED**; no capacity/placement claim is made. A minimal unprivileged LXC may be used only if its isolation, UID map, nesting requirements and prohibitions on production host mounts receive a separate review. Do not presume a Docker-in-LXC configuration or use a privileged container.

The following are **planning allowances, not measured requirements**: start with 2 vCPU, 4 GiB RAM, and 32 GiB discardable storage, then adjust before approval from the pinned Nextcloud image/runtime requirements and host capacity. The test instance has its own disposable database, file storage, container volumes and account namespace; **no host bind mount, reused production DB, existing WebDAV credentials, or personal Vault files**.

- **Version pinning:** select and record an immutable Nextcloud server release/build and image digest or package hashes, DB/runtime version, application/add-on versions, storage backend and schema before provisioning. The current design references official Nextcloud 35 docs but **does not pin or install a release**.
- **No production connection:** one isolated virtual network; deny routes to all production Nextcloud, Vault, infrastructure management endpoints and host storage. No public ingress or reverse proxy. Use an owner-selected, explicitly allowlisted observer route only at P2 after approval. Block redirects and default external egress; dependencies/image retrieval at P1-A must be separately scoped to a bootstrap allowlist, then disabled.
- **Failure boundary:** a test setup may be reachable only by designated fixture operators. Do not mount host Docker sockets, service-account tokens, prod secrets or shared storage. No Gitea snapshots, scheduled jobs, Core Promotion, Writer/Timer or production hostname rebinding.
- **Identity boundary:** use entirely synthetic tenant labels (fixture admin / account evaluated / share sender / share receiver). Unique account identities and application passwords must be created **inside the future disposable instance only**, not in this repository or manifest.
- **Access policy:** never use production URL, tokens, user names, real notes or existing Nextcloud shares for comparison. Any second disposable instance requires its own scope approval.

## P1-A — provision only (NOT executable yet)

1. Owner selects host, target kind and isolated network. Prove placement capacity and route separation **before** allocating a VM/LXC.
2. Owner approves exact Nextcloud/server/database/app versions, digest, storage allocation, temporary bootstrap egress, operator identity and private retention interval.
3. Create a disposable target with a unique destruction tag and test-only storage. Boot and install with its own database. Keep provider network access unavailable to the ObsidianAutomation workers.
4. Record local-only, redacted inventory: server/build/digest, app list, database, external storage support, storage backend, instance identity *source*, TLS setup, operator authorization, P1-A completion receipts. This records configuration, not authenticated DAV proof.
5. Disable bootstrap egress; verify no production volume, credential mount, or route. Gate out if any isolation predicate fails.
6. **Stop.** Do not create accounts/fixtures under P1-A alone. P1-B requires separate setup authorization.

No P1-A receipt can change a provider-proof state from `UNKNOWN` to `VERIFIED`.

## P1-B — preseed test-only dataset (separate approval required)

Use separate administration and evaluation accounts to prevent self-certified evidence:
- `fixture_admin`: owner of the disposable fixture setup, no runtime preview trust.
- `viewer_a`: the single account whose authorized view is to be studied.
- `share_sender_b`: used only for synthetic received-share fixtures.
- Optional `viewer_c`: only if a distinct account/ACL case is approved. A **second Nextcloud instance** is not implied.

The fixture seeder builds and stores an **independent expected-set receipt** immediately after each authorized setup transaction. Each receipt lists *opaque case IDs*, logical parent/child relationships, account-visible entries, expected ACL-hidden items, and intended alias/mount class. These logical fixture labels are **not** provider `oc:id`, `oc:fileid`, ETags, or fabricated provenance tokens. Never derive the oracle by copying the very DAV response being evaluated.

Required positive/negative fixture families are defined by the companion `tests/fixtures/oa303_v1b1_p1_environment_catalog.json`. Critical distinctions:
- Local folders with two or more siblings and nested descendants for authorized-view enumeration (A/I/L).
- Received user share; separately a group-folder mount, external storage and external-session mount **when supported by pinned apps/storage** (X). Each unsupported provider feature remains `X=UNKNOWN`; no substitute `is_alias=False` is permitted.
- In-root/out-of-root backend symlink, cycle and unknown classification **only if safe, disposable backend support exists**. Never point symlink to a host or production path.
- Two differently normalized Unicode names, duplicate/alias identity, denied child, ACL-hidden sibling, negative props/207 body scenarios (L). Some negative responses are **offline XML injections only**; a normal fixture seeder cannot make a provider silently omit an otherwise visible child.
- Source revision/credential rotation and A→B→A races are **P3**, not part of P1-B setup, unless separately approved.

All expected siblings must be account-visible under the target's authenticated principal or explicitly accounted for as *visibility uncertainty*. The seeder must record fixture-generation and independent evidence origin, but those labels do not prove global completeness or multi-level atomicity.

## P2 — future bounded read-only observation (NOT authorized)

No P2 runner is implemented by this document. The later, separately approved observer must:
- accept an exact immutable allowlist of a single disposable HTTPS origin, authenticated account DAV base and fixture paths; never accept a caller-supplied endpoint or follow redirects, and never send credentials to another origin;
- send only explicit `PROPFIND Depth:0/1`; no GET-content, PUT, MKCOL, MOVE, DELETE, PROPPATCH, REPORT, Depth:infinity or test-writer calls;
- enforce response bytes on the transport **before** XML allocation (proposal: 1 MiB per response / 2 MiB per run; to be requalified), timeout and row/depth bounds, and deny malformed/partial status, missing self-row, unrecognized properties, unresolvable mount/share or identities;
- compare the independently preseeded authorized-view set against the authenticated response and retain **redacted private receipts**; matching finite sets are observations, not a universal completeness theorem;
- report gate A/I/X/L/R as UNKNOWN, OBSERVED_WITH_LIMITS or FAILED according to independently documented evidence. **C never becomes VERIFIED from matching ETags, repeated reads or a clean finite race experiment**.

The original R0 gate semantics remain authoritative; this readiness document does not weaken them. A provider unable to supply authoritative alias or cross-directory coherence remains `BLOCKED`.

## P3 — mutation/race feasibility (separate permission)

Only after an independently authorized writer may a disposable fixture exercise rename/move/delete/create, credential rotation, mount changes, concurrent forked reads or fault injection. Capture explicit A→B→A, delete/recreate-same-path, post-final-check write, cross-parent move and source-rebinding counterexamples. Never infer snapshot atomicity from a passing run. Any required invariant without a documented provider mechanism is `C=UNKNOWN`.

## P1-A / P1-B / P2 admission checklist (human review)

**P1-A admission:** owner-selected disposable host and resource budget; operator access; version/digest and storage plan; isolated network and production-deny route evidence; private retention and teardown plan; exact approved creation rights. If any field is missing, `P1_A_ADMISSION=BLOCKED`.

**P1-B admission:** P1-A has completed; explicitly approved account and fixture setup/writes; expected visible/hidden child sets and independent-seeding oracle procedure; no real notes, production auth, or production mounts; clear unsupported-feature labeling. If any field is missing, `P1_B_ADMISSION=BLOCKED`.

**P2 admission:** P1-B completed with independently retained expected-set receipts; exact approved origin/account/path/read credential and network route; approved `PROPFIND` body and byte/time budgets; response redaction and credential lifetime; independent read-only permission. If any field is missing, `P2_ADMISSION=BLOCKED`.

Never reuse a past owner's implicit approval from another project or test for any of these gates.

## Cleanup and evidence handling (design, NOT authorized execution)

At the end of an approved experiment, revoke test-only credentials and block the observation network; capture redacted acceptance/failure receipts; then destroy **only** the exact disposable target identified by the future owner-approved destruction manifest, with an explicit lease/OID-like exact identity check. **No blind cleanup** of generic LXC/VM IDs, Docker volumes or shared filesystem paths. Teardown is a separately authorized destructive action, not automatic. Preserve only private, redacted aggregate results and current `UNKNOWN/FAILED` gate states.

## Related technical references

- Official WebDAV properties and modern file endpoint: https://docs.nextcloud.com/server/stable/developer_manual/client_apis/WebDAV/basic.html
- Nextcloud administration installation overview: https://docs.nextcloud.com/server/stable/admin_manual/installation/source_installation.html
- RFC 4918 PROPFIND semantics: https://www.rfc-editor.org/rfc/rfc4918.html
- Merged R0/B1 contract: `docs/oa303-v1b1-disposable-nextcloud-identity-provenance-proof-plan.md`
- Merged 41-case plan: `tests/fixtures/oa303_v1b1_proof_matrix.json`

This is a design document, not a provisioning script, network permit, credential request, executable manifest or proof of Nextcloud provider authority.
