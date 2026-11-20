"""test_decide_signals — 매수/매도 트리거·등급·분할 계획·보유 추적 (기획서 §3.4·§3.5)."""

from __future__ import annotations

import pytest

from engine.decide.signals import (EntryContext, build_entry_plan, default_params, evaluate_entry, evaluate_exit,
                                   evaluate_holdings, expected_mdd, margin_of_safety, target_band, timing_score)


def ctx(**kw) -> EntryContext:
    base = dict(ticker="AAA", raw_close=91.5, sadj_close=91.5, sector="Technology",
                dma50=95.0, dma200=88.0, dma200_slope=0.01, rsi14=32.0, pct_b=0.15,
                atr_pct=0.03, iv_median=120.0, iv_high=150.0, q=80.0, v=75.0, g=60.0,
                structural_risk=10.0, in_watchlist=True)
    base.update(kw)
    return EntryContext(**base)


def hold(stop: float = 90.0, **kw) -> dict:
    base = dict(shares=10.0, avg_cost=100.0, stop_price=stop, trailing_high=110.0, initial_stop=stop)
    base.update(kw)
    return base


# ── 기본 계산 ────────────────────────────────────────────────────────────────
def test_margin_and_timing_and_band():
    assert margin_of_safety(100.0, 80.0) == pytest.approx(0.20)
    assert margin_of_safety(None, 80.0) is None
    t = timing_score(rsi14=30, dma50_deviation=-0.09, pct_b=0.1, support_score=1.0, volume_score=1.0)
    assert 90 <= t <= 100
    assert timing_score(rsi14=70, dma50_deviation=0.02, pct_b=1.0, support_score=0.0, volume_score=0.0) < 20
    band = target_band(iv_median=522.0, price=412.35)
    assert band["lo"] == pytest.approx(431.5, abs=0.2) and band["hi"] == pytest.approx(450.7, abs=0.2)
    assert band["r_lo"] == pytest.approx(band["lo"] / band["base_price"] - 1, abs=1e-6)
    assert band["defensive"] is False
    gy = target_band(iv_median=300.0, price=400.0)
    assert gy["defensive"] is True and gy["lo"] < gy["hi"] < 400.0
    assert expected_mdd(0.005) == pytest.approx(-0.05)   # 클램프 하한
    assert expected_mdd(0.05) == pytest.approx(-0.20)    # 클램프 상한


# ── 매수 게이트·등급 ─────────────────────────────────────────────────────────
def test_entry_all_gates_pass_returns_buy():
    ev = evaluate_entry(ctx(), "neutral")
    assert ev["failed_gates"] == []
    assert ev["grade"] in ("BUY", "STRONG_BUY")
    assert ev["signal"] == "BUY"


def test_b1_blocks_held_and_outside_watchlist():
    for kw in ({"held": True}, {"in_watchlist": False}):
        ev = evaluate_entry(ctx(**kw), "neutral")
        assert ev["gates"]["B1"] is False
        assert "B1" in ev["failed_gates"]
        assert ev["signal"] != "BUY"


def test_b3_threshold_follows_regime():
    c = ctx(iv_median=110.0)          # margin = (110-91.5)/110 = 16.8%
    assert evaluate_entry(c, "risk_on")["gates"]["B3"] is True     # 15%
    assert evaluate_entry(c, "neutral")["gates"]["B3"] is False    # 20%
    assert evaluate_entry(c, "risk_off")["gates"]["B3"] is False   # 25%


def test_b2_and_b4_conditions():
    weak = evaluate_entry(ctx(dma200=120.0, dma200_slope=-0.01, rsi14=50.0, pct_b=0.5, dma50=91.0),
                          "neutral")
    assert weak["gates"]["B2"] is False
    assert weak["gates"]["B4"] is False               # 눌림 조건 0개
    ok = evaluate_entry(ctx(dma200=120.0, dma200_slope=0.01, sadj_close=121.0, dma50=118.0), "neutral")
    assert ok["gates"]["B2"] is True                   # 기울기 양수 & 50일선 위


