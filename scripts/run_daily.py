#!/usr/bin/env python3
"""scripts/run_daily.py — 하루 파이프라인: 수집 → 지표 → 신호 → Paper Ledger → 스냅샷 → 발행.

`.github/workflows/daily-postclose.yml` 계약 [고정]
    python scripts/run_daily.py --mode postclose
- `GITHUB_OUTPUT` 환경변수가 있으면 워크플로 분기용 결과를 쓴다(감사 11):
  `snapshot_id`, `snapshot_id_short`, `status`, `n_signals`, `n_blocked`,
  **`run_outcome`**(`normal`|`outage`|`rejected`), **`status_written`**(health 기록 여부),
  **`snapshot_created`**(패키징할 스냅샷이 있는가) + `snapshot_state`(created|reused|failed|missing),
  `data_status`, `outage_id`.
- 환경변수(FRED_API_KEY·SEC_USER_AGENT·FINNHUB_API_KEY)가 없거나 API 가 실패해도 **크래시하지 않는다**:
  소스별로 `degraded` 를 남기고, 가격이 하나도 없으면 **전일 데이터를 유지**한 채 종료한다
  (사이트가 빈 화면이 되지 않게 — 기획서 2.7).
- 산출물: `data/quant_signals.json`, `data/ledger/records.jsonl`(발행 의도),
  `data/ledger/executions/{signal_id}.json`(체결 이벤트), `data/ledger/portfolio/state.json`(비중만),
  `data/ledger/pilot_eval.json`, `data/params_history.json`, `data/snapshots/{as_of}/`(아카이브 원본 —
  같은 날 입력이 바뀌면 `{as_of}-r2` 로 보존).
- 발행 시각은 **결과가 확정되는 실제 시각**(`signal_published_at`)이고, 배치 시작 시각은
  `run_started_at` 으로 따로 남긴다. 같은 입력 지문(`inputs_hash`)의 재실행은 **원장의 발행본을
  그대로 배포**하고, 입력이 바뀐 발행은 회차 접미사(`-002`…)와 `supersedes` 로 남긴다(감사 01·02).

실행 순서(고정 · 감사 P0-2·P0-4·P2-1 · §10② · 01-d):
    ① 원장·체결 로드(정산 전 스냅샷 유지) → ② 토론 편입 검증(`now`=보고서 파일을 읽은 수신 시각)
    → ③ 1차 체결 **계산만**(기록 전 — 오늘 보유·섹터 비중 확정) → ④ 신호 생성·스냅샷
    → ⑤ 발행·대체 레코드 append → ⑥ 차단 대체 레코드(`make_blocked_supersede_record`)
    → ⑦ 1차 정산·취소 **기록**(발행 확정 뒤, `blocked=` 포함) → ⑧ 2·3차·손절·TRIM 정산
    → ⑨ Paper 채점 → ⑩ A/B 비교 → ⑪ 알림
- 발행 확정이 정산보다 **먼저**다: 같은 실행에서 만들어진 대체(`-002→-003`)의 취소가 그 자리에서
  기록되고(이전에는 다음 실행이 과거 시각으로 back-date 했다), 같은 배치의 차단이 시가 체결을 막는다.
- 편입 검증도 정산보다 **먼저**다: 같은 배치의 체결을 보고 소급 페어를 만들지 않는다.
- Paper 채점은 `holdout_start=None` 이다: 만기 봉이 관측된 신호를 보정 홀드아웃 때문에 잘라 내지 않는다.
- 장애(시세 전체 실패) 경로도 `data/health.json` 에 `data_status=outage`·`last_attempt_at`·
  `last_success_at`·`outage_id` 를 남기고 전일 정상 `quant_signals.json` 을 보존한다.

    python scripts/run_daily.py --mode postclose --offline --as-of 2026-11-20 --root .
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.backtest.holdout import window_for  # noqa: E402
from engine.backtest.snapshot import inputs_fingerprint, make_snapshot  # noqa: E402
from engine.decide.macro import classify_regime  # noqa: E402
from engine.decide.pipeline import build_payload, load_params  # noqa: E402
from engine.ledger import debate as debate_mod  # noqa: E402
from engine.ledger import live  # noqa: E402
from engine.ledger import record as record_mod  # noqa: E402
from engine.ledger.pilot import evaluate_pilot, write_pilot_eval  # noqa: E402
from engine.ledger.record import (RecordError, append_immutable, attach_target_session,  # noqa: E402
                                  load_records, resolve_release)
from scripts import notify  # noqa: E402
from engine.models import QuantSignalSchemaError, validate_quant_signals  # noqa: E402
from engine.utils.dates import (KST, data_cutoff_at, order_cutoff_at, signal_published_at,  # noqa: E402
                                target_session_for)

RECORDS = Path("data/ledger/records.jsonl")
EXECUTIONS = Path("data/ledger/executions")
PORTFOLIO = Path("data/ledger/portfolio")
SNAPSHOTS = Path("data/snapshots")
SIGNALS = Path("data/quant_signals.json")
PILOT = Path("data/ledger/pilot_eval.json")
PARAMS_HISTORY = Path("data/params_history.json")
WATCHLIST = Path("data/watchlist/current.json")
RADAR = Path("data/radar/13f.json")
FOLDS = Path("data/walkforward/folds/summary.json")
LIMITS = Path("data/pipeline_limits.json")
DEBATE_REPORTS = debate_mod.REPORTS_PATH
DEBATE_EVENTS = debate_mod.EVENTS_DIR
DEBATE_COMPARISON = debate_mod.COMPARISON_PATH
HEALTH = live.HEALTH_PATH
NOTIFICATIONS = Path("data/notifications_latest.json")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


#: `live.settle_positions` 가 처리하는 강제 청산 사유 — 엔진이 `TRIM` 을 지원하면 그 모듈 상수가
#: `{"S3", "S5", "EXIT", "TRIM"}` 이 되어 1/3 익절 경로도 배선된다(트랙 1 계약).
DEFAULT_FORCED_EXIT_REASONS = {"S3", "S5", "EXIT"}


def _supports(fn, name: str) -> bool:
    """그 함수가 이 키워드를 **명시적 매개변수로** 받는가.

    `**kwargs` 를 받는 래퍼(테스트 스파이 등)까지 "지원"으로 보면 안 된다 — 래퍼가 원본에 그대로
    넘겨 `TypeError` 가 나고 배치가 죽는다. 그래서 이름이 시그니처에 있을 때만 넘긴다.
    (다른 트랙이 아직 합치지 않은 시그니처에서도 배치가 살아 있게 하는 장치다.)
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params


def _forced_exit_reasons() -> set[str]:
    return set(getattr(live, "FORCED_EXIT_REASONS", DEFAULT_FORCED_EXIT_REASONS))


_MISSING = object()


