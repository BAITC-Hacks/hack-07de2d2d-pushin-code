"""Configuration shared by the SCADA pipeline and backtests."""

from __future__ import annotations

import os

SCADA_UTC_OFFSET_H = 5
SCADA_UTC_OFFSET_CANDIDATES = (5, 6)


def scada_utc_offset_hours(value: int | str | None = None) -> int:
    """Return a permitted constant SCADA-local-to-UTC offset.

    T3 evaluates both candidates with the same data pipeline and can either pass
    the candidate directly or set ``SCADA_UTC_OFFSET_H`` for its process.
    """
    raw = (
        os.environ.get("SCADA_UTC_OFFSET_H", SCADA_UTC_OFFSET_H)
        if value is None
        else value
    )
    try:
        offset = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("SCADA_UTC_OFFSET_H должен быть 5 или 6") from error
    if offset not in SCADA_UTC_OFFSET_CANDIDATES:
        raise ValueError("SCADA_UTC_OFFSET_H должен быть 5 или 6")
    return offset
