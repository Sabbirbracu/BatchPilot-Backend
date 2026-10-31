import io
import uuid

import segno
from fastapi import APIRouter, Header, Query, Response
from sqlalchemy import func, select

from app.core.db import set_context
from app.core.deps import DB, Auth, MaybeAuth, Tenant
from app.core.errors import AppError, not_found
from app.core.phone import normalize_bd_phone
from app.core.timeutil import current_month, local_day_bounds_utc, now_utc
from app.modules.academics import service as academics
from app.modules.academics.models import Batch, Student
from app.modules.academics.schemas import StudentIn
from app.modules.admissions import service
from app.modules.admissions.models import AdmissionLink, AdmissionRequest
from app.modules.admissions.schemas import (
    AdmissionOverview,
    AdmitIn,
    AdmittedOut,
    ApplyIn,
    GuardianLookupOut,
    JoinPageOut,
    LinkIn,
    LinkOut,
    LinkUpdate,
    OfferOut,
    OfficeAdmissionIn,
    AdmittedRow,
    RejectIn,
    RequestOut,
    SeatBatchOut,
    SiblingOut,
)
from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.fees.models import FeeDue
from app.modules.identity.models import Institution, Membership, User

router = APIRouter(prefix="/admissions", tags=["admissions"])
public = APIRouter(prefix="/public", tags=["public"])

IdemKey = Header(min_length=8, max_length=80, alias="Idempotency-Key")


async def _open_batches(db, ids: list[uuid.UUID] | None = None) -> list[SeatBatchOut]:
    q = select(Batch).where(Batch.deleted_at.is_(None), Batch.archived_at.is_(None))
    if ids is not None:
        q = q.where(Batch.id.in_(ids))
    batches = list((await db.execute(q.order_by(Batch.class_level.nulls_last(), Batch.start_time.nulls_last(), Batch.name))).scalars())
    stats = await service.seat_stats(db, [b.id for b in batches])
    return [service.seat_batch_out(b, stats[b.id]) for b in batches]


@router.get("/batches", response_model=list[SeatBatchOut])
async def admission_batches(t: Tenant):
    """Open batches with fees and seats left, for the new-admission screen and the QR editor."""
    await service.expire_stale(t.db)
    return await _open_batches(t.db)


@router.get("/overview", response_model=AdmissionOverview)
async def overview(t: Tenant):
    """Top of the admissions page: waiting requests, this month's admissions, free seats, QR views."""
    db = t.db
    await service.expire_stale(db)
    since, _ = local_day_bounds_utc(current_month())
    pending = await db.scalar(select(func.count()).where(AdmissionRequest.status == "pending"))
    admitted = await db.scalar(
        select(func.count()).where(AuditLog.action == "admission.admitted", AuditLog.created_at >= since)
    )
    fees = await db.scalar(
        select(func.coalesce(func.sum(FeeDue.amount - FeeDue.discount), 0)).where(
            FeeDue.kind == "admission", FeeDue.created_at >= since
        )
    )
    batches = [b for b in await _open_batches(db) if b.capacity]
    scans = await db.scalar(
        select(func.coalesce(func.sum(AdmissionLink.scan_count), 0)).where(AdmissionLink.deleted_at.is_(None))
    )
    return AdmissionOverview(
        pending=int(pending or 0), admitted_month=int(admitted or 0), admission_fees_month=int(fees or 0),
        free_seats=sum(b.seats_left or 0 for b in batches), seat_batches=len(batches), scans=int(scans or 0),
    )


