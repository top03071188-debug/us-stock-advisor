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

import copy
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from engine.ledger import pilot as pilot_mod
from engine.utils.dates import add_trading_days, trading_day_count

SCHEMA_VERSION = 1
REPORTS_PATH = Path("data/debate_reports.json")
EVENTS_DIR = Path("data/ledger/debate_events")
COMPARISON_PATH = Path("data/ledger/debate_comparison.json")
PARAMS_HISTORY_PATH = Path("data/params_history.json")
#: 주문 동결 마감(`live.FREEZE_BUFFER` 와 같은 값) — 이 시각 뒤에 검증된 결과는 당일 주문에 못 붙는다.
FREEZE_BUFFER = timedelta(minutes=5)

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
#: 검증이 **체결 뒤**라 소급 편입할 수 없는 경우 — "발행 전 완료"와 사유가 다르다(감사 02 부수).
BADGE_INVALID_AFTER_FILL = "무효(체결 후 검증)"

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


def _freeze_deadline(order_cutoff: datetime | None,
                     buffer: timedelta = FREEZE_BUFFER) -> datetime | None:
    """주문 동결 마감 — 주문 마감보다 앞선 내부 확정 시각(2.2.2)."""
    return None if order_cutoff is None else order_cutoff - buffer


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


# ────────────────────────────── 바이트 영수증(§10 · 치명 02) ──────────────────────────────
#: 영수증 필드 — 해시 계산에서 **제외**한다(자기참조 금지).
#: `codex_debate/validate.py` 의 `report_receipt_sha256` 과 **같은 정규화**를 쓴다(공유 계약).
RECEIPT_FIELDS = ("report_sha256",)


def _receipt_excluded(payload: dict) -> dict:
    """영수증 필드를 걷어낸 보고서 사본 — 정규 바이트의 입력이다."""
    body = json.loads(json.dumps(payload or {}, ensure_ascii=False, default=str))
    validation = body.get("validation")
    if isinstance(validation, dict):
        for key in RECEIPT_FIELDS:
            validation.pop(key, None)
    return body


