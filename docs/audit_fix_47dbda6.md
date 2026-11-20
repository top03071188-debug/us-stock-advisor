# R01~R11 감사 수정·통합 검증 보고서 (감사 기준 커밋 `47dbda6`)

- 검증 대상 감사 문서: `investment_app_implementation_audit_47dbda6-…md` (사용자 첨부, 2026-11-20)
- 기획서 정본: `downloads/미국주식_투자조언앱_개발기획서.md` **v1.3.1** (3,115줄) — 이번 수정에서 **바꾸지 않았다**
- 코드베이스: `repos/us-stock-advisor/` — 감사 기준 `47dbda6`, 수정 착수 시점 HEAD `605b60a`
- 감사 타당성 대조 기록: `scratch/audit_verdict_47dbda6.md`(원문·줄번호·재현 수치), `scratch/repro_r01_r11.py`(읽기 전용 재현)
- 작업 분할: **트랙 A** = R01·R02·R08·R09·R10·R11(실행·채점·시각·재생) / **트랙 B** = R03·R04·R05·R06·R07(SEC·분할·평가·FRED). 파일 소유권은 감사 검증 문서 §4.1을 그대로 따랐고, 한 파일을 두 트랙이 고치지 않았다.
- 이번 문서가 다루는 것: ① 감사 11건의 타당성 판정 ② **감사 문서 정정 3건** ③ 실제 수정 내역(트랙 A·B·통합 3건) ④ 테스트·린트·커밋 범위·출력 스키마 실측 ⑤ 실데이터 재스크린 결과 ⑥ 이번에 넣지 않은 항목.

---

## 0. 요약

| 항목 | 결과 |
|---|---|
| 감사 11건 판정 | **전부 실제 결함(전면 타당)**. 세부 메커니즘 2건(R02 방향점수, R09 발현 범위)과 결과가 갈리는 1건(AVGO)은 감사 서술을 그대로 옮기면 잘못 고치므로 **정정 3건**으로 분리했다(§2) |
| 수정 | 11건 전부 구현. 신규 회귀 테스트 **91개**(트랙 A 44 + 트랙 B 44 + 통합 3), 기존 테스트 6개는 새 규칙에 맞게 개정 |
| 테스트 | `pytest -m "not network"` **373 passed, 4 deselected**(감사 시점 기준선 281) · `ruff check engine scripts tests` **All checks passed** |
| 재스크린(실데이터) | 워치리스트 **19 → 22종목**. NVDA·WMT **복귀**, TSLA **신규 편입**(V 계산 가능해짐), **AVGO 탈락 유지(정당)**, NFLX는 HG-09는 해소됐지만 **HG-08로 여전히 탈락** |
| 판단이 필요한 관찰 | 워치리스트 22종목 **전부 V=0**(모델 IV가 현재가보다 낮다는 판정) → 이번 주 BUY 신호는 자연히 0건. 회귀가 아니라 R03 정상화 이후의 모델 레벨 문제이며, 규칙 완화는 이번 범위 밖이다(§6.1) |

---

## 1. 감사 11건 판정 요약

“전면 타당”은 감사가 인용한 파일:줄·함수·수치를 **직접 재현**했다는 뜻이다(재현 로그는 `scratch/audit_verdict_47dbda6.md` §6-A).

