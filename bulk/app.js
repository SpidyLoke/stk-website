'use strict';
(() => {
  /* =========================================================
     Bulk Clock: meal timer for a small-meals-often bulk.
     Everything lives in localStorage on the phone.
     ========================================================= */
  const KEY = 'bulkclock.v1';
  const ROLL_H = 4;            // a "day" runs 4am to 4am, so a 1am meal counts for the day before
  const DAY = 864e5;
  const ALERT_WINDOW = 15 * 60e3;

  const $ = (s, r = document) => r.querySelector(s);
  const pad = n => String(n).padStart(2, '0');
  const uid = () => Math.random().toString(36).slice(2, 9);
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmtK = n => Math.round(n).toLocaleString('en-GB');
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  const FOODS = [
    { n: 'Banana PB shake', d: '2 banana, 2 tbsp PB, 400ml full cream milk, honey', k: 730 },
    { n: 'Mass gainer shake', d: '1 serving + 300ml full cream milk (check your tub)', k: 650 },
    { n: 'Nasi lemak ayam goreng', k: 750 },
    { n: 'Nasi lemak biasa', d: 'bungkus', k: 450 },
    { n: 'Roti canai x2 + dhal', k: 650 },
    { n: 'Roti telur + dhal', k: 450 },
    { n: 'Mee goreng mamak', k: 650 },
    { n: 'Nasi goreng kampung', k: 650 },
    { n: 'Nasi ayam', k: 600 },
    { n: 'Maggi goreng + telur', k: 600 },
    { n: 'Oats + milk + honey', d: '60g oats, 250ml full cream milk', k: 450 },
    { n: 'Kaya butter toast x2', k: 350 },
    { n: 'PB toast x2', d: 'Gardenia + 2 tbsp peanut butter', k: 350 },
    { n: 'Half-boiled eggs x2', k: 140 },
    { n: 'Milo kaw', d: 'with condensed milk', k: 250 },
    { n: 'Teh tarik', k: 200 },
    { n: 'Full cream milk 500ml', k: 330 },
    { n: 'Nuts, handful 30g', k: 180 },
    { n: 'Banana', k: 105 },
    { n: 'Karipap', k: 200 },
    { n: 'Olive oil 1 tbsp', d: 'stir into rice or a shake', k: 120 },
  ];

  /* ---------- dates ---------- */
  const ymd = d => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  const parseYmd = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const dayKeyOf = ts => ymd(new Date(ts - ROLL_H * 3600e3));
  const todayKey = () => dayKeyOf(Date.now());
  const addDays = (key, n) => { const d = parseYmd(key); d.setDate(d.getDate() + n); return ymd(d); };
  const daysBetween = (a, b) => Math.round((parseYmd(b) - parseYmd(a)) / DAY);
  const shortDate = key => parseYmd(key).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const toMin = s => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
  const hhmm = m => { m = ((m % 1440) + 1440) % 1440; return pad(Math.floor(m / 60)) + ':' + pad(m % 60); };
  const clock12 = (m, parts) => {
    m = ((m % 1440) + 1440) % 1440;
    let h = Math.floor(m / 60); const ap = h >= 12 ? 'pm' : 'am';
    h = h % 12 || 12;
    const t = h + ':' + pad(m % 60);
    return parts ? [t, ap] : t + ' ' + ap;
  };

  function autoTimes(wake, bed, n) {
    const w = toMin(wake); let b = toMin(bed);
    if (b <= w) b += 1440;
    const start = w + 30, end = b - 60;
    if (n <= 1 || end <= start) return [start];
    const step = (end - start) / (n - 1);
    return Array.from({ length: n }, (_, i) => Math.round((start + step * i) / 5) * 5);
  }

  /* ---------- state ---------- */
  function defaults() {
    return {
      v: 2,
      startWeight: 53, goalWeight: 60, startDate: '2026-11-25', deadline: '2026-12-12',
      wake: '08:00', bed: '00:00', meals: 7, kcalPerMeal: 500,
      times: autoTimes('08:00', '00:00', 7),
      nudgeMin: 20, sound: true,
      foods: FOODS.map(f => ({ id: uid(), u: 0, ...f })),
      log: {}, weights: [{ d: '2026-11-25', kg: 53 }],
      notified: {}, onboardDone: false, calendarDone: false,
      aiKey: '', aiModel: '', aiRecent: [],
      pushUrl: '', pushOn: false, pushId: '', vapid: null, pushErr: '', calendarOff: false,
      age: 18, height: 170, sex: 'm', activity: 'light',
    };
  }
  let S;
  try { S = Object.assign(defaults(), JSON.parse(localStorage.getItem(KEY) || '{}')); }
  catch { S = defaults(); }
  // v2: the old iPhone build ticked the calendar step without adding anything, so clear it once
  if ((S.v || 1) < 2) { S.calendarDone = false; S.v = 2; }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch { /* storage blocked */ } queuePushSync(); };

  let tab = 'today';
  let sheetMeal = null;
  let lastSig = '';

  /* ---------- schedule maths ---------- */
  const dailyTarget = () => S.times.length * S.kcalPerMeal;
  const mealTs = (key, i) => { const d = parseYmd(key); return new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, S.times[i]).getTime(); };
  const entries = key => S.log[key] || [];
  const mealKcal = (key, i) => entries(key).filter(e => e.m === i).reduce((a, e) => a + e.k, 0);
  const dayKcal = key => entries(key).reduce((a, e) => a + e.k, 0);

  function statuses(key, now = Date.now()) {
    const n = S.times.length;
    return S.times.map((_, i) => {
      const ts = mealTs(key, i);
      const next = i < n - 1 ? mealTs(key, i + 1) : ts + 3 * 3600e3;
      const kcal = mealKcal(key, i);
      let st = 'upcoming';
      if (kcal >= S.kcalPerMeal * 0.5) st = 'done';
      else if (now >= next) st = 'missed';
      else if (now >= ts) st = 'due';
      return { i, ts, next, kcal, st };
    });
  }

  function heroState(now = Date.now()) {
    const key = dayKeyOf(now);
    const list = statuses(key, now);
    const due = list.find(m => m.st === 'due');
    if (due) return { kind: 'due', key, m: due, list };
    const up = list.find(m => m.st === 'upcoming' && m.ts > now);
    if (up) return { kind: 'next', key, m: up, list };
    const tk = addDays(key, 1);
    return { kind: 'tomorrow', key, m: { i: 0, ts: mealTs(tk, 0) }, list };
  }

  function defaultLogMeal() {
    const h = heroState();
    if (h.kind === 'due' || h.kind === 'next') return h.m.i;
    return S.times.length - 1;
  }

  function suggest(target, seed = 0) {
    const pool = [...S.foods].sort((a, b) => Math.abs(a.k - target) - Math.abs(b.k - target));
    const favs = S.foods.filter(f => f.u > 0).sort((a, b) => b.u - a.u).slice(0, 4);
    const opts = (favs.length ? favs : pool.slice(0, 4));
    const pick = opts[seed % opts.length];
    if (!pick) return '';
    if (pick.k >= target * 0.8) return `${pick.n} (${pick.k})`;
    const gap = target - pick.k;
    const add = [...S.foods].filter(f => f.id !== pick.id).sort((a, b) => Math.abs(a.k - gap) - Math.abs(b.k - gap))[0];
    return add ? `${pick.n} + ${add.n} (${pick.k + add.k})` : `${pick.n} (${pick.k})`;
  }

  /* ---------- weight maths ---------- */
  const sortedWeights = () => [...S.weights].sort((a, b) => a.d.localeCompare(b.d));
  function pace() {
    const ws = sortedWeights();
    const latest = ws.length ? ws[ws.length - 1].kg : S.startWeight;
    const tk = todayKey();
    const total = Math.max(1, daysBetween(S.startDate, S.deadline));
    const elapsed = clamp(daysBetween(S.startDate, tk), 0, total);
    const daysLeft = Math.max(0, daysBetween(tk, S.deadline));
    const targetToday = S.startWeight + (S.goalWeight - S.startWeight) * (elapsed / total);
    const toGo = S.goalWeight - latest;
    return { latest, daysLeft, targetToday, toGo, perDay: toGo / Math.max(1, daysLeft), diff: latest - targetToday, gained: latest - S.startWeight };
  }

  /* ---------- notifications ---------- */
  let swReg = null;
  if ('serviceWorker' in navigator) {
    // updateViaCache 'none' + update() on every reopen = new versions land without waiting out GitHub's cache
    navigator.serviceWorker.register('sw.js', { updateViaCache: 'none' }).then(r => { swReg = r; r.update().catch(() => {}); }).catch(() => {});
    let hadController = !!navigator.serviceWorker.controller, reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (hadController && !reloaded && sheetMeal === null) { reloaded = true; location.reload(); }
      hadController = true;
    });
  }
  const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const notifSupported = () => 'Notification' in window;
  const notifState = () => notifSupported() ? Notification.permission : 'unsupported';

  async function askNotif() {
    if (!notifSupported()) {
      toast(isIOS() && !isStandalone() ? 'Add to Home Screen first, then open from the icon' : 'This browser has no notifications');
      return;
    }
    const p = await Notification.requestPermission();
    if (p === 'granted') { toast('Alerts on'); fire('Bulk Clock is on', 'You will get a ping at every meal time while the app is open or recently used.', 'test'); }
    else toast('Alerts blocked. Turn them on in phone settings.');
    render();
  }

  async function fire(title, body, tag) {
    const opts = { body, tag, renotify: true, requireInteraction: true, vibrate: [300, 120, 300, 120, 600], icon: 'icons/icon-192.png', badge: 'icons/icon-192.png', data: { url: location.href } };
    if (notifState() === 'granted') {
      try {
        const reg = swReg || (await navigator.serviceWorker?.ready);
        if (reg) await reg.showNotification(title, opts); else new Notification(title, opts);
      } catch { try { new Notification(title, opts); } catch { /* ignore */ } }
    }
    if (navigator.vibrate) navigator.vibrate(opts.vibrate);
  }

  let audioCtx = null;
  function beep() {
    if (!S.sound) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const t = audioCtx.currentTime;
      [0, 0.22, 0.44].forEach((o, j) => {
        const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
        osc.frequency.value = j === 2 ? 1320 : 880; osc.type = 'square';
        g.gain.setValueAtTime(0.0001, t + o); g.gain.exponentialRampToValueAtTime(0.25, t + o + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + o + 0.18);
        osc.connect(g).connect(audioCtx.destination); osc.start(t + o); osc.stop(t + o + 0.2);
      });
    } catch { /* no audio */ }
  }

  function showAlarm(i, nudge) {
    const el = $('#alarm');
    const n = S.times.length;
    el.innerHTML = `
      <div style="font:700 13px var(--body);letter-spacing:.14em">${nudge ? 'YOU SKIPPED IT' : 'EAT NOW'}</div>
      <h2>Meal ${i + 1}<br>of ${n}</h2>
      <p>~${S.kcalPerMeal} kcal. ${esc(suggest(S.kcalPerMeal, i))}</p>
      <button class="btn" data-act="alarm-log" data-meal="${i}">Log it</button>
      <button class="btn ghost" data-act="alarm-close">Snooze, I'm on it</button>`;
    el.hidden = false;
    beep();
  }

  function checkAlerts() {
    const now = Date.now();
    const key = dayKeyOf(now);
    const nmap = S.notified[key] || (S.notified[key] = {});
    let changed = false;
    statuses(key, now).forEach(m => {
      if (m.st === 'done') return;
      const n = S.times.length;
      if (now >= m.ts && now < m.ts + ALERT_WINDOW && !nmap['m' + m.i]) {
        nmap['m' + m.i] = 1; changed = true;
        if (!S.pushOn) fire(`Eat now: meal ${m.i + 1} of ${n}`, `~${S.kcalPerMeal} kcal. Idea: ${suggest(S.kcalPerMeal, m.i)}`, 'meal');
        if (document.visibilityState === 'visible') showAlarm(m.i, false);
      }
      const nudgeAt = m.ts + S.nudgeMin * 60e3;
      if (S.nudgeMin > 0 && now >= nudgeAt && now < nudgeAt + ALERT_WINDOW && now < m.next && !nmap['n' + m.i]) {
        nmap['n' + m.i] = 1; changed = true;
        const behind = Math.max(0, (m.i + 1) * S.kcalPerMeal - dayKcal(key));
        if (!S.pushOn) fire(`Meal ${m.i + 1} still not logged`, `You're ${fmtK(behind)} kcal behind today. Eat something now, even a shake.`, 'meal');
        if (document.visibilityState === 'visible') showAlarm(m.i, true);
      }
    });
    // keep the notified map small
    Object.keys(S.notified).forEach(k => { if (k < addDays(key, -3)) { delete S.notified[k]; changed = true; } });
    if (changed) save();
  }

  /* ---------- real push, sent by the user's Cloudflare Worker (push-worker.js) ---------- */
  var pushTimer = null, pushLastSent = '', pushHealth = null, workerCode = '';
  const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const b64uDec = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), c => c.charCodeAt(0));
  function pushBase() { return (S.pushUrl || '').trim().replace(/\/+$/, ''); }
  fetch('push-worker.js').then(r => r.ok ? r.text() : '').then(t => { workerCode = t; }).catch(() => {});

  function doneMap() {
    const tk = todayKey(), out = {};
    [addDays(tk, -1), tk].forEach(k => { out[k] = statuses(k).filter(m => m.st === 'done').map(m => m.i); });
    return out;
  }
  function pushPayload() {
    const ideas = [...S.foods]
      .sort((a, b) => (b.u || 0) - (a.u || 0) || Math.abs(a.k - S.kcalPerMeal) - Math.abs(b.k - S.kcalPerMeal))
      .slice(0, 6).map(f => `${f.n} (${f.k})`);
    return { times: S.times, tz: -new Date().getTimezoneOffset(), kcal: S.kcalPerMeal, nudge: S.nudgeMin, deadline: S.deadline, ideas, done: doneMap() };
  }
  async function pushPost(path, extra) {
    let r;
    try { r = await fetch(pushBase() + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: S.pushId, ...extra }) }); }
    catch { throw new Error("Can't reach your Worker. Check the URL and your signal."); }
    let j = {}; try { j = await r.json(); } catch { /* not JSON */ }
    if (!r.ok || j.ok === false) { const e = new Error(j.error || (j.detail ? `Push service said ${j.status}: ${j.detail}` : `Worker error ${r.status}`)); e.status = r.status; throw e; }
    return j;
  }
  async function checkPushHealth() {
    if (!pushBase()) { pushHealth = null; return null; }
    try {
      const r = await fetch(pushBase(), { cache: 'no-store' });
      const j = await r.json();
      pushHealth = j.app === 'bulk-clock-push' ? { ok: j.ok, lastCron: j.lastCron, error: j.error } : { ok: false, error: "That URL isn't a Bulk Clock Worker." };
    } catch { pushHealth = { ok: false, error: "Can't reach that URL. Check it, and that you tapped Deploy." }; }
    return pushHealth;
  }
  function queuePushSync() {
    if (!S.pushOn || !S.pushId || !pushBase()) return;
    clearTimeout(pushTimer);
    pushTimer = setTimeout(() => syncPush(false), 1500);
  }
  async function syncPush(force) {
    const body = pushPayload(), sig = JSON.stringify(body);
    if (!force && sig === pushLastSent) return;
    try { await pushPost('/sync', body); pushLastSent = sig; S.pushErr = ''; }
    catch (e) {
      if (e.status === 404) { try { await pushSubscribe(); } catch (e2) { S.pushErr = e2.message; } }
      else S.pushErr = e.message;
    }
  }
  async function pushSubscribe() {
    if (!pushBase()) throw new Error('Paste your Worker URL first.');
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      throw new Error(isIOS() && !isStandalone() ? 'Open Bulk Clock from the home-screen icon first, then connect.' : "This browser can't receive push.");
    }
    if (Notification.permission !== 'granted') {
      const p = await Notification.requestPermission();
      if (p !== 'granted') throw new Error('Notifications are blocked. Turn them on in iPhone Settings → Notifications → Bulk Clock.');
    }
    const reg = swReg || await navigator.serviceWorker.ready;
    if (!S.vapid) {
      const kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign', 'verify']);
      S.vapid = { publicKey: b64u(await crypto.subtle.exportKey('raw', kp.publicKey)), privateJwk: await crypto.subtle.exportKey('jwk', kp.privateKey) };
    }
    if (!S.pushId) S.pushId = [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');
    let sub = await reg.pushManager.getSubscription();
    const subKey = sub && sub.options && sub.options.applicationServerKey;
    if (sub && subKey && b64u(subKey) !== S.vapid.publicKey) { await sub.unsubscribe(); sub = null; }
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uDec(S.vapid.publicKey) });
    const body = pushPayload();
    await pushPost('/subscribe', { sub: sub.toJSON(), vapid: S.vapid, ...body });
    pushLastSent = JSON.stringify(body);
    S.pushOn = true; S.pushErr = '';
    save();
  }
  async function pushConnect() {
    const el = $('#pushUrl'); if (el) S.pushUrl = el.value.trim();
    if (!/^https:\/\/[^ ]+$/.test(pushBase())) { toast('Paste the full Worker URL, starting https://'); return; }
    save();
    toast('Connecting…');
    const h = await checkPushHealth();
    if (!h || !h.ok) { render(); toast(h ? h.error : 'No URL'); return; }
    try {
      await pushSubscribe();
      await pushPost('/test', {});
      toast('Connected. A test notification is on its way.');
    } catch (e) { S.pushErr = e.message; toast(e.message); }
    render();
  }
  async function pushOff() {
    try { await pushPost('/unsubscribe', {}); } catch { /* server may already be gone */ }
    try { const reg = swReg || await navigator.serviceWorker.ready; const sub = await reg.pushManager.getSubscription(); if (sub) await sub.unsubscribe(); } catch { /* ignore */ }
    S.pushOn = false; S.pushErr = ''; save(); render(); toast('Push turned off');
  }
  // On open: make sure iOS hasn't dropped the subscription, and refresh the server's copy.
  async function pushOnOpen() {
    if (!S.pushOn) return;
    try {
      const reg = swReg || await navigator.serviceWorker.ready;
      if (!(await reg.pushManager.getSubscription())) { await pushSubscribe(); }
      else await syncPush(true);
    } catch (e) { S.pushErr = e.message; }
    if (tab === 'setup') render();
  }

  /* ---------- forecast ---------- */
  const ACT = { light: 1.375, moderate: 1.55, hard: 1.725 };
  const KCAL_PER_KG = 7700;
  const round50 = n => Math.round(n / 50) * 50;
  function trendWeight() { // latest weigh-in, so the forecast moves the moment you log a new weight
    const ws = sortedWeights();
    return ws.length ? ws[ws.length - 1].kg : S.startWeight;
  }
  const bmr = kg => 10 * kg + 6.25 * S.height - 5 * S.age + (S.sex === 'f' ? -161 : 5);
  // Formula maintenance, blended with what your own logs + weigh-ins imply once there are 10+ days of data.
  function maintenance() {
    const formula = Math.round(bmr(trendWeight()) * (ACT[S.activity] || ACT.light));
    const ws = sortedWeights().filter(w => w.d >= S.startDate);
    if (ws.length >= 2) {
      const a = ws[0], b = ws[ws.length - 1], span = daysBetween(a.d, b.d);
      if (span >= 10) {
        const ks = Array.from({ length: span }, (_, j) => addDays(a.d, j)).filter(k => entries(k).length);
        if (ks.length >= span * 0.7) {
          const avg = ks.reduce((s, k) => s + dayKcal(k), 0) / ks.length;
          const water = a.d === S.startDate ? 1.5 : 0;
          const est = avg - (b.kg - a.kg - water) * KCAL_PER_KG / span;
          return { kcal: Math.round(clamp((est + formula) / 2, formula * 0.8, formula * 1.25)), src: 'data', formula };
        }
      }
    }
    return { kcal: formula, src: 'formula', formula };
  }
  function avgIntake() {
    const tk = todayKey();
    const ks = Array.from({ length: 7 }, (_, j) => addDays(tk, -(j + 1))).filter(k => k >= S.startDate && entries(k).length);
    if (!ks.length) return null;
    return { kcal: Math.round(ks.reduce((s, k) => s + dayKcal(k), 0) / ks.length), days: ks.length };
  }
  function forecast() {
    const tk = todayKey();
    const daysLeft = Math.max(0, daysBetween(tk, S.deadline)); // eating days before the deadline morning
    const trend = trendWeight();
    const m = maintenance();
    const sinceStart = daysBetween(S.startDate, tk);
    // first ~10 days of eating big: ~1.5 kg of water + stored carbs on top of tissue
    const waterLeft = sinceStart < 10 ? clamp(1.5 - (trend - S.startWeight), 0, 1.5) : 0;
    const proj = kcal => trend + (kcal >= m.kcal + 500 ? waterLeft : 0) + (kcal - m.kcal) * daysLeft / KCAL_PER_KG;
    const intake = avgIntake();
    const paceKcal = intake ? intake.kcal : dailyTarget();
    const gap = S.goalWeight - trend - waterLeft;
    const needGoal = daysLeft > 0 && gap > 0 ? m.kcal + gap * KCAL_PER_KG / daysLeft : null;
    const eatenToday = dayKcal(tk);
    return {
      daysLeft, trend, m, intake, paceKcal, needGoal, proj,
      atPace: proj(paceKcal),
      todayLeft: Math.max(0, dailyTarget() - eatenToday),
      totalLeft: Math.max(0, dailyTarget() * daysLeft - eatenToday),
    };
  }
  function forecastCard() {
    const f = forecast();
    if (f.daysLeft === 0) return `<div class="card"><h3>Forecast</h3><p class="hint" style="margin:0">Deadline reached. Set a new one in Setup to keep forecasting.</p></div>`;
    const plan = dailyTarget();
    const opts = [...new Set([3000, 3500, 4000, 4500, plan, round50(f.paceKcal)])].sort((a, b) => a - b);
    const rows = opts.map(k => {
      const kg = f.proj(k);
      const tags = [k === plan ? 'your plan' : '', k === round50(f.paceKcal) && f.intake ? 'your pace' : ''].filter(Boolean).join(' · ');
      return `<div class="fc-row ${k === plan ? 'on' : ''} ${kg >= S.goalWeight ? 'hit' : ''}"><span>${fmtK(k)} kcal/day${tags ? ` <small>${tags}</small>` : ''}</span><b>${kg.toFixed(1)} kg</b></div>`;
    }).join('');
    const need = f.needGoal
      ? `<b>${S.goalWeight} kg needs about ${fmtK(round50(f.needGoal))} kcal a day</b> from today${f.needGoal > plan ? `, ${fmtK(round50(f.needGoal - plan))} more than your plan` : ''}.`
      : `You're on track for ${S.goalWeight} kg. Keep eating to plan.`;
    return `
      <div class="card fc">
        <h3>Forecast for ${shortDate(S.deadline)} <span class="r">${f.daysLeft} eating days left</span></h3>
        <div class="fc-big">${f.atPace.toFixed(1)}<small> kg</small></div>
        <p class="hint" style="margin-top:2px">${f.intake ? `At what you've actually eaten: <b>${fmtK(f.intake.kcal)} kcal/day</b> (average of the last ${f.intake.days} logged day${f.intake.days > 1 ? 's' : ''}).` : `If you hit your plan of <b>${fmtK(plan)} kcal/day</b>. Log food to forecast from what you really eat.`}</p>
        <p class="fc-need">${need}</p>
        <div class="fc-rows">${rows}</div>
        <div class="stats" style="margin-top:12px">
          <div class="stat"><div class="k">Left today</div><div class="v">${fmtK(f.todayLeft)}</div></div>
          <div class="stat"><div class="k">Left to ${shortDate(S.deadline)}</div><div class="v">${f.totalLeft >= 10000 ? (f.totalLeft / 1000).toFixed(1) + '<small>k</small>' : fmtK(f.totalLeft)}</div></div>
          <div class="stat"><div class="k">Maintenance</div><div class="v">${fmtK(f.m.kcal)}</div></div>
        </div>
        <p class="hint">Based on your latest weigh-in (${f.trend.toFixed(1)} kg). Maintenance is ${f.m.src === 'data' ? 'worked out from your own logs and weigh-ins' : `estimated from your age, height, weight and activity (set in Setup); it switches to your real data after 10 days of logging`}. Assumes 7,700 kcal per kg${f.daysLeft && daysBetween(S.startDate, todayKey()) < 10 ? ', plus ~1.5 kg of water and stored carbs in the first week of eating big' : ''}.</p>
      </div>`;
  }

  /* ---------- calendar export (fires with the app closed) ---------- */
  function icsLocal(d) { return d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + 'T' + pad(d.getHours()) + pad(d.getMinutes()) + '00'; }
  function buildIcs() {
    const n = S.times.length;
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+/, '');
    const seq = Math.floor(Date.now() / 1000) % 100000;
    const until = S.deadline.replace(/-/g, '') + 'T235900';
    const startKey = todayKey();
    const L = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Bulk Clock//EN', 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Bulk Clock'];
    S.times.forEach((_, i) => {
      const s = new Date(mealTs(startKey, i));
      const e = new Date(s.getTime() + 20 * 60e3);
      L.push('BEGIN:VEVENT', `UID:bulkclock-meal-${i + 1}@bulkclock.app`, `SEQUENCE:${seq}`, 'DTSTAMP:' + stamp,
        'DTSTART:' + icsLocal(s), 'DTEND:' + icsLocal(e), 'RRULE:FREQ=DAILY;UNTIL=' + until,
        `SUMMARY:Eat: meal ${i + 1} of ${n} (${S.kcalPerMeal} kcal)`,
        `DESCRIPTION:Bulk Clock. ${S.kcalPerMeal} kcal now. Log it in the app.`,
        'TRANSP:TRANSPARENT',
        'BEGIN:VALARM', 'ACTION:DISPLAY', `DESCRIPTION:Eat now: meal ${i + 1}`, 'TRIGGER:PT0M', 'END:VALARM',
        'END:VEVENT');
    });
    L.push('END:VCALENDAR');
    return L.join('\r\n') + '\r\n';
  }
  // iPhone: the home-screen app can't hand a calendar file to iOS, so open a page outside
  // the app's scope. iOS shows it in Safari, which does the "Add All" calendar import.
  const calUrl = () => `../bulk-cal/?t=${S.times.join(',')}&k=${S.kcalPerMeal}&u=${S.deadline}`;
  function icsButton(cls, label) {
    if (isIOS()) return `<a class="${cls}" href="${calUrl()}" target="_blank" rel="noopener" data-act="ics-ios">${label}</a>`;
    return `<button class="${cls}" data-act="ics">${label}</button>`;
  }
  function exportIcs() {
    download(new Blob([buildIcs()], { type: 'text/calendar' }), 'bulk-clock-meals.ics');
    S.calendarDone = true; save();
    toast('Saved. Open the file to add it to your calendar.');
    render();
  }
  function download(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
  }

  /* ---------- toast ---------- */
  let toastTimer;
  function toast(msg, undo) {
    const el = $('#toast');
    el.innerHTML = esc(msg) + (undo ? ' <button data-act="undo">Undo</button>' : '');
    el._undo = undo || null;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), undo ? 4500 : 2600);
  }

  /* ---------- logging ---------- */
  function addEntry(m, name, k, foodId) {
    const key = todayKey();
    const e = { id: uid(), m, n: name, k: Math.round(k), t: Date.now() };
    (S.log[key] = S.log[key] || []).push(e);
    if (foodId) { const f = S.foods.find(x => x.id === foodId); if (f) f.u = (f.u || 0) + 1; }
    save();
    toast(`+${e.k} kcal to meal ${m + 1}`, () => removeEntry(key, e.id, foodId));
    renderSheet(); render();
  }
  function removeEntry(key, id, foodId) {
    S.log[key] = entries(key).filter(e => e.id !== id);
    if (foodId) { const f = S.foods.find(x => x.id === foodId); if (f && f.u > 0) f.u--; }
    save(); renderSheet(); render();
  }

  /* =========================================================
     Rendering
     ========================================================= */
  function render() {
    const p = pace();
    $('#topMeta').innerHTML = `<b>${p.latest.toFixed(1)}</b> → ${S.goalWeight} kg · <b>${p.daysLeft}</b> days left`;
    if (tab === 'today') renderToday();
    if (tab === 'weight') renderWeight();
    if (tab === 'setup') renderSetup();
    if (tab === 'history') renderHistory();
    lastSig = sig();
  }
  const sig = () => { const h = heroState(); return h.kind + h.m.i + h.list.map(m => m.st).join(''); };

  function countdown(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h > 0 ? `${h}:${pad(m)}:${pad(x)}` : `${pad(m)}:${pad(x)}`;
  }

  function inShort(ms) {
    const mins = Math.max(1, Math.ceil(ms / 60e3));
    const h = Math.floor(mins / 60), m = mins % 60;
    return h ? `${h}h ${pad(m)}m` : `${m}m`;
  }

  function onboardCard() {
    if (S.onboardDone) return '';
    const ns = notifState();
    const ios = isIOS(), sa = isStandalone();
    const step1 = ios
      ? `<li class="${sa ? 'ok' : ''}"><div><b>Add to Home Screen</b><p>iPhone only sends web app alerts from the home-screen icon. Safari → Share → Add to Home Screen, then open it from there.</p></div></li>`
      : `<li class="${sa ? 'ok' : ''}"><div><b>Install the app</b><p>Chrome menu (⋮) → Add to Home screen / Install app. Then open it from the icon.</p></div></li>`;
    return `
    <div class="card onboard">
      <h3>Set up alerts <span class="r"><button class="btn sm ghost" data-act="onboard-done">Hide</button></span></h3>
      <ol class="steps">
        ${step1}
        <li class="${ns === 'granted' ? 'ok' : ''}"><div><b>Allow notifications</b><p>Pings at every meal time and a nudge if you skip one, while the app is open or recently used.</p>
          ${ns === 'granted' ? '' : '<button class="btn sm primary" data-act="notif">Allow notifications</button>'}</div></li>
        ${S.calendarOff
          ? `<li class="${S.pushOn ? 'ok' : ''}"><div><b>Turn on push notifications</b><p>Alerts at every meal with the app closed, sent by your own free Cloudflare Worker.</p>
            ${S.pushOn ? '' : '<button class="btn sm primary" data-act="go-push">Set up push</button>'}</div></li>`
          : `<li class="${S.calendarDone ? 'ok' : ''}"><div><b>Add meal alarms to your calendar</b><p>Your phone's calendar rings at every meal even with this app closed. Re-add it if you change meal times.</p>
            ${icsButton('btn sm', `Add ${S.times.length} daily alarms`)}</div></li>`}
      </ol>
    </div>`;
  }

  function renderToday() {
    const now = Date.now();
    const h = heroState(now);
    const key = h.key;
    const n = S.times.length;
    const total = dayKcal(key);
    const target = dailyTarget();
    const expected = h.list.filter(m => m.ts <= now).length * S.kcalPerMeal;
    const pct = clamp(total / target * 100, 0, 100);

    let eyebrow, sub, btn;
    if (h.kind === 'due') {
      eyebrow = 'EAT NOW';
      const late = Math.floor((now - h.m.ts) / 60e3);
      sub = `Meal <b>${h.m.i + 1}</b> of ${n} · due ${clock12(S.times[h.m.i])}${late > 0 ? ` · ${late} min ago` : ''}`;
      btn = `<button class="btn primary" data-act="log" data-meal="${h.m.i}">Log what I ate</button>`;
    } else if (h.kind === 'next') {
      eyebrow = `NEXT MEAL IN`;
      sub = `Meal <b>${h.m.i + 1}</b> of ${n} at <b>${clock12(S.times[h.m.i])}</b> · ~${S.kcalPerMeal} kcal`;
      btn = `<button class="btn primary" data-act="log" data-meal="${defaultLogMeal()}">Log food</button>`;
    } else {
      eyebrow = 'DONE FOR TODAY · FIRST MEAL IN';
      sub = `Tomorrow, meal 1 at <b>${clock12(S.times[0])}</b>`;
      btn = `<button class="btn" data-act="log" data-meal="${n - 1}">Log a late snack</button>`;
    }
    const big = h.kind === 'due' ? `${S.kcalPerMeal}<span style="font-size:32px"> kcal</span>` : countdown(h.m.ts - now);
    const idea = h.kind !== 'tomorrow' ? `<div class="idea">Idea: <b>${esc(suggest(S.kcalPerMeal, h.m.i))}</b></div>` : '';

    const meals = h.list.map(m => {
      const [t, ap] = clock12(S.times[m.i], true);
      const names = entries(key).filter(e => e.m === m.i).map(e => e.n).join(', ');
      const chip = { done: `${fmtK(m.kcal)} kcal`, due: 'Now', missed: 'Missed', upcoming: `in ${inShort(m.ts - now)}` }[m.st];
      return `<li><button class="meal ${m.st}" data-act="log" data-meal="${m.i}">
        <span class="t">${t}<small>${ap}</small></span>
        <span class="n">Meal ${m.i + 1}<small>${names ? esc(names) : m.st === 'missed' ? 'Tap to log it anyway' : '~' + S.kcalPerMeal + ' kcal'}</small></span>
        <span class="chip">${chip}</span></button></li>`;
    }).join('');

    const extra = entries(key).filter(e => e.m === -1);
    const extraRow = extra.length ? `<p class="hint">Extras: <b>${fmtK(extra.reduce((a, e) => a + e.k, 0))} kcal</b> (${esc(extra.map(e => e.n).join(', '))})</p>` : '';

    // last 7 days
    const tk = todayKey();
    const week = Array.from({ length: 7 }, (_, j) => addDays(tk, j - 6)).map(k => {
      const kc = dayKcal(k); const r = clamp(kc / target, 0, 1);
      const lbl = parseYmd(k).toLocaleDateString('en-GB', { weekday: 'narrow' });
      return `<div class="d ${k === tk ? 'today' : ''}"><div class="col ${r < 0.9 && k !== tk ? 'low' : ''}"><i style="height:${r * 100}%"></i></div><span class="lbl">${lbl}</span></div>`;
    }).join('');
    const hitDays = Array.from({ length: 7 }, (_, j) => addDays(tk, j - 7)).filter(k => dayKcal(k) >= target * 0.9).length;

    const p = pace();
    $('#view-today').innerHTML = `
      ${onboardCard()}
      <div class="hero ${h.kind === 'due' ? 'due' : ''}">
        <div class="eyebrow">${eyebrow}</div>
        <div class="big" id="cd">${big}</div>
        <div class="sub">${sub}</div>
        ${idea}
        ${btn}
      </div>

      <div class="card">
        <div class="kcal-top"><div class="num">${fmtK(total)}<small> / ${fmtK(target)} kcal</small></div><div class="pct">${Math.round(pct)}%</div></div>
        <div class="bar"><i style="width:${pct}%"></i><span class="mark" style="left:${clamp(expected / target * 100, 0, 100)}%"></span></div>
        <p class="hint">${total >= expected ? `On schedule. The line marks where you should be by now (<b>${fmtK(expected)}</b>).` : `<b>${fmtK(expected - total)} kcal behind</b> schedule. The line marks where you should be by now.`}</p>
      </div>

      <div class="card">
        <h3>Today's meals <span class="r">${h.list.filter(m => m.st === 'done').length}/${n} done</span></h3>
        <ol class="meals">${meals}</ol>
        ${extraRow}
      </div>

      <div class="card">
        <h3>Last 7 days <span class="r">${hitDays}/7 days hit target</span></h3>
        <div class="week">${week}</div>
      </div>

      <div class="card">
        <h3>Pace</h3>
        <div class="stats">
          <div class="stat ${p.gained > 0 ? 'good' : ''}"><div class="k">Gained</div><div class="v">${p.gained >= 0 ? '+' : ''}${p.gained.toFixed(1)}<small>kg</small></div></div>
          <div class="stat"><div class="k">To go</div><div class="v">${Math.max(0, p.toGo).toFixed(1)}<small>kg</small></div></div>
          <div class="stat ${p.diff < -0.3 ? 'bad' : 'good'}"><div class="k">vs plan</div><div class="v">${p.diff >= 0 ? '+' : ''}${p.diff.toFixed(1)}<small>kg</small></div></div>
        </div>
        ${(() => { const f = forecast(); return f.daysLeft ? `<p class="hint">Forecast: <b>${f.atPace.toFixed(1)} kg</b> on ${shortDate(S.deadline)} at your pace.${f.needGoal ? ` ${S.goalWeight} kg needs ~${fmtK(round50(f.needGoal))} kcal/day.` : ''} <b>${fmtK(f.todayLeft)}</b> kcal left today.</p>` : ''; })()}
        <div class="row-btns"><button class="btn sm" data-act="tab" data-tab="weight">Weigh in &amp; full forecast</button></div>
      </div>`;
  }

  function chartSvg() {
    const W = 340, H = 200, P = { l: 30, r: 34, t: 14, b: 24 };
    const t0 = parseYmd(S.startDate).getTime(), t1 = parseYmd(S.deadline).getTime();
    const ws = sortedWeights();
    const vals = [S.startWeight, S.goalWeight, ...ws.map(w => w.kg)];
    const lo = Math.floor(Math.min(...vals) - 0.5), hi = Math.ceil(Math.max(...vals) + 0.5);
    const x = t => P.l + clamp((t - t0) / ((t1 - t0) || 1), 0, 1) * (W - P.l - P.r);
    const y = kg => P.t + (hi - kg) / ((hi - lo) || 1) * (H - P.t - P.b);
    const step = hi - lo > 8 ? 2 : 1;
    let grid = '';
    for (let v = lo; v <= hi; v += step) grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y(v)}" y2="${y(v)}"/><text class="ax" x="${P.l - 6}" y="${y(v) + 3}" text-anchor="end">${v}</text>`;
    const tToday = parseYmd(todayKey()).getTime();
    const pts = ws.map(w => [x(parseYmd(w.d).getTime()), y(w.kg)]);
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Weight against plan">
      <g class="grid">${grid}</g>
      <line class="today" x1="${x(tToday)}" x2="${x(tToday)}" y1="${P.t}" y2="${H - P.b}"/>
      <line class="target" x1="${x(t0)}" y1="${y(S.startWeight)}" x2="${x(t1)}" y2="${y(S.goalWeight)}"/>
      <text class="goal" x="${x(t1) + 4}" y="${y(S.goalWeight) + 4}">${S.goalWeight}</text>
      ${pts.length > 1 ? `<polyline class="actual" points="${pts.map(p => p.join(',')).join(' ')}"/>` : ''}
      ${pts.map(p => `<circle class="pt" cx="${p[0]}" cy="${p[1]}" r="3.5"/>`).join('')}
      <text class="ax" x="${P.l}" y="${H - 6}">${shortDate(S.startDate)}</text>
      <text class="ax" x="${W - P.r}" y="${H - 6}" text-anchor="end">${shortDate(S.deadline)}</text>
    </svg>`;
  }

  function renderWeight() {
    const p = pace();
    const tk = todayKey();
    const ws = sortedWeights();
    const todays = ws.find(w => w.d === tk);
    const list = [...ws].reverse().map(w => {
      const prev = ws[ws.indexOf(w) - 1];
      const d = prev ? w.kg - prev.kg : 0;
      return `<li><div class="grow"><b>${w.kg.toFixed(1)} kg</b><small>${parseYmd(w.d).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })}${prev ? ` · ${d >= 0 ? '+' : ''}${d.toFixed(1)}` : ''}</small></div>
        <button class="x" data-act="del-weight" data-d="${w.d}" aria-label="Delete">×</button></li>`;
    }).join('');
    $('#view-weight').innerHTML = `
      <div class="card">
        <h3>Morning weigh-in</h3>
        <div class="weigh">
          <label class="field"><span>${todays ? 'Today (update)' : 'Today'} · after the toilet, before eating</span>
            <input id="wIn" type="number" inputmode="decimal" step="0.1" min="30" max="150" value="${(todays ? todays.kg : p.latest).toFixed(1)}"></label>
          <button class="btn primary" data-act="save-weight">Save</button>
        </div>
      </div>
      ${forecastCard()}
      <div class="card">
        <div class="stats">
          <div class="stat"><div class="k">Now</div><div class="v">${p.latest.toFixed(1)}</div></div>
          <div class="stat"><div class="k">Plan today</div><div class="v">${p.targetToday.toFixed(1)}</div></div>
          <div class="stat ${p.diff < -0.3 ? 'bad' : 'good'}"><div class="k">vs plan</div><div class="v">${p.diff >= 0 ? '+' : ''}${p.diff.toFixed(1)}</div></div>
          <div class="stat"><div class="k">To go</div><div class="v">${Math.max(0, p.toGo).toFixed(1)}<small>kg</small></div></div>
          <div class="stat"><div class="k">Days left</div><div class="v">${p.daysLeft}</div></div>
          <div class="stat"><div class="k">Need / day</div><div class="v">${Math.max(0, p.perDay).toFixed(2)}<small>kg</small></div></div>
        </div>
        ${p.diff < -0.5 && ws.length >= 3 ? `<p class="hint"><b>Behind plan.</b> Bump each meal by 100 kcal in Setup (that's +${S.times.length * 100} kcal a day), or add an 8th meal before bed.</p>` : ''}
      </div>
      <div class="card chart">
        <h3>Weight vs plan</h3>
        ${chartSvg()}
        <div class="legend"><span><i></i>You</span><span><i class="dash"></i>Plan</span></div>
      </div>
      <div class="card"><h3>History</h3><ul class="list">${list || '<li class="hint">No weigh-ins yet.</li>'}</ul></div>`;
  }

  function pushStatus() {
    const lines = [];
    if (S.pushErr) lines.push(`<p class="q-err">${esc(S.pushErr)}</p>`);
    if (pushHealth && !pushHealth.ok) lines.push(`<p class="q-err">${esc(pushHealth.error || 'Worker problem')}</p>`);
    if (pushHealth && pushHealth.ok) {
      const ago = pushHealth.lastCron ? Math.round((Date.now() - pushHealth.lastCron) / 60e3) : null;
      lines.push(`<p class="hint">Worker OK · ${ago === null ? 'cron not seen yet (it checks in every 10 min; add the Cron trigger if you haven\'t)' : ago <= 15 ? `cron running (checked in ${ago} min ago)` : `<b>cron hasn't checked in for ${ago} min</b>, check the Cron trigger`}</p>`);
    }
    return lines.join('');
  }

  function renderSetup() {
    if (pushBase() && !pushHealth) checkPushHealth().then(() => { if (tab === 'setup') render(); });
    const ns = notifState();
    const nsTxt = { granted: ['ok', 'On'], denied: ['no', 'Blocked in settings'], default: ['', 'Not asked yet'], unsupported: ['no', isIOS() && !isStandalone() ? 'Add to Home Screen first' : 'Not supported here'] }[ns];
    const times = S.times.map((m, i) => `<label><span>${i + 1}</span><input type="time" data-time="${i}" value="${hhmm(m)}"></label>`).join('');
    const foods = [...S.foods].map(f => `<li><div class="grow">${esc(f.n)}${f.d ? `<small>${esc(f.d)}</small>` : ''}</div>
      <input class="kc" type="number" inputmode="numeric" data-fk="${f.id}" value="${f.k}" aria-label="kcal">
      <button class="x" data-act="del-food" data-id="${f.id}" aria-label="Delete">×</button></li>`).join('');
    $('#view-setup').innerHTML = `
      <div class="card">
        <h3>Goal</h3>
        <div class="grid2">
          <label class="field"><span>Start weight (kg)</span><input type="number" step="0.1" inputmode="decimal" data-k="startWeight" value="${S.startWeight}"></label>
          <label class="field"><span>Goal weight (kg)</span><input type="number" step="0.1" inputmode="decimal" data-k="goalWeight" value="${S.goalWeight}"></label>
        </div>
        <div class="grid2">
          <label class="field"><span>Start date</span><input type="date" data-k="startDate" value="${S.startDate}"></label>
          <label class="field"><span>Deadline</span><input type="date" data-k="deadline" value="${S.deadline}"></label>
        </div>
      </div>

      <div class="card">
        <h3>Eating plan <span class="r"><b>${fmtK(dailyTarget())}</b> kcal / day</span></h3>
        <div class="grid2">
          <label class="field"><span>Wake up</span><input type="time" data-k="wake" value="${S.wake}"></label>
          <label class="field"><span>Bedtime</span><input type="time" data-k="bed" value="${S.bed}"></label>
        </div>
        <div class="field"><span>Meals per day</span>
          <div class="seg">${[5, 6, 7, 8].map(v => `<button data-act="meals" data-v="${v}" class="${S.times.length === v ? 'on' : ''}">${v}</button>`).join('')}</div></div>
        <div class="field"><span>kcal per meal</span>
          <div class="seg">${[400, 500, 600, 700].map(v => `<button data-act="kpm" data-v="${v}" class="${S.kcalPerMeal === v ? 'on' : ''}">${v}</button>`).join('')}</div></div>
        <p class="hint">First meal 30 min after waking, last one an hour before bed, the rest spaced evenly. Tweak any time below.</p>
      </div>

      <div class="card">
        <h3>Meal times <span class="r"><button class="btn sm ghost" data-act="respace">Re-space</button></span></h3>
        <div class="times">${times}</div>
      </div>

      <div class="card">
        <h3>Alerts <span class="r"><span class="status-pill ${nsTxt[0]}">${nsTxt[1]}</span></span></h3>
        <div class="row-btns">
          ${ns !== 'granted' ? '<button class="btn primary" data-act="notif">Allow notifications</button>' : '<button class="btn" data-act="test">Send a test alert</button>'}
        </div>
        <div class="field" style="margin-top:12px"><span>Nudge if a meal isn't logged after</span>
          <div class="seg">${[0, 15, 20, 30].map(v => `<button data-act="nudge" data-v="${v}" class="${S.nudgeMin === v ? 'on' : ''}">${v ? v + ' min' : 'Off'}</button>`).join('')}</div></div>
        <div class="field" style="margin-top:12px"><span>Alarm sound when the app is open</span>
          <div class="seg"><button data-act="sound" data-v="1" class="${S.sound ? 'on' : ''}">On</button><button data-act="sound" data-v="0" class="${!S.sound ? 'on' : ''}">Off</button></div></div>
        <div class="field" style="margin-top:12px"><span>Calendar alarms</span>
          <div class="seg"><button data-act="cal" data-v="1" class="${!S.calendarOff ? 'on' : ''}">Use calendar</button><button data-act="cal" data-v="0" class="${S.calendarOff ? 'on' : ''}">Notifications only</button></div></div>
        ${S.calendarOff
          ? `<p class="hint">${S.pushOn ? 'Push is on, so meal alerts arrive with the app closed.' : "Without push (below), alerts only fire while Bulk Clock is open on screen."} Already added the calendar alarms? In the Calendar app, tap a Bulk Clock event → Delete Event → <b>Delete All Future Events</b>, once per meal.</p>`
          : `<p class="hint">${S.pushOn ? 'Push is on, so you can switch to Notifications only and drop the calendar.' : 'Web apps can only alert while open on screen. Calendar alarms ring every time, or set up push below.'}</p>
        <div class="row-btns">${icsButton('btn', `Add ${S.times.length} daily alarms to calendar`)}</div>
        <p class="hint">${isIOS() ? 'iPhone: opens a Safari page, tap Add to Calendar, then "Add All". ' : 'Android: open the downloaded file with your calendar app. '}Changed your times? Delete the old Bulk Clock events, then add again.</p>`}
      </div>

      <div class="card" id="pushCard">
        <h3>Push notifications <span class="r"><span class="status-pill ${S.pushOn ? 'ok' : ''}">${S.pushOn ? 'On' : 'Off'}</span></span></h3>
        <p class="hint" style="margin-top:0">Real alerts at every meal and nudge, even with the app closed, sent by your own free Cloudflare Worker. One-time setup, about 10 minutes.</p>
        <div class="field" style="margin-top:10px"><span>Worker URL</span>
          <input type="url" id="pushUrl" value="${esc(S.pushUrl)}" placeholder="https://bulk-clock-push.yourname.workers.dev" autocomplete="off" autocapitalize="off" spellcheck="false"></div>
        ${pushStatus()}
        <div class="row-btns">${S.pushOn
          ? '<button class="btn sm" data-act="push-test">Send test</button><button class="btn sm" data-act="push-connect">Reconnect</button><button class="btn sm danger" data-act="push-off">Turn off</button>'
          : '<button class="btn primary" data-act="push-connect">Connect</button>'}</div>
        <details class="howto" ${S.pushUrl ? '' : 'open'}>
          <summary>How to set up the Worker</summary>
          <ol>
            <li>Tap <b>Copy Worker code</b>, then <b>Open Cloudflare</b> and sign up (free, no card).</li>
            <li><b>Workers &amp; Pages</b> → <b>Create</b> → <b>Start with Hello World</b>. Name it <b>bulk-clock-push</b> → <b>Deploy</b>.</li>
            <li>Tap <b>Edit code</b>. Select all, delete, paste. Tap <b>Deploy</b>.</li>
            <li><b>Storage &amp; Databases</b> → <b>Workers KV</b> → <b>Create</b>. Name it <b>bulk-clock</b>.</li>
            <li>Back in your Worker: <b>Settings</b> → <b>Bindings</b> → <b>Add</b> → <b>KV namespace</b>. Variable name <b>BULK</b>, pick <b>bulk-clock</b>, save.</li>
            <li><b>Settings</b> → <b>Trigger events</b> → <b>Add</b> → <b>Cron triggers</b> → every minute (<b>* * * * *</b>).</li>
            <li>Copy the Worker's URL (ends in <b>workers.dev</b>), paste it above, tap <b>Connect</b>.</li>
          </ol>
          <p class="hint">Cloudflare renames menus now and then; look for the closest match. Do the setup in Safari, then connect from the home-screen app.</p>
          <div class="row-btns"><button class="btn sm" data-act="push-copy">Copy Worker code</button><a class="btn sm" href="https://dash.cloudflare.com/" target="_blank" rel="noopener">Open Cloudflare</a></div>
        </details>
      </div>

      <div class="card">
        <h3>You <span class="r">for the forecast</span></h3>
        <div class="grid2">
          <label class="field"><span>Age</span><input type="number" inputmode="numeric" data-p="age" value="${S.age}"></label>
          <label class="field"><span>Height (cm)</span><input type="number" inputmode="numeric" data-p="height" value="${S.height}"></label>
        </div>
        <div class="field" style="margin-top:10px"><span>Sex</span>
          <div class="seg"><button data-act="sex" data-v="m" class="${S.sex === 'm' ? 'on' : ''}">Male</button><button data-act="sex" data-v="f" class="${S.sex === 'f' ? 'on' : ''}">Female</button></div></div>
        <div class="field" style="margin-top:10px"><span>Training</span>
          <div class="seg">${[['light', '1-2 days'], ['moderate', '3-5 days'], ['hard', '6-7 days']].map(([v, l]) => `<button data-act="activity" data-v="${v}" class="${S.activity === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>
        <p class="hint">Maintenance estimate: <b>${fmtK(maintenance().formula)} kcal/day</b>. The forecast replaces it with your real numbers after 10 days of logging.</p>
      </div>

      <div class="card">
        <h3>My foods <span class="r">tap kcal to edit</span></h3>
        <ul class="list">${foods}</ul>
        <div class="custom">
          <input type="text" id="nfName" placeholder="Add a food you'll actually eat" autocomplete="off">
          <input type="number" id="nfK" placeholder="kcal" inputmode="numeric">
          <button class="btn sm primary" data-act="add-food">Add</button>
        </div>
        <p class="hint">Calories are rough estimates for typical portions. Swap in numbers from the packet when you have them.</p>
      </div>

      <div class="card" id="aiCard">
        <h3>Gemini quick log <span class="r"><span class="status-pill ${S.aiKey ? 'ok' : ''}">${S.aiKey ? 'On' : 'Off'}</span></span></h3>
        <p class="hint" style="margin-top:0">Powers the round button: say or snap what you ate and it logs each item into the right meal. Get a free key at <b>aistudio.google.com/apikey</b>.</p>
        <div class="field" style="margin-top:10px"><span>API key</span>
          <input type="password" id="aiKey" value="${esc(S.aiKey)}" placeholder="Paste your Gemini API key" autocomplete="off" autocapitalize="off" spellcheck="false"></div>
        <div class="field"><span>Model</span>
          <input type="text" id="aiModel" value="${esc(S.aiModel)}" placeholder="${DEFAULT_MODEL} (auto)" autocomplete="off" autocapitalize="off" spellcheck="false"></div>
        ${S.aiKey ? `<div class="row-btns"><button class="btn sm" data-act="ai-test">Test key</button><button class="btn sm danger" data-act="ai-clear">Remove key</button></div>` : ''}
        <p class="hint">The key stays on this phone and is only sent to Google. Leave Model blank to use the latest Flash.</p>
      </div>

      <div class="card">
        <h3>Data</h3>
        <p class="hint" style="margin-top:0">Everything stays on this phone. Back up before clearing your browser.</p>
        <div class="row-btns">
          <button class="btn sm" data-act="backup">Back up</button>
          <label class="btn sm" style="position:relative">Restore<input type="file" accept="application/json" id="restore" style="position:absolute;inset:0;opacity:0"></label>
          <button class="btn sm danger" data-act="reset">Reset</button>
        </div>
      </div>`;
  }

  /* ---------- Gemini quick log ---------- */
  const GEM = 'https://generativelanguage.googleapis.com/v1beta';
  const DEFAULT_MODEL = 'gemini-flash-latest';
  // day: day key to log into (null = today), meal: slot index, -1 Extra, null = auto by time
  let Q = { text: '', img: null, busy: false, result: null, error: '', day: null, meal: null };
  let sheetMode = 'meal';
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const AI_SYSTEM = `You log food for someone in Malaysia who weighs about 53 kg and is bulking on roughly 500 kcal meals.
From the description and/or photo, list every food and drink as its own item with a realistic calorie estimate for a typical Malaysian portion (mamak, hawker, kopitiam, home cooking, convenience store, fast food). Use any quantity or size given. Count sugar and condensed milk in drinks.
When: if the user says when they ate an item ("breakfast at 9", "lunch", "this morning", "just now", "an hour ago"), set eaten_at to a 24h HH:MM time, using the current time given. Typical times: breakfast 08:30, lunch 13:00, tea 16:00, dinner 19:30, supper 23:00. If they say it was on an earlier day ("yesterday", "on Monday", "2 days ago"), set days_ago (1 = yesterday) using today's date given. Leave eaten_at empty and days_ago 0 when not said.
Keep item names short (max 4 words); keep Malay food names in Malay.
comment: one short line (max 12 words) on how this compares with a 500 kcal meal, or a quick add-on to reach it. No lectures.
Reply only with JSON: {"items":[{"name":"...","kcal":123,"eaten_at":"HH:MM or null","days_ago":0}],"comment":"..."}`;

  const AI_SCHEMA = {
    type: 'OBJECT',
    properties: {
      items: { type: 'ARRAY', items: { type: 'OBJECT', properties: {
        name: { type: 'STRING' }, kcal: { type: 'INTEGER' },
        eaten_at: { type: 'STRING', nullable: true }, days_ago: { type: 'INTEGER', nullable: true } }, required: ['name', 'kcal'] } },
      comment: { type: 'STRING' },
    },
    required: ['items'],
  };

  const gemHeaders = () => ({ 'Content-Type': 'application/json', 'x-goog-api-key': S.aiKey });
  let modelList = null;
  async function flashModels() {
    if (modelList) return modelList;
    try {
      const r = await fetch(`${GEM}/models?pageSize=200`, { headers: gemHeaders() });
      if (!r.ok) return [];
      const j = await r.json();
      const names = (j.models || [])
        .filter(m => (m.supportedGenerationMethods || []).includes('generateContent'))
        .map(m => m.name.replace(/^models\//, ''))
        .filter(n => /flash/.test(n) && !/image|tts|live|audio|embed|exp/.test(n))
        .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
      // stable before preview; within each, full Flash before Lite
      const rank = n => (/preview/.test(n) ? 2 : 0) + (/lite/.test(n) ? 1 : 0);
      modelList = names.sort((a, b) => rank(a) - rank(b));
    } catch { modelList = []; }
    return modelList;
  }
  const googleMsg = txt => { try { return JSON.parse(txt).error.message || ''; } catch { return ''; } };
  function gemError(status, txt) {
    const msg = googleMsg(txt);
    if (/API_KEY_INVALID|API key not valid/i.test(txt)) return "That Gemini API key isn't valid. Check it in Setup.";
    if (status === 403) return 'This key is not allowed to use the Gemini API. ' + msg;
    if (status === 429) return 'Gemini free-tier limit hit. Wait a minute and try again.';
    return `Gemini error ${status}${msg ? ': ' + msg : ''}`;
  }

  async function askGemini(text, img) {
    const now = new Date();
    const parts = [{ text: `Today is ${now.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}. Current time: ${pad(now.getHours())}:${pad(now.getMinutes())}.\nWhat I ate: ${text || '(see photo)'}` }];
    if (img) parts.push({ inline_data: { mime_type: 'image/jpeg', data: img.split(',')[1] } });
    const payload = schema => JSON.stringify({
      systemInstruction: { parts: [{ text: AI_SYSTEM }] },
      contents: [{ role: 'user', parts }],
      generationConfig: schema
        ? { temperature: 0.2, responseMimeType: 'application/json', responseSchema: AI_SCHEMA }
        : { temperature: 0.2, responseMimeType: 'application/json' },
    });
    const call = (m, schema) => fetch(`${GEM}/models/${encodeURIComponent(m)}:generateContent`, { method: 'POST', headers: gemHeaders(), body: payload(schema) });

    // Busy (503/429) or missing (404): move straight on to the next model; free-tier overloads are
    // per model, so another Flash or Flash-Lite usually answers. Internal errors (500) get one retry
    // without the JSON schema first.
    const tried = [];
    let queue = null;
    const nextModel = async () => {
      if (!queue) {
        const listed = await flashModels();
        queue = [...listed, 'gemini-flash-lite-latest', 'gemini-2.5-flash', 'gemini-2.5-flash-lite'];
      }
      while (queue.length) { const m = queue.shift(); if (!tried.includes(m)) return m; }
      return null;
    };
    let model = S.aiModel || DEFAULT_MODEL, schema = true, lastErr = '', busy = 0, retried500 = false;
    for (let attempt = 0; attempt < 10 && model; attempt++) {
      if (!tried.includes(model)) tried.push(model);
      let r;
      try { r = await call(model, schema); } catch { throw new Error('No connection to Gemini. Check your signal.'); }
      if (r.ok) {
        const j = await r.json();
        const txt = (j.candidates?.[0]?.content?.parts || []).filter(p => !p.thought).map(p => p.text || '').join('').trim();
        if (!txt) { lastErr = j.promptFeedback?.blockReason ? 'Gemini refused that one.' : 'Gemini sent back nothing.'; break; }
        let out;
        try { out = JSON.parse(txt.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')); } catch { lastErr = "Couldn't read Gemini's answer."; if (schema) { schema = false; continue; } break; }
        const items = (out.items || []).map(it => ({
          n: String(it.name || 'Food').slice(0, 40), k: Math.max(0, Math.round(Number(it.kcal) || 0)),
          at: it.eaten_at && it.eaten_at !== 'null' ? String(it.eaten_at) : null, ago: clamp(Math.round(Number(it.days_ago) || 0), 0, 30),
        })).filter(it => it.k > 0);
        if (!items.length) throw new Error("Gemini couldn't find any food in that.");
        if (model !== (S.aiModel || DEFAULT_MODEL)) { S.aiModel = model; save(); }
        return { items, comment: out.comment || '', model };
      }
      const body = await r.text();
      lastErr = gemError(r.status, body);
      if (/API_KEY_INVALID|API key not valid/i.test(body) || r.status === 403) break;
      if (r.status === 400 && schema && /schema|response_?schema|Invalid JSON payload|Unknown name/i.test(body)) { schema = false; continue; }
      if (r.status === 500 && !retried500) { retried500 = true; schema = false; continue; }
      if (r.status === 503 || r.status === 429) busy++;
      if (r.status === 404 || r.status >= 500 || r.status === 429) { model = await nextModel(); continue; }
      break;
    }
    if (busy >= 2) lastErr = `Google's Gemini servers are overloaded right now (tried ${tried.length} models). Spikes usually pass in a few minutes, or pick from your food list below.`;
    throw new Error(lastErr || 'Gemini failed. Try again.');
  }

  /* where things land */
  const dayLabel = k => { const tk = todayKey(); return k === tk ? 'Today' : k === addDays(tk, -1) ? 'Yesterday' : parseYmd(k).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }); };
  const nowMin = () => Math.round((Date.now() - parseYmd(todayKey()).getTime()) / 60e3);
  // closest slot to minute `min`; when not explicit, skip slots already ~full
  function slotFor(key, min, explicit) {
    const c = S.times.map((t, i) => ({ i, d: Math.abs(t - min), full: mealKcal(key, i) >= S.kcalPerMeal * 0.8 }));
    const pool = explicit ? c : (c.some(x => !x.full) ? c.filter(x => !x.full) : c);
    return pool.sort((a, b) => a.d - b.d)[0].i;
  }
  function firstOpenSlot(key) {
    const i = S.times.findIndex((_, j) => mealKcal(key, j) < S.kcalPerMeal * 0.8);
    return i === -1 ? S.times.length - 1 : i;
  }
  function atToMin(at, key) {
    const m = /^(\d{1,2}):(\d{2})/.exec(at || '');
    if (!m) return null;
    let v = Number(m[1]) * 60 + Number(m[2]);
    if (v < toMin(S.wake) - 60) v += 1440;
    if (key === todayKey() && v > nowMin() + 30) return null; // can't have eaten in the future
    return v;
  }
  // Q.day / Q.meal override what Gemini worked out
  function logItems(items, source) {
    const tk = todayKey();
    const added = [];
    items.forEach(it => {
      const key = Q.day || (it.ago ? addDays(tk, -it.ago) : tk);
      let m;
      if (Q.meal !== null) m = Q.meal;
      else {
        const at = atToMin(it.at, key);
        if (at !== null) m = slotFor(key, at, true);
        else m = key === tk ? slotFor(key, nowMin(), false) : firstOpenSlot(key);
      }
      const e = { id: uid(), m, n: it.n, k: it.k, t: Date.now(), ai: 1 };
      (S.log[key] = S.log[key] || []).push(e);
      added.push({ ...e, key });
    });
    if (source) {
      const t = source.slice(0, 48);
      S.aiRecent = [{ t, items: items.map(({ n, k }) => ({ n, k })) }, ...(S.aiRecent || []).filter(r => r.t !== t)].slice(0, 6);
    }
    save();
    return added;
  }
  function autoHint() {
    const tk = todayKey(), key = Q.day || tk;
    if (Q.meal !== null) return `Everything goes into <b>${Q.meal === -1 ? 'Extra' : `meal ${Q.meal + 1}`}</b>${key !== tk ? `, ${dayLabel(key).toLowerCase()}` : ''}.`;
    if (key === tk) { const s = slotFor(tk, nowMin(), false); return `Goes into <b>meal ${s + 1}</b> (${clock12(S.times[s])}) unless you say when, e.g. "roti canai at 9am, nasi ayam for lunch, yesterday's dinner was mee goreng".`; }
    return `Goes into the meal matching the time you mention ("lunch", "at 8pm"), otherwise the first empty meal ${/^(Today|Yesterday)$/.test(dayLabel(key)) ? dayLabel(key).toLowerCase() : 'on ' + dayLabel(key)}.`;
  }

  async function shrinkPhoto(file) {
    const url = URL.createObjectURL(file);
    try {
      const img = await new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url; });
      const s = Math.min(1, 1024 / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement('canvas');
      c.width = Math.round(img.naturalWidth * s); c.height = Math.round(img.naturalHeight * s);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      return c.toDataURL('image/jpeg', 0.8);
    } finally { URL.revokeObjectURL(url); }
  }

  async function runQuick() {
    const text = (Q.text || '').trim();
    if (!text && !Q.img) { Q.error = 'Type what you ate, or add a photo.'; renderSheet(); return; }
    Q.busy = true; Q.error = ''; renderSheet();
    try {
      const res = await askGemini(text, Q.img);
      const added = logItems(res.items, text || null);
      Q.result = { added, comment: res.comment };
      Q.text = ''; Q.img = null;
      if (navigator.vibrate) navigator.vibrate(40);
    } catch (err) {
      Q.error = err.message || 'Something went wrong.';
    }
    Q.busy = false;
    renderSheet(); render();
  }

  function renderQuick() {
    const tk = todayKey();
    const head = `<div class="grab"></div>
      <div class="sheet-head"><h2>Quick log</h2><button class="btn sm" data-act="close">Done</button></div>`;
    if (!S.aiKey) {
      $('#sheet').innerHTML = head + `
        <div class="card q-empty">
          <p>Add your <b>Gemini API key</b> in Setup and this button logs food from a sentence or a photo, straight into the right meal.</p>
          <div class="row-btns"><button class="btn primary" data-act="go-ai-setup">Add key in Setup</button></div>
          <div class="row-btns"><button class="btn" data-act="log" data-meal="${defaultLogMeal()}">Pick from my food list</button></div>
        </div>`;
      return;
    }
    if (Q.result) {
      const tot = Q.result.added.reduce((a, e) => a + e.k, 0);
      const groups = {};
      Q.result.added.forEach(e => { const g = e.key + '|' + e.m; (groups[g] = groups[g] || []).push(e); });
      const rows = Object.keys(groups).sort().map(g => {
        const [key, ms] = g.split('|'); const m = Number(ms);
        const name = m === -1 ? 'Extra' : `Meal ${m + 1}`;
        const when = (key !== tk ? dayLabel(key) + ', ' : '') + (m === -1 ? '' : clock12(S.times[m]));
        const fill = m === -1 ? '' : `<span>${fmtK(mealKcal(key, m))} / ${S.kcalPerMeal}</span>`;
        return `<div class="q-slot"><div class="q-slot-h"><b>${name}</b> · ${when}${fill}</div>
          ${groups[g].map(e => `<div class="q-item"><span>${esc(e.n)}</span><b>${e.k}</b></div>`).join('')}</div>`;
      }).join('');
      $('#sheet').innerHTML = head + `
        <div class="q-done"><div class="eyebrow">LOGGED</div><div class="q-total">+${fmtK(tot)} <small>kcal</small></div>
        ${Q.result.comment ? `<p class="hint">${esc(Q.result.comment)}</p>` : ''}</div>
        ${rows}
        <p class="hint">Wrong meal or number? Fix it in History.</p>
        <div class="row-btns"><button class="btn" data-act="ai-undo">Undo</button><button class="btn primary" data-act="ai-again">Log more</button></div>`;
      return;
    }
    const dayOpts = Array.from({ length: 7 }, (_, j) => addDays(tk, -j)).map(k => `<option value="${k}" ${(Q.day || tk) === k ? 'selected' : ''}>${dayLabel(k)}</option>`).join('');
    const key = Q.day || tk;
    const mealOpts = `<option value="auto" ${Q.meal === null ? 'selected' : ''}>Auto, by time</option>`
      + S.times.map((t, i) => `<option value="${i}" ${Q.meal === i ? 'selected' : ''}>Meal ${i + 1} · ${clock12(t)}${mealKcal(key, i) ? ` (${fmtK(mealKcal(key, i))})` : ''}</option>`).join('')
      + `<option value="-1" ${Q.meal === -1 ? 'selected' : ''}>Extra</option>`;
    const recent = (S.aiRecent || []).map((r, i) => `<button class="q-chip" data-act="ai-recent" data-i="${i}">${esc(r.t)} <b>${fmtK(r.items.reduce((a, x) => a + x.k, 0))}</b></button>`).join('');
    $('#sheet').innerHTML = head + `
      <div class="grid2 q-when">
        <label class="field"><span>Day</span><select id="qDay" ${Q.busy ? 'disabled' : ''}>${dayOpts}</select></label>
        <label class="field"><span>Meal</span><select id="qMeal" ${Q.busy ? 'disabled' : ''}>${mealOpts}</select></label>
      </div>
      <textarea id="qText" rows="3" placeholder="What did you eat? e.g. 2 roti canai with dhal and a teh tarik" ${Q.busy ? 'disabled' : ''}>${esc(Q.text)}</textarea>
      ${Q.img ? `<div class="q-photo"><img src="${Q.img}" alt="Your food photo"><button class="x" data-act="ai-noimg" aria-label="Remove photo">×</button></div>` : ''}
      <div class="q-actions">
        <label class="btn sm q-cam ${Q.busy ? 'off' : ''}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>Photo<input type="file" accept="image/*" id="qImg" hidden></label>
        <button class="btn primary" data-act="ai-go" ${Q.busy ? 'disabled' : ''}>${Q.busy ? '<span class="spin"></span>Reading it' : 'Log it'}</button>
      </div>
      ${Q.error ? `<p class="q-err">${esc(Q.error)}</p>${(Q.day || tk) === tk ? `<div class="row-btns"><button class="btn sm" data-act="log" data-meal="${Q.meal !== null ? Q.meal : defaultLogMeal()}">Pick from my food list instead</button></div>` : ''}` : ''}
      <p class="hint">${autoHint()} Tap the mic on your keyboard to say it.</p>
      ${recent ? `<div class="sec-lbl">Again · no AI needed</div><div class="q-chips">${recent}</div>` : ''}`;
  }

  async function testKey() {
    if (!S.aiKey) { toast('Paste your key first'); return; }
    toast('Testing…');
    try {
      const r = await askGemini('1 banana', null);
      toast(`Works (${r.model}): ${r.items[0].n} ≈ ${r.items[0].k} kcal`);
      render();
    } catch (err) { toast(err.message); }
  }

  function renderHistory() {
    const tk = todayKey();
    const target = dailyTarget();
    const days = Array.from({ length: 21 }, (_, j) => addDays(tk, -j)).filter((k, j) => entries(k).length || j < 3);
    const html = days.map(k => {
      const tot = dayKcal(k);
      const groups = S.times.map((t, i) => ({ i, list: entries(k).filter(e => e.m === i) })).filter(g => g.list.length);
      const extra = entries(k).filter(e => e.m === -1);
      if (extra.length) groups.push({ i: -1, list: extra });
      const label = k === tk ? 'Today' : k === addDays(tk, -1) ? 'Yesterday' : parseYmd(k).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
      return `<div class="card">
        <h3>${label}<span class="r"><b class="${tot >= target * 0.9 ? 'ok-t' : ''}">${fmtK(tot)}</b> / ${fmtK(target)}</span></h3>
        <div class="bar" style="margin:0 0 10px"><i style="width:${clamp(tot / target * 100, 0, 100)}%"></i></div>
        <button class="btn sm h-add" data-act="quick-day" data-k="${k}"><span class="ai-mark">✦</span>Log food for ${/^(Today|Yesterday)$/.test(label) ? label.toLowerCase() : label}</button>
        ${groups.length ? groups.map(g => `<div class="h-meal"><div class="h-lbl">${g.i === -1 ? 'Extra' : `Meal ${g.i + 1} <small>${clock12(S.times[g.i])}</small>`}</div>
          <ul class="entries">${g.list.map(e => `<li>${e.ai ? '<span class="ai-mark">✦</span>' : ''}${esc(e.n)} · ${e.k}<button class="x" data-act="del-hist" data-k="${k}" data-id="${e.id}" aria-label="Remove">×</button></li>`).join('')}</ul></div>`).join('') : '<p class="hint" style="margin:0">Nothing logged yet.</p>'}
      </div>`;
    }).join('');
    $('#view-history').innerHTML = html;
  }

  /* ---------- sheet ---------- */
  function openQuick(opts = {}) {
    sheetMode = 'quick'; sheetMeal = -2;
    if (!Q.busy) {
      Q.result = null; Q.error = '';
      Q.day = opts.day && opts.day !== todayKey() ? opts.day : null;
      Q.meal = opts.meal !== undefined ? opts.meal : null;
    }
    $('#scrim').hidden = false; $('#sheet').hidden = false;
    document.body.style.overflow = 'hidden';
    renderSheet();
  }
  function openSheet(m) {
    sheetMode = 'meal';
    sheetMeal = m;
    $('#scrim').hidden = false; $('#sheet').hidden = false;
    document.body.style.overflow = 'hidden';
    renderSheet();
  }
  function closeSheet() {
    sheetMeal = null; sheetMode = 'meal';
    $('#scrim').hidden = true; $('#sheet').hidden = true;
    document.body.style.overflow = '';
  }
  function renderSheet() {
    if (sheetMeal === null) return;
    if (sheetMode === 'quick') { renderQuick(); return; }
    const key = todayKey();
    const m = sheetMeal;
    const kc = m === -1 ? entries(key).filter(e => e.m === -1).reduce((a, e) => a + e.k, 0) : mealKcal(key, m);
    const ents = entries(key).filter(e => e.m === m);
    const picks = S.times.map((t, i) => `<button data-act="pick" data-meal="${i}" class="${i === m ? 'on' : ''} ${mealKcal(key, i) > 0 ? 'has' : ''}">${i + 1}<span style="font-weight:500;opacity:.7"> · ${clock12(t)}</span></button>`).join('')
      + `<button data-act="pick" data-meal="-1" class="${m === -1 ? 'on' : ''}">Extra</button>`;
    const foods = [...S.foods].sort((a, b) => (b.u || 0) - (a.u || 0)).map(f =>
      `<button class="food ${f.u > 0 ? 'fav' : ''}" data-act="food" data-id="${f.id}"><span class="fn">${esc(f.n)}</span><span class="fk">${f.k}</span></button>`).join('');
    $('#sheet').innerHTML = `
      <div class="grab"></div>
      <div class="sheet-head">
        <h2>${m === -1 ? 'Extra' : 'Meal ' + (m + 1)}</h2>
        <div class="tot"><b>${fmtK(kc)}</b>${m === -1 ? '' : ' / ' + S.kcalPerMeal} kcal</div>
        <button class="btn sm" data-act="close">Done</button>
      </div>
      <div class="mealpick">${picks}</div>
      ${S.aiKey && m !== -1 ? `<button class="btn q-describe" data-act="quick-meal" data-meal="${m}"><span class="ai-mark">✦</span>Describe meal ${m + 1} instead</button>` : ''}
      ${ents.length ? `<ul class="entries">${ents.map(e => `<li>${e.ai ? '<span class="ai-mark">✦</span>' : ''}${esc(e.n)} · ${e.k}<button class="x" data-act="del-entry" data-id="${e.id}" aria-label="Remove">×</button></li>`).join('')}</ul>` : ''}
      <div class="custom">
        <input type="text" id="cName" placeholder="Something else" autocomplete="off">
        <input type="number" id="cK" placeholder="kcal" inputmode="numeric">
        <button class="btn sm primary" data-act="custom">Add</button>
      </div>
      <div class="sec-lbl">Tap to add · most eaten first</div>
      <div class="foods">${foods}</div>`;
    const pickOn = $('#sheet .mealpick .on');
    if (pickOn) pickOn.scrollIntoView({ block: 'nearest', inline: 'center' });
  }

  /* =========================================================
     Events
     ========================================================= */
  function setTab(t) {
    tab = t;
    document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
    document.querySelectorAll('.view').forEach(v => { v.hidden = v.dataset.view !== t; });
    window.scrollTo(0, 0);
    render();
  }

  function regenTimes(n) { S.times = autoTimes(S.wake, S.bed, n || S.times.length); }

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-act], [data-tab]');
    if (!b) return;
    const act = b.dataset.act || (b.classList.contains('tab') ? 'tab' : null);
    const mi = b.dataset.meal !== undefined ? Number(b.dataset.meal) : null;
    switch (act) {
      case 'tab': setTab(b.dataset.tab); break;
      case 'log': openSheet(mi); break;
      case 'quick': openQuick(); break;
      case 'push-connect': pushConnect(); break;
      case 'push-test': pushPost('/test', {}).then(() => toast('Sent. It should land in a few seconds.')).catch(e => toast(e.message)); break;
      case 'push-off': pushOff(); break;
      case 'push-copy': {
        if (!workerCode) { toast('Still loading the code, try again'); break; }
        (navigator.clipboard ? navigator.clipboard.writeText(workerCode) : Promise.reject()).then(() => toast('Worker code copied')).catch(() => toast("Couldn't copy. Long-press to copy from push-worker.js"));
        break;
      }
      case 'go-push': setTab('setup'); setTimeout(() => { const el = $('#pushCard'); if (el) el.scrollIntoView({ behavior: 'smooth' }); }, 50); break;
      case 'cal': S.calendarOff = b.dataset.v === '0'; save(); render(); break;
      case 'sex': S.sex = b.dataset.v; save(); render(); break;
      case 'activity': S.activity = b.dataset.v; save(); render(); break;
      case 'quick-day': openQuick({ day: b.dataset.k }); break;
      case 'quick-meal': openQuick({ meal: Number(b.dataset.meal) }); break;
      case 'ai-go': { const t = $('#qText'); if (t) Q.text = t.value; runQuick(); break; }
      case 'ai-noimg': Q.img = null; renderSheet(); break;
      case 'ai-again': Q.result = null; renderSheet(); break;
      case 'ai-undo': {
        if (Q.result) { Q.result.added.forEach(e => { S.log[e.key] = entries(e.key).filter(x => x.id !== e.id); }); save(); }
        Q.result = null; renderSheet(); render(); toast('Undone');
        break;
      }
      case 'ai-recent': {
        const r = (S.aiRecent || [])[Number(b.dataset.i)];
        if (r) { const added = logItems(r.items.map(x => ({ ...x, at: null, ago: 0 })), r.t); Q.result = { added, comment: '' }; renderSheet(); render(); }
        break;
      }
      case 'go-ai-setup': closeSheet(); setTab('setup'); setTimeout(() => { const el = $('#aiCard'); if (el) el.scrollIntoView({ behavior: 'smooth' }); }, 50); break;
      case 'ai-test': testKey(); break;
      case 'ai-clear': S.aiKey = ''; S.aiModel = ''; save(); render(); toast('Key removed'); break;
      case 'del-hist': removeEntry(b.dataset.k, b.dataset.id); break;
      case 'pick': sheetMeal = mi; renderSheet(); break;
      case 'close': closeSheet(); break;
      case 'food': { const f = S.foods.find(x => x.id === b.dataset.id); if (f) addEntry(sheetMeal, f.n, f.k, f.id); break; }
      case 'custom': {
        const n = $('#cName').value.trim() || 'Food', k = Number($('#cK').value);
        if (!(k > 0)) { toast('Enter the kcal'); $('#cK').focus(); break; }
        addEntry(sheetMeal, n, k);
        break;
      }
      case 'del-entry': removeEntry(todayKey(), b.dataset.id); break;
      case 'undo': { const el = $('#toast'); if (el._undo) el._undo(); el._undo = null; el.classList.remove('show'); break; }
      case 'notif': askNotif(); break;
      case 'test': fire('Test: eat now', `~${S.kcalPerMeal} kcal. Idea: ${suggest(S.kcalPerMeal)}`, 'test'); showAlarm(0, false); break;
      case 'ics': exportIcs(); break;
      case 'ics-ios': S.calendarDone = true; save(); setTimeout(render, 300); break;
      case 'onboard-done': S.onboardDone = true; save(); render(); break;
      case 'meals': regenTimes(Number(b.dataset.v)); S.meals = S.times.length; save(); render(); break;
      case 'kpm': S.kcalPerMeal = Number(b.dataset.v); save(); render(); break;
      case 'nudge': S.nudgeMin = Number(b.dataset.v); save(); render(); break;
      case 'sound': S.sound = b.dataset.v === '1'; save(); render(); if (S.sound) beep(); break;
      case 'respace': regenTimes(); save(); render(); toast('Meals re-spaced'); break;
      case 'save-weight': {
        const kg = Number($('#wIn').value);
        if (!(kg > 20 && kg < 250)) { toast('Check that number'); break; }
        const d = todayKey();
        S.weights = S.weights.filter(w => w.d !== d).concat({ d, kg: Math.round(kg * 10) / 10 });
        save(); render(); toast(`Saved ${kg.toFixed(1)} kg`);
        break;
      }
      case 'del-weight': S.weights = S.weights.filter(w => w.d !== b.dataset.d); save(); render(); break;
      case 'del-food': S.foods = S.foods.filter(f => f.id !== b.dataset.id); save(); render(); break;
      case 'add-food': {
        const n = $('#nfName').value.trim(), k = Number($('#nfK').value);
        if (!n || !(k > 0)) { toast('Name and kcal, please'); break; }
        S.foods.unshift({ id: uid(), n, k: Math.round(k), u: 0 }); save(); render(); toast('Added to your foods');
        break;
      }
      case 'backup': download(new Blob([JSON.stringify(S, null, 1)], { type: 'application/json' }), `bulk-clock-${todayKey()}.json`); break;
      case 'reset':
        if (confirm('Wipe all meals, weights and settings on this phone?')) { S = defaults(); save(); setTab('today'); }
        break;
      case 'alarm-log': $('#alarm').hidden = true; openSheet(mi); break;
      case 'alarm-close': $('#alarm').hidden = true; break;
    }
  });

  $('#scrim').addEventListener('click', closeSheet);
  document.addEventListener('input', e => { if (e.target.id === 'qText') Q.text = e.target.value; });

  document.addEventListener('change', e => {
    const t = e.target;
    if (t.dataset.k) {
      const k = t.dataset.k;
      if (k === 'startWeight' || k === 'goalWeight') {
        const v = Number(t.value); if (!(v > 20)) return;
        S[k] = v;
        if (k === 'startWeight') { const first = sortedWeights()[0]; if (first && first.d === S.startDate) first.kg = v; }
      } else if (k === 'startDate' || k === 'deadline') {
        if (!t.value) return; S[k] = t.value;
      } else if (k === 'wake' || k === 'bed') {
        if (!t.value) return; S[k] = t.value; regenTimes();
      }
      save(); render();
    } else if (t.dataset.time !== undefined) {
      if (!t.value) return;
      let m = toMin(t.value);
      if (m < toMin(S.wake) - 60) m += 1440; // after midnight belongs to the same day
      S.times[Number(t.dataset.time)] = m;
      S.times.sort((a, b) => a - b);
      save(); render();
    } else if (t.dataset.fk) {
      const f = S.foods.find(x => x.id === t.dataset.fk); const v = Number(t.value);
      if (f && v > 0) { f.k = Math.round(v); save(); }
    } else if (t.id === 'qImg' && t.files[0]) {
      const ta = $('#qText'); if (ta) Q.text = ta.value;
      shrinkPhoto(t.files[0]).then(d => { Q.img = d; renderSheet(); }).catch(() => toast("Couldn't read that photo"));
    } else if (t.dataset.p) {
      const v = Number(t.value);
      if (t.dataset.p === 'age' && v >= 10 && v <= 90) S.age = v;
      if (t.dataset.p === 'height' && v >= 120 && v <= 230) S.height = v;
      save(); render();
    } else if (t.id === 'pushUrl') {
      S.pushUrl = t.value.trim(); pushHealth = null; save(); render();
    } else if (t.id === 'qDay') {
      const ta = $('#qText'); if (ta) Q.text = ta.value;
      Q.day = t.value === todayKey() ? null : t.value; renderSheet();
    } else if (t.id === 'qMeal') {
      const ta = $('#qText'); if (ta) Q.text = ta.value;
      Q.meal = t.value === 'auto' ? null : Number(t.value); renderSheet();
    } else if (t.id === 'aiKey') {
      S.aiKey = t.value.trim(); S.aiModel = ''; save(); render();
      if (S.aiKey) testKey();
    } else if (t.id === 'aiModel') {
      S.aiModel = t.value.trim(); save();
    } else if (t.id === 'restore' && t.files[0]) {
      t.files[0].text().then(txt => {
        const data = JSON.parse(txt);
        if (!data || !Array.isArray(data.times)) throw new Error('bad');
        S = Object.assign(defaults(), data); save(); render(); toast('Restored');
      }).catch(() => toast('That file is not a Bulk Clock backup'));
    }
  });

  /* ---------- clock ---------- */
  function tick() {
    checkAlerts();
    if (sig() !== lastSig) { render(); return; }
    if (tab === 'today') {
      const h = heroState();
      const cd = $('#cd');
      if (cd && h.kind !== 'due') cd.textContent = countdown(h.m.ts - Date.now());
    }
  }
  setInterval(tick, 1000);
  let minuteTimer = setInterval(() => { if (tab === 'today' && sheetMeal === null) render(); }, 60e3);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { checkAlerts(); render(); if (swReg) swReg.update().catch(() => {}); pushOnOpen(); } });

  // first weigh-in seed: keep start weight in history
  if (!S.weights.length) S.weights = [{ d: S.startDate, kg: S.startWeight }];
  S.meals = S.times.length;
  render();
  pushOnOpen();
  void minuteTimer;
})();
