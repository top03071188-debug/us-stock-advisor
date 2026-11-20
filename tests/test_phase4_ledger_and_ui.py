"""test_phase4_ledger_and_ui — Phase 4 트랙 2 (토론 편입·A/B 채점·실시간 2·3차 분할·알림·대시보드).

검사하는 계약 (기획서 3.5 · 3.11.8 · 4.8 · 4.12 · 5.3~5.7)
- 토론 편입 4조건: 발행 전 완료는 `invalid`, 주문 마감 후 완료는 `late`, 스냅샷·signal_id 불일치 제외
- 편입해도 `records.jsonl` 은 **바이트 그대로**(사후 편집 금지)
- 행동 분리: 권고 < 정량 → `buy_filled` vs `hold_cash` 페어 / 권고 ≥ 정량 → 행동 동일(승격 금지)
- A/B 수학: 회피 손실·놓친 수익·순효과·회피 정확도·`N_eff`·판정
- `auto_downgrade_enabled`: 기본 false, 30건·`N_eff` 15·+0.20%p 2분기 연속·방어력 개선 4조건
- 실시간 정산: 2차 분할 체결·3차 손절선 밖 무효·S5 잔여 주문 취소·재진입 차단 `health.json`
- 뉴스 상태 연동(토론 → 신뢰도 C 상한·등급 WATCH)과 **신뢰도 승격 금지**
- 알림 중복 0건 / 공개 빌드 금지 키 0건 / 토론 0건에서도 화면 데이터 정상
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from engine.data.prices import build_price_frame
from engine.decide.pipeline import _debate_news_state, build_views, worse_confidence
from engine.ledger import debate as debate_mod
from engine.ledger import live
from engine.ledger.record import execution_event, publish_record
from scripts import build_public_data, notify

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-20"
PUBLISHED = "2026-05-08T22:02:11Z"      # 금요일 발행 → 대상 세션 2026-05-11(월)
COMPLETED = "2026-05-08T23:15:00Z"
ORDER_CUTOFF = "2026-05-11T14:25:00Z"
SNAP = "sha256:abc123"


# ────────────────────────────── 헬퍼 ──────────────────────────────
def panel_of(prices: list[float], start: str = "2026-05-11", ticker: str = "AAA"):
    idx = pd.bdate_range(start=start, periods=len(prices))
    close = pd.Series(prices, index=idx, dtype=float)
    frame = pd.DataFrame({"Open": close, "High": close * 1.005, "Low": close * 0.995,
                          "Close": close, "Adj Close": close, "Volume": 1_000_000.0}, index=idx)
    return build_price_frame(ticker, frame, None)


def record_of(sid: str = "2026-11-18-AAA-BUY-001", *, ticker: str = "AAA", grade: str = "BUY",
              intent: str = "pending_buy", snapshot: str = SNAP, veto: list | None = None) -> dict:
    record = publish_record(
        signal_id=sid, ticker=ticker, type="BUY" if grade in ("BUY", "STRONG_BUY") else "WATCH",
        grade="B", entry_score=72.0, as_of="2026-05-08", data_cutoff_at="2026-05-08T22:00:00Z",
        signal_published_at=PUBLISHED, order_cutoff_at=ORDER_CUTOFF, target_session="2026-05-11",
        generated_price=100.0, target_band={"lo": 110.0, "hi": 130.0}, expected_mdd=-0.12,
        plan_weight=0.05, stop_distance=0.14, quant_intent=intent, snapshot_id=snapshot,
    )
    record["trade_grade"] = grade
    record["veto"] = veto or []
    return record


def report_of(sid: str = "2026-11-18-AAA-BUY-001", *, ticker: str = "AAA", snapshot: str = SNAP,
              completed: str = COMPLETED, published: str = PUBLISHED,
              order: str = ORDER_CUTOFF, status: str = "complete", recommendation: str = "WATCH",
              intent: str = "pending_hold_cash", vetoes: list | None = None,
              news_read: bool = True) -> dict:
    return {
        "report_id": f"{sid}-debate-01", "ticker": ticker, "signal_id": sid, "snapshot_id": snapshot,
        "as_of": "2026-05-08", "data_cutoff_at": "2026-05-08T22:00:00Z",
        "signal_published_at": published, "order_cutoff_at": order, "completed_at": completed,
        "late": False, "status": status, "intent": intent,
        "round1": [
            {"skill": "buffett", "score": 62, "stance": "buy", "confidence": 0.71,
             "reasons": ["IV 대비 21% 할인"], "news_read": ([{"news_id": "n1", "quote_sha256": "ab12",
                                                              "quote_verified": True}] if news_read else [])},
            {"skill": "lynch", "score": -18, "stance": "sell", "confidence": 0.55,
             "reasons": ["PEG 1.42"]},
            {"skill": "druckenmiller", "score": 35, "stance": "hold", "confidence": 0.60,
             "reasons": ["레짐 Neutral"]},
        ],
        "round2": [{"skill": "buffett", "score": 48, "changed": True,
                    "rebuttals": [{"target_skill": "lynch", "point": "PEG 는 결과다"}]}],
        "round3": {
            "guard": {"veto_codes": vetoes or [], "additional_blocks": [], "structural_risk": 58,
                      "reasoning": "코드 Veto 없음"},
            "chair": {"consensus": ["가격은 매력적"], "dissent": ["린치: GARP 미충족"],
                      "grade_recommendation": recommendation, "entry_plan_refs": [93.0],
                      "exit_refs": [130.0], "conflict": True,
                      "one_line_ko": "성장 둔화가 구조적이므로 2차 분할가까지 기다린다"},
        },
        "validation": {"skills_in": 3, "skills_kept": 3, "quotes_checked": 1, "quotes_dropped": 0,
                       "numbers_checked": 4, "numbers_rejected": 0},
        "quant_grade": "BUY", "display_grade": "BUY", "auto_downgrade_applied": False,
        "track": "codex_debated",
    }


def fill_of(record: dict, *, price: float = 100.0, weight: float | None = None) -> dict:
    weight = weight if weight is not None else float(record["position"]["tranche_weight"])
    return execution_event(signal_id=record["signal_id"], snapshot_id=record["snapshot_id"],
                           at=datetime(2026, 5, 11, 13, 30, tzinfo=timezone.utc),
                           execution_event="buy_filled", tranche=1, fill_price=price,
                           weight=weight, stop_price=price * (1 - record["position"]["stop_distance"]))


def synthetic_comparison(*, pairs: int, n_eff: int, net: float, defense: bool,
                         quarters: int = 2, per_quarter: int = 30) -> dict:
    rows = [{"quarter": f"2026Q{n + 1}", "n_graded": per_quarter, "n_eff": max(n_eff, per_quarter),
             "net_after_cost": net, "drawdown": {"defense_improved": defense},
             "verdict": debate_mod.VERDICT_EFFECTIVE}
            for n in range(quarters)]
    return {"schema_version": 1, "n_graded_pairs": pairs, "n_eff": n_eff, "net_after_cost": net,
            "net_after_cost_pp": round(net * 100, 4),
            "drawdown": {"defense_improved": defense}, "quarters": rows,
            "auto_downgrade_enabled": False, "verdict": debate_mod.VERDICT_EFFECTIVE}


# ── 1. 편입 4조건 ─────────────────────────────────────────────────────────────
def test_eligible_when_all_four_conditions_hold():
    el = debate_mod.eligibility(report_of(), record_of())
    assert el["eligible"] is True and el["late"] is False and el["invalid"] is False
    assert debate_mod.is_eligible_for_signal(report_of(), record_of()) is True
    assert debate_mod.is_eligible_for_signal(report_of(completed="2026-05-11T15:00:00Z"),
                                             record_of()) is False


def test_completed_before_publish_is_invalid_and_not_eligible():
    report = report_of(completed="2026-05-08T21:00:00Z")   # 발행(22:02Z) 전에 끝난 토론
    el = debate_mod.eligibility(report, record_of())
    assert el["invalid"] is True and el["eligible"] is False
    assert "COMPLETED_BEFORE_PUBLISHED" in el["reasons"]


def test_completed_after_order_cutoff_is_late_and_not_eligible():
    report = report_of(completed="2026-05-11T15:00:00Z")   # 주문 마감(14:25Z) 뒤
    el = debate_mod.eligibility(report, record_of())
    assert el["late"] is True and el["eligible"] is False
    assert "COMPLETED_AFTER_ORDER_CUTOFF" in el["reasons"]
    assert debate_mod.debate_badge(report, el) == debate_mod.BADGE_LATE


@pytest.mark.parametrize("kwargs, reason", [
    ({"snapshot": "sha256:other"}, "SNAPSHOT_MISMATCH"),
    ({"status": "quorum_failed"}, "STATUS_NOT_COMPLETE"),
])
def test_snapshot_or_status_mismatch_is_not_eligible(kwargs, reason):
    report = report_of(**kwargs)
    el = debate_mod.eligibility(report, record_of())
    assert el["eligible"] is False and reason in el["reasons"]
    if kwargs.get("status"):
        assert debate_mod.debate_badge(report, el) == debate_mod.BADGE_QUORUM


def test_signal_id_mismatch_is_not_eligible():
    report = report_of(sid="2026-11-18-AAA-BUY-999")
    report["signal_id"] = "2026-11-18-AAA-BUY-999"
    el = debate_mod.eligibility(report, record_of())
    assert el["eligible"] is False and "SIGNAL_MISMATCH" in el["reasons"]


# ── 2. 편입해도 records.jsonl 은 그대로 ────────────────────────────────────────
def test_reconcile_never_touches_records_file(tmp_path):
    records = [record_of(), record_of(sid="2026-11-18-BBB-BUY-001", ticker="BBB")]
    path = tmp_path / "records.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records) + "\n",
                    encoding="utf-8")
    before = path.read_bytes()
    doc = {"reports": [report_of(),
                       report_of(sid="2026-11-18-BBB-BUY-001", ticker="BBB",
                                 completed="2026-05-11T15:00:00Z")]}
    result = debate_mod.reconcile_debates(doc, records, {}, event_dir=tmp_path / "events",
                                          params={"debate": {"auto_downgrade_enabled": False}},
                                          as_of=date(2026, 11, 20))
    assert path.read_bytes() == before, "편입이 발행 레코드를 고쳤다(사후 편집 금지)"
    assert result["n_eligible"] == 1 and result["n_late"] == 1
    files = sorted(p.name for p in (tmp_path / "events").glob("*.json"))
    assert files == ["2026-11-18-AAA-BUY-001.json"], "자격 미달(지연) 리포트가 이벤트로 남았다"
    event = json.loads((tmp_path / "events" / files[0]).read_text(encoding="utf-8"))
    assert event["snapshot_id"] == SNAP
    assert event["events"][0]["divergent"] is True


def test_reconcile_is_idempotent(tmp_path):
    records = [record_of()]
    doc = {"reports": [report_of()]}
    for _ in range(2):
        debate_mod.reconcile_debates(doc, records, {}, event_dir=tmp_path / "events",
                                     params={"debate": {"auto_downgrade_enabled": False}})
    payload = json.loads((tmp_path / "events" / "2026-11-18-AAA-BUY-001.json").read_text(encoding="utf-8"))
    assert len(payload["events"]) == 1


# ── 3. 행동 분리(A/B) ─────────────────────────────────────────────────────────
def test_lower_recommendation_splits_actions():
    record = record_of()
    actions = debate_mod.actions_for(report_of(recommendation="WATCH"), record, fill_of(record))
    assert actions["quant_action"] == "buy_filled"
    assert actions["debate_action"] == "hold_cash"
    assert actions["divergent"] is True
    assert debate_mod.debate_badge(report_of(recommendation="WATCH"),
                                   {"eligible": True}, actions) == debate_mod.BADGE_DIFFERENT


def test_same_recommendation_keeps_actions_equal():
    record = record_of()
    actions = debate_mod.actions_for(report_of(recommendation="BUY"), record, fill_of(record))
    assert actions["debate_action"] == actions["quant_action"]
    assert actions["divergent"] is False


def test_higher_recommendation_never_promotes():
    record = record_of(grade="WATCH", intent="no_action")
    actions = debate_mod.actions_for(report_of(recommendation="STRONG_BUY"), record, None)
    assert actions["divergent"] is False
    assert actions["quant_action"] == "no_action"
    assert debate_mod.display_grade(actions) == "WATCH"      # 표시 등급도 올라가지 않는다


def test_veto_blocks_both_tracks():
    blocked = record_of(grade="WATCH", intent="no_action", veto=[{"code": "V03", "blocking": True}])
    actions = debate_mod.actions_for(report_of(vetoes=["V03"]), blocked, None)
    assert actions["quant_action"] == "no_action" and actions["debate_action"] == "no_action"
    assert actions["divergent"] is False


def test_auto_downgrade_on_makes_quant_track_hold_cash():
    record = record_of()
    actions = debate_mod.actions_for(report_of(), record, fill_of(record),
                                     auto_downgrade_enabled=True)
    assert actions["quant_action"] == "hold_cash"
    assert actions["auto_downgrade_applied"] is True
    assert actions["divergent"] is False           # 켜면 A/B 는 끝난다
    assert debate_mod.display_grade(actions) == "WATCH"


# ── 4. A/B 비교 수학 ──────────────────────────────────────────────────────────
def _path(last: float) -> list[float]:
    """63거래일 만기 봉이 마지막 값이 되도록 만든 경로(진입 봉 포함 64봉)."""
    return [100.0] * 63 + [last] + [last] * 6


def _comparison_fixture(tmp_path) -> dict:
    loss10 = _path(90.0)                 # −10%
    loss20 = _path(80.0)                 # −20%
    gain10 = _path(110.0)                # +10%
    panels = {"AAA": panel_of(loss10, ticker="AAA"), "BBB": panel_of(loss20, ticker="BBB"),
              "CCC": panel_of(gain10, ticker="CCC")}
    records, executions, by_signal = [], {}, {}
    for ticker, name in (("AAA", "L1"), ("BBB", "L2"), ("CCC", "G1")):
        sid = f"2026-05-11-{ticker}-BUY-001"
        record = record_of(sid=sid, ticker=ticker)
        execution = fill_of(record)
        records.append(record)
        executions[sid] = execution
        el = debate_mod.eligibility(report_of(sid=sid, ticker=ticker), record)
        by_signal[sid] = debate_mod.public_report(
            report_of(sid=sid, ticker=ticker), el,
            debate_mod.actions_for(report_of(sid=sid, ticker=ticker), record, execution))
        assert by_signal[sid]["divergent"] is True
    return debate_mod.compute_debate_comparison(by_signal, records, executions, panels,
                                                date(2026, 9, 30))


def test_comparison_math_for_divergent_pairs():
    comparison = _comparison_fixture(Path())
    assert comparison["divergent_pairs"] == 3
    assert comparison["n_graded_pairs"] == 3
    assert comparison["n_eff"] == 3
    # w = 0.02 (5% × 40%) · 회피 손실 = (0.10 + 0.20) × 0.02 = 0.006
    assert comparison["avoided_loss"] == pytest.approx(0.006, abs=1e-6)
    assert comparison["missed_gain"] == pytest.approx(0.002, abs=1e-6)
    assert comparison["net"] == pytest.approx(0.004, abs=1e-6)
    assert comparison["net_after_cost"] > comparison["net"]        # 아낀 비용이 더해진다
    assert comparison["avoidance_accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    assert comparison["drawdown"]["quant_tail_5pct"] is not None
    assert comparison["drawdown"]["defense_improved"] is True
    # 표본 3건 < 30건 → 판정 보류(작은 표본에 결론을 붙이지 않는다)
    assert comparison["verdict"] == debate_mod.VERDICT_INSUFFICIENT
    assert "표본" in comparison["verdict_ko"]
    assert comparison["by_regime"]["unknown"]["n_graded"] == 3


def test_comparison_counts_same_action_pairs_only():
    record = record_of()
    el = debate_mod.eligibility(report_of(recommendation="BUY"), record)
    view = debate_mod.public_report(report_of(recommendation="BUY"), el,
                                    debate_mod.actions_for(report_of(recommendation="BUY"), record,
                                                           fill_of(record)))
    comparison = debate_mod.compute_debate_comparison({record["signal_id"]: view}, [record],
                                                      {record["signal_id"]: fill_of(record)},
                                                      {"AAA": panel_of([100.0] * 70)}, date(2026, 9, 30))
    assert comparison["same_action_pairs"] == 1 and comparison["divergent_pairs"] == 0
    assert comparison["note_ko"]


def test_comparison_skips_incomplete_horizon():
    record = record_of()
    el = debate_mod.eligibility(report_of(), record)
    view = debate_mod.public_report(report_of(), el,
                                    debate_mod.actions_for(report_of(), record, fill_of(record)))
    comparison = debate_mod.compute_debate_comparison(
        {record["signal_id"]: view}, [record], {record["signal_id"]: fill_of(record)},
        {"AAA": panel_of([100.0] * 10)}, date(2026, 6, 1))
    assert comparison["divergent_pairs"] == 1
    assert comparison["n_graded_pairs"] == 0            # 63거래일이 지나지 않았다
    assert comparison["verdict"] == debate_mod.VERDICT_INSUFFICIENT


# ── 5. 자동 하향 전환 조건 ────────────────────────────────────────────────────
def test_auto_downgrade_requires_all_four_conditions():
    good = synthetic_comparison(pairs=30, n_eff=15, net=0.004, defense=True, quarters=2)
    verdict = debate_mod.evaluate_auto_downgrade_eligibility(good, [])
    assert verdict["recommend_enable"] is True and verdict["eligible"] is True
    assert verdict["current"] is False                 # 기본값은 false

    small = synthetic_comparison(pairs=29, n_eff=15, net=0.004, defense=True)
    assert debate_mod.evaluate_auto_downgrade_eligibility(small, [])["recommend_enable"] is False

    thin = synthetic_comparison(pairs=30, n_eff=14, net=0.004, defense=True)
    assert debate_mod.evaluate_auto_downgrade_eligibility(thin, [])["recommend_enable"] is False

    weak = synthetic_comparison(pairs=30, n_eff=15, net=0.001, defense=True)
    assert debate_mod.evaluate_auto_downgrade_eligibility(weak, [])["recommend_enable"] is False

    undefended = synthetic_comparison(pairs=30, n_eff=15, net=0.004, defense=False)
    assert debate_mod.evaluate_auto_downgrade_eligibility(undefended, [])["recommend_enable"] is False

    one_quarter = synthetic_comparison(pairs=30, n_eff=15, net=0.004, defense=True, quarters=1)
    result = debate_mod.evaluate_auto_downgrade_eligibility(one_quarter, [])
    assert result["recommend_enable"] is False
    assert result["consecutive_quarters"] == 1


def test_auto_downgrade_evaluation_is_recorded_in_params_history(tmp_path):
    path = tmp_path / "params_history.json"
    path.write_text(json.dumps([{"version": "params_v001", "created_at": "2026-11-19",
                                 "parent_version": None, "gate_result": {"status": "active"},
                                 "changed": [], "note_ko": "초기"}], ensure_ascii=False),
                    encoding="utf-8")
    comparison = synthetic_comparison(pairs=30, n_eff=15, net=0.004, defense=True)
    _, wrote = debate_mod.record_auto_downgrade_evaluation(path, comparison, "params_v001")
    assert wrote is True
    history = json.loads(path.read_text(encoding="utf-8"))
    assert history[-1]["debate"]["recommend_enable"] is True
    assert history[-1]["version"].startswith("params_v001-debate-")
    versions = [h["version"] for h in history]
    assert len(versions) == len(set(versions))
    _, again = debate_mod.record_auto_downgrade_evaluation(path, comparison, "params_v001")
    assert again is False                              # 같은 분기·같은 판정은 다시 쓰지 않는다
    empty = dict(comparison)
    empty["quarters"] = []
    _, wrote_empty = debate_mod.record_auto_downgrade_evaluation(path, empty, "params_v001")
    assert wrote_empty is False                        # 페어 0건이면 이력을 부풀리지 않는다


# ── 6. 실시간 2·3차 분할·손절·분할익절 ────────────────────────────────────────
def _settle(record: dict, prices: list[float], as_of: date, *, params: dict | None = None,
            forced: dict | None = None, execution: dict | None = None) -> dict:
    execution = execution or fill_of(record)
    return live.settle_positions([record], {record["signal_id"]: execution}, {},
                                 {record["ticker"]: panel_of(prices, ticker=record["ticker"])},
                                 as_of, params or {"risk": {"risk_per_trade": 0.0075, "max_position": 0.05}},
                                 forced_exits=forced)


def test_tranche_two_fills_at_limit_after_seven_percent_drop():
    record = record_of()
    prices = [100.0, 99.0, 92.0, 94.0, 96.0] + [96.0] * 10
    result = _settle(record, prices, date(2026, 5, 29))
    tranches = result["positions"]["AAA"]["tranches"]
    assert tranches[0]["status"] == "filled"
    assert tranches[1]["status"] == "filled"           # 1차 체결가 × 0.93 = 93 → 저가 92
    assert tranches[1]["price_ref"] == pytest.approx(93.0, abs=0.01)
    assert result["health"]["tranches_filled"] == 2
    assert any(e.get("event") == "buy_filled" and e.get("tranche") == 2 for e in result["events"])


def test_third_tranche_inside_initial_stop_is_invalid():
    record = record_of()
    record["position"]["stop_distance"] = 0.10          # 초기 손절가 = 90
    prices = [100.0] * 20
    result = _settle(record, prices, date(2026, 6, 5))
    tranches = result["positions"]["AAA"]["tranches"]
    assert tranches[2]["status"] == "invalid"           # 3차 86 ≤ 90 → 무효
    assert "무효" in (tranches[2]["note"] or "")


def test_stop_out_cancels_pending_tranches_and_records_health():
    record = record_of()
    record["position"]["stop_distance"] = 0.05          # 초기 손절가 95
    prices = [100.0, 101.0, 96.0, 90.0, 92.0] + [92.0] * 10
    result = _settle(record, prices, date(2026, 5, 29))
    position = result["positions"]["AAA"]
    assert position["exit"] is not None and position["exit"]["reason"] == "S5"
    cancelled = [r for r in position["tranches"] if r["status"] == "cancelled"]
    assert cancelled, "손절 후 잔여 2·3차 주문이 살아 있다 — 같은 봉 재진입 위험"
    assert all(r["cancelled_by"] == "S5" for r in cancelled)
    assert result["health"]["stop_reentry_blocked_count"] == len(cancelled)
    assert result["health"]["stop_count"] == 1
    # 재진입: 손절 뒤 저가가 2차 지정가(87)까지 내려가도 체결되지 않는다
    assert not any(e.get("event") == "buy_filled" and e.get("tranche", 1) > 1
                   for e in result["events"])


def test_forced_exit_on_as_of_day_cancels_tranches():
    record = record_of()
    prices = [100.0, 100.0, 100.0, 100.0]
    result = _settle(record, prices, date(2026, 5, 14), forced={"AAA": "S3"})
    position = result["positions"]["AAA"]
    assert position["exit"]["reason"] == "S3"
    assert position["tranches"][1]["status"] == "cancelled"
    assert position["tranches"][1]["cancelled_by"] == "S3"


def test_mark_positions_has_no_account_amounts():
    record = record_of()
    result = _settle(record, [100.0, 100.0, 100.0] + [100.0] * 5, date(2026, 5, 22))
    public = live.mark_positions(result, {"AAA": panel_of([100.0] * 8)}, date(2026, 5, 22))
    assert public["cash_pct"] + public["invested_pct"] == pytest.approx(1.0, abs=1e-6)
    keys = set()


    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.add(k)
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(public)
    assert not keys & {"shares", "avg_cost", "cash_balance"}
    position = public["positions"][0]
    assert {"tranches", "stop_distance", "stop_headroom_pct", "trailing", "trim_stage"} <= set(position)
    assert position["tranches"][0]["status"] == "filled"


def test_iv_high_profit_take_sells_one_third():
    record = record_of()
    record["valuation"] = {"iv_high": 104.0}
    prices = [100.0, 101.0, 105.0, 100.0, 100.0] + [100.0] * 5
    result = _settle(record, prices, date(2026, 5, 22))
    trims = [e for e in result["events"] if e.get("event") == "profit_take" and e.get("reason") == "iv_high"]
    assert trims and trims[0]["price"] == pytest.approx(104.0)
    assert result["positions"]["AAA"]["trim_stage"] == "iv_high"
    assert result["health"]["trim_count"] >= 1


# ── 7. 뉴스 상태 연동 · 신뢰도 승격 금지 ──────────────────────────────────────
def test_debate_news_state_is_never_an_upgrade_shortcut():
    assert _debate_news_state("not_read", {"news_state": "insufficient_evidence"}) == "insufficient_evidence"
    assert _debate_news_state("not_read", {"news_state": "read_done"}) == "read_done"
    assert _debate_news_state("read_done", {"news_state": "insufficient_evidence"}) == "insufficient_evidence"
    assert _debate_news_state("insufficient_evidence", {"news_state": "read_done"}) == "insufficient_evidence"
    assert worse_confidence("A", "C") == "C"
    assert worse_confidence("B", "A") == "B"
    assert worse_confidence("A") == "A"


def test_public_report_exposes_pipeline_fields():
    """편입 요약이 파이프라인이 읽는 news_state·structural_risk 를 실제로 담는지(배관 누락 방지)."""
    row = debate_mod.public_report(report_of(), {"eligible": True}, None)
    assert row["structural_risk"] == 58
    assert row["news_state"] == "read_done"
    assert "quote" not in row


def test_build_views_applies_debate_news_state_and_structural_risk():
    from engine.data.fixtures import dataset

    day = date(2026, 11, 20)
    data = dataset(day)
    base = {v.ticker: v for v in build_views(data, day)}
    assert base["FIXB"].news_state == "not_read"
    views = {v.ticker: v for v in build_views(
        data, day, {"FIXB": {"news_state": "insufficient_evidence", "structural_risk": 72.0},
                    "FIXA": {"news_state": "insufficient_evidence"}})}
    assert views["FIXB"].news_state == "insufficient_evidence"
    assert views["FIXB"].news_state_base == "not_read"
    # 판독이 이미 있던 종목은 '판독 완료'로 되돌리지 않는다(내리는 방향만)
    assert views["FIXA"].news_state == "insufficient_evidence"


# ── 8. 알림 중복 차단 ────────────────────────────────────────────────────────
def _notify_payload() -> dict:
    return {
        "as_of": AS_OF, "status": "normal",
        "signals": [{"signal_id": "2026-11-20-AAA-BUY-001", "ticker": "AAA", "decision": "BUY",
                     "trade_grade": "STRONG_BUY", "entry_score": 88.0, "quant_intent": "pending_buy"}],
        "position_checks": [{"ticker": "CCC", "verdict": "EXIT", "exit_score": 81.0,
                             "triggers": {"S5": True}}],
        "blocked": [{"ticker": "DDD", "codes": ["V06"]}],
        "diagnostics": {"degraded": ["fred:timeout"]},
    }


def test_notifications_are_deduplicated(tmp_path):
    log = tmp_path / "log.json"
    out = tmp_path / "latest.json"
    first = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=out,
                                          channels=[notify.ConsoleChannel()])
    kinds = {e["type"] for e in first["new"]}
    assert {"strong_buy", "exit", "v06", "degraded", "debate_recommended"} <= kinds
    second = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=out,
                                           channels=[notify.ConsoleChannel()])
    assert second["n_new"] == 0 and second["n_suppressed"] == len(first["new"])
    assert json.loads(out.read_text(encoding="utf-8"))["n_new"] == 0


def test_debate_recommendation_is_skipped_when_report_is_eligible(tmp_path):
    doc = {"by_signal": {"2026-11-20-AAA-BUY-001": {"signal_id": "2026-11-20-AAA-BUY-001",
                                                    "ticker": "AAA", "eligible": True}}}
    summary = notify.evaluate_notifications(_notify_payload(), log_path=tmp_path / "log.json",
                                            out_path=tmp_path / "out.json", debate=doc,
                                            channels=[notify.ConsoleChannel()])
    assert "debate_recommended" not in {e["type"] for e in summary["new"]}


# ── 9. 공개 빌드 · 금지 키 · 토론 0건 ────────────────────────────────────────
def _rules() -> dict:
    with open(ROOT / "config" / "public_allowlist.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build(out_dir: Path, *, reports: dict | None = None, records: list[dict] | None = None) -> dict:
    rules = _rules()
    signals = json.loads((ROOT / "data" / "quant_signals.json").read_text(encoding="utf-8"))
    return build_public_data.build(
        signals, json.loads((ROOT / "data" / "walkforward" / "folds" / "summary.json")
                            .read_text(encoding="utf-8")), records or [], rules,
        radar=json.loads((ROOT / "data" / "radar" / "13f.json").read_text(encoding="utf-8")),
        portfolio_state=json.loads((ROOT / "data" / "ledger" / "portfolio" / "state.json")
                                   .read_text(encoding="utf-8")),
        watch=json.loads((ROOT / "data" / "watchlist" / "current.json").read_text(encoding="utf-8")),
        params=yaml.safe_load((ROOT / "config" / "params_v001.yaml").read_text(encoding="utf-8")),
        debate_reports=reports)


def test_public_build_without_any_debate():
    out = _build(Path("/tmp"))
    assert out["debate.json"]["status"] == "none"
    assert out["debate.json"]["reports"] == []
    assert out["debate.json"]["comparison"]["verdict"] == debate_mod.VERDICT_INSUFFICIENT
    assert out["health.json"]["stop_reentry_blocked_count"] == 0
    assert out["notifications.json"]["new"] == []
    for name in ("meta.json", "signals/today.json", "ledger/summary.json", "debate.json",
                 "health.json", "notifications.json"):
        assert name in out
    assert out["meta.json"]["debate_status"] == "none"


def test_public_build_carries_debate_card_and_hides_quotes():
    records = [record_of()]
    reports = {"reports": [report_of()]}
    out = _build(Path("/tmp"), reports=reports, records=records)
    assert out["debate.json"]["status"] == "ok"
    report = out["debate.json"]["reports"][0]
    assert report["badge"] == debate_mod.BADGE_DIFFERENT
    assert report["round1"] and report["round3"]["chair"]["one_line_ko"]
    assert report["validation"]["quotes_dropped"] == 0
    text = json.dumps(out, ensure_ascii=False)
    assert '"quote"' not in text and "body_excerpt_1500" not in text
    rules = _rules()
    for name, payload in out.items():
        build_public_data.assert_no_forbidden(payload, rules, name)
    ticker = out.get("tickers/AAA.json")
    if ticker is not None:
        assert ticker["debate"] is not None
        assert ticker["debate_display"]["badge"] == debate_mod.BADGE_DIFFERENT


def test_public_files_pass_commit_scope_guard(tmp_path):
    from scripts import check_commit_scope

    out = _build(tmp_path)
    rules = _rules()
    for name, payload in out.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        path.write_text(text, encoding="utf-8")
        problems = check_commit_scope.check_text(path, text, rules)
        assert problems == [], f"{name}: {problems}"


# ── 10. run_daily 전체 경로(토론 0건 → 토론 1건) ──────────────────────────────
@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    for name in ("config", "schemas"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "redact_quotes.py", tmp_path / "scripts" / "redact_quotes.py")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(monkeypatch, as_of: str) -> int:
    from scripts import run_daily
    monkeypatch.setenv("GITHUB_OUTPUT", str(Path("gh.txt").resolve()))
    return run_daily.main(["--mode", "postclose", "--offline", "--as-of", as_of, "--root", "."])


def test_run_daily_end_to_end_with_debate(workdir, monkeypatch):
    from scripts import run_daily  # noqa: F401 — 경로 등록

    assert _run(monkeypatch, AS_OF) == 0
    records = [json.loads(line) for line in
               Path("data/ledger/records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    buy = next(r for r in records if r["quant_intent"] == "pending_buy")
    # 다음 장: 1차 체결 → 2·3차 분할 상태가 공개 계좌에 실린다
    assert _run(monkeypatch, "2026-11-24") == 0
    state = json.loads(Path("data/ledger/portfolio/state.json").read_text(encoding="utf-8"))
    assert Path("data/ledger/portfolio/latest.json").exists()
    assert state["positions"], "체결된 포지션이 공개 계좌에 없다"
    assert state["positions"][0]["tranches"][0]["status"] == "filled"
    assert Path("data/health.json").exists()
    health = json.loads(Path("data/health.json").read_text(encoding="utf-8"))
    assert "stop_reentry_blocked_count" in health

    # 편입 자격을 만족하는 토론 리포트를 넣고 다시 돌린다
    #   토론 창은 **신호 발행 직후 ~ 주문 마감** 이다(3.11.8) — 발행 1시간 뒤에 끝난 것으로 둔다.
    published = datetime.fromisoformat(str(buy["signal_published_at"]).replace("Z", "+00:00"))
    completed = (published + pd.Timedelta(minutes=60)).isoformat().replace("+00:00", "Z")
    report = report_of(sid=buy["signal_id"], ticker=buy["ticker"],
                       published=buy["signal_published_at"], order=buy["order_cutoff_at"],
                       completed=completed, snapshot=buy["snapshot_id"])
    Path("data/debate_reports.json").write_text(
        json.dumps({"schema_version": "1.0", "reports": [report]}, ensure_ascii=False), encoding="utf-8")
    records_before = Path("data/ledger/records.jsonl").read_bytes()
    assert _run(monkeypatch, "2026-11-24") == 0
    assert Path("data/ledger/records.jsonl").read_bytes() == records_before
    event_path = Path("data/ledger/debate_events") / f"{buy['signal_id']}.json"
    assert event_path.exists(), "편입 자격을 통과한 토론이 이벤트로 남지 않았다"
    comparison = json.loads(Path("data/ledger/debate_comparison.json").read_text(encoding="utf-8"))
    assert comparison["divergent_pairs"] >= 0
    assert "net_after_cost" in comparison
    signals = json.loads(Path("data/quant_signals.json").read_text(encoding="utf-8"))
    assert signals["debate"]["counts"]["eligible"] >= 1
    assert signals["debate"]["divergent_pairs"] >= 1
    assert Path("data/notifications_latest.json").exists()
    notifications = json.loads(Path("data/notifications_latest.json").read_text(encoding="utf-8"))
    assert notifications["n_new"] >= 0


# ── 11. 화면 배선 정적 점검(JS 런타임 없이) ──────────────────────────────────
def test_ui_wiring_ids_manifest_and_cache_bust():
    import re

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'\$\("([^"]+)"\)', js))
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert missing == [], f"app.js 가 찾는 id 가 index.html 에 없다: {missing}"
    assert ids & {"notify-banner", "debate-effect", "live-positions"}, "Phase 4 위젯 자리가 없다"
    assert 'rel="manifest" href="./manifest.json"' in html
    assert "app.js?v=phase4-1" in html and "styles.css?v=phase4-1" in html
    for name in ("debate.json", "health.json", "notifications.json"):
        assert f'"{name}"' in js, f"app.js 가 {name} 을 읽지 않는다"
    manifest = json.loads((ROOT / "web" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone" and manifest["start_url"]
    assert manifest["icons"] and manifest["icons"][0]["src"].endswith("icon.svg")


def test_runbook_documents_switch_and_badges():
    text = (ROOT / "docs" / "RUNBOOK_PHASE4.md").read_text(encoding="utf-8")
    for needle in ("auto_downgrade_enabled", "PR", "지연 도착", "정족수 미달", "재진입 차단",
                   "0.20%p", "validate_outputs.py"):
        assert needle in text, f"RUNBOOK 에 '{needle}' 절차가 없다"


def test_tranche_two_fills_next_open_when_previous_rsi_is_low():
    """RSI ≤ 25(전일 종가) → 다음 거래일 **시가** 2차 체결. 지정가(0.93)보다 위에서 체결된다."""
    record = record_of()
    prices = [round(130.0 * (0.996 ** t), 2) for t in range(30)]
    execution = fill_of(record, price=130.0)
    result = live.settle_positions(
        [record], {record["signal_id"]: execution}, {},
        {"AAA": panel_of(prices, ticker="AAA")}, date(2026, 6, 19),
        {"risk": {"risk_per_trade": 0.0075, "max_position": 0.05}})
    tranches = result["positions"]["AAA"]["tranches"]
    assert tranches[1]["status"] == "filled"
    fill = next(e for e in result["events"] if e.get("event") == "buy_filled" and e.get("tranche") == 2)
    assert fill["reason"] == "rsi_next_open"
    assert fill["price"] > 120.9, "지정가(1차×0.93)에서 체결됐다 — RSI 경로가 아니라 지정가 경로다"
    assert fill["price"] == pytest.approx(prices[15], abs=0.02)
