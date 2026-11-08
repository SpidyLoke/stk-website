"use strict";
/* KartCut UI. Plain JS, no build step. Talks to the local server at /api. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function pref(key, dflt) {
  try { const v = localStorage.getItem(`kc_${key}`); return v === null ? dflt : JSON.parse(v); } catch (_) { return dflt; }
}
function setPref(key, v) {
  try { localStorage.setItem(`kc_${key}`, JSON.stringify(v)); } catch (_) { /* private mode */ }
}

const S = {
  sel: new Set(), orient: pref("orient", "all"), kartOrient: pref("kartOrient", "all"), cropSize: pref("cropSize", 130), cardSize: pref("cardSize", 230), helmet: pref("helmet", false),
  tab: "ingest", status: null, clips: [], bins: null, drivers: [], bin: "all", selDriver: null,
  candidates: [], groups: [], groupStats: {}, kartView: "groups", exposure: null, plans: null, jobSeen: {}, offSources: [], ver: Date.now(), music: null, seed: 1,
};

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), err ? 7000 : 3500);
}

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) {
    const msg = (data && data.detail) || `${r.status} ${r.statusText}`;
    toast(typeof msg === "string" ? msg : JSON.stringify(msg), true);
    throw new Error(msg);
  }
  return data;
}

const media = (rel) => (rel ? `/media/${rel.split("/").map(encodeURIComponent).join("/")}?v=${S.ver}` : "");
const fmtDur = (s) => {
  s = Math.max(0, +s || 0);
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
};
const pct = (v) => `${Math.round((v || 0) * 100)}%`;
const sgn = (v, d = 1) => (v > 0 ? "+" : "") + (+v || 0).toFixed(d);
const driverName = (id) => {
  const d = S.drivers.find((x) => x.id === id);
  return d ? `${d.number ? "#" + d.number + " " : ""}${d.name || ""}`.trim() : "Unknown";
};

/* ------------------------------------------------------------------ tabs */
function showTab(name) {
  S.tab = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === `tab-${name}`));
  try { localStorage.setItem("kc_tab", name); } catch (_) { /* private mode */ }
  refreshTab();
}

async function refreshTab() {
  if (!S.status || !S.status.project) return;
  if (S.tab === "drivers") { await loadClips(); await loadCandidates(); await loadGroups(); renderDrivers(); }
  if (S.tab === "clips") { await loadClips(); renderClips(); }
  if (S.tab === "colour") { await loadExposure(); renderColour(); }
  if (S.tab === "montage") renderMontage();
  if (S.tab === "ingest") renderIngest();
}

/* ------------------------------------------------------------------ status */
async function loadStatus() {
  S.status = await api("GET", "/api/status");
  const p = S.status.project;
  $("#projBtn").textContent = p ? p.name : "No project: create one";
  $("#projPath").placeholder = `Default: ${S.status.home}`;
  renderChips();
  applySettings();
  if (!p) openProjects();
  return S.status;
}

function renderChips() {
  const sys = S.status.system || {};
  const m = S.status.models || {};
  const el = $("#chips");
  el.replaceChildren();
  const chip = (cls, txt, title) => el.append(h("span", { class: `chip ${cls}`, title }, txt));
  if (sys.device && sys.device.startsWith("cuda")) chip("ok", sys.gpu || "NVIDIA GPU", "AI runs on the GPU");
  else chip("warn", "CPU only", "No CUDA GPU found by PyTorch: analysis will be slower");
  chip(sys.nvenc ? "ok" : "warn", sys.nvenc ? "NVENC" : "x264", "Video encoder for renders");
  if (m.detector) chip(m.detector === "none" ? "no" : "ok", m.detector, "Kart detector");
  else chip("", sys.ultralytics ? "AI loads on first analysis" : "AI not installed", "");
  if (sys.ffmpeg_error) chip("no", "FFmpeg missing", sys.ffmpeg_error);
}

function applySettings() {
  const s = S.status.settings || {};
  const prof = $("#setProfile");
  if (!prof.options.length) {
    for (const p of S.status.profiles) prof.append(h("option", { value: p.key }, p.label));
  }
  prof.value = s.footage_profile;
  $("#setFps").value = String(s.analysis_fps || 0);
  $("#setProxies").checked = !!s.make_proxies;
  $("#expStr").value = s.match_exposure_strength;
  $("#wbStr").value = s.match_wb_strength;
  $("#expStrOut").textContent = pct(s.match_exposure_strength);
  $("#wbStrOut").textContent = pct(s.match_wb_strength);
  $("#musicPath").value = s.music_path || "";
  $("#musicStart").value = s.music_start || 0;
  $("#mEventLen").value = s.event_length;
  $("#mReelLen").value = s.reel_length;
  $("#mA169").checked = (s.aspects || []).includes("16:9");
  $("#mA916").checked = (s.aspects || []).includes("9:16");
  $("#mIncidents").checked = !!s.include_incidents;
  $("#mSlow").checked = !!s.slow_motion;
  $("#mEng").value = s.engine_mix;
  $("#mEngOut").textContent = pct(s.engine_mix);
  $("#mLook").value = s.look;
  $("#mFps").value = String(s.output_fps || 0);
  $("#mLut").value = s.custom_lut || "";
  $("#lutRow").hidden = s.look !== "custom";
}

async function saveSetting(obj) {
  if (!S.status.project) return;
  const s = await api("POST", "/api/settings", obj);
  S.status.settings = s;
}

/* ------------------------------------------------------------------ jobs */
async function pollJobs() {
  let jobs = [];
  try { jobs = await api("GET", "/api/jobs"); } catch (_) { /* server restarting */ }
  const active = jobs.find((j) => j.status === "running") || jobs.find((j) => j.status === "queued");
  const bar = $("#jobBar");
  if (active) {
    bar.hidden = false;
    $("#jobTitle").textContent = active.title;
    const eta = active.eta ? ` · ${fmtDur(active.eta)} left` : "";
    $("#jobMsg").textContent = (active.message || active.status) + eta;
    $("#jobFill").style.width = `${Math.round(active.progress * 100)}%`;
    $("#jobCancel").onclick = () => api("POST", `/api/jobs/${active.id}/cancel`);
  } else bar.hidden = true;
  for (const j of jobs) {
    const prev = S.jobSeen[j.id];
    S.jobSeen[j.id] = j.status;
    if (prev && prev !== j.status && ["done", "error", "cancelled"].includes(j.status)) onJobFinished(j);
  }
  setTimeout(pollJobs, active ? 1000 : 3000);
}

async function onJobFinished(j) {
  if (j.status === "error") { toast(`${j.title} failed: ${j.error}`, true); }
  else if (j.status === "cancelled") toast(`${j.title} cancelled`);
  else toast(`${j.title}: done`);
  S.ver = Date.now();
  await loadStatus();
  if (j.kind === "offload" && j.result) renderOffloadResult(j.result);
  if (j.kind === "shotmatch" && j.result) $("#matchResult").replaceChildren(
    h("div", { class: "banner good" }, `${j.result.count} LUTs written`),
    h("button", { class: "btn small", onclick: () => openPath(j.result.folder) }, "Open folder"));
  if (j.kind === "rematch" && j.result) toast(`Re-matched. ${j.result.hd_previews || 0} sharper kart previews, ${j.result.seeds || 0} karts known for sure, the rest matched by look.`);
  if (j.kind === "sort" && j.result) toast(`Sorted ${j.result.placed} clips into ${j.result.folder}${j.result.moved_out ? `, ${j.result.moved_out} moved out of old folders` : ""}`);
  if (j.kind === "analyse" && j.result && j.result.errors) toast(`${j.result.errors} clip(s) failed. See Clips > Pending.`, true);
  refreshTab();
}

