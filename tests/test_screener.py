"""test_screener — 하드게이트·워치리스트 규칙(최소 개수 없음, 섹터 캡, 보류 업종)."""

from __future__ import annotations

from engine.screener import Candidate, GATE_LABELS, hard_gates, screen, score_candidate
from tests.helpers import annual_fundamentals


def make(ticker="AAA", sector="Technology", cap=5e10, adv=2e8, price=100.0, rows=2000,
         margin=0.35, peg=1.1, growth=0.08, exchange="NYSE", xbrl=1.0, flags=None):
    return Candidate(
        ticker=ticker, sector=sector, market_cap_usd=cap, adv20_usd=adv, price=price,
        history_rows=rows, exchange=exchange, xbrl_completeness=xbrl,
        accounting_flags=flags or [], fundamentals=annual_fundamentals(), margin=margin,
        peg=peg, revenue_cagr=growth,
    )


def test_hard_gate_failures_are_recorded():
    c = make(cap=2e9, adv=1e6, price=3.0, rows=200, exchange="OTC", xbrl=0.5, flags=["8-K 4.02"])
    fails = hard_gates(c)
    for code in ("HG-01", "HG-02", "HG-03", "HG-04", "HG-10", "HG-11", "HG-12"):
        assert code in fails
    result = screen([c])
    assert result["watchlist"] == []
    assert result["excluded"][0]["ticker"] == c.ticker
    assert all(code in GATE_LABELS for code in result["excluded"][0]["gates"])


def test_zero_survivors_is_normal():
    result = screen([make(cap=1e9)])
    assert result["count"] == 0
    assert len(result["excluded"]) == 1


def test_nine_survivors_are_kept_without_forced_minimum():
    cands = [make(ticker=f"T{i}", sector=f"S{i % 3}") for i in range(9)]
    result = screen(cands)
    assert result["count"] == 9                    # 20개를 채우지 않는다
    assert result["excluded"] == []                # 기준을 낮춰 채우지도, 탈락시키지도 않는다


def json_note(result):
    return result["notes"]


def test_sector_cap_six():
    cands = [make(ticker=f"T{i}", sector="Technology", margin=0.40 - i * 0.01) for i in range(10)]
    result = screen(cands, sectors_max=6)
    assert result["count"] == 6
    assert result["sector_counts"]["Technology"] == 6


def test_ranking_uses_half_q_third_v_fifth_g():
    high_margin = make(ticker="HIGH", margin=0.40)
    low_margin = make(ticker="LOW", margin=0.05)
    result = screen([low_margin, high_margin])
    assert [w["ticker"] for w in result["watchlist"]] == ["HIGH", "LOW"]
    s = score_candidate(high_margin)
    assert s["rank_score"] == round(0.5 * s["q"] + 0.3 * s["v"] + 0.2 * s["g"], 3)


def test_missing_margin_is_excluded_not_scored_zero():
    c = make(margin=None)
    result = screen([c])
    assert result["watchlist"] == []
    assert result["excluded"][0]["gates"] == ["V_UNAVAILABLE"]


def test_watchlist_max_40():
    cands = [make(ticker=f"T{i}", sector=f"S{i % 10}", margin=0.30 + (i % 5) * 0.01) for i in range(60)]
    result = screen(cands, sectors_max=10, watchlist_max=40)
    assert result["count"] == 40
