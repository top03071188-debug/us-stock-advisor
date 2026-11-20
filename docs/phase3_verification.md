# Phase 3 검증 기록 (2026-11-20)

기획서 **v1.3.1** §7 Phase 3 — *실시간 Paper Ledger 운영 + Actions 일괄 배포 호환 + 대시보드 v1 +
13F 레이더 + 홀드아웃/파일럿/공개범위 가드* 구현·검증 결과.

실행 환경: Python 3.12.16, `.venv`, pandas 3.1.1 · numpy 2.5.5 · PyYAML 6.0.3 · pytest 9.2.0 · ruff 0.6.9.
모든 테스트는 **오프라인·결정론**이다(네트워크 마커 4건은 기본 제외).
`.github/workflows/*` 는 **한 글자도 수정하지 않았다**(토큰에 `workflows` 권한이 없다).

---

## 1. 구현 목록

| 영역 | 파일 | 핵심 규칙 |
|---|---|---|
| 일일 파이프라인 | `scripts/run_daily.py` | `--mode postclose\|premarket`, `--offline`(픽스처)·라이브, `GITHUB_OUTPUT` 에 `snapshot_id`·`snapshot_id_short`, 소스 실패는 `degraded` 로 남기고 **가격 0건이면 전일 데이터 유지** |
| 신호 조립 | `engine/decide/pipeline.py` | B0~B6 → 등급 → 40/30/30 계획, Veto 차단 목록 분리, 신뢰도 `A 데이터·근거 충족` 라벨, 주 1회 정규화 차트 |
| 라이브 수집 | `engine/data/collect.py` | yfinance·SEC·FRED·Finnhub, 키 없음/실패를 `missing_key`·`failed`·`degraded` 로 기록, 뉴스 48시간 규칙 |
| 오프라인 데이터 | `engine/data/fixtures.py` | 고정 시드 합성 6종목(눌림·횡보·하락·상승), 항상 `status: sample` |
| 13F 레이더 | `engine/data/holdings_13f.py` | 정보표 XML 파싱, `acceptance_datetime ≤ decision_at`, **분기말 +45일** 워터마크, 신규·확대·축소·청산 델타 |
| 홀드아웃 게이트 | `engine/backtest/holdout.py` | 분기 1회 개봉, `maturity < holdout_start` 경계 검증, `data/walkforward/holdout/{date}.json` append-only 잠금 |
| 스트레스·5년 확장 | `engine/backtest/stress.py` | 2022/2020/2018Q4 3구간, `universe_history`·`delisted` 없으면 **미검증**(`pass: null`) — 게이트 통과로 계산하지 않음 |
| 파일럿 4지표 | `engine/ledger/pilot.py` | `N_eff`(63거래일 클러스터)·비용 후 순수익·유사 현금비중 벤치·하위 20% 손실. 하나라도 계산 불가 → **보류** |
| 실시간 Ledger | `engine/ledger/live.py` | T+1 시가 + 10bp 체결, 발행 레코드 **무수정**, 체결 이벤트 분리 append, 모의 계좌 **비중만** 공개 |
| 주간 스크린 | `scripts/run_screen.py` | 1층 하드게이트 → 워치리스트 편입/편출 **사유 포함**, 13F 갱신 |
| 출력 검증 | `scripts/validate_outputs.py` | 인자 없이 실행, 계약 7종 검사, 실패 시 exit 1 |
| 공개 빌더 | `scripts/build_public_data.py` | 인자 생략 시 기본값(`data/quant_signals.json`·`data/walkforward/folds/summary.json`·`web/data`), §5.7 파일 9종 + `tickers/{T}.json`, 파일당 200KB 제한 |
| 초기 계약 데이터 | `scripts/init_contract_data.py` | 첫 배치 전 `status: awaiting_first_run` — **가짜 실전 신호를 만들지 않는다** |
| 대시보드 v1 | `web/index.html` · `web/app.js` · `web/styles.css` | 4탭(오늘·종목·성적표·레이더), 모바일 하단 탭/데스크톱 좌측 사이드바, 44px 터치, `tabular-nums`, 다크 모드, 색+텍스트 병기 |
| 표본 데이터 | `web/sample-data/**` | 화면 구성 확인용 합성 산출물. 화면 상단에 "샘플 화면" 배지를 띄운다 |
| 설정 | `config/universe.yaml`, `config/public_allowlist.yaml` | 부트스트랩 유니버스(대형주 부분집합), Phase 3 공개 필드 추가 |

### 데이터 계약 파일 (커밋 대상)

```text
data/quant_signals.json            # 발행 레코드(의도만) + charts + ledger_summary + portfolio(비중)
data/ledger/records.jsonl          # 발행 기록(불변, append-only)
data/ledger/executions/{id}.json   # 체결 이벤트(시가 확정 뒤 append)
data/ledger/portfolio/state.json   # 모의 계좌 — 비중만(cash_pct·invested_pct·positions[].weight)
data/ledger/pilot_eval.json        # 파일럿 4지표 + 판정
data/params_history.json           # 파라미터 버전 이력(append-only)
data/radar/13f.json                # 13F 변화 + 45일 워터마크
data/watchlist/current.json        # 워치리스트 + 편입/편출 사유
data/walkforward/folds/summary.json, data/walkforward/stress.json
```

---

## 2. 워크플로 계약 일치 확인

워크플로는 수정할 수 없으므로 **이미 올라간 YAML 이 부르는 것을 그대로 맞췄다.**