/* ------------------------------------------------------------------ picker */
let pk = null;
function openPicker({ title = "Choose a folder", kind = "dir", start = "" } = {}) {
  return new Promise((resolve) => {
    pk = { kind, resolve, path: start };
    $("#pkTitle").textContent = title;
    $("#pkSelect").hidden = kind !== "dir";
    $("#pkHint").textContent = kind === "dir" ? "Open the folder, then press Use this folder" : "Click a file to pick it";
    $("#picker").hidden = false;
    pkLoad(start);
  });
}
async function pkLoad(path) {
  let d;
  try { d = await api("GET", `/api/fs?path=${encodeURIComponent(path || "")}&kind=${pk.kind}`); }
  catch (_) { return; }
  pk.path = d.path;
  pk.parent = d.parent;
  $("#pkPath").value = d.path;
  const list = $("#pkList");
  list.replaceChildren();
  for (const x of d.dirs) list.append(h("div", { class: "pk-item", onclick: () => pkLoad(x.path) }, `📁 ${x.name}`));
  for (const f of d.files) {
    list.append(h("div", { class: "pk-item file", onclick: () => pkDone(f.path) }, f.name,
      h("small", {}, `${(f.size / 1e6).toFixed(1)} MB`)));
  }
  if (!d.dirs.length && !d.files.length) list.append(h("div", { class: "pk-item" }, "(empty)"));
}
function pkDone(val) {
  $("#picker").hidden = true;
  if (pk) pk.resolve(val);
  pk = null;
}

async function openPath(p, folder = true) {
  await api("POST", "/api/open", { path: p, folder });
}

/* ------------------------------------------------------------------ projects */
function openProjects() {
  const rec = $("#projRecent");
  rec.replaceChildren();
  for (const r of S.status.recent || []) {
    rec.append(h("div", { class: "rowitem" }, h("span", { class: "path", title: r.path }, `${r.name}  ·  ${r.path}`),
      h("button", { class: "btn small", onclick: () => openProject(r.path) }, "Open")));
  }
  if (!(S.status.recent || []).length) rec.append(h("p", { class: "hint" }, "No recent projects yet."));
  $("#projModal").hidden = false;
}
async function openProject(path) {
  await api("POST", "/api/project/open", { path });
  $("#projModal").hidden = true;
  S.drivers = []; S.clips = []; S.plans = null; S.music = null;
  await loadStatus();
  refreshTab();
}

/* ------------------------------------------------------------------ ingest */
function renderIngest() {
  const src = $("#offSources");
  src.replaceChildren();
  S.offSources.forEach((s, i) => {
    const reel = h("input", { value: s.reel, placeholder: "Reel name", oninput: (e) => (s.reel = e.target.value) });
    src.append(h("div", { class: "rowitem" }, h("span", { class: "path", title: s.path }, s.path), reel,
      h("button", { class: "link", onclick: () => { S.offSources.splice(i, 1); renderIngest(); } }, "✕")));
  });
  if (!S.offSources.length) src.append(h("p", { class: "hint" }, "No cards added. Plug in the card reader and add each card (e.g. E:\\)."));
  const p = S.status && S.status.project;
  const fl = $("#folders");
  fl.replaceChildren();
  if (p) {
    const counts = {};
    for (const c of S.clips) counts[c.folder] = (counts[c.folder] || 0) + 1;
    for (const f of p.folders) {
      fl.append(h("div", { class: "rowitem" }, h("span", { class: "path", title: f }, f),
        h("span", { class: "hint" }, counts[f] !== undefined ? `${counts[f]} clips` : ""),
        h("button", { class: "link", title: "Remove from project (files are not deleted)", onclick: async () => {
          await api("POST", "/api/footage/remove", { folder: f }); await loadStatus(); await loadClips(); renderIngest();
        } }, "✕")));
    }
    if (!p.folders.length) fl.append(h("p", { class: "hint" }, "Add the folder with today's footage (or offload the cards first)."));
    $("#footStats").textContent = `${p.clips} clips in project, ${p.analysed} analysed.`;
  }
  const sys = S.status ? S.status.system : {};
  const m = S.status ? S.status.models : {};
  const info = $("#sysInfo");
  info.replaceChildren();
  const row = (k, v) => info.append(h("span", {}, k), h("b", {}, v));
  row("GPU", sys.gpu || (sys.device || "cpu").toUpperCase());
  row("Encoder", sys.nvenc ? "NVIDIA NVENC" : "x264 (CPU)");
  row("GPU decode", sys.cuda_decode ? "available" : "no");
  row("Detector", m.detector || "loads on first analysis");
  if (m.detector_world_error) row("YOLO-World", m.detector_world_error.slice(0, 140));
  row("Number reading", m.ocr || (sys.ocr ? "RapidOCR (ready)" : "not installed"));
  row("Celebrations", m.pose || "loads on first analysis");
  row("Appearance", m.embedder || "loads on first analysis");
  row("FFmpeg", sys.ffmpeg || sys.ffmpeg_error || "?");
}

function renderOffloadResult(r) {
  const cls = r.safe_to_format ? "good" : r.verified ? "warn" : "bad";
  $("#offResult").replaceChildren(
    h("div", { class: `banner ${cls}` }, r.message),
    h("p", { class: "hint" }, `${r.files} files, ${(r.bytes / 1e9).toFixed(2)} GB. Manifest saved next to each copy.`),
    ...r.dest_folders.map((d) => h("div", { class: "rowitem" }, h("span", { class: "path" }, d),
      h("button", { class: "btn small", onclick: () => openPath(d) }, "Open"))),
    ...(r.mismatches || []).map((m) => h("div", { class: "hint danger" }, `FAILED: ${m}`)));
}

/* ------------------------------------------------------------------ clips data */
async function loadClips() {
  if (!S.status || !S.status.project) return;
  const d = await api("GET", "/api/clips");
  S.clips = d.clips;
  S.bins = d.bins;
  S.drivers = d.drivers;
}

function clipFilter(c) {
  const b = S.bin;
  const q = $("#clipSearch").value.trim().toLowerCase();
  if (q && !c.name.toLowerCase().includes(q)) return false;
  const junk = c.junk && c.junk.is_junk;
  if (b === "junk") return junk;
  if (b === "pending") return c.status !== "analysed";
  if (c.status !== "analysed" || junk) return false;
  if (S.orient === "vertical" && c.orientation !== "vertical") return false;
  if (S.orient === "horizontal" && c.orientation === "vertical") return false;
  if (b === "all") return true;
  if (b === "review") return c.needs_review;
  if (b === "multi") return c.drivers.length > 1;
  if (b === "broll") return !c.drivers.length && !c.needs_review;
  if (b === "unassigned") return !c.drivers.length;
  if (b === "vertical") return c.orientation === "vertical";
  if (b === "exposure") return ["warn", "bad"].includes(c.exposure.level);
  if (b === "incident") return c.tags.includes("incident");
  if (b.startsWith("driver:")) return c.drivers.some((d) => d.id === b.slice(7));
  return true;
}

