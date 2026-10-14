"""C5: explicit nonproduction C4 inference; no filesystem, Writer or Timer.

Uses the existing Ollama transport with bounded source reconstruction and model
calls. Receipts record observations, not cryptographic execution attestation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Callable

from . import github_daily_witnessed_narrative as c4
from . import ollama_generator as provider
from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object

VERSION = 1
MAX_ENTITIES = 8
MAX_CALLS = 32
MAX_REQUEST_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 32 * 1024
MAX_RUN_SECONDS = 600
MAX_SOURCE_WORK_BYTES = 256 * 1024 * 1024
MAX_SOURCE_RECONSTRUCTIONS = 512
EDITORIAL_REMINDER_VERSION = "c5-disjoint-source-accounting-ja-v1"
EDITORIAL_REMINDER = """出力検証の補足（元証拠ではなく形式上の制約）:
pending_source_refs は、どの文の source_refs にも含めなかった番号だけを入れてください。
同じ番号を文の source_refs と pending_source_refs の両方に入れると出力全体が拒否されます。
例: 元イベントが source_ref=0 の1件で、その内容を文で引用した場合は
{"sentences":[{"text":"根拠のある日本語文。","source_refs":[0]}],"pending_source_refs":[]} です。
引用できない場合は sentences を空にし pending_source_refs に0を残してください。
この例の文章を流用せず、実際の元証拠と対象日の未完了事項に忠実に書いてください。"""
GROUND_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["verdict", "reason"], "properties": {
        "verdict": {"type": "string", "enum": ["supported", "unsupported"]},
        "reason": {"type": "string", "minLength": 1, "maxLength": c4.MAX_TEXT_CHARS}}}


class NarrativeRuntimeError(ValueError):
    """Source, runtime configuration or complete request cannot be admitted."""


class NarrativeDeadlineError(NarrativeRuntimeError):
    """No further HTTP request may begin under this run's elapsed budget."""


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _number(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise NarrativeRuntimeError("invalid bounded runtime duration")
    return float(value)


@dataclass(frozen=True)
class NarrativeCallResult:
    raw_response_bytes: bytes
    receipt_bytes: bytes


@dataclass(frozen=True)
class NarrativeAttempt:
    stage: str
    context_sha256: str
    state: str
    raw_response_bytes: bytes | None
    receipt_bytes: bytes | None


@dataclass(frozen=True)
class NarrativeRun:
    """Owned private results. Raw attempts are separate from metadata-only report."""
    canonical_bytes: bytes
    attempts: tuple[NarrativeAttempt, ...]
    narrative_replies: tuple[c4.NarrativeReply, ...]
    grounding_replies: tuple[c4.NarrativeGroundReply, ...]
    preview: c4.NarrativePreview

    def report(self):
        return json.loads(self.canonical_bytes)


def ollama_narrative_infer(*, base_url: str, model: str, options=None,
                            timeout: float = 60, transport=None):
    """Configure lazily; no network until explicitly called with C4 input bytes.

    Endpoint/options are trusted operator input, never derived from model text.
    A tags observation before/after each request detects observed model changes,
    not a malicious server or an atomic loaded-model identity guarantee.
    """
    try:
        root = provider._validated_base_url(base_url)
        requested = provider._validated_identifier(model, label="model")
        seconds = _number(timeout, 120)
        option_bytes = _json(provider._validated_options(options))
    except (provider.OllamaProviderError, ArtifactLifecycleError, ValueError, TypeError):
        raise NarrativeRuntimeError("invalid inference configuration") from None
    request = transport or provider._request_json
    pinned = None

    def infer(context_bytes: bytes, *, deadline: float | None = None) -> NarrativeCallResult:
        nonlocal pinned
        http_requests = []
        def bounded_request(*args, **kw):
            remaining = seconds if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise NarrativeDeadlineError("inference elapsed budget exhausted")
            kw["timeout"] = min(seconds, remaining)
            http_requests.append({"method": kw["method"], "path": kw["path"],
                                  "timeout_seconds": kw["timeout"]})
            return request(*args, **kw)
        if type(context_bytes) is not bytes or not 0 < len(context_bytes) <= c4.MAX_GROUND_CONTEXT_BYTES:
            raise NarrativeRuntimeError("invalid inference context bytes")
        ctx = _decode_json_object(context_bytes, label="C4 inference context")
        stage = ctx.get("stage")
        if stage == "github_daily_narrative_context_v4":
            prompt, version = c4.EDITORIAL_PROMPT, "github-daily-narrative-ja-v1"
            schema, limit = ctx.get("response_schema"), c4.MAX_REPLY_BYTES
            if type(schema) is not dict:
                raise NarrativeRuntimeError("editorial schema missing")
        elif stage == "github_daily_narrative_ground_context_v4":
            prompt, version = c4.GROUND_PROMPT, "github-daily-narrative-ground-ja-v1"
            schema, limit = GROUND_SCHEMA, c4.MAX_GROUND_REPLY_BYTES
        else:
            raise NarrativeRuntimeError("inference stage not admitted")
        if (ctx.get("record_version") != c4.VERSION or ctx.get("publication_authorized") is not False
                or ctx.get("system_prompt") != prompt or ctx.get("prompt_version") != version):
            raise NarrativeRuntimeError("C4 prompt identity changed")
        # Identity/source admission is the runner's responsibility. This low-
        # level adapter by itself never certifies externally supplied contexts.
        before = provider.resolve_ollama_model(root, requested, timeout=seconds, transport=bounded_request)
        if pinned is None:
            pinned = before
        if before.identifier != pinned.identifier or before.digest != pinned.digest:
            raise NarrativeRuntimeError("model revision changed before inference")
        payload = {"model": pinned.identifier, "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": context_bytes.decode("utf-8")}],
            "stream": False, "think": False, "format": schema,
            "options": json.loads(option_bytes)}
        reminder = EDITORIAL_REMINDER if stage == "github_daily_narrative_context_v4" else ""
        if reminder:
            payload["messages"].append({"role": "user", "content": reminder})
        # Match the reused transport's exact compact JSON wire payload (no newline).
        request_bytes = _json(payload).removesuffix(b"\n")
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise NarrativeRuntimeError("inference request exceeds byte budget")
        response = bounded_request(root, method="POST", path="/api/chat",
                           payload=json.loads(request_bytes), timeout=seconds)
        if (type(response) is not dict or response.get("done") is not True
                or response.get("model") != pinned.identifier
                or response.get("done_reason", "stop") != "stop"):
            raise NarrativeRuntimeError("model response incomplete or changed")
        message = response.get("message")
        if type(message) is not dict or message.get("role") != "assistant" or type(message.get("content")) is not str:
            raise NarrativeRuntimeError("model response envelope invalid")
        raw = message["content"].encode("utf-8")
        if not 0 < len(raw) <= limit:
            raise NarrativeRuntimeError("model response exceeds byte limit")
        after = provider.resolve_ollama_model(root, requested, timeout=seconds, transport=bounded_request)
        if after.identifier != pinned.identifier or after.digest != pinned.digest:
            raise NarrativeRuntimeError("model revision changed during inference")
        receipt = _json({"record_version": VERSION, "adapter": "c4-ollama-chat-v1",
            "stage": stage, "context_sha256": _sha(context_bytes),
            "request_sha256": _sha(request_bytes), "raw_response_sha256": _sha(raw),
            "prompt_version": version, "system_prompt_sha256": _sha(prompt.encode()),
            "runtime_instruction_version": EDITORIAL_REMINDER_VERSION if reminder else None,
            "runtime_instruction_sha256": _sha(reminder.encode()) if reminder else None,
            "response_schema_sha256": _sha(_json(schema)), "options_sha256": _sha(option_bytes),
            "model_provider": "ollama", "model_identifier": pinned.identifier,
            "observed_model_revision": pinned.digest,
            "config": {"options": json.loads(option_bytes), "stream": False,
                       "think": False, "configured_request_timeout_seconds": seconds},
            "http_requests": http_requests,
            "model_execution_attested": False, "publication_authorized": False})
        return NarrativeCallResult(raw, receipt)
    return infer


