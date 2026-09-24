#!/usr/bin/env bash
# Runs test.mjs against ../firestore.rules in the Firestore emulator.
# Executed inside node:22-bookworm by `make rules-test` (no host Java needed).
set -euo pipefail
apt-get update -qq >/dev/null && apt-get install -y -qq openjdk-17-jre-headless >/dev/null
work=$(mktemp -d) && cp /t/test.mjs /t/firebase.json "$work"/ && cp /rules/firestore.rules "$work"/
cd "$work" && npm init -y >/dev/null && npm i -s firebase-tools@13 @firebase/rules-unit-testing firebase >/dev/null 2>&1
npx firebase emulators:exec --only firestore --project demo-rules "node test.mjs" 2>&1 \
  | grep -E "^\s+(ok|FAIL)|passed|^(robotics|fix|catalog)|Error"
