"""표시용 서식 도우미."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

WEEKDAYS = "월화수목금토일"


def hm(minutes: Optional[float]) -> str:
    """분 → 'H:MM' (엑셀에서 다루기 쉬운 형식)."""
    if minutes is None:
        return ""
    m = int(round(minutes))
    sign = "-" if m < 0 else ""
    m = abs(m)
    return f"{sign}{m // 60}:{m % 60:02d}"


def dur_kor(minutes: float) -> str:
    m = int(round(abs(minutes)))
    d, rem = divmod(m, 1440)
    h, mi = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}일")
    if h:
        parts.append(f"{h}시간")
    if mi or not parts:
        parts.append(f"{mi}분")
    return " ".join(parts)


def dt_full(dt: Optional[datetime]) -> str:
    return "" if dt is None else dt.strftime("%Y-%m-%d %H:%M:%S")


def dt_short(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def day_kor(d: date) -> str:
    return f"{d.isoformat()}({WEEKDAYS[d.weekday()]})"
