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
import re
from datetime import date, datetime
from pathlib import Path

from engine.utils.dates import is_trading_day, order_cutoff_at

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
                   tranche_drops=TRANCHE_DROPS, target_session: str | None = None,
                   signal_published_planned_at=None, inputs_hash: str | None = None) -> dict:
    """발행 시점의 **불변** 레코드. 체결가·손절가는 비어 있다.

    `target_session` 은 **실제 발행 시각** 기준 주문 대상 거래일(2.2.2, 3.4.4)이다.
    `signal_published_planned_at` 은 계획 시각(참고용) — 실제 시각과 다르면 화면이 함께 보여 준다.
    """
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
    band = {
        "lo": round(float(target_band["lo"]), 4),
        "hi": round(float(target_band["hi"]), 4),
        "horizon_td": int(target_band.get("horizon_td", 63)),
        "basis": "base_price",
        "base_price": base,
        "r_lo": round(float(target_band["lo"]) / base - 1.0, 6),
        "r_hi": round(float(target_band["hi"]) / base - 1.0, 6),
    }
    # 방어 목표(gap ≤ 0) 표시는 채점·화면이 함께 읽는 값이다 — 직렬화에서 조용히 떨어뜨리지 않는다(4.7.1).
    if target_band.get("defensive") is not None:
        band["defensive"] = bool(target_band["defensive"])
    # 채점 기준 봉 고정(감사 09-d) — 밴드와 수익률이 **같은 봉**을 가리키게 한다.
    band["return_base"] = str(target_band.get("return_base") or "base_price")
    if target_band.get("base_bar_date") is not None:
        band["base_bar_date"] = str(target_band["base_bar_date"])[:10]
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
        "signal_published_planned_at": (None if signal_published_planned_at is None
                                        else _iso(signal_published_planned_at)),
        "order_cutoff_at": _iso(order_cutoff_at),
        "target_session": target_session,
        "generated_price": float(generated_price),
        "quant_intent": quant_intent,
        "entry_plan": plan,
        "target_band": band,
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
    if inputs_hash:
        record["inputs_hash"] = str(inputs_hash)
        record["supersedes"] = None
        record["revision"] = 1
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
    try:
        check_target_session(record)
    except (RecordError, KeyError) as exc:
        problems.append(str(exc))
    if problems:
        raise RecordError("; ".join(problems))


def check_target_session(record: dict) -> None:
    """대상 거래일 불변식(2.2.2·3.4.4).

    - `target_session` 은 NYSE 거래일이어야 한다.
    - 그 세션의 주문 마감이 **실제 발행 시각**보다 뒤여야 한다(`order_cutoff_at > signal_published_at`).
      계획 시각으로 검사하면 마감을 넘긴 실행이 "제때 발행된 것처럼" 통과한다.
    """
    session = record.get("target_session")
    if session in (None, ""):
        return
    try:
        d = date.fromisoformat(str(session)[:10])
    except ValueError as exc:
        raise RecordError(f"target_session 형식 오류: {session!r}") from exc
    if not is_trading_day(d):
        raise RecordError(f"target_session {d.isoformat()} 은 휴장일이다 — 거래일만 쓸 수 있다")
    published = _ts(record["signal_published_at"])
    if not order_cutoff_at(d) > published:
        raise RecordError(
            f"target_session {d.isoformat()} 의 주문 마감({order_cutoff_at(d).isoformat()})이 "
            f"발행 시각({published.isoformat()})보다 이르다 — 장 전에 확정할 수 없다(3.4.4)."
        )
    cutoff = _ts(record["data_cutoff_at"])
    if not cutoff <= published:
        raise RecordError("data_cutoff_at 이 signal_published_at 보다 늦다")


def attach_target_session(record: dict, target_session: str | None,
                         signal_published_planned_at=None) -> dict:
    """발행 레코드에 대상 거래일(과 계획 시각)을 붙이고 불변식을 다시 검사한다.

    레코드는 `publish_record` 가 만들지만 `build_payload` 는 세 시각만 받는다. 그래서 실행
    스크립트가 실제 발행 시각으로 정한 `target_session` 을 여기서 붙인다(레코드 수정이 아니라
    **발행 시점에 함께 기록**하는 값이다 — 이후에는 append-only 로 불변).
    """
    if target_session:
        record["target_session"] = target_session
    if signal_published_planned_at is not None:
        record["signal_published_planned_at"] = _iso(signal_published_planned_at)
    check_target_session(record)
    return record


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


