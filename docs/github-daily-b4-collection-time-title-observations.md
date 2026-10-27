# B4 Daily — collection-time Issue/PR title observations (#444)

## Motivation

The v2 deterministic grouped Daily displays one linked title per Issue/PR,
but a real 2026-10-24 example included **41 entities and only 29 with a
title**. The remaining 12 were predominantly comment/review/commit-only
entities; dated event records do not include a reliable title envelope.

The title is display metadata, not an activity claim. Do **not** infer that a
current GitHub title existed at the time of an older Issue comment, PR review
or Git commit. Do not use a commit subject or comment text as the title.

## Source contract: v4 receipt

The existing route Collector already authenticates through pinned GitHub API
(https://api.github.com) and performs typed API identity checks. When it
collects a scheduled source with temporal policy, it now emits a **v4
source-route receipt** rather than v3, with an additional field named
entity_title_observations. Legacy v2/v3 receipts and stored captures
remain readable. The immutable original Evidence event list is unchanged.

Each title observation contains:
- repository, entity_type (issue or pull_request), number
- route: canonical authenticated /repos/<owner>/<repo>/issues/<number>
- observed_at_utc: UTC time when this GitHub resource was actually read
- title: nonempty, nontruncated bounded GitHub entity title (at most
  2048 UTF-8 bytes)

The source Validator requires the observation target to be an entity already
present in the **same** canonical dated Evidence, absent any usable original
title. It checks exact fields and types, strictly sorted and unique entity
keys, source route, canonical UTC timestamps within the collection interval,
a total cap of 128 titles, and canonical JSON encoding.

The Collector first reuses the trusted Issue/PR classification response it
already fetched for a comment; this avoids duplicate API calls for typical
comment-only entities. For missing titles not already in that in-process
cache, it performs at most one additional pinned API GET per entity up to
the global cap, verifies numeric and public Issue/PR identity and the PR API
route, and never follows a redirect or attaches secret data. HTTP
403/404/429/5xx for an **optional title lookup** does not drop valid source
events; the title stays unavailable. A mismatched route, entity number,
entity type or title format still fails closed.

The v4 title array is included in the existing canonical, SHA256-bound
source receipt. The capture index binds that receipt SHA to unchanged
Evidence SHA and the JST date. The existing nonroot Standard-CAS directory/
file ownership and 0600 rules remain unchanged. This is **not a remote
digital signature**; the authenticated pinned Collector process and its
private host role remain the trust anchor.

## Consumer / deterministic rendering

- read_standard_digest and Strict read_verified_source validate the
  v4 receipt and pass an admitted source object to a dedicated
  CAS-verified Digest adapter. The ordinary _build(bundle, capture_sha)
  accepts NO supplemental title arguments, including synthetic SHA inputs.
  The v4 verifier mints an in-process admission marker and binds the titles
  to the capture and original Evidence hash; changing a copied receipt
  object after admission also fails. The caller must still reach the actual
  private-CAS role/ownership read path before this content verification;
  neither a SHA nor an internal marker is a remote digital signature.
- The receipt's original dated events and their per-event timestamps do not
  change. Their typed grouping, event IDs, status diagnostics and issue
  counts remain identical to a digest without the new title observations.
- For an entity with no original usable title, the new observation supplies
  the **display label only**. The JSON Digest receipt records
  title_source=collection_observation and the observed UTC timestamp.
  Titles from real dated entity-event envelopes retain
  title_source=source_event_envelope; unresolved ones are missing.
- Never display observed_at_utc as an event occurred time; never promote
  an observed title into a claim that a feature was completed or merged.
- Markdown still escapes untrusted title text. The Digest report version
  becomes github-daily-deterministic-digest-v3, so verified Preview SHA
  and create-only Preview filenames intentionally change.
- Existing v2/v3 source receipts have no extra observation; no online
  lookup occurs during pure CAS replay or render. Historical v1/v2
  canonical Daily Notes are not modified retroactively.

## Acceptance

Offline fixture tests cover: comment-only PR with an issue-classification
response and no dated title; legitimate late-observed title; new v4
route/capture/Standard-CAS admission; old receipt read; deterministic output;
unchanged event status/IDs; same-source repeat immutability; optional
rate-limit/HTTP error without loss of the event; source identity mismatch,
bad route, cross-time, duplicate, oversized, untrusted title and Markdown
injection rejected or escaped.

The collector and Receipt changes do not enable the Writer, Timer, root
Worker, new API credentials or model. All real LXC service deployment,
new GitHub collection, Nextcloud publication and scheduled automation are
outside this **source-only** branch until separately authorized.
