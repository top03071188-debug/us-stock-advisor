# 감사 `8313fce` 수정 01~10 통합 기록 (v1.3.3)

기준 감사: 커밋 `8313fce`(주간 1층 재스크린 2026-11-20) 시점 코드 · 검증 문서 `scratch/audit_verdict_8313fce.md`.
사용자 결정 2건을 그대로 따랐다 — **01: 마감 전 FRED 빈티지 점조회로 통일해 룩어헤드 0 보장**, **04: V 감점(−4/−10) 현행 유지**.

작업은 트랙 A(01·02·07·09·10 계보·시점·채점·보정)와 트랙 B(03·04·05·06·07-XBRL·08 재무·밸류·표본·배선)로 나눠
같은 작업 트리에서 진행했고, 이 문서는 그 **경계 통합·실데이터 재스크린·배포 검증**까지 끝낸 결과다.

---

## 1. 항목별 수정 요약

| 항목 | 결함(감사) | 수정 | 주요 파일 |
|---|---|---|---|
| **01** | 라이브 수집이 마감 뒤 값을 `available_at = 수집 시각`으로 붙여 레짐 `verified: true`·`normal` 이 됐다 | 라이브도 **마감 빈티지 점조회**(`vintage = 마감일 ET − 1일`, `available_at <= data_cutoff_at`)로 통일. 배치 시작(`run_started_at`)과 발행(`signal_published_at`)을 분리하고, 발행이 주문 마감을 넘겼는지(`published_after_order_cutoff`)를 남긴다 | `engine/data/macro.py`, `scripts/run_daily.py`, `engine/data/collect.py` |
| **02** | 같은 날 재실행이 `record_skips`로 전부 거부된 채 화면엔 **다른 스냅샷**이 나갔다 | 입력 지문(`inputs_hash`)이 같으면 원장의 발행본을 그대로 배포하고, 입력·파라미터·대상 세션이 달라 새로 확정된 발행은 **회차 접미사(`-002`) + `supersedes`** 로 남긴다. 스냅샷은 최종 입력(워치리스트 필터·보유·섹터 비중·세 시각·`target_session`·파라미터) 확정 뒤에 만들고, 같은 날 입력이 바뀌면 `{as_of}-r2` 로 **덮어쓰지 않는다** | `engine/ledger/record.py`, `scripts/run_daily.py`, `engine/backtest/snapshot.py`, `engine/backtest/replay.py` |
| **03** | EV/EBIT 주식수 복원이 **표본 시점 이후** 10-K/A 를 읽어 과거 배수를 흔들었다 | 표본 시점·결정 시점 이전에 접수된 최근 주식수만 참조, EPS 와 순이익은 **같은 제출본**, 비교 전 분할 기준을 일치, `shares_source`(reported·eps_implied·missing) 기록 | `engine/data/xbrl_fundamentals.py` |
| **04** | V 감점 규칙은 유지(사용자 결정)하되, 설정이 `config/params_v001.yaml` 과 분리돼 **죽은 키**가 있었다 | 감점식은 v1.3.2 그대로. `valuation:` 섹션을 `valuation.py`·`xbrl_fundamentals.py` 가 단일 경로로 읽게 하고, 워치리스트 22행에 `v_raw`·`margin`·`hist_divergence_signed`·표본 품질을 남긴다 | `config/params_v001.yaml`, `engine/metrics/valuation.py`, `engine/screener.py`, `scripts/run_screen.py` |
| **05** | 성장률 0% 클립이 화면에 "허용 ±40% 이내"로 보였고, bear==bull 이라 민감도가 항상 0 | `growth_raw_cagr`·`growth_clipped_to_zero`·`effective_fcff_years`·`normalization_method` 를 명시 반환. 유효 FCFF 5년 미만은 5년 DCF 경로를 폐기(`dcf_dropped=SHORT_HISTORY`). 0% 클립이면 **WACC ±1%p·영구성장 ±0.5%p 민감도**를 별도로 계산해 화면에 쓴다 | `engine/data/xbrl_fundamentals.py`, `engine/metrics/valuation.py`, `web/app.js` |
| **06** | 최신 배수 무효일 때 과거 표본값을 현재값으로 재사용했고, `ev_ebit_window` 가 **예정** 날짜 범위였다 | 최신 배수를 과거 루프와 독립 계산하고 무효면 현재 배수·백분위를 `None`(보류). `ev_ebit_window` 는 **실제 유효 표본 첫날~마지막날**, `samples`·`years`·`reason` 을 공개 산출물까지 전달 | `engine/data/xbrl_fundamentals.py`, `config/public_allowlist.yaml`, `scripts/build_public_data.py`, `web/app.js` |
| **07** | 접수 시각 누락 시 날짜 문자열 비교로 통과(22:30Z 접수 공시가 22:00Z 결정에 샘), 공용 함수가 **두 곳**에 따로 있었다 | `engine/utils/dates.sec_conservative_available_at` **하나**가 정의이고, `engine.data.sec_edgar.acceptance_available_at` 은 같은 함수로 위임한다(구 이름 `acceptance_fallback_available_at` 은 같은 객체). XBRL 연차·TTM·개정 세 경로 모두 이 함수 + 최종 타임스탬프 비교를 쓴다 | `engine/utils/dates.py`, `engine/data/sec_edgar.py`, `engine/data/xbrl_fundamentals.py` |
| **08** | 영업 ΔNWC 가 단기차입금 증감에 오염되고, 총부채가 중복/누락되며, `minority_interest=0.0` 하드코딩, 세율은 연도가 어긋난 쌍으로 계산 | 영업 유동부채에서 단기금융부채를 뺀 순수 NWC, **부채 우선순위 함수**(비유동 + 유동 + 단기차입, 미보고/0 구분), 비지배지분을 기업가치에서 차감(모회사 귀속 자본 우선), 유효세율은 **같은 회계연도 쌍**만 | `engine/data/xbrl_fundamentals.py` |
| **09** | 과거 컷오프 재수집이 미래 분할을 남기고, 현재 `fast_info` 시총을 과거에 썼으며, 채점이 `tr_open` 시가만 쓰고 만기 후 체결도 채점했다 | 컷오프 시점 기준 `raw`/`sadj`/`split_events` **재기준화**, 컷오프 시총 = 컷오프 종가 × 컷오프 주식수(오늘 실행만 `fast_info`), `target_band.base_bar_date`·`return_base` 고정, 실제 체결가 정규화 채점 + 만기 후 체결은 보류 | `engine/data/prices.py`, `engine/data/collect.py`, `engine/decide/pipeline.py`, `engine/ledger/grade.py` |
| **10** | `--plan-only` 가 `attempts.json` 에 `"calibrate"` 라고 쓰고, 후보 수락이 `zip()` 위치 비교이며, 평면 키가 엔진 설정에 닿지 않았다 | `"planned"`/`"deferred"` 라벨, `--min-folds` 존중, `(horizon, fold_id, maturity)` 키 매칭 + 계약 검사(중복·누락·코드/스냅샷 버전·홀드아웃 침범·폴드당 신호 25건·`stress_pass`), `map_candidate_to_params()` | `scripts/run_walkforward.py`, `engine/backtest/calibration.py`, `engine/backtest/walk_forward.py` |

