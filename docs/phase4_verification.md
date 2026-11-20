# Phase 4 통합 검증 기록 (2026-11-20)

기획서 **v1.3.4** §7 Phase 4 — *Codex 6스킬 토론 · 토론 편입 · A/B 채점 · 실시간 2·3차 분할/익절 ·
알림 · PWA 대시보드* + **감사 판정서(`scratch/audit_verdict_phase4_5381f9c.md`)의 P0-1~P2-1 수정**
(A-1/A-2/A-7 포함)를 하나의 워킹트리로 합치고, 남아 있던 배선 3줄을 연결한 뒤 전체 검증·커밋한 기록.

실행 환경: Python 3.12.16, `.venv`, pandas 3.1.1 · numpy 2.5.5 · PyYAML 6.0.3 · pytest 9.2.0 · ruff 0.6.9.
모든 테스트는 **오프라인·결정론**이다(네트워크 마커 4건은 기본 제외).
`.github/workflows/*` 는 **한 글자도 수정·추가·스테이징하지 않았다**(CLI 토큰이 `workflows` 권한을 거부한다).

---

## 1. 통합 범위

| 트랙 | 커밋/작업 | 내용 |
|---|---|---|
| 트랙 1 | `codex_debate/` (신규) | 6스킬(버핏·린치·드러켄밀러·게이트13F·리스크가드·의장) 3라운드 토론, 프롬프트 4종, 스키마 5종, 인용·숫자·가격 검증, 뉴스 3상태 판독, 정족수·예산·마감 오케스트레이터 |
| 트랙 2 | `engine/ledger/debate.py` 외 | 토론 편입 4조건, A/B 페어·두 계좌 정책 비교, 실시간 2·3차 분할·손절·분할익절, 알림(중복 0건·재시도), PWA 대시보드 |
| 감사 수정 A | `record.py`·`live.py`·`sizing.py`·`portfolio.py`·`grade.py`·`pilot.py`·`calibration.py` | P0-1(발행 회차·주문 슬롯), P0-2(시각 계약), P0-3(섹터 캡·분할 익절가·A-1 2차 수량·A-2 DMA 다음 봉·A-7 3차 무장), P0-4(Paper/보정 분리), P1-3(비용 이중 차감·A/B 두 계좌) |
| 감사 수정 B | `pipeline.py`·`notify.py`·`build_public_data.py`·`web/*` | P1-1(토론 리스크 분리·신뢰도 C 상한), P1-2(경로 가드), P2-1(장애 상태부·생성 시각·PWA 캐시·알림 재시도) |

### 감사 수정 전 → 후 (같은 재현 스크립트·테스트)

| ID | 수정 전(감사 재현) | 수정 후 |
|---|---|---|
| P0-1(a) BUY→WATCH | 최신 판단이 WATCH 인데 **BUY 1건 체결** | **0건** + `cancel_pending`(`cancelled_by=WATCH_OR_BLOCKED`)·`superseded` 기록 |
| P0-1(b) A→B→A | 이미 대체된 **A-001 반환** | **`…-003`** 발급, `supersedes=B-002`, 대체본 재사용 0건 |
| P0-2 | 마감 45분 뒤 검증해도 `eligible=True, late=False` | `REPORT_VALIDATED_AFTER_ORDER_CUTOFF`·`VALIDATED_AFTER_FREEZE`·`ATTACHED_AFTER_FILL` → 편입 0건 |
| P0-3(a) 섹터 | Tech **30.00%** 체결 | Tech **25.00%**(5/6 체결, 6번째 `SECTOR_CAP`) — portfolio·live 두 엔진 |
| P0-3(b) 분할 익절가 | 2:1 분할 뒤 `iv_high` **$120 그대로** | **$60**(`trim_basis_qty` 동시 조정) |
| A-1 2차 수량 | 계획 1.5% 대신 **2.0%** | **1.5%**(`plan_weight × tranche weight`), 포지션 예산 내 |
| A-2 3차 DMA | 조건 봉 **당일 시가** 체결(룩어헤드) | **다음 거래일 시가**(`dma_next_open`) |
| A-7 3차 무효 | 기본 파라미터에서 3차가 **사실상 항상 무효** | 지정가 트랙만 무효, **200일선 트랙으로 무장**(`trigger="dma200"`) |
| P0-4 | 만기 봉 관측됐는데 `truncated=True` | `holdout_start=2026-08-20` 을 줘도 **`complete=True, truncated=False`**(Paper) |
| P1-3(a) 비용 | 손익 −0.0999% → **−0.4399%**(왕복 34bp 재차감) | **−0.3399%**, 거래 0건이면 비용 0원 |
| P1-3(b) A/B | `codex_debated` 계좌 없음, `mdd_debate=0.0` 하드코딩 | 두 계좌 **실운용**(`policy_diff_pp`·`quant_mdd`·`debate_mdd`·`pair_summary`) |
| P2-1 | 장애 실행이 `health.json` 을 쓰지 않는다 / 서비스워커 없음 | `data_status`·`last_attempt_at`·`last_success_at`·`outage_id` 기록, `web/sw.js` 발행 세트 캐시 |

