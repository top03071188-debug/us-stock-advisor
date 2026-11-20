"""test_ledger — 발행 기록·체결 이벤트·채점 (기획서 4.7.1·4.7.2, Phase 2 DoD)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from engine.backtest.walk_forward import agreement_components
from engine.decide.signals import target_band
from engine.ledger.grade import (HORIZONS_TD, grade_record, realized_mdd, summarize_grades,
                                 weighted_agreement)
from engine.ledger.debate import debate_event as debate_event_of
from engine.ledger.record import (RecordError, append_immutable, assert_publish_record, execution_event,
                                 load_records, merged_view, publish_record, reanchor_event)
from engine.utils.dates import trading_days

CUT = "2026-05-14T22:00:00Z"
PUB = "2026-05-14T22:02:11Z"
OC = "2026-05-15T13:25:00Z"


def record(**kw) -> dict:
    base = dict(
        signal_id="2026-05-15-MSFT-BUY-001", ticker="MSFT", type="BUY", grade="B", entry_score=69.4,
        as_of="2026-05-15", data_cutoff_at=CUT, signal_published_at=PUB, order_cutoff_at=OC,
        generated_price=412.35, target_band=target_band(522.0, 412.35),
        expected_mdd=-0.09, thesis_ko="테스트", plan_weight=0.05, stop_distance=0.09,
        evidence=[{"type": "fundamental", "metric": "FCF_margin_5y", "value": 0.22}],
        investor_scores={"buffett": 62, "lynch": -18}, conflict=True,
        params_version="params_v001", snapshot_id="sha256:abc", code_commit="deadbee",
    )
    base.update(kw)
    return publish_record(**base)


def bars(start: date, days: int, price: float = 412.35, drift: float = 0.0012) -> list[dict]:
    out = []
    p = price
    for d in trading_days(start, start + timedelta(days=int(days * 1.6) + 10))[:days]:
        o = p
        p = p * (1 + drift)
        out.append({"date": d.isoformat(), "open": o, "high": max(o, p) * 1.002,
                    "low": min(o, p) * 0.998, "close": p})
    return out


# ── 발행 기록 불변성 ─────────────────────────────────────────────────────────
def test_publish_record_has_no_fill_information():
    r = record()
    assert r["execution_event"] is None
    assert r["position"]["stop_price"] is None
    assert r["position"]["planned_loss_after_fill_pct"] is None
    assert r["track"] == "quant_only" and r["debate_ref"] is None
    assert all(t["provisional"] is True for t in r["entry_plan"])
    text = json.dumps(r, ensure_ascii=False)
    for key in ("fill_price", "avg_cost", "slippage_bps", "shares"):
        assert key not in text
    assert r["position"]["tranche_weight"] == pytest.approx(0.05 * 0.40)


def test_publish_record_third_tranche_outside_stop_is_inactive():
    r = record()
    assert r["entry_plan"][2]["active"] is False
    assert "손절선" in r["entry_plan"][2]["note"]


def test_publish_record_rejects_bad_time_order():
    with pytest.raises(RecordError):
        record(signal_published_at="2026-05-14T21:00:00Z")     # 데이터 마감보다 이르다
    with pytest.raises(RecordError):
        record(order_cutoff_at="2026-05-14T22:00:00Z")         # 발행보다 이르다


def test_assert_publish_record_catches_prefilled_values():
    r = record()
    r["execution_event"] = "buy_filled"
    with pytest.raises(RecordError):
        assert_publish_record(r)
    r2 = record()
    r2["position"]["stop_price"] = 377.74
    with pytest.raises(RecordError):
        assert_publish_record(r2)
    r3 = record()
    r3["entry_plan"][0]["provisional"] = False
    with pytest.raises(RecordError):
        assert_publish_record(r3)


def test_append_immutable_rejects_edit(tmp_path):
    path = tmp_path / "records.jsonl"
    r = record()
    append_immutable(path, r)
    append_immutable(path, r)                       # 같은 내용은 무해
    assert len(load_records(path)) == 1
    changed = dict(r, entry_score=99.9)
    with pytest.raises(RecordError):
        append_immutable(path, changed)


def test_execution_event_rules():
    ev = execution_event(signal_id="s", snapshot_id="sha", at=datetime(2026, 5, 18, 13, 30, tzinfo=timezone.utc),
                         execution_event="buy_filled", fill_price=415.10, weight=0.02,
                         stop_price=377.74, planned_loss_pct=0.0018, shares=0.004818)
    assert ev["quant_track"]["fill_price"] == 415.10
    assert ev["quant_track"]["execution_event"] == "buy_filled"
    with pytest.raises(RecordError):
        execution_event(signal_id="s", snapshot_id="sha", at=datetime.now(timezone.utc),
                        execution_event="buy_filled")
    with pytest.raises(RecordError):
        execution_event(signal_id="s", snapshot_id="sha", at=datetime.now(timezone.utc),
                        execution_event="hold_cash", fill_price=415.10)
    hold = execution_event(signal_id="s", snapshot_id="sha", at=datetime.now(timezone.utc),
                           execution_event="hold_cash")
    assert hold["quant_track"]["fill_price"] is None


def test_reanchor_keeps_original_rows():
    r = record()
    ev = reanchor_event(signal_id=r["signal_id"], snapshot_id=r["snapshot_id"],
                        at=datetime(2026, 5, 15, 13, 30, tzinfo=timezone.utc),
                        first_fill=415.10, stop_price=377.74,
                        plan=[{"tranche": 2, "price_ref": 386.04}, {"tranche": 3, "price_ref": 356.99,
                                                                    "active": False}])
    assert ev["execution_event"] == "plan_reanchor"
    assert ev["first_fill"] == 415.1
    assert r["entry_plan"][1]["price_ref"] == round(412.35 * 0.93, 2)   # 원본 행 유지


def test_debate_event_appends_separately():
    """[v1.3.4 · P1-2] 편입 이벤트는 `engine.ledger.debate` 가 **직접 판정**해 만든다.

    예전 `record.debate_event()` 는 호출자가 넣은 `eligible`·`late` 를 그대로 기록했다 —
    호출자가 틀리면 마감 뒤 검증된 결과도 통과로 남는다. 새 경로는 인자를 받지 않고 시각 계약을
    다시 계산한다(그래서 `record.debate_event` 는 저장소에서 사라졌다).
    """
    r = record()
    report = {
        "report_id": "r1", "ticker": r["ticker"], "signal_id": r["signal_id"],
        "snapshot_id": r["snapshot_id"], "status": "complete", "intent": "pending_hold_cash",
        "completed_at": "2026-05-14T23:15:00Z", "order_cutoff_at": OC,
        "signal_published_at": PUB,
        "round3": {"chair": {"grade_recommendation": "WATCH"}},
    }
    ev = debate_event_of(report, r, at=datetime(2026, 5, 14, 23, 26, tzinfo=timezone.utc))
    assert ev["events"][0]["track"] == "codex_debated"
    assert ev["events"][0]["eligible"] is True and ev["events"][0]["late"] is False
    assert ev["events"][0]["report_validated_at"] == "2026-05-14T23:26:00Z"
    # 마감 뒤에 검증된 보고서는 통과하지 못한다 — 보고서의 "마감 전 완료" 주장만 믿지 않는다
    late = debate_event_of(report, r, at=datetime(2026, 5, 15, 15, 0, tzinfo=timezone.utc))
    assert late["events"][0]["eligible"] is False and late["events"][0]["late"] is True
    assert late["events"][0]["event"] == "debate_rejected"
    with pytest.raises((ImportError, AttributeError)):
        from engine.ledger import record as record_mod  # noqa: PLC0415 — 삭제된 구 경로 확인
        record_mod.debate_event


def test_target_band_defensive_flag_survives_serialization():
    """방어 목표(gap ≤ 0) 표시가 발행 레코드에서 유실되지 않는다(4.7.1).

    `target_band()` 는 `defensive` 를 계산해 두는데 직렬화가 버리면 화면·채점이
    "방어 목표인데 상승 목표처럼 보이는" 밴드를 읽게 된다.
    """
    defensive = target_band(300.0, 412.35)                  # IV < 가격 → gap 음수
    assert defensive["defensive"] is True
    r = record(target_band=defensive)
    assert r["target_band"]["defensive"] is True
    assert r["target_band"]["r_hi"] < 0                     # 방어 목표는 상승폭이 음수
    assert r["target_band"]["basis"] == "base_price"

    up = target_band(522.0, 412.35)
    assert up["defensive"] is False
    assert record(target_band=up)["target_band"]["defensive"] is False

    # 플래그를 모르는 밴드(구 스냅샷)는 키를 만들지 않는다 — 기본값을 지어내지 않는다.
    legacy = {k: v for k, v in up.items() if k != "defensive"}
    assert "defensive" not in record(target_band=legacy)["target_band"]


def test_merged_view_action_only_from_execution():
    r = record()
    before = merged_view(r, None)
    assert before["action"] is None and before["fill"] is None
    ex = execution_event(signal_id=r["signal_id"], snapshot_id=r["snapshot_id"],
                         at=datetime(2026, 5, 15, 13, 30, tzinfo=timezone.utc),
                         execution_event="buy_filled", fill_price=415.10, weight=0.02)
    after = merged_view(r, ex)
    assert after["action"] == "buy_filled" and after["fill"]["price"] == 415.10
    assert r["execution_event"] is None                     # 원본은 그대로


# ── 채점 ─────────────────────────────────────────────────────────────────────
def test_realized_mdd():
    assert realized_mdd([100, 120, 90, 110], 100) == pytest.approx(-0.25)
    assert realized_mdd([100]) is None


def test_grade_horizons_and_holdout_truncation():
    data = bars(date(2026, 5, 15), 200)
    r = record()
    ex = execution_event(signal_id=r["signal_id"], snapshot_id=r["snapshot_id"],
                         at=datetime(2026, 5, 15, 13, 30, tzinfo=timezone.utc),
                         execution_event="buy_filled", fill_price=415.10, weight=0.02)
    graded = grade_record(r, ex, data, benchmark_returns={"1m": 0.01, "3m": 0.03, "6m": 0.05},
                          holdout_start="2026-08-19")
    assert graded["basis"] == "fill" and graded["action"] == "buy_filled"
    assert set(graded["horizons"]) == set(HORIZONS_TD)
    one_month = graded["horizons"]["1m"]
    assert one_month["eligible"] is True and one_month["truncated"] is False
    assert one_month["maturity"] == add_maturity(date(2026, 5, 15), 21)
    assert one_month["excess_return"] == pytest.approx(one_month["return"] - 0.01, abs=1e-6)
    assert 0 <= one_month["agreement"]["total"] <= 100
    six_month = graded["horizons"]["6m"]
    assert six_month["eligible"] is False and six_month["truncated"] is True
    assert six_month["observed_days"] > 0
    assert six_month["agreement"] is None


def add_maturity(start: date, h: int) -> str:
    from engine.utils.dates import add_trading_days
    return add_trading_days(start, h).isoformat()


def test_grade_uses_virtual_fill_when_no_execution():
    data = bars(date(2026, 5, 15), 80)
    r = record()
    graded = grade_record(r, None, data, benchmark_returns={"1m": 0.0})
    assert graded["basis"] == "virtual_fill"
    assert graded["horizons"]["1m"]["entry"] == pytest.approx(412.35)
    assert graded["horizons"]["1m"]["virtual"] is True


def test_band_basis_is_base_price():
    """밴드는 base, 방향은 fill — 기준을 섞으면 점수가 부풀려진다(4.2.4)."""
    base, fill, end = 412.35, 415.10, 454.57
    fixed = agreement_components(1, base, fill, 431.5, 450.7, end, -0.09, -0.075, fill, 396.2, 470.0,
                                 benchmark_return=0.0)
    assert fixed["band"] == pytest.approx(79.8, abs=0.2)          # 부록 C 검산
    assert fixed["direction"] == 100.0
    # 기준을 fill 로 통일해도(밴드·수익률 모두 같은 달러 기준) 값이 같다 — 비율이 같기 때문
    fill_basis = agreement_components(1, fill, fill, 431.5, 450.7, end, -0.09, -0.075,
                                      fill, 396.2, 470.0, benchmark_return=0.0)
    assert fill_basis["band"] == pytest.approx(fixed["band"], abs=0.1)
    # 밴드만 fill 달러로 환산하고 수익률은 base 로 두면 95.5 로 부풀려진다 — 금지된 혼합
    half_converted = agreement_components(1, base, fill, 431.5 / base * fill, 450.7 / base * fill,
                                         end, -0.09, -0.075, fill, 396.2, 470.0, benchmark_return=0.0)
    assert half_converted["band"] == pytest.approx(95.5, abs=0.2)
    assert half_converted["band"] != fixed["band"]


def test_grade_record_keeps_band_on_base_price():
    """밴드는 base(신호일 종가) 기준, 수익률은 total_return_adj 기준 — 두 기준을 섞지 않는다."""
    data = bars(date(2026, 5, 15), 80)
    r = record()
    ex = execution_event(signal_id=r["signal_id"], snapshot_id=r["snapshot_id"],
                         at=datetime(2026, 5, 15, 13, 30, tzinfo=timezone.utc),
                         execution_event="buy_filled", fill_price=415.10, weight=0.02)
    graded = grade_record(r, ex, data, benchmark_returns={"1m": 0.0}, holdout_start="2027-01-01")
    h = graded["horizons"]["1m"]
    signal_close = float(data[0]["close"])
    assert h["return_basis"] == "total_return_adj"
    assert h["entry_tr_source"] == "fill_price_tr_normalized"   # 시가 대신 **체결가**를 총수익 기준으로 환산(감사 09-b)
    assert h["entry"] == pytest.approx(415.10)                       # 표시용 체결가(raw)
    assert h["return"] == pytest.approx(h["end_price"] / h["entry_tr"] - 1, abs=1e-6)
    assert h["return_base"] == pytest.approx(h["end_price"] / signal_close - 1, abs=1e-6)
    assert h["return_base"] != h["return"]


def test_grade_record_reports_unavailable_horizon_without_data():
    r = record()
    graded = grade_record(r, None, [], benchmark_returns={})
    assert graded["horizons"]["3m"]["agreement"] is None
    assert graded["horizons"]["3m"]["note"].startswith("가격 데이터 없음")


def test_weighted_agreement_and_summary():
    data = bars(date(2026, 5, 15), 120)
    rows = []
    for i, grade in enumerate(("A", "B", "C")):
        r = record(signal_id=f"s{i}", grade=grade)
        rows.append(grade_record(r, None, data, benchmark_returns={"3m": 0.02}, holdout_start="2027-01-01"))
    w = weighted_agreement(rows, "3m")
    assert w["n"] == 3 and w["weighted_agreement"] is not None
    s = summarize_grades(rows, "3m")
    assert s["n"] == 3 and 0 <= s["win_rate"] <= 1
    assert summarize_grades([], "3m")["n"] == 0            # 신호 0건도 정상


def test_summary_with_no_signals_is_not_failure():
    summary = summarize_grades([], "6m")
    assert summary["agreement_mean"] is None and summary["win_rate"] is None