def test_valuation_suspended_sector_emits_no_signal():
    ev = evaluate_entry(ctx(valuation_suspended=True), "neutral")
    assert ev["signal"] == "SUSPENDED" and ev["grade"] == "NONE"
    assert ev["reason"] == "SECTOR_FORMULA_PENDING"
    assert ev["entry_score"] is None and ev["margin"] is None
    plan = build_entry_plan(ctx(valuation_suspended=True), ev, "neutral")
    assert plan["placeable"] is False


def test_growth_cap_and_news_cap_limit_grade_to_watch():
    ev = evaluate_entry(ctx(q=95.0, v=95.0, g=0.0), "neutral")
    assert ev["grade"] == "WATCH" and any("성장률" in r for r in ev["cap_reasons"])
    ev2 = evaluate_entry(ctx(news_state="insufficient_evidence"), "neutral")
    assert ev2["grade"] == "WATCH" and any("뉴스" in r for r in ev2["cap_reasons"])


def test_veto_blocks_b6():
    ev = evaluate_entry(ctx(vetoes=[{"code": "V01", "blocking": True, "name": "EARNINGS_WINDOW",
                                     "message_ko": "실적 발표 임박"}]), "neutral")
    assert ev["gates"]["B6"] is False
    assert ev["signal"] != "BUY"


def test_conflict_flag_from_investor_spread():
    ev = evaluate_entry(ctx(q=95.0, v=95.0, g=1.0), "neutral")
    assert ev["conflict"] is True and ev["spread"] >= 60


# ── 분할 계획 ────────────────────────────────────────────────────────────────
def test_entry_plan_shapes_and_no_same_day_price_input():
    """계획은 신호 시점 값만 쓴다 — 컨텍스트에 당일 시가·고가·저가 필드가 없다(LA-4)."""
    for forbidden in ("open", "high", "low", "raw_open", "raw_high", "raw_low"):
        assert forbidden not in EntryContext.__dataclass_fields__
    ev = evaluate_entry(ctx(), "neutral")
    plan = build_entry_plan(ctx(), ev, "neutral")
    assert plan["placeable"] is True
    assert [t["weight"] for t in plan["tranches"]] == [0.40, 0.30, 0.30]
    assert all(t["provisional"] is True for t in plan["tranches"])
    assert plan["entry_band"]["stop_min_sadj"] < plan["entry_band"]["limit_max_sadj"]
    assert plan["plan_weight"] <= 0.05 + 1e-12
    assert plan["planned_loss_pct"] <= 0.0075 + 1e-12
    assert plan["entry_band"]["limit_max_sadj"] == pytest.approx(120.0 * (1 - 0.20), rel=1e-6)


def test_third_tranche_disabled_outside_stop():
    ev = evaluate_entry(ctx(), "neutral")
    plan = build_entry_plan(ctx(), ev, "neutral")
    tranche3 = plan["tranches"][2]
    assert plan["stop_distance"] == pytest.approx(0.09)
    assert tranche3["active"] is False                      # 91.5×0.86 = 78.7 < 잠정 손절가 83.3
    assert plan["effective_weight"] == pytest.approx(0.05 * 0.70, rel=1e-6)
    assert "3차" in plan["tranche_note_ko"]


def test_watch_grade_places_no_order_unless_preview():
    ev = evaluate_entry(ctx(iv_median=110.0), "neutral")     # B3 실패 → WATCH
    assert ev["grade"] == "WATCH"
    assert build_entry_plan(ctx(iv_median=110.0), ev, "neutral")["placeable"] is False
    assert build_entry_plan(ctx(iv_median=110.0), ev, "neutral",
                            allow_watch=True)["placeable"] is True


def test_order_not_placeable_when_band_inverts():
    """stop_min ≥ limit_max → 1차 진입 불가(사유 기록)."""
    c = ctx(iv_median=95.0, sadj_close=90.0, dma200=95.0, dma200_slope=0.02)
    ev = evaluate_entry(c, "neutral")
    plan = build_entry_plan(c, ev, "neutral", allow_watch=True)
    assert plan["placeable"] is False and plan["reason"] == "outside_order_range"


