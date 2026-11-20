"""engine.ledger.live — 실시간 Paper Ledger 운영 (기획서 4.7.1·4.7.2·4.12).

매일 postclose 실행에서 하는 일:

1. **발행 의도 정산** — 전일 신호(`quant_intent: pending_buy`)의 시가가 확정됐으면
   `data/ledger/executions/{signal_id}.json` 에 **체결 이벤트를 append** 한다.
   발행 레코드(`records.jsonl`)는 한 글자도 고치지 않는다.
2. **모의 계좌 상태 갱신** — 체결을 재생해 수량·평단을 만들고 당일 종가로 평가한다.
   **수량·평단·현금 금액은 커밋 파일에 넣지 않는다**(`shares`·`avg_cost`·`cash_balance` 는
   공개 금지 키, 2.2.3). 커밋되는 상태 파일에는 **비중(%)만** 남긴다.
3. **만기 도래 채점** — 1주·1개월·3개월·6개월(거래일 5/21/63/126) 채점. 만기 봉이 관측되지
   않으면 채점하지 않고 `complete: false` 로 남긴다. 홀드아웃 경계를 넘는 구간은 `truncated`
   로 참고 표시만 한다(4.2.1).

[고정]
- 체결은 **발행일의 대상 세션(주문 마감이 아직 오지 않은 가장 이른 세션) 시가 + 슬리피지 10bp**,
  1차는 **장 전 커밋 범위**(`entry_band`) 안에서만 성립한다(LA-4·3.4.4·2.2.2).
- 체결 디렉터리에는 두 종류의 파일이 섞인다: `{signal_id}.json`(체결 이벤트)과
  `{signal_id}.reanchor.json`(분할가 재고정). **계좌는 체결 이벤트만** 읽는다(R01).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from engine.backtest.execution import EntryBand, band_from_dict, opening_fill
from engine.ledger import grade as grade_mod
from engine.decide.sizing import MAX_SECTOR, recheck_planned_loss
from engine.ledger.record import (EXECUTION_EVENTS, execution_event, reanchor_event,
                                 split_signal_id, superseded_ids)

INITIAL_CASH = 1_000_000.0
SLIPPAGE_BPS = 10.0
EXECUTIONS_DIR = Path("data/ledger/executions")
PORTFOLIO_DIR = Path("data/ledger/portfolio")
HISTORY_FILE = "history.jsonl"
RECORDS_PATH = Path("data/ledger/records.jsonl")
#: 2·3차 체결·손절·분할익절을 세션 순서로 남기는 append-only 로그(P0-3 · A-3).
POSITION_EVENTS_PATH = Path("data/ledger/position_events.jsonl")
#: 주문 동결 마감 — 주문 마감보다 **앞선 내부 확정 시각**(P0-2). 기본 5분.
FREEZE_BUFFER = timedelta(minutes=5)
#: `settle_positions(forced_exits=...)` 가 받는 강제 청산 사유.
#: `TRIM` 은 **전량이 아니라 보유 1/3 익절**이다(§3.5 · P-5) — 나머지 셋은 잔여 분할까지 취소한다.
FORCED_EXIT_REASONS = ("S3", "S5", "EXIT", "TRIM")

EXECUTION_SUFFIX = ".json"
REANCHOR_SUFFIX = ".reanchor.json"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ────────────────────────────── 이벤트 로더 (R01) ──────────────────────────────
def load_executions(root: str | Path = EXECUTIONS_DIR) -> dict[str, dict]:
    """`{signal_id}.json` **체결 이벤트만** 읽는다(없으면 빈 dict).

    같은 디렉터리의 `{signal_id}.reanchor.json` 은 `signal_id` 가 같고 정렬상 뒤에 오기 때문에,
    예전 로더는 재고정 이벤트로 체결 이벤트를 **덮어써** 체결이 사라진 것처럼 보였다.
    방어선은 두 겹이다: ① 파일명 접미사 ② `quant_track.execution_event` 타입 검사.
    """
    root = Path(root)
    out: dict[str, dict] = {}
    if not root.exists():
        return out
    for path in sorted(root.glob(f"*{EXECUTION_SUFFIX}")):
        if path.name.endswith(REANCHOR_SUFFIX):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        track = payload.get("quant_track") or {}
        if track.get("execution_event") not in EXECUTION_EVENTS:
            continue
        out[str(payload.get("signal_id") or path.stem)] = payload
    return out


def load_reanchors(root: str | Path = EXECUTIONS_DIR) -> dict[str, dict]:
    """`plan_reanchor` 이벤트만 — **손익에 쓰지 않는다**(표시·계획 재고정 전용)."""
    root = Path(root)
    out: dict[str, dict] = {}
    if not root.exists():
        return out
    for path in sorted(root.glob(f"*{REANCHOR_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("execution_event") != "plan_reanchor":
            continue
        out[str(payload.get("signal_id") or path.stem)] = payload
    return out


def public_event(event: dict) -> dict:
    """커밋용 체결 이벤트 — 공개 금지 키(`shares`)를 남기지 않는다.

    실수량은 `data/ledger/executions/{signal_id}.full.jsonl`(gitignore)과 메모리 상태에만 있다.
    """
    track = dict(event.get("quant_track") or {})
    if track.get("shares") is None:
        track.pop("shares", None)
    out = {k: v for k, v in event.items() if k != "quant_track"}
    out["quant_track"] = track
    return out


def _band_of(record: dict) -> EntryBand | None:
    return band_from_dict(record.get("entry_band") or {})


def _session_of(record: dict) -> date | None:
    """레코드의 `target_session`(주문 대상 거래일). 없으면 `None` — 구 레코드 폴백은 `settle` 이 한다."""
    value = record.get("target_session")
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_dt(value) -> datetime | None:
    """ISO 문자열·datetime → tz-aware datetime(없거나 못 읽으면 `None`)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def freeze_deadline_at(order_cutoff, buffer: timedelta = FREEZE_BUFFER) -> datetime | None:
    """주문 **동결 마감** — 주문 마감보다 앞선 내부 확정 시각(기획서 2.2.2 · P0-2).

    이 시각까지 도착한 WATCH·차단은 미체결 신규 진입을 취소할 수 있고, 이 시각을 넘겨 도착한
    변경은 **이미 동결된 주문을 사후에 취소·대체하지 않는다**(다음 세션용이다).
    """
    cutoff = _as_dt(order_cutoff)
    return None if cutoff is None else cutoff - buffer


#: 주문 슬롯의 계정 축 — 지금은 단일 모의 계좌지만 슬롯 키는 `(account_id, ticker, 세션)` 이다.
DEFAULT_ACCOUNT = "default"


def slot_key(account_id, ticker, session) -> tuple:
    """슬롯 키 — `(account_id, ticker, target_session)` (v1.3.5 · 치명 01)."""
    return (str(account_id or DEFAULT_ACCOUNT), str(ticker or ""), session)


def get_slot(slots: dict, ticker: str, session, account_id: str = DEFAULT_ACCOUNT) -> dict:
    """슬롯 조회 — 계정 축이 없는 옛 호출도 같은 슬롯을 찾는다."""
    if not slots:
        return {}
    found = slots.get(slot_key(account_id, ticker, session))
    if found is not None:
        return found
    return slots.get((str(ticker or ""), session)) or {}


def _release_kind(record: dict) -> str:
    """발행 종류(`BUY`/`WATCH`/`BLOCKED`) — 레코드가 스스로 밝힌 값을 먼저 믿는다."""
    kind = str(record.get("release_kind") or record.get("type") or "").upper()
    if kind in ("BUY", "WATCH", "BLOCKED"):
        return kind
    return "BUY" if record.get("quant_intent") == "pending_buy" else "WATCH"


def order_slots(records: list[dict], *, freeze_buffer: timedelta = FREEZE_BUFFER,
                account_id: str = DEFAULT_ACCOUNT) -> dict[tuple, dict]:
    """주문 슬롯 — `(account_id, ticker, target_session)` 별 **동결 주문·동결 시각**(P0-1 · v1.3.5).

    슬롯은 "그 세션에 이 종목을 산다"는 상태이고, 그 세션의 주문은 **동결 마감(`freeze_deadline_at`)
    까지 도착한 마지막 회차**가 확정한다. 슬롯은 다음을 함께 남긴다.

    - `signal_id`·`frozen_at`·`freeze_deadline_at` — 동결된 주문과 그 시각. `frozen_at` 은 슬롯이
      있으면 **절대 `None` 이 아니다**(감사 §10①: 취소 시각을 사후 검증할 수 있어야 한다).
    - `held` — 동결 뒤 도착한 회차. 당일 슬롯을 덮어쓰지 않고 다음 세션용으로 보류된다.
      (예전에는 동결 뒤 BUY 가 슬롯을 갈아치우고 **두 회차가 모두 체결**됐다 — 감사 재현 01-(a).)
    - `frozen_source` — `before_deadline`(마감 전 확정) · `no_prior_order`(마감 전 주문이 없어
      마감 뒤 첫 회차가 그 세션 주문이 됨 — 취소·대체할 기존 주문이 없다).

    `pending_buy` 뿐 아니라 WATCH·차단 재발행도 슬롯을 만든다. 주문이 없던 회차도 `frozen_at` 을
    남겨야 "동결 전에 취소됐나"를 사후에 판정할 수 있다(감사 §10①).
    """
    groups: dict[tuple, list[dict]] = {}
    for record in records or []:
        session = _session_of(record)
        if session is None:
            try:
                session = _next_trading_day(date.fromisoformat(str(record["as_of"])[:10]))
            except (ValueError, KeyError):
                session = None
        groups.setdefault((str(record.get("ticker") or ""), session), []).append(record)
    out: dict[tuple, dict] = {}
    for (ticker, session), rows in sorted(groups.items(),
                                          key=lambda kv: (str(kv[0][1] or ""), kv[0][0])):
        rows = sorted(rows, key=lambda r: (split_signal_id(str(r.get("signal_id")))[1],
                                           str(r.get("signal_published_at") or "")))
        frozen: dict | None = None
        late: list[dict] = []
        for record in rows:
            published = _as_dt(record.get("signal_published_at"))
            deadline = freeze_deadline_at(record.get("order_cutoff_at"), freeze_buffer)
            if published is not None and deadline is not None and published > deadline:
                late.append({"record": record, "published": published, "deadline": deadline})
                continue
            frozen = {"record": record, "published": published, "deadline": deadline}
        source = "before_deadline"
        if frozen is None:
            if not late:
                continue
            frozen = late.pop(0)          # 마감 전 주문이 없다 — 취소·대체할 기존 주문도 없다
            source = "no_prior_order"
        held = [{
            "signal_id": str(item["record"].get("signal_id")),
            "revision": split_signal_id(str(item["record"].get("signal_id")))[1],
            "release_kind": _release_kind(item["record"]),
            "intent": item["record"].get("quant_intent"),
            "signal_published_at": None if item["published"] is None else _iso(item["published"]),
            "reason": "ARRIVED_AFTER_FREEZE",
        } for item in late]
        record = frozen["record"]
        deadline = (frozen["deadline"] if frozen["deadline"] is not None
                    else freeze_deadline_at(record.get("order_cutoff_at"), freeze_buffer))
        published = frozen["published"]
        frozen_at = published
        if frozen_at is None or (deadline is not None and frozen_at > deadline):
            frozen_at = deadline if deadline is not None else published
        out[slot_key(account_id, ticker, session)] = {
            "slot_id": f"{str(account_id or DEFAULT_ACCOUNT)}:{ticker}:"
                       f"{session.isoformat() if session else 'none'}",
            "account_id": str(account_id or DEFAULT_ACCOUNT),
            "ticker": ticker,
            "target_session": session.isoformat() if session else None,
            "signal_id": str(record.get("signal_id")),
            "revision": split_signal_id(str(record.get("signal_id")))[1],
            "release_kind": _release_kind(record),
            "intent": record.get("quant_intent"),
            "frozen_at": None if frozen_at is None else _iso(frozen_at),
            "freeze_deadline_at": None if deadline is None else _iso(deadline),
            "frozen_source": source,
            "held": held,
        }
    return out