class DebateByKey(dict):
    """편입 토론 색인 — 키는 **(signal_id, snapshot_id)**, ticker 는 폴백이다(감사 07).

    `ticker` 만으로 넘기면 같은 날 다른 스냅샷의 토론이 새 판단의 `news_state`·구조적 리스크를
    바꿀 수 있다(기획서 4.8.1 은 `signal_id`+`snapshot_id` 로 묶으라고 못 박는다).
    통합 전 호출부(`debate.get(ticker)`)도 깨지지 않도록 ticker 폴백을 남긴다.
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_ticker: dict[str, dict] = {}

    def add(self, signal_id, snapshot_id, ticker, view: dict) -> None:
        if signal_id:
            dict.__setitem__(self, (str(signal_id), str(snapshot_id or "")), view)
        if ticker:
            self.by_ticker[str(ticker)] = view

    def get(self, key, default=None):
        found = self.lookup(key)
        return default if found is _MISSING else found

    def lookup(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if isinstance(key, str):
            return self.by_ticker.get(key, _MISSING)
        if isinstance(key, tuple) and key:
            sid = str(key[0])
            want_snapshot = str(key[1]) if len(key) > 1 and key[1] else None
            for (stored_sid, stored_snapshot), view in dict.items(self):
                if stored_sid != sid:
                    continue
                if want_snapshot is None or stored_snapshot == want_snapshot:
                    return view
        return _MISSING

    def __getitem__(self, key):
        found = self.lookup(key)
        if found is _MISSING:
            raise KeyError(key)
        return found

    def __contains__(self, key) -> bool:
        return self.lookup(key) is not _MISSING

    def __bool__(self) -> bool:
        return dict.__len__(self) > 0


def debate_index(by_signal: dict, records: list[dict]) -> DebateByKey:
    """`by_signal`(자격 통과분) → (signal_id, snapshot_id) 색인. 스냅샷이 다르면 다른 키다."""
    snapshots = {str(r.get("signal_id")): str(r.get("snapshot_id") or "") for r in records or []}
    index = DebateByKey()
    for sid, view in (by_signal or {}).items():
        if not view or not view.get("eligible"):
            continue
        index.add(sid, view.get("snapshot_id") or snapshots.get(str(sid)),
                  view.get("ticker"), view)
    return index


def _write_github_output(pairs: dict) -> None:
    """`GITHUB_OUTPUT` — 워크플로가 분기할 수 있는 실행 결과를 남긴다(감사 11).

    - `run_outcome`: `normal` | `outage` | `rejected`(계약 검증 실패)
    - `status_written`: `data/health.json` 에 실행 상태를 남겼는가
    - `snapshot_created`: 이번 실행이 참조한 스냅샷 디렉터리가 **있어 패키징할 수 있는가**
      (`snapshot_state` 가 `created`/`reused`/`failed` 로 이유를 알려 준다)
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in pairs.items():
            if value is None:
                continue
            text = str(value).lower() if isinstance(value, bool) else str(value)
            f.write(f"{key}={text}\n")


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _git_commit() -> str:
    """현재 커밋 해시(짧은 형식) — 기록용. 실패해도 파이프라인을 막지 않는다."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False)
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def outage_id_for(as_of: date, mode: str, degraded: list | None) -> str:
    """장애 ID — **같은 날 같은 실패**는 같은 ID 가 되어 알림이 중복되지 않는다(P2-1)."""
    parts = [as_of.isoformat(), mode] + sorted(str(d) for d in (degraded or []))
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"outage-{as_of.isoformat()}-{digest}"


def write_health_state(*, as_of: date, mode: str, data_status: str, degraded: list | None = None,
                       reason: str | None = None, target_session: date | None = None,
                       previous: dict | None = None, extra: dict | None = None) -> dict:
    """`data/health.json` 의 **실행 상태부**를 갱신한다(P2-1).

    - `data_status`: `ok` | `degraded` | `outage` — 화면이 "이번 세션 판단 보류"를 구분해 그린다.
    - `last_attempt_at`: 이번 시도 시각, `last_success_at`: **직전 성공 시각을 보존**한다.
    - `data_cutoff_at`·`target_session`·`outage_id`·`reason` 을 함께 남겨 왜 보류인지 추적한다.
    - 기존 파일의 규칙 건강 지표(보유·분할 대기·손절)는 **지우지 않는다** — 마지막 성공 값이다.
    """
    prev = previous if previous is not None else (_read_json(HEALTH) or {})
    if not isinstance(prev, dict):
        prev = {}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    last_success = prev.get("last_success_at")
    if not last_success and str(prev.get("data_status") or prev.get("status") or "") in ("ok", "normal", "sample"):
        last_success = prev.get("last_run_at") or prev.get("generated_at")
    if data_status == "ok":
        last_success = _iso(now)
    session = target_session or target_session_for(now)
    payload = {**prev}
    payload.update({
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "generated_at": _iso(now),
        "status": "degraded" if data_status != "ok" else prev.get("status", "normal"),
        "data_status": data_status,
        "last_attempt_at": _iso(now),
        "last_success_at": last_success,
        "data_cutoff_at": _iso(data_cutoff_at(as_of, mode)),
        "target_session": session.isoformat() if session else None,
        "outage_id": None if data_status == "ok" else outage_id_for(as_of, mode, degraded),
        "reason": reason,
        "degraded": list(degraded or []),
        "last_run_at": _iso(now),
    })
    if data_status == "outage":
        payload["note_ko"] = ("이번 세션 판단 보류 — 마지막 성공 데이터를 표시 중입니다"
                              "(직전 정상 신호는 그대로 보존).")
    elif data_status == "degraded":
        payload["note_ko"] = "일부 데이터 소스 실패 — 신선도를 확인하세요(판단은 계속)."
    payload.update(extra or {})
    return payload


def publish_times(as_of: date, mode: str, published_at: datetime | None = None) -> dict:
    """`data_cutoff_at ≤ signal_published_at < order_cutoff_at` — 세 시각을 따로 계산한다(2.2.2).

    `signal_published_at` 은 **결과 JSON 이 확정되는 실제 시각**이다. 배치 **시작 시각**으로 잡으면
    수집이 20분 걸리는 날 "14:10Z 발행"이라고 우기면서 이미 끝난 주문 마감의 세션을 가리킨다
    (감사 01-b). 그래서 수집·정산이 끝난 뒤 호출자가 **발행 직전 시각**을 넘긴다.
    대상 세션(`target_session`)은 그 시각 이후 주문 마감이 아직 오지 않은 가장 이른 NYSE 세션이다.
    과거 `as_of` 백필은 계획 시각을 발행 시각으로 쓴다(마감 전 실행 가정).
    """
    cutoff = data_cutoff_at(as_of, mode)
    planned = signal_published_at(cutoff)
    actual = (published_at if published_at is not None else datetime.now(timezone.utc)).replace(microsecond=0)
    if actual < planned:
        actual = planned
    session = target_session_for(actual)
    if session is None:
        raise SystemExit(f"대상 거래일을 찾을 수 없다: published={_iso(actual)}")
    order = order_cutoff_at(session)
    if not (cutoff <= actual < order):
        raise SystemExit(f"세 시각 순서 위반: {cutoff} / {actual} / {order}")
    return {"data_cutoff_at": _iso(cutoff), "signal_published_at": _iso(actual),
            "signal_published_planned_at": planned, "order_cutoff_at": _iso(order),
            "target_session": session.isoformat(),
            "_cutoff": cutoff}


def three_times(as_of: date, mode: str, now: datetime | None = None) -> dict:
    """구 이름 호환 — `publish_times(as_of, mode, published_at=now)` 와 같다."""
    return publish_times(as_of, mode, published_at=now)


def ensure_snapshot(root: Path, *, as_of: date, cutoff: datetime, frames: dict, inputs: dict,
                    extra: dict, inputs_hash: str) -> tuple[str, str]:
    """스냅샷을 고정한다 — 같은 입력 지문이면 **다시 쓰지 않고 재사용**한다.

    - 같은 날 재실행으로 입력이 같으면 기존 `data/snapshots/{as_of}` 를 그대로 쓰고 `snapshot_id`
      도 유지한다(화면과 원장이 같은 스냅샷을 가리키게).
    - 입력이 달라 **새로 확정**된 발행이면 `{as_of}-r2`, `-r3` … 로 **이전 스냅샷을 덮어쓰지 않고**
      보존한다(append-only 아카이브의 원본이 사라지지 않게).
    Returns: (`snapshot_id`, `"reused"|"created"`)
    """
    day = as_of.isoformat()

    def _existing_match(path: Path) -> str | None:
        manifest = _read_json(path / "manifest.json") or {}
        if (manifest.get("extra") or {}).get("inputs_hash") == inputs_hash:
            return str(manifest.get("snapshot_id") or "")
        return None

    base = root / day
    if base.exists():
        reused = _existing_match(base)
        if reused:
            return reused, "reused"
        n = 2
        while (root / f"{day}-r{n}").exists():
            reused = _existing_match(root / f"{day}-r{n}")
            if reused:
                return reused, "reused"
            n += 1
        base = root / f"{day}-r{n}"
    snap = make_snapshot(base, cutoff, frames, as_of=day, inputs=inputs,
                         extra={**extra, "inputs_hash": inputs_hash})
    return snap.snapshot_id, "created"


def load_dataset(args, as_of: date, cutoff: datetime) -> dict:
    """픽스처(오프라인·테스트) 또는 라이브 수집. 라이브 실패는 예외가 아니라 `degraded`."""
    if args.fixtures:
        from engine.data.fixtures import dataset  # noqa: PLC0415
        data = dataset(as_of)
        data["source"] = "fixtures"
        data["diagnostics"] = {"mode": "fixtures", "degraded": [], "sources": {"fixtures": {"status": "ok"}}}
        return data
    from engine.data.collect import collect  # noqa: PLC0415
    try:
        return collect(as_of, cutoff)
    except Exception as exc:  # noqa: BLE001 — 수집 전체 실패도 파이프라인 실패가 아니다
        return {"as_of": as_of.isoformat(), "universe": [], "panels": {}, "macro": {}, "macro_previous": {},
                "filings": {}, "news": {}, "fundamentals": {}, "valuation_inputs": {},
                "source": "live", "diagnostics": {"mode": "live", "degraded": [f"collect:{type(exc).__name__}"],
                                                  "sources": {"collect": {"status": "failed", "detail": str(exc)[:200]}}}}


def ensure_params_history(params: dict, path: Path = PARAMS_HISTORY) -> list[dict]:
    """활성 파라미터 버전을 이력에 남긴다(없을 때만 append — 이력은 append-only)."""
    version = str(params.get("version") or "params_v001")
    history = _read_json(path) or []
    if isinstance(history, dict):
        history = history.get("versions", [])
    if not any(h.get("version") == version for h in history):
        history.append({
            "version": version,
            "created_at": str(params.get("created_at") or date.today().isoformat()),
            "parent_version": history[-1]["version"] if history else None,
            "gate_result": {"status": "active", "note_ko": "워크포워드 보정은 PR 로만 반영한다(4.3)"},
            "changed": [],
            "note_ko": "초기 버전 — 보정 이력 없음",
        })
        _write_json(path, history)
    return history


def ensure_folds(as_of: date) -> dict:
    folds = _read_json(FOLDS)
    if folds and folds.get("horizons"):
        return folds
    from engine.utils.dates import fold_summary  # noqa: PLC0415
    folds = fold_summary(as_of)
    _write_json(FOLDS, folds)
    return folds


def ledger_view(records: list[dict], executions: dict, panels: dict, as_of: date,
                reanchors: dict | None = None, params: dict | None = None,
                forced_exits: dict | None = None,
                position_events_path: str | Path | None = live.POSITION_EVENTS_PATH,
                stop_reentry_blocked: list[dict] | None = None
                ) -> tuple[dict, dict, dict]:
    """1차 체결 재생 + **2·3차 분할·손절·분할익절 정산** → (공개 상태, 보유, 정산 결과).

    1차만 보던 `replay`/`mark` 는 그대로 두고(호환), 실시간 운영은 정산 결과를 쓴다 —
    수량은 메모리에서만 다루고 커밋 JSON 에는 비중·상태만 남긴다(2.2.3).

    [v1.3.4 · P0-3 배선]
    - `max_sector` 는 params `risk.max_sector`(없으면 엔진 기본 25%) — 2·3차 체결도 같은 섹터 상한을 본다.
    - `position_events_path` 기본값은 `live.POSITION_EVENTS_PATH`(상대 경로 → 실행 루트의
      `data/ledger/position_events.jsonl`)라 실제 배치에서 2·3차·손절·익절 세션 이벤트가 **영속**된다.
      같은 `event_id` 는 다시 쓰지 않으므로 하루 두 번(발행 전·후) 불러도 중복이 없다.
      저장소 파일을 건드리면 안 되는 탐색·테스트 호출은 `None` 을 넘긴다.
    - `stop_reentry_blocked` 는 **1차 `settle` 이 실제로 막은 재진입**(P-1)이다. 2·3차 정산은 세션
      로그만 보므로 이번 정산에서 막힌 건을 스스로 알 수 없다 — 실행기가 넘겨야
      `health.stop_reentry_blocked_count` 가 2·3차 취소 수와 섞이지 않고 실제 값이 된다.
    """
    risk = (params or {}).get("risk") or {}
    max_sector = risk.get("max_sector")
    cap = float(max_sector) if max_sector is not None else live.MAX_SECTOR
    try:
        settlement = live.settle_positions(
            records, executions, reanchors, panels, as_of, params, forced_exits=forced_exits,
            max_sector=cap, position_events_path=position_events_path,
            stop_reentry_blocked=stop_reentry_blocked)
    except ValueError as exc:
        # append-only 충돌(같은 event_id 다른 내용)은 로그만 남기고 정산·발행은 계속한다.
        print(f"position_events 기록 실패: {exc}", file=sys.stderr)
        settlement = live.settle_positions(
            records, executions, reanchors, panels, as_of, params, forced_exits=forced_exits,
            max_sector=cap, position_events_path=None,
            stop_reentry_blocked=stop_reentry_blocked)
    public = live.mark_positions(settlement, panels, as_of)
    return public, settlement["holdings"], settlement


def run(args) -> int:
    root = Path(args.root).resolve()
    if root != Path.cwd():
        os.chdir(root)
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(KST).date()
    run_started_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = data_cutoff_at(as_of, args.mode)
    params = load_params(Path(args.params))
    params_version = str(params.get("version") or "params_v001")
    commit = _git_commit()
    history = ensure_params_history(params)
    folds = ensure_folds(as_of)
    watchlist_payload = _read_json(WATCHLIST)

    data = load_dataset(args, as_of, cutoff)
    diag = data.get("diagnostics") or {}
    diag.setdefault("degraded", [])

    panels = data.get("panels") or {}
    if not panels:
        # 가격이 하나도 없다 — 전일 데이터 유지(빈 화면을 배포하지 않는다, 2.7)
        # P2-1: 이 경로도 **상태를 남긴다**. 장애 실행이 아무 흔적을 남기지 않으면
        # 화면은 전일 정상 신호를 "방금 갱신된 데이터"처럼 보여 준다.
        prev = _read_json(SIGNALS) or {}
        outage_id = outage_id_for(as_of, args.mode, diag.get("degraded"))
        health_payload = write_health_state(
            as_of=as_of, mode=args.mode, data_status="outage", degraded=diag.get("degraded"),
            reason="price_panels_empty", target_session=target_session_for(
                datetime.now(timezone.utc).replace(microsecond=0)),
            extra={"outage_previous_as_of": prev.get("as_of"),
                   "outage_previous_status": prev.get("status"),
                   "kept_previous_signals": bool(prev)})
        _write_json(HEALTH, health_payload)
        _write_json(Path("data/quant_signals.degraded.json"),
                    {"as_of": as_of.isoformat(), "status": "degraded", "data_status": "outage",
                     "degraded": diag.get("degraded"), "previous_status": prev.get("status"),
                     "outage_id": outage_id, "last_success_at": health_payload.get("last_success_at"),
                     "data_cutoff_at": health_payload.get("data_cutoff_at"),
                     "target_session": health_payload.get("target_session"),
                     "reason": "price_panels_empty",
                     "note_ko": "가격 수집 실패 — 전일 데이터를 유지했다."})
        print(json.dumps({"status": "degraded", "data_status": "outage", "outage_id": outage_id,
                          "kept_previous": SIGNALS.exists(), "degraded": diag.get("degraded")},
                         ensure_ascii=False))
        # 장애 알림도 `outage_id` 로 **한 번만** 나간다(P2-1). 실패해도 배치는 그대로 끝난다.
        try:
            notify.evaluate_notifications(
                {"as_of": as_of.isoformat(), "status": "degraded", "signals": [], "blocked": [],
                 "position_checks": [], "diagnostics": {"degraded": diag.get("degraded")}},
                log_path=notify.DEFAULT_LOG, out_path=NOTIFICATIONS,
                health={**health_payload, "data_status": "outage", "outage_id": outage_id},
                outage_id=outage_id)
        except Exception as exc:  # noqa: BLE001 — 알림 실패가 장애 기록을 지우지 않는다
            print(f"알림 실패: {type(exc).__name__}", file=sys.stderr)
        # 감사 11: 워크플로가 장애를 **분기**할 수 있게 결과를 남긴다. 스냅샷이 없으니 아카이브 단계는
        # `snapshot_created=false` 로 건너뛰고, 상태 PUBLISH(health·알림·Pages 빌드)는 계속 간다.
        _write_github_output({
            "run_outcome": "outage", "status_written": True, "snapshot_created": False,
            "snapshot_state": "missing", "snapshot_id": "", "snapshot_id_short": "",
            "status": "degraded", "n_signals": 0, "n_blocked": 0,
            "data_status": "outage", "outage_id": outage_id,
        })
        return 0

    # 1) Ledger — 원장·체결 로드. **정산 전** 체결 상태를 따로 잡아 둔다(P0-2 페어 기준).
    records = load_records(RECORDS)
    executions = live.load_executions(EXECUTIONS)
    reanchors = live.load_reanchors(EXECUTIONS)
    executions_pre_settle = {sid: dict(event) for sid, event in executions.items()}

    # 1-a) 토론 편입 검증 — **정산보다 먼저**(P0-2). 편입은 "그 판단이 주문 전에 확정됐나"를
    #      묻는 절차이므로, 같은 배치에서 방금 체결된 주문을 보고 소급 페어를 만들면 안 된다.
    #      그래서 ① 편입 판정을 정산 앞으로 옮기고 ② 정산 전 체결 스냅샷을 기준으로 쓴다.
    regime_view = classify_regime(data.get("macro"), data.get("macro_previous"))
    debate_doc = debate_mod.load_reports(DEBATE_REPORTS)
    # 감사 5331ab7-01: 신뢰 상한은 **이 보고서 파일을 실제로 읽은 시각**이다(배치 시작보다 이르지 않다).
    #   배치가 오래 걸려 그 사이 도착한 보고서를 읽었다면 "받은 시각"은 지금이다.
    debate_received_at = datetime.now(timezone.utc).replace(microsecond=0)
    debate_kwargs = {}
    if _supports(debate_mod.reconcile_debates, "now"):
        # 검증 시각 = **이번 배치의 실제 수신·검증 시각**(감사 02) — 보고서 자기신고에만 맡기지 않는다.
        debate_kwargs["now"] = debate_received_at
    if _supports(debate_mod.reconcile_debates, "require_receipt"):
        # 바이트 영수증(`report_sha256`)이 없으면 편입하지 않는다(트랙 1 계약).
        debate_kwargs["require_receipt"] = True
    debate_result = debate_mod.reconcile_debates(
        debate_doc, records, executions_pre_settle, event_dir=DEBATE_EVENTS, params=params,
        regime=regime_view.regime, as_of=as_of, **debate_kwargs)
    # [5331ab7-11] 알림 재시도 재판정용 **원장 이벤트** — 이번 배치가 방금 확정한 편입까지 포함한다.
    #   문서 단위(`{signal_id, adopted, events}`)가 아니라 **이벤트 행**으로 넘겨야 `eligible` 을 읽는다.
    debate_events = {str(sid): list(doc.get("events") or [])
                     for sid, doc in (debate_mod.load_events(DEBATE_EVENTS) or {}).items()
                     if isinstance(doc, dict)}
    # 감사 07: ticker 단독이 아니라 **신호 정체성(signal_id, snapshot_id)** 으로 찾는다 —
    # 구 스냅샷의 토론이 오늘 판단을 바꾸지 않게. ticker 폴백은 통합 전 호출부용이다.
    debate_by_key = debate_index(debate_result["by_signal"], records)

    # 2) 정산 **준비** — 1차 체결을 계산만 해 두고(기록 전), 오늘 보유·섹터 비중을 확정한다.
    #    P0-3 배선: 1차 체결의 섹터 캡은 **정산 전 보유**까지 합산한다. 정산 전 계좌를 한 번 읽어
    #    섹터별 평가액(달러)을 넘기지 않으면, 같은 날 같은 섹터 5% 주문만 세고 기존 보유는 놓친다.
    #    실제 기록은 발행·대체·차단 레코드가 확정된 **뒤**(7단계)에 한다 — 감사 §10②(대체 취소 누락)와
    #    감사 01-d(같은 배치 차단이 시가 체결을 못 막음)의 뿌리가 "정산이 발행 확정보다 먼저"였기 때문이다.
    held_pre = ledger_view(records, executions, panels, as_of, reanchors, params,
                           position_events_path=None)[0]
    # 섬터 캘의 분모는 **그 시점 순자산**이다 — 2·3차(`settle_positions` 의 `equity_at`)와 같은 기준.
    #   배선 전에는 `equity_base` 기본값(초기 100만 달러)이 그대로 쓰여 자산이 변해도 상한이 안 움직였다.
    equity_base = settle_equity_base(held_pre)
    settle_kwargs = {}
    if _supports(live.settle, "now"):
        # 정산 시각 = 배치 시작 시각. 취소·동결 사유의 시각을 **실행한 시각**으로 남긴다(감사 §10①).
        settle_kwargs["now"] = run_started_at
    if _supports(live.settle, "position_events_path"):
        # P-1: 세션 로그(S5 손절)를 읽어 **손절 당일 신규 1차 진입**을 막는다 — 경로만 넘기면 된다.
        settle_kwargs["position_events_path"] = live.POSITION_EVENTS_PATH
    if _supports(live.settle, "equity_base"):
        settle_kwargs["equity_base"] = equity_base
    probe = live.settle(records, executions, panels, as_of, slippage_bps=args.slippage_bps,
                        max_sector=sector_cap(params),
                        sector_exposure=sector_exposure_dollars(held_pre), **settle_kwargs)
    probe_executions = dict(executions)
    for event in probe["new_events"]:
        probe_executions[str(event.get("signal_id"))] = event
    portfolio_before, held_before, _ = ledger_view(
        records, probe_executions, panels, as_of, reanchors, params, position_events_path=None)
    watch = ({r["ticker"] for r in (watchlist_payload or {}).get("watchlist") or []}
             if watchlist_payload else None)
    n_universe_before = len(data.get("universe") or [])
    if watch is not None:
        data["universe"] = [r for r in data["universe"] if r["ticker"] in watch or r["ticker"] in held_before]

    # 3) 발행 시각 — **수집·정산이 끝난 지금**. 실행 시각으로 잡지 않는다(감사 01-b).
    #    `run_started_at`(배치 시작)과 `signal_published_at`(결과 확정 직전)은 다른 값이다.
    times = publish_times(as_of, args.mode)

    # 4) 판단 입력 확정 — 워치리스트 필터·보유·섹터 비중·target_session 까지 끝난 상태를 고정한다(감사 02-b).
    #    스냅샷은 이 **최종 입력**으로 만들고(입력 지문 기준), 재생은 같은 값을 그대로 읽는다.
    frames = {f"prices_{ticker}": panel.frame for ticker, panel in sorted(panels.items())}
    inputs = {k: v for k, v in data.items() if k != "panels"}
    held = list(held_before.values())
    sector_weights = portfolio_before.get("sector_weights") or {}
    watchlist_filter = {
        "source": "data/watchlist/current.json" if watch is not None else "universe(주간 스크린 전)",
        "n_before": int(n_universe_before),
        "n_after": len(data.get("universe") or []),
    }
    fingerprint_inputs = {
        **inputs,
        "held": held,
        "sector_weights": sector_weights,
        "params": params,
        "watchlist_filter": watchlist_filter,
        # 주문 대상 세션이 다르면 같은 데이터라도 **다른 발행**이다(늦은 재실행 → 새 회차).
        "target_session": times["target_session"],
    }
    try:
        inputs_hash = inputs_fingerprint(frames, fingerprint_inputs, cutoff, params_version)
    except Exception as exc:  # noqa: BLE001 — 지문 실패가 발행을 막지는 않는다(회차는 001 고정)
        inputs_hash = ""
        diag["inputs_hash_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    data["decision"] = {
        "mode": args.mode,
        "run_started_at": _iso(run_started_at),
        "data_cutoff_at": times["data_cutoff_at"],
        "signal_published_at": times["signal_published_at"],
        "signal_published_planned_at": _iso(times["signal_published_planned_at"]),
        "order_cutoff_at": times["order_cutoff_at"],
        "target_session": times["target_session"],
        "held": [{k: h.get(k) for k in ("ticker", "sector", "stop_price", "trailing_high", "initial_stop")}
                 for h in held],
        "sector_weights": sector_weights,
        "watchlist_filter": watchlist_filter,
        "params": params,
        "params_version": params_version,
        "code_commit": commit,
        "inputs_hash": inputs_hash,
        "cutoff_rule": "available_at <= decision_at",
    }
    snapshot_inputs = {k: v for k, v in data.items() if k != "panels"}
    try:
        snapshot_id, snapshot_state = ensure_snapshot(
            SNAPSHOTS, as_of=as_of, cutoff=cutoff, frames=frames, inputs=snapshot_inputs,
            extra={"mode": args.mode, "source": data.get("source"),
                   "params_version": params_version, "code_commit": commit,
                   "target_session": times["target_session"],
                   "cutoff_rule": "available_at <= decision_at"},
            inputs_hash=inputs_hash)
    except Exception as exc:  # noqa: BLE001 — 스냅샷 실패는 신호 발행을 막지 않는다
        snapshot_id, snapshot_state = "sha256:" + "0" * 64, "failed"
        diag["snapshot_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    diag["snapshot"] = {"id": snapshot_id, "state": snapshot_state, "inputs_hash": inputs_hash}
    short = snapshot_id.replace("sha256:", "")[:12]

    # 5) 신호 발행
    status = "normal" if not diag.get("degraded") else "degraded"
    if data.get("source") in ("fixtures", "sample"):
        status = "sample"
    payload = build_payload(
        data, as_of=as_of, mode=args.mode, params=params, params_version=params_version,
        code_commit=commit, snapshot_id=snapshot_id, status=status, three_times=times,
        held=list(held_before.values()), sector_weights=portfolio_before.get("sector_weights") or {},
        source=data.get("source"), inputs_hash=inputs_hash or None, debate=debate_by_key,
    )
    payload["diagnostics"] = diag
    payload["watchlist_source"] = watchlist_filter["source"]
    payload["run_started_at"] = _iso(run_started_at)
    payload["inputs_hash"] = inputs_hash

    # 6) 발행 회차 확정 → append → **원장에 기록된 발행본 그대로 배포**(감사 02)
    appended, skipped, duplicated, deployed = [], [], [], []
    for record in payload["signals"]:
        try:
            attach_target_session(record, times["target_session"],
                                  times.get("signal_published_planned_at"))
        except RecordError as exc:
            skipped.append({"signal_id": record["signal_id"], "reason": str(exc)[:120]})
            continue
        release = resolve_release(record, records)
        record["signal_id"] = release["signal_id"]
        record["revision"] = int(release["revision"])
        record["supersedes"] = release["supersedes"]
        if release["duplicate"]:
            # 같은 입력 지문의 재실행 — 이미 기록된 발행본을 그대로 배포한다(화면 == 원장).
            prior = next((r for r in records if str(r.get("signal_id")) == release["signal_id"]), None)
            if prior is not None:
                duplicated.append({"signal_id": release["signal_id"], "reused_from": release["reused_from"]})
                deployed.append(copy.deepcopy(prior))
                continue
        try:
            append_immutable(RECORDS, record)
        except RecordError as exc:
            skipped.append({"signal_id": record["signal_id"], "reason": str(exc)[:120]})
            continue
        records.append(record)
        appended.append(record["signal_id"])
        deployed.append(record)
    payload["signals"] = deployed
    if skipped:
        payload["diagnostics"]["record_skips"] = skipped
    if duplicated:
        payload["diagnostics"]["record_duplicates"] = duplicated

    # 6-b) 차단(BUY→blocked)도 원장에서 **이전 BUY 를 대체**한다(감사 01-d).
    #     차단은 발행 신호가 아니라 레코드가 없었고, 그래서 오전 BUY 가 그대로 최신으로 남아 시가에
    #     체결될 수 있었다. 트랙 1의 `record.make_blocked_supersede_record` 가 있으면 그 자리에서 남긴다.
    blocked_records = _append_blocked_supersedes(
        records, payload.get("blocked") or [], times=times, as_of=as_of, inputs_hash=inputs_hash,
        params_version=params_version, snapshot_id=snapshot_id, code_commit=commit, mode=args.mode)
    if blocked_records:
        payload["diagnostics"]["blocked_supersedes"] = [r.get("signal_id") for r in blocked_records]

    # 7) 정산 — 발행·대체·차단 레코드가 **확정된 뒤** 1차 체결·취소를 계산하고 기록한다.
    #    [감사 §10②] 정산이 발행보다 먼저 돌면, 같은 실행에서 만들어진 `-002→-003` 대체의 취소가
    #    다음 실행으로 밀려 **과거 시각으로 back-date** 되었다. 이제 새 회차가 이미 원장에 있으므로
    #    취소가 그 자리에서 기록된다.
    #    [감사 01-d] 같은 배치의 차단(`blocked`)도 `settle` 에 넘겨 시가 체결을 막는다(이전 as_of 의 BUY 가
    #    오늘 세션에 체결되는 경우는 대체 레코드만으로 막을 수 없다).
    newly_appended = {str(sid) for sid in appended}
    superseding_ids = {str(r.get("signal_id")) for r in deployed
                       if str(r.get("signal_id")) in newly_appended and r.get("supersedes")}
    superseding_ids |= {str(r.get("signal_id")) for r in blocked_records}
    blocked_map = _blocked_plans(payload.get("blocked") or [],
                                 target_session=times["target_session"],
                                 observed_at=times["signal_published_at"])
    if blocked_map and _supports(live.settle, "blocked"):
        settle_kwargs["blocked"] = blocked_map
    # [v1.3.6 · 중요 04 · 당일 S5 잠금] 같은 실행에서 관측된 손절을 `settle` 에도 넘긴다 —
    #   시가 **이전** 확정분은 그 세션 재진입을 막고, 종가 판정(시가 이후 관측)은 잠금을 다음
    #   세션으로 넘긴다(`stop_locks_next`). 이 값을 넘기지 않으면 "같은 실행의 S5"가 어느 쪽에도
    #   닿지 않아 세션 로그가 이미 있는지에 따라 결과가 갈렸다(감사 04).
    stop_plans = _stop_exit_plans(payload.get("position_checks") or [],
                                  observed_at=times["signal_published_at"],
                                  target_session=times["target_session"])
    if stop_plans and _supports(live.settle, "stop_exits"):
        settle_kwargs["stop_exits"] = stop_plans
    # 감사 06-(e): 자동 하향이 켜져 있으면 **화면은 보류(`hold_cash`)인데 원장은 체결**(`buy_filled`)이었다.
    #   `reconcile`/`actions_for` 가 자동 하향을 적용한 신호를 원장에도 넘겨 시가 체결을 막는다.
    #   기록은 `hold_cash`(체결 아님)이고, 사유는 `AUTO_DOWNGRADE` 로 남긴다.
    holds = debate_holds_for(debate_result, params)
    if holds and _supports(live.settle, "debate_holds"):
        settle_kwargs["debate_holds"] = holds
    settlement = live.settle(records, executions, panels, as_of, slippage_bps=args.slippage_bps,
                             max_sector=sector_cap(params),
                             sector_exposure=sector_exposure_dollars(held_pre), **settle_kwargs)
    for event in settlement["new_events"]:
        sid = str(event.get("signal_id"))
        if sid in executions:
            continue
        live.write_execution(event, EXECUTIONS)
        live.append_full_event(event, None, EXECUTIONS)
        executions[sid] = event
    for event in settlement["reanchors"]:
        live.write_reanchor(event, EXECUTIONS)
        reanchors[event["signal_id"]] = event
    # P0-3 배선: 동결 전 WATCH·차단으로 취소된 1차 주문도 **세션 이벤트 로그**에 남긴다.
    #   (`settled` 에만 있으면 콘솔 출력으로 사라진다 — 재생·감사 근거가 남지 않는다.)
    order_cancellations = settlement.get("cancellations") or []
    if order_cancellations:
        # 감사 R14#4: 같은 `event_id` 에 다른 내용이 생기면 여기서 ValueError 가 난다.
        #   발행 의도는 이미 기록됐으므로 **배치를 죽이지 않고** 경고만 남긴다(다음 실행이 다시 시도).
        try:
            live.append_position_events(
                live.position_event_rows([], order_cancellations, as_of), live.POSITION_EVENTS_PATH)
        except ValueError as exc:
            print(f"취소 이벤트 기록 실패(배치는 계속): {exc}", file=sys.stderr)
            diag["position_events_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    if superseding_ids:
        diag["supersession_cancellations"] = len(
            [c for c in order_cancellations if str(c.get("superseded_by")) in superseding_ids])

    # 8) 계좌 — S3/S5/EXIT 판정이 있으면 같은 봉에서 전량 청산 + 잔여 분할 취소(3.5)
    # [v1.3.6 · 중요 04] 매도 의도의 **관측 시각**(발행 시각)을 함께 넘긴다 — 종가 판정으로 만들어진
    #   S3/TRIM/S5 를 같은 날 시가로 되돌려 체결하던 경로를 막는다(`_forced_exit_plan` 새 계약).
    checks = payload.get("position_checks") or []
    forced_exit_reasons = _forced_exits(checks)          # 화면·health 용 `{ticker: 사유}`
    forced_exits = _forced_exits(checks, observed_at=times["signal_published_at"])  # 원장 계약
    records = load_records(RECORDS)
    portfolio_public, held_after, settlement_final = ledger_view(
        records, executions, panels, as_of, reanchors, params, forced_exits=forced_exits,
        stop_reentry_blocked=settlement.get("stop_reentry_blocked"))
    live.append_portfolio_history(portfolio_public, PORTFOLIO)
    first_entry = _first_entry_session(executions)
    portfolio_public["avg_exposure"] = live.avg_exposure_from_history(
        live.read_portfolio_history(PORTFOLIO), first_entry, as_of)
    benchmark_panel = panels.get("SPY")
    trailing = live.benchmark_trailing_returns(panels)
    # P0-4: Paper 채점은 **홀드아웃과 무관**하다. `holdout_start` 를 넘기면 이미 만기 봉이 관측된
    #   신호까지 `truncated` 로 잘려 성적표·파일럿 표본에서 빠졌다(감사 P0-4). 보정 코퍼스 적격은
    #   `window_for(as_of)` 기준으로 따로 관리한다 — 성적표는 `mature`/`data_missing` 만 본다.
    graded = live.grade_all(records, executions, panels, as_of,
                            holdout_start=None,
                            benchmark_panel=benchmark_panel)
    pilot = evaluate_pilot(records, graded, executions=executions,
                           portfolio={**portfolio_public,
                                      "total_return": (portfolio_public.get("equity_index") or 1.0) - 1.0},
                           benchmark_return=live.benchmark_period_return(benchmark_panel, first_entry, as_of),
                           risk_free=args.risk_free)
    write_pilot_eval(pilot, PILOT)
    holdout_status = holdout_status_for(as_of)
    summary = live.summary(graded, portfolio_public, pilot_summary_of(pilot), folds=folds,
                           params_history=history, benchmark_connected=benchmark_panel is not None)
    summary["benchmark_trailing"] = trailing  # 참고 표시(배치 실행일 기준) — 채점에는 쓰지 않는다
    summary["holdout"] = holdout_status
    # 보정 코퍼스 적격 창은 **따로** 기록한다 — Paper 성적표의 `truncated` 판정에 쓰지 않는다(P0-4).
    calibration_window = window_for(as_of)
    summary["calibration_window"] = {
        "holdout_start": calibration_window.holdout_start.isoformat(),
        "holdout_end": calibration_window.holdout_end.isoformat(),
        "months": calibration_window.months,
        "note_ko": "Paper 채점은 홀드아웃을 보지 않는다 — 이 창은 보정 코퍼스 적격에만 쓴다.",
    }
    summary["graded_sample"] = graded[:20]
    summary["settlement"] = {k: settlement[k] for k in ("settled", "pending")}
    summary["settlement"]["order_cancellations"] = len(order_cancellations)

    # 8) 토론 A/B 비교 → 자동 하향 판정 → 이력(params_history) · 비교 JSON
    regime_lookup = {str(r.get("signal_id")): regime_view.regime for r in records
                     if str(r.get("as_of")) == as_of.isoformat()}
    # P0-2: **편입 검증보다 먼저 체결된** 주문의 페어는 소급이다 — 표시는 남기고 비교에서는 뺀다.
    #   5331ab7-02: 기준 시각은 보고서 배열의 마지막이 아니라 **채택된 보고서**의 검증 시각이다.
    adopted_map = debate_result.get("adopted") if isinstance(debate_result, dict) else None
    comparison_input, retroactive = drop_retroactive_pairs(
        debate_result["by_signal"], executions, debate_doc, adopted=adopted_map)
    # 감사 5331ab7-05: 섹터 캡에 막힌 주문 의도를 **구조화 사유**로 넘긴다 — 두 계좌가 독립 평가한다.
    sector_cap_blocked = {str(row.get("signal_id")): "SECTOR_CAP"
                          for row in (settlement.get("settled") or [])
                          if str(row.get("reason") or "") == "SECTOR_CAP"
                          and row.get("signal_id")}
    comparison_kwargs = {}
    if _supports(debate_mod.compute_debate_comparison, "intent_blocked"):
        comparison_kwargs["intent_blocked"] = sector_cap_blocked
    comparison = debate_mod.compute_debate_comparison(
        comparison_input, records, executions, panels, as_of, params=params,
        regime_lookup=regime_lookup, **comparison_kwargs)
    comparison["counts"] = {"reports": debate_result["n_reports"],
                             "eligible": debate_result["n_eligible"],
                             "late": debate_result["n_late"],
                             "invalid": debate_result["n_invalid"],
                             "superseded": debate_result.get("n_superseded", 0),
                             "retroactive_dropped": len(retroactive)}
    comparison["retroactive_dropped"] = len(retroactive)
    if retroactive:
        comparison["retroactive_dropped_pairs"] = retroactive[:20]
    auto_eval = debate_mod.evaluate_auto_downgrade_eligibility(comparison, history)
    debate_mod.record_auto_downgrade_evaluation(PARAMS_HISTORY, comparison, params_version)
    debate_mod.write_comparison(comparison, DEBATE_COMPARISON)
    _write_json(Path("data/ledger/debate_summary.json"), {
        k: debate_result[k] for k in ("schema_version", "as_of", "generated_at", "received_at",
                                      "freeze_buffer_seconds", "auto_downgrade_enabled", "n_reports",
                                      "n_eligible", "n_late", "n_invalid", "n_superseded",
                                      "adopted", "superseded", "by_signal")
        if k in debate_result
    } | {"auto_downgrade": auto_eval})
    summary["debate_effect"] = debate_mod.comparison_summary(comparison)
    summary["debate"] = {"status": "ok" if debate_result["n_reports"] else "none",
                         "generated_at": debate_result["generated_at"],
                         "counts": comparison["counts"],
                         "auto_downgrade": auto_eval}

    payload["ledger_summary"] = summary
    payload["target_session"] = times["target_session"]
    payload["signal_published_planned_at"] = _iso(times["signal_published_planned_at"])
    payload["portfolio"] = {
        "as_of": portfolio_public["as_of"],
        "cash_pct": portfolio_public["cash_pct"],
        "invested_pct": portfolio_public["invested_pct"],
        "avg_exposure": portfolio_public.get("avg_exposure"),
        "equity_index": portfolio_public.get("equity_index"),
        # 분할·익절 상세는 `data/ledger/portfolio/state.json` 에만 넣는다 —
        # `quant_signals.json` 의 portfolio 는 계약 스키마가 좁게 고정돼 있다(부록 B).
        "positions": [{"ticker": p["ticker"], "weight": p["weight"], "sector": p["sector"],
                       "opened_at": p["opened_at"]} for p in portfolio_public["positions"]],
    }
    payload["debate"] = {"status": summary["debate"]["status"],
                         "generated_at": summary["debate"]["generated_at"],
                         "counts": summary["debate"]["counts"],
                         "divergent_pairs": comparison["divergent_pairs"],
                         "verdict": comparison["verdict"],
                         "auto_downgrade_enabled": auto_eval["current"]}
    # P2-1: 실행 상태부(data_status·last_attempt_at·last_success_at·outage_id) — 발행 파일과
    # `data/health.json` 이 **같은 값**을 쓰도록 여기서 한 번만 계산한다.
    data_status = "degraded" if diag.get("degraded") else "ok"
    run_state = write_health_state(
        as_of=as_of, mode=args.mode, data_status=data_status, degraded=diag.get("degraded"),
        reason="partial_sources" if diag.get("degraded") else None,
        target_session=date.fromisoformat(times["target_session"]))
    run_state["last_run_at"] = _iso(run_started_at)   # `last_run_at` 은 **배치 시작** 시각이다
    payload["health"] = {"stop_reentry_blocked_count":
                         settlement_final["health"]["stop_reentry_blocked_count"],
                         "positions_open": settlement_final["health"]["positions_open"],
                         "tranches_pending": settlement_final["health"]["tranches_pending"],
                         # 실행 상태부(P2-1) — 화면의 "판단 보류 / 마지막 성공" 배너 입력
                         "data_status": data_status,
                         "last_attempt_at": run_state.get("last_attempt_at"),
                         "last_success_at": run_state.get("last_success_at"),
                         "data_cutoff_at": run_state.get("data_cutoff_at"),
                         "target_session": run_state.get("target_session"),
                         "outage_id": run_state.get("outage_id")}
    payload["pilot_eval"] = pilot_summary_of(pilot)
    payload["prices_as_of"] = _price_as_of(panels)
    _write_json(Path("data/walkforward/stress.json"), stress_payload())

    # 9) 레이더(13F) — 없으면 빈 정상 상태
    _write_json(RADAR, data.get("radar") or {
        "as_of": as_of.isoformat(),
        "watermark_ko": "45일 전 분기말 기준",
        "period_end": None, "lag_days": 45, "stale": True, "stale_days": None,
        "filers": [], "n_filers": 0, "counts": {"new": 0, "increase": 0, "reduce": 0, "exit": 0},
        "watchlist_overlap": [],
        "note_ko": "13F 수집 이전 — 레이더는 비어 있는 것이 정상이다(weekly-screen 이 채운다).",
    })
    _write_json(LIMITS, _limits(data, as_of))
    live.write_health(settlement_final["health"], as_of, status=status,
                      degraded=diag.get("degraded"),
                      extra={"forced_exits": forced_exit_reasons,
                             "cash_pct": portfolio_public["cash_pct"],
                             "order_cancellations_count": len(order_cancellations),
                             "order_cancellations": order_cancellations[:20],
                             **{k: run_state.get(k) for k in (
                                 "data_status", "last_run_at", "last_attempt_at", "last_success_at",
                                 "data_cutoff_at", "target_session", "outage_id", "reason")}})

    # 10) 계약 검증 → 실패하면 이전 파일을 유지한다(2.2.2)
    #    발행 지연도 정직하게 남긴다 — 확정 시각이 주문 마감을 넘겼는지(01-b).
    written_at = datetime.now(timezone.utc)
    published = datetime.fromisoformat(str(payload["signal_published_at"]).replace("Z", "+00:00"))
    order_cutoff = datetime.fromisoformat(str(payload["order_cutoff_at"]).replace("Z", "+00:00"))
    payload["publish_lag_seconds"] = round((written_at - published).total_seconds(), 1)
    payload["published_after_order_cutoff"] = bool(written_at >= order_cutoff)
    try:
        validate_quant_signals(payload)
    except QuantSignalSchemaError as exc:
        _write_json(Path("data/quant_signals.rejected.json"),
                    {"errors": exc.errors[:50], "as_of": as_of.isoformat()})
        print(json.dumps({"status": "rejected", "errors": exc.errors[:8]}, ensure_ascii=False), file=sys.stderr)
        _write_github_output({"run_outcome": "rejected",
                              # health.json 은 이미 기록됐다 — 계약 검증에서 막혔을 뿐이다.
                              "status_written": HEALTH.exists(),
                              "snapshot_created": bool(snapshot_state in ("created", "reused")),
                              "snapshot_state": snapshot_state, "status": "rejected"})
        return 1

    # 11) 알림 — 중복 0건(같은 signal_id·event_key 는 다시 보내지 않는다)
    #    §10⑥: 정산 health(`positions_open`·`tranche*`·`stop_*`)만 넘기면 `data_status` 가 `None` 이 되어
    #    `notifications_latest.json` 의 실행 상태가 빈 값이 되었다. **실행 상태부를 합쳐** 넘긴다.
    notify_health = {
        **settlement_final["health"],
        "data_status": data_status,
        "outage_id": run_state.get("outage_id"),
        "last_attempt_at": run_state.get("last_attempt_at"),
        "last_success_at": run_state.get("last_success_at"),
        "data_cutoff_at": run_state.get("data_cutoff_at"),
        "target_session": run_state.get("target_session"),
    }
    notification = notify.evaluate_notifications(
        payload, log_path=notify.DEFAULT_LOG, out_path=NOTIFICATIONS,
        debate=debate_result, health=notify_health, outage_id=run_state.get("outage_id"),
        # [5331ab7-11] 재시도 전 **원장 이벤트 기반 재판정** — 이미 적격 토론이 편입된 "토론 실행 권장"은
        #   재전송하지 않고 `suppressed` 로 닫는다. `debate_events/` 를 넘기지 않으면 그 재판정이
        #   배치 요약(`debate=`)만 보고 판단해 하루 늦은 권장을 다시 보낼 수 있다.
        debate_events=debate_events)
    payload["notifications"] = {
        "n_new": notification["n_new"],
        "n_suppressed": notification["n_suppressed"],
        # [5331ab7-11] 채널별 재시도 장부 — 화면·원장이 "다음 실행에서 다시 보낼 알림"을 알 수 있게 한다.
        "n_retried": notification.get("n_retried", 0),
        "n_retry_suppressed": notification.get("n_retry_suppressed", 0),
        "retry_pending": notification.get("retry_pending") or [],
        "events": [{k: e.get(k) for k in ("event_key", "type", "severity",
                                          "message_ko", "ticker", "signal_id")}
                   for e in notification["new"]]}
    summary["notifications"] = payload["notifications"]

    _write_json(SIGNALS, payload)
    if not args.no_redact:
        redact([SIGNALS, RECORDS])
    live.write_public_state(portfolio_public, PORTFOLIO)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        _write_github_output({
            "snapshot_id": snapshot_id, "snapshot_id_short": short, "status": status,
            "n_signals": len(payload["signals"]), "n_blocked": len(payload["blocked"]),
            # 감사 11 — 워크플로 분기 근거. `snapshot_created` 은 "패키징할 스냅샷이 있는가"이며
            # `snapshot_state`(created|reused|failed)가 이유를 구분한다.
            "run_outcome": "outage" if data_status == "outage" else "normal",
            "status_written": True,
            "snapshot_created": snapshot_state in ("created", "reused"),
            "snapshot_state": snapshot_state,
            "data_status": data_status,
            "outage_id": run_state.get("outage_id") or "",
        })

    print(json.dumps({
        "as_of": as_of.isoformat(), "mode": args.mode, "status": status,
        "run_outcome": "outage" if data_status == "outage" else "normal",
        "snapshot_id": snapshot_id, "snapshot_id_short": short,
        "signals": [s["signal_id"] for s in payload["signals"]],
        "blocked": [b["ticker"] for b in payload["blocked"]],
        "settled": settlement["settled"], "pending": len(settlement["pending"]),
        "order_cancellations": len(order_cancellations),
        "records_appended": len(appended), "portfolio": {
            "cash_pct": portfolio_public["cash_pct"], "invested_pct": portfolio_public["invested_pct"],
            "n_positions": portfolio_public["n_positions"]},
        "pilot": pilot["verdict"], "degraded": diag.get("degraded"),
        "debate": {"reports": debate_result["n_reports"], "eligible": debate_result["n_eligible"],
                   "late": debate_result["n_late"], "invalid": debate_result["n_invalid"],
                   "divergent_pairs": comparison["divergent_pairs"],
                   "auto_downgrade_enabled": auto_eval["current"]},
        "notifications": {"new": notification["n_new"], "suppressed": notification["n_suppressed"]},
    }, ensure_ascii=False))
    return 0


def _ts(value):
    """ISO 문자열 → tz 있는 datetime(없으면 None)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def drop_retroactive_pairs(by_signal: dict, executions: dict, reports_doc: dict | None,
                           adopted: dict | None = None) -> tuple[dict, list[dict]]:
    """P0-2: **편입 검증 시각보다 먼저 체결된** 주문의 페어는 소급이므로 비교에서 제외한다.

    편입 자격은 "마감 전에 끝난 토론인가"만 본다. 그래서 이미 시가 체결이 끝난 주문에
    "토론 계좌는 현금을 들고 있었다"는 페어가 뒤늦게 붙을 수 있다 — 그런 페어는
    `quant_action` 이 사후에 정해진 값이라 A/B 비교의 증거가 될 수 없다.

    [v1.3.6 · 감사 5331ab7-02] 기준 시각은 **실제 채택된 보고서의 검증 시각**이다.
    예전에는 신호별 보고서 배열의 **마지막** 검증 시각을 써서, 동결 전에 적격으로 붙은 `-01` 페어를
    지연 `-02` 하나 때문에 통째로 버렸다. 이제는 ① `by_signal` 행의 `report_validated_at`(채택 결과)·
    ② `adopted` 매핑·③ 보고서 문서에서 채택본을 찾은 순서로 정한다. 문서에 회차가 하나뿐이면
    예전과 같은 결과다(하위 호환).

    반환: (비교용 by_signal, 제외 목록). 제외한 건도 화면에서는 `retroactive: true` 로 남긴다.
    """
    adopted = {str(k): str(v) for k, v in (adopted or {}).items() if v}
    rows = (reports_doc or {}).get("reports") or []
    grouped: dict[str, list[dict]] = {}
    for rep in rows:
        sid = str(rep.get("signal_id") or "")
        if sid:
            grouped.setdefault(sid, []).append(rep)

    def _validated_of(rep: dict | None):
        rep = rep or {}
        return _ts((rep.get("validation") or {}).get("report_validated_at")) or _ts(rep.get("completed_at"))

    validated: dict[str, object] = {}
    for sid, candidates in grouped.items():
        chosen = None
        wanted = adopted.get(str(sid))
        if wanted:
            chosen = next((rep for rep in candidates
                           if str(rep.get("report_id")) == wanted), None)
        if chosen is None:
            # 채택본을 모를 때는 **가장 먼저 검증된** 보고서(선착순 채택 규칙)를 기준으로 본다.
            with_time = [rep for rep in candidates if _validated_of(rep) is not None]
            chosen = min(with_time, key=_validated_of) if with_time else candidates[0]
        validated[sid] = _validated_of(chosen)
    # 채택 결과를 아는 `by_signal` 행이 문서보다 **우선**한다(원장이 확정한 값).
    for sid, row in (by_signal or {}).items():
        if isinstance(row, dict) and row.get("report_validated_at"):
            validated[str(sid)] = _ts(row["report_validated_at"])
    keep: dict[str, dict] = {}
    dropped: list[dict] = []
    for sid, row in (by_signal or {}).items():
        execution = (executions or {}).get(sid) or {}
        filled = _ts(execution.get("at") or execution.get("filled_at"))
        checked = validated.get(str(sid))
        if filled is not None and checked is not None and filled < checked:
            dropped.append({"signal_id": sid, "ticker": (row or {}).get("ticker"),
                            "execution_at": filled.isoformat(),
                            "report_validated_at": checked.isoformat(),
                            "reason": "FILLED_BEFORE_VALIDATION"})
            keep[sid] = {**row, "retroactive": True}
            continue
        keep[sid] = row
    return {sid: row for sid, row in keep.items() if not row.get("retroactive")}, dropped


