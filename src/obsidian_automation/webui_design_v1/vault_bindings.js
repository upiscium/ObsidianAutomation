"use strict";
// #584 P2-4 role folder mapping. Default OFF: without --vault-bindings-ui,
// GET /api/v1/vault-bindings is 404 and this script changes nothing.
// Saving happens only after a valid Preview and an explicit confirmation.
// Labels are rendered as text only.
(function () {
  const ROLE_LABELS = Object.freeze({
    daily: "Daily Note", ai: "AI", idea: "Idea", project: "Project",
    knowledge: "Knowledge", system: "System（Formatter）"
  });
  const REASONS = Object.freeze({
    ok: "確認しました。",
    candidate_missing: "フォルダが見つかりません。",
    unsafe_target: "Vaultの外の共有・外部ストレージ・ファイルなど、使えない場所です。",
    duplicate_folder: "他の役割と同じフォルダです。",
    nested_folder: "他の役割のフォルダの中、または外側にあります。",
    identity_changed: "同じ名前で作り直されたフォルダです。意図した変更なら「新しく固定する」にチェックしてください。",
    outside_root: "Vaultフォルダの外です。",
    root_changed: "Vaultフォルダが変更されています。先に「今の名前に戻す」を行ってください。",
    root_unpinned: "先に「Vaultフォルダの選択」でVaultを確定してください。",
    root_pin_mismatch: "Vaultフォルダの確認が一致しません。",
    path_too_long: "パスが長すぎます。",
    invalid_path: "使えない文字や形式が含まれています（半角英数字・空白・-_. と / のみ）。",
    invalid_roles: "入力内容を確認してください。",
    invalid_accept: "作り直されたフォルダ以外は「新しく固定する」を選べません。",
    identity_alias: "同じ実体を指す別名のフォルダです。",
    identity_unverified: "フォルダの実体を確認できませんでした。",
    stale_source: "Nextcloud接続が変更されました。もう一度確認してください。",
    provider_unproven: "Nextcloud側の前提が未証明のため、この機能は使えません。",
    not_enrolled: "先に「Vaultフォルダの選択」でNextcloudアカウントを確認してください。",
    binding_unavailable: "保存済みのNextcloud接続を確認できません。",
    rate_limited: "短時間に操作が多すぎます。1分ほど待ってから再度お試しください。",
    change_pending: "前回の変更が完了していません。「今の名前に戻す」を行ってください。",
    root_unsettled: "Vaultフォルダの切り替えが完了していません。割り当てがある場合は先に「今の名前に戻す」を行い、「Vaultフォルダの選択」で切り替えを完了させてください。",
    unreadable_record: "保存済みの割り当てを読み取れません。「今の名前に戻す」を行ってください。",
    handle_expired: "確認の有効期限が切れました。もう一度「確認する」を押してください。",
    stale_preview: "確認後にフォルダが変わりました。もう一度「確認する」を押してください。",
    jobs_in_flight: "実行中のジョブがあります。完了後に再度お試しください。",
    already_default: "すでに今の名前のままです。",
    nothing_to_roll_back: "戻せる変更はありません。",
    requires_job_reenable: "完了しました。各ジョブの定期実行は停止しています。確認のうえ再開してください。",
    record_write_uncertain: "書き込み結果が不明です。ジョブは安全のため停止しています。",
    job_pause_failed: "定期実行の停止に失敗しました。ジョブは安全のため実行されません。",
    pending_unfinalized: "変更の記録が完了していません。ジョブは安全のため実行されません。",
    pending_unrecorded: "記録を書き込めなかったため、変更していません。",
    previous_unrecorded: "元に戻すための記録を書き込めなかったため、変更していません。",
    role_unverified: "確認できなかった役割があります。"
  });
  const STATES = Object.freeze({
    default: "今の名前（初期設定）のままです。",
    ok: "保存済みの割り当てを使っています。",
    pending: "前回の変更が完了していません。ジョブは安全のため実行されません。",
    stale_root: "Vaultフォルダが変わったため、この割り当ては使えません。「今の名前に戻す」を行ってください。",
    unreadable: "保存済みの割り当てを読み取れません。ジョブは安全のため実行されません。"
  });
  const message = reason => Object.hasOwn(REASONS, reason)
    ? REASONS[reason] : "確認できませんでした（" + String(reason).slice(0, 40) + "）。";

  async function csrf() {
    const r = await fetch("/api/v1/operation-session", {cache:"no-store", credentials:"same-origin"});
    if (!r.ok) throw Error("csrf_unavailable");
    const body = await r.json();
    if (body.version !== 1 || typeof body.csrf_token !== "string") throw Error("csrf_invalid");
    return body.csrf_token;
  }
  async function post(path, body) {
    const token = await csrf();
    const r = await fetch(path, {method:"POST", cache:"no-store", credentials:"same-origin",
      headers:{"Content-Type":"application/json", "X-Obsidian-CSRF":token},
      body:JSON.stringify(body)});
    if (!r.ok) throw Error("request_failed");
    return r.json();
  }
  async function getStatus() {
    const r = await fetch("/api/v1/vault-bindings", {cache:"no-store", credentials:"same-origin"});
    if (!r.ok) return null;
    const s = await r.json();
    return (s.version === 1 && s.enabled === true) ? s : null;
  }
  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  async function once(button, action) {
    if (button.disabled) return;
    button.disabled = true;
    try { await action(); } finally { button.disabled = false; }
  }

  async function init() {
    let status;
    try { status = await getStatus(); } catch { return; }
    if (!status) return;
    // Place the card after the Vault root picker, which renders asynchronously.
    let anchor = null;
    for (let i = 0; i < 30 && !anchor; i += 1) {
      anchor = document.getElementById("vault-root-picker");
      if (!anchor) await new Promise(resolve => setTimeout(resolve, 100));
    }
    anchor = anchor || document.getElementById("manager-global-nextcloud");
    if (!anchor) return;

    const section = el("section", {id:"vault-bindings", class:"pipeline",
      "aria-label":"役割フォルダの割り当て"});
    section.append(el("h2", {}, "役割フォルダの割り当て"));
    section.append(el("p", {class:"small"},
      "Daily Note・Project・Systemのフォルダを、Vaultフォルダの中の別のフォルダに割り当てます。" +
      "ノートの移動や名前の変更はしません。確認したフォルダだけを保存できます。"));
    const state = el("p", {id:"vault-bindings-state", role:"status", "aria-live":"polite"});
    const table = el("table", {id:"vault-bindings-roles"});
    const result = el("p", {id:"vault-bindings-result", role:"status", "aria-live":"polite"});
    const check = el("button", {type:"button", id:"vault-bindings-preview"}, "確認する");
    const save = el("button", {type:"button", id:"vault-bindings-save", hidden:""}, "保存する");
    const reset = el("button", {type:"button", id:"vault-bindings-reset"}, "今の名前に戻す");
    const rollback = el("button", {type:"button", id:"vault-bindings-rollback"}, "1つ前に戻す");
    section.append(state, table, check, save, reset, rollback, result);
    anchor.after(section);
    const inputs = {};
    const accepts = {};
    const notes = {};
    let previewed = null;

    function render() {
      state.textContent = STATES[status.state] || STATES.unreadable;
      table.replaceChildren();
      const roles = status.roles || status.defaults;
      for (const role of Object.keys(status.defaults)) {
        const row = el("tr");
        row.append(el("th", {scope:"row"}, ROLE_LABELS[role] || role));
        const cell = el("td");
        if (status.editable_roles.includes(role)) {
          const input = el("input", {type:"text", maxlength:"100", autocomplete:"off",
            "aria-label":(ROLE_LABELS[role] || role) + "のフォルダ"});
          input.value = roles[role];
          input.addEventListener("input", clearPreview);
          const accept = el("input", {type:"checkbox", hidden:""});
          accept.addEventListener("change", clearPreview);
          const acceptLabel = el("label", {hidden:""});
          acceptLabel.append(accept, document.createTextNode("新しく固定する"));
          inputs[role] = input; accepts[role] = {box: accept, label: acceptLabel};
          cell.append(input, acceptLabel);
        } else {
          cell.append(el("span", {}, roles[role]),
                      el("span", {class:"small"}, "（AI ホスト側が未対応のため変更できません）"));
        }
        notes[role] = el("span", {class:"small"});
        cell.append(notes[role]);
        row.append(cell);
        table.append(row);
      }
      reset.hidden = !status.can_reset;
      rollback.hidden = !status.can_rollback;
    }
    function clearPreview() {
      previewed = null;
      save.hidden = true;
    }
    async function refresh() {
      try { status = (await getStatus()) || status; } catch { /* keep the last view */ }
      render();
    }

    check.addEventListener("click", () => once(check, async () => {
      clearPreview();
      const roles = {};
      for (const [role, input] of Object.entries(inputs)) roles[role] = input.value.trim();
      const accepted = Object.entries(accepts).filter(([, a]) => a.box.checked).map(([r]) => r);
      result.textContent = "確認しています…";
      let data;
      try { data = await post("/api/v1/vault-bindings/previews",
                              {roles, accept_recreated: accepted}); }
      catch { result.textContent = "確認できませんでした。"; return; }
      for (const role of Object.keys(notes)) {
        const info = (data.roles || {})[role];
        notes[role].textContent = info ? " " + message(info.status) +
          (info.repinned ? "（新しく固定します）" : "") : "";
        if (accepts[role]) {
          const recreated = info && (info.status === "identity_changed" || info.repinned);
          accepts[role].label.hidden = !recreated;
          accepts[role].box.hidden = !recreated;
        }
      }
      if (data.status === "valid" && typeof data.handle === "string") {
        previewed = {handle: data.handle, changed: Object.entries(data.roles)
          .filter(([, v]) => v.changed || v.repinned).map(([r]) => ROLE_LABELS[r] || r)};
        save.hidden = false;
        result.textContent = data.changed || previewed.changed.length
          ? "保存できます。" : "今の割り当てと同じです。";
      } else {
        result.textContent = message(data.reason);
      }
    }));
    save.addEventListener("click", () => once(save, async () => {
      if (!previewed) return;
      const list = previewed.changed.length ? previewed.changed.join("、") : "（変更なし）";
      if (!window.confirm("次の役割の割り当てを保存します: " + list +
          "\n保存後、各ジョブの定期実行は停止します。よろしいですか？")) return;
      const handle = previewed.handle;
      clearPreview();
      result.textContent = "保存しています…";
      try {
        const data = await post("/api/v1/vault-bindings/saves", {handle, confirm: true});
        result.textContent = (data.status === "saved" ? "保存しました。" : "") + message(data.reason);
      } catch { result.textContent = "保存できませんでした。"; }
      await refresh();
    }));
    reset.addEventListener("click", () => once(reset, async () => {
      if (!window.confirm("割り当てを今の名前（初期設定）に戻します。よろしいですか？")) return;
      clearPreview();
      result.textContent = "戻しています…";
      try {
        const data = await post("/api/v1/vault-bindings/resets", {confirm: true});
        result.textContent = (data.status === "reset" ? "今の名前に戻しました。" : "") +
          message(data.reason);
      } catch { result.textContent = "戻せませんでした。"; }
      await refresh();
    }));
    rollback.addEventListener("click", () => once(rollback, async () => {
      if (!window.confirm("割り当てを1つ前の状態に戻します。よろしいですか？")) return;
      clearPreview();
      result.textContent = "戻しています…";
      try {
        const data = await post("/api/v1/vault-bindings/rollbacks", {confirm: true});
        result.textContent = (data.status === "rolled_back" ? "1つ前に戻しました。" : "") +
          message(data.reason);
      } catch { result.textContent = "戻せませんでした。"; }
      await refresh();
    }));
    render();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
