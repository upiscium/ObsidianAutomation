"use strict";
// #303 Vault root picker. Default OFF: when the app runs without
// --vault-root-ui, GET /api/v1/vault-root is 404 and this script changes nothing.
// Switching happens only after a Preview and an explicit confirmation dialog.
// Labels are rendered as text only.
(function () {
  const REASONS = Object.freeze({
    provider_unproven: "Nextcloud側の前提が未証明のため、この機能は使えません。",
    binding_unavailable: "Nextcloudアカウントの確認が必要です。",
    handle_expired: "候補の有効期限が切れました。もう一度一覧を表示してください。",
    stale_source: "Nextcloud接続が変更されました。もう一度一覧を表示してください。",
    jobs_in_flight: "実行中のジョブがあります。完了後に再度お試しください。",
    rate_limited: "短時間に操作が多すぎます。1分ほど待ってから再度お試しください。",
    candidate_missing: "フォルダが見つかりません。",
    unsafe_target: "共有・外部ストレージなど、Vaultに使えないフォルダです。",
    casefold_collision: "大文字小文字などだけが違う同名フォルダがあります。",
    identity_alias: "同じ実体を指す別名のフォルダです。",
    identity_unverified: "フォルダの実体を確認できませんでした（作り直された可能性があります）。",
    snapshot_unverified: "確認中にフォルダ構成が変わりました。",
    already_selected: "現在のVaultと同じです。",
    requires_revalidation_and_activation: "このフォルダに切り替えられます。",
    enrolled: "Nextcloudアカウントを確認しました。",
    account_mismatch: "保存済みのユーザー名とNextcloudのアカウントが一致しません。",
    account_unverified: "Nextcloudアカウントを確認できませんでした。",
    instance_changed: "以前と別のNextcloudが応答しています。自動では受け付けません（管理者による対応が必要です）。",
    provider_unavailable: "Nextcloudに接続できませんでした。",
    requires_job_reenable: "完了しました。各ジョブの定期実行は停止しています。確認のうえ再開してください。",
    nothing_to_roll_back: "元に戻せる切り替えはありません。",
    rollback_uncertain: "前回の取り消しの結果が不明です。管理者による確認が必要です。",
    binding_diverged: "設定が別の方法で変更されたため、元に戻せません。",
    config_write_uncertain: "設定の書き込み結果が不明です。ジョブは安全のため停止しています。",
    job_pause_failed: "定期実行の停止に失敗しました。ジョブは安全のため実行されません。",
    pin_unfinalized: "記録が完了していません。ジョブは安全のため実行されません。",
    pin_unrecorded: "記録を書き込めなかったため、変更していません。",
    activation_disabled: "切り替え機能は無効です。"
  });
  const message = reason => Object.hasOwn(REASONS, reason)
    ? REASONS[reason] : "確認できませんでした（" + String(reason).slice(0, 40) + "）。";
  const CAVEAT = "確認後に他の端末でフォルダが変更された場合や、サーバーに直接置かれたファイル・再スキャン前の外部ストレージは検出できません。";

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
    const r = await fetch("/api/v1/vault-root", {cache:"no-store", credentials:"same-origin"});
    if (!r.ok) return null;
    const s = await r.json();
    return (s.version === 1 && s.enabled === true && s.writes_vault === false) ? s : null;
  }
  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  async function once(button, action) {
    if (button.disabled) return;  // double-click: one request at a time
    button.disabled = true;
    try { await action(); } finally { button.disabled = false; }
  }

  async function init() {
    let status;
    try { status = await getStatus(); } catch { return; }
    if (!status) return;
    const anchor = document.getElementById("manager-global-nextcloud");
    if (!anchor) return;

    const section = el("section", {id:"vault-root-picker", class:"pipeline",
      "aria-label":"Vaultフォルダの選択"});
    section.append(el("h2", {}, "Vaultフォルダの選択"));
    const current = el("p", {id:"vault-root-current"});
    const state = el("p", {id:"vault-root-state", role:"status", "aria-live":"polite"});
    const enroll = el("button", {type:"button", id:"vault-root-enroll"}, "Nextcloudアカウントを確認");
    const top = el("button", {type:"button", id:"vault-root-list"}, "最上位の候補を表示");
    const rollback = el("button", {type:"button", id:"vault-root-rollback"}, "直前の切り替えを元に戻す");
    const path = el("p", {id:"vault-root-path", class:"small"});
    const list = el("ul", {id:"vault-root-candidates"});
    const result = el("p", {id:"vault-root-result", role:"status", "aria-live":"polite"});
    const apply = el("button", {type:"button", id:"vault-root-activate", hidden:""},
                     "このフォルダに切り替える");
    section.append(el("p", {class:"small"}, "Previewで確認したフォルダだけに切り替えられます。Vault内のファイルは移動・変更しません。" + CAVEAT),
                   current, state, enroll, top, rollback, path, list, result, apply);
    anchor.after(section);
    let generation = 0;
    let previewed = null;

    function render() {
      current.textContent = "現在のVault: " + (typeof status.active_label === "string"
        ? status.active_label : "（未設定）");
      const usable = status.provider !== "unproven";
      enroll.hidden = !usable || status.enrolled === true;
      top.hidden = !usable || status.enrolled !== true;
      rollback.hidden = status.can_rollback !== true;
      if (!usable) state.textContent = message("provider_unproven");
      else if (status.enrolled !== true) state.textContent = message("binding_unavailable");
    }
    async function refresh() {
      try { status = (await getStatus()) || status; } catch { /* keep last */ }
      render();
    }
    function clearPreview() {
      previewed = null;
      apply.hidden = true;
    }

    async function show(parent, trail) {
      const mine = ++generation;
      clearPreview();
      list.replaceChildren();
      result.textContent = "";
      path.textContent = trail.length ? "場所: " + trail.join(" / ") : "場所: 最上位";
      state.textContent = "読み込み中…";
      try {
        const data = await post("/api/v1/vault-root/candidates", parent ? {parent} : {});
        if (mine !== generation) return;
        if (data.status !== "ok" || !Array.isArray(data.candidates)) {
          state.textContent = message(data.reason);
          return;
        }
        state.textContent = data.candidates.length
          ? (data.truncated ? "候補が多いため一部のみ表示しています。" : "候補を選んでください。")
          : "このフォルダの下に候補はありません。";
        for (const c of data.candidates) {
          if (typeof c.handle !== "string" || typeof c.label !== "string") continue;
          const item = el("li");
          item.append(el("span", {}, c.label + " "));
          const open = el("button", {type:"button"}, "中を見る");
          open.addEventListener("click", () => show(c.handle, trail.concat(c.label)));
          const check = el("button", {type:"button"}, "Preview");
          check.addEventListener("click", () => once(check,
            () => preview(c.handle, trail.concat(c.label))));
          item.append(open, check);
          list.append(item);
        }
      } catch {
        if (mine === generation) state.textContent = "候補を取得できませんでした。";
      }
    }
    async function preview(handle, trail) {
      const mine = generation;
      clearPreview();
      result.textContent = "確認中…";
      try {
        const data = await post("/api/v1/vault-root/previews", {candidate: handle});
        if (mine !== generation) return;
        if (data.writes_vault !== false || data.activation_allowed !== false) {
          result.textContent = "想定外の応答のため表示を中止しました。";
          return;
        }
        result.textContent = trail.join(" / ") + ": " + message(data.reason);
        if (data.status === "candidate_valid" && status.activation === "available") {
          previewed = {handle, label: trail.join(" / ")};
          apply.hidden = false;
        }
      } catch {
        if (mine === generation) result.textContent = "Previewを取得できませんでした。";
      }
    }

    enroll.addEventListener("click", () => once(enroll, async () => {
      state.textContent = "確認中…";
      try {
        const data = await post("/api/v1/vault-root/enrollment", {});
        state.textContent = message(data.reason);
      } catch { state.textContent = "確認できませんでした。"; }
      await refresh();
    }));
    top.addEventListener("click", () => show(null, []));
    apply.addEventListener("click", () => once(apply, async () => {
      if (!previewed) return;
      const target = previewed;
      const ok = window.confirm(
        "Vaultを「" + target.label + "」に切り替えます。\n" +
        "・Vault内のファイルは移動・変更しません。\n" +
        "・各ジョブの定期実行は停止します（確認後に再開してください）。\n・" + CAVEAT +
        "\n\n切り替えてよろしいですか？");
      if (!ok) return;
      generation += 1;
      clearPreview();
      list.replaceChildren();
      result.textContent = "切り替え中…";
      try {
        const data = await post("/api/v1/vault-root/activations",
                                {candidate: target.handle, confirm: true});
        result.textContent = (data.status === "activated" ? "切り替えました。" : "") +
          message(data.reason);
      } catch { result.textContent = "切り替えできませんでした。"; }
      await refresh();
    }));
    rollback.addEventListener("click", () => once(rollback, async () => {
      if (!window.confirm("直前のVault切り替えを元に戻します。よろしいですか？")) return;
      generation += 1;
      clearPreview();
      list.replaceChildren();
      result.textContent = "元に戻しています…";
      try {
        const data = await post("/api/v1/vault-root/rollbacks", {confirm: true});
        result.textContent = (data.status === "rolled_back" ? "元に戻しました。" : "") +
          message(data.reason);
      } catch { result.textContent = "元に戻せませんでした。"; }
      await refresh();
    }));
    render();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
