"""출입기록 파일 읽기/쓰기.

출입관리 단말기마다 내보내는 파일 형식이 다르므로, 열 이름(한글/영문)을 자동으로
인식하고 날짜 표기(2026-11-02, 2026.11.02, 20261102, 오전/오후 …)를 폭넓게
해석한다. 해석하지 못한 행은 조용히 버리지 않고 모두 보고한다.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .models import IN, OUT, Punch

# ── 열 이름 별칭 ──────────────────────────────────────────────
ALIASES: dict[str, list[str]] = {
    "emp_id": ["사번", "사원번호", "직원번호", "사용자id", "사용자번호", "개인번호", "등록번호",
               "카드번호", "id", "empid", "employeeid", "empno", "userid", "userno", "personid"],
    "name": ["이름", "성명", "사원명", "직원명", "사용자명", "name", "employeename", "username", "fullname"],
    "datetime": ["일시", "출입일시", "인증일시", "발생일시", "기록일시", "출퇴근일시", "근태일시", "일자시간",
                 "날짜시간", "datetime", "timestamp", "eventtime", "logtime", "punchtime", "checktime"],
    "date": ["날짜", "일자", "출입일자", "인증일자", "근무일자", "date", "day"],
    "time": ["시간", "시각", "출입시각", "인증시각", "출입시간", "인증시간", "time"],
    "label": ["구분", "출퇴근구분", "근태구분", "모드", "근태모드", "인증모드", "출입구분", "기능키", "상태",
              "이벤트", "type", "mode", "event", "status", "inout", "direction", "attendance", "punchtype",
              "checktype"],
    "device": ["단말기", "단말기명", "기기", "기기명", "장치", "장치명", "출입문", "게이트", "위치", "device",
               "terminal", "door", "reader", "location", "gate"],
}


def _norm(h: str) -> str:
    return re.sub(r"[\s_\-()\[\]./]", "", str(h or "")).lower()


# ── 값 해석 ──────────────────────────────────────────────────
_AMPM = r"(오전|오후|AM|PM|am|pm|A\.M\.|P\.M\.)"
_SEP_DT = re.compile(
    r"^\s*(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?\.?(?:\s*\([^)]*\))?[\sT]*"
    + r"(?:" + _AMPM + r"\s*)?(\d{1,2})\s*[:시]\s*(\d{1,2})\s*(?:[:분]\s*(\d{1,2})(?:\.\d+)?\s*초?)?\s*"
    + r"(?:" + _AMPM + r")?"
)
_COMPACT_DT = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})[\sT]?(\d{2})(\d{2})(\d{2})?\s*$")
_DATE = re.compile(r"^\s*(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
_COMPACT_DATE = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})\s*$")
_TIME = re.compile(
    r"^\s*(?:" + _AMPM + r"\s*)?(\d{1,2})\s*[:시]\s*(\d{1,2})\s*(?:[:분]\s*(\d{1,2})(?:\.\d+)?\s*초?)?\s*"
    + r"(?:" + _AMPM + r")?\s*$"
)
_COMPACT_TIME = re.compile(r"^\s*(\d{2})(\d{2})(\d{2})?\s*$")


def _apply_ampm(hour: int, *marks: Optional[str]) -> int:
    for m in marks:
        if not m:
            continue
        m = m.upper().replace(".", "")
        if m in ("오후", "PM") and hour < 12:
            return hour + 12
        if m in ("오전", "AM") and hour == 12:
            return 0
    return hour


def _excel_serial(v: float) -> datetime:
    return datetime(1899, 12, 30) + timedelta(days=float(v))


def parse_datetime(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None, microsecond=0)
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:
        return _excel_serial(v).replace(microsecond=0)
    s = str(v).strip()
    if not s:
        return None
    m = _SEP_DT.match(s)
    if m:
        y, mo, d, am1, hh, mi, ss, am2 = m.groups()
        hour = _apply_ampm(int(hh), am1, am2)
        return datetime(int(y), int(mo), int(d), hour, int(mi), int(ss or 0))
    m = _COMPACT_DT.match(s)
    if m:
        y, mo, d, hh, mi, ss = m.groups()
        return datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0))
    try:
        f = float(s)
        if 20000 < f < 80000:
            return _excel_serial(f).replace(microsecond=0)
    except ValueError:
        pass
    return None


def parse_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:
        return _excel_serial(v).date()
    s = str(v).strip()
    m = _DATE.match(s) or _COMPACT_DATE.match(s)
    if m:
        y, mo, d = m.groups()
        return date(int(y), int(mo), int(d))
    return None


def parse_time(v) -> Optional[time]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.time().replace(microsecond=0)
    if isinstance(v, time):
        return v.replace(microsecond=0)
    if isinstance(v, (int, float)) and 0 <= float(v) < 1:  # 엑셀 시간 일련값
        secs = round(float(v) * 86400)
        return time(secs // 3600 % 24, secs // 60 % 60, secs % 60)
    s = str(v).strip()
    m = _TIME.match(s)
    if m:
        am1, hh, mi, ss, am2 = m.groups()
        return time(_apply_ampm(int(hh), am1, am2), int(mi), int(ss or 0))
    m = _COMPACT_TIME.match(s)
    if m:
        hh, mi, ss = m.groups()
        if int(hh) < 24 and int(mi) < 60:
            return time(int(hh), int(mi), int(ss or 0))
    return None


_EN_OUT = re.compile(r"(?<![a-z])(out|exit|leave|check-?out|clock-?out|punch-?out|break-?out|ot-?out)(?![a-z])")
_EN_IN = re.compile(r"(?<![a-z])(in|enter|entry|arrive|check-?in|clock-?in|punch-?in|break-?in|ot-?in)(?![a-z])")


def parse_label(v, extra_in: Iterable[str] = (), extra_out: Iterable[str] = ()) -> Optional[str]:
    """메뉴 값 → IN / OUT / None.

    '외출'은 사업장 밖으로 나가는 것이므로 OUT, '복귀'는 IN 으로 본다.
    숫자 코드(1, 2, F1 …)는 단말기 제조사마다 뜻이 달라 기본으로는 해석하지 않고,
    --in-values / --out-values 옵션으로 지정할 수 있게 했다.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    low = s.lower().replace(" ", "")
    if low in {x.lower() for x in extra_in}:
        return IN
    if low in {x.lower() for x in extra_out}:
        return OUT
    if "복귀" in low:
        return IN
    for w in ("퇴근", "퇴실", "퇴장", "외출"):
        if w in low:
            return OUT
    for w in ("출근", "입실", "입장"):
        if w in low:
            return IN
    eng = s.lower()
    if _EN_OUT.search(eng):
        return OUT
    if _EN_IN.search(eng):
        return IN
    return None


