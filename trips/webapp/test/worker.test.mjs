// Worker tests: node --test trips/webapp/test/   (no dependencies, no network)
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import worker, { signSync, syncKey, toHex, verifyInitData } from "../worker.js";

const TOKEN = "123456:TEST-token_for-vectors";
const OWNER = "111";
const ORIGIN = "https://trip-app.example.workers.dev";
const now = () => Math.floor(Date.now() / 1000);

// The same vector is checked in trips/splitbot/test_webapp.py (Python's side of the scheme).
const VECTOR_BODY = '{"trip":{"id":"vector"},"generated_at":"2026-01-01T00:00:00+00:00","members":[]}';
const VECTOR_KEY = "825e2d8a08cd3a3e390fb3be38d59beff891fc43734bfeb26c71c8b36bd81f38";
const VECTOR_SIG = "3b8cec0892e10f8593b3f3d5ec34f28c761411eec546367378a3b8196867f2a3";

/** initData as Telegram builds it, signed independently with node:crypto. */
function initData(fields, token = TOKEN) {
  const check = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join("\n");
  const secret = createHmac("sha256", "WebAppData").update(token).digest();
  const hash = createHmac("sha256", secret).update(check).digest("hex");
  return new URLSearchParams({ ...fields, hash }).toString();
}
const userFields = (id, extra = {}) => ({
  auth_date: String(now() - 60),
  query_id: "AAHdF6IQAAAAAN0XohDhrOrc",
  user: JSON.stringify({ id: Number(id), first_name: "Ben", username: "ben" }),
  ...extra,
});

class MemoryKV {
  constructor() { this.m = new Map(); }
  async get(k, type) {
    const v = this.m.get(k);
    if (v === undefined) return null;
    return type === "json" || (type && type.type === "json") ? JSON.parse(v) : v;
  }
  async put(k, v) { this.m.set(k, String(v)); }
  async delete(k) { this.m.delete(k); }
}

function env() {
  return {
    BOT_TOKEN: TOKEN, OWNER_ID: OWNER, TRIPS: new MemoryKV(),
    ASSETS: { fetch: async () => new Response("<!doctype html><title>Trip</title>", { headers: { "Content-Type": "text/html" } }) },
  };
}

function snap(id, members, generated_at = new Date().toISOString()) {
  return {
    v: 1, generated_at,
    trip: { id, name: id.toUpperCase(), status: "open", tz: "Asia/Tokyo" },
    members, rows: [], nets: {}, settle: [], plan: null, wrapped: null,
  };
}

async function sync(e, body, { sig, token = TOKEN } = {}) {
  const raw = typeof body === "string" ? body : JSON.stringify(body);
  const s = sig ?? (await signSync(token, new TextEncoder().encode(raw)));
  return worker.fetch(new Request(`${ORIGIN}/api/sync`, { method: "POST", body: raw, headers: { "X-Sync-Signature": s } }), e);
}

async function trips(e, data, headers = {}) {
  return worker.fetch(new Request(`${ORIGIN}/api/trips`, {
    method: "POST", body: JSON.stringify({ initData: data }), headers: { "Content-Type": "application/json", ...headers },
  }), e);
}

async function seeded() {
  const e = env();
  assert.equal((await sync(e, snap("japan", [{ name: "Ana", tg_id: OWNER }, { name: "Ben", tg_id: "222" }, { name: "Chloe", tg_id: null }]))).status, 200);
  assert.equal((await sync(e, snap("bali", [{ name: "Ana", tg_id: OWNER }, { name: "Dan", tg_id: "333" }]))).status, 200);
  return e;
}

// ------------------------------------------------------------------ initData
test("valid initData is accepted and returns the user", async () => {
  const got = await verifyInitData(initData(userFields(222, { start_param: "japan" })), TOKEN);
  assert.equal(got.user.id, 222);
  assert.equal(got.start_param, "japan");
});

test("a tampered field is rejected", async () => {
  const good = new URLSearchParams(initData(userFields(222)));
  good.set("user", JSON.stringify({ id: 111, first_name: "Ben" })); // claim to be the owner
  assert.equal(await verifyInitData(good.toString(), TOKEN), null);
  const extra = new URLSearchParams(initData(userFields(222)));
  extra.set("start_param", "bali"); // a field added after signing
  assert.equal(await verifyInitData(extra.toString(), TOKEN), null);
});

test("initData signed with another bot's token is rejected", async () => {
  assert.equal(await verifyInitData(initData(userFields(222), "999:OTHER"), TOKEN), null);
});

