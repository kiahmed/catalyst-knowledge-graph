output "data_bucket" {
  value = google_storage_bucket.robotics_data.name
}

output "artifact_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.robotics.repository_id}"
}

output "ingest_function_url" {
  value = google_cloudfunctions2_function.robotics_ingest.service_config[0].uri
}

output "render_service_url" {
  value = google_cloud_run_v2_service.robotics_render.uri
}

output "og_service_url" {
  value = google_cloud_run_v2_service.robotics_og.uri
}

output "service_accounts" {
  value = {
    ingest    = google_service_account.robotics_ingest.email
    render    = google_service_account.robotics_render.email
    scheduler = google_service_account.robotics_scheduler.email
  }
}
