/**
 * Tennessee Weather Network - membership worker.
 *
 * Accounts + Stripe + members-only content delivery. Zero npm deps so
 * `wrangler deploy` works with no build step. Local development runs the
 * exact same code with:
 *   - BUCKET  -> rclone `serve http` stand-in for the R2 premium bucket
 *   - no DB   -> users/payments stored as JSON objects in the same bucket
 *   - no Stripe key -> checkout/portal return a clear 503, webhook
 *     signature check is skipped on localhost only
 *
 * Secrets (NEVER in git; `wrangler secret put <NAME>` in production):
 *   JWT_SECRET, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_MONTHLY,
 *   ADMIN_EMAIL, SITE_URL, EXTRA_ORIGINS, R2_ADMIN_TOKEN
 */

const CONFIG = {
  site: null,                    // from SITE_URL, e.g. https://rpleasant12.github.io/http-localhost-8765-
  cookieName: "twn_member",
  sessionDays: 30,
  price: "$4.99/mo",
  monthMs: 31 * 24 * 3600 * 1000,
};

/* ---------------- tiny helpers ---------------- */

const enc = new TextEncoder();

function b64url(buf, json) {
  const b = json ? enc.encode(JSON.stringify(buf)) : buf;
  let s = typeof b === "string" ? btoa(b) : btoa(String.fromCharCode(...new Uint8Array(b)));
  return s.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64urlDecode(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, c => c.charCodeAt(0))));
}
function nowSec() { return Math.floor(Date.now() / 1000); }

async function hmacKey(secret) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}
async function hmacSign(secret, msg) {
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(secret), enc.encode(msg));
  return b64url(new Uint8Array(sig));
}
function timingSafeEq(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

async function hashPassword(pw, saltHex) {
  const salt = saltHex ? hexToBuf(saltHex) : crypto.getRandomValues(new Uint8Array(16));
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations: 100000 },
    await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]),
    256);
  return { salt: bufToHex(salt), hash: bufToHex(new Uint8Array(bits)) };
}
function bufToHex(b) { return [...b].map(x => x.toString(16).padStart(2, "0")).join(""); }
function hexToBuf(h) { return Uint8Array.from(h.match(/.{2}/g).map(x => parseInt(x, 16))); }

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}
function cookieHeader(name, val, days) {
  const exp = days ? new Date(Date.now() + days * 864e5).toUTCString() : "Thu, 01 Jan 1970 00:00:00 GMT";
  return `${name}=${val}; Path=/; HttpOnly; Secure; SameSite=Lax; Expires=${exp}`;
}
function getCookie(req, name) {
  const c = req.headers.get("cookie") || "";
  for (const part of c.split(/;\s*/)) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i) === name) return part.slice(i + 1);
  }
  return null;
}

/* ---------------- JWT (HS256) ---------------- */

