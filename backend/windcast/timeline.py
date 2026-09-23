"""Time conventions from docs/CONTRACT.md §1 — the single source for every module.

Internally everything is UTC. Local time is a fixed UTC+5 (Asia/Almaty since 2024-03-01).
Issue D is made at T = (D+1) 00:00 local = D 19:00 UTC. Horizon h = 1..48, and target hour h
starts at T + (h - 1) hours, so h = 1 is (D+1) 00:00 local.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=5))
HORIZON = 48
TURBINES = ("1", "2", "plant")

TEST_FROM = date(2026, 1, 31)
TEST_TO = date(2026, 2, 28)
BACKTEST_FROM = date(2025, 12, 31)
BACKTEST_TO = date(2026, 1, 29)


def parse_issue_date(value: str | date) -> date:
    """Parse "YYYY-MM-DD"; raise ValueError with a message the API can return as 400."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(
            f"Неверная дата выпуска «{value}»: нужен формат ГГГГ-ММ-ДД"
        ) from None


def issue_time_utc(issue: str | date) -> datetime:
    d = parse_issue_date(issue)
    return datetime(d.year, d.month, d.day, 19, tzinfo=timezone.utc)


def issue_time_local(issue: str | date) -> datetime:
    return issue_time_utc(issue).astimezone(LOCAL_TZ)


def target_times_utc(issue: str | date) -> list[datetime]:
    start = issue_time_utc(issue)
    return [start + timedelta(hours=h) for h in range(HORIZON)]


def live_times(now: datetime | None = None) -> tuple[datetime, list[datetime]]:
    """Live issue: T = now; targets start at the next full hour (UTC)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    first = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now, [first + timedelta(hours=h) for h in range(HORIZON)]


def issue_dates(start: date = TEST_FROM, end: date = TEST_TO) -> list[date]:
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def in_test_range(issue: str | date) -> bool:
    return TEST_FROM <= parse_issue_date(issue) <= TEST_TO


def iso_local(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M+05:00")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
