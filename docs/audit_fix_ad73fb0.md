# 감사 `ad73fb0` 수정 기록 — 12건 · §10 6건 · 추가 발견 · 정책 P-1~P-5

- 작성: 2026-11-20 (통합 담당)
- 대상 감사: `scratch/audit_verdict_ad73fb0.md` (2026-11-20, 커밋 `ad73fb0` 기준)
- 수정 범위: `engine/ledger/{live,debate,record}.py` · `engine/decide/{pipeline,signals}.py` ·
  `codex_debate/{context,executors,validate,run_debate}.py` · `scripts/{run_daily,check_commit_scope,archive_snapshot,build_public_data,notify}.py` ·
  `config/public_allowlist.yaml` · `web/{app.js,index.html,sw.js}` · `tests/**`
- 검증: `pytest -m "not network"` **734 passed / 1 skipped / 4 deselected**, `ruff check .` **0건**,
  `scripts/validate_outputs.py` **ok**, `scratch/audit_repro_ad73fb0/verify_fix_map.py` **18/18 그룹 PASS**,
  `r01~r13` 재실행 출력 `scratch/audit_repro_ad73fb0/outputs_after_ad73fb0.txt`

감사 판정은 **12건 중 10건 타당, 2건 부분타당(02·04), 오탐 0건**, §10 은 **5건 타당 + 1건 오탐(④)** 이었다.
이 문서는 그 각각을 **어디를 어떻게 고쳤고, 재현 수치가 어떻게 바뀌었는지**로 정리한다.

---

## 0. 확정 정책 (P-1 ~ P-5)

감사 §15 가 "사용자 결정"으로 남긴 지점은 아래로 확정해 코드·문서·테스트에 같이 박았다.
(모두 **매매 행동이 달라지는** 결정이므로 여기에 근거를 남긴다.)

| ID | 사안 | 확정 | 근거·영향 |
|---|---|---|---|
| **P-1** | 손절 후 재진입 잠금 | **(b)** 손절이 난 **같은 종목·같은 세션**의 **신규 1차 진입**을 막고, 막은 건수를 `health.stop_reentry_blocked_count` 로 **분리 집계**. **다음 세션 재진입은 허용** | 기획서 §3.5 의 "같은 봉 잔여 주문 취소"를 넘어서는 해석이므로 잠금 범위를 *세션*으로 못 박음. 2·3차 취소 수(`tranche_cancel_count`)와 섞이지 않게 이름을 나눔 |
| **P-2** | 동결 정각 규칙 | **(a)** `validated_at ≤ frozen_at` 이면 **편입**(정각 포함), `>` 면 `late` | 기획서의 두 문장(“정각은 편입”/“동결 이후는 late”)을 하나로 고정. `eligibility()` docstring·테스트가 이 한 문장을 따른다 |
| **P-3** | V11 무결성 훼손 시 사전 중단 | **(b)** `injection_integrity_compromised` 면 **Codex 호출 전에** 그 종목을 접는다(10콜을 쓰지 않음) | 노출·예산을 함께 줄인다. 그날 그 종목의 토론은 0건(`status: blocked_integrity`) |
| **P-4** | A/B 청산 슬리피지 | **(a)** 진입은 체결가에 이미 10bp 포함, **청산에도 10bp** | 진입 중복 적용은 **버그**로 제거(아래 05). 왕복 20bp 는 의도된 보수 가정 |
| **P-5** | TRIM 실행 여부 | **(a)** `exit_score 60~74 → TRIM` 은 **보유 1/3 익절 주문**으로 실행(전량 청산 아님) | `live.FORCED_EXIT_REASONS = ("S3","S5","EXIT","TRIM")`, 잔여 2·3차 주문은 취소하지 않음 |
| (P-6) | `data_status` 요약 필드 | `run_daily` 가 **정산 health + 실행 상태(run_state)** 를 함께 넘긴다 | 정책이 아니라 1줄 배선(§10⑥). 감사 권고대로 즉시 수정 |

---

## 1. 감사 12건 — 수정 내용과 전후 수치

### 치명 01 — 슬롯 동결과 보유 원장

