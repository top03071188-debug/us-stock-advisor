/* web/app.js — 대시보드 v1 (기획서 §5.1~5.7). 빌드 도구 없이 도는 정적 앱.
 *
 * 데이터는 `./data/`(빌드 산출물)에서 읽고, 없으면 `./sample-data/`로 폴백한다.
 * 공개 산출물에는 원시 OHLC·뉴스 본문·보유 수량·평단이 없다(2.2.4).
 * 화면은 색만으로 구분하지 않는다 — 항상 텍스트 배지·화살표를 함께 쓴다(5.6).
 */
"use strict";

const CORE_FILES = [
  "meta.json",
  "signals/today.json",
  "folds.json",
  "watchlist.json",
  "ledger/summary.json",
  "ledger/records.json",
  "ledger/portfolio.json",
  "radar/13f.json",
  "config.json",
  // Phase 4 — 없으면(토론 0건·첫 배포) null 로 두고 "없음" 상태를 그린다.
  "debate.json",
  "health.json",
  "notifications.json",
];

const ROUTES = ["today", "ticker", "ledger", "radar"];
const STATUS_LABEL = {
  normal: { text: "데이터 정상", cls: "ok" },
  degraded: { text: "일부 소스 실패", cls: "warn" },
  stale: { text: "데이터 지연", cls: "warn" },
  sample: { text: "샘플(합성) 데이터", cls: "warn" },
  awaiting_first_run: { text: "첫 배치 대기", cls: "info" },
};
const REGIME_LABEL = { risk_on: "위험 선호(Risk-On)", neutral: "중립(Neutral)", risk_off: "위험 회피(Risk-Off)" };
const NEWS_LABEL = { not_read: "뉴스 미판독", insufficient_evidence: "뉴스 근거 부족", read_done: "뉴스 판독 완료" };
const DECISION_LABEL = { BUY: "매수", SELL: "매도", WATCH: "관찰" };
const STANCE_LABEL = { buy: "매수", hold: "보유", sell: "매도", avoid: "회피" };
const TRIM_LABEL = { none: "분할익절 전", iv_high: "IV_high 1/3 익절", rsi: "RSI 1/3 익절", trailing: "잔여 트레일링" };
const TRANCHE_STATUS = {
  filled: { text: "체결", cls: "ok" },
  pending: { text: "대기", cls: "info" },
  invalid: { text: "무효", cls: "warn" },
  cancelled: { text: "취소", cls: "bad" },
  rejected: { text: "거절", cls: "warn" },
};
const BADGE_CLASS = {
  "토론 완료": "ok",
  "토론 없음": "info",
  "정족수 미달": "warn",
  "지연 도착": "warn",
  "산식과 다름(보류 권고)": "warn",
  "무효(발행 전 완료)": "bad",
  // 02 · T1 채택 계약 — 채택되지 않은 회차는 “완료”가 아니다.
  "채택 아님": "info",
};

const state = { core: {}, tickers: [], ticker: null, route: "today", sampleMode: false, bound: false,
  bundle: null };

/* ────────────────────────── 공용 ────────────────────────── */
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = String(v);
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function badge(text, cls) {
  return el("span", { class: `badge ${cls || ""}`.trim(), text });
}

const NF = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });

function isNum(x) {
  return typeof x === "number" && Number.isFinite(x);
}

/** 첫 번째로 값이 있는(number/string) 인자를 돌려준다 — `??` 대신 쓰는 헬퍼. */
function pick(...values) {
  for (const v of values) {
    if (v !== null && v !== undefined) return v;
  }
  return null;
}

