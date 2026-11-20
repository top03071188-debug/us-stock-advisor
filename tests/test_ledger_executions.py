"""test_ledger_executions — 체결 이벤트와 재고정(`.reanchor.json`)의 분리 (R01).

`data/ledger/executions/` 에는 두 종류가 섞인다:
`{signal_id}.json`(체결 이벤트)과 `{signal_id}.reanchor.json`(분할가 재고정). 정렬상 재고정이
뒤에 오기 때문에 예전 로더는 체결을 덮어써 **재시작 후 계좌가 0주**가 됐다. 이 파일은 로더·
정산·재생이 그 두 파일을 절대 섞지 않는지 고정한다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from engine.backtest.execution import build_entry_band
from engine.data.prices import build_price_frame
from engine.decide.signals import target_band
from engine.ledger import live
from engine.ledger.record import execution_event, publish_record, reanchor_event


def panel(ticker: str = "MSFT", *, days: int = 12, start: str = "2026-05-11",
          price: float = 100.0) -> object:
    idx = pd.bdate_range(start=start, periods=days)
    frame = pd.DataFrame({
        "Open": price, "High": price * 1.01, "Low": price * 0.99, "Close": price,
        "Adj Close": price, "Volume": 1_000_000.0,
    }, index=idx)
    return build_price_frame(ticker, frame, None)


def record(*, signal_id: str = "2026-05-15-MSFT-BUY-001", target_session: str | None = "2026-05-18",
           plan_weight: float = 0.02, stop_distance: float = 0.09) -> dict:
    rec = publish_record(
        signal_id=signal_id, ticker="MSFT", type="BUY", grade="B", entry_score=69.4,
        as_of="2026-05-15", data_cutoff_at="2026-05-14T22:00:00Z",
        signal_published_at="2026-05-14T22:02:11Z", order_cutoff_at="2026-05-15T13:25:00Z",
        generated_price=100.0, target_band=target_band(140.0, 100.0), expected_mdd=-0.09,
        thesis_ko="테스트", plan_weight=plan_weight, stop_distance=stop_distance,
        target_session=target_session,
    )
    # `entry_band` 는 pipeline 이 계획에서 붙이는 값이다 — 여기서는 장부만 검증하므로 직접 넣는다.
    rec["entry_band"] = build_entry_band(
        iv_median_sadj=140.0, required_margin=0.20, dma200_sadj=90.0,
        signal_price_sadj=100.0, stop_distance=stop_distance,
    ).as_dict()
    return rec


def fill_event(sid: str = "2026-05-15-MSFT-BUY-001", fill: float = 100.0) -> dict:
    return execution_event(
        signal_id=sid, snapshot_id="sha256:abc",
        at=datetime(2026, 5, 18, 13, 30, tzinfo=timezone.utc),
        execution_event="buy_filled", fill_price=fill, weight=0.02, stop_price=91.0,
    )


def write_dirs(tmp_path, sid: str):
    execs = tmp_path / "executions"
    execs.mkdir(parents=True, exist_ok=True)
    (execs / f"{sid}.json").write_text(json.dumps(fill_event(sid)), encoding="utf-8")
    reanchor = reanchor_event(
        signal_id=sid, snapshot_id="sha256:abc",
        at=datetime(2026, 5, 18, 13, 30, tzinfo=timezone.utc), first_fill=100.0, stop_price=91.0,
        plan=[{"tranche": 1, "price_ref": 100.0}, {"tranche": 2, "price_ref": 93.0},
              {"tranche": 3, "price_ref": 86.0, "active": False}],
    )
    (execs / f"{sid}.reanchor.json").write_text(json.dumps(reanchor), encoding="utf-8")
    return execs


# ── 로더 ─────────────────────────────────────────────────────────────────────
def test_reanchor_does_not_override_execution(tmp_path):
    sid = "2026-05-15-MSFT-BUY-001"
    execs = write_dirs(tmp_path, sid)
    loaded = live.load_executions(execs)
    assert list(loaded) == [sid]
    assert loaded[sid]["quant_track"]["execution_event"] == "buy_filled"

    rec = record()
    state = live.replay([rec], loaded, live.load_reanchors(execs))
    # 체결 이벤트가 weight 0.02 · fill 100 을 말한다 → $980,000 · 200주 (감사 재현 시나리오)
    assert state["cash"] == pytest.approx(980_000.0)
    assert state["positions"]["MSFT"]["qty"] == pytest.approx(200.0)
    assert state["positions"]["MSFT"]["avg_price"] == 100.0
    assert state["positions"]["MSFT"]["stop_price"] == 91.0
    # 재고정은 표시용으로만 붙는다(손익에 영향 없음)
    assert state["plan_reanchored"][sid]["first_fill"] == 100.0


def test_reanchor_only_directory_has_no_executions(tmp_path):
    sid = "2026-05-15-MSFT-BUY-001"
    execs = tmp_path / "executions"
    execs.mkdir(parents=True, exist_ok=True)
    reanchor = reanchor_event(
        signal_id=sid, snapshot_id="sha256:abc",
        at=datetime(2026, 5, 18, 13, 30, tzinfo=timezone.utc), first_fill=100.0, stop_price=91.0,
        plan=[{"tranche": 1, "price_ref": 100.0}],
    )
    (execs / f"{sid}.reanchor.json").write_text(json.dumps(reanchor), encoding="utf-8")
    assert live.load_executions(execs) == {}
    assert list(live.load_reanchors(execs)) == [sid]
    assert live.replay([record()], {})["cash"] == live.INITIAL_CASH


def test_unknown_event_file_is_ignored(tmp_path):
    execs = tmp_path / "executions"
    execs.mkdir(parents=True, exist_ok=True)
    (execs / "mystery.json").write_text(json.dumps({
        "signal_id": "mystery", "execution_event": "mystery",
        "quant_track": {"execution_event": "mystery"},
    }), encoding="utf-8")
    assert live.load_executions(execs) == {}
    # 타입이 맞는 파일만 읽는다
    sid = "2026-05-15-MSFT-BUY-001"
    (execs / f"{sid}.json").write_text(json.dumps(fill_event(sid)), encoding="utf-8")
    assert list(live.load_executions(execs)) == [sid]


# ── 정산 멱등성(프로세스 재시작) ───────────────────────────────────────────────
def test_settle_is_idempotent_across_process_restart(tmp_path):
    execs = tmp_path / "executions"
    panels = {"MSFT": panel(days=12, start="2026-05-11")}
    rec = record(target_session="2026-05-18")

    first = live.settle([rec], {}, panels, date(2026, 5, 18))
    assert [e["signal_id"] for e in first["new_events"]] == [rec["signal_id"]]
    for event in first["new_events"]:
        live.write_execution(event, execs)
    for event in first["reanchors"]:
        live.write_reanchor(event, execs)

    # 새 프로세스처럼 디스크에서 다시 읽는다
    executions = live.load_executions(execs)
    reanchors = live.load_reanchors(execs)
    state_before = live.replay([rec], executions, reanchors)

    second = live.settle([rec], executions, panels, date(2026, 5, 19))
    assert second["new_events"] == [] and second["reanchors"] == []
    assert second["pending"] == []
    for event in second["new_events"]:                    # 방어: 있다면 append-only 가 터진다
        live.write_execution(event, execs)

    state_after = live.replay([rec], live.load_executions(execs), live.load_reanchors(execs))
    assert state_after["cash"] == state_before["cash"]
    assert state_after["positions"]["MSFT"]["qty"] == state_before["positions"]["MSFT"]["qty"]
