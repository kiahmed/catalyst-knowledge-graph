#!/usr/bin/env bash
# Bootstrap the Firestore composite indexes the Robotics module relies on.
#
# Why this script exists (and why you probably won't need it):
# As of 2026-05-10, Arboryx already maintains the index our cursor-walk
# needs (category ASC, timestamp ASC, __name__ ASC — index id CICAgJiUpoMK).
# `src/ingest.py` orders by `__name__` instead of `entry_id` precisely so we
# piggyback on that index. This script exists for the rare cases where:
#   - A Firestore migration / re-creation deletes the existing indexes
#   - A new sector module is provisioned in a new project
#   - You want to verify the required index actually exists before a deploy
#
# Located in dev-utils/ (NOT data/) because data/ is gitignored and would
# disappear on `make nuke`. dev-utils/ is bind-mounted into containers so
# you can also run this from inside the ingest container if convenient:
#   docker compose exec robotics-ingest bash /app/dev-utils/firestore_indexes_bootstrap.sh
#
# Usage:
#   ./dev-utils/firestore_indexes_bootstrap.sh             # list-only, no changes
#   ./dev-utils/firestore_indexes_bootstrap.sh --apply     # create the index
#
# Auth: needs roles/datastore.indexAdmin (or owner) on $PROJECT.

set -euo pipefail

PROJECT="${PROJECT:?set PROJECT (or GCP_PROJECT from .env)}"
COLLECTION="${COLLECTION:-findings}"
APPLY=false

for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=true ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

echo "── Existing composite indexes on $PROJECT/$COLLECTION ──────────"
gcloud firestore indexes composite list \
    --project="$PROJECT" \
    --filter="collectionGroup:$COLLECTION" \
    --format="table(name.basename(),fields[].fieldPath,fields[].order)" || {
    echo "Failed to list indexes. Check gcloud auth + project name." >&2
    exit 1
}

cat <<EOF

── Required by src/ingest.py ────────────────────────────────────
  Collection : $COLLECTION
  Fields     : category ASC, timestamp ASC, __name__ ASC
  Why        : oldest-first cursor walk for incremental ingest
EOF

if [ "$APPLY" = "false" ]; then
    cat <<EOF

(list-only mode. Pass --apply to create the index. The create call is
idempotent at the API level — gcloud errors with "already exists" if the
shape matches an existing index, which is harmless.)

EOF
    exit 0
fi

echo
echo "── Creating composite index ─────────────────────────────────────"
if gcloud firestore indexes composite create \
    --project="$PROJECT" \
    --collection-group="$COLLECTION" \
    --query-scope=COLLECTION \
    --field-config=field-path=category,order=ascending \
    --field-config=field-path=timestamp,order=ascending \
    --field-config=field-path=__name__,order=ascending 2>err.log; then
    echo "Index creation kicked off. Status will go CREATING → READY in 1-5 min."
else
    if grep -qi "already exists" err.log; then
        echo "Index already exists — no-op."
    else
        cat err.log >&2
        rm -f err.log
        exit 1
    fi
fi
rm -f err.log
