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
- Nearest nonblank neighbor **risk flags** are computed once per nonblank
  line in linear time. This also avoids repeated inspection of a distant
  nonblank line with an extremely long indentation prefix. Long runs of
  empty/space-only lines must not cause quadratic rescanning, while the
  ambiguous-neighbor decisions remain unchanged.
- Until container-aware Markdown parsing is implemented, files containing
  both list markers (including empty items with no trailing space) and any
  fence-looking delimiter run are skipped entirely
  with reason unsupported-fence-syntax. This deliberately includes fences
  outside a list in the same document: a nested list fence closer must not
  be misclassified as a top-level opener and expose following raw HTML.
- Ordinary fenced code closing markers accept only contiguous matching
  delimiters with an optional ASCII-space or tab suffix, as required by
  CommonMark. Embedded markers, trailing text, or 4+ spaces of indentation
  are not accepted as a close. The fence state must never remain open after
  a valid tab-suffixed close, or later HTML could be exposed to formatting.
- YAML frontmatter, fenced code including Dataview/Meta Bind, multiline
  Templater expressions, HTML comments, known raw HTML blocks and ambiguous
  blockquote/indented code are not changed.
- Quoted fenced code (policy v2) is protected only when both delimiters and
  every intervening line carry explicit quote markers of the same depth.
  Indeterminate or unclosed quoted fences are skipped, as are quoted-fence
  markers inside another protected construct (frontmatter, ordinary fenced
  code, HTML, or Templater). Template/HTML opening syntax inside a quoted
  fence or its info string also causes a whole-file skip: Templater/HTML
  source spans are independent of Markdown fence boundaries and could
  otherwise alter content outside the fence. A quoted-fence document
  containing any literal less-than sign anywhere is likewise skipped
  (including comparisons and otherwise harmless code): Templater/HTML
  source-language spans may cross Markdown, YAML and HTML protection
  boundaries, and split/unknown markup cannot be parsed reliably here. The
  protective marker detector deliberately recognizes both v1's legacy
  rejection patterns (including indentation/tabs) and v2's spaced nested
  quote prefixes; only the narrower, explicitly depth-matched grammar
  can be admitted for transformation.
  Incomplete raw HTML opening tags, including partial attributes or a
  greater-than sign inside an unclosed quoted attribute, are rejected as
  unsupported HTML rather than treated as bounded raw-tag contexts.
  A literal closing-tag string inside an HTML attribute is not treated as
  the end of that element. Documents combining Templater commands with raw
  HTML are conservatively skipped, as are HTML comments that open inside
  raw elements (or raw markup inside an active comment). Independent
  source-language spans must not expose trailing string/comment bytes.
  Inside an active raw element, a closing tag is admitted only on its
  own line: text resembling an end tag inside nested attributes is not
  sufficient evidence to resume normal Markdown formatting.
  Nested opening elements within a raw HTML block are also skipped:
  their quoted attributes can extend across a standalone-looking close.
  This is a conservative coverage improvement, not an unrestricted
  CommonMark parser.
- Policy v3 adds deliberately broad, fail-closed *document-level* checks for
  two pre-existing source-integrity hazards. If any Templater opener occurs
  in a document with an ordinary Markdown code fence, YAML frontmatter,
  HTML comment, or indented-code region, the entire file is skipped. The
  minimal formatter does not try to reconcile Templater and Markdown state.
  Files containing namespace-qualified HTML-like tokens (including Unicode
  combining marks, punctuation, and harmless examples inside code fences)
  are also skipped. A linear token scan checks for colons; this is a
  reject-only sentinel, not an XML parser.
  All such skips return the fixed unsupported-html-block reason. This
  intentionally reduces coverage, including otherwise safe whitespace
  changes, rather than risking a source-integrity error. A standalone
  Templater command without an opaque context remains eligible.
  HTML-comment endings mixed with fence-looking markers are also skipped.
  A backtick-fence opening line with a backtick in its info string is
  rejected as unsupported-fence-syntax (even inside another protected
  region); otherwise an invalid Markdown opener could hide active raw HTML
  or Templater contexts and cause unsafe whitespace changes.
  Ordinary fenced-code closure requires contiguous matching markers
  followed only by ASCII spaces or tabs; mixed marker text cannot
  prematurely expose code contents. A v2 plan cannot be reused with a v3
  formatter or Writer.
- **Unrecognized HTML elements are not parsed in Phase 1:** any such opening
  tag outside protected code/frontmatter causes the *whole file* to be skipped
  with reason unsupported-html-block. This includes potentially harmless inline
  tags; preserving embedded HTML meaning takes priority over edit coverage.
  HTML comment/element/Templater transitions and multiple protected
  openings on one line are also handled conservatively (the entire file is
  skipped when the minimal formatter cannot determine which context is active).
  Unknown or unterminated protected blocks can skip a whole file.
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
