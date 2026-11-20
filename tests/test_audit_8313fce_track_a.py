"""test_audit_8313fce_track_a — 감사 01·02·07·09·10 (계보·시점·채점·보정) 고정 테스트.

각 항목의 **감사 재현 → 수정 계약**을 그대로 검증한다.

- 01: 라이브도 마감 빈티지 점조회 + `run_started_at` / `signal_published_at` 분리
- 02: 발행 회차 ID(`supersedes`) · 스냅샷 최종 입력 고정 · 재생이 그 값을 읽는다
- 07: SEC 보수적 접수 시각 폴백 공통 함수(`sec_conservative_available_at`)
- 09: 과거 컷오프 분할 재기준화 · 컷오프 시총 · 체결가·만기 후 체결 채점
- 10: 보정 수락 계약(키 매칭·버전·표본) · `planned` 라벨 · 평면 키 → 엔진 설정 매핑
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from engine.backtest.calibration import (FoldObservation, align_observations, calibrate,
                                         check_observation_contract, map_candidate_to_params)
from engine.backtest.replay import NOTE_INPUTS_UNPINNED, replay
from engine.backtest.snapshot import inputs_fingerprint, load_snapshot
from engine.data import fixtures
from engine.data.collect import cutoff_market_cap
from engine.data.prices import build_price_frame
from engine.data.xbrl_fundamentals import split_events_from_panel
from engine.decide.pipeline import build_payload, load_params
from engine.decide.signals import EntryContext, evaluate_entry
from engine.ledger.grade import grade_record
from engine.ledger.record import resolve_release
from engine.ledger.record import execution_event as make_execution
from engine.ledger.record import publish_record
from engine.utils.dates import add_trading_days, is_trading_day, sec_conservative_available_at
from scripts import run_daily

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-20"
HORIZONS = {"3m": 63}


# ────────────────────────────── 공통 도우미 ──────────────────────────────
def ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def nyse_index(start: date, days: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < days:
        if is_trading_day(day):
            out.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return out


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    """`run_daily --offline` 를 돌릴 수 있는 최소 저장소 루트."""
    for name in ("config", "schemas"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "redact_quotes.py", tmp_path / "scripts" / "redact_quotes.py")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_daily(monkeypatch, as_of: str = AS_OF) -> int:
    monkeypatch.setenv("GITHUB_OUTPUT", str(Path("gh_output.txt").resolve()))
    return run_daily.main(["--mode", "postclose", "--offline", "--as-of", as_of, "--root", "."])


def _payload() -> dict:
    return json.loads(Path("data/quant_signals.json").read_text(encoding="utf-8"))


def _records() -> list[dict]:
    return [json.loads(line) for line in
            Path("data/ledger/records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeClock(datetime):
    """`run_daily.datetime` 을 대체하는 시계 — 첫 호출(배치 시작)과 이후(발행)를 나눈다."""

    calls: list[datetime] = []

    @classmethod
    def now(cls, tz=None):  # noqa: D102 — datetime API
        value = cls.calls.pop(0) if len(cls.calls) > 1 else cls.calls[0]
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


# ────────────────────────────── 01: 발행 시각 ──────────────────────────────
def test_signal_published_at_is_measured_at_publication_not_run_start(workdir, monkeypatch):
    """배치가 14:10Z 에 시작해 14:30Z 에 공개되면 — 발행은 14:30Z 이고 세션은 다음 세션이다."""
    FakeClock.calls = [ts("2026-11-20T14:10:00Z"), ts("2026-11-20T14:30:00Z")]
    monkeypatch.setattr(run_daily, "datetime", FakeClock)
    assert _run_daily(monkeypatch) == 0
    payload = _payload()

    assert payload["run_started_at"] == "2026-11-20T14:10:00Z"
    assert payload["signal_published_at"] == "2026-11-20T14:30:00Z"
    assert payload["signal_published_planned_at"] == "2026-11-19T22:02:11Z"
    # 14:25Z 주문 마감을 넘겼으므로 11-20 세션이 아니라 다음 거래일(월)로 넘어간다
    assert payload["target_session"] == "2026-11-23"
    assert ts(payload["data_cutoff_at"]) <= ts(payload["signal_published_at"]) < ts(payload["order_cutoff_at"])
    for record in _records():
        assert record["signal_published_at"] == "2026-11-20T14:30:00Z"
        assert record["target_session"] == "2026-11-23"


def test_run_started_at_and_published_at_are_separate_fields(workdir, monkeypatch):
    FakeClock.calls = [ts("2026-11-20T02:30:00Z"), ts("2026-11-20T02:52:00Z")]
    monkeypatch.setattr(run_daily, "datetime", FakeClock)
    assert _run_daily(monkeypatch) == 0
    payload = _payload()
    assert payload["run_started_at"] == "2026-11-20T02:30:00Z"
    assert payload["signal_published_at"] == "2026-11-20T02:52:00Z"     # 수집에 걸린 22분이 드러난다
    assert payload["target_session"] == "2026-11-20"
    assert payload["publish_lag_seconds"] == pytest.approx(0.0)
    assert payload["published_after_order_cutoff"] is False


# ────────────────────────────── 02: 발행 회차 ──────────────────────────────
def _record(inputs_hash: str, signal_id: str = "2026-11-20-FIXA-WATCH-001", **kw) -> dict:
    base = dict(
        signal_id=signal_id, ticker="FIXA", type="WATCH", grade="B", entry_score=61.0,
        as_of="2026-11-20", data_cutoff_at="2026-11-19T22:00:00Z",
        signal_published_at="2026-11-20T02:34:58Z", order_cutoff_at="2026-11-20T14:25:00Z",
        generated_price=100.0, target_band={"lo": 105.0, "hi": 110.0, "horizon_td": 63,
                                            "base_price": 100.0},
        expected_mdd=-0.09, plan_weight=0.0, stop_distance=0.0,
        target_session="2026-11-20", inputs_hash=inputs_hash, snapshot_id=f"sha256:{inputs_hash}",
    )
    base.update(kw)
    return publish_record(**base)


def test_resolve_release_reuses_same_input_fingerprint():
    first = _record("aaa")
    duplicate = resolve_release(_record("aaa", signal_published_at="2026-11-20T05:00:00Z"), [first])
    assert duplicate["duplicate"] is True
    assert duplicate["signal_id"] == first["signal_id"]          # 같은 발행 — 새 ID 를 만들지 않는다
    assert duplicate["supersedes"] is None

    changed = resolve_release(_record("bbb"), [first])
    assert changed["duplicate"] is False
    assert changed["signal_id"] == "2026-11-20-FIXA-WATCH-002"   # 다른 입력 → 새 회차
    assert changed["supersedes"] == first["signal_id"]
    assert changed["revision"] == 2

    third = resolve_release(_record("ccc"), [first, _record("bbb", "2026-11-20-FIXA-WATCH-002")])
    assert third["signal_id"] == "2026-11-20-FIXA-WATCH-003"
    assert third["supersedes"] == "2026-11-20-FIXA-WATCH-002"


def test_rerun_with_same_inputs_reuses_release_and_snapshot(workdir, monkeypatch):
    """같은 입력으로 다시 돌리면 — 원장은 그대로, 화면은 **원장에 기록된 발행본 그대로** 나간다."""
    assert _run_daily(monkeypatch) == 0
    first_records = _records()
    first_payload = _payload()
    assert not (first_payload.get("diagnostics") or {}).get("record_skips"), "첫 실행에 거부된 기록이 있다"

    assert _run_daily(monkeypatch) == 0
    second_payload = _payload()
    records = _records()
    assert len(records) == len(first_records), "같은 입력의 재실행이 원장을 늘렸다"
    assert (second_payload.get("diagnostics") or {}).get("record_skips") in (None, [])
    assert second_payload["diagnostics"]["record_duplicates"], "중복(재사용) 사실이 기록되지 않았다"
    assert second_payload["snapshot_id"] == first_payload["snapshot_id"], "같은 입력인데 스냅샷이 바뀌었다"
    assert second_payload["diagnostics"]["snapshot"]["state"] == "reused"
    by_id = {r["signal_id"]: r for r in records}
    for signal in second_payload["signals"]:
        assert signal == by_id[signal["signal_id"]], "화면 신호가 원장 발행본과 다르다"


def test_late_rerun_with_same_data_rolls_to_new_session_revision(workdir, monkeypatch):
    """같은 데이터라도 주문 대상 세션이 다르면 **다른 발행**이다 — 같은 날 늦은 재실행(감사 01·02)."""
    FakeClock.calls = [ts("2026-11-20T02:30:00Z"), ts("2026-11-20T02:34:00Z")]
    monkeypatch.setattr(run_daily, "datetime", FakeClock)
    assert _run_daily(monkeypatch) == 0
    first = _payload()
    assert first["target_session"] == "2026-11-20"
    first_ids = {record["signal_id"] for record in _records()}
    assert first_ids and all(sid.endswith("-001") for sid in first_ids)

    FakeClock.calls = [ts("2026-11-20T15:00:00Z"), ts("2026-11-20T15:02:00Z")]
    assert _run_daily(monkeypatch) == 0
    second = _payload()
    assert second["target_session"] == "2026-11-23"          # 14:25Z 주문 마감 이후 실행
    by_id = {record["signal_id"]: record for record in _records()}
    for signal in second["signals"]:
        assert signal["signal_id"].endswith("-002")
        assert signal["supersedes"] in first_ids
        assert by_id[signal["signal_id"]] == signal
    assert Path("data/snapshots/2026-11-20-r2/manifest.json").exists()


def test_changed_params_create_new_revision_and_keep_old_snapshot(workdir, monkeypatch):
    assert _run_daily(monkeypatch) == 0
    first_records = _records()
    first_ids = {r["signal_id"] for r in first_records}
    first_snapshot = _payload()["snapshot_id"]

    params_path = Path("config/params_v001.yaml")
    text = params_path.read_text(encoding="utf-8")
    params_path.write_text(text.replace("  buy: 68", "  buy: 66"), encoding="utf-8")

    assert _run_daily(monkeypatch) == 0
    records = _records()
    new = [r for r in records if r["signal_id"] not in first_ids]
    assert new, "파라미터가 바뀌었는데 새 회차가 없다"
    for record in new:
        assert record["revision"] >= 2
        assert record["supersedes"] in first_ids
        assert record["signal_id"].endswith(f"-{record['revision']:03d}")
    payload = _payload()
    assert payload["snapshot_id"] != first_snapshot
    assert Path("data/snapshots/2026-11-20/manifest.json").exists(), "이전 스냅샷을 덮어썼다"
    assert Path("data/snapshots/2026-11-20-r2/manifest.json").exists(), "새 회차 스냅샷이 없다"
    by_id = {r["signal_id"]: r for r in records}
    for signal in payload["signals"]:
        assert signal == by_id[signal["signal_id"]]


def test_superseded_release_is_not_settled_or_graded(tmp_path):
    """대체된 이전 회차는 체결하지 않는다 — 같은 날 재실행이 같은 종목을 두 번 사지 않게(감사 02)."""
    from engine.ledger import live

    as_of = date(2026, 11, 20)
    panel = fixtures.dataset(as_of)["panels"]["FIXA"]
    first = _record("aaa", signal_id="2026-11-20-FIXA-BUY-001", type="BUY", quant_intent="pending_buy",
                    plan_weight=0.02, stop_distance=0.09)
    second = _record("bbb", signal_id="2026-11-20-FIXA-BUY-002", type="BUY", quant_intent="pending_buy",
                     plan_weight=0.02, stop_distance=0.09)
    second["supersedes"] = first["signal_id"]

    result = live.settle([first, second], {}, {"FIXA": panel}, as_of)
    ids = [event["signal_id"] for event in result["new_events"]]
    assert first["signal_id"] not in ids, "대체된 회차가 체결됐다 — 중복 매수 위험"
    assert any(row.get("signal_id") == first["signal_id"] and row.get("event") == "superseded"
               for row in result["settled"])
    # 최신 회차는 버려지지 않는다(세션 봉이 없으면 pending 으로 남는다)
    assert any(row.get("signal_id") == second["signal_id"] for row in result["pending"] + result["settled"])

    graded = live.grade_all([first, second], {}, {"FIXA": panel}, as_of)
    assert [g["signal_id"] for g in graded] == [second["signal_id"]]


def test_inputs_fingerprint_ignores_publish_time(tmp_path):
    frames = {"prices_FIXA": fixtures.dataset(date(2026, 11, 20))["panels"]["FIXA"].frame}
    inputs = {"macro": {"vix": 18.4}}
    a = inputs_fingerprint(frames, inputs, ts("2026-11-19T22:00:00Z"), "params_v001")
    b = inputs_fingerprint(frames, inputs, ts("2026-11-19T22:00:00Z"), "params_v001")
    c = inputs_fingerprint(frames, {**inputs, "macro": {"vix": 19.0}}, ts("2026-11-19T22:00:00Z"),
                           "params_v001")
    assert a == b and a != c


def test_replay_reads_pinned_watchlist_holdings_and_session(tmp_path):
    """재생이 스냅샷의 **최종 입력**(필터된 유니버스·보유·섹터비중·실제 세 시각)을 그대로 쓴다."""
    as_of = date(2026, 11, 20)
    data = fixtures.dataset(as_of)
    keep = ["FIXA", "FIXB", "FIXC"]
    data["universe"] = [row for row in data["universe"] if row["ticker"] in keep]
    held = [{"ticker": "FIXA", "sector": "Information Technology", "stop_price": 90.0}]
    sector_weights = {"Information Technology": 0.18}
    decision = {
        "mode": "postclose",
        "data_cutoff_at": "2026-11-19T22:00:00Z",
        "signal_published_at": "2026-11-20T15:00:00Z",
        "signal_published_planned_at": "2026-11-19T22:02:11Z",
        "order_cutoff_at": "2026-11-23T14:25:00Z",
        "target_session": "2026-11-23",
        "held": held,
        "sector_weights": sector_weights,
        "params": load_params(ROOT / "config" / "params_v001.yaml"),
        "params_version": "params_v001",
        "inputs_hash": "sha256:" + "a" * 64,
    }
    data["decision"] = decision
    frames = {f"prices_{t}": p.frame for t, p in data["panels"].items()}
    inputs = {k: v for k, v in data.items() if k != "panels"}
    snap = run_daily.make_snapshot(tmp_path / "snap", ts("2026-11-19T22:00:00Z"), frames,
                                   as_of=as_of.isoformat(), inputs=inputs, extra={"mode": "postclose"})
    result = replay(snap, None)

    assert result.inputs_pinned is True and result.note_ko is None
    assert result.target_session == "2026-11-23"                   # as_of(11-20)가 아니라 고정된 세션
    assert result.params_source == "snapshot"
    assert {row["ticker"] for row in result.watchlist} == set(keep)   # 8종목 전체가 아니다
    # 보유 종목은 B1(워치리스트 밖/보유)에서 막힌다 — 예전에는 held=[] 로 항상 통과했다
    graded = {s["ticker"]: s for s in result.signals}
    assert graded.get("FIXA", {}).get("trade_grade") != "BUY"
    assert any(check.get("ticker") == "FIXA" for check in result.position_checks), "보유 상태가 전달되지 않았다"
    assert replay(load_snapshot(snap.root), None).digest == result.digest


def test_replay_flags_legacy_snapshot_without_pinned_inputs(tmp_path):
    as_of = date(2026, 11, 20)
    data = fixtures.dataset(as_of)
    frames = {f"prices_{t}": p.frame for t, p in data["panels"].items()}
    inputs = {k: v for k, v in data.items() if k != "panels"}
    snap = run_daily.make_snapshot(tmp_path / "legacy", ts("2026-11-19T22:00:00Z"), frames,
                                   as_of=as_of.isoformat(), inputs=inputs, extra={"mode": "postclose"})
    result = replay(snap, load_params(ROOT / "config" / "params_v001.yaml"))
    assert result.inputs_pinned is False
    assert result.note_ko == NOTE_INPUTS_UNPINNED
    assert result.target_session == as_of.isoformat()


def test_build_payload_records_target_session_and_base_bar(tmp_path):
    as_of = date(2026, 11, 20)
    data = fixtures.dataset(as_of)
    times = {"data_cutoff_at": "2026-11-19T22:00:00Z",
             "signal_published_at": "2026-11-20T02:34:58Z",
             "order_cutoff_at": "2026-11-20T14:25:00Z",
             "target_session": "2026-11-20"}
    payload = build_payload(data, as_of=as_of, mode="postclose",
                            params=load_params(ROOT / "config" / "params_v001.yaml"),
                            params_version="params_v001", code_commit="test",
                            snapshot_id="sha256:test", status="sample", three_times=times)
    assert payload["signals"]
    for record in payload["signals"]:
        assert record["target_session"] == "2026-11-20"
        band = record["target_band"]
        assert band["return_base"] == "base_price"
        last_bar = data["panels"][record["ticker"]].frame.index[-1].date()
        assert band["base_bar_date"] == last_bar.isoformat()


# ────────────────────────────── 07: SEC 접수 시각 ──────────────────────────────
def test_sec_conservative_available_at_uses_acceptance_datetime():
    assert sec_conservative_available_at(date(2026, 11, 19), "2026-11-19T21:30:00.000Z") == \
        ts("2026-11-19T21:30:00Z")
    assert sec_conservative_available_at(date(2026, 11, 19), "2026-11-19T16:30:00.000-05:00") == \
        ts("2026-11-19T21:30:00Z")
    # 시간대가 없는 값은 ET 로 본다
    assert sec_conservative_available_at(date(2026, 11, 19), "2026-11-19T16:30:00") == \
        ts("2026-11-19T21:30:00Z")


def test_sec_conservative_available_at_falls_back_to_et_day_end():
    """접수 시각 누락 → **ET 하루 끝** (서머타임 자동 반영: EST 04:59:59Z / EDT 03:59:59Z)."""
    assert sec_conservative_available_at(date(2026, 11, 19)) == ts("2026-11-20T04:59:59Z")
    assert sec_conservative_available_at(date(2026, 7, 20)) == ts("2026-07-21T03:59:59Z")
    # UTC 23:59:59 로 두면 22:00Z 결정에 접수 22:30Z 공시가 새어 들어간다 — 그 값보다 늦다
    assert sec_conservative_available_at(date(2026, 11, 19)) > ts("2026-11-19T22:00:00Z")


# ────────────────────────────── 09: 가격·시총·채점 ──────────────────────────────
def _split_history(traded_pre: float = 100.0, traded_post: float = 50.0, split_at: str = "2026-11-05",
                   start: str = "2026-10-05", days: int = 25) -> tuple[pd.DataFrame, pd.Series]:
    """2:1 분할 이력 — Yahoo 규약대로 `Close` 는 **오늘 기준 조정가**(분할 후 기준)다.

    분할 전 실제 거래가는 `Close × 2` 이므로 `traded_pre = 100` 은 `Close = 50` 이다.
    """
    idx = pd.bdate_range(start=start, periods=days)
    close = pd.Series([traded_post] * len(idx), index=idx)          # 오늘 기준 조정가
    assert traded_pre == traded_post * 2.0
    hist = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                         "Adj Close": close, "Volume": 1_000_000.0}, index=idx)
    splits = pd.Series([2.0], index=pd.to_datetime([split_at]))
    return hist, splits


def test_cutoff_panel_removes_future_split():
    """컷오프 이후 분할은 **가격 기준에서 제거**된다 — raw == sadj == 100, split_factor == 1."""
    hist, splits = _split_history()
    panel = build_price_frame("SPL", hist, splits)
    before = panel.as_of_cutoff(ts("2026-11-04T22:00:00Z"), splits)
    last = before.frame.iloc[-1]
    assert last["split_factor"] == pytest.approx(1.0)
    assert last["raw_close"] == pytest.approx(100.0)
    assert last["sadj_close"] == pytest.approx(100.0)
    assert split_events_from_panel(before) == []          # 미래 분할 이벤트 누수 없음

    # 예전 경로(행만 자르기)는 오늘 기준 조정가를 그대로 남긴다 — 이 차이가 감사 09-a 다
    legacy = panel.as_of(ts("2026-11-04T22:00:00Z"))
    assert legacy.frame.iloc[-1]["sadj_close"] == pytest.approx(50.0)
    assert legacy.frame.iloc[-1]["raw_close"] == pytest.approx(100.0)

    # 컷오프가 분할 이후면 계수 1·이벤트 1건으로 정상 복원된다
    after = panel.as_of_cutoff(ts("2026-11-06T22:00:00Z"), splits)
    assert after.frame.iloc[-1]["split_factor"] == pytest.approx(1.0)
    assert [e["ex_date"].isoformat() for e in split_events_from_panel(after)] == ["2026-11-05"]


def test_cutoff_market_cap_prefers_cutoff_close_over_fast_info():
    from engine.metrics.fundamentals import AnnualFundamentals

    hist, splits = _split_history()
    panel = build_price_frame("SPL", hist, splits).as_of_cutoff(ts("2026-11-04T22:00:00Z"), splits)
    fundamentals = AnnualFundamentals(
        ebit=[1.0], revenue=[10.0], net_income=[1.0], ocf=[1.0], capex=[0.1],
        depreciation=[0.1], total_assets=[5.0], non_interest_liabilities=[1.0],
        excess_cash=[0.5], total_debt=[1.0], cash=[1.0], equity=[3.0],
        diluted_shares=[10.0], interest_expense=[0.1],
    )
    past, past_source = cutoff_market_cap(panel, fundamentals, 9.9e9, as_of=date(2026, 11, 4),
                                          today=date(2026, 11, 20))
    assert past_source == "cutoff_close_x_diluted_shares"
    assert past == pytest.approx(100.0 * 10.0 * 1e6)      # 컷오프 종가 × 컷오프 주식수

    live_today, today_source = cutoff_market_cap(panel, fundamentals, 9.9e9,
                                                 as_of=date(2026, 11, 20), today=date(2026, 11, 20))
    assert today_source == "fast_info_market_cap" and live_today == pytest.approx(9.9e9)


def _grade_record(fill_price: float | None, fill_day: date | None, as_of: date,
                  price: float = 100.0, open_price: float = 100.0) -> dict:
    record = publish_record(
        signal_id="t1", ticker="AAA", type="WATCH", grade="B", entry_score=60.0,
        as_of=as_of.isoformat(), data_cutoff_at=f"{as_of.isoformat()}T02:00:00Z",
        signal_published_at=f"{as_of.isoformat()}T02:02:11Z",
        order_cutoff_at=f"{as_of.isoformat()}T14:25:00Z", generated_price=price,
        target_band={"lo": price * 1.05, "hi": price * 1.1, "horizon_td": 63,
                     "base_price": price, "base_bar_date": as_of.isoformat()},
        expected_mdd=-0.09, target_session=as_of.isoformat(),
    )
    execution = None
    if fill_price is not None and fill_day is not None:
        execution = make_execution(
            signal_id="t1", snapshot_id="sha256:x",
            at=datetime.combine(fill_day, datetime.min.time(), tzinfo=timezone.utc) + pd.Timedelta(hours=14),
            execution_event="buy_filled", fill_price=fill_price, weight=0.02)
    days = [d for d in nyse_index(as_of, 64) if d != as_of][:63]
    bars = [{"date": as_of, "raw_open": price, "raw_high": price, "raw_low": price,
             "raw_close": price, "sadj_open": price, "sadj_close": price, "adj_close": price}]
    bars += [{"date": d, "raw_open": open_price, "raw_high": price, "raw_low": price,
              "raw_close": price, "sadj_open": open_price, "sadj_close": price,
              "adj_close": price} for d in days]
    return grade_record(record, execution, bars, benchmark_returns={"3m": 0.0},
                        horizons=HORIZONS)["horizons"]["3m"]


def test_fill_price_is_normalized_to_total_return():
    """시가 $100·체결 $101·이후 불변 → **−0.99%** (예전에는 시가를 써서 0.0%)."""
    as_of = date(2026, 1, 5)
    entry_day = [d for d in nyse_index(as_of, 2) if d != as_of][0]
    row = _grade_record(101.0, entry_day, as_of)
    assert row["complete"] is True
    assert row["entry_tr_source"] == "fill_price_tr_normalized"
    assert row["return"] == pytest.approx(-0.0099, abs=1e-4)
    assert row["entry"] == pytest.approx(101.0)


def test_post_maturity_fill_is_held():
    """만기일(또는 이후)에 체결된 건은 채점하지 않는다 — `maturity_unreached_or_post_maturity`."""
    as_of = date(2026, 1, 5)
    maturity = add_trading_days(as_of, 63)
    row = _grade_record(100.0, maturity, as_of)
    assert row["complete"] is False and row["matured"] is False
    assert row["reason"] == "maturity_unreached_or_post_maturity"
    assert row["post_maturity_fill"] is True
    assert row["agreement"] is None and row["return"] is None


def test_return_base_uses_pinned_base_bar():
    """`r_base` 는 `base_bar_date` 봉의 종가 기준 — 나중에 알게 된 `as_of` 종가가 아니다."""
    as_of = date(2026, 1, 5)
    entry_day = [d for d in nyse_index(as_of, 2) if d != as_of][0]
    row = _grade_record(100.0, entry_day, as_of)
    assert row["base_bar_date"] == as_of.isoformat()
    assert row["return_base"] == pytest.approx(row["end_price"] / row["base_price"] - 1, abs=1e-6)


# ────────────────────────────── 10: 보정 수락 계약 ──────────────────────────────
def _obs(fold_id: str, score: float = 60.0, **kw) -> FoldObservation:
    base = dict(maturity_date=f"2026-0{fold_id[-1]}-15", holdout_start="2026-08-19",
                signal_count=30, code_version="abc123", snapshot_id="sha256:s1", horizon_td=63)
    base.update(kw)
    return FoldObservation(fold_id, score, 0.05, -0.10, **base)


def test_observation_contract_rejects_bad_folds():
    with pytest.raises(ValueError, match="중복"):                 # 같은 키 두 번
        check_observation_contract([_obs("F1"), _obs("F1")])
    with pytest.raises(ValueError, match="홀드아웃 경계"):         # 만기 ≥ 홀드아웃 시작
        check_observation_contract([_obs("F1", maturity_date="2026-08-19")])
    with pytest.raises(ValueError, match="신호 부족"):             # 폴드당 신호 < 25
        check_observation_contract([_obs("F1", signal_count=24)])
    check_observation_contract([_obs("F1"), _obs("F2")])          # 정상은 통과


def test_align_observations_matches_by_key_not_position():
    base = [_obs("F1"), _obs("F2")]
    with pytest.raises(ValueError, match="키 불일치"):              # 누락 폴드
        align_observations(base, [_obs("F1")])
    with pytest.raises(ValueError, match="키 불일치"):              # 같은 ID·다른 만기
        align_observations(base, [_obs("F1"), _obs("F2", maturity_date="2026-09-15")])
    with pytest.raises(ValueError, match="코드 버전"):              # 코드 버전 불일치
        align_observations(base, [_obs("F1", code_version="zzz"), _obs("F2")])
    with pytest.raises(ValueError, match="스냅샷"):                 # 스냅샷 불일치
        align_observations(base, [_obs("F1", snapshot_id="sha256:other"), _obs("F2")])
    pairs = align_observations(base, [_obs("F2", 70.0), _obs("F1", 65.0)])   # 순서가 달라도 키로 짝
    assert [(a.fold_id, b.agreement_mean) for a, b in pairs] == [("F1", 65.0), ("F2", 70.0)]


def test_calibrate_rejects_candidate_with_duplicate_or_foreign_folds():
    base_params = {"w_valuation": 0.25, "w_timing": 0.20, "w_macro": 0.15}

    def runner(params: dict) -> list[FoldObservation]:
        lifted = abs(float(params.get("w_valuation", 0.25)) - 0.25) > 1e-12
        if lifted:
            return [_obs("F1", 65.0), _obs("F1", 70.0)]          # 같은 폴드를 두 번
        return [_obs("F1", 60.0), _obs("F2", 60.0), _obs("F3", 60.0)]

    record = calibrate(runner, base_params, min_folds=3, stress_pass=lambda _p: True)
    assert record["accepted"] is False
    assert any("중복" in c.get("error", "") for c in record["candidates"])


def test_calibrate_rejects_base_observations_that_break_the_holdout():
    record = calibrate([_obs("F1", maturity_date="2026-08-25"), _obs("F2"), _obs("F3")],
                       {"w_valuation": 0.25}, min_folds=3, stress_pass=lambda _p: True)
    assert record["accepted"] is False
    assert record["reason"].startswith("INVALID_FOLDS")


def test_map_candidate_to_params_matches_engine_keys():
    base = load_params(ROOT / "config" / "params_v001.yaml")
    mapped = map_candidate_to_params({"w_valuation": 0.30, "rsi_buy": 30.0}, base)
    assert mapped["entry_weights"]["valuation"] == pytest.approx(0.30)
    assert mapped["entry_gates"]["rsi_buy"] == pytest.approx(30.0)
    # 나머지 설정은 그대로 남는다(깊은 복사 — base 를 바꾸지 않는다)
    assert mapped["entry_weights"]["quality"] == pytest.approx(base["entry_weights"]["quality"])
    assert base["entry_weights"]["valuation"] == pytest.approx(0.25)

    with pytest.raises(ValueError, match="가중치 합"):             # 합 1 검사
        map_candidate_to_params({k: 0.4 for k in
                                 ("w_quality", "w_valuation", "w_timing", "w_macro", "w_growth")})
    with pytest.raises(ValueError, match="매핑되지 않는"):          # 오타·미지원 키
        map_candidate_to_params({"w_valuaton": 0.3})


def test_map_candidate_to_params_changes_engine_decision():
    """매핑된 후보가 **실제 게이트**를 바꾼다 — 평면 키를 그대로 넘기면 아무 일도 일어나지 않는다."""
    base = load_params(ROOT / "config" / "params_v001.yaml")
    ctx = EntryContext(ticker="AAA", raw_close=100.0, sadj_close=100.0, dma50=101.0, dma200=95.0,
                       dma200_slope=0.02, rsi14=30.0, pct_b=0.15, atr_pct=0.02, iv_median=140.0,
                       q=70.0, v=60.0, g=50.0, structural_risk=10.0, news_state="read_done",
                       evidence=[{"type": "fundamental"}, {"type": "sec_filing"}, {"type": "market"}],
                       in_watchlist=True)
    strict = evaluate_entry(ctx, "neutral", map_candidate_to_params({"rsi_buy": 20.0}, base))
    loose = evaluate_entry(ctx, "neutral", map_candidate_to_params({"rsi_buy": 35.0}, base))
    assert "B4" in strict["failed_gates"], "rsi_buy 를 낮췄는데 눌림 조건이 그대로 통과했다"
    assert "B4" not in loose["failed_gates"]


def test_min_margin_maps_to_regime_dict_and_keeps_spread():
    base = load_params(ROOT / "config" / "params_v001.yaml")
    mapped = map_candidate_to_params({"min_margin": 0.18}, base)
    margins = mapped["entry_gates"]["margin_of_safety"]
    assert set(margins) == {"risk_on", "neutral", "risk_off"}
    assert margins["neutral"] == pytest.approx(0.18)
    # base 의 레짐 간격(−5p·0·+5p)을 유지한다 — risk_off 가 가장 보수적이다
    assert margins["risk_on"] < margins["neutral"] < margins["risk_off"]
    assert margins["risk_off"] - margins["risk_on"] == pytest.approx(0.10)


def test_observations_from_fold_results_skip_truncated_folds():
    from engine.backtest.calibration import observations_from_fold_results
    from engine.backtest.walk_forward import FoldResult

    good = FoldResult("F1", 63, date(2025, 11, 19), date(2026, 2, 20), True, False, 63,
                      signals=[{"ticker": "AAA"}] * 30, agreement_mean=62.5, ic=0.11, mdd=-0.12)
    cut = FoldResult("F4", 63, date(2026, 5, 19), date(2026, 8, 19), False, True, 62,
                     signals=[{"ticker": "AAA"}], agreement_mean=60.0)
    obs = observations_from_fold_results([good, cut], holdout_start="2026-08-19",
                                         code_version="abc123", snapshot_id="sha256:s1")
    assert len(obs) == 1                                     # 잘린 폴드는 들어가지 않는다
    assert obs[0].key() == (63, "F1", "2026-02-20")
    assert obs[0].signal_count == 30 and obs[0].code_version == "abc123"
    check_observation_contract(obs)                          # 계약(신호 수·홀드아웃)을 통과한다
