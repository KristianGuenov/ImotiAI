/* global chrome */
let lastResult = null;
let lastOnboard = null;
let isRunningBatch = false;

const $ = (id) => document.getElementById(id);

function setDot(id, kind) {
  const el = $(id);
  if (!el) return;
  el.classList.remove("ok", "err");
  if (kind === "ok") el.classList.add("ok");
  if (kind === "err") el.classList.add("err");
}

function setStatus(text, kind = "ok") {
  $("statusText").textContent = text;
  setDot("dot", kind);
}

function setBatchStatus(text, kind = "ok") {
  $("statusBatch").textContent = text;
  setDot("dotBatch", kind);
}

function setLastId(id) {
  $("lastId").textContent = id ? String(id) : "—";
}

function setExtractionMeta(result) {
  const meta = result?.meta || {};
  $("profile").textContent = meta.siteProfileUsed || "—";
  $("strategy").textContent = meta.strategyUsed || "—";
  $("timing").textContent = typeof meta.timingMs === "number" ? `${meta.timingMs} ms` : "—";
}

function formatOnboarding(report) {
  const checklist = report?.checklist || {};
  const override = report?.recommendedOverride || {};
  return JSON.stringify({ checklist, recommendedOverride: override, timingMs: report?.timingMs ?? null }, null, 2);
}

function downloadJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function sendMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) return reject(err);
      resolve(resp);
    });
  });
}

function safeName(title) {
  return (title || "extraction").replace(/[^a-z0-9\-_]+/gi, "_").slice(0, 60);
}

async function refreshTabInfo() {
  const info = await sendMessage({ type: "GET_TAB_INFO" });
  if (info?.ok) {
    $("sitePill").textContent = info.hostname || "—";
    $("url").textContent = info.url || "—";
  }
}

async function doExtract() {
  setStatus("Extracting…", "ok");
  $("btnSend").disabled = true;
  $("btnExport").disabled = true;
  $("preview").textContent = "Working…";
  lastResult = null;
  setExtractionMeta(null);

  const resp = await sendMessage({ type: "DETECT_AND_EXTRACT" });
  if (!resp?.ok) throw new Error(resp?.error || "Extraction failed");

  lastResult = resp.result;
  $("count").textContent = String((lastResult.items || []).length);
  setExtractionMeta(lastResult);
  $("preview").textContent = JSON.stringify(
    {
      pageTitle: lastResult.pageTitle,
      sourceUrl: lastResult.sourceUrl,
      extractedAt: lastResult.extractedAt,
      itemCount: lastResult.items?.length || 0,
      sampleItems: (lastResult.items || []).slice(0, 5),
    },
    null,
    2
  );

  $("btnSend").disabled = false;
  $("btnExport").disabled = false;
  setStatus("Ready", "ok");
}

async function doSend() {
  if (!lastResult) return;
  setStatus("Sending…", "ok");
  const resp = await sendMessage({ type: "SEND_TO_BACKEND", payload: lastResult });
  if (!resp?.ok) throw new Error(resp?.error || "Send failed");
  setLastId(resp.id);
  setStatus(`Sent (id: ${resp.id})`, "ok");
}

async function doBatchPagination() {
  if (isRunningBatch) return;
  isRunningBatch = true;

  $("btnStop").disabled = false;
  setBatchStatus("Running…", "ok");
  $("batchLog").textContent = "Starting…";

  const maxPages = Math.max(1, Math.min(200, parseInt($("maxPages").value || "25", 10)));
  const delayMs = Math.max(0, Math.min(20000, parseInt($("delayMs").value || "2500", 10)));

  try {
    const resp = await sendMessage({ type: "BATCH_EXTRACT_PAGINATION", options: { maxPages, delayMs } });
    if (!resp?.ok) throw new Error(resp?.error || "Batch failed");
    $("batchLog").textContent = JSON.stringify(resp, null, 2);
    if (resp.lastId) setLastId(resp.lastId);
    setBatchStatus(`Done (${resp.pagesDone} pages)`, "ok");
  } catch (e) {
    $("batchLog").textContent = String(e?.message || e);
    setBatchStatus("Batch error", "err");
  } finally {
    isRunningBatch = false;
    $("btnStop").disabled = true;
  }
}

async function doStopBatch() {
  await sendMessage({ type: "STOP_BATCH" });
  setBatchStatus("Stopping…", "ok");
}

async function doLoadMore() {
  setBatchStatus("Loading more…", "ok");
  const scrollSteps = Math.max(1, Math.min(200, parseInt($("scrollSteps").value || "16", 10)));
  const idleCycles = Math.max(1, Math.min(10, parseInt($("idleCycles").value || "2", 10)));

  try {
    const resp = await sendMessage({ type: "LOAD_MORE_THEN_EXTRACT", options: { scrollSteps, idleCycles } });
    if (!resp?.ok) throw new Error(resp?.error || "Load more failed");

    lastResult = resp.result;
    $("count").textContent = String((lastResult.items || []).length);
    setExtractionMeta(lastResult);
    $("preview").textContent = JSON.stringify(
      {
        pageTitle: lastResult.pageTitle,
        sourceUrl: lastResult.sourceUrl,
        extractedAt: lastResult.extractedAt,
        itemCount: lastResult.items?.length || 0,
        sampleItems: (lastResult.items || []).slice(0, 5),
      },
      null,
      2
    );

    $("btnSend").disabled = false;
    $("btnExport").disabled = false;

    setBatchStatus("Ready", "ok");
    setStatus("Ready", "ok");
  } catch (e) {
    $("batchLog").textContent = String(e?.message || e);
    setBatchStatus("Error", "err");
  }
}