# ── 매도 트리거 ──────────────────────────────────────────────────────────────
def test_sell_trigger_s1_valuation_overheat():
    ev = evaluate_exit(ctx(iv_median=100.0, sadj_close=125.0), hold(), "neutral")
    assert ev["triggers"]["S1"] is True
    assert ev["verdict"] in ("WATCH_DOWN", "TRIM", "EXIT")


def test_sell_trigger_s2_technical_overheat():
    ev = evaluate_exit(ctx(rsi14=75.0, pct_b=1.05, dma20_sigma=2.5), hold(), "neutral")
    assert ev["triggers"]["S2"] is True


def test_sell_trigger_s3_structural_and_forced_exit():
    ev = evaluate_exit(ctx(structural_risk=75.0), hold(), "neutral")
    assert ev["triggers"]["S3"] is True
    assert ev["verdict"] == "EXIT" and ev["forced"] is True
    assert ev["cancel_pending_tranches"] is True


def test_sell_trigger_s4_trend_break():
    ev = evaluate_exit(ctx(sadj_close=95.0, dma200=100.0, dma200_slope=-0.01, rel_strength_pctile=0.2),
                       hold(), "neutral")
    assert ev["triggers"]["S4"] is True


def test_sell_trigger_s5_stop_hit_forces_exit():
    ev = evaluate_exit(ctx(raw_close=89.0), hold(stop=90.0), "neutral")
    assert ev["triggers"]["S5"] is True and ev["verdict"] == "EXIT"
    ev_low = evaluate_exit(ctx(raw_close=95.0), hold(stop=90.0, raw_low=89.5), "neutral")
    assert ev_low["triggers"]["S5"] is True


def test_hold_when_nothing_fires():
    ev = evaluate_exit(ctx(), hold(stop=80.0), "neutral")
    assert ev["verdict"] == "HOLD" and ev["exit_score"] < 45


def test_suspended_sector_holding_skips_s1_only():
    ev = evaluate_exit(ctx(valuation_suspended=True, iv_median=100.0, sadj_close=125.0), hold(), "neutral")
    assert ev["triggers"]["S1"] is None
    assert "S2" in ev["triggers"] and "S5" in ev["triggers"]
    assert ev["note_ko"] and "S1" in ev["note_ko"]


# ── DoD: 워치리스트 편출 보유 종목도 S1~S5 추적 ──────────────────────────────
def test_holdings_tracked_after_watchlist_removal():
    contexts = {"AAA": ctx(structural_risk=75.0)}
    out = evaluate_holdings([{"ticker": "AAA", **hold(stop=90.0)}], contexts, watchlist=set())
    row = out["holdings"][0]
    assert set(row["triggers"]) == {"S1", "S2", "S3", "S4", "S5"}
    assert row["triggers"]["S3"] is True
    assert row["in_watchlist"] is False
    assert row["badge_ko"] == "워치리스트 외(보유)"
    assert row["new_buy_allowed"] is False
    assert row["sell_card"] is True
    assert out["exit_count"] == 1 and out["tracked_outside_watchlist"] == 1


def test_holdings_in_watchlist_have_no_outside_badge():
    out = evaluate_holdings([{"ticker": "AAA", **hold(stop=80.0)}], {"AAA": ctx()}, watchlist={"AAA"})
    row = out["holdings"][0]
    assert row["in_watchlist"] is True and row["badge_ko"] is None
    assert row["verdict"] == "HOLD"


def test_default_params_match_spec_numbers():
    p = default_params()
    assert p["risk"]["risk_per_trade"] == 0.0075
    assert p["risk"]["max_position"] == 0.05
    assert p["scaling"]["entry"] == [0.40, 0.30, 0.30]
    ev = evaluate_entry(ctx(), "neutral", params=p)
    assert ev["signal"] == "BUY"