| ID | 판정 | 결함(실측) | 수정 위치 |
|---|---|---|---|
| **R01** | 전면 타당(치명) | `load_executions()`가 `*.reanchor.json`까지 읽어 정렬상 뒤에서 체결 이벤트를 덮어씀 → 재시작 후 계좌·성적표·파일럿이 0주를 봄(재현: $980,000·200주 → $1,000,000·0주) | `engine/ledger/live.py` |
| **R02** | 전면 타당(치명) + **메커니즘 정정** | 만기 봉 없이 1봉만 있어도 `observed_days 1`·`Agreement 100`으로 채점. 신호별 기간이 아닌 실행일 기준 SPY 하나를 모든 신호에 전달. 종목 `raw_close` vs SPY `adj_close` 혼용 | `engine/ledger/grade.py`, `live.py`, `engine/backtest/walk_forward.py` |
| **R03** | 전면 타당(높음) | 최신 연도 EBIT·D&A·CAPEX, 매출 CAGR 성장률, FY EPS PEG, ΔNWC 단위 혼용(5% 상한 무력화), 순부채에서 시장성 단기투자 누락, WACC 두 경로 동명 | `engine/data/xbrl_fundamentals.py`, `engine/metrics/fundamentals.py` |
| **R04** | 전면 타당(높음) | 방법별 IV·bear/bull 밴드·근거를 **계산은 하는데 전달하지 않음**. 화면은 중앙값 ±15%로 재구성. `IV 대비 -233% 프리미엄` 부호 오류. 근거 재무일자가 실행일 | `engine/data/xbrl_fundamentals.py`, `collect.py`, `engine/decide/pipeline.py` |
| **R05** | 전면 타당(높음) + **결과 정정** | 분할을 희석으로 오인: NVDA `+877.8%`, WMT `+193.4%` → 분할 반영 시 `−2.22%`·`−2.19%`(둘 다 오탈락). AVGO는 `+14.7%`로 **정당 탈락** | `engine/data/xbrl_fundamentals.py`, `engine/metrics/fundamentals.py` |
| **R06** | 전면 타당(높음) | `filed` 날짜 문자열 비교만 → 같은 날 마감 후 접수분 통과. “가장 이른 filed 고정” → 10-K/A가 영구 무시 | `engine/data/sec_edgar.py`, `xbrl_fundamentals.py` |
| **R07** | 전면 타당(높음) | 스냅샷 응답의 모든 행에 관측일 기준 `available_at`을 부여 → 2026년에 개정된 값이 2025-03-01 결정에 노출 | `engine/data/macro.py`, `collect.py` |
| **R08** | 전면 타당(높음) | `signal_published_at`이 계획값(07:02)이고 실제는 11:34. `settle()`가 주문 마감 세션이 아닌 `next_trading_day` 시가로 체결 | `engine/utils/dates.py`, `engine/ledger/record.py`, `scripts/run_daily.py`, `live.py`, `web/app.js` |
| **R09** | 전면 타당(높음) + **발현 범위 정정** | `stop_min`은 sadj 200일선, 상한·체결가는 raw를 비교 → 재현: `stop_min=max(48.5,70)=70`, raw 시가 80 → `80.08 체결`. **상한(`limit_max`)도 같은 혼용** | `engine/backtest/execution.py`, `engine/decide/signals.py` |
| **R10** | 전면 타당(높음, 3개 세부) | (a) `snapshot_id`가 가격 CSV만 해시 (b) `replay()`가 밴드 유무만으로 BUY 결정 (c) `calibrate()`가 후보별 폴드를 재실행하지 않고 수락 규칙(Agreement +2·2폴드·스트레스)도 없음 | `engine/backtest/snapshot.py`, `replay.py`, `calibration.py` |
| **R11** | 전면 타당(높음) | 유효 표본을 발행 건수로 셈(WATCH 2건 → `n_eff 2`), `avg_exposure` 미전달로 파일럿 3지표 영구 계산 불가, `summary()`가 현재 `invested_pct`를 평균 노출도 자리에 대입 | `engine/ledger/pilot.py`, `live.py`, `scripts/run_daily.py`, `web/app.js` |

---

## 2. 감사 문서 정정 3건 (그대로 옮기면 잘못 고치는 문장)

이번 문서의 요구사항대로 **감사 서술과 실제 결함이 다른 지점 3곳**을 명시한다. 셋 다 “감사가 틀렸다”가 아니라, 문장 그대로 구현하면 헛수고가 되거나 고치지 말아야 할 것을 고치게 되는 경우다.

### 2.1 R02 — 방향점수 `D`는 “0% 대체”가 아니라 **초과수익 정의 미구현**

- 감사 문장: “SPY가 빠져도 방향 점수 계산 단계에서는 0%를 사용하며, 다음 단계에서 초과수익만 숨긴다.”
- 실제: `grade.py`의 `bench.get(h, 0.0)` 폴백은 사실이지만, **`agreement_components()`에는 벤치마크 인자 자체가 없었다**(`walk_forward.py:75` — `d = 100.0 if (r_fill > 0) == (direction > 0)`). 즉 SPY가 있어도 `D`는 **절대수익** 기준이었다.
- 수정: `agreement_components(..., benchmark_return=None)`로 시그니처를 바꾸고 `D = 100 if (r_fill − r_SPY > 0) == (direction > 0)`(§4.2.4)로 구현. 벤치마크가 없으면 `D=None` + 가중치 재정규화(0% 대체 금지). **정정 요지: R02는 “SPY 누락”보다 “초과수익 미구현”이 본체이고, 이걸 고치지 않으면 신호별 SPY를 붙여도 점수가 변하지 않는다.**

### 2.2 R05 — AVGO는 **정당 탈락**, WMT·NVDA는 **오탈락**, NFLX는 **원인 분리**

- 감사 문장: “AVGO와 WMT도 같은 게이트 단독으로 탈락 … 두 종목의 원본 입력은 각각 대조가 필요하다.”
- 실측(캐시 원본 XBRL + 재작성 제출본 대조):

| 종목 | 3년 희석(코드, 수정 전) | 분할 반영 후 | 판정 |
|---|---|---|---|
| NVDA | +877.8% | **−2.22%** | 오탈락 → **복귀** |
| WMT | +193.4% | **−2.19%** | 오탈락 → **복귀** |
| AVGO | +1047.3% | **+14.7%** | **정당 탈락** (VMware 인수 대가 주식 발행으로 실제 희석) |
| NFLX | +862% | **−4.4%** | HG-09는 해소됐지만 **HG-08(FCF/순이익<0.7)로 여전히 탈락** |

- 정정 요지: **AVGO를 통과시키면 게이트를 고치는 것이 아니라 눈을 가리는 것**이다. 반대로 NFLX는 “분할만 고치면 돌아온다”가 아니므로, 복귀 여부를 HG-09 하나로 판정하지 않는다. 이번 재스크린에서 실제로 AVGO는 `HG-09`로 남고 NFLX는 `HG-08` 단독으로 남았다(§5).

### 2.3 R09 — “라이브 버그”가 아니라 **백테스트·재생 버그**(상·하한 동시 혼용)

