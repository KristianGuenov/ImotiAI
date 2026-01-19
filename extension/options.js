/* global chrome */
const DEFAULTS = {
  endpoint: "http://localhost:8787/api/v1/extractions",
  apiKey: "dev-key-change-me",
  autoSend: false,
  siteTuningText: "{}",
};

const $ = (id) => document.getElementById(id);

async function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(DEFAULTS, (items) => resolve(items));
  });
}

async function setSettings(settings) {
  return new Promise((resolve) => {
    chrome.storage.sync.set(settings, () => resolve());
  });
}

function setStatus(text) {
  $("status").textContent = text;
}

function normalizeJsonText(s) {
  const t = String(s || "").trim();
  if (!t) return "{}";
  try {
    JSON.parse(t);
    return t;
  } catch (_) {
    // Keep the text but warn in UI; user can fix it
    return t;
  }
}

(async function init() {
  const settings = await getSettings();
  $("endpoint").value = settings.endpoint;
  $("apiKey").value = settings.apiKey;
  $("autoSend").checked = !!settings.autoSend;
  if ($("siteTuningText")) $("siteTuningText").value = settings.siteTuningText || "{}";
  setStatus("Loaded.");
})();

$("btnSave").addEventListener("click", async () => {
  const endpoint = $("endpoint").value.trim();
  const apiKey = $("apiKey").value.trim();
  const autoSend = $("autoSend").checked;

  const siteTuningText = $("siteTuningText") ? normalizeJsonText($("siteTuningText").value) : "{}";

  // Validate JSON (soft-fail): still save, but show status
  let jsonOk = true;
  try {
    JSON.parse(siteTuningText || "{}");
  } catch (_) {
    jsonOk = false;
  }

  await setSettings({ endpoint, apiKey, autoSend, siteTuningText });

  setStatus(jsonOk ? "Saved." : "Saved, but Site tuning JSON is invalid (will be ignored until fixed).");
});
