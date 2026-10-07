# GitHub Daily Project Progress projection and CAS writer v0

Related Epic: #258
Depends on: merged #260
Companion UI contract: upiscium/ObsidianCore#200
Implementation: #261

## Purpose

This stage consumes the grounded structured summary from #260 and performs the
first canonical Daily write in the pipeline.

It deliberately separates deterministic rendering from canonical transport:

\`\`\`text
GitHub Event Evidence
        |
        v
grounded summary
        |
        v
deterministic Renderer
        |
        v
content-addressed Daily Progress Projection
        |
        | narrow handoff
        v
Daily Writer
        |
        | GET + strong ETag
        | PUT If-Match
        | exact GET verification
        v
00-DailyNote/YYYY/MM/YYYY-MM-DD.md
\`\`\`

The writer never reads raw GitHub evidence or LLM output. It reads only the
validated content-addressed Projection produced by the renderer.

## Daily ownership boundary

ObsidianCore#200 defines one visible exact H1:

\`\`\`markdown
# Project Progress
\`\`\`

Automation owns only the body after that H1 through the next visible H1 or EOF.

Markdown comments are not ownership markers.

The writer requires:

- canonical path \`00-DailyNote/YYYY/MM/YYYY-MM-DD.md\`;
- the Projection date to match that path exactly;
- frontmatter \`type: daily-review\`;
- exactly one visible H1 named \`Project Progress\`;
- the heading to end with a normal LF or CRLF newline.

H1-looking text inside fenced code blocks is not an ownership boundary.

A missing or duplicate visible Project Progress H1 fails closed.

## Deterministic renderer

The renderer revalidates:

- exact Evidence bundle identity;
- exact grounded-summary identity;
- summary -> Evidence SHA binding;
- accepted claim identity;
- every accepted claim's original \`evidence_id[]\`;
- claim repository == cited-event repository;
- Project binding exists for every claim repository;
- source URLs remain canonical \`https://github.com/<owner>/<repo>/...\`.

The model does not choose Markdown structure.

Stable rendering order:

1. repository, case-insensitive then byte order;
2. claim kind: decision, implementation, bugfix, issue/PR progress;
3. summary text;
4. claim identity.

Repository sections use H2. Associated Obsidian Project names are rendered as
plain text. Claim kind labels are deterministic:

\`\`\`text
decision          -> 決定
implementation    -> 実装
bugfix            -> バグ修正
issue_pr_progress -> Issue/PR
\`\`\`

Common inline Markdown metacharacters in model-produced summaries are escaped
before rendering. GitHub links are reconstructed from the cited raw Evidence
events and are the only renderer-owned links appended to claims.

A zero-claim day renders one empty line under \`# Project Progress\`; it does not
invent a semantic "no progress" claim.

The rendered Project Progress body is bounded to 2 MiB. The renderer never
truncates claims to satisfy that bound; an oversized body fails closed.

## Projection

Schema:

\`\`\`text
schemas/github-daily-progress-projection-v1.schema.json
\`\`\`

The content-addressed Projection binds:

- date;
- deterministic Daily target path;
- Evidence bundle SHA-256;
- grounded-summary SHA-256;
- exact LF section-body bytes and SHA-256.

Projection section bodies may contain H2+ headings but are rejected if they
contain any H1, preventing a Projection from escaping its owned region.

Example:

\`\`\`bash
obsidian-github-daily-progress-render \
  --evidence /var/lib/obsidian-github-pipeline/daily-evidence/<evidence>.github-daily-evidence.json \
  --summary /var/lib/obsidian-github-pipeline/github-daily-summary/final/<summary>.github-daily-grounded-summary.json \
  --output-dir /var/lib/obsidian-github-pipeline/daily-progress
\`\`\`

Output:

\`\`\`text
<projection-sha>.github-daily-progress.json
\`\`\`

## Canonical writer

The writer requires an already-existing Daily Note. It never uses conditional
create and receives no Daily create authority.

Protocol:

\`\`\`text
GET target Daily
  -> require 200
  -> validate type + ownership heading
  -> compute exact desired bytes from current canonical bytes
  -> require strong ETag
PUT If-Match: <etag>
GET target Daily
  -> require exact desired bytes
\`\`\`

Only the owned section body is replaced. Current canonical bytes outside the
section are carried forward from the writer-side GET, so a stale mirror or
renderer artifact cannot overwrite unrelated Human edits.

LF/CRLF outside the owned region is preserved. Inserted body lines use the
Project Progress heading's newline style.

The full resulting Daily is bounded to 4 MiB.

## Missing Daily semantics

HTTP 404 on the initial GET is:

\`\`\`text
target_missing
exit code 4
\`\`\`

This is a retryable scheduling state, not permission to create the note.

The production scheduler in #262 may retry after ObsidianCore / the Human has
created the Daily Note.

## CAS and ambiguous transport

HTTP outcome classification follows the existing canonical-write model:

- \`412\`: deterministic ETag CAS conflict;
- \`401 / 403\`: deterministic authority rejection;
- other \`4xx\`: deterministic client rejection;
- \`500 / 502 / 503 / 504\`: ambiguous/transient PUT;
- network failure during PUT: ambiguous;
- other non-2xx PUT: deterministic transport rejection.

After an ambiguous PUT, a fresh GET is mandatory.

Only exact desired bytes permit \`recovered\`.

If remote bytes remain exactly the pre-write bytes, the outcome remains
retryable ambiguous failure. Any other divergence is a canonical conflict.

A successful non-ambiguous PUT also requires exact post-GET desired bytes.

## Canonical I/O lock

The apply CLI requires \`--state-root\` and acquires the existing shared
\`canonical-io.lock\` before WebDAV mutation and transport-result persistence.

#262 must provision the Daily writer identity with only the ACL needed for that
shared lock and its Projection/result handoff. It must not widen access to
unrelated AI lifecycle state.

## Transport result

The durable result binds:

- Projection SHA;
- date and target path;
- Evidence SHA;
- grounded-summary SHA;
- section-body SHA;
- outcome: \`applied\`, \`recovered\`, or \`already_desired\`;
- exact before/after canonical Daily SHA-256;
- completion timestamp.

Example:

\`\`\`bash
obsidian-github-daily-progress-apply \
  --projection /var/lib/obsidian-github-pipeline/daily-progress/<projection>.github-daily-progress.json \
  --state-root /var/lib/obsidian-ai/state \
  --base-url 'https://nextcloud.example/remote.php/dav/files/<daily-writer>' \
  --username '<daily-writer>' \
  --password-file /etc/obsidian-daily-progress/webdav-password \
  --result /var/lib/obsidian-github-pipeline/daily-progress-results/<projection>.transport-result.json
\`\`\`

Exit codes:

\`\`\`text
0  applied / recovered / already_desired
2  malformed input / authority / transport / retryable ambiguous error
3  canonical or ETag conflict
4  target Daily does not exist yet
\`\`\`

## Authority

Renderer needs:

- read exact Evidence;
- read exact grounded summary;
- write local Projection directory.

Renderer has no Nextcloud credential.

Daily Writer needs:

- read exact Projection;
- read/write the shared canonical I/O lock;
- one dedicated Nextcloud credential shared only to \`00-DailyNote\` with
  Read + Update.

Daily Writer does not need:

- raw GitHub Evidence;
- model/provider configuration;
- Semantic Index;
- Human Review state;
- Daily Create;
- Delete;
- Share.

The target Nextcloud permission set is therefore:

\`\`\`text
Read   = yes
Update = yes
Create = no
Delete = no
Share  = no
\`\`\`

## Rollout dependency

Do not enable the recurring writer before ObsidianCore#200 / PR #201 is promoted
to the Vault template surface. New Daily Notes must contain the exact visible
\`# Project Progress\` H1 before automatic apply is enabled.

Historical Daily Notes are not silently migrated by this writer; missing heading
is a conflict and remains a Human/Core migration concern.

## Next stage

#262 adds the previous-day scheduler, retries, authority provisioning, systemd
units, production smoke tests, and controlled canary/acceptance.
