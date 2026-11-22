"""Synthetic, clock-controlled phase scheduler tests; no provider or Vault IO."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from obsidian_automation.phase_schedule import (
    StaleScheduleError, next_slot_after, preview, validate_phase,
)
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.core_promotion_job import CorePromotionJob
from test_three_services_server import request
from obsidian_automation.three_services_server import make_server

JST=timezone(timedelta(hours=9))


def t(hour,minute=0,second=0,day=22):
    return datetime(2026,11,day,hour,minute,second,tzinfo=JST).timestamp()


def hour(ts):
    return datetime.fromtimestamp(ts,JST).strftime("%Y-%m-%d %H:%M:%S")


def test_wall_clock_anchor_crosses_midnight_without_saved_time_drift():
    assert hour(next_slot_after(t(11,59),3600,"12:00")) == "2026-11-22 12:00:00"
    assert hour(next_slot_after(t(12,0),3600,"12:00")) == "2026-11-22 13:00:00"
    assert hour(next_slot_after(t(12,37),3600,"12:00")) == "2026-11-22 13:00:00"
    assert hour(next_slot_after(t(23,59),3600,"12:00")) == "2026-11-23 00:00:00"
    assert hour(next_slot_after(t(23,59),86400,"12:00")) == "2026-11-23 12:00:00"
    p=preview("Asia/Tokyo","12:00",3600,t(12,37))
    assert [hour(x) for x in p["next_slots"]]==[
        "2026-11-22 13:00:00","2026-11-22 14:00:00","2026-11-22 15:00:00"]
    assert p["timezone"]=="Asia/Tokyo"


@pytest.mark.parametrize("tz,anchor,interval",[
    ("UTC","12:00",3600),("Asia/Tokyo","24:00",3600),
    ("Asia/Tokyo","12:60",3600),("Asia/Tokyo","12:1",3600),
    ("Asia/Tokyo","12:00",0),("Asia/Tokyo","12:00",4500),
    ("Asia/Tokyo","12:00",True),("Asia/Tokyo","12:00",86460),
])
def test_phase_rejects_ambiguous_or_unsupported_values(tz,anchor,interval):
    with pytest.raises(ValueError):
        validate_phase(tz,anchor,interval)


@pytest.fixture(params=["snapshot","daily","promotion"])
def job(request,tmp_path):
    clock=[t(12,37)]
    if request.param=="snapshot":
        model=SnapshotJob(tmp_path/"snap",lambda _:("no_changes","verify",None),
                          configured=True,clock=lambda:clock[0])
    elif request.param=="daily":
        model=DailyJob(tmp_path/"daily",lambda :{},configured=lambda:True,
                       source_generation=lambda:"a"*64,clock=lambda:clock[0])
    else:
        model=CorePromotionJob(tmp_path/"promo",lambda :{},
                               configured=lambda:True,source_generation=lambda:"a"*64,
                               clock=lambda:clock[0])
    yield request.param,model,clock
    model.close()


def old_payload(kind,enabled,interval):
    p={"enabled":enabled,"interval_seconds":interval}
    if kind=="snapshot": p.update(source_name="nextcloud",destination_name="gitea")
    return p


def anchor_payload(kind,enabled,interval,revision):
    p=old_payload(kind,enabled,interval)
    p.update(timezone="Asia/Tokyo",anchor_local_time="12:00",expected_revision=revision)
    return p


def test_persisted_job_anchor_off_then_on_with_revision_cas(job):
    kind,model,clock=job
    old=model.settings()
    assert old["schedule"]["mode"]=="legacy_interval"
    assert old["enabled"] is False
    result=model.update(anchor_payload(kind,False,3600,0))
    assert result["enabled"] is False
    assert result["next_due"] is None
    assert result["schedule"] == {
        "mode":"anchored","timezone":"Asia/Tokyo",
        "anchor_local_time":"12:00","revision":1}
    assert model.update(anchor_payload(kind,False,3600,1))==result
    with pytest.raises(StaleScheduleError):
        model.update(anchor_payload(kind,True,3600,0))
    enabled=model.update(anchor_payload(kind,True,3600,1))
    assert enabled["schedule"]["revision"]==2
    assert hour(enabled["next_due"])=="2026-11-22 13:00:00"
    assert model.update(anchor_payload(kind,True,3600,2))==enabled
    clock[0]=t(12,55)
    assert model.settings()["next_due"]==enabled["next_due"]
    paused=model.update(old_payload(kind,False,3600))
    assert paused["schedule"]["mode"]=="anchored"  # internal pause keeps phase
    assert paused["next_due"] is None


def test_jst_scheduler_skips_missed_slot_instead_of_burst(job):
    kind,model,clock=job
    model.update(anchor_payload(kind,True,3600,0))
    assert hour(model.settings()["next_due"])=="2026-11-22 13:00:00"
    clock[0]=t(13,0,8)  # 8s after due => missed
    assert model.tick() is False
    assert hour(model.settings()["next_due"])=="2026-11-22 14:00:00"
    assert model.tick() is False


def test_slot_not_replayed_on_duplicate_tick(job):
    kind,model,clock=job
    model.update(anchor_payload(kind,True,3600,0))
    # Do not start workers; replace only admission after due is durably moved.
    if kind!="daily":
        model.submit=lambda *_:("accepted",None)
    else:
        model._launch_worker=lambda *_:None
    clock[0]=t(13,0,1)
    assert model.tick() is True
    assert hour(model.settings()["next_due"])=="2026-11-22 14:00:00"
    assert model.tick() is False


def test_legacy_interval_remains_unchanged_until_explicit_anchor(job):
    kind,model,clock=job
    result=model.update(old_payload(kind,True,3600))
    assert result["schedule"]["mode"]=="legacy_interval"
    assert result["next_due"]==t(12,37)+3600


def test_fixed_preview_http_cannot_mutate_job_and_enforces_csrf():
    from test_three_services_server import StubJob
    import threading
    with make_server(port=0) as server:
        server.snapshot_job=StubJob("snapshot")
        port=server.server_port
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            url="/api/v1/snapshot/schedule-preview"
            body={"timezone":"Asia/Tokyo","anchor_local_time":"12:00",
                  "interval_seconds":3600}
            host=f"http://127.0.0.1:{port}"
            assert request(port,"POST",url,payload=body,origin=host)[0]==403
            token=json.loads(request(port,"GET","/api/v1/session")[1])["csrf"]
            code,raw=request(port,"POST",url,payload=body,origin=host,csrf=token)
            assert code==200 and len(json.loads(raw)["next_slots"])==3
            assert server.snapshot_job.settings_value["enabled"] is False
            token=json.loads(request(port,"GET","/api/v1/session")[1])["csrf"]
            bad={**body,"timezone":"UTC"}
            assert request(port,"POST",url,payload=bad,origin=host,csrf=token)[0]==400
            assert request(port,"POST","/api/v1/b3/schedule-preview",
                           payload=body,origin=host)[0]==405
        finally:
            server.shutdown()
            thread.join(timeout=3)


@pytest.mark.parametrize("kind",["snapshot","daily","promotion"])
def test_legacy_sqlite_settings_migrate_without_changing_enabled_or_due(kind,tmp_path):
    """An old disk schema and legacy due time must not migrate implicitly."""
    import sqlite3
    clock=[t(12,37)]
    state=tmp_path/kind

    def start():
        if kind=="snapshot":
            return SnapshotJob(state,lambda _:("no_changes","verify",None),
                               configured=True,clock=lambda:clock[0])
        if kind=="daily":
            return DailyJob(state,lambda:{},configured=lambda:True,
                            source_generation=lambda:"a"*64,clock=lambda:clock[0])
        return CorePromotionJob(state,lambda:{},configured=lambda:True,
                                source_generation=lambda:"a"*64,
                                clock=lambda:clock[0])

    with_dummy=start()
    legacy=with_dummy.update(old_payload(kind,True,3600))
    previous_due=legacy["next_due"]
    with_dummy.close()
    name={"snapshot":"snapshot.sqlite3","daily":"daily.sqlite3",
          "promotion":"job.sqlite3"}[kind]
    with sqlite3.connect(state/name) as db:
        for column in ("schedule_tz","schedule_anchor","schedule_revision"):
            db.execute(f"ALTER TABLE settings DROP COLUMN {column}")
    restarted=start()
    try:
        now=restarted.settings()
        assert now["enabled"] is True
        assert now["next_due"]==previous_due
        assert now["schedule"]["mode"]=="legacy_interval"
        assert now["schedule"]["revision"]==0
        # Migration does not run the historical overdue slot.
        assert restarted.run("1"*32) is None
        anchored=restarted.update(anchor_payload(kind,True,3600,0))
        assert anchored["schedule"]["mode"]=="anchored"
        assert anchored["schedule"]["revision"]==1
    finally:
        restarted.close()
    last=start()
    try:
        assert last.settings()["schedule"]["mode"]=="anchored"
        assert last.settings()["schedule"]["anchor_local_time"]=="12:00"
        assert last.settings()["next_due"]==t(13,0)
        assert last.settings()["schedule"]["revision"]==1
    finally:
        last.close()


def test_busy_schedule_skips_slot_without_admitting_second_job(job):
    kind,model,clock=job
    model.update(anchor_payload(kind,True,3600,0))
    if kind=="snapshot":
        model.db.execute(
            "INSERT INTO runs (run_id,request_id,trigger,status,created,stage) "
            "VALUES ('a','b','manual','queued',0,'queued')")
    else:
        model.db.execute(
            "INSERT INTO runs (run_id,request_id,trigger,status,created) "
            "VALUES ('a','b','manual','queued',0)")
    model.db.commit()
    clock[0]=t(13,0,1)
    assert model.tick() is False
    assert model.settings()["next_due"] == t(14)
    assert model.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_versioned_http_settings_are_cas_and_old_format_cannot_revert_mode(tmp_path):
    import threading
    from test_three_services_connections import save
    model=SnapshotJob(tmp_path/"snap",lambda _:("no_changes","verify",None),
                      configured=True,clock=lambda:t(12,37))
    try:
        with make_server(port=0) as server:
            server.snapshot_job=model
            port=server.server_port
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                anchored=anchor_payload("snapshot",False,3600,0)
                code,body=save(port,"/api/v1/snapshot/settings",anchored)
                assert code==200
                assert json.loads(body)["schedule"]["revision"]==1
                assert model.settings()["enabled"] is False
                assert save(port,"/api/v1/snapshot/settings",anchored)[0]==409
                assert save(port,"/api/v1/snapshot/settings",
                            old_payload("snapshot",True,3600))[0]==409
                assert model.settings()["enabled"] is False
                good=anchor_payload("snapshot",False,3600,1)
                assert save(port,"/api/v1/snapshot/settings",good)[0]==200
                assert model.settings()["schedule"]["revision"]==1
            finally:
                server.shutdown()
                thread.join(timeout=3)
    finally:
        model.close()


def test_source_rotation_revokes_old_schedule_save_and_preserves_anchor(job):
    kind, model, clock = job
    configured=model.update(anchor_payload(kind,False,3600,0))
    assert configured["schedule"]["revision"]==1
    assert configured["schedule"]["mode"]=="anchored"
    if kind=="snapshot":
        model.connections_saved("a"*64)
    else:
        model.source_invalidated()
    current=model.settings()
    assert current["enabled"] is False
    assert current["schedule"]=={
        "mode":"anchored","timezone":"Asia/Tokyo",
        "anchor_local_time":"12:00","revision":2}
    assert current["next_due"] is None
    with pytest.raises(StaleScheduleError):
        model.update(anchor_payload(kind,False,3600,1))
    assert model.settings()["schedule"]["revision"]==2
