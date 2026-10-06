// Trip app: a Cloudflare Worker serving the Telegram Mini App and a tiny API.
//
//   GET  /            the single-page app (public/, served through the ASSETS binding)
//   POST /api/trips   {initData} -> the trips this Telegram user is on (all, for the owner)
//   POST /api/sync    a trip snapshot (or {delete, ts}) from the bot session, signed
//   POST /api/action  {initData, trip_id, action, ...} -> queue a change for the bot
//                     (rename_member, add_member); the bot applies it, never this Worker
//   POST /api/actions       {op:"list", ts} -> the queued changes (bot session, signed)
//   POST /api/actions/ack   {op:"ack", ids, ts} -> drop the ones it handled (signed)
//
// The git repo stays the record. The bot session pushes one JSON snapshot per
// trip after each commit (trips/splitbot/webapp_sync.py); this Worker keeps them
// in KV (TRIPS). The only other thing it stores is the small queue of changes
// asked for in the app (actions:<trip id>), until the bot acknowledges them.
//
// Secrets: BOT_TOKEN (the split bot's token), OWNER_ID (the owner's Telegram id).
// Nothing here logs request bodies or initData.

const enc = new TextEncoder();
const DAY = 86400;
const MAX_INIT = 16 * 1024;
const MAX_SYNC = 5 * 1024 * 1024;
const ID_RE = /^[a-z0-9][a-z0-9-]{0,79}$/;
const CLOSED_DAYS = 7; // a closed trip disappears from the app this many days after /endtrip
const MAX_PENDING = 10; // queued app changes per trip, until the bot applies them
const SIGNED_WINDOW = 10 * 60 * 1000; // a signed bot request is good for this long
// A member's name from the app: what splitlog.py accepts (1-31 characters, no
// = ; , : @), narrowed to letters, digits, spaces and . ' - starting with a
// letter or digit, because the bot session passes it on a command line.
const NAME_RE = /^[\p{L}\p{N}][\p{L}\p{M}\p{N} .'\-]{0,30}$/u;
const nameOk = (v) => NAME_RE.test(v) && v.trim() === v;
const NAME_ERR = "A name is 1–31 letters or digits; spaces and . ' - are fine.";
const ACTION_ID_RE = /^[0-9]{13}-[0-9a-f]{8}$/;

// ------------------------------------------------------------------ crypto
async function hmacKey(keyBytes, usage) {
  return crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, [usage]);
}

export async function hmac(keyBytes, msgBytes) {
  return new Uint8Array(await crypto.subtle.sign("HMAC", await hmacKey(keyBytes, "sign"), msgBytes));
}

// crypto.subtle.verify compares in constant time.
async function hmacVerify(keyBytes, msgBytes, sigBytes) {
  return crypto.subtle.verify("HMAC", await hmacKey(keyBytes, "verify"), sigBytes, msgBytes);
}

export function toHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function fromHex64(hex) {
  if (typeof hex !== "string" || !/^[0-9a-fA-F]{64}$/.test(hex)) return null;
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i++) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

/** Telegram WebApp initData check. Returns {user, auth_date, start_param} or null.
 *  secret_key = HMAC_SHA256(key="WebAppData", msg=bot token); hash = hex
 *  HMAC_SHA256(secret_key, the other fields as sorted "key=value" lines). */
export async function verifyInitData(initData, botToken, now = Math.floor(Date.now() / 1000), maxAge = DAY) {
  if (typeof initData !== "string" || !initData || initData.length > MAX_INIT || !botToken) return null;
  const params = new URLSearchParams(initData);
  const sig = fromHex64(params.get("hash"));
  if (!sig) return null;
  const pairs = [];
  const seen = new Set();
  for (const [k, v] of params) {
    if (k === "hash") continue;
    if (seen.has(k)) return null; // a repeated field is never sent by Telegram
    seen.add(k);
    pairs.push([k, v]);
  }
  pairs.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const check = pairs.map(([k, v]) => `${k}=${v}`).join("\n");
  const secret = await hmac(enc.encode("WebAppData"), enc.encode(botToken));
  if (!(await hmacVerify(secret, enc.encode(check), sig))) return null;
  const auth = Number(params.get("auth_date"));
  if (!Number.isInteger(auth) || now - auth > maxAge || auth - now > 300) return null;
  let user;
  try {
    user = JSON.parse(params.get("user") || "null");
  } catch {
    return null;
  }
  if (!user || !/^\d{1,20}$/.test(String(user.id))) return null;
  return { user, auth_date: auth, start_param: params.get("start_param") || null };
}

