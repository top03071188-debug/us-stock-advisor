"""engine.ledger.live — 실시간 Paper Ledger 운영 (기획서 4.7.1·4.7.2·4.12).

매일 postclose 실행에서 하는 일:

1. **발행 의도 정산** — 전일 신호(`quant_intent: pending_buy`)의 시가가 확정됐으면
   `data/ledger/executions/{signal_id}.json` 에 **체결 이벤트를 append** 한다.
   발행 레코드(`records.jsonl`)는 한 글자도 고치지 않는다.
2. **모의 계좌 상태 갱신** — 체결을 재생해 수량·평단을 만들고 당일 종가로 평가한다.
   **수량·평단·현금 금액은 커밋 파일에 넣지 않는다**(`shares`·`avg_cost`·`cash_balance` 는
   공개 금지 키, 2.2.3). 커밋되는 상태 파일에는 **비중(%)만** 남긴다.
3. **만기 도래 채점** — 1주·1개월·3개월·6개월(거래일 5/21/63/126) 채점. 홀드아웃 경계를
   넘는 구간은 `truncated` 로 참고 표시만 한다(4.2.1).

[고정] 체결은 **T+1 시가 + 슬리피지 10bp**, 1차는 **장 전 커밋 범위**(`entry_band`) 안에서만
성립한다(LA-4·3.4.4).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from engine.backtest.execution import EntryBand, opening_fill
from engine.ledger import grade as grade_mod
from engine.ledger.record import execution_event, reanchor_event

INITIAL_CASH = 1_000_000.0
SLIPPAGE_BPS = 10.0
EXECUTIONS_DIR = Path("data/ledger/executions")
PORTFOLIO_DIR = Path("data/ledger/portfolio")
RECORDS_PATH = Path("data/ledger/records.jsonl")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_executions(root: str | Path = EXECUTIONS_DIR) -> dict[str, dict]:
    """`{signal_id}.json` 체결 이벤트를 모두 읽는다(없으면 빈 dict)."""
    root = Path(root)
    out: dict[str, dict] = {}
    if not root.exists():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        sid = payload.get("signal_id") or path.stem
        out[str(sid)] = payload
    return out


def public_event(event: dict) -> dict:
    """커밋용 체결 이벤트 — 공개 금지 키(`shares`)를 남기지 않는다.

    실수량은 `data/ledger/executions/{signal_id}.full.jsonl`(gitignore)과 메모리 상태에만 있다.
    """
    track = dict(event.get("quant_track") or {})
    if track.get("shares") is None:
        track.pop("shares", None)
    out = {k: v for k, v in event.items() if k != "quant_track"}
    out["quant_track"] = track
    return out


def _band_of(record: dict) -> EntryBand | None:
    band = record.get("entry_band")
    if not band:
        return None
    try:
        return EntryBand(stop_min=float(band["stop_min"]), limit_max=float(band["limit_max"]),
                         dma200_floor=float(band["dma200_floor"]),
                         provisional_stop=float(band["provisional_stop"]),
                         required_margin=float(band["required_margin"]))
    except (KeyError, TypeError, ValueError):
        return None


def settle(records: list[dict], executions: dict[str, dict], panels: dict,
           as_of: date, slippage_bps: float = SLIPPAGE_BPS) -> dict:
    """발행 의도 → 체결 이벤트. 시가가 확정되지 않은 신호는 `pending` 으로 남긴다."""
    new_events: list[dict] = []
    reanchors: list[dict] = []
    pending: list[dict] = []
    settled: list[dict] = []
    for record in records:
        sid = str(record.get("signal_id"))
        if sid in executions:
            continue
        intent = record.get("quant_intent")
        if intent != "pending_buy":
            continue
        ticker = record.get("ticker")
        panel = panels.get(ticker)
        signal_day = date.fromisoformat(str(record["as_of"])[:10])
        fill_day = _next_trading_day(signal_day)
        if fill_day is None or fill_day > as_of:
            pending.append({"signal_id": sid, "ticker": ticker,
                            "expected_fill_day": None if fill_day is None else fill_day.isoformat()})
            continue
        stamp = pd.Timestamp(fill_day)
        if panel is None or stamp not in panel.frame.index:
            pending.append({"signal_id": sid, "ticker": ticker,
                            "expected_fill_day": fill_day.isoformat(),
                            "reason": "NO_BAR"})
            continue
        bar = panel.frame.loc[stamp]
        band = _band_of(record)
        if band is None:
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(fill_day), execution_event="no_action",
                                    note="entry_band 없음 — 주문 범위를 확인할 수 없어 체결하지 않는다")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "BAND_MISSING"})
            continue
        result = opening_fill(band, float(bar["raw_open"]), slippage_bps)
        if result["execution_event"] != "buy_filled":
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(fill_day), execution_event="no_action",
                                    note=f"시가 {result['open_price']:.2f} 가 주문 범위를 벗어남")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "outside_order_range"})
            continue
        fill = float(result["fill_price"])
        position = record.get("position") or {}
        stop_distance = float(position.get("stop_distance") or 0.0)
        stop_price = round(fill * (1.0 - stop_distance), 4)
        planned_loss = round(float(position.get("plan_weight_target") or 0.0) * stop_distance, 6)
        event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                at=_open_at(fill_day), execution_event="buy_filled", tranche=1,
                                fill_price=fill, slippage_bps=slippage_bps,
                                weight=float(position.get("tranche_weight") or 0.0),
                                stop_price=stop_price, trailing_high=result["open_price"],
                                planned_loss_pct=planned_loss, shares=None,
                                note="T+1 시가 체결(발행 레코드는 수정하지 않는다)")
        new_events.append(public_event(event))
        plan = _reanchor_plan(record, fill, stop_price)
        reanchors.append(reanchor_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                        at=_open_at(fill_day), first_fill=fill,
                                        stop_price=stop_price, plan=plan))
        settled.append({"signal_id": sid, "event": "buy_filled", "fill_price": fill})
    return {"new_events": new_events, "reanchors": reanchors, "pending": pending, "settled": settled}


def _next_trading_day(day: date) -> date | None:
    from engine.utils.dates import next_trading_day
    return next_trading_day(day)


def _open_at(day: date) -> datetime:
    from engine.utils.dates import market_open_at
    return market_open_at(day)


def _reanchor_plan(record: dict, fill: float, stop_price: float) -> list[dict]:
    rows = []
    for row in record.get("entry_plan") or []:
        t = int(row["tranche"])
        price = round(fill * (1.0 - 0.07 * (t - 1)), 2)
        rows.append({"tranche": t, "price_ref": price, "provisional": False,
                     "active": price > stop_price, "note": None if price > stop_price else "손절선 밖 → 무효"})
    return rows


def replay(records: list[dict], executions: dict[str, dict]) -> dict:
    """체결 이벤트를 재생해 모의 계좌(수량·평단)를 만든다 — 메모리에서만 쓴다."""
    cash = INITIAL_CASH
    positions: dict[str, dict] = {}
    for record in records:
        sid = str(record.get("signal_id"))
        event = executions.get(sid)
        if not event:
            continue
        track = event.get("quant_track") or {}
        if track.get("execution_event") != "buy_filled" or track.get("fill_price") is None:
            continue
        ticker = record.get("ticker")
        price = float(track["fill_price"])
        weight = float(track.get("weight") or (record.get("position") or {}).get("tranche_weight") or 0.0)
        notional = max(0.0, weight) * INITIAL_CASH
        qty = notional / price if price > 0 else 0.0
        if qty <= 0:
            continue
        pos = positions.setdefault(ticker, {
            "ticker": ticker, "sector": record.get("sector") or "", "qty": 0.0, "avg_price": 0.0,
            "stop_price": float(track.get("stop_price") or 0.0),
            "stop_distance": float((record.get("position") or {}).get("stop_distance") or 0.0),
            "opened_at": str(record.get("as_of")), "tranches_filled": [], "signal_ids": [],
        })
        prev_cost = pos["qty"] * pos["avg_price"]
        pos["qty"] += qty
        pos["avg_price"] = (prev_cost + notional) / pos["qty"] if pos["qty"] else price
        pos["stop_price"] = max(pos["stop_price"], float(track.get("stop_price") or 0.0))
        pos["tranches_filled"].append(int(track.get("tranche") or 1))
        pos["signal_ids"].append(sid)
        cash -= notional
    return {"cash": cash, "positions": positions}


def mark(state: dict, panels: dict, as_of: date, executions: dict[str, dict] | None = None,
         records: list[dict] | None = None) -> dict:
    """당일 종가로 평가 → **공개용 비중 요약**(수량·평단 없음).

    청산(`exit_filled`)된 종목은 보유에서 뺀다.
    """
    executions = executions or {}
    cash = float(state["cash"])
    rows: list[dict] = []
    holdings_value = 0.0
    for ticker, pos in sorted(state["positions"].items()):
        exited = False
        for sid in pos.get("signal_ids", []):
            event = executions.get(sid) or {}
            if (event.get("quant_track") or {}).get("execution_event") == "exit_filled":
                exited = True
                cash += float(pos["qty"]) * float((event.get("quant_track") or {}).get("fill_price") or 0.0)
        if exited:
            continue
        panel = panels.get(ticker)
        price = pos["avg_price"]
        if panel is not None and not panel.frame.empty:
            price = float(panel.frame.iloc[-1]["raw_close"])
        value = float(pos["qty"]) * price
        holdings_value += value
        rows.append({
            "ticker": ticker,
            "sector": pos.get("sector") or "",
            "opened_at": pos.get("opened_at"),
            "tranches_filled": sorted(set(pos.get("tranches_filled") or [])),
            "stop_distance": round(float(pos.get("stop_distance") or 0.0), 4),
            "value": value,
        })
    equity = cash + holdings_value
    total = equity if equity > 0 else INITIAL_CASH
    positions = [{
        "ticker": r["ticker"], "sector": r["sector"], "opened_at": r["opened_at"],
        "tranches_filled": r["tranches_filled"], "stop_distance": r["stop_distance"],
        "weight": round(r["value"] / total, 6),
    } for r in rows]
    sector_weights: dict[str, float] = {}
    for p in positions:
        sector_weights[p["sector"]] = round(sector_weights.get(p["sector"], 0.0) + p["weight"], 6)
    return {
        "as_of": as_of.isoformat(),
        "cash_pct": round(cash / total, 6),
        "invested_pct": round(holdings_value / total, 6),
        "equity_index": round(equity / INITIAL_CASH, 6),
        "positions": positions,
        "n_positions": len(positions),
        "sector_weights": sector_weights,
        "note_ko": "모의 계좌(초기 100만 달러) — 수량·평단·금액은 공개 산출물에 넣지 않는다.",
    }


def bars_for(panel, as_of: date, limit: int = 200) -> list[dict]:
    """채점용 봉 — **체결·손절과 같은 기준**(`raw_as_traded`)."""
    if panel is None or panel.frame.empty:
        return []
    frame = panel.frame.loc[panel.frame.index <= pd.Timestamp(as_of)].tail(limit)
    out = []
    for idx, row in frame.iterrows():
        out.append({"date": idx.date() if hasattr(idx, "date") else idx,
                    "close": float(row["raw_close"]),
                    "high": float(row["raw_high"]),
                    "low": float(row["raw_low"])})
    return out


def grade_all(records: list[dict], executions: dict[str, dict], panels: dict, as_of: date,
              holdout_start: str | None = None, benchmark_returns: dict | None = None) -> list[dict]:
    """모든 발행 레코드를 기간별로 채점한다(만기 전 구간은 `unavailable`)."""
    graded: list[dict] = []
    for record in records:
        ticker = record.get("ticker")
        bars = bars_for(panels.get(ticker), as_of)
        result = grade_mod.grade_record(record, executions.get(str(record.get("signal_id"))),
                                        bars=bars, benchmark_returns=benchmark_returns or {},
                                        holdout_start=holdout_start)
        result["entry_score"] = record.get("entry_score")
        result["sector"] = record.get("sector")
        result["params_version"] = record.get("params_version")
        if not benchmark_returns:
            for horizon in result["horizons"].values():
                horizon["excess_return"] = None
                horizon["benchmark_missing"] = True
        graded.append(result)
    return graded


def summary(graded: list[dict], portfolio: dict, pilot: dict | None, folds: dict | None = None,
            params_history: list[dict] | None = None, benchmark_connected: bool = False) -> dict:
    """성적표 요약(`web/data/ledger/summary.json` 의 원본) — 신호 정확도와 계좌 성과를 분리."""
    horizons: dict[str, dict] = {}
    for name in ("1w", "1m", "3m", "6m"):
        stats = grade_mod.summarize_grades(graded, horizon=name)
        weighted = grade_mod.weighted_agreement(graded, horizon=name)
        horizons[name] = {
            "horizon": name,
            "n": stats["n"],
            "agreement": weighted["weighted_agreement"],
            "agreement_mean": stats["agreement_mean"],
            "ic": stats["ic"],
            "win_rate": stats["win_rate"],
            "mean_excess": stats["mean_excess"],
            "profit_factor": stats["profit_factor"],
        }
    returns = []
    for g in graded:
        h = (g.get("horizons") or {}).get("3m") or {}
        if h.get("excess_return") is not None:
            returns.append({"signal_id": g.get("signal_id"), "ticker": g.get("ticker"),
                            "excess": h["excess_return"], "return": h.get("return"),
                            "action": g.get("action")})
    valid = [r for r in returns]
    wins = [r for r in valid if r["excess"] > 0]
    return {
        "schema_version": 1,
        "as_of": portfolio.get("as_of"),
        "n_signals": len(graded),
        "n_graded_3m": len(valid),
        "accuracy": {
            "label_ko": "신호 정확도",
            "agreement_3m": horizons["3m"]["agreement"],
            "ic_3m": horizons["3m"]["ic"],
            "win_rate": None if not valid else round(len(wins) / len(valid), 4),
            "mean_excess": None if not valid else round(sum(r["excess"] for r in valid) / len(valid), 6),
            "benchmark_connected": benchmark_connected,
            "note_ko": None if benchmark_connected else "SPY 벤치마크 미연결 — 초과수익을 표시하지 않는다",
        },
        "strategy": {
            "label_ko": "전략(모의 계좌)",
            "equity_index": portfolio.get("equity_index"),
            "cash_pct": portfolio.get("cash_pct"),
            "invested_pct": portfolio.get("invested_pct"),
            "avg_exposure": portfolio.get("invested_pct"),
            "n_positions": portfolio.get("n_positions"),
            "note_ko": "신호가 맞아도 현금이 놀면 계좌 성과는 다르다(4.12).",
        },
        "horizons": horizons,
        "folds": folds or {},
        "pilot": pilot or {"verdict": "보류", "reason_ko": "파일럿 지표 미산출"},
        "params_history": params_history or [],
        "signal_history": sorted(returns, key=lambda r: str(r["signal_id"]), reverse=True)[:200],
    }


def write_public_state(state: dict, root: str | Path = PORTFOLIO_DIR) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def append_full_event(event: dict, shares: float | None, root: str | Path = "data/ledger/executions") -> None:
    """실수량 포함 원본 이벤트 — gitignore 경로(`*.full.jsonl`)에만 남긴다."""
    path = Path(root) / f"{event['signal_id']}.full.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload["quant_track"] = {**(event.get("quant_track") or {}), "shares": shares}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def write_execution(event: dict, root: str | Path = EXECUTIONS_DIR) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{event['signal_id']}.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old != event:
            raise ValueError(f"{event['signal_id']} 체결 이벤트가 다른 내용으로 이미 기록돼 있다 — append-only")
        return path
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_reanchor(event: dict, root: str | Path = "data/ledger/executions") -> Path:
    path = Path(root) / f"{event['signal_id']}.reanchor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def beta_free_benchmark(panels: dict) -> dict:
    """SPY 가 데이터셋에 있으면 기간별 총수익을 돌려준다. 없으면 빈 dict(미연결)."""
    spy = panels.get("SPY")
    if spy is None or spy.frame.empty:
        return {}
    closes = spy.frame["adj_close"] if "adj_close" in spy.frame else spy.frame["sadj_close"]
    out: dict[str, float] = {}
    for name, days in (("1w", 5), ("1m", 21), ("3m", 63), ("6m", 126)):
        if len(closes) > days:
            out[name] = float(closes.iloc[-1] / closes.iloc[-1 - days] - 1.0)
    return out


def stats_from_curve(index_points: list[float]) -> dict:
    """정규화 자산곡선 → 간단 통계(있으면 CAGR·MDD)."""
    if len(index_points) < 2:
        return {"cagr": None, "mdd": None}
    arr = np.asarray(index_points, dtype=float)
    peak = np.maximum.accumulate(arr)
    mdd = float((arr / peak - 1.0).min())
    return {"cagr": None, "mdd": round(mdd, 6)}


def missing_benchmark_note() -> str:
    return "SPY 총수익 벤치마크가 데이터셋에 없어 초과수익·IC 를 계산하지 않았다(정직 표기)."
