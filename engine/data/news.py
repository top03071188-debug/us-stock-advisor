"""engine.data.news — Finnhub 뉴스·보조 시세(무료 60콜/분).

Phase 1 범위에서는 **스모크 테스트와 수집 뼈대**만 둔다(뉴스 판독은 Phase 4).
본문 발췌는 저장하지 않고 제목·매체·시각·URL 만 남긴다(2.2.4).

v1.3.7: Finnhub `summary` 와 Yahoo 보조 뉴스를 **수집 단계에서 보존**한다 — 단, 저장 위치는
비공개 스냅샷(`data/snapshots/.../inputs.json`)과 로컬 컨텍스트뿐이다. 공개 파일에는
`engine.decide.pipeline` 이 제목·매체·시각·URL 만 골라 쓰므로 요약·본문이 섞이지 않는다.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

import requests

FINNHUB_BASE = "https://finnhub.io/api/v1"


@dataclass
class FinnhubClient:
    api_key: str | None = None
    session: requests.Session | None = None
    timeout: int = 20
    max_calls_per_min: int = 60
    _calls: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("FINNHUB_API_KEY")
        if not self.api_key:
            raise RuntimeError("FINNHUB_API_KEY 가 없다 — .env 또는 환경변수로만 주입한다")
        self.session = self.session or requests.Session()

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self.max_calls_per_min:
                wait = 60 - (now - self._calls[0]) + 0.5
                time.sleep(max(0.0, wait))
            self._calls.append(time.monotonic())

    def _get(self, path: str, **params) -> dict:
        self._throttle()
        params["token"] = self.api_key
        r = self.session.get(f"{FINNHUB_BASE}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def quote(self, symbol: str) -> dict:
        return self._get("/quote", symbol=symbol)

    def company_news(self, symbol: str, from_date: str, to_date: str) -> list[dict]:
        rows = self._get("/company-news", symbol=symbol, **{"from": from_date, "to": to_date})
        out = []
        for n in rows or []:
            out.append({
                "headline": n.get("headline"),
                "source": n.get("source"),
                "url": n.get("url"),
                "published_at": n.get("datetime"),   # unix seconds — available_at 의 근거
                "summary": (n.get("summary") or "")[:280],
                "provider": "finnhub",
            })
        return out


def yahoo_news(symbol: str, limit: int = 10) -> list[dict]:
    """Yahoo Finance 뉴스(`yfinance` `Ticker.news`) — **API 키가 필요 없는 보조 소스**(v1.3.7).

    Finnhub 무료 뉴스는 매체가 Yahoo 하나로 몰리거나 종목과 무관한 기사가 섞이기도 한다.
    Yahoo 뉴스는 `summary`(기사 요약)와 **원문 canonical URL**, `related_tickers` 를 함께 주기 때문에
    본문 추출이 막힌 기사에도 인용 가능한 실제 문장을 남길 수 있다.
    실패는 그 종목만 빈 리스트로 돌려준다 — 수집 파이프라인을 멈추지 않는다.
    """
    if not symbol:
        return []
    try:
        import yfinance as yf  # 지역 import — 오프라인 테스트에서 모듈 로딩 비용을 만들지 않는다

        raw = yf.Ticker(symbol).news or []
    except Exception:  # noqa: BLE001 — 보조 소스 실패는 본 수집 실패가 아니다
        return []
    out: list[dict] = []
    for row in list(raw)[:max(0, limit)]:
        if not isinstance(row, dict):
            continue
        content = row.get("content") if isinstance(row.get("content"), dict) else row
        title = content.get("title") or row.get("title")
        url = (((content.get("canonicalUrl") or {}).get("url"))
               or ((content.get("clickThroughUrl") or {}).get("url"))
               or row.get("link"))
        if not title or not url:
            continue
        related = []
        finance = content.get("finance") or {}
        for item in (finance.get("stockTickers") or []):
            ticker = str((item or {}).get("symbol") or "").upper().strip()
            if ticker:
                related.append(ticker)
        published = content.get("pubDate") or content.get("displayTime") or row.get("providerPublishTime")
        out.append({
            "headline": title,
            "source": ((content.get("provider") or {}).get("displayName")
                       or row.get("publisher") or "Yahoo Finance"),
            "url": url,
            "published_at": published,
            "summary": (content.get("summary") or row.get("summary") or "")[:280],
            "related_tickers": related,
            "provider": "yahoo",
        })
    return out
