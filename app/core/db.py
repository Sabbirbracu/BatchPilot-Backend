import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import get_settings
from app.core.ids import new_id

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


engine = create_async_engine(get_settings().database_url, pool_size=10, max_overflow=10, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

# Cross-tenant reads only (e.g. "which institutions know this guardian's phone?"). Runs as bp_ops
# (BYPASSRLS), so keep every use narrow and never hand its sessions to request code.
ops_engine = create_async_engine(get_settings().ops_database_url, pool_size=2, max_overflow=2, pool_pre_ping=True)
OpsSessionLocal = async_sessionmaker(ops_engine, expire_on_commit=False, autoflush=False)


async def set_context(
    db: AsyncSession, *, user_id: uuid.UUID | None = None, institution_id: uuid.UUID | None = None
) -> None:
    """Transaction-local settings read by the RLS policies (safe with PgBouncer transaction mode)."""
    if user_id is not None:
        await db.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": str(user_id)})
    if institution_id is not None:
        await db.execute(
            text("SELECT set_config('app.institution_id', :v, true)"), {"v": str(institution_id)}
        )


async def get_db() -> AsyncIterator[AsyncSession]:
    """One transaction per request: committed when the endpoint returns, rolled back on error."""
    async with SessionLocal() as session:
        async with session.begin():
            yield session
