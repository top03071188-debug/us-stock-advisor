/* web/sw.js — 발행 세트 단위 캐시 · 오프라인 열람 (P2-1, 기획서 §5.6).
 *
 * [고정]
 * - 앱 셸(index·app.js·styles.css·manifest·icon)은 `shell-<버전>` 캐시에 한 번 담는다.
 * - 데이터 파일(`./data/*.json`)은 **현재 발행 세트**(`meta.json` 의 `bundle_version`) 캐시에만 담는다.
 *   세트가 바뀌면 이전 세트 캐시는 지운다 → 화면이 서로 다른 날의 JSON 을 섞어 읽지 않는다.
 * - 네트워크가 없으면 마지막 성공 세트로 화면을 열고, 클라이언트는 "캐시된 세트" 배지를 띄운다.
 * - 세트가 바뀐 뒤 첫 요청이 오래된 세트로 응답되면 클라이언트에 `mismatch` 를 알린다("이전/혼합 세트").
 *
 * 캐시 키를 바꿀 때는 `SW_VERSION` 과 index.html 의 `?v=` 를 함께 올린다.
 */
"use strict";

const SW_VERSION = "phase4-2";
const SHELL_CACHE = `shell-${SW_VERSION}`;
const SET_PREFIX = "set-";
const SHELL_ASSETS = [
  "./",
  "./index.html",
  "./styles.css?v=phase4-2",
  "./app.js?v=phase4-2",
  "./manifest.json",
  "./icon.svg",
];
/** 파일 이름만으로 셸 요청을 식별한다(하위 경로 배포에서도 동작). */
const SHELL_FILES = ["index.html", "styles.css", "app.js", "manifest.json", "icon.svg", "sw.js"];

/** 현재 발행 세트 버전(`meta.json` 의 `bundle_version`). 모르면 null — 데이터를 캐시하지 않는다. */
let activeSet = null;
/** 마지막 응답이 네트워크였는지 캐시였는지 — 클라이언트가 배지로 표시한다. */
let lastServed = "network";

const setCacheName = (version) => `${SET_PREFIX}${version || "unknown"}`;

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
  event.waitUntil(self.clients.claim());
});

/** 현재 세트가 아닌 set-* 캐시를 지운다 — 섞인 세트를 남기지 않는다. */
async function trimSets(keep) {
  const names = await caches.keys();
  await Promise.all(
    names
      .filter((name) => name.startsWith(SET_PREFIX) && name !== setCacheName(keep))
      .map((name) => caches.delete(name)),
  );
}

async function handleData(request) {
  try {
    const response = await fetch(request);
    if (!response || !response.ok) return response;
    const copy = response.clone();
    if (request.url.endsWith("/data/meta.json")) {
      try {
        const body = await copy.clone().json();
        if (body && body.bundle_version) activeSet = body.bundle_version;
      } catch (err) {
        /* meta 가 아니면 무시 */
      }
    }
    if (activeSet) {
      const cache = await caches.open(setCacheName(activeSet));
      await cache.put(request, copy);
    }
    lastServed = "network";
    return response;
  } catch (err) {
    const cached = await caches.match(request);
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
  const cached = await caches.match(request);
  if (cached) {
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          caches.open(SHELL_CACHE).then((cache) => cache.put(request, response.clone()));
        }
      })
      .catch(() => undefined);
    return cached;
  }
  try {
    return await fetch(request);
  } catch (err) {
    const fallback = await caches.match("./index.html");
    return fallback || Response.error();
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
  if (data.type === "set-bundle-version") {
    const incoming = data.bundle_version || null;
    const previous = activeSet;
    if (incoming) activeSet = incoming;
    const work = trimSets(activeSet);
    if (typeof event.waitUntil === "function") event.waitUntil(work);
    work.then(() =>
      reply({
        type: "bundle-settled",
        sw_version: SW_VERSION,
        bundle_version: activeSet,
        previous_bundle_version: previous,
        mismatch: Boolean(previous && incoming && previous !== incoming),
        last_served: lastServed,
      }),
    );
  } else if (data.type === "status") {
    reply({
      type: "status",
      sw_version: SW_VERSION,
      bundle_version: activeSet,
      last_served: lastServed,
      shell_cache: SHELL_CACHE,
    });
  }
});
