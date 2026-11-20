"""engine.backtest.replay — 스냅샷에서 시점 T 의 1·2층 계산을 **라이브와 같은 함수로** 다시 실행한다.

[고정, 4.12]
- 화면과 검증이 다른 규칙을 쓰면 4장의 성과는 화면의 성과가 아니다. 그래서 재생은 자체 최소
  루프(`screener.screen` → 밴드 유무로 BUY/WATCH)를 쓰지 않고 **`build_payload` 를 그대로** 부른다.
  B0~B6·Veto·등급·분할 밴드가 라이브와 자동으로 같아진다.
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

    def to_json(self) -> str:
        base = {
            "as_of": self.as_of,
            "snapshot_id": self.snapshot_id,
            "watchlist": self.watchlist,
            "signals": self.signals,
            "blocked": self.blocked,
            "regime": self.regime,
            "position_checks": self.position_checks,
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
    """스냅샷의 결정 시각 → 재생용 세 시각(라이브와 같은 규칙, 실제 시계는 쓰지 않는다)."""
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


def replay(snapshot: Snapshot, params: dict, params_version: str = "params_v001",
           code_commit: str = "replay", snapshot_root=None) -> ReplayResult:
    """스냅샷 시점의 1·2층 계산을 라이브와 같은 `build_payload` 로 다시 만든다."""
    root = snapshot_root or snapshot.root
    data = load_snapshot_dataset(root, snapshot)
    as_of = date.fromisoformat(str(snapshot.as_of)[:10])
    payload = build_payload(
        data, as_of=as_of, mode="replay", params=params, params_version=params_version,
        code_commit=code_commit, snapshot_id=snapshot.snapshot_id, status="replay",
        three_times=fixed_times_from_manifest(snapshot.manifest),
        held=[], sector_weights={},
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
    )
