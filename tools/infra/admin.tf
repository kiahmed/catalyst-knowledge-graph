# robotics-admin — data editor API used by robotics.arboryx.ai/admin.
# The page calls this service's run.app URL directly (CORS; no Hosting
# rewrite — one network path, so the client IP is the front-end-appended last
# X-Forwarded-For entry). Publicly invocable; access is enforced in-app: a
# verified Firebase sign-in whose email AND client IP are listed in the
# private Firestore doc ckg_admin/<sector> (see tools/robotics-admin/main.py).
# Scales to zero: no cost while idle. max 1 instance — edits are serialized.

resource "google_service_account" "robotics_admin" {
  account_id   = "robotics-admin-sa"
  display_name = "robotics-admin — data editor Cloud Run SA"
}

# Firestore: read the allowlist + write CKG-<sector> cards/graph/vectors.
resource "google_project_iam_member" "admin_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.robotics_admin.email}"
}

# Firebase Auth ID-token verification needs no role (public certs).
resource "google_storage_bucket_iam_member" "admin_data_rw" {
  bucket = google_storage_bucket.robotics_data.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.robotics_admin.email}"
}

resource "google_secret_manager_secret_iam_member" "admin_gemini" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.robotics_admin.email}"
}

# Read-only, for the "backend activity" banner: scheduled runs (next/last
# start) and ingest/render start-done log events.
resource "google_project_iam_member" "admin_scheduler_viewer" {
  project = var.project_id
  role    = "roles/cloudscheduler.viewer"
  member  = "serviceAccount:${google_service_account.robotics_admin.email}"
}

resource "google_project_iam_member" "admin_logs_viewer" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.robotics_admin.email}"
}

resource "google_cloud_run_v2_service" "robotics_admin" {
  name                = "robotics-admin"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.robotics_admin.email
    max_instance_request_concurrency = 4
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    # An edit = pull DB + apply + push + Firestore delta export (~1–2 min).
    timeout = "300s"

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.robotics.repository_id}/robotics-admin:${var.image_tag}"

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
        cpu_idle = true
      }

      env {
        name  = "SECTOR"
        value = var.sector
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "DUCKDB_PATH"
        value = "/tmp/robotics.duckdb"
      }
      env {
        name  = "DUCKDB_GCS_BUCKET"
        value = google_storage_bucket.robotics_data.name
      }
      env {
        name  = "FIRESTORE_EXPORT_ENABLED"
        value = "true"
      }
      env {
        name  = "FIRESTORE_EXPORT_COLLECTION"
        value = "CKG-${var.sector}"
      }
      env {
        name  = "STORAGE_UPLOAD_ENABLED"
        value = "false"
      }
      env {
        name  = "CARDS_JSON_PATH"
        value = "/tmp/cards.json"
      }
      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name  = "ADMIN_ALLOWED_ORIGINS"
        value = "https://robotics.arboryx.ai"
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }
    }
  }

  depends_on = [google_project_service.required, google_secret_manager_secret_iam_member.admin_gemini]
}

# Anonymous invocation — the browser calls the run.app URL with a Firebase ID
# token (not a Google OIDC token); the app rejects anyone who isn't an
# allowlisted admin from an allowed IP.
resource "google_cloud_run_v2_service_iam_member" "admin_public_invoker" {
  name     = google_cloud_run_v2_service.robotics_admin.name
  location = google_cloud_run_v2_service.robotics_admin.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
