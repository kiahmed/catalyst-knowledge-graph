# Least-privilege service accounts — one per tool. Role bindings on the
# specific resources they need; no project-level roles.

resource "google_service_account" "robotics_ingest" {
  account_id   = "robotics-ingest-sa"
  display_name = "robotics-ingest — Cloud Function SA"
}

resource "google_service_account" "robotics_render" {
  account_id   = "robotics-render-sa"
  display_name = "robotics-render — Cloud Run service SA"
}

resource "google_service_account" "robotics_scheduler" {
  account_id   = "robotics-scheduler-sa"
  display_name = "Cloud Scheduler invoker SA"
}

resource "google_service_account" "pubsub_invoker" {
  account_id   = "pubsub-invoker-sa"
  display_name = "Pub/Sub push SA — invokes Cloud Run fan-out targets"
}

# --- Ingest: read Arboryx findings + write CKG-Robotics + read/write data bucket ---
# Single-project deployment: ingest reads `findings/*` and writes `CKG-Robotics/*`
# in the same Firestore (default) database. We grant `roles/datastore.user`
# (read+write) — not just `viewer` — because the SA also writes the export
# collection. Trust boundary: this SA's code path never touches `findings/*`
# for writes (enforced in src/ingest.py + reviewed via Cloud Audit Logs).
# Firestore IAM has no collection-scoped roles, so this blast radius is
# unavoidable without splitting databases.
resource "google_project_iam_member" "ingest_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.robotics_ingest.email}"
}

resource "google_storage_bucket_iam_member" "ingest_data_rw" {
  bucket = google_storage_bucket.robotics_data.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.robotics_ingest.email}"
}

resource "google_secret_manager_secret_iam_member" "ingest_gemini" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.robotics_ingest.email}"
}

# Ingest publishes a message on robotics-ingest-done after a successful run,
# which fans out to robotics-render via push subscription.
resource "google_pubsub_topic_iam_member" "ingest_publish_done" {
  topic  = google_pubsub_topic.ingest_done.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.robotics_ingest.email}"
}

# --- Render: read/write Robotics data + write public card PNGs ---
resource "google_storage_bucket_iam_member" "render_data_rw" {
  bucket = google_storage_bucket.robotics_data.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.robotics_render.email}"
}

# Render uploads PNGs to the public cards bucket after each successful render.
# Read access is via uniform bucket-level public-read (see bucket.tf); the
# render SA only needs write.
resource "google_storage_bucket_iam_member" "render_cards_write" {
  bucket = google_storage_bucket.robotics_cards.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.robotics_render.email}"
}

# robotics-og (same SA) streams card PNGs back out via /card-img/<id>.png —
# objectCreator doesn't include get, so add read.
resource "google_storage_bucket_iam_member" "render_cards_read" {
  bucket = google_storage_bucket.robotics_cards.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.robotics_render.email}"
}

# robotics-og reads card docs from Firestore (CKG-<sector>/catalysts/items/*)
# for per-card OG titles/descriptions. Read-only — exports are written by the
# ingest SA. Firestore IAM has no collection-scoped roles (see ingest note).
resource "google_project_iam_member" "render_firestore_read" {
  project = var.project_id
  role    = "roles/datastore.viewer"
  member  = "serviceAccount:${google_service_account.robotics_render.email}"
}

# --- Scheduler: invoke ingest function ---
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoke_ingest" {
  location = google_cloudfunctions2_function.robotics_ingest.location
  name     = google_cloudfunctions2_function.robotics_ingest.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.robotics_scheduler.email}"
}

# --- Pub/Sub push: invoke robotics-render on ingest-done fan-out ---
resource "google_cloud_run_v2_service_iam_member" "pubsub_invoke_render" {
  location = google_cloud_run_v2_service.robotics_render.location
  name     = google_cloud_run_v2_service.robotics_render.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

# --- Handle resolver: read/write the shared handle-cache Firestore collection ---
resource "google_service_account" "handle_resolver" {
  account_id   = "handle-resolver-sa"
  display_name = "handle-resolver — Cloud Function SA"
}

# Same collection-scoping caveat as ingest: Firestore IAM has no
# collection-level roles, so datastore.user is project-wide. This SA's code
# path only touches the `handle-cache` collection (tools/handle-resolver).
resource "google_project_iam_member" "handles_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.handle_resolver.email}"
}

resource "google_secret_manager_secret_iam_member" "handles_cse_key" {
  secret_id = google_secret_manager_secret.cse_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.handle_resolver.email}"
}

resource "google_secret_manager_secret_iam_member" "handles_serper_key" {
  secret_id = google_secret_manager_secret.serper_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.handle_resolver.email}"
}

resource "google_secret_manager_secret_iam_member" "handles_brave_key" {
  secret_id = google_secret_manager_secret.brave_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.handle_resolver.email}"
}

# Ingest runs the in-process handles sweep post-ingest (HANDLE_SWEEP_ENABLED)
# — it needs the search-API key too.
resource "google_secret_manager_secret_iam_member" "ingest_brave" {
  secret_id = google_secret_manager_secret.brave_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.robotics_ingest.email}"
}
