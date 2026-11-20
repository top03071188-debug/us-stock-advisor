# Phase 4 RUNBOOK — 토론 편입 · A/B 채점 · 실시간 분할/익절 · 알림

기획서 v1.3.3 §3.5 · §3.11.8 · §4.8 · §4.12 · §5.7 운영 절차.
이 문서는 **사람이 읽고 그대로 따라 하는 순서**만 적는다. 규칙의 근거는 기획서를 본다.

---

## 0. 한눈에 — 매일 도는 것과 사람이 하는 것

| 단계 | 누가 | 무엇을 | 산출물 |
|---|---|---|---|
| 07:00 KST `daily-postclose` | Actions | 수집 → 신호 발행 → 1차 정산 → **2·3차 분할·손절·분할익절 정산** → 토론 편입 확인 → A/B 비교 → 알림 | `data/quant_signals.json`, `data/ledger/portfolio/state.json`, `data/health.json`, `data/ledger/debate_comparison.json`, `data/notifications_latest.json` |
| 신호 발행 ~ 주문 마감(09:25 ET) | PC/Cloud | 토론 실행(`codex_debate/`) → `data/debate_reports.json` 커밋 | `data/debate_reports.json` |
| 다음 배치 | Actions | 그 리포트가 **편입 자격**을 통과하면 `data/ledger/debate_events/{signal_id}.json` 에 붙는다 | 토론 카드·A/B 페어 |
| 분기 | 사람 | §4.8.2 판정 확인 → 필요하면 `auto_downgrade_enabled` PR | `data/params_history.json` |

**토론을 돌리지 않으면?** 정량 트랙은 그대로 돌고 화면은 "토론 없음"을 보여 준다. A/B 페어는 0건으로 남고 판정은 "표본 부족 — 판단 보류"다. 이것이 정상 상태다.

---

## 1. 일상 운영 (매일)

1. `daily-postclose` 실행 결과의 `status` 를 본다: `normal` / `degraded` / `stale` / `sample`.
2. `data/health.json` 을 확인한다.
   - `stop_reentry_blocked_count` — **손절 직후 재진입 차단 건수**. 이 값이 오르면 S5(손절)가 났고, 같은 봉의 잔여 2·3차 주문이 정상적으로 취소된 것이다(규칙이 작동한 증거).
   - `tranches_pending` — 아직 살아 있는 2·3차 주문 수. 3차가 `invalid` 면 "3차 손절선 밖 무효"다.
3. `data/notifications_latest.json` 의 `n_new` 를 확인한다. 같은 `event_key` 는 다시 오지 않는다(중복 0건 계약).
4. 화면(성적표 탭)의 **실시간 모의 보유·분할 현황**에서 종목별 1·2·3차 상태·손절 여력·분할익절 단계를 확인한다.

### 손절이 났을 때

```
data/ledger/portfolio/state.json → 해당 종목이 목록에서 사라진다(청산).
data/health.json → stop_count +1, stop_reentry_blocked_count +(취소된 주문 수)
data/notifications_latest.json → "청산 신호(EXIT/S3/S5)" 1건
```
같은 봉에서 3차 지정가가 다시 체결돼 포지션이 되살아나면 **버그**다. `tests/test_phase4_ledger_and_ui.py::test_stop_out_cancels_pending_tranches_and_records_health` 가 그 회귀를 막는다.

---

## 2. 토론 실행 (PC/Cloud)

```bash
# 후보 1~6종목 (기획서 max_tickers_per_run: 6)
python codex_debate/run_debate.py --tickers MSFT NVDA
```

- 실행 창은 **신호 발행 직후 ~ 주문 마감(09:25 ET)** 이다. 그 창을 넘겨 끝나면 `late: true` 로 저장되고 **그날 결정에는 붙지 않는다**(다음 스냅샷의 입력으로만 쓰인다).
- 신호가 새 스냅샷을 발행하면 `snapshot_id` 가 달라져 자동으로 빠진다 — 편입 자격을 다시 확인하는 것이지, 사람이 손으로 붙이는 것이 아니다.
- 호출 한도는 `config/params_v001.yaml` 의 `debate.max_calls_per_day`(120)·`max_calls_per_run`(80) 이다. 한도에 걸리면 `stopped_by_limit` 상태로 끝나고 편입되지 않는다(정족수 미달 배지).