| 워크플로 호출 | 구현 | 확인 |
|---|---|---|
| `python scripts/run_daily.py --mode postclose` | `--mode` 선택, 인자 없이도 동작 | `GITHUB_OUTPUT` 에 `snapshot_id=sha256:…`, `snapshot_id_short=12자` 기록(테스트 `test_postclose_writes_contract_and_github_output`) |
| (그 실행의 outputs) `snapshot_id`, `snapshot_id_short` | `make_snapshot` manifest 해시 | 12자 short 가 `snapshot_$(date -u +%F)_${SNAPSHOT_SHORT}` 이름 규칙에 그대로 들어간다 |
| `python scripts/validate_outputs.py` | 인자 파서 없이 실행 | `{"ok": true, …}` exit 0 / 위반 시 exit 1 |
| `archive_snapshot.py package --out dist_archive --name "snapshot_…"` | 기본 `--source data/snapshots` | `run_daily` 가 `data/snapshots/{as_of}/manifest.json` 을 만든다 |
| `archive_snapshot.py check-asset --assets … --file "$TGZ"` | 0=일치, 10=없음, 1=불일치 | 세 코드 모두 테스트로 고정 |
| `redact_quotes.py --mode sha256 data/quant_signals.json data/ledger/records.jsonl` | `--mode sha256` | 파이프라인도 자체적으로 redact 한다(두 번 돌려도 멱등) |
| `git add … data/ledger/portfolio/ …` | `state.json`(비중만) | `check_commit_scope.py --staged` 통과 |
| `python scripts/build_public_data.py` (인자 없음) | 기본값 3종 | CI·deploy 두 호출 형태 모두 테스트 |
| `deploy.yml`: `--signals … --folds … --out web/data` | 명시 인자 | 동일 |

> `ci.yml` 의 `ruff check … || true` 는 그대로 두었지만, 현재 `ruff check engine scripts tests` 는
> **0건(All checks passed)** 이다.

---

## 3. 테스트 실측

```text
$ .venv/bin/python -m pytest -m "not network"
255 passed, 4 deselected in 7.93s

$ .venv/bin/ruff check engine scripts tests
All checks passed!
```

Phase 1·2의 205개는 그대로 통과하고 **50개를 추가**했다(모두 신규 파일).

| 파일 | 개수 | 검사 |
|---|---|---|
| `tests/test_public_scope.py` | 7 | 금지 키 0건·이메일 0건, 파일당 200KB, §5.7 파일 존재, 차트가 **정규화 지수(기준일=100)** 인지, status 정직성 |
| `tests/test_commit_scope.py` | 12 | `quote`·`body_excerpt_1500` 차단→해시 후 통과, `raw_close`·`shares`·`avg_cost` 차단, `.env`·`auth.json`·`data/raw` 경로 차단, 이메일 패턴, 예시 플레이스홀더 예외 |
| `tests/test_run_daily_offline.py` | 11 | postclose/premarket, `GITHUB_OUTPUT`, 발행 레코드에 체결가 없음, `shares` 부재, 차단 사유, T+1 체결 분리, 모의 계좌 비중, `validate_outputs` 통과/검출, `check-asset` 0/1/10, 패키징 결정론 |
| `tests/test_radar_holdout_pilot.py` | 20 | 13F XML 파싱·45일 게이트·델타 4종·워치리스트 겹침, 홀드아웃 분기 1회·경계 규칙·잠금, 스트레스 미검증, `N_eff` 클러스터, 파일럿 4지표 판정 |

---

## 4. 재현 실행 (오프라인 픽스처 — 실데이터 아님)

```text
$ .venv/bin/python scripts/run_screen.py --offline --as-of 2026-11-20
{"as_of": "2026-11-20", "count": 5, "entered": ["FIXA","FIXB","FIXC","FIXE","FIXF"],
 "exited": [], "valuation_suspended": ["FIXD"], "excluded": 0}

$ .venv/bin/python scripts/run_daily.py --mode postclose --offline --as-of 2026-11-20
{"as_of": "2026-11-20", "mode": "postclose", "status": "sample",
 "snapshot_id": "sha256:05190565500643a9…", "snapshot_id_short": "051905655006",
 "signals": ["2026-11-20-FIXA-BUY-001","2026-11-20-FIXB-WATCH-001",
             "2026-11-20-FIXE-WATCH-001","2026-11-20-FIXF-WATCH-001"],
 "blocked": ["FIXC"], "settled": [], "pending": 0, "records_appended": 4,
 "portfolio": {"cash_pct": 1.0, "invested_pct": 0.0, "n_positions": 0},
 "pilot": "보류", "degraded": []}

$ .venv/bin/python scripts/run_daily.py --mode postclose --offline --as-of 2026-11-24
{"settled": [{"signal_id": "2026-11-20-FIXA-BUY-001", "event": "buy_filled", "fill_price": 175.162}],
 "portfolio": {"cash_pct": 0.980152, "invested_pct": 0.019848, "n_positions": 1}, …}

$ .venv/bin/python scripts/validate_outputs.py
{"ok": true, "notes": {"signals": {"n_signals": 4, "n_blocked": 2, …}, "records": {"n": 8},
 "pilot": {"verdict": "보류", "n_eff": 8}, "params_history": ["params_v001"], …}}

$ .venv/bin/python scripts/build_public_data.py           # 인자 없음 = daily-postclose 계약
written: meta.json, signals/today.json, ledger/summary.json, ledger/records.json,
         ledger/portfolio.json, radar/13f.json, config.json, folds.json, watchlist.json,
         tickers/{FIXA,FIXB,FIXC,FIXE,FIXF}.json
```

- **체결은 하루 늦게 기록된다**: 07:00 KST 실행은 그날 개장(22:30 KST) 전이므로 **전일 시가**까지만
  알 수 있다. 그래서 금요일 신호의 체결 이벤트는 **월요일 개장가**로, 다음 실행(화요일 07:00)에서 append 된다.
  기록되는 `at` 은 실제 개장 시각이므로 성적표는 정확하고, 지연은 "언제 알았는가"의 정직한 표시다.
- 신호 0건인 날은 `signals: []` + `ledger_summary.accuracy: null` 로 정상 발행된다(`test_zero_...` 계열).

