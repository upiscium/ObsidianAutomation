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

PROMPT_VERSION = {
    DEEP_KNOWLEDGE: "deep-knowledge-generator-v2",
    IDEA_DISCOVERY: "idea-discovery-generator-v0",
    PROJECT_ADOPTION: "project-adoption-generator-v0",
}

OBJECTIVE_OPENAI_ADAPTER_VERSION = "openai-semantic-objective-json-schema-v0"
OBJECTIVE_OLLAMA_ADAPTER_VERSION = "ollama-semantic-objective-json-schema-v0"
