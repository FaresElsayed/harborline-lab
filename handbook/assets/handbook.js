/* Harborline handbook runtime. No dependencies. Works from file:// and from http://localhost:8080/handbook/.
 *
 * ============================== PAGE CONTRACT (how to write a handbook page) ==============================
 *
 *  <html lang="en" data-hb-page="brand">           page id; must match an id in PAGES below
 *  <head>
 *    <script>try{var t=localStorage.getItem("harborline.theme");if(t==="light"||t==="dark")
 *      document.documentElement.setAttribute("data-theme",t)}catch(e){}</script>   (avoids a theme flash)
 *    <link rel="stylesheet" href="../brand/fonts.css">       (web fonts; offline the token stacks fall back)
 *    <link rel="stylesheet" href="../brand/tokens.css">
 *    <link rel="stylesheet" href="assets/handbook.css">
 *    <link rel="icon" href="../brand/logo/favicon.svg" type="image/svg+xml">
 *    <script src="assets/handbook.js" defer></script>
 *  </head>
 *  <body>
 *    <div class="hb-shell">
 *      <aside class="hb-side" data-hb-side></aside>          sidebar: brand, page list, progress tools, theme
 *      <main class="hb-sheet" id="main"><div class="hb-page">
 *        <article class="hb-content"> ...h1, .hb-lead, sections... </article>
 *        <aside class="hb-toc" data-hb-toc></aside>         sticky table of contents (built from h2[id])
 *      </div></main>
 *    </div>
 *  </body>
 *
 * ATTRIBUTES
 *  data-track="<page>:<item>"   on <input type="checkbox">. Persisted. Use "<section>.<slug>" for <item>.
 *                               Section = closest [data-section] ancestor, else the part of <item> before ".".
 *  data-section="<name>"        on <section>; groups tracked items. Its first h2[id] feeds the TOC.
 *                               A slim section progress bar is inserted under that h2 when the section has
 *                               2+ items (add data-no-progress to the section to skip it).
 *  data-progress="<page>[:<section>]"  renders a progress bar. Options: data-variant="slim",
 *                               data-total="<n>" (fallback total for a page that has not been opened yet).
 *  data-hb-summary              renders "pending vs done" for the current page, grouped by section.
 *  data-copy="#element-id"      on <button>: copies that element's text. Every <pre> inside .hb-content also
 *                               gets a Copy button automatically (opt out with <pre data-nocopy>).
 *  <details data-spoiler="<id>" data-spoiler-label="Project 3">  asks "I've built this already" before opening;
 *                               the unlock is remembered (and reset by Reset).
 *  data-action="export|import|reset"   on any <button>: progress tools (the sidebar already has them).
 *  data-hb-app="/tracker"       on <a>: points at the running app (same origin when served by it, else
 *                               http://localhost:8080). Write the localhost URL in href as the no-JS fallback.
 *
 * MARKUP HELPERS (handbook.css)
 *  .task > label.task-head(input[data-track] + span.task-title + span.task-time) + div.task-body
 *  ul.checklist > li > label(input[data-track] + span)        .verify (.verify-title)   details.errors > summary + div
 *  .callout(.callout--note|--warn|--danger|--good) (.callout-title)   table.hb-table   .hb-figure   .hb-panel
 *  .hb-chip(--accent|--warn|--good)   .hb-btn(--primary|--quiet|--lg)   .hb-grid-2/.hb-grid-3   pre[data-shell="PowerShell"]
 *  SVG diagrams: .dg-box(--tint|--accent|--warn) .dg-title .dg-text .dg-small .dg-mono .dg-key .dg-line(--accent|--dash) .dg-arrow
 *
 * JS API  window.Harborline.handbook
 *  .refresh()            re-scan the DOM after you add tracked checkboxes dynamically
 *  .state()              deep copy of the stored progress
 *  .toast(text)          small confirmation message
 *  .confirm({title, body, confirm, cancel}) -> Promise<boolean>
 *  document event "hb:change"  detail {page, item, done}
 *
 * STORAGE
 *  localStorage["harborline.handbook.v1"] = {version: 1, pages: {<page>: {items: {<item>: ISO}, total, sections,
 *      spoilers: {<id>: ISO}, data: {links, site}, updated_at}}}   data is written by the setup files; kept here
 *  localStorage["harborline.theme"] = "light" | "dark" (absent = follow the system)
 *  Served over http(s): GET/PUT /api/handbook/progress/<page>, body {state: <page state>}; newest updated_at wins.
 * ==========================================================================================================
 */
