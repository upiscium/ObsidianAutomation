# #303 V1/B1 P1-A — disposable Nextcloud stack, placement and isolation admission

**Status:** Design-only, source-only candidate; **reviewed Commit/Push/Draft PR publication separately authorized**. This is not a provisioning instruction.
**Source baseline:** `upiscium/ObsidianAutomation main@5932a0982d86e12407f9c18e31b6226391818224` (PR #568 merged).
**Date of public-source verification:** 2026-11-08 JST. Do not treat this date as an installation receipt.
**Permissions:** Only the reviewed three-file source-only Commit/Push/Draft PR is authorized by the later owner approval. **No Ready/Merge, VM/LXC creation, production-host writes, provider/credential access, test-account creation, teardown, deployment, activation or schedule changes.** Keep #303/#301/#304/#298 OPEN.

## Proposed software stack — never silently promote a candidate into an approval

The public Nextcloud project states the latest stable version on 2026-11-08 is **35.0.2** (released 2026-10-15). Version **35.0.3 is a November 12 future maintenance release** at the time of this design, so using the RC is unnecessary for the initial reproducible test. Links: https://github.com/nextcloud/server/wiki/Maintenance-and-Release-Schedule and https://nextcloud.com/install/.

Candidate for an isolated disposable VM, pending owner approval:

| Component | **Candidate only** | Admission requirements |
| --- | --- | --- |
| Nextcloud Server | **35.0.2** source tar archive | Exact immutable archive name, SHA-256 **UNVERIFIED**, official signature verification and checksum chain, downloaded only after explicitly approved scoped bootstrap |
| Guest OS | **Ubuntu Server 24.04 LTS**, x86-64 | Exact ISO URL + digest **UNVERIFIED**, release/security maintenance and package repository pins |
| PHP | **8.3**, FPM | Exact patch/package hashes **UNVERIFIED**, required extensions and web server integration verified against pinned packages |
| Database | **PostgreSQL 16** | Exact package versions and hashes **UNVERIFIED**, local test-only database, private DB role, no production DB reuse |
| Web server | **Apache 2.4 + PHP-FPM** | Exact packages and TLS origin **UNSELECTED**; do not expose HTTP(S) until separately approved observer access |
| Optional Nextcloud apps | **NONE preselected** | Files sharing/group folders/external storage compatibility and individual app version/digest require P1-B feature-specific approval |
| Resources | **2 vCPU, 4 GiB RAM, 32 GiB discardable disk** | Planning budget only; actual host capacity and headroom **NOT OBSERVED**; separate disk snapshot/backup restrictions |

Nextcloud 35 supports Ubuntu 24.04, PHP 8.3 and PostgreSQL 16 according to the official administration manual: https://docs.nextcloud.com/server/stable/admin_manual/installation/system_requirements.html. The official source install guide describes Ubuntu 24.04 tar installations: https://docs.nextcloud.com/server/stable/admin_manual/installation/source_installation.html. Compatibility is **documentation-level**, not a running fixture receipt. No current VM/package/digest or host capacity was inspected. Do not use floating `latest` images or mutable tags to certify reproducibility.

**Pinning order:** first approve installation *method*, then verify exact archive signature/checksum and guest ISO, then pin package set, then approve the single disposable host/network and bootstrap egress. Read-only publication of a candidate list does not authorize fetching, installing, updating, or provisioning any component.

## Placement and network: deny by default

One throwaway **VM**, with ordinary guest accounts and no host-agent trust elevation, is preferred to Docker-in-LXC or a privileged LXC. The VM is a *candidate architecture*, not selected hardware. **The owner must first nominate an exact candidate Proxmox host and the permitted read-only inspection targets.** The owner then separately approves a bounded inspection of **that named host and its specified network/storage settings**; no cluster-wide discovery, scanning, implicit node enumeration, reservation or allocation. Inspection receipts may support final placement selection, and only then can separate P1-A provisioning authorization be considered. No attempt has been made here to inspect, name or reserve a host, IP, VMID, VLAN/SDN, subnet, gateway, DNS resolver or production bridge.

**Before P1-A approval, establish independent receipts:**
- **Inspection scope and host:** owner-nominated exact candidate node, approved bounded read-only target/fields, observed CPU/RAM/disk accounting and cluster headroom, and later explicitly selected VMID and resource identity. **Do not reserve a VMID or enumerate a cluster until separately authorized.** Never infer spare capacity from nominal totals.
- **Isolation:** private test-only guest network; no production LAN/Vault/Nextcloud/Obsidian, hypervisor administration, storage control or management services reachable from the guest. Explicit route/ACL/egress denial evidence must cover routed and same-L2 paths, IPv4 and IPv6, DNS, metadata access and east-west VM traffic. A Proxmox bridge alone does not prove isolation.
- **Guest and storage:** discardable encrypted/private guest volume if needed, private database and local files, no host bind mounts, host sockets, GPU passthrough, production credentials, real Vault exports, shared volumes, external backup/sync or local production secrets. **Verify the hypervisor does not automatically enroll the guest in production backup, snapshot, replication, guest-agent secret transfer or shared storage policies.** Any test-only backup/rollback arrangement needs its own exact owner approval.
- **Bootstrap:** offline/approved release media is preferred; any temporary external retrieval or package repository network window must be independently scoped to exact destinations with bounded duration, hash verification and explicit owner permission. The scope must include **bootstrap DNS resolver, redirect behavior and mirror/CDN endpoints**, with cross-origin redirects blocked unless specifically allowed. Disable temporary routes after staging, and require deny policy to persist across guest reboots. No automatic updates, telemetry, unapproved redirects or unfenced external egress.
- **Observation:** P2-only route/HTTPS origin must remain denied through P1-A; P2 is a separately authorized service-private credential-backed read-only `PROPFIND Depth:0/1` experiment. Package installations can auto-start Apache, PHP or PostgreSQL listeners: verify they bind only to explicitly approved local interfaces, or keep services stopped, until network isolation has been independently tested. Never open a public ingress or reverse proxy by default.
- **Cleanup:** record lease/identity/tag for exact future teardown. Destruction, old volume deletion and cleanup remain *independent destructive permissions*, never implied by P1-A.

## Critical setup boundary — initial admin creation belongs to P1-B, not P1-A

The previous P1 plan described P1-A as installing Nextcloud while P1-B alone permitted account creation. A typical `occ maintenance:install` flow creates the initial admin and a configured database/data-directory. **Do not smuggle that account creation into P1-A.**

**P1-A** may prepare an owner-authorized isolated guest, install/pin PHP, webserver and database packages, stage the verified Nextcloud source archive and configure an *uninitialized* private database location. It must stop **before** any Nextcloud application initialization or installation action that creates an admin account, users, shares or content. Mere package staging does not yield a working Nextcloud provider and cannot satisfy A/I/X/L/C/R.

**P1-B** explicitly includes a separately approved one-time Nextcloud initialization and disposable bootstrap admin, plus the named test-only viewer/share accounts and fixture seeding. If a future chosen distribution/install method automatically creates an initial account during P1-A, it **fails P1-A admission** unless the user gives a distinct P1-B authorization; do not weaken the boundary.

No P1-A step may issue authenticated WebDAV requests, browse Nextcloud file contents or confirm provider identity using real credentials; P2 must be separately approved.

## Explicit admission outputs and missing information

All available results are **candidate design / UNKNOWN / BLOCKED**. The companion inert JSON must keep `allowed_actions_now=[]`, every `authority` false, all `admission` values `BLOCKED`, and A/I/X/L/C/R `UNKNOWN`.

A future human P1-A admission can be considered only when there is evidence for all of: selected host and VMID; headroom; exact isolated network/ACL/route proof; version/ISO/tar/package digests and signature; installation approach that stops before admin creation; resource sizes; test-only private storage; bootstrap operator/egress scope and lease; exact creation permission and separate cleanup decision.

If any detail is unknown, **`P1_A_ADMISSION=BLOCKED`**. Neither a recorded version candidate nor a green synthetic test alters this. After an independently authorized P1-A provision, stop at the P1-B gate. P2 read-only authenticated proof and P3 concurrent mutation never inherit permission from P1-A/P1-B.

## Material uncertainties that cannot be resolved by source-only tests

- Whether any particular Proxmox host has capacity or whether cluster networking can deny all production and management paths.
- Whether the chosen Nextcloud release and app versions actually expose authoritative identity/alias/symlink semantics for the required 41 scenarios. App compatibility and backend capabilities are untested.
- Whether Nextcloud provides an atomic multi-level filesystem snapshot or a producer fence that remains valid through preview-to-save. Matching ETags, identical reads, a finite oracle, or successful race tests **cannot** prove C.
- Whether unsupported app storage behaviors require reducing the intended root-selection feature scope, postponing V1 integration or changing the research question. That is a later design decision, not an automatic fallback.

**Next appropriate action after owner accepts this design:** the owner **nominates one exact candidate Proxmox host and a bounded list of fields/paths to inspect**. Seek separate approval for read-only inspection **of only that nominated target**, without listing or probing unapproved cluster nodes, creating a VM or reserving resources. Then use the resulting receipts to make a final placement decision. P1-A provisioning approval must follow only after those receipts and version pins are reviewed. Do not authorize P1-A based solely on this document.
