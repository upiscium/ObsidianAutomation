# Ollama Generator Adapter v0

## Purpose

This adapter is the first networked Generator implementation for Phase 2. It connects the existing immutable Context Bundle and Generator contracts to an operator-controlled Ollama endpoint without changing Validator, Human review, Executor, Sync Transport, or canonical-write authority.

```text
05-Context/<context_sha>.context.json
        ↓ exact load
Prompt Contract v0
        ↓
Ollama /api/chat
        ↓ strict semantic JSON
Generator Output Contract v0
        ↓ deterministic assembler
00-Untrusted/<proposal_sha>.proposal.json
        ↓
00-Untrusted/<generation_sha>.generation.json
        ↓
Validator
```

## Provider API

The adapter uses the native Ollama API:

- `GET /api/tags` to resolve the requested model to an installed canonical identifier and immutable model digest;
- `POST /api/chat` with `stream=false`;
- the Generator JSON Schema is passed through Ollama's `format` field;
- `think=false` is fixed by adapter v0;
- inference options default to `{ "temperature": 0 }` and may be overridden by an explicit JSON options file.

The model digest returned by `/api/tags`, not merely the mutable tag string, is bound to `GenerationRecord.model.revision`.

## Network trust boundary

Selected Context Bundle bytes are private data and become visible to the configured Ollama endpoint. The endpoint is therefore an explicit data recipient and must be operator-controlled.

Adapter v0 applies the following transport constraints:

- remote endpoints require HTTPS;
- plain HTTP is accepted only for loopback (`localhost`, `127.0.0.0/8`, `::1`);
- credentials may not be embedded in the URL;
- endpoint URLs may not contain a path, query, or fragment;
- environment HTTP/HTTPS proxy settings are ignored;
- HTTP redirects are not followed;
- normal Python TLS certificate validation remains enabled;
- provider responses are size-bounded;
- raw prompts and provider responses are not persisted by this adapter.

These controls reduce accidental Context disclosure. They do not prove that the configured Ollama host itself is trustworthy.

## Model resolution

The CLI accepts either a complete installed identifier such as:

```text
gemma3:latest
```

or an implicit `:latest` alias such as:

```text
gemma3
```

Resolution fails closed when:

- no installed model matches;
- the match is ambiguous;
- `/api/tags` does not contain a lowercase 64-character SHA-256 digest.

The canonical identifier resolved from `/api/tags` is then used for `/api/chat`. The chat response must report the same model identifier.

## Provider request

The semantic request is structurally equivalent to:

```json
{
  "model": "<resolved model>",
  "messages": [
    {"role": "system", "content": "<Generator Prompt Contract system prompt>"},
    {"role": "user", "content": "<exact deterministic Context payload>"}
  ],
  "stream": false,
  "think": false,
  "format": {"...": "Generator Output JSON Schema"},
  "options": {"temperature": 0}
}
```

The adapter does not accept free-form provider output. `message.content` must still pass `parse_generator_output()` after Ollama structured-output enforcement.

## Provenance

A successful generation records at least:

```text
context_sha256
proposal_sha256
implementation_revision
prompt_template_version
prompt_template_sha256
model.provider      = ollama
model.identifier    = resolved installed identifier
model.revision      = /api/tags digest
model_config.adapter_version = ollama-chat-structured-v0
model_config.think           = false
model_config.options         = exact inference options
```

The Generation Record remains untrusted audit provenance because it is written by the Generator identity. It is not a Validator attestation.

## CLI

After installation:

```bash
obsidian-knowledge-generate \
  --ai-root /var/lib/obsidian-ai/state \
  --context-sha256 <CONTEXT_SHA> \
  --ollama-base-url https://ollama.example.internal:11434 \
  --model gemma3 \
  --implementation-revision <DEPLOYED_OBSIDIAN_AUTOMATION_COMMIT>
```

Optional provider options can be supplied as a strict JSON object:

```json
{
  "temperature": 0,
  "num_ctx": 8192
}
```

and passed with:

```bash
--options-file /etc/obsidian-ai/ollama-options.json
```

The options file must not contain provider credentials or other secrets. Adapter v0 does not implement provider authentication headers.

## Failure semantics

No proposal is persisted when model resolution, network transport, structured output parsing, or semantic-output validation fails.

After a valid semantic output is obtained, the proposal is persisted first because the Generation Record contract requires an existing exact `proposal_sha256` binding. If a later local persistence failure occurs, an orphaned artifact may remain in `00-Untrusted`; this grants no authority and can be diagnosed by absence of a corresponding Generation Record / Validation record.

## Production acceptance

Before enabling real automatic generation:

1. deploy one immutable ObsidianAutomation revision;
2. verify the Generator still cannot read canonical Vault or `04-Index` and cannot write `05-Context` or later authority stages;
3. verify the configured Ollama endpoint is operator-controlled and TLS validation succeeds;
4. execute a disposable Context -> Generator local E2E;
5. inspect generated proposal bytes and Generation Record;
6. run the existing Validator independently on the proposal;
7. do not enable automatic Human approval.

## Out of scope

- Ollama authentication or API keys;
- cloud model providers;
- redirect/proxy support;
- raw request/response archival;
- automatic retries;
- Evaluator LLM;
- factual-correctness guarantees;
- automatic Human approval;
- update/merge/delete/rename canonical mutations.
