"""Test fixtures.

Every test runs against a fresh in-memory SQLite database. `StaticPool` keeps
one connection alive so the app and the test share the same in-memory schema —
without it each session would get its own empty database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("AUTOMATION_ENABLED", "false")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import httpx
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.enums import UserRole


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def client(engine) -> AsyncGenerator[httpx.AsyncClient, None]:
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def register(
    client: httpx.AsyncClient, email: str, *, role: str = UserRole.DRIVER, plate: str | None = None
) -> dict:
    payload = {
        "email": email,
        "password": "TestPassword123!",
        "full_name": "Test User",
        "role": role,
    }
    if plate:
        payload["plate"] = plate
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def owner(client) -> dict:
    return await register(client, "owner@test.dev", role=UserRole.OWNER)


@pytest.fixture
async def driver(client) -> dict:
    return await register(client, "driver@test.dev", plate="GJ01AB1234")


@pytest.fixture
async def facility(client, owner) -> dict:
    """A public facility with one level, one gate and a 4x6 grid of bays."""
    response = await client.post(
        "/api/v1/facilities",
        headers=auth(owner["access_token"]),
        json={
            "name": "Test Car Park",
            "city": "Gandhinagar",
            "base_rate_minor_per_hour": 4000,
            "free_minutes": 15,
            "billing_increment_minutes": 15,
            "tax_percent": 18.0,
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()

    levels = (
        await client.get(f"/api/v1/facilities/{created['id']}/levels")
    ).json()
    generated = await client.post(
        f"/api/v1/facilities/{created['id']}/slots/generate"
        f"?level_id={levels[0]['id']}&rows=4&columns=6&zone_prefix=A&ev_every=6",
        headers=auth(owner["access_token"]),
    )
    assert generated.status_code == 201, generated.text
    created["slots"] = generated.json()
    created["level_id"] = levels[0]["id"]
    return created
