/* Project status panel (B3-lite). Count-only: never shows note names, repository
   names or text. Rendered only when the server was started with --project-status-ui.
   A run reads the Project notes and GitHub and changes the status line of the notes
   whose rule says so; the schedule is off until switched on here. */
(() => {
  "use strict";
  const mount = document.querySelector("main");
  if (!mount) return;
  const RUN = {queued: "待機中", running: "実行中", ok: "完了", failed: "失敗", skipped: "見送り"};
  const MODE = {apply: "書き込みあり", dry_run: "確認のみ"};
  const HELD = {unchanged: "変更なし", cooldown: "24時間以内に変更済み", no_snapshot: "GitHubを読めない",
                not_automatic: "自動では変えない状態", run_cap: "1回の上限"};
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

  const note = text("p",
    "GitHubの直近の動きから、Projectノートの status（planning / running / stable）を自動で書き換えます。" +
    "書き換えるのは status の1行だけで、同じノートは24時間に1回までです。" +
    "stopped・done・cancelled は変えません。ノート名や本文はここには表示しません。", {className: "muted"});
  const message = text("p", "", {id: "project-status-message", className: "feedback"});
  const statusBox = text("dl", "", {id: "project-status-status"});
  const scheduleButton = text("button", "", {id: "project-status-schedule", className: "outline"});
  const checkButton = text("button", "確認だけ実行（書き込まない）", {id: "project-status-check", className: "outline"});
  const applyButton = text("button", "今すぐ実行（書き込む）", {id: "project-status-apply"});
  const runsBox = el("div", {id: "project-status-runs"});

  const panel = el("details", {className: "connections", id: "project-status"}, [
    text("summary", "Projectの状態を自動更新"), note, message, statusBox,
    el("div", {className: "actions"}, [scheduleButton, checkButton, applyButton]),
    text("h3", "直近の実行"), runsBox
  ]);
  panel.hidden = true;
  mount.appendChild(panel);

  let busy = false;
  let schedule = null;
  let timer = null;
  const setMessage = (value) => { message.textContent = value; };
  const when = (seconds) => (typeof seconds === "number")
    ? new Date(seconds * 1000).toLocaleString("ja-JP") : "-";
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
    rows.push(...row("間隔 / 1回の上限", Math.round(schedule.interval_seconds / 60) + " 分 / " +
      schedule.max_changes + " 件"));
    rows.push(...row("今日の変更", schedule.changed_today + " 件"));
    if (schedule.last_skip) rows.push(...row("直近の見送り", schedule.last_skip));
    statusBox.textContent = "";
    for (const node of rows) statusBox.appendChild(node);
    scheduleButton.textContent = schedule.enabled ? "定期実行をオフにする" : "定期実行をオンにする";
    for (const button of [scheduleButton, checkButton, applyButton]) button.disabled = busy;
    runsBox.textContent = "";
    const runs = status.recent_runs || [];
    if (!runs.length) runsBox.appendChild(text("p", "まだ実行していません。", {className: "muted"}));
    for (const run of runs) {
      const detail = [RUN[run.status] || run.status, run.trigger === "scheduled" ? "定期" : "手動",
                      MODE[run.mode] || run.mode];
      const counts = summarize(run.counts);
      if (counts) detail.push(counts);
      if (run.error_code) detail.push(run.error_code);
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

  async function start(apply) {
    if (busy) return;
    if (apply && !confirm("GitHubの動きを見て、Projectノートの status を今すぐ書き換えます" +
        "（同じノートは24時間に1回、1回に最大 " + schedule.max_changes + " 件）。よろしいですか。")) return;
    setMessage(apply ? "実行を開始しています…" : "確認を開始しています…");
    try {
      await post("/api/v1/project-status/runs", {apply: apply});
      setMessage("実行中です。");
    } catch (e) { setMessage(describe(e.message)); }
    await refresh();
  }
  checkButton.addEventListener("click", () => start(false));
  applyButton.addEventListener("click", () => start(true));
  scheduleButton.addEventListener("click", async () => {
    if (busy || !schedule) return;
    const enable = !schedule.enabled;
    if (enable && !confirm("定期実行をオンにします。" + Math.round(schedule.interval_seconds / 60) +
        " 分ごとに、GitHubの動きから Projectノートの status を自動で書き換えます" +
        "（同じノートは24時間に1回、1回に最大 " + schedule.max_changes + " 件）。")) return;
    try {
      await post("/api/v1/project-status/settings", {enabled: enable});
      setMessage(enable ? "定期実行をオンにしました。" : "定期実行をオフにしました。");
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