- 감사 문장: “현 코드는 $50의 97%와 $70을 직접 비교해 $80 시가 매수를 허용했다.”
- 실제: 최신 봉의 `split_factor`는 항상 1.0이라 **라이브에서는 `raw_close == sadj_close`** 로 수치상 무해하다. 폴드·재생 패널은 미래 분할까지 반영해 만들어지므로 `split_factor(T) ≠ 1` → **모든 폴드의 1차 진입 판정이 오염**된다. 또한 감사가 짚은 `stop_min`뿐 아니라 **`limit_max`(IV_median은 sadj, 비교는 raw_open)와 `fill = min(fill, limit_max)`도 같은 혼용**이다.
- 정정 요지: 고칠 것은 한 줄이 아니라 **밴드 전체의 기준 통일**이고, 결과적으로 **기존 폴드 성적과 비교 불가**가 된다(폴드 재실행 필요, §6.3).

---

## 3. 수정 내역

### 3.1 트랙 A — 실행·채점·시각·재생·파일럿 (R01·R02·R08·R09·R10·R11)

**R01 · `engine/ledger/live.py`**
- `load_executions()`: `*.reanchor.json`을 ① 파일명 접미사 ② `quant_track.execution_event ∈ EXECUTION_EVENTS` **두 겹**으로 제외. `load_reanchors()` 신설.
- `replay(records, executions, reanchors=None)`: 재고정은 손익에 쓰지 않고 `plan_reanchored`(표시용)로만 붙임. `settle()`의 “이미 체결” 판정도 체결 이벤트만 본다.

**R02 · `engine/ledger/grade.py`, `live.py`, `engine/backtest/walk_forward.py`**
- `grade_record()`: 만기 봉 관측 + 구간 봉 수 == `h`일 때만 채점(`complete`/`matured`). 아니면 `observed_days`만 남기고 `agreement=None`, `note="만기 봉 미관측 — 채점 보류"`.
- 수익률은 `total_return_adj`: `r_fill = tr_close(maturity)/tr_open(entry_session) − 1`. MDD도 TR 종가. 밴드는 base=1 정규화, `P_min/P_max`만 raw.
- `benchmark_returns_for(panel, entry_session, maturities, virtual)`: **신호마다** 같은 진입 세션·같은 만기 봉의 SPY 총수익. `benchmark_trailing_returns()`는 참고 표시 전용으로 격리.
- `agreement_components(..., benchmark_return)`: §2.1대로 `D`를 초과수익 기준으로. `summarize_grades`도 초과수익 없는 신호를 0으로 갈음하지 않고 제외.
- `bars_for(..., limit=None)`: 200봉 절단 제거(raw/sadj/adj/tr_open 동시 제공).

**R08 · `engine/utils/dates.py`, `engine/ledger/record.py`, `scripts/run_daily.py`, `live.py`, `web/app.js`**
- `target_session_for(published_at)`: 발행 시각 이후 주문 마감이 아직 오지 않은 가장 이른 NYSE 세션(감사 예시 4건 일치).
- `three_times(...)`가 **실제 발행 시각**을 쓰고 `target_session`·`signal_published_planned_at`을 반환. 늦은 실행은 다음 세션으로 롤오버.
- `publish_record(..., target_session, signal_published_planned_at)` + `check_target_session()`(거래일 여부 · `order_cutoff_at(session) > 실제 발행 시각` · `cutoff ≤ published`). `attach_target_session()`으로 `build_payload` 시그니처를 건드리지 않고 붙임(경계 계약 유지).
- `settle()`: `target_session`의 시가만 사용, 봉이 없으면 `SESSION_BAR_MISSING` 보류(대체 세션 금지). 구 레코드는 폴백 + `legacy_session: true`.
- `app.js`: 발행=실제 시각, 계획과 1분 이상 다르면 `· 예정 …`, `· 주문 {target_session}` 병기.

**R09 · `engine/backtest/execution.py`, `engine/decide/signals.py`**
- `EntryBand`를 `stop_min_sadj/limit_max_sadj/dma200_floor_sadj/provisional_stop_sadj/split_factor`로 재정의(구 이름 프로퍼티 유지 → `portfolio.py`·`demo.py` 무수정 호환). `as_dict()`는 `basis:"split_adj_ohlc"` + `display_raw`.
- `build_entry_band(iv_median_sadj, required_margin, dma200_sadj, signal_price_sadj, stop_distance, …)`: raw 인자명은 `TypeError`.
- `opening_fill(band, open_raw, slippage_bps, split_factor_open=None)`: 세션 factor로 **1회 환산 후 판정**, 체결가는 raw로 되돌림. `band_from_dict()`로 구 레코드 마이그레이션(`band_basis_migrated`).

**R10 · `engine/backtest/snapshot.py`, `replay.py`, `calibration.py`**
- `make_snapshot(..., inputs=)`: `panels`를 뺀 데이터셋 전체를 canonical JSON(`inputs.json`)으로 저장 → `snapshot_id`에 자동 포함. 제네릭 직렬화(NaN→null, datetime→ISO Z, set→정렬, dataclass 태그) + `load_snapshot_dataset()`(panels CSV + inputs 복원, `AnnualFundamentals`까지).
- `replay()`: **`build_payload`를 그대로 호출**(최소 루프 삭제). 라이브 payload와 등급·게이트·veto·entry_band 동일성을 테스트로 고정.
- `calibrate(evaluate, base_params, …, stress_pass=)`: 후보마다 폴드 재실행, 수락은 **Agreement +2.0점 · 개선 폴드 2개 · 스트레스 통과**. 목적함수는 기록용.

