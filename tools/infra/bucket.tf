# Robotics-module data bucket — holds the DuckDB file + JSON exports + PNG card images.
resource "google_storage_bucket" "robotics_data" {
  name                        = var.data_bucket_name
  location                    = "US"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age        = 30
      with_state = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  # Card images older than 90 days are stale (delete; re-render on demand if needed).
  lifecycle_rule {
    condition {
      age            = 90
      matches_prefix = ["exports/card_images/"]
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}

# Card-PNG bucket for the Firebase-hosted frontend.
# PRIVATE — no allUsers IAM. robotics-render writes via its service account
# (IAM, below). Browsers read through the Firebase Storage SDK, gated by the
# Security Rules in tools/frontend-deploy/storage.rules. To make those rules
# apply, this bucket must be imported into Firebase Storage once (Firebase
# Console → Storage → Add bucket) — see DEPLOYMENT.md.
resource "google_storage_bucket" "robotics_cards" {
  name                        = var.cards_bucket_name
  location                    = "US"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = false

  cors {
    # Allow the Firebase Hosting frontend to fetch images cross-origin.
    origin          = ["*"]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type", "Cache-Control"]
    max_age_seconds = 3600
  }

  # PNGs older than 90 days are stale; delete and let render regenerate on demand.
  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}
