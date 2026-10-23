import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog


def _jsonable(v: Any) -> Any:
    if isinstance(v, (uuid.UUID, date, datetime, time)):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def snapshot(obj: Any, fields: list[str]) -> dict:
    return {f: _jsonable(getattr(obj, f)) for f in fields}


async def record(
    db: AsyncSession,
    *,
    institution_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            institution_id=institution_id,
            actor_id=actor_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before=_jsonable(before) if before else None,
            after=_jsonable(after) if after else None,
            ip=ip,
        )
    )
