"""B1-lite phase 1: one text -> one proposal note. Fake LLM, local files only."""
import json
import os
import stat
import zipfile

import pytest

from obsidian_automation import knowledge_proposal as kp
from obsidian_automation.knowledge_proposal import (
    ProposalError, SourceText, check_active_content, propose, write_proposal,
)
from obsidian_automation.openai_compatible import OpenAICompatibleProviderError

BASE = "https://llm.example.invalid/v1"
MODEL = "qwen-test"
SOURCE = SourceText("private-claude", "[user]\nHow does X work?\n\n[assistant]\nX works by Y.")


def llm(body="# Heading\n\nX works by Y.\n", **overrides):
    output = {"title": "How X works", "category": "explanation",
              "source_type": "self", "body": body, **overrides}
    calls = []

    def transport(base_url, *, method, path, payload, timeout, api_key=None):
        calls.append(payload)
        return {"model": payload["model"],
                "choices": [{"message": {"role": "assistant",
                                         "content": json.dumps(output)}}]}
    transport.calls = calls
    return transport


def run(tmp_path, transport, source=SOURCE):
    return propose(tmp_path / "ai", source, base_url=BASE, model=MODEL,
                   transport=transport)


def test_proposal_is_a_proposal_under_04_ai_with_the_approval_embed(tmp_path):
    transport = llm()
    proposal = run(tmp_path, transport)
    text = proposal.content.decode("utf-8")
    assert proposal.target_path == "04-AI/Proposals/How X works.md"
    front = text.split("---\n")[1]
    assert "type: knowledge-proposal" in front and "status: proposed" in front
    assert "category: explanation" in front and "source_type: self" in front
    assert "type: knowledge-note" not in front and "status: active" not in front
    assert text.count("[[knowledge-proposal-meta]]") == 1
    assert "[[knowledge-meta]]" not in text
    assert "X works by Y." in text
    last = text.rstrip("\n").splitlines()[-1]
    assert last.startswith("<!-- oa-proposal: ") and last.endswith(" -->")
    record = json.loads(last[len("<!-- oa-proposal: "):-len(" -->")])
    assert record["scope"] == "private-claude" and record["model"] == MODEL
    # the source text really reached the model, once
    assert len(transport.calls) == 1
    assert "How does X work?" in json.dumps(transport.calls[0])


def test_write_is_create_only_and_private(tmp_path):
    proposal = run(tmp_path, llm())
    out = tmp_path / "out"
    path = write_proposal(out, proposal)
    assert path.read_bytes() == proposal.content
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    with pytest.raises(ProposalError, match="proposal_exists"):
        write_proposal(out, proposal)
    assert path.read_bytes() == proposal.content  # untouched


@pytest.mark.parametrize("body", [
    "text\n```meta-bind-button\nlabel: x\n```\n",
    "text\n```dataviewjs\ndv.pages()\n```\n",
    "text\n```templater\n<% tp.file.title %>\n```\n",
    "text <% tp.system.prompt() %>\n",
    "press `BUTTON[promote-to-knowledge]`\n",
    "set `INPUT[text:status]`\n",
    "[go](obsidian://open?vault=x)\n",
    "<script>alert(1)</script>\n",
    "[x](javascript:alert(1))\n",
])
def test_active_content_in_the_generated_body_is_refused(tmp_path, body):
    with pytest.raises(ProposalError, match="active_content_in_generated_note"):
        run(tmp_path, llm(body=body))


def test_plain_code_fences_are_allowed():
    check_active_content("```js\nconsole.log(1)\n```\n```python\nprint(1)\n```\n")


@pytest.mark.parametrize("source,code", [
    (SourceText("private-claude", "   "), "empty_source"),
    (SourceText("private-claude", "x" * (128 * 1024 + 1)), "source_too_large"),
    (SourceText("Bad Scope", "text"), "invalid_scope"),
])
def test_source_guards_run_before_any_model_call(tmp_path, source, code):
    transport = llm()
    with pytest.raises(ProposalError, match=code):
        run(tmp_path, transport, source)
    assert transport.calls == []


def test_provider_failures_do_not_leak_details(tmp_path):
    def broken(*args, **kwargs):
        raise OpenAICompatibleProviderError("secret-host.internal refused")
    with pytest.raises(ProposalError) as raised:
        run(tmp_path, broken)
    assert str(raised.value) == "generation_failed"
    assert "secret-host" not in repr(raised.value)

    def wrong_model(base_url, *, method, path, payload, timeout, api_key=None):
        return {"model": "other", "choices": [{"message": {
            "role": "assistant", "content": "{}"}}]}
    with pytest.raises(ProposalError, match="generation_failed"):
        run(tmp_path, wrong_model)


