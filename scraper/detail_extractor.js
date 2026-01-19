(function () {
  "use strict";

  if (window.__listingDetailExtractor?.extract) return;

  function normalize(s) {
    return String(s || "").replace(/\s+/g, " ").trim();
  }

  function isVisible(el) {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return !!r && r.width > 0 && r.height > 0;
  }

  function getText(el, maxLen = 20000) {
    if (!el) return "";
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const t = normalize(node.nodeValue);
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
      out += " " + normalize(walker.currentNode.nodeValue);
      if (out.length >= maxLen) break;
    }
    return normalize(out).slice(0, maxLen);
  }

  function scoreCandidate(el) {
    if (!el || !(el instanceof Element)) return -Infinity;
    if (!isVisible(el)) return -Infinity;

    const id = (el.id || "").toLowerCase();
    const cls = (el.className || "").toString().toLowerCase();
    const tag = (el.tagName || "").toLowerCase();
    const hint = `${id} ${cls}`.trim();

    const hintBonus =
      (hint.includes("desc") ? 3 : 0) +
      (hint.includes("opis") ? 3 : 0) +
      (hint.includes("text") ? 1 : 0) +
      (hint.includes("detail") ? 1 : 0) +
      (hint.includes("info") ? 1 : 0) +
      (hint.includes("content") ? 1 : 0);

    const badPenalty =
      (tag === "nav" ? 4 : 0) +
      (tag === "header" ? 4 : 0) +
      (tag === "footer" ? 4 : 0) +
      (tag === "aside" ? 4 : 0) +
      (hint.includes("menu") ? 3 : 0) +
      (hint.includes("header") ? 3 : 0) +
      (hint.includes("footer") ? 3 : 0) +
      (hint.includes("breadcrumb") ? 2 : 0) +
      (hint.includes("pagination") ? 2 : 0) +
      (hint.includes("social") ? 2 : 0) +
      (hint.includes("share") ? 2 : 0) +
      (hint.includes("comment") ? 2 : 0);

    const txt = getText(el, 12000);
    const len = txt.length;

    const lenScore =
      len < 80 ? -5 :
      len < 150 ? -2 :
      len < 400 ? 1 :
      len < 2000 ? 4 :
      len < 8000 ? 2 : 0;

    return hintBonus + lenScore - badPenalty;
  }

  function findByLabelOpisanie() {
    const nodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,strong,b,div,span,p"))
      .filter((n) => {
        const t = normalize(n.textContent).toLowerCase();
        return t === "описание" || t.startsWith("описание");
      });

    for (const n of nodes.slice(0, 20)) {
      const next =
        n.nextElementSibling ||
        n.parentElement?.nextElementSibling ||
        n.parentElement?.querySelector?.("p,div,span");

      if (next && isVisible(next)) {
        const t = getText(next, 12000);
        if (t.length >= 120) return t;
      }
    }
    return "";
  }

  function pickTitle() {
    const h1 = document.querySelector("h1");
    const t1 = normalize(h1?.textContent);
    if (t1 && t1.length >= 3) return t1.slice(0, 200);

    const og = document.querySelector('meta[property="og:title"]');
    const t2 = normalize(og?.getAttribute("content"));
    if (t2 && t2.length >= 3) return t2.slice(0, 200);

    const t3 = normalize(document.title);
    return t3 ? t3.slice(0, 200) : null;
  }

  function extractDescription() {
    const ogd = document.querySelector('meta[property="og:description"]');
    const ogdText = normalize(ogd?.getAttribute("content"));
    if (ogdText && ogdText.length >= 120) return ogdText;

    const itemprop = document.querySelector('[itemprop="description"]');
    const itempropText = getText(itemprop, 12000);
    if (itempropText && itempropText.length >= 120) return itempropText;

    const byLabel = findByLabelOpisanie();
    if (byLabel && byLabel.length >= 120) return byLabel;

    const candidates = [];

    const semantic = [
      "#description",
      ".description",
      ".descr",
      ".desc",
      ".opisanie",
      ".opis",
      ".text",
      ".adText",
      ".ad-text",
      ".details",
      ".detail",
      ".offer",
      ".offertext",
    ];

    for (const sel of semantic) {
      for (const el of Array.from(document.querySelectorAll(sel))) {
        if (isVisible(el)) candidates.push(el);
      }
    }

    const genericRoots = Array.from(
      new Set([
        document.querySelector("main"),
        document.querySelector("article"),
        document.querySelector("#content"),
        document.querySelector(".content"),
        document.body,
      ].filter(Boolean))
    );

    for (const root of genericRoots) {
      const els = Array.from(root.querySelectorAll("section,article,div,p,ul,ol")).slice(0, 400);
      for (const el of els) {
        const sc = scoreCandidate(el);
        if (sc > 0) candidates.push(el);
      }
    }

    let best = null;
    let bestScore = -Infinity;
    for (const el of candidates) {
      const sc = scoreCandidate(el);
      if (sc > bestScore) {
        bestScore = sc;
        best = el;
      }
    }

    const bestText = best ? getText(best, 12000) : "";
    if (bestText && bestText.length >= 120) return bestText;

    const ps = Array.from(document.querySelectorAll("p,div,span")).slice(0, 800);
    let longest = "";
    for (const el of ps) {
      if (!isVisible(el)) continue;
      const t = getText(el, 12000);
      if (t.length > longest.length) longest = t;
    }
    return longest;
  }

  window.__listingDetailExtractor = {
    version: 1,
    extract() {
      const description = normalize(extractDescription());
      const title = pickTitle();
      return { ok: true, url: location.href, title, description };
    },
  };
})();
