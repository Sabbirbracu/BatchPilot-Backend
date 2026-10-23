import hashlib
import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.deps import Tenant
from app.core.errors import not_found
from app.core.timeutil import today_local
from app.modules.academics.models import Enrollment, Student
from app.modules.audit import service as audit
from app.modules.messaging import service
from app.modules.messaging.models import MessageTemplate, OutboxMessage
from app.modules.messaging.templates import count_segments

router = APIRouter(prefix="/messages", tags=["messaging"])


class MessageOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID | None
    student_name: str | None
    to_phone: str
    template_key: str
    body: str
    segments: int
    status: str
    send_after: datetime
    sent_at: datetime | None
    created_at: datetime


class TemplateOut(BaseModel):
    key: str
    body_bn: str
    body_en: str | None
    is_enabled: bool
    segments: int


class TemplateUpdate(BaseModel):
    body_bn: str | None = Field(default=None, min_length=5, max_length=500)
    is_enabled: bool | None = None


class NoticeIn(BaseModel):
    message: str = Field(min_length=3, max_length=300)
    batch_id: uuid.UUID | None = None


@router.get("", response_model=list[MessageOut])
async def list_messages(
    t: Tenant,
    student_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    q = (
        select(OutboxMessage, Student.name)
        .outerjoin(Student, Student.id == OutboxMessage.student_id)
        .order_by(OutboxMessage.created_at.desc())
        .limit(limit)
    )
    if student_id:
        q = q.where(OutboxMessage.student_id == student_id)
    if status:
        q = q.where(OutboxMessage.status == status)
    return [
        MessageOut(
            id=m.id, student_id=m.student_id, student_name=name, to_phone=m.to_phone, template_key=m.template_key,
            body=m.body, segments=m.segments, status=m.status, send_after=m.send_after, sent_at=m.sent_at,
            created_at=m.created_at,
        )
        for m, name in (await t.db.execute(q)).all()
    ]


@router.get("/stats")
async def message_stats(t: Tenant):
    rows = (await t.db.execute(select(OutboxMessage.status, func.count()).group_by(OutboxMessage.status))).all()
    return {status: n for status, n in rows}


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(t: Tenant):
    rows = (await t.db.execute(select(MessageTemplate).order_by(MessageTemplate.key))).scalars()
    return [
        TemplateOut(key=r.key, body_bn=r.body_bn, body_en=r.body_en, is_enabled=r.is_enabled, segments=count_segments(r.body_bn))
        for r in rows
    ]


@router.patch("/templates/{key}", response_model=TemplateOut)
async def update_template(key: str, body: TemplateUpdate, t: Tenant):
    t.require("message.edit_templates")
    tpl = (await t.db.execute(select(MessageTemplate).where(MessageTemplate.key == key))).scalar_one_or_none()
    if not tpl:
        raise not_found("Template")
    before = {"body_bn": tpl.body_bn, "is_enabled": tpl.is_enabled}
    if body.body_bn is not None:
        tpl.body_bn = body.body_bn.strip()
    if body.is_enabled is not None:
        tpl.is_enabled = body.is_enabled
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="template.updated",
        entity="message_template", entity_id=tpl.id, before=before,
        after={"body_bn": tpl.body_bn, "is_enabled": tpl.is_enabled}, ip=t.ip,
    )
    return TemplateOut(
        key=tpl.key, body_bn=tpl.body_bn, body_en=tpl.body_en, is_enabled=tpl.is_enabled,
        segments=count_segments(tpl.body_bn),
    )


@router.post("/notice")
async def send_notice(body: NoticeIn, t: Tenant):
    """Notice to a whole batch or everyone: 'আগামীকাল ক্লাস বন্ধ'."""
    t.require("message.send_notice")
    q = select(Student).where(Student.status == "active", Student.deleted_at.is_(None), Student.guardian_phone.is_not(None))
    if body.batch_id:
        q = q.join(Enrollment, Enrollment.student_id == Student.id).where(
            Enrollment.batch_id == body.batch_id, Enrollment.end_month.is_(None), Enrollment.deleted_at.is_(None)
        )
    students = list((await t.db.execute(q)).scalars().unique())
    digest = hashlib.sha1(f"{body.message}:{body.batch_id}:{today_local()}".encode()).hexdigest()[:12]
    queued, seen = 0, set()
    for s in students:
        if s.guardian_phone in seen:  # siblings share a guardian phone: one SMS is enough
            continue
        seen.add(s.guardian_phone)
        msg = await service.enqueue(
            t.db, institution_id=t.institution_id, template_key="notice", to_phone=s.guardian_phone,
            student_id=s.id, dedupe_key=f"notice:{digest}:{s.guardian_phone}",
            values={"message": body.message.strip(), "institution": t.institution.name},
        )
        queued += 1 if msg else 0
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="notice.sent", entity="outbox",
        after={"message": body.message, "batch_id": body.batch_id, "queued": queued}, ip=t.ip,
    )
    return {"queued": queued, "recipients": len(seen)}
