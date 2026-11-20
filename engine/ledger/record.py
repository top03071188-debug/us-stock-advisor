"""engine.ledger.record — 발행 기록(불변) · 체결 이벤트(append-only) (기획서 4.7.1).

[고정 — v1.3.1]
- 신호가 발행되는 순간의 기록에는 **의도(`quant_intent`)만** 있다.
  `entry_plan[].price_ref` 는 잠정가(`provisional: true`), `position.stop_price`·
  `planned_loss_after_fill_pct`·`execution_event` 는 `None` 이다. **체결가를 미리 박지 않는다.**
- 시가가 확정된 뒤 `data/ledger/executions/{signal_id}.json` 에 **체결 이벤트를 따로 append** 한다.
  발행 레코드는 한 글자도 고치지 않는다.
- 토론은 `data/ledger/debate_events/{signal_id}.json` 에 따로 쌓는다(발행 레코드의 `track` 은 `quant_only`).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

INTENTS = ("pending_buy", "pending_hold_cash", "no_action", "pending_exit")
EXECUTION_EVENTS = ("buy_filled", "hold_cash", "no_action", "exit_filled")
TRACKS = ("quant_only", "codex_debated")
TRANCHE_WEIGHTS = (0.40, 0.30, 0.30)
TRANCHE_DROPS = (0.0, -0.07, -0.14)


class RecordError(ValueError):
    """발행 기록 규칙 위반(체결가 선기입·시각 순서·불변성)."""


def _ts(value) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise RecordError(f"시각에 시간대가 없다: {value!r} (Z 또는 오프셋 필요)")
    return ts


def check_three_times(data_cutoff_at, signal_published_at, order_cutoff_at) -> None:
    """`data_cutoff_at ≤ signal_published_at < order_cutoff_at` [고정, 2.2.2]."""
    a, b, c = _ts(data_cutoff_at), _ts(signal_published_at), _ts(order_cutoff_at)
    if not (a <= b < c):
        raise RecordError(
            f"세 시각 순서 위반: data_cutoff_at({a.isoformat()}) ≤ signal_published_at({b.isoformat()}) "
            f"< order_cutoff_at({c.isoformat()}) 를 만족해야 한다."
        )


def publish_record(*, signal_id: str, ticker: str, type: str, grade: str, entry_score: float,
                   as_of: str, data_cutoff_at, signal_published_at, order_cutoff_at,
                   generated_price: float, target_band: dict, expected_mdd: float | None,
                   thesis_ko: str = "", evidence: list[dict] | None = None,
                   investor_scores: dict | None = None, conflict: bool = False,
                   position: dict | None = None, plan_weight: float = 0.0,
                   stop_distance: float = 0.0, veto: list[dict] | None = None,
                   params_version: str = "params_v001", snapshot_id: str = "",
                   code_commit: str = "", invalidation: dict | None = None,
                   quant_intent: str = "pending_buy", tranche_weights=TRANCHE_WEIGHTS,
                   tranche_drops=TRANCHE_DROPS) -> dict:
    """발행 시점의 **불변** 레코드. 체결가·손절가는 비어 있다."""
    check_three_times(data_cutoff_at, signal_published_at, order_cutoff_at)
    if quant_intent not in INTENTS:
        raise RecordError(f"알 수 없는 intent: {quant_intent!r} (허용: {INTENTS})")
    plan = []
    for i, (w, d) in enumerate(zip(tranche_weights, tranche_drops), start=1):
        price_ref = round(float(generated_price) * (1.0 + d), 2)
        row = {"tranche": i, "weight": float(w), "price_ref": price_ref, "provisional": True}
        if i == 3:
            provisional_stop = float(generated_price) * (1.0 - float(stop_distance))
            row["active"] = price_ref > provisional_stop
            if not row["active"]:
                row["note"] = "잠정 손절가보다 낮아 손절선 밖 → 1차 체결가로 재확인"
        plan.append(row)
    base = float(target_band.get("base_price", generated_price))
    record = {
        "schema_version": 1,
        "signal_id": signal_id,
        "ticker": ticker,
        "type": type,
        "grade": grade,
        "entry_score": round(float(entry_score), 2),
        "as_of": as_of,
        "data_cutoff_at": _iso(data_cutoff_at),
        "signal_published_at": _iso(signal_published_at),
        "order_cutoff_at": _iso(order_cutoff_at),
        "generated_price": float(generated_price),
        "quant_intent": quant_intent,
        "entry_plan": plan,
        "target_band": {
            "lo": round(float(target_band["lo"]), 4),
            "hi": round(float(target_band["hi"]), 4),
            "horizon_td": int(target_band.get("horizon_td", 63)),
            "basis": "base_price",
            "base_price": base,
            "r_lo": round(float(target_band["lo"]) / base - 1.0, 6),
            "r_hi": round(float(target_band["hi"]) / base - 1.0, 6),
        },
        "expected_mdd": None if expected_mdd is None else round(float(expected_mdd), 4),
        "invalidation": invalidation,
        "thesis_ko": thesis_ko,
        "evidence": evidence or [],
        "investor_scores": investor_scores or {},
        "conflict": bool(conflict),
        "position": {
            "plan_weight_target": round(float(plan_weight), 6),
            "tranche": 1 if quant_intent == "pending_buy" else 0,
            "tranche_weight": round(float(plan_weight) * float(tranche_weights[0]), 6),
            "stop_distance": round(float(stop_distance), 6),
            "stop_price": None,
            "planned_loss_after_fill_pct": None,
        },
        "debate_ref": None,
        "execution_event": None,
        "track": "quant_only",
        "veto": veto or [],
        "params_version": params_version,
        "snapshot_id": snapshot_id,
        "code_commit": code_commit,
    }
    assert_publish_record(record)
    return record


def _iso(value) -> str:
    ts = _ts(value)
    return ts.isoformat().replace("+00:00", "Z")


def assert_publish_record(record: dict) -> None:
    """발행 레코드 규칙 — 체결가·손절가·행동이 미리 들어 있으면 실패(2.2.2, 4.7.1)."""
    problems: list[str] = []
    if record.get("quant_intent") not in INTENTS:
        problems.append(f"quant_intent={record.get('quant_intent')!r}")
    if record.get("execution_event") is not None:
        problems.append("execution_event 는 발행 시점에 None 이어야 한다")
    pos = record.get("position") or {}
    if pos.get("stop_price") is not None:
        problems.append("position.stop_price 는 발행 시점에 None")
    if pos.get("planned_loss_after_fill_pct") is not None:
        problems.append("position.planned_loss_after_fill_pct 는 발행 시점에 None")
    for row in record.get("entry_plan") or []:
        if row.get("provisional") is not True:
            problems.append(f"entry_plan[{row.get('tranche')}].provisional 이 True 가 아니다")
        if "fill_price" in row:
            problems.append(f"entry_plan[{row.get('tranche')}] 에 fill_price 가 있다")
    for key in ("fill_price", "shares", "avg_cost", "slippage_bps"):
        if key in record:
            problems.append(f"발행 레코드에 체결 필드 {key} 가 있다")
    try:
        check_three_times(record["data_cutoff_at"], record["signal_published_at"], record["order_cutoff_at"])
    except (RecordError, KeyError) as exc:
        problems.append(str(exc))
    if problems:
        raise RecordError("; ".join(problems))


def execution_event(*, signal_id: str, snapshot_id: str, at, execution_event: str,
                    tranche: int = 1, fill_price: float | None = None,
                    slippage_bps: float = 10.0, weight: float = 0.0,
                    stop_price: float | None = None, trailing_high: float | None = None,
                    planned_loss_pct: float | None = None, shares: float | None = None,
                    slippage_bps_override: float | None = None, note: str = "") -> dict:
    """시가 확정 뒤 append 하는 체결 이벤트 (data/ledger/executions/{signal_id}.json)."""
    if execution_event not in EXECUTION_EVENTS:
        raise RecordError(f"알 수 없는 execution_event: {execution_event!r}")
    if execution_event in ("buy_filled", "exit_filled") and fill_price is None:
        raise RecordError("체결 이벤트에는 fill_price 가 필요하다")
    if execution_event == "hold_cash" and fill_price is not None:
        raise RecordError("hold_cash 는 체결이 아니다 — fill_price 는 None, 가상 체결가는 virtual_fill_price")
    return {
        "signal_id": signal_id,
        "snapshot_id": snapshot_id,
        "at": _iso(at),
        "quant_track": {
            "execution_event": execution_event,
            "tranche": tranche,
            "fill_price": None if fill_price is None else round(float(fill_price), 4),
            "slippage_bps": float(slippage_bps_override if slippage_bps_override is not None else slippage_bps),
            "weight": round(float(weight), 6),
            "stop_price": None if stop_price is None else round(float(stop_price), 4),
            "trailing_high": None if trailing_high is None else round(float(trailing_high), 4),
            "planned_loss_pct": None if planned_loss_pct is None else round(float(planned_loss_pct), 6),
            "shares": None if shares is None else round(float(shares), 6),
        },
        "debate_track": None,
        "note": note or None,
    }


def reanchor_event(*, signal_id: str, snapshot_id: str, at, first_fill: float,
                   stop_price: float, plan: list[dict]) -> dict:
    """1차 실제 체결가 기준 분할가 재고정 — 원본 발행 행은 남긴다(4.7.1)."""
    return {
        "signal_id": signal_id,
        "snapshot_id": snapshot_id,
        "at": _iso(at),
        "execution_event": "plan_reanchor",
        "first_fill": round(float(first_fill), 4),
        "stop_price": round(float(stop_price), 4),
        "plan": [
            {"tranche": int(p["tranche"]), "price_ref": round(float(p["price_ref"]), 2),
             "active": bool(p.get("active", True)), "provisional": False,
             "note": p.get("note")}
            for p in plan
        ],
    }


def debate_event(*, signal_id: str, snapshot_id: str, report_id: str, intent: str,
                 completed_at, recorded_at, eligible: bool, late: bool,
                 auto_downgrade_applied: bool = False) -> dict:
    """토론 편입 이벤트 — 발행 레코드가 아니라 이 파일에만 쌓는다(3.11.8)."""
    if intent not in INTENTS:
        raise RecordError(f"알 수 없는 토론 intent: {intent!r}")
    return {
        "signal_id": signal_id,
        "snapshot_id": snapshot_id,
        "events": [{
            "event": "debate_attached",
            "report_id": report_id,
            "track": "codex_debated",
            "intent": intent,
            "completed_at": _iso(completed_at),
            "recorded_at": _iso(recorded_at),
            "eligible": bool(eligible),
            "late": bool(late),
            "auto_downgrade_applied": bool(auto_downgrade_applied),
        }],
    }


def append_immutable(path: str | Path, record: dict, key: str = "signal_id") -> Path:
    """append-only JSONL. 같은 키의 다른 내용은 거부한다(사후 수정 금지)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            old = json.loads(line)
            if old.get(key) == record.get(key):
                if old == record:
                    return p
                raise RecordError(f"{key}={record.get(key)} 가 다른 내용으로 이미 기록돼 있다 — 수정 금지")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return p


def load_records(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def merged_view(record: dict, execution: dict | None) -> dict:
    """발행 기록(의도·밴드·근거) + 체결 이벤트(행동·실제가) 합치기 — `action` 은 체결에서만 온다."""
    view = copy.deepcopy(record)
    if execution:
        track = execution.get("quant_track") or {}
        view["action"] = track.get("execution_event")
        view["fill"] = {
            "price": track.get("fill_price"),
            "tranche": track.get("tranche"),
            "shares": track.get("shares"),
            "stop_price": track.get("stop_price"),
            "slippage_bps": track.get("slippage_bps"),
            "planned_loss_pct": track.get("planned_loss_pct"),
        }
    else:
        view["action"] = None
        view["fill"] = None
    return view
