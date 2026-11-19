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
from dataclasses import dataclass

import numpy as np


@dataclass
class AnnualFundamentals:
    """연간 재무(백만 달러). 최근 5년 값을 최신순으로 넣는다."""

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


def wacc(f: AnnualFundamentals) -> float:
    e = max(f.equity[0], 0.0)
    d = max(f.total_debt[0], 0.0)
    v = e + d
    if v <= 0:
        return float("nan")
    re = f.risk_free + f.beta * f.equity_risk_premium
    return (e / v) * re + (d / v) * f.cost_of_debt * (1 - f.tax_rate)


def roic_spread(f: AnnualFundamentals) -> float:
    """ROIC(5년 평균) − WACC."""
    r = _mean([roic(f, i) for i in range(min(5, len(f.ebit)))])
    return r - wacc(f)


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
    if len(f.diluted_shares) < 4 or f.diluted_shares[3] <= 0:
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
        "roic_spread": 30 * _scale(spread, 0.0, 0.10),
        # 안정성 15점: 표준편차 6%p → 0, 3%p → 100, 0%p → 100 (낮을수록 좋다)
        "roe_stability": 15 * (1.0 - _scale(roe_sd, 0.03, 0.09)) if np.isfinite(roe_sd) else 0.0,
        # 현금창출 20점: FCF 마진 0 → 0, 20% → 100
        "fcf_margin": 20 * _scale(fcf_m, 0.0, 0.20),
        # 이익 품질 15점: FCF/순이익 0.7 → 0, 1.0 → 100
        "fcf_quality": 15 * _scale(fcf_ni, 0.70, 1.00),
        # 재무 건전성 10점: 순부채/EBITDA 2.5배 → 0, 1.0배 이하 → 100 + 이자보상 6배 이상
        "balance_sheet": 10 * (
            0.6 * (1.0 - _scale(nd_e, 1.0, 2.5) / 100.0)
            + 0.4 * (1.0 if ic >= 15 else _scale(ic, 6.0, 15.0) / 100.0)
        ),
        # 희석 10점: +5% → 0, 0% → 100
        "dilution": 10 * (1.0 - _scale(dil, 0.0, 0.05)) if np.isfinite(dil) else 0.0,
    }
    total = float(sum(parts.values()))
    return {
        "q": round(total, 2),
        "parts": {k: round(v, 2) for k, v in parts.items()},
        "metrics": {
            "roic_spread": spread, "roe_stdev": roe_sd, "fcf_margin": fcf_m,
            "fcf_to_ni": fcf_ni, "net_debt_ebitda": nd_e, "interest_cover": ic,
            "dilution_3y": dil, "wacc": wacc(f), "roic": roic(f),
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
    if np.isfinite(dil) and dil > 0.05:
        fails.append("HG-09")
    return fails
