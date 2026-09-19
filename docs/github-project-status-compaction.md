# GitHub Project status request compaction

## Purpose

Project status requests are content-addressed immutable handoff artifacts:

```text
25-Execution/<proposal-sha256>.github-status.json
```

The writer stores terminal evidence separately:

```text
27-Transport/<proposal-sha256>.github-status.transport-result.json
27-Transport/<proposal-sha256>.github-status.rejection.json
```

Keeping every terminal request forever makes the status worker rescan historical
requests on every cycle. The compactor removes only status requests whose
terminal evidence is already durable and verified.

Overview requests are intentionally different. A
`*.github-overview.json` file is the current desired-state request for a
Project and is never handled by this compactor.

## Authority split

Compaction uses a credential-free Unix identity:

```text
obsidian-github-compactor
```

Its effective authority is:

```text
25-Execution  read + delete
27-Transport  read-only
24-Locks      no required access
GitHub credential              inaccessible
Nextcloud writer credential    inaccessible
mirror credential              inaccessible
Vault mirror / canonical Vault inaccessible
```

The writer therefore remains read-only on `25-Execution`; queue cleanup does
not weaken the existing writer/request separation.

Production provisioning is idempotent:

```bash
sh examples/github-sync/bootstrap-compactor-authority.sh
```

The bootstrap requires `setfacl` and the existing pipeline directories. It
creates the service identity if necessary, adds it to the read-only pipeline
handoff group, and grants only a named-user directory ACL with write authority
on `25-Execution`.

## Removal contract

For each `<sha>.github-status.json`, the compactor:

1. requires a 64-character lowercase SHA-256 filename;
2. rejects symlinks and non-regular requests;
3. parses the request through the canonical watcher proposal parser;
4. requires the parsed canonical proposal SHA to equal the filename SHA;
5. looks for exactly one terminal artifact:
   - transport result, or
   - rejection;
6. rejects symlinks/non-regular terminal artifacts;
7. requires terminal `record_version == 1`;
8. requires `stage == github_project_status_transport`;
9. requires terminal `proposal_sha256` to equal the request SHA;
10. requires a recognized terminal outcome;
11. re-reads and re-stats the request immediately before unlink;
12. unlinks only when inode metadata and exact bytes still match the validated
    request;
13. fsyncs the request directory after removals.

Recognized successful result outcomes are:

```text
applied
recovered
already_desired
```

Recognized rejection outcomes are:

```text
rejected_conflict
rejected_transport
```

If no terminal artifact exists, the request is pending and remains queued.

If both result and rejection exist, or any request/result binding is malformed,
the compactor fails closed and retains the request.

Terminal artifacts are not removed. They remain the durable transport history.

## Cycle ordering

The recurring chain is:

```text
obsidian-github-sync.timer
  -> obsidian-github-compactor.service
       -> obsidian-github-writer.service
            -> obsidian-github-sync.service
                 -> obsidian-github-sync-vault-pull.service
```

A timer activation therefore refreshes the mirror, observes GitHub, writes
status/overview proposals, applies canonical mutations, and only then compacts
terminal content-addressed status requests.

The compactor is not part of the writer service itself because the writer must
remain unable to modify `25-Execution`.

## CLI

Diagnostic/manual invocation:

```bash
obsidian-github-project-status-compact \
  --request-dir /var/lib/obsidian-github-pipeline/25-Execution \
  --result-dir /var/lib/obsidian-github-pipeline/27-Transport
```

The command emits a single summary JSON event with:

- `removed`;
- `kept_pending`;
- `failures`;
- `status` (`idle`, `completed`, or `failed`).

Malformed or ambiguous artifacts also emit bounded error events. File contents,
credentials, transport response bodies, and environment secrets are never
logged.

## Backlog migration

No special migration format is required. On the first successful production
cycle after the compactor is deployed, all historical
`*.github-status.json` requests with valid matching terminal evidence are
eligible for removal.

Pending requests and any malformed/ambiguous historical pair remain in place
for manual investigation.

The stable `*.github-overview.json` desired-state requests remain untouched.
