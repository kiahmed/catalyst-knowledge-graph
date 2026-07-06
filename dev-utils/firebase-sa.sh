#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# make firebase-sa — one-time Firebase deploy credential.
#
# Creates a dedicated service account + key for headless `make
# deploy-frontend`. Replaces the deprecated `firebase login:ci` token
# flow: a SA key never expires and needs no browser re-auth.
#
#   1. create the SA            firebase-deployer@<project>.iam...
#   2. grant deploy roles       firebase.admin + serviceUsageConsumer
#   3. create a key JSON        .secrets/firebase-deployer.json  (gitignored)
#   4. record its path          FIREBASE_DEPLOY_KEY in .env.prod
#
# Run once, as a project Owner (gcloud CLI authenticated). Idempotent —
# re-running reuses the SA + key. To rotate the key: delete the JSON and
# re-run.
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root
ROOT="$(pwd)"
ENVF=".env.prod"

[ -f "$ENVF" ] || { echo "!! $ENVF missing — cp .env.prod.example $ENVF and fill in"; exit 1; }
set -a; # shellcheck disable=SC1090
source "$ENVF"; set +a
: "${GCP_PROJECT:?set GCP_PROJECT in .env.prod}"

command -v gcloud >/dev/null 2>&1 || { echo "!! gcloud CLI not found"; exit 1; }

SA_NAME="firebase-deployer"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
KEY_DIR="${ROOT}/.secrets"
KEY_FILE="${KEY_DIR}/firebase-deployer.json"

# This needs project-IAM-admin rights — authenticate as an Owner. Uses the
# gcloud CLI account (not ADC); falls back with a clear message if missing.
if ! gcloud auth print-access-token >/dev/null 2>&1; then
  echo "!! gcloud CLI is not authenticated. Sign in as a project Owner:"
  echo "     gcloud auth login"
  exit 1
fi
echo "Project: ${GCP_PROJECT}   SA: ${SA_EMAIL}"

# ── 1. Service account (idempotent) ─────────────────────────────────
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$GCP_PROJECT" >/dev/null 2>&1; then
  echo "1/4  service account exists — reusing"
else
  echo "1/4  creating service account ${SA_NAME}"
  gcloud iam service-accounts create "$SA_NAME" \
    --project="$GCP_PROJECT" \
    --display-name="Firebase frontend deployer"
fi

# ── 2. IAM roles (add-iam-policy-binding is idempotent) ─────────────
# firebase.admin covers Hosting, Firestore/Storage rules, and the apps:*
# / projects:addfirebase calls deploy.sh makes. serviceUsageConsumer lets
# the SA call enabled APIs against this project.
echo "2/4  granting roles"
for role in roles/firebase.admin roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$role" \
    --condition=None >/dev/null
  echo "     ${role}"
done

# ── 3. Key JSON ─────────────────────────────────────────────────────
mkdir -p "$KEY_DIR"
if [ -f "$KEY_FILE" ]; then
  echo "3/4  key exists at ${KEY_FILE} — reusing (delete it + re-run to rotate)"
else
  echo "3/4  creating key ${KEY_FILE}"
  # Org policy iam.disableServiceAccountKeyCreation can block this.
  if ! gcloud iam service-accounts keys create "$KEY_FILE" \
         --project="$GCP_PROJECT" --iam-account="$SA_EMAIL" 2>/tmp/fbsa_err; then
    cat /tmp/fbsa_err; rm -f /tmp/fbsa_err
    echo ""
    echo "!! Key creation failed. If this is an org-policy block"
    echo "   (iam.disableServiceAccountKeyCreation), either lift it for this"
    echo "   project or fall back to 'firebase login:ci' (deprecated)."
    exit 1
  fi
  rm -f /tmp/fbsa_err
  chmod 600 "$KEY_FILE"
fi

# ── 4. Record the key path in .env.prod ─────────────────────────────
echo "4/4  recording FIREBASE_DEPLOY_KEY in ${ENVF}"
if grep -q '^FIREBASE_DEPLOY_KEY=' "$ENVF"; then
  sed -i "s|^FIREBASE_DEPLOY_KEY=.*|FIREBASE_DEPLOY_KEY=${KEY_FILE}|" "$ENVF"
else
  printf '\n# ── Firebase deploy credential (make firebase-sa) ─────────────────\nFIREBASE_DEPLOY_KEY=%s\n' "$KEY_FILE" >> "$ENVF"
fi
# The deprecated token, if present, would override the SA key — drop it.
if grep -q '^FIREBASE_TOKEN=.' "$ENVF"; then
  sed -i 's|^FIREBASE_TOKEN=.*|FIREBASE_TOKEN=|' "$ENVF"
  echo "     cleared the old FIREBASE_TOKEN (SA key supersedes it)"
fi

echo ""
echo "Done. Headless deploy credential ready — now run: make deploy-frontend"
