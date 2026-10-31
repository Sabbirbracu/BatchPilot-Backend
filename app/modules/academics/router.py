import uuid

from fastapi import APIRouter, Query
from sqlalchemy import Integer, and_, func, select

from app.core.deps import Tenant
from app.core.errors import AppError
from app.core.phone import normalize_optional
from app.core.timeutil import add_months, current_month, now_utc, today_local
from app.modules.academics import service
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.academics.schemas import (
    BatchIn,
    BatchMemberOut,
    BatchOut,
    BatchUpdate,
    EnrollmentIn,
    EnrollmentOut,
    LeaveIn,
    StudentIn,
    StudentOut,
    StudentPage,
    StudentSummary,
    StudentUpdate,
)
from app.modules.attendance.models import AttendanceRecord, AttendanceSession
from app.modules.audit import service as audit
from app.modules.fees.models import FeeDue

router = APIRouter(tags=["academics"])

BATCH_FIELDS = [
    "name", "subject", "class_level", "days", "start_time", "end_time", "monthly_fee", "admission_fee", "capacity",
    "fee_plan", "course_fee", "installments", "location", "starts_on",
]
NULLABLE_BATCH_FIELDS = ("subject", "class_level", "start_time", "end_time", "admission_fee", "capacity", "course_fee", "location", "starts_on")


async def _batch_stats(t: Tenant, batch_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    if not batch_ids:
        return {}
    db, cm, today = t.db, current_month(), today_local()
    stats: dict[uuid.UUID, dict] = {b: {} for b in batch_ids}

    counts = await db.execute(
        select(Enrollment.batch_id, func.count())
        .join(Student, Student.id == Enrollment.student_id)
        .where(
            Enrollment.batch_id.in_(batch_ids),
            Enrollment.end_month.is_(None),
            Enrollment.deleted_at.is_(None),
            Student.status == "active",
            Student.deleted_at.is_(None),
        )
        .group_by(Enrollment.batch_id)
    )
    for bid, n in counts:
        stats[bid]["student_count"] = n

    money = await db.execute(
        select(
            FeeDue.batch_id,
            func.coalesce(func.sum(FeeDue.paid_amount), 0),
            func.coalesce(func.sum(FeeDue.amount - FeeDue.discount - FeeDue.paid_amount), 0),
            func.count(func.distinct(FeeDue.student_id)).filter(FeeDue.amount - FeeDue.discount - FeeDue.paid_amount > 0),
        )
        .where(FeeDue.batch_id.in_(batch_ids), FeeDue.month == cm)
        .group_by(FeeDue.batch_id)
    )
    for bid, collected, due, owing in money:
        stats[bid]["month_collected"] = int(collected)
        stats[bid]["month_due"] = int(due)
        stats[bid]["due_students"] = int(owing)

    for key, month in (("month_attendance", cm), ("last_month_attendance", add_months(cm, -1))):
        rates = await db.execute(
            select(AttendanceSession.batch_id, _rate())
            .join(AttendanceRecord, AttendanceRecord.session_id == AttendanceSession.id)
            .where(
                AttendanceSession.batch_id.in_(batch_ids),
                AttendanceSession.date >= month,
                AttendanceSession.date < add_months(month, 1),
            )
            .group_by(AttendanceSession.batch_id)
        )
        for bid, rate in rates:
            stats[bid][key] = rate

    att = await db.execute(
        select(
            AttendanceSession.batch_id,
            func.count().filter(AttendanceRecord.status != "absent"),
            func.count().filter(AttendanceRecord.status == "absent"),
        )
        .join(AttendanceRecord, AttendanceRecord.session_id == AttendanceSession.id)
        .where(AttendanceSession.batch_id.in_(batch_ids), AttendanceSession.date == today)
        .group_by(AttendanceSession.batch_id)
    )
    for bid, present, absent in att:
        stats[bid]["today_present"] = present
        stats[bid]["today_absent"] = absent
    return stats


def _rate():
    """% of attendance records that are not absences, rounded."""
    return func.round(100.0 * func.count().filter(AttendanceRecord.status != "absent") / func.count()).cast(Integer)


def _batch_out(b: Batch, stats: dict) -> BatchOut:
    out = BatchOut.model_validate(b)
    for k, v in stats.items():
        setattr(out, k, v)
    return out


@router.get("/batches", response_model=list[BatchOut])
async def list_batches(t: Tenant, archived: bool = False):
    q = select(Batch).where(Batch.deleted_at.is_(None)).order_by(Batch.start_time.nulls_last(), Batch.name)
    q = q.where(Batch.archived_at.is_not(None) if archived else Batch.archived_at.is_(None))
    batches = list((await t.db.execute(q)).scalars())
    stats = await _batch_stats(t, [b.id for b in batches])
    return [_batch_out(b, stats.get(b.id, {})) for b in batches]


@router.post("/batches", response_model=BatchOut, status_code=201)
async def create_batch(body: BatchIn, t: Tenant):
    t.require("batch.manage")
    b = Batch(institution_id=t.institution_id, **body.model_dump())
    t.db.add(b)
    await t.db.flush()
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="batch.created",
        entity="batch", entity_id=b.id, after=audit.snapshot(b, BATCH_FIELDS), ip=t.ip,
    )
    return _batch_out(b, {})


