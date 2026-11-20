"""engine.backtest.execution — 개장 경매 주문 범위(1차 분할)와 체결 판정 [고정, v1.3.1].

확정 시가를 보고 게이트를 검사한 뒤 같은 시가에 체결하면 미래 참조다. 그래서 1차는
**장 시작 전에 계산한 주문 범위**(`stop_min` < P ≤ `limit_max`)를 커밋하고,
그 범위에서 성립한 경매 체결가만 인정한다.

    limit_max = IV_median × (1 − required_margin)          # B3 안전마진 상한
    stop_min  = max(DMA200_sadj × 0.97,                    # B2 200일선 하한
                    signal_price_sadj × (1 − 잠정 손절 거리))  # 잠정 손절가 하한

가격 기준 [고정, 3.4.4 · 2.4.1]
- 밴드는 **모두 `split_adj_ohlc`(sadj) 한 기준**으로 만든다. `signal_price`·`iv_median`·`dma200` 이
  같은 기준이어야 상·하한이 서로 비교된다.
- 체결 판정 때만 그 세션의 `split_factor` 로 **한 번** 환산해 `raw` 시가와 비교한다
  (`raw_t = sadj_t × split_factor(t)`). 화면에 보여 줄 때도 같은 환산을 쓴다(`display_raw`).
- 한 계산 안에서 두 기준을 섞으면 안 된다 — `build_entry_band` 는 raw 인자를 **받지 않는다**.
"""

from __future__ import annotations

from dataclasses import dataclass


class OrderNotPlaceable(ValueError):
    """stop_min ≥ limit_max — 그날 1차 진입 자체가 불가능하다."""


class PriceBasisError(TypeError):
    """밴드에 raw 시세를 넣었다 — 인자 이름으로 강제한다(2.4.1)."""


@dataclass(frozen=True, init=False)
class EntryBand:
    """주문 범위 — **`split_adj_ohlc` 단일 기준**.

    `stop_min_sadj < P_sadj ≤ limit_max_sadj` 가 성립하는 경매 체결만 인정한다.
    `split_factor` 는 신호일의 누적 분할계수(화면용 raw 환산에만 쓴다).

    하위 호환: 예전 코드(모의 계좌·데모)가 쓰던 `stop_min`·`limit_max` 등 이름을 생성자와
    프로퍼티 양쪽에서 그대로 받는다. 값은 같은 sadj 기준이며 `split_factor=1.0` 이면 raw 와 같다.
    """

    stop_min_sadj: float
    limit_max_sadj: float
    dma200_floor_sadj: float
    provisional_stop_sadj: float
    required_margin: float
    split_factor: float = 1.0

    def __init__(self, stop_min_sadj: float | None = None, limit_max_sadj: float | None = None,
                 dma200_floor_sadj: float | None = None, provisional_stop_sadj: float | None = None,
                 required_margin: float = 0.0, split_factor: float = 1.0, *,
                 stop_min: float | None = None, limit_max: float | None = None,
                 dma200_floor: float | None = None, provisional_stop: float | None = None) -> None:
        values = {
            "stop_min_sadj": stop_min_sadj if stop_min_sadj is not None else stop_min,
            "limit_max_sadj": limit_max_sadj if limit_max_sadj is not None else limit_max,
            "dma200_floor_sadj": dma200_floor_sadj if dma200_floor_sadj is not None else dma200_floor,
            "provisional_stop_sadj": (provisional_stop_sadj if provisional_stop_sadj is not None
                                      else provisional_stop),
        }
        missing = [k for k, v in values.items() if v is None]
        if missing:
            raise TypeError(f"EntryBand 에 필요한 값이 없다: {missing}")
        for field_name, value in values.items():
            object.__setattr__(self, field_name, float(value))
        object.__setattr__(self, "required_margin", float(required_margin))
        object.__setattr__(self, "split_factor", float(split_factor) if split_factor else 1.0)

    # ── 하위 호환 프로퍼티(같은 sadj 값) ────────────────────────────────────
    @property
    def stop_min(self) -> float:
        return self.stop_min_sadj

    @property
    def limit_max(self) -> float:
        return self.limit_max_sadj

    @property
    def dma200_floor(self) -> float:
        return self.dma200_floor_sadj

    @property
    def provisional_stop(self) -> float:
        return self.provisional_stop_sadj

    def contains_sadj(self, price_sadj: float) -> bool:
        return self.stop_min_sadj < float(price_sadj) <= self.limit_max_sadj

    def contains(self, price: float) -> bool:
        """sadj 기준 판정(이름 하위 호환용) — raw 가격을 넣으면 안 된다."""
        return self.contains_sadj(price)

    def to_raw(self, price_sadj: float, split_factor: float | None = None) -> float:
        factor = self.split_factor if split_factor is None else float(split_factor)
        return float(price_sadj) * factor

    def as_dict(self) -> dict:
        factor = self.split_factor
        return {
            "stop_min_sadj": round(self.stop_min_sadj, 4),
            "limit_max_sadj": round(self.limit_max_sadj, 4),
            "dma200_floor_sadj": round(self.dma200_floor_sadj, 4),
            "provisional_stop_sadj": round(self.provisional_stop_sadj, 4),
            "required_margin": self.required_margin,
            "split_factor": factor,
            "basis": "split_adj_ohlc",
            "display_raw": {
                "stop_min": round(self.stop_min_sadj * factor, 4),
                "limit_max": round(self.limit_max_sadj * factor, 4),
                "dma200_floor": round(self.dma200_floor_sadj * factor, 4),
                "provisional_stop": round(self.provisional_stop_sadj * factor, 4),
            },
        }