---

## 5. 워크플로 초안 (파일로 만들지 않음 — 복사용)

토큰에 `workflows` 권한이 없어 `.github/workflows/*` 를 추가할 수 없다. 아래는 **그대로 복사해
GitHub 웹에서 추가**하면 되는 초안이다(스크립트·인자는 이미 구현·검증됨).

### `premarket.yml` (매일 21:50 KST = 12:50 UTC)

```yaml
name: premarket
on:
  schedule: [{cron: "50 12 * * *"}]
  workflow_dispatch:
concurrency: {group: premarket, cancel-in-progress: false}
permissions: {contents: write, pages: write, id-token: write}
jobs:
  report:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12", cache: pip}
      - run: pip install -r requirements.lock
      - env:
          FRED_API_KEY: ${{ secrets.FRED_API_KEY }}
          SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}
          FINNHUB_API_KEY: ${{ secrets.FINNHUB_API_KEY }}
        run: python scripts/run_daily.py --mode premarket
      - run: python scripts/validate_outputs.py
      - run: python scripts/redact_quotes.py --mode sha256 data/quant_signals.json data/ledger/records.jsonl
      - run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/quant_signals.json data/ledger/records.jsonl data/ledger/portfolio/ \
                  data/ledger/executions/ data/ledger/pilot_eval.json data/params_history.json
          python scripts/check_commit_scope.py --staged
          git diff --cached --quiet || git commit -m "data: premarket $(date -u +%F)"
          git push
      - run: python scripts/build_public_data.py
      - uses: actions/upload-pages-artifact@v3
        with: {path: web}
  deploy:
    needs: report
    runs-on: ubuntu-latest
    environment: github-pages
    steps:
      - uses: actions/deploy-pages@v4
```

### `weekly-screen.yml` (토 08:00 KST = 23:00 UTC 금)

```yaml
name: weekly-screen
on:
  schedule: [{cron: "0 23 * * 5"}]
  workflow_dispatch:
permissions: {contents: write, pages: write, id-token: true}
jobs:
  screen:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12", cache: pip}
      - run: pip install -r requirements.lock
      - env:
          SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}
          FRED_API_KEY: ${{ secrets.FRED_API_KEY }}
        run: python scripts/run_screen.py
      - run: python scripts/build_public_data.py
      - run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/watchlist/ data/radar/13f.json
          python scripts/check_commit_scope.py --staged
          git diff --cached --quiet || git commit -m "data: watchlist $(date -u +%F)"
          git push
      - uses: actions/upload-pages-artifact@v3
        with: {path: web}
  deploy:
    needs: screen
    runs-on: ubuntu-latest
    environment: github-pages
    steps:
      - uses: actions/deploy-pages@v4
```

### `quarterly-walkforward.yml` (분기 1일 00:00 UTC) · `semiannual-extended.yml` (1·7월 15일)

분기 워크포워드는 **보정 후보를 PR 로만** 올린다(4.3 가드2). 실행 골격:

```yaml
      - run: python scripts/run_walkforward.py --plan-only
      - run: python -c "import json,datetime as d; from engine.backtest.holdout import HoldoutLedger; print(json.dumps(HoldoutLedger().can_open(d.date.today()), ensure_ascii=False))"
      # 개봉일이고 이번 분기 미평가일 때만: 폴드 재생 → HoldoutLedger().record(...)
      - run: gh pr create --title "chore(params): 분기 보정 후보" --body-file reports/calibration.md
```

반기 확장은 `engine.backtest.stress` 결과를 `data/walkforward/stress.json` 에 쓰고 **`gate_passed` 가
`false`(미검증)이면 채택하지 않는다.**

> `daily-postclose.yml` 의 `schedule:` 은 Phase 1에 주석 처리된 상태다. 이제 `run_daily.py` 가
> 있으므로 그 두 줄의 주석을 풀면 매일 07:00 KST 무인 실행이 시작된다. **워크플로 파일 수정 권한이
> 있는 사람이 해야 한다**(이 작업에서는 손대지 않았다).

---

## 6. 고정 규칙이 코드에서 지켜지는 지점

| 규칙 | 코드 |
|---|---|
| 세 시각 `data_cutoff ≤ published < order_cutoff` | `engine/utils/dates.py` + `run_daily.three_times`(위반 시 즉시 중단), `record.check_three_times` |
| 발행 레코드에 체결가·손절가 없음 | `record.publish_record` / `assert_publish_record`, `validate_outputs` |
| 체결은 T+1 시가 + 10bp, 1차는 커밋 범위 안 | `live.settle` → `execution.opening_fill(record["entry_band"], raw_open)` |
| 손절·청산 뒤 재진입 0건 | `PortfolioSimulator`(Phase 2) 그대로 사용 |
| 홀드아웃 `maturity < holdout_start` | `holdout.is_eligible_fold` / `validate_boundary`, `grade.grade_record` |
| 13F 45일 시차 | `Filing13F.available_at` / `watermark` |
| 스트레스 미확보 = 미검증 | `stress.evaluate_window`(`verified: False`, `pass: None`) |
| 파일럿 4지표 동시 판정 | `pilot.evaluate_pilot`(계산 불가 1개라도 있으면 보류) |
| 공개 금지 필드 | `build_public_data.assert_no_forbidden` + `check_commit_scope` |
| 신호 0건 정상 | `run_daily` 는 빈 배열로 발행, 화면은 "오늘 신호 없음" + 차단 사유 |

---

## 7. 보안·공개 범위

- `.env`·실 API 키·개인 이메일(`top03071188@…`)을 새 파일·테스트 픽스처·커밋·웹 산출물에 넣지 않았다.
  `config/public_allowlist.yaml` 의 이메일 패턴이 **산출물 빌드 시점**에 걸리고,
  `check_commit_scope.py --staged` 가 커밋 직전에 한 번 더 걸른다.