def compute_report_sha256(report: dict) -> str:
    """보고서 **정규 바이트**의 SHA-256 — `sha256:<hex>`.

    정규화는 `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`
    이고, `validation.report_sha256` 만 **자기참조를 피해** 뺀다. 발행 측
    (`codex_debate/validate.report_receipt_sha256`)과 같은 식이므로 양쪽으로 계산해 대조할 수 있다.
    """
    blob = json.dumps(_receipt_excluded(report), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def with_report_receipt(report: dict) -> dict:
    """보고서 사본에 영수증을 붙인다(발행 측·픽스처용). 원본은 그대로 둔다."""
    out = copy.deepcopy(dict(report or {}))
    out["validation"] = {**(out.get("validation") or {}),
                         "report_sha256": compute_report_sha256(report)}
    return out


def report_receipt(report: dict) -> str | None:
    """보고서에 실린 영수증 값 — 없으면 `None`."""
    validation = report.get("validation") or {}
    for value in (validation.get("report_sha256"), report.get("report_sha256"),
                  validation.get("receipt_sha256"), report.get("receipt_sha256")):
        if value:
            return str(value)
    return None


# ────────────────────────────── 편입 자격 ──────────────────────────────
def eligibility(report: dict, record: dict, *, now: datetime | None = None,
                report_validated_at=None, frozen_at=None,
                execution: dict | None = None,
                require_receipt: bool = False, strict: bool = False) -> dict:
    """토론 편입 자격 — **시각 계약**(3.11.8 · 2.2.2 · P0-2 · v1.3.5).

    `signal_observed_at ≤ completed_at ≤ report_validated_at ≤ frozen_at < order_cutoff_at` 를
    모두 만족해야 한다. 그 외:

    ① `completed_at < signal_observed_at` → `invalid`(소급 반영 금지)
    ② `completed_at ≥ order_cutoff_at` 또는 **검증 시각** ≥ 마감 → `late`(당일 결정 미편입)
    ③ `report_validated_at` 은 **편입 프로세스가 정확한 바이트를 받아 검증한 시각**이다. 명시 인자 >
       보고서 > `validation` 블록 > 레코드 > `now` 순으로 찾는다.
       **동결 정각은 편입**이다 — `validated_at <= frozen_at` 이면 `late=False`, `> frozen_at` 이면
       `late=True`(P-2: 기획서의 두 문장을 이 하나로 고정).
    ④ 이미 체결된 주문(`execution.at` < 검증 시각)에는 **소급 편입 금지** — `ATTACHED_AFTER_FILL`.
    ⑤ 스냅샷·`signal_id`·`status` 일치.

    [v1.3.5 · 치명 02]
    - **`now` 는 원장을 실제로 읽어 판정한 수신 시각이다.** 보고서가 그보다 늦은 검증 시각을 적으면 그
      값을 믿지 않고 `now` 로 깎는다(`POSTHOC_VALIDATION`) — 나중에 만들어진 보고서가 마감 전 검증으로
      통과하지 않는다.
    - **원장이 우선**: `order_cutoff_at`·`frozen_at` 은 레코드(원장) 값을 먼저 쓰고 보고서 내부 값은
      폴백이다. 보고서가 자기 마감을 넓게 적어 통과하는 경로를 막는다.

    [v1.3.6 · 감사 5331ab7-01] **신뢰 경계는 수신 시각 하나다.**
    - `completed_at > now`(배치가 그 파일을 읽은 시각보다 **나중에 완료**된 보고서)는 존재할 수 없다 —
      `now` 상한 보정이 그런 보고서를 "동결 전 완료"로 **구제하지 못하도록** `invalid` +
      `COMPLETED_AFTER_NOW` 로 **무조건** 부적격 처리한다(예전에는 `POSTHOC_VALIDATION` 메모만 달고
      `eligible=True` 로 통과했다).
    - 남는 사슬은 `completed_at ≤ report_validated_at ≤ now` 다 — 검증 시각을 `now` 로 깎은 뒤에도
      이 순서가 유지된다.
    - **규칙 마감과 실제 시각을 섞지 않는다** — `freeze_deadline_at`(주문마감 − 5분, 규칙값)과
      `frozen_at`(레코드가 밝힌 실제 동결 시각, 없으면 규칙값)을 따로 돌려준다. `frozen_at` 은
      기존 호출 호환을 위해 **그대로 둔다**.
    - **`strict=True`** 면 `completed_at`·`report_validated_at`·`order_cutoff_at` 이 빠진 보고서는
      검사를 건너뛰지 않고 `MISSING_TIMESTAMPS` 로 **부적격**이다(구 리포트의 무검증 통과 방지).
      기본값은 `False`(단위 호출 호환)이고 프로덕션 경로(`reconcile_debates`)는 `True` 다.
    - **`report_sha256` 영수증**: 보고서에 영수증이 있으면 영수증 필드를 뺀 정규 바이트의 SHA-256 과
      대조해 다르면 `REPORT_HASH_MISMATCH`(부적격), `require_receipt=True` 인데 없으면
      `REPORT_RECEIPT_MISSING`(부적격)이다.
    """
    reasons: list[str] = []
    notes: list[str] = []
    invalid = False
    late = False
    completed = _ts(report.get("completed_at"))
    published = _ts(report.get("signal_published_at")) or _ts(record.get("signal_published_at"))
    observed = (_ts(report.get("signal_observed_at")) or _ts(record.get("signal_observed_at"))
                or published)
    # [v1.3.5] 원장의 마감·동결이 보고서 내부 값보다 **항상 우선**한다.
    order = _ts(record.get("order_cutoff_at")) or _ts(report.get("order_cutoff_at"))
    received = _ts(now)                       # 배치가 이 보고서를 읽어 판정한 수신 시각(신뢰 상한)
    argued = _ts(report_validated_at)
    in_report = _ts(report.get("report_validated_at"))
    in_validation = _ts((report.get("validation") or {}).get("report_validated_at"))
    in_record = _ts(record.get("report_validated_at"))
    claimed = argued or in_report or in_validation or in_record
    validation_source = ("argument" if argued is not None else "report" if in_report is not None
                         else "validation" if in_validation is not None
                         else "record" if in_record is not None else None)
    validated = claimed
    if validated is None and not strict:
        # 구 호출 호환: 시각이 없으면 `now`(검증 시각)로 대신한다. strict 에서는 `now` 가 **상한**일 뿐이라
        # 이 폴백을 쓰지 않는다 — 그러지 않으면 시각 없는 구 리포트가 "방금 검증"으로 통과한다.
        validated = received
        validation_source = "now_fallback" if received is not None else None
    # 규칙 마감(주문마감 − 5분)과 실제 동결 시각을 **따로** 계산한다 — 같은 이름으로 섞지 않는다(01).
    freeze_deadline = _freeze_deadline(order)
    if _ts(frozen_at) is not None:
        frozen_source = "argument"
    elif _ts(record.get("frozen_at")) is not None:
        frozen_source = "record"
    elif freeze_deadline is not None:
        frozen_source = "rule"
    elif _ts(report.get("frozen_at")) is not None:
        frozen_source = "report"
    else:
        frozen_source = None
    frozen = (_ts(frozen_at) or _ts(record.get("frozen_at"))
              or freeze_deadline or _ts(report.get("frozen_at")))
    posthoc = False
    if received is not None and validated is not None and validated > received:
        # 보고서가 원장 수신 시각보다 늦은 검증 시각을 적었다 — 값을 `now` 로 깎는다.
        #   깎은 값이 동결·마감을 넘으면 아래 시각 검사가 정상적으로 `late` 로 떨어뜨린다
        #   (그래서 이 사실은 **막는 사유가 아니라 기록**이다 — now 가 마감 전이면 편입은 정당하다).
        validated = received
        posthoc = True
        notes.append("POSTHOC_VALIDATION")
    if completed is not None and received is not None and completed > received:
        # [01] 배치가 읽은 시각보다 **나중에 완료**된 보고서는 그 자리에 있을 수 없다.
        #   `now` 상한 보정이 미래 완료를 적격으로 구제하지 못하게 **무조건 부적격**으로 막는다.
        invalid = True
        reasons.append("COMPLETED_AFTER_NOW")
    if strict:
        missing = [name for name, value in (("completed_at", completed),
                                            ("report_validated_at", validated),
                                            ("order_cutoff_at", order)) if value is None]
        if missing:
            invalid = True
            reasons.append("MISSING_TIMESTAMPS")
            reasons.extend(f"MISSING_{name.upper()}" for name in missing)
    elif completed is None:
        reasons.append("COMPLETED_AT_MISSING")
    receipt = report_receipt(report)
    if receipt is not None:
        if receipt != compute_report_sha256(report):
            invalid = True
            reasons.append("REPORT_HASH_MISMATCH")
    elif require_receipt:
        invalid = True
        reasons.append("REPORT_RECEIPT_MISSING")
    if observed is not None and completed is not None and completed < observed:
        invalid = True
        reasons.append("COMPLETED_BEFORE_SIGNAL_OBSERVED")
        if published is not None and completed < published:
            reasons.append("COMPLETED_BEFORE_PUBLISHED")
    if completed is not None and order is not None and completed >= order:
        late = True
        reasons.append("COMPLETED_AFTER_ORDER_CUTOFF")
    if not posthoc and validated is not None and completed is not None and validated < completed:
        # 사후 판정으로 `now` 로 깎은 값은 "검증이 완료보다 먼저"의 증거가 아니다(깎기의 부산물).
        invalid = True
        reasons.append("VALIDATED_BEFORE_COMPLETED")
    if validated is not None and order is not None and validated >= order:
        late = True
        reasons.append("REPORT_VALIDATED_AFTER_ORDER_CUTOFF")
    if validated is not None and frozen is not None and validated > frozen:
        late = True
        reasons.append("VALIDATED_AFTER_FREEZE")
    if completed is None and validated is not None and order is not None and validated >= order:
        reasons.append("COMPLETED_AT_MISSING_AND_LATE")
    fill_at = _ts((execution or {}).get("at")) if execution else None
    if fill_at is not None and validated is not None and fill_at < validated:
        invalid = True
        reasons.append("ATTACHED_AFTER_FILL")
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
    return {"eligible": not reasons, "invalid": invalid, "late": late, "reasons": reasons,
            "notes": notes,
            "signal_observed_at": None if observed is None else _iso(observed),
            "completed_at": None if completed is None else _iso(completed),
            "report_validated_at": None if validated is None else _iso(validated),
            # 검증 시각의 **출처**와 보고서가 적은 원값(깎기 전) — 신뢰 경계를 화면·로그에서 구분한다.
            "validation_claimed_at": None if claimed is None else _iso(claimed),
            "validation_source": validation_source,
            "received_at": None if received is None else _iso(received),
            "frozen_at": None if frozen is None else _iso(frozen),
            "freeze_deadline_at": None if freeze_deadline is None else _iso(freeze_deadline),
            "frozen_source": frozen_source,
            "order_cutoff_at": None if order is None else _iso(order),
            "posthoc_validation": bool(posthoc),
            "receipt_required": bool(require_receipt),
            "receipt_ok": None if receipt is None else receipt == compute_report_sha256(report),
            "report_sha256": None if receipt is None else compute_report_sha256(report)}


def is_eligible_for_signal(report: dict, record: dict, **kw) -> bool:
    """편입 4조건 판정의 불리언 이름(3.11.8) — 상세 사유는 `eligibility()` 를 본다."""
    return bool(eligibility(report, record, **kw)["eligible"])


def debate_event(report: dict, record: dict, *, at: datetime | None = None,
                 report_validated_at=None, frozen_at=None, execution: dict | None = None,
                 params: dict | None = None, regime: str | None = None) -> dict:
    """편입 이벤트 — **편입 프로세스가 스스로 판정**해 만든다(v1.3.4 · P1-2).

    예전 `record.debate_event()` 는 호출자가 `eligible`·`late` 를 넣어 줘야 했고, 그 값이 틀리면
    그대로 기록됐다. 이 함수는 `eligibility()` 로 시각 계약을 다시 계산하고, 통과한 경우에만
    `debate_attached` 이벤트를 만든다(통과하지 않으면 `event="debate_rejected"` + 사유).

    Returns: `{"signal_id", "snapshot_id", "events": [ {...} ]}` — `append_event()` 로 append 한다.
    """
    stamp = at or datetime.now(timezone.utc)
    el = eligibility(report, record, now=stamp, report_validated_at=report_validated_at,
                     frozen_at=frozen_at, execution=execution)
    actions = actions_for(report, record, execution,
                          auto_downgrade_enabled=bool(((params or {}).get("debate") or {})
                                                      .get("auto_downgrade_enabled", False)))
    payload = _event_payload(report, record, el, actions, regime=regime, at=stamp)
    if not el["eligible"]:
        payload["events"][0].update({"event": "debate_rejected", "reasons": list(el["reasons"])})
    return payload


def debate_step_times(record: dict, report: dict | None = None, *,
                      now: datetime | None = None) -> dict:
    """편입 단계별 시각을 한눈에 보기(화면·로그용) — 동결 전/후를 화면이 같은 기준으로 보여 준다."""
    order = _ts(record.get("order_cutoff_at"))
    return {
        "signal_observed_at": _ts(record.get("signal_observed_at")) or _ts(record.get("signal_published_at")),
        "completed_at": _ts((report or {}).get("completed_at")),
        "report_validated_at": (_ts((report or {}).get("report_validated_at"))
                                or _ts(((report or {}).get("validation") or {}).get("report_validated_at"))
                                or _ts(record.get("report_validated_at")) or _ts(now)),
        "frozen_at": _ts(record.get("frozen_at")) or _freeze_deadline(order),
        # 규칙 마감과 실제 동결 시각을 화면도 같은 뜻으로 쓴다(01).
        "freeze_deadline_at": _freeze_deadline(order),
        "order_cutoff_at": order,
    }


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
    """화면 배지 — `토론 완료`/`토론 없음`/`정족수 미달`/`지연 도착`/`산식과 다름(보류 권고)`.

    무효 사유는 **사유에 맞는 문구**를 쓴다(감사 02 부수 ②): `ATTACHED_AFTER_FILL`(검증이 체결 뒤)을
    "발행 전 완료"라고 적으면 화면이 거짓말을 한다 — 그 둘을 갈라 표기한다.
    """
    if not report:
        return BADGE_NONE
    if str(report.get("status") or "") != "complete":
        return BADGE_QUORUM
    el = el or {}
    if el.get("invalid"):
        if "ATTACHED_AFTER_FILL" in [str(r) for r in (el.get("reasons") or [])]:
            return BADGE_INVALID_AFTER_FILL
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
        # 편입 시각 계약 — 규칙 마감(`freeze_deadline_at`)과 실제 값(`frozen_at`)을 섞지 않는다(01).
        "report_validated_at": el.get("report_validated_at") or report.get("report_validated_at"),
        "freeze_deadline_at": el.get("freeze_deadline_at"),
        "frozen_at": el.get("frozen_at"),
        "received_at": el.get("received_at"),
        "validation_source": el.get("validation_source"),
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

    [02] 채택 결과는 신호 단위로 **한 번** 확정된다 — `adopted` 블록(`report_id`·`report_sha256`·
    `snapshot_id`·검증 시각)을 문서에 남기고, 뒤에 온 보고서는 `superseded` 목록에만 쌓는다.
    이미 채택된 보고서가 있으면 **덮어쓰지 않는다**(append-only) — 화면·감사가 "어느 보고서가
    그 결정에 붙었나"를 파일 하나로 확인할 수 있게 한다.
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
                for key in ("adopted", "superseded"):
                    if old.get(key):
                        doc[key] = old[key]
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
    changed = added > 0
    rows = payload.get("superseded") or []
    if rows:
        known = doc.setdefault("superseded", [])
        for row in rows:
            if any(old.get("report_id") == row.get("report_id") for old in known):
                continue
            known.append(row)
            changed = True
    adopted = payload.get("adopted")
    if adopted and not doc.get("adopted"):
        doc["adopted"] = adopted
        changed = True
    if not changed:
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
    """편입 이벤트 1건 — `record.debate_event` 와 같은 골격에 A/B 판정 필드를 더한다.

    [02] 신호 단위로 **채택된 보고서의 정체성**(`report_id`·`report_sha256`·`snapshot_id`)을
    이벤트와 문서(`adopted` 블록) 양쪽에 남긴다 — 화면이 마지막 회차를 "채택"으로 오해하지 않게.
    """
    report_sha = el.get("report_sha256")
    snapshot = report.get("snapshot_id") or record.get("snapshot_id")
    event = {
        "event": "debate_attached",
        "report_id": report.get("report_id"),
        "report_sha256": report_sha,
        "snapshot_id": snapshot,
        "adoption": "adopted",
        "track": "codex_debated",
        "intent": (report.get("intent") if report.get("intent") in INTENTS
                   else ("pending_hold_cash" if actions["divergent"]
                         else record.get("quant_intent") or "no_action")),
        "completed_at": report.get("completed_at"),
        "recorded_at": _iso(at),
        "signal_observed_at": el.get("signal_observed_at"),
        "report_validated_at": el.get("report_validated_at"),
        "validation_claimed_at": el.get("validation_claimed_at"),
        "validation_source": el.get("validation_source"),
        "received_at": el.get("received_at"),
        "frozen_at": el.get("frozen_at"),
        "freeze_deadline_at": el.get("freeze_deadline_at"),
        "frozen_source": el.get("frozen_source"),
        "order_cutoff_at": el.get("order_cutoff_at"),
        "eligible": bool(el.get("eligible")),
        "late": bool(el.get("late")),
        "invalid": bool(el.get("invalid")),
        "reasons": list(el.get("reasons") or []),
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
    adopted = {"report_id": report.get("report_id"), "report_sha256": report_sha,
               "snapshot_id": snapshot, "signal_id": str(record.get("signal_id")),
               "report_validated_at": el.get("report_validated_at"),
               "freeze_deadline_at": el.get("freeze_deadline_at"),
               "received_at": el.get("received_at"), "adopted_at": _iso(at)}
    return {"signal_id": str(record.get("signal_id")), "snapshot_id": record.get("snapshot_id"),
            "adopted": adopted, "events": [event]}


def _adoption_prior(doc: dict | None) -> str:
    """이전 배치가 이 신호에 채택한 `report_id` — `adopted` 블록 > 마지막 편입 이벤트."""
    doc = doc or {}
    adopted = doc.get("adopted") or {}
    if adopted.get("report_id"):
        return str(adopted["report_id"])
    for event in reversed(list(doc.get("events") or [])):
        if event.get("event") == "debate_attached" and event.get("report_id"):
            return str(event["report_id"])
    return ""


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _adoption_key(item: tuple[dict, dict, dict]) -> tuple:
    """채택 순위 — **가장 먼저 적격으로 확정된** 보고서(검증 시각, 동률이면 `report_id` 선착순)."""
    report, el, _actions = item
    return (_ts(el.get("report_validated_at")) or _FAR_FUTURE, str(report.get("report_id") or ""))


def _adopted_from_prior(doc: dict | None, record: dict, sid: str) -> dict | None:
    """이번 문서에 채택 보고서가 없을 때 — **이전 이벤트**로 화면 요약을 복원한다(채택은 되돌리지 않는다)."""
    prior_id = _adoption_prior(doc)
    if not prior_id:
        return None
    events = [e for e in ((doc or {}).get("events") or []) if str(e.get("report_id")) == prior_id]
    if not events:
        return None
    event = events[-1]
    return {"signal_id": sid, "ticker": record.get("ticker"), "report_id": prior_id,
            "snapshot_id": event.get("snapshot_id") or record.get("snapshot_id"),
            "status": "complete",
            "badge": debate_badge({"status": "complete"}, event, event),
            "eligible": bool(event.get("eligible")), "late": bool(event.get("late")),
            "invalid": bool(event.get("invalid")), "reasons": list(event.get("reasons") or []),
            "completed_at": event.get("completed_at"),
            "report_validated_at": event.get("report_validated_at"),
            "freeze_deadline_at": event.get("freeze_deadline_at"),
            "frozen_at": event.get("frozen_at"),
            "quant_action": event.get("quant_action"), "debate_action": event.get("debate_action"),
            "divergent": bool(event.get("divergent")),
            "auto_downgrade_applied": bool(event.get("auto_downgrade_applied")),
            "quant_grade": event.get("quant_grade"),
            "recommendation": event.get("recommendation"),
            "display_grade": event.get("display_grade"),
            "adoption": "adopted", "adopted_report_id": prior_id,
            "adopted_report_sha256": event.get("report_sha256"),
            "adopted_snapshot_id": event.get("snapshot_id"),
            "recovered_from_event": True}


def reconcile_debates(reports_doc: dict | None, records: list[dict],
                      executions: dict[str, dict] | None = None, *,
                      event_dir: str | Path = EVENTS_DIR, params: dict | None = None,
                      regime: str | None = None, as_of: date | None = None,
                      write: bool = True, now: datetime | None = None,
                      report_validated_at=None, require_receipt: bool = True,
                      strict: bool = True) -> dict:
    """리포트 → 편입 자격 판정 → `debate_events` append. 토론 0건이면 조용히 0건이다.

    `by_signal` 은 자격 미달 건도 담는다 — 화면이 `지연 도착`·`정족수 미달` 배지를
    보여 줘야 하기 때문이다(다만 그 결정의 트랙에는 붙지 않는다).

    [v1.3.5 · 치명 02 · §10]
    - `now` 는 **이번 배치가 원장을 읽어 판정한 수신 시각**이다(실행기는 보고서 파일을 읽은 시각을
      넘긴다). 보고서가 그보다 늦은 검증 시각을 적으면 믿지 않고 깎는다 — 예전에는 `now` 자리에
      보고서 자신의 검증 시각을 넣어 **`now` 보호를 사실상 끄고** 있었다.
    - `strict=True`(기본) — `completed_at`·`report_validated_at`·`order_cutoff_at` 이 빠진 보고서는
      여섯 개 시각 검사를 건너뛰지 못하고 `MISSING_TIMESTAMPS` 로 받는다.
    - `require_receipt=True`(기본) — `report_sha256` 영수증이 없는 보고서는 편입하지 않는다
      (`REPORT_RECEIPT_MISSING`). 영수증이 있으면 바이트가 그대로인지 대조한다(`REPORT_HASH_MISMATCH`).
      기존 단위 호출 호환은 `eligibility(..., strict=False)` 로 남겨 둔다.

    [v1.3.6 · 감사 5331ab7-02] **신호당 채택은 하나다.**
    - 같은 `signal_id` 에 `-01`·`-02` 가 오면 **가장 먼저 적격으로 확정된** 보고서를 채택하고,
      뒤에 온 보고서는 적격이어도 `superseded`, 지연·무효면 `rejected` 로만 남긴다.
      `by_signal[sid]` 는 **채택 결과를 덮어쓰지 않는다**(예전에는 배열 마지막 보고서가 무조건 덮었다).
    - 이미 채택된 보고서(이전 배치의 `adopted` 블록)는 재실행에서도 그대로 채택으로 돌아온다.
    - 채택 정체성(`report_id`·`report_sha256`·`snapshot_id`)은 이벤트와 `debate_events` 문서
      (`adopted` 블록)·`by_signal`(`adopted_*`)에 함께 남는다.
    """
    executions = executions or {}
    auto = bool(((params or {}).get("debate") or {}).get("auto_downgrade_enabled", False))
    reports = reports_of(reports_doc)
    index = {str(r.get("signal_id")): r for r in records or []}
    at = now or datetime.now(timezone.utc)
    prior_docs = load_events(event_dir) if write else {}
    attached: list[dict] = []
    rejected: list[dict] = []
    superseded: list[dict] = []
    by_signal: dict[str, dict] = {}
    adopted_map: dict[str, str] = {}
    n_late = n_invalid = 0
    groups: dict[str, list[dict]] = {}
    for report in reports:
        groups.setdefault(str(report.get("signal_id") or ""), []).append(report)
    for sid, rows in groups.items():
        record = index.get(sid)
        if record is None:
            for report in rows:
                rejected.append({"signal_id": sid, "report_id": report.get("report_id"),
                                 "reason": "RECORD_MISSING"})
            by_signal[sid] = {"signal_id": sid, "ticker": rows[-1].get("ticker"),
                              "status": rows[-1].get("status"),
                              "badge": debate_badge(rows[-1], {"eligible": False, "late": False,
                                                              "invalid": False}, None),
                              "eligible": False, "report_id": rows[-1].get("report_id")}
            continue
        evaluated: list[tuple[dict, dict, dict]] = []
        for report in rows:
            el = eligibility(report, record, now=at, report_validated_at=report_validated_at,
                             execution=executions.get(sid), require_receipt=require_receipt,
                             strict=strict)
            actions = actions_for(report, record, executions.get(sid),
                                  auto_downgrade_enabled=auto)
            evaluated.append((report, el, actions))
            if el.get("late"):
                n_late += 1
            if el.get("invalid"):
                n_invalid += 1
        prior_id = _adoption_prior(prior_docs.get(sid))
        fixed = bool(prior_id)
        adopted = next((item for item in evaluated
                        if fixed and str(item[0].get("report_id")) == prior_id), None)
        if adopted is None and not fixed:
            eligible = [item for item in evaluated if item[1]["eligible"]]
            adopted = min(eligible, key=_adoption_key) if eligible else None
        for report, el, _actions in evaluated:
            if adopted is not None and report is adopted[0]:
                continue
            if el["eligible"]:
                # 적격인데 채택되지 못한 회차 — 채택은 이미 확정됐다(뒤 회차·이전 배치 채택).
                superseded.append({"signal_id": sid, "report_id": report.get("report_id"),
                                   "reason": "ADOPTION_FIXED" if adopted is None else "SUPERSEDED_BY_ADOPTED",
                                   "adopted_report_id": (adopted[0].get("report_id")
                                                         if adopted is not None else prior_id)})
                continue
            rejected.append({"signal_id": sid, "report_id": report.get("report_id"),
                             "reasons": el["reasons"],
                             "late": bool(el.get("late")), "invalid": bool(el.get("invalid")),
                             "report_validated_at": el.get("report_validated_at"),
                             "freeze_deadline_at": el.get("freeze_deadline_at")})
        if adopted is not None:
            report, el, actions = adopted
            summary = public_report(report, el, actions)
            summary.update({"adoption": "adopted", "adopted_report_id": report.get("report_id"),
                            "adopted_report_sha256": el.get("report_sha256"),
                            "adopted_snapshot_id": report.get("snapshot_id")})
            by_signal[sid] = summary
            adopted_map[sid] = str(report.get("report_id"))
            payload = _event_payload(report, record, el, actions, regime=regime, at=at)
            payload["superseded"] = [row for row in superseded if row["signal_id"] == sid]
            if write:
                append_event(event_dir, payload)
            attached.append({"signal_id": sid, "ticker": record.get("ticker"),
                             "report_id": report.get("report_id"),
                             "report_sha256": el.get("report_sha256"),
                             "snapshot_id": report.get("snapshot_id"),
                             "report_validated_at": el.get("report_validated_at"),
                             "divergent": actions["divergent"],
                             "quant_action": actions["quant_action"],
                             "debate_action": actions["debate_action"]})
            continue
        restored = _adopted_from_prior(prior_docs.get(sid), record, sid)
        if restored is not None:
            # 이전 배치가 채택한 보고서가 이번 문서에 없다 — 채택은 유지하고 새 보고서는 superseded 다.
            by_signal[sid] = restored
            adopted_map[sid] = str(restored["report_id"])
            continue
        if evaluated:
            report, el, actions = evaluated[-1]
            by_signal[sid] = public_report(report, el, actions)
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat() if as_of else None,
        "generated_at": _iso(at),
        "received_at": _iso(at),
        "freeze_buffer_seconds": int(FREEZE_BUFFER.total_seconds()),
        "auto_downgrade_enabled": auto,
        "n_reports": len(reports),
        "n_eligible": len(attached),
        "n_late": n_late,
        "n_invalid": n_invalid,
        "n_superseded": len(superseded),
        "n_rejected": len(rejected),
        "attached": attached,
        "rejected": rejected,
        "superseded": superseded,
        "adopted": adopted_map,
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


def quarter_window(quarter: str | None) -> tuple[date, date] | None:
    """`2026Q2` → `(2026-04-01, 2026-06-30)` — 분기 계좌 시뮬레이션의 창(05)."""
    idx = quarter_index(quarter)
    if idx is None:
        return None
    year, q = idx
    start = date(year, 3 * (q - 1) + 1, 1)
    if q == 4:
        end = date(year, 12, 31)
    else:
        end = date(year, 3 * q + 1, 1) - timedelta(days=1)
    return start, end


def _entry_day_of(record: dict, event: dict | None = None) -> date | None:
    """레코드의 **진입 세션** — 체결 시각이 있으면 그날, 없으면 대상 세션."""
    return (_as_date(str((event or {}).get("at") or "")[:10])
            or _as_date(record.get("target_session")))


def _verdict(pairs: int, n_eff: int, net_after_cost: float | None, defense_improved: bool) -> str:
    """§4.8.2 해석 규칙 — 결과를 보고 기준을 고치지 않는다(사전에 못 박은 네 갈래)."""
    if pairs < MIN_DIVERGENT_PAIRS or n_eff < MIN_N_EFF or net_after_cost is None:
        return VERDICT_INSUFFICIENT
    if net_after_cost <= -NET_THRESHOLD:
        return VERDICT_HARMFUL
    if net_after_cost >= NET_THRESHOLD and defense_improved:
        return VERDICT_EFFECTIVE
    return VERDICT_NEUTRAL


def _quarter_policy_versions(pairs: list[dict], index: dict[str, dict]) -> list[str]:
    """그 분기 페어들이 쓰인 정책 버전(`params_version`) — 빈 값은 뺀다."""
    versions = set()
    for pair in pairs or []:
        record = index.get(str(pair.get("signal_id"))) or {}
        version = str(record.get("params_version") or "").strip()
        if version:
            versions.add(version)
    return sorted(versions)


def _maturity_verified_at(pairs: list[dict], horizon_td: int) -> str | None:
    """그 분기의 **마지막 만기 봉 세션** — 만기 미확인 페어가 하나라도 있으면 `None`.

    만기 확인은 "63거래일 뒤 봉을 실제로 관측했다"는 뜻이다. 그래서 전 페어가 `complete` 일 때만
    시각을 남긴다(불완전한 표본에 확인 도장을 찍지 않는다).
    """
    if not pairs or any(not pair.get("complete") for pair in pairs):
        return None
    days = []
    for pair in pairs:
        day = _as_date(pair.get("entry_day"))
        if day is None:
            return None
        days.append(add_trading_days(day, horizon_td))
    return f"{max(days).isoformat()}T00:00:00Z"


def quarter_index(quarter: str) -> tuple[int, int] | None:
    """`2026Q3` → `(2026, 3)`."""
    text = str(quarter or "").strip().upper()
    if "Q" not in text:
        return None
    year, _, q = text.partition("Q")
    try:
        return int(year), int(q)
    except ValueError:
        return None


def consecutive_quarter_count(quarters: list[str]) -> int:
    """분기 목록의 **인접 연속** 길이 — 달력으로 바로 이어져야 한다(Q2 공백 불허).

    `2026Q1 · 2026Q3` 는 2개가 아니라 **1개**다(감사 재현 06-(a)). 정렬 뒤 마지막 분기부터 거슬러
    올라가며 정확히 한 분기씩 앞으로 가는지를 본다.
    """
    parsed = [(quarter_index(q), q) for q in quarters or []]
    parsed = [(idx, q) for idx, q in parsed if idx is not None]
    if len(parsed) != len(quarters or []):
        return 0
    parsed.sort()
    longest = 0
    run = 0
    prev: tuple[int, int] | None = None
    for idx, _q in parsed:
        year, quarter = idx
        if prev is not None:
            py, pq = prev
            step = (year - py) * 4 + (quarter - pq)
            run = run + 1 if step == 1 else 1
        else:
            run = 1
        longest = max(longest, run)
        prev = idx
    return longest


def _qualifies_for_downgrade(row: dict) -> bool:
    """자동 하향 전환 조건 중 분기 단위로 볼 수 있는 것 — 표본·**계좌 효과**·방어력.

    [v1.3.5 · 중요 06] 구 페어 손익(`net_after_cost`) fallback 을 **없앴다**. 전환은 "두 계좌를 실제로
    운용했을 때의 순자산 차이"(`policy_diff.policy_diff_pp`)로만 판단한다 — 페어 합계는 계좌 성과가
    아니다(감사 재현 06-(d)).
    """
    effect = (row.get("policy_diff") or {}).get("policy_diff_pp")
    if effect is None:
        return False
    value = float(effect) / 100.0
    return bool(value >= NET_THRESHOLD
                and (row.get("drawdown") or {}).get("defense_improved")
                and int(row.get("n_graded") or 0) >= MIN_DIVERGENT_PAIRS
                and int(row.get("n_eff") or 0) >= MIN_N_EFF)


# ────────────────────────────── 두 계좌(A/B) 실제 운용 ──────────────────────────────
#
# [v1.3.4 · P1-3] 예전 A/B 는 **계좌가 아니라 라벨**이었다. `codex_debated` 의 순자산을 계산하지 않고
# 보류한 건의 기회비용을 가상으로 더하고 빼서 `net_pp` 를 만들었고, MDD 는 `[0.0 for _ in graded]`
# 로 채워 방어력 개선이 **자동으로 참**이 되었다(자동 하향 4조건 ④ 무력화). 이제 같은 현금·시세·
# 비용·한도로 두 계좌를 시간순으로 운용해 **순자산 곡선과 MDD 를 실제로 계산**한다.

ACCOUNT_KEYS = ("quant_only", "codex_debated")
ACCOUNT_INITIAL_CASH = 100_000.0
ACCOUNT_COSTS = {"slippage": 10.0, "commission": 2.0, "fx": 5.0}


def _day_bars(panel, as_of: date) -> dict[date, dict]:
    frame = _frames(panel)
    if frame is None:
        return {}
    window = frame.loc[frame.index <= pd.Timestamp(as_of)]
    out: dict[date, dict] = {}
    for idx, row in window.iterrows():
        day = idx.date() if hasattr(idx, "date") else idx
        out[day] = {"open": float(row["raw_open"]), "high": float(row["raw_high"]),
                    "low": float(row["raw_low"]), "close": float(row["raw_close"])}
    return out


def _max_drawdown(points: list[float], initial: float | None = None) -> float:
    """순자산 곡선의 최대 낙폭 — **초기 자산도 고점 후보**다(v1.3.5 · 중요 05).

    예전에는 첫 평가 점을 고점으로 시작해 **첫날 하락이 MDD 에 잡히지 않았다**(감사 재현 05-(3):
    첫날 −0.18% 인데 MDD 0.0). 초기 자산을 곡선 앞에 붙여야 첫날 낙폭이 계산된다.
    """
    series = [float(initial)] if (initial is not None and float(initial) > 0) else []
    series.extend(float(value) for value in points or [])
    peak = None
    worst = 0.0
    for value in series:
        peak = value if peak is None else max(peak, value)
        if peak and peak > 0:
            worst = min(worst, value / peak - 1.0)
    return round(float(worst), 6)


def simulate_policy_account(candidates: list[dict], panels: dict, as_of: date, *,
                            initial_cash: float = ACCOUNT_INITIAL_CASH,
                            costs: dict | None = None, horizon_td: int = CLUSTER_TD,
                            max_position: float = 0.05, max_sector: float = 0.25) -> dict:
    """한 정책의 모의 계좌 — 후보를 **시간순**으로 사고 팔아 순자산 곡선을 만든다.

    [v1.3.5 · 중요 05 + P-4]
    - **진입 슬리피지 중복 제거** — 원장 체결가(`fill_price`)는 이미 10bp 를 포함한다. 그 값이 있으면
      그대로 쓰고, 없을 때만 `raw_open × (1+slip)` 로 계산한다. 예전에는 둘 다 곱해 10bp 를 두 번 냈다.
    - **청산 10bp 는 유지**(P-4) — 매도에는 슬리피지가 붙는다.
    - **시가 비중은 전일 종가 자산으로** — 진입 판단 시점에 알 수 있는 값만 쓴다.
    - **MDD 에 초기 자산 포함**(`_max_drawdown`).

    [v1.3.6 · 감사 5331ab7-05] 하루의 시간 순서를 한 가지로 고정한다.
    ① 시가 갭 손절(전일까지 보유) → ② 시가 진입 → ③ 장중 손절(진입 당일 포함) → ④ 만기 종가 청산.
    예전에는 진입이 먼저라 **갭으로 손절할 포지션을 전일 종가로 계속 평가**해 주문 크기를 부풀렸다.

    또 신규 진입분은 진입일에 `mark` 가 아직 0 이다 — 그대로 합산하면 섹터 캡이 **0원**으로 보여
    25% 캡을 뚫었다(6×5%=30%, 2×20%=36%). 이제 진입가(`entry_px`)로 **즉시 평가해 합산**한다.
    """
    costs = costs or ACCOUNT_COSTS
    slip = float(costs.get("slippage", 0.0)) / 1e4
    fee_bps = (float(costs.get("commission", 0.0)) + float(costs.get("fx", 0.0))) / 1e4
    bars = {t: _day_bars(panels.get(t), as_of) for t in {c.get("ticker") for c in candidates or []}}
    plans = []
    for cand in candidates or []:
        ticker = str(cand.get("ticker") or "")
        entry_day = _as_date(cand.get("entry_day"))
        if not ticker or entry_day is None or entry_day not in bars.get(ticker, {}):
            continue
        raw_open = float(bars[ticker][entry_day]["open"])
        fill = cand.get("fill_price")
        # 이미 슬리피지가 포함된 원장 체결가는 다시 올리지 않는다(P-4 · 감사 05-(1)).
        entry_px = float(fill) if fill not in (None, 0) else raw_open * (1.0 + slip)
        stop = cand.get("stop_price")
        stop_distance = float(cand.get("stop_distance") or 0.0)
        stop_price = float(stop) if stop else entry_px * (1.0 - stop_distance)
        weight = float(cand.get("weight") or 0.0)
        if weight <= 0 or entry_px <= 0:
            continue
        plans.append({"ticker": ticker, "sector": str(cand.get("sector") or ""),
                      "entry_day": entry_day, "entry_px": entry_px, "raw_open": raw_open,
                      "stop": stop_price, "weight": weight, "signal_id": cand.get("signal_id"),
                      # 만기(63거래일)에는 종가로 청산한다 — 페어 채점 구간과 같은 창을 쓴다.
                      "maturity_day": add_trading_days(entry_day, horizon_td),
                      "qty": 0.0, "open": True, "exit_day": None, "exit_px": None})
    plans.sort(key=lambda p: (p["entry_day"], str(p["signal_id"])))
    days = sorted({d for table in bars.values() for d in table})
    if plans:
        days = [d for d in days if d >= min(p["entry_day"] for p in plans)]
    cash = float(initial_cash)
    curve: list[dict] = []
    n_trades = 0
    last_close: dict[str, float] = {}

    def mark(ticker: str) -> float:
        return last_close.get(ticker, 0.0)

    def value_of(plan: dict) -> float:
        """포지션 평가액 — 진입 당일(시세 mark 없음)은 **진입가로 즉시 평가**한다(05 섹터 캡)."""
        if plan["qty"] <= 0:
            return 0.0
        last = mark(plan["ticker"])
        return plan["qty"] * (last if last > 0 else plan["entry_px"])

    def sector_value(sector: str) -> float:
        return sum(value_of(p) for p in plans if p["qty"] > 0 and p["sector"] == sector)

    def close_plan(plan: dict, day: date, price: float) -> None:
        nonlocal cash, n_trades
        plan["open"] = False
        plan["exit_day"] = day
        plan["exit_px"] = price * (1.0 - slip)          # 청산 10bp 는 유지(P-4)
        cash += plan["qty"] * plan["exit_px"] * (1.0 - fee_bps)
        plan["qty"] = 0.0
        n_trades += 1

    for day in days:
        # 1) 시가 갭 손절 — **진입보다 먼저** 처리해 현금을 먼저 확보한다(시간 순서 일관화).
        for plan in plans:
            if plan["qty"] <= 0 or plan["entry_day"] >= day:
                continue
            bar = bars[plan["ticker"]].get(day)
            if bar is not None and bar["open"] <= plan["stop"]:
                close_plan(plan, day, bar["open"])
        # 2) 시가 진입 — 비중은 **전일 종가 기준 순자산**으로 계산한다(P-4).
        equity_open = cash + sum(value_of(p) for p in plans if p["qty"] > 0)
        for plan in plans:
            if plan["entry_day"] != day or not plan["open"] or plan["qty"] > 0:
                continue
            if equity_open <= 0:
                break
            notional = min(plan["weight"], max_position) * equity_open
            # 당일 진입분은 진입가로 이미 섹터 누적에 들어가 있다 — 같은 날 6건이어도 25% 를 넘지 않는다.
            room = max_sector * equity_open - sector_value(plan["sector"])
            if notional > room:
                notional = max(0.0, room)
            if notional <= 0 or notional + notional * fee_bps > cash:
                plan["open"] = False
                continue
            plan["qty"] = notional / plan["entry_px"]
            cash -= notional + notional * fee_bps
            equity_open = cash + sum(value_of(p) for p in plans if p["qty"] > 0)
            n_trades += 1
        # 3) 장중 손절(진입 당일 포함) → 만기 종가 청산
        for plan in plans:
            if not plan["open"] or plan["entry_day"] > day or plan["qty"] <= 0:
                continue
            bar = bars[plan["ticker"]].get(day)
            if bar is None:
                continue
            exit_px = None
            if bar["low"] <= plan["stop"]:
                exit_px = plan["stop"]        # 진입 당일 장중 저가도 포함된다(수량 0 청산 금지)
            if exit_px is None and day >= plan["maturity_day"]:
                exit_px = bar["close"]
            if exit_px is None:
                continue
            close_plan(plan, day, exit_px)
        # 4) 종가 갱신·평가
        for ticker, table in bars.items():
            if day in table:
                last_close[ticker] = float(table[day]["close"])
        holdings = sum(value_of(p) for p in plans if p["qty"] > 0)
        equity = cash + holdings
        curve.append({"date": day.isoformat(), "equity": round(equity, 4),
                      "return": round(equity / initial_cash - 1.0, 6)})
    final = curve[-1]["equity"] if curve else float(initial_cash)
    return {
        "initial_cash": round(float(initial_cash), 4),
        "final_equity": round(final, 4),
        "total_return": round(final / float(initial_cash) - 1.0, 6) if initial_cash else None,
        "mdd": _max_drawdown([p["equity"] for p in curve], initial=initial_cash) if curve else 0.0,
        "n_trades": n_trades,
        "n_candidates": len(plans),
        "equity_curve": curve,
    }


def _account_candidates(by_signal: dict[str, dict], records: list[dict],
                        executions: dict[str, dict], *, policy: str,
                        intent_blocked: dict[str, str] | None = None) -> list[dict]:
    """계좌별 후보 목록 — **공통 주문 의도(동결 주문)** 가 모집단이다(중요 04 · 05).

    [v1.3.5 · 중요 04] 예전에는 적격 토론 보고서(`by_signal`)의 행만 돌며 `pending_buy`(의도)를
    `buy_filled`(체결)로 바꿔 읽었다. 그래서 ① 보고서가 없는 날의 정량 체결이 두 계좌에서 모두
    사라지고 ② 체결되지 않은 주문에 수익률이 붙었다. 기획서 §4.8.1 은 "토론을 돌리지 않은 날에도 정량
    트랙은 계속 채점된다"고 못 박는다.

    [v1.3.6 · 감사 5331ab7-05] 모집단을 **정량 계좌의 `buy_filled` 로 제한하지 않는다.**
    `(종목, 대상 세션)` 별 **동결 주문 의도**(`live.order_slots`)에서 출발해 두 계좌가 **각자의 현금·
    보유·섹터 캡**으로 독립 체결·평가된다. 그래서 A 의 섹터 캡에 막혀 `no_action` 이 된 후보도 B 가
    자기 여유로 평가할 수 있다(예전에는 그런 후보가 두 계좌 모두에서 사라졌다).

    후보로 올리는 것은 **원장이 실제로 판정한 의도** 뿐이다.
    - `buy_filled` — 체결된 동결 주문(체결가·손절가를 원장에서 그대로 받는다).
    - **섹터 캡 차단** — `settle` 이 `no_action` 으로 남긴 주문 중 사유가 섹터 상한인 건. 남은 이유가
      시장·범위(밴드 이탈·차단·잠금)인 주문은 두 계좌 모두 대상이 아니므로 빠진다.

    - `quant_only` — 공통 의도 전부.
    - `codex_debated` — 그 위에 **토론이 보류·하향한 건만** 뺀다. 토론이 없거나 편입되지 않았으면
      (`view.eligible` 거짓) 정량과 **같은 행동**을 한다(자체 현금·섹터 한도로 독립 운용).
    """
    out: list[dict] = []
    blocked = {str(k): str(v) for k, v in (intent_blocked or {}).items() if v}
    by_sid = {str(r.get("signal_id")): r for r in records or []}
    for sid, slot in _frozen_buy_intents(records).items():
        record = by_sid.get(sid)
        if record is None:
            continue
        event = executions.get(sid) or {}
        track = event.get("quant_track") or {}
        filled = (track.get("execution_event") == "buy_filled"
                  and track.get("fill_price") is not None)
        block_reason = blocked.get(sid) or _sector_cap_block(event)
        cap_blocked = (not filled) and block_reason == "SECTOR_CAP"
        if not (filled or cap_blocked):
            continue
        view = by_signal.get(sid) or {}
        if view.get("eligible"):
            action = view.get("debate_action") if policy == "codex_debated" else view.get("quant_action")
            if action not in (None, "buy_filled"):
                continue
        position = record.get("position") or {}
        weight = track.get("weight")
        if weight in (None, 0):
            weight = position.get("tranche_weight") or 0.0
        entry_day = (_as_date(str(event.get("at") or "")[:10])
                     or _as_date(record.get("target_session"))
                     or _as_date(str(slot.get("target_session") or "")[:10]))
        out.append({"signal_id": sid, "ticker": record.get("ticker") or view.get("ticker"),
                    "sector": record.get("sector") or "", "entry_day": entry_day,
                    "weight": weight,
                    # 캡에 막힌 의도는 체결가가 없다 — 모의 계좌가 시가 + 슬리피지로 직접 산다.
                    "fill_price": track.get("fill_price") if filled else None,
                    "stop_price": track.get("stop_price"),
                    "stop_distance": position.get("stop_distance"),
                    "intent_blocked": "SECTOR_CAP" if cap_blocked else None})
    out.sort(key=lambda c: (str(c.get("entry_day")), str(c.get("signal_id"))))
    return out


def _sector_cap_block(event: dict | None) -> str | None:
    """`settle` 이 남긴 구조화 사유는 없으므로 `no_action` 사유 문구을 보조 증거로 읽는다.

    실행기는 `settlement["settled"]` 의 `reason` 을 `intent_blocked` 로 직접 넘길 수 있다(그쪽이 우선).
    """
    if str(((event or {}).get("quant_track") or {}).get("execution_event") or "") != "no_action":
        return None
    note = str((event or {}).get("note") or "")
    if "섹터" in note and "상한" in note and "4.12" in note:
        return "SECTOR_CAP"
    return None


def _frozen_buy_intents(records: list[dict]) -> dict[str, dict]:
    """`(종목, 대상 세션)` 동결 주문 중 **`pending_buy` 인 것** → `{signal_id: slot}`.

    회차·동결 규칙은 `live.order_slots` 하나만 쓴다 — 여기서 다시 구현하면 두 곳이 갈라진다.
    동결 뒤 도착해 `held` 된 회차는 슬롯이 아니므로 후보로 올라오지 않는다.
    """
    try:
        from engine.ledger import live as live_mod
        slots = live_mod.order_slots(records or [])
    except Exception:                       # noqa: BLE001 — 슬롯을 못 만들면 후보 없음(배치를 죽이지 않는다)
        return {}
    frozen: dict[str, dict] = {}
    for slot in slots.values():
        if str(slot.get("intent") or "") != "pending_buy":
            continue
        sid = str(slot.get("signal_id") or "")
        if sid:
            frozen[sid] = slot
    return frozen


def policy_accounts(by_signal: dict[str, dict], records: list[dict],
                    executions: dict[str, dict], panels: dict, as_of: date, *,
                    initial_cash: float = ACCOUNT_INITIAL_CASH,
                    costs: dict | None = None, horizon_td: int = CLUSTER_TD,
                    params: dict | None = None,
                    intent_blocked: dict[str, str] | None = None) -> dict:
    """`quant_only`·`codex_debated` 두 계좌를 **같은 규칙**으로 돌려 순자산·MDD 를 낸다."""
    risk = (params or {}).get("risk") or {}
    max_position = float(risk.get("max_position") or 0.05)
    max_sector = float(risk.get("max_sector") or 0.25)
    accounts: dict[str, dict] = {}
    for key in ACCOUNT_KEYS:
        candidates = _account_candidates(by_signal, records, executions, policy=key,
                                         intent_blocked=intent_blocked)
        accounts[key] = simulate_policy_account(candidates, panels, as_of, initial_cash=initial_cash,
                                                costs=costs, horizon_td=horizon_td,
                                                max_position=max_position, max_sector=max_sector)
    quant = accounts["quant_only"]
    debated = accounts["codex_debated"]
    r_quant = quant.get("total_return")
    r_debated = debated.get("total_return")
    diff_pp = None if (r_quant is None or r_debated is None) else round(100.0 * (r_debated - r_quant), 4)
    defense = (abs(float(debated.get("mdd") or 0.0)) < abs(float(quant.get("mdd") or 0.0)))
    return {
        "accounts": accounts,
        "initial_cash": round(float(initial_cash), 4),
        "costs": dict(costs or ACCOUNT_COSTS),
        "R_quant": r_quant,
        "R_debated": r_debated,
        "policy_diff_pp": diff_pp,
        "quant_mdd": quant.get("mdd"),
        "debate_mdd": debated.get("mdd"),
        "defense_improved": bool(defense),
    }


def _bucket(pairs: list[dict], accounts: dict | None = None) -> dict:
    """페어 묶음 → 회피 손실·놓친 수익·순효과·정확도·방어력 (4.8.2).

    [v1.3.4 · P1-3] `debate_mdd` 를 `0.0` 으로 채우지 않는다 — `accounts`(계좌 곡선)가 없으면
    방어력은 `None`(판정 불가)이고, 있으면 **두 계좌 MDD 실측치**로 비교한다.
    """
    graded = [p for p in pairs if p.get("complete")]
    weights = [float(p.get("weight") or 0.0) for p in graded]
    avoided = sum(max(0.0, -float(p["return"])) * float(p.get("weight") or 0.0) for p in graded)
    missed = sum(max(0.0, float(p["return"])) * float(p.get("weight") or 0.0) for p in graded)
    net = avoided - missed
    lost = [p for p in graded if float(p["return"]) < 0]
    mdd_quant = [float(p.get("mdd") or 0.0) for p in graded]
    tail_quant = _quantile(mdd_quant, 0.05)
    quant_mdd = None if accounts is None else (accounts.get("accounts") or {}).get("quant_only", {}).get("mdd")
    debate_mdd = None if accounts is None else (accounts.get("accounts") or {}).get("codex_debated", {}).get("mdd")
    if accounts is not None and quant_mdd is not None and debate_mdd is not None:
        defense = bool(abs(float(debate_mdd)) < abs(float(quant_mdd)))
    else:
        defense = None                     # 계산하지 않은 것을 "개선됨"으로 두지 않는다
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
                     "quant_mdd": quant_mdd, "debate_mdd": debate_mdd,
                     "debate_mean_mdd": debate_mdd, "debate_tail_5pct": debate_mdd,
                     "defense_improved": defense},
    }


