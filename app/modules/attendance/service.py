"""One-tap attendance: the roster defaults to present, the assistant taps absentees.

Sync is an idempotent upsert keyed by (batch, date) and (session, student), so a retry
from a phone on bad mobile data can never double-count or double-SMS."""

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import TenantContext
from app.core.errors import AppError
from app.core.ids import new_id
from app.core.money import BN_DIGITS, month_label
from app.core.timeutil import current_month, today_local
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.academics.service import get_batch
from app.modules.attendance.models import AttendanceRecord, AttendanceSession
from app.modules.attendance.schemas import SyncIn
from app.modules.fees.models import FeeDue
from app.modules.messaging import service as messaging

PY_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def weekday_code(d: date) -> str:
    return PY_WEEKDAY[d.weekday()]


async def enrolled_students(db: AsyncSession, batch_id: uuid.UUID) -> list[Student]:
    return list(
        (
            await db.execute(
                select(Student)
                .join(Enrollment, Enrollment.student_id == Student.id)
                .where(
                    Enrollment.batch_id == batch_id,
                    Enrollment.end_month.is_(None),
                    Enrollment.deleted_at.is_(None),
                    Student.status == "active",
                    Student.deleted_at.is_(None),
                )
                # Numeric order: "2" before "10".
                .order_by(func.length(Student.student_code), Student.student_code)
            )
        ).scalars()
    )


async def due_months_by_student(db: AsyncSession, student_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not student_ids:
        return {}
    rows = await db.execute(
        select(FeeDue.student_id, func.count(func.distinct(FeeDue.month)))
        .where(
            FeeDue.student_id.in_(student_ids),
            FeeDue.status.in_(("due", "partial")),
            FeeDue.month <= current_month(),
        )
        .group_by(FeeDue.student_id)
    )
    return {sid: n for sid, n in rows}


async def sync(t: TenantContext, data: SyncIn) -> tuple[uuid.UUID, int, int, int]:
    t.require("attendance.take")
    db, inst = t.db, t.institution
    batch: Batch = await get_batch(db, data.batch_id)
    today = today_local()
    if data.date > today:
        raise AppError("future_date", "Can't take attendance for a future date", "ভবিষ্যতের তারিখে হাজিরা নেওয়া যাবে না", 422, "date")
    if data.date < today - timedelta(days=30):
        raise AppError("too_old", "Attendance older than 30 days can't be changed", "৩০ দিনের পুরনো হাজিরা বদলানো যাবে না", 422, "date")

    roster = {s.id: s for s in await enrolled_students(db, batch.id)}
    unknown = [r.student_id for r in data.records if r.student_id not in roster]
    if unknown:
        raise AppError("not_in_batch", "Some students are not in this batch", "কিছু স্টুডেন্ট এই ব্যাচে নেই", 422, "records")

    session_id = (
        await db.execute(
            insert(AttendanceSession)
            .values(
                id=data.id or new_id(),
                institution_id=inst.id,
                batch_id=batch.id,
                date=data.date,
                method=data.method,
                taken_by=t.user.id,
            )
            .on_conflict_do_update(
                index_elements=["batch_id", "date"],
                set_={"taken_by": t.user.id, "method": data.method, "updated_at": func.now()},
            )
            .returning(AttendanceSession.id)
        )
    ).scalar_one()

    previous = {
        sid: st
        for sid, st in await db.execute(
            select(AttendanceRecord.student_id, AttendanceRecord.status).where(AttendanceRecord.session_id == session_id)
        )
    }
    if data.records:
        stmt = insert(AttendanceRecord).values(
            [
                dict(
                    id=new_id(),
                    institution_id=inst.id,
                    session_id=session_id,
                    student_id=r.student_id,
                    date=data.date,
                    status=r.status,
                )
                for r in data.records
            ]
        )
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["session_id", "student_id"],
                set_={"status": stmt.excluded.status, "updated_at": func.now()},
            )
        )

    sms_queued = 0
    send_sms = inst.settings.get("absent_sms", True) and data.date == today
    for r in data.records:
        key = f"absent:{r.student_id}:{data.date.isoformat()}"
        was = previous.get(r.student_id)
        if r.status == "absent" and was != "absent" and send_sms:
            st = roster[r.student_id]
            msg = await messaging.enqueue(
                db,
                institution_id=inst.id,
                template_key="absent",
                to_phone=st.guardian_phone,
                student_id=st.id,
                dedupe_key=key,
                values={
                    "student": st.name,
                    "date": f"{str(data.date.day).translate(BN_DIGITS)} {month_label(data.date)}",
                    "batch": batch.name,
                    "institution": inst.name,
                },
            )
            sms_queued += 1 if msg else 0
        elif r.status != "absent" and was == "absent":
            await messaging.cancel_pending(db, key)

    counts = dict(
        (
            await db.execute(
                select(AttendanceRecord.status, func.count())
                .where(AttendanceRecord.session_id == session_id)
                .group_by(AttendanceRecord.status)
            )
        ).all()
    )
    present = counts.get("present", 0) + counts.get("late", 0)
    return session_id, present, counts.get("absent", 0), sms_queued