test("stale or future auth_date is rejected", async () => {
  assert.equal(await verifyInitData(initData(userFields(222, { auth_date: String(now() - 86400 - 5) })), TOKEN), null);
  assert.equal(await verifyInitData(initData(userFields(222, { auth_date: String(now() + 3600) })), TOKEN), null);
  assert.ok(await verifyInitData(initData(userFields(222, { auth_date: String(now() - 86400 + 60) })), TOKEN));
});

test("missing, malformed or duplicated hash fields are rejected", async () => {
  assert.equal(await verifyInitData("", TOKEN), null);
  assert.equal(await verifyInitData("user=%7B%7D&auth_date=1", TOKEN), null);
  assert.equal(await verifyInitData(initData(userFields(222)).replace(/hash=[0-9a-f]+/, "hash=zz"), TOKEN), null);
  assert.equal(await verifyInitData(`${initData(userFields(222))}&user=x`, TOKEN), null);
});

// ------------------------------------------------------------------ /api/trips
test("a member sees only their trips, without anyone's Telegram id", async () => {
  const e = await seeded();
  const res = await trips(e, initData(userFields(222, { start_param: "japan" })));
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.owner, false);
  assert.deepEqual(body.trips.map((t) => t.trip.id), ["japan"]);
  assert.equal(body.trips[0].me, "Ben");
  assert.equal(body.start_param, "japan");
  assert.ok(!JSON.stringify(body).includes("tg_id"));
  assert.equal(res.headers.get("Cache-Control"), "no-store");
});

test("the owner sees every trip; a stranger sees none", async () => {
  const e = await seeded();
  const owner = await (await trips(e, initData(userFields(OWNER)))).json();
  assert.equal(owner.owner, true);
  assert.deepEqual(owner.trips.map((t) => t.trip.id).sort(), ["bali", "japan"]);
  const stranger = await (await trips(e, initData(userFields(555)))).json();
  assert.deepEqual(stranger.trips, []);
});

test("bad initData gets 401; wrong method 405; cross-origin 403", async () => {
  const e = await seeded();
  assert.equal((await trips(e, initData(userFields(222), "999:OTHER"))).status, 401);
  assert.equal((await trips(e, "garbage")).status, 401);
  const get = await worker.fetch(new Request(`${ORIGIN}/api/trips`), e);
  assert.equal(get.status, 405);
  assert.equal((await trips(e, initData(userFields(222)), { Origin: "https://evil.example" })).status, 403);
  assert.equal((await trips(e, initData(userFields(222)), { Origin: ORIGIN })).status, 200);
});

// ------------------------------------------------------------------ /api/sync
test("sync key and signature match the fixed vector shared with Python", async () => {
  assert.equal(toHex(await syncKey(TOKEN)), VECTOR_KEY);
  assert.equal(await signSync(TOKEN, new TextEncoder().encode(VECTOR_BODY)), VECTOR_SIG);
  const e = env();
  const res = await sync(e, VECTOR_BODY, { sig: VECTOR_SIG });
  assert.equal(res.status, 200);
  assert.ok(await e.TRIPS.get("trip:vector"));
});

test("sync stores the snapshot and indexes members", async () => {
  const e = await seeded();
  const index = await e.TRIPS.get("index", "json");
  assert.deepEqual(index.find((t) => t.id === "japan").members, [OWNER, "222"]);
  const stored = await e.TRIPS.get("trip:japan", "json");
  assert.equal(stored.trip.name, "JAPAN");
});

test("bad sync signatures are rejected and nothing is stored", async () => {
  const e = env();
  const body = snap("japan", []);
  assert.equal((await sync(e, body, { token: "999:OTHER" })).status, 401);
  assert.equal((await sync(e, body, { sig: "" })).status, 401);
  assert.equal((await sync(e, body, { sig: "00".repeat(32) })).status, 401);
  const raw = JSON.stringify(body);
  const sig = await signSync(TOKEN, new TextEncoder().encode(raw));
  assert.equal((await sync(e, raw.replace("JAPAN", "HACKED"), { sig })).status, 401); // body changed after signing
  assert.equal(await e.TRIPS.get("trip:japan"), null);
});

test("an older snapshot can't overwrite a newer one; bad ids are refused", async () => {
  const e = env();
  assert.equal((await sync(e, snap("japan", [], "2026-10-05T10:00:00Z"))).status, 200);
  assert.equal((await sync(e, snap("japan", [], "2026-10-05T09:00:00Z"))).status, 409);
  assert.equal((await sync(e, snap("../index", []))).status, 400);
  assert.equal((await sync(e, { trip: { id: "x" } })).status, 400);
});

