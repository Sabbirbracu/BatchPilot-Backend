import uuid

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import TenantContext
from app.core.errors import AppError, not_found
from app.core.phone import normalize_optional
from app.core.security import new_qr_token
from app.core.timeutil import add_months, current_month, today_local
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.academics.schemas import EnrollmentIn, EnrollmentOut, StudentIn, StudentOut
from app.modules.audit import service as audit
from app.modules.fees import service as fees
from app.modules.fees.models import FeeDue
from app.modules.identity.models import Institution
from app.modules.messaging import service as messaging

STUDENT_FIELDS = ["name", "phone", "guardian_name", "guardian_phone", "school", "class_level", "note", "status"]


async def get_batch(db: AsyncSession, batch_id: uuid.UUID) -> Batch:
    b = await db.get(Batch, batch_id)
    if not b or b.deleted_at:
        raise not_found("Batch")
    return b


async def get_student(db: AsyncSession, student_id: uuid.UUID) -> Student:
    s = await db.get(Student, student_id)
    if not s or s.deleted_at:
        raise not_found("Student")
    return s


async def _next_roll(db: AsyncSession, inst: Institution) -> str:
    while True:
        n = (
            await db.execute(
                update(Institution)
                .where(Institution.id == inst.id)
                .values(next_roll=Institution.next_roll + 1)
                .returning(Institution.next_roll - 1)
            )
        ).scalar_one()
        roll = str(n)
        taken = await db.scalar(
            select(func.count()).select_from(Student).where(Student.roll_no == roll, Student.deleted_at.is_(None))
        )
        if not taken:
            return roll


async def enroll(
    db: AsyncSession, inst: Institution, student: Student, data: EnrollmentIn
) -> Enrollment:
    batch = await get_batch(db, data.batch_id)
    if batch.archived_at:
        raise AppError("batch_archived", "This batch is closed", "এই ব্যাচ বন্ধ করা হয়েছে", 409, "batch_id")
    already = await db.scalar(
        select(Enrollment.id).where(
            Enrollment.student_id == student.id,
            Enrollment.batch_id == batch.id,
            Enrollment.end_month.is_(None),
            Enrollment.deleted_at.is_(None),
        )
    )
    if already:
        raise AppError("already_enrolled", "Already in this batch", "এই ব্যাচে আগেই আছে", 409, "batch_id")
    cm = current_month()
    start = (data.start_month or cm).replace(day=1)
    enr = Enrollment(
        institution_id=inst.id,
        student_id=student.id,
        batch_id=batch.id,
        custom_fee=data.custom_fee,
        discount_note=data.discount_note,
        start_month=start,
    )
    db.add(enr)
    await db.flush()
    # Dues from the start month up to now (a back-dated start creates arrears, capped at a year).
    month = max(start, add_months(cm, -11))
    while month <= cm:
        await fees.ensure_due(db, inst, enr, batch, month)
        month = add_months(month, 1)
    return enr


async def create_student(t: TenantContext, data: StudentIn) -> Student:
    t.require("student.manage")
    if any(e.custom_fee is not None for e in data.enrollments) and not t.can("due.discount"):
        raise AppError("forbidden", "Only the owner can set a special fee", "বিশেষ ফি শুধু মালিক দিতে পারবেন", 403, "custom_fee")
    db, inst = t.db, t.institution
    phone = normalize_optional(data.phone, "phone")
    guardian_phone = normalize_optional(data.guardian_phone, "guardian_phone")

    if not data.force:
        contact = [c for c in (phone, guardian_phone) if c]
        if contact:
            dup = (
                await db.execute(
                    select(Student).where(
                        Student.deleted_at.is_(None),
                        func.lower(Student.name) == data.name.strip().lower(),
                        or_(Student.guardian_phone.in_(contact), Student.phone.in_(contact)),
                    )
                )
            ).scalars().first()
            if dup:
                raise AppError(
                    "possible_duplicate",
                    f"A student with this name and phone already exists (roll {dup.roll_no})",
                    f"এই নাম ও নম্বরে একজন স্টুডেন্ট আগেই আছে (রোল {dup.roll_no})",
                    409,
                )

    roll = (data.roll_no or "").strip()
    if roll:
        taken = await db.scalar(select(Student.id).where(Student.roll_no == roll, Student.deleted_at.is_(None)))
        if taken:
            raise AppError("roll_taken", "This roll number is taken", "এই রোল নম্বর আগেই আছে", 409, "roll_no")
    else:
        roll = await _next_roll(db, inst)

    st = Student(
        institution_id=inst.id,
        roll_no=roll,
        name=data.name.strip(),
        phone=phone,
        guardian_name=(data.guardian_name or "").strip() or None,
        guardian_phone=guardian_phone,
        school=data.school,
        class_level=data.class_level,
        joined_at=data.joined_at or today_local(),
        qr_token=new_qr_token(),
        note=data.note,
    )
    db.add(st)
    await db.flush()
    for e in data.enrollments:
        await enroll(db, inst, st, e)

    if guardian_phone:
        await messaging.enqueue(
            db,
            institution_id=inst.id,
            template_key="welcome",
            to_phone=guardian_phone,
            student_id=st.id,
            dedupe_key=f"welcome:{st.id}",
            values={"student": st.name, "institution": inst.name},
        )
    await audit.record(
        db,
        institution_id=inst.id,
        actor_id=t.user.id,
        action="student.created",
        entity="student",
        entity_id=st.id,
        after={**audit.snapshot(st, STUDENT_FIELDS), "roll_no": roll, "batches": [e.batch_id for e in data.enrollments]},
        ip=t.ip,
    )
    await db.flush()
    return st