**원인**: ① 체결 루프가 *슬롯*이 아니라 *회차*를 돌아 같은 세션에 두 번 샀다. ② `settle_positions` 가
`position_events` 의 청산·2·3차 이벤트를 **재생하지 않아** 동결 뒤 WATCH 로 체결이 취소되면 보유가 0주가 됐다.
③ `blocked` 는 레코드를 만들지 않아 이전 BUY 를 대체·취소할 대상이 없었다. ④ 취소 `at` 이 동결 마감 시각,
`frozen_at` 은 `None`(슬롯이 `pending_buy` 만 만들어져) 이었다.

**수정**
- `live.order_slots()` — `(account_id, ticker, target_session)` 슬롯 단위로 **동결 주문 1건**만 확정.
  동결 뒤 도착분은 `held`(사유 `ARRIVED_AFTER_FREEZE`)로 보류. WATCH·차단 재발행도 슬롯을 만들어 `frozen_at` 을 남긴다.
- `live.settle()` — 체결을 **슬롯 루프**로 돌린다. 계보는 `superseded_ids`+`_superseding_map` 으로 `-001→-002→-003` 을 끝까지 따라간다.
- `record.make_blocked_supersede_record()` — 차단을 `blocked=True`·`blocked_reason`·`blocked_codes`·
  `supersede_only=True`·`release_kind="BLOCKED"` 인 `no_action` 레코드로 남겨 **이전 BUY 를 대체**한다.
- `live.settle_positions()` — 세션 로그(`position_events`)의 `exit_filled`·`profit_take`·`cancel_pending` 을 **먼저 재생**한다.
- 취소 `at` = **실제 대체 발행 시각**(`now` 또는 대체 레코드의 `signal_published_at`), `freeze_deadline_at` 은 참고 필드로 분리.

**재현 전 → 후** (`r01_slots.py`)

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| (a) BUY → 동결 뒤 BUY | `buy_filled` **2건** | **1건**(100.1), 보유 199.8주 |
| (b) BUY → 동결 뒤 WATCH | 체결 1건인데 보유 **0주**·현금 1,000,000·`equity_index 1.0` | 보유 **199.8주**·현금 980,000·`equity_index 1.00018` (replay 와 일치) |
| (c) 동결 전 WATCH | 정상(취소) | 정상 유지 |
| (d) 같은 배치 차단 | 이전 BUY 가 그대로 시가 체결 | 차단 레코드가 대체 + `settle(blocked=…)` 로 시가 체결 차단 |

### 치명 02 — 토론 편입의 시각

**원인**: `run_daily` 가 `now`(배치 수신 시각)를 넘기지 않아 보고서 **자기신고 시각**이 그대로 통과했고,
바이트 영수증이 없었으며, 필드 없는 구 리포트는 여섯 시각 검사를 **전부 건너뛰었다**.

**수정**
- `run_daily` → `reconcile_debates(now=run_started_at, require_receipt=True)`.
- `debate.report_sha256` **바이트 영수증**(영수증 필드를 뺀 정규 바이트 SHA-256) 필수·대조.
- `strict=True` 기본 — `completed_at`·`report_validated_at`·`order_cutoff_at` 이 없으면 `MISSING_TIMESTAMPS`.
- `_debate_link_ok` — `snapshot_id`·`signal_id` 일치까지 요구(07 과 같은 경계).
- 보고서가 `now` 보다 늦은 검증 시각을 적으면 `now` 로 깎고 `POSTHOC_VALIDATION` 기록.

**재현 전 → 후** (`r02_windows.py`): 인자 없는 호출 `eligible 1 / late 0` → **`late 1`**,
15:00Z 검증 → `late 1 + ATTACHED_AFTER_FILL`, 영수증 없는 보고서 → `REPORT_RECEIPT_MISSING`(부적격).

### 중요 03 — 매도·분할·섹터 한도의 세션 순서

**원인**: ① 세션 이벤트를 재생하지 않아 재실행 시 청산이 사라짐 ② `as_of` 봉이 없으면 청산 자체를 건너뜀
③ 2·3차 섹터 캡이 `cash_spent`(미래 지출 포함)라 입력 순서에 의존 ④ 손절 뒤 재진입 잠금 없음 ⑤ TRIM 미실행.