### 리포트를 붙인 뒤 확인할 것

1. 화면 종목 상세 ⑦ **찬반 토론** 카드의 배지
   | 배지 | 의미 | 그날 결정에 붙나 |
   |---|---|---|
   | `토론 완료` | 4조건 통과, 권고 = 정량 등급 | 붙는다(행동 동일) |
   | `산식과 다름(보류 권고)` | 4조건 통과, 권고 < 정량 등급 | 붙는다(**행동 갈린 페어**) |
   | `지연 도착` | 주문 마감 뒤 완료 | 붙지 않는다 |
   | `정족수 미달` | `status != complete` | 붙지 않는다 |
   | `무효(발행 전 완료)` | 신호 발행 전에 끝남 | 붙지 않는다(소급 금지) |
   | `토론 없음` | 리포트 자체가 없음 | — |
2. `data/ledger/debate_events/{signal_id}.json` 에 `signal_id`+`snapshot_id` 로 이벤트가 쌓였는지.
3. `data/ledger/records.jsonl` 은 **바뀌지 않았는지**(발행 레코드 불변). 배포 전 `python scripts/validate_outputs.py` 가 이 불변식을 검사한다.

---

## 3. A/B 판정 읽기 (분기 1회)

성적표 탭 **AI 토론 효과 (A/B)** 위젯과 `data/ledger/debate_comparison.json`.

| 필드 | 읽는 법 |
|---|---|
| `divergent_pairs` / `n_graded_pairs` | 비교의 모집단. **30건 미만이면 숫자를 결론으로 읽지 않는다** |
| `n_eff` | 같은 종목이 63거래일 안에 겹치면 하나로 묶은 표본. **15 미만이면 판정 보류** |
| `avoided_loss` / `missed_gain` | 총자산 대비 %p. `net = avoided − missed` |
| `net_after_cost` | 위 값 + 아낀 왕복 비용(슬리피지 10bp·수수료 2bp·환전 5bp ×2) |
| `avoidance_accuracy` | 보류한 건 중 주가가 내린 비율(3개월) |
| `drawdown.quant_tail_5pct` / `debate_tail_5pct` | 최악 5% 분위 MDD. 방어력 비교 |
| `by_regime` | Risk-On/Neutral/Risk-Off 분해. 국면마다 표본이 작으니 같은 기준을 적용한다 |
| `verdict` | `표본 부족 — 판단 보류` / `토론 유효` / `차이 없음` / `토론 역효과` |

**사전에 못 박은 해석 규칙**(결과를 보고 기준을 고치지 않는다):

| 결과 | 판정 | 다음 행동 |
|---|---|---|
| 페어 < 30 또는 `N_eff` < 15 | 표본 부족 — 판단 보류 | 표시만. 결론 없음 |
| 비용 후 순효과 ≥ **+0.20%p** 2분기 연속 **&** 방어력 개선 | 토론 유효 | 자동 하향 전환 **후보** |
| ±0.20%p 이내 | 차이 없음 | 자동 하향 계속 OFF |
| ≤ **−0.20%p** | 토론 역효과 | 호출 예산 축소, 스킬 문구 재검토, `quant_only` 유지 |

---

## 4. `auto_downgrade_enabled` 전환 절차 (사람이 PR 로만)

> 기본값은 **false** 다. 켜기 전까지 토론은 "설명 + 모의 비교"용이고, 체결은 정량 그대로 나간다.

1. **조건 확인** — 아래 넷을 모두 만족해야 한다(§4.8.2 · §3.11.8).
   ```bash
   .venv/bin/python - <<'PY'
   import json, sys
   sys.path.insert(0, ".")
   from engine.ledger import debate
   cmp = json.load(open("data/ledger/debate_comparison.json"))
   hist = json.load(open("data/params_history.json"))
   print(json.dumps(debate.evaluate_auto_downgrade_eligibility(cmp, hist), ensure_ascii=False, indent=1))
   PY
   ```
   - `pairs >= 30` · `n_eff >= 15` · `net_after_cost >= +0.0020` **2분기 연속** · `defense_improved`
   - 출력의 `recommend_enable: true` 가 아니면 **켜지 않는다**.