@router.get("/batches/{batch_id}", response_model=BatchOut)
async def get_batch(batch_id: uuid.UUID, t: Tenant):
    b = await service.get_batch(t.db, batch_id)
    stats = await _batch_stats(t, [b.id])
    return _batch_out(b, stats.get(b.id, {}))


@router.get("/batches/{batch_id}/members", response_model=list[BatchMemberOut])
async def batch_members(batch_id: uuid.UUID, t: Tenant):
    """Active students of a batch, in student ID order, with this month's fee and attendance in this batch."""
    db, cm = t.db, current_month()
    await service.get_batch(db, batch_id)
    dues = service.outstanding_subquery()
    month_due = (
        select(
            FeeDue.student_id.label("student_id"),
            func.sum(FeeDue.amount - FeeDue.discount).label("amount"),
            func.sum(FeeDue.paid_amount).label("paid"),
            func.max(FeeDue.status).label("status"),
        )
        .where(FeeDue.batch_id == batch_id, FeeDue.month == cm)
        .group_by(FeeDue.student_id)
        .subquery()
    )
    att = (
        select(AttendanceRecord.student_id.label("student_id"), _rate().label("rate"))
        .join(AttendanceSession, AttendanceSession.id == AttendanceRecord.session_id)
        .where(
            AttendanceSession.batch_id == batch_id,
            AttendanceSession.date >= cm,
            AttendanceSession.date < add_months(cm, 1),
        )
        .group_by(AttendanceRecord.student_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Student, month_due.c.amount, month_due.c.paid, month_due.c.status, dues.c.outstanding, att.c.rate)
            .join(Enrollment, Enrollment.student_id == Student.id)
            .outerjoin(month_due, month_due.c.student_id == Student.id)
            .outerjoin(dues, dues.c.student_id == Student.id)
            .outerjoin(att, att.c.student_id == Student.id)
            .where(
                Enrollment.batch_id == batch_id,
                Enrollment.end_month.is_(None),
                Enrollment.deleted_at.is_(None),
                Student.status == "active",
                Student.deleted_at.is_(None),
            )
            .order_by(func.length(Student.student_code), Student.student_code)
        )
    ).all()
    return [
        BatchMemberOut(
            student_id=s.id, student_code=s.student_code, name=s.name, guardian_phone=s.guardian_phone,
            month_amount=int(amount or 0), month_paid=int(paid or 0), month_status=status,
            outstanding=int(outstanding or 0), attendance=rate,
        )
        for s, amount, paid, status, outstanding, rate in rows
    ]