**수정**: `settle_positions` 를 **날짜 바깥 루프**로 다시 썼다 — 날짜마다 `시가 예약(강제 청산·TRIM·예약 매도/매수)
→ 장중 손절 → 지정가 2·3차 → 종가(트레일링·평가)`. 봉이 없으면 `pending_exit` 으로 남겨 다음 봉 시가에 체결,
섹터 캡은 **그 시점 순자산·그 시점 섹터 평가액**(`equity_at`/`sector_value_at`)으로 통일.
`stop_reentry_locks()` 로 세션 로그의 S5 에서 **같은 세션** 잠금을 파생(P-1).

**재현 전 → 후** (`r03_replay.py`)

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| (1b) S3 청산 뒤 재실행 | `exit=None`·현금 999,960 → 980,000(포지션 부활) | `exit={'S3','2026-05-12'}` 유지·현금 999,960.04 |
| (2) `as_of` 봉 없음 | 청산 미실행 | `pending_exit` 예약(`{'S3','since':'2026-05-13'}`) |
| (3) 입력 순서 뒤집기 | 55,000 vs 51,401 | **51,419 / 51,419** (순서 무관) |
| (4) 같은 종목 재진입 | 잠금 없음, 두 번 체결 | P-1 = **같은 세션만** 잠금 → 다음 세션 재진입은 허용(정책 확정), `stop_reentry_blocked_count` 와 `tranche_cancel_count` 분리(0 / 2) |
| (5) TRIM | `_forced_exits` 가 TRIM 제외 | `TRIM` 전달 → 1/3 익절 실행 |

### 중요 04 — A/B 계좌의 거래 모집단

**원인**: 후보 모집단이 `by_signal`(적격 보고서)에서만 나와 **보고서 없는 정량 체결**이 두 계좌 모두에서 빠졌고,
`pending_buy`(의도)를 `buy_filled`(체결)로 세는 경로가 있었다. (③ "B 계좌 독립 정산 부재"는 **오탐** — 계좌별 현금·섹터를 이미 따로 계산한다.)

**수정**: 후보는 **원장 전체**(records+executions)에서 만들고, 두 계좌는 같은 후보 목록을 각자의 정책으로 채점한다.
거래는 **체결 이벤트가 있을 때만** 센다(`intent_without_fill` 제외).

**재현 전 → 후** (`r04_accounts.py`): (a) `quant_only 0건 / codex_debated 0건` → **둘 다 2건·후보 1건·순자산 100,190.67**,
(b) 적격 보고서 1건 → `quant_only 2건` vs `codex_debated 0건`(보류), (c) 의도만 있으면 **양쪽 0건**.

### 중요 05 — A/B 비용·시가·손절 순서

**원인**: 진입 체결가에 이미 10bp 가 들어 있는데 **또** 적용(중복), 시가 비중을 당일 종가로 계산,
첫날 손절 수량 0, MDD 가 초기 자산을 고점으로 보지 않음.

**수정**: 진입 슬리피지 1회, 비중은 **직전 종가**, 첫날 손절도 체결, MDD 곡선 맨 앞에 초기 자산(1.0)을 넣는다.

**재현 전 → 후** (`r05_costs.py`)

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| (1) 왕복 비용 | 99,794.61(진입 10bp 중복) | **99,796.40** (= 청산 10bp 1회) |
| (2) 첫날 손절 | 거래 1건·100,000 | **2건·99,796.40** |
| (3) 첫날 −0.2% | MDD 0.0 | **MDD −0.1818%** |
| (4) 같은 날 종가만 100→130 | 다른 종목 주문 순자산 +29.85 | **0.00** (당일 종가 미사용) |

### 중요 06 — 자동 하향의 분기 자격과 주문 적용

**원인**: ① 인접 분기 검증 없음(2026Q1·Q3 통과) ② 정책 버전 없으면 `same_policy_version=True` ③ 분기 행에
`policy_version` 미기록 ④ 계좌 차이 없이 페어 손익으로 `eligible` ⑤ **ON 인데 화면은 `hold_cash`, 원장은 `buy_filled`**.

