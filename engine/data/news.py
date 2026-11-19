"""engine.data.news — Finnhub 뉴스·보조 시세(무료 60콜/분).

Phase 1 범위에서는 **스모크 테스트와 수집 뼈대**만 둔다(뉴스 판독은 Phase 4).
본문 발췌는 저장하지 않고 제목·매체·시각·URL 만 남긴다(2.2.4).
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
            })
        return out
