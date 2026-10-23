"""Transactional outbox. Callers enqueue inside their own transaction; nothing here calls
an SMS provider. A worker (next module) picks rows up and sends them."""

import uuid
from datetime import datetime, time, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_id
from app.core.timeutil import DHAKA, local_day_bounds_utc, now_utc
from app.modules.messaging.models import MessageTemplate, OutboxMessage
from app.modules.messaging.templates import (
    CAP_EXEMPT,
    DEFAULT_TEMPLATES,
    PRIORITY,
    count_segments,
    render,
)

DAILY_CAP = 2


async def ensure_default_templates(db: AsyncSession, institution_id: uuid.UUID) -> None:
    rows = [
        {"institution_id": institution_id, "key": k, "body_bn": v["bn"], "body_en": v["en"]}
        for k, v in DEFAULT_TEMPLATES.items()
    ]
    stmt = insert(MessageTemplate).values([{"id": new_id(), **r} for r in rows])
    await db.execute(stmt.on_conflict_do_nothing(index_elements=["institution_id", "key"]))


def next_allowed_send_time(at: datetime) -> tuple[datetime, bool]:
    """Returns (send_after, held). Guardians get nothing between 21:00 and 08:00 Asia/Dhaka."""
    s = get_settings()
    local = at.astimezone(DHAKA)
    if s.quiet_end_hour <= local.hour < s.quiet_start_hour:
        return at, False
    day = local.date() if local.hour < s.quiet_end_hour else local.date() + timedelta(days=1)
    opens = datetime.combine(day, time(s.quiet_end_hour), tzinfo=DHAKA)
    return opens, True


async def enqueue(
    db: AsyncSession,
    *,
    institution_id: uuid.UUID,
    template_key: str,
    to_phone: str | None,
    values: dict,
    dedupe_key: str,
    student_id: uuid.UUID | None = None,
) -> OutboxMessage | None:
    """Queue one SMS. Returns None when skipped (no phone, template off, or duplicate)."""
    if not to_phone:
        return None
    tpl = (
        await db.execute(
            select(MessageTemplate).where(
                MessageTemplate.institution_id == institution_id, MessageTemplate.key == template_key
            )
        )
    ).scalar_one_or_none()
    if tpl is not None and not tpl.is_enabled:
        return None
    body_src = tpl.body_bn if tpl else DEFAULT_TEMPLATES[template_key]["bn"]
    body = render(body_src, values)

    now = now_utc()
    send_after, held = next_allowed_send_time(now)

    if template_key not in CAP_EXEMPT:
        start, end = local_day_bounds_utc(send_after.astimezone(DHAKA).date())
        sent_today = await db.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(
                OutboxMessage.to_phone == to_phone,
                OutboxMessage.template_key.not_in(CAP_EXEMPT),
                OutboxMessage.status != "cancelled",
                OutboxMessage.send_after >= start,
                OutboxMessage.send_after < end,
            )
        )
        if (sent_today or 0) >= DAILY_CAP:
            # Over the cap: hold until the window opens on the next day.
            send_after, _ = next_allowed_send_time(end)
            held = True

    stmt = (
        insert(OutboxMessage)
        .values(
            id=new_id(),
            institution_id=institution_id,
            student_id=student_id,
            to_phone=to_phone,
            template_key=template_key,
            body=body,
            segments=count_segments(body),
            priority=PRIORITY.get(template_key, 5),
            status="held" if held else "queued",
            dedupe_key=dedupe_key,
            send_after=send_after,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(OutboxMessage)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def cancel_pending(db: AsyncSession, dedupe_key: str) -> None:
    """Cancel a message that hasn't gone out yet (e.g. an absence corrected to present)."""
    await db.execute(
        update(OutboxMessage)
        .where(OutboxMessage.dedupe_key == dedupe_key, OutboxMessage.status.in_(("queued", "held")))
        .values(status="cancelled")
    )