**수정**: `evaluate_auto_downgrade_eligibility()` 가 **연속 분기·명시적 동일 정책 버전·계좌 순자산 차이(`policy_diff_pp`)·방어력 개선**
네 조건을 모두 요구하고, 분기 행에 `policy_version`·`policy_versions` 를 기록. 페어 손익 fallback **제거**.
⑤는 **경계 배선**(§2-4)으로 닫았다 — `run_daily` 가 자동 하향 적용분을 `live.settle(debate_holds=…)` 로 넘겨
원장에 `hold_cash`(체결 아님)를 남긴다.

**재현 전 → 후** (`r06_quarters.py`): (a) `eligible True` → **False(연속 분기 아님)**,
(b) `True` → **False(정책 버전 없음)**, (d) `pair_net_legacy` → **`insufficient`**,
(e) 원장 `buy_filled` → **`hold_cash`·`settled.reason=AUTO_DOWNGRADE`**.

### 중요 07 — 정량 기준선과 신뢰도 A

**원인**: `ticker` 키 하나로 토론을 붙여 **구 스냅샷 판단**이 오늘 `news_state`·구조 리스크를 바꿨고,
`regime_sample=None` 이면 A 가 통과했으며, 데이터 신선도 인자·8-K 대체 근거가 없었다.

**수정**: `run_daily.DebateByKey((signal_id, snapshot_id))` + `pipeline.debate_row_for()` 가
스냅샷·신호·종목·자격을 모두 대조한 행만 반영. `confidence_grade(..., data_fresh=, alt_8k_evidence=, grade_basis=)` 로
**신선도(직전 영업일 봉)·국면 표본·대체 근거**를 요구하고 A 의 근거를 레코드(`confidence_detail`)에 남긴다.
토론의 `structural_risk` 는 자동 하향 ON 일 때만 게이트에 들어간다(P1-1 분리).

**재현 전 → 후** (`r07_pipeline.py`): 구 스냅샷 행 → **버려짐**(`_debate_link_ok` `snapshot_mismatch`),
`regime_sample=None` → **B**, 신선도 인자 존재(`data_fresh`), A 근거가 `grade_basis` 에 남는다.
※ `_debate_news_state()` 자체는 여전히 값만 병합한다 — **경계(어떤 행을 붙일지)가 `debate_row_for` 로 옮겨진 것**이 수정이다.

### 중요 08 — V11 격리와 환각 검증

**원인**: 격리 목록에 `source`·`url` 이 남고 URL 은 스캔 대상이 아니었으며, 오탐 3건,
48h 판독 분모가 모델 제출 id 수였고, 숫자 검증이 단위·필드를 보지 않았다. 무결성 훼손 시에도 10콜을 다 돌았다.

**수정**: URL 까지 스캔·격리(`prompt_injection_url`), 격리 항목은 `article_id`·`content_sha256`·`reason_code` 만 남김,
패턴을 문장 맥락으로 좁혀 오탐 제거, `items_in` = **48h 유효 기사 수**, 숫자는 **필드·단위 인식** 검증,
P-3 로 호출 전 종료.

**재현 전 → 후** (`r08_news.py`): `items_in 2 / drop_rate 0 / read_done` → **`items_in 10 / drop_rate 0.8 / insufficient_evidence`**,
오탐 3건(`system prompt`·`role_override`·`developer prompt`) → **0건**, URL 지시문 → **탐지**,
`−3.5`·`350`·`35`·`0.035` 허용 → **거절**(3.5·4·7 만 통과).

### 중요 09 — PC 실행과 부분 재개

**수정**: `strict_schema_file` 임시 경로를 호출별 고유 이름으로(병렬 8회 예외 7 → **0**),
`git_pull` → `select_targets` 순서, `resume` 는 **비어 있지 않고 일치하는 키**를 요구하며 `Validated.ok`/`data` 를 복원,
콜 타임아웃은 `min(timeout_s, 남은 마감)`, 2R 전부 실패면 `status: partial`(complete 아님), `--deadline` 오타는 **거절**.

**재현 전 → 후** (`r09_runner.py`): 병렬 예외 **7/8 → 0**, 호출 순서 `select_targets, git_pull` → **`git_pull, select_targets`**,
복원 `ok=True` 4건 → **0건**, `resolve_deadline('not-a-date') → None` → **ValueError**, 2R 전멸 `complete` → **`partial`**.

