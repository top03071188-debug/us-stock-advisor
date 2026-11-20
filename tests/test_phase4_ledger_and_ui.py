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


def panel_ohlc(opens, highs, lows, closes, start: str = "2026-05-11", ticker: str = "AAA",
               splits=None):
    """OHLC 경로를 직접 주는 패널.

    `splits` 는 `build_price_frame` 의 계약 그대로 `{ex_date: ratio}` 다(2:1 분할이면 계수 2→1).
    """
    idx = pd.bdate_range(start=start, periods=len(closes))
    frame = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes,
                          "Adj Close": closes, "Volume": 1_000_000.0}, index=idx)
    return build_price_frame(ticker, frame, splits)


def nyse_panel(prices: dict[date, float], ticker: str = "AAA", start: date | None = None,
               end: date | None = None):
    """거래일 달력 그대로의 패널 — 만기 채점(63거래일)은 영업일이 아니라 NYSE 달력을 씁니다."""
    from engine.utils.dates import trading_days as _td

    days = _td(start or min(prices), end or max(prices))
    close = pd.Series([float(prices[d]) for d in days],
                      index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]), dtype=float)
    frame = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                          "Adj Close": close, "Volume": 1_000_000.0})
    return build_price_frame(ticker, frame, None)


def record_of(sid: str = "2026-11-18-AAA-BUY-001", *, ticker: str = "AAA", grade: str = "BUY",
              intent: str = "pending_buy", snapshot: str = SNAP, veto: list | None = None,
              as_of: str = "2026-05-08", plan_weight: float = 0.05, stop_distance: float = 0.14,
              band: dict | None = None, sector: str = "Tech", price: float = 100.0) -> dict:
    record = publish_record(
        signal_id=sid, ticker=ticker, type="BUY" if grade in ("BUY", "STRONG_BUY") else "WATCH",
        grade="B", entry_score=72.0, as_of=as_of, data_cutoff_at="2026-05-08T22:00:00Z",
        signal_published_at=PUBLISHED, order_cutoff_at=ORDER_CUTOFF, target_session="2026-05-11",
        generated_price=price, target_band={"lo": 110.0, "hi": 130.0}, expected_mdd=-0.12,
        plan_weight=plan_weight, stop_distance=stop_distance, quant_intent=intent,
        snapshot_id=snapshot,
    )
    record["trade_grade"] = grade
    record["veto"] = veto or []
    record["sector"] = sector
    record["entry_band"] = band or {"stop_min_sadj": price * 0.90, "limit_max_sadj": price * 1.05,
                                    "dma200_floor_sadj": price * 0.70,
                                    "provisional_stop_sadj": price * 0.86,
                                    "required_margin": 0.05, "split_factor": 1.0}
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


def test_third_tranche_below_initial_stop_is_armed_for_dma_only():
    """3차 지정가가 초기 손절가 아래면 **지정가 트랙만 무효** — 200일선 트랙으로 살려 둔다(A-7).

    기획서 3.5의 3차는 두 트리거(① 지정가 ② 200일선 −3%)를 가진다. 손절 거리 ≥ 14% 인 종목은
    ①이 손절선 아래가 되는데, 둘 다 끄면 기본 파라미터에서 3차가 **영원히 걸리지 않는다**.
    """
    record = record_of()
    record["position"]["stop_distance"] = 0.10          # 초기 손절가 = 90
    prices = [100.0] * 20
    result = _settle(record, prices, date(2026, 6, 5))
    tranches = result["positions"]["AAA"]["tranches"]
    assert tranches[2]["status"] == "pending"           # 3차 86 ≤ 90 → 지정가 무효, DMA 대기
    assert tranches[2]["trigger"] == "dma200"
    assert "200일선" in (tranches[2]["note"] or "")
    # 지정가 경로로는 체결되지 않는다(저가가 86 아래로 내려가도)
    assert not any(e.get("event") == "buy_filled" and e.get("tranche") == 3 and e.get("reason") == "limit"
                   for e in result["events"])


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
    assert "app.js?v=phase4-2" in html and "styles.css?v=phase4-2" in html
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


# ══════════════════════════════════════════════════════════════════════════════
# 12. 검토 문서 §2~§8.1 회귀 — P0-1 주문 슬롯·동결 / P0-2 시각 계약 / P0-3 공통 모의 체결
#     / P0-4 Paper 채점 분리 / P1-3 독립 A/B 계좌·비용 이중 차감
# ══════════════════════════════════════════════════════════════════════════════
from engine.ledger.debate import eligibility as _eligibility  # noqa: E402
from engine.ledger.pilot import evaluate_pilot  # noqa: E402
from engine.ledger.portfolio import (Bar, BuyOrder, CorporateAction, PortfolioSimulator,  # noqa: E402
                                     TrancheOrder)
from engine.ledger.record import (superseded_ids, superseding_release)  # noqa: E402


