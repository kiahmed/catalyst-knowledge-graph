variable "project_id" {
  description = "GCP project ID for the Robotics module"
  type        = string
}

variable "region" {
  description = "Default GCP region"
  type        = string
  default     = "us-central1"
}

variable "data_bucket_name" {
  description = "Private bucket: DuckDB file + JSON exports + raw card image cache. Read by ingest/render SAs only."
  type        = string
  default     = "robotics-data"
}

variable "cards_bucket_name" {
  description = "Public bucket: card PNGs the Firebase-hosted frontend fetches. Uniform bucket-level public-read; render SA writes."
  type        = string
  default     = "robotics-cards"
}

variable "arboryx_firestore_database" {
  description = "Firestore database holding Arboryx's `findings/*` (and this module's `CKG-*` collections). Same project as var.project_id — single-project deployment."
  type        = string
  default     = "(default)"
}

variable "arboryx_firestore_collection" {
  description = "Source collection that holds one doc per finding."
  type        = string
  default     = "findings"
}

variable "sector" {
  description = "Sector this Robotics-module deployment tracks. Phase 1 = Robotics."
  type        = string
  default     = "Robotics"
}

variable "ingest_schedule" {
  description = "Cron for robotics-ingest trigger (UTC)"
  type        = string
  default     = "0 6 * * *"
}

variable "canonical_origin" {
  description = "Public origin of the frontend — robotics-og builds og:image / og:url links against it."
  type        = string
  default     = "https://robotics.arboryx.ai"
}

variable "image_tag" {
  description = "Container image tag (usually git SHA). `make deploy` sets this per-run."
  type        = string
  default     = "latest"
}

variable "ingest_source_object" {
  description = "GCS object (in the data bucket) holding the robotics-ingest Cloud Function source zip. `make deploy` sets this per-run to a SHA-tagged path so Terraform redeploys the function on every code change."
  type        = string
  default     = "function-sources/robotics-ingest.zip"
}

variable "handles_source_object" {
  description = "GCS object (in the data bucket) holding the handle-resolver Cloud Function source zip. `make deploy` sets this per-run (SHA-tagged) so Terraform redeploys on code change."
  type        = string
  default     = "function-sources/handle-resolver.zip"
}

variable "google_cse_id" {
  description = "Programmable Search Engine ID (cx) for handle resolution. Empty = CSE disabled (direct-fetch fallback only)."
  type        = string
  default     = ""
}

variable "handle_search_delay_s" {
  description = "Seconds between search-API queries in handle-resolver. Empty = code default (2)."
  type        = string
  default     = ""
}

variable "handle_fetch_delay_s" {
  description = "Seconds between direct LinkedIn fetches in handle-resolver. Empty = code default (8)."
  type        = string
  default     = ""
}

variable "handle_direct_delay_s" {
  description = "Seconds before a direct-fetch fallback in handle-resolver. Empty = code default (30)."
  type        = string
  default     = ""
}

variable "handle_cache_collection" {
  description = "Firestore collection used by handle-resolver as its shared handle cache."
  type        = string
  default     = "handle-cache"
}
