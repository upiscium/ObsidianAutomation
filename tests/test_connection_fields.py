"""Per-field settings require individual safe writes and never disclose secrets."""
import json
import os
import threading

import pytest

from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.three_services_server import make_server
from test_three_services_server import request
from test_three_services_connections import save


NEXTCLOUD = {
    "webdav_url": "https://cloud.invalid/remote.php/dav/files/user/",
    "vault_folder": "Vault",
    "nextcloud_user": "fixture_owner_42",
    "nextcloud_password": "SYNTHETIC-NEVER-EXPOSE",
}
GITEA = {
    "git_url": "https://gitea.invalid/u/ObsidianVault.git",
    "git_user": "gitbot",
    "git_token": "SYNTHETIC-GITEA-SECRET",
    "branch": "main",
}


def run_server(store,job):
    server=make_server(port=0,snapshot_setup=store)
    server.snapshot_job=job
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    return server,thread


def setup(tmp_path):
    store=SetupStore(tmp_path/"private",probe=lambda _:True,
                     obscure=lambda _:"fake-obscured-password")
    store.nextcloud_probe=lambda _:True
    job=SnapshotJob(tmp_path/"jobs",lambda _:("no_changes","verify",None),
                    configured=False)
    return store,job


def field(port,provider,name,value):
    path={"nextcloud":"/api/v1/nextcloud/connections/field",
          "gitea":"/api/v1/snapshot/connections/field",
          "github":"/api/v1/daily/connections/field"}[provider]
    code,body=save(port,path,{"field":name,"value":value})
    return code,json.loads(body)


def states(port,provider):
    path={"nextcloud":"/api/v1/nextcloud/connections",
          "gitea":"/api/v1/snapshot/connections",
          "github":"/api/v1/daily/connections"}[provider]
    code,body=request(port,"GET",path)
    assert code==200
    return json.loads(body)


def test_independent_initial_fields_staged_private_then_verified(tmp_path):
    store,job=setup(tmp_path)
    server,thread=run_server(store,job)
    try:
        port=server.server_port
        assert all(v=="missing" for v in states(port,"nextcloud")["fields"].values())
        for index,(name,value) in enumerate(NEXTCLOUD.items()):
            code,result=field(port,"nextcloud",name,value)
            assert code==200
            assert result["field_result"]==("staged" if index<3 else "verified")
            assert result["fields"][name]==("saved" if index<3 else "verified")
            if index<3:
                assert store.load_nextcloud() is None
                assert not store.path.exists()
        assert store.load_nextcloud()==NEXTCLOUD
        assert all(v=="verified" for v in states(port,"nextcloud")["fields"].values())
        pending=store.state/"connection-field-drafts.json"
        assert pending.is_file() and pending.stat().st_mode&0o777==0o600
        for endpoint in ("/api/v1/nextcloud/connections","/api/v1/snapshot/connections"):
            code,body=request(port,"GET",endpoint)
            assert code==200
            for secret in (NEXTCLOUD["nextcloud_password"],
                           NEXTCLOUD["webdav_url"], NEXTCLOUD["nextcloud_user"]):
                assert secret.encode() not in body
        assert job.settings()["enabled"] is False
    finally:
        server.shutdown();thread.join(timeout=3);job.close()


def test_gitea_can_be_completed_one_field_at_time_without_resupplying_nextcloud(tmp_path):
    store,job=setup(tmp_path)
    assert store.save_nextcloud(NEXTCLOUD)
    server,thread=run_server(store,job)
    try:
        port=server.server_port
        for index,(name,value) in enumerate(GITEA.items()):
            code,result=field(port,"gitea",name,value)
            assert code==200
            assert result["field_result"]==("staged" if index<3 else "verified")
        assert store.load()=={**NEXTCLOUD,**GITEA}
        assert all(v=="verified" for v in states(port,"gitea")["fields"].values())
        before=store.path.read_bytes()
        code,result=field(port,"gitea","branch","main")
        assert code==200 and result["field_result"]=="unchanged"
        assert store.path.read_bytes()==before
        for secret in (GITEA["git_token"],GITEA["git_url"]):
            assert secret.encode() not in json.dumps(states(port,"gitea")).encode()
        assert job.settings()["enabled"] is False
    finally:
        server.shutdown();thread.join(timeout=3);job.close()


