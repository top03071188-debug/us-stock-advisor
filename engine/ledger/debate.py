"""engine.ledger.debate — 토론 편입 자격 · 행동 분리(A/B) · 자동 하향 판정 (기획서 3.11.8 · 4.8).

이 모듈이 하는 일은 셋이다.

1. **편입 자격** — `data/debate_reports.json` 의 리포트가 **그 결정**에 붙을 수 있는지
   세 시각과 스냅샷으로 판정한다. 통과한 건만 `data/ledger/debate_events/{signal_id}.json`
   에 append 한다. 발행 레코드(`records.jsonl`)는 **한 글자도 고치지 않는다**(4.7.1).
2. **행동 분리** — 같은 신호에서 `quant_only` 는 그대로 사고, `codex_debated` 는 보류하면
   행동이 갈린 페어다. 의장 권고 등급이 정량 등급보다 **낮을 때만** 갈린다. 올리는 권고는
   언제나 불가(`grade_promote_from_debate: false`).
3. **A/B 비교 채점** — 회피 손실·놓친 수익·순효과·회피 정확도·방어력·`N_eff`·레짐별 분해와
   §4.8.2 의 네 가지 해석 판정. `auto_downgrade_enabled` 전환 조건(30건·`N_eff` 15·
   비용 후 +0.20%p 2분기 연속·방어력 개선)도 여기서만 판단한다.

[고정]
- `auto_downgrade_enabled` 기본값은 **false**. 검증 전에는 표시·기록만 바꾸고 체결은 그대로다.
- `completed_at < signal_published_at` 인 리포트는 `invalid: true` — 과거 결정에 소급해 붙이지 않는다.
- `completed_at >= order_cutoff_at` 인 리포트는 `late: true` — 그날 결정에 쓰지 않는다.
- 토론은 **신뢰도 등급을 올리지 않는다**(3.10). 이 모듈은 등급을 내리는 방향의 재료만 낸다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from engine.ledger import pilot as pilot_mod
from engine.utils.dates import add_trading_days, trading_day_count

SCHEMA_VERSION = 1
REPORTS_PATH = Path("data/debate_reports.json")
EVENTS_DIR = Path("data/ledger/debate_events")
COMPARISON_PATH = Path("data/ledger/debate_comparison.json")
PARAMS_HISTORY_PATH = Path("data/params_history.json")

STATUSES = ("complete", "partial", "failed", "quorum_failed", "stopped_by_limit")
INTENTS = ("pending_buy", "pending_hold_cash", "no_action", "pending_exit")
GRADE_RANK = {"STRONG_BUY": 4, "BUY": 3, "WATCH": 2, "NONE": 1, "": 0}
INTENT_ACTION = {"pending_buy": "buy_filled", "pending_hold_cash": "hold_cash",
                 "no_action": "no_action", "pending_exit": "exit_filled"}
PROMOTE_FROM_DEBATE = False          # [고정] 올리는 권고는 언제나 불가

CLUSTER_TD = 63
MIN_DIVERGENT_PAIRS = 30
MIN_N_EFF = 15
NET_THRESHOLD = 0.0020               # 총자산 대비 +0.20%p
CONSECUTIVE_QUARTERS = 2
#: 왕복 비용 가정(4.7.3 ②와 같은 값) — 보류한 건이 아끼는 비용
COST_BPS = {"slippage": 10.0, "commission": 2.0, "fx": 5.0}

BADGE_DONE = "토론 완료"
BADGE_NONE = "토론 없음"
BADGE_QUORUM = "정족수 미달"
BADGE_LATE = "지연 도착"
BADGE_DIFFERENT = "산식과 다름(보류 권고)"
BADGE_INVALID = "무효(발행 전 완료)"

VERDICT_INSUFFICIENT = "표본 부족 — 판단 보류"
VERDICT_EFFECTIVE = "토론 유효"
VERDICT_NEUTRAL = "차이 없음"
VERDICT_HARMFUL = "토론 역효과"


# ────────────────────────────── 읽기/시각 ──────────────────────────────
def _ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _as_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_reports(path: str | Path = REPORTS_PATH) -> dict:
    """`data/debate_reports.json` — 없으면 `{"reports": []}`(토론 0건은 정상)."""
    p = Path(path)
    if not p.exists():
        return {"schema_version": "1.0", "reports": []}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schema_version": "1.0", "reports": []}
    if not isinstance(payload, dict):
        return {"schema_version": "1.0", "reports": []}
    payload.setdefault("reports", [])
    return payload


def reports_of(doc: dict | None) -> list[dict]:
    return list((doc or {}).get("reports") or [])


def grade_rank(grade) -> int:
    """등급 서열 — `STRONG_BUY > BUY > WATCH > NONE`. 모르는 값은 0(가장 낮게)."""
    return int(GRADE_RANK.get(str(grade or "").upper(), 0))


def quant_grade_of(record: dict) -> str:
    for key in ("trade_grade", "decision", "type"):
        value = record.get(key)
        if value:
            return str(value).upper()
    return ""


# ────────────────────────────── 편입 자격 ──────────────────────────────
def eligibility(report: dict, record: dict) -> dict:
    """토론 편입 4조건(3.11.8) — 하나라도 어기면 그 결정에 붙이지 않는다.

    ① `signal_published_at <= completed_at` — 어기면 `invalid: true`(소급 반영 금지)
    ② `completed_at < order_cutoff_at` — 어기면 `late: true`(그날 결정 미편입)
    ③ `report.snapshot_id == record.snapshot_id`
    ④ `report.signal_id == record.signal_id`
    ⑤ `status == "complete"` — 정족수 미달·실패·중단은 제외
    """
    reasons: list[str] = []
    invalid = False
    late = False
    completed = _ts(report.get("completed_at"))
    published = _ts(report.get("signal_published_at")) or _ts(record.get("signal_published_at"))
    order = _ts(report.get("order_cutoff_at")) or _ts(record.get("order_cutoff_at"))
    if completed is None:
        reasons.append("COMPLETED_AT_MISSING")
    if published is not None and completed is not None and completed < published:
        invalid = True
        reasons.append("COMPLETED_BEFORE_PUBLISHED")
    if order is not None and completed is not None and completed >= order:
        late = True
        reasons.append("COMPLETED_AFTER_ORDER_CUTOFF")
    if str(report.get("snapshot_id") or "") != str(record.get("snapshot_id") or ""):
        reasons.append("SNAPSHOT_MISMATCH")
    if str(report.get("signal_id") or "") != str(record.get("signal_id") or ""):
        reasons.append("SIGNAL_MISMATCH")
    if str(report.get("status") or "") != "complete":
        reasons.append("STATUS_NOT_COMPLETE")
    if bool(report.get("invalid")):
        invalid = True
        reasons.append("REPORT_MARKED_INVALID")
    if bool(report.get("late")):
        late = True
        reasons.append("REPORT_MARKED_LATE")
    return {"eligible": not reasons, "invalid": invalid, "late": late, "reasons": reasons}


def is_eligible_for_signal(report: dict, record: dict) -> bool:
    """편입 4조건 판정의 불리언 이름(3.11.8) — 상세 사유는 `eligibility()` 를 본다."""
    return bool(eligibility(report, record)["eligible"])


# ────────────────────────────── 행동 분리 ──────────────────────────────
def recommendation_of(report: dict) -> str:
    chair = ((report.get("round3") or {}).get("chair") or {})
    return str(chair.get("grade_recommendation") or "").upper()


def veto_codes_of(report: dict, record: dict | None = None) -> list[str]:
    codes = [str(v.get("code")) for v in (record or {}).get("veto") or [] if v.get("code")]
    guard = ((report.get("round3") or {}).get("guard") or {})
    for code in guard.get("veto_codes") or []:
        if code and code not in codes:
            codes.append(str(code))
    return codes


def structural_risk_of(report: dict) -> float | None:
    guard = ((report.get("round3") or {}).get("guard") or {})
    value = guard.get("structural_risk")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def news_state_of(report: dict) -> str | None:
    """리포트에서 읽어 낼 수 있는 뉴스 판독 상태 — 없으면 `None`(기존 상태 유지)."""
    value = report.get("news_state")
    if value in ("not_read", "insufficient_evidence", "read_done"):
        return str(value)
    validation = report.get("validation") or {}
    dropped = int(validation.get("quotes_dropped") or 0)
    checked = int(validation.get("quotes_checked") or 0)
    news_read = [n for row in report.get("round1") or [] for n in (row.get("news_read") or [])]
    if dropped and not checked:
        return "insufficient_evidence"
    if news_read and not dropped:
        return "read_done"
    return None


def actions_for(report: dict, record: dict, execution: dict | None = None,
                *, auto_downgrade_enabled: bool = False) -> dict:
    """`quant_only` 와 `codex_debated` 의 행동을 가른다(3.11.8 표).

    - Veto 있음 → 둘 다 `no_action`(AI 무관, 채점 안 함)
    - 권고 < 정량 등급 → 정량은 `buy_filled`, 토론은 `hold_cash` (**행동이 갈린 페어**)
    - 권고 ≥ 정량 등급 → 행동 동일 (`grade_promote_from_debate: false` 는 승격을 막는다)
    - `auto_downgrade_enabled: true` 면 정량 트랙도 보류한다(그때는 A/B 가 끝난다)
    """
    quant_grade = quant_grade_of(record)
    recommendation = recommendation_of(report)
    vetoes = veto_codes_of(report, record)
    intent = str(record.get("quant_intent") or "no_action")
    quant_action = INTENT_ACTION.get(intent, "no_action")
    track = (execution or {}).get("quant_track") or {}
    if track.get("execution_event"):
        quant_action = str(track["execution_event"])
    report_intent = str(report.get("intent") or "")
    divergent = False
    applied = False
    reason = None
    if vetoes:
        debate_action = "no_action"
        reason = "VETO_BLOCKED"
    elif (not PROMOTE_FROM_DEBATE and recommendation and quant_grade
          and grade_rank(recommendation) < grade_rank(quant_grade)
          and quant_action == "buy_filled"):
        debate_action = "hold_cash"
        divergent = True
        reason = "RECOMMENDATION_LOWER"
        if auto_downgrade_enabled:
            quant_action = "hold_cash"
            divergent = False
            applied = True
    else:
        debate_action = quant_action
        reason = "SAME_ACTION"
    return {
        "quant_grade": quant_grade,
        "recommendation": recommendation,
        "report_intent": report_intent or None,
        "quant_action": quant_action,
        "debate_action": debate_action,
        "divergent": bool(divergent),
        "same_action": bool(not divergent),
        "auto_downgrade_applied": bool(applied),
        "veto_codes": vetoes,
        "reason": reason,
    }


def debate_badge(report: dict | None, el: dict | None = None, actions: dict | None = None) -> str:
    """화면 배지 — `토론 완료`/`토론 없음`/`정족수 미달`/`지연 도착`/`산식과 다름(보류 권고)`."""
    if not report:
        return BADGE_NONE
    if str(report.get("status") or "") != "complete":
        return BADGE_QUORUM
    el = el or {}
    if el.get("invalid"):
        return BADGE_INVALID
    if el.get("late"):
        return BADGE_LATE
    if (actions or {}).get("divergent"):
        return BADGE_DIFFERENT
    return BADGE_DONE


def display_grade(actions: dict) -> str | None:
    """표시 등급 — 자동 하향이 꺼져 있으면 정량 등급 그대로(3.11.8)."""
    if actions.get("auto_downgrade_applied"):
        return actions.get("recommendation") or actions.get("quant_grade")
    return actions.get("quant_grade")


def public_report(report: dict, el: dict | None = None, actions: dict | None = None) -> dict:
    """공개용 토론 요약 — 인용 원문·본문 발췌는 싣지 않는다(2.2.3). 해시 검증 배지만."""
    actions = actions or {}
    el = el or {}
    rounds1 = []
    for row in report.get("round1") or []:
        rounds1.append({
            "skill": row.get("skill"),
            "score": row.get("score"),
            "stance": row.get("stance"),
            "confidence": row.get("confidence"),
            "reasons": list(row.get("reasons") or [])[:3],
            "quote_verified": bool(row.get("news_read")) and all(
                n.get("quote_verified", True) for n in row.get("news_read") or []),
            "quote_sha256_count": len([n for n in row.get("news_read") or [] if n.get("quote_sha256")]),
        })
    rounds2 = []
    for row in report.get("round2") or []:
        rounds2.append({
            "skill": row.get("skill"),
            "score": row.get("score"),
            "changed": bool(row.get("changed")),
            "rebuttals": [{"target_skill": r.get("target_skill"), "point": r.get("point")}
                          for r in (row.get("rebuttals") or [])[:2]],
        })
    round3 = report.get("round3") or {}
    guard = round3.get("guard") or {}
    chair = round3.get("chair") or {}
    validation = report.get("validation") or {}
    return {
        "report_id": report.get("report_id"),
        "ticker": report.get("ticker"),
        "signal_id": report.get("signal_id"),
        "snapshot_id": report.get("snapshot_id"),
        "status": report.get("status"),
        "badge": debate_badge(report, el, actions),
        "eligible": bool(el.get("eligible")),
        "late": bool(el.get("late")),
        "invalid": bool(el.get("invalid")),
        "reasons": list(el.get("reasons") or []),
        "completed_at": report.get("completed_at"),
        "signal_published_at": report.get("signal_published_at"),
        "order_cutoff_at": report.get("order_cutoff_at"),
        "quant_grade": actions.get("quant_grade") or report.get("quant_grade"),
        "display_grade": display_grade(actions),
        "recommendation": actions.get("recommendation") or chair.get("grade_recommendation"),
        "track": "codex_debated" if el.get("eligible") else None,
        "divergent": bool(actions.get("divergent")),
        "quant_action": actions.get("quant_action"),
        "debate_action": actions.get("debate_action"),
        "auto_downgrade_applied": bool(actions.get("auto_downgrade_applied")),
        "round1": rounds1,
        "round2": rounds2,
        "round3": {
            "guard": {"veto_codes": list(guard.get("veto_codes") or []),
                      "additional_blocks": list(guard.get("additional_blocks") or []),
                      "structural_risk": structural_risk_of(report),
                      "reasoning": guard.get("reasoning")},
            "chair": {"consensus": list(chair.get("consensus") or [])[:3],
                      "dissent": list(chair.get("dissent") or [])[:3],
                      "grade_recommendation": chair.get("grade_recommendation"),
                      "entry_plan_refs": list(chair.get("entry_plan_refs") or [])[:5],
                      "exit_refs": list(chair.get("exit_refs") or [])[:5],
                      "conflict": bool(chair.get("conflict")),
                      "one_line_ko": chair.get("one_line_ko")},
        },
        "validation": {k: validation.get(k) for k in ("skills_in", "skills_kept", "quotes_checked",
                                                      "quotes_dropped", "numbers_checked",
                                                      "numbers_rejected")},
        # 파이프라인이 읽는 값 — 토론 유래 뉴스 상태와 구조적 리스크(3.11.8).
        "news_state": news_state_of(report),
        "structural_risk": structural_risk_of(report),
        "quote_verified": all(row.get("quote_verified", True) for row in rounds1) if rounds1 else None,
    }


# ────────────────────────────── 이벤트 append ──────────────────────────────
def append_event(event_dir: str | Path, payload: dict) -> tuple[Path, bool]:
    """`debate_events/{signal_id}.json` 에 append — 같은 `report_id` 는 한 번만.

    발행 레코드는 건드리지 않는다. 파일이 이미 있으면 **이벤트 배열에만** 덧붙인다.
    """
    sid = str(payload.get("signal_id"))
    path = Path(event_dir) / f"{sid}.json"
    doc = {"signal_id": sid, "snapshot_id": payload.get("snapshot_id"), "events": []}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                doc = {"signal_id": old.get("signal_id") or sid,
                       "snapshot_id": old.get("snapshot_id") or payload.get("snapshot_id"),
                       "events": list(old.get("events") or [])}
        except json.JSONDecodeError:
            pass
    added = 0
    for event in payload.get("events") or []:
        duplicate = any(old.get("report_id") == event.get("report_id")
                        and old.get("event") == event.get("event") for old in doc["events"])
        if duplicate:
            continue
        doc["events"].append(event)
        added += 1
    if not added:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path, True


def load_events(event_dir: str | Path = EVENTS_DIR) -> dict[str, dict]:
    root = Path(event_dir)
    out: dict[str, dict] = {}
    if not root.exists():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("signal_id"):
            out[str(payload["signal_id"])] = payload
    return out


def _event_payload(report: dict, record: dict, el: dict, actions: dict, *, regime: str | None,
                   at: datetime) -> dict:
    """편입 이벤트 1건 — `record.debate_event` 와 같은 골격에 A/B 판정 필드를 더한다."""
    event = {
        "event": "debate_attached",
        "report_id": report.get("report_id"),
        "track": "codex_debated",
        "intent": (report.get("intent") if report.get("intent") in INTENTS
                   else ("pending_hold_cash" if actions["divergent"]
                         else record.get("quant_intent") or "no_action")),
        "completed_at": report.get("completed_at"),
        "recorded_at": _iso(at),
        "eligible": bool(el.get("eligible")),
        "late": bool(el.get("late")),
        "invalid": bool(el.get("invalid")),
        "auto_downgrade_applied": bool(actions.get("auto_downgrade_applied")),
        "quant_grade": actions.get("quant_grade"),
        "recommendation": actions.get("recommendation"),
        "display_grade": display_grade(actions),
        "quant_action": actions.get("quant_action"),
        "debate_action": actions.get("debate_action"),
        "divergent": bool(actions.get("divergent")),
        "veto_codes": list(actions.get("veto_codes") or []),
        "regime": regime,
    }
    return {"signal_id": str(record.get("signal_id")), "snapshot_id": record.get("snapshot_id"),
            "events": [event]}


def reconcile_debates(reports_doc: dict | None, records: list[dict],
                      executions: dict[str, dict] | None = None, *,
                      event_dir: str | Path = EVENTS_DIR, params: dict | None = None,
                      regime: str | None = None, as_of: date | None = None,
                      write: bool = True, now: datetime | None = None) -> dict:
    """리포트 → 편입 자격 판정 → `debate_events` append. 토론 0건이면 조용히 0건이다.

    `by_signal` 은 자격 미달 건도 담는다 — 화면이 `지연 도착`·`정족수 미달` 배지를
    보여 줘야 하기 때문이다(다만 그 결정의 트랙에는 붙지 않는다).
    """
    executions = executions or {}
    auto = bool(((params or {}).get("debate") or {}).get("auto_downgrade_enabled", False))
    reports = reports_of(reports_doc)
    index = {str(r.get("signal_id")): r for r in records or []}
    at = now or datetime.now(timezone.utc)
    attached: list[dict] = []
    rejected: list[dict] = []
    by_signal: dict[str, dict] = {}
    n_late = n_invalid = 0
    for report in reports:
        sid = str(report.get("signal_id") or "")
        record = index.get(sid)
        if record is None:
            rejected.append({"signal_id": sid, "report_id": report.get("report_id"),
                             "reason": "RECORD_MISSING"})
            by_signal[sid] = {"signal_id": sid, "ticker": report.get("ticker"),
                              "status": report.get("status"),
                              "badge": debate_badge(report, {"eligible": False, "late": False,
                                                             "invalid": False}, None),
                              "eligible": False, "report_id": report.get("report_id")}
            continue
        el = eligibility(report, record)
        actions = actions_for(report, record, executions.get(sid),
                              auto_downgrade_enabled=auto)
        summary = public_report(report, el, actions)
        by_signal[sid] = summary
        if el.get("late"):
            n_late += 1
        if el.get("invalid"):
            n_invalid += 1
        if not el["eligible"]:
            rejected.append({"signal_id": sid, "report_id": report.get("report_id"),
                             "reasons": el["reasons"]})
            continue
        payload = _event_payload(report, record, el, actions, regime=regime, at=at)
        if write:
            append_event(event_dir, payload)
        attached.append({"signal_id": sid, "ticker": record.get("ticker"),
                         "report_id": report.get("report_id"),
                         "divergent": actions["divergent"],
                         "quant_action": actions["quant_action"],
                         "debate_action": actions["debate_action"]})
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat() if as_of else None,
        "generated_at": _iso(at),
        "auto_downgrade_enabled": auto,
        "n_reports": len(reports),
        "n_eligible": len(attached),
        "n_late": n_late,
        "n_invalid": n_invalid,
        "n_rejected": len(rejected),
        "attached": attached,
        "rejected": rejected,
        "by_signal": by_signal,
    }


# ────────────────────────────── A/B 비교 채점 ──────────────────────────────
def _frames(panel) -> pd.DataFrame | None:
    if panel is None:
        return None
    frame = getattr(panel, "frame", None)
    if frame is None or frame.empty:
        return None
    return frame


def pair_outcome(panel, entry_day: date | None, as_of: date, horizon_td: int = CLUSTER_TD) -> dict:
    """진입 세션 → 63거래일 뒤 수익률과 그 구간 MDD. 만기 봉이 없으면 `complete: false`."""
    frame = _frames(panel)
    empty = {"return": None, "mdd": None, "complete": False, "days": 0}
    if frame is None or entry_day is None:
        return empty
    window = frame.loc[frame.index <= pd.Timestamp(as_of)]
    if window.empty:
        return empty
    start = window.loc[window.index >= pd.Timestamp(entry_day)]
    if start.empty:
        return empty
    first = start.index[0]
    path = window.loc[window.index >= first].head(horizon_td + 1)
    if len(path) < horizon_td + 1:
        return {"return": None, "mdd": None, "complete": False, "days": len(path) - 1}
    closes = path["sadj_close"].astype(float)
    base = float(closes.iloc[0])
    if base <= 0:
        return empty
    growth = closes / base
    peak = growth.cummax()
    mdd = float((growth / peak - 1.0).min())
    return {"return": round(float(growth.iloc[-1] - 1.0), 6), "mdd": round(mdd, 6),
            "complete": True, "days": horizon_td}


def _quantile(values: list[float], q: float) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 6)
    pos = (len(vals) - 1) * q
    low = int(pos)
    high = min(low + 1, len(vals) - 1)
    frac = pos - low
    return round(vals[low] * (1 - frac) + vals[high] * frac, 6)


def quarter_of(day: date | None) -> str | None:
    if day is None:
        return None
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def _verdict(pairs: int, n_eff: int, net_after_cost: float | None, defense_improved: bool) -> str:
    """§4.8.2 해석 규칙 — 결과를 보고 기준을 고치지 않는다(사전에 못 박은 네 갈래)."""
    if pairs < MIN_DIVERGENT_PAIRS or n_eff < MIN_N_EFF or net_after_cost is None:
        return VERDICT_INSUFFICIENT
    if net_after_cost <= -NET_THRESHOLD:
        return VERDICT_HARMFUL
    if net_after_cost >= NET_THRESHOLD and defense_improved:
        return VERDICT_EFFECTIVE
    return VERDICT_NEUTRAL


def _qualifies_for_downgrade(row: dict) -> bool:
    """자동 하향 전환 조건 중 분기 단위로 볼 수 있는 것 — 표본·효과·방어력."""
    return bool(float(row.get("net_after_cost") or 0.0) >= NET_THRESHOLD
                and (row.get("drawdown") or {}).get("defense_improved")
                and int(row.get("n_graded") or 0) >= MIN_DIVERGENT_PAIRS
                and int(row.get("n_eff") or 0) >= MIN_N_EFF)


def _bucket(pairs: list[dict]) -> dict:
    """페어 묶음 → 회피 손실·놓친 수익·순효과·정확도·방어력 (4.8.2)."""
    graded = [p for p in pairs if p.get("complete")]
    weights = [float(p.get("weight") or 0.0) for p in graded]
    avoided = sum(max(0.0, -float(p["return"])) * float(p.get("weight") or 0.0) for p in graded)
    missed = sum(max(0.0, float(p["return"])) * float(p.get("weight") or 0.0) for p in graded)
    net = avoided - missed
    lost = [p for p in graded if float(p["return"]) < 0]
    mdd_quant = [float(p.get("mdd") or 0.0) for p in graded]
    mdd_debate = [0.0 for _ in graded]
    tail_quant = _quantile(mdd_quant, 0.05)
    tail_debate = _quantile(mdd_debate, 0.05)
    defense = bool(tail_quant is not None and tail_debate is not None and tail_debate > tail_quant)
    cost = sum(float(w) * (2.0 * sum(COST_BPS.values()) / 10_000.0) for w in weights)
    cluster = pilot_mod.n_eff([{"ticker": p.get("ticker"), "as_of": p.get("entry_day")}
                               for p in graded], horizon_td=CLUSTER_TD)
    return {
        "pairs": len(pairs),
        "n_graded": len(graded),
        "n_eff": cluster["n_eff"],
        "avoided_loss": round(avoided, 6),
        "missed_gain": round(missed, 6),
        "net": round(net, 6),
        "cost": round(cost, 6),
        "net_after_cost": round(net + cost, 6),
        "avoidance_accuracy": None if not graded else round(len(lost) / len(graded), 4),
        "net_pp": round(net * 100.0, 4),
        "net_after_cost_pp": round((net + cost) * 100.0, 4),
        "drawdown": {"quant_mean_mdd": _quantile(mdd_quant, 0.5), "quant_tail_5pct": tail_quant,
                     "debate_mean_mdd": 0.0, "debate_tail_5pct": 0.0,
                     "defense_improved": defense},
    }


def compute_debate_comparison(by_signal: dict[str, dict], records: list[dict],
                              executions: dict[str, dict] | None = None, panels: dict | None = None,
                              as_of: date | None = None, *, params: dict | None = None,
                              horizon_td: int = CLUSTER_TD,
                              regime_lookup: dict[str, str] | None = None,
                              now: datetime | None = None) -> dict:
    """`debate_comparison.json` — 행동이 갈린 페어만 채점한다.

    `by_signal` 은 `reconcile_debates` 의 결과(자격 판정이 끝난 화면용 요약)다.
    체결이 없는 결정(`hold_cash`)은 같은 기간에 샀을 때의 손익을 **가상 체결가**로 계산한다
    (4.7.2) — 다만 페어의 기준 수익률은 실제 체결가가 있는 `quant_only` 쪽이므로,
    `quant_action == "buy_filled"` 인 페어만 집계한다.
    """
    executions = executions or {}
    panels = panels or {}
    regime_lookup = regime_lookup or {}
    auto = bool(((params or {}).get("debate") or {}).get("auto_downgrade_enabled", False))
    index = {str(r.get("signal_id")): r for r in records or []}
    pairs: list[dict] = []
    same_action = 0
    for sid, view in (by_signal or {}).items():
        if not view.get("eligible"):
            continue
        if not view.get("divergent"):
            same_action += 1
            continue
        record = index.get(sid) or {}
        event = executions.get(sid) or {}
        track = event.get("quant_track") or {}
        position = record.get("position") or {}
        weight = track.get("weight")
        if weight in (None, 0):
            weight = position.get("tranche_weight") or 0.0
        entry_day = _as_date(str(event.get("at") or "")[:10]) or _as_date(record.get("target_session"))
        ticker = record.get("ticker") or view.get("ticker")
        outcome = pair_outcome(panels.get(ticker), entry_day, as_of or date.today(), horizon_td)
        regime = (regime_lookup.get(sid) or event.get("regime") or record.get("regime") or "unknown")
        pairs.append({
            "signal_id": sid, "ticker": ticker, "entry_day": entry_day.isoformat() if entry_day else None,
            "weight": round(float(weight or 0.0), 6), "regime": str(regime),
            "quant_action": view.get("quant_action"), "debate_action": view.get("debate_action"),
            "quant_grade": view.get("quant_grade"), "recommendation": view.get("recommendation"),
            **outcome,
        })
    overall = _bucket(pairs)
    by_regime: dict[str, dict] = {}
    for name in ("risk_on", "neutral", "risk_off", "unknown"):
        bucket = [p for p in pairs if p.get("regime") == name]
        if bucket:
            by_regime[name] = _bucket(bucket)
    quarters: dict[str, list[dict]] = {}
    for pair in pairs:
        key = quarter_of(_as_date(pair.get("entry_day")))
        if key:
            quarters.setdefault(key, []).append(pair)
    quarter_rows = []
    for key in sorted(quarters):
        row = _bucket(quarters[key])
        row["quarter"] = key
        row["verdict"] = _verdict(row["n_graded"], row["n_eff"], row["net_after_cost"],
                                  row["drawdown"]["defense_improved"])
        quarter_rows.append(row)
    verdict = _verdict(overall["n_graded"], overall["n_eff"], overall["net_after_cost"],
                       overall["drawdown"]["defense_improved"])
    consecutive = 0
    for row in reversed(quarter_rows):
        if _qualifies_for_downgrade(row):
            consecutive += 1
        else:
            break
    at = now or datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat() if as_of else None,
        "generated_at": _iso(at),
        "horizon_td": horizon_td,
        "auto_downgrade_enabled": auto,
        "divergent_pairs": len(pairs),
        "same_action_pairs": same_action,
        "n_graded_pairs": overall["n_graded"],
        "n_eff": overall["n_eff"],
        "avoided_loss": overall["avoided_loss"],
        "missed_gain": overall["missed_gain"],
        "net": overall["net"],
        "cost": overall["cost"],
        "net_after_cost": overall["net_after_cost"],
        "avoided_loss_pp": round(overall["avoided_loss"] * 100.0, 4),
        "missed_gain_pp": round(overall["missed_gain"] * 100.0, 4),
        "net_pp": overall["net_pp"],
        "net_after_cost_pp": overall["net_after_cost_pp"],
        "avoidance_accuracy": overall["avoidance_accuracy"],
        "drawdown": overall["drawdown"],
        "pairs": pairs,
        "by_regime": by_regime,
        "quarters": quarter_rows,
        "consecutive_quarters": consecutive,
        "verdict": verdict,
        "verdict_ko": verdict,
        "note_ko": ("토론 비교 표본 없음 — 행동이 갈린 페어가 아직 없다(토론 0건이면 정상)."
                    if not pairs else None),
    }


def write_comparison(comparison: dict, path: str | Path = COMPARISON_PATH) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# ────────────────────────────── 자동 하향 판정 ──────────────────────────────
def evaluate_auto_downgrade_eligibility(comparison: dict,
                                        params_history: list[dict] | None = None) -> dict:
    """`auto_downgrade_enabled` 전환 권고 (4.8.2 · 3.11.8).

    조건을 **모두** 만족할 때만 `recommend_enable: true` 다.
    ① 행동 갈린 페어 ≥ 30  ② `N_eff` ≥ 15  ③ 비용 후 순효과 ≥ +0.20%p **2분기 연속**
    ④ 방어력 개선. 하나라도 못 넘으면 자동 하향은 계속 OFF 다.
    """
    comparison = comparison or {}
    rows = comparison.get("quarters") or []
    consecutive = 0
    for row in reversed(rows):
        if _qualifies_for_downgrade(row):
            consecutive += 1
        else:
            break
    pairs = int(comparison.get("n_graded_pairs") or 0)
    n_eff = int(comparison.get("n_eff") or 0)
    net = comparison.get("net_after_cost")
    defense = bool((comparison.get("drawdown") or {}).get("defense_improved"))
    reasons = []
    if pairs < MIN_DIVERGENT_PAIRS:
        reasons.append(f"행동 갈린 페어 {pairs}건 < {MIN_DIVERGENT_PAIRS}건")
    if n_eff < MIN_N_EFF:
        reasons.append(f"유효 표본 N_eff {n_eff} < {MIN_N_EFF}")
    if net is None or float(net) < NET_THRESHOLD:
        reasons.append(f"비용 후 순효과 {0.0 if net is None else float(net) * 100:.2f}%p < +0.20%p")
    if not defense:
        reasons.append("방어력 개선 없음")
    if consecutive < CONSECUTIVE_QUARTERS:
        reasons.append(f"기준 충족 분기 {consecutive}개 < {CONSECUTIVE_QUARTERS}개 연속")
    eligible = not reasons
    history = params_history if isinstance(params_history, list) else []
    return {
        "current": bool(comparison.get("auto_downgrade_enabled")),
        "recommend_enable": bool(eligible),
        "eligible": bool(eligible),
        "verdict": comparison.get("verdict") or VERDICT_INSUFFICIENT,
        "pairs": pairs,
        "n_eff": n_eff,
        "net_after_cost": net,
        "net_after_cost_pp": comparison.get("net_after_cost_pp"),
        "defense_improved": defense,
        "consecutive_quarters": consecutive,
        "quarters": [{"quarter": row.get("quarter"), "n_graded": row.get("n_graded"),
                      "n_eff": row.get("n_eff"), "net_after_cost": row.get("net_after_cost"),
                      "defense_improved": (row.get("drawdown") or {}).get("defense_improved"),
                      "verdict": row.get("verdict")}
                     for row in rows],
        "reasons_ko": reasons or ["네 조건을 모두 충족 — 전환 후보다(전환은 PR 로만 한다)"],
        "history_versions": [h.get("version") for h in history][-5:],
        "note_ko": "자동 하향은 이 판정을 통과해도 사람이 PR 로 켠다 — 기본값은 false(4.8.2).",
    }


def record_auto_downgrade_evaluation(path: str | Path, comparison: dict,
                                     params_version: str = "params_v001",
                                     now: datetime | None = None) -> tuple[Path, bool]:
    """판정을 `params_history.json` 에 이력으로 남긴다(append-only, 같은 분기·같은 판정은 재기록 없음).

    분기가 없으면(페어 0건) 아무것도 쓰지 않는다 — 빈 판정으로 이력을 부풀리지 않는다.
    """
    p = Path(path)
    history: list[dict] = []
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            history = loaded if isinstance(loaded, list) else list(loaded.get("versions") or [])
        except json.JSONDecodeError:
            history = []
    evaluation = evaluate_auto_downgrade_eligibility(comparison, history)
    rows = comparison.get("quarters") or []
    if not rows:
        return p, False
    latest = rows[-1]
    quarter = str(latest.get("quarter"))
    verdict = str(latest.get("verdict"))
    for entry in history:
        debate = entry.get("debate") or {}
        if debate.get("quarter") == quarter and debate.get("verdict") == verdict:
            return p, False
    n = sum(1 for entry in history if str((entry.get("debate") or {}).get("quarter")) == quarter) + 1
    at = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    history.append({
        "version": f"{params_version}-debate-{quarter}-{n:02d}",
        "created_at": at.date().isoformat(),
        "parent_version": params_version,
        "changed": [],
        "gate_result": {"status": "recommend_enable" if evaluation["recommend_enable"] else "hold",
                        "note_ko": ("자동 하향 전환 조건 충족 — PR 로 켤 수 있다" if evaluation["recommend_enable"]
                                    else "자동 하향 유지(false) — 조건 미충족")},
        "note_ko": f"토론 A/B 판정 이력({quarter}) — 체결 규칙은 바뀌지 않는다.",
        "debate": {
            "quarter": quarter,
            "verdict": verdict,
            "pairs": latest.get("n_graded"),
            "n_eff": latest.get("n_eff"),
            "net_after_cost": latest.get("net_after_cost"),
            "defense_improved": (latest.get("drawdown") or {}).get("defense_improved"),
            "auto_downgrade_enabled": bool(comparison.get("auto_downgrade_enabled")),
            "recommend_enable": evaluation["recommend_enable"],
            "reasons_ko": evaluation["reasons_ko"],
        },
    })
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p, True


def comparison_summary(comparison: dict) -> dict:
    """화면·요약용 최소 필드 — 표본이 작으면 숫자 대신 '판정 보류'를 크게 보여 준다(5.4)."""
    comparison = comparison or {}
    pairs = int(comparison.get("n_graded_pairs") or 0)
    net_pp = comparison.get("net_after_cost_pp")
    return {
        "pairs": pairs,
        "divergent_pairs": int(comparison.get("divergent_pairs") or 0),
        "same_action_pairs": int(comparison.get("same_action_pairs") or 0),
        "n_eff": int(comparison.get("n_eff") or 0),
        "avoided_loss_pp": comparison.get("avoided_loss_pp"),
        "missed_gain_pp": comparison.get("missed_gain_pp"),
        "net_pp": comparison.get("net_pp"),
        "net_after_cost_pp": comparison.get("net_after_cost_pp"),
        "avoidance_accuracy": comparison.get("avoidance_accuracy"),
        "verdict": comparison.get("verdict") or VERDICT_INSUFFICIENT,
        "auto_downgrade_enabled": bool(comparison.get("auto_downgrade_enabled")),
        "sample_short": pairs < MIN_DIVERGENT_PAIRS,
        "label_ko": (f"표본 {pairs}건 — 판정 보류" if pairs < MIN_DIVERGENT_PAIRS or net_pp is None
                     else f"순효과 {float(net_pp):+.2f}%p"),
    }


def next_trading_session(day: date, n: int = 1) -> date:
    """편의 함수 — 테스트가 63거래일 만기를 만들 때 쓴다."""
    return add_trading_days(day, n)


def sessions_between(start: date, end: date) -> int:
    return trading_day_count(start, end)
