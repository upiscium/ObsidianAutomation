# Installation

This guide is the entry point for installing ObsidianAutomation on a production automation host.

It intentionally describes the order of operations and the minimum production contract. Detailed authority, failure semantics, and component-specific behavior remain documented in the linked design and runbook documents.

## What gets installed

The consolidated production profile uses one `obsidian-automation` trust domain for the automation software while retaining separate Unix identities for each authority.

Canonical application paths are:

```text
/opt/obsidian-automation/app
/opt/obsidian-automation/venv
/usr/local/sbin/obsidian-automation-update
/var/lib/obsidian-automation/deployments
```

AI runtime data remains separated into a pull-only Vault mirror and local-only lifecycle state:

```text
/var/lib/obsidian-ai/
├── vault/
│   ├── 00-DailyNote/
│   ├── 05-Idea/
│   ├── 10-Project/
│   └── 11-Knowledge/
└── state/
    ├── 00-Untrusted/
    ├── 02-Orchestration/
    ├── 04-Index/
    │   ├── semantic-corpus/
    │   ├── semantic-embedding-requests/
    │   ├── semantic-embedding-plans/
    │   ├── semantic-embedding-results/
    │   ├── semantic-embedding-result-sets/
    │   └── semantic-index/
    ├── 05-Context/
    ├── 10-Validation/
    ├── 12-Evaluation-Request/
    ├── 14-Evaluation-Context/
    ├── 15-Evaluation/
    ├── 16-Human-Projection/
    ├── 17-Human-Projection-Result/
    ├── 20-Review/
    ├── 24-Locks/
    ├── 25-Execution/
    ├── 27-Transport/
    └── 30-Receipts/
```

Do not place lifecycle state inside the Vault mirror.

## Requirements

The production host is expected to be a dedicated Linux host or LXC. The current production model is single-host; the shared execution lock is not a distributed lock.

Required host capabilities include:

- Python 3;
- Git;
- Python venv support;
- Debian wheelhouse packages for offline/non-PyPI production installation;
- Linux POSIX ACL support with `setfacl` and `getfacl`;
- CA certificates;
- systemd;
- network access to the configured LLM endpoint and Nextcloud only for the identities that require it.

For a Debian-family host, install the bootstrap dependencies before the first deployment:

```bash
sudo apt-get update
sudo apt-get install -y \
  git \
  ca-certificates \
  acl \
  python3 \
  python3-venv \
  python3-setuptools-whl \
  python3-wheel-whl
```

The production bootstrap does not rely on PyPI. Build requirements are resolved from the local Debian wheelhouse.

## Nextcloud accounts

Keep the following remote authorities separate.

### Canonical writer

The `obsidian-ai-sync` identity is the only identity that may hold the canonical Nextcloud writer credential.

Its credential is used for conditional canonical creation and for Human-facing projection CREATE operations. Do not give this account Delete authority merely for projection cleanup.

### Review reader

Review Intake uses a separate read-only Nextcloud account. It reads the Human-edited Review projection and cannot write the Vault.

Example non-secret configuration:

```text
/etc/obsidian-ai/review-intake.env
/etc/obsidian-ai/review-intake-password
```

See `examples/ai/review-intake.env.example`.

### Projection cleanup account

Projection cleanup uses another dedicated account scoped only to the existing `04-AI` share.

Grant only Read + Delete permission to that share. Do not expose `11-Knowledge` or another canonical Vault root to this account.

Configure:

```text
/etc/obsidian-ai/projection-cleanup.env
/etc/obsidian-ai/projection-cleanup-password
```

See `examples/ai/projection-cleanup.env.example`.

## Provider configuration

Generator and Evaluator use deployment-private OpenAI-compatible provider configuration.

Create:

```text
/etc/obsidian-ai/pre-review-generator.env
/etc/obsidian-ai/pre-review-evaluator.env
```

Typical content is:

```text
OPENAI_BASE_URL=https://llm.example/v1
OPENAI_API_KEY=...
```

`OPENAI_API_KEY` is optional when the endpoint requires no bearer authentication.

Do not store these files in this repository. Model names and exact model revisions are bound by the immutable job recipe rather than these environment files.

### Semantic embedding provider

Semantic Embedding Index v1 uses a separate `obsidian-ai-embedder` Unix identity
instead of granting provider access to Reader.

The initial rollout is operator-driven and uses Ollama's native embedding API.
No recurring semantic-index service or timer is installed by this stage.

Reader pins an exact installed model identifier and digest into the immutable
embedding plan. Embedder re-resolves that identity before inference and refuses a
digest mismatch.

For a local Ollama endpoint, no provider credential file is required. Run the
prepare/embed/finalize flow described in
[Semantic Embedding Index v1](semantic-embedding-index-v1.md).

The Embedder has no Vault access. Do not work around an ACL failure by granting
it read access to `/var/lib/obsidian-ai/vault`; the bounded embedding request is
the intended data boundary.

Semantic retrieval query embeddings and offline benchmark queries reuse the same
request/result subtrees and the same Reader -> Embedder -> Reader authority
boundary. See [Semantic Hybrid Retrieval v1](semantic-hybrid-retrieval-v1.md).
Phase C remains operator-driven and does not install or activate a recurring
semantic retrieval service.

Semantic Planner Phase E is also observation-first. After a verified Semantic
Index exists, Reader may run `obsidian-semantic-selection` manually to inspect
versioned policy output and novelty decisions. No production generation service
is switched to semantic selection by installation alone.

See [Semantic Selection and Novelty v1](semantic-selection-novelty-v1.md).

