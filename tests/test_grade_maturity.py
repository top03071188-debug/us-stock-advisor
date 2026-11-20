"""test_grade_maturity — 만기 완료 판정 · 신호별 SPY 구간 · 총수익률 · 방향점수 초과수익 (R02).

감사 재현: 63거래일 만기에 하루치 가격만 넣어도 `observed_days: 1` · `Agreement: 100` 이 나왔다.
벤치마크는 배치 실행일 기준 5/21/63/126일 SPY 하나를 **모든 신호**에 돌려주었고, 방향 점수 `D`
는 초과수익이 아니라 절대수익 기준이었다. 이 파일은 네 가지를 모두 고정한다.

봉 날짜는 **NYSE 거래일**로 만든다(`bdate_range` 는 휴장일과 어긋나 만기 봉 판정이 흔들린다).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from engine.backtest.execution import build_entry_band
from engine.backtest.walk_forward import agreement_components
from engine.data.prices import build_price_frame
from engine.decide.signals import target_band
from engine.ledger import live
from engine.ledger.grade import grade_record
from engine.ledger.record import execution_event, publish_record
from engine.utils.dates import add_trading_days, is_trading_day

HORIZONS = {"3m": 63}


def nyse_index(start: str, days: int) -> list[date]:
    d = date.fromisoformat(start)
    out: list[date] = []
    while len(out) < days:
        if is_trading_day(d):
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def history(start: str = "2026-01-05", days: int = 400, price: float = 100.0,
            drift: float = 0.0005, wave: float = 0.0) -> pd.DataFrame:
    import math

    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in nyse_index(start, days)])
    close = pd.Series([price * (1 + drift) ** i * (1 + wave * math.sin(i / 11.0))
                       for i in range(days)], index=idx)
    return pd.DataFrame({
        "Open": close.shift(1).fillna(close.iloc[0]), "High": close * 1.002,
        "Low": close * 0.998, "Close": close, "Adj Close": close, "Volume": 1_000_000.0,
    }, index=idx)


def open_at(day: date) -> pd.Timestamp:
    """09:30 ET 개장 시각(UTC 13:30/14:30) — 분봉이 아니라 날짜만 맞추면 된다."""
    return pd.Timestamp(day).tz_localize("UTC") + pd.Timedelta(hours=14)


def make_record(as_of: str, *, signal_id: str = "s1", ticker: str = "AAA") -> dict:
    rec = publish_record(
        signal_id=signal_id, ticker=ticker, type="BUY", grade="B", entry_score=70.0, as_of=as_of,
        data_cutoff_at="2026-01-04T22:00:00Z", signal_published_at="2026-01-04T22:02:11Z",
        order_cutoff_at="2026-01-05T14:25:00Z", generated_price=100.0,
        target_band=target_band(140.0, 100.0), expected_mdd=-0.09, plan_weight=0.02,
        stop_distance=0.09, target_session=as_of,
    )
    rec["entry_band"] = build_entry_band(
        iv_median_sadj=140.0, required_margin=0.20, dma200_sadj=90.0,
        signal_price_sadj=100.0, stop_distance=0.09).as_dict()
    return rec


def fill_for(rec: dict, *, day: date, price: float = 100.0) -> dict:
    return execution_event(
        signal_id=rec["signal_id"], snapshot_id="sha256:x", at=open_at(day),
        execution_event="buy_filled", fill_price=price,
        weight=float(rec["position"]["tranche_weight"]), stop_price=price * 0.91,
    )


# ── 1) 만기 봉이 있어야 채점한다 ───────────────────────────────────────────────
def test_63d_grading_requires_maturity_bar():
    as_of = date(2026, 1, 5)
    maturity = add_trading_days(as_of, 63)
    hist = history(start="2026-01-06", days=70)
    rec = make_record(as_of.isoformat())
    ex = fill_for(rec, day=hist.index[0].date(), price=100.0)
    all_bars = live.bars_for(build_price_frame("AAA", hist, None), date(2026, 12, 31))
    assert len(all_bars) == 70
    signal_bar = {"date": as_of, "raw_open": 100.0, "raw_high": 100.0, "raw_low": 100.0,
                  "raw_close": 100.0, "adj_close": 100.0, "sadj_open": 100.0, "sadj_close": 100.0}

    one = grade_record(rec, ex, [signal_bar] + all_bars[:1], benchmark_returns=0.01,
                       horizons=HORIZONS)["horizons"]["3m"]
    assert one["complete"] is False and one["matured"] is False
    assert one["observed_days"] == 1 and one["agreement"] is None and one["return"] is None
    assert one["note"] == "만기 봉 미관측 — 채점 보류"

    sixty_two = grade_record(rec, ex, [signal_bar] + all_bars[:62], benchmark_returns=0.01,
                             horizons=HORIZONS)["horizons"]["3m"]
    assert sixty_two["complete"] is False and sixty_two["observed_days"] == 62

    full = grade_record(rec, ex, [signal_bar] + all_bars[:63], benchmark_returns=0.01,
                        horizons=HORIZONS)["horizons"]["3m"]
    assert full["complete"] is True and full["matured"] is True
    assert full["observed_days"] == 63
    assert full["maturity"] == maturity.isoformat()
    assert full["agreement"]["total"] is not None


# ── 2) 신호마다 같은 구간의 SPY ────────────────────────────────────────────────
def test_benchmark_matches_each_signal_window():
    stock = build_price_frame("AAA", history(days=400), None)
    spy = build_price_frame("SPY", history(days=400, drift=0.0009, wave=0.06), None)
    first = date(2026, 1, 5)
    second = add_trading_days(first, 63)
    records = [make_record(first.isoformat(), signal_id="a"),
               make_record(second.isoformat(), signal_id="b")]

    graded = live.grade_all(records, {}, {"AAA": stock}, add_trading_days(second, 63),
                            holdout_start=None, benchmark_panel=spy, horizons=HORIZONS)
    by_id = {g["signal_id"]: g for g in graded}
    b1 = by_id["a"]["horizons"]["3m"]["benchmark_return"]
    b2 = by_id["b"]["horizons"]["3m"]["benchmark_return"]
    assert b1 is not None and b2 is not None
    assert b1 != b2, "신호마다 같은 SPY 수익률이 들어갔다 — 배치 실행일 기준 값이 새고 있다"

    tr = spy.frame["adj_close"]
    for rec, bench in ((records[0], b1), (records[1], b2)):
        start = date.fromisoformat(rec["as_of"])
        end = add_trading_days(start, 63)
        expected = float(tr.loc[pd.Timestamp(end)]) / float(tr.loc[pd.Timestamp(start)]) - 1.0
        assert bench == pytest.approx(expected, abs=1e-6)


# ── 3) SPY 가 없으면 방향 점수는 None (0% 로 대체 금지) ──────────────────────────
def test_missing_benchmark_skips_direction_score():
    hist = history(days=70)
    rec = make_record("2026-01-05")
    row = grade_record(rec, fill_for(rec, day=hist.index[0].date()), live.bars_for(
        build_price_frame("AAA", hist, None), date(2026, 4, 30)),
        benchmark_returns={}, horizons=HORIZONS)["horizons"]["3m"]
    assert row["complete"] is True
    assert row["benchmark_return"] is None and row["excess_return"] is None
    assert row["benchmark_missing"] is True
    assert row["agreement"]["direction"] is None
    # 나머지 항목으로 재정규화한다(방향 40점 없이 25/15/20)
    weights = {"band": 25.0, "drawdown": 15.0, "timing": 20.0}
    usable = {k: row["agreement"][k] for k in weights if row["agreement"][k] is not None}
    assert usable
    num = sum(weights[k] * v for k, v in usable.items())
    den = sum(weights[k] for k in usable)
    assert row["agreement"]["total"] == pytest.approx(round(num / den, 1), abs=0.11)


# ── 4) 분할은 손실이 아니다 / 배당은 총수익에 들어간다 ───────────────────────────
def test_split_does_not_create_fake_loss():
    """2:1 분할 — `Close`(sadj)는 절반, `Adj Close`(총수익)는 불변."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in nyse_index("2026-01-05", 90)])
    split_at = len(idx) // 2
    close = pd.Series([100.0 if i < split_at else 50.0 for i in range(len(idx))], index=idx)
    adj = pd.Series([100.0] * len(idx), index=idx)
    hist = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Adj Close": adj, "Volume": 1_000_000.0}, index=idx)
    panel = build_price_frame("SPL", hist, pd.Series([2.0], index=pd.to_datetime([idx[split_at]])))
    bars = live.bars_for(panel, date(2026, 5, 8))
    entry_day = bars[1]["date"]                       # 분할 전(가격 2배)
    entry_raw = bars[1]["raw_close"]
    assert entry_raw == pytest.approx(200.0)

    rec = make_record("2026-01-05", signal_id="split", ticker="SPL")
    rec["generated_price"] = entry_raw
    rec["target_band"]["base_price"] = entry_raw
    ex = execution_event(signal_id="split", snapshot_id="sha256:x", at=open_at(entry_day),
                         execution_event="buy_filled", fill_price=entry_raw, weight=0.02)
    row = grade_record(rec, ex, bars, benchmark_returns=0.0, horizons=HORIZONS)["horizons"]["3m"]
    assert row["complete"] is True
    assert row["return"] == pytest.approx(0.0, abs=0.02), "분할이 −50% 손실로 보인다"
    raw_return = bars[-1]["raw_close"] / entry_raw - 1.0
    assert raw_return == pytest.approx(-0.75, abs=0.02)     # raw 로 계산하면 이 값이 나온다


