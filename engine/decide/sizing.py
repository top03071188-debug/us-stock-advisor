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

[v1.3.4 — P0-3] 체결 직전 검사는 **종목 상한 하나로 끝나지 않는다**(감사 P0-3·track-2 부록 A-1).
같은 섹터에 여러 주문이 같은 봉에 닿으면 종목 상한 5%씩만 지켜도 섹터 25%를 넘었다(재현 30.00%).
그래서 `recheck_planned_loss` 는 **섹터 상한**(`max_sector`·`sector_value`)과 **전체 미청산 위험**
(`open_risk`·`open_risk_limit`)을 선택 인자로 받는다. 기본값(`max_sector=None`·`open_risk=0.0`)은
기존 호출과 100% 같은 결과다 — 새 제약은 호출자가 현재 섹터·위험을 알려 줄 때만 적용된다.
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
                         min_shares: float = 1e-9, max_sector: float | None = None,
                         sector_value: float = 0.0, open_risk: float = 0.0,
                         open_risk_limit: float = MAX_OPEN_RISK) -> dict:
    """체결 직전 재검증 — 초과하면 축소, 최소 수량도 안 되면 거절(3.8 · 4.12 · P0-3).

    검사 순서(고정): ① 계획 손실 예산 ② 단일 종목 상한 ③ **섹터 상한** ④ **전체 미청산 위험**
    ⑤ 현금. 어느 하나라도 모자라면 **가장 작은 수량**을 쓴다(부분 체결 = 축소).

    - `sector_value`: 같은 섹터의 **다른 종목** 현재 평가액(달러). 이 종목의 기존 보유는 따로 센다.
      `max_sector` 가 `None` 이면 섹터 검사를 하지 않는다(기존 호출 호환).
    - `open_risk`: 이 포지션을 **뺀** 전체 미청산 위험(총자산 대비 비율). `0.0` 이면 미적용.

    반환 `action`: `full` | `shrink` | `reject`. `capped_by` 는 실제로 수량을 깎은 제약이다.
    """
    if equity <= 0 or fill_price <= 0:
        return {"action": "reject", "allowed_shares": 0.0, "requested_shares": float(requested_shares),
                "reason": "INVALID_STATE", "planned_loss_pct": None, "weight_after": None,
                "capped_by": None}
    fill_price = float(fill_price)
    budget = float(equity) * float(risk_per_trade)
    existing_loss = float(existing_shares) * max(0.0, float(existing_avg_cost) - float(stop_price))
    per_share_risk = max(0.0, fill_price - float(stop_price))
    allowed_by_loss = (budget - existing_loss) / per_share_risk if per_share_risk > 0 else float(requested_shares)
    existing_value = float(existing_shares) * fill_price
    limits: list[tuple[str, float]] = [
        ("PLANNED_LOSS", max(0.0, allowed_by_loss)),
        ("POSITION_CAP", max(0.0, (float(equity) * float(max_position) - existing_value) / fill_price)),
    ]
    if max_sector is not None:
        # 다른 종목의 섹터 평가액 + 이 종목의 기존·신규 평가액 ≤ 총자산 × 섹터 상한
        headroom = float(equity) * float(max_sector) - float(sector_value) - existing_value
        limits.append(("SECTOR_CAP", max(0.0, headroom / fill_price)))
    if float(open_risk or 0.0) > 0:
        risk_headroom = (float(open_risk_limit) - float(open_risk)) * float(equity) - existing_loss
        if per_share_risk > 0:
            limits.append(("OPEN_RISK", max(0.0, risk_headroom / per_share_risk)))
        elif risk_headroom < 0:
            limits.append(("OPEN_RISK", 0.0))
    if cash is not None:
        limits.append(("CASH", max(0.0, float(cash)) / fill_price))
    allowed = min([float(requested_shares)] + [value for _, value in limits])
    capped_by = next((name for name, value in limits if value < float(requested_shares) - 1e-12), None)
    if allowed <= min_shares:
        binding = capped_by or "PLANNED_LOSS_OR_POSITION_CAP"
        return {"action": "reject", "allowed_shares": 0.0, "requested_shares": float(requested_shares),
                "reason": "SECTOR_CAP" if binding == "SECTOR_CAP" else "PLANNED_LOSS_OR_POSITION_CAP",
                "capped_by": binding,
                "planned_loss_pct": round(
                    planned_loss_pct(existing_shares, existing_avg_cost, stop_price, equity), 8),
                "weight_after": round(existing_value / equity, 6)}
    action = "full" if allowed >= float(requested_shares) - 1e-12 else "shrink"
    total_shares = float(existing_shares) + allowed
    avg_after = (float(existing_shares) * float(existing_avg_cost) + allowed * fill_price) / total_shares
    return {
        "action": action,
        "allowed_shares": allowed,
        "requested_shares": float(requested_shares),
        "reason": None if action == "full" else (capped_by or "PLANNED_LOSS_OR_POSITION_CAP"),
        "capped_by": capped_by,
        "planned_loss_pct": round(planned_loss_pct(total_shares, avg_after, stop_price, equity), 8),
        "weight_after": round(total_shares * fill_price / equity, 6),
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
