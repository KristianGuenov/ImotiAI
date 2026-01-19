/* global chrome */
(function () {
  "use strict";

  // ✅ Guard: avoid re-registering listeners if injected multiple times
  if (window.__realEstateExtractorInjected) return;
  window.__realEstateExtractorInjected = true;

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
    constructor(container, items, score, avgSim) {
      this.container = container;
      this.items = items;
      this.score = score;
      this.avgSim = avgSim;
    }
  }

  class ListDetector {
    constructor() {
      this.MIN_ITEMS = 4;
      this.MAX_ITEMS = 80;
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

        const linkCount = items.slice(0, 12).filter((it) => it.querySelector("a[href]")).length;
        const linkRatio = linkCount / Math.min(items.length, 12);
        const textLen = DomText.visibleText(c, 800).length;

        const score = items.length * avgSim + linkRatio * 5 + Math.min(textLen / 400, 2);
        candidates.push(new ListCandidate(c, items, score, avgSim));
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

  class ItemExtractor {
  constructor(options = {}) {
    // optional hint; if you later pass it from a site-profile, it helps even more
    this.preferHrefIncludes = options.preferHrefIncludes || null; // e.g. "/obiava"
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
    // "1/8", " 1 / 10 ", "1 /10"
    return /^\d+\s*\/\s*\d+$/.test(s);
  }

  _isNoiseTitle(t) {
    const s = DomText.normalize(t).toLowerCase();
    if (!s) return true;
    if (this._isFractionLikeTitle(s)) return true;
    // common UI labels that shouldn't be treated as listing title
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

      // Base: clickable area matters
      let score = area;

      // Prefer meaningful link text
      // (small bonus to avoid overlay counters winning)
      score += Math.min(textLen / 20, 4) * 250;

      // Strongly prefer listing-detail href patterns (imoti.info: /obiava)
      if (this.preferHrefIncludes) {
        try {
          const u = new URL(l.href, location.href);
          if ((u.pathname || "").toLowerCase().includes(this.preferHrefIncludes.toLowerCase())) {
            score += 1500;
          }
        } catch (_) {}
      } else {
        // generic heuristic: if it's same-host and looks like a detail page (long path)
        try {
          const u = new URL(l.href, location.href);
          if (u.hostname === location.hostname && (u.pathname || "").split("/").filter(Boolean).length >= 2) {
            score += 150;
          }
        } catch (_) {}
      }

      // Penalize "fraction-like" overlay text (e.g. 1/8)
      if (this._isFractionLikeTitle(text)) score -= 2000;

      // Penalize links that are basically just an image (often gallery wrapper)
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
    // 1) headings win if present
    const head = el.querySelector("h1,h2,h3,h4");
    if (head) {
      const t = DomText.normalize(head.textContent);
      if (t && !this._isNoiseTitle(t)) return DomText.safeTruncate(t, 160);
    }

    // 2) primary link text, but reject noise like "1/8"
    if (primaryLink) {
      const t = DomText.normalize(primaryLink.textContent);
      if (t && t.length >= 3 && !this._isNoiseTitle(t)) return DomText.safeTruncate(t, 160);

      const aria = DomText.normalize(primaryLink.getAttribute("aria-label"));
      if (aria && !this._isNoiseTitle(aria)) return DomText.safeTruncate(aria, 160);
    }

    // 3) fallback: pick best-looking anchor text inside card
    const anchors = Array.from(el.querySelectorAll("a[href]")).slice(0, 60);
    let best = null;
    let bestScore = -Infinity;

    for (const a of anchors) {
      const txt = DomText.normalize(a.textContent);
      if (!txt || txt.length < 3) continue;
      if (this._isNoiseTitle(txt)) continue;

      // prefer longer, human-ish titles, but keep it bounded
      let score = Math.min(txt.length, 60);

      // prefer links that look like detail pages (imoti.info: /obiava)
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

    // 4) last resort: visible text
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
    static async run(options = {}) {
      const scrollSteps = Math.max(1, Math.min(200, Number(options.scrollSteps || 12)));
      const idleCycles = Math.max(1, Math.min(10, Number(options.idleCycles || 2)));
      const stepDelayMs = Math.max(200, Math.min(6000, Number(options.stepDelayMs || 800)));

      const detector = new ListDetector();
      const cand = detector.detect();
      const getCount = () => cand?.items?.length || 0;

      let prevCount = getCount();
      let idle = 0;

      for (let i = 0; i < scrollSteps; i++) {
        window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
        await new Promise((r) => setTimeout(r, stepDelayMs));

        const cand2 = detector.detect();
        const count = cand2?.items?.length || prevCount;

        if (count > prevCount) {
          prevCount = count;
          idle = 0;
        } else {
          idle += 1;
          if (idle >= idleCycles) break;
        }
      }
      return true;
    }
  }

  class ExtractionRunner {
    run() {
      const t0 = performance.now();
      const host = (location.hostname || "").toLowerCase();

      const isImoti =
        host === "imoti.info" ||
        host.endsWith(".imoti.info") ||
        host === "imoti.net" ||
        host.endsWith(".imoti.net");

      // ---------- Strategy 1: imoti.* group-by-href (/obiava/) ----------
      if (isImoti) {
        const out = this._extractImotiGroupedByHref();
        if (out?.items?.length) {
          const timingMs = Math.round(performance.now() - t0);
          const result = {
            dataVersion: 1,
            sourceUrl: location.href,
            pageTitle: document.title || null,
            extractedAt: new Date().toISOString(),
            meta: {
              siteProfileUsed: "imoti.*",
              strategyUsed: "groupByHref(/obiava/)->cardWrapper->forcedUrl",
              itemCount: out.items.length,
              sampleLinks: out.items.slice(0, 5).map((x) => x.url).filter(Boolean),
              containerPath: out.containerPath || null,
              timingMs,
            },
            items: out.items,
          };
          return { ok: true, result };
        }
        // fallback to generic below if needed
      }

      // ---------- Strategy 2: generic ListDetector (unchanged) ----------
      const detector = new ListDetector();
      const cand = detector.detect();
      if (!cand) {
        return { ok: false, error: "No repeating list detected. Scroll so items are rendered, then run again." };
      }

      const extractor = new ItemExtractor();
      const items = cand.items.map((el) => extractor.extractItem(el));

      const timingMs = Math.round(performance.now() - t0);
      const result = {
        dataVersion: 1,
        sourceUrl: location.href,
        pageTitle: document.title || null,
        extractedAt: new Date().toISOString(),
        meta: {
          siteProfileUsed: "generic",
          strategyUsed: "generic(ListDetector direct-children)",
          itemCount: items.length,
          sampleLinks: items.slice(0, 5).map((x) => x.url).filter(Boolean),
          avgStructuralSimilarity: cand.avgSim,
          containerTag: cand.container.tagName.toLowerCase(),
          containerPath: this._cssPath(cand.container),
          timingMs,
        },
        items,
      };
      return { ok: true, result };
    }

    _extractImotiGroupedByHref() {
      const prefer = "/obiava/";
      const all = Array.from(document.querySelectorAll("a[href]"))
        .filter((a) => {
          try {
            const u = new URL(a.href, location.href);
            return u.hostname === location.hostname && (u.pathname || "").toLowerCase().includes(prefer);
          } catch {
            return false;
          }
        })
        .filter((a) => !a.closest("nav,.pagination,.pager,[aria-label*='page'],[class*='pag'],footer"));

      if (all.length < 10) return null;

      // Group anchors by listing href
      const byHref = new Map();
      for (const a of all) {
        const key = a.href;
        if (!byHref.has(key)) byHref.set(key, []);
        byHref.get(key).push(a);
      }

      // Use your ItemExtractor noise logic to pick best title anchor per href
      const tmpExtractor = new ItemExtractor({ preferHrefIncludes: prefer });

      const items = [];
      let firstCardEl = null;

      for (const [href, anchors] of byHref.entries()) {
        // choose best anchor text for title
        const bestA = this._pickBestTitleAnchor(tmpExtractor, anchors);
        const card = this._pickCardWrapperForHref(href, anchors);

        if (!card) continue;
        if (!firstCardEl) firstCardEl = card;

        // Extract from the card but FORCE url to the listing href
        const title = tmpExtractor._pickTitle(card, bestA || anchors[0]);
        const images = tmpExtractor._pickImages(card);
        const texts = tmpExtractor._pickTexts(card);

        items.push({
          title,
          url: href,
          image: images[0] || null,
          images,
          texts,
          rawText: DomText.visibleText(card, 600),
        });
      }

      if (items.length < 6) return null;

      return {
        items: items.slice(0, 80),
        containerPath: firstCardEl ? this._cssPath(firstCardEl) : null,
      };
    }

    _pickBestTitleAnchor(extractor, anchors) {
      let best = null;
      let bestScore = -Infinity;

      for (const a of anchors) {
        const txt = DomText.normalize(a.textContent);
        if (!txt) continue;
        if (extractor._isNoiseTitle(txt)) continue;

        // prefer human titles like "3-стаен..." over long price blobs
        let score = Math.min(txt.length, 80);

        // mild boost if it contains common listing words
        const s = txt.toLowerCase();
        if (s.includes("стаен") || s.includes("кв.м") || s.includes("тухла")) score += 25;

        if (score > bestScore) {
          bestScore = score;
          best = a;
        }
      }
      return best;
    }

    _pickCardWrapperForHref(href, anchors) {
      // Find a local wrapper that contains both picture+info OR is "card-sized",
      // and does NOT contain many other /obiava/ hrefs.
      // We'll try candidates from the best anchor upwards.
      const start = anchors.find((a) => DomText.normalize(a.textContent)) || anchors[0];
      let cur = start;

      for (let i = 0; i < 12 && cur; i++) {
        cur = cur.parentElement;
        if (!cur) break;

        const r = cur.getBoundingClientRect();
        if (!r || r.width < 250 || r.height < 60) continue;

        const vw = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0);
        const vh = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0);

        // reject huge wrappers (whole page / main layout)
        if (r.width > vw * 0.98 && r.height > vh * 0.85) continue;

        const style = window.getComputedStyle(cur);
        if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") continue;

        const hasPic = !!cur.querySelector("div.picture");
        const hasInfo = !!cur.querySelector("div.info");

        const listingAnchors = Array.from(cur.querySelectorAll("a[href*='/obiava/']"));
        const uniqueListings = new Set(listingAnchors.map((a) => a.href)).size;

        // A card wrapper should not contain many other listing hrefs
        if (uniqueListings > 2) continue;

        // And should contain the current href
        if (!listingAnchors.some((a) => a.href === href)) continue;

        // Prefer wrappers that look like the actual card composition
        if (hasPic && hasInfo) return cur;

        // Otherwise accept if it has a small number of anchors and reasonable text
        const txtLen = DomText.visibleText(cur, 280).length;
        if (txtLen >= 15 && listingAnchors.length >= 2 && listingAnchors.length <= 8) return cur;
      }

      return null;
    }

    _cssPath(el) {
      if (!el || !(el instanceof Element)) return null;
      const parts = [];
      let cur = el;
      for (let i = 0; i < 6 && cur; i++) {
        let part = cur.tagName.toLowerCase();
        if (cur.id) {
          part += "#" + cur.id;
          parts.unshift(part);
          break;
        }
        const cls = (cur.className || "")
          .toString()
          .trim()
          .split(/\s+/)
          .filter(Boolean)
          .slice(0, 2);
        if (cls.length) part += "." + cls.join(".");
        parts.unshift(part);
        cur = cur.parentElement;
      }
      return parts.join(" > ");
    }
  }




  class Onboarding {
    static _cssPath(el) {
      if (!el || !(el instanceof Element)) return null;
      const parts = [];
      let cur = el;
      for (let i = 0; i < 6 && cur; i++) {
        let part = cur.tagName.toLowerCase();
        if (cur.id) {
          part += "#" + cur.id;
          parts.unshift(part);
          break;
        }
        const cls = (cur.className || "")
          .toString()
          .trim()
          .split(/\s+/)
          .filter(Boolean)
          .slice(0, 2);
        if (cls.length) part += "." + cls.join(".");
        parts.unshift(part);
        cur = cur.parentElement;
      }
      return parts.join(" > ");
    }

    static _stableSelectorFor(el) {
      if (!el || !(el instanceof Element)) return null;
      const tag = el.tagName.toLowerCase();
      if (el.id) return `#${CSS.escape(el.id)}`;

      const classes = Array.from(el.classList || [])
        .map((c) => String(c))
        .filter((c) => c.length >= 3)
        .filter((c) => /^[a-zA-Z][a-zA-Z0-9_-]+$/.test(c))
        .filter((c) => !/^(active|selected|open|closed|hover|focus|ng-|js-)/i.test(c))
        .slice(0, 2);

      if (classes.length) {
        return `${tag}.${classes.map((c) => CSS.escape(c)).join(".")}`;
      }

      // Fallbacks for common controls
      const rel = el.getAttribute("rel");
      if (rel === "next") return `${tag}[rel="next"]`;

      const aria = (el.getAttribute("aria-label") || "").trim();
      if (aria) return `${tag}[aria-label*="${aria.replace(/\"/g, "")}"]`;

      return tag;
    }

    static _pickListingLinkIncludes(pathsLower) {
      // Priority: obvious real-estate listing tokens
      const priority = ["/obiava", "/offer", "/listing", "/imot", "/property", "/estate", "/ad/"];
      for (const p of priority) {
        if (pathsLower.some((x) => x.includes(p))) return p;
      }

      // Otherwise, choose the most frequent non-generic 1-2 segment prefix
      const stopSeg = new Set([
        "bg",
        "en",
        "search",
        "filter",
        "login",
        "register",
        "account",
        "user",
        "profile",
        "favorites",
        "favourites",
        "static",
        "assets",
        "css",
        "js",
        "images",
        "img",
        "api",
      ]);

      const counts = new Map();
      for (const path of pathsLower) {
        const segs = String(path || "")
          .split("?")[0]
          .split("#")[0]
          .split("/")
          .filter(Boolean);
        if (!segs.length) continue;
        if (stopSeg.has(segs[0])) continue;
        const one = "/" + segs[0];
        counts.set(one, (counts.get(one) || 0) + 1);
        if (segs.length >= 2 && !stopSeg.has(segs[1])) {
          const two = "/" + segs[0] + "/" + segs[1];
          counts.set(two, (counts.get(two) || 0) + 1);
        }
      }

      const ranked = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
      const best = ranked[0]?.[0] || null;
      return best;
    }

    static run() {
      const hostname = location.hostname || null;
      const url = location.href || null;
      const title = document.title || null;

      const allAnchors = Array.from(document.querySelectorAll("a[href]"))
        .map((a) => a)
        .filter((a) => !!a.getAttribute("href"));

      const hrefs = [];
      const pathsLower = [];
      for (const a of allAnchors.slice(0, 4000)) {
        try {
          const u = new URL(a.href, location.href);
          hrefs.push(u.href);
          if (u.hostname === location.hostname) pathsLower.push((u.pathname || "").toLowerCase());
        } catch (_) {}
      }

      const listingLinkIncludes = Onboarding._pickListingLinkIncludes(pathsLower);
      const listingAnchors = listingLinkIncludes
        ? allAnchors.filter((a) => {
            try {
              return new URL(a.href, location.href).pathname.toLowerCase().includes(listingLinkIncludes);
            } catch (_) {
              return false;
            }
          })
        : [];

      // Suggest card roots via common ancestors of listing anchors
      const rootCounts = new Map();
      const rootEls = new Map();
      const rootsSample = [];
      const rootTags = ["article", "li", "div"]; // conservative

      for (const a of listingAnchors.slice(0, 250)) {
        let root = null;
        for (const tag of rootTags) {
          root = a.closest(tag);
          if (root) break;
        }
        root = root || a.parentElement;
        if (!root) continue;

        const sel = Onboarding._stableSelectorFor(root);
        if (!sel) continue;
        rootCounts.set(sel, (rootCounts.get(sel) || 0) + 1);
        if (!rootEls.has(sel)) rootEls.set(sel, root);
      }

      const rankedRoots = Array.from(rootCounts.entries()).sort((a, b) => b[1] - a[1]);
      const cardRootSelectors = rankedRoots.slice(0, 5).map((x) => x[0]);
      const bestRootSel = rankedRoots[0]?.[0] || null;
      const bestRootEl = bestRootSel ? rootEls.get(bestRootSel) : null;
      const containerPath = bestRootEl ? Onboarding._cssPath(bestRootEl) : null;

      // Pagination hint
      const nextCtrl = Navigation.findNextControl();
      const paginationHint = (() => {
        if (!nextCtrl) return null;
        if (nextCtrl.type === "href") return { type: "href", href: nextCtrl.href || null };
        if (nextCtrl.type === "click" && nextCtrl.el) {
          return {
            type: "click",
            selector: Onboarding._stableSelectorFor(nextCtrl.el),
            containerPath: Onboarding._cssPath(nextCtrl.el),
          };
        }
        return null;
      })();

      const checklist = {
        hostname,
        url,
        title,
        anchorsTotal: allAnchors.length,
        listingLinkIncludes,
        listingAnchorsFound: listingAnchors.length,
        cardRootSelectors,
        containerPath,
        paginationHint,
      };

      const recommendedOverride = hostname
        ? {
            [hostname]: {
              listingLinkIncludes: listingLinkIncludes || null,
              cardRootSelectors: cardRootSelectors.length ? cardRootSelectors : null,
              waitForSelector: cardRootSelectors.length ? cardRootSelectors[0] : null,
              pagination: paginationHint ? { next: paginationHint } : null,
            },
          }
        : {};

      return {
        checklist,
        recommendedOverride,
        timingMs: null,
      };
    }
  }

  // ✅ STEP 1: Expose a reusable API on window (Chrome extension OR Playwright)
  const api = {
    version: 1,
    run() {
      const runner = new ExtractionRunner();
      return runner.run();
    },
    async navigateNext() {
      return Navigation.navigateNext();
    },
    async loadMore(options = {}) {
      return LoadMore.run(options);
    },
    async loadMoreThenExtract(options = {}) {
      await LoadMore.run(options);
      const runner = new ExtractionRunner();
      return runner.run();
    },

    // ✅ Step 5 (as code): "Site onboarding checklist" runner
    // Produces a self-report + a suggested per-host override.
    // This is intentionally heuristic — the goal is to generate *something*
    // you can save in chrome.storage and iterate on.
    onboard() {
      const t0 = performance.now();
      const report = Onboarding.run();
      report.timingMs = Math.round(performance.now() - t0);
      return report;
    },
    // Optional: expose internals for debugging
    _internals: { DomText, Fingerprint, ListDetector, ItemExtractor, Navigation, LoadMore, ExtractionRunner },
  };

  // Keep existing if already present (don’t break other injections)
  window.__imotiExtractor = window.__imotiExtractor || api;

  // ✅ Chrome-only messaging (guarded so Playwright doesn’t crash)
  if (typeof chrome !== "undefined" && chrome?.runtime?.onMessage?.addListener) {
    chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
      (async () => {
        const type = msg?.type;

        // Backward compatible
        if (type === "RUN_EXTRACTION" || type === "DETECT_AND_EXTRACT") {
          sendResponse(window.__imotiExtractor.run());
          return;
        }

        if (type === "NAVIGATE_NEXT_PAGE" || type === "NAVIGATE_NEXT") {
          const didNavigate = await window.__imotiExtractor.navigateNext();
          sendResponse({ ok: true, didNavigate });
          return;
        }

        if (type === "LOAD_MORE") {
          await window.__imotiExtractor.loadMore(msg.options || {});
          sendResponse({ ok: true });
          return;
        }

        if (type === "LOAD_MORE_THEN_EXTRACT") {
          const out = await window.__imotiExtractor.loadMoreThenExtract(msg.options || {});
          sendResponse(out);
          return;
        }

        if (type === "ONBOARD_SITE") {
          const report = window.__imotiExtractor.onboard();
          sendResponse({ ok: true, report });
          return;
        }

        sendResponse({ ok: false, error: "Unknown message type" });
      })().catch((e) => {
        sendResponse({ ok: false, error: String(e?.message || e) });
      });
      return true;
    });
  }
})();
