# Secret Manager entries. Values set out-of-band:
#   echo -n "$GEMINI_API_KEY" | gcloud secrets versions add GEMINI_API_KEY --data-file=-

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "GEMINI_API_KEY"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "cse_api_key" {
  secret_id = "GOOGLE_CSE_API_KEY"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "serper_api_key" {
  secret_id = "SERPER_API_KEY"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}
