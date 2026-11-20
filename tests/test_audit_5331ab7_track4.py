"""test_audit_5331ab7_track4 — 감사 `5331ab7` 트랙 4(09 · 10 + 부수 ①③⑤) 회귀 테스트.

검사 항목
- **09 + 부수 ①③** 정적 데이터 200KiB
  · 크기 검사는 **실제로 게시되는 바이트**(`dumps` = indent=2)로 한다 — compact 통과/실파일 초과를 막는다.
  · `debate.json` 은 요약만 담고 라운드 원문은 `debate_details/{report_id}.json` 으로 나간다.
    43종목 × 수십 건이 쌓여도 200KiB 를 넘지 않고, 상세 파일도 각각 200KiB 미만이다.
  · 작은 배포는 예전처럼 **인라인** 라운드를 유지한다(구 화면·샘플과의 호환).
  · 종목 상세의 `debate` 행은 `snapshot_matched` 로 "오늘 신호의 스냅샷"인지 밝힌다.
- **10** 공개 가드·스키마·CI 신뢰 경계
  · 스키마 예외는 **최상위** `schemas/`·`codex_debate/schemas/` 의 `*.schema.json` 에만, `$schema` 는
    정식 JSON Schema URL 일 때만. `docs/schemas/…` 같은 경로 중간 일치는 예외가 아니다.
  · 금지 키 면제는 **필드 이름 선언 자리**(`properties`/`$defs`/`definitions` 바로 아래)에서만.
  · `validation` 객체는 `additionalProperties: false` 로 닫혀 있고, 실제 데이터는 그대로 통과한다.
  · `debate-validate.yml` 은 **기본 브랜치**의 의존성·검증기로 돌고, PR 에서는 리포트 **단일 blob** 만 읽는다.
- **build_public_data** 는 상세 파일을 매니페스트·금지 키·크기 검사에 똑같이 태운다.

이 파일은 트랙 4 의 소유 파일만 건드리지 않는다(엔진·원장은 다른 트랙의 몫이다).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from codex_debate import validate as debate_validate
from codex_debate.validate import load_schema, report_receipt_sha256, schema_errors
from scripts import build_public_data as bpd
from scripts import check_commit_scope as guard

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 200 * 1024
TICKERS = [f"T{i:02d}" for i in range(43)]


# ────────────────────────────── 공용 픽스처 ──────────────────────────────
def _rules() -> dict:
    return bpd.load_allowlist()


def _signals() -> dict:
    return json.loads((ROOT / "data" / "quant_signals.json").read_text(encoding="utf-8"))


def _real_report() -> dict:
    return json.loads((ROOT / "data" / "debate_reports.json").read_text(encoding="utf-8"))["reports"][0]


def _reports(tickers: list[str], per_ticker: int) -> list[dict]:
    """실제 GOOGL 리포트 본문을 종목·회차만 바꿔 **수십 건**으로 불린다(감사 09 재현과 같은 방식)."""
    template = _real_report()
    reports: list[dict] = []
    for ticker in tickers:
        for index in range(1, per_ticker + 1):
            report = copy.deepcopy(template)
            report["report_id"] = f"2026-11-{9 + index:02d}-{ticker}-debate-{index:02d}"
            report["ticker"] = ticker
            report["signal_id"] = f"2026-11-{9 + index:02d}-{ticker}-BUY-001"
            report["completed_at"] = f"2026-11-{9 + index:02d}T13:5{index % 10}:00Z"
            reports.append(report)
    return reports


@pytest.fixture(scope="module")
def wide_build() -> dict:
    """43종목 × 10건 = 430리포트 — 감사 09 가 500KB 를 만들던 모양."""
    return bpd.build(_signals(), None, [], _rules(), debate_reports={"reports": _reports(TICKERS, 10)})


def _detail_files(out: dict) -> dict[str, dict]:
    return {name: payload for name, payload in out.items() if name.startswith("debate_details/")}


def _detail_of(row: dict) -> str | None:
    path = row.get("detail_path")
    return path.replace("data/", "", 1) if path else None


# ══════════════════════════ 09-A. 실바이트 크기 검사 ══════════════════════════
def test_published_size_is_the_exact_bytes_we_write():
    """감사 09 추가 결함: 검사는 compact, 파일은 indent=2 였다 — 둘을 같은 값으로 묶는다."""
    payload = bpd.build_ticker(_signals(), "GOOGL") if "GOOGL" in json.dumps(_signals()) else {
        "rows": [{"ticker": f"T{i}", "nested": {"a": list(range(12))}} for i in range(60)]}
    written = bpd.dumps(payload).encode("utf-8")
    compact = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert bpd._json_size(payload) == len(written), "크기 검사가 실제 파일 바이트와 다르다"
    assert bpd._json_size(payload) > len(compact), "indent=2 파일은 compact 보다 크다"


def test_build_refuses_a_file_that_only_fits_when_compact():
    """compact 로는 200KiB 미만인데 실제 파일은 넘는 문서는 **빌드 실패**해야 한다."""
    signals = {
        "as_of": "2026-11-20", "status": "normal",
        "signals": [{"ticker": "AAA", "signal_id": "s1", "decision": "WATCH"}],
        # 들여쓰기에서만 커지는 문서(원소가 짧은 긴 배열) — 실제 사고 모양과 같다.
        "charts": {"AAA": {"basis": "split_adj_index_base100", "labels": ["2026-01-01"],
                           "index": [100.0 + i / 10 for i in range(16000)]}},
    }
    payload = bpd.build_ticker(signals, "AAA")
    compact = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert compact < MAX_BYTES, "전제: compact 로는 통과하던 크기"
    assert bpd._json_size(payload) > MAX_BYTES, "전제: 실제 파일은 200KiB 초과"
    with pytest.raises(SystemExit) as excinfo:
        bpd.build(signals, None, [], _rules())
    assert "200KB" in str(excinfo.value)


def test_written_files_match_the_manifest_and_stay_under_200kib(tmp_path):
    out = tmp_path / "web"
    code = bpd.main(["--out", str(out)])
    assert code == 0
    manifest = json.loads((out / "deploy_manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["files"].items():
        raw = (out / name).read_bytes()
        assert len(raw) <= MAX_BYTES, f"{name}: {len(raw)} bytes — 200KB 제한(5.6)"
        assert entry["size"] == len(raw), f"{name}: 매니페스트 size 가 실제 파일과 다르다"
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest(), f"{name}: 해시 불일치"


# ═════════════════════ 09-B. 토론 분리 · 목록 예산 ═════════════════════
def test_debate_rows_are_summaries_that_point_to_detail_files(wide_build):
    debate = wide_build["debate.json"]
    assert bpd._json_size(debate) <= MAX_BYTES, "43종목 × 10건에서 debate.json 이 200KiB 를 넘었다"
    assert debate["counts"]["reports"] == 430
    assert len(debate["reports"]) == bpd.MAX_DEBATE_REPORTS, "목록은 상한까지만 싣는다"
    assert len(debate["by_ticker"]) == len(TICKERS), "종목별 요약은 상한에 잘리면 안 된다"
    for row in debate["reports"] + list(debate["by_ticker"].values()):
        for field in ("round1", "round2", "round3"):
            assert field not in row, f"{field} 가 요약 행에 인라인으로 남았다"
        assert row["detail_path"].startswith("data/debate_details/")
        assert row["one_line_ko"], "의장 한 줄은 요약에 남아야 화면이 비지 않는다"


def test_every_detail_path_resolves_to_a_complete_and_manifested_file(wide_build):
    debate = wide_build["debate.json"]
    details = _detail_files(wide_build)
    manifest = wide_build["deploy_manifest.json"]
    assert details, "상세 파일이 하나도 없다"
    for row in debate["reports"] + list(debate["by_ticker"].values()):
        name = _detail_of(row)
        assert name in details, f"{name} 가 상세 파일로 만들어지지 않았다"
    for name, payload in details.items():
        assert bpd._json_size(payload) <= MAX_BYTES, f"{name} 이 200KiB 를 넘었다"
        assert payload["round1"] and payload["round3"]["chair"]["one_line_ko"], f"{name}: 원문이 비었다"
        bpd.assert_no_forbidden(payload, _rules(), name)      # 인용 원문·계좌 정보 없음
        assert name in manifest["files"], f"{name} 이 배포 매니페스트에 없다"
        digest = hashlib.sha256(bpd.dumps(payload).encode("utf-8")).hexdigest()
        assert manifest["files"][name]["sha256"] == digest
    for core in ("debate.json", "meta.json"):
        assert core in bpd.CORE_PUBLIC_FILES
    assert not any(item.startswith("debate_details/") for item in bpd.CORE_PUBLIC_FILES), \
        "상세는 핵심 세트가 아니다(종목 화면에서 처음 필요해진다)"


def test_list_budget_drops_oldest_but_says_so(wide_build):
    debate = wide_build["debate.json"]
    assert len(debate["reports"]) == bpd.MAX_DEBATE_REPORTS
    assert "오래된 390건" in (debate["note_ko"] or "")
    assert debate["status"] == "ok"


def test_small_debate_keeps_inline_rounds_for_compatibility():
    """한두 건짜리 배포는 예전과 **같은 모양**(인라인 라운드)을 유지한다 — 화면·샘플 호환."""
    out = bpd.build(_signals(), None, [], _rules(),
                    debate_reports={"reports": _reports(["AAA"], 1)})
    row = out["debate.json"]["reports"][0]
    assert row["round1"] and row["round3"]["chair"], "작은 배포에서 인라인 원문이 사라졌다"
    assert row["detail_path"] == "data/debate_details/2026-11-10-AAA-debate-01.json"
    assert _detail_of(row) in _detail_files(out), "인라인이어도 상세 파일은 있어야 한다"


# ═════════════════════ 부수 ③. 스냅샷 대조(snapshot_matched) ═════════════════════
def _debate_row(signal_id: str, snapshot_id: str, one_line: str = "의장 한 줄") -> dict:
    return {"report_id": "R1", "ticker": "AAA", "signal_id": signal_id, "snapshot_id": snapshot_id,
            "badge": "토론 완료", "quant_grade": "BUY", "display_grade": "BUY", "recommendation": "BUY",
            "one_line_ko": one_line, "validation": {"quotes_checked": 1, "quotes_dropped": 0}}


def test_ticker_debate_row_marks_snapshot_mismatch():
    signals = {"as_of": "2026-11-20", "snapshot_id": "sha256:new",
               "signals": [{"ticker": "AAA", "signal_id": "2026-11-20-AAA-BUY-001", "decision": "BUY"}]}
    same = bpd.build_ticker(signals, "AAA", None, _debate_row("2026-11-20-AAA-BUY-001", "sha256:new"))
    assert same["debate"]["snapshot_matched"] is True
    assert same["debate_display"]["one_line_ko"] == "의장 한 줄", "요약 행의 의장 한 줄을 읽어야 한다"

    stale = bpd.build_ticker(signals, "AAA", None, _debate_row("2026-11-19-AAA-BUY-001", "sha256:old"))
    assert stale["debate"]["snapshot_matched"] is False, "구 스냅샷 리포트를 오늘 확정 토론으로 오인하면 안 된다"

    unknown = bpd.build_ticker({"as_of": "2026-11-20", "signals": []}, "AAA", None,
                               _debate_row("", ""))
    assert unknown["debate"]["snapshot_matched"] is False, "대조할 식별자가 없으면 단정하지 않는다"


# ═════════════════════ 10-A. 스키마 예외(경로·값·선언 자리) ═════════════════════
#: 스키마 안에서 금지 키(`quote`)가 **필드 이름**으로만 나오는 본문 — 예외 판정 표본이다.
_SCHEMA_BODY = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "news": {
            "type": "array",
            "items": {"properties": {"quote": {"type": "string"}}},
        }
    },
}


def _write(tmp_path: Path, rel: str, payload) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("rel", ["schemas/x.schema.json", "codex_debate/schemas/x.schema.json"])
def test_schema_exemption_applies_to_approved_top_level_paths(tmp_path, rel):
    path = _write(tmp_path, rel, _SCHEMA_BODY)
    assert guard.check_paths([str(path)], _rules(), root=tmp_path) == []


@pytest.mark.parametrize("rel", ["docs/schemas/x.schema.json",
                                "assets/vendor/schemas/x.schema.json",
                                "schemas/nested/x.schema.json"])
def test_schema_exemption_does_not_match_a_path_in_the_middle(tmp_path, rel):
    """경로 중간 일치(`docs/schemas/…`)·하위 폴더는 스키마 예외가 아니다(감사 10 · T4)."""
    path = _write(tmp_path, rel, _SCHEMA_BODY)
    problems = guard.check_paths([str(path)], _rules(), root=tmp_path)
    assert any("forbidden keys" in p and "quote" in p for p in problems), problems


@pytest.mark.parametrize("declared", [1, "1", "not-a-schema-url", None, "http://example.com/schema"])
def test_schema_exemption_requires_a_real_json_schema_url(tmp_path, declared):
    payload = dict(_SCHEMA_BODY)
    payload["$schema"] = declared
    path = _write(tmp_path, "schemas/x.schema.json", payload)
    problems = guard.check_paths([str(path)], _rules(), root=tmp_path)
    assert any("forbidden keys" in p for p in problems), problems


def test_forbidden_key_is_exempt_only_where_a_field_name_is_declared(tmp_path):
    """이름 자리(`properties.quote`)만 예외 — 값 자리(`default`·`examples`·임의 객체)는 데이터다."""
    declared = _write(tmp_path, "schemas/ok.schema.json", _SCHEMA_BODY)
    assert guard.check_paths([str(declared)], _rules(), root=tmp_path) == []

    value_position = _write(tmp_path, "schemas/value.schema.json", {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": {"signal": {"type": "object", "default": {"properties": {"avg_cost": 3.2}}}}})
    problems = guard.check_paths([str(value_position)], _rules(), root=tmp_path)
    assert any("avg_cost" in p for p in problems), "`default` 안의 값 자리는 면제되면 안 된다"

    examples = _write(tmp_path, "schemas/examples.schema.json", {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": {"a": {"type": "string"}}, "examples": [{"properties": {"shares": 3}}]})
    assert any("shares" in p for p in guard.check_paths([str(examples)], _rules(), root=tmp_path))

    # 선언 자리가 아닌 컨테이너(패턴 이름)는 예외를 주지 않는다 — 예전에는 면제됐다.
    patterns = _write(tmp_path, "schemas/pattern.schema.json", {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "patternProperties": {"quote": {"type": "string"}}})
    assert any("quote" in p for p in guard.check_paths([str(patterns)], _rules(), root=tmp_path))


def test_units_exemption_stays_narrow(tmp_path):
    xbrl = {"facts": {"us-gaap": {"Shares": {"units": {"shares": [{"val": 1}]}}}}}
    assert guard.check_paths([str(_write(tmp_path, "facts.json", xbrl))], _rules()) == []
    account = {"position": {"ticker": "AAA", "shares": 3.2}}
    assert any("shares" in p for p in
               guard.check_paths([str(_write(tmp_path, "account.json", account))], _rules()))


def test_repository_report_schema_and_workflow_pass_the_guard():
    paths = ["data/debate_reports.json", "codex_debate/schemas/debate_reports.schema.json",
             ".github/workflows/debate-validate.yml", "codex_debate/workflows/debate-validate.yml"]
    existing = [p for p in paths if (ROOT / p).exists()]
    assert existing
    assert guard.check_paths(existing, _rules(), root=ROOT) == []


# ═════════════════════ 10-B. `validation` 닫기 ═════════════════════
def test_validation_object_is_closed():
    """자유 키였던 `validation` 에 기사 문장을 넣고 재서명해도 계약이 막는다(감사 10)."""
    doc = json.loads((ROOT / "data" / "debate_reports.json").read_text(encoding="utf-8"))
    bad = copy.deepcopy(doc)
    bad["reports"][0]["validation"]["memo"] = "기사 문장을 그대로 옮겨 적은 자유 필드"
    errors = schema_errors(bad, load_schema("debate_reports"))
    assert any("validation" in e and "memo" in e for e in errors), errors
    # 영수증을 다시 찍어도(자기 체크섬 통과) 스키마가 막는다.
    bad["reports"][0]["validation"]["report_sha256"] = report_receipt_sha256(bad["reports"][0])
    assert any("memo" in e for e in debate_validate.validate_reports_file(bad))


def test_real_reports_file_passes_the_closed_schema():
    """실데이터(`data/debate_reports.json`)가 쓰는 필드는 정확히 허용 목록 안에 있다."""
    path = ROOT / "data" / "debate_reports.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert schema_errors(doc, load_schema("debate_reports")) == []
    assert debate_validate.validate_reports_file(path) == []
    used = set()
    for report in doc.get("reports") or []:
        used |= set((report.get("validation") or {}).keys())
    schema_keys = set(load_schema("debate_reports")["$defs"]["report"]
                      ["properties"]["validation"]["properties"])
    assert used <= schema_keys, f"스키마에 없는 validation 필드: {sorted(used - schema_keys)}"
    assert {"report_validated_at", "resume_key", "round_state", "article_receipts"} <= schema_keys


def test_report_root_allows_provenance_from_the_assembler():
    """T3 조립기가 리포트 루트에 남기는 `provenance` 를 스키마가 받아들인다."""
    doc = json.loads((ROOT / "data" / "debate_reports.json").read_text(encoding="utf-8"))
    out = copy.deepcopy(doc)
    report = out["reports"][0]
    report["provenance"] = {"report_id": report["report_id"], "context_sha256": "sha256:" + "a" * 8,
                            "round_hashes": {"round1": "sha256:bb"}, "codex_version": "test"}
    report["validation"]["report_sha256"] = report_receipt_sha256(report)
    assert schema_errors(out, load_schema("debate_reports")) == []


# ═════════════════════ 10-C. 워크플로 신뢰 경계 ═════════════════════
def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "debate-validate.yml")
                          .read_text(encoding="utf-8"))


def _steps(doc: dict) -> list[dict]:
    return doc["jobs"]["validate"]["steps"]


def _step(doc: dict, needle: str) -> dict:
    for step in _steps(doc):
        if needle in str(step.get("name") or ""):
            return step
    raise AssertionError(f"'{needle}' 단계가 없다: {[s.get('name') for s in _steps(doc)]}")


def test_workflow_copies_stay_identical():
    registered = (ROOT / ".github" / "workflows" / "debate-validate.yml").read_text(encoding="utf-8")
    copy_path = ROOT / "codex_debate" / "workflows" / "debate-validate.yml"
    assert copy_path.read_text(encoding="utf-8") == registered


def test_workflow_installs_and_verifies_from_the_base_branch_only():
    doc = _workflow()
    install = _step(doc, "Install")
    assert "/tmp/trusted/requirements.lock" in install["run"], "의존성은 기본 브랜치 잠금으로 설치한다"
    assert "requirements.lock" not in json.dumps(install.get("uses", "")), install
    for step in _steps(doc):
        run = str(step.get("run") or "")
        if "pip install" in run:
            assert "/tmp/trusted/requirements.lock" in run, f"{step.get('name')}: PR 잠금을 설치한다"
        assert "checkout" not in run or "git checkout" not in run, step.get("name")

    # 검증기는 기본 브랜치 사본 — PR 코드의 검사기·허용 목록을 쓰지 않는다.
    trusted = _step(doc, "Trusted verifier")
    assert "git archive" in trusted["run"] and "origin/" in trusted["run"]
    assert "/tmp/trusted" in trusted["run"]

    scope = _step(doc, "Public scope")
    assert "/tmp/trusted/scripts/check_commit_scope.py" in scope["run"]
    assert "--allowlist /tmp/trusted/config/public_allowlist.yaml" in scope["run"]
    assert '--root "$GITHUB_WORKSPACE"' in scope["run"]
    assert "--paths" in scope["run"]

    contract = _step(doc, "Reports contract")
    assert "cd /tmp/trusted" in contract["run"], "계약 검증도 기본 브랜치 코드로 돌린다"

    tests = _step(doc, "Debate tests")
    assert "/tmp/trusted" in tests["run"] and "tests/test_phase4_debate.py" in tests["run"]


def test_workflow_reads_only_the_pr_report_blob():
    doc = _workflow()
    extract = _step(doc, "Extract PR report blob")
    run = extract["run"]
    assert "git show" in run, "PR 은 **단일 blob** 으로만 읽는다"
    assert "PR_SHA" in json.dumps(extract.get("env") or {}), extract.get("env")
    assert "steps.changed.outputs.pr_sha" in json.dumps(extract.get("env") or {})
    assert "refs/pull/" in _step(doc, "Collect changed paths")["run"], "PR head 를 명시적으로 받는다"
    assert "data/debate_reports.json" in run
    # 꺼내는 파일은 **그 둘뿐**이다(루프 목록이 곧 허용 목록이다).
    assert re.search(r"for f in data/debate_reports\.json data/debate/manifest\.json; do", run), run
    show_lines = [line for line in run.splitlines() if "git show" in line]
    assert len(show_lines) == 1 and "${f}" in show_lines[0], show_lines
    assert "tar" not in run and "checkout" not in run
    env = doc["jobs"]["validate"].get("env") or {}
    assert "base_ref" in json.dumps(env) or any("base_ref" in str(v) for v in env.values())


# ═════════════════════ 09-C. 화면 · 서비스워커 ═════════════════════
_UI_HARNESS = r"""
globalThis.__server = {};
globalThis.__nodes = {};
function makeEl(tag) {
  const node = {
    tagName: tag, children: [], attrs: {}, className: "", textContent: "", hidden: false,
    dataset: {}, disabled: false, value: "",
    append() {
      for (let i = 0; i < arguments.length; i += 1) {
        const k = arguments[i];
        if (k === null || k === undefined || k === false) continue;
        this.children.push(k);
      }
    },
    appendChild(k) { this.children.push(k); return k; },
    removeChild(k) { this.children = this.children.filter((c) => c !== k); return k; },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    querySelectorAll() { return []; },
    get firstChild() { return this.children.length ? this.children[0] : null; },
    get innerHTML() { return ""; },
    set innerHTML(v) {},
  };
  return node;
}
globalThis.makeEl = makeEl;
globalThis.document = {
  createElement: (tag) => makeEl(tag),
  createTextNode: (text) => ({ text }),
  getElementById(id) { if (!__nodes[id]) __nodes[id] = makeEl("div#" + id); return __nodes[id]; },
  querySelectorAll() { return []; },
  addEventListener() {},
};
globalThis.window = { addEventListener() {}, scrollTo() {} };
globalThis.location = { hash: "", protocol: "https:", href: "https://user.github.io/app/",
                        origin: "https://user.github.io" };