# ── 12-1. P0-1 발행 회차·주문 슬롯 ───────────────────────────────────────────
def test_buy_then_watch_same_day_cancels_unfilled_entry():
    """같은 as_of 에서 BUY → WATCH 로 바뀌면 이전 BUY 는 체결되지 않고 **취소 이벤트**가 남는다."""
    buy = record_of(sid="2026-05-08-AAA-BUY-001")
    records = [buy]
    watch = superseding_release(records, ticker="AAA", as_of="2026-05-08", type="WATCH",
                                data_cutoff_at="2026-05-08T22:00:00Z",
                                signal_published_at="2026-05-08T23:40:00Z",
                                order_cutoff_at=ORDER_CUTOFF, target_session="2026-05-11",
                                inputs_hash="hash-watch", grade="WATCH")
    records.append(watch)
    assert watch["supersedes"] == buy["signal_id"], "WATCH 재발행이 이전 BUY 를 대체하지 않았다"
    assert buy["signal_id"] in superseded_ids(records)
    assert watch["quant_intent"] == "no_action" and watch["position"]["plan_weight_target"] == 0.0
    assert watch["release_kind"] == "WATCH"

    out = live.settle(records, {}, {"AAA": panel_of([100.0, 100.0, 100.0])}, date(2026, 5, 11))
    assert not [e for e in out["new_events"] if e["quant_track"]["execution_event"] == "buy_filled"], \
        "최신 판단이 WATCH 인데 BUY 가 체결됐다(P0-1)"
    assert out["cancellations"] and out["cancellations"][0]["cancelled_by"] == "WATCH_OR_BLOCKED"
    assert out["cancellations"][0]["signal_id"] == buy["signal_id"]
    assert any(row.get("event") == "superseded" for row in out["settled"])


def test_buy_then_blocked_same_day_cancels_unfilled_entry():
    """BUY→차단(BLOCKED)도 같은 계보의 회차로 남아 이전 BUY 를 취소한다."""
    buy = record_of(sid="2026-05-08-BBB-BUY-001", ticker="BBB")
    records = [buy]
    blocked = superseding_release(records, ticker="BBB", as_of="2026-05-08", type="BLOCKED",
                                  data_cutoff_at="2026-05-08T22:00:00Z",
                                  signal_published_at="2026-05-08T23:40:00Z",
                                  order_cutoff_at=ORDER_CUTOFF, target_session="2026-05-11",
                                  inputs_hash="hash-blocked", reason_ko="V06 차단",
                                  grade="NONE")
    records.append(blocked)
    assert blocked["supersedes"] == buy["signal_id"]
    assert blocked["thesis_ko"] == "V06 차단"
    out = live.settle(records, {}, {"BBB": panel_of([100.0, 100.0, 100.0], ticker="BBB")},
                      date(2026, 5, 11))
    assert not out["new_events"]
    assert out["cancellations"][0]["signal_id"] == buy["signal_id"]


def test_a_b_a_creates_third_revision_and_fills_only_latest():
    """A→B→A 재실행은 **003** 을 만들고, 001(대체됨)은 체결되지 않는다."""
    from engine.ledger.record import resolve_release

    a1 = record_of(sid="2026-05-08-AAA-BUY-001")
    a1["inputs_hash"] = "hash-A"
    records = [a1]
    rel_b = resolve_release(_with_hash(record_of(sid="2026-05-08-AAA-BUY-001"), "hash-B"), records)
    b2 = _with_hash(record_of(sid=f"2026-05-08-AAA-BUY-{rel_b['revision']:03d}"), "hash-B")
    b2["revision"], b2["supersedes"] = rel_b["revision"], rel_b["supersedes"]
    records.append(b2)
    third = resolve_release(_with_hash(record_of(sid="2026-05-08-AAA-BUY-001"), "hash-A"), records)
    assert third["revision"] == 3 and third["signal_id"].endswith("-003"), third
    assert third["supersedes"] == b2["signal_id"]
    assert third["duplicate"] is False
    assert third["signal_id"] not in superseded_ids(records), "이미 대체된 발행본을 다시 골랐다"


def test_same_run_request_resend_reuses_release():
    """같은 입력이라도 **다른 요청 id** 면 새 회차, 같은 요청 id 재전송이면 원 응답 재사용."""
    from engine.ledger.record import resolve_release

    first = _with_hash(record_of(sid="2026-05-08-AAA-BUY-001"), "hash-A")
    first["run_request_id"] = "req-1"
    same_req = _with_hash(record_of(sid="2026-05-08-AAA-BUY-001"), "hash-A")
    same_req["run_request_id"] = "req-1"
    other_req = _with_hash(record_of(sid="2026-05-08-AAA-BUY-001"), "hash-A")
    other_req["run_request_id"] = "req-2"
    assert resolve_release(same_req, [first])["duplicate"] is True
    other = resolve_release(other_req, [first])
    assert other["duplicate"] is False and other["revision"] == 2


def test_watch_after_freeze_does_not_cancel_frozen_order():
    """동결 마감(주문 마감 5분 전)을 넘겨 도착한 WATCH 는 이미 동결된 주문을 취소하지 않는다."""
    buy = record_of(sid="2026-05-08-AAA-BUY-001")
    records = [buy]
    buy["order_cutoff_at"] = "2026-05-11T13:25:00Z"          # 5월(EDT) 실제 주문 마감
    late_watch = superseding_release(records, ticker="AAA", as_of="2026-05-08", type="WATCH",
                                     data_cutoff_at="2026-05-08T22:00:00Z",
                                     signal_published_at="2026-05-11T13:22:00Z",   # 마감 3분 전 = 동결 뒤
                                     order_cutoff_at="2026-05-11T13:25:00Z", target_session="2026-05-11",
                                     inputs_hash="hash-late", grade="WATCH")
    records.append(late_watch)
    slots = live.order_slots(records)
    slot = slots[("AAA", date(2026, 5, 11))]
    assert slot["freeze_deadline_at"] == "2026-05-11T13:20:00Z"
    assert slot["frozen_at"] <= slot["freeze_deadline_at"]
    out = live.settle(records, {}, {"AAA": panel_of([100.0, 100.0, 100.0])}, date(2026, 5, 11))
    assert any(e["quant_track"]["execution_event"] == "buy_filled" for e in out["new_events"]), \
        "동결 뒤 도착한 WATCH 가 이미 동결된 주문을 취소했다"
    assert not out["cancellations"]
    assert any(row.get("event") == "superseded_after_freeze" for row in out["settled"])


