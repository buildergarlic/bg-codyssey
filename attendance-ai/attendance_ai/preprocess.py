"""전처리: 직원별 정렬 → 짧은 시간 안의 중복 입력을 한 번의 행동으로 묶기."""

from __future__ import annotations

from collections import defaultdict

from .config import Settings
from .models import Event, Punch


def group_events(punches: list[Punch], settings: Settings) -> dict[str, list[Event]]:
    by_emp: dict[str, list[Punch]] = defaultdict(list)
    for p in punches:
        by_emp[p.emp_id].append(p)

    window = settings.duplicate_window_min * 60
    result: dict[str, list[Event]] = {}
    for emp, ps in by_emp.items():
        ps.sort(key=lambda p: (p.ts, p.row_no))
        name = next((p.name for p in ps if p.name), "")
        events: list[Event] = []
        for p in ps:
            # 직전 묶음의 '마지막' 입력 기준으로 이어 붙인다 (연속 재입력 대응)
            if events and (p.ts - events[-1].last_ts).total_seconds() <= window:
                events[-1].punches.append(p)
            else:
                events.append(Event(emp_id=emp, name=name, punches=[p]))
        result[emp] = events
    return dict(sorted(result.items()))