test("delete removes the trip and its index entry; old delete requests are refused", async () => {
  const e = await seeded();
  assert.equal((await sync(e, { delete: "bali", ts: new Date(Date.now() - 3600e3).toISOString() })).status, 400);
  assert.equal((await sync(e, { delete: "bali", ts: new Date().toISOString() })).status, 200);
  assert.equal(await e.TRIPS.get("trip:bali"), null);
  assert.deepEqual((await e.TRIPS.get("index", "json")).map((t) => t.id), ["japan"]);
  const owner = await (await trips(e, initData(userFields(OWNER)))).json();
  assert.deepEqual(owner.trips.map((t) => t.trip.id), ["japan"]);
});

// ------------------------------------------------------------------ the page
test("the page is served with security headers", async () => {
  const e = env();
  const res = await worker.fetch(new Request(`${ORIGIN}/`), e);
  assert.equal(res.status, 200);
  const csp = res.headers.get("Content-Security-Policy");
  assert.match(csp, /script-src 'self' https:\/\/telegram\.org/);
  assert.match(csp, /connect-src 'self'/);
  assert.doesNotMatch(csp, /unsafe-inline/);
  assert.equal(res.headers.get("X-Content-Type-Options"), "nosniff");
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
  assert.equal((await worker.fetch(new Request(`${ORIGIN}/`, { method: "POST" }), e)).status, 405);
  assert.equal((await worker.fetch(new Request(`${ORIGIN}/api/nope`, { method: "POST" }), e)).status, 404);
});

test("without BOT_TOKEN the API refuses to run", async () => {
  const e = { ...env(), BOT_TOKEN: "" };
  assert.equal((await trips(e, initData(userFields(222)))).status, 503);
});

test("a trip closed more than 7 days ago disappears from the app", async () => {
  const e = env();
  const day = (n) => new Date(Date.now() - n * 86400 * 1000).toISOString().slice(0, 10);
  for (const [id, closed] of [["recent", day(3)], ["old", day(8)]]) {
    const s = snap(id, [{ name: "Ben", tg_id: "222" }]);
    s.trip.status = "closed";
    s.trip.closed = closed;
    assert.equal((await sync(e, s)).status, 200);
  }
  for (const uid of [222, Number(OWNER)]) {
    const body = await (await trips(e, initData(userFields(uid)))).json();
    assert.deepEqual(body.trips.map((t) => t.trip.id), ["recent"]);
  }
});

// ------------------------------------------------------------ app changes
// Rename / add asked for in the app: queued by the Worker, applied by the bot.
// The same ack vector is checked in trips/splitbot/test_webapp.py.
const ACK_BODY = '{"op":"ack","ids":["1791000000000-0a1b2c3d"],"ts":"2026-01-01T00:00:00+00:00"}';
const ACK_SIG = "8aab54f148ffb547bc2ad70105e4972784d532554e6cb4490f6ec91618aa5f9d";

async function act(e, uid, body, headers = {}) {
  const res = await worker.fetch(new Request(`${ORIGIN}/api/action`, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify({ initData: initData(userFields(uid)), trip_id: "japan", ...body }),
  }), e);
  return { status: res.status, body: await res.json() };
}

async function bot(e, path, body, { sig, token = TOKEN } = {}) {
  const raw = typeof body === "string" ? body : JSON.stringify(body);
  const s = sig ?? (await signSync(token, new TextEncoder().encode(raw)));
  const res = await worker.fetch(new Request(`${ORIGIN}${path}`, { method: "POST", body: raw, headers: { "X-Sync-Signature": s } }), e);
  return { status: res.status, body: await res.json() };
}
const listBody = () => ({ op: "list", ts: new Date().toISOString() });
const queued = async (e) => (await bot(e, "/api/actions", listBody())).body.actions;

