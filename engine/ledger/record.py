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


# P1-2 [v1.3.4]: 예전 `record.debate_event()`(호출자가 `eligible`·`late` 를 넘기던 구 경로)는
# **삭제**했다. 편입 이벤트는 `engine.ledger.debate.debate_event(report, record, …)` 가 시각 계약을
# 스스로 계산해 만든다 — 같은 규칙이 두 벌로 갈라지면 "호출자가 틀린 값"이 그대로 기록된다.

#: `{as_of}-{ticker}-{BUY|WATCH}-{회차 3자리}`
SIGNAL_SEQ_RE = re.compile(r"^(?P<base>.+)-(?P<seq>\d{3})$")

#: 발행 묶음을 가르는 "판단" 축 — 같은 날 같은 종목이라도 BUY/WATCH/차단은 **다른 발행**이다.
RELEASE_KINDS = ("BUY", "WATCH", "BLOCKED")


def split_signal_id(signal_id: str) -> tuple[str, int]:
    """`…-001` → (`…`, 1). 접미사가 없으면 (`그대로`, 1)."""
    match = SIGNAL_SEQ_RE.match(str(signal_id or ""))
    if not match:
        return str(signal_id or ""), 1
    return match.group("base"), int(match.group("seq"))


def release_group(record: dict) -> tuple[str, str]:
    """같은 발행 묶음의 키 — `(as_of, ticker)`.

    [v1.3.4 · P0-1] 예전 키는 `(as_of, ticker, type)` 이었다. 그래서 같은 날 같은 종목의 판단이
    BUY→WATCH 로 바뀌어도 **다른 묶음**이 되어 이전 BUY 를 대체하지 못했고, 최신 판단이 WATCH 인데
    BUY 가 그대로 체결됐다(감사 P0-1 재현: 체결 1건, 기대 0건). 판단 종류는 **회차 내용**이지
    묶음이 아니다 — 한 종목·한 날의 회차는 하나의 계보를 이룬다.
    """
    return (str(record.get("as_of") or ""), str(record.get("ticker") or ""))


def release_fingerprint(record: dict) -> tuple:
    """같은 발행인지 판단하는 **입력 지문** — 판단 입력·컷오프·파라미터·**판단 종류**.

    `inputs_hash`(입력·보유·섹터비중·파라미터의 canonical 해시)가 있으면 그 값이 기준이다.
    구 레코드처럼 없으면 `(snapshot_id, data_cutoff_at, params_version)` 로 대신한다.

    [v1.3.4] 판단 종류(`type`)·의도(`quant_intent`)·등급도 지문에 넣는다. 입력이 같아도
    BUY→WATCH 로 바뀐 재실행은 **새 발행**이어야 하는데, 예전 지문은 입력만 보아
    "같은 발행"으로 재사용했다. `run_request_id` 가 있으면 요청 자체를 구분자로 더한다
    (별개 요청의 재전송을 같은 발행으로 접지 않는다).
    """
    decision = (str(record.get("type") or "").upper(), str(record.get("quant_intent") or ""),
                str(record.get("grade") or ""))
    request = record.get("run_request_id") or ""
    if record.get("inputs_hash"):
        return ("inputs_hash", str(record["inputs_hash"]), *decision, str(request))
    return ("legacy", str(record.get("snapshot_id") or ""),
            str(record.get("data_cutoff_at") or ""), str(record.get("params_version") or ""),
            *decision, str(request))


def resolve_release(record: dict, existing: list[dict]) -> dict:
    """새 발행본의 회차를 정한다 — `append_immutable` 과 충돌하지 않게.

    [v1.3.4 · P0-1] 두 가지를 고친다.

    1. **이미 대체된 발행본은 절대 재사용하지 않는다.** 같은 `(as_of, ticker)` 계보에서 다른 회차가
       `supersedes` 로 가리킨 발행본은 "과거"다. 입력 A→B→A 에서 마지막 A 가 이미 B 에 대체된
       A-001 을 다시 고르면 ① 화면에는 대체된 회차가 나가고 ② 실제 체결은 더 이상 최신이 아닌 회차가
       된다(감사 P0-1 재현). 지금 유효한 회차 중에서만 지문을 비교하고, 아니면 **새 회차(003)** 를 만든다.
    2. 같은 `run_request_id` 의 **재전송**일 때만 원 응답을 재사용한다. 요청 id 가 다르면 같은 입력이라도
       새 요청이므로 새 회차다.

    Returns: `{"signal_id", "revision", "supersedes", "duplicate", "reused_from"}`
    """
    group = release_group(record)
    same = [r for r in existing if release_group(r) == group]
    superseded = superseded_ids(existing)
    live = [r for r in same if str(r.get("signal_id")) not in superseded]
    fingerprint = release_fingerprint(record)
    request_id = str(record.get("run_request_id") or "")
    for old in sorted(live, key=lambda r: split_signal_id(str(r.get("signal_id")))[1], reverse=True):
        if release_fingerprint(old) != fingerprint:
            continue
        if request_id and str(old.get("run_request_id") or "") not in ("", request_id):
            continue        # 다른 요청의 재전송 — 원 응답을 주지 않는다
        return {"signal_id": str(old.get("signal_id")), "revision": int(old.get("revision") or 1),
                "supersedes": old.get("supersedes"), "duplicate": True,
                "reused_from": str(old.get("signal_id"))}
    base, _ = split_signal_id(str(record.get("signal_id") or ""))
    last_seq = max((split_signal_id(str(r.get("signal_id")))[1] for r in same), default=0)
    revision = last_seq + 1
    latest = max(live, key=lambda r: split_signal_id(str(r.get("signal_id")))[1]) if live else None
    if latest is None and same:
        latest = max(same, key=lambda r: split_signal_id(str(r.get("signal_id")))[1])
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


