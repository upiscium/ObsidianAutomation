# GitHub Daily Project Activity Evidence v1

Related Epic: #258
Implementation: #259

## Purpose

This contract captures one \`Asia/Tokyo\` calendar day of GitHub activity for
Obsidian Projects that opt in with \`github_watch: true\`.

The evidence layer is deliberately separate from both existing GitHub Project
status synchronization and Semantic Planner retrieval.

\`\`\`text
10-Project/** binding
        |
        v
GitHub API
        |
        v
GitHub Daily Event Evidence v1
        |
        v
future bounded AI summary pipeline
\`\`\`

Project notes determine only which repository belongs to which Project. Progress
content is collected from GitHub only.

## Project selection

The collector reuses the existing Project scanner. Therefore v1 watches Project
entries that:

- are below \`10-Project/**\`;
- have \`type: project\`;
- have \`github_watch: true\`;
- have a valid \`github_repo: owner/name\`;
- are in \`planning\`, \`running\`, or \`stable\`.

\`stopped\`, \`done\`, and \`cancelled\` remain outside GitHub polling.

If the Project scan emits a warning for an opted-in binding, Daily evidence
collection fails closed instead of silently publishing an incomplete day.

## Day boundary

The canonical timezone is fixed:

\`\`\`text
Asia/Tokyo
\`\`\`

For date \`D\`, the evidence window is:

\`\`\`text
D 00:00:00 JST <= event < D+1 00:00:00 JST
\`\`\`

The artifact records the equivalent exact UTC \`window_start\` and \`window_end\`.
The CLI requires an explicit \`--date YYYY-MM-DD\`, so scheduler policy and manual
replay use the same collection semantics.

## Initial event taxonomy

v1 records:

\`\`\`text
issue_created
issue_snapshot
issue_closed
issue_reopened
issue_comment

pull_request_created
pull_request_snapshot
pull_request_closed
pull_request_reopened
pull_request_merged
pull_request_ready_for_review
pull_request_converted_to_draft
pull_request_comment
pull_request_review
pull_request_review_comment
pull_request_commit

default_branch_commit
\`\`\`

\`issue_snapshot\` / \`pull_request_snapshot\` provide bounded title/body context when
an existing item was updated during the target day. They are supporting evidence,
not a claim that the body itself changed at \`updated_at\`.

Issue/PR lifecycle evidence is read from GitHub's issue-event API. Review
submissions and review comments use their dedicated Pull Request endpoints.
Default-branch commits use the repository commit endpoint.

GitHub Actions runs/logs are outside v1.

## Per-event bounds

There is deliberately no Daily-wide event-count or evidence-byte truncation
policy. A high-activity day must become more downstream batches, not a lossy
subset of the day.

Text is bounded independently at each event field:

\`\`\`text
title                  2 KiB
Issue / PR body       16 KiB
Issue / PR comment     8 KiB
PR review body         8 KiB
commit message         4 KiB
\`\`\`

Bounds are UTF-8 byte bounds, not character counts.

Every bounded field records:

\`\`\`json
{
  "text": "...",
  "truncated": true,
  "original_bytes": 22000,
  "included_bytes": 16384
}
\`\`\`

A UTF-8 multibyte character is never persisted partially; \`included_bytes\` may
therefore be slightly below the configured bound.

No event is dropped merely because another event is large or because many events
occurred on the same day.

## Evidence identity

Each normalized event is canonicalized independently and SHA-256 hashed. The
result is its \`evidence_id\`.

The hash includes:

- event kind;
- repository;
- exact UTC occurrence timestamp;
- GitHub URL;
- actor when available;
- entity type/number;
- stable source ID or commit SHA;
- bounded text fields and truncation metadata;
- bounded state/draft metadata.

Therefore the same normalized GitHub snapshot reproduces the same evidence ID.
If GitHub source text is edited later, a replay may intentionally produce a new
event identity reflecting the newly observed source bytes.

The Daily bundle is also canonical JSON and content-addressed by SHA-256. It does
not contain a collection timestamp, so an unchanged replay remains byte-identical.

## Bundle shape

Schema:

\`\`\`text
schemas/github-daily-evidence-v1.schema.json
\`\`\`

Top-level fields:

\`\`\`text
record_version
date
timezone
window_start
window_end
projects[]
repositories[]
events[]
\`\`\`

\`projects[]\` keeps the exact Project path -> repository binding.

Events are globally sorted by stable deterministic keys after collection. Exact
duplicate evidence IDs are de-duplicated, but distinct GitHub events are never
collapsed based on semantic similarity.

## Collection strategy

The collector uses only GitHub repository endpoints needed by the watched
repositories:

- repository Issues updated since the start of the window;
- per-relevant-Issue lifecycle events;
- repository Issue comments since the start;
- repository PR review comments since the start;
- reviews for PRs relevant to the day;
- commits for PRs relevant to the day;
- default-branch commits inside the exact window.

GitHub timestamps are always filtered again locally against the exact half-open
JST-derived UTC window. API query boundaries are not treated as sufficient proof.

PR commit timestamps currently use Git commit author/committer metadata exposed
by the REST commit payload. They do not claim to be an exact branch-push
timestamp. A future source may add repository push-event evidence without
reinterpreting v1 records.

## Authority

The collector:

- reads the pull-only \`10-Project\` mirror;
- reads GitHub;
- writes only an explicitly supplied local evidence directory.

It does not:

- write the canonical Vault;
- read the Semantic Index;
- call an LLM;
- approve a mutation;
- hold a Nextcloud writer credential.

This keeps GitHub observation distinct from future AI summarization and Daily
transport authority.

## CLI

\`\`\`bash
obsidian-github-daily-activity-collect \
  --config /etc/obsidian-github-sync/config.toml \
  --date 2026-10-05 \
  --output-dir /var/lib/obsidian-github-pipeline/daily-evidence
\`\`\`

The output filename is:

\`\`\`text
<bundle-sha256>.github-daily-evidence.json
\`\`\`

An existing identical content-addressed artifact is accepted idempotently.
A path collision with different bytes fails closed.

## Downstream contract

Issue #260 will partition the complete \`events[]\` sequence into bounded model
contexts. Partitioning may increase the number of inference calls but must not
delete events.

Partial and final claims must keep the original \`evidence_id[]\`; intermediate LLM
text never becomes the provenance root.

Issue #261 will render only grounded structured claims into the Daily
\`# Project Progress\` section defined by \`upiscium/ObsidianCore#200\`.
