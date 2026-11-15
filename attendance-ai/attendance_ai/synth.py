"""정답이 있는 가상 출입기록 생성기 (시연·성능 검증용).

실제 현장에서 생기는 문제를 일부러 섞어 넣는다.
  - 메뉴 잘못 누름 (사람마다 빈도가 다름, '항상 출근만 누르는' 사람 포함)
  - 인식 실패로 연속 재입력, 잘못 누른 걸 알아채고 곧바로 바른 메뉴로 재입력
  - 퇴근(또는 출근) 지문을 아예 안 찍음
  - 자정을 넘는 야간근무, 3교대, 2교대(12시간), 점심 외출, 유연근무
생성되는 이름/사번은 모두 가상이다.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .models import IN, KOR, OUT, Punch

_SURNAMES = "김이박최정강조윤장임한오서신권황안송류홍"
_GIVEN = "민서지현수준영도윤하은우진호연유재성태경아빈채원나혜승주희"
_DEPTS = ["생산1팀", "생산2팀", "물류팀", "품질팀", "경영지원팀", "영업팀", "연구소", "보안팀"]


@dataclass
class Action:
    emp_id: str
    state: str  # 실제 출입 종류
    times: list[datetime] = field(default_factory=list)  # 실제로 찍힌 시각들 (재입력 포함)
    labels: list[Optional[str]] = field(default_factory=list)
    forgotten: bool = False  # 지문을 안 찍음
    work_date: Optional[date] = None


@dataclass
class Employee:
    emp_id: str
    name: str
    dept: str
    kind: str
    error_rate: float
    habit: str = "normal"  # normal / always_in / no_menu


@dataclass
class Truth:
    employees: list[Employee]
    state_by_row: dict[int, str]  # 원본 행 번호 → 실제 출입 종류
    day_minutes: dict[tuple[str, date], float]  # (사번, 근무일) → 실제 근무시간(분)
    day_missing: set  # 지문 누락이 있었던 (사번, 근무일)
    actions: list[Action]


def _name(rng: random.Random) -> str:
    return rng.choice(_SURNAMES) + rng.choice(_GIVEN) + rng.choice(_GIVEN)


def _clock(d: date, hour: float, rng: random.Random, sd_min: float) -> datetime:
    base = datetime.combine(d, datetime.min.time()) + timedelta(hours=hour)
    return base + timedelta(minutes=rng.gauss(0, sd_min), seconds=rng.randint(0, 59))


def _shifts_for(emp: Employee, start: date, days: int, rng: random.Random, pers: dict):
    """(근무일, [(출근시각, 퇴근시각), ...]) 목록."""
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        wd = d.weekday()
        if emp.kind in ("office", "flex"):
            if wd >= 5 or rng.random() < 0.03:  # 주말, 가끔 연차
                continue
            sd = 6 if emp.kind == "office" else 25
            t_in = _clock(d, pers["start"], rng, sd)
            extra = max(0.0, rng.gauss(20, 40)) if rng.random() < 0.6 else 0.0
            t_out = t_in + timedelta(hours=9, minutes=extra + rng.uniform(-5, 10))
            if pers.get("lunch") and rng.random() < 0.6:
                lo = _clock(d, 12.05, rng, 4)
                li = lo + timedelta(minutes=rng.uniform(45, 65))
                out.append((d, [(t_in, lo), (li, t_out)]))
            else:
                out.append((d, [(t_in, t_out)]))
        elif emp.kind == "night":
            if wd in (4, 5) or rng.random() < 0.03:  # 금·토 밤 휴무
                continue
            t_in = _clock(d, 21.75, rng, 8)
            t_out = _clock(d + timedelta(days=1), 7.1, rng, 10)
            out.append((d, [(t_in, t_out)]))
        elif emp.kind == "rotating3":
            if wd >= 5:
                continue
            slot = ((i // 7) + pers["phase"]) % 3
            s_h = (6, 14, 22)[slot]
            t_in = _clock(d, s_h - 0.3, rng, 7)
            t_out = t_in + timedelta(hours=8, minutes=rng.uniform(15, 40))
            out.append((d, [(t_in, t_out)]))
        elif emp.kind == "twelve":
            cyc = (i + pers["phase"]) % 4  # 주간2일-휴무2일 / 야간 교대 반복
            if cyc >= 2:
                continue
            night = ((i + pers["phase"]) // 8) % 2 == 1
            s_h = 19.75 if night else 7.75
            t_in = _clock(d, s_h, rng, 7)
            t_out = t_in + timedelta(hours=12, minutes=rng.uniform(10, 30))
            out.append((d, [(t_in, t_out)]))
    return out


def generate(n_employees: int = 40, days: int = 28, start: date = date(2026, 10, 5), seed: int = 7,
             base_error: float = 0.05) -> tuple[list[Punch], Truth]:
    rng = random.Random(seed)
    kinds = (["office"] * 16 + ["flex"] * 8 + ["night"] * 5 + ["rotating3"] * 6 + ["twelve"] * 5)
    employees: list[Employee] = []
    used_names: set = set()
    for i in range(n_employees):
        kind = kinds[(i * 7) % len(kinds)]  # 인원이 적어도 근무 형태가 고르게 섞이도록
        name = _name(rng)
        while name in used_names:
            name = _name(rng)
        used_names.add(name)
        r = rng.random()
        err = base_error * rng.uniform(0.4, 1.6)
        if r < 0.12:
            err = rng.uniform(0.15, 0.3)  # 자주 잘못 누르는 사람
        employees.append(Employee(emp_id=f"E{1001 + i}", name=name, dept=rng.choice(_DEPTS), kind=kind,
                                  error_rate=err))
    if n_employees >= 6:
        employees[3].habit = "always_in"  # 메뉴를 바꾸지 않고 늘 '출근'으로 찍는 사람
        employees[17 % n_employees].habit = "no_menu"  # 메뉴 선택이 없는 단말기 사용

    actions: list[Action] = []
    day_minutes: dict = {}
    day_missing: set = set()
    for emp in employees:
        pers = {"start": rng.uniform(8.4, 9.1) if emp.kind == "office" else rng.uniform(7.0, 10.5),
                "lunch": rng.random() < 0.25, "phase": rng.randrange(0, 8)}
        for d, intervals in _shifts_for(emp, start, days, rng, pers):
            day_minutes[(emp.emp_id, d)] = sum((b - a).total_seconds() / 60 for a, b in intervals)
            for a, b in intervals:
                for st, t in ((IN, a), (OUT, b)):
                    act = Action(emp_id=emp.emp_id, state=st, work_date=d)
                    if rng.random() < 0.012:
                        act.forgotten = True
                        day_missing.add((emp.emp_id, d))
                        actions.append(act)
                        continue
                    lab = _choose_label(emp, st, rng)
                    act.times.append(t.replace(microsecond=0))
                    act.labels.append(lab)
                    wrong = lab is not None and lab != st
                    if wrong and rng.random() < 0.35:  # 잘못 누른 걸 알아채고 바로 다시 찍음
                        act.times.append(act.times[-1] + timedelta(seconds=rng.randint(4, 50)))
                        act.labels.append(st)
                    elif rng.random() < 0.03:  # 인식 실패 등으로 같은 메뉴로 한 번 더
                        act.times.append(act.times[-1] + timedelta(seconds=rng.randint(3, 90)))
                        act.labels.append(lab)
                    elif rng.random() < 0.006:  # 한참 뒤 같은 메뉴로 다시 찍음 (확인차)
                        act.times.append(act.times[-1] + timedelta(minutes=rng.uniform(12, 40)))
                        act.labels.append(lab)
                    actions.append(act)
    # 실제 근무시간: 출근은 처음 찍은 시각, 퇴근은 마지막 찍은 시각 기준
    _recompute_truth_minutes(actions, day_minutes)

    flat = []
    emp_by_id = {e.emp_id: e for e in employees}
    for act in actions:
        for t, lab in zip(act.times, act.labels):
            flat.append((t, act, lab))
    flat.sort(key=lambda x: (x[0], x[1].emp_id))
    punches: list[Punch] = []
    state_by_row: dict[int, str] = {}
    for i, (t, act, lab) in enumerate(flat):
        e = emp_by_id[act.emp_id]
        row_no = i + 2  # 머리글 다음 줄부터
        punches.append(Punch(row_no=row_no, emp_id=e.emp_id, name=e.name, ts=t, label=lab,
                             raw_label="" if lab is None else KOR[lab], device=_device(e, rng)))
        state_by_row[row_no] = act.state
    return punches, Truth(employees=employees, state_by_row=state_by_row, day_minutes=day_minutes,
                          day_missing=day_missing, actions=actions)


def _recompute_truth_minutes(actions: list[Action], day_minutes: dict) -> None:
    by_day: dict = {}
    for a in actions:
        by_day.setdefault((a.emp_id, a.work_date), []).append(a)
    for key, acts in by_day.items():
        total, open_t = 0.0, None
        for a in acts:  # 생성 순서 = 시간 순서
            if a.forgotten:
                open_t = None if a.state == OUT else open_t
                continue
            if a.state == IN:
                open_t = min(a.times)
            elif open_t is not None:
                total += (max(a.times) - open_t).total_seconds() / 60
                open_t = None
        day_minutes[key] = total


def _choose_label(emp: Employee, st: str, rng: random.Random) -> Optional[str]:
    if emp.habit == "no_menu":
        return None
    if emp.habit == "always_in":
        return IN
    if rng.random() < emp.error_rate:
        return OUT if st == IN else IN
    return st


def _device(e: Employee, rng: random.Random) -> str:
    return {"office": "본관 1층", "flex": "본관 1층", "night": "공장 출입구",
            "rotating3": "공장 출입구", "twelve": "물류센터"}[e.kind]


def write_export_csv(punches: list[Punch], truth: Truth, path: Path) -> None:
    """출입관리 단말기 내보내기 파일과 비슷한 형식으로 저장."""
    dept = {e.emp_id: e.dept for e in truth.employees}
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["번호", "출입일시", "사번", "이름", "부서", "단말기", "구분"])
        for i, p in enumerate(sorted(punches, key=lambda p: p.row_no), start=1):
            w.writerow([i, p.ts.strftime("%Y-%m-%d %H:%M:%S"), p.emp_id, p.name, dept.get(p.emp_id, ""),
                        p.device, p.raw_label])