def test_invalid_generator_output_is_refused(tmp_path):
    with pytest.raises(ProposalError, match="generation_failed"):
        run(tmp_path, llm(category="not-a-category"))
    with pytest.raises(ProposalError, match="generation_failed"):
        run(tmp_path, llm(title="bad/title"))


def test_cli_list_shows_counts_only_and_rejects_bad_arguments(tmp_path, capsys):
    messages = [{"uuid": "m1", "sender": "human", "text": "SECRET-QUESTION",
                 "created_at": "2026-01-02T03:04:05Z"},
                {"uuid": "m2", "sender": "assistant", "text": "SECRET-ANSWER",
                 "created_at": "2026-01-02T03:04:07Z"}]
    conv = {"uuid": "c1", "name": "SECRET-TITLE", "chat_messages": messages}
    archive = tmp_path / "e.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("conversations.json", json.dumps([conv]))
    assert kp.main(["list", "--zip", str(archive), "--provider", "claude"]) == 0
    out = capsys.readouterr().out
    row = json.loads(out.strip())
    assert row == {"index": 0, "messages": 2, "bytes": len(
        "[user]\nSECRET-QUESTION\n\n[assistant]\nSECRET-ANSWER".encode()),
        "first_day": "2026-01-02", "fits": "ok"}
    assert "SECRET" not in out
    assert kp.main(["propose", "--zip", str(archive), "--provider", "claude",
                    "--index", "9"]) == 1
    assert "invalid_arguments" in capsys.readouterr().out


def test_structured_output_is_the_default_and_sends_response_format(tmp_path):
    transport = llm()
    run(tmp_path, transport)
    payload = transport.calls[0]
    assert payload["response_format"]["type"] == "json_schema"
    assert "The output JSON Schema is below" not in payload["messages"][0]["content"]


def test_a_server_that_rejects_response_format_gets_the_schema_in_the_prompt(tmp_path):
    """TSUMGI answers 400 unsupported_field to response_format: send none, prompt the schema."""
    transport = llm()
    proposal = propose(tmp_path / "ai", SOURCE, base_url=BASE, model=MODEL,
                       transport=transport, structured_output=False)
    payload = transport.calls[0]
    assert "response_format" not in payload
    system = payload["messages"][0]["content"]
    assert "The output JSON Schema is below" in system
    assert '"additionalProperties":false' in system and '"body"' in system
    assert payload["stream"] is False and payload["model"] == MODEL
    # the strict local parsing is still the gate, and the result is a normal proposal
    assert proposal.target_path == "04-AI/Proposals/How X works.md"


def test_the_generation_record_names_the_prompt_schema_adapter_when_no_response_format(tmp_path):
    from obsidian_automation.openai_generator import (
        ADAPTER_VERSION, ADAPTER_VERSION_PROMPT_SCHEMA, provider_model_config,
    )
    assert provider_model_config({})["adapter_version"] == ADAPTER_VERSION
    assert provider_model_config({}, structured_output=False)["adapter_version"] == \
        ADAPTER_VERSION_PROMPT_SCHEMA != ADAPTER_VERSION


def test_a_reply_that_breaks_the_schema_is_still_refused_without_response_format(tmp_path):
    transport = llm(category="not-a-category")
    with pytest.raises(ProposalError, match="generation_failed"):
        propose(tmp_path / "ai", SOURCE, base_url=BASE, model=MODEL,
                transport=transport, structured_output=False)


def test_a_lower_source_limit_refuses_a_conversation_before_any_request(tmp_path):
    transport = llm()
    big = SourceText("private-claude", "x" * 2000)
    with pytest.raises(ProposalError, match="source_too_large"):
        propose(tmp_path / "ai", big, base_url=BASE, model=MODEL, transport=transport,
                max_source_bytes=1024)
    assert transport.calls == []
    # the hard ceiling cannot be raised by the argument
    from obsidian_automation.knowledge_proposal import MAX_SOURCE_BYTES
    huge = SourceText("private-claude", "x" * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(ProposalError, match="source_too_large"):
        propose(tmp_path / "ai", huge, base_url=BASE, model=MODEL, transport=transport,
                max_source_bytes=MAX_SOURCE_BYTES * 4)
