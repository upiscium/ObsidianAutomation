/* Knowledge Gen panel (B1-lite). Count-only: never shows titles or
   text. Rendered only when the server was started with --knowledge-ui. A run
   creates one proposal note in the Vault (04-AI/Proposals/, create-only). */
(() => {
  "use strict";
  const main = document.querySelector("main");
  if (!main) return;
  const mount = (document.getElementById && document.getElementById("panels")) || main;
  const PROVIDER = {chatgpt: "ChatGPT", claude: "Claude"};
  const STATE = {
    new: "未処理", proposed: "提案済み", failed: "失敗（手動で再試行）",
    unconfirmed: "結果不明（手動で再試行）", too_large: "長すぎる（分割しても入りきらない）", empty: "空"
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
    models_unavailable: "モデルの一覧を取得できませんでした（エンドポイントを確認してください）",
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

  const message = text("p", "", {id: "knowledge-message", className: "feedback"});
  const statusBox = text("dl", "", {id: "knowledge-status"});
  const field = (id, label, props) => {
    const input = el("input", Object.assign({id}, props));
    return {input, node: el("label", {}, [text("span", label), input])};
  };
  const fEnabled = el("input", {type: "checkbox", id: "knowledge-enabled"});
  const enabledLabel = el("label", {className: "toggle"}, [fEnabled, text("span", " 自動実行")]);
  const fEndpoint = field("knowledge-endpoint", "Endpoint（OpenAI互換のベースURL）", {type: "url", maxLength: 300, autocomplete: "off"});
  const fModel = field("knowledge-model", "Model", {type: "text", maxLength: 120, autocomplete: "off"});
  const modelList = el("datalist", {id: "knowledge-model-list"});
  fModel.input.setAttribute("list", "knowledge-model-list");
  const modelsButton = text("button", "モデル一覧を取得", {id: "knowledge-models", className: "outline", type: "button"});
  const fInterval = field("knowledge-interval", "間隔（分、60以上）", {type: "number", min: 60, step: 1});
  const fPerDay = field("knowledge-per-day", "1日の上限（件）", {type: "number", min: 1, max: 100, step: 1});
  const fPending = field("knowledge-pending", "最大の承認待ち件数", {type: "number", min: 1, max: 1000, step: 1});
  const fAnchor = field("knowledge-anchor", "基準時刻（日本時間。空欄＝オンにした時点から）", {type: "time"});
  const saveButton = text("button", "SAVE", {id: "knowledge-save"});
  const closeButton = text("button", "閉じる", {className: "outline", type: "button"});
  const dialogMessage = text("p", "", {id: "knowledge-dialog-message", className: "feedback"});
  const settingsForm = el("div", {id: "knowledge-settings", className: "knowledge-settings"},
    [fEndpoint.node, fModel.node, modelList, el("div", {className: "actions"}, [modelsButton]),
     fInterval.node, fPerDay.node, fPending.node, fAnchor.node]);
  const dialog = el("dialog", {className: "eva-dialog", id: "knowledge-dialog"}, [
    el("div", {className: "eva-dialog-head"}, [text("span", "CONFIG", {className: "eva-tag"}), text("h2", "Knowledge Gen")]),
    enabledLabel, settingsForm, dialogMessage,
    el("div", {className: "actions"}, [saveButton, closeButton])
  ]);
  const gearButton = el("button", {className: "outline gear", id: "knowledge-config", type: "button"},
    [text("span", "", {className: "gear-icon"}), text("span", "設定")]);
  const loadButton = text("button", "会話の一覧を読み込む", {id: "knowledge-load"});
  const listBox = el("div", {id: "knowledge-list"});
  const runsBox = el("div", {id: "knowledge-runs"});

  const panel = el("section", {className: "connections panel", id: "knowledge"}, [
    el("div", {className: "service-header"}, [text("h2", "Knowledge Gen")]),
    message, statusBox,
    el("div", {className: "actions"}, [gearButton, loadButton]), listBox,
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

  function renderStatus(status) {
    schedule = status.schedule;
    busy = status.busy === true;
    const rows = [];
    rows.push(...row("定期実行", schedule.enabled ? "オン（次回 " + when(schedule.next_due) + "）" : "オフ"));
    rows.push(...row("今日の提案", schedule.proposed_today + " 件"));
    if (schedule.last_skip) rows.push(...row("直近の見送り", SKIP[schedule.last_skip] || schedule.last_skip));
    statusBox.textContent = "";
    for (const node of rows) statusBox.appendChild(node);
    // The form always holds the current settings, unless the owner is editing it.
    if (!settingsForm.dirty) {
      fEnabled.checked = schedule.enabled === true;
      fEndpoint.input.value = status.llm_url || "";
      fModel.input.value = status.model || "";
      fInterval.input.value = String(Math.round(schedule.interval_seconds / 60));
      fPerDay.input.value = String(schedule.max_per_day);
      fPending.input.value = String(schedule.max_pending);
      fAnchor.input.value = schedule.anchor_minute == null ? "" : clock(schedule.anchor_minute);
    }
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
      listBox.appendChild(text("p", "取り込み済みの会話がありません。先に「Chat import」を使ってください。",
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
        text("td", String(item.index + 1) + (item.parts > 1 ? "（" + (item.part + 1) + "/" + item.parts + "）" : "")), text("td", PROVIDER[item.provider] || item.provider),
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
      await post("/api/v1/knowledge/runs", {archive: item.archive, index: item.index, part: item.part || 0});
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
  gearButton.addEventListener("click", openDialog);
  closeButton.addEventListener("click", closeDialog);
  for (const f of [fEndpoint, fModel, fInterval, fPerDay, fPending, fAnchor]) {
    f.input.addEventListener("input", () => { settingsForm.dirty = true; });
  }
  fEnabled.addEventListener("change", () => { settingsForm.dirty = true; });
  modelsButton.addEventListener("click", async () => {
    setMessage("モデルの一覧を取得しています…");
    try {
      const data = await post("/api/v1/knowledge/models", {});
      modelList.textContent = "";
      for (const id of data.models || []) modelList.appendChild(text("option", id, {value: id}));
      setMessage((data.models || []).length + " 件のモデルが見つかりました。Modelの欄から選べます。");
    } catch (e) { setMessage(describe(e.message)); }
  });
  saveButton.addEventListener("click", async () => {
    const body = {
      enabled: fEnabled.checked,
      llm_url: fEndpoint.input.value.trim(),
      model: fModel.input.value.trim(),
      interval_seconds: Math.round(Number(fInterval.input.value) * 60),
      max_per_day: Number(fPerDay.input.value),
      max_pending: Number(fPending.input.value),
      anchor_minute: parseClock(fAnchor.input.value)
    };
    if (!Number.isInteger(body.max_per_day) || !Number.isInteger(body.max_pending) ||
        !Number.isInteger(body.interval_seconds) || body.interval_seconds < 3600 ||
        !/^https?:\/\//.test(body.llm_url) || !body.model) {
      setMessage("値を確認してください（Endpointはhttp(s)のURL、Modelは必須、間隔は60分以上、上限は整数）"); return;
    }
    if (body.enabled && !(schedule && schedule.enabled) && !confirm("自動実行をオンにします。" +
        Math.round(body.interval_seconds / 60) + " 分ごとに、未処理の会話を1件ずつ自動で選び、Vaultに提案を新規作成します（1日 " +
        body.max_per_day + " 件まで、未承認が " + body.max_pending + " 件以上なら停止）。")) return;
    try {
      await post("/api/v1/knowledge/settings", body);
      settingsForm.dirty = false;
      setMessage("設定を保存しました。");
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
