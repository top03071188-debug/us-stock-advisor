"""engine.decide.sizing — 포지션 사이징과 분할별 계획 손실 재검증 (기획서 3.8).

[고정]
    atr_pct   = atr14_sadj / close_sadj          # 같은 분할 조정 기준의 무차원 비율
    손절 거리  = min(3 × atr_pct, 20%)            # 실제 청산식(S5)과 **같은 식**
    비중      = (리스크 예산 / 손절 거리) × 레짐 배수,  ≤ min(5%, 현금, 섹터 여유)
    계획 손실  = 비중 × 손절 거리 ≤ 0.75%

2·3차 체결**직전**에는 전체 포지션 기준으로 다시 본다:
    (체결 후 총수량 × max(0, 평단 또는 예상체결가 − 실제 손절가)) / 총자산 ≤ 0.75%
    그리고 (체결 후 평가금액 / 총자산) ≤ 단일 5% 상한
    → 넘으면 **수량을 축소**하고, 최소 1주도 안 되면 **거절**한다.
"""

from __future__ import annotations

from dataclasses import dataclass

RISK_PER_TRADE = 0.0075
STOP_ATR_MULT = 3.0
STOP_MAX = 0.20
MAX_POSITION = 0.05
MAX_SECTOR = 0.25
MAX_OPEN_RISK = 0.045


def stop_distance_for(atr_pct: float | None, atr_mult: float = STOP_ATR_MULT,
                      cap: float = STOP_MAX) -> float:
    """`min(3 × atr_pct, 20%)`. ATR 이 없으면 보수적으로 상한(20%)을 쓴다."""
    if atr_pct is None:
        return float(cap)
    value = float(atr_mult) * max(0.0, float(atr_pct))
    return float(min(value, cap))


@dataclass(frozen=True)
class SizeResult:
    weight: float                # 총자산 대비 목표 비중
    stop_distance: float
    planned_loss_pct: float      # 비중 × 손절 거리 (총자산 대비)
    risk_budget_pct: float
    capped_by: str               # risk | position_cap | cash | sector
    atr_pct: float | None

    def as_dict(self) -> dict:
        return {
            "weight": round(self.weight, 6),
            "stop_distance": round(self.stop_distance, 6),
            "planned_loss_pct": round(self.planned_loss_pct, 6),
            "risk_budget_pct": self.risk_budget_pct,
            "capped_by": self.capped_by,
            "atr_pct": None if self.atr_pct is None else round(self.atr_pct, 6),
        }


def size_position(atr_pct: float | None, regime_mult: float = 1.0,
                  risk_per_trade: float = RISK_PER_TRADE,
                  max_position: float = MAX_POSITION,
                  cash_available_pct: float = 1.0,
                  sector_headroom_pct: float = MAX_SECTOR,
                  atr_mult: float = STOP_ATR_MULT, stop_max: float = STOP_MAX) -> SizeResult:
    """레짐 배수는 **비중을 줄이는 방향으로만** 작동한다(3.8). 상한은 그대로다."""
    dist = stop_distance_for(atr_pct, atr_mult, stop_max)
    raw = risk_per_trade / dist if dist > 0 else 0.0
    scaled = raw * max(0.0, float(regime_mult))
    weight = scaled
    capped_by = "risk"
    for name, limit in (("position_cap", max_position), ("cash", cash_available_pct),
                        ("sector", sector_headroom_pct)):
        if weight > limit:
            weight = limit
            capped_by = name
    weight = max(0.0, weight)
    return SizeResult(weight=weight, stop_distance=dist, planned_loss_pct=weight * dist,
                      risk_budget_pct=risk_per_trade, capped_by=capped_by,
                      atr_pct=None if atr_pct is None else float(atr_pct))


def planned_loss_pct(shares: float, entry_price: float, stop_price: float, equity: float) -> float:
    """계획 손실(총자산 대비). 갭 하락 **전** 값이다(3.8)."""
    if equity <= 0:
        return float("inf")
    return shares * max(0.0, float(entry_price) - float(stop_price)) / float(equity)


def recheck_planned_loss(existing_shares: float, existing_avg_cost: float, stop_price: float,
                         requested_shares: float, fill_price: float, equity: float,
                         cash: float | None = None, max_position: float = MAX_POSITION,
                         risk_per_trade: float = RISK_PER_TRADE,
                         min_shares: float = 1e-9) -> dict:
    """2·3차 체결 직전 재검증 — 초과하면 축소, 최소 수량도 안 되면 거절(3.8).

    반환 `action`: `full` | `shrink` | `reject`.
    """
    if equity <= 0 or fill_price <= 0:
        return {"action": "reject", "allowed_shares": 0.0, "requested_shares": float(requested_shares),
                "reason": "INVALID_STATE", "planned_loss_pct": None, "weight_after": None}
    budget = float(equity) * float(risk_per_trade)
    existing_loss = float(existing_shares) * max(0.0, float(existing_avg_cost) - float(stop_price))
    per_share_risk = max(0.0, float(fill_price) - float(stop_price))
    allowed_by_loss = (budget - existing_loss) / per_share_risk if per_share_risk > 0 else float(requested_shares)
    existing_value = float(existing_shares) * float(fill_price)
    allowed_by_cap = (float(equity) * float(max_position) - existing_value) / float(fill_price)
    allowed = min(float(requested_shares), max(0.0, allowed_by_loss), max(0.0, allowed_by_cap))
    if cash is not None:
        allowed = min(allowed, max(0.0, float(cash)) / float(fill_price))
    if allowed <= min_shares:
        return {"action": "reject", "allowed_shares": 0.0, "requested_shares": float(requested_shares),
                "reason": "PLANNED_LOSS_OR_POSITION_CAP", "planned_loss_pct": round(
                    planned_loss_pct(existing_shares, existing_avg_cost, stop_price, equity), 8),
                "weight_after": round(existing_value / equity, 6)}
    action = "full" if allowed >= float(requested_shares) - 1e-12 else "shrink"
    total_shares = float(existing_shares) + allowed
    avg_after = (float(existing_shares) * float(existing_avg_cost) + allowed * float(fill_price)) / total_shares
    return {
        "action": action,
        "allowed_shares": allowed,
        "requested_shares": float(requested_shares),
        "reason": None if action == "full" else "PLANNED_LOSS_OR_POSITION_CAP",
        "planned_loss_pct": round(planned_loss_pct(total_shares, avg_after, stop_price, equity), 8),
        "weight_after": round(total_shares * float(fill_price) / equity, 6),
        "avg_cost_after": round(avg_after, 6),
    }


def open_risk_total(positions: list[dict], equity: float, risk_per_trade: float = RISK_PER_TRADE) -> dict:
    """동시 위험 합계 = Σ(포지션 계획 손실) ≤ 4.5% (0.75% × 6)."""
    total = 0.0
    for p in positions:
        total += planned_loss_pct(p["shares"], p["avg_cost"], p["stop_price"], equity)
    return {
        "open_risk_pct": round(total, 6),
        "limit_pct": MAX_OPEN_RISK,
        "ok": total <= MAX_OPEN_RISK + 1e-12,
        "max_positions": int(round(MAX_OPEN_RISK / risk_per_trade)),
    }
