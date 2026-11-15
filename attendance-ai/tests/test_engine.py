import unittest
from datetime import date, datetime

from attendance_ai.config import Settings
from attendance_ai.engine import Confirmation, analyze
from attendance_ai.models import IN, OUT
from attendance_ai.sessions import night_overlap_minutes

from helpers import Builder, day, find


class MenuMistakeTest(unittest.TestCase):
    def test_out_punched_as_in_is_corrected(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10)
        b.add("E1", "2026-10-19 08:58", "출근").add("E1", "2026-10-19 18:02", "출근")  # 퇴근인데 출근 선택
        b.add("E1", "2026-10-20 09:01", "출근").add("E1", "2026-10-20 18:00", "퇴근")
        r = analyze(b.punches)
        d = find(r, "E1", "2026-10-19 18:02")
        self.assertEqual(d.state, OUT)
        self.assertEqual(d.action, "자동정정")
        self.assertGreaterEqual(d.confidence, 0.95)
        self.assertTrue(any("실제로는 '퇴근'으로 판단" in x for x in d.reasons))
        row = day(r, "E1", "2026-10-19")
        self.assertEqual(round(row.work_minutes), 9 * 60 + 4)  # 실제 지문 시각 기준 9시간 4분
        self.assertIsNone(row.baseline_minutes)  # 메뉴만 믿으면 계산 불가였던 날
        self.assertEqual(row.status, "자동정정")

    def test_in_punched_as_out_is_corrected(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10)
        b.add("E1", "2026-10-19 08:55", "퇴근").add("E1", "2026-10-19 18:05", "퇴근")
        b.add("E1", "2026-10-20 09:01", "출근").add("E1", "2026-10-20 18:00", "퇴근")
        r = analyze(b.punches)
        self.assertEqual(find(r, "E1", "2026-10-19 08:55").state, IN)
        self.assertEqual(round(day(r, "E1", "2026-10-19").work_minutes), 9 * 60 + 10)

    def test_correct_records_are_left_alone(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 12)
        r = analyze(b.punches)
        self.assertTrue(all(d.action == "정상" for d in r.decisions["E1"]))
        self.assertEqual(r.review, [])

    def test_employee_who_always_presses_in(self):
        b = Builder().background()
        b.office_days("E2", date(2026, 10, 5), 15)
        for p in b.punches:
            if p.emp_id == "E2":
                p.label, p.raw_label = IN, "출근"
        r = analyze(b.punches)
        states = [d.state for d in r.decisions["E2"]]
        self.assertEqual(states, [IN, OUT] * 15)
        # 퇴근 때 메뉴를 바꾸지 않는 습관을 학습 (기록이 쌓일수록 낮아짐)
        self.assertLess(r.model.menu_accuracy("E2")[OUT], 0.5)
        ps = sorted((p for p in b.punches if p.emp_id == "E2"), key=lambda p: p.ts)
        want = [round((o.ts - i.ts).total_seconds() / 60) for i, o in zip(ps[::2], ps[1::2])]
        self.assertEqual([round(x.work_minutes) for x in r.days if x.emp_id == "E2"], want)

    def test_no_menu_device(self):
        b = Builder().background()
        b.office_days("E3", date(2026, 10, 5), 10)
        for p in b.punches:
            if p.emp_id == "E3":
                p.label, p.raw_label = None, ""
        r = analyze(b.punches)
        self.assertEqual([d.state for d in r.decisions["E3"]], [IN, OUT] * 10)
        self.assertTrue(all(d.action == "AI판정" for d in r.decisions["E3"]))