async function doOnboard() {
  setStatus("Onboarding…", "ok");
  $("btnCopyOverride").disabled = true;
  $("btnSaveOverride").disabled = true;
  lastOnboard = null;

  const resp = await sendMessage({ type: "ONBOARD_SITE" });
  if (!resp?.ok) throw new Error(resp?.error || "Onboarding failed");

  lastOnboard = resp.report;
  $("onboardOut").textContent = formatOnboarding(lastOnboard);

  const hostname = lastOnboard?.checklist?.hostname;
  const overrideForHost = hostname ? lastOnboard?.recommendedOverride?.[hostname] : null;
  $("btnCopyOverride").disabled = !overrideForHost;
  $("btnSaveOverride").disabled = !overrideForHost;

  setStatus("Onboarding ready", "ok");
}

async function checkHealth() {
  // uses the configured endpoint in options; strip /api/v1/extractions
  const settings = await new Promise((resolve) => {
    chrome.storage.sync.get({ endpoint: "" }, (items) => resolve(items));
  });

  const base = (settings.endpoint || "").replace(/\/api\/v1\/extractions\s*$/i, "");
  if (!base) {
    $("healthOut").textContent = "Endpoint not set in Options.";
    return;
  }

  try {
    const res = await fetch(base + "/health");
    const text = await res.text();
    $("healthOut").textContent = res.ok ? `OK: ${text}` : `Error ${res.status}: ${text}`;
  } catch (e) {
    $("healthOut").textContent = String(e?.message || e);
  }
}

async function openDocs() {
  const settings = await new Promise((resolve) => {
    chrome.storage.sync.get({ endpoint: "" }, (items) => resolve(items));
  });

 const base = (settings.endpoint || "").replace(/\/api\/v1\/extractions\s*$/i, "");
  if (!base) return;
  chrome.tabs.create({ url: base + "/docs" });
}

function initTabs() {
  const tabs = Array.from(document.querySelectorAll(".tab"));
  tabs.forEach((t) => {
    t.addEventListener("click", () => {
      tabs.forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      const key = t.getAttribute("data-tab");
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      document.getElementById("tab-" + key).classList.add("active");
    });
  });
}

function wire() {
  $("btnOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("btnRefresh").addEventListener("click", refreshTabInfo);

  $("btnExtract").addEventListener("click", async () => {
    try { await refreshTabInfo(); await doExtract(); }
    catch (e) { setStatus("Error", "err"); $("preview").textContent = String(e?.message || e); }
  });

  $("btnSend").addEventListener("click", async () => {
    try { await doSend(); }
    catch (e) { setStatus("Send error", "err"); }
  });

  $("btnExport").addEventListener("click", () => {
    if (!lastResult) return;
    downloadJson(lastResult, `${safeName(lastResult.pageTitle)}.json`);
  });

  $("btnBatch").addEventListener("click", doBatchPagination);
  $("btnStop").addEventListener("click", doStopBatch);
  $("btnLoadMore").addEventListener("click", doLoadMore);

  // Onboarding / tuning
  $("btnOnboard").addEventListener("click", async () => {
    try {
      await refreshTabInfo();
      await doOnboard();
    } catch (e) {
      $("onboardOut").textContent = String(e?.message || e);
      setStatus("Onboarding error", "err");
    }
  });

  $("btnCopyOverride").addEventListener("click", async () => {
    try {
      const hostname = lastOnboard?.checklist?.hostname;
      const overrideForHost = hostname ? lastOnboard?.recommendedOverride?.[hostname] : null;
      if (!overrideForHost) return;
      await navigator.clipboard.writeText(JSON.stringify({ [hostname]: overrideForHost }, null, 2));
      setStatus("Override copied", "ok");
    } catch (e) {
      setStatus("Copy failed", "err");
    }
  });

  $("btnSaveOverride").addEventListener("click", async () => {
    try {
      const hostname = lastOnboard?.checklist?.hostname;
      const overrideForHost = hostname ? lastOnboard?.recommendedOverride?.[hostname] : null;
      if (!hostname || !overrideForHost) return;
      await sendMessage({ type: "SAVE_SITE_OVERRIDE", hostname, override: overrideForHost });
      setStatus("Override saved", "ok");
    } catch (e) {
      setStatus("Save failed", "err");
    }
  });

  $("btnClearOnboard").addEventListener("click", () => {
    lastOnboard = null;
    $("onboardOut").textContent = "Run “Run checklist” on a listings page to generate a suggested override.";
    $("btnCopyOverride").disabled = true;
    $("btnSaveOverride").disabled = true;
    setStatus("Cleared", "ok");
  });

  $("btnHealth").addEventListener("click", checkHealth);
  $("btnOpenDocs").addEventListener("click", openDocs);
}

(async () => {
  initTabs();
  wire();
  await refreshTabInfo();
  setStatus("Idle", "ok");
  setBatchStatus("Ready", "ok");
})();
