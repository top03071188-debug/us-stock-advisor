"""engine.backtest.replay — 스냅샷에서 시점 T 의 1·2층 계산을 **라이브와 같은 함수로** 다시 실행한다.

[고정, 4.12]
- 화면과 검증이 다른 규칙을 쓰면 4장의 성과는 화면의 성과가 아니다. 그래서 재생은 자체 최소
  루프(`screener.screen` → 밴드 유무로 BUY/WATCH)를 쓰지 않고 **`build_payload` 를 그대로** 부른다.
  B0~B6·Veto·등급·분할 밴드가 라이브와 자동으로 같아진다.
- 재생은 스냅샷에 **고정된 최종 판단 입력**을 그대로 읽는다(감사 02-b·02-c): 워치리스트 필터를 마친
  `universe`, 보유 종목(`held`), 섹터 비중, **실제 세 시각**, `target_session`, 파라미터(`params`).
  예전에는 `held=[]`·`sector_weights={}` 로 43종목을 전부 돌려 B1(워치리스트 밖/보유)을 항상
  통과시켰고, 세 시각은 계획값이었다.
- `decision` 블록이 없는 **과거 스냅샷**은 그 사실을 `inputs_pinned: false`·`note_ko` 로 드러낸다 —
  "입력이 고정된 것처럼" 조용히 재생하지 않는다.
- 이 모듈은 **결정론**만 책임진다: 같은 스냅샷 + 같은 파라미터면 같은 신호 JSON(타임스탬프·id 제외).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from engine.backtest.snapshot import Snapshot, load_snapshot_dataset
from engine.decide.pipeline import build_payload
from engine.utils.dates import data_cutoff_at, order_cutoff_at, signal_published_at

#: digest 에서 제외하는 값 — 실행 시각·식별자는 재현의 단위가 아니다.
VOLATILE_KEYS = ("generated_at", "signal_id", "snapshot_id", "code_commit")

NOTE_INPUTS_UNPINNED = (
    "스냅샷에 최종 판단 입력(보유·섹터 비중·실제 세 시각·target_session)이 고정돼 있지 않다 — "
    "`held=[]`·`sector_weights={}` 로 재생한 과거 스냅샷이며, 그 날의 재생 결과는 원본과 다를 수 있다."
)


@dataclass
class ReplayResult:
    as_of: str
    snapshot_id: str
    watchlist: list[dict]
    signals: list[dict]
    blocked: list[dict]
    digest: str
    regime: dict = field(default_factory=dict)
    position_checks: list[dict] = field(default_factory=list)
    inputs_pinned: bool = False
    target_session: str | None = None
    params_source: str = "caller"
    note_ko: str | None = None

    def to_json(self) -> str:
        base = {
            "as_of": self.as_of,
            "snapshot_id": self.snapshot_id,
            "watchlist": self.watchlist,
            "signals": self.signals,
            "blocked": self.blocked,
            "regime": self.regime,
            "position_checks": self.position_checks,
            "inputs_pinned": self.inputs_pinned,
            "target_session": self.target_session,
            "params_source": self.params_source,
            "note_ko": self.note_ko,
        }
        return json.dumps(base, ensure_ascii=False, sort_keys=True, indent=2)


def _digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable(obj):
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in sorted(obj.items()) if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_stable(v) for v in obj]
    return obj


def fixed_times_from_manifest(manifest: dict) -> dict:
    """스냅샷의 결정 시각 → 재생용 세 시각(라이브와 같은 규칙, 실제 시계는 쓰지 않는다).

    `decision` 블록이 없는 과거 스냅샷의 **폴백**이다 — 계획 시각과 `target_session = as_of` 를 쓴다.
    """
    as_of = date.fromisoformat(str(manifest["as_of"])[:10])
    mode = str((manifest.get("extra") or {}).get("mode") or "postclose")
    cutoff = data_cutoff_at(as_of, mode if mode in ("postclose", "premarket") else "postclose")
    return {
        "data_cutoff_at": _iso(cutoff),
        "signal_published_at": _iso(signal_published_at(cutoff)),
        "order_cutoff_at": _iso(order_cutoff_at(as_of)),
        "target_session": as_of.isoformat(),
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def replay_times(manifest: dict, decision: dict | None) -> tuple[dict, bool]:
    """스냅샷 → (재생용 세 시각, 입력 고정 여부). 고정된 **실제** 시각을 먼저 쓴다."""
    fallback = fixed_times_from_manifest(manifest)
    decision = decision or {}
    need = ("data_cutoff_at", "signal_published_at", "order_cutoff_at", "target_session")
    if all(decision.get(key) for key in need):
        times = {key: decision[key] for key in need}
        times["signal_published_planned_at"] = decision.get("signal_published_planned_at")
        return times, True
    return fallback, False


def replay(snapshot: Snapshot, params: dict | None = None, params_version: str | None = None,
           code_commit: str = "replay", snapshot_root=None) -> ReplayResult:
    """스냅샷 시점의 1·2층 계산을 라이브와 같은 `build_payload` 로 다시 만든다.

    `params` 를 주지 않으면 **스냅샷에 고정된 파라미터**를 쓴다(발행 당시 설정 그대로).
    """
    root = snapshot_root or snapshot.root
    data = load_snapshot_dataset(root, snapshot)
    as_of = date.fromisoformat(str(snapshot.as_of)[:10])
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    three_times, pinned = replay_times(snapshot.manifest, decision)
    held = [h for h in (decision.get("held") or []) if isinstance(h, dict)]
    sector_weights = {str(k): float(v) for k, v in (decision.get("sector_weights") or {}).items()
                      if isinstance(v, (int, float))}
    params_source = "caller"
    if params is None and isinstance(decision.get("params"), dict):
        params = decision["params"]
        params_source = "snapshot"
    if params is None:
        raise ValueError("재생에 쓸 파라미터가 없다 — 인자로 주거나 `decision.params` 가 고정된 스냅샷을 쓴다")
    if params_version is None:
        params_version = str(decision.get("params_version") or params.get("version") or "params_v001")
    payload = build_payload(
        data, as_of=as_of, mode=decision.get("mode") or "replay", params=params,
        params_version=params_version, code_commit=code_commit, snapshot_id=snapshot.snapshot_id,
        status="replay", three_times=three_times, held=held, sector_weights=sector_weights,
        inputs_hash=decision.get("inputs_hash"),
    )
    stable = _stable({
        "signals": payload.get("signals") or [],
        "blocked": payload.get("blocked") or [],
        "watchlist": payload.get("watchlist") or [],
    })
    return ReplayResult(
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        watchlist=payload.get("watchlist") or [],
        signals=payload.get("signals") or [],
        blocked=payload.get("blocked") or [],
        digest=_digest(json.dumps(stable, ensure_ascii=False, sort_keys=True)),
        regime=payload.get("regime") or {},
        position_checks=payload.get("position_checks") or [],
        inputs_pinned=pinned,
        target_session=three_times.get("target_session"),
        params_source=params_source,
        note_ko=None if pinned else NOTE_INPUTS_UNPINNED,
    )