def _with_hash(record: dict, value: str) -> dict:
    record["inputs_hash"] = value
    return record


# ── 12-2. P0-2 시각 계약 ─────────────────────────────────────────────────────
def test_validated_after_cutoff_is_late_even_if_completed_before():
    """완료는 마감 전, 검증은 마감 뒤 → 당일 편입 0건."""
    el = _eligibility(report_of(completed="2026-05-11T14:00:00Z"), record_of(),
                      report_validated_at="2026-05-11T14:26:00Z")
    assert el["late"] is True and el["eligible"] is False
    assert "REPORT_VALIDATED_AFTER_ORDER_CUTOFF" in el["reasons"]
    assert el["report_validated_at"] == "2026-05-11T14:26:00Z"


@pytest.mark.parametrize("completed, ok", [
    ("2026-05-11T14:24:59Z", True),     # 마감 1초 전 완료 → 통과
    ("2026-05-11T14:25:00Z", False),    # 정각 → 마감 도달(late)
    ("2026-05-11T14:25:01Z", False),    # 1초 뒤
])
def test_completed_second_before_at_and_after_cutoff(completed, ok):
    el = _eligibility(report_of(completed=completed), record_of())
    assert el["eligible"] is ok
    assert el["late"] is (not ok)


@pytest.mark.parametrize("validated, ok", [
    ("2026-05-11T14:20:00Z", True),     # 동결 마감(마감 5분 전) 정각 → 통과
    ("2026-05-11T14:20:01Z", False),    # 동결 마감 뒤 → 당일 주문에 못 붙는다
])
def test_validation_within_freeze_deadline(validated, ok):
    el = _eligibility(report_of(completed="2026-05-11T14:00:00Z"), record_of(),
                      report_validated_at=validated)
    assert el["eligible"] is ok
    if not ok:
        assert "VALIDATED_AFTER_FREEZE" in el["reasons"]


def test_validation_time_is_read_from_validation_block():
    """트랙 B는 스키마 때문에 `validation.report_validated_at` 에 쓴다 — 그 값도 검증 시각이다."""
    report = report_of(completed="2026-05-11T14:00:00Z")
    report["validation"] = {**report["validation"], "report_validated_at": "2026-05-11T14:30:00Z"}
    el = _eligibility(report, record_of())
    assert el["late"] is True and el["eligible"] is False
    assert "REPORT_VALIDATED_AFTER_ORDER_CUTOFF" in el["reasons"]


def test_retroactive_pair_is_rejected_when_fill_precedes_validation():
    """이미 시가 체결된 주문에 늦게 검증된 보고서를 붙이면 소급 페어다 — 편입 금지."""
    record = record_of()
    execution = fill_of(record)                       # at = 2026-05-11T13:30Z
    el = _eligibility(report_of(completed="2026-05-11T13:00:00Z"), record,
                      report_validated_at="2026-05-11T13:45:00Z", execution=execution)
    assert el["eligible"] is False and el["invalid"] is True
    assert "ATTACHED_AFTER_FILL" in el["reasons"]
    # 검증이 체결 전이면 통과한다(정상 경로)
    ok = _eligibility(report_of(completed="2026-05-11T13:00:00Z"), record,
                      report_validated_at="2026-05-11T13:10:00Z", execution=execution)
    assert ok["eligible"] is True


def test_now_is_used_as_validation_time_when_report_has_no_timestamp():
    record = record_of()
    late = _eligibility(report_of(completed="2026-05-11T14:00:00Z"), record,
                        now=datetime(2026, 5, 11, 14, 45, tzinfo=timezone.utc))
    assert late["late"] is True and late["report_validated_at"] == "2026-05-11T14:45:00Z"


# ── 12-3. P0-3 공통 모의 체결 ────────────────────────────────────────────────
def _sim_order(ticker: str, *, plan_weight: float = 0.125, sector: str = "Tech",
               iv_high: float | None = None) -> BuyOrder:
    from engine.backtest.execution import EntryBand

    return BuyOrder(signal_id=f"sig-{ticker}", ticker=ticker,
                    committed_at=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
                    atr_pct=0.02, plan_weight=plan_weight, sector=sector, stop_distance=0.10,
                    iv_high=iv_high,
                    band=EntryBand(stop_min=90.0, limit_max=105.0, dma200_floor=70.0,
                                   provisional_stop=86.0, required_margin=0.05),
                    tranches=[TrancheOrder(index=1, weight=0.40, limit_price=None)])


