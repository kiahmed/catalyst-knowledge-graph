# Daily cron → robotics-ingest Cloud Function.
resource "google_cloud_scheduler_job" "ingest_daily" {
  name        = "robotics-ingest-daily"
  description = "Daily ingestion of Arboryx Robotics findings"
  region      = var.region
  schedule    = var.ingest_schedule
  time_zone   = "UTC"

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
