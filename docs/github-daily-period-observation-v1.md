# #357 — Day-scoped mutable-source observations (opt-in, nonproduction)

## Failure and scope

An API resource fetched today is not its historical body at `created_at`.
The bounded real-source diagnostic for 2026-10-14 JST found an in-day PR
creation carrying text last updated on 2026-10-15 JST. Route authentication,
SHA binding and model Grounding do not fix that temporal mismatch.

The existing `collect_fresh_verified_routes` now accepts an **explicit**
`period_policy="github-day-snapshot-v1"`. The legacy default `None` preserves
v1 collection behavior and receipt v2 byte contracts; it is NOT retroactively
period-verified. No deployment default or historical immutable artifact is
changed here. Rollout of the new policy must be separately explicit.

This is a projection inside the existing collector, not a model step or a
new aggregation layer. It adds no GitHub requests, model calls, Writer/Timer,
CLI, role change, hidden retry, filesystem write or production publication.

## Event time, observation time, and content scope

The old collector still reads the bounded original source. A forwarding wrapper
records canonical API `created_at`, `updated_at`, `submitted_at` (if supplied)
and local `observed_at_utc`, without changing transport, pagination, endpoints,
credentials or selected repositories. Comment rechecks must preserve those
resource timestamps as well as the existing exact bounded body/identity check.
Changing `updated_at` with an unchanged body is not a stable temporal recheck.

For each event, the new policy creates a new hash-bound event with nontext
`temporal_observation` metadata. The event timestamp/kind/identity survives:

* Issue/PR created or snapshot, discussion comment, review comment, or review:
  mutable title/body/message/state/draft are eligible **only when** the source
  modification time is known and within `[JST-day-start, JST-day-end)`. Their
  scope is explicitly `day_snapshot_not_event_instant`, not a historical field
  version at the event's exact creation/submission instant. A modification
  outside that day or an absent modification time withholds these fields
  (`null` in model Evidence) while retaining the actual dated event.
* Timeline actions retain the dated action but do not copy the current embedded
  Issue/PR title/state. Downstream derives any transition from the event kind,
  not an unrelated current snapshot.
* Commit message/SHA/date are preserved with the scope
  `content_addressed_commit_not_push_time`: this does **not** prove when the
  commit arrived on a branch or that its recorded clock is trustworthy.

Malformed, impossible, future-relative-to-observation or backwards resource
update times reject the collection rather than invent dates. A missing update
time is an explicit withheld state, not a proof that the body never changed.
Dates are parsed as timezone-aware instants; strings with fractional seconds
cannot bypass the exclusive JST end boundary.

An `updated_at` inside the day is only the API resource's last-modification
observation. It does not prove which particular field changed, modification
authorship, historical truth, completeness of updates, or all facts as of a
point in time. Deleted data, edits to comments created outside the day,
missing historical transitions, and original/edited state unavailable from the
API are not recovered by this patch. Such broader coverage remains unaccepted.

## Receipt v3 and model separation

The v1 Evidence **envelope** remains compatible, but projected events add one
explicit policy metadata field; their hashes therefore differ from legacy
originals. They must be bound to a **source receipt v3**, never mislabeled v2.
The v3 receipt includes `temporal_policy` and exactly one ordered
`temporal_observations` record per output event. Each record retains the full
**bounded original pre-projection event** plus observed timestamp metadata and
the projected event ID. This is private captured-source data, not a model input.
Original API wire bytes and unbounded/truncated-away bodies are not claimed to
be preserved by the collector.

`parse_verified_routes` verifies every original hash, replays the deterministic
projection and requires canonical-byte equality with model Evidence. It also
validates all original event types, URLs and day bounds. Missing/duplicate/
foreign/unsorted records, changed original data, illegal policy and v3→v2
downgrade with temporal metadata reject. Retained originals are found only in
the private receipt; C0/C1 returns the projected Evidence plus ordinary routing
proofs, not the held text. The Capture Index records the actual validated source
receipt version (2 for legacy, 3 for this policy). No extra CAS object or
transaction is introduced; existing receipt/Evidence byte limits still apply.
A larger v3 receipt may hit the existing 4-MiB ceiling and fail closed; it is
never silently truncated to fit.

Metadata notes do not authenticate the GitHub origin by themselves. The existing
trusted Collector UID/ACL, fixed CAS namespace and exact-hash provenance remain
necessary; root/compromised producer and forged API timestamps remain outside
those guarantees. A self-consistent triple is not a source signature.

## Human review and existing consumers

The existing C5 private original-source companion snapshots any temporal notes
before inference. It marks retained in-day fields as day-level snapshots, and
explains that unproven fields have been withheld. It shows captured/updated times
and field names without forwarding private out-of-period bodies to the model or
displaying them as the day's progress. The complete bounded originals remain
in the private receipt for separate audit. This note adds no model or CAS reads.
Legacy runs retain their prior shape, without acquiring temporal approval.

A dated creation/transition may still be rendered as that structural fact. The
new metadata is not authorization to claim current text/state was true then.
The C5 metadata-only report does not gain source text; all semantic/factuality/
publication flags remain false. Existing manifests/Evidence and old source
receipts remain readable but are not silently migrated or admitted as v3.

## Validation and release

Synthetic tests cover D creation + D+1 mutable fields, exact JST boundary,
current-day control, discussion/review-comment edits, missing modification time,
current lifecycle state versus dated action, mutable review without update time,
unchanged commit content, timestamps changing between API calls, malformed time,
complete/private original retention, original/projection tampering, receipt
version downgrade, canonical bool/int substitution, unchanged legacy requests
and bytes, actual POSIX-ACL CAS readback and C1/model separation, and C5 temporal
review notices. Offline replay of the previously captured real API responses
is a separate regression, NOT a fresh GitHub run or production acceptance.

Deploying this policy as default, time-valid producer/checkpoint admission,
large-day capacity, example-vs-actual-work semantics, full historical coverage,
#337 HTTP409 compatibility and explicit Writer/Timer release remain separate
work. Keep this change in a child Draft. Do not rewrite historical refs or
merge to main based on synthetic tests or a model's supported verdict.


## Bounded projection / review corrections

The opt-in wrapper charges **every returned source row** (including commits,
timeline rows and direct classification responses), with aggregate caps of
50,000 rows and 32 MiB canonical JSON across endpoints/repositories. Projection
checks total event count before constructing new objects, charges each bounded
original-record encoding before retaining it, and streams the complete receipt
into a maximum 4-MiB output buffer, stopping before materializing an oversized
aggregate. Existing transport/per-endpoint pagination allocations occur before
this wrapper and are unchanged; these are limits on added aggregate work, not
a hard upper bound on Python process RSS or all inherited page allocations.
Encoding may allocate one individual string chunk before charging its size.

Malformed original-event timestamps (including a rehashed original in a v3
receipt) are translated into the typed temporal/route rejection, not leaked
`GitHubDailyActivityError` exceptions. These corrections do not alter the
projection, model-visible content, API destinations, or publication decisions
for admitted bounded inputs.