@router.patch("/batches/{batch_id}", response_model=BatchOut)
async def update_batch(batch_id: uuid.UUID, body: BatchUpdate, t: Tenant):
    t.require("batch.manage")
    b = await service.get_batch(t.db, batch_id)
    if body.version is not None and body.version != b.version:
        raise AppError("stale_version", "Someone else changed this batch. Reload and try again.",
                       "অন্য কেউ এই ব্যাচ বদলেছে। রিলোড করে আবার চেষ্টা করুন।", 409)
    before = audit.snapshot(b, BATCH_FIELDS)
    for k, v in body.model_dump(exclude_unset=True, exclude={"version"}).items():
        if v is not None or k in NULLABLE_BATCH_FIELDS:
            setattr(b, k, v)
    if b.fee_plan == "course" and not b.course_fee:
        raise AppError("course_fee_required", "Enter the course fee", "কোর্স ফি লিখুন", 422, "course_fee")
    b.version += 1
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="batch.updated",
        entity="batch", entity_id=b.id, before=before, after=audit.snapshot(b, BATCH_FIELDS), ip=t.ip,
    )
    await t.db.flush()
    stats = await _batch_stats(t, [b.id])
    return _batch_out(b, stats.get(b.id, {}))


@router.post("/batches/{batch_id}/archive", response_model=BatchOut)
async def archive_batch(batch_id: uuid.UUID, t: Tenant, restore: bool = False):
    """Closing a batch keeps all its data; it just stops new fees."""
    t.require("batch.manage")
    b = await service.get_batch(t.db, batch_id)
    b.archived_at = None if restore else now_utc()
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id,
        action="batch.restored" if restore else "batch.archived", entity="batch", entity_id=b.id, ip=t.ip,
    )
    await t.db.flush()
    return _batch_out(b, {})


# ---------------------------------------------------------------- students


@router.get("/students", response_model=StudentPage)
async def list_students(
    t: Tenant,
    q: str | None = None,
    batch_id: uuid.UUID | None = None,
    status: str | None = Query(default="active", pattern="^(active|left|all)$"),
    has_due: bool | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    items, next_cursor, total = await service.search_students(
        t.db, q=q, batch_id=batch_id, status=None if status == "all" else status,
        has_due=has_due, cursor=cursor, limit=limit,
    )
    return StudentPage(items=items, next_cursor=str(next_cursor) if next_cursor else None, total=total)


@router.post("/students", response_model=StudentOut, status_code=201)
async def create_student(body: StudentIn, t: Tenant):
    st = await service.create_student(t, body)
    return await _student_full(t, st.id)


async def _student_full(t: Tenant, student_id: uuid.UUID) -> StudentOut:
    q, _ = service.student_query()
    row = (await t.db.execute(q.where(and_(Student.id == student_id, Student.deleted_at.is_(None))))).first()
    if not row:
        await service.get_student(t.db, student_id)  # raises not_found
    return service.student_out(*row)


@router.get("/students/summary", response_model=StudentSummary)
async def student_summary(t: Tenant):
    """Counts for the students page header tiles."""
    cm = current_month()
    dues = service.outstanding_subquery()
    active = Student.status == "active"
    row = (
        await t.db.execute(
            select(
                func.count().filter(active),
                func.count().filter(active, Student.joined_at >= cm),
                func.count().filter(active, dues.c.outstanding > 0),
                func.count().filter(Student.status == "left"),
            )
            .select_from(Student)
            .outerjoin(dues, dues.c.student_id == Student.id)
            .where(Student.deleted_at.is_(None))
        )
    ).one()
    return StudentSummary(active=row[0], joined_this_month=row[1], with_due=row[2], left=row[3])


@router.get("/students/{student_id}", response_model=StudentOut)
async def get_student(student_id: uuid.UUID, t: Tenant):
    return await _student_full(t, student_id)


@router.patch("/students/{student_id}", response_model=StudentOut)
async def update_student(student_id: uuid.UUID, body: StudentUpdate, t: Tenant):
    t.require("student.manage")
    st = await service.get_student(t.db, student_id)
    if body.version is not None and body.version != st.version:
        raise AppError("stale_version", "Someone else changed this student. Reload and try again.",
                       "অন্য কেউ এই স্টুডেন্টের তথ্য বদলেছে। রিলোড করে আবার চেষ্টা করুন।", 409)
    before = audit.snapshot(st, service.STUDENT_FIELDS)
    data = body.model_dump(exclude_unset=True, exclude={"version"})
    for k in ("phone", "guardian_phone"):
        if k in data:
            new = normalize_optional(data[k], k)
            if k == "guardian_phone" and new != st.guardian_phone:
                st.guardian_verified_at = None
            data[k] = new
    for k, v in data.items():
        if k == "name" and not v:
            continue
        setattr(st, k, v)
    st.version += 1
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="student.updated",
        entity="student", entity_id=st.id, before=before, after=audit.snapshot(st, service.STUDENT_FIELDS), ip=t.ip,
    )
    await t.db.flush()
    return await _student_full(t, st.id)