def compute_debate_comparison(by_signal: dict[str, dict], records: list[dict],
                              executions: dict[str, dict] | None = None, panels: dict | None = None,
                              as_of: date | None = None, *, params: dict | None = None,
                              horizon_td: int = CLUSTER_TD,
                              regime_lookup: dict[str, str] | None = None,
                              now: datetime | None = None,
                              initial_cash: float = ACCOUNT_INITIAL_CASH,
                              costs: dict | None = None,
                              intent_blocked: dict[str, str] | None = None) -> dict:
    """`debate_comparison.json` — **두 계좌의 순자산 차이**와 **페어 손익**을 따로 낸다(4.8 · P1-3).

    - `policy_diff_pp = 100 × (R_debated − R_quant)` — 같은 초기 현금·시세·비용 규칙으로 운용한
      두 계좌의 비용 후 순자산 수익률 차이다(정책 효과).
    - `pair_summary` — 행동이 갈린 페어의 회피 손실·놓친 수익·순손익(원인 분석). 예전에는 이
      페어 합계를 "순효과"라는 한 숫자로 계좌 성과 자리에 올렸다.
    - 하위 호퐌 키(`avoided_loss`·`missed_gain`·`net`·`net_after_cost`·`net_pp`·`net_after_cost_pp`)는
      **페어 기준 값**으로 유지한다(화면·기존 코드 호환).

    [v1.3.6 · 감사 5331ab7-05] 분기 행의 **계좌 시뮬레이션 창을 그 분기로 고정**한다 — 그 분기 `eval_day`
    상한(`min(as_of, 분기말)`)과 그 분기에 진입한 레코드만 넘긴다. 예전에는 전체 기간의 레코드를
    넘겨 **Q1 점수가 4월(Q2) 체결에 좌우됐다**(감사 재현 05-C).
    """
    executions = executions or {}
    panels = panels or {}
    regime_lookup = regime_lookup or {}
    auto = bool(((params or {}).get("debate") or {}).get("auto_downgrade_enabled", False))
    index = {str(r.get("signal_id")): r for r in records or []}
    eval_day = as_of or date.today()
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
        outcome = pair_outcome(panels.get(ticker), entry_day, eval_day, horizon_td)
        regime = (regime_lookup.get(sid) or event.get("regime") or record.get("regime") or "unknown")
        pairs.append({
            "signal_id": sid, "ticker": ticker, "entry_day": entry_day.isoformat() if entry_day else None,
            "weight": round(float(weight or 0.0), 6), "regime": str(regime),
            "sector": record.get("sector") or "",
            "quant_action": view.get("quant_action"), "debate_action": view.get("debate_action"),
            "quant_grade": view.get("quant_grade"), "recommendation": view.get("recommendation"),
            **outcome,
        })
    account_filters = {"initial_cash": initial_cash, "costs": costs, "horizon_td": horizon_td,
                       "params": params, "intent_blocked": intent_blocked}
    accounts = policy_accounts(by_signal, records, executions, panels, eval_day, **account_filters)
    overall = _bucket(pairs, accounts)
    by_regime: dict[str, dict] = {}
    for name in ("risk_on", "neutral", "risk_off", "unknown"):
        bucket = [p for p in pairs if p.get("regime") == name]
        if bucket:
            sids = {p["signal_id"] for p in bucket}
            by_regime[name] = _bucket(bucket, policy_accounts(
                {k: v for k, v in (by_signal or {}).items() if k in sids},
                records, executions, panels, eval_day, **account_filters))
    at = now or datetime.now(timezone.utc)
    quarters: dict[str, list[dict]] = {}
    for pair in pairs:
        key = quarter_of(_as_date(pair.get("entry_day")))
        if key:
            quarters.setdefault(key, []).append(pair)
    index = {str(r.get("signal_id")): r for r in records or []}
    quarter_rows = []
    for key in sorted(quarters):
        sids = {p["signal_id"] for p in quarters[key]}
        subset = {k: v for k, v in (by_signal or {}).items() if k in sids}
        # [05] 그 분기 창으로 시뮬레이션을 **고정**한다 — 뒤 분기 체결이 앞 분기 점수를 오염시키지 않는다.
        window = quarter_window(key)
        quarter_records = list(records or [])
        quarter_eval = eval_day
        if window is not None:
            start, end = window
            quarter_eval = min(eval_day, end)
            quarter_records = [
                r for r in records or []
                if (day := _entry_day_of(r, executions.get(str(r.get("signal_id"))))) is not None
                and start <= day <= end]
        quarter_accounts = policy_accounts(subset, quarter_records, executions, panels, quarter_eval,
                                           **account_filters)
        row = _bucket(quarters[key], quarter_accounts)
        row["quarter"] = key
        row["policy_diff"] = {"policy_diff_pp": _policy_diff_pp(quarter_accounts)}
        row["eval_day"] = quarter_eval.isoformat()
        if window is not None:
            row["window"] = {"start": window[0].isoformat(), "end": window[1].isoformat(),
                             "capped_at": quarter_eval.isoformat(),
                             "n_records": len(quarter_records)}
        row["verdict"] = _verdict(row["n_graded"], row["n_eff"],
                                  (row["policy_diff"]["policy_diff_pp"] or 0.0) / 100.0,
                                  bool(row["drawdown"]["defense_improved"]))
        # [v1.3.5 · 중요 06] 분기 행은 **스스로 판정 근거를 밝힌다** — 정책 버전·평가 시각·만기 확인 시각.
        #   예전에는 어느 정책으로 낸 숫자인지 행에 남지 않아 `evaluate_auto_downgrade_eligibility`
        #   가 "같은 정책 버전"을 확인할 수 없었고, 버전 집합이 비어도 참이 됐다(감사 재현 06-(b)(c)).
        row["policy_versions"] = _quarter_policy_versions(quarters[key], index)
        row["policy_version"] = (row["policy_versions"][0] if len(row["policy_versions"]) == 1
                                 else None)
        row["evaluated_at"] = _iso(at)
        row["maturity_verified_at"] = _maturity_verified_at(quarters[key], horizon_td)
        quarter_rows.append(row)
    diff_pp = _policy_diff_pp(accounts)
    verdict = _verdict(overall["n_graded"], overall["n_eff"],
                       (diff_pp or 0.0) / 100.0, accounts["defense_improved"])
    consecutive = 0
    for row in reversed(quarter_rows):
        if _qualifies_for_downgrade(row):
            consecutive += 1
        else:
            break
    pair_summary = {
        "pairs": len(pairs),
        "n_graded": overall["n_graded"],
        "n_eff": overall["n_eff"],
        "avoided_loss": overall["avoided_loss"],
        "missed_gain": overall["missed_gain"],
        "pair_net": overall["net"],
        "pair_net_pp": overall["net_pp"],
        "avoided_loss_usd": round(overall["avoided_loss"] * float(initial_cash), 2),
        "missed_gain_usd": round(overall["missed_gain"] * float(initial_cash), 2),
        "pair_net_usd": round(overall["net"] * float(initial_cash), 2),
        "avoidance_accuracy": overall["avoidance_accuracy"],
        "note_ko": "행동이 갈린 페어의 손익이다 — 계좌 순자산 차이(policy_diff_pp)와 다른 값이다(4.8).",
    }
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
        # ── 두 계좌(정책 효과) — P1-3 ──
        "accounts": accounts["accounts"],
        "account_rules": {"initial_cash": accounts["initial_cash"], "costs": accounts["costs"],
                          "horizon_td": horizon_td, "policy_versions": sorted(
                              {str(r.get("params_version") or "") for r in records or []})},
        "R_quant": accounts["R_quant"],
        "R_debated": accounts["R_debated"],
        "policy_diff_pp": diff_pp,
        "quant_mdd": accounts["quant_mdd"],
        "debate_mdd": accounts["debate_mdd"],
        "pair_summary": pair_summary,
        "pairs": pairs,
        "by_regime": by_regime,
        "quarters": quarter_rows,
        "consecutive_quarters": consecutive,
        "verdict": verdict,
        "verdict_ko": verdict,
        "note_ko": ("토론 비교 표본 없음 — 행동이 갈린 페어가 아직 없다(토론 0건이면 정상)."
                    if not pairs else None),
    }