# ── 감사 P1-1: 신뢰도 A 요건 · structural_risk 정책 분리 · C 상한 ─────────────────
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

from engine.data import fixtures  # noqa: E402
from engine.decide.pipeline import (  # noqa: E402
    MIN_INDEPENDENT_SOURCES,
    build_payload,
    build_views,
    confidence_grade,
    evidence_items,
    independent_source_count,
    load_params,
    regime_sample_ok,
    verified_quote_items,
)

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 20)
TIMES = {"data_cutoff_at": "2026-11-19T22:00:00Z",
         "signal_published_at": "2026-11-20T02:34:58Z",
         "order_cutoff_at": "2026-11-20T14:25:00Z",
         "target_session": "2026-11-20"}


def _core_evidence(**news) -> list[dict]:
    base = [{"type": "fundamental", "metric": "quality_composite", "value_kind": "score_0_100"},
            {"type": "sec_filing", "form": "10-Q", "role": "reference"},
            {"type": "market", "metric": "atr_pct", "value_kind": "ratio"}]
    if news:
        base.insert(2, {"type": "news", **news})
    return base


def _payload(data: dict | None = None, params: dict | None = None, debate: dict | None = None) -> dict:
    data = data or fixtures.dataset(AS_OF)
    return build_payload(data, as_of=AS_OF, mode="postclose",
                         params=params or load_params(ROOT / "config" / "params_v001.yaml"),
                         params_version="params_v001", code_commit="test", snapshot_id="sha256:test",
                         status="sample", three_times=TIMES, debate=debate)


def test_confidence_grade_signature_is_backward_compatible():
    evidence = _core_evidence(source="Wire", url="https://a/1", quote_verified=True, quote_sha256="x")
    assert confidence_grade(evidence, "read_done") == "B"   # 출처 1곳 → A 아님
    assert confidence_grade(evidence=_core_evidence(), news_state="not_read") == "B"
    assert confidence_grade(_core_evidence(), "read_done", regime_sample={"n": 30}) == "B"


def test_missing_quote_hash_is_not_a_pass():
    """해시 없는 뉴스는 '통과'가 아니라 미검증이다 — A 를 줄 수 없다(감사 P1-1-a)."""
    items = evidence_items("AAA", [{"form": "10-Q", "url": "https://sec/1", "filed_date": "2026-11-01",
                                    "acceptance_datetime": "2026-11-01T20:00:00Z", "accession": "acc-1"}],
                           [{"headline": "h", "source": "Wire", "url": "https://a/1",
                             "published_at": "2026-11-19T20:00:00Z"}],
                           {"metric": "x"}, "read_done", AS_OF.isoformat())
    news = [item for item in items if item["type"] == "news"]
    assert news and news[0]["quote_verified"] is False         # 키 누락이 아니라 명시 False
    assert verified_quote_items(items) == []
    assert confidence_grade(items, "read_done") == "B"


def test_grade_a_needs_verified_quote_two_sources_and_regime_sample():
    a = _core_evidence(source="WireA", url="https://a/1", quote_verified=True, quote_sha256="x")
    b = {"type": "news", "source": "WireB", "url": "https://b/2", "quote_verified": True,
         "quote_sha256": "y"}
    evidence = _core_evidence() + [a[2], b]
    # 표본을 **넘기지 않으면** A 가 아니다(기획서 3.10 "미전달 시 B 상한" — 감사 07-b)
    assert confidence_grade(evidence, "read_done") == "B"
    assert confidence_grade(evidence, "read_done", regime_sample={"n": 30}) == "A"
    # 같은 매체 두 건은 독립 출처 2곳이 아니다
    same_source = [a[2], {**b, "source": "WireA"}]
    assert independent_source_count(same_source) == 1 < MIN_INDEPENDENT_SOURCES
    assert confidence_grade(_core_evidence()[:2] + same_source, "read_done", regime_sample={"n": 30}) == "B"
    # 국면 표본이 모자라면 A 가 아니라 B — `min_n` 을 낮춰도 기획서 하한 30 아래로는 못 내린다
    assert confidence_grade(evidence, "read_done", regime_sample={"n": 3}) == "B"
    assert confidence_grade(evidence, "read_done", regime_sample={"n": 12, "min_n": 10}) == "B"
    assert confidence_grade(evidence, "read_done", regime_sample={"n": 29}) == "B"
    assert confidence_grade(evidence, "read_done", regime_sample=4) == "B"