def live_releases(records: list[dict], *, as_of: str | None = None,
                  ticker: str | None = None) -> list[dict]:
    """지금 유효한(아무도 대체하지 않은) 발행본 — 회차 번호 오름차순.

    `as_of`·`ticker` 를 주면 그 묶음만 본다. WATCH·차단 회차 생성과 정산이 같은 기준을 쓴다.
    """
    superseded = superseded_ids(records)
    rows = [r for r in records or [] if str(r.get("signal_id")) not in superseded]
    if as_of is not None:
        rows = [r for r in rows if str(r.get("as_of") or "")[:10] == str(as_of)[:10]]
    if ticker is not None:
        rows = [r for r in rows if str(r.get("ticker") or "") == str(ticker)]
    return sorted(rows, key=lambda r: split_signal_id(str(r.get("signal_id")))[1])


def superseding_release(existing: list[dict], *, ticker: str, as_of: str, type: str,
                        data_cutoff_at, signal_published_at, order_cutoff_at,
                        target_session: str | None = None, grade: str = "NONE",
                        entry_score: float = 0.0, reason_ko: str = "",
                        snapshot_id: str = "", params_version: str = "params_v001",
                        code_commit: str = "", inputs_hash: str | None = None,
                        run_request_id: str | None = None,
                        generated_price: float | None = None,
                        target_band: dict | None = None) -> dict:
    """[v1.3.4 · P0-1] WATCH·차단(BLOCKED)도 **회차로 기록**해 직전 BUY 를 대체한다.

    예전에는 BUY 가 아닌 판단이 발행 레코드를 만들지 않아, 같은 날 판단이 WATCH·차단으로 바뀌어도
    이전 BUY 가 원장에 그대로 남아 다음 세션 시가에 체결됐다(감사 P0-1 재현). 이 헬퍼는
    `quant_intent="no_action"` 인 최소 발행 레코드를 만들어 `resolve_release()` 로 회차를 받고
    `supersedes` 로 직전 회차를 가리키게 한다. 발행 레코드는 여전히 **불변**이다 — 원장에 append 만 한다.

    `existing` 에서 같은 `(as_of, ticker)` 의 직전 발행을 찾아 가격·밴드를 물려받는다(없으면 제네릭).
    """
    kind = str(type or "").upper()
    if kind not in RELEASE_KINDS[1:]:
        raise RecordError(f"superseding_release 는 WATCH·BLOCKED 만 만든다: {type!r}")
    prior = live_releases(existing, as_of=as_of, ticker=ticker)
    template = prior[-1] if prior else {}
    price = generated_price
    if price is None:
        price = template.get("generated_price")
    band = target_band or (template.get("target_band") if template else None)
    if price is None and band:
        price = float((band or {}).get("base_price") or 0.0) or None
    if price is None:
        raise RecordError("superseding_release 에 generated_price 또는 이전 발행본이 필요하다")
    price = float(price)
    band = dict(band or {"lo": price * 1.10, "hi": price * 1.30, "horizon_td": 63,
                         "base_price": price})
    record = publish_record(
        signal_id=f"{as_of}-{ticker}-{kind}-001", ticker=ticker, type=kind,
        grade=str(grade or "NONE"), entry_score=float(entry_score or 0.0), as_of=as_of,
        data_cutoff_at=data_cutoff_at, signal_published_at=signal_published_at,
        order_cutoff_at=order_cutoff_at, target_session=target_session,
        generated_price=price, target_band=band, expected_mdd=None,
        thesis_ko=reason_ko or ("관찰 — 신규 진입 없음" if kind == "WATCH" else "차단 — 신규 진입 없음"),
        plan_weight=0.0, stop_distance=0.0, quant_intent="no_action",
        snapshot_id=snapshot_id, params_version=params_version, code_commit=code_commit,
        inputs_hash=inputs_hash,
    )
    if run_request_id:
        record["run_request_id"] = str(run_request_id)
    release = resolve_release(record, existing)
    record["signal_id"] = release["signal_id"]
    record["revision"] = int(release["revision"])
    record["supersedes"] = release["supersedes"]
    record["release_kind"] = kind
    return record


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
