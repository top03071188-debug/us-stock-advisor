"""engine.backtest.snapshot — as_of 절단 + manifest + sha256 스냅샷.

[고정]
- 모든 조회는 `available_at ≤ decision_at`. 이 조건이 없는 프레임은 스냅샷에 넣지 않는다.
- `manifest.json` 에 파일 목록·sha256·decision_at 을 기록하고, `snapshot_id` 는 manifest 내용의 해시다.
- 같은 입력이면 같은 `snapshot_id` (재현의 단위, 4.9).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


class SnapshotError(ValueError):
    pass


def _canonical_csv(df: pd.DataFrame) -> bytes:
    out = df.copy()
    out = out.sort_index()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = pd.to_datetime(out[col], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(out.index, pd.DatetimeIndex):
        out.index = out.index.strftime("%Y-%m-%d")
        out = out.reset_index().rename(columns={"index": "date"})
    return out.to_csv(index=False, lineterminator="\n").encode("utf-8")


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def as_of_frame(df: pd.DataFrame, decision_at: datetime, column: str = "available_at") -> pd.DataFrame:
    """`available_at ≤ decision_at` 강제. 컬럼이 없으면 실패한다(날짜 기준 조인 금지)."""
    if df is None or len(df) == 0:
        return df if df is not None else pd.DataFrame()
    if column not in df.columns:
        raise SnapshotError(
            f"'{column}' 컬럼이 없다 — date/filed_date 기준으로 자르지 않는다(4.2.2, LA-11)."
        )
    ts = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df.loc[ts <= decision_at].copy()


@dataclass
class Snapshot:
    root: Path
    decision_at: datetime
    as_of: str
    snapshot_id: str
    files: dict[str, str]
    manifest: dict

    def frame(self, name: str) -> pd.DataFrame:
        path = self.root / f"{name}.csv"
        if not path.exists():
            raise SnapshotError(f"스냅샷에 {name} 이 없다")
        return pd.read_csv(path, parse_dates=True)


def make_snapshot(root: str | Path, decision_at: datetime, frames: dict[str, pd.DataFrame],
                  as_of: str | None = None, extra: dict | None = None) -> Snapshot:
    """프레임을 canonical CSV 로 저장하고 manifest·snapshot_id 를 만든다."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    as_of = as_of or decision_at.date().isoformat()
    files: dict[str, str] = {}
    for name, df in frames.items():
        sliced = as_of_frame(df, decision_at)
        data = _canonical_csv(sliced)
        (root / f"{name}.csv").write_bytes(data)
        files[f"{name}.csv"] = sha256_bytes(data)

    manifest = {
        "decision_at": decision_at.isoformat(),
        "as_of": as_of,
        "files": files,
        "extra": extra or {},
        "cutoff_rule": "available_at <= decision_at",
    }
    sid = snapshot_id_of(manifest)
    manifest["snapshot_id"] = sid
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return Snapshot(root=root, decision_at=decision_at, as_of=as_of, snapshot_id=sid, files=files, manifest=manifest)


def snapshot_id_of(manifest: dict) -> str:
    base = {k: v for k, v in manifest.items() if k not in ("snapshot_id", "created_at")}
    payload = json.dumps(base, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_snapshot(root: str | Path) -> Snapshot:
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return Snapshot(
        root=root,
        decision_at=pd.Timestamp(manifest["decision_at"]).to_pydatetime(),
        as_of=manifest["as_of"],
        snapshot_id=manifest["snapshot_id"],
        files=manifest["files"],
        manifest=manifest,
    )


def verify_snapshot(root: str | Path) -> dict:
    """파일 해시와 snapshot_id 를 다시 계산해 대조한다(변조·부분 삭제 탐지)."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for name, expected in manifest["files"].items():
        p = root / name
        if not p.exists():
            problems.append(f"missing:{name}")
            continue
        actual = sha256_bytes(p.read_bytes())
        if actual != expected:
            problems.append(f"hash_mismatch:{name}")
    recomputed = snapshot_id_of(manifest)
    if recomputed != manifest.get("snapshot_id"):
        problems.append("snapshot_id_mismatch")
    return {
        "ok": not problems,
        "problems": problems,
        "snapshot_id": manifest.get("snapshot_id"),
        "recomputed_snapshot_id": recomputed,
        "files": len(manifest["files"]),
    }