/** The sync key: HMAC_SHA256(key="TripSync", msg=bot token). Derived on both
 *  sides, so the bot token is the only secret. */
export async function syncKey(botToken) {
  return hmac(enc.encode("TripSync"), enc.encode(botToken));
}

/** Hex signature of a sync body (what X-Sync-Signature carries). */
export async function signSync(botToken, bodyBytes) {
  return toHex(await hmac(await syncKey(botToken), bodyBytes));
}

// ---------------------------------------------------------------- responses
const BASE_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Strict-Transport-Security": "max-age=31536000",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(self), payment=(), usb=()",
  "Cross-Origin-Resource-Policy": "same-origin",
};
// The page may be framed only by Telegram's web clients (phones use a webview).
const PAGE_CSP = [
  "default-src 'none'",
  "script-src 'self' https://telegram.org",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors https://web.telegram.org https://*.telegram.org",
].join("; ");

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...BASE_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
      ...extra,
    },
  });
}

async function page(request, env) {
  if (!env.ASSETS) return new Response("Not deployed with assets", { status: 500, headers: BASE_HEADERS });
  const res = await env.ASSETS.fetch(request);
  const out = new Response(res.body, res);
  for (const [k, v] of Object.entries(BASE_HEADERS)) out.headers.set(k, v);
  out.headers.set("Content-Security-Policy", PAGE_CSP);
  out.headers.set("Cache-Control", "no-cache");
  return out;
}

async function readBody(request, limit) {
  const len = Number(request.headers.get("Content-Length") || 0);
  if (len > limit) return null;
  const buf = new Uint8Array(await request.arrayBuffer());
  return buf.length > limit ? null : buf;
}

// --------------------------------------------------------------------- KV
async function readIndex(env) {
  return (await env.TRIPS.get("index", "json")) || [];
}

function indexEntry(snap) {
  const t = snap.trip;
  return {
    id: t.id,
    name: String(t.name || t.id),
    status: t.status || "open",
    closed: t.closed || null,
    generated_at: snap.generated_at,
    members: (snap.members || []).map((m) => (m && m.tg_id ? String(m.tg_id) : null)).filter(Boolean),
  };
}

async function readQueue(env, tripId) {
  const q = await env.TRIPS.get(`actions:${tripId}`, "json");
  return Array.isArray(q) ? q : [];
}

/** What a viewer gets: the snapshot without Telegram ids, plus who they are on it
 *  and the changes still waiting for the bot (without who asked). */
function forViewer(snap, uid, queue = []) {
  const me = (snap.members || []).find((m) => m && m.tg_id && String(m.tg_id) === uid);
  return {
    ...snap,
    members: (snap.members || []).map((m) => ({ name: m.name })),
    me: me ? me.name : null,
    pending: queue.map((a) => ({ action: a.action, name: a.name, to: a.to || null })),
  };
}

// ---------------------------------------------------------------- handlers
async function apiTrips(request, env) {
  const raw = await readBody(request, MAX_INIT + 1024);
  if (!raw) return json({ error: "too large" }, 413);
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const auth = await verifyInitData(body && body.initData, env.BOT_TOKEN);
  if (!auth) return json({ error: "unauthorised" }, 401);
  const uid = String(auth.user.id);
  const owner = Boolean(env.OWNER_ID) && uid === String(env.OWNER_ID).trim();
  const cutoff = new Date(Date.now() - CLOSED_DAYS * 86400 * 1000).toISOString().slice(0, 10);
  const ids = (await readIndex(env))
    .filter((t) => !(t.status === "closed" && t.closed && t.closed < cutoff)) // closed > 7 days: gone
    .filter((t) => owner || t.members.includes(uid))
    .map((t) => t.id);
  const snaps = await Promise.all(ids.map((id) => env.TRIPS.get(`trip:${id}`, "json")));
  const queues = await Promise.all(ids.map((id) => readQueue(env, id)));
  return json({
    owner,
    start_param: auth.start_param,
    trips: snaps.map((s, i) => s && forViewer(s, uid, queues[i])).filter(Boolean),
  });
}

/** Verify a signed bot request (X-Sync-Signature over the raw body) and parse it.
 *  Returns {body} or {res} (an error response). The body must name its op and
 *  carry a timestamp within SIGNED_WINDOW, so a captured request can't be
 *  replayed later or against another endpoint. */
