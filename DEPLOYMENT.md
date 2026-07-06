# Deployment

Two environments, two config files, nothing else to memorize.

| Environment | Config file | Run path |
|---|---|---|
| **Local** | `.env` | docker-compose stack on your machine |
| **Prod** | `.env.prod` | Cloud Function + Cloud Run + Firebase, on GCP |

`.env` and `.env.prod` have the same *shape* but different *values* (local
flags off, prod flags on). You never hand-edit Terraform or Cloud Run config —
`make deploy` translates `.env.prod` into everything downstream.

For the day-to-day reference (commands, troubleshooting), see
`OPERATING_MANUAL.md`. This file is deploy-only.

---

## Local

Prereqs: Docker, an SA JSON key (or `gcloud auth application-default login`)
with `roles/datastore.user` on `sample-gcp-project-id`, a Gemini API key.

```bash
cp .env.example .env          # 1. fill GOOGLE_APPLICATION_CREDENTIALS, GEMINI_API_KEY
cp config/config.yaml.example config/config.yaml  # set firestore.project (or rely on GCP_PROJECT)
make doctor                   # 2. preflight — docker memory, ports, creds
make setup                    # 3. build images + init DuckDB
make up                       # 4. start the stack
make firestore-ping           # 5. verify Firestore read auth (no LLM cost)
make ingest LIMIT=5           # 6. first bounded ingest — eyeball a few
make ingest                   # 7. drain the rest
make render-batch             # 8. render card PNGs
make frontend                 # 9. open http://localhost:8000
```

Everything stays local: catalysts in `data/robotics.duckdb`, cards in
`data/exports/cards.json`, PNGs in `data/exports/card_images/`. No Firestore,
no Firebase Storage — `FIRESTORE_EXPORT_ENABLED` / `STORAGE_UPLOAD_ENABLED`
are `false` in `.env`.

Daily loop afterward: `make up` → `make ingest` → `make render-batch`.

---

## Prod

### One-time setup

```bash
gcloud auth login                         # 1. authenticate gcloud
gcloud config set project sample-gcp-project-id
cp .env.prod.example .env.prod             # 2. fill in — see below
$EDITOR .env.prod
```

`.env.prod` needs: `GCP_PROJECT`, `REGION`, bucket names, schedules, and the
`GEMINI_API_KEY` secret. The prod *behaviour* flags are fixed ON inside
Terraform — not in `.env.prod`.

### Deploy

```bash
make deploy
```

One command. It runs `deploy-preflight` (checks gcloud auth + IAM admin +
`.env.prod`), then `dev-utils/deploy.sh`, which:

1. generates `tools/infra/prod.auto.tfvars` from `.env.prod`
2. bootstraps Terraform — data bucket, Artifact Registry repo, secret containers
3. pushes secret *values* to Secret Manager (never into Terraform state)
4. zips the ingest function source → uploads to the data bucket
5. builds + pushes the `robotics-render` image (Cloud Build)
6. `terraform apply` — wires Cloud Function, Cloud Run, Scheduler, Pub/Sub

The final `terraform apply` is interactive — review the plan, type `yes`.
Re-run `make deploy` any time code or `.env.prod` changes; the image tag is
the git SHA, so every change redeploys cleanly.

**robotics-og** — a second Cloud Run service running the *same*
`robotics-render` image with `OG_ONLY=true`. It serves social-preview (Open
Graph) deep links: `GET /card/<id>` returns per-card OG tags for crawlers
(then redirects humans to `/?card=<id>`), and `GET /card-img/<id>.png`
streams the already-rendered PNG out of the private cards bucket. Firebase
Hosting rewrites `/card/**` and `/card-img/**` to it (firebase.json), so it
carries an `allUsers` invoker binding — robotics-render itself stays
IAM-locked. Shipping a change to the /card routes needs `make deploy`
(`FORCE=1 make deploy` if the git tag hasn't changed, since the image build
is skipped when the tag already exists) **and** `make deploy-frontend` for
the Hosting rewrites.

### First-time data backfill (optional)

To put your local graph live immediately instead of waiting for the first
scheduled ingest:

```bash
make firestore-sync
```

Pushes the current **local** DuckDB's catalysts + graph to Firestore
`CKG-Robotics`, and local PNGs to Firebase Storage. One-time — after this,
prod ingest keeps Firestore current on its own.

### Frontend

The deployed page **requires sign-in** (Google / email+password / phone)
and reads catalysts + graph **live from Firestore** (`CKG-Robotics`) in the
browser — no `cards.json` is deployed. Local dev still uses `cards.json` with
no sign-in gate; the page picks prod-vs-local by whether a real
`firebase-config.js` is present (`assets/auth.js`, `assets/firestore-source.js`).
Every sign-in upserts a `users/{uid}` profile doc in Firestore.

**Public preview** — before sign-in, the landing page shows the newest 4
real catalyst cards (no graph) under the sign-in modal: a genuine peek.
`deploy.sh` snapshots them from Firestore into `frontend/preview.json` (a
static, public subset) at deploy time via `build_preview.py`. The full
catalyst set + the graph stay auth-gated in Firestore and load after sign-in.

Deploy the bundle:

```bash
make firebase-sa       # once — creates the deploy service account + key
make deploy-frontend
```