def test_dividend_flows_into_total_return():
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in nyse_index("2026-01-05", 70)])
    sadj = pd.Series([100.0] * 70, index=idx)
    adj = pd.Series([100.0 + 0.03 * i for i in range(70)], index=idx)   # 배당 포함 총수익
    hist = pd.DataFrame({"Open": sadj, "High": sadj, "Low": sadj, "Close": sadj,
                         "Adj Close": adj, "Volume": 1_000_000.0}, index=idx)
    panel = build_price_frame("DIV", hist, None)
    bars = live.bars_for(panel, date(2026, 4, 30))
    rec = make_record("2026-01-05", signal_id="div", ticker="DIV")
    ex = fill_for(rec, day=bars[1]["date"], price=100.0)
    row = grade_record(rec, ex, bars, benchmark_returns=0.0, horizons=HORIZONS)["horizons"]["3m"]
    assert row["complete"] is True
    assert row["return"] > 0.015                       # 배당이 반영된 총수익(약 +1.9%)
    assert row["return_basis"] == "total_return_adj"
    assert bars[-1]["raw_close"] == pytest.approx(bars[1]["raw_open"], rel=1e-6)  # raw 는 제자리


# ── 5) 방향 점수는 초과수익 기준 ───────────────────────────────────────────────
def test_direction_uses_excess():
    base = fill = 100.0
    stock = 110.0
    beat = agreement_components(1, base, fill, 100.0, 100.0, stock, -0.05, -0.05, fill,
                                fill, stock, benchmark_return=0.05)
    lose = agreement_components(1, base, fill, 100.0, 100.0, stock, -0.05, -0.05, fill,
                                fill, stock, benchmark_return=0.15)
    assert beat["direction"] == 100.0      # +10% > +5%
    assert lose["direction"] == 0.0        # +10% < +15%
    assert beat["excess_return"] == pytest.approx(0.05, abs=1e-6)
    assert lose["excess_return"] == pytest.approx(-0.05, abs=1e-6)


def test_grade_all_marks_immature_horizons():
    hist = history(days=20)
    rec = make_record("2026-01-05")
    graded = live.grade_all([rec], {}, {"AAA": build_price_frame("AAA", hist, None)},
                            date(2026, 2, 2), holdout_start=None, benchmark_panel=None)
    h = graded[0]["horizons"]["3m"]
    assert h["complete"] is False and h["matured"] is False
    assert graded[0]["complete"]["3m"] is False