**R11 · `engine/ledger/pilot.py`, `live.py`, `scripts/run_daily.py`, `web/app.js`**
- `eligible_samples()`: `buy_filled` + 3개월 `complete`만. WATCH/no_action은 표본 0건이며 4지표 계산 불가 사유를 남긴다. 결과에 `n_records`·`n_eligible` 병기.
- `data/ledger/portfolio/history.jsonl`(하루 1행 멱등) + `avg_exposure_from_history()`. `summary().strategy.avg_exposure`는 history 평균(없으면 None — `invested_pct` 대입 제거).
- 파일럿 ③은 평가 기간 SPY 총수익. `app.js`: `유효 표본 N_eff · 발행 신호 N건(체결·만기 M건)` + 지표별 계산 불가 사유 표시.

### 3.2 트랙 B — SEC 접수 시각·분할·평가·FRED 빈티지 (R03·R04·R05·R06·R07)

**R06 · `engine/data/sec_edgar.py`, `engine/data/xbrl_fundamentals.py`**
- `acceptance_index(submissions) -> {accn: acceptanceDateTime}` 신설(**추가 네트워크 0** — `filings.recent` 재사용).
- `annual_facts(payload, decision_at, acceptance=)`: 시점 필터를 **시각 비교**로(`acceptance[accn] ≤ decision_at`, 없으면 `filed ≤ 날짜` + 진단 플래그). 같은 (지표, 회계연도)는 결정 시점까지 **가장 늦게 공개된 유효값**을 쓴다(10-K/A·재작성 비교치 포함).
- `collect_fundamentals`가 `.date()`로 깎지 않고 datetime 그대로 전달(누수의 뿌리 제거).

**R05 · `xbrl_fundamentals.py`, `engine/metrics/fundamentals.py`**
- `split_events_from_panel(panel)`: `split_factor` 하락 지점에서 `{ex_date, ratio}` 복원(별도 API 없음).
- `_share_basis(...)`: 결정일까지 알려진 분할만 반영. 그 분할 ex-date 이후 제출본은 이미 재작성(restated)으로 보고 곱하지 않으며, 근거를 연도별 `shares_basis`(`as_reported|restated|split_adjusted|unverified`)로 남긴다.
- `dilution_3y()`는 기준 미확인(`unverified`)이면 NaN, `hard_gate_checks`는 NaN을 **`HG-09_UNVERIFIED`로 탈락**(§3.3의 라벨 연결).

**R03 · `xbrl_fundamentals.py`, `metrics/fundamentals.py`**
- 6년 수집(ΔNWC 5년 시계열 확보), 점수·게이트는 앞 5년 기준 유지.
- `operating_nwc()` = (유동자산 − 현금 − 시장성단기투자) − 유동부채, `delta_nwc_series()`의 5% 상한을 **백만 달러 단위**로 적용.
- `valuation_multiplier`가 5년 중앙값으로 정상화(`nopat_norm`·`fcff_norm`·`growth = clamp(5년 FCFF CAGR, 0, 15%)`). FCFF≤0이면 DCF 폐기 후 EPV·Graham 중앙값(`v_confidence C`, `dcf_dropped`).
- 순부채 = 이자부채 − 현금 − 시장성단기투자. PEG는 **TTM EPS**(10-Q 누적) ÷ **5년 EPS CAGR**(불가 시 매출 CAGR 폴백, `peg_basis`·`peg_growth_basis` 표시).
- WACC 이름 분리: `metrics.fundamentals.wacc → qc_wacc`(Q 경로), `xbrl.valuation_wacc`(평가 경로). 워치리스트·신호에 `wacc_qc`·`wacc_valuation` 병기.
- **`engine/metrics/valuation.py`는 계산식을 그대로 두었다** — 결함은 배관이었다(감사 검증 문서 §2-R04).

**R04 · `xbrl_fundamentals.py`, `collect.py`, `pipeline.py`**
- `valuation_multiplier` 반환 확장: `iv_median/iv_low/iv_high`, `iv_dcf{base,bear,bull}`, `iv_epv`, `iv_graham`, `methods{name: value}`, `median_method`, `v_confidence`, `net_debt`, `iv_band_basis`, `fcff_normalized/fcff_years`, `growth/growth_basis`, `dcf_dropped`.
- `collect_fundamentals`: `reason`만 남기던 유실 제거 → `valuation_inputs[ticker]`에 전부 + `fundamental_sources`(연도별 10-K `accn`·`filed`·접수시각)·`shares_used`·`peg_basis`.
- `build_views`: `iv_median/iv_low/iv_high`를 detail 우선으로 사용, 없을 때만 ±15% 폴백 + `iv_band_basis:"fallback_15pct"`.
- `_thesis` 부호 수정: `margin ≥ 0` → “IV 대비 N% 할인”, 음수 → “IV 대비 N% 프리미엄(안전마진 −N%)”.
- `evidence_items`: 재무 근거에 실제 계산에 쓴 10-K의 `period_end/form/accession/filed_date/available_at`, 최근 10-Q는 `role:"reference"` 별도 항목, 모르는 값은 `as_of` 폴백 대신 키 생략.

