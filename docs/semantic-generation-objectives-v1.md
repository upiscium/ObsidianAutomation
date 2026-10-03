# Semantic Generation Objectives v1

## Purpose

Semantic Generation Objectives v1 implements Semantic Planner Phase F / issue #203.

It separates which sources are selected from what kind of candidate the Generator should produce.
Selection remains Reader-owned. Objective execution receives only an immutable Reader-prepared Objective Context. Generator still has no direct Vault, Semantic Index, or Selection-store access.

## Artifact flow

~~~text
Semantic Selection Record
        |
        | Reader verifies exact selected chunks
        v
05-Context/<sha>.objective-context.json
        | read-only Generator boundary
        v
Generator + objective-specific JSON Schema
        |
        +--> 00-Untrusted/<sha>.objective-candidate.json
        +--> 00-Untrusted/<sha>.objective-generation.json
        +--> 16-Human-Projection/generator/<sha>.projection.json
                 |
                 v
          04-AI/20-Generation/<case>.md
~~~

The Objective Context binds the objective policy, candidate kind, exact Semantic Selection SHA, selection policy, exact Semantic Index and Corpus identities, exact selected chunk/source identities, and exact bounded selected chunk bytes.

## Objective and Selection independence

The initial compatibility matrix is many-to-many:

~~~text
deep-knowledge-v1
  <- focus / project-distill / timeline / bridge / gap / idea-development

idea-discovery-v0
  <- focus / project-distill / timeline / bridge / gap

project-adoption-proposal-v0
  <- idea-development
~~~

The Project adoption objective is narrower because its output contract requires an exact active Idea anchor and selected Project Entry identities.

## deep-knowledge-v1

Candidate kind: knowledge_candidate.

The normal Knowledge output fields remain title, category, source_type and body. Before provider generation, Reader applies a deterministic evidence-sufficiency gate over substantive selected bytes. Deep Knowledge requires at least two substantive sources (32 bytes each) and at least 160 substantive bytes in total; structural Markdown does not count. If this gate fails, Planner records a bounded `skipped_evidence` result and creates no durable job. The prompt asks for one narrow, self-contained reusable note and, when supported by evidence, the central idea, mechanism, assumptions, constraints, trade-offs and concrete implications. Unsupported padding is prohibited.

`deep-knowledge-generator-v6` keeps the v4 deterministic Reader admission boundary, Knowledge-only provider schema and v5 reusable-synthesis objective, then requires epistemic-status preservation. Generator must not turn a research question, hypothesis, prediction, proposed evaluation, assumption, limitation or open question into an observed result or established relationship. Causal/result verbs and titles must match the strongest selected evidence, and conditional fallback conclusions must retain their dependencies. Historical v2/v3 structured `{status: no_candidate, reason: insufficient_evidence}` artifacts remain parseable; exact v2-v5 prompt identities remain supported for immutable provenance.

Phase F stores this as an Objective Candidate rather than the existing create-note proposal. This prevents the new objective system from silently entering the canonical Knowledge execution path before the selection/objective integration is explicitly reviewed.

## idea-discovery-v0

Candidate kind: idea_candidate.

Output fields:

~~~text
title
summary
rationale
supporting_evidence[]
uncertainties[]
~~~

The model cannot choose or emit canonical Workspace, Project relation, Idea status, canonical file path, or save/adoption action. Those fields are intentionally absent from the provider schema. Human/Core action owns the later save.

## project-adoption-proposal-v0

Candidate kind: project_adoption_proposal.

Output fields:

~~~text
idea_path
proposals[]:
  project_path
  fit_rationale
  supporting_evidence[]
  risks_conflicts[]
  missing_information[]
~~~

The Objective Context must contain an exact selected Idea anchor and at least one selected Project Entry. The provider schema fixes idea_path to the selected Idea and restricts project_path to an enum of selected Project Entry paths. Deterministic parsing validates the same allowlist again after provider output.

The output never changes Idea.project, Idea.workspace, Idea.status, or Project content/status. Human/Core action owns any later adoption.

## Prompt and output identity

Prompt versions:

~~~text
deep-knowledge-generator-v2
idea-discovery-generator-v0
project-adoption-generator-v0
~~~

Generation provenance binds the objective policy, prompt version/hash, exact Objective Context SHA, Selection SHA, Semantic Index SHA, provider/model identity and model configuration.

Candidate schema: schemas/semantic-objective-candidate-v1.schema.json.

Runtime validation is stricter where Context-dependent constraints are required, especially the Project allowlist.

## Provider adapters

Phase F supports both existing Generator provider families: OpenAI-compatible structured output and Ollama structured output.

Both adapters load the exact Objective Context, build the objective-specific prompt/schema, perform one bounded provider call, normalize only CRLF representation differences, strictly parse the output, persist a content-addressed Objective Candidate, and persist exact generation provenance. Ollama continues to bind the installed model digest.

## Human-facing projection

Semantic Objective generations reuse 04-AI/20-Generation/<case>.md but explicitly bind:

~~~text
objective_policy
candidate_kind
selection_sha256
semantic_index_sha256
objective_context_sha256
objective_candidate_sha256
~~~

Generated candidate bytes are rendered inside an inert JSON code fence. Idea candidates state that Human/Core save is required; Project adoption proposals state that Human/Core adoption is required; Deep Knowledge projections explicitly create nothing canonical.

## CLI

Reader prepares an Objective Context:

~~~bash
sudo -u obsidian-ai-reader \
  obsidian-semantic-objective-context \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --selection-sha <semantic-selection-sha256> \
  --objective deep-knowledge-v1
~~~

Generator performs objective-specific inference:

~~~bash
sudo -u obsidian-ai-generator \
  obsidian-semantic-objective-generate \
  --provider ollama \
  --ai-root /var/lib/obsidian-ai/state \
  --objective-context-sha <objective-context-sha256> \
  --base-url http://127.0.0.1:11434 \
  --model <model> \
  --implementation-revision <exact-reviewed-sha>
~~~

Use --provider openai-compatible for an OpenAI-compatible endpoint and the existing optional OPENAI_API_KEY environment binding.

When Human projection is enabled, the generation CLI also creates the Generator-owned Projection Request.

## Rollout boundary

Phase F does not yet switch the production Input Planner to semantic generation. Existing automatic generation continues to use coverage-shuffle-v0 / random-set-v0 plus synthesize-v0.

The new Objective path is explicit/operator-driven until the integration step binds Semantic Selection + Generation Objective + exact Objective Context + durable job provenance while preserving lifecycle gates.

Non-Knowledge candidates must never be routed through the canonical Knowledge mutation executor.

## Non-goals

Phase F does not grant Generator Vault/index access, automatically create Knowledge, save Ideas, adopt Ideas into Projects, reuse Knowledge Validator/Executor for non-Knowledge candidates, change Evaluator Knowledge BM25, or change Human approval authority.
