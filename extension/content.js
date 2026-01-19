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
    constructor(container, items, totalCount, score, avgSim) {
      this.container = container;
      this.items = items; // capped list for extraction
      this.totalCount = totalCount; // full child count (for load-more growth detection)
      this.score = score;
      this.avgSim = avgSim;
    }
  }

  class ListDetector {
    constructor() {
      this.MIN_ITEMS = 4;
      this.MAX_ITEMS = 200;
    }
    detect() {
      const containers = this._collectContainers();
      const candidates = [];
      for (const c of containers) {
        const kids = Array.from(c.children || []).filter((x) => x instanceof Element);
        if (kids.length < this.MIN_ITEMS) continue;

        const totalCount = kids.length;
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
        candidates.push(new ListCandidate(c, items, totalCount, score, avgSim));
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
    static _isVisible(el) {
      if (!(el instanceof Element)) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 10 || r.height < 10) return false;
      const s = window.getComputedStyle(el);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
      return true;
    }

    static _isDisabled(el) {
      return !!(
        el.hasAttribute("disabled") ||
        el.getAttribute("aria-disabled") === "true" ||
        el.getAttribute("disabled") === "true"
      );
    }

    static _norm(s) {
      return DomText.normalize(s).toLowerCase();
    }

    static _scoreLoadMore(el) {
      const text = LoadMore._norm(el.textContent);
      const aria = LoadMore._norm(el.getAttribute("aria-label"));
      const title = LoadMore._norm(el.getAttribute("title"));
      const cls = LoadMore._norm(el.className || "");
      const id = LoadMore._norm(el.id || "");
      const combined = `${text} ${aria} ${title} ${cls} ${id}`;

      const positives = [
        "load more",
        "show more",
        "see more",
        "more results",
        "load next",
        "покажи още",
        "покажи повече",
        "виж още",
        "зареди още",
        "още",
        "повече",
      ];
      const negatives = ["next", "следва", "следваща", "previous", "предиш", "page", "страница"]; // avoid pagination

      let score = 0;
      for (const p of positives) {
        if (combined.includes(p)) score += p.length >= 5 ? 6 : 3;
      }
      for (const n of negatives) {
        if (combined.includes(n)) score -= 5;
      }

      // Extra signals from common classnames / attributes
      if (cls.includes("load") && cls.includes("more")) score += 6;
      if (cls.includes("show") && cls.includes("more")) score += 5;
      if (cls.includes("infinite")) score += 3;
      if (cls.includes("btn") && cls.includes("more")) score += 3;

      // Prefer controls that are lower on the page
      const r = el.getBoundingClientRect();
      const nearBottom = r.top > window.innerHeight * 0.35;
      if (nearBottom) score += 2;

      // Prefer actual buttons/anchors
      const tag = el.tagName.toLowerCase();
      if (tag === "button") score += 2;
      if (tag === "a" && el.getAttribute("href")) score += 1;

      return score;
    }

    static findLoadMoreControl(container) {
      const roots = [];
      if (container instanceof Element) roots.push(container);
      if (container?.parentElement) roots.push(container.parentElement);
      roots.push(document.body);

      const selector = "button, a[href], [role='button'], input[type='button'], input[type='submit']";

      let best = null;
      let bestScore = 0;

      for (const root of roots) {
        const nodes = Array.from(root.querySelectorAll(selector)).slice(0, 400);
        for (const el of nodes) {
          if (!LoadMore._isVisible(el)) continue;
          if (LoadMore._isDisabled(el)) continue;

          // Avoid picking a button inside the extension UI or unrelated overlays
          if (el.closest("#__imotiExtractorUi, [data-imoti-extractor-ui]")) continue;

          const score = LoadMore._scoreLoadMore(el);
          if (score > bestScore) {
            bestScore = score;
            best = el;
          }
        }

        // If we already found a strong candidate near the container, stop early
        if (best && bestScore >= 8) break;
      }

      return bestScore >= 6 ? best : null;
    }

    static async _humanClick(el) {
      try {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
      } catch (_) {}

      // Give layout a moment
      await new Promise((r) => setTimeout(r, 150));

      // Try standard click
      try {
        el.click();
        return;
      } catch (_) {}

      // Fallback: dispatch mouse events
      try {
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const opts = { bubbles: true, cancelable: true, clientX: cx, clientY: cy };
        el.dispatchEvent(new MouseEvent("mouseover", opts));
        el.dispatchEvent(new MouseEvent("mousedown", opts));
        el.dispatchEvent(new MouseEvent("mouseup", opts));
        el.dispatchEvent(new MouseEvent("click", opts));
      } catch (_) {}
    }

    static async _waitForListGrowth(detector, prevCount, timeoutMs) {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        const cand = detector.detect();
        const count = cand?.totalCount ?? prevCount;
        if (count > prevCount) return { grew: true, cand, count };
        await new Promise((r) => setTimeout(r, 250));
      }
      const cand = detector.detect();
      const count = cand?.totalCount ?? prevCount;
      return { grew: count > prevCount, cand, count };
    }

    static async run(options = {}) {
      // Keep UI naming for compatibility: scrollSteps = max actions
      const maxActions = Math.max(1, Math.min(250, Number(options.scrollSteps || 12)));
      const idleCycles = Math.max(1, Math.min(15, Number(options.idleCycles || 2)));
      const stepDelayMs = Math.max(150, Math.min(8000, Number(options.stepDelayMs || 800)));
      const growthTimeoutMs = Math.max(1000, Math.min(20000, Number(options.growthTimeoutMs || 8000)));
      const clickLoadMore = options.clickLoadMore !== false;

      const detector = new ListDetector();

      const state0 = detector.detect();
      let prevCount = state0?.totalCount || 0;
      let idle = 0;

      for (let i = 0; i < maxActions; i++) {
        const cand = detector.detect();
        const container = cand?.container || null;

        let acted = false;

        // Prefer clicking explicit "Load more" if present
        if (clickLoadMore && container) {
          const btn = LoadMore.findLoadMoreControl(container);
          if (btn) {
            await LoadMore._humanClick(btn);
            acted = true;
          }
        }

        // Fallback: infinite scroll
        if (!acted) {
          window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
          acted = true;
        }

        await new Promise((r) => setTimeout(r, stepDelayMs));

        const after = await LoadMore._waitForListGrowth(detector, prevCount, growthTimeoutMs);
        if (after.count > prevCount) {
          prevCount = after.count;
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
      const detector = new ListDetector();
      const cand = detector.detect();
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
          totalDetectedItems: cand.totalCount ?? cand.items.length,
          extractedItems: items.length,
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

        sendResponse({ ok: false, error: "Unknown message type" });
      })().catch((e) => {
        sendResponse({ ok: false, error: String(e?.message || e) });
      });
      return true;
    });
  }
})();