def test_grade_a_requires_fresh_data():
    """데이터가 신선하지 않으면(종가 지연 등) A 를 줄 수 없다 — 최대 B(감사 07-b)."""
    a = _core_evidence(source="WireA", url="https://a/1", quote_verified=True, quote_sha256="x")
    b = {"type": "news", "source": "WireB", "url": "https://b/2", "quote_verified": True,
         "quote_sha256": "y"}
    evidence = _core_evidence() + [a[2], b]
    basis: dict = {}
    assert confidence_grade(evidence, "read_done", regime_sample={"n": 30}, data_fresh=False,
                            grade_basis=basis) == "B"
    assert basis["reason"] == "data_stale" and basis["data_fresh"] is False
    assert confidence_grade(evidence, "read_done", regime_sample={"n": 30}, data_fresh=True) == "A"


def test_grade_a_alt_path_8k_and_fundamentals_without_news_read():
    """뉴스 미판독이어도 8-K·검증된 재무 근거·독립 출처 2곳이면 A(기획서 3.10 대체 경로)."""
    evidence = [
        {"type": "fundamental", "metric": "quality_composite", "accession": "acc-10k",
         "available_at": "2026-11-01T20:00:00Z"},
        {"type": "sec_filing", "form": "8-K", "items": ["2.02"], "accession": "acc-8k",
         "url": "https://sec.gov/8k"},
        {"type": "market", "metric": "atr_pct", "value_kind": "ratio"},
    ]
    filing = {"form": "8-K", "items": ["2.02"]}
    basis: dict = {}
    assert confidence_grade(evidence, "not_read", regime_sample={"n": 30}, alt_8k_evidence=[filing],
                            grade_basis=basis) == "A"
    assert basis["alt_8k"] is True
    # 구조적 악재 8-K(3.01 상장폐지 등)는 대체 근거가 될 수 없다
    assert confidence_grade(evidence, "not_read", regime_sample={"n": 30},
                            alt_8k_evidence=[{"form": "8-K", "items": ["3.01"]}]) == "B"
    # 8-K 가 없으면 대체 경로 자체가 없다
    assert confidence_grade(evidence, "not_read", regime_sample={"n": 30}) == "B"
    # 재무 근거의 접수번호가 없으면(검증 불가) 역시 B
    unverified = [{**evidence[0], "accession": None, "available_at": None}, evidence[1], evidence[2]]
    assert confidence_grade(unverified, "not_read", regime_sample={"n": 30},
                            alt_8k_evidence=[filing]) == "B"
    # 신선도·표본 조건은 대체 경로에도 똑같이 걸린다
    assert confidence_grade(evidence, "not_read", regime_sample={"n": 30}, data_fresh=False,
                            alt_8k_evidence=[filing]) == "B"
    assert confidence_grade(evidence, "not_read", regime_sample=None,
                            alt_8k_evidence=[filing]) == "B"
    # 접수번호가 붙은 10-K 참조도 재무 근거로 인정한다(8-K 자신은 공시 쪽 조건이다)
    mixed = [
        {"type": "fundamental", "metric": "ROIC"},
        {"type": "sec_filing", "form": "10-K", "accession": "acc-10k", "url": "https://sec.gov/10k"},
        {"type": "sec_filing", "form": "8-K", "items": ["2.02"], "accession": "acc-8k",
         "url": "https://sec.gov/8k"},
        {"type": "market", "metric": "atr_pct", "value_kind": "ratio"},
    ]
    assert confidence_grade(mixed, "not_read", regime_sample={"n": 30},
                            alt_8k_evidence=[filing]) == "A"


