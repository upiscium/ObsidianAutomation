"""Explicit, non-production transport-timeout retry contract for Issue #288."""
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from obsidian_automation.github_daily_summary import (
    GitHubDailySummaryError,
    SummaryRetryPolicy,
    _invoke_model_stage,
)
from obsidian_automation.ollama_generator import OllamaProviderError
from obsidian_automation.openai_compatible import OpenAICompatibleProviderError


CONTEXT = SimpleNamespace(stage="partial", batch_index=6, batch_count=16)
PROMPT = object()


def _provider_timeout():
    try:
        raise TimeoutError("SECRET: never print underlying transport exception")
    except TimeoutError as exc:
        raise OllamaProviderError("Ollama HTTP request failed") from exc


def test_retry_two_timeouts_then_success_with_bounded_backoff(monkeypatch):
    calls = 0
    sleeps = []
    monkeypatch.setattr(
        "obsidian_automation.github_daily_summary.time.sleep", sleeps.append
    )
    expected = object()

    def infer(_prompt, _context):
        nonlocal calls
        calls += 1
        if calls < 3:
            _provider_timeout()
        return expected

    result = _invoke_model_stage(
        infer, PROMPT, CONTEXT,
        retry_policy=SummaryRetryPolicy(max_transient_retries=2, backoff_seconds=0.5),
    )
    assert result is expected
    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_timeout_exhaustion_fails_closed_with_bounded_metadata(monkeypatch):
    monkeypatch.setattr("obsidian_automation.github_daily_summary.time.sleep", lambda _: None)
    calls = 0

    def fail(_prompt, _context):
        nonlocal calls
        calls += 1
        _provider_timeout()

    with pytest.raises(
        GitHubDailySummaryError,
        match=r"partial batch 7/16: provider=OllamaProviderError, cause=TimeoutError, attempts=2",
    ) as info:
        _invoke_model_stage(
            fail, PROMPT, CONTEXT,
            retry_policy=SummaryRetryPolicy(max_transient_retries=1),
        )
    assert calls == 2
    assert "SECRET" not in str(info.value)


@pytest.mark.parametrize(
    "exc",
    [
        OllamaProviderError("Ollama model alias digest changed after inference"),
        OllamaProviderError("Ollama HTTP request failed with status 503"),
        OllamaProviderError("Ollama Daily summary response model does not match resolved model"),
        OpenAICompatibleProviderError("OpenAI-compatible response schema invalid"),
    ],
)
def test_non_timeout_failure_never_retried(exc):
    calls = 0

    def fail(_prompt, _context):
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(GitHubDailySummaryError, match="attempts=1"):
        _invoke_model_stage(
            fail, PROMPT, CONTEXT,
            retry_policy=SummaryRetryPolicy(max_transient_retries=2),
        )
    assert calls == 1


def test_urllib_timeout_reason_is_retryable_without_retrying_other_url_errors():
    calls = 0

    def fail_once(_prompt, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                raise URLError(TimeoutError("socket timed out"))
            except URLError as exc:
                raise OllamaProviderError("Ollama HTTP request failed") from exc
        return object()

    _invoke_model_stage(
        fail_once, PROMPT, CONTEXT,
        retry_policy=SummaryRetryPolicy(max_transient_retries=1),
    )
    assert calls == 2


def test_deadline_without_cause_is_retryable_but_garbage_does_not_retry():
    calls = 0

    def fail_once(_prompt, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OllamaProviderError("Ollama response read exceeded request timeout")
        return object()

    _invoke_model_stage(
        fail_once, PROMPT, CONTEXT,
        retry_policy=SummaryRetryPolicy(max_transient_retries=1),
    )
    assert calls == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_transient_retries": True},
        {"max_transient_retries": -1},
        {"max_transient_retries": 3},
        {"max_transient_retries": 0.5},
        {"backoff_seconds": -0.1},
        {"backoff_seconds": float("inf")},
        {"backoff_seconds": float("nan")},
        {"backoff_seconds": 31.0},
        {"backoff_seconds": True},
    ],
)
def test_retry_policy_rejects_invalid_unbounded_inputs(kwargs):
    with pytest.raises(GitHubDailySummaryError):
        SummaryRetryPolicy(**kwargs)


def test_default_policy_does_not_retry():
    calls = 0

    def fail(_prompt, _context):
        nonlocal calls
        calls += 1
        _provider_timeout()

    with pytest.raises(GitHubDailySummaryError, match="attempts=1"):
        _invoke_model_stage(fail, PROMPT, CONTEXT)
    assert calls == 1


def test_unbound_raw_timeout_is_bounded_without_leaking_error_text():
    calls = 0
    def fail(_prompt, _context):
        nonlocal calls
        calls += 1
        raise TimeoutError("SECRET model text")
    with pytest.raises(GitHubDailySummaryError, match="attempts=2") as err:
        _invoke_model_stage(
            fail, PROMPT, CONTEXT,
            retry_policy=SummaryRetryPolicy(max_transient_retries=1),
        )
    assert calls == 2
    assert "SECRET" not in str(err.value)


def test_invalid_public_pipeline_policy_precedes_any_filesystem_access(tmp_path):
    from obsidian_automation.github_daily_summary import run_pipeline

    missing_root = tmp_path / "no-state-root"
    with pytest.raises(GitHubDailySummaryError, match="invalid Daily Summary retry policy"):
        run_pipeline(
            evidence_path=tmp_path / "missing-evidence.json",
            state_root=missing_root,
            infer=lambda *_: pytest.fail("must not invoke model"),
            implementation_revision="a" * 40,
            retry_policy=object(),
        )
    assert not missing_root.exists()


def test_invalid_production_api_policy_precedes_provider_preflight(tmp_path, monkeypatch):
    from obsidian_automation import github_daily_production as production

    contacted = []
    monkeypatch.setattr(
        production, "ollama_infer",
        lambda **_: contacted.append(True),
    )
    with pytest.raises(GitHubDailySummaryError, match="invalid Daily Summary retry policy"):
        production.summarize_pending(
            root=tmp_path / "missing-daily",
            provider="ollama",
            base_url="https://provider.invalid",
            model="model",
            implementation_revision="a" * 40,
            retry_policy=object(),
        )
    assert contacted == []


def test_invalid_internal_stage_policy_precedes_context_cas(tmp_path):
    from obsidian_automation.github_daily_summary import _run_claim_stage

    with pytest.raises(GitHubDailySummaryError, match="invalid Daily Summary retry policy"):
        _run_claim_stage(
            tmp_path,
            bundle=None,
            contexts=(),
            infer=lambda *_: pytest.fail("must not infer"),
            implementation_revision="a" * 40,
            retry_policy=object(),
        )
    assert list(tmp_path.iterdir()) == []
