#!/usr/bin/env python3
"""scripts/build_sample_signals.py — 데이터 계약 예시(`data/samples/quant_signals.example.json`) 생성.

실제 신호가 아니라 **스키마·공개 범위 가드**를 검증하기 위한 고정 예시다.
`engine.ledger.record.publish_record` 로 만들기 때문에 발행 레코드 규칙(체결가 없음,
세 시각 순서, 잠정 분할가)을 그대로 만족한다.

    python scripts/build_sample_signals.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.decide.macro import classify_regime  # noqa: E402
from engine.decide.signals import expected_mdd, target_band  # noqa: E402
from engine.ledger.record import publish_record  # noqa: E402

OUT = Path("data/samples/quant_signals.example.json")


def build() -> dict:
    data_cutoff = "2026-11-18T22:00:00Z"
    published = "2026-11-18T22:02:11Z"
    order_cutoff = "2026-11-19T14:25:00Z"
    band = target_band(iv_median=522.0, price=412.35)
    record = publish_record(
        signal_id="2026-11-19-SAMPLE-BUY-001",
        ticker="SAMPLE",
        type="BUY",
        grade="B",
        entry_score=69.4,
        as_of="2026-11-19",
        data_cutoff_at=data_cutoff,
        signal_published_at=published,
        order_cutoff_at=order_cutoff,
        generated_price=412.35,
        target_band=band,
        expected_mdd=expected_mdd(0.011),
        thesis_ko="예시 데이터입니다 — 실제 신호가 아닙니다.",
        evidence=[
            {"type": "fundamental", "metric": "FCF_margin_5y", "value_kind": "ratio"},
            {"type": "sec_filing", "form": "10-Q", "url": "https://www.sec.gov/Archives/"},
        ],
        investor_scores={"buffett": 62, "graham": 55, "lynch": -18, "druckenmiller": 35},
        conflict=True,
        plan_weight=0.05,
        stop_distance=0.09,
        veto=[],
        params_version="params_v001",
        snapshot_id="sha256:sample" + "0" * 58,
        code_commit="sample",
    )
    record["trade_grade"] = "BUY"
    record["components"] = {"quality": 82, "valuation": 71, "timing": 64, "macro": 55, "growth": 60}
    record["valuation"] = {"iv_low": 455.0, "iv_median": 522.0, "iv_high": 610.0,
                           "price": 412.35, "margin": 0.21}
    record["news_state"] = "read_done"

    regime = classify_regime(
        {"dff": 3.9, "cpi_yoy": 2.9, "t10y2y": 0.35, "hy_oas_bp": 380, "vix": 20.5, "dtwexbg": 120.0},
        {"dff": 4.2, "cpi_yoy": 3.0, "dtwexbg": 119.0},
    )
    return {
        "schema_version": 1,
        "generated_at": "2026-11-19T07:02:11+09:00",
        "data_cutoff_at": data_cutoff,
        "signal_published_at": published,
        "order_cutoff_at": order_cutoff,
        "as_of": "2026-11-19",
        "params_version": "params_v001",
        "snapshot_id": record["snapshot_id"],
        "code_commit": "sample",
        "status": "sample",
        "regime": regime.as_dict(),
        "signals": [record],
        "blocked": [{"ticker": "OTHER", "reason": "V01", "codes": ["V01"],
                     "message_ko": "실적 발표 임박 — 신규 매수 보류"}],
        "watchlist": [{"ticker": "SAMPLE", "sector": "Information Technology",
                       "q": 82, "v": 71, "g": 60, "rank_score": 72.9,
                       "valuation_suspended": False}],
        "watchlist_count": 1,
        "ledger_summary": {"agreement": None, "win_rate": None, "n_graded": 0,
                           "note": "예시 데이터 — 채점 표본 없음"},
        "portfolio": {"as_of": "2026-11-19", "cash_pct": 1.0, "invested_pct": 0.0, "positions": []},
    }


def main() -> int:
    payload = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(OUT)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
