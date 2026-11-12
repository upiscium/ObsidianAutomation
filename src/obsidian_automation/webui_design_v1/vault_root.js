"use strict";
// #303 V1/C read-only Vault root picker. Default OFF: when the server has no
// picker service, GET /api/v1/vault-root is 404 and this script changes nothing.
// There is no save or activation control. Labels are rendered as text only.
(function () {
  const REASONS = Object.freeze({
    provider_unproven: "Nextcloud側の前提（ID・一覧・整合性）が未証明のため、候補を表示できません。",
    binding_unavailable: "保存済みのNextcloud接続を確認できません。",
    handle_expired: "候補の有効期限が切れました。もう一度一覧を表示してください。",
    stale_source: "Nextcloud接続が変更されました。もう一度一覧を表示してください。",
    jobs_in_flight: "実行中のジョブがあります。完了後に再確認してください。",
    rate_limited: "短時間に操作が多すぎます。1分ほど待ってから再度お試しください。",
    candidate_missing: "フォルダが見つかりません。",
    unsafe_target: "共有・外部ストレージなど、Vaultに使えないフォルダです。",
    casefold_collision: "大文字小文字などだけが違う同名フォルダがあります。",
    identity_alias: "同じ実体を指す別名のフォルダです。",
    snapshot_unverified: "確認中にフォルダ構成が変わりました。",
    already_selected: "現在のVaultと同じです。",
    requires_revalidation_and_activation:
      "Previewは有効です。切り替えはまだ行われていません（別途承認が必要）。"
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
  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function init() {
    let status;
    try {
      const r = await fetch("/api/v1/vault-root", {cache:"no-store", credentials:"same-origin"});
      if (!r.ok) return;
      status = await r.json();
    } catch { return; }
    if (status.version !== 1 || status.enabled !== true || status.writes_vault !== false) return;
    const anchor = document.getElementById("manager-global-nextcloud");
    if (!anchor) return;

    const section = el("section", {id:"vault-root-picker", class:"pipeline",
      "aria-label":"Vaultフォルダの候補確認"});
    section.append(el("h2", {}, "Vaultフォルダの候補確認（読み取りのみ）"));
    section.append(el("p", {class:"small"},
      "ここでは候補の確認だけを行います。現在のVault設定は変更されません。"));
    const state = el("p", {id:"vault-root-state", role:"status", "aria-live":"polite"});
    const path = el("p", {id:"vault-root-path", class:"small"});
    const list = el("ul", {id:"vault-root-candidates"});
    const result = el("p", {id:"vault-root-result", role:"status", "aria-live":"polite"});
    const top = el("button", {type:"button", id:"vault-root-list"}, "最上位の候補を表示");
    section.append(state, top, path, list, result);
    anchor.after(section);

    state.textContent = status.provider === "verified"
      ? "候補を表示できます。"
      : status.provider === "restricted"
        ? "候補を表示できます。ただし確認後に他の端末でフォルダが変更された場合や、サーバーに直接置かれたファイル・再スキャン前の外部ストレージは検出できません。"
        : message("provider_unproven");
    let generation = 0;

    async function show(parent, trail) {
      const mine = ++generation;
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
          check.addEventListener("click", async () => {
            if (check.disabled) return;  // double-click: one request at a time
            check.disabled = true;
            try { await preview(c.handle, trail.concat(c.label)); }
            finally { check.disabled = false; }
          });
          item.append(open, check);
          list.append(item);
        }
      } catch {
        if (mine === generation) state.textContent = "候補を取得できませんでした。";
      }
    }
    async function preview(handle, trail) {
      const mine = generation;
      result.textContent = "確認中…";
      try {
        const data = await post("/api/v1/vault-root/previews", {candidate: handle});
        if (mine !== generation) return;
        if (data.writes_vault !== false || data.activation_allowed !== false) {
          result.textContent = "想定外の応答のため表示を中止しました。";
          return;
        }
        result.textContent = trail.join(" / ") + ": " + message(data.reason);
      } catch {
        if (mine === generation) result.textContent = "Previewを取得できませんでした。";
      }
    }
    top.addEventListener("click", () => show(null, []));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
