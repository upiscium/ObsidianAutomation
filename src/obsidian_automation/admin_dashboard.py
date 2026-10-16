"""Loopback Dashboard; status + deliberately no-effect operation PREVIEW.

The POST preview route is strictly CSRF/Origin-guarded and cannot execute a
systemd operation, write Vault data or grant privileged control. No login is
implemented, so this must remain on loopback/private ingress only.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Sequence

from .admin_broker_socket import capabilities_from_socket, _unavailable_capabilities
from .admin_status import aggregate_status
from .admin_ai_status import load_ai_health
from .admin_operation_preview import preview_from_socket
from .admin_operation_web import (
    MAX_REQUEST_BYTES, OneTimePreviewCsrf, PreviewRequestRejected,
    parse_operation_preview_request, strict_browser_preview, unavailable_preview,
)

HTML = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ObsidianAutomation | Status</title>
  <link rel="stylesheet" href="/static/style.css">
  <script defer src="/static/app.js"></script>
</head>
<body>
  <main class="frame">
    <header>
      <div><p class="eyebrow">OBSIDIAN AUTOMATION</p>
        <h1>サービスの稼働状況</h1>
        <p class="desc">読み取り専用の管理Dashboard（試作版）</p></div>
      <button id="refresh" type="button">状態を更新</button>
    </header>
    <section aria-label="システムの説明" class="notice">
      <strong>Observation only</strong>
      <span>この画面からサービスの起動・停止やVaultへの書き込みはできません。</span>
    </section>
    <div class="summary">
      <span id="updated" aria-live="polite">状態を取得しています…</span>
      <span id="count">—</span>
    </div>
    <section id="pipeline" class="pipeline" aria-label="AI Pipeline" aria-live="polite">
      <h2>AI Pipeline</h2>
      <p id="pipeline-summary">AIの運用状況を取得しています…</p>
      <div id="pipeline-details" class="pipeline-details"></div>
    </section>
    <section id="services" class="cards" aria-label="サービス一覧" aria-live="polite"></section>
    <section id="operation-preview" class="pipeline" aria-label="操作の事前確認">
      <h2>サービス操作のプレビュー</h2>
      <p>現在の状態で操作を検討できるか確認します。これは実行許可ではなく、サービスへの変更も発生しません。</p>
      <div class="operation-controls">
        <label>対象サービス
          <select id="preview-service" aria-label="対象サービス" disabled></select>
        </label>
        <label>操作内容
          <select id="preview-action" aria-label="操作内容">
            <option value="pause">定期実行を一時停止</option>
            <option value="resume">定期実行を再開</option>
            <option value="run_once">一度だけ実行</option>
          </select>
        </label>
        <button id="preview-check" type="button" disabled>プレビュー</button>
        <button type="button" disabled aria-label="実際のサービス操作は未対応">実行不可</button>
      </div>
      <p id="preview-message" role="status" aria-live="polite">状態を取得してからプレビューを行えます。</p>
    </section>
    <p id="error" class="error" role="status"></p>
    <footer>状態はsystemdの直近の観測結果です。成功履歴やVaultの完全性を証明するものではありません。</footer>
  </main>
</body>
</html>
"""