async function makeToken(env, user) {
  const payload = { sub: user.email, uid: user.uid, adm: !!user.admin, iat: nowSec(), exp: nowSec() + CONFIG.sessionDays * 86400 };
  const h = b64url({ alg: "HS256", typ: "JWT" }, true);
  const p = b64url(payload, true);
  const sig = await hmacSign(env.JWT_SECRET || "dev-insecure-secret", `${h}.${p}`);
  return `${h}.${p}.${sig}`;
}
async function verifyToken(env, token) {
  if (!token || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const want = await hmacSign(env.JWT_SECRET || "dev-insecure-secret", `${parts[0]}.${parts[1]}`);
  if (!timingSafeEq(want, parts[2])) return null;
  let payload;
  try { payload = b64urlDecode(parts[1]); } catch (_e) { return null; }
  if (!payload || payload.exp < nowSec()) return null;
  return payload;
}
async function currentUser(env, req) {
  return verifyToken(env, getCookie(req, CONFIG.cookieName) || (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, ""));
}

/* ---------------- user stores: D1 (prod) or bucket JSON (dev) ---------------- */

class D1Store {
  constructor(db) { this.db = db; }
  async _init() {
    await this.db.prepare(`CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, pw_hash TEXT, salt TEXT,
      admin INTEGER DEFAULT 0, created INTEGER, premium_until INTEGER DEFAULT 0, stripe_customer TEXT)`)
      .run();
    await this.db.prepare(`CREATE TABLE IF NOT EXISTS payments (
      id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, session_id TEXT UNIQUE,
      amount_cents INTEGER, status TEXT, ts INTEGER)`).run();
  }
  async getUser(email) {
    await this._init();
    const r = await this.db.prepare("SELECT * FROM users WHERE email = ?").bind(email).first();
    return r ? { ...r, admin: !!r.admin } : null;
  }
  async anyAdmin() {
    await this._init();
    const r = await this.db.prepare("SELECT COUNT(*) AS n FROM users WHERE admin = 1").first();
    return r.n > 0;
  }
  async createUser(email, pw, admin) {
    await this._init();
    const { salt, hash } = await hashPassword(pw);
    const n = await this.db.prepare("SELECT COUNT(*) AS n FROM users").first();
    const r = await this.db.prepare(
      "INSERT INTO users (email, pw_hash, salt, admin, created) VALUES (?, ?, ?, ?, ?)")
      .bind(email, hash, salt, admin ? 1 : 0, nowSec()).run();
    return { uid: r.meta.last_row_id, email, admin };
  }
  async verifyUser(email, pw) {
    const u = await this.getUser(email);
    if (!u) return null;
    const { hash } = await hashPassword(pw, u.salt);
    return timingSafeEq(hash, u.pw_hash) ? { uid: u.id, email: u.email, admin: !!u.admin } : null;
  }
  async setPremium(email, untilSec, customerId) {
    await this._init();
    if (customerId) await this.db.prepare("UPDATE users SET stripe_customer = ? WHERE email = ?").bind(customerId, email).run();
    await this.db.prepare("UPDATE users SET premium_until = ? WHERE email = ?").bind(untilSec, email).run();
  }
  async addPayment(email, sessionId, cents, status) {
    await this._init();
    await this.db.prepare("INSERT OR IGNORE INTO payments (email, session_id, amount_cents, status, ts) VALUES (?, ?, ?, ?, ?)")
      .bind(email, sessionId, cents, status, nowSec()).run();
  }
  async listUsers() {
    await this._init();
    const { results } = await this.db.prepare(
      "SELECT id, email, admin, created, premium_until, stripe_customer FROM users ORDER BY id").all();
    return results.map(u => ({ ...u, admin: !!u.admin }));
  }
  async listPayments(limit = 50) {
    await this._init();
    const { results } = await this.db.prepare(
      "SELECT email, amount_cents, status, ts FROM payments ORDER BY ts DESC LIMIT ?").bind(limit).all();
    return results;
  }
}

class BucketStore {
  // dev stand-in: users.json / payments.json objects inside the bucket
  constructor(bucket) { this.b = bucket; }
  async _read(name, fallback) {
    const r = await this.b.fetch(new Request(`http://internal/fetch/${name}`));
    if (!r.ok) return fallback;
    try { return await r.json(); } catch (_e) { return fallback; }
  }
  async _write(name, obj) {
    await this.b.fetch(new Request(`http://internal/fetch/${name}`, { method: "PUT", body: JSON.stringify(obj) }));
  }
  async _users() { return this._read("users.json", {}); }
  async getUser(email) {
    const us = await this._users();
    const u = us[email];
    return u ? { id: u.uid, ...u } : null;
  }
  async anyAdmin() { return Object.values(await this._users()).some(u => u.admin); }
  async createUser(email, pw, admin) {
    const us = await this._users();
    const { salt, hash } = await hashPassword(pw);
    const uid = Object.values(us).reduce((m, u) => Math.max(m, u.uid || 0), 0) + 1;
    us[email] = { uid, pw_hash: hash, salt, admin: !!admin, created: nowSec(), premium_until: 0, stripe_customer: null };
    await this._write("users.json", us);
    return { uid, email, admin: !!admin };
  }
  async verifyUser(email, pw) {
    const u = await this.getUser(email);
    if (!u) return null;
    const { hash } = await hashPassword(pw, u.salt);
    return timingSafeEq(hash, u.pw_hash) ? { uid: u.uid, email, admin: !!u.admin } : null;
  }
  async setPremium(email, untilSec, customerId) {
    const us = await this._users();
    if (us[email]) {
      us[email].premium_until = untilSec;
      if (customerId) us[email].stripe_customer = customerId;
      await this._write("users.json", us);
    }
  }
  async addPayment(email, sessionId, cents, status) {
    const ps = await this._read("payments.json", []);
    if (ps.some(p => p.session_id === sessionId)) return;
    ps.unshift({ email, session_id: sessionId, amount_cents: cents, status, ts: nowSec() });
    await this._write("payments.json", ps.slice(0, 500));
  }
  async listUsers() {
    const us = await this._users();
    return Object.entries(us).map(([email, u]) => ({
      id: u.uid, email, admin: !!u.admin, created: u.created,
      premium_until: u.premium_until, stripe_customer: u.stripe_customer,
    }));
  }
  async listPayments(limit = 50) { return (await this._read("payments.json", [])).slice(0, limit); }
}

function store(env) { return env.DB ? new D1Store(env.DB) : new BucketStore(env.BUCKET); }

/* ---------------- Stripe ---------------- */

async function stripe(env, path, params) {
  const key = env.STRIPE_SECRET_KEY;
  if (!key) return { error: { message: "Stripe is not configured yet (STRIPE_SECRET_KEY missing)." }, status: 503 };
  const r = await fetch(`https://api.stripe.com/v1/${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${key}`, "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params).toString(),
  });
  const data = await r.json();
  return { status: r.status, data };
}

