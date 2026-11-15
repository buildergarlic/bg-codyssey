import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path

from attendance_ai.io_utils import (load_punches, parse_datetime, parse_label, parse_time, punches_from_rows,
                                    read_csv_bytes)
from attendance_ai.models import IN, OUT


class ParseDatetimeTest(unittest.TestCase):
    def test_formats(self):
        cases = {
            "2026-11-02 09:01:23": datetime(2026, 11, 2, 9, 1, 23),
            "2026.11.02 09:01": datetime(2026, 11, 2, 9, 1),
            "2026/11/2 18:05:00": datetime(2026, 11, 2, 18, 5),
            "2026-11-02 오후 6:05:00": datetime(2026, 11, 2, 18, 5),
            "2026-11-02 오전 12:30:00": datetime(2026, 11, 2, 0, 30),
            "2026-11-02 오후 12:30": datetime(2026, 11, 2, 12, 30),
            "2026-11-02 6:05:00 PM": datetime(2026, 11, 2, 18, 5),
            "20261102180500": datetime(2026, 11, 2, 18, 5),
            "2026-11-02T09:00:00": datetime(2026, 11, 2, 9, 0),
            "2026년 11월 2일 09시 15분": datetime(2026, 11, 2, 9, 15),
            "2026-11-02 (월) 09:15": datetime(2026, 11, 2, 9, 15),
        }
        for s, want in cases.items():
            with self.subTest(s=s):
                self.assertEqual(parse_datetime(s), want)

    def test_excel_serial_and_bad(self):
        self.assertEqual(parse_datetime(46328.5), datetime(2026, 11, 2, 12, 0))
        self.assertIsNone(parse_datetime("어제 아침"))
        self.assertIsNone(parse_datetime(""))

    def test_time_only(self):
        self.assertEqual(parse_time("오후 6:05"), time(18, 5))
        self.assertEqual(parse_time("0905"), time(9, 5))
        self.assertEqual(parse_time(0.5), time(12, 0))


class ParseLabelTest(unittest.TestCase):
    def test_korean_and_english(self):
        cases = {"출근": IN, "퇴근": OUT, "외출": OUT, "복귀": IN, "외출복귀": IN, "출근(F1)": IN, "퇴근 인증": OUT,
                 "Check-In": IN, "CheckOut": OUT, "clock out": OUT, "IN": IN, "OUT": OUT, "Break-Out": OUT,
                 "지문(Finger)": None, "1": None, "": None, "정상": None}
        for s, want in cases.items():
            with self.subTest(s=s):
                self.assertEqual(parse_label(s), want)

    def test_custom_codes(self):
        self.assertEqual(parse_label("1", extra_in=["1"], extra_out=["2"]), IN)
        self.assertEqual(parse_label("F2", extra_in=["f1"], extra_out=["f2"]), OUT)


class LoadTest(unittest.TestCase):
    def test_title_rows_and_korean_headers(self):
        rows = [["출입기록 조회 (2026년 11월)"], [], ["번호", "출입일시", "사번", "이름", "부서", "단말기", "구분"],
                ["1", "2026-11-02 09:01:00", "1001", "홍길동", "생산팀", "정문", "출근"],
                ["2", "2026-11-02 18:01:00", "1001", "홍길동", "생산팀", "정문", "출근"],
                ["3", "엉망", "1001", "홍길동", "생산팀", "정문", "퇴근"]]
        ps, rep = punches_from_rows(rows)
        self.assertEqual(rep.header_row, 3)
        self.assertEqual(rep.columns["datetime"], "출입일시")
        self.assertEqual(len(ps), 2)
        self.assertEqual(ps[0].row_no, 4)  # 원본 파일 행 번호 보존
        self.assertEqual(len(rep.skipped), 1)
        self.assertEqual(rep.skipped[0][0], 6)

    def test_separate_date_and_time_columns(self):
        rows = [["사원번호", "성명", "일자", "시간", "근태구분"],
                ["A1", "김", "2026-11-02", "오전 8:59:10", "출근"],
                ["A1", "김", "20261102", "1802", "퇴근"]]
        ps, _ = punches_from_rows(rows)
        self.assertEqual([p.ts for p in ps], [datetime(2026, 11, 2, 8, 59, 10), datetime(2026, 11, 2, 18, 2)])

    def test_time_column_holding_full_datetime(self):
        rows = [["사번", "시간", "모드"], ["1", "2026-11-02 08:59", "출근"]]
        ps, rep = punches_from_rows(rows)
        self.assertEqual(ps[0].ts, datetime(2026, 11, 2, 8, 59))
        self.assertIn("datetime", rep.columns)

    def test_unknown_label_reported(self):
        rows = [["사번", "일시", "구분"], ["1", "2026-11-02 08:59", "F1"], ["1", "2026-11-02 18:00", "F2"]]
        ps, rep = punches_from_rows(rows)
        self.assertEqual(rep.unknown_labels, {"F1": 1, "F2": 1})
        ps, rep = punches_from_rows(rows, in_values=["F1"], out_values=["F2"])
        self.assertEqual([p.label for p in ps], [IN, OUT])

    def test_cp949_csv(self):
        text = "사번,이름,일시,구분\n7,이몽룡,2026-11-02 09:00,출근\n"
        rows, enc = read_csv_bytes(text.encode("cp949"))
        self.assertEqual(enc, "cp949")
        ps, _ = punches_from_rows(rows)
        self.assertEqual(ps[0].name, "이몽룡")

    def test_xlsx(self):
        try:
            import openpyxl
        except ImportError:  # pragma: no cover
            self.skipTest("openpyxl 없음")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["사번", "이름", "출입일시", "구분"])
        ws.append([1001, "홍길동", datetime(2026, 11, 2, 9, 0), "출근"])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.xlsx"
            wb.save(p)
            ps, _ = load_punches(p)
        self.assertEqual(ps[0].emp_id, "1001")
        self.assertEqual(ps[0].ts, datetime(2026, 11, 2, 9, 0))


if __name__ == "__main__":
    unittest.main()