**R07 · `engine/data/macro.py`, `collect.py`, `pipeline.py`**
- `parse_observations(..., mode="auto"|"live"|"replay")`: **live** = 모든 행 `available_at = first_ingested_at` / **replay** = `realtime_start=realtime_end=V` 점조회(요청 빈티지보다 늦은 `realtime_start`가 오면 `MacroVintageError` → 그 시리즈만 제외·`macro_missing_series` 기록) / **auto** = 기존 v1.3.2 규칙 보존.
- `collect_macro()`가 `diag["macro_ingest"] = {data_cutoff_at, inputs_ingested_at, lag_seconds, after_cutoff, available_at_source}`를 남기고 `payload["ingest"]`로 올라간다. 3개월 추세·CPI YoY는 **같은 빈티지 프레임**에서 계산.

### 3.3 통합 마무리 3건 (트랙 경계에 걸쳐 있던 것)

1. **`engine/screener.py` — `GATE_LABELS`에 `HG-09_UNVERIFIED` 추가**
   `"HG-09_UNVERIFIED": "3년 희석 판정 불가(주식수 기준 미확인)"`. 없으면 탈락 사유에 코드가 그대로 노출된다(트랙 B가 만든 새 게이트 코드인데 라벨 사전은 트랙 A 소유 파일에 있었다).
   - 회귀 테스트: `tests/test_screener.py::test_unverified_dilution_is_excluded_with_korean_reason` — `HG-09_UNVERIFIED`로 탈락하고 사유가 **문장**으로 찍히며 코드 문자열이 새지 않음을 고정.
2. **`web/app.js` + `scripts/build_public_data.py` + `config/public_allowlist.yaml` — 수집 지연 배너**
   `payload.ingest`(`lag_seconds`·`after_cutoff`·`inputs_ingested_at`)가 있으면 상단 신선도 영역에 **`수집 지연 N분(마감 후 수집) · 수집 <시각>`** 한 줄을 붙인다(`ingestHint()`). `after_cutoff=true` 또는 지연 ≥ 60초일 때만 표시하고, 값이 없으면 아무것도 그리지 않는다.
   - 배관: `build_meta()`가 `ingest`를 넘기고, allowlist에 **`ingest`·`lag_seconds`·`after_cutoff`·`inputs_ingested_at`** 4키만 추가(그 전에는 허용 목록 필터에서 통째로 사라졌다).
3. **`engine/ledger/record.py` — `target_band.defensive` 직렬화**
   `target_band()`는 `gap ≤ 0`일 때 `defensive: true`를 계산하는데 발행 레코드 직렬화가 버려, 화면·채점이 “방어 목표인데 상승 목표처럼 보이는” 밴드를 읽었다. `defensive`가 **있을 때만**(None이 아닐 때만) 저장하고, 플래그를 모르는 구 밴드는 키를 만들지 않는다(기본값을 지어내지 않음).
   - 스키마: `schemas/quant_signals.schema.json`의 `target_band`가 이미 `defensive: boolean`을 허용(선택 키) → 스키마 무수정. allowlist에 `defensive` 키 추가(공개 산출물 필터 통과).
   - 회귀 테스트: `tests/test_ledger.py::test_target_band_defensive_flag_survives_serialization` — True/False 보존 + 구 밴드에서 키 미생성.

**추가(커밋 가드 정밀화) — `scripts/check_commit_scope.py`**

SEC companyfacts 픽스처(`tests/fixtures/googl_companyfacts.json`, 캐시 축약본)의 `units: {"shares": [...]}`는 XBRL **단위 이름**인데, 금지 키 `shares`(계좌 보유 주식수)와 같은 키라 가드가 커밋을 막았다. 해결은 `shares`를 허용하는 것이 아니라 **부모가 `units`인 키만** 금지 키 검사에서 제외하는 것이다(‘단위·스키마 이름’). `position: {"shares": 3.2}` 같은 계좌 정보는 그대로 차단된다 — `tests/test_commit_scope.py::test_xbrl_unit_named_shares_is_not_a_position`가 두 방향을 함께 고정한다.

### 3.4 경계 계약 이행 확인 (감사 검증 문서 §4.2)

| 계약 | 이행 |
|---|---|
| `build_payload(data, …)` 시그니처 불변 | 지켜짐 — `target_session`은 `attach_target_session()`으로 실행 스크립트가 붙임 |
| `snapshot.inputs = {k: v for k, v in data.items() if k != "panels"}` | 지켜짐 — B의 신규 필드가 자동으로 해시에 포함 |
| `EntryContext.split_factor` 공급(B) / 사용(A) | 지켜짐 — `pipeline.build_views`가 `panel.frame["split_factor"].iloc[-1]`을 넣고 A가 `build_entry_band`로 전달 |
| 테스트 파일 비공유 | 지켜짐 — A 7파일 / B 5파일, 통합분만 기존 `test_screener.py`·`test_ledger.py`에 1개씩 추가 |
| `.github/workflows/*` 무수정 | 지켜짐 — 이번 작업에서 워크플로 파일 diff 0건 |

---

## 4. 테스트·검증 실측

