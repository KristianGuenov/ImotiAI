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

  /**
   * ✅ Site-specific detector for imoti.net:
   * - imoti.net listings contain anchors with href including "/obiava"
   * - generic ListDetector can miss items due to nested structure / non-uniform direct children
   * - This detector finds card roots around unique listing links
   *
   * IMPORTANT: Only used when hostname matches imoti.net, so imot.bg remains on generic path.
   */
  class ImotiNetListingDetector {
    static isImotiNetHost() {
      const h = (location.hostname || "").toLowerCase();
      return h === "imoti.net" || h.endsWith(".imoti.net");
    }

    static _isVisible(el) {
      if (!(el instanceof Element)) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 12 || r.height < 12) return false;
      const s = window.getComputedStyle(el);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
      return true;
    }

    static _normalizeListingUrl(href) {
      try {
        const u = new URL(href, location.href);
        // Only keep origin + pathname to dedupe (strip query/hash)
        let p = u.pathname || "";
        // Normalize trailing slash
        if (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);
        return `${u.origin}${p}`;
      } catch (_) {
        return null;
      }
    }

    static _looksLikeListingLink(a) {
      if (!a || !a.href) return false;
      try {
        const u = new URL(a.href, location.href);
        if (!(u.hostname === "imoti.net" || u.hostname.endsWith(".imoti.net"))) return false;
        // Loose match: your console check was "/obiava"
        if (!u.pathname || !u.pathname.includes("/obiava")) return false;
        return true;
      } catch (_) {
        return false;
      }
    }

    static _scoreCardRoot(el) {
      if (!(el instanceof Element)) return -Infinity;
      if (!this._isVisible(el)) return -Infinity;

      const r = el.getBoundingClientRect();
      // Filter out absurd sizes
      if (r.width < 220 || r.height < 60 || r.height > 2200) return -Infinity;

      const txt = DomText.visibleText(el, 600).toLowerCase();
      const hasPriceSignal =
        /\b(лв|eur|€)\b/.test(txt) || /\b\d{2,}\s*(лв|eur|€)\b/.test(txt) || /\bцена\b/.test(txt);

      const imgCount = el.querySelectorAll("img").length;
      const linkCount = el.querySelectorAll("a[href*='/obiava']").length;

      // Prefer elements that look like listing cards: at least one listing link,
      // some text, and optionally price/img
      let score = 0;
      score += Math.min(linkCount, 3) * 3;
      score += Math.min(imgCount, 3) * 2;
      if (hasPriceSignal) score += 4;
      score += Math.min(txt.length / 200, 4);

      // Penalize if it's clearly a big container (likely full list)
      if (r.height > 1200) score -= 6;

      return score;
    }

    static _findCardRootFromAnchor(a) {
      // Try a few common semantic containers first
      const direct =
        a.closest("article") ||
        a.closest("li") ||
        a.closest("[role='listitem']") ||
        a.closest(".offer, .offer-item, .item, .result, .listing, .card") ||
        null;

      // If that works, keep it
      if (direct && this._scoreCardRoot(direct) > 0) return direct;

      // Otherwise: walk up and pick the best-scoring ancestor
      let cur = a;
      let best = null;
      let bestScore = -Infinity;

      // Walk up a bounded number of levels to avoid reaching body/html
      for (let i = 0; i < 12 && cur && cur.parentElement; i++) {
        cur = cur.parentElement;

        // Stop if we reached too high
        const tag = cur.tagName?.toLowerCase();
        if (tag === "body" || tag === "html") break;

        const sc = this._scoreCardRoot(cur);
        if (sc > bestScore) {
          bestScore = sc;
          best = cur;
        }

        // Early exit if we found something strongly card-like
        if (bestScore >= 10) break;
      }

      return bestScore > 0 ? best : null;
    }

    static _commonAncestor(nodes) {
      if (!nodes || !nodes.length) return null;
      const first = nodes[0];
      if (!first || !(first instanceof Element)) return null;

      // Collect ancestors of first
      const ancestors = [];
      let cur = first;
      for (let i = 0; i < 10 && cur; i++) {
        ancestors.push(cur);
        cur = cur.parentElement;
      }

      // Find the lowest ancestor that contains all nodes
      for (const a of ancestors) {
        let ok = true;
        for (const n of nodes) {
          if (!a.contains(n)) {
            ok = false;
            break;
          }
        }
        if (ok) return a;
      }
      return first.parentElement || first;
    }

    static detect(maxItems = 80) {
      // Gather listing anchors
      const anchors = Array.from(document.querySelectorAll("a[href*='/obiava']"))
        .filter((a) => a instanceof HTMLAnchorElement)
        .filter((a) => this._looksLikeListingLink(a))
        .filter((a) => this._isVisible(a));

      if (anchors.length < 4) return null;

      // Deduplicate by normalized listing URL (imoti.net often repeats links per card)
      const byUrl = new Map();
      for (const a of anchors) {
        const norm = this._normalizeListingUrl(a.href);
        if (!norm) continue;

        // Keep the anchor with the biggest clickable area as representative
        const prev = byUrl.get(norm);
        if (!prev) {
          byUrl.set(norm, a);
          continue;
        }
        const r1 = prev.getBoundingClientRect();
        const r2 = a.getBoundingClientRect();
        const area1 = Math.max(0, r1.width) * Math.max(0, r1.height);
        const area2 = Math.max(0, r2.width) * Math.max(0, r2.height);
        if (area2 > area1) byUrl.set(norm, a);
      }

      const reps = Array.from(byUrl.values());
      if (reps.length < 4) return null;

      // Convert anchors to card roots
      const roots = [];
      for (const a of reps) {
        const root = this._findCardRootFromAnchor(a);
        if (root) roots.push(root);
      }

      // Deduplicate roots
      const uniq = [];
      const seen = new Set();
      for (const r of roots) {
        if (!r || seen.has(r)) continue;
        seen.add(r);
        uniq.push(r);
      }

      if (uniq.length < 4) return null;

      const items = uniq.slice(0, Math.max(1, Math.min(maxItems, 200)));

      // Compute avg structural similarity like ListDetector for consistent meta
      let avgSim = 0;
      try {
        const fp0 = Fingerprint.fromElement(items[0]);
        const sims = [];
        for (let i = 1; i < Math.min(items.length, 12); i++) {
          const fpi = Fingerprint.fromElement(items[i]);
          sims.push(Fingerprint.cosineSimilarity(fp0.map, fpi.map));
        }
        avgSim = sims.length ? sims.reduce((a, b) => a + b, 0) / sims.length : 0.75;
      } catch (_) {
        avgSim = 0.75;
      }

      const container = this._commonAncestor(items) || items[0].parentElement || items[0];
      const score = items.length * avgSim + 5; // basic boost; we already have strong signals

      return new ListCandidate(container, items, score, avgSim);
    }
  }

  class ItemExtractor {
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
    _pickPrimaryLink(el) {
      const links = Array.from(el.querySelectorAll("a[href]"))
        .map((x) => x)
        .filter((a) => a.href && !a.href.endsWith("#"));
      if (!links.length) return null;

      let best = links[0],
        bestScore = -Infinity;
      for (const l of links.slice(0, 20)) {
        const rect = l.getBoundingClientRect();
        const area = Math.max(0, rect.width) * Math.max(0, rect.height);
        const t = DomText.normalize(l.textContent);
        const tScore = Math.min(t.length / 40, 2);
        const score = area + tScore * 400;
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
        if (t) return DomText.safeTruncate(t, 160);
      }
      if (primaryLink) {
        const t = DomText.normalize(primaryLink.textContent);
        if (t && t.length >= 3) return DomText.safeTruncate(t, 160);
        const aria = DomText.normalize(primaryLink.getAttribute("aria-label"));
        if (aria) return DomText.safeTruncate(aria, 160);
      }
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
      // ✅ Try imoti.net specialized detector first (only on imoti.net)
      let cand = null;
      if (ImotiNetListingDetector.isImotiNetHost()) {
        cand = ImotiNetListingDetector.detect(80);
      }

      // Fallback: generic detection for all sites (incl. imot.bg)
      if (!cand) {
        const detector = new ListDetector();
        cand = detector.detect();
      }

      if (!cand) {
        return { ok: false, error: "No repeating list detected. Scroll so items are rendered, then run again." };
      }

      const extractor = new ItemExtractor();
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
          siteDetector: ImotiNetListingDetector.isImotiNetHost() ? "imoti.net-specialized" : "generic",
        },
        items,
      };
      return { ok: true, result };
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
    // Optional: expose internals for debugging
    _internals: {
      DomText,
      Fingerprint,
      ListDetector,
      ItemExtractor,
      Navigation,
      LoadMore,
      ExtractionRunner,
      ImotiNetListingDetector,
    },
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

        sendResponse({ ok: false, error: "Unknown message type" });
      })().catch((e) => {
        sendResponse({ ok: false, error: String(e?.message || e) });
      });
      return true;
    });
  }
})();
