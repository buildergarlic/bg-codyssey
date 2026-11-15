"""정답이 있는 가상 데이터로 품질 기준을 지키는지 확인 (회귀 방지)."""

import unittest

from attendance_ai.evaluate import run_benchmark


class QualityTest(unittest.TestCase):
    def test_quality_bar_across_scenarios(self):
        # (106, 0.10)·(105, 0.20): 주야 12시간 맞교대에서 기록 누락 때문에 출퇴근 짝이 밀릴 수 있는 사례 포함
        for n, days, seed, err in ((50, 28, 1, 0.05), (50, 28, 3, 0.10), (50, 28, 2, 0.20),
                                   (60, 30, 106, 0.10), (60, 30, 105, 0.20)):
            with self.subTest(seed=seed, error=err):
                _, _, m = run_benchmark(n_employees=n, days=days, seed=seed, base_error=err)
                self.assertGreaterEqual(m.ai_correct / m.events, 0.99)
                # 가장 중요한 기준: 맞게 찍은 기록을 AI가 '자동으로' 틀리게 바꾸지 않는다
                self.assertEqual(m.false_fix, 0)
                # 사람 검토 없이 자동 처리된 날의 근무시간이 틀린 경우가 없다
                self.assertEqual(m.days_ai_silent_wrong, 0)
                # 메뉴만 믿는 방식보다 자동 처리 가능한 날이 훨씬 많다
                self.assertGreater(m.days_ai_exact, m.days_baseline_exact)
                self.assertGreaterEqual(m.days_ai_auto / m.days, 0.93)
                # 지문 누락이 있었던 날은 (그날 하나도 안 찍은 경우를 빼고) 모두 검토로 보낸다
                self.assertGreaterEqual(m.days_missing_flagged, m.days_missing - m.days_missing_whole)


if __name__ == "__main__":
    unittest.main()
