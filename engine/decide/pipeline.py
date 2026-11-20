"""engine.decide.pipeline — 수집 스냅샷 → 지표 → 1·2층 신호 → 발행 레코드 (기획서 2.6 ⑧·⑪).

이 모듈은 **입력 데이터셋**(네트워크 수집이든 픽스처든)을 받아 `data/quant_signals.json` 의
본문을 만든다. 시각·파일 쓰기·커밋은 `scripts/run_daily.py` 가 한다(순수 함수 유지 → 결정론).

[고정]
- 판단(B2·B3)은 `split_adj_ohlc`, 화면·주문·체결은 `raw_as_traded` + `atr_pct` 비율.
- Veto 로 차단된 종목은 삭제하지 않고 `blocked[]` 에 사유와 함께 남긴다("안 산 이유도 정보").
- 신호 0건은 정상이다 — `signals: []` 로 발행하고 화면이 "오늘 신호 없음"을 쓴다.
- 발행 레코드는 `publish_record` 로만 만든다(체결가 비어 있음, 잠정 분할가).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from engine.decide.macro import classify_regime
from engine.decide.signals import (EntryContext, build_entry_plan, evaluate_entry, evaluate_exit,
                                   expected_mdd, margin_of_safety, target_band)
from engine.decide.veto import VetoInputs, veto_check
from engine.ledger.record import publish_record
from engine.metrics.fundamentals import growth_score, qc_wacc, quality_score, sanitize_fundamentals
from engine.metrics.technical import dma, snapshot_indicators
from engine.metrics.valuation import classify_sector, v_score
from engine.models import PriceKind
from engine.utils.dates import next_trading_day, trading_day_count

DEFAULT_PARAMS = Path("config/params_v001.yaml")
EVIDENCE_REQUIRED = ("fundamental", "sec_filing")


def load_params(path: str | Path = DEFAULT_PARAMS) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _f(value, nd: int = 4):
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return round(out, nd)


def daily_vol(panel, window: int = 60) -> float | None:
    closes = panel.series(PriceKind.split_adj_ohlc).close().dropna()
    if len(closes) < 21:
        return None
    rets = closes.pct_change().dropna().tail(window)
    if len(rets) < 10:
        return None
    return float(rets.std(ddof=1))


def dma_slope(panel, window: int = 200, span: int = 20) -> float | None:
    sadj = panel.series(PriceKind.split_adj_ohlc)
    line = dma(sadj, window).dropna()
    if len(line) < span + 1:
        return None
    return float(line.iloc[-1] / line.iloc[-1 - span] - 1.0)


def dma20_sigma(panel, window: int = 20) -> float | None:
    sadj = panel.series(PriceKind.split_adj_ohlc)
    closes = sadj.close().dropna()
    if len(closes) < window:
        return None
    d20 = dma(sadj, window).dropna()
    if d20.empty:
        return None
    recent = closes.tail(window)
    sigma = float(recent.std(ddof=0))
    if sigma <= 0:
        return None
    return float((closes.iloc[-1] - d20.iloc[-1]) / sigma)


def chart_series(panel, weeks: int = 52) -> dict:
    """공개용 차트 — **주 1회 간격 정규화 지수(기준일=100)** + 50/200일선(같은 기준).

    원시 OHLC·거래량 시계열은 내보내지 않는다(2.2.4 대안 규칙).
    """
    sadj = panel.series(PriceKind.split_adj_ohlc)
    frame = sadj.frame
    if frame.empty:
        return {"labels": [], "index": [], "dma50": [], "dma200": []}
    weekly = frame.resample("W-FRI").last().dropna(subset=["sadj_close"])
    weekly = weekly.tail(weeks)
    if weekly.empty:
        return {"labels": [], "index": [], "dma50": [], "dma200": []}
    base = float(weekly["sadj_close"].iloc[0])
    d50 = dma(sadj, 50).resample("W-FRI").last().reindex(weekly.index).ffill()
    d200 = dma(sadj, 200).resample("W-FRI").last().reindex(weekly.index).ffill()
    return {
        "basis": "split_adj_index_base100",
        "labels": [d.date().isoformat() for d in weekly.index],
        "index": [round(float(v) / base * 100.0, 3) for v in weekly["sadj_close"]],
        "dma50": [None if pd.isna(v) else round(float(v) / base * 100.0, 3) for v in d50],
        "dma200": [None if pd.isna(v) else round(float(v) / base * 100.0, 3) for v in d200],
    }


def evidence_items(ticker: str, filings: list[dict], news: list[dict], fundamentals: dict | None,
                   news_state: str, as_of: str,
                   valuation_detail: dict | None = None) -> list[dict]:
    """공개 허용 근거 — 종류·지표명·시각·링크·인용 해시만(원문 문자열 금지, 2.2.3).

    R04: 재무 근거는 **실제 계산에 쓴 회계연도 10-K** 의 `accn`·`period_end`·`filed`·접수 시각을 단다.
    최근 10-Q(참고 공시)는 `role: "reference"` 로 **별도 항목**이고, 날짜를 모르면 `as_of` 로 대신하지 않는다.
    """
    items: list[dict] = []
    vdetail = valuation_detail or {}
    sources = [s for s in (vdetail.get("fundamental_sources") or []) if s.get("accn")]
    src = sources[0] if sources else {}
    if fundamentals:
        base = {"type": "fundamental", "period_end": src.get("fy"), "form": src.get("form"),
                "accession": src.get("accn"), "filed_date": src.get("filed"),
                "available_at": src.get("available_at")}
        # 모르는 날짜는 `as_of` 로 대신하지 않고 **키를 비운다**(스키마는 string 만 받는다).
        base = {k: v for k, v in base.items() if v is not None}
        items.append({**base, "metric": "quality_composite", "value_kind": "score_0_100"})
        items.append({**base, "metric": "fcf_margin_5y", "value_kind": "ratio"})
    reference = next((f for f in filings if f.get("form") == "10-Q"), None) \
        or next((f for f in filings if f.get("form") == "10-K"), None)
    if reference is not None:
        items.append({"type": "sec_filing", "form": reference["form"], "url": reference.get("url"),
                      "filed_date": reference.get("filed_date"),
                      "available_at": reference.get("acceptance_datetime"),
                      "accession": reference.get("accession"), "role": "reference"})
    if news and news_state != "not_read":
        n = news[0]
        item = {"type": "news", "url": n.get("url"), "headline": n.get("headline"),
                "source": n.get("source"), "published_at": n.get("published_at"),
                "available_at": n.get("published_at")}
        if n.get("quote_sha256"):
            item["quote_sha256"] = n["quote_sha256"]
            item["quote_verified"] = bool(n.get("quote_verified", True))
        items.append(item)
    if len(items) >= 3:
        items.append({"type": "market", "metric": "atr_pct", "value_kind": "ratio"})
    return items


def confidence_grade(evidence: list[dict], news_state: str) -> str:
    """신뢰도 A/B/C (3.10) — `A 데이터·근거 충족` 라벨의 근거."""
    types = {e.get("type") for e in evidence}
    verified_ok = all(e.get("quote_verified") is not False for e in evidence)
    if not verified_ok or news_state == "insufficient_evidence":
        return "C"
    core = all(t in types for t in EVIDENCE_REQUIRED)
    if core and news_state == "read_done" and len(evidence) >= 3:
        return "A"
    if core:
        return "B"
    return "C"


def earnings_days(filings: list[dict], as_of: date) -> int | None:
    """8-K Item 2.02 이력으로 다음 실적까지 남은 거래일을 추정한다(V01 입력)."""
    dates = []
    for f in filings:
        if f.get("form") == "8-K" and "2.02" in (f.get("items") or []):
            try:
                dates.append(date.fromisoformat(str(f["filed_date"])[:10]))
            except (KeyError, ValueError):
                continue
    if not dates:
        return None
    last = max(dates)
    estimate = last + timedelta(days=91)
    while estimate < as_of:
        estimate += timedelta(days=91)
    return trading_day_count(as_of, estimate) if estimate >= as_of else -trading_day_count(estimate, as_of)


def close_lag_business_days(last_bar: date | None, as_of: date) -> int | None:
    if last_bar is None:
        return None
    target = as_of - timedelta(days=1)
    if last_bar >= target:
        return 0
    return trading_day_count(last_bar, target)


@dataclass
class TickerView:
    ticker: str
    sector: str
    indicators: dict
    panel: object
    iv_low: float | None
    iv_median: float | None
    iv_high: float | None
    margin: float | None
    q: float
    v: float | None
    g: float | None
    news_state: str
    evidence: list[dict]
    filings: list[dict]
    news: list[dict]
    valuation_suspended: bool
    suspended_reason: str | None
    structure: dict
    valuation_detail: dict | None = None
    iv_band_basis: str | None = None
    split_factor: float = 1.0
    v_penalties: float = 0.0                   # v1.3.2: DCF 민감도 −4 · 역사 백분위 괴리 −10
    ev_ebit_percentile: float | None = None    # 10년 EV/EBIT 백분위(0~100) → S1 과열


def _num(value) -> float | None:
    """양수 유한값만 숫자로 넘긴다(0·NaN·문자열은 None)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) and out > 0 else None


