# Pub/Sub topic — robotics-ingest publishes on success, fanning out to
# robotics-render. This decouples the orchestration: scheduler only knows
# about ingest; downstream tools subscribe to the topic.

resource "google_pubsub_topic" "ingest_done" {
  name = "robotics-ingest-done"

  message_retention_duration = "86400s" # 1 day

  depends_on = [google_project_service.required]
}

# Subscription for robotics-render — Pub/Sub push to /render-batch.
# Uses OIDC auth; `pubsub_invoker` SA holds run.invoker on the service
# (see service_accounts.tf → pubsub_invoke_render).
resource "google_pubsub_subscription" "render_on_ingest" {
  name  = "robotics-render-on-ingest"
  topic = google_pubsub_topic.ingest_done.name

  ack_deadline_seconds       = 120
  message_retention_duration = "86400s"

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.robotics_render.uri}/render-batch"

    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      audience              = google_cloud_run_v2_service.robotics_render.uri
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  depends_on = [google_cloud_run_v2_service_iam_member.pubsub_invoke_render]
}
