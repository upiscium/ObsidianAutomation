# #303 V1/B1 R1 — synthetic finite-oracle and coherence non-proof

Status: **synthetic-only source prototype, with Draft PR publication
separately authorized**. Built atop immutable main
`bc61c8803cfc92b3ad51fc2ad0b877a8e17b2cf4`, after owner-authorized
V1/B0 PR #563 merge. R1's earlier independent review was local-only.
Source-only Commit/Push/Draft PR was subsequently approved; no
Ready/Merge/Deploy, production activation, test-instance provisioning,
provider traffic, credentials, Vault access or Job effects are authorized.

## Inputs, outputs and what they do NOT prove

`src/obsidian_automation/vault_root_proof_oracle.py` is deliberately pure
Python without network, storage, process or authentication integration.
The synthetic `SyntheticAccountView` dataclasses are **fabricatable
caller assertions**, never trusted browser or authenticated provider
receipts. This is a separate bounded test exercise, not a new Dashboard
endpoint and not an admissible replacement for the V1/A root validator.

- `compare_synthetic_account_view(expected, observed)` tests exact
  unordered (member name, resource ID) equality against a *fabricated*
  oracle representing the account-visible immediate-child universe.
  Before comparison it checks exact runtime types, bounded component
  labels, unique IDs including the parent, and both V1/A canonical/
  legacy Unicode collision rules (including unselected siblings).
- A row missing from the observed inventory produces
  `blocked/fixture_inventory_mismatch`; additional or reassigned rows
  also fail closed. Unknown ACL-hidden children block a supposed
  complete view even when all visible children match.
- `finite_fixture_match/provider_unverified` is a narrow result.
  **It never returns** `provider_verified=True`,
  `activation_allowed=True` or `writes_vault=True`.
  When the supposed oracle itself omits something and claims no hidden
  children, matching that false oracle cannot reveal the omission.
- `assess_synthetic_coherence(frames)` refuses known source revision
  changes, known tree changes and independently logged mutation events.
  A-B-A restoration and mutation after a last read are tested as
  synthetic schedules. When no change is **observed**, the result is
  always `unknown/provider_atomicity_unproved`; absence of a known
  event is not proof of absence of an unseen one.
- **Results returned by the two evaluator functions** use fixed redacted
  status/reason strings and non-authorizing flags, without reflecting paths,
  resource IDs, usernames, tokens or fixture contents. The publicly
  constructible `SyntheticProofResult` dataclass is **not** an attestation:
  any caller could manufacture arbitrary fields/flags. Dataclass freezing
  does not enforce authenticity, and no downstream code may accept a
  direct caller-constructed result as proof or activation authority.

## What the R1 offline tests establish

`tests/test_303_v1b1_synthetic_proof_oracle.py` covers:
1. Exact finite visible-set comparison, unordered siblings.
2. Missing/extra/rebound member; scope and source revision mismatch.
3. Hidden/ACL-uncertain and incomplete claims; a deceptively matching
   fixture with unknown physical siblings never upgrades to provider proof.
4. Parent ID reuse, duplicate sibling IDs/names, Unicode canonical aliases,
   unsafe names, overlong labels and an oversized inventory.
5. Hostile `str` subclasses and malformed dataclass shape or boolean type.
6. Known A-B-A restoration and known post-final-read mutation both block.
   Unobserved A-B-A and matching repeated observations remain **UNKNOWN**.
7. Fixed redacted results and filesystem/network/process I/O tripwires.

R0 V1/B1 plan + fixture matrix + schema tests are copied byte-for-byte
into an isolated R1 worktree, rather than modifying the R0 handoff.
R0's 41 inert planning scenarios and hard authority gates are unchanged.

## Provider feasibility remains blocked

A test-only, independently preseeded expected set can serve as a finite
oracle for a **disposable, explicitly approved** Nextcloud instance.
It cannot establish a universal completeness guarantee; this is
particularly important for ACL-hidden or multiply mounted children.
The provider itself must demonstrate the appropriate account-visible
inventory semantics and authoritative identity/alias evidence for every
required sibling. No fabricated `complete_claim` can provide this.

For a later V1/B1 real provider decision:
- **A:** authenticated account/instance/modern DAV base binding
- **I:** stable globally scoped canonical identities; share/mount equivalence
- **X:** authoritative symlink/alias/mount classification across all siblings
- **L:** independently evidenced complete account-visible inventory
- **C:** documented coherent cross-directory snapshot or equivalent
  server-side transaction/fence protected through the future CAS boundary
- **R:** source revision binding and invalidation after rotation/rebinding

All A/I/X/L/C/R are **UNKNOWN**, not merely pending tests.
Matching two remote `PROPFIND` responses or directory ETags cannot
prove C. Local Job quiescence cannot fence other Nextcloud clients.
A future policy weaker than all-level snapshot consistency requires its
own owner-approved design/security analysis, not silent fallback.
Until each required actual-provider proof is established, activation,
root switching and production Nextcloud integration stay **BLOCKED/NYI**.

## Separate future owner decisions, not implied by this source

1. Approve a disposable Nextcloud instance and test-only account/data
   provisioning. No production account or personal Vault.
2. Approve service-private credentials and bounded authenticated read-only
   `PROPFIND` against a fixed test-only origin and paths. A README/matrix
   does not authorize network calls.
3. Approve test-only fixture mutations, shares, mounts, race injection,
   second-instance comparison and any writer separately. The read-only
   probe approval is not permission to create or modify any resource.
4. V1/B2 real source adapter and V1/C GUI and V1/D guarded activation
   remain independent authorizations.

Keep #303, #301, #304 and parent #298 OPEN and current production unchanged.
