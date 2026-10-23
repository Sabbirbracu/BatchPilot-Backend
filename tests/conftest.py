"""Integration tests run against the local Postgres (as bp_app, so RLS is real)."""

import os
import random
import uuid

# Tests use their own database (create it once with: DB=batchpilot_test ./scripts/setup_local_db.sh).
TEST_DB = os.environ.get("TEST_DB", "batchpilot_test")
os.environ["DATABASE_URL"] = f"postgresql+psycopg://bp_app:bp_app_dev@localhost:5432/{TEST_DB}"
os.environ["OPS_DATABASE_URL"] = f"postgresql+psycopg://bp_ops:bp_ops_dev@localhost:5432/{TEST_DB}"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.main import app  # noqa: E402


def rand_phone() -> str:
    return "017" + "".join(random.choice("0123456789") for _ in range(8))


async def login(client: httpx.AsyncClient, phone: str) -> dict:
    r = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = r.json()["dev_code"]
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def owner():
    """A logged-in owner with a fresh institution and one ৳800 batch."""
    async with new_client() as c:
        await login(c, rand_phone())
        r = await c.post("/api/v1/institutions", json={"name": f"Test Coaching {uuid.uuid4().hex[:6]}", "owner_name": "Sir"})
        assert r.status_code == 201, r.text
        r = await c.post(
            "/api/v1/batches",
            json={"name": "SSC A", "days": ["sat", "mon"], "start_time": "16:00", "monthly_fee": 80000},
        )
        assert r.status_code == 201, r.text
        c.batch_id = r.json()["id"]
        yield c


async def add_student(c: httpx.AsyncClient, name: str = "Rafi", guardian: str | None = None) -> dict:
    r = await c.post(
        "/api/v1/students",
        json={"name": name, "guardian_phone": guardian or rand_phone(), "enrollments": [{"batch_id": c.batch_id}]},
    )
    assert r.status_code == 201, r.text
    return r.json()
