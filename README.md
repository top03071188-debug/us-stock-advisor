# us-stock-advisor — Phase 1 엔진

미국 주식 하이브리드 투자 조언 앱의 **Phase 1 기반**: 시점 고정(`available_at ≤ decision_at`),
3종 `PriceKind`, 주당 달러(`USD/share`) 내재가치, NYSE 거래일 폴드, 그리고 공개 범위 가드.

> 이 저장소는 **판단 보조** 도구이며 투자 권유가 아니다. 자동 주문·계좌 연동은 범위 밖이다.
> 기획서: `미국주식_투자조언앱_개발기획서.md` **v1.3.1** (supersedes v1.3).

## Phase 1 에서 실제로 도는 것

| 영역 | 모듈 | 확인 |
|---|---|---|
| NYSE 거래일·세 시각·폴드 | `engine/utils/dates.py` | 2026-11-19 기준 **1M 4 · 3M 3 · 6M 2(보류)** 재현 |
| 가격 3종·분할 역산 | `engine/data/prices.py` | `raw = sadj × split_factor`, `available_at` = 16:00 ET |
| ATR 비율(손절·사이징) | `engine/metrics/technical.py` | **`atr_pct = atr14_sadj / close_sadj`** (v1.3.1) |
| 내재가치 | `engine/metrics/valuation.py` | EV 65,836 → $60.64 / EPV $38.69 / Graham $37.35 / 중앙값 $38.69 |
| 재무비율·Q 점수 | `engine/metrics/fundamentals.py` | ROIC·WACC·FCF·희석·안정성 |
| 1층 스크리너 | `engine/screener.py` | 최소 개수 강제 없음, 은행·보험·REIT 보류 |
| 거시 빈티지 | `engine/data/macro.py` | **D = `realtime_start`**, D 종료(23:59:59 ET) 후 첫 결정 |
| SEC EDGAR | `engine/data/sec_edgar.py` | 초당 10요청, `acceptanceDateTime`, 원문 URL |
| 스냅샷·폴드·보정 | `engine/backtest/` | manifest + sha256 `snapshot_id`, 잘린 폴드 목적함수 제외 |
| 개장 경매 범위 | `engine/backtest/execution.py` | `stop_min < P_open ≤ limit_max` (v1.3.1) |
| 아카이브 | `scripts/archive_snapshot.py` | `gh release view --json assets` → sha256 대조 → 없을 때만 업로드 |
| 공개 범위 | `scripts/{build_public_data,redact_quotes,check_commit_scope}.py` | 금지 키·이메일·원시가 경로 0건 |
| 대시보드 초안 | `web/` | 의존성 없는 정적 HTML/JS, JSON 로더(워치리스트·폴드) |

## 설치·실행

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock

cp .env.example .env      # 실제 키는 .env 에만 (gitignore)
.venv/bin/python -m pytest -m "not network" -q        # 오프라인 단위 테스트
RUN_NETWORK=1 .venv/bin/python -m pytest tests/test_network_smoke.py -q   # 실제 API 1회
.venv/bin/python scripts/smoke_network.py              # 상세 리포트 → out/smoke_report.json
.venv/bin/python scripts/run_walkforward.py --today 2026-11-19 --plan-only
.venv/bin/python scripts/build_public_data.py --signals data/samples/quant_signals.example.json \
    --folds data/walkforward/folds/summary.json --out web/data
python3 -m http.server 8000 -d web                     # 대시보드 초안 확인
```

## 보안·공개 범위 원칙

- `.env`·`~/.codex/auth.json`·`local/holdings.json` 은 **절대 커밋하지 않는다**. CI 가 `check_commit_scope.py` 로 검사한다.
- 커밋·Pages 산출물에는 점수·등급·요약·지표·메타·`url`·`quote_sha256` 만. **기사 인용 원문·본문 발췌·원시 OHLC·보유 정보는 금지**.
- 스냅샷 원본은 **비공개 저장소** `PRIVATE_ARCHIVE_REPO` 에 append-only. 공개 Release 를 쓰지 않는다.
- `gh release upload --skip-existing` 은 **존재하지 않는 플래그**다(gh 2.102.0 확인). `gh release view --json assets` 로 먼저 조회하고 sha256 이 같으면 건너뛴다.

## GitHub 구성 (확정)

| 저장소 | 공개 범위 | 역할 |
|---|---|---|
| `top03071188-debug/us-stock-advisor` | **public** | 코드·Actions·Pages (가공된 요약만) |
| `top03071188-debug/us-stock-advisor-archive` | **private** | 원본 스냅샷 `snapshot_{T}_{sha8}.tgz` (append-only) |

필요한 설정(사용자 웹 작업): `Settings → Secrets and variables → Actions`

- Secret `FRED_API_KEY`, `FINNHUB_API_KEY`, `SEC_USER_AGENT`
- Secret `ARCHIVE_REPO_TOKEN` — **아카이브 저장소에만** `contents: write` 를 준 fine-grained PAT
- Variable `PRIVATE_ARCHIVE_REPO` = `top03071188-debug/us-stock-advisor-archive`
- Pages: Source = GitHub Actions

## Phase 1 밖(다음 단계)

`scripts/run_daily.py`(수집→신호), 실시간 Paper Ledger, 13F, Codex 토론, 알림은 Phase 2~4 다.
`daily-postclose.yml` 은 그 형태를 미리 담아 두었고 **schedule 은 꺼 두었다**(수동 실행만).
