# Daily cron → robotics-ingest Cloud Function.
resource "google_cloud_scheduler_job" "ingest_daily" {
  name        = "robotics-ingest-daily"
  description = "Daily ingestion of Arboryx Robotics findings"
  region      = var.region
  schedule    = var.ingest_schedule
  # Local zone, not UTC: Arboryx doesn't publish until ~07:30 ET, so a UTC
  # morning cron always ran BEFORE the day's findings existed and stayed a
  # day behind. Naming the zone also keeps the offset right across DST.
  time_zone = "America/New_York"
  # Ingest+sweep legitimately runs 3-8 min; the 180s default made the
  # scheduler mark DEADLINE_EXCEEDED daily and fire 3 retry runs (each
  # burning a full handles sweep) even though every request returned 200.
  attempt_deadline = "1800s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.robotics_ingest.service_config[0].uri
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode({
      sector  = var.sector
      dry_run = false
    }))

    oidc_token {
      service_account_email = google_service_account.robotics_scheduler.email
      audience              = google_cloudfunctions2_function.robotics_ingest.service_config[0].uri
    }
  }

  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

  depends_on = [google_project_service.required]
}

# On-demand layer healing (technical_spec.md §2.9c part 3). PAUSED: it never
# fires on a schedule — `make layers-heal APPLY=1` runs it explicitly (the
# Cloud Scheduler :run API ignores the pause), same path as `make ingest-prod`.
# Same function + invoker SA as the daily ingest; the DuckDB push is
# generation-guarded, so a collision with a running ingest fails loudly (409).
resource "google_cloud_scheduler_job" "layers_heal" {
  name             = "robotics-layers-heal"
  description      = "On-demand AI layer healing for unclassified/doubtful companies"
  region           = var.region
  schedule         = "0 3 1 1 *" # placeholder — job is paused
  time_zone        = "America/New_York"
  paused           = true
  attempt_deadline = "1800s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.robotics_ingest.service_config[0].uri
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode({
      sector      = var.sector
      layers_heal = { limit = 200 }
    }))

    oidc_token {
      service_account_email = google_service_account.robotics_scheduler.email
      audience              = google_cloudfunctions2_function.robotics_ingest.service_config[0].uri
    }
  }

  retry_config {
    retry_count = 0
  }

  depends_on = [google_project_service.required]
}
