"""Typed application settings, loaded from environment / .env.

Every AI-provider key is optional. `Settings` exposes `*_enabled` properties so
services can degrade to deterministic local engines instead of crashing when a
credential is absent — the platform must always be demo-able offline.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Application ───────────────────────────────────────────
    app_env: str = "development"
    app_name: str = "SmartPark"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    # ── Security ──────────────────────────────────────────────
    secret_key: str = "dev-only-insecure-secret-change-me"
    access_token_expire_minutes: int = 720
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # ── Database ──────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./data/smartpark.db"
    db_echo: bool = False

    # ── Cache ─────────────────────────────────────────────────
    redis_url: str = ""

    # ── Generative AI ─────────────────────────────────────────
    anthropic_api_key: str = ""
    genai_model: str = "claude-opus-5"
    genai_fast_model: str = "claude-haiku-4-5"
    genai_max_tokens: int = 2048

    # ── Computer vision ───────────────────────────────────────
    anpr_ocr_backends: list[str] = Field(
        default_factory=lambda: ["easyocr", "tesseract", "vision_llm", "synthetic"]
    )
    anpr_min_confidence: float = 0.55
    anpr_plate_region: str = "IN"

    # ── Voice ─────────────────────────────────────────────────
    voice_stt_backend: str = "faster_whisper"
    voice_tts_backend: str = "pyttsx3"

    # ── Payments ──────────────────────────────────────────────
    payment_provider: str = "mock"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    stripe_secret_key: str = ""

    # ── Third-party integrations ──────────────────────────────
    openweather_api_key: str = ""
    maps_provider: str = "osm"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # ── Smart-city feed ───────────────────────────────────────
    city_feed_enabled: bool = True
    city_feed_operator_id: str = "pdeu-smartpark-001"

    # ── Automation ────────────────────────────────────────────
    automation_enabled: bool = True
    automation_tick_seconds: int = 60

    # ── Derived paths ─────────────────────────────────────────
    @property
    def data_dir(self) -> Path:
        d = BACKEND_ROOT / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def upload_dir(self) -> Path:
        d = self.data_dir / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def model_dir(self) -> Path:
        d = self.data_dir / "models"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Capability flags ──────────────────────────────────────
    @property
    def genai_enabled(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @field_validator("cors_origins", "anpr_ocr_backends", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, v: str) -> str:
        """Make relative SQLite paths resolve against the backend root, not cwd."""
        prefix = "sqlite+aiosqlite:///./"
        if v.startswith(prefix):
            return f"sqlite+aiosqlite:///{(BACKEND_ROOT / v[len(prefix):]).resolve()}"
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
