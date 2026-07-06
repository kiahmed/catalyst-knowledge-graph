# Catalyst knowledge-graph — developer commands (Robotics module).
# Most commands wrap `docker compose` + `curl` for the tool HTTP endpoints.

.PHONY: help setup doctor up down logs ps ps-short \
        ingest ingest-dry export watermark check-log firestore-ping \
        reextract backtest \
        render render-batch render-status \
        db db-query \
        frontend open \
        deploy deploy-preflight deploy-frontend firebase-sa link-domain firestore-sync \
        nuke prune test fmt

SHELL := /bin/bash

# Load .env if it exists. `-include` = don't error if missing (so `help`
# and `doctor` still work on a fresh clone). `export` makes vars visible
# to recipe commands.
-include .env
export

# Port fallbacks — mirror the ${VAR:-default} defaults in docker-compose.yml
# so `make ingest` works even before .env is created.
INGEST_PORT   ?= 8080
RENDER_PORT   ?= 8081
FRONTEND_PORT ?= 8000
MIN_DOCKER_MEM_MB ?= 4096

help:
	@echo "Catalyst knowledge-graph — Makefile"
	@echo ""
	@echo "Setup & runtime"
	@echo "  setup              Build images + initialize DuckDB"
	@echo "  doctor             Preflight: docker memory, ports, .env, creds"
	@echo "  up                 Start all services"
	@echo "  down               Stop services (keep volume)"
	@echo "  logs SERVICE=name  Tail logs for a service (default: all)"
	@echo "  ps                 List running services (full table)"
	@echo "  ps-short           List services — name, status, host port only"
	@echo "  nuke               Stop + wipe volumes (full reset)"
	@echo "  prune              Reclaim disk: remove dangling images + unused build cache"
	@echo ""
	@echo "Pipeline triggers"
	@echo "  firestore-ping     Read 1 finding from Arboryx Firestore — verifies auth + connectivity"
	@echo "  check-log          Sanity-check upstream Arboryx data (dup IDs, order, shape)"
	@echo "  watermark          Show last-processed date + entry_id"
	@echo "  ingest [LIMIT=N]   Real run (bounded if LIMIT set). Always re-exports cards.json."
	@echo "  ingest-dry         Preview ingest (reads, no writes, no export)"
	@echo "  export             Re-export cards.json from current DuckDB (no ingest)"
	@echo "  reextract ID=... [IDS=a,b,c] [WRITE=1] [PV=v2]  Re-extract one or many entries"
	@echo "  backtest [LIMIT=N] [IDS=a,b,c]  Dump extractor output to JSONL for eyeballing"
	@echo "  render             Render one card (CARD_ID=ROB-...)"
	@echo "  render-batch       Render PNGs. Three modes:"
	@echo "                       (no args)               → all unrendered"
	@echo "                       LIMIT=N                 → N latest"
	@echo "                       FROM=ROB-... LIMIT=N    → N starting at this entry_id"
	@echo "  render-status      Count catalysts vs rendered PNGs; show backlog"
	@echo ""
	@echo "DB + frontend"
	@echo "  db                 Open DuckDB CLI"
	@echo "  db-query Q='...'   Run a one-off query"
	@echo "  frontend           Open http://localhost:8000"
	@echo ""
	@echo "Prod deploy  (see DEPLOYMENT.md — reads .env.prod)"
	@echo "  deploy             Build + push images/source + terraform apply (runs preflight)"
	@echo "  deploy-preflight   Verify gcloud auth + IAM admin + .env.prod"
	@echo "  firestore-sync     One-time: local DuckDB → CKG-<sector> + PNGs → Firebase Storage"
	@echo "  deploy-frontend    Deploy frontend/ → Firebase Hosting"
	@echo "  firebase-sa        One-time: create Firebase deploy service account + key"
	@echo "  link-domain        One-time: link Hosting to CUSTOM_DOMAIN via Cloudflare DNS"

# ── Setup & runtime ────────────────────────────────────────────────

setup:
	@test -f .env || (echo "!! .env missing — cp .env.example .env and fill in" && exit 1)
	docker compose build
	docker compose up -d duckdb
	@echo "DuckDB initialized at robotics-data volume."