CSS = """
:root { color-scheme: light dark; font: 15px/1.6 system-ui, -apple-system, sans-serif;
  --bg:#f7f8fb; --surface:#ffffff; --ink:#182536; --muted:#5c6979;
  --border:#dce2e9; --accent:#315cb5; --focus:#3169db }
@media (prefers-color-scheme: dark) { :root { --bg:#111820; --surface:#1a2632; --ink:#f1f5fa;
  --muted:#b2becd; --border:#344455; --accent:#8dacff; --focus:#8dacff } }
* { box-sizing: border-box }
body { background:var(--bg); color:var(--ink); margin:0 }
.frame { max-width:1080px; padding:40px 22px; margin:auto }
header { display:flex; justify-content:space-between; align-items:center; gap:24px }
h1 { font-size:clamp(1.5rem, 4vw, 2.15rem); letter-spacing:-.04em; margin:.1em 0 .2em }
.eyebrow { color:var(--accent); font-weight:750; letter-spacing:.12em; font-size:.73rem; margin:0 }
.desc,.summary,footer { color:var(--muted) }
.desc { margin:.15em 0 }
button { background:var(--accent); color:var(--bg); border:0; padding:9px 16px; border-radius:9px;
  font:inherit; font-weight:700; cursor:pointer; white-space:nowrap }
button:focus-visible { outline:3px solid var(--focus); outline-offset:3px }
button:disabled { opacity:.55; cursor:wait }
.notice { background:var(--surface); border:1px solid var(--border); border-left:4px solid var(--accent);
  border-radius:10px; padding:13px 16px; display:flex; gap:16px; margin:26px 0 22px; flex-wrap:wrap }
.summary { display:flex; justify-content:space-between; gap:12px; margin:0 0 15px }
.pipeline { background:var(--surface); border:1px solid var(--border); border-radius:14px;
  padding:18px 19px; margin:0 0 16px }
.pipeline h2 { margin:0; font-size:1.1rem }
.pipeline p { color:var(--muted); margin:5px 0 12px }
.pipeline-details { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:8px 18px }
.metric { display:flex; flex-direction:column; font-size:.82rem; color:var(--muted) }
.metric strong { font-size:1.08rem; color:var(--ink) }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:16px }
.card { background:var(--surface); border:1px solid var(--border); border-radius:14px;
  padding:18px 19px; min-height:168px }
.card h2 { margin:0; font-size:1.08rem }
.card .small { font-size:.81rem; color:var(--muted); margin-top:8px; overflow-wrap:anywhere }
.headline { display:flex; justify-content:space-between; align-items:center; gap:8px }
.state { padding:3px 9px; border:1px solid var(--border); border-radius:999px;
  color:var(--muted); white-space:nowrap; font-size:.73rem; font-weight:700 }
.state[data-health="healthy"] { color:#2e9164 }
.state[data-health="failed"] { color:#cf5b51 }
.state[data-health="paused"] { color:#a77d2d }
.rowline { display:flex; justify-content:space-between; gap:12px; font-size:.88rem; margin-top:12px }
.rowline .value { color:var(--muted); text-align:right }
.error { color:#cb4545; min-height:1.5em }
.operation-controls { display:flex; flex-wrap:wrap; align-items:end; gap:12px; margin:12px 0 }
.operation-controls label { display:flex; flex:1 1 170px; flex-direction:column; gap:5px; min-width:0 }
.operation-controls select { width:100%; min-height:42px; border:1px solid var(--border);
  background:var(--surface); color:var(--ink); border-radius:9px; font:inherit; padding:7px 9px }
.operation-controls button { flex:0 0 auto; }
#preview-message { overflow-wrap:anywhere }
footer { margin-top:32px; font-size:.78rem }
@media (max-width:620px) { .frame { padding:26px 14px } header { align-items:start; flex-direction:column }
  button { width:100% } .notice { margin-top:18px } }
"""