### 중요 10 — Git·Pages 공개 범위

**수정**: `--staged`/`--commit` 는 **커밋될 바이트**(인덱스·커밋 blob)를 본다, UTF-8 실패는 `ERROR non-utf8`,
`*.schema.json` 예외는 **승인 경로의 유효 스키마 `properties`/`required`/`$defs` 이름**에만, `units` 예외는 XBRL 구조에만,
`data/debate/manifest.json` 은 허용(PR 규칙과 일치), 실행기 커밋은 **자기 경로만** 스테이징, PR 검사는 **기본 브랜치 검증기**로.

**재현 전 → 후** (`r10_scope.py`): 워킹트리에서 지운 `quote` → **차단**, 비UTF-8+토큰 → **차단**,
`payload.schema.json`/`account.json` 우회 → **차단**, `data/debate/manifest.json` → **통과**.
※ (e) 는 스크립트 자체 헬퍼를 재현한 것이라 여전히 4파일이 보인다 — 실행기 쪽은
`test_commit_does_not_carry_unrelated_staged_files` 가 고정한다.

### 중요 11 — 장애 시 공개 상태 발행

**수정**: `run_daily` 가 `GITHUB_OUTPUT` 에 `run_outcome`·`snapshot_created`·`status_written`·`data_status`·`outage_id` 를 남기고,
`archive_snapshot.py package` 는 스냅샷이 없으면 **종료 3 + 한 줄 경고**(스택 트레이스 아님).
`daily-postclose.yml`(웹 업로드본)은 `snapshot_created == 'true'` 일 때만 스냅샷 패키징·아카이브하고,
health·알림·`position_events`·`web/data` 는 장애 실행에서도 **항상** 커밋·배포한다.

**재현 전 → 후** (`r11_outage.py`): `package` rc **1 FileNotFoundError** → **rc 3 + `::warning::스냅샷 없음`**.

### 중요 12 — PWA 정적 세트의 일관성

**수정**: `build_public_data` 가 **모든 공개 파일의 sha256** 으로 `bundle_version` 을 만들고 `deploy_manifest.json` 에 파일별 해시를 남긴다.
`sw.js` 는 `set-bundle-version` 이 올 때까지 새 세트를 **스테이징**하고, 한 번에 활성화하며, 파일 자체의 `bundle_version` 이
활성 세트와 다르면 저장하지 않는다. `meta` 가 마지막에 와도 나머지 파일이 사라지지 않는다. 화면은 실행 상태·세트 불일치 배지를 띄운다.

**재현**: `r12_sw.py` 는 **구 `sw.js` 로직을 파이썬으로 옮긴 재현기**라 새 동작을 보여 주지 않는다(스크립트 정적).
실제 검증은 `tests/test_phase4_ui_and_pipeline.py::test_sw_activates_a_whole_set_atomically`(node 로 새 `sw.js` 실행)와
`test_public_scope.py::test_bundle_version_covers_all_files_not_only_signals` 가 한다 — 둘 다 통과.

---

## 2. §10 6건

| # | 판정 | 수정 |
|---|---|---|
| ① 취소 `at`=동결 마감·`frozen_at=null` | 타당 | 취소 `at` = **실제 대체 발행 시각**, `freeze_deadline_at` 분리. WATCH·차단 재발행도 슬롯을 만들어 `frozen_at` 을 남긴다 (`test_cancel_event_uses_real_supersede_time_not_freeze_deadline`) |
| ② `-002→-003` 취소 누락 | 타당(원인 정정) | `run_daily` 순서를 **발행·대체·차단 append(6) → 정산(7)** 로 바꿔 같은 실행에서 취소가 기록된다(다음 날 back-date 없음) (`test_revision_chain_cancellations_are_recorded_in_the_same_run`) |
| ③ `-001` 간 `signal_published_at` 차이 | 타당 | `revision` 필드를 `-001` 에도 기록하고, 회차는 `split_signal_id()` 로 파생 — 화면·원장이 같은 번호를 쓴다 (`test_revision_is_recorded_on_first_release_and_every_revision`) |
| ④ 신호 `validation: null` | **오탐** | 수정 대상 아님(정량 신호에 검증 시각 계약은 없다). 대신 07 의 **신선도**를 `confidence_detail` 로 남겼다 |
| ⑤ BUY 없던 WATCH 의 `cancel_pending` | 타당 | `quant_intent` 를 **먼저** 보고, 주문이 없던 회차는 `intent_superseded`(회차 무효화)로만 기록. `cancel_kind` 로 사유를 가른다 (`test_watch_only_reissues_never_emit_cancel_pending`) |
| ⑥ `notifications_latest.json` `data_status: null` | 타당 | `run_daily` 가 `health=run_state + 정산 health` 를 넘긴다 — 장애 경로와 같은 값 |