async function signedBody(request, env, op, limit = 64 * 1024) {
  const raw = await readBody(request, limit);
  if (!raw) return { res: json({ error: "too large" }, 413) };
  const sig = fromHex64(request.headers.get("X-Sync-Signature"));
  if (!sig || !(await hmacVerify(await syncKey(env.BOT_TOKEN), raw, sig))) {
    return { res: json({ error: "bad signature" }, 401) };
  }
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return { res: json({ error: "bad json" }, 400) };
  }
  const ts = Date.parse((body && body.ts) || "");
  if (!body || body.op !== op || !(Math.abs(Date.now() - ts) < SIGNED_WINDOW)) {
    return { res: json({ error: "bad request" }, 400) };
  }
  return { body };
}

const str = (v) => (typeof v === "string" ? v : "");

/** A change asked for in the app. Only queued here: the bot session applies it
 *  with splitlog.py, so the repo stays the record. Who may do what is decided
 *  from the stored snapshot's members (Telegram ids), never from names the
 *  client sends. */
async function apiAction(request, env) {
  const raw = await readBody(request, MAX_INIT + 2048);
  if (!raw) return json({ error: "too large" }, 413);
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json({ error: "bad request" }, 400);
  }
  const auth = await verifyInitData(body && body.initData, env.BOT_TOKEN);
  if (!auth) return json({ error: "unauthorised" }, 401);
  const uid = String(auth.user.id);
  const owner = Boolean(env.OWNER_ID) && uid === String(env.OWNER_ID).trim();
  const tripId = body.trip_id;
  if (typeof tripId !== "string" || !ID_RE.test(tripId)) return json({ error: "bad request" }, 400);

  const snap = await env.TRIPS.get(`trip:${tripId}`, "json");
  const members = (snap && Array.isArray(snap.members) ? snap.members : []).filter((m) => m && typeof m.name === "string");
  const meM = members.find((m) => m.tg_id && String(m.tg_id) === uid);
  if (!snap || (!owner && !meM)) return json({ error: "You're not on this trip." }, 403);
  if ((snap.trip && snap.trip.status) !== "open") return json({ error: "This trip is closed, so it can't be changed." }, 409);

  const action = body.action;
  if (action !== "rename_member" && action !== "add_member") return json({ error: "Unknown action." }, 400);
  if (action === "add_member" && !owner) return json({ error: "Only the trip's owner can add people." }, 403);

  const queue = await readQueue(env, tripId);
  if (queue.length >= MAX_PENDING) {
    return json({ error: "A few changes are already waiting for the bot. Try again in a minute." }, 429);
  }
  const lc = (x) => x.toLowerCase();
  // Names taken: everyone on the trip, plus names that queued changes will add.
  const taken = (except) => [
    ...members.map((m) => m.name).filter((n) => n !== except),
    ...queue.map((a) => (a.action === "add_member" ? a.name : a.to)).filter(Boolean),
  ].map(lc);

  const name = str(body.name);
  let to = null;
  let target = null;
  if (action === "rename_member") {
    target = members.find((m) => m.name === name);
    if (!target) return json({ error: "That person isn't on this trip." }, 400);
    if (!owner && target !== meM) return json({ error: "You can only rename yourself. Ask the trip's owner." }, 403);
    to = str(body.to);
    if (!nameOk(to)) return json({ error: NAME_ERR }, 400);
    if (to === name) return json({ error: "That's already the name." }, 400);
    if (queue.some((a) => a.action === "rename_member" && a.name === name)) {
      return json({ error: `A change to ${name} is already waiting for the bot.` }, 409);
    }
    if (taken(name).includes(lc(to))) return json({ error: `${to} is already on this trip.` }, 409);
  } else {
    if (!nameOk(name)) return json({ error: NAME_ERR }, 400);
    if (taken(null).includes(lc(name))) return json({ error: `${name} is already on this trip.` }, 409);
  }

  const u = auth.user;
  const tgName = [u.first_name, u.last_name].filter((x) => typeof x === "string" && x).join(" ").slice(0, 64) || "someone";
  const rnd = toHex(crypto.getRandomValues(new Uint8Array(4)));
  const item = {
    id: `${Date.now()}-${rnd}`,
    trip_id: tripId,
    action,
    name,
    to,
    by: { tg_id: uid, name: meM ? meM.name : tgName, member: meM ? meM.name : null, owner },
    at: new Date().toISOString(),
  };
  queue.push(item);
  await env.TRIPS.put(`actions:${tripId}`, JSON.stringify(queue));
  return json({ ok: true, queued: true, id: item.id });
}

