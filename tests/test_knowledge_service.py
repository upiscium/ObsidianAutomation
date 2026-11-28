"""B1-lite phase 3: knowledge panel backend and opt-in schedule. Everything synthetic."""
from __future__ import annotations

import io
import uuid
import json
import threading

import pytest

from obsidian_automation.chat_import_service import ChatImportService
from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.openai_compatible import OpenAICompatibleProviderError
from obsidian_automation.knowledge_service import (
    KnowledgeConfig, KnowledgeRefused, KnowledgeService, _jst_midnight,
)
from obsidian_automation.three_services_server import make_server
from test_chat_import_server import ORIGIN, zip_bytes
from test_knowledge_proposal_publish import FakeVault, SOURCE, store  # noqa: F401 (fixture)
from test_three_services_server import request

MODEL = "qwen-test"
LLM_URL = "https://llm.example.invalid/v1"


def claude(uuid, day, question="private question"):
    return {"uuid": uuid, "name": "t", "created_at": day + "T00:00:00Z",
            "updated_at": day + "T00:00:00Z", "account": {"uuid": "a"},
            "chat_messages": [
                {"uuid": uuid + "m1", "sender": "human", "text": question + " " + uuid,
                 "created_at": day + "T00:00:00Z"},
                {"uuid": uuid + "m2", "sender": "assistant", "created_at": day + "T00:01:00Z",
                 "content": [{"type": "text", "text": "private answer " + uuid}]}]}


