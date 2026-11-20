# 감사 `5331ab7` 수정 기록 — T1~T5 통합 배선 + 전수 재검토(내일부터 실작동 · 데이터 누적)

- 작성: 2026-11-21 (통합 담당)
- 대상: `repos/us-stock-advisor` (기준 커밋 `5331ab7`, T1~T5 작업 트리 위)
- 배경 문서: `scratch/audit_verdict_5331ab7.md`(12건 판정), 기획서 v1.3.5
  (`downloads/미국주식_투자조언앱_개발기획서.md`)
- 검증: `ruff check .` **0건**, `pytest -m "not network"` **911 passed / 1 skipped / 4 deselected**,
  공개 빌드 35파일 가드 **0건**, 3일 연속 배치·검증·빌드 **exit 0/0/0**(3회),
  180리포트·43신호·300레코드 누적 빌드 **exit 0**(어떤 파일도 200KiB 미만)

이 문서는 **① T1~T5 를 하나로 잇는 배선**, **② 그 통합 상태에서 처음부터 다시 해 본 전수 재검토에서
새로 찾은 결함과 수정**, **③ 실측 수치**를 순서대로 남긴다.

---

## 1. T1~T5 잔여 배선 — 실행기(`scripts/run_daily.py`)

| # | 무엇이 끊겨 있었나 | 지금 | 근거 |
|---|---|---|---|
| A-1 | `blocked_map` 이 `{ticker: 사유}` 문자열이라 **다른 세션용 차단**이 같은 티커의 오늘 슬롯을 막고, **장중·장후 관측 차단**이 이미 동결된 시가 주문을 `no_action@시가` 로 뒤집었다(감사 03-C·03-D) | `_blocked_plans()` 가 `{ticker: {reason, signal_id, target_session, observed_at}}` 를 만들어 `live.settle(blocked=...)` 의 새 계약으로 넘긴다. `observed_at` = **그 배치의 발행 시각**(`signal_published_at`) | `live._block_decision` 의 `other_session`/`after_open` 판정 |
| A-2 | `_forced_exits` 가 `{ticker: "S3"}` 문자열이라 종가 판정이 **그 날 시가**에 체결됐다(감사 04) | `_forced_exits(checks, observed_at=발행시각)` 이 `{reason, observed_at}` 구조를 돌려주고 `settle_positions` 의 `_forced_exit_plan` 새 계약을 탄다. 인자를 주지 않는 옛 호출(테스트 포함)은 문자열 맵 그대로 | `test_phase4_ui_and_pipeline.py::test_forced_exits_...` 유지 |
| A-3 | 같은 실행의 S5 가 `settle` 에 전달되지 않아 **세션 로그 유무**로 결과가 갈렸다(감사 04-C) | `_stop_exit_plans()` 가 S5 만 골라 `settle(stop_exits=...)` 로 넘긴다(시가 이전 확정 → 그 세션 잠금 / 종가 관측 → 다음 세션 잠금) | `live._stop_exit_items`·`stop_locks_next` |
| A-4 | 아직 체결되지 않은 S5 예약(`exit_pending`)은 잠금으로 파생되지 않아 **손절이 실제로 팔리는 세션**의 재진입을 놓쳤다 | `live.stop_reentry_locks()` 가 `exit_pending`+`expected_fill_day` 도 잠금으로 읽는다(체결 세션 기준) | `tests/test_audit_5331ab7_track2.py::test_pending_s5_...` |
| A-5 | 매도 예약 세션을 `다음 거래일 하나`로만 셈 — 배치가 늦어 관측이 **다음 세션 시가보다도 늦으면**(05-13 15:00Z) 이미 지난 05-13 시가에 체결된 것으로 적혔다 | `live._fill_session_after(session, observed)` — **관측보다 시가가 늦는 첫 세션** | `test_audit_5331ab7_track2.py::test_late_postclose_...` |
| A-6 | `notify.evaluate_notifications` 에 `debate_events` 를 넘기지 않아, 실패한 "토론 실행 권장"을 **다음 실행이 그대로 재전송**했다(그 사이 편입된 토론 무시) | 원장 이벤트(`data/ledger/debate_events/*.json` 의 **이벤트 행**)를 넘겨 재판정한다. `payload["notifications"]` 에 `n_retried`·`n_retry_suppressed`·`retry_pending` 도 실린다 | 실측: 재판정 없으면 webhook 2회 호출, 있으면 1회·`n_retry_suppressed=1` |

