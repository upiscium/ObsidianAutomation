/* Manual chat-history import panel. Count-only; no Vault write, no schedule.
   Rendered only when the server was started with --chat-import-ui. */
(() => {
  "use strict";
  const main = document.querySelector("main");
  if (!main) return;
  const mount = (document.getElementById && document.getElementById("panels")) || main;
  const LABELS = {chatgpt: "ChatGPT", claude: "Claude"};
  const errors = {
    import_busy: "別の取り込み処理が進行中です",
    upload_too_large: "ファイルが大きすぎます",
    invalid_request: "リクエストが不正です",
    not_a_valid_export: "有効なエクスポートZIPとして読み取れません",
    unsupported_export_format: "ChatGPT / Claudeのエクスポート形式として認識できません",
    upload_truncated: "アップロードが途中で切れました",
    upload_timeout: "アップロードが時間切れです",
    upload_unavailable: "アップロードを受け取れませんでした",
    csrf_rejected: "操作セッションが期限切れです。もう一度実行してください",
    origin_rejected: "アクセス元を検証できませんでした",
    scope_contract_changed: "この取り込み先は別の形式に固定されています",
    import_storage_unavailable: "保存領域を使えません",
    journal_missing_with_ready_archives: "取り込み履歴が欠けています。管理者による確認が必要です",
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
  async function postJson(url, body) {
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
  function uploadZip(file, onProgress) {
    return session().then((csrf) => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/chat-import/upload");
      xhr.setRequestHeader("Content-Type", "application/zip");
      xhr.setRequestHeader("X-Obsidian-CSRF", csrf);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      xhr.onload = () => {
        let data;
        try { data = JSON.parse(xhr.responseText); } catch (e) { reject(Error("operation_unavailable")); return; }
        if (xhr.status === 200) resolve(data); else reject(Error(data.error || "operation_unavailable"));
      };
      xhr.onerror = () => reject(Error("operation_unavailable"));
      xhr.send(file);
    }));
  }

  const fileInput = el("input", {type: "file", id: "chat-import-file", accept: ".zip,application/zip"});
  const uploadButton = text("button", "アップロードして形式を判定", {id: "chat-import-upload", disabled: true});
  const provider = el("select", {id: "chat-import-provider", disabled: true}, Object.keys(LABELS).map(
    (key) => text("option", LABELS[key], {value: key})));
  const previewButton = text("button", "在庫をプレビュー", {id: "chat-import-preview", className: "outline", disabled: true});
  const commitButton = text("button", "在庫を確定", {id: "chat-import-commit", disabled: true});
  const message = text("p", "", {id: "chat-import-message", className: "feedback"});
  const summary = text("dl", "", {id: "chat-import-summary"});
  const note = text("p",
    "ChatGPT / Claudeのエクスポートを、個人用の取り込み領域に保管し件数だけを記録します。" +
    "Vault・WebDAV・外部サービスへは何も書き込まず、自動実行もしません。", {className: "muted"});

  const panel = el("section", {className: "connections panel", id: "chat-import"}, [
    el("div", {className: "service-header"}, [text("h2", "チャット履歴の取り込み（ChatGPT / Claude）")]), note,
    el("label", {}, [text("span", "エクスポートZIP"), fileInput]),
    el("div", {className: "actions"}, [uploadButton]),
    el("label", {}, [text("span", "形式"), provider]),
    el("div", {className: "actions"}, [previewButton, commitButton]),
    message, summary
  ]);
  mount.appendChild(panel);

  let busy = false;
  const setBusy = (value) => {
    busy = value;
    uploadButton.disabled = value || !fileInput.files || !fileInput.files.length;
    provider.disabled = value;
    previewButton.disabled = value;
    commitButton.disabled = value;
  };
  const setMessage = (value) => { message.textContent = value; };

  function row(label, value) {
    return [text("dt", label), text("dd", String(value))];
  }
  function renderSummary(status) {
    const rows = [];
    rows.push(...row("保管済みZIP", status.ready_archives));
    for (const key of Object.keys(LABELS)) {
      const scope = (status.scopes || {})["private-" + key];
      if (!scope) { rows.push(...row(LABELS[key], "未取り込み")); continue; }
      const last = scope.last_run;
      rows.push(...row(LABELS[key],
        "会話 " + scope.known_conversations + " 件" +
        (last ? "（最終: 追加 " + last.added + " / 変更 " + last.changed +
          " / 変更なし " + last.unchanged + " / 保留 " + last.quarantined_slots + "）" : "")));
    }
    summary.textContent = "";
    for (const node of rows) summary.appendChild(node);
  }
  async function refresh() {
    try { renderSummary(await get("/api/v1/chat-import")); }
    catch (e) { setMessage(describe(e.message)); }
  }
  function receiptText(result) {
    const runs = result.runs || [];
    if (!runs.length) return "この形式の未処理のZIPはありません（取り込み済みか、別の形式のZIPだけです）";
    return runs.map((run) => run.status === "normalization_rejected"
      ? "このZIPは形式を解釈できませんでした（拒否として記録）"
      : "追加 " + run.added + " / 変更 " + run.changed + " / 変更なし " + run.unchanged +
        " / 保留 " + run.quarantined_slots + " / 取り込んだ発言 " + run.retained_messages).join("；");
  }

  fileInput.addEventListener("change", () => setBusy(busy));
  uploadButton.addEventListener("click", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file || busy) return;
    setBusy(true);
    setMessage("アップロード中…");
    try {
      const result = await uploadZip(file, (ratio) => setMessage("アップロード中… " + Math.round(ratio * 100) + "%"));
      provider.value = result.provider;
      setMessage(LABELS[result.provider] + " のエクスポートとして保管しました" +
        "（会話 " + result.counts.conversations + " 件 / 発言 " + result.counts.messages +
        " 件 / 空の枠 " + result.counts.empty_slots + "）。在庫をプレビューしてください。");
      fileInput.value = "";
    } catch (e) {
      setMessage(describe(e.message));
    } finally {
      setBusy(false);
      await refresh();
    }
  });
  async function inventory(commit) {
    if (busy) return;
    if (commit && !confirm(LABELS[provider.value] + " の在庫を確定します。Vaultには書き込みません。")) return;
    setBusy(true);
    setMessage(commit ? "在庫を確定中…" : "プレビュー中…");
    try {
      const result = await postJson("/api/v1/chat-import/inventory", {provider: provider.value, commit});
      setMessage((commit ? "確定しました: " : "プレビュー: ") + receiptText(result));
    } catch (e) {
      setMessage(describe(e.message));
    } finally {
      setBusy(false);
      await refresh();
    }
  }
  previewButton.addEventListener("click", () => inventory(false));
  commitButton.addEventListener("click", () => inventory(true));

  // Hide the whole panel unless the server enabled it for this process.
  panel.hidden = true;
  get("/api/v1/chat-import").then((status) => {
    if (!status || status.version !== 1 || status.enabled !== true) return;
    panel.hidden = false;
    setBusy(false);
    renderSummary(status);
  }).catch(() => { panel.hidden = true; });
})();
