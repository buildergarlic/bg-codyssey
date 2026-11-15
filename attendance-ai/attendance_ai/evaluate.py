"""정답이 있는 가상 데이터로 AI 판정 성능을 측정한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Settings
from .engine import AnalysisResult, analyze
from .synth import Truth, generate


@dataclass
class Metrics:
    events: int
    menu_correct: int  # 메뉴 선택이 실제와 맞았던 건수
    ai_correct: int  # AI 판정이 실제와 맞은 건수
    menu_wrong: int  # 메뉴가 틀렸던 건수
    fixed_auto: int  # 메뉴가 틀렸고 AI가 자동으로 바로잡은 건수
    fixed_review: int  # 메뉴가 틀렸고 검토 목록으로 보낸 건수
    missed: int  # 메뉴가 틀렸는데 AI도 놓친 건수 (조용한 오류)
    false_fix: int  # 메뉴가 맞았는데 AI가 '자동으로' 틀리게 바꾼 건수 (가장 위험)
    days: int
    days_ai_exact: int  # 자동 처리된 날 중 실제 근무시간과 1분 이내로 일치
    days_ai_auto: int  # 사람 검토 없이 자동 처리된 날
    days_ai_silent_wrong: int  # 자동 처리됐지만 5분 넘게 틀린 날 (조용한 오류)
    days_baseline_ok: int  # 기존 방식(메뉴 신뢰)으로 계산된 날
    days_baseline_exact: int
    days_baseline_silent_wrong: int
    days_missing: int  # 지문 누락이 실제로 있었던 날
    days_missing_flagged: int  # 그중 AI가 검토 목록으로 보낸 날
    days_missing_whole: int  # 그중 그날 지문을 하나도 안 찍은 날 (기록만으로는 휴무와 구분 불가)
    review_items: int

    def as_rows(self) -> list[tuple[str, str]]:
        pct = lambda a, b: f"{a / b * 100:.2f}%" if b else "-"
        return [
            ("출입 행동(중복 묶은 후)", f"{self.events:,}건"),
            ("메뉴 선택 정확도 (기존 방식)", pct(self.menu_correct, self.events)),
            ("AI 판정 정확도", pct(self.ai_correct, self.events)),
            ("메뉴가 틀린 건", f"{self.menu_wrong:,}건"),
            ("  └ AI가 자동 정정", f"{self.fixed_auto:,}건 ({pct(self.fixed_auto, self.menu_wrong)})"),
            ("  └ 검토 목록으로 보냄", f"{self.fixed_review:,}건 ({pct(self.fixed_review, self.menu_wrong)})"),
            ("  └ 놓침", f"{self.missed:,}건 ({pct(self.missed, self.menu_wrong)})"),
            ("맞는 메뉴를 AI가 자동으로 틀리게 바꿈", f"{self.false_fix:,}건"),
            ("근무일 수", f"{self.days:,}일"),
            ("기존 방식: 계산 가능한 날", f"{self.days_baseline_ok:,}일 ({pct(self.days_baseline_ok, self.days)})"),
            ("기존 방식: 근무시간 정확(±1분)", f"{self.days_baseline_exact:,}일 ({pct(self.days_baseline_exact, self.days)})"),
            ("기존 방식: 계산은 됐지만 틀린 날", f"{self.days_baseline_silent_wrong:,}일"),
            ("AI: 사람 검토 없이 자동 처리", f"{self.days_ai_auto:,}일 ({pct(self.days_ai_auto, self.days)})"),
            ("AI: 근무시간 정확(±1분)", f"{self.days_ai_exact:,}일 ({pct(self.days_ai_exact, self.days)})"),
            ("AI: 자동 처리됐지만 틀린 날", f"{self.days_ai_silent_wrong:,}일"),
            ("지문 누락이 있었던 날", f"{self.days_missing:,}일 → AI가 검토 요청 {self.days_missing_flagged:,}일"),
            ("  └ 그날 지문을 하나도 안 찍음(휴무와 구분 불가)", f"{self.days_missing_whole:,}일"),
            ("검토 목록 항목 수", f"{self.review_items:,}건"),
        ]


def evaluate(result: AnalysisResult, truth: Truth) -> Metrics:
    ev_total = menu_ok = ai_ok = wrong = fa = fr = missed = false_fix = 0
    for ds in result.decisions.values():
        for d in ds:
            real = truth.state_by_row[d.event.punches[0].row_no]
            lab = d.event.label
            ev_total += 1
            menu_ok += lab == real
            ai_ok += d.state == real
            if lab is not None and lab != real:
                wrong += 1
                if d.needs_review:
                    fr += 1
                elif d.state == real:
                    fa += 1
                else:
                    missed += 1
            elif lab == real and d.state != real and not d.needs_review:
                false_fix += 1

    days = exact = auto = silent = b_ok = b_exact = b_silent = 0
    flagged_missing = 0
    rows = {(r.emp_id, r.work_date): r for r in result.days}
    # 검토로 보낸 근무의 시간 범위 (누락 근무는 앞뒤 근무와 합쳐져 다른 날짜로 잡힐 수 있음)
    flagged_spans: dict[str, list] = {}
    for emp, ss in result.sessions.items():
        for x in ss:
            if x.needs_review or x.boundary:
                a = x.start or x.end
                b = x.end or x.start
                flagged_spans.setdefault(emp, []).append((a, b, x.work_date))
    for key, minutes in truth.day_minutes.items():
        days += 1
        r = rows.get(key)
        if key in truth.day_missing:
            lo = datetime.combine(key[1], datetime.min.time())
            hi = lo + timedelta(days=1, hours=12)
            flagged_missing += any((a <= hi and b >= lo) or abs((wd - key[1]).days) <= 1
                                   for a, b, wd in flagged_spans.get(key[0], []))
            continue
        if r is None:
            continue
        if r.status in ("정상", "자동정정"):
            auto += 1
            if abs(r.work_minutes - minutes) <= 1:
                exact += 1
            elif abs(r.work_minutes - minutes) > 5:
                silent += 1
        if r.baseline_minutes is not None:
            b_ok += 1
            if abs(r.baseline_minutes - minutes) <= 1:
                b_exact += 1
            elif abs(r.baseline_minutes - minutes) > 5:
                b_silent += 1
    return Metrics(events=ev_total, menu_correct=menu_ok, ai_correct=ai_ok, menu_wrong=wrong, fixed_auto=fa,
                   fixed_review=fr, missed=missed, false_fix=false_fix, days=days, days_ai_exact=exact,
                   days_ai_auto=auto, days_ai_silent_wrong=silent, days_baseline_ok=b_ok,
                   days_baseline_exact=b_exact, days_baseline_silent_wrong=b_silent,
                   days_missing=len(truth.day_missing), days_missing_flagged=flagged_missing,
                   days_missing_whole=sum(1 for k in truth.day_missing
                                          if all(a.forgotten for a in truth.actions
                                                 if (a.emp_id, a.work_date) == k)),
                   review_items=len(result.review))


def run_benchmark(n_employees: int = 40, days: int = 28, seed: int = 7, settings: Settings | None = None,
                  base_error: float = 0.05):
    punches, truth = generate(n_employees=n_employees, days=days, seed=seed, base_error=base_error)
    result = analyze(punches, settings)
    return result, truth, evaluate(result, truth)


def run_sweep(errors=(0.05, 0.10, 0.20), seeds=range(100, 110), n_employees: int = 60, days: int = 30,
              settings: Settings | None = None) -> list[dict]:
    """여러 오선택 비율 × 여러 무작위 시드로 반복 측정해 합산한다."""
    out = []
    for err in errors:
        a = dict(error=err, runs=0, events=0, menu=0, ai=0, wrong=0, fixed_auto=0, fixed_review=0, missed=0,
                 false_fix=0, days=0, b_ok=0, b_exact=0, b_silent=0, ai_auto=0, ai_exact=0, silent=0,
                 missing=0, missing_flagged=0, missing_whole=0, review_items=0)
        for seed in seeds:
            _, _, m = run_benchmark(n_employees=n_employees, days=days, seed=seed, settings=settings, base_error=err)
            a["runs"] += 1
            for k, v in (("events", m.events), ("menu", m.menu_correct), ("ai", m.ai_correct),
                         ("wrong", m.menu_wrong), ("fixed_auto", m.fixed_auto), ("fixed_review", m.fixed_review),
                         ("missed", m.missed), ("false_fix", m.false_fix), ("days", m.days),
                         ("b_ok", m.days_baseline_ok), ("b_exact", m.days_baseline_exact),
                         ("b_silent", m.days_baseline_silent_wrong), ("ai_auto", m.days_ai_auto),
                         ("ai_exact", m.days_ai_exact), ("silent", m.days_ai_silent_wrong),
                         ("missing", m.days_missing), ("missing_flagged", m.days_missing_flagged),
                         ("missing_whole", m.days_missing_whole), ("review_items", m.review_items)):
                a[k] += v
        out.append(a)
    return out


def sweep_table(rows: list[dict]) -> str:
    pct = lambda a, b: f"{a / b * 100:.2f}%" if b else "-"
    lines = ["| 평균 메뉴 오선택률 | 메뉴 정확도(기존) | AI 판정 정확도 | 틀린 메뉴 → 자동정정/검토/놓침 | 맞는 기록을 자동으로 틀리게 바꿈 "
             "| 기존 방식: 계산 가능한 날 | AI: 자동 처리된 날 | AI 자동 처리 중 틀린 날 | 지문 누락일 → 검토 요청 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for a in rows:
        lines.append(
            f"| {a['error'] * 100:.0f}% | {pct(a['menu'], a['events'])} | {pct(a['ai'], a['events'])} "
            f"| {a['wrong']:,} → {a['fixed_auto']:,} / {a['fixed_review']:,} / {a['missed']:,} "
            f"| {a['false_fix']:,}건 / {a['events']:,}건 | {pct(a['b_ok'], a['days'])} | {pct(a['ai_auto'], a['days'])} "
            f"| {a['silent']:,}일 / {a['days']:,}일 "
            f"| {a['missing_flagged']:,} / {a['missing']:,}일 (못 잡은 {a['missing'] - a['missing_flagged']}일 중 "
            f"{a['missing_whole']}일은 그날 지문 0건) |")
    return "\n".join(lines)
