/* web/sw.js — 발행 세트 단위 캐시 · 원자적 세트 전환 · 오프라인 열람 (P2-1, 감사 12, 기획서 §5.6).
 *
 * [고정]
 * - 앱 셸(index·app.js·styles.css·manifest·icon)은 `shell-<버전>` 캐시에 한 번 담는다.
 * - 데이터는 **발행 세트(manifest) 단위**로만 캐시한다. 세트를 바꾸는 방법은 하나뿐이다:
 *   `deploy_manifest.json` 을 읽어 **핵심 세트 전체**를 받고, 파일마다 sha256 을 검증한 뒤,
 *   전부 통과했을 때만 `activeSet` 을 **한 번에** 바꾼다. 하나라도 실패하면 직전 세트를 그대로 둔다.
 * - 새 세트로 전환하기 **전에는** 직전 세트를 지우지 않는다. 전환이 끝난 뒤에만 다른 `set-*` 를 지운다.
 * - `meta.json` 만 보고 세트를 바꾸지 않는다(감사 12 — 병렬 fetch 에서 구 health.json 이 새 세트에 섞였다).
 * - 파일 자신의 `bundle_version`·`?v=` 가 현재 세트와 다르면 **저장하지 않는다**(뒤늦은 구 버전 응답 차단).
 * - 네트워크가 없으면 마지막 성공 세트로 화면을 열고, 클라이언트는 "캐시된 세트" 배지를 띄운다.
 *
 * 캐시 키를 바꿀 때는 `SW_VERSION` 과 index.html 의 `?v=` 를 함께 올린다.
 *
 * [감사 09] 토론 상세는 `data/debate_details/{report_id}.json` 로 나뉜다. 이 파일들은
 * `CORE_FILES` 가 아니다(종목 화면을 열 때 처음 필요해진다) — 그래도 **같은 발행 세트**로 취급한다:
 *   - `handleData` 가 `/data/` 아래 모든 경로를 받고, `cacheIfCurrent` 가 매니페스트의 sha256 과
 *     대조한 뒤에만 세트 캐시에 넣는다(온라인·오프라인 모두 같은 규칙).
 *   - 매니페스트에 없는 상세 경로는 캐시하지 않는다 — 낡은 세트가 새 파일을 섞지 않는다.
 *   - 세트를 바꿀 때 `trimSets` 가 옛 세트 캐시를 지우므로 상세도 함께 정리된다.
 */
"use strict";

/** 서비스워커·셸 캐시 키 — sw.js 가 바뀌면 올린다. */
const SW_VERSION = "phase4-6";
/** index.html 의 `?v=` 와 **같은** 값 — 셸 자산 URL 은 html 이 소유한다(둘을 함께 올린다). */
const ASSET_VERSION = "phase4-6";
const SHELL_CACHE = `shell-${SW_VERSION}`;
const SHELL_PREFIX = "shell-";
const SET_PREFIX = "set-";
const DATA_PREFIX = "./data/";
const MANIFEST = "deploy_manifest.json";
/** 발행 세트에서 **반드시 함께** 받아야 하는 핵심 파일(app.js 의 CORE_FILES 와 같다). */
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
  "debate.json",
  "health.json",
  "notifications.json",
];
const SHELL_ASSETS = [
  "./",
  "./index.html",
  `./styles.css?v=${ASSET_VERSION}`,
  `./app.js?v=${ASSET_VERSION}`,
  "./manifest.json",
  "./icon.svg",
];
/** 파일 이름만으로 셸 요청을 식별한다(하위 경로 배포에서도 동작). */
const SHELL_FILES = ["index.html", "styles.css", "app.js", "manifest.json", "icon.svg", "sw.js"];

/** 현재 발행 세트 버전(`deploy_manifest.json` 의 `bundle_version`). 모르면 null. */
let activeSet = null;
/** 현재 세트의 파일 해시 목록(`manifest.files`). */
let activeFiles = null;
/** 마지막 응답이 네트워크였는지 캐시였는지 — 클라이언트가 배지로 표시한다. */
let lastServed = "network";

const setCacheName = (version) => `${SET_PREFIX}${version || "unknown"}`;
const dataUrl = (name) => `${DATA_PREFIX}${name}`;
/** 캐시 키·fetch 는 **절대 URL** 이어야 한다(상대 URL 로 `new Request` 하면 예외). */
const absoluteUrl = (name) => new URL(dataUrl(name), self.location.href).href;
const canonicalRequest = (name) => new Request(absoluteUrl(name));
const jsonResponse = (text) =>
  new Response(text, { headers: { "Content-Type": "application/json; charset=utf-8" } });

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .catch(() => undefined)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    self.clients
      .claim()
      // [감사 09] 셸 캐시는 **지금 버전 하나만** 남긴다. 예전 셸이 남아 있으면
      // index.html 이 같은 `?v=` 로 요청하는 app.js 를 **옛 캐시**에서 돌려줄 수 있다.
      // install 이 새 셸을 이미 채운 뒤에만 부르므로 오프라인 낙폭은 없다.
      .then(() => trimShells()),
  );
});

