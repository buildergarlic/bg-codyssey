"""AI 판정 결과로 근무 단위(세션)를 만들고 근로시간을 계산한다.

달력 날짜가 아니라 '실제 출근 → 퇴근' 흐름으로 묶기 때문에 자정을 넘는
야간근무도 하나의 근무로 계산된다. 근무일은 출근한 날짜로 한다.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from .config import Settings
from .models import IN, OUT, Decision, Punch, Session


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


def shift_date_from_out(out_ts: datetime) -> date:
    """출근 기록 없이 퇴근만 있을 때의 근무일 추정: 퇴근 10시간 전의 날짜.

    (주간 18시 퇴근 → 같은 날, 야간 07시 퇴근 → 전날 밤 근무로 귀속)
    """
    return (out_ts - timedelta(hours=10)).date()


def night_overlap_minutes(a: datetime, b: datetime, start_h: int, end_h: int) -> float:
    """[a, b) 구간 중 야간(start_h ~ 다음날 end_h)에 해당하는 분."""
    total = 0.0
    day = a.date() - timedelta(days=1)
    while datetime.combine(day, datetime.min.time()) <= b:
        ns = datetime.combine(day, datetime.min.time()) + timedelta(hours=start_h)
        ne = datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(hours=end_h)
        lo, hi = max(a, ns), min(b, ne)
        if hi > lo:
            total += _minutes(lo, hi)
        day += timedelta(days=1)
    return total


def build_sessions(decisions: list[Decision], settings: Settings,
                   data_start: Optional[datetime] = None, data_end: Optional[datetime] = None) -> list[Session]:
    if not decisions:
        return []
    emp, name = decisions[0].event.emp_id, decisions[0].event.name
    new_gap = settings.new_shift_gap_hours * 60
    repunch = settings.repunch_window_min

    sessions: list[Session] = []
    cur: Optional[Session] = None
    open_in: Optional[datetime] = None
    last: Optional[Decision] = None

    def start_session(d: Decision, missing_in: bool = False) -> Session:
        s = Session(emp_id=emp, name=name, work_date=d.ts.date(), missing_in=missing_in)
        s.start = None if missing_in else d.ts
        if missing_in:
            s.notes.append("출근 기록 없음")
        sessions.append(s)
        return s

    for d in decisions:
        t = d.ts
        if d.state == IN:
            if cur is None:
                cur = start_session(d)
                open_in = t
            elif last.state == OUT:
                gap = _minutes(last.ts, d.event.first_ts)
                if gap >= new_gap:
                    cur = start_session(d)
                else:
                    cur.notes.append(f"외출 {last.ts:%H:%M}~{t:%H:%M}")
                open_in = t
            else:  # 출근 → 출근
                gap = _minutes(last.event.last_ts, d.event.first_ts)
                if gap <= repunch:
                    cur.notes.append(f"출근 재입력 {t:%H:%M} (첫 출근 {open_in:%H:%M} 기준)")
                    if gap > settings.repunch_review_min:
                        cur.anomalies.append(f"출근이 {gap:.0f}분 간격으로 두 번({last.ts:%H:%M}, {t:%H:%M}) — "
                                             f"외출 메뉴 오선택인지 단순 재입력인지 확인 필요")
                else:
                    cur.missing_out = True
                    cur.notes.append("퇴근 기록 없음")
                    cur = start_session(d)
                    open_in = t
        else:
            if cur is None:
                cur = start_session(d, missing_in=True)
                cur.end = t
            elif last.state == IN:
                cur.intervals.append((open_in, t))
                cur.end = t
                open_in = None
            else:  # 퇴근 → 퇴근
                gap = _minutes(last.event.last_ts, d.event.first_ts)
                if gap <= repunch:
                    if gap > settings.repunch_review_min:
                        cur.anomalies.append(f"퇴근이 {gap:.0f}분 간격으로 두 번({last.ts:%H:%M}, {t:%H:%M}) — "
                                             f"복귀 메뉴 오선택인지 단순 재입력인지 확인 필요")
                    if cur.intervals and cur.intervals[-1][1] == cur.end:
                        cur.intervals[-1] = (cur.intervals[-1][0], t)
                    cur.notes.append(f"퇴근 재입력 {t:%H:%M} (마지막 퇴근 기준)")
                    cur.end = t
                else:
                    cur = start_session(d, missing_in=True)
                    cur.end = t
        cur.decisions.append(d)
        last = d
    if cur is not None and open_in is not None:
        cur.missing_out = True
        cur.notes.append("퇴근 기록 없음")

    # 조회 기간 경계: 첫 근무의 출근이 기간 이전이거나 마지막 근무의 퇴근이 기간 이후일 수 있음
    one_day = timedelta(hours=24)
    if sessions and data_start is not None:
        f = sessions[0]
        if f.missing_in and not f.missing_out and f.end - data_start <= one_day:
            f.boundary = True
            f.notes.append("조회 시작 이전에 출근했을 가능성 (이전 기간 파일과 합산 필요)")
    if sessions and data_end is not None:
        l = sessions[-1]
        if l.missing_out and not l.missing_in and data_end - l.start <= one_day:
            l.boundary = True
            l.notes.append("조회 종료 이후에 퇴근했을 가능성 (다음 기간 파일과 합산 필요)")

    for s in sessions:
        s.work_minutes = sum(_minutes(a, b) for a, b in s.intervals)
        s.night_minutes = sum(night_overlap_minutes(a, b, settings.night_start_hour, settings.night_end_hour)
                              for a, b in s.intervals)
        if s.start and s.end and s.complete:
            s.stay_minutes = _minutes(s.start, s.end)
            s.outing_minutes = max(0.0, s.stay_minutes - s.work_minutes)
        if s.missing_in and s.end is not None:
            s.work_date = shift_date_from_out(s.end)
    return sessions


def baseline_by_day(punches: list[Punch], max_shift_hours: float = 20.0,
                    duplicate_window_min: float = 5.0) -> dict[tuple[str, date], dict]:
    """'선택한 메뉴를 그대로 믿는' 기존 방식의 근무시간 계산 (비교 기준).

    출근 메뉴로 찍은 기록을 열고, 다음 퇴근 메뉴 기록으로 닫는다. 짝이 맞지 않으면
    그 날은 '확인 필요'가 되어 지금처럼 담당자가 직접 확인해야 하는 날이 된다.
    (공정한 비교를 위해 같은 메뉴를 몇 분 안에 다시 찍은 중복 입력은 기존 방식에서도 정상 처리)
    """
    by_emp: dict[str, list[Punch]] = defaultdict(list)
    for p in punches:
        by_emp[p.emp_id].append(p)
    out: dict[tuple[str, date], dict] = {}
    limit = max_shift_hours * 60

    def day(emp, d):
        return out.setdefault((emp, d), {"minutes": 0.0, "problem": False, "why": []})

    for emp, ps in by_emp.items():
        ps.sort(key=lambda p: (p.ts, p.row_no))
        open_in: Optional[datetime] = None
        last_pair: Optional[tuple[date, datetime]] = None  # (근무일, 퇴근시각)
        for p in ps:
            if p.label == IN:
                if open_in is not None and _minutes(open_in, p.ts) > limit:
                    r = day(emp, open_in.date())
                    r["problem"] = True
                    r["why"].append("퇴근 없음")
                    open_in = None
                if open_in is None:
                    open_in = p.ts
            elif p.label == OUT:
                if open_in is not None and _minutes(open_in, p.ts) <= limit:
                    day(emp, open_in.date())["minutes"] += _minutes(open_in, p.ts)
                    last_pair = (open_in.date(), p.ts)
                    open_in = None
                elif open_in is None and last_pair and _minutes(last_pair[1], p.ts) <= duplicate_window_min:
                    day(emp, last_pair[0])["minutes"] += _minutes(last_pair[1], p.ts)  # 퇴근 중복 입력
                    last_pair = (last_pair[0], p.ts)
                else:
                    if open_in is not None:
                        r = day(emp, open_in.date())
                        r["problem"] = True
                        r["why"].append("퇴근 없음")
                        open_in = None
                    r = day(emp, shift_date_from_out(p.ts))
                    r["problem"] = True
                    r["why"].append("출근 없이 퇴근")
        if open_in is not None:
            r = day(emp, open_in.date())
            r["problem"] = True
            r["why"].append("퇴근 없음")
    return out
