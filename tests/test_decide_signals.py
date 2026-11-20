"""test_decide_signals — 매수/매도 트리거·등급·분할 계획·보유 추적 (기획서 §3.4·§3.5)."""

from __future__ import annotations

import pytest

from engine.decide.macro import RegimeView, classify_regime
from engine.decide.signals import (EntryContext, build_entry_plan, default_params, evaluate_entry, evaluate_exit,
                                   evaluate_holdings, expected_mdd, margin_of_safety, target_band, timing_score)


def ctx(**kw) -> EntryContext:
    base = dict(ticker="AAA", raw_close=91.5, sadj_close=91.5, sector="Technology",
                dma50=95.0, dma200=88.0, dma200_slope=0.01, rsi14=32.0, pct_b=0.15,
                atr_pct=0.03, iv_median=120.0, iv_high=150.0, q=80.0, v=75.0, g=60.0,
                structural_risk=10.0, in_watchlist=True)
    base.update(kw)
    return EntryContext(**base)


def hold(stop: float = 90.0, **kw) -> dict:
    base = dict(shares=10.0, avg_cost=100.0, stop_price=stop, trailing_high=110.0, initial_stop=stop)
    base.update(kw)
    return base


# ── 기본 계산 ────────────────────────────────────────────────────────────────
def test_margin_and_timing_and_band():
    assert margin_of_safety(100.0, 80.0) == pytest.approx(0.20)
    assert margin_of_safety(None, 80.0) is None
    t = timing_score(rsi14=30, dma50_deviation=-0.09, pct_b=0.1, support_score=1.0, volume_score=1.0)
    assert 90 <= t <= 100
    assert timing_score(rsi14=70, dma50_deviation=0.02, pct_b=1.0, support_score=0.0, volume_score=0.0) < 20
    band = target_band(iv_median=522.0, price=412.35)
    assert band["lo"] == pytest.approx(431.5, abs=0.2) and band["hi"] == pytest.approx(450.7, abs=0.2)
    assert band["r_lo"] == pytest.approx(band["lo"] / band["base_price"] - 1, abs=1e-6)
    assert band["defensive"] is False
    gy = target_band(iv_median=300.0, price=400.0)
    assert gy["defensive"] is True and gy["lo"] < gy["hi"] < 400.0
    assert expected_mdd(0.005) == pytest.approx(-0.05)   # 클램프 하한
    assert expected_mdd(0.05) == pytest.approx(-0.20)    # 클램프 상한


# ── 매수 게이트·등급 ─────────────────────────────────────────────────────────
def test_entry_all_gates_pass_returns_buy():
    ev = evaluate_entry(ctx(), "neutral")
    assert ev["failed_gates"] == []
    assert ev["grade"] in ("BUY", "STRONG_BUY")
    assert ev["signal"] == "BUY"


def test_b1_blocks_held_and_outside_watchlist():
    for kw in ({"held": True}, {"in_watchlist": False}):
        ev = evaluate_entry(ctx(**kw), "neutral")
        assert ev["gates"]["B1"] is False
        assert "B1" in ev["failed_gates"]
        assert ev["signal"] != "BUY"


def test_b3_threshold_follows_regime():
    c = ctx(iv_median=110.0)          # margin = (110-91.5)/110 = 16.8%
    assert evaluate_entry(c, "risk_on")["gates"]["B3"] is True     # 15%
    assert evaluate_entry(c, "neutral")["gates"]["B3"] is False    # 20%
    assert evaluate_entry(c, "risk_off")["gates"]["B3"] is False   # 25%


def test_b2_and_b4_conditions():
    weak = evaluate_entry(ctx(dma200=120.0, dma200_slope=-0.01, rsi14=50.0, pct_b=0.5, dma50=91.0),
                          "neutral")
    assert weak["gates"]["B2"] is False
    assert weak["gates"]["B4"] is False               # 눌림 조건 0개
    ok = evaluate_entry(ctx(dma200=120.0, dma200_slope=0.01, sadj_close=121.0, dma50=118.0), "neutral")
    assert ok["gates"]["B2"] is True                   # 기울기 양수 & 50일선 위


