# Phase 2 검증 기록 (2026-11-20)

기획서 **v1.3.1** §7 Phase 2 — *실제 매매 규칙 모의 포트폴리오 + 워크포워드 전략 검증* 구현·검증 결과.
실행 환경: Python 3.12.16, `.venv`, pandas 3.1.1 · numpy 2.5.5 · PyYAML 6.0.3 · pytest 9.2.0.
모든 테스트는 **오프라인·결정론**이다(네트워크 마커 4건은 기본 제외).

## 1. 무엇을 만들었나

| 영역 | 파일 | 핵심 규칙 |
|---|---|---|
| 매수/매도 트리거·등급·분할 계획 | `engine/decide/signals.py` | B0~B6, STRONG_BUY/BUY/WATCH/NONE, 40/30/30, 3차가 손절선 밖이면 무효(실효 70%) |
| 리스크 가드 12종 | `engine/decide/veto.py`, `config/veto_rules.yaml` | V01~V12, 그룹(hard_gate·disclosure·liquidity·macro·limit·thesis·data·evidence), 발동 사유 보존 |
| 거시 레짐 | `engine/decide/macro.py` | 5신호 3개 이상 일치 → Risk-On/Neutral/Risk-Off, 배수 1.0/0.7/0.4, 안전마진 15/20/25%, 신규 종목 제한 |
| 포지션 사이징·재검증 | `engine/decide/sizing.py` | `atr_pct = atr14_sadj/close_sadj` → `min(3×atr_pct, 20%)`, 계획 손실 ≤ 0.75%, 단일 5%·섹터 25%, 2·3차 직전 축소/거절 |
| 모의 포트폴리오 | `engine/ledger/portfolio.py` | 현금·수량 추적, 개장 경매 범위 체결, 손절 먼저·잔여 주문 즉시 취소, 분할·배당, 트레일링, 분할익절 |
| 폴드 실행기 | `engine/backtest/runner.py` | 시나리오(일별 봉 + 장 전 주문 + 기업행동) → 모의 계좌, 입력은 읽기 전용(복사본 실행) |
| 성과 리포트 | `engine/backtest/report.py` | CAGR·MDD·Sharpe·Sortino·Calmar·회전율·평균 노출도·승률·손익비, 슬리피지 0/5/10/20bp, 기간별 채택/보류 표(6M 보류 표기) |
| 발행 기록·체결 이벤트 | `engine/ledger/record.py` | 발행 레코드 불변(체결가 **비움**), `execution_event`·`plan_reanchor`·`debate_events` 분리, append-only |
| 채점 | `engine/ledger/grade.py` | 1주 5td / 1개월 21 / 3개월 63 / 6개월 126, Agreement `0.40D+0.25B+0.15DD+0.20T`, 밴드 `base`·성과 `fill`, `maturity_date < holdout_start` |
| 데이터 계약 | `schemas/quant_signals.schema.json`, `engine/schema.py`, `engine/models.py` | 스키마 검증 + 세 시각 순서 + 발행 레코드 체결가 금지 + 공개 금지 필드 차단 |
| 재현 스크립트 | `scripts/run_walkforward.py` | `--plan-only`(Phase 1) / `--portfolio-demo` / `--scenario` |
| 계약 예시 | `scripts/build_sample_signals.py`, `data/samples/quant_signals.example.json` | 스키마·공개 범위 가드가 통과하는 예시(실제 신호 아님) |

가격 기준은 모듈마다 고정이다: **판단(B2·B3) = `split_adj_ohlc`**, **체결·손절·계좌 = `raw_as_traded` + `atr_pct` 비율**.

## 2. 오프라인 테스트 실측

```text
$ .venv/bin/python -m pytest -m "not network"
205 passed, 4 deselected in 1.70s
```

Phase 1의 90개는 그대로 통과하고 **115개를 추가**했다(모두 신규 파일).

| 파일 | 개수 | 검사 |
|---|---|---|
| `tests/test_portfolio.py` | 21 | 결정론(바이트 동일), 계획 손실 불변식(ATR 1~10%), 손절 봉 재진입 0건, 분할별 재검증, 2:1 분할·배당, 갭 체결, 슬리피지 방향, 장 전 커밋 |
| `tests/test_decide_signals.py` | 23 | B0~B6, 등급·상한(G≤0·뉴스 근거 부족), 가치평가 보류 업종, 분할 계획·3차 무효, S1~S5, **편출 보유 종목 추적** |
| `tests/test_veto_regime.py` | 30 | Veto 12종 발동·예외(5.02 원문 확인, 분할 공시), 그룹 요약, 레짐 3종·미검증·극단 플래그·조정치 |
| `tests/test_ledger.py` | 17 | 발행 레코드 체결가 없음, 세 시각 순서, append-only, 체결·재고정·토론 이벤트, 4기간 채점, 홀드아웃 절단, 밴드 기준 |
| `tests/test_report.py` | 9 | CAGR·MDD·Sharpe·조정치, 벤치마크 비교, 슬리피지 0/5/10/20bp, **신호 0건 폴드 completed**, 기간별 채택/보류 표(6M 보류), 파일 산출 |
| `tests/test_quant_signals_schema.py` | 15 | 스키마 필수필드·enum·타입, 세 시각, `execution_event` null, 잠정가, 금지 필드, 중복 id, 0건 정상 |

