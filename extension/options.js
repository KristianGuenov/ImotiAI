/* global chrome */
const DEFAULTS = {
  endpoint: "http://localhost:8787/api/v1/extractions",
  apiKey: "dev-key-change-me",
  autoSend: false
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

(async function init() {
  const settings = await getSettings();
  $("endpoint").value = settings.endpoint;
  $("apiKey").value = settings.apiKey;
  $("autoSend").checked = !!settings.autoSend;
  setStatus("Loaded.");
})();

$("btnSave").addEventListener("click", async () => {
  const endpoint = $("endpoint").value.trim();
  const apiKey = $("apiKey").value.trim();
  const autoSend = $("autoSend").checked;
  await setSettings({ endpoint, apiKey, autoSend });
  setStatus("Saved.");
});
