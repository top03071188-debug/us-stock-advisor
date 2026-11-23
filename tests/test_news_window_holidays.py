"""휴장 다음 날 뉴스 공백 보완 — **유효 뉴스 창**의 수집·컨텍스트·검증 일관성 테스트.

실패했던 실제 현상(2026-11-23 07:00 KST `daily-postclose`): 고정 48시간 창이 금요일 장중 기사를
전부 잘라 `ADBE` 가 `news 0건` 이 됐다. 수집기만 넓히면 `valid_articles_in_window` 가 다시 48시간으로
자를 수 있으므로, 세 지점이 **같은 창**을 쓰는지 고정 날짜로 검증한다(벽시계 의존 없음).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from codex_debate.context import build_context, news_window_hours, select_targets
from codex_debate.validate import (
    STRUCTURAL_NUMBERS,
    evaluate_news_state,
    valid_articles_in_window,
)
from engine.data.collect import collect_news
from engine.utils.dates import effective_news_window_hours

#: 2026-11-23(월) 07:00 KST postclose = 2026-11-22T22:00Z = 일요일 17:00 ET
MONDAY_POSTCLOSE = datetime(2026, 11, 22, 22, 0, tzinfo=timezone.utc)
#: 2026-11-23(월) 21:50 KST premarket = 2026-11-23T12:50Z = 월요일 07:50 ET
MONDAY_PREMARKET = datetime(2026, 11, 23, 12, 50, tzinfo=timezone.utc)
#: 2026-11-18(수) 07:00 KST postclose — 평일이므로 창이 늘어나지 않는다.
WEDNESDAY_POSTCLOSE = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
#: 2026-11-26(목) 추수감사절 휴장 — 17:00 ET postclose
THANKSGIVING_POSTCLOSE = datetime(2026, 11, 26, 22, 0, tzinfo=timezone.utc)
#: 2026-11-27(금) 07:50 ET premarket — 전일이 휴장
BLACK_FRIDAY_PREMARKET = datetime(2026, 11, 27, 12, 50, tzinfo=timezone.utc)
#: 2027-01-18(월) MLK 휴장 — 17:00 ET postclose
MLK_POSTCLOSE = datetime(2027, 1, 18, 22, 0, tzinfo=timezone.utc)

BODY = ("Adobe said it now expects digital media revenue growth of 9 percent for the quarter "
        "and reiterated its full-year guidance for earnings per share.")
QUOTE = "expects digital media revenue growth of 9 percent"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ── 창 계산 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("decision_at, expected", [
    (WEDNESDAY_POSTCLOSE, 48),            # 평일 postclose — 그대로
    (datetime(2026, 11, 18, 12, 50, tzinfo=timezone.utc), 48),   # 평일 premarket — 그대로
    (MONDAY_POSTCLOSE, 72),               # 월요일 postclose — 금요일 하루 포함
    (MONDAY_PREMARKET, 96),               # 월요일 premarket — 금요일 + 주말
    (THANKSGIVING_POSTCLOSE, 48),         # 추수감사절 postclose — 수요일 하루 포함
    (BLACK_FRIDAY_PREMARKET, 72),         # 추수감사절 다음 날 premarket — 수요일 하루 포함
    (MLK_POSTCLOSE, 96),                  # 3일 연휴 다음 날 postclose — 금요일 하루 포함
])
def test_effective_news_window_hours(decision_at: datetime, expected: int) -> None:
    assert effective_news_window_hours(decision_at) == expected


def test_effective_news_window_never_below_base_and_handles_naive() -> None:
    # 기본값 아래로 줄지 않는다 — 창을 줄이면 기존에 읽던 기사가 사라진다.
    assert effective_news_window_hours(MONDAY_POSTCLOSE, base_hours=24) >= 24
    assert effective_news_window_hours(MONDAY_POSTCLOSE, base_hours=120) == 120
    # naive 는 UTC 로 본다(문서화된 처리).
    assert effective_news_window_hours(datetime(2026, 11, 22, 22, 0)) == 72


# ── 수집기 ───────────────────────────────────────────────────────────────
class _FakeFinnhub:
    calls: list[tuple[str, str, str]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def company_news(self, symbol: str, from_date: str, to_date: str) -> list[dict]:
        _FakeFinnhub.calls.append((symbol, from_date, to_date))
        return [
            # 실측 그대로: 금요일 15:13 ET — 48시간 창(금 17:00 ET)에 1시간 46분 못 미쳤던 기사
            {"headline": "Adobe dips as software stocks slide", "source": "Testwire",
             "url": "https://example.com/adbe-fri", "published_at": 1795205604,
             "summary": "Adobe shares fell with software peers on Friday."},
            # 목요일 — 72시간 창 밖
            {"headline": "Adobe old story", "source": "Testwire",
             "url": "https://example.com/adbe-old", "published_at": 1795100400,
             "summary": "older"},
        ]


def test_collect_news_keeps_previous_trading_day_articles(monkeypatch: pytest.MonkeyPatch) -> None:
    """월요일 배치가 **금요일 장중 기사**를 버리지 않는다 — ADBE 0건의 실제 원인."""
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr("engine.data.collect.FinnhubClient", _FakeFinnhub)
    monkeypatch.setattr("engine.data.collect.yahoo_news", lambda symbol, limit=10: [])
    _FakeFinnhub.calls = []
    diag: dict = {"mode": "live", "sources": {}, "degraded": []}
    rows = collect_news(["ADBE"], MONDAY_POSTCLOSE, diag)["ADBE"]
    headlines = [row["headline"] for row in rows]
    assert "Adobe dips as software stocks slide" in headlines
    assert "Adobe old story" not in headlines
    # Finnhub `from` 도 늘어난 창만큼 앞당겨야 연휴에 앞날이 잘리지 않는다.
    _, from_date, _ = _FakeFinnhub.calls[0]
    assert from_date <= (MONDAY_POSTCLOSE.date() - timedelta(days=6)).isoformat()


def test_collect_news_keeps_48h_rule_on_ordinary_weekday(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr("engine.data.collect.FinnhubClient", _FakeFinnhub)
    monkeypatch.setattr("engine.data.collect.yahoo_news", lambda symbol, limit=10: [])
    diag: dict = {"mode": "live", "sources": {}, "degraded": []}
    rows = collect_news(["ADBE"], WEDNESDAY_POSTCLOSE, diag)["ADBE"]
    assert [row["headline"] for row in rows] == []


# ── 컨텍스트 ─────────────────────────────────────────────────────────────
def _signals(cutoff: datetime, **extra) -> dict:
    return {
        "as_of": cutoff.astimezone(timezone.utc).date().isoformat(),
        "data_cutoff_at": _iso(cutoff),
        "signals": [{"ticker": "ADBE", "signal_id": "2026-11-23-ADBE-WATCH-001",
                     "trade_grade": "WATCH", "entry_score": 55.0}],
        **extra,
    }


def _news(*stamps: datetime) -> list[dict]:
    return [{"id": f"n{i}", "headline": f"Adobe news {i}", "source": "Testwire",
             "url": f"https://example.com/n{i}", "published_at": _iso(stamp),
             "body_excerpt_1500": BODY} for i, stamp in enumerate(stamps, start=1)]


def test_build_context_expands_window_on_monday_and_validator_agrees() -> None:
    friday_close = datetime(2026, 11, 20, 20, 13, 24, tzinfo=timezone.utc)   # 금 15:13 ET
    thu = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)                # 72h 밖
    ctx = build_context("ADBE", _signals(MONDAY_POSTCLOSE),
                        news_items=_news(friday_close, thu))
    assert ctx["news_window_hours"] == 72
    # 검증기도 같은 창을 쓴다 — 넓힌 기사를 다시 자르지 않는다.
    assert valid_articles_in_window(ctx) == ["n1"]


def test_build_context_keeps_48h_on_weekday() -> None:
    recent = WEDNESDAY_POSTCLOSE - timedelta(hours=30)
    old = WEDNESDAY_POSTCLOSE - timedelta(hours=60)
    ctx = build_context("ADBE", _signals(WEDNESDAY_POSTCLOSE), news_items=_news(recent, old))
    assert ctx["news_window_hours"] == 48
    assert valid_articles_in_window(ctx) == ["n1"]


def test_quant_signals_window_field_wins_and_legacy_context_recomputes() -> None:
    friday_close = datetime(2026, 11, 20, 20, 13, 24, tzinfo=timezone.utc)
    pinned = build_context("ADBE", _signals(MONDAY_POSTCLOSE, news_window_hours=48),
                           news_items=_news(friday_close))
    assert pinned["news_window_hours"] == 48
    assert valid_articles_in_window(pinned) == []
    # 구형 컨텍스트(필드 없음)도 `data_cutoff_at` 으로 같은 결론에 도달한다.
    legacy = dict(pinned)
    legacy.pop("news_window_hours")
    assert valid_articles_in_window(legacy) == ["n1"]


def test_news_window_hours_helper_defaults() -> None:
    assert news_window_hours({}, None) == 48
    assert news_window_hours({"news_window_hours": 96}, None) == 96
    assert news_window_hours({}, MONDAY_POSTCLOSE.replace(tzinfo=None)) == 72


def test_select_targets_counts_articles_in_effective_window() -> None:
    friday_close = _iso(datetime(2026, 11, 20, 20, 13, 24, tzinfo=timezone.utc))
    thu = _iso(datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc))
    signals = _signals(MONDAY_POSTCLOSE)
    signals["signals"][0]["news"] = [{"headline": "a", "url": "u1", "published_at": friday_close},
                                     {"headline": "b", "url": "u2", "published_at": thu}]
    picked = select_targets(signals)
    assert picked[0]["news_count_48h"] == 1     # 키 이름은 계약 호환, 값은 유효 창 기준
    assert picked[0]["news_window_hours"] == 72


# ── 판정 ─────────────────────────────────────────────────────────────────
def test_read_done_on_monday_with_expanded_window() -> None:
    """월요일 컨텍스트의 금요일 기사를 다 읽으면 `read_done` 이다(48h 로 자르면 0건 → not_read)."""
    stamps = [datetime(2026, 11, 20, 20, 13, tzinfo=timezone.utc),
              datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc),
              datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc),
              datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)]
    ctx = build_context("ADBE", _signals(MONDAY_POSTCLOSE), news_items=_news(*stamps))
    assert len(valid_articles_in_window(ctx)) == 4
    state = evaluate_news_state(ctx, [_round1(["n1", "n2", "n3", "n4"])])
    assert state["state"] == "read_done"
    assert state["articles"] == 4


def test_structural_numbers_cover_expanded_windows() -> None:
    for value in (72.0, 96.0, 120.0, 144.0):
        assert value in STRUCTURAL_NUMBERS


def _round1(ids: list[str]):
    """본문 인용이 검증된 1R 결과 1건(최소 형태)."""
    from codex_debate.validate import Validated

    return Validated(
        "round1",
        {"skill": "buffett", "score": 10.0, "stance": "hold", "confidence": 0.5, "reasons": [],
         "news_read": [{"news_id": nid, "durability": "structural", "impact": "negative",
                        "severity": 40.0, "quote_sha256": "sha256:0", "quote_verified": True,
                        "quote_chars": len(QUOTE)} for nid in ids]},
        True, [], {}, {})