function renderClips() {
  const bins = $("#bins");
  bins.replaceChildren();
  if (!S.bins) return;
  const sp = S.bins.special || {};
  const bin = (key, label, count) => {
    const el = h("div", { class: `bin${S.bin === key ? " on" : ""}`, onclick: () => { S.bin = key; renderClips(); } },
      h("span", {}, label), h("span", { class: "c" }, count ?? ""));
    const target = key.startsWith("driver:") ? key.slice(7) : key === "broll" ? "" : key === "junk" ? "junk" : null;
    if (target !== null) {
      el.title = target === "junk" ? "Drop clips here to junk them" : target ? `Drop clips here to move them to ${label}` : "Drop clips here: no driver";
      el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop"); });
      el.addEventListener("dragleave", () => el.classList.remove("drop"));
      el.addEventListener("drop", (e) => {
        e.preventDefault(); el.classList.remove("drop");
        const ids = JSON.parse(e.dataTransfer.getData("text/plain") || "[]");
        if (!ids.length) return;
        if (target === "junk") junkClips(ids); else moveClips(ids, target || null, "move");
      });
    }
    bins.append(el);
  };
  const analysedCount = S.clips.filter((c) => c.status === "analysed" && !(c.junk && c.junk.is_junk)).length;
  bin("all", "All clips", analysedCount);
  if (sp.review) bin("review", "⚠ Needs review", sp.review);
  for (const [cat, list] of Object.entries(S.bins.categories || {})) {
    bins.append(h("div", { class: "bin-h" }, cat));
    for (const d of list) bin(`driver:${d.id}`, d.label, d.count);
  }
  bins.append(h("div", { class: "bin-h" }, "Smart bins"));
  bin("multi", "Multiple drivers", sp.multi || 0);
  bin("broll", "B-roll / no kart", sp.broll || 0);
  bin("vertical", "Shot vertical", sp.vertical || 0);
  bin("exposure", "Exposure issues", sp.exposure || 0);
  bin("incident", "Incidents", sp.incident || 0);
  bin("junk", "Junk", sp.junk || 0);
  if (sp.pending) bin("pending", "Not analysed yet", sp.pending);

  const sort = $("#clipSort").value;
  // orientation toggle shows how many of each the current bin holds
  const saved = S.orient;
  S.orient = "all";
  const inBin = S.clips.filter(clipFilter);
  S.orient = saved;
  const nV = inBin.filter((c) => c.orientation === "vertical").length;
  const counts = { all: inBin.length, horizontal: inBin.length - nV, vertical: nV };
  $$("#orientSeg button").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.v === S.orient);
    btn.textContent = `${{ all: "All", horizontal: "Horizontal", vertical: "Vertical" }[btn.dataset.v]} ${counts[btn.dataset.v]}`;
  });
  let list = S.clips.filter(clipFilter);
  if (sort === "score") list.sort((a, b) => b.best_score - a.best_score);
  else if (sort === "name") list.sort((a, b) => a.name.localeCompare(b.name));
  const grid = $("#clipGrid");
  grid.style.setProperty("--card", `${S.cardSize}px`);
  grid.replaceChildren();
  renderBulk();
  if (!list.length) {
    grid.append(h("div", { class: "empty" }, S.clips.length ? "Nothing in this bin." :
      "No clips yet. Add a footage folder on the Ingest tab and press Analyse."));
    return;
  }
  for (const c of list.slice(0, 600)) grid.append(clipCard(c));
}