```
$ .venv/bin/python -m pytest -m "not network"
373 passed, 4 deselected in 10.50s

$ .venv/bin/python -m ruff check engine scripts tests
All checks passed!

$ .venv/bin/python scripts/validate_outputs.py
{"ok": true, "signals": {"n_signals": 7, "n_blocked": 0, "status": "normal", ...},
 "pilot": {"verdict": "보류", "n_eff": 0}, "watchlist": {"n": 22}}   # 스키마·3시각 순서·체결가 null·금지 필드 부재 통과

$ git add … && .venv/bin/python scripts/check_commit_scope.py --staged
(문제 0건)
```

**신규 회귀 테스트 90개**

| 트랙 | 파일 | 개수 | 고정한 것 |
|---|---|---|---|
| A | `test_ledger_executions.py` | 4 | 재고정 파일이 체결을 덮지 않음, 재정산 차단 해소 |
| A | `test_grade_maturity.py` | 7 | 만기 봉 미관측 시 채점 보류, TR 기준 수익률·MDD, 신호별 SPY |
| A | `test_session_and_times.py` | 7 | 실제 발행 시각·대상 세션 4예시, 늦은 실행 롤오버, 세션 불변식 |
| A | `test_execution_order_band.py` | 7 | sadj 단일 기준(감사 재현: `stop_min 48.5`, raw 97 미체결), 구 레코드 마이그레이션 |
| A | `test_snapshot_replay.py` | 6 | 전체 입력 해시·왕복 복원, 라이브/재생 동일 등급 |
| A | `test_calibration_reruns.py` | 5 | 후보별 폴드 재실행, 수락 규칙 3종 |
| A | `test_pilot_samples.py` | 8 | 체결+만기 표본(WATCH 15건 → `n_eff 0`), 노출도 history, 발행 수 병기 |
| B | `test_xbrl_acceptance.py` | 6 | 접수 시각 경계, 정정 공개 전/후, 폴백 플래그 |
| B | `test_split_dilution.py` | 7 | NVDA −2.22 / WMT −2.19 / AVGO +14.7 유지, 결정일 이후 분할 미적용 |
| B | `test_valuation_normalized.py` | 12 | 6년 수집·5년 채점, 영업 ΔNWC, 5% 상한 단위, 정상화·WACC 분리·TTM PEG |
| B | `test_valuation_plumbing.py` | 10 | bear/bull 밴드 미재구성, 방법별 값 전달, 10-K `accn` 근거, 10-Q reference 분리 |
| B | `test_macro_vintage_paths.py` | 9 | 라이브 수집 시각·`ingest`, 재생 빈티지 점조회, 결정별 CPI YoY |
| 통합 | `test_screener.py` +1 / `test_ledger.py` +1 / `test_commit_scope.py` +1 | 3 | `HG-09_UNVERIFIED` 라벨 / `defensive` 보존 / XBRL `units.shares` 오탐 방지 |

**개정한 기존 테스트 6개**: `test_xbrl_fundamentals.py`(최초값 고정 → 최신 유효값 + 경계 2건), `test_radar_holdout_pilot.py`(체결·만기 표본), `test_no_holdout_leak.py`(`calibrate(evaluate, …)`), `test_ledger.py`(TR·밴드), `test_decide_signals.py`·`test_lookahead.py`·`test_technical_price_kind.py`(밴드 인자명). 테스트 픽스처 `tests/fixtures/googl_companyfacts.json`(SEC 캐시 축약 124KB) 추가 — 캐시 원본은 gitignore라 커밋에 넣지 않았다.

---

## 5. 실데이터 재스크린 (`scripts/run_screen.py --as-of 2026-11-20`, 로컬 실키)

R03·R05·R06 반영 후 1층 스크리너를 실데이터로 다시 돌린 결과다(소스: yfinance·SEC XBRL·FRED 라이브).

| 항목 | 이전(커밋본) | 이번 재스크린 |
|---|---|---|
| 워치리스트 | 19종목 | **22종목** |
| 신규 편입 | — | **NVDA**(HG-09 해소) · **WMT**(HG-09 해소) · **TSLA**(V_UNAVAILABLE → 계산 가능) |
| 편출 | — | **없음** |
| 가치평가 보류 | 6종목 `JPM·BAC·V·MA·AMT·PLD` | 동일 6종목 |
| 탈락 | 18종목 | 15종목 |

**탈락 사유 변화(핵심)**

| 종목 | 이전 게이트 | 이번 | 해석 |
|---|---|---|---|
| **AVGO** | `HG-09` | `HG-09` | **정당 탈락 유지** — VMware 인수 대가 주식 발행으로 실제 3년 희석 +14.7% |
| **NVDA** | `HG-09` | *(통과)* | 분할(10:1, 2024-06) 반영 시 −2.22% — **오탈락 해소** |
| **WMT** | `HG-09` | *(통과)* | 분할(3:1, 2024-02) 반영 시 −2.19% — **오탈락 해소** |
| **NFLX** | `HG-08`, `HG-09` | `HG-08` | 분할 오인은 해소됐지만 **FCF/순이익 < 0.7로 여전히 탈락** |
| **TSLA** | `V_UNAVAILABLE` | *(통과)* | 정상화 FCFF/EPV로 V 계산 가능 |
| ORCL·AMZN·BA | `HG-09` 포함 | 동일 | 실제 희석 |

