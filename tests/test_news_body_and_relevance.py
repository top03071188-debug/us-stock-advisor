"""v1.3.7 — 본문 추출 개선 · 종목 관련도 정렬 · `news_state` 분모/unknown 규칙.

실패했던 실제 현상을 그대로 재현한다:
- Yahoo Finance 링크가 `"Oops, something went wrong Skip to navigation…"` 껍데기로 1,500자를 채워
  \"본문 1,500자\"로 통과했고, 인용 대조가 전부 실패했다.
- Finnhub `summary` 가 수집 단계에서 버려져 본문이 막힌 기사에는 인용할 문장이 없었다.
- NVDA 에 `Frontline Ltd. Q3` 처럼 무관한 기사가 상위에 섞였고, 봇 차단 기사 몇 건 때문에
  나머지를 다 읽어도 `insufficient_evidence` 로 떨어졌다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_debate.context import (
    BODY_MIN_CHARS,
    build_context,
    fetch_article_body,
    fetch_article_body_detail,
)
from codex_debate.validate import Validated, evaluate_news_state
from engine.data.collect import collect_news
from engine.data.relevance import rank_news, relevance_score

T0 = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)   # 수요일 09:00 ET — 평일 기준 뉴스 창(48h)


def at_hour(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


BODY_A = ("Microsoft said it now expects Azure growth of 18%, down from 24%. "
          "It cited enterprise slowdown and a longer sales cycle across large customers.")
BODY_B = ("The company announced a 10 billion dollar buyback program on Tuesday and raised its "
          "quarterly dividend by 10 percent, citing strong free cash flow.")
SHELL = ("Oops, something went wrong Skip to navigation Skip to main content Sign in Mail "
         "Finance Sports More News Today's headlines Markets screener Watchlists " * 12)


# ── 본문 추출기 ────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, text: str, status: int = 200, url: str = "https://example.com/article"):
        self.text = text
        self.status_code = status
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_get(monkeypatch: pytest.MonkeyPatch, resp: _Resp, seen: list[dict] | None = None) -> None:
    def fake_get(url, **kwargs):
        if seen is not None:
            seen.append({"url": url, **kwargs})
        return resp

    monkeypatch.setattr("requests.get", fake_get)


def test_jsonld_article_body_wins_over_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Nvidia said demand for its data center chips remained supply constrained. " * 6
    html = f"""<html><head><title>Nvidia results</title>
    <script type="application/ld+json">{json.dumps({"@type": "NewsArticle", "articleBody": body})}</script>
    </head><body><nav>Home Markets Watchlist Sign in</nav><p>Enable more personalization</p></body></html>"""
    _patch_get(monkeypatch, _Resp(html))
    detail = fetch_article_body_detail("https://example.com/a")
    assert detail["source"] == "jsonld"
    assert "demand for its data center chips" in detail["body"]
    assert "Watchlist" not in detail["body"]


def test_container_extraction_drops_nav_header_footer(monkeypatch: pytest.MonkeyPatch) -> None:
    html = f"""<html><head><title>Adobe earnings</title></head><body>
    <header>MarketWatch logo Subscribe now</header>
    <nav>Home Markets Investing Personal finance</nav>
    <article><h1>Adobe results</h1>
      <p>{BODY_A}</p>
      <p>{BODY_B}</p>
      <figure>chart caption here</figure>
    </article>
    <aside>Related: other stories you may like</aside>
    <footer>Copyright Example Media All rights reserved</footer></body></html>"""
    _patch_get(monkeypatch, _Resp(html))
    body = fetch_article_body("https://example.com/b")
    assert "Azure growth of 18%" in body
    assert "buyback program" in body
    for junk in ("MarketWatch logo", "Subscribe now", "Personal finance", "other stories", "Copyright Example"):
        assert junk not in body


def test_block_page_is_not_a_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Resp(f"<html><body><div>{SHELL}</div></body></html>"))
    detail = fetch_article_body_detail("https://example.com/c")
    assert detail["body"] == ""
    assert detail["source"] == "blocked"


def test_short_page_is_not_a_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Resp("<html><head><title>t</title></head><body><p>Insights that click</p></body></html>"))
    detail = fetch_article_body_detail("https://example.com/d")
    assert detail["body"] == ""
    assert detail["source"] == "thin"


def test_http_error_is_not_a_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Resp("<html>nope</html>", status=403))
    detail = fetch_article_body_detail("https://example.com/e")
    assert detail["body"] == "" and detail["source"] == "blocked"


def test_fetch_uses_browser_headers_and_follows_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    html = f"<html><body><article><p>{BODY_A}</p><p>{BODY_B}</p></article></body></html>"
    _patch_get(monkeypatch, _Resp(html, url="https://finance.yahoo.com/markets/article/x.html"), seen=seen)
    fetch_article_body_detail("https://finnhub.io/api/news?id=abc")
    assert seen and seen[0]["allow_redirects"] is True
    assert "Mozilla" in seen[0]["headers"]["User-Agent"]
    assert seen[0]["headers"]["Accept-Language"].startswith("en-US")


def test_long_jsonld_description_is_not_treated_as_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """신문사가 160자로 잘라 넣은 JSON-LD `description` 은 본문이 아니다(예전 160자 껍데기 통과의 원인)."""
    short = "Adobe joined more than 40 organizations to launch the AI Transparency Initiative. " * 2
    html = f"""<html><head><script type="application/ld+json">
    {json.dumps({"@type": "NewsArticle", "description": short})}</script></head>
    <body><article><p>{BODY_A}</p><p>{BODY_B}</p></article></body></html>"""
    _patch_get(monkeypatch, _Resp(html))
    detail = fetch_article_body_detail("https://example.com/f")
    assert detail["source"] == "article"
    assert "Azure growth of 18%" in detail["body"]


# ── 컨텍스트: summary 폴백·껍데기 차단 ──────────────────────────────────────
def _signals(news: list[dict] | None = None) -> dict:
    cutoff = at_hour(-6)
    return {
        "as_of": T0.date().isoformat(),
        "data_cutoff_at": cutoff,
        "signal_published_at": at_hour(-4),
        "order_cutoff_at": at_hour(2),
        "signals": [{"ticker": "MSFT", "news": news or [], "as_of": T0.date().isoformat()}],
        "watchlist": [{"ticker": "MSFT"}],
    }


def _item(index: int, *, body: str | None = None, summary: str | None = None) -> dict:
    item = {"id": f"n{index}", "headline": f"Microsoft news {index}", "source": "Testwire",
            "published_at": at_hour(-8), "url": f"https://example.com/n{index}"}
    if body is not None:
        item["body_excerpt_1500"] = body
    if summary is not None:
        item["summary"] = summary
    return item


def test_context_falls_back_to_summary_when_body_blocked() -> None:
    summary = ("Microsoft (MSFT) shares fell after the company said Azure growth would slow to 18%, "
               "citing longer sales cycles and enterprise budget scrutiny for the quarter.")
    ctx = build_context("MSFT", _signals(), news_items=[_item(1, summary=summary)],
                        fetch_body=True, body_fetcher=lambda url: "")
    article = ctx["news"][0]
    assert article["body_available"] is True
    assert article["body_source"] == "summary"
    assert "Azure growth" in article["body_excerpt_1500"]


def test_context_rejects_blocked_shell_body_in_input() -> None:
    """저장된 발췌가 차단 껍데기면 본문으로 인정하지 않고 summary 로 넘어간다."""
    summary = ("Microsoft said it now expects Azure growth of 18 percent, down from 24 percent, "
               "as large customers extend sales cycles into next year.")
    ctx = build_context("MSFT", _signals(), news_items=[_item(1, body=SHELL, summary=summary)], fetch_body=False)
    article = ctx["news"][0]
    assert article["body_source"] == "summary"
    assert "Oops" not in article["body_excerpt_1500"]


def test_context_marks_fetched_body_source() -> None:
    ctx = build_context("MSFT", _signals(), news_items=[_item(1)], fetch_body=True,
                        body_fetcher=lambda url: BODY_A)
    article = ctx["news"][0]
    assert article["body_available"] is True
    assert article["body_source"] == "article"


def test_context_drops_future_news() -> None:
    """시점 고정 — `data_cutoff_at` 이후 기사는 모델에 전달하지 않는다."""
    future = {"id": "n9", "headline": "Microsoft future news", "source": "Testwire",
              "published_at": at_hour(3), "url": "https://example.com/n9", "body_excerpt_1500": BODY_A}
    ctx = build_context("MSFT", _signals(), news_items=[_item(1, body=BODY_A), future])
    assert [a["id"] for a in ctx["news"]] == ["n1"]
    assert ctx["future_news_dropped"] == 1


# ── news_state: 분모는 '읽을 수 있었던 기사' ────────────────────────────────
def _round1(kept: list[tuple[str, str]]) -> Validated:
    data = {
        "skill": "buffett", "score": 10, "stance": "hold", "confidence": 0.5, "reasons": ["근거"],
        "news_read": [{"news_id": nid, "durability": durability, "impact": "negative", "severity": 30,
                       "quote_sha256": "x" * 64, "quote_verified": True, "quote_chars": 10}
                      for nid, durability in kept],
    }
    return Validated("round1", data, True, [], {}, {})


def _mixed_context(readable: int, blocked: int) -> dict:
    news = [_item(i, body=BODY_A) for i in range(1, readable + 1)]
    news += [_item(100 + i) for i in range(1, blocked + 1)]
    return build_context("MSFT", _signals(), news_items=news, fetch_body=True, body_fetcher=lambda url: "")


def test_readable_denominator_ignores_blocked_urls() -> None:
    """기사 5건 중 3건이 차단이어도, 읽은 2건으로 판독 완료가 될 수 있다(예전에는 40% 폐기로 탈락)."""
    ctx = _mixed_context(readable=2, blocked=3)
    state = evaluate_news_state(ctx, [_round1([("n1", "structural"), ("n2", "transient")])])
    assert state["state"] == "read_done"
    assert state["denominator"] == "readable"
    assert state["items_in"] == 2 and state["items_kept"] == 2
    assert state["readable_items"] == 2 and state["unreadable_items"] == 3
    assert state["drop_rate"] == 0.0


def test_single_readable_article_is_still_insufficient() -> None:
    ctx = _mixed_context(readable=1, blocked=3)
    state = evaluate_news_state(ctx, [_round1([("n1", "structural")])])
    assert state["state"] == "insufficient_evidence"
    assert "readable_items_lt_2" in state["reasons"]
    assert "partial_read_only_1" in state["reasons"]
    assert state["grade_cap"] == "WATCH" and state["confidence_cap"] == "C"


def test_no_readable_article_is_insufficient() -> None:
    ctx = _mixed_context(readable=0, blocked=4)
    state = evaluate_news_state(ctx, [])
    assert state["state"] == "insufficient_evidence"
    assert "readable_items_lt_2" in state["reasons"]
    assert state["denominator"] == "all"


def test_unreadable_ids_reported_separately() -> None:
    ctx = _mixed_context(readable=2, blocked=2)
    state = evaluate_news_state(ctx, [_round1([("n1", "structural"), ("n2", "transient")])])
    assert state["unreadable_news_ids"] == ["n101", "n102"]
    assert state["dropped_news_ids"] == []


# ── 관련도 정렬 ───────────────────────────────────────────────────────────
def test_relevance_ranking_pushes_unrelated_news_down() -> None:
    rows = [
        {"headline": "Frontline Ltd. Q3 2026 Earnings Call Summary", "published_at": at_hour(-1)},
        {"headline": "Navitas Sinks 15% as AI Chip Rout Deepens", "published_at": at_hour(-3),
         "summary": "Nvidia shares fell again, dragging AI chip suppliers lower."},
        {"headline": "Nvidia's troubles mount as China chip sales face scrutiny", "published_at": at_hour(-5)},
    ]
    ranked = rank_news(rows, "NVDA")
    assert ranked[0]["headline"].startswith("Nvidia's troubles")
    assert ranked[-1]["headline"].startswith("Frontline")


def test_ranking_never_returns_zero_items() -> None:
    rows = [{"headline": "Some unrelated market story", "published_at": at_hour(-1)},
            {"headline": "Another unrelated piece", "published_at": at_hour(-2)}]
    assert len(rank_news(rows, "NVDA")) == 2


def test_relevance_score_uses_ticker_summary_and_related_tickers() -> None:
    assert relevance_score({"headline": "NVDA stock falls"}, "NVDA") > 0
    assert relevance_score({"headline": "Chip rout deepens", "summary": "Nvidia led the decline"}, "NVDA") > 0
    assert relevance_score({"headline": "Chip rout deepens", "related_tickers": ["NVDA"]}, "NVDA") > 0
    assert relevance_score({"headline": "Frontline Q3 call", "summary": "tanker rates improved"}, "NVDA") == 0


# ── 수집: summary 보존 · Yahoo 보조 · 공개 파일 누출 금지 ────────────────────
class _FakeFinnhub:
    calls: list[tuple[str, str, str]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def company_news(self, symbol: str, from_date: str, to_date: str) -> list[dict]:
        _FakeFinnhub.calls.append((symbol, from_date, to_date))
        stamp = int((T0 - timedelta(hours=5)).timestamp())
        return [
            {"headline": "Microsoft cuts Azure guidance", "source": "Yahoo", "url": "https://finnhub.io/api/news?id=1",
             "published_at": stamp, "summary": "Microsoft said Azure growth would slow to 18 percent.",
             "provider": "finnhub"},
            {"headline": "Old unrelated story", "source": "Yahoo", "url": "https://finnhub.io/api/news?id=2",
             "published_at": int((T0 - timedelta(hours=60)).timestamp()), "summary": "too old",
             "provider": "finnhub"},
        ]


def test_collect_news_keeps_summary_and_adds_yahoo_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr("engine.data.collect.FinnhubClient", _FakeFinnhub)

    def fake_yahoo(symbol: str, limit: int = 10) -> list[dict]:
        return [
            # 같은 기사(제목만 같고 URL 이 다름) — 중복으로 접히고 빈 필드가 채워져야 한다.
            {"headline": "Microsoft cuts Azure guidance", "source": "Yahoo Finance",
             "url": "https://finance.yahoo.com/news/msft-azure", "published_at": at_hour(-5),
             "summary": "Microsoft said Azure growth would slow to 18 percent.", "provider": "yahoo"},
            {"headline": "Microsoft (MSFT) secures new AI chip deal", "source": "Yahoo Finance",
             "url": "https://finance.yahoo.com/news/msft-chips", "published_at": at_hour(-3),
             "summary": "The deal supports Azure capacity.", "provider": "yahoo"},
            # 미래 기사 — 시점 고정 위반이라 버려져야 한다.
            {"headline": "Microsoft future rumor", "source": "Yahoo Finance",
             "url": "https://finance.yahoo.com/news/future", "published_at": at_hour(2),
             "summary": "not yet", "provider": "yahoo"},
        ]

    monkeypatch.setattr("engine.data.collect.yahoo_news", fake_yahoo)
    diag: dict = {"mode": "live", "sources": {}, "degraded": []}
    news = collect_news(["MSFT"], T0, diag)

    rows = news["MSFT"]
    headlines = [row["headline"] for row in rows]
    assert "Microsoft cuts Azure guidance" in headlines
    assert "Microsoft (MSFT) secures new AI chip deal" in headlines
    assert "Old unrelated story" not in headlines       # 48시간 밖
    assert "Microsoft future rumor" not in headlines     # 미래 뉴스
    assert headlines.count("Microsoft cuts Azure guidance") == 1
    first = next(row for row in rows if row["headline"] == "Microsoft cuts Azure guidance")
    assert first["summary"].startswith("Microsoft said")
    assert first["available_at"] == first["published_at"]
    assert diag["sources"]["yahoo_news"]["status"] == "ok"


def test_collect_news_still_works_without_finnhub_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr("engine.data.collect.yahoo_news", lambda symbol, limit=10: [
        {"headline": "Microsoft news", "source": "Yahoo Finance", "url": "https://finance.yahoo.com/news/x",
         "published_at": at_hour(-2), "summary": "summary text", "provider": "yahoo"}])
    diag: dict = {"mode": "live", "sources": {}, "degraded": []}
    rows = collect_news(["MSFT"], T0, diag)["MSFT"]
    assert [row["headline"] for row in rows] == ["Microsoft news"]
    assert diag["sources"]["finnhub"]["status"] == "missing_key"


def test_quant_signals_never_carries_news_summary() -> None:
    """공개 `data/quant_signals.json` 에 기사 요약·본문이 섞이지 않는다(§2.2.3)."""
    payload = json.loads(Path("data/quant_signals.json").read_text(encoding="utf-8"))
    forbidden = {"summary", "body", "body_excerpt_1500", "quote"}

    def walk(node) -> None:
        if isinstance(node, dict):
            leaked = forbidden & set(node)
            assert not leaked, f"금지 키 노출: {leaked}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)


def test_body_min_chars_is_a_real_threshold() -> None:
    assert BODY_MIN_CHARS >= 200  # 160자 잘린 메타 설명이 본문으로 통과하지 않게