#: `{as_of}-{ticker}-{BUY|WATCH}-{회차 3자리}`
SIGNAL_SEQ_RE = re.compile(r"^(?P<base>.+)-(?P<seq>\d{3})$")


def split_signal_id(signal_id: str) -> tuple[str, int]:
    """`…-001` → (`…`, 1). 접미사가 없으면 (`그대로`, 1)."""
    match = SIGNAL_SEQ_RE.match(str(signal_id or ""))
    if not match:
        return str(signal_id or ""), 1
    return match.group("base"), int(match.group("seq"))


def release_group(record: dict) -> tuple[str, str, str]:
    """같은 발행 묶음의 키 — `(as_of, ticker, type)`. 회차만 다른 같은 종목·같은 날의 발행."""
    return (str(record.get("as_of") or ""), str(record.get("ticker") or ""), str(record.get("type") or ""))


def release_fingerprint(record: dict) -> tuple:
    """같은 발행인지 판단하는 **입력 지문** — 발행 시각·스냅샷 경로가 아니라 판단 입력·컷오프·파라미터.

    `inputs_hash`(입력·보유·섹터비중·파라미터의 canonical 해시)가 있으면 그 값이 기준이다.
    구 레코드처럼 없으면 `(snapshot_id, data_cutoff_at, params_version)` 로 대신한다.
    """
    if record.get("inputs_hash"):
        return ("inputs_hash", str(record["inputs_hash"]))
    return ("legacy", str(record.get("snapshot_id") or ""),
            str(record.get("data_cutoff_at") or ""), str(record.get("params_version") or ""))


def resolve_release(record: dict, existing: list[dict]) -> dict:
    """새 발행본의 회차를 정한다 — `append_immutable` 과 충돌하지 않게.

    - 같은 `(as_of, ticker, type)` 의 기존 발행 중 **입력 지문이 같은 것** 이 있으면 그 발행본을 재사용
      한다(`duplicate=True`, `signal_id` 는 기존 것). 같은 날 같은 입력으로 다시 돌린 재시도다.
    - 지문이 다르면(입력·컷오프·파라미터가 바뀌어 **새로 확정**된 발행) 다음 회차 접미사(`002`, `003`…)를
      붙이고 `supersedes` 로 직전 발행본을 가리킨다. ``…-001`` 을 다른 내용으로 덮어쓰지 않는다.

    Returns: `{"signal_id", "revision", "supersedes", "duplicate", "reused_from"}`
    """
    group = release_group(record)
    same = [r for r in existing if release_group(r) == group]
    fingerprint = release_fingerprint(record)
    for old in sorted(same, key=lambda r: split_signal_id(str(r.get("signal_id")))[1], reverse=True):
        if release_fingerprint(old) == fingerprint:
            return {"signal_id": str(old.get("signal_id")), "revision": int(old.get("revision") or 1),
                    "supersedes": old.get("supersedes"), "duplicate": True,
                    "reused_from": str(old.get("signal_id"))}
    base, _ = split_signal_id(str(record.get("signal_id") or ""))
    last_seq = max((split_signal_id(str(r.get("signal_id")))[1] for r in same), default=0)
    revision = last_seq + 1
    latest = max(same, key=lambda r: split_signal_id(str(r.get("signal_id")))[1]) if same else None
    return {"signal_id": f"{base}-{revision:03d}", "revision": revision,
            "supersedes": str(latest.get("signal_id")) if latest is not None else None,
            "duplicate": False, "reused_from": None}


def superseded_ids(records: list[dict]) -> set[str]:
    """다른 발행본에 `supersedes` 로 대체된 이전 회차 id — 정산·표시에서 제외한다."""
    out: set[str] = set()
    for record in records or []:
        old = record.get("supersedes")
        if old:
            out.add(str(old))
    return out


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