def _superseding_map(records: list[dict]) -> dict[str, dict]:
    """`대체된 signal_id → 그 발행본을 대체한 회차`(마지막 대체만 남긴다)."""
    out: dict[str, dict] = {}
    for record in records or []:
        old = record.get("supersedes")
        if old:
            out[str(old)] = record
    return out


def _lock_keys(stop_locks) -> set[tuple[str, str]]:
    """손절 재진입 잠금 → `{(ticker, 'YYYY-MM-DD')}` (P-1).

    받아들이는 형태: `{"TICKER|2026-05-12", …}`, `[("TICKER", "2026-05-12"), …]`,
    `{("TICKER", "2026-05-12"): "S5"}`(사유 포함) — 실행기가 어느 쪽을 넘겨도 같은 집합이 된다.
    """
    out: set[tuple[str, str]] = set()
    if not stop_locks:
        return out
    items = stop_locks.items() if isinstance(stop_locks, dict) else [(item, None) for item in stop_locks]
    for key, _value in items:
        if isinstance(key, dict):
            ticker = str(key.get("ticker") or "")
            day = str(key.get("session") or key.get("date") or key.get("lock") or "")
            if not ticker and "|" in day:
                ticker, _, day = day.partition("|")
            if ticker and day:
                out.add((ticker, day[:10]))
            continue
        if isinstance(key, str):
            if "|" in key:
                ticker, _, day = key.partition("|")
            else:
                ticker, _, day = key.partition(":")
            if ticker and day:
                out.add((ticker.strip(), day.strip()[:10]))
            continue
        if isinstance(key, (tuple, list)) and len(key) >= 2:
            out.add((str(key[0]), str(key[1])[:10]))
    return out


def _hold_reason(debate_holds, signal_id: str, ticker: str) -> str | None:
    """토론 보류 목록에서 이 신호의 사유를 찾는다 — 신호 정체성 우선, 종목 폴백.

    받아들이는 형태: `{signal_id: "AUTO_DOWNGRADE"}`, `{ticker: 사유}`, `[signal_id, …]`.
    `None` 이면 보류가 아니다.
    """
    if not debate_holds:
        return None
    sid, tic = str(signal_id or ""), str(ticker or "")
    if isinstance(debate_holds, dict):
        for key in (sid, tic):
            if key and key in debate_holds:
                return str(debate_holds.get(key) or "AUTO_DOWNGRADE")
        return None
    items = [str(item) for item in debate_holds]
    if sid in items or tic in items:
        return "AUTO_DOWNGRADE"
    return None


