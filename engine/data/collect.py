"""engine.data.collect — 라이브 수집(yfinance·SEC·FRED·Finnhub)과 **부분 실패 처리**.

[고정] 소스 하나가 실패해도 파이프라인은 계속 돈다(기획서 2.7 실패 대응).
- 실패한 소스는 `diagnostics.degraded` 에 **사유와 함께** 남긴다(조용한 0건 금지).
- 가격이 하나도 없으면 신호를 만들지 않는다 — 호출자가 "전일 데이터 유지"로 처리한다.
- 키가 없으면 그 소스는 `missing_key` 로 표시하고 건너뛴다(크래시 금지).
- 뉴스 48시간 규칙(2.7): `published_at ≤ decision_at` 이고 48시간 안의 기사만 판독 대상으로 남긴다.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

from engine.data.macro import DEFAULT_SERIES, as_of_macro, FredClient
from engine.data.news import FinnhubClient
from engine.data.prices import PriceQualityError, build_price_frame
from engine.data.sec_edgar import SecClient, parse_submissions

DEFAULT_UNIVERSE = Path("config/universe.yaml")
NEWS_MAX_AGE_HOURS = 48
MACRO_TO_VALUES = {
    "DFF": "dff", "T10Y2Y": "t10y2y", "BAMLH0A0HYM2": "hy_oas_bp",
    "VIXCLS": "vix", "DTWEXBGS": "dtwexbg", "CPIAUCSL": "cpi",
}


def load_universe(path: str | Path = DEFAULT_UNIVERSE) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _note(diag: dict, source: str, status: str, detail: str | None = None) -> None:
    diag["sources"][source] = {"status": status, "detail": detail}


def collect_prices(tickers: list[str], as_of: date, decision_at: datetime, diag: dict,
                   history_years: int = 3) -> dict:
    """yfinance 일봉 → `PricePanel`(3종 가격). 종목 단위 실패는 건너뛰고 사유를 남긴다."""
    import yfinance as yf

    out: dict = {}
    failed: list[str] = []
    period = f"{history_years}y"
    for i, ticker in enumerate(tickers):
        for attempt in range(2):
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period=period, auto_adjust=False, actions=False)
                splits = t.splits if hasattr(t, "splits") else None
                if hist is None or len(hist) == 0:
                    raise PriceQualityError("빈 이력")
                panel = build_price_frame(ticker, hist, splits)
                out[ticker] = panel.as_of(decision_at)
                break
            except Exception as exc:  # noqa: BLE001 — 소스 실패는 파이프라인 실패가 아니다
                if attempt == 1:
                    failed.append(f"{ticker}:{type(exc).__name__}")
                else:
                    time.sleep(0.6)
        if i and i % 20 == 0:
            time.sleep(1.0)
    _note(diag, "yfinance", "ok" if out else "failed",
          None if not failed else f"실패 {len(failed)}/{len(tickers)}: {', '.join(failed[:6])}")
    return out


def collect_macro(decision_at: datetime, diag: dict) -> tuple[dict, dict]:
    """FRED 11시리즈 → 레짐 입력값 + 약 3개월 전 값. 키가 없으면 `({}, {})`."""
    if not os.environ.get("FRED_API_KEY"):
        _note(diag, "fred", "missing_key", "FRED_API_KEY 없음 — 레짐 미검증(포지션 배수 0.5)")
        return {}, {}
    try:
        client = FredClient()
        frames = client.fetch_many(DEFAULT_SERIES)
    except Exception as exc:  # noqa: BLE001
        _note(diag, "fred", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        return {}, {}
    values: dict = {}
    previous: dict = {}
    try:
        for series, key in MACRO_TO_VALUES.items():
            df = frames.get(series)
            if df is None or df.empty:
                continue
            usable = as_of_macro(df, decision_at).dropna(subset=["value"])
            if usable.empty:
                continue
            last = usable.iloc[-1]
            value = float(last["value"])
            if key == "hy_oas_bp":
                value *= 100.0
            values[key] = value
        cpi = frames.get("CPIAUCSL")
        if cpi is not None and not cpi.empty:
            usable = as_of_macro(cpi, decision_at).dropna(subset=["value"])
            if len(usable) > 12:
                now, year_ago = float(usable.iloc[-1]["value"]), float(usable.iloc[-13]["value"])
                if year_ago > 0:
                    values["cpi_yoy"] = (now / year_ago - 1.0) * 100.0
        # 약 3개월 전 값(추세 계산용)
        for series, key in MACRO_TO_VALUES.items():
            df = frames.get(series)
            if df is None or df.empty:
                continue
            usable = as_of_macro(df, decision_at).dropna(subset=["value"])
            if usable.empty:
                continue
            cutoff = usable.iloc[-1]["date"] - timedelta(days=90)
            older = usable[usable["date"] <= cutoff]
            if older.empty:
                continue
            previous[key] = float(older.iloc[-1]["value"]) * (100.0 if key == "hy_oas_bp" else 1.0)
    except Exception as exc:  # noqa: BLE001
        _note(diag, "fred", "degraded", f"{type(exc).__name__}: {str(exc)[:120]}")
        return {}, {}
    _note(diag, "fred", "ok" if values else "degraded", None if values else "유효 시리즈 없음")
    diag["macro_series"] = sorted(frames)
    return values, previous


def collect_filings(tickers: list[str], decision_at: datetime, diag: dict,
                    max_rows: int = 40) -> dict[str, list[dict]]:
    """SEC submissions → 종목별 공시(접수 시각 포함). User-Agent 가 없으면 건너뛴다."""
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        _note(diag, "sec", "missing_key", "SEC_USER_AGENT 없음 — 공시·8-K 미수집(뉴스 상태 미판독)")
        return {}
    out: dict[str, list[dict]] = {}
    try:
        client = SecClient(user_agent=ua, cache_dir="data/cache/sec_cik")
        table = client.company_tickers()
        cik_by_ticker = dict(zip(table["ticker"], table["cik"]))
    except Exception as exc:  # noqa: BLE001
        _note(diag, "sec", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        return {}
    failed = 0
    for ticker in tickers:
        cik = cik_by_ticker.get(ticker)
        if cik is None:
            failed += 1
            continue
        try:
            rows = parse_submissions(ticker, int(cik), client.submissions(int(cik)), max_rows=max_rows)
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        out[ticker] = [r for r in rows
                       if r.get("available_at") and pd.Timestamp(r["available_at"]) <= decision_at]
    _note(diag, "sec", "ok" if out else "failed", f"CIK 미확인/실패 {failed}건" if failed else None)
    return out


def collect_news(tickers: list[str], decision_at: datetime, diag: dict) -> dict[str, list[dict]]:
    """Finnhub 종목 뉴스 → 48시간 규칙 적용(2.7). 원문 본문은 받지 않는다(메타만)."""
    if not os.environ.get("FINNHUB_API_KEY"):
        _note(diag, "finnhub", "missing_key", "FINNHUB_API_KEY 없음 — 뉴스 미판독")
        return {}
    out: dict[str, list[dict]] = {}
    try:
        client = FinnhubClient()
    except Exception as exc:  # noqa: BLE001
        _note(diag, "finnhub", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        return {}
    start = (decision_at.date() - timedelta(days=5)).isoformat()
    end = decision_at.date().isoformat()
    failed = 0
    for ticker in tickers:
        try:
            rows = client.company_news(ticker, start, end)
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        kept: list[dict] = []
        for row in rows or []:
            published = row.get("published_at")
            if isinstance(published, (int, float)):
                published = datetime.fromtimestamp(float(published), tz=timezone.utc)
            if published is None:
                continue
            published = published if getattr(published, "tzinfo", None) else published.replace(tzinfo=timezone.utc)
            if published > decision_at:
                continue
            if decision_at - published > timedelta(hours=NEWS_MAX_AGE_HOURS):
                continue
            kept.append({
                "headline": row.get("headline"),
                "source": row.get("source"),
                "url": row.get("url"),
                "published_at": published.isoformat().replace("+00:00", "Z"),
                "available_at": published.isoformat().replace("+00:00", "Z"),
            })
        kept.sort(key=lambda r: str(r.get("published_at")), reverse=True)
        out[ticker] = kept[:10]
    _note(diag, "finnhub", "ok" if out else "failed", f"실패 {failed}건" if failed else None)
    return out


def collect(as_of: date, decision_at: datetime, universe_path: str | Path = DEFAULT_UNIVERSE) -> dict:
    """라이브 데이터셋. `source: live`, 실패 내역은 `diagnostics.degraded` 에 남는다."""
    config = load_universe(universe_path)
    rows = list(config.get("tickers") or [])
    tickers = [r["ticker"] for r in rows]
    benchmark = config.get("benchmark") or "SPY"
    diag: dict = {"mode": "live", "sources": {}, "degraded": []}
    panels = collect_prices(tickers + [benchmark], as_of, decision_at, diag)
    macro, macro_prev = collect_macro(decision_at, diag)
    filings = collect_filings(tickers, decision_at, diag)
    news = collect_news(tickers, decision_at, diag)
    for source, info in diag["sources"].items():
        if info["status"] not in ("ok",):
            diag["degraded"].append(f"{source}:{info['status']}")
    universe = []
    for row in rows:
        panel = panels.get(row["ticker"])
        if panel is None or panel.frame.empty:
            continue
        universe.append({
            **row,
            "exchange": "NYSE",
            "history_rows": int(len(panel.frame)),
            "adv20_usd": float((panel.frame["raw_close"] * panel.frame["volume"]).tail(20).mean())
            if "volume" in panel.frame else None,
            "market_cap_usd": None,
        })
    return {
        "as_of": as_of.isoformat(),
        "universe": universe,
        "panels": {k: v for k, v in panels.items()},
        "macro": macro,
        "macro_previous": macro_prev,
        "filings": filings,
        "news": news,
        "fundamentals": {},
        "valuation_inputs": {},
        "source": "live",
        "diagnostics": diag,
        "benchmark": benchmark,
    }
