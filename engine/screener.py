"""engine.screener — 1층 하드게이트 · Q/V/G 점수 · 워치리스트 확정.

[고정]
- 최소 개수 강제 없음: 통과 종목이 9개면 9개, 0개면 0개가 정상이다(3.2.5).
- 은행·보험·REIT 는 `valuation_suspended: true` / `SECTOR_FORMULA_PENDING` 으로 **신호·워치리스트 대상에서 제외**한다.
- 정렬: `0.5×Q + 0.3×V + 0.2×G`, 섹터당 최대 6개, 최대 40개.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.metrics.fundamentals import AnnualFundamentals, growth_score, hard_gate_checks, quality_score
from engine.metrics.valuation import SUSPEND_REASON, classify_sector, v_score


@dataclass
class Candidate:
    ticker: str
    sector: str
    market_cap_usd: float
    adv20_usd: float
    price: float
    history_rows: int
    exchange: str = "NYSE"
    xbrl_completeness: float = 1.0
    accounting_flags: list[str] = field(default_factory=list)   # 8-K 4.02·감사의견·상폐통지
    fundamentals: AnnualFundamentals | None = None
    margin: float | None = None            # 안전마진(B3 기준, sadj 가격) — **원시값**
    v_penalties: float = 0.0               # v1.3.2: DCF 민감도 −4 · 역사 백분위 괴리 −10
    # ── v1.3.3 04: 감점 전 V·부호 있는 괴리·표본 품질을 함께 보관한다(화면·JSON 검증용) ──
    v_raw: float | None = None             # 감점 전 V 점수(안전마진만)
    hist_divergence_signed: float | None = None   # (모델 IV − 역사 IV)/역사 IV — 부호 유지
    ev_ebit_percentile: float | None = None  # 10년 EV/EBIT 백분위(0~100)
    ev_ebit_current: float | None = None
    ev_ebit_median_10y: float | None = None
    ev_ebit_samples: int | None = None
    ev_ebit_years: int | None = None
    ev_ebit_window: str | None = None
    ev_ebit_reason: str | None = None
    # ── v1.3.3 05: 성장률 클립·유효 연수·민감도 ──
    growth: float | None = None
    growth_raw_cagr: float | None = None
    growth_clipped_to_zero: bool | None = None
    effective_fcff_years: int | None = None
    normalization_method: str | None = None
    dcf_sensitivity: float | None = None
    dcf_dropped: str | None = None         # 05: SHORT_HISTORY·FCFF_NONPOSITIVE — "왜 DCF 가 빠졌나"
    wacc_sensitivity: float | None = None
    terminal_g_sensitivity: float | None = None
    peg: float | None = None
    revenue_cagr: float | None = None
    roic_wacc_2q_negative: bool = False


GATE_LABELS = {
    "HG-01": "시가총액 < $10B", "HG-02": "20일 평균 거래대금 < $30M", "HG-03": "주가 < $5",
    "HG-04": "데이터 이력 부족(5년/12분기)", "HG-05": "FCF 양수 연도 < 4/5",
    "HG-06": "순부채/EBITDA > 2.5", "HG-07": "이자보상배율 < 6배", "HG-08": "FCF/순이익 < 0.7",
    "HG-09": "3년 희석 > 5%", "HG-09_UNVERIFIED": "3년 희석 판정 불가(주식수 기준 미확인)",
    "HG-10": "회계 신뢰 이슈", "HG-11": "XBRL 완전성 < 90%",
    "HG-12": "비상장(OTC) 종목",
}


def hard_gates(c: Candidate, *, min_market_cap=1e10, min_adv=3e7, min_price=5.0,
               min_history_rows=1260, primary_exchanges=("NYSE", "NASDAQ", "NYSEARCA")) -> list[str]:
    fails: list[str] = []
    if c.market_cap_usd < min_market_cap:
        fails.append("HG-01")
    if c.adv20_usd < min_adv:
        fails.append("HG-02")
    if c.price < min_price:
        fails.append("HG-03")
    if c.history_rows < min_history_rows:
        fails.append("HG-04")
    if c.fundamentals is not None:
        fails.extend(hard_gate_checks(c.fundamentals, {}))
    if c.accounting_flags:
        fails.append("HG-10")
    if c.xbrl_completeness < 0.9:
        fails.append("HG-11")
    if c.exchange.upper() not in primary_exchanges:
        fails.append("HG-12")
    return sorted(set(fails))


def score_candidate(c: Candidate) -> dict:
    """Q·V·G 와 정렬 점수. 가치평가 보류 업종은 V 없이 표시한다.

    v1.3.3 04: **감점 전 V(`v_raw`)·원시 안전마진·부호 있는 괴리·EV/EBIT 표본 품질**을 같이 남긴다.
    V 점수식과 감점은 v1.3.2 그대로다(사용자 결정) — 바뀐 것은 "보이는 정도"다.
    """
    sec = classify_sector(c.sector)
    q = quality_score(c.fundamentals) if c.fundamentals else {"q": 0.0, "parts": {}, "metrics": {}}
    v = None
    v_raw = None
    if not sec["valuation_suspended"] and c.margin is not None:
        v_raw = round(v_score(c.margin, 0.0), 2)
        v = round(v_score(c.margin, c.v_penalties), 2)
    g = growth_score(c.peg, c.revenue_cagr) if not sec["valuation_suspended"] else {"g": None}
    rank = None
    if v is not None and g.get("g") is not None:
        rank = round(0.5 * q["q"] + 0.3 * v + 0.2 * g["g"], 3)
    return {
        "ticker": c.ticker,
        "sector": c.sector,
        "q": round(q["q"], 2),
        "v": v,
        "v_raw": v_raw,
        "margin": None if c.margin is None else round(float(c.margin), 6),
        "v_penalties": round(float(c.v_penalties or 0.0), 2),
        "hist_divergence_signed": c.hist_divergence_signed,
        "ev_ebit_percentile": c.ev_ebit_percentile,
        "ev_ebit_current": c.ev_ebit_current,
        "ev_ebit_median_10y": c.ev_ebit_median_10y,
        "ev_ebit_samples": c.ev_ebit_samples,
        "ev_ebit_years": c.ev_ebit_years,
        "ev_ebit_window": c.ev_ebit_window,
        "ev_ebit_reason": c.ev_ebit_reason,
        "growth": c.growth,
        "growth_raw_cagr": c.growth_raw_cagr,
        "growth_clipped_to_zero": c.growth_clipped_to_zero,
        "effective_fcff_years": c.effective_fcff_years,
        "normalization_method": c.normalization_method,
        "dcf_sensitivity": c.dcf_sensitivity,
        "dcf_dropped": c.dcf_dropped,
        "wacc_sensitivity": c.wacc_sensitivity,
        "terminal_g_sensitivity": c.terminal_g_sensitivity,
        "g": g.get("g"),
        "rank_score": rank,
        "parts": q["parts"],
        "metrics": q["metrics"],
        "valuation_suspended": sec["valuation_suspended"],
        "suspend_reason": sec["reason"],
    }


def screen(candidates: list[Candidate], sectors_max: int = 6, watchlist_max: int = 40) -> dict:
    """워치리스트 확정. 반환값에는 탈락 사유와 보류 업종이 모두 남는다(조용한 누락 금지)."""
    scored: list[dict] = []
    excluded: list[dict] = []
    suspended: list[dict] = []

    for c in candidates:
        sec = classify_sector(c.sector)
        if sec["valuation_suspended"]:
            suspended.append({"ticker": c.ticker, "sector": c.sector, "reason": SUSPEND_REASON,
                              "valuation_suspended": True})
            continue
        fails = hard_gates(c)
        if fails:
            excluded.append({
                "ticker": c.ticker,
                "gates": fails,
                "reasons": [GATE_LABELS.get(f, f) for f in fails],
            })
            continue
        s = score_candidate(c)
        if s["rank_score"] is None:
            excluded.append({"ticker": c.ticker, "gates": ["V_UNAVAILABLE"],
                             "reasons": ["안전마진 없음 — V 계산 불가"]})
            continue
        scored.append(s)

    scored.sort(key=lambda x: x["rank_score"], reverse=True)
    picked: list[dict] = []
    per_sector: dict[str, int] = {}
    for s in scored:
        if len(picked) >= watchlist_max:
            break
        sector = s["sector"] or "UNKNOWN"
        if per_sector.get(sector, 0) >= sectors_max:
            continue
        per_sector[sector] = per_sector.get(sector, 0) + 1
        picked.append(s)

    return {
        "watchlist": picked,
        "count": len(picked),
        "excluded": excluded,
        "valuation_suspended": suspended,
        "sector_counts": per_sector,
        "notes": "통과 종목 0개도 정상 결과다(최소 개수 강제 없음).",
    }