def stop_reentry_locks(prior_events: list[dict] | None, *, as_of: date | None = None) -> list[dict]:
    """`position_events` 의 **S5 손절** → 재진입 잠금 목록 `[{"ticker", "session", "lock"}]`.

    P-1: 손절이 난 종목·세션은 그 세션의 신규 1차 진입까지 막는다. 잠금은 세션 로그에서 파생한다 —
    별도 상태 파일을 만들면 원장(append-only)과 어긋날 수 있다.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in prior_events or []:
        if row.get("event") != "exit_filled" or str(row.get("reason") or "") != "S5":
            continue
        ticker = str(row.get("ticker") or "")
        day = str(row.get("date") or "")[:10]
        if not ticker or not day or (ticker, day) in seen:
            continue
        if as_of is not None and day > str(as_of)[:10]:
            continue
        seen.add((ticker, day))
        out.append({"ticker": ticker, "session": day, "signal_id": row.get("signal_id"),
                    "lock": f"{ticker}|{day}"})
    return out


def settle(records: list[dict], executions: dict[str, dict], panels: dict,
           as_of: date, slippage_bps: float = SLIPPAGE_BPS, *,
           max_sector: float | None = MAX_SECTOR, equity_base: float = INITIAL_CASH,
           sector_exposure: dict[str, float] | None = None,
           freeze_buffer: timedelta = FREEZE_BUFFER,
           now: str | datetime | None = None,
           stop_locks=None, account_id: str = DEFAULT_ACCOUNT,
           position_events_path: str | Path | None = None,
           prior_events: list[dict] | None = None,
           blocked: dict[str, str] | None = None,
           debate_holds: dict[str, str] | None = None) -> dict:
    """발행 의도 → 체결 이벤트. 시가가 확정되지 않은 신호는 `pending` 으로 남긴다.

    체결 세션은 **레코드의 `target_session`** 이다 — `next_trading_day(signal_day)` 로 밀면
    금요일 신호가 월요일 시가에 체결된다(R08). 그 세션 봉이 없으면 대체 세션을 쓰지 않고
    `SESSION_BAR_MISSING` 으로 보류한다.

    [v1.3.5 · 치명 01 · §10①②③⑤ · P-1]

    1. **슬롯당 체결은 1건**이다. 체결은 `order_slots()` 가 확정한 **동결 주문**(동결 마감까지 도착한
       마지막 회차)만 대상으로 하고, 회차 루프가 아니라 **슬롯 루프**로 돈다. 동결 뒤 도착한 회차는
       `held`/`pending`(사유 `ARRIVED_AFTER_FREEZE`)으로만 남아 같은 세션을 두 번 사지 않는다.
    2. **취소는 실제 주문이 있을 때만** 남긴다 — WATCH·차단으로 대체된 회차가 `pending_buy` 였고
       아직 체결되지 않았을 때만 `cancellations`(`cancel_pending`)에 넣는다. 처음부터 `no_action`
       이던 WATCH 재발행은 `intent_superseded`(회차 무효화)로만 기록한다(§10⑤).
    3. 취소 이벤트의 `at` 은 **실제 대체 발행 시각**(`now` 또는 대체 레코드의 `signal_published_at`)
       이다. `freeze_deadline_at` 은 참고 메타 필드로 따로 남긴다(§10①).
    4. `-001→-002→-003` 처럼 여러 회차가 이어져도 계보를 빠짐없이 따라간다(§10②). `revision` 은
       **종목·세션 단위 회차**다(§10③).
    5. **P-1**: `stop_locks` 에 `(ticker, 세션)` 이 있으면 그 세션의 신규 1차 진입을 체결하지 않고
       `no_action`(`STOP_REENTRY_LOCK`)으로 남긴다. 차단 건수는 `stop_reentry_blocked_count` 로
       따로 보고한다(2·3차 취소 수와 분리). `stop_locks` 를 주지 않고 `position_events_path`(세션 로그)
       나 `prior_events` 를 주면 S5 손절 행에서 잠금을 **파생**한다 — 실행기는 경로만 넘기면 된다.
    6. **차단 우선**: `blocked={ticker: 사유}` 를 주면 그 종목의 1차 진입을 체결하지 않는다. 배치가
       차단 레코드를 **정산 뒤에** append 하는 실행기에서도 같은 배치의 시가 체결을 막는다.
    7. **토론 보류**: `debate_holds={signal_id: 사유}`(종목 키도 받는다)를 주면 그 신호의 1차 진입을
       체결하지 않고 `hold_cash`(체결 아님)로 남긴다 — 자동 하향이 켜진 배치에서 화면(`hold_cash`)과
       원장(`buy_filled`)이 갈리지 않게 한다(감사 06-(e)). 사유는 `settled[].reason="AUTO_DOWNGRADE"`.

    `sector_exposure`(현재 섹터 평가액 달러)를 주면 기존 보유까지 합산하고, 주지 않으면 이번 배치에서
    체결한 금액만 센다. 체결 순서는 `(세션, 종목, 회차)` 로 고정해 **입력 순서에 의존하지 않는다**.
    """
    new_events: list[dict] = []
    reanchors: list[dict] = []
    pending: list[dict] = []
    settled: list[dict] = []
    cancellations: list[dict] = []
    held: list[dict] = []
    reentry_blocked: list[dict] = []
    superseded = superseded_ids(records)
    by_supersedes = _superseding_map(records)
    by_sid = {str(r.get("signal_id")): r for r in records or []}
    slots = order_slots(records, freeze_buffer=freeze_buffer, account_id=account_id)
    slot_by_group = {(s["ticker"], str(s["target_session"] or "")): s for s in slots.values()}
    arrived_at = _as_dt(now)

    def _slot_for(record: dict) -> dict:
        session = _session_of(record)
        if session is None:
            try:
                session = _next_trading_day(date.fromisoformat(str(record.get("as_of"))[:10]))
            except (ValueError, KeyError):
                session = None
        return slot_by_group.get((str(record.get("ticker") or ""),
                                  session.isoformat() if session else ""), {})

    # ── 1) 계보 — 대체·취소·회차 무효화 ─────────────────────────────────────────
    ordered = sorted(records or [], key=lambda r: (str(r.get("as_of") or ""),
                                                   str(r.get("ticker") or ""),
                                                   split_signal_id(str(r.get("signal_id")))[1]))
    for record in ordered:
        sid = str(record.get("signal_id"))
        if sid in executions:
            continue
        newer = by_supersedes.get(sid)
        if sid not in superseded or not newer:
            continue
        slot = _slot_for(record)
        deadline = freeze_deadline_at(record.get("order_cutoff_at"), freeze_buffer)
        arrived = _as_dt(newer.get("signal_published_at")) or arrived_at
        after_freeze = bool(deadline is not None and arrived is not None and arrived > deadline)
        if after_freeze:
            settled.append({"signal_id": sid, "event": "superseded_after_freeze",
                            "reason": "FROZEN_ORDER_STANDS",
                            "superseded_by": newer.get("signal_id"),
                            "frozen_at": slot.get("frozen_at"),
                            "freeze_deadline_at": slot.get("freeze_deadline_at")})
            continue
        if record.get("quant_intent") != "pending_buy":
            # 처음부터 주문이 없던 회차(WATCH·차단 재발행) — 주문 취소가 아니라 **회차 무효화**다.
            settled.append({"signal_id": sid, "event": "superseded",
                            "reason": "INTENT_SUPERSEDED",
                            "superseded_by": newer.get("signal_id"),
                            "frozen_at": slot.get("frozen_at"),
                            "freeze_deadline_at": slot.get("freeze_deadline_at")})
            continue
        if newer.get("quant_intent") == "pending_buy":
            # 미체결 BUY 가 새 BUY 회차로 교체됨 — 슬롯 교체(취소 + 새 주문)다.
            settled.append({"signal_id": sid, "event": "superseded", "reason": "REVISION_REPLACED",
                            "superseded_by": newer.get("signal_id"),
                            "frozen_at": slot.get("frozen_at"),
                            "freeze_deadline_at": slot.get("freeze_deadline_at")})
            cancellations.append({
                "signal_id": sid, "ticker": record.get("ticker"),
                "target_session": (newer.get("target_session") or record.get("target_session")),
                "cancelled_by": "REVISION_REPLACED", "cancel_kind": _release_kind(newer),
                "superseded_by": newer.get("signal_id"),
                "at": None if arrived is None else _iso(arrived),
                "frozen_at": slot.get("frozen_at"),
                "freeze_deadline_at": slot.get("freeze_deadline_at")})
            continue
        settled.append({"signal_id": sid, "event": "superseded", "reason": "SUPERSEDED_BEFORE_FREEZE",
                        "superseded_by": newer.get("signal_id"),
                        "frozen_at": slot.get("frozen_at"),
                        "freeze_deadline_at": slot.get("freeze_deadline_at")})
        cancellations.append({
            "signal_id": sid, "ticker": record.get("ticker"),
            "target_session": (newer.get("target_session") or record.get("target_session")),
            "cancelled_by": "WATCH_OR_BLOCKED", "cancel_kind": _release_kind(newer),
            "superseded_by": newer.get("signal_id"),
            "at": None if arrived is None else _iso(arrived),
            "frozen_at": slot.get("frozen_at"),
            "freeze_deadline_at": slot.get("freeze_deadline_at")})

    # ── 2) 슬롯별 1차 체결 — 슬롯당 1건 ─────────────────────────────────────────
    if stop_locks is None and (prior_events is not None or position_events_path is not None):
        # P-1 잠금은 세션 로그에서 파생한다 — 실행기가 경로나 이벤트 목록을 주면 여기서 만든다.
        stop_locks = stop_reentry_locks(
            prior_events if prior_events is not None else read_position_events(position_events_path),
            as_of=as_of)
    locks = _lock_keys(stop_locks)
    sector_dollars: dict[str, float] = {str(k): float(v) for k, v in (sector_exposure or {}).items()}
    slot_rows = sorted(slots.values(),
                       key=lambda s: (str(s["target_session"] or ""), s["ticker"], s["signal_id"]))
    for slot in slot_rows:
        sid = str(slot["signal_id"])
        record = by_sid.get(sid) or {}
        if not record or sid in executions:
            continue
        if record.get("quant_intent") != "pending_buy":
            continue
        ticker = record.get("ticker")
        panel = panels.get(ticker)
        signal_day = date.fromisoformat(str(record["as_of"])[:10])
        session = _session_of(record)
        legacy = session is None
        if legacy:
            session = _next_trading_day(signal_day)
        if session is None:
            pending.append({"signal_id": sid, "ticker": ticker, "expected_fill_day": None,
                            "reason": "NO_SESSION"})
            continue
        if session > as_of:
            pending.append({"signal_id": sid, "ticker": ticker,
                            "expected_fill_day": session.isoformat()})
            continue
        if blocked and str(ticker) in {str(k) for k in blocked}:
            # 같은 배치에서 확정된 차단 — 발행 레코드 append 가 정산 뒤에 오더라도 시가에 사지 않는다.
            reason = str(blocked.get(ticker) or blocked.get(str(ticker)) or "BLOCKED")
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(session), execution_event="no_action",
                                    note=f"차단({reason}) — 신규 진입 없음(치명 01)")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "BLOCKED",
                            "target_session": session.isoformat(), "blocked_reason": reason})
            continue
        hold_reason = _hold_reason(debate_holds, sid, ticker)
        if hold_reason is not None:
            # 감사 06-(e): 토론 자동 하향으로 보류된 신호 — 원장도 체결하지 않고 `hold_cash` 로 남긴다.
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(session), execution_event="hold_cash",
                                    note=f"토론 자동 하향({hold_reason}) — 이번 세션 신규 1차 진입 보류")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "hold_cash", "reason": "AUTO_DOWNGRADE",
                            "debate_reason": hold_reason, "target_session": session.isoformat()})
            continue
        if locks and (str(ticker), session.isoformat()) in locks:
            # P-1: 같은 종목·같은 세션은 손절 뒤 신규 1차 진입을 하지 않는다.
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(session), execution_event="no_action",
                                    note="S5 손절 당일 — 같은 세션 신규 1차 진입 차단(3.5 · P-1)")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "STOP_REENTRY_LOCK",
                            "target_session": session.isoformat()})
            reentry_blocked.append({"signal_id": sid, "ticker": ticker,
                                    "target_session": session.isoformat(),
                                    "reason": "S5_STOP_SAME_SESSION"})
            continue
        stamp = pd.Timestamp(session)
        if panel is None or stamp not in panel.frame.index:
            pending.append({"signal_id": sid, "ticker": ticker,
                            "expected_fill_day": session.isoformat(),
                            "reason": "SESSION_BAR_MISSING"})
            continue
        bar = panel.frame.loc[stamp]
        band_dict = record.get("entry_band") or {}
        band = band_from_dict(band_dict)
        migrated = bool(band_dict) and "stop_min_sadj" not in band_dict
        if band is None:
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(session), execution_event="no_action",
                                    note="entry_band 없음 — 주문 범위를 확인할 수 없어 체결하지 않는다")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "BAND_MISSING"})
            continue
        factor_open = float(bar["split_factor"]) if "split_factor" in bar else 1.0
        result = opening_fill(band, float(bar["raw_open"]), slippage_bps,
                              split_factor_open=factor_open)
        if result["execution_event"] != "buy_filled":
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(session), execution_event="no_action",
                                    note=f"시가 {result['open_price']:.2f} 가 주문 범위를 벗어남")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "outside_order_range",
                            "target_session": session.isoformat(), "legacy_session": legacy,
                            "band_basis_migrated": migrated})
            continue
        fill = float(result["fill_price"])
        position = record.get("position") or {}
        stop_distance = float(position.get("stop_distance") or 0.0)
        stop_price = round(fill * (1.0 - stop_distance), 4)
        planned_loss = round(float(position.get("plan_weight_target") or 0.0) * stop_distance, 6)
        weight = float(position.get("tranche_weight") or 0.0)
        sector = str(record.get("sector") or "")
        notional = max(0.0, weight) * float(equity_base)
        if max_sector is not None and sector and equity_base > 0:
            room = float(max_sector) * float(equity_base) - sector_dollars.get(sector, 0.0)
            if notional > room + 1e-9:
                event = execution_event(
                    signal_id=sid, snapshot_id=record.get("snapshot_id", ""), at=_open_at(session),
                    execution_event="no_action",
                    note=(f"섹터 {sector} 상한 {float(max_sector):.0%} 초과 예정"
                          f"({(sector_dollars.get(sector, 0.0) + notional) / float(equity_base):.2%})"
                          " — 체결하지 않는다(4.12)"))
                new_events.append(public_event(event))
                settled.append({"signal_id": sid, "event": "no_action", "reason": "SECTOR_CAP",
                                "target_session": session.isoformat(), "sector": sector})
                continue
            sector_dollars[sector] = sector_dollars.get(sector, 0.0) + notional
        event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                at=_open_at(session), execution_event="buy_filled", tranche=1,
                                fill_price=fill, slippage_bps=slippage_bps,
                                weight=weight,
                                stop_price=stop_price, trailing_high=result["open_price"],
                                planned_loss_pct=planned_loss, shares=None,
                                note="대상 세션 시가 체결(발행 레코드는 수정하지 않는다)")
        new_events.append(public_event(event))
        plan = _reanchor_plan(record, fill, stop_price)
        reanchors.append(reanchor_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                        at=_open_at(session), first_fill=fill,
                                        stop_price=stop_price, plan=plan))
        settled.append({"signal_id": sid, "event": "buy_filled", "fill_price": fill,
                        "target_session": session.isoformat(), "legacy_session": legacy,
                        "band_basis_migrated": migrated})

    # ── 3) 동결 뒤 도착분 — 당일 체결 없이 다음 세션용으로 보류 ──────────────────
    for slot in slot_rows:
        for item in slot.get("held") or []:
            held.append({**item, "ticker": slot["ticker"], "target_session": slot["target_session"]})
            next_day = None
            if slot.get("target_session"):
                try:
                    next_day = _next_trading_day(date.fromisoformat(slot["target_session"]))
                except ValueError:
                    next_day = None
            pending.append({"signal_id": item["signal_id"], "ticker": slot["ticker"],
                            "expected_fill_day": next_day.isoformat() if next_day else None,
                            "reason": "ARRIVED_AFTER_FREEZE"})
    return {"new_events": new_events, "reanchors": reanchors, "pending": pending,
            "settled": settled, "cancellations": cancellations, "held": held,
            "slots": slot_rows,
            "stop_reentry_blocked_count": len(reentry_blocked),
            "stop_reentry_blocked": reentry_blocked,
            "stop_locks": sorted(f"{ticker}|{day}" for ticker, day in locks)}


def _next_trading_day(day: date) -> date | None:
    from engine.utils.dates import next_trading_day
    return next_trading_day(day)


def _open_at(day: date) -> datetime:
    from engine.utils.dates import market_open_at
    return market_open_at(day)


def _reanchor_plan(record: dict, fill: float, stop_price: float) -> list[dict]:
    rows = []
    for row in record.get("entry_plan") or []:
        t = int(row["tranche"])
        price = round(fill * (1.0 - 0.07 * (t - 1)), 2)
        rows.append({"tranche": t, "price_ref": price, "provisional": False,
                     "active": price > stop_price, "note": None if price > stop_price else "손절선 밖 → 무효"})
    return rows


def replay(records: list[dict], executions: dict[str, dict],
           reanchors: dict[str, dict] | None = None) -> dict:
    """체결 이벤트를 재생해 모의 계좌(수량·평단)를 만든다 — 메모리에서만 쓴다.

    `reanchors` 는 **손익에 영향을 주지 않는다.** 재고정된 분할가를 표시용으로만 붙인다.
    """
    reanchors = reanchors or {}
    cash = INITIAL_CASH
    positions: dict[str, dict] = {}
    displayed: dict[str, dict] = {}
    for record in records:
        sid = str(record.get("signal_id"))
        if sid in reanchors:
            displayed[sid] = {k: reanchors[sid].get(k) for k in ("first_fill", "stop_price", "plan", "at")}
        event = executions.get(sid)
        if not event:
            continue
        track = event.get("quant_track") or {}
        if track.get("execution_event") != "buy_filled" or track.get("fill_price") is None:
            continue
        ticker = record.get("ticker")
        price = float(track["fill_price"])
        weight = float(track.get("weight") or (record.get("position") or {}).get("tranche_weight") or 0.0)
        notional = max(0.0, weight) * INITIAL_CASH
        qty = notional / price if price > 0 else 0.0
        if qty <= 0:
            continue
        pos = positions.setdefault(ticker, {
            "ticker": ticker, "sector": record.get("sector") or "", "qty": 0.0, "avg_price": 0.0,
            "stop_price": float(track.get("stop_price") or 0.0),
            "stop_distance": float((record.get("position") or {}).get("stop_distance") or 0.0),
            "opened_at": str(record.get("as_of")), "tranches_filled": [], "signal_ids": [],
        })
        prev_cost = pos["qty"] * pos["avg_price"]
        pos["qty"] += qty
        pos["avg_price"] = (prev_cost + notional) / pos["qty"] if pos["qty"] else price
        pos["stop_price"] = max(pos["stop_price"], float(track.get("stop_price") or 0.0))
        pos["tranches_filled"].append(int(track.get("tranche") or 1))
        pos["signal_ids"].append(sid)
        cash -= notional
    return {"cash": cash, "positions": positions, "plan_reanchored": displayed}


def mark(state: dict, panels: dict, as_of: date, executions: dict[str, dict] | None = None,
         records: list[dict] | None = None) -> dict:
    """당일 종가로 평가 → **공개용 비중 요약**(수량·평단 없음).

    청산(`exit_filled`)된 종목은 보유에서 뺀다.
    """
    executions = executions or {}
    cash = float(state["cash"])
    rows: list[dict] = []
    holdings_value = 0.0
    for ticker, pos in sorted(state["positions"].items()):
        exited = False
        for sid in pos.get("signal_ids", []):
            event = executions.get(sid) or {}
            if (event.get("quant_track") or {}).get("execution_event") == "exit_filled":
                exited = True
                cash += float(pos["qty"]) * float((event.get("quant_track") or {}).get("fill_price") or 0.0)
        if exited:
            continue
        panel = panels.get(ticker)
        price = pos["avg_price"]
        if panel is not None and not panel.frame.empty:
            price = float(panel.frame.iloc[-1]["raw_close"])
        value = float(pos["qty"]) * price
        holdings_value += value
        rows.append({
            "ticker": ticker,
            "sector": pos.get("sector") or "",
            "opened_at": pos.get("opened_at"),
            "tranches_filled": sorted(set(pos.get("tranches_filled") or [])),
            "stop_distance": round(float(pos.get("stop_distance") or 0.0), 4),
            "value": value,
        })
    equity = cash + holdings_value
    total = equity if equity > 0 else INITIAL_CASH
    positions = [{
        "ticker": r["ticker"], "sector": r["sector"], "opened_at": r["opened_at"],
        "tranches_filled": r["tranches_filled"], "stop_distance": r["stop_distance"],
        "weight": round(r["value"] / total, 6),
    } for r in rows]
    sector_weights: dict[str, float] = {}
    for p in positions:
        sector_weights[p["sector"]] = round(sector_weights.get(p["sector"], 0.0) + p["weight"], 6)
    return {
        "as_of": as_of.isoformat(),
        "cash_pct": round(cash / total, 6),
        "invested_pct": round(holdings_value / total, 6),
        "equity_index": round(equity / INITIAL_CASH, 6),
        "positions": positions,
        "n_positions": len(positions),
        "sector_weights": sector_weights,
        "note_ko": "모의 계좌(초기 100만 달러) — 수량·평단·금액은 공개 산출물에 넣지 않는다.",
    }


# ────────────────────────────── 채점 (R02) ──────────────────────────────
def bars_for(panel, as_of: date, limit: int | None = None) -> list[dict]:
    """채점용 봉 — 세 기준을 함께 낸다.

    - `raw_*`: 밴드·타이밍(체결·손절과 같은 기준)
    - `sadj_*`: 분할 조정
    - `adj_close`·`tr_open`: 수익률·벤치마크(`total_return_adj`)

    `limit` 기본값은 `None` 이다 — 6개월(126거래일) 만기 채점이 200봉 창에 아슬아슬하게
    걸리면 만기 봉 판정이 흔들린다.
    """
    if panel is None or panel.frame.empty:
        return []
    frame = panel.frame.loc[panel.frame.index <= pd.Timestamp(as_of)]
    if limit:
        frame = frame.tail(limit)
    out = []
    for idx, row in frame.iterrows():
        sadj_close = float(row["sadj_close"])
        sadj_open = float(row["sadj_open"])
        adj_close = float(row["adj_close"])
        ratio = (adj_close / sadj_close) if sadj_close else 1.0
        out.append({
            "date": idx.date() if hasattr(idx, "date") else idx,
            "raw_open": float(row["raw_open"]),
            "raw_high": float(row["raw_high"]),
            "raw_low": float(row["raw_low"]),
            "raw_close": float(row["raw_close"]),
            "sadj_open": sadj_open,
            "sadj_close": sadj_close,
            "adj_close": adj_close,
            "split_factor": float(row["split_factor"]) if "split_factor" in row else 1.0,
            "tr_open": sadj_open * ratio,
            "tr_close": adj_close,
        })
    return out


def _tr_frame(panel) -> pd.DataFrame:
    frame = panel.frame
    if frame.empty:
        return pd.DataFrame(columns=["tr_open", "tr_close"])
    sadj_close = frame["sadj_close"].astype(float)
    sadj_open = frame["sadj_open"].astype(float)
    adj = frame["adj_close"].astype(float)
    ratio = (adj / sadj_close.where(sadj_close != 0)).fillna(1.0)
    return pd.DataFrame({"tr_open": sadj_open * ratio, "tr_close": adj}, index=frame.index)


def benchmark_returns_for(benchmark_panel, entry_session: date | None,
                          maturities: dict[str, date], virtual: bool = False) -> dict[str, float]:
    """SPY **총수익**을 종목과 **같은 진입 세션·같은 만기 봉**으로 계산한다(4.7.2).

    종목 수익률과 같은 규칙(`tr_close(maturity)/tr_open(entry) − 1`, 가상 체결이면
    `tr_close(maturity)/tr_close(as_of) − 1`)을 쓴다. 만기 봉이 없으면 그 기간은 넣지 않는다.
    """
    if benchmark_panel is None:
        return {}
    tr = _tr_frame(benchmark_panel)
    if tr.empty:
        return {}
    if entry_session is None:
        return {}
    start = None
    if virtual:
        # 가상 체결은 신호일 종가에서 시작한다(종목 수익률과 같은 규칙)
        exact = tr.loc[tr.index == pd.Timestamp(entry_session)]
        start = float(exact["tr_close"].iloc[-1]) if not exact.empty else None
    else:
        exact = tr.loc[tr.index == pd.Timestamp(entry_session)]
        start = float(exact["tr_open"].iloc[-1]) if not exact.empty else None
    if not start or start <= 0:
        return {}
    out: dict[str, float] = {}
    for name, maturity in maturities.items():
        stamp = pd.Timestamp(maturity)
        if stamp not in tr.index:
            continue
        out[name] = float(tr.loc[stamp, "tr_close"]) / start - 1.0
    return out


def grade_all(records: list[dict], executions: dict[str, dict], panels: dict, as_of: date,
              holdout_start: str | None = None, benchmark_panel=None,
              horizons: dict[str, int] | None = None, mode: str = "paper") -> list[dict]:
    """모든 발행 레코드를 기간별로 채점한다(만기 봉이 없으면 `complete: false`).

    벤치마크는 **신호마다** 그 신호의 진입 세션·만기 봉으로 계산한다 — 배치 실행일 기준
    5/21/63/126일 SPY 하나를 모든 신호에 돌려주면 초과수익이 신호와 무관해진다.

    [v1.3.4 · P0-4] 기본 `mode="paper"` 는 **Paper 운용 채점**이다. 호출자가 백테스트 홀드아웃
    경계(`holdout_start`)를 넘겨도 만기 판정에는 쓰지 않는다(truncated 는 항상 False). 보정 코퍼스
    자격을 보려면 `mode="calibration"` 을 명시해야 하고, 그때만 홀드아웃 경계 잘림이 표시된다.
    """
    from engine.utils.dates import add_trading_days

    mode = str(mode or "paper").lower()
    if mode not in ("paper", "calibration"):
        raise ValueError(f"알 수 없는 채점 모드: {mode!r} (paper | calibration)")
    cutoff = None if mode == "paper" else holdout_start
    hz = horizons or grade_mod.HORIZONS_TD
    graded: list[dict] = []
    superseded = superseded_ids(records)
    for record in records:
        ticker = record.get("ticker")
        sid = str(record.get("signal_id"))
        execution = executions.get(sid)
        if sid in superseded and not execution:
            continue            # 대체된 회차(체결 없음) — 최신 발행본만 채점한다
        track = (execution or {}).get("quant_track") or {}
        fill = track.get("fill_price")
        signal_day = date.fromisoformat(str(record["as_of"])[:10])
        entry_session = signal_day
        if fill is not None and execution and execution.get("at"):
            try:
                entry_session = date.fromisoformat(str(execution["at"])[:10])
            except ValueError:
                entry_session = signal_day
        bars = bars_for(panels.get(ticker), as_of)
        maturities = {name: add_trading_days(signal_day, h) for name, h in hz.items()}
        bench = benchmark_returns_for(benchmark_panel, entry_session, maturities,
                                      virtual=fill is None)
        result = grade_mod.grade_record(record, execution, bars=bars, benchmark_returns=bench,
                                        holdout_start=cutoff, horizons=hz, mode=mode)
        result["entry_score"] = record.get("entry_score")
        result["sector"] = record.get("sector")
        result["params_version"] = record.get("params_version")
        graded.append(result)
    return graded


def benchmark_period_return(panel, start: date | None, as_of: date) -> float | None:
    """평가 기간 SPY 총수익(파일럿 ③) — `tr_open(첫 진입일) → tr_close(as_of)`."""
    if panel is None:
        return None
    tr = _tr_frame(panel)
    if tr.empty:
        return None
    end_rows = tr.loc[tr.index <= pd.Timestamp(as_of)]
    if end_rows.empty:
        return None
    end = float(end_rows["tr_close"].iloc[-1])
    if start is None:
        start_rows = end_rows
    else:
        exact = tr.loc[tr.index == pd.Timestamp(start)]
        start_rows = exact if not exact.empty else tr.loc[tr.index <= pd.Timestamp(start)]
    if start_rows.empty:
        return None
    begin = float(start_rows["tr_open"].iloc[-1])
    if not begin or begin <= 0:
        return None
    return end / begin - 1.0


# ────────────────────────────── 요약·기록 ──────────────────────────────
def summary(graded: list[dict], portfolio: dict, pilot: dict | None, folds: dict | None = None,
            params_history: list[dict] | None = None, benchmark_connected: bool = False) -> dict:
    """성적표 요약(`web/data/ledger/summary.json` 의 원본) — 신호 정확도와 계좌 성과를 분리."""
    horizons: dict[str, dict] = {}
    for name in ("1w", "1m", "3m", "6m"):
        stats = grade_mod.summarize_grades(graded, horizon=name)
        weighted = grade_mod.weighted_agreement(graded, horizon=name)
        horizons[name] = {
            "horizon": name,
            "n": stats["n"],
            "agreement": weighted["weighted_agreement"],
            "agreement_mean": stats["agreement_mean"],
            "ic": stats["ic"],
            "win_rate": stats["win_rate"],
            "mean_excess": stats["mean_excess"],
            "profit_factor": stats["profit_factor"],
        }
    returns = []
    for g in graded:
        h = (g.get("horizons") or {}).get("3m") or {}
        if h.get("excess_return") is not None:
            returns.append({"signal_id": g.get("signal_id"), "ticker": g.get("ticker"),
                            "excess": h["excess_return"], "return": h.get("return"),
                            "action": g.get("action")})
    valid = [r for r in returns]
    wins = [r for r in valid if r["excess"] > 0]
    return {
        "schema_version": 1,
        "as_of": portfolio.get("as_of"),
        "n_signals": len(graded),
        "n_graded_3m": len(valid),
        "accuracy": {
            "label_ko": "신호 정확도",
            "agreement_3m": horizons["3m"]["agreement"],
            "ic_3m": horizons["3m"]["ic"],
            "win_rate": None if not valid else round(len(wins) / len(valid), 4),
            "mean_excess": None if not valid else round(sum(r["excess"] for r in valid) / len(valid), 6),
            "benchmark_connected": benchmark_connected,
            "note_ko": None if benchmark_connected else "SPY 벤치마크 미연결 — 초과수익을 표시하지 않는다",
        },
        "strategy": {
            "label_ko": "전략(모의 계좌)",
            "equity_index": portfolio.get("equity_index"),
            "cash_pct": portfolio.get("cash_pct"),
            "invested_pct": portfolio.get("invested_pct"),
            "avg_exposure": portfolio.get("avg_exposure"),
            "n_positions": portfolio.get("n_positions"),
            "note_ko": "신호가 맞아도 현금이 놀면 계좌 성과는 다르다(4.12).",
        },
        "horizons": horizons,
        "folds": folds or {},
        "pilot": pilot or {"verdict": "보류", "reason_ko": "파일럿 지표 미산출"},
        "params_history": params_history or [],
        "signal_history": sorted(returns, key=lambda r: str(r["signal_id"]), reverse=True)[:200],
    }


def write_public_state(state: dict, root: str | Path = PORTFOLIO_DIR) -> Path:
    """공개 계좌 스냅샷 — `state.json` 과 `latest.json` 에 같은 내용을 쓴다.

    `latest.json` 은 화면·문서가 가리키는 이름이고, `state.json` 은 기존 계약 이름이다.
    둘 다 **비중·상태만** 담는다(`shares`·`avg_cost`·`cash_balance` 금지, 2.2.3).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2)
    path = root / "state.json"
    path.write_text(text, encoding="utf-8")
    (root / "latest.json").write_text(text, encoding="utf-8")
    return path