def test_sector_cap_blocks_sixth_five_percent_fill():
    """같은 섹터 5% 주문 6건 — 25% 를 넘는 체결 0건(P0-3 재현 30.00%)."""
    sim = PortfolioSimulator(initial_cash=1_000_000.0, slippage_bps=0.0, max_sector=0.25)
    tickers = [f"T{i}" for i in range(6)]
    for t in tickers:
        sim.submit(_sim_order(t))
    day = date(2026, 5, 11)
    row = sim.process_day(day, {t: Bar(date=day, raw_open=100.0, raw_high=101.0, raw_low=99.0,
                                       raw_close=100.0, sector="Tech") for t in tickers})
    filled = [e for e in sim.events if e["event"] == "buy_filled"]
    assert len(filled) == 5, f"섹터 상한을 넘겨 체결됐다: {len(filled)}건"
    assert row["sector_weights"]["Tech"] <= 0.25 + 1e-9
    assert any(e["event"] == "tranche_rejected" and e.get("capped_by") == "SECTOR_CAP"
               for e in sim.events)


def test_split_halves_iv_high_in_portfolio_simulator():
    """2:1 분할 — 익절가 $120 이 $60, 기준 수량도 함께 조정된다(P0-3)."""
    sim = PortfolioSimulator(initial_cash=1_000_000.0, slippage_bps=0.0)
    d1, d2 = date(2026, 5, 11), date(2026, 5, 12)
    sim.submit(_sim_order("AAA", iv_high=120.0))
    sim.process_day(d1, {"AAA": Bar(date=d1, raw_open=100.0, raw_high=101.0, raw_low=99.0,
                                    raw_close=100.0, sadj_close=100.0, sector="Tech")})
    pos = sim.positions["AAA"]
    shares_before, basis_before = pos.shares, pos.trim_basis_qty
    sim.register_corporate_actions([CorporateAction(ticker="AAA", ex_date=d2, kind="split", ratio=2.0)])
    sim.process_day(d2, {"AAA": Bar(date=d2, raw_open=52.0, raw_high=53.0, raw_low=51.0,
                                    raw_close=52.0, sadj_close=52.0, sector="Tech")})
    assert pos.iv_high == pytest.approx(60.0)
    assert pos.shares == pytest.approx(shares_before * 2)
    assert pos.trim_basis_qty == pytest.approx(basis_before * 2)
    # 분할 뒤 raw_high(53) < 60 이므로 분할익절은 아직 닿지 않는다 — 그래도 조건은 살아 있다
    sim.process_day(date(2026, 5, 13),
                    {"AAA": Bar(date=date(2026, 5, 13), raw_open=59.0, raw_high=61.0, raw_low=58.0,
                                raw_close=60.0, sadj_close=60.0, sector="Tech")})
    trims = [e for e in sim.events if e["event"] == "exit_filled" and e.get("reason") == "iv_high"]
    assert trims, "분할 뒤 익절가가 $60 으로 내려가지 않았다"


def test_live_split_halves_iv_high():
    """live 원장도 분할 때 `iv_high` 를 1/ratio 로 내리고 수량·기준 수량을 함께 조정한다(P0-3)."""
    record = record_of(sid="2026-02-06-AAA-BUY-001", as_of="2026-02-06")
    record["valuation"] = {"iv_high": 120.0}
    # 2:1 분할 — Yahoo `Close`(sadj)는 50 으로 이어지고 계수는 2→1 (raw = sadj × 계수)
    closes = [50.0, 50.5] * 12
    panel = panel_ohlc(closes, [c + 0.3 for c in closes], [c - 0.3 for c in closes], closes,
                       splits={date(2026, 6, 10): 2.0})
    entry_day = panel.frame.index[20].date()          # 분할 전 진입(RSI 워밍업 뒤)
    execution = {"signal_id": record["signal_id"], "snapshot_id": record["snapshot_id"],
                 "at": f"{entry_day.isoformat()}T13:30:00Z",
                 "quant_track": {"execution_event": "buy_filled", "tranche": 1,
                                 "fill_price": 100.0, "weight": 0.02, "stop_price": 86.0}}
    result = live.settle_positions([record], {record["signal_id"]: execution}, {},
                                   {"AAA": panel}, panel.frame.index[-1].date())
    st = result["by_signal"][record["signal_id"]]
    assert st["iv_high"] == pytest.approx(60.0), "2:1 분할 뒤 익절가가 $60 이 아니다"
    assert st["qty"] == pytest.approx(0.02 * 1_000_000 / 100.0 * 2)      # 200주 → 400주
    assert st["trim_basis_qty"] == pytest.approx(st["qty"])              # 기준 수량도 함께
    split = [e for e in result["events"] if e["event"] == "split_adjustment"]
    assert split and split[0]["iv_high"] == pytest.approx(60.0)


def test_live_first_fill_respects_sector_cap():
    """live `settle` 도 1차 체결에서 섹터 상한을 본다 — 5% × 6 이 25% 를 넘지 않는다."""
    records, panels = [], {}
    for i in range(6):
        ticker = f"S{i}"
        records.append(record_of(sid=f"2026-05-08-{ticker}-BUY-001", ticker=ticker,
                                 plan_weight=0.125))
        panels[ticker] = panel_of([100.0, 100.0], ticker=ticker)
    out = live.settle(records, {}, panels, date(2026, 5, 11), equity_base=1_000_000.0)
    fills = [e for e in out["new_events"] if e["quant_track"]["execution_event"] == "buy_filled"]
    assert len(fills) == 5
    assert sum(e["quant_track"]["weight"] for e in fills) <= 0.25 + 1e-9
    assert any(row.get("reason") == "SECTOR_CAP" for row in out["settled"])