---

## 2. 남아 있던 배선 3줄 — 연결 완료

트랙 A가 엔진 API만 만들고 `scripts/run_daily.py`(트랙 B 소유) 연결을 남긴 3건이다.

### ① 2·3차·손절·익절 세션 이벤트 영속 — `ledger_view()` → `settle_positions(position_events_path=…)`

```python
def ledger_view(records, executions, panels, as_of, reanchors=None, params=None,
                forced_exits=None,
                position_events_path: str | Path | None = live.POSITION_EVENTS_PATH):
    ...
    settlement = live.settle_positions(..., max_sector=cap,
                                       position_events_path=position_events_path)
```

- 기본값이 `live.POSITION_EVENTS_PATH`(상대 경로)라 **실행 루트의 `data/ledger/position_events.jsonl`** 에 남는다.
  `run()` 이 `--root` 로 `chdir` 하므로 테스트(`tmp_path`)에서는 임시 디렉터리에만 쓰여 **저장소 파일을 오염시키지 않는다**
  (`test_ledger_view_persists_position_events_under_its_root` — 두 번 실행해도 파일이 바이트 그대로 = `event_id` 멱등).
- append-only 충돌(같은 `event_id` 다른 내용)은 `ValueError` → stderr 한 줄 남기고 **로그 없이 정산·발행은 계속**한다
  (이벤트 로그 실패가 그날 배포를 막지 않는다).

### ② 1차 섹터 캡이 **기존 보유까지** 합산 — `live.settle(sector_exposure=…)`

```python
held_pre = ledger_view(records, executions, panels, as_of, reanchors, params,
                       position_events_path=None)[0]        # 정산 전 계좌를 '읽기만' 한다
settlement = live.settle(..., max_sector=sector_cap(params),
                         sector_exposure=sector_exposure_dollars(held_pre))
```

- `sector_exposure_dollars()` 는 공개 비중(`sector_weights`)을 그 시점 총자산(`initial_cash × equity_index`)으로
  되돌려 **달러**로 넘긴다(`settle` 의 `max_sector × equity_base` 와 같은 단위).
- 효과: 기존 보유 Tech 25% + 신규 Tech 1차 2% → **27% > 25%** 로 체결하지 않는다. 배선 전에는 기존 보유를 0으로 보고 체결했다
  (`test_existing_holdings_count_toward_first_fill_sector_cap` — 같은 입력으로 두 동작을 나란히 고정).
- 2·3차 체결도 params `risk.max_sector` 를 쓴다(하드코딩 아님) — `test_ledger_view_passes_sector_cap_from_params`
  (`risk.max_sector` 를 0.18 로 바꾼 params 로 실행하면 그 값이 그대로 전달된다).

### ③ 동결 전 WATCH·차단 취소 이벤트 기록 — `position_events.jsonl` + `health.json`

```python
order_cancellations = settlement.get("cancellations") or []
if order_cancellations:
    live.append_position_events(live.position_event_rows([], order_cancellations, as_of),
                                live.POSITION_EVENTS_PATH)
...
live.write_health(..., extra={"order_cancellations_count": len(order_cancellations),
                             "order_cancellations": order_cancellations[:20], ...})
```

- `build_public_data.build_health` 가 `order_cancellations_count`·`order_cancellations`(최대 20건)를 `web/data/health.json`
  에 내보내고, `config/public_allowlist.yaml` 에 그 4개 키(`order_cancellations`·`order_cancellations_count`·
  `superseded_by`·`frozen_at`)를 **allow 에만** 추가했다(deny 는 그대로).
- `ledger_summary.settlement.order_cancellations`·콘솔 출력에도 건수를 남긴다.
- 검증: `test_order_cancellations_are_logged_to_position_events_and_health`(픽스처 배치에 BUY→WATCH 회차를 심고 실행 →
  `cancel_pending` 행 + `health.json` 건수 확인, 재실행해도 중복 0건).

---

## 3. 통합 검증 (전부 필수 · 실측)

