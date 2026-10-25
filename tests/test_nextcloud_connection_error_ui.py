"""Nextcloud save failures must identify HTTP cause, not echo any credentials."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from obsidian_automation.admin_dashboard import JS


@pytest.mark.parametrize("status,expected", [
    (400, "400"),
    (403, "403"),
    (422, "422"),
    (503, "503"),
    (502, "通信"),
])
def test_real_webui_handler_summarizes_http_error_without_secrets(status, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node runtime unavailable")
    begin = JS.index('$("nextcloud-form").addEventListener')
    end = JS.index("async function connectionRefresh", begin)
    post = JS.index("async function snapshotPost")
    post_end = JS.index('$("snapshot-save").addEventListener', post)
    script = JS[post:post_end] + "\n" + JS[begin:end]
    validator = r"""
"use strict";
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const status = Number(process.argv[1]);
const expected = process.argv[2];
const submittedPassword = "SUPER_PRIVATE_SYNTHETIC_APP_PASSWORD";
let requests = 0;
const fields = {
  "nextcloud-message":{textContent:""},
  "nextcloud-form":{handlers:{},addEventListener(type,callback) {this.handlers[type]=callback;}},
};
const form = fields["nextcloud-form"];
const submitButton = {disabled:false};
form.querySelector = () => submitButton;
form.reset = () => {throw Error("must not reset on failure");};
const sandbox = {
  $: id => fields[id],
  FormData: class {
    constructor(_) {}
    *[Symbol.iterator]() {
      yield ["webdav_url","https://cloud.invalid/remote.php/dav/files/user/"];
      yield ["vault_folder","Vault"];
      yield ["nextcloud_user","user"];
      yield ["nextcloud_password",submittedPassword];
    }
  },
  newCsrfToken: async () => "synthetic-csrf",
  fetch: async (path, options) => {
    ++requests;
    assert.equal(path,"/api/v1/nextcloud/connections");
    assert.equal(options.method,"POST");
    assert.equal(options.headers["X-Obsidian-CSRF"],"synthetic-csrf");
    const sent=JSON.parse(options.body);
    assert.equal(sent.nextcloud_password,submittedPassword);
    return {ok:false,status};
  },
  nextcloudRefresh: async()=>{},
  connectionRefresh:async()=>{},
  snapshotRefresh:async()=>{},
  dailyConnectionRefresh:async()=>{},
  dailyRefresh:async()=>{},
  workflowCatalogRefresh:async()=>{},
};
vm.runInNewContext(fs.readFileSync(0,"utf8"),sandbox);
assert.equal(typeof form.handlers.submit,"function");
Promise.resolve(form.handlers.submit({
  preventDefault(){},
  currentTarget:form
})).then(() => {
  assert.equal(requests,1);
  assert.equal(submitButton.disabled,false);
  const message=fields["nextcloud-message"].textContent;
  assert.ok(message.includes(expected),message);
  assert.ok(!message.includes(submittedPassword));
  assert.ok(!message.includes("定期実行の状態を確認"));
}).catch(e => {console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([node,"-e",validator,str(status),expected],
                            input=script,text=True,capture_output=True,timeout=15)
    assert result.returncode == 0, result.stderr


def test_urlsafe_rclone_obscure_and_http_diagnostics_scopes():
    assert "_RCLONE_OBSCURED_RE" not in JS  # private server regex not embedded in browser
    assert "Nextcloud WebDAVの接続検証に失敗" in JS
    assert "WebDAV URLは専用ユーザー" in JS
    assert "Nextcloud設定の保存処理で内部エラー" in JS
