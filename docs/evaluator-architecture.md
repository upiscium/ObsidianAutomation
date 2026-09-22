# Evaluator Architecture v0

## Purpose

The Evaluator is an advisory machine-assessment stage between deterministic Validation and Human Review. It exists because a proposal can be structurally valid and safe to create while still being low-value, redundant, weakly grounded, or inconsistent with existing Knowledge.

The production smoke that generated a second `Nextcloud/RemotelySave` note demonstrated this distinction directly:

```text
structurally valid
!=
worth creating
```

The Evaluator does not receive validation, approval, execution, transport, or canonical-write authority.

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

Reader ranks the immutable Index without a lock, then acquires the host-local
mirror read-view lock for the current-index verification and candidate source
re-read. The lock is released before the immutable Evaluation Context is handed
to Evaluator and is never held during LLM inference. This prevents one local
rclone refresh from splitting the retrieval view without claiming remote Vault
freshness.

## Evaluator assessment contract

Current Evaluation Records use version 2 and bind:

- proposal SHA-256;
- accepted mutation SHA-256;
- generation record SHA-256;
- evaluation-context SHA-256;
- evaluator implementation revision;
- evaluator prompt version/SHA;
- model provider, identifier, revision and model config;
- evaluation timestamp;
- advisory assessment.

The current prompt/output identities are:

```text
output: knowledge-note-evaluator-output-v3
prompt: knowledge-note-evaluator-v4
SHA:    9411d74c10cd8c3450be6b79f12c644433862a4b292a26db7444d32606ddea3b
```

The historical prompt `knowledge-note-evaluator-v3` with SHA
`bf6265294a4b346f12d1951f594760c80221380ccee9993c6ab866b6b1eca937` remains
readable in recipes for audit. Current runtime preflight blocks that historical
recipe before provider contact. Unknown and cross-paired prompt version/hash
identities are rejected.

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

Consistency `concern` requires structured conflict evidence. The model-facing
shape omits the deterministic path:

```json
{
  "assessment": "concern",
  "findings": [{"detail": "..."}],
  "conflicts": [
    {
      "proposal_claim": "...",
      "candidate_claim": "...",
      "incompatibility": "..."
    }
  ]
}
```

Each conflict field is bounded to 1,000 characters and at most four conflicts
are accepted. Deterministic code binds `candidate_path` to the exact supplied
candidate after strict parsing. `pass` and `unknown` return an empty conflicts
array; `concern` requires a non-empty array. `unknown` means the pair is
insufficient or ambiguous. Different topic/scope,
missing framework/details, omissions, extra detail, formatting, and style are
not conflicts.

Findings are bounded to four per dimension in the provider output (16 in the
persisted assessment), with bounded detail strings. Evaluation Record v2 stores
the path-bound conflicts under `assessment.conflicts`:

```json
{
  "record_version": 2,
  "assessment": {
    "consistency": "concern",
    "conflicts": [
      {
        "candidate_path": "11-Knowledge/example.md",
        "proposal_claim": "...",
        "candidate_claim": "...",
        "incompatibility": "..."
      }
    ]
  }
}
```

Across candidates, deterministic aggregation selects the winning severity using
`none < possible < likely` for Redundancy and `pass < unknown < concern` for
Consistency. Only findings/conflicts at that winning severity survive bounded
deduplication; no candidates yield Redundancy `none` and Consistency `pass`.

Historical Evaluation Record v1 artifacts remain readable as legacy evidence;
their assessment shape has no `conflicts` member. The complete prompt, binding,
and aggregation contract is documented in
[Evaluator Prompt / Output Contract](evaluator-prompt-output-contract.md).

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
  24-Locks (including 24-Locks/read-view)
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

- automatic interpretation of evaluator recommendation;
- automatic Human approval/rejection;
- semantic/vector duplicate retrieval;
- update/merge canonical mutation support.

The prompt and provider adapter are current implementation details described in
[Evaluator Prompt / Output Contract](evaluator-prompt-output-contract.md) and
[Ollama Evaluator Adapter](ollama-evaluator-adapter.md); they do not change this
authority topology.
