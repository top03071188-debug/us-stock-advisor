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
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
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
