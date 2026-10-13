# Daily v2 route capture index — Stage A (#336)

**State:** opt-in in-memory validation primitive only; no persistence, no
independent source authority, no Writer/Timer/model effects.

**Dependency:** exact source-route verification contract from Draft
[#329](https://github.com/upiscium/ObsidianAutomation/pull/329) and its
zero-read transport correction in child Draft
[#335](https://github.com/upiscium/ObsidianAutomation/pull/335).
It does **not** bypass source-type ambiguity or historical v1 Evidence rules.

## Purpose

An authenticated, fresh Collector operation returns two distinct immutable
*byte candidates*: a GitHub Daily Evidence bundle (v1 schema) and a matching
GitHub source-route receipt (v2 schema). Later persistence and downstream
readers must bind **these exact bytes** and the original capture mode/time,
rather than independently looking up a route, guessing PR membership from
a public URL, or silently substituting old Evidence.

The nonpublishing `CaptureIndex` names the two blobs using full SHA-256 and
provides a canonical manifest-shaped third payload. There is no filesystem
transaction at this stage.

## Exact envelope

Canonical UTF-8 JSON with sorted keys, compact separators, trailing newline
and at most 4 KiB. The exact required fields are:

- `record_version: 1`, `source_receipt_version: 2`
- `date` (JST day), `captured_at_utc`, `collection_mode`, `route_count`
- `evidence_sha256`, `route_receipt_sha256`
- `producer_attestation_verified: false`
- `publication_authorized: false`

`build_capture_index(FreshRouteCollection)` checks that collection hashes,
route count and target date match. It *reruns*
`parse_verified_routes(receipt_bytes,evidence_bytes)` and then computes an
index that identifies the exact pair. `parse_capture_index(index_bytes,
evidence_bytes,receipt_bytes)` independently reconstructs the canonical
payload from the exact source pair and requires byte-for-byte equality.
Unknown/extra fields, false authority, noncanonical JSON, missing source,
incorrect route, wrong count/date/mode, tampering and oversized bytes fail
closed. Index bytes are **not** an authorization credential and should not
be accepted from arbitrary model output as a trusted capture.

Even a perfectly consistent SHA triple may be forged by a caller that
controls *all three blobs*. Hashes prove content binding, not the origin of
a GitHub API response or the identity/privileges of its producer.
The flags therefore remain permanently `false` in this Stage A format.

## Next: trusted Collector CAS Stage B

Before any consumer may trust the index as collector-originated, require all
of the following in an independently reviewed, nonwired integration:

1. Integrate the *merged main* atomic CAS publication helper from
   [#293](https://github.com/upiscium/ObsidianAutomation/pull/293).
   #335 was based on older main and does not itself contain that helper.
   Do not use its earlier direct-final-`O_EXCL` writer or duplicate the CAS
   implementation.
2. A dedicated, admin-provisioned Collector UID and root-owned directory
   ancestry with audited POSIX ACL: producer is the only unprivileged writer,
   summarizer/renderer are read-only. Same-UID process compromise, root and
   uncontrolled ancestor renames remain outside the claims of SHA integrity.
3. Atomically publish Evidence and source receipt as hash-named complete
   objects, then the capture index *last* as the transaction commit marker.
   Crash before the final index leaves uncommitted blobs, not an admissible
   complete capture. Crash after publication requires directory-fsync and
   exact-byte idempotent resume.
4. Reader accepts only fixed trusted CAS paths constructed from validated
   digests, checks owners/ACLs/type/links/size/hash, reparses the exact
   triplet, and returns a typed nonpublishing verified route source to the
   isolated #325/#327 entity consumers. Arbitrary on-disk paths and model
   text are never used as authoritative locators.
5. A failed source API call, changed/rejected route, or 409 empty-repository
   ambiguity is **PENDING/BLOCKED**, not a successful empty Daily. Track
   bounded 409 compatibility in [#337](https://github.com/upiscium/ObsidianAutomation/issues/337).

Do not change historical v1 CAS refs, deploy, merge to main, or authorize
Nextcloud Writer/Timer effects based on this Stage A alone. The final release
still needs independent Security/Correctness review, exact-head CI, controlled
real-date private quality review, filesystem role/ACL acceptance, and
separately approved production rollout.