async function verifyStripeSignature(env, req, body) {
  const secret = env.STRIPE_WEBHOOK_SECRET;
  if (!secret) {
    // dev convenience only: unsigned webhooks accepted from localhost
    const u = new URL(req.url);
    if (/^(localhost|127\.0\.0\.1|\[::1\])$/.test(u.hostname)) return true;
    return false;
  }
  const sig = req.headers.get("stripe-signature") || "";
  const ts = (sig.match(/t=(\d+)/) || [])[1];
  const v1 = (sig.match(/v1=([0-9a-f]+)/) || [])[1];
  if (!ts || !v1) return false;
  if (Math.abs(nowSec() - +ts) > 600) return false;
  const expected = await hmacSign(secret, `${ts}.${body}`);
  return timingSafeEq(expected, v1);
}

async function extendPremium(env, store_, email, sessionId, cents, customerId, status) {
  const u = await store_.getUser(email);
  if (!u) return;                       // pre-registration checkout: skipped
  const base = Math.max(nowSec(), u.premium_until || 0);
  await store_.setPremium(email, base + CONFIG.monthMs / 1000, customerId);
  await store_.addPayment(email, sessionId, cents, status);
}

/* ---------------- rate limit (per-isolate, best effort) ---------------- */

const RATE = new Map();
function rateLimited(ip, kind, max, windowMs) {
  const k = `${kind}:${ip}`;
  const now = Date.now();
  const arr = (RATE.get(k) || []).filter(t => now - t < windowMs);
  if (arr.length >= max) { RATE.set(k, arr); return true; }
  arr.push(now); RATE.set(k, arr);
  return false;
}

/* ---------------- premium content storage ----------------
 * Real deployment: an R2 binding (env.BUCKET.get/put). Local dev: rclone
 * `serve http` stand-in proxied over HTTP (BUCKET_BASE). Both paths return
 * { ok, status, body, headers }.
 */
const CT = { png: "image/png", gif: "image/gif", jpg: "image/jpeg", json: "application/json", html: "text/html; charset=utf-8", ico: "image/x-icon" };

function ctFor(path) { return CT[(path.split(".").pop() || "").toLowerCase()] || "application/octet-stream"; }

