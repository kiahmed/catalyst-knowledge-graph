"""Config loader — merges config/config.yaml, vocab, and env overrides.

Env vars that override YAML values:
    SECTOR                            → sector
    GEMINI_MODEL                      → extraction.model
    GEMINI_API_KEY                    → extraction.api_key (not in YAML; env-only)
    DUCKDB_PATH                       → duckdb.path
    GCP_PROJECT                       → firestore.project
    FIRESTORE_DATABASE                → firestore.database
    FIRESTORE_COLLECTION              → firestore.collection                (Arboryx source)
    FIRESTORE_EXPORT_ENABLED          → firestore_export.enabled            (off in local by default)
    FIRESTORE_EXPORT_COLLECTION       → firestore_export.collection         (e.g. "CKG-Robotics")
    FIRESTORE_EXPORT_CATALYSTS_SUBPATH→ firestore_export.catalysts_subpath
    FIRESTORE_EXPORT_GRAPH_SUBPATH    → firestore_export.graph_subpath
    STORAGE_UPLOAD_ENABLED            → storage.enabled                     (off in local by default)
    STORAGE_BUCKET                    → storage.bucket                      (Firebase Storage / GCS bucket)
    STORAGE_CARDS_PREFIX              → storage.cards_prefix
    CARDS_JSON_PATH                   → export.cards_json_path
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"
_VOCAB_PATH = _REPO_ROOT / "config" / "relationship_vocab.yaml"


@dataclass(frozen=True)
class GroundingConfig:
    """Gemini google_search grounding (Pass 1)."""
    enabled: bool
    when: str                # always | missing_source_url | never
    model: str
    max_results_hint: int


@dataclass(frozen=True)
class UrlFetchConfig:
    """Article fetcher used when entry.source_url is present."""
    enabled: bool
    timeout_s: int
    max_chars: int
    user_agent: str


@dataclass(frozen=True)
class SourceRefsConfig:
    """Per-edge citation traceability (§2.2)."""
    enabled: bool
    downgrade_inferred_without_refs: bool


@dataclass(frozen=True)
class ExtractionConfig:
    model: str
    temperature: float
    max_output_tokens: int
    thinking_budget: str             # low | medium | high (diagnostic; the resolved
                                     # token count is max_output_tokens above)
    prompt_version: str
    confidence_drop: float
    confidence_write: float
    significance_min_for_inference: float
    source_refs: SourceRefsConfig
    grounding: GroundingConfig
    url_fetch: UrlFetchConfig
    api_key: str


@dataclass(frozen=True)
class ResolutionConfig:
    fuzzy_threshold: int


@dataclass(frozen=True)
class ExportConfig:
    cards_json_path: str
    schema_version: int
    card_limit: int
    public_base_url: str


@dataclass(frozen=True)
class GraphConfig:
    """Frontend render filters (§2.5)."""
    time_window_days: int            # 0 = no cap
    min_edge_confidence: float
    show_invalidated: bool


@dataclass(frozen=True)
class LifecycleConfig:
    """Phase-2 edge-lifecycle detector knobs (§2.6)."""
    enabled: bool
    event_driven: bool
    confirm_weight_boost: float
    invalidate_weight_penalty: float
    validator_model: str


@dataclass(frozen=True)
class FirestoreConfig:
    """Arboryx (sibling) findings collection — single source of truth."""
    project: str
    database: str        # "(default)" for the default DB
    collection: str      # "findings"


@dataclass(frozen=True)
class FirestoreExportConfig:
    """Where the Robotics module writes its catalysts + graph projection.

    Lives alongside Arboryx's `findings/*` in the same `(default)` Firestore
    database — convention is collection prefix `CKG-<Sector>` so future
    sectors (Crypto, AI-Stack, ...) get sibling collections without DB-level
    overhead. The `enabled` flag is False by default; flip to True (typically
    in prod) once the frontend is reading from Firestore directly.
    """
    enabled: bool                # gate: don't write in local dev by default
    collection: str              # e.g. "CKG-Robotics"
    catalysts_subpath: str       # e.g. "catalysts" → CKG-Robotics/catalysts/{entry_id}
    graph_subpath: str           # e.g. "graph"     → CKG-Robotics/graph/{sector}


@dataclass(frozen=True)
class StorageConfig:
    """Firebase Storage (= GCS) bucket where card PNGs live.

    Path scheme: gs://{bucket}/{cards_prefix}/{entry_id}.png
    The bucket is private — robotics-render writes via its service account;
    the frontend reads via the Firebase Storage SDK (getDownloadURL), gated
    by tools/frontend-deploy/storage.rules. No per-card URL is stored.
    """
    enabled: bool                # gate: don't upload in local dev by default
    bucket: str                  # e.g. "robotics-cards" or "<project>.firebasestorage.app"
    cards_prefix: str            # e.g. "cards"


@dataclass(frozen=True)
class RoboticsConfig:
    sector: str
    duckdb_path: str
    firestore: FirestoreConfig
    firestore_export: FirestoreExportConfig
    storage: StorageConfig
    extraction: ExtractionConfig
    resolution: ResolutionConfig
    export: ExportConfig
    graph: GraphConfig
    lifecycle: LifecycleConfig
    backtest_dir: str
    vocab: dict[str, Any] = field(default_factory=dict)


# Maps the thinking_budget enum to concrete token caps. Explicit
# max_output_tokens in YAML takes precedence over this mapping.
_THINKING_BUDGET_TOKENS: dict[str, int] = {
    "low": 4096,
    "medium": 8192,
    "high": 16384,
}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var. Falsy strings ('false','0','no','') override
    YAML defaults to False; anything else (including '1','true','yes') is True.
    Unset → fall back to YAML default."""
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def load_config() -> RoboticsConfig:
    cfg = _load_yaml(_CONFIG_PATH)
    vocab = _load_yaml(_VOCAB_PATH)

    extraction = cfg.get("extraction", {})
    resolution = cfg.get("resolution", {})
    duckdb_cfg = cfg.get("duckdb", {})
    firestore_cfg = cfg.get("firestore", {})
    firestore_export_cfg = cfg.get("firestore_export", {})
    storage_cfg = cfg.get("storage", {})
    export_cfg = cfg.get("export", {})
    graph_cfg = cfg.get("graph", {})
    lifecycle_cfg = cfg.get("lifecycle", {})
    backtest_cfg = cfg.get("backtest", {})
    confidence = extraction.get("confidence", {})
    significance = extraction.get("significance", {})
    grounding = extraction.get("grounding", {})
    url_fetch = extraction.get("url_fetch", {})
    source_refs = extraction.get("source_refs", {})

    # thinking_budget → token count, unless max_output_tokens is set explicitly.
    thinking_budget = str(extraction.get("thinking_budget", "medium")).lower()
    if thinking_budget not in _THINKING_BUDGET_TOKENS:
        thinking_budget = "medium"
    explicit_tokens = extraction.get("max_output_tokens")
    max_output_tokens = (
        int(explicit_tokens) if explicit_tokens is not None
        else _THINKING_BUDGET_TOKENS[thinking_budget]
    )

    api_key = os.environ.get("GEMINI_API_KEY", "")

    return RoboticsConfig(
        sector=os.environ.get("SECTOR", cfg.get("sector", "Robotics")),
        duckdb_path=os.environ.get("DUCKDB_PATH", duckdb_cfg.get("path", "/data/robotics.duckdb")),
        firestore=FirestoreConfig(
            project=os.environ.get("GCP_PROJECT", firestore_cfg.get("project", "")),
            database=os.environ.get("FIRESTORE_DATABASE", firestore_cfg.get("database", "(default)")),
            collection=os.environ.get("FIRESTORE_COLLECTION", firestore_cfg.get("collection", "findings")),
        ),
        firestore_export=FirestoreExportConfig(
            enabled=_env_bool("FIRESTORE_EXPORT_ENABLED", firestore_export_cfg.get("enabled", False)),
            collection=os.environ.get(
                "FIRESTORE_EXPORT_COLLECTION",
                firestore_export_cfg.get("collection", "CKG-Robotics"),
            ),
            catalysts_subpath=os.environ.get(
                "FIRESTORE_EXPORT_CATALYSTS_SUBPATH",
                firestore_export_cfg.get("catalysts_subpath", "catalysts"),
            ),
            graph_subpath=os.environ.get(
                "FIRESTORE_EXPORT_GRAPH_SUBPATH",
                firestore_export_cfg.get("graph_subpath", "graph"),
            ),
        ),
        storage=StorageConfig(
            enabled=_env_bool("STORAGE_UPLOAD_ENABLED", storage_cfg.get("enabled", False)),
            bucket=os.environ.get("STORAGE_BUCKET", storage_cfg.get("bucket", "")),
            cards_prefix=os.environ.get("STORAGE_CARDS_PREFIX", storage_cfg.get("cards_prefix", "cards")),
        ),
        extraction=ExtractionConfig(
            model=os.environ.get("GEMINI_MODEL", extraction.get("model", "gemini-2.5-flash")),
            temperature=float(extraction.get("temperature", 0.2)),
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
            prompt_version=extraction.get("prompt_version", "v2"),
            confidence_drop=float(confidence.get("drop", 0.30)),
            confidence_write=float(confidence.get("write", 0.60)),
            significance_min_for_inference=float(significance.get("min_for_inference", 0.5)),
            source_refs=SourceRefsConfig(
                enabled=bool(source_refs.get("enabled", True)),
                downgrade_inferred_without_refs=bool(
                    source_refs.get("downgrade_inferred_without_refs", True)
                ),
            ),
            grounding=GroundingConfig(
                enabled=bool(grounding.get("enabled", True)),
                when=str(grounding.get("when", "missing_source_url")),
                model=str(grounding.get("model", extraction.get("model", "gemini-2.5-flash"))),
                max_results_hint=int(grounding.get("max_results_hint", 5)),
            ),
            url_fetch=UrlFetchConfig(
                enabled=bool(url_fetch.get("enabled", True)),
                timeout_s=int(url_fetch.get("timeout_s", 10)),
                max_chars=int(url_fetch.get("max_chars", 20000)),
                user_agent=str(url_fetch.get("user_agent", "CatalystGraphBot/1.0")),
            ),
            api_key=api_key,
        ),
        resolution=ResolutionConfig(
            fuzzy_threshold=int(resolution.get("fuzzy_threshold", 88)),
        ),
        export=ExportConfig(
            cards_json_path=os.environ.get(
                "CARDS_JSON_PATH", export_cfg.get("cards_json_path", "/data/exports/cards.json")
            ),
            schema_version=int(export_cfg.get("schema_version", 1)),
            card_limit=int(export_cfg.get("card_limit", 200)),
            public_base_url=export_cfg.get("public_base_url", "/"),
        ),
        graph=GraphConfig(
            time_window_days=int(graph_cfg.get("time_window_days", 90)),
            min_edge_confidence=float(graph_cfg.get("min_edge_confidence", 0.0)),
            show_invalidated=bool(graph_cfg.get("show_invalidated", False)),
        ),
        lifecycle=LifecycleConfig(
            enabled=bool(lifecycle_cfg.get("enabled", False)),
            event_driven=bool(lifecycle_cfg.get("event_driven", True)),
            confirm_weight_boost=float(lifecycle_cfg.get("confirm_weight_boost", 0.15)),
            invalidate_weight_penalty=float(lifecycle_cfg.get("invalidate_weight_penalty", 0.30)),
            validator_model=str(lifecycle_cfg.get("validator_model", "gemini-2.5-flash")),
        ),
        backtest_dir=backtest_cfg.get("output_dir", "/data/exports/backtests"),
        vocab=vocab,
    )
