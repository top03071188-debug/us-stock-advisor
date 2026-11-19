"""공용 타입 — 가격 종류(PriceKind)와 단위 태그.

기획서 2.4.1 / 4.5 LA-9·LA-12: 가격은 3종이며 한 계산 안에서 섞으면 안 된다.
지표 = split_adj_ohlc / 수익률·벤치마크 = total_return_adj / 체결·손절·계좌 = raw_as_traded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

import pandas as pd


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