async function bucketGet(env, path) {
  if (env.BUCKET && typeof env.BUCKET.get === "function") {
    const obj = await env.BUCKET.get(path);
    if (!obj) return { ok: false, status: 404 };
    return { ok: true, status: 200, body: obj.body, headers: new Headers({ "etag": obj.httpEtag || "" }) };
  }
  if (!env.BUCKET) {
    // No bucket binding at all (e.g. R2 not enabled yet): the HTTP stand-in
    // below is a localhost address that cannot exist in production - return
    // a clean 503 instead of an unhandled fetch exception (2026-10-02).
    return { ok: false, status: 503 };
  }
  try {
    const up = await fetch(`${env.BUCKET_BASE || "http://127.0.0.1:8790"}/fetch/${path}`);
    return { ok: up.ok, status: up.status, body: up.ok ? up.body : null, headers: up.headers };
  } catch (_e) {
    return { ok: false, status: 503 };   // stand-in not running (dev)
  }
}

async function bucketPut(env, path, body) {
  if (env.BUCKET && typeof env.BUCKET.put === "function") {
    await env.BUCKET.put(path, body);
    return { ok: true, status: 200 };
  }
  if (!env.BUCKET) return { ok: false, status: 503 };   // no bucket binding
  try {
    const up = await fetch(`${env.BUCKET_BASE || "http://127.0.0.1:8790"}/fetch/${path}`, { method: "PUT", body });
    return { ok: up.ok, status: up.status };
  } catch (_e) {
    return { ok: false, status: 503 };
  }
}

async function proxyPremium(env, req, path) {
  if (!path || path.includes("..") || path.startsWith("/")) return json({ error: "bad path" }, 400);
  const up = await bucketGet(env, `premium/${path}`);
  if (!up.ok) return json({ error: up.status === 503 ? "Premium library is not online yet." : "not found" }, up.status);
  const h = new Headers(up.headers);
  h.set("content-type", ctFor(path));
  h.set("cache-control", "private, max-age=60");
  h.set("x-content-type-options", "nosniff");
  return new Response(up.body, { status: up.status, headers: h });
}

/* ---------------- router ---------------- */

export default {
  async fetch(request, env, ctx) {
    try { return await handle(request, env, ctx); }
    catch (e) { return json({ error: `worker: ${e.message}` }, 500); }
  },
};

async function handle(req, env, ctx) {
  // CORS allow-list for credentialed member calls (member.js uses
  // credentials:"include"). Browsers send Origin as scheme+host only - NO
  // path - so SITE_URL must be reduced to its origin (the full github.io
  // project URL with its path could never match a browser Origin).
  // EXTRA_ORIGINS (comma-separated var) adds mirrors, e.g. the Cloudflare
  // Pages copy; entries ending in ".pages.dev" also match their own
  // project's preview subdomains (<hash>.tnwn-weather.pages.dev).
  const origin = req.headers.get("origin") || "";
  const norm = (s) => (s || "").trim().replace(/\/+$/, "");
  const siteOrigin = (() => { try { return new URL(env.SITE_URL || "").origin; } catch { return ""; } })();
  const allowedList = [siteOrigin,
    ...(env.EXTRA_ORIGINS || "").split(",").map(norm)].filter(Boolean);
  const originHost = origin.replace(/^https?:\/\//, "");
  const allowed = !!origin && allowedList.some((a) => {
    const aHost = a.replace(/^https?:\/\//, "");
    return origin === a
      || (a.endsWith(".pages.dev") && originHost.endsWith("." + aHost));
  });
  const cors = allowed
    ? { "access-control-allow-origin": origin, "access-control-allow-credentials": "true",
        "access-control-allow-headers": "content-type, authorization",
        "access-control-allow-methods": "GET, POST, OPTIONS" } : {};
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  // Attach the CORS headers to EVERY response, not just the preflight:
  // without ACAO on the response itself the browser refuses to read the
  // JSON even after a successful OPTIONS (pre-fix bug: only OPTIONS had
  // CORS, so no browser signup/login could ever complete).
  let res;
  try {
    res = await route(req, env, ctx);
  } catch (e) {
    res = json({ error: `worker: ${e.message}` }, 500);
  }
  if (!cors["access-control-allow-origin"]) return res;
  const h = new Headers(res.headers);
  for (const [k, v] of Object.entries(cors)) h.set(k, v);
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: h });
}

