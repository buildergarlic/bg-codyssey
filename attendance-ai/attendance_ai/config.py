"""분석 정책(설정값).

근태는 임금과 직결되는 민감한 데이터이므로, AI가 '얼마나 확신할 때 자동으로
정정할지' 같은 정책은 코드에 숨기지 않고 모두 이 설정으로 드러낸다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Settings:
    # ── 전처리 ──────────────────────────────────────────────
    # 같은 사람이 이 시간(분) 안에 연속으로 찍은 지문은 한 번의 행동으로 본다
    # (인식 실패로 다시 찍기, 메뉴를 잘못 눌러 곧바로 다시 찍기 등).
    duplicate_window_min: float = 5.0

    # ── 판정 정책 ───────────────────────────────────────────
    # AI 판정 확신도가 이 값 이상이면 사람이 보지 않아도 자동 정정한다.
    auto_correct_threshold: float = 0.95
    # 확신도가 이 값 미만이면 (메뉴와 일치하더라도) 사람 검토 대상으로 보낸다.
    review_threshold: float = 0.80

    # ── 근무 단위(세션) 구성 ────────────────────────────────
    # 퇴근(OUT) 후 이 시간 이상 지나서 다시 출근(IN)하면 새로운 근무로 본다.
    # 이보다 짧으면 '외출 후 복귀'로 보고 같은 근무에 포함한다.
    new_shift_gap_hours: float = 4.0
    # 같은 종류(출근-출근, 퇴근-퇴근)가 이 시간(분) 안에 반복되면 '재입력'으로 보고,
    # 그보다 길면 중간 기록이 '누락'된 것으로 본다.
    repunch_window_min: float = 60.0
    # 재입력 간격이 이 시간(분)을 넘으면, 사실은 외출/복귀를 잘못 누른 것일 수 있어 검토로 보낸다.
    repunch_review_min: float = 15.0

    # ── 안전장치 (AI 판정 결과에 대한 규칙 기반 검증) ─────────
    # 출근~퇴근 한 구간이 이 시간을 넘으면 중간 기록 누락으로 보고 검토로 보낸다.
    max_interval_hours: float = 16.0
    # 자동 정정한 근무의 길이가 평소(개인 중앙값)의 이 비율 범위를 벗어나면
    # 정정을 보류하고 검토로 보낸다. (기록 누락을 메뉴 오류로 오해하는 것을 방지)
    anomaly_ratio_low: float = 0.5
    anomaly_ratio_high: float = 1.8

    # ── 근로시간 계산 ───────────────────────────────────────
    # 야간근로 시간대 (근로기준법 제56조: 22:00 ~ 06:00)
    night_start_hour: int = 22
    night_end_hour: int = 6

    # ── 모델 학습 ───────────────────────────────────────────
    em_iterations: int = 12
    # 개인 패턴이 충분히 쌓이기 전까지 전체 직원 패턴을 얼마나 참고할지
    # (가상의 기록 건수). 클수록 신규 입사자 등에게 보수적으로 동작한다.
    personal_prior_strength: float = 12.0
    # 증거별 가중치 (독립 가정에 따른 과신을 막기 위한 온도 조절)
    weight_time_of_day: float = 0.8
    weight_gap: float = 0.8
    weight_label: float = 1.0

    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
