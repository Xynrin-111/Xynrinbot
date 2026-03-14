"""
数据库初始化与会话工厂。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import plugin_settings
from .models import Base


engine: AsyncEngine = create_async_engine(
    plugin_settings.database_url,
    echo=False,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """启动时自动建表。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