**랭킹(상위)** — GOOGL 63.669 / ADBE 62.500 / META 60.735 / NVDA 58.770 / MSFT 54.387 / DIS 49.300 / CSCO 48.840 …

**주의할 관찰 2가지(회귀 아님, 기록)**
1. **전 22종목 V=0.00** — 모델 IV가 현재가보다 낮다는 판정이다. 예: GOOGL `IV_median $60.79`(`EPV 60.79 / Graham 90.86 / DCF 51.09`, `median_method epv`, 밴드 `51.67~69.91`, `wacc_valuation 10.64%`) vs 현재가 **$272.66** → 안전마진 **−348.5%**. 그래서 이번 주 2층 BUY 신호는 자연히 0건이다. 규칙을 완화하지 않았다(§6.1).
   - 참고: 로컬 오프라인 재계산(β=1.15·rf=4.37% 가정)에서는 IV $69.07이 나왔다. 라이브는 가격 회귀 β로 `wacc_valuation 10.64%`를 써서 **$60.79**다. 화면·문서의 기준값은 **실행 산출물($60.79)** 이다.
2. **GOOGL G 점수 39.36 → 91.97** — PEG가 `2.134`(FY 순이익 ÷ 매출 CAGR)에서 `1.197`(TTM EPS 19.27 ÷ 매출 CAGR 11.82% 폴백)로 바뀐 결과다. `peg_growth_basis`로 폴백임을 화면에 표시한다.
3. `data/watchlist/history.jsonl`은 **`as_of` 당 1행 멱등**으로 바꿨다(같은 날 재실행 시 앞선 행 대체). `data/watchlist/current.json`의 `metrics`에는 `wacc_qc`와 `wacc_valuation`이 함께 남는다(§3.3 관련 후속 확인 항목).

---

## 6. 이번에 포함하지 않은 항목 (의도적)

### 6.1 `v_penalties`·`v_score` 규칙 자체 — Phase 4 후보
- `Candidate.v_penalties`는 존재하지만 `run_screen`이 값을 넣지 않고, `v_score(margin)`의 구간(−20% = 0)도 기획서 §3.2.3 그대로다. **R03~R05는 IV의 정확성을 고친 것이지 V 규칙을 바꾼 것이 아니다.**
- 결과적으로 전 종목 V=0 → BUY 0건이 지속된다. 이는 “IV가 시장가보다 낮다”는 모델 판정이므로 코드 결함은 아니지만, **투자 판단에 쓰려면 IV 레벨(성장 프리미엄 반영 여부)을 사용자와 정해야 한다.** 규칙 변경은 기획서 개정 사항이라 이번 범위에서 손대지 않았다.

### 6.2 폴드 재실행·보정 러너 배선
- R09로 1차 진입 판정 기준이 바뀌어 **기존 폴드 성적과 비교 불가**다. `walkforward/folds`를 다시 돌려야 하는데, 이번 작업은 코드·테스트·스크리너까지이고 **폴드 재생성은 다음 배치/Phase 4 작업**이다.
- `scripts/run_walkforward.py --calibrate`의 실제 `evaluate` 주입은 목록 밖 파일이라 남겼다(인터페이스 `calibrate(evaluate, …)`는 고정).

### 6.3 라이브 Ledger의 2·3차 분할·손절·익절 연결
- 라이브 일일 실행은 여전히 **1차 체결 중심**이고 2·3차·손절·익절·트레일링이 일일 루프에 연결되지 않았다(`docs/phase3_verification.md` §9에 이미 미연결로 기록). `live.replay()`가 `INITIAL_CASH × weight`로 수량을 잡아 자산 증가가 반영되지 않는 한계도 같은 뿌리다 — **Phase 4 범위**.

### 6.4 그 밖
- 13F 라이브 수집기 없음(레이더는 입력 파일/지난 값 유지), `daily-postclose.yml`의 `schedule` 주석 상태(워크플로 파일은 토큰 권한 403으로 수정 불가 — 수동 `workflow_dispatch`만).
- `collect_macro()`의 시리즈별 실패 격리는 R07에서 개선했지만, “한 시리즈 예외로 전체 macro를 비우는” 경로가 남아 있는지 추가 점검은 하지 않았다.

---

## 7. Actions · 실사이트 검증 (2026-11-20 13:0x KST)

| 항목 | 값 |
|---|---|
| 통합 커밋 | `44da6ca` (R01~R11 + 통합 3건 + 재스크린 데이터 + 문서) |
| `ci` run | [44671788223](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44671788223) **success** — Lint · 오프라인 테스트 · scope 가드 · 공개 빌드 가드 전 스텝 성공 |
| `daily-postclose` run | [44671878797](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44671878797) **success** — report(1m44s) + deploy(9s) 전 스텝 성공 |
| 후속 수정 커밋 | `666ed4a`(파일럿 문구 모순 제거) → `ci` [44672238173](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44672238173) **success**, `daily-postclose` [44672331459](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44672331459) **success**(report + deploy) |
| Actions 커밋 | `a1f4cef`, `7b863e7` (`data: quant signals 2026-11-20`) — 로컬 `git pull --ff-only`로 동기화 |

**동기화한 실데이터(`data/quant_signals.json`, 7b863e7)**