def llm(fail=False, gate=None):
    calls = []

    def transport(base_url, *, method, path, payload, timeout, api_key=None):
        calls.append(payload)
        if gate is not None:
            assert gate.wait(5)
        if fail:
            raise OpenAICompatibleProviderError("synthetic LLM failure with a private path /secret")
        output = {"title": "Note %d" % len(calls), "category": "explanation",
                  "source_type": "self", "body": "# H\n\nBody %d.\n" % len(calls)}
        return {"model": payload["model"],
                "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}]}
    transport.calls = calls
    return transport


class Env:
    def __init__(self, tmp_path, store, **config):
        self.state = tmp_path / "state"
        self.state.mkdir(exist_ok=True)
        self.dav = FakeVault()
        self.llm = llm()
        self.now = [1_800_000_000.0]
        self.config = KnowledgeConfig(llm_url=LLM_URL, model=MODEL, **config)
        self.guard_calls = 0
        self.store = store
        self.build()

    def guard(self):
        self.guard_calls += 1

    def build(self):
        self.service = KnowledgeService(
            self.state, shared=self.store, guard=self.guard, config=self.config,
            transport=self.dav, json_transport=self.llm, clock=lambda: self.now[0])

    def import_zip(self, conversations):
        data = zip_bytes(conversations)
        ChatImportService(self.state).upload(io.BytesIO(data).read, len(data))

    def wait(self):
        worker = self.service._worker
        if worker is not None:
            worker.join(10)
            assert not worker.is_alive()


@pytest.fixture
def env(tmp_path, store):
    made = Env(tmp_path, store)
    yield made
    made.service.close()


def first(listing, state="new"):
    return next(c for c in listing["conversations"] if c["state"] == state)


def test_listing_is_count_only_deduplicated_and_marks_what_was_proposed(env):
    env.import_zip([claude("a", "2026-01-02"), claude("b", "2026-01-01")])
    env.import_zip([claude("a", "2026-01-02"), claude("c", "2026-01-03")])  # "a" again
    listing = env.service.conversations()
    assert listing["total"] == 3 and listing["counts"] == {"new": 3}
    blob = json.dumps(listing)
    assert "private" not in blob and "question" not in blob
    assert set(listing["conversations"][0]) == {
        "archive", "index", "part", "parts", "provider", "messages", "bytes", "first_day", "state"}
    env.service.start_manual({"archive": first(listing)["archive"], "index": first(listing)["index"]})
    env.wait()
    after = env.service.conversations()
    assert after["counts"] == {"new": 2, "proposed": 1}


def test_a_manual_run_creates_the_note_once_and_records_the_result(env):
    env.import_zip([claude("a", "2026-01-01")])
    item = first(env.service.conversations())
    assert env.service.start_manual({"archive": item["archive"], "index": item["index"]})["outcome"] == "accepted"
    env.wait()
    status = env.service.status()
    run = status["recent_runs"][0]
    assert (run["status"], run["trigger"], run["outcome"]) == ("proposed", "manual", "created")
    assert run["file"].endswith(".md") and run["error_code"] is None
    assert any(path.startswith("04-AI/Proposals/") for path in env.dav.files)
    assert env.guard_calls >= 1 and len(env.llm.calls) == 1
    # the same conversation is not generated or written again
    env.service.start_manual({"archive": item["archive"], "index": item["index"]})
    env.wait()
    assert len(env.llm.calls) == 1 and len(env.dav.files) == 1
    assert env.service.status()["recent_runs"][0]["outcome"] == "already_proposed"
    assert "private" not in json.dumps(status)


def test_only_one_run_at_a_time(env):
    env.import_zip([claude("a", "2026-01-01"), claude("b", "2026-01-02")])
    gate = threading.Event()
    env.llm = llm(gate=gate)
    env.service.close()
    env.build()
    listing = env.service.conversations()
    one, two = listing["conversations"][:2]
    env.service.start_manual({"archive": one["archive"], "index": one["index"]})
    with pytest.raises(KnowledgeRefused) as busy:
        env.service.start_manual({"archive": two["archive"], "index": two["index"]})
    assert busy.value.code == "knowledge_busy"
    assert env.service.status()["busy"] is True
    gate.set()
    env.wait()
    assert env.service.status()["busy"] is False


@pytest.mark.parametrize("payload", [
    None, {}, {"archive": "x", "index": 0}, {"archive": "a" * 64, "index": -1},
    {"archive": "a" * 64, "index": True}, {"archive": "a" * 64, "index": "0"},
    {"archive": "a" * 64, "index": 0, "extra": 1}, {"archive": "A" * 64, "index": 0},
])
def test_bad_manual_requests_are_refused_without_a_run(env, payload):
    with pytest.raises(KnowledgeRefused):
        env.service.start_manual(payload)
    assert env.service.status()["recent_runs"] == []


def test_a_failed_run_is_recorded_with_a_fixed_code_blocks_the_schedule_until_retried(env):
    env.import_zip([claude("a", "2026-01-01")])
    env.llm = llm(fail=True)
    env.service.close()
    env.build()
    item = first(env.service.conversations())
    env.service.start_manual({"archive": item["archive"], "index": item["index"]})
    env.wait()
    run = env.service.status()["recent_runs"][0]
    assert run["status"] == "failed" and run["error_code"] == "generation_failed"
    assert "secret" not in json.dumps(env.service.status())
    assert env.service.conversations()["counts"] == {"failed": 1}
    # the schedule never retries it on its own
    env.service.update_settings({"enabled": True})
    env.now[0] += 3601
    assert env.service.tick() is True
    env.wait()
    assert env.service.status()["recent_runs"][0]["error_code"] == "nothing_to_do"
    assert len(env.llm.calls) == 1
    # a person retries it by hand and it is no longer blocked
    env.llm = llm()
    env.service.close()
    env.build()
    env.service.start_manual({"archive": item["archive"], "index": item["index"]})
    env.wait()
    assert env.service.conversations()["counts"] == {"proposed": 1}


def test_the_schedule_is_off_by_default_and_never_catches_up(env):
    env.import_zip([claude("a", "2026-01-01")])
    assert env.service.settings() == {"enabled": False, "next_due": None}
    env.now[0] += 10 * 3600
    assert env.service.tick() is False and env.llm.calls == []
    for bad in (None, {}, {"enabled": 1}, {"enabled": True, "x": 1}):
        with pytest.raises(KnowledgeRefused):
            env.service.update_settings(bad)
    env.service.update_settings({"enabled": True})
    due = env.service.settings()["next_due"]
    assert due == env.now[0] + 3600
    assert env.service.tick() is False  # not due yet
    env.now[0] = due + 5 * 3600  # a long outage: one run, then one interval away
    assert env.service.tick() is True
    env.wait()
    assert env.service.settings()["next_due"] == env.now[0] + 3600
    assert env.service.tick() is False
    assert len(env.llm.calls) == 1
    env.service.update_settings({"enabled": False})
    assert env.service.settings() == {"enabled": False, "next_due": None}


def test_the_schedule_takes_the_oldest_new_conversation_first(env):
    env.import_zip([claude("qqlast", "2026-03-01"), claude("qqfirst", "2026-01-01"),
                    claude("qqmid", "2026-02-01")])
    env.service.update_settings({"enabled": True})
    env.now[0] += 3601
    env.service.tick()
    env.wait()
    assert "qqfirst" in json.dumps(env.llm.calls[0])
    assert "qqlast" not in json.dumps(env.llm.calls[0])
    assert "qqmid" not in json.dumps(env.llm.calls[0])


def test_daily_cap_pending_limit_and_unreadable_count_each_stop_a_scheduled_run(tmp_path, store):
    env = Env(tmp_path, store, max_per_day=1, max_pending=2)
    try:
        env.import_zip([claude(n, "2026-01-0%d" % i) for i, n in enumerate("abcd", 1)])
        env.service.update_settings({"enabled": True})
        env.now[0] += 3601
        assert env.service.tick() is True
        env.wait()
        assert len(env.llm.calls) == 1
        # the daily cap (one per Japan-time day) now stops the next slot
        env.now[0] += 3601
        assert env.service.tick() is False
        assert env.service.status()["schedule"]["last_skip"] == "daily_cap"
        env.now[0] = _jst_midnight(env.now[0]) + 86400 + 3600  # next Japan-time day
        # pending proposals at or above the limit stop generation
        env.dav.files["04-AI/Proposals/x.md"] = b"1"
        env.dav.files["04-AI/Proposals/y.md"] = b"2"
        assert env.service.tick() is True
        env.wait()
        assert env.service.status()["recent_runs"][0]["error_code"] == "pending_limit"
        assert len(env.llm.calls) == 1
        # an unreadable count also stops it (fail closed)
        env.dav.files.clear()
        real = env.dav.__call__

        def broken(**kwargs):
            if kwargs["method"] == "PROPFIND" and kwargs["headers"].get("Depth") == "1":
                return HTTPResponse(500, b"", None)
            return real(**kwargs)

        env.service.transport = broken
        env.now[0] += 3601
        assert env.service.tick() is True
        env.wait()
        assert env.service.status()["recent_runs"][0]["error_code"] == "pending_count_unavailable"
        assert len(env.llm.calls) == 1
    finally:
        env.service.close()


def test_a_run_interrupted_by_a_restart_is_unconfirmed_never_replayed(env):
    with env.service.db:
        env.service.db.execute(
            "INSERT INTO runs (run_id,trigger,status,created) VALUES ('r1','manual','running',1)")
    env.service.close()
    env.build()
    run = env.service.status()["recent_runs"][0]
    assert (run["status"], run["error_code"]) == ("unconfirmed", "interrupted")
    assert env.llm.calls == []


def test_config_validation():
    for bad in (dict(interval_seconds=60), dict(max_per_day=0), dict(max_pending=0),
                dict(max_per_day=True)):
        with pytest.raises(ValueError):
            KnowledgeConfig(llm_url=LLM_URL, model=MODEL, **bad)
    for url, model in (("", "m"), ("u", ""), (None, "m")):
        with pytest.raises(ValueError):
            KnowledgeConfig(llm_url=url, model=model)


# ----------------------------------------------------------------- HTTP routes

class Api:
    def __init__(self, service):
        self.server = make_server(port=0, bind_address="127.0.0.1")
        self.server.knowledge = service
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, payload, *, csrf=True, origin=True):
        token = None
        if csrf:
            _, body = request(self.port, "GET", "/api/v1/session")
            token = json.loads(body)["csrf"]
        code, body = request(self.port, "POST", path, payload=payload, csrf=token,
                             origin=ORIGIN.format(port=self.port) if origin else None)
        return code, json.loads(body)