(function () {
  "use strict";

  var STORE_KEY = "harborline.handbook.v1";
  var THEME_KEY = "harborline.theme";
  var APP_DEFAULT = "http://localhost:8080";

  // Every page in the handbook. total = fallback item count until the page has been opened once.
  var PAGES = [
    { id: "start", title: "Start here", file: "START_HERE.html", root: true, total: 4 },
    { id: "getting-started", title: "Getting started", file: "getting-started.html", total: 18 },
    { id: "known-issues", title: "Known issues", file: "known-issues.html", total: 0 },
    { id: "project-brief", title: "Project brief", file: "project-brief.html", total: 62 },
    { id: "team", title: "Your team", file: "team.html", total: 26 },
    { id: "source-systems", title: "Source systems", file: "source-systems.html", total: 10 },
    { id: "brand", title: "Brand kit", file: "brand.html", total: 19 },
    { id: "dashboards", title: "Dashboard specs", file: "dashboards.html", total: 105 },
    { id: "answer-key", title: "Answer key", file: "answer-key.html", total: 0 }
  ];

  var doc = document;
  var root = doc.documentElement;
  var pageId = root.getAttribute("data-hb-page") || "unknown";
  var scriptEl = doc.currentScript || doc.querySelector('script[src*="handbook.js"]');
  var assetsURL = new URL(".", scriptEl ? scriptEl.src : location.href);
  var handbookURL = new URL("../", assetsURL);
  var rootURL = new URL("../", handbookURL);
  var isHttp = /^https?:$/.test(location.protocol);
  var appOrigin = isHttp ? location.origin : APP_DEFAULT;

  // ---------------------------------------------------------------- storage
  var store = load();
  var sync = { state: isHttp ? "checking" : "local", timer: null };

  function load() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      if (parsed && parsed.version === 1 && parsed.pages) return parsed;
    } catch (e) { /* private mode or corrupt value: start fresh */ }
    return { version: 1, pages: {} };
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(store)); } catch (e) { /* storage unavailable */ }
  }
  function pageState(id) {
    var p = store.pages[id];
    if (!p) { p = store.pages[id] = { items: {}, total: 0, sections: {}, spoilers: {}, data: {}, updated_at: null }; }
    p.items = p.items || {}; p.sections = p.sections || {}; p.spoilers = p.spoilers || {}; p.data = p.data || {};
    return p;
  }
  function touch(id) { pageState(id).updated_at = new Date().toISOString(); save(); scheduleSync(id); }

  // ---------------------------------------------------------------- server sync
  function api(id) { return "/api/handbook/progress/" + encodeURIComponent(id); }
  function fetchJSON(url, opts, ms) {
    var ctl = "AbortController" in window ? new AbortController() : null;
    var t = ctl ? setTimeout(function () { ctl.abort(); }, ms || 3000) : null;
    opts = opts || {};
    if (ctl) opts.signal = ctl.signal;
    return fetch(url, opts).then(function (r) {
      if (t) clearTimeout(t);
      if (!r.ok) { var err = new Error("HTTP " + r.status); err.status = r.status; throw err; }
      return r.status === 204 ? null : r.json();
    });
  }
  function newer(a, b) { return (Date.parse(a || 0) || 0) > (Date.parse(b || 0) || 0); }
  function pull(id) {
    return fetchJSON(api(id), { headers: { Accept: "application/json" } }).then(function (res) {
      var remote = res && res.state;
      var local = pageState(id);
      var remoteAt = (remote && remote.updated_at) || (res && res.updated_at);
      if (remote && remote.items && newer(remoteAt, local.updated_at)) {
        store.pages[id] = { items: remote.items || {}, total: remote.total || local.total, sections: remote.sections || local.sections,
          spoilers: remote.spoilers || {}, data: remote.data || local.data || {}, updated_at: remoteAt };
        save();
        return "pulled";
      }
      if (local.updated_at && (!remote || newer(local.updated_at, remoteAt))) return push(id);
      return "same";
    });
  }
  function push(id) {
    return fetchJSON(api(id), {
      method: "PUT", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ state: pageState(id) })
    }).then(function () { return "pushed"; });
  }
  function scheduleSync(id) {
    if (sync.state !== "synced") return;
    clearTimeout(sync.timer);
    sync.timer = setTimeout(function () {
      push(id).catch(function () { setSync("error"); });
    }, 500);
  }
  function setSync(s) {
    sync.state = s;
    var el = doc.querySelector("[data-hb-sync]");
    if (!el) return;
    el.setAttribute("data-state", s);
    el.textContent = s === "synced" ? "Saved in this browser and the app"
      : s === "checking" ? "Checking the app…"
      : s === "error" ? "Saved in this browser (app sync unavailable)"
      : "Saved in this browser";
  }
  function startSync() {
    if (!isHttp) { setSync("local"); return; }
    var ids = [pageId];
    doc.querySelectorAll("[data-progress]").forEach(function (el) {
      var id = el.getAttribute("data-progress").split(":")[0];
      if (ids.indexOf(id) < 0) ids.push(id);
    });
    PAGES.forEach(function (p) { if (ids.indexOf(p.id) < 0) ids.push(p.id); });
    pull(pageId).then(function () {
      setSync("synced");
      applyStateToDom();
      renderAll();
      return Promise.all(ids.slice(1).map(function (id) { return pull(id).catch(function () { return null; }); }));
    }).then(function () { renderAll(); }).catch(function () { setSync("error"); });
  }

  // ---------------------------------------------------------------- tracked items
  var items = [];          // [{el, page, id, section, label, container}]
  function sectionOf(el, id) {
    var s = el.closest("[data-section]");
    if (s) return s.getAttribute("data-section");
    var dot = id.indexOf(".");
    return dot > 0 ? id.slice(0, dot) : "_";
  }
  function labelOf(el) {
    var task = el.closest(".task");
    if (task) { var t = task.querySelector(".task-title"); if (t) return t.textContent.trim(); }
    var lab = el.closest("label");
    return (el.getAttribute("data-label") || (lab ? lab.textContent : "") || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
  }
  function anchorFor(el, id) {
    var c = el.closest(".task") || el.closest("li") || el.closest("label") || el;
    if (!c.id) c.id = "t-" + id.replace(/[^a-z0-9]+/gi, "-").toLowerCase();
    return c;
  }
  function scan() {
    items = [];
    doc.querySelectorAll('input[type="checkbox"][data-track]').forEach(function (el) {
      var key = el.getAttribute("data-track");
      var colon = key.indexOf(":");
      if (colon < 1) return;
      var page = key.slice(0, colon), id = key.slice(colon + 1);
      var it = { el: el, page: page, id: id, section: sectionOf(el, id), label: labelOf(el), container: anchorFor(el, id) };
      items.push(it);
      if (!el.dataset.hbBound) {
        el.dataset.hbBound = "1";
        el.addEventListener("change", function () { onToggle(it, el.checked); });
      }
    });
    var byPage = {};
    items.forEach(function (it) {
      var p = byPage[it.page] = byPage[it.page] || { total: 0, sections: {} };
      p.total++; p.sections[it.section] = (p.sections[it.section] || 0) + 1;
    });
    Object.keys(byPage).forEach(function (id) {
      var ps = pageState(id);
      ps.total = byPage[id].total; ps.sections = byPage[id].sections;
    });
    save();
  }
  function applyStateToDom() {
    items.forEach(function (it) {
      var done = !!pageState(it.page).items[it.id];
      it.el.checked = done;
      markDone(it, done);
    });
  }
  function markDone(it, done) {
    var task = it.el.closest(".task");
    if (task) { if (done) task.setAttribute("data-done", ""); else task.removeAttribute("data-done"); }
  }
  function onToggle(it, done) {
    var ps = pageState(it.page);
    if (done) ps.items[it.id] = new Date().toISOString(); else delete ps.items[it.id];
    // keep twin checkboxes (same key twice on a page) in step
    items.forEach(function (o) { if (o !== it && o.page === it.page && o.id === it.id) o.el.checked = done; });
    markDone(it, done);
    touch(it.page);
    renderAll();
    doc.dispatchEvent(new CustomEvent("hb:change", { detail: { page: it.page, item: it.id, done: done } }));
  }

  // ---------------------------------------------------------------- counting
  function counts(page, section) {
    var onPage = items.filter(function (it) { return it.page === page && (!section || it.section === section); });
    if (onPage.length) {
      var seen = {}, total = 0, done = 0;
      onPage.forEach(function (it) {
        if (seen[it.id]) return; seen[it.id] = 1; total++;
        if (pageState(page).items[it.id]) done++;
      });
      return { done: done, total: total };
    }
    var ps = store.pages[page];
    var reg = PAGES.filter(function (p) { return p.id === page; })[0];
    var tot = section ? (ps && ps.sections && ps.sections[section]) || 0 : (ps && ps.total) || (reg ? reg.total : 0);
    var d = 0;
    if (ps && ps.items) Object.keys(ps.items).forEach(function (k) {
      if (!section || k.indexOf(section + ".") === 0) d++;
    });
    return { done: Math.min(d, tot || d), total: tot };
  }
  function overall() {
    var d = 0, t = 0;
    PAGES.forEach(function (p) { var c = counts(p.id); d += c.done; t += c.total; });
    return { done: d, total: t };
  }

  // ---------------------------------------------------------------- rendering helpers
  function h(tag, attrs, kids) {
    var el = doc.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") el.textContent = attrs[k];
      else if (k === "html") el.innerHTML = attrs[k];
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) el.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) el.appendChild(typeof c === "string" ? doc.createTextNode(c) : c); });
    return el;
  }
  function pageURL(p) { return new URL(p.root ? p.file : p.file, p.root ? rootURL : handbookURL).href; }
  function progressBar(el, c, variant) {
    var pct = c.total ? Math.round((c.done / c.total) * 100) : 0;
    el.classList.add("hb-progress");
    if (variant === "slim") el.classList.add("hb-progress--slim");
    if (c.total && c.done >= c.total) el.setAttribute("data-complete", ""); else el.removeAttribute("data-complete");
    el.innerHTML = "";
    var track = h("div", { class: "hb-progress-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(c.total),
      "aria-valuenow": String(c.done), "aria-label": el.getAttribute("data-progress-label") || "Progress" });
    var fill = h("span", { class: "hb-progress-fill" });
    fill.style.setProperty("--p", pct + "%");
    track.appendChild(fill);
    var label = h("span", { class: "hb-progress-label" });
    if (!c.total) label.textContent = "Not opened yet";
    else if (c.done >= c.total) label.innerHTML = "<b>All " + c.total + " done</b>";
    else label.innerHTML = "<b>" + c.done + "</b> of " + c.total + " done";
    el.appendChild(track); el.appendChild(label);
  }

  function renderProgress() {
    doc.querySelectorAll("[data-progress]").forEach(function (el) {
      var spec = el.getAttribute("data-progress").split(":");
      var c = counts(spec[0], spec[1]);
      if (!c.total && el.hasAttribute("data-total")) c.total = parseInt(el.getAttribute("data-total"), 10) || 0;
      progressBar(el, c, el.getAttribute("data-variant"));
    });
    doc.querySelectorAll("[data-hb-overall]").forEach(function (el) {
      progressBar(el, overall(), el.getAttribute("data-variant"));
    });
  }

  function renderSummary() {
    doc.querySelectorAll("[data-hb-summary]").forEach(function (box) {
      var page = box.getAttribute("data-hb-summary") || pageId;
      var list = items.filter(function (it) { return it.page === page; });
      var seen = {};
      list = list.filter(function (it) { if (seen[it.id]) return false; seen[it.id] = 1; return true; });
      var ps = pageState(page);
      var pending = list.filter(function (it) { return !ps.items[it.id]; });
      var done = list.filter(function (it) { return ps.items[it.id]; });
      box.className = "hb-summary";
      box.innerHTML = "";
      function col(title, arr, isDone) {
        var wrap = h("div");
        wrap.appendChild(h("h3", { text: title + " (" + arr.length + ")" }));
        if (!arr.length) { wrap.appendChild(h("p", { class: "hb-summary-empty", text: isDone ? "Nothing ticked yet." : "Nothing left on this page." })); return wrap; }
        var ul = h("ul");
        arr.forEach(function (it) {
          var when = isDone ? new Date(ps.items[it.id]).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : sectionTitle(it.section);
          ul.appendChild(h("li", null, [h("a", { href: "#" + it.container.id, text: it.label || it.id }), h("span", { text: when })]));
        });
        wrap.appendChild(ul);
        return wrap;
      }
      box.appendChild(col("Still to do", pending, false));
      box.appendChild(col("Done", done, true));
    });
  }
  function sectionTitle(name) {
    var s = doc.querySelector('[data-section="' + name + '"]');
    var hd = s && s.querySelector("h2, h3");
    return hd ? (hd.getAttribute("data-toc-label") || hd.textContent).trim() : "";
  }

  // ---------------------------------------------------------------- sidebar, top bar
  // Brand mark from brand/logo (light and dark variants; CSS shows the one matching the theme).
  function markHTML() {
    var base = new URL("brand/logo/", rootURL).href;
    return '<img class="hb-brand-mark hb-brand-mark--light" alt="" src="' + base + 'harborline-mark-light.svg">' +
      '<img class="hb-brand-mark hb-brand-mark--dark" alt="" src="' + base + 'harborline-mark-dark.svg">';
  }

  function renderSide() {
    var side = doc.querySelector("[data-hb-side]");
    if (!side) return;
    side.innerHTML = "";
    side.setAttribute("aria-label", "Handbook navigation");
    var start = PAGES[0];
    var brand = h("a", { class: "hb-brand", href: pageURL(start), html: markHTML() + '<span class="hb-brand-name">Harborline<span class="hb-brand-sub">Data engineering handbook</span></span>' });
    side.appendChild(brand);

    var ov = h("div", { class: "hb-side-overall" }, [h("p", { text: "Your progress, all pages" }), h("div", { "data-hb-overall": "", "data-variant": "slim", "data-progress-label": "Progress across the handbook" })]);
    side.appendChild(ov);

    var nav = h("nav", { class: "hb-nav", "aria-label": "Handbook pages" });
    nav.appendChild(h("p", { class: "hb-nav-title", text: "Handbook" }));
    var ul = h("ul");
    PAGES.forEach(function (p) {
      var a = h("a", { href: pageURL(p) }, [h("span", { text: p.title }), h("span", { class: "hb-nav-count", "data-hb-navcount": p.id })]);
      if (p.id === pageId) a.setAttribute("aria-current", "page");
      ul.appendChild(h("li", null, [a]));
    });
    nav.appendChild(ul);
    side.appendChild(nav);

    var app = h("nav", { class: "hb-nav", "aria-label": "Simulator app" });
    app.appendChild(h("p", { class: "hb-nav-title", text: "Simulator app" }));
    var ul2 = h("ul");
    [["Control Center", "/"], ["Team", "/team"], ["Tracker", "/tracker"]].forEach(function (x) {
      ul2.appendChild(h("li", null, [h("a", { href: appOrigin + x[1], class: "hb-nav-ext", target: "_blank", rel: "noopener" }, [h("span", { text: x[0] })])]));
    });
    app.appendChild(ul2);
    side.appendChild(app);

    var tools = h("div", { class: "hb-side-tools hb-noprint" });
    tools.appendChild(h("p", { class: "hb-sync", "data-hb-sync": "", "aria-live": "polite" }));
    var row = h("div", { class: "hb-btn-row" }, [
      h("button", { type: "button", class: "hb-btn", "data-action": "export", text: "Export" }),
      h("button", { type: "button", class: "hb-btn", "data-action": "import", text: "Import" }),
      h("button", { type: "button", class: "hb-btn hb-btn--quiet hb-btn--danger", "data-action": "reset", text: "Reset" })
    ]);
    tools.appendChild(row);
    tools.appendChild(themeSwitch());
    side.appendChild(tools);
    setSync(sync.state);
  }

  function renderNavCounts() {
    doc.querySelectorAll("[data-hb-navcount]").forEach(function (el) {
      var c = counts(el.getAttribute("data-hb-navcount"));
      if (!c.total) { el.textContent = ""; el.removeAttribute("data-complete"); return; }
      el.textContent = c.done >= c.total ? "Done" : c.done + "/" + c.total;
      if (c.done >= c.total) el.setAttribute("data-complete", ""); else el.removeAttribute("data-complete");
    });
  }

  function renderTopbar() {
    var side = doc.querySelector("[data-hb-side]");
    var shell = doc.querySelector(".hb-shell");
    if (!side || !shell || doc.querySelector(".hb-topbar")) return;
    var title = (doc.querySelector(".hb-content h1") || {}).textContent || "Handbook";
    var btn = h("button", { type: "button", class: "hb-btn", "aria-expanded": "false", "aria-controls": "hb-side", text: "Menu" });
    side.id = side.id || "hb-side";
    var bar = h("div", { class: "hb-topbar hb-noprint" }, [btn, h("span", { class: "hb-topbar-title", text: title.trim() })]);
    shell.parentNode.insertBefore(bar, shell);
    var scrim = null;
    function close() {
      side.removeAttribute("data-open"); btn.setAttribute("aria-expanded", "false");
      if (scrim) { scrim.remove(); scrim = null; }
    }
    btn.addEventListener("click", function () {
      if (side.hasAttribute("data-open")) { close(); return; }
      side.setAttribute("data-open", ""); btn.setAttribute("aria-expanded", "true");
      scrim = h("div", { class: "hb-scrim" }); scrim.addEventListener("click", close); doc.body.appendChild(scrim);
      var first = side.querySelector("a, button"); if (first) first.focus();
    });
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape" && side.hasAttribute("data-open")) { close(); btn.focus(); } });
  }

  // ---------------------------------------------------------------- theme
  function currentTheme() { try { return localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { return "auto"; } }
  function setTheme(t) {
    try { if (t === "auto") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, t); } catch (e) { /* ignore */ }
    if (t === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", t);
  }
  function themeSwitch() {
    var wrap = h("div", { class: "hb-theme", role: "radiogroup", "aria-label": "Colour theme" });
    var cur = currentTheme();
    [["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]].forEach(function (o) {
      var input = h("input", { type: "radio", name: "hb-theme", value: o[0], "aria-label": o[1] + " theme" });
      if (o[0] === cur) input.checked = true;
      input.addEventListener("change", function () { setTheme(o[0]); });
      wrap.appendChild(h("label", null, [input, h("span", { text: o[1] })]));
    });
    return wrap;
  }

  // ---------------------------------------------------------------- TOC + section progress
  var tocLinks = [];
  function renderToc() {
    var box = doc.querySelector("[data-hb-toc]");
    var heads = Array.prototype.slice.call(doc.querySelectorAll(".hb-content h2[id]:not([data-toc-skip])"));
    if (!box) return;
    box.innerHTML = "";
    if (!heads.length) { box.remove(); return; }
    box.setAttribute("aria-label", "On this page");
    if (items.some(function (it) { return it.page === pageId; })) {
      box.appendChild(h("p", { class: "hb-toc-title", text: "This page" }));
      box.appendChild(h("div", { "data-progress": pageId, "data-variant": "slim", "data-progress-label": "Progress on this page" }));
    }
    box.appendChild(h("p", { class: "hb-toc-title", text: "On this page" }));
    var ol = h("ol");
    tocLinks = [];
    heads.forEach(function (hd) {
      var sec = hd.closest("[data-section]");
      var name = sec ? sec.getAttribute("data-section") : null;
      var count = h("span", { class: "hb-toc-count" });
      if (name) count.setAttribute("data-hb-toccount", name);
      var a = h("a", { href: "#" + hd.id }, [h("span", { text: (hd.getAttribute("data-toc-label") || hd.textContent).trim() }), count]);
      tocLinks.push({ a: a, hd: hd });
      ol.appendChild(h("li", null, [a]));
    });
    box.appendChild(ol);
    var actions = h("div", { class: "hb-toc-actions" }, [
      h("a", { href: "#top", class: "hb-btn hb-btn--quiet", text: "Back to top" }),
      h("button", { type: "button", class: "hb-btn hb-btn--quiet", text: "Print this page", "data-hb-print": "" })
    ]);
    box.appendChild(actions);
    actions.querySelector("[data-hb-print]").addEventListener("click", function () { window.print(); });
    spy();
  }
  function renderTocCounts() {
    doc.querySelectorAll("[data-hb-toccount]").forEach(function (el) {
      var c = counts(pageId, el.getAttribute("data-hb-toccount"));
      if (!c.total) { el.textContent = ""; return; }
      el.textContent = c.done >= c.total ? "Done" : c.done + "/" + c.total;
      if (c.done >= c.total) el.setAttribute("data-complete", ""); else el.removeAttribute("data-complete");
    });
  }
  function insertSectionBars() {
    doc.querySelectorAll(".hb-content section[data-section]:not([data-no-progress])").forEach(function (sec) {
      var name = sec.getAttribute("data-section");
      if (sec.querySelector('[data-progress="' + pageId + ":" + name + '"]')) return;
      var n = sec.querySelectorAll('input[data-track^="' + pageId + ':"]').length;
      var hd = sec.querySelector("h2");
      if (n < 2 || !hd) return;
      var bar = h("div", { "data-progress": pageId + ":" + name, "data-variant": "slim", "data-progress-label": "Progress in this section", class: "hb-section-progress" });
      bar.style.margin = "0 0 18px"; bar.style.maxWidth = "22rem";
      hd.insertAdjacentElement("afterend", bar);
    });
  }
  function spy() {
    if (!tocLinks.length) return;
    window.addEventListener("scroll", throttle(updateActive, 100), { passive: true });
    updateActive();
  }
  function updateActive() {
    var active = null;
    for (var i = 0; i < tocLinks.length; i++) {
      if (tocLinks[i].hd.getBoundingClientRect().top < window.innerHeight * 0.3) active = tocLinks[i]; else break;
    }
    tocLinks.forEach(function (l) { if (l === active) l.a.setAttribute("data-active", ""); else l.a.removeAttribute("data-active"); });
  }
  function throttle(fn, ms) {
    var last = 0, t = null;
    return function () {
      var now = Date.now();
      if (now - last >= ms) { last = now; fn(); }
      else { clearTimeout(t); t = setTimeout(function () { last = Date.now(); fn(); }, ms); }
    };
  }

  // ---------------------------------------------------------------- copy
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = h("textarea", { style: "position:fixed;top:-1000px;opacity:0" });
      ta.value = text; doc.body.appendChild(ta); ta.select();
      try { doc.execCommand("copy") ? resolve() : reject(new Error("copy failed")); } catch (e) { reject(e); }
      ta.remove();
    });
  }
  function flashCopied(btn) {
    var prev = btn.getAttribute("data-label") || btn.textContent;
    btn.setAttribute("data-label", prev);
    btn.textContent = "Copied"; btn.setAttribute("data-copied", "");
    setTimeout(function () { btn.textContent = prev; btn.removeAttribute("data-copied"); }, 1600);
  }
  function enhanceCode() {
    doc.querySelectorAll(".hb-content pre:not([data-nocopy])").forEach(function (pre) {
      if (pre.parentNode.classList && pre.parentNode.classList.contains("hb-code")) return;
      var wrap = h("div", { class: "hb-code" });
      pre.parentNode.insertBefore(wrap, pre); wrap.appendChild(pre);
      var btn = h("button", { type: "button", class: "hb-copy", text: "Copy", "aria-label": "Copy " + (pre.getAttribute("data-shell") || "code") });
      btn.addEventListener("click", function () {
        copyText(pre.innerText.replace(/\n$/, "")).then(function () { flashCopied(btn); }, function () { toast("Copy failed: select the text and press Ctrl+C"); });
      });
      wrap.appendChild(btn);
    });
    doc.querySelectorAll("button[data-copy]").forEach(function (btn) {
      if (btn.dataset.hbBound) return; btn.dataset.hbBound = "1";
      btn.addEventListener("click", function () {
        var target = doc.querySelector(btn.getAttribute("data-copy"));
        if (!target) return;
        var text = "value" in target && target.tagName !== "BUTTON" ? target.value : target.innerText;
        copyText(text).then(function () { flashCopied(btn); toast("Copied to the clipboard"); }, function () { toast("Copy failed: select the text and press Ctrl+C"); });
      });
    });
  }

  // ---------------------------------------------------------------- dialog + toast
  function confirmDialog(o) {
    return new Promise(function (resolve) {
      var dlg = h("dialog", { class: "hb-dialog", "aria-labelledby": "hb-dlg-title" });
      var buttons = (o.buttons || [{ label: o.cancel || "Cancel", value: false }, { label: o.confirm || "OK", value: true, primary: true, danger: o.danger }]);
      var actions = h("div", { class: "hb-dialog-actions" });
      buttons.forEach(function (b) {
        var btn = h("button", { type: "button", class: "hb-btn" + (b.primary ? " hb-btn--primary" : "") + (b.danger && !b.primary ? " hb-btn--danger" : ""), text: b.label });
        btn.addEventListener("click", function () { finish(b.value); });
        actions.appendChild(btn);
      });
      dlg.appendChild(h("h2", { id: "hb-dlg-title", text: o.title }));
      dlg.appendChild(h("p", { text: o.body }));
      dlg.appendChild(actions);
      doc.body.appendChild(dlg);
      var done = false;
      function finish(v) { if (done) return; done = true; try { dlg.close(); } catch (e) { /* closed */ } dlg.remove(); resolve(v); }
      dlg.addEventListener("cancel", function (e) { e.preventDefault(); finish(o.cancelValue === undefined ? false : o.cancelValue); });
      if (typeof dlg.showModal === "function") { dlg.showModal(); actions.firstChild.focus(); }
      else { finish(window.confirm(o.title + "\n\n" + o.body)); }
    });
  }
  var toastEl = null, toastTimer = null;
  function toast(text) {
    if (!toastEl) { toastEl = h("div", { class: "hb-toast", role: "status", "aria-live": "polite" }); doc.body.appendChild(toastEl); }
    toastEl.textContent = text;
    toastEl.setAttribute("data-show", "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.removeAttribute("data-show"); }, 2200);
  }

  // ---------------------------------------------------------------- export / import / reset
  function exportProgress() {
    var data = { app: "harborline-handbook", version: 1, exported_at: new Date().toISOString(), pages: store.pages };
    var blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    var a = h("a", { href: URL.createObjectURL(blob), download: "harborline-handbook-progress-" + new Date().toISOString().slice(0, 10) + ".json" });
    doc.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    toast("Progress exported");
  }
  function importProgress() {
    var input = h("input", { type: "file", accept: "application/json,.json", style: "display:none" });
    doc.body.appendChild(input);
    input.addEventListener("change", function () {
      var f = input.files && input.files[0];
      input.remove();
      if (!f) return;
      var r = new FileReader();
      r.onload = function () {
        var data;
        try { data = JSON.parse(r.result); } catch (e) { toast("That file is not valid JSON"); return; }
        if (!data || data.app !== "harborline-handbook" || !data.pages) { toast("That is not a handbook progress file"); return; }
        var ids = Object.keys(data.pages);
        confirmDialog({ title: "Import progress?", body: "This replaces your progress on " + ids.length + " page" + (ids.length === 1 ? "" : "s") +
          " with the file exported " + (data.exported_at ? new Date(data.exported_at).toLocaleString() : "earlier") + ".", confirm: "Import", cancel: "Cancel" })
          .then(function (ok) {
            if (!ok) return;
            var now = new Date().toISOString();
            ids.forEach(function (id) {
              var p = data.pages[id] || {};
              store.pages[id] = { items: p.items || {}, total: p.total || 0, sections: p.sections || {}, spoilers: p.spoilers || {}, data: p.data || {}, updated_at: now };
            });
            save();
            scan(); applyStateToDom(); renderAll();
            if (sync.state === "synced") ids.forEach(function (id) { push(id).catch(function () { setSync("error"); }); });
            toast("Progress imported");
          });
      };
      r.readAsText(f);
    });
    input.click();
  }
  function resetProgress() {
    confirmDialog({
      title: "Reset progress?", body: "Clear the ticks (and spoiler unlocks) on this page only, or on every handbook page. Export first if you might want them back.",
      buttons: [{ label: "Cancel", value: null }, { label: "Everything", value: "all", danger: true }, { label: "This page", value: "page", primary: true }],
      cancelValue: null
    }).then(function (scope) {
      if (!scope) return;
      var now = new Date().toISOString();
      var ids = scope === "all" ? PAGES.map(function (p) { return p.id; }).concat(Object.keys(store.pages)) : [pageId];
      ids.forEach(function (id) {
        var old = store.pages[id] || {};
        store.pages[id] = { items: {}, total: old.total || 0, sections: old.sections || {}, spoilers: {}, data: old.data || {}, updated_at: now };
      });
      save(); applyStateToDom(); relockSpoilers(); renderAll();
      if (sync.state === "synced") ids.forEach(function (id) { if (store.pages[id]) push(id).catch(function () { setSync("error"); }); });
      toast(scope === "all" ? "All progress cleared" : "This page was reset");
    });
  }
  function bindActions() {
    doc.addEventListener("click", function (e) {
      var btn = e.target.closest && e.target.closest("[data-action]");
      if (!btn) return;
      var a = btn.getAttribute("data-action");
      if (a === "export") exportProgress();
      else if (a === "import") importProgress();
      else if (a === "reset") resetProgress();
    });
  }

  // ---------------------------------------------------------------- spoilers
  function spoilerId(d) { return d.getAttribute("data-spoiler") || d.id || "spoiler"; }
  function relockSpoilers() {
    doc.querySelectorAll("details[data-spoiler]").forEach(function (d) { if (!pageState(pageId).spoilers[spoilerId(d)]) d.open = false; });
  }
  function bindSpoilers() {
    doc.querySelectorAll("details[data-spoiler]").forEach(function (d) {
      var sum = d.querySelector("summary");
      if (!sum || d.dataset.hbBound) return;
      d.dataset.hbBound = "1";
      d.open = false;
      sum.addEventListener("click", function (e) {
        if (d.open) return;
        var id = spoilerId(d);
        if (pageState(pageId).spoilers[id]) return;
        e.preventDefault();
        var label = d.getAttribute("data-spoiler-label") || "this project";
        confirmDialog({
          title: "Spoilers for " + label,
          body: "This section explains what is planted in the data for " + label + ". Open it after you have shipped that project's dashboards: finding it yourself is the exercise.",
          buttons: [{ label: "Not yet", value: false }, { label: "I've built this already", value: true, primary: true }]
        }).then(function (ok) {
          if (!ok) return;
          pageState(pageId).spoilers[id] = new Date().toISOString();
          touch(pageId);
          d.open = true;
          sum.focus();
        });
      });
    });
  }

  // ---------------------------------------------------------------- app links
  function bindAppLinks() {
    doc.querySelectorAll("a[data-hb-app]").forEach(function (a) { a.href = appOrigin + a.getAttribute("data-hb-app"); });
    doc.querySelectorAll("[data-hb-app-origin]").forEach(function (el) { el.textContent = appOrigin; });
  }

  // ---------------------------------------------------------------- boot
  function renderAll() {
    renderProgress(); renderNavCounts(); renderTocCounts(); renderSummary();
  }
  function boot() {
    if (!doc.querySelector(".hb-skip")) {
      var skip = h("a", { class: "hb-skip", href: "#main", text: "Skip to content" });
      doc.body.insertBefore(skip, doc.body.firstChild);
    }
    if (!doc.getElementById("top")) {
      var h1 = doc.querySelector(".hb-content h1"); if (h1 && !h1.id) h1.id = "top";
    }
    renderSide();
    renderTopbar();
    scan();
    insertSectionBars();
    renderToc();
    enhanceCode();
    bindActions();
    bindSpoilers();
    bindAppLinks();
    applyStateToDom();
    renderAll();
    startSync();
    window.addEventListener("storage", function (e) {
      if (e.key === STORE_KEY) { store = load(); applyStateToDom(); renderAll(); }
      if (e.key === THEME_KEY) setTheme(currentTheme());
    });
  }

  window.Harborline = window.Harborline || {};
  window.Harborline.handbook = {
    refresh: function () { scan(); insertSectionBars(); enhanceCode(); bindSpoilers(); applyStateToDom(); renderAll(); },
    state: function () { return JSON.parse(JSON.stringify(store)); },
    toast: toast,
    confirm: confirmDialog,
    pages: PAGES.slice()
  };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot); else boot();
})();