def _policy_diff_pp(accounts: dict | None) -> float | None:
    if not accounts:
        return None
    return accounts.get("policy_diff_pp")


def write_comparison(comparison: dict, path: str | Path = COMPARISON_PATH) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# ────────────────────────────── 자동 하향 판정 ──────────────────────────────
def evaluate_auto_downgrade_eligibility(comparison: dict,
                                        params_history: list[dict] | None = None) -> dict:
    """`auto_downgrade_enabled` 전환 권고 (4.8.2 · 3.11.8 · v1.3.5).

    조건을 **모두** 만족할 때만 `recommend_enable: true` 다.
    ① 만기 확인된 행동 차이 페어 ≥ 30 ② `N_eff` ≥ 15
    ③ **비용 후 계좌 순자산 차이 `policy_diff_pp` ≥ +0.20%p** 가 **2분기 연속**
    ④ **MDD 절댓값이 정량 계좌보다 작음**(`abs(debate_mdd) < abs(quant_mdd)` 실측)
    ⑤ 그 2분기가 **달력으로 인접**하고(`2026Q1·2026Q2`), **같은 정책 버전을 명시**할 것

    [v1.3.5 · 중요 06] 세 가지를 조인다.
    - **인접 연속** — `2026Q1 · 2026Q3`(Q2 공백)는 2분기가 아니라 1분기다.
    - **정책 버전 명시** — 두 분기 모두 `policy_version` 이 있어야 하고 같아야 한다. 버전 집합이
      비었다고 "같은 정책"으로 보지 않는다.
    - **실 계좌 차이만** — `policy_diff_pp` 가 없으면 자격 없음(`basis="insufficient"`). 구 페어 손익
      (`net_after_cost`) fallback 은 **제거**했다.
    """
    comparison = comparison or {}
    rows = comparison.get("quarters") or []
    consecutive = 0
    used: list[dict] = []
    for row in reversed(rows):
        if _qualifies_for_downgrade(row):
            consecutive += 1
            used.append(row)
        else:
            break
    pairs = int(comparison.get("n_graded_pairs") or 0)
    n_eff = int(comparison.get("n_eff") or 0)
    diff_pp = comparison.get("policy_diff_pp")
    net = comparison.get("net_after_cost")
    basis = "account_policy_diff" if diff_pp is not None else "insufficient"
    effect = (float(diff_pp) / 100.0) if diff_pp is not None else None
    drawdown = comparison.get("drawdown") or {}
    quant_mdd = comparison.get("quant_mdd", drawdown.get("quant_mdd"))
    debate_mdd = comparison.get("debate_mdd", drawdown.get("debate_mdd"))
    if quant_mdd is not None and debate_mdd is not None:
        defense = bool(abs(float(debate_mdd)) < abs(float(quant_mdd)))
    else:
        defense = bool(drawdown.get("defense_improved"))
    used_quarters = [str(row.get("quarter") or "") for row in used]
    adjacent = consecutive_quarter_count(used_quarters)
    versions = {str(row.get("policy_version")) for row in used
                if row.get("policy_version") not in (None, "")}
    missing_versions = [str(row.get("quarter") or "?") for row in used
                        if row.get("policy_version") in (None, "")]
    same_policy = bool(used) and not missing_versions and len(versions) == 1
    reasons = []
    if pairs < MIN_DIVERGENT_PAIRS:
        reasons.append(f"행동 갈린 페어 {pairs}건 < {MIN_DIVERGENT_PAIRS}건")
    if n_eff < MIN_N_EFF:
        reasons.append(f"유효 표본 N_eff {n_eff} < {MIN_N_EFF}")
    if effect is None:
        reasons.append("계좌 순자산 차이(policy_diff_pp) 미산출 — 페어 손익으로 대신 판정하지 않는다")
    elif effect < NET_THRESHOLD:
        reasons.append(f"계좌 순자산 차이 {effect * 100:+.2f}%p < +0.20%p")
    if quant_mdd is not None and debate_mdd is not None and not defense:
        reasons.append(f"MDD 절댓값 개선 없음(정량 {float(quant_mdd):.4f} vs 토론 {float(debate_mdd):.4f})")
    elif quant_mdd is None and not defense:
        reasons.append("방어력 개선 없음(계좌 MDD 미계산)")
    if consecutive < CONSECUTIVE_QUARTERS:
        reasons.append(f"기준 충족 분기 {consecutive}개 < {CONSECUTIVE_QUARTERS}개 연속")
    if consecutive >= CONSECUTIVE_QUARTERS and adjacent < consecutive:
        reasons.append(f"연속 분기가 달력으로 이어지지 않는다: {', '.join(sorted(used_quarters))}")
    if missing_versions:
        reasons.append(f"분기 정책 버전이 없다: {', '.join(sorted(missing_versions))}")
    elif used and len(versions) != 1:
        reasons.append(f"연속 분기의 정책 버전이 다름: {', '.join(sorted(versions))}")
    eligible = not reasons
    history = params_history if isinstance(params_history, list) else []
    return {
        "current": bool(comparison.get("auto_downgrade_enabled")),
        "recommend_enable": bool(eligible),
        "eligible": bool(eligible),
        "verdict": comparison.get("verdict") or VERDICT_INSUFFICIENT,
        "pairs": pairs,
        "n_eff": n_eff,
        "basis": basis,
        "policy_diff_pp": None if diff_pp is None else round(float(diff_pp), 4),
        "net_after_cost": net,
        "net_after_cost_pp": comparison.get("net_after_cost_pp"),
        "quant_mdd": quant_mdd,
        "debate_mdd": debate_mdd,
        "defense_improved": defense,
        "same_policy_version": bool(same_policy),
        "policy_versions": sorted(versions),
        "consecutive_quarters": consecutive,
        "quarters": [{"quarter": row.get("quarter"), "n_graded": row.get("n_graded"),
                      "n_eff": row.get("n_eff"),
                      "policy_diff_pp": (row.get("policy_diff") or {}).get("policy_diff_pp"),
                      "net_after_cost": row.get("net_after_cost"),
                      "defense_improved": (row.get("drawdown") or {}).get("defense_improved"),
                      "verdict": row.get("verdict")}
                     for row in rows],
        "reasons_ko": reasons or ["네 조건을 모두 충족 — 전환 후보다(전환은 PR 로만 한다)"],
        "history_versions": [h.get("version") for h in history][-5:],
        "note_ko": ("정책 효과는 두 계좌의 순자산 차이로, 원인은 페어 손익으로 판단한다(4.8). "
                    "자동 하향은 이 판정을 통과해도 사람이 PR 로 켠다 — 기본값은 false."),
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
    """화면·요약용 최소 필드 — 표본이 작으면 숫자 대신 '판정 보류'를 크게 보여 준다(5.4).

    [v1.3.4 · P1-3] **계좌 정책 효과**(`policy_diff_pp`)와 **페어 손익**(`pair_net_pp`)을 나란히 둔다.
    예전에는 페어 합계를 "순효과"라는 한 숫자로 계좌 성과 자리에 올렸다.
    """
    comparison = comparison or {}
    pairs = int(comparison.get("n_graded_pairs") or 0)
    net_pp = comparison.get("net_after_cost_pp")
    diff_pp = comparison.get("policy_diff_pp")
    pair_summary = comparison.get("pair_summary") or {}
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
        "policy_diff_pp": diff_pp,
        "R_quant": comparison.get("R_quant"),
        "R_debated": comparison.get("R_debated"),
        "quant_mdd": comparison.get("quant_mdd"),
        "debate_mdd": comparison.get("debate_mdd"),
        "pair_summary": pair_summary,
        "verdict": comparison.get("verdict") or VERDICT_INSUFFICIENT,
        "auto_downgrade_enabled": bool(comparison.get("auto_downgrade_enabled")),
        "sample_short": pairs < MIN_DIVERGENT_PAIRS,
        "label_ko": (f"표본 {pairs}건 — 판정 보류" if pairs < MIN_DIVERGENT_PAIRS or net_pp is None
                     else (f"정책 효과 {float(diff_pp):+.2f}%p" if diff_pp is not None
                           else f"페어 순효과 {float(net_pp):+.2f}%p")),
    }


def next_trading_session(day: date, n: int = 1) -> date:
    """편의 함수 — 테스트가 63거래일 만기를 만들 때 쓴다."""
    return add_trading_days(day, n)


def sessions_between(start: date, end: date) -> int:
    return trading_day_count(start, end)