# Preflight: verify the local host can actually run the stack.
# Run BEFORE `make up` or `make ingest-dry`. Exits non-zero if any
# check fails, so CI can gate on it.
doctor:
	@bash -euo pipefail -c '\
	fail=0; \
	row() { printf "  %-32s %s\n" "$$1" "$$2"; }; \
	echo "Catalyst knowledge-graph preflight"; echo ""; \
	echo "── Docker ─────────────────────────────────────────────"; \
	if docker info >/dev/null 2>&1; then \
	  row "daemon reachable" "OK"; \
	  mem_bytes=$$(docker info --format "{{.MemTotal}}" 2>/dev/null || echo 0); \
	  mem_mb=$$(( mem_bytes / 1024 / 1024 )); \
	  min=$(MIN_DOCKER_MEM_MB); \
	  if [ "$$mem_mb" -ge "$$min" ]; then \
	    row "memory ($${mem_mb} MiB / $${min} min)" "OK"; \
	  else \
	    row "memory ($${mem_mb} MiB / $${min} min)" "FAIL — raise Docker Desktop memory"; fail=1; \
	  fi; \
	else \
	  row "daemon reachable" "FAIL — is Docker running?"; fail=1; \
	fi; \
	echo ""; echo "── Host ports ─────────────────────────────────────────"; \
	for entry in "INGEST:$(INGEST_PORT)" "RENDER:$(RENDER_PORT)" "FRONTEND:$(FRONTEND_PORT)"; do \
	  name=$${entry%%:*}; port=$${entry##*:}; \
	  if (echo > /dev/tcp/127.0.0.1/$$port) >/dev/null 2>&1; then \
	    row "$$name ($$port)" "FAIL — in use, override $${name}_PORT in .env"; fail=1; \
	  else \
	    row "$$name ($$port)" "free"; \
	  fi; \
	done; \
	echo ""; echo "── Config ─────────────────────────────────────────────"; \
	if [ -f .env ]; then row ".env present" "OK"; else row ".env present" "FAIL — cp .env.example .env"; fail=1; fi; \
	if [ -n "$${GEMINI_API_KEY:-}" ]; then row "GEMINI_API_KEY set" "OK"; else row "GEMINI_API_KEY set" "FAIL"; fail=1; fi; \
	if [ -n "$${GOOGLE_APPLICATION_CREDENTIALS:-}" ] && [ -r "$${GOOGLE_APPLICATION_CREDENTIALS}" ]; then \
	  row "GOOGLE_APPLICATION_CREDENTIALS" "OK"; \
	else \
	  row "GOOGLE_APPLICATION_CREDENTIALS" "FAIL — run: gcloud auth application-default login"; fail=1; \
	fi; \
	echo ""; \
	if [ "$$fail" -eq 0 ]; then echo "All checks passed. Safe to run: make up"; else echo "$$fail check(s) failed. Fix above then re-run."; exit 1; fi'

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f $(SERVICE)

ps:
	docker compose ps

ps-short:
	@docker compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Publishers}}"

nuke:
	docker compose down -v
	@echo "All volumes wiped."

# Reclaim disk after repeated `make setup` rebuilds. Removes:
#   - dangling images (old `robotics-ingest:latest` layers replaced by a rebuild)
#   - unused build cache layers
# Safe: leaves running containers, tagged-and-in-use images, and other
# projects' images untouched.
prune:
	@echo "Pruning dangling images + unused build cache..."
	docker image prune -f
	docker builder prune -f
	@echo "Done."

# ── Pipeline triggers ──────────────────────────────────────────────

ingest:
	@if [ -n "$(LIMIT)" ]; then \
		body='{"sector":"$(SECTOR)","limit":$(LIMIT)}'; \
	else \
		body='{"sector":"$(SECTOR)"}'; \
	fi; \
	echo "── live progress (extract_start / extract_ok per entry) ─────"; \
	docker compose logs -f --since 0s robotics-ingest 2>&1 \
		| grep --line-buffered -E "extract_start entry_id|extract_ok entry_id|extract_fail entry_id|run_summary" \
		| sed -E 's/.*\| //' & \
	tail_pid=$$!; \
	trap "kill $$tail_pid 2>/dev/null || true" EXIT INT TERM; \
	curl -fsS -X POST http://localhost:$(INGEST_PORT) \
		-H "Content-Type: application/json" \
		-d "$$body" | jq .

