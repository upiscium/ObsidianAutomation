"""Opt-in real Apache mod_dav_fs HTTPS E2E against ephemeral *synthetic* storage.

Run only with OA601_HTTPD and OA601_OPENSSL set to local installed binaries.
Binds localhost, creates no remote shares, and destroys its own temp dir.
"""
from __future__ import annotations

import contextlib
import grp
import http.client
import os
import pwd
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from obsidian_automation import core_promotion_transport as transport

sys.path.insert(0, str(Path(__file__).parent))
from test_core_promotion_transport import _base_remote, _fixture

PORT=18986


def require_executable(name):
    p=os.environ.get(name)
    if not p or not Path(p).is_file() or not os.access(p,os.X_OK):
        raise RuntimeError(f"{name} executable must be explicitly supplied")
    return Path(p)


def apache_config(httpd: Path, root: Path):
    prefix=httpd.parent.parent
    modules=prefix/"modules"
    lines=[
        f'ServerRoot "{prefix}"','ServerName localhost',
        f'Listen 127.0.0.1:{PORT}',f'User {pwd.getpwuid(os.getuid()).pw_name}',
        f'Group {grp.getgrgid(os.getgid()).gr_name}',
        f'PidFile "{root}/httpd.pid"',f'ErrorLog "{root}/apache.log"',
        'LogLevel warn',
    ]
    for n in ("mpm_event","unixd","authz_core","socache_shmcb","ssl","dav","dav_fs","auth_basic","authn_core","authn_file","authz_user"):
        lines.append(f'LoadModule {n}_module "{modules}/mod_{n}.so"')
    lines += [
        f'DavLockDB "{root}/locks/DavLock"',
        f'<VirtualHost 127.0.0.1:{PORT}>',
        'ServerName localhost','SSLEngine on',
        f'SSLCertificateFile "{root}/cert.pem"',
        f'SSLCertificateKeyFile "{root}/key.pem"',
        f'DocumentRoot "{root}/www"',
        f'<Directory "{root}/www">',
        'Dav On','Options None','AllowOverride None',
        'AuthType Basic','AuthName "fixture"',
        f'AuthUserFile "{root}/htpasswd"',
        'Require valid-user',
        '</Directory>','</VirtualHost>',
    ]
    path=root/"httpd.conf"
    path.write_text("\n".join(lines)+"\n")
    return path