### Phase 2 DoD 8항목 대응

| # | DoD | 근거 테스트 | 결과 |
|---|---|---|---|
| 1 | 40/30/30·분할익절·손절·갭·슬리피지를 화면과 같은 식으로 실행, 같은 스냅샷 재실행 바이트 동일 | `test_portfolio_determinism`, `test_gap_down_fills_at_open`, `test_profit_take_sells_one_third_once`, `test_slippage_is_applied_in_right_direction` | 통과 — 같은 시나리오 2회 실행 `to_json()` 동일(슬리피지만 바꾸면 달라짐) |
| 2 | 계획 손실 = 비중 × 손절 거리 ≤ 0.75% (ATR 1~10%) | `test_planned_loss_invariant_all_atr`, `test_planned_loss_invariant_with_regime_multiplier`, `test_full_tranche_fill_keeps_planned_loss_under_budget` | 통과 — 19개 ATR 값 × 3개 레짐 배수, 전 분할 체결 후에도 ≤ 0.75% |
| 3 | 손절(S5)·전량 청산(S3/EXIT) 봉에서 잔여 2·3차 미체결(재진입 0건) | `test_cancel_after_stop`, `test_force_exit_cancels_pending_and_sells_at_open`, `test_structural_break_cancels_same_day_split` | 통과 — 손절 봉 `buy_filled` 0건, `cancelled_by: S5` 기록 |
| 4 | 2·3차 체결 직전 재검증이 수량 축소·거절로 이어짐 | `test_recheck_rejects_when_budget_exhausted`, `test_recheck_shrinks_quantity_to_budget`, `test_recheck_full_when_within_budget`, `test_simulator_recheck_keeps_position_within_cap` | 통과 — 축소·거절·정상 3경로 확인 |
| 5 | 2:1 분할·배당 ex-date 조정 | `test_corporate_actions`, `test_split_then_stop_uses_adjusted_prices` | 통과 — 수량 ×2, 평단·손절가·미체결 주문가 ÷2, 배당 `cash` 가산 |
| 6 | 워치리스트 편출 보유 종목도 S1~S5 계산 | `test_holdings_tracked_after_watchlist_removal`, `test_suspended_sector_holding_skips_s1_only` | 통과 — 편출 배지 + `new_buy_allowed: False` + 5트리거 전부 계산 |
| 7 | 폴드별 전략 CAGR·MDD·평균 노출도 + 슬리피지 0/5/10/20bp | `test_cost_sensitivity_runs_four_levels_and_decays`, `test_metrics_exposure_and_turnover_bounded` | 통과 — 4수준 결과가 슬리피지 단조 감소 |
| 8 | 신호 0건 폴드도 정상 완료 | `test_fold_report_zero_signals_is_completed`, `test_summary_with_no_signals_is_not_failure` | 통과 — `status: completed`, 지표 `None`, 실패 아님 |

## 3. 재현 실행 (합성 시나리오, 실데이터 아님)

```text
$ .venv/bin/python scripts/run_walkforward.py --today 2026-11-19 --plan-only
{"counts": {"1m": 4, "3m": 3, "6m": 2}, "blocked": ["6m"], "holdout_start": "2026-08-19",
 "calibration_table": [{"horizon": "1m", "calibration": "calibrate"}, {"horizon": "3m", "calibration": "calibrate"},
                       {"horizon": "6m", "calibration": "deferred", "note": "6m 보정 보류 — 적격 폴드 2/3 …"}]}

$ .venv/bin/python scripts/run_walkforward.py --portfolio-demo
{"portfolio_demo": {"signals": 6, "status": "completed",
  "strategy": {"total_return": -0.001567, "cagr": -0.001317, "mdd": -0.003994,
               "sharpe": -0.803606, "sortino": -0.395724, "calmar": -0.329668,
               "avg_exposure": 0.006783, "turnover": 0.150034,
               "win_rate": 0.625, "n_trades": 8},
  "cost_sensitivity": {"0bp": -0.001017, "5bp": -0.001167, "10bp": -0.001317, "20bp": -0.001592},
  "digest": "sha256:9577fd00…"}}
```

- 이 값은 **경로 재현용 합성 데이터**(`engine/backtest/demo.py`, 고정 시드)이며 **전략 성과의 증거가 아니다**.
  실데이터 폴드(1M/3M/6M) 실행은 Phase 3 수집 파이프라인이 데이터를 쌓은 뒤 `--scenario` 로 연결한다.
