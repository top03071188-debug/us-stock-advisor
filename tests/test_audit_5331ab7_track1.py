"""test_audit_5331ab7_track1 — 감사 5331ab7 트랙 1 (01 · 02 · 05 · 부수 ②).

검사하는 계약
- **[01] 검증 시각 신뢰 경계** — 배치가 읽은 시각(`now`)보다 **나중에 완료**된 보고서는 무조건
  부적격(`COMPLETED_AFTER_NOW`)이고, `now` 상한 보정이 미래 완료를 구제하지 못한다.
  규칙 마감(`freeze_deadline_at`)과 실제 동결 시각(`frozen_at`)은 따로 기록되며 `frozen_at` 하위 호환은
  그대로다.
- **[02] 다중 회차 채택** — 한 `signal_id` 에 `-01`·`-02` 가 오면 **가장 먼저 적격으로 확정된** 보고서를
  채택하고, 뒤 회차는 `superseded`/`rejected` 로만 남는다. `by_signal`·`debate_events` 문서·요약이
  채택된 `report_id`·`report_sha256`·`snapshot_id` 를 명시하고, 소급 검사(`drop_retroactive_pairs`)는
  배열의 마지막이 아니라 **채택본**의 검증 시각을 쓴다. `ATTACHED_AFTER_FILL` 배지는
  "무효(체결 후 검증)"다.
- **[05] A/B 독립 모집단·당일 섹터 캡·분기 창** — 모집단은 정량 `buy_filled` 가 아니라 **공통 동결 주문
  의도**이고(섹터 캡에 막힌 후보 포함), 같은 날 신규 진입은 진입 즉시 섹터 누적에 합산돼 25% 캡을
  뚫지 못하며, 갭 손절은 신규 진입보다 **먼저** 현금을 만든다. 분기 성적의 계좌 시뮬레이션은 그 분기
  창으로 고정된다.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
import pytest

from engine.data.prices import build_price_frame
from engine.ledger import debate as debate_mod
from engine.ledger.record import execution_event, publish_record
from scripts import build_public_data, run_daily

SNAP = "sha256:abc123"
PUBLISHED = "2026-05-08T22:02:11Z"
ORDER_CUTOFF = "2026-05-11T14:25:00Z"      # 규칙 동결 마감 = 14:20:00Z
FREEZE = "2026-05-11T14:20:00Z"
FILL_AT = "2026-05-11T13:30:00Z"
FREE = {"slippage": 0.0, "commission": 0.0, "fx": 0.0}


# ────────────────────────────── 헬퍼 ──────────────────────────────
def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def panel_of(prices: list[float], start: str = "2026-05-11", ticker: str = "AAA"):
    idx = pd.bdate_range(start=start, periods=len(prices))
    close = pd.Series(prices, index=idx, dtype=float)
    frame = pd.DataFrame({"Open": close, "High": close * 1.005, "Low": close * 0.995,
                          "Close": close, "Adj Close": close, "Volume": 1_000_000.0}, index=idx)
    return build_price_frame(ticker, frame, None)


def panel_ohlc(opens, highs, lows, closes, start: str = "2026-05-11", ticker: str = "AAA"):
    idx = pd.bdate_range(start=start, periods=len(closes))
    frame = pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes,
                          "Adj Close": closes, "Volume": 1_000_000.0}, index=idx)
    return build_price_frame(ticker, frame, None)


def record_of(sid: str = "2026-05-08-AAA-BUY-001", *, ticker: str = "AAA", as_of: str = "2026-05-08",
              target_session: str = "2026-05-11", plan_weight: float = 0.05, sector: str = "Tech",
              published: str = PUBLISHED, order_cutoff: str = ORDER_CUTOFF, price: float = 100.0,
              data_cutoff: str = "2026-05-08T22:00:00Z") -> dict:
    record = publish_record(
        signal_id=sid, ticker=ticker, type="BUY", grade="B", entry_score=72.0, as_of=as_of,
        data_cutoff_at=data_cutoff, signal_published_at=published, order_cutoff_at=order_cutoff,
        target_session=target_session, generated_price=price,
        target_band={"lo": 110.0, "hi": 130.0}, expected_mdd=-0.12, plan_weight=plan_weight,
        stop_distance=0.14, quant_intent="pending_buy", snapshot_id=SNAP,
    )
    record["trade_grade"] = "BUY"
    record["veto"] = []
    record["sector"] = sector
    record["entry_band"] = {"stop_min_sadj": price * 0.90, "limit_max_sadj": price * 1.05,
                            "dma200_floor_sadj": price * 0.70,
                            "provisional_stop_sadj": price * 0.86,
                            "required_margin": 0.05, "split_factor": 1.0}
    return record


def report_of(sid: str = "2026-05-08-AAA-BUY-001", *, ticker: str = "AAA", report_id: str | None = None,
              completed: str = "2026-05-11T12:00:00Z", validated: str = "2026-05-11T13:00:00Z",
              recommendation: str = "WATCH", snapshot: str = SNAP,
              published: str = PUBLISHED, order: str = ORDER_CUTOFF) -> dict:
    report = {
        "report_id": report_id or f"{sid}-debate-01", "ticker": ticker, "signal_id": sid,
        "snapshot_id": snapshot, "as_of": "2026-05-08", "data_cutoff_at": "2026-05-08T22:00:00Z",
        "signal_published_at": published, "order_cutoff_at": order, "completed_at": completed,
        "late": False, "status": "complete", "intent": "pending_hold_cash",
        "round1": [{"skill": "buffett", "score": 62, "stance": "buy", "confidence": 0.71,
                    "reasons": ["IV 대비 21% 할인"], "news_read": []}],
        "round2": [],
        "round3": {"guard": {"veto_codes": [], "additional_blocks": [], "structural_risk": 58},
                   "chair": {"consensus": ["가격은 매력적"], "dissent": [],
                             "grade_recommendation": recommendation, "conflict": False,
                             "one_line_ko": "2차 분할가까지 기다린다"}},
        "validation": {"report_validated_at": validated},
        "quant_grade": "BUY",
    }
    return debate_mod.with_report_receipt(report)


def fill_of(record: dict, *, price: float = 100.0) -> dict:
    return execution_event(
        signal_id=record["signal_id"], snapshot_id=record["snapshot_id"], at=FILL_AT,
        execution_event="buy_filled", tranche=1, fill_price=price,
        weight=float(record["position"]["tranche_weight"]),
        stop_price=price * (1.0 - float(record["position"]["stop_distance"])))


def sector_cap_no_action(record: dict, *, sector: str = "Tech", pct: str = "26.00%") -> dict:
    """`live.settle` 이 섹터 캡 차단 때 남기는 `no_action` 이벤트와 같은 모양."""
    return execution_event(
        signal_id=record["signal_id"], snapshot_id=record["snapshot_id"], at=FILL_AT,
        execution_event="no_action",
        note=f"섹터 {sector} 상한 25% 초과 예정({pct}) — 체결하지 않는다(4.12)")


def view_of(record: dict, *, execution: dict | None, eligible: bool = True,
            divergent: bool = True) -> dict:
    report = report_of(sid=record["signal_id"], ticker=record["ticker"])
    el = {"eligible": eligible, "late": False, "invalid": False, "reasons": []}
    actions = {"quant_grade": "BUY", "recommendation": "WATCH", "quant_action": "buy_filled",
               "debate_action": "hold_cash" if (eligible and divergent) else "buy_filled",
               "divergent": bool(eligible and divergent), "auto_downgrade_applied": False,
               "veto_codes": []}
    return debate_mod.public_report(report, el, actions)


def candidate(ticker: str, *, sector: str = "Tech", entry_day: date = date(2026, 5, 11),
              weight: float = 0.05, fill_price: float | None = None,
              stop_price: float = 50.0) -> dict:
    return {"signal_id": f"s-{ticker}", "ticker": ticker, "sector": sector,
            "entry_day": entry_day, "weight": weight, "fill_price": fill_price,
            "stop_price": stop_price, "stop_distance": 0.5}


# ══════════════════════════════ 01 ══════════════════════════════
def test_completed_after_receipt_is_rejected_even_when_now_caps_validation():
    """삭제 전 코드: 완료 14:21Z·검증 14:22Z·배치 14:19:59Z 가 `eligible=True` 였다(감사 01-A1)."""
    record = record_of()
    el = debate_mod.eligibility(
        report_of(completed="2026-05-11T14:21:00Z", validated="2026-05-11T14:22:00Z"), record,
        now=ts("2026-05-11T14:19:59Z"))
    assert el["invalid"] is True and el["eligible"] is False
    assert "COMPLETED_AFTER_NOW" in el["reasons"]
    # 값은 now 로 깎였지만 그 사실이 **구제 사유가 되지 않는다** — 원값·수신 시각이 함께 남는다.
    assert el["report_validated_at"] == "2026-05-11T14:19:59Z"
    assert el["validation_claimed_at"] == "2026-05-11T14:22:00Z"
    assert el["received_at"] == "2026-05-11T14:19:59Z"
    assert el["posthoc_validation"] is True and "POSTHOC_VALIDATION" in el["notes"]


def test_completion_exactly_at_receipt_is_still_eligible():
    """경계: 완료 == 수신 시각은 미래가 아니다(정각 규칙은 `<=`)."""
    record = record_of()
    el = debate_mod.eligibility(report_of(completed="2026-05-11T14:00:00Z",
                                          validated="2026-05-11T14:19:59Z"),
                                record, now=ts("2026-05-11T14:19:59Z"))
    assert "COMPLETED_AFTER_NOW" not in el["reasons"] and el["eligible"] is True


def test_future_completion_is_not_rescued_by_a_pre_freeze_validation_claim():
    """완료 14:25Z(수신 14:20Z)에 검증 14:05Z 를 적어도 — **동결 전 검증** 주장으로 구제되지 않는다."""
    record = record_of()
    el = debate_mod.eligibility(report_of(completed="2026-05-11T14:25:00Z",
                                          validated="2026-05-11T14:05:00Z"),
                                record, now=ts("2026-05-11T14:20:00Z"))
    assert el["invalid"] is True and el["eligible"] is False
    assert "COMPLETED_AFTER_NOW" in el["reasons"]
    assert el["validation_claimed_at"] == "2026-05-11T14:05:00Z"      # 주장은 기록으로만 남는다


def test_claimed_pre_freeze_validation_is_trusted_only_up_to_receipt():
    """수신이 동결 뒤여도 **보고서가 적은** 검증 시각이 동결 전이면 편입된다(상한은 수신 시각)."""
    record = record_of()
    el = debate_mod.eligibility(report_of(completed="2026-05-11T13:00:00Z",
                                          validated="2026-05-11T14:10:00Z"),
                                record, now=ts("2026-05-11T14:30:00Z"))
    assert el["report_validated_at"] == "2026-05-11T14:10:00Z"
    assert el["received_at"] == "2026-05-11T14:30:00Z"
    assert el["posthoc_validation"] is False and el["eligible"] is True
    # 그 주장이 수신 시각을 넘으면(미래) 깎기고 late 가 된다 — 상한은 한 방향으로만 막는다
    late = debate_mod.eligibility(report_of(completed="2026-05-11T13:00:00Z",
                                            validated="2026-05-11T15:10:00Z"),
                                  record, now=ts("2026-05-11T14:30:00Z"))
    assert late["report_validated_at"] == "2026-05-11T14:30:00Z"
    assert late["posthoc_validation"] is True and "VALIDATED_AFTER_FREEZE" in late["reasons"]


def test_freeze_deadline_and_frozen_at_are_recorded_separately():
    """규칙 마감(주문마감 − 5분)과 레코드가 밝힌 실제 동결 시각은 **다른 값**이다(01 용어 분리)."""
    record = record_of()
    record["frozen_at"] = "2026-05-11T13:05:00Z"      # 주문 확정 시각처럼 이른 값(구 슬롯 기록)
    el = debate_mod.eligibility(report_of(completed="2026-05-11T12:00:00Z",
                                          validated="2026-05-11T13:10:00Z"),
                                record, now=ts("2026-05-11T14:00:00Z"))
    assert el["freeze_deadline_at"] == FREEZE
    assert el["frozen_at"] == "2026-05-11T13:05:00Z"
    assert el["frozen_source"] == "record"
    # 하위 호환: `frozen_at` 은 예전처럼 **비교에 쓰인 유효값**이다(이른 값이면 검증이 동결 뒤로 보인다)
    assert "VALIDATED_AFTER_FREEZE" in el["reasons"]


def test_event_and_summary_keep_freeze_deadline_and_receipt_time(tmp_path):
    record = record_of()
    result = debate_mod.reconcile_debates(
        {"reports": [report_of()]}, [record], {}, event_dir=tmp_path / "events",
        now=ts("2026-05-11T14:24:00Z"), as_of=date(2026, 5, 11))
    assert result["received_at"] == "2026-05-11T14:24:00Z"
    assert result["freeze_buffer_seconds"] == 300
    doc = json.loads((tmp_path / "events" / f"{record['signal_id']}.json").read_text(encoding="utf-8"))
    event = doc["events"][0]
    assert event["freeze_deadline_at"] == FREEZE
    assert event["frozen_at"] == FREEZE
    assert event["received_at"] == "2026-05-11T14:24:00Z"
    assert result["by_signal"][record["signal_id"]]["freeze_deadline_at"] == FREEZE


def test_reconcile_rejects_future_completed_report_without_writing_event(tmp_path):
    record = record_of()
    result = debate_mod.reconcile_debates(
        {"reports": [report_of(completed="2026-05-11T14:21:00Z",
                               validated="2026-05-11T14:22:00Z")]},
        [record], {}, event_dir=tmp_path / "events", now=ts("2026-05-11T14:19:59Z"),
        as_of=date(2026, 5, 11))
    assert result["n_eligible"] == 0
    assert "COMPLETED_AFTER_NOW" in result["rejected"][0]["reasons"]
    assert result["rejected"][0]["freeze_deadline_at"] == FREEZE
    assert not (tmp_path / "events" / f"{record['signal_id']}.json").exists()


# ══════════════════════════════ 02 ══════════════════════════════
def test_late_second_report_never_replaces_the_adopted_one(tmp_path):
    """`-01`(13:00 검증) + `-02`(15:01 검증) — 채택은 `-01`, `-02` 는 rejected/rejected 이지 덮어쓰기 아님."""
    record = record_of()
    sid = record["signal_id"]
    first = report_of(completed="2026-05-11T12:00:00Z", validated="2026-05-11T13:00:00Z")
    second = report_of(report_id=f"{sid}-debate-02", completed="2026-05-11T13:10:00Z",
                       validated="2026-05-11T15:01:00Z")
    result = debate_mod.reconcile_debates(
        {"reports": [first, second]}, [record], {sid: {"at": FILL_AT,
                                                       "quant_track": {"execution_event": "no_action"}}},
        event_dir=tmp_path / "events", now=ts("2026-05-11T14:24:00Z"), as_of=date(2026, 5, 11))
    assert result["n_reports"] == 2 and result["n_eligible"] == 1
    adopted = result["by_signal"][sid]
    assert adopted["report_id"] == first["report_id"], "지연 -02 가 적격 -01 의 채택을 덮었다"
    assert adopted["eligible"] is True and adopted["badge"] == debate_mod.BADGE_DONE
    assert result["adopted"] == {sid: first["report_id"]}
    assert [row["report_id"] for row in result["rejected"]] == [second["report_id"]]
    assert result["rejected"][0]["late"] is True and result["rejected"][0]["reasons"]
    # 이벤트 문서는 채택본 하나만 담고, -02 는 어디에도 편입으로 남지 않는다
    doc = json.loads((tmp_path / "events" / f"{sid}.json").read_text(encoding="utf-8"))
    assert [e["report_id"] for e in doc["events"]] == [first["report_id"]]
    # 재실행도 같은 채택을 돌려받고 이벤트를 늘리지 않는다
    again = debate_mod.reconcile_debates(
        {"reports": [first, second]}, [record], {}, event_dir=tmp_path / "events",
        now=ts("2026-05-11T14:26:00Z"), as_of=date(2026, 5, 11))
    assert again["by_signal"][sid]["report_id"] == first["report_id"]
    doc = json.loads((tmp_path / "events" / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(doc["events"]) == 1


def test_second_eligible_report_is_superseded_not_adopted(tmp_path):
    """둘 다 동결 전에 적격이어도 **먼저 확정된** 하나만 채택되고 나머지는 superseded 다."""
    record = record_of()
    sid = record["signal_id"]
    first = report_of(completed="2026-05-11T11:00:00Z", validated="2026-05-11T11:30:00Z")
    second = report_of(report_id=f"{sid}-debate-02", completed="2026-05-11T12:00:00Z",
                       validated="2026-05-11T13:10:00Z")
    # 배열 순서를 뒤집어도 결과는 같다 — 선착순(검증 시각)이 기준이다.
    result = debate_mod.reconcile_debates(
        {"reports": [second, first]}, [record], {}, event_dir=tmp_path / "events",
        now=ts("2026-05-11T14:24:00Z"), as_of=date(2026, 5, 11))
    assert result["n_eligible"] == 1 and result["n_superseded"] == 1
    assert result["by_signal"][sid]["report_id"] == first["report_id"]
    assert result["superseded"] == [{"signal_id": sid, "report_id": second["report_id"],
                                     "reason": "SUPERSEDED_BY_ADOPTED",
                                     "adopted_report_id": first["report_id"]}]
    doc = json.loads((tmp_path / "events" / f"{sid}.json").read_text(encoding="utf-8"))
    assert doc["adopted"]["report_id"] == first["report_id"]
    assert doc["superseded"][0]["report_id"] == second["report_id"]


def test_event_and_summary_store_adopted_report_identity(tmp_path):
    """채택된 `report_id`·`report_sha256`·`snapshot_id` 가 이벤트·문서·by_signal 에 함께 남는다."""
    record = record_of()
    sid = record["signal_id"]
    report = report_of()
    result = debate_mod.reconcile_debates({"reports": [report]}, [record], {},
                                          event_dir=tmp_path / "events",
                                          now=ts("2026-05-11T14:24:00Z"), as_of=date(2026, 5, 11))
    doc = json.loads((tmp_path / "events" / f"{sid}.json").read_text(encoding="utf-8"))
    event = doc["events"][0]
    expected_sha = debate_mod.compute_report_sha256(report)
    assert doc["adopted"] == {**doc["adopted"], "report_id": report["report_id"],
                              "report_sha256": expected_sha, "snapshot_id": SNAP}
    assert doc["adopted"]["report_validated_at"] == "2026-05-11T13:00:00Z"
    assert event["report_id"] == report["report_id"]
    assert event["report_sha256"] == expected_sha and event["snapshot_id"] == SNAP
    row = result["by_signal"][sid]
    assert row["adopted_report_id"] == report["report_id"]
    assert row["adopted_report_sha256"] == expected_sha
    assert row["adopted_snapshot_id"] == SNAP
    assert result["attached"][0]["report_sha256"] == expected_sha


def test_public_rows_do_not_lend_the_adopted_event_to_a_later_report(tmp_path):
    """02 화면 쪽: `-02` 행이 `-01` 의 편입 이벤트를 받아 "토론 완료"로 보이던 증상의 종단 확인."""
    record = record_of()
    sid = record["signal_id"]
    first = report_of(completed="2026-05-11T12:00:00Z", validated="2026-05-11T13:00:00Z")
    second = report_of(report_id=f"{sid}-debate-02", completed="2026-05-11T13:10:00Z",
                       validated="2026-05-11T15:01:00Z")
    debate_mod.reconcile_debates({"reports": [first, second]}, [record], {},
                                 event_dir=tmp_path / "events", now=ts("2026-05-11T14:24:00Z"),
                                 as_of=date(2026, 5, 11))
    events = debate_mod.load_events(tmp_path / "events")
    doc = build_public_data.build_debate({"reports": [first, second]}, [record], {}, None,
                                         "2026-05-08", debate_events=events)
    rows = {row["report_id"]: row for row in doc["reports"]}
    assert rows[first["report_id"]]["eligible"] is True
    assert rows[first["report_id"]]["badge"] == debate_mod.BADGE_DIFFERENT
    assert rows[second["report_id"]]["eligible"] is False
    assert "NOT_ADOPTED" in rows[second["report_id"]]["reasons"], \
        "채택되지 않은 -02 가 -01 의 편입 결과를 그대로 받았다"
    assert doc["counts"]["eligible"] == 1


def test_badge_labels_attached_after_fill_as_after_fill():
    """부수 ②: 검증이 체결 뒤면 "무효(체결 후 검증)" — "발행 전 완료"와 사유가 다르다."""
    record = record_of()
    execution = fill_of(record)                       # 13:30Z 체결
    el = debate_mod.eligibility(report_of(completed="2026-05-11T15:20:00Z",
                                          validated="2026-05-11T15:30:00Z"),
                                record, now=ts("2026-05-11T16:00:00Z"), execution=execution)
    assert "ATTACHED_AFTER_FILL" in el["reasons"]
    assert debate_mod.debate_badge(report_of(), el) == debate_mod.BADGE_INVALID_AFTER_FILL
    assert debate_mod.BADGE_INVALID_AFTER_FILL == "무효(체결 후 검증)"
    # 발행 전에 끝난 보고서는 예전 문구 그대로다
    early = debate_mod.eligibility(report_of(completed="2026-05-08T21:00:00Z"), record)
    assert debate_mod.debate_badge(report_of(), early) == debate_mod.BADGE_INVALID


def test_drop_retroactive_pairs_uses_adopted_validation_time():
    """`-01` 은 10:00 검증(체결 전), `-02` 는 15:00 — 페어는 살아 있어야 한다(02 소급 검사)."""
    record = record_of(sid="2026-05-08-BBB-BUY-001", ticker="BBB")
    sid = record["signal_id"]
    first = report_of(sid=sid, ticker="BBB", completed="2026-05-11T09:00:00Z",
                      validated="2026-05-11T10:00:00Z")
    second = report_of(sid=sid, ticker="BBB", report_id=f"{sid}-debate-02",
                       completed="2026-05-11T14:50:00Z", validated="2026-05-11T15:00:00Z")
    execution = fill_of(record)
    row = {"signal_id": sid, "ticker": "BBB", "eligible": True, "divergent": True,
           "quant_action": "buy_filled", "debate_action": "hold_cash",
           "report_id": first["report_id"], "report_validated_at": "2026-05-11T10:00:00Z",
           "adopted_report_id": first["report_id"]}
    kept, dropped = run_daily.drop_retroactive_pairs({sid: row}, {sid: execution},
                                                     {"reports": [first, second]})
    assert kept == {sid: row} and dropped == [], "지연 -02 하나 때문에 적격 -01 페어가 버려졌다"
    # 채택 결과가 문서보다 우선한다는 규칙은 명시 매핑으로도 같다
    bare = {sid: {k: v for k, v in row.items() if k != "report_validated_at"}}
    kept2, dropped2 = run_daily.drop_retroactive_pairs(
        bare, {sid: execution}, {"reports": [first, second]},
        adopted={sid: first["report_id"]})
    assert set(kept2) == {sid} and dropped2 == []


def test_drop_retroactive_pairs_single_report_backward_compatible():
    """회차가 하나뿐인 문서는 예전과 같은 판정이다(하위 호환)."""
    record = record_of(sid="2026-05-08-CCC-BUY-001", ticker="CCC")
    sid = record["signal_id"]
    report = report_of(sid=sid, ticker="CCC", completed="2026-05-11T13:00:00Z",
                       validated="2026-05-11T13:45:00Z")
    row = {"signal_id": sid, "ticker": "CCC", "eligible": True, "divergent": True}
    kept, dropped = run_daily.drop_retroactive_pairs({sid: row}, {sid: fill_of(record)},
                                                     {"reports": [report]})
    assert kept == {} and dropped == [{"signal_id": sid, "ticker": "CCC",
                                       "execution_at": "2026-05-11T13:30:00+00:00",
                                       "report_validated_at": "2026-05-11T13:45:00+00:00",
                                       "reason": "FILLED_BEFORE_VALIDATION"}]


# ══════════════════════════════ 05 ══════════════════════════════
def _six_tech_records(plan_weight: float = 0.05) -> tuple[list[dict], dict]:
    records, executions = [], {}
    for ticker in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"):
        record = record_of(sid=f"2026-05-08-{ticker}-BUY-001", ticker=ticker, plan_weight=plan_weight)
        records.append(record)
        executions[record["signal_id"]] = (sector_cap_no_action(record) if ticker == "FFF"
                                           else fill_of(record))
    return records, executions


def test_common_intent_population_includes_sector_cap_blocked_candidate():
    """A 의 섹터 캡에 막힌 `no_action` 후보도 모집단에 오른다 — 예전에는 두 계좌 모두에서 사라졌다."""
    records, executions = _six_tech_records()
    quant = debate_mod._account_candidates({}, records, executions, policy="quant_only")
    debated = debate_mod._account_candidates({}, records, executions, policy="codex_debated")
    assert [c["ticker"] for c in quant] == ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    assert [c["ticker"] for c in debated] == ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    blocked = next(c for c in quant if c["ticker"] == "FFF")
    assert blocked["intent_blocked"] == "SECTOR_CAP" and blocked["fill_price"] is None


def test_structured_block_reason_from_settlement_is_used_first():
    """실행기가 `settlement['settled']` 의 SECTOR_CAP 을 넘기면 문구 파싱 없이 그 사유를 쓴다."""
    records, executions = _six_tech_records()
    sid = records[-1]["signal_id"]
    plain = debate_mod._account_candidates({}, records, {sid: {"at": FILL_AT,
                                                             "quant_track": {"execution_event": "no_action"}}},
                                           policy="quant_only", intent_blocked={sid: "SECTOR_CAP"})
    assert [c["ticker"] for c in plain] == ["FFF"]
    # 다른 사유(시장·범위)는 모집단이 아니다 — 두 계좌 모두 대상이 아니다.
    others = debate_mod._account_candidates({}, records, executions, policy="quant_only",
                                            intent_blocked={sid: "BLOCKED"})
    assert [c["ticker"] for c in others] == ["AAA", "BBB", "CCC", "DDD", "EEE"]


def test_intent_without_ledger_decision_is_not_a_candidate():
    """의도만 있고 원장이 판정한 이벤트가 없는 주문은 후보가 아니다(기존 계약 유지)."""
    record = record_of()
    assert debate_mod._account_candidates({}, [record], {}, policy="quant_only") == []


def test_same_day_entries_cannot_exceed_sector_cap():
    """6×5% 도 2×20% 도 25% 를 넘지 않는다 — 신규 진입일 `mark=0` 으로 0원 평가되던 구멍."""
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    panels = {t: panel_ohlc([100.0] * 4, [121.0] * 4, [99.0] * 4, [120.0] * 4, ticker=t)
              for t in tickers}
    six = debate_mod.simulate_policy_account([candidate(t) for t in tickers], panels,
                                             date(2026, 5, 15), initial_cash=100_000.0, costs=FREE,
                                             max_position=0.05, max_sector=0.25)
    # 종가 120(진입 100) → 평가액 초과분 = 투입액 × 0.2
    spent = (six["equity_curve"][0]["equity"] - 100_000.0) / 0.2
    assert six["n_trades"] == 5, "6번째 주문이 25% 캡을 뚫었다"
    assert spent == pytest.approx(25_000.0, abs=0.5)
    two = debate_mod.simulate_policy_account([candidate(tickers[0], weight=0.20),
                                             candidate(tickers[1], weight=0.20)],
                                            panels, date(2026, 5, 15), initial_cash=100_000.0,
                                            costs=FREE, max_position=0.20, max_sector=0.25)
    spent_two = (two["equity_curve"][0]["equity"] - 100_000.0) / 0.2
    assert spent_two == pytest.approx(25_000.0, abs=0.5), "2×20% 가 25% 캡을 뚫었다"


def test_sector_cap_accounts_for_preexisting_holdings():
    """기존 보유(다른 날 진입)가 있으면 그 섹터 신규 진입 여유는 25% 에서 그만큼 줄어든다."""
    panels = {"AAA": panel_ohlc([100.0, 100.0, 100.0, 100.0], [101.0, 121.0, 121.0, 121.0],
                                [99.0, 99.0, 119.0, 119.0], [100.0, 120.0, 120.0, 120.0],
                                ticker="AAA")}
    plans = [candidate("AAA", weight=0.20),
             candidate("AAA", weight=0.20, entry_day=date(2026, 5, 12))]
    result = debate_mod.simulate_policy_account(plans, panels, date(2026, 5, 15),
                                                initial_cash=100_000.0, costs=FREE,
                                                max_position=0.20, max_sector=0.25)
    # 1일차 20,000 · 2일차 여유 5,000 (합계 25,000 = 25%) → 250주 × 120 = 30,000
    assert result["n_trades"] == 2
    assert result["equity_curve"][1]["equity"] == pytest.approx(105_000.0, abs=0.5)


def test_gap_stop_runs_before_new_entry():
    """갭 손절이 **진입보다 먼저** 현금을 만든다 — 전일 종가로 부풀린 주문 크기를 쓰지 않는다."""
    panels = {
        "OLD": panel_ohlc([100.0, 80.0, 80.0, 80.0], [101.0, 81.0, 81.0, 81.0],
                          [99.0, 79.0, 79.0, 79.0], [100.0, 80.0, 80.0, 80.0], ticker="OLD"),
        "NEW": panel_ohlc([100.0, 100.0, 120.0, 120.0], [101.0, 101.0, 121.0, 121.0],
                          [99.0, 99.0, 119.0, 119.0], [100.0, 120.0, 120.0, 120.0], ticker="NEW"),
    }
    plans = [{"signal_id": "old", "ticker": "OLD", "sector": "A", "entry_day": date(2026, 5, 11),
              "weight": 0.5, "fill_price": 100.0, "stop_price": 90.0},
             {"signal_id": "new", "ticker": "NEW", "sector": "B", "entry_day": date(2026, 5, 12),
              "weight": 0.5, "fill_price": 100.0, "stop_price": 50.0}]
    result = debate_mod.simulate_policy_account(plans, panels, date(2026, 5, 15),
                                                initial_cash=100_000.0, costs=FREE,
                                                max_position=0.5, max_sector=1.0)
    # 2일차: 갭 손절 40,000 회수 → 현금 90,000 → 신규 45,000 × 1.2 = 54,000 → 자산 99,000
    assert result["n_trades"] == 3
    assert result["equity_curve"][1]["equity"] == pytest.approx(99_000.0, abs=0.5)
    assert result["equity_curve"][1]["equity"] != pytest.approx(100_000.0, abs=0.5)


def _quarter_fixture() -> tuple[dict, list[dict], dict, dict]:
    q1 = record_of(sid="2026-02-02-AAA-BUY-001", ticker="AAA", as_of="2026-02-02",
                   target_session="2026-02-03", published="2026-02-02T15:00:00Z",
                   order_cutoff="2026-02-03T14:25:00Z", data_cutoff="2026-01-30T21:00:00Z")
    q2 = record_of(sid="2026-04-06-BBB-BUY-001", ticker="BBB", as_of="2026-04-06",
                   target_session="2026-04-07", published="2026-04-06T15:00:00Z",
                   order_cutoff="2026-04-07T13:25:00Z", data_cutoff="2026-04-02T19:00:00Z")
    executions = {
        q1["signal_id"]: execution_event(signal_id=q1["signal_id"], snapshot_id=SNAP,
                                         at="2026-02-03T14:30:00Z", execution_event="buy_filled",
                                         tranche=1, fill_price=100.0, weight=0.05, stop_price=90.0),
        q2["signal_id"]: execution_event(signal_id=q2["signal_id"], snapshot_id=SNAP,
                                         at="2026-04-07T13:30:00Z", execution_event="buy_filled",
                                         tranche=1, fill_price=100.0, weight=0.05, stop_price=90.0),
    }
    panels = {
        "AAA": panel_of([100.0] * 5 + [120.0] * 5, start="2026-02-02", ticker="AAA"),
        "BBB": panel_of([100.0] * 5 + [60.0] * 5, start="2026-04-06", ticker="BBB"),
    }
    by_signal = {q1["signal_id"]: view_of(q1, execution=executions[q1["signal_id"]]),
                 q2["signal_id"]: view_of(q2, execution=executions[q2["signal_id"]])}
    return by_signal, [q1, q2], executions, panels


def test_two_accounts_execute_the_common_intent_independently():
    """A 가 섹터 캡에 막힌 6번째 주문을, 여유가 있는 B 는 산다 — 모집단이 갈라지지 않는 이유."""
    # tranche_weight = plan_weight × 0.4 → 5% 한도를 만들려면 plan_weight 0.125
    records, executions = _six_tech_records(plan_weight=0.125)
    by_signal = {records[0]["signal_id"]: view_of(records[0], execution=executions[records[0]["signal_id"]])}
    # AAA 만 토론이 보류(divergent) → B 는 AAA 를 사지 않아 Tech 여유가 5% 남는다.
    panels = {}
    for record in records:
        ticker = record["ticker"]
        panels[ticker] = panel_of([100.0, 200.0 if ticker == "FFF" else 100.0,
                                   200.0 if ticker == "FFF" else 100.0,
                                   200.0 if ticker == "FFF" else 100.0],
                                  start="2026-05-11", ticker=ticker)
    comp = debate_mod.compute_debate_comparison(
        by_signal, records, executions, panels, date(2026, 5, 15), costs=FREE,
        params={"risk": {"max_position": 0.05, "max_sector": 0.25}}
    )
    quant, debated = comp["accounts"]["quant_only"], comp["accounts"]["codex_debated"]
    assert quant["n_candidates"] == 6 and debated["n_candidates"] == 5      # B 는 보류한 AAA 하나만 뺀다
    # A: 5건 × 5% = 25% → FFF 없음(수익률 0) · B: AAA 하나 빼서 FFF 를 5% 사서 +100%
    assert quant["final_equity"] == pytest.approx(100_000.0, abs=0.5)
    assert debated["final_equity"] == pytest.approx(105_000.0, abs=0.5)
    assert comp["policy_diff_pp"] == pytest.approx(5.0, abs=0.01)


def test_quarter_account_window_is_fixed_to_that_quarter():
    """Q1 점수는 4월(Q2) 체결에 좌우되지 않는다 — 창은 `min(as_of, 분기말)` 이다."""
    by_signal, records, executions, panels = _quarter_fixture()
    params = {"risk": {"max_position": 0.05, "max_sector": 0.25}}
    comp = debate_mod.compute_debate_comparison(by_signal, records, executions, panels,
                                                date(2026, 6, 30), params=params)
    rows = {row["quarter"]: row for row in comp["quarters"]}
    assert set(rows) == {"2026Q1", "2026Q2"}
    assert rows["2026Q1"]["eval_day"] == "2026-03-31"
    assert rows["2026Q1"]["window"]["capped_at"] == "2026-03-31"
    assert rows["2026Q1"]["window"]["n_records"] == 1, "다른 분기 레코드가 Q1 창에 들어왔다"
    q1_only = debate_mod.policy_accounts({records[0]["signal_id"]: by_signal[records[0]["signal_id"]]},
                                         [records[0]], executions, panels, date(2026, 3, 31),
                                         params=params)
    assert rows["2026Q1"]["policy_diff"]["policy_diff_pp"] == pytest.approx(q1_only["policy_diff_pp"])
    # 두 분기를 모두 넣으면 4월 체결이 섞여 다른 숫자가 된다 — 그 혼입을 막은 것이 이 수정이다.
    mixed = debate_mod.policy_accounts(by_signal, records, executions, panels, date(2026, 6, 30),
                                       params=params)
    assert mixed["R_quant"] != pytest.approx(rows["2026Q1"]["policy_diff"]["policy_diff_pp"] / 100.0)


def test_quarter_row_carries_its_window_metadata():
    by_signal, records, executions, panels = _quarter_fixture()
    comp = debate_mod.compute_debate_comparison(by_signal, records, executions, panels,
                                                date(2026, 6, 30))
    for row in comp["quarters"]:
        assert row["window"]["start"] <= row["window"]["end"]
        assert row["window"]["capped_at"] == row["eval_day"]
