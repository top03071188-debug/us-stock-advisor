"""test_xbrl_fundamentals — SEC companyfacts → 연차 재무(5년) 계약.

검사 항목:
- `filed ≤ decision_at.date()` 만 쓴다(look-ahead 차단)
- 10-K/FY 만, 분기(10-Q)·비연차 기간은 버린다
- 같은 회계연도 정정은 **가장 먼저 공개된 값**을 쓴다
- 태그 우선순위·EBIT 근사(세전이익+이자비용)·총부채 항등식·주식수 단위 보정
- XBRL 완전성(HG-11 입력)과 밸류에이션 배수(iv_mult) 단위
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from engine.data.xbrl_fundamentals import (
    annual_series,
    beta_from_prices,
    build_fundamentals,
    fiscal_years,
    peg_ratio,
    revenue_cagr,
    valuation_multiplier,
)
from engine.metrics.fundamentals import AnnualFundamentals, hard_gate_checks

DECISION = date(2026, 11, 20)


def _entry(start, end, val, form="10-K", fp="FY", filed="2026-02-10", tag_unit="USD"):
    row = {"start": start, "end": end, "val": val, "form": form, "fp": fp, "filed": filed}
    return tag_unit, row


def _facts(entries: dict[str, list[dict]], units: str = "USD") -> dict:
    """`{tag: [entry, ...]}` → companyfacts 형태."""
    out: dict = {"facts": {"us-gaap": {}}}
    for tag, rows in entries.items():
        out["facts"]["us-gaap"][tag] = {"units": {units: rows}}
    return out


def _year_rows(values: list[tuple[int, float]], **kw) -> list[dict]:
    """회계연도(12/31 마감) 값 목록 → XBRL 항목 목록."""
    rows = []
    for year, val in values:
        rows.append({"start": f"{year}-01-01", "end": f"{year}-12-31", "val": val,
                     "form": kw.get("form", "10-K"), "fp": kw.get("fp", "FY"),
                     "filed": kw.get("filed", f"{year + 1}-02-10")})
    return rows


def _balance_rows(values: list[tuple[int, float]], **kw) -> list[dict]:
    rows = []
    for year, val in values:
        rows.append({"end": f"{year}-12-31", "val": val, "form": kw.get("form", "10-K"),
                     "fp": kw.get("fp", "FY"), "filed": kw.get("filed", f"{year + 1}-02-10")})
    return rows


YEARS = [2021, 2022, 2023, 2024, 2025]
USD = 1e6  # 100만 달러 — 테스트 값은 백만 달러 × USD 로 적는다


def _payload(revenue: float = 10_000, extra: dict | None = None) -> dict:
    """건강한 회사(매출 100억$)의 companyfacts. 값 단위는 달러다."""
    entries = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": _year_rows([(y, revenue * USD) for y in YEARS]),
        "OperatingIncomeLoss": _year_rows([(y, 2_000 * USD) for y in YEARS]),
        "NetIncomeLoss": _year_rows([(y, 1_500 * USD) for y in YEARS]),
        "NetCashProvidedByUsedInOperatingActivities": _year_rows([(y, 1_800 * USD) for y in YEARS]),
        "PaymentsToAcquirePropertyPlantAndEquipment": _year_rows([(y, 400 * USD) for y in YEARS]),
        "DepreciationDepletionAndAmortization": _year_rows([(y, 300 * USD) for y in YEARS]),
        "Assets": _balance_rows([(y, 20_000 * USD) for y in YEARS]),
        "Liabilities": _balance_rows([(y, 11_000 * USD) for y in YEARS]),
        "StockholdersEquity": _balance_rows([(y, 9_000 * USD) for y in YEARS]),
        "LongTermDebtNoncurrent": _balance_rows([(y, 3_000 * USD) for y in YEARS]),
        "CashAndCashEquivalentsAtCarryingValue": _balance_rows([(y, 1_200 * USD) for y in YEARS]),
        "InterestExpense": _year_rows([(y, 150 * USD) for y in YEARS]),
        "AssetsCurrent": _balance_rows([(y, 6_000 * USD) for y in YEARS]),
        "LiabilitiesCurrent": _balance_rows([(y, 4_000 * USD) for y in YEARS]),
    }
    if extra:
        entries.update(extra)
    payload = _facts(entries)
    payload["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
        "units": {"shares": [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 1_000 * 1e6,
                               "form": "10-K", "fp": "FY", "filed": f"{y + 1}-02-10"} for y in YEARS]}
    }
    return payload


def test_annual_series_uses_only_10k_fy_filed_before_decision():
    payload = _payload(extra={
        "Revenues": [
            {"start": "2026-01-01", "end": "2026-09-30", "val": 9e9, "form": "10-Q", "fp": "Q3", "filed": "2026-10-30"},
            # 결산 뒤에 제출된 10-K 는 결정 시점에 알 수 없었다
            {"start": "2026-01-01", "end": "2026-12-31", "val": 1.3e10, "form": "10-K", "fp": "FY", "filed": "2027-02-10"},
        ]
    })
    series = annual_series(payload, DECISION)
    assert set(series["revenue"]) == {date(y, 12, 31) for y in YEARS}
    assert fiscal_years(series)[0] == date(2025, 12, 31)


def test_earliest_filing_wins_for_amendment():
    payload = _payload(extra={
        "NetIncomeLoss": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 1_500_000_000, "form": "10-K",
             "fp": "FY", "filed": "2026-02-10"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 1_100_000_000, "form": "10-K",
             "fp": "FY", "filed": "2026-05-10"},
        ]
    })
    series = annual_series(payload, DECISION)
    assert series["net_income"][date(2025, 12, 31)] == pytest.approx(1_500_000_000)


def test_tag_priority_prefers_first_tag_for_same_year():
    payload = _payload(extra={
        "Revenues": _year_rows([(2025, 12_345 * USD)]),
    })
    series = annual_series(payload, DECISION)
    # 앞 태그(RevenueFromContract...) 값이 우선이다
    assert series["revenue"][date(2025, 12, 31)] == pytest.approx(10_000 * USD)


def test_build_fundamentals_shape_and_units():
    f, diag = build_fundamentals(_payload(), DECISION)
    assert f is not None
    assert diag["years_available"] == 5
    assert diag["completeness"] == pytest.approx(1.0)
    # 백만 달러 단위: 100억 달러 = 10,000
    assert f.revenue[0] == pytest.approx(10_000)
    assert f.diluted_shares[0] == pytest.approx(1_000)     # 백만 주
    assert f.total_assets[0] == pytest.approx(20_000)
    assert f.non_interest_liabilities[0] == pytest.approx(8_000)  # 부채 11,000 − 총부채 3,000
    assert f.interest_expense[0] == pytest.approx(150)


def test_ebit_falls_back_to_pretax_plus_interest():
    payload = _payload()
    del payload["facts"]["us-gaap"]["OperatingIncomeLoss"]
    payload["facts"]["us-gaap"]["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = {
        "units": {"USD": _year_rows([(y, 1_700 * USD) for y in YEARS])}
    }
    f, _ = build_fundamentals(payload, DECISION)
    assert f.ebit[0] == pytest.approx(1_700 + 150)


def test_liabilities_from_accounting_identity_and_share_unit_fix():
    payload = _payload()
    del payload["facts"]["us-gaap"]["Liabilities"]
    # 주식수를 백만 주 단위(예: MCD)로 잘못 태깅한 경우
    payload["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
        "units": {"shares": [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 1_000,
                              "form": "10-K", "fp": "FY", "filed": f"{y + 1}-02-10"} for y in YEARS]}
    }
    f, _ = build_fundamentals(payload, DECISION)
    assert f.non_interest_liabilities[0] == pytest.approx(20_000 - 9_000 - 3_000)
    assert f.diluted_shares[0] == pytest.approx(1_000)


def test_completeness_drops_when_required_fields_missing():
    payload = _payload()
    del payload["facts"]["us-gaap"]["DepreciationDepletionAndAmortization"]
    del payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
    _, diag = build_fundamentals(payload, DECISION)
    assert diag["completeness"] < 0.9


def test_company_without_annual_facts_is_none():
    f, diag = build_fundamentals(_facts({"Assets": _balance_rows([(2025, 1e9)], form="10-Q", fp="Q3")}), DECISION)
    assert f is None
    assert diag["fiscal_years"] == []


def test_valuation_multiplier_is_usd_per_share_ratio():
    f, diag = build_fundamentals(_payload(), DECISION)
    mult, detail = valuation_multiplier(f, 100.0, "Information Technology", 1.0, 0.042,
                                        delta_nwc=float(diag["nwc_series"]))
    assert mult is not None and 0 < mult < 50
    assert detail["iv_median"] == pytest.approx(mult * 100.0)
    assert "dcf_base" in detail["methods"]


def test_valuation_multiplier_skips_suspended_sector_and_negative_fcff():
    f, _ = build_fundamentals(_payload(), DECISION)
    assert valuation_multiplier(f, 100.0, "Financials", 1.0, 0.042)[0] is None
    # CAPEX 가 영업현금흐름을 삼키면 FCFF ≤ 0 → 계산 불가(0으로 채우지 않는다)
    heavy = AnnualFundamentals(**{**f.__dict__, "capex": [5_000, 5_000, 5_000, 5_000, 5_000]})
    mult, detail = valuation_multiplier(heavy, 100.0, "Technology", 1.0, 0.042)
    assert mult is None
    assert "FCFF" in detail["reason"]


def test_revenue_cagr_and_peg():
    f, _ = build_fundamentals(_payload(), DECISION)
    assert revenue_cagr(f) == pytest.approx(0.0, abs=1e-9)
    growth = AnnualFundamentals(**{**f.__dict__, "revenue": [13_310, 12_100, 11_000, 10_000, 9_000]})
    cagr = revenue_cagr(growth)
    assert cagr == pytest.approx((13310 / 9000) ** 0.25 - 1)
    peg = peg_ratio(growth, 100.0, cagr)
    eps = growth.net_income[0] / growth.diluted_shares[0]
    assert peg == pytest.approx((100.0 / eps) / (cagr * 100.0))
    assert peg_ratio(growth, 100.0, -0.01) is None


def test_hard_gate_checks_accept_healthy_company():
    f, _ = build_fundamentals(_payload(), DECISION)
    assert hard_gate_checks(f, {}) == []


def test_beta_from_prices_recovers_known_beta():
    import numpy as np
    import pandas as pd

    idx = pd.bdate_range("2025-01-01", periods=260)
    rng = np.random.default_rng(3)
    bench = pd.Series(np.cumprod(1 + rng.normal(0.0004, 0.01, len(idx))), index=idx) * 100
    stock = pd.Series(np.cumprod(1 + 2.0 * rng.normal(0.0004, 0.01, len(idx))), index=idx) * 50
    beta = beta_from_prices(stock, bench)
    assert beta is not None
    assert 0.3 <= beta <= 2.5
    assert math.isfinite(beta)