```text
$ .venv/bin/python -m pytest -m "not network"
625 passed, 4 deselected in 33.36s

$ .venv/bin/python -m ruff check .
All checks passed!

$ .venv/bin/python codex_debate/run_debate.py --dry-run --top 3
{"as_of":"2026-11-20","targets":[GOOGL,ADBE,NVDA],"estimated_calls":30,
 "deadline_at":"2026-11-20T14:20:00+00:00","order_cutoff_at":"2026-11-20T14:25:00Z"}   # 호출 0건(dry-run)

$ .venv/bin/python scripts/build_public_data.py
{"written":[... 34 files ...],"signals_status":"normal"}
  · 파일 34개 · 합계 426.5KB · **최대 파일 `ledger/summary.json` 130.5KB < 200KB**(파일당 제한, 5.6)
  · 금지 키·패턴 **0건**(`check_commit_scope.check_text` 로 산출물 34개 전수 검사)

$ .venv/bin/python scripts/validate_outputs.py
{"ok": true, "notes": {"signals": {"n_signals": 7, "status": "normal"}, "records": {"n": 14},
 "pilot": {"verdict": "보류", "n_eff": 0}, "portfolio": {"cash_pct": 1.0, "n_positions": 0}, ...}}

$ .venv/bin/python scripts/check_commit_scope.py --staged
scope ok  (아래 §5)
```

테스트 구성 — 기준선(감사 수정 시점) **618** → 이번 **625**(신규 7: 배선 6 + 커밋 가드 스키마 예외 1).
Phase 4 관련 170건:

| 파일 | 건수 | 검사 |
|---|---|---|
| `tests/test_phase4_debate.py` | 79 | 6스킬 규칙·프롬프트 4종·스키마 5종·`context.json` 입력 패킷·인용/숫자/가격 검증·프롬프트 인젝션 격리·뉴스 3상태 경계·정족수·예산/마감/재개·Codex CLI/OpenAI 실행기 |
| `tests/test_phase4_ledger_and_ui.py` | 69 | 발행 회차·주문 슬롯·동결, 시각 계약, 섹터 캡·분할 익절가·2·3차 수량/DMA, Paper 채점, A/B 두 계좌·비용, 알림, 공개 빌드 금지 키, 대시보드·RUNBOOK 계약 |
| `tests/test_phase4_ui_and_pipeline.py` | 22 | 실행 순서(reconcile → settle → settle_positions)·정산 전 체결 스냅샷, Paper 홀드아웃 분리, 장애 상태부, 메타 시각·`bundle_version`, 알림 재시도, PWA, **배선 3줄**(§2) |

---

## 4. 아키텍처·보안·공개 허용목록 검증

### 4.1 실행 순서 계약 (P0-2 · 이번 배선 반영)

```text
원장·체결 로드 → 토론 편입 검증(reconcile, 정산 전 체결 스냅샷)
→ [읽기 전용] 정산 전 계좌 조회(섹터 캡 분모) → 1차 정산(settle) → 체결 append
→ 신호 발행·동결(원장 append) → 2·3차 정산(settle_positions, position_events 영속)
→ Paper 채점 → A/B 비교 → 알림
```

- 편입이 정산보다 먼저이고, 편입 검증 시각이 체결보다 늦은 페어는 비교에서 빠진다(`FILLED_BEFORE_VALIDATION`).
- 정산 **앞**의 `settle_positions` 1회는 `position_events_path=None` 인 **조회**다(이벤트 로그 미기록).
  계좌를 확정하는 호출만 파일에 쓴다 — `test_run_daily_reconciles_before_settling` 이 호출 순서와 kwargs 로 고정한다.

### 4.2 공개 금지 필드 0건

- `web/data` 34개 파일 전수 검사 → **금지 키 0건**(`quote`·`body_excerpt_1500`·`shares`·`avg_cost`·`cash_balance`·
  `holdings`·`raw_open` 등), 이메일·토큰 패턴 0건.
- `position_events.jsonl` 행은 `date·ticker·signal_id·event·tranche·reason·price·qty_pct·ratio·fraction·
  cancelled_by·fill_basis`(비중·가격만) — `POSITION_EVENT_FIELDS` 로 고정, 금지 키 없음.
- 모의 계좌 공개물은 여전히 **비중만**(`cash_pct`·`invested_pct`·`positions[].weight`·`sector_weights`).

### 4.3 허용목록 변경 범위

`config/public_allowlist.yaml` 은 **`allow` 목록에만** 키를 추가했다
(트랙 B: 실행 상태·발행 세트·토론 리스크 분리·페어/정책 효과 / 이번 배선: 주문 취소 4키).
`deny_keys`·`deny_paths`·`deny_patterns` 는 **한 항목도 바꾸지 않았다**.

### 4.4 워크플로 불변

