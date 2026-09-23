from datetime import date

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
