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

# A model run is not usable at its start time: ECMWF IFS open data is published ~7-8 h after
# initialisation. The "no future" rule is therefore init + RUN_AVAILABILITY_DELAY <= T.
RUN_AVAILABILITY_DELAY = timedelta(hours=8)


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


def lead_days(h: int, *, previous: bool = False) -> int:
    """Open-Meteo Previous Runs bucket N (`*_previous_dayN`) for horizon h.

    previous_dayN for target t comes from a run started at most t - 24*N hours, so the run is
    published by T when (h - 1) + 8 <= 24*N: h 1-17 -> 1, h 18-41 -> 2, h 42-48 -> 3.
    previous=True (version v1, the older run) takes one day more.
    """
    if not 1 <= h <= HORIZON:
        raise ValueError(f"h must be 1..{HORIZON}, got {h}")
    delay_h = int(RUN_AVAILABILITY_DELAY.total_seconds() // 3600)
    n = -(-(h - 1 + delay_h) // 24)
    return n + 1 if previous else n


def run_available_at(init_utc: datetime) -> datetime:
    return init_utc.astimezone(timezone.utc) + RUN_AVAILABILITY_DELAY


def published_before_issue(init_utc: datetime, issue: str | date) -> bool:
    """The contract §2 check: the run was already published at the issue moment T."""
    return run_available_at(init_utc) <= issue_time_utc(issue)


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
