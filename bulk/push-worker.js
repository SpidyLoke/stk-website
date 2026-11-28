// Bulk Clock push server: a Cloudflare Worker that sends meal notifications to your phone,
// even when the app is closed.
//
// Setup (free plan is enough):
//   1. Create a Worker, paste this whole file in, Deploy.
//   2. Create a KV namespace and bind it to the Worker with the variable name BULK.
//   3. Add a Cron trigger: * * * * *  (every minute).
//   4. Paste the Worker's URL into Bulk Clock → Setup → Push notifications.
//
// The app sends its meal times, what you've logged, and its own push keys. Nothing else is stored.

const SUB = 'https://spidyloke.github.io/stk-website/bulk/';
const ROLL_MIN = 4 * 60; // a "day" runs 4am to 4am, same as the app

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { ...CORS, 'Content-Type': 'application/json' } });

export default {
  async fetch(req, env) {
    if (req.method === 'OPTIONS') return new Response(null, { headers: CORS });
    if (!env.BULK) return json({ ok: false, error: 'KV namespace not bound. Add a KV binding named BULK.' }, 500);
    const url = new URL(req.url);

    if (req.method === 'GET') {
      const lastCron = Number(await env.BULK.get('meta:lastCron')) || null;
      return json({ ok: true, app: 'bulk-clock-push', v: 1, lastCron });
    }
    if (req.method !== 'POST') return json({ ok: false, error: 'POST only' }, 405);

    let body;
    try { body = await req.json(); } catch { return json({ ok: false, error: 'Bad JSON' }, 400); }
    if (!body || typeof body.id !== 'string' || !/^[a-f0-9]{32}$/.test(body.id)) return json({ ok: false, error: 'Bad id' }, 400);
    const key = 'dev:' + body.id;

    if (url.pathname === '/subscribe') {
      if (!body.sub?.endpoint || !body.sub?.keys?.p256dh || !body.sub?.keys?.auth || !body.vapid?.publicKey || !body.vapid?.privateJwk) {
        return json({ ok: false, error: 'Missing subscription or keys' }, 400);
      }
      await env.BULK.put(key, JSON.stringify({ ...clean(body), sub: body.sub, vapid: body.vapid, updated: Date.now() }));
      const ids = JSON.parse((await env.BULK.get('meta:ids')) || '[]');
      if (!ids.includes(body.id)) { ids.push(body.id); await env.BULK.put('meta:ids', JSON.stringify(ids)); }
      return json({ ok: true });
    }

    const dev = JSON.parse((await env.BULK.get(key)) || 'null');
    if (!dev) return json({ ok: false, error: 'Not subscribed' }, 404);

    if (url.pathname === '/sync') {
      await env.BULK.put(key, JSON.stringify({ ...dev, ...clean(body), updated: Date.now() }));
      return json({ ok: true });
    }
    if (url.pathname === '/test') {
      const r = await sendPush(dev, { title: 'Bulk Clock push works', body: 'You will get one of these at every meal, even with the app closed.', tag: 'test' });
      return json({ ok: r.ok, status: r.status, detail: r.ok ? '' : (await r.text()).slice(0, 300) });
    }
    if (url.pathname === '/unsubscribe') {
      await forget(env, body.id);
      return json({ ok: true });
    }
    return json({ ok: false, error: 'Unknown path' }, 404);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, event.scheduledTime));
  },
};

// Only keep the schedule fields we use, with sane types.
function clean(b) {
  const out = {};
  if (Array.isArray(b.times)) out.times = b.times.map(Number).filter(n => Number.isFinite(n) && n >= 0 && n < 2880).slice(0, 12);
  if (Number.isFinite(b.tz)) out.tz = Math.max(-840, Math.min(840, Math.round(b.tz)));
  if (Number.isFinite(b.kcal)) out.kcal = Math.round(b.kcal);
  if (Number.isFinite(b.nudge)) out.nudge = Math.max(0, Math.min(120, Math.round(b.nudge)));
  if (typeof b.deadline === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(b.deadline)) out.deadline = b.deadline;
  if (Array.isArray(b.ideas)) out.ideas = b.ideas.map(s => String(s).slice(0, 80)).slice(0, 8);
  if (b.done && typeof b.done === 'object') {
    out.done = {};
    for (const [k, v] of Object.entries(b.done).slice(-3)) if (/^\d{4}-\d{2}-\d{2}$/.test(k) && Array.isArray(v)) out.done[k] = v.map(Number);
  }
  if (typeof b.paused === 'boolean') out.paused = b.paused;
  return out;
}

async function forget(env, id) {
  await env.BULK.delete('dev:' + id);
  const ids = JSON.parse((await env.BULK.get('meta:ids')) || '[]').filter(x => x !== id);
  await env.BULK.put('meta:ids', JSON.stringify(ids));
}

const pad = n => String(n).padStart(2, '0');
const ymdUTC = d => d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate());

