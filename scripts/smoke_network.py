#!/usr/bin/env python3
"""scripts/smoke_network.py — 실제 외부 API 스모크 테스트(1회).

- yfinance: AAPL(분할 이력)·MSFT·SPY 의 3종 PriceKind 생성과 분할 역산 대조
- SEC EDGAR: company_tickers.json (User-Agent 필수) + AAPL submissions 1건
- FRED: DFF·CPIAUCSL·T10Y2Y·BAMLH0A0HYM2·VIXCLS·DTWEXBGS 실제 조회 → `data/limits.json`
- Finnhub: MSFT quote + company-news 1콜

키는 **로컬 .env / 환경변수**에서만 온다. 결과는 `out/smoke_report.json`(gitignore)에 남긴다.
실패해도 단위 테스트는 오프라인 픽스처로 100% 재현된다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.data.macro import FredClient, build_limits  # noqa: E402
from engine.data.news import FinnhubClient  # noqa: E402
from engine.data.prices import build_price_frame, verify_against_raw_source  # noqa: E402
from engine.data.sec_edgar import SecClient, parse_submissions  # noqa: E402
from engine.metrics.technical import atr_pct, snapshot_indicators  # noqa: E402
from engine.models import PriceKind  # noqa: E402
from engine.utils.io import load_dotenv  # noqa: E402


def smoke_prices(symbols: list[str], period: str = "10y") -> dict:
    import yfinance as yf

    out = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period=period, auto_adjust=False, actions=True)
            splits = t.splits
            panel = build_price_frame(sym, hist, splits)
            frame = panel.frame
            ind = snapshot_indicators(panel)
            # 역산 대조: Yahoo 가 준 OHLC(분할 조정) × split_factor == raw_* (정의상 항상 참이므로
            # 분할 이벤트 존재 여부와 가격 연속성으로 검증한다)
            pre = frame[frame["split_factor"] != 1.0]
            info = {
                "rows": int(len(frame)),
                "first_date": frame.index[0].date().isoformat(),
                "last_date": frame.index[-1].date().isoformat(),
                "splits": {str(pd_ts.date()): float(ratio) for pd_ts, ratio in splits.items()},
                "split_adjusted_days": int(len(pre)),
                "max_reconstruction_error": float((frame["raw_close"] / (frame["sadj_close"] * frame["split_factor"]) - 1).abs().max()),
                "last_raw_close": float(frame["raw_close"].iloc[-1]),
                "last_sadj_close": float(frame["sadj_close"].iloc[-1]),
                "last_adj_close": float(frame["adj_close"].iloc[-1]),
                "atr_pct": ind.get("atr_pct"),
                "rsi14": ind.get("rsi14"),
                "available_at_last": str(frame["available_at"].iloc[-1]),
                "issues": panel.issues,
                "ok": True,
            }
            # 분할 전 구간에서 raw/sadj 비율이 분할비율과 일치하는지(2:1 이면 2.0, 4:1 이면 4.0)
            if len(pre):
                ratio = float((pre["raw_close"] / pre["sadj_close"]).median())
                info["raw_to_sadj_before_split"] = ratio
            out[sym] = info
        except Exception as e:  # noqa: BLE001
            out[sym] = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=2)}
    return out


def smoke_sec() -> dict:
    try:
        client = SecClient()
        tickers = client.company_tickers()
        aapl = tickers[tickers["ticker"] == "AAPL"]
        result = {
            "ok": True,
            "tickers": int(len(tickers)),
            "aapl_cik": int(aapl["cik"].iloc[0]) if len(aapl) else None,
            "user_agent_configured": bool(os.environ.get("SEC_USER_AGENT")),
        }
        if result["aapl_cik"]:
            subs = client.submissions(result["aapl_cik"])
            filings = parse_submissions("AAPL", result["aapl_cik"], subs, max_rows=5)
            result["latest_filings"] = [
                {"form": f["form"], "filed": f["filed_date"], "acceptance": f["acceptance_datetime"]}
                for f in filings
            ]
        return result
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def smoke_fred(series: list[str]) -> dict:
    try:
        client = FredClient()
        frames = {}
        result: dict = {"ok": True, "series": {}}
        for sid in series:
            try:
                df = client.observations(sid, observation_start="2018-01-01")
                frames[sid] = df
                first = df["date"].min() if not df.empty else None
                last = df["date"].max() if not df.empty else None
                last_val = df.dropna(subset=["value"])["value"].iloc[-1] if df["value"].notna().any() else None
                result["series"][sid] = {
                    "rows": int(len(df)),
                    "first": str(first),
                    "last": str(last),
                    "last_value": float(last_val) if last_val is not None else None,
                    "available_at_last": str(df["available_at"].iloc[-1]) if not df.empty else None,
                    "source": df["available_at_source"].iloc[-1] if not df.empty else None,
                }
            except Exception as e:  # noqa: BLE001
                result["series"][sid] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        limits = build_limits(frames)
        Path("data").mkdir(exist_ok=True)
        Path("data/limits.json").write_text(json.dumps(limits, ensure_ascii=False, indent=2), encoding="utf-8")
        result["limits_written"] = "data/limits.json"
        result["hy_oas_range"] = limits["series"].get("BAMLH0A0HYM2")
        result["hy_available_from"] = limits.get("hy_oas_available_from")
        result["documented_limit"] = "2023-11-20"
        result["hy_matches_document"] = limits.get("hy_oas_available_from") == "2023-11-20"
        return result
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def smoke_finnhub() -> dict:
    try:
        client = FinnhubClient()
        q = client.quote("MSFT")
        to = date.today()
        frm = to - timedelta(days=7)
        news = client.company_news("MSFT", frm.isoformat(), to.isoformat())
        return {
            "ok": True,
            "quote": {k: q.get(k) for k in ("c", "d", "dp", "h", "l", "o", "pc", "t")},
            "news_count_7d": len(news),
            "news_sample": [{"headline": n["headline"], "source": n["source"], "published_at": n["published_at"]} for n in news[:3]],
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=["AAPL", "MSFT", "SPY"])
    ap.add_argument("--series", nargs="*", default=["DFF", "CPIAUCSL", "T10Y2Y", "BAMLH0A0HYM2", "VIXCLS", "DTWEXBGS"])
    ap.add_argument("--out", default="out/smoke_report.json")
    ap.add_argument("--skip", nargs="*", default=[])
    args = ap.parse_args(argv)

    load_dotenv()
    report: dict = {"generated_at": date.today().isoformat(), "skipped": args.skip}
    if "prices" not in args.skip:
        report["yfinance"] = smoke_prices(args.symbols)
    if "sec" not in args.skip:
        report["sec"] = smoke_sec()
    if "fred" not in args.skip:
        report["fred"] = smoke_fred(args.series)
    if "finnhub" not in args.skip:
        report["finnhub"] = smoke_finnhub()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "yfinance": {k: v.get("ok") for k, v in report.get("yfinance", {}).items()},
        "sec": report.get("sec", {}).get("ok"),
        "fred": report.get("fred", {}).get("ok"),
        "hy_oas": report.get("fred", {}).get("hy_oas_range"),
        "finnhub": report.get("finnhub", {}).get("ok"),
        "out": str(out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