### A-6 실측 (실제 `data/notifications_log.json` 모양 그대로)

```text
1회차  n_new=1  n_retried=0  retry_pending=[{webhook, attempts:1}]
2회차(웹훅 계속 실패)  n_retried=1  webhook 호출 2회  retry_pending=[{webhook, attempts:2}]
2회차(그 사이 편입 이벤트 존재)  n_retry_suppressed=1  webhook 호출 1회  retry_pending=[]
```

---

## 2. 전수 재검토에서 **새로 찾은 결함**과 수정

### 2.1 ★ 다음 배치의 커밋 가드가 실패할 상태였다 (커밋·배포 중단)

- T5 가 `scripts/notify.py` 에서 `n_retry_suppressed`·`retry_pending_inactive` 를 쓰기 시작했는데
  `scripts/check_commit_scope.py` 의 `notify_latest` 계약과 `config/public_allowlist.yaml` 에 없었다.
  실측: 그 두 필드를 넣은 문서 → `notify_latest: 계약에 없는 필드 ['n_retry_suppressed',
  'retry_pending_inactive']`.
- `daily-postclose.yml` 은 `data/notifications_latest.json` 을 `git add` 한 뒤
  `check_commit_scope.py --staged` 를 돌리므로, **다음 07:00 배치가 그 자리에서 실패**했을 것이다
  (데이터 커밋·Pages 배포 모두 중단).
- 수정: 계약 허용 키(`notify_latest`.`allowed`·`CONTRACT_SAFE_KEYS`)에 두 필드와 `sent`(채널 장부의
  상태 이름)를 추가하고, `build_notifications()` 가 재시도 요약(`n_retried`·`n_retry_suppressed`·
  `retry_pending` 20행)을 공개 요약에 싣도록 했다. 허용 목록에도 `n_retry_suppressed`·
  `retry_pending_inactive`·`sent`·`pending_retry`·`unconfigured`·`retried` 를 넣었다.

### 2.2 ★ `signals/today.json` 이 신호가 많은 날 빌드를 통째로 죽인다

- 실측(실제 신호 한 건의 모양을 복제): **7건 65,997B / 20건 184,743B / 43건 396,389B**.
  `build()` 는 파일 하나가 200KiB 를 넘으면 `SystemExit` 이므로, **신호가 20건을 넘는 날에는
  모든 공개 파일이 기록되지 않고** Pages 배포가 멈춘다(감사 09 와 같은 뿌리, 다른 파일).
- 수정: `_fit_today_size()` — 상한(`TODAY_MAX = 190KiB`)을 넘으면 **점수 낮은 행부터 요약 행**으로
  내린다(요약 행: 식별·등급·점수·판단·한 줄 근거 + `detail_path: data/tickers/{T}.json` +
  `compact: true`). `counts` 는 **전체**를 그대로 말하고, 몇 건을 요약으로 내렸는지 `notes` 에 적는다.
  화면은 `compact` 카드에 "요약 — 종목 상세에서 전체 내용" 배지를 붙인다(`web/app.js`).
- 실측(수정 후): 7건 65,122B(요약 0) / 20건 184,743B(요약 0) / 43건 **192,255B(요약 24)**.

### 2.3 ★ `ledger/records.json` 이 원장 200건에서 200KiB 를 넘는다

- 실측(실제 레코드 28건을 200건으로 늘려 공개 행으로 변환): 행당 **1,073B** → 200건 **214,705B**.
  행 수 상한(200)만 있어 `build()` 가 `SystemExit` — 190건 부근에서 사이트 전체가 멈춘다.
