/* global chrome */
(function () {
  "use strict";

  // Chrome API compatibility (Playwright / non-extension runs)
  // Ensures the identifier `chrome` exists even outside the extension context.
  // We only fill missing pieces; we do NOT override real extension APIs.
  const __g = typeof globalThis !== 'undefined' ? globalThis : window;
  if (typeof __g.chrome === 'undefined') __g.chrome = {};
  // eslint-disable-next-line no-var
  var chrome = __g.chrome;
  chrome.runtime = chrome.runtime || {};
  chrome.runtime.sendMessage = chrome.runtime.sendMessage || function () {};
  chrome.runtime.onMessage = chrome.runtime.onMessage || { addListener: function () {} };
  chrome.storage = chrome.storage || {};
  chrome.storage.sync = chrome.storage.sync || {};
  chrome.storage.sync.get = chrome.storage.sync.get || function (defaults, cb) {
    try { cb && cb(defaults || {}); } catch (_) {}
  };
  chrome.storage.sync.set = chrome.storage.sync.set || function (_items, cb) {
    try { cb && cb(); } catch (_) {}
  };


  // ---------------------------------------------------------------------------
  // Utilities
  // ---------------------------------------------------------------------------

  class DomText {
    static normalize(s) {
      return String(s || "").replace(/\s+/g, " ").trim();
    }
    static safeTruncate(s, maxLen) {
      const str = String(s || "");
      if (str.length <= maxLen) return str;
      let t = str.slice(0, maxLen);
      // Avoid leaving a dangling high surrogate at the end of the slice
      const last = t.charCodeAt(t.length - 1);
      if (last >= 0xd800 && last <= 0xdbff) {
        t = t.slice(0, -1);
      }
      return t;
    }
    static visibleText(el, maxLen = 400) {
      if (!el) return "";
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          const t = DomText.normalize(node.nodeValue);
          if (!t) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (!parent) return NodeFilter.FILTER_REJECT;
          const tag = parent.tagName?.toLowerCase();
          if (tag === "script" || tag === "style" || tag === "noscript") return NodeFilter.FILTER_REJECT;
          const style = window.getComputedStyle(parent);
          if (style.display === "none" || style.visibility === "hidden") return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let out = "";
      while (walker.nextNode()) {
        out += " " + DomText.normalize(walker.currentNode.nodeValue);
        if (out.length >= maxLen) break;
      }
      return DomText.safeTruncate(DomText.normalize(out), maxLen);
    }
  }

  class SiteOverrides {
    static getAll() {
      return new Promise((resolve) => {
        chrome.storage.sync.get({ siteOverrides: {} }, (items) => resolve(items.siteOverrides || {}));
      });
    }
    static async getForHost(hostname) {
      const all = await SiteOverrides.getAll();
      return all[hostname] || null;
    }
  }

  function nowMs() {
    return (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
  }

  function tryUrl(href) {
    try {
      return new URL(href, location.href);
    } catch (_) {
      return null;
    }
  }

  function isProbablyFooterOrNav(a) {
    const el = a instanceof Element ? a : null;
    if (!el) return false;
    if (el.closest("footer")) return true;
    if (el.closest("nav")) return true;
    if (el.closest("[role='navigation']")) return true;
    return false;
  }

  // ---------------------------------------------------------------------------
  // Holmes.bg special-case helpers
  // ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Luximmo.com special-case helpers
// ---------------------------------------------------------------------------

function isLuximmoHost(hostname) {
  const h = String(hostname || "").toLowerCase();
  return h === "luximmo.com" || h === "www.luximmo.com";
}

// Luximmo listing pages typically include anchors like:
// .../luxury-property-46745-... .html
function closestLuximmoCardRoot(a, maxDepth = 8) {
  let el = a;
  for (let i = 0; i < maxDepth && el; i++) {
    const p = el.parentElement;
    if (!p) break;
    let n = 999;
    try { n = p.querySelectorAll("a[href*='luxury-property-'][href$='.html']").length; } catch (_) { n = 999; }
    if (n === 1) return p;
    el = p;
  }
  return a.parentElement || a;
}

function extractLuximmoItemFromAnchor(a) {
  const root = closestLuximmoCardRoot(a, 8);

  const url = absUrl(a.getAttribute("href") || a.href || "");

  // Prefer heading-like text within the root; fallback to anchor text.
  let title = null;
  try {
    const h = root.querySelector("h1,h2,h3,h4,.title,.property-title,.offer-title");
    const ht = DomText.normalize(h ? h.textContent : "");
    if (ht) title = DomText.safeTruncate(ht, 160);
  } catch (_) {}

  if (!title) {
    const at = DomText.normalize(a.textContent);
    if (at) title = DomText.safeTruncate(at, 160);
  }

  const images = [];
  try {
    const imgs = Array.from(root.querySelectorAll("img"));
    for (const img of imgs) {
      const src = img.currentSrc || img.getAttribute("src") || img.getAttribute("data-src") || "";
      const u = absUrl(src);
      if (isUsefulImageSrc(u)) images.push(u);
      if (images.length >= 8) break;
    }
  } catch (_) {}

  const texts = [];
  try {
    const nodes = Array.from(root.querySelectorAll("span,div,p,li,strong,em,small")).slice(0, 140);
    for (const n of nodes) {
      const t = DomText.normalize(n.textContent);
      if (!t) continue;
      if (t.length < 3 || t.length > 180) continue;
      texts.push(t);
      if (texts.length >= 10) break;
    }
  } catch (_) {}

  return {
    title: title,
    url: url || null,
    image: images[0] || null,
    images: Array.from(new Set(images)).slice(0, 8),
    texts: Array.from(new Set(texts)).slice(0, 10),
    rawText: DomText.visibleText(root, 650),
  };
}

function extractLuximmoItemsFromPage() {
  // Use Luximmo's stable listing URL pattern as the unit.
  const anchors = Array.from(document.querySelectorAll("a[href*='luxury-property-'][href$='.html']"))
    .filter((a) => a && a.getAttribute)
    .filter((a) => !isProbablyFooterOrNav(a));

  if (!anchors.length) return [];

  const seen = new Set();
  const out = [];

  for (const a of anchors) {
    const href = a.getAttribute("href") || "";
    if (!href) continue;
    const key = href;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(extractLuximmoItemFromAnchor(a));
    if (out.length >= 250) break;
  }
  return out;
}


  function isHolmesHost(hostname) {
    const h = String(hostname || "").toLowerCase();
    return h === "holmes.bg" || h === "www.holmes.bg";
  }

  function absUrl(href) {
    if (!href) return "";
    try { return new URL(href, location.href).toString(); } catch (_) { return String(href); }
  }

  function isUsefulImageSrc(src) {
    if (!src) return false;
    const s = String(src).toLowerCase();
    if (s.endsWith(".svg")) return false;
    // UI icons frequently present on holmes list pages
    if (s.includes("assets/images/")) return false;
    if (s.includes("filter.svg") || s.includes("sort.svg") || s.includes("map.svg")) return false;
    if (s.includes("favourite.svg") || s.includes("chevron")) return false;
    if (s.includes("top.svg")) return false;
    return true;
  }

  // Pick a "card root" around a listing anchor by walking upwards until the parent
  // contains exactly ONE listing link. This scopes text/images to one listing block
  // and avoids pulling page-title and UI toolbar icons.
  function closestHolmesCardRoot(a, maxDepth = 8) {
    let el = a;
    for (let i = 0; i < maxDepth && el; i++) {
      const p = el.parentElement;
      if (!p) break;
      let n = 999;
      try { n = p.querySelectorAll("a[href^='/obiava/']").length; } catch (_) { n = 999; }
      if (n === 1) return p;
      el = p;
    }
    return a.parentElement || a;
  }

  function extractHolmesItemFromAnchor(a) {
    const root = closestHolmesCardRoot(a, 8);

    const url = absUrl(a.getAttribute("href") || a.href || "");

    // Prefer heading-like text within the root; fallback to anchor text.
    let title = null;
    try {
      const h = root.querySelector("h1,h2,h3,h4,.offer-title,.title");
      const ht = DomText.normalize(h ? h.textContent : "");
      if (ht) title = DomText.safeTruncate(ht, 160);
    } catch (_) {}

    if (!title) {
      const at = DomText.normalize(a.textContent);
      if (at) title = DomText.safeTruncate(at, 160);
    }

    const images = [];
    try {
      const imgs = Array.from(root.querySelectorAll("img"));
      for (const img of imgs) {
        const src = img.currentSrc || img.getAttribute("src") || img.getAttribute("data-src") || "";
        const u = absUrl(src);
        if (isUsefulImageSrc(u)) images.push(u);
        if (images.length >= 8) break;
      }
    } catch (_) {}

    const texts = [];
    // Keep texts compatible with existing schema: short lines inside the root
    try {
      const nodes = Array.from(root.querySelectorAll("span,div,p,li,strong,em,small")).slice(0, 120);
      for (const n of nodes) {
        const t = DomText.normalize(n.textContent);
        if (!t) continue;
        if (t.length < 3 || t.length > 160) continue;
        texts.push(t);
        if (texts.length >= 10) break;
      }
    } catch (_) {}

    return {
      title: title,
      url: url || null,
      image: images[0] || null,
      images: Array.from(new Set(images)).slice(0, 8),
      texts: Array.from(new Set(texts)).slice(0, 10),
      rawText: DomText.visibleText(root, 600),
    };
  }

  function extractHolmesItemsFromPage() {
    // Use the listing anchor pattern as the primary unit.
    const anchors = Array.from(document.querySelectorAll("a[href^='/obiava/']"))
      .filter((a) => a && a.getAttribute && (a.getAttribute("href") || "").startsWith("/obiava/"))
      .filter((a) => !isProbablyFooterOrNav(a));

    if (!anchors.length) return [];

    const seen = new Set();
    const out = [];

    for (const a of anchors) {
      const href = a.getAttribute("href") || "";
      if (!href || seen.has(href)) continue;
      seen.add(href);
      out.push(extractHolmesItemFromAnchor(a));
      if (out.length >= 250) break;
    }
    return out;
  }


  function chooseListingLinkPatternFromPage() {
    const patterns = [
      "/obiava/",
      "/offer/",
      "/listing/",
      "/ad/",
      "/property/",
      "/imot/",
      "/annonce/",
      "/objava/",
    ];

    const anchors = Array.from(document.querySelectorAll("a[href]"))
      .filter((a) => a.href && !a.href.endsWith("#") && !isProbablyFooterOrNav(a));

    const counts = {};
    for (const p of patterns) counts[p] = 0;

    for (const a of anchors) {
      const u = tryUrl(a.href);
      const path = (u?.pathname || "").toLowerCase();
      for (const p of patterns) {
        if (path.includes(p.toLowerCase())) counts[p] += 1;
      }
    }

    let best = null;
    let bestCount = 0;
    for (const p of patterns) {
      if (counts[p] > bestCount) {
        bestCount = counts[p];
        best = p;
      }
    }
    return bestCount >= 5 ? best : null;
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(String(s));
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function pickStableClasses(el, max = 2) {
    const ignore = [
      "active",
      "selected",
      "hover",
      "focus",
      "open",
      "closed",
      "current",
      "ng-star-inserted",
    ];
    const out = [];
    for (const c of Array.from(el.classList || [])) {
      const lc = c.toLowerCase();
      if (!lc) continue;
      if (ignore.includes(lc)) continue;
      if (lc.startsWith("_ng")) continue;
      if (lc.startsWith("ng-")) continue;
      if (lc.includes("mat-")) continue;
      out.push(c);
      if (out.length >= max) break;
    }
    return out;
  }

  // Build a reusable selector for a "card" (should match many cards).
  function buildReusableCardSelector(clickedEl) {
    const maxUp = 6;
    let cur = clickedEl;

    for (let i = 0; i < maxUp && cur; i++) {
      if (!(cur instanceof Element)) break;

      const tag = cur.tagName.toLowerCase();
      const cls = pickStableClasses(cur, 2);
      let sel = tag;
      if (cls.length) sel += "." + cls.map(cssEscape).join(".");
      const count = safeQueryAllCount(sel);

      // We WANT multiple matches (list of cards), but not "everything".
      if (count >= 3 && count <= 500) return { selector: sel, matchCount: count };

      cur = cur.parentElement;
    }

    // fallback: div
    const fallbackCount = safeQueryAllCount("div");
    return { selector: "div", matchCount: fallbackCount };
  }

  function safeQueryAllCount(selector) {
    try {
      return document.querySelectorAll(selector).length;
    } catch (_) {
      return 0;
    }
  }

  function firstNonEmpty(arr) {
    for (const x of arr) {
      if (x) return x;
    }
    return null;
  }

  // ---------------------------------------------------------------------------
  // Structural fingerprint detector (generic fallback)
  // ---------------------------------------------------------------------------

  class Fingerprint {
    constructor(map) {
      this.map = map;
    }
    static fromElement(el, depth = 3, maxNodes = 250) {
      const counts = new Map();
      const queue = [{ node: el, d: 0 }];
      let visited = 0;
      while (queue.length && visited < maxNodes) {
        const { node, d } = queue.shift();
        if (!(node instanceof Element)) continue;
        visited++;
        const tag = node.tagName.toLowerCase();
        counts.set(tag, (counts.get(tag) || 0) + 1);
        if (d >= depth) continue;
        for (const c of Array.from(node.children || [])) queue.push({ node: c, d: d + 1 });
      }
      return new Fingerprint(counts);
    }
    static cosineSimilarity(a, b) {
      let dot = 0,
        na = 0,
        nb = 0;
      const keys = new Set([...a.keys(), ...b.keys()]);
      for (const k of keys) {
        const va = a.get(k) || 0;
        const vb = b.get(k) || 0;
        dot += va * vb;
        na += va * va;
        nb += vb * vb;
      }
      if (na === 0 || nb === 0) return 0;
      return dot / (Math.sqrt(na) * Math.sqrt(nb));
    }
  }

  class ListCandidate {
    constructor(container, items, score, avgSim, preferRatio = 0) {
      this.container = container;
      this.items = items;
      this.score = score;
      this.avgSim = avgSim;
      this.preferRatio = preferRatio;
    }
  }

  class ListDetector {
    constructor(options = {}) {
      this.MIN_ITEMS = 4;
      this.MAX_ITEMS = 80;
      this.preferHrefIncludes = options.preferHrefIncludes || null;
    }
    detect() {
      const containers = this._collectContainers();
      const candidates = [];
      for (const c of containers) {
        const kids = Array.from(c.children || []).filter((x) => x instanceof Element);
        if (kids.length < this.MIN_ITEMS) continue;

        const items = kids.slice(0, this.MAX_ITEMS);
        const fp0 = Fingerprint.fromElement(items[0]);
        const sims = [];

        for (let i = 1; i < Math.min(items.length, 12); i++) {
          const fpi = Fingerprint.fromElement(items[i]);
          sims.push(Fingerprint.cosineSimilarity(fp0.map, fpi.map));
        }
        const avgSim = sims.length ? sims.reduce((a, b) => a + b, 0) / sims.length : 0;
        if (avgSim < 0.55) continue;

        let preferRatio = 0;
        if (this.preferHrefIncludes) {
          const pref = String(this.preferHrefIncludes).toLowerCase();
          const sample = items.slice(0, Math.min(items.length, 12));
          let hits = 0;
          for (const it of sample) {
            const anchors = Array.from(it.querySelectorAll('a[href]')).slice(0, 40);
            let ok = false;
            for (const a of anchors) {
              const u = tryUrl(a.getAttribute('href') || a.href);
              const p = (u?.pathname || '').toLowerCase();
              if (p.includes(pref)) {
                ok = true;
                break;
              }
            }
            if (ok) hits += 1;
          }
          preferRatio = sample.length ? hits / sample.length : 0;
          if (preferRatio < 0.2) continue;
        }

        const linkCount = items.slice(0, 12).filter((it) => it.querySelector("a[href]")).length;
        const linkRatio = linkCount / Math.min(items.length, 12);
        const textLen = DomText.visibleText(c, 800).length;

        const score = items.length * avgSim + linkRatio * 5 + Math.min(textLen / 400, 2) + preferRatio * 20;
        candidates.push(new ListCandidate(c, items, score, avgSim, preferRatio));
      }
      candidates.sort((a, b) => b.score - a.score);
      return candidates[0] || null;
    }
    _collectContainers() {
      const selectors = ["main", "section", "article", "div", "ul", "ol"];
      const els = new Set();
      for (const sel of selectors) document.querySelectorAll(sel).forEach((e) => els.add(e));
      return Array.from(els).filter((e) => {
        const r = e.getBoundingClientRect();
        if (!r || r.width < 200 || r.height < 120) return false;
        const style = window.getComputedStyle(e);
        if (style.display === "none" || style.visibility === "hidden") return false;
        return true;
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Item extractor (improved: avoids “1/10” overlay titles; can prefer href pattern)
  // ---------------------------------------------------------------------------

  class ItemExtractor {
    constructor(options = {}) {
      this.preferHrefIncludes = options.preferHrefIncludes || null; // e.g. "/obiava/"
    }

    extractItem(el) {
      const a = this._pickPrimaryLink(el);
      const title = this._pickTitle(el, a);
      const images = this._pickImages(el);
      const texts = this._pickTexts(el);
      return {
        title,
        url: a?.href || null,
        image: images[0] || null,
        images,
        texts,
        rawText: DomText.visibleText(el, 600),
      };
    }

    _isFractionLikeTitle(t) {
      const s = DomText.normalize(t);
      return /^\d+\s*\/\s*\d+$/.test(s);
    }

    _isNoiseTitle(t) {
      const s = DomText.normalize(t).toLowerCase();
      if (!s) return true;
      if (this._isFractionLikeTitle(s)) return true;
      const noise = ["контакт", "vip", "сделка", "виж", "подроб", "details", "more"];
      if (noise.some((w) => s === w)) return true;
      return false;
    }

    _pickPrimaryLink(el) {
      const links = Array.from(el.querySelectorAll("a[href]"))
        .map((x) => x)
        .filter((a) => a.href && !a.href.endsWith("#"));

      if (!links.length) return null;

      let best = links[0],
        bestScore = -Infinity;

      for (const l of links.slice(0, 40)) {
        const rect = l.getBoundingClientRect();
        const area = Math.max(0, rect.width) * Math.max(0, rect.height);

        const text = DomText.normalize(l.textContent);
        const textLen = text.length;

        let score = area;
        score += Math.min(textLen / 20, 4) * 250;

        if (this.preferHrefIncludes) {
          try {
            const u = new URL(l.href, location.href);
            if ((u.pathname || "").toLowerCase().includes(this.preferHrefIncludes.toLowerCase())) {
              score += 1500;
            }
          } catch (_) {}
        } else {
          try {
            const u = new URL(l.href, location.href);
            if (u.hostname === location.hostname && (u.pathname || "").split("/").filter(Boolean).length >= 2) {
              score += 150;
            }
          } catch (_) {}
        }

        if (this._isFractionLikeTitle(text)) score -= 2000;

        const hasImg = !!l.querySelector("img");
        if (hasImg && textLen <= 4) score -= 800;

        if (score > bestScore) {
          bestScore = score;
          best = l;
        }
      }

      return best;
    }

    _pickTitle(el, primaryLink) {
      const head = el.querySelector("h1,h2,h3,h4");
      if (head) {
        const t = DomText.normalize(head.textContent);
        if (t && !this._isNoiseTitle(t)) return DomText.safeTruncate(t, 160);
      }

      if (primaryLink) {
        const t = DomText.normalize(primaryLink.textContent);
        if (t && t.length >= 3 && !this._isNoiseTitle(t)) return DomText.safeTruncate(t, 160);

        const aria = DomText.normalize(primaryLink.getAttribute("aria-label"));
        if (aria && !this._isNoiseTitle(aria)) return DomText.safeTruncate(aria, 160);
      }

      const anchors = Array.from(el.querySelectorAll("a[href]")).slice(0, 60);
      let best = null;
      let bestScore = -Infinity;

      for (const a of anchors) {
        const txt = DomText.normalize(a.textContent);
        if (!txt || txt.length < 3) continue;
        if (this._isNoiseTitle(txt)) continue;

        let score = Math.min(txt.length, 60);

        if (this.preferHrefIncludes) {
          try {
            const u = new URL(a.href, location.href);
            if ((u.pathname || "").toLowerCase().includes(this.preferHrefIncludes.toLowerCase())) score += 40;
          } catch (_) {}
        }

        if (score > bestScore) {
          bestScore = score;
          best = txt;
        }
      }

      if (best) return DomText.safeTruncate(best, 160);

      const t = DomText.visibleText(el, 220);
      return t || null;
    }

    _pickImages(el) {
      const imgs = Array.from(el.querySelectorAll("img"))
        .map((img) => img.currentSrc || img.src)
        .filter((src) => !!src);
      return Array.from(new Set(imgs)).slice(0, 8);
    }

    _pickTexts(el) {
      const lines = [];
      const nodes = Array.from(el.querySelectorAll("span,div,p,li,strong,em,small")).slice(0, 120);
      for (const n of nodes) {
        const t = DomText.normalize(n.textContent);
        if (!t) continue;
        if (t.length < 3 || t.length > 160) continue;
        lines.push(t);
        if (lines.length >= 10) break;
      }
      return Array.from(new Set(lines));
    }
  }

  // ---------------------------------------------------------------------------
  // Pagination helper (unchanged)
  // ---------------------------------------------------------------------------

  class Navigation {
    static _isVisible(el) {
      if (!(el instanceof Element)) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 12 || r.height < 12) return false;
      const s = window.getComputedStyle(el);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
      return true;
    }

    static findNextControl() {
      const relNext = document.querySelector('link[rel="next"][href]');
      if (relNext && relNext.href) return { type: "href", href: relNext.href, score: 10 };

      const candidates = [];
      const nodes = Array.from(document.querySelectorAll("a[href], button, [role='button']"));

      for (const el of nodes) {
        if (!Navigation._isVisible(el)) continue;

        const tag = el.tagName.toLowerCase();
        const href = tag === "a" ? el.href : null;
        const disabled = el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
        if (disabled) continue;

        const text = DomText.normalize(el.textContent).toLowerCase();
        const aria = DomText.normalize(el.getAttribute("aria-label")).toLowerCase();
        const title = DomText.normalize(el.getAttribute("title")).toLowerCase();
        const cls = (el.className || "").toString().toLowerCase();

        const inPager = !!el.closest("nav, .pagination, .pager, .paginator, [aria-label*='page'], [class*='pag']");
        let score = 0;

        const signals = [
          text.includes("next"),
          text.includes("следва"),
          text === ">",
          text === "»",
          aria.includes("next"),
          aria.includes("следва"),
          title.includes("next"),
          title.includes("следва"),
          cls.includes("next"),
          cls.includes("pager-next"),
        ];
        if (signals.some(Boolean)) score += 8;
        if (inPager) score += 4;
        if (href) score += 1;

        if (text.includes("previous") || text.includes("предиш")) score -= 6;

        if (score > 0) candidates.push({ el, href, score });
      }

      candidates.sort((a, b) => b.score - a.score);

      const active = document.querySelector(".pagination .active, .pager .active, [aria-current='page']");
      if (active) {
        const nextA =
          active.parentElement?.nextElementSibling?.querySelector?.("a[href]") ||
          active.nextElementSibling?.querySelector?.("a[href]");
        if (nextA && Navigation._isVisible(nextA)) return { type: "click", el: nextA, score: 20 };
      }

      return candidates.length ? { type: "click", el: candidates[0].el, score: candidates[0].score } : null;
    }

    static async navigateNext() {
      const next = Navigation.findNextControl();
      if (!next) return false;

      if (next.type === "href" && next.href) {
        // Holmes pagination links often drop the current query string (filters like ?raioni=...).
        // Preserve location.search when the next href has no query of its own.
        if (/(^|\.)holmes\.bg$/i.test(location.hostname) && location.search) {
          try {
            const u = new URL(next.href, location.href);
            if (!u.search) u.search = location.search;
            location.href = u.toString();
            return true;
          } catch (e) {
            // fall through to default behavior
          }
        }
        location.href = next.href;
        return true;
      }
      if (next.type === "click" && next.el) {
        next.el.scrollIntoView({ block: "center" });
        next.el.click();
        return true;
      }
      return false;
    }
  }

  class LoadMore {
    static _norm(s) {
      return String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
    }

    static _isVisible(el) {
      if (!(el instanceof Element)) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 10 || r.height < 10) return false;
      const s = window.getComputedStyle(el);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
      return true;
    }

    static _findLoadMoreControl(options = {}) {
      // Allow explicit selector override
      const explicit = (options && options.buttonSelector) ? String(options.buttonSelector) : null;
      if (explicit) {
        try {
          const el = document.querySelector(explicit);
          if (el && LoadMore._isVisible(el)) return el;
        } catch (_) {}
      }

      const keywords = (options && Array.isArray(options.keywords) && options.keywords.length)
        ? options.keywords.map((x) => LoadMore._norm(x)).filter(Boolean)
        : [
            "зареди още",
            "зареди oще",
            "покажи още",
            "покажи oще",
            "виж още",
            "още",
            "load more",
            "show more",
            "more",
            "see more"
          ];

      const nodes = Array.from(document.querySelectorAll("button, a[href], [role='button'], input[type='button'], input[type='submit']"))
        .slice(0, 800);

      let best = null;
      let bestScore = -Infinity;

      for (const el of nodes) {
        if (!LoadMore._isVisible(el)) continue;

        const tag = el.tagName.toLowerCase();
        const disabled =
          el.hasAttribute("disabled") ||
          el.getAttribute("aria-disabled") === "true" ||
          (tag === "a" && (el.getAttribute("aria-disabled") === "true"));

        if (disabled) continue;

        const text = tag === "input" ? (el.value || "") : (el.textContent || "");
        const t = LoadMore._norm(text);
        const aria = LoadMore._norm(el.getAttribute("aria-label"));
        const title = LoadMore._norm(el.getAttribute("title"));
        const cls = LoadMore._norm(el.className);

        const hay = [t, aria, title].join(" ");

        let score = 0;
        for (const kw of keywords) {
          if (!kw) continue;
          if (hay.includes(kw)) score += (kw.length >= 6 ? 8 : 4);
        }

        // Prefer elements that look like "load more" controls
        if (cls.includes("load") && cls.includes("more")) score += 6;
        if (cls.includes("more")) score += 1;

        // Prefer near bottom / within paginator-ish containers
        const inPager = !!el.closest(".pagination, .pager, .paginator, [class*='pag'], [aria-label*='page']");
        if (inPager) score += 2;

        // Small boost for being near the bottom of the page
        try {
          const r = el.getBoundingClientRect();
          if (r && r.top > window.innerHeight * 0.4) score += 1;
        } catch (_) {}

        if (score > bestScore && score > 0) {
          bestScore = score;
          best = el;
        }
      }

      return best;
    }

    static _waitForMoreItems(detector, prevCount, timeoutMs) {
      const maxWait = Math.max(300, Math.min(20000, Number(timeoutMs || 3500)));
      return new Promise((resolve) => {
        let done = false;
        const finish = (grew) => {
          if (done) return;
          done = true;
          try { obs.disconnect(); } catch (_) {}
          resolve(!!grew);
        };

        // Fast path: check immediately
        try {
          const c0 = detector.detect()?.items?.length || 0;
          if (c0 > prevCount) return finish(true);
        } catch (_) {}

        const obs = new MutationObserver(() => {
          try {
            const c = detector.detect()?.items?.length || 0;
            if (c > prevCount) finish(true);
          } catch (_) {}
        });

        try {
          obs.observe(document.documentElement || document.body, { childList: true, subtree: true });
        } catch (_) {
          // If observe fails, just time out
        }

        setTimeout(() => finish(false), maxWait);
      });
    }

    static async run(options = {}) {
      // Strategy:
      // 1) Prefer clicking a visible "load more" control (e.g., "Зареди още")
      // 2) Fall back to scroll-to-bottom
      // Stop when item count doesn't increase for idleCycles attempts.

      const maxActions = Math.max(1, Math.min(200, Number(options.maxActions || options.scrollSteps || 12)));
      const idleCycles = Math.max(1, Math.min(10, Number(options.idleCycles || 2)));
      const stepDelayMs = Math.max(200, Math.min(6000, Number(options.stepDelayMs || 900)));
      const waitAfterClickMs = Math.max(300, Math.min(20000, Number(options.waitAfterClickMs || 4500)));

      const preferPattern = chooseListingLinkPatternFromPage();
      const detector = new ListDetector({ preferHrefIncludes: preferPattern });

      const getCount = () => (detector.detect()?.items?.length || 0);

      let prevCount = getCount();
      let idle = 0;
      let clicks = 0;
      let scrolls = 0;

      for (let i = 0; i < maxActions; i++) {
        const btn = LoadMore._findLoadMoreControl(options);

        if (btn) {
          try {
            btn.scrollIntoView({ block: "center" });
          } catch (_) {}
          try {
            btn.click();
            clicks += 1;
          } catch (_) {
            // if click failed, attempt scroll fallback
            window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
            scrolls += 1;
          }

          // Wait for DOM to change / new items to appear
          const grew = await LoadMore._waitForMoreItems(detector, prevCount, waitAfterClickMs);
          if (!grew) {
            // also give a short grace delay (some sites load late)
            await new Promise((r) => setTimeout(r, stepDelayMs));
          }
        } else {
          window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
          scrolls += 1;
          await new Promise((r) => setTimeout(r, stepDelayMs));
        }

        const count = getCount();

        if (count > prevCount) {
          prevCount = count;
          idle = 0;
        } else {
          idle += 1;
          if (idle >= idleCycles) break;
        }
      }

      return {
        ok: true,
        finalCount: prevCount,
        clicks,
        scrolls,
        preferPattern: preferPattern || null
      };
    }
  }

  // ---------------------------------------------------------------------------
  // Extraction runner (uses override-first, then generic fallback)
  // ---------------------------------------------------------------------------

  class ExtractionRunner {
    async run() {
      const t0 = nowMs();
      const hostname = location.hostname;

      const override = await SiteOverrides.getForHost(hostname);


// Luximmo.com list pages: anchor-based extraction using stable listing URL pattern
// avoids page-level URL fallbacks that collapse many items into 1 unique.
if (isLuximmoHost(hostname)) {
  const items = extractLuximmoItemsFromPage();
  if (items && items.length >= 3) {
    const timingMs = Math.round(nowMs() - t0);
    return {
      ok: true,
      result: {
        dataVersion: 1,
        sourceUrl: location.href,
        pageTitle: document.title || null,
        extractedAt: new Date().toISOString(),
        meta: {
          siteProfileUsed: hostname,
          strategyUsed: "luximmo(anchor-root)",
          timingMs,
          itemCount: items.length,
          sampleLinks: items.map((it) => it.url).filter(Boolean).slice(0, 5),
        },
        items,
      },
    };
  }
}

      // Prefer override selectorCards if present
      if (override?.selectorCards?.length) {
        const preferPattern = override.listingLinkPattern || chooseListingLinkPatternFromPage();
        const extracted = this._extractUsingSelectors(override.selectorCards, preferPattern);

        if (extracted?.items?.length >= 3) {
          const timingMs = Math.round(nowMs() - t0);
          extracted.meta = extracted.meta || {};
          extracted.meta.siteProfileUsed = hostname;
          extracted.meta.strategyUsed = `override(selectorCards: ${override.selectorCards.join(", ")})`;
          extracted.meta.timingMs = timingMs;
          extracted.meta.itemCount = extracted.items.length;
          extracted.meta.sampleLinks = extracted.items.map((it) => it.url).filter(Boolean).slice(0, 5);
          return { ok: true, result: extracted };
        }
      }


      // Holmes.bg list pages: anchor-based extraction avoids page-level fallbacks (page title / toolbar icons)
      if (isHolmesHost(hostname)) {
        const items = extractHolmesItemsFromPage();
        if (items && items.length >= 3) {
          const timingMs = Math.round(nowMs() - t0);
          return {
            ok: true,
            result: {
              dataVersion: 1,
              sourceUrl: location.href,
              pageTitle: document.title || null,
              extractedAt: new Date().toISOString(),
              meta: {
                siteProfileUsed: hostname,
                strategyUsed: "holmes(anchor-root)",
                timingMs,
                itemCount: items.length,
                sampleLinks: items.map((it) => it.url).filter(Boolean).slice(0, 5),
              },
              items,
            },
          };
        }
      }

      // Fallback: generic list detector
      const preferPattern = override?.listingLinkPattern || chooseListingLinkPatternFromPage();
      const detector = new ListDetector({ preferHrefIncludes: preferPattern });
      const cand = detector.detect();

      if (!cand) {
        // ✅ Anchor-based fallback (helps sites where cards are nested, not direct children)
        const pattern = (preferPattern || "").toLowerCase();
        const anchors = Array.from(document.querySelectorAll("a[href]"))
          .filter((a) => a.href && !a.href.endsWith("#"))
          .filter((a) => {
            try {
              const u = new URL(a.href, location.href);
              return pattern ? (u.pathname || "").toLowerCase().includes(pattern) : false;
            } catch (_) {
              return false;
            }
          });

        // Collect likely card roots
        const roots = [];
        for (const a of anchors.slice(0, 400)) {
          const r = a.closest("article, li, div, section");
          if (r && r instanceof Element) roots.push(r);
        }

        if (roots.length >= 4) {
          // Build a reusable selector from a representative root
          const { selector } = buildReusableCardSelector(roots[0]);
          let nodes = [];
          try { nodes = Array.from(document.querySelectorAll(selector)); } catch (_) {}

          if (nodes.length >= 3) {
            const extractor = new ItemExtractor({ preferHrefIncludes: preferPattern });
            const items = nodes.slice(0, 200).map((el) => extractor.extractItem(el));

            const timingMs = Math.round(nowMs() - t0);
            return {
              ok: true,
              result: {
                dataVersion: 1,
                sourceUrl: location.href,
                pageTitle: document.title || null,
                extractedAt: new Date().toISOString(),
                meta: {
                  siteProfileUsed: hostname,
                  strategyUsed: `anchor-fallback(${preferPattern})`,
                  timingMs,
                  itemCount: items.length,
                  sampleLinks: items.map((it) => it.url).filter(Boolean).slice(0, 5),
                  containerTag: nodes[0]?.tagName?.toLowerCase?.() || null,
                  containerPath: this._cssPath(nodes[0] || null),
                },
                items,
              },
            };
          }
        }

        return { ok: false, error: "No repeating list detected. Scroll so items are rendered, then run again." };
      }

      const extractor = new ItemExtractor({ preferHrefIncludes: preferPattern });
      const items = cand.items.map((el) => extractor.extractItem(el));

      const result = {
        dataVersion: 1,
        sourceUrl: location.href,
        pageTitle: document.title || null,
        extractedAt: new Date().toISOString(),
        meta: {
          avgStructuralSimilarity: cand.avgSim,
          containerTag: cand.container.tagName.toLowerCase(),
          containerPath: this._cssPath(cand.container),
          siteProfileUsed: hostname,
          strategyUsed: "generic(ListDetector direct-children)",
          timingMs: Math.round(nowMs() - t0),
          itemCount: items.length,
          sampleLinks: items.map((it) => it.url).filter(Boolean).slice(0, 5),
        },
        items,
      };
      return { ok: true, result };
    }

    _extractUsingSelectors(selectors, preferPattern) {
      let nodes = [];
      for (const sel of selectors) {
        try {
          const found = Array.from(document.querySelectorAll(sel));
          if (found.length) {
            nodes = found;
            break;
          }
        } catch (_) {}
      }

      if (!nodes.length) return null;

      const extractor = new ItemExtractor({ preferHrefIncludes: preferPattern });
      const items = nodes.slice(0, 200).map((el) => extractor.extractItem(el));

      return {
        dataVersion: 1,
        sourceUrl: location.href,
        pageTitle: document.title || null,
        extractedAt: new Date().toISOString(),
        meta: {
          containerTag: nodes[0]?.tagName?.toLowerCase?.() || null,
          containerPath: this._cssPath(nodes[0]),
        },
        items,
      };
    }

    _cssPath(el) {
      if (!el || !(el instanceof Element)) return null;
      const parts = [];
      let cur = el;
      for (let i = 0; i < 6 && cur; i++) {
        let part = cur.tagName.toLowerCase();
        const cls = pickStableClasses(cur, 2);
        if (cls.length) part += "." + cls.map(cssEscape).join(".");
        parts.unshift(part);
        cur = cur.parentElement;
      }
      return parts.join(" > ");
    }
  }

  // ---------------------------------------------------------------------------
  // Onboarding checklist (returns report used by sidepanel)
  // ---------------------------------------------------------------------------

  class Onboarding {
    static run() {
      const hostname = location.hostname;
      const url = location.href;

      const listingLinkPattern = chooseListingLinkPatternFromPage();

      const allAnchors = Array.from(document.querySelectorAll("a[href]"))
        .filter((a) => a.href && !a.href.endsWith("#") && !isProbablyFooterOrNav(a));

      const matchingAnchors = listingLinkPattern
        ? allAnchors.filter((a) => {
            const u = tryUrl(a.href);
            return (u?.pathname || "").toLowerCase().includes(listingLinkPattern.toLowerCase());
          })
        : [];

      const wrapperCounts = new Map();
      const wrapperSamples = [];

      for (const a of matchingAnchors.slice(0, 400)) {
        const wrap = a.closest("article, li, div");
        if (!wrap) continue;
        const { selector } = buildReusableCardSelector(wrap);
        wrapperCounts.set(selector, (wrapperCounts.get(selector) || 0) + 1);
        if (wrapperSamples.length < 5) wrapperSamples.push(selector);
      }

      const rankedWrappers = Array.from(wrapperCounts.entries())
        .map(([selector, count]) => ({ selector, count }))
        .sort((a, b) => b.count - a.count)
        .slice(0, 8);

      const bestSelector = rankedWrappers[0]?.selector || null;

      const report = {
        ok: true,
        report: {
          hostname,
          url,
          listingLinkPattern,
          anchorsTotal: allAnchors.length,
          anchorsMatchingPattern: matchingAnchors.length,
          wrapperCandidates: rankedWrappers,
          suggestedOverride: bestSelector
            ? {
                selectorCards: [bestSelector],
                listingLinkPattern: listingLinkPattern || null,
              }
            : null,
        },
      };

      return report;
    }
  }

  // ---------------------------------------------------------------------------
  // Click-to-select picker (highlights elements; click returns selector)
  // ---------------------------------------------------------------------------

  class Picker {
    constructor() {
      this.active = false;
      this.overlay = null;
      this._onMove = this._onMove.bind(this);
      this._onClick = this._onClick.bind(this);
      this._lastEl = null;
    }

    start() {
      if (this.active) return;
      this.active = true;

      this.overlay = document.createElement("div");
      this.overlay.style.position = "fixed";
      this.overlay.style.pointerEvents = "none";
      this.overlay.style.zIndex = "2147483647";
      this.overlay.style.outline = "3px solid lime";
      this.overlay.style.background = "rgba(0,255,0,0.05)";
      document.documentElement.appendChild(this.overlay);

      document.addEventListener("mousemove", this._onMove, true);
      document.addEventListener("click", this._onClick, true);
    }

    stop() {
      if (!this.active) return;
      this.active = false;

      document.removeEventListener("mousemove", this._onMove, true);
      document.removeEventListener("click", this._onClick, true);

      if (this.overlay) this.overlay.remove();
      this.overlay = null;
      this._lastEl = null;
    }

    _onMove(e) {
      if (!this.active) return;
      const el = document.elementFromPoint(e.clientX, e.clientY);
      if (!el || !(el instanceof Element)) return;
      if (this.overlay && el === this.overlay) return;

      const target = el.closest("article, li, div") || el;
      if (!(target instanceof Element)) return;

      this._lastEl = target;

      const r = target.getBoundingClientRect();
      if (!this.overlay) return;
      this.overlay.style.left = `${Math.max(0, r.left)}px`;
      this.overlay.style.top = `${Math.max(0, r.top)}px`;
      this.overlay.style.width = `${Math.max(0, r.width)}px`;
      this.overlay.style.height = `${Math.max(0, r.height)}px`;
    }

    _onClick(e) {
      if (!this.active) return;

      e.preventDefault();
      e.stopPropagation();

      const clicked = this._lastEl || (document.elementFromPoint(e.clientX, e.clientY) instanceof Element
        ? document.elementFromPoint(e.clientX, e.clientY)
        : null);

      if (!(clicked instanceof Element)) {
        this.stop();
        return;
      }

      const { selector, matchCount } = buildReusableCardSelector(clicked);

      const anchors = Array.from(clicked.querySelectorAll("a[href]"));
      let listingLinkPattern = null;
      let sampleListingHref = null;

      for (const a of anchors) {
        const u = tryUrl(a.href);
        if (!u) continue;
        const p = (u.pathname || "").toLowerCase();
        if (p.includes("/obiava/")) listingLinkPattern = "/obiava/";
        else if (p.includes("/offer/")) listingLinkPattern = "/offer/";
        else if (p.includes("/listing/")) listingLinkPattern = "/listing/";
        else if (p.includes("/ad/")) listingLinkPattern = "/ad/";
        else if (p.includes("/property/")) listingLinkPattern = "/property/";
        if (listingLinkPattern) {
          sampleListingHref = a.href;
          break;
        }
      }

      if (!listingLinkPattern) listingLinkPattern = chooseListingLinkPatternFromPage();
      if (!sampleListingHref) sampleListingHref = firstNonEmpty(anchors.map((a) => a.href));

      chrome.runtime.sendMessage({
        type: "PICKER_RESULT",
        result: {
          hostname: location.hostname,
          url: location.href,
          selector,
          matchCount,
          listingLinkPattern,
          sampleListingHref,
        },
      });

      this.stop();
    }
  }

  const picker = new Picker();

  // ---------------------------------------------------------------------------
  // ✅ ADDITION: Initialize extractor API for Playwright runner (fixes timeout)
  // ---------------------------------------------------------------------------

  window.__imotiExtractor = window.__imotiExtractor || {
    version: 1,
    run: async () => {
      const runner = new ExtractionRunner();
      return await runner.run();
    },
    navigateNext: async () => {
      return await Navigation.navigateNext();
    },
    loadMore: async (options = {}) => {
      return await LoadMore.run(options);
    },
    loadMoreThenExtract: async (options = {}) => {
      await LoadMore.run(options);
      const runner = new ExtractionRunner();
      return await runner.run();
    },
  };

  // ---------------------------------------------------------------------------
  // Messaging
  // ---------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    (async () => {
      const type = msg?.type;

      if (type === "RUN_EXTRACTION") {
        const runner = new ExtractionRunner();
        sendResponse(await runner.run());
        return;
      }

      if (type === "NAVIGATE_NEXT_PAGE") {
        const didNavigate = await Navigation.navigateNext();
        sendResponse({ ok: true, didNavigate });
        return;
      }

      if (type === "LOAD_MORE") {
        await LoadMore.run(msg.options || {});
        sendResponse({ ok: true });
        return;
      }

      if (type === "LOAD_MORE_THEN_EXTRACT") {
        await LoadMore.run(msg.options || {});
        const runner = new ExtractionRunner();
        sendResponse(await runner.run());
        return;
      }

      if (type === "ONBOARD_SITE") {
        sendResponse(Onboarding.run());
        return;
      }

      if (type === "START_PICKER") {
        picker.start();
        sendResponse({ ok: true, started: true });
        return;
      }

      if (type === "STOP_PICKER") {
        picker.stop();
        sendResponse({ ok: true, stopped: true });
        return;
      }

      sendResponse({ ok: false, error: "Unknown message type" });
    })().catch((e) => {
      sendResponse({ ok: false, error: String(e?.message || e) });
    });
    return true;
  });
})();