"""AI 판정 엔진 — 직원별 지문 기록 순서를 은닉 마르코프 모델(HMM)로 해석한다.

핵심 아이디어
-------------
'출근/퇴근 메뉴'는 사람이 누르는 것이라 틀릴 수 있다. 하지만 지문을 찍은
**시각**과 **순서**는 거짓말을 하지 않는다. 그래서 각 지문 기록의 실제 종류
(IN=출근/복귀, OUT=퇴근/외출)를 '숨겨진 상태'로 두고, 다음 증거를 함께 따져
가장 그럴듯한 해석을 찾는다.

  1. 순서     — 출근 다음엔 퇴근, 퇴근 다음엔 출근이 오는 것이 정상.
                같은 종류가 연달아 나오면 '재입력' 또는 '기록 누락'으로 본다.
  2. 시각     — 그 직원이 평소 출근하는 시간대 / 퇴근하는 시간대 (개인별 학습).
  3. 경과시간 — 출근→퇴근 사이(근무 길이), 퇴근→출근 사이(휴식 길이)의 분포.
  4. 메뉴     — 사용자가 고른 메뉴. 단, '틀릴 수 있는 힌트'로만 취급하며
                직원별로 메뉴를 잘못 누르는 비율까지 학습한다.

정답(올바른 출퇴근)이 표시된 학습 데이터 없이, 기록 자체에서 EM 알고리즘
(Baum-Welch)으로 개인별 패턴을 스스로 학습한다. 신규 입사자처럼 기록이 적은
사람은 전체 직원 패턴을 참고하도록(베이지안 축소) 해서 과신하지 않는다.

모든 판정은 증거별 기여도(로그 오즈)로 분해되어 '왜 그렇게 판단했는지'를
사람이 확인할 수 있다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .config import Settings
from .models import IN, OUT, STATES, Event

TOD_BINS = 96  # 하루를 15분 단위로
GAP_BINS = 72  # 1분 ~ 30일을 로그 눈금으로
_GAP_LOG_MAX = math.log10(60 * 24 * 30)
_LOG_HALF = math.log(0.5)


def tod_bin(dt: datetime) -> int:
    return (dt.hour * 60 + dt.minute) // 15


def gap_bin(minutes: float) -> int:
    x = math.log10(max(minutes, 1.0)) / _GAP_LOG_MAX * GAP_BINS
    return min(GAP_BINS - 1, max(0, int(x)))


def gap_bin_minutes(i: int) -> float:
    return 10 ** ((i + 0.5) / GAP_BINS * _GAP_LOG_MAX)


def _lse(a: float, b: float) -> float:
    m = a if a > b else b
    if m == -math.inf:
        return m
    return m + math.log(math.exp(a - m) + math.exp(b - m))


def _smooth(hist: list[float], sigma: float, circular: bool) -> list[float]:
    r = max(1, int(3 * sigma))
    ker = [math.exp(-0.5 * (j / sigma) ** 2) for j in range(-r, r + 1)]
    n = len(hist)
    out = [0.0] * n
    for i, v in enumerate(hist):
        if v == 0.0:
            continue
        if circular:
            for j, w in enumerate(ker):
                out[(i + j - r) % n] += v * w
        else:
            norm = sum(w for j, w in enumerate(ker) if 0 <= i + j - r < n)
            for j, w in enumerate(ker):
                t = i + j - r
                if 0 <= t < n:
                    out[t] += v * w / norm
    if circular:
        s = sum(ker)
        out = [x / s for x in out]
    return out


def _normalize(hist: list[float], floor: float = 0.0) -> list[float]:
    s = sum(hist)
    n = len(hist)
    if s <= 0:
        return [1.0 / n] * n
    return [(1 - floor) * h / s + floor / n for h in hist]


def _same_state_gap_dist(repunch_min: float) -> list[float]:
    """같은 종류가 연달아 나올 때의 간격 분포 (고정).

    - 짧은 간격: 같은 메뉴로 한 번 더 찍은 '재입력'
    - 하루 안팎: 중간 기록(퇴근 또는 출근)을 빼먹은 '누락'
    """
    rep, miss = [], []
    for i in range(GAP_BINS):
        m = gap_bin_minutes(i)
        rep.append(1.0 if m <= repunch_min else 0.0)
        miss.append(math.exp(-0.5 * ((math.log10(m) - math.log10(1440)) / 0.35) ** 2))
    rep, miss = _normalize(rep), _normalize(miss)
    return _normalize([0.5 * a + 0.5 * b for a, b in zip(rep, miss)], floor=0.02)


@dataclass
class Params:
    tod: dict  # state -> [TOD_BINS] 확률
    gap: dict  # (prev, cur) -> [GAP_BINS] 확률 (서로 다른 종류 전환만)
    acc: dict  # state -> 메뉴를 올바로 누를 확률 P(메뉴=s | 실제=s)


@dataclass
class _Seq:
    events: list[Event]
    tod: list[int]
    labels: list[Optional[str]]
    gaps: list[Optional[int]]
    forced: list[Optional[str]]


@dataclass
class _Counts:
    tod: dict = field(default_factory=lambda: {s: [0.0] * TOD_BINS for s in STATES})
    gap: dict = field(default_factory=lambda: {(IN, OUT): [0.0] * GAP_BINS, (OUT, IN): [0.0] * GAP_BINS})
    lab: dict = field(default_factory=lambda: {s: {IN: 0.0, OUT: 0.0} for s in STATES})
    same: float = 0.0
    trans: float = 0.0


@dataclass
class Inference:
    """한 직원에 대한 추론 결과."""

    states: list[str]
    confidence: list[float]
    evidence: list[dict]
    log_likelihood: float


class AttendanceHMM:
    def __init__(self, settings: Optional[Settings] = None):
        self.s = settings or Settings()
        self.same_gap = _same_state_gap_dist(self.s.repunch_window_min)
        self.delta = 0.02  # 같은 종류가 연달아 나올 확률 (재입력/누락)
        self.global_params: Optional[Params] = None
        self.emp_params: dict[str, Params] = {}
        self.emp_counts: dict[str, _Counts] = {}
        self._seqs: dict[str, _Seq] = {}
        self.history: list[float] = []
        self._typical_cache: dict = {}

    # ── 학습 ────────────────────────────────────────────────
    def fit(self, events_by_emp: dict[str, list[Event]]) -> "AttendanceHMM":
        self._seqs = {e: self._make_seq(evs) for e, evs in events_by_emp.items() if evs}
        init = Params(
            tod={s: [1.0 / TOD_BINS] * TOD_BINS for s in STATES},
            gap={k: [1.0 / GAP_BINS] * GAP_BINS for k in ((IN, OUT), (OUT, IN))},
            acc={IN: 0.85, OUT: 0.85},
        )
        self.emp_params = {e: init for e in self._seqs}
        prev_ll = None
        for _ in range(max(1, self.s.em_iterations)):
            total_ll = 0.0
            counts: dict[str, _Counts] = {}
            for emp, seq in self._seqs.items():
                c, ll = self._e_step(seq, self.emp_params[emp])
                counts[emp] = c
                total_ll += ll
            self._m_step(counts)
            self.history.append(total_ll)
            if prev_ll is not None and abs(total_ll - prev_ll) < 1e-4 * max(1.0, abs(prev_ll)):
                break
            prev_ll = total_ll
        return self

    def _make_seq(self, events: list[Event]) -> _Seq:
        gaps: list[Optional[int]] = [None]
        for a, b in zip(events, events[1:]):
            gaps.append(gap_bin((b.first_ts - a.last_ts).total_seconds() / 60))
        return _Seq(events=events, tod=[tod_bin(e.first_ts) for e in events],
                    labels=[e.label for e in events], gaps=gaps, forced=[e.forced for e in events])

    def _potentials(self, seq: _Seq, p: Params, delta: Optional[float] = None):
        s = self.s
        n = len(seq.events)
        un = []
        for k in range(n):
            row = []
            for st in STATES:
                v = s.weight_time_of_day * math.log(p.tod[st][seq.tod[k]])
                lab = seq.labels[k]
                if lab is not None:
                    a = p.acc[st]
                    v += s.weight_label * math.log(a if lab == st else 1 - a)
                if seq.forced[k] is not None and seq.forced[k] != st:
                    v = -1e9  # 담당자가 확정한 기록은 그대로 따른다
                row.append(v)
            un.append(row)
        dl = self.delta if delta is None else delta
        log_same, log_diff = math.log(dl), math.log(1 - dl)
        pair: list = [None]
        for k in range(1, n):
            g = seq.gaps[k]
            m = [[0.0, 0.0], [0.0, 0.0]]
            for ai, a in enumerate(STATES):
                for bi, b in enumerate(STATES):
                    if a == b:
                        m[ai][bi] = log_same + s.weight_gap * math.log(self.same_gap[g])
                    else:
                        m[ai][bi] = log_diff + s.weight_gap * math.log(p.gap[(a, b)][g])
            pair.append(m)
        return un, pair

    def _forward_backward(self, un, pair):
        n = len(un)
        alpha = [[0.0, 0.0] for _ in range(n)]
        beta = [[0.0, 0.0] for _ in range(n)]
        alpha[0] = [_LOG_HALF + un[0][0], _LOG_HALF + un[0][1]]
        for k in range(1, n):
            for b in (0, 1):
                alpha[k][b] = _lse(alpha[k - 1][0] + pair[k][0][b], alpha[k - 1][1] + pair[k][1][b]) + un[k][b]
        for k in range(n - 2, -1, -1):
            for a in (0, 1):
                beta[k][a] = _lse(pair[k + 1][a][0] + un[k + 1][0] + beta[k + 1][0],
                                  pair[k + 1][a][1] + un[k + 1][1] + beta[k + 1][1])
        log_z = _lse(alpha[-1][0], alpha[-1][1])
        return alpha, beta, log_z

    def _e_step(self, seq: _Seq, p: Params) -> tuple[_Counts, float]:
        un, pair = self._potentials(seq, p)
        alpha, beta, log_z = self._forward_backward(un, pair)
        c = _Counts()
        for k in range(len(un)):
            for si, st in enumerate(STATES):
                g = math.exp(alpha[k][si] + beta[k][si] - log_z)
                c.tod[st][seq.tod[k]] += g
                if seq.labels[k] is not None:
                    c.lab[st][seq.labels[k]] += g
            if k == 0:
                continue
            gb = seq.gaps[k]
            for ai, a in enumerate(STATES):
                for bi, b in enumerate(STATES):
                    x = math.exp(alpha[k - 1][ai] + pair[k][ai][bi] + un[k][bi] + beta[k][bi] - log_z)
                    if a == b:
                        c.same += x
                    else:
                        c.gap[(a, b)][gb] += x
            c.trans += 1
        return c, log_z

    def _m_step(self, counts: dict[str, _Counts]) -> None:
        alpha = self.s.personal_prior_strength
        # 전체 직원 공통 패턴
        g = _Counts()
        for c in counts.values():
            for st in STATES:
                for i in range(TOD_BINS):
                    g.tod[st][i] += c.tod[st][i]
                for l in STATES:
                    g.lab[st][l] += c.lab[st][l]
            for k in g.gap:
                for i in range(GAP_BINS):
                    g.gap[k][i] += c.gap[k][i]
            g.same += c.same
            g.trans += c.trans
        gp = Params(
            tod={st: _normalize(_smooth(g.tod[st], 1.5, True), floor=0.03) for st in STATES},
            gap={k: _normalize(_smooth(g.gap[k], 1.5, False), floor=0.02) for k in g.gap},
            acc={st: (g.lab[st][st] + 9.0) / (g.lab[st][IN] + g.lab[st][OUT] + 10.0) for st in STATES},
        )
        self.delta = min(0.2, max(0.002, (g.same + 1.0) / (g.trans + 50.0)))
        self.global_params = gp

        # 개인별 패턴 (기록이 적을수록 전체 패턴 쪽으로 당겨짐)
        self.emp_counts = counts
        for emp, c in counts.items():
            tod = {}
            for st in STATES:
                sm = _smooth(c.tod[st], 1.5, True)
                n = sum(c.tod[st])
                tod[st] = _normalize([(x + alpha * q) / (n + alpha) for x, q in zip(sm, gp.tod[st])], floor=0.03)
            gap = {}
            for k in c.gap:
                sm = _smooth(c.gap[k], 1.5, False)
                n = sum(c.gap[k])
                gap[k] = _normalize([(x + alpha * q) / (n + alpha) for x, q in zip(sm, gp.gap[k])], floor=0.02)
            acc = {}
            for st in STATES:
                tot = c.lab[st][IN] + c.lab[st][OUT]
                a = (c.lab[st][st] + alpha * gp.acc[st]) / (tot + alpha)
                acc[st] = min(0.995, max(0.02, a))
            self.emp_params[emp] = Params(tod=tod, gap=gap, acc=acc)

    # ── 추론 ────────────────────────────────────────────────
    def infer(self, emp: str, delta: Optional[float] = None) -> Inference:
        """delta 를 바꾸면 '기록 누락이 얼마나 드문가'에 대한 가정을 바꿔 다시 해석한다."""
        seq = self._seqs[emp]
        p = self.emp_params[emp]
        un, pair = self._potentials(seq, p, delta)
        alpha, beta, log_z = self._forward_backward(un, pair)
        n = len(un)

        # 비터비: 전체 순서가 가장 그럴듯한 하나의 해석
        dv = [[0.0, 0.0] for _ in range(n)]
        bp = [[0, 0] for _ in range(n)]
        dv[0] = [_LOG_HALF + un[0][0], _LOG_HALF + un[0][1]]
        for k in range(1, n):
            for b in (0, 1):
                c0, c1 = dv[k - 1][0] + pair[k][0][b], dv[k - 1][1] + pair[k][1][b]
                bp[k][b] = 0 if c0 >= c1 else 1
                dv[k][b] = max(c0, c1) + un[k][b]
        path = [0] * n
        path[-1] = 0 if dv[-1][0] >= dv[-1][1] else 1
        for k in range(n - 1, 0, -1):
            path[k - 1] = bp[k][path[k]]

        states, conf, evidence = [], [], []
        w = self.s
        for k in range(n):
            si = path[k]
            ai = 1 - si
            post = math.exp(alpha[k][si] + beta[k][si] - log_z)
            lab = seq.labels[k]
            if lab is not None:
                lp = lambda i: math.log(p.acc[STATES[i]] if lab == STATES[i] else 1 - p.acc[STATES[i]])
                label_llr = w.weight_label * (lp(si) - lp(ai))
            else:
                label_llr = 0.0
            tod_llr = w.weight_time_of_day * (math.log(p.tod[STATES[si]][seq.tod[k]])
                                              - math.log(p.tod[STATES[ai]][seq.tod[k]]))
            if k == 0:
                prev_llr = 0.0
            else:
                fm = lambda b: _lse(alpha[k - 1][0] + pair[k][0][b], alpha[k - 1][1] + pair[k][1][b])
                prev_llr = fm(si) - fm(ai)
            next_llr = beta[k][si] - beta[k][ai]
            states.append(STATES[si])
            conf.append(post)
            evidence.append({"label": label_llr, "time": tod_llr, "prev": prev_llr, "next": next_llr})
        return Inference(states=states, confidence=conf, evidence=evidence, log_likelihood=log_z)

    # ── 설명용 요약 ─────────────────────────────────────────
    def menu_accuracy(self, emp: str) -> dict:
        p = self.emp_params[emp]
        return {st: p.acc[st] for st in STATES}

    def personal_counts(self, emp: str) -> dict:
        c = self.emp_counts.get(emp)
        return {st: sum(c.tod[st]) for st in STATES} if c else {IN: 0.0, OUT: 0.0}

    def typical_times(self, emp: str, state: str, min_share: float = 0.12) -> list[str]:
        """개인 기록에서 학습한 대표 출근/퇴근 시각 (여러 근무조면 여러 개)."""
        key = (emp, state, min_share, len(self.history))
        if key not in self._typical_cache:
            self._typical_cache[key] = self._typical_times(emp, state, min_share)
        return list(self._typical_cache[key])

    def _typical_times(self, emp: str, state: str, min_share: float) -> list[str]:
        c = self.emp_counts.get(emp)
        if c is None or sum(c.tod[state]) < 2.5:
            return []
        prof = _smooth(c.tod[state], 1.5, True)
        total = sum(prof)
        win = 4
        mass = [sum(prof[(i + j) % TOD_BINS] for j in range(-win, win + 1)) for i in range(TOD_BINS)]
        cands = sorted(range(TOD_BINS), key=lambda i: -mass[i])
        picked: list[int] = []
        for i in cands:
            if mass[i] < min_share * total:
                break
            if all(min(abs(i - j), TOD_BINS - abs(i - j)) > 2 * win for j in picked):
                picked.append(i)
        out = []
        for i in sorted(picked):
            # 봉우리 주변의 원형 가중평균으로 대표 시각 계산
            sx = sy = 0.0
            for j in range(-win, win + 1):
                b = (i + j) % TOD_BINS
                ang = 2 * math.pi * (b + 0.5) / TOD_BINS
                sx += prof[b] * math.cos(ang)
                sy += prof[b] * math.sin(ang)
            minutes = (math.atan2(sy, sx) % (2 * math.pi)) / (2 * math.pi) * 1440
            minutes = int(round(minutes / 5.0) * 5) % 1440
            out.append(f"{minutes // 60:02d}:{minutes % 60:02d}")
        return out

    def time_density(self, emp: str, state: str, dt: datetime) -> float:
        return self.emp_params[emp].tod[state][tod_bin(dt)]
