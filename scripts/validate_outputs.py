#!/usr/bin/env python3
"""scripts/validate_outputs.py — 계약 데이터 무결성 검사 (인자 없이 실행).

`daily-postclose.yml` 이 `python scripts/validate_outputs.py` 로 부른다 [고정].
하나라도 어긋나면 **exit 1** 이고, 그 실행의 커밋·배포가 멈춘다(깨진 데이터를 배포하지 않는다).

검사 항목
1. `data/quant_signals.json` — 스키마·세 시각 순서·발행 레코드에 체결가 없음·공개 금지 필드 0건
2. `data/ledger/records.jsonl` — 레코드 불변 규칙·signal_id 중복·금지 필드
3. `data/ledger/pilot_eval.json` — 4지표가 모두 있고 판정이 있다
4. `data/ledger/portfolio/state.json` — 비중만 있고 공개 금지 필드가 없다
5. `data/params_history.json` — append-only 이력 목록
6. `data/radar/13f.json` — "45일 전 분기말 기준" 워터마크
7. `data/watchlist/current.json`(있으면) — 티커 목록
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.ledger.record import RecordError, assert_publish_record  # noqa: E402
from engine.models import FORBIDDEN_KEYS, QuantSignalSchemaError, validate_quant_signals  # noqa: E402

SIGNALS = Path("data/quant_signals.json")
RECORDS = Path("data/ledger/records.jsonl")
PILOT = Path("data/ledger/pilot_eval.json")
PORTFOLIO = Path("data/ledger/portfolio/state.json")
PARAMS_HISTORY = Path("data/params_history.json")
RADAR = Path("data/radar/13f.json")
WATCHLIST = Path("data/watchlist/current.json")

PILOT_METRICS = ("n_eff", "net_after_cost", "cash_equivalent", "tail_loss")
PILOT_VERDICTS = ("보류", "소액 파일럿 후보")
STATUS = ("normal", "degraded", "stale", "sample", "awaiting_first_run")


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_signals(problems: list[str], notes: dict) -> None:
    if not SIGNALS.exists():
        problems.append(f"{SIGNALS}: 파일 없음 — 계약 데이터가 없다")
        return
    try:
        payload = _read_json(SIGNALS)
    except json.JSONDecodeError as exc:
        problems.append(f"{SIGNALS}: JSON 파싱 실패({exc})")
        return
    try:
        summary = validate_quant_signals(payload)
    except QuantSignalSchemaError as exc:
        problems.extend(f"{SIGNALS}: {e}" for e in exc.errors[:12])
        return
    bad = sorted(set(_keys(payload)) & FORBIDDEN_KEYS)
    if bad:
        problems.append(f"{SIGNALS}: 공개 금지 필드 {bad}")
    status = payload.get("status")
    if status not in STATUS:
        problems.append(f"{SIGNALS}: 알 수 없는 status {status!r} (허용: {STATUS})")
    notes["signals"] = {**summary, "status": status}
    for record in payload.get("signals") or []:
        try:
            assert_publish_record(record)
        except RecordError as exc:
            problems.append(f"{SIGNALS}: {record.get('signal_id')} — {exc}")


def check_records(problems: list[str], notes: dict) -> None:
    if not RECORDS.exists():
        notes["records"] = {"n": 0, "note": "파일 없음(첫 실행 전 정상)"}
        return
    rows = []
    for i, line in enumerate(RECORDS.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            problems.append(f"{RECORDS}:{i}: JSON 파싱 실패({exc})")
    ids = [r.get("signal_id") for r in rows]
    if len(ids) != len(set(ids)):
        problems.append(f"{RECORDS}: signal_id 중복")
    for record in rows:
        try:
            assert_publish_record(record)
        except RecordError as exc:
            problems.append(f"{RECORDS}: {record.get('signal_id')} — {exc}")
        bad = sorted(set(_keys(record)) & FORBIDDEN_KEYS)
        if bad:
            problems.append(f"{RECORDS}: {record.get('signal_id')} 금지 필드 {bad}")
    notes["records"] = {"n": len(rows)}


def check_pilot(problems: list[str], notes: dict) -> None:
    if not PILOT.exists():
        problems.append(f"{PILOT}: 파일 없음 — 파일럿 4지표를 남겨야 판정이 난다(4.7.3)")
        return
    ev = _read_json(PILOT)
    keys = {m.get("key") for m in ev.get("metrics") or []}
    missing = [k for k in PILOT_METRICS if k not in keys]
    if missing:
        problems.append(f"{PILOT}: 지표 누락 {missing} — 4개가 모두 있어야 판정한다")
    if ev.get("verdict") not in PILOT_VERDICTS:
        problems.append(f"{PILOT}: 알 수 없는 판정 {ev.get('verdict')!r} (허용: {PILOT_VERDICTS})")
    notes["pilot"] = {"verdict": ev.get("verdict"), "n_eff": ev.get("n_eff")}


def check_portfolio(problems: list[str], notes: dict) -> None:
    if not PORTFOLIO.exists():
        problems.append(f"{PORTFOLIO}: 파일 없음 — 모의 계좌 상태(비중)가 필요하다")
        return
    state = _read_json(PORTFOLIO)
    bad = sorted(set(_keys(state)) & FORBIDDEN_KEYS)
    if bad:
        problems.append(f"{PORTFOLIO}: 공개 금지 필드 {bad}")
    cash, invested = state.get("cash_pct"), state.get("invested_pct")
    if cash is None or invested is None:
        problems.append(f"{PORTFOLIO}: cash_pct/invested_pct 누락")
    elif abs(float(cash) + float(invested) - 1.0) > 0.01:
        problems.append(f"{PORTFOLIO}: cash_pct({cash}) + invested_pct({invested}) 가 1 이 아니다")
    notes["portfolio"] = {"cash_pct": cash, "invested_pct": invested,
                          "n_positions": state.get("n_positions")}


def check_params_history(problems: list[str], notes: dict) -> None:
    if not PARAMS_HISTORY.exists():
        problems.append(f"{PARAMS_HISTORY}: 파일 없음 — 파라미터 버전 이력이 필요하다")
        return
    history = _read_json(PARAMS_HISTORY)
    if not isinstance(history, list) or not history:
        problems.append(f"{PARAMS_HISTORY}: 비어 있거나 목록이 아니다")
        return
    versions = [h.get("version") for h in history]
    if len(versions) != len(set(versions)):
        problems.append(f"{PARAMS_HISTORY}: 버전 중복")
    notes["params_history"] = versions


def check_radar(problems: list[str], notes: dict) -> None:
    if not RADAR.exists():
        problems.append(f"{RADAR}: 파일 없음 — 레이더는 빈 정상 상태라도 남긴다")
        return
    radar = _read_json(RADAR)
    if "watermark_ko" not in radar:
        problems.append(f"{RADAR}: 워터마크(watermark_ko) 누락 — 45일 시차를 표시해야 한다")
    if not isinstance(radar.get("filers"), list):
        problems.append(f"{RADAR}: filers 목록 누락")
    notes["radar"] = {"n_filers": radar.get("n_filers"), "watermark": radar.get("watermark_ko")}


def check_watchlist(problems: list[str], notes: dict) -> None:
    if not WATCHLIST.exists():
        notes["watchlist"] = {"note": "파일 없음(주간 스크린 전 정상)"}
        return
    payload = _read_json(WATCHLIST)
    rows = payload.get("watchlist") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        problems.append(f"{WATCHLIST}: watchlist 목록 누락")
        return
    notes["watchlist"] = {"n": len(rows)}


def main(argv: list[str] | None = None) -> int:
    problems: list[str] = []
    notes: dict = {}
    for check in (check_signals, check_records, check_pilot, check_portfolio,
                  check_params_history, check_radar, check_watchlist):
        try:
            check(problems, notes)
        except Exception as exc:  # noqa: BLE001 — 검사기 자체가 죽으면 그것도 실패다
            problems.append(f"{check.__name__}: {type(exc).__name__}: {exc}")
    if problems:
        for problem in problems:
            print(f"ERROR {problem}", file=sys.stderr)
        print(json.dumps({"ok": False, "problems": problems, "notes": notes}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "notes": notes}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
