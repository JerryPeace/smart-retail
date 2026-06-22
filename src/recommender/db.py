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
    """FastAPI dependency: yield 一次 request 用的 session"""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """應用啟動時呼叫(目前 Alembic 處理 migration,這裡保留 hook)"""
    # 之後可以加 connection check 或其他啟動驗證
    pass