function clipCard(c) {
  const lvl = (c.exposure && c.exposure.level) || "";
  const tl = h("div", { class: "tl" }, lvl ? h("span", { class: `dot ${lvl}`, title: (c.exposure.issues || []).join("\n") || "Exposure OK" }) : null,
    c.orientation === "vertical" ? h("span", { class: "pill" }, "9:16") : null,
    c.profile ? h("span", { class: "pill" }, c.profile.startsWith("slog3") ? "S-LOG3" : c.profile.toUpperCase()) : null);
  const thumb = h("div", { class: "thumb" }, c.thumb ? h("img", { src: media(c.thumb), loading: "lazy", alt: "" }) : null, tl,
    h("span", { class: "br" }, fmtDur(c.duration)));
  const tags = h("div", { class: "tags" });
  if (c.junk && c.junk.is_junk) tags.append(h("span", { class: "tag review" }, (c.junk.reasons || ["Junk"])[0] || "Marked junk"));
  for (const d of c.drivers) tags.append(h("span", { class: "tag drv", title: `${Math.round(d.prob * 100)}% sure` }, d.label));
  if (c.needs_review) tags.append(h("span", { class: "tag review" }, "Needs review"));
  if (c.status === "error") tags.append(h("span", { class: "tag review", title: c.error }, "Analysis failed"));
  if (c.status === "new") tags.append(h("span", { class: "tag" }, "Not analysed"));
  for (const t of c.tags) tags.append(h("span", { class: `tag ${t.replace(/\s+/g, "-")}` }, t));
  const pick = h("input", { type: "checkbox", class: "pick", checked: S.sel.has(c.id), title: "Select",
    onclick: (e) => { e.stopPropagation(); toggleSel(c.id); } });
  const card = h("div", { class: `clip${S.sel.has(c.id) ? " selected" : ""}`, draggable: "true",
    onclick: (e) => { if (e.ctrlKey || e.metaKey || e.shiftKey) { toggleSel(c.id); } else openClip(c.id); } },
  pick, thumb,
  h("div", { class: "meta" }, h("div", { class: "name" }, h("span", {}, c.name), h("span", {}, (c.created || "").slice(11, 16))), tags));
  card.addEventListener("dragstart", (e) => {
    const ids = S.sel.has(c.id) ? [...S.sel] : [c.id];
    e.dataTransfer.setData("text/plain", JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  return card;
}

function toggleSel(id) {
  if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
  renderClips();
}

function driverOptions(placeholder, withNone) {
  const ds = S.drivers.slice().sort((a, b) => (a.category || "").localeCompare(b.category || "") || (+a.number || 0) - (+b.number || 0));
  return [h("option", { value: "" }, placeholder), ...ds.map((d) => h("option", { value: d.id }, driverName(d.id))),
    withNone ? h("option", { value: "__none" }, "No driver (B-roll / other racer)") : null];
}

async function moveClips(ids, driver, mode = "move") {
  await api("POST", "/api/clips/move", { clips: ids, driver, mode });
  S.sel.clear();
  await loadClips();
  if (S.tab === "clips") renderClips();
  const who = driver ? driverName(driver) : "no driver";
  toast(mode === "add" ? `Also added to ${who}` : mode === "remove" ? `Removed from ${who}` :
    `${ids.length} clip${ids.length > 1 ? "s" : ""} moved to ${who}. KartCut learns from this.`);
}

async function junkClips(ids) {
  for (const id of ids) await api("POST", `/api/clips/${id}/junk`, { junk: true });
  S.sel.clear();
  await loadClips(); renderClips();
  toast(`${ids.length} clip${ids.length > 1 ? "s" : ""} moved to Junk`);
}

function renderBulk() {
  const bar = $("#bulkBar");
  bar.replaceChildren();
  bar.hidden = S.sel.size === 0;
  if (!S.sel.size) return;
  const ids = [...S.sel];
  bar.append(h("strong", {}, `${ids.length} selected`),
    h("select", { onchange: (e) => { const v = e.target.value; if (v) moveClips(ids, v === "__none" ? null : v, "move"); } },
      driverOptions("Move to…", true)),
    h("select", { onchange: (e) => { if (e.target.value) moveClips(ids, e.target.value, "add"); } }, driverOptions("Also add…", false)),
    h("button", { class: "btn small ghost", onclick: () => junkClips(ids) }, "Junk"),
    h("span", { class: "spacer" }),
    h("button", { class: "link", onclick: () => { S.sel.clear(); renderClips(); } }, "Clear selection"));
}

/* ------------------------------------------------------------------ clip drawer */
async function openClip(cid) {
  const d = await api("GET", `/api/clips/${cid}`);
  const s = d.summary;
  $("#dTitle").textContent = `${d.clip.name}  ·  ${fmtDur(d.clip.duration)}  ·  ${(d.clip.fps || 0).toFixed(2)} fps`;
  const body = $("#dBody");
  body.replaceChildren();
  const chips = (s.drivers || []).map((x) => h("span", { class: "dchip" }, x.label,
    h("button", { title: "Remove this driver from the clip", onclick: async () => { await moveClips([cid], x.id, "remove"); openClip(cid); } }, "✕")));
  body.append(h("div", { class: "dmove" },
    h("strong", {}, "Driver:"), chips.length ? chips : h("span", { class: "hint" }, s.needs_review ? "not sure (needs review)" : "none"),
    h("span", { class: "spacer" }),
    h("select", { onchange: async (e) => { const v = e.target.value; if (!v) return; await moveClips([cid], v === "__none" ? null : v, "move"); openClip(cid); } },
      driverOptions("Move to…", true)),
    h("select", { onchange: async (e) => { if (!e.target.value) return; await moveClips([cid], e.target.value, "add"); openClip(cid); } },
      driverOptions("Also in…", false)),
    h("button", { class: "btn small ghost", title: "Forget manual driver choices for this clip", onclick: async () => {
      await api("POST", `/api/clips/${cid}/drivers`, { drivers: null }); await loadClips(); if (S.tab === "clips") renderClips(); openClip(cid);
    } }, "Auto")));
  let video = null;
  if (d.clip.proxy) {
    video = h("video", { src: media(d.clip.proxy), controls: true, preload: "metadata" });
    body.append(video);
  } else if (s.thumb) body.append(h("img", { class: "still", src: media(s.thumb), alt: "" }), h("p", { class: "hint" }, "No preview proxy yet (made after analysis if enabled)."));

  if (d.segments.length) {
    body.append(h("h3", {}, "Highlights"));
    for (const sg of d.segments.slice().sort((a, b) => b.score - a.score)) {
      body.append(h("div", { class: "seg", onclick: () => { if (video) { video.currentTime = Math.max(0, sg.a); video.play(); } } },
        h("span", {}, `${sg.label || sg.type}${sg.incident ? "  ⚠ incident" : ""}`),
        h("span", { class: "hint" }, `${sg.a.toFixed(1)} to ${sg.b.toFixed(1)}s · score ${sg.score.toFixed(2)}`)));
    }
  }
  const e = s.exposure || {};
  const q = s.quality || {};
  const a = d.analysis || {};
  const expBox = h("div", {}, h("h3", {}, "Exposure + colour"),
    h("div", { class: `banner ${e.level === "bad" ? "bad" : e.level === "warn" ? "warn" : "good"}` },
      e.level === "good" ? "Exposure OK" : e.level === "bad" ? "Problem" : "Check"),
    (e.issues || []).length ? h("ul", { class: "issues" }, e.issues.map((i) => h("li", {}, i))) : null,
    h("div", { class: "kv" },
      h("span", {}, "Exposure"), h("span", {}, `${sgn(e.ev)} st (${sgn(e.ev_rel)} vs session)`),
      h("span", {}, "Clipped"), h("span", {}, `${e.clip_pct ?? 0}% frame · ${e.subj_clip_pct ?? 0}% kart`),
      h("span", {}, "White balance"), h("span", {}, `~${e.cct || "?"} K · ${sgn(e.cast_rel, 0)} mired vs session`),
      h("span", {}, "Profile"), h("span", {}, `${a.profile || "?"} (${a.profile_source || ""})`)),
    s.waveform ? h("img", { class: "wave", src: media(s.waveform), alt: "Luma waveform", title: "Luma waveform. Guides: clip, 90% white, 18% grey, black" }) : null);
  const qBox = h("div", {}, h("h3", {}, "Shot quality"), h("div", { class: "kv" },
    h("span", {}, "Kart focus"), h("span", {}, q.focus === null || q.focus === undefined ? "no kart" : pct(q.focus)),
    h("span", {}, "Stability"), h("span", {}, `${pct(q.stability)} (shake ${q.shake})`),
    h("span", {}, "Panning"), h("span", {}, q.pan ? `${pct(q.pan)} of the time` : "static"),
    h("span", {}, "Orientation"), h("span", {}, `${s.orientation} (${s.orientation_source || ""})`),
    h("span", {}, "Detector"), h("span", {}, a.detector || "?"),
    h("span", {}, "Analysis"), h("span", {}, `${a.seconds ?? "?"} s at ${(a.analysis_fps || 0).toFixed(1)} fps`)),
    (q.flags || []).length ? h("div", { class: "tags" }, q.flags.map((f) => h("span", { class: `tag ${f.replace(/\s+/g, "-")}` }, f))) : null);
  body.append(h("div", { class: "dgrid" }, expBox, qBox));

  body.append(h("h3", {}, `Karts in this clip (${d.tracks.length})`));
  if (!d.tracks.length) body.append(h("p", { class: "hint" }, "No karts detected."));
  for (const t of d.tracks) {
    const sel = h("select", { onchange: async (ev) => {
      const v = ev.target.value;
      await api("POST", `/api/clips/${cid}/assign`, { track: t.id, driver: v === "auto" ? null : v });
      toast("Saved. KartCut will learn from this.");
      await loadClips(); if (S.tab === "clips") renderClips(); openClip(cid);
    } }, h("option", { value: "auto" }, t.source === "manual" ? "Auto (undo my choice)" : `Auto: ${t.driver_label || "unknown"}`),
      S.drivers.map((dr) => h("option", { value: dr.id, selected: t.source === "manual" && t.driver === dr.id }, driverName(dr.id))),
      h("option", { value: "none", selected: t.source === "manual" && !t.driver }, "Not a race kart / ignore"));
    const refBtn = t.driver ? h("button", { class: "btn small", disabled: t.is_ref, onclick: async () => {
      await api("POST", `/api/drivers/${t.driver}/ref`, { clip: cid, track: t.id, add: true });
      toast("Added as reference shot"); openClip(cid);
    } }, t.is_ref ? "Reference ✓" : "Use as reference") : null;
    const alts = (t.alts || []).filter((x) => x.id !== t.driver).slice(0, 2).map((x) => `${x.label} ${pct(x.prob)}`).join(", ");
    body.append(h("div", { class: "track" }, t.crop ? h("img", { src: media(t.crop), alt: "" }) : null,
      h("div", { class: "info" },
        h("div", {}, h("strong", {}, t.driver_label || "Unknown"), t.prob ? h("span", { class: "hint" }, `  ${pct(t.prob)}${t.source === "manual" ? " (you)" : ""}`) : null),
        h("div", { class: "hint" }, `Number read: ${t.number_read ? "#" + t.number_read : "none"} · on screen ${t.t0.toFixed(1)} to ${t.t1.toFixed(1)}s · ${pct(t.share)} of kart time`),
        alts ? h("div", { class: "hint" }, `Also possible: ${alts}`) : null),
      h("div", {}, sel, refBtn)));
  }

  const junk = s.junk || {};
  body.append(h("h3", {}, "Clip"), h("div", { class: "row gap" },
    h("button", { class: "btn small", onclick: async () => {
      await api("POST", `/api/clips/${cid}/junk`, { junk: !junk.is_junk }); await loadClips(); renderClips(); openClip(cid);
    } }, junk.is_junk ? "Not junk, restore" : "Mark as junk"),
    junk.override !== null && junk.override !== undefined ? h("button", { class: "btn small ghost", onclick: async () => {
      await api("POST", `/api/clips/${cid}/junk`, { junk: null }); await loadClips(); renderClips(); openClip(cid);
    } }, "Back to automatic") : null,
    h("button", { class: "btn small", onclick: async () => { await api("POST", "/api/hero", { clip: cid }); toast("Colour hero set"); } }, "Use as colour hero"),
    h("button", { class: "btn small ghost", onclick: () => openPath(d.clip.path) }, "Show file")),
    (junk.reasons || []).length ? h("p", { class: "hint" }, `Auto junk reasons: ${junk.reasons.join("; ")}`) : null,
    h("div", { class: "row gap" }, h("span", { class: "hint" }, "Wrong orientation or profile? Re-analyse as:"),
      h("button", { class: "btn small ghost", onclick: () => reanalyse(cid, { transpose: 90 }) }, "Rotated ↻"),
      h("button", { class: "btn small ghost", onclick: () => reanalyse(cid, { transpose: -90 }) }, "Rotated ↺"),
      h("button", { class: "btn small ghost", onclick: () => reanalyse(cid, { transpose: 0 }) }, "Upright"),
      h("select", { onchange: (ev) => reanalyse(cid, { profile: ev.target.value }) }, h("option", { value: "" }, "Profile…"),
        S.status.profiles.map((p) => h("option", { value: p.key }, p.label)))));
  $("#drawer").hidden = false;
}

async function reanalyse(cid, body) {
  await api("POST", `/api/clips/${cid}/reanalyse`, body);
  toast("Re-analysing clip");
  $("#drawer").hidden = true;
}

/* ------------------------------------------------------------------ drivers */
async function loadCandidates() {
  if (!S.status.project) return;
  S.candidates = await api("GET", "/api/drivers/candidates");
}

async function loadGroups() {
  if (!S.status.project) return;
  const g = await api("GET", "/api/groups");
  S.groups = g.groups; S.groupStats = g.stats || {};
}

async function assignGroup(g, driver) {
  const r = await api("POST", "/api/groups/assign", { tracks: g.tracks, driver });
  S.groups = r.groups; S.groupStats = r.stats || {};
  await loadClips(); await loadCandidates(); renderDrivers();
  toast(driver === "none" ? "Hidden as other racer" : `Named ${driverName(driver)}. The rest were re-matched.`);
}

const orientOk = (o) => S.kartOrient === "all" || (S.kartOrient === "vertical" ? o === "vertical" : o !== "vertical");

function setKartOrientCounts(items) {
  const nV = items.filter((o) => o === "vertical").length;
  const counts = { all: items.length, horizontal: items.length - nV, vertical: nV };
  $$("#kartOrient button").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.v === S.kartOrient);
    btn.textContent = `${{ all: "All", horizontal: "Horizontal", vertical: "Vertical" }[btn.dataset.v]} ${counts[btn.dataset.v]}`;
  });
}