### 1.1 정책으로 못 박은 것

- **01 빈티지 차단**: `ingest.policy = "cutoff_vintage_point_query"`, `cutoff_filter = "available_at <= data_cutoff_at"`,
  `vintage = 마감일 ET − 1일`. 대가: 마감 당일 발표분은 그날 결정에서 빠지고, 빈티지 응답이 스냅샷이면 그 시리즈를 버려
  `inputs_missing`(레짐 미검증·포지션 배수 하향)이 된다. **누수 0을 우선**한 사용자 결정에 따른 것이다.
  수집 지연은 숨기지 않고 `ingest.lag_seconds`·`after_cutoff` 로 남긴다.
- **04 V 감점 유지**: `v_penalty_hist = −10`(역사 EV/EBIT 괴리 ≥ 20%), `v_penalty_dcf = −4`(DCF 민감도 ≥ 40%),
  `v_score` 3구간식 모두 **v1.3.2 그대로**다. 바뀐 것은 "보이는 정도"뿐 — `v_raw`(감점 전)와 부호 있는 괴리
  (`hist_divergence_signed`)를 함께 낸다.

---

## 2. 경계 통합 (트랙 A ↔ 트랙 B)

트랙이 서로의 파일을 건드리지 않아 생긴 **배관 단절**을 여기서 닫았다.