def test_failed_quote_and_insufficient_evidence_are_c():
    failed = _core_evidence(source="Wire", url="https://a/1", quote_verified=False, quote_sha256="x")
    assert confidence_grade(failed, "read_done") == "C"
    ok = _core_evidence(source="Wire", url="https://a/1", quote_verified=True, quote_sha256="x")
    assert confidence_grade(ok, "insufficient_evidence") == "C"
    assert confidence_grade(ok, "not_read") == "B"


def test_regime_sample_helper_caps_a_without_sample():
    """미전달(`None`)은 **통과가 아니라 미충족**이다 — 기획서 3.10 "미전달 시 B 상한"(감사 07-b)."""
    assert regime_sample_ok(None) == (False, "regime_sample_missing")
    assert regime_sample_ok(3)[0] is False
    assert regime_sample_ok({"count": 20})[0] is False      # 30 미만
    assert regime_sample_ok({"count": 30}) == (True, None)
    assert regime_sample_ok("nonsense")[0] is False


def _linked(ticker: str, **fields) -> dict:
    """오늘 스냅샷에 묶인 토론 행(`signal_id`·`snapshot_id` 포함) — 편입 계약(4.8.1)을 만족한다."""
    row = {"ticker": ticker, "signal_id": f"{AS_OF.isoformat()}-{ticker}-BUY-001",
           "snapshot_id": "sha256:test", "eligible": True}
    row.update(fields)
    return row


def test_debate_structural_risk_does_not_change_quant_grade_when_auto_downgrade_off():
    """P1-1-b: 자동 하향이 꺼져 있으면 토론 리스크는 기록·배지만, 정량 등급은 그대로."""
    debate = {"FIXA": _linked("FIXA", structural_risk=58.0)}
    baseline = next(r for r in _payload()["signals"] if r["ticker"] == "FIXA")
    record = next(r for r in _payload(debate=debate)["signals"] if r["ticker"] == "FIXA")
    assert baseline["trade_grade"] == "BUY" and record["trade_grade"] == "BUY"
    assert record["decision"] == "BUY" and record["quant_intent"] == "pending_buy"
    assert record["debate_structural_risk"] == 58.0
    assert record["debate_risk_gate_applied"] is False
    assert "B5" not in (record["gate_summary"]["failed"] or [])


def test_debate_structural_risk_applies_to_gate_when_auto_downgrade_on():
    params = load_params(ROOT / "config" / "params_v001.yaml")
    params["debate"]["auto_downgrade_enabled"] = True
    debate = {"FIXA": _linked("FIXA", structural_risk=58.0)}
    record = next(r for r in _payload(params=params, debate=debate)["signals"] if r["ticker"] == "FIXA")
    assert record["decision"] == "WATCH" and record["quant_intent"] == "no_action"
    assert record["debate_risk_gate_applied"] is True
    assert "B5" in (record["gate_summary"]["failed"] or [])


def test_c_grade_with_insufficient_evidence_caps_watch_and_intent():
    """C + 뉴스 근거 부족은 발행 등급·의도 모두 WATCH 상한이다(P1-1)."""
    payload = _payload()
    capped = [r for r in payload["signals"] if r["news_state"] == "insufficient_evidence"]
    assert capped, "픽스처에 근거 부족 종목이 있어야 이 계약을 검증할 수 있다"
    for record in capped:
        assert record["grade"] == "C"
        assert record["decision"] == "WATCH"
        assert record["quant_intent"] == "no_action"
        assert record["confidence_cap"] == "WATCH"
    # 근거 부족이 아닌 종목에는 상한 표시가 붙지 않는다
    assert all(r["confidence_cap"] is None for r in payload["signals"]
               if r["news_state"] != "insufficient_evidence")


# ── 중요 07: 스냅샷·신호로 묶인 토론만 판단에 붙는다 · 신선도·표본·8-K 대체 경로 ──────────────
from engine.decide.pipeline import debate_row_for  # noqa: E402


