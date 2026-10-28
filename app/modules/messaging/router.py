import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.core.config import get_settings
from app.core.deps import Tenant
from app.core.errors import AppError, not_found
from app.core.ids import new_id
from app.core.money import BN_DIGITS, month_label
from app.core.timeutil import DHAKA, current_month, local_day_bounds_utc, now_utc, today_local
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.messaging import service
from app.modules.messaging.models import MessageTemplate, OutboxMessage
from app.modules.messaging.templates import DEFAULT_TEMPLATES, NOTICE_PREFIX, count_segments, render

router = APIRouter(prefix="/messages", tags=["messaging"])

# Log filter groups, as the SMS page's tabs name them.
KINDS = {
    "attendance": ("absent", "present"),
    "receipt": ("receipt",),
    "reminder": ("reminder_1", "reminder_2", "reminder_3"),
    "notice": ("notice",),
    "welcome": ("welcome",),
}
QUEUED = ("queued", "held", "sending")
REACHED = ("sent", "delivered")


class MessageOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID | None
    student_name: str | None
    guardian_name: str | None
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
    name: str | None
    kind: str  # auto | notice
    body_bn: str
    body_en: str | None
    default_bn: str | None  # the shipped text of an automatic template, for "restore"
    is_enabled: bool
    segments: int


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    body_bn: str | None = Field(default=None, min_length=5, max_length=500)
    is_enabled: bool | None = None


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    body_bn: str = Field(min_length=5, max_length=500)


class Audience(BaseModel):
    """Who a notice goes to: everyone, some batches, and/or hand-picked students."""

    all: bool = False
    batch_ids: list[uuid.UUID] = Field(default_factory=list)
    student_ids: list[uuid.UUID] = Field(default_factory=list)


class NoticeIn(Audience):
    message: str = Field(min_length=3, max_length=500)
    batch_id: uuid.UUID | None = None  # single-batch shortcut kept for older screens
    title: str | None = Field(default=None, max_length=60)
    send_at: datetime | None = None  # None = now


class AudienceOut(BaseModel):
    students: int
    guardians: int  # distinct phones (siblings share one SMS)
    missing_phone: int


class MessageOverview(BaseModel):
    month: str
    sent: int  # messages created this month, cancelled ones left out
    guardians: int
    segments: int
    reached: int
    queued: int
    failed: int
    gateway_connected: bool
    scheduled_notices: int
    counts: dict[str, int]  # per KINDS group, plus "failed"


class NoticeOut(BaseModel):
    id: uuid.UUID
    title: str | None
    message: str
    targets: list[str]  # batch names, or ["সব ব্যাচ"]
    students: int
    queued: int
    created_at: datetime
    send_at: datetime | None
    pending: bool  # scheduled and not yet due


def _template_out(r: MessageTemplate) -> TemplateOut:
    auto = not r.key.startswith(NOTICE_PREFIX)
    return TemplateOut(
        key=r.key, name=r.name, kind="auto" if auto else "notice", body_bn=r.body_bn, body_en=r.body_en,
        default_bn=DEFAULT_TEMPLATES.get(r.key, {}).get("bn") if auto else None, is_enabled=r.is_enabled,
        segments=count_segments(r.body_bn),
    )


def _bn_date(d) -> str:
    return f"{str(d.day).translate(BN_DIGITS)} {month_label(d)}"


@router.get("", response_model=list[MessageOut])
async def list_messages(
    t: Tenant,
    student_id: uuid.UUID | None = None,
    status: str | None = None,
    kind: str | None = Query(default=None, pattern="^(attendance|receipt|reminder|notice|welcome|failed)$"),
    limit: int = Query(default=100, ge=1, le=500),
):
    q = (
        select(OutboxMessage, Student.name, Student.guardian_name)
        .outerjoin(Student, Student.id == OutboxMessage.student_id)
        .order_by(OutboxMessage.created_at.desc())
        .limit(limit)
    )
    if student_id:
        q = q.where(OutboxMessage.student_id == student_id)
    if status:
        q = q.where(OutboxMessage.status == status)
    if kind == "failed":
        q = q.where(OutboxMessage.status == "failed")
    elif kind:
        q = q.where(OutboxMessage.template_key.in_(KINDS[kind]))
    return [
        MessageOut(
            id=m.id, student_id=m.student_id, student_name=name, guardian_name=guardian, to_phone=m.to_phone,
            template_key=m.template_key,
            body=m.body, segments=m.segments, status=m.status, send_after=m.send_after, sent_at=m.sent_at,
            created_at=m.created_at,
        )
        for m, name, guardian in (await t.db.execute(q)).all()
    ]