- 노출도가 0.7% 수준인 것은 정상이다: 단일 5% 상한 × 분할 40/30/30 이라 6종목을 다 채워도 최대 21%이고,
  합성 시나리오는 손절·익절로 일찍 빠져나간다. MDD·CAGR 정의는 4.6과 같다.
- 폴드 산출 경로: `out/walkforward/folds/{fold_id}/{report.json, portfolio.json}`.
  `portfolio.json` 에는 수량·평단이 들어가므로 **공개 커밋 경로(`data/`)에 두지 않는다**(2.2.3·Phase 3 DoD).

## 4. 고정 규칙이 코드에서 지켜지는 지점

| 규칙 | 코드 |
|---|---|
| `atr_pct` 비율 하나로 손절·사이징 동일 적용 | `engine/decide/sizing.py::stop_distance_for` ← `engine/metrics/technical.py::atr_pct` |
| 1차는 장 전 커밋 범위(`stop_min < P ≤ limit_max`) 안에서만 | `engine/backtest/execution.py`, `PortfolioSimulator.process_day` (커밋 시각 < 개장) |
| 손절 먼저, 같은 봉 잔여 주문 취소 | `PortfolioSimulator.process_day` 3~5단계, `_exit` → `_cancel_pending` |
| 3차 ≤ 초기 손절가 → 무효 | `engine/backtest/execution.py::entry_plan`, `_reanchor` |
| 분할·배당을 먼저 적용(분할 충격을 손절로 오인 금지) | `PortfolioSimulator._apply_corporate_actions` |
| 발행 레코드는 체결가 없이 불변 | `engine/ledger/record.py::publish_record` / `assert_publish_record` |
| 세 시각 분리 | `record.check_three_times`, `models.check_signal_times` |
| 홀드아웃 절단 | `engine/ledger/grade.py`(`maturity ≥ holdout_start` → `truncated`), 기존 `walk_forward.py` |
| Veto 는 신호를 지우지 않고 남긴다 | `engine/decide/veto.py::veto_check` → `veto[]` |

## 5. 보안·공개 범위

- `.env`·실 API 키·개인 이메일을 새 파일에 넣지 않았다. `scripts/check_commit_scope.py --staged` 로 커밋 전 검사한다.
- `schemas/quant_signals.schema.json` 의 `portfolio` 는 **비중(%)만** 허용한다(`shares`·`avg_cost`·`cash_balance` 금지).
- `validate_quant_signals` 는 공개 금지 필드(`raw_close`·`avg_cost`·`shares`·`body_excerpt_1500` …)가 있으면 파일을 버린다.
- `.github/workflows/*` 는 수정하지 않았다.

## 6. 미검증 한계 (정직하게)

- **실데이터 폴드 성과는 아직 없다.** Phase 2 코드는 yfinance/FRED 수집물이 없는 상태에서 검증됐고, 합성 시나리오로 경로만 확인했다. `scripts/run_daily.py`(수집→신호)와의 연결은 Phase 3다.
- **생존편향·하락장 스트레스는 여전히 미검증**이다. `universe_history`·`delisted_securities`(2.4.5)가 없어 2022/2020/2018Q4 스트레스와 5년 20폴드는 계산하지 않았다(게이트 통과로 쓰지 않는다).
- **6개월 보정은 보류** 상태다(적격 폴드 2개, 15개월 창 확보 전). `data/calibration/attempts.json` 에 사유를 남긴다.
- **일봉만 쓴다.** 장중 순서를 알 수 없으므로 손절을 먼저 판정하는 보수 가정을 쓴다. 같은 봉에서 지정가가 손절보다 유리했던 경우는 재현하지 않는다.
- **부분 수량을 허용**한다(기획서 계산 예의 0.004818주). 정수 주 단위 계좌·최소 수량 규칙은 Phase 3에서 정한다.
- **비용은 슬리피지·수수료만** 반영한다. 세금(양도소득세 22%)·환율·환전 스프레드·배당 원천징수 15%는 8.2의 별도 계산이며 이 엔진에 없다.
- **벤치마크 곡선(SPY 총수익)은 아직 연결되지 않았다.** `strategy_metrics(benchmark_curve=...)` 는 준비돼 있으나 합성 시나리오에는 SPY 데이터가 없다.
- **뉴스 LLM·13F·V11(프롬프트 인젝션 탐지)은 입력만 받는다.** 실제 판독·탐지 파이프라인은 Phase 3~4다. V01의 실적일은 8-K 2.02 제출일 추정 규칙을 쓰며 실데이터 피드와 연결되지 않았다.
- **`estimate` 성격의 값**: 대가별 점수(`investor_scores`)는 0~100 복합점수를 `2c−100` 으로 옮긴 결정론 근사이고, exit_score 의 heat 항(밸류·기술)은 3.5의 서술을 단조 함수로 구현한 것이다. 화면 문구·설명은 Phase 3 대시보드에서 확정한다.
