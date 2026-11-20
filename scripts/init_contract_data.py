#!/usr/bin/env python3
"""scripts/init_contract_data.py — **첫 실행 전** 계약 데이터의 정직한 초기 상태를 만든다.

왜 필요한가: `deploy.yml` 은 사람이 푸시할 때마다 `build_public_data.py` 로 사이트를 만든다.
`data/quant_signals.json` 이 아예 없으면 빌드가 "데이터 없음"이 되고, 가짜 신호를 넣으면
"실전 신호인 척" 하는 더 나쁜 상태가 된다. 그래서 **비어 있음이 정상임을 명시한 상태**
(`status: awaiting_first_run`)를 계약 형식 그대로 둔다.

    python scripts/init_contract_data.py            # 없는 파일만 만든다
    python scripts/init_contract_data.py --force    # 초기 상태로 다시 만든다(기존 기록 삭제 위험)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.decide.macro import classify_regime  # noqa: E402
from engine.decide.pipeline import load_params  # noqa: E402
from engine.utils.dates import data_cutoff_at, fold_summary, order_cutoff_at, signal_published_at  # noqa: E402

SIGNALS = Path("data/quant_signals.json")
RECORDS = Path("data/ledger/records.jsonl")
PILOT = Path("data/ledger/pilot_eval.json")
PORTFOLIO = Path("data/ledger/portfolio/state.json")
PARAMS_HISTORY = Path("data/params_history.json")
RADAR = Path("data/radar/13f.json")
WATCHLIST = Path("data/watchlist/current.json")
FOLDS = Path("data/walkforward/folds/summary.json")

NOTE = "아직 실전 배치가 한 번도 돌지 않았습니다 — 신호가 비어 있는 것이 정상입니다."


def _write(path: Path, payload, force: bool) -> str:
    if path.exists() and not force:
        return f"skip(exists): {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"written: {path}"


def signals_payload(as_of: date, params_version: str) -> dict:
    cutoff = data_cutoff_at(as_of, "postclose")
    published = signal_published_at(cutoff)
    order = order_cutoff_at(as_of)
    from engine.utils.dates import UTC  # noqa: PLC0415
    iso = lambda d: d.astimezone(UTC).isoformat().replace("+00:00", "Z")  # noqa: E731
    regime = classify_regime({}, None).as_dict()
    return {
        "schema_version": 1,
        "generated_at": iso(published),
        "data_cutoff_at": iso(cutoff),
        "signal_published_at": iso(published),
        "order_cutoff_at": iso(order),
        "as_of": as_of.isoformat(),
        "params_version": params_version,
        "snapshot_id": "sha256:" + "0" * 64,
        "code_commit": "init",
        "status": "awaiting_first_run",
        "mode": "postclose",
        "source": "none",
        "regime": regime,
        "signals": [],
        "blocked": [],
        "watchlist": [],
        "watchlist_count": 0,
        "ledger_summary": {
            "schema_version": 1,
            "as_of": as_of.isoformat(),
            "n_signals": 0,
            "n_graded_3m": 0,
            "accuracy": {"label_ko": "신호 정확도", "agreement_3m": None, "ic_3m": None,
                         "win_rate": None, "mean_excess": None, "benchmark_connected": False,
                         "note_ko": "채점 표본 없음"},
            "strategy": {"label_ko": "전략(모의 계좌)", "equity_index": 1.0, "cash_pct": 1.0,
                         "invested_pct": 0.0, "n_positions": 0},
            "horizons": {},
            "folds": {},
            "pilot": {"verdict": "보류", "reason_ko": "채점 표본 0건 — 판정 보류(4.7.3)"},
            "params_history": [],
            "signal_history": [],
            "debate_effect": {"pairs": 0, "net_pp": None, "verdict": "표본 0건 — 판정 보류"},
            "note_ko": NOTE,
        },
        "portfolio": {"as_of": as_of.isoformat(), "cash_pct": 1.0, "invested_pct": 0.0,
                      "equity_index": 1.0, "positions": []},
        "pilot_eval": {"verdict": "보류", "reason_ko": "채점 표본 0건 — 판정 보류(4.7.3)", "n_eff": 0,
                       "metrics": [
                           {"key": "n_eff", "label_ko": "유효 표본 N_eff", "value": 0,
                            "threshold": "≥ 15", "pass": False},
                           {"key": "net_after_cost", "label_ko": "비용 후 순수익(SPY 대비)",
                            "value": None, "threshold": "> 0", "pass": None},
                           {"key": "cash_equivalent", "label_ko": "유사 현금비중 벤치마크 대비",
                            "value": None, "threshold": "> 0", "pass": None},
                           {"key": "tail_loss", "label_ko": "손실 구간(하위 20%) 평균",
                            "value": None, "threshold": "≥ -0.08", "pass": None},
                       ]},
        "charts": {},
        "position_checks": [],
        "diagnostics": {"mode": "init", "degraded": [], "sources": {}},
        "note_ko": NOTE,
    }


def pilot_payload(as_of: date) -> dict:
    return {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "horizon": "3m",
        "n_records": 0,
        "n_graded": 0,
        "n_eff": 0,
        "metrics": [
            {"key": "n_eff", "label_ko": "유효 표본 N_eff", "value": 0, "threshold": "≥ 15",
             "pass": False, "note_ko": "신호 0건"},
            {"key": "net_after_cost", "label_ko": "비용 후 순수익(SPY 대비)", "value": None,
             "threshold": "> 0", "pass": None, "note_ko": "채점 표본 없음"},
            {"key": "cash_equivalent", "label_ko": "유사 현금비중 벤치마크 대비", "value": None,
             "threshold": "> 0", "pass": None, "note_ko": "계좌 곡선 없음"},
            {"key": "tail_loss", "label_ko": "손실 구간(하위 20%) 평균", "value": None,
             "threshold": "≥ -0.08", "pass": None, "note_ko": "채점 표본 없음"},
        ],
        "verdict": "보류",
        "verdict_reason_ko": "채점 표본 0건 — 판정 보류(4.7.3)",
        "note_ko": "55점·승률 52% 는 참고치다 — 게이트는 이 4지표로만 판단한다.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="첫 실행 전 계약 데이터 초기화")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    params = load_params()
    version = str(params.get("version") or "params_v001")
    results = [
        _write(SIGNALS, signals_payload(as_of, version), args.force),
        _write(PILOT, pilot_payload(as_of), args.force),
        _write(PORTFOLIO, {"as_of": as_of.isoformat(), "cash_pct": 1.0, "invested_pct": 0.0,
                           "equity_index": 1.0, "positions": [], "n_positions": 0,
                           "sector_weights": {}, "note_ko": "모의 계좌 — 첫 체결 전"}, args.force),
        _write(PARAMS_HISTORY, [{
            "version": version, "created_at": str(params.get("created_at") or as_of.isoformat()),
            "parent_version": None, "gate_result": {"status": "active",
                                                    "note_ko": "워크포워드 보정은 PR 로만 반영한다(4.3)"},
            "changed": [], "note_ko": "초기 버전 — 보정 이력 없음",
        }], args.force),
        _write(RADAR, {"as_of": as_of.isoformat(), "watermark_ko": "45일 전 분기말 기준",
                       "period_end": None, "lag_days": 45, "stale": True, "stale_days": None,
                       "filers": [], "n_filers": 0,
                       "counts": {"new": 0, "increase": 0, "reduce": 0, "exit": 0},
                       "watchlist_overlap": [],
                       "note_ko": "13F 수집 이전 — 레이더는 비어 있는 것이 정상이다."}, args.force),
        _write(WATCHLIST, {"schema_version": 1, "as_of": as_of.isoformat(), "watchlist": [],
                           "count": 0, "excluded": [], "valuation_suspended": [],
                           "sector_counts": {}, "entered": [], "exited": [],
                           "notes": "주간 스크린 전 — 통과 종목 0개도 정상이다."}, args.force),
        _write(FOLDS, fold_summary(as_of), args.force),
    ]
    if not RECORDS.exists() or args.force:
        RECORDS.parent.mkdir(parents=True, exist_ok=True)
        RECORDS.write_text("", encoding="utf-8")
        results.append(f"written(empty): {RECORDS}")
    print(json.dumps({"as_of": as_of.isoformat(), "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