def test_live_tranche_two_uses_plan_weight():
    """2차 수량은 `plan_weight × tranche weight` — 계획 1.5%, 상한까지(2.0%)가 아니다(A-1)."""
    record = record_of()
    prices = [100.0, 99.0, 92.0, 94.0, 96.0] + [96.0] * 10
    result = _settle(record, prices, date(2026, 5, 29))
    fill = next(e for e in result["events"] if e.get("event") == "buy_filled" and e.get("tranche") == 2)
    assert fill["qty_pct"] == pytest.approx(0.05 * 0.30, rel=0.02), fill
    total = result["positions"]["AAA"]
    assert total["tranches"][1]["status"] == "filled"


def test_live_third_tranche_dma_fills_next_open_not_same_bar():
    """200일선 −3% 조건은 **다음 거래일 시가**에 체결한다 — 같은 봉 시가 룩어헤드 제거(A-2)."""
    record = record_of(sid="2026-02-06-AAA-BUY-001", as_of="2026-02-06")
    prefix = [100.0, 101.0] * 15                       # RSI 워밍업(완만한 등락)
    decline = [round(100.0 - 0.01 * i, 2) for i in range(1, 201)]
    closes = prefix + decline + [95.5, 95.0, 95.0]      # 마지막 3봉: 200일선 −3% 이탈
    opens = closes[:]
    opens[-3] = 97.0
    opens[-2] = 95.0
    panel = panel_ohlc(opens, [c + 0.5 for c in closes], [c - 0.5 for c in closes], closes,
                       start="2026-01-05")
    entry_day = panel.frame.index[30].date()
    execution = {"signal_id": record["signal_id"], "snapshot_id": record["snapshot_id"],
                 "at": f"{entry_day.isoformat()}T13:30:00Z",
                 "quant_track": {"execution_event": "buy_filled", "tranche": 1,
                                 "fill_price": 100.0, "weight": 0.02, "stop_price": 86.0}}
    reanchor = {"first_fill": 100.0, "stop_price": 80.0,
                "plan": [{"tranche": 2, "price_ref": 93.0, "active": True},
                         {"tranche": 3, "price_ref": 86.0, "active": True}]}
    result = live.settle_positions(
        [record], {record["signal_id"]: execution}, {record["signal_id"]: reanchor},
        {"AAA": panel}, panel.frame.index[-1].date(),
        {"risk": {"risk_per_trade": 0.0075, "max_position": 0.05}})
    third = [e for e in result["events"] if e.get("event") == "buy_filled" and e.get("tranche") == 3]
    assert third, "200일선 조건 체결이 없다"
    assert third[0]["reason"] == "dma_next_open"
    assert third[0]["price"] == pytest.approx(95.0, abs=0.01), "조건 봉 시가(97.0)로 체결됐다 — 룩어헤드"
    assert third[0]["fill_basis"] == "daily_ohlc_assumption"


def test_live_stop_and_tranche_same_bar_stops_first_and_cancels():
    """같은 봉에서 손절과 추가 진입이 모두 닿으면 손절 우선 + 잔여 주문 취소."""
    record = record_of()
    record["position"]["stop_distance"] = 0.05                 # 초기 손절 95
    panel = panel_ohlc([100.0, 94.0, 94.0], [100.5, 94.5, 94.5],
                       [99.5, 92.0, 92.0], [100.0, 92.0, 92.0])
    result = live.settle_positions([record], {record["signal_id"]: fill_of(record)}, {},
                                   {"AAA": panel}, date(2026, 5, 13),
                                   {"risk": {"risk_per_trade": 0.0075, "max_position": 0.05}})
    events = result["events"]
    assert any(e["event"] == "exit_filled" and e["reason"] == "S5" for e in events)
    assert not any(e.get("event") == "buy_filled" and e.get("tranche") == 2 for e in events)
    cancelled = [r for r in result["positions"]["AAA"]["tranches"] if r["status"] == "cancelled"]
    assert cancelled and all(r["cancelled_by"] == "S5" for r in cancelled)


def test_position_events_are_idempotent_on_rerun(tmp_path):
    """세션 이벤트 로그를 두 번 써도 늘어나지 않는다 — 재실행 멱등성(A-3)."""
    record = record_of()
    prices = [100.0, 99.0, 92.0, 94.0, 96.0] + [96.0] * 10
    path = tmp_path / "position_events.jsonl"
    for _ in range(2):
        live.settle_positions([record], {record["signal_id"]: fill_of(record)}, {},
                              {"AAA": panel_of(prices)}, date(2026, 5, 29),
                              {"risk": {"risk_per_trade": 0.0075, "max_position": 0.05}},
                              position_events_path=path)
    rows = live.read_position_events(path)
    keys = [r["event_id"] for r in rows]
    assert len(keys) == len(set(keys))
    assert any(r["event"] == "buy_filled" and r.get("tranche") == 2 for r in rows)
    text = path.read_text(encoding="utf-8")
    for banned in ("\"shares\"", "\"avg_cost\"", "\"cash_balance\""):
        assert banned not in text, f"position_events 에 금지 키 {banned}"


