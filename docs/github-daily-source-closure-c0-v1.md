# Daily v2: verified-source Closure C0 (Issue #342)

**Status:** Experimental, strictly read-only, nonpublishing. Stacked on
[#341](https://github.com/upiscium/ObsidianAutomation/pull/341) (Collector-owned
atomic source CAS). C0 does **not** change #325/#327 context admission,
#331/#333 Grounding/rollup, Daily v1 Collector, Nextcloud Writer, Vault,
model calls, timers or historical immutable v1 snapshots.

## Purpose and authority boundary

The existing v2 entity Context (#327) correctly quarantines both Issue/PR
conversation comments on `github.com/{owner}/{repo}/issues/N`, since GitHub
uses that path for both and the inherited v1 `known_pr_numbers` cache can
misclassify a PR comment as an Issue. It also quarantines PR commits with only
public `/commit/SHA` URLs. Neither public path proves the actual Issue/PR
route.

Collector `collect_fresh_verified_routes` (#329) independently checks exact
GitHub API comment IDs, the linked Issue or PR record, and bounded PR-commit
membership, issuing an Evidence-v1 bundle with new canonical event IDs and a
matching Route Receipt-v2. #336's Stage A Capture Index binds the exact source
bytes; Stage B #341 stores those under the dedicated Collector UID with
independent OS-principal/ACL/inode/hash validation and manifest-last CAS.

C0 provides only:

```text
trusted scheduler/capture digest (64 lowercase SHA-256 characters)
    |
    v
read_verified_source_closure(capture_sha256)
    |
    | invokes ONLY github_daily_route_cas.read_verified_capture(digest)
    |   service UID, pinned directory/ACL, regular single-link, SHA,
    |   bounded Evidence+Receipt+Index, complete manifest check
    | independently reparses source.parse_verified_routes(receipt,evidence)
    | and capture_index.parse_capture_index(index,evidence,receipt)
    v
VerifiedSourceClosure v1 (in memory, private, not a capability)
    exact Capture/Receipt/Evidence SHA and time/mode/date
    route binding for every independently witnessed comment/PR-commit event
    record count + unattributed and non-route event counts
    publication_authorized: false
    requires_human_quality_review: true
```

No path or arbitrary Evidence/Receipt/JSON or `VerifiedCapture` object is a
public input to the C0 function. A caller-supplied digest names a local object;
it does **not** by itself establish trusted scheduler authority, which is
a later separate checkpoint requirement. C0 calls the real CAS reader for the
digest on every invocation. Even the CAS reader's own `routes` tuple consists
of mutable Python objects, so C0 rebuilds source-route bindings from the exact
readback bytes rather than trusting that tuple or a caller-supplied Boolean.

## Exact output contract

Canonical, sorted-key UTF-8 JSON with a trailing newline, at most 2 MiB.
A deterministic `sha256` identifies this **derived private read model** but
does not certify producer identity. Output contains exactly:

- `record_version = 1`
- `capture_sha256`, `evidence_sha256`, `route_receipt_sha256` (full 64-byte
  lowercase hex)
- JST `date`, `captured_at_utc`, `collection_mode` (scheduled recent API
  recheck or explicitly labelled historical reconfirmation)
- `evidence_event_count`, `witnessed_source_count`,
  `unattributed_default_branch_commit_count`,
  `other_unwitnessed_event_count` (summing exactly to event count)
- `routes`, sorted by original Evidence ID, with one exact record for each of:
  `issue_comment`, `pull_request_comment`,
  `pull_request_review_comment`, `pull_request_commit`
- `publication_authorized = false` and
  `requires_human_quality_review = true`

Each witnessed record includes the exact original `evidence_id`, event
`source_kind`, `repository`, verified `entity_type` (Issue or PR),
positive `number`, `source_id`, `occurred_at`, fixed authenticated
`verified_api_path`, `entity_api_path` and `remote_id`. No source body,
private comment text, raw model output, GitHub authentication secret,
untrusted filesystem path, or publish/deploy assertion is included.

**Scope of attestation:** Only the four route-sensitive kinds have independent
fresh API route witnesses. Remaining structural GitHub lifecycle events are
preserved in the original Evidence and counted; their presence in the bundle
does **not** make them individually API-reverified route events.
Default-branch commits remain unattributed. The model may never infer a PR
number from a commit URL. A missing witness or malformed CAS result fails
closed with a typed `DailySourceClosureError`, not an empty-progress report
and not a silently truncated route list.

The C0 `VerifiedSourceClosure` dataclass is ordinary Python and is **not an
unforgeable attestation capability**. C1 and later consumers must reopen the
trusted CAS by a separately admitted digest, validate all three exact bytes and
bind that digest into their *versioned* entity-context identities. Simply
accepting a constructed `VerifiedSourceClosure` instance, digest field,
boolean, model claim or archived receipt would recreate the trust bypass.
C0 does not change #327's current conservative quarantine rules.

## Testing and next gates

The no-network, synthetic local fixtures run the real #341 CAS storage and
read path on a POSIX ACL-enabled scratch directory. Cases include the PR
discussion mislabelled as an Issue by the v1 collector; API-backed Issue and
PR comment distinction; exact PR-commit membership and review comments;
deterministic complete source count; deliberately altered in-memory reader
route tuples (ignored and rebuilt); wrong capture hash/role/Receipt bytes;
missing CAS source file; no write, GitHub API or model action; historical
reconfirmation without promotion; and bounds/type/in-memory forgery checks.

**Not established by synthetic tests:** production Collector/summarizer UID
provisioning, ancestor ACL stability, same-UID/root compromise prevention,
credential safety under a real live API session, or current Writer eligibility.
#337 HTTP409 empty-repository compatibility also remains separately blocked.

C1 needs a new, explicit, jointly reviewed source-bound Context schema rather
than silently amending an old Context SHA. That schema must integrate #325's
canonical entity resolution and #327's partition/normalization/grounding
reconstruction so an unverified v1 event can never inherit authority from
an unrelated Capture Index. Full v1 Evidence must remain lossless; unsupported
model text never becomes a verified merge, deployment or progress claim.

C0 is a stepping stone, not end-to-end Daily v2. Separate human review for
semantic omissions (#290), Japanese entity editorial quality (#283), completed
v2 Summary/Projection contracts and **explicit** Writer/Timer rollout consent
are still mandatory. No merge-to-main or deployment is authorized here.