function renderGroups() {
  const el = $("#groupList");
  el.replaceChildren();
  if (!S.groups.length) {
    el.append(h("p", { class: "hint" }, S.candidates.length
      ? "No unnamed groups left. Anything still unknown is a one-off: check All karts, or Clips > Needs review."
      : "Kart groups appear here after analysis."));
    return;
  }
  const drivers = S.drivers.slice().sort((a, b) => (+a.number || 0) - (+b.number || 0));
  setKartOrientCounts(S.groups.flatMap((g) => g.orients || g.tracks.map(() => "horizontal")));
  let shown = 0;
  for (const g0 of S.groups) {
    const keep = g0.tracks.map((_, i) => orientOk((g0.orients || [])[i] || "horizontal"));
    if (!keep.some(Boolean)) continue;
    const g = { ...g0, tracks: g0.tracks.filter((_, i) => keep[i]), crops: g0.crops.filter((_, i) => keep[i]) };
    g.clips = new Set(g.tracks.map((t) => t[0])).size;
    shown += 1;
    const reads = (g.numbers || []).filter((n) => n[1] >= 0.5).map((n) => `#${n[0]}`).join(" / ");
    const sel = h("select", { onchange: (e) => { if (e.target.value) assignGroup(g, e.target.value); } },
      h("option", { value: "" }, "This is…"),
      drivers.map((d) => h("option", { value: d.id }, driverName(d.id))),
      h("option", { value: "none" }, "Other racer (hide)"));
    const add = h("button", { class: "btn small ghost", onclick: async () => {
      const name = prompt("Driver name for this group?");
      if (!name) return;
      const num = prompt("Kart number?", (g.numbers && g.numbers[0] && g.numbers[0][0]) || "");
      const d = await api("POST", "/api/drivers", { name, number: num || "", category: "" });
      await loadClips();
      assignGroup(g, d.id);
    } }, "+ New driver");
    el.append(h("div", { class: "group" },
      h("div", { class: "gcrops" }, g.crops.slice(0, 12).map((c) => h("div", { class: "gci" }, h("img", { src: media(c), loading: "lazy", alt: "" })))),
      h("div", { class: "gfoot" }, h("strong", {}, `${g.tracks.length} karts · ${g.clips} clips`),
        reads ? h("span", { class: "num" }, `reads ${reads}`) : null, h("span", { class: "spacer" }), sel, add)));
  }
  if (!shown) el.append(h("p", { class: "hint" }, `No unnamed ${S.kartOrient} groups.`));
}