- 수정: `RECORDS_MAX = 170KiB` 바이트 예산으로 **오래된 행부터** 빼고 `omitted`·`note_ko` 로 밝힌다
  (화면은 최근 50건만 그린다). `build_records()` 결과(허용 목록 필터 전) 실측: 200건·300건 입력 모두
  **173,428B / 161건**(`omitted 39`), 필터·직렬화 뒤 실제 게시 파일은 **150,866B**.

### 2.4 누적 최악 케이스 종단 실측 (수정 후)

| 입력 | 파일 수 | 최대 파일 | 200KiB 초과 |
|---|---|---|---|
| 180 리포트(실제 GOOGL 크기) · 43 신호 · 300 원장 · 300 알림 | 117 (상세 40) | `signals/today.json` **181,200B** | **0건** |
| 같은 입력에서 `ledger/records.json` | — | **150,866B** | 0 |
| 같은 입력에서 `debate.json` | — | **69,062B** | 0 |
| 같은 입력에서 `deploy_manifest.json` | 117파일 해시 | **17,481B** | 0 |
| 같은 입력에서 `debate_details/{report_id}.json` | 40 | 7,671B(최대) | 0 |

→ `debate.json`(요약 40행 + 종목당 1행)·`notifications.json`(최근 20건)·`ledger/portfolio.json`·
`ledger/summary.json`(signal_history 200행 37KB)은 상한 안에서 상수로 유지된다. **빌드가 죽는 경로는
남기지 않았다**(상한을 넘기면 요약/제외 + 사유 문구, 최후에는 조용한 절단이 아니라 명시적 실패).

- 상세 파일(`debate_details/*.json`)도 같은 원칙으로 바꿨다: 리포트 하나의 라운드 원문이 200KB 를
  넘으면 `SystemExit` 로 빌드를 죽이던 경로를, **그 리포트만 원문을 빼고 요약 상세**로 내리고
  (`note_ko` 에 사유) 목록 `note_ko` 에 건수를 적는 쪽으로 바꿨다. 요약으로도 넘치면 그때만
  상세 파일을 만들지 않고 `detail_path` 를 지운다(없는 파일을 가리키지 않게).

### 2.5 토론 검증 워크플로의 삭제·빈 파일 edge case

- T4 의 `debate-validate.yml` 은 PR 의 리포트 blob 이 **없으면** "삭제·미변경"으로 넘겼다 —
  `data/debate_reports.json` 을 지운 PR 이 모든 검사를 통과하고, 파일이 비어 있으면 계약 검증의
  JSON 오류로만 드러났다.
- 수정(등록본·템플릿 동일): ① 기본 브랜치에 있던 리포트를 **삭제한 PR 은 실패**, ② 0바이트/공백뿐인
  리포트 blob 도 **실패**(사유를 명시). `fetch-depth: 0`·`base_ref`·`pull/<n>/head` fetch·
  `requirements.lock` 경로(`/tmp/trusted`)는 그대로 검증했다.
- 준비본(`scratch/workflows_upload/debate-validate.yml`)도 같은 바이트로 갱신했다 — **GitHub 웹 편집기로
  다시 등록해야 실제 CI 에 적용된다**(CLI 토큰에 `workflows` 권한이 없다).

### 2.6 셸 버전

- `web/app.js` 가 바뀌어 `SW_VERSION`/`ASSET_VERSION` 을 `phase4-5` 로 올리고 `index.html` 의 `?v=` 도
  함께 올렸다(서로 어긋나면 옛 app.js 가 셸 캐시에서 계속 돈다).
- 기존 테스트 3건이 `"phase4-3"`·`"phase4-4"` **고정 문자열**을 단언하고 있어, 값이 아니라 **계약**
  (index.html 의 `?v=` == `ASSET_VERSION`, `phase4-` 접두)을 검사하도록 바꿨다
  (`tests/test_phase4_ui_and_pipeline.py` 2곳, `tests/test_audit_5331ab7_track4.py` 1곳).

---

## 3. 재검토 결과 — 문제 없음으로 확인한 항목

