"""engine.data.universe — 시점별 지수 구성종목과 상장폐지 데이터(생존편향 대응, 2.4.5).

- 구성종목 조회는 `effective_at ≤ decision_at < removed_at`(발효일 기준). 발표일(`announcement_at`)을 쓰지 않는다.
- 데이터셋이 없거나 커버리지가 부족하면 `universe_verified: false` 를 돌려주고, **게이트 사용을 차단**한다.
- 상장폐지 종목은 삭제하지 않고 `delisting_return` 이 없으면 "불명 청산"으로 포함한다.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

UNIVERSE_COLUMNS = ["date", "ticker", "index_name", "sector", "added_at", "removed_at", "announcement_at", "effective_at"]
DELISTED_COLUMNS = ["ticker", "cik", "delist_date", "delist_type", "final_price", "delisting_return", "source"]


class UniverseNotVerified(Exception):
    """미검증 유니버스로 게이트·성과를 계산하려 할 때."""


def load_universe_history(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=UNIVERSE_COLUMNS)
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    for col in ("added_at", "removed_at", "announcement_at", "effective_at"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def load_delisted(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=DELISTED_COLUMNS)
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    if "delist_date" in df.columns:
        df["delist_date"] = pd.to_datetime(df["delist_date"], errors="coerce")
    return df


def validate_universe(df: pd.DataFrame, quarter_end_counts: dict[str, int] | None = None,
                      tolerance: float = 0.02) -> dict:
    """2.4.5 검증 기준 ① 구성종목 수 ±2% (공식 발표치가 있을 때)."""
    result = {"ok": True, "reasons": [], "rows": int(len(df))}
    if df.empty:
        return {"ok": False, "reasons": ["EMPTY"], "rows": 0}
    required = {"ticker", "effective_at"}
    missing = required - set(df.columns)
    if missing:
        result["ok"] = False
        result["reasons"].append(f"MISSING_COLUMNS:{sorted(missing)}")
    if df["removed_at"].notna().all() if "removed_at" in df.columns else False:
        result["ok"] = False
        result["reasons"].append("NO_ACTIVE_MEMBERS")
    if quarter_end_counts:
        for q, expected in quarter_end_counts.items():
            got = df[(df["effective_at"] <= pd.Timestamp(q))
                     & ((df["removed_at"].isna()) | (df["removed_at"] > pd.Timestamp(q)))]["ticker"].nunique()
            if expected and abs(got - expected) / expected > tolerance:
                result["ok"] = False
                result["reasons"].append(f"COUNT_MISMATCH:{q}:{got}/{expected}")
    return result


def _to_utc(series: pd.Series) -> pd.Series:
    """tz-naive/aware 가 섞여도 비교되게 UTC aware 로 통일."""
    return pd.to_datetime(series, errors="coerce", utc=True)


def membership_at(df: pd.DataFrame, decision_at: datetime) -> set[str]:
    """발효일 ≤ decision_at < 제거일 인 티커. 제거일이 없으면 현재 구성종목."""
    if df.empty:
        return set()
    decision_ts = pd.Timestamp(decision_at)
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.tz_localize("UTC")
    eff = _to_utc(df["effective_at"])
    rem = _to_utc(df["removed_at"])
    mask = (eff <= decision_ts) & (rem.isna() | (rem > decision_ts))
    return set(df.loc[mask, "ticker"].astype(str).str.upper())


def coverage(df: pd.DataFrame, decision_at: datetime) -> dict:
    """그 시점을 실제로 덮고 있는가 — 데이터 시작일이 decision_at 이전이어야 한다."""
    if df.empty:
        return {"covered": False, "reason": "EMPTY"}
    decision_ts = pd.Timestamp(decision_at)
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.tz_localize("UTC")
    eff = _to_utc(df["effective_at"]).dropna()
    if eff.empty:
        return {"covered": False, "reason": "NO_EFFECTIVE_AT"}
    start = eff.min()
    if start > decision_ts:
        return {"covered": False, "reason": f"STARTS_AFTER_DECISION:{start.date().isoformat()}"}
    return {"covered": True, "start": start.date().isoformat()}


def universe_view(df: pd.DataFrame, decision_at: datetime) -> dict:
    """시점별 유니버스 + 검증 플래그(미검증이면 게이트 사용 금지)."""
    cov = coverage(df, decision_at)
    members = membership_at(df, decision_at) if cov["covered"] else set()
    return {
        "decision_at": decision_at.isoformat(),
        "tickers": sorted(members),
        "count": len(members),
        "universe_verified": bool(cov["covered"] and members),
        "reason": None if cov["covered"] else cov.get("reason"),
    }


def require_verified(view: dict) -> None:
    if not view.get("universe_verified"):
        raise UniverseNotVerified(
            f"universe_verified=false ({view.get('reason')}) — 이 결과는 게이트·성과 근거로 쓸 수 없다(2.4.5)."
        )


def delisting_return_or_flag(delisted: pd.DataFrame) -> pd.DataFrame:
    """`delisting_return` 결측을 '불명 청산'으로 표시하고 **제외하지 않는다**."""
    out = delisted.copy()
    if out.empty:
        return out
    out["delisting_status"] = out["delisting_return"].apply(
        lambda x: "known" if pd.notna(x) else "unknown_liquidation"
    )
    return out


def sector_map(df: pd.DataFrame) -> dict[str, str]:
    if df.empty or "sector" not in df.columns:
        return {}
    latest = df.sort_values("effective_at").groupby("ticker").tail(1)
    return dict(zip(latest["ticker"].astype(str).str.upper(), latest["sector"]))