def sector_cap(params: dict | None) -> float:
    """params `risk.max_sector` — 없으면 엔진 기본값(25%). `live.settle` 기본값과 같은 출처."""
    value = ((params or {}).get("risk") or {}).get("max_sector")
    return float(value) if value is not None else float(live.MAX_SECTOR)


def settle_equity_base(portfolio: dict | None, initial_cash: float = live.INITIAL_CASH) -> float:
    """1차 섬터 캘·비중의 분모 — **그 시점 순자산**(초기자본 × equity_index).

    2·3차 정산은 세션마다 `equity_at(day)`(현금 + 보유 평가액)를 쓴다. 1차 `settle` 도 같은 기준을
    받아야 한 배치 안에서 두 기준이 섞이지 않는다(감사 03-(3) · §14#2).
    """
    portfolio = portfolio or {}
    return float(initial_cash) * float(portfolio.get("equity_index") or 1.0)


def debate_holds_for(debate_result: dict | None, params: dict | None) -> dict[str, str]:
    """자동 하향(`auto_downgrade_enabled`)으로 **보류된** 신호 → `{signal_id|ticker: "AUTO_DOWNGRADE"}`.

    감사 06-(e): 화면(`hold_cash`)과 원장(`buy_filled`)이 갈렸다. `reconcile_debates` 가 적격으로
    붙인 보고서 중 `actions_for` 가 자동 하향을 적용한 건(`auto_downgrade_applied`)을 원장 `settle`
    에 넘겨, 시가에 사지 않고 `hold_cash` 로 남긴다. 자동 하향이 꺼져 있으면 항상 빈 dict 다.
    """
    auto = bool(((params or {}).get("debate") or {}).get("auto_downgrade_enabled", False))
    if not auto:
        return {}
    out: dict[str, str] = {}
    for sid, row in ((debate_result or {}).get("by_signal") or {}).items():
        if not isinstance(row, dict) or not row.get("eligible"):
            continue
        if not bool(row.get("auto_downgrade_applied")):
            continue
        # 신호 정체성으로 먼저 잠그고, 종목 폴백은 같은 티커의 **다른 회차**까지 막지 않도록 남기지 않는다.
        out[str(sid)] = "AUTO_DOWNGRADE"
    return out


