(function () {
  "use strict";

  if (window.__listingDetailExtractor?.extract) return;

  function normalize(s) {
    return String(s || "").replace(/\s+/g, " ").trim();
  }

  function isBoilerplateText(value) {
    const text = normalize(value).toLowerCase();
    if (!text) return false;
    const markers = [
      "използваме бисквитки",
      "настройки на бисквитките",
      "отговорно използване на вашите данни",
      "we use cookies",
      "cookie preferences",
      "privacy preferences",
      "verify you are human",
      "human verification",
      "enable javascript and cookies",
      "checking your browser",
      "access denied",
    ];
    return markers.some((marker) => text.includes(marker));
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
    if (isBoilerplateText(txt)) return -Infinity;
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

  function extractDescription(preferredSelectors = []) {
    for (const selector of preferredSelectors || []) {
      if (typeof selector !== "string" || !selector) continue;
      let matches = [];
      try { matches = Array.from(document.querySelectorAll(selector)); } catch (_) { continue; }
      let preferred = "";
      for (const el of matches) {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        // Exact per-domain selectors are authoritative. Some publishers leave
        // their active tab at opacity:0 during animation even though its full
        // readable body is rendered; only structural hiding should reject it.
        if (style.display === "none" || style.visibility === "hidden" || !rect.width || !rect.height) continue;
        const text = getText(el, 20000);
        if (!isBoilerplateText(text) && text.length > preferred.length) preferred = text;
      }
      if (preferred.length >= 40) return preferred;
    }

    const ogd = document.querySelector('meta[property="og:description"]');
    const ogdText = normalize(ogd?.getAttribute("content"));
    if (ogdText && ogdText.length >= 120 && !isBoilerplateText(ogdText)) return ogdText;

    const itemprop = document.querySelector('[itemprop="description"]');
    const itempropText = getText(itemprop, 12000);
    if (itempropText && itempropText.length >= 120 && !isBoilerplateText(itempropText)) return itempropText;

    const byLabel = findByLabelOpisanie();
    if (byLabel && byLabel.length >= 120 && !isBoilerplateText(byLabel)) return byLabel;

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
    if (bestText && bestText.length >= 120 && !isBoilerplateText(bestText)) return bestText;

    const ps = Array.from(document.querySelectorAll("p,div,span")).slice(0, 800);
    let longest = "";
    for (const el of ps) {
      if (!isVisible(el)) continue;
      const t = getText(el, 12000);
      if (isBoilerplateText(t)) continue;
      if (t.length > longest.length) longest = t;
    }
    return longest;
  }

  function toAbsUrl(u) {
    try {
      if (!u) return null;
      const s = String(u).trim();
      if (!s || s.startsWith("data:")) return null;
      return new URL(s, location.href).toString();
    } catch (e) {
      return null;
    }
  }

  function pickLargestFromSrcset(srcset) {
    const s = normalize(srcset);
    if (!s) return null;
    const parts = s
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean)
      .map((p) => {
        const seg = p.split(/\s+/);
        const url = seg[0];
        const d = seg[seg.length - 1] || "";
        let w = 0;
        let x = 0;
        if (d.endsWith("w")) w = parseInt(d.slice(0, -1), 10) || 0;
        if (d.endsWith("x")) x = parseFloat(d.slice(0, -1)) || 0;
        return { url, score: w || Math.round((x || 0) * 1000) };
      })
      .sort((a, b) => b.score - a.score);
    return parts.length ? parts[0].url : null;
  }

  function extractImages(maxCount = 60, preferredSelectors = []) {
    const out = [];
    const seen = new Set();

    function add(u) {
      const abs = toAbsUrl(u);
      if (!abs) return;
      const low = abs.toLowerCase();
      if (low.includes("sprite") || low.includes("icon") || low.endsWith(".svg")) return;
      if (seen.has(abs)) return;
      seen.add(abs);
      out.push(abs);
    }

    function scanRoots(roots) {
      const dataAttrs = [
        "data-full", "data-large", "data-big", "data-zoom", "data-image",
        "data-img", "data-photo", "data-src", "data-original", "data-lazy",
        "data-bg", "data-background", "data-background-image",
      ];
      const descendants = (root, selector) => [
        ...(root.matches?.(selector) ? [root] : []),
        ...Array.from(root.querySelectorAll(selector)),
      ];

      for (const root of roots) {
        for (const img of descendants(root, "img")) {
          add(pickLargestFromSrcset(img.getAttribute("srcset")));
          for (const attr of dataAttrs) add(img.getAttribute(attr));
          add(img.currentSrc);
          add(img.getAttribute("src"));
          if (out.length >= maxCount) return;
        }
        for (const source of descendants(root, "picture source[srcset]")) {
          add(pickLargestFromSrcset(source.getAttribute("srcset")));
          if (out.length >= maxCount) return;
        }
        for (const attr of dataAttrs) {
          for (const el of descendants(root, `[${attr}]`)) {
            add(el.getAttribute(attr));
            if (out.length >= maxCount) return;
          }
        }
        for (const anchor of descendants(root, "a[href]")) {
          const href = anchor.getAttribute("href");
          if (href && /\.(jpe?g|png|webp|gif)(\?|#|$)/i.test(href)) add(href);
          if (out.length >= maxCount) return;
        }
      }
    }

    const preferredRoots = [];
    for (const selector of preferredSelectors || []) {
      if (typeof selector !== "string" || !selector) continue;
      try { preferredRoots.push(...Array.from(document.querySelectorAll(selector))); } catch (_) {}
    }
    if (preferredRoots.length) {
      scanRoots(Array.from(new Set(preferredRoots)));
      // A configured gallery is authoritative: never fall through to page-wide
      // logos, maps, broker portraits, or recommended-property thumbnails.
      return out.slice(0, maxCount);
    }

    // meta images (often the cover)
    const metaSelectors = [
      'meta[property="og:image"]',
      'meta[property="og:image:url"]',
      'meta[name="twitter:image"]',
      'meta[property="twitter:image"]',
    ];
    for (const sel of metaSelectors) {
      for (const m of Array.from(document.querySelectorAll(sel))) {
        add(m.getAttribute("content"));
        if (out.length >= maxCount) return out.slice(0, maxCount);
      }
    }

    // <img> tags
    for (const img of Array.from(document.querySelectorAll("img"))) {
      const best = pickLargestFromSrcset(img.getAttribute("srcset"));
      if (best) add(best);
      add(img.getAttribute("data-full"));
      add(img.getAttribute("data-large"));
      add(img.getAttribute("data-src"));
      add(img.getAttribute("data-original"));
      add(img.getAttribute("data-lazy"));
      add(img.getAttribute("src"));
      if (out.length >= maxCount) return out.slice(0, maxCount);
    }

    // picture/source srcset
    for (const s of Array.from(document.querySelectorAll("picture source[srcset]"))) {
      add(pickLargestFromSrcset(s.getAttribute("srcset")));
      if (out.length >= maxCount) return out.slice(0, maxCount);
    }

    // dataset / data-* attributes
    const dataAttrs = [
      "data-full",
      "data-large",
      "data-big",
      "data-zoom",
      "data-image",
      "data-img",
      "data-photo",
      "data-src",
      "data-original",
      "data-lazy",
      "data-bg",
      "data-background",
      "data-background-image",
    ];
    for (const a of dataAttrs) {
      for (const el of Array.from(document.querySelectorAll(`[${a}]`))) {
        add(el.getAttribute(a));
        if (out.length >= maxCount) return out.slice(0, maxCount);
      }
    }

    // anchor hrefs that point directly to image files
    const isImgHref = (h) => !!h && /\.(jpe?g|png|webp|gif)(\?|#|$)/i.test(h);
    for (const a of Array.from(document.querySelectorAll("a[href]"))) {
      const href = a.getAttribute("href");
      if (isImgHref(href)) add(href);
      if (out.length >= maxCount) return out.slice(0, maxCount);
    }

    // background-image URLs (inline + computed)
    const urlRe = /url\((['"]?)(.*?)\1\)/g;
    const candidates = Array.from(document.querySelectorAll("[style]"))
      .filter((el) => /background/i.test(el.getAttribute("style") || ""))
      .slice(0, 800);

    for (const el of candidates) {
      const styleAttr = el.getAttribute("style") || "";
      let m;
      while ((m = urlRe.exec(styleAttr)) !== null) {
        add(m[2]);
        if (out.length >= maxCount) return out.slice(0, maxCount);
      }

      try {
        const bg = window.getComputedStyle(el).backgroundImage || "";
        let m2;
        while ((m2 = urlRe.exec(bg)) !== null) {
          add(m2[2]);
          if (out.length >= maxCount) return out.slice(0, maxCount);
        }
      } catch (_) {}
    }

    return out.slice(0, maxCount);
  }


  window.__listingDetailExtractor = {
    version: 2,
    extract(options = {}) {
      // Preserve existing behavior for title/description/images
      const preferredSelectors = Array.isArray(options.descriptionSelectors)
        ? options.descriptionSelectors
        : [];
      const description = normalize(extractDescription(preferredSelectors));
      const title = pickTitle();
      const preferredImageSelectors = Array.isArray(options.imageSelectors)
        ? options.imageSelectors
        : [];
      const images = extractImages(60, preferredImageSelectors);
      const image = images.length ? images[0] : null;

      // ---- v2 raw harvester additions (generic, site-agnostic) ----
      const STATE_MARKERS = [
        "__NEXT_DATA__",
        "__NUXT__",
        "window.__INITIAL_STATE__",
        "INITIAL_STATE",
        "apolloState",
        "preloadedState",
        "reduxState",
      ];

      const PHONE_RE = /(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)/g;
      const EMAIL_RE = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig;

      function capText(s, maxLen) {
        const t = String(s || "");
        if (t.length <= maxLen) return t;
        return t.slice(0, maxLen) + `\n\n[TRUNCATED ${t.length - maxLen} chars]`;
      }

      function uniq(arr) {
        const out = [];
        const seen = new Set();
        for (const x of arr || []) {
          const v = normalize(x);
          if (!v) continue;
          if (seen.has(v)) continue;
          seen.add(v);
          out.push(v);
        }
        return out;
      }

      function extractJsonLd(maxBlocks = 20) {
        const out = [];
        for (const s of Array.from(document.querySelectorAll('script[type="application/ld+json"]'))) {
          const t = (s.textContent || "").trim();
          if (!t) continue;
          try {
            const parsed = JSON.parse(t);
            const blocks = Array.isArray(parsed) ? parsed : [parsed];
            for (const block of blocks) {
              if (block && typeof block === "object" && !Array.isArray(block)) {
                out.push(block);
                if (out.length >= maxBlocks) break;
              }
            }
          } catch (_) {}
          if (out.length >= maxBlocks) break;
        }
        return out;
      }

      function extractStateBlobs(maxBlocks = 8) {
        const blobs = [];
        const scripts = Array.from(document.querySelectorAll("script"));
        for (const s of scripts) {
          const id = normalize(s.id || "");
          const txt = s.textContent || "";
          const len = txt.length || 0;
          if (!txt || len < 2000) continue;

          let marker = null;
          if (id && STATE_MARKERS.includes(id)) marker = id;
          if (!marker) {
            for (const m of STATE_MARKERS) {
              if (txt.includes(m)) { marker = m; break; }
            }
          }
          const looksJson = txt.trim().startsWith("{") || txt.trim().startsWith("[");
          if (marker || (looksJson && len >= 50_000)) {
            blobs.push({
              marker: marker,
              id: id || null,
              length: len,
              snippet: capText(txt, 20_000),
            });
          }
          if (blobs.length >= maxBlocks) break;
        }
        return blobs;
      }

      function extractKvPairs(maxPairs = 250) {
        const pairs = [];

        // dl dt/dd
        for (const dl of Array.from(document.querySelectorAll("dl"))) {
          const dts = Array.from(dl.querySelectorAll("dt"));
          const dds = Array.from(dl.querySelectorAll("dd"));
          const n = Math.min(dts.length, dds.length);
          if (n >= 2) {
            for (let i = 0; i < n; i++) {
              const k = normalize(dts[i]?.textContent);
              const v = normalize(dds[i]?.textContent);
              if (k && v) pairs.push({ k, v, source: "dl" });
            }
          }
        }

        // tables
        for (const t of Array.from(document.querySelectorAll("table"))) {
          for (const r of Array.from(t.querySelectorAll("tr"))) {
            const th = r.querySelector("th");
            const tds = Array.from(r.querySelectorAll("td"));
            if (th && tds.length) {
              const k = normalize(th.textContent);
              const v = normalize(tds[0].textContent);
              if (k && v) pairs.push({ k, v, source: "table" });
            } else if (tds.length >= 2) {
              const k = normalize(tds[0].textContent);
              const v = normalize(tds[1].textContent);
              if (k && v) pairs.push({ k, v, source: "table" });
            }
          }
        }

        // label:value patterns (short)
        const candidates = Array.from(document.querySelectorAll("li, p, div, span"))
          .slice(0, 4000)
          .map((n) => normalize(n.textContent))
          .filter((t) => t && t.length <= 120 && t.includes(":"));
        for (const t of candidates) {
          const parts = t.split(":");
          if (parts.length !== 2) continue;
          const k = normalize(parts[0]);
          const v = normalize(parts[1]);
          if (k && v && k.length <= 40 && v.length <= 70) {
            pairs.push({ k, v, source: "label_value" });
          }
        }

        const seen = new Set();
        const out = [];
        for (const p of pairs) {
          const key = `${p.k}||${p.v}`;
          if (seen.has(key)) continue;
          seen.add(key);
          out.push(p);
          if (out.length >= maxPairs) break;
        }
        return out;
      }

      function extractTextBlocks(maxBlocks = 50) {
        const blocks = [];
        const headings = Array.from(document.querySelectorAll("h1,h2,h3")).slice(0, 30);
        for (const h of headings) {
          const section = normalize(h.textContent);
          if (!section) continue;
          let txt = "";
          let n = h.nextElementSibling;
          let steps = 0;
          while (n && steps < 6) {
            const t = getText(n, 8000);
            if (t) txt += (txt ? "\n" : "") + t;
            n = n.nextElementSibling;
            steps++;
          }
          txt = normalize(txt);
          if (txt) blocks.push({ section, text: capText(txt, 12_000) });
          if (blocks.length >= maxBlocks) break;
        }
        if (blocks.length === 0) {
          const bodyExcerpt = capText(getText(document.body, 30_000), 30_000);
          if (bodyExcerpt) blocks.push({ section: "body_excerpt", text: bodyExcerpt });
        }
        return blocks;
      }

      function extractContacts() {
        const bodyText = capText(getText(document.body, 250_000), 250_000);
        const phones = uniq(bodyText.match(PHONE_RE) || []).slice(0, 20);
        const emails = uniq(bodyText.match(EMAIL_RE) || []).slice(0, 20);
        return { phones, emails };
      }

      function extractMediaLinks() {
        const out = [];
        for (const a of Array.from(document.querySelectorAll("a[href]"))) {
          const href = a.getAttribute("href") || "";
          try { out.push(new URL(href, location.href).toString()); } catch (_) {}
        }
        const links = uniq(out);

        const video = links.filter((u) => /youtube\.com|youtu\.be|vimeo\.com/i.test(u)).slice(0, 10);
        const virtual_tour = links.filter((u) => /virtual|tour|360|matterport/i.test(u)).slice(0, 10);
        const floorplan = links
          .filter((u) => /floor|plan|схема|разпредел/i.test(u))
          .slice(0, 10);
        const map_links = links.filter((u) => /google\.com\/maps|maps\.google|openstreetmap|map/i.test(u)).slice(0, 10);

        return { video, virtual_tour, floorplan, map_links };
      }

      function computeSignals(payload) {
        const txt = payload.description || "";
        const hasJsonld = Array.isArray(payload.raw_jsonld) && payload.raw_jsonld.length > 0;
        const hasState = Array.isArray(payload.raw_state_blobs) && payload.raw_state_blobs.length > 0;
        const kvCount = Array.isArray(payload.raw_kv) ? payload.raw_kv.length : 0;
        const hasKv = kvCount > 0;
        const hasContacts = payload.raw_contacts && ((payload.raw_contacts.phones || []).length + (payload.raw_contacts.emails || []).length) > 0;

        let primary = "description_only";
        if (hasState) primary = "state_blob";
        else if (hasJsonld && hasKv) primary = "mixed";
        else if (hasJsonld) primary = "jsonld";
        else if (hasKv) primary = "kv";

        return {
          has_jsonld: hasJsonld,
          has_state_blob: hasState,
          has_kv_pairs: hasKv,
          kv_pair_count: kvCount,
          has_contacts: hasContacts,
          images_count: (payload.images || []).length,
          text_length: txt.length,
          primary_payload_source: primary,
        };
      }

      const raw_jsonld = extractJsonLd(20);
      const raw_state_blobs = extractStateBlobs(8);
      const raw_kv = extractKvPairs(250);
      const raw_text_blocks = extractTextBlocks(50);
      const raw_contacts = extractContacts();
      const raw_media = extractMediaLinks();

      const payload = {
        ok: true,
        url: location.href,
        title,
        description,
        image,
        images,
        raw_jsonld,
        raw_state_blobs,
        raw_kv,
        raw_text_blocks,
        raw_contacts,
        raw_media,
      };
      payload.signals = computeSignals(payload);
      return payload;
    },
  };

})();