# ── 12-4. P0-4 Paper 채점 분리 ───────────────────────────────────────────────
def test_paper_grade_ignores_holdout_boundary():
    """2026-07-31 신호가 63거래일 만기(10-29)를 채웠으면 홀드아웃과 무관하게 complete."""
    from engine.utils.dates import add_trading_days, trading_days

    as_of = date(2026, 7, 31)
    maturity = add_trading_days(as_of, 63)
    days = trading_days(as_of, date(2026, 12, 31))
    panel = nyse_panel({d: 100.0 for d in days}, start=as_of, end=date(2026, 12, 31))
    record = record_of(sid="2026-07-31-AAA-BUY-001", as_of="2026-07-31")
    record["signal_id"] = "2026-07-31-AAA-BUY-001"
    record["data_cutoff_at"] = "2026-07-30T22:00:00Z"
    record["signal_published_at"] = "2026-07-31T02:00:00Z"
    record["order_cutoff_at"] = "2026-07-31T14:25:00Z"
    record["target_session"] = "2026-07-31"
    execution = {"signal_id": record["signal_id"], "snapshot_id": SNAP,
                 "at": "2026-07-31T13:30:00Z",
                 "quant_track": {"execution_event": "buy_filled", "tranche": 1,
                                 "fill_price": 100.0, "weight": 0.02, "stop_price": 86.0}}
    graded = live.grade_all([record], {record["signal_id"]: execution}, {"AAA": panel},
                            date(2026, 11, 20), holdout_start="2026-08-20")
    h = graded[0]["horizons"]["3m"]
    assert h["maturity"] == maturity.isoformat()
    assert h["complete"] is True and h["truncated"] is False and h["eligible"] is True
    assert h["observed_days"] == 63
    assert graded[0]["mode"] == "paper"
    # 같은 데이터를 보정 모드로 채점하면 홀드아웃 경계 표시가 살아난다(경로가 분리돼 있다)
    calibrated = live.grade_all([record], {record["signal_id"]: execution}, {"AAA": panel},
                                date(2026, 11, 20), holdout_start="2026-08-20",
                                mode="calibration")
    assert calibrated[0]["horizons"]["3m"]["truncated"] is True
    assert calibrated[0]["horizons"]["3m"]["complete"] is False


def test_calibration_contract_requires_metadata():
    """필수 메타가 빠진 폴드 관측은 조용히 통과하지 않는다(P0-4)."""
    from engine.backtest.calibration import (FoldObservation, check_observation_contract,
                                             observations_from_fold_results)

    bare = [FoldObservation("F1", 60.0, 0.05, -0.10)]
    with pytest.raises(ValueError, match="메타 누락"):
        check_observation_contract(bare)
    full = [FoldObservation("F1", 60.0, 0.05, -0.10, maturity_date="2026-02-15",
                            holdout_start="2026-08-20", signal_count=30,
                            code_version="abc", snapshot_id="sha256:s1", horizon_td=63)]
    check_observation_contract(full)                     # 정상은 통과
    assert observations_from_fold_results([], holdout_start=None) == []


# ── 12-5. P1-3 독립 A/B 계좌 · 비용 이중 차감 ────────────────────────────────
def _e2e_fixture(*, validated="2026-05-11T13:00:00Z", prices=None):
    """§8.1 종단 픽스처 — 기초 $100,000, 20주, 비용 0bp, $100 진입 → $90 손절."""
    record = record_of(sid="2026-05-08-AAA-BUY-001", stop_distance=0.10)
    execution = {"signal_id": record["signal_id"], "snapshot_id": SNAP,
                 "at": "2026-05-11T13:30:00Z",
                 "quant_track": {"execution_event": "buy_filled", "tranche": 1,
                                 "fill_price": 100.0, "weight": 0.02, "stop_price": 90.0,
                                 "slippage_bps": 0.0}}
    report = report_of(sid=record["signal_id"], ticker="AAA", completed="2026-05-08T23:15:00Z")
    el = _eligibility(report, record, report_validated_at=validated, execution=execution)
    view = debate_mod.public_report(report, el,
                                    debate_mod.actions_for(report, record, execution))
    panel = panel_of(prices or ([100.0] * 3 + [90.0] + [90.0] * 70))
    return record, execution, view, panel


