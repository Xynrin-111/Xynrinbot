"""
SQLAlchemy 异步 ORM 模型定义。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class VerifyStatus:
    """验证状态常量，统一集中管理方便维护。"""

    PENDING = "待验证"
    PASSED = "已通过"
    KICKED = "已踢出"
    TIMEOUT_KICKED = "已超时踢出"


class GroupConfig(Base):
    """群配置表，控制指定群是否启用验证及验证参数。"""

    __tablename__ = "group_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_error_times: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )


class AppConfig(Base):
    """插件全局配置表，供本地 Web 管理页面保存交互式配置。"""

    __tablename__ = "app_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    config_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )


class VerifyRecord(Base):
    """新人验证记录表。"""

    __tablename__ = "verify_records"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_verify_user_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    group_id: Mapped[int] = mapped_column(Integer, index=True)
    verify_code: Mapped[str] = mapped_column(String(4), nullable=False)
    join_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expire_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=VerifyStatus.PENDING, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )
