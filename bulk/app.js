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
    };
  }
  let S;
  try { S = Object.assign(defaults(), JSON.parse(localStorage.getItem(KEY) || '{}')); }
  catch { S = defaults(); }
  // v2: the old iPhone build ticked the calendar step without adding anything, so clear it once
  if ((S.v || 1) < 2) { S.calendarDone = false; S.v = 2; }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch { /* storage blocked */ } };

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
    navigator.serviceWorker.register('sw.js').then(r => { swReg = r; }).catch(() => {});
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
        fire(`Eat now: meal ${m.i + 1} of ${n}`, `~${S.kcalPerMeal} kcal. Idea: ${suggest(S.kcalPerMeal, m.i)}`, 'meal');
        if (document.visibilityState === 'visible') showAlarm(m.i, false);
      }
      const nudgeAt = m.ts + S.nudgeMin * 60e3;
      if (S.nudgeMin > 0 && now >= nudgeAt && now < nudgeAt + ALERT_WINDOW && now < m.next && !nmap['n' + m.i]) {
        nmap['n' + m.i] = 1; changed = true;
        const behind = Math.max(0, (m.i + 1) * S.kcalPerMeal - dayKcal(key));
        fire(`Meal ${m.i + 1} still not logged`, `You're ${fmtK(behind)} kcal behind today. Eat something now, even a shake.`, 'meal');
        if (document.visibilityState === 'visible') showAlarm(m.i, true);
      }
    });
    // keep the notified map small
    Object.keys(S.notified).forEach(k => { if (k < addDays(key, -3)) { delete S.notified[k]; changed = true; } });
    if (changed) save();
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
        <li class="${S.calendarDone ? 'ok' : ''}"><div><b>Add meal alarms to your calendar</b><p>The bulletproof one: your phone's calendar rings at every meal even with this app closed. Re-add it if you change meal times.</p>
          ${icsButton('btn sm', `Add ${S.times.length} daily alarms`)}</div></li>
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
        <div class="row-btns"><button class="btn sm" data-act="tab" data-tab="weight">Log today's weight</button></div>
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

  function renderSetup() {
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
        <p class="hint">Phones only let web apps alert while open or recently used. The calendar alarms below ring every time, even with the app closed. Use both.</p>
        <div class="row-btns">${icsButton('btn', `Add ${S.times.length} daily alarms to calendar`)}</div>
        <p class="hint">${isIOS() ? 'iPhone: opens a Safari page, tap Add to Calendar, then "Add All". ' : 'Android: open the downloaded file with your calendar app (Samsung Calendar opens it; for Google Calendar import it at calendar.google.com → Settings → Import). '}Changed your times? Delete the old Bulk Clock events, then add again.</p>
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
  let Q = { text: '', img: null, busy: false, result: null, error: '' };
  let sheetMode = 'meal';

  const AI_SYSTEM = `You log food for someone in Malaysia who weighs about 53 kg and is bulking on roughly 500 kcal meals.
From the description and/or photo, list every food and drink as its own item with a realistic calorie estimate for a typical Malaysian portion (mamak, hawker, kopitiam, home cooking, convenience store, fast food). Use any quantity or size given. Count sugar and condensed milk in drinks.
If the user says when they ate an item ("breakfast at 9", "lunch", "this morning", "just now"), set eaten_at to a 24h HH:MM time for that item, using the current time given to resolve words like "just now" or "an hour ago". If they don't say, leave eaten_at empty.
Keep item names short (max 4 words); keep Malay food names in Malay.
comment: one short line (max 12 words) on how this compares with a 500 kcal meal, or a quick add-on to reach it. No lectures.`;

  const AI_SCHEMA = {
    type: 'OBJECT',
    properties: {
      items: { type: 'ARRAY', items: { type: 'OBJECT', properties: {
        name: { type: 'STRING' }, kcal: { type: 'INTEGER' }, eaten_at: { type: 'STRING', nullable: true } }, required: ['name', 'kcal'] } },
      comment: { type: 'STRING' },
    },
    required: ['items'],
  };

  const gemHeaders = () => ({ 'Content-Type': 'application/json', 'x-goog-api-key': S.aiKey });
  async function pickModel() {
    const r = await fetch(`${GEM}/models?pageSize=200`, { headers: gemHeaders() });
    if (!r.ok) return null;
    const j = await r.json();
    const names = (j.models || [])
      .filter(m => (m.supportedGenerationMethods || []).includes('generateContent'))
      .map(m => m.name.replace(/^models\//, ''))
      .filter(n => /flash/.test(n) && !/image|tts|live|audio|embed|exp|preview/.test(n))
      .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
    return names.find(n => !/lite/.test(n)) || names[0] || null;
  }
  function gemError(status, txt) {
    if (/API_KEY_INVALID|API key not valid/i.test(txt)) return "That Gemini API key isn't valid. Check it in Setup.";
    if (status === 403) return 'This key is not allowed to use the Gemini API.';
    if (status === 429) return 'Gemini quota hit. Wait a minute and try again.';
    if (status >= 500) return 'Gemini is having a moment. Try again.';
    return `Gemini error ${status}.`;
  }
  async function askGemini(text, img) {
    const now = new Date();
    const parts = [{ text: `Current time: ${pad(now.getHours())}:${pad(now.getMinutes())}.\nWhat I ate: ${text || '(see photo)'}` }];
    if (img) parts.push({ inline_data: { mime_type: 'image/jpeg', data: img.split(',')[1] } });
    const body = JSON.stringify({
      systemInstruction: { parts: [{ text: AI_SYSTEM }] },
      contents: [{ role: 'user', parts }],
      generationConfig: { temperature: 0.2, responseMimeType: 'application/json', responseSchema: AI_SCHEMA },
    });
    const call = m => fetch(`${GEM}/models/${encodeURIComponent(m)}:generateContent`, { method: 'POST', headers: gemHeaders(), body });
    let model = S.aiModel || DEFAULT_MODEL;
    let r;
    try { r = await call(model); } catch { throw new Error('No connection to Gemini.'); }
    if (r.status === 404) {
      const alt = await pickModel().catch(() => null);
      if (alt && alt !== model) { model = alt; S.aiModel = alt; save(); r = await call(alt); }
    }
    if (!r.ok) throw new Error(gemError(r.status, await r.text()));
    const j = await r.json();
    const txt = (j.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('').trim();
    if (!txt) throw new Error(j.promptFeedback?.blockReason ? 'Gemini refused that one.' : 'Gemini sent back nothing. Try again.');
    let out;
    try { out = JSON.parse(txt.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')); } catch { throw new Error("Couldn't read Gemini's answer. Try again."); }
    const items = (out.items || []).map(it => ({ n: String(it.name || 'Food').slice(0, 40), k: Math.max(0, Math.round(Number(it.kcal) || 0)), at: it.eaten_at || null })).filter(it => it.k > 0);
    if (!items.length) throw new Error("Gemini couldn't find any food in that.");
    return { items, comment: out.comment || '', model };
  }

  // Which meal slot does food eaten at minute `min` (of today's day) belong to?
  function slotFor(min, explicit) {
    const key = todayKey();
    const c = S.times.map((t, i) => ({ i, d: Math.abs(t - min), full: mealKcal(key, i) >= S.kcalPerMeal * 0.8 }));
    const pool = explicit ? c : (c.some(x => !x.full) ? c.filter(x => !x.full) : c);
    return pool.sort((a, b) => a.d - b.d)[0].i;
  }
  const nowMin = () => Math.round((Date.now() - parseYmd(todayKey()).getTime()) / 60e3);
  function atToMin(at) {
    const m = /^(\d{1,2}):(\d{2})/.exec(at || '');
    if (!m) return null;
    let v = Number(m[1]) * 60 + Number(m[2]);
    if (v < toMin(S.wake) - 60) v += 1440;
    return v > nowMin() + 30 ? null : v;
  }
  function logItems(items, source) {
    const key = todayKey();
    const nowSlot = slotFor(nowMin(), false);
    const added = items.map(it => {
      const at = atToMin(it.at);
      const m = at === null ? nowSlot : slotFor(at, true);
      const e = { id: uid(), m, n: it.n, k: it.k, t: Date.now(), ai: 1 };
      (S.log[key] = S.log[key] || []).push(e);
      return e;
    });
    if (source) {
      S.aiRecent = [{ t: source.slice(0, 48), items: items.map(({ n, k }) => ({ n, k })) }, ...(S.aiRecent || []).filter(r => r.t !== source.slice(0, 48))].slice(0, 6);
    }
    save();
    return added;
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
    const key = todayKey();
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
      const bySlot = {};
      Q.result.added.forEach(e => { (bySlot[e.m] = bySlot[e.m] || []).push(e); });
      const rows = Object.keys(bySlot).map(Number).sort((a, b) => a - b).map(m => `
        <div class="q-slot"><div class="q-slot-h"><b>Meal ${m + 1}</b> · ${clock12(S.times[m])}<span>${fmtK(mealKcal(key, m))} / ${S.kcalPerMeal}</span></div>
        ${bySlot[m].map(e => `<div class="q-item"><span>${esc(e.n)}</span><b>${e.k}</b></div>`).join('')}</div>`).join('');
      $('#sheet').innerHTML = head + `
        <div class="q-done"><div class="eyebrow">LOGGED</div><div class="q-total">+${fmtK(tot)} <small>kcal</small></div>
        ${Q.result.comment ? `<p class="hint">${esc(Q.result.comment)}</p>` : ''}</div>
        ${rows}
        <p class="hint">Wrong meal or number? Tap the meal on Today to fix it.</p>
        <div class="row-btns"><button class="btn" data-act="ai-undo">Undo</button><button class="btn primary" data-act="ai-again">Log more</button></div>`;
      return;
    }
    const recent = (S.aiRecent || []).map((r, i) => `<button class="q-chip" data-act="ai-recent" data-i="${i}">${esc(r.t)} <b>${fmtK(r.items.reduce((a, x) => a + x.k, 0))}</b></button>`).join('');
    const slot = slotFor(nowMin(), false);
    $('#sheet').innerHTML = head + `
      <textarea id="qText" rows="3" placeholder="What did you eat? e.g. 2 roti canai with dhal and a teh tarik" ${Q.busy ? 'disabled' : ''}>${esc(Q.text)}</textarea>
      ${Q.img ? `<div class="q-photo"><img src="${Q.img}" alt="Your food photo"><button class="x" data-act="ai-noimg" aria-label="Remove photo">×</button></div>` : ''}
      <div class="q-actions">
        <label class="btn sm q-cam ${Q.busy ? 'off' : ''}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>Photo<input type="file" accept="image/*" id="qImg" hidden></label>
        <button class="btn primary" data-act="ai-go" ${Q.busy ? 'disabled' : ''}>${Q.busy ? '<span class="spin"></span>Reading it' : 'Log it'}</button>
      </div>
      ${Q.error ? `<p class="q-err">${esc(Q.error)}</p>` : ''}
      <p class="hint">Goes into <b>meal ${slot + 1}</b> (${clock12(S.times[slot])}) unless you say a time, e.g. "roti canai at 9am, nasi ayam for lunch". Tap the mic on your keyboard to say it instead of typing.</p>
      ${recent ? `<div class="sec-lbl">Again · no AI needed</div><div class="q-chips">${recent}</div>` : ''}`;
    if (!Q.busy && !Q.img) setTimeout(() => { const t = $('#qText'); if (t && !Q.text) t.focus(); }, 250);
  }

  async function testKey() {
    if (!S.aiKey) { toast('Paste your key first'); return; }
    toast('Testing…');
    try {
      const r = await askGemini('1 banana', null);
      toast(`Works: ${r.items[0].n} ≈ ${r.items[0].k} kcal`);
      render();
    } catch (err) { toast(err.message); }
  }

  function renderHistory() {
    const tk = todayKey();
    const target = dailyTarget();
    const days = Array.from({ length: 21 }, (_, j) => addDays(tk, -j)).filter(k => entries(k).length || k === tk);
    const html = days.map(k => {
      const tot = dayKcal(k);
      const groups = S.times.map((t, i) => ({ i, list: entries(k).filter(e => e.m === i) })).filter(g => g.list.length);
      const extra = entries(k).filter(e => e.m === -1);
      if (extra.length) groups.push({ i: -1, list: extra });
      const label = k === tk ? 'Today' : k === addDays(tk, -1) ? 'Yesterday' : parseYmd(k).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
      return `<div class="card">
        <h3>${label}<span class="r"><b class="${tot >= target * 0.9 ? 'ok-t' : ''}">${fmtK(tot)}</b> / ${fmtK(target)}</span></h3>
        <div class="bar" style="margin:0 0 10px"><i style="width:${clamp(tot / target * 100, 0, 100)}%"></i></div>
        ${groups.length ? groups.map(g => `<div class="h-meal"><div class="h-lbl">${g.i === -1 ? 'Extra' : `Meal ${g.i + 1} <small>${clock12(S.times[g.i])}</small>`}</div>
          <ul class="entries">${g.list.map(e => `<li>${e.ai ? '<span class="ai-mark">✦</span>' : ''}${esc(e.n)} · ${e.k}<button class="x" data-act="del-hist" data-k="${k}" data-id="${e.id}" aria-label="Remove">×</button></li>`).join('')}</ul></div>`).join('') : '<p class="hint" style="margin:0">Nothing logged yet.</p>'}
      </div>`;
    }).join('');
    $('#view-history').innerHTML = html;
  }

  /* ---------- sheet ---------- */
  function openQuick() {
    sheetMode = 'quick'; sheetMeal = -2;
    if (!Q.busy) { Q.result = null; Q.error = ''; }
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
      case 'ai-go': { const t = $('#qText'); if (t) Q.text = t.value; runQuick(); break; }
      case 'ai-noimg': Q.img = null; renderSheet(); break;
      case 'ai-again': Q.result = null; renderSheet(); break;
      case 'ai-undo': {
        if (Q.result) { const ids = new Set(Q.result.added.map(e => e.id)); const k = todayKey(); S.log[k] = entries(k).filter(e => !ids.has(e.id)); save(); }
        Q.result = null; renderSheet(); render(); toast('Undone');
        break;
      }
      case 'ai-recent': {
        const r = (S.aiRecent || [])[Number(b.dataset.i)];
        if (r) { const added = logItems(r.items.map(x => ({ ...x, at: null })), r.t); Q.result = { added, comment: '' }; renderSheet(); render(); }
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
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { checkAlerts(); render(); } });

  // first weigh-in seed: keep start weight in history
  if (!S.weights.length) S.weights = [{ d: S.startDate, kg: S.startWeight }];
  S.meals = S.times.length;
  render();
  void minuteTimer;
})();
