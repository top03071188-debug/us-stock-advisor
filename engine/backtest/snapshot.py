"""engine.backtest.snapshot — as_of 절단 + manifest + sha256 스냅샷.

[고정]
- 모든 조회는 `available_at ≤ decision_at`. 이 조건이 없는 프레임은 스냅샷에 넣지 않는다.
- `manifest.json` 에 파일 목록·sha256·decision_at 을 기록하고, `snapshot_id` 는 manifest 내용의 해시다.
- **판단 입력 전체**를 스냅샷에 넣는다(R10a). 가격만 해시하면(market/economy/fundamentals/news 가
  바뀌어도) 같은 `snapshot_id` 가 나와 재현 단위가 무너진다. `inputs` 는 `panels` 를 뺀 데이터셋
  전체를 **제네릭 직렬화**한 것이다 — 특정 키를 나열하지 않으므로 데이터 계층이 필드를 늘려도
  자동으로 해시에 들어간다(경계 계약 §4.2-3).
- 같은 입력이면 같은 `snapshot_id` (재현의 단위, 4.9).
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from engine.data.prices import PricePanel

INPUTS_FILE = "inputs.json"
DATACLASS_TAG = "__dataclass__"


class SnapshotError(ValueError):
    pass


# ────────────────────────────── 직렬화 유틸 (경계 계약) ──────────────────────────────
def _jsonable(value):  # noqa: PLR0911 — 타입별 분기 그 자체가 목적이다
    """임의의 판단 입력 → JSON 가능한 값. NaN/Inf → None, 날짜 → ISO8601 UTC."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (float, np.floating)):
        out = float(value)
        return out if math.isfinite(out) else None
    if isinstance(value, (datetime, pd.Timestamp)):
        ts = pd.Timestamp(value)
        if ts is pd.NaT or pd.isna(ts):
            return None
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        payload = {DATACLASS_TAG: f"{type(value).__module__}.{type(value).__qualname__}"}
        for field in dataclasses.fields(value):
            payload[field.name] = _jsonable(getattr(value, field.name))
        return payload
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, pd.Series):
        return {str(_jsonable(k)): _jsonable(v) for k, v in value.items()}
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if hasattr(value, "isoformat"):
        return _jsonable(value.isoformat())
    return str(value)


def canonical_json_bytes(obj) -> bytes:
    """정렬된 키·고정 구분자의 canonical JSON — 같은 입력이면 같은 바이트."""
    return json.dumps(_jsonable(obj), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _restore(value):
    """`_jsonable` 의 역방향 — dataclass 태그만 되살린다(없으면 그대로)."""
    if isinstance(value, dict):
        tag = value.get(DATACLASS_TAG)
        if tag:
            rest = {k: _restore(v) for k, v in value.items() if k != DATACLASS_TAG}
            cls = _resolve_class(tag)
            if cls is not None:
                try:
                    return cls(**rest)
                except TypeError:
                    return rest
            return rest
        return {k: _restore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore(v) for v in value]
    return value


def _resolve_class(tag: str):
    module_name, _, cls_name = tag.rpartition(".")
    if not module_name.startswith("engine."):
        return None
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    cls = getattr(module, cls_name, None)
    return cls if isinstance(cls, type) and dataclasses.is_dataclass(cls) else None


# ────────────────────────────── CSV·스냅샷 ──────────────────────────────
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
                  as_of: str | None = None, extra: dict | None = None,
                  inputs: dict | None = None) -> Snapshot:
    """프레임(CSV) + 판단 입력(canonical JSON) 을 저장하고 manifest·snapshot_id 를 만든다."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    as_of = as_of or decision_at.date().isoformat()
    files: dict[str, str] = {}
    for name, df in frames.items():
        sliced = as_of_frame(df, decision_at)
        data = _canonical_csv(sliced)
        (root / f"{name}.csv").write_bytes(data)
        files[f"{name}.csv"] = sha256_bytes(data)
    if inputs is not None:
        payload = canonical_json_bytes(inputs)
        (root / INPUTS_FILE).write_bytes(payload)
        files[INPUTS_FILE] = sha256_bytes(payload)

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


def _panel_from_csv(ticker: str, path: Path) -> PricePanel:
    df = pd.read_csv(path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    if "available_at" in df.columns:
        df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    return PricePanel(ticker=ticker, frame=df)


def load_snapshot_dataset(root: str | Path, snapshot: Snapshot | None = None) -> dict:
    """스냅샷 → 데이터셋(`panels` + `inputs.json` 복원). 재생이 `build_payload` 에 그대로 넣는다."""
    root = Path(root)
    snap = snapshot or load_snapshot(root)
    panels: dict[str, PricePanel] = {}
    for name in snap.files:
        if not name.startswith("prices_") or not name.endswith(".csv"):
            continue
        ticker = name[len("prices_"):-len(".csv")]
        panels[ticker] = _panel_from_csv(ticker, root / name)
    data: dict = {}
    inputs_path = root / INPUTS_FILE
    if inputs_path.exists():
        data = _restore(json.loads(inputs_path.read_text(encoding="utf-8")))
        if not isinstance(data, dict):
            data = {}
    data["panels"] = panels
    data.setdefault("as_of", snap.as_of)
    return data


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