@contextlib.contextmanager
def apache_server(root, httpd, openssl):
    (root/"www"/"vault").mkdir(parents=True)
    (root/"locks").mkdir()
    cert=root/"cert.pem"
    subprocess.run([
        str(openssl),"req","-x509","-newkey","rsa:2048","-nodes",
        "-days","1","-keyout",str(root/"key.pem"),"-out",str(cert),
        "-subj","/CN=localhost",
        "-addext","subjectAltName=DNS:localhost,IP:127.0.0.1",
    ],check=True,capture_output=True)
    (root/"key.pem").chmod(0o600)
    user="synthetic-reviewer"
    password="fixture-"+str(PORT)
    subprocess.run([str(httpd.parent/"htpasswd"),"-i","-B","-c",str(root/"htpasswd"),user],
                   input=password+"\n",check=True,text=True,capture_output=True)
    (root/"htpasswd").chmod(0o600)
    conf=apache_config(httpd,root)
    checked=subprocess.run([str(httpd),"-t","-f",str(conf)],
                           check=False,capture_output=True,text=True)
    if checked.returncode:
        raise RuntimeError("Apache fixture config validation failed: "+checked.stderr.strip())
    with socket.socket() as sock:
        sock.settimeout(0.5)
        assert sock.connect_ex(("127.0.0.1",PORT))!=0,"fixture port already in use"
    proc=subprocess.Popen([str(httpd),"-X","-DFOREGROUND","-f",str(conf)],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if proc.poll() is not None:
                raise RuntimeError("Apache fixture unexpectedly stopped")
            try:
                with socket.create_connection(("127.0.0.1",PORT),timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("fixture port did not open")
        ctx=ssl.create_default_context(cafile=str(cert))
        base=f"https://localhost:{PORT}/vault"
        original=transport._connection

        def connect(url,*,timeout):
            assert url.scheme=="https" and url.hostname=="localhost" and url.port==PORT
            return http.client.HTTPSConnection(url.hostname,url.port,timeout=timeout,context=ctx)
        transport._connection=connect
        try:
            yield root/"www"/"vault",base,user,password
        finally:
            transport._connection=original
    finally:
        proc.terminate()
        try:proc.wait(timeout=7)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_apache_full_run(vault,base,user,password,tmp,name="complete"):
    case=tmp/name
    case.mkdir()
    repo,config,plan,checkpoint,receipts=_fixture(case)
    for name,data in _base_remote(plan,repo).items():
        dst=vault/name
        dst.parent.mkdir(parents=True,exist_ok=True)
        dst.write_bytes(data)
        # Apache may issue W/ ETags immediately after a file is written
        # (the server's clock safety heuristic). A real existing Vault file
        # is older than a freshly seeded fixture; pin an old deterministic
        # mtime here, and separately reject weak ETags in unit tests.
        past=time.time()-10
        os.utime(dst,(past,past))
    kwargs=dict(base_url=base,username=user,password=password,timeout=5)
    observed=transport.preflight_remote(plan,**kwargs)
    print("PRE-FLIGHT",[(p.change.action,p.remote.status_code,p.remote.etag,
                         p.disposition) for p in observed])
    assert all(p.disposition=="apply" for p in observed)
    sha,receipt_path,receipt,advanced=transport.execute_promotion(
        plan=plan,core_repository=repo,config_path=config,
        checkpoint_path=checkpoint,receipt_directory=receipts,**kwargs)
    assert receipt_path.is_file() and len(sha)==64
    assert advanced.last_observed_core_commit==plan.head_commit
    assert (vault/"98-System/new/path/feature.css").read_bytes()==b".feature{}\n"
    assert (vault/"98-System/01-script/task.js").read_bytes()==b"version=2\n"
    assert not (vault/"98-System/obsolete.md").exists()
    assert [(o.action,o.result) for o in receipt.outcomes]==[
        ("update","applied"),("create","applied"),("delete","applied")
    ] or {o.result for o in receipt.outcomes}=={"applied"}
    recheck=transport.ensure_parent_collections(path="98-System/new/path/feature.css",
                                                 transport=None,**kwargs)
    assert recheck==()
    # The actual Apache server must enforce both conditional protection
    # modes, not merely return a 2xx because the fixture has no competing
    # writer. A conflict can never overwrite the verified result.
    target=base+"/98-System/01-script/task.js"
    attempted=b"must-not-overwrite-human-or-core"
    for headers in ({"If-Match": '"a-deliberately-stale-etag"'},
                    {"If-None-Match": "*"}):
        refused=transport._real_http_request(
            method="PUT",target_url=target,headers=headers,body=attempted,
            username=user,password=password,timeout=5,response_limit=65536)
        assert refused.status in (412, 409),refused.status
        assert (vault/"98-System/01-script/task.js").read_bytes()==b"version=2\n"
    # Unauthorized Basic credentials must never enable writer effects.
    bad=transport._real_http_request(
        method="GET",target_url=target,headers={},body=None,
        username=user,password=password+"-wrong",timeout=5,response_limit=65536)
    assert bad.status==401,bad.status
    print("E2E APACHE TLS BASIC AUTH: nested CREATE/UPDATE/DELETE, CAS and checkpoint: PASS")
    print("E2E Apache strong If-Match / If-None-Match and Basic auth rejection: PASS")


def test_real_mkcol_conflicts(vault,base,user,password):
    kwargs=dict(username=user,password=password,timeout=5)
    root=base.rstrip("/")
    parent=root+"/manual/"
    missing=root+"/no-parent/child/"
    # Missing intermediate path must be 409 under Apache.
    missing_resp=transport._real_http_request(method="MKCOL",target_url=missing,
                  headers={},body=None,response_limit=65536,**kwargs)
    assert missing_resp.status==409,missing_resp.status
    created=transport._real_http_request(method="MKCOL",target_url=parent,
                  headers={},body=None,response_limit=65536,**kwargs)
    assert created.status==201,created.status
    existing=transport._real_http_request(method="MKCOL",target_url=parent,
                  headers={},body=None,response_limit=65536,**kwargs)
    assert existing.status==405,existing.status
    from obsidian_automation.webdav_connection import _collection_multistatus, PROPFIND_BODY
    response=transport._real_http_request(method="PROPFIND",target_url=parent,
               headers={"Depth":"0","Content-Type":"application/xml"},
               body=PROPFIND_BODY,response_limit=65536,**kwargs)
    assert response.status==207 and _collection_multistatus(response.body,parent)
    print("REAL Apache MKCOL 409 / 201 / 405 and strict recheck: PASS")


def test_proposal_publish(www,user,password):
    """B1-lite phase 2 on real Apache: create-only, readback, no overwrite."""
    from obsidian_automation.knowledge_proposal import Proposal, PROPOSAL_EMBED
    from obsidian_automation.knowledge_proposal_publish import PublishError, publish_proposal
    note=("---\ntype: knowledge-proposal\nstatus: proposed\ncategory: x\nmaturity: draft\n"
          "source_type: y\n---\n\n"+PROPOSAL_EMBED+"\n日本語の本文。\n").encode()
    path="04-AI/Proposals/2026-11-26-real-apache.md"
    proposal=Proposal(target_path=path,content=note,context_sha256="a"*64,proposal_sha256="b"*64,
                      generation_sha256="c"*64,model_identifier="m",source_sha256="d"*64)

    class Shared:
        def load_nextcloud(self):
            return {"webdav_url":f"https://localhost:{PORT}/","vault_folder":"/",
                    "nextcloud_user":user,"nextcloud_password":password}

    receipt=publish_proposal(proposal,shared=Shared())
    assert receipt["outcome"]=="created",receipt
    assert (www/path).read_bytes()==note
    from obsidian_automation.knowledge_proposal_publish import count_pending_proposals
    assert count_pending_proposals(shared=Shared())==1  # real Apache Depth: 1 listing
    again=publish_proposal(proposal,shared=Shared())
    assert again["outcome"]=="already_present" and again["vault_write"] is False
    (www/path).write_bytes(b"edited by the owner")
    try:
        publish_proposal(proposal,shared=Shared())
    except PublishError as exc:
        assert str(exc)=="proposal_exists"
    else:
        raise AssertionError("an existing file must never be overwritten")
    assert (www/path).read_bytes()==b"edited by the owner"
    (www/path).unlink()
    assert count_pending_proposals(shared=Shared())==0
    print("REAL Apache proposal publish: created / already_present / never overwritten: PASS")


def rclone_root_snapshot(root,user,password,base):
    """Real rclone ``sync nextcloud:/`` through the production config writer."""
    rclone=os.environ.get("OA601_RCLONE")
    if not rclone:
        print("rclone root-Vault sync: SKIPPED (OA601_RCLONE not set)")
        return
    from obsidian_automation.snapshot_setup import _rclone_config
    out=subprocess.run([rclone,"obscure","-"],input=password+"\n",check=True,
                       capture_output=True,text=True).stdout.strip()
    cfg=root/"rclone.conf"
    cfg.write_text(_rclone_config({"webdav_url":base,"nextcloud_user":user},out))
    cfg.chmod(0o600)
    mirror=root/"rclone-mirror"
    from obsidian_automation.vault_snapshot import TRANSFER_RESIDUE_RCLONE_FILTER
    residue="ca05901997a09923835bd9e24dc9d2f1.ocTransferId1007821865.part"
    (root/"www"/residue).write_bytes(b"partial")
    subprocess.run([rclone,"sync","nextcloud:/",str(mirror),"--config",str(cfg),
                    "--exclude",TRANSFER_RESIDUE_RCLONE_FILTER,
                    "--ca-cert",str(root/"cert.pem"),"--ignore-times",
                    "--delete-after","--retries","1"],check=True,
                   capture_output=True,timeout=120)
    assert (mirror/"98-System/01-script/task.js").read_bytes()==b"version=2\n"
    assert (mirror/"98-System/new/path/feature.css").read_bytes()==b".feature{}\n"
    assert not (mirror/"98-System/obsolete.md").exists()
    assert not (mirror/residue).exists()
    print("E2E real rclone sync nextcloud:/ against root-Vault Apache: PASS")


def main():
    httpd=require_executable("OA601_HTTPD")
    openssl=require_executable("OA601_OPENSSL")
    with tempfile.TemporaryDirectory(prefix="oa601-apache-real-") as temp:
        root=Path(temp)
        with apache_server(root,httpd,openssl) as (vault,base,user,password):
            test_real_mkcol_conflicts(vault,base,user,password)
            test_apache_full_run(vault,base,user,password,root)
            # #607: the DAV root itself is the Vault (vault_folder="/"), as on
            # the production Apache where DocumentRoot is the Vault.
            from obsidian_automation.webdav_connection import vault_collection_url
            root_base=vault_collection_url(f"https://localhost:{PORT}/","/")
            assert root_base==f"https://localhost:{PORT}/"
            test_apache_full_run(root/"www",root_base,user,password,root,"root-vault")
            test_proposal_publish(root/"www",user,password)
            print("E2E APACHE ROOT-AS-VAULT (vault_folder=/): PASS")
            rclone_root_snapshot(root,user,password,root_base)
    print("Synthetic fixture, TLS certificate and server process cleaned")


if __name__=="__main__":
    main()
