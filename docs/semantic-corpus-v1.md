# Semantic Corpus v1

## Purpose

Semantic Planner needs one Reader-owned, reproducible corpus contract before any embedding provider or vector backend is introduced.

This stage does not change automatic generation behavior. It only derives a content-addressed source/chunk manifest from the pull-only Vault mirror.

```text
00-DailyNote
05-Idea
10-Project
11-Knowledge
    |
    v
Reader
    |
    v
04-Index/semantic-corpus/<sha>.semantic-corpus.json
```

The manifest contains identities and metadata, not canonical source authority. Generator does not read the manifest or the Vault directly.

## Source kinds

```text
daily
idea
project
project-note
knowledge
```

### Daily

Eligible source:

```yaml
type: daily-review
```

Only the body of the H1 `# Note` section participates in v1 semantic chunking. Work, Tasks, Finance, Condition, Meta Bind boilerplate, and all other H1 sections are excluded by default.

An empty `# Note` section produces no semantic source.

### Idea

Eligible sources:

```yaml
type: idea
status: active | adopted
```

`archived` Idea notes are excluded from the default corpus. The manifest retains bounded metadata such as title, created, workspace, project, status, and tags.

Automation still has no canonical `05-Idea` write authority.

### Project

A `type: project` entry is a semantic anchor when its status is a supported non-cancelled Project status. Only the body of `# Project Summary` participates.

An active `type: project-note` is a content source only when its Project relation resolves unambiguously and the referenced Project is not cancelled.

### Knowledge

Eligible sources remain:

```yaml
type: knowledge-note
status: active
```

The source path and exact source SHA remain compatible with the existing BM25 Knowledge identity. Semantic Corpus does not replace the BM25 index.

## Chunk contract

Chunk policy:

```text
heading-section-lf-v0
```

Each chunk is bound to:

- exact source path;
- exact source SHA-256;
- chunk policy version;
- deterministic ordinal;
- original source line range;
- heading path;
- normalized LF chunk content SHA-256;
- bounded byte size.

The manifest does not persist chunk text. A later Embedder must reconstruct bounded chunk bytes from the exact canonical mirror source and verify source/chunk hashes before inference.

Meta Bind fenced blocks are excluded from semantic chunks.

One chunk is bounded to 16 KiB. One source is bounded to 128 KiB.

## Derived-state identity

The canonical manifest bytes contain record version, chunk policy version, ordered eligible source records, ordered chunk identities, and deterministic warnings. Unchanged Vault bytes and policy produce the same manifest SHA.

## Staleness

A selected manifest is stale when the current eligible ordered `(path, source_sha256)` set differs.

The Reader rebuilds and verifies against the pull-only mirror. Symlinks and case-fold collisions fail closed.

The host-local mirror read-view lock prevents one local rclone refresh from racing a manifest build. It does not claim remote Nextcloud freshness.

## Authority

The semantic corpus remains Reader-owned derived state.

- Sync owns the pull-only local mirror.
- Reader can read Daily / Idea / Project / Knowledge corpus roots.
- Reader writes `04-Index`.
- Generator cannot read Vault corpus roots or `04-Index`.
- Validator / Evaluator / Reviewer / Executor gain no Daily or Idea access.
- no canonical Idea, Project, or Knowledge mutation authority changes.

The production mirror adds `00-DailyNote/**` and `05-Idea/**` so Reader can derive the corpus. Existing automatic Input Planner selection remains Knowledge + active Project Note until a later Semantic Planner rollout.

## CLI

```bash
obsidian-semantic-corpus \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault
```

The command reports the manifest SHA, source/chunk counts, source-kind counts, and warning count.

## Next stage

Semantic Corpus v1 is the input contract for
[Semantic Embedding Index v1](semantic-embedding-index-v1.md).

The embedding stage keeps provider access in a dedicated `obsidian-ai-embedder`
identity and binds vectors to this exact corpus/chunk identity. Hybrid
BM25/vector ranking, novelty policy, and generation objectives remain outside
the Semantic Corpus contract.