HISTORY_FIELDS = ("as_of", "cash_pct", "invested_pct", "equity_index", "n_positions")


def read_portfolio_history(root: str | Path = PORTFOLIO_DIR) -> list[dict]:
    """`data/ledger/portfolio/history.jsonl` — 하루 1행 **비중만**(공개 허용 목록 안전)."""
    path = Path(root) / HISTORY_FILE
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_portfolio_history(state: dict, root: str | Path = PORTFOLIO_DIR) -> Path:
    """일별 비중을 append-only 로 남긴다 — 같은 `as_of` 는 다시 쓰지 않는다(멱등)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / HISTORY_FILE
    row = {k: state.get(k) for k in HISTORY_FIELDS}
    if row.get("as_of") is None:
        return path
    if any(r.get("as_of") == row["as_of"] for r in read_portfolio_history(root)):
        return path
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def avg_exposure_from_history(rows: list[dict], start: date | None = None,
                              as_of: date | None = None) -> float | None:
    """평균 노출도 — 첫 진입일부터 `as_of` 까지 `invested_pct` 의 평균.

    단일 시점 `invested_pct` 를 평균 자리에 대입하지 않는다(R11). 기록이 없으면 `None`.
    """
    values = []
    for row in rows or []:
        day = row.get("as_of")
        if not day:
            continue
        try:
            d = date.fromisoformat(str(day)[:10])
        except ValueError:
            continue
        if start is not None and d < start:
            continue
        if as_of is not None and d > as_of:
            continue
        if row.get("invested_pct") is None:
            continue
        values.append(float(row["invested_pct"]))
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def append_full_event(event: dict, shares: float | None, root: str | Path = "data/ledger/executions") -> None:
    """실수량 포함 원본 이벤트 — gitignore 경로(`*.full.jsonl`)에만 남긴다."""
    path = Path(root) / f"{event['signal_id']}.full.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload["quant_track"] = {**(event.get("quant_track") or {}), "shares": shares}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def write_execution(event: dict, root: str | Path = EXECUTIONS_DIR) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{event['signal_id']}.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old != event:
            raise ValueError(f"{event['signal_id']} 체결 이벤트가 다른 내용으로 이미 기록돼 있다 — append-only")
        return path
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_reanchor(event: dict, root: str | Path = "data/ledger/executions") -> Path:
    path = Path(root) / f"{event['signal_id']}{REANCHOR_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def benchmark_trailing_returns(panels: dict) -> dict:
    """SPY 가 데이터셋에 있으면 **배치 실행일 기준** 최근 5·21·63·126거래일 총수익을 돌려준다.

    참고 표시 전용이다 — 신호별 채점에는 쓰지 않는다(`grade_all` 은 `benchmark_returns_for`
    로 신호마다 같은 구간을 계산한다). 이름을 바꾼 이유가 그것이다(옛 `beta_free_benchmark`).
    """
    spy = panels.get("SPY")
    if spy is None or spy.frame.empty:
        return {}
    closes = spy.frame["adj_close"] if "adj_close" in spy.frame else spy.frame["sadj_close"]
    out: dict[str, float] = {}
    for name, days in (("1w", 5), ("1m", 21), ("3m", 63), ("6m", 126)):
        if len(closes) > days:
            out[name] = float(closes.iloc[-1] / closes.iloc[-1 - days] - 1.0)
    return out


def stats_from_curve(index_points: list[float]) -> dict:
    """정규화 자산곡선 → 간단 통계(있으면 CAGR·MDD)."""
    if len(index_points) < 2:
        return {"cagr": None, "mdd": None}
    arr = np.asarray(index_points, dtype=float)
    peak = np.maximum.accumulate(arr)
    mdd = float((arr / peak - 1.0).min())
    return {"cagr": None, "mdd": round(mdd, 6)}


def missing_benchmark_note() -> str:
    return "SPY 총수익 벤치마크가 데이터셋에 없어 초과수익·IC 를 계산하지 않았다(정직 표기)."


# ══════════════════════ 실시간 2·3차 분할 · 손절/분할익절 정산 (Phase 4) ══════════════════════
#
# 기획서 3.5 · 4.12 의 "한 거래일의 처리 순서"를 실시간 Paper Ledger 에 그대로 적용한다.
#   1) 기업 이벤트(분할) → 2) 갭 손절 → 3) 장중 **손절 먼저** → 4) 2·3차 지정가 →
#   5) 분할익절(IV_high·RSI) → 6) 종가 트레일링 갱신.
# 전량 청산(S5·S3·EXIT)이 나면 **같은 봉의 잔여 2·3차 주문을 즉시 취소**하고
# `cancelled_by` 를 남긴다 — 그러지 않으면 손절로 다 팔고 같은 날 3차가 체결돼 포지션이 다시 열린다.

PROFIT_TAKE_FRACTION = 1.0 / 3.0
TRANCHE_DROPS_LIVE = (0.0, -0.07, -0.14)
HEALTH_PATH = Path("data/health.json")


def _tranche_plan(record: dict, reanchor: dict | None, first_fill: float,
                  initial_stop: float, params: dict | None) -> list[dict]:
    """2·3차 주문 상태 — 재고정(`plan_reanchor`)이 있으면 그 가격을 쓴다(실제 1차 체결가 기준)."""
    scaling = (params or {}).get("portfolio") or {}
    weights = list(scaling.get("tranches") or [0.40, 0.30, 0.30])
    drops = list(scaling.get("tranche_drops") or TRANCHE_DROPS_LIVE)
    plan = list((reanchor or {}).get("plan") or [])
    by_index = {int(row.get("tranche")): row for row in plan if row.get("tranche") is not None}
    rows = []
    for index, weight in zip((1, 2, 3), weights):
        row = by_index.get(index) or {}
        if index == 1:
            rows.append({"tranche": 1, "weight": float(weight), "price_ref": round(first_fill, 4),
                         "status": "filled", "filled_at": None, "cancelled_by": None, "note": None})
            continue
        drop = float(drops[index - 1]) if len(drops) >= index else TRANCHE_DROPS_LIVE[index - 1]
        price = float(row.get("price_ref") if row.get("price_ref") is not None else first_fill * (1.0 + drop))
        status = "pending"
        trigger = "limit"
        note = None
        if index == 3 and price <= initial_stop:
            # [v1.3.4 · A-7] 지정가는 초기 손절가 아래라 **지정가 트랙은 무효**지만, 3차의 다른 트랙인
            #   200일선 조건은 살아 있다(기획서 3.5의 두 번째 트리거). 전부 무효로 만들면 기본 파라미터에서
            #   (−14% 지정가 vs −14% 손절) 3차가 영원히 걸리지 않는다 → **DMA 전용으로 무장**한다.
            trigger = "dma200"
            note = "3차 지정가가 초기 손절가 이하 → 200일선 조건(다음 거래일 시가)으로만 진입"
        elif row.get("active") is False:
            status = "invalid"
            note = row.get("note") or "손절선 밖 → 무효"
        elif row.get("note"):
            note = row.get("note")
        rows.append({"tranche": index, "weight": float(weight), "price_ref": round(price, 4),
                     "status": status, "trigger": trigger, "filled_at": None,
                     "cancelled_by": None, "note": note})
    return rows


def _indicator_lines(panel) -> dict:
    """봉별 200일선·RSI(14) — 3차 200일선 조건과 분할익식 RSI 조건에 쓴다(sadj 기준)."""
    if panel is None:
        return {}
    try:
        from engine.metrics.technical import dma, rsi14
        from engine.models import PriceKind
        series = panel.series(PriceKind.split_adj_ohlc)
        line200 = dma(series, 200)
        rsi = rsi14(series, 14)
    except Exception:  # noqa: BLE001 — 지표를 못 만들면 조건을 쓰지 않는다(체결 규칙은 유지)
        return {}
    out = {}
    for idx in panel.frame.index:
        d200 = line200.get(idx)
        r = rsi.get(idx)
        out[idx.date()] = (None if d200 is None or pd.isna(d200) else float(d200),
                           None if r is None or pd.isna(r) else float(r))
    return out


def settle_positions(records: list[dict], executions: dict[str, dict],
                     reanchors: dict[str, dict] | None, panels: dict, as_of: date,
                     params: dict | None = None, *, forced_exits: dict[str, str] | None = None,
                     initial_cash: float = INITIAL_CASH,
                     slippage_bps: float = SLIPPAGE_BPS,
                     max_sector: float | None = MAX_SECTOR,
                     position_events_path: str | Path | None = None,
                     prior_events: list[dict] | None = None,
                     stop_reentry_blocked: list[dict] | None = None) -> dict:
    """1차 체결 이후의 2·3차 분할·손절·분할익절·TRIM 을 **세션 순서**로 정산한다.

    발행 레코드·체결 이벤트는 고치지 않는다 — 여기서 만드는 것은 **파생 상태**(화면·건강 지표)다.

    [v1.3.5 · 중요 03 + P-1 + P-5 + §14#2]

    - **확정 매도를 되돌리지 않는다.** `position_events` 의 기존 `exit_filled`(S3·S5·S4·EXIT)·
      `profit_take`(iv_high·rsi·TRIM)·`cancel_pending` 을 먼저 재생해 상태에 반영한다. 예전에는
      청산 뒤 다음 날 재실행하면 그 청산이 사라져 포지션이 되살아났다(감사 재현 03-(1)).
    - **시세 봉이 없으면 버리지 않는다.** `as_of` 봉이 아직 없으면 청산·TRIM 주문을
      `exit_pending` 으로 남기고, 다음 이용 가능 봉 시가에 체결한다(감사 재현 03-(2)).
    - **날짜 바깥 루프** — 날짜마다 `시가 예약(강제 청산·TRIM·예약 매도·예약 매수) → 장중 손절 →
      지정가 2·3차 → 종가(트레일링·예약·평가)` 순서로 **모든 보유**를 같은 규칙으로 돌린다.
    - **섹터 한도 분모 통일** — 1차(`settle`)와 2·3차가 모두 **그 시점 열린 포지션 평가액 / 그 시점
      순자산**을 쓴다. 예전 2·3차는 `cash_spent`(미래 지출 포함)를 써서 종목 입력 순서로 결과가
      달라졌다(감사 재현 03-(3), §14#2).
    - **P-5 TRIM** — `forced_exits` 에 `"TRIM"` 이 오면 **보유 수량의 1/3 익절 매도**를 실행하고
      사유를 `TRIM` 으로 남긴다(S4 200일선 잔여 전량 정리와 구분). 잔여 2·3차 주문은 취소하지 않는다.
    - **P-1** — S5 손절은 세션 로그에 남아 `stop_reentry_locks()` 로 재진입 잠금이 되고, 이번 정산에서
      실제로 막은 재진입은 `stop_reentry_blocked`(실행기 전달값)로 센다. 2·3차 취소 수는
      `tranche_cancel_count` 로 **분리**한다.

    2·3차 수량은 **계획 비중 × 분할 비율**, 3차 200일선 조건은 **다음 거래일 시가**, 분할익절은
    **고정 기준 수량의 1/3** 이다(3.5 · 4.12). 체결·손절·익절 이벤트는 `position_events.jsonl` 에
    세션 순서로 남기며 금지 키(`shares`·`avg_cost`·`cash_balance`)는 싣지 않는다(2.2.3).
    """
    reanchors = reanchors or {}
    forced_exits = {str(k): str(v) for k, v in (forced_exits or {}).items()}
    risk = (params or {}).get("risk") or {}
    risk_per_trade = float(risk.get("risk_per_trade") or 0.0075)
    max_position = float(risk.get("max_position") or 0.05)
    events: list[dict] = []
    cancelled: list[dict] = []
    replayed: list[dict] = []
    states: dict[str, dict] = {}
    bars_cache: dict[str, dict[date, dict]] = {}
    lines_cache: dict[str, dict] = {}

    def bars_of(ticker: str) -> dict[date, dict]:
        if ticker not in bars_cache:
            bars_cache[ticker] = {bar["date"]: bar for bar in bars_for(panels.get(ticker), as_of)}
        return bars_cache[ticker]

    def lines_of(ticker: str) -> dict:
        if ticker not in lines_cache:
            lines_cache[ticker] = _indicator_lines(panels.get(ticker))
        return lines_cache[ticker]

    if prior_events is None:
        prior_events = (read_position_events(position_events_path)
                        if position_events_path is not None else [])
    prior: dict[str, list[dict]] = {}
    for row in prior_events or []:
        sid = str(row.get("signal_id") or "")
        day = str(row.get("date") or "")[:10]
        if not sid or not day:
            continue
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        prior.setdefault(sid, []).append({**row, "date": day})
    for rows in prior.values():
        rows.sort(key=lambda r: str(r.get("date")))

    def cash_now() -> float:
        return (float(initial_cash) - sum(float(st["cash_spent"]) for st in states.values())
                + sum(float(st["cash_returned"]) for st in states.values()))

    def mark_price(sid_value: str, day: date) -> float:
        st = states[sid_value]
        bar = bars_of(st["ticker"]).get(day)
        if bar is not None:
            return float(bar["raw_open"])
        return float(st.get("last_price") or st.get("avg_cost") or 0.0)

    def equity_at(day: date) -> float:
        total = cash_now()
        for key, st in states.items():
            if st["exit"] is not None or float(st["qty"]) <= 0:
                continue
            total += float(st["qty"]) * mark_price(key, day)
        return total

    def sector_value_at(day: date, sector: str, exclude: str) -> float:
        if not sector:
            return 0.0
        return float(sum(float(st["qty"]) * mark_price(key, day)
                         for key, st in states.items()
                         if key != exclude and (st.get("sector") or "") == sector
                         and st["exit"] is None and float(st["qty"]) > 0))

    # ── 상태 만들기 — 실제 1차 체결이 있는 발행만 ───────────────────────────────
    for record in records or []:
        sid = str(record.get("signal_id"))
        if sid in states:
            continue
        event = executions.get(sid) or {}
        track = event.get("quant_track") or {}
        if track.get("execution_event") != "buy_filled" or track.get("fill_price") is None:
            continue
        # [v1.3.5 · 치명 01] `superseded` 로 표시돼도 **실제 체결이 있으면 보유를 유지**한다.
        #   예전에는 동결 뒤 WATCH 로 바뀐 종목의 체결분이 다음 날 사라져 계좌가 전액 현금이 됐다.
        ticker = record.get("ticker")
        panel = panels.get(ticker)
        if panel is None or panel.frame.empty:
            continue
        bars = bars_of(ticker)
        if not bars:
            continue
        fill = float(track["fill_price"])
        fill_day = None
        if event.get("at"):
            try:
                fill_day = date.fromisoformat(str(event["at"])[:10])
            except ValueError:
                fill_day = None
        if fill_day is None:
            fill_day = _session_of(record)
        if fill_day is None:
            continue
        start = next((d for d in sorted(bars) if d >= fill_day), None)
        if start is None:
            continue
        first_fill = float((reanchors.get(sid) or {}).get("first_fill") or fill)
        stop_distance = float((record.get("position") or {}).get("stop_distance") or 0.0)
        initial_stop = float((reanchors.get(sid) or {}).get("stop_price")
                             or first_fill * (1.0 - stop_distance))
        tranches = _tranche_plan(record, reanchors.get(sid), first_fill, initial_stop, params)
        weight = float(track.get("weight") or (record.get("position") or {}).get("tranche_weight") or 0.0)
        plan_weight = float((record.get("position") or {}).get("plan_weight_target") or weight)
        qty = (weight * initial_cash / fill) if fill > 0 else 0.0
        if qty <= 0:
            continue
        valuation = record.get("valuation") or {}
        iv_high = valuation.get("iv_high") if not record.get("valuation_suspended") else None
        st = {
            "signal_id": sid, "ticker": ticker, "sector": record.get("sector") or "",
            "opened_at": fill_day.isoformat(), "first_fill": round(first_fill, 4),
            "qty": qty, "avg_cost": fill, "equity_base": initial_cash,
            "plan_weight": plan_weight,
            "stop_distance": round(stop_distance, 6), "initial_stop": round(initial_stop, 4),
            "stop_price": round(initial_stop, 4), "trailing_high": fill,
            "iv_high": None if iv_high is None else float(iv_high),
            "atr_pct": (record.get("indicators") or {}).get("atr_pct"),
            "tranches": tranches, "trim_done": [], "trim_stage": "none",
            # 분할익절 기준 수량 — **1차 체결 수량으로 고정**한다(매번 잔량의 1/3 이 아니다).
            "trim_basis_qty": qty, "trim_count": 0,
            "cash_spent": weight * initial_cash, "cash_returned": 0.0,
            "exit": None, "last_price": fill, "pending_sell_fraction": 0.0,
            "last_event_ko": "1차 체결",
        }
        tranches[0]["filled_at"] = fill_day.isoformat()
        st["start_day"] = start
        st["factor_prev"] = float(bars[start].get("split_factor") or 1.0)
        st["cash_left"] = plan_weight * initial_cash
        st["pending_exit"] = None
        st["prior_by_day"] = {}
        for row in prior.get(sid) or []:
            st["prior_by_day"].setdefault(str(row.get("date")), []).append(row)
            if row.get("event") == "exit_pending":
                st["pending_exit"] = {"reason": str(row.get("reason") or "EXIT"),
                                      "since": str(row.get("date"))[:10]}
        states[sid] = st

    ordered_sids = sorted(states, key=lambda s: (states[s]["opened_at"], s))

    def _apply_split(st: dict, bar: dict, day: date) -> None:
        factor = float(bar.get("split_factor") or 1.0)
        if factor > 0 and abs(factor - st["factor_prev"]) > 1e-12:
            ratio = st["factor_prev"] / factor
            st["qty"] *= ratio
            st["avg_cost"] /= ratio
            st["stop_price"] /= ratio
            st["initial_stop"] /= ratio
            st["trailing_high"] /= ratio
            st["first_fill"] /= ratio
            st["trim_basis_qty"] *= ratio
            if st["iv_high"] is not None:
                st["iv_high"] = round(float(st["iv_high"]) / ratio, 6)
            for row in st["tranches"]:
                if row.get("price_ref") is not None:
                    row["price_ref"] = round(float(row["price_ref"]) / ratio, 4)
            events.append({"date": day.isoformat(), "ticker": st["ticker"], "signal_id": st["signal_id"],
                           "event": "split_adjustment", "ratio": round(ratio, 6),
                           "iv_high": None if st["iv_high"] is None else round(st["iv_high"], 4)})
        st["factor_prev"] = factor

    def _exit_all(st: dict, day: date, price: float, reason: str, cancel: bool = True,
                  replay: bool = False) -> None:
        price = float(price)
        st["exit"] = {"reason": reason, "at": day.isoformat(), "price": round(price, 4)}
        st["cash_returned"] += float(st["qty"]) * price * (1.0 - slippage_bps / 1e4)
        row = {"date": day.isoformat(), "ticker": st["ticker"], "signal_id": st["signal_id"],
               "event": "exit_filled", "reason": reason, "price": round(price, 4),
               "fill_basis": "daily_ohlc_assumption"}
        (replayed if replay else events).append(row)
        if cancel:
            _cancel_pending(st, st["signal_id"], st["ticker"], day, reason, cancelled,
                            replay=replay, replayed=replayed)
        st["last_event_ko"] = f"{reason} — 잔여 분할 즉시 취소" if cancel else f"{reason} 청산"

    def _sell(st: dict, day: date, price: float, qty: float, reason: str,
              fraction: float | None = None, replay: bool = False) -> None:
        qty = min(float(qty), float(st["qty"]))
        if qty <= 1e-12:
            return
        price = float(price)
        st["qty"] -= qty
        st["cash_returned"] += qty * price * (1.0 - slippage_bps / 1e4)
        row = {"date": day.isoformat(), "ticker": st["ticker"], "signal_id": st["signal_id"],
               "event": "profit_take", "reason": reason,
               "fraction": round(float(fraction), 4) if fraction is not None else None,
               "qty_pct": round(qty * price / float(st["equity_base"]), 6),
               "price": round(price, 4), "fill_basis": "daily_ohlc_assumption"}
        (replayed if replay else events).append(row)
        if reason in ("iv_high", "rsi"):
            st["trim_stage"] = reason
        elif reason == "TRIM":
            st["trim_stage"] = "score"
        if st["qty"] <= 1e-9:
            st["exit"] = {"reason": "S2", "at": day.isoformat(), "price": round(price, 4)}

    # ── 마감 봉이 없는 강제 청산·TRIM → carry-over ──────────────────────────────
    for sid in ordered_sids:
        st = states[sid]
        reason = forced_exits.get(st["ticker"])
        if reason and st["exit"] is None and as_of not in bars_of(st["ticker"]):
            st["pending_exit"] = {"reason": reason, "since": as_of.isoformat()}
            events.append({"date": as_of.isoformat(), "ticker": st["ticker"], "signal_id": sid,
                           "event": "exit_pending", "reason": reason})

    day_list = sorted({d for cache in bars_cache.values() for d in cache})
    if ordered_sids:
        day_list = [d for d in day_list if d >= min(states[s]["start_day"] for s in ordered_sids)]

    for day in day_list:
        iso_day = day.isoformat()
        for sid in ordered_sids:
            st = states[sid]
            if st["exit"] is not None or day < st["start_day"]:
                continue
            bar = bars_of(st["ticker"]).get(day)
            if bar is None:
                continue
            # 0) 기업 이벤트 → 기록된 확정 이벤트 재생(청산·TRIM·취소는 되돌리지 않는다)
            _apply_split(st, bar, day)
            for row in st["prior_by_day"].pop(iso_day, []):
                kind = row.get("event")
                if kind == "exit_filled" and st["exit"] is None:
                    _exit_all(st, day, float(row.get("price") or bar["raw_open"]),
                              str(row.get("reason") or "EXIT"), replay=True)
                elif kind == "profit_take" and st["qty"] > 0:
                    why = str(row.get("reason") or "TRIM")
                    marker = {"TRIM": "score"}.get(why, why)      # TRIM 의 1회 표식은 "score"
                    if marker not in st["trim_done"]:
                        st["trim_done"].append(marker)
                        qty = float(st["qty"]) / 3.0 if why == "TRIM" else _trim_qty(st)
                        _sell(st, day, float(row.get("price") or bar["raw_open"]), qty, why,
                              fraction=row.get("fraction"), replay=True)
                        if why == "rsi":
                            # 전일 종가가 예약한 RSI 매도를 **이미 재생**했으면 이번 봉에서 또 팔지 않는다.
                            st["pending_sell_fraction"] = 0.0
                elif kind == "cancel_pending":
                    _cancel_pending(st, sid, st["ticker"], day,
                                    str(row.get("cancelled_by") or "CANCELLED"), cancelled,
                                    only=int(row["tranche"]) if row.get("tranche") is not None else None,
                                    replay=True, replayed=replayed)
            if st["exit"] is not None:
                continue
            # 1) 시가 예약 — 강제 청산(S3·S5·EXIT) · TRIM · 예약 매도 · 예약 매수
            forced = forced_exits.get(st["ticker"]) if day == as_of else None
            if forced is None and st.get("pending_exit") and iso_day >= st["pending_exit"]["since"]:
                forced = st["pending_exit"]["reason"]
            if forced == "TRIM":
                st["pending_exit"] = None
                if "score" not in st["trim_done"]:
                    st["trim_done"].append("score")
                    st["trim_count"] += 1
                    # P-5: 보유 수량의 1/3 익절 — 잔여 2·3차 주문은 취소하지 않는다.
                    _sell(st, day, float(bar["raw_open"]), float(st["qty"]) / 3.0, "TRIM",
                          fraction=PROFIT_TAKE_FRACTION)
                    st["last_event_ko"] = "TRIM — 보유 1/3 익절"
                    if st["exit"] is not None:
                        continue
            elif forced:
                st["pending_exit"] = None
                _exit_all(st, day, float(bar["raw_open"]), forced)
                continue
            if st["pending_sell_fraction"] > 0:
                fraction = st["pending_sell_fraction"]
                st["pending_sell_fraction"] = 0.0
                if fraction >= 0.999:
                    _exit_all(st, day, float(bar["raw_open"]), "S4")
                    continue
                sold = _trim_qty(st)
                st["trim_count"] += 1
                _sell(st, day, float(bar["raw_open"]), sold, "rsi", fraction=fraction)
                if st["exit"] is not None:
                    continue
            if st.get("pending_buy"):
                index = int(st.pop("pending_buy"))
                row = next((r for r in st["tranches"] if int(r["tranche"]) == index), None)
                if row is not None and row["status"] == "pending":
                    _fill_tranche(st, row, sid, st["ticker"], day, float(bar["raw_open"]),
                                  "rsi_next_open", st["cash_left"], equity_at(day),
                                  sector_value_at(day, st["sector"], sid), cash_now(),
                                  risk_per_trade, max_position, events, max_sector)
            if st.get("pending_dma_buy"):
                index = int(st.pop("pending_dma_buy"))
                row = next((r for r in st["tranches"] if int(r["tranche"]) == index), None)
                if row is not None and row["status"] == "pending":
                    if float(bar["raw_open"]) <= st["initial_stop"]:
                        row["status"] = "invalid"
                        row["note"] = "200일선 조건 시가가 초기 손절가 이하 → 무효(3.5)"
                    else:
                        _fill_tranche(st, row, sid, st["ticker"], day, float(bar["raw_open"]),
                                      "dma_next_open", st["cash_left"], equity_at(day),
                                      sector_value_at(day, st["sector"], sid), cash_now(),
                                      risk_per_trade, max_position, events, max_sector)
            # 2) 장중 손절 — 손절 먼저(같은 봉의 추가 진입은 취소된다)
            if float(bar["raw_open"]) <= st["stop_price"]:
                _exit_all(st, day, float(bar["raw_open"]), "S5")
                st["last_event_ko"] = "S5 손절 — 잔여 분할 즉시 취소"
                continue
            if float(bar["raw_low"]) <= st["stop_price"]:
                _exit_all(st, day, float(st["stop_price"]), "S5")
                st["last_event_ko"] = "S5 손절 — 잔여 분할 즉시 취소"
                continue
            # 3) 지정가 2·3차 — 손절이 나지 않은 봉에서만
            for row in st["tranches"]:
                if row["status"] != "pending":
                    continue
                index = int(row["tranche"])
                price_ref = float(row["price_ref"])
                triggered = index == 2 and float(bar["raw_low"]) <= price_ref
                if index == 3:
                    triggered = (row.get("trigger") != "dma200" and float(bar["raw_low"]) <= price_ref)
                if not triggered:
                    continue
                if price_ref <= st["initial_stop"]:
                    row["status"] = "invalid"
                    row["note"] = "지정가가 초기 손절가 이하 → 무효"
                    continue
                _fill_tranche(st, row, sid, st["ticker"], day, price_ref, "limit",
                              st["cash_left"], equity_at(day), sector_value_at(day, st["sector"], sid),
                              cash_now(), risk_per_trade, max_position, events, max_sector)
            # 4) 분할익절 — IV_high 도달 시 기준 수량의 1/3
            if (st["iv_high"] is not None and float(bar["raw_high"]) >= st["iv_high"]
                    and "iv_high" not in st["trim_done"] and st["qty"] > 0):
                sold = _trim_qty(st)
                st["trim_done"].append("iv_high")
                st["trim_count"] += 1
                _sell(st, day, float(st["iv_high"]), sold, "iv_high", fraction=PROFIT_TAKE_FRACTION)
                st["last_event_ko"] = "IV_high 도달 — 기준 수량 1/3 분할익절"
                if st["exit"] is not None:
                    continue
            # 5) 종가 — 트레일링 갱신, 다음 세션 예약
            st["trailing_high"] = max(st["trailing_high"], float(bar["raw_high"]))
            st["stop_price"] = max(st["stop_price"],
                                   st["trailing_high"] * (1.0 - st["stop_distance"]))
            st["last_price"] = float(bar["raw_close"])
            _line200, rsi = lines_of(st["ticker"]).get(day, (None, None))
            line200 = _line200
            if (rsi is not None and rsi <= 25.0
                    and any(int(r["tranche"]) == 2 and r["status"] == "pending" for r in st["tranches"])):
                st["pending_buy"] = 2           # 전일 RSI ≤ 25 → 다음 거래일 시가 2차 체결
            if (line200 is not None and float(bar["sadj_close"]) <= line200 * 0.97
                    and any(int(r["tranche"]) == 3 and r["status"] == "pending" for r in st["tranches"])):
                st["pending_dma_buy"] = 3       # 200일선 −3% → 다음 거래일 시가 3차 체결(A-2)
            if rsi is not None and rsi >= 75.0 and "rsi" not in st["trim_done"]:
                st["trim_done"].append("rsi")
                st["pending_sell_fraction"] = PROFIT_TAKE_FRACTION
            if line200 is not None and float(bar["sadj_close"]) < line200 and st["trim_stage"] != "none":
                st["pending_sell_fraction"] = 1.0

    for sid in ordered_sids:
        st = states[sid]
        if st["exit"] is None:
            st["stop_price"] = max(st["stop_price"], st["initial_stop"])

    by_ticker: dict[str, dict] = {}
    for sid, st in sorted(states.items(), key=lambda kv: (kv[1]["opened_at"], kv[0])):
        ticker = st["ticker"]
        if ticker not in by_ticker:
            by_ticker[ticker] = {**st, "signal_ids": [sid], "qty": st["qty"],
                                 "cash_spent": st["cash_spent"], "cash_returned": st["cash_returned"]}
            continue
        agg = by_ticker[ticker]
        total = agg["qty"] + st["qty"]
        agg["avg_cost"] = ((agg["avg_cost"] * agg["qty"] + st["avg_cost"] * st["qty"]) / total) if total else 0.0
        agg["qty"] = total
        agg["cash_spent"] += st["cash_spent"]
        agg["cash_returned"] += st["cash_returned"]
        agg["stop_price"] = max(agg["stop_price"], st["stop_price"])
        agg["trailing_high"] = max(agg["trailing_high"], st["trailing_high"])
        agg["signal_ids"].append(sid)
        if agg["exit"] is None:
            agg["exit"] = st["exit"]
        agg["tranches"] = st["tranches"]
        agg["trim_stage"] = st["trim_stage"]
        agg["trim_done"] = sorted(set(agg["trim_done"]) | set(st["trim_done"]))
        agg["last_price"] = st["last_price"]

    cash = initial_cash - sum(st["cash_spent"] for st in states.values()) + \
        sum(st["cash_returned"] for st in states.values())
    blocked_entries = list(stop_reentry_blocked or [])
    health = {
        "positions_open": sum(1 for st in states.values() if st["exit"] is None),
        "tranches_filled": sum(1 for st in states.values() for row in st["tranches"]
                               if row["status"] == "filled"),
        "tranches_pending": sum(1 for st in states.values() for row in st["tranches"]
                                if row["status"] == "pending"),
        "stop_count": sum(1 for st in states.values() if (st["exit"] or {}).get("reason") == "S5"),
        "trim_count": sum(1 for item in [*events, *replayed] if item.get("event") == "profit_take"),
        # P-1: 재진입 차단 수는 **2·3차 취소 수와 분리**한다(예전에는 `len(cancelled)` 였다).
        "stop_reentry_blocked_count": len(blocked_entries),
        "tranche_cancel_count": len(cancelled),
        "pending_exit_count": sum(1 for st in states.values()
                                  if st.get("pending_exit") and st["exit"] is None),
        "blocked_reentries": blocked_entries[:50],
        "cancelled_tranches": cancelled[:50],
    }
    rows = position_event_rows(events, cancelled, as_of)
    if position_events_path is not None:
        append_position_events(rows, position_events_path)
    return {
        "as_of": as_of.isoformat(),
        "positions": by_ticker,
        "by_signal": states,
        "events": events,
        "cancelled": cancelled,
        "event_log": rows,
        "health": health,
        "cash": round(cash, 4),
        "initial_cash": initial_cash,
        "holdings": {
            t: {"ticker": t, "shares": st["qty"], "avg_cost": st["avg_cost"],
                "stop_price": st["stop_price"], "trailing_high": st["trailing_high"],
                "initial_stop": st["initial_stop"], "tranches_filled":
                sorted({int(row["tranche"]) for row in st["tranches"] if row["status"] == "filled"})}
            for t, st in by_ticker.items() if st["exit"] is None and st["qty"] > 1e-9
        },
    }


#: `position_events.jsonl` 에 남기는 필드 — 금지 키(`shares`·`avg_cost`·`cash_balance`)는 없다.
POSITION_EVENT_FIELDS = ("date", "ticker", "signal_id", "event", "tranche", "reason",
                         "price", "qty_pct", "ratio", "fraction", "cancelled_by", "fill_basis")


def position_event_rows(events: list[dict], cancelled: list[dict] | None = None,
                        as_of: date | None = None) -> list[dict]:
    """정산 이벤트 → 공개 가능한 세션 로그 행(비중·가격만, 수량 없음).

    `event_id` 는 `(session, signal_id, event, tranche, reason)` 로 고정된다 — 같은 세션을 다시 돌려도
    같은 id 이므로 append 가 멱등하다(P0-3 A-3 재생성·부분 실패 복구 근거).
    """
    rows: list[dict] = []
    for event in events or []:
        row = {k: event.get(k) for k in POSITION_EVENT_FIELDS}
        row["event"] = event.get("event")
        row["recorded_for"] = as_of.isoformat() if as_of else None
        row["event_id"] = "|".join(str(x) for x in (
            event.get("date"), event.get("signal_id"), event.get("event"),
            event.get("tranche"), event.get("reason")))
        rows.append({k: v for k, v in row.items() if v is not None or k in ("event", "event_id")})
    for cancel in cancelled or []:
        row = {"date": cancel.get("at"), "ticker": cancel.get("ticker"),
               "signal_id": cancel.get("signal_id"), "event": "cancel_pending",
               "tranche": cancel.get("tranche"), "cancelled_by": cancel.get("cancelled_by"),
               "recorded_for": as_of.isoformat() if as_of else None}
        row["event_id"] = "|".join(str(x) for x in (
            cancel.get("at"), cancel.get("signal_id"), "cancel_pending", cancel.get("tranche")))
        rows.append(row)
    return rows


def read_position_events(path: str | Path = POSITION_EVENTS_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_position_events(rows: list[dict], path: str | Path = POSITION_EVENTS_PATH) -> Path:
    """세션 이벤트를 append-only 로 남긴다 — 같은 `event_id` 는 다시 쓰지 않는다(멱등).

    같은 `event_id` 에 **다른 내용**이 오면 `ValueError` 로 막는다(원장 위조 변경 금지).

    [v1.3.5] `recorded_for`(그 행을 만든 배치의 `as_of`)만 다른 재발행은 **같은 사건의 재계산**
    이므로 조용히 건너뛴다. 예전에는 다음 날 재실행이 과거 사건을 다시 만들면(= `recorded_for`
    만 달라짐) `ValueError` 로 배치 이벤트 기록이 통째로 실패했다.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {str(r.get("event_id")): r for r in read_position_events(p)}
    added = 0
    with open(p, "a", encoding="utf-8") as f:
        for row in rows or []:
            key = str(row.get("event_id"))
            if key in existing:
                if existing[key] != row:
                    old = {k: v for k, v in existing[key].items() if k != "recorded_for"}
                    new = {k: v for k, v in row.items() if k != "recorded_for"}
                    if old != new:
                        raise ValueError(
                            f"position event {key} 가 다른 내용으로 이미 기록돼 있다 — append-only")
                continue
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            existing[key] = row
            added += 1
    return p


def _trim_qty(st: dict) -> float:
    """분할익절 수량 — **고정 기준 수량의 1/3**(3.5 · P0-3).

    매번 `qty × 1/3` 로 팔면 두 번째 뒤에 잔량이 4/9 로 남아 "세 번에 걸쳐 0"이 되지 않는다.
    기준 수량은 1차 체결 수량으로 고정하고(분할 시 함께 조정), 남은 수량보다 크면 남은 만큼만 판다.
    """
    basis = float(st.get("trim_basis_qty") or 0.0)
    qty = float(st.get("qty") or 0.0)
    if basis <= 0 or qty <= 0:
        return 0.0
    return min(qty, basis / 3.0)


def _fill_tranche(st: dict, row: dict, sid: str, ticker: str, day: date, price: float,
                  reason: str, cash_left: float, equity: float, sector_value: float,
                  cash_available: float, risk_per_trade: float, max_position: float,
                  events: list[dict], max_sector: float | None = None) -> float:
    """분할 체결 직전 **계획 손실·종목 상한·섹터 상한** 재검증(3.8 · 4.12) → 통과하면 체결, 초과하면 축소/거절.

    [v1.3.5 · §14#2] 섹터 상한의 분모를 **그 시점 순자산·그 시점 섹터 평가액**으로 받는다. 예전에는
    `cash_spent`(전 기간 지출 합, 미래 지출 포함)를 써서 종목 입력 순서에 따라 결과가 달라졌다.

    - 수량 = **계획 비중 × 분할 비율** × 그 시점 순자산 / 가격. 포지션 예산
      (`plan_weight × 순자산 − 이미 쓴 금액`)과 **실제 가용 현금**을 넘지 않는다(A-1).

    Returns: 체결 금액(달러). 체결하지 않았으면 `0.0`.
    """
    index = int(row["tranche"])
    price = float(price)
    if price <= 0:
        return 0.0
    plan_weight = float(st.get("plan_weight") or 0.0)
    equity = float(equity if equity and equity > 0 else st.get("equity_base") or 0.0)
    if equity <= 0:
        return 0.0
    want = float(row["weight"]) * plan_weight * equity / price
    budget_left = max(0.0, plan_weight * equity - float(st.get("cash_spent") or 0.0))
    cash = min(float(cash_left), budget_left, max(0.0, float(cash_available)))
    check = recheck_planned_loss(st["qty"], st["avg_cost"], st["stop_price"], want, price,
                                 equity, cash=cash,
                                 max_position=max_position, risk_per_trade=risk_per_trade,
                                 max_sector=max_sector, sector_value=sector_value)
    if check["action"] == "reject":
        row["status"] = "rejected"
        row["note"] = ("섹터 상한 초과 — 수량 축소 불가" if check.get("capped_by") == "SECTOR_CAP"
                       else "체결 전 계획 손실 재검증 초과 — 수량 축소 불가")
        events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                       "event": "tranche_rejected", "tranche": index,
                       "reason": check.get("reason"), "capped_by": check.get("capped_by")})
        return 0.0
    add = float(check["allowed_shares"])
    notional = add * price
    if notional <= 0 or notional > cash + 1e-9:
        return 0.0
    st["avg_cost"] = (st["avg_cost"] * st["qty"] + add * price) / (st["qty"] + add)
    st["qty"] += add
    st["cash_spent"] += notional
    st["cash_left"] = max(0.0, float(cash_left) - notional)
    row["status"] = "filled"
    row["filled_at"] = day.isoformat()
    st["last_event_ko"] = f"{index}차 분할 체결"
    events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                   "event": "buy_filled", "tranche": index, "price": round(price, 4),
                   "weight": round(row["weight"], 6), "recheck": check["action"],
                   "qty_pct": round(notional / equity, 6), "reason": reason,
                   "fill_basis": "daily_ohlc_assumption"})
    return notional