def build_views(data: dict, as_of: date) -> list[TickerView]:
    """데이터셋 → 종목별 지표·가치·점수(신호 판단의 입력)."""
    views: list[TickerView] = []
    panels = data["panels"]
    for row in data["universe"]:
        ticker = row["ticker"]
        panel = panels.get(ticker)
        if panel is None or panel.frame.empty:
            continue
        ind = snapshot_indicators(panel)
        if not ind:
            continue
        sector = row.get("sector") or ""
        sec = classify_sector(sector)
        fundamentals = sanitize_fundamentals((data.get("fundamentals") or {}).get(ticker))
        q = quality_score(fundamentals)["q"] if fundamentals else 50.0
        vdetail = (data.get("valuation_inputs") or {}).get(ticker)
        if not isinstance(vdetail, dict):
            vdetail = {}   # 픽스처는 ValuationInputs 를 직접 넣는다 — 배관 대상은 dict 뿐이다
        iv_median = iv_low = iv_high = None
        band_basis = None
        if not sec["valuation_suspended"]:
            iv_median = _num(vdetail.get("iv_median")) or _num(row.get("iv_median"))
            if iv_median is None:
                mult = row.get("iv_mult")
                if mult is not None:
                    iv_median = ind["sadj_close"] * float(mult)
            if iv_median is not None:
                # R04: `value_company` 가 계산한 bear/base/bull 밴드를 그대로 쓴다.
                iv_low = _num(vdetail.get("iv_low")) or _num(row.get("iv_low"))
                iv_high = _num(vdetail.get("iv_high")) or _num(row.get("iv_high"))
                band_basis = vdetail.get("iv_band_basis") or row.get("iv_band_basis")
                if iv_low is None or iv_high is None:
                    iv_low = iv_median * 0.85
                    iv_high = iv_median * 1.15
                    band_basis = "fallback_15pct"
        margin = None if (iv_median is None or sec["valuation_suspended"]) else margin_of_safety(
            iv_median, ind["sadj_close"])
        # v1.3.2: 감점은 1층(워치리스트)과 **같은 값**을 쓴다 — 두 층의 V 가 갈라지면 순위와 등급이 모순된다.
        v_penalties = float(vdetail.get("v_penalties") or 0.0)
        ev_ebit_percentile = (float(vdetail["ev_ebit_percentile"])
                              if vdetail.get("ev_ebit_percentile") is not None else None)
        v = None if margin is None else round(v_score(margin, v_penalties), 2)
        g = None
        if not sec["valuation_suspended"]:
            # 데이터셋이 G 점수를 직접 주면 그것을 쓴다(픽스처). 라이브는 PEG·성장률로 계산한다.
            g = row.get("g")
            if g is None and (row.get("peg") is not None or row.get("revenue_cagr") is not None):
                g = growth_score(row.get("peg"), row.get("revenue_cagr")).get("g")
        news_state = row.get("news_state") or "not_read"
        filings = (data.get("filings") or {}).get(ticker, [])
        news = (data.get("news") or {}).get(ticker, [])
        evidence = evidence_items(ticker, filings, news, row, news_state, as_of.isoformat(), vdetail)
        split_factor = 1.0
        if len(panel.frame.index) and "split_factor" in panel.frame.columns:
            value = panel.frame["split_factor"].iloc[-1]
            if np.isfinite(value):
                split_factor = float(value)
        structure = {
            "daily_vol_60d": daily_vol(panel),
            "dma200_slope": dma_slope(panel),
            "dma20_sigma": dma20_sigma(panel),
            "last_bar": panel.frame.index[-1].date() if len(panel.frame.index) else None,
            "chart": chart_series(panel),
        }
        views.append(TickerView(
            ticker=ticker, sector=sector, indicators=ind, panel=panel,
            iv_low=iv_low, iv_median=iv_median, iv_high=iv_high, margin=margin,
            q=round(float(q), 2), v=v, g=None if g is None else round(float(g), 2),
            news_state=news_state, evidence=evidence, filings=filings, news=news,
            valuation_suspended=bool(sec["valuation_suspended"]),
            suspended_reason=sec["reason"], structure=structure,
            valuation_detail=vdetail, iv_band_basis=band_basis, split_factor=split_factor,
            v_penalties=v_penalties, ev_ebit_percentile=ev_ebit_percentile,
        ))
    return views


