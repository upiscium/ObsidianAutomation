/* Project updater panel (B3-lite). Count-only: never shows note names, repository
   names or text. Rendered only when the server was started with --project-status-ui.
   A run reads the Project notes and GitHub and changes the status line of the notes
   whose rule says so; the schedule is off until switched on here. */
(() => {
  "use strict";
  const main = document.querySelector("main");
  if (!main) return;
  const mount = (document.getElementById && document.getElementById("panels")) || main;
  const RUN = {queued: "待機中", running: "実行中", ok: "完了", failed: "失敗", skipped: "見送り"};
  const MODE = {apply: "書き込みあり", dry_run: "確認のみ"};
  const HELD = {unchanged: "変更なし", cooldown: "24時間以内に変更済み", no_snapshot: "GitHubを読めない",
                not_automatic: "自動では変えない状態", run_cap: "1回の上限"};
  const SKIP = {busy: "別の実行中", daily_cap: "1日の上限"};
  const errors = {
    project_status_busy: "別の実行中です",
    invalid_request: "リクエストが不正です",
    csrf_rejected: "操作セッションが期限切れです。もう一度実行してください",
    origin_rejected: "アクセス元を検証できませんでした",
    operation_unavailable: "結果不明です。再送せず、状態を確認してください"
  };
  const describe = (code) => errors[code] || ("操作できませんでした (" + String(code || "unknown") + ")");

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const child of children || []) node.appendChild(child);
    return node;
  }
  const text = (tag, value, props) => el(tag, Object.assign({textContent: value}, props || {}));

  async function get(url) {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
    const body = await response.json();
    if (!response.ok) throw Error(body.error || "read_unavailable");
    return body;
  }
  async function post(url, body) {
    const csrf = (await get("/api/v1/session")).csrf;
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin", mode: "same-origin",
      headers: {"Content-Type": "application/json", "X-Obsidian-CSRF": csrf},
      body: JSON.stringify(body)
    });
    let data;
    try { data = await response.json(); } catch (e) { throw Error("operation_unavailable"); }
    if (!response.ok) throw Error(data.error || "operation_unavailable");
    return data;
  }

  const message = text("p", "", {id: "project-status-message", className: "feedback"});
  const statusBox = text("dl", "", {id: "project-status-status"});
  const field = (id, label, props) => {
    const input = el("input", Object.assign({id}, props));
    return {input, node: el("label", {}, [text("span", label), input])};
  };
  const fEnabled = el("input", {type: "checkbox", id: "project-status-enabled"});
  const enabledLabel = el("label", {className: "toggle"}, [fEnabled, text("span", " 自動実行")]);
  const fInterval = field("project-status-interval", "間隔（分、60以上）", {type: "number", min: 60, step: 1});
  const fPerRun = field("project-status-per-run", "1回の変更の上限（件）", {type: "number", min: 1, max: 100, step: 1});
  const fPerDay = field("project-status-per-day", "1日の変更の上限（件）", {type: "number", min: 1, max: 500, step: 1});
  const fCooldown = field("project-status-cooldown", "同じノートの変更間隔（時間）", {type: "number", min: 1, max: 720, step: 1});
  const fWindow = field("project-status-window", "活動とみなす日数（コミットの期間）", {type: "number", min: 1, max: 90, step: 1});
  const fAnchor = field("project-status-anchor", "基準時刻（日本時間。空欄＝オンにした時点から）", {type: "time"});
  const saveButton = text("button", "SAVE", {id: "project-status-save"});
  const closeButton = text("button", "閉じる", {className: "outline", type: "button"});
  const dialogMessage = text("p", "", {id: "project-status-dialog-message", className: "feedback"});
  const settingsForm = el("div", {id: "project-status-settings", className: "knowledge-settings"},
    [fInterval.node, fPerRun.node, fPerDay.node, fCooldown.node, fWindow.node, fAnchor.node]);
  const dialog = el("dialog", {className: "eva-dialog", id: "project-status-dialog"}, [
    el("div", {className: "eva-dialog-head"}, [text("span", "CONFIG", {className: "eva-tag"}), text("h2", "Project updater")]),
    enabledLabel, settingsForm, dialogMessage,
    el("div", {className: "actions"}, [saveButton, closeButton])
  ]);
  const gearButton = el("button", {className: "outline gear", id: "project-status-config", type: "button"},
    [text("span", "", {className: "gear-icon"}), text("span", "設定")]);
  const runButton = text("button", "Run once", {id: "project-status-apply", className: "outline"});
  const runsBox = el("div", {id: "project-status-runs"});

  const panel = el("section", {className: "connections panel", id: "project-status"}, [
    el("div", {className: "service-header"}, [text("h2", "Project updater")]),
    message, statusBox,
    el("div", {className: "actions"}, [runButton, gearButton]),
    text("h3", "直近の実行"), runsBox, dialog
  ]);
  panel.hidden = true;
  mount.appendChild(panel);

  const openDialog = () => { if (dialog.showModal) { if (!dialog.open) dialog.showModal(); } else dialog.open = true; };
  const closeDialog = () => { if (dialog.close) dialog.close(); else dialog.open = false; };

  let busy = false;
  let schedule = null;
  let timer = null;
  const setMessage = (value) => { message.textContent = value; dialogMessage.textContent = value; };
  const when = (seconds) => (typeof seconds === "number")
    ? new Date(seconds * 1000).toLocaleString("ja-JP") : "-";
  const pad = (n) => String(n).padStart(2, "0");
  const clock = (minute) => pad(Math.floor(minute / 60)) + ":" + pad(minute % 60);
  const parseClock = (value) => {
    const m = /^(\d{2}):(\d{2})$/.exec(value || "");
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
  };
  function row(label, value) { return [text("dt", label), text("dd", String(value))]; }

  function summarize(counts) {
    if (!counts) return "";
    const parts = ["対象 " + counts.watched + " 件"];
    if (typeof counts.applied === "number" && counts.mode === "apply") parts.push("変更 " + counts.applied + " 件");
    else parts.push("変更の見込み " + (counts.planned || 0) + " 件");
    for (const key of Object.keys(counts.held || {})) {
      if (key !== "unchanged" && counts.held[key]) parts.push((HELD[key] || key) + " " + counts.held[key]);
    }
    if (counts.repo_errors) parts.push("GitHubを読めない " + counts.repo_errors);
    for (const key of Object.keys(counts.failed || {})) parts.push(key + " " + counts.failed[key]);
    return parts.join(" / ");
  }

  function renderStatus(status) {
    schedule = status.schedule;
    busy = status.busy === true;
    const rows = [];
    rows.push(...row("定期実行", schedule.enabled ? "オン（次回 " + when(schedule.next_due) + "）" : "オフ"));
    rows.push(...row("今日の変更", schedule.changed_today + " 件"));
    if (schedule.last_skip) rows.push(...row("直近の見送り", SKIP[schedule.last_skip] || schedule.last_skip));
    statusBox.textContent = "";
    for (const node of rows) statusBox.appendChild(node);
    // The form always holds the current settings, unless the owner is editing it.
    if (!settingsForm.dirty) {
      fEnabled.checked = schedule.enabled === true;
      fInterval.input.value = String(Math.round(schedule.interval_seconds / 60));
      fPerRun.input.value = String(schedule.max_changes);
      fPerDay.input.value = String(schedule.max_per_day);
      fCooldown.input.value = String(schedule.cooldown_hours);
      fWindow.input.value = schedule.active_window_days == null ? "" : String(schedule.active_window_days);
      fAnchor.input.value = schedule.anchor_minute == null ? "" : clock(schedule.anchor_minute);
    }
    runButton.disabled = busy;
    runsBox.textContent = "";
    const runs = status.recent_runs || [];
    if (!runs.length) runsBox.appendChild(text("p", "まだ実行していません。", {className: "muted"}));
    for (const run of runs) {
      const detail = [RUN[run.status] || run.status, run.trigger === "scheduled" ? "定期" : "手動",
                      MODE[run.mode] || run.mode];
      const counts = summarize(run.counts);
      if (counts) detail.push(counts);
      if (run.error_code) detail.push(SKIP[run.error_code] || run.error_code);
      runsBox.appendChild(text("p", when(run.created) + "  " + detail.join(" / ")));
    }
  }
  async function refresh() {
    try {
      renderStatus(await get("/api/v1/project-status"));
      if (busy && !timer) timer = setInterval(refresh, 3000);
      if (!busy && timer) { clearInterval(timer); timer = null; }
    } catch (e) { setMessage(describe(e.message)); }
  }

  runButton.addEventListener("click", async () => {
    if (busy) return;
    if (!confirm("GitHubの動きを見て、Projectノートの status を今すぐ書き換えます" +
        "（同じノートは" + (schedule ? schedule.cooldown_hours : 24) + "時間に1回、1回に最大 " +
        (schedule ? schedule.max_changes : 10) + " 件）。よろしいですか。")) return;
    setMessage("実行を開始しています…");
    try {
      await post("/api/v1/project-status/runs", {apply: true});
      setMessage("実行中です。");
    } catch (e) { setMessage(describe(e.message)); }
    await refresh();
  });
  gearButton.addEventListener("click", openDialog);
  closeButton.addEventListener("click", closeDialog);
  for (const f of [fInterval, fPerRun, fPerDay, fCooldown, fWindow, fAnchor]) {
    f.input.addEventListener("input", () => { settingsForm.dirty = true; });
  }
  fEnabled.addEventListener("change", () => { settingsForm.dirty = true; });
  saveButton.addEventListener("click", async () => {
    const body = {
      enabled: fEnabled.checked,
      interval_seconds: Math.round(Number(fInterval.input.value) * 60),
      max_changes: Number(fPerRun.input.value),
      max_per_day: Number(fPerDay.input.value),
      cooldown_hours: Number(fCooldown.input.value),
      anchor_minute: parseClock(fAnchor.input.value)
    };
    if (fWindow.input.value !== "") body.active_window_days = Number(fWindow.input.value);
    const whole = [body.max_changes, body.max_per_day, body.cooldown_hours, body.interval_seconds]
      .concat(body.active_window_days === undefined ? [] : [body.active_window_days]);
    if (!whole.every(Number.isInteger) || body.interval_seconds < 3600) {
      setMessage("値を確認してください（間隔は60分以上、上限は整数）"); return;
    }
    if (body.enabled && !(schedule && schedule.enabled) && !confirm("自動実行をオンにします。" +
        Math.round(body.interval_seconds / 60) + " 分ごとに、GitHubの動きから Projectノートの status を自動で書き換えます" +
        "（同じノートは" + body.cooldown_hours + "時間に1回、1回に最大 " + body.max_changes + " 件）。")) return;
    try {
      await post("/api/v1/project-status/settings", body);
      settingsForm.dirty = false;
      setMessage("設定を保存しました。");
    } catch (e) { setMessage(describe(e.message)); }
    await refresh();
  });

  get("/api/v1/project-status").then((status) => {
    if (!status || status.version !== 1 || status.enabled !== true) return;
    panel.hidden = false;
    renderStatus(status);
    if (busy) timer = setInterval(refresh, 3000);
  }).catch(() => { panel.hidden = true; });
})();
