/* DineSafe Toronto dashboard. Plain JavaScript, no build step.
 * Data comes from data/dinesafe-data.js (window.DINESAFE_DATA), written by `python -m dinesafe build`.
 * See src/dinesafe/export.py for the bundle layout. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const D = window.DINESAFE_DATA;
  if (!D || !D.est) {
    document.querySelector(".layout").innerHTML =
      '<p class="fatal">No data found. Build it first:<br><code>python -m dinesafe run</code><br>' +
      "then reload this page.</p>";
    return;
  }

  // ------------------------------------------------------------------ decoding
  const DICT = D.dict;
  const C = Object.fromEntries(D.est.cols.map((c, i) => [c, i]));
  const FC = Object.fromEntries(D.feat.cols.map((c, i) => [c, i]));
  const N = D.est.rows.length;
  const DAY = 86400000;
  const fmtDate = (days) => (days == null ? "unknown" : new Date(days * DAY).toISOString().slice(0, 10));
  const toDays = (iso) => Math.floor(Date.parse(iso + "T00:00:00Z") / DAY);
  const dataToDay = D.meta.dataTo ? toDays(D.meta.dataTo) : Math.floor(Date.now() / DAY);
  const nf = new Intl.NumberFormat("en-CA");
  const pct = (p) => (p == null ? "n/a" : (p * 100 < 1 && p > 0 ? "<1" : Math.round(p * 100)) + "%");
  const money = (v) => "$" + nf.format(Math.round(v || 0));

  const norm = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, " ").trim();

  const est = D.est.rows.map((r, i) => {
    const postal = r[C.postal] || "";
    const e = {
      i, id: r[C.id], name: r[C.name] || "(unnamed)", address: r[C.address] || "", postal, phone: r[C.phone],
      lat: r[C.lat], lon: r[C.lon], cat: r[C.cat], catSrc: r[C.catSrc], catConf: r[C.catConf],
      type: r[C.type] >= 0 ? DICT.type[r[C.type]] : null, minInsp: r[C.minInsp], legacyId: r[C.legacyId],
      newId: r[C.newId], inactive: r[C.inactive] === 1, lastDate: r[C.lastDate], lastStatus: r[C.lastStatus],
      pending: r[C.pending] === 1, nInsp3y: r[C.nInsp3y], nInspTotal: r[C.nInspTotal], firstDate: r[C.firstDate],
      nCond3y: r[C.nCond3y], nClosed3y: r[C.nClosed3y], nCrucial3y: r[C.nCrucial3y], nSig3y: r[C.nSig3y],
      nMinor3y: r[C.nMinor3y], fines3y: r[C.fines3y], convicted3y: r[C.convicted3y] === 1, chain: r[C.chain],
      pAny: r[C.pAny], pSerious: r[C.pSerious], pNotPass: r[C.pNotPass], band: r[C.band], factors: r[C.factors] || [],
    };
    e.nameKey = norm(e.name);
    e.hay = norm([e.name, e.address, postal, postal.replace(" ", ""), e.id, e.legacyId, e.newId, e.phone].join(" "));
    return e;
  });
  const byId = new Map(est.map((e) => [String(e.id), e]));
  for (const e of est) {
    if (e.legacyId) byId.set(String(e.legacyId), e);
    if (e.newId) byId.set(String(e.newId), e);
  }

  // inspections: grouped per establishment, dates delta-encoded, flags bit-packed
  const IC = D.insp.count, DD = D.insp.dday, FL = D.insp.flags;
  const inspStart = new Int32Array(N + 1);
  for (let i = 0; i < N; i++) inspStart[i + 1] = inspStart[i] + IC[i];
  const inspDay = new Int32Array(DD.length);
  for (let i = 0; i < N; i++) {
    let d = 0;
    for (let k = inspStart[i]; k < inspStart[i + 1]; k++) { d = k === inspStart[i] ? DD[k] : d + DD[k]; inspDay[k] = d; }
  }
  const infStart = new Int32Array(FL.length + 1);
  for (let k = 0; k < FL.length; k++) infStart[k + 1] = infStart[k] + (FL[k] >> 5);
  if (infStart[FL.length] !== D.inf.text.length) console.warn("Infraction counts do not match the bundle");
  const outcomes = new Map(D.outcomes.map((o) => [o[0], o]));

  function inspections(e) {
    const out = [];
    for (let k = inspStart[e.i]; k < inspStart[e.i + 1]; k++) {
      const f = FL[k];
      const infs = [];
      for (let j = infStart[k]; j < infStart[k + 1]; j++) {
        const code = D.inf.code[j];
        infs.push({ text: DICT.infraction[D.inf.text[j]], sev: code & 7, cat: DICT.infCategory[code >> 3], outcome: outcomes.get(j) });
      }
      out.push({ day: inspDay[k], status: (f & 7) - 1, archive: ((f >> 3) & 1) === 1, followup: ((f >> 4) & 1) === 1, infs });
    }
    return out.reverse(); // newest first
  }

  // ------------------------------------------------------------------ labels
  const STATUS = DICT.status; // Pass, Conditional Pass, Closed, Temporarily Not Operating
  const BAND = DICT.band;
  const statusBadge = (s) => `<span class="badge s${s}">${s >= 0 ? esc(STATUS[s]) : "No status"}</span>`;
  const bandBadge = (b) => (b >= 0 ? `<span class="badge r${b}">${esc(BAND[b])} risk</span>` : "");
  const STATUS_COLOR = ["--pass", "--cond", "--closed", "--unknown"];
  const BAND_COLOR = ["--r-low", "--r-avg", "--r-elev", "--r-high"];
  const css = getComputedStyle(document.documentElement);
  const cssVar = (v) => css.getPropertyValue(v).trim() || "#888";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ------------------------------------------------------------------ header
  $("asof").textContent = `Inspections ${D.meta.dataFrom} to ${D.meta.dataTo}` +
    (D.meta.sourceRefreshed ? ` · source refreshed ${String(D.meta.sourceRefreshed).slice(0, 10)}` : "");
  if (D.meta.auc && D.meta.auc.serious) {
    $("aucText").textContent = `${D.meta.auc.any.toFixed(2)} (any infraction), ${D.meta.auc.serious.toFixed(2)} ` +
      `(significant/crucial) and ${D.meta.auc.not_pass.toFixed(2)} (not passed)`;
  }
  $("aboutBtn").addEventListener("click", () => $("about").showModal());

  const activeCount = est.filter((e) => !e.inactive).length;
  const catCounts = new Array(DICT.estCategory.length).fill(0);
  est.forEach((e) => { if (!e.inactive && e.cat >= 0) catCounts[e.cat]++; });
  DICT.estCategory.forEach((c, k) => {
    const o = document.createElement("option");
    o.value = String(k); o.textContent = `${c} (${nf.format(catCounts[k])})`;
    $("fCat").appendChild(o);
  });

  // ------------------------------------------------------------------ filtering
  const state = { shown: 100, selected: null, inViewBounds: null };

  function readFilters() {
    const statuses = new Set([...document.querySelectorAll('input[name="status"]:checked')].map((x) => +x.value));
    const risk = $("fRisk").value;
    return {
      tokens: norm($("q").value).split(" ").filter(Boolean),
      statuses,
      cat: $("fCat").value === "" ? null : +$("fCat").value,
      risk: risk === "" ? null : risk.split("").map(Number),
      recent: $("fRecent").value === "" ? null : +$("fRecent").value,
      crucial: $("fCrucial").checked,
      pending: $("fPending").checked,
      inactive: $("fInactive").checked,
    };
  }

  function matches(e, f) {
    if (!f.inactive && e.inactive) return false;
    // Status filter: establishments whose latest result is something else (e.g. "Temporarily
    // Not Operating") only show when every box is ticked.
    if (f.statuses.size < 3 && !f.statuses.has(e.lastStatus)) return false;
    if (f.cat !== null && e.cat !== f.cat) return false;
    if (f.risk && !f.risk.includes(e.band)) return false;
    if (f.recent !== null) {
      const age = dataToDay - (e.lastDate ?? -1e9);
      if (f.recent > 0 ? age > f.recent : age <= -f.recent) return false;
    }
    if (f.crucial && !(e.nCrucial3y > 0)) return false;
    if (f.pending && !e.pending) return false;
    for (const t of f.tokens) if (!e.hay.includes(t)) return false;
    return true;
  }

  function rank(e, f) {
    if (!f.tokens.length) return 0;
    const q = f.tokens.join(" ");
    if (e.nameKey === q) return 0;
    if (e.nameKey.startsWith(q)) return 1;
    if (f.tokens.every((t) => e.nameKey.includes(t))) return 2;
    return 3;
  }

  let filtered = [];
  function applyFilters() {
    const f = readFilters();
    filtered = est.filter((e) => matches(e, f));
    const sort = $("sortBy").value;
    const byName = (a, b) => a.nameKey.localeCompare(b.nameKey) || a.address.localeCompare(b.address);
    const ranks = new Map(f.tokens.length ? filtered.map((e) => [e.i, rank(e, f)]) : []);
    filtered.sort((a, b) => {
      if (f.tokens.length) { const d = ranks.get(a.i) - ranks.get(b.i); if (d) return d; }
      if (sort === "risk") return (b.pSerious ?? -1) - (a.pSerious ?? -1) || byName(a, b);
      if (sort === "recent") return (b.lastDate ?? -1) - (a.lastDate ?? -1) || byName(a, b);
      return byName(a, b);
    });
    state.shown = 100;
    renderMarkers();
    renderList();
  }

  function listed() {
    if (!$("fInView").checked || !map) return filtered;
    const b = map.getBounds();
    return filtered.filter((e) => e.lat != null && b.contains([e.lat, e.lon]));
  }

  function renderList() {
    const items = listed();
    const withInactive = $("fInactive").checked;
    const total = withInactive ? N : activeCount;
    let txt = `${nf.format(filtered.length)} of ${nf.format(total)} ${withInactive ? "" : "active "}establishments match`;
    if ($("fInView").checked && map) txt += ` · ${nf.format(items.length)} visible on the map`;
    $("count").textContent = txt;
    const ol = $("results");
    const frag = document.createDocumentFragment();
    for (const e of items.slice(0, state.shown)) {
      const li = document.createElement("li");
      li.innerHTML = `<button type="button" data-i="${e.i}"><span class="name">${esc(e.name)}</span>` +
        `<span class="addr">${esc(e.address)}</span><span class="badges">${statusBadge(e.lastStatus)}` +
        `${bandBadge(e.band)}${e.pending ? '<span class="badge tag">Re-inspection pending</span>' : ""}` +
        `${e.inactive ? '<span class="badge tag">Likely closed</span>' : ""}` +
        `<span class="tag small">inspected ${fmtDate(e.lastDate)}</span></span></button>`;
      frag.appendChild(li);
    }
    ol.replaceChildren(frag);
    $("more").hidden = items.length <= state.shown;
    $("more").textContent = `Show more (${nf.format(items.length - state.shown)} remaining)`;
  }

  // ------------------------------------------------------------------ map
  let map = null, markerLayer = null, highlight = null, legend = null;
  if (window.L) {
    map = L.map("map", { preferCanvas: true, minZoom: 9, maxZoom: 19 }).setView([43.715, -79.385], 11);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    const renderer = L.canvas({ padding: 0.3, tolerance: 4 });
    markerLayer = L.layerGroup().addTo(map);
    state.renderer = renderer;
    legend = L.control({ position: "bottomright" });
    legend.onAdd = () => L.DomUtil.create("div", "legend");
    legend.addTo(map);
    map.on("moveend", () => { if ($("fInView").checked) { state.shown = 100; renderList(); } });
  } else {
    $("map").innerHTML = '<p class="map-msg">The map library could not be loaded. Search and details still work.</p>';
  }

  function worse(a, b, mode) {
    if (mode === "risk") return (b.band ?? -1) > (a.band ?? -1) ? b : a;
    const sev = (e) => (e.lastStatus === 2 ? 3 : e.lastStatus === 1 ? 2 : e.lastStatus === 0 ? 1 : 0);
    return sev(b) > sev(a) ? b : a;
  }

  function colorOf(e, mode) {
    if (mode === "risk") return cssVar(BAND_COLOR[e.band] || "--unknown");
    return cssVar(STATUS_COLOR[e.lastStatus] || "--unknown");
  }

  function renderLegend(mode) {
    if (!legend) return;
    const rows = mode === "risk"
      ? BAND.map((b, k) => [BAND_COLOR[k], `${b} risk`])
      : [["--pass", "Pass"], ["--cond", "Conditional Pass"], ["--closed", "Closed"]];
    legend.getContainer().innerHTML = `<strong>${mode === "risk" ? "Predicted risk" : "Latest result"}</strong><br>` +
      rows.map(([v, t]) => `<span class="dot" style="color:${cssVar(v)}"></span>${esc(t)}`).join("<br>") +
      '<br><span class="muted">Larger dot = several establishments</span>';
  }

  function renderMarkers() {
    if (!map) return;
    const mode = $("colorBy").value;
    renderLegend(mode);
    const sites = new Map();
    for (const e of filtered) {
      if (e.lat == null || e.lon == null) continue;
      const key = e.lat.toFixed(5) + "," + e.lon.toFixed(5);
      let s = sites.get(key);
      if (!s) { s = { lat: e.lat, lon: e.lon, items: [], top: e }; sites.set(key, s); }
      s.items.push(e);
      s.top = worse(s.top, e, mode);
    }
    // Draw the most concerning sites last so they sit on top.
    const order = [...sites.values()].sort((a, b) => (mode === "risk" ? (a.top.band ?? -1) - (b.top.band ?? -1)
      : (a.top.lastStatus === 0 ? 0 : 1) - (b.top.lastStatus === 0 ? 0 : 1)));
    markerLayer.clearLayers();
    for (const s of order) {
      const color = colorOf(s.top, mode);
      const m = L.circleMarker([s.lat, s.lon], {
        renderer: state.renderer, radius: s.items.length > 1 ? 7 : 5, color: "#ffffff", weight: 1,
        fillColor: color, fillOpacity: 0.9,
      });
      m.on("click", () => {
        if (s.items.length === 1) { navigate(s.items[0].id); return; }
        const div = document.createElement("div");
        div.className = "site-popup";
        const h = document.createElement("strong");
        h.textContent = `${s.items.length} establishments here`;
        div.appendChild(h);
        s.items.slice().sort((a, b) => a.nameKey.localeCompare(b.nameKey)).forEach((e) => {
          const b = document.createElement("button");
          b.type = "button";
          b.innerHTML = `${esc(e.name)} ${statusBadge(e.lastStatus)}`;
          b.addEventListener("click", () => { map.closePopup(); navigate(e.id); });
          div.appendChild(b);
        });
        L.popup({ maxWidth: 320 }).setLatLng([s.lat, s.lon]).setContent(div).openOn(map);
      });
      markerLayer.addLayer(m);
    }
  }

  function showOnMap(e, zoom) {
    if (!map || e.lat == null) return;
    if (highlight) highlight.remove();
    // Same renderer as the markers: a second canvas would sit on top and swallow clicks.
    highlight = L.circleMarker([e.lat, e.lon], { renderer: state.renderer, radius: 12,
      color: getComputedStyle(document.body).color, weight: 3, fill: false, interactive: false }).addTo(map);
    if (zoom) map.setView([e.lat, e.lon], Math.max(map.getZoom(), 16));
  }

  // ------------------------------------------------------------------ detail view
  const KIND_NAMES = { recent_pest: "pest control", recent_hygiene: "hand washing/hygiene",
    recent_temperature: "temperature control", recent_protection: "food protection",
    recent_food_contact: "dishwashing/food-contact surfaces" };

  // Mirrors model._detail(): plain-language facts behind each factor group.
  function factorDetail(name, v) {
    const cnt = (f) => Math.round(Math.expm1(v[f] || 0));
    const yes = (f) => (v[f] || 0) >= 0.5;
    switch (name) {
      case "Recent inspection results": {
        const n = cnt("last_n_infractions");
        let t = `Last routine inspection: ${n} infraction${n === 1 ? "" : "s"}`;
        if (yes("last_crucial")) t += " incl. crucial"; else if (yes("last_serious")) t += " incl. significant";
        if (yes("last_not_pass")) t += ", not passed";
        return `${t}. Last 3 routine inspections: ${cnt("recent_significant")} significant, ${cnt("recent_crucial")} crucial infraction(s).`;
      }
      case "Kinds of recent infractions": {
        const kinds = Object.keys(KIND_NAMES).filter((f) => cnt(f) > 0).map((f) => `${KIND_NAMES[f]} (${cnt(f)})`);
        return kinds.length ? `Recent infractions involved ${kinds.join(", ")}.`
          : "No pest, hygiene, temperature, food-protection or dishwashing infractions recently.";
      }
      case "Long-term track record": {
        let t = `Estimated long-run rates: ${pct(v.rate_serious)} of inspections with significant/crucial infractions, ` +
          `${pct(v.rate_any)} with any infraction (blended with the city average when the history is short)`;
        const extra = [];
        if (yes("ever_closed")) extra.push("has been closed before");
        if (yes("ever_convicted")) extra.push("has a food-safety conviction");
        return t + (extra.length ? "; " + extra.join(" and ") : "") + ".";
      }
      case "Length of inspection history": {
        const n = cnt("log_n_inspections");
        if (n <= 1) return "Only one inspection on record (new or newly registered establishment).";
        const span = v.history_years >= 0.1 ? `over ${v.history_years.toFixed(1)} years` : "within the last month";
        return `${n} inspections on record ${span}${yes("has_archive_history") ? " (incl. pre-2023 archive)" : ""}.`;
      }
      case "Inspection frequency / City risk tier":
        return `About ${(v.routine_per_year || 0).toFixed(1)} routine inspections per year; City minimum: ` +
          `${yes("min_inspections_unknown") ? "unknown" : (v.min_inspections || 0) + " per year"}.`;
      case "Type of establishment":
        return "";
      case "Chain or independent": {
        const n = Math.round(Math.expm1(v.log_chain_size || 0) + 1);
        return n > 1 ? `Name shared by ${n} locations.` : "Single location (not a chain).";
      }
    }
    return "";
  }

  function effectLabel(x) {
    const a = Math.abs(x);
    const strength = a >= 0.4 ? "strongly " : a < 0.15 ? "slightly " : "";
    return x > 0 ? `<span class="eff up">▲ ${strength}raises</span>` : `<span class="eff down">▼ ${strength}lowers</span>`;
  }

  function renderDetail(e) {
    const featRow = D.feat.rows[e.i] || [];
    const v = Object.fromEntries(D.feat.cols.map((c) => [c, featRow[FC[c]]]));
    const br = D.meta.baseRates || {};
    const catName = DICT.estCategory[e.cat] || "Unknown";
    const catNote = DICT.catSource[e.catSrc] === "inferred"
      ? ` <span class="muted">(inferred from name, ${Math.round((e.catConf || 0) * 100)}% confidence)</span>` : "";
    const ids = [e.id, e.legacyId && e.legacyId !== e.id ? e.legacyId : null, e.newId && e.newId !== e.id ? e.newId : null]
      .filter(Boolean).join(" / ");
    const ins = inspections(e);
    const recent = ins.filter((x) => !x.archive);
    const archive = ins.filter((x) => x.archive);
    const st = e.lastStatus;

    let h = `<button type="button" class="back" id="backBtn">&larr; Back to results</button>`;
    h += `<h2>${esc(e.name)}</h2>`;
    h += `<p class="meta-line">${esc(e.address)}${e.phone ? " · " + esc(e.phone) : ""}</p>`;
    h += `<p class="meta-line">${esc(catName)}${e.type ? " · " + esc(e.type) : ""}${catNote}` +
      `${e.minInsp ? ` · City minimum: ${e.minInsp} inspection${e.minInsp > 1 ? "s" : ""}/year` : ""}</p>`;
    h += `<p class="meta-line small">DineSafe ID ${esc(ids)}</p>`;
    if (e.inactive) {
      h += `<p class="notice">This establishment was not carried into the City's new inspection system ` +
        `(November 2025), so it has most likely closed or moved.</p>`;
    }
    h += `<div class="latest s${st}"><strong>${st >= 0 ? esc(STATUS[st]) : "No status"}</strong> ` +
      `<span class="sub">latest result, inspected ${fmtDate(e.lastDate)}</span></div>`;
    if (e.pending) {
      h += `<p class="notice">Re-inspection pending. After a Conditional Pass or Closed result, Toronto Public ` +
        `Health normally re-inspects within a few days.</p>`;
    }

    h += `<h3>Last 3 years</h3><table class="kv"><tbody>` +
      `<tr><td>Inspections</td><td class="num">${e.nInsp3y}</td></tr>` +
      `<tr><td>Conditional passes</td><td class="num">${e.nCond3y}</td></tr>` +
      `<tr><td>Closures</td><td class="num">${e.nClosed3y}</td></tr>` +
      `<tr><td>Crucial infractions</td><td class="num">${e.nCrucial3y}</td></tr>` +
      `<tr><td>Significant infractions</td><td class="num">${e.nSig3y}</td></tr>` +
      `<tr><td>Minor infractions</td><td class="num">${e.nMinor3y}</td></tr>` +
      `<tr><td>Fines${e.convicted3y ? " (convicted)" : ""}</td><td class="num">${money(e.fines3y)}</td></tr>` +
      `</tbody></table>`;

    h += `<h3>Next routine inspection: model estimate</h3>`;
    if (e.pSerious == null) {
      h += `<p class="muted">No prediction available.</p>`;
    } else {
      h += `<table class="kv"><thead><tr><th>Chance of</th><th class="num">This place</th><th class="num">City average</th></tr></thead><tbody>` +
        `<tr><td>Significant or crucial infraction</td><td class="num"><strong>${pct(e.pSerious)}</strong></td><td class="num">${pct(br.serious)}</td></tr>` +
        `<tr><td>Conditional Pass or Closed</td><td class="num">${pct(e.pNotPass)}</td><td class="num">${pct(br.not_pass)}</td></tr>` +
        `<tr><td>Any infraction</td><td class="num">${pct(e.pAny)}</td><td class="num">${pct(br.any)}</td></tr>` +
        `</tbody></table>`;
      if (e.band >= 0 && br.serious) {
        h += `<p>${bandBadge(e.band)} <span class="muted">${(e.pSerious / br.serious).toFixed(1)}&times; the city-wide ` +
          `chance of a significant/crucial infraction</span></p>`;
      }
      if (e.factors.length) {
        h += `<h4>What drives this estimate</h4><ul class="factors">`;
        for (const [g, eff] of e.factors) {
          const name = DICT.factor[g];
          let detail = factorDetail(name, v);
          if (name === "Type of establishment") detail = `Category: ${esc(catName)}.`;
          h += `<li>${effectLabel(eff)}<span><strong>${esc(name)}</strong>${detail ? ": " + esc(detail) : ""}</span></li>`;
        }
        h += `</ul>`;
      }
      h += `<p class="muted small">Statistical estimate from this establishment's past inspection record. It is not an ` +
        `official rating. See <button type="button" class="link" id="aboutLink">About</button> for how it works.</p>`;
    }

    h += `<h3>Inspection history</h3>`;
    h += `<label class="small"><input type="checkbox" id="onlyInf"> Only inspections with infractions</label>`;
    h += `<ol class="history" id="histList">${recent.map(inspectionHtml).join("") || "<li class='muted'>No inspections in the current feed.</li>"}</ol>`;
    if (archive.length) {
      const years = `${fmtDate(archive[archive.length - 1].day).slice(0, 4)} to ${fmtDate(archive[0].day).slice(0, 4)}`;
      h += `<details class="archive"><summary>Archived inspections, ${years} (${archive.length})</summary>` +
        `<p class="muted small">The archive lists infractions reliably but rarely records Conditional Pass or ` +
        `Closed results.</p><ol class="history">${archive.map(inspectionHtml).join("")}</ol></details>`;
    }

    const view = $("detailView");
    view.innerHTML = h;
    $("backBtn").addEventListener("click", () => navigate(null));
    const al = $("aboutLink");
    if (al) al.addEventListener("click", () => $("about").showModal());
    $("onlyInf").addEventListener("change", (ev) => {
      view.querySelectorAll(".history > li[data-clean]").forEach((li) => { li.hidden = ev.target.checked; });
    });
  }

  function inspectionHtml(x) {
    const tags = [];
    if (x.followup) tags.push('<span class="badge tag">Re-inspection</span>');
    if (x.archive) tags.push('<span class="badge tag">Archive</span>');
    const n = x.infs.length;
    let h = `<li${n ? "" : " data-clean"}><div class="ih"><span class="date">${fmtDate(x.day)}</span>` +
      `${statusBadge(x.status)}${tags.join("")}<span class="muted">${n ? n + " infraction" + (n > 1 ? "s" : "") : "No infractions"}</span></div>`;
    if (n) {
      h += `<ul class="inf">`;
      for (const f of x.infs) {
        h += `<li><span class="sev sev${f.sev}">${esc(DICT.severity[f.sev])}</span>${esc(f.text)} ` +
          `<span class="muted small">(${esc(f.cat)})</span>`;
        if (f.outcome) {
          const [, oi, od, fine] = f.outcome;
          const parts = [oi >= 0 ? DICT.outcome[oi] : "Court outcome", fine != null ? money(fine) : null, od != null ? fmtDate(od) : null];
          h += `<span class="outcome">Prosecution: ${esc(parts.filter(Boolean).join(" · "))}</span>`;
        }
        h += `</li>`;
      }
      h += `</ul>`;
    }
    return h + `</li>`;
  }

  // ------------------------------------------------------------------ routing
  // The selected establishment lives in the URL hash (#id=...), so links can be shared and
  // the browser's back button returns to the list.
  function navigate(id) {
    if (id) {
      location.hash = "id=" + encodeURIComponent(id);
    } else {
      history.pushState(null, "", location.pathname + location.search);
      route();
    }
  }

  function route() {
    const m = /^#id=(.+)$/.exec(location.hash);
    const e = m ? byId.get(decodeURIComponent(m[1])) : null;
    if (e) {
      state.selected = e;
      renderDetail(e);
      $("searchView").hidden = true;
      $("detailView").hidden = false;
      $("panel").scrollTop = 0;
      showOnMap(e, true);
      // On narrow screens the panel sits below the map, so bring the details into view.
      if (window.matchMedia("(max-width: 820px)").matches) $("detailView").scrollIntoView({ block: "start" });
      document.title = `${e.name} · DineSafe Toronto Map`;
    } else {
      state.selected = null;
      $("detailView").hidden = true;
      $("searchView").hidden = false;
      if (highlight) { highlight.remove(); highlight = null; }
      document.title = "DineSafe Toronto Map";
    }
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------------ events
  let timer = null;
  const debounced = () => { clearTimeout(timer); timer = setTimeout(applyFilters, 150); };
  $("q").addEventListener("input", debounced);
  $("q").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { const first = listed()[0]; if (first) navigate(first.id); }
  });
  document.querySelectorAll("#filters input, #filters select").forEach((el) => {
    el.addEventListener("change", el.id === "fInView" ? () => { state.shown = 100; renderList(); } : applyFilters);
  });
  $("reset").addEventListener("click", () => {
    $("q").value = "";
    document.querySelectorAll('input[name="status"]').forEach((x) => { x.checked = true; });
    ["fCat", "fRisk", "fRecent"].forEach((id) => { $(id).value = ""; });
    ["fCrucial", "fPending", "fInactive", "fInView"].forEach((id) => { $(id).checked = false; });
    $("colorBy").value = "status"; $("sortBy").value = "name";
    applyFilters();
  });
  $("results").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-i]");
    if (b) navigate(est[+b.dataset.i].id);
  });
  $("more").addEventListener("click", () => { state.shown += 200; renderList(); });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && state.selected && !$("about").open) navigate(null);
  });

  applyFilters();
  route();
  window.dinesafe = { est, inspections, applyFilters, map }; // handy for debugging in the console
})();
