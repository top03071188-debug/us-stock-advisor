"""test_valuation_normalized — 5년 정상화·단위·순부채·PEG (R03).

GOOGL 공시(`tests/fixtures/googl_companyfacts.json`, SEC companyfacts 축약)로 실제 숫자를 고정한다.
- FCFF·NOPAT 는 **최근 5년 중앙값**(§3.2.3) — 최신 연도 값 하나를 쓰지 않는다
- ΔNWC 는 **영업 기준**(현금·시장성 단기투자 제외)이고 **백만 달러 단위**로 5% 상한을 건다
- 순부채 = 이자부채 − 현금 − **시장성 단기투자**
- PEG = PER(**TTM EPS**) ÷ 5년 **EPS CAGR**
- Q 점수 경로(`qc_wacc`)와 가치평가 경로(`valuation_wacc`)의 WACC 는 이름부터 다르다
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from engine.data.xbrl_fundamentals import (
    build_fundamentals,
    delta_nwc_series,
    eps_cagr,
    fcff_cagr,
    operating_nwc,
    peg_basis,
    peg_growth,
    peg_ratio,
    revenue_cagr,
    valuation_multiplier,
    valuation_wacc,
)
from engine.metrics.fundamentals import AnnualFundamentals, qc_wacc

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "googl_companyfacts.json"
DECISION = date(2026, 11, 20)
PRICE = 272.66


@pytest.fixture(scope="module")
def googl():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    f, diag = build_fundamentals(payload, DECISION)
    assert f is not None
    return f, diag, payload


def test_collects_six_years_but_scores_five(googl):
    """ΔNWC 5년 시계열을 만들려면 6년 운전자본이 필요하다 — 점수·게이트는 5년 기준."""
    _, diag, _ = googl
    assert len(diag["fiscal_years"]) == 6
    assert len(diag["delta_nwc_series"]) == 5
    assert diag["completeness"] == pytest.approx(0.9778)   # 앞 5년 × 필수 9항목


def test_googl_operating_nwc_excludes_cash_and_marketable(googl):
    """2025 Δ(CA−CL)=+28,704 는 현금·시장성 증권이 만든 변화 — 영업 ΔNWC 는 −2,482 다."""
    f, diag, _ = googl
    assert diag["marketable_securities"][0] == pytest.approx(96_135.0)
    assert f.operating_nwc[0] == pytest.approx(-23_550.0)      # 206,038−30,708−96,135−102,745
    assert f.delta_nwc[0] == pytest.approx(-2_482.0, abs=1.0)
    # 현금·시장성만 바뀌면 영업 NWC 는 그대로다(금융항목 제외 확인)
    ca, cl = [23_550.0 * 0 + 100.0], [0.0]
    assert operating_nwc([500.0], [200.0], [100.0], [50.0]) == [150.0]
    assert operating_nwc([650.0], [200.0], [250.0], [50.0]) == [150.0]   # 현금 +150, CA +150
    assert operating_nwc(ca, cl, [0.0], [0.0]) == [100.0]


def test_nwc_cap_uses_same_units():
    """ΔNWC 는 **백만 달러** 단위에서 매출 5% 로 자른다 — 단위가 섞이면 상한이 사라진다."""
    capped = delta_nwc_series([29_704.0, 1_000.0], [402_836.0])
    assert capped[0] == pytest.approx(0.05 * 402_836.0)        # 20,141.8
    negative = delta_nwc_series([-29_704.0, 0.0], [402_836.0])
    assert negative[0] == pytest.approx(-0.05 * 402_836.0)
    small = delta_nwc_series([1_100.0, 1_000.0], [402_836.0])
    assert small[0] == pytest.approx(100.0)                     # 상한 이하이면 그대로


def test_googl_normalized_fcff_and_nopat(googl):
    f, diag, _ = googl
    mult, det = valuation_multiplier(f, PRICE, "Information Technology", 1.0, 0.0437)
    assert mult is not None
    # 최신 연도 EBIT 만 쓰면 FCFF 40,538 — 5년 중앙값은 48,265 다
    assert det["fcff_years"][0] == pytest.approx(40_537.95, abs=0.1)
    assert det["fcff_normalized"] == pytest.approx(48_265.02, abs=0.1)
    assert det["fcff_normalized"] == pytest.approx(float(np.median(det["fcff_years"])))
    assert det["ebit_normalized"] == pytest.approx(84_293.0)                 # 5년 중앙값
    assert det["nopat_normalized"] == pytest.approx(70_789.26, abs=0.1)      # × (1−0.1602)
    assert det["tax_rate"] == pytest.approx(0.1602)
    assert diag["fiscal_years"][0] == "2025-12-31"


def test_googl_growth_is_fcff_cagr_clamped(googl):
    f, _, _ = googl
    _, det = valuation_multiplier(f, PRICE, "Information Technology", 1.0, 0.0437)
    assert det["growth_basis"] == "fcff_cagr"
    assert 0.0 <= det["growth"] <= 0.15
    assert det["growth"] == pytest.approx(max(0.0, min(0.15, fcff_cagr(f))))


def test_googl_net_debt_includes_marketable_securities(googl):
    """시장성 단기투자 96,135 를 빼면 순부채가 음수(순현금)로 뒤집힌다."""
    f, _, _ = googl
    _, det = valuation_multiplier(f, PRICE, "Information Technology", 1.0, 0.0437)
    assert det["net_debt"] == pytest.approx(-78_300.0)        # 48,543 − 30,708 − 96,135
    no_marketable = AnnualFundamentals(**{**f.__dict__, "marketable_securities": [0.0] * len(f.cash)})
    _, det2 = valuation_multiplier(no_marketable, PRICE, "Information Technology", 1.0, 0.0437)
    assert det2["net_debt"] == pytest.approx(17_835.0)        # 48,543 − 30,708
    assert det2["iv_median"] < det["iv_median"]               # 순부채가 크면 주당 가치는 낮다


def test_googl_iv_components_and_median_method(googl):
    f, _, _ = googl
    _, det = valuation_multiplier(f, PRICE, "Information Technology", 1.0, 0.0437)
    assert det["iv_epv"] == pytest.approx(73.981, abs=1e-3)
    assert det["iv_graham"] == pytest.approx(90.864, abs=1e-3)      # √(22.5×10.807×33.9546)
    assert det["iv_median"] == pytest.approx(det["iv_epv"])
    assert det["median_method"] == "epv"
    assert set(det["methods"]) == {"dcf_base", "epv", "graham"}
    assert det["v_confidence"] == "B"
    assert det["wacc"] == pytest.approx(0.085651, abs=1e-6)
    assert det["iv_band_basis"] == "bear_bull"
    assert det["iv_low"] < det["iv_median"] < det["iv_high"]


def test_wacc_paths_are_named_apart(googl):
    """Q 점수 WACC(β=1.0·rf 고정)와 가치평가 WACC(회귀 β·DGS10)는 다른 값이다 — 같은 이름 금지."""
    f, _, _ = googl
    qc = qc_wacc(f)
    valuation = valuation_wacc(f, 1.15, 0.0437, 0.02)
    assert np.isfinite(qc) and np.isfinite(valuation)
    assert abs(valuation - qc) > 0.005
    assert valuation == pytest.approx(0.092366, abs=1e-5)


def test_peg_uses_ttm_eps_and_eps_cagr(googl):
    """PER 은 TTM EPS, 성장률은 EPS CAGR — 매출 CAGR 과 섞지 않는다."""
    f, diag, _ = googl
    assert diag["ttm_eps"] == pytest.approx(19.2718, abs=1e-3)
    assert peg_basis(f) == "ttm"
    growth, basis = peg_growth(f)
    # GOOGL 은 FY2021 주식수가 공시에 없어 EPS CAGR 을 만들 수 없다 → 매출 CAGR 폴백을 **표시**한다
    assert basis in ("eps_cagr", "revenue_cagr_fallback")
    assert growth is not None
    assert peg_ratio(f, PRICE, growth) == pytest.approx((PRICE / f.ttm_eps) / (growth * 100.0))
    assert eps_cagr(f) is None
    assert revenue_cagr(f) == pytest.approx(0.118227, abs=1e-5)
    # 성장률을 명시적으로 주면 그 값을 쓴다(§3.2.4 정렬 확인)
    eps_growth = 0.15
    assert peg_ratio(f, PRICE, eps_growth) == pytest.approx((PRICE / f.ttm_eps) / (eps_growth * 100.0))


def test_peg_falls_back_to_fy_eps_and_uses_eps_cagr():
    """TTM EPS 가 없으면 FY EPS 로 폴백하고, 성장률은 EPS CAGR 을 쓴다(매출 CAGR 아님)."""
    f = AnnualFundamentals(
        ebit=[100.0] * 5, revenue=[100.0, 95.0, 90.0, 85.0, 80.0],
        net_income=[100.0, 90.0, 80.0, 70.0, 60.0], ocf=[100.0] * 5, capex=[10.0] * 5,
        depreciation=[5.0] * 5, total_assets=[500.0] * 5, non_interest_liabilities=[100.0] * 5,
        excess_cash=[10.0] * 5, total_debt=[50.0] * 5, cash=[50.0] * 5, equity=[400.0] * 5,
        diluted_shares=[10.0] * 5, interest_expense=[2.0] * 5,
    )
    growth, basis = peg_growth(f)
    assert basis == "eps_cagr"
    assert growth == pytest.approx((10.0 / 6.0) ** 0.25 - 1)     # EPS 10 → 6
    assert peg_basis(f) == "fy_fallback"
    assert peg_ratio(f, 200.0, growth) == pytest.approx((200.0 / 10.0) / (growth * 100.0))


def _small_company(extra: dict | None = None) -> dict:
    """시장성 단기투자 태그 유무만 다른 최소 companyfacts(5년)."""
    years = ["2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31", "2026-12-31"]

    def annual(end: str, val: float) -> dict:
        return {"start": f"{int(end[:4])}-01-01", "end": end, "val": val, "form": "10-K",
                "fp": "FY", "filed": f"{int(end[:4]) + 1}-02-10", "accn": f"A-{end}"}

    def instant(end: str, val: float) -> dict:
        return {"end": end, "val": val, "form": "10-K", "fp": "FY",
                "filed": f"{int(end[:4]) + 1}-02-10", "accn": f"A-{end}"}

    entries = {
        "Revenues": [annual(e, 1_000.0 * 1e6) for e in years],
        "OperatingIncomeLoss": [annual(e, 200.0 * 1e6) for e in years],
        "NetIncomeLoss": [annual(e, 150.0 * 1e6) for e in years],
        "NetCashProvidedByUsedInOperatingActivities": [annual(e, 180.0 * 1e6) for e in years],
        "PaymentsToAcquirePropertyPlantAndEquipment": [annual(e, 40.0 * 1e6) for e in years],
        "DepreciationDepletionAndAmortization": [annual(e, 30.0 * 1e6) for e in years],
        "Assets": [instant(e, 2_000.0 * 1e6) for e in years],
        "Liabilities": [instant(e, 1_100.0 * 1e6) for e in years],
        "StockholdersEquity": [instant(e, 900.0 * 1e6) for e in years],
        "CashAndCashEquivalentsAtCarryingValue": [instant(e, 100.0 * 1e6) for e in years],
        "AssetsCurrent": [instant(e, 600.0 * 1e6) for e in years],
        "LiabilitiesCurrent": [instant(e, 400.0 * 1e6) for e in years],
        "WeightedAverageNumberOfDilutedSharesOutstanding": [
            {"start": f"{int(e[:4])}-01-01", "end": e, "val": 100 * 1e6, "form": "10-K",
             "fp": "FY", "filed": f"{int(e[:4]) + 1}-02-10", "accn": f"A-{e}"} for e in years],
    }
    entries.update(extra or {})
    out: dict = {"facts": {"us-gaap": {}}}
    for tag, rows in entries.items():
        unit = "shares" if tag.startswith("WeightedAverage") else "USD"
        out["facts"]["us-gaap"][tag] = {"units": {unit: rows}}
    return out


def test_marketable_falls_back_to_cash_plus_short_term_investments():
    """`MarketableSecuritiesCurrent` 가 없으면 `현금+단기투자` − 현금으로 대체한다."""
    years = ["2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31", "2026-12-31"]
    agg = [{"end": e, "val": 250.0 * 1e6, "form": "10-K", "fp": "FY",
            "filed": f"{int(e[:4]) + 1}-02-10", "accn": f"A-{e}"} for e in years]
    f, diag = build_fundamentals(_small_company({"CashCashEquivalentsAndShortTermInvestments": agg}), DECISION)
    assert f is not None
    assert f.marketable_securities[0] == pytest.approx(150.0)
    assert "nwc_no_marketable" not in diag


def test_missing_marketable_tag_is_flagged():
    """두 태그가 모두 없으면 0 으로 보고 **진단에 남긴다**(조용한 0 금지)."""
    f, diag = build_fundamentals(_small_company(), DECISION)
    assert f is not None
    assert np.isnan(f.marketable_securities[0])
    assert diag["nwc_no_marketable"] is True
