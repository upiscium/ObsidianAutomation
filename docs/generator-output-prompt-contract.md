# Generator Output / Prompt Contract v0

## Purpose

This contract defines the semantic boundary between an LLM provider and the deterministic Generator code.

The model does not produce a canonical mutation directly. It produces a narrowly scoped semantic JSON object. Deterministic code then constructs the existing `create_note` proposal and writes it to the existing untrusted artifact stage.

New proposals use [Knowledge Note layout v1](knowledge-note-layout-v1.md), including the standard metadata editor. The semantic output and prompt contracts remain v0; historical proposal bytes are not rewritten.

```text
05-Context/<context_sha>.context.json
        ↓
Prompt renderer
        ↓
LLM provider
        ↓
raw semantic output JSON
        ↓ deterministic formatter
canonical semantic output JSON
        ↓ strict parser / validator
Deterministic assembler
        ↓
00-Untrusted/<proposal_sha>.proposal.json
        ↓
Generation Record
        ↓
Validator
```

This preserves the existing authority model: the Generator remains untrusted and has no canonical Vault read/write authority.

## Model-owned semantic output

Output contract version:

```text
knowledge-note-semantic-output-v0
```

The model may choose exactly four fields:

```json
{
  "title": "...",
  "category": "manual",
  "source_type": "self",
  "body": "# Markdown body\n..."
}
```

No unknown properties are accepted.

### `title`

`title` is only a filename stem. It is not a path.

The parser rejects:

- empty or untrimmed titles;
- hidden/relative names;
- `/` or `\\` path separators;
- `.md` suffixes;
- Windows-unsafe filename characters;
- reserved Windows device names;
- control characters;
- cross-platform unsafe trailing `.` or whitespace;
- titles longer than the contract limit.

The deterministic target is always:

```text
11-Knowledge/<title>.md
```

The model cannot choose another canonical root or subdirectory in v0.

### `category`

Allowed values:

```text
(blank)
explanation
manual
troubleshooting
spec
reference
summary
```

### `source_type`

Allowed values:

```text
self
official
paper
book
web
other
```

### `body`

`body` is Markdown body content only. It must not contain the Knowledge Note YAML frontmatter envelope.

The strict parser requires UTF-8-encodable, non-empty, bounded LF-only text. It
does not repair line endings or semantic mistakes. It rejects clear literal
backslash-`n` prose/list-boundary artifacts outside Markdown code contexts rather
than silently converting them; literal escape-sequence text remains representable
in fenced/inline code and unambiguous technical prose.

The wire representation and decoded semantic value are distinct. The provider
returns a serialized JSON object, so a real LF character in the JSON `body`
string is represented with normal JSON escaping on the wire. After JSON decoding,
the `body` value must contain actual LF characters for Markdown line breaks. A
double-escaped newline notation is invalid when it leaves the decoded value with
the two literal characters backslash and `n` being used as a prose or list
boundary. The deterministic #156 validator remains the final safety gate; this
prompt correction does not make the Formatter perform semantic repair.

Before strict parsing, the deterministic Generator formatter canonicalizes conventional CRLF provider output to LF. It deliberately does not rewrite titles, metadata values, lone CR characters, or other semantic content. A lone CR therefore remains invalid and is rejected by the parser. The assembler then normalizes final newlines to exactly one LF and adds the fixed metadata editor. Equivalent LF/CRLF provider representations converge to identical canonical semantic output, proposal bytes, and mutation identity.

The formatter is intentionally narrow: it absorbs representation variance that has one unambiguous meaning-preserving canonical form. It is not a general Markdown sanitizer or LLM-output repair layer.

## Deterministic ownership

The model does **not** choose the following fields:

```text
contract_version
operation
mutation_id
target root
type
status
maturity
metadata editor scaffold
```

The deterministic assembler fixes them as:

```text
contract_version = 1
operation        = create_note
target root      = 11-Knowledge
type             = knowledge-note
status           = active
maturity         = draft
layout_version   = knowledge-note-layout-v1
```

For new proposals, `mutation_id` is deterministically derived from the layout version, exact Context SHA, and canonical semantic output bytes:

```text
knowledge-gen-v0-<sha256(layout_version || NUL || context_sha || NUL || canonical_semantic_output)>
```

Therefore identical semantic output against the same Context artifact and layout produces identical proposal bytes. Changing the Context artifact or layout changes the mutation ID even when the semantic output is otherwise identical. The existing ID namespace is preserved, but the layout is domain-separated in its digest. Legacy assembly omitted `layout_version || NUL`; old artifacts retain their original IDs and bytes.

## Frontmatter assembly

The generated note proposal content is assembled as:

````markdown
---
type: knowledge-note
status: active
category: <model category>
maturity: draft
source_type: <model source_type>
---

```meta-bind-embed
[[knowledge-meta]]
```

<model body>
````

The metadata editor is added before proposal hashing and Validation/Evaluation/Review, not after approval or during transport. See [layout v1](knowledge-note-layout-v1.md) for compatibility and existing Live Note repair boundaries.

