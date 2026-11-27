/* WebDAV user admin panel. Never stores a password: a generated one is shown once. */
(() => {
  "use strict";
  const ERRORS = {
    invalid_name: "ユーザー名が不正です（英小文字で始まり、英数字と . _ - 、3〜32 文字）",
    invalid_password: "パスワードは 12〜128 文字で、コロンと制御文字は使えません",
    invalid_description: "説明は 80 文字までで、制御文字は使えません",
    user_exists: "同じ名前のユーザーがすでにあります",
    user_not_found: "ユーザーが見つかりません",
    user_locked: "このユーザーはロックされています。ロックを解除してから削除してください",
    last_user: "最後の 1 人は、削除できません",
    confirmation_mismatch: "確認のユーザー名が一致しません",
    write_failed: "書き込みに失敗しました。元に戻しました",
    csrf_rejected: "操作セッションが期限切れです。もう一度実行してください",
    origin_rejected: "アクセス元を検証できませんでした",
    too_many_attempts: "認証の失敗が続いたため、しばらく待ってください",
    authentication_required: "認証が必要です。ページを再読み込みしてください",
    invalid_request: "リクエストが不正です",
    operation_unavailable: "結果不明です。一覧で状態を確認してください"
  };
  const describe = (code) => ERRORS[code] || ("操作できませんでした (" + String(code || "unknown") + ")");
  const el = (tag, props, children) => {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const child of children || []) node.appendChild(child);
    return node;
  };
  const text = (tag, value, props) => el(tag, Object.assign({textContent: value}, props || {}));

  const message = document.getElementById("message");
  const usersBox = document.getElementById("users");
  const secretBox = document.getElementById("secret");
  const form = document.getElementById("create");
  const setMessage = (value) => { message.textContent = value; };

  async function api(method, url, body) {
    const headers = {};
    if (method === "POST") {
      const session = await fetch("/api/v1/session", {credentials: "same-origin", cache: "no-store"});
      headers["X-Admin-CSRF"] = (await session.json()).csrf;
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(url, {
      method, credentials: "same-origin", cache: "no-store", headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    let data;
    try { data = await response.json(); } catch (e) { throw Error("operation_unavailable"); }
    if (!response.ok) throw Error(data.error || "operation_unavailable");
    return data;
  }

  function showSecret(name, password) {
    secretBox.textContent = "";
    secretBox.hidden = false;
    const value = text("div", password, {className: "secret-value", id: "secret-value"});
    const copy = text("button", "コピー");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(password); setMessage("コピーしました。"); }
      catch (e) { setMessage("コピーできませんでした。手で選択してください。"); }
    });
    const close = text("button", "閉じる", {id: "secret-close"});
    close.addEventListener("click", () => { secretBox.hidden = true; secretBox.textContent = ""; });
    for (const node of [
      text("h2", "新しいパスワード（ユーザー: " + name + "）"), value,
      text("p", "このパスワードは、この画面でしか表示されません。保管してから閉じてください。",
        {className: "muted"}), copy, close]) secretBox.appendChild(node);
  }

  function resetForm(user) {
    const input = el("input", {type: "password", autocomplete: "new-password",
      placeholder: "空欄なら自動生成（指定する場合は 12 文字以上）"});
    const run = text("button", "再発行する", {className: "reset-run"});
    const cancel = text("button", "やめる");
    const holder = el("div", {className: "row-form"}, [input, run, cancel]);
    run.addEventListener("click", async () => {
      try {
        const body = input.value ? {password: input.value} : {};
        const result = await api("POST", "/api/v1/users/" + user.name + "/password", body);
        if (result.password) showSecret(result.name, result.password);
        setMessage("パスワードを再発行しました。接続している端末の設定も、更新してください。");
      } catch (e) { setMessage(describe(e.message)); }
      await load();
    });
    cancel.addEventListener("click", load);
    return holder;
  }

  function descriptionForm(user) {
    const input = el("input", {value: user.description || "", maxlength: 80});
    const save = text("button", "保存する", {className: "description-save"});
    const cancel = text("button", "やめる");
    const holder = el("div", {className: "row-form"}, [input, save, cancel]);
    save.addEventListener("click", async () => {
      try {
        await api("POST", "/api/v1/users/" + user.name + "/description", {description: input.value});
        setMessage("説明を保存しました。");
      } catch (e) { setMessage(describe(e.message)); }
      await load();
    });
    cancel.addEventListener("click", load);
    return holder;
  }

  async function toggleLock(user) {
    try {
      await api("POST", "/api/v1/users/" + user.name + "/lock", {locked: !user.locked});
      setMessage(user.locked ? "ロックを解除しました。" : "ロックしました。");
    } catch (e) { setMessage(describe(e.message)); }
    await load();
  }

  async function remove(user) {
    if (user.locked) { setMessage(describe("user_locked")); return; }
    if (!confirm("ユーザー " + user.name + " を削除します。このユーザーで接続している端末は、すぐに接続できなくなります。よろしいですか。")) return;
    try {
      await api("POST", "/api/v1/users/" + user.name + "/delete", {confirm: user.name});
      setMessage("削除しました。");
    } catch (e) { setMessage(describe(e.message)); }
    await load();
  }

  function render(data) {
    usersBox.textContent = "";
    const table = el("table");
    table.appendChild(el("tr", {}, ["ユーザー名", "説明", "更新", "操作"].map((label) => text("th", label))));
    for (const user of data.users) {
      const actions = el("td");
      const cell = el("td", {}, [text("span", user.name)]);
      if (user.locked) cell.appendChild(text("span", " 🔒", {className: "lock", title: "ロック中（削除できません）"}));
      const kebab = text("button", "⋮", {className: "kebab", title: "操作", ariaLabel: "操作"});
      actions.appendChild(kebab);
      let menu = null;
      const closeMenu = () => { if (menu) { menu.hidden = true; menu.textContent = ""; } };
      const item = (label, className, run) => {
        const button = text("button", label, {className: "menu-item " + className});
        button.addEventListener("click", () => { closeMenu(); run(); });
        return button;
      };
      kebab.addEventListener("click", () => {
        if (menu && !menu.hidden) { closeMenu(); return; }
        if (!menu) { menu = el("div", {className: "menu"}); actions.appendChild(menu); }
        menu.hidden = false;
        for (const button of [
          item("パスワード再発行", "reset", () => actions.appendChild(resetForm(user))),
          item("説明を編集", "edit", () => actions.appendChild(descriptionForm(user))),
          item(user.locked ? "ロックを解除" : "ロックする", "lock-toggle", () => toggleLock(user)),
          item("削除", "danger delete", () => remove(user))]) menu.appendChild(button);
      });
      table.appendChild(el("tr", {}, [cell, text("td", user.description || "-"),
        text("td", user.updated || "-"), actions]));
    }
    usersBox.appendChild(table);
  }

  async function load() {
    try { render(await api("GET", "/api/v1/users")); }
    catch (e) { setMessage(describe(e.message)); }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = document.getElementById("create-name");
    const description = document.getElementById("create-description");
    const password = document.getElementById("create-password");
    const body = {name: name.value.trim(), description: description.value};
    if (password.value) body.password = password.value;
    try {
      const result = await api("POST", "/api/v1/users", body);
      if (result.password) showSecret(result.name, result.password);
      name.value = description.value = password.value = "";
      setMessage("ユーザーを追加しました。");
    } catch (e) { setMessage(describe(e.message)); }
    await load();
  });

  load();
})();