def _last_return(panel) -> float | None:
    closes = panel.series(PriceKind.split_adj_ohlc).close().dropna()
    if len(closes) < 2:
        return None
    return float(closes.iloc[-1] / closes.iloc[-2] - 1.0)


def build_payload(data: dict, *, as_of: date, mode: str, params: dict, params_version: str,
                  code_commit: str, snapshot_id: str, status: str,
                  three_times: dict, held: list[dict] | None = None,
                  sector_weights: dict | None = None, source: str | None = None,
                  inputs_hash: str | None = None) -> dict:
    """수집 스냅샷 → `quant_signals` 본문(레저 요약은 호출자가 붙인다)."""
    views = build_views(data, as_of)
    regime = classify_regime(data.get("macro"), data.get("macro_previous"))
    held = held or []
    held_tickers = {h["ticker"] for h in held}
    sector_weights = dict(sector_weights or {})

    signals: list[dict] = []
    blocked: list[dict] = []
    watchlist: list[dict] = []
    diagnostics: list[dict] = []
    charts: dict[str, dict] = {}
    contexts: dict[str, EntryContext] = {}

    for view in views:
        ind = view.indicators
        last_bar = view.structure.get("last_bar")
        lag = close_lag_business_days(last_bar, as_of)
        earnings = row_days = next((r.get("days_to_earnings") for r in data["universe"]
                                    if r["ticker"] == view.ticker), None)
        if row_days is None:
            earnings = earnings_days(view.filings, as_of)
        veto_in = VetoInputs(
            days_to_earnings=earnings,
            close_lag_business_days=lag,
            filing_items=[i for f in view.filings if f.get("form") == "8-K" for i in (f.get("items") or [])],
            filing_items_verified=[],
            adv20_usd=next((r.get("adv20_usd") for r in data["universe"] if r["ticker"] == view.ticker), None),
            vix=(data.get("macro") or {}).get("vix"),
            hy_oas_bp=(data.get("macro") or {}).get("hy_oas_bp"),
            projected_sector_weight=sector_weights.get(view.sector, 0.0) + 0.05,
            projected_position_weight=0.05,
            one_day_return=_last_return(view.panel),
            news_injection=False,
            evidence=view.evidence,
        )
        veto = veto_check(veto_in)
        ctx = EntryContext(
            ticker=view.ticker,
            raw_close=ind["raw_close"],
            sadj_close=ind["sadj_close"],
            split_factor=view.split_factor,
            sector=view.sector,
            dma50=ind.get("dma50"),
            dma200=ind.get("dma200"),
            dma200_slope=view.structure.get("dma200_slope"),
            rsi14=ind.get("rsi14"),
            pct_b=ind.get("pct_b"),
            dma20_sigma=view.structure.get("dma20_sigma"),
            atr_pct=ind.get("atr_pct"),
            iv_median=view.iv_median,
            iv_high=view.iv_high,
            ev_ebit_percentile=view.ev_ebit_percentile,
            q=view.q,
            v=view.v,
            g=view.g,
            structural_risk=10.0,
            in_watchlist=view.ticker not in held_tickers,
            held=view.ticker in held_tickers,
            valuation_suspended=view.valuation_suspended,
            news_state=view.news_state,
            evidence=view.evidence,
            vetoes=veto.as_list(),
        )
        contexts[view.ticker] = ctx
        evaluation = evaluate_entry(ctx, regime=regime, params=params)
        watchlist.append({
            "ticker": view.ticker, "sector": view.sector, "q": view.q, "v": view.v, "g": view.g,
            "v_raw": None if view.margin is None else round(v_score(view.margin, 0.0), 2),
            "margin": _f(view.margin),
            "v_penalties": round(float((view.valuation_detail or {}).get("v_penalties") or 0.0), 2),
            "ev_ebit_percentile": (view.valuation_detail or {}).get("ev_ebit_percentile"),
            # v1.3.3 06: 표본 품질(개수·연수·실제 범위·사유)을 워치리스트 행에도 같이 낸다.
            "ev_ebit_samples": (view.valuation_detail or {}).get("ev_ebit_samples"),
            "ev_ebit_years": (view.valuation_detail or {}).get("ev_ebit_years"),
            "ev_ebit_window": (view.valuation_detail or {}).get("ev_ebit_window"),
            "ev_ebit_reason": (view.valuation_detail or {}).get("ev_ebit_reason"),
            "hist_divergence_signed": _f((view.valuation_detail or {}).get("hist_divergence_signed")),
            "growth_clipped_to_zero": (view.valuation_detail or {}).get("growth_clipped_to_zero"),
            "wacc_sensitivity": _f((view.valuation_detail or {}).get("wacc_sensitivity")),
            "rank_score": None if (view.v is None or view.g is None) else round(
                0.5 * view.q + 0.3 * float(view.v) + 0.2 * float(view.g), 3),
            "valuation_suspended": view.valuation_suspended,
            # R03: Q 점수 경로의 WACC 와 가치평가 경로의 WACC 는 다른 값이다 — 이름을 나눠 내보낸다.
            "wacc_qc": _f(qc_wacc(data["fundamentals"][view.ticker]))
            if (data.get("fundamentals") or {}).get(view.ticker) else None,
            "wacc_valuation": _f((view.valuation_detail or {}).get("wacc")),
            "iv_band_basis": view.iv_band_basis,
        })
        charts[view.ticker] = view.structure.get("chart") or {}
        diag = {
            "ticker": view.ticker,
            "grade": evaluation.get("grade"),
            "entry_score": evaluation.get("entry_score"),
            "failed_gates": evaluation.get("failed_gates"),
            "veto": [v["code"] for v in veto.vetoes],
            "close_lag": lag,
            "days_to_earnings": earnings,
            "news_state": view.news_state,
        }
        diagnostics.append(diag)

        if veto.blocked:
            blocked.append({
                "ticker": view.ticker,
                "reason": ",".join(veto.blocking),
                "codes": veto.blocking,
                "message_ko": " · ".join(v["message_ko"] for v in veto.vetoes),
                "grade": evaluation.get("grade"),
            })
            continue
        if evaluation.get("signal") == "SUSPENDED":
            blocked.append({"ticker": view.ticker, "reason": "SECTOR_FORMULA_PENDING",
                            "codes": ["SUSPENDED"],
                            "message_ko": "가치평가 보류 업종 — 신호를 발행하지 않는다(3.2.1)."})
            continue
        if evaluation.get("grade") in ("NONE", None):
            continue

        # 신뢰도 등급 + 분할 계획
        conf = confidence_grade(view.evidence, view.news_state)
        buy = evaluation.get("signal") == "BUY"
        plan = None
        if buy:
            plan = build_entry_plan(ctx, evaluation, regime=regime, params=params,
                                    total_asset=1.0, allow_watch=True)
        plan_weight = 0.0
        stop_distance = 0.0
        sizing_note = None
        if plan and plan.get("placeable"):
            plan_weight = float(plan["plan_weight"])
            stop_distance = float(plan["stop_distance"])
        else:
            sizing_note = "주문 범위 밖 — 관찰만(3.4.4)"
        band = target_band(iv_median=view.iv_median or ind["raw_close"] * 1.2, price=ind["raw_close"])
        # 채점의 기준 봉을 **발행 시점에 함께 고정**한다 — 나중에 알게 되는 `as_of` 당일 종가를
        # 기준으로 삼으면 밴드(`base_price`)와 수익률(`r_base`)이 다른 봉을 가리킨다(감사 09-d).
        band["return_base"] = "base_price"
        band["base_bar_date"] = last_bar.isoformat() if hasattr(last_bar, "isoformat") else last_bar
        record = publish_record(
            signal_id=f"{as_of.isoformat()}-{view.ticker}-{('BUY' if buy else 'WATCH')}-001",
            ticker=view.ticker,
            type="BUY" if buy else "WATCH",
            grade=conf,
            entry_score=float(evaluation.get("entry_score") or 0.0),
            as_of=as_of.isoformat(),
            data_cutoff_at=three_times["data_cutoff_at"],
            signal_published_at=three_times["signal_published_at"],
            order_cutoff_at=three_times["order_cutoff_at"],
            target_session=three_times.get("target_session"),
            generated_price=ind["raw_close"],
            target_band=band,
            expected_mdd=expected_mdd(view.structure.get("daily_vol_60d")),
            thesis_ko=_thesis(view, evaluation),
            evidence=view.evidence,
            investor_scores=evaluation.get("investor_scores") or {},
            conflict=bool(evaluation.get("conflict")),
            plan_weight=plan_weight,
            stop_distance=stop_distance,
            veto=[],
            params_version=params_version,
            snapshot_id=snapshot_id,
            code_commit=code_commit,
            invalidation={"price": round(float(ind["dma200"] or ind["sadj_close"]) * 0.97, 2),
                          "reason": "200일선 −3% 이탈 + ROIC<WACC"} if ind.get("dma200") else None,
            quant_intent="pending_buy" if (buy and plan and plan.get("placeable")) else "no_action",
            inputs_hash=inputs_hash,
        )
        record["trade_grade"] = evaluation.get("grade")
        record["components"] = evaluation.get("components")
        detail = view.valuation_detail or {}
        dcf_detail = detail.get("iv_dcf") or {}
        record["valuation"] = {
            "iv_low": _f(view.iv_low), "iv_median": _f(view.iv_median), "iv_high": _f(view.iv_high),
            "price": _f(ind["raw_close"]), "margin": _f(view.margin),
            # R04: 방법별 IV·시나리오 밴드·중앙값 선정 근거를 레코드까지 그대로 넘긴다.
            "iv_band_basis": view.iv_band_basis,
            "median_method": detail.get("median_method"),
            "methods": {k: _f(v) for k, v in (detail.get("methods") or {}).items()},
            "iv_dcf_bear": _f(dcf_detail.get("bear")), "iv_dcf_bull": _f(dcf_detail.get("bull")),
            "fcff_normalized": _f(detail.get("fcff_normalized")),
            "growth": _f(detail.get("growth")), "growth_basis": detail.get("growth_basis"),
            "net_debt": _f(detail.get("net_debt")),
            "wacc_valuation": _f(detail.get("wacc")),
            "v_confidence": detail.get("v_confidence"),
            "v_confidence_reason": detail.get("v_confidence_reason"),
            "v_penalties": _f(detail.get("v_penalties")),
            "v_penalty_dcf": _f(detail.get("v_penalty_dcf")),
            "v_penalty_hist": _f(detail.get("v_penalty_hist")),
            "dcf_sensitivity": _f(detail.get("dcf_sensitivity")),
            "ev_ebit_percentile": _f(detail.get("ev_ebit_percentile")),
            "ev_ebit_median_10y": _f(detail.get("ev_ebit_median_10y")),
            "iv_hist_ev_ebit": _f(detail.get("iv_hist_ev_ebit")),
            "hist_divergence": _f(detail.get("hist_divergence")),
            "dcf_dropped": detail.get("dcf_dropped"),
            # ── v1.3.3 (트랙 B 04·05·06·08) 배관 ──────────────────────────────────────────
            # 화면·JSON 이 "왜 이 V 인가"를 보여 줄 수 있게 감점 전 V·원시 값·표본 품질을 레코드까지 넘긴다.
            "v_raw": None if view.margin is None else round(v_score(view.margin, 0.0), 2),
            "growth_raw_cagr": _f(detail.get("growth_raw_cagr")),
            "growth_clipped_to_zero": detail.get("growth_clipped_to_zero"),
            "effective_fcff_years": detail.get("effective_fcff_years"),
            "normalization_method": detail.get("normalization_method"),
            "wacc_sensitivity": _f(detail.get("wacc_sensitivity")),
            "terminal_g_sensitivity": _f(detail.get("terminal_g_sensitivity")),
            "rate_sensitivity": _f(detail.get("rate_sensitivity")),
            "hist_divergence_signed": _f(detail.get("hist_divergence_signed")),
            "ev_ebit_current": _f(detail.get("ev_ebit_current")),
            "ev_ebit_samples": detail.get("ev_ebit_samples"),
            "ev_ebit_years": detail.get("ev_ebit_years"),
            "ev_ebit_window": detail.get("ev_ebit_window"),
            "ev_ebit_reason": detail.get("ev_ebit_reason"),
            "minority_interest": _f(detail.get("minority_interest")),
            "debt_reporting": detail.get("debt_reporting"),
        }
        record["news_state"] = view.news_state
        record["news"] = [{"headline": n.get("headline"), "source": n.get("source"),
                           "published_at": n.get("published_at"), "url": n.get("url"),
                           "impact": n.get("impact"), "durability": n.get("durability"),
                           "quote_verified": n.get("quote_verified")} for n in view.news[:5]]
        record["filings"] = [{k: f.get(k) for k in ("form", "filed_date", "url", "accession", "items")}
                             for f in view.filings[:5]]
        record["risk"] = {"veto": [], "note_ko": None}
        record["indicators"] = {k: _f(ind.get(k)) for k in ("rsi14", "pct_b", "dma50", "dma200", "atr_pct")}
        record["gate_summary"] = {"failed": evaluation.get("failed_gates") or [],
                                  "reasons": evaluation.get("gate_reasons") or {}}
        record["sizing_note_ko"] = None if (plan and plan.get("placeable")) else sizing_note
        record["decision"] = "BUY" if buy else "WATCH"
        record["sector"] = view.sector
        record["entry_band"] = (plan or {}).get("entry_band")
        signals.append(record)

    position_checks = []
    if held:
        for h in held:
            ctx = contexts.get(h["ticker"])
            if ctx is None:
                position_checks.append({"ticker": h["ticker"], "verdict": "NO_DATA"})
                continue
            position_checks.append(evaluate_exit(ctx, h, regime=regime, params=params))

    return {
        "schema_version": 1,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "data_cutoff_at": three_times["data_cutoff_at"],
        "signal_published_at": three_times["signal_published_at"],
        "order_cutoff_at": three_times["order_cutoff_at"],        "as_of": as_of.isoformat(),
        "params_version": params_version,
        "snapshot_id": snapshot_id,
        "code_commit": code_commit,
        "status": status,
        "mode": mode,
        "source": source or data.get("source", "live"),
        "regime": regime.as_dict(),
        "ingest": data.get("macro_ingest"),
        "signals": signals,
        "blocked": blocked,
        "watchlist": watchlist,
        "watchlist_count": len(watchlist),
        "position_checks": position_checks,
        "diagnostics": diagnostics,
        "charts": charts,
        "next_trading_day": next_trading_day(as_of).isoformat(),
    }


def _thesis(view: TickerView, evaluation: dict) -> str:
    """한 문장 이유 — 수치를 그대로 인용한다(창작 금지).

    부호 규칙(R04): `margin = (IV − 가격)/IV` 이다. 양수면 가격이 IV 보다 **낮다(할인)**,
    음수면 **프리미엄**이고 그 크기는 절대값으로 쓴다(과거 `-233% 프리미엄` 오확).
    """
    parts: list[str] = []
    if view.margin is not None:
        if view.margin >= 0:
            parts.append(f"IV 대비 {view.margin:.0%} 할인")
        else:
            parts.append(f"IV 대비 {-view.margin:.0%} 프리미엄(안전마진 {view.margin:.0%})")
    rsi = view.indicators.get("rsi14")
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    if view.g is not None and view.g <= 0:
        parts.append("성장률 0 이하 → 등급 상한 WATCH")
    if not parts:
        parts.append("근거 수치 부족 — 관찰만")
    return " · ".join(parts)
