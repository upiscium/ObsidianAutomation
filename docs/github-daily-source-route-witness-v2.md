# Daily v2: authenticated GitHub source-route witness (#328)

- **Status:** experimental, nonwired, read-only; no production authority
- **Parent:** [#324](https://github.com/upiscium/ObsidianAutomation/issues/324)
- **Dependent:** Draft [#325](https://github.com/upiscium/ObsidianAutomation/pull/325), Draft [#327](https://github.com/upiscium/ObsidianAutomation/pull/327)
- **Unchanged:** Daily v1 Collector CLI and content-addressed Evidence contract; 2026-10-07/08 immutable records, model/Renderer, Nextcloud Writer and timer

## Problem

A GitHub Issue comment can belong to a PR even when its public link is
`github.com/owner/repo/issues/N#issuecomment-ID`. The v1 Collector uses
`known_pr_numbers` and link substrings to classify it. If the PR was absent
from the day's `/issues?since=` enumeration, v1 can incorrectly classify
the PR's discussion as an Issue. The source's `entity_type` and immutable
`evidence_id` then self-consistently preserve that **incorrect** type.
A PR commit's `/commit/SHA` URL similarly does not prove membership in PR N.

The v2 Issue/PR-only renderer and entity Context must not treat these
weak identifiers as trusted. They quarantine ambiguous events while this
Collector-origin problem remains unresolved. Quarantined material is not
proof that there was no substantive progress.

## First independent implementation slice

`github_daily_source_route.collect_fresh_verified_routes` is an
**explicit opt-in API**, not the existing Daily v1 collector executable.
It requires an explicitly configured **authenticated GitHub.com API** client
(`https://api.github.com` with a nonempty token) and runs the current
collector for the requested JST day. It then **rechecks the GitHub source
immediately in the same invocation**:

- For each Issue/PR discussion comment, fetch the exact API
  `/repos/{owner}/{repo}/issues/comments/{numeric_comment_id}`.
  Check numeric source ID, original comment URL, author, timestamp, bounded
  content and the canonical `issue_url` route. Then fetch the exact
  `/repos/{owner}/{repo}/issues/{N}` resource to verify real Issue
  identity and `pull_request.url` linkage (or authoritative absence).
  A known-PR cache or HTML substring never settles classification.
- If the freshly verified comment changes entity class, create a **new**
  in-memory, v1-schema-compatible Evidence event with a fresh canonical
  `evidence_id`, then recompute a new Evidence-bundle SHA. Never rewrite
  any existing immutable v1 record or silently change old source identities.
- For PR review comments, refetch its exact
  `/pulls/comments/{numeric_comment_id}` and require its linked
  `pull_request_url` to match the selected repository/PR.
- For PR commits, check membership in the **bounded, authenticated**
  `/pulls/{N}/commits` API response. Each SHA, public commit URL, original
  normalized message, actor and timestamp must agree; a commit URL alone is
  insufficient. A missing or duplicated source SHA blocks verification.
- The exact-v2 route receipt (record version 2) binds each required event ID,
  type, repository, number and source ID to the **fixed, canonical GitHub
  endpoint paths**, verified remote object ID and exact newly generated
  Evidence bundle SHA. Route entries are sorted and canonical JSON hashed.
  It contains no raw GitHub comment, model output, secret, local filesystem
  path or authorization to publish.

The receipt is **not a remote cryptographic signature**. Content SHA proves
integrity **only relative to a trusted capture and storage authority**, not
the truth of a malicious API response or compromised Collector identity.
A future role-separated producer must publish receipt bytes, original
Evidence and collection identity atomically under an independently reviewed
CAS/ACL contract before a v2 summary may rely on them.

`parse_verified_routes(receipt_bytes, evidence_bytes)` strictly reparses
the old Evidence-v1 schema and all original event hashes, **validates all
original event kinds/types, canonical GitHub links, and the exact JST date
window even against freshly recomputed SHA values**, then demands one
matching route entry for every comment/PR-commit event requiring independent
attribution. Missing, extra, duplicate, incorrect target, unsorted, old-format,
noncanonical or mismatched receipt/evidence fail closed.

**Important compatibility rule:** Existing v1 Evidence (including the
already published October 7 canary and the October 8 immutable 269-event
snapshot) is **not retrospectively reclassified** or granted a new witness.
This helper's output can only describe a **new explicitly fresh-source API
reconfirmation**. A normal invocation is limited to the local JST today or
yesterday (to support next-day Daily finalize). A previous date requires
`explicit_historical_reconfirmation=True`, and then the route receipt is
labelled `explicit_historical_reconfirmation` with its actual UTC capture
time, **never** as an original historical collection. This opt-in does not
authorize historical Writer effects or retrospective ref changes. Any new
v2 CAS publication requires separate explicit design/approval.

## Bounds and safety

- At most 2,048 source events needing verification per fresh collection;
  more events fail closed **without silently dropping any original event**.
- At most 32 pages of 100 commits per PR; exceeded collections fail closed.
  This opt-in postcollection bound does **not** retrofit a cap on the existing
  v1 `GitHubClient._paged` traversal. A full-v2 collector must enforce
  bounded source pagination before collection, not just at verification.
- At most 32 MiB evidence and 4 MiB route receipt for independent read-model
  verification. Violations are errors, not silently truncated data.
- API target routes are constructed from the scanned allowlisted repository
  and positive numeric IDs; response-provided URLs are compared, **never
  requested directly**. Refetched API errors, missing/deleted entity,
  rate-limit, ambiguous type, wrong author/time/body, mismatched PR membership
  or changing source during collection block v2 candidate qualification.
- No I/O beyond the operator-authorized source GitHub API calls and existing
  pull-only Project-binding scan. No filesystem persistence, model inference,
  human action, production Writer/Timer/systemd/Nextcloud action or
  live Vault mutation is wired here.

## Next gated implementation work

1. Independent Correctness and Security review + exact-SHA CI of this
   nonwired source-route API.
2. Add an audited **Collector-owned** v2 sealed receipt writer with explicit
   role permissions, atomic no-overwrite CAS and exact source timing.
   Preserve original Evidence v1 and historical checkpoints.
3. Extend v2 #325 / #327 to accept only exact witnessed entity events and
   require a content-addressed sidecar in the same trusted date-scoped
   collection transaction; until then quarantine is correct.
4. Add a v2 collection pagination budget that counts **all source pages**,
   not only verified result events, and define an explicit retry/pending state
   for time-varying or unavailable GitHub sources.
5. Add per-entity Grounding, unresolved/semantic omission calibration #290,
   Japanese final editorial #283, resume/CAS version migrations #291/#293,
   manual human quality acceptance and **separately consented** date-scoped
   Writer/Timer rollout.

This slice **does not complete Issue #328** or certify Daily v2 production.