function fmtPct(x, digits = 1) {
  if (!isNum(x)) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

function fmtSignedPct(x, digits = 1) {
  if (!isNum(x)) return "—";
  const arrow = x > 0 ? "▲ " : x < 0 ? "▼ " : "";
  return `${arrow}${x > 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;
}

function fmtNum(x, digits = 1) {
  if (!isNum(x)) return "—";
  return x.toFixed(digits);
}

function fmtMoney(x) {
  if (!isNum(x)) return "—";
  return `$${NF.format(Math.round(x * 100) / 100)}`;
}

function fmtKo(iso, withTime = true) {
  if (!iso) return "—";
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(iso))) {
    const [y, m, day] = String(iso).split("-");
    return `${y}. ${m}. ${day}.`;
  }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  const opts = withTime
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Seoul" }
    : { year: "numeric", month: "2-digit", day: "2-digit" };
  return new Intl.DateTimeFormat("ko-KR", opts).format(d);
}

function ruleMap(config) {
  const map = {};
  ((config && config.groups) || []).forEach((group) => {
    (group.items || []).forEach((item) => { map[item.key] = item.value; });
  });
  return map;
}

function passMark(pass) {
  if (pass === true) return el("span", { class: "good", text: "✓ 통과" });
  if (pass === false) return el("span", { class: "bad", text: "✗ 미달" });
  return el("span", { class: "muted", text: "— 계산 불가" });
}

async function loadJson(name) {
  const bases = state.sampleMode ? ["./sample-data/"] : ["./data/", "./sample-data/"];
  for (const base of bases) {
    try {
      const res = await fetch(base + name, { cache: "no-cache" });
      if (res.ok) return await res.json();
    } catch (err) {
      /* 다음 경로 시도 — 파일이 없으면 정상 상태로 처리한다 */
    }
  }
  return null;
}

function section(title, children, extra) {
  return el("section", { class: "card" }, [el("h2", { text: title }, extra || null), ...[].concat(children)]);
}

function subBlock(title, children) {
  return el("div", { class: "sub" }, [el("h3", { text: title }), ...[].concat(children)]);
}

function emptyState(text) {
  return el("p", { class: "empty", text });
}

/* ────────────────────────── 오늘 ────────────────────────── */
function renderStatus(meta) {
  const node = $("status-badge");
  const status = (meta && meta.status) || "awaiting_first_run";
  const info = STATUS_LABEL[status] || { text: `상태 ${status}`, cls: "warn" };
  node.textContent = info.text;
  node.className = `badge ${info.cls}`;
  const bits = [];
  if (meta && meta.as_of) bits.push(`기준일 ${meta.as_of}`);
  if (meta && meta.prices_as_of) bits.push(`마지막 종가 ${meta.prices_as_of}`);
  if (meta && meta.params_version) bits.push(meta.params_version);
  $("build").textContent = bits.join(" · ") + (meta && meta.generated_at ? ` · 빌드 ${fmtKo(meta.generated_at)}` : "");
}

function sampleToggle(label, onClick) {
  return el("button", {
    type: "button", class: "badge info",
    style: "min-height:44px;padding:8px 12px;cursor:pointer;background:transparent;",
    text: label, onclick: onClick,
  });
}

function plannedHint(planned, actual) {
  // 계획 시각과 실제 발행 시각이 1분 이상 다르면 함께 보여 준다(2.2.2 세 시각의 분리).
  if (!planned || !actual) return "";
  const diff = Math.abs(new Date(planned).getTime() - new Date(actual).getTime());
  return diff >= 60000 ? ` · 예정 ${fmtKo(planned)}` : "";
}

function ingestHint(ingest) {
  // R07: 마감 이후에 실제로 받은 데이터라면 그 지연을 숨기지 않는다.
  // `after_cutoff` 가 참이거나 지연이 1분 이상이면 "수집 지연 N분" 한 줄을 붙인다.
  if (!ingest || typeof ingest !== "object") return null;
  const lag = isNum(ingest.lag_seconds) ? Number(ingest.lag_seconds) : null;
  const late = ingest.after_cutoff === true || (lag !== null && lag >= 60);
  if (!late) return null;
  const minutes = lag === null ? null : Math.max(1, Math.round(lag / 60));
  const at = ingest.inputs_ingested_at ? ` · 수집 ${fmtKo(ingest.inputs_ingested_at)}` : "";
  return `수집 지연 ${minutes === null ? "확인 불가" : `${minutes}분`}(마감 후 수집)${at}`;
}

function renderFreshness(meta) {
  const host = clear($("freshness"));
  const status = (meta && meta.status) || "awaiting_first_run";
  const info = STATUS_LABEL[status] || { text: status, cls: "warn" };
  const cls = info.cls === "ok" ? "is-ok" : info.cls === "bad" ? "is-bad" : "is-warn";
  const rows = [
    el("div", { class: "banner " + cls }, [
      el("strong", { text: info.text }),
      el("span", { class: "tiny", text: `발행 ${fmtKo(meta && meta.signal_published_at)}`
        + plannedHint(meta && meta.signal_published_planned_at, meta && meta.signal_published_at)
        + ` · 마감 ${fmtKo(meta && meta.data_cutoff_at)}` }),
    ]),
  ];
  if (meta && meta.note_ko) rows.push(el("p", { class: "tiny", text: meta.note_ko }));
  const ingest = ingestHint(meta && meta.ingest);
  if (ingest) rows.push(el("p", { class: "tiny", text: ingest }));
  // v1.3.3 01-b: 배치 시작 시각과 발행 시각을 따로 보여 준다 — 수집이 오래 걸린 날을 숨기지 않는다.
  if (meta && (meta.run_started_at || isNum(meta.publish_lag_seconds))) {
    const lag = isNum(meta.publish_lag_seconds) ? Math.round(Number(meta.publish_lag_seconds) / 60) : null;
    rows.push(el("p", { class: "tiny", text: `배치 시작 ${fmtKo(meta.run_started_at)}`
      + (lag === null ? "" : ` · 발행까지 ${lag}분`)
      + (meta.inputs_hash ? ` · 입력 지문 ${String(meta.inputs_hash).replace("sha256:", "").slice(0, 12)}` : "") }));
  }
  if (meta && meta.published_after_order_cutoff === true) {
    rows.push(el("p", { class: "tiny" }, [badge("주문 마감 후 발행", "warn"),
      el("span", { text: ` 이 신호는 주문 마감(${fmtKo(meta.order_cutoff_at)}) 뒤에 확정됐습니다 — 대상 세션 ${meta.target_session || "—"}.` })]));
  }
  if (meta && meta.wacc_risk_free && meta.wacc_risk_free.fallback === true) {
    rows.push(el("p", { class: "tiny", text: "WACC 무위험금리: 마감 빈티지에서 DGS10 미확보 — 기본값으로 계산했습니다(01)." }));
  }
  if (meta && meta.source && meta.source !== "none") {
    rows.push(el("p", { class: "tiny", text: `데이터 출처: ${meta.source === "fixtures" ? "픽스처(합성)" : meta.source}` }));
  }
  if (state.sampleMode) {
    rows.push(el("div", { class: "banner is-warn" }, [
      el("strong", { text: "샘플 화면" }),
      el("span", { class: "tiny", text: "합성 데이터입니다 — 실전 신호가 아닙니다." }),
      sampleToggle("실데이터로 돌아가기", () => { state.sampleMode = false; state.ticker = null; boot(); }),
    ]));
  } else if (status === "awaiting_first_run") {
    rows.push(el("div", { class: "banner is-warn" }, [
      el("strong", { text: "아직 실전 배치가 돌지 않았습니다" }),
      el("span", { class: "tiny", text: "신호가 비어 있는 것이 정상입니다. 화면 구성을 샘플(합성) 데이터로 미리 볼 수 있습니다." }),
      sampleToggle("샘플 화면 보기", () => { state.sampleMode = true; state.ticker = null; boot(); }),
    ]));
  }
  host.append(...rows);
}

/* ── 실행 상태(P2-1) — `전일 정상 데이터`와 `이번 세션 판단`을 구분한다 ────────── */
const DATA_STATUS_LABEL = {
  ok: { text: "정상 갱신", cls: "is-ok" },
  degraded: { text: "일부 소스 실패 — 판단은 계속", cls: "is-warn" },
  outage: { text: "이번 세션 판단 보류 — 마지막 성공 데이터 표시 중", cls: "is-bad" },
};

function renderDataStatus(meta, health) {
  const host = clear($("data-status-banner"));
  const h = health || {};
  const status = h.data_status || (meta && meta.data_status) || (meta && meta.status) || "";
  const info = DATA_STATUS_LABEL[status];
  const attempt = h.last_attempt_at || (meta && meta.last_attempt_at);
  const success = h.last_success_at || (meta && meta.last_success_at);
  const outageId = h.outage_id || (meta && meta.outage_id);
  if (!info) {
    if (attempt || success) {
      host.append(el("p", { class: "tiny", text:
        `마지막 시도 ${fmtKo(attempt)} · 마지막 성공 ${fmtKo(success)}` }));
    }
    return;
  }
  host.append(el("div", { class: `banner ${info.cls}` }, [
    el("strong", { text: info.text }),
    el("span", { class: "tiny", text: status === "outage"
      ? ` · 직전 정상 신호 ${meta && meta.as_of ? `${meta.as_of} 기준` : "그대로"} 표시 · `
        + (success ? `마지막 성공 ${fmtKo(success)}` : "이전 정상 배치 없음")
      : ` · 마지막 시도 ${fmtKo(attempt)}` }),
  ]));
  // `last_attempt_at`(이번 시도)과 `last_success_at`(마지막 성공)은 **다른 값**이다 — 둘을 나란히 보여 준다.
  // 첫 장애 실행이면 `last_success_at` 이 `null` 이다 — “배치가 한 번도 성공한 적 없다”를 숨기지 않는다.
  host.append(el("p", { class: "tiny", text:
    `시도 ${fmtKo(attempt)} · 성공 ${success ? fmtKo(success) : "이전 정상 배치 없음"} · `
    + `마감 ${fmtKo(h.data_cutoff_at || (meta && meta.data_cutoff_at))}`
    + (h.target_session ? ` · 대상 세션 ${h.target_session}` : "")
    + (outageId ? ` · 장애 ${outageId}` : "") }));
}

/* ── 발행 세트(P2-1 · 감사 12) — 매니페스트로 세트 **전체**를 검증하고 한 번에 전환한다 ── */
function shortBundle(bundle) {
  if (!bundle) return "—";
  const [, snap] = String(bundle).split("|");
  return String(snap || bundle).replace("sha256:", "").slice(0, 12);
}

function postToServiceWorker(message, timeoutMs = 1500) {
  return new Promise((resolve) => {
    if (!("serviceWorker" in navigator) || !navigator.serviceWorker.controller) {
      resolve(null);
      return;
    }
    const channel = new MessageChannel();
    const timer = setTimeout(() => resolve(null), timeoutMs);
    channel.port1.onmessage = (event) => {
      clearTimeout(timer);
      resolve(event.data || null);
    };
    try {
      navigator.serviceWorker.controller.postMessage(message, [channel.port2]);
    } catch (err) {
      clearTimeout(timer);
      resolve(null);
    }
  });
}

function currentAppAssetVersion() {
  const script = Array.from(document.scripts || []).find((s) => /app\.js(\?|$)/.test(s.src || ""));
  if (!script) return null;
  try {
    return new URL(script.src, location.href).searchParams.get("v");
  } catch (err) {
    return null;
  }
}

function reloadOnceForSw(targetVersion) {
  const key = "us_stock_sw_reloaded_v";
  const marker = String(targetVersion || "changed");
  try {
    if (sessionStorage.getItem(key) === marker) return false;
    sessionStorage.setItem(key, marker);
  } catch (err) {
    // sessionStorage 차단 환경에서는 자동 리로드를 생략해 루프를 피한다.
    return false;
  }
  location.reload();
  return true;
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator) || !/^https?:$/.test(location.protocol)) return null;
  const hadController = Boolean(navigator.serviceWorker.controller);
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController) reloadOnceForSw("controller");
  });
  try {
    return await navigator.serviceWorker.register("./sw.js", { scope: "./", updateViaCache: "none" });
  } catch (err) {
    return null;
  }
}

/* ────────────────────── 앱 설치(PWA·Android WebAPK) ──────────────────────
 * Android Chrome 은 설치 조건(manifest + 192/512 PNG 아이콘 + 서비스워커)을 만족하면
 * `beforeinstallprompt` 를 던진다. 그 이벤트를 붙잡아 두었다가 버튼 클릭 때 띄운다.
 * - 이미 standalone(설치된 앱)으로 돌고 있으면 버튼을 숨긴다.
 * - 버튼은 이벤트를 받기 전까지 `hidden` — 설치를 지원하지 않는 브라우저(iOS Safari 등)에서
 *   눌러도 아무 일이 없는 버튼을 보여 주지 않는다.
 * - 프롬프트는 한 번만 쓸 수 있으므로 쓴 뒤에는 `deferredPrompt` 를 비우고 버튼을 숨긴다.
 */
let deferredPrompt = null;

/** 설치된 앱(standalone) 모드로 실행 중인가. */
function isStandalone() {
  const nav = (typeof window !== "undefined" && window.navigator) ? window.navigator
    : (typeof navigator !== "undefined" ? navigator : null);
  if (nav && nav.standalone === true) return true; // iOS 홈 화면 앱
  const matchMedia = (typeof window !== "undefined") ? window.matchMedia : null;
  if (typeof matchMedia !== "function") return false;
  const media = matchMedia("(display-mode: standalone)");
  return Boolean(media && media.matches);
}

function setupInstallButton() {
  const btn = $("pwa-install-btn");
  if (!btn) return;
  if (isStandalone()) {
    btn.hidden = true;
    return;
  }
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    btn.hidden = false;
  });
  window.addEventListener("appinstalled", () => {
    deferredPrompt = null;
    btn.hidden = true;
  });
  btn.addEventListener("click", async () => {
    if (!deferredPrompt) {
      btn.hidden = true;
      return;
    }
    btn.disabled = true;
    try {
      deferredPrompt.prompt();
      await deferredPrompt.userChoice;
    } catch (err) {
      // 프롬프트가 이미 쓰였거나 브라우저가 거부한 경우 — 조용히 넘어간다.
    }
    deferredPrompt = null;
    btn.disabled = false;
    btn.hidden = true;
  });
}

/** 파일 목록 → `{name: parsed}` — 파싱 실패한 파일은 null 로 둔다(화면이 "없음"을 그린다). */
function parseFiles(files) {
  const docs = {};
  (files || []).forEach((file) => {
    try {
      docs[file.name] = JSON.parse(file.text);
    } catch (err) {
      docs[file.name] = null;
    }
  });
  return docs;
}

function renderBundle(info) {
  const host = clear($("bundle-banner"));
  if (info && info.source === "sample") return;   // 샘플 화면에는 발행 세트 배너가 없다
  const bundle = (info && info.bundle_version) || null;
  if (!info || info.source === "direct") {
    host.append(el("p", { class: "tiny", text:
      `발행 세트 ${shortBundle(bundle)} · 오프라인 열람은 새로고침 뒤에 준비됩니다` }));
    return;
  }
  if (info.mismatch) {
    host.append(el("div", { class: "banner is-warn" }, [
      el("strong", { text: "이전/혼합 세트" }),
      el("span", { class: "tiny", text:
        ` 캐시된 세트(${shortBundle(info.previous)})와 현재 발행 세트(${shortBundle(bundle)})가 달랐습니다 —`
        + " 이번 화면은 검증된 새 세트로 맞췄습니다." }),
    ]));
    return;
  }
  if (info.source === "cache") {
    host.append(el("div", { class: "banner is-warn" }, [
      el("strong", { text: "오프라인 — 캐시된 세트 표시 중" }),
      el("span", { class: "tiny", text: ` 세트 ${shortBundle(bundle)} · 연결되면 새로고침하세요.` }),
    ]));
    return;
  }
  host.append(el("p", { class: "tiny", text:
    `발행 세트 ${shortBundle(bundle)} · 오프라인 열람 준비됨(${info.count || 0}개 파일 해시 검증)` }));
}

/**
 * 발행 세트 적재 — `deploy_manifest.json` 의 **핵심 세트 전체**를 받아 검증한 뒤에만 화면에 쓴다.
 * 실패하면 ① 직전 완전한 세트(서비스워커 캐시) ② 직접 fetch 순으로 내려간다.
 */
async function loadPublicSet() {
  const prepared = await postToServiceWorker({ type: "prepare-set" }, 9000);
  if (prepared && prepared.ok && Array.isArray(prepared.files) && prepared.files.length) {
    const appVer = currentAppAssetVersion();
    if (prepared.sw_version && appVer && prepared.sw_version !== appVer) {
      reloadOnceForSw(prepared.sw_version);
    }
    return {
      docs: parseFiles(prepared.files),
      info: { source: "network", bundle_version: prepared.bundle_version,
        previous: prepared.previous_bundle_version, mismatch: prepared.mismatch,
        count: prepared.file_count },
    };
  }
  const cached = await postToServiceWorker({ type: "read-set" }, 3000);
  if (cached && cached.ok && Array.isArray(cached.files) && cached.files.length) {
    return {
      docs: parseFiles(cached.files),
      info: { source: "cache", bundle_version: cached.bundle_version,
        count: cached.files.length },
    };
  }
  const results = await Promise.all(CORE_FILES.map(loadJson));
  return { docs: coreDocs(results), info: { source: "direct" } };
}

function coreDocs(results) {
  const docs = {};
  CORE_FILES.forEach((name, index) => { docs[name] = results[index] || null; });
  return docs;
}

function renderNotifyBanner(notif, debate, today) {  const host = clear($("notify-banner"));
  const events = (notif && notif.new) || [];
  const signals = (today && today.signals) || [];
  const covered = (debate && debate.by_ticker) || {};
  const candidates = signals.filter((sig) => {
    const row = covered[sig.ticker];
    if (row && row.signal_id && sig.signal_id && row.signal_id !== sig.signal_id) return false;
    return row ? false : (sig.decision === "BUY" || sig.decision === "WATCH");
  });
  if (candidates.length) {
    // 3.11.8 — 후보가 있는데 토론이 없으면 붙일 수 있는 창(발행 ~ 주문 마감)을 알려 준다.
    host.append(el("div", { class: "banner is-warn" }, [
      el("strong", { text: "PC/Cloud 토론 실행 권장" }),
      el("span", { class: "tiny", text:
        ` 후보 ${candidates.length}건(${candidates.slice(0, 4).map((c) => c.ticker).join(" · ")})에 토론이 없습니다 — `
        + "신호 발행 뒤 ~ 주문 마감 전에 끝나야 그 결정에 붙습니다." }),
    ]));
  }
  if (events.length) {
    host.append(el("div", { class: "banner " + (events.some((e) => e.severity === "bad") ? "is-bad" : "is-warn") }, [
      el("strong", { text: `새 알림 ${events.length}건` }),
      el("span", { class: "tiny", text: notif.n_suppressed ? ` · 중복 억제 ${notif.n_suppressed}건` : "" }),
    ]));
    const list = el("div", { class: "notify-list" });
    events.slice(0, 5).forEach((e) => {
      list.append(el("div", { class: "notify-item" }, [
        el("div", {}, [el("span", { text: e.message_ko || e.label_ko || "" }),
          el("p", { class: "tiny", text: `${e.sent_at ? fmtKo(e.sent_at) : ""} ${e.ticker || ""}`.trim() })]),
        badge(e.label_ko || e.type, e.severity === "bad" ? "bad" : (e.severity === "warn" ? "warn" : "info")),
      ]));
    });
    host.append(list);
  }
}

function renderRegime(meta) {
  const host = clear($("regime-banner"));
  if (!meta || !meta.regime) {
    host.append(el("div", { class: "banner" }, [el("strong", { text: "시장 레짐: 판정 불가" }),
      el("span", { class: "tiny", text: "거시 지표가 부족합니다 — 포지션 배수 0.5 로 보수 운용(2.7)." })]));
    return;
  }
  const label = REGIME_LABEL[meta.regime] || meta.regime;
  const verified = meta.regime_verified === false ? "미검증(신호 3개 미만)" : "검증됨";
  host.append(el("div", { class: "banner" }, [
    el("strong", { text: `시장 레짐: ${label}` }),
    el("span", { class: "tiny", text: `포지션 배수 ${fmtNum(meta.regime_multiplier, 2)} · ${verified}` }),
  ]));
}

function signalNumbers(sig) {
  const v = sig.valuation || {};
  const ind = sig.indicators || {};
  const items = [];
  items.push(el("span", {}, [el("span", { class: "tiny", text: "안전마진 " }),
    el("span", { class: "num", text: fmtPct(v.margin) })]));
  items.push(el("span", {}, [el("span", { class: "tiny", text: "RSI " }),
    el("span", { class: "num", text: fmtNum(ind.rsi14, 0) })]));
  if (isNum(v.iv_low) && isNum(v.iv_high)) {
    items.push(el("span", {}, [el("span", { class: "tiny", text: "IV 밴드 " }),
      el("span", { class: "num", text: `${fmtMoney(v.iv_low)}~${fmtMoney(v.iv_high)}` })]));
  }
  return el("div", { class: "sig-nums" }, items);
}

function debateBadgeFor(sig) {
  const debate = state.core && state.core.debate;
  const row = debate && debate.by_ticker ? debate.by_ticker[sig.ticker] : null;
  if (!row || (row.signal_id && sig.signal_id && row.signal_id !== sig.signal_id)) return null;
  return row;
}

function signalCard(sig) {
  const decision = sig.decision || sig.type;
  const typeCls = decision === "BUY" ? "type-buy" : decision === "SELL" ? "type-sell" : "type-watch";
  const arrow = decision === "BUY" ? "arrow-up" : decision === "SELL" ? "arrow-down" : "";
  const head = el("div", { class: "sig-head" }, [
    el("span", { class: `badge ${decision === "BUY" ? "ok" : decision === "SELL" ? "bad" : "warn"}`,
      text: `${DECISION_LABEL[decision] || decision} ${decision}` }),
    el("span", { class: "sig-ticker" }, [el("span", { class: `${typeCls} ${arrow}`, text: "" }), sig.ticker]),
    el("span", { class: "sig-score num", text: `${fmtNum(sig.entry_score, 1)}점` }),
    badge(sig.confidence_label_ko || "신뢰도 미상", "info"),
  ]);
  const foot = [];
  if (sig.news_badge_ko) foot.push(badge(sig.news_badge_ko, "warn"));
  const entry = Array.isArray(sig.entry_plan) ? sig.entry_plan.find((t) => t.tranche === 1) : null;
  if (decision === "BUY" && entry && isNum(entry.price_ref)) {
    foot.push(el("span", { class: "tiny", text: `분할 1차 ${fmtMoney(entry.price_ref)}` }));
  }
  if (sig.sizing_note_ko) foot.push(el("span", { class: "tiny", text: sig.sizing_note_ko }));
  // v1.3.3 02: 회차는 **(as_of, ticker) 단위**다 — 같은 날 다른 종목의 2회차와 뜻이 다르므로 “종목별”을 붙인다.
  const revision = isNum(sig.revision) ? Number(sig.revision) : 1;
  foot.push(badge(`종목별 ${revision}회차${sig.supersedes ? ` · ${sig.supersedes} 대체` : ""}`, "info"));
  const debateRow = debateBadgeFor(sig);
  if (debateRow) {
    // 3.11.8 — 화면 기본 등급은 정량 그대로, 토론은 배지로만.
    // 원장 편입 이벤트가 아직 없는 리포트(02)는 배지가 비어 있다 — 빈 배지 대신 이유를 보여 준다.
    foot.push(badge(debateRow.badge || "토론 편입 미확정", BADGE_CLASS[debateRow.badge] || "info"));
  }
  // [통합 재검토] 발행 신호가 많은 날은 목록 크기(200KB) 때문에 뒤쪽 카드가 **요약 행**으로 온다
  // (상세는 `tickers/<티커>.json`). 카드가 조용히 비어 보이지 않게 이유를 적는다.
  if (sig.compact) foot.push(badge("요약 — 종목 상세에서 전체 내용", "info"));
  foot.push(el("span", { class: "tiny", text: `근거 ${(sig.evidence || []).length}건` }));
  foot.push(el("span", { class: "tiny", text: `발행 ${fmtKo(sig.signal_published_at)}`
    + plannedHint(sig.signal_published_planned_at, sig.signal_published_at)
    + (sig.target_session ? ` · 주문 ${fmtKo(sig.target_session)}` : "")
    + ` · ${sig.params_version || ""}` }));

  return el("button", {
    class: "sig", type: "button", "data-ticker": sig.ticker,
    "aria-label": `${sig.ticker} ${DECISION_LABEL[decision] || decision} ${fmtNum(sig.entry_score, 1)}점 상세 보기`,
    onclick: () => setRoute("ticker", sig.ticker),
  }, [
    head,
    signalNumbers(sig),
    el("p", { class: "sig-why", text: sig.thesis_ko || "요약 없음" }),
    el("div", { class: "sig-foot" }, foot),
  ]);
}

function renderToday(today, config) {
  const host = clear($("signals"));
  const signals = (today && today.signals) || [];
  const counts = (today && today.counts) || {};
  $("signal-count").textContent = signals.length
    ? `매수 ${counts.buy || 0} · 매도 ${counts.sell || 0} · 관찰 ${counts.watch || 0}`
    : "";
  if (!signals.length) {
    host.append(emptyState((today && today.notes) || "오늘 신호 없음 — 기준을 넘은 종목이 없습니다(정상)."));
    if (today && today.blocked_note_ko) host.append(el("p", { class: "tiny", text: today.blocked_note_ko }));
  } else {
    signals.forEach((sig) => host.append(signalCard(sig)));
  }

  const blockedHost = clear($("blocked"));
  const blocked = (today && today.blocked) || [];
  if (!blocked.length) {
    blockedHost.append(emptyState("차단된 종목 없음 — Veto 가 발동하지 않았습니다."));
  } else {
    blocked.forEach((b) => {
      blockedHost.append(el("div", { class: "row" }, [
        el("div", {}, [
          el("span", { class: "sig-ticker", text: b.ticker }),
          el("span", { class: "tiny", text: `  ${(b.codes || []).join("·")}` }),
          el("p", { class: "tiny", text: b.message_ko || b.reason || "" }),
        ]),
        b.grade ? badge(b.grade, "warn") : null,
      ]));
    });
  }

  const rulesHost = clear($("today-rules"));
  const rules = ruleMap(config);
  const val = (key) => (rules[key] === undefined ? "—" : rules[key]);
  rulesHost.append(el("div", { class: "kv" }, [
    el("dt", { text: "안전마진" }),
    el("dd", { class: "num", text: `Risk-On ${val("margin_risk_on")} · Neutral ${val("margin_neutral")} · Risk-Off ${val("margin_risk_off")}` }),
    el("dt", { text: "타이밍" }),
    el("dd", { class: "num", text: `T ≥ ${val("timing_min")} · 눌림 조건 ${val("dip_conditions_required")}개` }),
    el("dt", { text: "등급" }),
    el("dd", { class: "num", text: `STRONG_BUY ${val("strong_buy")} · BUY ${val("buy")} · WATCH ${val("watch")}` }),
    el("dt", { text: "파라미터" }),
    el("dd", { text: (config && config.params_version) || "—" }),
  ]));
}

/* ────────────────────────── 종목 ────────────────────────── */
function investorBars(scores, conflict) {
  const names = [["buffett", "버핏"], ["graham", "그레이엄"], ["lynch", "린치"], ["druckenmiller", "드러켄밀러"]];
  const rows = names.filter(([key]) => isNum(scores[key])).map(([key, label]) => {
    const value = scores[key];
    const width = Math.min(100, Math.abs(value) / 2);
    return el("div", { class: "bar-row" }, [
      el("span", { text: label }),
      el("div", { class: "bar-track", role: "img", "aria-label": `${label} ${value}` }, [
        el("span", { class: "bar-zero" }),
        el("span", { class: `bar-fill ${value >= 0 ? "pos" : "neg"}`, style: `width:${width}%` }),
      ]),
      el("span", { class: "bar-val num", text: `${value > 0 ? "+" : ""}${value.toFixed(1)}` }),
    ]);
  });
  if (!rows.length) return emptyState("대가별 점수 없음 — 근거 부족으로 계산하지 않았습니다.");
  return el("div", { class: "bars" }, [
    conflict ? el("p", { class: "tiny" }, [badge("의견 충돌", "warn"),
      el("span", { text: "  대가 간 점수 차이가 60 이상입니다 — 어느 한쪽 근거만 보고 결정하지 않습니다." })]) : null,
    ...rows,
    el("p", { class: "tiny", text: "−100 ~ +100 · 0 기준선" }),
  ]);
}

function valuationBand(valuation) {
  const v = valuation || {};
  const notes = valuationPenaltyNotes(v);
  if (!isNum(v.iv_low) || !isNum(v.iv_high) || !isNum(v.price)) {
    return el("div", { class: "stack" }, [
      emptyState("밸류에이션 밴드 없음 — 가치평가 보류 업종이거나 IV 계산 불가(3.2.1)."),
      notes,
    ].filter(Boolean));
  }
  const lo = Math.min(v.iv_low, v.price);
  const hi = Math.max(v.iv_high, v.price);
  const span = hi > lo ? hi - lo : 1;
  const left = ((v.iv_low - lo) / span) * 100;
  const width = ((v.iv_high - v.iv_low) / span) * 100;
  const pricePos = Math.max(0, Math.min(100, ((v.price - lo) / span) * 100));
  return el("div", { class: "band" }, [
    el("p", { class: "mos" }, [el("span", { text: `안전마진 ${fmtPct(v.margin)}` }),
      el("span", { class: "tiny", text: isNum(v.margin) && v.margin >= 0 ? "  (IV 대비 할인)" : "  (IV 대비 프리미엄)" })]),
    el("div", { class: "band-track", role: "img",
      "aria-label": `IV 밴드 ${fmtMoney(v.iv_low)}~${fmtMoney(v.iv_high)}, 중앙값 ${fmtMoney(v.iv_median)}, 현재가 ${fmtMoney(v.price)}` }, [
      el("span", { class: "band-span", style: `left:${left}%;width:${width}%` }),
      el("span", { class: "band-price", style: `left:${pricePos}%` }),
    ]),
    el("div", { class: "band-legend num" }, [
      el("span", { text: `IV_low ${fmtMoney(v.iv_low)}` }),
      el("span", { text: `IV 중앙 ${fmtMoney(v.iv_median)}` }),
      el("span", { text: `IV_high ${fmtMoney(v.iv_high)}` }),
    ]),
    el("p", { class: "tiny", text: `현재가 ${fmtMoney(v.price)} (raw_as_traded) · IV 는 USD/share` }),
    notes,
  ]);
}

/* v1.3.3 — V 근거: 성장률 0% 클립의 WACC 민감도 · EV/EBIT 표본 품질 · 감점 전 V */
function evEbitReasonKo(reason) {
  if (!reason) return null;
  if (reason === "insufficient_history") return "유효 이력 3년 미만";
  if (reason === "current_bar_after_cutoff") return "마지막 봉이 결정 시점 뒤";
  if (reason.startsWith("current_")) return "최신 공시 재무가 무효(부채·현금·주식수 결측 또는 EBIT≤0)";
  return reason;
}

function valuationPenaltyNotes(v) {
  const items = [];
  const meaningful = isNum(v.dcf_sensitivity) || Boolean(v.dcf_dropped) || isNum(v.ev_ebit_percentile)
    || Boolean(v.ev_ebit_reason) || (isNum(v.v_penalty_hist) && v.v_penalty_hist > 0) || isNum(v.v_raw);
  if (!meaningful) return null;
  if (isNum(v.dcf_sensitivity)) {
    if (isNum(v.v_penalty_dcf) && v.v_penalty_dcf > 0) {
      items.push(`DCF 민감도 ±${(v.dcf_sensitivity * 100).toFixed(0)}%`
        + ` → 신뢰도 C · 감점 −${v.v_penalty_dcf}`);
    } else if (v.growth_clipped_to_zero || v.growth_basis === "unavailable") {
      // 성장률이 0 으로 잘리면(또는 CAGR 산출 불가면) bear==bull 이라 "±0%"가 통과처럼 읽힜다 — 다른 축으로 보여 준다.
      const raw = isNum(v.growth_raw_cagr) ? ` (원 CAGR ${(v.growth_raw_cagr * 100).toFixed(1)}%)` : "";
      const head = v.growth_clipped_to_zero ? "성장률 0% 하한 적용" : "성장률 산출 불가(FCFF CAGR 없음)";
      const wacc = isNum(v.wacc_sensitivity)
        ? `WACC ±1%p 민감도 ±${(v.wacc_sensitivity * 100).toFixed(0)}%`
        : "WACC 민감도 계산 불가";
      items.push(`${head}${raw} · ${wacc}`);
    } else {
      items.push(`DCF 민감도 ±${(v.dcf_sensitivity * 100).toFixed(0)}% (허용 ±40% 이내)`);
    }
  } else if (v.dcf_dropped === "SHORT_HISTORY") {
    const years = isNum(v.effective_fcff_years) ? `유효 FCFF ${v.effective_fcff_years}년` : "유효 FCFF 5년 미만";
    items.push(`${years} → 5년 DCF 폐기(EPV·Graham 만) · 신뢰도 ${v.v_confidence || "C"}`);
  } else if (v.dcf_dropped) {
    items.push(`DCF 폐기(${v.dcf_dropped}) → 신뢰도 ${v.v_confidence || "C"}`);
  }
  const span = v.ev_ebit_window ? ` ${v.ev_ebit_window}` : "";
  const sampleNote = [
    isNum(v.ev_ebit_samples) ? `표본 ${v.ev_ebit_samples}개` : null,
    isNum(v.ev_ebit_years) ? `${v.ev_ebit_years}개년` : null,
  ].filter(Boolean).join(" · ");
  if (isNum(v.ev_ebit_percentile)) {
    items.push(`10년 EV/EBIT 백분위 ${v.ev_ebit_percentile.toFixed(0)}`
      + (isNum(v.ev_ebit_median_10y) ? ` (10년 중앙 ${v.ev_ebit_median_10y.toFixed(1)}배)` : "")
      + (sampleNote ? ` [${sampleNote}${span}]` : "")
      + (v.ev_ebit_percentile >= 90 ? " · 과열 구간(S1)" : ""));
  } else {
    const why = evEbitReasonKo(v.ev_ebit_reason) || "이력 부족";
    items.push(`10년 EV/EBIT 백분위 보류 — ${why}`
      + (sampleNote ? ` [${sampleNote}${span}]` : ""));
  }
  if (isNum(v.v_penalty_hist) && v.v_penalty_hist > 0) {
    const div = isNum(v.hist_divergence_signed)
      ? `${v.hist_divergence_signed > 0 ? "+" : ""}${(v.hist_divergence_signed * 100).toFixed(0)}%`
      : (isNum(v.hist_divergence) ? `${(v.hist_divergence * 100).toFixed(0)}%` : "20%+");
    items.push(`역사적 EV/EBIT 대비 IV 괴리 ${div} → 감점 −${v.v_penalty_hist}`);
  }
  const total = isNum(v.v_penalties) ? v.v_penalties : 0;
  const raw = isNum(v.v_raw) ? ` · 감점 전 V ${v.v_raw.toFixed(1)}` : "";
  items.push(`V 감점 합계 −${total}${total === 0 ? " (감점 없음)" : ""}${raw}`);
  return el("p", { class: "tiny", text: `감점 근거 · ${items.join(" · ")}` });
}

function svgChart(chart) {
  const NS = "http://www.w3.org/2000/svg";
  if (!chart || !Array.isArray(chart.index) || chart.index.length < 2) {
    return emptyState("차트 없음 — 주 1회 정규화 지수가 아직 없습니다.");
  }
  const width = 320;
  const height = 150;
  const pad = { l: 4, r: 4, t: 10, b: 16 };
  const series = [chart.index, chart.dma50, chart.dma200].filter((s) => Array.isArray(s) && s.length);
  const values = [];
  series.forEach((s) => s.forEach((v) => { if (isNum(v)) values.push(v); }));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max > min ? max - min : 1;
  const x = (i, n) => pad.l + (i / Math.max(1, n - 1)) * (width - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - min) / span) * (height - pad.t - pad.b);

  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "chart");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label",
    `주 1회 정규화 지수 차트 · ${chart.labels ? chart.labels[0] : ""} ~ ${chart.labels ? chart.labels[chart.labels.length - 1] : ""}`);

  const draw = (arr, color, dashed, widthPx) => {
    if (!Array.isArray(arr) || arr.length !== chart.index.length) return;
    let d = "";
    let started = false;
    arr.forEach((v, i) => {
      if (!isNum(v)) { started = false; return; }
      d += `${started ? "L" : "M"}${x(i, arr.length).toFixed(1)} ${y(v).toFixed(1)} `;
      started = true;
    });
    if (!d) return;
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d.trim());
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", String(widthPx));
    path.setAttribute("stroke-linejoin", "round");
    if (dashed) path.setAttribute("stroke-dasharray", "4 3");
    svg.append(path);
  };

  draw(chart.dma200, "var(--muted)", true, 1.2);
  draw(chart.dma50, "var(--info)", true, 1.4);
  draw(chart.index, "var(--text)", false, 1.8);

  const baseline = document.createElementNS(NS, "line");
  baseline.setAttribute("x1", pad.l);
  baseline.setAttribute("x2", width - pad.r);
  baseline.setAttribute("y1", y(100));
  baseline.setAttribute("y2", y(100));
  baseline.setAttribute("stroke", "var(--line)");
  baseline.setAttribute("stroke-width", "1");
  svg.append(baseline);

  return el("div", {}, [
    svg,
    el("div", { class: "legend" }, [
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--text)" }), "지수(기준일=100)"]),
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--info)" }), "50일선"]),
      el("span", {}, [el("span", { class: "swatch", style: "background:var(--muted)" }), "200일선"]),
    ]),
    el("p", { class: "tiny", text: "주 1회 간격 정규화 지수만 공개합니다 — 원시 OHLC·거래량 시계열은 공개 산출물에 없습니다(2.2.4)." }),
  ]);
}

function newsBlock(sig) {
  const items = (sig && sig.news) || [];
  const stateLabel = NEWS_LABEL[(sig && sig.news_state) || ""] || "뉴스 상태 미상";
  const host = el("div", { class: "stack" }, [
    el("p", { class: "tiny" }, [badge(stateLabel, sig && sig.news_state === "read_done" ? "ok" : "warn"),
      el("span", { text: "  인용문은 공개 페이지에 싣지 않습니다 — 근거는 원문 링크와 검증 배지로만." })]),
  ]);
  if (!items.length) {
    host.append(emptyState("표시할 기사 없음 — 유효 뉴스 창(기본 48시간, 주말·휴장 다음 날은 직전 거래일까지 확장)이 지난 기사는 판독 대상에서 제외합니다(2.7)."));
    return host;
  }
  items.forEach((n) => {
    host.append(el("div", { class: "row" }, [
      el("div", {}, [
        n.url ? el("a", { href: n.url, target: "_blank", rel: "noopener noreferrer", text: n.headline || n.url }) :
          el("span", { text: n.headline || "제목 없음" }),
        el("p", { class: "tiny", text: `${n.source || "출처 미상"} · ${fmtKo(n.published_at)}` }),
      ]),
      n.quote_verified === true ? badge("인용 검증", "ok") : (n.quote_verified === false ? badge("근거 미확인", "warn") : null),
    ]));
  });
  return host;
}

function riskBlock(sig, tickerDoc) {
  const vetoes = ((sig && sig.risk && sig.risk.veto) || []).concat([]);
  const filings = (sig && sig.filings) || [];
  const invalidation = (sig && sig.invalidation) || null;
  const check = tickerDoc && tickerDoc.position_check;
  const host = el("div", { class: "stack" });
  if (vetoes.length) {
    vetoes.forEach((v) => host.append(el("div", { class: "row" }, [
      el("div", {}, [el("strong", { text: `${v.code} ${v.name || ""}` }),
        el("p", { class: "tiny", text: v.message_ko || v.detail || "" })]),
      v.blocking ? badge("차단", "bad") : badge("경고", "warn"),
    ])));
  } else {
    host.append(el("p", { class: "tiny" }, [badge("Veto 없음", "ok"),
      el("span", { text: "  12종 리스크 가드가 발동하지 않았습니다(3.6)." })]));
  }
  if (filings.length) {
    const table = el("table", {}, [
      el("caption", { text: "최근 공시 (filed_date = 접수 시각 기준)" }),
      el("thead", {}, [el("tr", {}, [el("th", { text: "양식" }), el("th", { class: "num", text: "접수일" }),
        el("th", { text: "Item" }), el("th", { text: "원문" })])]),
      el("tbody", {}, filings.map((f) => el("tr", {}, [
        el("td", { text: f.form || "—" }),
        el("td", { class: "num", text: f.filed_date || "—" }),
        el("td", { class: "wrap", text: (f.items || []).join(", ") || "—" }),
        el("td", {}, [f.url ? el("a", { href: f.url, target: "_blank", rel: "noopener noreferrer", text: "SEC" }) : "—"]),
      ]))),
    ]);
    host.append(table);
  }
  if (invalidation) {
    host.append(el("p", { class: "tiny", text: `이 신호가 틀릴 조건: ${fmtMoney(invalidation.price)} 이탈 — ${invalidation.reason || ""}` }));
  }
  if (check) {
    const verdict = String(check.verdict || "");
    const trim = verdict === "TRIM";
    host.append(el("div", { class: "row" }, [
      el("div", {}, [el("strong", { text: `보유 점검: ${verdict}` }),
        el("p", { class: "tiny", text: `exit_score ${fmtNum(check.exit_score, 1)} · S1~S5 는 매일 다시 계산합니다(3.2.5)` }),
        trim ? el("p", { class: "tiny", text:
          "exit_score 60~74 = 1/3 익절(TRIM) 구간입니다 — 분할익절 단계를 올려 고정 기준 수량의 1/3 을 팔니다(3.5)." }) : null]),
      verdict === "EXIT" ? badge("청산 신호", "bad")
        : (trim ? badge("1/3 익절(TRIM)", "warn") : badge("유지", "ok")),
    ]));
  }
  return host;
}

function round1List(row) {
  const rows = (row.round1 || []).filter((r) => isNum(r.score));
  if (!rows.length) return emptyState("1R 점수 없음 — 스키마 검증을 통과하지 못했습니다.");
  return el("div", {}, rows.map((r) => el("div", { class: "skill-row" }, [
    el("span", {}, [el("strong", { text: r.skill || "—" }),
      el("span", { class: "tiny", text: ` ${STANCE_LABEL[r.stance] || r.stance || ""}` })]),
    el("span", { class: `skill-score ${r.score >= 0 ? "pos" : "neg"}`,
      text: `${r.score > 0 ? "+" : ""}${fmtNum(r.score, 0)}` }),
    el("p", { class: "tiny", text: (r.reasons || []).join(" · ") || "근거 요약 없음" }),
    r.quote_verified === true ? badge("인용 검증", "ok") : (r.quote_sha256_count ? badge("해시 확인", "info") : null),
  ])));
}

function round2List(row) {
  const rows = (row.round2 || []).filter((r) => isNum(r.score));
  if (!rows.length) return emptyState("2R 반박 없음 — 합의로 끝났습니다.");
  const scores = {};
  (row.round1 || []).forEach((r) => { scores[r.skill] = r.score; });
  return el("div", {}, rows.map((r) => el("div", { class: "skill-row" }, [
    el("span", {}, [el("strong", { text: r.skill || "—" }),
      r.changed ? badge("입장 변경", "warn") : null]),
    el("span", { class: "skill-score", text: isNum(scores[r.skill]) && isNum(r.score)
      ? `${scores[r.skill] > 0 ? "+" : ""}${fmtNum(scores[r.skill], 0)} → ${r.score > 0 ? "+" : ""}${fmtNum(r.score, 0)}`
      : fmtNum(r.score, 0) }),
    el("p", { class: "tiny", text: (r.rebuttals || []).map((x) => `${x.target_skill}: ${x.point}`).join(" · ") || "반박 요약 없음" }),
    null,
  ])));
}

function round3Block(row) {
  const guard = (row.round3 || {}).guard || {};
  const chair = (row.round3 || {}).chair || {};
  const vetoCodes = guard.veto_codes || [];
  return el("div", {}, [
    el("p", { class: "tiny" }, [badge(chair.conflict ? "의견 갈림" : "합의", chair.conflict ? "warn" : "ok"),
      el("span", { text: `  구조적 리스크 ${isNum(guard.structural_risk) ? fmtNum(guard.structural_risk, 0) : "—"}` })]),
    el("p", { class: "tiny", text: `리스크 가드 Veto: ${vetoCodes.length ? vetoCodes.join(" · ") : "없음"}` }),
    chair.one_line_ko ? el("p", { class: "sig-why", text: chair.one_line_ko }) : emptyState("의장 한 줄 권고 없음"),
    el("p", { class: "tiny", text: `권고 등급 ${chair.grade_recommendation || "—"} · 정량 ${row.quant_grade || "—"}` }),
    (chair.consensus || []).length ? el("p", { class: "tiny", text: `합의: ${chair.consensus.join(" · ")}` }) : null,
    (chair.dissent || []).length ? el("p", { class: "tiny", text: `이견: ${chair.dissent.join(" · ")}` }) : null,
    el("p", { class: "tiny", text: `분할가 참조 ${(chair.entry_plan_refs || []).map((v) => fmtMoney(v)).join(" · ") || "—"}` }),
  ]);
}

function debateBlock(tickerDoc) {
  const row = tickerDoc && tickerDoc.debate;
  const host = el("div", { class: "stack" });
  if (!row) {
    host.append(el("p", { class: "empty", text: (tickerDoc && tickerDoc.debate_note_ko) ||
      "토론 없음 — PC에서 `python codex_debate/run_debate.py --tickers ...` 로 돌릴 수 있습니다." }));
    host.append(el("p", { class: "tiny", text:
      "토론은 행동이 갈릴 때만 별도 줄로 표시하고, 화면 기본 등급은 바꾸지 않습니다(자동 하향 OFF)." }));
    return host;
  }
  const validation = row.validation || {};
  host.append(el("p", {}, [badge(row.badge, BADGE_CLASS[row.badge] || "info"),
    row.news_state ? badge(NEWS_LABEL[row.news_state] || row.news_state,
      row.news_state === "read_done" ? "ok" : (row.news_state === "insufficient_evidence" ? "warn" : "info")) : null,
    el("span", { class: "tiny", text: `  완료 ${fmtKo(row.completed_at)} · 주문 마감 ${fmtKo(row.order_cutoff_at)}` })]));
  if (row.snapshot_matched === false) {
    // 부수 ③ — 티커별 최신 행은 오늘 신호의 스냅샷이 아닐 수 있다(구 리포트). 확정 토론으로 읽지 않게 밝힌다.
    host.append(el("p", { class: "note-box", text:
      "이 토론은 지금 화면의 신호 스냅샷에 붙은 것이 아닙니다 — 옛 스냅샷의 참고 기록이고, 오늘 결정의 확정 토론이 아닙니다." }));
  }
  if (row.news_state === "insufficient_evidence") {
    host.append(el("p", { class: "note-box", text:
      "뉴스 근거 부족 — 신뢰도 상한 C·매수 등급 상한 WATCH 로 반영됩니다(3.6·3.10)." }));
  }
  if (row.quant_grade !== row.display_grade) {
    host.append(el("p", { class: "tiny" }, [badge(`정량 ${row.quant_grade} · 토론 ${row.display_grade}`, "warn")]));
  }
  if (row.badge === "지연 도착") {
    host.append(el("p", { class: "note-box", text:
      "주문 마감 뒤에 끝난 토론입니다 — 그날 결정에는 붙이지 않고 다음 스냅샷의 입력으로만 둡니다(4.8.4)." }));
  }
  if (row.reasons && row.reasons.length) {
    host.append(el("p", { class: "tiny", text: `편입 판정: ${row.reasons.join(" · ")}` }));
  }
  if (row.badge === "채택 아님") {
    // 02 · T1: 같은 신호에 회차가 여럿이어도 **채택본 하나만** 그 결정에 붙는다.
    host.append(el("p", { class: "note-box", text:
      "이 회차는 그날 결정에 채택되지 않았습니다 — 같은 신호의 다른 회차(또는 앞선 배포의 보고서)가 채택되었습니다." }));
  }
  const roundsHost = el("div", {});
  host.append(roundsHost);
  fillDebateRounds(roundsHost, row);
  host.append(el("p", { class: "tiny", text:
    `인용 원문은 공개하지 않습니다 — 해시 검증 배지만(검증 ${validation.quotes_checked ?? 0}건, 폐기 ${validation.quotes_dropped ?? 0}건).` }));
  host.append(el("p", { class: "tiny", text:
    "토론은 신뢰도 등급을 올리지 않습니다 — 등급은 데이터·근거·표본의 함수입니다(3.10)." }));
  return host;
}

/** 라운드 원문이 이 문서에 **인라인**으로 들어 있는가 — 분리 파일(감사 09)과 구 샘플 둘 다 지원. */
function hasInlineRounds(row) {
  if (!row) return false;
  return (Array.isArray(row.round1) && row.round1.length > 0)
    || (Array.isArray(row.round2) && row.round2.length > 0)
    || Boolean(row.round3 && (row.round3.chair || row.round3.guard));
}

/** 1R → 2R → 3R 세 칸 — 예전 인라인 렌더와 **같은 마크업**을 쓴다. */
function debateRounds(row) {
  return el("div", { class: "debate-rounds" }, [
    el("div", { class: "debate-col" }, [el("h3", { text: "1R 독립 판단" }), round1List(row)]),
    el("div", { class: "debate-col" }, [el("h3", { text: "2R 반박" }), round2List(row)]),
    el("div", { class: "debate-col" }, [el("h3", { text: "3R 리스크·결론" }), round3Block(row)]),
  ]);
}

/** `data/debate_details/{report_id}.json` 경로 → `./data/` 기준 이름. */
function debateDetailName(row) {
  const path = String((row && row.detail_path) || "").trim();
  if (!path) return null;
  return path.replace(/^\.?\/?data\//, "").replace(/^\/+/, "") || null;
}

/**
 * 토론 상세를 채운다 — 인라인이 있으면 바로, 없으면 `detail_path` 를 받아 **같은 마크업**으로 그린다.
 * 상세는 발행 세트 캐시에 실려 오프라인에서도 열린다(서비스워커 `handleData`).
 */
function fillDebateRounds(roundsHost, row) {
  if (hasInlineRounds(row)) {
    roundsHost.append(debateRounds(row));
    return;
  }
  const name = debateDetailName(row);
  if (!name) {
    roundsHost.append(emptyState("이 리포트의 라운드 원문은 공개 상세 파일로 제공되지 않습니다."));
    if (row.one_line_ko) roundsHost.append(el("p", { class: "sig-why", text: row.one_line_ko }));
    return;
  }
  roundsHost.append(el("p", { class: "tiny", text: "1R → 2R → 3R 원문을 불러오는 중입니다…" }));
  loadJson(name).then((detail) => {
    clear(roundsHost);
    if (detail && hasInlineRounds(detail)) {
      roundsHost.append(debateRounds({ ...row, ...detail }));
      return;
    }
    roundsHost.append(emptyState("토론 상세 원문을 불러오지 못했습니다 — 오프라인이거나 보관 범위를 넘은 리포트입니다."));
    if (row.one_line_ko) roundsHost.append(el("p", { class: "sig-why", text: row.one_line_ko }));
  }).catch(() => {
    clear(roundsHost);
    roundsHost.append(emptyState("토론 상세 원문을 불러오지 못했습니다."));
  });
}

function positionBlock(tickerDoc) {
  const position = tickerDoc && tickerDoc.position;
  const host = el("div", { class: "stack" });
  if (!position) {
    host.append(emptyState("이 종목은 모의 계좌에 없습니다 — 1차 체결 전이거나 이미 청산됐습니다."));
    host.append(el("p", { class: "tiny", text: "분할·익절 상태는 체결 뒤 공개 계좌 스냅샷에만 비중·상태로 실립니다." }));
    return host;
  }
  const tranches = position.tranches || [];
  host.append(el("div", { class: "tranche-row" }, [
    el("strong", { text: "분할" }),
    el("div", { class: "stack" }, tranches.map((t) => {
      const meta = TRANCHE_STATUS[t.status] || { text: t.status || "—", cls: "info" };
      const width = Math.max(4, Math.min(100, (Number(t.weight) || 0) * 100 / 0.4));
      return el("div", {}, [
        el("div", { class: "chip-row" }, [
          el("span", { class: "sig-ticker tranche-chip", text: `${t.tranche}차` }),
          badge(meta.text, meta.cls),
          el("span", { class: "tiny num", text: t.price_ref ? fmtMoney(t.price_ref) : "—" }),
          el("span", { class: "tiny", text: `비중 ${fmtPct(t.weight)}` }),
          t.filled_at ? el("span", { class: "tiny", text: `체결 ${t.filled_at}` }) : null,
          t.cancelled_by ? badge(`취소 ${t.cancelled_by}`, "bad") : null,
        ]),
        el("div", { class: "tranche-bar", role: "img", "aria-label": `${t.tranche}차 비중 ${fmtPct(t.weight)}` },
          [el("span", { style: `width:${width}%` })]),
        t.note ? el("p", { class: "tiny", text: t.note }) : null,
      ]);
    })),
  ]));
  host.append(el("p", { class: "note-box", text:
    "3차 지정가가 초기 손절가보다 낮으면 무효입니다 — 손절로 다 팔고 같은 봉에서 다시 열리지 않게 하기 위해서입니다(3.5)." }));
  const trailing = position.trailing || {};
  host.append(el("div", { class: "metric-grid" }, [
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "손절 거리" }),
      el("div", { class: "metric-value", text: fmtPct(position.stop_distance) })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "손절가 대비 여력" }),
      el("div", { class: "metric-value", text: fmtPct(position.stop_headroom_pct) }),
      el("p", { class: "tiny", text: `손절가 ${fmtMoney(position.stop_price)}` })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "트레일링" }),
      el("div", { class: "metric-value", text: trailing.active ? "작동" : "대기" }),
      el("p", { class: "tiny", text: `최고가 대비 ${fmtPct(trailing.high_gain_pct)}` })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "분할익절 단계" }),
      el("div", { class: "metric-value", text: TRIM_LABEL[position.trim_stage] || position.trim_stage || "—" }),
      el("p", { class: "tiny", text: (position.trim_done || []).length ? `완료 ${position.trim_done.join(" · ")}` : "아직 없음" })]),
  ]));
  if (position.last_event_ko) host.append(el("p", { class: "tiny", text: `최근: ${position.last_event_ko}` }));
  return host;
}

function renderTicker(tickerDoc) {
  const host = clear($("ticker-body"));
  if (!tickerDoc) {
    host.append(emptyState("이 종목의 데이터 파일이 없습니다 — 오늘 신호 또는 워치리스트에 없습니다."));
    return;
  }
  const sig = tickerDoc.signal;
  const decision = (sig && (sig.decision || sig.type)) || null;
  const header = el("section", { class: "card" }, [
    el("h2", { text: "① 결론" }),
    el("div", { class: "sig-head" }, [
      decision ? el("span", { class: `badge ${decision === "BUY" ? "ok" : decision === "SELL" ? "bad" : "warn"}`,
        text: `${DECISION_LABEL[decision] || decision} ${decision}` }) : badge("신호 없음", "info"),
      el("span", { class: "sig-ticker", text: tickerDoc.ticker }),
      sig ? el("span", { class: "sig-score num", text: `${fmtNum(sig.entry_score, 1)}점` }) : null,
      sig ? badge(sig.confidence_label_ko || "신뢰도 미상", "info") : null,
    ]),
    el("p", { class: "tiny", text: [
      `정량 등급 ${(sig && sig.trade_grade) || "—"}`,
      `갱신 ${fmtKo(tickerDoc.as_of)}`,
      tickerDoc.watchlist && tickerDoc.watchlist.sector ? tickerDoc.watchlist.sector : null,
      tickerDoc.params_version,
    ].filter(Boolean).join(" · ") }),
    sig && sig.thesis_ko ? el("p", { class: "sig-why", text: sig.thesis_ko }) : el("p", { class: "empty", text: tickerDoc.last_signal || "최근 신호 없음" }),
    sig && sig.gate_summary && (sig.gate_summary.failed || []).length
      ? el("p", { class: "tiny", text: `통과하지 못한 게이트: ${sig.gate_summary.failed.join(" · ")}` }) : null,
  ]);

  const bars = el("section", { class: "card" }, [el("h2", { text: "② 대가별 점수" }), investorBars((sig && sig.investor_scores) || {}, sig && sig.conflict)]);
  // 신호 레코드의 valuation 이 우선이고, 없는 키는 워치리스트 근거(valuation_quality)로 채운다.
  const valuation = { ...((tickerDoc.valuation_quality) || {}), ...((sig && sig.valuation) || {}) };
  const bandNotes = [];
  if (sig && sig.target_band && sig.target_band.base_bar_date) {
    // v1.3.3 09-d: 채점 기준 봉을 발행 시점에 고정했다는 사실을 화면에 남긴다(나중 종가로 갈아타지 않는다).
    bandNotes.push(el("p", { class: "tiny", text: `목표 밴드 ${fmtMoney(sig.target_band.lo)}~${fmtMoney(sig.target_band.hi)}`
      + ` · 기준 봉 ${sig.target_band.base_bar_date} 종가 ${fmtMoney(sig.target_band.base_price)} (base_price 고정)` }));
  }
  const band = el("section", { class: "card" }, [el("h2", { text: "③ 밸류에이션 밴드" }), valuationBand(valuation), ...bandNotes]);
  const chart = el("section", { class: "card" }, [el("h2", { text: "④ 차트 (1년 주 1회 정규화)" }), svgChart(tickerDoc.chart)]);
  const news = el("section", { class: "card" }, [el("h2", { text: "⑤ 뉴스 판독" }), newsBlock(sig)]);
  const risk = el("section", { class: "card" }, [el("h2", { text: "⑥ 리스크 가드 · 근거 원문" }), riskBlock(sig, tickerDoc)]);
  const debate = el("section", { class: "card" }, [el("h2", { text: "⑦ 찬반 토론 (1R → 2R → 3R)" }), debateBlock(tickerDoc)]);
  const position = el("section", { class: "card" }, [el("h2", { text: "⑧ 보유 · 분할 · 익절" }), positionBlock(tickerDoc)]);
  host.append(header, bars, band, chart, news, risk, debate, position);
}

/* ────────────────────────── 성적표 ────────────────────────── */
function renderHoldout(summary) {
  const host = clear($("holdout-banner"));
  const holdout = (summary && summary.holdout) || {};
  const text = holdout.note_ko || `홀드아웃은 분기 1회만 열립니다 — 다음 개봉 ${holdout.next_open || "—"}`;
  host.append(el("div", { class: "banner is-warn" }, [
    el("strong", { text: "🔒 홀드아웃 잠금" }),
    el("span", { class: "tiny", text: text }),
  ]));
  if (holdout.holdout_start) {
    host.append(el("p", { class: "tiny", text: `잠금 구간 ${holdout.holdout_start} ~ ${holdout.holdout_end} · 평가 ${holdout.evaluations || 0}회` }));
  }
}

function renderFolds(summary) {
  const host = clear($("folds"));
  const folds = (summary && summary.folds) || {};
  const horizons = folds.horizons || {};
  const names = Object.keys(horizons);
  if (!names.length) {
    host.append(emptyState("폴드 요약 없음 — 분기 워크포워드가 아직 돌지 않았습니다."));
    return;
  }
  const table = el("table", {}, [
    el("caption", { text: "기간별 적격 폴드 — 6개월 보류를 숨기지 않는다(4.2.1·4.2.7)" }),
    el("thead", {}, [el("tr", {}, [el("th", { text: "기간" }), el("th", { class: "num", text: "적격 폴드" }),
      el("th", { text: "상태" }), el("th", { text: "폴드" })])]),
    el("tbody", {}, names.map((name) => {
      const h = horizons[name] || {};
      const ok = h.sufficient !== false;
      return el("tr", {}, [
        el("td", { text: `${name.toUpperCase()} (${h.horizon_td || "—"}거래일)` }),
        el("td", { class: "num", text: `${isNum(h.count) ? h.count : 0}개 (기준 ${h.min_folds || 3})` }),
        el("td", {}, [ok ? el("span", { class: "good", text: "보정 가능" }) : el("span", { class: "deferred", text: "보정 보류" })]),
        el("td", { class: "tiny wrap", text: (h.eligible_folds || []).join(" · ") || "—" }),
      ]);
    })),
  ]);
  host.append(table);
  if (folds.holdout_start) {
    host.append(el("p", { class: "tiny", text: `홀드아웃 ${folds.holdout_start} ~ ${folds.holdout_end} · 경계에서 잘린 폴드는 참고용으로만 표시` }));
  }
  (folds.blocked || []).forEach((name) => {
    host.append(el("p", { class: "tiny deferred", text: `${name.toUpperCase()} 보정 보류 — 적격 폴드가 3개 미만입니다.` }));
  });
}

function metricRow(label, value, note) {
  return el("div", { class: "row" }, [
    el("div", {}, [el("span", { text: label }), note ? el("p", { class: "tiny", text: note }) : null]),
    el("span", { class: "num", text: value }),
  ]);
}

function renderStrategy(summary, portfolio) {
  const host = clear($("strategy"));
  const accuracy = (summary && summary.accuracy) || {};
  const strategy = (summary && summary.strategy) || {};
  const p = portfolio || {};
  host.append(subBlock("신호 정확도 (방향이 맞았나)", [
    metricRow("3개월 Agreement", fmtNum(accuracy.agreement_3m, 1), "A/B/C 가중 평균(4.2.4)"),
    metricRow("IC (점수-수익률 순위상관)", fmtNum(accuracy.ic_3m, 2), "3건 미만이면 계산하지 않음"),
    metricRow("승률", fmtPct(accuracy.win_rate), "참고치 55% — 게이트 아님"),
    metricRow("평균 3개월 초과수익", fmtSignedPct(accuracy.mean_excess), accuracy.note_ko || "SPY 대비"),
  ]));
  host.append(subBlock("전략 (모의 계좌)", [
    metricRow("자산 지수(초기=1)", fmtNum(pick(p.equity_index, strategy.equity_index), 4), null),
    metricRow("현금 비중", fmtPct(pick(p.cash_pct, strategy.cash_pct)), null),
    metricRow("투자 비중", fmtPct(pick(p.invested_pct, strategy.invested_pct)), "평균 노출도"),
    metricRow("보유 종목", `${pick(p.n_positions, strategy.n_positions, 0)}개`, strategy.note_ko || null),
  ], null));
}

function metricValue(metric) {
  if (!isNum(metric.value)) return "—";
  const key = String(metric.key || "");
  if (key === "n_eff") return String(Math.round(metric.value));
  if (key === "tail_loss") return fmtPct(metric.value);
  return fmtNum(metric.value, 3);
}

function renderPilot(summary, pilotEval) {
  const host = clear($("pilot"));
  const pilot = (summary && summary.pilot) || {};
  const verdict = pilot.verdict || "보류";
  host.append(el("p", { class: "mos" }, [
    el("span", { class: verdict === "소액 파일럿 후보" ? "good" : "deferred", text: `판정: ${verdict}` }),
  ]));
  if (pilot.reason_ko) host.append(el("p", { class: "tiny", text: pilot.reason_ko }));
  if (isNum(pilot.n_eff) || isNum(pilot.n_records)) {
    // N_eff 는 체결·만기 표본, 발행 신호 수는 관찰 카드까지 포함한다 — 둘을 함께 보여 준다(4.7.3).
    host.append(el("p", { class: "mos" }, [
      el("span", { text: `유효 표본 ${isNum(pilot.n_eff) ? pilot.n_eff : "—"}` }),
      el("span", { class: "tiny", text: ` · 발행 신호 ${isNum(pilot.n_records) ? pilot.n_records : "—"}건`
        + (isNum(pilot.n_eligible) ? ` (체결·만기 ${pilot.n_eligible}건)` : "") }),
    ]));
  }
  const metrics = pilot.metrics || (pilotEval && pilotEval.metrics) || [];
  if (!metrics.length) {
    host.append(emptyState("파일럿 지표 없음"));
    return;
  }
  const table = el("table", {}, [
    el("caption", { text: "네 지표를 함께 본다 — 하나만 통과시키지 않는다(4.7.3)" }),
    el("thead", {}, [el("tr", {}, [el("th", { text: "지표" }), el("th", { class: "num", text: "값" }),
      el("th", { text: "기준" }), el("th", { text: "판정" })])]),
    el("tbody", {}, metrics.map((m) => el("tr", {}, [
      el("td", { class: "wrap" }, [el("span", { text: m.label_ko || m.key }),
        m.note_ko ? el("p", { class: "tiny", text: m.note_ko }) : null]),
      el("td", { class: "num", text: metricValue(m) }),
      el("td", { class: "tiny", text: m.threshold || "—" }),
      el("td", {}, [passMark(m.pass)]),
    ]))),
  ]);
  host.append(table);
  host.append(el("p", { class: "tiny", text: "N_eff 는 같은 종목이 63거래일 안에 겹치면 하나로 묶은 표본 수입니다." }));
}

function renderHistory(summary, records) {
  const host = clear($("history"));
  const rows = (summary && summary.signal_history) || [];
  const all = records && Array.isArray(records.records) ? records.records : [];
  if (!rows.length && !all.length) {
    host.append(emptyState("신호 이력 없음 — 아직 발행된 신호가 없습니다."));
    return;
  }
  const body = (rows.length ? rows : all).slice(0, 50).map((r) => el("tr", {}, [
    el("td", { class: "num", text: String(r.as_of || (r.signal_id || "").slice(0, 10)) }),
    el("td", { text: r.ticker || "—" }),
    el("td", { text: `종목별 ${isNum(r.revision) ? Number(r.revision) : 1}회차`
      + (isNum(r.revision) && r.revision > 1 && r.supersedes ? ` (${String(r.supersedes).slice(11)} 대체)` : "") }),
    el("td", { text: r.action || (r.quant_intent === "pending_buy" ? "체결 대기" : r.quant_intent === "no_action" ? "관찰" : (r.action || "—")) }),
    el("td", { class: "num", text: isNum(r.excess) ? fmtSignedPct(r.excess) : "—" }),
  ]));
  host.append(el("table", {}, [
    el("caption", { text: `최근 ${body.length}건 · 초과수익은 SPY 대비 3개월` }),
    el("thead", {}, [el("tr", {}, [el("th", { text: "발행일" }), el("th", { text: "티커" }),
      el("th", { text: "종목별 회차" }),
      el("th", { text: "행동" }), el("th", { class: "num", text: "3M 초과" })])]),
    el("tbody", {}, body),
  ]));
  host.append(el("p", { class: "tiny", text: "`action` 은 체결 이벤트에서만 나온다 — 발행 레코드에는 의도만 있다(4.7.1)." }));
}

function renderParams(summary) {
  const host = clear($("params"));
  const rows = (summary && summary.params_history) || [];
  if (!rows.length) {
    host.append(emptyState("파라미터 이력 없음"));
    return;
  }
  rows.slice().reverse().forEach((row) => {
    host.append(el("div", { class: "row" }, [
      el("div", {}, [el("strong", { text: row.version || "—" }),
        el("p", { class: "tiny", text: `이전 ${row.parent_version || "—"} · ${row.created_at || ""}` }),
        row.note_ko ? el("p", { class: "tiny", text: row.note_ko }) : null]),
      badge((row.gate_result && row.gate_result.status) || "active", "info"),
    ]));
  });
  host.append(el("p", { class: "tiny", text: "파라미터 변경은 PR 로만 반영한다(4.3 가드2)." }));
}

function verdictClass(verdict) {
  if (verdict === "토론 유효") return "ok";
  if (verdict === "토론 역효과") return "bad";
  return "warn";
}

function renderDebateEffect(debate) {
  const host = clear($("debate-effect"));
  const comparison = (debate && debate.comparison) || {};
  const pairs = Number(comparison.n_graded_pairs ?? comparison.divergent_pairs ?? 0);
  const nEff = Number(comparison.n_eff ?? 0);
  const enabled = comparison.auto_downgrade_enabled === true;
  const verdict = comparison.verdict || "표본 부족 — 판단 보류";

  host.append(el("p", {}, [badge(`자동 하향 ${enabled ? "ON" : "OFF"}`, enabled ? "warn" : "info"),
    el("span", { class: "tiny", text: " · 기본값은 false — 검증 전에는 표시·기록만 바꿉니다(3.11.8)" })]));
  // 페어 손익 표 — 두 계좌의 **순자산 차이**(policy-effect)와 섞지 않는다(4.8 · P1-3).
  host.append(el("p", { class: "tiny", text: "아래는 **페어 손익**입니다 — 계좌 전체의 순자산 차이는 위 `계좌 정책 순효과` 칸에서 따로 읽습니다." }));

  if (pairs < 30 || nEff < 15) {
    // 작은 표본의 숫자를 자신 있게 보여 주는 것이 가장 위험하다(5.4)
    host.append(el("p", { class: "big-verdict warn", text: `표본 ${pairs}건 — 판정 보류` }));
    host.append(el("p", { class: "tiny", text:
      `행동이 갈린 페어 30건·N_eff 15 이상이 되어야 판정합니다(현재 N_eff ${nEff}).` }));
    if (comparison.note_ko) host.append(el("p", { class: "tiny", text: comparison.note_ko }));
    host.append(el("p", { class: "note-box", text:
      "표본이 쌓이기 전에는 회피 손실·놓친 수익을 결론으로 읽지 않습니다. 지금 보이는 값은 기록일 뿐입니다." }));
    return;
  }

  host.append(el("p", { class: `big-verdict ${verdictClass(verdict)}`, text: verdict }));
  host.append(el("div", { class: "metric-grid" }, [
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "행동 갈린 페어" }),
      el("div", { class: "metric-value", text: `${pairs}건` }),
      el("p", { class: "tiny", text: `동일 행동 ${comparison.same_action_pairs ?? 0}건` })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "유효 표본 N_eff" }),
      el("div", { class: "metric-value", text: `${nEff}` }),
      el("p", { class: "tiny", text: "63거래일 클러스터" })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "회피 손실" }),
      el("div", { class: "metric-value", text: ppText(comparison.avoided_loss_pp) }),
      el("p", { class: "tiny", text: "안 산 게 옳았던 몫" })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "놓친 수익" }),
      el("div", { class: "metric-value", text: ppText(-Math.abs(Number(comparison.missed_gain_pp) || 0)) }),
      el("p", { class: "tiny", text: "안 산 게 손해였던 몫" })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "순효과(비용 후)" }),
      el("div", { class: "metric-value", text: ppText(comparison.net_after_cost_pp) }),
      el("p", { class: "tiny", text: `기준 +0.20%p · 2분기 연속` })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "회피 정확도" }),
      el("div", { class: "metric-value", text: fmtPct(comparison.avoidance_accuracy, 0) }),
      el("p", { class: "tiny", text: "보류 중 주가 하락 비율" })]),
  ]));
  const dd = comparison.drawdown || {};
  host.append(el("p", { class: "tiny", text:
    `방어력 · 최악 5% MDD quant ${fmtPct(dd.quant_tail_5pct)} / 토론 ${fmtPct(dd.debate_tail_5pct)} · `
    + `${dd.defense_improved ? "개선" : "개선 없음"}` }));
  const quarters = comparison.quarters || [];
  if (quarters.length) {
    host.append(el("table", {}, [
      el("caption", { text: "분기별 판정 — 사전에 못 박은 기준으로만 읽는다(4.8.2)" }),
      el("thead", {}, [el("tr", {}, [el("th", { text: "분기" }), el("th", { class: "num", text: "페어" }),
        el("th", { class: "num", text: "비용 후" }), el("th", { text: "판정" })])]),
      el("tbody", {}, quarters.map((q) => el("tr", {}, [
        el("td", { text: q.quarter || "—" }),
        el("td", { class: "num", text: `${q.n_graded ?? 0}` }),
        el("td", { class: "num", text: ppText(q.net_after_cost != null ? q.net_after_cost * 100 : null) }),
        el("td", { class: "wrap", text: q.verdict || "—" }),
      ]))),
    ]));
  }
}

function ppText(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const num = Number(value);
  return `${num > 0 ? "+" : ""}${num.toFixed(2)}%p`;
}

/**
 * 계좌 정책 순효과(P1-3) — `quant_only` 와 `codex_debated` **두 모의 계좌의 순자산 차이**다.
 * 페어 손익(회피손실·놓친수익)과 다른 질문이므로 한 칸으로 섞지 않는다.
 */
function renderPolicyEffect(debate) {
  const host = clear($("policy-effect"));
  const comparison = (debate && debate.comparison) || {};
  const policy = comparison.policy_diff_pp;
  const pair = comparison.pair_summary || {};
  host.append(el("h3", { text: "계좌 정책 순효과 (두 모의 계좌의 순자산 차이)" }));
  if (!isNum(policy)) {
    host.append(el("p", { class: "big-verdict warn", text: "산출 전 — 표시 보류" }));
    host.append(el("p", { class: "tiny", text:
      "두 계좌(정량 단독 / 토론 포함)의 순자산 곡선이 쌓여야 계산됩니다. "
      + "페어 손익과 다른 값이라 여기에 대신 쓰지 않습니다(4.8 · P1-3)." }));
  } else {
    host.append(el("p", { class: "big-verdict " + (policy > 0 ? "ok" : policy < 0 ? "bad" : "warn"),
      text: `정책 순효과 ${ppText(policy)}` }));
    host.append(el("p", { class: "tiny", text: "비용 후 · 같은 기간 두 계좌 차이" }));
  }
  host.append(el("div", { class: "metric-grid" }, [
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "페어 회피 손실" }),
      el("div", { class: "metric-value", text: ppText(pair.avoided_loss_pp ?? comparison.avoided_loss_pp) }),
      el("p", { class: "tiny", text: "안 산 게 옳았던 몫(페어)" })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "페어 놓친 수익" }),
      el("div", { class: "metric-value", text: ppText(-Math.abs(Number(pair.missed_gain_pp ?? comparison.missed_gain_pp ?? 0))) }),
      el("p", { class: "tiny", text: "안 산 게 손해였던 몫(페어)" })]),
    el("div", { class: "metric-cell" }, [el("div", { class: "metric-label", text: "페어 비용 후" }),
      el("div", { class: "metric-value", text: ppText(pair.net_after_cost_pp ?? comparison.net_after_cost_pp) }),
      el("p", { class: "tiny", text: `페어 ${pair.pairs ?? comparison.n_graded_pairs ?? 0}건` })]),
  ]));
  // 소급 페어는 **표시만** 하고 비교에서는 뺀다(P0-2) — 숫자는 `counts` 쪽과 같은 값이다.
  const retroactiveDropped = Number(comparison.retroactive_dropped
    || (comparison.counts || {}).retroactive_dropped || 0);
  if (retroactiveDropped > 0) {
    host.append(el("p", { class: "tiny", text:
      `소급 페어 ${retroactiveDropped}건은 비교에서 뺐습니다 — 편입 검증보다 먼저 체결된 주문입니다.` }));
  }
}

function renderLivePositions(portfolio, health) {
  const host = clear($("live-positions"));
  const positions = (portfolio && portfolio.positions) || [];
  const h = health || {};
  host.append(el("p", { class: "tiny", text:
    `보유 ${positions.length}종목 · 분할 대기 ${h.tranches_pending ?? 0}건 · 손절 ${h.stop_count ?? 0}건 · `
    + `1/3 익절(TRIM) ${h.trim_count ?? 0}건` }));
  // 두 카운터는 **다른 사건**이다(원장 `live.write_health` 계약) — 합치지 않는다.
  host.append(el("p", { class: "tiny", text:
    `손절 후 재진입 차단 ${h.stop_reentry_blocked_count ?? 0}건(손절이 난 종목·세션의 신규 1차 진입 차단) · `
    + `같은 봉 분할 취소 ${h.tranche_cancel_count ?? 0}건(잔여 2·3차 주문 취소)` }));
  const forced = h.forced_exits || {};
  const forcedRows = Object.keys(forced).map((ticker) => `${ticker} ${forced[ticker]}`);
  if (forcedRows.length) {
    host.append(el("p", { class: "tiny", text:
      `강제 청산·익절 판정: ${forcedRows.join(" · ")}(TRIM 은 1/3 익절 주문으로 원장에 남습니다)` }));
  }
  if (!positions.length) {
    host.append(emptyState("모의 계좌에 보유 종목이 없습니다 — 1차 체결 전이면 정상입니다."));
    host.append(el("p", { class: "tiny", text:
      `현금 비중 ${fmtPct(portfolio && portfolio.cash_pct)} · 투자 비중 ${fmtPct(portfolio && portfolio.invested_pct)}` }));
    return;
  }
  const table = el("table", {}, [
    el("caption", { text: "1·2·3차 분할 · 손절 여력 · 분할익절 단계" }),
    el("thead", {}, [el("tr", {}, [el("th", { text: "종목" }), el("th", { text: "분할" }),
      el("th", { class: "num", text: "비중" }), el("th", { class: "num", text: "손절 여력" }),
      el("th", { text: "익절" })])]),
    el("tbody", {}, positions.map((p) => {
      const chips = (p.tranches || []).map((t) => {
        const meta = TRANCHE_STATUS[t.status] || { text: t.status, cls: "info" };
        return el("span", { class: "tiny", text: `${t.tranche}차 ${meta.text}${t.cancelled_by ? `(${t.cancelled_by})` : ""} ` });
      });
      return el("tr", {}, [
        el("td", { class: "wrap" }, [el("button", {
          type: "button", class: "linkish", text: p.ticker,
          onclick: () => setRoute("ticker", p.ticker),
        })]),
        el("td", { class: "wrap" }, chips),
        el("td", { class: "num", text: fmtPct(p.weight) }),
        el("td", { class: "num", text: fmtPct(p.stop_headroom_pct) }),
        el("td", { class: "tiny wrap", text: TRIM_LABEL[p.trim_stage] || p.trim_stage || "—" }),
      ]);
    })),
  ]);
  host.append(table);
  if ((h.blocked_reentries || []).length) {
    host.append(el("p", { class: "tiny", text:
      `최근 차단: ${h.blocked_reentries.slice(0, 3).map((b) => `${b.ticker} ${b.tranche}차(${b.cancelled_by})`).join(" · ")}` }));
  }
}

function renderLedger(summary, portfolio, records, pilotEval, debate, health) {
  renderHoldout(summary);
  renderFolds(summary);
  renderStrategy(summary, portfolio);
  renderPilot(summary, pilotEval);
  renderHistory(summary, records);
  renderParams(summary);
  renderPolicyEffect(debate);
  renderDebateEffect(debate);
  renderLivePositions(portfolio, health);
}

/* ────────────────────────── 레이더 ────────────────────────── */
function renderRadar(radar, watch, config, today) {
  const host = clear($("radar-watermark"));
  const mark = (radar && radar.watermark_ko) || "45일 전 분기말 기준";
  host.append(el("div", { class: `banner ${radar && radar.stale ? "is-warn" : "is-ok"}` }, [
    el("strong", { text: mark }),
    el("span", { class: "tiny", text: [
      radar && radar.period_end ? `분기말 ${radar.period_end}` : "분기말 미확인",
      radar && radar.stale_days !== null && radar && radar.stale_days !== undefined ? `지연 ${radar.stale_days}일` : null,
      `제출 기한 +${(radar && radar.lag_days) || 45}일 반영`,
    ].filter(Boolean).join(" · ") }),
  ]));
  if (radar && radar.stale) host.append(el("p", { class: "tiny", text: "13F 는 지연 표시만 하고 신호에는 쓰지 않습니다(2.7)." }));

  const radarHost = clear($("radar"));
  const filers = (radar && radar.filers) || [];
  if (!filers.length) {
    radarHost.append(emptyState((radar && radar.note_ko) || "13F 제출 없음 — 레이더는 비어 있는 것이 정상입니다."));
  } else {
    filers.forEach((filer) => {
      radarHost.append(el("h3", { class: "tiny", text: `${filer.filer_name || filer.filer_cik} · ${filer.period_end || ""} · 보유 ${filer.n_holdings || 0}종목` }));
      const changes = (filer.changes || []).filter((c) => c.kind !== "unchanged").slice(0, 12);
      if (!changes.length) {
        radarHost.append(emptyState("이번 분기 유의미한 변동 없음"));
        return;
      }
      const table = el("table", {}, [
        el("caption", { text: "신규 편입·확대·축소·청산 (평가액 기준)" }),
        el("thead", {}, [el("tr", {}, [el("th", { text: "종목" }), el("th", { text: "변화" }),
          el("th", { class: "num", text: "평가액" }), el("th", { class: "num", text: "변화율" })])]),
        el("tbody", {}, changes.map((c) => el("tr", {}, [
          el("td", { class: "wrap" }, [c.issuer || c.cusip,
            c.in_watchlist ? el("span", {}, [el("br"), badge("워치리스트", "info")]) : null]),
          el("td", { text: c.kind_ko || c.kind }),
          el("td", { class: "num", text: fmtMoney(c.value_usd) }),
          el("td", { class: "num", text: isNum(c.delta_pct) ? fmtSignedPct(c.delta_pct) : "신규" }),
        ]))),
      ]);
      radarHost.append(table);
    });
  }

  const diffHost = clear($("watch-diff"));
  const entered = (watch && watch.entered) || [];
  const exited = (watch && watch.exited) || [];
  if (!entered.length && !exited.length) {
    diffHost.append(emptyState(`이번 주 변동 없음 — 워치리스트 ${pick(watch && watch.count, 0)}종목 유지.`));
  } else {
    entered.forEach((e) => diffHost.append(el("div", { class: "row" }, [
      el("div", {}, [el("strong", { text: `+ ${e.ticker}` }), el("p", { class: "tiny", text: e.reason_ko || "" })]),
      badge("편입", "ok"),
    ])));
    exited.forEach((e) => diffHost.append(el("div", { class: "row" }, [
      el("div", {}, [el("strong", { text: `− ${e.ticker}` }), el("p", { class: "tiny", text: e.reason_ko || "" })]),
      badge("편출", "bad"),
    ])));
  }
  ((watch && watch.valuation_suspended) || []).forEach((s) => {
    diffHost.append(el("p", { class: "tiny", text: `${s.ticker}: 가치평가 보류 업종(${s.reason || "SECTOR_FORMULA_PENDING"}) — 워치리스트 제외.` }));
  });

  const calHost = clear($("calendar"));
  const t = today || {};
  if (t.as_of) {
    calHost.append(el("p", { class: "tiny", text: `기준일 ${t.as_of} · 다음 거래일 주문 마감은 발행 정보의 order_cutoff_at 을 따릅니다.` }));
  }
  calHost.append(emptyState("실적·공시 캘린더는 8-K Item 2.02 이력으로 추정하지만 아직 화면에 연결하지 않았습니다(정직 표기)."));

  const rulesHost = clear($("rules"));
  const groups = (config && config.groups) || [];
  if (!groups.length) {
    rulesHost.append(emptyState("규칙 요약 없음 — config/params_v001.yaml 을 읽지 못했습니다."));
  }
  groups.forEach((group) => {
    rulesHost.append(el("h3", { class: "tiny", text: group.name }));
    rulesHost.append(el("table", {}, [
      el("caption", { text: `${group.name} — 현재 파라미터 (${(config && config.params_version) || "—"})` }),
      el("tbody", {}, (group.items || []).map((item) => el("tr", {}, [
        el("td", { class: "wrap", text: item.label_ko || item.key }),
        el("td", { class: "num", text: item.value === undefined ? "—" : String(item.value) }),
      ]))),
    ]));
  });
  const vetoRules = (config && config.veto_rules) || [];
  if (vetoRules.length) {
    rulesHost.append(el("h3", { class: "tiny", text: "리스크 가드 12종 (고정)" }));
    rulesHost.append(el("table", {}, [
      el("caption", { text: "Veto 코드 — 발동해도 신호를 지우지 않고 사유를 남긴다" }),
      el("thead", {}, [el("tr", {}, [el("th", { text: "코드" }), el("th", { text: "이름" }), el("th", { text: "차단 사유" })])]),
      el("tbody", {}, vetoRules.map((v) => el("tr", {}, [
        el("td", { text: v.code }), el("td", { class: "tiny wrap", text: v.name || "" }),
        el("td", { class: "tiny wrap", text: v.message_ko || "" }),
      ]))),
    ]));
  }
}

/* ────────────────────────── 라우팅 ────────────────────────── */
function setRoute(route, ticker) {
  const next = ROUTES.includes(route) ? route : "today";
  state.route = next;
  if (ticker) state.ticker = ticker;
  if (next === "ticker" && !state.ticker && state.tickers.length) state.ticker = state.tickers[0];
  const hash = next === "ticker" && state.ticker ? `#ticker/${state.ticker}` : `#${next}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  ROUTES.forEach((name) => {
    const view = $(`view-${name}`);
    const tab = $(`tab-${name}`);
    const active = name === next;
    if (view) view.hidden = !active;
    if (tab) tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (next === "ticker") loadTicker(state.ticker);
  window.scrollTo({ top: 0, behavior: "auto" });
}

async function loadTicker(ticker) {
  if (!ticker) return;
  const select = $("ticker-select");
  if (select && select.value !== ticker) select.value = ticker;
  const doc = await loadJson(`tickers/${ticker}.json`);
  renderTicker(doc);
}

function parseHash() {
  const raw = (location.hash || "#today").replace(/^#/, "");
  const [route, ticker] = raw.split("/");
  if (route === "ticker" && ticker) state.ticker = decodeURIComponent(ticker);
  return ROUTES.includes(route) ? route : "today";
}

function fillTickerSelect(today, watch) {
  const select = $("ticker-select");
  if (!select) return;
  clear(select);
  const seen = new Set();
  const add = (ticker, label) => {
    if (!ticker || seen.has(ticker)) return;
    seen.add(ticker);
    select.append(el("option", { value: ticker, text: label ? `${ticker} · ${label}` : ticker }));
  };
  ((today && today.signals) || []).forEach((s) => add(s.ticker, DECISION_LABEL[s.decision || s.type] || ""));
  ((watch && watch.watchlist) || []).forEach((w) => add(w.ticker, w.sector || ""));
  state.tickers = Array.from(seen);
  if (!state.tickers.length) {
    select.append(el("option", { value: "", text: "종목 없음" }));
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.value = state.ticker && seen.has(state.ticker) ? state.ticker : state.tickers[0];
  state.ticker = select.value;
  select.addEventListener("change", (event) => setRoute("ticker", event.target.value));
}

/* ────────────────────────── 시작 ────────────────────────── */
async function boot() {
  // 샘플 화면은 저장소의 sample-data 로 열고, 실데이터는 **검증된 발행 세트**로 연다(감사 12).
  const loaded = state.sampleMode
    ? { docs: coreDocs(await Promise.all(CORE_FILES.map(loadJson))), info: { source: "sample" } }
    : await loadPublicSet();
  const docs = loaded.docs || {};
  const { meta, today, folds, watch, summary, records, portfolio, radar, config,
    debate, health, notif } = {
    meta: docs["meta.json"],
    today: docs["signals/today.json"],
    folds: docs["folds.json"],
    watch: docs["watchlist.json"],
    summary: docs["ledger/summary.json"],
    records: docs["ledger/records.json"],
    portfolio: docs["ledger/portfolio.json"],
    radar: docs["radar/13f.json"],
    config: docs["config.json"],
    debate: docs["debate.json"],
    health: docs["health.json"],
    notif: docs["notifications.json"],
  };
  state.core = { meta, today, folds, watch, summary, records, portfolio, radar, config,
    debate, health, notif, foldsOnly: folds };
  state.bundle = loaded.info || null;

  renderStatus(meta);
  renderFreshness(meta);
  renderDataStatus(meta, health);
  renderNotifyBanner(notif, debate, today);
  renderRegime(meta);
  renderToday(today, config);
  renderLedger(summary, portfolio, records, (summary && summary.pilot) || null, debate, health);
  renderRadar(radar, watch, config, today);
  fillTickerSelect(today, watch);
  renderBundle(loaded.info || { source: "direct", bundle_version: meta && meta.bundle_version });

  if (!state.bound) {
    state.bound = true;
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => setRoute(tab.dataset.route));
    });
    window.addEventListener("hashchange", () => setRoute(parseHash()));
  }

  const route = parseHash();
  setRoute(route, state.ticker);
}

// 서비스워커 등록 — 오프라인 열람·발행 세트 캐시(P2-1). 실패해도 화면은 그대로 동작한다.
registerServiceWorker();
// 앱 설치 버튼 — beforeinstallprompt 를 받은 뒤에만 보인다.
setupInstallButton();

boot().catch((err) => {
  const host = $("signals");
  if (host) {
    clear(host);
    host.append(emptyState(`데이터를 불러오지 못했습니다: ${err && err.message ? err.message : err}`));
  }
  const node = $("status-badge");
  if (node) {
    node.textContent = "데이터 오류";
    node.className = "badge bad";
  }
});