Separate from `make deploy` (Firebase Hosting needs `firebase-tools`).
`firebase.json` + `.firebaserc` live at the repo root (Firebase requires the
config and the `public` dir to share a tree); `.firebaserc` and
`frontend/firebase-config.js` are auto-created on first deploy.
`make deploy-frontend` deploys the **static bundle only** (`--only hosting`).

`make firebase-sa` creates a dedicated `firebase-deployer` service account,
grants it `roles/firebase.admin`, downloads a key JSON to `.secrets/`
(gitignored), and records its path in `.env.prod` as `FIREBASE_DEPLOY_KEY`.
`firebase-tools` authenticates from that key — headless, no browser, no
expiry. Run it once, as a project Owner (gcloud CLI authenticated). To rotate
the key, delete `.secrets/firebase-deployer.json` and re-run.

**Auth setup** (Firebase Console → project `sample-gcp-project-id`
→ **Authentication** → **Get started** if first use → **Sign-in method**).
Live providers — the sign-in modal renders all three:

1. **Google** — Add provider → Google → Enable → pick a support email → Save.
2. **Email/Password** — Add provider → Email/Password → Enable → Save.
3. **Phone** — Add provider → Phone → Enable → Save. The modal does an
   invisible reCAPTCHA, then SMS code. Free SMS quota is small — watch it.

Deferred — **X and GitHub**. They need an external OAuth app each, and the
modal does **not** render their buttons yet (`SOCIAL` in `assets/auth.js`).
To add one: create the OAuth app, enable the provider in the Console with its
key/secret + callback `https://sample-gcp-project-id.firebaseapp.com/__/auth/handler`,
then add `'twitter'` / `'github'` to `SOCIAL` and redeploy.

The Hosting domains (`*.web.app`, `*.firebaseapp.com`) are authorized for
sign-in automatically. A custom domain must be added under
**Authentication → Settings → Authorized domains**.

**Then deploy the Firestore Security Rules** (once, from the repo root):

```bash
firebase deploy --only firestore --project <GCP_PROJECT>
```

`firestore.rules` grants signed-in users read on `CKG-*` and gives each user
read/write on their own `users/{uid}` doc; everything else is denied.
`--only firestore` replaces the `(default)` database ruleset — safe here, since
Arboryx's `findings` is touched only by the backend ingest via the Admin SDK,
which bypasses security rules entirely. (Any future *browser* client on this
database would need adding to `firestore.rules`.)

Card PNGs are for social posts; they live in the private `robotics-cards`
bucket. The web frontend renders HTML cards and does not fetch them, so no
Storage / `storage.rules` step is needed for the site to work.

### Custom domain

```bash
make link-domain
```

Links the Hosting site to `CUSTOM_DOMAIN` (`robotics.arboryx.ai`): registers
the custom domain with Firebase, then creates the DNS records Firebase
requires in the Cloudflare zone via the Cloudflare API. Idempotent — re-run
to re-check; SSL provisioning can take up to ~24h.

One-time, set in `.env.prod`: `CUSTOM_DOMAIN`, `CF_ZONE_NAME`, and
`CF_API_TOKEN` — a Cloudflare token with **DNS:Edit** on the zone (Cloudflare
→ My Profile → API Tokens → Create → "Edit zone DNS"). Records are added
DNS-only (no Cloudflare proxy) so Firebase can verify ownership + issue the
cert. Firebase auto-adds the custom domain to Auth's authorized domains, so
sign-in keeps working on it.

### Verify

```bash
gcloud functions logs read robotics-ingest --region=us-central1 --limit=20
```

Check the Firestore `CKG-Robotics` collection has docs, and the frontend
loads cards.

### What runs automatically after deploy

- **Cloud Scheduler** fires `robotics-ingest` daily (`0 6 * * *` UTC).
  (`robotics-social` retired 2026-07-05 — social posting moved to soljet-postiz.)
- **Ingest** pulls the DuckDB file from GCS, extracts new findings, pushes
  the DuckDB file back, and writes catalysts/graph to `CKG-Robotics`.
- **Pub/Sub** fans `ingest → render` (ingest publishes to `robotics-ingest-done`
  after each run that writes new catalysts; push subscription hits
  `/render-batch`); render pulls the DuckDB file, renders PNGs, uploads them
  to Firebase Storage.
- The Firebase-hosted frontend reads `CKG-Robotics` from Firestore directly
  in the browser — no redeploy needed when new catalysts land.

---

## How local and prod differ — the whole story

Same container images, same code paths. The only difference is env vars:

| Env var | Local | Prod | Effect |
|---|---|---|---|
| `DUCKDB_GCS_BUCKET` | unset | `robotics-data` | set → ingest/render copy the DuckDB file to/from GCS; unset → use the bind-mounted file |
| `FIRESTORE_EXPORT_ENABLED` | `false` | `true` | publish catalysts/graph to `CKG-Robotics` |
| `STORAGE_UPLOAD_ENABLED` | `false` | `true` | upload card PNGs to Firebase Storage |

In prod the DuckDB file lives in the `robotics-data` GCS bucket. Each run
copies it to local disk (`pull`), works on it, and — for ingest only —
copies it back (`push`). Render is read-only, so it only pulls. See
`src/duckdb_sync.py`.
