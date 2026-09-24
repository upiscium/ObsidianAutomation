# Generated Knowledge Note layout v1

New proposals use `knowledge-note-layout-v1`. The semantic output schema and
prompt remain `knowledge-note-semantic-output-v0` and the current
`knowledge-note-generator-v2`. The prompt version is independent of the
deterministic layout version.
This rendering change does not require the LLM to generate application controls.

## Assembly

After the existing deterministic YAML envelope, the assembler inserts:

````markdown
```meta-bind-embed
[[knowledge-meta]]
```
````

It then adds a blank line and the semantic Markdown body. This is the same
metadata embed used by ObsidianCore's
`98-System/03-template/01-note/knowledge-note-template.md`, checked at Core
revision `892c6086635c6c8c71bf7fdbf9553d0acf141de2`.
The UI implementation remains Core-owned. Automation does not fetch Core at
runtime or allow the model to choose the deterministic embed target.

The renderer folds exact leading copies of this scaffold into one owned block.
It preserves quoted code examples, other embeds, and occurrences later in the
body. It is not a Markdown sanitizer or a general duplicate-block remover.
An output containing only the scaffold is rejected rather than becoming an
empty Knowledge Note. Ordinary body bytes, including meaningful trailing
spaces, are preserved except for normalization of final newlines to one LF.
The assembled-content byte limit includes the added UI.

The embed is included **before** immutable proposal persistence. Therefore
Validation, Evaluation, Human Review, Transport, and Receipt bind the same
complete bytes. Do not append the UI after approval or at the transport boundary.

## Identity and history

The new digest is:

```text
sha256(layout_version || NUL || context_sha || NUL || canonical_semantic_output)
```

The ID still uses the existing `knowledge-gen-v0-<digest>` namespace; `v0` is not
an assertion that rendering remains unchanged. The layout version is explicitly
domain-separated in the digest. Identical inputs on the same layout produce
identical proposals, but the old and new renderers do not reuse an ID for
different note bytes. The Generation Record continues to bind exact proposal
bytes and implementation revision; its record format is unchanged.

Historical v0 proposals did not include the metadata embed or layout-version
prefix in their digest input. They remain historical artifacts, not migration
inputs. The shared `knowledge-note-v0` validator is intentionally unchanged:
previously accepted notes without the embed are not retroactively rejected.
This is a guarantee of the new Generator assembler, not a new global mutation
policy or a claim that hand-written proposals are automatically repaired.

## Existing Live Notes

No migration, remote update command, or startup scan is provided.
For a Live Note still missing its editor, a human may use normal Obsidian source
editing to insert the block above immediately after the YAML closing delimiter,
unless it is already present. The embed requires the existing Core
`knowledge-meta` note and Meta Bind plugin configuration in that Vault.
Do not replace the embed with `![[knowledge-meta]]`: use the standard template.

Never edit the pull-only mirror to repair a canonical note. Do not change its
old proposal, Validation, Evaluation, Review, Transport Result, or Receipt, and
do not replay a completed create operation. A later normal edit does not change
what the original Receipt attested at creation time.

## Acceptance and deployment

Unit tests cover deterministic insertion, exact leading duplicate folding,
Markdown preservation, UI-only rejection, identity separation, total-size
checks, and compatibility with a legacy proposal. The normal repository CI
must also pass, including the separate-identity authority fixture.

A merge or green CI does not deploy production. The user merges and deploys the
pinned revision separately. The final UI acceptance uses a **new disposable
candidate**; the old completed note is not regenerated or re-PUT. Confirm the
embed in the validated content and review it before any permitted canonical
create. Confirm interactive rendering in Obsidian after deployment. No unattended
approval, overwrite, credential change, or ACL expansion is part of this fix.
