/* Knowledge-proposal panel (B1-lite phase 3). Count-only: never shows titles or
   text. Rendered only when the server was started with --knowledge-ui. A run
   creates one proposal note in the Vault (04-AI/Proposals/, create-only). */
(() => {
  "use strict";
  const mount = document.querySelector("main");
  if (!mount) return;
  const PROVIDER = {chatgpt: "ChatGPT", claude: "Claude"};
  const STATE = {
    new: "未処理", proposed: "提案済み", failed: "失敗（手動で再試行）",
    unconfirmed: "結果不明（手動で再試行）", too_large: "長すぎる（モデルの入力上限）", empty: "空"
  };
  const RUN = {
    queued: "待機中", running: "実行中", proposed: "提案を作成", failed: "失敗",
    unconfirmed: "結果不明", skipped: "見送り"
  };
  const SKIP = {
    busy: "別の実行中", daily_cap: "1日の上限", pending_limit: "未承認の提案が多い",
    nothing_to_do: "対象の会話なし", pending_count_unavailable: "未承認の件数を確認できない",
    vault_root_refused: "Vaultルートの確認に失敗", source_not_configured: "Vault接続が未設定",
    invalid_vault_root: "Vault接続が不正"
  };
  const errors = {
    knowledge_busy: "別の提案を実行中です",
    invalid_request: "リクエストが不正です",
    archive_not_found: "取り込み済みのZIPが見つかりません",
    csrf_rejected: "操作セッションが期限切れです。もう一度実行してください",
    origin_rejected: "アクセス元を検証できませんでした",
    conversation_not_usable: "この会話は使えません（空か、モデルの入力上限を超えています）",
    conversation_changed: "会話の内容が変わりました。一覧を読み込み直してください",
    not_a_valid_export: "取り込み済みのZIPを読み取れません",
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
  async function session() { return (await get("/api/v1/session")).csrf; }
  async function post(url, body) {
    const csrf = await session();
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
    "取り込み済みのChatGPT / Claudeの会話から、ローカルLLMで知識の提案を1件ずつ作り、" +
    "Vaultの 04-AI/Proposals/ に新規作成します（既存のファイルは変更しません）。" +
    "承認は、Obsidianで提案ノートを開いて「Knowledgeに昇格」を押します。" +
    "タイトルや本文はここには表示しません。", {className: "muted"});
  const message = text("p", "", {id: "knowledge-message", className: "feedback"});
  const statusBox = text("dl", "", {id: "knowledge-status"});
  const scheduleButton = text("button", "", {id: "knowledge-schedule", className: "outline"});
  const loadButton = text("button", "会話の一覧を読み込む", {id: "knowledge-load"});
  const listBox = el("div", {id: "knowledge-list"});
  const runsBox = el("div", {id: "knowledge-runs"});

  const panel = el("details", {className: "connections", id: "knowledge"}, [
    text("summary", "知識提案（ローカルLLM）"), note, message, statusBox,
    el("div", {className: "actions"}, [scheduleButton]),
    el("div", {className: "actions"}, [loadButton]), listBox,
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

  function renderStatus(status) {
    schedule = status.schedule;
    busy = status.busy === true;
    const rows = [];
    rows.push(...row("モデル", status.model));
    rows.push(...row("定期実行", schedule.enabled ? "オン（次回 " + when(schedule.next_due) + "）" : "オフ"));
    rows.push(...row("間隔 / 1日の上限 / 未承認の上限",
      Math.round(schedule.interval_seconds / 60) + " 分 / " + schedule.max_per_day +
      " 件 / " + schedule.max_pending + " 件"));
    rows.push(...row("今日の提案", schedule.proposed_today + " 件"));
    if (schedule.last_skip) rows.push(...row("直近の見送り", SKIP[schedule.last_skip] || schedule.last_skip));
    statusBox.textContent = "";
    for (const node of rows) statusBox.appendChild(node);
    scheduleButton.textContent = schedule.enabled ? "定期実行をオフにする" : "定期実行をオンにする";
    scheduleButton.disabled = busy;
    loadButton.disabled = busy;
    for (const button of listBox.querySelectorAll("button")) button.disabled = busy;
    runsBox.textContent = "";
    const runs = status.recent_runs || [];
    if (!runs.length) runsBox.appendChild(text("p", "まだ実行していません。", {className: "muted"}));
    for (const run of runs) {
      const detail = [RUN[run.status] || run.status, run.trigger === "scheduled" ? "定期" : "手動"];
      if (run.file) detail.push(run.file);
      if (run.outcome && run.outcome !== "created") detail.push("(" + run.outcome + ")");
      if (run.error_code) detail.push(SKIP[run.error_code] || run.error_code);
      runsBox.appendChild(text("p", when(run.created) + "  " + detail.join(" / ")));
    }
  }
  async function refresh() {
    try {
      renderStatus(await get("/api/v1/knowledge"));
      if (busy && !timer) timer = setInterval(refresh, 3000);
      if (!busy && timer) { clearInterval(timer); timer = null; }
    } catch (e) { setMessage(describe(e.message)); }
  }

  function renderList(data) {
    listBox.textContent = "";
    if (!data.conversations.length) {
      listBox.appendChild(text("p", "取り込み済みの会話がありません。先に「チャット履歴の取り込み」を使ってください。",
        {className: "muted"}));
      return;
    }
    const counts = Object.keys(data.counts).map((key) => (STATE[key] || key) + " " + data.counts[key]).join(" / ");
    listBox.appendChild(text("p", "会話 " + data.total + " 件（" + counts + "）" +
      (data.truncated ? "。先頭の一部だけを表示しています" : "")));
    const table = el("table");
    table.appendChild(el("tr", {}, ["#", "提供元", "発言数", "サイズ", "最初の日", "状態", ""].map(
      (label) => text("th", label))));
    for (const item of data.conversations) {
      const actionable = item.state === "new" || item.state === "failed" || item.state === "unconfirmed";
      const button = text("button", "提案を作る", {className: "outline", disabled: busy || !actionable});
      button.addEventListener("click", () => start(item));
      table.appendChild(el("tr", {}, [
        text("td", String(item.index + 1)), text("td", PROVIDER[item.provider] || item.provider),
        text("td", String(item.messages)), text("td", Math.ceil(item.bytes / 1024) + " KiB"),
        text("td", item.first_day), text("td", STATE[item.state] || item.state),
        el("td", {}, actionable ? [button] : [])]));
    }
    listBox.appendChild(table);
  }

  async function start(item) {
    if (busy) return;
    if (!confirm("この会話から、ローカルLLMで提案を1件作り、Vaultの 04-AI/Proposals/ に新規作成します。" +
        "既存のファイルは変更しません。よろしいですか。")) return;
    setMessage("提案を開始しています…");
    try {
      await post("/api/v1/knowledge/runs", {archive: item.archive, index: item.index});
      setMessage("実行中です。完了まで数分かかることがあります。");
    } catch (e) { setMessage(describe(e.message)); }
    await refresh();
  }

  loadButton.addEventListener("click", async () => {
    if (busy) return;
    loadButton.disabled = true;
    setMessage("会話の一覧を読み込んでいます…");
    try {
      renderList(await post("/api/v1/knowledge/conversations", {}));
      setMessage("");
    } catch (e) { setMessage(describe(e.message)); }
    await refresh();
  });
  scheduleButton.addEventListener("click", async () => {
    if (busy || !schedule) return;
    const enable = !schedule.enabled;
    if (enable && !confirm("定期実行をオンにします。" + Math.round(schedule.interval_seconds / 60) +
        " 分ごとに、未処理の会話を1件ずつ自動で選び、Vaultに提案を新規作成します（1日 " +
        schedule.max_per_day + " 件まで、未承認が " + schedule.max_pending + " 件以上なら停止）。")) return;
    try {
      await post("/api/v1/knowledge/settings", {enabled: enable});
      setMessage(enable ? "定期実行をオンにしました。" : "定期実行をオフにしました。");
    } catch (e) { setMessage(describe(e.message)); }
    await refresh();
  });

  get("/api/v1/knowledge").then((status) => {
    if (!status || status.version !== 1 || status.enabled !== true) return;
    panel.hidden = false;
    renderStatus(status);
    if (busy) timer = setInterval(refresh, 3000);
  }).catch(() => { panel.hidden = true; });
})();
