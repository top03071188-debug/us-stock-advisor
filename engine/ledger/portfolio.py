"""engine.ledger.portfolio — 모의 포트폴리오 시뮬레이터 (기획서 4.12) [고정].

현금과 보유 수량을 추적해 **화면의 전략**(40/30/30 분할매수·분할익절·손절·갭 체결·슬리피지)을
그대로 실행한다. 같은 스냅샷 + 같은 체결 규칙이면 자산 곡선이 **바이트 단위로 동일**하다.

한 거래일의 처리 순서(봉 단위) [고정]
0. 주문 커밋(장 전) — `order_committed_at < market_open`. 당일 시가로 범위를 만들면 미래 참조.
1. 기업 이벤트 — 분할·배당을 **먼저** 적용(수량·평단·손절가·미체결 주문가 조정, 배당은 cash 가산).
2. 개장 경매 — 1차는 커밋된 범위(`stop_min < P ≤ limit_max`) 안에서만 체결. 시가 ≤ 손절가면 갭 하락.
3. 전량 청산(S3/S5/EXIT)이 먼저면 **잔여 2·3차 주문 즉시 취소** — 같은 봉 재진입 0건.
4. 구조적 훼손이 확정된 종목은 당일 추가 분할 취소.
5. 장중 도달 판정 — **손절을 먼저**, 그다음 지정가(2·3차, 분할익절). 3차 ≤ 초기 손절가면 무효.
6. 분할 체결 직전 계획 손실 재검증 — 초과하면 수량 축소 또는 거절.
7. 종가 평가·트레일링 갱신·노출도 기록.

가격 기준 [고정]: 손절·체결·평가는 `raw_as_traded`, 손절 거리는 `atr_pct` 비율.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime

from engine.backtest.execution import EntryBand, opening_fill
from engine.decide.sizing import (MAX_POSITION, MAX_SECTOR, RISK_PER_TRADE, recheck_planned_loss,
                                  stop_distance_for)
from engine.utils.dates import market_open_at


class LookaheadError(ValueError):
    """장 시작 뒤에 커밋된 주문(시가를 보고 범위를 만든 주문)."""


@dataclass(frozen=True)
class Bar:
    """하루치 봉. 지표는 그 봉 시점 값(전일 종가까지로 계산된 값)이다."""

    date: date
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    sadj_close: float | None = None
    dma200: float | None = None
    atr_pct: float | None = None
    rsi14: float | None = None
    iv_high: float | None = None
    sector: str = ""


@dataclass(frozen=True)
class CorporateAction:
    """분할(ex-split) 또는 배당(ex-date). 같은 날이면 분할을 먼저 적용한다(4.12)."""

    ticker: str
    ex_date: date
    kind: str                  # "split" | "dividend"
    ratio: float = 1.0         # split: 2.0 이면 2:1 분할
    dps: float = 0.0           # dividend: 주당 배당금


@dataclass
class TrancheOrder:
    index: int
    weight: float              # 포지션 목표 비중에 대한 분할 비율(0.40/0.30/0.30)
    limit_price: float | None
    active: bool = True
    trigger: str = "limit"     # limit | rsi | dma200
    filled_price: float | None = None
    note: str | None = None


@dataclass
class BuyOrder:
    """장 전에 커밋된 매수 주문 묶음(1차 경매 범위 + 2·3차 지정가)."""

    signal_id: str
    ticker: str
    committed_at: datetime
    atr_pct: float | None
    plan_weight: float
    tranches: list[TrancheOrder]
    sector: str = ""
    band: EntryBand | None = None
    stop_distance: float | None = None
    first_fill: float | None = None
    stop_price: float | None = None
    iv_high: float | None = None
    status: str = "pending"    # pending | active | done | cancelled | rejected
    cancelled_reason: str | None = None

    def active_tranches(self) -> list[TrancheOrder]:
        return [t for t in self.tranches if t.active and t.filled_price is None]

    @property
    def tranche1(self) -> TrancheOrder:
        return self.tranches[0]

    def as_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "ticker": self.ticker,
            "sector": self.sector,
            "status": self.status,
            "plan_weight": self.plan_weight,
            "stop_distance": self.stop_distance,
            "first_fill": self.first_fill,
            "stop_price": self.stop_price,
            "cancelled_reason": self.cancelled_reason,
            "tranches": [
                {"index": t.index, "weight": t.weight, "limit_price": t.limit_price,
                 "active": t.active, "trigger": t.trigger, "filled_price": t.filled_price,
                 "note": t.note}
                for t in self.tranches
            ],
        }


@dataclass
class Position:
    ticker: str
    sector: str
    shares: float
    avg_cost: float
    stop_price: float
    stop_distance: float
    trailing_high: float
    opened_at: date
    signal_id: str = ""
    initial_stop: float = 0.0
    iv_high: float | None = None
    tranches_filled: list[int] = field(default_factory=list)
    trim_done: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "sector": self.sector,
            "shares": round(self.shares, 6),
            "avg_cost": round(self.avg_cost, 6),
            "stop_price": round(self.stop_price, 6),
            "trailing_high": round(self.trailing_high, 6),
            "opened_at": self.opened_at.isoformat(),
            "tranches_filled": list(self.tranches_filled),
        }


@dataclass
class PortfolioResult:
    equity_curve: list[dict]
    trades: list[dict]
    events: list[dict]
    orders: list[dict]
    final_state: dict
    params: dict

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "params": self.params,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "events": self.events,
            "orders": self.orders,
            "final_state": self.final_state,
        }

    def to_json(self) -> str:
        """정규 JSON — 같은 입력이면 **바이트 동일**(determinism 테스트의 기준)."""
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), indent=2) + "\n"

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(float(x), nd)


class PortfolioSimulator:
    """결정론적 모의 계좌. 모든 시각·가격은 인자로만 들어온다(전역 상태 없음)."""

    def __init__(self, initial_cash: float = 1_000_000.0, slippage_bps: float = 10.0,
                 commission_bps: float = 0.0, risk_per_trade: float = RISK_PER_TRADE,
                 max_position: float = MAX_POSITION, max_sector: float = MAX_SECTOR,
                 profit_take_fractions: tuple[float, ...] = (1 / 3,),
                 min_shares: float = 1e-9) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash 는 0보다 커야 한다")
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.slippage_bps = float(slippage_bps)
        self.commission_bps = float(commission_bps)
        self.risk_per_trade = float(risk_per_trade)
        self.max_position = float(max_position)
        self.max_sector = float(max_sector)
        self.profit_take_fractions = tuple(profit_take_fractions)
        self.min_shares = float(min_shares)

        self.positions: dict[str, Position] = {}
        self.orders: list[BuyOrder] = []
        self.corporate_actions: list[CorporateAction] = []
        self._next_open: list[dict] = []
        self._prev_ind: dict[str, Bar] = {}
        self._last_close: dict[str, float] = {}

        self.equity_curve: list[dict] = []
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self._current_day: date | None = None
        self._last_day: date | None = None

    # ────────────────────────── 입력 ──────────────────────────
    def register_corporate_actions(self, actions: list[CorporateAction]) -> None:
        self.corporate_actions.extend(sorted(actions, key=lambda a: (a.ex_date, a.ticker, a.kind)))

    def submit(self, order: BuyOrder) -> None:
        """주문 등록. 장 전 커밋 검사는 `process_day` 가 그날 개장 시각으로 한다(LA-4)."""
        self.orders.append(order)

    @staticmethod
    def _validate_commit(order: BuyOrder, day: date) -> None:
        """`order_committed_at < market_open` — 당일 시가로 범위를 만들면 미래 참조다(3.4.4)."""
        if order.committed_at >= market_open_at(day):
            raise LookaheadError(
                f"{order.ticker}: 주문 커밋 {order.committed_at} 이 {day} 개장 이후다 — "
                "범위를 시가로 만들 수 없다(3.4.4, LA-4)."
            )

    # ────────────────────────── 평가 ──────────────────────────
    def equity(self, price_map: dict[str, float] | None = None) -> float:
        total = self.cash
        for ticker, pos in sorted(self.positions.items()):
            price = (price_map or {}).get(ticker, self._last_close.get(ticker, pos.avg_cost))
            total += pos.shares * float(price)
        return total

    def sector_weight(self, sector: str, price_map: dict[str, float] | None = None) -> float:
        eq = self.equity(price_map)
        if eq <= 0:
            return 0.0
        value = sum(pos.shares * float((price_map or {}).get(t, self._last_close.get(t, pos.avg_cost)))
                    for t, pos in self.positions.items() if pos.sector == sector)
        return value / eq

    # ────────────────────────── 하루 ──────────────────────────
    def process_day(self, day: date, bars: dict[str, Bar], new_orders: list[BuyOrder] | None = None,
                    regime: str = "neutral", structural_breaks: frozenset[str] = frozenset(),
                    force_exits: dict[str, str] | None = None) -> dict:
        if self._current_day is not None and day <= self._current_day:
            raise ValueError("거래일은 오름차순으로만 처리한다")
        self._current_day = day
        force_exits = dict(force_exits or {})

        # 0. 장 전 커밋 주문
        for order in new_orders or []:
            self._validate_commit(order, day)
            self.submit(order)
        queued, self._next_open = self._next_open, []

        # 4(pre). 구조적 훼손 — 체결 전에 잔여 분할을 취소한다
        for order in self._open_orders():
            if order.ticker in structural_breaks:
                self._cancel_pending(order, "structural_break", day)

        # 1. 기업 이벤트
        self._apply_corporate_actions(day)

        marks = {t: b.raw_open for t, b in bars.items()}

        # 2a. 갭 하락 — 시가가 손절가 이하면 시가 체결
        for ticker in sorted(self.positions):
            bar = bars.get(ticker)
            pos = self.positions.get(ticker)
            if bar is None or pos is None:
                continue
            if bar.raw_open <= pos.stop_price:
                self._exit(ticker, bar.raw_open, day, reason="gap_stop")

        # 2b. 구조적 훼손·전량 청산 — 시가에 전량 매도
        for ticker, reason in sorted(force_exits.items()):
            bar = bars.get(ticker)
            if ticker in self.positions and bar is not None:
                self._exit(ticker, bar.raw_open, day, reason=reason)

        # 2c. 1차 개장 경매 — 커밋된 범위 안에서만
        for order in self._open_orders():
            if order.status != "pending" or not order.tranche1.active:
                continue
            bar = bars.get(order.ticker)
            if bar is None:
                continue
            if order.band is None:
                order.status = "rejected"
                order.cancelled_reason = "outside_order_range"
                self._event(day, "order_rejected", order.ticker, reason="outside_order_range")
                continue
            fill = opening_fill(order.band, bar.raw_open, self.slippage_bps)
            if fill["execution_event"] != "buy_filled":
                order.status = "rejected"
                order.cancelled_reason = "outside_order_range"
                self._event(day, "order_rejected", order.ticker, reason="outside_order_range",
                            open_price=bar.raw_open, band=fill["band"])
                continue
            self._fill_tranche(order, order.tranche1, fill["fill_price"], day, marks, reason="opening_auction")

        # 2d. 전일 종가 조건으로 예약된 다음날 시가 실행
        for action in queued:
            if action["kind"] == "buy_tranche":
                order = action["order"]
                bar = bars.get(order.ticker)
                if bar is None or order.status in ("cancelled", "rejected"):
                    continue
                tranche = order.tranches[action["tranche"] - 1]
                if not tranche.active or tranche.filled_price is not None:
                    continue
                self._fill_tranche(order, tranche, bar.raw_open * (1 + self.slippage_bps / 1e4),
                                   day, marks, reason=action["reason"])
            elif action["kind"] == "sell_fraction":
                bar = bars.get(action["ticker"])
                if bar is None or action["ticker"] not in self.positions:
                    continue
                self._sell_fraction(action["ticker"], bar.raw_open, day, action["fraction"], action["reason"])

        # 3·5. 장중 — 손절 먼저, 그다음 지정가
        stopped_today: set[str] = set()
        for ticker in sorted(self.positions):
            bar, pos = bars.get(ticker), self.positions.get(ticker)
            if bar is None or pos is None:
                continue
            if bar.raw_low <= pos.stop_price:
                self._exit(ticker, min(pos.stop_price, bar.raw_open) if bar.raw_open <= pos.stop_price
                           else pos.stop_price, day, reason="S5")
                stopped_today.add(ticker)

        for ticker in sorted(self.positions):
            if ticker in stopped_today:
                continue
            bar, pos = bars.get(ticker), self.positions.get(ticker)
            if bar is None or pos is None or pos.iv_high is None:
                continue
            if bar.raw_high >= pos.iv_high:
                if "iv_high" not in pos.trim_done:
                    self._sell_fraction(ticker, pos.iv_high, day, self.profit_take_fractions[0], "iv_high")
                    pos.trim_done.add("iv_high")

        # 5b. 2·3차 지정가 도달
        for order in self._open_orders():
            bar = bars.get(order.ticker)
            if bar is None or order.ticker in stopped_today:
                continue
            for tranche in order.active_tranches():
                if tranche.index == 1 or tranche.trigger != "limit" or tranche.limit_price is None:
                    continue
                pos = self.positions.get(order.ticker)
                if pos is None or pos.initial_stop is not None and tranche.limit_price <= pos.initial_stop:
                    continue
                if bar.raw_low <= tranche.limit_price:
                    # 손절이 같은 봉에서 먼저 발동했다면 여기 도달하지 않는다(stopped_today).
                    self._fill_tranche(order, tranche, tranche.limit_price, day, marks, reason="limit")

        # 6. 종가 — 트레일링 갱신, 다음날 시가 예약
        for ticker in sorted(self.positions):
            bar, pos = bars.get(ticker), self.positions.get(ticker)
            if bar is None or pos is None:
                continue
            pos.trailing_high = max(pos.trailing_high, bar.raw_high)
            pos.stop_price = max(pos.stop_price, pos.trailing_high * (1.0 - pos.stop_distance))
        self._queue_next_open(bars)

        # 7. 종가 평가
        for ticker, bar in bars.items():
            self._last_close[ticker] = bar.raw_close
            self._prev_ind[ticker] = bar
        eq = self.equity()
        invested = eq - self.cash
        row = {
            "date": day.isoformat(),
            "cash": _r(self.cash, 4),
            "equity": _r(eq, 4),
            "exposure": _r(invested / eq if eq else 0.0, 6),
            "positions": len(self.positions),
            "sector_weights": {s: _r(self.sector_weight(s), 6)
                               for s in sorted({p.sector for p in self.positions.values()})},
        }
        self.equity_curve.append(row)
        self._last_day = day
        return row

    def result(self) -> PortfolioResult:
        return PortfolioResult(
            equity_curve=list(self.equity_curve),
            trades=list(self.trades),
            events=list(self.events),
            orders=[o.as_dict() for o in self.orders],
            final_state={
                "cash": _r(self.cash, 4),
                "equity": _r(self.equity(), 4),
                "positions": [p.as_dict() for _, p in sorted(self.positions.items())],
            },
            params={
                "initial_cash": self.initial_cash,
                "slippage_bps": self.slippage_bps,
                "commission_bps": self.commission_bps,
                "risk_per_trade": self.risk_per_trade,
                "max_position": self.max_position,
                "max_sector": self.max_sector,
            },
        )

    # ────────────────────────── 내부 ──────────────────────────
    def _open_orders(self) -> list[BuyOrder]:
        return [o for o in self.orders if o.status in ("pending", "active")]

    def _event(self, day: date, event: str, ticker: str, **kw) -> None:
        payload = {"date": day.isoformat(), "event": event, "ticker": ticker}
        payload.update({k: _r(v, 6) if isinstance(v, float) else v for k, v in kw.items()})
        self.events.append(payload)

    def _apply_corporate_actions(self, day: date) -> None:
        for ca in sorted([a for a in self.corporate_actions if a.ex_date == day],
                         key=lambda a: (0 if a.kind == "split" else 1, a.ticker)):
            if ca.kind == "split":
                ratio = float(ca.ratio)
                if ratio <= 0:
                    raise ValueError("분할 비율은 0보다 커야 한다")
                pos = self.positions.get(ca.ticker)
                if pos is not None:
                    pos.shares *= ratio
                    pos.avg_cost /= ratio
                    pos.stop_price /= ratio
                    pos.trailing_high /= ratio
                    pos.initial_stop /= ratio
                for order in self.orders:
                    if order.ticker != ca.ticker:
                        continue
                    if order.first_fill is not None:
                        order.first_fill /= ratio
                    if order.stop_price is not None:
                        order.stop_price /= ratio
                    for t in order.tranches:
                        if t.limit_price is not None and t.filled_price is None:
                            t.limit_price /= ratio
                self._event(day, "corporate_action", ca.ticker, kind="split", ratio=ratio,
                            shares=_r(pos.shares, 6) if pos else None,
                            avg_cost=_r(pos.avg_cost, 6) if pos else None,
                            stop_price=_r(pos.stop_price, 6) if pos else None)
            elif ca.kind == "dividend":
                pos = self.positions.get(ca.ticker)
                if pos is not None and ca.dps > 0:
                    amount = pos.shares * float(ca.dps)
                    self.cash += amount
                    self._event(day, "corporate_action", ca.ticker, kind="dividend",
                                dps=ca.dps, cash_added=_r(amount, 6))
            else:
                raise ValueError(f"알 수 없는 기업행동: {ca.kind}")

    def _fill_tranche(self, order: BuyOrder, tranche: TrancheOrder, price: float, day: date,
                      marks: dict[str, float], reason: str) -> None:
        price = float(price)
        if price <= 0:
            return
        if self.cash <= 0:
            self._cancel_pending(order, "no_cash", day)
            return
        equity = self.equity({**marks, order.ticker: price})
        target_weight = order.plan_weight * tranche.weight
        requested = target_weight * equity / price
        pos = self.positions.get(order.ticker)
        existing_shares = pos.shares if pos else 0.0
        existing_avg = pos.avg_cost if pos else price
        stop_price = pos.stop_price if pos else price * (1.0 - (order.stop_distance or 0.0))
        check = recheck_planned_loss(existing_shares, existing_avg, stop_price, requested, price,
                                     equity, cash=self.cash, max_position=self.max_position,
                                     risk_per_trade=self.risk_per_trade, min_shares=self.min_shares)
        if check["action"] == "reject":
            self._event(day, "tranche_rejected", order.ticker, tranche=tranche.index,
                        reason=check["reason"], requested_shares=_r(requested, 6),
                        planned_loss_pct=check.get("planned_loss_pct"))
            tranche.active = False
            if order.status == "pending":
                order.status = "rejected"
                order.cancelled_reason = check["reason"]
            return
        shares = float(check["allowed_shares"])
        notional = shares * price
        fee = notional * self.commission_bps / 1e4
        if notional + fee > self.cash + 1e-9:
            shares = max(0.0, (self.cash - fee) / price) if price else 0.0
            notional = shares * price
            if shares <= self.min_shares:
                self._cancel_pending(order, "no_cash", day)
                return
        self.cash -= notional + fee
        if pos is None:
            pos = Position(ticker=order.ticker, sector=order.sector, shares=shares, avg_cost=price,
                           stop_price=price * (1.0 - (order.stop_distance or 0.0)),
                           stop_distance=float(order.stop_distance or 0.0), trailing_high=price,
                           opened_at=day, signal_id=order.signal_id,
                           initial_stop=price * (1.0 - (order.stop_distance or 0.0)),
                           iv_high=order.iv_high)
            self.positions[order.ticker] = pos
        else:
            pos.avg_cost = (pos.avg_cost * pos.shares + shares * price) / (pos.shares + shares)
            pos.shares += shares
        pos.tranches_filled.append(tranche.index)
        tranche.filled_price = price
        order.status = "active"
        if order.first_fill is None:
            order.first_fill = price
            order.stop_price = pos.stop_price
            self._reanchor(order, pos, day)
        if not order.active_tranches():
            order.status = "done"
        self._event(day, "buy_filled", order.ticker, tranche=tranche.index, price=_r(price, 6),
                    shares=_r(shares, 6), weight=_r(shares * price / equity, 6),
                    stop_price=_r(pos.stop_price, 6), planned_loss_pct=check.get("planned_loss_pct"),
                    recheck=check["action"], reason=reason)

    def _reanchor(self, order: BuyOrder, pos: Position, day: date) -> None:
        """1차 **실제 체결가** 기준으로 2·3차 지정가를 다시 적는다(plan_reanchor)."""
        first = float(order.first_fill or 0.0)
        plan = []
        for tranche in order.tranches[1:]:
            drop = -0.07 if tranche.index == 2 else -0.14
            tranche.limit_price = round(first * (1.0 + drop), 6)
            if tranche.index == 3 and tranche.limit_price <= pos.initial_stop:
                tranche.active = False
                tranche.note = "3차 지정가가 초기 손절가 이하 → 무효"
            plan.append({"tranche": tranche.index, "limit": tranche.limit_price, "active": tranche.active})
        self._event(day, "plan_reanchor", order.ticker, first_fill=_r(first, 6),
                    stop_price=_r(pos.stop_price, 6), plan=plan)

    def _queue_next_open(self, bars: dict[str, Bar]) -> None:
        """전일 종가 조건(RSI≤25·200일선 −3%)을 다음 거래일 시가 실행으로 예약한다."""
        for order in self._open_orders():
            if order.first_fill is None:
                continue
            bar = bars.get(order.ticker)
            if bar is None:
                continue
            for tranche in order.active_tranches():
                if tranche.index == 1 or tranche.filled_price is not None:
                    continue
                if tranche.index == 2 and bar.rsi14 is not None and float(bar.rsi14) <= 25.0:
                    tranche.trigger = "rsi"
                    self._next_open.append({"kind": "buy_tranche", "order": order,
                                            "tranche": tranche.index, "reason": "rsi_next_open"})
                if tranche.index == 3 and bar.dma200 is not None and bar.sadj_close is not None and \
                        float(bar.sadj_close) <= float(bar.dma200) * 0.97:
                    tranche.trigger = "dma200"
                    self._next_open.append({"kind": "buy_tranche", "order": order,
                                            "tranche": tranche.index, "reason": "dma200_next_open"})
        for ticker, pos in sorted(self.positions.items()):
            bar = bars.get(ticker)
            if bar is not None and bar.rsi14 is not None and float(bar.rsi14) >= 75.0 \
                    and pos.iv_high is not None and "rsi" not in pos.trim_done:
                pos.trim_done.add("rsi")   # 분할익절은 1/3씩 한 번만(3.5)
                self._next_open.append({"kind": "sell_fraction", "ticker": ticker,
                                        "fraction": self.profit_take_fractions[0], "reason": "rsi_next_open"})

    def _sell_fraction(self, ticker: str, price: float, day: date, fraction: float, reason: str) -> None:
        pos = self.positions.get(ticker)
        if pos is None or pos.shares <= 0:
            return
        shares = pos.shares if fraction >= 1.0 else pos.shares * float(fraction)
        if shares <= self.min_shares:
            return
        self._sell(ticker, shares, price, day, reason)

    def _exit(self, ticker: str, price: float, day: date, reason: str) -> None:
        pos = self.positions.get(ticker)
        if pos is None:
            return
        self._sell(ticker, pos.shares, price, day, reason)
        for order in self.orders:
            if order.ticker == ticker and order.status in ("pending", "active"):
                self._cancel_pending(order, reason, day)

    def _sell(self, ticker: str, shares: float, price: float, day: date, reason: str) -> None:
        pos = self.positions[ticker]
        shares = min(shares, pos.shares)
        fill = float(price) * (1.0 - self.slippage_bps / 1e4)
        notional = shares * fill
        fee = notional * self.commission_bps / 1e4
        self.cash += notional - fee
        pnl_pct = fill / pos.avg_cost - 1.0 if pos.avg_cost else 0.0
        planned = None
        if pos.initial_stop and pos.avg_cost:
            planned = -(pos.avg_cost - pos.initial_stop) / pos.avg_cost
        self.trades.append({
            "ticker": ticker,
            "signal_id": pos.signal_id,
            "opened_at": pos.opened_at.isoformat(),
            "closed_at": day.isoformat(),
            "shares": _r(shares, 6),
            "avg_cost": _r(pos.avg_cost, 6),
            "exit_price": _r(fill, 6),
            "pnl_pct": _r(pnl_pct, 6),
            "reason": reason,
            "planned_loss_pct": _r(planned, 6),
            "excess_vs_plan": _r(pnl_pct - planned, 6) if planned is not None and reason in ("S5", "gap_stop") else None,
        })
        pos.shares -= shares
        self._event(day, "exit_filled", ticker, price=_r(fill, 6), shares=_r(shares, 6),
                    reason=reason, pnl_pct=_r(pnl_pct, 6))
        if pos.shares <= self.min_shares:
            self.positions.pop(ticker, None)

    def _cancel_pending(self, order: BuyOrder, reason: str, day: date) -> None:
        remaining = [t.index for t in order.active_tranches() if t.index != 1]
        for tranche in order.tranches:
            if tranche.filled_price is None:
                tranche.active = False
        order.status = "cancelled" if order.first_fill is None else "done_partial"
        order.cancelled_reason = reason
        self._next_open = [a for a in self._next_open
                           if not (a.get("order") is order or a.get("ticker") == order.ticker)]
        self._event(day, "tranches_cancelled", order.ticker, cancelled_by=reason,
                    remaining_tranches=remaining)


def order_from_plan(signal_id: str, ticker: str, committed_at: datetime, plan: dict,
                    sector: str = "") -> BuyOrder:
    """`engine.decide.signals.build_entry_plan` 의 결과 → 장 전 커밋 주문.

    `plan["entry_band"]` 는 dict 이므로 `EntryBand` 로 되살린다. 3차 무효는 `active=False` 로 보존된다.
    """
    band_d = plan.get("entry_band")
    band = None
    if band_d:
        band = EntryBand(stop_min=float(band_d["stop_min"]), limit_max=float(band_d["limit_max"]),
                         dma200_floor=float(band_d["dma200_floor"]),
                         provisional_stop=float(band_d["provisional_stop"]),
                         required_margin=float(band_d["required_margin"]))
    tranches = []
    for row in plan.get("tranches", []):
        tranches.append(TrancheOrder(index=int(row["tranche"]), weight=float(row["weight"]),
                                     limit_price=None if row["tranche"] == 1 else float(row["price_ref"]),
                                     active=bool(row.get("active", True)),
                                     note=row.get("note")))
    return BuyOrder(
        signal_id=signal_id, ticker=ticker, committed_at=committed_at,
        atr_pct=plan.get("atr_pct"), plan_weight=float(plan.get("plan_weight", 0.0)),
        tranches=tranches, sector=sector, band=band,
        stop_distance=plan.get("stop_distance"), iv_high=plan.get("iv_high"),
    )