def test_debate_row_for_requires_matching_snapshot():
    today = "sha256:today"
    row = {"ticker": "FIXA", "snapshot_id": "sha256:old", "news_state": "insufficient_evidence",
           "signal_id": "2026-11-19-FIXA-BUY-001", "eligible": True}
    assert debate_row_for({"FIXA": row}, ticker="FIXA", as_of=AS_OF, snapshot_id=today) == {}
    ok = {**row, "snapshot_id": today, "signal_id": f"{AS_OF.isoformat()}-FIXA-BUY-001"}
    assert debate_row_for({"FIXA": ok}, ticker="FIXA", as_of=AS_OF, snapshot_id=today) == ok
    # `(signal_id, snapshot_id)` 복합 키도 받는다 — 행에 식별자가 없어도 키가 채워진다
    bare = {"news_state": "insufficient_evidence", "eligible": True}
    got = debate_row_for({(ok["signal_id"], today): bare}, ticker="FIXA", as_of=AS_OF, snapshot_id=today)
    assert got["snapshot_id"] == today and got["signal_id"] == ok["signal_id"]
    # 다른 종목·다른 회차(어제)·자격 미달 행은 붙지 않는다
    assert debate_row_for({"FIXA": {**ok, "signal_id": "2026-11-19-FIXA-BUY-001"}},
                          ticker="FIXA", as_of=AS_OF, snapshot_id=today) == {}
    assert debate_row_for({"FIXA": {**ok, "eligible": False}},
                          ticker="FIXA", as_of=AS_OF, snapshot_id=today) == {}
    # 현재 스냅샷을 모르는 호출(픽스처·구 경로)은 비교 기준이 없으므로 그대로 쓴다
    legacy = {"ticker": "FIXA", "news_state": "read_done"}
    assert debate_row_for({"FIXA": legacy}, ticker="FIXA", as_of=AS_OF) == legacy
    # 행 목록으로 와도 같은 규칙으로 고른다
    rows = [{**ok, "ticker": "OTHER"}, ok]
    assert debate_row_for(rows, ticker="FIXA", as_of=AS_OF, snapshot_id=today) == ok


def test_old_snapshot_debate_cannot_change_today_views():
    """구 스냅샷 리포트가 오늘 정량 상태(news_state)를 바꾸지 못한다(감사 07-a)."""
    data = fixtures.dataset(AS_OF)
    today = "sha256:today"
    base = {v.ticker: v for v in build_views(data, AS_OF, snapshot_id=today)}
    old = {"FIXB": {"ticker": "FIXB", "snapshot_id": "sha256:old",
                    "signal_id": "2026-11-19-FIXB-WATCH-001", "eligible": True,
                    "news_state": "insufficient_evidence", "structural_risk": 72.0}}
    stale = {v.ticker: v for v in build_views(data, AS_OF, old, snapshot_id=today)}
    assert base["FIXB"].news_state == "not_read"
    assert stale["FIXB"].news_state == "not_read" and stale["FIXB"].debate == {}

    fresh = {"FIXB": {"ticker": "FIXB", "snapshot_id": today,
                      "signal_id": f"{AS_OF.isoformat()}-FIXB-WATCH-001", "eligible": True,
                      "news_state": "insufficient_evidence"}}
    linked = {v.ticker: v for v in build_views(data, AS_OF, fresh, snapshot_id=today)}
    assert linked["FIXB"].news_state == "insufficient_evidence"
    assert linked["FIXB"].news_state_base == "not_read"
    assert linked["FIXB"].debate["snapshot_id"] == today