# ── 파일 읽기 ────────────────────────────────────────────────
@dataclass
class LoadReport:
    source: str
    header_row: int = 0
    columns: dict = field(default_factory=dict)
    total_rows: int = 0
    loaded: int = 0
    skipped: list[tuple[int, str, str]] = field(default_factory=list)  # (행번호, 사유, 원본)
    unknown_labels: dict = field(default_factory=dict)  # 해석 못 한 메뉴 값 → 건수
    encoding: str = ""


def _read_table(path: Path) -> tuple[list[list], str]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise SystemExit("엑셀(.xlsx) 파일을 읽으려면 openpyxl 이 필요합니다: pip install openpyxl\n"
                             "또는 엑셀에서 'CSV UTF-8' 로 저장해 사용하세요.") from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        return rows, "xlsx"
    if path.suffix.lower() == ".xls":
        raise SystemExit("구형 엑셀(.xls)은 지원하지 않습니다. 엑셀에서 .xlsx 또는 CSV 로 저장해 주세요.")
    data = path.read_bytes()
    return read_csv_bytes(data)


def read_csv_bytes(data: bytes) -> tuple[list[list], str]:
    text, enc = None, ""
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-16"):
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        text, enc = data.decode("latin-1"), "latin-1"
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect)]
    return rows, enc


def _match_columns(header: list) -> dict[str, int]:
    normed = [_norm(h) for h in header]
    found: dict[str, int] = {}
    used: set[int] = set()
    # 정확히 일치하는 이름을 먼저, 그다음 포함 관계로 찾는다.
    for exact in (True, False):
        for fld, names in ALIASES.items():
            if fld in found:
                continue
            for alias in names:
                for i, h in enumerate(normed):
                    if i in used or not h:
                        continue
                    if exact:
                        ok = h == alias
                    else:  # 포함 관계는 한글 별칭이거나 충분히 긴 영문 별칭일 때만 허용
                        ok = (not alias.isascii() or len(alias) >= 4) and alias in h
                    if ok:
                        found[fld] = i
                        used.add(i)
                        break
                if fld in found:
                    break
    return found


def load_punches(path: str | Path, column_overrides: Optional[dict] = None,
                 in_values: Iterable[str] = (), out_values: Iterable[str] = ()) -> tuple[list[Punch], LoadReport]:
    path = Path(path)
    rows, enc = _read_table(path)
    return punches_from_rows(rows, str(path), enc, column_overrides, in_values, out_values)


