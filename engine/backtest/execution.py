"""engine.backtest.execution — 개장 경매 주문 범위(1차 분할)와 체결 판정 [고정, v1.3.1].

확정 시가를 보고 게이트를 검사한 뒤 같은 시가에 체결하면 미래 참조다. 그래서 1차는
**장 시작 전에 계산한 주문 범위**(`stop_min` < P ≤ `limit_max`)를 커밋하고,
그 범위에서 성립한 경매 체결가만 인정한다.

    limit_max = IV_median × (1 − required_margin)          # B3 안전마진 상한
    stop_min  = max(DMA200_sadj × 0.97,                    # B2 200일선 하한
                    signal_price × (1 − 잠정 손절 거리))     # 잠정 손절가 하한
"""

from __future__ import annotations

from dataclasses import dataclass


class OrderNotPlaceable(ValueError):
    """stop_min ≥ limit_max — 그날 1차 진입 자체가 불가능하다."""


@dataclass(frozen=True)
class EntryBand:
    stop_min: float
    limit_max: float
    dma200_floor: float
    provisional_stop: float
    required_margin: float

    def contains(self, price: float) -> bool:
        return self.stop_min < float(price) <= self.limit_max

    def as_dict(self) -> dict:
        return {
            "stop_min": round(self.stop_min, 4),
            "limit_max": round(self.limit_max, 4),
            "dma200_floor": round(self.dma200_floor, 4),
            "provisional_stop": round(self.provisional_stop, 4),
            "required_margin": self.required_margin,
        }


def build_entry_band(iv_median: float, required_margin: float, dma200_sadj: float,
                     signal_price: float, stop_distance: float, dma200_mult: float = 0.97) -> EntryBand:
    """**전일 종가까지만** 쓴다. 여기에 당일 시가·고가·저가가 들어가면 미래 참조(3.4.4)."""
    if signal_price <= 0:
        raise ValueError("signal_price 는 0보다 커야 한다")
    limit_max = float(iv_median) * (1.0 - float(required_margin))
    dma_floor = float(dma200_sadj) * dma200_mult
    provisional_stop = float(signal_price) * (1.0 - float(stop_distance))
    stop_min = max(dma_floor, provisional_stop)
    if stop_min >= limit_max:
        raise OrderNotPlaceable(
            f"stop_min({stop_min:.2f}) ≥ limit_max({limit_max:.2f}) — 1차 주문 불가(사유 기록)"
        )
    return EntryBand(stop_min, limit_max, dma_floor, provisional_stop, float(required_margin))


def opening_fill(band: EntryBand, open_price: float, slippage_bps: float = 10.0) -> dict:
    """경매 체결가가 범위 안이면 체결, 아니면 미체결(사유를 남긴다)."""
    if not band.contains(open_price):
        return {
            "execution_event": "no_action",
            "fill_price": None,
            "rejected_reason": "outside_order_range",
            "open_price": float(open_price),
            "band": band.as_dict(),
        }
    fill = float(open_price) * (1.0 + slippage_bps / 10_000.0)
    fill = min(fill, band.limit_max)  # 지정가 상한을 넘겨 체결되지 않는다
    return {
        "execution_event": "buy_filled",
        "fill_price": round(fill, 4),
        "slippage_bps": slippage_bps,
        "rejected_reason": None,
        "open_price": float(open_price),
        "band": band.as_dict(),
    }


def entry_plan(first_fill: float, tranches=(0.40, 0.30, 0.30), drops=(0.0, -0.07, -0.14),
               stop_price: float | None = None) -> list[dict]:
    """1차 실제 체결가 기준 2·3차 지정가. 3차가 손절가 밖이면 무효."""
    plan = []
    for i, (weight, drop) in enumerate(zip(tranches, drops), start=1):
        price = round(float(first_fill) * (1.0 + drop), 2)
        active = True
        note = None
        if i == 3 and stop_price is not None and price <= stop_price:
            active = False
            note = "3차 지정가가 초기 손절가 이하 → 무효"
        plan.append({"tranche": i, "weight": weight, "price_ref": price, "provisional": False,
                     "active": active, "note": note})
    return plan