@router.get("/admitted", response_model=list[AdmittedRow])
async def admitted(t: Tenant, limit: int = Query(default=100, le=300)):
    """Every admission, newest first — at the desk and from QR requests alike (read from the audit log)."""
    db = t.db
    owed = academics.outstanding_subquery()
    rows = (
        await db.execute(
            select(AuditLog, Student, User.name, func.coalesce(owed.c.outstanding, 0))
            .join(Student, Student.id == AuditLog.entity_id)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .outerjoin(owed, owed.c.student_id == Student.id)
            .where(AuditLog.action == "admission.admitted", Student.deleted_at.is_(None))
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    names = {str(k): v for k, v in (await db.execute(select(Batch.id, Batch.name))).all()}  # audit stores ids as text
    out = []
    for log, st, actor, outstanding in rows:
        a = log.after or {}
        out.append(
            AdmittedRow(
                student_id=st.id, student_code=st.student_code, name=st.name, class_level=st.class_level, school=st.school,
                guardian_name=st.guardian_name, guardian_phone=st.guardian_phone,
                batch_name=names.get(str(a.get("batch_id")), ""), at=log.created_at, paid=int(a.get("paid") or 0),
                admission_fee=max(0, int(a.get("admission_fee") or 0) - int((a.get("offer") or [0])[0] or 0) - int(a.get("admission_discount") or 0)),
                outstanding=int(outstanding or 0), from_qr=bool(a.get("request_id")), admitted_by=actor, status=st.status,
            )
        )
    return out


# ---------------------------------------------------------------- QR links


async def _links_out(t: Tenant, links: list[AdmissionLink]) -> list[LinkOut]:
    counts = await service.link_counts(t.db, [x.id for x in links])
    names = dict((await t.db.execute(select(Batch.id, Batch.name))).all())
    out = []
    for x in links:
        o = LinkOut.model_validate(x)
        o.pending = counts[x.id].get("pending", 0)
        o.admitted = counts[x.id].get("admitted", 0)
        o.batch_names = [names[b] for b in x.batch_ids if b in names]
        out.append(o)
    return out


@router.get("/links", response_model=list[LinkOut])
async def list_links(t: Tenant):
    await service.expire_stale(t.db)
    links = list(
        (
            await t.db.execute(
                select(AdmissionLink).where(AdmissionLink.deleted_at.is_(None)).order_by(AdmissionLink.created_at.desc())
            )
        ).scalars()
    )
    return await _links_out(t, links)


@router.post("/links", response_model=LinkOut, status_code=201)
async def create_link(body: LinkIn, t: Tenant):
    link = await service.create_link(t, body)
    return (await _links_out(t, [link]))[0]


async def _get_link(t: Tenant, link_id: uuid.UUID) -> AdmissionLink:
    link = await t.db.get(AdmissionLink, link_id)
    if not link or link.deleted_at:
        raise not_found("Link")
    return link


@router.patch("/links/{link_id}", response_model=LinkOut)
async def update_link(link_id: uuid.UUID, body: LinkUpdate, t: Tenant):
    t.require("batch.manage")
    link = await _get_link(t, link_id)
    data = body.model_dump(exclude_unset=True)
    if "batch_ids" in data and data["batch_ids"] is not None:
        data["batch_ids"] = await service.check_link_batches(t.db, data["batch_ids"])
    for k, v in data.items():
        if v is not None or k in ("note", "offer_kind", "offer_value", "offer_until"):
            setattr(link, k, v)
    if not link.offer_kind:
        link.offer_value = None
    elif not link.offer_value:
        raise AppError("offer_value_required", "Enter the offer amount", "ছাড়ের পরিমাণ লিখুন", 422, "offer_value")
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="admission_link.updated",
        entity="admission_link", entity_id=link.id, after={k: str(v) for k, v in data.items()}, ip=t.ip,
    )
    await t.db.flush()
    return (await _links_out(t, [link]))[0]


@router.delete("/links/{link_id}", status_code=204)
async def delete_link(link_id: uuid.UUID, t: Tenant):
    """Printed leaflets with this QR will show "admission closed"; requests already sent stay."""
    t.require("batch.manage")
    link = await _get_link(t, link_id)
    link.deleted_at, link.is_active = now_utc(), False
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="admission_link.deleted",
        entity="admission_link", entity_id=link.id, ip=t.ip,
    )


# ---------------------------------------------------------------- requests (teacher side)


@router.get("/requests", response_model=list[RequestOut])
async def list_requests(t: Tenant, status: str = Query(default="pending", pattern="^(pending|admitted|closed|done|all)$")):
    """Newest first. closed = rejected, expired or withdrawn; done = everything handled."""
    await service.expire_stale(t.db)
    groups = {
        "pending": ("pending",),
        "admitted": ("admitted",),
        "closed": ("rejected", "expired", "cancelled"),
        "done": ("admitted", "rejected", "expired", "cancelled"),
    }
    q = select(AdmissionRequest)
    if status in groups:
        q = q.where(AdmissionRequest.status.in_(groups[status]))
    q = q.order_by(AdmissionRequest.created_at.desc()).limit(100)
    rows = list((await t.db.execute(q)).scalars())
    return [await service.request_out(t.db, r) for r in rows]


@router.post("/requests/{request_id}/admit", response_model=AdmittedOut)
async def admit_request(request_id: uuid.UUID, body: AdmitIn, t: Tenant, idempotency_key: str = IdemKey):
    r = await service.get_request(t.db, request_id)
    if r.status == "admitted" and r.student_id:
        return await service._admitted_out(t, await academics.get_student(t.db, r.student_id), r.batch_id, None)
    if r.status not in ("pending", "expired"):
        raise AppError("request_closed", "This request is already handled", "এই আবেদনের কাজ আগেই হয়ে গেছে", 409)
    student = StudentIn(
        name=r.student_name, phone=r.student_phone, guardian_name=r.guardian_name, guardian_phone=r.guardian_phone,
        school=r.school, class_level=r.class_level, force=body.force,
    )
    return await service.admit(
        t, batch_id=r.batch_id, student=student, fee=body, idempotency_key=idempotency_key,
        offer=(r.offer_discount_admission, r.offer_discount_fee), request=r, force=body.force,
    )


@router.post("/requests/{request_id}/reject", response_model=RequestOut)
async def reject_request(request_id: uuid.UUID, body: RejectIn, t: Tenant):
    r = await service.reject(t, request_id, body.reason)
    return await service.request_out(t.db, r)