1. **07 공용 함수 단일화** — `engine/utils/dates.sec_conservative_available_at(filed_date, acceptance_datetime)` 이
   유일한 정의. `datetime`·`str`·`None` 을 모두 받는다(문자열 시간대 없으면 ET, 없으면 `23:59:59 ET` → UTC).
   `sec_edgar.acceptance_available_at` 과 구 이름 `acceptance_fallback_available_at` 은 **같은 함수 객체**이고,
   XBRL 연차·TTM·개정 3경로가 모두 이 한 곳을 거친다. 테스트 `test_sec_acceptance_helper_is_one_path_to_dates`.
2. **`pipeline.build_payload` → `record["valuation"]`** — 트랙 B 계산 필드 14개를 레코드에 배관:
   `v_raw`·`growth_raw_cagr`·`growth_clipped_to_zero`·`effective_fcff_years`·`normalization_method`·
   `wacc_sensitivity`·`terminal_g_sensitivity`·`rate_sensitivity`·`hist_divergence_signed`·
   `ev_ebit_current/samples/years/window/reason`·`minority_interest`·`debt_reporting`.
   `watchlist[]` 행에도 `v_raw`·`margin`·`ev_ebit_*`·`growth_clipped_to_zero`·`wacc_sensitivity` 를 추가해
   1층(주간)과 2층(일일)이 같은 근거를 보여 준다.
3. **`collect` → `universe[]`** — `margin`(IV 중앙값 ÷ 컷오프 종가 기준), `hist_divergence_signed`,
   `ev_ebit_current/samples/years/window/reason`, `growth_raw_cagr`·`growth_clipped_to_zero`·
   `effective_fcff_years`·`normalization_method`·`wacc_sensitivity`·`terminal_g_sensitivity` 를 일관되게 채운다.
4. **공개 계층** — `config/public_allowlist.yaml` 에 01·02·09 의 새 키(`run_started_at`·`publish_lag_seconds`·
   `published_after_order_cutoff`·`inputs_hash`·`revision`·`supersedes`·`base_bar_date`·`return_base`·
   `market_cap_source`·`wacc_risk_free`·`fallback`, 그리고 거시 빈티지 메타 `policy`·`vintage`·`series_dropped`·
   `cutoff_filter`·`available_at_source`·`replay`)를 추가하고 `build_public_data` 가
   `meta.json`(배치 시작·발행 지연·마감 후 발행·입력 지문·WACC 무위험금리 출처)과
   `signals/today.json`·`ledger/records.json`(`revision`·`supersedes`)에 실제로 내보내게 했다.
5. **화면(`web/app.js`)** — 배치 시작·발행까지 걸린 시간·입력 지문, **"주문 마감 후 발행" 배지**(`published_after_order_cutoff`),
   DGS10 미확보 시 기본값 사실, 신호 카드의 **"재발행 2회차 · …-001 대체"** 배지, 원장 탭 **회차 열**,
   종목 상세의 **"기준 봉 YYYY-MM-DD 종가 (base_price 고정)"** 줄.
6. **스냅샷 디렉터리 vs 워크플로** — `data/snapshots/{as_of}-r2` 는 `daily-postclose.yml`(커밋 대상 아님)·
   `scripts/archive_snapshot.py`(디렉터리 전체를 결정론적 tgz 로 패키징)·`scripts/check_commit_scope.py`
   (`data/snapshots/` 는 `deny_paths`)와 **충돌 없음**. 하루 1디렉터리 가정은 어디에도 없다.
   `.github/workflows/*` 는 한 줄도 고치지 않았다.