- 커밋본·Pages 산출물에서 **0건으로 확인된 필드**: `raw_open/high/low/close`, `sadj_*`, `adj_close`,
  `ohlc`, `volume_series`, `shares`, `avg_cost`, `cash_balance`, `total_asset`, `holdings`, `quote`,
  `body_excerpt_1500` (`test_public_scope`, `test_commit_scope`).
- 차트는 **주 1회 정규화 지수(기준일=100)** + 50/200일선만 내보낸다 — 원시 시세 시계열이 아니다.
- 모의 계좌 상태는 **비중(%)** 만 커밋한다(아래 8-① 참고).
- 공개 저장소 Release 자산 0건: 원본 스냅샷은 `PRIVATE_ARCHIVE_REPO`(비공개)로만 올린다(워크플로 계약 그대로).

---

## 8. 판단이 필요했던 지점 (기록)

**① 공개 금지 키와 "ledger/portfolio 커밋"이 충돌한다.**
기획서 2.2.3은 `data/ledger/portfolio/**` 를 커밋 대상으로 두지만, 같은 문서의 금지 키 목록에는
`shares`·`avg_cost`·`cash_balance` 가 들어 있고 "하나라도 있으면 커밋 실패 [고정]"이다. 그래서
**커밋되는 모의 계좌 상태는 비중만** 남기고(`cash_pct`·`invested_pct`·`positions[].weight`·`equity_index`),
수량·평단은 **커밋 파일에 쓰지 않는다**(런타임 메모리에서 체결 이벤트로 재생). 체결 이벤트도
`quant_track.shares` 를 커밋본에서 제외하고, 실수량이 필요한 원본은 gitignore 경로
`data/ledger/executions/*.full.jsonl` 에만 남긴다. 키 이름을 바꿔 가드를 우회하지 않았다.

**② `quality_score` 의 배점 버그를 고쳤다.**
`_scale()` 이 이미 0~100 을 돌려주는데 `30 * _scale(...)` 처럼 곱해 Q 점수가 5,800점대로 나왔다
(Phase 1·2 테스트는 Q 값을 고정하지 않아 드러나지 않았다). `_scale(...)/100` 으로 바로잡아
Q 가 0~100 이 되었고, 예시 재무의 Q 는 **92.71** 이다. 이 수정 없이는 `entry_score`·`rank_score` 가
의미를 갖지 못해 Phase 3 신호 판단 자체가 성립하지 않는다.

**③ 체결 기록이 하루 늦다.** 07:00 KST 실행은 개장 전이라 **전일 시가**까지만 안다. 그래서 금요일
신호의 체결은 월요일 개봉가로 **화요일 실행에서** append 된다. `at` 은 실제 개장 시각이므로 채점은
정확하고, 지연은 "언제 알았는가"의 정직한 표시다(4.7.1의 "시가 확정 뒤 append" 규칙 그대로).

**④ 허용 목록을 넓혔다.** 화면이 읽는 공개 필드(`decision`·`indicators`·`chart`·`pilot`·`watermark_ko` 등)를
`config/public_allowlist.yaml` 의 `allow` 에만 추가했다. **`deny_keys`·`deny_paths`·`deny_patterns` 는
한 항목도 바꾸지 않았다**(테스트가 금지 키 0건을 다시 확인).

**⑤ 라이브 유니버스는 부트스트랩 부분집합이다.** 지수 구성종목 시계열(`universe_history`)이 없어
`config/universe.yaml` 의 대형주 43종목으로 돌린다. 생존편향이 있으므로 `data/pipeline_limits.json` 과
화면에 "미검증·상한 추정"으로 표기한다.

**⑥ 초기 계약 데이터는 "비어 있음"을 상태로 남겼다.** `deploy.yml` 이 첫 푸시에서도 사이트를
빌드할 수 있도록 `status: awaiting_first_run` + 빈 신호를 커밋했다. 화면은 "아직 실전 배치가 돌지
않았습니다" 배너와 **샘플 화면 보기** 버튼을 제공하고, 샘플 데이터는 `web/sample-data/` 에 분리해
실데이터와 섞이지 않게 했다.

---

## 9. 미검증 한계 (정직하게)

- **실데이터 파이프라인은 2026-11-20 첫 배치에서 두 가지 데이터 계층 결함이 드러나 2차 수정을 거쳤다**
  (§12). 파일럿·스트레스·5년 확장은 여전히 미검증이다.
- **`daily-postclose.yml` 의 `schedule` 은 여전히 주석 상태**다. 무인 운영(DoD "2주 연속 무인 실행")은
  워크플로 권한이 있는 사람이 주석을 풀고 `cron: "0 22 * * *"` 을 활성화한 뒤에야 측정된다.
- **생존편향·하락장 스트레스·5년 20폴드는 미검증**이다. `universe_history`·`delisted_securities` 가
  없어 `stress.stress_report()` 는 `stress_pass: null`, `gate_passed: false` 로 남는다(게이트 통과 아님).
- **13F 는 라이브 수집 경로가 없다.** 파서·델타·워터마크는 구현·테스트했지만 SEC 13F 원문을 받아오는
  수집기는 Phase 4 과제다. 그래서 `data/radar/13f.json` 은 현재 빈 정상 상태다.
- **실적·공시 캘린더**는 8-K 2.02 이력 추정 규칙만 있고 화면에 연결하지 않았다(레이더 탭에 그 사실을
  그대로 적었다).
- **토론(Phase 4) 미구현**: `debate_effect` 는 "표본 0건 — 판정 보류", 종목 상세 ⑦은 빈 상태 카드다.
- **모의 계좌의 2·3차 분할 체결·익절·트레일링은 일일 실행에 연결되지 않았다.** 1차 시가 체결만 라이브로
  기록되고, 손절·익절 규칙은 Phase 2 시뮬레이터(`PortfolioSimulator`)에 있다 — 라이브 Ledger 는
  발행·체결·채점에 집중했고, 청산 자동화는 다음 단계다.
