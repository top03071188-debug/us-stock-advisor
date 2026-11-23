# Codex 스킬 3라운드 토론 — 실행 안내 (Phase 4)

기획서 v1.3.3 §3.9·§3.11·부록 D·E·G·H 구현의 사용 설명서다.
**PC의 Codex CLI**(영서 님 ChatGPT 구독 로그인, 달러 0원)와 **Codex Cloud PR 모드** 둘 다 같은 코드를 쓴다.

---

## 1. 무엇을 하는가

```text
1R 독립 분석   buffett   lynch   druckenmiller   gates_13f     ← 병렬 4콜, 서로 못 봄
                    └────────┬────────┘
2R 교차 반박   각자 타 3인의 1R을 읽고 1회 반박 + 수정 점수      ← 병렬 4콜
                    └────────┬────────┘
3R 가드·의장   risk_guard(Veto 심사) → committee_chair(종합)   ← 순차 2콜
                              ▼
              data/debate_reports.json (종목 단위 즉시 append)
```

- **계산은 파이썬, 해석만 AI**다. 스킬이 말할 수 있는 숫자는 `data/debate/context/{TICKER}.json` 안에 있는 값뿐이다.
- 1R은 서로의 결과를 보지 못한다(독립성). 2R은 **1회만** 한다(수렴 압력·예산 방지).
- 3R 의장은 새 근거를 도입할 수 없고, 정량 등급보다 **올리는** 권고를 할 수 없다.
- 토론은 신호를 삭제하지 않는다. 코드 Veto는 그대로 남고, 의장 권고는 배지·기록에만 붙는다
  (`auto_downgrade_enabled` 기본 **false**).

파일 지도:

| 경로 | 무엇 | 커밋 |
|---|---|---|
| `codex_debate/skills/*.md` | 페르소나 6종(부록 H 형식) | O |
| `codex_debate/prompts/*.md` | 라운드 프롬프트 4종 | O |
| `codex_debate/schemas/*.schema.json` | 라운드 4종 + 리포트 파일 1종 | O |
| `codex_debate/context.py` | 입력 패킷·본문 발췌·인젝션 탐지 | O |
| `codex_debate/validate.py` | 스키마·인용·숫자·가격 검증, 뉴스 3상태, 리포트 조립 | O |
| `codex_debate/executors.py` | Codex CLI / OpenAI 어댑터 | O |
| `codex_debate/run_debate.py` | 오케스트레이터(CLI) | O |
| `data/debate/context/{TICKER}.json` | 입력 패킷(로컬 전용, .gitignore) | **X** |
| `out/rounds/*.json` | `codex exec -o` 출력(로컬 전용) | **X** |
| `data/debate/usage.json` | 1일 호출 수·월 API 비용 원장 | **X** |
| `data/debate_reports.json` | 토론 리포트(공개 계약, 인용 원문 없음) | O |

---

## 2. 준비 (PC, 한 번만)

```bash
# 1) Codex CLI 설치·로그인 (ChatGPT 구독 계정)
codex --version
codex login

# 2) 저장소·가상환경
cd ~/us-stock-advisor
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock

# 3) 출력 스키마 옵션이 있는지 확인 (버전에 따라 다르다)
codex exec --help | grep -E "output-schema|ephemeral"
```

`codex exec`에 `--output-schema`·`--ephemeral`이 없으면 CLI가 오래된 것이다(`npm i -g @openai/codex@latest`).
설치·로그인이 어려우면 §9의 API 어댑터로 대체할 수 있다(월 $1~2, 하드 캡 $5).

---

## 3. 매일 실행 (PC)

```bash
cd ~/us-stock-advisor
. .venv/bin/activate

# 1) 먼저 무엇을 돌릴지 확인 (호출 0회)
python -m codex_debate.run_debate --dry-run --top 6

# 2) 실제 실행 (기본: 신호 후보 상위 6종목, Codex CLI)
python -m codex_debate.run_debate --top 6
```

