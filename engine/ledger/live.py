"""engine.ledger.live — 실시간 Paper Ledger 운영 (기획서 4.7.1·4.7.2·4.12).

매일 postclose 실행에서 하는 일:

1. **발행 의도 정산** — 전일 신호(`quant_intent: pending_buy`)의 시가가 확정됐으면
   `data/ledger/executions/{signal_id}.json` 에 **체결 이벤트를 append** 한다.
   발행 레코드(`records.jsonl`)는 한 글자도 고치지 않는다.
2. **모의 계좌 상태 갱신** — 체결을 재생해 수량·평단을 만들고 당일 종가로 평가한다.
   **수량·평단·현금 금액은 커밋 파일에 넣지 않는다**(`shares`·`avg_cost`·`cash_balance` 는
   공개 금지 키, 2.2.3). 커밋되는 상태 파일에는 **비중(%)만** 남긴다.
3. **만기 도래 채점** — 1주·1개월·3개월·6개월(거래일 5/21/63/126) 채점. 만기 봉이 관측되지
   않으면 채점하지 않고 `complete: false` 로 남긴다. 홀드아웃 경계를 넘는 구간은 `truncated`
   로 참고 표시만 한다(4.2.1).

[고정]
- 체결은 **발행일의 대상 세션(주문 마감이 아직 오지 않은 가장 이른 세션) 시가 + 슬리피지 10bp**,
  1차는 **장 전 커밋 범위**(`entry_band`) 안에서만 성립한다(LA-4·3.4.4·2.2.2).
- 체결 디렉터리에는 두 종류의 파일이 섞인다: `{signal_id}.json`(체결 이벤트)과
  `{signal_id}.reanchor.json`(분할가 재고정). **계좌는 체결 이벤트만** 읽는다(R01).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from engine.backtest.execution import EntryBand, band_from_dict, opening_fill
from engine.ledger import grade as grade_mod
from engine.decide.sizing import recheck_planned_loss
from engine.ledger.record import EXECUTION_EVENTS, execution_event, reanchor_event, superseded_ids

INITIAL_CASH = 1_000_000.0
SLIPPAGE_BPS = 10.0
EXECUTIONS_DIR = Path("data/ledger/executions")
PORTFOLIO_DIR = Path("data/ledger/portfolio")
HISTORY_FILE = "history.jsonl"
RECORDS_PATH = Path("data/ledger/records.jsonl")

EXECUTION_SUFFIX = ".json"
REANCHOR_SUFFIX = ".reanchor.json"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ────────────────────────────── 이벤트 로더 (R01) ──────────────────────────────
def load_executions(root: str | Path = EXECUTIONS_DIR) -> dict[str, dict]:
    """`{signal_id}.json` **체결 이벤트만** 읽는다(없으면 빈 dict).

    같은 디렉터리의 `{signal_id}.reanchor.json` 은 `signal_id` 가 같고 정렬상 뒤에 오기 때문에,
    예전 로더는 재고정 이벤트로 체결 이벤트를 **덮어써** 체결이 사라진 것처럼 보였다.
    방어선은 두 겹이다: ① 파일명 접미사 ② `quant_track.execution_event` 타입 검사.
    """
    root = Path(root)
    out: dict[str, dict] = {}
    if not root.exists():
        return out
    for path in sorted(root.glob(f"*{EXECUTION_SUFFIX}")):
        if path.name.endswith(REANCHOR_SUFFIX):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        track = payload.get("quant_track") or {}
        if track.get("execution_event") not in EXECUTION_EVENTS:
            continue
        out[str(payload.get("signal_id") or path.stem)] = payload
    return out


def load_reanchors(root: str | Path = EXECUTIONS_DIR) -> dict[str, dict]:
    """`plan_reanchor` 이벤트만 — **손익에 쓰지 않는다**(표시·계획 재고정 전용)."""
    root = Path(root)
    out: dict[str, dict] = {}
    if not root.exists():
        return out
    for path in sorted(root.glob(f"*{REANCHOR_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("execution_event") != "plan_reanchor":
            continue
        out[str(payload.get("signal_id") or path.stem)] = payload
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
    return band_from_dict(record.get("entry_band") or {})


def _session_of(record: dict) -> date | None:
    """레코드의 `target_session`(주문 대상 거래일). 없으면 `None` — 구 레코드 폴백은 `settle` 이 한다."""
    value = record.get("target_session")
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def settle(records: list[dict], executions: dict[str, dict], panels: dict,
           as_of: date, slippage_bps: float = SLIPPAGE_BPS) -> dict:
    """발행 의도 → 체결 이벤트. 시가가 확정되지 않은 신호는 `pending` 으로 남긴다.

    체결 세션은 **레코드의 `target_session`** 이다 — `next_trading_day(signal_day)` 로 밀면
    금요일 신호가 월요일 시가에 체결된다(R08). 그 세션 봉이 없으면 대체 세션을 쓰지 않고
    `SESSION_BAR_MISSING` 으로 보류한다.
    """
    new_events: list[dict] = []
    reanchors: list[dict] = []
    pending: list[dict] = []
    settled: list[dict] = []
    superseded = superseded_ids(records)
    for record in records:
        sid = str(record.get("signal_id"))
        if sid in executions:
            continue
        if sid in superseded:
            # 같은 날 재실행으로 새 회차가 확정된 이전 발행본 — 체결하지 않는다(중복 매수 방지, 감사 02).
            settled.append({"signal_id": sid, "event": "superseded", "reason": "SUPERSEDED_BY_NEWER_RELEASE"})
            continue
        intent = record.get("quant_intent")
        if intent != "pending_buy":
            continue
        ticker = record.get("ticker")
        panel = panels.get(ticker)
        signal_day = date.fromisoformat(str(record["as_of"])[:10])
        session = _session_of(record)
        legacy = session is None
        if legacy:
            session = _next_trading_day(signal_day)
        if session is None:
            pending.append({"signal_id": sid, "ticker": ticker, "expected_fill_day": None,
                            "reason": "NO_SESSION"})
            continue
        if session > as_of:
            pending.append({"signal_id": sid, "ticker": ticker,
                            "expected_fill_day": session.isoformat()})
            continue
        stamp = pd.Timestamp(session)
        if panel is None or stamp not in panel.frame.index:
            pending.append({"signal_id": sid, "ticker": ticker,
                            "expected_fill_day": session.isoformat(),
                            "reason": "SESSION_BAR_MISSING"})
            continue
        bar = panel.frame.loc[stamp]
        band_dict = record.get("entry_band") or {}
        band = band_from_dict(band_dict)
        migrated = bool(band_dict) and "stop_min_sadj" not in band_dict
        if band is None:
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(session), execution_event="no_action",
                                    note="entry_band 없음 — 주문 범위를 확인할 수 없어 체결하지 않는다")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "BAND_MISSING"})
            continue
        factor_open = float(bar["split_factor"]) if "split_factor" in bar else 1.0
        result = opening_fill(band, float(bar["raw_open"]), slippage_bps,
                              split_factor_open=factor_open)
        if result["execution_event"] != "buy_filled":
            event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                    at=_open_at(session), execution_event="no_action",
                                    note=f"시가 {result['open_price']:.2f} 가 주문 범위를 벗어남")
            new_events.append(public_event(event))
            settled.append({"signal_id": sid, "event": "no_action", "reason": "outside_order_range",
                            "target_session": session.isoformat(), "legacy_session": legacy,
                            "band_basis_migrated": migrated})
            continue
        fill = float(result["fill_price"])
        position = record.get("position") or {}
        stop_distance = float(position.get("stop_distance") or 0.0)
        stop_price = round(fill * (1.0 - stop_distance), 4)
        planned_loss = round(float(position.get("plan_weight_target") or 0.0) * stop_distance, 6)
        event = execution_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                at=_open_at(session), execution_event="buy_filled", tranche=1,
                                fill_price=fill, slippage_bps=slippage_bps,
                                weight=float(position.get("tranche_weight") or 0.0),
                                stop_price=stop_price, trailing_high=result["open_price"],
                                planned_loss_pct=planned_loss, shares=None,
                                note="대상 세션 시가 체결(발행 레코드는 수정하지 않는다)")
        new_events.append(public_event(event))
        plan = _reanchor_plan(record, fill, stop_price)
        reanchors.append(reanchor_event(signal_id=sid, snapshot_id=record.get("snapshot_id", ""),
                                        at=_open_at(session), first_fill=fill,
                                        stop_price=stop_price, plan=plan))
        settled.append({"signal_id": sid, "event": "buy_filled", "fill_price": fill,
                        "target_session": session.isoformat(), "legacy_session": legacy,
                        "band_basis_migrated": migrated})
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


def replay(records: list[dict], executions: dict[str, dict],
           reanchors: dict[str, dict] | None = None) -> dict:
    """체결 이벤트를 재생해 모의 계좌(수량·평단)를 만든다 — 메모리에서만 쓴다.

    `reanchors` 는 **손익에 영향을 주지 않는다.** 재고정된 분할가를 표시용으로만 붙인다.
    """
    reanchors = reanchors or {}
    cash = INITIAL_CASH
    positions: dict[str, dict] = {}
    displayed: dict[str, dict] = {}
    for record in records:
        sid = str(record.get("signal_id"))
        if sid in reanchors:
            displayed[sid] = {k: reanchors[sid].get(k) for k in ("first_fill", "stop_price", "plan", "at")}
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
    return {"cash": cash, "positions": positions, "plan_reanchored": displayed}


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


# ────────────────────────────── 채점 (R02) ──────────────────────────────
def bars_for(panel, as_of: date, limit: int | None = None) -> list[dict]:
    """채점용 봉 — 세 기준을 함께 낸다.

    - `raw_*`: 밴드·타이밍(체결·손절과 같은 기준)
    - `sadj_*`: 분할 조정
    - `adj_close`·`tr_open`: 수익률·벤치마크(`total_return_adj`)

    `limit` 기본값은 `None` 이다 — 6개월(126거래일) 만기 채점이 200봉 창에 아슬아슬하게
    걸리면 만기 봉 판정이 흔들린다.
    """
    if panel is None or panel.frame.empty:
        return []
    frame = panel.frame.loc[panel.frame.index <= pd.Timestamp(as_of)]
    if limit:
        frame = frame.tail(limit)
    out = []
    for idx, row in frame.iterrows():
        sadj_close = float(row["sadj_close"])
        sadj_open = float(row["sadj_open"])
        adj_close = float(row["adj_close"])
        ratio = (adj_close / sadj_close) if sadj_close else 1.0
        out.append({
            "date": idx.date() if hasattr(idx, "date") else idx,
            "raw_open": float(row["raw_open"]),
            "raw_high": float(row["raw_high"]),
            "raw_low": float(row["raw_low"]),
            "raw_close": float(row["raw_close"]),
            "sadj_open": sadj_open,
            "sadj_close": sadj_close,
            "adj_close": adj_close,
            "split_factor": float(row["split_factor"]) if "split_factor" in row else 1.0,
            "tr_open": sadj_open * ratio,
            "tr_close": adj_close,
        })
    return out


def _tr_frame(panel) -> pd.DataFrame:
    frame = panel.frame
    if frame.empty:
        return pd.DataFrame(columns=["tr_open", "tr_close"])
    sadj_close = frame["sadj_close"].astype(float)
    sadj_open = frame["sadj_open"].astype(float)
    adj = frame["adj_close"].astype(float)
    ratio = (adj / sadj_close.where(sadj_close != 0)).fillna(1.0)
    return pd.DataFrame({"tr_open": sadj_open * ratio, "tr_close": adj}, index=frame.index)


def benchmark_returns_for(benchmark_panel, entry_session: date | None,
                          maturities: dict[str, date], virtual: bool = False) -> dict[str, float]:
    """SPY **총수익**을 종목과 **같은 진입 세션·같은 만기 봉**으로 계산한다(4.7.2).

    종목 수익률과 같은 규칙(`tr_close(maturity)/tr_open(entry) − 1`, 가상 체결이면
    `tr_close(maturity)/tr_close(as_of) − 1`)을 쓴다. 만기 봉이 없으면 그 기간은 넣지 않는다.
    """
    if benchmark_panel is None:
        return {}
    tr = _tr_frame(benchmark_panel)
    if tr.empty:
        return {}
    if entry_session is None:
        return {}
    start = None
    if virtual:
        # 가상 체결은 신호일 종가에서 시작한다(종목 수익률과 같은 규칙)
        exact = tr.loc[tr.index == pd.Timestamp(entry_session)]
        start = float(exact["tr_close"].iloc[-1]) if not exact.empty else None
    else:
        exact = tr.loc[tr.index == pd.Timestamp(entry_session)]
        start = float(exact["tr_open"].iloc[-1]) if not exact.empty else None
    if not start or start <= 0:
        return {}
    out: dict[str, float] = {}
    for name, maturity in maturities.items():
        stamp = pd.Timestamp(maturity)
        if stamp not in tr.index:
            continue
        out[name] = float(tr.loc[stamp, "tr_close"]) / start - 1.0
    return out


def grade_all(records: list[dict], executions: dict[str, dict], panels: dict, as_of: date,
              holdout_start: str | None = None, benchmark_panel=None,
              horizons: dict[str, int] | None = None) -> list[dict]:
    """모든 발행 레코드를 기간별로 채점한다(만기 봉이 없으면 `complete: false`).

    벤치마크는 **신호마다** 그 신호의 진입 세션·만기 봉으로 계산한다 — 배치 실행일 기준
    5/21/63/126일 SPY 하나를 모든 신호에 돌려주면 초과수익이 신호와 무관해진다.
    """
    from engine.utils.dates import add_trading_days

    hz = horizons or grade_mod.HORIZONS_TD
    graded: list[dict] = []
    superseded = superseded_ids(records)
    for record in records:
        ticker = record.get("ticker")
        sid = str(record.get("signal_id"))
        execution = executions.get(sid)
        if sid in superseded and not execution:
            continue            # 대체된 회차(체결 없음) — 최신 발행본만 채점한다
        track = (execution or {}).get("quant_track") or {}
        fill = track.get("fill_price")
        signal_day = date.fromisoformat(str(record["as_of"])[:10])
        entry_session = signal_day
        if fill is not None and execution and execution.get("at"):
            try:
                entry_session = date.fromisoformat(str(execution["at"])[:10])
            except ValueError:
                entry_session = signal_day
        bars = bars_for(panels.get(ticker), as_of)
        maturities = {name: add_trading_days(signal_day, h) for name, h in hz.items()}
        bench = benchmark_returns_for(benchmark_panel, entry_session, maturities,
                                      virtual=fill is None)
        result = grade_mod.grade_record(record, execution, bars=bars, benchmark_returns=bench,
                                        holdout_start=holdout_start, horizons=hz)
        result["entry_score"] = record.get("entry_score")
        result["sector"] = record.get("sector")
        result["params_version"] = record.get("params_version")
        graded.append(result)
    return graded


def benchmark_period_return(panel, start: date | None, as_of: date) -> float | None:
    """평가 기간 SPY 총수익(파일럿 ③) — `tr_open(첫 진입일) → tr_close(as_of)`."""
    if panel is None:
        return None
    tr = _tr_frame(panel)
    if tr.empty:
        return None
    end_rows = tr.loc[tr.index <= pd.Timestamp(as_of)]
    if end_rows.empty:
        return None
    end = float(end_rows["tr_close"].iloc[-1])
    if start is None:
        start_rows = end_rows
    else:
        exact = tr.loc[tr.index == pd.Timestamp(start)]
        start_rows = exact if not exact.empty else tr.loc[tr.index <= pd.Timestamp(start)]
    if start_rows.empty:
        return None
    begin = float(start_rows["tr_open"].iloc[-1])
    if not begin or begin <= 0:
        return None
    return end / begin - 1.0


# ────────────────────────────── 요약·기록 ──────────────────────────────
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
            "avg_exposure": portfolio.get("avg_exposure"),
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
    """공개 계좌 스냅샷 — `state.json` 과 `latest.json` 에 같은 내용을 쓴다.

    `latest.json` 은 화면·문서가 가리키는 이름이고, `state.json` 은 기존 계약 이름이다.
    둘 다 **비중·상태만** 담는다(`shares`·`avg_cost`·`cash_balance` 금지, 2.2.3).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2)
    path = root / "state.json"
    path.write_text(text, encoding="utf-8")
    (root / "latest.json").write_text(text, encoding="utf-8")
    return path