ingest-dry:
	@curl -fsS -X POST http://localhost:$(INGEST_PORT) \
		-H "Content-Type: application/json" \
		-d '{"sector":"$(SECTOR)","dry_run":true}' | jq .

# Re-run export_cards() against the current DuckDB state. No ingest, no LLM,
# no Firestore write. Useful when:
#   - ingest crashed after writing some catalysts but before the export step
#   - you changed src/export.py and want cards.json regenerated without
#     burning new LLM calls
#   - the cards.json file got deleted (e.g. after `make nuke`) but DuckDB
#     state is still intact from a backup
export:
	@docker compose exec robotics-ingest python -c "import json; from src.config import load_config; from src.export import export_cards; print(json.dumps(export_cards(load_config()), indent=2, default=str))"

# Show the current ingest cursor (what was last processed).
watermark:
	@docker compose exec duckdb duckdb /data/robotics.duckdb \
		"SELECT sector, last_processed_date, last_processed_entry_id, last_processed_at FROM ingestion_meta;"

# Connectivity probe: read 1 doc from Arboryx Firestore and print its
# entry_id. Exit non-zero on auth/permission/database-name failures so
# you catch them before burning an LLM call on `make ingest`.
firestore-ping:
	@docker compose exec robotics-ingest python -c "\
import os; \
from google.cloud import firestore; \
from google.cloud.firestore_v1.base_query import FieldFilter; \
db = os.environ.get('FIRESTORE_DATABASE', '(default)'); \
kw = {'project': os.environ['GCP_PROJECT']}; \
kw['database'] = db if db != '(default)' else kw.get('database', None); \
kw = {k: v for k, v in kw.items() if v is not None}; \
client = firestore.Client(**kw); \
col = os.environ.get('FIRESTORE_COLLECTION', 'findings'); \
sector = os.environ.get('SECTOR', 'Robotics'); \
docs = list(client.collection(col).where(filter=FieldFilter('category','==',sector)).order_by('timestamp').limit(1).stream()); \
print(f'OK: project={kw[\"project\"]} db={db} col={col} sector={sector}'); \
print(f'first_entry_id={docs[0].to_dict().get(\"entry_id\") if docs else \"<empty>\"}'); \
print(f'first_timestamp={docs[0].to_dict().get(\"timestamp\") if docs else \"<empty>\"}'); \
"

# Sanity-check the upstream Arboryx master log — duplicate IDs, out-of-order
# timestamps, malformed IDs, missing fields. Read-only; writes nothing back.
check-log:
	@docker compose exec robotics-ingest python /app/dev-utils/master_log_corrector.py --dry-run

# Re-extract one or many entries. Dry by default; WRITE=1 persists.
# Usage: make reextract ID=ROB-041726-001
#        make reextract ID=ROB-041726-001 WRITE=1 PV=v2
#        make reextract IDS=ROB-041726-001,ROB-041726-002 WRITE=1
reextract:
	@ids="$(if $(IDS),$(IDS),$(ID))"; \
	if [ -z "$$ids" ]; then echo "!! Set ID=ROB-... or IDS=a,b,c"; exit 1; fi; \
	for id in $$(echo "$$ids" | tr ',' ' '); do \
		docker compose exec robotics-ingest python /app/dev-utils/reextract.py "$$id" \
			$(if $(WRITE),--write) $(if $(PV),--prompt-version $(PV)); \
	done

# Run the whole (or a subset of the) extractor over Robotics entries and
# dump JSONL to /data/exports/backtests/ for eyeballing.
# Usage: make backtest LIMIT=10
#        make backtest IDS=ROB-041726-001,ROB-041726-002
backtest:
	@docker compose exec robotics-ingest python /app/dev-utils/backtest.py \
		$(if $(LIMIT),--limit $(LIMIT)) $(if $(IDS),--ids $(IDS))

