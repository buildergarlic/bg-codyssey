"""분석 결과를 파일로 저장 (엑셀에서 바로 열 수 있는 CSV + HTML 보고서 + 모델 요약 JSON)."""

from __future__ import annotations

import json
from pathlib import Path

from .engine import AnalysisResult, evidence_text
from .fmt import day_kor, dt_full, hm
from .io_utils import write_csv
from .models import IN, KOR, OUT

FILES = {
    "corrected_punches.csv": "지문 기록별 AI 판정 (원본 1행 = 결과 1행, 감사 추적용)",
    "daily_attendance.csv": "직원별·근무일별 출퇴근 시각과 근로시간 (메뉴 기준 계산과 비교)",
    "review_queue.csv": "담당자 확인이 필요한 항목 (확정 열을 채워 다시 넣으면 반영)",
    "employee_summary.csv": "직원별 요약 (평소 출퇴근 시각, 메뉴 정확도, 정정·검토 건수)",
    "report.html": "한눈에 보는 분석 보고서",
    "model_summary.json": "분석 설정과 학습된 모델 요약",
}


def punch_rows(result: AnalysisResult) -> list[list]:
    rows = []
    for emp, ds in result.decisions.items():
        for d in ds:
            used = d.event.first_ts if d.state == IN else d.event.last_ts
            for p in d.event.punches:
                merged = len(d.event.punches) > 1
                role = ("기준 시각" if p.ts == used else "중복(1건으로 병합)") if merged else ""
                rows.append([
                    "수기" if p.manual else p.row_no, p.emp_id, p.name, dt_full(p.ts), p.device,
                    p.raw_label, KOR[p.label] if p.raw_label else "", KOR[d.state], f"{d.confidence:.3f}",
                    d.action, role, d.ts.date().isoformat(), " | ".join(d.reasons), evidence_text(d.evidence),
                ])
    for p in result.deleted:
        rows.append([p.row_no, p.emp_id, p.name, dt_full(p.ts), p.device, p.raw_label,
                     KOR[p.label] if p.raw_label else "", "", "", "삭제(담당자)", "", "", p.note, ""])
    rows.sort(key=lambda r: (r[1], r[3]))
    return rows


PUNCH_HEADER = ["원본행", "사번", "이름", "지문시각", "단말기", "원본메뉴값", "선택메뉴", "AI판정", "확신도",
                "처리", "중복묶음", "근무일", "판정근거", "증거별기여도(+는 판정 지지)"]


def write_outputs(result: AnalysisResult, outdir: str | Path) -> dict[str, Path]:
    from .report import render_report

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {name: out / name for name in FILES}

    write_csv(paths["corrected_punches.csv"], PUNCH_HEADER, punch_rows(result))

    write_csv(paths["daily_attendance.csv"],
              ["사번", "이름", "근무일", "출근", "퇴근", "실근무(H:MM)", "실근무(분)", "체류(H:MM)", "외출(H:MM)",
               "야간근무(H:MM)", "근무횟수", "상태", "AI정정건수", "메뉴기준근무(H:MM)", "메뉴기준상태", "차이(AI-메뉴기준)",
               "비고"],
              ([r.emp_id, r.name, day_kor(r.work_date), dt_full(r.first_in), dt_full(r.last_out),
                hm(r.work_minutes), round(r.work_minutes), hm(r.stay_minutes), hm(r.outing_minutes),
                hm(r.night_minutes), r.session_count, r.status, r.corrected,
                hm(r.baseline_minutes) if r.baseline_minutes is not None else "계산불가",
                ("확인필요: " + r.baseline_why) if r.baseline_problem else "정상",
                hm(r.work_minutes - r.baseline_minutes) if r.baseline_minutes is not None else "",
                r.notes] for r in result.days))

    write_csv(paths["review_queue.csv"],
              ["번호", "유형", "사번", "이름", "근무일", "지문시각", "선택메뉴", "AI제안", "확신도", "설명",
               "확정", "추가시각", "메모"],
              ([i, it.kind, it.emp_id, it.name, it.work_date.isoformat() if it.work_date else "",
                dt_full(it.ts), it.selected, it.suggestion,
                f"{it.confidence:.3f}" if it.confidence is not None else "", it.detail, "", "", ""]
               for i, it in enumerate(result.review, start=1)))

    write_csv(paths["employee_summary.csv"],
              ["사번", "이름", "지문기록수", "출입행동수", "근무일수", "총실근무(H:MM)", "AI자동정정", "검토필요",
               "메뉴기준 확인필요일", "메뉴선택정확도(추정)", "평소출근", "평소퇴근"],
              ([e.emp_id, e.name, e.punches, e.events, e.days, hm(e.work_minutes), e.corrected, e.review,
                e.baseline_problem_days, f"{e.menu_accuracy * 100:.0f}%", " / ".join(e.typical_in),
                " / ".join(e.typical_out)] for e in result.employees))

    paths["report.html"].write_text(render_report(result), encoding="utf-8")

    summary = {
        "settings": result.settings.to_dict(),
        "stats": result.stats,
        "period": [dt_full(result.data_start), dt_full(result.data_end)],
        "load": None if result.load_report is None else {
            "source": result.load_report.source, "encoding": result.load_report.encoding,
            "header_row": result.load_report.header_row, "columns": result.load_report.columns,
            "rows": result.load_report.total_rows, "loaded": result.load_report.loaded,
            "skipped": [{"row": r, "reason": why} for r, why, _ in result.load_report.skipped],
            "unknown_labels": result.load_report.unknown_labels,
        },
        "confirmations": result.confirm_log,
        "model": {
            "same_type_transition_rate": result.model.delta,
            "global_menu_accuracy": {KOR[k]: v for k, v in result.model.global_params.acc.items()},
            "em_log_likelihood": result.model.history,
            "employees": {e.emp_id: {"menu_accuracy": {KOR[k]: round(v, 3) for k, v in
                                                       result.model.menu_accuracy(e.emp_id).items()},
                                     "typical_in": e.typical_in, "typical_out": e.typical_out}
                          for e in result.employees},
        },
    }
    paths["model_summary.json"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths
