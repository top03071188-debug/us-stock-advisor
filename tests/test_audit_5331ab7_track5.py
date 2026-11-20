"""test_audit_5331ab7_track5 — 감사 5331ab7 중요 11·보통 12 고정 테스트.

검사하는 계약
- **[11] 알림 채널별 전달 장부와 재시도**: 발송 장부는 `(event_key, channel)` 단위이고, 콘솔이
  성공해도 실패한 웹훅은 `pending_retry` 로 남아 **다음 실행에서 다시 호출**된다. 이미 전달된
  채널은 재출력하지 않는다. 미설정 채널은 `unconfigured`(실패 아님)다. 재시도 전 "토론 실행 권장"은
  현재 편입 상태로 다시 판정해 무효면 `suppressed` 로 닫는다. 웹훅 POST 에는
  `X-Idempotency-Key: {event_key}` 가 붙고, 발송 **전**에 장부가 디스크에 내구 기록된다.
- **[12] 국면 표본 운영 공급**: 발행 원장(`records.jsonl`)의 발행 시점 국면 + `as_of` 이전 만기 +
  실제 관측 봉으로 국면별 표본 수(`n`)와 기준일(`matured_as_of`)을 세어 `collect()` → `build_payload()`
  로 넘긴다. 29건이면 B 상한(실제 수치 유지), 30건이면 같은 배선에서 A 경로가 열린다. 표본 근거가
  없으면 `None`(B 상한)이고, 만기가 `as_of` 이후인 신호는 표본이 아니다.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from pathlib import Path

import pytest  # noqa: F401 — 픽스처(`tmp_path`·`monkeypatch`)용
import yaml

from engine.data import collect as collect_mod
from engine.data import fixtures
from engine.data.collect import DEFAULT_LEDGER_RECORDS, regime_sample_counts
from engine.decide.pipeline import (
    _regime_sample_for,
    build_payload,
    confidence_grade,
    load_params,
)
from engine.models import validate_quant_signals
from engine.utils.dates import add_trading_days, prev_trading_day
from scripts import notify
from tests.conftest import make_history
from engine.data.prices import build_price_frame

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 11, 20)
TIMES = {"data_cutoff_at": "2026-11-19T22:00:00Z",
         "signal_published_at": "2026-11-20T02:34:58Z",
         "order_cutoff_at": "2026-11-20T14:25:00Z",
         "target_session": "2026-11-20"}

#: 5개 투표가 모두 risk_off 인 거시값(분류는 `engine.decide.macro`).
RISK_OFF = {"t10y2y": -0.55, "hy_oas_bp": 640.0, "vix": 31.0, "dtwexbg": 126.0,
            "dff": 4.10, "cpi_yoy": 2.00}
RISK_OFF_PREV = {"t10y2y": -0.10, "hy_oas_bp": 420.0, "vix": 20.0, "dtwexbg": 118.0,
                 "dff": 3.70, "cpi_yoy": 2.30}


# ────────────────────────── [11] 알림: 채널별 장부·재시도 ──────────────────────────
def _notify_payload(as_of: str = "2026-11-21") -> dict:
    return {"as_of": as_of, "status": "normal",
            "signals": [{"signal_id": f"{as_of}-AAA-BUY-001", "ticker": "AAA", "decision": "BUY",
                         "trade_grade": "STRONG_BUY", "entry_score": 88.0,
                         "quant_intent": "pending_buy"}],
            "position_checks": [], "blocked": [], "diagnostics": {}}


class Hook:
    """웹훅 대역 — `ok` 를 바꿔 실패→복구를 재현한다."""

    name = "webhook"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    def send(self, event: dict) -> dict:
        self.calls.append(event)
        return {"ok": self.ok, "detail": "HTTP 200" if self.ok else "HTTP 500"}


class CountingConsole:
    name = "console"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, event: dict) -> dict:
        self.calls.append(event)
        return {"ok": True, "detail": None}


def _log(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _key(kind: str, as_of: str = "2026-11-21") -> str:
    return f"{kind}:{as_of}-AAA-BUY-001"


def test_console_success_keeps_failed_webhook_in_retry_queue(tmp_path):
    """콘솔 성공이 실패한 웹훅을 `sent` 로 덮지 않고, 다음 실행이 웹훅을 재호출한다(감사 11)."""
    log = tmp_path / "log.json"
    webhook = Hook(ok=False)
    first = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                          channels=[notify.ConsoleChannel(), webhook])
    doc = _log(log)
    entry = doc["keys"][_key("strong_buy")]
    assert entry["channels"]["console"]["status"] == "sent"
    assert entry["channels"]["webhook"]["status"] == "pending_retry"
    assert entry["channels"]["webhook"]["attempts"] == 1
    assert {"event_key": _key("strong_buy"), "channel": "webhook"}.items() <= \
        next(r for r in first["retry_pending"] if r["channel"] == "webhook").items()
    assert first["channels"]["webhook"]["pending_retry"] >= 1

    webhook.ok = True                       # 다음 실행에서 웹훅이 복구됐다
    second = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                           channels=[notify.ConsoleChannel(), webhook])
    doc = _log(log)
    assert doc["keys"][_key("strong_buy")]["channels"]["webhook"]["status"] == "sent"
    assert doc["keys"][_key("strong_buy")]["channels"]["webhook"]["attempts"] == 2
    assert second["n_retried"] >= 1
    assert second["retry_pending"] == []
    # 이미 전달된 콘솔은 다시 출력하지 않는다 — 콘솔 발송 행은 이벤트당 1건뿐이다.
    console_rows = [r for r in doc["deliveries"]
                    if r["event_key"] == _key("strong_buy") and r["channel"] == "console"]
    assert len(console_rows) == 1 and console_rows[0]["status"] == "sent"


def test_retry_targets_only_channels_that_have_not_delivered(tmp_path):
    """재시도는 **전달되지 않은 채널**에만 간다 — 콘솔 재출력 0건(감사 11)."""
    log = tmp_path / "log.json"
    console, webhook = CountingConsole(), Hook(ok=False)
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[console, webhook])
    assert len(console.calls) == 2 and len(webhook.calls) == 2      # strong_buy + debate_recommended
    webhook.ok = True
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[console, webhook])
    assert len(console.calls) == 2          # 콘솔은 다시 부르지 않는다
    assert len(webhook.calls) == 4          # 실패했던 두 사건만 재시도


def test_channel_attempts_are_counted_per_channel(tmp_path):
    """채널별 시도 상한 — 웹훅은 3회에서 `failed` 로 닫히고, 콘솔 성공은 그대로 남는다(감사 11)."""
    log = tmp_path / "log.json"
    webhook = Hook(ok=False)
    for _ in range(notify.MAX_ATTEMPTS):
        notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                      channels=[notify.ConsoleChannel(), webhook])
    entry = _log(log)["keys"][_key("strong_buy")]
    assert entry["channels"]["webhook"]["status"] == "failed"
    assert entry["channels"]["webhook"]["attempts"] == notify.MAX_ATTEMPTS
    assert entry["channels"]["console"]["status"] == "sent"
    assert entry["channels"]["console"]["attempts"] == 1
    assert entry["status"] == "sent"          # 이벤트 자체는 콘솔로 전달됐다
    assert len(webhook.calls) == 2 * notify.MAX_ATTEMPTS
    # 상한을 넘긴 채널은 다시 부르지 않는다
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[notify.ConsoleChannel(), webhook])
    assert len(webhook.calls) == 2 * notify.MAX_ATTEMPTS


def test_unconfigured_channel_is_not_a_failure(tmp_path, monkeypatch):
    """설정이 없는 채널은 `unconfigured` — 실패·재시도로 세지 않는다(감사 11)."""
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)
    log = tmp_path / "log.json"
    summary = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                            channels=[notify.ConsoleChannel(), notify.WebhookChannel()])
    entry = _log(log)["keys"][_key("strong_buy")]
    assert entry["channels"]["webhook"]["status"] == "unconfigured"
    assert entry["channels"]["webhook"]["attempts"] == 0
    assert entry["status"] == "sent"
    assert summary["retry_pending"] == []
    assert summary["channels"]["webhook"]["pending_retry"] == 0


def test_retry_of_debate_recommendation_is_rejudged_before_resend(tmp_path):
    """재시도 전 재판정 — 그 사이 적격 토론이 편입된 후보 추천은 보내지 않고 `suppressed`(감사 11)."""
    log = tmp_path / "log.json"
    webhook = Hook(ok=False)
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[notify.ConsoleChannel(), webhook])
    assert _log(log)["keys"][_key("debate_recommended")]["channels"]["webhook"]["status"] == "pending_retry"
    webhook.ok = True
    debate = {"by_signal": {"2026-11-21-AAA-BUY-001": {"signal_id": "2026-11-21-AAA-BUY-001",
                                                       "ticker": "AAA", "eligible": True}}}
    calls_before = len(webhook.calls)
    summary = notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                            channels=[notify.ConsoleChannel(), webhook], debate=debate)
    doc = _log(log)
    assert doc["keys"][_key("debate_recommended")]["channels"]["webhook"]["status"] == "suppressed"
    assert summary["n_retry_suppressed"] == 1
    rejudged = [row for row in summary["suppressed"] if row.get("reason") == "eligible_debate_now"]
    assert [row["event_key"] for row in rejudged] == [_key("debate_recommended")]
    sent_keys = {e["event_key"] for e in webhook.calls[calls_before:]}
    assert _key("debate_recommended") not in sent_keys         # 무효가 된 추천은 재전송하지 않는다
    assert _key("strong_buy") in sent_keys                     # 다른 사건은 정상 재시도


def test_retry_rejudge_reads_debate_events_too(tmp_path):
    """`debate_events`(원장 이벤트) 만 넘어와도 같은 재판정을 한다(감사 11)."""
    log = tmp_path / "log.json"
    webhook = Hook(ok=False)
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[notify.ConsoleChannel(), webhook])
    webhook.ok = True
    events = {"2026-11-21-AAA-BUY-001": {"signal_id": "2026-11-21-AAA-BUY-001", "status": "eligible"}}
    calls_before = len(webhook.calls)
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[notify.ConsoleChannel(), webhook], debate_events=events)
    sent_keys = {e["event_key"] for e in webhook.calls[calls_before:]}
    assert _key("debate_recommended") not in sent_keys


def test_event_is_durable_in_log_before_external_send(tmp_path):
    """외부 발송 **전**에 장부가 디스크에 내구 기록된다(감사 11 — 재실행 중복 위험)."""
    log = tmp_path / "log.json"

    class CrashChannel:
        name = "webhook"

        def __init__(self) -> None:
            self.seen: dict | None = None

        def send(self, event: dict) -> dict:
            # 발송 시점에 이미 장부가 있는지 — 프로세스가 여기서 죽어도 재실행이 같은 사건을 안다.
            self.seen = json.loads(log.read_text(encoding="utf-8"))["keys"].get(event["event_key"])
            raise RuntimeError("network died after the request left")

    channel = CrashChannel()
    notify.evaluate_notifications(_notify_payload(), log_path=log, out_path=None,
                                  channels=[notify.ConsoleChannel(), channel])
    assert channel.seen is not None, "발송 전에 이벤트가 장부에 없었다"
    assert channel.seen["channels"]["webhook"]["status"] == "pending_retry"
    assert channel.seen["channels"]["webhook"]["detail"] == "in_flight"
    # 실패로 끝난 실행도 재시도 대상으로 남는다
    assert _log(log)["keys"][_key("strong_buy")]["channels"]["webhook"]["status"] == "pending_retry"
    # 원자적 쓰기 — 임시 파일이 남지 않고 장부는 항상 완결된 JSON 이다
    assert not list(tmp_path.glob(".log.json.tmp"))


def test_webhook_sends_idempotency_key(monkeypatch):
    """웹훅 POST 에 `X-Idempotency-Key: {event_key}`(감사 11 — 받는 쪽이 재시도를 접는다)."""
    captured: dict = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(request, timeout=10):  # noqa: ARG001 — 시그니처 호환 유지
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    channel = notify.WebhookChannel(url="https://example.invalid/hook")
    result = channel.send({"event_key": "strong_buy:2026-11-21-AAA-BUY-001", "type": "strong_buy"})
    assert result["ok"] is True
    assert captured["headers"]["x-idempotency-key"] == "strong_buy:2026-11-21-AAA-BUY-001"
    assert captured["body"]["event_key"] == "strong_buy:2026-11-21-AAA-BUY-001"


def test_legacy_schema2_pending_event_is_retried(tmp_path):
    """구 로그(schema 2, 채널 장부 없음)의 재시도 잔여도 이어서 처리한다(감사 11 호환)."""
    log = tmp_path / "log.json"
    event = notify._event("strong_buy", _key("strong_buy"), "AAA STRONG_BUY",
                          ticker="AAA", signal_id="2026-11-21-AAA-BUY-001")
    log.write_text(json.dumps({"schema_version": 2,
                               "keys": {_key("strong_buy"): {"sent_at": None, "type": "strong_buy",
                                                             "status": "pending_retry", "attempts": 1,
                                                             "event": event}},
                               "deliveries": []}, ensure_ascii=False), encoding="utf-8")
    console = CountingConsole()
    summary = notify.evaluate_notifications({"as_of": "2026-11-21", "signals": [], "blocked": [],
                                             "position_checks": [], "diagnostics": {}},
                                            log_path=log, out_path=None, channels=[console])
    assert summary["n_retried"] == 1
    assert _log(log)["keys"][_key("strong_buy")]["status"] == "sent"
    assert [e["event_key"] for e in console.calls] == [_key("strong_buy")]


# ────────────────────────── [12] 국면 표본: 실제 원천 → A/B 판정 ──────────────────────────
def _panel(ticker: str = "FIXA", end: date | None = None):
    """마감(`prev_trading_day(as_of)`)까지만 있는 패널 — 실행 시점에 볼 수 있는 봉과 같다."""
    hist, splits = make_history(days=520, start="2024-11-01")
    hist = hist.loc[hist.index <= _ts(end or prev_trading_day(AS_OF))]
    return build_price_frame(ticker, hist, splits)


def _ts(day: date):
    import pandas as pd

    return pd.Timestamp(day)


def _write_records(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _matured_records(count: int, *, regime: str | None = "neutral", ticker: str = "FIXA",
                     horizon_td: int = 63) -> list[dict]:
    """`as_of` 이전에 3개월 만기가 끝나는 발행 기록."""
    start = add_trading_days(AS_OF, -(horizon_td + 120))
    rows = []
    for i in range(count):
        day = add_trading_days(start, i * 2)
        row = {"as_of": day.isoformat(), "ticker": ticker, "type": "WATCH",
               "signal_id": f"{day.isoformat()}-{ticker}-WATCH-{i:03d}"}
        if regime is not None:
            row["regime"] = regime
        rows.append(row)
    return rows


def test_regime_sample_counts_matured_only_before_as_of(tmp_path):
    """만기 확정 조건 — `as_of` 이전 만기·관측 봉·국면 확인. 미래 만기·미관측·국면 누락은 빠진다(감사 12)."""
    records = _matured_records(29)
    records.append({"as_of": add_trading_days(AS_OF, -30).isoformat(), "ticker": "FIXA",
                    "signal_id": "later", "regime": "neutral"})                 # 만기가 as_of 이후
    records.append({"as_of": add_trading_days(AS_OF, -200).isoformat(), "ticker": "GONE",
                    "signal_id": "no-price", "regime": "neutral"})             # 관측 봉 없음
    records.append({"as_of": add_trading_days(AS_OF, -200).isoformat(), "ticker": "FIXA",
                    "signal_id": "no-regime"})                                 # 국면 근거 없음
    path = _write_records(tmp_path / "records.jsonl", records)
    doc = regime_sample_counts(AS_OF, {"FIXA": _panel()}, records_path=path,
                               snapshots_root=tmp_path / "snapshots")
    assert doc["by_regime"]["neutral"]["n"] == 29
    assert doc["by_regime"]["neutral"]["min_n"] == 30
    assert doc["by_regime"]["neutral"]["matured_as_of"] == AS_OF.isoformat()
    assert doc["by_regime"]["risk_off"]["n"] == 0
    assert doc["matured_as_of"] == AS_OF.isoformat()
    assert doc["horizon"] == "3m" and doc["horizon_td"] == 63
    assert doc["counts"]["matured"] == 29
    assert doc["counts"]["not_matured"] == 1
    assert doc["counts"]["no_price"] == 1
    assert doc["counts"]["no_regime"] == 1
    assert doc["verified"] is True


def test_regime_sample_29_caps_b_and_30_opens_a():
    """29건은 실제 수치를 유지한 B 상한, 30건이면 같은 배선에서 A 가 열린다(감사 12)."""
    sample29 = {"n": 29, "min_n": 30, "matured_as_of": AS_OF.isoformat(), "horizon": "3m"}
    sample30 = {"n": 30, "min_n": 30, "matured_as_of": AS_OF.isoformat(), "horizon": "3m"}
    evidence = [
        {"type": "fundamental", "metric": "quality_composite", "accession": "acc-10q",
         "available_at": "2026-11-01T20:00:00Z"},
        {"type": "sec_filing", "form": "10-Q", "accession": "acc-10q", "url": "https://sec.gov/10q"},
        {"type": "news", "source": "WireA", "url": "https://a/1", "quote_verified": True,
         "quote_sha256": "x"},
        {"type": "news", "source": "WireB", "url": "https://b/2", "quote_verified": True,
         "quote_sha256": "y"},
        {"type": "market", "metric": "atr_pct", "value_kind": "ratio"},
    ]
    basis: dict = {}
    assert confidence_grade(evidence, "read_done", regime_sample=sample30, data_fresh=True,
                            grade_basis=basis) == "A"
    assert basis["regime_sample_ok"] is True
    basis29: dict = {}
    assert confidence_grade(evidence, "read_done", regime_sample=sample29, data_fresh=True,
                            grade_basis=basis29) == "B"
    assert basis29["regime_sample_ok"] is False
    assert basis29["reason"] == "regime_sample_lt_30"
    # 8-K·재무 대체 경로(뉴스 미판독)에도 같은 표본 조건이 걸린다
    alt = [evidence[0],
           {"type": "sec_filing", "form": "8-K", "items": ["2.02"], "accession": "acc-8k",
            "url": "https://sec.gov/8k"},
           {"type": "market", "metric": "atr_pct", "value_kind": "ratio"}]
    filing = {"form": "8-K", "items": ["2.02"]}
    assert confidence_grade(alt, "not_read", regime_sample=sample30,
                            alt_8k_evidence=[filing], data_fresh=True) == "A"
    assert confidence_grade(alt, "not_read", regime_sample=sample29,
                            alt_8k_evidence=[filing], data_fresh=True) == "B"


def test_missing_sample_keeps_b_cap_and_is_not_passed_through():
    """표본이 없으면 `None` → `regime_sample_missing` 으로 B 상한(통과 금지, 감사 12)."""
    assert _regime_sample_for({}, regime_hint="neutral") is None
    assert _regime_sample_for({"regime_sample": None}, regime_hint="risk_off") is None
    doc = {"by_regime": {"neutral": {"n": 30, "min_n": 30}}, "horizon": "3m"}
    # 국면을 모르는 호출은 국면별 문서를 통째로 넘기지 않는다(엉뚱한 국면 표본으로 A 금지)
    assert _regime_sample_for({"regime_sample": doc}, regime_hint=None) is None
    assert _regime_sample_for({"regime_sample": doc}, regime_hint="neutral")["n"] == 30
    assert _regime_sample_for({"regime_sample": doc}, regime_hint="risk_on") is None
    evidence = [{"type": "fundamental", "metric": "quality_composite", "accession": "a"},
                {"type": "sec_filing", "form": "10-Q", "accession": "a", "url": "u"},
                {"type": "market", "metric": "atr_pct"}]
    basis: dict = {}
    assert confidence_grade(evidence, "not_read", regime_sample=None,
                            alt_8k_evidence=[{"form": "8-K", "items": ["2.02"]}],
                            grade_basis=basis) == "B"
    assert basis["regime_sample_ok"] is False and "regime_sample" in basis["reason"]


def test_legacy_record_regime_is_restored_from_frozen_vintage(tmp_path):
    """국면 라벨이 없는 구 레코드는 **그 스냅샷의 동결 입력**으로 국면을 복원한다(사후 집계 금지)."""
    snapshots = tmp_path / "snapshots"
    folder = snapshots / "2025-12-01"
    folder.mkdir(parents=True)
    (folder / "inputs.json").write_text(json.dumps({"macro": RISK_OFF, "macro_previous": RISK_OFF_PREV}),
                                        encoding="utf-8")
    rows = [{"as_of": "2025-12-01", "ticker": "FIXA", "signal_id": "legacy-1"}]
    path = _write_records(tmp_path / "records.jsonl", rows)
    doc = regime_sample_counts(AS_OF, {"FIXA": _panel()}, records_path=path, snapshots_root=snapshots)
    assert doc["by_regime"]["risk_off"]["n"] == 1
    assert doc["by_regime"]["neutral"]["n"] == 0
    # 아카이브가 없으면 국면을 단정하지 않는다(추정 국면으로 표본을 만들지 않는다)
    other = _write_records(tmp_path / "records2.jsonl",
                           [{"as_of": "2025-12-01", "ticker": "FIXA", "signal_id": "legacy-2"}])
    doc2 = regime_sample_counts(AS_OF, {"FIXA": _panel()}, records_path=other,
                                snapshots_root=tmp_path / "empty")
    assert doc2["counts"]["no_regime"] == 1
    assert all(row["n"] == 0 for row in doc2["by_regime"].values())


def test_missing_ledger_reports_zero_sample(tmp_path):
    """원장이 없으면 0건·사유를 남긴다 — 없는 표본을 통과로 바꾸지 않는다(감사 12)."""
    doc = regime_sample_counts(AS_OF, {"FIXA": _panel()},
                               records_path=tmp_path / "nope.jsonl",
                               snapshots_root=tmp_path / "snapshots")
    assert doc["counts"]["records"] == 0 and doc["counts"]["matured"] == 0
    assert doc["reason"] == "ledger_missing"
    assert doc["verified"] is False
    assert DEFAULT_LEDGER_RECORDS.as_posix().endswith("records.jsonl")


def _universe_yaml(path: Path, tickers: list[str]) -> Path:
    rows = [{"ticker": ticker, "sector": row["sector"], "q": row["q"], "g": row["g"],
             "iv_mult": row["iv_mult"], "market_cap_usd": row["market_cap_usd"],
             "adv20_usd": row["adv20_usd"], "days_to_earnings": row["earnings_in"]}
            for ticker, row in ((t, next(r for r in fixtures.UNIVERSE if r["ticker"] == t))
                                for t in tickers)]
    path.write_text(yaml.safe_dump({"benchmark": "SPY", "tickers": rows}, allow_unicode=True,
                                   sort_keys=False), encoding="utf-8")
    return path


def _fake_collect(monkeypatch, *, panels: dict) -> None:
    """네트워크 없이 **실수집 계약**을 흉내 낸다 — collect() 의 각 소스만 대체한다."""
    monkeypatch.setattr(collect_mod, "collect_prices", lambda tickers, as_of, decision_at, diag: dict(panels))
    monkeypatch.setattr(collect_mod, "collect_macro", lambda decision_at, diag, mode="live", now=None: (
        dict(RISK_OFF), dict(RISK_OFF_PREV)))
    monkeypatch.setattr(collect_mod, "collect_filings", lambda *a, **k: {})
    monkeypatch.setattr(collect_mod, "collect_news", lambda *a, **k: {})
    monkeypatch.setattr(collect_mod, "collect_meta", lambda tickers, diag, pause=0.25: {
        t: {"market_cap_usd": 2.4e12, "exchange": "NYSE", "shares_outstanding": 1e9} for t in tickers})

    def _fundamentals(tickers, *args, **kwargs):  # noqa: ARG001 — 시그니처 호환
        rows = {row["ticker"]: row for row in fixtures.UNIVERSE}
        fundamentals = {t: fixtures._fundamentals(1.0) for t in tickers if t in rows}
        valuation = {t: {"iv_mult": rows[t]["iv_mult"], "iv_median": 100.0 * rows[t]["iv_mult"],
                         "price": 100.0}
                     for t in tickers if t in rows and rows[t]["iv_mult"]}
        return fundamentals, valuation

    monkeypatch.setattr(collect_mod, "collect_fundamentals", _fundamentals)


def test_collect_supplies_regime_sample_end_to_end(tmp_path, monkeypatch):
    """`collect()` → `build_payload()` — 국면별 표본 수와 기준일이 정직하게 배선된다(감사 12)."""
    tickers = ["FIXA", "FIXB", "FIXC", "FIXD", "FIXE", "FIXF"]
    panels = fixtures.panels(AS_OF)
    _fake_collect(monkeypatch, panels=panels)
    universe = _universe_yaml(tmp_path / "universe.yaml", tickers)
    records = _matured_records(30, regime="risk_off", ticker="FIXA")
    records += _matured_records(30, regime="neutral", ticker="FIXB")
    path = _write_records(tmp_path / "records.jsonl", records)
    data = collect_mod.collect(AS_OF, collect_mod.datetime(2026, 11, 19, 22, 0,
                                                           tzinfo=collect_mod.timezone.utc),
                               universe_path=universe, ledger_records=path,
                               snapshots_root=tmp_path / "snapshots")
    sample = data["regime_sample"]
    assert data["diagnostics"]["regime_sample"]["n"] == sample["n"]
    # 수집 시점 거시는 5표 모두 risk_off → 현재 국면의 표본은 30건
    assert sample["regime"] == "risk_off"
    assert sample["n"] == 30 and sample["min_n"] == 30
    assert sample["matured_as_of"] == AS_OF.isoformat()
    assert sample["by_regime"]["risk_off"]["n"] == 30
    assert sample["by_regime"]["neutral"]["n"] == 30

    payload = build_payload(data, as_of=AS_OF, mode="postclose",
                            params=load_params(ROOT / "config" / "params_v001.yaml"),
                            params_version="params_v001", code_commit="test",
                            snapshot_id="sha256:test", status="live", three_times=TIMES)
    assert payload["regime_sample"]["n"] == 30
    assert payload["regime_sample"]["matured_as_of"] == AS_OF.isoformat()
    assert payload["regime_sample"]["regime"] == "risk_off"
    assert payload["regime_sample"]["by_regime"]["neutral"]["n"] == 30
    assert payload["regime_sample"]["reason"] is None
    for record in payload["signals"]:
        detail = record["confidence_detail"]
        assert detail["regime_sample_n"] == 30
        assert detail["regime_sample_ok"] is True
        assert detail["matured_as_of"] == AS_OF.isoformat()
        assert record["regime"] == "risk_off"       # 발행 시점 국면이 레코드에 남는다
    assert payload["signals"], "표본 배선을 확인하려면 신호가 있어야 한다"
    # 새 최상위 키(`regime_sample`)가 데이터 계약을 깨지 않는지 — 저장된 실제 페이로드로 검증한다.
    stored = json.loads((ROOT / "data" / "quant_signals.json").read_text(encoding="utf-8"))
    stored["regime_sample"] = payload["regime_sample"]
    validate_quant_signals(stored)


def test_below_threshold_keeps_real_count_and_b_cap(tmp_path, monkeypatch):
    """29건이면 실제 수치(29)가 그대로 남고 A 는 열리지 않는다 — 30건에서 열린다(감사 12)."""
    tickers = ["FIXA", "FIXB", "FIXC", "FIXD", "FIXE", "FIXF"]
    panels = fixtures.panels(AS_OF)
    _fake_collect(monkeypatch, panels=panels)
    universe = _universe_yaml(tmp_path / "universe.yaml", tickers)
    path = _write_records(tmp_path / "records.jsonl",
                          _matured_records(29, regime="risk_off", ticker="FIXA"))
    data = collect_mod.collect(AS_OF, collect_mod.datetime(2026, 11, 19, 22, 0,
                                                           tzinfo=collect_mod.timezone.utc),
                               universe_path=universe, ledger_records=path,
                               snapshots_root=tmp_path / "snapshots")
    payload = build_payload(data, as_of=AS_OF, mode="postclose",
                            params=load_params(ROOT / "config" / "params_v001.yaml"),
                            params_version="params_v001", code_commit="test",
                            snapshot_id="sha256:test", status="live", three_times=TIMES)
    assert payload["regime_sample"]["n"] == 29
    assert payload["signals"]
    for record in payload["signals"]:
        detail = record["confidence_detail"]
        assert detail["regime_sample_n"] == 29
        assert detail["regime_sample_ok"] is False
        assert detail["matured_as_of"] == AS_OF.isoformat()


def test_records_published_now_carry_regime_for_future_samples(tmp_path, monkeypatch):
    """이번 실행이 발행한 레코드는 국면을 남겨 **다음 표본 집계**가 아카이브 없이 돈다(감사 12)."""
    tickers = ["FIXA", "FIXB", "FIXC", "FIXD", "FIXE", "FIXF"]
    panels = fixtures.panels(AS_OF)
    _fake_collect(monkeypatch, panels=panels)
    universe = _universe_yaml(tmp_path / "universe.yaml", tickers)
    data = collect_mod.collect(AS_OF, collect_mod.datetime(2026, 11, 19, 22, 0,
                                                           tzinfo=collect_mod.timezone.utc),
                               universe_path=universe,
                               ledger_records=tmp_path / "empty.jsonl",
                               snapshots_root=tmp_path / "snapshots")
    payload = build_payload(data, as_of=AS_OF, mode="postclose",
                            params=load_params(ROOT / "config" / "params_v001.yaml"),
                            params_version="params_v001", code_commit="test",
                            snapshot_id="sha256:test", status="live", three_times=TIMES)
    assert payload["signals"]
    path = _write_records(tmp_path / "records.jsonl", payload["signals"])
    later = add_trading_days(AS_OF, 70)
    doc = regime_sample_counts(later, fixtures.panels(later), records_path=path,
                               snapshots_root=tmp_path / "snapshots")
    assert doc["by_regime"]["risk_off"]["n"] >= 1       # 발행 시점 국면이 그대로 표본 근거가 된다
    assert doc["counts"]["no_regime"] == 0
    # 만기 전(as_of 당일)에는 아직 표본이 아니다 — 시점 계약
    same_day = regime_sample_counts(AS_OF, fixtures.panels(later), records_path=path,
                                    snapshots_root=tmp_path / "snapshots")
    assert same_day["by_regime"]["risk_off"]["n"] == 0
    assert same_day["counts"]["not_matured"] == len(payload["signals"])