def test_valuation_suspended_sector_emits_no_signal():
    ev = evaluate_entry(ctx(valuation_suspended=True), "neutral")
    assert ev["signal"] == "SUSPENDED" and ev["grade"] == "NONE"
    assert ev["reason"] == "SECTOR_FORMULA_PENDING"
    assert ev["entry_score"] is None and ev["margin"] is None
    plan = build_entry_plan(ctx(valuation_suspended=True), ev, "neutral")
    assert plan["placeable"] is False


def test_growth_cap_and_news_cap_limit_grade_to_watch():
    ev = evaluate_entry(ctx(q=95.0, v=95.0, g=0.0), "neutral")
    assert ev["grade"] == "WATCH" and any("성장률" in r for r in ev["cap_reasons"])
    ev2 = evaluate_entry(ctx(news_state="insufficient_evidence"), "neutral")
    assert ev2["grade"] == "WATCH" and any("뉴스" in r for r in ev2["cap_reasons"])


def test_veto_blocks_b6():
    ev = evaluate_entry(ctx(vetoes=[{"code": "V01", "blocking": True, "name": "EARNINGS_WINDOW",
                                     "message_ko": "실적 발표 임박"}]), "neutral")
    assert ev["gates"]["B6"] is False
    assert ev["signal"] != "BUY"


def test_conflict_flag_from_investor_spread():
    ev = evaluate_entry(ctx(q=95.0, v=95.0, g=1.0), "neutral")
    assert ev["conflict"] is True and ev["spread"] >= 60


# ── 분할 계획 ────────────────────────────────────────────────────────────────
def test_entry_plan_shapes_and_no_same_day_price_input():
    """계획은 신호 시점 값만 쓴다 — 컨텍스트에 당일 시가·고가·저가 필드가 없다(LA-4)."""
    for forbidden in ("open", "high", "low", "raw_open", "raw_high", "raw_low"):
        assert forbidden not in EntryContext.__dataclass_fields__
    ev = evaluate_entry(ctx(), "neutral")
    plan = build_entry_plan(ctx(), ev, "neutral")
    assert plan["placeable"] is True
    assert [t["weight"] for t in plan["tranches"]] == [0.40, 0.30, 0.30]
    assert all(t["provisional"] is True for t in plan["tranches"])
    assert plan["entry_band"]["stop_min"] < plan["entry_band"]["limit_max"]
    assert plan["plan_weight"] <= 0.05 + 1e-12
    assert plan["planned_loss_pct"] <= 0.0075 + 1e-12
    assert plan["entry_band"]["limit_max"] == pytest.approx(120.0 * (1 - 0.20), rel=1e-6)


def test_third_tranche_disabled_outside_stop():
    ev = evaluate_entry(ctx(), "neutral")
    plan = build_entry_plan(ctx(), ev, "neutral")
    tranche3 = plan["tranches"][2]
    assert plan["stop_distance"] == pytest.approx(0.09)
    assert tranche3["active"] is False                      # 91.5×0.86 = 78.7 < 잠정 손절가 83.3
    assert plan["effective_weight"] == pytest.approx(0.05 * 0.70, rel=1e-6)
    assert "3차" in plan["tranche_note_ko"]


def test_watch_grade_places_no_order_unless_preview():
    ev = evaluate_entry(ctx(iv_median=110.0), "neutral")     # B3 실패 → WATCH
    assert ev["grade"] == "WATCH"
    assert build_entry_plan(ctx(iv_median=110.0), ev, "neutral")["placeable"] is False
    assert build_entry_plan(ctx(iv_median=110.0), ev, "neutral",
                            allow_watch=True)["placeable"] is True


def test_order_not_placeable_when_band_inverts():
    """stop_min ≥ limit_max → 1차 진입 불가(사유 기록)."""
    c = ctx(iv_median=95.0, sadj_close=90.0, dma200=95.0, dma200_slope=0.02)
    ev = evaluate_entry(c, "neutral")
    plan = build_entry_plan(c, ev, "neutral", allow_watch=True)
    assert plan["placeable"] is False and plan["reason"] == "outside_order_range"