def _stop_out(st: dict, sid: str, ticker: str, day: date, price: float, reason: str,
              cancelled: list[dict], events: list[dict], slippage_bps: float) -> None:
    """전량 손절 + 같은 봉 잔여 2·3차 주문 취소(3.5 [고정])."""
    st["exit"] = {"reason": reason, "at": day.isoformat(), "price": round(float(price), 4)}
    st["cash_returned"] += st["qty"] * float(price) * (1.0 - slippage_bps / 1e4)
    events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                   "event": "exit_filled", "reason": reason, "price": round(float(price), 4),
                   "fill_basis": "daily_ohlc_assumption"})
    _cancel_pending(st, sid, ticker, day, reason, cancelled)
    st["last_event_ko"] = f"{reason} 손절 — 잔여 분할 즉시 취소"


def _cancel_pending(st: dict, sid: str, ticker: str, day: date, reason: str,
                    cancelled: list[dict], *, only: int | None = None,
                    replay: bool = False, replayed: list[dict] | None = None) -> None:
    """잔여 2·3차 주문 취소 — `only` 를 주면 그 회차만, `replay=True` 면 재생 로그로 보낸다.

    재생(`replay`)은 **이미 기록된 취소**를 상태에만 다시 적용하는 경로다. 같은 `event_id` 에
    `recorded_for` 만 다른 행이 다시 append 되면 append-only 검사에 걸리므로, 재생 행은
    `replayed` 로 분리해 로그에 다시 쓰지 않는다.
    """
    rows = replayed if replay else cancelled
    for row in st["tranches"]:
        if row["status"] != "pending":
            continue
        if only is not None and int(row["tranche"]) != int(only):
            continue
        row["status"] = "cancelled"
        row["cancelled_by"] = reason
        rows.append({"ticker": ticker, "signal_id": sid, "tranche": int(row["tranche"]),
                     "cancelled_by": reason, "at": day.isoformat()})