def test_routes_are_off_without_the_service_and_guarded_with_it(env):
    off = Api(None)
    try:
        assert request(off.port, "GET", "/api/v1/knowledge")[0] == 503
        assert off.post("/api/v1/knowledge/conversations", {})[0] == 503
        code, page = request(off.port, "GET", "/")
        assert code == 200 and b"/static/knowledge.js" in page
    finally:
        off.close()
    env.import_zip([claude("a", "2026-01-01")])
    on = Api(env.service)
    try:
        code, body = request(on.port, "GET", "/api/v1/knowledge")
        assert code == 200 and json.loads(body)["enabled"] is True
        assert request(on.port, "GET", "/static/knowledge.js")[0] == 200
        for path, payload in (("/api/v1/knowledge/conversations", {}),
                              ("/api/v1/knowledge/runs", {"archive": "a" * 64, "index": 0}),
                              ("/api/v1/knowledge/settings", {"enabled": True})):
            assert on.post(path, payload, csrf=False)[0] == 403
            assert on.post(path, payload, origin=False)[0] == 403
        code, listing = on.post("/api/v1/knowledge/conversations", {})
        assert code == 200 and listing["total"] == 1
        assert on.post("/api/v1/knowledge/conversations", {"x": 1})[0] == 400
        assert on.post("/api/v1/knowledge/runs", {"archive": "z", "index": 0})[0] == 400
        assert on.post("/api/v1/knowledge/runs", {"archive": "a" * 64, "index": 0})[0] == 404
        code, body = on.post("/api/v1/knowledge/settings", {"enabled": True})
        assert code == 200 and body["enabled"] is True
        item = listing["conversations"][0]
        code, body = on.post("/api/v1/knowledge/runs",
                             {"archive": item["archive"], "index": item["index"]})
        assert code == 202 and body["outcome"] == "accepted"
        env.wait()
        assert json.loads(request(on.port, "GET", "/api/v1/knowledge")[1])["recent_runs"][0][
            "status"] == "proposed"
    finally:
        on.close()


