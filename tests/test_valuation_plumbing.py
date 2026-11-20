"""test_valuation_plumbing — 방법별 IV·시나리오 밴드·근거 배관 (R04).

`engine.metrics.valuation.value_company` 는 이미 §3.2.3 대로 `iv_low/iv_high` 를 **계산**한다.
결함은 계산이 아니라 배관이었다: `collect_fundamentals` → `build_views` → 발행 레코드 → 화면까지
값이 사라지고 중앙값 ±15% 로 다시 만들어졌다. 이 파일은 **값이 유실되지 않는지**와
`IV 대비 프리미엄/할인` 부호, 근거의 `accn` 연결을 고정한다.
"""

from __future__ import annotations

import statistics
from datetime import date

import pytest

import pandas as pd

from engine.data.fixtures import synthetic_history
from engine.data.prices import build_price_frame
from engine.data.xbrl_fundamentals import valuation_multiplier
from engine.decide.pipeline import TickerView, _thesis, build_views
from engine.metrics.fundamentals import AnnualFundamentals

AS_OF = date(2026, 11, 20)
PRICE = 100.0


def _company() -> AnnualFundamentals:
    """DCF·EPV·Graham 이 서로 다른 값을 내고, **약세 DCF 가 중앙값을 바꾸는** 회사."""
    ebit0 = 1_200.0
    ebit = [ebit0, ebit0 * 0.95, ebit0 * 0.9, ebit0 * 0.85, ebit0 * 0.8]
    return AnnualFundamentals(
        ebit=ebit, revenue=[5_000.0] * 5, net_income=[300.0] * 5, ocf=[500.0] * 5,
        capex=[600.0] * 5, depreciation=[200.0] * 5, total_assets=[6_000.0] * 5,
        non_interest_liabilities=[2_000.0] * 5, excess_cash=[100.0] * 5,
        total_debt=[1_000.0] * 5, cash=[800.0] * 5, equity=[3_000.0] * 5,
        diluted_shares=[100.0] * 5, interest_expense=[40.0] * 5, tax_rate=0.21,
        marketable_securities=[300.0] * 5,
    )


def _detail() -> dict:
    _, detail = valuation_multiplier(_company(), PRICE, "Information Technology", 1.0, 0.042)
    return detail


def test_iv_band_comes_from_bear_bull():
    """`iv_low` 는 약세 DCF 쪽 중앙값 ×0.85 — 중앙값 ×0.85 로 다시 만들면 안 된다."""
    d = _detail()
    bear_set = [d["iv_dcf"]["bear"], d["iv_epv"], d["iv_graham"]]
    bull_set = [d["iv_dcf"]["bull"], d["iv_epv"], d["iv_graham"]]
    assert d["iv_low"] == pytest.approx(statistics.median(bear_set) * 0.85)
    assert d["iv_high"] == pytest.approx(statistics.median(bull_set) * 1.15)
    assert d["iv_low"] != pytest.approx(d["iv_median"] * 0.85)   # 과거 재구성값과 다르다
    assert d["iv_band_basis"] == "bear_bull"


def test_method_values_and_median_are_passed_through():
    d = _detail()
    assert set(d["methods"]) == {"dcf_base", "epv", "graham"}
    assert d["method_count"] == 3
    assert d["median_method"] == "epv"
    assert d["iv_median"] == pytest.approx(statistics.median(d["methods"].values()))
    assert d["iv_dcf"]["bear"] < d["iv_dcf"]["base"] < d["iv_dcf"]["bull"]
    assert d["iv_mult"] == pytest.approx(d["iv_median"] / PRICE)


