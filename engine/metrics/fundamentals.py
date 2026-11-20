"""engine.metrics.fundamentals — Q 점수에 필요한 재무비율(결정론 계산, LLM 없음).

3.2.2:
  NOPAT = EBIT × (1 − 유효세율)
  투하자본 = 총자산 − 비이자부채 − 초과현금
  ROIC = NOPAT / 투하자본, WACC = (E/V)Re + (D/V)Rd(1−t), Re = DGS10 + β×5%
  FCF(지표) = 영업현금흐름 − CAPEX, FCFF(DCF) = EBIT(1−t) + 감가상각 − CAPEX − ΔNWC
  Owner Earnings = 영업현금흐름 − 유지보수 CAPEX(참고 표시, DCF 입력 아님)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np


@dataclass
class AnnualFundamentals:
    """연간 재무(백만 달러). 최근 회계연도부터 최신순으로 넣는다(6년 수집이면 6개).

    점수·게이트 계산은 **앞 5년**만 쓴다(6번째 해는 ΔNWC 5년 시계열을 만들기 위한 것).
    `diluted_shares` 는 **결정 시점 기준으로 분할·재작성이 반영된 주식수**이고,
    공시 그대로의 값은 `diluted_shares_reported`, 연도별 근거는 `shares_basis` 에 남는다(R05).
    """

    ebit: list[float]
    revenue: list[float]
    net_income: list[float]
    ocf: list[float]
    capex: list[float]
    depreciation: list[float]
    total_assets: list[float]
    non_interest_liabilities: list[float]
    excess_cash: list[float]
    total_debt: list[float]
    cash: list[float]
    equity: list[float]
    diluted_shares: list[float]
    interest_expense: list[float]
    tax_rate: float = 0.21
    beta: float = 1.0
    risk_free: float = 0.042
    equity_risk_premium: float = 0.05
    cost_of_debt: float = 0.05
    minority_interest: float = 0.0
    # ── R03·R05: 밸류에이션 정상화·주식수 근거(기본값이면 기존 데이터셋 그대로 동작) ──
    marketable_securities: list[float] = field(default_factory=list)   # 시장성 단기투자(백만 달러)
    operating_nwc: list[float] = field(default_factory=list)           # (유동자산−현금−시장성)−유동부채
    delta_nwc: list[float] = field(default_factory=list)               # 연도별 ΔNWC(5% 상한 적용, 백만 달러)
    diluted_shares_reported: list[float] = field(default_factory=list)  # 공시 그대로의 주식수
    shares_basis: list[str] = field(default_factory=list)              # as_reported | restated | split_adjusted | unverified
    ttm_eps: float = float("nan")                                      # 10-Q 누적에서 계산한 TTM EPS(USD)
    eps_cagr: float | None = None                                      # 5년 EPS CAGR(소수)


def _mean(xs: list[float]) -> float:
    vals = [x for x in xs if x is not None and np.isfinite(x)]
    return float(np.mean(vals)) if vals else float("nan")


def invested_capital(f: AnnualFundamentals, i: int = 0) -> float:
    return f.total_assets[i] - f.non_interest_liabilities[i] - f.excess_cash[i]


def roic(f: AnnualFundamentals, i: int = 0) -> float:
    ic = invested_capital(f, i)
    if ic <= 0:
        return float("nan")
    return (f.ebit[i] * (1 - f.tax_rate)) / ic


def sanitize_fundamentals(f: AnnualFundamentals | None) -> AnnualFundamentals | None:
    """`None` 이 섞인 재무 리스트를 `NaN` 으로 되돌린다(수치 함수는 NaN 만 견딘다).

    스냅샷 `inputs.json` 은 NaN 을 `null` 로 직렬화하므로, 복원한 `AnnualFundamentals` 에는
    `None` 이 들어올 수 있다. 그대로 두면 `np.isfinite(None)` 에서 터진다.
    """
    if f is None:
        return None
    def _clean(values) -> list[float]:
        out: list[float] = []
        for v in values or []:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(float("nan"))
        return out

    names = ("ebit", "revenue", "net_income", "ocf", "capex", "depreciation", "total_assets",
             "non_interest_liabilities", "excess_cash", "total_debt", "cash", "equity",
             "diluted_shares", "interest_expense", "marketable_securities", "operating_nwc",
             "delta_nwc", "diluted_shares_reported")
    updates = {name: _clean(getattr(f, name, None)) for name in names}
    updates["ttm_eps"] = float("nan") if f.ttm_eps is None else f.ttm_eps
    return replace(f, **updates)


def qc_wacc(f: AnnualFundamentals) -> float:
    """**품질 점수(Q) 경로**의 WACC — β=1.0·rf 고정값을 쓰는 근사식(3.2.2).

    가치평가(V) 경로의 `valuation_wacc`(가격 회귀 베타·DGS10)와 **다른 값**이다(R03).
    같은 이름으로 두 경로를 부르면 8.68%와 10.64%가 조용히 섞인다.
    """
    e = max(f.equity[0], 0.0)
    d = max(f.total_debt[0], 0.0)
    v = e + d
    if v <= 0:
        return float("nan")
    re = f.risk_free + f.beta * f.equity_risk_premium
    return (e / v) * re + (d / v) * f.cost_of_debt * (1 - f.tax_rate)


def roic_spread(f: AnnualFundamentals) -> float:
    """ROIC(5년 평균) − WACC(Q 경로)."""
    r = _mean([roic(f, i) for i in range(min(5, len(f.ebit)))])
    return r - qc_wacc(f)


def roe_series(f: AnnualFundamentals) -> list[float]:
    return [f.net_income[i] / f.equity[i] for i in range(min(5, len(f.equity))) if f.equity[i] > 0]


def roe_stdev(f: AnnualFundamentals) -> float:
    vals = roe_series(f)
    return float(np.std(vals, ddof=0)) if len(vals) >= 2 else float("nan")


def fcf(f: AnnualFundamentals, i: int = 0) -> float:
    return f.ocf[i] - f.capex[i]


def fcf_margin_5y(f: AnnualFundamentals) -> float:
    vals = [fcf(f, i) / f.revenue[i] for i in range(min(5, len(f.revenue))) if f.revenue[i] > 0]
    return _mean(vals)


def fcf_to_ni_5y(f: AnnualFundamentals) -> float:
    vals = [fcf(f, i) / f.net_income[i] for i in range(min(5, len(f.net_income))) if f.net_income[i] > 0]
    return _mean(vals)


def ebitda(f: AnnualFundamentals, i: int = 0) -> float:
    return f.ebit[i] + f.depreciation[i]


def net_debt_ebitda(f: AnnualFundamentals, i: int = 0) -> float:
    nd = f.total_debt[i] - f.cash[i]
    e = ebitda(f, i)
    if e <= 0:
        return float("inf") if nd > 0 else 0.0
    return nd / e


def interest_cover(f: AnnualFundamentals, i: int = 0) -> float:
    if f.interest_expense[i] <= 0:
        return float("inf")  # 무차입 기업은 통과
    return f.ebit[i] / f.interest_expense[i]


def dilution_3y(f: AnnualFundamentals) -> float:
    """3년 주식수 증가율 — `diluted_shares` 는 이미 분할 환산·재작성 반영된 기준이다(R05).

    기준이 확인되지 않은 연도(`unverified`)가 있으면 계산하지 않는다(모르면 0으로 채우지 않는다).
    """
    if len(f.diluted_shares) < 4 or f.diluted_shares[3] <= 0:
        return float("nan")
    basis = list(f.shares_basis or [])
    if any(basis[i] == "unverified" for i in (0, 3) if i < len(basis)):
        return float("nan")
    if not np.isfinite(f.diluted_shares[0]):
        return float("nan")
    return f.diluted_shares[0] / f.diluted_shares[3] - 1.0


def fcf_positive_years(f: AnnualFundamentals, years: int = 5) -> int:
    return sum(1 for i in range(min(years, len(f.ocf))) if fcf(f, i) > 0)


def owner_earnings(f: AnnualFundamentals, i: int = 0, revenue_growth_3y: float = 0.0) -> float:
    """참고 표시값 — DCF 입력으로 쓰지 않는다(3.2.2)."""
    maint_capex = max(0.0, f.depreciation[i] * (1 + revenue_growth_3y))
    return f.ocf[i] - maint_capex


def _scale(value: float, zero: float, full: float) -> float:
    """zero → 0점, full → 100점 선형(방향은 zero<full 기준), 범위 밖은 자른다."""
    if not np.isfinite(value):
        return 0.0
    if full == zero:
        return 0.0
    x = (value - zero) / (full - zero) * 100.0
    return float(max(0.0, min(100.0, x)))


def quality_score(f: AnnualFundamentals) -> dict:
    """Q (0~100) — 3.2.2 표의 배점/만점 기준."""
    spread = roic_spread(f)
    roe_sd = roe_stdev(f)
    fcf_m = fcf_margin_5y(f)
    fcf_ni = fcf_to_ni_5y(f)
    nd_e = net_debt_ebitda(f)
    ic = interest_cover(f)
    dil = dilution_3y(f)

    parts = {
        # 자본수익성 30점: 스프레드 0 → 0, +10%p → 100
        "roic_spread": 30 * _scale(spread, 0.0, 0.10) / 100.0,
        # 안정성 15점: 표준편차 6%p → 0, 3%p → 100, 0%p → 100 (낮을수록 좋다)
        "roe_stability": 15 * (1.0 - _scale(roe_sd, 0.03, 0.09) / 100.0) if np.isfinite(roe_sd) else 0.0,
        # 현금창출 20점: FCF 마진 0 → 0, 20% → 100
        "fcf_margin": 20 * _scale(fcf_m, 0.0, 0.20) / 100.0,
        # 이익 품질 15점: FCF/순이익 0.7 → 0, 1.0 → 100
        "fcf_quality": 15 * _scale(fcf_ni, 0.70, 1.00) / 100.0,
        # 재무 건전성 10점: 순부채/EBITDA 2.5배 → 0, 1.0배 이하 → 100 + 이자보상 6배 이상
        "balance_sheet": 10 * (
            0.6 * (1.0 - _scale(nd_e, 1.0, 2.5) / 100.0)
            + 0.4 * (1.0 if ic >= 15 else _scale(ic, 6.0, 15.0) / 100.0)
        ),
        # 희석 10점: +5% → 0, 0% → 100
        "dilution": 10 * (1.0 - _scale(dil, 0.0, 0.05) / 100.0) if np.isfinite(dil) else 0.0,
    }
    total = float(sum(parts.values()))
    return {
        "q": round(total, 2),
        "parts": {k: round(v, 2) for k, v in parts.items()},
        "metrics": {
            "roic_spread": spread, "roe_stdev": roe_sd, "fcf_margin": fcf_m,
            "fcf_to_ni": fcf_ni, "net_debt_ebitda": nd_e, "interest_cover": ic,
            "dilution_3y": dil, "wacc_qc": qc_wacc(f), "roic": roic(f),
        },
    }


def growth_score(peg: float | None, revenue_cagr: float | None,
                 peg_full: float = 0.8, peg_zero: float = 3.0) -> dict:
    """G (0~100, 피터 린치) — PEG 중심, 성장률 음수면 0."""
    if revenue_cagr is not None and revenue_cagr <= 0:
        return {"g": 0.0, "reason": "negative_growth"}
    if peg is None or not math.isfinite(peg) or peg <= 0:
        return {"g": 0.0, "reason": "peg_unavailable"}
    base = _scale(-peg, -peg_zero, -peg_full)  # PEG 작을수록 높은 점수
    bonus = 10.0 if (revenue_cagr or 0) >= 0.05 and peg <= 1.5 else 0.0
    return {"g": round(min(100.0, base + bonus), 2), "reason": None}


def hard_gate_checks(f: AnnualFundamentals, market: dict) -> list[str]:
    """HG-05~HG-09 (재무로 판정 가능한 것만). 실패 코드 목록을 반환한다."""
    fails: list[str] = []
    if fcf_positive_years(f) < 4:
        fails.append("HG-05")
    nd = net_debt_ebitda(f)
    if np.isfinite(nd) and nd > 2.5:
        fails.append("HG-06")
    ic = interest_cover(f)
    if np.isfinite(ic) and ic < 6.0:
        fails.append("HG-07")
    fn = fcf_to_ni_5y(f)
    if np.isfinite(fn) and fn < 0.7:
        fails.append("HG-08")
    dil = dilution_3y(f)
    if not np.isfinite(dil):
        # 모르면 통과시키지 않는다(§3.2.1 "하나라도 걸리면 제외" — HG-11 취지).
        fails.append("HG-09_UNVERIFIED")
    elif dil > 0.05:
        fails.append("HG-09")
    return fails