def test_end_to_end_policy_accounts_numbers():
    """§8.1: quant −0.20%, debated 0.00%, 순효과 +0.20%p, 회피 손실 $200."""
    record, execution, view, panel = _e2e_fixture()
    comp = debate_mod.compute_debate_comparison(
        {record["signal_id"]: view}, [record], {record["signal_id"]: execution}, {"AAA": panel},
        date(2026, 9, 30), initial_cash=100_000.0,
        costs={"slippage": 0.0, "commission": 0.0, "fx": 0.0})
    assert comp["R_quant"] == pytest.approx(-0.002, abs=1e-6)
    assert comp["R_debated"] == pytest.approx(0.0, abs=1e-9)
    assert comp["policy_diff_pp"] == pytest.approx(0.2, abs=1e-6)
    assert comp["quant_mdd"] == pytest.approx(-0.002, abs=1e-6)
    assert comp["debate_mdd"] == pytest.approx(0.0, abs=1e-9)
    assert comp["accounts"]["quant_only"]["final_equity"] == pytest.approx(99_800.0, abs=0.5)
    assert comp["accounts"]["codex_debated"]["final_equity"] == pytest.approx(100_000.0, abs=0.5)
    pair = comp["pair_summary"]
    assert pair["avoided_loss_usd"] == pytest.approx(200.0, abs=0.5)
    assert pair["missed_gain_usd"] == pytest.approx(0.0, abs=0.5)
    assert pair["pair_net"] == pytest.approx(0.002, abs=1e-6)
    assert pair["pairs"] == 1
    # 페어 손익과 계좌 순효과는 서로 다른 키로 분리돼 있다
    assert comp["net_pp"] == pytest.approx(0.2, abs=1e-4)
    assert comp["policy_diff_pp"] == pytest.approx(0.2, abs=1e-4)
    # 표본 1건은 자동 하향 판단에 넣지 않는다
    assert comp["verdict"] == debate_mod.VERDICT_INSUFFICIENT


def test_late_validation_makes_policy_diff_zero():
    """검증만 마감 후로 옮기면 두 계좌가 같은 주문을 처리해 순효과 0%p, 행동 차이 0건."""
    record, execution, view, panel = _e2e_fixture(validated="2026-05-11T14:40:00Z")
    assert view["eligible"] is False
    comp = debate_mod.compute_debate_comparison(
        {record["signal_id"]: view}, [record], {record["signal_id"]: execution}, {"AAA": panel},
        date(2026, 9, 30), initial_cash=100_000.0,
        costs={"slippage": 0.0, "commission": 0.0, "fx": 0.0})
    assert comp["divergent_pairs"] == 0
    assert comp["policy_diff_pp"] == pytest.approx(0.0, abs=1e-9)
    assert comp["R_quant"] == pytest.approx(0.0, abs=1e-9)
    assert comp["R_debated"] == pytest.approx(0.0, abs=1e-9)


def test_bucket_has_no_hardcoded_debate_mdd():
    """계좌 곡선이 없으면 방어력은 None — 0.0 을 채워 '개선됨'으로 만들지 않는다(A-4)."""
    pairs = [{"return": -0.10, "weight": 0.02, "mdd": -0.10, "complete": True,
              "ticker": "AAA", "entry_day": "2026-05-11"}]
    bucket = debate_mod._bucket(pairs)
    assert bucket["drawdown"]["defense_improved"] is None
    assert bucket["drawdown"]["debate_mdd"] is None
    accounts = {"accounts": {"quant_only": {"mdd": -0.10}, "codex_debated": {"mdd": -0.02}}}
    improved = debate_mod._bucket(pairs, accounts)
    assert improved["drawdown"]["debate_mdd"] == pytest.approx(-0.02)
    assert improved["drawdown"]["defense_improved"] is True
    worse = {"accounts": {"quant_only": {"mdd": -0.02}, "codex_debated": {"mdd": -0.10}}}
    assert debate_mod._bucket(pairs, worse)["drawdown"]["defense_improved"] is False


def test_auto_downgrade_requires_better_mdd_and_policy_diff():
    """계좌 순자산 차이 +0.20%p 라도 MDD 절댓값이 나빠지면 전환 후보가 아니다."""
    rows = [{"quarter": f"2026Q{n + 1}", "n_graded": 30, "n_eff": 30,
             "policy_diff": {"policy_diff_pp": 0.30},
             "drawdown": {"defense_improved": True},
             "policy_version": "params_v001", "verdict": debate_mod.VERDICT_EFFECTIVE}
            for n in range(2)]
    good = {"n_graded_pairs": 30, "n_eff": 30, "policy_diff_pp": 0.30,
            "quant_mdd": -0.10, "debate_mdd": -0.08, "quarters": rows,
            "drawdown": {"defense_improved": True}, "auto_downgrade_enabled": False}
    verdict = debate_mod.evaluate_auto_downgrade_eligibility(good, [])
    assert verdict["recommend_enable"] is True and verdict["basis"] == "account_policy_diff"
    assert verdict["consecutive_quarters"] == 2

    worse_mdd = {**good, "debate_mdd": -0.20}
    assert debate_mod.evaluate_auto_downgrade_eligibility(worse_mdd, [])["recommend_enable"] is False

    other_version = {**good, "quarters": [{**rows[0], "policy_version": "params_v002"}, rows[1]]}
    verdict_v = debate_mod.evaluate_auto_downgrade_eligibility(other_version, [])
    assert verdict_v["recommend_enable"] is False and verdict_v["same_policy_version"] is False