7. **`--plan-only` 라벨** — `data/calibration/attempts.json` 이 `"calibrate"` → **`"planned"`/`"deferred"`** 로 갱신됐다
   (2026-11-20 실행: 1m·3m `planned`, 6m `deferred`). 기존 `attempts.json` 은 다음 `--plan-only` 실행부터 새 라벨을 쓴다.

---

## 3. 실데이터 재스크린 (전후 비교)

- **전**: 2026-11-20 13:38 KST `daily-postclose` run 44675727239 가 만든 커밋 데이터(감사 기준 `8313fce`).
- **후**: 같은 날 실키(`FRED_API_KEY`·`FINNHUB_API_KEY`·`SEC_USER_AGENT`)로 새 코드를 실행 —
  `scripts/run_screen.py --as-of 2026-11-20`(1층) → `scripts/run_daily.py --mode postclose`(2층).
  원본: `out/pre_audit_8313fce/`(전) · `out/run_8313fce/`(후, 로컬 검증용 · git 제외).

### 3.1 워치리스트 (22 → 21, SHW 편출)

| 티커 | V (전→후) | 감점 전 V | 안전마진 | 역사 괴리(부호) | EV/EBIT 백분위 (전→후) | 표본/연수 | 성장률 클립 | WACC ±1%p |
|---|---|---|---|---|---|---|---|---|
| AAPL | 0.00→0.00 | 0.00 | −311.6% | −59.4% | 84.3→84.3 | 121/11 | 아니오 | ±26.7% |
| ADBE | 3.36→3.36 | 13.36 | −39.7% | −76.4% | 1.6→1.6 | 121/11 | 아니오 | ±38.2% |
| CAT | 0.00→0.00 | 0.00 | −206.8% | −59.3% | 67.8→67.8 | 121/11 | 아니오 | ±31.6% |
| COST | 0.00→0.00 | 0.00 | −375.2% | −65.3% | 82.6→82.6 | 121/10 | 아니오 | ±31.6% |
| CSCO | 0.00→0.00 | 0.00 | −208.2% | −37.9% | 89.3→89.3 | 121/11 | **예** | ±17.7% |
| CVX | 0.94→0.94 | 10.94 | −68.7% | −28.9% | 68.4→68.4 | 98/9 | **예** | ±38.7% |
| DIS | 3.82→3.82 | 13.82 | −34.2% | −27.9% | 8.2→8.2 | 85/8 | 아니오 | ±34.8% |
| GOOGL | 0.00→0.00 | 0.00 | −349.4% | −70.8% | **58.7→35.5** | 121/9 | **예** | ±10.8% |
| HD | 0.00→0.00 | 7.34 | −111.9% | −52.7% | 57.0→57.0 | 121/11 | **예** | ±61.6% |
| **HON** | **4.03→0.00** | 0.00 | −201.2% | — | —→—(사유 `insufficient_history`) | 22/2 | 아니오 | — |
| JNJ | 0.00→0.00 | 0.61 | −192.7% | −40.6% | 41.3→40.5 | 121/11 | 아니오 | ±55.7% |
| KO | 0.00→0.00 | 0.00 | −325.5% | −64.6% | 90.1→90.1 | 121/11 | **예** | ±62.9% |
| LIN | 0.00→0.00 | 0.00 | −285.2% | −72.7% | 41.9→39.8 | 93/8 | 아니오 | ±73.8% |
| META | 0.00→0.00 | 0.00 | −290.2% | −65.4% | 21.5→21.5 | 121/11 | **예** | ±12.9% |
| MRK | 0.00→0.00 | 6.67 | −120.0% | −58.3% | 19.0→19.0 | 121/11 | 아니오 | ±67.6% |
| MSFT | 0.00→0.00 | 0.00 | −289.3% | −69.1% | **23.1→21.5** | 121/11 | **예** | ±13.1% |
| NVDA | 0.00→0.00 | 0.00 | −790.8% | −78.4% | 6.6→6.6 | 121/11 | 아니오 | ±10.0% |
| PG | 0.00→0.00 | 4.02 | −151.8% | −58.3% | 44.6→43.0 | 121/11 | 아니오 | ±54.1% |
| **SHW** | 0.00→**편출** | — | — | — | 71.1→— | —/— | — | — |
| TSLA | 0.00→0.00 | 0.00 | −1477.5% | −92.2% | 74.3→74.3 | 70/6 | **예** | ±3.0% |
| UPS | 4.06→4.05 | 14.05 | −31.4% | −39.8% | 39.7→39.7 | 121/11 | **예** | ±40.9% |
| WMT | 0.00→0.00 | 0.00 | −382.1% | −56.3% | 96.7→96.7 | 121/11 | **예** | ±38.4% |

