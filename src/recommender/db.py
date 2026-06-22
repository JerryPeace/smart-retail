"""SQLModel async engine + session factory"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from recommender.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a session for one request."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Called at application startup (Alembic currently handles migrations; this keeps a hook)."""
    # Could later add a connection check or other startup validation
    pass