def test_single_existing_value_rotates_only_that_field_and_pauses_job(tmp_path):
    store,job=setup(tmp_path)
    assert store.save({**NEXTCLOUD,**GITEA})
    job.configured=True
    job.update({"enabled":True,"interval_seconds":3600,
                "source_name":"nextcloud","destination_name":"gitea"})
    assert job.settings()["enabled"] is True
    server,thread=run_server(store,job)
    try:
        port=server.server_port
        code,result=field(port,"gitea","git_token","SYNTHETIC-NEW-GITEA")
        assert code==200 and result["field_result"]=="verified"
        now=store.load()
        assert now["git_token"]=="SYNTHETIC-NEW-GITEA"
        for key,value in {**NEXTCLOUD,**GITEA}.items():
            if key!="git_token":assert now[key]==value
        assert job.settings()["enabled"] is False
    finally:
        server.shutdown();thread.join(timeout=3);job.close()


@pytest.mark.parametrize("group,name,value",[
    ("nextcloud","webdav_url","http://insecure.invalid/"),
    ("nextcloud","vault_folder","../escape"),
    ("nextcloud","nextcloud_user","bad:user"),
    ("gitea","git_url","https://u:pw@gitea.invalid/repo.git"),
    ("gitea","branch","../main"),
    ("gitea","git_token","line\nsecret"),
    ("nextcloud","git_token","cross-provider"),
    ("nextcloud","vault_folder",""),
])
def test_reject_invalid_staged_field_without_private_state_mutation(
    tmp_path,group,name,value,
):
    store,job=setup(tmp_path)
    server,thread=run_server(store,job)
    try:
        url=("/api/v1/nextcloud/connections/field" if group=="nextcloud"
             else "/api/v1/snapshot/connections/field")
        code,body=save(server.server_port,url,{"field":name,"value":value})
        assert code==400
        assert store.load_nextcloud() is None
        assert not (store.state/"connection-field-drafts.json").exists()
    finally:
        server.shutdown();thread.join(timeout=3);job.close()


def test_fail_closed_on_invalid_draft_file(tmp_path):
    store,job=setup(tmp_path)
    path=store.state/"connection-field-drafts.json"
    path.symlink_to(tmp_path/"secret.txt")
    server,thread=run_server(store,job)
    try:
        code,body=request(server.server_port,"GET","/api/v1/nextcloud/connections")
        assert code==503
        code,_=field(server.server_port,"nextcloud","vault_folder","Vault")
        assert code==400
        assert path.is_symlink()
    finally:
        server.shutdown();thread.join(timeout=3);job.close()


def test_oneuse_csrf_and_no_public_fallback(tmp_path):
    store,job=setup(tmp_path)
    server,thread=run_server(store,job)
    try:
        port=server.server_port;path="/api/v1/nextcloud/connections/field"
        payload={"field":"vault_folder","value":"Vault"}
        origin=f"http://127.0.0.1:{port}"
        assert request(port,"POST",path,payload=payload,origin=origin)[0]==403
        csrf=json.loads(request(port,"GET","/api/v1/session")[1])["csrf"]
        assert request(port,"POST",path,payload=payload,origin=origin,
                       csrf=csrf)[0]==200
        assert request(port,"POST",path,payload=payload,origin=origin,
                       csrf=csrf)[0]==403
        assert request(port,"POST","/api/v1/formatter/connections/field",
                       payload=payload,origin=origin)[0]==405
    finally:
        server.shutdown();thread.join(timeout=3);job.close()


