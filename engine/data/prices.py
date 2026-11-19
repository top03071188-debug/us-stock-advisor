"""engine.data.prices — yfinance 일봉을 3종 PriceKind 로 정규화하고 원시가를 분할 이력으로 복원한다.

핵심(기획서 2.4.1 / 4.5 LA-9·LA-12):
- Yahoo `Close` 는 **이미 분할 조정**된 값이다(auto_adjust=False 여도). 그래서 `Close` = `split_adj_ohlc`.
- `Adj Close` 는 분할+배당 = `total_return_adj`.
- `raw_as_traded` = `sadj × split_factor`(t 이후 분할비율의 누적곱)로 복원하고, 직접 소스가 있으면 대조한다.
- `available_at` = 그 봉의 NYSE 마감(16:00 ET → UTC).

ATR 비율은 반드시 같은 기준에서 만든다: `atr_pct = atr14_sadj / close_sadj` (v1.3.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from engine.models import PriceKind, PriceSeries
from engine.utils.dates import bar_available_at

PRICE_COLUMNS = [
    "raw_open", "raw_high", "raw_low", "raw_close",
    "sadj_open", "sadj_high", "sadj_low", "sadj_close",
    "adj_close", "split_factor", "volume", "available_at",
]


class PriceQualityError(ValueError):
    """검증을 통과하지 못한 가격 프레임."""


@dataclass
class PricePanel:
    """한 종목의 3종 가격을 함께 담는다. 기준별 슬라이스는 `series()` 로만 꺼낸다."""

    ticker: str
    frame: pd.DataFrame = field(repr=False)
    issues: dict[str, list[str]] = field(default_factory=dict)

    def series(self, kind: PriceKind) -> PriceSeries:
        return PriceSeries(ticker=self.ticker, kind=kind, frame=self.frame)

    def as_of(self, decision_at: datetime) -> "PricePanel":
        mask = pd.to_datetime(self.frame["available_at"], utc=True) <= decision_at
        return PricePanel(self.ticker, self.frame.loc[mask].copy(), dict(self.issues))

    def last_close(self, on_or_before: date, kind: PriceKind = PriceKind.raw_as_traded) -> float | None:
        sub = self.frame.loc[self.frame.index <= on_or_before]
        if sub.empty:
            return None
        return float(sub.iloc[-1][f"{'raw' if kind is PriceKind.raw_as_traded else 'sadj'}_close"])


def _to_date_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("America/New_York").normalize().tz_localize(None)
    out.index = idx
    out.index.name = "date"
    return out


def split_factor_series(index: pd.DatetimeIndex, splits: pd.Series | dict | None) -> pd.Series:
    """t일의 누적 분할계수 = t **이후** 발생한 분할비율의 곱.

    ex-date 당일의 가격은 이미 분할 후이므로, ex-date 이전 날짜만 ratio 를 곱해 되돌린다.
    """
    factors = pd.Series(1.0, index=index, dtype=float)
    if splits is None or len(splits) == 0:
        return factors
    if isinstance(splits, dict):
        splits = pd.Series(splits)
    sp = splits.copy()
    sp.index = pd.to_datetime(sp.index).tz_localize(None) if getattr(sp.index, "tz", None) else pd.to_datetime(sp.index)
    sp = sp.sort_index()
    for ex_date, ratio in sp.items():
        if ratio is None or not np.isfinite(ratio) or ratio <= 0:
            continue
        factors = factors.where(~(index < ex_date), factors * float(ratio))
    return factors


def build_price_frame(ticker: str, history: pd.DataFrame, splits: pd.Series | dict | None = None) -> PricePanel:
    """yfinance `history(auto_adjust=False, actions=True)` 결과 → 3종 가격 프레임."""
    if history is None or len(history) == 0:
        raise PriceQualityError(f"{ticker}: 빈 가격 이력")
    df = _to_date_index(history)
    missing = [c for c in ("Open", "High", "Low", "Close") if c not in df.columns]
    if missing:
        raise PriceQualityError(f"{ticker}: 필수 컬럼 없음 {missing}")

    out = pd.DataFrame(index=df.index)
    for src, dst in (("Open", "sadj_open"), ("High", "sadj_high"), ("Low", "sadj_low"), ("Close", "sadj_close")):
        out[dst] = df[src].astype(float)
    out["adj_close"] = df["Adj Close"].astype(float) if "Adj Close" in df.columns else out["sadj_close"]
    out["volume"] = df["Volume"].astype(float) if "Volume" in df.columns else np.nan

    out["split_factor"] = split_factor_series(out.index, splits)
    for dst in ("open", "high", "low", "close"):
        out[f"raw_{dst}"] = out[f"sadj_{dst}"] * out["split_factor"]

    out["available_at"] = [
        bar_available_at(d.date() if hasattr(d, "date") else d) for d in out.index
    ]
    out = out[PRICE_COLUMNS]
    issues = validate_frame(ticker, out, splits)
    return PricePanel(ticker=ticker, frame=out, issues=issues)


def validate_frame(ticker: str, frame: pd.DataFrame, splits: pd.Series | dict | None = None) -> dict[str, list[str]]:
    """검증 코드 목록(2.4.1). 실패는 예외가 아니라 이슈 코드로 남긴다(파이프라인은 배너 표시)."""
    issues: dict[str, list[str]] = {}
    price_cols = ["sadj_open", "sadj_high", "sadj_low", "sadj_close", "raw_close"]
    bad = frame[(frame[price_cols] <= 0).any(axis=1)]
    if not bad.empty:
        issues["NON_POSITIVE_PRICE"] = [d.date().isoformat() for d in bad.index]

    recon = frame["raw_close"] / (frame["sadj_close"] * frame["split_factor"]) - 1.0
    mismatch = frame[recon.abs() > 1e-3]
    if not mismatch.empty:
        issues["ADJUSTMENT_MISMATCH"] = [d.date().isoformat() for d in mismatch.index]

    ret = frame["sadj_close"].pct_change()
    has_split = False
    if splits is not None and len(splits) > 0:
        sp_index = pd.to_datetime(list(splits.index if hasattr(splits, "index") else splits.keys()))
        has_split = len(sp_index) > 0
    anomaly = frame[ret.abs() > 0.40]
    if not anomaly.empty and not has_split:
        issues["SPLIT_ANOMALY"] = [d.date().isoformat() for d in anomaly.index]

    if len(frame) < 1260:  # 5년 ≈ 1260거래일
        issues.setdefault("INSUFFICIENT_HISTORY", []).append(f"{len(frame)} rows")
    return issues


def reconstruct_raw(sadj: pd.Series, splits: pd.Series | dict | None) -> pd.Series:
    """분할 조정가 → 당시 거래가(raw) 역산. 테스트·검증용 공개 함수."""
    factor = split_factor_series(pd.DatetimeIndex(sadj.index), splits)
    return sadj.astype(float) * factor


def verify_against_raw_source(panel: PricePanel, direct_raw: pd.DataFrame, tolerance: float = 0.001) -> dict:
    """직접 원시가 소스(최근 2년)와 역산값을 대조한다. 기간별 `raw_verified` 를 남긴다."""
    sub = panel.frame.join(direct_raw[["Open", "High", "Low", "Close"]], how="inner", suffixes=("", "_direct"))
    if sub.empty:
        return {"verified_days": 0, "mismatch_days": 0, "raw_verified": False}
    err = (sub["raw_close"] / sub["Close"].astype(float) - 1).abs()
    return {
        "verified_days": int(len(sub)),
        "mismatch_days": int((err > tolerance).sum()),
        "raw_verified": bool((err > tolerance).sum() == 0),
        "max_abs_error": float(err.max()),
    }


def atr_basis_note() -> str:
    return "atr_pct = atr14_sadj / close_sadj (split_adj_ohlc 기준 비율)"


def price_stats(panel: PricePanel, day: date) -> dict:
    """화면·게이트용 요약(당일 기준). 지표는 sadj, 표시 가격은 raw."""
    sub = panel.frame.loc[panel.frame.index <= day]
    if sub.empty:
        return {}
    row = sub.iloc[-1]
    return {
        "price": float(row["raw_close"]),
        "sadj_close": float(row["sadj_close"]),
        "split_factor": float(row["split_factor"]),
        "volume": float(row["volume"]) if np.isfinite(row["volume"]) else None,
    }