`python codex_debate/run_debate.py …`처럼 **파일 경로로 직접 실행해도** 된다(스크립트가 저장소 루트를
모듈 경로에 넣는다). 아래 예시는 `python -m` 형태로 적는다.

정상이면 마지막에 이런 요약이 찍힌다.

```json
{"status": "complete", "done": ["MSFT", "NVDA"], "failed": [], "skipped": [],
 "calls": 20, "reports_path": "data/debate_reports.json", "errors": [], "committed": true}
```

종목 1개 = **10콜**(1R 4 + 2R 4 + 3R 2)이다. `--tickers MSFT`로 1종목만 돌려 흐름을 확인하는 것이 첫 단계다.

### 옵션

| 옵션 | 뜻 |
|---|---|
| `--tickers MSFT,NVDA` | 지정 종목만(신호·워치리스트 안에 있어야 한다) |
| `--top N` | 신호 점수 상위 N종목 |
| `--provider codex\|openai` | 기본 `codex`(구독, 0원), `openai`는 API 키 경로 |
| `--resume` | 같은 `as_of`의 `partial`/`stopped_by_limit` 리포트를 2R/3R부터 이어서 |
| `--no-git` | `git pull`·commit·push를 모두 하지 않는다(테스트용) |
| `--no-push` | 커밋만 하고 push는 하지 않는다(Codex Cloud PR 모드) |
| `--dry-run` | 대상·예상 호출 수만 출력(호출 0회) |
| `--validate-only` | `data/debate_reports.json`만 검증하고 끝낸다(exit 0/2) |
| `--fetch-body` / `--no-fetch-body` | 기사 원문 본문 발췌 여부(기본 **켜짐**) |
| `--max-tickers`, `--max-calls-per-run`, `--max-calls-per-day` | 상한(기본 6 / 80 / 120) |
| `--model` | Codex/OpenAI 모델 지정(기본값 사용 권장) |
| `--timeout` | 콜당 타임아웃(기본 180초) |
| `--deadline` | **전체 시간 제한**(ISO8601). 없으면 신호의 `order_cutoff_at` − 5분 |
| `--deadline-margin` | 주문 마감 몇 초 전에 멈출지(기본 300초) |

### 본문 발췌(`--fetch-body`)에 대해

뉴스 인용 검증은 **본문 발췌**가 있어야 통과한다. 본문을 받지 않으면(`--no-fetch-body`) 스킬이 인용할
문장이 없어 유효 item이 0건이 되고, 그 종목의 뉴스 상태가 자동으로 **`근거 부족`(insufficient_evidence)**
이 된다(신뢰도 C 상한, 등급 상한 WATCH). 즉 **뉴스를 실제로 판독하려면 본문을 받아야 한다.**
본문 발췌는 `data/debate/context/`(로컬, gitignore)에만 남고 커밋·배포되지 않는다.

---

## 4. 실행 흐름에서 실제로 일어나는 일

1. **`git pull --ff-only`** — 실패하면 **시작하지 않는다**(남의 변경과 섞이면 기록이 오염된다).
   `--no-git`이면 생략.
2. **대상 선별** — `--tickers` → 신호 후보(점수 순) → **유효 뉴스 창**(기본 48시간, 주말·휴장 다음 날은
   직전 거래일 하루를 포함하도록 72/96시간으로 자동 확장) 내 뉴스 3건 이상 → 오늘 신규 8-K.
   워치리스트·신호 밖 종목은 넣을 수 없다.
3. **`context.json` 생성** — 숫자는 여기서만 만들어진다. 기사 본문·헤드라인에서 인젝션 패턴이 나오면
   그 **기사 하나만** 격리하고(`excluded_news` 에 ID·사유·패턴 기록) **나머지 유효 기사로 판단을 계속한다**.
   격리 뒤 유효 근거가 2건 미만이면 뉴스 상태가 `insufficient_evidence` 로 내려가고(신뢰도 C·등급 WATCH 상한),
   공시·자유 텍스트 같은 **비뉴스 필드**까지 침투한 경우에만 `NEWS_INJECTION`(V11)으로 종목 전체를 차단한다.
4. **1R → 2R → 3R** — 스킬 1개 = `codex exec` 1회. `--ephemeral`이라 라운드 간 대화 기록이 남지 않는다.
5. **검증** — 스키마·enum·점수 범위, 뉴스 ID·인용 대조, 창작 숫자/가격, 금지 필드, 정족수.
6. **종목 단위 즉시 저장** — `data/debate_reports.json`에 append(중간 실패 대비).
7. **파일 전체 검증 통과 시에만** commit & push. 실패하면 로컬 파일만 남기고 끝난다.

### 시각 3종과 `late`/`invalid`

```text
data_cutoff_at(07:00 KST) ≤ signal_published_at(07:02) ≤ completed_at ≤ report_validated_at < order_cutoff_at(23:25 KST)
```

- `report_validated_at` 은 **이 프로세스가 결과를 받아 검증한 시각**이다(모델이 스스로 적는 `completed_at` 과 다르다).
  리포트에는 스키마 제약 때문에 **`validation.report_validated_at`** 에 기록된다 — 편입 자격 판정도 이 값을 읽는다.
- `max(completed_at, report_validated_at) ≥ order_cutoff_at` → **`late: true`**: 저장은 하되 그날 주문 결정에 쓰지 않는다.
  (완료는 마감 전인데 검증이 마감 뒤인 경우도 늦다 — 감사 P0-2.)
- `completed_at < signal_published_at` → **`invalid: true`**: 그 결정에 붙이지 않는다.
- 장전 갱신(21:52) 창은 짧다. 그 안에 못 끝낸 토론은 `late`로 남고 다음 스냅샷 입력이 된다.
- **전체 시간 제한**: `--deadline`(또는 기본 `order_cutoff_at − 5분`)을 넘으면 남은 종목을 시작하지 않고
  `status: stopped_by_deadline` 으로 멈춘다. 마감을 넘겨 끝난 토론은 그 결정에 쓰이지 않는다.

---

## 5. 검증 규칙 (코드가 강제)

| 검증 | 규칙 | 실패 시 |
|---|---|---|
| 스키마 | `codex_debate/schemas/round*.schema.json` | 그 스킬 결과 폐기(재시도 후) |
| 점수·신뢰도 | `-100 ≤ score ≤ 100`, `0 ≤ confidence ≤ 1` | 클램프 + 신뢰도 C 상한 |
| 뉴스 ID | `context.json`의 기사 목록에 없으면 | item 폐기 |
| 인용 | `body_excerpt_1500`에 글자 그대로(공백·NFC 정규화) | item 폐기 |
| 숫자 | `context.json`에 없는 수치가 문장에 있으면 | 그 문장 삭제, 3회 이상이면 스킬 폐기 |
| 가격 | `entry_plan_refs`·`exit_refs`는 `allowed.prices` 원소만 | 그 가격 폐기(전부 창작이면 의장 결과 폐기) |
| 금지 필드 | `quote`·`target_price`·`veto_override` 등 | **파일 전체 거부** |
| 정족수 | 유효 1R 스킬 3개 미만 | `status: quorum_failed`, 등급 권고 미적용 |
| 뉴스 상태 | 유효 item < 2 **또는** 폐기율 > 30% **또는** `unknown` ≥ 50% | `insufficient_evidence`(C 상한, WATCH 상한, V12 미발동) |
| 인젝션(기사) | 본문·헤드라인에 지시문 패턴 | 그 **기사만** 격리(`excluded_news`) — 판단은 계속 |
| 인젝션(비뉴스) | 공시 항목·자유 텍스트에 지시문 침투 | `NEWS_INJECTION`(V11) — 종목 전체 차단 |