JS = """
"use strict";
const labels = {
  healthy:"正常", failed:"異常", paused:"一時停止", unknown:"未確認",
  unverified:"未検証", scheduled:"待機中", triggering:"起動処理中",
  not_installed:"未導入", blocked:"ブロック", stopped:"停止",
  no_next_run:"次回未設定", running:"実行中", succeeded:"直近成功",
  not_observed:"実行記録なし", fresh:"最新", stale:"古い状態", unavailable:"未取得",
  invalid:"不正な状態", ok:"Pre-reviewの既知障害なし",
  warning:"Pre-review失敗・再試行待ち", critical:"Pre-review障害",
  observed_success:"直近成功（鮮度未検証）"
};
const $ = (id) => document.getElementById(id);
const stateText = (value) => labels[value] || "不明";
function line(name, value) {
  const row = document.createElement("div");
  row.className = "rowline";
  const label = document.createElement("span");
  label.textContent = name;
  const content = document.createElement("span");
  content.className = "value";
  content.textContent = stateText(value);
  row.append(label, content);
  return row;
}
function card(service) {
  const box = document.createElement("article");
  box.className = "card";
  const top = document.createElement("div");
  top.className = "headline";
  const title = document.createElement("h2");
  title.textContent = service.label;
  const status = document.createElement("span");
  status.className = "state";
  status.dataset.health = service.health;
  status.textContent = stateText(service.health);
  top.append(title, status);
  const name = document.createElement("div");
  name.className = "small";
  name.textContent = service.id;
  box.append(top, name, line("定期実行", service.schedule), line("直近の実行", service.latest_execution));
  return box;
}
function renderAi(p) {
  if (!p || !["fresh","stale","unavailable","invalid"].includes(p.freshness)) {
    throw Error("invalid_ai_projection");
  }
  $("pipeline-summary").textContent = p.freshness === "fresh"
    ? "Pipeline: " + stateText(p.health)
    : "Pipeline: " + stateText(p.freshness) + "（現在の状態は未確認）";
  const value = (v) => Number.isSafeInteger(v) && v >= 0 ? String(v) : "—";
  const metric = (name, v) => {
    const box = document.createElement("div");
    box.className = "metric";
    const label = document.createElement("span");
    label.textContent = name;
    const strong = document.createElement("strong");
    strong.textContent = v;
    box.append(label, strong);
    return box;
  };
  $("pipeline-details").replaceChildren(
    metric("Review待ち", value(p.review_count)),
    metric("要対応エラー", value(p.operational_failures)),
    metric("失敗・再試行待ち（予算未確認）", value(p.pending_retry_failures)),
    metric("Validation棄却（正常）", value(p.deterministic_rejections)),
    metric("Backpressure", p.backpressure === null ? "—" : p.backpressure === true ? "有効" : "無効"),
    metric("観測経過", p.age_seconds === null ? "—" : value(p.age_seconds) + "秒"),
  );
  const knownIncidents = {
    blocked_generations: "Blocked",
    retry_exhausted_generations: "Retry exhausted",
    retryable_failure_pending: "失敗後の再試行待ち"
  };
  const incidents = p.freshness === "fresh" && Array.isArray(p.incidents)
    ? p.incidents : [];
  for (const incident of incidents.slice(0, 3)) {
    if (!Object.hasOwn(knownIncidents, incident.code)) continue;
    $("pipeline-details").append(
      metric(knownIncidents[incident.code], value(incident.affected))
    );
  }
}
let previewGeneration = 0;
async function refresh() {
  ++previewGeneration; // Invalidate any in-flight operation preview.
  $("preview-check").disabled = true;
  $("preview-message").textContent = "状態の更新中です。";
  $("refresh").disabled = true;
  $("error").textContent = "";
  try {
    const response = await fetch("/api/v1/status", {cache:"no-store", credentials:"same-origin"});
    if (!response.ok) throw Error("server");
    const data = await response.json();
    if (data.version !== 1 || !Array.isArray(data.services)) throw Error("schema");
    renderAi(data.ai_pipeline);
    $("services").replaceChildren(...data.services.map(card));
    const selectable = data.services.filter(s =>
      ["ai-vault-pull","pre-review","github-sync","github-daily","core-promotion"].includes(s.id));
    $("preview-service").replaceChildren(...selectable.map(s => {
      const option = document.createElement("option");
      option.value = s.id;
      option.textContent = s.label;
      return option;
    }));
    $("preview-service").disabled = selectable.length === 0;
    $("preview-check").disabled = selectable.length === 0;
    $("preview-message").textContent = "操作をプレビューできます。実行は無効です。";
    $("updated").textContent = "観測日時: " + data.generated_at;
    $("count").textContent = data.services.length + " services";
  } catch {
    // Previous observations must never remain visible as current on errors.
    $("services").replaceChildren();
    $("preview-service").replaceChildren();
    $("preview-service").disabled = true;
    $("preview-check").disabled = true;
    $("preview-message").textContent = "観測できないためプレビューできません。";
    $("pipeline-details").replaceChildren();
    $("pipeline-summary").textContent = "Pipeline: 現在の状態は未確認";
    $("updated").textContent = "観測失敗";
    $("count").textContent = "—";
    $("error").textContent = "状態を取得できませんでした。サーバー側の設定を確認してください。";
  } finally {
    $("refresh").disabled = false;
  }
}
$("refresh").addEventListener("click", refresh);
refresh();

async function previewOperation() {
  const button = $("preview-check");
  const serviceId = $("preview-service").value;
  const action = $("preview-action").value;
  const generation = ++previewGeneration;
  const stillCurrent = () => (
    generation === previewGeneration
    && $("preview-service").value === serviceId
    && $("preview-action").value === action
    && !$("preview-service").disabled
  );
  if (!["ai-vault-pull","pre-review","github-sync","github-daily","core-promotion"].includes(serviceId)
      || !["pause","resume","run_once"].includes(action)) {
    $("preview-message").textContent = "操作の対象を検証できません。";
    return;
  }
  button.disabled = true;
  $("preview-message").textContent = "現在の状態を検証しています…";
  try {
    if (!globalThis.crypto || !crypto.randomUUID) throw Error("random_uuid_missing");
    const sessionResponse = await fetch("/api/v1/operation-session", {
      cache:"no-store", credentials:"same-origin",
    });
    if (!sessionResponse.ok) throw Error("csrf_unavailable");
    const session = await sessionResponse.json();
    if (session.version !== 1 || session.authority !== "preview_only"
        || session.can_execute !== false || typeof session.csrf_token !== "string") {
      throw Error("invalid_csrf");
    }
    const request = {
      version:1, service_id:serviceId, action, request_id:crypto.randomUUID(),
    };
    const response = await fetch("/api/v1/operation-previews", {
      method:"POST", cache:"no-store", credentials:"same-origin",
      headers:{"Content-Type":"application/json", "X-Obsidian-CSRF":session.csrf_token},
      body:JSON.stringify(request),
    });
    if (!response.ok) throw Error("preview_request_failed");
    const result = await response.json();
    if (result.version !== 1 || result.authority !== "preview_only"
        || result.can_execute !== false
        || !["rejected","compatible"].includes(result.status)) {
      throw Error("untrusted_preview_response");
    }
    if (stillCurrent()) {
      $("preview-message").textContent = result.status === "compatible"
        ? "現在の状態では操作候補です。ただし実行権限は未承認のため、変更はできません。"
        : "操作の状態検証が完了しませんでした。操作は実行されていません。";
    }
  } catch {
    if (stillCurrent()) {
      $("preview-message").textContent = "プレビューが利用できません。操作は実行されていません。";
    }
  } finally {
    if (generation === previewGeneration) {
      button.disabled = $("preview-service").disabled;
    }
  }
}
for (const id of ["preview-service", "preview-action"]) {
  $(id).addEventListener("change", () => {
    ++previewGeneration;
    $("preview-message").textContent = "対象・操作が変わりました。プレビューをやり直してください。";
    $("preview-check").disabled = $("preview-service").disabled;
  });
}
$("preview-check").addEventListener("click", previewOperation);
"""