def band_from_dict(band: dict) -> EntryBand | None:
    """레코드의 `entry_band` dict → `EntryBand`. 구 형식은 raw 로 보고 sadj 로 승격한다.

    구 레코드(`stop_min` 등만 있는 형식)는 그 값이 그대로 sadj 라고 간주하고 `split_factor=1.0`
    으로 읽는다 — 신호일 최신봉의 `split_factor` 는 항상 1.0 이므로 라이브 값은 바뀌지 않는다.
    """
    if not band:
        return None
    try:
        if "stop_min_sadj" in band:
            return EntryBand(
                stop_min_sadj=float(band["stop_min_sadj"]),
                limit_max_sadj=float(band["limit_max_sadj"]),
                dma200_floor_sadj=float(band["dma200_floor_sadj"]),
                provisional_stop_sadj=float(band["provisional_stop_sadj"]),
                required_margin=float(band["required_margin"]),
                split_factor=float(band.get("split_factor") or 1.0),
            )
        return EntryBand(
            stop_min=float(band["stop_min"]), limit_max=float(band["limit_max"]),
            dma200_floor=float(band["dma200_floor"]),
            provisional_stop=float(band["provisional_stop"]),
            required_margin=float(band["required_margin"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_entry_band(iv_median_sadj: float, required_margin: float, dma200_sadj: float,
                     signal_price_sadj: float, stop_distance: float, dma200_mult: float = 0.97,
                     split_factor: float = 1.0) -> EntryBand:
    """**전일 종가까지만** 쓴다. 여기에 당일 시가·고가·저가가 들어가면 미래 참조(3.4.4).

    인자는 모두 `split_adj_ohlc` 기준이다(`*_sadj`). raw 가격(당시 거래가)을 넣으면
    하한과 상한이 다른 기준이 되어 판정이 틀어진다 — 이름으로 막는다.
    """
    if float(signal_price_sadj) <= 0:
        raise ValueError("signal_price_sadj 는 0보다 커야 한다")
    limit_max = float(iv_median_sadj) * (1.0 - float(required_margin))
    dma_floor = float(dma200_sadj) * dma200_mult
    provisional_stop = float(signal_price_sadj) * (1.0 - float(stop_distance))
    stop_min = max(dma_floor, provisional_stop)
    if stop_min >= limit_max:
        raise OrderNotPlaceable(
            f"stop_min({stop_min:.2f}) ≥ limit_max({limit_max:.2f}) — 1차 주문 불가(사유 기록)"
        )
    return EntryBand(stop_min_sadj=stop_min, limit_max_sadj=limit_max, dma200_floor_sadj=dma_floor,
                     provisional_stop_sadj=provisional_stop, required_margin=float(required_margin),
                     split_factor=split_factor)


def opening_fill(band: EntryBand, open_price: float, slippage_bps: float = 10.0,
                 split_factor_open: float | None = None) -> dict:
    """경매 체결가가 범위 안이면 체결, 아니면 미체결(사유를 남긴다).

    `open_price` 는 그 세션의 **raw 시가**이고, 판정은 세션의 `split_factor` 로 sadj 로 환산해
    한다. 반환하는 `fill_price` 는 다시 raw 로 돌린 값(체결·손절은 raw 기준, 4.2.2).
    """
    factor = band.split_factor if split_factor_open is None else float(split_factor_open or 1.0)
    open_sadj = float(open_price) / factor
    if not band.contains_sadj(open_sadj):
        return {
            "execution_event": "no_action",
            "fill_price": None,
            "rejected_reason": "outside_order_range",
            "open_price": float(open_price),
            "open_price_sadj": round(open_sadj, 4),
            "split_factor": factor,
            "band": band.as_dict(),
        }
    fill_sadj = min(open_sadj * (1.0 + slippage_bps / 10_000.0), band.limit_max_sadj)
    return {
        "execution_event": "buy_filled",
        "fill_price": round(fill_sadj * factor, 4),
        "fill_price_sadj": round(fill_sadj, 4),
        "slippage_bps": slippage_bps,
        "rejected_reason": None,
        "open_price": float(open_price),
        "open_price_sadj": round(open_sadj, 4),
        "split_factor": factor,
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