읽는 법: `margin`·`v_raw`·`hist_divergence_signed`·`samples/years`·`growth_clipped_to_zero`·`wacc_sensitivity` 는
**이번에 처음 채워진 키**다(전에는 `None`). V 값 자체는 04 정책(감점 유지)대로 대부분 그대로다.

### 3.2 주요 종목 방법별 IV · WACC · 순부채

| 티커 | DCF IV (전→후) | EPV (전→후) | Graham (전→후) | IV 중앙값 (전→후) | WACC (전→후) | 순부채 (전→후, $M) | FCFF 성장률 (전→후) | DCF 민감도 (전→후) |
|---|---|---|---|---|---|---|---|---|
| GOOGL | 51.09→51.88 | 60.79→60.67 | 90.86 (동일) | 60.79→60.67 | 10.64%→10.66% | −78,300→−77,758 | 0.0%(클립, 원 −5.35%) | 0.0→0.0 |
| ADBE | **306.75→377.85** | 158.66 (동일) | 101.13 (동일) | 158.66 (동일) | 동일 | 동일 | **5.17%→10.30%** | **0.112→0.215** |
| MSFT | 101.06→103.22 | 128.36→128.01 | 154.81 (동일) | 128.36→128.01 | 9.78%→9.80% | 동일 | 0.0%(클립, 원 −13.19%) | 0.0→0.0 |
| META | 134.22→133.82 | 158.32→157.96 | 211.19 (동일) | 158.32→157.96 | 9.74%→9.76% | 동일 | 0.0%(클립, 원 −9.19%) | 0.0→0.0 |
| NVDA | 16.434→16.388 | 8.841→8.823 | 26.59 (동일) | 16.434→16.388 | 13.31%→13.34% | 동일 | 90.27% | 0.283→0.283 |
| CSCO | **67.17→33.08** | 28.95→30.00 | 30.73 (동일) | 30.73 (동일) | **9.61%→8.82%** | **6,954→13,615** | **11.74%→0.0%(원 −0.33%)** | **0.244→0.0** |
| CAT | **362.17→453.22** | 175.16 (동일) | 138.20 (동일) | 175.16 (동일) | 동일 | 동일 | **6.94%→15.00%(원 16.82%)** | **0.171→0.342** |

- **08 ΔNWC 재정의의 파급**이 가장 크다. 단기금융부채를 영업 유동부채에서 빼면서 FCFF 계열이 바뀌어
  CAT(+8.0%p)·ADBE(+5.1%p) 성장률이 올라 DCF IV 가 20~25% 상승했고, CSCO 는 +11.7% → **0%(원 −0.33%)** 로 뒤집혀
  DCF 민감도가 0 이 됐다(이제 WACC 민감도 ±17.7% 로 검증). 04 감점 규칙은 그대로라 **V 순위는 유지**된다.