function renderDrivers() {
  const tbl = $("#driverTable");
  tbl.replaceChildren();
  const counts = {};
  for (const list of Object.values((S.bins && S.bins.categories) || {})) for (const d of list) counts[d.id] = d.count;
  const widths = ["64px", "", "22%", "150px", "48px", "104px", "28px"];
  tbl.append(h("tr", {}, ["#", "Name", "Category", "Refs", "Clips", "Incidents", ""].map((t, i) =>
    h("th", { style: widths[i] ? `width:${widths[i]}` : null }, t))));
  const save = async (d, patch) => { await api("POST", "/api/drivers", { id: d.id, ...patch }); await loadClips(); };
  const sorted = S.drivers.slice().sort((a, b) => (a.category || "").localeCompare(b.category || "") || (+a.number || 0) - (+b.number || 0));
  for (const d of sorted) {
    const refs = h("div", { class: "refs" });
    const refList = d.refs || [];
    for (const r of refList.slice(0, 3)) {
      const c = S.candidates.find((x) => x.clip === r.clip && x.track === r.track);
      if (c) refs.append(h("img", { src: media(c.crop), alt: "", title: c.clip_name }));
    }
    if (refList.length > 3) refs.append(h("span", { class: "more" }, `+${refList.length - 3}`));
    if (!(d.refs || []).length) refs.append(h("span", { class: "hint" }, "none"));
    const inc = h("select", { onclick: (e) => e.stopPropagation(), onchange: (e) => save(d, { include_incidents: e.target.value === "" ? null : e.target.value === "1" }) },
      h("option", { value: "" }, "Default"), h("option", { value: "1", selected: d.include_incidents === true }, "Include"),
      h("option", { value: "0", selected: d.include_incidents === false }, "Leave out"));
    const ph = { number: "No.", name: "Name", category: "Class" };
    const inp = (k, cls = "") => h("input", { class: cls, value: d[k] || "", placeholder: ph[k], onclick: (e) => e.stopPropagation(), onchange: (e) => save(d, { [k]: e.target.value }) });
    tbl.append(h("tr", { class: `clickable${S.selDriver === d.id ? " sel" : ""}`, onclick: () => { S.selDriver = d.id; renderDrivers(); } },
      h("td", { class: "n" }, inp("number", "n")), h("td", {}, inp("name")), h("td", {}, inp("category")),
      h("td", {}, refs), h("td", {}, counts[d.id] ?? 0), h("td", {}, inc),
      h("td", {}, h("button", { class: "link danger", title: "Delete driver", onclick: async (e) => {
        e.stopPropagation(); if (!confirm(`Delete ${d.name}?`)) return;
        await api("DELETE", `/api/drivers/${d.id}`); await loadClips(); renderDrivers();
      } }, "✕"))));
  }
  if (!S.drivers.length) tbl.append(h("tr", {}, h("td", { colspan: 7, class: "hint" }, "No drivers yet. Paste the entry list above.")));

  const sel = S.drivers.find((x) => x.id === S.selDriver);
  const groupsView = S.kartView === "groups";
  $$("#kartView button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.kartView));
  $("#candUnassigned").parentElement.hidden = groupsView;
  $("#groupList").hidden = !groupsView;
  $("#candidates").hidden = groupsView;
  $("#refFor").textContent = groupsView ? `${S.groups.length} unnamed groups`
    : (sel ? `references for ${driverName(sel.id)}` : "(select a driver)");
  $("#kartHint").textContent = groupsView
    ? "Karts that look alike (helmet, suit and kart colours) are grouped. Name a group once and every clip in it is sorted, then KartCut re-matches the rest. Pick Other racer for competitors you don't need. Kart numbers in the table help too."
    : "Select a driver on the left, then click 1 to 3 clear shots of their kart as references.";
  for (const id of ["#candidates", "#groupList"]) {
    $(id).style.setProperty("--cw", `${S.cropSize}px`);
    $(id).classList.toggle("helmet", !!S.helmet);
  }
  if (groupsView) { renderGroups(); return; }
  const grid = $("#candidates");
  grid.replaceChildren();
  const only = $("#candUnassigned").checked;
  const base = S.candidates.filter((c) => !only || (!c.driver && !c.other));
  setKartOrientCounts(base.map((c) => c.orientation || "horizontal"));
  const list = base.filter((c) => orientOk(c.orientation || "horizontal"));
  if (!list.length) grid.append(h("p", { class: "hint" }, "Kart shots appear here after analysis."));
  for (const c of list) {
    const isRef = sel && (sel.refs || []).some((r) => r.clip === c.clip && r.track === c.track);
    const cap = `${c.number_read ? "#" + c.number_read + " read · " : ""}${c.driver ? driverName(c.driver) : "?"}`;
    grid.append(h("div", { class: `crop${isRef ? " ref" : ""}`, title: `${c.clip_name}\n${cap}`, onclick: async () => {
      if (!sel) { toast("Select a driver in the table first"); return; }
      await api("POST", `/api/drivers/${sel.id}/ref`, { clip: c.clip, track: c.track, add: !isRef });
      await loadClips(); await loadCandidates(); renderDrivers();
    } }, h("div", { class: "ci" }, h("img", { src: media(c.crop), loading: "lazy", alt: "" })), h("div", { class: "cap" }, cap)));
  }
}

/* ------------------------------------------------------------------ colour */
async function loadExposure() {
  if (!S.status.project) return;
  S.exposure = await api("GET", "/api/exposure");
}

function renderColour() {
  const ex = S.exposure;
  if (!ex) return;
  const hero = ex.rows.find((r) => r.id === ex.hero);
  const pick = h("select", { onchange: async (e) => { await api("POST", "/api/hero", { clip: e.target.value || null }); await loadExposure(); renderColour(); } },
    h("option", { value: "" }, "Auto-pick the best exposed clip"),
    ex.rows.filter((r) => !r.junk).map((r) => h("option", { value: r.id, selected: S.status.project.hero === r.id }, `${r.name} ${r.driver ? "· " + r.driver : ""}`)));
  $("#hero").replaceChildren(hero && hero.thumb ? h("img", { src: media(hero.thumb), alt: "" }) : h("div", { class: "hint" }, "No hero yet"),
    h("div", { style: "flex:1" }, h("div", {}, h("strong", {}, "Hero: "), hero ? hero.name : "none"),
      h("div", { class: "hint" }, "Every clip is matched to this one."), pick));
  const sm = S.status.project.shot_match;
  if (sm && !$("#matchResult").children.length) {
    $("#matchResult").replaceChildren(h("div", { class: "hint" }, `Last run: ${Object.keys(sm.corrections || {}).length} LUTs.`),
      h("div", { class: "row gap" }, h("button", { class: "btn small", onclick: () => openPath(sm.folder) }, "Open LUT folder"),
        h("button", { class: "btn small ghost", onclick: () => openPath(sm.report, false) }, "Open exposure report (CSV)")));
  }
  const sess = ex.session || {};
  $("#sessionInfo").textContent = `session median ${sgn(sess.median_ev || 0)} st`;
  drawExposureChart(ex.rows.filter((r) => !r.junk));
  const tbl = $("#expTable");
  tbl.replaceChildren(h("tr", {}, ["", "Clip", "Driver", "", "Exposure", "vs session", "Clipped", "WB", "Match", "Issues"].map((t) => h("th", {}, t))));
  const only = $("#expOnlyIssues").checked;
  for (const r of ex.rows) {
    if (r.junk || (only && r.level === "good")) continue;
    const m = r.match;
    tbl.append(h("tr", { class: "clickable", onclick: () => openClip(r.id) },
      h("td", {}, r.thumb ? h("img", { class: "mini", src: media(r.thumb), alt: "" }) : null),
      h("td", {}, r.name), h("td", {}, r.driver), h("td", {}, h("span", { class: `dot ${r.level}` })),
      h("td", {}, `${sgn(r.ev)} st`), h("td", {}, `${sgn(r.ev_rel)} st`),
      h("td", {}, `${r.clip_pct}%${r.subj_clip_pct ? ` (kart ${r.subj_clip_pct}%)` : ""}`),
      h("td", {}, `${r.cct || "?"}K ${sgn(r.cast_rel, 0)}`),
      h("td", {}, m ? `${sgn(m.ev, 2)} st · R${m.gains[0].toFixed(2)} B${m.gains[2].toFixed(2)}` : "-"),
      h("td", { class: "hint" }, (r.issues || []).join("; "))));
  }
}

function drawExposureChart(rows) {
  const el = $("#expChart");
  el.replaceChildren();
  if (!rows.length) return;
  const W = 800, H = 220, pad = 34;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const mk = (tag, attrs, text) => {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    svg.append(e);
    return e;
  };
  const x = (i) => pad + (i / Math.max(1, rows.length - 1)) * (W - pad * 2);
  const y = (ev) => H / 2 - (Math.max(-3, Math.min(3, ev)) / 3) * (H / 2 - 16);
  const yc = (m) => H / 2 - (Math.max(-60, Math.min(60, m)) / 60) * (H / 2 - 16);
  for (const v of [-2, -1, 0, 1, 2]) {
    mk("line", { x1: pad, x2: W - pad, y1: y(v), y2: y(v), stroke: v === 0 ? "#3a424a" : "#23292e", "stroke-width": 1 });
    mk("text", { x: 4, y: y(v) + 3 }, `${v > 0 ? "+" : ""}${v} st`);
  }
  mk("text", { x: W - pad + 4, y: 14 }, "warm");
  mk("text", { x: W - pad + 4, y: H - 6 }, "cool");
  let path = "";
  rows.forEach((r, i) => { path += `${i ? "L" : "M"}${x(i).toFixed(1)},${yc(r.cast_rel || 0).toFixed(1)}`; });
  mk("path", { d: path, fill: "none", stroke: "#4ea1ff", "stroke-width": 1.4, opacity: 0.7 });
  const col = { good: "#3ecf8e", warn: "#f6b400", bad: "#ff5a4f" };
  rows.forEach((r, i) => {
    const c = mk("circle", { cx: x(i), cy: y(r.ev_rel || 0), r: 4, fill: col[r.level] || "#888", style: "cursor:pointer" });
    c.addEventListener("click", () => openClip(r.id));
    const t = document.createElementNS(ns, "title");
    t.textContent = `${r.name}: ${sgn(r.ev_rel)} st, ${sgn(r.cast_rel, 0)} mired`;
    c.append(t);
  });
  el.append(svg, h("div", { class: "legend" }, h("span", {}, h("i", { style: "background:#3ecf8e;height:8px;width:8px;border-radius:50%" }), "exposure vs session (dot per clip, shooting order)"),
    h("span", {}, h("i", { style: "background:#4ea1ff" }), "white balance drift (warm up, cool down)")));
}

/* ------------------------------------------------------------------ montage */
function montageBody() {
  const aspects = [];
  if ($("#mA169").checked) aspects.push("16:9");
  if ($("#mA916").checked) aspects.push("9:16");
  return { event: $("#mEvent").checked, reels: $("#mReels").checked, aspects,
    event_length: +$("#mEventLen").value, reel_length: +$("#mReelLen").value, seed: S.seed };
}

function renderMontage() {
  const mi = $("#musicInfo");
  if (S.music) {
    const m = S.music;
    const bars = h("div", { class: "energy" });
    const n = m.energy.length;
    m.energy.forEach((v, i) => {
      const t = (i / n) * m.duration;
      bars.append(h("i", { class: m.drop && Math.abs(t - m.drop) < m.duration / n ? "d" : "", style: `height:${Math.max(4, v * 100)}%` }));
    });
    mi.replaceChildren(h("div", { class: "kv" }, h("span", {}, "Tempo"), h("span", {}, `${m.tempo} BPM (${m.beats} beats)`),
      h("span", {}, "Drop"), h("span", {}, m.drop ? `${m.drop.toFixed(1)} s, fast cuts start here` : "none found, standard pacing"),
      h("span", {}, "Length"), h("span", {}, fmtDur(m.duration))), bars);
  } else mi.replaceChildren(h("p", { class: "hint" }, "No music: cuts fall on a 120 BPM grid."));
  const outs = $("#outputs");
  outs.replaceChildren();
  const list = ((S.status.project && S.status.project.outputs) || []).slice().reverse();
  if (!list.length) outs.append(h("p", { class: "hint" }, "Renders and exports appear here."));
  for (const o of list.slice(0, 20)) {
    if (o.kind === "render") {
      outs.append(h("div", { class: "out" }, h("div", { class: "row gap" }, h("strong", {}, o.name), h("span", { class: "hint" }, `${fmtDur(o.duration)} · ${o.when}`),
        h("span", { class: "spacer" }), h("button", { class: "btn small", onclick: () => openPath(o.path) }, "Show file")),
        h("video", { src: media(o.url), controls: true, preload: "none" })));
    } else {
      outs.append(h("div", { class: "out" }, h("div", { class: "row gap" }, h("strong", {}, o.name), h("span", { class: "hint" }, o.when),
        h("span", { class: "spacer" }), h("button", { class: "btn small", onclick: () => openPath(o.path) }, "Open folder")),
        h("p", { class: "hint" }, "Resolve: copy the .lua file to %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility, open your project, then Workspace > Scripts. Builds driver bins, markers, LUTs and timelines. Premiere: File > Import the .xml.")));
    }
  }
}

function renderPlans(data) {
  const el = $("#plans");
  el.replaceChildren();
  for (const n of data.notes || []) el.append(h("div", { class: "banner warn" }, n));
  el.append(h("div", { class: "legend" }, ["intro", "build", "climax", "outro"].map((s) =>
    h("span", {}, h("i", { style: `background:var(--sec-${s})` }), s))));
  for (const p of data.plans) {
    const strip = h("div", { class: "strip" });
    for (const c of p.clips) {
      const w = Math.max(18, Math.round((c.frames / p.fps) * 22));
      strip.append(h("div", { class: `shot ${c.section}`, style: `width:${w}px`, title: `${c.name} @ ${c.src_in.toFixed(1)}s\n${c.label}${c.speed !== 1 ? "\nslow motion" : ""}`, onclick: () => openClip(c.clip_id) },
        c.thumb ? h("img", { src: media(c.thumb), alt: "" }) : null, h("span", {}, (c.speed !== 1 ? "◐ " : "") + c.seg_type)));
    }
    el.append(h("div", { class: "plan" }, h("div", { class: "plan-h" }, h("strong", {}, p.name),
      h("span", { class: "hint" }, `${fmtDur(p.duration)} · ${p.clips.length} shots · ${p.fps} fps`)), strip,
      ...(p.notes || []).map((n) => h("div", { class: "hint" }, n))));
  }
  $("#renderBtn").disabled = !data.plans.length;
  $("#exportBtn").disabled = !data.plans.length;
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#projBtn").onclick = openProjects;
  $("#projClose").onclick = () => ($("#projModal").hidden = true);
  $("#projCreate").onclick = async () => {
    await api("POST", "/api/project/new", { name: $("#projName").value, path: $("#projPath").value || null });
    $("#projModal").hidden = true;
    S.drivers = []; S.clips = []; S.plans = null;
    await loadStatus(); showTab("ingest");
  };
  $("#projOpen").onclick = async () => { const p = await openPicker({ title: "Open project folder" }); if (p) openProject(p); };
  $$("[data-pick]").forEach((b) => (b.onclick = async () => {
    const p = await openPicker({ title: "Choose a folder" }); if (p) $(`#${b.dataset.pick}`).value = p;
  }));
  $("#pkClose").onclick = () => pkDone(null);
  $("#pkUp").onclick = () => pkLoad(pk ? pk.parent || "" : "");
  $("#pkGo").onclick = () => pkLoad($("#pkPath").value);
  $("#pkPath").addEventListener("keydown", (e) => { if (e.key === "Enter") pkLoad(e.target.value); });
  $("#pkSelect").onclick = () => { if (pk && pk.path) pkDone(pk.path); };
  $("#dClose").onclick = () => ($("#drawer").hidden = true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#drawer").hidden = true; $("#sortModal").hidden = true; if (pk) pkDone(null); } });

  // ingest
  $("#offAddSrc").onclick = async () => {
    const p = await openPicker({ title: "Pick a card (or its root folder)" });
    if (!p) return;
    const n = S.offSources.length + 1;
    S.offSources.push({ path: p, reel: `Card${String(n).padStart(2, "0")}` });
    renderIngest();
  };
  $("#offStart").onclick = async () => {
    if (!S.offSources.length) return toast("Add at least one card", true);
    const dests = [$("#offDestA").value, $("#offDestB").value].filter(Boolean);
    if (!dests.length) return toast("Pick drive A (and ideally drive B)", true);
    $("#offResult").replaceChildren();
    await api("POST", "/api/offload", { sources: S.offSources, dests, label: $("#offLabel").value, add_to_project: $("#offAdd").checked });
    toast("Offload started");
  };
  $("#addFolder").onclick = async () => {
    const p = await openPicker({ title: "Pick the footage folder" });
    if (p) { await api("POST", "/api/footage/add", { folder: p }); toast("Scanning folder"); }
  };
  $("#analyseNew").onclick = async () => { await api("POST", "/api/analyse", {}); toast("Analysis started"); };
  $("#analyseAll").onclick = async () => {
    if (!confirm("Re-analyse every clip? Manual driver corrections are kept.")) return;
    await api("POST", "/api/analyse", { force: true });
  };
  $("#setProfile").onchange = (e) => saveSetting({ footage_profile: e.target.value });
  $("#setFps").onchange = (e) => saveSetting({ analysis_fps: +e.target.value });
  $("#setProxies").onchange = (e) => saveSetting({ make_proxies: e.target.checked });

  // drivers
  $("#entryImport").onclick = async () => {
    const r = await api("POST", "/api/drivers/import", { text: $("#entryText").value });
    toast(`Imported ${r.imported} drivers`);
    $("#entryText").value = "";
    await loadClips(); renderDrivers();
  };
  $("#driverAdd").onclick = async () => {
    const d = await api("POST", "/api/drivers", { number: "", name: "New driver", category: "" });
    S.selDriver = d.id; await loadClips(); renderDrivers();
  };
  $("#candUnassigned").onchange = renderDrivers;
  $("#cropSize").value = S.cropSize;
  $("#cropSize").oninput = (e) => { S.cropSize = +e.target.value; setPref("cropSize", S.cropSize); renderDrivers(); };
  $$("#kartOrient button").forEach((b) => (b.onclick = () => { S.kartOrient = b.dataset.v; setPref("kartOrient", S.kartOrient); renderDrivers(); }));
  $("#helmetMode").checked = !!S.helmet;
  $("#helmetMode").onchange = (e) => { S.helmet = e.target.checked; setPref("helmet", S.helmet); renderDrivers(); };
  $("#cardSize").value = S.cardSize;
  $("#cardSize").oninput = (e) => { S.cardSize = +e.target.value; setPref("cardSize", S.cardSize); $("#clipGrid").style.setProperty("--card", `${S.cardSize}px`); };
  $$("#kartView button").forEach((b) => (b.onclick = () => { S.kartView = b.dataset.v; renderDrivers(); }));
  $("#rematchBtn").onclick = async () => { await api("POST", "/api/rematch", {}); toast("Re-matching karts"); };

  // clips
  $("#clipSearch").oninput = renderClips;
  $("#clipSort").onchange = renderClips;
  $$("#orientSeg button").forEach((b) => (b.onclick = () => { S.orient = b.dataset.v; setPref("orient", S.orient); renderClips(); }));
  $("#sortDisk").onclick = () => {
    $("#sortDest").value = pref("sortDest", "");
    $("#sortOrient").value = S.orient === "all" ? pref("sortOrient", "all") : S.orient;
    $("#sortMethod").value = pref("sortMethod", "link");
    $("#sortModal").hidden = false;
  };
  $("#sortClose").onclick = () => ($("#sortModal").hidden = true);
  $("#sortBrowse").onclick = async () => {
    const p = await openPicker({ title: "Where should the driver folders go?" });
    if (p) $("#sortDest").value = p;
  };
  $("#sortGo").onclick = async () => {
    const dest = $("#sortDest").value.trim();
    if (!dest) return toast("Pick where the folders should go", true);
    const body = { dest, method: $("#sortMethod").value, orientation: $("#sortOrient").value, include_junk: $("#sortJunk").checked };
    setPref("sortDest", dest); setPref("sortOrient", body.orientation); setPref("sortMethod", body.method);
    await api("POST", "/api/sort", body);
    $("#sortModal").hidden = true;
    toast("Sorting started");
  };

  // colour
  $("#expStr").oninput = (e) => ($("#expStrOut").textContent = pct(+e.target.value));
  $("#wbStr").oninput = (e) => ($("#wbStrOut").textContent = pct(+e.target.value));
  $("#expStr").onchange = (e) => saveSetting({ match_exposure_strength: +e.target.value });
  $("#wbStr").onchange = (e) => saveSetting({ match_wb_strength: +e.target.value });
  $("#runMatch").onclick = async () => { $("#matchResult").replaceChildren(); await api("POST", "/api/shotmatch"); };
  $("#expOnlyIssues").onchange = renderColour;

  // montage
  $("#musicPick").onclick = async () => { const p = await openPicker({ title: "Pick a music track", kind: "audio" }); if (p) $("#musicPath").value = p; };
  $("#lutPick").onclick = async () => {
    const p = await openPicker({ title: "Pick your .cube LUT", kind: "lut" });
    if (p) { $("#mLut").value = p; saveSetting({ custom_lut: p }); }
  };
  $("#musicAnalyse").onclick = async () => {
    const path = $("#musicPath").value.trim();
    if (!path) return toast("Pick a track first", true);
    $("#musicInfo").replaceChildren(h("p", { class: "hint" }, "Listening for beats…"));
    const r = await api("POST", "/api/music", { path, start: +$("#musicStart").value || 0 });
    S.music = r.music; renderMontage();
  };
  $("#mEventLen").onchange = (e) => saveSetting({ event_length: +e.target.value });
  $("#mReelLen").onchange = (e) => saveSetting({ reel_length: +e.target.value });
  const saveAspects = () => saveSetting({ aspects: montageBody().aspects });
  $("#mA169").onchange = saveAspects;
  $("#mA916").onchange = saveAspects;
  $("#mIncidents").onchange = (e) => saveSetting({ include_incidents: e.target.checked });
  $("#mSlow").onchange = (e) => saveSetting({ slow_motion: e.target.checked });
  $("#mEng").oninput = (e) => ($("#mEngOut").textContent = pct(+e.target.value));
  $("#mEng").onchange = (e) => saveSetting({ engine_mix: +e.target.value });
  $("#mLook").onchange = (e) => { saveSetting({ look: e.target.value }); $("#lutRow").hidden = e.target.value !== "custom"; };
  $("#mFps").onchange = (e) => saveSetting({ output_fps: +e.target.value });
  const plan = async () => {
    if (!montageBody().aspects.length) return toast("Tick 16:9 and/or 9:16", true);
    $("#plans").replaceChildren(h("p", { class: "hint" }, "Planning…"));
    try { renderPlans(await api("POST", "/api/montage/plan", montageBody())); }
    catch (_) { $("#plans").replaceChildren(h("p", { class: "hint" }, "Could not plan. See the message above.")); }
  };
  $("#planBtn").onclick = plan;
  $("#shuffleBtn").onclick = () => { S.seed += 1; plan(); };
  $("#renderBtn").onclick = async () => { await api("POST", "/api/montage/render", { render: true, export: true }); toast("Rendering started"); };
  $("#exportBtn").onclick = async () => { await api("POST", "/api/montage/render", { render: false, export: true }); };
}

(async function init() {
  wire();
  await loadStatus();
  let tab = "ingest";
  try { tab = localStorage.getItem("kc_tab") || "ingest"; } catch (_) { /* private mode */ }
  if (S.status.project) await loadClips();
  showTab(tab);
  pollJobs();
})();