def sector_exposure_dollars(portfolio: dict | None, initial_cash: float = live.INITIAL_CASH) -> dict[str, float]:
    """공개 비중 요약 → **섹터 평가액(달러)**. `live.settle(sector_exposure=...)` 의 분자다.

    `settle` 의 섹터 캡은 `max_sector × equity_base`(달러)와 비교하므로, 화면·원장에 남는 비중
    (`sector_weights`)을 그 시점 총자산(`initial_cash × equity_index`)으로 되돌려 넘긴다.
    """
    portfolio = portfolio or {}
    equity = float(initial_cash) * float(portfolio.get("equity_index") or 1.0)
    return {str(sector): round(float(weight) * equity, 4)
            for sector, weight in (portfolio.get("sector_weights") or {}).items() if sector}


def _forced_exit_reason(check: dict, supported: set[str]) -> str | None:
    """포지션 점검 1건 → 강제 청산 사유(`S3`/`S5`/`EXIT`/`TRIM`) 또는 없음."""
    verdict = str(check.get("verdict") or "").upper()
    triggers = check.get("triggers") or {}
    if check.get("cancel_pending_tranches") or verdict == "EXIT":
        return "S3" if triggers.get("S3") else ("S5" if triggers.get("S5") else "EXIT")
    if verdict == "TRIM" and "TRIM" in supported:
        return "TRIM"
    return None