test("the owner can rename anyone and add people; the change is queued for the bot", async () => {
  const e = await seeded();
  let r = await act(e, OWNER, { action: "rename_member", name: "Chloe", to: "Clo" });
  assert.equal(r.status, 200);
  assert.deepEqual([r.body.ok, r.body.queued], [true, true]);
  r = await act(e, OWNER, { action: "add_member", name: "Mei" });
  assert.equal(r.status, 200);
  const q = await queued(e);
  assert.deepEqual(q.map((a) => [a.action, a.name, a.to]), [["rename_member", "Chloe", "Clo"], ["add_member", "Mei", null]]);
  assert.deepEqual(q[0].by, { tg_id: OWNER, name: "Ana", member: "Ana", owner: true });
  assert.match(q[0].id, /^\d{13}-[0-9a-f]{8}$/);
  // the app shows what's waiting, without who asked
  const view = await (await trips(e, initData(userFields(222)))).json();
  assert.deepEqual(view.trips[0].pending, [{ action: "rename_member", name: "Chloe", to: "Clo" }, { action: "add_member", name: "Mei", to: null }]);
  assert.ok(!JSON.stringify(view).includes(OWNER));
});

test("a member can rename only themselves and can't add people", async () => {
  const e = await seeded();
  assert.equal((await act(e, 222, { action: "rename_member", name: "Ben", to: "Benji" })).status, 200);
  const other = await act(e, 222, { action: "rename_member", name: "Chloe", to: "Clo" });
  assert.equal(other.status, 403);
  assert.match(other.body.error, /only rename yourself/);
  assert.equal((await act(e, 222, { action: "rename_member", name: "Ana", to: "Boss" })).status, 403);
  assert.equal((await act(e, 222, { action: "add_member", name: "Mei" })).status, 403);
  const q = await queued(e);
  assert.deepEqual(q.map((a) => [a.name, a.to, a.by.member, a.by.owner]), [["Ben", "Benji", "Ben", false]]);
});

test("a stranger, another trip's member or bad initData can't queue anything", async () => {
  const e = await seeded();
  assert.equal((await act(e, 555, { action: "rename_member", name: "Ben", to: "X" })).status, 403);
  assert.equal((await act(e, 333, { action: "rename_member", name: "Ben", to: "X" })).status, 403); // Dan is on bali only
  assert.equal((await act(e, 333, { action: "rename_member", name: "Dan", to: "X" })).status, 403); // Dan isn't on japan
  const forged = await worker.fetch(new Request(`${ORIGIN}/api/action`, {
    method: "POST", body: JSON.stringify({ initData: initData(userFields(OWNER), "999:OTHER"), trip_id: "japan", action: "add_member", name: "X" }),
  }), e);
  assert.equal(forged.status, 401);
  assert.equal((await act(e, OWNER, { action: "add_member", name: "Mei" }, { Origin: "https://evil.example" })).status, 403);
  assert.equal((await act(e, OWNER, { action: "add_member", name: "Mei", trip_id: "nope" })).status, 403);
  assert.equal((await act(e, OWNER, { action: "add_member", name: "Mei", trip_id: "../index" })).status, 400);
  assert.equal((await act(e, OWNER, { action: "delete_trip", name: "Mei" })).status, 400);
  assert.deepEqual(await queued(e), []);
});

test("bad or clashing names are refused", async () => {
  const e = await seeded();
  for (const to of ["", " Ben2", "Ben2 ", "a=b", "a;b", "a,b", "a:b", "@ben", "x".repeat(32), 7, null,
    "$(id)", "`id`", 'a"b', "a\\b", "-rf", "a\nb", "a|b", "a&b", "a<b"]) {
    assert.equal((await act(e, OWNER, { action: "rename_member", name: "Chloe", to })).status, 400, String(to));
  }
  assert.equal((await act(e, OWNER, { action: "rename_member", name: "Nobody", to: "X" })).status, 400);
  assert.equal((await act(e, OWNER, { action: "rename_member", name: "Chloe", to: "Chloe" })).status, 400);
  assert.equal((await act(e, OWNER, { action: "rename_member", name: "Chloe", to: "ben" })).status, 409); // case-insensitive clash
  assert.equal((await act(e, OWNER, { action: "add_member", name: "ANA" })).status, 409);
  assert.equal((await act(e, OWNER, { action: "add_member", name: "Mei,Ben" })).status, 400);
  assert.equal((await act(e, OWNER, { action: "add_member", name: "O'Brien-Smith Jr." })).status, 200);
  assert.equal((await act(e, OWNER, { action: "rename_member", name: "Chloe", to: "x".repeat(31) })).status, 200);
  assert.equal((await act(e, 222, { action: "rename_member", name: "Ben", to: "Bén" })).status, 200);
  // a name a queued change will take is taken; one change per person at a time
  assert.equal((await act(e, OWNER, { action: "add_member", name: "bén" })).status, 409);
  assert.equal((await act(e, OWNER, { action: "rename_member", name: "Ben", to: "Benjamin" })).status, 409);
  assert.equal((await queued(e)).length, 3);
});

