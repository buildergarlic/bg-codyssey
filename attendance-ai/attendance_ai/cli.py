"""명령줄 사용법.

  python -m attendance_ai analyze 출입기록.csv -o 결과폴더
  python -m attendance_ai analyze 출입기록.csv --confirm 검토완료.csv -o 결과폴더
  python -m attendance_ai demo -o demo_out
  python -m attendance_ai serve
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .engine import analyze
from .fmt import hm
from .io_utils import load_confirmations, load_punches
from .outputs import FILES, write_outputs


def _settings(a) -> Settings:
    s = Settings()
    for attr, key in (("auto", "auto_correct_threshold"), ("review", "review_threshold"),
                      ("dup_min", "duplicate_window_min"), ("new_shift_hours", "new_shift_gap_hours"),
                      ("max_interval_hours", "max_interval_hours"), ("repunch_review_min", "repunch_review_min")):
        v = getattr(a, attr, None)
        if v is not None:
            setattr(s, key, v)
    if s.review_threshold > s.auto_correct_threshold:
        raise SystemExit("--review 값은 --auto 값보다 클 수 없습니다.")
    return s


def _add_policy_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("판정 정책")
    g.add_argument("--auto", type=float, help="자동 정정 최소 확신도 (기본 0.95)")
    g.add_argument("--review", type=float, help="이 확신도 미만은 검토 (기본 0.80)")
    g.add_argument("--dup-min", type=float, help="이 시간(분) 안의 연속 입력은 1건으로 묶음 (기본 5)")
    g.add_argument("--new-shift-hours", type=float, help="퇴근 후 이 시간 이상 지나면 새 근무 (기본 4)")
    g.add_argument("--max-interval-hours", type=float, help="출근~퇴근 한 구간 최대 시간, 초과 시 검토 (기본 16)")
    g.add_argument("--repunch-review-min", type=float, help="같은 종류 재입력 간격이 이보다 길면 검토 (기본 15)")


def print_summary(result, paths=None, out=None) -> None:
    out = out or sys.stdout
    s = result.stats
    ba = s["by_action"]
    lr = result.load_report
    w = lambda *x: print(*x, file=out)
    w("")
    w("━━━━━━━━━━━━━━━━━━━━ AI 근태 판정 결과 ━━━━━━━━━━━━━━━━━━━━")
    if lr:
        w(f" 원본: {lr.source}  (인코딩 {lr.encoding}, 머리글 {lr.header_row}행)")
        w(f" 인식한 열: " + ", ".join(f"{k}={v}" for k, v in lr.columns.items()))
        w(f" 읽은 행: {lr.loaded:,} / {lr.total_rows:,}" + (f"  ⚠ 읽지 못한 행 {len(lr.skipped)}개" if lr.skipped else ""))
        for row, why, raw in lr.skipped[:5]:
            w(f"    - {row}행: {why} ({raw[:60]})")
        if lr.unknown_labels:
            w(" ⚠ 해석하지 못한 메뉴 값: " + ", ".join(f"'{k}' {v}건" for k, v in lr.unknown_labels.items())
              + "  → --in-values / --out-values 로 지정 가능")
    if result.data_start:
        w(f" 기간: {result.data_start:%Y-%m-%d %H:%M} ~ {result.data_end:%Y-%m-%d %H:%M}")
    for line in result.confirm_log:
        w(f" 담당자 확정 {line}")
    w(f" 직원 {s['employees']:,}명 · 지문 {s['punches']:,}건 (중복 입력 {s['merged_duplicates']:,}건 병합)")
    w("")
    w(f"   정상 (메뉴와 일치)     {ba.get('정상', 0):>6,}건")
    w(f"   AI 판정 (메뉴 없음)    {ba.get('AI판정', 0):>6,}건")
    w(f"   ✔ 자동 정정            {ba.get('자동정정', 0):>6,}건")
    w(f"   ? 정정 제안 (검토)     {ba.get('정정제안', 0):>6,}건")
    w(f"   ? 판정 불확실 (검토)   {ba.get('검토필요', 0):>6,}건")
    if ba.get("담당자확정"):
        w(f"   ★ 담당자 확정          {ba.get('담당자확정', 0):>6,}건")
    w("")
    w(f" 근무일 {s['days']:,}일 · 총 실근무 {hm(s['work_minutes'])} · 야간 {hm(s['night_minutes'])}")
    w(f" 확인이 필요한 근무일: 메뉴 기준 {s['baseline_problem_days']:,}일 → AI 판정 후 {s['ai_review_days']:,}일")
    w(f" 담당자 검토 목록: {s['review_items']:,}건")
    if paths:
        w("")
        w(" 결과 파일:")
        for name, p in paths.items():
            w(f"   {p}  — {FILES[name]}")
    w("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def cmd_analyze(a) -> int:
    overrides = {"emp_id": a.col_emp, "name": a.col_name, "datetime": a.col_datetime, "date": a.col_date,
                 "time": a.col_time, "label": a.col_label, "device": a.col_device}
    punches, rep = load_punches(a.input, overrides, a.in_values or (), a.out_values or ())
    if not punches:
        print("읽을 수 있는 지문 기록이 없습니다.", file=sys.stderr)
        return 1
    confirmations = []
    for c in a.confirm or []:
        confirmations.extend(load_confirmations(c))
    result = analyze(punches, _settings(a), rep, confirmations)
    out = Path(a.output or (Path(a.input).stem + "_AI판정"))
    paths = write_outputs(result, out)
    print_summary(result, paths)
    return 0


def cmd_demo(a) -> int:
    from .evaluate import evaluate
    from .synth import generate, write_export_csv

    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    punches, truth = generate(n_employees=a.employees, days=a.days, seed=a.seed, base_error=a.error)
    src = out / "sample_punches.csv"
    write_export_csv(punches, truth, src)
    print(f"가상 출입기록을 만들었습니다: {src}  (직원 {a.employees}명, {a.days}일, 이름·사번은 모두 가상)")
    loaded, rep = load_punches(src)
    result = analyze(loaded, _settings(a), rep)
    paths = write_outputs(result, out / "result")
    print_summary(result, paths)
    m = evaluate(result, truth)
    print("\n[정답과 비교한 성능]")
    for k, v in m.as_rows():
        print(f"  {k:<36} {v}")
    return 0


def cmd_benchmark(a) -> int:
    from .evaluate import run_sweep, sweep_table

    print(f"가상 데이터 성능 측정: 직원 {a.employees}명 × {a.days}일, 오선택률 {a.errors}, 시드 {a.seeds}개씩 …")
    rows = run_sweep(errors=a.errors, seeds=range(100, 100 + a.seeds), n_employees=a.employees, days=a.days,
                     settings=_settings(a))
    print(sweep_table(rows))
    return 0


def cmd_serve(a) -> int:
    from .server import serve

    serve(a.host, a.port, open_browser=not a.no_browser)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="attendance_ai", description="AI 근태 판정기 — 출근/퇴근 메뉴 오선택을 자동으로 바로잡아 정확한 근로시간을 계산합니다.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("analyze", help="출입기록 파일 분석")
    pa.add_argument("input", help="출입관리 단말기에서 내려받은 CSV 또는 XLSX")
    pa.add_argument("-o", "--output", help="결과 폴더 (기본: <파일명>_AI판정)")
    pa.add_argument("--confirm", action="append", help="담당자가 '확정' 열을 채운 검토 파일 (여러 개 가능)")
    g = pa.add_argument_group("열 이름 직접 지정 (자동 인식이 안 될 때)")
    for name, h in (("emp", "사번"), ("name", "이름"), ("datetime", "일시"), ("date", "날짜"), ("time", "시각"),
                    ("label", "출퇴근 구분"), ("device", "단말기")):
        g.add_argument(f"--col-{name}", help=f"{h} 열 이름")
    g.add_argument("--in-values", nargs="*", help="출근으로 볼 메뉴 값 (예: 1 F1)")
    g.add_argument("--out-values", nargs="*", help="퇴근으로 볼 메뉴 값 (예: 2 F2)")
    _add_policy_args(pa)
    pa.set_defaults(func=cmd_analyze)

    pd = sub.add_parser("demo", help="가상 데이터로 시연하고 정답과 비교")
    pd.add_argument("-o", "--output", default="demo_out")
    pd.add_argument("--employees", type=int, default=40)
    pd.add_argument("--days", type=int, default=28)
    pd.add_argument("--seed", type=int, default=7)
    pd.add_argument("--error", type=float, default=0.05, help="평균 메뉴 오선택 비율 (기본 0.05)")
    _add_policy_args(pd)
    pd.set_defaults(func=cmd_demo)

    pb = sub.add_parser("benchmark", help="정답이 있는 가상 데이터로 여러 번 반복 측정")
    pb.add_argument("--employees", type=int, default=60)
    pb.add_argument("--days", type=int, default=30)
    pb.add_argument("--seeds", type=int, default=10)
    pb.add_argument("--errors", type=float, nargs="+", default=[0.05, 0.10, 0.20])
    _add_policy_args(pb)
    pb.set_defaults(func=cmd_benchmark)

    ps = sub.add_parser("serve", help="내 PC에서만 동작하는 웹 화면 실행 (파일이 외부로 전송되지 않음)")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8765)
    ps.add_argument("--no-browser", action="store_true")
    ps.set_defaults(func=cmd_serve)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
