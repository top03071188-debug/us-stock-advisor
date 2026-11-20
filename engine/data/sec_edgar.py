"""engine.data.sec_edgar — SEC EDGAR 수집(company_tickers · submissions · companyfacts XBRL).

규칙(2.4.2):
- `User-Agent` 에 이름 + 연락 이메일이 없으면 403. **이메일은 코드에 하드코딩하지 않는다**(Secret/로컬 .env).
- 초당 10요청 이하.
- 재무 값은 `filed` 날짜와 접수 시각(`acceptanceDateTime`)을 함께 저장한다 — look-ahead 차단의 핵심(4.5).
- 공시 원문 URL·item·해시를 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

SEC_DATA = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

#: companyfacts 에서 매핑하는 XBRL 태그(2.4.2 예시)
XBRL_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "net_income": ["NetIncomeLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "equity": ["StockholdersEquity"],
    "long_term_debt": ["LongTermDebtNoncurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
}


class RateLimiter:
    """초당 max_per_sec 요청(기본 10) — SEC 규정."""

    def __init__(self, max_per_sec: int = 10, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / max_per_sec
        self._clock = clock
        self._sleep = sleep
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            delta = now - self._last
            if delta < self.interval:
                self._sleep(self.interval - delta)
                now = self._clock()
            self._last = now


def default_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT 가 없다 — 형식 '<app>/<version> (contact: <email>)'. 공개 저장소에 하드코딩하지 않는다."
        )
    return ua


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SecClient:
    user_agent: str | None = None
    session: requests.Session | None = None
    cache_dir: str | None = None
    timeout: int = 25
    max_per_sec: int = 10

    def __post_init__(self) -> None:
        self.user_agent = self.user_agent or default_user_agent()
        self.session = self.session or requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})
        self.limiter = RateLimiter(self.max_per_sec)

    def _get(self, url: str) -> dict:
        self.limiter.wait()
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def company_tickers(self) -> pd.DataFrame:
        payload = self._get(TICKERS_URL)
        rows = [
            {"ticker": v["ticker"].upper(), "cik": int(v["cik_str"]), "title": v["title"]}
            for v in payload.values()
        ]
        return pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)

    def submissions(self, cik: int) -> dict:
        return self._get(f"{SEC_DATA}/submissions/CIK{int(cik):010d}.json")

    def company_facts(self, cik: int, max_age_hours: float = 24.0) -> dict:
        """XBRL companyfacts(수 MB). `cache_dir` 가 있으면 디스크 캐시를 쓰고 rate-limit 을 지킨다.

        캐시는 `{cache_dir}/CIK{10d}.json` 이고 `max_age_hours` 안이면 재요청하지 않는다.
        (일일 파이프라인이 같은 날 두 번 돌아도 SEC 를 두 번 두드리지 않는다.)
        """
        path = None
        if self.cache_dir:
            path = Path(self.cache_dir) / f"CIK{int(cik):010d}.json"
            if path.exists():
                age_h = (time.time() - path.stat().st_mtime) / 3600.0
                if age_h <= max_age_hours:
                    try:
                        return json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        pass
        payload = self._get(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{int(cik):010d}.json")
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                tmp.replace(path)
            except OSError:
                pass  # 캐시 실패는 수집 실패가 아니다
        return payload


def _norm_dt(value: str | None) -> str | None:
    """`acceptanceDateTime`(예: 2026-10-24T16:05:00.000Z) → ISO(UTC)."""
    if not value:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def parse_submissions(ticker: str, cik: int, payload: dict, max_rows: int = 40) -> list[dict]:
    """최근 공시를 정규화한다. 결정에 쓰는 시각은 `acceptance_datetime`(= available_at)."""
    recent = (payload.get("filings") or {}).get("recent") or {}
    n = min(len(recent.get("accessionNumber", [])), max_rows)
    out = []
    for i in range(n):
        acc = recent["accessionNumber"][i]
        acc_nodash = acc.replace("-", "")
        form = recent["form"][i]
        primary_doc = (recent.get("primaryDocument") or [None] * n)[i]
        items_raw = (recent.get("items") or [""] * n)[i] or ""
        items = [x.strip() for x in items_raw.split(",") if x.strip()]
        acceptance = _norm_dt((recent.get("acceptanceDateTime") or [None] * n)[i])
        filed = (recent.get("filingDate") or [None] * n)[i]
        out.append(
            {
                "ticker": ticker.upper(),
                "cik": int(cik),
                "accession": acc,
                "form": form,
                "filed_date": filed,
                "acceptance_datetime": acceptance,
                "available_at": acceptance,  # 접수 시각 = 세상에 나온 시각
                "items": items,
                "url": f"{SEC_ARCHIVES}/{int(cik)}/{acc_nodash}/{primary_doc or ''}",
                "primary_doc": primary_doc,
                "doc_hash": None,
            }
        )
    return out


def eight_k_items(filings: list[dict]) -> list[dict]:
    return [f for f in filings if f["form"].startswith("8-K")]


def parse_companyfacts(ticker: str, cik: int, payload: dict,
                       acceptance_by_accession: dict[str, str] | None = None) -> list[dict]:
    """companyfacts → 정규화 재무 rows.

    `acceptance_by_accession` 을 주면 접수 시각까지 붙는다(없으면 filed 날짜 16:00 ET 로 보수 처리).
    """
    facts = payload.get("facts", {}).get("us-gaap", {})
    metric_by_tag = {tag: metric for metric, tags in XBRL_TAGS.items() for tag in tags}
    rows: list[dict] = []
    for tag, metric in metric_by_tag.items():
        if tag not in facts:
            continue
        for unit, entries in (facts[tag].get("units") or {}).items():
            for e in entries:
                if e.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
                    continue
                acc = e.get("accn")
                filed = e.get("filed")
                acceptance = (acceptance_by_accession or {}).get(acc)
                if acceptance:
                    available_at = acceptance
                else:
                    ts = pd.Timestamp(filed, tz="America/New_York") + pd.Timedelta(hours=16)
                    available_at = ts.tz_convert("UTC").isoformat()
                rows.append(
                    {
                        "ticker": ticker.upper(),
                        "cik": int(cik),
                        "metric": metric,
                        "tag": tag,
                        "period_end": e.get("end"),
                        "filed_date": filed,
                        "acceptance_datetime": acceptance,
                        "form": e.get("form"),
                        "fy": e.get("fy"),
                        "fp": e.get("fp"),
                        "frame": e.get("frame"),
                        "value": e.get("val"),
                        "unit": unit,
                        "accession": acc,
                        "available_at": available_at,
                    }
                )
    df = pd.DataFrame(rows)
    if not df.empty:
        # 같은 (metric, period_end) 는 가장 먼저 공개된 값을 쓴다(사후 정정은 diff 로 남긴다)
        df = df.sort_values(["metric", "period_end", "available_at"]).reset_index(drop=True)
    return df.to_dict("records") if not df.empty else []


def filings_as_of(filings: list[dict], decision_at: datetime) -> list[dict]:
    """`acceptance_datetime ≤ decision_at` 만."""
    out = []
    for f in filings:
        if not f.get("available_at"):
            continue
        ts = pd.Timestamp(f["available_at"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if ts <= decision_at:
            out.append(f)
    return out


def hash_document(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()
