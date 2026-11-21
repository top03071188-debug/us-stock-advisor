#!/usr/bin/env python3
"""scripts/run_screen.py — 주간 1층 스크리너 + 워치리스트 편입/편출 + 13F 레이더 갱신.

`weekly-screen.yml` 이 부르는 계약(초안은 `docs/phase3_verification.md` 참고):
    python scripts/run_screen.py --as-of YYYY-MM-DD [--offline] [--13f FILE]

[고정]
- 통과 종목 0개도 정상이다(최소 개수 강제 없음). 편입/편출은 **사유와 함께** 남긴다 —
  왜 빠졌는지가 다음 주 판단의 근거다(3.2.5).
- 13F 는 `acceptance_datetime ≤ decision_at`(분기말 +45일)만 반영하고 항상
  "45일 전 분기말 기준" 워터마크를 붙인다(5.5).
- 가치평가 보류 업종(은행·보험·REIT)은 워치리스트에서 제외하고 `valuation_suspended` 에 남긴다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.data.holdings_13f import (  # noqa: E402
    FILERS_PATH,
    STALE_DAYS,
    STATUS_KO,
    build_issuer_ticker_index,
    collect_13f_filings,
    load_company_tickers,
    radar_payload,
)
from engine.decide.pipeline import load_params  # noqa: E402
from engine.metrics.valuation import classify_sector  # noqa: E402
from engine.screener import Candidate, screen  # noqa: E402
from engine.utils.dates import data_cutoff_at as cutoff_of  # noqa: E402

WATCHLIST = Path("data/watchlist/current.json")
HISTORY = Path("data/watchlist/history.jsonl")
RADAR = Path("data/radar/13f.json")

def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_candidates(data: dict) -> list[Candidate]:
    """데이터셋 → 1층 후보. 안전마진은 IV 중앙값과 현재가로 계산한다.

    v1.3.3 04: 워치리스트 **모든 행**에 감점 전 V·부호 있는 괴리·EV/EBIT 표본 품질·성장률 근거를 남긴다.
    값은 `valuation_inputs`(=`valuation_multiplier` 의 상세)에서 먼저 읽고, 없으면 유니버스 행을 본다.
    """
    out: list[Candidate] = []
    for row in data.get("universe") or []:
        ticker = row["ticker"]
        panel = (data.get("panels") or {}).get(ticker)
        if panel is None or panel.frame.empty:
            continue
        frame = panel.frame.dropna(subset=["raw_close", "sadj_close"], how="all")
        if frame.empty:
            continue
        last = frame.iloc[-1]
        price = float(last["raw_close"])
        sadj = float(last["sadj_close"])
        raw_detail = (data.get("valuation_inputs") or {}).get(ticker)
        detail = dict(raw_detail) if isinstance(raw_detail, dict) else {}   # 픽스처는 ValuationInputs 를 직접 넣는다

        def pick(key: str, default=None):
            value = detail.get(key)
            if value is None:
                value = row.get(key)
            return default if value is None else value

        mult = row.get("iv_mult")
        if mult is None:
            mult = detail.get("iv_mult")
        margin = None
        if mult is not None and not classify_sector(row.get("sector"))["valuation_suspended"]:
            iv = sadj * float(mult)
            margin = (iv - sadj) / iv if iv > 0 else None
        adv = row.get("adv20_usd")
        if adv is None and "volume" in panel.frame:
            adv = float((panel.frame["raw_close"] * panel.frame["volume"]).tail(20).mean())

        def _f(key: str) -> float | None:
            value = pick(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _i(key: str) -> int | None:
            value = pick(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        out.append(Candidate(
            ticker=ticker,
            sector=row.get("sector") or "",
            market_cap_usd=float(row.get("market_cap_usd") or 0.0),
            adv20_usd=float(adv or 0.0),
            price=price,
            history_rows=int(len(panel.frame)),
            exchange=str(row.get("exchange") or "UNKNOWN"),
            xbrl_completeness=float(row.get("xbrl_completeness") if row.get("xbrl_completeness") is not None else 1.0),
            fundamentals=(data.get("fundamentals") or {}).get(ticker),
            margin=margin,
            v_penalties=float(pick("v_penalties", 0.0) or 0.0),
            hist_divergence_signed=_f("hist_divergence_signed"),
            ev_ebit_percentile=_f("ev_ebit_percentile"),
            ev_ebit_current=_f("ev_ebit_current"),
            ev_ebit_median_10y=_f("ev_ebit_median_10y"),
            ev_ebit_samples=_i("ev_ebit_samples"),
            ev_ebit_years=_i("ev_ebit_years"),
            ev_ebit_window=pick("ev_ebit_window"),
            ev_ebit_reason=pick("ev_ebit_reason"),
            growth=_f("growth"),
            growth_raw_cagr=_f("growth_raw_cagr"),
            growth_clipped_to_zero=(None if pick("growth_clipped_to_zero") is None
                                    else bool(pick("growth_clipped_to_zero"))),
            effective_fcff_years=_i("effective_fcff_years"),
            normalization_method=pick("normalization_method"),
            dcf_sensitivity=_f("dcf_sensitivity"),
            dcf_dropped=pick("dcf_dropped"),
            wacc_sensitivity=_f("wacc_sensitivity"),
            terminal_g_sensitivity=_f("terminal_g_sensitivity"),
            peg=row.get("peg"),
            revenue_cagr=row.get("revenue_cagr"),
        ))
    return out


def diff_watchlist(previous: dict | None, current: list[dict], excluded: list[dict]) -> dict:
    """편입/편출 + 사유. 사유가 없으면 "사유 미기록"으로 남긴다(조용한 누락 금지)."""
    prev_rows = {r["ticker"]: r for r in (previous or {}).get("watchlist") or []}
    cur_rows = {r["ticker"]: r for r in current}
    reason_by_ticker = {e["ticker"]: (e.get("reasons") or e.get("gates") or []) for e in excluded}
    entered: list[dict] = []
    for ticker in sorted(set(cur_rows) - set(prev_rows)):
        reasons = reason_by_ticker.get(ticker) or []
        entered.append({
            "ticker": ticker,
            "sector": cur_rows[ticker].get("sector"),
            "rank_score": cur_rows[ticker].get("rank_score"),
            "reason_ko": (f"지난주 제외 사유 해소({', '.join(reasons)})" if reasons
                          else "신규 후보 편입(지난주 명단에 없음)"),
        })
    exited: list[dict] = []
    for ticker in sorted(set(prev_rows) - set(cur_rows)):
        reasons = reason_by_ticker.get(ticker) or []
        exited.append({
            "ticker": ticker,
            "sector": prev_rows[ticker].get("sector"),
            "reason_ko": (f"하드게이트/가치 미달 — {', '.join(reasons)}" if reasons
                          else "점수 하락 또는 후보 제외(사유 미기록)"),
        })
    return {"entered": entered, "exited": exited}


def attach_valuation_wacc(result: dict, data: dict) -> None:
    """워치리스트 행에 평가 경로 WACC(`wacc_valuation`)를 함께 남긴다.

    Q 점수 경로의 `wacc_qc`(metrics)와 다른 값이고, 화면·리포트가 "어느 WACC 로
    평가했나"를 물을 때 필요하다. 유니버스에 없으면(가치평가 보류 등) 키를 만들지 않는다.
    """
    rows = {row.get("ticker"): row for row in data.get("universe") or []}
    for item in result.get("watchlist") or []:
        value = (rows.get(item.get("ticker")) or {}).get("wacc_valuation")
        if value is not None:
            item.setdefault("metrics", {})["wacc_valuation"] = value


def append_history(as_of: date, payload: dict) -> None:
    """`as_of` 당 1행(멱등). 같은 날 재실행하면 앞선 행을 대체한다 — 중복 행이 레이더 차트를 왜곡하지 않게."""
    line = json.dumps({"as_of": as_of.isoformat(), "count": payload["count"],
                       "entered": [r["ticker"] for r in payload["entered"]],
                       "exited": [r["ticker"] for r in payload["exited"]]}, ensure_ascii=False)
    kept: list[str] = []
    if HISTORY.exists():
        for raw in HISTORY.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                if json.loads(raw).get("as_of") == as_of.isoformat():
                    continue
            except json.JSONDecodeError:
                pass
            kept.append(raw)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text("\n".join(kept + [line]) + "\n", encoding="utf-8")


# ────────────────────────────── 13F 레이더 ──────────────────────────────
def _existing_radar() -> dict | None:
    """이전 실행이 남긴 레이더 — 깨졌거나 없으면 None."""
    if not RADAR.exists():
        return None
    try:
        payload = json.loads(RADAR.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _watchlist_tickers() -> set[str]:
    """지금 워크리스트의 티커 집합(13F 매칭용)."""
    if not WATCHLIST.exists():
        return set()
    try:
        payload = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(r.get("ticker")).upper() for r in payload.get("watchlist") or [] if r.get("ticker")}


def _issuer_ticker_index() -> dict[str, list[str]]:
    """회사명 → 티커 색인. 실패하면 빈 색인(이름·CUSIP 직접 비교로 폴백) — 레이더는 계속 만들린다."""
    try:
        return build_issuer_ticker_index(load_company_tickers())
    except Exception as exc:  # noqa: BLE001 — 색인 실패가 13F 수집·빈도 위험을 만들지 않는다
        print(json.dumps({"13f_ticker_index": "failed",
                          "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}, ensure_ascii=False),
              file=sys.stderr)
        return {}


def refresh_radar(decision_at: datetime, as_of: date, watch_tickers: set[str], *,
                  filings: list | None = None, live: bool = True,
                  filers_path: str | Path = FILERS_PATH,
                  previous: dict | None = None) -> tuple[dict, dict]:
    """13F 레이더 갱신 — **어떤 실패도 예외로 내보내지 않는다**(주간 배치가 멈추면 안 된다).

    `filings` 가 주어지면(`--13f FILE`) 수집하지 않고 그 입력으로 만든다. 없고 `live` 이면
    SEC EDGAR 에서 직접 수집한다. 수집이 전부 실패했는데 지난 레이더에 펀드가 있으면
    **지난 값을 지우지 않고** 사유와 경과일을 다시 적는다(일시 장애로 좋은 데이터를 날리지 않는다).

    Returns: (`radar` payload, 진단 dict).
    """
    diag: dict = {"status": "static", "errors": [], "n_filings": len(filings or [])}
    issuer_index = _issuer_ticker_index() if live else {}
    if filings is None and live:
        try:
            collected = collect_13f_filings(decision_at, config_path=filers_path)
        except Exception as exc:  # noqa: BLE001 — 수집기 자체 예외도 진단으로
            collected = {"filings": [], "status": "failed", "note_ko": "13F 수집 실패",
                         "errors": [{"stage": "collect",
                                     "error": f"{type(exc).__name__}: {str(exc)[:160]}"}],
                         "skipped": []}
        filings = collected.get("filings") or []
        diag = {"status": collected.get("status"), "errors": collected.get("errors") or [],
                "skipped": collected.get("skipped") or [], "note_ko": collected.get("note_ko"),
                "n_filings": len(filings)}

    radar = radar_payload(filings or [], decision_at, watchlist=watch_tickers,
                          issuer_to_ticker=issuer_index or None)
    radar["as_of"] = as_of.isoformat()
    status = str(diag.get("status") or ("ok" if radar.get("n_filers") else "empty"))
    radar["status"] = status
    if diag.get("note_ko") and status in ("partial", "missing_key", "no_filers", "failed", "empty"):
        radar["note_ko"] = str(diag["note_ko"])

    if status in ("failed", "missing_key", "no_filers") and (previous or {}).get("filers"):
        carried = dict(previous)
        period_end = carried.get("period_end")
        if period_end:
            try:
                age = (as_of - date.fromisoformat(str(period_end))).days
                carried["stale_days"] = age
                carried["stale"] = age > STALE_DAYS
            except ValueError:
                pass
        carried["as_of"] = as_of.isoformat()
        carried["status"] = status
        carried["note_ko"] = f"{STATUS_KO.get(status, status)} — 지난 레이더 유지({period_end or '분기말 미확인'})"
        diag["carried_forward"] = True
        radar = carried
    return radar, diag


def radar_for(decision_at: datetime, as_of: date, watch_tickers: set[str], *,
              filings: list | None = None, live: bool = True,
              filers_path: str | Path = FILERS_PATH) -> tuple[dict, dict]:
    """레이더 1건 — 라이브 수집·입력 파일·지난 값 유지 중 무엇을 했는지 함께 돌려준다."""
    previous = _existing_radar()
    if filings is None and not live:
        radar = dict(previous) if previous else radar_payload([], decision_at, watchlist=watch_tickers)
        radar["as_of"] = as_of.isoformat()
        radar["status"] = radar.get("status") or ("ok" if radar.get("n_filers") else "empty")
        radar.setdefault("note_ko", "13F 입력 없음 — 지난 레이더 유지")
        return radar, {"status": "static", "errors": [], "n_filings": 0,
                       "carried_forward": bool(previous), "note_ko": radar.get("note_ko")}
    return refresh_radar(decision_at, as_of, watch_tickers, filings=filings, live=live,
                         filers_path=filers_path, previous=previous)


def update(data: dict, as_of: date, decision_at: datetime, params: dict,
           thirteen_f: list[dict] | None = None, *, live: bool = True,
           filers_path: str | Path = FILERS_PATH,
           radar_info: dict | None = None) -> dict:
    result = screen(build_candidates(data))
    attach_valuation_wacc(result, data)
    previous = json.loads(WATCHLIST.read_text(encoding="utf-8")) if WATCHLIST.exists() else None
    changes = diff_watchlist(previous, result["watchlist"], result["excluded"])
    payload = {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "params_version": params.get("version"),
        "watchlist": result["watchlist"],
        "count": result["count"],
        "excluded": result["excluded"],
        "valuation_suspended": result["valuation_suspended"],
        "sector_counts": result["sector_counts"],
        "entered": changes["entered"],
        "exited": changes["exited"],
        "notes": result["notes"],
        "source": data.get("source"),
    }
    _write_json(WATCHLIST, payload)
    append_history(as_of, payload)

    watch_tickers = {r["ticker"] for r in result["watchlist"]}
    radar, radar_diag = radar_for(decision_at, as_of, watch_tickers, filings=thirteen_f,
                                 live=live, filers_path=filers_path)
    if radar_info is not None:
        radar_info.update(radar_diag)
    _write_json(RADAR, radar)
    return payload


def load_dataset(args, as_of: date, decision_at: datetime) -> dict:
    if args.fixtures:
        from engine.data.fixtures import dataset  # noqa: PLC0415
        data = dataset(as_of)
        data["source"] = "fixtures"
        return data
    from engine.data.collect import collect  # noqa: PLC0415
    return collect(as_of, decision_at)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="주간 1층 스크린 + 워치리스트 diff + 13F")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("--params", default="config/params_v001.yaml")
    ap.add_argument("--13f", dest="thirteen_f", default=None, help="13F 제출 JSON 목록 파일")
    ap.add_argument("--13f-only", dest="thirteen_f_only", action="store_true",
                    help="워치리스트는 그대로 두고 13F 레이더만 갱신한다")
    ap.add_argument("--filers", default=FILERS_PATH, help="13F 수집 대상 펀드 목록 YAML")
    args = ap.parse_args(argv)
    if args.offline:
        args.fixtures = True
    root = Path(args.root).resolve()
    if root != Path.cwd():
        os.chdir(root)
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    decision_at = cutoff_of(as_of, "postclose")
    live = not args.fixtures                    # 라이브 모드 = 13F 자동 수집 대상
    filings = None
    if args.thirteen_f:
        loaded = json.loads(Path(args.thirteen_f).read_text(encoding="utf-8"))
        filings = loaded.get("filings") if isinstance(loaded, dict) else loaded

    if args.thirteen_f_only:
        # 워치리스트·내역은 손대지 않고 레이더만 새로 만든다(13F 만 급히 손볼 때).
        radar, diag = radar_for(decision_at, as_of, _watchlist_tickers(), filings=filings,
                                live=live, filers_path=args.filers)
        _write_json(RADAR, radar)
        print(json.dumps({
            "as_of": as_of.isoformat(), "mode": "13f-only", "status": diag.get("status"),
            "n_filers": radar.get("n_filers"), "n_filings": diag.get("n_filings"),
            "period_end": radar.get("period_end"),
            "watchlist_overlap": radar.get("watchlist_overlap") or [],
            "carried_forward": bool(diag.get("carried_forward")),
            "errors": [f"{e.get('filer_name') or e.get('cik') or '-'}:{e.get('stage')}"
                       for e in diag.get("errors") or []][:10],
            "note_ko": radar.get("note_ko"),
        }, ensure_ascii=False))
        return 0

    params = load_params(Path(args.params))
    data = load_dataset(args, as_of, decision_at)
    radar_info: dict = {}
    payload = update(data, as_of, decision_at, params, filings, live=live,
                     filers_path=args.filers, radar_info=radar_info)
    print(json.dumps({
        "as_of": as_of.isoformat(), "count": payload["count"],
        "entered": [r["ticker"] for r in payload["entered"]],
        "exited": [r["ticker"] for r in payload["exited"]],
        "valuation_suspended": [r["ticker"] for r in payload["valuation_suspended"]],
        "excluded": len(payload["excluded"]),
        "13f": {"status": radar_info.get("status"), "n_filings": radar_info.get("n_filings"),
                "errors": len(radar_info.get("errors") or [])},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