async function tick(env, scheduledTime) {
  const minute = Math.floor(scheduledTime / 60000);
  if (minute % 10 === 0) await env.BULK.put('meta:lastCron', String(scheduledTime));
  const ids = JSON.parse((await env.BULK.get('meta:ids')) || '[]');
  for (const id of ids) {
    const dev = JSON.parse((await env.BULK.get('dev:' + id)) || 'null');
    if (!dev || dev.paused || !Array.isArray(dev.times) || !dev.times.length) continue;
    // phone-local clock, then the 4am-to-4am day the app uses
    const localMin = minute + (dev.tz || 0);
    const dayStartMin = Math.floor((localMin - ROLL_MIN) / 1440) * 1440;
    const dayKey = ymdUTC(new Date(dayStartMin * 60000));
    const nowMin = localMin - dayStartMin;
    if (dev.deadline && dayKey > dev.deadline) continue;
    const done = new Set((dev.done && dev.done[dayKey]) || []);
    const n = dev.times.length;
    for (let i = 0; i < n; i++) {
      if (done.has(i)) continue;
      const t = dev.times[i];
      const next = i < n - 1 ? dev.times[i + 1] : t + 180;
      const idea = dev.ideas && dev.ideas.length ? ` Idea: ${dev.ideas[(i + dayStartMin / 1440) % dev.ideas.length]}` : '';
      let msg = null;
      if (nowMin === t) {
        msg = { title: `Eat now: meal ${i + 1} of ${n}`, body: `~${dev.kcal || 500} kcal.${idea}`, tag: 'meal' };
      } else if (dev.nudge && nowMin === t + dev.nudge && t + dev.nudge < next) {
        msg = { title: `Meal ${i + 1} still not logged`, body: 'Eat something now, even a shake, then log it.', tag: 'meal' };
      }
      if (!msg) continue;
      const r = await sendPush(dev, msg);
      if (r.status === 404 || r.status === 410) { await forget(env, id); break; } // phone dropped the subscription
    }
  }
}

/* ---------- Web Push (RFC 8291 encryption + RFC 8292 VAPID), WebCrypto only ---------- */
const te = new TextEncoder();
const b64e = u8 => btoa(String.fromCharCode(...u8)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const b64d = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), c => c.charCodeAt(0));
const concat = (...arrs) => { const out = new Uint8Array(arrs.reduce((a, x) => a + x.length, 0)); let o = 0; for (const x of arrs) { out.set(x, o); o += x.length; } return out; };

async function hkdf(salt, ikm, info, len) {
  const key = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt, info }, key, len * 8));
}

async function encrypt(keys, text) {
  const uaPub = b64d(keys.p256dh), auth = b64d(keys.auth);
  const as = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const asPub = new Uint8Array(await crypto.subtle.exportKey('raw', as.publicKey));
  const uaKey = await crypto.subtle.importKey('raw', uaPub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: 'ECDH', public: uaKey }, as.privateKey, 256));
  const ikm = await hkdf(auth, shared, concat(te.encode('WebPush: info\0'), uaPub, asPub), 32);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const cek = await hkdf(salt, ikm, te.encode('Content-Encoding: aes128gcm\0'), 16);
  const nonce = await hkdf(salt, ikm, te.encode('Content-Encoding: nonce\0'), 12);
  const aes = await crypto.subtle.importKey('raw', cek, 'AES-GCM', false, ['encrypt']);
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce }, aes, concat(te.encode(text), new Uint8Array([2]))));
  const header = new Uint8Array(86);
  header.set(salt, 0);
  new DataView(header.buffer).setUint32(16, 4096);
  header[20] = 65;
  header.set(asPub, 21);
  return concat(header, ct);
}

async function vapidJwt(privateJwk, aud) {
  const jwk = { kty: privateJwk.kty, crv: privateJwk.crv, x: privateJwk.x, y: privateJwk.y, d: privateJwk.d };
  const key = await crypto.subtle.importKey('jwk', jwk, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const head = b64e(te.encode(JSON.stringify({ typ: 'JWT', alg: 'ES256' })));
  const claims = b64e(te.encode(JSON.stringify({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: SUB })));
  const sig = new Uint8Array(await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key, te.encode(head + '.' + claims)));
  return head + '.' + claims + '.' + b64e(sig);
}

async function sendPush(dev, payload) {
  const endpoint = dev.sub.endpoint;
  const jwt = await vapidJwt(dev.vapid.privateJwk, new URL(endpoint).origin);
  const body = await encrypt(dev.sub.keys, JSON.stringify({ ...payload, url: SUB }));
  return fetch(endpoint, {
    method: 'POST',
    headers: {
      TTL: '900',
      Urgency: 'high',
      'Content-Encoding': 'aes128gcm',
      'Content-Type': 'application/octet-stream',
      Authorization: `vapid t=${jwt}, k=${dev.vapid.publicKey}`,
    },
    body,
  });
}