Observer = Callable[[], list[dict[str, object]]]
AIObserver = Callable[[], dict[str, object]]

MAX_DASHBOARD_HANDLERS = 12
HEADER_DEADLINE_SECONDS = 2.0
MAX_HEADER_BYTES = 8192


class _BoundedHeaderReader:
    """One monotonic deadline across request-line + all HTTP header lines."""

    def __init__(self, raw, conn):
        self.raw = raw
        self.conn = conn
        self.deadline = time.monotonic() + HEADER_DEADLINE_SECONDS
        self.read_total = 0

    def readline(self, size=-1):
        limit = min(size if isinstance(size, int) and size > 0
                    else MAX_HEADER_BYTES, MAX_HEADER_BYTES)
        data = bytearray()
        while True:
            if len(data) >= limit or self.read_total >= MAX_HEADER_BYTES:
                raise TimeoutError("http_header_limit")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("http_header_deadline")
            self.conn.settimeout(remaining)
            value = self.raw.read1(1)
            if not value:
                return bytes(data)
            data.extend(value)
            self.read_total += len(value)
            if value == b"\n":
                return bytes(data)

    def read1(self, size=-1):
        # POST body has its own independent 1-second monotonic deadline.
        return self.raw.read1(size)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class BoundedDashboardServer(ThreadingHTTPServer):
    """Bound concurrent slow clients before a new handler thread is spawned."""

    request_queue_size = MAX_DASHBOARD_HANDLERS
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_DASHBOARD_HANDLERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            return super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _StatusSingleFlight:
    """At most one costly systemd/AI projection collection in flight.

    Repeated cross-origin GETs without a readable Origin cannot cause dozens
    of concurrent subprocess invocations. A busy reader fails closed quickly;
    a recent immutable JSON result is shared for at most one second. A failed
    read NEVER serves a former healthy/stale observation.
    """

    def __init__(self, observer: Observer, ai_observer: AIObserver):
        self.observer = observer
        self.ai_observer = ai_observer
        self._lock = threading.Lock()
        self._active = False
        self._cache: bytes | None = None
        self._expires = 0.0

    def get(self) -> tuple[int, bytes]:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now < self._expires:
                return 200, self._cache
            if self._active:
                return 503, b'{"error":"status_busy"}'
            self._active = True
        try:
            services = self.observer()
            ai_pipeline = self.ai_observer()
            payload = json.dumps({
                "version": 1, "authority": "observation_only",
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "services": services, "ai_pipeline": ai_pipeline,
                "actions": {"start": False, "stop": False, "trigger": False},
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except Exception:
            with self._lock:
                self._cache = None
                self._expires = 0.0
                self._active = False
            return 503, b'{"error":"status_unavailable"}'
        with self._lock:
            self._cache = payload
            self._expires = time.monotonic() + 1.0
            self._active = False
        return 200, payload


class _CapabilitiesSingleFlight:
    """Bound optional local socket reads to one in-flight request only."""

    def __init__(self, observer: Callable[[], dict[str, object]]):
        self.observer = observer
        self._lock = threading.Lock()
        self._active = False
        self._cache: bytes | None = None
        self._expires = 0.0

    @staticmethod
    def _safe_payload(candidate: object) -> bytes:
        if (
            type(candidate) is not dict
            or set(candidate) != {"version", "authority", "operations", "reason"}
            or type(candidate["version"]) is not int
            or candidate["version"] != 1
            or candidate["authority"] != "observation_only"
            or candidate["reason"] not in (
                "broker_unavailable", "cross_updater_exclusion_unqualified"
            )
            or type(candidate["operations"]) is not dict
            or set(candidate["operations"]) != {"pause", "resume", "run_once"}
            or any(type(v) is not bool or v is not False
                   for v in candidate["operations"].values())
        ):
            candidate = _unavailable_capabilities()
        return json.dumps(
            candidate, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")

    def get(self) -> bytes:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now < self._expires:
                return self._cache
            if self._active:
                # Every capability is false; never wait for an already slow
                # privileged-socket query while consuming HTTP worker slots.
                return self._safe_payload(_unavailable_capabilities())
            self._active = True
        try:
            result = self._safe_payload(self.observer())
        except Exception:
            result = self._safe_payload(_unavailable_capabilities())
        with self._lock:
            self._cache = result
            self._expires = time.monotonic() + 1.0
            self._active = False
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = "ObsidianStatus/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        # A per-read socket timeout alone is insufficient: a slow client
        # can drip one byte before every timeout indefinitely. The wrapper
        # enforces a total request-line+headers deadline before parsing.
        self.rfile = _BoundedHeaderReader(self.rfile, self.connection)

    def log_message(self, format: str, *args: object) -> None:
        # Never log a browser URL, user input, exception, or systemd data.
        return

    def _reply(self, status: int, body: bytes = b"", mime: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _origin_allowed(self) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        received_hosts = self.headers.get_all("Host", [])
        received_origins = self.headers.get_all("Origin", [])
        if len(received_hosts) != 1 or len(received_origins) > 1:
            return False
        origin = received_origins[0] if received_origins else None
        return (received_hosts[0] in hosts and (
            origin is None or origin in {f"http://{host}" for host in hosts}
        ))

    def do_GET(self) -> None:
        if not self._origin_allowed():
            self._reply(403, b"Forbidden")
            return
        if self.path == "/":
            self._reply(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/static/style.css":
            self._reply(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
        elif self.path == "/static/app.js":
            self._reply(200, JS.encode("utf-8"), "text/javascript; charset=utf-8")
        elif self.path == "/api/v1/operation-capabilities":
            self._reply(
                200, self.server.capabilities_singleflight.get(),
                "application/json; charset=utf-8",
            )
        elif self.path == "/api/v1/operation-session":
            try:
                session = self.server.preview_csrf.issue()
            except PreviewRequestRejected:
                self._reply(503, b'{"error":"preview_session_unavailable"}',
                            "application/json; charset=utf-8")
                return
            data = json.dumps(
                session, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            self._reply(200, data, "application/json; charset=utf-8")
        elif self.path == "/api/v1/status":
            code, payload = self.server.status_singleflight.get()
            self._reply(code, payload, "application/json; charset=utf-8")
        else:
            self._reply(404, b"Not found")

    def do_HEAD(self) -> None:
        if self.path == "/api/v1/operation-session":
            # A HEAD request has no body to return the token and must not
            # manufacture an otherwise unreadable operator session.
            self._reply(405, b"Method not allowed")
            return
        self.do_GET()

    def _write_origin_allowed(self) -> bool:
        """Mutating HTTP verb policy is stricter than read-only GET."""
        if not self._origin_allowed():
            return False
        received_hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        fetch_site = self.headers.get_all("Sec-Fetch-Site", [])
        return (
            len(received_hosts) == 1 and len(origins) == 1
            and origins[0] == f"http://{received_hosts[0]}"
            and len(fetch_site) <= 1
            and (not fetch_site or fetch_site[0] in ("same-origin", "none"))
        )

    def _read_bounded_json(self) -> bytes:
        """One hard deadline across all fragmented HTTP body reads."""
        lengths = self.headers.get_all("Content-Length", [])
        types = self.headers.get_all("Content-Type", [])
        if (
            len(lengths) != 1
            or len(types) != 1
            or types[0] != "application/json"
            or self.headers.get_all("Transfer-Encoding")
            or self.headers.get_all("Content-Encoding")
            or self.headers.get_all("Expect")
        ):
            raise PreviewRequestRejected("invalid_preview_headers")
        value = lengths[0]
        if not value.isascii() or not value.isdecimal() or len(value) > 4:
            raise PreviewRequestRejected("invalid_preview_headers")
        remaining = int(value)
        if not 1 <= remaining <= MAX_REQUEST_BYTES:
            raise PreviewRequestRejected("invalid_preview_headers")
        chunks = []
        deadline = time.monotonic() + 1.0
        while remaining:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise PreviewRequestRejected("preview_body_timeout")
            try:
                self.connection.settimeout(seconds)
                # read1 returns at most one buffered/OS read, not a
                # potentially indefinite exact-byte read loop.
                part = self.rfile.read1(min(remaining, 256))
            except (OSError, TimeoutError):
                raise PreviewRequestRejected("preview_body_unavailable") from None
            if not part:
                raise PreviewRequestRejected("preview_body_truncated")
            chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)

    def do_POST(self) -> None:
        if self.path != "/api/v1/operation-previews":
            self._reply(405, b"Method not allowed")
            return
        if not self._write_origin_allowed():
            self._reply(403, b'{"error":"origin_rejected"}', "application/json")
            return
        tokens = self.headers.get_all("X-Obsidian-CSRF", [])
        if len(tokens) != 1 or not self.server.preview_csrf.consume(tokens[0]):
            self._reply(403, b'{"error":"csrf_rejected"}', "application/json")
            return
        try:
            request = parse_operation_preview_request(self._read_bounded_json())
        except PreviewRequestRejected:
            self._reply(
                400, b'{"error":"invalid_preview_request"}',
                "application/json",
            )
            return
        try:
            preview = self.server.preview_observer(request)
            preview = strict_browser_preview(preview, request)
        except Exception:
            preview = unavailable_preview()
        data = json.dumps(
            preview, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        self._reply(200, data, "application/json; charset=utf-8")

    do_PUT = lambda self: self._reply(405, b"Method not allowed")
    do_PATCH = do_PUT
    do_DELETE = do_PUT
    do_OPTIONS = do_PUT


def make_server(
    *, port: int = 8370,
    observer: Observer = aggregate_status,
    ai_observer: AIObserver = load_ai_health,
    capabilities_observer: Callable[[], dict[str, object]] = _unavailable_capabilities,
    preview_observer: Callable[[dict[str, object]], dict[str, object]] = lambda _: unavailable_preview(),
) -> ThreadingHTTPServer:
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    server = BoundedDashboardServer(("127.0.0.1", port), Handler)
    server.observer = observer
    server.ai_observer = ai_observer
    server.status_singleflight = _StatusSingleFlight(observer, ai_observer)
    server.capabilities_observer = capabilities_observer
    server.capabilities_singleflight = _CapabilitiesSingleFlight(capabilities_observer)
    server.preview_observer = preview_observer
    server.preview_csrf = OneTimePreviewCsrf()
    server.daemon_threads = True
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental no-effect localhost Dashboard")
    parser.add_argument("--port", type=int, default=8370)
    args = parser.parse_args(argv)
    with make_server(
        port=args.port, capabilities_observer=capabilities_from_socket,
        preview_observer=preview_from_socket,
    ) as server:
        server.serve_forever(poll_interval=0.25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
