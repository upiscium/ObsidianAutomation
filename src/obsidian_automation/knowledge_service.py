"""B1-lite phase 3: panel backend and opt-in schedule for knowledge proposals.

Built only with ``--knowledge-ui``. It lists the conversations already imported
through the chat-import panel (count and size only, never titles or text), runs
one proposal at a time (manual button, or the opt-in schedule that picks the
next unproposed conversation) and delegates the actual work to the same
``generate_and_publish`` the CLI uses: create-only into ``04-AI/Proposals/``,
exact-byte readback, pinned-root guard, idempotency journal.

Scheduled runs are brakes-first (all values are start-up arguments):

* off until switched on in the panel; at most ``max_per_day`` proposals per
  Japan-time day; never more often than ``interval_seconds`` (>= 1 hour);
* new generation stops while ``max_pending`` or more proposals still wait in
  ``04-AI/Proposals/`` (counted from the Vault before each scheduled run; if the
  count cannot be read, the run is skipped);
* a conversation whose run failed or is unconfirmed is skipped until a person
  retries it by hand; one proposal per run.

Every response is count-only: no titles, ids of conversations, text or paths.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .chatgpt_export_inspect import InspectionError, inspect_export
from .chatgpt_export_normalize import NormalizeError, PARSER_VERSIONS, normalize_export
from .job_health import job_health
from .knowledge_proposal import (
    MAX_SOURCE_BYTES, ProposalError, SourceText, conversation_text, split_conversation,
)
from .knowledge_proposal_publish import (
    ProposalJournal, PublishError, count_pending_proposals, generate_and_publish,
    recipe_key,
)

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_JST = timezone(timedelta(hours=9))
MIN_INTERVAL_SECONDS = 3600
LIST_ARCHIVES = 4          # newest archives considered when listing
LIST_LIMIT = 200           # conversations returned to the panel
RECENT_RUNS = 8
ACTIVE = ("queued", "running")
OUTCOME_STATUSES = frozenset({"proposed", "failed", "unconfirmed", "skipped"})


class KnowledgeRefused(ValueError):
    def __init__(self, code: str, http: int = 409):
        super().__init__(code)
        self.code, self.http = code, http


@dataclass(frozen=True)
class KnowledgeConfig:
    llm_url: str
    model: str
    api_key_file: Path | None = None
    timeout: float = 300.0
    interval_seconds: int = MIN_INTERVAL_SECONDS
    max_per_day: int = 3
    max_pending: int = 10
    structured_output: bool = True   # False for servers that reject response_format
    max_source_bytes: int = MAX_SOURCE_BYTES   # lower for a small-context model

    def __post_init__(self) -> None:
        if (type(self.llm_url) is not str or not self.llm_url
                or type(self.model) is not str or not self.model):
            raise ValueError("llm_url and model are required")
        if type(self.interval_seconds) is not int or self.interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError("interval_seconds must be an integer >= 3600")
        if type(self.max_per_day) is not int or not 1 <= self.max_per_day <= 100:
            raise ValueError("max_per_day must be 1..100")
        if type(self.max_pending) is not int or not 1 <= self.max_pending <= 1000:
            raise ValueError("max_pending must be 1..1000")
        if (type(self.max_source_bytes) is not int
                or not 1024 <= self.max_source_bytes <= MAX_SOURCE_BYTES):
            raise ValueError("max_source_bytes must be 1024..%d" % MAX_SOURCE_BYTES)


def _first_day(conversation) -> str:
    for message in conversation.messages:
        if isinstance(message.created_at, (int, float)):
            return datetime.fromtimestamp(message.created_at, timezone.utc).strftime("%Y-%m-%d")
    return "-"


def _jst_midnight(now: float) -> float:
    local = datetime.fromtimestamp(now, _JST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class KnowledgeService:
    def __init__(self, state: Path, *, shared, guard: Callable[[], object] | None,
                 config: KnowledgeConfig, transport=None, json_transport=None,
                 clock: Callable[[], float] = time.time, models_fetch=None):
        self.state = state
        self.chat_root = state / "chat-import"
        self.root = state / "knowledge"
        # The CLI (--publish) and this service share one journal, so a conversation
        # proposed by either is never generated twice.
        self.journal_dir = state / "knowledge-proposals"
        self.shared, self.guard, self.config = shared, guard, config
        self.transport, self.json_transport, self.clock = transport, json_transport, clock
        self.models_fetch = models_fetch or _fetch_models
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "service.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1),"
                        " enabled INTEGER NOT NULL, next_due REAL)")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (1,0,NULL)")
        # Limits edited in the panel; a NULL column keeps the command-line value.
        self.db.execute("CREATE TABLE IF NOT EXISTS limits (id INTEGER PRIMARY KEY CHECK(id=1),"
                        " interval_seconds INTEGER, max_per_day INTEGER, max_pending INTEGER,"
                        " anchor_minute INTEGER)")
        for column in ("llm_url", "model"):  # endpoint and model chosen in the panel
            try:
                self.db.execute(f"ALTER TABLE limits ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass  # already migrated
        self.db.execute("INSERT OR IGNORE INTO limits (id) VALUES (1)")
        row = self.db.execute("SELECT interval_seconds,max_per_day,max_pending,anchor_minute,"
                              "llm_url,model FROM limits WHERE id=1").fetchone()
        self.anchor_minute: int | None = row[3]
        try:
            self.config = replace(self.config, **{k: v for k, v in zip(
                ("interval_seconds", "max_per_day", "max_pending", "llm_url", "model"),
                (*row[:3], row[4], row[5])) if v is not None})
        except ValueError:
            pass  # a stored value that no longer validates falls back to the command line
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, trigger TEXT NOT NULL,"
                        " status TEXT NOT NULL, source_sha TEXT, archive_sha TEXT, idx INTEGER,"
                        " outcome TEXT, file TEXT, error_code TEXT, created REAL NOT NULL, completed REAL)")
        try:  # a long conversation is proposed part by part; 0 = the whole conversation
            self.db.execute("ALTER TABLE runs ADD COLUMN part INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # already migrated
        # A run left queued/running by a crash is indeterminate: never replayed.
        self.db.execute("UPDATE runs SET status='unconfirmed',error_code='interrupted',completed=?"
                        " WHERE status IN ('queued','running')", (self.clock(),))
        self.db.commit()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._cache: dict[str, tuple[str, list[dict]]] = {}
        self._parse_lock = threading.Lock()  # heavy archive parsing; never held with _lock
        self._last_skip: str | None = None

    # ------------------------------------------------------------------ status

    def settings(self) -> dict:
        row = self.db.execute("SELECT enabled,next_due FROM settings WHERE id=1").fetchone()
        return {"enabled": bool(row[0]), "next_due": row[1]}

    def _recent(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT run_id,trigger,status,outcome,file,error_code,created,completed FROM runs"
            " ORDER BY created DESC,rowid DESC LIMIT ?", (RECENT_RUNS,)).fetchall()
        keys = ("run_id", "trigger", "status", "outcome", "file", "error_code", "created", "completed")
        return [dict(zip(keys, row)) for row in rows]

    def status(self) -> dict:
        with self._lock:
            settings = self.settings()
            today = _jst_midnight(self.clock())
            made = self.db.execute(
                "SELECT COUNT(*) FROM runs WHERE status='proposed' AND completed>=?", (today,)).fetchone()[0]
            return {
                "version": 1, "enabled": True, "model": self.config.model,
                "llm_url": self.config.llm_url,
                "schedule": {"enabled": settings["enabled"], "next_due": settings["next_due"],
                             "interval_seconds": self.config.interval_seconds,
                             "max_per_day": self.config.max_per_day,
                             "max_pending": self.config.max_pending,
                             "anchor_minute": self.anchor_minute,
                             "proposed_today": made, "last_skip": self._last_skip},
                "busy": self._busy(), "recent_runs": self._recent(), "vault_write": True,
            }

    def health(self) -> dict:
        """Consecutive-failure streak for the panel banner; a skipped slot is neutral."""
        return job_health(self.db, self._lock, service="knowledge", success=("proposed",),
                          failure=("failed", "unconfirmed"), neutral=("skipped",), threshold=3)

    def _busy(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None

    def _next_slot(self, now: float) -> float:
        """The next scheduled time after ``now``: one interval away, or, with a base
        time (JST minute of day), the next point of base + k * interval."""
        interval = self.config.interval_seconds
        if self.anchor_minute is None:
            return now + interval
        base = _jst_midnight(now) + self.anchor_minute * 60
        return base + (math.floor((now - base) / interval) + 1) * interval

    def update_settings(self, payload: object) -> dict:
        """Any subset of enabled / interval_seconds / max_per_day / max_pending /
        anchor_minute (0..1439, JST, or null to clear); nothing else."""
        allowed = {"enabled", "interval_seconds", "max_per_day", "max_pending", "anchor_minute",
                   "llm_url", "model"}
        if (type(payload) is not dict or not payload or not set(payload) <= allowed
                or ("enabled" in payload and type(payload["enabled"]) is not bool)):
            raise KnowledgeRefused("invalid_request", 400)
        anchor = payload.get("anchor_minute", self.anchor_minute)
        if anchor is not None and (type(anchor) is not int or not 0 <= anchor < 1440):
            raise KnowledgeRefused("invalid_request", 400)
        try:
            if "llm_url" in payload and not _valid_llm_url(payload["llm_url"]):
                raise ValueError("llm_url")
            if "model" in payload and not _valid_model(payload["model"]):
                raise ValueError("model")
            config = replace(self.config, **{k: payload[k] for k in
                             ("interval_seconds", "max_per_day", "max_pending", "llm_url", "model")
                             if k in payload})
        except ValueError:
            raise KnowledgeRefused("invalid_request", 400)
        with self._lock:
            current = self.settings()
            enabled = payload.get("enabled", current["enabled"])
            schedule_changed = (config.interval_seconds != self.config.interval_seconds
                                or anchor != self.anchor_minute)
            self.config, self.anchor_minute = config, anchor
            due = current["next_due"]
            if not enabled:
                due = None
            elif (not current["enabled"] or due is None or schedule_changed):
                due = self._next_slot(self.clock())
            with self.db:
                self.db.execute("UPDATE limits SET interval_seconds=?,max_per_day=?,max_pending=?,"
                                "anchor_minute=?,llm_url=?,model=? WHERE id=1",
                                (config.interval_seconds, config.max_per_day, config.max_pending, anchor,
                                 config.llm_url, config.model))
                self.db.execute("UPDATE settings SET enabled=?,next_due=? WHERE id=1",
                                (int(enabled), due))
            self._last_skip = None
            return self.settings()

    # ----------------------------------------------------------- conversations

    def _ready_archives(self) -> list[str]:
        ready = self.chat_root / "ready"
        if not ready.is_dir():
            return []
        found = []
        for entry in os.scandir(ready):
            name = entry.name[:-4] if entry.name.endswith(".zip") else ""
            if _SHA.fullmatch(name) and entry.is_file(follow_symlinks=False):
                found.append((entry.stat(follow_symlinks=False).st_mtime, name))
        return [name for _, name in sorted(found, reverse=True)]

    def _archive_path(self, archive_sha: str) -> Path:
        if not isinstance(archive_sha, str) or not _SHA.fullmatch(archive_sha):
            raise KnowledgeRefused("invalid_request", 400)
        path = self.chat_root / "ready" / (archive_sha + ".zip")
        if path.is_symlink() or not path.is_file():
            raise KnowledgeRefused("archive_not_found", 404)
        return path

    def _summaries(self, archive_sha: str) -> tuple[str, list[dict]]:
        """Provider and per-conversation summaries of one (immutable) archive; cached."""
        with self._parse_lock:
            return self._summaries_locked(archive_sha)

    def _summaries_locked(self, archive_sha: str) -> tuple[str, list[dict]]:
        if archive_sha in self._cache:
            return self._cache[archive_sha]
        path = self._archive_path(archive_sha)
        try:
            provider = inspect_export(path).get("provider")
            if provider not in PARSER_VERSIONS:
                raise KnowledgeRefused("unsupported_export_format", 422)
            batch = normalize_export(path, scope="private-" + provider, provider=provider)
        except (InspectionError, NormalizeError):
            raise KnowledgeRefused("not_a_valid_export", 422) from None
        items = []
        cap = self.config.max_source_bytes
        for index, conversation in enumerate(batch.conversations):
            text = conversation_text(conversation)
            encoded = text.encode("utf-8")
            base = {"index": index, "provider": provider, "messages": len(conversation.messages),
                    "first_day": _first_day(conversation), "empty": not text.strip()}
            parts = None
            if len(encoded) > cap and not base["empty"]:
                parts = split_conversation(conversation, cap)
            if parts is None:  # fits whole, is empty, or needs too many parts
                items.append({**base, "part": 0, "parts": 1, "bytes": len(encoded),
                              "fits": len(encoded) <= cap,
                              "source_sha": hashlib.sha256(encoded).hexdigest()})
                continue
            for number, part_text in enumerate(parts):
                part_bytes = part_text.encode("utf-8")
                items.append({**base, "part": number, "parts": len(parts), "bytes": len(part_bytes),
                              "fits": True, "source_sha": hashlib.sha256(part_bytes).hexdigest()})
        self._cache[archive_sha] = (provider, items)
        return self._cache[archive_sha]

    def _journal_keys(self) -> set[str]:
        journal = ProposalJournal(self.journal_dir)
        try:
            return {row[0] for row in journal.db.execute("SELECT key FROM proposals")}
        finally:
            journal.close()

    def _proposed_sources(self) -> set[str]:
        """Conversations the panel already proposed, with whatever model made them.

        The journal key includes the model, so switching model would otherwise make every
        proposed conversation "new" again and the schedule would write a duplicate.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT source_sha FROM runs WHERE status='proposed' AND source_sha IS NOT NULL"
            ).fetchall()
        return {row[0] for row in rows}

    def _blocked(self) -> dict[str, str]:
        """Source digests whose latest run failed or is unconfirmed (skipped until retried)."""
        blocked: dict[str, str] = {}
        with self._lock:
            rows = self.db.execute(
                "SELECT source_sha,status FROM runs WHERE source_sha IS NOT NULL ORDER BY rowid").fetchall()
        for source_sha, status in rows:
            if status in ("failed", "unconfirmed"):
                blocked[source_sha] = status
            elif status == "proposed":
                blocked.pop(source_sha, None)
        return blocked

    def conversations(self) -> dict:
        """Count-only list of conversations, newest archive first, de-duplicated by content."""
        # Parsing can take long: it runs without the service lock, so status polling
        # and the scheduler tick never wait for it.
        blocked = self._blocked()
        journal_keys = self._journal_keys()
        proposed_sources = self._proposed_sources()
        seen: set[str] = set()
        out: list[dict] = []
        for archive_sha in self._ready_archives()[:LIST_ARCHIVES]:
            try:
                _, items = self._summaries(archive_sha)
            except KnowledgeRefused:
                continue
            for item in items:
                if item["source_sha"] in seen:
                    continue
                seen.add(item["source_sha"])
                if (recipe_key(item["source_sha"], self.config.model) in journal_keys
                        or item["source_sha"] in proposed_sources):
                    state = "proposed"
                elif item["empty"]:
                    state = "empty"
                elif not item["fits"]:
                    state = "too_large"
                else:
                    state = blocked.get(item["source_sha"], "new")
                out.append({"archive": archive_sha, "index": item["index"],
                            "part": item["part"], "parts": item["parts"],
                            "provider": item["provider"], "messages": item["messages"],
                            "bytes": item["bytes"], "first_day": item["first_day"],
                            "state": state})
        counts: dict[str, int] = {}
        for entry in out:
            counts[entry["state"]] = counts.get(entry["state"], 0) + 1
        return {"version": 1, "total": len(out), "counts": counts,
                "conversations": out[:LIST_LIMIT], "truncated": len(out) > LIST_LIMIT}

    # -------------------------------------------------------------------- runs

    def _api_key(self) -> str | None:
        if self.config.api_key_file is None:
            return None
        return self.config.api_key_file.read_text(encoding="utf-8").strip() or None

    def start_manual(self, payload: object) -> dict:
        if (type(payload) is not dict or not {"archive", "index"} <= set(payload) <= {"archive", "index", "part"}
                or type(payload["index"]) is not int or payload["index"] < 0
                or type(payload.get("part", 0)) is not int or payload.get("part", 0) < 0):
            raise KnowledgeRefused("invalid_request", 400)
        self._archive_path(payload["archive"])
        with self._lock:
            if self._busy():
                raise KnowledgeRefused("knowledge_busy")
            run_id = uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,trigger,status,archive_sha,idx,part,created)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (run_id, "manual", "queued", payload["archive"], payload["index"],
                     payload.get("part", 0), self.clock()))
            self._worker = threading.Thread(target=self._execute, args=(run_id,), daemon=False)
            self._worker.start()
            return {"outcome": "accepted", "run_id": run_id}

    def tick(self) -> bool:
        """Cheap, called by the app scheduler. The heavy work happens in the worker."""
        with self._lock:
            settings = self.settings()
            now = self.clock()
            due = settings["next_due"]
            if not settings["enabled"] or due is None or now < due:
                return False
            with self.db:  # never a catch-up queue: the next slot is one interval away
                self.db.execute("UPDATE settings SET next_due=? WHERE id=1",
                                (self._next_slot(now),))
            if self._busy():
                self._last_skip = "busy"
                return False
            made = self.db.execute(
                "SELECT COUNT(*) FROM runs WHERE status='proposed' AND completed>=?",
                (_jst_midnight(now),)).fetchone()[0]
            if made >= self.config.max_per_day:
                self._last_skip = "daily_cap"
                return False
            run_id = uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,trigger,status,created) VALUES (?,?,?,?)",
                    (run_id, "scheduled", "queued", now))
            self._worker = threading.Thread(target=self._execute, args=(run_id,), daemon=False)
            self._worker.start()
            return True

    def _finish(self, run_id: str, status: str, *, outcome=None, file=None, error=None,
                source_sha=None, archive_sha=None, idx=None, part=None) -> None:
        assert status in OUTCOME_STATUSES
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status=?,outcome=?,file=?,error_code=?,completed=?,"
                    "source_sha=COALESCE(?,source_sha),archive_sha=COALESCE(?,archive_sha),"
                    "idx=COALESCE(?,idx),part=COALESCE(?,part) WHERE run_id=?",
                    (status, outcome, file, error, self.clock(), source_sha, archive_sha, idx, part, run_id))
            if status == "skipped":
                self._last_skip = error

    @staticmethod
    def _find(items: list[dict], index: int, part: int) -> dict | None:
        return next((i for i in items if i["index"] == index and i["part"] == part), None)

    def _pick_next(self) -> tuple[str, int, int, str] | None:
        """Oldest new conversation part (by first message day, then part) among the newest archives."""
        listing = self.conversations()["conversations"]
        candidates = [c for c in listing if c["state"] == "new"]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c["first_day"] == "-", c["first_day"], c["archive"],
                                       c["index"], c["part"]))
        chosen = candidates[0]
        _, items = self._summaries(chosen["archive"])
        item = self._find(items, chosen["index"], chosen["part"])
        return chosen["archive"], chosen["index"], chosen["part"], item["source_sha"]

    def _execute(self, run_id: str) -> None:
        with self._lock:
            with self.db:
                self.db.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
            row = self.db.execute("SELECT trigger,archive_sha,idx,part FROM runs WHERE run_id=?",
                                  (run_id,)).fetchone()
        trigger, archive_sha, index, part = row
        source_sha = None
        try:
            if trigger == "scheduled":
                try:
                    pending = count_pending_proposals(
                        shared=self.shared, guard=self.guard, transport=self.transport)
                except PublishError as exc:
                    self._finish(run_id, "skipped", error=str(exc))
                    return
                if pending >= self.config.max_pending:
                    self._finish(run_id, "skipped", error="pending_limit")
                    return
                picked = self._pick_next()
                if picked is None:
                    self._finish(run_id, "skipped", error="nothing_to_do")
                    return
                archive_sha, index, part, source_sha = picked
            provider, items = self._summaries(archive_sha)
            item = self._find(items, index, part)
            if item is None or not item["fits"] or item["empty"]:
                raise KnowledgeRefused("conversation_not_usable", 422)
            source_sha = item["source_sha"]
            batch = normalize_export(self._archive_path(archive_sha), scope="private-" + provider,
                                     provider=provider)
            conversation = batch.conversations[index]
            if item["parts"] == 1:
                text = conversation_text(conversation)
            else:
                parts = split_conversation(conversation, self.config.max_source_bytes)
                if parts is None or len(parts) != item["parts"]:
                    raise KnowledgeRefused("conversation_changed", 409)
                text = parts[part]
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != source_sha:
                raise KnowledgeRefused("conversation_changed", 409)
            journal = ProposalJournal(self.journal_dir)
            try:
                report = generate_and_publish(
                    SourceText("private-" + provider, text), shared=self.shared, guard=self.guard,
                    journal=journal, ai_root=self.root / "ai", base_url=self.config.llm_url,
                    model=self.config.model, timeout=self.config.timeout, api_key=self._api_key(),
                    transport=self.transport, json_transport=self.json_transport,
                    structured_output=self.config.structured_output,
                    max_source_bytes=self.config.max_source_bytes)
            finally:
                journal.close()
            self._finish(run_id, "proposed", outcome=report.get("outcome", report["status"]),
                         file=report.get("file"), source_sha=source_sha,
                         archive_sha=archive_sha, idx=index, part=part)
        except PublishError as exc:
            unconfirmed = str(exc) == "outcome_unconfirmed"
            self._finish(run_id, "unconfirmed" if unconfirmed else "failed", error=str(exc),
                         source_sha=source_sha, archive_sha=archive_sha, idx=index, part=part)
        except (ProposalError, KnowledgeRefused) as exc:
            self._finish(run_id, "failed", error=str(exc), source_sha=source_sha,
                         archive_sha=archive_sha, idx=index, part=part)
        except Exception:
            # Never persist an exception text: it can hold paths, URLs or note text.
            self._finish(run_id, "unconfirmed", error="unexpected_exception",
                         source_sha=source_sha, archive_sha=archive_sha, idx=index, part=part)

    def models(self) -> dict:
        """Model ids the configured endpoint offers (OpenAI-compatible GET <base>/models)."""
        try:
            ids = self.models_fetch(self.config.llm_url, self._api_key(), 15.0)
        except Exception:
            raise KnowledgeRefused("models_unavailable", 502) from None
        clean = sorted({i for i in ids if _valid_model(i)})[:100]
        return {"version": 1, "models": clean}

    def close(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        self.db.close()


def _valid_llm_url(value: object) -> bool:
    """An http(s) base URL without credentials, query or fragment (the endpoint is chosen in the panel)."""
    from urllib.parse import urlsplit
    if type(value) is not str or not 8 <= len(value) <= 300 or not value.isprintable() or " " in value:
        return False
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 (raises on a bad port)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname)
            and not parts.username and not parts.password and not parts.query and not parts.fragment)


def _valid_model(value: object) -> bool:
    return (type(value) is str and 1 <= len(value) <= 120 and value.isprintable()
            and value == value.strip() and not any(c in value for c in "\"'\\<>"))


def _fetch_models(base_url: str, api_key: str | None, timeout: float) -> list[str]:
    """GET <base>/models; the reply is bounded (256 KiB) and only string ids are read."""
    import json as _json
    import urllib.request
    request = urllib.request.Request(base_url.rstrip("/") + "/models", method="GET")
    request.add_header("Accept", "application/json")
    if api_key:
        request.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (http/https only)
        body = response.read(256 * 1024 + 1)
    if len(body) > 256 * 1024:
        raise ValueError("too large")
    data = _json.loads(body.decode("utf-8"))
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("unexpected shape")
    return [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]