@router.post("/students/{student_id}/leave", response_model=StudentOut)
async def student_leave(student_id: uuid.UUID, body: LeaveIn, t: Tenant):
    """Student left: no new fees from next month; old dues stay."""
    t.require("student.manage")
    st = await service.get_student(t.db, student_id)
    st.status, st.left_at = "left", today_local()
    ended = await service.end_enrollments(t.db, st.id)
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="student.left",
        entity="student", entity_id=st.id, after={"reason": body.reason, "enrollments_ended": ended}, ip=t.ip,
    )
    await t.db.flush()
    return await _student_full(t, st.id)


@router.post("/students/{student_id}/rejoin", response_model=StudentOut)
async def student_rejoin(student_id: uuid.UUID, t: Tenant):
    t.require("student.manage")
    st = await service.get_student(t.db, student_id)
    st.status, st.left_at = "active", None
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="student.rejoined",
        entity="student", entity_id=st.id, ip=t.ip,
    )
    await t.db.flush()
    return await _student_full(t, st.id)


@router.get("/students/{student_id}/enrollments", response_model=list[EnrollmentOut])
async def list_enrollments(student_id: uuid.UUID, t: Tenant, include_ended: bool = False):
    await service.get_student(t.db, student_id)
    return await service.enrollments_out(t.db, student_id, include_ended)


@router.post("/students/{student_id}/enrollments", response_model=list[EnrollmentOut], status_code=201)
async def add_enrollment(student_id: uuid.UUID, body: EnrollmentIn, t: Tenant):
    t.require("student.manage")
    service.check_fee_overrides(t, [body])
    st = await service.get_student(t.db, student_id)
    if st.status != "active":
        raise AppError("student_left", "This student has left", "এই স্টুডেন্ট ছেড়ে দিয়েছে", 409)
    enr = await service.enroll(t.db, t.institution, st, body)
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="enrollment.added",
        entity="enrollment", entity_id=enr.id,
        after={"student_id": st.id, "batch_id": body.batch_id, "custom_fee": body.custom_fee}, ip=t.ip,
    )
    await t.db.flush()
    return await service.enrollments_out(t.db, student_id)


@router.delete("/students/{student_id}/enrollments/{batch_id}", response_model=list[EnrollmentOut])
async def remove_enrollment(student_id: uuid.UUID, batch_id: uuid.UUID, t: Tenant):
    t.require("student.manage")
    await service.get_student(t.db, student_id)
    ended = await service.end_enrollments(t.db, student_id, batch_id)
    if not ended:
        raise AppError("not_enrolled", "Not in this batch", "এই ব্যাচে নেই", 404)
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="enrollment.ended",
        entity="enrollment", after={"student_id": student_id, "batch_id": batch_id}, ip=t.ip,
    )
    return await service.enrollments_out(t.db, student_id)
