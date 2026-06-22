"""Health check endpoints — liveness vs readiness"""
from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from recommender.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness():
    """Process 還活著嗎? — 用於 K8s liveness probe"""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(session: SessionDep):
    """真的能服務嗎? — 用於 K8s readiness probe"""
    try:
        await session.exec(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(503, f"db unavailable: {e}") from e
    return {"status": "ready", "db": "ok"}


@router.get("/")
async def root():
    return {"service": "marketing-cleaner", "version": "0.1.0"}