class ShiftTest(unittest.TestCase):
    def test_night_shift_across_midnight(self):
        b = Builder().background().night_days("N1", date(2026, 10, 5), 12)
        b.add("N1", "2026-10-17 22:03", "퇴근").add("N1", "2026-10-18 06:58", "출근")  # 둘 다 반대로 누름
        r = analyze(b.punches)
        self.assertEqual(find(r, "N1", "2026-10-17 22:03").state, IN)
        self.assertEqual(find(r, "N1", "2026-10-18 06:58").state, OUT)
        row = day(r, "N1", "2026-10-17")  # 근무일 = 출근한 날
        self.assertEqual(round(row.work_minutes), 8 * 60 + 55)
        self.assertEqual(round(row.night_minutes), 7 * 60 + 57)  # 야간(22~06시)에 해당하는 22:03~06:00
        self.assertEqual(row.session_count, 1)

    def test_night_overlap(self):
        a, b = datetime(2026, 10, 5, 21, 0), datetime(2026, 10, 6, 7, 0)
        self.assertEqual(night_overlap_minutes(a, b, 22, 6), 8 * 60)
        self.assertEqual(night_overlap_minutes(datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 18), 22, 6), 0)

    def test_lunch_outing_excluded(self):
        b = Builder().background()
        for i, d in enumerate(range(5, 10)):
            s = f"2026-10-{d:02d}"
            b.add("L1", f"{s} 08:50", "출근").add("L1", f"{s} 12:00", "퇴근").add("L1", f"{s} 13:00", "출근")
            b.add("L1", f"{s} 18:00", "퇴근")
        b.add("L1", "2026-10-12 08:50", "출근").add("L1", "2026-10-12 12:01", "출근")  # 외출을 출근으로
        b.add("L1", "2026-10-12 12:58", "출근").add("L1", "2026-10-12 18:00", "퇴근")
        r = analyze(b.punches)
        self.assertEqual(find(r, "L1", "2026-10-12 12:01").state, OUT)
        row = day(r, "L1", "2026-10-12")
        self.assertEqual(round(row.work_minutes), (12 * 60 + 1 - (8 * 60 + 50)) + (18 * 60 - (12 * 60 + 58)))
        self.assertEqual(round(row.outing_minutes), 57)


class DuplicateAndMissingTest(unittest.TestCase):
    def test_relabel_burst_is_one_event(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10)
        b.add("E1", "2026-10-19 09:00:05", "출근")
        b.add("E1", "2026-10-19 18:00:00", "출근").add("E1", "2026-10-19 18:00:25", "퇴근")  # 잘못 누르고 바로 재입력
        r = analyze(b.punches)
        d = find(r, "E1", "2026-10-19 18:00")
        self.assertEqual(len(d.event.punches), 2)
        self.assertEqual(d.state, OUT)
        self.assertEqual(d.ts, datetime(2026, 10, 19, 18, 0, 25))  # 퇴근은 마지막 찍은 시각
        self.assertEqual(d.action, "정상")

    def test_missing_out_goes_to_review(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10, skip=(date(2026, 10, 12),))
        b.add("E1", "2026-10-12 09:00", "출근")  # 퇴근을 안 찍음
        r = analyze(b.punches)
        kinds = [(it.kind, it.work_date) for it in r.review if it.emp_id == "E1"]
        self.assertIn(("퇴근 기록 누락", date(2026, 10, 12)), kinds)
        self.assertEqual(day(r, "E1", "2026-10-12").status, "검토필요(기록누락)")

    def test_missing_punch_does_not_cause_silent_flip(self):
        # 퇴근 누락 + 다음날 출근 누락: 확률 모델만으로는 멀쩡한 기록을 뒤집어 '24시간 근무'로 볼 수 있다.
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10)
        b.add("E1", "2026-10-19 09:00", "출근")
        b.add("E1", "2026-10-20 18:00", "퇴근")
        b.add("E1", "2026-10-21 09:00", "출근").add("E1", "2026-10-21 18:00", "퇴근")
        r = analyze(b.punches)
        auto_wrong = [x for x in r.days if x.emp_id == "E1" and not x.status.startswith("검토")
                      and x.work_minutes > 16 * 60]
        self.assertEqual(auto_wrong, [])
        self.assertTrue(any(it.emp_id == "E1" and it.work_date in (date(2026, 10, 19), date(2026, 10, 20))
                            for it in r.review))

    def test_boundary_sessions_not_counted_as_errors(self):
        b = Builder()
        b.add("N1", "2026-10-05 07:00", "퇴근")  # 조회 시작 전날 밤에 출근
        b.night_days("N1", date(2026, 10, 5), 10)
        b.add("N1", "2026-10-15 22:00", "출근")  # 조회 종료 후 퇴근
        r = analyze(b.punches)
        sess = r.sessions["N1"]
        self.assertTrue(sess[0].boundary and sess[-1].boundary)
        self.assertFalse(any(it.emp_id == "N1" and "누락" in it.kind for it in r.review))
        self.assertEqual(sess[0].status, "기간경계")

    def test_records_stopping_mid_period_are_reviewed(self):
        # 다른 직원 기록은 계속되는데 이 직원만 출근 후 기록이 끊기면 경계가 아니라 누락이다
        b = Builder().background()
        b.night_days("N1", date(2026, 10, 5), 5)
        b.add("N1", "2026-10-10 22:00", "출근")
        r = analyze(b.punches)
        self.assertFalse(r.sessions["N1"][-1].boundary)
        self.assertTrue(any(it.emp_id == "N1" and it.kind == "퇴근 기록 누락" for it in r.review))


