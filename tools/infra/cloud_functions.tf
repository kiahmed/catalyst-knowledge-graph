# robotics-ingest as a Cloud Functions Gen 2 (= Cloud Run under the hood).
#
# Terraform fully owns this function — source AND config. `make deploy`
# (dev-utils/deploy.sh) zips the function source, uploads it to
# gs://<data bucket>/<var.ingest_source_object> (a SHA-tagged path), then
# runs `terraform apply`. Because the object path changes per deploy,
# Terraform redeploys the function on every code change — no out-of-band
# `gcloud functions deploy` step, no ignore_changes drift.

resource "google_cloudfunctions2_function" "robotics_ingest" {
  name        = "robotics-ingest"
  location    = var.region
  description = "Ingests Arboryx master log → DuckDB"

  build_config {
    runtime     = "python312"
    entry_point = "run_ingest"
    source {
      storage_source {
        bucket = google_storage_bucket.robotics_data.name
        object = var.ingest_source_object
      }
    }
  }

  service_config {
    available_memory                 = "2Gi"
    available_cpu                    = "1"
    timeout_seconds                  = 540
    max_instance_count               = 1
    max_instance_request_concurrency = 1
    service_account_email            = google_service_account.robotics_ingest.email
    ingress_settings                 = "ALLOW_INTERNAL_AND_GCLB"

    environment_variables = {
      SECTOR               = var.sector
      GCP_PROJECT          = var.project_id
      FIRESTORE_DATABASE   = var.arboryx_firestore_database
      FIRESTORE_COLLECTION = var.arboryx_firestore_collection
      # Robotics-module export to Firestore + Firebase Storage (prod = on)
      FIRESTORE_EXPORT_ENABLED    = "true"
      FIRESTORE_EXPORT_COLLECTION = "CKG-${var.sector}"
      STORAGE_UPLOAD_ENABLED      = "true"
      STORAGE_BUCKET              = google_storage_bucket.robotics_cards.name
      STORAGE_CARDS_PREFIX        = "cards"
      # Cloud Functions filesystem is read-only except /tmp — the container
      # default (/data/...) is compose-only. Without this the very first
      # duckdb_pull dies with EACCES.
      DUCKDB_PATH                 = "/tmp/robotics.duckdb"
      DUCKDB_GCS_BUCKET           = google_storage_bucket.robotics_data.name
      PUBSUB_DONE_TOPIC           = google_pubsub_topic.ingest_done.name
      # Handles sweep for new entities, run in-process after each ingest
      # and before export (prod mirror of the local make-ingest chain).
      HANDLE_SWEEP_ENABLED        = "true"
      HANDLE_SWEEP_LIMIT          = "25"
      LOG_LEVEL                   = "INFO"
    }

    secret_environment_variables {
      key        = "GEMINI_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.gemini_api_key.secret_id
      version    = "latest"
    }

    secret_environment_variables {
      key        = "BRAVE_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.brave_api_key.secret_id
      version    = "latest"
    }
  }

  # The function mounts GEMINI_API_KEY + BRAVE_API_KEY — its SA needs
  # secretAccessor before the function is created.
  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.ingest_gemini,
    google_secret_manager_secret_iam_member.ingest_brave,
  ]
}

# handle-resolver — on-demand per-entity social handle resolution for
# soljet-postiz (spec contract B, docs/handle-resolution-spec.md).
# Verify-or-abstain, no LLM; caches results in the `handle-cache` Firestore
# collection. IAM-locked: grant roles/run.invoker to the posting side's
# identity when activating (no allUsers binding here on purpose).
resource "google_cloudfunctions2_function" "handle_resolver" {
  name        = "handle-resolver"
  location    = var.region
  description = "Resolves per-entity social handles (LinkedIn verify-or-abstain; X cache-only)"

  build_config {
    runtime     = "python312"
    entry_point = "resolve_handles"
    source {
      storage_source {
        bucket = google_storage_bucket.robotics_data.name
        object = var.handles_source_object
      }
    }
  }

  service_config {
    available_memory                 = "512Mi"
    available_cpu                    = "1"
    timeout_seconds                  = 300
    max_instance_count               = 1
    max_instance_request_concurrency = 1
    service_account_email            = google_service_account.handle_resolver.email
    ingress_settings                 = "ALLOW_ALL"

    environment_variables = {
      GCP_PROJECT             = var.project_id
      HANDLE_CACHE_COLLECTION = var.handle_cache_collection
      GOOGLE_CSE_ID           = var.google_cse_id
      HANDLE_SEARCH_DELAY_S   = var.handle_search_delay_s
      HANDLE_FETCH_DELAY_S    = var.handle_fetch_delay_s
      HANDLE_DIRECT_DELAY_S   = var.handle_direct_delay_s
      LOG_LEVEL               = "INFO"
    }

    secret_environment_variables {
      key        = "BRAVE_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.brave_api_key.secret_id
      version    = "latest"
    }

    secret_environment_variables {
      key        = "SERPER_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.serper_api_key.secret_id
      version    = "latest"
    }

    secret_environment_variables {
      key        = "GOOGLE_CSE_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.cse_api_key.secret_id
      version    = "latest"
    }
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.handles_brave_key,
    google_secret_manager_secret_iam_member.handles_serper_key,
    google_secret_manager_secret_iam_member.handles_cse_key,
  ]
}