---

## 3. 12건 밖 추가 발견 (§14)

| # | 내용 | 수정 |
|---|---|---|
| 1 | **§3.5 TRIM 미실행** (화면 카드에만) | P-5 — `FORCED_EXIT_REASONS` 에 `TRIM`, 보유 1/3 익절. 잔여 2·3차는 유지 |
| 2 | **섹터 캡 분모 이원화**(1차 달러 vs 2·3차 `cash_spent`) | 양쪽 모두 그 시점 순자산·섹터 평가액. 실행기가 `equity_base = INITIAL_CASH × equity_index` 를 넘긴다 |
| 3 | `-001` 레코드에 `revision` 없음 | `publish_record` 가 항상 `revision=1` 기록 |
| 4 | `position_events` append 충돌이 배치를 죽임 | 1차 취소 append 도 `try/except ValueError` 로 감싸고 `diagnostics.position_events_error` 만 남긴다 |
| 5 | `outage` 시 `last_success_at: null` | `write_health_state` 가 마지막 성공 실행을 이어받아 남긴다(없으면 `null` + "마지막 성공 기록 없음" 문구) |
| 6 | `debate-validate` 등록본이 "템플릿" 주석 | 등록본 머리말을 **운영 문구**로 바꾸고 `codex_debate/workflows/` 사본과 내용 일치를 테스트로 고정 |

---

## 4. 경계 배선 — 실행기에서 실제로 켜야 하는 것들

엔진 규칙이 있어도 `run_daily` 가 인자를 넘기지 않으면 운영에는 적용되지 않는다. 이번 통합에서 닫은 배선:

1. **`live.settle(..., position_events_path=live.POSITION_EVENTS_PATH)`** — S5 손절 세션 로그에서 **P-1 잠금**을 파생.
2. **`live.settle(..., equity_base=…)`** — 1차 섹터 캡 분모 = `INITIAL_CASH × equity_index`(정산 전 순자산).
   `run_daily.settle_equity_base(portfolio)` 로 한 곳에서 계산.
3. **`live.settle_positions(..., stop_reentry_blocked=settlement["stop_reentry_blocked"])`** — `health.stop_reentry_blocked_count`
   가 2·3차 취소 수와 섞이지 않고 **이번 정산에서 실제로 막은 수**가 된다. `ledger_view(stop_reentry_blocked=…)` 로 전달.
4. **`live.settle(..., debate_holds=…)`** — 감사 06-(e). `run_daily.debate_holds_for()` 가
   `reconcile`/`actions_for` 의 `auto_downgrade_applied` 신호를 모아 **신호 정체성(signal_id) 키**로 넘기고,
   원장은 `hold_cash` + `settled.reason="AUTO_DOWNGRADE"` 로 남긴다. 자동 하향이 꺼져 있으면 빈 dict.
5. **`DebateByKey((signal_id, snapshot_id))`** — `pipeline.build_views`/`build_payload` 가 `debate_row_for()` 로
   스냅샷까지 대조한다(구 스냅샷 차단).

**계약 정합**: `config/public_allowlist.yaml` ↔ `check_commit_scope.ARTIFACT_CONTRACTS` —
`blocked`·`blocked_reason`·`blocked_codes`·`supersede_only`·`release_kind`·`cancel_kind`·`tranche_cancel_count`·
`cancelled_tranches`·`pending_exit_count` 를 포함해, 실제 배치가 커밋하는 산출물 19개(`quant_signals`·`health`·알림 2종·
`params_history`·`pipeline_limits`·`walkforward`·`ledger/*`)와 `web/data` 17개가 **가드 위반 0건**임을 확인했다
(`check_commit_scope.py --paths`, `--staged` 29파일 ok).