스키마 파일에는 `-100 ≤ score ≤ 100`·`quote ≤ 200자` 같은 제약이 문서처럼 적혀 있고, `codex exec`에는
strict 모드가 받지 않는 검증 키워드를 걷어낸 **사본**(`out/rounds/*.strict.schema.json`)을 넘긴다.
그 제약은 어차피 **코드가 마지막에** 다시 강제한다(클램프·200자 절단·개수 제한).

**저장용 리포트에는 인용 원문이 없다** — `quote_sha256`·`quote_verified`·`quote_chars`만 남는다
(`data/debate_reports.json`은 공개 저장소에 커밋된다).

뉴스 상태 3종:

| 상태 | 조건 | 화면 |
|---|---|---|
| `not_read` | 판독 미실행 또는 **유효 뉴스 창**(48h·휴장 보정) 내 기사 0건 | 회색 "뉴스 미판독" |
| `insufficient_evidence` | 유효 < 2건 / 폐기율 > 30% / `unknown` ≥ 50% | 주황 "근거 부족" |
| `read_done` | 유효 ≥ 2건 & 폐기율 ≤ 30% & `unknown` < 50% | "판독 완료 · 기사 N건" |

---

## 6. 호출 예산·한도·재개

```text
1종목 = 10콜, 기본 상한 = 1회 80콜 / 1일 120콜
초과 시 = 그 실행을 멈추고 status: stopped_by_limit 로 저장(다음 종목부터 건너뛴다)
```

- 5시간·주간 사용량 한도에 닿으면 **그 실행만** 멈춘다. 이미 끝난 라운드·종목은 저장돼 있다.
- 다음 실행에서 `--resume`을 붙이면 같은 `as_of`의 `partial`/`stopped_by_limit` 리포트를 이어서
  **남은 라운드만** 호출한다(성공한 1R은 다시 부르지 않는다).
- **재개 키**: `signal_id + snapshot_id + context_hash + prompt_version + model_version` 의 해시를
  리포트의 `validation.resume_key` 에 남긴다. 재개할 때 키가 다르면(입력·프롬프트·모델이 바뀌면)
  이전 라운드를 재사용하지 않고 새 리포트로 시작한다 — 다른 입력의 1R을 2R에 섞지 않는다.
- 로그인 만료는 재시도하지 않고 즉시 멈춘다(`status: auth_expired`, `codex login` 후 재실행).
- 1일 사용량은 `data/debate/usage.json`에 날짜별로 남는다(로컬 전용).

```bash
python -m codex_debate.run_debate --resume --top 6     # 어제/오늘 끊긴 것 이어서
```

---

## 7. Codex Cloud PR 모드

PC를 켜 둘 수 없을 때는 Codex Cloud에서 이 저장소를 열어 같은 명령을 실행하고 PR로 올린다.

```bash
python -m codex_debate.run_debate --top 6 --no-push    # 커밋만, push는 Cloud가 PR로 처리
```

- Cloud 샌드박스에서도 `codex login`(또는 Cloud의 Codex 자체 실행)이 필요하다. 인증이 없으면
  `status: auth_expired`로 끝나고 아무것도 커밋하지 않는다.
- 수집된 기사 본문(`data/debate/context/`)은 `.gitignore`라 PR에 포함되지 않는다. **PR에 본문·인용 원문이
  들어가면 안 된다** — 그래서 리포트에는 해시만 남긴다.
- PR 검사는 **등록된** `.github/workflows/debate-validate.yml`이 한다(§10). 저장소의
  `codex_debate/workflows/debate-validate.yml`은 **같은 내용의 사본**이고, 사본이 어긋나면 테스트가 실패한다.
  이 워크플로는 PR 이 건드린 **전체 경로**를 공개 범위 가드로 검사하고, 리포트 PR 이
  `data/debate_reports.json`·`data/debate/manifest.json` 외 경로(코드·규칙·워크플로)를 건드리면 차단한다.

---