async function route(req, env, ctx) {
  const url = new URL(req.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const ip = req.headers.get("cf-connecting-ip") || "local";
  const store_ = store(env);

  /* ----- public ----- */
  if (req.method === "GET" && path === "/api/config") {
    return json({ site: env.SITE_URL || "", stripeReady: !!env.STRIPE_SECRET_KEY, price: CONFIG.price, worker: url.origin });
  }
  if (req.method === "GET" && path === "/api/me") {
    const u = await currentUser(env, req);
    if (!u) return json({ member: false, admin: false });
    const full = await store_.getUser(u.email);
    return json({
      member: true, admin: !!u.adm, email: u.email,
      premiumUntil: full ? (full.premium_until || 0) * 1000 : 0,
      premiumActive: full ? (full.premium_until || 0) > nowSec() : false,
    });
  }

  /* ----- auth ----- */
  if (path === "/api/auth/signup" && req.method === "POST") {
    if (rateLimited(ip, "auth", 20, 600000)) return json({ error: "Too many attempts - try later." }, 429);
    const { email, password } = await req.json().catch(() => ({}));
    if (!EMAIL_RE.test(email || "")) return json({ error: "Valid email required." }, 400);
    if (!password || password.length < 8) return json({ error: "Password must be at least 8 characters." }, 400);
    if (await store_.getUser(email)) return json({ error: "Account already exists - log in instead." }, 409);
    // bootstrap: the very first account owns the admin dashboard
    const admin = !(await store_.anyAdmin());
    const user = await store_.createUser(email, password, admin);
    const token = await makeToken(env, user);
    return json({ ok: true, member: true, admin, note: admin ? "This first account is the ADMIN account." : undefined },
      200, { "set-cookie": cookieHeader(CONFIG.cookieName, token, CONFIG.sessionDays) });
  }
  if (path === "/api/auth/login" && req.method === "POST") {
    if (rateLimited(ip, "auth", 20, 600000)) return json({ error: "Too many attempts - try later." }, 429);
    const { email, password } = await req.json().catch(() => ({}));
    const user = await store_.verifyUser(email || "", password || "");
    if (!user) return json({ error: "Wrong email or password." }, 401);
    const token = await makeToken(env, user);
    return json({ ok: true, member: true, admin: !!user.admin },
      200, { "set-cookie": cookieHeader(CONFIG.cookieName, token, CONFIG.sessionDays) });
  }
  if (path === "/api/auth/logout" && req.method === "POST") {
    return json({ ok: true }, 200, { "set-cookie": cookieHeader(CONFIG.cookieName, "", 0) });
  }

  /* ----- billing ----- */
  if (path === "/api/billing/checkout" && req.method === "POST") {
    const u = await currentUser(env, req);
    if (!u) return json({ error: "Log in first." }, 401);
    const site = (env.SITE_URL || url.origin).replace(/\/$/, "");
    const modeParams = env.STRIPE_PRICE_MONTHLY
      ? { mode: "subscription", line_items: JSON.stringify([{ price: env.STRIPE_PRICE_MONTHLY, quantity: 1 }]) }
      : { mode: "payment", line_items: JSON.stringify([{ price_data: {
            currency: "usd", unit_amount: 499, quantity: 1,
            product_data: { name: "TNWN Premium - 1 month" } } }]) };
    const r = await stripe(env, "checkout/sessions", {
      customer_email: u.email,
      client_reference_id: u.email,
      metadata: JSON.stringify({ email: u.email }),
      success_url: `${site}/member.html?paid=1`,
      cancel_url: `${site}/pricing.html?canceled=1`,
      ...modeParams,
    });
    if (r.error) return json({ error: r.error.message }, r.status || 503);
    return json({ url: r.data.url });
  }
  if (path === "/api/billing/portal" && req.method === "POST") {
    const u = await currentUser(env, req);
    if (!u) return json({ error: "Log in first." }, 401);
    const full = await store_.getUser(u.email);
    if (!full || !full.stripe_customer)
      return json({ error: "No Stripe customer yet - subscribe first." }, 400);
    const site = (env.SITE_URL || url.origin).replace(/\/$/, "");
    const r = await stripe(env, "billing_portal/sessions", { customer: full.stripe_customer, return_url: `${site}/member.html` });
    if (r.error) return json({ error: r.error.message }, r.status || 503);
    return json({ url: r.data.url });
  }

  /* ----- Stripe webhook ----- */
  if (path === "/api/stripe/webhook" && req.method === "POST") {
    const body = await req.text();
    if (!await verifyStripeSignature(env, req, body)) return json({ error: "bad signature" }, 400);
    let evt;
    try { evt = JSON.parse(body); } catch (_e) { return json({ error: "bad json" }, 400); }
    const t = evt.type, d = evt.data && evt.data.object;
    if (t === "checkout.session.completed" && d) {
      const email = (d.metadata && d.metadata.email) || d.client_reference_id || d.customer_email;
      if (email) await extendPremium(env, store_, email, d.id, d.amount_total || 499, d.customer, t);
    } else if (t === "invoice.paid" && d) {
      const email = d.customer_email || (d.customer && d.customer.email);
      if (email) await extendPremium(env, store_, email, d.id, d.amount_paid || 499, typeof d.customer === "string" ? d.customer : null, t);
    } else if (t === "customer.subscription.deleted" && d) {
      const email = (d.metadata && d.metadata.email) || d.customer_email;
      if (email) await store_.setPremium(email, nowSec());
    }
    return json({ received: true });
  }

  /* ----- members-only content ----- */
  if (req.method === "GET" && (path === "/api/premium" || path.startsWith("/api/premium/"))) {
    const u = await currentUser(env, req);
    if (!u) return json({ error: "Members only." }, 401);
    const full = await store_.getUser(u.email);
    const active = full && (full.premium_until || 0) > nowSec();
    if (!active && !u.adm) return json({ error: "Premium subscription inactive." }, 402);
    if (path === "/api/premium") {
      return proxyPremium(env, req, "index.json");
    }
    return proxyPremium(env, req, path.replace("/api/premium/", ""));
  }

  /* ----- admin ----- */
  if (path.startsWith("/api/admin")) {
    const u = await currentUser(env, req);
    const tokenOk = env.R2_ADMIN_TOKEN && timingSafeEq(req.headers.get("x-admin-token") || "", env.R2_ADMIN_TOKEN);
    if (!tokenOk && (!u || !u.adm)) return json({ error: "Admin only." }, 403);
    if (path === "/api/admin/members" && req.method === "GET") {
      const members = await store_.listUsers();
      return json({
        members,
        total: members.length,
        premiumActive: members.filter(m => (m.premium_until || 0) > nowSec()).length,
      });
    }
    if (path === "/api/admin/payments" && req.method === "GET") {
      const pays = await store_.listPayments(100);
      const mrr = pays.filter(p => p.status !== "refunded")
        .reduce((s, p) => s + (p.amount_cents || 0), 0);
      return json({ payments: pays, revenueCents: mrr, note: "sum of recorded payments (lifetime)" });
    }
    if (path === "/api/admin/grant" && req.method === "POST") {
      const { email, days } = await req.json().catch(() => ({}));
      if (!EMAIL_RE.test(email || "")) return json({ error: "bad email" }, 400);
      const full = await store_.getUser(email);
      if (!full) return json({ error: "no such user" }, 404);
      const base = Math.max(nowSec(), full.premium_until || 0);
      await store_.setPremium(email, base + (+days || 30) * 86400);
      return json({ ok: true, email, until: base + (+days || 30) * 86400 });
    }
    if (path === "/api/admin/upload" && req.method === "PUT" && env.BUCKET) {
      // direct premium-content upload: /api/admin/upload/models/x.png
      const sub = path.replace("/api/admin/upload/", "");
      if (!sub || sub.includes("..")) return json({ error: "bad path" }, 400);
      const r = await bucketPut(env, `premium/${sub}`, await req.arrayBuffer());
      return json({ ok: r.ok, status: r.status });
    }
    return json({ error: "unknown admin route" }, 404);
  }

  return json({ error: "not found", routes: ["/api/config", "/api/me", "/api/auth/*", "/api/billing/*", "/api/stripe/webhook", "/api/premium/*", "/api/admin/*"] }, 404);
}
