"""engine.backtest.runner — 시나리오(일별 봉 + 장 전 주문 + 기업행동)를 모의 계좌로 실행한다.

폴드 백테스트와 `scripts/run_walkforward.py` 가 같은 함수를 쓴다. 입력은 전부 명시적이며
전역 상태가 없으므로 같은 시나리오 + 같은 슬리피지면 결과가 바이트 동일하다(4.12).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from datetime import date

from engine.ledger.portfolio import Bar, BuyOrder, CorporateAction, PortfolioSimulator


def _as_bar(day: date, row: dict) -> Bar:
    return Bar(
        date=day,
        raw_open=float(row["raw_open"]),
        raw_high=float(row["raw_high"]),
        raw_low=float(row["raw_low"]),
        raw_close=float(row["raw_close"]),
        sadj_close=row.get("sadj_close"),
        dma200=row.get("dma200"),
        atr_pct=row.get("atr_pct"),
        rsi14=row.get("rsi14"),
        iv_high=row.get("iv_high"),
        sector=row.get("sector", ""),
    )


def run_scenario(scenario: dict, slippage_bps: float = 10.0,
                 initial_cash: float = 1_000_000.0, regime: str | None = None) -> PortfolioSimulator:
    """`scenario` = {days:[{date, bars:{ticker:{...}}, regime?, structural_breaks?, force_exits?}],
    orders:[{day, order}], corporate_actions:[CorporateAction]}."""
    sim = PortfolioSimulator(initial_cash=initial_cash, slippage_bps=slippage_bps)
    actions = [a if isinstance(a, CorporateAction) else CorporateAction(**a)
               for a in scenario.get("corporate_actions", [])]
    sim.register_corporate_actions(actions)

    # 시나리오는 **읽기 전용**이다 — 체결로 주문 상태가 바뀌므로 복사본으로 실행한다(재실행 결정론).
    orders_by_day: dict[date, list[BuyOrder]] = defaultdict(list)
    for row in scenario.get("orders", []):
        orders_by_day[row["day"]].append(copy.deepcopy(row["order"]))

    for day_row in scenario["days"]:
        day = day_row["date"] if isinstance(day_row["date"], date) else date.fromisoformat(str(day_row["date"]))
        bars = {ticker: _as_bar(day, row) for ticker, row in day_row["bars"].items()}
        sim.process_day(
            day,
            bars,
            new_orders=orders_by_day.get(day, []),
            regime=day_row.get("regime", regime or "neutral"),
            structural_breaks=frozenset(day_row.get("structural_breaks", [])),
            force_exits=day_row.get("force_exits", {}),
        )
    return sim
