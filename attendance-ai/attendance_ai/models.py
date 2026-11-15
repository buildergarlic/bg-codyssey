"""데이터 구조."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

IN = "IN"
OUT = "OUT"
STATES = (IN, OUT)
KOR = {IN: "출근", OUT: "퇴근", None: "(선택없음)"}


@dataclass
class Punch:
    """단말기에서 내려받은 원본 지문 기록 한 줄."""

    row_no: int  # 원본 파일의 행 번호 (감사 추적용)
    emp_id: str
    name: str
    ts: datetime  # 실제 지문을 찍은 시각
    label: Optional[str]  # 사용자가 고른 메뉴 (IN / OUT / None)
    raw_label: str  # 원본에 적혀 있던 값 그대로
    device: str = ""
    manual: bool = False  # 담당자가 수기로 추가한 기록
    forced: Optional[str] = None  # 담당자가 확정한 출입 종류 (AI 판정보다 우선)
    note: str = ""


@dataclass
class Event:
    """중복 입력을 하나로 묶은 '한 번의 출입 행동'."""

    emp_id: str
    name: str
    punches: list[Punch]

    @property
    def first_ts(self) -> datetime:
        return self.punches[0].ts

    @property
    def last_ts(self) -> datetime:
        return self.punches[-1].ts

    @property
    def label(self) -> Optional[str]:
        """묶음 안에서 마지막으로 고른 메뉴 = 사용자의 최종 의도로 간주."""
        for p in reversed(self.punches):
            if p.label is not None:
                return p.label
        return None

    @property
    def forced(self) -> Optional[str]:
        """담당자가 확정한 값이 있으면 그것이 최종 (사람 > AI)."""
        for p in reversed(self.punches):
            if p.forced is not None:
                return p.forced
        return None

    @property
    def relabelled(self) -> bool:
        """짧은 시간 안에 서로 다른 메뉴로 다시 찍었는가."""
        labels = {p.label for p in self.punches if p.label is not None}
        return len(labels) > 1

    @property
    def device(self) -> str:
        return self.punches[0].device

    def time_for(self, state: str) -> datetime:
        """출근은 처음 찍은 시각, 퇴근은 마지막 찍은 시각을 실제 시각으로 사용한다.

        묶음의 처음~마지막 사이에는 본인이 단말기 앞(사업장 안)에 있었으므로
        이 방식이 실제 체류 사실과 가장 일치한다.
        """
        return self.first_ts if state == IN else self.last_ts


@dataclass
class Decision:
    """AI가 하나의 출입 행동에 대해 내린 판정."""

    event: Event
    state: str  # AI가 판단한 실제 출입 종류
    confidence: float  # 판정 확신도 (0~1, 사후확률)
    evidence: dict  # 증거별 기여도 (로그 오즈, +면 판정을 지지)
    action: str = ""  # 정상 / 자동정정 / 정정제안(검토) / 검토필요
    reasons: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.event.label is not None and self.event.label != self.state

    @property
    def needs_review(self) -> bool:
        return self.action in ("검토필요", "정정제안")

    @property
    def ts(self) -> datetime:
        return self.event.time_for(self.state)


@dataclass
class Session:
    """출근부터 퇴근까지 하나의 근무 단위 (자정을 넘는 야간근무 포함)."""

    emp_id: str
    name: str
    work_date: date  # 근무일 = 출근한 날짜
    decisions: list[Decision] = field(default_factory=list)
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    missing_in: bool = False
    missing_out: bool = False
    notes: list[str] = field(default_factory=list)

    work_minutes: float = 0.0
    stay_minutes: float = 0.0
    outing_minutes: float = 0.0
    night_minutes: float = 0.0
    boundary: bool = False  # 누락된 부분이 조회 기간 바깥일 가능성 (앞/뒤 파일과 합쳐야 함)
    anomalies: list[str] = field(default_factory=list)  # 안전장치가 발견한 이상 징후
    baseline_minutes: Optional[float] = None  # 메뉴 선택을 그대로 믿었을 때의 계산값

    @property
    def complete(self) -> bool:
        return not (self.missing_in or self.missing_out)

    @property
    def needs_review(self) -> bool:
        return ((not self.complete and not self.boundary) or bool(self.anomalies)
                or any(d.needs_review for d in self.decisions))

    @property
    def corrected(self) -> bool:
        return any(d.changed for d in self.decisions)

    @property
    def status(self) -> str:
        if not self.complete and self.boundary:
            return "기간경계"
        if not self.complete:
            return "검토필요(기록누락)"
        if self.anomalies:
            return "검토필요(이상근무)"
        if any(d.needs_review for d in self.decisions):
            return "검토필요"
        if self.corrected:
            return "자동정정"
        return "정상"