## 8. 결과 파일 계약 (`data/debate_reports.json`)

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-11-20T08:16:02+09:00",
  "codex": {"version": "0.52.0", "model": "default"},
  "inputs": {"quant_signals_commit": "a1b2c3d", "as_of": "2026-11-20",
             "context_hash": "sha256:…", "prompt_hash": "sha256:…"},
  "reports": [
    {"report_id": "2026-11-20-MSFT-debate-01", "ticker": "MSFT",
     "signal_id": "…", "snapshot_id": "sha256:…", "as_of": "2026-11-20",
     "data_cutoff_at": "…", "signal_published_at": "…", "order_cutoff_at": "…",
     "completed_at": "…", "late": false, "invalid": false, "status": "complete",
     "round1": [{"skill": "buffett", "score": 62, "stance": "buy", "confidence": 0.71,
                 "reasons": ["…"], "news_read": [{"news_id": "…", "durability": "structural",
                 "quote_sha256": "…", "quote_verified": true, "quote_chars": 64}]}],
     "round2": [{"skill": "buffett", "score": 48, "changed": true, "rebuttals": [], "conflict_points": []}],
     "round3": {"guard": {"veto_codes": [], "additional_blocks": [], "structural_risk": 58,
                          "reasoning": "…", "opinion": "caution"},
                "chair": {"consensus": ["…"], "dissent": ["…"], "grade_recommendation": "WATCH",
                          "entry_plan_refs": [383.5], "exit_refs": [522.0], "conflict": true,
                          "one_line_ko": "…"}},
     "scores": {"buffett": {"round1": 62, "round2": 48}},
     "validation": {"skills_in": 6, "skills_kept": 6, "quotes_checked": 3, "quotes_dropped": 0,
                    "numbers_checked": 12, "numbers_rejected": 0,
                    "report_validated_at": "…", "resume_key": "sha256:…",
                    "injection_scope": null, "injection_blocking": false, "excluded_news": []},
     "quant_grade": "BUY", "display_grade": "BUY", "intent": "pending_hold_cash",
     "auto_downgrade_applied": false, "track": "codex_debated",
     "news_state": "read_done", "structural_risk": 58}
  ]
}
```

- `status` ∈ `complete`·`partial`·`failed`·`quorum_failed`·`stopped_by_limit`
- `intent` ∈ `pending_buy`·`pending_hold_cash`·`no_action` (체결 `action`은 이 파일에 넣지 않는다)
- `auto_downgrade_enabled: false`인 동안 `display_grade == quant_grade`여야 한다(검증이 거부).
- 손으로 고치지 말 것. 고쳐야 하면 `python -m codex_debate.run_debate --validate-only`로 먼저 확인한다.

---

## 9. API 어댑터 (PC 없이 돌릴 때만)

```bash
export LLM_API_KEY=sk-...            # Actions Secret 또는 로컬 .env
export MAX_MONTHLY_USD=5             # 하드 캡(코드가 5 달러로 다시 자른다)
python -m codex_debate.run_debate --provider openai --top 6
```

- 같은 스키마를 `response_format.json_schema(strict)`로 강제하고, 받은 뒤 코드가 다시 검증한다.
- 월 누적 비용은 `data/debate/usage.json`에 남고, 상한을 넘으면 그 실행을 **시작하지 않는다**.
- 키가 없으면 즉시 `ExecutorUnavailable`로 끝난다(조용히 Codex로 대체하지 않는다).

---

## 10. 검증 워크플로 — 등록·운영 상태

`.github/workflows/debate-validate.yml`은 **등록되어 실제로 도는 워크플로**다(2026-11-20 확인).
저장소의 `codex_debate/workflows/debate-validate.yml`은 같은 내용의 **사본**이다 — 이 저장소에 쓰는 CLI 토큰에는
`workflows` 권한이 없어 `.github/workflows/`로 push하면 **403으로 거부**되므로, 갱신은 GitHub 웹 편집기로 한다.

- 등록본 머리말은 운영 문구다(“이 파일은 **실제로 도는 워크플로**”). 사본과 내용이 같아야 하고,
  어긋나면 `test_debate_pr_workflow_uses_base_branch_verifier` 가 실패한다.
- 내용을 바꿀 때는 **둘 다** 같은 내용으로 바꾸고, 웹 업로드로 `.github/workflows/` 를 갱신한다.

이 워크플로가 검사하는 것(v1.3.5 · P1-2 · 감사 10):

| 단계 | 내용 |
|---|---|
| 토론 테스트 | `python -m pytest -q tests/test_phase4_debate.py` |
| 리포트 계약 | `--validate-only` (부록 G 스키마) |
| **PR 전체 범위** | `git diff --name-only origin/<base>...HEAD` → `check_commit_scope.py --paths …` (손으로 나열하지 않는다) |
| **리포트 PR 경로 가드** | `data/debate_reports.json` 변경 시 `data/debate_reports.json`·`data/debate/manifest.json` 외 경로가 있으면 실패 |
| 본문 미커밋 | `data/debate/context` 가 추적되면 실패 |

로컬에서 같은 검사를 손으로 돌리려면:

```bash
python -m pytest -q tests/test_phase4_debate.py
python -m codex_debate.run_debate --validate-only --reports data/debate_reports.json
python scripts/check_commit_scope.py --paths data/debate_reports.json
```

---

## 11. 문제 해결

| 증상 | 원인 | 조치 |
|---|---|---|
| `codex CLI 를 찾을 수 없다` | 미설치/ PATH | `codex --version`, 없으면 설치 후 `codex login` |
| `status: auth_expired` | 로그인 만료 | `codex login` 후 `--resume` |
| `status: stopped_by_limit` | 5시간·주간 한도 또는 1회/1일 예산 | 시간을 두고 `--resume` |
| `status: stopped_by_deadline` | 전체 시간 제한(`--deadline`/문 마감 5분 전) 도달 | 남은 종목은 다음 실행에서. 입력이 같으면 `--resume` |
| `injection_scope: non_news_field` | 공시·자유 텍스트에 지시문 침투 | 그 종목은 `NEWS_INJECTION` 차단 — 원본 데이터 확인 |
| `excluded_news` 가 여러 건 | 기사 본문 지시문 격리 | 조치 필요 없음(그 기사만 빠지고 판단은 계속) |
| `schema_failed`가 반복 | 모델이 스키마를 계속 어김 | `--model` 변경, 해당 스킬만 다시 실행, 프롬프트 수정 후 테스트 |
| `news_state: insufficient_evidence` | 본문 없음/인용 실패/폐기율 초과 | `--fetch-body` 확인, 기사 품질 확인 |
| `status: quorum_failed` | 유효 1R 스킬 ≤ 2 | 스키마 실패 로그(`validation.drop_reasons`) 확인 후 재실행 |
| `git pull --ff-only 실패` | 원격 변경·충돌 | 정리 후 재실행(**강제 push 금지**) |
| `validation_failed` | 리포트 파일이 계약 위반 | `--validate-only`로 오류 확인, 이전 파일 유지 |

---

## 12. 운영 체크리스트

- [ ] `--dry-run`으로 대상·예상 콜 수(종목×10)를 먼저 본다.
- [ ] 첫 실행은 `--tickers MSFT`로 10콜 완주·스키마 100%를 확인한다.
- [ ] `completed_at`이 `order_cutoff_at` 전인지 확인한다(늦으면 `late: true`).
- [ ] `data/debate_reports.json`에 `quote` 원문이 0건인지 확인한다.
- [ ] 토론 등급은 화면 기본 등급을 바꾸지 않는다(`auto_downgrade_enabled: false`).
- [ ] `--resume`은 같은 `as_of`에서만 의미가 있다(다음 날이면 새로 돈다).
- [ ] 같은 `as_of`에서 이미 `complete`인 종목은 다시 돌지 않고 건너뛴다(`skipped`에 표시).
- [ ] 실패한 종목(`failed`·`quorum_failed`)만 다시 돌리려면 `--tickers`로 지정해 실행한다.