**임시 작업물**: `/workspace/tmp_check`, `/workspace/tmp_build`, `/workspace/tmp_build_log.json`, `/workspace/tmp_blk2` 는
정리되어 현재 없음(정리 전 `node` 로 `sw.js` 하네스 통과를 확인했고, 지금은 그 하네스가 테스트로 고정돼 있다).

---

## 5. 테스트

| 구분 | 결과 |
|---|---|
| `pytest -m "not network"` | **734 passed, 1 skipped, 4 deselected** (38s) |
| `ruff check .` | **All checks passed** |
| `scripts/validate_outputs.py` | `{"ok": true, …}` (신호 4·차단 2·레코드 8·포트폴리오 1) |
| `verify_fix_map.py` (결함 → 테스트 지도) | **18/18 그룹 PASS** (64 테스트 노드) |
| `r01`~`r13` 재실행 | `scratch/audit_repro_ad73fb0/outputs_after_ad73fb0.txt` |

새로 추가한 통합 테스트 `tests/test_audit_ad73fb0_wiring.py` (13건):

- `test_settle_receives_position_events_path_and_equity_base` — 배선 계약(시그니처 드러내는 spy + 순자산 1.05 배율)
- `test_stop_session_log_blocks_new_first_entry_and_reports_count` — P-1 행동 + `health` 카운트 1 + 세션 로그 없는 대조군은 체결
- `test_auto_downgrade_hold_reaches_ledger` / `test_auto_downgrade_off_keeps_quant_fill` — ON 은 `hold_cash`, OFF 는 `buy_filled`(회귀)
- `test_debate_key_blocks_stale_snapshot_in_pipeline`, `test_debate_holds_only_when_auto_downgrade_enabled`
- `test_hold_cash_event_is_not_a_fill`, `test_hold_does_not_fill_when_session_already_passed`
- `test_stop_lock_and_hold_share_the_same_slot_rules`, `test_settle_positions_counts_only_passed_reentry_blocks`,
  `test_ledger_view_forwards_reentry_blocks`, `test_settle_equity_base_tracks_equity_index`, `test_hold_reason_matches_by_signal_then_ticker`

---

## 6. 남은 한계 · 운영 주의

1. **`r12_sw.py`, `r13_section10.py` 는 정적 재현기** — 전자는 구 `sw.js` 로직 사본, 후자는 `ad73fb0` 커밋 데이터를 읽는다.
   두 결함의 수정은 각각 node 하네스 테스트(`test_sw_activates_a_whole_set_atomically`)와
   `test_revision_chain_cancellations_are_recorded_in_the_same_run`·`test_cancel_event_uses_real_supersede_time_not_freeze_deadline` 이 고정한다.
2. **과거 데이터는 소급 수정하지 않는다** — `data/ledger/records.jsonl`·`position_events.jsonl` 의 `2026-11-20` 이전 행에는
   `frozen_at: null`·`at: 14:20Z` 가 남아 있다(append-only). 새 실행부터 새 규칙으로 기록된다.
3. **`.github/workflows/*` 는 git 으로 push 하지 않는다** — CLI 토큰에 `workflows` 권한이 없다(403).
   운영 워크플로는 GitHub 웹 업로드로만 갱신하고, `codex_debate/workflows/debate-validate.yml` 사본과 내용 일치를 테스트가 확인한다.
4. **자동 하향 ON 은 여전히 기본 OFF** — `params.debate.auto_downgrade_enabled=false`. ON 으로 바꾸면 06 의 네 조건을
   통과한 분기에서만 실제로 켜지고, 켜진 배치는 원장도 `hold_cash` 로 남는다(이번 배선).
5. **P-3 로 토론 0건이 되는 날이 있다** — 무결성 훼손 종목은 호출 전에 접히므로 그 종목의 A/B 페어가 비어 있다.
   `debate_summary.json` 의 `status: blocked_integrity` 로 사유가 남는다.
