"""engine.data.collect — 라이브 수집(yfinance·SEC·FRED·Finnhub)과 **부분 실패 처리**.

[고정] 소스 하나가 실패해도 파이프라인은 계속 돈다(기획서 2.7 실패 대응).
- 실패한 소스는 `diagnostics.degraded` 에 **사유와 함께** 남긴다(조용한 0건 금지).
- 가격이 하나도 없으면 신호를 만들지 않는다 — 호출자가 "전일 데이터 유지"로 처리한다.
- 키가 없으면 그 소스는 `missing_key` 로 표시하고 건너뛴다(크래시 금지).
- 뉴스 48시간 규칙(2.7): `published_at ≤ decision_at` 이고 48시간 안의 기사만 판독 대상으로 남긴다.
- 재무(SEC XBRL)는 `filed ≤ decision_at.date()` 연차 공시만 써서 look-ahead 를 막는다(4.5).
- 가격 이력 기본 **10년** — HG-04(5년/1,260행) 게이트를 실데이터로 채우면서, 10년 EV/EBIT 백분위
  (v1.3.2 §3.2.3)를 계산할 수 있는 창을 확보한다.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from engine.data.macro import DEFAULT_SERIES, FredClient, as_of_macro
from engine.data.news import FinnhubClient
from engine.data.prices import PriceQualityError, build_price_frame
from engine.data.sec_edgar import SecClient, acceptance_index, parse_submissions
from engine.data.xbrl_fundamentals import (
    beta_from_prices,
    build_fundamentals,
    ev_ebit_history,
    peg_basis,
    peg_growth,
    peg_ratio,
    revenue_cagr,
    split_events_from_panel,
    valuation_multiplier,
)
from engine.metrics.fundamentals import AnnualFundamentals, qc_wacc

DEFAULT_UNIVERSE = Path("config/universe.yaml")
NEWS_MAX_AGE_HOURS = 48
MACRO_TO_VALUES = {
    "DFF": "dff", "T10Y2Y": "t10y2y", "BAMLH0A0HYM2": "hy_oas_bp",
    "VIXCLS": "vix", "DTWEXBGS": "dtwexbg", "CPIAUCSL": "cpi",
    "DGS10": "dgs10",
}

#: yfinance 거래소 코드 → HG-12 가 인정하는 거래소 이름
EXCHANGE_CODES = {
    "NYQ": "NYSE", "NYS": "NYSE",
    "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ",
    "PCX": "NYSEARCA", "ASE": "NYSEARCA", "ARC": "NYSEARCA",
    "BTS": "BATS", "PNK": "OTC", "OQB": "OTC", "OQX": "OTC",
}


def load_universe(path: str | Path = DEFAULT_UNIVERSE) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _note(diag: dict, source: str, status: str, detail: str | None = None) -> None:
    diag["sources"][source] = {"status": status, "detail": detail}


def collect_prices(tickers: list[str], as_of: date, decision_at: datetime, diag: dict,
                   history_years: int = 10) -> dict:
    """yfinance 일봉 → `PricePanel`(3종 가격). 종목 단위 실패는 건너뛰고 사유를 남긴다.

    `history_years=10`(≈2,500 거래일) — 5년 이력 게이트(HG-04, `min_history_rows=1260`)를
    실데이터로 충족하면서, 10년 EV/EBIT 백분위·중앙값(v1.3.2)을 계산할 창을 만든다.
    3년 미만 상장 종목은 있는 행만큼만 받아 HG-04 로 탈락하고, 6~9년차 종목은 그 이력으로 계산한다.
    """
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
                # Yahoo 가 마지막 봉을 빈 값(NaN)으로 주는 경우가 있다 — 빈 봉은 이력으로 세지 않는다.
                frame = panel.frame.dropna(subset=["raw_close", "sadj_close"], how="all")
                panel = type(panel)(ticker, frame, dict(panel.issues))
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


def collect_macro(decision_at: datetime, diag: dict, mode: str = "live",
                  client: FredClient | None = None, now: datetime | None = None
                  ) -> tuple[dict, dict]:
    """FRED 11시리즈 → 레짐 입력값 + 약 3개월 전 값. 키가 없으면 `({}, {})`.

    경로를 둘로 나눈다(R07, §2.4.3).
    - `mode="live"`: 빈티지 파라미터 없이 받은 스냅샷의 모든 행 `available_at = 최초 수집 시각`.
      실제로 마감 뒤에 받은 값을 "그때 알 수 있었다"고 우기지 않고, `ingest`(lag_seconds·after_cutoff)로
      드러낸다. 시점 필터는 수집 시각을 기준으로 건다.
    - `mode="replay"`: `realtime_start = realtime_end = V`(결정일 전날) 점조회만 쓴다. 스냅샷 응답이
      오면 그 시리즈를 **버리고** `inputs_missing` 에 남긴다 — 관측일로 역산해 앞당기지 않는다.
    """
    ingest_at = now or datetime.now(timezone.utc)
    if not os.environ.get("FRED_API_KEY") and client is None:
        _note(diag, "fred", "missing_key", "FRED_API_KEY 없음 — 레짐 미검증(포지션 배수 0.5)")
        return {}, {}
    replay = str(mode) == "replay"
    vintage = None
    if replay:
        decision_et = pd.Timestamp(decision_at)
        decision_et = decision_et.tz_localize("UTC") if decision_et.tzinfo is None else decision_et
        vintage = (decision_et.tz_convert("America/New_York").date() - timedelta(days=1)).isoformat()
    try:
        client = client or FredClient()
        if replay:
            frames = client.fetch_many(DEFAULT_SERIES, realtime_start=vintage, realtime_end=vintage,
                                       mode="replay")
        else:
            frames = client.fetch_many(DEFAULT_SERIES, mode="live", first_ingested_at=ingest_at)
    except Exception as exc:  # noqa: BLE001
        _note(diag, "fred", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        return {}, {}
    # 라이브는 수집 시각, 재생은 결정 시각을 시점 필터로 쓴다(라이브 값은 마감 뒤에 도착했다).
    ref = ingest_at if not replay else decision_at
    values: dict = {}
    previous: dict = {}
    excluded: list[str] = []
    try:
        for series, key in MACRO_TO_VALUES.items():
            df = frames.get(series)
            if df is None or df.empty:
                excluded.append(series)
                continue
            usable = as_of_macro(df, ref).dropna(subset=["value"])
            if usable.empty:
                excluded.append(series)
                continue
            last = usable.iloc[-1]
            value = float(last["value"])
            if key == "hy_oas_bp":
                value *= 100.0
            values[key] = value
        cpi = frames.get("CPIAUCSL")
        if cpi is not None and not cpi.empty:
            usable = as_of_macro(cpi, ref).dropna(subset=["value"])
            if len(usable) > 12:
                now_val, year_ago = float(usable.iloc[-1]["value"]), float(usable.iloc[-13]["value"])
                if year_ago > 0:
                    values["cpi_yoy"] = (now_val / year_ago - 1.0) * 100.0
                # 3개월 전 CPI YoY — 실질금리 추세(레짐 신호)를 계산하려면 두 시점이 모두 필요하다.
                cutoff = usable.iloc[-1]["date"] - timedelta(days=90)
                older = usable[usable["date"] <= cutoff]
                if len(older) > 12:
                    prev_now, prev_year = float(older.iloc[-1]["value"]), float(older.iloc[-13]["value"])
                    if prev_year > 0:
                        previous["cpi_yoy"] = (prev_now / prev_year - 1.0) * 100.0
        # 약 3개월 전 값(추세 계산용) — 빈티지 필터가 끝난 `usable` 안에서만 계산한다(같은 시점 프레임).
        for series, key in MACRO_TO_VALUES.items():
            df = frames.get(series)
            if df is None or df.empty:
                continue
            usable = as_of_macro(df, ref).dropna(subset=["value"])
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
    diag["macro_missing_series"] = sorted(excluded)
    if not replay:
        lag = (ingest_at - decision_at).total_seconds()
        diag["macro_ingest"] = {
            "data_cutoff_at": decision_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "inputs_ingested_at": ingest_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "lag_seconds": round(lag, 1),
            "after_cutoff": bool(lag > 0),
            "available_at_source": "first_ingested_at",
        }
    else:
        diag["macro_ingest"] = {
            "data_cutoff_at": decision_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "inputs_ingested_at": None, "lag_seconds": None, "after_cutoff": False,
            "available_at_source": "vintage_point_query", "vintage": vintage,
        }
    return values, previous


def collect_filings(tickers: list[str], decision_at: datetime, diag: dict,
                    max_rows: int = 40, client: SecClient | None = None,
                    cik_by_ticker: dict[str, int] | None = None,
                    acceptance_out: dict[str, dict[str, datetime]] | None = None
                    ) -> dict[str, list[dict]]:
    """SEC submissions → 종목별 공시(접수 시각 포함). User-Agent 가 없으면 건너뛴다.

    `acceptance_out` 을 주면 종목별 `{accn: acceptanceDateTime}` 인덱스를 함께 채운다 — XBRL 재무의
    시점 필터(R06)가 **추가 요청 없이** 같은 submissions 로 돌 수 있게 하기 위해서다.
    """
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        _note(diag, "sec", "missing_key", "SEC_USER_AGENT 없음 — 공시·8-K 미수집(뉴스 상태 미판독)")
        return {}
    out: dict[str, list[dict]] = {}
    try:
        client = client or SecClient(user_agent=ua, cache_dir="data/cache/sec_facts")
        if cik_by_ticker is None:
            table = client.company_tickers()
            cik_by_ticker = {str(t): int(c) for t, c in zip(table["ticker"], table["cik"], strict=False)}
    except Exception as exc:  # noqa: BLE001
        _note(diag, "sec", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        return {}
    failed = 0
    for ticker in tickers:
        cik = (cik_by_ticker or {}).get(ticker)
        if cik is None:
            failed += 1
            continue
        try:
            submissions = client.submissions(int(cik))
            rows = parse_submissions(ticker, int(cik), submissions, max_rows=max_rows)
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        out[ticker] = [r for r in rows
                       if r.get("available_at") and pd.Timestamp(r["available_at"]) <= decision_at]
        if acceptance_out is not None:
            acceptance_out[ticker] = acceptance_index(submissions)
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


def collect_meta(tickers: list[str], diag: dict, pause: float = 0.25) -> dict[str, dict]:
    """yfinance 메타 → `market_cap_usd`, `exchange`(거래소 코드 매핑).

    - 종목 하나가 실패해도 예외를 전파하지 않는다 — 결측은 그대로 두고 `detail` 에 센다.
    - 40여 종목을 연속 호출하므로 호출 사이에 쉬어 rate-limit(429)을 피한다.
    """
    import yfinance as yf

    out: dict[str, dict] = {}
    failed: list[str] = []
    for i, ticker in enumerate(tickers):
        info: dict = {"market_cap_usd": None, "exchange": None, "shares_outstanding": None}
        for attempt in range(2):
            try:
                fast = yf.Ticker(ticker).fast_info
                cap = fast.get("marketCap") if hasattr(fast, "get") else getattr(fast, "market_cap", None)
                exch = fast.get("exchange") if hasattr(fast, "get") else getattr(fast, "exchange", None)
                shares = fast.get("shares") if hasattr(fast, "get") else getattr(fast, "shares", None)
                info = {
                    "market_cap_usd": float(cap) if cap else None,
                    "exchange": EXCHANGE_CODES.get(str(exch or "").upper(), None),
                    "shares_outstanding": float(shares) if shares else None,
                }
                if info["market_cap_usd"] or info["exchange"]:
                    break
            except Exception:  # noqa: BLE001 — 메타 실패는 파이프라인 실패가 아니다
                if attempt == 1:
                    failed.append(ticker)
                else:
                    time.sleep(0.5)
        out[ticker] = info
        if pause and i and i % 10 == 0:
            time.sleep(pause * 4)
    missing_cap = [t for t, v in out.items() if not v.get("market_cap_usd")]
    _note(diag, "yfinance_meta", "ok" if len(missing_cap) < len(tickers) else "failed",
          f"시총 결측 {len(missing_cap)}/{len(tickers)}" + (f": {', '.join(missing_cap[:6])}" if missing_cap else ""))
    diag["meta_missing"] = missing_cap
    return out


def collect_fundamentals(tickers: list[str], decision_at: datetime, diag: dict,
                         panels: dict, benchmark_panel, risk_free: float | None,
                         sectors: dict[str, str], client: SecClient | None = None,
                         cik_by_ticker: dict[str, int] | None = None,
                         acceptance: dict[str, dict[str, datetime]] | None = None
                         ) -> tuple[dict, dict]:
    """SEC XBRL 연차 재무(10년) + 밸류에이션 + 10년 EV/EBIT 역사.

    Returns: (`fundamentals` = ticker→AnnualFundamentals, `valuation_inputs` = ticker→{iv_mult, iv_low, iv_high,
    iv_dcf, iv_epv, iv_graham, methods, median_method, wacc, net_debt, shares_used, fundamental_sources, ...})
    종목 단위 실패는 예외를 전파하지 않고 `detail` 에 남긴다.
    R04: `value_company` 가 낸 방법별 IV·시나리오 밴드·중앙값 선정 근거를 **버리지 않고** 전부 넘긴다.
    """
    fundamentals: dict[str, AnnualFundamentals] = {}
    valuation: dict[str, dict] = {}
    if client is None and not os.environ.get("SEC_USER_AGENT"):
        _note(diag, "sec_xbrl", "missing_key", "SEC_USER_AGENT 없음 — 재무 미수집(HG-05~09·11 판정 불가)")
        return fundamentals, valuation
    try:
        client = client or SecClient(cache_dir="data/cache/sec_facts")
        if cik_by_ticker is None:
            table = client.company_tickers()
            cik_by_ticker = {str(t): int(c) for t, c in zip(table["ticker"], table["cik"], strict=False)}
    except Exception as exc:  # noqa: BLE001
        _note(diag, "sec_xbrl", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        return fundamentals, valuation

    rf = float(risk_free) if risk_free is not None else 0.042
    if rf > 1.0:  # 방어: 퍼센트(5.07)로 들어오면 소수로 바꿔준다
        rf = rf / 100.0
    failed: list[str] = []
    detail: dict[str, dict] = {}
    bench = benchmark_panel
    for ticker in tickers:
        cik = cik_by_ticker.get(ticker)
        if cik is None:
            failed.append(f"{ticker}:no_cik")
            continue
        panel = panels.get(ticker)
        splits = split_events_from_panel(panel) if panel is not None else None
        try:
            payload = client.company_facts(int(cik))
            f, fdiag = build_fundamentals(payload, decision_at,
                                          acceptance=(acceptance or {}).get(ticker), splits=splits)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{ticker}:{type(exc).__name__}")
            continue
        if f is None:
            failed.append(f"{ticker}:no_annual")
            detail[ticker] = {"completeness": 0.0, "reason": "no_annual_facts"}
            continue
        fundamentals[ticker] = f
        price = None
        if panel is not None and not panel.frame.empty:
            sadj = panel.frame["sadj_close"].dropna()
            price = float(sadj.iloc[-1]) if len(sadj) else None
        beta = None
        if panel is not None and bench is not None and not panel.frame.empty and not bench.frame.empty:
            beta = beta_from_prices(panel.frame["sadj_close"], bench.frame["sadj_close"])
        growth, growth_basis = peg_growth(f)
        # v1.3.2: 10년 역사 EV/EBIT — 과거 스냅샷마다 `available_at` 이전 재무만 쓰는 계산(룩어헤드 차단).
        hist = ev_ebit_history(payload, None if panel is None else panel.frame, decision_at,
                               acceptance=(acceptance or {}).get(ticker), splits=splits)
        mult, mdetail = valuation_multiplier(f, price, sectors.get(ticker), beta, rf, hist=hist)
        valuation[ticker] = {
            "iv_mult": mult,
            "peg": peg_ratio(f, price, growth) if price else None,
            "peg_basis": peg_basis(f),
            "peg_growth": growth,
            "peg_growth_basis": growth_basis,
            "revenue_cagr": revenue_cagr(f),
            "eps_cagr": f.eps_cagr,
            "xbrl_completeness": fdiag.get("completeness", 0.0),
            "beta": beta,
            "fiscal_years": fdiag.get("fiscal_years"),
            "fundamental_sources": fdiag.get("fundamental_sources"),
            "shares_used": fdiag.get("shares_used"),
            "xbrl_acceptance_missing": fdiag.get("xbrl_acceptance_missing"),
            "wacc_qc": qc_wacc(f),
            **mdetail,
        }
        valuation[ticker].setdefault("reason", None)
        detail[ticker] = fdiag

    ok_ratio = len(fundamentals) / len(tickers) if tickers else 0.0
    status = "ok" if ok_ratio >= 0.8 else ("partial" if ok_ratio > 0 else "failed")
    _note(diag, "sec_xbrl", status,
          f"연차 재무 {len(fundamentals)}/{len(tickers)}" + (f", 실패: {', '.join(failed[:6])}" if failed else ""))
    diag["xbrl_detail"] = {t: {"completeness": v.get("completeness"),
                               "fiscal_years": v.get("fiscal_years"),
                               "reason": v.get("reason"),
                               "shares_used": v.get("shares_used"),
                               "xbrl_acceptance_missing": v.get("xbrl_acceptance_missing")}
                          for t, v in detail.items()}
    return fundamentals, valuation


def collect(as_of: date, decision_at: datetime, universe_path: str | Path = DEFAULT_UNIVERSE,
            mode: str = "live", now: datetime | None = None) -> dict:
    """라이브 데이터셋. `source: live`, 실패 내역은 `diagnostics.degraded` 에 남는다.

    `mode` 는 경계 계약(§4.2)의 키워드다 — `run_daily` 는 기본값(`live`)을 그대로 쓴다.
    거시만 `mode` 에 따라 경로가 갈린다(R07): 라이브는 최초 수집 시각, 재생은 빈티지 점조회.
    """
    config = load_universe(universe_path)
    rows = list(config.get("tickers") or [])
    tickers = [r["ticker"] for r in rows]
    benchmark = config.get("benchmark") or "SPY"
    diag: dict = {"mode": mode, "sources": {}, "degraded": []}
    panels = collect_prices(tickers + [benchmark], as_of, decision_at, diag)
    # 거시 수집 시각은 `collect_macro` 가 잡는다(가격 수집에 걸린 시간을 지연으로 우기지 않는다).
    macro, macro_prev = collect_macro(decision_at, diag, mode=mode, now=now)

    sec_client: SecClient | None = None
    cik_by_ticker: dict[str, int] = {}
    ua = os.environ.get("SEC_USER_AGENT")
    if ua:
        try:
            sec_client = SecClient(user_agent=ua, cache_dir="data/cache/sec_facts")
            table = sec_client.company_tickers()
            cik_by_ticker = {str(t): int(c) for t, c in zip(table["ticker"], table["cik"], strict=False)}
        except Exception as exc:  # noqa: BLE001
            _note(diag, "sec", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")

    acceptance: dict[str, dict[str, datetime]] = {}
    filings = collect_filings(tickers, decision_at, diag, client=sec_client,
                              cik_by_ticker=cik_by_ticker, acceptance_out=acceptance)
    news = collect_news(tickers, decision_at, diag)
    meta = collect_meta(tickers, diag)
    sectors = {r["ticker"]: r.get("sector") or "" for r in rows}
    dgs10 = macro.get("dgs10")
    risk_free = (float(dgs10) / 100.0) if dgs10 is not None else None  # FRED 는 %p, WACC 는 소수
    fundamentals, valuation = collect_fundamentals(
        tickers, decision_at, diag, panels, panels.get(benchmark), risk_free,
        sectors, client=sec_client, cik_by_ticker=cik_by_ticker, acceptance=acceptance,
    )
    for source, info in diag["sources"].items():
        if info["status"] not in ("ok",):
            diag["degraded"].append(f"{source}:{info['status']}")
    universe = []
    for row in rows:
        ticker = row["ticker"]
        panel = panels.get(ticker)
        if panel is None or panel.frame.empty:
            continue
        m = meta.get(ticker) or {}
        v = valuation.get(ticker) or {}
        f = fundamentals.get(ticker)
        market_cap = m.get("market_cap_usd")
        if not market_cap and f is not None and np.isfinite(f.diluted_shares[0] if f.diluted_shares else np.nan):
            market_cap = float(panel.frame.iloc[-1]["sadj_close"]) * float(f.diluted_shares[0]) * 1e6
        universe.append({
            **row,
            "exchange": m.get("exchange") or "UNKNOWN",
            "history_rows": int(len(panel.frame)),
            "adv20_usd": float((panel.frame["raw_close"] * panel.frame["volume"]).tail(20).mean())
            if "volume" in panel.frame else None,
            "market_cap_usd": market_cap,
            "iv_mult": v.get("iv_mult"),
            "iv_median": v.get("iv_median"),
            "iv_low": v.get("iv_low"),
            "iv_high": v.get("iv_high"),
            "iv_band_basis": v.get("iv_band_basis"),
            "median_method": v.get("median_method"),
            "wacc_qc": v.get("wacc_qc"),
            "wacc_valuation": v.get("wacc"),
            "v_penalties": v.get("v_penalties"),
            "v_penalty_dcf": v.get("v_penalty_dcf"),
            "v_penalty_hist": v.get("v_penalty_hist"),
            "v_confidence": v.get("v_confidence"),
            "v_confidence_reason": v.get("v_confidence_reason"),
            "dcf_sensitivity": v.get("dcf_sensitivity"),
            "ev_ebit_percentile": v.get("ev_ebit_percentile"),
            "ev_ebit_median_10y": v.get("ev_ebit_median_10y"),
            "iv_hist_ev_ebit": v.get("iv_hist_ev_ebit"),
            "peg": v.get("peg"),
            "revenue_cagr": v.get("revenue_cagr"),
            "xbrl_completeness": v.get("xbrl_completeness", 0.0),
            "beta": v.get("beta"),
        })
    return {
        "as_of": as_of.isoformat(),
        "universe": universe,
        "panels": {k: v for k, v in panels.items()},
        "macro": macro,
        "macro_previous": macro_prev,
        "macro_ingest": diag.get("macro_ingest"),
        "filings": filings,
        "news": news,
        "fundamentals": fundamentals,
        "valuation_inputs": valuation,
        "source": "live",
        "diagnostics": diag,
        "benchmark": benchmark,
    }
