# `tools/frontend-deploy` — Firebase Hosting deploy

Publishes `frontend/` (the static bundle) to Firebase Hosting.

`firebase.json` + `.firebaserc` live at the **repo root** — Firebase requires
the config and the `public` dir (`frontend/`) to share a directory tree.
`deploy.sh` runs the firebase CLI from the repo root.

## First-time setup

```bash
npm install -g firebase-tools     # Firebase CLI, once
make firebase-sa                  # once — deploy service account + key
```

`make firebase-sa` creates a `firebase-deployer` service account, grants it
`roles/firebase.admin`, writes a key JSON to `.secrets/` (gitignored), and
records its path in `.env.prod` (`FIREBASE_DEPLOY_KEY`). `firebase-tools`
then authenticates from that key — headless, no browser, no expiry. Run it as
a project Owner. (No `firebase login` needed.)

`.firebaserc` and `frontend/firebase-config.js` are **auto-created** on first
deploy — `.firebaserc` from `GCP_PROJECT` in `.env.prod`, `firebase-config.js`
from the project's Firebase Web app via the CLI. You write neither by hand.

## Deploy

```bash
./deploy.sh                              # live deploy
DEPLOY_TARGET=preview-pr42 ./deploy.sh   # 7-day disposable preview channel
```

Makefile shortcut: `make deploy-frontend`.

The bundle is **static** — it carries no data. The deployed page **requires
sign-in** (`assets/auth.js` — Google / X / GitHub / email+password), then
reads catalysts + graph live from Firestore (`CKG-Robotics`) via
`assets/firestore-source.js` + the Firebase Web SDK. Each sign-in upserts a
`users/{uid}` profile doc. Make sure `make deploy` + `make firestore-sync`
have populated `CKG-Robotics` first, or the page loads its built-in offline
preview. `cards.json` is **not** deployed (it's in `firebase.json`'s `ignore`)
— it's the local-dev data source only, and local dev has no sign-in gate.

### One-time setup so the page works

1. Console → **Authentication** → enable the sign-in providers: Google,
   Email/Password, X (Twitter), GitHub. X + GitHub each need an external
   OAuth app — full steps + callback URLs in `DEPLOYMENT.md`.
2. Deploy the rules once, from the repo root:
   `firebase deploy --only firestore --project <GCP_PROJECT>`

`firestore.rules` is deny-by-default: signed-in read on `CKG-*`, plus each
user read/write on their own `users/{uid}` doc. `--only firestore` replaces
the whole `(default)` database ruleset — safe, since Arboryx's `findings` is
backend-only (Admin SDK bypasses rules).

## What's in `firebase.json`

`firebase.json` (repo root) holds the Hosting config — public dir, rewrites,
cache headers — plus a `firestore` block (`firestore.rules`) and a `storage`
block (`storage.rules`), both pointing into `tools/frontend-deploy/`.
It knows nothing about where the data lives; that's `firebase-config.js`.

**Cache headers** — the static bundle only:

| Path | Cache-Control | Why |
|---|---|---|
| `assets/**` (CSS/JS) | `max-age=86400` | 1 day — okay to be stale briefly. Could go immutable if we add hash suffixes. |
| `*.html` | `max-age=60, must-revalidate` | Short TTL so index.html updates propagate quickly. |

(Catalyst data isn't served by Hosting — it comes from Firestore at runtime.)

**SPA rewrite:** every unknown path falls through to `/index.html`. The frontend router reads the URL hash (`#card=ROB-041726-001`) to decide what to show.

## Daily flow

Once `make deploy` is live, the backend keeps the data current on its own:

```
robotics-ingest (daily) → catalysts + graph → Firestore CKG-Robotics
     │
     ▼
the hosted frontend reads Firestore live in the browser — no redeploy for new data

robotics-render → card PNGs → Firebase Storage   (for social posts, not the site)
```

You only re-run `make deploy-frontend` when the frontend **code** changes —
never just because there's new data.

## Custom domain

`make link-domain` wires the Hosting site to `CUSTOM_DOMAIN` (e.g.
`robotics.arboryx.ai`) end-to-end — one command, no Console clicks:

1. Registers the custom domain with Firebase Hosting (customDomains API).
2. Reads the DNS records Firebase requires (A/AAAA + a TXT ownership record).
3. Creates them in the Cloudflare zone (`CF_ZONE_NAME`) via the Cloudflare API.
4. Polls until Firebase reports the domain active; SSL is auto-provisioned.

One-time setup in `.env.prod`: `CUSTOM_DOMAIN`, `CF_ZONE_NAME`, and
`CF_API_TOKEN` — a Cloudflare API token with **DNS:Edit** on that zone
(Cloudflare → My Profile → API Tokens → Create → "Edit zone DNS").

`dev-utils/link_domain.py` is idempotent: re-run it any time to re-check
provisioning (a cert can take up to ~24h). Records are added **DNS-only**
(no Cloudflare proxy) — required for Firebase verification + cert issuance.

## Rollback

```bash
firebase hosting:clone ${CURRENT}:live ${CURRENT}:previous-$(date +%s)
firebase hosting:rollback
```

Firebase keeps the last 10 versions; one-click rollback in the console.

## Why not GCS + Cloud CDN + Load Balancer

Same result, more configuration:

- GCS+LB: ~6 Terraform resources (bucket, backend, URL map, certificate, forwarding rule, load balancer)
- Firebase Hosting: 1 config file

Switch to GCS+LB only when we need:
- Custom cache header logic beyond what Firebase allows
- Multi-origin routing (A/B tests, canary)
- Full VPC-SC isolation

None apply in Phase 1 or Phase 2.
