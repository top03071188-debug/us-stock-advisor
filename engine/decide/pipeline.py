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
from engine.metrics.fundamentals import growth_score, quality_score
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
                   news_state: str, as_of: str) -> list[dict]:
    """공개 허용 근거 — 종류·지표명·시각·링크·인용 해시만(원문 문자열 금지, 2.2.3)."""
    items: list[dict] = []
    if fundamentals:
        items.append({"type": "fundamental", "metric": "quality_composite",
                      "value_kind": "score_0_100", "filed_date": fundamentals.get("filed_date", as_of)})
        items.append({"type": "fundamental", "metric": "fcf_margin_5y",
                      "value_kind": "ratio", "filed_date": fundamentals.get("filed_date", as_of)})
    for f in filings:
        if f.get("form") not in ("10-K", "10-Q"):
            continue
        items.append({"type": "sec_filing", "form": f["form"], "url": f.get("url"),
                      "filed_date": f.get("filed_date"), "available_at": f.get("acceptance_datetime"),
                      "accession": f.get("accession")})
        break
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
        fundamentals = (data.get("fundamentals") or {}).get(ticker)
        q = quality_score(fundamentals)["q"] if fundamentals else 50.0
        iv_median = iv_low = iv_high = None
        mult = row.get("iv_mult")
        if mult is not None and not sec["valuation_suspended"]:
            iv_median = ind["sadj_close"] * float(mult)
            iv_low = iv_median * 0.85
            iv_high = iv_median * 1.15
        margin = None if (iv_median is None or sec["valuation_suspended"]) else margin_of_safety(
            iv_median, ind["sadj_close"])
        v = None if margin is None else round(v_score(margin), 2)
        g = None
        if not sec["valuation_suspended"]:
            # 데이터셋이 G 점수를 직접 주면 그것을 쓴다(픽스처). 라이브는 PEG·성장률로 계산한다.
            g = row.get("g")
            if g is None and (row.get("peg") is not None or row.get("revenue_cagr") is not None):
                g = growth_score(row.get("peg"), row.get("revenue_cagr")).get("g")
        news_state = row.get("news_state") or "not_read"
        filings = (data.get("filings") or {}).get(ticker, [])
        news = (data.get("news") or {}).get(ticker, [])
        evidence = evidence_items(ticker, filings, news, row, news_state, as_of.isoformat())
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
                  sector_weights: dict | None = None, source: str | None = None) -> dict:
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
            "rank_score": None if (view.v is None or view.g is None) else round(
                0.5 * view.q + 0.3 * float(view.v) + 0.2 * float(view.g), 3),
            "valuation_suspended": view.valuation_suspended,
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
        )
        record["trade_grade"] = evaluation.get("grade")
        record["components"] = evaluation.get("components")
        record["valuation"] = {
            "iv_low": _f(view.iv_low), "iv_median": _f(view.iv_median), "iv_high": _f(view.iv_high),
            "price": _f(ind["raw_close"]), "margin": _f(view.margin),
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
        "order_cutoff_at": three_times["order_cutoff_at"],
        "as_of": as_of.isoformat(),
        "params_version": params_version,
        "snapshot_id": snapshot_id,
        "code_commit": code_commit,
        "status": status,
        "mode": mode,
        "source": source or data.get("source", "live"),
        "regime": regime.as_dict(),
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
    """한 문장 이유 — 수치를 그대로 인용한다(창작 금지)."""
    parts: list[str] = []
    if view.margin is not None:
        parts.append(f"IV 대비 {view.margin:.0%} {'할인' if view.margin >= 0 else '프리미엄'}")
    rsi = view.indicators.get("rsi14")
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    if view.g is not None and view.g <= 0:
        parts.append("성장률 0 이하 → 등급 상한 WATCH")
    if not parts:
        parts.append("근거 수치 부족 — 관찰만")
    return " · ".join(parts)