`.github/workflows/*` 는 수정·추가·스테이징 **0건**(토큰 권한 없음). `codex_debate/workflows/debate-validate.yml` 은
**템플릿**(사람이 GitHub 웹에서 추가)이며, PR 전체 경로를 검사하고 `.github/workflows/`·`config/params*` 변경을 가드한다.

## 4.5 커밋 가드의 스키마 예외(한 곳)

`codex_debate/schemas/round1.schema.json` 은 1R 스킬 출력의 `quote` 필드를 **선언**해야 한다(원문은 검증 뒤
`quote_sha256` 으로 바뀐다). 기존 `UNIT_CONTAINERS`(SEC XBRL 의 `units.shares`)와 같은 원리로,
**`*.schema.json` 파일의 `properties`/`required` 아래 이름만** 구조 선언으로 인정하도록 가드를 좁혔다.
같은 키라도 데이터 파일(`.schema.json` 이 아닌 `.json`)에서는 **계속 차단**되고, 스키마 파일이라도 값 안의
이메일·토큰 패턴은 계속 걸린다 — `deny_keys`·`deny_patterns` 목록은 **한 항목도 바꾸지 않았다**
(`test_schema_file_declaration_is_structure_not_data`).

---

## 5. 커밋·CI·배포

- 커밋: `9de2c88` — `feat(phase4): codex 6-skill debate, dual A/B ledger, 2nd/3rd tranche & audit fixes`
  (50 files, +9163/−290)
- 스테이징: 실제 변경·신규 파일만(`out/`·`web/data/`·`data/` 미포함 — `.gitignore` 확인).
  `scripts/check_commit_scope.py --staged` → **`scope ok (50 files)`**.
- 브랜치: `main`, `5381f9c..9de2c88` 푸시.

| 실행 | run ID | 결론 | 소요 |
|---|---|---|---|
| `ci` | [44695897072](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44695897072) | **success** | 1m11s |
| `deploy` | [44695896967](https://github.com/top03071188-debug/us-stock-advisor/actions/runs/44695896967) | **success** | 59s |

`ci` 스텝 전부 success: Install(locked) · Lint(`All checks passed!`) · Unit & look-ahead tests(offline) ·
Commit scope guard(`scope ok (4 files)` — `ci.yml` 이 아직 4경로만 검사한다, P1-2 잔여) ·
Public data build guard(34파일 생성, 금지 키 0건). `deploy` 는 `build` → `deploy` 둘 다 success(공개 빌드 34파일, `signals_status: normal`).

> `gh workflow run daily-postclose.yml` 은 **아직 실행하지 않았다** — `.github/workflows/` 의
> `daily-postclose.yml`·`ci.yml`·`debate-validate.yml` 을 웹에서 갱신할 수 있는지 확인 중이다.
> 그래서 이번 검증은 **로컬 계약 파일 기준**(`data/quant_signals.json` as_of 2026-11-19 발행 7건)이다.

---

## 6. 남은 작업 / 미검증

1. `.github/workflows/` 3종(ci·daily-postclose·debate-validate) 갱신 — 권한 있는 경로(웹 업로드) 필요.
2. **`daily-postclose.yml` 의 `git add` 목록이 부족하다** — 이번 Phase 4가 새로 쓰는 파일 중 아래가 빠져 있다
   (지금은 러너 체크아웃에만 쓰이고 사라진다 → 공개 `web/data/health.json` 의 `as_of`·`last_attempt_at`·
   `last_success_at` 이 `null` 로 나간다. 라이브 사이트에서 확인함).

   ```text
   data/health.json                    # P2-1 실행 상태부 (현재 미커밋 → 상태 배너 입력이 빈다)
   data/ledger/position_events.jsonl   # 이번 배선 ①·③ (2·3차·손절·익절·취소 세션 이벤트)
   data/notifications_latest.json      # 알림 요약 (build_public_data 입력)
   data/notifications_log.json         # 채널별 발송·재시도 기록 (P2-1)
   data/ledger/debate_summary.json     # 토론 편입 요약
   data/pipeline_limits.json · data/walkforward/stress.json
   ```

   (`data/snapshots/` 는 gitignore 대상이라 제외, `data/debate_reports.json` 은 PC/Cloud 토론 실행 후 사람이 커밋)
3. 실데이터 배치(`daily-postclose` 수동 실행)로 2·3차 정산·`position_events.jsonl`·토론 편입 실측.
4. `data/debate_reports.json` 은 **PC/Cloud 토론 실행 후 커밋**되어야 채워진다(현재 0건 = 화면 "토론 없음" 정상).
5. 파일럿 `N_eff`·두 계좌 `policy_diff_pp` 는 표본이 쌓여야 판정된다(현재 `보류`).
