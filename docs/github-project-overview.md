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

A newly-created note has this shape:

```md
---
type: github-status
project: "[[Terreate]]"
github_repo: upiscium/Terreate
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

### Free-form notes

Only the region between the managed markers is generated. Everything outside that region, including `## Notes`, is preserved byte-for-byte.

If a pre-existing `Status.md` does not contain exactly one valid managed block, automation fails closed rather than replacing human content.

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

## Local pipeline

The feature is deliberately separate from Project status mutation.

```text
obsidian-github-sync.service
  -> obsidian-github-project-watch-enqueue
  -> obsidian-github-project-overview-enqueue

obsidian-github-writer.service
  -> obsidian-github-project-status-worker
  -> obsidian-github-project-overview-worker
```

The overview queue uses one stable local request file per Project path:

```text
25-Execution/<sha256(project-path)>.github-overview.json
```

If GitHub Issue/PR content is unchanged, request bytes remain unchanged. When the desired overview changes, the watcher atomically replaces that Project's request. Requests for Projects that no longer opt in are removed from the local handoff directory. This does not delete canonical `Status.md` files.

The overview worker intentionally re-evaluates every active request each cycle. This is required because checkbox and free-form edits happen in canonical Obsidian state, not in the GitHub proposal. If the rendered canonical bytes are already correct, the worker returns `already_desired` and performs no PUT.

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

Verify the new CLIs before the canary:

```bash
/opt/obsidian-github-sync/venv/bin/obsidian-github-project-overview-enqueue --help
/opt/obsidian-github-sync/venv/bin/obsidian-github-project-overview-worker --help
```

## Canary

Keep the recurring timer stopped during first deployment:

```bash
systemctl disable --now obsidian-github-sync.timer
systemctl start obsidian-github-writer.service
```

The dependency chain should perform mirror refresh, status enqueue, overview enqueue, status mutation and overview apply.

For a Project without an existing `Status.md`, expect an overview worker event with:

```json
{"status":"completed","outcome":"created"}
```

Confirm the new note in Nextcloud/Obsidian and toggle at least one Issue checkbox. Run the writer again. The second cycle should preserve the checked state and normally report `already_desired` when GitHub content has not changed.

After the canary passes:

```bash
systemctl enable --now obsidian-github-sync.timer
```