/** 현재 버전이 아닌 `shell-*` 캐시를 지운다 — 새 셸이 준비된 뒤에만. */
async function trimShells() {
  const names = await caches.keys();
  await Promise.all(
    names
      .filter((name) => name.startsWith(SHELL_PREFIX) && name !== SHELL_CACHE)
      .map((name) => caches.delete(name)),
  );
}

/** 현재 세트가 아닌 set-* 캐시를 지운다 — **전환이 끝난 뒤에만** 부른다. */
async function trimSets(keep) {
  const names = await caches.keys();
  await Promise.all(
    names
      .filter((name) => name.startsWith(SET_PREFIX) && name !== setCacheName(keep))
      .map((name) => caches.delete(name)),
  );
}

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function versionedUrl(name, version, hash) {
  const suffix = hash ? `&h=${String(hash).slice(0, 16)}` : "";
  return `${dataUrl(name)}?v=${encodeURIComponent(version)}${suffix}`;
}

/** 한 파일을 새 세트용으로 받아 **해시까지 검증**한다. 검증 실패는 예외다. */
async function fetchVerified(name, entry, version) {
  const url = new URL(versionedUrl(name, version, entry && entry.sha256), self.location.href).href;
  const response = await fetch(new Request(url, { cache: "no-store" }));
  if (!response || !response.ok) throw new Error(`HTTP ${response ? response.status : 0} ${name}`);
  const text = await response.text();
  const digest = await sha256Hex(text);
  if (entry && entry.sha256 && digest !== entry.sha256) throw new Error(`hash mismatch ${name}`);
  let body = null;
  try {
    body = JSON.parse(text);
  } catch (err) {
    body = null;
  }
  // 버전을 담는 파일(meta·health)은 **세트 버전도** 대조한다 — 해시가 같아도 버전이 어긋나면 거부.
  if (body && body.bundle_version && body.bundle_version !== version) {
    throw new Error(`version mismatch ${name}`);
  }
  return { name, text, body };
}

/**
 * 새 발행 세트를 준비한다 — 핵심 세트 전체를 받고 검증한 뒤에만 `activeSet` 을 바꾼다.
 * 실패하면 캐시도 `activeSet` 도 그대로 둔다(직전 완전한 세트 폴백).
 */
async function prepareSet() {
  const started = Date.now();
  const manifestResponse = await fetch(
    new Request(new URL(dataUrl(MANIFEST), self.location.href).href, { cache: "no-store" }));
  if (!manifestResponse || !manifestResponse.ok) throw new Error("manifest fetch failed");
  const manifest = await manifestResponse.json();
  if (!manifest || !manifest.bundle_version || !manifest.files) throw new Error("manifest invalid");
  const version = manifest.bundle_version;
  const missing = CORE_FILES.filter((name) => !manifest.files[name]);
  if (missing.length) throw new Error(`manifest missing core files: ${missing.join(",")}`);
  const previous = activeSet;
  const entries = await Promise.all(CORE_FILES.map((name) => fetchVerified(name, manifest.files[name], version)));
  // 여기까지 왔으면 **전부 검증됐다** — 이제 전환한다.
  const cacheName = setCacheName(version);
  await caches.delete(cacheName); // 같은 이름의 반쪽 캐시를 먼저 비운다(부분 세트 방지)
  const cache = await caches.open(cacheName);
  const manifestText = JSON.stringify(manifest);
  await cache.put(canonicalRequest(MANIFEST), jsonResponse(manifestText));
  for (const entry of entries) {
    await cache.put(canonicalRequest(entry.name), jsonResponse(entry.text));
  }
  activeSet = version;
  activeFiles = manifest.files;
  lastServed = "network";
  await trimSets(version); // 전환이 끝난 뒤에만 직전 세트를 지운다
  return {
    type: "set-activated",
    ok: true,
    sw_version: SW_VERSION,
    bundle_version: version,
    previous_bundle_version: previous,
    mismatch: Boolean(previous && previous !== version),
    file_count: entries.length,
    elapsed_ms: Date.now() - started,
    files: entries.map((entry) => ({ name: entry.name, text: entry.text })),
  };
}

/** 직전 **완전한** 세트를 돌려준다(네트워크 실패 폴백). */
async function readActiveSet() {
  if (!activeSet) return { type: "set-read", ok: false, bundle_version: null, files: [] };
  const cache = await caches.open(setCacheName(activeSet));
  const files = [];
  for (const name of CORE_FILES) {
    const hit = await cache.match(canonicalRequest(name));
    if (!hit) return { type: "set-read", ok: false, bundle_version: activeSet, files: [] };
    files.push({ name, text: await hit.text() });
  }
  lastServed = "cache";
  return { type: "set-read", ok: true, bundle_version: activeSet, files };
}