- **벤치마크(SPY) 없으면 초과수익을 표시하지 않는다.** 픽스처에는 SPY 가 없어 `benchmark_connected: false`,
  파일럿 `net_after_cost` 는 "계산 불가 → 보류"가 정상이다.

---

## 10. 배포 검증 (GitHub Actions · Pages 실사이트)

`main` 브랜치 커밋 이력(작성자 `김영서` (GitHub noreply 주소로 커밋)):

| 커밋 | 내용 |
|---|---|
| `96d85e9` | Phase 3 본 구현(엔진 6종·스크립트 4종·대시보드 v1·테스트 50개) |
| `70fa1cb` | 현재 규칙을 허용 목록 친화 행(`groups`)으로 내보내 값이 비지 않게 |
| `317cc0c` | 정적 자산 캐시 버전(`?v=`) + `meta.source` 없음 표기 정리 |
| `4e9225c` | 폴드 표(1m/3m/6m)·파일럿 4지표·파라미터 버전이 필터에 지워지던 문제 |
| `58ea47d` | N_eff 정수 표기·작은 비율 소수 표기·관찰 카드 분할가 숨김 |
| `37a8127` | 차트 높이 고정 + 공시 표 접수일 머리글 정렬 |
| `feabf32` | 성적표 하위 블록을 카드 중첩 대신 구획으로 |
| `8614714` | 문단 기본 여백이 행 높이를 부풀리던 문제 |
| `1449e5b` | 첫 배치 전에도 폴드·홀드아웃·파일럿·파라미터 이력이 채워지도록 폴백 |
| `aff8466` | 배포·실사이트 검증 기록 + 폴드 개수 표기(`4개 (기준 3)`) |

| 실행 | run ID | 결론 |
|---|---|---|
| `ci` (`96d85e9`) | [44654033579](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44654033579) | success |
| `deploy` (`96d85e9`) | [44654033607](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44654033607) | success |
| `ci` (`1449e5b`) | [44655932990](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44655932990) | success |
| `deploy` (`1449e5b`) | [44655934824](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44655934824) | success |
| `ci` (`aff8466`) | [44656218015](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44656218015) | success |
| `deploy` (`aff8466`) | [44656217985](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44656217985) | success |

실사이트 `https://top03071188-debug.github.io/us-stock-advisor/` 확인(브라우저 실측):

- HTML·`app.js`·`styles.css`·`data/*.json`·`sample-data/*.json` 모두 **200**.
- 4탭 전부 렌더: 오늘(신선도·레짐 배너, 신호 카드 4장, 차단 1건, 현재 규칙),
  종목(FIXA 7블록 — 대가 막대·IV 밴드·SVG 차트·뉴스·공시 표·invalidation·토론 빈 카드),
  성적표(폴드 1M 4개·3M 3개·6M 2개 **보정 보류**, 홀드아웃 잠금 "다음 개봉 2027-01-01",
  정확도/전략 분리, 파일럿 4지표 ✓/✗/—, 파라미터 이력, 토론 효과 "표본 0건 — 판정 보류"),
  레이더(45일 워터마크·13F 빈 정상·워치리스트 편입 5건 사유·현재 규칙 8표 + Veto 12종).
- 콘솔 예외 없음(렌더 실패 시 상단 배지가 "데이터 오류"로 바뀌는 경로가 동작하지 않음).
- 데스크톱(≥900px) 좌측 사이드바 + 중앙 정렬, 모바일은 하단 고정 탭.
- 스크린샷: `browser/today-first-run-2026-11-20-103938.png`(첫 배치 대기),
  `browser/today-sample-2026-11-20-103956.png`(샘플 오늘),
  `browser/ticker-detail-2026-11-20-104007.png`·`browser/ticker-detail-2-2026-11-20-104017.png`(종목 7블록),
  `browser/ledger-2026-11-20-104154.png`·`browser/ledger-real-initial-2026-11-20-104909.png`(성적표).

> 배포는 `deploy.yml`(사람 푸시 트리거)이 수행했고 **데이터 생성 워크플로는 아직 돌지 않았다**.
> `daily-postclose` 는 `workflow_dispatch` 로 수동 실행할 수 있다:
> `gh workflow run daily-postclose -R top03071188-debug/us-stock-advisor`
> (Secret 미등록 시 가격 수집이 실패해 `degraded` 로 끝나고 전일 데이터가 유지된다.)

---

## 11. 남은 후속 작업

1. `daily-postclose.yml` 의 `schedule` 주석 해제(워크플로 권한 필요) → 2주 무인 실행 관찰.
2. `premarket.yml`·`weekly-screen.yml`·`quarterly-walkforward.yml`·`semiannual-extended.yml` 추가(§5 초안).
3. Secret/Variable 등록: `FRED_API_KEY`, `SEC_USER_AGENT`, `FINNHUB_API_KEY`,
   `PRIVATE_ARCHIVE_REPO`(vars), `ARCHIVE_REPO_TOKEN`(secrets).
4. `universe_history`·`delisted_securities` 를 `data/corpus/` 에 확보 → 스트레스·5년 확장 실행.
5. 13F 수집기(SEC 원문) + 실적 캘린더 연결.
6. Phase 4: `codex_debate/` 3라운드 토론, `debate_reports.json` 계약, 자동 하향 스위치.

---

## 12. 실데이터 2차 수정 (FRED 빈티지 탈락 · 라이브 수집기 확장)

첫 실데이터 배치(`daily-postclose` run 44658667329)는 **워크플로는 성공**했지만 데이터 계층 결함 두 가지로
`status: degraded`, `watchlist_count: 0`, 신호 0건이었다. 원인을 고치고 재검증했다.