def outstanding_subquery():
    cm = current_month()
    return (
        select(
            FeeDue.student_id.label("student_id"),
            func.sum(FeeDue.amount - FeeDue.discount - FeeDue.paid_amount).label("outstanding"),
            func.count().label("due_months"),
        )
        .where(FeeDue.status.in_(fees.OPEN), FeeDue.month <= cm)
        .group_by(FeeDue.student_id)
        .subquery()
    )


def batch_names_subquery():
    return (
        select(
            Enrollment.student_id.label("student_id"),
            func.array_agg(Batch.name).label("batch_names"),
        )
        .join(Batch, Batch.id == Enrollment.batch_id)
        .where(Enrollment.end_month.is_(None), Enrollment.deleted_at.is_(None))
        .group_by(Enrollment.student_id)
        .subquery()
    )


def student_out(st: Student, outstanding: int | None, due_months: int | None, batch_names: list[str] | None) -> StudentOut:
    out = StudentOut.model_validate(st)
    out.outstanding = int(outstanding or 0)
    out.due_months = int(due_months or 0)
    out.batch_names = list(batch_names or [])
    return out


async def search_students(
    db: AsyncSession,
    *,
    q: str | None,
    batch_id: uuid.UUID | None,
    status: str | None,
    has_due: bool | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> tuple[list[StudentOut], uuid.UUID | None, int]:
    dues = outstanding_subquery()
    names = batch_names_subquery()
    conds = [Student.deleted_at.is_(None)]
    if status:
        conds.append(Student.status == status)
    if q and q.strip():
        term = q.strip()
        like = f"%{term}%"
        digits = "".join(ch for ch in term if ch.isdigit())
        ors = [Student.name.ilike(like), Student.roll_no == term, Student.guardian_name.ilike(like)]
        if len(digits) >= 4:
            ors += [Student.phone.like(f"%{digits[-10:]}%"), Student.guardian_phone.like(f"%{digits[-10:]}%")]
        conds.append(or_(*ors))
    if batch_id:
        conds.append(
            exists().where(
                Enrollment.student_id == Student.id,
                Enrollment.batch_id == batch_id,
                Enrollment.end_month.is_(None),
                Enrollment.deleted_at.is_(None),
            )
        )
    if has_due is True:
        conds.append(dues.c.outstanding > 0)
    elif has_due is False:
        conds.append(or_(dues.c.outstanding.is_(None), dues.c.outstanding <= 0))

    base = (
        select(Student, dues.c.outstanding, dues.c.due_months, names.c.batch_names)
        .outerjoin(dues, dues.c.student_id == Student.id)
        .outerjoin(names, names.c.student_id == Student.id)
        .where(and_(*conds))
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    page_q = base.order_by(Student.id).limit(limit + 1)
    if cursor:
        page_q = page_q.where(Student.id > cursor)
    rows = (await db.execute(page_q)).all()
    items = [student_out(s, o, m, n) for s, o, m, n in rows[:limit]]
    next_cursor = rows[limit - 1][0].id if len(rows) > limit else None
    return items, next_cursor, int(total or 0)


async def enrollments_out(db: AsyncSession, student_id: uuid.UUID, include_ended: bool = False) -> list[EnrollmentOut]:
    q = (
        select(Enrollment, Batch)
        .join(Batch, Batch.id == Enrollment.batch_id)
        .where(Enrollment.student_id == student_id, Enrollment.deleted_at.is_(None))
        .order_by(Enrollment.start_month)
    )
    if not include_ended:
        q = q.where(Enrollment.end_month.is_(None))
    return [
        EnrollmentOut(
            id=e.id,
            batch_id=b.id,
            batch_name=b.name,
            monthly_fee=e.custom_fee if e.custom_fee is not None else b.monthly_fee,
            custom_fee=e.custom_fee,
            discount_note=e.discount_note,
            start_month=e.start_month,
            end_month=e.end_month,
        )
        for e, b in (await db.execute(q)).all()
    ]


async def end_enrollments(db: AsyncSession, student_id: uuid.UUID, batch_id: uuid.UUID | None = None) -> int:
    """Stop future fees. The current month's due and older arrears stay."""
    conds = [Enrollment.student_id == student_id, Enrollment.end_month.is_(None), Enrollment.deleted_at.is_(None)]
    if batch_id:
        conds.append(Enrollment.batch_id == batch_id)
    res = await db.execute(update(Enrollment).where(*conds).values(end_month=current_month()).returning(Enrollment.id))
    return len(res.all())

