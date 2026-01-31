/* global chrome */
"use strict";

const $ = (id) => document.getElementById(id);

let lastResult = null;
let isRunningBatch = false;
let lastOnboard = null;
let pickedCard = null;
let pickedNext = null;

function setDot(dotId, kind) {
  const el = $(dotId);
  if (!el) return;
  el.classList.remove("ok", "warn", "err");
  el.classList.add(kind || "ok");
}

function setStatus(text, kind = "ok") {
  $("status").textContent = text;
  setDot("dot", kind);
}

function setBatchStatus(text, kind = "ok") {
  $("statusBatch").textContent = text;
  setDot("dotBatch", kind);
}

function setOnboardStatus(text, kind = "ok") {
  const el = $("statusOnboard");
  if (el) el.textContent = text;
  setDot("dotOnboard", kind);
}

function safeName(s) {
  return String(s || "page")
    .toLowerCase()
    .replace(/[^a-z0-9\-_.]+/g, "_")
    .slice(0, 64);
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

function initTabs() {
  const tabs = Array.from(document.querySelectorAll(".tab"));
  const panels = Array.from(document.querySelectorAll(".panel"));
  for (const t of tabs) {
    t.addEventListener("click", () => {
      tabs.forEach((x) => x.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      t.classList.add("active");
      const id = `tab-${t.dataset.tab}`;
      const panel = document.getElementById(id);
      if (panel) panel.classList.add("active");
    });
  }
}

async function sendMessage(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (resp) => resolve(resp));
  });
}

async function refreshTabInfo() {
  const info = await sendMessage({ type: "GET_TAB_INFO" });
  if (info?.ok) {
    $("sitePill").textContent = info.hostname || "—";
    $("url").textContent = info.url || "—";
  }
}

function renderOnboardOut(obj) {
  const out = $("onboardOut");
  if (!out) return;
  out.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
}

function renderPicked(p) {
  const kind = p?.kind || "card";
  if (kind === "next") {
    pickedNext = p;
  } else {
    pickedCard = p;
  }

  if ($("pickedSelector")) {
    $("pickedSelector").textContent = pickedCard?.selector || "—";
    $("pickedPattern").textContent = pickedCard?.listingLinkPattern || "—";
    $("pickedMatches").textContent = String(pickedCard?.matchCount ?? 0);
  }

  if ($("pickedNextSelector")) {
    $("pickedNextSelector").textContent = pickedNext?.selector || "—";
    $("pickedNextMatches").textContent = String(pickedNext?.matchCount ?? 0);
  }

  if ($("btnSaveOverride")) $("btnSaveOverride").disabled = !pickedCard?.selector;
}

async function doOnboard() {
  setOnboardStatus("Running…", "warn");
  renderOnboardOut("Running checklist…");

  const resp = await sendMessage({ type: "ONBOARD_SITE" });
  if (!resp?.ok) {
    setOnboardStatus("Onboarding error", "err");
    renderOnboardOut(resp);
    return;
  }

  lastOnboard = resp.report || null;
  setOnboardStatus("Checklist done", "ok");
  renderOnboardOut(resp);
}

async function startPick() {
  setOnboardStatus("Pick mode… click a card", "warn");
  renderOnboardOut("Pick mode started. Click a listing card on the page…");

  if ($("btnPick")) $("btnPick").disabled = true;
  if ($("btnPickNext")) $("btnPickNext").disabled = true;
  if ($("btnPickStop")) $("btnPickStop").disabled = false;

  const resp = await sendMessage({ type: "START_PICKER", kind: "card" });
  if (!resp?.ok) {
    setOnboardStatus("Pick start failed", "err");
    if ($("btnPick")) $("btnPick").disabled = false;
    if ($("btnPickNext")) $("btnPickNext").disabled = false;
    if ($("btnPickStop")) $("btnPickStop").disabled = true;
    renderOnboardOut(resp);
  }
}

async function startPickNext() {
  setOnboardStatus("Pick mode… click NEXT", "warn");
  renderOnboardOut("Pick mode started. Click the pagination NEXT button on the page…");

  if ($("btnPick")) $("btnPick").disabled = true;
  if ($("btnPickNext")) $("btnPickNext").disabled = true;
  if ($("btnPickStop")) $("btnPickStop").disabled = false;

  const resp = await sendMessage({ type: "START_PICKER_NEXT" });
  if (!resp?.ok) {
    setOnboardStatus("Pick start failed", "err");
    if ($("btnPick")) $("btnPick").disabled = false;
    if ($("btnPickNext")) $("btnPickNext").disabled = false;
    if ($("btnPickStop")) $("btnPickStop").disabled = true;
    renderOnboardOut(resp);
  }
}

async function stopPick() {
  const resp = await sendMessage({ type: "STOP_PICKER" });
  if (!resp?.ok) {
    setOnboardStatus("Stop failed", "err");
    renderOnboardOut(resp);
    return;
  }

  setOnboardStatus("Pick stopped", "ok");
  if ($("btnPick")) $("btnPick").disabled = false;
  if ($("btnPickNext")) $("btnPickNext").disabled = false;
  if ($("btnPickStop")) $("btnPickStop").disabled = true;
}

async function saveOverride() {
  if (!pickedCard?.selector) return;

  // store minimal override: selectorCards + link pattern (if found)
  const hostname = $("sitePill").textContent || "";
  const override = {
    selectorCards: [pickedCard.selector],
    listingLinkPattern: pickedCard.listingLinkPattern || null,
    ...(pickedNext?.selector ? { nextPageSelector: pickedNext.selector } : {}),
  };

  setOnboardStatus("Saving override…", "warn");
  const resp = await sendMessage({ type: "SAVE_SITE_OVERRIDE", hostname, override });
  if (!resp?.ok) {
    setOnboardStatus("Save failed", "err");
    renderOnboardOut(resp);
    return;
  }

  setOnboardStatus("Override saved", "ok");
  renderOnboardOut(resp);
}