/** 세트에 포함된 파일을 **요청의 세트 버전과 대조**한 뒤에만 저장한다(뒤늦은 구 응답 차단). */
async function cacheIfCurrent(request, text) {
  if (!activeSet) return false;
  const url = new URL(request.url);
  const requested = url.searchParams.get("v");
  if (requested && requested !== activeSet) return false;
  let body = null;
  try {
    body = JSON.parse(text);
  } catch (err) {
    body = null;
  }
  if (body && body.bundle_version && body.bundle_version !== activeSet) return false;
  const name = url.pathname.split("/data/").pop();
  const entry = activeFiles && activeFiles[name];
  if (entry && entry.sha256) {
    const digest = await sha256Hex(text);
    if (digest !== entry.sha256) return false; // 매니페스트가 기대한 바이트가 아니다
  }
  const cache = await caches.open(setCacheName(activeSet));
  await cache.put(canonicalRequest(name), new Response(text,
    { headers: { "Content-Type": "application/json; charset=utf-8" } }));
  return true;
}

async function cachedFile(name) {
  if (!activeSet) return null;
  const cache = await caches.open(setCacheName(activeSet));
  return cache.match(canonicalRequest(name));
}

async function handleData(request) {
  const url = new URL(request.url);
  const name = url.pathname.split("/data/").pop();
  const cached = await cachedFile(name);
  const requested = url.searchParams.get("v");
  // 세트 버전을 붙여 온 요청은 **그 세트의 캐시**로 답한다(온라인에서도 화면이 세트를 섞지 않게).
  if (cached && activeSet && requested === activeSet) {
    lastServed = "cache";
    return cached;
  }
  try {
    const response = await fetch(request);
    if (!response || !response.ok) {
      if (cached) {
        lastServed = "cache";
        return cached;
      }
      return response;
    }
    const text = await response.clone().text();
    const stored = await cacheIfCurrent(request, text);
    if (!stored && cached) {
      // 요청/세트와 다른 버전의 응답이다 — 새 세트에 섞지 않고 **직전 완전한 세트**로 답한다.
      lastServed = "cache";
      return cached;
    }
    lastServed = "network";
    return response;
  } catch (err) {
    if (cached) {
      lastServed = "cache";
      return cached;
    }
    return new Response(JSON.stringify({ offline: true, bundle_version: activeSet }), {
      status: 503,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }
}

async function handleShell(request) {
  const url = new URL(request.url);
  const isHtml = request.mode === "navigate"
    || url.pathname.endsWith("/")
    || url.pathname.endsWith("/index.html");
  if (isHtml) {
    // HTML 문서는 항상 네트워크 우선 — 구버전 index.html 이 새 데이터 형식(debate_details 등)과
    // 섞이는 현상을 막는다. (`mode === "navigate"` Request 는 init 과 함께 복제할 수 없으므로 URL 문자열 사용)
    // 오프라인/네트워크 오류 시에만 현재 셸 캐시로 폴백한다.
    try {
      const response = await fetch(request.url, { cache: "no-store" });
      if (response && response.ok) {
        const cache = await caches.open(SHELL_CACHE);
        await cache.put(request.url, response.clone());
        return response;
      }
    } catch (err) {
      // 오프라인 폴백으로 내려간다.
    }
    const cache = await caches.open(SHELL_CACHE);
    const cached = (await cache.match(request.url)) || (await cache.match(request));
    if (cached) return cached;
    const fallback = await cache.match("./index.html");
    return fallback || (await caches.match("./index.html")) || Response.error();
  }
  // `?v=` 가 붙은 정적 자산(CSS/JS)과 아이콘·매니페스트는 현재 셸 캐시에서만 찾는다.
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      await cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    return Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.includes("/data/")) {
    event.respondWith(handleData(request));
    return;
  }
  // 앱 셸(문서·스크립트·스타일·매니페스트·아이콘)만 셸 캐시로 처리한다.
  const isShell = SHELL_FILES.some((name) => url.pathname.endsWith(`/${name}`));
  if (isShell || url.pathname.endsWith("/") || request.mode === "navigate") {
    event.respondWith(handleShell(request));
  }
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  const port = event.ports && event.ports[0];
  const reply = (payload) => {
    if (port) port.postMessage(payload);
    else if (event.source) event.source.postMessage(payload);
  };
  const work = (async () => {
    if (data.type === "prepare-set") {
      try {
        return await prepareSet();
      } catch (err) {
        // 실패해도 `activeSet`·캐시를 건드리지 않았다 — 직전 완전한 세트가 그대로 남아 있다.
        return { type: "set-activated", ok: false, sw_version: SW_VERSION,
                 bundle_version: activeSet, error: String(err && err.message ? err.message : err) };
      }
    }
    if (data.type === "read-set") return readActiveSet();
    if (data.type === "status") {
      return { type: "status", ok: true, sw_version: SW_VERSION, bundle_version: activeSet,
               last_served: lastServed, shell_cache: SHELL_CACHE,
               file_count: activeFiles ? Object.keys(activeFiles).length : 0 };
    }
    return { type: "unknown", ok: false };
  })();
  if (typeof event.waitUntil === "function") event.waitUntil(work);
  work.then(reply);
});