def test_staged_gitea_can_be_verified_after_nextcloud_is_ready(tmp_path):
    store, job = setup(tmp_path)
    server, thread = run_server(store, job)
    try:
        port = server.server_port
        for key, value in GITEA.items():
            code, response = field(port, "gitea", key, value)
            assert code == 200 and response["field_result"] == "staged"
        assert states(port, "gitea")["ready_to_verify"] is False
        for key, value in NEXTCLOUD.items():
            assert field(port, "nextcloud", key, value)[0] == 200
        assert states(port, "gitea")["ready_to_verify"] is True
        code, response = save(port, "/api/v1/snapshot/connections/verify", {})
        assert code == 200
        assert json.loads(response)["field_result"] == "verified"
        assert store.load() == {**NEXTCLOUD, **GITEA}
        assert states(port, "gitea")["ready_to_verify"] is False
    finally:
        server.shutdown()
        thread.join(timeout=3)
        job.close()


def test_partial_draft_cannot_be_activated(tmp_path):
    store, job = setup(tmp_path)
    server, thread = run_server(store, job)
    try:
        port = server.server_port
        assert field(port, "gitea", "git_token", GITEA["git_token"])[0] == 200
        code, body = save(port, "/api/v1/snapshot/connections/verify", {})
        assert code == 400
        assert GITEA["git_token"].encode() not in body
        assert store.load() is None
    finally:
        server.shutdown()
        thread.join(timeout=3)
        job.close()


def test_existing_github_credential_does_not_get_a_false_green_check(tmp_path):
    import stat
    from obsidian_automation.connection_fields import FieldDrafts
    store,job=setup(tmp_path)
    try:
        token_path=store.state/"github-daily"/"github.token"
        token_path.parent.mkdir()
        token_path.write_text("fixture-token\n")
        token_path.chmod(0o600)
        assert FieldDrafts(store).status("github")["fields"]["github_token"]=="stale"
        assert FieldDrafts(store).status("github")["ready_to_verify"] is False
    finally:
        job.close()


def test_probe_failure_precedes_per_field_target_quarantine(tmp_path):
    from types import SimpleNamespace
    store,job=setup(tmp_path)
    assert store.save({**NEXTCLOUD,**GITEA})
    job.configured=True
    calls=[]
    server,thread=run_server(store,job)
    server.core_promotion_runtime=SimpleNamespace(
        quarantine_for_target_change=lambda candidate:calls.append(candidate)
    )
    try:
        port=server.server_port
        store.probe=lambda _:False
        updated="Vault-2"
        code,_=field(port,"nextcloud","vault_folder",updated)
        assert code==422
        assert calls==[]
        assert store.load_nextcloud()["vault_folder"]=="Vault"
        store.probe=lambda _:True
        code,result=field(port,"nextcloud","vault_folder",updated)
        assert code==200 and result["field_result"]=="verified"
        assert len(calls)==1
        assert store.load_nextcloud()["vault_folder"]==updated
        assert store.load()["git_token"]==GITEA["git_token"]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        job.close()


def test_post_commit_draft_cleanup_failure_is_not_false_invalid_request(
    tmp_path, monkeypatch,
):
    from obsidian_automation.connection_fields import FieldDrafts, FieldError

    store, job = setup(tmp_path)
    assert store.save({**NEXTCLOUD, **GITEA})
    job.configured=True
    server, thread=run_server(store, job)
    try:
        def fail_after_commit(self, group):
            raise FieldError("unreadable_field_draft")

        monkeypatch.setattr(FieldDrafts,"committed",fail_after_commit)
        code, raw=save(server.server_port,
                       "/api/v1/snapshot/connections/field",
                       {"field":"branch","value":"next"})
        body=json.loads(raw)
        assert code==503
        assert body["error"]=="operation_unavailable"
        assert store.load()["branch"]=="next"
        assert job.settings()["enabled"] is False
    finally:
        server.shutdown()
        thread.join(timeout=3)
        job.close()
