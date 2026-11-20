"""engine.decide.signals — 2층 매수/매도 트리거·등급·분할 계획 (기획서 §3.4·§3.5).

가격 기준 [고정, v1.3.1]
- 판단(B2 장기추세, B3 안전마진): `split_adj_ohlc` (`sadj_close`·`sadj_dma`).
- 화면·주문·체결·손절: `raw_as_traded` + `atr_pct` **비율**.
- 한 계산 안에서 두 기준을 섞으면 안 된다.

보유 종목 추적 [고정]
- 워치리스트에서 편출돼도 S1~S5·리스크 경고를 **계속** 계산한다. 신규 매수만 막는다.
- 가치평가 보류 업종을 보유하면 S1 을 계산하지 않고 S2·S4·S5 만 적용하며 그 사실을 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.backtest.execution import OrderNotPlaceable, build_entry_band, entry_plan
from engine.decide.macro import RegimeView, regime_adjustments
from engine.decide.sizing import size_position, stop_distance_for

GRADE_ORDER = {"NONE": 0, "WATCH": 1, "BUY": 2, "STRONG_BUY": 3}
NEWS_STATES = ("not_read", "insufficient_evidence", "read_done")


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def margin_of_safety(iv_median: float | None, price_sadj: float | None) -> float | None:
    """`(IV_median − price_adj) / IV_median` — B3·S1 의 기준(3.4.2)."""
    if iv_median is None or price_sadj is None or float(iv_median) <= 0:
        return None
    return (float(iv_median) - float(price_sadj)) / float(iv_median)


def target_band(iv_median: float, price: float, fraction: float = 0.35,
                horizon_td: int = 63) -> dict:
    """4.7.1 목표 밴드 — 달러로 저장하고 채점 때 `base_price` 로 수익률 환산한다.

    3개월: `[P×(1+0.35×gap×0.5), P×(1+0.35×gap)]`, 6개월: 0.60. gap ≤ 0 이면 방어 목표로 별도 표기.
    """
    gap = float(iv_median) / float(price) - 1.0
    lo_raw = float(price) * (1.0 + fraction * gap * 0.5)
    hi_raw = float(price) * (1.0 + fraction * gap)
    lo, hi = min(lo_raw, hi_raw), max(lo_raw, hi_raw)
    return {
        "lo": round(lo, 4),
        "hi": round(hi, 4),
        "horizon_td": int(horizon_td),
        "basis": "base_price",
        "base_price": float(price),
        "r_lo": round(lo / float(price) - 1.0, 6),
        "r_hi": round(hi / float(price) - 1.0, 6),
        "defensive": bool(gap <= 0),
    }


def expected_mdd(daily_vol_60d: float | None, horizon_td: int = 63,
                 low: float = -0.20, high: float = -0.05) -> float | None:
    """`−1.2 × 일간 변동성 × √horizon`, −5%~−20% 클램프 (4.7.1)."""
    if daily_vol_60d is None:
        return None
    value = -1.2 * float(daily_vol_60d) * (float(horizon_td) ** 0.5)
    return round(max(low, min(high, value)), 4)


def timing_score(rsi14: float | None, dma50_deviation: float | None, pct_b: float | None,
                 support_score: float | None = None, volume_score: float | None = None) -> float:
    """T = 0.30×RSI + 0.25×이격 + 0.20×%B + 0.15×지지 + 0.10×거래량 (3.4.3)."""
    parts: list[tuple[float, float]] = []
    if rsi14 is not None:
        parts.append((0.30, 100.0 * _clip01((70.0 - float(rsi14)) / 40.0)))
    if dma50_deviation is not None:
        parts.append((0.25, 100.0 * _clip01(0.5 + (float(dma50_deviation) / -0.08) * 0.5)))
    if pct_b is not None:
        parts.append((0.20, 100.0 * _clip01(1.0 - (float(pct_b) - 0.2) / 0.8)))
    parts.append((0.15, 100.0 * _clip01(0.5 if support_score is None else support_score)))
    parts.append((0.10, 100.0 * _clip01(0.5 if volume_score is None else volume_score)))
    total_w = sum(w for w, _ in parts)
    return round(sum(w * v for w, v in parts) / total_w, 2) if total_w else 0.0


def dip_conditions(rsi14: float | None, dma50_deviation: float | None, pct_b: float | None,
                   rebound_after_low: bool = False, rsi_buy: float = 35.0,
                   dma50_threshold: float = -0.08) -> dict:
    """눌림 조건 4가지 (3.4.2) — B4 는 2개 이상."""
    return {
        "rsi_le_threshold": bool(rsi14 is not None and float(rsi14) <= rsi_buy),
        "below_dma50": bool(dma50_deviation is not None and float(dma50_deviation) <= dma50_threshold),
        "pctb_le_0_2": bool(pct_b is not None and float(pct_b) <= 0.2),
        "rebound_after_low": bool(rebound_after_low),
    }


def entry_score(q: float | None, v: float | None, t: float | None, m: float | None,
                g: float | None, weights: dict | None = None) -> dict:
    """`0.30Q + 0.25V + 0.20T + 0.15M + 0.10G`, Q+V ≥ 0.45 제약 [고정]."""
    w = {"quality": 0.30, "valuation": 0.25, "timing": 0.20, "macro": 0.15, "growth": 0.10}
    if weights:
        w.update({k: float(weights[k]) for k in w if k in weights})
    if abs(sum(w.values()) - 1.0) > 1e-9:
        raise ValueError(f"매수 가중치 합이 1이 아니다: {w}")
    if w["quality"] + w["valuation"] < 0.45 - 1e-9:
        raise ValueError("Q+V 가중치 합이 0.45 미만 — 품질·가치가 판단의 절반 이상이어야 한다(3.4.1)")
    parts = {"quality": q, "valuation": v, "timing": t, "macro": m, "growth": g}
    available = {k: float(x) for k, x in parts.items() if x is not None}
    if "quality" not in available or "valuation" not in available:
        return {"score": None, "components": parts, "weights": w}
    total_w = sum(w[k] for k in available)
    score = sum(w[k] * available[k] for k in available) / total_w
    return {"score": round(score, 2), "components": parts, "weights": w}


def investor_scores(q: float | None, v: float | None, g: float | None, m: float | None) -> dict:
    """대가별 점수(-100~+100) — 0~100 복합점수를 `2c−100` 으로 옮긴 결정론 값(3.3)."""
    def conv(composite: float | None) -> float | None:
        return None if composite is None else round(max(-100.0, min(100.0, 2.0 * float(composite) - 100.0)), 1)

    vv = v if v is not None else 50.0
    qq = q if q is not None else 50.0
    gg = g if g is not None else 50.0
    mm = m if m is not None else 50.0
    return {
        "buffett": conv(0.55 * vv + 0.30 * qq + 0.15 * 50.0),
        "graham": conv(0.50 * vv + 0.30 * qq + 0.20 * 50.0),
        "lynch": conv(gg),
        "druckenmiller": conv(mm),
    }


@dataclass
class EntryContext:
    """한 종목의 2층 판단 입력(스냅샷 시점). 지표는 이미 계산된 값만 받는다."""

    ticker: str
    raw_close: float
    sadj_close: float
    sector: str = ""
    dma50: float | None = None
    dma200: float | None = None
    dma200_slope: float | None = None          # 200일선의 20일 기울기(양수 = 상승)
    rsi14: float | None = None
    pct_b: float | None = None
    dma50_deviation: float | None = None       # None 이면 dma50 으로 계산
    dma20_sigma: float | None = None
    atr_pct: float | None = None
    iv_median: float | None = None
    iv_high: float | None = None
    ev_ebit_percentile: float | None = None
    q: float | None = None
    v: float | None = None
    g: float | None = None
    structural_risk: float = 0.0
    support_score: float | None = None
    volume_score: float | None = None
    rebound_after_low: bool = False
    in_watchlist: bool = True
    held: bool = False
    valuation_suspended: bool = False
    news_state: str = "not_read"
    roic_wacc_2q_negative: bool = False
    rel_strength_pctile: float | None = None
    evidence: list[dict] = field(default_factory=list)
    vetoes: list[dict] = field(default_factory=list)
    invalidation: dict | None = None

    def dev50(self) -> float | None:
        if self.dma50_deviation is not None:
            return self.dma50_deviation
        if self.dma50 and self.dma50 > 0 and self.sadj_close:
            return self.sadj_close / float(self.dma50) - 1.0
        return None


def _regime_view(regime) -> RegimeView:
    if isinstance(regime, RegimeView):
        return regime
    name = str(regime)
    return RegimeView(regime=name, votes={}, votes_risk_on=0, votes_risk_off=0,
                      inputs_missing=(), verified=False)


def evaluate_entry(ctx: EntryContext, regime="neutral", params: dict | None = None) -> dict:
    """B0~B6 게이트 → 등급 → (BUY 이상이면) 분할 계획. 진입 불가는 사유를 남긴다."""
    view = _regime_view(regime)
    cfg = params or {}
    entry_cfg = cfg.get("entry_gates", {})
    grade_cfg = cfg.get("grades", {})
    weights_cfg = cfg.get("entry_weights", {})

    t = timing_score(ctx.rsi14, ctx.dev50(), ctx.pct_b, ctx.support_score, ctx.volume_score)
    comp = entry_score(ctx.q, ctx.v, t, view.score, ctx.g, weights_cfg)
    scores = investor_scores(ctx.q, ctx.v, ctx.g, view.score)
    spread = None if None in scores.values() else max(scores.values()) - min(scores.values())
    margin = margin_of_safety(ctx.iv_median, ctx.sadj_close)
    required = float(entry_cfg.get("margin_of_safety", {}).get(view.regime, view.margin_of_safety))
    dips = dip_conditions(ctx.rsi14, ctx.dev50(), ctx.pct_b, ctx.rebound_after_low,
                          float(entry_cfg.get("rsi_buy", 35)), float(entry_cfg.get("dma50_deviation", -0.08)))
    dip_count = sum(1 for v in dips.values() if v)

    if ctx.valuation_suspended:
        return {
            "ticker": ctx.ticker, "signal": "SUSPENDED", "grade": "NONE",
            "reason": "SECTOR_FORMULA_PENDING", "valuation_suspended": True,
            "margin": None, "timing": t, "entry_score": None, "investor_scores": scores,
            "conflict": False, "gates": {}, "dip_conditions": dips, "veto": ctx.vetoes,
            "note_ko": "가치평가 보류 — 업종 산식 준비 중(3.2.1). 신호를 발행하지 않는다.",
        }

    b1 = bool(ctx.in_watchlist and not ctx.held)
    if ctx.dma200 is not None:
        b2_trend = ctx.sadj_close > float(ctx.dma200) * float(entry_cfg.get("dma200_mult", 0.97))
        b2_slope = bool(ctx.dma200_slope is not None and ctx.dma200_slope > 0 and
                        ctx.dma50 is not None and ctx.sadj_close > float(ctx.dma50))
        b2 = bool(b2_trend or b2_slope)
    else:
        b2 = False
    b3 = bool(margin is not None and margin >= required)
    b4 = bool(t >= float(entry_cfg.get("timing_min", 55)) and
              dip_count >= int(entry_cfg.get("dip_conditions_required", 2)))
    b5 = bool(float(ctx.structural_risk) < float(entry_cfg.get("structural_risk_block", 55)))
    b6 = not any(v.get("blocking", True) for v in ctx.vetoes)
    value_or_growth = max([s for k, s in scores.items() if k in ("buffett", "graham", "lynch") and s is not None],
                          default=None)
    b0 = bool(value_or_growth is not None and value_or_growth >= 20)

    gates = {"B0": b0, "B1": b1, "B2": b2, "B3": b3, "B4": b4, "B5": b5, "B6": b6}
    reasons = {
        "B0": "대가 쿼럼 미달(가치·성장 점수 +20 이상 필요)",
        "B1": "워치리스트 밖 또는 보유 종목(추가 매수 금지)",
        "B2": "장기 추세 이탈(200일선 −3% 이하 & 기울기 하락)",
        "B3": f"안전마진 {margin:.1%} < 임계 {required:.0%}" if margin is not None else "안전마진 계산 불가",
        "B4": f"눌림 조건 {dip_count}/4 · T {t}",
        "B5": f"구조적 리스크 {ctx.structural_risk} ≥ 차단 임계",
        "B6": "Veto 발동: " + ", ".join(v["code"] for v in ctx.vetoes),
    }
    failed = [k for k, ok in gates.items() if not ok]
    score = comp["score"]
    grade = "NONE"
    if score is not None:
        if not failed and score >= float(grade_cfg.get("strong_buy", 80)) and (spread or 0) < 60:
            grade = "STRONG_BUY"
        elif not failed and score >= float(grade_cfg.get("buy", 68)):
            grade = "BUY"
        elif score >= float(grade_cfg.get("watch", 55)):
            grade = "WATCH"
    cap_reasons: list[str] = []
    if ctx.g is not None and float(ctx.g) <= 0 and GRADE_ORDER[grade] > GRADE_ORDER["WATCH"]:
        grade = "WATCH"
        cap_reasons.append("성장률 ≤ 0 → 매수 등급 상한 WATCH(3.2.4)")
    if ctx.news_state == "insufficient_evidence" and GRADE_ORDER[grade] > GRADE_ORDER["WATCH"]:
        grade = "WATCH"
        cap_reasons.append("뉴스 근거 부족 → 매수 등급 상한 WATCH(3.9.4)")

    return {
        "ticker": ctx.ticker,
        "signal": "BUY" if grade in ("BUY", "STRONG_BUY") else ("WATCH" if grade == "WATCH" else "NONE"),
        "grade": grade,
        "entry_score": score,
        "components": comp["components"],
        "timing": t,
        "margin": None if margin is None else round(margin, 4),
        "required_margin": required,
        "investor_scores": scores,
        "conflict": bool(spread is not None and spread >= 60),
        "spread": None if spread is None else round(spread, 1),
        "gates": gates,
        "failed_gates": failed,
        "gate_reasons": {k: reasons[k] for k in failed},
        "dip_conditions": dips,
        "dip_count": dip_count,
        "cap_reasons": cap_reasons,
        "veto": ctx.vetoes,
        "structural_risk": ctx.structural_risk,
        "valuation_suspended": False,
    }


def build_entry_plan(ctx: EntryContext, evaluation: dict, regime="neutral",
                     params: dict | None = None, total_asset: float = 1.0,
                     allow_watch: bool = False) -> dict:
    """분할 계획(1차 경매 범위·2·3차 지정가·손절가·비중). 화면과 시뮬레이터가 같은 식을 쓴다.

    WATCH 는 **관찰**이므로 주문을 걸지 않는다(3.4.4). `allow_watch=True` 는 화면 미리보기용이다.
    """
    view = _regime_view(regime)
    if evaluation.get("signal") not in ("BUY", "WATCH"):
        return {"placeable": False, "reason": "NOT_A_BUY_SIGNAL"}
    if evaluation.get("signal") == "WATCH" and not allow_watch:
        return {"placeable": False, "reason": "GRADE_BELOW_BUY"}
    if ctx.iv_median is None or ctx.dma200 is None:
        return {"placeable": False, "reason": "IV_OR_DMA200_MISSING"}
    idx = params or {}
    entry_cfg = idx.get("entry_gates", {})
    size = size_position(ctx.atr_pct, regime_mult=view.multiplier)
    dist = size.stop_distance
    band = None
    band_error = None
    try:
        band = build_entry_band(
            iv_median=ctx.iv_median,
            required_margin=evaluation.get("required_margin", view.margin_of_safety),
            dma200_sadj=ctx.dma200,
            signal_price=ctx.raw_close,
            stop_distance=dist,
            dma200_mult=float(entry_cfg.get("dma200_mult", 0.97)),
        ).as_dict()
    except OrderNotPlaceable as exc:
        band_error = str(exc)
    provisional_stop = round(ctx.raw_close * (1.0 - dist), 4)
    plan = entry_plan(ctx.raw_close, stop_price=provisional_stop)
    for row in plan:
        # 신호 시점의 분할가는 **잠정값**이다 — 1차 실제 체결가로 재고정(plan_reanchor)한다(4.7.1).
        row["provisional"] = True
        if row["tranche"] == 1:
            row["price_ref"] = ctx.raw_close
    tradable = [r for r in plan if r["active"]]
    effective_weight = round(sum(r["weight"] for r in tradable) * size.weight, 6)
    return {
        "placeable": band is not None,
        "reason": None if band is not None else "outside_order_range",
        "band_error": band_error,
        "entry_band": band,
        "signal_price": ctx.raw_close,
        "stop_distance": round(dist, 6),
        "provisional_stop": provisional_stop,
        "atr_pct": ctx.atr_pct,
        "iv_high": ctx.iv_high,
        "plan_weight": round(size.weight, 6),
        "planned_loss_pct": round(size.planned_loss_pct, 6),
        "sizing": size.as_dict(),
        "tranches": plan,
        "effective_weight": effective_weight,
        "tranche_note_ko": (None if len(tradable) == 3 else
                            "3차 조건은 손절선 밖이라 실행되지 않습니다(실효 분할 70%)."),
        "regime": view.regime,
        "regime_multiplier": view.multiplier,
        "total_asset_basis": total_asset,
    }


def evaluate_exit(ctx: EntryContext, holding: dict, regime="neutral",
                  params: dict | None = None) -> dict:
    """S1~S5 + exit_score → EXIT / TRIM / WATCH_DOWN / HOLD (3.5).

    `holding` 은 `{shares, avg_cost, stop_price, trailing_high, initial_stop}` 를 담는다.
    """
    view = _regime_view(regime)
    cfg = (params or {}).get("exit", {})
    margin = None if ctx.valuation_suspended else margin_of_safety(ctx.iv_median, ctx.sadj_close)
    s1 = None if ctx.valuation_suspended else bool(
        (margin is not None and margin <= float(cfg.get("margin_overheat", -0.15))) or
        (ctx.ev_ebit_percentile is not None and ctx.ev_ebit_percentile >= float(cfg.get("pe_percentile", 0.90)))
    )
    s2 = bool(ctx.rsi14 is not None and float(ctx.rsi14) >= float(cfg.get("rsi_overheat", 72)) and
              ctx.pct_b is not None and float(ctx.pct_b) >= 1.0 and
              ctx.dma20_sigma is not None and float(ctx.dma20_sigma) >= 2.0)
    s3 = bool(ctx.roic_wacc_2q_negative or float(ctx.structural_risk) >= float(cfg.get("structural_exit", 70)) or
              any(v.get("code") in ("V03", "V09") for v in ctx.vetoes))
    s4 = bool(ctx.dma200 is not None and ctx.dma200_slope is not None and
              ctx.sadj_close < float(ctx.dma200) and float(ctx.dma200_slope) < 0 and
              ctx.rel_strength_pctile is not None and float(ctx.rel_strength_pctile) <= 0.30)
    stop_price = holding.get("stop_price")
    ref_low = ctx.raw_close if holding.get("raw_low") is None else holding["raw_low"]
    s5 = bool(stop_price is not None and float(ref_low) <= float(stop_price))

    val_heat = 100.0 if s1 else (0.0 if margin is None else 100.0 * _clip01((0.10 - margin) / 0.25))
    tech_heat = 100.0 if s2 else (0.0 if ctx.rsi14 is None else
                                 100.0 * _clip01((float(ctx.rsi14) - 50.0) / 30.0))
    struct_heat = max(0.0, min(100.0, float(ctx.structural_risk)))
    macro_heat = {"risk_off": 100.0, "neutral": 50.0, "risk_on": 0.0}[view.regime]
    exit_score = round(0.35 * val_heat + 0.25 * tech_heat + 0.25 * struct_heat + 0.15 * macro_heat, 2)
    if s3 or s5:
        verdict = "EXIT"
    elif exit_score >= float(cfg.get("exit_score", 75)):
        verdict = "EXIT"
    elif exit_score >= float(cfg.get("trim_score", 60)):
        verdict = "TRIM"
    elif exit_score >= 45:
        verdict = "WATCH_DOWN"
    else:
        verdict = "HOLD"
    return {
        "ticker": ctx.ticker,
        "verdict": verdict,
        "exit_score": exit_score,
        "triggers": {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5},
        "forced": bool(s3 or s5),
        "cancel_pending_tranches": bool(s3 or s5 or verdict == "EXIT"),
        "margin": None if margin is None else round(margin, 4),
        "valuation_suspended": bool(ctx.valuation_suspended),
        "note_ko": ("가치평가 보류 업종 — S1 은 계산하지 않고 S2·S4·S5 만 적용한다."
                    if ctx.valuation_suspended else None),
        "reason_ko": {
            "S1": "밸류에이션 과열", "S2": "기술적 과열", "S3": "구조적 훼손",
            "S4": "추세 이탈", "S5": "손실 한도(트레일링 스톱)",
        },
    }


def evaluate_holdings(holdings: list[dict], contexts: dict[str, EntryContext],
                      watchlist: set[str] | None = None, regime="neutral",
                      params: dict | None = None) -> dict:
    """보유 종목 추적 — 워치리스트 편출과 무관하게 S1~S5 를 매번 계산한다(3.2.5)."""
    watch = set(watchlist or ())
    rows: list[dict] = []
    for h in holdings:
        ticker = h["ticker"]
        ctx = contexts.get(ticker)
        if ctx is None:
            rows.append({"ticker": ticker, "verdict": "NO_DATA", "triggers": {},
                         "error": "컨텍스트 없음 — 가격·지표 조회 실패"})
            continue
        exit_eval = evaluate_exit(ctx, h, regime, params)
        outside = ticker not in watch
        rows.append({
            **exit_eval,
            "in_watchlist": not outside,
            "badge_ko": "워치리스트 외(보유)" if outside else None,
            "new_buy_allowed": False if outside else None,
            "sell_card": exit_eval["verdict"] in ("EXIT", "TRIM", "WATCH_DOWN"),
        })
    return {
        "holdings": rows,
        "exit_count": sum(1 for r in rows if r.get("verdict") == "EXIT"),
        "tracked_outside_watchlist": sum(1 for r in rows if r.get("badge_ko")),
        "note_ko": "보유 종목은 워치리스트를 따라가지 않는다 — 편출돼도 매도·경고는 계속 계산한다.",
    }


def default_params() -> dict:
    """`config/params_v001.yaml` 의 관련 절만 기본값으로 (테스트·데모용)."""
    return {
        "entry_weights": {"quality": 0.30, "valuation": 0.25, "timing": 0.20, "macro": 0.15, "growth": 0.10},
        "entry_gates": {"margin_of_safety": {"risk_on": 0.15, "neutral": 0.20, "risk_off": 0.25},
                        "rsi_buy": 35, "dma50_deviation": -0.08, "dma200_mult": 0.97,
                        "timing_min": 55, "dip_conditions_required": 2, "structural_risk_block": 55},
        "grades": {"strong_buy": 80, "buy": 68, "watch": 55},
        "exit": {"margin_overheat": -0.15, "rsi_overheat": 72, "pe_percentile": 0.90,
                 "structural_exit": 70, "exit_score": 75, "trim_score": 60},
        "risk": {"risk_per_trade": 0.0075, "stop_atr_mult": 3.0, "stop_max": 0.20,
                 "max_position": 0.05, "max_sector": 0.25, "cash_min": 0.10, "cash_min_risk_off": 0.30},
        "scaling": {"entry": [0.40, 0.30, 0.30], "entry_drops": [0.0, -0.07, -0.14]},
    }