def test_collect_fundamentals_keeps_every_method():
    """수집 단계가 방법별 IV·밴드·근거를 **버리지 않고** 넘긴다(과거엔 `reason` 만 남겼다)."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    from engine.data.collect import collect_fundamentals

    payload = json.loads((Path(__file__).resolve().parent / "fixtures" / "googl_companyfacts.json")
                         .read_text(encoding="utf-8"))

    class FakeSec:
        def company_facts(self, cik):
            assert cik == 1652044
            return payload

    hist = synthetic_history("GOOGL", days=1300, end=date(2026, 11, 19), pattern="flat")
    panel = build_price_frame("GOOGL", hist, None)
    diag = {"sources": {}, "degraded": []}
    fundamentals, valuation = collect_fundamentals(
        ["GOOGL"], datetime(2026, 11, 20, 2, 34, tzinfo=timezone.utc), diag,
        {"GOOGL": panel}, panel, 0.0437, {"GOOGL": "Information Technology"},
        client=FakeSec(), cik_by_ticker={"GOOGL": 1652044},
    )
    assert "GOOGL" in fundamentals
    v = valuation["GOOGL"]
    assert v["reason"] is None
    assert v["iv_median"] and v["iv_low"] and v["iv_high"]
    assert set(v["methods"]) == {"dcf_base", "epv", "graham"}
    assert v["median_method"] == "epv"
    assert v["iv_band_basis"] == "bear_bull"        # 계산해서 넘어온 밴드(폴백 아님)
    assert v["iv_dcf"]["bear"] is not None and v["iv_dcf"]["bull"] is not None
    assert v["net_debt"] == pytest.approx(-78_300.0)
    assert v["wacc_qc"] != pytest.approx(v["wacc"])
    assert v["fundamental_sources"][0]["accn"]
    assert v["fundamental_sources"][0]["filed"] == "2026-02-05"
    assert v["peg_basis"] in ("ttm", "fy_fallback")
    assert v["peg_growth_basis"] in ("eps_cagr", "revenue_cagr_fallback")


def _dataset(detail: dict) -> dict:
    hist = synthetic_history("FIXA", days=1300, end=date(2026, 11, 19), pattern="flat")
    panel = build_price_frame("FIXA", hist, None)
    return {
        "source": "live",
        "universe": [{"ticker": "FIXA", "sector": "Information Technology", "exchange": "NYSE",
                      "market_cap_usd": 2.4e12, "adv20_usd": 9e9, "history_rows": 1300,
                      "iv_mult": detail["iv_mult"]}],
        "panels": {"FIXA": panel},
        "fundamentals": {"FIXA": _company()},
        "valuation_inputs": {"FIXA": detail},
        "filings": {"FIXA": [
            {"form": "10-Q", "filed_date": "2026-10-29", "acceptance_datetime": "2026-10-29T20:05:00Z",
             "url": "https://www.sec.gov/Archives/edgar/data/1/10q.htm", "accession": "0001-26-000084"},
            {"form": "10-K", "filed_date": "2026-02-05", "acceptance_datetime": "2026-02-05T21:04:00Z",
             "url": "https://www.sec.gov/Archives/edgar/data/1/10k.htm", "accession": "0001-26-000018"},
        ]},
        "news": {},
    }


def test_build_views_uses_collected_band_and_evidence():
    detail = {**_detail(),
               "fundamental_sources": [{"fy": "2025-12-31", "accn": "0001-26-000018", "form": "10-K",
                                        "filed": "2026-02-05", "available_at": "2026-02-05T21:04:00Z"}]}
    views = build_views(_dataset(detail), AS_OF)
    assert len(views) == 1
    view = views[0]
    assert view.iv_low == pytest.approx(detail["iv_low"])
    assert view.iv_high == pytest.approx(detail["iv_high"])
    assert view.iv_median == pytest.approx(detail["iv_median"])
    assert view.iv_band_basis == "bear_bull"

    fundamental = [e for e in view.evidence if e["type"] == "fundamental"]
    assert fundamental and all(e["accession"] == "0001-26-000018" for e in fundamental)
    assert all(e["period_end"] == "2025-12-31" for e in fundamental)
    assert all(e["form"] == "10-K" for e in fundamental)
    # 근거 날짜가 실행일(as_of)로 채워지면 안 된다 — 실제 접수 시각이어야 한다
    assert all(e["filed_date"] != AS_OF.isoformat() for e in fundamental)
    assert all(e["available_at"] == "2026-02-05T21:04:00Z" for e in fundamental)


def test_reference_filing_is_separated():
    """최신 10-Q 는 `role: reference` 로만 붙고 재무 근거와 섞이지 않는다."""
    detail = {**_detail(),
               "fundamental_sources": [{"fy": "2025-12-31", "accn": "0001-26-000018", "form": "10-K",
                                        "filed": "2026-02-05", "available_at": "2026-02-05T21:04:00Z"}]}
    evidence = build_views(_dataset(detail), AS_OF)[0].evidence
    filings = [e for e in evidence if e["type"] == "sec_filing"]
    assert len(filings) == 1
    assert filings[0]["role"] == "reference"
    assert filings[0]["form"] == "10-Q"
    assert filings[0]["accession"] == "0001-26-000084"


def test_band_falls_back_only_when_detail_is_missing():
    """값이 없을 때만 중앙값 ±15% 로 폴백하고, 그 사실을 `fallback_15pct` 로 남긴다."""
    data = _dataset(_detail())
    data["valuation_inputs"] = {}      # 라이브 밖(픽스처·구 데이터) 경로
    view = build_views(data, AS_OF)[0]
    assert view.iv_band_basis == "fallback_15pct"
    assert view.iv_low == pytest.approx(view.iv_median * 0.85)


def test_build_views_supplies_split_factor_for_replay_panels():
    """재생·폴드 패널은 미래 분할까지 반영해 만들어지므로 마지막 봉의 `split_factor` 가 1 이 아니다.

    경계 계약(§4.2-4): 이 값을 `pipeline` 이 `EntryContext.split_factor` 로 넘기고, 밴드 환산은 트랙 A 가 맡는다.
    """
    from datetime import datetime, timezone

    hist = synthetic_history("FIXA", days=1300, end=date(2026, 11, 19), pattern="flat")
    splits = pd.Series({pd.Timestamp("2026-12-01"): 2.0})   # 결정일 이후 분할(패널에는 반영됨)
    panel = build_price_frame("FIXA", hist, splits)
    cut = datetime(2026, 11, 20, 2, 34, tzinfo=timezone.utc)
    view = build_views({**_dataset(_detail()), "panels": {"FIXA": panel.as_of(cut)}}, AS_OF)[0]
    assert view.split_factor == pytest.approx(2.0)
    assert view.indicators["raw_close"] == pytest.approx(view.indicators["sadj_close"] * 2.0)


def test_snapshot_restored_none_values_do_not_crash():
    """스냅샷 `inputs.json` 복원값(NaN→null→None)이 섞여도 점수·밸류에이션이 돈다.

    `engine/backtest/snapshot.py` 의 제네릭 직렬화가 NaN 을 null 로 쓰므로, 복원한
    `AnnualFundamentals` 에는 `None` 이 들어온다. 수치 함수는 NaN 만 견디므로 진입점에서 되돌린다.
    """
    from engine.metrics.fundamentals import sanitize_fundamentals

    restored = AnnualFundamentals(
        ebit=[1_200.0, 1_140.0, 1_080.0, 1_020.0, 960.0], revenue=[5_000.0] * 5,
        net_income=[300.0] * 5, ocf=[500.0] * 5, capex=[600.0] * 5,
        depreciation=[200.0, None, None, None, None],          # null 로 복원된 값
        total_assets=[6_000.0] * 5, non_interest_liabilities=[2_000.0] * 5,
        excess_cash=[100.0] * 5, total_debt=[1_000.0] * 5, cash=[800.0] * 5,
        equity=[3_000.0] * 5, diluted_shares=[100.0, 100.0, 100.0, 100.0, None],
        interest_expense=[40.0] * 5, tax_rate=0.21,
        marketable_securities=[300.0, None, None, None, None],
        delta_nwc=[0.0, 0.0, 0.0, 0.0, 0.0], ttm_eps=None,
    )
    clean = sanitize_fundamentals(restored)
    assert clean is not restored                       # 원본을 바꾸지 않는다
    assert all(value is not None for value in clean.diluted_shares)
    assert clean.diluted_shares[4] != clean.diluted_shares[4]      # NaN
    mult, detail = valuation_multiplier(clean, PRICE, "Information Technology", 1.0, 0.042)
    assert mult is not None and detail["iv_median"] > 0
    data = _dataset(_detail())
    data["fundamentals"] = {"FIXA": restored}
    view = build_views(data, AS_OF)[0]
    assert view.q is not None and view.iv_median is not None


def test_thesis_premium_sign():
    """margin 은 (IV−가격)/IV — 음수면 **프리미엄**이고 크기는 절대값으로 쓴다."""
    view = TickerView(
        ticker="FIXA", sector="Information Technology", indicators={"rsi14": 55.0}, panel=None,
        iv_low=None, iv_median=100.0, iv_high=None, margin=-2.333, q=70.0, v=None, g=None,
        news_state="not_read", evidence=[], filings=[], news=[], valuation_suspended=False,
        suspended_reason=None, structure={},
    )
    thesis = _thesis(view, {})
    assert "233% 프리미엄" in thesis
    assert "안전마진 -233%" in thesis
    assert "-233% 프리미엄" not in thesis
    # 할인은 그대로 양수 표기
    view.margin = 0.25
    assert "25% 할인" in _thesis(view, {})


def test_record_keeps_valuation_detail():
    """발행 레코드의 `valuation` 에 방법별 IV·밴드 근거가 남는다(배관 유실 금지)."""
    from engine.decide.pipeline import build_payload, load_params

    detail = {**_detail(),
              "fundamental_sources": [{"fy": "2025-12-31", "accn": "0001-26-000018", "form": "10-K",
                                       "filed": "2026-02-05", "available_at": "2026-02-05T21:04:00Z"}]}
    payload = build_payload(
        _dataset(detail), as_of=AS_OF, mode="test", params=load_params(), params_version="params_v001",
        code_commit="deadbeef", snapshot_id="snap", status="test",
        three_times={"data_cutoff_at": "2026-11-19T22:00:00Z",
                     "signal_published_at": "2026-11-19T22:02:11Z",
                     "order_cutoff_at": "2026-11-20T14:25:00Z"},
    )
    assert payload["signals"], "신호가 발행되지 않았다"
    valuation = payload["signals"][0]["valuation"]
    assert valuation["methods"] and {} != valuation["methods"]
    assert valuation["median_method"] == "epv"
    assert valuation["iv_dcf_bear"] == pytest.approx(detail["iv_dcf"]["bear"], abs=1e-4)
    assert valuation["iv_dcf_bull"] == pytest.approx(detail["iv_dcf"]["bull"], abs=1e-4)
    assert valuation["iv_band_basis"] == "bear_bull"
    assert valuation["wacc_valuation"] == pytest.approx(detail["wacc"], abs=1e-4)
    assert payload["watchlist"][0]["wacc_valuation"] == pytest.approx(detail["wacc"], abs=1e-4)
    assert payload["watchlist"][0]["wacc_qc"] is not None
