#!/usr/bin/env python3
"""scripts/notify.py — 알림 이벤트 감지 · 중복 차단 · 채널 어댑터 (기획서 Phase 4 · 5.2).

알리는 것(그리고 알리지 않는 것)
- `STRONG_BUY` 신호, `EXIT`/`S3`/`S5` 청산, `V06` 시장 급변, 데이터·아카이브 지연(`degraded`),
  그리고 **신호 후보가 있는데 토론이 없을 때** "토론 실행 권장".
- 같은 `signal_id`/`event_key` 는 **두 번 보내지 않는다**(`data/notifications_log.json`).
  재발송 0건이 계약이다(Phase 4 DoD).

채널
- 콘솔(항상) + JSON 요약(`data/notifications_latest.json`).
- GitHub Issue / Webhook 은 **환경변수가 있을 때만** 켠다(`NOTIFY_GITHUB_REPO`+`gh` CLI,
  `NOTIFY_WEBHOOK_URL`). 없으면 조용히 건너뛴다 — 알림 실패가 배치를 멈추지 않는다.

    python scripts/notify.py --signals data/quant_signals.json --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_LOG = Path("data/notifications_log.json")
DEFAULT_OUT = Path("data/notifications_latest.json")
#: 감사 5331ab7-11: 발송 장부가 `(event_key, channel)` 단위가 됐다 — 구 로그(schema 2)도 읽는다.
SCHEMA_VERSION = 3
#: 채널 한 곳의 전달 상태. `unconfigured` 는 "설정이 없어 보내지 않았다"(실패가 아니다).
CHANNEL_SENT = "sent"
CHANNEL_PENDING = "pending_retry"
CHANNEL_FAILED = "failed"
CHANNEL_UNCONFIGURED = "unconfigured"
CHANNEL_SUPPRESSED = "suppressed"
MAX_EVENTS_PER_RUN = 20
#: 실패한 발송을 다시 시도하는 횟수 상한 — 넘으면 `failed` 로 남긴다(P2-1).
MAX_ATTEMPTS = 3
#: 로그에 남기는 발송 상세 행 수 — 파일이 무한히 자라지 않게.
MAX_DELIVERY_ROWS = 500

#: 이벤트 종류 → (화면 표기, 심각도)
EVENT_LABELS = {
    "strong_buy": ("강한 매수 신호(STRONG_BUY)", "info"),
    "exit": ("청산 신호(EXIT/S3/S5)", "bad"),
    "v06": ("시장 급변(V06) — 신규 매수 중단", "bad"),
    "degraded": ("데이터·아카이브 지연", "warn"),
    "outage": ("데이터 장애 — 이번 세션 판단 보류", "bad"),
    "debate_recommended": ("토론 실행 권장", "info"),
}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _label(kind: str) -> str:
    return EVENT_LABELS.get(kind, (kind, "info"))[0]


def _severity(kind: str) -> str:
    return EVENT_LABELS.get(kind, (kind, "info"))[1]


def _event(kind: str, key: str, message: str, **extra) -> dict:
    payload = {"type": kind, "event_key": key, "label_ko": _label(kind), "severity": _severity(kind),
               "message_ko": message}
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def data_status_of(*values) -> str | None:
    """실행 상태를 공개 계약(`ok` | `degraded` | `outage`)으로 정규화한다(§10⑥).

    `run_daily` 가 **정산 health**(`positions_open`·`tranche*`·`stop_*` 뿐)만 넘기면
    `health.get("data_status")` 가 `None` 이 되어 `notifications_latest.json` 의 `data_status` 가
    비었다(감사 §10⑥). 그래서 실제 실행 상태가 담긴 곳을 차례로 본다:
    `health.data_status` → `payload.health.data_status` → `payload.status`.
    알 수 없는 값이면 `None`(모르는 것을 정상으로 꾸미지 않는다).
    """
    for value in values:
        text = str(value or "").strip().lower()
        if text in ("ok", "normal", "complete", "sample"):
            return "ok"
        if text in ("degraded", "stale", "partial"):
            return "degraded"
        if text in ("outage", "failed", "down"):
            return "outage"
    return None


def resolve_data_status(health: dict | None, payload: dict | None) -> str | None:
    """알림·요약이 쓰는 실행 상태 — **health 가 우선**, 없으면 페이로드의 상태부."""
    health = health or {}
    payload = payload or {}
    embedded = payload.get("health") or {}
    return data_status_of(health.get("data_status"), embedded.get("data_status"),
                          health.get("status"), payload.get("status"))


# ────────────────────────────── 감지 ──────────────────────────────
def collect_events(payload: dict, *, debate: dict | None = None, health: dict | None = None,
                   outage_id: str | None = None) -> list[dict]:
    """발행 페이로드 → 알릴 사건 목록. **신호 0건·토론 0건이어도 정상 동작**한다."""
    payload = payload or {}
    as_of = str(payload.get("as_of") or "")
    events: list[dict] = []

    # 장애 알림은 `outage_id` 로 묶는다 — 같은 장애를 매 시간 다시 알리지 않는다(P2-1).
    data_status = resolve_data_status(health, payload) or ""
    if data_status == "outage":
        oid = outage_id or str((health or {}).get("outage_id") or f"outage:{as_of}")
        last_success = (health or {}).get("last_success_at")
        events.append(_event(
            "outage", f"outage:{oid}",
            f"데이터 장애 — 이번 세션 판단 보류(마지막 성공 {last_success or '기록 없음'}). "
            f"직전 정상 신호를 그대로 표시합니다.",
            outage_id=oid))

    for signal in payload.get("signals") or []:
        grade = str(signal.get("trade_grade") or signal.get("decision") or "").upper()
        if grade == "STRONG_BUY":
            events.append(_event(
                "strong_buy", f"strong_buy:{signal.get('signal_id')}",
                f"{signal.get('ticker')} STRONG_BUY — 점수 {signal.get('entry_score')}, "
                f"1차 분할가 확인 필요",
                ticker=signal.get("ticker"), signal_id=signal.get("signal_id")))

    for check in payload.get("position_checks") or []:
        if str(check.get("verdict") or "").upper() != "EXIT":
            continue
        triggers = check.get("triggers") or {}
        codes = [code for code in ("S3", "S5") if triggers.get(code)]
        reason = codes[0] if codes else "EXIT"
        events.append(_event(
            "exit", f"exit:{check.get('ticker')}:{as_of}:{reason}",
            f"{check.get('ticker')} 청산 신호({reason}) — exit_score {check.get('exit_score')}",
            ticker=check.get("ticker")))

    v06 = [b for b in payload.get("blocked") or [] if "V06" in (b.get("codes") or [])]
    if v06:
        events.append(_event(
            "v06", f"v06:{as_of}",
            f"시장 급변(V06) — 신규 매수 전면 중단({len(v06)}종목 차단)"))

    diagnostics = payload.get("diagnostics") or {}
    degraded = list(diagnostics.get("degraded") or [])
    status = str(payload.get("status") or "")
    if degraded or status in ("degraded", "stale"):
        digest = hashlib.sha256(",".join(sorted(degraded)).encode("utf-8")).hexdigest()[:8]
        events.append(_event(
            "degraded", f"degraded:{as_of}:{status}:{digest}",
            f"{'데이터 지연' if status in ('degraded', 'stale') else '일부 소스 실패'}"
            f"({status or 'unknown'}) — {', '.join(degraded[:3]) or '소스 응답 없음'}"))

    covered = {str(v.get("signal_id")) for v in ((debate or {}).get("by_signal") or {}).values()
               if v.get("eligible")}
    for signal in payload.get("signals") or []:
        sid = str(signal.get("signal_id"))
        decision = str(signal.get("decision") or signal.get("type") or "").upper()
        if decision not in ("BUY", "WATCH") or sid in covered:
            continue
        if signal.get("quant_intent") == "no_action" and decision != "WATCH":
            continue
        events.append(_event(
            "debate_recommended", f"debate_recommended:{sid}",
            f"{signal.get('ticker')} 후보({decision}) — PC/Cloud 에서 토론을 돌리면 "
            "편입 자격(발행 후·주문 마감 전) 안에 붙일 수 있습니다",
            ticker=signal.get("ticker"), signal_id=sid))

    if health and int(health.get("stop_reentry_blocked_count") or 0) > 0:
        blocked = int(health["stop_reentry_blocked_count"])
        events.append(_event(
            "degraded", f"reentry_blocked:{as_of}:{blocked}",
            f"손절 직후 재진입 차단 {blocked}건 — 잔여 분할 주문이 같은 봉에서 취소됐습니다"))
    return events[:MAX_EVENTS_PER_RUN]


# ────────────────────────────── 중복 차단 ──────────────────────────────
def load_log(path: str | Path = DEFAULT_LOG) -> dict:
    p = Path(path)
    empty = {"schema_version": SCHEMA_VERSION, "keys": {}, "deliveries": []}
    if not p.exists():
        return empty
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty
    if not isinstance(payload, dict):
        return empty
    payload.setdefault("keys", {})
    payload.setdefault("deliveries", [])
    return payload


def dedupe(events: list[dict], log: dict) -> tuple[list[dict], list[dict]]:
    """이미 보낸 `event_key` 는 새 이벤트에서 뺀다 — 한 실행 안의 중복도 함께 막는다."""
    seen = set((log or {}).get("keys") or {})
    new: list[dict] = []
    suppressed: list[dict] = []
    for event in events:
        key = str(event.get("event_key"))
        if key in seen:
            suppressed.append(event)
            continue
        seen.add(key)
        new.append(event)
    return new, suppressed


def channel_rows(entry: dict) -> dict:
    """이벤트 장부의 채널별 상태 — 구 로그(채널 장부 이전)는 빈 dict 로 읽는다."""
    channels = (entry or {}).get("channels")
    return channels if isinstance(channels, dict) else {}


def pending_retries(log: dict, active: set[str] | list[str] | None = None) -> list[dict]:
    """다시 보내야 할 이벤트의 재시도 큐 — **아직 전달되지 않은 채널**이 근거다(감사 5331ab7-11).

    예전에는 이벤트 전체 상태(`any(channel.ok)`)만 봤기 때문에 콘솔이 성공하면 실패한 웹훅이
    다시 호출되지 않았다. 이제 큐는 채널 장부(`(event_key, channel)`)에서 `pending_retry` 인
    채널이 있는 이벤트를 모은다. 실제 재전송은 **이번 실행에서 켜졌고 아직 `sent` 가 아닌
    채널** 에만 한다(`_attempt`) — 이미 전달된 콘솔은 다시 출력하지 않는다.

    `active` 를 주면, 실패한 채널이 모두 꺼져 있어도 **아직 받지 못한 활성 채널**이 있으면
    큐에 넣는다(설정을 나중에 켜도 그 채널은 사건을 받을 자격이 있다).
    """
    rows: list[dict] = []
    active_names = None if active is None else {str(name) for name in active}
    for key, entry in ((log or {}).get("keys") or {}).items():
        if not isinstance(entry, dict):
            continue
        event = entry.get("event")
        if not isinstance(event, dict):
            continue
        channels = channel_rows(entry)
        if entry.get("status") == CHANNEL_SUPPRESSED:
            continue
        if channels:
            pending = [name for name, row in channels.items()
                       if isinstance(row, dict) and row.get("status") == CHANNEL_PENDING
                       and int(row.get("attempts") or 0) < MAX_ATTEMPTS]
            if not pending:
                continue
            waiting_active = [name for name in (active_names or set())
                              if name not in channels or channels.get(name, {}).get("status") != CHANNEL_SENT]
            if active_names is not None and not (set(pending) & active_names) and not waiting_active:
                continue
            rows.append({"event": event, "key": key, "channels": pending,
                         "attempts": max(int((channels.get(name) or {}).get("attempts") or 0)
                                         for name in pending)})
            continue
        # 구 로그(schema 2): 채널 장부가 없다 — 이벤트 상태가 pending_retry 면 활성 채널로 다시 본다.
        if entry.get("status") != CHANNEL_PENDING:
            continue
        if int(entry.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        if active_names is not None and not active_names:
            continue
        rows.append({"event": event, "key": key, "channels": [],
                     "attempts": int(entry.get("attempts") or 0)})
    return rows


def _is_unconfigured(result: dict | None) -> bool:
    """채널이 **설정이 없어 보내지 않은** 경우(실패가 아니다) — 재시도 큐에 넣지 않는다."""
    result = result or {}
    if result.get("unconfigured"):
        return True
    return str(result.get("detail") or "").endswith("건너뜀")


def event_status(channels: dict) -> str:
    """채널 상태들 → 이벤트 상태. 한 채널이라도 전달되면 `sent`(다른 채널 실패는 채널 장부에 남는다)."""
    statuses = [str((row or {}).get("status")) for row in (channels or {}).values()
                if isinstance(row, dict)]
    if CHANNEL_SENT in statuses:
        return CHANNEL_SENT
    if CHANNEL_PENDING in statuses:
        return CHANNEL_PENDING
    if CHANNEL_FAILED in statuses:
        return CHANNEL_FAILED
    if CHANNEL_SUPPRESSED in statuses:
        return CHANNEL_SUPPRESSED
    if statuses:
        return CHANNEL_UNCONFIGURED
    return CHANNEL_PENDING          # 시도 기록 없음 → 다음 실행에서 다시 본다


# ────────────────────────────── 채널 ──────────────────────────────
class ConsoleChannel:
    name = "console"

    def send(self, event: dict) -> dict:
        print(f"[notify] {event.get('label_ko')} · {event.get('message_ko')}", file=sys.stderr)
        return {"ok": True, "detail": None}


class GithubIssueChannel:
    """`gh` CLI 로 이슈를 만든다 — `NOTIFY_GITHUB_REPO` 가 없으면 건너뛴다."""

    name = "github_issue"

    def __init__(self, repo: str | None = None) -> None:
        self.repo = repo or os.environ.get("NOTIFY_GITHUB_REPO")

    def send(self, event: dict) -> dict:
        if not self.repo:
            return {"ok": False, "unconfigured": True, "detail": "NOTIFY_GITHUB_REPO 미설정 — 건너뜀"}
        title = f"[알림] {event.get('label_ko')} — {event.get('ticker') or event.get('event_key')}"
        try:
            proc = subprocess.run(["gh", "issue", "create", "--repo", self.repo, "--title", title,
                                   "--body", str(event.get("message_ko") or ""), "--label", "alert"],
                                  capture_output=True, text=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}
        return {"ok": proc.returncode == 0, "detail": (proc.stdout or proc.stderr)[:200]}


class WebhookChannel:
    """`NOTIFY_WEBHOOK_URL` 로 JSON POST — 없으면 건너뛴다."""

    name = "webhook"

    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("NOTIFY_WEBHOOK_URL")

    def send(self, event: dict) -> dict:
        if not self.url:
            return {"ok": False, "unconfigured": True, "detail": "NOTIFY_WEBHOOK_URL 미설정 — 건너뜀"}
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # 감사 5331ab7-11: 재시도·워크플로 재실행의 중복을 받는 쪽이 접을 수 있게 멱등 키를 붙인다.
        idempotency_key = str(event.get("event_key") or "").strip()
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 — 설정된 URL 만
                return {"ok": 200 <= response.status < 300, "detail": f"HTTP {response.status}"}
        except Exception as exc:  # noqa: BLE001 — 알림 실패가 배치를 멈추지 않는다
            return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}


def default_channels(names: list[str] | None = None) -> list:
    factory = {"console": ConsoleChannel, "github_issue": GithubIssueChannel,
               "webhook": WebhookChannel}
    chosen = names or ["console", "github_issue", "webhook"]
    return [factory[name]() for name in chosen if name in factory]


# ────────────────────────────── 본체 ──────────────────────────────
def _eligible_debate_row(row: dict) -> bool:
    """이 토론 행이 **편입 적격**인가 — `eligible: true` 또는 적격 상태 표기."""
    if not isinstance(row, dict):
        return False
    if row.get("eligible") is False:
        return False
    if row.get("eligible") is True:
        return True
    return str(row.get("status") or "").strip().lower() in ("eligible", "adopted", "confirmed", "included")


def retry_is_stale(event: dict, *, debate: dict | None = None, debate_events=None) -> bool:
    """재시도 대상 후보 추천이 **지금은 무효**인가 — 이미 적격 토론이 편입된 신호다(감사 5331ab7-11).

    발송에 실패한 "토론 실행 권장"을 다음 실행에서 그대로 다시 보내면, 그 사이 편입된 적격
    토론을 무시한 권장이 나간다. 재전송 전에 현재 `debate` 요약·`debate_events` 로 다시 판정해
    무효면 보내지 않고 `suppressed` 로 닫는다.
    """
    if str(event.get("type") or "") != "debate_recommended":
        return False
    target = str(event.get("signal_id") or "").strip()
    if not target:
        return False
    by_signal = (debate or {}).get("by_signal") if isinstance(debate, dict) else None
    for key, value in (by_signal or {}).items():
        rows = list(value) if isinstance(value, (list, tuple)) else [value]
        for row in rows:
            if not _eligible_debate_row(row):
                continue
            sid = str((row or {}).get("signal_id") or key).strip() if isinstance(row, dict) else str(key)
            if sid == target:
                return True
    for row in _iter_debate_events(debate_events):
        if str(row.get("signal_id") or "").strip() != target:
            continue
        if _eligible_debate_row(row):
            return True
    return False


def _iter_debate_events(debate_events) -> list[dict]:
    """`debate_events` 입력 → 행 목록. dict(신호 ID 키)·list 어느 형태든 받는다."""
    if isinstance(debate_events, dict):
        out: list[dict] = []
        for key, value in debate_events.items():
            rows = list(value) if isinstance(value, (list, tuple)) else [value]
            for row in rows:
                if isinstance(row, dict):
                    out.append(row if row.get("signal_id") else {**row, "signal_id": str(key)})
        return out
    if isinstance(debate_events, (list, tuple)):
        return [row for row in debate_events if isinstance(row, dict)]
    return []


def _persist_log(log: dict, log_path: str | Path) -> None:
    """발송 장부를 지금 디스크에 쓴다 — 외부 발송 **전**에도 한 번 써서 재실행 중복을 줄인다.

    **원자적 쓰기**(임시 파일 → `os.replace`)로 남긴다. 쓰는 도중 프로세스가 죽어 장부가 깨지면
    다음 실행이 모든 사건을 "처음"으로 보고 다시 보낼 수 있다(감사 5331ab7-11).
    """
    log["schema_version"] = SCHEMA_VERSION
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def evaluate_notifications(payload: dict, *, log_path: str | Path = DEFAULT_LOG,
                           out_path: str | Path | None = DEFAULT_OUT,
                           debate: dict | None = None, health: dict | None = None,
                           outage_id: str | None = None,
                           channels: list | None = None, now: datetime | None = None,
                           dry_run: bool = False,
                           debate_events=None) -> dict:
    """사건 감지 → 중복 차단 → 채널 발송 → 로그·요약 JSON. 실패해도 파이프라인을 멈추지 않는다.

    발송 장부는 **`(event_key, channel)` 단위**다(감사 5331ab7-11). 채널별 상태는
    `sent` / `pending_retry` / `failed` / `unconfigured` / `suppressed` 중 하나이고,
    **콘솔이 성공해도 실패한 웹훅은 `pending_retry` 로 남아 다음 실행에서 다시 시도된다**.
    이미 전달된 채널은 다시 보내지 않고, 재시도 전 "토론 실행 권장"은 현재 편입 상태로 재판정한다.
    """
    at = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    log = load_log(log_path)
    active_channels = channels if channels is not None else default_channels()
    active_names = [channel.name for channel in active_channels]
    events = collect_events(payload, debate=debate, health=health, outage_id=outage_id)
    new, suppressed = dedupe(events, log)
    retries = pending_retries(log, active=active_names)

    sent: list[dict] = []
    channel_results: dict[str, dict] = {}
    delivery_rows: list[dict] = []
    retried = 0
    rejudged: list[dict] = []

    def _count(rows: list[dict]) -> None:
        for row in rows:
            bucket = channel_results.setdefault(row["channel"],
                                                {"attempts": 0, "sent": 0, "pending_retry": 0,
                                                 "failed": 0, "unconfigured": 0, "suppressed": 0})
            bucket["attempts"] += 1
            status = str(row.get("status") or "")
            if status in bucket:
                bucket[status] += 1

    def _attempt(event: dict, *, is_retry: bool, pending_channel: str | None = None) -> None:
        nonlocal retried
        key = str(event.get("event_key"))
        entries = log.setdefault("keys", {})
        entry = entries.get(key)
        if not isinstance(entry, dict):
            entry = {"sent_at": None, "type": event.get("type"), "status": CHANNEL_PENDING,
                     "attempts": 0, "event": event, "channels": {}}
            entries[key] = entry
        entry["event"] = event
        entry["type"] = entry.get("type") or event.get("type")
        channels_doc = channel_rows(entry)
        if not channels_doc:
            channels_doc = {}
        entry["channels"] = channels_doc
        # 이미 전달된 채널은 **다시 보내지 않는다**. 재시도에서는 실패한 채널(또는 시도 기록이
        # 없는 신규 활성 채널)만 대상으로 삼는다.
        targets = []
        for channel in active_channels:
            row = channels_doc.get(channel.name)
            row = row if isinstance(row, dict) else {}
            if row.get("status") == CHANNEL_SENT:
                continue
            if row.get("status") in (CHANNEL_FAILED, CHANNEL_SUPPRESSED):
                continue
            if int(row.get("attempts") or 0) >= MAX_ATTEMPTS:
                continue
            targets.append(channel)
        if not dry_run:
            # 발송 **전**에 시도 사실을 디스크에 남긴다 — 워크플로가 중간에 죽어도 재실행이
            # 같은 사건을 처음부터 다시 만들지 않고, 채널 멱등 키로 중복을 줍는다.
            for channel in targets:
                row = channels_doc.setdefault(channel.name, {"attempts": 0})
                row["attempts"] = int(row.get("attempts") or 0) + 1
                row["status"] = CHANNEL_PENDING
                row["attempt_at"] = _iso(at)
                row["detail"] = row.get("detail") or "in_flight"
            entry["status"] = event_status(channels_doc)
            entry["attempt_at"] = _iso(at)
            _persist_log(log, log_path)
        for channel in targets:
            started = time.monotonic()
            try:
                result = channel.send(event)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "detail": f"{type(exc).__name__}"}
            latency = round(time.monotonic() - started, 4)
            row = channels_doc.setdefault(channel.name, {"attempts": 0})
            attempts = int(row.get("attempts") or 0)
            if result.get("ok"):
                row["status"] = CHANNEL_SENT
            elif _is_unconfigured(result):
                row["status"] = CHANNEL_UNCONFIGURED
                row["attempts"] = max(0, attempts - 1)    # 설정이 없었던 것은 시도가 아니다
            else:
                row["status"] = CHANNEL_FAILED if attempts >= MAX_ATTEMPTS else CHANNEL_PENDING
            row["attempt_at"] = _iso(at)
            row["latency_seconds"] = latency
            row["detail"] = str(result.get("detail") or "")[:120] or None
            row["attempts"] = int(row.get("attempts") or 0)
            delivery_rows.append({"channel": channel.name, "event_key": key, "type": event.get("type"),
                                  "status": row["status"], "attempts": row["attempts"],
                                  "attempt_at": _iso(at), "latency_seconds": latency,
                                  "detail": row["detail"]})
            _count([{"channel": channel.name, "status": row["status"]}])
        status = event_status(channels_doc)
        entry["status"] = status
        entry["attempts"] = max([int((row or {}).get("attempts") or 0)
                                for row in channels_doc.values()] or [0])
        entry["sent_at"] = _iso(at) if status == CHANNEL_SENT else None
        entry["attempt_at"] = _iso(at)
        record = {**event, "sent_at": _iso(at), "channels": channels_doc, "status": status,
                  "retried": bool(is_retry)}
        if status == CHANNEL_SENT:
            sent.append(record)
        if is_retry:
            retried += 1

    def _close_suppressed(event: dict, channel: str | None) -> None:
        """재판정으로 무효가 된 재시도 — 보내지 않고 그 채널을 `suppressed` 로 닫는다."""
        key = str(event.get("event_key"))
        entry = (log.get("keys") or {}).get(key)
        if not isinstance(entry, dict):
            return
        channels_doc = channel_rows(entry)
        if not channels_doc:
            channels_doc = {channel or "unknown": {"attempts": int(entry.get("attempts") or 0)}}
            entry["channels"] = channels_doc
        for name, row in channels_doc.items():
            if not isinstance(row, dict):
                continue
            if channel is not None and name != channel:
                continue
            if row.get("status") == CHANNEL_SENT:
                continue
            row["status"] = CHANNEL_SUPPRESSED
            row["detail"] = "적격 토론 편입으로 무효 — 재전송하지 않음"
            row["attempt_at"] = _iso(at)
            _count([{"channel": name, "status": CHANNEL_SUPPRESSED}])
        entry["status"] = event_status(channels_doc)
        entry["attempt_at"] = _iso(at)

    for pending in retries:
        event = pending["event"]
        if retry_is_stale(event, debate=debate, debate_events=debate_events):
            _close_suppressed(event, None)
            rejudged.append({"event_key": event.get("event_key"), "type": event.get("type"),
                             "reason": "eligible_debate_now"})
            continue
        _attempt(event, is_retry=True)
    for event in new:
        _attempt(event, is_retry=False)

    if not dry_run:
        if delivery_rows:
            log["deliveries"] = (list(log.get("deliveries") or []) + delivery_rows)[-MAX_DELIVERY_ROWS:]
        _persist_log(log, log_path)
    retry_pending = []
    retry_pending_inactive = []
    for row in pending_retries(log, active=active_names):
        key = row["key"]
        entry = (log.get("keys") or {}).get(key) or {}
        channels = channel_rows(entry)
        for name in row.get("channels") or []:
            item = {"event_key": key, "channel": name, "type": (row.get("event") or {}).get("type"),
                    "attempts": int((channels.get(name) or {}).get("attempts") or 0)}
            if name in active_names:
                retry_pending.append(item)
            else:
                retry_pending_inactive.append(item)
    suppressed_events = ([{"event_key": e.get("event_key"), "type": e.get("type")} for e in suppressed]
                         + rejudged)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(at),
        "as_of": (payload or {}).get("as_of"),
        # §10⑥ — 정산 health 만 넘어와도 실행 상태가 비지 않게 한다.
        "data_status": resolve_data_status(health, payload),
        "outage_id": ((health or {}).get("outage_id") or (payload or {}).get("health", {}).get("outage_id")
                      or outage_id),
        "n_events": len(events),
        "n_new": len([r for r in sent if not r.get("retried")]) if not dry_run else 0,
        "n_suppressed": len(suppressed_events) + (len(sent) if dry_run else 0),
        "n_retried": retried,
        "n_retry_suppressed": len(rejudged),
        "retry_pending": retry_pending[:20],
        # 꺼져 있는 채널(환경변수 없음)의 재시도 잔여 — 다시 켜지면 그때 보낸다.
        "retry_pending_inactive": retry_pending_inactive[:20],
        "new": sent,
        "suppressed": suppressed_events,
        "channels": channel_results,
        "note_ko": ("같은 signal_id·event_key 는 다시 보내지 않는다(중복 0건 계약). "
                    "전달 장부는 (event_key, channel) 단위이며, 실패한 채널만 다음 실행에서 재시도한다."),
    }
    if out_path and not dry_run:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def log_deliveries(event: dict, delivery: dict, latency: dict, at: datetime) -> list[dict]:
    """채널별 발송 결과 → 로그 행(``channel + event_key + status + attempt_at + latency_seconds``)."""
    rows: list[dict] = []
    for name, result in delivery.items():
        detail = str((result or {}).get("detail") or "")
        if (result or {}).get("ok"):
            status = CHANNEL_SENT
        elif _is_unconfigured(result):
            status = CHANNEL_UNCONFIGURED
        else:
            status = CHANNEL_PENDING
        rows.append({"channel": name, "event_key": event.get("event_key"),
                     "type": event.get("type"), "status": status,
                     "attempt_at": _iso(at), "latency_seconds": latency.get(name),
                     "detail": detail[:120] or None})
    return rows


def _load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="알림 감지·중복 차단(단독 실행용)")
    ap.add_argument("--signals", default="data/quant_signals.json")
    ap.add_argument("--debate", default="data/ledger/debate_summary.json")
    ap.add_argument("--health", default="data/health.json")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--channel", action="append", default=None,
                    choices=["console", "github_issue", "webhook"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    payload = _load(Path(args.signals)) or {}
    summary = evaluate_notifications(payload, log_path=Path(args.log), out_path=Path(args.out),
                                     debate=_load(Path(args.debate)), health=_load(Path(args.health)),
                                     channels=default_channels(args.channel) if args.channel else None,
                                     dry_run=args.dry_run)
    print(json.dumps({k: summary[k] for k in ("n_events", "n_new", "n_suppressed")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