def test_pilot_does_not_double_count_entry_slippage():
    """진입 슬리피지가 체결가에 있으면 왕복 34bp 를 다시 빼지 않는다(P1-3)."""
    record = record_of()
    execution = {"signal_id": record["signal_id"], "snapshot_id": SNAP,
                 "at": "2026-05-11T13:30:00Z",
                 "quant_track": {"execution_event": "buy_filled", "tranche": 1,
                                 "fill_price": 100.1, "weight": 0.02, "slippage_bps": 10.0}}
    graded = [{"signal_id": record["signal_id"],
               "horizons": {"3m": {"return": -0.000999, "excess_return": -0.000999,
                                   "mdd": -0.001, "complete": True}}}]
    ev = evaluate_pilot([record], graded, executions={record["signal_id"]: execution},
                        portfolio={"total_return": 0.0, "avg_exposure": 0.0},
                        benchmark_return=0.0)
    metrics = {m["key"]: m for m in ev["metrics"]}
    assert ev["cost_breakdown"]["entry_slippage_bps"] == 0.0
    assert ev["cost_breakdown"]["exit_slippage_bps"] == 10.0
    assert ev["cost_applied"] == pytest.approx(0.0024, abs=1e-9)      # 10+4+10 bp
    assert metrics["net_after_cost"]["value"] == pytest.approx(-0.000999 - 0.0024, abs=1e-6)
    # 진입 슬리피지가 0 인 체결이면 미반영 비용에 진입분도 들어간다
    no_slip = {**execution, "quant_track": {**execution["quant_track"], "slippage_bps": 0.0}}
    ev2 = evaluate_pilot([record], graded, executions={record["signal_id"]: no_slip},
                         portfolio={"total_return": 0.0, "avg_exposure": 0.0},
                         benchmark_return=0.0)
    assert ev2["cost_breakdown"]["entry_slippage_bps"] == 10.0
    assert ev2["cost_applied"] == pytest.approx(0.0034, abs=1e-9)


def test_pilot_charges_no_cost_without_trades():
    ev = evaluate_pilot([record_of()], [], portfolio={"total_return": 0.0, "avg_exposure": 0.0},
                        benchmark_return=0.0)
    metrics = {m["key"]: m for m in ev["metrics"]}
    assert ev["n_eligible"] == 0
    assert ev["cost_applied"] == pytest.approx(0.0)
    assert metrics["net_after_cost"]["value"] is None


def test_gap_outside_band_creates_no_fill_and_no_pair():
    """§8.1: 두 계좌가 볼 수 있는 시가가 밴드 밖이면 체결도 행동 차이도 0건이다."""
    record = record_of(sid="2026-05-08-AAA-BUY-001")
    out = live.settle([record], {}, {"AAA": panel_of([120.0, 120.0, 120.0])}, date(2026, 5, 11))
    assert not [e for e in out["new_events"] if e["quant_track"]["execution_event"] == "buy_filled"]
    assert out["settled"][0]["reason"] == "outside_order_range"
    executions: dict = {}
    view = debate_mod.public_report(report_of(sid=record["signal_id"]), {"eligible": True, "late": False,
                                                                       "invalid": False}, None)
    comp = debate_mod.compute_debate_comparison(
        {record["signal_id"]: view}, [record], executions,
        {"AAA": panel_of([120.0] * 70)}, date(2026, 9, 30), initial_cash=100_000.0,
        costs={"slippage": 0.0, "commission": 0.0, "fx": 0.0})
    assert comp["divergent_pairs"] == 0
    assert comp["policy_diff_pp"] == pytest.approx(0.0)
    assert comp["accounts"]["quant_only"]["n_trades"] == 0


def test_policy_account_closes_position_at_maturity():
    """두 계좌는 63거래일 만기에 종가로 청산한다 — 페어 채점 구간과 같은 창을 쓴다."""
    record = record_of(sid="2026-05-08-AAA-BUY-001", stop_distance=0.50)
    execution = {"signal_id": record["signal_id"], "snapshot_id": SNAP,
                 "at": "2026-05-11T13:30:00Z",
                 "quant_track": {"execution_event": "buy_filled", "tranche": 1,
                                 "fill_price": 100.0, "weight": 0.02, "stop_price": 50.0,
                                 "slippage_bps": 0.0}}
    from engine.utils.dates import add_trading_days, trading_days

    start = date(2026, 5, 11)
    end = add_trading_days(start, 80)
    prices = {d: (100.0 if d <= add_trading_days(start, 63) else 130.0)
              for d in trading_days(start, end)}
    panel = nyse_panel(prices, start=start, end=end)
    view = debate_mod.public_report(
        report_of(sid=record["signal_id"]), {"eligible": True, "late": False, "invalid": False},
        debate_mod.actions_for(report_of(sid=record["signal_id"]), record, execution))
    comp = debate_mod.compute_debate_comparison(
        {record["signal_id"]: view}, [record], {record["signal_id"]: execution}, {"AAA": panel},
        end, initial_cash=100_000.0, costs={"slippage": 0.0, "commission": 0.0, "fx": 0.0})
    # 63거래일 창 안에서는 가격이 그대로 → 계좌 수익률 0, 만기 뒤 급등은 계좌에 들어오지 않는다
    assert comp["R_quant"] == pytest.approx(0.0, abs=1e-6)
    assert comp["accounts"]["quant_only"]["final_equity"] == pytest.approx(100_000.0, abs=0.5)


def test_default_stop_distance_third_tranche_is_not_dead_code():
    """기본 손절 14%·3차 −14% 조합에서도 3차가 죽지 않는다 — DMA 전용으로 무장(A-7)."""
    record = record_of()                     # stop_distance = 0.14
    result = _settle(record, [100.0] * 20, date(2026, 6, 5))
    third = result["positions"]["AAA"]["tranches"][2]
    assert third["status"] == "pending" and third["trigger"] == "dma200"
    assert third["price_ref"] == pytest.approx(86.0, abs=0.5)
