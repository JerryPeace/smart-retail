"""End-to-end pipeline tests — mock mode, zero Bedrock calls.

Pre-conditions:
    docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres

These tests require a live Postgres instance at localhost:5434 (the dev
docker-compose default).  If the DB is unreachable the whole module is
skipped so the suite does not spuriously fail in CI environments without
Docker.

ANALYZER_MOCK_MODE=true is enforced in conftest.py — no AWS credentials
or Bedrock calls happen during these tests.
"""
import asyncio
import os

import pytest

# DB reachability check — run once at collection time.
# We import asyncpg directly (already a transitive dep via asyncpg/sqlmodel).
_DB_AVAILABLE = False
try:
    import asyncpg  # noqa: F401

    async def _ping() -> bool:
        db_url = os.getenv(
            "DATABASE_URL",
            "postgresql://poc:poc@localhost:5434/marketing_cleaner",
        )
        # asyncpg does not accept the SQLAlchemy "+asyncpg" driver suffix —
        # strip it so the URL is valid for asyncpg.connect().
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        try:
            conn = await asyncpg.connect(db_url, timeout=3)
            await conn.close()
            return True
        except Exception:
            return False

    _DB_AVAILABLE = asyncio.run(_ping())
except Exception:
    _DB_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(
        not _DB_AVAILABLE,
        reason="Postgres not available — run: docker compose -f docker-compose.dev.yml --env-file .env.local up -d postgres",
    ),
    # All tests in this module share one event loop so the SQLAlchemy asyncpg
    # connection pool (created in the first test) remains valid for subsequent
    # tests.  Without this, each test function gets its own loop (the default),
    # and asyncpg connections created in loop N raise
    # "Future attached to a different loop" in loop N+1.
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_POLL = 30       # iterations
_POLL_INTERVAL = 0.5  # seconds between polls


async def _poll_until_done(client, job_id: int) -> dict:
    """Poll GET /pipelines/{job_id} until status is 'done' or terminal failure."""
    for _ in range(_MAX_POLL):
        resp = await client.get(f"/pipelines/{job_id}")
        assert resp.status_code == 200, f"GET /pipelines/{job_id} returned {resp.status_code}: {resp.text}"
        data = resp.json()
        status = data["status"]
        if status == "done":
            return data
        if status == "failed":
            pytest.fail(f"Pipeline job {job_id} failed: {data.get('error')}")
        await asyncio.sleep(_POLL_INTERVAL)
    pytest.fail(f"Pipeline job {job_id} did not reach 'done' within {_MAX_POLL * _POLL_INTERVAL}s")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_pipeline_full_flow_mock_mode(client):
    """POST /pipelines/run → poll done → GET recommendation → POST evaluation → list evaluations."""
    # Step 1: trigger pipeline
    resp = await client.post(
        "/pipelines/run",
        json={"customer_id": "TEST_DEALER_001", "brand": "Apple", "month": "2026-05"},
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    job = resp.json()
    assert "job_id" in job
    job_id = job["job_id"]
    assert job["status"] == "queued"

    # Step 2: poll until done
    done_job = await _poll_until_done(client, job_id)
    assert done_job["status"] == "done"
    rec_id = done_job.get("recommendation_id")
    assert rec_id is not None, "Done job must have recommendation_id"

    # Step 3: fetch recommendation
    resp = await client.get(f"/recommendations/{rec_id}")
    assert resp.status_code == 200, f"GET /recommendations/{rec_id} returned {resp.status_code}: {resp.text}"
    rec = resp.json()

    # Verify core fields are present (mock mode produces a real RecommendationOutput structure)
    assert rec["id"] == rec_id
    assert rec["customer_id"] == "TEST_DEALER_001"
    assert "payload" in rec
    assert "customer_segment" in rec
    assert "confidence_score" in rec
    assert isinstance(rec["payload"], dict)
    # payload must follow RecommendationOutput schema
    assert "recommended_products" in rec["payload"]
    assert len(rec["payload"]["recommended_products"]) >= 1

    # Step 4: create evaluation
    resp = await client.post(f"/evaluations/{rec_id}")
    assert resp.status_code == 201, f"POST /evaluations/{rec_id} returned {resp.status_code}: {resp.text}"
    evaluation = resp.json()

    assert evaluation["recommendation_id"] == rec_id
    assert evaluation["judge_model_id"] == "mock"
    # All score fields must be present and in [0,1]
    for score_field in (
        "relevance_score", "specificity_score", "actionability_score",
        "hallucination_score", "overall_score",
    ):
        assert score_field in evaluation, f"Missing field: {score_field}"
        assert 0.0 <= evaluation[score_field] <= 1.0, f"{score_field} out of range"
    assert "judge_reasoning" in evaluation

    # Step 5: list evaluations by recommendation
    resp = await client.get(f"/evaluations/by-recommendation/{rec_id}")
    assert resp.status_code == 200
    evals = resp.json()
    assert isinstance(evals, list)
    assert len(evals) >= 1
    eval_ids = [e["id"] for e in evals]
    assert evaluation["id"] in eval_ids


# ---------------------------------------------------------------------------
# Negative paths — verify B1: NotFoundError → handler → 404
# ---------------------------------------------------------------------------

async def test_get_job_not_found(client):
    resp = await client.get("/pipelines/999999")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body


async def test_get_recommendation_not_found(client):
    resp = await client.get("/recommendations/999999")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body


async def test_get_evaluation_not_found(client):
    resp = await client.get("/evaluations/999999")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body


async def test_create_evaluation_recommendation_not_found(client):
    """POST /evaluations/{id} for non-existent recommendation → 404."""
    resp = await client.post("/evaluations/999999")
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
