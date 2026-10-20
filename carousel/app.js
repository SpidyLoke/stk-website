'use strict';

// Race Carousel Maker: lays photos out on one long panorama, then slices it into
// Instagram carousel slides so photos can run across the swipe between slides.

const FORMATS = { '4:5': [1080, 1350], '3:4': [1080, 1440], '1:1': [1080, 1080] };
const FULL_EDGE = 2048;   // export source, kept as a JPEG blob to save memory
const SMALL_EDGE = 900;   // preview source, kept as a canvas
const THUMB_EDGE = 220;
const PREVIEW_H = 560;    // editor canvas height in pixels
const MAX_SLIDES = 20;    // Instagram carousel limit
const FEATURE_WEIGHT = 2.6;

const state = {
  photos: [],
  style: 'mosaic',
  format: '4:5',
  slides: 3,
  autoSlides: true,
  seamless: true,
  keepOrder: false,
  radius: 0,
  seed: (Math.random() * 2 ** 32) >>> 0,
  mosaic: { gap: 16, margin: 32, bg: '#0b1230' },
  layered: { bgMode: 'slide', blur: 20, darken: 15, size: 100, border: 0, borderColor: '#ffffff', shadow: true },
  layout: null,
  selected: -1,
};

let nextId = 1;
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const sum = (a) => a.reduce((t, v) => t + v, 0);
const photoById = (id) => state.photos.find((p) => p.id === id);

function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffled(arr, rng) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function stdev(a) {
  if (a.length < 2) return 0;
  const m = sum(a) / a.length;
  return Math.sqrt(sum(a.map((v) => (v - m) ** 2)) / a.length);
}

function dims() {
  const [SW, SH] = FORMATS[state.format];
  const N = state.slides;
  return { SW, SH, N, W: SW * N, H: SH };
}

function autoSlideCount() {
  const per = state.style === 'mosaic' ? 3.5 : 3;
  return clamp(Math.round(state.photos.length / per), 1, MAX_SLIDES);
}

// ---------- image loading ----------

function drawTo(src, w, h) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(src, 0, 0, w, h);
  return c;
}

// Halve in steps before the final resize so big camera files downscale cleanly.
function scaleTo(src, w, h, maxEdge) {
  const s = Math.min(1, maxEdge / Math.max(w, h));
  const tw = Math.max(1, Math.round(w * s)), th = Math.max(1, Math.round(h * s));
  let cur = src, cw = w, ch = h;
  while (cw / 2 > tw) {
    cw = Math.round(cw / 2); ch = Math.round(ch / 2);
    cur = drawTo(cur, cw, ch);
  }
  return drawTo(cur, tw, th);
}

const toBlob = (canvas, type, q) => new Promise((res, rej) =>
  canvas.toBlob((b) => (b ? res(b) : rej(new Error('Could not encode image'))), type, q));

function loadImage(url) {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => rej(new Error('Unsupported image'));
    img.src = url;
  });
}

async function loadPhoto(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = await loadImage(url);
    const w = img.naturalWidth, h = img.naturalHeight;
    const full = scaleTo(img, w, h, FULL_EDGE);
    const fullBlob = await toBlob(full, 'image/jpeg', 0.93);
    const small = scaleTo(full, full.width, full.height, SMALL_EDGE);
    const thumbUrl = URL.createObjectURL(await toBlob(scaleTo(small, small.width, small.height, THUMB_EDGE), 'image/jpeg', 0.8));
    full.width = full.height = 0; // release canvas memory early (iOS keeps a tight canvas budget)
    return { id: nextId++, name: file.name, ar: w / h, small, fullBlob, thumbUrl, star: false, fx: 0.5, fy: 0.5, blur: null };
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function addFiles(files) {
  const list = [...files].filter((f) => f.type.startsWith('image/') || /\.(heic|heif|jpe?g|png|webp|avif)$/i.test(f.name));
  if (!list.length) return;
  const status = $('#loadStatus');
  const failed = [];
  status.hidden = false;
  for (let i = 0; i < list.length; i++) {
    status.textContent = `Loading ${i + 1} of ${list.length}…`;
    try {
      state.photos.push(await loadPhoto(list[i]));
    } catch {
      failed.push(list[i].name);
    }
  }
  if (failed.length) {
    status.textContent = `Couldn't read ${failed.join(', ')}. HEIC only opens in Safari; export those as JPG.`;
  } else {
    status.hidden = true;
  }
  renderThumbs();
  regenerate();
}

// ---------- mosaic layout ----------
// A guillotine tree: every node splits its rectangle vertically or horizontally.
// Many random trees are scored and the best kept. Gap and margin are applied when
// the tree is turned into rectangles, so those sliders don't reshuffle the layout.

function shrink(r, m) {
  return { x: r.x + m, y: r.y + m, w: Math.max(1, r.w - 2 * m), h: Math.max(1, r.h - 2 * m) };
}

function geoMeanAR(ps) {
  return Math.exp(sum(ps.map((p) => Math.log(p.ar))) / ps.length);
}

function buildNode(rect, items, slots, rng) {
  if (items.length === 1) return { leaf: items[0] };
  const ps = items.map((i) => photoById(slots[i].pid));
  const ws = ps.map((p) => (p.star ? FEATURE_WEIGHT : 1));
  const total = sum(ws);
  const target = 0.5 + (rng() - 0.5) * 0.6;
  let k = 1, best = Infinity, acc = 0;
  for (let i = 1; i < items.length; i++) {
    acc += ws[i - 1];
    const d = Math.abs(acc / total - target);
    if (d < best) { best = d; k = i; }
  }
  const ratio = clamp((sum(ws.slice(0, k)) / total) * (1 + (rng() - 0.5) * 0.35), 0.12, 0.88);
  const bias = Math.log(rect.w / rect.h / geoMeanAR(ps));
  const pV = 1 / (1 + Math.exp(-2.5 * bias));
  const dir = rng() < pV ? 'v' : 'h';
  const [ra, rb] = dir === 'v'
    ? [{ ...rect, w: rect.w * ratio }, { ...rect, x: rect.x + rect.w * ratio, w: rect.w * (1 - ratio) }]
    : [{ ...rect, h: rect.h * ratio }, { ...rect, y: rect.y + rect.h * ratio, h: rect.h * (1 - ratio) }];
  return { dir, ratio, a: buildNode(ra, items.slice(0, k), slots, rng), b: buildNode(rb, items.slice(k), slots, rng) };
}

function placeNode(node, r, out, gap, d) {
  if ('leaf' in node) { out[node.leaf] = r; return; }
  if (node.dir === 'v') {
    const usable = r.w - gap;
    let wa = usable * node.ratio;
    if (state.seamless) {
      // A cut that lands just beside a seam looks like a mistake; snap it onto the seam.
      const cut = r.x + wa + gap / 2;
      const s = Math.round(cut / d.SW) * d.SW;
      if (s > 0 && s < d.W && Math.abs(cut - s) < d.SW * 0.07) {
        const w2 = s - gap / 2 - r.x;
        if (w2 > usable * 0.1 && w2 < usable * 0.9) wa = w2;
      }
    }
    placeNode(node.a, { x: r.x, y: r.y, w: wa, h: r.h }, out, gap, d);
    placeNode(node.b, { x: r.x + wa + gap, y: r.y, w: usable - wa, h: r.h }, out, gap, d);
  } else {
    const usable = r.h - gap;
    const ha = usable * node.ratio;
    placeNode(node.a, { x: r.x, y: r.y, w: r.w, h: ha }, out, gap, d);
    placeNode(node.b, { x: r.x, y: r.y + ha + gap, w: r.w, h: usable - ha }, out, gap, d);
  }
}

function mosaicRects(layout, d) {
  const out = new Array(layout.slots.length);
  for (const t of layout.trees) {
    if (t.node) placeNode(t.node, shrink(t.root, state.mosaic.margin), out, state.mosaic.gap, d);
  }
  return out;
}

function chunkSizes(n, parts) {
  const base = Math.floor(n / parts), extra = n % parts;
  return Array.from({ length: parts }, (_, i) => base + (i < extra ? 1 : 0));
}

function scoreMosaic(layout, d) {
  const { SW, SH, N } = d;
  const rects = mosaicRects(layout, d);
  const crossed = new Array(N).fill(false);
  const sizes = [];
  let s = 0;
  rects.forEach((r, i) => {
    const p = photoById(layout.slots[i].pid);
    const ar = r.w / r.h;
    s += Math.abs(Math.log(ar / p.ar)) * 3 * Math.sqrt((r.w * r.h) / (SW * SH));
    if (r.w < SW * 0.2 || r.h < SH * 0.17) s += 4;
    if (Math.abs(Math.log(ar)) > Math.log(3)) s += 3;
    if (r.w > SW * 1.3) s += 2;
    for (let k = 1; k < N; k++) {
      const x = k * SW;
      if (x > r.x + 1 && x < r.x + r.w - 1) {
        const m = Math.min(x - r.x, r.x + r.w - x);
        if (m < SW * 0.2) s += 1 + 5 * (1 - m / (SW * 0.2));
        else crossed[k] = true;
      }
    }
    sizes.push(Math.log((r.w * r.h) / (p.star ? FEATURE_WEIGHT : 1)));
  });
  if (state.seamless) for (let k = 1; k < N; k++) if (!crossed[k]) s += 2;
  return s + stdev(sizes) * 1.2;
}

function genMosaic(d) {
  const { SW, SH, N, W, H } = d;
  const rng = mulberry32(state.seed);
  const ids = state.photos.map((p) => p.id);
  const roots = state.seamless
    ? [{ x: 0, y: 0, w: W, h: H }]
    : Array.from({ length: N }, (_, i) => ({ x: i * SW, y: 0, w: SW, h: SH }));
  const counts = chunkSizes(ids.length, roots.length);
  let best = null;
  for (let c = 0; c < 400; c++) {
    const order = state.keepOrder ? ids : shuffled(ids, rng);
    const slots = order.map((pid) => ({ kind: 'tile', pid }));
    let idx = 0;
    const trees = roots.map((root, ri) => {
      const items = Array.from({ length: counts[ri] }, () => idx++);
      return { root, node: items.length ? buildNode(shrink(root, state.mosaic.margin), items, slots, rng) : null };
    });
    const layout = { type: 'mosaic', slots, trees };
    const score = scoreMosaic(layout, d);
    if (!best || score < best.score) best = { score, layout };
  }
  return best.layout;
}

// ---------- layered layout ----------
// Full-bleed backgrounds (one per slide, or one across the whole carousel) with
// uncropped inset photos on top, some straddling the seams between slides.

function insetRect(slot, d) {
  const p = photoById(slot.pid);
  const { SW, SH, W, H } = d;
  const scale = (state.layered.size / 100) * slot.k;
  let w, h;
  if (p.ar >= 1) { w = 0.62 * SW * scale; h = w / p.ar; } else { h = 0.52 * SH * scale; w = h * p.ar; }
  const fit = Math.min(1, (0.94 * SW) / w, (0.86 * SH) / h);
  w *= fit; h *= fit;
  const m = 0.035 * SW + state.layered.border;
  let minX = m, maxX = W - m - w;
  if (!state.seamless) {
    const si = clamp(Math.floor(slot.cx / SW), 0, d.N - 1);
    minX = si * SW + m; maxX = (si + 1) * SW - m - w;
  }
  const x = maxX < minX ? (minX + maxX) / 2 : clamp(slot.cx - w / 2, minX, maxX);
  const y = clamp(slot.cy - h / 2, m, Math.max(m, H - m - h));
  return { x, y, w, h };
}

function scoreLayered(layout, d) {
  const { SW, SH, N } = d;
  const insets = layout.slots.filter((s) => s.kind === 'inset').map((s) => insetRect(s, d));
  const b = state.layered.border;
  let s = 0;
  const crossed = new Array(N).fill(false);
  const perSlide = new Array(N).fill(0);
  insets.forEach((r, i) => {
    for (let j = i + 1; j < insets.length; j++) {
      const o = insets[j];
      const ox = Math.min(r.x + r.w, o.x + o.w) - Math.max(r.x, o.x) + 2 * b;
      const oy = Math.min(r.y + r.h, o.y + o.h) - Math.max(r.y, o.y) + 2 * b;
      if (ox > 0 && oy > 0) {
        const frac = (ox * oy) / Math.min(r.w * r.h, o.w * o.h);
        s += frac > 0.03 ? 14 * frac + 1.5 : 0;
      }
    }
    for (let k = 1; k < N; k++) {
      const x = k * SW;
      if (x > r.x && x < r.x + r.w) {
        const m = Math.min(x - r.x, r.x + r.w - x);
        if (m < Math.max(0.25 * r.w, 0.12 * SW)) s += 4;
        else crossed[k] = true;
      }
    }
    perSlide[clamp(Math.floor((r.x + r.w / 2) / SW), 0, N - 1)]++;
    s += Math.abs((r.y + r.h / 2) / SH - 0.5) < 0.08 ? 0.5 : 0; // dead-centre insets hide the background
  });
  if (state.seamless && insets.length) for (let k = 1; k < N; k++) if (!crossed[k]) s += 1.5;
  if (insets.length >= N) s += perSlide.filter((c) => c === 0).length * 1.5;
  return s;
}

function genLayered(d) {
  const { SW, SH, N, W, H } = d;
  const rng = mulberry32(state.seed);
  const ids = state.photos.map((p) => p.id);
  const span = state.layered.bgMode === 'span';
  const nb = span ? 1 : N;
  let best = null;
  for (let c = 0; c < 600; c++) {
    const order = state.keepOrder ? ids : shuffled(ids, rng);
    const pool = order.filter((id) => photoById(id).star).concat(order.filter((id) => !photoById(id).star));
    const bgIds = Array.from({ length: nb }, (_, i) => pool[i % pool.length]);
    const used = new Set(bgIds);
    const insetIds = order.filter((id) => !used.has(id));
    const slots = bgIds.map((pid, i) => ({
      kind: 'bg', pid, rect: span ? { x: 0, y: 0, w: W, h: H } : { x: i * SW, y: 0, w: SW, h: SH },
    }));
    const k = insetIds.length;
    const density = clamp(1.25 - 0.22 * (k / N), 0.7, 1.05); // more insets per slide, smaller insets
    const flip = rng() < 0.5;
    const perSlide = chunkSizes(k, N);
    insetIds.forEach((pid, j) => {
      let cx;
      if (state.seamless) {
        const step = W / k;
        cx = (j + 0.5) * step + (rng() - 0.5) * step * 0.9;
      } else {
        let si = 0, q = j;
        while (q >= perSlide[si]) q -= perSlide[si++];
        const step = SW / perSlide[si];
        cx = si * SW + (q + 0.5) * step + (rng() - 0.5) * step * 0.3;
      }
      const top = (j % 2 === 0) !== flip;
      const cy = (top ? 0.3 : 0.7) * H + (rng() - 0.5) * 0.16 * H;
      slots.push({ kind: 'inset', pid, cx, cy, k: density * (0.82 + rng() * 0.3) });
    });
    const layout = { type: 'layered', slots };
    const score = scoreLayered(layout, d);
    if (!best || score < best.score) best = { score, layout };
  }
  // Store the clamped positions so dragging starts from where the inset is drawn.
  for (const s of best.layout.slots) {
    if (s.kind !== 'inset') continue;
    const r = insetRect(s, d);
    s.cx = r.x + r.w / 2; s.cy = r.y + r.h / 2;
  }
  return best.layout;
}

// ---------- drawing ----------

function slotRects(d) {
  const L = state.layout;
  if (!L) return [];
  if (L.type === 'mosaic') return mosaicRects(L, d);
  return L.slots.map((s) => (s.kind === 'bg' ? s.rect : insetRect(s, d)));
}

function roundRectPath(ctx, x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

// Crop fractions: how much of the source width/height a cover-fit into `r` uses.
function coverFrac(p, r) {
  const ar = r.w / r.h;
  return p.ar > ar ? { cw: ar / p.ar, ch: 1 } : { cw: 1, ch: p.ar / ar };
}

function drawCover(ctx, src, p, r, radius) {
  const { cw, ch } = coverFrac(p, r);
  const sw = src.width * cw, sh = src.height * ch;
  const sx = (src.width - sw) * p.fx, sy = (src.height - sh) * p.fy;
  if (radius > 0) {
    ctx.save();
    roundRectPath(ctx, r.x, r.y, r.w, r.h, radius);
    ctx.clip();
    ctx.drawImage(src, sx, sy, sw, sh, r.x, r.y, r.w, r.h);
    ctx.restore();
  } else {
    ctx.drawImage(src, sx, sy, sw, sh, r.x, r.y, r.w, r.h);
  }
}

let filterBlur = null;
function supportsFilterBlur() {
  if (filterBlur !== null) return filterBlur;
  try {
    const c = document.createElement('canvas');
    c.width = c.height = 12;
    const ctx = c.getContext('2d');
    ctx.filter = 'blur(2px)';
    ctx.fillStyle = '#fff';
    ctx.fillRect(5, 5, 2, 2);
    filterBlur = ctx.getImageData(2, 6, 1, 1).data[3] > 0;
  } catch { filterBlur = false; }
  return filterBlur;
}

function blurredSource(p) {
  const amt = state.layered.blur;
  if (p.blur && p.blur.amt === amt) return p.blur.canvas;
  const src = p.small;
  let out;
  if (supportsFilterBlur()) {
    const r = (amt / 100) * Math.max(src.width, src.height) * 0.04;
    out = document.createElement('canvas');
    out.width = src.width; out.height = src.height;
    const ctx = out.getContext('2d');
    ctx.filter = `blur(${r}px)`;
    const pad = r * 2.5; // overscan so the blur doesn't pull transparent edges in
    ctx.drawImage(src, -pad, -pad, src.width + pad * 2, src.height + pad * 2);
  } else {
    // Fallback: shrink and let the upscale soften it.
    const edge = Math.max(16, Math.round(SMALL_EDGE * Math.pow(0.03, amt / 100)));
    out = scaleTo(src, src.width, src.height, edge);
  }
  p.blur = { amt, canvas: out };
  return out;
}

const intersects = (r, x0, x1) => r.x < x1 && r.x + r.w > x0;

// Draws the panorama region [viewX, viewX + viewW] into ctx at `scale`.
// `hi` maps photo id to a full-resolution bitmap (export); otherwise previews are used.
function drawScene(ctx, scale, viewX, viewW, hi) {
  const d = dims();
  const L = state.layout;
  const x1 = viewX + viewW;
  const src = (p) => (hi && hi.get(p.id)) || p.small;
  ctx.setTransform(scale, 0, 0, scale, -viewX * scale, 0);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  const rects = slotRects(d);

  if (L.type === 'mosaic') {
    ctx.fillStyle = state.mosaic.bg;
    ctx.fillRect(viewX, 0, viewW, d.H);
    L.slots.forEach((s, i) => {
      if (intersects(rects[i], viewX, x1)) drawCover(ctx, src(photoById(s.pid)), photoById(s.pid), rects[i], state.radius);
    });
    return;
  }

  const { blur, darken, border, borderColor, shadow } = state.layered;
  ctx.fillStyle = '#000';
  ctx.fillRect(viewX, 0, viewW, d.H);
  L.slots.forEach((s, i) => {
    if (s.kind !== 'bg' || !intersects(rects[i], viewX, x1)) return;
    const p = photoById(s.pid);
    drawCover(ctx, blur > 0 ? blurredSource(p) : src(p), p, rects[i], 0);
    if (darken > 0) {
      ctx.fillStyle = `rgba(0,0,0,${darken / 100})`;
      ctx.fillRect(rects[i].x, rects[i].y, rects[i].w, rects[i].h);
    }
  });
  L.slots.forEach((s, i) => {
    const r = rects[i];
    if (s.kind !== 'inset' || !intersects({ x: r.x - 80, w: r.w + 160 }, viewX, x1)) return;
    const p = photoById(s.pid);
    const outer = { x: r.x - border, y: r.y - border, w: r.w + border * 2, h: r.h + border * 2 };
    const rad = state.radius;
    if (shadow || border > 0) {
      ctx.save();
      if (shadow) {
        // Shadow sizes ignore the transform, so scale them by hand.
        ctx.shadowColor = 'rgba(0,0,0,0.55)';
        ctx.shadowBlur = 48 * scale;
        ctx.shadowOffsetY = 14 * scale;
      }
      ctx.fillStyle = border > 0 ? borderColor : '#000';
      roundRectPath(ctx, outer.x, outer.y, outer.w, outer.h, rad > 0 ? rad + border : 0);
      ctx.fill();
      ctx.restore();
    }
    drawCover(ctx, src(p), p, r, rad);
  });
}

// ---------- editor + preview ----------

const editor = $('#editor');
const ectx = editor.getContext('2d');
let phoneCanvases = [];
let raf = 0;

function render() {
  if (!raf) raf = requestAnimationFrame(doRender);
}

function doRender() {
  raf = 0;
  const d = dims();
  const ps = PREVIEW_H / d.H;
  const cw = Math.round(d.W * ps), ch = PREVIEW_H;
  if (editor.width !== cw || editor.height !== ch) { editor.width = cw; editor.height = ch; }
  ectx.setTransform(1, 0, 0, 1, 0, 0);
  ectx.clearRect(0, 0, cw, ch);
  if (state.layout) {
    drawScene(ectx, ps, 0, d.W, null);
  } else {
    ectx.setTransform(ps, 0, 0, ps, 0, 0);
    ectx.fillStyle = '#10141c';
    ectx.fillRect(0, 0, d.W, d.H);
  }

  // Phone preview gets a clean copy before the editor overlays go on.
  phoneCanvases.forEach((c, i) => {
    const sw = Math.round(d.SW * ps);
    if (c.width !== sw || c.height !== ch) { c.width = sw; c.height = ch; }
    const x = c.getContext('2d');
    x.clearRect(0, 0, sw, ch);
    x.drawImage(editor, Math.round(i * d.SW * ps), 0, sw, ch, 0, 0, sw, ch);
  });

  drawOverlays(d, ps);
}

function drawOverlays(d, ps) {
  ectx.setTransform(1, 0, 0, 1, 0, 0);
  ectx.save();
  ectx.setLineDash([10, 8]);
  ectx.lineWidth = 2;
  ectx.strokeStyle = 'rgba(255,255,255,0.75)';
  for (let k = 1; k < d.N; k++) {
    const x = Math.round(k * d.SW * ps) + 0.5;
    ectx.beginPath(); ectx.moveTo(x, 0); ectx.lineTo(x, PREVIEW_H); ectx.stroke();
  }
  ectx.restore();
  ectx.font = '600 14px Inter, system-ui, sans-serif';
  ectx.textBaseline = 'middle';
  for (let k = 0; k < d.N; k++) {
    const x = k * d.SW * ps + 10;
    const label = String(k + 1);
    const w = Math.max(24, ectx.measureText(label).width + 14);
    ectx.fillStyle = 'rgba(0,0,0,0.6)';
    roundRectPath(ectx, x, 10, w, 24, 12);
    ectx.fill();
    ectx.fillStyle = '#fff';
    ectx.textAlign = 'center';
    ectx.fillText(label, x + w / 2, 22);
  }
  if (!state.layout) {
    ectx.fillStyle = '#5d667a';
    ectx.textAlign = 'center';
    ectx.font = 'italic 700 28px "Barlow Condensed", sans-serif';
    ectx.fillText('ADD PHOTOS TO BUILD YOUR CAROUSEL', Math.min(editor.width, 900) / 2, PREVIEW_H / 2);
    return;
  }
  if (state.selected >= 0) {
    const r = slotRects(d)[state.selected];
    ectx.lineWidth = 4;
    ectx.strokeStyle = '#ffc400';
    ectx.strokeRect(r.x * ps + 2, r.y * ps + 2, r.w * ps - 4, r.h * ps - 4);
  }
}

function rebuildPhone(d) {
  const box = $('#phoneSlides');
  const dots = $('#dots');
  if (phoneCanvases.length !== d.N) {
    box.innerHTML = '';
    dots.innerHTML = '';
    phoneCanvases = Array.from({ length: d.N }, () => box.appendChild(document.createElement('canvas')));
    for (let i = 0; i < d.N; i++) dots.appendChild(document.createElement('i'));
  }
  phoneCanvases.forEach((c) => { c.style.aspectRatio = `${d.SW} / ${d.SH}`; });
  updateDots();
}

function currentPhoneSlide() {
  const box = $('#phoneSlides');
  return clamp(Math.round(box.scrollLeft / Math.max(1, box.clientWidth)), 0, phoneCanvases.length - 1);
}

function updateDots() {
  const i = currentPhoneSlide();
  $$('#dots i').forEach((el, j) => el.classList.toggle('on', i === j));
}

function regenerate() {
  state.selected = -1;
  if (state.autoSlides && state.photos.length) state.slides = autoSlideCount();
  const d = dims();
  state.layout = state.photos.length ? (state.style === 'mosaic' ? genMosaic(d) : genLayered(d)) : null;
  rebuildPhone(d);
  syncUI();
  render();
}

// ---------- editor interaction ----------

function toScene(e) {
  const r = editor.getBoundingClientRect();
  const d = dims();
  return { x: ((e.clientX - r.left) / r.width) * d.W, y: ((e.clientY - r.top) / r.height) * d.H, k: d.W / r.width };
}

function hitTest(pt) {
  const rects = slotRects(dims());
  for (let i = rects.length - 1; i >= 0; i--) {
    const r = rects[i];
    if (pt.x >= r.x && pt.x <= r.x + r.w && pt.y >= r.y && pt.y <= r.y + r.h) return i;
  }
  return -1;
}

let drag = null;

editor.addEventListener('pointerdown', (e) => {
  if (!state.layout) return;
  const pt = toScene(e);
  const hit = hitTest(pt);
  if (hit < 0) { drag = { hit, tapOnly: true }; return; }
  // On touch, only a selected photo can be dragged, so swiping still scrolls the strip.
  if (e.pointerType !== 'mouse' && hit !== state.selected) { drag = { hit, tapOnly: true }; return; }
  editor.setPointerCapture(e.pointerId);
  const slot = state.layout.slots[hit];
  const p = photoById(slot.pid);
  drag = { hit, sx: e.clientX, sy: e.clientY, k: pt.k, moved: false, fx: p.fx, fy: p.fy, cx: slot.cx, cy: slot.cy };
});

editor.addEventListener('pointermove', (e) => {
  if (!drag || drag.tapOnly) return;
  const dxc = e.clientX - drag.sx, dyc = e.clientY - drag.sy;
  if (!drag.moved && Math.hypot(dxc, dyc) < 5) return;
  drag.moved = true;
  const dx = dxc * drag.k, dy = dyc * drag.k;
  const slot = state.layout.slots[drag.hit];
  const p = photoById(slot.pid);
  if (slot.kind === 'inset') {
    slot.cx = drag.cx + dx;
    slot.cy = drag.cy + dy;
  } else {
    const r = slotRects(dims())[drag.hit];
    const { cw, ch } = coverFrac(p, r);
    if (cw < 0.999) p.fx = clamp(drag.fx - ((dx / r.w) * cw) / (1 - cw), 0, 1);
    if (ch < 0.999) p.fy = clamp(drag.fy - ((dy / r.h) * ch) / (1 - ch), 0, 1);
  }
  render();
});

editor.addEventListener('pointerup', () => {
  if (!drag) return;
  const { hit, moved } = drag;
  drag = null;
  if (moved) {
    const slot = state.layout.slots[hit];
    if (slot.kind === 'inset') { // keep the stored centre in sync with the clamped position
      const r = insetRect(slot, dims());
      slot.cx = r.x + r.w / 2; slot.cy = r.y + r.h / 2;
    }
    return;
  }
  if (hit < 0) state.selected = -1;
  else if (state.selected < 0) state.selected = hit;
  else if (state.selected === hit) state.selected = -1;
  else {
    const s = state.layout.slots;
    [s[state.selected].pid, s[hit].pid] = [s[hit].pid, s[state.selected].pid];
    state.selected = -1;
  }
  syncSelection();
  render();
});

editor.addEventListener('pointercancel', () => { drag = null; });

function syncSelection() {
  $('#selbar').hidden = state.selected < 0;
  editor.classList.toggle('locked', state.selected >= 0);
}

$('#resetFrame').addEventListener('click', () => {
  if (state.selected < 0) return;
  const p = photoById(state.layout.slots[state.selected].pid);
  p.fx = p.fy = 0.5;
  render();
});
$('#deselect').addEventListener('click', () => { state.selected = -1; syncSelection(); render(); });

// ---------- sidebar ----------

function renderThumbs() {
  const box = $('#thumbs');
  box.innerHTML = '';
  for (const p of state.photos) {
    const el = document.createElement('div');
    el.className = 'thumb';
    el.innerHTML = `<img alt=""><button class="star${p.star ? ' on' : ''}" type="button" title="Feature photo">★</button><button class="del" type="button" title="Remove">✕</button>`;
    el.querySelector('img').src = p.thumbUrl;
    el.querySelector('img').alt = p.name;
    el.querySelector('.star').addEventListener('click', () => { p.star = !p.star; renderThumbs(); regenerate(); });
    el.querySelector('.del').addEventListener('click', () => {
      URL.revokeObjectURL(p.thumbUrl);
      state.photos = state.photos.filter((q) => q !== p);
      renderThumbs();
      regenerate();
    });
    box.appendChild(el);
  }
  const n = state.photos.length;
  $('#photoMeta').hidden = !n;
  $('#photoCount').textContent = `${n} photo${n === 1 ? '' : 's'}`;
}

const getPath = (path) => path.split('.').reduce((o, k) => o[k], state);
function setPath(path, v) {
  const keys = path.split('.');
  const last = keys.pop();
  keys.reduce((o, k) => o[k], state)[last] = v;
}

const REGEN_PATHS = new Set(['style', 'format', 'layered.bgMode']);

function syncUI() {
  $$('.seg[data-path]').forEach((seg) => {
    const v = getPath(seg.dataset.path);
    seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === v));
  });
  $$('input[data-path]').forEach((el) => {
    const v = getPath(el.dataset.path);
    if (el.type === 'checkbox') el.checked = v;
    else el.value = v;
    const out = el.parentElement.querySelector('output');
    if (out && el.type === 'range') out.textContent = `${v}${el.dataset.unit || ''}`;
  });
  $('#slides').value = state.slides;
  $('#slidesOut').textContent = state.autoSlides ? `${state.slides} (auto)` : state.slides;
  $$('[data-show]').forEach((el) => { el.hidden = el.dataset.show !== state.style; });
  $('#editorHint').textContent = state.style === 'mosaic'
    ? 'Tap two photos to swap them. Drag a photo to reframe it.'
    : 'Tap two photos to swap them. Drag an inset to move it, or a background to reframe it.';
  syncSelection();

  const n = state.photos.length;
  let note = '';
  if (n && state.style === 'mosaic' && n < state.slides) note = 'Fewer photos than slides, so tiles get stretched. Add photos or drop the slide count.';
  if (n && state.style === 'layered' && state.layered.bgMode === 'slide' && n < state.slides) note = 'Fewer photos than slides, so backgrounds repeat.';
  if (n && state.style === 'layered' && state.layered.bgMode === 'slide' && n === state.slides) note = 'Every photo is a background. Add photos or drop the slide count to get insets.';
  $('#note').hidden = !note;
  $('#note').textContent = note;

  ['#dlZip', '#dlOne', '#share'].forEach((s) => { $(s).disabled = !n || exporting; });
}

$$('.seg[data-path]').forEach((seg) => {
  seg.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    setPath(seg.dataset.path, b.dataset.v);
    regenerate();
  });
});

$$('input[data-path]').forEach((el) => {
  el.addEventListener('input', () => {
    const v = el.type === 'checkbox' ? el.checked : el.type === 'range' ? Number(el.value) : el.value;
    setPath(el.dataset.path, v);
    if ('regen' in el.dataset || REGEN_PATHS.has(el.dataset.path)) { regenerate(); return; }
    syncUI();
    render();
  });
});

$('#slides').addEventListener('input', (e) => {
  state.slides = Number(e.target.value);
  state.autoSlides = false;
  regenerate();
});

$('#shuffle').addEventListener('click', () => {
  state.seed = (Math.random() * 2 ** 32) >>> 0;
  regenerate();
});

$('#clearAll').addEventListener('click', () => {
  state.photos.forEach((p) => URL.revokeObjectURL(p.thumbUrl));
  state.photos = [];
  renderThumbs();
  regenerate();
});

const fileInput = $('#file');
fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });

const drop = $('#drop');
['dragenter', 'dragover'].forEach((t) => document.addEventListener(t, (e) => {
  if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
  e.preventDefault();
  drop.classList.add('over');
}));
['dragleave', 'drop'].forEach((t) => document.addEventListener(t, (e) => {
  if (t === 'dragleave' && e.relatedTarget) return;
  drop.classList.remove('over');
}));
document.addEventListener('drop', (e) => {
  if (!e.dataTransfer?.files?.length) return;
  e.preventDefault();
  addFiles(e.dataTransfer.files);
});

$('#phoneSlides').addEventListener('scroll', updateDots, { passive: true });

// ---------- export ----------

let exporting = false;

async function decodeFull(p) {
  if ('createImageBitmap' in window) return createImageBitmap(p.fullBlob);
  const url = URL.createObjectURL(p.fullBlob);
  try { return await loadImage(url); } finally { URL.revokeObjectURL(url); }
}

function photosInSlide(i, d) {
  const rects = slotRects(d);
  const ids = new Set();
  const blurredBg = state.layout.type === 'layered' && state.layered.blur > 0;
  state.layout.slots.forEach((s, j) => {
    if (blurredBg && s.kind === 'bg') return; // blurred backgrounds come from the preview copy
    if (intersects(rects[j], i * d.SW, (i + 1) * d.SW)) ids.add(s.pid);
  });
  return ids;
}

// Renders the requested slides at full size. Bitmaps are decoded on demand and
// dropped as soon as no later slide needs them, to stay inside phone memory limits.
async function renderSlides(indices, progress) {
  const d = dims();
  const cache = new Map();
  const blobs = [];
  for (let n = 0; n < indices.length; n++) {
    const i = indices[n];
    progress?.(n + 1, indices.length);
    const need = photosInSlide(i, d);
    for (const id of need) if (!cache.has(id)) cache.set(id, await decodeFull(photoById(id)));
    const c = document.createElement('canvas');
    c.width = d.SW; c.height = d.SH;
    drawScene(c.getContext('2d'), 1, i * d.SW, d.SW, cache);
    blobs.push(await toBlob(c, 'image/jpeg', 0.92));
    c.width = c.height = 0;
    const next = n + 1 < indices.length ? photosInSlide(indices[n + 1], d) : new Set();
    for (const [id, bmp] of cache) if (!next.has(id)) { bmp.close?.(); cache.delete(id); }
  }
  return blobs;
}

const slideName = (i) => `carousel-${String(i + 1).padStart(2, '0')}.jpg`;

function download(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
}

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(data) {
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++) c = CRC_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// Minimal store-only ZIP; JPEGs don't compress further, so no deflate needed.
async function makeZip(files) {
  const now = new Date();
  const time = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
  const date = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  const enc = new TextEncoder();
  const parts = [], central = [];
  let offset = 0;
  for (const f of files) {
    const data = new Uint8Array(await f.blob.arrayBuffer());
    const name = enc.encode(f.name);
    const crc = crc32(data);
    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true);
    lh.setUint16(4, 20, true);
    lh.setUint16(10, time, true);
    lh.setUint16(12, date, true);
    lh.setUint32(14, crc, true);
    lh.setUint32(18, data.length, true);
    lh.setUint32(22, data.length, true);
    lh.setUint16(26, name.length, true);
    parts.push(lh, name, data);
    const ch = new DataView(new ArrayBuffer(46));
    ch.setUint32(0, 0x02014b50, true);
    ch.setUint16(4, 20, true);
    ch.setUint16(6, 20, true);
    ch.setUint16(12, time, true);
    ch.setUint16(14, date, true);
    ch.setUint32(16, crc, true);
    ch.setUint32(20, data.length, true);
    ch.setUint32(24, data.length, true);
    ch.setUint16(28, name.length, true);
    ch.setUint32(42, offset, true);
    central.push(ch, name);
    offset += 30 + name.length + data.length;
  }
  const cdSize = sum(central.map((p) => p.byteLength));
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, cdSize, true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, end], { type: 'application/zip' });
}

