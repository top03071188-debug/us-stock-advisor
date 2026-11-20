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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_LOG = Path("data/notifications_log.json")
DEFAULT_OUT = Path("data/notifications_latest.json")
SCHEMA_VERSION = 1
MAX_EVENTS_PER_RUN = 20

#: 이벤트 종류 → (화면 표기, 심각도)
EVENT_LABELS = {
    "strong_buy": ("강한 매수 신호(STRONG_BUY)", "info"),
    "exit": ("청산 신호(EXIT/S3/S5)", "bad"),
    "v06": ("시장 급변(V06) — 신규 매수 중단", "bad"),
    "degraded": ("데이터·아카이브 지연", "warn"),
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


# ────────────────────────────── 감지 ──────────────────────────────
def collect_events(payload: dict, *, debate: dict | None = None, health: dict | None = None) -> list[dict]:
    """발행 페이로드 → 알릴 사건 목록. **신호 0건·토론 0건이어도 정상 동작**한다."""
    payload = payload or {}
    as_of = str(payload.get("as_of") or "")
    events: list[dict] = []

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
    if not p.exists():
        return {"schema_version": SCHEMA_VERSION, "keys": {}}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schema_version": SCHEMA_VERSION, "keys": {}}
    if not isinstance(payload, dict):
        return {"schema_version": SCHEMA_VERSION, "keys": {}}
    payload.setdefault("keys", {})
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
            return {"ok": False, "detail": "NOTIFY_GITHUB_REPO 미설정 — 건너뜀"}
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
            return {"ok": False, "detail": "NOTIFY_WEBHOOK_URL 미설정 — 건너뜀"}
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
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
def evaluate_notifications(payload: dict, *, log_path: str | Path = DEFAULT_LOG,
                           out_path: str | Path | None = DEFAULT_OUT,
                           debate: dict | None = None, health: dict | None = None,
                           channels: list | None = None, now: datetime | None = None,
                           dry_run: bool = False) -> dict:
    """사건 감지 → 중복 차단 → 채널 발송 → 로그·요약 JSON. 실패해도 파이프라인을 멈추지 않는다."""
    at = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    log = load_log(log_path)
    events = collect_events(payload, debate=debate, health=health)
    new, suppressed = dedupe(events, log)
    sent: list[dict] = []
    channel_results: dict[str, dict] = {}
    for event in new:
        delivery = {}
        for channel in (channels if channels is not None else default_channels()):
            try:
                delivery[channel.name] = channel.send(event)
            except Exception as exc:  # noqa: BLE001
                delivery[channel.name] = {"ok": False, "detail": f"{type(exc).__name__}"}
        record = {**event, "sent_at": _iso(at), "channels": delivery}
        sent.append(record)
        for name, result in delivery.items():
            bucket = channel_results.setdefault(name, {"attempts": 0, "ok": 0, "skipped": 0})
            bucket["attempts"] += 1
            if result.get("ok"):
                bucket["ok"] += 1
            elif result.get("detail", "").endswith("건너뜀"):
                bucket["skipped"] += 1
    if not dry_run:
        for event in sent:
            log.setdefault("keys", {})[str(event["event_key"])] = {
                "sent_at": event["sent_at"], "type": event.get("type")}
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(at),
        "as_of": (payload or {}).get("as_of"),
        "n_events": len(events),
        "n_new": len(sent) if not dry_run else 0,
        "n_suppressed": len(suppressed) + (len(sent) if dry_run else 0),
        "new": sent,
        "suppressed": [{"event_key": e.get("event_key"), "type": e.get("type")} for e in suppressed],
        "channels": channel_results,
        "note_ko": "같은 signal_id·event_key 는 다시 보내지 않는다(중복 0건 계약).",
    }
    if out_path and not dry_run:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


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