@pytest.mark.parametrize("extra", [
    ["--knowledge-ui"],
    ["--knowledge-ui", "--knowledge-llm-url", LLM_URL],
    ["--knowledge-ui", "--knowledge-model", MODEL],
    ["--knowledge-ui", "--knowledge-llm-url", LLM_URL, "--knowledge-model", MODEL,
     "--knowledge-interval-seconds", "60"],
    ["--knowledge-ui", "--knowledge-llm-url", LLM_URL, "--knowledge-model", MODEL,
     "--knowledge-max-per-day", "0"],
    ["--knowledge-ui", "--knowledge-llm-url", LLM_URL, "--knowledge-model", MODEL,
     "--knowledge-max-pending", "0"],
])
def test_the_app_refuses_an_incomplete_or_unsafe_knowledge_configuration(
        tmp_path, monkeypatch, capsys, extra):
    from obsidian_automation import snapshot_app
    monkeypatch.setattr("sys.argv", ["obsidian-snapshot-app", "--state", str(tmp_path)] + extra)
    with pytest.raises(SystemExit) as exit_info:
        snapshot_app.main()
    assert exit_info.value.code == 2
    assert not (tmp_path / "knowledge").exists()  # nothing was set up


def test_a_small_context_model_gets_a_lower_source_limit_and_empty_chats_are_labelled(tmp_path, store):
    env = Env(tmp_path, store, max_source_bytes=1024)
    try:
        long_chat = claude("big", "2026-01-01", question="x" * 12000)  # > 8 parts of 1 KiB
        empty_chat = dict(claude("none", "2026-01-02"), chat_messages=[])
        env.import_zip([long_chat, empty_chat, claude("small", "2026-01-03")])
        listing = env.service.conversations()
        assert sorted(c["state"] for c in listing["conversations"]) == ["empty", "new", "too_large"]
        big = next(c for c in listing["conversations"] if c["state"] == "too_large")
        with_empty = next(c for c in listing["conversations"] if c["state"] == "empty")
        for refused in (big, with_empty):
            env.service.start_manual({"archive": refused["archive"], "index": refused["index"]})
            env.wait()
            run = env.service.status()["recent_runs"][0]
            assert (run["status"], run["error_code"]) == ("failed", "conversation_not_usable")
        assert env.llm.calls == []  # nothing too large or empty ever reaches the model
        # the schedule picks only the conversation that fits
        env.service.update_settings({"enabled": True})
        env.now[0] += 3601
        env.service.tick()
        env.wait()
        assert len(env.llm.calls) == 1 and "small" in json.dumps(env.llm.calls[0])
    finally:
        env.service.close()


