# Getting started

This guide explains what to expect after ObsidianAutomation is installed and how to use the Human-facing AI projection from Obsidian.

For host installation and exact-SHA deployment, start with [Installation](installation.md).

## Mental model

The most important distinction is:

```text
private lifecycle state = authority
04-AI/**              = Human-facing read model
```

Files under `04-AI/**` show pipeline progress and expose the Human Review request control, but they are not authoritative Validation, Review, Execution, Transport, or Receipt artifacts.

Editing a projection does not directly authorize a canonical Vault write.

## Projection layout

A normal case progresses through:

```text
04-AI/
├── 00-Input/
├── 10-Context/
├── 20-Generation/
├── 30-Validation/
├── 40-Evaluation/
├── 50-Review/
├── 60-Execution/
├── 70-Transport/
├── 80-Completed/
└── 90-Failed/
```

Each case is identified by one `ai_case_id`, and each stage is represented by a deterministic Markdown projection.

The private source artifacts remain below `/var/lib/obsidian-ai/state/**`.

## End-to-end flow

The normal production path is:

```text
Input Planner
    ↓
Generator
    ↓
Validator
    ↓
Reader
    ↓
Evaluator
    ↓
04-AI/50-Review/<case>.md
    ↓ Human changes review_request only
Review Intake
    ↓
20-Review/<mutation>.approval.json
    ↓
Executor prepare
    ↓
25-Execution/<mutation>.transport-request.json
    ↓
Sync transport
    ↓
11-Knowledge/<Knowledge Note>.md
    ↓
27-Transport/<mutation>.transport-result.json
    ↓
Executor finalize
    ↓
30-Receipts/<mutation>.receipt.json
    ↓
scheduler reconcile
```

The Human-facing read model follows the same post-review progress:

```text
50-Review
   ↓
60-Execution
   ↓
70-Transport
   ↓
80-Completed
```

## What the stages mean

### 00-Input

Shows which source notes were selected for the case and the generation objective.

### 10-Context

Shows the exact context bundle passed across the Reader -> Generator boundary.

### 20-Generation

Shows the generated candidate and its intended target path. Generated Markdown is rendered inertly inside the projection.

### 30-Validation

Shows deterministic Validation. Validation is authoritative in private state; this Markdown file is only its read model.

A rejected Validation does not proceed to Human Review.

### 40-Evaluation

Shows the Evaluator assessment, including groundedness, redundancy, consistency, recommendation, and bounded findings.

The recommendation is advisory. It is not approval authority.

### 50-Review

This is the Human decision surface.

The projection contains a `review_request` control with:

- `approve`;
- `reject`;
- pending/blank.

Changing this field is a Human request only.

Review Intake fetches the published projection, compares it against the original immutable projection, and requires that no semantic field other than `review_request` changed.

Only after that verification does it create authoritative `20-Review`.

### 60-Execution

Appears after an approved Review has produced the exact Execution transport request.

It means canonical execution has been prepared and is waiting for or entering Sync transport.

### 70-Transport

Appears only after Sync has produced an exact `created_verified` transport result.

It means the canonical remote bytes were verified and Receipt finalization is pending.

### 80-Completed

Appears only after the exact Receipt is present and bound to the verified transport chain.

It is the terminal Human-facing success state.

### 90-Failed

Reserved for Human-visible failed pipeline states.

## Approving a Review

Open the case under:

```text
04-AI/50-Review/
```

Review:

- target path;
- deterministic Validation result;
- Evaluator assessment;
- candidate content.

Then set `review_request` to `approve`.

Do not edit the candidate, hashes, case ID, target path, or other frontmatter fields. Review Intake intentionally fails closed when anything other than `review_request` differs from the immutable published projection.

Approve does not directly write `11-Knowledge`.

The expected progression is:

```text
awaiting_human_review
        ↓
approved_pending_execution
        ↓
completed
```

On the Human-facing side:

```text
04-AI/50-Review
        ↓
04-AI/60-Execution
        ↓
04-AI/70-Transport
        ↓
04-AI/80-Completed
```

## Rejecting a Review

Set `review_request` to `reject`.

Review Intake creates an authoritative Reject Review. The canonical Knowledge transport path is not run.

After reconciliation, the dedicated cleanup transport deletes that case's pre-terminal Human-facing projections.

Private lifecycle and audit artifacts remain available for diagnosis and audit.

## Completed cleanup

For an approved case, terminal cleanup does not delete the Completed projection.

Cleanup waits until the exact `80-Completed` projection has a successful immutable projection-result artifact. Only then may the cleanup service delete:

```text
00-Input
10-Context
20-Generation
30-Validation
40-Evaluation
50-Review
60-Execution
70-Transport
```

The final state is therefore approximately:

```text
04-AI/80-Completed/<case>.md
```

This prevents an old Review projection from continuing to make a completed case look like it is waiting for Human action.

## AI Hub

ObsidianCore owns the Human UI under:

```text
98-System/02-embed/hub/ai-hub.md
```

The Hub groups the latest projection for each case into:

- Processing;
- Review;
- Delivery;
- Completed;
- Failed.

Stage order is authoritative for the Human-facing current view. For example, an `80-Completed` projection wins over an older-stage `50-Review` projection even if filesystem timestamps are unusual.

The current Core implementation reads canonical `04-AI` projections only.

## Checking production status

Check the aggregate scheduler projection:

```bash
/opt/obsidian-automation/venv/bin/obsidian-pre-review-status \
  --status-file /var/lib/obsidian-ai/state/02-Orchestration/status/pre-review-status.json \
  --json
```

Check the main recurrence timer:

```bash
systemctl status obsidian-pre-review.timer --no-pager
```

Check the post-review services:

```bash
systemctl show \
  obsidian-ai-review-intake.service \
  obsidian-ai-post-review-executor-prepare.service \
  obsidian-ai-post-review-transport.service \
  obsidian-ai-post-review-executor-finalize.service \
  obsidian-ai-post-review-projection-sync.service \
  obsidian-ai-post-review-reconcile.service \
  obsidian-ai-human-projection-cleanup-sync.service \
  -p LoadState -p ActiveState -p SubState -p Result
```

These are mostly oneshot services, so `inactive/dead` between cycles is normal. Use fresh journal/service execution evidence when diagnosing a specific cycle; an old `Result=success` alone does not prove that new work ran.

## Verifying one approved case

For production acceptance, choose one Review case and record its `ai_case_id`.

Before approval, confirm:

```text
04-AI/50-Review/<case>.md
```

After approval and a successful cycle, expect:

```text
04-AI/60-Execution/<case>.md
04-AI/70-Transport/<case>.md
04-AI/80-Completed/<case>.md
```

After terminal cleanup, expect the first two and the older pre-review stages to be absent, while:

```text
04-AI/80-Completed/<case>.md
```

remains.

Also confirm the canonical Knowledge Note exists at the target path bound in the case and that a corresponding private Receipt exists.

## Projection failures versus canonical execution

Human-facing projection is deliberately non-authoritative.

A projection emission failure does not retroactively invalidate a correctly approved and verified canonical Knowledge write. Post-review dispatch records projection errors separately and later cycles can reconstruct missing read-model projections from durable authoritative artifacts.

Conversely, the existence of a projection is never sufficient proof that a canonical mutation was authorized or completed. Authoritative evidence remains in Validation, Review, Execution, Transport, and Receipt artifacts.

## Projection root

Human-facing projection uses `04-AI` as its sole runtime root.

## Next references

- [Installation](installation.md)
- [Human-facing AI lifecycle projection](human-ai-projection.md)
- [Pre-review production rollout and acceptance](pre-review-production.md)
- [Production authority topology](ai-production-authority.md)
