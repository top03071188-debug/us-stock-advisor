# Phase 1 검증 기록 (2026-11-19)

기획서 **v1.3.1** §7 Phase 1 범위의 실제 실행 결과. 실행 환경: Python 3.12.16, `.venv`,
yfinance 1.8.0 · pandas 3.1.1 · numpy 2.5.5 · pydantic 2.14.3 · pyarrow 26.0.1 · pytest 9.2.0.

## 1. 오프라인 테스트 (`pytest`)

```text
$ .venv/bin/python -m pytest
90 passed, 4 skipped in 0.64s      # 4 skipped = 실제 API 스모크(기본 비활성)
```

| 파일 | 검사 |
|---|---|
| `test_valuation_unit.py` | EV 65,836 → $60.64 / EPV $38.69 / Graham $37.35 / 중앙값 $38.69 / 안전마진 19.9%, 단위(USD/share) 강제 |
| `test_sector_hold.py` | 은행·보험·REIT `valuation_suspended` + `SECTOR_FORMULA_PENDING`, 워치리스트·신호 제외 |
| `test_technical_price_kind.py` | `atr_pct = atr14_sadj/close_sadj`(2:1 분할 전 $100 → 손절 6%, 3% 아님), `raw_as_traded` 입력 시 `TypeError`, 개장 경매 범위 |
| `test_split_reconstruction.py` | `raw = sadj × split_factor`, 직접 원시가 대조, `ADJUSTMENT_MISMATCH`·`SPLIT_ANOMALY`, `available_at`=16:00 ET |
| `test_macro_vintage.py` | **D = `realtime_start`**, D 23:59:59 ET 이후 첫 결정, 개정 빈티지 차단, `BAMLH0A0HYM2` 범위 기록, 4신호 폴백 |
| `test_no_holdout_leak.py` | `maturity_date < holdout_start`, 잘린 폴드 목적함수 제외, 6M 보류 `attempts.json`, 보정 모듈의 홀드아웃 미참조 |
| `test_lookahead.py` | LA-1 ~ LA-14 + 개장 경매 순서(LA-4 확장) |
| `test_archive_and_scope.py` | `check-asset` 0/10/1, `--clobber`·`--skip-existing` 미사용, 결정론적 tgz, redact, 공개 데이터 필터, 커밋 범위 가드 |
| `test_screener.py` | 하드게이트 12종, 0건·9건 정상, 섹터 캡 6, 40개 상한, 정렬식 |
| `test_dates_folds.py` | 2026 휴장일, EDT/EST, 세 시각, **1M 4 · 3M 3 · 6M 2**, 15개월 창에서 6M 3 |

폴드 실측(2026-11-19 기준, 홀드아웃 2026-08-19):

| 기간 | 적격 폴드 | 만기 |
|---|---|---|
| 1개월(21td) | F1~F4 | 2025-09-18 · 2025-12-19 · 2026-03-20 · 2026-06-18 |
| 3개월(63td) | F1~F3 | 2025-11-17 · 2026-02-23 · 2026-05-20 (F4는 2026-08-19 = 홀드아웃 첫 봉) |
| 6개월(126td) | F1~F2 | 2026-02-19 · 2026-05-22 (F3는 2026-08-20, 하루 초과 → **보정 보류**) |

## 2. 실제 API 스모크 (1회, `scripts/smoke_network.py`)

키는 로컬 `.env`(gitignore)로만 주입했고, 결과 리포트는 `out/smoke_report.json`(gitignore)에 남겼다.
아래는 값·범위만 옮긴 것이며 인증 정보는 없다.

| 소스 | 결과 |
|---|---|
| yfinance `AAPL` | 2,512행(2016-11-21 ~ 2026-11-18), 분할 2020-08-31 4.0 포함, 분할 전 `raw/sadj` = 4.0, 재구성 오차 0.0, ATR% 2.18%, RSI 56.6 |
| yfinance `MSFT` | 2,512행, 최근 10년 분할 없음(마지막 2003), ATR% 2.94%, RSI 42.9 |
| yfinance `SPY` | 2,512행, 분할 없음, ATR% 1.55%, RSI 36.6 |
| SEC EDGAR | `company_tickers.json` 10,398개, `AAPL` CIK 320193, 최근 공시 접수시각 수집(8-K 2026-11-09T21:30:40Z) |
| FRED `DFF`/`CPIAUCSL`/`T10Y2Y`/`VIXCLS`/`DTWEXBGS` | 2018-01-01 ~ 2026-11-17/18, 최근값 3.88 / 337.476 / 0.27 / 25.46 / 122.6503 |
| FRED `BAMLH0A0HYM2` | **2023-11-20 ~ 2026-11-17 (793행)** — 기획서 2.4.3의 확인값과 **일치** → `data/limits.json` 기록 |
| Finnhub | `MSFT` quote 508.29 (전일 506.24), 최근 7일 뉴스 250건 수집 |

네트워크 스모크 pytest: `RUN_NETWORK=1 pytest tests/test_network_smoke.py` → **4 passed**.

## 3. 고정 규칙이 코드에서 지켜지는 지점

| 규칙 | 코드 |
|---|---|
| `atr_pct = atr14_sadj / close_sadj` 후 체결가에 적용 | `engine/metrics/technical.py::atr_pct`, `stop_price` |
| 1차 체결은 장 전 주문 범위만 | `engine/backtest/execution.py::build_entry_band`, `opening_fill` |
| D = `realtime_start`, D 종료 후 첫 결정 | `engine/data/macro.py::resolve_available_at` |
| 아카이브는 `gh release view --json assets` → sha256 → 없을 때만 업로드 | `scripts/archive_snapshot.py` |
| 발행 레코드 불변, 토론은 `debate_events` 로 분리 | `config/params_v001.yaml::ledger`, `engine/backtest/walk_forward.py` |

## 4. 아직 남은 것

- GitHub 저장소 생성·푸시: 이 환경의 `gh` 는 기존 저장소 읽기/쓰기는 되지만
  **저장소 생성 권한이 없다**(`createRepository` 403). 사용자가 웹에서 만든 뒤 푸시한다.
- Actions Secret `FRED_API_KEY`·`FINNHUB_API_KEY`·`SEC_USER_AGENT`, Secret `ARCHIVE_REPO_TOKEN`,
  Variable `PRIVATE_ARCHIVE_REPO`, Pages 소스 설정.
- `scripts/run_daily.py`(수집→신호)와 실시간 Paper Ledger 는 Phase 2~3.