| 항목 | 값 |
|---|---|
| `status` / `mode` / `source` | `normal` / `postclose` / `live` |
| `signal_published_at` | **`2026-11-20T04:08:24Z`** (13:08 KST, 실제 실행 시각) |
| `signal_published_planned_at` | `2026-11-19T22:02:11Z` (07:02 KST 계획값, 화면에 `· 예정`으로 병기) |
| `data_cutoff_at` / `order_cutoff_at` | `2026-11-19T22:00:00Z` / `2026-11-20T14:25:00Z` |
| `target_session` | **`2026-11-20`** (발행 뒤 마감이 남은 세션) |
| `ingest` | `lag_seconds 22131.4`(약 369분) · `after_cutoff true` · `inputs_ingested_at 04:08:51Z` → 화면 배너 `수집 지연 369분(마감 후 수집)` |
| 레짐 | `risk_on` · `verified true` · 투표 `real_rate/t10y2y/credit risk_on · volatility/dollar neutral` · 배수 1.00 |
| 신호 | 매수 0 · 매도 0 · **관찰 7**(MSFT·NVDA·ADBE·CSCO·GOOGL·META·CAT), 차단 0 |
| GOOGL | 점수 65.78 · IV `60.79` DCF 51.09/EPV 60.79/Graham 90.86 · 밴드 `51.67~69.91` · `iv_band_basis bear_bull` · 문구 “IV 대비 349% 프리미엄(안전마진 -349%)” · `target_band.defensive true` · 근거 10-K `0001652044-26-000018`(filed 2026-02-05) + 10-Q `0001652044-26-000084`(`role: reference`) |
| `pilot_eval.json` | `n_records 7` · `n_eligible 0` · `n_eff 0` · `verdict 보류` · `cash_equivalent 0.0`(평균 노출 0.0 혼합 대비, WATCH 부풀리기 해소) |
| 모의 계좌 | `cash_pct 1.0` · `invested_pct 0.0` · `avg_exposure 0.0` · 보유 0 |
| 아카이브 | 비공개 `us-stock-advisor-archive` Release `snapshots-2026`에 `snapshot_2026-11-20_5058a21f4561.tgz`(2,548,448B) / `…_27c9e9b5e1ab.tgz` 업로드 — 입력 전체를 담아 크기가 1.16MB → 2.5MB로 커졌다 |
| `summary.json` | `data/ledger/summary.json`은 만들지 않는다(계약상 `web/data/ledger/summary.json`, 23.5KB — 빌드 산출물·gitignore). `quant_signals.ledger_summary`에 같은 값이 커밋된다 |

**실사이트** `https://top03071188-debug.github.io/us-stock-advisor/?_cb=20261120T0410` (브라우저 실측 13:10 KST)

- `오늘`: `데이터 정상` · `발행 11. 20. 13:08 · 예정 11. 20. 07:02 · 마감 11. 20. 07:00` · **`수집 지연 369분(마감 후 수집) · 수집 11. 20. 13:08`** · `시장 레짐: 위험 선호(Risk-On) 포지션 배수 1.00 · 검증됨` · 신호 카드 7장(각 카드에 `발행 13:08 · 예정 07:02 · 주문 2026. 11. 20.`) · `차단된 종목 없음`.
- `종목` → GOOGL: 7블록 전부 렌더(결론·대가별 점수·IV 밴드 $51.67~$69.91·주 1회 차트·뉴스 5건·공시 표 5행·invalidation `$327.18`·토론 빈 카드). 콘솔 예외·빈 영역 없음.
- `성적표`: 홀드아웃 잠금(다음 개봉 2027-01-01) · 폴드 1M 4/3M 3/6M 2(보류) · 파일럿 `판정: 보류` `유효 표본 0 · 발행 신호 7건 (체결·만기 0건)` · 유사 현금비중 `0.000 · 평균 노출 0.0 혼합 대비`(모순 문구 제거 확인) · 신호 이력 7건.
- `레이더`: 워치리스트 변동 `+NVDA · +TSLA · +WMT`, 보류 업종 6, 규칙 표·Veto 12종 정상.

**스크린샷**(`downloads/phase3-screenshots/`)
- `r01r11-today-2026-11-20.png` — 오늘 탭(수집 지연 배너 포함)
- `r01r11-ticker-GOOGL-2026-11-20.png` — 종목 상세 · GOOGL
- `r01r11-ledger-pilot-2026-11-20.png` — 성적표 · 파일럿 4지표

## 8. 재현 방법

```bash
cd repos/us-stock-advisor
.venv/bin/python -m pytest -m "not network"          # 372 passed, 4 deselected
.venv/bin/python -m ruff check engine scripts tests   # All checks passed
# 실데이터 재스크린(.env 의 FRED_API_KEY·FINNHUB_API_KEY·SEC_USER_AGENT 필요)
set -a; . ./.env; set +a                              # SEC_USER_AGENT 에 괄호가 있으면 export 로 넘긴다
.venv/bin/python scripts/run_screen.py --as-of 2026-11-20
.venv/bin/python scripts/validate_outputs.py
.venv/bin/python scripts/build_public_data.py          # web/data/* (allowlist 필터·200KB 상한)
git add -A && .venv/bin/python scripts/check_commit_scope.py --staged
```