# ── 매도 트리거 ──────────────────────────────────────────────────────────────
def test_sell_trigger_s1_valuation_overheat():
    ev = evaluate_exit(ctx(iv_median=100.0, sadj_close=125.0), hold(), "neutral")
    assert ev["triggers"]["S1"] is True
    assert ev["verdict"] in ("WATCH_DOWN", "TRIM", "EXIT")


def test_sell_trigger_s2_technical_overheat():
    ev = evaluate_exit(ctx(rsi14=75.0, pct_b=1.05, dma20_sigma=2.5), hold(), "neutral")
    assert ev["triggers"]["S2"] is True


def test_sell_trigger_s3_structural_and_forced_exit():
    ev = evaluate_exit(ctx(structural_risk=75.0), hold(), "neutral")
    assert ev["triggers"]["S3"] is True
    assert ev["verdict"] == "EXIT" and ev["forced"] is True
    assert ev["cancel_pending_tranches"] is True


def test_sell_trigger_s4_trend_break():
    ev = evaluate_exit(ctx(sadj_close=95.0, dma200=100.0, dma200_slope=-0.01, rel_strength_pctile=0.2),
                       hold(), "neutral")
    assert ev["triggers"]["S4"] is True


def test_sell_trigger_s5_stop_hit_forces_exit():
    ev = evaluate_exit(ctx(raw_close=89.0), hold(stop=90.0), "neutral")
    assert ev["triggers"]["S5"] is True and ev["verdict"] == "EXIT"
    ev_low = evaluate_exit(ctx(raw_close=95.0), hold(stop=90.0, raw_low=89.5), "neutral")
    assert ev_low["triggers"]["S5"] is True


def test_hold_when_nothing_fires():
    ev = evaluate_exit(ctx(), hold(stop=80.0), "neutral")
    assert ev["verdict"] == "HOLD" and ev["exit_score"] < 45


def test_suspended_sector_holding_skips_s1_only():
    ev = evaluate_exit(ctx(valuation_suspended=True, iv_median=100.0, sadj_close=125.0), hold(), "neutral")
    assert ev["triggers"]["S1"] is None
    assert "S2" in ev["triggers"] and "S5" in ev["triggers"]
    assert ev["note_ko"] and "S1" in ev["note_ko"]


# ── DoD: 워치리스트 편출 보유 종목도 S1~S5 추적 ──────────────────────────────
def test_holdings_tracked_after_watchlist_removal():
    contexts = {"AAA": ctx(structural_risk=75.0)}
    out = evaluate_holdings([{"ticker": "AAA", **hold(stop=90.0)}], contexts, watchlist=set())
    row = out["holdings"][0]
    assert set(row["triggers"]) == {"S1", "S2", "S3", "S4", "S5"}
    assert row["triggers"]["S3"] is True
    assert row["in_watchlist"] is False
    assert row["badge_ko"] == "워치리스트 외(보유)"
    assert row["new_buy_allowed"] is False
    assert row["sell_card"] is True
    assert out["exit_count"] == 1 and out["tracked_outside_watchlist"] == 1


def test_holdings_in_watchlist_have_no_outside_badge():
    out = evaluate_holdings([{"ticker": "AAA", **hold(stop=80.0)}], {"AAA": ctx()}, watchlist={"AAA"})
    row = out["holdings"][0]
    assert row["in_watchlist"] is True and row["badge_ko"] is None
    assert row["verdict"] == "HOLD"


def test_default_params_match_spec_numbers():
    p = default_params()
    assert p["risk"]["risk_per_trade"] == 0.0075
    assert p["risk"]["max_position"] == 0.05
    assert p["scaling"]["entry"] == [0.40, 0.30, 0.30]
    ev = evaluate_entry(ctx(), "neutral", params=p)
    assert ev["signal"] == "BUY"