globalThis.history = { replaceState() {} };
globalThis.navigator = {};
globalThis.fetch = async (url) => {
  const key = String(url).replace(/^https:\/\/user\.github\.io\/app\//, "").replace(/^\.\//, "");
  if (Object.prototype.hasOwnProperty.call(__server, key)) {
    const body = __server[key];
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
  }
  return { ok: false, status: 404, json: async () => null, text: async () => "missing" };
};
if (typeof Intl === "undefined") {
  globalThis.Intl = { NumberFormat: class { constructor() {} format(v) { return String(v); } } };
}
globalThis.flatten = function (node) {
  if (node === null || node === undefined) return "";
  if (typeof node === "string") return node;
  let out = node.textContent || "";
  for (const kid of (node.children || [])) out += " " + flatten(kid);
  return out;
};
"""


def _ui():  # pragma: no cover - quickjs 가 없으면 건너뛴다
    quickjs = pytest.importorskip("quickjs", reason="quickjs 가 없어 화면 로직을 돌리지 않는다")
    ctx = quickjs.Context()
    ctx.eval(_UI_HARNESS)
    ctx.eval((ROOT / "web" / "app.js").read_text(encoding="utf-8"))
    _pump(ctx)
    return ctx


def _pump(ctx, rounds: int = 80) -> None:
    for _ in range(rounds):
        if not ctx.execute_pending_job():
            return


_DETAIL = {
    "round1": [{"skill": "buffett", "score": 12, "stance": "hold", "reasons": ["상세 1R 근거"]}],
    "round2": [{"skill": "buffett", "score": 5, "changed": True,
                "rebuttals": [{"target_skill": "lynch", "point": "반박"}]}],
    "round3": {"guard": {"veto_codes": [], "structural_risk": 12},
               "chair": {"one_line_ko": "상세 3R 한 줄", "conflict": False, "consensus": ["합의"],
                         "grade_recommendation": "WATCH"}},
}
_SLIM_ROW = {"report_id": "R1", "ticker": "AAA", "detail_path": "data/debate_details/R1.json",
             "one_line_ko": "요약 한 줄",
             "validation": {"quotes_checked": 3, "quotes_dropped": 1}, "snapshot_matched": True}


def _render_rounds(ctx, row: dict, server: dict | None = None) -> str:
    ctx.set("__server", ctx.parse_json(json.dumps(server or {})))
    ctx.set("__row", ctx.parse_json(json.dumps(row)))
    ctx.eval("globalThis.__host = makeEl('div'); fillDebateRounds(__host, __row);")
    _pump(ctx)
    return ctx.eval("flatten(__host)")


def test_ui_fetches_the_detail_file_when_rounds_are_not_inline():
    """감사 09: 목록에서 원문을 뺐으니 화면은 `detail_path` 를 읽어 **같은 1R→2R→3R** 을 그린다."""
    ctx = _ui()
    with_detail = _render_rounds(ctx, _SLIM_ROW, {"data/debate_details/R1.json": _DETAIL})
    for label in ("1R 독립 판단", "2R 반박", "3R 리스크·결론"):
        assert label in with_detail, with_detail
    assert "상세 1R 근거" in with_detail and "상세 3R 한 줄" in with_detail
    assert "불러오지 못했습니다" not in with_detail

    missing = _render_rounds(ctx, _SLIM_ROW, {})
    assert "불러오지 못했습니다" in missing
    assert "요약 한 줄" in missing, "상세가 없어도 의장 한 줄은 보여 준다"


def test_ui_keeps_inline_rounds_and_explains_when_there_is_no_detail():
    ctx = _ui()
    inline = dict(_SLIM_ROW, round1=_DETAIL["round1"], round2=_DETAIL["round2"], round3=_DETAIL["round3"])
    del inline["detail_path"]
    text = _render_rounds(ctx, inline, {})
    assert "1R 독립 판단" in text and "상세 3R 한 줄" in text, "인라인 원문은 그대로 그린다"
    assert "불러오지 못했습니다" not in text

    bare = {k: v for k, v in _SLIM_ROW.items() if k != "detail_path"}
    text = _render_rounds(ctx, bare, {})
    assert "제공되지 않습니다" in text and "요약 한 줄" in text


def test_ui_warns_when_the_debate_is_from_an_older_snapshot():
    ctx = _ui()
    ctx.set("__row", ctx.parse_json(json.dumps(dict(_SLIM_ROW, snapshot_matched=False))))
    ctx.eval("""
    globalThis.__host = debateBlock({debate: __row, ticker: "AAA"});
    """)
    _pump(ctx, 10)
    text = ctx.eval("flatten(__host)")
    assert "옛 스냅샷" in text or "붙은 것이 아닙니다" in text, text


def test_service_worker_bumps_its_shell_and_trims_the_old_one():
    """셸 캐시 키를 올리면 예전 셸 캐시를 지워야 index.html 의 같은 `?v=` 요청이 새 app.js 로 간다."""
    sw = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
    # 버전 문자열은 **고정값이 아니라 계약**이다 — app.js 를 고칠 때마다 올려야 하므로 값은 자유롭고,
    # `index.html` 의 `?v=` 와 **같아야** 한다(다르면 옛 app.js 가 셸 캐시에서 계속 돈다).
    sw_version = re.search(r'SW_VERSION = "([^"]+)"', sw).group(1)
    asset_version = re.search(r'ASSET_VERSION = "([^"]+)"', sw).group(1)
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert sw_version.startswith("phase4-")
    assert re.search(r'app\.js\?v=' + re.escape(asset_version), html), "index.html 의 ?v= 와 맞춘다"
    assert "trimShells" in sw and "SHELL_PREFIX" in sw
    assert "debate_details" in sw, "분리된 상세 경로가 캐시 규칙 안에 있다는 설명이 없다"
    # /data/ 아래 모든 경로(상세 포함)를 세트 캐시 규칙으로 처리한다(온라인·오프라인 공통).
    assert 'url.pathname.includes("/data/")' in sw
    assert "cacheIfCurrent" in sw and "sha256" in sw
    core = re.search(r"const CORE_FILES = \[(.*?)\];", sw, re.S).group(1)
    assert "debate.json" in core and "debate_details" not in core, "상세는 핵심 세트가 아니다"