Semantic Planner Phase F adds an explicit operator-driven Objective Context and
Generator path. Reader prepares the exact selected chunk bytes with
`obsidian-semantic-objective-context`; Generator consumes only that immutable
`05-Context` artifact through `obsidian-semantic-objective-generate`.
OpenAI-compatible and Ollama providers are supported. This does not enable the
automatic Planner or grant Generator Vault/index access.

See [Semantic Generation Objectives v1](semantic-generation-objectives-v1.md).

## Vault mirror configuration

The AI input mirror is pull-only. The reviewed default filter includes the current Generation roots plus Reader-owned Semantic Corpus roots:

```text
+ /00-DailyNote/**
+ /05-Idea/**
+ /11-Knowledge/**
+ /10-Project/**
- /**
```

Daily and Idea are mirrored for Reader-owned semantic indexing only. The current automatic Input Planner still submits Generation Context from active Knowledge and active Project Notes until the Semantic Planner rollout explicitly changes that contract.

See `examples/ai/vault-pull.filters`.

The mirror must never upload lifecycle state back to Nextcloud.

## Fresh installation

Use one exact reviewed full commit SHA. Do not deploy a floating `main` reference.

Obtain a clean checkout of that reviewed revision in a temporary source directory, then run:

```bash
sudo python3 tools/production_bootstrap.py \
  --target-sha <reviewed-full-merge-sha> \
  --profile automation
```

The bootstrap:

1. verifies the exact target;
2. creates or validates the production checkout;
3. creates the production venv;
4. installs ObsidianAutomation non-editably;
5. creates the production Unix identities and directory layout;
6. applies the POSIX ACL authority matrix;
7. installs the standalone exact-SHA update launcher;
8. writes a secret-free bootstrap receipt.

The bootstrap does not create production credentials.

For the complete bootstrap contract and fail-closed behavior, see [Source-side production bootstrap](production-bootstrap.md).

## Install private configuration

After the authority layout exists, install the deployment-private configuration and credentials required by the roles you intend to enable.

At minimum, an AI deployment normally needs:

```text
/etc/obsidian-ai/rclone.conf
/etc/obsidian-ai/vault-pull.filters
/etc/obsidian-ai/pre-review-generator.env
/etc/obsidian-ai/pre-review-evaluator.env
/etc/obsidian-ai/human-projection.env
/etc/obsidian-ai/webdav-password
/etc/obsidian-ai/review-intake.env
/etc/obsidian-ai/review-intake-password
/etc/obsidian-ai/projection-cleanup.env
/etc/obsidian-ai/projection-cleanup-password
```

Use the repository examples for non-secret shape only. Keep secrets outside Git.

## Exact-SHA deployment

After the standalone launcher exists, normal updates use:

```bash
sudo /usr/local/sbin/obsidian-automation-update \
  --target-sha <reviewed-full-merge-sha> \
  --profile automation
```

Do not manually stop or disable the managed timers before a normal contract-4 update. The updater records their original enabled/active state, quiesces them, performs the exact-SHA update and safe smoke, then restores the captured state.

The managed timers are:

```text
obsidian-ai-vault-pull.timer
obsidian-pre-review.timer
obsidian-github-sync.timer
obsidian-core-promotion.timer
```

For the full transaction and recovery rules, see [Exact-SHA automation host lifecycle](host-runtime-lifecycle.md).

## Verify the deployment

Confirm the deployed revision:

```bash
git -C /opt/obsidian-automation/app rev-parse HEAD
cat /etc/obsidian-ai/pre-review-revision.env
```

Both values must match the reviewed target SHA.

Verify the managed timers:

```bash
systemctl is-enabled \
  obsidian-ai-vault-pull.timer \
  obsidian-pre-review.timer \
  obsidian-github-sync.timer \
  obsidian-core-promotion.timer

systemctl is-active \
  obsidian-ai-vault-pull.timer \
  obsidian-pre-review.timer \
  obsidian-github-sync.timer \
  obsidian-core-promotion.timer
```

Verify the post-review projection publisher is installed:

```bash
systemctl show \
  obsidian-ai-post-review-projection-sync.service \
  -p LoadState \
  -p ActiveState \
  -p SubState
```

For a waiting oneshot service, `LoadState=loaded` with `ActiveState=inactive` and `SubState=dead` is normal.

## Safe smoke and acceptance

The exact-SHA updater automatically runs the credential-free safe smoke. It validates unit wiring, identity separation, revision binding, and required sandbox paths without calling the LLM provider or performing a canonical Nextcloud write.

A fresh deployment should additionally complete the production acceptance sequence documented in [Pre-review production rollout and acceptance](pre-review-production.md), including:

- provider canary;
- production identity idle-chain check;
- real pipeline observation;
- Human Review acceptance;
- post-review projection verification.

## Human-facing projection prerequisite

Before enabling AI projection, ensure the canonical Vault contains the `04-AI` root and that the appropriate accounts can access it according to their authority.

Projection requests target only `04-AI/**`.

For how projection works after installation, continue with [Getting started](getting-started.md).

## Recovery

If an exact-SHA update fails after quiescing recurrence, do not delete the deployment journal or deploy a different SHA immediately.

The pending intent is stored under:

```text
/var/lib/obsidian-automation/deployments/pending-runtime.json
```

Repair the reported prerequisite and retry the same target SHA. See [Exact-SHA automation host lifecycle](host-runtime-lifecycle.md) for containment and recovery semantics.

## Detailed references

- [Getting started](getting-started.md)
- [Human-facing AI lifecycle projection](human-ai-projection.md)
- [Pre-review production rollout and acceptance](pre-review-production.md)
- [Production authority topology](ai-production-authority.md)
- [Source-side production bootstrap](production-bootstrap.md)
- [Exact-SHA automation host lifecycle](host-runtime-lifecycle.md)