### 12.1 결함 1 — FRED 11시리즈 전부 탈락 (`engine/data/macro.py`)

- **원인**: `/series/observations` 를 빈티지 파라미터 없이 부르면 응답의 **모든 행** `realtime_start` 가
  **요청 당일**(예: `2026-11-19`)로 내려온다. v1.3.1 은 이 값을 발표일로 썼기 때문에 모든 관측치의
  `available_at` 이 `2026-11-20 12:50Z` 가 되어 11-20 postclose 마감(`2026-11-19T22:00Z`)보다 뒤로 밀렸고,
  `as_of_macro()` 가 전부 걸러 **"유효 시리즈 없음" → `fred: degraded` → 레짐 미검증**이 됐다.
- **수정 (v1.3.2)**: 응답의 모든 행 `realtime_start` 가 같고 `release_at`·`first_ingested_at` 이 없으면
  스냅샷 응답으로 보고 **관측일 기준 보수 규칙**을 쓴다.
  - 일간·주간(`DFF`·`DGS10`·`DGS2`·`T10Y2Y`·`BAMLH0A0HYM2`·`VIXCLS`·`DTWEXBGS`·`WALCL`):
    관측일 `date` 의 `23:59:59 ET` 이후 첫 의사결정 → `available_at_source: obs_date_plus_one`
  - 월간(`CPIAUCSL`·`CPILFESL`·`UNRATE`): **관측월 다음 달 20일** `23:59:59 ET` 이후 첫 의사결정
    → `obs_month_lag_20d` (예: 2026-10-01 관측 → 2026-11-21 12:50Z, 11-20 마감 뒤)
  - 행마다 `realtime_start` 가 다르거나 발표시각 필드가 오면 **v1.3.1 규칙 그대로**(ALFRED 빈티지·테스트).
  - 한 행짜리 응답은 스냅샷인지 빈티지 한 줄인지 구분할 수 없어 더 보수적인 빈티지 규칙을 쓴다.
- **추가 수정**: `collect_macro` 가 3개월 전 **CPI YoY** 도 함께 계산한다. 이전에는 최신 CPI YoY 만 있어
  실질금리 추세(`real_rate`)가 항상 결측이었고, 레짐이 4신호로만 판정됐다.
- **실측(2026-11-20 postclose 마감 2026-11-19T22:00Z)**: 11시리즈 전부 값이 잡히고
  `DFF 3.88 · T10Y2Y 0.27 · HY OAS 347bp · VIX 24.56 · DTWEXBGS 122.65 · CPI YoY 4.02%`,
  레짐 `neutral`(t10y2y·credit risk_on, vol·dollar neutral) **`verified: true`**.

### 12.2 결함 2 — 라이브 수집기 5년 이력·시총·재무 누락 (`engine/data/collect.py`, `sec_edgar.py`, 신규 `xbrl_fundamentals.py`)

| 항목 | 이전 | 지금 |
|---|---|---|
| 가격 이력 | `history_years=3`(≈750행) → `HG-04`(1,260행) 전 종목 탈락 | **6년(≈1,506행)** — 5년 미만 상장은 있는 만큼 받아 정상 탈락 |
| 시총·거래소 | `market_cap_usd: None`, `exchange: "NYSE"` 하드코딩 | yfinance `fast_info`(시총·거래소 코드) → `NYQ/NMS/PCX` 매핑, 실패는 결측 + 사유 |
| 재무 | `fundamentals: {}` | SEC XBRL companyfacts → **최근 5 회계연도** `AnnualFundamentals` |
| 밸류에이션 | `iv_mult: None` | DCF·EPV·Graham 중앙값(USD/share) ÷ 현재가 = `iv_mult` |
| PEG·성장 | 없음 | `revenue_cagr`(4년 CAGR)·`peg`(P/E ÷ 성장률 %p) |
| XBRL 완전성 | 하드코딩 1.0 | 필수 9지표 × 5년 채움 비율 → `HG-11` 입력 |

- **look-ahead 규칙**: `form ∈ {10-K, 10-K/A, 20-F, 40-F}` · `fp = FY` · **`filed ≤ decision_at.date()`** 만 사용.
  같은 회계연도의 정정·재작성은 **가장 먼저 공개된 값**을 쓴다(그때 알 수 있었던 값).
- **태그 보정**: 영업이익 태그가 없는 기업은 세전이익 + 이자비용으로 EBIT 근사, 총부채 태그가 없으면
  `자산 − 자본` 항등식, 이자비용·감가상각 결측은 최근 보고값으로 대체, 주식수 백만/천 주 태깅 보정(MCD).
- **캐시·rate limit**: companyfacts(수 MB)를 `data/cache/sec_facts/CIK{10d}.json` 에 24시간 캐시,
  `RateLimiter`(10 req/s) 유지. 단일 종목 실패는 예외 전파 없이 `diagnostics.xbrl_detail` 에 남는다.
- **가격 캐시 없음/빈 봉**: Yahoo 가 마지막 봉을 NaN 으로 주는 경우가 있어 빈 봉은 이력에서 제거한다.

### 12.3 검증 (로컬 실키, 2026-11-20)

- `pytest -m "not network"`: **281 passed** (이전 255 + 신규 26: `test_xbrl_fundamentals.py` 13,
  `test_collect_live.py` 8, `test_macro_vintage.py` 5). `ruff check engine scripts tests`: **All checks passed!**
- `python scripts/run_screen.py --as-of 2026-11-20` (실데이터):

