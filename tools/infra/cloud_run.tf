# robotics-render — Cloud Run service (HTTP, Chromium-heavy).
# Ingress is ALL but invoker is IAM-locked (no allUsers binding), so
# public URL + authenticated-only is the effective posture. Pub/Sub push
# reaches in via OIDC; scheduler + daily_run via OIDC. No anonymous path.
resource "google_cloud_run_v2_service" "robotics_render" {
  name     = "robotics-render"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Stateless service — let Terraform manage its lifecycle (replace on change)
  # without the provider-6.x deletion guard blocking applies.
  deletion_protection = false

  template {
    service_account                  = google_service_account.robotics_render.email
    max_instance_request_concurrency = 1
    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }
    # 300s: a backlog /render-batch can exceed 120s, and the Pub/Sub push
    # subscription (ack deadline 120s) would retry mid-render.
    timeout = "300s"

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.robotics.repository_id}/robotics-render:${var.image_tag}"

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        cpu_idle = true
      }

      env {
        name  = "DUCKDB_GCS_BUCKET"
        value = google_storage_bucket.robotics_data.name
      }
      env {
        name  = "CARD_IMAGES_GCS_PREFIX"
        value = "exports/card_images/"
      }
      env {
        name  = "STORAGE_UPLOAD_ENABLED"
        value = "true"
      }
      env {
        name  = "STORAGE_BUCKET"
        value = google_storage_bucket.robotics_cards.name
      }
      env {
        name  = "STORAGE_CARDS_PREFIX"
        value = "cards"
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }
    }
  }

  depends_on = [google_project_service.required]
}

# robotics-og — social-preview (Open Graph) deep links.
# Same image as robotics-render, but OG_ONLY=true flips it into a tiny
# read-only HTTP service: GET /card/<id> (per-card OG tags) and
# GET /card-img/<id>.png (streams the already-rendered PNG from the private
# cards bucket). POST /render + /render-batch return 404; no DuckDB envs,
# Chromium never launches.
#
# PUBLIC BY DESIGN: Firebase Hosting rewrites /card/** and /card-img/**
# here, and Hosting can't attach OIDC — so this service (and ONLY this one)
# gets an allUsers invoker binding. robotics-render stays IAM-locked; the
# exposure here is limited to OG HTML + PNG passthrough for valid card ids.
resource "google_cloud_run_v2_service" "robotics_og" {
  name     = "robotics-og"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Stateless service — let Terraform manage its lifecycle (replace on change)
  # without the provider-6.x deletion guard blocking applies.
  deletion_protection = false

  template {
    service_account = google_service_account.robotics_render.email
    # No concurrency=1 override — OG requests are lightweight (no Chromium),
    # so the Cloud Run default (80) is fine.
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    timeout = "30s"

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.robotics.repository_id}/robotics-render:${var.image_tag}"

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "OG_ONLY"
        value = "true"
      }
      env {
        name  = "SECTOR"
        value = var.sector
      }
      env {
        name  = "STORAGE_BUCKET"
        value = google_storage_bucket.robotics_cards.name
      }
      env {
        name  = "STORAGE_CARDS_PREFIX"
        value = "cards"
      }
      env {
        name  = "CANONICAL_ORIGIN"
        value = var.canonical_origin
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }
    }
  }

  depends_on = [google_project_service.required]
}

# Anonymous invocation — required for Firebase Hosting rewrites (crawlers
# hit /card/** unauthenticated). Scoped to robotics-og only.
resource "google_cloud_run_v2_service_iam_member" "og_public_invoker" {
  location = google_cloud_run_v2_service.robotics_og.location
  name     = google_cloud_run_v2_service.robotics_og.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
