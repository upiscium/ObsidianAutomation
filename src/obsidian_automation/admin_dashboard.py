"""Loopback Dashboard; status + deliberately no-effect operation PREVIEW.

The POST preview route is strictly CSRF/Origin-guarded and cannot execute a
systemd operation, write Vault data or grant privileged control. No login is
implemented, so this must remain on loopback/private ingress only.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import ipaddress
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Callable, Sequence

from .admin_broker_socket import capabilities_from_socket, _unavailable_capabilities
from .admin_operation_preview import preview_from_socket
from .admin_operator_http import (
    OperatorHTTPClient, ConfirmationTickets, disabled_capabilities,
    safe_capabilities, safe_operator_receipt, parse_confirm_request,
)
from .admin_operation_preflight import _REQUEST_ID
from .admin_control_queue_socket import DEFAULT_OPERATION_QUEUE_SOCKET
from .admin_operation_web import (
    MAX_REQUEST_BYTES, OneTimePreviewCsrf, PreviewRequestRejected,
    parse_operation_preview_request, strict_browser_preview, unavailable_preview,
)
from .admin_status import aggregate_status
from .admin_ai_status import load_ai_health
from .snapshot_job import SnapshotJob, Invalid as SnapshotInvalid
from .snapshot_setup import validate_nextcloud, GITEA_FIELDS
from .github_daily_job import DailyJob, Invalid as DailyInvalid
from .github_daily_setup import DailySetup, DailySetupError
from .vault_format_job import FormatterJob, FormatterInvalid
from .unified_workflows import workflow_catalog
from .webui_control_get import read_control_catalog
from .webui_control_read import SCHEMA_VERSION as WORKFLOW_CONTROL_SCHEMA

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
        <p class="desc" id="dashboard-mode-detail">読み取り専用の状態表示です。実操作はWorker有効化時のみ可能です。</p></div>
      <button id="refresh" type="button">状態を更新</button>
    </header>
    <section aria-label="システムの説明" class="notice">
      <strong id="dashboard-mode-title">Observation only</strong>
      <span id="dashboard-mode-description">現在は状態確認のみ可能です。サービス操作には権限付きWorkerが必要です。Vaultや設定の変更はできません。</span>
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
    <section class="pipeline" aria-label="5つの自動化機能">
      <h2>自動化機能 B1〜B5</h2>
      <p><a href="/workflow-control">B1–B5 read-only live observations</a>（操作・設定変更なし）</p>
      <p id="workflow-summary">共通WebUIへの接続状態を取得しています…</p>
      <p class="small">「統合状態」が未接続の機能は、この画面から実行できません。各機能の状態と最後の結果を確認してください。</p>
      <div id="workflow-catalog" class="cards" aria-live="polite"></div>
    </section>
    <section id="services" class="cards" aria-label="既存systemdサービス一覧" aria-live="polite"></section>
    <section class="pipeline" aria-label="共通Nextcloud接続">
      <h2>Nextcloud（共有アカウント）</h2>
      <p id="nextcloud-state">未設定 / 状態確認中</p>
      <form id="nextcloud-form" autocomplete="off">
        <label>WebDAV URL <input name="webdav_url" type="url" required
                placeholder="https://cloud.example/remote.php/dav/files/user/"></label>
        <label>Vault folder <input name="vault_folder" required placeholder="Vault"></label>
        <label>Nextcloud user <input name="nextcloud_user" required></label>
        <label>Nextcloud app password
          <input name="nextcloud_password" type="password" autocomplete="off" required></label>
        <button type="submit">Nextcloudのみ接続・検証</button>
      </form>
      <p id="nextcloud-message" role="status"></p>
      <p>GitHub DailyのPreviewにはNextcloudとGitHubだけで十分です。GiteaはSnapshot利用時に追加します。</p>
    </section>
    <section class="pipeline" aria-label="Snapshot接続設定">
      <h2>Snapshot用 Gitea接続（必要な場合のみ）</h2>
      <p id="connection-state">状態を確認中</p>
      <form id="connections-form" autocomplete="off">
        <label>Nextcloud WebDAV URL <input name="webdav_url" type="url" required placeholder="https://cloud.example/remote.php/dav/files/user/"></label>
        <label>Vault folder <input name="vault_folder" required placeholder="Vault"></label>
        <label>Nextcloud user <input name="nextcloud_user" required></label>
        <label>Nextcloud app password <input name="nextcloud_password" type="password" required></label>
        <label>Private Gitea HTTPS .git URL <input name="git_url" type="url" required></label>
        <label>Gitea user <input name="git_user" required></label>
        <label>Gitea token <input name="git_token" type="password" required></label>
        <label>Branch <input name="branch" value="main" required></label>
        <button type="submit">接続を検証して保存</button>
      </form>
      <p id="connection-message" role="status"></p>
    </section>
    <section id="snapshot-controls" class="pipeline" aria-label="Vault Snapshot">
      <h2>Vault Snapshot → private Gitea</h2>
      <p id="snapshot-state">未設定 / 状態未確認</p>
      <p id="snapshot-last">最終結果: —</p><p id="snapshot-due">次回: —</p>
      <label>定期実行 <input id="snapshot-enabled" type="checkbox" disabled></label>
      <label>間隔（秒） <input id="snapshot-interval" type="number" min="60" max="86400" disabled></label>
      <button id="snapshot-save" type="button" disabled>設定を保存</button>
      <button id="snapshot-run" type="button" disabled>Run now</button>
      <p id="snapshot-message" role="status"></p>
    </section>
    <section id="github-daily" class="pipeline" aria-label="GitHub Daily Preview">
      <h2 id="daily-title">GitHub Daily（LLM不使用・Previewのみ）</h2>
      <p id="daily-mode">Daily Note反映は未有効化です。</p>
      <p id="daily-connection-state">GitHub接続を確認中</p>
      <form id="daily-connections-form" autocomplete="off">
        <label>GitHub Access Token
          <input name="github_token" type="password" autocomplete="off" required
                 placeholder="GitHub token（画面には再表示されません）"></label>
        <button id="daily-connections-save" type="submit" disabled>GitHub接続を検証して保存</button>
      </form>
      <p id="daily-connection-message" role="status"></p>
      <p>B2で設定したNextcloudアカウントを共有してProjectノートを読み取ります。新しいNextcloudアカウントは不要です。</p>
      <p id="daily-state">未設定 / 状態未確認</p>
      <p id="daily-last">最終結果: —</p><p id="daily-due">次回: —</p>
      <label>定期実行 <input id="daily-enabled" type="checkbox" disabled></label>
      <label>間隔（秒） <input id="daily-interval" type="number" min="3600" max="86400" disabled></label>
      <button id="daily-save" type="button" disabled>設定を保存</button>
      <button id="daily-run" type="button" disabled>Run now</button>
      <button id="daily-refresh" type="button">結果を更新</button>
      <p id="daily-message" role="status"></p>
      <p>GitHub進捗はLLMを使わず決定的に生成します。Writer有効時も前日JSTの既存Daily Noteの該当セクションだけを更新し、結果を検証します。</p>
    </section>
    <section id="vault-formatter" class="pipeline" aria-label="Vault Formatter">
      <h2>Vault Formatter / 98-System</h2>
      <p id="formatter-state">Formatterの状態を取得中</p>
      <p id="formatter-capability">Live Vaultへの書戻しは無効です。</p>
      <p id="formatter-last">最終結果: —</p>
      <p id="formatter-skip-reasons" aria-live="polite">スキップ内訳: —</p>
      <p id="formatter-due">次回: —</p>
      <label>モード
        <select id="formatter-mode" disabled>
          <option value="off">Off</option>
          <option value="dry-run">Dry-run（読取のみ）</option>
          <option value="auto-apply" disabled>Auto apply（別途本番承認）</option>
        </select>
      </label>
      <label>定期実行間隔（秒）
        <input id="formatter-interval" type="number" min="300" max="86400" value="3600" disabled>
      </label>
      <button id="formatter-save" type="button" disabled>設定を保存</button>
      <button id="formatter-run" type="button" disabled>Run Once（Dry-run）</button>
      <button id="formatter-refresh" type="button">結果を更新</button>
      <p id="formatter-message" role="status"></p>
      <p>変更候補と件数だけを表示します。元ファイルや秘密情報は画面に送信しません。</p>
    </section>
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
        <button id="preview-confirm" type="button" disabled
          aria-label="実際のサービス操作は未対応">実行不可</button>
        <button id="preview-receipt-check" type="button" disabled>結果を再取得</button>
      </div>
      <p id="preview-message" role="status" aria-live="polite">状態を取得してからプレビューを行えます。</p>
      <p id="preview-receipt" role="status" aria-live="polite">操作の受付記録はありません。</p>
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
.card h2,.card h3 { margin:0; font-size:1.08rem }
.card a { color:var(--accent); display:inline-block; margin-top:8px; font-weight:650 }
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
#connections-form { display:grid; gap:10px; max-width:620px }
#connections-form label { display:grid; gap:3px }
#connections-form input { padding:7px; max-width:100%; background:var(--surface); color:var(--ink); border:1px solid var(--border) }
.operation-controls label { display:flex; flex:1 1 170px; flex-direction:column; gap:5px; min-width:0 }
.operation-controls select { width:100%; min-height:42px; border:1px solid var(--border);
  background:var(--surface); color:var(--ink); border-radius:9px; font:inherit; padding:7px 9px }
.operation-controls button { flex:0 0 auto; }
#preview-message, #preview-receipt { overflow-wrap:anywhere; }
#preview-confirm:not(:disabled) { border:2px solid currentColor; }
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
const workflowLabels = {
  pending:"共通Job未接続", connected:"このWebUIに接続済み",
  not_attached:"このサーバーでは未接続", unavailable:"状態取得不可",
  configured:"設定済み", missing:"未設定", not_observed:"未観測",
  on:"On", off:"Off", not_applicable:"定期実行は不要",
  saved:"リモート保存確認済み", no_changes:"変更なし",
  failed:"失敗", unconfirmed:"結果不明", queued:"受付済み",
  running:"実行中", preview_ready:"Preview生成済み（Vault未反映）",
  published:"Daily Note反映確認済み", publish_failed:"Preview生成済み・反映未確認",
  unknown:"未確認",
};
function catalogLine(name, state) {
  const node = line(name, "unknown");
  node.lastElementChild.textContent = workflowLabels[state] || "未確認";
  return node;
}
function workflowCard(w) {
  const box = document.createElement("article");
  box.className = "card";
  const heading = document.createElement("h3");
  heading.textContent = w.id.toUpperCase() + " " + w.label;
  const detail = document.createElement("p");
  detail.className = "small";
  detail.textContent = w.note;
  box.append(heading, detail, catalogLine("統合状態", w.integration),
    catalogLine("接続設定", w.configuration),
    catalogLine("定期実行", w.schedule),
    catalogLine("最終結果", w.last_result || "not_observed"));
  if (w.id === "b2" && w.control_available === true) {
    const link = document.createElement("a");
    link.href = "#snapshot-controls";
    link.textContent = "Snapshot操作へ";
    box.append(link);
  }
  if (w.id === "b4" && w.control_available === true) {
    const link = document.createElement("a");
    link.href = "#github-daily";
    link.textContent = "GitHub Daily Preview操作へ";
    box.append(link);
  }
  return box;
}
let workflowRefreshGeneration = 0;
async function workflowCatalogRefresh() {
  const generation = ++workflowRefreshGeneration;
  try {
    const response = await fetch("/api/v1/workflows", {
      cache:"no-store", credentials:"same-origin",
    });
    if (!response.ok) throw Error("workflows_unavailable");
    const data = await response.json();
    if (generation !== workflowRefreshGeneration) return;
    const ids = ["b1","b2","b3","b4","b5"];
    const counts = {connected:0, pending:0, not_attached:0, unavailable:0};
    if (data.version !== 1 || data.authority !== "observation_only"
        || !Array.isArray(data.workflows) || data.workflows.length !== 5
        || data.workflows.some((w, i) =>
          w.id !== ids[i] || typeof w.label !== "string"
          || typeof w.note !== "string" || typeof w.control_available !== "boolean"
          || !Object.hasOwn(counts, w.integration)
          || !Object.hasOwn(workflowLabels, w.configuration)
          || !Object.hasOwn(workflowLabels, w.schedule))) {
      throw Error("invalid_workflow_projection");
    }
    $("workflow-catalog").replaceChildren(...data.workflows.map(workflowCard));
    for (const w of data.workflows) ++counts[w.integration];
    $("workflow-summary").textContent =
      "WebUI接続済み " + counts.connected + "/5機能 ・ 共通Job未接続 " + counts.pending +
      "機能 ・ このサーバーでは未接続 " + counts.not_attached +
      "機能 ・ 状態取得不可 " + counts.unavailable +
      "機能。旧systemdの稼働状況とは別に判定します。";
  } catch {
    if (generation !== workflowRefreshGeneration) return;
    $("workflow-catalog").replaceChildren();
    $("workflow-summary").textContent = "統合状況を取得できません（古い表示は消去しました）";
  }
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
let snapshotSettings = null;
let nextcloudReady = false;
async function nextcloudRefresh() {
  try {
    const r = await fetch("/api/v1/nextcloud/connections", {
      cache:"no-store", credentials:"same-origin"});
    if (!r.ok) throw Error("unavailable");
    const data = await r.json();
    if (data.version !== 1 || data.scope !== "shared_nextcloud"
        || typeof data.configured !== "boolean") throw Error("schema");
    nextcloudReady = data.configured;
    $("nextcloud-state").textContent = nextcloudReady
      ? "Nextcloud接続済み（資格情報は非表示、B2/B4共有）" : "未設定";
    const snapshotForm = $("connections-form");
    for (const field of ["webdav_url","vault_folder","nextcloud_user","nextcloud_password"]) {
      const input = snapshotForm.elements.namedItem(field);
      input.disabled = nextcloudReady;
      input.required = !nextcloudReady;
    }
  } catch {
    nextcloudReady = false;
    $("nextcloud-state").textContent = "Nextcloud接続状態を取得できません";
    for (const field of ["webdav_url","vault_folder","nextcloud_user","nextcloud_password"]) {
      const input = $("connections-form").elements.namedItem(field);
      input.disabled = false;
      input.required = true;
    }
  }
}
$("nextcloud-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button");
  submit.disabled = true;
  try {
    await snapshotPost("/api/v1/nextcloud/connections", Object.fromEntries(new FormData(form)));
    $("nextcloud-message").textContent = "Nextcloud接続を検証して保存しました。GiteaなしでGitHub Dailyを設定できます。";
    form.reset();
  } catch {
    $("nextcloud-message").textContent = "Nextcloud接続を保存できませんでした。定期実行の状態を確認してください。";
  }
  submit.disabled = false;
  await nextcloudRefresh();
  await connectionRefresh();
  await snapshotRefresh();
  await dailyConnectionRefresh();
  await dailyRefresh();
  await formatterRefresh();
  await workflowCatalogRefresh();
});
async function connectionRefresh() {
  try {
    const r = await fetch("/api/v1/snapshot/connections", {cache:"no-store", credentials:"same-origin"});
    if (!r.ok) throw Error("unavailable");
    const data = await r.json();
    $("connection-state").textContent = data.configured ? "接続設定済み（秘密情報は表示されません）" : "未設定";
  } catch { $("connection-state").textContent = "接続状態を確認できません"; }
}
$("connections-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const body = Object.fromEntries(new FormData(form));
    await snapshotPost("/api/v1/snapshot/connections", body);
    $("connection-message").textContent = "接続を確認して保存しました。定期実行を Off にしました。Git 書き込みは初回の保存成功まで未検証です。";
    form.reset();
  } catch { $("connection-message").textContent = "検証または保存に失敗しました。以前の設定は変更されていません。"; }
  button.disabled = false;
  await nextcloudRefresh();
  await connectionRefresh();
  await snapshotRefresh();
  await dailyConnectionRefresh();
  await dailyRefresh();
  await formatterRefresh();
  await workflowCatalogRefresh();
});
async function snapshotRefresh() {
  try {
    const r = await fetch("/api/v1/snapshot", {cache:"no-store", credentials:"same-origin"});
    if (!r.ok) throw Error("unavailable");
    const data = await r.json();
    if (data.version !== 1 || data.job !== "vault_snapshot") throw Error("schema");
    snapshotSettings = data.settings;
    $("snapshot-enabled").checked = data.settings.enabled;
    $("snapshot-interval").value = data.settings.interval_seconds;
    $("snapshot-state").textContent = data.settings.configured
      ? (data.settings.enabled ? "定期実行 On" : "定期実行 Off") : "未設定（実行不可）";
    if (data.settings.configured) $("connection-state").textContent =
      "Gitea private / 読み取り検証済み（保存時）。書き込み: " +
      (data.settings.gitea_write === "verified" ? "最終 push 検証済み" : "未検証（初回の保存成功まで）");
    $("snapshot-last").textContent = data.last_run
      ? "最終結果: " + data.last_run.status + " / " + data.last_run.stage + " / " + (data.last_run.completed || data.last_run.created)
      : "最終結果: なし";
    $("snapshot-due").textContent = "次回: " + (data.settings.next_due ? new Date(data.settings.next_due * 1000).toLocaleString() : "なし");
    for (const id of ["snapshot-enabled","snapshot-interval","snapshot-save","snapshot-run"])
      $(id).disabled = !data.settings.configured;
  } catch {
    snapshotSettings = null;
    $("snapshot-state").textContent = "状態未確認（操作不可）";
    $("snapshot-last").textContent = "最終結果: —";
    $("snapshot-due").textContent = "次回: —";
    for (const id of ["snapshot-enabled","snapshot-interval","snapshot-save","snapshot-run"])
      $(id).disabled = true;
  } finally {
    await workflowCatalogRefresh();
  }
}
async function snapshotPost(path, body) {
  const token = await newCsrfToken();
  const response = await fetch(path, {method:"POST", cache:"no-store", credentials:"same-origin",
    headers:{"Content-Type":"application/json", "X-Obsidian-CSRF":token}, body:JSON.stringify(body)});
  if (!response.ok) throw Error("request_failed");
  return response.json();
}
$("snapshot-save").addEventListener("click", async () => {
  if (!snapshotSettings) return;
  $("snapshot-save").disabled = true;
  try {
    await snapshotPost("/api/v1/snapshot/settings", {enabled:$("snapshot-enabled").checked,
      interval_seconds:Number($("snapshot-interval").value),
      source_name:snapshotSettings.source_name, destination_name:snapshotSettings.destination_name});
    $("snapshot-message").textContent = "設定を保存しました（ジョブは開始していません）";
  } catch { $("snapshot-message").textContent = "保存できませんでした"; }
  await snapshotRefresh();
});
$("snapshot-run").addEventListener("click", async () => {
  $("snapshot-run").disabled = true;
  try {
    const result = await snapshotPost("/api/v1/snapshot/runs", {request_id:crypto.randomUUID().replaceAll("-", "")});
    $("snapshot-message").textContent = result.run_id
      ? "受付: " + result.run_id + "（結果は未確認。更新して確認）" : "実行中または未設定";
  } catch { $("snapshot-message").textContent = "受付不明。二重実行せず管理者に確認してください"; }
  await snapshotRefresh();
});
async function dailyConnectionRefresh() {
  try {
    const r = await fetch("/api/v1/daily/connections", {
      cache:"no-store",credentials:"same-origin"});
    if (!r.ok) throw Error("unavailable");
    const data = await r.json();
    if (data.version !== 1) throw Error("schema");
    $("daily-connection-state").textContent = data.configured
      ? "GitHub接続済み（秘密情報は非表示、Projectミラーは実行時に読み取り）"
      : data.nextcloud_source === "configured"
        ? "GitHub未設定（B2のNextcloud接続を再利用）"
        : "先にNextcloud・Gitea接続設定を保存してください";
    $("daily-connections-save").disabled = data.nextcloud_source !== "configured";
  } catch {
    $("daily-connection-state").textContent = "GitHub接続状態を確認できません";
    $("daily-connections-save").disabled = true;
  }
}
$("daily-connections-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  $("daily-connections-save").disabled = true;
  try {
    await snapshotPost("/api/v1/daily/connections", Object.fromEntries(new FormData(form)));
    $("daily-connection-message").textContent = "GitHubを検証して保存しました。定期PreviewはOffです。";
    form.reset();
  } catch {
    $("daily-connection-message").textContent = "GitHubの検証または保存に失敗しました";
  }
  await dailyConnectionRefresh();
  await dailyRefresh();
  await formatterRefresh();
  await workflowCatalogRefresh();
});
async function formatterRefresh() {
  try {
    const response = await fetch("/api/v1/formatter", {
      cache:"no-store", credentials:"same-origin"
    });
    if (!response.ok) throw Error("formatter_unavailable");
    const data = await response.json();
    if (data.version !== 1 || data.job !== "vault_formatter"
        || !data.settings
        || !["off","dry-run","auto-apply"].includes(data.settings.mode)
        || typeof data.settings.write_capable !== "boolean")
      throw Error("formatter_schema");
    const settings = data.settings;
    $("formatter-mode").value = settings.mode;
    $("formatter-mode").querySelector('option[value="auto-apply"]').disabled =
      !settings.write_capable;
    $("formatter-interval").value = settings.interval_seconds;
    $("formatter-state").textContent = settings.configured
      ? "状態: " + settings.mode + " / 対象: " + settings.scope
      : "Nextcloud未設定（実行不可）";
    $("formatter-capability").textContent = settings.write_capable
      ? "Auto applyは独立承認後のWriterのみです。Run OnceはDry-runです。"
      : "Live Vault書戻しは無効。Run Onceは常にDry-runです。";
    const last = data.last_run;
    $("formatter-last").textContent = last
      ? "最終結果: " + last.status + " / 対象変更候補 "
        + (last.would_change ?? "—") + "件 / スキップ "
        + (last.skipped ?? "—") + "件"
        + (last.writer_outcome ? " / Writer: " + last.writer_outcome : "")
        + (last.reason_code ? " / 理由: " + last.reason_code : "")
      : "最終結果: なし";
    const skipBreakdown = last?.skip_reasons;
    if (last && ["queued","running"].includes(last.status)) {
      $("formatter-skip-reasons").textContent = "スキップ内訳: 実行中";
    } else if (last && skipBreakdown && typeof skipBreakdown === "object"
               && !Array.isArray(skipBreakdown)) {
      const safeReasons = Object.entries(skipBreakdown).filter(([reason, count]) =>
        /^[a-z][a-z0-9-]{1,63}$/.test(reason)
        && Number.isSafeInteger(count) && count > 0);
      $("formatter-skip-reasons").textContent = "スキップ内訳: " +
        (safeReasons.length
          ? safeReasons.sort(([a],[b]) => a.localeCompare(b))
            .map(([reason,count]) => reason + " " + count + "件").join(" / ")
          : "なし");
    } else if (last && last.skipped > 0) {
      $("formatter-skip-reasons").textContent = "スキップ内訳: 旧記録では未取得";
    } else {
      $("formatter-skip-reasons").textContent = last
        ? "スキップ内訳: 未取得" : "スキップ内訳: —";
    }
    $("formatter-due").textContent = "次回: " +
      (settings.next_due ? new Date(settings.next_due * 1000).toLocaleString() : "なし");
    for (const id of ["formatter-mode","formatter-interval","formatter-save","formatter-run"])
      $(id).disabled = !settings.configured;
    if (last && ["queued","running"].includes(last.status))
      $("formatter-run").disabled = true;
  } catch {
    $("formatter-state").textContent = "状態未確認（操作不可）";
    $("formatter-capability").textContent = "Writerの有効状態を確認できません";
    $("formatter-last").textContent = "最終結果: —";
    $("formatter-skip-reasons").textContent = "スキップ内訳: —";
    $("formatter-due").textContent = "次回: —";
    for (const id of ["formatter-mode","formatter-interval","formatter-save","formatter-run"])
      $(id).disabled = true;
  }
}
$("formatter-save").addEventListener("click", async () => {
  $("formatter-save").disabled = true;
  try {
    await snapshotPost("/api/v1/formatter/settings", {
      mode:$("formatter-mode").value,
      interval_seconds:Number($("formatter-interval").value)
    });
    $("formatter-message").textContent = "Formatter設定を保存しました。実行はまだ開始していません。";
  } catch {
    $("formatter-message").textContent = "保存できませんでした（設定・権限を確認してください）";
  }
  await formatterRefresh();
});
$("formatter-run").addEventListener("click", async () => {
  $("formatter-run").disabled = true;
  try {
    const data = await snapshotPost("/api/v1/formatter/runs", {
      request_id:crypto.randomUUID().replaceAll("-", "")
    });
    $("formatter-message").textContent = data.run_id
      ? "Dry-run受付: " + data.run_id + "。結果を更新して確認してください。"
      : "実行中または未設定";
  } catch {
    $("formatter-message").textContent =
      "受付は未確認です。二重実行せず、結果を更新して確認してください。";
  }
  await formatterRefresh();
});
$("formatter-refresh").addEventListener("click", formatterRefresh);
async function dailyRefresh() {
  try {
    const r = await fetch("/api/v1/daily", {cache:"no-store",credentials:"same-origin"});
    if (!r.ok) throw Error("unavailable");
    const data = await r.json();
    if (data.version !== 1 || data.job !== "github_daily_preview"
        || (data.publication_enabled !== undefined
            && typeof data.publication_enabled !== "boolean")) throw Error("schema");
    const publicationEnabled = data.publication_enabled === true;
    if ($("daily-title")) $("daily-title").textContent = publicationEnabled
      ? "GitHub Daily（決定的生成・Writer有効）"
      : "GitHub Daily（LLM不使用・Previewのみ）";
    if ($("daily-mode")) $("daily-mode").textContent = publicationEnabled
      ? "書き込み対象: 既存Daily NoteのProject Progressのみ。結果は確認後に表示します。"
      : "Daily Note反映は未有効化です。";
    $("daily-enabled").checked = data.settings.enabled;
    $("daily-interval").value = data.settings.interval_seconds;
    $("daily-state").textContent = data.settings.configured
      ? (data.settings.enabled ? "定期Preview On" : "定期Preview Off") : "GitHub接続未設定（実行不可）";
    $("daily-last").textContent = data.last_run
      ? "最終結果: " + data.last_run.status
        + (data.last_run.status === "published"
            ? "（Daily Note反映確認済み: " + data.last_run.publication_outcome + "）"
            : data.last_run.status === "publish_failed"
            ? "（Preview生成済み・反映未確認）"
            : data.last_run.status === "preview_ready" ? "（Vault未反映）" : "")
        + (data.last_run.date_jst ? " / " + data.last_run.date_jst : "")
      : "最終結果: なし";
    $("daily-due").textContent = "次回: " +
      (data.settings.next_due ? new Date(data.settings.next_due * 1000).toLocaleString() : "なし");
    for (const id of ["daily-enabled","daily-interval","daily-save","daily-run"])
      $(id).disabled = !data.settings.configured;
    if (data.last_run && ["queued","running"].includes(data.last_run.status))
      $("daily-run").disabled = true;
  } catch {
    if ($("daily-title")) $("daily-title").textContent = "GitHub Daily（状態未確認）";
    if ($("daily-mode")) $("daily-mode").textContent = "Writerの現在の有効状態を確認できません";
    $("daily-state").textContent = "状態未確認（操作不可）";
    $("daily-last").textContent = "最終結果: —";
    $("daily-due").textContent = "次回: —";
    for (const id of ["daily-enabled","daily-interval","daily-save","daily-run"])
      $(id).disabled = true;
  }
}
$("daily-save").addEventListener("click", async () => {
  $("daily-save").disabled = true;
  try {
    await snapshotPost("/api/v1/daily/settings", {
      enabled:$("daily-enabled").checked,
      interval_seconds:Number($("daily-interval").value)});
    $("daily-message").textContent =
      "定期実行設定を保存しました。実行モードは起動設定で固定されています。";
  } catch { $("daily-message").textContent = "設定の保存に失敗しました"; }
  await dailyRefresh();
  await formatterRefresh();
  await workflowCatalogRefresh();
});
async function pollDailyResult(runId) {
  if (!/^[a-f0-9]{32}$/.test(runId)) throw Error("invalid_run_id");
  for (let attempt = 0; attempt < 90; ++attempt) {
    const response = await fetch("/api/v1/daily/runs/" + runId, {
      cache:"no-store",credentials:"same-origin"});
    if (!response.ok) throw Error("run_status_unavailable");
    const receipt = await response.json();
    if (receipt.run_id !== runId || typeof receipt.status !== "string")
      throw Error("run_status_invalid");
    if (!["queued","running"].includes(receipt.status)) {
      const result = ["preview_ready","published","publish_failed",
                      "failed","unconfirmed"].includes(receipt.status)
        ? receipt.status : "unknown";
      $("daily-message").textContent = result === "preview_ready"
        ? "Preview生成完了（Vaultへは未反映）"
        : result === "published" ? "Daily Noteへの反映を再取得して確認しました"
        : result === "publish_failed" ? "Preview生成済み・Daily Note反映に失敗または未確認"
        : "実行結果: " + result;
      await dailyRefresh();
      await workflowCatalogRefresh();
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  $("daily-message").textContent = "処理中または結果未確定。「結果を更新」で再確認してください";
  await dailyRefresh();
  await workflowCatalogRefresh();
}
$("daily-run").addEventListener("click", async () => {
  $("daily-run").disabled = true;
  let runId = null;
  try {
    const data = await snapshotPost("/api/v1/daily/runs", {
      request_id:crypto.randomUUID().replaceAll("-", "")});
    runId = data.run_id;
    $("daily-message").textContent = runId
      ? "受付: " + runId + "（GitHub進捗を処理中）" : "実行中または未設定";
  } catch { $("daily-message").textContent = "受付未確認。同じ処理を自動で再投入しません"; }
  await dailyRefresh();
  await workflowCatalogRefresh();
  if (runId) {
    try {
      await pollDailyResult(runId);
    } catch {
      $("daily-message").textContent = "結果の取得に失敗しました。「結果を更新」で再確認してください（自動再投入はしません）";
      await dailyRefresh();
      await workflowCatalogRefresh();
    }
  }
});
$("daily-refresh").addEventListener("click", async () => {
  await dailyRefresh();
  await workflowCatalogRefresh();
});
let previewGeneration = 0;
let pendingConfirmation = null;
let latestOperationId = null;
let enabledOperations = {pause:false, resume:false, run_once:false};
function invalidateConfirmation() {
  pendingConfirmation = null;
  $("preview-confirm").disabled = true;
  $("preview-confirm").textContent = "実行不可";
  $("preview-confirm").setAttribute("aria-label", "実際のサービス操作は未対応");
}
function updateOperatorMode() {
  const available = ["pause", "resume", "run_once"]
    .some(action => enabledOperations[action] === true);
  const heading = $("dashboard-mode-title");
  const description = $("dashboard-mode-description");
  const detail = $("dashboard-mode-detail");
  if (heading) heading.textContent = available
    ? "サービス操作が有効" : "Observation only";
  if (description) description.textContent = available
    ? "権限付きWorkerが利用可能な場合、固定サービスのPause・Resume・Run onceを確認後に実行できます。Vaultと設定の変更はできません。"
    : "現在は状態確認のみ可能です。サービス操作には権限付きWorkerが必要です。Vaultや設定の変更はできません。";
  if (detail) detail.textContent = available
    ? "管理操作はWorkerの現在の状態に依存します。実行時にも再検証します。"
    : "読み取り専用の状態表示です。実操作はWorker有効化時のみ可能です。";
}
async function fetchOperatorReadiness() {
  try {
    const response = await fetch("/api/v1/operator-ready", {
      cache:"no-store", credentials:"same-origin",
    });
    if (!response.ok) throw Error("operator_unavailable");
    const cap = await response.json();
    if (cap.version !== 1 || cap.authority !== "operator_ready"
        || cap.reason !== "worker_ready"
        || !cap.operations
        || !["pause","resume","run_once"].every(x => typeof cap.operations[x] === "boolean")) {
      throw Error("operator_not_ready");
    }
    enabledOperations = cap.operations;
  } catch {
    enabledOperations = {pause:false, resume:false, run_once:false};
  } finally {
    updateOperatorMode();
  }
}
async function refresh() {
  await nextcloudRefresh();
  await connectionRefresh();
  await snapshotRefresh();
  await dailyConnectionRefresh();
  await dailyRefresh();
  await formatterRefresh();
  ++previewGeneration; // Invalidate any in-flight operation preview.
  invalidateConfirmation();
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
    await fetchOperatorReadiness();
    $("preview-message").textContent = Object.values(enabledOperations).some(Boolean)
      ? "プレビュー結果を確認した後、操作を確定できます。"
      : "操作をプレビューできます。実行は現在無効です。";
    $("updated").textContent = "観測日時: " + data.generated_at;
    $("count").textContent = data.services.length + " services";
  } catch {
    // Previous observations must never remain visible as current on errors.
    $("services").replaceChildren();
    $("preview-service").replaceChildren();
    $("preview-service").disabled = true;
    $("preview-check").disabled = true;
    invalidateConfirmation();
    enabledOperations = {pause:false, resume:false, run_once:false};
    updateOperatorMode();
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
  invalidateConfirmation();
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
      if (result.status === "compatible"
          && enabledOperations[action] === true
          && typeof result.confirmation_ticket === "string") {
        pendingConfirmation = {request, ticket:result.confirmation_ticket};
        $("preview-confirm").disabled = false;
        $("preview-confirm").textContent = "確認して実行";
        $("preview-confirm").setAttribute("aria-label", "プレビューした操作を確認して実行");
        $("preview-message").textContent =
          "現在の観測では実行候補です。ボタンで対象と操作を最終確認してください。";
      } else {
        $("preview-message").textContent = result.status === "compatible"
          ? "状態は適合していますが、権限付きWorkerは実行可能ではありません。"
          : "操作の状態検証が完了しませんでした。操作は実行されていません。";
      }
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
    invalidateConfirmation();
    $("preview-message").textContent = "対象・操作が変わりました。プレビューをやり直してください。";
    $("preview-check").disabled = $("preview-service").disabled;
  });
}
$("preview-check").addEventListener("click", previewOperation);

async function newCsrfToken() {
  const response = await fetch("/api/v1/operation-session", {
    cache:"no-store", credentials:"same-origin",
  });
  if (!response.ok) throw Error("csrf_unavailable");
  const body = await response.json();
  if (body.version !== 1 || body.authority !== "preview_only"
      || body.can_execute !== false || typeof body.csrf_token !== "string") {
    throw Error("invalid_csrf");
  }
  return body.csrf_token;
}
function receiptText(result) {
  const status = result && typeof result.state === "string" ? result.state : "";
  switch (status) {
    case "accepted": return "受付済み・実行待ち";
    case "processing": return "systemdへ操作要求中";
    case "completed": return "タイマー操作が完了しました";
    case "systemd_job_accepted": return "systemdジョブを受付済み（処理の成功は未確認）";
    case "indeterminate": return "結果不明：自動再実行せず管理者確認が必要";
    case "completed_pending_cleanup":
    case "systemd_job_accepted_pending_cleanup":
      return "処理は記録済みですが監査データの後片付けが未完了です";
    default: return "操作結果を確認できません。要求IDから再照会してください。";
  }
}
async function refreshReceipt() {
  if (!latestOperationId) return;
  $("preview-receipt-check").disabled = true;
  try {
    const response = await fetch("/api/v1/operations/" + latestOperationId, {
      cache:"no-store", credentials:"same-origin",
    });
    if (!response.ok) throw Error("receipt_unavailable");
    const receipt = await response.json();
    if (receipt.version !== 1 || receipt.request_id !== latestOperationId
        || receipt.can_execute === true) throw Error("untrusted_receipt");
    $("preview-receipt").textContent = "要求ID " + latestOperationId + " — " + receiptText(receipt);
  } catch {
    $("preview-receipt").textContent = "要求ID " + latestOperationId +
      " — 取得失敗。受付された可能性があるため新しいIDで再実行せず照会してください。";
  } finally {
    $("preview-receipt-check").disabled = false;
  }
}
$("preview-receipt-check").addEventListener("click", refreshReceipt);

async function confirmOperation() {
  const choice = pendingConfirmation;
  if (!choice) return;
  if ($("preview-service").value !== choice.request.service_id
      || $("preview-action").value !== choice.request.action) return;
  const description = choice.request.service_id + " に " + choice.request.action +
    " を実行します。既存の処理へ影響する可能性があります。確定しますか？";
  if (!window.confirm(description)) return;
  invalidateConfirmation(); // prevent double clicks while request in flight
  latestOperationId = choice.request.request_id;
  $("preview-receipt-check").disabled = false;
  $("preview-receipt").textContent = "要求ID " + latestOperationId + " — 受付を確認中";
  try {
    const token = await newCsrfToken();
    const response = await fetch("/api/v1/operations", {
      method:"POST", cache:"no-store", credentials:"same-origin",
      headers:{"Content-Type":"application/json", "X-Obsidian-CSRF":token},
      body:JSON.stringify({
        ...choice.request, confirmation_ticket:choice.ticket, confirm:true,
      }),
    });
    if (!response.ok) throw Error("operation_unavailable");
    const result = await response.json();
    if (result.version !== 1) throw Error("invalid_response");
    if (typeof result.error === "string") {
      $("preview-receipt").textContent = "要求ID " + latestOperationId +
        " — 操作結果を確認できません。管理者に要求IDを提示してください。";
    } else if (result.request_id === latestOperationId && result.can_execute === false) {
      $("preview-receipt").textContent = "要求ID " + latestOperationId + " — " + receiptText(result);
    } else throw Error("untrusted_response");
  } catch {
    $("preview-receipt").textContent = "要求ID " + latestOperationId +
      " — 応答不明です。二重実行を避けるため要求IDから照会してください。";
  }
  await refreshReceipt();
}
$("preview-confirm").addEventListener("click", confirmOperation);
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


# Exact request targets and resource names only. No URL decoding, directory
# traversal, generic static handler, demo entrypoint, or runtime initialization.
WORKFLOW_UI_ASSETS = {
    "/workflow-control": ("live.html", "text/html; charset=utf-8"),
    "/static/workflow-control/model.js": ("model.js", "text/javascript; charset=utf-8"),
    "/static/workflow-control/live.js": ("live.js", "text/javascript; charset=utf-8"),
    "/static/workflow-control/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


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
        address, port = self.server.server_address[:2]
        # Host and Origin must match the actual interface this one Dashboard
        # bound to. Never accept arbitrary Host values or proxy wildcards.
        hosts = ({f"127.0.0.1:{port}", f"localhost:{port}"}
                 if address == "127.0.0.1" else {f"{address}:{port}"})
        received_hosts = self.headers.get_all("Host", [])
        received_origins = self.headers.get_all("Origin", [])
        if len(received_hosts) != 1 or len(received_origins) > 1:
            return False
        origin = received_origins[0] if received_origins else None
        return (received_hosts[0] in hosts and (
            origin is None or origin in {f"http://{host}" for host in hosts}
        ))

    def _operator_network_allowed(self) -> bool:
        """LAN/VPN-only operator console, with no per-user Web login.

        The Web server is bound to loopback. A trusted reverse proxy/firewall
        must restrict external access to LAN and authenticated VPN peers.
        Every permitted network client is an operator under the accepted
        deployment trust model. Reject obsolete client-supplied ingress
        capability headers, but retain strict Host/Origin/CSRF controls.
        """
        return (
            self.server.operator_ui_enabled is True
            and self.server.server_address[0] == "127.0.0.1"
            and not self.headers.get_all("X-Obsidian-Private-Ingress", [])
        )

    def _operator_capabilities(self) -> dict[str, object]:
        if not self._operator_network_allowed():
            return disabled_capabilities()
        try:
            return safe_capabilities(self.server.operator.capabilities())
        except Exception:
            return disabled_capabilities()

    def do_GET(self) -> None:
        if not self._origin_allowed():
            self._reply(403, b"Forbidden")
            return
        if self.path in WORKFLOW_UI_ASSETS:
            # BaseHTTPRequestHandler collapses leading // before dispatch.
            # For this fixed allowlist, reject that alias rather than serve it.
            if self.requestline.split()[1] != self.path:
                self._reply(404, b"Not found")
                return
            name, mime = WORKFLOW_UI_ASSETS[self.path]
            try:
                body = files("obsidian_automation").joinpath(
                    "webui_design_v1", name).read_bytes()
            except (OSError, ModuleNotFoundError):
                self._reply(404, b"Not found")
                return
            self._reply(200, body, mime)
            return
        # Versioned observation-only workflow views. Never share the
        # existing mutable operator/snapshot/daily POST authority here.
        prefix = "/api/v1/workflow-control"
        if self.path == prefix or self.path.startswith(prefix + "/"):
            name = None
            if self.path != prefix:
                parts = self.path[len(prefix) + 1:].split("/")
                if parts[0] not in ("b1", "b2", "b3", "b4", "b5"):
                    self._reply(404, b"Not found")
                    return
                name = parts[0]
                if len(parts) == 3 and parts[1] == "requests" and re.fullmatch(
                        r"[0-9a-f]{32}", parts[2]) is not None:
                    # The unified admission/receipt ledger has not been
                    # attached. Absence cannot prove an ID was not admitted.
                    result = {
                        "schemaVersion": WORKFLOW_CONTROL_SCHEMA,
                        "workflowId": name, "requestId": parts[2],
                        "decision": "indeterminate", "receipt": None,
                        "error": {"code": "admission_unknown", "retry": "lookup"},
                    }
                    self._reply(503, json.dumps(result, sort_keys=True).encode("utf-8"),
                                "application/json; charset=utf-8")
                    return
                if len(parts) != 1:
                    self._reply(404, b"Not found")
                    return
            try:
                catalog = read_control_catalog(
                    snapshot_job=self.server.snapshot_job,
                    daily_job=self.server.daily_job,
                    optional_snapshots=self.server.workflow_control_snapshots,
                )
                result = catalog if name is None else next(
                    w for w in catalog["workflows"] if w["id"] == name)
                body = json.dumps(result, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False).encode("utf-8")
            except Exception:
                # Never echo exceptions, private legacy source, or untrusted
                # candidate data into an unauthenticated read-only endpoint.
                self._reply(503, b'{"error":"observation_unavailable"}',
                            "application/json; charset=utf-8")
                return
            self._reply(200, body, "application/json; charset=utf-8")
            return
        if self.path == "/api/v1/nextcloud/connections":
            store = self.server.snapshot_setup
            self._reply(200 if store else 503, json.dumps(
                store.nextcloud_status() if store else
                {"error":"nextcloud_setup_disabled"}).encode(), "application/json")
            return
        if self.path == "/api/v1/daily/connections":
            setup = self.server.daily_setup
            self._reply(200 if setup else 503,
                        json.dumps(setup.status() if setup else
                                   {"error":"daily_setup_disabled"}).encode(), "application/json")
            return
        if self.path == "/api/v1/daily" or self.path.startswith("/api/v1/daily/runs/"):
            job = self.server.daily_job
            if job is None:
                self._reply(503, b'{"error":"daily_disabled"}', "application/json")
                return
            value = (job.status() if self.path == "/api/v1/daily" else
                     job.run(self.path[len("/api/v1/daily/runs/"):]))
            self._reply(200 if value is not None else 404,
                        json.dumps(value).encode() if value is not None else b'{"error":"not_found"}',
                        "application/json")
            return
        if self.path == "/api/v1/formatter" or self.path.startswith("/api/v1/formatter/runs/"):
            job = getattr(self.server, "formatter_job", None)
            if job is None:
                self._reply(503, b'{"error":"formatter_disabled"}', "application/json")
                return
            value = (job.status() if self.path == "/api/v1/formatter" else
                     job.run(self.path[len("/api/v1/formatter/runs/"):]))
            self._reply(200 if value is not None else 404,
                        json.dumps(value, sort_keys=True).encode("utf-8")
                        if value is not None else b'{"error":"not_found"}',
                        "application/json")
            return
        if self.path == "/api/v1/workflows":
            value = workflow_catalog(self.server.snapshot_job, self.server.daily_job)
            self._reply(200, json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                        "application/json; charset=utf-8")
            return
        if self.path == "/api/v1/snapshot/connections":
            store = self.server.snapshot_setup
            self._reply(200 if store else 503, json.dumps(store.status() if store else {"error":"setup_disabled"}).encode(), "application/json")
            return
        if self.path == "/api/v1/snapshot" or self.path.startswith("/api/v1/snapshot/runs/"):
            job = self.server.snapshot_job
            if job is None:
                self._reply(503, b'{"error":"snapshot_disabled"}', "application/json")
                return
            value = (job.status() if self.path == "/api/v1/snapshot" else
                     job.run(self.path[len("/api/v1/snapshot/runs/"):]))
            if value is None:
                self._reply(404, b"Not found")
            else:
                self._reply(200, json.dumps(value).encode(), "application/json")
            return
        if self.path == "/api/v1/operator-ready":
            result = self._operator_capabilities()
            self._reply(200, json.dumps(
                result, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"), "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/v1/operations/"):
            value = self.path[len("/api/v1/operations/"):]
            if _REQUEST_ID.fullmatch(value) is None:
                self._reply(404, b"Not found")
                return
            if not self._operator_network_allowed():
                self._reply(403, b'{"error":"operator_network_required"}', "application/json")
                return
            try:
                receipt = safe_operator_receipt(
                    self.server.operator.receipt(value), value,
                )
            except Exception:
                receipt = {
                    "version": 1, "request_id": value,
                    "status": "unavailable", "authority": "observation_only",
                }
            self._reply(200, json.dumps(
                receipt, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"), "application/json; charset=utf-8")
            return
        if self.path == "/api/v1/operation-capabilities":
            self._reply(
                200, self.server.capabilities_singleflight.get(),
                "application/json; charset=utf-8",
            )
            return
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
            return
        if self.path == "/":
            self._reply(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/static/style.css":
            self._reply(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
        elif self.path == "/static/app.js":
            self._reply(200, JS.encode("utf-8"), "text/javascript; charset=utf-8")
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
        if self.path not in (
            "/api/v1/operation-previews", "/api/v1/operations",
            "/api/v1/snapshot/settings", "/api/v1/snapshot/runs",
            "/api/v1/snapshot/connections",
            "/api/v1/nextcloud/connections",
            "/api/v1/daily/settings", "/api/v1/daily/runs",
            "/api/v1/daily/connections",
            "/api/v1/formatter/settings", "/api/v1/formatter/runs",
        ):
            self._reply(405, b"Method not allowed")
            return
        if (
            self.path == "/api/v1/operations"
            and self.server.operator_ui_enabled is not True
        ):
            # Disabled mode preserves the existing strict no-mutation API
            # contract. No CSRF ticket or payload is consumed on this route.
            self._reply(405, b"Method not allowed")
            return
        if not self._write_origin_allowed():
            self._reply(403, b'{"error":"origin_rejected"}', "application/json")
            return
        if (
            self.path == "/api/v1/operations"
            and not self._operator_network_allowed()
        ):
            self._reply(403, b'{"error":"operator_network_required"}', "application/json")
            return
        tokens = self.headers.get_all("X-Obsidian-CSRF", [])
        if len(tokens) != 1 or not self.server.preview_csrf.consume(tokens[0]):
            self._reply(403, b'{"error":"csrf_rejected"}', "application/json")
            return
        if self.path == "/api/v1/nextcloud/connections":
            store, snapshot_job, daily_job = (self.server.snapshot_setup,
                                              self.server.snapshot_job,
                                              self.server.daily_job)
            formatter = getattr(self.server, "formatter_job", None)
            if store is None or snapshot_job is None:
                self._reply(503, b'{"error":"nextcloud_setup_disabled"}', "application/json")
                return
            try:
                payload = validate_nextcloud(json.loads(self._read_bounded_json()))
                with snapshot_job._lock, (daily_job._lock if daily_job else nullcontext()), (
                        formatter._lock if formatter else nullcontext()):
                    if snapshot_job.db.execute(
                        "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone() or (daily_job and daily_job.db.execute(
                        "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone()):
                        self._reply(409, b'{"error":"connection_busy"}', "application/json")
                        return
                    if formatter and formatter.db.execute(
                        "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone():
                        self._reply(409, b'{"error":"connection_busy"}', "application/json")
                        return
                    current = store.load_nextcloud()
                    if current != payload:
                        full = store.load()
                        combined = {**full, **payload} if full else payload
                        # Network probes happen before modifying either the
                        # B4 schedule or any persisted shared credentials.
                        checked = (store.probe(combined) if full else
                                   store.nextcloud_probe(payload))
                        if not checked:
                            self._reply(422, b'{"error":"nextcloud_connection_probe_failed"}',
                                        "application/json")
                            return
                        if daily_job:
                            old_settings = daily_job.settings()
                            daily_job.update({"enabled": False,
                                              "interval_seconds": old_settings["interval_seconds"]})
                        # The provider probe above was already successful.
                        # No second, racing remote check after pausing B4.
                        saved = (store.save(combined, probed=True) if full else
                                 store.save_nextcloud(payload, probed=True))
                        if not saved:
                            self._reply(503, b'{"error":"nextcloud_connection_unavailable"}',
                                        "application/json")
                            return
                        # Only the completed provider change invalidates the
                        # Formatter schedule. A failed save cannot stop it.
                        if formatter:
                            formatter.connections_saved()
                        if full:
                            snapshot_job.connections_saved(store.generation())
                self._reply(200, json.dumps(store.nextcloud_status()).encode(),
                            "application/json")
            except (PreviewRequestRejected, ValueError, TypeError):
                self._reply(400, b'{"error":"invalid_nextcloud_connection"}', "application/json")
            except Exception:
                self._reply(503, b'{"error":"nextcloud_connection_unavailable"}', "application/json")
            return

        if self.path == "/api/v1/daily/connections":
            setup, job = self.server.daily_setup, self.server.daily_job
            if setup is None or job is None:
                self._reply(503, b'{"error":"daily_setup_disabled"}', "application/json")
                return
            try:
                payload = json.loads(self._read_bounded_json())
                with job._lock:
                    if job.db.execute(
                        "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone():
                        self._reply(409, b'{"error":"daily_busy"}', "application/json")
                        return
                    setup.validate_payload(payload)
                    # Commit Off before replacing the GitHub credential.
                    # A failed SQLite reset must leave the previous token
                    # untouched, not bind new credentials to an On schedule.
                    current = job.settings()
                    job.update({"enabled":False,
                                "interval_seconds":current["interval_seconds"]})
                    if not setup.save(payload):
                        self._reply(422, b'{"error":"github_connection_probe_failed"}',
                                    "application/json")
                        return
                self._reply(200, json.dumps(setup.status()).encode(), "application/json")
            except (DailySetupError, DailyInvalid, PreviewRequestRejected, ValueError, TypeError):
                self._reply(400, b'{"error":"invalid_github_connection"}', "application/json")
            except Exception:
                self._reply(503, b'{"error":"github_connection_unavailable"}', "application/json")
            return

        if self.path in ("/api/v1/daily/settings", "/api/v1/daily/runs"):
            job = self.server.daily_job
            if job is None:
                self._reply(503, b'{"error":"daily_disabled"}', "application/json")
                return
            try:
                payload = json.loads(self._read_bounded_json())
                if self.path.endswith("/settings"):
                    result = job.update(payload)
                    code = 200
                else:
                    if type(payload) is not dict or set(payload) != {"request_id"}:
                        raise DailyInvalid("invalid_daily_request")
                    outcome, receipt = job.submit(payload["request_id"])
                    result = {"version":1, "outcome":outcome,
                              "run_id":receipt["run_id"] if receipt else None}
                    code = 202 if outcome in ("accepted","replayed") else 409
            except (DailyInvalid, PreviewRequestRejected, ValueError, TypeError):
                self._reply(400, b'{"error":"invalid_daily_request"}', "application/json")
                return
            except Exception:
                self._reply(503, b'{"error":"daily_unavailable"}', "application/json")
                return
            self._reply(code, json.dumps(result).encode(), "application/json")
            return

        if self.path in ("/api/v1/formatter/settings", "/api/v1/formatter/runs"):
            job = getattr(self.server, "formatter_job", None)
            if job is None:
                self._reply(503, b'{"error":"formatter_disabled"}', "application/json")
                return
            try:
                payload = json.loads(self._read_bounded_json())
                if self.path.endswith("/settings"):
                    result = job.update(payload)
                    code = 200
                else:
                    if type(payload) is not dict or set(payload) != {"request_id"}:
                        raise FormatterInvalid("invalid_formatter_request")
                    outcome, receipt = job.submit(payload["request_id"])
                    result = {"version": 1, "outcome": outcome,
                              "run_id": receipt["run_id"] if receipt else None}
                    code = 202 if outcome in ("accepted", "replayed") else 409
            except (FormatterInvalid, PreviewRequestRejected, ValueError, TypeError):
                self._reply(400, b'{"error":"invalid_formatter_request"}', "application/json")
                return
            except Exception:
                self._reply(503, b'{"error":"formatter_unavailable"}', "application/json")
                return
            self._reply(code, json.dumps(result).encode(), "application/json")
            return

        if self.path in ("/api/v1/snapshot/settings", "/api/v1/snapshot/runs"):
            job = self.server.snapshot_job
            if job is None:
                self._reply(503, b'{"error":"snapshot_disabled"}', "application/json")
                return
            try:
                payload = json.loads(self._read_bounded_json())
                if self.path.endswith("/settings"):
                    result = job.update(payload)
                    code = 200
                else:
                    if type(payload) is not dict or set(payload) != {"request_id"}:
                        raise SnapshotInvalid("invalid_request")
                    outcome, receipt = job.submit(payload["request_id"])
                    result = {"version": 1, "outcome": outcome, "run_id": receipt["run_id"] if receipt else None}
                    code = 202 if outcome in ("accepted", "replayed") else 409
            except (SnapshotInvalid, PreviewRequestRejected, ValueError, TypeError):
                self._reply(400, b'{"error":"invalid_snapshot_request"}', "application/json")
                return
            except Exception:
                self._reply(503, b'{"error":"snapshot_unavailable"}', "application/json")
                return
            self._reply(code, json.dumps(result).encode(), "application/json")
            return

        if self.path == "/api/v1/snapshot/connections":
            store, job = self.server.snapshot_setup, self.server.snapshot_job
            if store is None or job is None:
                self._reply(503, b'{"error":"setup_disabled"}', "application/json")
                return
            try:
                payload = json.loads(self._read_bounded_json())
                if type(payload) is dict and set(payload) == GITEA_FIELDS:
                    nextcloud = store.load_nextcloud()
                    if nextcloud is None:
                        raise ValueError("nextcloud_missing")
                    payload = {**nextcloud, **payload}
                daily = self.server.daily_job
                formatter = getattr(self.server, "formatter_job", None)
                with job._lock, (daily._lock if daily is not None else nullcontext()), (
                        formatter._lock if formatter else nullcontext()):
                    if job.db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                        self._reply(409, b'{"error":"snapshot_busy"}', "application/json")
                        return
                    # A running B4 pull must not race replacing the only
                    # shared Nextcloud connection.
                    if daily is not None and daily.db.execute(
                        "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone():
                        self._reply(409, b'{"error":"daily_busy"}', "application/json")
                        return
                    if formatter and formatter.db.execute(
                        "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone():
                        self._reply(409, b'{"error":"formatter_busy"}', "application/json")
                        return
                    github_setup = self.server.daily_setup
                    previous_source = (github_setup.source_fingerprint()
                                       if github_setup is not None else None)
                    previous_formatter_source = (formatter.source_generation()
                                                 if formatter is not None else None)
                    if not store.save(payload):
                        self._reply(422, b'{"error":"connection_probe_failed"}', "application/json")
                        return
                    try:
                        job.connections_saved(store.generation())
                        # B4 and Formatter follow only the canonical Nextcloud
                        # source, not unrelated Gitea connection changes.
                        if (formatter is not None and
                                previous_formatter_source != formatter.source_generation()):
                            formatter.connections_saved()
                        # B4 only depends on the Nextcloud source, not Gitea.
                        if (daily is not None and github_setup is not None
                                and previous_source != github_setup.source_fingerprint()):
                            settings = daily.settings()
                            daily.update({"enabled":False,
                                          "interval_seconds":settings["interval_seconds"]})
                    except Exception:
                        # Disk commit succeeded but schedule reset did not.
                        # Never claim the prior B2 schedule remains valid.
                        job.configured = False
                        raise
                self._reply(200, json.dumps(store.status()).encode(), "application/json")
            except (ValueError, TypeError, PreviewRequestRejected):
                self._reply(400, b'{"error":"invalid_connection"}', "application/json")
            except Exception:
                self._reply(503, b'{"error":"connection_unavailable"}', "application/json")
            return

        if self.path == "/api/v1/operations":
            if self.server.operator_ui_enabled is not True:
                self._reply(503, b'{"error":"operator_unavailable"}', "application/json")
                return
            try:
                payload, ticket = parse_confirm_request(self._read_bounded_json())
            except (PreviewRequestRejected, ValueError):
                self._reply(400, b'{"error":"invalid_operation_request"}', "application/json")
                return
            if not self.server.operator_tickets.verify(payload, ticket):
                self._reply(403, b'{"error":"confirmation_required"}', "application/json")
                return
            caps = self._operator_capabilities()
            if caps["operations"][payload["action"]] is not True:
                self._reply(503, b'{"error":"operator_unavailable"}', "application/json")
                return
            try:
                result = self.server.operator.submit(payload)
            except Exception:
                result = {"version": 1, "error": "operator_submission_unconfirmed"}
            if (
                type(result) is not dict
                or set(result) == {"version", "error"} and (
                    type(result.get("version")) is not int or result["version"] != 1
                    or type(result.get("error")) is not str
                    or result["error"] not in {
                        "queue_disabled", "queue_busy", "admission_lock_busy",
                        "queue_recovery_required", "queue_missing_receipt",
                        "run_once_unqualified", "queue_request_rejected",
                        "operator_submission_unconfirmed", "operator_worker_unavailable",
                    }
                )
            ):
                result = {"version": 1, "error": "operator_submission_unconfirmed"}
            elif "error" not in result:
                result = safe_operator_receipt(result, payload["request_id"])
            self._reply(200, json.dumps(
                result, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"), "application/json; charset=utf-8")
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
        if (
            preview["status"] == "compatible"
            and self._operator_capabilities()["operations"][request["action"]] is True
        ):
            # A ticket proves only the *exact* Preview selection that was
            # shown. The root Worker still observes actual systemd state.
            payload = {
                "version": 1, "request_id": request["request_id"],
                "service_id": request["service_id"],
                "action": request["action"],
            }
            try:
                preview = dict(preview)
                preview["confirmation_ticket"] = self.server.operator_tickets.issue(payload)
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


_PRIVATE_IPV4 = tuple(ipaddress.IPv4Network(v) for v in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def validate_bind_address(value: str) -> str:
    """Accept only a concrete loopback or RFC1918 IPv4 interface."""
    if type(value) is not str:
        raise ValueError("invalid_private_bind")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("invalid_private_bind") from exc
    if value != str(address):
        raise ValueError("invalid_private_bind")
    if address != ipaddress.IPv4Address("127.0.0.1") and not any(
            address in network for network in _PRIVATE_IPV4):
        raise ValueError("nonprivate_bind_refused")
    return value


def make_server(
    *, port: int = 8370, bind_address: str = "127.0.0.1",
    observer: Observer = aggregate_status,
    ai_observer: AIObserver = load_ai_health,
    capabilities_observer: Callable[[], dict[str, object]] = _unavailable_capabilities,
    preview_observer: Callable[[dict[str, object]], dict[str, object]] = lambda _: unavailable_preview(),
    operator: OperatorHTTPClient | None = None,
    operator_ui_enabled: bool = False,
    workflow_control_snapshots: dict[str, object] | None = None,
    snapshot_job: SnapshotJob | None = None,
    snapshot_setup=None,
    daily_job: DailyJob | None = None,
    daily_setup: DailySetup | None = None,
    formatter_job: FormatterJob | None = None,
) -> ThreadingHTTPServer:
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    bind_address = validate_bind_address(bind_address)
    server = BoundedDashboardServer((bind_address, port), Handler)
    server.observer = observer
    server.ai_observer = ai_observer
    server.status_singleflight = _StatusSingleFlight(observer, ai_observer)
    server.capabilities_observer = capabilities_observer
    server.capabilities_singleflight = _CapabilitiesSingleFlight(capabilities_observer)
    server.preview_observer = preview_observer
    server.preview_csrf = OneTimePreviewCsrf()
    server.operator_tickets = ConfirmationTickets()
    server.operator = operator if operator is not None else OperatorHTTPClient()
    # Operator UI trusts the configured LAN/VPN network perimeter.
    # It remains disabled unless --operator-ui is explicitly selected.
    server.operator_ui_enabled = operator_ui_enabled is True
    # Explicitly supplied, already-materialized mock/read-only B1/B3/B5
    # observations. Do not construct adapters, discover sources or run jobs.
    server.workflow_control_snapshots = workflow_control_snapshots
    server.snapshot_job = snapshot_job
    server.snapshot_setup = snapshot_setup
    server.daily_job = daily_job
    server.daily_setup = daily_setup
    server.formatter_job = formatter_job
    server.daemon_threads = True
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental no-effect localhost Dashboard")
    parser.add_argument("--port", type=int, default=8370)
    parser.add_argument(
        "--operator-ui", action="store_true",
        help="Allow confirmed operations on a network-restricted loopback console.",
    )
    args = parser.parse_args(argv)
    operator = OperatorHTTPClient()
    with make_server(
        port=args.port,
        capabilities_observer=capabilities_from_socket,
        preview_observer=lambda request: preview_from_socket(
            request, socket_path=DEFAULT_OPERATION_QUEUE_SOCKET,
        ),
        operator=operator,
        operator_ui_enabled=args.operator_ui,
    ) as server:
        server.serve_forever(poll_interval=0.25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