async function doExtract() {
  setStatus("Extracting…", "warn");
  const resp = await sendMessage({ type: "DETECT_AND_EXTRACT" });
  if (!resp?.ok) {
    setStatus("Extraction failed", "err");
    $("preview").textContent = JSON.stringify(resp, null, 2);
    return;
  }
  lastResult = resp.result;
  setStatus(`OK: ${resp.result?.items?.length || 0} items`, "ok");
  $("preview").textContent = JSON.stringify(resp.result, null, 2);
}

async function doSend() {
  if (!lastResult) {
    setStatus("No result to send", "warn");
    return;
  }
  setStatus("Sending…", "warn");
  const resp = await sendMessage({ type: "SEND_TO_BACKEND", payload: lastResult });
  if (!resp?.ok) {
    setStatus("Send failed", "err");
    return;
  }
  setStatus(`Sent (id: ${resp.id})`, "ok");
}

async function doLoadMore() {
  setBatchStatus("Load more…", "warn");

  const scrollSteps = Number($("scrollSteps").value || 12);
  const idleCycles = Number($("idleCycles").value || 2);

  const resp = await sendMessage({
    type: "LOAD_MORE_THEN_EXTRACT",
    options: { scrollSteps, idleCycles, stepDelayMs: 800, afterLoadDelayMs: 800 },
  });

  if (!resp?.ok) {
    setBatchStatus("Load more failed", "err");
    $("batchLog").textContent = JSON.stringify(resp, null, 2);
    return;
  }

  lastResult = resp.result;
  setBatchStatus(`OK: ${resp.result?.items?.length || 0} items`, "ok");
  $("batchLog").textContent = JSON.stringify(resp.result, null, 2);
}

async function doBatchPagination() {
  if (isRunningBatch) return;
  isRunningBatch = true;

  $("btnBatch").disabled = true;
  $("btnStop").disabled = false;

  const maxPages = Number($("maxPages").value || 25);
  const delayMs = Number($("delayMs").value || 2500);

  setBatchStatus("Running…", "warn");
  $("batchLog").textContent = "";

  const resp = await sendMessage({
    type: "BATCH_EXTRACT_PAGINATION",
    options: { maxPages, delayMs },
  });

  if (!resp?.ok) {
    setBatchStatus("Batch failed", "err");
    $("batchLog").textContent = JSON.stringify(resp, null, 2);
  } else {
    const msg = `Done. pagesDone=${resp.pagesDone}, lastId=${resp.lastId}, stopped=${resp.stopped}`;
    setBatchStatus("Done", "ok");
    $("batchLog").textContent = msg;
  }

  isRunningBatch = false;
  $("btnBatch").disabled = false;
  $("btnStop").disabled = true;
}

async function doStopBatch() {
  const resp = await sendMessage({ type: "STOP_BATCH" });
  if (resp?.ok) setBatchStatus("Stopping…", "warn");
}

async function checkHealth() {
  const resp = await sendMessage({ type: "CHECK_HEALTH" });
  const out = $("healthOut");
  if (!out) return;
  if (!resp?.ok) {
    out.textContent = resp?.error || "Health check failed";
    return;
  }
  out.textContent = resp?.text || "OK";
}

async function openDocs() {
  const resp = await sendMessage({ type: "OPEN_DOCS" });
  if (!resp?.ok) setStatus("Open docs failed", "err");
}

function wire() {
  $("btnOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("btnRefresh").addEventListener("click", refreshTabInfo);

  $("btnExtract").addEventListener("click", async () => {
    try {
      await refreshTabInfo();
      await doExtract();
    } catch (e) {
      setStatus("Error", "err");
      $("preview").textContent = String(e?.message || e);
    }
  });

  $("btnSend").addEventListener("click", async () => {
    try {
      await doSend();
    } catch (e) {
      setStatus("Send error", "err");
    }
  });

  $("btnExport").addEventListener("click", () => {
    if (!lastResult) return;
    downloadJson(lastResult, `${safeName(lastResult.pageTitle)}.json`);
  });

  $("btnBatch").addEventListener("click", doBatchPagination);
  $("btnStop").addEventListener("click", doStopBatch);
  $("btnLoadMore").addEventListener("click", doLoadMore);

  $("btnHealth").addEventListener("click", checkHealth);
  $("btnOpenDocs").addEventListener("click", openDocs);

  // Onboarding
  if ($("btnOnboard")) $("btnOnboard").addEventListener("click", doOnboard);
  if ($("btnPick")) $("btnPick").addEventListener("click", startPick);
  if ($("btnPickNext")) $("btnPickNext").addEventListener("click", startPickNext);
  if ($("btnPickStop")) $("btnPickStop").addEventListener("click", stopPick);
  if ($("btnSaveOverride")) $("btnSaveOverride").addEventListener("click", saveOverride);
}

// Receive picker results broadcast by background.js
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "PICKER_RESULT") {
    renderPicked(msg.result);
    setOnboardStatus("Picked", "ok");
    if ($("btnPick")) $("btnPick").disabled = false;
    if ($("btnPickNext")) $("btnPickNext").disabled = false;
    if ($("btnPickStop")) $("btnPickStop").disabled = true;
    renderOnboardOut({ pickerResult: msg.result });
  }
});

(async () => {
  initTabs();
  wire();
  await refreshTabInfo();
  setStatus("Idle", "ok");
  setBatchStatus("Ready", "ok");
  setOnboardStatus("Ready", "ok");
})();