"""공용 타입 — 가격 종류(PriceKind)와 단위 태그, 그리고 데이터 계약 검증.

기획서 2.4.1 / 4.5 LA-9·LA-12: 가격은 3종이며 한 계산 안에서 섞으면 안 된다.
지표 = split_adj_ohlc / 수익률·벤치마크 = total_return_adj / 체결·손절·계좌 = raw_as_traded.

`dataclasses` 외에 `validate_quant_signals` 가 `schemas/quant_signals.schema.json` 을 읽어
데이터 계약을 강제한다(2.2.2): 세 시각 순서, 발행 레코드의 `execution_event == None`,
공개 금지 필드(`avg_cost`·`shares`·원시 OHLC) 미포함.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Mapping

import pandas as pd

from engine.schema import load_schema, validate as validate_schema


class PriceKind(str, Enum):
    total_return_adj = "total_return_adj"   # 분할+배당 (adj_close)
    split_adj_ohlc = "split_adj_ohlc"       # 분할만 (sadj_*)
    raw_as_traded = "raw_as_traded"         # 당시 거래가 (raw_*)


#: PriceKind 별 OHLC 컬럼 이름
OHLC_COLUMNS: Mapping[PriceKind, tuple[str, str, str, str]] = {
    PriceKind.split_adj_ohlc: ("sadj_open", "sadj_high", "sadj_low", "sadj_close"),
    PriceKind.raw_as_traded: ("raw_open", "raw_high", "raw_low", "raw_close"),
}
CLOSE_COLUMN: Mapping[PriceKind, str] = {
    PriceKind.total_return_adj: "adj_close",
    PriceKind.split_adj_ohlc: "sadj_close",
    PriceKind.raw_as_traded: "raw_close",
}


class PriceKindError(TypeError):
    """가격 기준이 섞였을 때(예: 지표 함수에 raw_as_traded 전달)."""


class UnitMismatchError(ValueError):
    """내재가치 중앙값에 USD/share 가 아닌 값(EV·시총 등)이 들어왔을 때."""


@dataclass(frozen=True)
class PriceSeries:
    """한 종목의 한 가격 기준. 지표·체결 함수는 이 태그를 검사한다."""

    ticker: str
    kind: PriceKind
    frame: pd.DataFrame = field(repr=False)

    def close(self) -> pd.Series:
        return self.frame[CLOSE_COLUMN[self.kind]].astype(float)

    def ohlc(self) -> pd.DataFrame:
        if self.kind not in OHLC_COLUMNS:
            raise PriceKindError(
                f"{self.kind.value} 에는 OHLC 가 없다. 지표는 split_adj_ohlc, 수익률은 total_return_adj 를 쓴다."
            )
        return self.frame[list(OHLC_COLUMNS[self.kind])].astype(float)


@dataclass(frozen=True)
class PerShareValue:
    """주당 달러(USD/share) 값. 단위를 태그해 EV·주식수와 섞이지 않게 한다."""

    value: float
    method: str = ""
    unit: str = "USD/share"

    def __post_init__(self) -> None:
        if self.unit != "USD/share":
            raise UnitMismatchError(f"{self.method}: unit={self.unit!r} — USD/share 만 중앙값에 넣을 수 있다.")


# ────────────────────────────── 데이터 계약 검증 ──────────────────────────────
SCHEMA_PATH = Path("schemas/quant_signals.schema.json")

#: 커밋·공개 금지 키(2.2.3). 하나라도 있으면 신호 파일을 버린다.
FORBIDDEN_KEYS = {
    "quote", "body", "body_excerpt_1500", "raw_open", "raw_high", "raw_low", "raw_close",
    "sadj_open", "sadj_high", "sadj_low", "sadj_close", "adj_close", "ohlc", "volume_series",
    "shares", "shares_held", "avg_cost", "account", "holdings", "cash_balance", "total_asset",
    "api_key", "secret",
}


class QuantSignalSchemaError(ValueError):
    """`data/quant_signals.json` 이 데이터 계약을 어겼다 — 파일을 버리고 이전 파일을 유지한다."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors[:8]) + (f" … 총 {len(errors)}건" if len(errors) > 8 else ""))


def _iter_keys(obj, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{prefix}.{k}" if prefix else k, k
            yield from _iter_keys(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_keys(v, f"{prefix}[{i}]")


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"시각에 시간대가 없다: {value!r}")
    return ts


def check_signal_times(record: dict) -> list[str]:
    """`data_cutoff_at ≤ signal_published_at < order_cutoff_at` (2.2.2 [고정])."""
    try:
        a = _parse_ts(record["data_cutoff_at"])
        b = _parse_ts(record["signal_published_at"])
        c = _parse_ts(record["order_cutoff_at"])
    except (KeyError, ValueError) as exc:
        return [f"{record.get('signal_id', '?')}: 시각 필드 오류 — {exc}"]
    if not (a <= b < c):
        return [f"{record.get('signal_id', '?')}: 세 시각 순서 위반"]
    return []


def validate_quant_signals(payload: dict, schema_path: str | Path | None = None) -> dict:
    """데이터 계약 검증. 통과하면 요약을, 실패하면 `QuantSignalSchemaError` 를 던진다."""
    errors = validate_schema(payload, load_schema(schema_path or SCHEMA_PATH))
    for path, key in _iter_keys(payload):
        if key in FORBIDDEN_KEYS:
            errors.append(f"{path}: 공개 금지 필드({key})")
    for record in payload.get("signals") or []:
        sid = record.get("signal_id", "?")
        if record.get("execution_event") is not None:
            errors.append(f"{sid}: 발행 레코드의 execution_event 가 None 이 아니다")
        position = record.get("position") or {}
        if position.get("stop_price") is not None:
            errors.append(f"{sid}: position.stop_price 가 발행 시점에 채워져 있다")
        if position.get("planned_loss_after_fill_pct") is not None:
            errors.append(f"{sid}: position.planned_loss_after_fill_pct 가 발행 시점에 채워져 있다")
        for row in record.get("entry_plan") or []:
            if row.get("provisional") is not True:
                errors.append(f"{sid}: entry_plan[{row.get('tranche')}] 이 잠정가가 아니다")
            if "fill_price" in row:
                errors.append(f"{sid}: entry_plan[{row.get('tranche')}] 에 fill_price")
        errors.extend(check_signal_times(record))
        band = record.get("target_band") or {}
        if band and abs(float(band.get("r_lo", 0.0)) - (float(band.get("lo", 0)) / float(band["base_price"]) - 1)) > 1e-4:
            errors.append(f"{sid}: target_band.r_lo 가 base_price 기준이 아니다")
    try:
        errors.extend(check_signal_times(payload))
    except (KeyError, TypeError):
        pass
    ids = [r.get("signal_id") for r in payload.get("signals") or []]
    if len(ids) != len(set(ids)):
        errors.append("signal_id 중복")
    if errors:
        raise QuantSignalSchemaError(errors)
    return {
        "ok": True,
        "schema_version": payload.get("schema_version"),
        "n_signals": len(ids),
        "n_blocked": len(payload.get("blocked") or []),
        "regime": (payload.get("regime") or {}).get("regime"),
        "data_cutoff_at": payload.get("data_cutoff_at"),
        "signal_published_at": payload.get("signal_published_at"),
        "order_cutoff_at": payload.get("order_cutoff_at"),
    }