test("changes are capped per trip, and a closed trip takes none", async () => {
  const e = await seeded();
  for (let i = 0; i < 10; i++) assert.equal((await act(e, OWNER, { action: "add_member", name: `P${i}` })).status, 200);
  assert.equal((await act(e, OWNER, { action: "add_member", name: "P10" })).status, 429);
  const s = snap("japan", [{ name: "Ana", tg_id: OWNER }, { name: "Ben", tg_id: "222" }]);
  s.trip.status = "closed";
  s.trip.closed = new Date().toISOString().slice(0, 10);
  assert.equal((await sync(e, s)).status, 200);
  assert.equal(await e.TRIPS.get("actions:japan"), null); // dropped on close
  assert.equal((await act(e, OWNER, { action: "add_member", name: "Mei" })).status, 409);
  assert.equal((await act(e, 222, { action: "rename_member", name: "Ben", to: "Benji" })).status, 409);
});

test("bot calls must be signed, fresh and for the right op", async () => {
  const e = await seeded();
  await act(e, OWNER, { action: "add_member", name: "Mei" });
  assert.equal((await bot(e, "/api/actions", listBody(), { token: "999:OTHER" })).status, 401);
  assert.equal((await bot(e, "/api/actions", listBody(), { sig: "" })).status, 401);
  const raw = JSON.stringify(listBody());
  const sig = await signSync(TOKEN, new TextEncoder().encode(raw));
  assert.equal((await bot(e, "/api/actions", raw.replace("list", "lisT"), { sig })).status, 401); // changed after signing
  assert.equal((await bot(e, "/api/actions", { op: "list", ts: new Date(Date.now() - 11 * 60e3).toISOString() })).status, 400);
  assert.equal((await bot(e, "/api/actions", { op: "ack", ts: new Date().toISOString() })).status, 400); // wrong op
  assert.equal((await bot(e, "/api/actions", { delete: "japan", ts: new Date().toISOString() })).status, 400);
  const got = await bot(e, "/api/actions", raw, { sig });
  assert.equal(got.status, 200);
  assert.equal(got.body.actions.length, 1);
  const get = await worker.fetch(new Request(`${ORIGIN}/api/actions`), e);
  assert.equal(get.status, 405);
  // a signed list body is not a snapshot
  assert.equal((await bot(e, "/api/sync", listBody())).status, 400);
});

test("ack removes the handled changes only", async () => {
  const e = await seeded();
  await act(e, OWNER, { action: "add_member", name: "Mei" });
  await act(e, 222, { action: "rename_member", name: "Ben", to: "Benji" });
  const [a, b] = await queued(e);
  assert.equal((await bot(e, "/api/actions/ack", { op: "ack", ids: [a.id], ts: new Date().toISOString() }, { token: "999:OTHER" })).status, 401);
  assert.equal((await bot(e, "/api/actions/ack", { op: "ack", ids: [a.id], ts: new Date(Date.now() - 11 * 60e3).toISOString() })).status, 400);
  assert.equal((await bot(e, "/api/actions/ack", { op: "ack", ids: ["../x"], ts: new Date().toISOString() })).status, 400);
  const r = await bot(e, "/api/actions/ack", { op: "ack", ids: [a.id], ts: new Date().toISOString() });
  assert.deepEqual([r.status, r.body.removed], [200, 1]);
  assert.deepEqual((await queued(e)).map((x) => x.id), [b.id]);
  await bot(e, "/api/actions/ack", { op: "ack", ids: [b.id], ts: new Date().toISOString() });
  assert.deepEqual(await queued(e), []);
  assert.equal(await e.TRIPS.get("actions:japan"), null);
});

test("ack signature matches the fixed vector shared with Python", async () => {
  assert.equal(await signSync(TOKEN, new TextEncoder().encode(ACK_BODY)), ACK_SIG);
});

test("API responses keep the strict headers", async () => {
  const e = await seeded();
  const res = await worker.fetch(new Request(`${ORIGIN}/api/action`, {
    method: "POST", body: JSON.stringify({ initData: initData(userFields(OWNER)), trip_id: "japan", action: "add_member", name: "Mei" }),
  }), e);
  assert.equal(res.headers.get("Content-Security-Policy"), "default-src 'none'; frame-ancestors 'none'");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
});
