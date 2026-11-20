"""engine.ledger.grade — 발행 기록 채점 (1주·1개월·3개월·6개월, 기획서 4.2.4·4.7.2).

[고정]
- 기간은 **거래일**(5 / 21 / 63 / 126). 달력 개월이 아니다.
- **만기 봉이 실제로 관측돼야 채점한다**(`complete`). 63거래일 만기에 하루치 가격만 넣으면
  `observed_days: 1` 로 조용히 채점되던 결함을 막는다 — 만기 봉이 없으면 `matured: false`.
- 수익률·벤치마크는 **`total_return_adj`**(분할+배당), 밴드 비교는 **`base_price` 기준 수익률**,
  타이밍(`P_min`·`P_max`)은 **`raw_as_traded`** 로 계산한다 — 한 계산 안에서 섞지 않는다(4.2.2).
- 채점 기준 봉은 **발행 시점에 고정**된다: 밴드는 `base_price`, `r_base` 도 **같은 봉의 종가**
  (`target_band.base_bar_date`)를 총수익 기준으로 환산해 쓴다 — 나중에 알게 되는 `as_of` 당일
  종가로 계산하면 한 계산 안에 두 기준이 섞인다(감사 09-d).
- 실제 체결이 있으면 **체결가를 총수익 기준으로 환산**해 쓴다(`tr_open × fill / raw_open`).
  예: 시가 $100·체결 $101·이후 불변 → `−0.99%`(예전에는 시가를 써서 0.0%).
- 체결일이 **만기일 이후(당일 포함)** 이면 채점하지 않는다
  (`maturity_unreached_or_post_maturity`) — 만기일에 체결된 건을 "1주 수익률"로 채점하던 경로를 막는다(09-c).
- 초과수익 = `r_i^fill − r_SPY`. 방향 점수 `D` 도 **초과수익**으로 판정한다(4.2.4).
  SPY 가 없으면 `D` 는 `None` 이고 가중치를 재정규화한다(0%로 대체하지 않는다).
- `maturity_date ≥ holdout_start` 인 폴드는 **적격이 아니다** — `truncated: true` + `observed_days` 로
  참고 표시만 하고 보정 목적함수에 넣지 않는다(4.2.1, 4.3).
- **[v1.3.4 · P0-4] 그 적격 판정은 `mode="calibration"` 에서만 적용한다.** Paper 운용(`mode="paper"`)은
  발행 때 고정한 기준 봉과 만기 봉 관측으로만 채점한다 — 이미 만기된 신호가 백테스트 홀드아웃 때문에
  `truncated` 로 빠지면 성적표·파일럿 표본이 통째로 비어 버린다(감사 P0-4).
- 체결이 없는 결정(`hold_cash`)도 **가상 체결가**로 채점한다(4.7.2).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from engine.backtest.walk_forward import agreement_components
from engine.utils.dates import add_trading_days, trading_day_count

HORIZONS_TD = {"1w": 5, "1m": 21, "3m": 63, "6m": 126}
GRADE_WEIGHTS = {"A": 1.0, "B": 0.7, "C": 0.3}
NOTE_NOT_MATURED = "만기 봉 미관측 — 채점 보류"
NOTE_POST_MATURITY = "체결일이 만기일 이후(또는 관측 기간 0일) — 채점 보류"
POST_MATURITY_REASON = "maturity_unreached_or_post_maturity"


def _normalize_bars(bars: list[dict]) -> pd.DataFrame:
    """봉 목록 → 채점용 프레임. 세 기준을 모두 담는다.

    `raw_*`(밴드·타이밍) · `sadj_*`(분할) · `adj_close`(총수익). 예전 형식(`close`/`high`/`low`/
    `open` 만 있는 목록)은 raw 로 읽고, 총수익 값이 없으면 raw 로 대체한다(배당·분할 없음과 같다).
    """
    df = pd.DataFrame(bars or [])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date").reset_index(drop=True)

    def pick(name: str, *alts: str) -> pd.Series | None:
        for col in (name, *alts):
            if col in df.columns:
                return df[col].astype(float)
        return None

    raw_close = pick("raw_close", "close")
    if raw_close is None:
        raise ValueError("봉에 종가가 없다 — raw_close 또는 close 가 필요하다")
    raw_open = pick("raw_open", "open")
    raw_high = pick("raw_high", "high")
    raw_low = pick("raw_low", "low")
    adj_close = pick("adj_close")
    sadj_close = pick("sadj_close")
    sadj_open = pick("sadj_open")
    tr_open = pick("tr_open")

    df["raw_close"] = raw_close
    df["raw_open"] = raw_open if raw_open is not None else raw_close
    df["raw_high"] = raw_high if raw_high is not None else raw_close
    df["raw_low"] = raw_low if raw_low is not None else raw_close
    df["adj_close"] = adj_close if adj_close is not None else raw_close
    df["sadj_close"] = sadj_close if sadj_close is not None else df["raw_close"]
    df["sadj_open"] = sadj_open if sadj_open is not None else df["raw_open"]
    if tr_open is not None:
        df["tr_open"] = tr_open
    else:
        ratio = df["adj_close"] / df["sadj_close"].where(df["sadj_close"] != 0)
        df["tr_open"] = df["sadj_open"] * ratio.fillna(1.0)
    df["tr_close"] = df["adj_close"]
    return df


def _window(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df.iloc[0:0]
    return df[(df["date"] > start) & (df["date"] <= end)]


def _row_on(df: pd.DataFrame, day: date, *, exact: bool = False) -> pd.Series | None:
    if df.empty:
        return None
    hit = df[df["date"] == day]
    if not hit.empty:
        return hit.iloc[-1]
    if exact:
        return None
    earlier = df[df["date"] <= day]
    return None if earlier.empty else earlier.iloc[-1]


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


def _fill_in_total_return(fill: float, entry_bar) -> tuple[float, str]:
    """실제 체결가를 **총수익 기준**으로 환산한다(기획서 4.2.4 `r_i^fill = P_end / fill_price − 1`).

    체결은 그 세션 **시가 근처**이므로 `tr_open`(같은 세션 시가의 총수익 기준 값)에
    `fill / raw_open` 을 곱하면 체결가의 총수익 기준 등가값이 된다. 예: 시가 $100·체결 $101·이후 불변
    → `tr_entry = tr_open × 1.01` → `−0.99%`(예전에는 시가를 써서 `0.0%`).
    """
    if entry_bar is None:
        return float(fill), "fill_price_raw"
    raw_open = float(entry_bar.get("raw_open") or 0.0)
    tr_open = float(entry_bar.get("tr_open") or 0.0)
    if raw_open > 0 and tr_open > 0:
        return tr_open * (float(fill) / raw_open), "fill_price_tr_normalized"
    return float(fill), "fill_price_raw"


def _entry_session_of(execution: dict | None, fill: float | None, as_of: date) -> date:
    """체결 세션(체결 이벤트의 `at` 날짜) — 없으면 신호일(`as_of`)."""
    if fill is None or not execution or not execution.get("at"):
        return as_of
    try:
        return date.fromisoformat(str(execution["at"])[:10])
    except ValueError:
        return as_of


def _benchmark_for(horizon_name: str, h: int, benchmark_returns) -> float | None:
    """신호별 벤치마크 — dict(기간별) 또는 float(전 기간 공통) 모두 받는다."""
    if benchmark_returns is None:
        return None
    if isinstance(benchmark_returns, (int, float)):
        return float(benchmark_returns)
    value = benchmark_returns.get(horizon_name, benchmark_returns.get(h))
    return None if value is None else float(value)


def grade_record(record: dict, execution: dict | None = None, bars: list[dict] | None = None,
                 benchmark_returns: dict[str, float] | float | None = None,
                 holdout_start: date | str | None = None,
                 horizons: dict[str, int] | None = None,
                 benchmark_return: float | None = None,
                 mode: str = "calibration") -> dict:
    """한 신호의 기간별 채점. 만기 봉이 없으면 그 기간은 `complete: false` 로 남긴다.

    `benchmark_returns` 는 **신호별·기간별 SPY 총수익**이다(dict). `benchmark_return` 은 전 기간에
    같은 값을 쓰는 편의 인자다.

    [v1.3.4 · P0-4] `mode` 로 **Paper 채점**과 **보정 코퍼스 자격**을 나른다.

    - `mode="paper"`(기본 운용): 발행 때 고정한 기준 봉과 **만기 봉 관측**으로만 판정한다.
      `holdout_start` 를 줘도 무시하며 `truncated` 는 항상 `False` — 이미 만기된 Paper 신호가
      백테스트 홀드아웃 경계 때문에 성적표에서 빠지던 결함을 없앤다(감사 P0-4).
    - `mode="calibration"`: 백테스트 보정용. 만기가 홀드아웃 경계를 거스르면 `truncated: true`,
      `eligible: false` 로 **참고 표시만** 한다(기존 동작 보존).
    """
    horizons = horizons or HORIZONS_TD
    mode = str(mode or "paper").lower()
    if mode not in ("paper", "calibration"):
        raise ValueError(f"알 수 없는 채점 모드: {mode!r} (paper | calibration)")
    holdout = None
    if mode != "paper" and holdout_start is not None:
        holdout = holdout_start if isinstance(holdout_start, date) else date.fromisoformat(str(holdout_start))
    df = _normalize_bars(bars or [])
    as_of = date.fromisoformat(str(record["as_of"])[:10])
    base = float((record.get("target_band") or {}).get("base_price", record.get("generated_price")))
    track = (execution or {}).get("quant_track") or {}
    action = track.get("execution_event")
    fill = track.get("fill_price")
    basis = "fill" if fill is not None else "virtual_fill"
    entry = float(fill) if fill is not None else base
    band = record.get("target_band") or {}
    band_lo = float(band.get("lo", base))
    band_hi = float(band.get("hi", base))
    r_lo = float(band["r_lo"]) if "r_lo" in band else band_lo / base - 1.0
    r_hi = float(band["r_hi"]) if "r_hi" in band else band_hi / base - 1.0
    direction = -1 if str(record.get("type", "BUY")).upper().startswith("SELL") else 1
    entry_session = _entry_session_of(execution, fill, as_of)
    last_bar = df["date"].max() if not df.empty else None
    as_of_bar = _row_on(df, as_of)
    # 기준 봉: 발행 때 고정한 `base_bar_date`(− 없으면 `as_of` 봉). 밴드와 `r_base` 가 같은 봉이다.
    base_bar_date = band.get("base_bar_date")
    try:
        base_day = date.fromisoformat(str(base_bar_date)[:10]) if base_bar_date else None
    except ValueError:
        base_day = None
    base_bar = _row_on(df, base_day, exact=True) if base_day else as_of_bar
    if base_bar is None:
        base_bar = as_of_bar

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
                "truncated": True, "matured": False, "complete": False, "observed_days": int(observed),
                "status": "truncated_for_calibration",
                "agreement": None, "return": None, "excess_return": None,
                "note": "홀드아웃 경계에서 잘린 — 참고용 표시만, 보정 목적함수 제외(4.2.1)",
            }
            continue
        win = _window(df, as_of, maturity)
        observed = int(len(win))
        # 만기 봉이 관측됐고, 그 구간의 봉 수가 거래일 정의와 같아야 채점한다.
        complete = bool(last_bar is not None and last_bar >= maturity and observed == h)
        # 실체결이 만기일 **당일 또는 이후**에 일어난 건은 관측 기간이 0일이라 채점할 수 없다(감사 09-c).
        if fill is not None and entry_session >= maturity:
            out[name] = {
                "horizon_td": h, "maturity": maturity.isoformat(), "eligible": True,
                "truncated": False, "matured": False, "complete": False,
                "observed_days": int(observed), "reason": POST_MATURITY_REASON,
                "status": "post_maturity_fill",
                "post_maturity_fill": True, "entry_session": entry_session.isoformat(),
                "agreement": None, "return": None, "excess_return": None,
                "note": NOTE_POST_MATURITY,
            }
            continue
        if not complete:
            out[name] = {
                "horizon_td": h, "maturity": maturity.isoformat(), "eligible": True,
                "truncated": False, "matured": False, "complete": False,
                "observed_days": observed,
                "status": "data_missing" if df.empty else "not_matured",
                "agreement": None, "return": None, "excess_return": None,
                "note": ("가격 데이터 없음 — 채점 불가" if df.empty else NOTE_NOT_MATURED),
            }
            continue

        tr_close_end = float(win["tr_close"].iloc[-1])
        raw_end = float(win["raw_close"].iloc[-1])
        tr_base = float(base_bar["tr_close"]) if base_bar is not None else base
        if fill is not None:
            tr_entry, tr_entry_source = _fill_in_total_return(float(fill), _row_on(df, entry_session, exact=True))
            if not tr_entry or tr_entry <= 0:
                tr_entry, tr_entry_source = float(fill), "fill_price_raw"
        else:
            tr_entry, tr_entry_source = (tr_base, "base_bar_tr_close") if tr_base > 0 else (base, "base_price_raw")
        r_fill = tr_close_end / tr_entry - 1.0
        r_base = tr_close_end / tr_base - 1.0 if tr_base > 0 else None
        p_min = float(win["raw_low"].min())
        p_max = float(win["raw_high"].max())
        tr_closes = [float(x) for x in win["tr_close"]]
        mdd = realized_mdd(tr_closes, tr_entry)
        bench_ret = _benchmark_for(name, h, benchmark_returns)
        if bench_ret is None and benchmark_return is not None:
            bench_ret = float(benchmark_return)
        # 밴드 점수는 base=1 로 정규화한 뒤 total_return_adj 수익률로 비교한다(기준 혼합 금지).
        end_norm = 1.0 + (r_base if r_base is not None else 0.0)
        fill_norm = end_norm / (1.0 + r_fill) if (1.0 + r_fill) != 0 else end_norm
        agreement = agreement_components(
            direction=direction,
            base_price=1.0,
            fill_price=fill_norm,
            band_lo=1.0 + r_lo,
            band_hi=1.0 + r_hi,
            end_price=end_norm,
            predicted_mdd=float(record.get("expected_mdd") or 0.0),
            realized_mdd=mdd if mdd is not None else 0.0,
            p_entry=entry,
            p_min=p_min,
            p_max=p_max,
            benchmark_return=bench_ret,
        )
        excess = None if bench_ret is None else r_fill - bench_ret
        out[name] = {
            "horizon_td": h,
            "maturity": maturity.isoformat(),
            "eligible": True,
            "truncated": False,
            "matured": True,
            "complete": True,
            "status": "complete",
            "observed_days": observed,
            "basis": basis,
            "return_basis": "total_return_adj",
            "entry": round(entry, 4),
            "entry_tr": round(tr_entry, 6),
            "entry_tr_source": tr_entry_source,
            "entry_session": entry_session.isoformat(),
            "base_bar_date": None if base_bar_date is None else str(base_bar_date)[:10],
            "base_price": round(base, 6),
            "end_price": round(raw_end, 4),
            "return": round(r_fill, 6),
            "return_base": None if r_base is None else round(r_base, 6),
            "benchmark_return": None if bench_ret is None else round(bench_ret, 6),
            "excess_return": None if excess is None else round(excess, 6),
            "benchmark_missing": bench_ret is None,
            "mdd": None if mdd is None else round(mdd, 6),
            "agreement": agreement,
            "virtual": fill is None,
        }
    complete_flags = {k: bool(v.get("complete")) for k, v in out.items()}
    return {
        "signal_id": record.get("signal_id"),
        "ticker": record.get("ticker"),
        "as_of": record["as_of"],
        "grade": record.get("grade"),
        "confidence": record.get("grade"),
        "mode": mode,
        "action": action,
        "basis": basis,
        "entry_session": entry_session.isoformat(),
        "complete": complete_flags,
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
        "n": int(den > 0 and sum(
            1 for g in grades
            if (((g.get("horizons") or {}).get(horizon) or {}).get("agreement") or {}).get("total") is not None)),
        "weights": GRADE_WEIGHTS,
    }


def summarize_grades(grades: list[dict], horizon: str = "3m") -> dict:
    """폴드 요약 — 승률·손익비·평균 초과수익·Agreement·IC. 신호 0건도 정상(None).

    SPY 벤치마크가 없어 초과수익을 계산하지 못한 신호는 **넣지 않는다**(0으로 대체하지 않는다).
    """
    rows = []
    for g in grades:
        h = (g.get("horizons") or {}).get(horizon) or {}
        if (h.get("agreement") or {}).get("total") is None:
            continue
        if h.get("excess_return") is None:
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