def test_old_snapshot_debate_cannot_flip_today_intent():
    """BUY → (구 스냅샷 리포트의 근거 부족) → WATCH 로 뒤집히던 경로를 막는다."""
    old = {"FIXA": {"ticker": "FIXA", "snapshot_id": "sha256:old",
                    "signal_id": "2026-11-19-FIXA-BUY-001", "eligible": True,
                    "news_state": "insufficient_evidence"}}
    record = next(r for r in _payload(debate=old)["signals"] if r["ticker"] == "FIXA")
    assert record["decision"] == "BUY" and record["quant_intent"] == "pending_buy"
    assert record["news_state"] == "read_done"
    assert record["debate_ref"] is None and record["debate_advice"] is None

    matched = {"FIXA": _linked("FIXA", news_state="insufficient_evidence",
                               recommendation="WATCH", structural_risk=40.0, divergent=True)}
    record = next(r for r in _payload(debate=matched)["signals"] if r["ticker"] == "FIXA")
    assert record["news_state"] == "insufficient_evidence"
    assert record["decision"] == "WATCH" and record["quant_intent"] == "no_action"
    assert record["debate_ref"]["snapshot_id"] == "sha256:test"
    assert record["debate_ref"]["signal_id"] == f"{AS_OF.isoformat()}-FIXA-BUY-001"
    # 토론 **권고**는 따로 실린다 — 정량 판단 필드와 섞이지 않는다
    assert record["debate_advice"]["recommendation"] == "WATCH"
    assert record["debate_advice"]["divergent"] is True
    assert record["debate_structural_risk"] == 40.0
    assert record["debate_risk_gate_applied"] is False


def test_quant_grade_records_why_it_is_not_a():
    """신선도·국면 표본을 넘기지 않으면 A 가 아니다 — 레코드에 판정 근거가 남는다(감사 07-b·c)."""
    payload = _payload()
    assert payload["signals"], "픽스처에 신호가 있어야 등급 계약을 검증할 수 있다"
    for record in payload["signals"]:
        assert record["grade"] in ("B", "C")
        detail = record["confidence_detail"]
        assert detail["regime_sample_ok"] is False      # 표본 미전달 → A 상한
        assert detail["data_fresh"] is True
        if record["grade"] == "B":
            assert detail["reason"] == "regime_sample_missing"
        else:                                            # C 는 뉴스 근거 부족이 먼저 걸린다
            assert detail["reason"] in ("insufficient_evidence", "quote_unverified")


def test_regime_sample_from_dataset_can_reach_grade_a():
    """데이터셋이 국면 표본을 주면 A 가 가능해진다 — 배선이 살아 있음을 고정한다."""
    data = fixtures.dataset(AS_OF)
    data = {**data, "regime_sample": {"n": 30}}
    view = {v.ticker: v for v in build_views(data, AS_OF)}["FIXA"]
    evidence = [
        {"type": "fundamental", "metric": "quality_composite", "value_kind": "score_0_100"},
        {"type": "sec_filing", "form": "10-Q"},
        {"type": "news", "source": "WireA", "url": "https://a/1", "quote_verified": True,
         "quote_sha256": "x"},
        {"type": "news", "source": "WireB", "url": "https://b/2", "quote_verified": True,
         "quote_sha256": "y"},
    ]
    assert view.regime_sample == {"n": 30}
    assert confidence_grade(evidence, "read_done", regime_sample=view.regime_sample) == "A"


def test_debate_advice_never_changes_entry_gates():
    """토론 권고는 결과에 실리기만 한다 — 게이트·등급을 바꾸는 것은 정량 입력뿐이다(감사 07)."""
    from engine.decide.signals import DEBATE_ADVICE_FIELDS, QUANT_DECISION_FIELDS

    base = evaluate_entry(ctx(), "neutral")
    advised = evaluate_entry(ctx(debate_advice={"recommendation": "WATCH", "structural_risk": 99.0,
                                                "divergent": True}), "neutral")
    assert base["signal"] == advised["signal"] == "BUY"
    assert base["grade"] == advised["grade"]
    assert base["failed_gates"] == advised["failed_gates"]
    assert advised["debate_advice"]["recommendation"] == "WATCH"
    assert set(DEBATE_ADVICE_FIELDS) == {"debate_advice"}
    assert "debate_advice" not in QUANT_DECISION_FIELDS
    # 구조적 리스크(B5)는 **정량 값**으로만 판정된다
    assert evaluate_entry(ctx(structural_risk=90.0), "neutral")["gates"]["B5"] is False