def _snapshot(partials, grounds, decisions):
    roll = c4.roll
    p, ps = roll._snapshot(partials, roll.PartialReply, "context_sha256",
                          roll.MAX_PARTIAL_REPLIES, roll.c1b.MAX_MODEL_REPLY_BYTES)
    g, gs = roll._snapshot(grounds, roll.GroundReply, "ground_context_sha256",
                          roll.MAX_GROUND_REPLIES, roll.c2a.MAX_GROUND_REPLY_BYTES)
    if ps + gs > roll.MAX_INPUT_BYTES:
        raise NarrativeRuntimeError("combined source reply limit exceeded")
    d = c4.c3._decisions(decisions)
    return (tuple(roll.PartialReply(k,v) for k,v in sorted(p.items())),
            tuple(roll.GroundReply(k,v) for k,v in sorted(g.items())),
            tuple(c4.c3.MaterialityDecision(*row) for row in d))


def run_witnessed_narrative_inference(capture_sha256, partial_replies, grounding_replies,
        materiality_decisions=(), *, infer: Callable[..., NarrativeCallResult],
        max_entities: int = 4, max_calls: int = 16, max_elapsed_seconds: float = 120,
        max_context_bytes: int = c4.roll.c1a.MAX_CONTEXT_BYTES) -> NarrativeRun:
    """Run editorial then NEW post-edit Grounding; retain failures as pending.

    Reconstructs C4 three times, never once per sentence. No automatic retries,
    silent subset selection, disk writes, model installation or publication.
    Deadline checked between calls; cannot interrupt an arbitrary callback.
    """
    if (type(max_entities) is not int or not 1 <= max_entities <= MAX_ENTITIES
            or type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS
            or not callable(infer)):
        raise NarrativeRuntimeError("invalid bounded runtime settings")
    seconds = _number(max_elapsed_seconds, MAX_RUN_SECONDS)
    started = time.monotonic()
    try:
        p,g,d = _snapshot(partial_replies, grounding_replies, materiality_decisions)
        planned = 1 + 5*len(p) + 3*len(g)
        if 3 * planned > MAX_SOURCE_RECONSTRUCTIONS:
            raise NarrativeRuntimeError("aggregate reconstruction budget exceeded")
        args = dict(capture_sha256=capture_sha256, partial_replies=p, grounding_replies=g,
                    materiality_decisions=d, max_context_bytes=max_context_bytes)
        plan = c4.build_witnessed_narrative_contexts(**args)
        data = json.loads(plan.canonical_bytes)
        if len(data["contexts"]) > max_entities or len(data["contexts"])*(1+c4.MAX_SENTENCES) > max_calls:
            raise NarrativeRuntimeError("complete run exceeds entity or call budget")
        stored = c4.cas.read_verified_capture(capture_sha256)
        if (stored.index_sha256 != capture_sha256 or stored.collector_storage_attested is not True
                or stored.capture_index.evidence_sha256 != data["preview"]["evidence_sha256"]):
            raise NarrativeRuntimeError("source changed before inference")
        source_bytes = len(stored.evidence_bytes)+len(stored.receipt_bytes)+len(stored.capture_index.canonical_bytes)
        # C2b=2*planned+1, C3 and C4 add one each; three public C4 calls
        # plus the direct measurement read above. Before any inference.
        planned_reads = 3*(2*planned+3)+1
        if source_bytes * planned_reads > MAX_SOURCE_WORK_BYTES:
            raise NarrativeRuntimeError("aggregate source-read budget exceeded")
        attempts, drafts, verdicts = [], [], []

        def invoke(stage, identity, ctx):
            body = _json(ctx)
            if _sha(body) != identity:
                raise NarrativeRuntimeError("rebuilt context hash changed")
            raw = receipt = None
            if time.monotonic() - started >= seconds:
                attempts.append(NarrativeAttempt(stage, identity, "deadline_pending", None, None))
                return None
            try:
                result = infer(body, deadline=started + seconds)
                if type(result) is not NarrativeCallResult:
                    raise NarrativeRuntimeError("inference result type invalid")
                candidate_raw, candidate_receipt = result.raw_response_bytes, result.receipt_bytes
                limit = c4.MAX_REPLY_BYTES if stage == "editorial" else c4.MAX_GROUND_REPLY_BYTES
                raw = candidate_raw if type(candidate_raw) is bytes and 0 < len(candidate_raw) <= limit else None
                receipt = candidate_receipt if type(candidate_receipt) is bytes and 0 < len(candidate_receipt) <= MAX_RECEIPT_BYTES else None
                if raw is None or receipt is None:
                    raise NarrativeRuntimeError("inference result byte envelope invalid")
                meta = _decode_json_object(receipt, label="inference receipt")
                if (meta.get("context_sha256") != identity or meta.get("raw_response_sha256") != _sha(raw)
                        or meta.get("model_execution_attested") is not False
                        or meta.get("publication_authorized") is not False):
                    raise NarrativeRuntimeError("inference receipt mismatch")
                if time.monotonic() - started >= seconds:
                    attempts.append(NarrativeAttempt(stage, identity, "deadline_pending", raw, receipt))
                    return None
                if stage == "editorial":
                    c4._normalize(ctx, raw)
                else:
                    obj = _decode_json_object(raw, label="narrative verdict")
                    if (set(obj) != {"verdict", "reason"} or type(obj["verdict"]) is not str
                            or obj["verdict"] not in ("supported", "unsupported")):
                        raise NarrativeRuntimeError("invalid model verdict")
                    c4._texts(obj["reason"])
            except NarrativeDeadlineError:
                attempts.append(NarrativeAttempt(stage, identity, "deadline_pending", raw, receipt))
                return None
            except Exception:
                # Exact bounded replies are retained privately; raw provider
                # exception strings (possibly credentials/source) are not.
                attempts.append(NarrativeAttempt(stage, identity, "inference_or_validation_failed", raw, receipt))
                return None
            attempts.append(NarrativeAttempt(stage, identity, "accepted", raw, receipt))
            return raw

        for row in data["contexts"]:
            raw = invoke("editorial", row["context_sha256"], row["context"])
            if raw is not None:
                drafts.append(c4.NarrativeReply(row["context_sha256"],raw))
        grounds = c4.build_witnessed_narrative_grounding_contexts(**args, narrative_replies=tuple(drafts)).report()
        for row in grounds["contexts"]:
            raw = invoke("grounding", row["ground_context_sha256"], row["context"])
            if raw is not None:
                verdicts.append(c4.NarrativeGroundReply(row["ground_context_sha256"],raw))
        preview = c4.build_witnessed_narrative_preview(**args, narrative_replies=tuple(drafts),
                                                      narrative_grounding_replies=tuple(verdicts))
        if preview.report()["source_context_set_sha256"] != plan.sha256:
            raise NarrativeRuntimeError("source context changed across inference")
        report = {"record_version": VERSION, "stage": "github_daily_narrative_runtime_v1",
            "capture_sha256": capture_sha256, "context_set_sha256": plan.sha256,
            "preview_sha256": preview.sha256, "date": data["preview"]["date"],
            "collection_mode": data["preview"]["collection_mode"],
            "entity_count": len(data["contexts"]), "planned_max_calls": len(data["contexts"])*(1+c4.MAX_SENTENCES),
            "planned_cas_triplet_reads": planned_reads,
            "attempts": [{"stage": a.stage, "context_sha256": a.context_sha256, "state": a.state,
                "raw_response_sha256": None if a.raw_response_bytes is None else _sha(a.raw_response_bytes),
                "receipt_sha256": None if a.receipt_bytes is None else _sha(a.receipt_bytes)} for a in attempts],
            "inference_responses_complete": all(a.state == "accepted" for a in attempts),
            "publication_authorized": False, "model_execution_attested": False,
            "requires_human_quality_review": True, "semantic_coverage_verified": False}
        return NarrativeRun(_json(report),tuple(attempts),tuple(drafts),tuple(verdicts),preview)
    except NarrativeRuntimeError:
        raise
    except (c4.DailyNarrativeError, ArtifactLifecycleError, ValueError, TypeError,
            AttributeError, KeyError, IndexError, RecursionError):
        raise NarrativeRuntimeError("trusted narrative runtime source unavailable") from None
