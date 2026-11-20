#!/usr/bin/env python3
"""scripts/run_daily.py — 하루 파이프라인: 수집 → 지표 → 신호 → Paper Ledger → 스냅샷 → 발행.

`.github/workflows/daily-postclose.yml` 계약 [고정]
    python scripts/run_daily.py --mode postclose
- `GITHUB_OUTPUT` 환경변수가 있으면 `snapshot_id`, `snapshot_id_short` 를 그 파일에 쓴다.
- 환경변수(FRED_API_KEY·SEC_USER_AGENT·FINNHUB_API_KEY)가 없거나 API 가 실패해도 **크래시하지 않는다**:
  소스별로 `degraded` 를 남기고, 가격이 하나도 없으면 **전일 데이터를 유지**한 채 종료한다
  (사이트가 빈 화면이 되지 않게 — 기획서 2.7).
- 산출물: `data/quant_signals.json`, `data/ledger/records.jsonl`(발행 의도),
  `data/ledger/executions/{signal_id}.json`(체결 이벤트), `data/ledger/portfolio/state.json`(비중만),
  `data/ledger/pilot_eval.json`, `data/params_history.json`, `data/snapshots/{as_of}/`(아카이브 원본 —
  같은 날 입력이 바뀌면 `{as_of}-r2` 로 보존).
- 발행 시각은 **결과가 확정되는 실제 시각**(`signal_published_at`)이고, 배치 시작 시각은
  `run_started_at` 으로 따로 남긴다. 같은 입력 지문(`inputs_hash`)의 재실행은 **원장의 발행본을
  그대로 배포**하고, 입력이 바뀐 발행은 회차 접미사(`-002`…)와 `supersedes` 로 남긴다(감사 01·02).

    python scripts/run_daily.py --mode postclose --offline --as-of 2026-11-20 --root .
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.backtest.holdout import window_for  # noqa: E402
from engine.backtest.snapshot import inputs_fingerprint, make_snapshot  # noqa: E402
from engine.decide.pipeline import build_payload, load_params  # noqa: E402
from engine.ledger import live  # noqa: E402
from engine.ledger.pilot import evaluate_pilot, write_pilot_eval  # noqa: E402
from engine.ledger.record import (RecordError, append_immutable, attach_target_session,  # noqa: E402
                                  load_records, resolve_release)
from engine.models import QuantSignalSchemaError, validate_quant_signals  # noqa: E402
from engine.utils.dates import (KST, data_cutoff_at, order_cutoff_at, signal_published_at,  # noqa: E402
                                target_session_for)

RECORDS = Path("data/ledger/records.jsonl")
EXECUTIONS = Path("data/ledger/executions")
PORTFOLIO = Path("data/ledger/portfolio")
SNAPSHOTS = Path("data/snapshots")
SIGNALS = Path("data/quant_signals.json")
PILOT = Path("data/ledger/pilot_eval.json")
PARAMS_HISTORY = Path("data/params_history.json")
WATCHLIST = Path("data/watchlist/current.json")
RADAR = Path("data/radar/13f.json")
FOLDS = Path("data/walkforward/folds/summary.json")
LIMITS = Path("data/pipeline_limits.json")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def publish_times(as_of: date, mode: str, published_at: datetime | None = None) -> dict:
    """`data_cutoff_at ≤ signal_published_at < order_cutoff_at` — 세 시각을 따로 계산한다(2.2.2).

    `signal_published_at` 은 **결과 JSON 이 확정되는 실제 시각**이다. 배치 **시작 시각**으로 잡으면
    수집이 20분 걸리는 날 "14:10Z 발행"이라고 우기면서 이미 끝난 주문 마감의 세션을 가리킨다
    (감사 01-b). 그래서 수집·정산이 끝난 뒤 호출자가 **발행 직전 시각**을 넘긴다.
    대상 세션(`target_session`)은 그 시각 이후 주문 마감이 아직 오지 않은 가장 이른 NYSE 세션이다.
    과거 `as_of` 백필은 계획 시각을 발행 시각으로 쓴다(마감 전 실행 가정).
    """
    cutoff = data_cutoff_at(as_of, mode)
    planned = signal_published_at(cutoff)
    actual = (published_at if published_at is not None else datetime.now(timezone.utc)).replace(microsecond=0)
    if actual < planned:
        actual = planned
    session = target_session_for(actual)
    if session is None:
        raise SystemExit(f"대상 거래일을 찾을 수 없다: published={_iso(actual)}")
    order = order_cutoff_at(session)
    if not (cutoff <= actual < order):
        raise SystemExit(f"세 시각 순서 위반: {cutoff} / {actual} / {order}")
    return {"data_cutoff_at": _iso(cutoff), "signal_published_at": _iso(actual),
            "signal_published_planned_at": planned, "order_cutoff_at": _iso(order),
            "target_session": session.isoformat(),
            "_cutoff": cutoff}


def three_times(as_of: date, mode: str, now: datetime | None = None) -> dict:
    """구 이름 호환 — `publish_times(as_of, mode, published_at=now)` 와 같다."""
    return publish_times(as_of, mode, published_at=now)


def ensure_snapshot(root: Path, *, as_of: date, cutoff: datetime, frames: dict, inputs: dict,
                    extra: dict, inputs_hash: str) -> tuple[str, str]:
    """스냅샷을 고정한다 — 같은 입력 지문이면 **다시 쓰지 않고 재사용**한다.

    - 같은 날 재실행으로 입력이 같으면 기존 `data/snapshots/{as_of}` 를 그대로 쓰고 `snapshot_id`
      도 유지한다(화면과 원장이 같은 스냅샷을 가리키게).
    - 입력이 달라 **새로 확정**된 발행이면 `{as_of}-r2`, `-r3` … 로 **이전 스냅샷을 덮어쓰지 않고**
      보존한다(append-only 아카이브의 원본이 사라지지 않게).
    Returns: (`snapshot_id`, `"reused"|"created"`)
    """
    day = as_of.isoformat()

    def _existing_match(path: Path) -> str | None:
        manifest = _read_json(path / "manifest.json") or {}
        if (manifest.get("extra") or {}).get("inputs_hash") == inputs_hash:
            return str(manifest.get("snapshot_id") or "")
        return None

    base = root / day
    if base.exists():
        reused = _existing_match(base)
        if reused:
            return reused, "reused"
        n = 2
        while (root / f"{day}-r{n}").exists():
            reused = _existing_match(root / f"{day}-r{n}")
            if reused:
                return reused, "reused"
            n += 1
        base = root / f"{day}-r{n}"
    snap = make_snapshot(base, cutoff, frames, as_of=day, inputs=inputs,
                         extra={**extra, "inputs_hash": inputs_hash})
    return snap.snapshot_id, "created"


def load_dataset(args, as_of: date, cutoff: datetime) -> dict:
    """픽스처(오프라인·테스트) 또는 라이브 수집. 라이브 실패는 예외가 아니라 `degraded`."""
    if args.fixtures:
        from engine.data.fixtures import dataset  # noqa: PLC0415
        data = dataset(as_of)
        data["source"] = "fixtures"
        data["diagnostics"] = {"mode": "fixtures", "degraded": [], "sources": {"fixtures": {"status": "ok"}}}
        return data
    from engine.data.collect import collect  # noqa: PLC0415
    try:
        return collect(as_of, cutoff)
    except Exception as exc:  # noqa: BLE001 — 수집 전체 실패도 파이프라인 실패가 아니다
        return {"as_of": as_of.isoformat(), "universe": [], "panels": {}, "macro": {}, "macro_previous": {},
                "filings": {}, "news": {}, "fundamentals": {}, "valuation_inputs": {},
                "source": "live", "diagnostics": {"mode": "live", "degraded": [f"collect:{type(exc).__name__}"],
                                                  "sources": {"collect": {"status": "failed", "detail": str(exc)[:200]}}}}


def ensure_params_history(params: dict, path: Path = PARAMS_HISTORY) -> list[dict]:
    """활성 파라미터 버전을 이력에 남긴다(없을 때만 append — 이력은 append-only)."""
    version = str(params.get("version") or "params_v001")
    history = _read_json(path) or []
    if isinstance(history, dict):
        history = history.get("versions", [])
    if not any(h.get("version") == version for h in history):
        history.append({
            "version": version,
            "created_at": str(params.get("created_at") or date.today().isoformat()),
            "parent_version": history[-1]["version"] if history else None,
            "gate_result": {"status": "active", "note_ko": "워크포워드 보정은 PR 로만 반영한다(4.3)"},
            "changed": [],
            "note_ko": "초기 버전 — 보정 이력 없음",
        })
        _write_json(path, history)
    return history


def ensure_folds(as_of: date) -> dict:
    folds = _read_json(FOLDS)
    if folds and folds.get("horizons"):
        return folds
    from engine.utils.dates import fold_summary  # noqa: PLC0415
    folds = fold_summary(as_of)
    _write_json(FOLDS, folds)
    return folds


def ledger_view(records: list[dict], executions: dict, panels: dict, as_of: date,
                reanchors: dict | None = None) -> tuple[dict, dict]:
    """체결 재생 → (모의 계좌 공개 상태, 종목별 보유)"""
    state = live.replay(records, executions, reanchors)
    public = live.mark(state, panels, as_of, executions, records)
    holdings = {t: {"ticker": t, "shares": p["qty"], "avg_cost": p["avg_price"],
                    "stop_price": p["stop_price"], "trailing_high": p["avg_price"],
                    "initial_stop": p["stop_price"]}
                for t, p in state["positions"].items()}
    return public, holdings


def run(args) -> int:
    root = Path(args.root).resolve()
    if root != Path.cwd():
        os.chdir(root)
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(KST).date()
    run_started_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = data_cutoff_at(as_of, args.mode)
    params = load_params(Path(args.params))
    params_version = str(params.get("version") or "params_v001")
    commit = _git_commit()
    history = ensure_params_history(params)
    folds = ensure_folds(as_of)
    watchlist_payload = _read_json(WATCHLIST)

    data = load_dataset(args, as_of, cutoff)
    diag = data.get("diagnostics") or {}
    diag.setdefault("degraded", [])

    panels = data.get("panels") or {}
    if not panels:
        # 가격이 하나도 없다 — 전일 데이터 유지(빈 화면을 배포하지 않는다, 2.7)
        prev = _read_json(SIGNALS) or {}
        _write_json(Path("data/quant_signals.degraded.json"),
                    {"as_of": as_of.isoformat(), "status": "degraded",
                     "degraded": diag.get("degraded"), "previous_status": prev.get("status"),
                     "note_ko": "가격 수집 실패 — 전일 데이터를 유지했다."})
        print(json.dumps({"status": "degraded", "kept_previous": SIGNALS.exists(),
                          "degraded": diag.get("degraded")}, ensure_ascii=False))
        return 0

    # 1) Ledger — 대상 세션 의도 정산(시가 확정분만) + 체결 이벤트 append
    records = load_records(RECORDS)
    executions = live.load_executions(EXECUTIONS)
    reanchors = live.load_reanchors(EXECUTIONS)
    settlement = live.settle(records, executions, panels, as_of, slippage_bps=args.slippage_bps)
    for event in settlement["new_events"]:
        live.write_execution(event, EXECUTIONS)
        live.append_full_event(event, None, EXECUTIONS)
        executions[event["signal_id"]] = event
    for event in settlement["reanchors"]:
        live.write_reanchor(event, EXECUTIONS)
        reanchors[event["signal_id"]] = event

    portfolio_before, held_before = ledger_view(records, executions, panels, as_of, reanchors)
    watch = ({r["ticker"] for r in (watchlist_payload or {}).get("watchlist") or []}
             if watchlist_payload else None)
    n_universe_before = len(data.get("universe") or [])
    if watch is not None:
        data["universe"] = [r for r in data["universe"] if r["ticker"] in watch or r["ticker"] in held_before]

    # 2) 발행 시각 — **수집·정산이 끝난 지금**. 실행 시작 시각으로 잡지 않는다(감사 01-b).
    #    `run_started_at`(배치 시작)과 `signal_published_at`(결과 확정 직전)은 다른 값이다.
    times = publish_times(as_of, args.mode)

    # 3) 판단 입력 확정 — 워치리스트 필터·보유·섹터 비중·target_session 까지 끝난 상태를 고정한다(감사 02-b).
    #    스냅샷은 이 **최종 입력**으로 만들고(입력 지문 기준), 재생은 같은 값을 그대로 읽는다.
    frames = {f"prices_{ticker}": panel.frame for ticker, panel in sorted(panels.items())}
    inputs = {k: v for k, v in data.items() if k != "panels"}
    held = list(held_before.values())
    sector_weights = portfolio_before.get("sector_weights") or {}
    watchlist_filter = {
        "source": "data/watchlist/current.json" if watch is not None else "universe(주간 스크린 전)",
        "n_before": int(n_universe_before),
        "n_after": len(data.get("universe") or []),
    }
    fingerprint_inputs = {
        **inputs,
        "held": held,
        "sector_weights": sector_weights,
        "params": params,
        "watchlist_filter": watchlist_filter,
        # 주문 대상 세션이 다르면 같은 데이터라도 **다른 발행**이다(늦은 재실행 → 새 회차).
        "target_session": times["target_session"],
    }
    try:
        inputs_hash = inputs_fingerprint(frames, fingerprint_inputs, cutoff, params_version)
    except Exception as exc:  # noqa: BLE001 — 지문 실패가 발행을 막지는 않는다(회차는 001 고정)
        inputs_hash = ""
        diag["inputs_hash_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    data["decision"] = {
        "mode": args.mode,
        "run_started_at": _iso(run_started_at),
        "data_cutoff_at": times["data_cutoff_at"],
        "signal_published_at": times["signal_published_at"],
        "signal_published_planned_at": _iso(times["signal_published_planned_at"]),
        "order_cutoff_at": times["order_cutoff_at"],
        "target_session": times["target_session"],
        "held": [{k: h.get(k) for k in ("ticker", "sector", "stop_price", "trailing_high", "initial_stop")}
                 for h in held],
        "sector_weights": sector_weights,
        "watchlist_filter": watchlist_filter,
        "params": params,
        "params_version": params_version,
        "code_commit": commit,
        "inputs_hash": inputs_hash,
        "cutoff_rule": "available_at <= decision_at",
    }
    snapshot_inputs = {k: v for k, v in data.items() if k != "panels"}
    try:
        snapshot_id, snapshot_state = ensure_snapshot(
            SNAPSHOTS, as_of=as_of, cutoff=cutoff, frames=frames, inputs=snapshot_inputs,
            extra={"mode": args.mode, "source": data.get("source"),
                   "params_version": params_version, "code_commit": commit,
                   "target_session": times["target_session"],
                   "cutoff_rule": "available_at <= decision_at"},
            inputs_hash=inputs_hash)
    except Exception as exc:  # noqa: BLE001 — 스냅샷 실패는 신호 발행을 막지 않는다
        snapshot_id, snapshot_state = "sha256:" + "0" * 64, "failed"
        diag["snapshot_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    diag["snapshot"] = {"id": snapshot_id, "state": snapshot_state, "inputs_hash": inputs_hash}
    short = snapshot_id.replace("sha256:", "")[:12]

    # 4) 신호 발행
    status = "normal" if not diag.get("degraded") else "degraded"
    if data.get("source") in ("fixtures", "sample"):
        status = "sample"
    payload = build_payload(
        data, as_of=as_of, mode=args.mode, params=params, params_version=params_version,
        code_commit=commit, snapshot_id=snapshot_id, status=status, three_times=times,
        held=list(held_before.values()), sector_weights=portfolio_before.get("sector_weights") or {},
        source=data.get("source"), inputs_hash=inputs_hash or None,
    )
    payload["diagnostics"] = diag
    payload["watchlist_source"] = watchlist_filter["source"]
    payload["run_started_at"] = _iso(run_started_at)
    payload["inputs_hash"] = inputs_hash

    # 4) 발행 회차 확정 → append → **원장에 기록된 발행본 그대로 배포**(감사 02)
    appended, skipped, duplicated, deployed = [], [], [], []
    for record in payload["signals"]:
        try:
            attach_target_session(record, times["target_session"],
                                  times.get("signal_published_planned_at"))
        except RecordError as exc:
            skipped.append({"signal_id": record["signal_id"], "reason": str(exc)[:120]})
            continue
        release = resolve_release(record, records)
        record["signal_id"] = release["signal_id"]
        record["revision"] = int(release["revision"])
        record["supersedes"] = release["supersedes"]
        if release["duplicate"]:
            # 같은 입력 지문의 재실행 — 이미 기록된 발행본을 그대로 배포한다(화면 == 원장).
            prior = next((r for r in records if str(r.get("signal_id")) == release["signal_id"]), None)
            if prior is not None:
                duplicated.append({"signal_id": release["signal_id"], "reused_from": release["reused_from"]})
                deployed.append(copy.deepcopy(prior))
                continue
        try:
            append_immutable(RECORDS, record)
        except RecordError as exc:
            skipped.append({"signal_id": record["signal_id"], "reason": str(exc)[:120]})
            continue
        records.append(record)
        appended.append(record["signal_id"])
        deployed.append(record)
    payload["signals"] = deployed
    if skipped:
        payload["diagnostics"]["record_skips"] = skipped
    if duplicated:
        payload["diagnostics"]["record_duplicates"] = duplicated

    # 5) 계좌·채점·파일럿 요약
    records = load_records(RECORDS)
    portfolio_public, held_after = ledger_view(records, executions, panels, as_of, reanchors)
    live.append_portfolio_history(portfolio_public, PORTFOLIO)
    first_entry = _first_entry_session(executions)
    portfolio_public["avg_exposure"] = live.avg_exposure_from_history(
        live.read_portfolio_history(PORTFOLIO), first_entry, as_of)
    window = window_for(as_of)
    benchmark_panel = panels.get("SPY")
    trailing = live.benchmark_trailing_returns(panels)
    graded = live.grade_all(records, executions, panels, as_of,
                            holdout_start=window.holdout_start.isoformat(),
                            benchmark_panel=benchmark_panel)
    pilot = evaluate_pilot(records, graded, executions=executions,
                           portfolio={**portfolio_public,
                                      "total_return": (portfolio_public.get("equity_index") or 1.0) - 1.0},
                           benchmark_return=live.benchmark_period_return(benchmark_panel, first_entry, as_of),
                           risk_free=args.risk_free)
    write_pilot_eval(pilot, PILOT)
    holdout_status = holdout_status_for(as_of)
    summary = live.summary(graded, portfolio_public, pilot_summary_of(pilot), folds=folds,
                           params_history=history, benchmark_connected=benchmark_panel is not None)
    summary["benchmark_trailing"] = trailing  # 참고 표시(배치 실행일 기준) — 채점에는 쓰지 않는다
    summary["holdout"] = holdout_status
    summary["graded_sample"] = graded[:20]
    summary["settlement"] = {k: settlement[k] for k in ("settled", "pending")}

    payload["ledger_summary"] = summary
    payload["target_session"] = times["target_session"]
    payload["signal_published_planned_at"] = _iso(times["signal_published_planned_at"])
    payload["portfolio"] = {
        "as_of": portfolio_public["as_of"],
        "cash_pct": portfolio_public["cash_pct"],
        "invested_pct": portfolio_public["invested_pct"],
        "avg_exposure": portfolio_public.get("avg_exposure"),
        "equity_index": portfolio_public.get("equity_index"),
        "positions": [{"ticker": p["ticker"], "weight": p["weight"], "sector": p["sector"],
                       "opened_at": p["opened_at"]} for p in portfolio_public["positions"]],
    }
    payload["pilot_eval"] = pilot_summary_of(pilot)
    payload["prices_as_of"] = _price_as_of(panels)
    _write_json(Path("data/walkforward/stress.json"), stress_payload())

    # 6) 레이더(13F) — 없으면 빈 정상 상태
    _write_json(RADAR, data.get("radar") or {
        "as_of": as_of.isoformat(),
        "watermark_ko": "45일 전 분기말 기준",
        "period_end": None, "lag_days": 45, "stale": True, "stale_days": None,
        "filers": [], "n_filers": 0, "counts": {"new": 0, "increase": 0, "reduce": 0, "exit": 0},
        "watchlist_overlap": [],
        "note_ko": "13F 수집 이전 — 레이더는 비어 있는 것이 정상이다(weekly-screen 이 채운다).",
    })
    _write_json(LIMITS, _limits(data, as_of))

    # 7) 계약 검증 → 실패하면 이전 파일을 유지한다(2.2.2)
    #    발행 지연도 정직하게 남긴다 — 확정 시각이 주문 마감을 넘겼는지(01-b).
    written_at = datetime.now(timezone.utc)
    published = datetime.fromisoformat(str(payload["signal_published_at"]).replace("Z", "+00:00"))
    order_cutoff = datetime.fromisoformat(str(payload["order_cutoff_at"]).replace("Z", "+00:00"))
    payload["publish_lag_seconds"] = round((written_at - published).total_seconds(), 1)
    payload["published_after_order_cutoff"] = bool(written_at >= order_cutoff)
    try:
        validate_quant_signals(payload)
    except QuantSignalSchemaError as exc:
        _write_json(Path("data/quant_signals.rejected.json"),
                    {"errors": exc.errors[:50], "as_of": as_of.isoformat()})
        print(json.dumps({"status": "rejected", "errors": exc.errors[:8]}, ensure_ascii=False), file=sys.stderr)
        return 1

    _write_json(SIGNALS, payload)
    if not args.no_redact:
        redact([SIGNALS, RECORDS])
    live.write_public_state(portfolio_public, PORTFOLIO)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"snapshot_id={snapshot_id}\n")
            f.write(f"snapshot_id_short={short}\n")
            f.write(f"status={status}\n")
            f.write(f"n_signals={len(payload['signals'])}\n")
            f.write(f"n_blocked={len(payload['blocked'])}\n")

    print(json.dumps({
        "as_of": as_of.isoformat(), "mode": args.mode, "status": status,
        "snapshot_id": snapshot_id, "snapshot_id_short": short,
        "signals": [s["signal_id"] for s in payload["signals"]],
        "blocked": [b["ticker"] for b in payload["blocked"]],
        "settled": settlement["settled"], "pending": len(settlement["pending"]),
        "records_appended": len(appended), "portfolio": {
            "cash_pct": portfolio_public["cash_pct"], "invested_pct": portfolio_public["invested_pct"],
            "n_positions": portfolio_public["n_positions"]},
        "pilot": pilot["verdict"], "degraded": diag.get("degraded"),
    }, ensure_ascii=False))
    return 0


def holdout_status_for(as_of: date) -> dict:
    from engine.backtest.holdout import HoldoutLedger  # noqa: PLC0415
    try:
        return HoldoutLedger().status(as_of)
    except Exception as exc:  # noqa: BLE001
        return {"locked": True, "note_ko": f"홀드아웃 상태 확인 실패: {type(exc).__name__}",
                "next_open": None}


def stress_payload() -> dict:
    from engine.backtest.stress import stress_report  # noqa: PLC0415
    return stress_report(None, None)


def pilot_summary_of(pilot: dict) -> dict:
    from engine.ledger.pilot import pilot_summary  # noqa: PLC0415
    return pilot_summary(pilot)


def _first_entry_session(executions: dict) -> date | None:
    """체결 이벤트 중 가장 이른 대상 세션 — 파일럿 기간·평균 노출도의 시작점."""
    days: list[date] = []
    for event in (executions or {}).values():
        if (event.get("quant_track") or {}).get("execution_event") != "buy_filled":
            continue
        try:
            days.append(date.fromisoformat(str(event.get("at", ""))[:10]))
        except ValueError:
            continue
    return min(days) if days else None


def _price_as_of(panels: dict) -> str | None:
    dates = [p.frame.index.max() for p in panels.values() if not p.frame.empty]
    if not dates:
        return None
    last = max(dates)
    return last.date().isoformat() if hasattr(last, "date") else str(last)


def _limits(data: dict, as_of: date) -> dict:
    diag = data.get("diagnostics") or {}
    return {
        "as_of": as_of.isoformat(),
        "source": data.get("source"),
        "universe": {"mode": "bootstrap_subset" if data.get("source") != "fixtures" else "fixtures",
                     "universe_verified": False,
                     "note_ko": "지수 구성종목 시계열(universe_history) 미확보 — 생존편향 있음(상한 추정)"},
        "sources": diag.get("sources", {}),
        "degraded": diag.get("degraded", []),
        "notes": [
            "부분집합·픽스처 결과는 전략 성과의 증거가 아니다(기획서 4.4·8.1).",
            "13F 는 레이더 전용이며 신호에 쓰지 않는다.",
        ],
    }


def redact(paths: list[Path]) -> None:
    """커밋 직전 인용 원문을 sha256 으로 바꾼다(2.2.3). 실패해도 발행은 유지."""
    existing = [str(p) for p in paths if Path(p).exists()]
    if not existing:
        return
    proc = subprocess.run([sys.executable, "scripts/redact_quotes.py", "--mode", "sha256", *existing],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(f"redact 실패: {proc.stderr[:200]}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일일 파이프라인 (postclose|premarket)")
    ap.add_argument("--mode", choices=["postclose", "premarket"], default="postclose")
    ap.add_argument("--offline", action="store_true", help="네트워크 없이 픽스처로 실행(테스트)")
    ap.add_argument("--fixtures", action="store_true", help="--offline 과 같다")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--params", default="config/params_v001.yaml")
    ap.add_argument("--slippage-bps", type=float, default=live.SLIPPAGE_BPS)
    ap.add_argument("--risk-free", type=float, default=0.0, help="단기국채 3개월 수익률(연)")
    ap.add_argument("--no-redact", action="store_true")
    args = ap.parse_args(argv)
    if args.offline:
        args.fixtures = True
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