2. **이력 확인** — `data/params_history.json` 의 `debate` 블록에 분기별 판정이 쌓여 있어야 한다(`params_v001-debate-2026Q4-01` 형태).
3. **PR** — `config/params_v001.yaml` 의
   ```yaml
   debate:
     auto_downgrade_enabled: false   # → true
   ```
   를 바꾸고, PR 본문에 위 판정 출력(페어 수·`N_eff`·비용 후 순효과·방어력)을 붙인다.
   `grade_promote_from_debate: false` 는 **절대 바꾸지 않는다**(올리는 권고 금지).
4. **머지 후 확인** — 다음 배치부터 `debate_comparison.auto_downgrade_enabled: true`, 화면 위젯의 배지가 `자동 하향 ON` 으로 바뀐다. 켠 뒤로는 새 페어가 쌓이지 않는다(A/B 가 끝났기 때문). 켠 시점 전후 성과는 `params_history.json` 의 버전으로 나눠 비교한다.
5. **되돌리기** — 같은 절차로 `false` 로 되돌린다. 되돌린 사실도 `params_history.json` 에 남는다.

---

## 5. 알림 채널

- 기본: 콘솔 + `data/notifications_latest.json`(항상 켜짐).
- GitHub Issue: `NOTIFY_GITHUB_REPO=owner/repo` + `gh` CLI 인증이 있을 때만 동작.
- Webhook: `NOTIFY_WEBHOOK_URL` 이 있을 때만 동작.
- 단독 실행:
  ```bash
  python scripts/notify.py --signals data/quant_signals.json --dry-run   # 발송 없이 감지만
  ```
- **중복 0건**: `data/notifications_log.json` 의 `event_key` 로 막는다. 같은 신호·같은 사건은 다시 오지 않는다. 로그를 지우면 과거 알림이 다시 나갈 수 있으니 지우지 않는다.

알림이 오는 사건: `STRONG_BUY` · `EXIT`/`S3`/`S5` · `V06`(시장 급변) · 데이터·아카이브 지연(`degraded`) · **토론 실행 권장**(후보가 있는데 토론이 없을 때).

---

## 6. 장애 대응

| 증상 | 원인 후보 | 조치 |
|---|---|---|
| 화면에 "데이터 지연" | 소스 실패(`diagnostics.degraded`) | `data/health.json`·`data/pipeline_limits.json` 확인. 전일 데이터가 유지되므로 사이트는 비지 않는다 |
| 모든 종목이 `토론 없음` | `data/debate_reports.json` 미커밋 | PC 에서 토론 실행 → 커밋. 화면은 정상 상태다 |
| 토론이 계속 `지연 도착` | 주문 마감(09:25 ET) 뒤 완료 | 토론을 더 일찍 돌린다. 장전(premarket) 갱신은 창이 33~93분뿐이다 |
| `정족수 미달` 반복 | 스킬 실행 실패·한도 초과 | `debate.max_calls_per_day`·`timeout_sec` 확인, 스킬 파일 점검 |
| `3차 무효` 만 보임 | 손절 거리가 좁아 2·3차 지정가가 손절선 밖 | 정상 규칙이다(손절 후 재진입 방지). 1차 비중만 남는다 |
| 페어가 30건을 못 넘김 | 토론 실행 빈도 부족 | 판정하지 않는다. 억지로 켜지 않는다 |

---

## 7. 배포 전 체크리스트

```bash
.venv/bin/python -m pytest -q -m "not network"          # 도메인·UI 계약
.venv/bin/python -m ruff check engine scripts tests
.venv/bin/python scripts/validate_outputs.py            # 발행 레코드 불변·금지 필드 0건
.venv/bin/python scripts/build_public_data.py           # 공개 JSON 빌드(금지 키 0건)
python scripts/check_commit_scope.py --staged           # 스테이징 범위 가드
```

`build_public_data.py` 는 `web/data/debate.json`·`health.json`·`notifications.json` 을 **항상** 만든다.
토론이 0건이면 `status: "none"` 으로 만들고, 화면은 그것을 "토론 없음"으로 읽는다(파일 부재로 실패하지 않는다).