| 항목 | 값 |
|---|---|
| 워치리스트 | **19종목** — ADBE 69.4, GOOGL 53.1, MSFT 50.1, CSCO 48.8, PG 47.7, MRK 46.9, AAPL 42.5, META 40.4, JNJ 40.3, HON 40.0, KO 39.9, COST 39.1, CAT 38.2, LIN 36.4, SHW 33.9, CVX 33.5, UPS 30.0, DIS 29.3, HD 19.2 |
| 가치평가 보류 | **6종목** `JPM·BAC·V·MA·AMT·PLD` (`SECTOR_FORMULA_PENDING`) |
| 탈락 18종목 | `HG-09`(3년 희석>5%) NVDA·AVGO·WMT / `HG-05·08·09` NFLX·AMZN·BA / `HG-05·07·08·09` ORCL·DUK / `HG-06` MCD·PEP / `HG-07` UNH / `HG-06·07` ABBV / `HG-11`(XBRL 완전성<90%) NKE·GE·XOM / `HG-05·11` LLY / `HG-05·06·11` NEE / `V_UNAVAILABLE`(FCFF≤0) TSLA |
| 소스 상태 | `yfinance ok · fred ok · sec ok · finnhub ok · yfinance_meta ok · sec_xbrl ok` (42/43 연차 재무) — `degraded: []` |
| XBRL 완전성 | 42종목 평균 **0.978** (5년 전부 채움 32종목, 0.889 8종목: NKE·LLY·GE·JPM·BAC·V·NEE·PLD) |

- **남은 데이터 한계(정직하게)**: ① `XOM` 은 2026년 재편으로 티커가 새 CIK(2115436)에 매핑돼 10-K 연차
  이력이 없어 `HG-11` 로 탈락한다(이전 CIK 34088 의 이력은 쓰지 않았다). ② `NEE` 는 XBRL 에 capex 태그가
  없어 `HG-05`·`HG-11` 로 탈락한다. ③ 시총은 yfinance 현재값이라 **라이브 일일 실행 전용**이며 과거 재생에는
  쓰지 않는다. ④ 대부분 종목의 `v=0` 은 결측이 아니라 **모델 IV 가 현재가보다 낮다는 판정**이다(안전마진
  음수) — 규칙을 완화하지 않았고, 그래서 이번 배치의 BUY 신호는 자연히 0건이다.

### 12.4 재실행 결과 (`daily-postclose` run [44661625253](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44661625253))

- 커밋 `47dbda6` 푸시 → `ci` [44661477715](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44661477715) **success**(Lint·281 테스트·scope 가드·공개 빌드 가드 전 스텝 success).
- `gh workflow run daily-postclose.yml` → 전 스텝 success(파이프라인·스키마 검증·아카이브·redact·계약 커밋·공개 빌드·Pages 배포).
  파이프라인 출력: `status: normal`, `snapshot_id: sha256:e6fa8470…`, 신호 2건, `blocked: []`, `degraded: []`.

| 항목 | 값 |
|---|---|
| `status` | **`normal`** (이전 `degraded`) |
| 레짐 | `neutral` · **`verified: true`** · 투표 `real_rate neutral / t10y2y risk_on / credit risk_on / volatility neutral / dollar neutral` · `inputs_missing: []` |
| 소스 4+2 | `yfinance ok · fred ok · sec ok · finnhub ok · yfinance_meta ok · sec_xbrl ok` |
| `watchlist_count` | **19** (이전 0) |
| 신호 / 차단 | **2 / 0** — `ADBE` WATCH 66.41(IV $204.71 vs 현재가 $221.68, 안전마진 −8.3%), `GOOGL` WATCH 56.02(IV $81.81 vs $272.66, −233%) |
| 모의 계좌 | `cash_pct 1.0` · 보유 0 (BUY 신호 0건 → 주문 없음, 정상) |
| 파일럿 | `보류` — `N_eff 2` (<15) · 나머지 3지표 계산 불가(표본 부족) |
| 아카이브 | 비공개 저장소 `top03071188-debug/us-stock-advisor-archive` Release `snapshots-2026` 에 `snapshot_2026-11-20_e6fa84704bc4.tgz` 업로드(공개 저장소 Release **0건**) |

실사이트 `https://top03071188-debug.github.io/us-stock-advisor/` 브라우저 실측(2026-11-20 11:36 KST):

- `오늘`: `데이터 정상` · `발행 11. 20. 07:02 · 마감 11. 20. 07:00` · `데이터 출처: live` · `시장 레짐: 중립(Neutral) 포지션 배수 0.70 · 검증됨`,
  신호 카드 2장(ADBE·GOOGL 관찰), `차단된 종목 없음`, 현재 규칙 요약 정상.
- `종목`: 종목 선택 목록에 **실제 워치리스트 19종목**(ADBE 선택 → 7블록 전부 렌더 — 대가별 점수, IV 밴드 $174.01~$235.42,
  주 1회 정규화 차트, Finnhub 뉴스 5건, SEC 공시 표 5행, invalidation, 토론 빈 카드). 콘솔 예외 없음.
- 스크린샷: `downloads/phase3-screenshots/live-second-run-today.png`(오늘),
  `downloads/phase3-screenshots/live-second-run.png`(종목·ADBE).

## 13. R01~R11 감사 수정 (2026-11-20)

감사 문서(`investment_app_implementation_audit_47dbda6`)의 11개 항목을 두 트랙으로 나눠 수정·검증했다.
정본 문서는 **`docs/audit_fix_47dbda6.md`** 이고, 이 절은 요점만 남긴다.

