# TNWN Membership — one-time production setup (~30 minutes, all free tier)

The membership system ships **dormant**: the public site builds exactly as
today until you flip one switch (`MEMBER_WORKER_URL` in `config.py`) after
the worker below is deployed. Nothing user-facing changes until then.

Architecture (what runs where):

| Piece | Runs on | What it does |
|---|---|---|
| Public site | GitHub Pages (unchanged) | Everything free: radar, forecasts, basic models |
| `tnwn-members` Worker | Cloudflare Workers (free) | Accounts, Stripe checkout/webhooks, members-only content proxy, admin API |
| `tnwn-members` D1 | Cloudflare D1 (free) | Users + payment records |
| `tnwn-premium` R2 | Cloudflare R2 (10 GB free) | Members-only images/library (premium/ tree) |
| Premium content build | this PC (updater) | Renders member-only AI-model maps into R2 each cycle |

Secrets live ONLY in Cloudflare (`wrangler secret put`). The git repo never
sees Stripe keys or tokens.

## 1. Cloudflare account + tools (5 min)
1. Create a free account at https://dash.cloudflare.com (no card needed).
2. Install Node LTS from https://nodejs.org, then in this project:
   `npm install -g wrangler` (or use `npx wrangler` everywhere below).
3. `wrangler login` (browser popup).

## 2. Create the database + bucket (2 min)
```bash
wrangler d1 create tnwn-members      # copy the printed database_id
wrangler r2 bucket create tnwn-premium
```
Paste the `database_id` into `wrangler.toml` (replaces PASTE_D1_ID_AFTER_CREATE).

## 3. Stripe (10 min, test mode)
1. Create the account at https://dashboard.stripe.com/register (test mode is
   on by default; going live later needs bank details + a toggle).
2. From the test dashboard:
   - Developers → API keys → copy the **Secret key** (`sk_test_...`).
   - Billing → Products → create "TNWN Premium", price **$4.99 / month
     recurring** → copy the **price ID** (`price_...`).
   - Developers → Webhooks → add endpoint
     `https://tnwn-members.<your-subdomain>.workers.dev/api/stripe/webhook`
     with events `checkout.session.completed`, `invoice.paid`,
     `customer.subscription.deleted` → copy the **signing secret** (`whsec_...`).

## 4. Deploy + secrets (5 min)
```bash
wrangler d1 execute tnwn-members --remote --command "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, pw_hash TEXT, salt TEXT, admin INTEGER DEFAULT 0, created INTEGER, premium_until INTEGER DEFAULT 0, stripe_customer TEXT);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, session_id TEXT UNIQUE, amount_cents INTEGER, status TEXT, ts INTEGER);"
wrangler deploy
wrangler secret put JWT_SECRET            # any long random string
wrangler secret put STRIPE_SECRET_KEY     # sk_test_...
wrangler secret put STRIPE_PRICE_MONTHLY  # price_...
wrangler secret put STRIPE_WEBHOOK_SECRET # whsec_...
```
Note the worker URL it prints (also on the dashboard): e.g.
`https://tnwn-members.roberts-account.workers.dev`

## 5. Fill the premium bucket (2 min)
```bash
wrangler r2 object put tnwn-premium/premium/index.json --file premium/index.json
for f in premium/models/*;  do wrangler r2 object put tnwn-premium/$f --file $f; done
for f in premium/longrange/**/*; do wrangler r2 object put tnwn-premium/$f --file $f; done
```

## 6. Flip the site on (1 min)
- `config.py`: set `MEMBER_WORKER_URL = "https://tnwn-members.<your-subdomain>.workers.dev"`
- Next updater cycle rebuilds + publishes the live member UI automatically
  (pricing.html / member.html / admin.html, footer strip, Models teaser).
- Visit the site → ⭐ Premium → **create the first account: it becomes the
  ADMIN account automatically.**

## 7. Ongoing premium content (optional but recommended)
`premium/index.json` + images are the member library. The local dev bucket
mirrors this via rclone (`.freebuff/tools/rclone.exe serve http`). For the
real R2, either re-run step 5 after cycles, or have the updater call the
worker's admin upload endpoint (`PUT /api/admin/upload/...` with the
`R2_ADMIN_TOKEN` secret). Ask and it can be wired into the cycle.

## Local development (already working, no cloud needed)
- `rclone serve http` stand-in for R2 runs via `.freebuff/tools/rclone.exe`
  (premium/ folder, port 8790).
- `wrangler dev` runs the worker locally at `127.0.0.1:8787` — set
  `MEMBER_WORKER_URL = "http://127.0.0.1:8787"` and skip secrets (Stripe
  endpoints return a clear "not configured" until keys exist; the webhook
  accepts unsigned calls from localhost only).

## What each tier gets (current build)
FREE (unchanged, nothing removed): current weather, forecasts, live radar +
future cast, severe/storms centers, national, core model walls, tropical,
winter — everything already on the site.

PREMIUM ($4.99/mo): members-only library at member.html — advanced AI-model
maps (GraphCast omega + PWAT, Pangu 500mb temps, Aurora surface analysis,
FourCastNet vorticity), the long-range CFSv2 monthly library, ad-free
everywhere. Placeholder ad slots (house ads for now) sit on index/models;
swap in a real network tag in `_AD_TAGS` when you sign one up.