def punches_from_rows(rows: list[list], source: str = "", encoding: str = "",
                      column_overrides: Optional[dict] = None,
                      in_values: Iterable[str] = (), out_values: Iterable[str] = ()) -> tuple[list[Punch], LoadReport]:
    rep = LoadReport(source=source, encoding=encoding)
    in_values, out_values = list(in_values), list(out_values)

    # 머리글 행 찾기 (엑셀 내보내기 파일은 위쪽에 제목 행이 있는 경우가 많다)
    best, best_score = 0, -1
    for i, r in enumerate(rows[:15]):
        score = len(_match_columns(r))
        if score > best_score:
            best, best_score = i, score
    header = [str(h) if h is not None else "" for h in rows[best]] if rows else []
    cols = _match_columns(header)
    if column_overrides:
        for fld, name in column_overrides.items():
            if not name:
                continue
            idx = next((i for i, h in enumerate(header) if h.strip() == name or _norm(h) == _norm(name)), None)
            if idx is None:
                raise SystemExit(f"지정한 열 '{name}' 을(를) 찾을 수 없습니다. 열 목록: {header}")
            cols[fld] = idx

    # '시간' 열에 날짜까지 들어 있는 경우(예: '2026-11-02 09:00')를 처리
    data_rows = rows[best + 1:]
    if "datetime" not in cols and "time" in cols:
        sample = [r[cols["time"]] for r in data_rows[:50] if cols["time"] < len(r) and r[cols["time"]] not in (None, "")]
        if sample and sum(parse_datetime(v) is not None for v in sample) >= 0.8 * len(sample):
            cols["datetime"] = cols.pop("time")
    if "datetime" not in cols and "date" in cols:
        sample = [r[cols["date"]] for r in data_rows[:50] if cols["date"] < len(r) and r[cols["date"]] not in (None, "")]
        if sample and "time" not in cols and sum(parse_datetime(v) is not None for v in sample) >= 0.8 * len(sample):
            cols["datetime"] = cols.pop("date")

    if "emp_id" not in cols and "name" in cols:
        cols["emp_id"] = cols["name"]  # 사번이 없으면 이름으로 구분
    missing = [f for f in ("emp_id",) if f not in cols]
    if "datetime" not in cols and not ("date" in cols and "time" in cols):
        missing.append("일시(또는 날짜+시간)")
    if missing:
        raise SystemExit(f"필수 열을 찾지 못했습니다: {missing}. 인식된 머리글: {header}\n"
                         f"--col-emp, --col-datetime 등의 옵션으로 열 이름을 직접 지정할 수 있습니다.")

    rep.header_row = best + 1
    rep.columns = {k: header[v] for k, v in cols.items()}

    def cell(r, fld):
        i = cols.get(fld)
        return r[i] if i is not None and i < len(r) else None

    punches: list[Punch] = []
    for offset, r in enumerate(data_rows):
        row_no = best + 2 + offset
        if not r or all(c in (None, "") for c in r):
            continue
        rep.total_rows += 1
        emp = cell(r, "emp_id")
        emp = str(emp).strip() if emp is not None else ""
        if emp.endswith(".0") and emp[:-2].isdigit():  # 엑셀이 숫자로 바꾼 사번
            emp = emp[:-2]
        if not emp:
            rep.skipped.append((row_no, "사번 없음", ",".join(map(str, r))))
            continue
        if "datetime" in cols:
            ts = parse_datetime(cell(r, "datetime"))
        else:
            d, t = parse_date(cell(r, "date")), parse_time(cell(r, "time"))
            ts = datetime.combine(d, t) if d and t else None
        if ts is None:
            rep.skipped.append((row_no, "일시 해석 불가", ",".join("" if c is None else str(c) for c in r)))
            continue
        raw_label = cell(r, "label")
        raw_label = "" if raw_label is None else str(raw_label).strip()
        label = parse_label(raw_label, in_values, out_values)
        if raw_label and label is None:
            rep.unknown_labels[raw_label] = rep.unknown_labels.get(raw_label, 0) + 1
        name = cell(r, "name")
        dev = cell(r, "device")
        punches.append(Punch(row_no=row_no, emp_id=emp, name="" if name is None else str(name).strip(),
                             ts=ts, label=label, raw_label=raw_label,
                             device="" if dev is None else str(dev).strip()))
    rep.loaded = len(punches)
    return punches, rep


# ── 파일 쓰기 ────────────────────────────────────────────────
def write_csv(path: Path, header: list[str], rows: Iterable[list]) -> None:
    """엑셀에서 한글이 깨지지 않도록 UTF-8 BOM 으로 저장한다."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def load_confirmations(path: str | Path):
    """담당자가 채운 확정 파일(review_queue.csv 형식) 읽기.

    필요한 열: 사번, 지문시각, 확정(출근/퇴근/삭제), [추가시각], [메모]
    """
    from .engine import Confirmation

    rows, _ = _read_table(Path(path))
    if not rows:
        return []
    header = [_norm(h) for h in rows[0]]

    def col(*names):
        for n in names:
            if _norm(n) in header:
                return header.index(_norm(n))
        return None

    c_emp = col("사번", "emp_id", "id")
    c_ts = col("지문시각", "일시", "출입일시", "datetime")
    c_dec = col("확정", "확정판정", "decision")
    c_add = col("추가시각", "add_time")
    c_memo = col("메모", "memo", "비고")
    if c_emp is None or c_dec is None:
        raise SystemExit(f"확정 파일에 '사번'과 '확정' 열이 필요합니다: {path}")
    out = []
    for r in rows[1:]:
        get = lambda i: (r[i] if i is not None and i < len(r) else None)
        dec = get(c_dec)
        if dec in (None, ""):
            continue
        emp = str(get(c_emp) or "").strip()
        if emp.endswith(".0") and emp[:-2].isdigit():
            emp = emp[:-2]
        out.append(Confirmation(emp_id=emp, ts=parse_datetime(get(c_ts)), decision=str(dec).strip(),
                                add_ts=parse_datetime(get(c_add)), memo=str(get(c_memo) or "").strip()))
    return out