- 판정: 11건 **전부 실제 결함**. 감사 서술과 다른 지점 3건은 정정해 구현했다 — R02 `D`는 “SPY 0% 대체”가 아니라 **초과수익 정의 미구현**, R05 **AVGO는 정당 탈락**(NVDA·WMT는 오탈락, NFLX는 HG-08로 별도 탈락), R09는 라이브가 아니라 **백테스트·재생의 상·하한 동시 혼용**.
- 테스트: `pytest -m "not network"` **373 passed, 4 deselected**, `ruff` 0건, `check_commit_scope`·`validate_outputs` 통과.
- 재스크린(2026-11-20): 워치리스트 **19 → 22**(NVDA·WMT 복귀, TSLA 신규, AVGO 탈락 유지).
- 기대 변화(회귀 아님): GOOGL IV $81.81 → **$60.79**(5년 정상화·순현금·가격회귀 β, 밴드 $51.67~$69.91), G 39.36 → 91.97(TTM PEG), **전 종목 V=0 → BUY 신호 0건**, 기존 폴드 성적은 R09로 비교 불가(재실행 필요).
- 실데이터 재실행(`daily-postclose` 44671878797·44672331459): `status normal` · `signal_published_at 2026-11-20T04:08:24Z`(계획 22:02:11Z) · `target_session 2026-11-20` · `ingest.lag_seconds 22131`(약 369분) · 관찰 7건 · 파일럿 `n_eff 0`.
- 미포함: `v_penalties`/`v_score` 규칙 변경, 폴드 재실행·`--calibrate` 실제 evaluate 배선, 라이브 Ledger의 2·3차·손절 연결(Phase 4).

## 14. v1.3.2 — `v_score` 완만 감쇠 · 감점 2종 · 10년 EV/EBIT 백분위 (2026-11-20)

기획서 **v1.3.2**(§3.2.3)에 맞춰 밸류에이션 고도화 2건을 구현했다. 상세 결함 서술은 `docs/audit_fix_47dbda6.md` §8.

### 14.1 무엇이 바뀌었나

| 항목 | v1.3.1 | v1.3.2 |
|---|---|---|
| `v_score` 하락 구간 | −20% 이하 전부 0점 | **−20% = 15점, −200% = 0점** 완만 선형(양수 구간 불변) |
| DCF 민감도 | 함수만 존재(미연결) | `abs(bull−bear)/(2·base) ≥ 0.40` → **−4점 + 신뢰도 C**(`DCF_HIGH_SENSITIVITY`) |
| 10년 EV/EBIT | 문장만 존재 | **백분위(0~100)** · 중앙값 기반 `iv_hist_ev_ebit` · **20% 괴리 −10점** · **S1 ≥ 90** |
| 데이터 창 | 가격 6년 · XBRL 6년 | 가격 **10년** · XBRL **10년**(`years=10`) |
| 감점 배관 | `Candidate.v_penalties`만 존재(항상 0) | `collect → screener → run_screen → pipeline → 기록 → 화면` 전 구간 |

### 14.2 룩어헤드 차단 방식

- `annual_revisions(companyfacts)`가 **모든 연차 개정 이력**을 `available_at`과 함께 남긴다(단일 "최신값"이 아니다).
- 10년 중 21거래일 간격 스냅샷 `t`마다 **그 봉의 마감 시각까지 접수된** 재무 중 최신 회계연도만 쓴다.
  접수일이 봉 마감(21:00Z) 뒤 23:59Z면 그날 스냅샷에는 **아직 안 쓴다**(테스트로 고정).
- 주식수 누락 시 `순이익 ÷ 희석 EPS` 폴백(최근 주식수의 0.3~3배 가드), EBIT 누락 시 `세전이익 + 이자비용` 근사.
- 유효 회계연도 3년 미만이면 `None`(감점 0) — 6~9년차 종목은 계산되고, 5년 미만 가격 이력만 HG-04로 탈락한다.

### 14.3 검증·실측

- `pytest -m "not network"` **390 passed**(신규 16건: `tests/test_valuation_penalties_and_10y.py`), `ruff` 0건,
  `check_commit_scope`·`validate_outputs`·`build_public_data` 통과.
- 워치리스트 22종목 유지(편입·편출 0), V 변별력 회복: ADBE 0 → **3.36**, DIS 0 → **3.76**, UPS 0 → **4.06**,
  HON 0 → **4.00**(감점 0), CVX 0 → **0.90**, GOOGL 0 유지(감점 −10, 백분위 58.7).
- 10년 EV/EBIT 백분위 실측: WMT 96.7 · KO 90.1 · CSCO 89.3 · AAPL 84.3 · COST 82.6 · TSLA 74.3 · CVX 68.4 …
  **S1 과열(밸류) ≥ 90** 경로가 이제 실제로 발동한다(이전엔 단위 불일치로 절대 발동하지 않았다).
- 감점 −10은 22종목 중 21종목에 적용됐다 — 모델 IV가 10년 시장 배수보다 구조적으로 낮아 괴리가 20%를 넘는다
  (ADBE 76%, GOOGL 56%). 규칙 그대로의 결과이며 임계는 파라미터로 조정 가능하다.

### 14.4 실배치 (2026-11-20 13:38 KST)

- `ci` [44675649182](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44675649182) **success** → `daily-postclose`
  [44675727239](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44675727239) **success**(report 1m41s + deploy 13s),
  Actions 커밋 `194e0f4` → `git pull --ff-only origin main` 동기화.
- `signal_published_at 2026-11-20T04:38:32Z`(13:38 KST) · `status normal` · 관찰 7 · 차단 0 · 워치리스트 22(편입·편출 0).
- GOOGL: V 0.0 · 감점 −10 · 백분위 **58.7**(중앙 24.0배) · `iv_hist_ev_ebit` $171.78 vs IV $60.79(괴리 65%) · `dcf_sensitivity` 0.0.
  ADBE: V **3.36** · 감점 −10 · 백분위 **1.65**(중앙 43.2배) · 괴리 76% · 민감도 0.11.
- `v_penalty_dcf`(−4) 0건(최대 NVDA 0.28 < 0.40). S1 백분위 90 이상: KO 90.1 · WMT 96.7.
- 실사이트 스크린샷: `downloads/phase3-screenshots/v132-today-2026-11-20.png`,
  `downloads/phase3-screenshots/v132-ticker-GOOGL-2026-11-20.png`.