def mark_positions(settlement: dict, panels: dict, as_of: date) -> dict:
    """정산 상태 → **공개용 비중 요약**(수량·평단·금액 없음, 2.2.3)."""
    initial = float(settlement.get("initial_cash") or INITIAL_CASH)
    cash = float(settlement.get("cash") or initial)
    rows = []
    holdings_value = 0.0
    for ticker, st in sorted(settlement.get("positions", {}).items()):
        if st.get("exit") is not None or float(st.get("qty") or 0.0) <= 1e-9:
            continue
        panel = panels.get(ticker)
        price = float(st.get("last_price") or st.get("avg_cost") or 0.0)
        if panel is not None and not panel.frame.empty:
            price = float(panel.frame.iloc[-1]["raw_close"])
        value = float(st["qty"]) * price
        holdings_value += value
        stop = float(st.get("stop_price") or 0.0)
        rows.append({
            "ticker": ticker,
            "sector": st.get("sector") or "",
            "opened_at": st.get("opened_at"),
            "tranches_filled": sorted({int(r["tranche"]) for r in st.get("tranches") or []
                                       if r.get("status") == "filled"}),
            "tranches": [{k: r.get(k) for k in ("tranche", "weight", "price_ref", "status",
                                                "trigger", "filled_at", "cancelled_by", "note")}
                         for r in st.get("tranches") or []],
            "stop_distance": round(float(st.get("stop_distance") or 0.0), 4),
            "stop_price": round(stop, 4),
            "stop_headroom_pct": (None if price <= 0 else round((price - stop) / price, 4)),
            "trailing": {"active": bool(stop > float(st.get("initial_stop") or 0.0)),
                         "high_gain_pct": (None if not st.get("first_fill")
                                           else round(float(st.get("trailing_high") or 0.0)
                                                      / float(st["first_fill"]) - 1.0, 4)),
                         "stop_distance": round(float(st.get("stop_distance") or 0.0), 4)},
            "trim_stage": st.get("trim_stage") or "none",
            "trim_done": list(st.get("trim_done") or []),
            "exit": st.get("exit"),
            "last_event_ko": st.get("last_event_ko"),
            "value": value,
        })
    equity = cash + holdings_value
    total = equity if equity > 0 else initial
    positions = [{
        "ticker": r["ticker"], "sector": r["sector"], "opened_at": r["opened_at"],
        "tranches_filled": r["tranches_filled"], "tranches": r["tranches"],
        "stop_distance": r["stop_distance"], "stop_price": r["stop_price"],
        "stop_headroom_pct": r["stop_headroom_pct"], "trailing": r["trailing"],
        "trim_stage": r["trim_stage"], "trim_done": r["trim_done"],
        "last_event_ko": r["last_event_ko"],
        "weight": round(r["value"] / total, 6),
    } for r in rows]
    sector_weights: dict[str, float] = {}
    for p in positions:
        sector_weights[p["sector"]] = round(sector_weights.get(p["sector"], 0.0) + p["weight"], 6)
    return {
        "as_of": as_of.isoformat(),
        "cash_pct": round(cash / total, 6),
        "invested_pct": round(holdings_value / total, 6),
        "equity_index": round(equity / initial, 6),
        "positions": positions,
        "n_positions": len(positions),
        "sector_weights": sector_weights,
        "note_ko": "모의 계좌(초기 100만 달러) — 수량·평단·금액은 공개 산출물에 넣지 않는다.",
    }


