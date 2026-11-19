/* Phase 1 대시보드 초안 — 정적 JSON 로더 (빌드 도구 없이 바로 동작).
 * 데이터는 `data/`(빌드 산출물) 에서 읽고, 없으면 `sample-data/` 로 폴백한다.
 * 공개 산출물에는 원시 OHLC·뉴스 본문·보유 정보가 없다(2.2.4).
 */
const FILES = ["meta.json", "folds.json", "watchlist.json", "signals/today.json", "ledger/summary.json"];

async function loadJson(name) {
  for (const base of ["./data/", "./sample-data/"]) {
    try {
      const r = await fetch(base + name, { cache: "no-cache" });
      if (r.ok) return await r.json();
    } catch (_) { /* 다음 경로 */ }
  }
  return null;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function badge(text, kind) {
  return el("span", { class: `tag ${kind || ""}`, text });
}

function gradeKind(type) {
  if (!type) return "";
  if (type.startsWith("BUY") || type === "STRONG_BUY") return "buy";
  if (type === "WATCH") return "watch";
  if (type.startsWith("TRIM") || type === "SELL") return "sell";
  return "";
}

function renderMeta(meta) {
  const badgeEl = document.getElementById("status-badge");
  const status = (meta && meta.status) || "sample";
  badgeEl.textContent = status === "normal" ? "데이터 정상" : `데이터 ${status}`;
  badgeEl.classList.add(status === "normal" ? "ok" : "warn");
  document.getElementById("regime").textContent =
    meta && meta.regime ? `레짐: ${meta.regime}` : "레짐 정보 없음";
  const bits = [];
  if (meta && meta.params_version) bits.push(meta.params_version);
  if (meta && meta.data_cutoff_at) bits.push(`마감 ${meta.data_cutoff_at}`);
  if (meta && meta.snapshot_id) bits.push(meta.snapshot_id.slice(0, 18));
  document.getElementById("meta").textContent = bits.join(" · ");
  document.getElementById("build").textContent =
    meta && meta.generated_at ? `빌드 ${meta.generated_at}` : "";
}

function renderFolds(folds) {
  const host = document.getElementById("folds");
  host.replaceChildren();
  if (!folds || !folds.horizons) {
    host.append(el("p", { class: "empty", text: "폴드 요약 파일이 없습니다." }));
    return;
  }
  const table = el("table");
  const head = el("tr", {}, [el("th", { text: "기간" }), el("th", { text: "적격 폴드" }),
    el("th", { text: "상태" }), el("th", { text: "만기" })]);
  table.append(head);
  for (const [name, h] of Object.entries(folds.horizons)) {
    const mats = (h.folds || []).filter((f) => f.eligible).map((f) => f.maturity).join(", ");
    table.append(el("tr", {}, [
      el("td", { text: name }),
      el("td", { class: "num", text: `${h.count}개 (${(h.eligible_folds || []).join("·")})` }),
      el("td", {}, [badge(h.sufficient ? "정상" : "보정 보류", h.sufficient ? "buy" : "watch")]),
      el("td", { class: "tiny num", text: mats }),
    ]));
  }
  host.append(table);
  if (folds.holdout_start) {
    host.append(el("p", { class: "tiny",
      text: `홀드아웃 ${folds.holdout_start} ~ ${folds.holdout_end} · 보정 금지(분기 1회 평가)` }));
  }
}

function renderSignals(today) {
  const host = document.getElementById("signals");
  host.replaceChildren();
  const signals = (today && today.signals) || [];
  if (!signals.length) {
    host.className = "empty";
    host.textContent = (today && today.notes) || "오늘 기준을 넘은 신호 없음 — 차단 사유는 다음 단계에서 표시됩니다.";
    return;
  }
  host.className = "";
  for (const s of signals) {
    const row = el("div", { class: "row" });
    const left = el("div", {}, [
      el("div", {}, [badge(s.type || "NONE", gradeKind(s.type)), el("span", { class: "ticker", text: ` ${s.ticker}` })]),
      el("div", { class: "tiny", text: s.thesis_ko || s.note || "" }),
    ]);
    const right = el("div", { class: "num", text: s.entry_score != null ? `${s.entry_score}점` : "" });
    row.append(left, right);
    host.append(row);
  }
}

function renderWatchlist(list) {
  const host = document.getElementById("watchlist");
  host.replaceChildren();
  const rows = (list && list.watchlist) || [];
  if (!rows.length) {
    host.className = "empty";
    host.textContent = "통과 종목 0개 — 최소 개수를 채우지 않는 것이 정상입니다.";
    return;
  }
  host.className = "";
  const table = el("table");
  table.append(el("tr", {}, [el("th", { text: "티커" }), el("th", { text: "섹터" }),
    el("th", { text: "Q" }), el("th", { text: "V" }), el("th", { text: "G" }), el("th", { text: "정렬" })]));
  for (const w of rows) {
    table.append(el("tr", {}, [
      el("td", { class: "ticker", text: w.ticker }),
      el("td", { class: "tiny", text: w.sector || "" }),
      el("td", { class: "num", text: w.q ?? "—" }),
      el("td", { class: "num", text: w.v ?? "—" }),
      el("td", { class: "num", text: w.g ?? "—" }),
      el("td", { class: "num", text: w.rank_score ?? "—" }),
    ]));
  }
  host.append(table);
}

(async function main() {
  const [meta, folds, watchlist, today, ledger] = await Promise.all(FILES.map(loadJson));
  renderMeta(meta);
  renderFolds(folds);
  renderSignals(today);
  renderWatchlist(watchlist);
  if (ledger && ledger.agreement) {
    document.getElementById("signals").append(
      el("p", { class: "tiny", text: `누적 Agreement ${ledger.agreement} · 승률 ${ledger.win_rate ?? "—"}` })
    );
  }
})();
