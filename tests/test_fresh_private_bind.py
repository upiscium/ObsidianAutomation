"""Private RFC1918 WebUI: same Origin/CSRF; no additional proxy/systemd."""
import http.client
from pathlib import Path
import json
import threading

import pytest

from obsidian_automation.admin_dashboard import make_server, validate_bind_address

ROOT = Path(__file__).resolve().parents[1]


def test_validated_lan_binding_refuses_public_and_wildcard():
    for value in ("127.0.0.1","10.12.2.15","172.16.22.5","192.168.1.10"):
        assert validate_bind_address(value)==value
    for value in ("0.0.0.0","8.8.8.8","localhost","::","127.0.0.2","",
                  "10.12.2.15\nevil",None,123):
        with pytest.raises(ValueError):
            validate_bind_address(value)
    with pytest.raises(ValueError):
        make_server(port=0,bind_address="0.0.0.0")
    with pytest.raises(ValueError):
        make_server(port=0,bind_address="8.8.8.8")


def test_host_origin_policy_remains_single_exact_private_bind():
    """Mock only bound-address advertisement; HTTP handler executes for real."""
    with make_server(port=0,observer=lambda:[],ai_observer=lambda:{}) as server:
        actual_port=server.server_address[1]
        # Tests/CI need no 10.12 interface; advertise the private source
        # address while preserving an actual loopback TCP listener.
        server.server_address=("10.12.2.15",actual_port)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def call(path, host, origin=None):
            conn=http.client.HTTPConnection("127.0.0.1",actual_port,timeout=4)
            headers={"Host":host}
            if origin is not None: headers["Origin"]=origin
            conn.request("GET",path,headers=headers)
            resp=conn.getresponse()
            status,body=resp.status,resp.read()
            conn.close()
            return status,body
        try:
            right=f"10.12.2.15:{actual_port}"
            wrong=f"127.0.0.1:{actual_port}"
            assert call("/api/v1/workflows",right)[0]==200
            assert call("/api/v1/workflows",right,"http://"+right)[0]==200
            assert call("/api/v1/workflows",wrong)[0]==403
            assert call("/api/v1/workflows",right,"http://evil.invalid")[0]==403
            assert call("/api/v1/workflows",right,"http://"+wrong)[0]==403
        finally:
            server.shutdown()
            thread.join(3)


def test_snapshot_app_passes_private_bind_without_enabling_work(tmp_path,monkeypatch):
    from obsidian_automation import snapshot_app
    import sys

    observed = []
    class FakeServer:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def serve_forever(self,**_):
            observed.append((
                self.snapshot_job.settings()["enabled"],
                self.daily_job.settings()["enabled"],
                self.daily_job.settings()["configured"],
            ))

    def fake_server(**kwargs):
        assert kwargs["bind_address"]=="10.12.2.15"
        assert kwargs["port"]==8370
        return FakeServer()

    monkeypatch.setattr(snapshot_app,"make_server",fake_server)
    monkeypatch.setattr(sys,"argv",[
        "obsidian-snapshot-app","--state",str(tmp_path / "fresh"),
        "--bind","10.12.2.15",
    ])
    assert snapshot_app.main()==0
    assert observed==[(False,False,False)]


def test_installer_plan_cli_explicit_private_host_is_pure(tmp_path,capsys):
    import importlib.util
    import sys
    spec=importlib.util.spec_from_file_location(
        "private_bind_lxc_installer", str(ROOT / "tools" / "install_snapshot_lxc.py")
    )
    assert spec is not None and spec.loader is not None
    impl=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=impl
    spec.loader.exec_module(impl)
    root=tmp_path/"source"
    root.mkdir()
    (root/"pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=75"]\n'
    )
    assert impl.main(["--source-root",str(root),"--bind-address","10.12.2.15"])==0
    data=json.loads(capsys.readouterr().out)
    assert data["mode"]=="plan_only"
    unit=next(x["content"] for x in data["steps"] if x["kind"]=="write_file")
    assert "--bind 10.12.2.15" in unit