async function runExport(label, job) {
  if (exporting || !state.layout) return;
  exporting = true;
  syncUI();
  const status = $('#exportStatus');
  status.hidden = false;
  try {
    await job((n, total) => { status.textContent = `${label} slide ${n} of ${total}…`; });
    status.textContent = 'Done.';
    setTimeout(() => { if (!exporting) status.hidden = true; }, 2500);
  } catch (err) {
    if (err?.name !== 'AbortError') status.textContent = `Export failed: ${err?.message || err}`;
    else status.hidden = true;
  } finally {
    exporting = false;
    syncUI();
  }
}

const allSlides = () => Array.from({ length: state.slides }, (_, i) => i);

$('#dlZip').addEventListener('click', () => runExport('Rendering', async (progress) => {
  const blobs = await renderSlides(allSlides(), progress);
  download(await makeZip(blobs.map((blob, i) => ({ name: slideName(i), blob }))), 'carousel.zip');
}));

$('#dlOne').addEventListener('click', () => runExport('Rendering', async (progress) => {
  const i = currentPhoneSlide();
  const [blob] = await renderSlides([i], progress);
  download(blob, slideName(i));
}));

$('#share').addEventListener('click', () => runExport('Rendering', async (progress) => {
  const blobs = await renderSlides(allSlides(), progress);
  const files = blobs.map((b, i) => new File([b], slideName(i), { type: 'image/jpeg' }));
  await navigator.share({ files });
}));

(function initShare() {
  try {
    const probe = new File([new Uint8Array([0xff, 0xd8, 0xff])], 'probe.jpg', { type: 'image/jpeg' });
    $('#share').hidden = !(navigator.canShare && navigator.canShare({ files: [probe] }));
  } catch { /* no Web Share */ }
})();

regenerate();
document.fonts?.ready.then(render);
