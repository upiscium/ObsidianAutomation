# GitHub Project Status.md overview

## Purpose

For every Obsidian Project that opts in with `github_watch: true`, the GitHub sync pipeline maintains a sibling `Status.md` containing the repository's currently-open Issues and Pull Requests.

For the canonical Project:

```text
10-Project/Terreate/Terreate.md
```

the generated note is:

```text
10-Project/Terreate/Status.md
```

## User-facing format

A newly-created note is also a canonical Project Note. Automation owns its
frontmatter and keeps a structured PR snapshot for Dataview consumers:

```md
---
type: project-note
project: "[[10-Project/Terreate/Terreate|Terreate]]"
workspace: "[[03-Workspace/Example/Example|Example]]"
category: list
lifecycle: active
aliases: []
tags: []
github_repo: upiscium/Terreate
github_status_managed: true
github_pull_requests:
  - number: 42
    title: "Example PR"
    url: "https://github.com/upiscium/Terreate/pull/42"
    status: ready
    bound_issues:
      - repository: "upiscium/Terreate"
        number: 203
        url: "https://github.com/upiscium/Terreate/issues/203"
---

# GitHub Status

<!-- obsidian-github-sync:overview:start -->
## Issues
- [ ] [#203 Example issue](https://github.com/upiscium/Terreate/issues/203) <!-- github:issue:203 -->

## Pull Requests
- [ ] [#42 Example PR](https://github.com/upiscium/Terreate/pull/42) <!-- github:pr:42 -->
<!-- obsidian-github-sync:overview:end -->

## Notes
```

The HTML comments are machine bindings and should remain intact. They are unobtrusive in Obsidian reading view.

### Review checkboxes

Users may toggle an Issue or Pull Request checkbox from `[ ]` to `[x]` in Obsidian. The next refresh preserves the checkbox state by `(kind, number)` even when the GitHub title changes.

New items start unchecked. Items that are no longer open disappear from the managed block.

### Structured Pull Request metadata

`github_pull_requests` contains the currently-open PRs for Project Entry Dataview
views. `status` is `draft` or `ready`. `bound_issues` is derived from GitHub
closing-keyword references in the PR body, such as `Closes #203` or
`Fixes owner/repository#10`. Plain issue mentions are not treated as bindings.

The production watcher reuses the PR rows already fetched for the normal repository
snapshot, so this metadata does not add a second GitHub Issue/PR collection pass.

### Free-form notes

Automation owns and rewrites the `Status.md` frontmatter and the region between
the managed markers. Human-authored body content outside the managed markers,
including `## Notes`, is preserved.

If a pre-existing `Status.md` does not contain exactly one valid managed block,
automation fails closed rather than replacing human content.

## Authority

The dedicated Nextcloud account `obsidian-github-writer` must still be shared only the canonical `10-Project` folder, but the overview feature requires create authority in addition to the existing status-update authority:

```text
Read   = yes
Update = yes
Create = yes
Delete = no
Share  = no
```

The Nextcloud share permission bitmask is therefore `7` (`Read=1 + Update=2 + Create=4`).

Create authority is used only for sibling `Status.md` files selected from validated `github_watch: true` Project bindings. The writer has no delete operation.

## One GitHub observation, two outputs

The normal watcher already fetches the open Issue and Pull Request endpoints in order to implement terminal Project reactivation. The production overview path reuses those exact response rows; it does not make a second Issue/PR collection pass.

```text
obsidian-github-sync.service
  -> obsidian-github-project-watch-enqueue
       -> GitHub snapshot once
       -> pending Project status proposal, when needed
       -> current Status.md desired-state request, every watched Project

obsidian-github-writer.service
  -> obsidian-github-project-status-worker
  -> obsidian-github-project-overview-worker
```

The standalone `obsidian-github-project-overview-enqueue` CLI remains available for diagnostics, but is not called by the production systemd unit.

The overview queue uses one stable local request file per Project path:

```text
25-Execution/<sha256(project-path)>.github-overview.json
```

If GitHub Issue/PR content is unchanged, request bytes remain unchanged. When the desired overview changes, the watcher atomically replaces that Project's request. Requests for Projects that no longer opt in are removed from the local handoff directory only after a complete successful watcher pass. This does not delete canonical `Status.md` files.

The overview worker intentionally re-evaluates every active request each cycle. This is required because checkbox and free-form edits happen in canonical Obsidian state, not in the GitHub proposal. If the rendered canonical bytes are already correct, the worker returns `already_desired` and performs no PUT.

## Path collision policy

A sibling `Status.md` is unambiguous when each watched Project has its own directory, for example:

```text
10-Project/Terreate/Terreate.md
10-Project/Terreate/Status.md
```

If two watched Project notes would target the same `Status.md` (for example two flat notes directly under `10-Project/`), enqueue fails closed. Automation never chooses one Project arbitrarily or overwrites another Project's overview.

## Canonical write protocol

For every request the writer:

1. GETs the canonical Project note;
2. verifies `type: project`, exact `github_repo`, and enabled `github_watch`;
3. derives sibling `Status.md` from the validated Project path;
4. GETs `Status.md`;
5. preserves checkbox state and all bytes outside the managed block;
6. creates a missing note with `PUT + If-None-Match: *`, or updates an existing note with strong ETag `PUT + If-Match`;
7. GETs `Status.md` again and requires exact desired bytes.

Writer results are stored in:

```text
27-Transport/<sha256(project-path)>.github-overview.transport-result.json
```

## Deployment

Before deploying the code, change the `obsidian-github-writer` share on canonical `10-Project` from Read+Update to Read+Update+Create. Do not grant Delete or Share.

After merging the feature, update the package and systemd units:

```bash
cd /opt/obsidian-github-sync/app
git fetch origin
git checkout main
git reset --hard origin/main

/opt/obsidian-github-sync/venv/bin/pip install \
  --no-deps --force-reinstall /opt/obsidian-github-sync/app

install -m 0644 \
  examples/github-sync/obsidian-github-sync.service \
  /etc/systemd/system/
install -m 0644 \
  examples/github-sync/obsidian-github-writer.service \
  /etc/systemd/system/

systemctl daemon-reload
```

Verify the relevant CLIs before the canary:

```bash
/opt/obsidian-github-sync/venv/bin/obsidian-github-project-watch-enqueue --help
/opt/obsidian-github-sync/venv/bin/obsidian-github-project-overview-worker --help
```

## Canary

Keep the recurring timer stopped during first deployment:

```bash
systemctl disable --now obsidian-github-sync.timer
systemctl start obsidian-github-writer.service
```

The dependency chain should perform mirror refresh, one GitHub observation, status/overview enqueue, status mutation if needed, and overview apply.

For a Project without an existing `Status.md`, expect an overview worker event with:

```json
{"status":"completed","outcome":"created"}
```

Confirm the new note in Nextcloud/Obsidian. Toggle at least one Issue checkbox and optionally add text under `## Notes`, then run the writer again. The next cycle must preserve those edits. If GitHub content is otherwise unchanged, no semantically unnecessary content should be lost; the worker may update once to incorporate the user's checkbox/notes-aware render and should settle to `already_desired` thereafter.

After the canary passes:

```bash
systemctl enable --now obsidian-github-sync.timer
```