HISTORY_FIELDS = ("as_of", "cash_pct", "invested_pct", "equity_index", "n_positions")


def read_portfolio_history(root: str | Path = PORTFOLIO_DIR) -> list[dict]:
    """`data/ledger/portfolio/history.jsonl` — 하루 1행 **비중만**(공개 허용 목록 안전)."""
    path = Path(root) / HISTORY_FILE
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_portfolio_history(state: dict, root: str | Path = PORTFOLIO_DIR) -> Path:
    """일별 비중을 append-only 로 남긴다 — 같은 `as_of` 는 다시 쓰지 않는다(멱등)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / HISTORY_FILE
    row = {k: state.get(k) for k in HISTORY_FIELDS}
    if row.get("as_of") is None:
        return path
    if any(r.get("as_of") == row["as_of"] for r in read_portfolio_history(root)):
        return path
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def avg_exposure_from_history(rows: list[dict], start: date | None = None,
                              as_of: date | None = None) -> float | None:
    """평균 노출도 — 첫 진입일부터 `as_of` 까지 `invested_pct` 의 평균.

    단일 시점 `invested_pct` 를 평균 자리에 대입하지 않는다(R11). 기록이 없으면 `None`.
    """
    values = []
    for row in rows or []:
        day = row.get("as_of")
        if not day:
            continue
        try:
            d = date.fromisoformat(str(day)[:10])
        except ValueError:
            continue
        if start is not None and d < start:
            continue
        if as_of is not None and d > as_of:
            continue
        if row.get("invested_pct") is None:
            continue
        values.append(float(row["invested_pct"]))
    if not values:
        return None
    return round(sum(values) / len(values), 6)


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
    path = Path(root) / f"{event['signal_id']}{REANCHOR_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def benchmark_trailing_returns(panels: dict) -> dict:
    """SPY 가 데이터셋에 있으면 **배치 실행일 기준** 최근 5·21·63·126거래일 총수익을 돌려준다.

    참고 표시 전용이다 — 신호별 채점에는 쓰지 않는다(`grade_all` 은 `benchmark_returns_for`
    로 신호마다 같은 구간을 계산한다). 이름을 바꾼 이유가 그것이다(옛 `beta_free_benchmark`).
    """
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


# ══════════════════════ 실시간 2·3차 분할 · 손절/분할익절 정산 (Phase 4) ══════════════════════
#
# 기획서 3.5 · 4.12 의 "한 거래일의 처리 순서"를 실시간 Paper Ledger 에 그대로 적용한다.
#   1) 기업 이벤트(분할) → 2) 갭 손절 → 3) 장중 **손절 먼저** → 4) 2·3차 지정가 →
#   5) 분할익절(IV_high·RSI) → 6) 종가 트레일링 갱신.
# 전량 청산(S5·S3·EXIT)이 나면 **같은 봉의 잔여 2·3차 주문을 즉시 취소**하고
# `cancelled_by` 를 남긴다 — 그러지 않으면 손절로 다 팔고 같은 날 3차가 체결돼 포지션이 다시 열린다.

PROFIT_TAKE_FRACTION = 1.0 / 3.0
TRANCHE_DROPS_LIVE = (0.0, -0.07, -0.14)
HEALTH_PATH = Path("data/health.json")


def _tranche_plan(record: dict, reanchor: dict | None, first_fill: float,
                  initial_stop: float, params: dict | None) -> list[dict]:
    """2·3차 주문 상태 — 재고정(`plan_reanchor`)이 있으면 그 가격을 쓴다(실제 1차 체결가 기준)."""
    scaling = (params or {}).get("portfolio") or {}
    weights = list(scaling.get("tranches") or [0.40, 0.30, 0.30])
    drops = list(scaling.get("tranche_drops") or TRANCHE_DROPS_LIVE)
    plan = list((reanchor or {}).get("plan") or [])
    by_index = {int(row.get("tranche")): row for row in plan if row.get("tranche") is not None}
    rows = []
    for index, weight in zip((1, 2, 3), weights):
        row = by_index.get(index) or {}
        if index == 1:
            rows.append({"tranche": 1, "weight": float(weight), "price_ref": round(first_fill, 4),
                         "status": "filled", "filled_at": None, "cancelled_by": None, "note": None})
            continue
        drop = float(drops[index - 1]) if len(drops) >= index else TRANCHE_DROPS_LIVE[index - 1]
        price = float(row.get("price_ref") if row.get("price_ref") is not None else first_fill * (1.0 + drop))
        status = "pending"
        note = None
        if index == 3 and price <= initial_stop:
            status = "invalid"
            note = "3차 지정가가 초기 손절가 이하 → 무효(3.5)"
        elif row.get("active") is False:
            status = "invalid"
            note = row.get("note") or "손절선 밖 → 무효"
        elif row.get("note"):
            note = row.get("note")
        rows.append({"tranche": index, "weight": float(weight), "price_ref": round(price, 4),
                     "status": status, "filled_at": None, "cancelled_by": None, "note": note})
    return rows


def _indicator_lines(panel) -> dict:
    """봉별 200일선·RSI(14) — 3차 200일선 조건과 분할익식 RSI 조건에 쓴다(sadj 기준)."""
    if panel is None:
        return {}
    try:
        from engine.metrics.technical import dma, rsi14
        from engine.models import PriceKind
        series = panel.series(PriceKind.split_adj_ohlc)
        line200 = dma(series, 200)
        rsi = rsi14(series, 14)
    except Exception:  # noqa: BLE001 — 지표를 못 만들면 조건을 쓰지 않는다(체결 규칙은 유지)
        return {}
    out = {}
    for idx in panel.frame.index:
        d200 = line200.get(idx)
        r = rsi.get(idx)
        out[idx.date()] = (None if d200 is None or pd.isna(d200) else float(d200),
                           None if r is None or pd.isna(r) else float(r))
    return out


def settle_positions(records: list[dict], executions: dict[str, dict],
                     reanchors: dict[str, dict] | None, panels: dict, as_of: date,
                     params: dict | None = None, *, forced_exits: dict[str, str] | None = None,
                     initial_cash: float = INITIAL_CASH,
                     slippage_bps: float = SLIPPAGE_BPS) -> dict:
    """1차 체결 이후의 2·3차 분할·손절·분할익절을 봉 단위로 정산한다.

    발행 레코드·체결 이벤트는 고치지 않는다 — 여기서 만드는 것은 **파생 상태**(화면·건강 지표)다.
    `forced_exits` 는 `pipeline` 이 계산한 S1~S5/EXIT 판정(`{ticker: "S3"|"S5"|"EXIT"}`)으로,
    `as_of` 봉 시가에 전량 청산하고 잔여 분할을 취소한다.
    """
    reanchors = reanchors or {}
    forced_exits = forced_exits or {}
    risk = (params or {}).get("risk") or {}
    risk_per_trade = float(risk.get("risk_per_trade") or 0.0075)
    max_position = float(risk.get("max_position") or 0.05)
    superseded = superseded_ids(records)
    states: dict[str, dict] = {}
    events: list[dict] = []
    cancelled: list[dict] = []

    for record in records or []:
        sid = str(record.get("signal_id"))
        if sid in superseded:
            continue
        ticker = record.get("ticker")
        panel = panels.get(ticker)
        if panel is None or panel.frame.empty:
            continue
        event = executions.get(sid) or {}
        track = event.get("quant_track") or {}
        if track.get("execution_event") != "buy_filled" or track.get("fill_price") is None:
            continue
        bars = bars_for(panel, as_of)
        if not bars:
            continue
        fill_day = None
        if event.get("at"):
            try:
                fill_day = date.fromisoformat(str(event["at"])[:10])
            except ValueError:
                fill_day = None
        if fill_day is None:
            fill_day = _session_of(record)
        if fill_day is None:
            continue
        dates = [bar["date"] for bar in bars]
        start = next((i for i, d in enumerate(dates) if d >= fill_day), None)
        if start is None:
            continue
        fill = float(track["fill_price"])
        first_fill = float((reanchors.get(sid) or {}).get("first_fill") or fill)
        stop_distance = float((record.get("position") or {}).get("stop_distance") or 0.0)
        initial_stop = float((reanchors.get(sid) or {}).get("stop_price") or first_fill * (1.0 - stop_distance))
        tranches = _tranche_plan(record, reanchors.get(sid), first_fill, initial_stop, params)
        weight = float(track.get("weight") or (record.get("position") or {}).get("tranche_weight") or 0.0)
        qty = (weight * initial_cash / fill) if fill > 0 else 0.0
        if qty <= 0:
            continue
        valuation = record.get("valuation") or {}
        iv_high = valuation.get("iv_high") if not record.get("valuation_suspended") else None
        st = {
            "signal_id": sid, "ticker": ticker, "sector": record.get("sector") or "",
            "opened_at": fill_day.isoformat(), "first_fill": round(first_fill, 4),
            "qty": qty, "avg_cost": fill, "equity_base": initial_cash,
            "stop_distance": round(stop_distance, 6), "initial_stop": round(initial_stop, 4),
            "stop_price": round(initial_stop, 4), "trailing_high": fill,
            "iv_high": None if iv_high is None else float(iv_high),
            "atr_pct": (record.get("indicators") or {}).get("atr_pct"),
            "tranches": tranches, "trim_done": [], "trim_stage": "none",
            "cash_spent": weight * initial_cash, "cash_returned": 0.0,
            "exit": None, "last_price": fill, "pending_sell_fraction": 0.0,
            "last_event_ko": "1차 체결",
        }
        tranches[0]["filled_at"] = fill_day.isoformat()
        lines = _indicator_lines(panel)
        factor_prev = float(bars[start].get("split_factor") or 1.0)
        cash_left = float(track.get("weight") or 0.0) * initial_cash
        for bar in bars[start:]:
            day = bar["date"]
            if st["exit"] is not None:
                break
            # 1) 기업 이벤트 — 분할을 먼저 적용한다(수량·가격만 조정, 손절 거리는 그대로)
            factor = float(bar.get("split_factor") or 1.0)
            if factor > 0 and abs(factor - factor_prev) > 1e-12:
                ratio = factor_prev / factor
                st["qty"] *= ratio
                st["avg_cost"] /= ratio
                st["stop_price"] /= ratio
                st["initial_stop"] /= ratio
                st["trailing_high"] /= ratio
                st["first_fill"] /= ratio
                for row in st["tranches"]:
                    if row.get("price_ref") is not None:
                        row["price_ref"] = round(float(row["price_ref"]) / ratio, 4)
                events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                               "event": "split_adjustment", "ratio": round(ratio, 6)})
            factor_prev = factor
            # 2) 전일 종가 조건으로 예약된 매도(RSI 75 · 200일선 이탈) → 오늘 시가
            if st["pending_sell_fraction"] > 0 and st["exit"] is None:
                fraction = st["pending_sell_fraction"]
                st["pending_sell_fraction"] = 0.0
                if fraction >= 0.999:
                    st["exit"] = {"reason": "S4", "at": day.isoformat(), "price": round(float(bar["raw_open"]), 4)}
                    st["cash_returned"] += st["qty"] * float(bar["raw_open"]) * (1.0 - slippage_bps / 1e4)
                    events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                                   "event": "exit_filled", "reason": "S4",
                                   "price": round(float(bar["raw_open"]), 4)})
                    _cancel_pending(st, sid, ticker, day, "S4", cancelled)
                    st["last_event_ko"] = "200일선 이탈 — 잔여 청산(S4)"
                    break
                sold = st["qty"] * fraction
                st["qty"] -= sold
                st["cash_returned"] += sold * float(bar["raw_open"]) * (1.0 - slippage_bps / 1e4)
                st["trim_stage"] = "rsi"
                events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                               "event": "profit_take", "reason": "rsi", "fraction": round(fraction, 4),
                               "price": round(float(bar["raw_open"]), 4)})
                if st["qty"] <= 1e-9:
                    st["exit"] = {"reason": "S2", "at": day.isoformat(), "price": round(float(bar["raw_open"]), 4)}
                    break
            # 3) 전량 청산 판정(pipeline) — as_of 봉 시가에 청산하고 잔여 분할 취소
            if day == as_of and forced_exits.get(ticker) and st["exit"] is None:
                reason = str(forced_exits[ticker])
                st["exit"] = {"reason": reason, "at": day.isoformat(), "price": round(float(bar["raw_open"]), 4)}
                st["cash_returned"] += st["qty"] * float(bar["raw_open"]) * (1.0 - slippage_bps / 1e4)
                events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                               "event": "exit_filled", "reason": reason,
                               "price": round(float(bar["raw_open"]), 4)})
                _cancel_pending(st, sid, ticker, day, reason, cancelled)
                st["last_event_ko"] = f"{reason} — 잔여 분할 즉시 취소"
                break
            # 4) 손절 먼저 — 갭이면 시가, 아니면 손절가
            if float(bar["raw_open"]) <= st["stop_price"]:
                _stop_out(st, sid, ticker, day, float(bar["raw_open"]), "S5", cancelled, events,
                          slippage_bps)
                break
            if float(bar["raw_low"]) <= st["stop_price"]:
                _stop_out(st, sid, ticker, day, float(st["stop_price"]), "S5", cancelled, events,
                          slippage_bps)
                break
            # 5) 전일 종가 조건(RSI ≤ 25)으로 예약된 2차 매수 → 오늘 시가(3.4.4 표)
            if st.get("pending_buy"):
                index = int(st.pop("pending_buy"))
                row = next((r for r in st["tranches"] if int(r["tranche"]) == index), None)
                if row is not None and row["status"] == "pending":
                    cash_left = _fill_tranche(st, row, sid, ticker, day, float(bar["raw_open"]),
                                              "rsi_next_open", cash_left, risk_per_trade,
                                              max_position, events)
            # 5b) 2·3차 지정가 — 손절이 나지 않은 봉에서만(장중 순서는 '손절 먼저')
            line200, _rsi = lines.get(day, (None, None))
            for row in st["tranches"]:
                if row["status"] != "pending":
                    continue
                index = int(row["tranche"])
                price_ref = float(row["price_ref"])
                triggered = index == 2 and float(bar["raw_low"]) <= price_ref
                if index == 3:
                    by_limit = float(bar["raw_low"]) <= price_ref
                    by_dma = line200 is not None and float(bar["sadj_close"]) <= line200 * 0.97
                    triggered = by_limit or by_dma
                    if by_dma and not by_limit:
                        price_ref = float(bar["raw_open"])     # 200일선 조건은 다음 거래일 시가 체결
                if not triggered:
                    continue
                if price_ref <= st["initial_stop"]:
                    row["status"] = "invalid"
                    row["note"] = "지정가가 초기 손절가 이하 → 무효"
                    continue
                cash_left = _fill_tranche(st, row, sid, ticker, day, price_ref, "limit",
                                          cash_left, risk_per_trade, max_position, events)
            # 6) 분할익절 — IV_high 1/3
            if (st["iv_high"] is not None and float(bar["raw_high"]) >= st["iv_high"]
                    and "iv_high" not in st["trim_done"] and st["qty"] > 0):
                sold = st["qty"] * PROFIT_TAKE_FRACTION
                st["qty"] -= sold
                st["cash_returned"] += sold * float(st["iv_high"]) * (1.0 - slippage_bps / 1e4)
                st["trim_done"].append("iv_high")
                st["trim_stage"] = "iv_high"
                st["last_event_ko"] = "IV_high 도달 — 1/3 분할익절"
                events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                               "event": "profit_take", "reason": "iv_high",
                               "fraction": round(PROFIT_TAKE_FRACTION, 4),
                               "price": round(float(st["iv_high"]), 4)})
            # 7) 종가 — 트레일링 갱신, 전일 종가 조건 예약
            st["trailing_high"] = max(st["trailing_high"], float(bar["raw_high"]))
            st["stop_price"] = max(st["stop_price"],
                                  st["trailing_high"] * (1.0 - st["stop_distance"]))
            st["last_price"] = float(bar["raw_close"])
            _line200, rsi = lines.get(day, (None, None))
            if (rsi is not None and rsi <= 25.0
                    and any(int(r["tranche"]) == 2 and r["status"] == "pending" for r in st["tranches"])):
                st["pending_buy"] = 2           # 전일 RSI ≤ 25 → 다음 거래일 시가 2차 체결
            if rsi is not None and rsi >= 75.0 and "rsi" not in st["trim_done"]:
                st["trim_done"].append("rsi")
                st["pending_sell_fraction"] = PROFIT_TAKE_FRACTION
            if line200 is not None and float(bar["sadj_close"]) < line200 and st["trim_stage"] != "none":
                st["pending_sell_fraction"] = 1.0
        # 청산되지 않았으면 마지막 종가로 평가한다
        if st["exit"] is None:
            st["stop_price"] = max(st["stop_price"], st["initial_stop"])
        states[sid] = st

    by_ticker: dict[str, dict] = {}
    for sid, st in sorted(states.items(), key=lambda kv: (kv[1]["opened_at"], kv[0])):
        ticker = st["ticker"]
        if ticker not in by_ticker:
            by_ticker[ticker] = {**st, "signal_ids": [sid], "qty": st["qty"],
                                 "cash_spent": st["cash_spent"], "cash_returned": st["cash_returned"]}
            continue
        agg = by_ticker[ticker]
        total = agg["qty"] + st["qty"]
        agg["avg_cost"] = ((agg["avg_cost"] * agg["qty"] + st["avg_cost"] * st["qty"]) / total) if total else 0.0
        agg["qty"] = total
        agg["cash_spent"] += st["cash_spent"]
        agg["cash_returned"] += st["cash_returned"]
        agg["stop_price"] = max(agg["stop_price"], st["stop_price"])
        agg["trailing_high"] = max(agg["trailing_high"], st["trailing_high"])
        agg["signal_ids"].append(sid)
        if agg["exit"] is None:
            agg["exit"] = st["exit"]
        agg["tranches"] = st["tranches"]
        agg["trim_stage"] = st["trim_stage"]
        agg["trim_done"] = sorted(set(agg["trim_done"]) | set(st["trim_done"]))
        agg["last_price"] = st["last_price"]

    cash = initial_cash - sum(st["cash_spent"] for st in states.values()) + \
        sum(st["cash_returned"] for st in states.values())
    health = {
        "positions_open": sum(1 for st in states.values() if st["exit"] is None),
        "tranches_filled": sum(1 for st in states.values() for row in st["tranches"]
                               if row["status"] == "filled"),
        "tranches_pending": sum(1 for st in states.values() for row in st["tranches"]
                                if row["status"] == "pending"),
        "stop_count": sum(1 for st in states.values() if (st["exit"] or {}).get("reason") == "S5"),
        "trim_count": sum(1 for event in events if event.get("event") == "profit_take"),
        "stop_reentry_blocked_count": len(cancelled),
        "blocked_reentries": cancelled[:50],
    }
    return {
        "as_of": as_of.isoformat(),
        "positions": by_ticker,
        "by_signal": states,
        "events": events,
        "cancelled": cancelled,
        "health": health,
        "cash": round(cash, 4),
        "initial_cash": initial_cash,
        "holdings": {
            t: {"ticker": t, "shares": st["qty"], "avg_cost": st["avg_cost"],
                "stop_price": st["stop_price"], "trailing_high": st["trailing_high"],
                "initial_stop": st["initial_stop"], "tranches_filled":
                sorted({int(row["tranche"]) for row in st["tranches"] if row["status"] == "filled"})}
            for t, st in by_ticker.items() if st["exit"] is None and st["qty"] > 1e-9
        },
    }


def _fill_tranche(st: dict, row: dict, sid: str, ticker: str, day: date, price: float,
                  reason: str, cash_left: float, risk_per_trade: float, max_position: float,
                  events: list[dict]) -> float:
    """분할 체결 직전 **계획 손실 재검증**(3.8) → 통과하면 체결, 초과하면 축소/거절.

    Returns: 남은 현금(체결했으면 줄어든 값).
    """
    index = int(row["tranche"])
    price = float(price)
    if price <= 0:
        return cash_left
    want = float(row["weight"]) * float(st["equity_base"]) / price
    check = recheck_planned_loss(st["qty"], st["avg_cost"], st["stop_price"], want, price,
                                 float(st["equity_base"]), cash=cash_left,
                                 max_position=max_position, risk_per_trade=risk_per_trade)
    if check["action"] == "reject":
        row["status"] = "rejected"
        row["note"] = "체결 전 계획 손실 재검증 초과 — 수량 축소 불가"
        events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                       "event": "tranche_rejected", "tranche": index,
                       "reason": check.get("reason")})
        return cash_left
    add = float(check["allowed_shares"])
    notional = add * price
    if notional > cash_left:
        return cash_left
    cash_left -= notional
    st["avg_cost"] = (st["avg_cost"] * st["qty"] + add * price) / (st["qty"] + add)
    st["qty"] += add
    st["cash_spent"] += notional
    row["status"] = "filled"
    row["filled_at"] = day.isoformat()
    st["last_event_ko"] = f"{index}차 분할 체결"
    events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                   "event": "buy_filled", "tranche": index, "price": round(price, 4),
                   "weight": round(row["weight"], 6), "recheck": check["action"],
                   "reason": reason})
    return cash_left


def _stop_out(st: dict, sid: str, ticker: str, day: date, price: float, reason: str,
              cancelled: list[dict], events: list[dict], slippage_bps: float) -> None:
    """전량 손절 + 같은 봉 잔여 2·3차 주문 취소(3.5 [고정])."""
    st["exit"] = {"reason": reason, "at": day.isoformat(), "price": round(float(price), 4)}
    st["cash_returned"] += st["qty"] * float(price) * (1.0 - slippage_bps / 1e4)
    events.append({"date": day.isoformat(), "ticker": ticker, "signal_id": sid,
                   "event": "exit_filled", "reason": reason, "price": round(float(price), 4)})
    _cancel_pending(st, sid, ticker, day, reason, cancelled)
    st["last_event_ko"] = f"{reason} 손절 — 잔여 분할 즉시 취소"


def _cancel_pending(st: dict, sid: str, ticker: str, day: date, reason: str,
                    cancelled: list[dict]) -> None:
    for row in st["tranches"]:
        if row["status"] != "pending":
            continue
        row["status"] = "cancelled"
        row["cancelled_by"] = reason
        cancelled.append({"ticker": ticker, "signal_id": sid, "tranche": int(row["tranche"]),
                          "cancelled_by": reason, "at": day.isoformat()})


def mark_positions(settlement: dict, panels: dict, as_of: date) -> dict:
    """정산 상태 → **공개용 비중 요약**(수량·평단·금액 없음, 2.2.3)."""
    initial = float(settlement.get("initial_cash") or INITIAL_CASH)
    cash = float(settlement.get("cash") or initial)
    rows = []
    holdings_value = 0.0
    for ticker, st in sorted(settlement.get("positions", {}).items()):
        if st.get("exit") is not None or float(st.get("qty") or 0.0) <= 1e-9:
            continue
        panel = panels.get(ticker)
        price = float(st.get("last_price") or st.get("avg_cost") or 0.0)
        if panel is not None and not panel.frame.empty:
            price = float(panel.frame.iloc[-1]["raw_close"])
        value = float(st["qty"]) * price
        holdings_value += value
        stop = float(st.get("stop_price") or 0.0)
        rows.append({
            "ticker": ticker,
            "sector": st.get("sector") or "",
            "opened_at": st.get("opened_at"),
            "tranches_filled": sorted({int(r["tranche"]) for r in st.get("tranches") or []
                                       if r.get("status") == "filled"}),
            "tranches": [{k: r.get(k) for k in ("tranche", "weight", "price_ref", "status",
                                                "filled_at", "cancelled_by", "note")}
                         for r in st.get("tranches") or []],
            "stop_distance": round(float(st.get("stop_distance") or 0.0), 4),
            "stop_price": round(stop, 4),
            "stop_headroom_pct": (None if price <= 0 else round((price - stop) / price, 4)),
            "trailing": {"active": bool(stop > float(st.get("initial_stop") or 0.0)),
                         "high_gain_pct": (None if not st.get("first_fill")
                                           else round(float(st.get("trailing_high") or 0.0)
                                                      / float(st["first_fill"]) - 1.0, 4)),
                         "stop_distance": round(float(st.get("stop_distance") or 0.0), 4)},
            "trim_stage": st.get("trim_stage") or "none",
            "trim_done": list(st.get("trim_done") or []),
            "exit": st.get("exit"),
            "last_event_ko": st.get("last_event_ko"),
            "value": value,
        })
    equity = cash + holdings_value
    total = equity if equity > 0 else initial
    positions = [{
        "ticker": r["ticker"], "sector": r["sector"], "opened_at": r["opened_at"],
        "tranches_filled": r["tranches_filled"], "tranches": r["tranches"],
        "stop_distance": r["stop_distance"], "stop_price": r["stop_price"],
        "stop_headroom_pct": r["stop_headroom_pct"], "trailing": r["trailing"],
        "trim_stage": r["trim_stage"], "trim_done": r["trim_done"],
        "last_event_ko": r["last_event_ko"],
        "weight": round(r["value"] / total, 6),
    } for r in rows]
    sector_weights: dict[str, float] = {}
    for p in positions:
        sector_weights[p["sector"]] = round(sector_weights.get(p["sector"], 0.0) + p["weight"], 6)
    return {
        "as_of": as_of.isoformat(),
        "cash_pct": round(cash / total, 6),
        "invested_pct": round(holdings_value / total, 6),
        "equity_index": round(equity / initial, 6),
        "positions": positions,
        "n_positions": len(positions),
        "sector_weights": sector_weights,
        "note_ko": "모의 계좌(초기 100만 달러) — 수량·평단·금액은 공개 산출물에 넣지 않는다.",
    }


def write_health(health: dict, as_of: date, *, path: str | Path = HEALTH_PATH,
                 status: str | None = None, degraded: list | None = None,
                 extra: dict | None = None) -> Path:
    """`data/health.json` — 규칙이 실제로 작동했는지 보는 건강 지표(3.5)."""
    payload = {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "generated_at": _iso(datetime.now(timezone.utc)),
        "status": status,
        "degraded": list(degraded or []),
        **{k: health.get(k) for k in ("positions_open", "tranches_filled", "tranches_pending",
                                      "stop_count", "trim_count", "stop_reentry_blocked_count")},
        "blocked_reentries": list(health.get("blocked_reentries") or []),
        "note_ko": "손절 직후 재진입 차단 건수는 같은 봉에서 취소된 2·3차 주문 수다(3.5).",
    }
    payload.update(extra or {})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def position_checks_from(settlement: dict) -> list[dict]:
    """정산 상태 → `evaluate_exit` 입력(보유 dict). `pipeline` 이 S1~S5 를 다시 계산한다."""
    out = []
    for ticker, st in sorted(settlement.get("positions", {}).items()):
        if st.get("exit") is not None or float(st.get("qty") or 0.0) <= 1e-9:
            continue
        out.append({
            "ticker": ticker, "sector": st.get("sector") or "",
            "shares": st["qty"], "avg_cost": st["avg_cost"],
            "stop_price": st.get("stop_price"), "trailing_high": st.get("trailing_high"),
            "initial_stop": st.get("initial_stop"),
            "tranche": 1, "tranches_filled": sorted({int(r["tranche"]) for r in st.get("tranches") or []
                                                    if r.get("status") == "filled"}),
            "stop_distance": st.get("stop_distance"),
        })
    return out