def _forced_exits(checks: list[dict], observed_at: str | None = None) -> dict:
    """S1~S5/EXIT(+TRIM) 판정 → `{ticker: "S3"|"S5"|"EXIT"|"TRIM"}` — 같은 봉에서 잔여 분할을 취소한다(3.5).

    기획서 §3.5 의 `exit_score 60~74 → TRIM(1/3 익절)` 은 어떤 원장 경로에도 연결돼 있지 않았다(감사 14#1).
    그래서 TRIM 도 넘기되, **엔진이 TRIM 을 1/3 익절로 처리한다고 알릴 때만**(`live.FORCED_EXIT_REASONS`)
    넘긴다 — 지원 전에 넘기면 전량 청산으로 읽혀 매도 행동이 달라진다.

    [v1.3.6 · 중요 04] `observed_at`(그 판정을 관측한 **발행 시각**)을 주면 `_forced_exit_plan` 의
    새 계약(`{"reason", "observed_at"}`)으로 돌려준다. 종가 판정은 시가 이후 관측이므로 그 날 시가에
    체결되지 않고 다음 세션 시가로 넘어간다. 주지 않으면 구 계약(문자열 맵)이다 — 기존 호출·테스트 호환.
    """
    supported = _forced_exit_reasons()
    out: dict[str, object] = {}
    for check in checks or []:
        ticker = str(check.get("ticker"))
        reason = _forced_exit_reason(check, supported)
        if reason is None:
            continue
        out[ticker] = reason if observed_at is None else {"reason": reason, "observed_at": observed_at}
    return out


