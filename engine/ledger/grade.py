"""engine.ledger.grade — 발행 기록 채점 (1주·1개월·3개월·6개월, 기획서 4.2.4·4.7.2).

[고정]
- 기간은 **거래일**(5 / 21 / 63 / 126). 달력 개월이 아니다.
- 초과수익·방향·모의 계좌는 **`fill` 기준**, 목표 밴드는 **`base` 기준** 수익률로 비교한다.
  두 값을 섞으면 밴드 점수가 의미를 잃는다(`test_band_basis`).
- `maturity_date ≥ holdout_start` 인 폴드는 **적격이 아니다** — `truncated: true` + `observed_days` 로
  참고 표시만 하고 보정 목적함수에 넣지 않는다(4.2.1, 4.3).
- 체결이 없는 결정(`hold_cash`)도 **가상 체결가**로 채점한다(4.7.2).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from engine.backtest.walk_forward import agreement_components
from engine.utils.dates import add_trading_days, trading_day_count

HORIZONS_TD = {"1w": 5, "1m": 21, "3m": 63, "6m": 126}
GRADE_WEIGHTS = {"A": 1.0, "B": 0.7, "C": 0.3}


def _bars_frame(bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.sort_values("date").reset_index(drop=True)


def _window(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df.iloc[0:0]
    return df[(df["date"] > start) & (df["date"] <= end)]


def realized_mdd(closes: list[float], entry_price: float | None = None) -> float | None:
    """구간 최고점 대비 최대 하락(진입가 포함)."""
    series = ([float(entry_price)] if entry_price else []) + [float(c) for c in closes]
    if len(series) < 2:
        return None
    peak = series[0]
    worst = 0.0
    for value in series:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def grade_record(record: dict, execution: dict | None = None, bars: list[dict] | None = None,
                 benchmark_returns: dict[str, float] | None = None,
                 holdout_start: date | str | None = None,
                 horizons: dict[str, int] | None = None) -> dict:
    """한 신호의 기간별 채점. 신호가 없거나 데이터가 모자라면 그 기간은 `unavailable` 로 남긴다."""
    horizons = horizons or HORIZONS_TD
    holdout = None
    if holdout_start is not None:
        holdout = holdout_start if isinstance(holdout_start, date) else date.fromisoformat(str(holdout_start))
    df = _bars_frame(bars or [])
    as_of = date.fromisoformat(str(record["as_of"])[:10])
    base = float((record.get("target_band") or {}).get("base_price", record.get("generated_price")))
    track = (execution or {}).get("quant_track") or {}
    action = track.get("execution_event")
    fill = track.get("fill_price")
    basis = "fill" if fill is not None else "virtual_fill"
    entry = float(fill) if fill is not None else base
    band = record.get("target_band") or {}
    direction = -1 if str(record.get("type", "BUY")).upper().startswith("SELL") else 1
    bench = benchmark_returns or {}

    out: dict[str, dict] = {}
    for name, h in horizons.items():
        maturity = add_trading_days(as_of, h)
        eligible = bool(holdout is None or maturity < holdout)
        if not eligible:
            cut = holdout
            last = df[df["date"] < cut]["date"].max() if not df.empty else None
            observed = trading_day_count(as_of, min(cut, last)) if last is not None and last > as_of else 0
            out[name] = {
                "horizon_td": h, "maturity": maturity.isoformat(), "eligible": False,
                "truncated": True, "observed_days": int(observed),
                "agreement": None, "return": None, "excess_return": None,
                "note": "홀드아웃 경계에서 잘림 — 참고용 표시만, 보정 목적함수 제외(4.2.1)",
            }
            continue
        win = _window(df, as_of, maturity)
        if win.empty:
            out[name] = {"horizon_td": h, "maturity": maturity.isoformat(), "eligible": True,
                         "truncated": False, "observed_days": 0, "agreement": None,
                         "return": None, "excess_return": None, "note": "가격 데이터 없음 — 채점 불가"}
            continue
        closes = [float(x) for x in win["close"]]
        end = closes[-1]
        r_base = end / base - 1.0
        r_fill = end / entry - 1.0
        p_min = float(win["low"].min()) if "low" in win else min(closes)
        p_max = float(win["high"].max()) if "high" in win else max(closes)
        mdd = realized_mdd(closes, entry)
        bench_ret = float(bench.get(name, bench.get(h, 0.0)) or 0.0)
        agreement = agreement_components(
            direction=direction,
            base_price=base,
            fill_price=entry,
            band_lo=float(band.get("lo", base)),
            band_hi=float(band.get("hi", base)),
            end_price=end,
            predicted_mdd=float(record.get("expected_mdd") or 0.0),
            realized_mdd=mdd if mdd is not None else 0.0,
            p_entry=entry,
            p_min=p_min,
            p_max=p_max,
        )
        out[name] = {
            "horizon_td": h,
            "maturity": maturity.isoformat(),
            "eligible": True,
            "truncated": False,
            "observed_days": int(len(win)),
            "basis": basis,
            "entry": round(entry, 4),
            "end_price": round(end, 4),
            "return": round(r_fill, 6),
            "return_base": round(r_base, 6),
            "benchmark_return": round(bench_ret, 6),
            "excess_return": round(r_fill - bench_ret, 6),
            "mdd": None if mdd is None else round(mdd, 6),
            "agreement": agreement,
            "virtual": fill is None,
        }
    return {
        "signal_id": record.get("signal_id"),
        "ticker": record.get("ticker"),
        "as_of": record["as_of"],
        "grade": record.get("grade"),
        "confidence": record.get("grade"),
        "action": action,
        "basis": basis,
        "horizons": out,
    }


def weighted_agreement(grades: list[dict], horizon: str = "3m") -> dict:
    """신뢰도(A:1.0 / B:0.7 / C:0.3) 가중 평균 Agreement — 분모 0 이면 None(0점으로 눈치주지 않는다)."""
    num = 0.0
    den = 0.0
    for g in grades:
        h = (g.get("horizons") or {}).get(horizon) or {}
        total = (h.get("agreement") or {}).get("total")
        if total is None:
            continue
        w = GRADE_WEIGHTS.get(str(g.get("confidence") or g.get("grade") or "C").upper()[:1], 0.3)
        num += w * total
        den += w
    return {
        "horizon": horizon,
        "weighted_agreement": None if den == 0 else round(num / den, 2),
        "n": int(den > 0 and sum(1 for g in grades if ((g.get("horizons") or {}).get(horizon) or {}).get("agreement", {}).get("total") is not None)),
        "weights": GRADE_WEIGHTS,
    }


def summarize_grades(grades: list[dict], horizon: str = "3m") -> dict:
    """폴드 요약 — 승률·손익비·평균 초과수익·Agreement·IC. 신호 0건도 정상(None)."""
    rows = []
    for g in grades:
        h = (g.get("horizons") or {}).get(horizon) or {}
        if h.get("agreement", {}).get("total") is None:
            continue
        rows.append({"score": g.get("entry_score", 0.0), "excess": h["excess_return"],
                     "agreement": h["agreement"]["total"]})
    if not rows:
        return {"horizon": horizon, "n": 0, "agreement_mean": None, "ic": None,
                "win_rate": None, "profit_factor": None, "mean_excess": None}
    excess = [r["excess"] for r in rows]
    wins = [x for x in excess if x > 0]
    losses = [x for x in excess if x <= 0]
    pf = None
    if losses and sum(losses) != 0:
        pf = round(sum(wins) / abs(sum(losses)), 4)
    scores = [r["score"] for r in rows]
    ic = None
    if len(rows) >= 3:
        s = pd.Series(scores).rank()
        e = pd.Series(excess).rank()
        if s.std(ddof=0) > 0 and e.std(ddof=0) > 0:
            ic = round(float(s.corr(e)), 4)
    return {
        "horizon": horizon,
        "n": len(rows),
        "agreement_mean": round(sum(r["agreement"] for r in rows) / len(rows), 2),
        "ic": ic,
        "win_rate": round(len(wins) / len(rows), 4),
        "profit_factor": pf,
        "mean_excess": round(sum(excess) / len(excess), 6),
    }
