"""test_portfolio — 모의 포트폴리오 시뮬레이터 (기획서 4.12 / Phase 2 DoD).

모든 테스트는 오프라인·결정론이다. 같은 스냅샷 재실행 → 바이트 동일.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from engine.backtest.demo import build_demo_scenario
from engine.backtest.execution import EntryBand
from engine.backtest.runner import run_scenario
from engine.decide.sizing import recheck_planned_loss, size_position, stop_distance_for
from engine.ledger.portfolio import (Bar, BuyOrder, CorporateAction, LookaheadError, PortfolioSimulator,
                                     TrancheOrder)

D1 = date(2026, 11, 19)
D2 = date(2026, 11, 20)
D3 = date(2026, 11, 23)
COMMIT = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)


def bar(day: date, o: float, h: float, lo: float, c: float, **kw) -> Bar:
    return Bar(date=day, raw_open=o, raw_high=h, raw_low=lo, raw_close=c, **kw)


def order(stop_min: float = 90.0, limit_max: float = 110.0, plan_weight: float = 0.05,
          stop_distance: float = 0.09, iv_high: float | None = None,
          tranches: list[TrancheOrder] | None = None, committed_at: datetime = COMMIT) -> BuyOrder:
    return BuyOrder(
        signal_id="s1", ticker="AAA", committed_at=committed_at, atr_pct=0.03,
        plan_weight=plan_weight,
        tranches=tranches or [TrancheOrder(1, 0.40, None), TrancheOrder(2, 0.30, 91.5),
                              TrancheOrder(3, 0.30, 84.0)],
        sector="Technology",
        band=EntryBand(stop_min=stop_min, limit_max=limit_max, dma200_floor=stop_min * 0.95,
                       provisional_stop=stop_min * 0.9, required_margin=0.20),
        stop_distance=stop_distance, iv_high=iv_high,
    )


# ── DoD: 같은 스냅샷 재실행 시 자산 곡선 바이트 동일 ────────────────────────────
def test_portfolio_determinism():
    scenario = build_demo_scenario()          # 시나리오는 읽기 전용(주문은 복사본으로 실행)
    first = run_scenario(scenario, slippage_bps=10).result()
    second = run_scenario(scenario, slippage_bps=10).result()
    assert first.to_json() == second.to_json()
    assert first.digest() == second.digest()
    assert len(first.equity_curve) == len(second.equity_curve) > 0
    # 슬리피지가 다르면 결과도 달라야 한다(같으면 비용이 무시되고 있다는 뜻)
    other = run_scenario(scenario, slippage_bps=20).result()
    assert other.to_json() != first.to_json()


def test_determinism_after_mutation_is_not_shared_state():
    scenario = build_demo_scenario()
    run_scenario(scenario, slippage_bps=10)
    # 첫 실행이 시나리오의 주문 상태를 바꾸지 않았다
    statuses = [(row["order"].status, row["order"].first_fill) for row in scenario["orders"]]
    assert all(status == "pending" and fill is None for status, fill in statuses)


# ── DoD: 계획 손실 = 비중 × 손절 거리 ≤ 0.75% (ATR 1~10%) ─────────────────────
def test_planned_loss_invariant_all_atr():
    for pct in [i / 1000 for i in range(10, 101, 5)]:      # 1.0% ~ 10.0%
        size = size_position(pct)
        dist = stop_distance_for(pct)
        assert dist == pytest.approx(min(3 * pct, 0.20))
        assert size.weight <= 0.05 + 1e-12
        assert size.planned_loss_pct <= 0.0075 + 1e-12
        assert size.planned_loss_pct == pytest.approx(size.weight * dist)
        if min(3 * pct, 0.20) < 0.15:                      # 5% 상한이 먼저 걸리는 구간
            assert size.weight == pytest.approx(0.05)


def test_planned_loss_invariant_with_regime_multiplier():
    for mult in (1.0, 0.7, 0.4):
        for pct in (0.01, 0.03, 0.05, 0.08, 0.10):
            size = size_position(pct, regime_mult=mult)
            assert size.planned_loss_pct <= 0.0075 + 1e-12
            assert size.weight <= size_position(pct, regime_mult=1.0).weight + 1e-12


def test_full_tranche_fill_keeps_planned_loss_under_budget():
    """3분할이 모두 채워진 포지션의 계획 손실도 0.75% 이하(3.8 분할별 재검증)."""
    sim = PortfolioSimulator(initial_cash=1_000_000)
    plan_weight = size_position(0.03).weight          # 5% 상한
    o = order(plan_weight=plan_weight, stop_distance=stop_distance_for(0.03),
              tranches=[TrancheOrder(1, 0.40, None), TrancheOrder(2, 0.30, 91.5),
                        TrancheOrder(3, 0.30, 84.0)])
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [o])
    sim.positions["AAA"].trailing_high = 95.2       # 트레일링이 손절가를 끌어올리지 않게 고정
    sim.process_day(D2, {"AAA": bar(D2, 95, 95.2, 88.2, 89)}, [])
    assert sim.positions["AAA"].tranches_filled == [1, 2]
    fills = [e for e in sim.events if e["event"] == "buy_filled"]
    assert len(fills) == 2
    for e in fills:
        assert e["planned_loss_pct"] is not None
        assert e["planned_loss_pct"] <= 0.0075 + 1e-12
    pos = sim.positions["AAA"]
    loss = pos.shares * max(0.0, pos.avg_cost - pos.initial_stop) / sim.equity({"AAA": 89.0})
    assert loss <= 0.0075 + 1e-12


# ── DoD: 손절·전량 청산 봉에서 잔여 2·3차 재진입 0건 ───────────────────────────
def test_cancel_after_stop():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    o = order(stop_distance=0.09)
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [o])
    assert "AAA" in sim.positions
    # 다음 봉: 저가가 손절가(≈86.5)와 2차 지정가(≈88.4)를 모두 통과
    sim.process_day(D2, {"AAA": bar(D2, 95, 95.5, 80, 82)}, [])
    day2_fills = [e for e in sim.events if e["event"] == "buy_filled" and e["date"] == D2.isoformat()]
    assert day2_fills == []                                  # 같은 봉 재진입 0건
    assert "AAA" not in sim.positions
    exited = [e for e in sim.events if e["event"] == "exit_filled" and e["date"] == D2.isoformat()]
    assert exited and exited[0]["reason"] == "S5"
    cancelled = [e for e in sim.events if e["event"] == "tranches_cancelled"]
    assert cancelled and cancelled[-1]["cancelled_by"] == "S5"
    order_row = sim.result().orders[0]
    assert order_row["cancelled_reason"] == "S5"
    assert all(t["filled_price"] is None for t in order_row["tranches"][1:])


def test_force_exit_cancels_pending_and_sells_at_open():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [order()])
    sim.process_day(D2, {"AAA": bar(D2, 94, 94.5, 93, 94)}, [], force_exits={"AAA": "S3"})
    assert "AAA" not in sim.positions
    fills = [e for e in sim.events if e["event"] == "buy_filled" and e["date"] == D2.isoformat()]
    assert fills == []
    assert any(e["event"] == "exit_filled" and e["reason"] == "S3" for e in sim.events)


def test_structural_break_cancels_same_day_split():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [order()])
    sim.positions["AAA"].trailing_high = 95.2
    sim.process_day(D2, {"AAA": bar(D2, 95, 95.2, 92.5, 93)}, [],
                    structural_breaks=frozenset({"AAA"}))
    assert "AAA" in sim.positions                                 # 청산은 아니고 추가 분할만 취소
    assert not [e for e in sim.events if e["event"] == "buy_filled" and e["date"] == D2.isoformat()]
    assert any(e["event"] == "tranches_cancelled" and e["cancelled_by"] == "structural_break"
               for e in sim.events)


# ── DoD: 2·3차 체결 직전 계획 손실 재검증(수량 축소·거절) ──────────────────────
def test_recheck_rejects_when_budget_exhausted():
    out = recheck_planned_loss(existing_shares=0.5, existing_avg_cost=100.0, stop_price=91.0,
                               requested_shares=0.2, fill_price=93.0, equity=500.0)
    assert out["action"] == "reject" and out["allowed_shares"] == 0.0


def test_recheck_shrinks_quantity_to_budget():
    out = recheck_planned_loss(existing_shares=0.1, existing_avg_cost=100.0, stop_price=91.0,
                               requested_shares=0.5, fill_price=93.0, equity=700.0)
    assert out["action"] == "shrink"
    assert 0 < out["allowed_shares"] < 0.5
    assert out["planned_loss_pct"] <= 0.0075 + 1e-9


def test_recheck_full_when_within_budget():
    out = recheck_planned_loss(existing_shares=0.0, existing_avg_cost=100.0, stop_price=91.0,
                               requested_shares=0.2, fill_price=100.0, equity=1_000_000)
    assert out["action"] == "full"


def test_simulator_recheck_keeps_position_within_cap():
    """5% 상한을 넘는 계획(의도적)은 체결 때 수량이 축소되고, 포지션 전체가 5% 안에 머문다."""
    sim = PortfolioSimulator(initial_cash=1_000_000)
    o = order(plan_weight=0.30, stop_distance=0.09)
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [o])
    fill = [e for e in sim.events if e["event"] == "buy_filled"][0]
    assert fill["recheck"] == "shrink"
    assert fill["weight"] <= 0.05 + 1e-6
    sim.positions["AAA"].trailing_high = 95.2
    sim.process_day(D2, {"AAA": bar(D2, 95, 95.2, 88.0, 89)}, [])
    fills = [e for e in sim.events if e["event"] == "buy_filled"]
    assert len(fills) == 2 and fills[1]["recheck"] == "shrink"
    pos = sim.positions["AAA"]
    equity = sim.equity({"AAA": 89.0})
    assert pos.shares * 88.44 / equity <= 0.05 + 1e-3
    assert pos.shares * max(0.0, pos.avg_cost - pos.initial_stop) / equity <= 0.0075 + 1e-9


# ── DoD: 기업행동(2:1 분할·배당 ex-date) ─────────────────────────────────────
def test_corporate_actions():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    o = order(stop_distance=0.09)
    sim.register_corporate_actions([
        CorporateAction(ticker="AAA", ex_date=D2, kind="split", ratio=2.0),
        CorporateAction(ticker="AAA", ex_date=D3, kind="dividend", dps=0.50),
    ])
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [o])
    pos = sim.positions["AAA"]
    shares_before, avg_before, stop_before = pos.shares, pos.avg_cost, pos.stop_price
    limit_before = sim.orders[0].tranches[1].limit_price
    cash_before = sim.cash
    sim.process_day(D2, {"AAA": bar(D2, 47.5, 48, 47, 47.8)}, [])
    pos = sim.positions["AAA"]
    assert pos.shares == pytest.approx(shares_before * 2)
    assert pos.avg_cost == pytest.approx(avg_before / 2)
    split_event = [e for e in sim.events if e["event"] == "corporate_action" and e["kind"] == "split"][0]
    assert split_event["stop_price"] == pytest.approx(stop_before / 2)
    assert pos.stop_price >= stop_before / 2 - 1e-9          # 트레일링은 올라가기만 한다
    assert sim.orders[0].tranches[1].limit_price == pytest.approx(limit_before / 2)   # 미체결 주문가
    assert pos.trailing_high <= 96.0
    sim.process_day(D3, {"AAA": bar(D3, 48, 48.5, 47.5, 48.2)}, [])
    assert sim.cash == pytest.approx(cash_before + pos.shares * 0.50, rel=1e-9)
    kinds = [e["kind"] for e in sim.events if e["event"] == "corporate_action"]
    assert kinds == ["split", "dividend"]


def test_split_then_stop_uses_adjusted_prices():
    """분할과 손절이 같은 날 겹치면 분할을 **먼저** 적용한다(분할 충격을 손절로 오인 금지)."""
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.register_corporate_actions([CorporateAction(ticker="AAA", ex_date=D2, kind="split", ratio=2.0)])
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [order()])
    sim.process_day(D2, {"AAA": bar(D2, 47.5, 48, 47.2, 47.8)}, [])
    assert "AAA" in sim.positions


# ── 갭 하락·슬리피지·주문 커밋 ────────────────────────────────────────────────
def test_gap_down_fills_at_open():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [order(stop_distance=0.09)])
    sim.process_day(D2, {"AAA": bar(D2, 70, 71, 69, 70.5)}, [])
    exit_row = [e for e in sim.events if e["event"] == "exit_filled"][0]
    assert exit_row["reason"] == "gap_stop"
    assert exit_row["price"] == pytest.approx(70 * (1 - 0.001), rel=1e-6)


def test_slippage_is_applied_in_right_direction():
    sim = PortfolioSimulator(initial_cash=1_000_000, slippage_bps=20)
    sim.process_day(D1, {"AAA": bar(D1, 100, 100.2, 99.8, 100)}, [order(stop_min=90, limit_max=120)])
    fill = [e for e in sim.events if e["event"] == "buy_filled"][0]
    assert fill["price"] == pytest.approx(100 * 1.002, rel=1e-6)      # 매수는 불리하게


def test_order_committed_after_open_is_rejected():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    late = order(committed_at=datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc))
    with pytest.raises(LookaheadError):
        sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [late])


def test_order_outside_band_is_not_filled():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.process_day(D1, {"AAA": bar(D1, 115, 116, 114, 115)}, [order(stop_min=90, limit_max=110)])
    assert "AAA" not in sim.positions
    assert any(e["event"] == "order_rejected" and e["reason"] == "outside_order_range"
               for e in sim.events)


def test_profit_take_sells_one_third_once():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [order(iv_high=120.0)])
    shares_before = sim.positions["AAA"].shares
    sim.process_day(D2, {"AAA": bar(D2, 118, 121, 117, 119)}, [])
    shares_after = sim.positions["AAA"].shares
    assert shares_after == pytest.approx(shares_before * (1 - 1 / 3), rel=1e-6)
    sim.process_day(D3, {"AAA": bar(D3, 119, 122, 118, 120)}, [])
    assert sim.positions["AAA"].shares == pytest.approx(shares_after)   # 반복 매도 없음


def test_sector_weight_and_exposure_recorded():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [order()])
    row = sim.equity_curve[-1]
    assert row["positions"] == 1
    assert 0 < row["exposure"] < 0.05
    assert row["sector_weights"]["Technology"] == pytest.approx(row["exposure"], rel=1e-3)


def test_day_order_is_enforced():
    sim = PortfolioSimulator(initial_cash=1_000_000)
    sim.process_day(D2, {"AAA": bar(D2, 95, 95.2, 94.9, 95)}, [])
    with pytest.raises(ValueError):
        sim.process_day(D1, {"AAA": bar(D1, 95, 95.2, 94.9, 95)}, [])
