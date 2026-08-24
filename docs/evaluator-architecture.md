# Evaluator Architecture v0

## Purpose

The Evaluator is an advisory machine-assessment stage between deterministic Validation and Human Review. It exists because a proposal can be structurally valid and safe to create while still being low-value, redundant, weakly grounded, or inconsistent with existing Knowledge.

The production smoke that generated a second `Nextcloud/RemotelySave` note demonstrated this distinction directly:

```text
structurally valid
!=
worth creating
```

Evaluator v0 does not receive validation, approval, execution, transport, or canonical-write authority.

## Lifecycle

```text
Generator
  00-Untrusted proposal + generation provenance
        ↓
Validator
  10-Validation accepted mutation
        ↓
Validator
  12-Evaluation-Request
        ↓ read-only bridge
Reader / Indexer
  04-Index + canonical 11-Knowledge
        ↓
  14-Evaluation-Context
        ↓ read-only bridge
Evaluator
  read original 05-Context
  read proposal/generation provenance
  read accepted Validation
  read 14-Evaluation-Context
        ↓
  15-Evaluation
        ↓
Human Review
```

## Why `12-Evaluation-Request` exists

Reader must not gain direct read access to `00-Untrusted` or `10-Validation` merely to construct duplicate candidates. Validator already has authority to read the proposal and accepted mutation, so it deterministically projects only the minimum retrieval input into an immutable Evaluation Request.

The request binds:

- exact proposal SHA-256;
- exact accepted mutation SHA-256;
- target path;
- a bounded deterministic lexical retrieval query derived from the validated note title, headings, and body prefix.

Reader may read this request but cannot write it.

## Why Evaluation Context is `14-`, not `06-`

Evaluation candidates depend on the accepted mutation rather than only the original generation query. Therefore evaluation retrieval logically occurs after Validation. Numbering the stage before Validation would misrepresent the lifecycle and would encourage Reader to consume unvalidated Generator output.

## Evaluation candidate retrieval v0

Policy:

```text
bm25-topk-recall-v0
```

The Reader uses the current deterministic Knowledge Index and the Evaluation Request query, ranks active Knowledge notes with the existing BM25 ranker, and retains up to five positive-score candidates subject to the existing 512 KiB aggregate Context budget.

Unlike production generation retrieval, Evaluation candidate retrieval deliberately does not apply the `coverage >= 0.2` or relative `0.8` noise gates. Duplicate/contradiction detection is recall-oriented: extra lexical candidates are acceptable because the Evaluator can reject them, while silently omitting a near-duplicate is more damaging.

The Evaluation Context contains exact Markdown bytes and content SHA-256 values for each candidate. It is non-authoritative derived state.

## Evaluator assessment contract

Evaluation Record v0 binds:

- proposal SHA-256;
- accepted mutation SHA-256;
- generation record SHA-256;
- evaluation-context SHA-256;
- evaluator implementation revision;
- evaluator prompt version/SHA;
- model provider, identifier, revision and model config;
- evaluation timestamp;
- advisory assessment.

Assessment dimensions:

```text
groundedness:
  pass | concern | unknown

redundancy:
  none | possible | likely

consistency:
  pass | concern | unknown

recommendation:
  proceed | manual_review | do_not_proceed
```

`findings` contains bounded human-readable reasons.

## Authority semantics

An Evaluation Record is advisory machine assessment. It is not deterministic validation and it is not Human approval.

```text
Evaluation
!= Validation
Evaluation
!= Human approval
Evaluation recommendation
!= execution authority
```

A future workflow may require an Evaluation artifact to exist before presenting a proposal for Human Review. That is a workflow-completeness rule, not a transfer of approval authority to the Evaluator.

The Executor remains bound only to deterministic Validation plus exact Human approval. It does not consume Evaluator recommendations as canonical-write authorization.

## Linux identity

New identity:

```text
obsidian-ai-evaluator
```

Required access:

```text
read:
  00-Untrusted
  05-Context
  10-Validation
  14-Evaluation-Context
  15-Evaluation

write:
  15-Evaluation

no direct access:
  canonical Vault / 11-Knowledge
  04-Index
  12-Evaluation-Request
  20-Review
  24-Locks
  25-Execution
  27-Transport
  30-Receipts
  Nextcloud writer credential
```

Additional stage writers/readers:

```text
12-Evaluation-Request
  writer: Validator
  reader: Reader

14-Evaluation-Context
  writer: Reader
  reader: Evaluator

15-Evaluation
  writer: Evaluator
  reader: Human reviewer
```

## Failure isolation

The separation prevents several undesirable shortcuts:

- Evaluator cannot scan the canonical Vault directly; candidate selection remains Reader authority.
- Reader cannot inspect the proposal or Validation; it receives only the bounded retrieval request.
- Validator cannot manufacture Evaluation Context or Evaluation results.
- Evaluator cannot alter Validation or Human Review.
- Human reviewer cannot rewrite machine-produced Evaluation artifacts.
- Executor and Sync do not need Evaluation write access and cannot forge evaluator output.

## Scope of this architecture PR

Included:

- Evaluation Request contract and immutable storage;
- recall-biased deterministic Evaluation Context retrieval;
- Evaluation Record contract and cross-artifact hash binding;
- `obsidian-ai-evaluator` authority topology;
- POSIX ACL fixture and negative/positive authority gates;
- CLI for Evaluation Request and Evaluation Context creation.

Not included:

- Evaluator LLM prompt;
- Ollama evaluator adapter;
- automatic interpretation of evaluator recommendation;
- automatic Human approval/rejection;
- semantic/vector duplicate retrieval;
- update/merge canonical mutation support.

The next stage is to add the Evaluator prompt/output contract and then connect the Evaluator to Ollama without changing the authority topology defined here.
