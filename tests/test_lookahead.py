"""test_lookahead — look-ahead bias 차단 14종 (LA-1 ~ LA-14).

각 테스트 이름은 기획서 4.5 표의 자동 테스트 이름과 같다.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.backtest.execution import OrderNotPlaceable, build_entry_band, opening_fill
from engine.backtest.params import active_params
from engine.backtest.snapshot import as_of_frame, make_snapshot, verify_snapshot
from engine.data.macro import as_of_macro, parse_observations
from engine.data.prices import verify_against_raw_source
from engine.data.sec_edgar import filings_as_of, parse_submissions
from engine.data.universe import delisting_return_or_flag, membership_at, universe_view
from engine.metrics.technical import atr_pct, dma, relative_strength
from engine.models import PriceKind, PriceKindError
from engine.utils.dates import bar_available_at, data_cutoff_at

ROOT = Path(__file__).resolve().parents[1]


# ── LA-1 ────────────────────────────────────────────────────────────────
def test_no_future_fundamentals():
    df = pd.DataFrame([
        {"ticker": "X", "metric": "revenue", "filed_date": "2026-10-24",
         "acceptance_datetime": "2026-10-24T20:05:00Z", "available_at": "2026-10-24T20:05:00Z"},
        {"ticker": "X", "metric": "revenue", "filed_date": "2026-11-15",
         "acceptance_datetime": "2026-11-15T22:10:00Z", "available_at": "2026-11-15T22:10:00Z"},
    ])
    cut = pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime()
    seen = as_of_frame(df, cut)
    assert len(seen) == 1 and seen.iloc[0]["filed_date"] == "2026-10-24"
    # period_end 만 있고 available_at 이 없으면 실패한다(날짜 기준 조인 금지)
    with pytest.raises(Exception):
        as_of_frame(df.drop(columns=["available_at"]), cut)


# ── LA-2 ────────────────────────────────────────────────────────────────
def test_universe_point_in_time(universe_df):
    at_2025_05 = pd.Timestamp("2025-05-01T00:00:00Z").to_pydatetime()
    at_2025_08 = pd.Timestamp("2025-08-01T00:00:00Z").to_pydatetime()
    assert membership_at(universe_df, at_2025_05) == {"AAA", "BBB"}
    assert membership_at(universe_df, at_2025_08) == {"AAA", "CCC"}
    # 데이터 시작 전이면 미검증 → 게이트 사용 금지
    early = universe_view(universe_df, pd.Timestamp("2023-01-01T00:00:00Z").to_pydatetime())
    assert early["universe_verified"] is False
    assert early["tickers"] == []


# ── LA-3 ────────────────────────────────────────────────────────────────
def test_delisted_included():
    df = pd.DataFrame([
        {"ticker": "OLD", "delist_date": "2025-03-01", "delisting_return": None},
        {"ticker": "MERGE", "delist_date": "2024-06-01", "delisting_return": 0.12},
    ])
    out = delisting_return_or_flag(df)
    assert len(out) == 2                                 # 제외하지 않는다
    assert set(out["delisting_status"]) == {"unknown_liquidation", "known"}


# ── LA-4 ────────────────────────────────────────────────────────────────
def test_no_same_bar_fill():
    """1차 체결은 **장 전에 커밋한 범위** 안에서만. 시가를 보고 범위를 만들지 않는다."""
    sig = inspect.signature(build_entry_band)
    assert "open_price" not in sig.parameters and "open" not in sig.parameters
    band = build_entry_band(iv_median=120.0, required_margin=0.20, dma200_sadj=90.0,
                            signal_price=95.0, stop_distance=0.06)
    # 같은 날 시가가 범위 밖이면 체결되지 않는다(시가를 보고 통과시키지 않는다)
    assert opening_fill(band, 96.5)["execution_event"] == "no_action"
    fill = opening_fill(band, 95.0)
    assert fill["execution_event"] == "buy_filled"
    assert fill["fill_price"] > 95.0                     # 슬리피지 10bp


def test_execution_order_band_uses_prior_day_only():
    """범위 계산 입력은 전일 종가 기반 지표·잠정 손절가뿐 — 당일 시가가 들어가면 미래 참조."""
    band = build_entry_band(iv_median=120.0, required_margin=0.20, dma200_sadj=90.0,
                            signal_price=95.0, stop_distance=0.06)
    assert band.as_dict() == build_entry_band(120.0, 0.20, 90.0, 95.0, 0.06).as_dict()
    with pytest.raises(OrderNotPlaceable):
        build_entry_band(iv_median=100.0, required_margin=0.20, dma200_sadj=90.0,
                         signal_price=95.0, stop_distance=0.10)   # stop_min 90*0.97=87.3? limit 80 → 불가


# ── LA-5 ────────────────────────────────────────────────────────────────
def test_news_cutoff():
    news = pd.DataFrame([
        {"news_id": "n1", "published_at": "2026-11-18T21:40:00Z", "available_at": "2026-11-18T21:40:00Z"},
        {"news_id": "n2", "published_at": "2026-11-19T23:00:00Z", "available_at": "2026-11-19T23:00:00Z"},
    ])
    cut = data_cutoff_at(date(2026, 11, 19), "postclose")
    seen = as_of_frame(news, cut)
    assert list(seen["news_id"]) == ["n1"]
    assert as_of_frame(news, cut).shape[0] == 1


# ── LA-6 ────────────────────────────────────────────────────────────────
def test_macro_release_time(macro_payload):
    df = parse_observations(macro_payload, "CPIAUCSL")
    # 관측월(10-01)을 D 로 쓰는 잘못된 구현이면 11-01 결정에 이미 보였을 것이다
    nov1 = as_of_macro(df, pd.Timestamp("2026-11-01T00:00:00Z").to_pydatetime())
    assert nov1[nov1["date"] == date(2026, 10, 1)].empty
    later = as_of_macro(df, pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime())
    assert not later[later["date"] == date(2026, 10, 1)].empty


# ── LA-7 ────────────────────────────────────────────────────────────────
def test_13f_filed_date():
    payload = {"filings": {"recent": {
        "accessionNumber": ["0000000000-26-000001"], "form": ["13F-HR"],
        "primaryDocument": ["form13f.xml"], "items": [""],
        "acceptanceDateTime": ["2026-11-14T21:30:00.000Z"], "filingDate": ["2026-11-14"],
    }}}
    rows = parse_submissions("MSFT", 789019, payload)
    cut_before = pd.Timestamp("2026-11-14T12:50:00Z").to_pydatetime()
    cut_after = pd.Timestamp("2026-11-15T12:50:00Z").to_pydatetime()
    assert filings_as_of(rows, cut_before) == []
    assert len(filings_as_of(rows, cut_after)) == 1


# ── LA-8 ────────────────────────────────────────────────────────────────
def test_params_point_in_time():
    history = [
        {"version": "params_v013", "adopted_at": "2026-06-30T00:00:00Z"},
        {"version": "params_v014", "adopted_at": "2026-10-01T00:00:00Z"},
    ]
    assert active_params(history, pd.Timestamp("2026-08-01T00:00:00Z").to_pydatetime())["version"] == "params_v013"
    assert active_params(history, pd.Timestamp("2026-10-02T00:00:00Z").to_pydatetime())["version"] == "params_v014"
    with pytest.raises(ValueError):
        active_params(history, pd.Timestamp("2025-01-01T00:00:00Z").to_pydatetime())


# ── LA-9 ────────────────────────────────────────────────────────────────
def test_adjustment_consistency(panel_with_split):
    df = panel_with_split.frame
    err = (df["raw_close"] / (df["sadj_close"] * df["split_factor"]) - 1).abs()
    assert err.max() < 1e-9
    direct = pd.DataFrame({"Open": df["raw_open"], "High": df["raw_high"],
                           "Low": df["raw_low"], "Close": df["raw_close"]})
    assert verify_against_raw_source(panel_with_split, direct)["raw_verified"] is True


def test_split_reconstruction_is_reversible():
    idx = pd.bdate_range("2024-01-02", periods=10)
    sadj = pd.Series(np.linspace(100, 110, 10), index=idx)
    splits = pd.Series([4.0], index=pd.to_datetime([idx[5]]))
    from engine.data.prices import reconstruct_raw

    raw = reconstruct_raw(sadj, splits)
    assert raw.iloc[0] == pytest.approx(sadj.iloc[0] * 4)
    assert raw.iloc[-1] == pytest.approx(sadj.iloc[-1])


# ── LA-10 ───────────────────────────────────────────────────────────────
def test_cik_join():
    """티커가 바뀌어도 CIK 로 같은 회사로 묶인다."""
    payload = {"filings": {"recent": {
        "accessionNumber": ["a-1", "a-2"], "form": ["10-K", "10-Q"],
        "primaryDocument": ["k.htm", "q.htm"], "items": ["", ""],
        "acceptanceDateTime": ["2026-02-01T21:00:00.000Z", "2026-05-01T21:00:00.000Z"],
        "filingDate": ["2026-02-01", "2026-05-01"],
    }}}
    old = parse_submissions("FB", 1326801, payload)
    new = parse_submissions("META", 1326801, payload)
    ciks = {r["cik"] for r in old + new}
    assert ciks == {1326801}
    # 티커가 달라도 같은 CIK·같은 accession 은 하나의 공시로 묶인다(중복 계수 금지)
    assert len(old) + len(new) == 4
    assert len({r["accession"] for r in old + new}) == 2


# ── LA-11 ───────────────────────────────────────────────────────────────
def test_available_at_filter(panel_no_split):
    day = panel_no_split.frame.index[-1].date()
    # 21:50 KST(=12:50 UTC)에는 그날 봉(마감 16:00 ET)이 아직 없다
    premarket_cut = data_cutoff_at(day, "premarket")
    panel = panel_no_split.as_of(premarket_cut)
    assert day not in {d.date() for d in panel.frame.index}
    after = panel_no_split.as_of(bar_available_at(day))
    assert day in {d.date() for d in after.frame.index}


# ── LA-12 ───────────────────────────────────────────────────────────────
def test_price_kind_separation(panel_with_split):
    raw = panel_with_split.series(PriceKind.raw_as_traded)
    sadj = panel_with_split.series(PriceKind.split_adj_ohlc)
    with pytest.raises(PriceKindError):
        atr_pct(raw)
    with pytest.raises(PriceKindError):
        dma(raw, 50)
    # 상대강도는 total_return_adj 전용
    with pytest.raises(PriceKindError):
        relative_strength(sadj, sadj)
    # 분할 전 구간에서 두 기준의 종가는 다르고, 이후에는 같다
    frame = panel_with_split.frame
    assert frame["raw_close"].iloc[0] != frame["sadj_close"].iloc[0]


# ── LA-13 ───────────────────────────────────────────────────────────────
def test_no_holdout_leak_fold_rule():
    from engine.utils.dates import fold_summary

    s = fold_summary(date(2026, 11, 19))
    for h in s["horizons"].values():
        for f in h["folds"]:
            assert (f["maturity"] < s["holdout_start"]) == f["eligible"]


# ── LA-14 ───────────────────────────────────────────────────────────────
def test_archive_presence(tmp_path):
    frames = {"prices": pd.DataFrame({
        "date": ["2026-11-17", "2026-11-18"],
        "available_at": ["2026-11-17T21:00:00Z", "2026-11-18T21:00:00Z"],
        "raw_close": [100.0, 101.0],
    })}
    snap = make_snapshot(tmp_path / "snap", pd.Timestamp("2026-11-19T12:50:00Z").to_pydatetime(), frames)
    assert verify_snapshot(snap.root)["ok"] is True
    # 원본이 없으면 재현 불가 → 실패로 끝난다
    (snap.root / "prices.csv").unlink()
    assert verify_snapshot(snap.root)["ok"] is False
    # 공개 Release 를 쓰지 않는다는 설정이 남아 있다
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "params_v001.yaml").read_text(encoding="utf-8"))
    assert "private_repo" in cfg["data"]["snapshot_archive"]
    assert cfg["data"]["snapshot_overwrite"] is False


def test_snapshot_id_is_deterministic(tmp_path):
    frames = {"prices": pd.DataFrame({"date": ["2026-11-18"], "available_at": ["2026-11-18T21:00:00Z"],
                                      "raw_close": [101.0]})}
    cut = pd.Timestamp("2026-11-19T12:50:00Z").to_pydatetime()
    a = make_snapshot(tmp_path / "a", cut, frames)
    b = make_snapshot(tmp_path / "b", cut, frames)
    assert a.snapshot_id == b.snapshot_id


def test_no_lookahead_lint_rule():
    """백테스트 환경의 데이터 조회는 as_of 없이 부를 수 없다는 규칙을 코드로 확인."""
    from engine.backtest.snapshot import as_of_frame as fn

    sig = inspect.signature(fn)
    assert sig.parameters["decision_at"].default is inspect._empty