# ---------------------------------------------------------------- in the office


@router.post("", response_model=AdmittedOut, status_code=201)
async def office_admission(body: OfficeAdmissionIn, t: Tenant, idempotency_key: str = IdemKey):
    """নতুন ভর্তি: student and guardian at the desk. Batch, two fields, fee card, done."""
    student = StudentIn(
        name=body.name, phone=body.phone, guardian_name=body.guardian_name, guardian_relation=body.guardian_relation,
        guardian_phone=body.guardian_phone,
        school=body.school, class_level=body.class_level, force=body.force,
    )
    return await service.admit(
        t, batch_id=body.batch_id, student=student, fee=body, idempotency_key=idempotency_key, force=body.force
    )


@router.get("/guardian", response_model=GuardianLookupOut)
async def guardian_lookup(t: Tenant, phone: str = Query(min_length=6, max_length=20)):
    """Typing a known guardian number offers to fill their name: siblings join in seconds."""
    name, school, kids = await service.guardian_children(t.db, normalize_bd_phone(phone))
    names = academics.batch_names_subquery()
    batch_names = dict(
        (await t.db.execute(select(names.c.student_id, names.c.batch_names).where(names.c.student_id.in_([k.id for k in kids])))).all()
    ) if kids else {}
    return GuardianLookupOut(
        guardian_name=name, school=school,
        children=[
            SiblingOut(id=k.id, name=k.name, student_code=k.student_code, class_level=k.class_level, batch_names=batch_names.get(k.id) or [])
            for k in kids
        ],
    )


# ---------------------------------------------------------------- public QR page


async def _load_public(db, code: str) -> tuple[AdmissionLink, Institution]:
    found = await service.find_link(code)
    if not found:
        raise AppError("link_not_found", "This admission link doesn't exist", "এই ভর্তির লিংক পাওয়া যায়নি", 404)
    link_id, inst_id = found
    await set_context(db, institution_id=inst_id)
    link = await db.get(AdmissionLink, link_id)
    inst = await db.get(Institution, inst_id)
    if not link or not inst or inst.deleted_at:
        raise AppError("link_not_found", "This admission link doesn't exist", "এই ভর্তির লিংক পাওয়া যায়নি", 404)
    return link, inst


@public.get("/join/{code}", response_model=JoinPageOut)
async def join_page(code: str, db: DB, auth: MaybeAuth, count: bool = False):
    """The page a leaflet QR opens. No login needed to look; applying needs the phone OTP."""
    link, inst = await _load_public(db, code)
    if count:
        link.scan_count += 1
    await service.expire_stale(db)
    owner = await db.scalar(
        select(User.name)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.institution_id == inst.id, Membership.role == "owner")
        .limit(1)
    )
    batches = await _open_batches(db, link.batch_ids)
    order = {b: i for i, b in enumerate(link.batch_ids)}
    batches.sort(key=lambda b: order.get(b.id, 99))
    mine: list[RequestOut] = []
    if auth:
        rows = (
            await db.execute(
                select(AdmissionRequest)
                .where(AdmissionRequest.link_id == link.id, AdmissionRequest.user_id == auth.user.id)
                .order_by(AdmissionRequest.created_at.desc())
            )
        ).scalars()
        mine = [await service.request_out(db, r) for r in rows]
    offer = (
        OfferOut(kind=link.offer_kind, value=link.offer_value, on=link.offer_on, until=link.offer_until)
        if service.offer_active(link)
        else None
    )
    return JoinPageOut(
        code=link.code, title=link.title, note=link.note, institution_name=inst.name, teacher_name=owner,
        city=inst.city, is_open=link.is_active, offer=offer, hold_days=link.hold_days,
        bkash_number=inst.settings.get("bkash_number"), nagad_number=inst.settings.get("nagad_number"),
        batches=batches, my_requests=mine,
    )


@public.post("/join/{code}/apply", response_model=RequestOut, status_code=201)
async def join_apply(code: str, body: ApplyIn, auth: Auth):
    db = auth.db
    link, inst = await _load_public(db, code)
    r = await service.apply(db, inst=inst, link=link, user_id=auth.user.id, user_phone=auth.user.phone, data=body)
    # A brand-new number lands in the family dashboard from now on, not the teacher setup wizard.
    sess = auth.session
    if sess.portal is None:
        sess.active_institution_id = inst.id
        sess.portal = "guardian" if r.guardian_phone == auth.user.phone else "student"
    await db.flush()
    return await service.request_out(db, r, inst.name)


@public.get("/qr.svg")
async def qr_svg(text: str = Query(min_length=1, max_length=300), scale: int = Query(default=8, ge=1, le=40)):
    """QR image for an admission page or poster. The URL comes from the frontend, which knows its origin."""
    buf = io.BytesIO()  # a standalone SVG (with xmlns) so it works in <img>
    segno.make(text, error="m").save(buf, kind="svg", scale=scale, border=2, dark="#1E1B2E", light="#FFFFFF", xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=86400"})
