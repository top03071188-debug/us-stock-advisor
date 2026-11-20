"""engine.backtest.demo — 데이터 수집 전에 Phase 2 전 경로를 재현하는 결정론적 시나리오.

실데이터(yfinance/FRED)가 쌓이기 전에도 `신호 → 분할 계획 → 장 전 주문 → 모의 계좌 → 리포트`가
한 번에 도는지 확인하기 위한 **합성 데이터**다. 값은 고정 시드로만 만들어지며, 실데이터 결과로
쓰지 않는다(`is_demo: true` 로 표시).
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from engine.backtest.execution import EntryBand
from engine.decide.signals import EntryContext, build_entry_plan, evaluate_entry
from engine.ledger.portfolio import Bar, BuyOrder, CorporateAction, TrancheOrder
from engine.utils.dates import trading_days

DEMO_TICKERS = {"AAA": 120.0, "BBB": 78.0, "CCC": 240.0, "DDD": 55.0, "EEE": 310.0, "FFF": 165.0}


def _series(start_price: float, n: int, rng: random.Random, phase: int) -> list[dict]:
    """완만한 상승 → 중간 눌림 → 회복의 고정 시나리오. 지표는 그 시점까지의 값만 쓴다."""
    rows: list[dict] = []
    price = start_price
    for i in range(n):
        drift = 0.0012
        if 180 <= i < 210:
            drift = -0.006           # 눌림: 2·3차 트리거 구간
        elif 210 <= i < 240:
            drift = 0.004
        elif i >= 240:
            drift = 0.001
        shock = rng.gauss(0, 0.006)
        o = price * (1 + rng.gauss(0, 0.002))
        c = max(1.0, price * (1 + drift + shock))
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
        rows.append({"raw_open": o, "raw_high": hi, "raw_low": lo, "raw_close": c})
        price = c
    # 지표(전일까지) — dma200·atr_pct·rsi14·iv_high
    closes = [r["raw_close"] for r in rows]
    for i, r in enumerate(rows):
        window = closes[max(0, i - 199): i + 1]
        dma = sum(window) / len(window)
        trs = [rows[j]["raw_high"] - rows[j]["raw_low"] for j in range(max(1, i - 13), i + 1)]
        atr = sum(trs) / len(trs) if trs else rows[i]["raw_close"] * 0.02
        gains = [max(0.0, closes[j] - closes[j - 1]) for j in range(max(1, i - 13), i + 1)]
        losses = [max(0.0, closes[j - 1] - closes[j]) for j in range(max(1, i - 13), i + 1)]
        avg_gain = sum(gains) / len(gains) if gains else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        r["sadj_close"] = r["raw_close"]
        r["dma200"] = dma
        r["atr_pct"] = atr / r["raw_close"] if r["raw_close"] else None
        r["rsi14"] = rsi
        r["iv_high"] = r["raw_close"] * 1.35
    return rows


def _context(ticker: str, row: dict) -> EntryContext:
    """합성 봉 → 2층 판단 입력(지표는 그 시점까지의 값)."""
    return EntryContext(
        ticker=ticker, raw_close=row["raw_close"], sadj_close=row["sadj_close"],
        sector="Technology", dma50=row["dma200"], dma200=row["dma200"], dma200_slope=0.01,
        rsi14=row["rsi14"], pct_b=0.15, atr_pct=row["atr_pct"], iv_median=row["raw_close"] * 1.40,
        iv_high=row["iv_high"], q=86.0, v=78.0, g=66.0, structural_risk=8.0,
        in_watchlist=True, news_state="read_done",
        evidence=[{"type": "fundamental", "metric": "FCF_margin_5y", "value": 0.22},
                  {"type": "sec_filing", "form": "10-Q", "url": "https://www.sec.gov/"}],
    )


def build_demo_scenario(seed: int = 7, days: int = 300, signal_offset: int = 214) -> dict:
    """3종목 · 300거래일. `signal_offset` 일에 신호를 만들고 다음 거래일 개장에 주문이 걸린다."""
    rng = random.Random(seed)
    start = date(2026, 1, 2)
    end = start + timedelta(days=int(days * 1.45) + 10)
    calendar = trading_days(start, end)[:days]
    series = {t: _series(p, len(calendar), random.Random(seed + i), i)
              for i, (t, p) in enumerate(sorted(DEMO_TICKERS.items()))}

    day_rows = []
    for i, d in enumerate(calendar):
        bars = {t: {**series[t][i], "sector": "Technology"} for t in series}
        day_rows.append({"date": d, "bars": bars, "regime": "neutral"})

    # 신호 → 분할 계획 → 장 전 주문 (T+1 개장 전 커밋)
    orders = []
    signals = []
    chosen: list[int] = []
    for ticker in sorted(series):
        picked = None
        for i in range(150, min(len(calendar) - 2, 275)):
            row = series[ticker][i]
            ctx = _context(ticker, row)
            ev = evaluate_entry(ctx, "neutral")
            plan = build_entry_plan(ctx, ev, "neutral")
            if plan.get("placeable"):
                picked = (i, row, ev, plan)
                break
        if picked is None:
            continue
        i, row, ev, plan = picked
        t_sig, d_fill = calendar[i], calendar[i + 1]
        committed = datetime.combine(t_sig, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=22)
        chosen.append(i)
        signals.append({"ticker": ticker, "grade": ev["grade"], "entry_score": ev["entry_score"],
                        "margin": ev["margin"], "timing": ev["timing"], "atr_pct": row["atr_pct"],
                        "stop_distance": plan["stop_distance"], "plan_weight": plan["plan_weight"],
                        "planned_loss_pct": plan["planned_loss_pct"],
                        "signal_date": t_sig.isoformat(), "fill_date": d_fill.isoformat()})
        band = plan["entry_band"]
        order = BuyOrder(
            signal_id=f"{t_sig.isoformat()}-{ticker}-BUY-001", ticker=ticker, committed_at=committed,
            atr_pct=row["atr_pct"], plan_weight=plan["plan_weight"],
            tranches=[TrancheOrder(index=t["tranche"], weight=t["weight"],
                                   limit_price=None if t["tranche"] == 1 else float(t["price_ref"]),
                                   active=bool(t.get("active", True)), note=t.get("note"))
                      for t in plan["tranches"]],
            sector="Technology",
            band=EntryBand(stop_min=band["stop_min"], limit_max=band["limit_max"],
                           dma200_floor=band["dma200_floor"], provisional_stop=band["provisional_stop"],
                           required_margin=band["required_margin"]),
            stop_distance=plan["stop_distance"], iv_high=row["iv_high"],
        )
        orders.append({"day": d_fill, "order": order})

    actions = [CorporateAction(ticker="AAA", ex_date=calendar[250], kind="split", ratio=2.0),
               CorporateAction(ticker="BBB", ex_date=calendar[260], kind="dividend", dps=0.35)]
    return {
        "is_demo": True,
        "seed": seed,
        "days": day_rows,
        "orders": orders,
        "corporate_actions": actions,
        "signals": signals,
        "signal_days": [calendar[i].isoformat() for i in chosen],
    }
