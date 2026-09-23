from datetime import date, datetime, timedelta, timezone

import pytest

from windcast import timeline


def test_issue_moment_and_horizon():
    assert (
        timeline.iso_utc(timeline.issue_time_utc("2026-02-13")) == "2026-02-13T19:00Z"
    )
    assert (
        timeline.iso_local(timeline.issue_time_local("2026-02-13"))
        == "2026-02-14T00:00+05:00"
    )
    targets = timeline.target_times_utc("2026-02-13")
    assert len(targets) == 48
    assert timeline.iso_local(targets[0]) == "2026-02-14T00:00+05:00"
    assert timeline.iso_local(targets[-1]) == "2026-02-15T23:00+05:00"


def test_test_range_has_29_issues():
    days = timeline.issue_dates()
    assert len(days) == 29
    assert days[0] == date(2026, 1, 31) and days[-1] == date(2026, 2, 28)


def test_bad_date_message():
    with pytest.raises(ValueError, match="ГГГГ-ММ-ДД"):
        timeline.parse_issue_date("13.02.2026")


def test_lead_days_keep_runs_published_before_issue():
    assert [timeline.lead_days(h) for h in (1, 17, 18, 41, 42, 48)] == [
        1,
        1,
        2,
        2,
        3,
        3,
    ]
    assert timeline.lead_days(1, previous=True) == 2
    issue = "2026-02-13"
    for h, target in enumerate(timeline.target_times_utc(issue), start=1):
        latest_possible_init = target - timedelta(days=timeline.lead_days(h))
        assert timeline.published_before_issue(latest_possible_init, issue)


def test_run_started_before_issue_but_published_after_is_future():
    issue = "2026-02-13"
    run_18z = datetime(2026, 2, 13, 18, tzinfo=timezone.utc)
    assert not timeline.published_before_issue(run_18z, issue)
    run_06z = datetime(2026, 2, 13, 6, tzinfo=timezone.utc)
    assert timeline.published_before_issue(run_06z, issue)