def _stop_exit_plans(checks: list[dict], *, observed_at: str | None = None,
                     target_session: str | None = None) -> list[dict]:
    """같은 실행에서 관측된 **손절(S5)** → `settle(stop_exits=...)` 입력(중요 04 · 당일 S5 잠금).

    `settle` 은 이 목록으로 ① 시가 이전 확정 손절이면 그 세션 재진입을 막고(`STOP_REENTRY_LOCK`)
    ② 시가 이후(종가 판정) 관측이면 잠금을 **다음 세션**으로 넘긴다(`stop_locks_next`).
    넘기지 않으면 같은 실행의 S5 가 세션 로그 유무에 따라 결과를 갈랐다(감사 04).
    """
    plans: list[dict] = []
    for check in checks or []:
        if str(check.get("verdict") or "").upper() != "EXIT":
            continue
        triggers = check.get("triggers") or {}
        if not triggers.get("S5"):
            continue
        ticker = str(check.get("ticker") or "")
        if not ticker:
            continue
        plan = {"ticker": ticker, "reason": "S5", "observed_at": observed_at}
        if target_session:
            plan["target_session"] = target_session
        plans.append(plan)
    return plans


def _blocked_plans(rows: list[dict], *, target_session: str | None = None,
                   observed_at: str | None = None) -> dict[str, dict]:
    """차단 목록 → `settle(blocked=...)` 의 **새 계약**(치명 03).

    `{"reason", "signal_id", "target_session", "observed_at"}` 로 넘겨야 ① 다른 세션용 차단이
    같은 종목의 오늘 슬롯을 막지 않고 ② 장중·종가에 관측된 차단이 이미 동결·체결된 시가 주문을
    `no_action@시가` 로 **소급해 뒤집지 않는다**. 종전에는 `{ticker: 사유}` 뿐이라 둘 다 구분할
    수 없었다(감사 재현 03-C·03-D).
    """
    out: dict[str, dict] = {}
    for row in rows or []:
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        entry = {"reason": str(row.get("reason") or "BLOCKED"),
                 "signal_id": row.get("signal_id"),
                 "target_session": target_session,
                 "observed_at": observed_at}
        out[ticker] = entry
    return out


