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

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from engine.data.macro import DEFAULT_SERIES, FredClient, as_of_macro, cutoff_vintage
from engine.data.news import FinnhubClient, yahoo_news
from engine.data.prices import PriceQualityError, build_price_frame
from engine.data.relevance import rank_news
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
from engine.utils.dates import KST, add_trading_days

DEFAULT_UNIVERSE = Path("config/universe.yaml")
#: 발행 원장 — 국면 표본의 **실제 원천**이다(발행 시점에 기록된 국면 + 만기일).
DEFAULT_LEDGER_RECORDS = Path("data/ledger/records.jsonl")
#: 일별 동결 입력 아카이브 — 국면 라벨이 없는 구 레코드의 국면을 **그 시점 값으로** 복원한다.
DEFAULT_SNAPSHOT_ROOT = Path("data/snapshots")
#: 국면 표본 기간 — 기획서 3.10 의 "해당 국면 백테스트 표본"은 3개월 채점 기준이다.
REGIME_SAMPLE_HORIZONS = {"1w": 5, "1m": 21, "3m": 63, "6m": 126}
DEFAULT_REGIME_SAMPLE_HORIZON = "3m"
#: A 등급 하한(기획서 3.10). 판정은 `pipeline.regime_sample_ok` 가 하지만, 공급 문서에도 같은 값을 적는다.
MIN_REGIME_SAMPLE = 30
#: 구 레코드의 국면을 되살릴 때 읽을 아카이브 상한 — 실행 시간을 묶어 둔다(입력 1건 ≈ 1MB).
MAX_ARCHIVE_LOOKUPS = 120
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


def cutoff_market_cap(panel, fundamentals, fast_info_cap: float | None, *, as_of: date,
                      today: date | None = None) -> tuple[float | None, str]:
    """시총 — **컷오프 기준 종가 × 컷오프 기준 주식수**가 우선이다.

    `yfinance fast_info.marketCap` 는 **오늘 값**이라 과거 컷오프 재수집·재생에 쓰면 look-ahead다
    (감사 09-b). 실시간 라이브(오늘 `as_of`)에서만 fast_info 를 먼저 쓰고, 과거 컷오프는
    그 시점 `raw_close`(당시 거래가) × 그 시점 주식수(`diluted_shares`)를 쓴다.
    컷오프 값이 없고 fast_info 만 있으면 값은 쓰되 `fast_info_fallback` 으로 출처를 드러낸다.
    """
    today = today or datetime.now(KST).date()
    live_today = as_of >= today
    if live_today and fast_info_cap:
        return float(fast_info_cap), "fast_info_market_cap"
    if panel is not None and not panel.frame.empty and fundamentals is not None:
        shares = getattr(fundamentals, "diluted_shares", None) or []
        value = float(shares[0]) if shares else float("nan")
        if np.isfinite(value) and value > 0:
            return float(panel.frame.iloc[-1]["raw_close"]) * value * 1e6, "cutoff_close_x_diluted_shares"
    if fast_info_cap:
        return float(fast_info_cap), "fast_info_fallback"
    return None, "missing"