@router.get("/stats")
async def message_stats(t: Tenant):
    rows = (await t.db.execute(select(OutboxMessage.status, func.count()).group_by(OutboxMessage.status))).all()
    return {status: n for status, n in rows}


@router.get("/overview", response_model=MessageOverview)
async def message_overview(t: Tenant):
    """This month's SMS numbers for the SMS page header."""
    cm = current_month()
    start = local_day_bounds_utc(cm)[0]
    month = select(OutboxMessage).where(OutboxMessage.created_at >= start, OutboxMessage.status != "cancelled").subquery()
    sent, guardians, segments, reached, queued, failed = (
        await t.db.execute(
            select(
                func.count(),
                func.count(func.distinct(month.c.to_phone)),
                func.coalesce(func.sum(month.c.segments), 0),
                func.count().filter(month.c.status.in_(REACHED)),
                func.count().filter(month.c.status.in_(QUEUED)),
                func.count().filter(month.c.status == "failed"),
            ).select_from(month)
        )
    ).one()
    by_key = dict((await t.db.execute(select(month.c.template_key, func.count()).group_by(month.c.template_key))).all())
    counts = {k: sum(by_key.get(x, 0) for x in keys) for k, keys in KINDS.items()}
    counts["failed"] = int(failed)
    scheduled = len(await _scheduled_notices(t))
    return MessageOverview(
        month=cm.isoformat(), sent=sent, guardians=guardians, segments=int(segments), reached=reached, queued=queued,
        failed=int(failed), gateway_connected=get_settings().sms_provider != "console", scheduled_notices=scheduled,
        counts=counts,
    )


async def _scheduled_notices(t: Tenant) -> list[AuditLog]:
    rows = (
        await t.db.execute(
            select(AuditLog)
            .where(AuditLog.action == "notice.sent", AuditLog.after["send_at"].astext > now_utc().astimezone(UTC).isoformat())
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars()
    return list(rows)


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(t: Tenant):
    # Institutions created before notice templates existed get the starter set here.
    await service.ensure_default_templates(t.db, t.institution_id)
    rows = (await t.db.execute(select(MessageTemplate).order_by(MessageTemplate.created_at, MessageTemplate.key))).scalars()
    return [_template_out(r) for r in rows]


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(body: TemplateIn, t: Tenant):
    """A new notice template. Automatic messages have a fixed set and cannot be added."""
    t.require("message.edit_templates")
    tpl = MessageTemplate(
        institution_id=t.institution_id, key=f"{NOTICE_PREFIX}{new_id().hex[:12]}", name=body.name.strip(),
        body_bn=body.body_bn.strip(),
    )
    t.db.add(tpl)
    await t.db.flush()
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="template.created",
        entity="message_template", entity_id=tpl.id, after={"name": tpl.name, "body_bn": tpl.body_bn}, ip=t.ip,
    )
    return _template_out(tpl)


async def _template(t: Tenant, key: str) -> MessageTemplate:
    tpl = (await t.db.execute(select(MessageTemplate).where(MessageTemplate.key == key))).scalar_one_or_none()
    if not tpl:
        raise not_found("Template")
    return tpl


@router.patch("/templates/{key}", response_model=TemplateOut)
async def update_template(key: str, body: TemplateUpdate, t: Tenant):
    t.require("message.edit_templates")
    tpl = await _template(t, key)
    before = {"name": tpl.name, "body_bn": tpl.body_bn, "is_enabled": tpl.is_enabled}
    if body.body_bn is not None:
        tpl.body_bn = body.body_bn.strip()
    if body.is_enabled is not None:
        tpl.is_enabled = body.is_enabled
    if body.name is not None and key.startswith(NOTICE_PREFIX):
        tpl.name = body.name.strip()
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="template.updated",
        entity="message_template", entity_id=tpl.id, before=before,
        after={"name": tpl.name, "body_bn": tpl.body_bn, "is_enabled": tpl.is_enabled}, ip=t.ip,
    )
    return _template_out(tpl)


@router.delete("/templates/{key}", status_code=204)
async def delete_template(key: str, t: Tenant):
    t.require("message.edit_templates")
    if not key.startswith(NOTICE_PREFIX):
        raise AppError("template_fixed", "Automatic messages can be turned off, not deleted",
                       "অটো মেসেজ মোছা যায় না, বন্ধ করা যায়", 409)
    tpl = await _template(t, key)
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="template.deleted",
        entity="message_template", entity_id=tpl.id, before={"name": tpl.name, "body_bn": tpl.body_bn}, ip=t.ip,
    )
    await t.db.delete(tpl)


