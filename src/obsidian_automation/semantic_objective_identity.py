from __future__ import annotations


DEEP_KNOWLEDGE = "deep-knowledge-v1"
IDEA_DISCOVERY = "idea-discovery-v0"
PROJECT_ADOPTION = "project-adoption-proposal-v0"
OBJECTIVES = (DEEP_KNOWLEDGE, IDEA_DISCOVERY, PROJECT_ADOPTION)

CANDIDATE_KIND = {
    DEEP_KNOWLEDGE: "knowledge_candidate",
    IDEA_DISCOVERY: "idea_candidate",
    PROJECT_ADOPTION: "project_adoption_proposal",
}

DEEP_KNOWLEDGE_PROMPT_V2_VERSION = "deep-knowledge-generator-v2"
DEEP_KNOWLEDGE_PROMPT_V3_VERSION = "deep-knowledge-generator-v3"
DEEP_KNOWLEDGE_PROMPT_V4_VERSION = "deep-knowledge-generator-v4"
DEEP_KNOWLEDGE_PROMPT_VERSIONS = frozenset(
    {
        DEEP_KNOWLEDGE_PROMPT_V2_VERSION,
        DEEP_KNOWLEDGE_PROMPT_V3_VERSION,
        DEEP_KNOWLEDGE_PROMPT_V4_VERSION,
    }
)

PROMPT_VERSION = {
    DEEP_KNOWLEDGE: DEEP_KNOWLEDGE_PROMPT_V4_VERSION,
    IDEA_DISCOVERY: "idea-discovery-generator-v0",
    PROJECT_ADOPTION: "project-adoption-generator-v0",
}

OBJECTIVE_OPENAI_ADAPTER_VERSION = "openai-semantic-objective-json-schema-v0"
OBJECTIVE_OLLAMA_ADAPTER_VERSION = "ollama-semantic-objective-json-schema-v0"
