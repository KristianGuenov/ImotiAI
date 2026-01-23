// Built-in site profiles used by content.js (and by the runner via injection).
// This file must execute BEFORE content.js so window.__imotiBuiltInProfiles is available.

(() => {
  /**
   * Bridge: whenever extension code saves { siteOverrides } to chrome.storage.sync,
   * also POST them to the local profile sink so Docker runner can load site_profiles.json.
   *
   * Local sink: http://127.0.0.1:8788/profile
   */
  const PROFILE_SINK_URL = "http://127.0.0.1:8788/profile";

  async function pushSiteOverridesToRunner(siteOverrides) {
    try {
      await fetch(PROFILE_SINK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          siteOverrides,
          source: "extension",
          ts: Date.now(),
        }),
      });
    } catch (_) {
      // Sink not running / blocked / offline: ignore so extension remains usable.
    }
  }

  // Patch chrome.storage.sync.set to auto-push overrides when they are saved.
  try {
    if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.sync) {
      const area = chrome.storage.sync;
      const origSet = area.set;

      if (typeof origSet === "function" && !origSet.__imotiPatched) {
        const patched = function (items, cb) {
          return origSet.call(area, items, function (...args) {
            try {
              if (items && typeof items === "object" && items.siteOverrides && typeof items.siteOverrides === "object") {
                pushSiteOverridesToRunner(items.siteOverrides);
              }
            } catch (_) {}

            if (typeof cb === "function") {
              try { cb.apply(this, args); } catch (_) {}
            }
          });
        };

        patched.__imotiPatched = true;
        area.set = patched;
        origSet.__imotiPatched = true;
      }
    }
  } catch (_) {}

  // ---- Built-in profiles ----
  const profiles = [
    {
      id: "imot.bg",
      matchHosts: ["imot.bg", "www.imot.bg"],
      discovery: {
        anchorHrefIncludes: "/obiavi/",
        cardRootClosestSelectors: ["article", "li", ".offer", ".offer-item", ".imot", ".listItem", "div"],
      },
      timing: {
        waitForSelector: "a[href*='/obiavi/']",
        waitTimeoutMs: 15000,
      },
      pagination: {
        nextSelectors: [
          "link[rel='next']",
          "a[rel='next']",
          ".pagination a.next",
          ".pagination a[rel='next']",
          "a[title*='Следваща']",
          "a[aria-label*='Next']",
        ],
      },
      nested: {
        enabled: true,
      },
    },

    {
      id: "imoti.info",
      matchHosts: ["imoti.info", "www.imoti.info", "*.imoti.info"],
      discovery: {
        anchorHrefIncludes: "/obiava",
        cardRootClosestSelectors: ["article", "li"],
      },
      timing: {
        waitForSelector: "a[href*='/obiava/'], a[href*='/obiava']",
        waitTimeoutMs: 25000,
      },
      pagination: {
        nextSelectors: [
          "link[rel='next']",
          "a[rel='next']",
          ".pagination a.next",
          ".pagination a[rel='next']",
          "a[title*='Следваща']",
          "a[aria-label*='Next']",
        ],
      },
      nested: {
        enabled: true,
      },
    },
  ];

  window.__imotiBuiltInProfiles = profiles;
})();
