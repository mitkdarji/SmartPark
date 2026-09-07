"""Settings parsing.

These exist because of a real failure: `CORS_ORIGINS=a,b` crashed the container
at import time while local development was fine. pydantic-settings JSON-decodes
a list-typed environment variable *before* any validator runs, and the default
factory meant the code path was never exercised until the value was actually set
in an environment — which only happens in deployment.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key.upper(), value)
    # Ignore any .env on the developer's machine so the test is hermetic.
    return Settings(_env_file=None)


def test_comma_separated_list_env_vars_parse(monkeypatch):
    settings = build(
        monkeypatch,
        cors_origins="http://a.example,http://b.example",
        anpr_ocr_backends="easyocr,tesseract",
    )
    assert settings.cors_origins == ["http://a.example", "http://b.example"]
    assert settings.anpr_ocr_backends == ["easyocr", "tesseract"]


def test_json_array_list_env_vars_also_parse(monkeypatch):
    settings = build(monkeypatch, cors_origins='["http://x.example","http://y.example"]')
    assert settings.cors_origins == ["http://x.example", "http://y.example"]


def test_whitespace_and_empty_entries_are_stripped(monkeypatch):
    settings = build(monkeypatch, cors_origins=" http://a.example , , http://b.example ")
    assert settings.cors_origins == ["http://a.example", "http://b.example"]


def test_defaults_apply_when_unset(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("ANPR_OCR_BACKENDS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.cors_origins
    assert settings.anpr_ocr_backends


def test_relative_sqlite_path_resolves_against_the_backend_root(monkeypatch):
    settings = build(monkeypatch, database_url="sqlite+aiosqlite:///./data/x.db")
    # Must be absolute, or the database lands wherever the process happened to start.
    assert "sqlite+aiosqlite:////" in settings.database_url or settings.database_url.count("/") > 4
    assert "/./" not in settings.database_url


def test_postgres_url_is_left_untouched(monkeypatch):
    url = "postgresql+asyncpg://u:p@db:5432/smartpark"
    assert build(monkeypatch, database_url=url).database_url == url


def test_genai_is_disabled_without_a_key(monkeypatch):
    assert not build(monkeypatch, anthropic_api_key="").genai_enabled
    assert not build(monkeypatch, anthropic_api_key="   ").genai_enabled
    assert build(monkeypatch, anthropic_api_key="sk-ant-test").genai_enabled
