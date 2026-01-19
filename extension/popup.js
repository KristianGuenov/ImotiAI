/* global chrome */
let lastResult = null;
let isRunningBatch = false;

async function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get({ autoSend: false }, (items) => resolve(items));
  });
}

const $ = (id) => document.getElementById(id);

function setStatus(text) {
  $("status").textContent = text;
}

function setLastId(id) {
  $("lastId").textContent = id ? String(id) : "—";
}

function formatPreview(result) {
  const sample = (result.items || []).slice(0, 5);
  return JSON.stringify(
    {
      pageTitle: result.pageTitle,
      sourceUrl: result.sourceUrl,
      extractedAt: result.extractedAt,
      itemCount: result.items.length,
      sampleItems: sample,
    },
    null,
    2
  );
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

async function runSingleExtraction(autoSendOverride = null) {
  setStatus("Extracting…");
  $("btnExport").disabled = true;
  $("btnSend").disabled = true;
  $("preview").textContent = "Working…";
  lastResult = null;

  const resp = await sendMessage({ type: "DETECT_AND_EXTRACT" });
  if (!resp || !resp.ok) throw new Error(resp?.error || "Unknown error");

  lastResult = resp.result;
  $("url").textContent = lastResult.sourceUrl || "—";
  $("count").textContent = String((lastResult.items || []).length);
  $("preview").textContent = formatPreview(lastResult);

  $("btnExport").disabled = false;
  $("btnSend").disabled = false;

  setStatus("Ready");

  const settings = await getSettings();
  const autoSend = autoSendOverride !== null ? autoSendOverride : !!settings.autoSend;

  if (autoSend) {
    setStatus("Sending…");
    const sresp = await sendMessage({ type: "SEND_TO_BACKEND", payload: lastResult });
    if (!sresp || !sresp.ok) throw new Error(sresp?.error || "Send failed");
    setLastId(sresp.id);
    setStatus(`Sent (id: ${sresp.id})`);
    return sresp.id;
  }
  return null;
}

$("btnExtract").addEventListener("click", async () => {
  try {
    await runSingleExtraction();
  } catch (e) {
    $("preview").textContent = String(e?.message || e);
    setStatus("Error");
  }
});

$("btnExport").addEventListener("click", () => {
  if (!lastResult) return;
  const safe = (lastResult.pageTitle || "extraction").replace(/[^a-z0-9\-_]+/gi, "_").slice(0, 60);
  downloadJson(lastResult, `${safe}.json`);
});

$("btnSend").addEventListener("click", async () => {
  if (!lastResult) return;
  setStatus("Sending…");
  try {
    const resp = await sendMessage({ type: "SEND_TO_BACKEND", payload: lastResult });
    if (!resp || !resp.ok) throw new Error(resp?.error || "Send failed");
    setLastId(resp.id);
    setStatus(`Sent (id: ${resp.id})`);
  } catch (e) {
    setStatus("Send error");
    alert(String(e?.message || e));
  }
});

$("btnBatch").addEventListener("click", async () => {
  if (isRunningBatch) return;
  isRunningBatch = true;
  $("btnStop").disabled = false;
  setStatus("Batch starting…");

  const maxPages = Math.max(1, Math.min(200, parseInt($("maxPages").value || "10", 10)));
  const delayMs = Math.max(0, Math.min(20000, parseInt($("delayMs").value || "1500", 10)));

  try {
    const resp = await sendMessage({ type: "BATCH_EXTRACT_PAGINATION", options: { maxPages, delayMs } });
    if (!resp || !resp.ok) throw new Error(resp?.error || "Batch failed");
    setLastId(resp.lastId || null);
    $("count").textContent = String(resp.lastPageItemCount || 0);
    $("url").textContent = resp.lastUrl || $("url").textContent;
    $("preview").textContent = JSON.stringify(resp, null, 2);
    setStatus(`Batch done (${resp.pagesDone} pages)`);
  } catch (e) {
    $("preview").textContent = String(e?.message || e);
    setStatus("Batch error");
  } finally {
    isRunningBatch = false;
    $("btnStop").disabled = true;
  }
});

$("btnStop").addEventListener("click", async () => {
  try {
    await sendMessage({ type: "STOP_BATCH" });
    setStatus("Stopping…");
  } catch (_) {}
});

$("btnLoadMore").addEventListener("click", async () => {
  const scrollSteps = Math.max(1, Math.min(200, parseInt($("scrollSteps").value || "12", 10)));
  const idleCycles = Math.max(1, Math.min(10, parseInt($("idleCycles").value || "2", 10)));
  setStatus("Loading more…");
  try {
    const resp = await sendMessage({ type: "LOAD_MORE_THEN_EXTRACT", options: { scrollSteps, idleCycles } });
    if (!resp || !resp.ok) throw new Error(resp?.error || "Load more failed");

    const s = await getSettings();
    if (s.autoSend) {
      setStatus("Sending…");
      const sent = await sendMessage({ type: "SEND_TO_BACKEND", payload: resp.result });
      if (!sent?.ok) throw new Error(sent?.error || "Send failed");
      setLastId(sent.id);
      setStatus(`Sent (id: ${sent.id})`);
    } else {
      lastResult = resp.result;
      $("btnExport").disabled = false;
      $("btnSend").disabled = false;
      setStatus("Ready");
    }

    $("url").textContent = resp.result.sourceUrl || "—";
    $("count").textContent = String((resp.result.items || []).length);
    $("preview").textContent = formatPreview(resp.result);
  } catch (e) {
    $("preview").textContent = String(e?.message || e);
    setStatus("Error");
  }
});

$("btnOptions").addEventListener("click", async () => {
  await chrome.runtime.openOptionsPage();
});