def write_health(health: dict, as_of: date, *, path: str | Path = HEALTH_PATH,
                 status: str | None = None, degraded: list | None = None,
                 extra: dict | None = None) -> Path:
    """`data/health.json` — 규칙이 실제로 작동했는지 보는 건강 지표(3.5)."""
    payload = {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "generated_at": _iso(datetime.now(timezone.utc)),
        "status": status,
        "degraded": list(degraded or []),
        **{k: health.get(k) for k in ("positions_open", "tranches_filled", "tranches_pending",
                                      "stop_count", "trim_count", "stop_reentry_blocked_count",
                                      "tranche_cancel_count", "pending_exit_count")},
        "blocked_reentries": list(health.get("blocked_reentries") or []),
        "cancelled_tranches": list(health.get("cancelled_tranches") or []),
        "note_ko": ("손절 후 재진입 차단 수는 손절이 난 종목·세션의 신규 1차 진입을 막은 건수이고,"
                    " 같은 봉에서 취소된 2·3차 주문 수(tranche_cancel_count)와 따로 센다(3.5)."),
    }
    payload.update(extra or {})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def position_checks_from(settlement: dict) -> list[dict]:
    """정산 상태 → `evaluate_exit` 입력(보유 dict). `pipeline` 이 S1~S5 를 다시 계산한다."""
    out = []
    for ticker, st in sorted(settlement.get("positions", {}).items()):
        if st.get("exit") is not None or float(st.get("qty") or 0.0) <= 1e-9:
            continue
        out.append({
            "ticker": ticker, "sector": st.get("sector") or "",
            "shares": st["qty"], "avg_cost": st["avg_cost"],
            "stop_price": st.get("stop_price"), "trailing_high": st.get("trailing_high"),
            "initial_stop": st.get("initial_stop"),
            "tranche": 1, "tranches_filled": sorted({int(r["tranche"]) for r in st.get("tranches") or []
                                                    if r.get("status") == "filled"}),
            "stop_distance": st.get("stop_distance"),
        })
    return out
