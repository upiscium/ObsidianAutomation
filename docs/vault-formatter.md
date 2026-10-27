# Vault Formatter, Phase 1: read-only 98-System Markdown planning

Tracking: [formatter Epic #469](https://github.com/upiscium/ObsidianAutomation/issues/469),
[Phase 1 engine #470](https://github.com/upiscium/ObsidianAutomation/issues/470),
[future conditional writer #471](https://github.com/upiscium/ObsidianAutomation/issues/471).

## Purpose

Private Obsidian Vault Markdown occasionally contains whitespace-only lines with
one to three trailing ASCII spaces. A subsequent Gitea public projection fails
the unchanged `git diff --check` gate on these lines. The format planner
predicts a minimal whitespace-only correction before an authorized future
Writer can make any changes to the canonical Live Vault.

**This phase is intentionally read-only.** It cannot upload, rewrite, rename,
or delete anything. It does not repair Core Drift, run Gitea jobs, or modify a
production systemd unit.

## Scope

- Only files below `98-System/**` with `.md` or `.markdown` extensions
  can be candidates for change.
- Non-Markdown assets, JS, CSS, JSON and distribution bundles are skipped.
  These require type-aware later phases; a skip is not an error.
- Only lines consisting of exactly one to three ASCII spaces can be emptied.
  Non-empty lines retain all spaces, including Markdown hard breaks.
- Source BOM, existing LF/CRLF convention, final newline and all other bytes
  are retained.
- YAML frontmatter, fenced code including Dataview/Meta Bind, multiline
  Templater expressions, HTML comments, known raw HTML blocks and ambiguous
  blockquote/indented code are not changed. Unknown or unterminated blocks can
  skip a whole file.
- Paths with symlinks, hidden directories/files, invalid UTF-8, NUL bytes,
  unsupported line endings, or unsupported types are skipped.
- If the scope is absent/unsafe, or enumeration exceeds its file/byte budgets,
  planning fails closed; no partial plan is accepted.
- A source that changed while being read is not used for a plan.

This is *not* a general-purpose Markdown reformatter. Phase 2 will add
Obsidian-aware Markdown support for the rest of the Vault, and Phase 3 will
add pinned JS/JSON/CSS adapters.

## Usage

On a trusted machine with a local read-only copy of the Vault:

```sh
obsidian-vault-format-plan --vault /path/to/ObsidianVault
```

The entrypoint is a standard-library-only Python command available after
installing this package. Alternatively, set `PYTHONPATH=src` and use
`python3 -m obsidian_automation.vault_formatter` with the same arguments.

The default command prints JSON **metadata only**: relative paths, proposed
changed line numbers, old/new SHA-256, result/reason, bounded counters and a
deterministic plan SHA-256. No Vault contents are included. Every invocation
reports `mode: "dry-run"`, and the command **never performs writes**.

```sh
obsidian-vault-format-plan --vault /path/to/ObsidianVault --check
```

`--check` returns 1 if at least one safe change is proposed, 0 when none is
required, and 2 on invalid input or read/structural errors.

```sh
obsidian-vault-format-plan --vault /path/to/ObsidianVault --diff
```

`--diff` includes a unified diff in the local JSON. **Diffs may contain
private Vault content**. Do not send this output to public GitHub comments,
CI job logs, telemetry or untrusted services.

Safety resource limits can be lowered using `--max-files`, `--max-bytes`
and `--max-total-bytes`.

## Why normalizing a GitHub-only copy is not a repair

The Live Nextcloud Vault is user-authoritative. Normalizing only Gitea's
working copy, or only GitHub's public projection, leaves the original Live
file unchanged. Core Promotion would report Live Drift again.

Later issue #471 may consume this immutable format plan in an independently
authorized, source-digest/strong-ETag-checked, conditional WebDAV writer.
That writer must re-read and reformat the *current* bytes, compare the plan
and policy version, reject concurrent edits, verify the remote readback,
record per-file effects and fail closed. It must never infer write authority
from a JSON plan, and must not change read-only Snapshot credentials.

Public allowlist constraints in `configs/public-export.example.toml` and
existing `git diff --check` / Node validation remain untouched.

## Tests and acceptance

The standard-library `unittest` suite at `tests/test_vault_formatter.py`
runs without Vault access:

```sh
python3 -m unittest -v tests/test_vault_formatter.py
```

The suite covers the reported blank-line failure using a disposable Git
repository, CRLF/BOM/final-newline preservation, frontmatter/Dataview/
Templater/code/HTML protection, skip reasons, source scope, determinism,
file bounds, symlink exclusion and read-only CLI semantics.

Completion of #470 proves source-only planning and synthetic fixtures, *not*
production formatting. Live Vault Auto Apply requires #471/#472 implementation,
separate authorizations, independent acceptance, and a staged canary.