| 관점 | 확인 방법 | 결과 |
|---|---|---|
| 현재 실데이터 ↔ 닫힌 스키마 | `validate_reports_file('data/debate_reports.json')` | **[] (오류 0)** |
| 현재 실데이터 ↔ 커밋 가드 | `check_paths(quant_signals·records·health·notifications_latest·notifications_log·position_events·debate_reports)` | **[] (0건)** |
| `assemble_cloud` 산출물(provenance) | `context_sha256`+재서명 후 스키마·가드·공개 빌드 | **모두 통과**, `debate.json` 11,712B |
| 공개 빌드 산출물 ↔ 가드·매니페스트 | 35파일 `check_paths`, 매니페스트 sha256 대조 | **0건 / 불일치 0**, 상세 1파일 포함 |
| 3일 연속 배치(11-21 → 11-22 → 11-24, 오프라인 픽스처) | `run_daily --mode postclose` → `validate_outputs` → `build_public_data` ×3 | **exit 0 ×9** · 원장 28→32→36→40 · 1차 체결 1건(투자 1.98%) · 알림 4건 발행 · 공개 18파일 최대 45,286B |
| 워크플로 YAML 5종 + 템플릿 | `yaml.safe_load` + 트리거/단계 확인, 등록본·템플릿 바이트 동일 | **파싱 정상**, `debate-validate` 11단계 |
| `web/app.js`·`web/sw.js` 문법 | node 미설치 → **quickjs(ES2020)** 로 파싱 | **둘 다 정상** |
| `detail_path` 서브패스 호환 | `./data/` 상대 경로 + `debateDetailName` 의 `data/` 제거 | `/us-stock-advisor/` 하위 배포에서 그대로 동작 |
| `sw.js` 상세 캐시 | `/data/` 전체를 `handleData` → 매니페스트 sha256 대조 후 세트 캐시 | 오프라인에서도 상세 열람 유지 |

---

## 4. 남은 판단(사용자·기획서)

1. **기획서 v1.3.6 동기화** — 코드가 기획서와 어긋나는 지점을 아래로 정리했다(문서는 아직 v1.3.5).
   - §2.2.2: 동결 마감 뒤 첫 도착은 `held`(다음 세션) — 코드·테스트가 그렇게 동작한다(`NO_PRIOR_ORDER`).
   - §3.5: 매도 의도의 **관측 시각**과 **실행 세션** 분리(종가 판정 → 다음 세션 시가).
   - §4.8.1: A/B 모집단은 정량 `buy_filled` 가 아니라 **공통 동결 주문 의도**(섹터 캡에 막힌 후보 포함).
   - §5.6: `debate.json` 은 요약, 라운드 원문은 `data/debate_details/{report_id}.json` 분리 + 파일별 200KiB.
   - §5.6(신규): `signals/today.json`·`ledger/records.json` 의 **행 요약·바이트 예산** 규칙.
2. **`data/debate_reports.json` 의 실제 리포트 1건**은 원장 이벤트가 아직 없어 화면 배지가 비어 있다
   (`LEDGER_EVENT_MISSING`) — 다음 배치가 편입 판정을 남기면 채워진다(정상 상태).
3. `scratch/workflows_upload/` 의 워크플로 4종은 **GitHub 웹 편집기 등록**이 필요하다(토큰 권한).
4. `git commit/push` 는 하지 않았다 — 통합 확인 뒤 메인에서 진행한다.

---

## 5. 손댄 파일

- 실행기·엔진: `scripts/run_daily.py`, `engine/ledger/live.py`
- 공개 계약: `scripts/build_public_data.py`, `scripts/check_commit_scope.py`, `config/public_allowlist.yaml`
- 웹: `web/app.js`, `web/index.html`, `web/sw.js`
- 워크플로(등록본·템플릿 동일): `.github/workflows/debate-validate.yml`, `codex_debate/workflows/debate-validate.yml`
- 테스트: `tests/test_audit_5331ab7_final_review.py`(신규), `tests/test_audit_5331ab7_track2.py`(+3),
  `tests/test_audit_5331ab7_track3.py`(+1 pr-only 신호파일 없이), `tests/test_audit_5331ab7_track4.py`(버전 계약 1곳),
  `tests/test_phase4_ui_and_pipeline.py`(버전 계약 2곳)
- 문서: 이 파일