/** The bot session collects queued changes for open trips. */
async function apiActions(request, env) {
  const { res } = await signedBody(request, env, "list");
  if (res) return res;
  const ids = (await readIndex(env)).filter((t) => t.status === "open").map((t) => t.id);
  const queues = await Promise.all(ids.map((id) => readQueue(env, id)));
  return json({ ok: true, actions: queues.flat() });
}

/** The bot session acknowledges changes it handled (applied or refused). */
async function apiActionsAck(request, env) {
  const { body, res } = await signedBody(request, env, "ack");
  if (res) return res;
  const ids = Array.isArray(body.ids) ? body.ids.filter((x) => typeof x === "string" && ACTION_ID_RE.test(x)) : [];
  if (!ids.length || ids.length > 100) return json({ error: "bad ids" }, 400);
  const drop = new Set(ids);
  const index = await readIndex(env);
  let removed = 0;
  for (const t of index) {
    const queue = await readQueue(env, t.id);
    const keep = queue.filter((a) => !drop.has(a.id));
    if (keep.length === queue.length) continue;
    removed += queue.length - keep.length;
    if (keep.length) await env.TRIPS.put(`actions:${t.id}`, JSON.stringify(keep));
    else await env.TRIPS.delete(`actions:${t.id}`);
  }
  return json({ ok: true, removed });
}

async function apiSync(request, env) {
  const raw = await readBody(request, MAX_SYNC);
  if (!raw) return json({ error: "too large" }, 413);
  const sig = fromHex64(request.headers.get("X-Sync-Signature"));
  if (!sig || !env.BOT_TOKEN || !(await hmacVerify(await syncKey(env.BOT_TOKEN), raw, sig))) {
    return json({ error: "bad signature" }, 401);
  }
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json({ error: "bad json" }, 400);
  }
  const now = Date.now();
  const index = await readIndex(env);

  if (body && typeof body.delete === "string") {
    const ts = Date.parse(body.ts || "");
    if (!ID_RE.test(body.delete) || !(Math.abs(now - ts) < 10 * 60 * 1000)) {
      return json({ error: "bad delete" }, 400);
    }
    await env.TRIPS.delete(`trip:${body.delete}`);
    await env.TRIPS.delete(`actions:${body.delete}`);
    await env.TRIPS.put("index", JSON.stringify(index.filter((t) => t.id !== body.delete)));
    return json({ ok: true, deleted: body.delete });
  }

  const t = body && body.trip;
  const gen = Date.parse((body && body.generated_at) || "");
  if (!t || typeof t.id !== "string" || !ID_RE.test(t.id) || !Array.isArray(body.members) || !gen) {
    return json({ error: "bad snapshot" }, 400);
  }
  if (gen > now + 10 * 60 * 1000) return json({ error: "snapshot from the future" }, 400);
  const old = index.find((x) => x.id === t.id);
  if (old && Date.parse(old.generated_at) > gen) return json({ error: "stale snapshot" }, 409);
  await env.TRIPS.put(`trip:${t.id}`, new TextDecoder().decode(raw));
  // A closed trip takes no more changes: drop any still queued.
  if (t.status === "closed" && (await env.TRIPS.get(`actions:${t.id}`))) await env.TRIPS.delete(`actions:${t.id}`);
  const next = index.filter((x) => x.id !== t.id);
  next.push(indexEntry(body));
  await env.TRIPS.put("index", JSON.stringify(next));
  return json({ ok: true, id: t.id, bytes: raw.length });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) {
      const route = {
        "/api/trips": apiTrips, "/api/sync": apiSync, "/api/action": apiAction,
        "/api/actions": apiActions, "/api/actions/ack": apiActionsAck,
      }[url.pathname];
      if (!route) return json({ error: "not found" }, 404);
      if (request.method !== "POST") return json({ error: "method not allowed" }, 405, { Allow: "POST" });
      const origin = request.headers.get("Origin");
      if (origin && origin !== url.origin) return json({ error: "forbidden" }, 403);
      if (!env.BOT_TOKEN) return json({ error: "not configured" }, 503);
      return route(request, env);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { ...BASE_HEADERS, Allow: "GET, HEAD" } });
    }
    return page(request, env);
  },
};