def test_source_limit_validation_in_config_and_app(tmp_path, monkeypatch):
    for bad in (100, 1023, 128 * 1024 + 1, True, "2048"):
        with pytest.raises(ValueError):
            KnowledgeConfig(llm_url=LLM_URL, model=MODEL, max_source_bytes=bad)
    from obsidian_automation import snapshot_app
    monkeypatch.setattr("sys.argv", [
        "obsidian-snapshot-app", "--state", str(tmp_path), "--knowledge-ui",
        "--knowledge-llm-url", LLM_URL, "--knowledge-model", MODEL,
        "--knowledge-max-source-bytes", "10"])
    with pytest.raises(SystemExit) as exit_info:
        snapshot_app.main()
    assert exit_info.value.code == 2


def test_failures_raise_the_health_alert_but_skipped_slots_do_not(env):
    def add(status, created):
        with env.service.db:
            env.service.db.execute(
                "INSERT INTO runs (run_id,trigger,status,created,completed) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, "scheduled", status, created, created))

    assert env.service.health()["alert"] is False
    add("proposed", 1.0)
    add("failed", 2.0)
    add("skipped", 3.0)  # neutral: neither breaks nor extends the streak
    add("unconfirmed", 4.0)
    assert env.service.health()["consecutive_failures"] == 2
    add("failed", 5.0)
    result = env.service.health()
    assert (result["service"], result["consecutive_failures"], result["alert"]) == ("knowledge", 3, True)
    assert result["last_success"] == 1.0 and result["failing_since"] == 2.0
    add("proposed", 6.0)
    assert env.service.health()["alert"] is False


def test_changing_the_model_does_not_make_proposed_conversations_new_again(env):
    """The journal key includes the model; the schedule must still not write a duplicate."""
    env.import_zip([claude("qqfirst", "2026-01-01"), claude("qqsecond", "2026-01-02")])
    first_item = first(env.service.conversations())
    env.service.start_manual({"archive": first_item["archive"], "index": first_item["index"]})
    env.wait()
    assert env.service.conversations()["counts"] == {"new": 1, "proposed": 1}
    env.service.close()
    env.config = KnowledgeConfig(llm_url=LLM_URL, model="another-model")
    env.build()  # same state, a different model
    assert env.service.conversations()["counts"] == {"new": 1, "proposed": 1}
    env.service.update_settings({"enabled": True})
    env.now[0] += 3601
    env.service.tick()
    env.wait()
    assert len(env.llm.calls) == 2  # the manual run, then one scheduled run for the new conversation only
    assert "qqfirst" in json.dumps(env.llm.calls[0])
    assert "qqsecond" in json.dumps(env.llm.calls[1]) and "qqfirst" not in json.dumps(env.llm.calls[1])


def test_limits_and_base_time_are_editable_persist_and_validate(env):
    from obsidian_automation.knowledge_service import KnowledgeRefused, KnowledgeService
    svc = env.service
    for bad in ({}, {"x": 1}, {"max_per_day": 0}, {"max_pending": 5000}, {"interval_seconds": 60},
                {"anchor_minute": 1440}, {"anchor_minute": True}, {"enabled": "yes"}):
        with pytest.raises(KnowledgeRefused):
            svc.update_settings(bad)
    svc.update_settings({"max_per_day": 7, "max_pending": 20, "interval_seconds": 7200})
    sched = svc.status()["schedule"]
    assert (sched["max_per_day"], sched["max_pending"], sched["interval_seconds"]) == (7, 20, 7200)
    assert sched["anchor_minute"] is None and sched["enabled"] is False
    # base time 09:30 JST with a 2 h interval: slots 09:30, 11:30, ... (next point after now)
    svc.update_settings({"enabled": True, "anchor_minute": 9 * 60 + 30})
    due = svc.settings()["next_due"]
    assert due > env.now[0] and due - env.now[0] <= 7200
    jst_minutes = ((due + 9 * 3600) % 86400) / 60
    assert (jst_minutes - 570) % 120 == 0
    # a limit edit alone keeps the pending slot; the values survive a restart
    svc.update_settings({"max_per_day": 8})
    assert svc.settings()["next_due"] == due
    svc.close()
    again = KnowledgeService(svc.state, shared=svc.shared, guard=svc.guard, config=svc.config,
                             clock=svc.clock)
    st = again.status()["schedule"]
    assert (st["max_per_day"], st["anchor_minute"]) == (8, 570)
    again.close()


def test_a_long_conversation_is_proposed_part_by_part_and_the_schedule_walks_the_parts(tmp_path, store):
    env = Env(tmp_path, store, max_source_bytes=2048)
    try:
        # about 4 KiB of question text: three parts of at most 2 KiB each
        env.import_zip([claude("long", "2026-01-01", question="line\n" * 800)])
        listing = env.service.conversations()
        rows = listing["conversations"]
        assert [(c["part"], c["parts"], c["state"]) for c in rows] == [(0, 3, "new"), (1, 3, "new"), (2, 3, "new")]
        assert all(c["bytes"] <= 2048 for c in rows)
        env.service.update_settings({"enabled": True})
        for expected in (1, 2, 3):
            env.now[0] += 3601
            assert env.service.tick() is True
            env.wait()
            assert len(env.llm.calls) == expected
            assert env.service.status()["recent_runs"][0]["status"] == "proposed"
        sent = [json.dumps(call, ensure_ascii=False) for call in env.llm.calls]
        for number, text in enumerate(sent, 1):
            assert f"長い会話の一部 {number}/3" in text
        states = [c["state"] for c in env.service.conversations()["conversations"]]
        assert states == ["proposed"] * 3
        env.now[0] += 86400  # next JST day: the daily cap is not what stops it
        env.service.tick()
        env.wait()
        assert env.service.status()["recent_runs"][0]["error_code"] == "nothing_to_do"
    finally:
        env.service.close()


def test_a_single_part_can_be_started_by_hand_and_a_bad_part_is_refused(tmp_path, store):
    env = Env(tmp_path, store, max_source_bytes=2048)
    try:
        env.import_zip([claude("long", "2026-01-01", question="line\n" * 800)])
        row = env.service.conversations()["conversations"][1]
        env.service.start_manual({"archive": row["archive"], "index": row["index"], "part": 1})
        env.wait()
        assert env.service.status()["recent_runs"][0]["status"] == "proposed"
        assert len(env.llm.calls) == 1 and "2/3" in json.dumps(env.llm.calls[0], ensure_ascii=False)
        states = [c["state"] for c in env.service.conversations()["conversations"]]
        assert states == ["new", "proposed", "new"]
        env.service.start_manual({"archive": row["archive"], "index": row["index"], "part": 9})
        env.wait()
        run = env.service.status()["recent_runs"][0]
        assert (run["status"], run["error_code"]) == ("failed", "conversation_not_usable")
        for bad in ({"archive": row["archive"], "index": 0, "part": -1},
                    {"archive": row["archive"], "index": 0, "part": "1"}):
            with pytest.raises(KnowledgeRefused):
                env.service.start_manual(bad)
    finally:
        env.service.close()


def test_endpoint_and_model_are_editable_validated_and_survive_a_restart(env):
    env.service.update_settings({"llm_url": "https://other.example.invalid/v1", "model": "model-b"})
    status = env.service.status()
    assert (status["llm_url"], status["model"]) == ("https://other.example.invalid/v1", "model-b")
    env.service.close()
    env.build()
    status = env.service.status()
    assert (status["llm_url"], status["model"]) == ("https://other.example.invalid/v1", "model-b")
    for bad in ({"llm_url": "ftp://x.example/v1"}, {"llm_url": "https://u:p@x.example/v1"},
                {"llm_url": "https://x.example/v1?key=1"}, {"llm_url": "https://x.example/v1#f"},
                {"llm_url": "x"}, {"llm_url": 5}, {"llm_url": "https://x.example/ v1"},
                {"model": ""}, {"model": " m"}, {"model": 'a"b'}, {"model": "x" * 121}, {"model": 3}):
        with pytest.raises(KnowledgeRefused):
            env.service.update_settings(bad)
    assert env.service.status()["model"] == "model-b"  # a refused change leaves the settings alone


def test_saving_unchanged_schedule_values_does_not_move_the_next_run(env):
    env.service.update_settings({"enabled": True, "interval_seconds": 7200})
    first = env.service.settings()["next_due"]
    env.now[0] += 600
    env.service.update_settings({"enabled": True, "interval_seconds": 7200, "max_per_day": 5})
    assert env.service.settings()["next_due"] == first
    env.service.update_settings({"interval_seconds": 3600})
    assert env.service.settings()["next_due"] == env.now[0] + 3600


def test_the_model_list_comes_from_the_configured_endpoint_and_failures_are_a_fixed_code(env):
    seen = []

    def fetch(url, key, timeout):
        seen.append(url)
        return ["m-b", "m-a", "m-a", "bad model\n", 'q"x']
    env.service.models_fetch = fetch
    assert env.service.models() == {"version": 1, "models": ["m-a", "m-b"]}
    assert seen == [LLM_URL]

    def boom(url, key, timeout):
        raise OSError("secret-host connection refused")
    env.service.models_fetch = boom
    with pytest.raises(KnowledgeRefused) as refused:
        env.service.models()
    assert refused.value.code == "models_unavailable" and "secret" not in str(refused.value)


def test_the_models_route_is_guarded_and_returns_ids(env):
    env.service.models_fetch = lambda url, key, timeout: ["m-a"]
    on = Api(env.service)
    try:
        assert on.post("/api/v1/knowledge/models", {}, csrf=False)[0] == 403
        assert on.post("/api/v1/knowledge/models", {}, origin=False)[0] == 403
        assert on.post("/api/v1/knowledge/models", {"x": 1})[0] == 400
        code, body = on.post("/api/v1/knowledge/models", {})
        assert code == 200 and body["models"] == ["m-a"]
        env.service.models_fetch = lambda url, key, timeout: (_ for _ in ()).throw(OSError("x"))
        assert on.post("/api/v1/knowledge/models", {})[0] == 502
    finally:
        on.close()
