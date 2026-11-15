"""분석 파이프라인: 원본 기록 → AI 판정 → 근무 단위 → 근로시간 → 검토 목록."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .config import Settings
from .fmt import dt_short, dur_kor
from .hmm import AttendanceHMM
from .io_utils import LoadReport, parse_label
from .models import IN, KOR, OUT, Decision, Event, Punch, Session
from .preprocess import group_events
from .sessions import baseline_by_day, build_sessions


@dataclass
class DayRow:
    emp_id: str
    name: str
    work_date: date
    first_in: Optional[datetime]
    last_out: Optional[datetime]
    work_minutes: float
    stay_minutes: float
    outing_minutes: float
    night_minutes: float
    session_count: int
    status: str
    corrected: int
    baseline_minutes: Optional[float]
    baseline_problem: bool
    baseline_why: str
    notes: str


@dataclass
class EmployeeSummary:
    emp_id: str
    name: str
    punches: int
    events: int
    corrected: int
    review: int
    days: int
    work_minutes: float
    baseline_problem_days: int
    menu_accuracy: float  # 학습된 메뉴 선택 정확도 (출근/퇴근 평균)
    typical_in: list[str]
    typical_out: list[str]


@dataclass
class ReviewItem:
    kind: str
    emp_id: str
    name: str
    work_date: Optional[date]
    ts: Optional[datetime]
    selected: str
    suggestion: str
    confidence: Optional[float]
    detail: str


@dataclass
class AnalysisResult:
    settings: Settings
    load_report: Optional[LoadReport]
    punches: list[Punch]
    deleted: list[Punch]
    confirm_log: list[str]
    decisions: dict[str, list[Decision]]
    sessions: dict[str, list[Session]]
    days: list[DayRow]
    employees: list[EmployeeSummary]
    review: list[ReviewItem]
    stats: dict
    model: AttendanceHMM
    data_start: Optional[datetime] = None
    data_end: Optional[datetime] = None
    extra: dict = field(default_factory=dict)


# ── 담당자 확정 반영 (사람 판단 > AI 판단) ─────────────────────────
@dataclass
class Confirmation:
    emp_id: str
    ts: Optional[datetime]  # 대상 지문 시각
    decision: str  # 출근 / 퇴근 / 삭제
    add_ts: Optional[datetime] = None  # 누락 기록을 추가할 시각
    memo: str = ""


DELETE_WORDS = ("삭제", "무시", "제외", "delete", "ignore")


def apply_confirmations(punches: list[Punch], confirmations: list[Confirmation]) -> tuple[list[Punch], list[Punch], list[str]]:
    log: list[str] = []
    deleted: list[Punch] = []
    names = {p.emp_id: p.name for p in punches if p.name}
    index: dict[str, list[Punch]] = defaultdict(list)
    for p in punches:
        index[p.emp_id].append(p)
    kept = list(punches)
    for c in confirmations:
        dec = (c.decision or "").strip()
        if not dec:
            continue
        is_delete = any(w in dec.lower() for w in DELETE_WORDS)
        state = None if is_delete else parse_label(dec)
        if c.add_ts is not None:
            if state is None:
                log.append(f"[무시] {c.emp_id} {c.add_ts}: 추가할 기록의 종류(출근/퇴근)를 알 수 없음 '{dec}'")
                continue
            kept.append(Punch(row_no=0, emp_id=c.emp_id, name=names.get(c.emp_id, ""), ts=c.add_ts, label=None,
                              raw_label="", device="(수기)", manual=True, forced=state,
                              note=c.memo or "담당자 수기 추가"))
            log.append(f"[추가] {c.emp_id} {c.add_ts:%Y-%m-%d %H:%M} {KOR[state]} ({c.memo})")
            continue
        if c.ts is None:
            continue
        cands = [p for p in index.get(c.emp_id, []) if abs((p.ts - c.ts).total_seconds()) <= 60]
        if not cands:
            log.append(f"[무시] {c.emp_id} {c.ts}: 해당 시각의 지문 기록을 찾을 수 없음")
            continue
        target = min(cands, key=lambda p: abs((p.ts - c.ts).total_seconds()))
        if is_delete:
            target.note = c.memo or "담당자 삭제"
            deleted.append(target)
            kept = [p for p in kept if p is not target]
            log.append(f"[삭제] {c.emp_id} {target.ts:%Y-%m-%d %H:%M} ({c.memo})")
        elif state is not None:
            target.forced = state
            target.note = c.memo or "담당자 확정"
            log.append(f"[확정] {c.emp_id} {target.ts:%Y-%m-%d %H:%M} → {KOR[state]} ({c.memo})")
        else:
            log.append(f"[무시] {c.emp_id} {c.ts}: 확정 값 '{dec}' 을(를) 이해할 수 없음 (출근/퇴근/삭제)")
    return kept, deleted, log


# ── 판정 설명 ──────────────────────────────────────────────────
def _decide_action(d: Decision, s: Settings) -> str:
    """처리 결정.

    - 메뉴와 AI 판단이 같으면(사람과 AI가 동의) 검토 기준(80%)만 넘으면 정상.
    - 메뉴가 없거나 메뉴와 다르면 AI 판단만이 근거이므로 자동 처리 기준(95%)을 넘어야 한다.
    """
    if d.event.forced is not None:
        return "담당자확정"
    lab = d.event.label
    if lab is not None and lab == d.state:
        return "정상" if d.confidence >= s.review_threshold else "검토필요"
    if d.confidence >= s.auto_correct_threshold:
        return "자동정정" if lab is not None else "AI판정"
    if lab is not None and d.confidence >= s.review_threshold:
        return "정정제안"
    return "검토필요"


def _explain(d: Decision, prev: Optional[Decision], nxt: Optional[Decision], model: AttendanceHMM) -> list[str]:
    ev, s = d.event, d.state
    alt = OUT if s == IN else IN
    emp = ev.emp_id
    e = d.evidence
    out: list[str] = []

    if ev.forced is not None:
        note = next((p.note for p in reversed(ev.punches) if p.forced is not None), "")
        out.append(f"담당자가 '{KOR[ev.forced]}'으로 확정함" + (f" — {note}" if note else ""))
        return out

    lab = ev.label
    if lab is None:
        out.append("메뉴 선택값 없음 → 시각과 앞뒤 순서로 판단")
    elif lab == s:
        out.append(f"선택한 메뉴 '{KOR[lab]}'과 AI 판단 일치")
    else:
        out.append(f"선택한 메뉴는 '{KOR[lab]}'이었지만 실제로는 '{KOR[s]}'으로 판단")

    if len(ev.punches) > 1:
        span = (ev.last_ts - ev.first_ts).total_seconds() / 60
        if ev.relabelled:
            seq = "→".join(KOR[p.label] for p in ev.punches)
            out.append(f"{span:.0f}분 안에 {len(ev.punches)}번 찍으며 메뉴를 바꿔 선택({seq}) → 1건으로 묶고 마지막 선택을 반영")
        else:
            out.append(f"{span:.0f}분 안에 {len(ev.punches)}번 중복 입력 → 1건으로 처리"
                       f" ({'처음' if s == IN else '마지막'} 시각 기준)")

    # 순서 근거
    seq_bits = []
    if prev is not None:
        gap = (ev.first_ts - prev.event.last_ts).total_seconds() / 60
        rel = "정상 순서" if prev.state != s else "같은 종류 연속"
        seq_bits.append(f"직전 기록 {dt_short(prev.ts)} {KOR[prev.state]} 후 {dur_kor(gap)} 경과({rel})")
    if nxt is not None:
        gap = (nxt.event.first_ts - ev.last_ts).total_seconds() / 60
        seq_bits.append(f"다음 기록 {dt_short(nxt.ts)} {KOR[nxt.state]}까지 {dur_kor(gap)}")
    if seq_bits:
        out.append(" / ".join(seq_bits))

    # 시각 근거
    typ = model.typical_times(emp, s)
    typ_alt = model.typical_times(emp, alt)
    t = ev.time_for(s)
    if e["time"] >= 0.7:
        if typ:
            out.append(f"찍은 시각 {t:%H:%M} — 이 직원의 평소 {KOR[s]} 시간대({', '.join(typ)} 전후)에 해당")
        else:
            out.append(f"찍은 시각 {t:%H:%M} — 전체 직원의 일반적인 {KOR[s]} 시간대에 해당")
    elif e["time"] <= -0.7:
        where = f"평소 {KOR[alt]} 시간대({', '.join(typ_alt)} 전후)" if typ_alt else f"일반적인 {KOR[alt]} 시간대"
        out.append(f"찍은 시각 {t:%H:%M}만 보면 {where}에 가깝지만, 앞뒤 순서를 근거로 {KOR[s]}으로 판단")

    acc = model.menu_accuracy(emp)
    if lab is not None and lab != s:
        a = acc[s]
        if a < 0.8:
            out.append(f"이 직원은 실제 {KOR[s]} 때 메뉴를 바르게 고르는 비율이 약 {a * 100:.0f}%로 낮음 → 메뉴보다 시각·순서를 더 신뢰")
    return out


def evidence_text(ev: dict) -> str:
    """증거별 기여도(로그 오즈) 요약. +는 판정을 지지, -는 반대."""
    return (f"메뉴 {ev['label']:+.1f} / 시각 {ev['time']:+.1f} / "
            f"앞 순서 {ev['prev']:+.1f} / 뒤 순서 {ev['next']:+.1f}")


def _apply_guards(sessions: list[Session], s: Settings) -> None:
    """안전장치 1: AI 정정 결과가 상식적인 근무 형태인지 규칙으로 한 번 더 검증한다.

    지문을 아예 안 찍은 경우(기록 누락), 확률 모델은 출근-퇴근 순서를 맞추려고
    멀쩡한 기록의 종류를 뒤집는 쪽으로 해석할 수 있다. 그 결과는 대개 '23시간 근무'
    나 '3시간 근무'처럼 비정상적인 근무시간으로 드러나므로, 그런 경우 자동 정정을
    보류하고 사람에게 넘긴다.
    """
    limit = s.max_interval_hours * 60
    for x in sessions:
        for a, b in x.intervals:
            m = (b - a).total_seconds() / 60
            if m > limit:
                x.anomalies.append(f"출근 {a:%m/%d %H:%M} ~ 퇴근 {b:%m/%d %H:%M} 한 구간이 {dur_kor(m)} "
                                   f"({s.max_interval_hours:g}시간 초과) — 중간 기록 누락 가능성")
    med = _median_work(sessions)
    for x in sessions:
        if med and x.corrected and x.complete and not x.anomalies:
            ratio = x.work_minutes / med
            if ratio < s.anomaly_ratio_low or ratio > s.anomaly_ratio_high:
                x.anomalies.append(f"정정 후 근무시간 {dur_kor(x.work_minutes)}이(가) 평소({dur_kor(med)})와 크게 다름"
                                   f" — 메뉴 오류가 아니라 기록 누락일 가능성")


def _hold_unsafe_corrections(sessions: list[Session]) -> None:
    """자동 정정은 '빠진 기록 없이 완결되고 이상 징후가 없는 근무' 안에서만 적용한다.

    바로 앞뒤(24시간 이내) 근무에 기록 누락이나 이상 징후가 있으면 출퇴근 짝이 통째로
    한 칸씩 밀렸을 수 있으므로, 그 근무의 정정도 함께 확인하도록 보류한다.
    """
    def troubled(x: Session) -> bool:
        return (not x.complete) or bool(x.anomalies)

    def gap_h(a: Session, b: Session) -> float:
        ta = a.end or a.start
        tb = b.start or b.end
        return abs((tb - ta).total_seconds()) / 3600 if ta and tb else 1e9

    for i, x in enumerate(sessions):
        if troubled(x):
            why = x.anomalies[-1] if x.anomalies else ("조회 기간 경계의 근무" if x.boundary else "기록이 빠진 근무")
        else:
            near = [y for j, y in ((i - 1, sessions[i - 1] if i > 0 else None),
                                   (i + 1, sessions[i + 1] if i + 1 < len(sessions) else None))
                    if y is not None and troubled(y) and not y.boundary
                    and gap_h(*((y, x) if j < i else (x, y))) <= 24]
            if not near:
                continue
            why = "바로 앞뒤 근무에 기록 누락/이상 징후가 있어 출퇴근 짝이 밀렸을 수 있음"
        for d in x.decisions:
            if d.action in ("자동정정", "AI판정"):
                d.action = "정정제안" if d.action == "자동정정" else "검토필요"
                d.reasons.append("자동 처리 보류: " + why)


def _median_work(sessions: list[Session]) -> Optional[float]:
    normal = sorted(x.work_minutes for x in sessions if x.complete and not x.anomalies and x.work_minutes > 0)
    return normal[len(normal) // 2] if len(normal) >= 5 else None


def _plausible(x: Session, s: Settings, med: Optional[float]) -> bool:
    if not x.complete:
        return False
    if any((b - a).total_seconds() / 60 > s.max_interval_hours * 60 for a, b in x.intervals):
        return False
    if med and not (s.anomaly_ratio_low <= x.work_minutes / med <= s.anomaly_ratio_high):
        return False
    return True


def _check_alternative(ds: list[Decision], sessions: list[Session], model: AttendanceHMM, s: Settings,
                       data_start, data_end) -> None:
    """안전장치 2: 기록이 빠졌거나 이상한 근무가 있으면 '다르게 해석하면 어떻게 되나'를 따져 본다.

    AI가 '지문 2개 누락'으로 본 곳이 사실은 '메뉴 3번 오선택'일 수도 있고, 그 반대일 수도 있다.
    특히 주야 12시간 맞교대처럼 출근·퇴근 시각과 근무·휴식 길이가 같으면 시각만으로는
    구분이 안 된다. 기록 누락을 지금보다 30배 드물게/흔하게 가정하고 다시 해석했을 때 판정이
    달라지면서도 그 결과가 정상적인 근무 형태라면 두 해석 모두 그럴듯하다는 뜻이므로
    사람이 확인하도록 넘긴다.
    """
    if not ds or not any((not x.complete and not x.boundary) or x.anomalies for x in sessions):
        return
    emp = ds[0].event.emp_id
    med = _median_work(sessions)
    cur_of = {id(d): x for x in sessions for d in x.decisions}
    for delta, story in ((model.delta / 30, "기록 누락이 아니라 메뉴 오선택이라면"),
                         (min(0.3, model.delta * 30), "메뉴 오선택이 아니라 기록 누락이라면")):
        alt = model.infer(emp, delta=delta)
        changed = [i for i, (d, st) in enumerate(zip(ds, alt.states)) if d.state != st and d.event.forced is None]
        if not changed:
            continue
        alt_ds = [Decision(event=d.event, state=st, confidence=0.0, evidence={}) for d, st in zip(ds, alt.states)]
        alt_sessions = build_sessions(alt_ds, s, data_start, data_end)
        alt_of = {id(d): x for x in alt_sessions for d in x.decisions}
        runs: list[list[int]] = []
        for i in changed:
            if runs and i - runs[-1][-1] <= 1:
                runs[-1].append(i)
            else:
                runs.append([i])
        for run in runs:
            alts = list({id(alt_of[id(alt_ds[i])]): alt_of[id(alt_ds[i])] for i in run}.values())
            if not all(_plausible(x, s, med) for x in alts):
                continue
            desc = ", ".join(f"{x.start:%m/%d %H:%M}~{x.end:%m/%d %H:%M}({dur_kor(x.work_minutes)})"
                             for x in sorted(alts, key=lambda x: x.start))
            for i in run:
                d = ds[i]
                if d.action != "검토필요":
                    d.action = "검토필요"
                    d.reasons.append(f"다른 해석 가능: {story} '{KOR[alt.states[i]]}' → 근무 {desc}")
            for x in {id(cur_of[id(ds[i])]): cur_of[id(ds[i])] for i in run if id(ds[i]) in cur_of}.values():
                msg = f"출퇴근 짝이 다르게 맞춰질 수 있음 — {story} 근무 {desc}"
                if msg not in x.anomalies:
                    x.anomalies.append(msg)


# ── 파이프라인 ─────────────────────────────────────────────────
def analyze(punches: list[Punch], settings: Optional[Settings] = None,
            load_report: Optional[LoadReport] = None,
            confirmations: Optional[list[Confirmation]] = None) -> AnalysisResult:
    s = settings or Settings()
    deleted: list[Punch] = []
    confirm_log: list[str] = []
    if confirmations:
        punches, deleted, confirm_log = apply_confirmations(punches, confirmations)

    data_start = min((p.ts for p in punches if not p.manual), default=None)
    data_end = max((p.ts for p in punches if not p.manual), default=None)

    events = group_events(punches, s)
    model = AttendanceHMM(s).fit(events)

    decisions: dict[str, list[Decision]] = {}
    sessions: dict[str, list[Session]] = {}
    for emp, evs in events.items():
        inf = model.infer(emp)
        ds = [Decision(event=ev, state=st, confidence=c, evidence=e)
              for ev, st, c, e in zip(evs, inf.states, inf.confidence, inf.evidence)]
        for i, d in enumerate(ds):
            if d.event.forced is not None:
                d.confidence = 1.0
            d.action = _decide_action(d, s)
            d.reasons = _explain(d, ds[i - 1] if i > 0 else None, ds[i + 1] if i + 1 < len(ds) else None, model)
        decisions[emp] = ds
        sessions[emp] = build_sessions(ds, s, data_start, data_end)
        _apply_guards(sessions[emp], s)
        _check_alternative(ds, sessions[emp], model, s, data_start, data_end)
        _hold_unsafe_corrections(sessions[emp])

    baseline = baseline_by_day([p for p in punches if not p.manual], duplicate_window_min=s.duplicate_window_min)
    days = _build_days(sessions, baseline, events)
    review = _build_review(decisions, sessions)
    employees = _build_employees(events, decisions, sessions, days, model)
    stats = _stats(punches, events, decisions, sessions, days, review, deleted)
    return AnalysisResult(settings=s, load_report=load_report, punches=punches, deleted=deleted,
                          confirm_log=confirm_log, decisions=decisions, sessions=sessions, days=days,
                          employees=employees, review=review, stats=stats, model=model,
                          data_start=data_start, data_end=data_end)


_STATUS_RANK = {"참고": -1, "정상": 0, "자동정정": 1, "기간경계": 2, "검토필요": 3, "검토필요(이상근무)": 4,
                "검토필요(기록누락)": 5}


def _build_days(sessions, baseline, events) -> list[DayRow]:
    rows: list[DayRow] = []
    names = {emp: (evs[0].name if evs else "") for emp, evs in events.items()}
    seen: set = set()
    for emp, ss in sessions.items():
        by_date: dict[date, list[Session]] = defaultdict(list)
        for x in ss:
            by_date[x.work_date].append(x)
        for d, group in sorted(by_date.items()):
            seen.add((emp, d))
            b = baseline.get((emp, d))
            starts = [x.start for x in group if x.start]
            ends = [x.end for x in group if x.end]
            status = max((x.status for x in group), key=lambda st: _STATUS_RANK.get(st, 0))
            notes = []
            for x in group:
                notes.extend(x.anomalies)
                notes.extend(x.notes)
            rows.append(DayRow(
                emp_id=emp, name=names.get(emp, ""), work_date=d,
                first_in=min(starts) if starts else None, last_out=max(ends) if ends else None,
                work_minutes=sum(x.work_minutes for x in group),
                stay_minutes=sum(x.stay_minutes for x in group),
                outing_minutes=sum(x.outing_minutes for x in group),
                night_minutes=sum(x.night_minutes for x in group),
                session_count=len(group), status=status,
                corrected=sum(1 for x in group for dd in x.decisions if dd.changed),
                baseline_minutes=None if b is None or b["problem"] else b["minutes"],
                baseline_problem=bool(b and b["problem"]) or b is None,
                baseline_why=", ".join(sorted(set(b["why"]))) if b and b["why"] else ("기록 없음" if b is None else ""),
                notes="; ".join(notes)))
    for (emp, d), b in baseline.items():
        if (emp, d) in seen or (b["minutes"] == 0 and b["problem"]):
            continue
        rows.append(DayRow(emp_id=emp, name=names.get(emp, ""), work_date=d, first_in=None, last_out=None,
                           work_minutes=0.0, stay_minutes=0.0, outing_minutes=0.0, night_minutes=0.0,
                           session_count=0, status="참고", corrected=0,
                           baseline_minutes=None if b["problem"] else b["minutes"], baseline_problem=b["problem"],
                           baseline_why=", ".join(sorted(set(b["why"]))),
                           notes="메뉴 기준으로만 근무가 잡힌 날 (AI 판정: 이 날 시작한 근무 없음)"))
    rows.sort(key=lambda r: (r.emp_id, r.work_date))
    return rows


def _build_review(decisions, sessions) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    for emp, ds in decisions.items():
        sess_of = {}
        for x in sessions[emp]:
            for d in x.decisions:
                sess_of[id(d)] = x
        for d in ds:
            if not d.needs_review:
                continue
            x = sess_of.get(id(d))
            kind = "정정 제안" if d.action == "정정제안" else "판정 불확실"
            items.append(ReviewItem(kind=kind, emp_id=emp, name=d.event.name, work_date=x.work_date if x else None,
                                    ts=d.ts, selected=KOR[d.event.label], suggestion=KOR[d.state],
                                    confidence=d.confidence, detail=" | ".join(d.reasons)))
        for x in sessions[emp]:
            if x.anomalies and x.complete:
                items.append(ReviewItem(kind="이상 징후", emp_id=emp, name=x.name, work_date=x.work_date,
                                        ts=x.start, selected="", suggestion="앞뒤 기록과 함께 확인",
                                        confidence=None, detail="; ".join(x.anomalies)))
            if x.complete or x.boundary:
                continue
            if x.missing_out:
                ref = x.decisions[-1] if x.decisions else None
                items.append(ReviewItem(kind="퇴근 기록 누락", emp_id=emp, name=x.name, work_date=x.work_date,
                                        ts=ref.ts if ref else x.start, selected=KOR[ref.event.label] if ref else "",
                                        suggestion="퇴근 시각 확인 후 '추가시각'에 입력", confidence=None,
                                        detail="; ".join(x.notes)))
            if x.missing_in:
                ref = x.decisions[0] if x.decisions else None
                items.append(ReviewItem(kind="출근 기록 누락", emp_id=emp, name=x.name, work_date=x.work_date,
                                        ts=ref.ts if ref else x.end, selected=KOR[ref.event.label] if ref else "",
                                        suggestion="출근 시각 확인 후 '추가시각'에 입력", confidence=None,
                                        detail="; ".join(x.notes)))
    items.sort(key=lambda r: (r.emp_id, r.ts or datetime.min))
    return items


def _build_employees(events, decisions, sessions, days, model) -> list[EmployeeSummary]:
    out = []
    for emp, evs in events.items():
        ds = decisions[emp]
        acc = model.menu_accuracy(emp)
        my_days = [r for r in days if r.emp_id == emp]
        out.append(EmployeeSummary(
            emp_id=emp, name=evs[0].name if evs else "",
            punches=sum(len(e.punches) for e in evs), events=len(evs),
            corrected=sum(1 for d in ds if d.changed and not d.needs_review),
            review=sum(1 for d in ds if d.needs_review) + sum(1 for x in sessions[emp] if not x.complete and not x.boundary),
            days=sum(1 for r in my_days if r.session_count),
            work_minutes=sum(r.work_minutes for r in my_days),
            baseline_problem_days=sum(1 for r in my_days if r.baseline_problem),
            menu_accuracy=(acc[IN] + acc[OUT]) / 2,
            typical_in=model.typical_times(emp, IN), typical_out=model.typical_times(emp, OUT)))
    return out


def _stats(punches, events, decisions, sessions, days, review, deleted) -> dict:
    all_d = [d for ds in decisions.values() for d in ds]
    all_s = [x for ss in sessions.values() for x in ss]
    by_action: dict[str, int] = defaultdict(int)
    for d in all_d:
        by_action[d.action] += 1
    ai_review_days = sum(1 for r in days if r.status.startswith("검토필요"))
    return {
        "employees": len(events),
        "punches": len(punches),
        "events": len(all_d),
        "merged_duplicates": sum(len(d.event.punches) - 1 for d in all_d),
        "deleted": len(deleted),
        "manual_added": sum(1 for p in punches if p.manual),
        "by_action": dict(by_action),
        "label_mismatch": sum(1 for d in all_d if d.changed),
        "sessions": len(all_s),
        "sessions_complete": sum(1 for x in all_s if x.complete),
        "sessions_boundary": sum(1 for x in all_s if x.boundary),
        "days": len(days),
        "days_corrected": sum(1 for r in days if r.corrected and not r.status.startswith("검토")),
        "ai_review_days": ai_review_days,
        "baseline_problem_days": sum(1 for r in days if r.baseline_problem),
        "review_items": len(review),
        "work_minutes": sum(r.work_minutes for r in days),
        "night_minutes": sum(r.night_minutes for r in days),
    }
