"""Pure Project watcher list: no Vault/DB/GitHub/network access."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from obsidian_automation.github_project_watch_view import (
    GlobalWatchStatus, ProjectWatchConfig, ProjectWatchObservation,
    ProjectWatchProjectionUnavailable, project_watch_projection,
)

NOW=datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
PATH="10-Project/Terreate/Terreate.md"
REPO="upiscium/TERREATE"


def config(**kw):
    return replace(
        ProjectWatchConfig(PATH,"running",True,REPO), **kw,
    )


def state(**kw):
    return replace(GlobalWatchStatus(True,True,True),**kw)


def observed(**kw):
    return replace(
        ProjectWatchObservation(PATH,REPO,NOW-timedelta(minutes=10)),
        **kw,
    )


def show(projects=None, histories=None, global_status=None, **kwargs):
    return project_watch_projection(
        (config(),) if projects is None else projects,
        (observed(),) if histories is None else histories,
        state() if global_status is None else global_status,
        now=kwargs.pop("now",NOW),**kwargs,
    )


def item(**kw):
    return show(**kw)["projects"][0]


def test_fresh_watcher_is_observation_not_canonical_write():
    result=item()
    assert result["monitoringState"]=="recently_observed"
    assert result["lastVerifiedVaultWrite"] is None
    assert result["lastWatcherObservedAt"] is not None
    assert result["repository"]==REPO


def test_disabled_project_must_remain_in_list():
    result=item(projects=(config(github_watch=False),))
    assert result["monitoringState"]=="disabled_by_project"
    assert result["githubWatchConfigured"] is False
    assert result["lastVerifiedVaultWrite"] is None


def test_missing_flag_does_not_become_enabled():
    result=item(projects=(config(github_watch=None),))
    assert result["monitoringState"]=="configuration_unknown"


@pytest.mark.parametrize("repo", (None,"","bad repo","../../private","a/..","a/b/c"))
def test_invalid_repo_is_misconfigured_but_not_leaked(repo):
    result=item(projects=(config(repository=repo),))
    assert result["monitoringState"]=="misconfigured"
    assert result["repository"] is None


@pytest.mark.parametrize("s", ("done","stopped","cancelled"))
def test_terminal_project_stays_visible_ineligible(s):
    assert item(projects=(config(status=s),))["monitoringState"]=="ineligible_status"


def test_global_off_with_verified_legacy_exclusion_pauses_effectively():
    result=item(global_status=state(desired_enabled=False))
    assert result["monitoringState"]=="paused_globally"
    assert show(global_status=state(desired_enabled=False))["globalControlVerified"] is True


def test_global_off_is_not_proven_when_old_lxc_may_still_run_watcher():
    result=item(global_status=state(desired_enabled=False,legacy_watchers_excluded=False))
    assert result["monitoringState"]=="global_control_unverified"
    assert show(global_status=state(desired_enabled=False,legacy_watchers_excluded=False))["globalControlVerified"] is False


def test_unknown_authority_never_advertises_active_or_paused():
    for g in (
        state(authoritative_runtime=False),
        state(legacy_watchers_excluded=False),
        state(desired_enabled=None),
    ):
        assert item(global_status=g)["monitoringState"]=="global_control_unverified"


def test_no_matching_history_is_not_observed():
    assert item(histories=())["monitoringState"]=="not_observed"


def test_repo_renamed_does_not_inherit_old_last_observation():
    result=item(projects=(config(repository="upiscium/Another"),))
    assert result["monitoringState"]=="binding_changed"
    assert result["lastWatcherObservedAt"] is None


def test_stale_observation_is_distinguished_from_recent():
    result=item(histories=(observed(observed_at=NOW-timedelta(hours=3)),))
    assert result["monitoringState"]=="stale"
    assert result["lastWatcherObservedAt"] is not None


def test_future_observation_cannot_be_recent():
    result=item(histories=(observed(observed_at=NOW+timedelta(minutes=5)),))
    assert result["monitoringState"]=="observation_invalid"
    assert result["lastWatcherObservedAt"] is None


def test_multiple_projects_sort_deterministically_and_bound_only_names():
    a=config()
    b=replace(a,project_path="10-Project/TSUMGI/TSUMGI.md",repository="upiscium/TSUMGI",github_watch=False)
    result=show(projects=(b,a),histories=(observed(),))
    assert result["projectCount"] == 2
    assert [p["name"] for p in result["projects"]] == ["TSUMGI","Terreate"]
    assert len({p["projectId"] for p in result["projects"]}) == 2
    assert "projectPath" not in result["projects"][0]


def test_top_level_project_is_a_valid_watch_record():
    p=config(project_path="10-Project/Standalone.md")
    result=item(projects=(p,), histories=())
    assert result["name"]=="Standalone"
    assert result["monitoringState"]=="not_observed"


@pytest.mark.parametrize("bad_status",(None,"","unexpected"))
def test_invalid_project_status_remains_visible_but_redacted(bad_status):
    result=item(projects=(config(status=bad_status),))
    assert result["monitoringState"]=="misconfigured_status"
    assert result["projectStatus"] is None


def test_empty_set_returns_normal_valid_observation():
    result=show(projects=(),histories=())
    assert result["projectCount"]==0
    assert result["projects"]==()


@pytest.mark.parametrize("bad", (
    "10-Project/../private/x.md",
    "/absolute/Terreate.md",
    "01-Inbox/Idea.md",
    "10-Project/X/X.txt",
    "10-Project/X/../../x.md",
    "10-Project/X/X\x00.md",
))
def test_unsafe_note_path_rejected(bad):
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(projects=(config(project_path=bad),))


def test_duplicate_project_ids_rejected():
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(projects=(config(),config()))


def test_duplicate_historical_project_records_rejected():
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(histories=(observed(),observed()))


def test_invalid_history_path_or_repo_rejected():
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(histories=(observed(project_path="../../secret"),))
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(histories=(observed(repository="bad repo"),))


@pytest.mark.parametrize("t", (
    None,"invalid",datetime(2026,11,6,12,0),True,
))
def test_invalid_observation_clock_fails_closed(t):
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(histories=(observed(observed_at=t),))


@pytest.mark.parametrize("g", (
    GlobalWatchStatus(0, True, True),
    GlobalWatchStatus(True, 1, True),
    GlobalWatchStatus(True, True, 1),
    object(),
))
def test_invalid_global_authority_fails_closed(g):
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(global_status=g)


@pytest.mark.parametrize("limit", (0, 59, 86401, True, 60.0))
def test_invalid_staleness_budget_rejected(limit):
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(stale_after_seconds=limit)


def test_unbounded_project_list_rejected():
    projects=tuple(
        config(project_path=f"10-Project/W/S{i}.md")
        for i in range(257)
    )
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(projects=projects,histories=())


def test_output_schema_contains_no_raw_note_body_or_success_claim():
    result=show()
    assert set(result) == {
        "schemaVersion","projectedAt","globalDesiredEnabled",
        "globalControlVerified","projectCount","projects",
    }
    row=result["projects"][0]
    assert "lastVerifiedVaultWrite" in row and row["lastVerifiedVaultWrite"] is None
    assert "body" not in row and "credentials" not in row
    assert len(row["projectId"]) == 24



def test_display_name_replaces_bidi_and_unicode_line_separators():
    # Distinct raw paths remain stable IDs; only presentation is sanitized.
    path = "10-Project/T/Project\u202eHidden\u2028Name.md"
    row = item(projects=(config(project_path=path),), histories=())
    assert row["name"] == "Project?Hidden?Name"
    assert "\u202e" not in row["name"] and "\u2028" not in row["name"]
    assert row["lastVerifiedVaultWrite"] is None


def test_lone_surrogate_in_project_path_rejected_without_unicode_error():
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(projects=(config(project_path="10-Project/T/Bad\ud800.md"),), histories=())


def test_invalid_orphan_observation_clock_is_always_rejected():
    extra = observed(
        project_path="10-Project/Orphan/Orphan.md",
        observed_at="not-a-timestamp",
    )
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(histories=(observed(), extra))


def test_invalid_clock_in_disabled_project_history_is_rejected():
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(projects=(config(github_watch=False),),
             histories=(observed(observed_at=None),))


def test_exactly_256_projects_and_observations_are_allowed():
    projects = tuple(
        config(project_path=f"10-Project/L/S{i}.md")
        for i in range(256)
    )
    observations = tuple(
        observed(project_path=f"10-Project/L/S{i}.md")
        for i in range(256)
    )
    result = show(projects=projects, histories=observations)
    assert result["projectCount"] == 256
    assert len(result["projects"]) == 256


def test_257_observations_rejected_even_with_one_project():
    observations = tuple(
        observed(project_path=f"10-Project/L/S{i}.md")
        for i in range(257)
    )
    with pytest.raises(ProjectWatchProjectionUnavailable):
        show(histories=observations)


def test_projection_timestamp_never_claims_evidence_collection_time():
    moment = NOW - timedelta(hours=1)
    result = show(histories=(observed(observed_at=moment),))
    assert result["projectedAt"] == NOW.isoformat()
    assert result["projects"][0]["lastWatcherObservedAt"] == moment.isoformat()
    assert "observedAt" not in result
