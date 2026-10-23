import uuid
from datetime import date

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.core.deps import Tenant
from app.core.timeutil import today_local
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.academics.service import get_batch
from app.modules.attendance import service
from app.modules.attendance.models import AttendanceRecord, AttendanceSession
from app.modules.attendance.schemas import (
    RosterItem,
    RosterOut,
    StudentAttendanceOut,
    StudentAttendanceRow,
    SyncIn,
    SyncOut,
    TodayBatch,
)
from app.modules.identity.models import User

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.get("/today", response_model=list[TodayBatch])
async def today_batches(t: Tenant, day: date | None = None):
    d = day or today_local()
    code = service.weekday_code(d)
    batches = list(
        (
            await t.db.execute(
                select(Batch)
                .where(Batch.archived_at.is_(None), Batch.deleted_at.is_(None))
                .order_by(Batch.start_time.nulls_last(), Batch.name)
            )
        ).scalars()
    )
    counts = dict(
        (
            await t.db.execute(
                select(Enrollment.batch_id, func.count())
                .join(Student, Student.id == Enrollment.student_id)
                .where(Enrollment.end_month.is_(None), Enrollment.deleted_at.is_(None), Student.status == "active")
                .group_by(Enrollment.batch_id)
            )
        ).all()
    )
    taken = {
        bid: (p, a)
        for bid, p, a in await t.db.execute(
            select(
                AttendanceSession.batch_id,
                func.count().filter(AttendanceRecord.status != "absent"),
                func.count().filter(AttendanceRecord.status == "absent"),
            )
            .join(AttendanceRecord, AttendanceRecord.session_id == AttendanceSession.id, isouter=True)
            .where(AttendanceSession.date == d)
            .group_by(AttendanceSession.batch_id)
        )
    }
    out = [
        TodayBatch(
            batch_id=b.id,
            name=b.name,
            subject=b.subject,
            start_time=b.start_time.strftime("%H:%M") if b.start_time else None,
            end_time=b.end_time.strftime("%H:%M") if b.end_time else None,
            scheduled_today=code in (b.days or []),
            student_count=counts.get(b.id, 0),
            taken=b.id in taken,
            present=taken.get(b.id, (0, 0))[0],
            absent=taken.get(b.id, (0, 0))[1],
        )
        for b in batches
    ]
    # Today's scheduled batches first.
    out.sort(key=lambda x: (not x.scheduled_today,))
    return out


@router.get("/batches/{batch_id}", response_model=RosterOut)
async def roster(batch_id: uuid.UUID, t: Tenant, day: date | None = Query(default=None, alias="date")):
    d = day or today_local()
    batch = await get_batch(t.db, batch_id)
    students = await service.enrolled_students(t.db, batch.id)
    sess = (
        await t.db.execute(
            select(AttendanceSession).where(AttendanceSession.batch_id == batch.id, AttendanceSession.date == d)
        )
    ).scalar_one_or_none()
    stored: dict[uuid.UUID, str] = {}
    taken_by_name = None
    if sess:
        stored = {
            sid: st
            for sid, st in await t.db.execute(
                select(AttendanceRecord.student_id, AttendanceRecord.status).where(AttendanceRecord.session_id == sess.id)
            )
        }
        user = await t.db.get(User, sess.taken_by)
        taken_by_name = user.name if user else None
    dues = await service.due_months_by_student(t.db, [s.id for s in students])
    items = [
        RosterItem(
            student_id=s.id,
            name=s.name,
            roll_no=s.roll_no,
            status=stored.get(s.id, "present"),
            guardian_phone=s.guardian_phone,
            due_months=dues.get(s.id, 0),
        )
        for s in students
    ]
    return RosterOut(
        batch_id=batch.id,
        batch_name=batch.name,
        date=d,
        session_id=sess.id if sess else None,
        taken_by_name=taken_by_name,
        taken_at=sess.updated_at if sess else None,
        present=sum(1 for i in items if i.status != "absent"),
        absent=sum(1 for i in items if i.status == "absent"),
        items=items,
    )


@router.post("/sync", response_model=SyncOut)
async def sync(body: SyncIn, t: Tenant):
    session_id, present, absent, sms = await service.sync(t, body)
    return SyncOut(session_id=session_id, present=present, absent=absent, sms_queued=sms)


@router.get("/students/{student_id}", response_model=StudentAttendanceOut)
async def student_attendance(student_id: uuid.UUID, t: Tenant, limit: int = Query(default=30, le=200)):
    totals = dict(
        (
            await t.db.execute(
                select(AttendanceRecord.status, func.count())
                .where(AttendanceRecord.student_id == student_id)
                .group_by(AttendanceRecord.status)
            )
        ).all()
    )
    rows = (
        await t.db.execute(
            select(AttendanceRecord.date, Batch.name, AttendanceRecord.status)
            .join(AttendanceSession, AttendanceSession.id == AttendanceRecord.session_id)
            .join(Batch, Batch.id == AttendanceSession.batch_id)
            .where(AttendanceRecord.student_id == student_id)
            .order_by(AttendanceRecord.date.desc())
            .limit(limit)
        )
    ).all()
    present = totals.get("present", 0) + totals.get("late", 0)
    absent = totals.get("absent", 0)
    sessions = present + absent
    return StudentAttendanceOut(
        sessions=sessions,
        present=present,
        absent=absent,
        rate=round(present * 100 / sessions, 1) if sessions else None,
        recent=[StudentAttendanceRow(date=d, batch_name=n, status=s) for d, n, s in rows],
    )