async def _audience(t: Tenant, a: Audience) -> list[tuple[Student, str | None]]:
    """Active students the notice reaches, each with the batch name to put in {batch}."""
    base = (
        select(Student, Batch.name)
        .outerjoin(Enrollment, (Enrollment.student_id == Student.id) & Enrollment.end_month.is_(None) & Enrollment.deleted_at.is_(None))
        .outerjoin(Batch, Batch.id == Enrollment.batch_id)
        .where(Student.status == "active", Student.deleted_at.is_(None))
        .order_by(Student.id, Batch.name)
    )
    if not a.all:
        picked = []
        if a.batch_ids:
            picked.append(Enrollment.batch_id.in_(a.batch_ids))
        if a.student_ids:
            picked.append(Student.id.in_(a.student_ids))
        if not picked:
            return []
        base = base.where(or_(*picked))
    out: dict[uuid.UUID, tuple[Student, str | None]] = {}
    for st, batch in (await t.db.execute(base)).all():
        out.setdefault(st.id, (st, batch))
    return list(out.values())


@router.post("/notice/audience", response_model=AudienceOut)
async def notice_audience(body: Audience, t: Tenant):
    """How many guardians a notice would reach, before sending it."""
    people = await _audience(t, body)
    phones = {s.guardian_phone for s, _ in people if s.guardian_phone}
    return AudienceOut(students=len(people), guardians=len(phones), missing_phone=sum(1 for s, _ in people if not s.guardian_phone))


@router.get("/notices", response_model=list[NoticeOut])
async def list_notices(t: Tenant, limit: int = Query(default=10, ge=1, le=50)):
    """Notices sent (or scheduled) before, newest first."""
    rows = (
        await t.db.execute(
            select(AuditLog).where(AuditLog.action == "notice.sent").order_by(AuditLog.created_at.desc()).limit(limit)
        )
    ).scalars()
    out = []
    now = now_utc().astimezone(UTC).isoformat()
    for r in rows:
        a = r.after or {}
        out.append(
            NoticeOut(
                id=r.id, title=a.get("title"), message=a.get("message", ""), targets=a.get("targets") or [],
                students=a.get("students", a.get("queued", 0)), queued=a.get("queued", 0), created_at=r.created_at,
                send_at=a.get("send_at"), pending=bool(a.get("send_at")) and a["send_at"] > now,
            )
        )
    return out


@router.post("/notice")
async def send_notice(body: NoticeIn, t: Tenant):
    """Notice to everyone, some batches or chosen students: 'আগামীকাল ক্লাস বন্ধ'.

    {student}, {batch} and {date} in the text are filled in per student. One SMS per guardian
    phone (siblings share it). `send_at` schedules it; quiet hours still apply.
    """
    t.require("message.send_notice")
    if body.batch_id and body.batch_id not in body.batch_ids:
        body.batch_ids.append(body.batch_id)
    if not (body.batch_ids or body.student_ids):
        body.all = True
    send_at = body.send_at if body.send_at and body.send_at > now_utc() else None
    people = await _audience(t, body)
    day = (send_at.astimezone(DHAKA).date() if send_at else today_local())
    digest = hashlib.sha1(f"{body.message}:{sorted(map(str, body.batch_ids))}:{sorted(map(str, body.student_ids))}:{day}".encode()).hexdigest()[:12]
    queued, seen = 0, set()
    for s, batch in people:
        if not s.guardian_phone or s.guardian_phone in seen:
            continue
        seen.add(s.guardian_phone)
        text = render(body.message.strip(), {"student": s.name, "batch": batch or "", "date": _bn_date(day)})
        msg = await service.enqueue(
            t.db, institution_id=t.institution_id, template_key="notice", to_phone=s.guardian_phone,
            student_id=s.id, dedupe_key=f"notice:{digest}:{s.guardian_phone}", not_before=send_at,
            values={"message": text, "institution": t.institution.name},
        )
        queued += 1 if msg else 0
    if body.all:
        targets = ["সব ব্যাচ"]
    else:
        names = (await t.db.execute(select(Batch.name).where(Batch.id.in_(body.batch_ids)))).scalars().all() if body.batch_ids else []
        targets = list(names) + ([f"{len(body.student_ids)} জন স্টুডেন্ট"] if body.student_ids else [])
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="notice.sent", entity="outbox",
        after={
            "title": body.title, "message": body.message, "batch_ids": [str(b) for b in body.batch_ids],
            "targets": targets, "students": len(people), "queued": queued,
            # Stored in UTC so scheduled notices can be found by plain string comparison.
            "send_at": send_at.astimezone(UTC).isoformat() if send_at else None,
        },
        ip=t.ip,
    )
    return {"queued": queued, "recipients": len(seen)}
