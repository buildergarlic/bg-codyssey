from __future__ import annotations

from datetime import date, datetime, timedelta

from attendance_ai.io_utils import parse_label
from attendance_ai.models import Punch


class Builder:
    """테스트용 지문 기록을 간단히 만드는 도우미."""

    def __init__(self):
        self.punches: list[Punch] = []

    def add(self, emp: str, ts: str | datetime, label: str | None, name: str = "") -> "Builder":
        if isinstance(ts, str):
            ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S" if ts.count(":") == 2 else "%Y-%m-%d %H:%M")
        self.punches.append(Punch(row_no=len(self.punches) + 2, emp_id=emp, name=name or f"직원{emp}", ts=ts,
                                  label=parse_label(label) if label else None, raw_label=label or ""))
        return self

    def office_days(self, emp: str, start: date, n: int, t_in=(9, 0), t_out=(18, 0), skip=(),
                    jitter: int = 7) -> "Builder":
        d, made = start, 0
        while made < n:
            if d.weekday() < 5:
                if d not in skip:
                    j = (made * 3) % (2 * jitter + 1) - jitter  # 결정적인 작은 흔들림
                    base = datetime.combine(d, datetime.min.time())
                    self.add(emp, base + timedelta(hours=t_in[0], minutes=t_in[1] + j), "출근")
                    self.add(emp, base + timedelta(hours=t_out[0], minutes=t_out[1] - j), "퇴근")
                made += 1
            d += timedelta(days=1)
        return self

    def night_days(self, emp: str, start: date, n: int) -> "Builder":
        for i in range(n):
            d = start + timedelta(days=i)
            j = (i * 5) % 11 - 5
            self.add(emp, datetime.combine(d, datetime.min.time()).replace(hour=22) + timedelta(minutes=j), "출근")
            self.add(emp, datetime.combine(d + timedelta(days=1), datetime.min.time()).replace(hour=7)
                     + timedelta(minutes=-j), "퇴근")
        return self

    def background(self) -> "Builder":
        """다른 직원들의 평범한 기록 (전체 패턴 학습용)."""
        for k in range(4):
            self.office_days(f"BG{k}", date(2026, 10, 5), 15, t_in=(8, 40 + k * 5), t_out=(17, 50 + k * 5))
        return self


def find(result, emp: str, ts: str):
    t = datetime.strptime(ts, "%Y-%m-%d %H:%M")
    for d in result.decisions[emp]:
        if any(abs((p.ts - t).total_seconds()) < 60 for p in d.event.punches):
            return d
    raise AssertionError(f"{emp} {ts} 판정을 찾을 수 없음")


def day(result, emp: str, d: str):
    for r in result.days:
        if r.emp_id == emp and r.work_date.isoformat() == d:
            return r
    raise AssertionError(f"{emp} {d} 근무일 없음")