- **08 부채 우선순위**로 CSCO 순부채가 6,954 → 13,615 $M(단기차입 포함)로 늘어 WACC 가 0.79%p 내려갔다(부채 비중↑).
- **01 빈티지 차단**의 레짐 효과: 실측 레짐은 **변화 없음** — `risk_on`, `verified: true`, `inputs_missing: []`,
  votes(real_rate·t10y2y·credit = risk_on, volatility·dollar = neutral). FRED 는 빈티지 **2026-11-18** 점조회로
  DGS10 **5.10%** 를 확보했고(`wacc_risk_free.source = "FRED:DGS10"`, `fallback: false`), 11개 시리즈 중 버린 것 0개.
  즉 "마감 뒤 값을 쓰던 경로"는 닫혔지만 **결론은 뒤집히지 않았다**(같은 값이 마감 전에도 유효했음).
- **02 발행 회차**: 새 코드 실행에서 7건이 `…-002` 로 기록되고 `supersedes = …-001` 이 붙었다.
  `diagnostics.record_skips` 는 **비어 있었다**(전에는 7건 전부 거부). 화면 `quant_signals.json` 의 `signals` 는
  원장에 append 된 그 발행본 그자체(같은 `signal_id`·`revision`·`inputs_hash`)다.
- **09 채점 기준 봉**: 7건 모두 `target_band.base_bar_date = 2026-11-19`(META 만 11-18), `return_base = "base_price"`.
- **01-b 시각 분리**: `run_started_at 05:49:00Z` → `signal_published_at 05:49:49Z`(49초), `publish_lag_seconds 6.1`,
  `published_after_order_cutoff: false`, `target_session 2026-11-20`.

### 3.3 편출·급락 종목의 원인 (실측)

- **SHW 편출** — `HG-06 순부채/EBITDA > 2.5`. 08-b 부채 우선순위가 단기차입까지 합산하면서
  총부채 9,670.8 → **10,871.3 $M**(비유동 9,320.7 + 유동성장기 350.1 + 단기차입 1,200.5),
  순부채 9,463.6 → **10,664.1 $M**, EBITDA 4,143.5 $M → 비율 **2.28 → 2.57**. 게이트는 옳게 걸렸지만
  "부채를 이제 제대로 세니 탈락"이므로, 사용자 관점에서는 **규칙 변경에 따른 편출**임을 화면·문서에 남긴다.
- **HON V 4.03 → 0** — `effective_fcff_years = 4`(2021·2020 `OperatingIncomeLoss` 미태깅)로 05 가드가 5년 DCF 를 폐기.
  남은 방법(EPV·Graham)만으로는 IV 가 크게 낮아져 안전마진이 −201% 가 됐다. EV/EBIT 표본도 22개/2년뿐이라
  백분위가 보류(`insufficient_history`)다. **데이터 커버리지 결함이지 규칙 결함은 아니지만**, 태그 폴백
  (`IncomeLossFromContinuingOperationsBeforeIncomeTaxes…` 등)을 넣기 전까지 HON·유사 종목은 과소평가된다.

---

## 4. 검증

| 항목 | 결과 |
|---|---|
| `pytest -m "not network"` | **446 passed**, 4 deselected (신규 49건: track A 26 · track B 23 → 통합 경계 3건 추가) |
| `ruff check engine scripts tests` | **All checks passed** (0건) |
| `scripts/validate_outputs.py` | `ok: true` — 신호 7 · 차단 0 · 레짐 risk_on · 원장 14 · 워치리스트 21 |
| `scripts/build_public_data.py` | 30개 파일 생성, 금지 키 0 · 200KB 초과 0 |
| `scripts/run_walkforward.py --plan-only` | `attempts` = 1m/3m **planned**, 6m **deferred**(적격 폴드 1/3) |
| `check_commit_scope` (CI 스텝) | 통과(로컬 동일 명령으로 확인) |

통합 경계 신규 테스트 3건: `test_sec_acceptance_helper_is_one_path_to_dates`,
`test_pipeline_record_carries_track_b_valuation_fields`, `test_public_meta_and_today_carry_new_run_fields`.

---

## 5. 배포 · 실사이트 검증

(이 절은 커밋·CI·`daily-postclose`·실사이트 확인 뒤 채운다 — 아래 배포 기록 참조)
