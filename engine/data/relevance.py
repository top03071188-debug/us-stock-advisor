"""engine.data.relevance — 뉴스·종목 관련도 점수와 정렬 (v1.3.7).

Finnhub 무료 뉴스에는 종목과 직접 무관한 기사가 섞인다(예: NVDA 에 `Frontline Ltd. Q3`).
그 기사를 그대로 상단에 두면 토론 컨텍스트의 5~10칸을 잡기사가 차지해 **인용할 본문**이 줄어든다.

여기서는 기사 제목·요약에 **티커 표기·회사 별칭·핵심 사업 키워드**가 있는지만 본다.
본문을 읽지 않고도 계산되는 값이며(수집 단계에서 쓴다), 판정(정량 점수·등급)에는 쓰지 않는다.
정렬 규칙은 **보수적**이다 — 관련 기사만 남기다 0건이 되면 안 되므로, 무관 기사도 최대
`MAX_IRRELEVANT` 건까지 뒤에 붙이고, 관련 기사가 하나도 없으면 원래 순서(최신순)를 그대로 쓴다.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

#: 종목별 회사명·제품·핵심 사업 키워드. 유니버스(43종목) 기준이며, 목록에 없는 종목은
#: **티커 표기만**으로 판정한다(별칭을 지어내지 않는다 — 없는 이름으로 관련도를 만들면 오탐이 된다).
COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "AAPL": ("apple", "iphone", "ipad", "macbook", "app store", "apple intelligence"),
    "MSFT": ("microsoft", "azure", "windows", "xbox", "copilot", "linkedin", "bing",
             "office 365", "microsoft 365", "teams"),
    "NVDA": ("nvidia", "geforce", "cuda", "blackwell", "hopper", "rubin", "tensor core",
             "data center gpu"),
    "AVGO": ("broadcom", "vmware", "tomahawk", "custom silicon", "ai asic"),
    "ORCL": ("oracle", "oci", "oracle cloud", "netsuite", "people soft", "peoplesoft"),
    "ADBE": ("adobe", "photoshop", "creative cloud", "firefly", "acrobat", "experience cloud",
             "substance 3d", "figma"),
    "CSCO": ("cisco", "webex", "catalyst", "nexus", "splunk"),
    "GOOGL": ("alphabet", "google", "youtube", "waymo", "deepmind", "android", "gemini",
              "google cloud", "gcp", "search ads"),
    "GOOG": ("alphabet", "google", "youtube", "waymo", "deepmind", "android", "gemini",
             "google cloud", "gcp", "search ads"),
    "META": ("meta platforms", "facebook", "instagram", "whatsapp", "threads", "reels",
             "reality labs", "metaverse"),
    "NFLX": ("netflix", "streaming subscribers", "squid game"),
    "DIS": ("disney", "disney+", "espn", "marvel", "pixar", "lucasfilm", "abc network"),
    "AMZN": ("amazon", "aws", "alexa", "prime video", "prime membership", "amazon web services"),
    "TSLA": ("tesla", "model 3", "model y", "cybertruck", "megapack", "fsd", "optimus"),
    "HD": ("home depot",),
    "MCD": ("mcdonald", "mcdonald's", "big mac"),
    "NKE": ("nike", "jordan brand"),
    "WMT": ("walmart", "sam's club", "sams club"),
    "PG": ("procter", "procter & gamble", "p&g", "tide", "pampers", "gillette"),
    "KO": ("coca-cola", "coca cola", "coke"),
    "PEP": ("pepsico", "pepsi", "frito-lay", "frito lay", "gatorade", "quaker"),
    "COST": ("costco", "kirkland"),
    "JNJ": ("johnson & johnson", "johnson and johnson", "janssen"),
    "UNH": ("unitedhealth", "united health", "optum"),
    "LLY": ("eli lilly", "lilly", "zepbound", "mounjaro", "trulicity"),
    "MRK": ("merck", "keytruda", "gardasil"),
    "ABBV": ("abbvie", "humira", "skyrizi", "rinvoq"),
    "XOM": ("exxon", "exxonmobil", "exxon mobil"),
    "CVX": ("chevron",),
    "CAT": ("caterpillar",),
    "GE": ("general electric", "ge aerospace", "ge vernova"),
    "HON": ("honeywell",),
    "UPS": ("ups", "united parcel service"),
    "BA": ("boeing", "737", "787", "777x"),
    "JPM": ("jpmorgan", "jp morgan", "jamie dimon", "chase"),
    "BAC": ("bank of america", "bofa", "merrill"),
    "V": ("visa", "visa inc"),
    "MA": ("mastercard", "master card"),
    "LIN": ("linde",),
    "SHW": ("sherwin-williams", "sherwin williams"),
    "NEE": ("nextera", "next era energy", "florida power & light"),
    "DUK": ("duke energy",),
    "AMT": ("american tower",),
    "PLD": ("prologis",),
}

#: 제목에서 별칭을 찾았을 때·요약에서 찾았을 때의 가중치.
HEADLINE_ALIAS_SCORE = 2.0
SUMMARY_ALIAS_SCORE = 1.0
#: 티커 표기는 회사명보다 강한 신호지만, 1~2글자 티커(`V`·`MA`·`GE`·`PG`·`KO`)는
#: 본문 어디에나 나오는 글자라 약하게 센다.
HEADLINE_SYMBOL_SCORE = 3.0
SUMMARY_SYMBOL_SCORE = 1.5
SHORT_SYMBOL_SCORE = 0.5
#: 관련도 0(무관) 기사라도 뒤에 붙이는 최대 건수 — 기사가 통째로 사라지지 않게 한다.
MAX_IRRELEVANT = 2

_WS_RE = re.compile(r"\s+")


def _norm(text: Any) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", str(text))).strip()


def aliases_for(ticker: str) -> tuple[str, ...]:
    """종목 별칭 — 없으면 빈 튜플(티커 표기만 쓴다)."""
    return COMPANY_ALIASES.get(str(ticker or "").upper().strip(), ())


def _has_alias(text: str, aliases: Iterable[str]) -> bool:
    low = text.lower()
    for alias in aliases:
        if not alias:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])", low):
            return True
    return False


def _has_symbol(text: str, ticker: str) -> bool:
    """티커 표기 — 대문자 그대로의 단어만(소문자 `v`·`ma` 같은 일반 단어와 구분)."""
    if not ticker:
        return False
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(ticker) + r"(?![A-Za-z0-9])", text) is not None


def news_text(item: dict) -> str:
    """관련도 판정에 쓰는 제목·요약 문자열."""
    return " ".join(_norm(item.get(k)) for k in ("headline", "title", "summary") if item.get(k))

def relevance_score(item: dict, ticker: str, aliases: Iterable[str] | None = None) -> float:
    """기사 1건의 종목 관련도(0 = 무관). 제목의 신호가 요약보다 세다."""
    if not isinstance(item, dict):
        return 0.0
    ticker = str(ticker or "").upper().strip()
    alias_list = tuple(aliases) if aliases is not None else aliases_for(ticker)
    headline = _norm(item.get("headline") or item.get("title"))
    summary = _norm(item.get("summary"))
    related = {str(t).upper().strip() for t in (item.get("related_tickers") or [])}
    score = 0.0
    if ticker and ticker in related:
        # 수집기(Yahoo)가 “이 기사는 그 종목 기사”라고 표시한 경우 — 가장 강한 신호.
        score += HEADLINE_SYMBOL_SCORE
    short = len(ticker) <= 2
    if headline:
        if _has_symbol(headline, ticker):
            score += SHORT_SYMBOL_SCORE if short else HEADLINE_SYMBOL_SCORE
        elif _has_alias(headline, alias_list):
            score += HEADLINE_ALIAS_SCORE
    if summary:
        if _has_symbol(summary, ticker):
            score += SHORT_SYMBOL_SCORE if short else SUMMARY_SYMBOL_SCORE
        elif _has_alias(summary, alias_list):
            score += SUMMARY_ALIAS_SCORE
    return score


def _ts_key(item: dict) -> float:
    raw = str(item.get("published_at") or item.get("available_at") or "")
    if not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def rank_news(items: Iterable[dict], ticker: str, *, limit: int = 10,
              aliases: Iterable[str] | None = None,
              max_irrelevant: int = MAX_IRRELEVANT) -> list[dict]:
    """종목 관련 기사를 앞으로, 무관 기사를 뒤로 — **0건이 되지 않게** 보수적으로 자른다.

    - 관련 기사: 점수 내림차순, 같은 점수면 최신순.
    - 무관 기사: 최신순으로 최대 `max_irrelevant` 건만 뒤에 붙인다.
    - 관련 기사가 하나도 없으면 원래 순서(보통 최신순)를 그대로 `limit` 까지 쓴다.
    """
    rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        return []
    scored = [(relevance_score(item, ticker, aliases), index, item) for index, item in enumerate(rows)]
    relevant = [row for row in scored if row[0] > 0]
    if not relevant:
        return rows[:limit]
    irrelevant = [(score, index, item) for score, index, item in scored if score <= 0]
    relevant.sort(key=lambda row: (-row[0], -_ts_key(row[2]), row[1]))
    irrelevant.sort(key=lambda row: (-_ts_key(row[2]), row[1]))
    ordered = [row[2] for row in relevant] + [row[2] for row in irrelevant[:max_irrelevant]]
    return ordered[:limit]


__all__ = ["COMPANY_ALIASES", "MAX_IRRELEVANT", "aliases_for", "news_text", "rank_news",
           "relevance_score"]
