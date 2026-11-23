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


def test_apache_full_run(vault,base,user,password,tmp):
    case=tmp/"complete"
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


def main():
    httpd=require_executable("OA601_HTTPD")
    openssl=require_executable("OA601_OPENSSL")
    with tempfile.TemporaryDirectory(prefix="oa601-apache-real-") as temp:
        root=Path(temp)
        with apache_server(root,httpd,openssl) as (vault,base,user,password):
            test_real_mkcol_conflicts(vault,base,user,password)
            test_apache_full_run(vault,base,user,password,root)
    print("Synthetic fixture, TLS certificate and server process cleaned")


if __name__=="__main__":
    main()
