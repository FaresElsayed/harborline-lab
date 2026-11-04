"""Runtime configuration, read from environment variables (Docker passes them from .env).

Every path defaults to the package layout (``R/config``, ``R/landing`` ...) so the simulator also runs
from a source checkout. Inside the container the compose file points them at the ``/data`` mounts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]   # R/ (holds config/, answer_key/, simulator/ ...)
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
TRACKER_SQL = Path(__file__).resolve().parent / "tracker" / "tracker.sql"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _env_path(name: str, default: Path) -> Path:
    return Path(_env(name, str(default))).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    # Postgres (inside Docker the host is the compose service name "postgres")
    pg_host: str
    pg_port: int
    pg_superuser: str
    pg_superpassword: str
    app_user: str
    app_password: str
    etl_user: str
    etl_password: str
    reviewer_user: str            # Iris Novak's read-only reporting login (dashboard UAT); never the data engineer's
    reviewer_password: str
    # Host-side ports shown to the user on the Connections panel
    pg_public_host: str
    pg_public_port: int
    app_public_port: int
    # Folders
    root_dir: Path
    config_dir: Path
    answer_key_dir: Path
    tracker_dir: Path
    landing_dir: Path
    handbook_dir: Path
    brand_dir: Path
    start_here: Path
    web_dist: Path
    # Mock SaaS credentials
    sf_client_id: str
    sf_client_secret: str
    carrier_api_key: str
    # Simulation defaults
    default_preset: str
    default_seed: int
    offline: bool                 # never call public APIs (FX, weather); use the bundled fallback series
    stream_checkpoint_s: int      # max seconds between world checkpoints while streaming
    landing_host_hint: str
    history_end: str | None       # YYYY-MM-DD to pin the build's history end (default: build date)

    @property
    def rules_path(self) -> Path:
        return self.config_dir / "business_rules.yaml"

    @property
    def planted_path(self) -> Path:
        return self.answer_key_dir / "planted_stories.yaml"

    @property
    def anomalies_csv(self) -> Path:
        return self.answer_key_dir / "anomalies.csv"


def load_settings() -> Settings:
    root = _env_path("HL_ROOT", PACKAGE_ROOT)
    return Settings(
        pg_host=_env("HL_PG_HOST", "localhost"),
        pg_port=_env_int("HL_PG_PORT", 5432),
        pg_superuser=_env("POSTGRES_USER", "postgres"),
        pg_superpassword=_env("POSTGRES_PASSWORD", "harborline-admin"),
        app_user=_env("HL_APP_USER", "harborline_app"),
        app_password=_env("HL_APP_PASSWORD", "harborline-app"),
        etl_user=_env("HL_ETL_USER", "etl_reader"),
        etl_password=_env("HL_ETL_PASSWORD", "etl-reader-2026"),
        reviewer_user=_env("HL_REVIEWER_USER", "uat_reviewer"),
        reviewer_password=_env("HL_REVIEWER_PASSWORD", "uat-review-2026"),
        pg_public_host=_env("HL_PG_PUBLIC_HOST", "localhost"),
        pg_public_port=_env_int("HL_PG_PUBLIC_PORT", _env_int("HL_PG_PORT", 5432)),
        app_public_port=_env_int("HL_APP_PUBLIC_PORT", _env_int("HL_APP_PORT", 8080)),
        root_dir=root,
        config_dir=_env_path("HL_CONFIG_DIR", root / "config"),
        answer_key_dir=_env_path("HL_ANSWER_KEY_DIR", root / "answer_key"),
        tracker_dir=_env_path("HL_TRACKER_DIR", root / "tracker"),
        landing_dir=_env_path("HL_LANDING_DIR", root / "landing"),
        handbook_dir=_env_path("HL_HANDBOOK_DIR", root / "handbook"),
        brand_dir=_env_path("HL_BRAND_DIR", root / "brand"),
        start_here=_env_path("HL_START_HERE", root / "START_HERE.html"),
        web_dist=_env_path("HL_WEB_DIST", root / "web" / "dist"),
        sf_client_id=_env("SF_CLIENT_ID", "3MVG9harborline.etl.client"),
        sf_client_secret=_env("SF_CLIENT_SECRET", "hl-sf-secret-7c41"),
        carrier_api_key=_env("CARRIER_API_KEY", "hl-carrier-live-5e2a9d"),
        default_preset=_env("HL_DEFAULT_PRESET", "medium").lower(),
        default_seed=_env_int("HL_SEED", 20261031),
        offline=_env_bool("HL_OFFLINE", False),
        stream_checkpoint_s=_env_int("HL_STREAM_CHECKPOINT_S", 60),
        landing_host_hint=_env("HL_LANDING_HOST_HINT", r".\landing (next to docker-compose.yml)"),
        history_end=os.environ.get("HL_HISTORY_END") or None,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings() -> None:
    """Forget the cached settings (tests change the environment and call this)."""
    get_settings.cache_clear()
