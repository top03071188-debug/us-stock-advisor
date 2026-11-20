"""engine.ledger.pilot — 소액 파일럿 4지표 판정 (기획서 4.7.3).

[고정]
- 네 지표를 **함께** 본다. 하나만 골라 통과시키지 않는다 — 기록에 4개가 모두 있어야 판정이 난다.
- 표본이 기준 미만이거나 지표를 계산할 수 없으면 **"보류"** 다. 작은 표본의 숫자를 자신 있게
  보여 주는 것이 가장 위험하다(4.8.2 와 같은 원칙).
- **유효 표본 `N_eff` 는 체결(`buy_filled`) + 3개월 만기 채점 완료(`complete`)만 센다.**
  관찰(WATCH·`no_action`) 카드는 표본이 아니다 — 4.7.3 의 "진입일 기준"·"진입 후 3개월 경과"
  문언과 지표 ②③(계좌 성과·노출도)이 모두 체결 기준이기 때문이다. 발행 신호 수는 따로 남긴다.
- `55점·승률 52%` 는 참고치이지 게이트가 아니다. 판정은 아래 4개로만 한다.

| # | 지표 | 통과 기준(참고) |
|---|---|---|
| 1 | 유효 표본 `N_eff` (63거래일 클러스터) | ≥ 15 |
| 2 | 비용 후 순수익 (SPY 대비 − 비용) | > 0 |
| 3 | 유사 현금비중 벤치마크 대비 | > 0 |
| 4 | 손실 구간(하위 20% 평균 손실) | ≥ −8%, MDD 방어 |
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from engine.utils.dates import trading_day_count

CLUSTER_TD = 63
MIN_N_EFF = 15
TAIL_QUANTILE = 0.20
TAIL_FLOOR = -0.08
#: 왕복 비용 가정(4.7.3 ②): 슬리피지 10bp × 2 + 수수료 2bp × 2 + 환전 스프레드 5bp
DEFAULT_COST_BPS = {"slippage": 10.0, "commission": 2.0, "fx": 5.0}
VERDICT_HOLD = "보류"
VERDICT_CANDIDATE = "소액 파일럿 후보"


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def n_eff(records: list[dict], horizon_td: int = CLUSTER_TD) -> dict:
    """같은 종목의 신호가 `horizon_td` 거래일 안에 겹치면 하나의 클러스터(4.7.3 ①)."""
    by_ticker: dict[str, list[date]] = {}
    for record in records:
        ticker = str(record.get("ticker") or "")
        as_of = record.get("as_of")
        if not ticker or not as_of:
            continue
        by_ticker.setdefault(ticker, []).append(_as_date(as_of))
    clusters: dict[str, list[dict]] = {}
    for ticker, days in by_ticker.items():
        days.sort()
        groups: list[list[date]] = []
        for d in days:
            if groups and trading_day_count(groups[-1][0], d) < horizon_td:
                groups[-1].append(d)
            else:
                groups.append([d])
        clusters[ticker] = [{"start": g[0].isoformat(), "n": len(g)} for g in groups]
    total = sum(len(v) for v in clusters.values())
    return {
        "n_records": len(records),
        "n_eff": total,
        "horizon_td": horizon_td,
        "by_ticker": {t: v for t, v in sorted(clusters.items())},
        "clustered_out": len(records) - total,
    }


def eligible_samples(records: list[dict], graded: list[dict],
                     executions: dict[str, dict] | None = None,
                     horizon: str = "3m") -> list[dict]:
    """유효 표본 = **체결(`buy_filled`) + 3개월 만기 채점 완료(`complete`)**.

    `executions` 가 없으면 레코드의 `execution_event` 필드로 판정하고, 그것도 없으면 표본 0건이다
    — WATCH·`no_action` 카드를 표본으로 세지 않는다(조용한 통과 금지).
    """
    gmap = {str(g.get("signal_id")): g for g in graded or []}
    out: list[dict] = []
    for record in records or []:
        sid = str(record.get("signal_id"))
        event = (executions or {}).get(sid) or {}
        action = (event.get("quant_track") or {}).get("execution_event")
        if action is None:
            action = record.get("execution_event")
        if action != "buy_filled":
            continue
        horizon_row = ((gmap.get(sid) or {}).get("horizons") or {}).get(horizon) or {}
        if not horizon_row.get("complete"):
            continue
        out.append(record)
    return out


def total_cost(*, slippage_bps: float = 10.0, commission_bps: float = 2.0, fx_bps: float = 5.0,
               round_trips: float = 2.0) -> float:
    """비용 후 순수익에서 빼는 값(비율). 기본 왕복 2회 가정."""
    bps = (slippage_bps + commission_bps + fx_bps) * round_trips
    return round(bps / 10_000.0, 8)


def tail_loss(returns: list[float], quantile: float = TAIL_QUANTILE) -> dict:
    """수익률 하위 20% 의 평균 — 꼬리가 나쁘면 평균이 좋아도 파일럿을 열지 않는다."""
    values = sorted(float(r) for r in returns if r is not None)
    if not values:
        return {"bottom_mean": None, "cutoff": None, "n": 0, "pass": None}
    k = max(1, int(round(len(values) * quantile)))
    bottom = values[:k]
    mean = sum(bottom) / len(bottom)
    return {
        "bottom_mean": round(mean, 6),
        "cutoff": round(bottom[-1], 6),
        "n": k,
        "pass": bool(mean >= TAIL_FLOOR),
    }


def cash_equivalent_excess(portfolio_total_return: float | None, benchmark_return: float | None,
                           exposure: float | None, risk_free: float | None = 0.0) -> float | None:
    """실제 평균 노출도와 같은 비중의 `SPY×노출 + 단기국채×(1−노출)` 대비 초과(4.7.3 ③)."""
    if portfolio_total_return is None or benchmark_return is None or exposure is None:
        return None
    exp = max(0.0, min(1.0, float(exposure)))
    rf = float(risk_free or 0.0)
    mix = exp * float(benchmark_return) + (1.0 - exp) * rf
    return round(float(portfolio_total_return) - mix, 6)


def _metric(name: str, label_ko: str, value, threshold, passed: bool | None, note_ko: str | None = None) -> dict:
    return {"key": name, "label_ko": label_ko, "value": value, "threshold": threshold,
            "pass": passed, "note_ko": note_ko}


def evaluate_pilot(records: list[dict], graded: list[dict], portfolio: dict | None = None,
                   benchmark_return: float | None = None, risk_free: float | None = 0.0,
                   horizon: str = "3m", costs: dict | None = None,
                   executions: dict[str, dict] | None = None) -> dict:
    """4지표 + 판정. 계산할 수 없는 지표는 `None` 으로 남기고 판정은 "보류"."""
    costs = costs or DEFAULT_COST_BPS
    eligible = eligible_samples(records, graded, executions, horizon=horizon)
    eligible_ids = {str(r.get("signal_id")) for r in eligible}
    returns: list[float] = []
    excess: list[float] = []
    mdds: list[float] = []
    for row in graded or []:
        if str(row.get("signal_id")) not in eligible_ids:
            continue
        h = ((row.get("horizons") or {}).get(horizon) or {})
        if h.get("return") is not None:
            returns.append(float(h["return"]))
        if h.get("excess_return") is not None:
            excess.append(float(h["excess_return"]))
        if h.get("mdd") is not None:
            mdds.append(float(h["mdd"]))

    ne = n_eff(eligible)
    cost = total_cost(slippage_bps=costs.get("slippage", 10.0),
                      commission_bps=costs.get("commission", 2.0),
                      fx_bps=costs.get("fx", 5.0))
    net = None if not excess else round(sum(excess) / len(excess) - cost, 6)
    exposure = None if not portfolio else portfolio.get("avg_exposure")
    strat_total = None if not portfolio else portfolio.get("total_return")
    cash_excess = cash_equivalent_excess(strat_total, benchmark_return, exposure, risk_free)
    tail = tail_loss(returns)
    no_sample = not eligible

    metrics = [
        _metric("n_eff", "유효 표본 N_eff", ne["n_eff"], f"≥ {MIN_N_EFF}",
                bool(ne["n_eff"] >= MIN_N_EFF),
                f"체결·3개월 만기 {ne['n_eff']}개(발행 신호 {len(records or [])}건 중 유효 {len(eligible)}건)"),
        _metric("net_after_cost", "비용 후 순수익(SPY 대비)", net, "> 0",
                None if net is None else bool(net > 0),
                ("유효 표본 0건 — 계산 불가" if no_sample else
                 f"평균 초과 {None if not excess else round(sum(excess) / len(excess), 6)} − 비용 {cost}")),
        _metric("cash_equivalent", "유사 현금비중 벤치마크 대비", cash_excess, "> 0",
                None if cash_excess is None else bool(cash_excess > 0),
                ("유효 표본 0건 — 계산 불가" if no_sample else
                 (None if exposure is None else f"평균 노출 {round(float(exposure), 4)} 혼합 대비"))),
        _metric("tail_loss", "손실 구간(하위 20%) 평균", tail["bottom_mean"], f"≥ {TAIL_FLOOR}",
                tail["pass"],
                ("유효 표본 0건 — 계산 불가" if no_sample else
                 (None if not mdds else f"구간 MDD 최악 {round(min(mdds), 6)}"))),
    ]
    computable = [m for m in metrics if m["value"] is not None]
    all_pass = len(computable) == 4 and all(bool(m["pass"]) for m in metrics)
    missing = [m["key"] for m in metrics if m["value"] is None]
    if len(computable) < 4:
        verdict = VERDICT_HOLD
        reason = f"지표 {4 - len(computable)}개 계산 불가({', '.join(missing)}) — 판정 보류(4.7.3)"
    elif all_pass:
        verdict = VERDICT_CANDIDATE
        reason = "4지표 모두 통과 — 소액 파일럿(총자산 5% 이내) 후보"
    else:
        failed = [m["key"] for m in metrics if not m["pass"]]
        verdict = VERDICT_HOLD
        reason = f"미달 지표: {', '.join(failed)} — 보류"
    return {
        "schema_version": 1,
        "as_of": date.today().isoformat(),
        "horizon": horizon,
        "n_records": len(records or []),
        "n_eligible": len(eligible),
        "n_graded": len(returns),
        "n_eff": ne["n_eff"],
        "metrics": metrics,
        "verdict": verdict,
        "verdict_reason_ko": reason,
        "benchmark_return": benchmark_return,
        "risk_free": risk_free,
        "costs": costs,
        "note_ko": "55점·승률 52% 는 참고치다 — 게이트는 이 4지표로만 판단한다.",
    }


def pilot_summary(ev: dict) -> dict:
    """대시보드 성적표 탭용 압축 값(5.4 소액 파일럿 4지표)."""
    return {
        "verdict": ev.get("verdict"),
        "reason_ko": ev.get("verdict_reason_ko"),
        "n_eff": ev.get("n_eff"),
        "n_records": ev.get("n_records"),
        "n_eligible": ev.get("n_eligible"),
        "metrics": [
            {"key": m["key"], "label_ko": m["label_ko"], "value": m["value"],
             "threshold": m["threshold"], "pass": m["pass"], "note_ko": m.get("note_ko")}
            for m in ev.get("metrics", [])
        ],
    }


def write_pilot_eval(ev: dict, path: str | Path = "data/ledger/pilot_eval.json") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_pilot_eval(path: str | Path = "data/ledger/pilot_eval.json") -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