def collect_prices(tickers: list[str], as_of: date, decision_at: datetime, diag: dict,
                   history_years: int = 10) -> dict:
    """yfinance 일봉 → `PricePanel`(3종 가격). 종목 단위 실패는 건너뛰고 사유를 남긴다.

    `history_years=10`(≈2,500 거래일) — 5년 이력 게이트(HG-04, `min_history_rows=1260`)를
    실데이터로 충족하면서, 10년 EV/EBIT 백분위·중앙값(v1.3.2)을 계산할 창을 만든다.
    3년 미만 상장 종목은 있는 행만큼만 받아 HG-04 로 탈락하고, 6~9년차 종목은 그 이력으로 계산한다.

    과거 컷오프는 **컷오프 시점 기준으로 재기준화**한 패널을 쓴다(`PricePanel.as_of_cutoff`) —
    컷오프 이후 분할을 가격 기준에서 제거해 `raw == sadj`·`split_factor == 1` 이 되게 한다(감사 09).
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
                out[ticker] = panel.as_of_cutoff(decision_at, splits)
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

    [v1.3.3 — 라이브·재생 같은 경로] **마감(`data_cutoff_at`) 빈티지 점조회**만 쓴다.
    - 빈티지는 `cutoff_vintage(decision_at)`(마감일 ET 전날)로 고정하고, 응답이 그보다 늦은
      빈티지(스냅샷)면 그 시리즈를 **버린다** — 마감 뒤에 수집한 값을 "그때 알 수 있었다"고
      우기지 않는다(감사 01).
    - 시점 필터는 **언제나 `decision_at`** 이다(`ref = decision_at`). 수집 시각(`ingest_at`)으로
      필터하면 마감 후 값이 그대로 들어와 레짐·WACC 무위험금리(DGS10)가 샌다.
    - 수집 지연은 숨기지 않고 `ingest`(`lag_seconds`·`after_cutoff`) 메타로 남긴다.
    - 확보하지 못한 시리즈는 `excluded`/`macro_missing_series` 에 남기고, 호출자가 `inputs_missing`
      으로 받아 레짐 `verified: false` 가 된다.
    """
    ingest_at = now or datetime.now(timezone.utc)
    if not os.environ.get("FRED_API_KEY") and client is None:
        _note(diag, "fred", "missing_key", "FRED_API_KEY 없음 — 레짐 미검증(포지션 배수 0.5)")
        return {}, {}
    replay = str(mode) == "replay"
    vintage = cutoff_vintage(decision_at)
    try:
        client = client or FredClient()
        frames = client.fetch_many(DEFAULT_SERIES, realtime_start=vintage, realtime_end=vintage,
                                   mode="replay")
    except Exception as exc:  # noqa: BLE001
        _note(diag, "fred", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
        return {}, {}
    # 라이브도 재생도 **마감 시각**을 시점 필터로 쓴다(수집 시각이 아니다).
    ref = decision_at
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
    lag = (ingest_at - decision_at).total_seconds()
    diag["macro_ingest"] = {
        "policy": "cutoff_vintage_point_query",
        "mode": str(mode),
        "data_cutoff_at": decision_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inputs_ingested_at": ingest_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "lag_seconds": round(lag, 1),
        "after_cutoff": bool(lag > 0),
        "available_at_source": "vintage_point_query",
        "vintage": vintage,
        "cutoff_filter": "available_at <= data_cutoff_at",
        "series_dropped": list(sorted(excluded)),
        "replay": replay,
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


def _news_time(value) -> datetime | None:
    """뉴스 시각 정규화 — unix 초·ISO 문자열·datetime 을 UTC datetime 으로."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _news_key(headline: str) -> str:
    """중복 판정 키 — 제목의 영숫자만 소문자로(매체마다 붙는 접미사·구두점 차이 흡수)."""
    return "".join(ch for ch in str(headline or "").lower() if ch.isalnum())[:80]


def _dedupe_news(rows: list[dict]) -> list[dict]:
    """같은 기사를 두 소스가 각각 줄 때 하나로 — 빈 필드(요약·관련 티커)는 채워 넣는다."""
    by_key: dict[str, dict] = {}
    by_url: dict[str, str] = {}
    order: list[str] = []
    for row in rows:
        headline = str(row.get("headline") or "")
        url = str(row.get("url") or "").strip()
        key = _news_key(headline)
        existing = by_key.get(key) or by_key.get(by_url.get(url, ""))
        if existing is not None:
            for field in ("summary", "related_tickers", "source"):
                if not existing.get(field) and row.get(field):
                    existing[field] = row[field]
            continue
        by_key[key] = row
        if url:
            by_url[url] = key
        order.append(key)
    return [by_key[key] for key in order]


def collect_news(tickers: list[str], decision_at: datetime, diag: dict,
                 *, supplement: bool = True) -> dict[str, list[dict]]:
    """Finnhub(+Yahoo 보조) 종목 뉴스 → 48시간·`available_at <= decision_at` 규칙 적용(2.7).

    **수집 하는 것**: 제목·매체·URL·발행 시각·`summary`(최대 280자).
    - `summary` 는 **비공개 스냅샷**(`data/snapshots/{as_of}/inputs.json` → 비공개 아카이브)과
      로컬 컨텍스트에만 남는다. 공개 `data/quant_signals.json` 은 `engine.decide.pipeline` 이
      제목·매체·시각·URL 만 골라 쓰므로 요약이 섞이지 않는다(§2.2.3·§2.2.4).
    - `supplement=True` 면 Yahoo Finance 뉴스(키 불필요)를 합친다 — Finnhub 무료 뉴스는 매체가
      Yahoo 로 몰리거나 종목과 무관한 기사가 섞인다. 보조 소스 실패는 본 수집 실패가 아니다.
    - 종목 관련도 순으로 정렬한 뭐 10건(`rank_news`)을 돌려준다 — 무관 기사가 상단을 잡지 않게.
    """
    window_start = decision_at - timedelta(hours=NEWS_MAX_AGE_HOURS)
    out: dict[str, list[dict]] = {}
    client: FinnhubClient | None = None
    if not os.environ.get("FINNHUB_API_KEY"):
        _note(diag, "finnhub", "missing_key", "FINNHUB_API_KEY 없음 — 뉴스 미판독")
    else:
        try:
            client = FinnhubClient()
        except Exception as exc:  # noqa: BLE001
            _note(diag, "finnhub", "failed", f"{type(exc).__name__}: {str(exc)[:120]}")
    start = (decision_at.date() - timedelta(days=5)).isoformat()
    end = decision_at.date().isoformat()
    failed = 0
    yahoo_used = 0
    yahoo_empty = 0
    for index, ticker in enumerate(tickers):
        rows: list[dict] = []
        if client is not None:
            try:
                rows.extend(client.company_news(ticker, start, end))
            except Exception:  # noqa: BLE001 — 한 종목 실패는 그 종목만 비운다
                failed += 1
        if supplement:
            extra = yahoo_news(ticker)
            if extra:
                yahoo_used += 1
            else:
                yahoo_empty += 1
            rows.extend(extra)
        kept: list[dict] = []
        for row in rows:
            published = _news_time(row.get("published_at"))
            if published is None:
                continue
            # 시점 고정: 마감 이후 기사(미래 뉴스)와 48시간 밖 기사는 버린다.
            if published > decision_at or published < window_start:
                continue
            headline = row.get("headline")
            url = row.get("url")
            if not headline or not url:
                continue
            stamp = published.isoformat().replace("+00:00", "Z")
            entry = {
                "headline": headline,
                "source": row.get("source"),
                "url": url,
                "published_at": stamp,
                "available_at": stamp,
                "summary": (row.get("summary") or "")[:280],
            }
            if row.get("related_tickers"):
                entry["related_tickers"] = list(row["related_tickers"])[:8]
            kept.append(entry)
        kept = _dedupe_news(kept)
        kept.sort(key=lambda row: str(row.get("published_at")), reverse=True)
        out[ticker] = rank_news(kept, ticker, limit=10)
        if supplement and index and index % 10 == 0:
            time.sleep(0.5)  # Yahoo 보조 호출 간 짧은 휴지 — 몰아 치지 않는다
    if client is not None:
        _note(diag, "finnhub", "ok" if out else "failed", f"실패 {failed}건" if failed else None)
    if supplement:
        _note(diag, "yahoo_news", "ok" if yahoo_used else "failed",
              f"사용 {yahoo_used}종목 · 빈 응답 {yahoo_empty}종목")
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


def load_ledger_records(path: str | Path = DEFAULT_LEDGER_RECORDS) -> list[dict]:
    """발행 원장(`records.jsonl`) — 한 줄에 한 신호. 없는 파일·깨진 줄은 건너뛴다."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("as_of"):
            rows.append(row)
    return rows


def _regime_label(value) -> str | None:
    """레코드의 국면 값(`"neutral"` 또는 `{"regime": "neutral", ...}`) → 국면 이름."""
    if isinstance(value, dict):
        value = value.get("regime")
    text = str(value or "").strip().lower()
    return text or None


def _archive_regime(snapshot_id: str | None, day: date | None, snapshots_root: Path,
                    cache: dict, budget: list[int]) -> str | None:
    """구 레코드의 국면을 **그 시점 동결 입력**으로 복원한다(사후 집계가 아니다).

    `data/snapshots/{snapshot_id}/inputs.json` 의 `macro`·`macro_previous` 는 그 결정 시점의
    빈티지 값이다. 사후에 최신 거시값으로 다시 분류하면 그 시점에 알 수 없었던 값이 들어간다.
    투표가 3개 미만(`verified: false`)이면 국면을 단정하지 않고 `None` 을 돌려준다.
    """
    key = str(snapshot_id or (day.isoformat() if day else "")) or None
    if key is None:
        return None
    if key in cache:
        return cache[key]
    if budget[0] <= 0:
        return None
    candidates: list[Path] = []
    if snapshot_id:
        candidates.append(snapshots_root / str(snapshot_id))
    if day is not None:
        base = snapshots_root / day.isoformat()
        candidates.append(base)
        if base.parent.exists():
            candidates.extend(sorted(base.parent.glob(f"{day.isoformat()}-r*"), reverse=True))
    regime = None
    for folder in candidates:
        path = folder / "inputs.json"
        if not path.exists():
            continue
        budget[0] -= 1
        try:
            inputs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            break
        from engine.decide.macro import classify_regime  # noqa: PLC0415 — 순환 임포트 방지
        view = classify_regime((inputs or {}).get("macro"), (inputs or {}).get("macro_previous"))
        regime = view.regime if view.verified else None
        break
    cache[key] = regime
    return regime


def _panel_last_bar(panel) -> date | None:
    """패널의 마지막 봉 날짜 — 이 시점에 **관측할 수 있었던** 마지막 값의 기준이다."""
    if panel is None or getattr(panel, "frame", None) is None or panel.frame.empty:
        return None
    last = panel.frame.index[-1]
    return last.date() if hasattr(last, "date") else None


def regime_sample_counts(as_of: date, panels: dict | None = None, *,
                         records_path: str | Path = DEFAULT_LEDGER_RECORDS,
                         snapshots_root: str | Path = DEFAULT_SNAPSHOT_ROOT,
                         horizon: str = DEFAULT_REGIME_SAMPLE_HORIZON,
                         min_n: int = MIN_REGIME_SAMPLE) -> dict:
    """국면별 **시점 확정 표본 수** — 신뢰도 A 게이트(기획서 3.10)에 공급하는 실제 집계.

    원천은 발행 원장(`data/ledger/records.jsonl`)이다: 각 신호는 발행 시점의 국면과 `as_of` 를
    남기고, 채점 기간(`horizon`, 기본 3개월 = 63거래일)의 만기일은 `as_of` 에서 거래일로 계산된다.
    표본으로 세는 조건은 세 가지다.

    1. **국면이 그 시점에 정해져 있다** — 레코드의 `regime`(발행 시점 기록) 또는 그 스냅샷의
       동결 입력(`inputs.json`)으로 복원. 복원도 안 되면 `no_regime` 으로 뺐다(추정 국면 금지).
    2. **만기가 `as_of` 보다 이전이다** — `maturity < as_of`. 이후에 만기가 오는 신호는 표본이
       아니다(시점 계약: 미래 만기 표본을 빌려 오지 않는다).
    3. **그 만기 봉을 실제로 관측할 수 있었다** — 현재 수집 패널(마감까지 잘린 프레임)의 마지막
       봉이 만기일 이상. 봉이 없으면 `no_price` 로 뺐다(관측 못 한 채점을 표본으로 세지 않는다).

    그래서 30건 미만이면 실제 수치만큼 `B` 상한이 유지되고, 30건 이상 쌓이면 같은 배선에서 A 경로가
    자동으로 열린다. 판정은 `engine.decide.pipeline.regime_sample_ok` 가 한다.
    """
    h = int(REGIME_SAMPLE_HORIZONS.get(str(horizon), REGIME_SAMPLE_HORIZONS[DEFAULT_REGIME_SAMPLE_HORIZON]))
    horizon_name = str(horizon) if str(horizon) in REGIME_SAMPLE_HORIZONS else DEFAULT_REGIME_SAMPLE_HORIZON
    root = Path(snapshots_root)
    by_regime: dict[str, dict] = {name: {"n": 0, "min_n": int(min_n), "matured_as_of": as_of.isoformat(),
                                         "horizon": horizon_name, "horizon_td": h,
                                         "source": "ledger_records"}
                                  for name in ("risk_on", "neutral", "risk_off")}
    counts = {"records": 0, "matured": 0, "duplicate": 0, "no_regime": 0,
              "not_matured": 0, "no_price": 0}
    records = load_ledger_records(records_path)
    counts["records"] = len(records)
    cache: dict = {}
    budget = [MAX_ARCHIVE_LOOKUPS]
    seen: set[str] = set()
    observed_through: date | None = None
    for panel in (panels or {}).values():
        last = _panel_last_bar(panel)
        if last is not None and (observed_through is None or last > observed_through):
            observed_through = last
    reason = None
    if not Path(records_path).exists():
        reason = "ledger_missing"
    for record in records:
        try:
            day = date.fromisoformat(str(record.get("as_of"))[:10])
        except ValueError:
            counts["no_regime"] += 1
            continue
        ticker = str(record.get("ticker") or "").upper()
        signal_key = str(record.get("signal_id") or f"{day.isoformat()}:{ticker}:{record.get('snapshot_id')}")
        if signal_key in seen:
            counts["duplicate"] += 1
            continue
        seen.add(signal_key)
        regime = _regime_label(record.get("regime")) or _archive_regime(
            record.get("snapshot_id"), day, root, cache, budget)
        if regime not in by_regime:
            counts["no_regime"] += 1
            continue
        maturity = add_trading_days(day, h)
        # 시점 계약: `as_of` 이전에 만기가 끝난 신호만 표본이다(같은 날 만기 포함 금지).
        if maturity >= as_of:
            counts["not_matured"] += 1
            continue
        last_bar = _panel_last_bar((panels or {}).get(ticker))
        if last_bar is None or last_bar < maturity:
            counts["no_price"] += 1
            continue
        by_regime[regime]["n"] += 1
        counts["matured"] += 1
    if counts["records"] == 0 and reason is None:
        reason = "no_records"
    return {
        "as_of": as_of.isoformat(),
        "matured_as_of": as_of.isoformat(),
        "observed_through": None if observed_through is None else observed_through.isoformat(),
        "horizon": horizon_name,
        "horizon_td": h,
        "min_n": int(min_n),
        "source": "ledger_matured_signals",
        "verified": bool(counts["matured"] > 0),
        "reason": reason,
        "by_regime": by_regime,
        "counts": counts,
        "note_ko": ("발행 원장의 국면·만기·관측 봉으로 센 표본이다 — 사후 집계·폴드 총수를 쓰지 않는다. "
                    "30건 미만인 국면은 A 상한(B)이 유지된다."),
    }


def collect(as_of: date, decision_at: datetime, universe_path: str | Path = DEFAULT_UNIVERSE,
            mode: str = "live", now: datetime | None = None,
            today: date | None = None,
            *, ledger_records: str | Path | None = None,
            snapshots_root: str | Path | None = None,
            regime_sample_horizon: str = DEFAULT_REGIME_SAMPLE_HORIZON) -> dict:
    """라이브 데이터셋. `source: live`, 실패 내역은 `diagnostics.degraded` 에 남는다.

    `mode` 는 경계 계약(§4.2)의 키워드다 — `run_daily` 는 기본값(`live`)을 그대로 쓴다.
    거시는 라이브·재생이 **같은 경로**(마감 빈티지 점조회)를 쓴다(v1.3.3).
    `today` 는 "오늘" 의 기준일이다(기본: 실제 오늘) — 오늘 실행에서만 fast_info 시총을 허용한다.

    `ledger_records`·`snapshots_root`·`regime_sample_horizon` 은 국면 표본 공급(감사 5331ab7-12)의
    입력 경로다. 기본값은 저장소의 `data/ledger/records.jsonl`·`data/snapshots` 이며, 발행 원장이
    없으면 빈 표본(`n: 0`)을 돌려준다 — 없는 표본을 통과로 바꾸지 않는다.
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
    # WACC 무위험금리의 출처를 남긴다 — 마감 빈티지에서 DGS10 을 못 얻으면 **기본값으로 계산했다**는
    # 사실이 보여야 한다(감사 01: 레짐·WACC 입력은 마감 이하 값만).
    diag["wacc_risk_free"] = {
        "source": "FRED:DGS10" if dgs10 is not None else "default",
        "value": risk_free,
        "fallback": dgs10 is None,
        "note_ko": None if dgs10 is not None else "DGS10 미확보(마감 빈티지) — 무위험금리 기본값으로 계산했다.",
    }
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
        market_cap, market_cap_source = cutoff_market_cap(
            panel, f, m.get("market_cap_usd"), as_of=as_of, today=today)
        # v1.3.3 04: 안전마진도 **여기서** 한 번 만든다 — 1층(run_screen)과 2층(pipeline)이
        # 같은 가격 기준(`sadj_close`)·같은 IV 로 계산하게 하려면 유니버스 행에 값이 있어야 한다.
        iv_median = v.get("iv_median")
        price_sadj = v.get("price")
        margin = (None if iv_median is None or price_sadj is None or float(iv_median) <= 0
                  else (float(iv_median) - float(price_sadj)) / float(iv_median))
        universe.append({
            **row,
            "exchange": m.get("exchange") or "UNKNOWN",
            "history_rows": int(len(panel.frame)),
            "adv20_usd": float((panel.frame["raw_close"] * panel.frame["volume"]).tail(20).mean())
            if "volume" in panel.frame else None,
            "market_cap_usd": market_cap,
            "market_cap_source": market_cap_source,
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
            "margin": margin,
            "dcf_sensitivity": v.get("dcf_sensitivity"),
            "ev_ebit_percentile": v.get("ev_ebit_percentile"),
            "ev_ebit_median_10y": v.get("ev_ebit_median_10y"),
            "ev_ebit_current": v.get("ev_ebit_current"),
            "iv_hist_ev_ebit": v.get("iv_hist_ev_ebit"),
            # v1.3.3 04·06: 부호 있는 괴리·EV/EBIT 표본 품질·성장률 클립 근거
            "hist_divergence_signed": v.get("hist_divergence_signed"),
            "ev_ebit_samples": v.get("ev_ebit_samples"),
            "ev_ebit_years": v.get("ev_ebit_years"),
            "ev_ebit_window": v.get("ev_ebit_window"),
            "ev_ebit_reason": v.get("ev_ebit_reason"),
            "growth_raw_cagr": v.get("growth_raw_cagr"),
            "growth_clipped_to_zero": v.get("growth_clipped_to_zero"),
            "effective_fcff_years": v.get("effective_fcff_years"),
            "normalization_method": v.get("normalization_method"),
            "wacc_sensitivity": v.get("wacc_sensitivity"),
            "terminal_g_sensitivity": v.get("terminal_g_sensitivity"),
            "peg": v.get("peg"),
            "revenue_cagr": v.get("revenue_cagr"),
            "xbrl_completeness": v.get("xbrl_completeness", 0.0),
            "beta": v.get("beta"),
        })
    diag["market_cap_basis"] = (
        "cutoff_close_x_diluted_shares" if as_of < (today or datetime.now(KST).date())
        else "fast_info_market_cap")
    # 감사 5331ab7-12: 국면 표본을 **실제 원장**에서 세어 공급한다. 실패해도 배치는 계속되고,
    # 표본을 못 채우면 `None`(=B 상한)으로 남는다 — 모르는 것을 통과로 쓰지 않는다.
    try:
        sample = regime_sample_counts(
            as_of, panels,
            records_path=ledger_records or DEFAULT_LEDGER_RECORDS,
            snapshots_root=snapshots_root or DEFAULT_SNAPSHOT_ROOT,
            horizon=regime_sample_horizon)
    except Exception as exc:  # noqa: BLE001 — 표본 공급 실패가 수집 실패는 아니다
        sample = {"as_of": as_of.isoformat(), "matured_as_of": as_of.isoformat(),
                  "observed_through": None, "horizon": regime_sample_horizon,
                  "horizon_td": REGIME_SAMPLE_HORIZONS.get(regime_sample_horizon),
                  "min_n": MIN_REGIME_SAMPLE, "source": "ledger_matured_signals",
                  "verified": False, "reason": f"regime_sample_error:{type(exc).__name__}",
                  "by_regime": {}, "counts": {"records": 0}}
    from engine.decide.macro import classify_regime  # noqa: PLC0415 — 순환 임포트 방지
    regime_view = classify_regime(macro, macro_prev)
    current = regime_view.regime if regime_view.verified else None
    sample["regime"] = current
    picked = (sample.get("by_regime") or {}).get(current) if current else None
    sample["n"] = (picked or {}).get("n") if isinstance(picked, dict) else None
    diag["regime_sample"] = {
        "source": sample.get("source"), "regime": current, "n": sample.get("n"),
        "min_n": sample.get("min_n"), "matured_as_of": sample.get("matured_as_of"),
        "horizon": sample.get("horizon"), "reason": sample.get("reason"),
        "counts": sample.get("counts"),
    }
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
        "regime_sample": sample,
        "source": "live",
        "diagnostics": diag,
        "benchmark": benchmark,
    }