class ConfirmationTest(unittest.TestCase):
    def test_human_confirmation_overrides_and_fills_missing(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10, skip=(date(2026, 10, 12),))
        b.add("E1", "2026-10-12 09:00", "출근")
        b.add("E1", "2026-10-13 08:57", "퇴근").add("E1", "2026-10-13 18:00", "퇴근")
        conf = [
            Confirmation("E1", datetime(2026, 10, 12, 9, 0), "퇴근", add_ts=datetime(2026, 10, 12, 18, 30),
                         memo="팀장 확인"),
            Confirmation("E1", datetime(2026, 10, 13, 8, 57), "출근", memo="본인 소명"),
        ]
        r = analyze(b.punches, confirmations=conf)
        self.assertEqual(day(r, "E1", "2026-10-12").status, "정상")
        self.assertEqual(round(day(r, "E1", "2026-10-12").work_minutes), 9 * 60 + 30)
        d = find(r, "E1", "2026-10-13 08:57")
        self.assertEqual((d.state, d.action, d.confidence), (IN, "담당자확정", 1.0))
        self.assertTrue(any("본인 소명" in x for x in d.reasons))
        self.assertEqual(r.stats["manual_added"], 1)

    def test_delete_confirmation(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10)
        b.add("E1", "2026-10-19 09:00", "출근").add("E1", "2026-10-19 09:40", "출근").add("E1", "2026-10-19 18:00", "퇴근")
        r = analyze(b.punches, confirmations=[Confirmation("E1", datetime(2026, 10, 19, 9, 40), "삭제", memo="오입력")])
        self.assertEqual(len(r.deleted), 1)
        self.assertEqual(day(r, "E1", "2026-10-19").status, "정상")


class PolicyTest(unittest.TestCase):
    def test_stricter_auto_threshold_sends_more_to_review(self):
        b = Builder().background().office_days("E1", date(2026, 10, 5), 10)
        b.add("E1", "2026-10-19 08:58", "출근").add("E1", "2026-10-19 18:02", "출근")
        r = analyze(b.punches, Settings(auto_correct_threshold=0.99999999, review_threshold=0.5))
        self.assertEqual(find(r, "E1", "2026-10-19 18:02").action, "정정제안")

    def test_every_input_row_is_accounted_for(self):
        from attendance_ai.outputs import punch_rows

        b = Builder().background().office_days("E1", date(2026, 10, 5), 10)
        b.add("E1", "2026-10-19 18:00:00", "출근").add("E1", "2026-10-19 18:00:25", "퇴근")
        r = analyze(list(b.punches))
        self.assertEqual(len(punch_rows(r)), len(b.punches))


if __name__ == "__main__":
    unittest.main()
