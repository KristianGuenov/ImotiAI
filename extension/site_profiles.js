/* global window */
(function () {
  "use strict";

  // Built-in site profiles.
  // These are intentionally conservative: they only add site-specific hints,
  // while preserving the generic detector as the ultimate fallback.
  const PROFILES = [
    {
      id: "imot.bg",
      matchHosts: ["imot.bg", "www.imot.bg"],
      discovery: {
        // Intentionally empty: use the generic repeating-list detector.
      },
    },
    {
      id: "imoti.net",
      matchHosts: ["imoti.net", "www.imoti.net"],
      discovery: {
        anchorHrefIncludes: "/obiava",
        cardRootClosestSelectors: ["article", "li", "div", "section"],
      },
      timing: {
        // Many pages are client-side rendered; waiting avoids extracting before results mount.
        waitForSelector: "a[href*='/obiava']",
        waitTimeoutMs: 12000,
      },
      pagination: {
        // Try obvious pagers first; if these fail, the heuristic next finder runs.
        nextSelectors: ["link[rel='next']", "a[rel='next']", ".pagination a.next", ".pagination a[rel='next']"],
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
        cardRootClosestSelectors: ["article", "li", "div", "section"],
      },
      timing: {
        waitForSelector: "a[href*='/obiava']",
        waitTimeoutMs: 12000,
      },
      pagination: {
        nextSelectors: ["link[rel='next']", "a[rel='next']", ".pagination a.next", ".pagination a[rel='next']"],
      },
      nested: {
        enabled: true,
      },
    },
  ];

  // Expose for content scripts injected later.
  window.__imotiBuiltInProfiles = PROFILES;
})();
