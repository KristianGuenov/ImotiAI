/* global chrome */
const DEFAULTS = {
  endpoint: "http://localhost:8787/api/v1/extractions",
  apiKey: "dev-key-change-me",
};

let stopRequested = false;

function hostFromUrl(url) {
  try {
    return new URL(url).hostname;
  } catch (_) {
    return null;
  }
}

function getActiveTab() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const err = chrome.runtime.lastError;
      if (err) return reject(err);
      if (!tabs || !tabs[0]) return reject(new Error("No active tab found"));
      resolve(tabs[0]);
    });
  });
}

async function ensureContentScript(tabId) {
  // Inject content script on-demand (MV3)
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"],
  });
}

function sendToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) return reject(err);
      resolve(resp);
    });
  });
}

function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(DEFAULTS, (items) => resolve(items));
  });
}

function getSiteOverrides() {
  return new Promise((resolve) => {
    chrome.storage.sync.get({ siteOverrides: {} }, (items) => resolve(items.siteOverrides || {}));
  });
}

function setSiteOverrides(siteOverrides) {
  return new Promise((resolve, reject) => {
    chrome.storage.sync.set({ siteOverrides }, () => {
      const err = chrome.runtime.lastError;
      if (err) return reject(err);
      resolve(true);
    });
  });
}

async function postExtraction(payload) {
  const settings = await getSettings();
  if (!settings.endpoint) throw new Error("Missing endpoint (set it in Options)");

  const res = await fetch(settings.endpoint, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": settings.apiKey || "",
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Backend error ${res.status}: ${text || res.statusText}`);
  }
  return res.json();
}

function delay(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForTabLoadComplete(tabId, timeoutMs = 45000) {
  const start = Date.now();

  while (Date.now() - start < timeoutMs) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return;
    await delay(300);
  }
  throw new Error("Timed out waiting for page load");
}

function makeUtf8Safe(obj) {
  return JSON.parse(
    JSON.stringify(obj, (_k, v) => {
      if (typeof v !== "string") return v;
      return v.replace(/[\uD800-\uDFFF]/g, "");
    })
  );
}

async function runExtraction(tabId) {
  await ensureContentScript(tabId);

  // ✅ FIX: content.js expects RUN_EXTRACTION (not DETECT_AND_EXTRACT)
  const resp = await sendToTab(tabId, { type: "RUN_EXTRACTION" });
  if (!resp?.ok) throw new Error(resp?.error || "Content extraction failed");

  return makeUtf8Safe(resp.result);
}

async function runLoadMoreThenExtract(tabId, options) {
  await ensureContentScript(tabId);

  // ✅ FIX: LOAD_MORE then RUN_EXTRACTION (content.js supports these)
  const lm = await sendToTab(tabId, { type: "LOAD_MORE", options });
  if (!lm?.ok) throw new Error(lm?.error || "Load more failed");

  // Give it a beat to render new items
  await delay(Math.max(200, Math.min(4000, Number(options?.afterLoadDelayMs || 800))));

  const resp = await sendToTab(tabId, { type: "RUN_EXTRACTION" });
  if (!resp?.ok) throw new Error(resp?.error || "Extraction failed after load more");

  return makeUtf8Safe(resp.result);
}

async function navigateNext(tabId) {
  await ensureContentScript(tabId);

  // ✅ FIX: content.js expects NAVIGATE_NEXT_PAGE (not NAVIGATE_NEXT)
  const resp = await sendToTab(tabId, { type: "NAVIGATE_NEXT_PAGE" });
  return !!resp?.ok && !!resp?.didNavigate;
}

async function runOnboarding(tabId) {
  await ensureContentScript(tabId);
  const resp = await sendToTab(tabId, { type: "ONBOARD_SITE" });
  if (!resp?.ok) throw new Error(resp?.error || "Onboarding failed in content script");
  return resp.report;
}

async function saveSiteOverride(hostname, override) {
  if (!hostname) throw new Error("Missing hostname");
  const existing = await getSiteOverrides();
  existing[hostname] = override || {};
  await setSiteOverrides(existing);
  return true;
}

async function batchExtractPagination(tabId, options) {
  stopRequested = false;

  const maxPages = Math.max(1, Math.min(200, Number(options?.maxPages || 25)));
  const delayMs = Math.max(0, Math.min(20000, Number(options?.delayMs || 2500)));

  let pagesDone = 0;
  let lastId = null;

  for (let i = 0; i < maxPages; i += 1) {
    if (stopRequested) break;

    const extracted = await runExtraction(tabId);
    const out = await postExtraction(extracted);
    lastId = out?.id ?? lastId;
    pagesDone += 1;

    const didNav = await navigateNext(tabId);
    if (!didNav) break;

    await waitForTabLoadComplete(tabId);
    if (delayMs) await delay(delayMs);
  }

  return { pagesDone, lastId, stopped: stopRequested };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (msg?.type === "GET_TAB_INFO") {
      const tab = await getActiveTab();
      const url = tab?.url || null;
      sendResponse({ ok: true, url, hostname: url ? hostFromUrl(url) : null });
      return;
    }

    // Popup can still send DETECT_AND_EXTRACT; background translates it correctly now
    if (msg?.type === "DETECT_AND_EXTRACT") {
      const tab = await getActiveTab();
      const result = await runExtraction(tab.id);
      sendResponse({ ok: true, result });
      return;
    }

    if (msg?.type === "SEND_TO_BACKEND") {
      const out = await postExtraction(msg.payload);
      sendResponse({ ok: true, id: out.id });
      return;
    }

    if (msg?.type === "LOAD_MORE_THEN_EXTRACT") {
      const tab = await getActiveTab();
      const result = await runLoadMoreThenExtract(tab.id, msg.options || {});
      sendResponse({ ok: true, result });
      return;
    }

    if (msg?.type === "ONBOARD_SITE") {
      const tab = await getActiveTab();
      const report = await runOnboarding(tab.id);
      sendResponse({ ok: true, report });
      return;
    }

    if (msg?.type === "SAVE_SITE_OVERRIDE") {
      await saveSiteOverride(msg.hostname, msg.override);
      sendResponse({ ok: true });
      return;
    }

    if (msg?.type === "BATCH_EXTRACT_PAGINATION") {
      const tab = await getActiveTab();
      const out = await batchExtractPagination(tab.id, msg.options || {});
      sendResponse({ ok: true, ...out });
      return;
    }

    if (msg?.type === "STOP_BATCH") {
      stopRequested = true;
      sendResponse({ ok: true });
      return;
    }

    sendResponse({ ok: false, error: "Unknown message type" });
  })().catch((e) => {
    sendResponse({ ok: false, error: String(e?.message || e) });
  });

  return true;
});

// Side panel behavior (if side_panel is defined in manifest)
try {
  chrome.runtime.onInstalled.addListener(async () => {
    if (chrome.sidePanel?.setPanelBehavior) {
      await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
    }
  });
} catch (_) {}

// Fallback: open side panel on action click
try {
  chrome.action.onClicked.addListener(async (tab) => {
    if (!tab?.id) return;
    if (chrome.sidePanel?.open) {
      await chrome.sidePanel.open({ tabId: tab.id });
    }
  });
} catch (_) {}