def _live_buy_to_supersede(records: list[dict], ticker: str, as_of: date) -> dict | None:
    """같은 날 그 종목의 **살아 있는** BUY 발행본 — 차단이 대체해야 할 대상."""
    superseded = record_mod.superseded_ids(records)
    candidates = [r for r in records or []
                  if str(r.get("ticker")) == ticker and str(r.get("as_of"))[:10] == as_of.isoformat()
                  and str(r.get("quant_intent")) == "pending_buy"
                  and str(r.get("signal_id")) not in superseded]
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: str(r.get("signal_published_at") or ""))[-1]


def _append_blocked_supersedes(records, blocked_rows, *, times: dict, as_of: date, inputs_hash: str,
                               params_version: str, snapshot_id: str, code_commit: str,
                               mode: str) -> list[dict]:
    """차단 목록 → `supersede` 만 하는 발행 레코드(트랙 1 `make_blocked_supersede_record` 계약).

    차단은 지금까지 **레코드를 만들지 않았다** — 오전에 BUY 를 발행했는데 같은 날 차단되면 원장에는
    BUY 가 최신으로 남아 시가에 체결됐다(감사 01-d). 헬퍼가 없거나 시그니처가 다르면 배치를 죽이지 않고
    건너뛴다(트랙 통합 전 저장소에서도 파이프라인이 돌아야 한다). 결과는 진단에 남긴다.
    """
    maker = getattr(record_mod, "make_blocked_supersede_record", None)
    if maker is None or not blocked_rows:
        return []
    accepted = None
    has_kwargs = False
    try:
        params = inspect.signature(maker).parameters
        accepted = set(params)
        has_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        return []
    appended: list[dict] = []
    for blocked in blocked_rows:
        ticker = str(blocked.get("ticker") or "")
        prior = _live_buy_to_supersede(records, ticker, as_of)
        if prior is None:
            continue
        candidate = {
            "blocked": blocked, "prior": prior, "times": times, "as_of": as_of.isoformat(),
            "inputs_hash": inputs_hash, "params_version": params_version, "snapshot_id": snapshot_id,
            "code_commit": code_commit, "mode": mode,
            "signal_published_at": times.get("signal_published_at"),
        }
        call = candidate if has_kwargs else {k: v for k, v in candidate.items() if k in accepted}
        try:
            # ① 실행기 형태: 첫 인자는 **지금까지의 원장**(대체 계보를 찾는 풀)이다.
            record = maker(records, **call)
        except TypeError:
            return appended   # 시그니처 불일치 — 통합 단계에서 맞춘다(배치는 계속)
        except (RecordError, ValueError):
            continue
        if not isinstance(record, dict) or not record.get("signal_id"):
            continue
        try:
            append_immutable(RECORDS, record)
        except RecordError:
            continue
        records.append(record)
        appended.append(record)
    return appended


