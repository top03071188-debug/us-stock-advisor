"""test_valuation_penalties_and_10y — v1.3.2 밸류에이션 고도화.

검사 항목:
- `v_score` 완만 감쇠 곡선(0 / −0.20 / −0.40 / −1.0 / −2.0 / −3.5)과 감점 반영
- DCF 민감도 `abs(bull−bear)/(2×base) ≥ 0.40` → V 신뢰도 C + `v_penalty_dcf = −4`
- 10년 `EV/EBIT` 백분위 — **룩어헤드 차단**(`available_at ≤ t` 이전 재무만) + 20% 괴리 → `v_penalty_hist = −10`
- S1 과열(밸류) `ev_ebit_percentile ≥ 90` 발동
- 1층(스크리너)과 2층(파이프라인)의 `v_penalties`·V 점수 일치
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from engine.data.prices import build_price_frame
from engine.data.xbrl_fundamentals import (
    ev_ebit_history,
    historical_iv,
    valuation_multiplier,
)
from engine.decide.pipeline import build_views
from engine.decide.signals import EntryContext, evaluate_exit
from engine.metrics.fundamentals import AnnualFundamentals
from engine.screener import Candidate, score_candidate, screen
from engine.metrics.valuation import (
    DCF_SENSITIVITY_MAX,
    V_PENALTY_DCF,
    V_PENALTY_HIST,
    dcf_sensitivity,
    v_score,
)
from engine.utils.dates import bar_available_at
from scripts.run_screen import build_candidates
from tests.helpers import example_inputs  # noqa: F401

AS_OF = date(2026, 11, 20)
PRICE = 100.0


# ───────────────────────────── v_score 완만 감쇠 ─────────────────────────────
def test_v_score_slow_decay_curve():
    assert v_score(0.0) == pytest.approx(50.0)
    assert v_score(0.30) == pytest.approx(100.0)
    assert v_score(0.15) == pytest.approx(75.0)
    # −20% 까지는 예전과 같은 기울기(2.5점/%p)지만 끝점이 0 이 아니라 15 다.
    assert v_score(-0.10) == pytest.approx(32.5)
    assert v_score(-0.20) == pytest.approx(15.0)
    # −20% 아래는 완만한 선형(8.33점/%p) — −200% 에서 0
    assert v_score(-0.40) == pytest.approx(15.0 - 0.20 * (15.0 / 1.80))
    assert v_score(-1.00) == pytest.approx(15.0 - 0.80 * (15.0 / 1.80))
    assert v_score(-2.00) == pytest.approx(0.0)
    assert v_score(-3.50) == pytest.approx(0.0)          # 클램프
    # 변별력: ADBE(−40%)와 GOOGL(−349%)이 같은 0점으로 뭉개지지 않는다
    assert v_score(-0.40) > v_score(-3.49) == 0.0


def test_v_score_penalties_are_subtracted_and_clamped():
    assert v_score(0.21, penalties=V_PENALTY_HIST + V_PENALTY_DCF) == pytest.approx(
        50.0 + 0.21 * (50.0 / 0.30) - 14.0)
    assert v_score(-0.30, penalties=V_PENALTY_HIST) == pytest.approx(
        v_score(-0.30) - 10.0)
    assert v_score(0.0, penalties=99.0) == pytest.approx(0.0)


def test_dcf_sensitivity_is_one_side_relative_swing():
    assert dcf_sensitivity(80.0, 120.0, 100.0) == pytest.approx(0.20)
    assert dcf_sensitivity(120.0, 80.0, 100.0) == pytest.approx(0.20)   # 부호 무관
    assert dcf_sensitivity(40.0, 200.0, 100.0) == pytest.approx(0.80)
    assert dcf_sensitivity(1.0, 2.0, 0.0) == float("inf")


# ───────────────────────────── 회사 헬퍼 ─────────────────────────────
def _company(ebit0: float = 1_200.0, ratio: float = 0.95, debt: float = 1_000.0,
             cash: float = 800.0, shares: float = 100.0, ocf: float = 500.0,
             capex: float = 600.0) -> AnnualFundamentals:
    """최신순(내림차순) EBIT 5년 — 지표 규약과 같다."""
    ebit = [ebit0]
    for _ in range(4):
        ebit.append(ebit[-1] * ratio)
    return AnnualFundamentals(
        ebit=ebit, revenue=[5_000.0] * 5, net_income=[300.0] * 5, ocf=[ocf] * 5,
        capex=[capex] * 5, depreciation=[200.0] * 5, total_assets=[6_000.0] * 5,
        non_interest_liabilities=[2_000.0] * 5, excess_cash=[100.0] * 5,
        total_debt=[debt] * 5, cash=[cash] * 5, equity=[3_000.0] * 5,
        diluted_shares=[shares] * 5, interest_expense=[40.0] * 5, tax_rate=0.21,
        marketable_securities=[300.0] * 5,
    )


def _frame(start: str, end: str, price: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, end)
    return pd.DataFrame({
        "sadj_close": price, "raw_close": price,
        "available_at": [bar_available_at(d.date()) for d in idx],
    }, index=idx)


# ─────────────────── DCF 민감도 → 신뢰도 C + −4점 ───────────────────
def test_high_dcf_sensitivity_downgrades_confidence_and_penalises():
    # 레버리지가 큰 회사: 지분가치가 EV 대비 작아 성장률 가정에 IV 가 크게 흔들린다.
    mult, detail = valuation_multiplier(_company(ratio=0.85, debt=8_000.0, cash=800.0),
                                        PRICE, "Information Technology", 0.5, 0.042)
    assert mult is not None
    assert detail["dcf_sensitivity"] >= DCF_SENSITIVITY_MAX
    assert detail["v_confidence"] == "C"
    assert detail["v_confidence_reason"] == "DCF_HIGH_SENSITIVITY"
    assert detail["v_penalty_dcf"] == pytest.approx(V_PENALTY_DCF)
    assert detail["v_penalties"] >= V_PENALTY_DCF


def test_low_dcf_sensitivity_keeps_confidence():
    mult, detail = valuation_multiplier(_company(ratio=1.0), PRICE,
                                        "Information Technology", 1.0, 0.042)
    assert mult is not None
    assert detail["dcf_sensitivity"] == pytest.approx(0.0)
    assert detail["v_confidence"] != "C"
    assert detail["v_penalty_dcf"] == pytest.approx(0.0)
    assert detail["v_penalties"] == pytest.approx(0.0)


# ─────────────────── 10년 EV/EBIT 백분위 · 룩어헤드 ───────────────────
def _filing(fy: int, filed: str, ebit: float, shares_m: float = 10.0,
            debt: float = 0.0, cash: float = 0.0) -> dict:
    """한 회계연도의 10-K 항목 묶음(달러·주 단위)."""
    flow = {"start": f"{fy}-01-01", "end": f"{fy}-12-31", "form": "10-K", "fp": "FY", "filed": filed}
    inst = {"end": f"{fy}-12-31", "form": "10-K", "fp": "FY", "filed": filed}
    return {
        "OperatingIncomeLoss": {**flow, "val": ebit * 1e6},
        "WeightedAverageNumberOfDilutedSharesOutstanding": {**flow, "val": shares_m * 1e6},
        "LongTermDebtNoncurrent": {**inst, "val": debt * 1e6},
        "CashAndCashEquivalentsAtCarryingValue": {**inst, "val": cash * 1e6},
    }


def _payload(filings: list[dict]) -> dict:
    """`{tag: {start/end/val/form/fp/filed}}` 목록 → companyfacts 형태."""
    out: dict = {"facts": {"us-gaap": {}}}
    for filing in filings:
        for tag, row in filing.items():
            block = out["facts"]["us-gaap"].setdefault(
                tag, {"units": {"shares" if "Shares" in tag else "USD": []}})
            unit = "shares" if "Shares" in tag else "USD"
            block["units"].setdefault(unit, []).append(row)
    return out


LOOK_AHEAD = _payload([
    # FY2019 10-K 는 2020-02-21(봉 마감 21:00Z) 이후 23:59Z 에 접수됐다
    _filing(2018, "2019-02-15", ebit=40.0),          # 1000 / 40 = 25배
    _filing(2019, "2020-02-21", ebit=50.0),          # 1000 / 50 = 20배
    _filing(2020, "2021-02-09", ebit=100.0),         # 1000 / 100 = 10배
])


def test_ev_ebit_history_blocks_look_ahead_by_acceptance():
    """FY2019 10-K 가 접수되기 전 스냅샷은 **FY2018** 재무를 쓰고, 접수된 뒤에야 바뀐다."""
    frame = _frame("2019-06-03", "2021-06-30")
    hist = ev_ebit_history(LOOK_AHEAD, frame, date(2021, 6, 30), include_samples=True)
    rows = hist["sample_rows"]
    assert rows

    before = [r for r in rows if r["date"] < "2020-02-21"]
    assert before and all(r["fy"] == "2018-12-31" for r in before)
    assert all(r["ev_ebit"] == pytest.approx(25.0) for r in before)

    # 접수 당일 봉(마감 21:00Z)보다 늦은 23:59Z 접수 — 그날 표본은 아직 FY2018 이다
    same_day = next((r for r in rows if r["date"] == "2020-02-21"), None)
    assert same_day is not None and same_day["fy"] == "2018-12-31"

    after = next((r for r in rows if r["date"] > "2020-02-21"), None)
    assert after is not None and after["fy"] == "2019-12-31"
    assert after["ev_ebit"] == pytest.approx(20.0)

    last = rows[-1]
    assert last["fy"] == "2020-12-31"
    assert last["ev_ebit"] == pytest.approx(10.0)


def test_ev_ebit_history_requires_three_years():
    """유효 회계연도 3년 미만이면 계산하지 않는다(감점 0) — 6~9년차 종목은 그 이력으로 계산."""
    frame = _frame("2019-06-03", "2021-06-30")
    two = _payload([_filing(2018, "2019-02-15", ebit=40.0),
                    _filing(2019, "2020-02-21", ebit=50.0)])
    hist = ev_ebit_history(two, frame, date(2021, 6, 30))
    assert hist["reason"] == "insufficient_history"
    assert hist["ev_ebit_percentile"] is None
    assert hist["ev_ebit_years"] == 2
    assert ev_ebit_history(LOOK_AHEAD, frame, date(2021, 6, 30))["reason"] is None


def test_ev_ebit_percentile_and_median():
    """현재 배수가 10년 분포의 어디에 있는지(0~100)와 중앙값을 낸다."""
    hist = ev_ebit_history(LOOK_AHEAD, _frame("2019-06-03", "2021-06-30"), date(2021, 6, 30))
    assert hist["reason"] is None
    assert hist["ev_ebit_years"] == 3
    assert hist["ev_ebit_current"] == pytest.approx(100.0 * 10.0 / 100.0)
    # 현재(10배)가 표본 중 가장 낮은 구간 → 백분위가 낮다
    assert 0.0 <= hist["ev_ebit_percentile"] <= 34.0
    assert hist["ev_ebit_median_10y"] >= hist["ev_ebit_current"]
    assert hist["ev_ebit_window"] and hist["ev_ebit_samples"] >= 12


def test_ev_ebit_history_none_without_prices():
    hist = ev_ebit_history(LOOK_AHEAD, None, date(2021, 6, 30))
    assert hist["reason"] == "insufficient_history"


def test_historical_iv_formula():
    assert historical_iv(12.0, 100.0, 200.0, 10.0) == pytest.approx((12.0 * 100.0 - 200.0) / 10.0)
    assert historical_iv(0.0, 100.0, 0.0, 10.0) is None


# ─────────────────── 20% 괴리 → −10점 ───────────────────
def test_historical_divergence_penalty():
    f = _company(ratio=1.0, debt=1_000.0, cash=800.0)     # 순부채 −100
    mult, detail = valuation_multiplier(f, PRICE, "Information Technology", 1.0, 0.042)
    iv_median = detail["iv_median"]
    ebit_norm = detail["ebit_normalized"]
    net_debt = detail["net_debt"]
    shares = detail["diluted_shares"]

    def ratio_for(target: float) -> float:
        return (target * shares + net_debt) / ebit_norm

    # IV 와 약 10% 차이 — 감점 없음 (괴리는 iv_hist 로 나누므로 0.10/0.90 = 0.111)
    near = valuation_multiplier(f, PRICE, "Information Technology", 1.0, 0.042,
                                hist={"ev_ebit_median_10y": ratio_for(iv_median * 0.90),
                                      "ev_ebit_percentile": 55.0, "reason": None})[1]
    assert near["hist_divergence"] == pytest.approx(0.1111, abs=0.01)
    assert near["hist_divergence"] < 0.20
    assert near["v_penalty_hist"] == pytest.approx(0.0)

    # IV 와 25% 차이 — −10점
    far = valuation_multiplier(f, PRICE, "Information Technology", 1.0, 0.042,
                               hist={"ev_ebit_median_10y": ratio_for(iv_median * 1.30),
                                     "ev_ebit_percentile": 55.0, "reason": None})[1]
    assert far["hist_divergence"] >= 0.20
    assert far["v_penalty_hist"] == pytest.approx(V_PENALTY_HIST)
    assert far["v_penalties"] == pytest.approx(V_PENALTY_HIST)
    assert far["iv_hist_ev_ebit"] == pytest.approx(iv_median * 1.30, rel=1e-3)


# ─────────────────── S1 과열(밸류) 백분위 ───────────────────
def _ctx(**kw) -> EntryContext:
    base = dict(ticker="TEST", raw_close=100.0, sadj_close=100.0, iv_median=110.0,
                dma50=99.0, dma200=95.0, dma200_slope=0.01, rsi14=60.0, pct_b=0.6)
    base.update(kw)
    return EntryContext(**base)


def test_s1_triggers_on_ev_ebit_percentile_90():
    hot = evaluate_exit(_ctx(ev_ebit_percentile=95.0), {"shares": 1, "avg_cost": 100.0})
    assert hot["triggers"]["S1"] is True
    assert hot["verdict"] in ("TRIM", "EXIT", "WATCH_DOWN", "HOLD")   # S1 이 exit_score 를 밀어 올린다
    assert hot["exit_score"] > 0

    cold = evaluate_exit(_ctx(ev_ebit_percentile=89.0), {"shares": 1, "avg_cost": 100.0})
    assert cold["triggers"]["S1"] is False

    none = evaluate_exit(_ctx(), {"shares": 1, "avg_cost": 100.0})
    assert none["triggers"]["S1"] is False


def test_s1_still_triggers_by_margin():
    hot = evaluate_exit(_ctx(iv_median=80.0), {"shares": 1, "avg_cost": 100.0})
    assert hot["triggers"]["S1"] is True          # margin −25% ≤ −15%


# ─────────────────── 1층 · 2층 v_penalties 일치 ───────────────────
def _dataset(penalties: float, margin: float = -0.40) -> dict:
    from engine.data.fixtures import synthetic_history

    hist = synthetic_history("FIXP", days=1300, end=date(2026, 11, 19), pattern="flat")
    panel = build_price_frame("FIXP", hist, None)
    price = float(panel.frame["sadj_close"].iloc[-1])
    iv = price / (1.0 - margin)
    detail = {"iv_median": iv, "iv_low": iv * 0.85, "iv_high": iv * 1.15,
              "iv_mult": iv / price, "v_penalties": penalties,
              "ev_ebit_percentile": 93.0, "dcf_sensitivity": 0.1,
              "v_penalty_dcf": 0.0, "v_penalty_hist": penalties,
              "v_confidence": "B", "iv_band_basis": "bear_bull"}
    return {
        "source": "live",
        "universe": [{"ticker": "FIXP", "sector": "Information Technology", "exchange": "NYSE",
                      "market_cap_usd": 2.4e12, "adv20_usd": 9e9, "history_rows": 1300,
                      "iv_mult": detail["iv_mult"], "v_penalties": penalties,
                      "ev_ebit_percentile": 93.0}],
        "panels": {"FIXP": panel},
        "fundamentals": {"FIXP": _company()},
        "valuation_inputs": {"FIXP": detail},
        "filings": {}, "news": {},
    }


def test_screener_and_pipeline_share_v_penalties():
    data = _dataset(penalties=V_PENALTY_HIST, margin=-0.40)
    cand = build_candidates(data)[0]
    assert cand.v_penalties == pytest.approx(V_PENALTY_HIST)
    floor = score_candidate(cand)
    assert floor["v"] == pytest.approx(v_score(cand.margin, V_PENALTY_HIST), abs=0.01)
    assert floor["v"] < v_score(cand.margin, 0.0)          # 감점이 실제로 반영된다

    view = build_views(data, AS_OF)[0]
    assert view.v == pytest.approx(floor["v"], abs=0.01)


def test_screen_uses_penalties_for_ranking():
    """1층 워치리스트 V 가 감점을 반영하지 않으면 순위가 틀어진다."""
    c_pen = Candidate(ticker="AAA", sector="Information Technology", market_cap_usd=5e11,
                      adv20_usd=1e9, price=100.0, history_rows=1300, exchange="NYSE",
                      margin=-0.30, v_penalties=V_PENALTY_HIST, fundamentals=_company(ocf=900.0, capex=400.0),
                      peg=1.2, revenue_cagr=0.10)
    c_free = Candidate(ticker="BBB", sector="Information Technology", market_cap_usd=5e11,
                       adv20_usd=1e9, price=100.0, history_rows=1300, exchange="NYSE",
                       margin=-0.30, v_penalties=0.0, fundamentals=_company(ocf=900.0, capex=400.0),
                       peg=1.2, revenue_cagr=0.10)
    result = screen([c_pen, c_free], sectors_max=6, watchlist_max=40)
    rows = {r["ticker"]: r for r in result["watchlist"]}
    assert rows["BBB"]["v"] - rows["AAA"]["v"] == pytest.approx(V_PENALTY_HIST, abs=0.01)
    assert result["watchlist"][0]["ticker"] == "BBB"        # 감점 없는 쪽이 앞선다


def test_candidate_ev_ebit_percentile_reaches_entry_context():
    data = _dataset(penalties=0.0, margin=-0.10)
    view = build_views(data, AS_OF)[0]
    assert view.valuation_detail.get("ev_ebit_percentile") == pytest.approx(93.0)