render:
	@test -n "$(CARD_ID)" || (echo "!! Set CARD_ID=ROB-MMDDYY-NNN" && exit 1)
	@curl -fsS -X POST http://localhost:$(RENDER_PORT)/render \
		-H "Content-Type: application/json" \
		-d '{"card_id":"$(CARD_ID)"}' | jq .

# Render PNGs. Three modes — all idempotent (skip if PNG exists, FORCE=1 to override):
#   make render-batch                           # all unrendered
#   make render-batch LIMIT=N                   # N latest catalysts
#   make render-batch FROM=ROB-... LIMIT=N      # N starting at this entry_id (oldest-forward)
render-batch:
	@force="$(if $(FORCE),true,false)"; \
	body='{"force":'"$$force"; \
	[ -n "$(FROM)" ] && body=$$body',"from_id":"$(FROM)"'; \
	[ -n "$(LIMIT)" ] && body=$$body',"limit":$(LIMIT)'; \
	body=$$body'}'; \
	curl -fsS -X POST http://localhost:$(RENDER_PORT)/render-batch \
		-H "Content-Type: application/json" \
		-d "$$body" | jq .

# Render progress: how many catalysts in DuckDB vs how many PNGs on disk.
# No persistent cursor exists — the gap between these counts IS the backlog.
render-status:
	@total=$$(docker compose exec -T duckdb duckdb /data/robotics.duckdb -noheader -list \
		"SELECT COUNT(*) FROM catalysts;" 2>/dev/null | tr -d '[:space:]'); \
	rendered=$$(ls data/exports/card_images/*.png 2>/dev/null | wc -l); \
	missing=$$(( total - rendered )); \
	printf "  %-24s %s\n" "catalysts in DuckDB" "$$total"; \
	printf "  %-24s %s\n" "PNGs rendered locally" "$$rendered"; \
	printf "  %-24s %s\n" "missing PNGs" "$$missing"; \
	if [ "$$missing" -gt 0 ]; then \
		echo ""; echo "Catch up:  make render-batch  (or LIMIT=N for a bounded run)"; \
	fi

# ── DB + frontend ──────────────────────────────────────────────────

db:
	docker compose exec duckdb duckdb /data/robotics.duckdb

db-query:
	@test -n "$(Q)" || (echo "!! Set Q='SELECT ...'" && exit 1)
	@docker compose exec duckdb duckdb /data/robotics.duckdb -c "$(Q)"

frontend:
	@echo "Opening http://localhost:$(FRONTEND_PORT) ..."
	@(command -v xdg-open && xdg-open http://localhost:$(FRONTEND_PORT)) || \
	 (command -v open && open http://localhost:$(FRONTEND_PORT)) || \
	 echo "Open http://localhost:$(FRONTEND_PORT) manually."

open: frontend

# ── GCP deploy ─────────────────────────────────────────────────────
# Prod backend = one command: `make deploy`. It reads .env.prod, generates
# Terraform vars, pushes secrets, builds/pushes artifacts, and applies.
# See DEPLOYMENT.md for the full local + prod runbook.

# Single-project deployment: GCP_PROJECT is the only project that matters.
# Verifies tooling + active gcloud account + project-level IAM admin
# before `terraform apply` tries to write IAM bindings.
deploy-preflight:
	@bash -euo pipefail -c '\
	fail=0; \
	row() { printf "  %-40s %s\n" "$$1" "$$2"; }; \
	echo "Deploy preflight"; echo ""; \
	echo "── Tooling ───────────────────────────────────────────────"; \
	if command -v gcloud >/dev/null 2>&1; then row "gcloud installed" "OK"; else row "gcloud installed" "FAIL"; fail=1; fi; \
	if command -v terraform >/dev/null 2>&1; then row "terraform installed" "OK"; else row "terraform installed" "FAIL"; fail=1; fi; \
	active=$$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null || true); \
	if [ -n "$$active" ]; then row "gcloud active account" "$$active"; else row "gcloud active account" "FAIL — gcloud auth login"; fail=1; fi; \
	echo ""; echo "── .env.prod ─────────────────────────────────────────────"; \
	if [ -f .env.prod ]; then row ".env.prod present" "OK"; else row ".env.prod present" "FAIL — cp .env.prod.example .env.prod"; fail=1; exit $$fail; fi; \
	proj=$$(grep -E "^GCP_PROJECT=" .env.prod | head -1 | cut -d= -f2- | tr -d "[:space:]" || true); \
	if [ -n "$$proj" ]; then row "GCP_PROJECT set" "$$proj"; else row "GCP_PROJECT set" "FAIL — set GCP_PROJECT in .env.prod"; fail=1; fi; \
	echo ""; echo "── IAM admin on $$proj ───────────────────────────────────"; \
	if [ -n "$$proj" ] && [ -n "$$active" ]; then \
	  roles=$$(gcloud projects get-iam-policy "$$proj" --flatten="bindings[].members" --filter="bindings.members:$$active" --format="value(bindings.role)" 2>/dev/null || true); \
	  if echo "$$roles" | grep -qE "roles/(owner|resourcemanager.projectIamAdmin)"; then \
	    row "$$active can set IAM on $$proj" "OK"; \
	  else \
	    row "$$active can set IAM on $$proj" "WARN — not Owner/projectIamAdmin (non-blocking)"; \
	    echo "      Terraform creates service-account role bindings, which need"; \
	    echo "      roles/owner or roles/resourcemanager.projectIamAdmin on the project."; \
	  fi; \
	else \
	  row "IAM admin" "SKIP (project_id or gcloud login missing)"; \
	fi; \
	echo ""; \
	if [ "$$fail" -eq 0 ]; then echo "Preflight passed. Safe to run: make deploy"; else echo "$$fail check(s) failed."; exit 1; fi'

# One command, prod backend. Reads .env.prod → generates Terraform vars,
# pushes secrets, zips/builds artifacts, terraform apply. See dev-utils/deploy.sh.
deploy: deploy-preflight
	@bash dev-utils/deploy.sh

# One-time prod bootstrap: push the current LOCAL DuckDB's catalysts +
# graph to Firestore CKG-<sector>, and local PNGs to Firebase Storage —
# without an ingest run. After this, prod ingest keeps Firestore current.
# Needs `make up` running and credentials with Firestore + Storage write.
firestore-sync:
	@test -f .env.prod || (echo "!! .env.prod missing — cp .env.prod.example .env.prod" && exit 1)
	@echo "One-time backfill: local DuckDB → Firestore CKG-<sector> + PNGs → Firebase Storage"
	@set -a; . ./.env.prod; set +a; \
	docker compose exec -T \
		-e FIRESTORE_EXPORT_ENABLED=true \
		-e STORAGE_UPLOAD_ENABLED=true \
		-e FIRESTORE_EXPORT_COLLECTION="CKG-$$SECTOR" \
		-e STORAGE_BUCKET="$$CARDS_BUCKET" \
		robotics-ingest python -c "import json; from src.config import load_config; from src.firestore_export import export_to_firestore; print(json.dumps(export_to_firestore(load_config()), indent=2, default=str))"

# Frontend → Firebase Hosting. Separate from `make deploy` (which does the
# backend pipeline) because Hosting needs firebase-tools + a .firebaserc.
deploy-frontend:
	./tools/frontend-deploy/deploy.sh

# One-time: create a Firebase deploy service account + key for headless
# `make deploy-frontend`. Replaces the deprecated `firebase login:ci` token —
# a SA key never expires and needs no browser re-auth. Writes
# FIREBASE_DEPLOY_KEY into .env.prod. Run once, as a project Owner.
firebase-sa:
	./dev-utils/firebase-sa.sh

# One-time: link the Firebase Hosting site to CUSTOM_DOMAIN (e.g.
# robotics.arboryx.ai) — registers the custom domain with Firebase and
# creates the required DNS records in Cloudflare. Needs CF_API_TOKEN in
# .env.prod. Idempotent — re-run to re-check provisioning status.
link-domain:
	python3 dev-utils/link_domain.py

# ── Dev ────────────────────────────────────────────────────────────

test:
	@echo "Phase 1 ticket 1.9 — wire pytest across tools/."
	@false

fmt:
	@command -v ruff >/dev/null && ruff format tools/ || echo "(ruff not installed)"
	@command -v terraform >/dev/null && (cd tools/infra && terraform fmt) || true