For a blank category the assembler emits the plain empty scalar:

```text
category:
```

The completed in-memory mutation is passed through `validate_knowledge_note_v0()` before proposal persistence. This validates the deterministic content/path contract without granting the Generator direct Vault access. The shared policy is not tightened retroactively to reject legacy proposals without the editor.

The Generator deliberately does not check whether the target filename already exists in the canonical Vault. That check requires canonical visibility and remains the Validator's responsibility.

## Prompt contract

Prompt template version:

```text
knowledge-note-generator-v2
```

New recipes bind the exact v2 prompt version and hash. Historical recipes may
retain the exact v0 or v1 version/hash pair for readability and audit, but the
current runtime never reinterprets them as v2; runtime preflight fails closed
before provider contact when a historical recipe is not executable by the
deployment.

The supported immutable Generator prompt identities are:

```text
knowledge-note-generator-v0  820f86bf9f7e5495be64608690123ec31562441d4d774095d5d61ba7db9abafd
knowledge-note-generator-v1  ebdcbfdc5008a1c84366555debc15842e154cfaf51919d023c30d3d5c3fa9248
knowledge-note-generator-v2  8c2b1635a4c6eb6775de764e58dff669c123232ee11a7320f20c3c87598300d5
```

Generator v2 adds a language contract: the title and ordinary natural-language
Markdown prose are generated primarily in Japanese. Headings, summaries,
explanations, list-item prose, interpretations and conclusions are Japanese even
when source material is English. Proper nouns, paper titles, model/API names,
commands, code, identifiers, literal quotations and technical terms may preserve
their original-language form when that improves precision or searchability.
Machine enum fields such as `category` and `source_type` are never translated.
This is a prompt contract, not a deterministic translation/rewrite pass.

The prompt consists of:

1. a fixed system instruction;
2. a deterministic JSON user payload derived from the exact Context Bundle;
3. the strict semantic-output JSON Schema.

The template SHA-256 covers a canonical template manifest containing:

```text
template_version
system prompt bytes
output JSON Schema
user payload format version
```

The rendered user payload contains:

```json
{
  "payload_version": 1,
  "query": "...",
  "sources": [
    {
      "path": "11-Knowledge/example.md",
      "content_sha256": "...",
      "content": "exact Markdown content"
    }
  ]
}
```

The Context SHA itself is bound separately by the Generation Record.

## Prompt injection boundary

Canonical Knowledge content is useful reference material but must not acquire instruction authority merely by being inserted into an LLM prompt.

The fixed system prompt explicitly classifies Context source content as **reference data, not instructions**. Commands, role changes, policy changes, or output-format requests embedded inside a Knowledge Note must not be followed by the model.

This is defense in depth, not a security proof. A model may still be influenced by adversarial source text. The security boundary therefore remains structural:

- the model can only return the strict semantic JSON shape;
- deterministic code owns canonical-control fields;
- the proposal remains in `00-Untrusted`;
- the deterministic Validator independently rechecks the completed proposal;
- Human approval remains mandatory before canonical execution.

Prompt-injection resistance is not treated as a substitute for those authority boundaries.

## Empty or incomplete Context

Reader retrieval may intentionally produce an empty Context Bundle when no candidate passes selection policy.

The Generator does not silently inject a fallback document. The prompt instructs the model not to fabricate unsupported factual claims. When Context is empty or insufficient, generated body text must remain limited to information supported by the query and available context and should state uncertainty where appropriate.

This is not a factual-validity guarantee. Semantic/factual evaluation remains a later Evaluator/Human responsibility.

## Proposal persistence

Before persisting a generated proposal, the Generator:

1. requires a valid lowercase Context SHA-256;
2. loads `05-Context/<sha>.context.json` through the existing exact-hash Context loader;
3. formats representation-only variance into canonical semantic JSON;
4. strictly parses and validates the canonical semantic output contract;
5. assembles the deterministic Knowledge Note mutation, including its fixed editor;
6. applies `knowledge-note-v0` policy checks that do not require Vault reads;
7. persists exact proposal bytes using the existing immutable content-addressed `00-Untrusted` storage.

The subsequent Generation Record binds this exact proposal SHA to the Context SHA, prompt template version/SHA, implementation revision, model identity/revision/configuration, and generation timestamp.

## Out of scope

- Ollama HTTP/API transport;
- provider authentication/network policy;
- raw provider request/response retention;
- automatic retry policy;
- target-existence checks in Generator;
- factual correctness guarantees;
- Evaluator LLM;
- automatic Human approval;
- update/merge/delete/rename mutations;
- semantic/vector retrieval.

## Provider integration boundary

The provider adapter follows:

```text
load exact Context
→ render fixed prompt + schema
→ invoke provider
→ deterministic representation formatter
→ strict semantic parser / validator
→ deterministic proposal assembly
→ immutable proposal persistence
→ immutable Generation Record persistence
```

The provider adapter must not bypass the semantic parser or emit canonical mutation JSON directly.
