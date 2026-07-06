# One repo for all Robotics-module images: robotics-ingest, robotics-render.
resource "google_artifact_registry_repository" "robotics" {
  location      = var.region
  repository_id = "robotics"
  description   = "Container images for Robotics-module tools"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent-20"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }

  cleanup_policies {
    id     = "delete-older-than-90d"
    action = "DELETE"
    condition {
      older_than = "7776000s" # 90 days
    }
  }

  depends_on = [google_project_service.required]
}