def holdout_status_for(as_of: date) -> dict:
    from engine.backtest.holdout import HoldoutLedger  # noqa: PLC0415
    try:
        return HoldoutLedger().status(as_of)
    except Exception as exc:  # noqa: BLE001
        return {"locked": True, "note_ko": f"홀드아웃 상태 확인 실패: {type(exc).__name__}",
                "next_open": None}


def stress_payload() -> dict:
    from engine.backtest.stress import stress_report  # noqa: PLC0415
    return stress_report(None, None)


def pilot_summary_of(pilot: dict) -> dict:
    from engine.ledger.pilot import pilot_summary  # noqa: PLC0415
    return pilot_summary(pilot)


def _first_entry_session(executions: dict) -> date | None:
    """체결 이벤트 중 가장 이른 대상 세션 — 파일럿 기간·평균 노출도의 시작점."""
    days: list[date] = []
    for event in (executions or {}).values():
        if (event.get("quant_track") or {}).get("execution_event") != "buy_filled":
            continue
        try:
            days.append(date.fromisoformat(str(event.get("at", ""))[:10]))
        except ValueError:
            continue
    return min(days) if days else None


def _price_as_of(panels: dict) -> str | None:
    dates = [p.frame.index.max() for p in panels.values() if not p.frame.empty]
    if not dates:
        return None
    last = max(dates)
    return last.date().isoformat() if hasattr(last, "date") else str(last)


def _limits(data: dict, as_of: date) -> dict:
    diag = data.get("diagnostics") or {}
    return {
        "as_of": as_of.isoformat(),
        "source": data.get("source"),
        "universe": {"mode": "bootstrap_subset" if data.get("source") != "fixtures" else "fixtures",
                     "universe_verified": False,
                     "note_ko": "지수 구성종목 시계열(universe_history) 미확보 — 생존편향 있음(상한 추정)"},
        "sources": diag.get("sources", {}),
        "degraded": diag.get("degraded", []),
        "notes": [
            "부분집합·픽스처 결과는 전략 성과의 증거가 아니다(기획서 4.4·8.1).",
            "13F 는 레이더 전용이며 신호에 쓰지 않는다.",
        ],
    }


def redact(paths: list[Path]) -> None:
    """커밋 직전 인용 원문을 sha256 으로 바꾼다(2.2.3). 실패해도 발행은 유지."""
    existing = [str(p) for p in paths if Path(p).exists()]
    if not existing:
        return
    proc = subprocess.run([sys.executable, "scripts/redact_quotes.py", "--mode", "sha256", *existing],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(f"redact 실패: {proc.stderr[:200]}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일일 파이프라인 (postclose|premarket)")
    ap.add_argument("--mode", choices=["postclose", "premarket"], default="postclose")
    ap.add_argument("--offline", action="store_true", help="네트워크 없이 픽스처로 실행(테스트)")
    ap.add_argument("--fixtures", action="store_true", help="--offline 과 같다")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--params", default="config/params_v001.yaml")
    ap.add_argument("--slippage-bps", type=float, default=live.SLIPPAGE_BPS)
    ap.add_argument("--risk-free", type=float, default=0.0, help="단기국채 3개월 수익률(연)")
    ap.add_argument("--no-redact", action="store_true")
    args = ap.parse_args(argv)
    if args.offline:
        args.fixtures = True
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
