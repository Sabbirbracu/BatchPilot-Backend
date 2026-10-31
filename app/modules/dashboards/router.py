"""The four dashboards: teacher (owner), staff (assistant), guardian and student.

Live queries for now; a precomputed daily_stats table replaces the heavy parts of the
teacher dashboard when an institution's history grows (System Design §17)."""

import uuid
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.core.db import OpsSessionLocal, set_context
from app.core.deps import Auth, Family, FamilyContext, Tenant
from app.core.errors import forbidden
from app.core.timeutil import add_months, current_month, local_day_bounds_utc, now_utc, today_local
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.admissions import service as admissions
from app.modules.admissions.models import AdmissionRequest
from app.modules.admissions.schemas import RequestOut
from app.modules.attendance.models import AttendanceRecord, AttendanceSession
from app.modules.attendance.router import today_batches
from app.modules.attendance.schemas import TodayBatch
from app.modules.fees.models import FeeDue, Payment
from app.modules.fees.router import list_dues, my_cash
from app.modules.fees.schemas import CashSummary, DueStudentRow
from app.modules.identity.models import Institution, Membership, User
from app.modules.identity.service import family_links
from app.modules.messaging.models import OutboxMessage

router = APIRouter(prefix="/dashboard", tags=["dashboards"])

OPEN = ("due", "partial")


class BatchMoney(BaseModel):
    batch_id: uuid.UUID
    name: str
    collected: int
    due: int
    rate: float | None


class TopDue(BaseModel):
    student_id: uuid.UUID
    name: str
    student_code: str
    outstanding: int
    months: int


class Collector(BaseModel):
    user_id: uuid.UUID
    name: str | None
    amount: int
    count: int


class TodayClass(TodayBatch):
    absent_names: list[str]  # first few, for the "who hasn't come" avatars


class LiveMark(BaseModel):
    student_id: uuid.UUID
    name: str
    student_code: str
    batch_name: str
    status: str
    at: datetime


class AbsentStreak(BaseModel):
    student_id: uuid.UUID
    name: str
    days: int


class TeacherDashboard(BaseModel):
    date: date
    month: date
    today_collected: int
    today_count: int
    month_collected: int
    total_due: int
    due_students: int
    overdue_2m_students: int
    month_rate: float | None
    last_month_rate: float | None
    absent_today: int
    active_students: int
    batches: list[BatchMoney]
    top_dues: list[TopDue]
    collectors_today: list[Collector]
    attendance_rate_today: float | None
    attendance_rate_last_week: float | None
    today_classes: list[TodayClass]
    live_marks: list[LiveMark]
    absent_streaks: list[AbsentStreak]


STREAK_MIN = 3  # consecutive absences before a student shows up under "needs attention"


def _rate(paid: int, net: int) -> float | None:
    return round(paid * 100 / net, 1) if net else None


@router.get("/teacher", response_model=TeacherDashboard)
async def teacher_dashboard(t: Tenant):
    """Owner (স্যার) dashboard — blueprint F8."""
    t.require("reports.view_all")
    db = t.db
    today, cm = today_local(), current_month()
    d_start, d_end = local_day_bounds_utc(today)
    m_start = local_day_bounds_utc(cm)[0]

    today_sum, today_cnt = (
        await db.execute(
            select(
                func.coalesce(func.sum(Payment.amount), 0),
                func.count(func.distinct(Payment.student_id)).filter(Payment.kind == "payment"),
            ).where(Payment.received_at >= d_start, Payment.received_at < d_end)
        )
    ).one()
    month_sum = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.received_at >= m_start, Payment.received_at < d_end)
    )

    per_student = (
        select(
            FeeDue.student_id,
            func.sum(FeeDue.amount - FeeDue.discount - FeeDue.paid_amount).label("outstanding"),
            func.count(func.distinct(FeeDue.month)).label("months"),
        )
        .join(Student, Student.id == FeeDue.student_id)
        .where(FeeDue.status.in_(OPEN), FeeDue.month <= cm, Student.status == "active", Student.deleted_at.is_(None))
        .group_by(FeeDue.student_id)
        .subquery()
    )
    total_due, due_students, overdue_2m = (
        await db.execute(
            select(
                func.coalesce(func.sum(per_student.c.outstanding), 0),
                func.count(),
                func.count().filter(per_student.c.months >= 2),
            ).select_from(per_student)
        )
    ).one()

    async def month_rate(m: date) -> float | None:
        paid, net = (
            await db.execute(
                select(
                    func.coalesce(func.sum(FeeDue.paid_amount), 0),
                    func.coalesce(func.sum(FeeDue.amount - FeeDue.discount), 0),
                ).where(FeeDue.month == m, FeeDue.status != "waived")
            )
        ).one()
        return _rate(int(paid), int(net))

    batches = [
        BatchMoney(batch_id=bid, name=name, collected=int(c), due=int(d), rate=_rate(int(c), int(c) + int(d)))
        for bid, name, c, d in await db.execute(
            select(
                Batch.id,
                Batch.name,
                func.coalesce(func.sum(FeeDue.paid_amount), 0),
                func.coalesce(func.sum(FeeDue.amount - FeeDue.discount - FeeDue.paid_amount), 0),
            )
            .outerjoin(FeeDue, (FeeDue.batch_id == Batch.id) & (FeeDue.month == cm))
            .where(Batch.archived_at.is_(None), Batch.deleted_at.is_(None))
            .group_by(Batch.id, Batch.name)
            .order_by(Batch.name)
        )
    ]

    top = [
        TopDue(student_id=sid, name=name, student_code=code, outstanding=int(o), months=int(m))
        for sid, name, code, o, m in await db.execute(
            select(Student.id, Student.name, Student.student_code, per_student.c.outstanding, per_student.c.months)
            .join(per_student, per_student.c.student_id == Student.id)
            .order_by(per_student.c.outstanding.desc())
            .limit(10)
        )
    ]

    collectors = [
        Collector(user_id=uid, name=name, amount=int(a), count=int(n))
        for uid, name, a, n in await db.execute(
            select(User.id, User.name, func.sum(Payment.amount), func.count().filter(Payment.kind == "payment"))
            .join(User, User.id == Payment.received_by)
            .where(Payment.received_at >= d_start, Payment.received_at < d_end)
            .group_by(User.id, User.name)
            .order_by(func.sum(Payment.amount).desc())
        )
    ]

    absent = await db.scalar(
        select(func.count()).where(AttendanceRecord.date == today, AttendanceRecord.status == "absent")
    )
    active = await db.scalar(
        select(func.count()).select_from(Student).where(Student.status == "active", Student.deleted_at.is_(None))
    )

    async def attendance_rate(start: date, end: date) -> float | None:
        present, total = (
            await db.execute(
                select(func.count().filter(AttendanceRecord.status != "absent"), func.count()).where(
                    AttendanceRecord.date >= start, AttendanceRecord.date <= end
                )
            )
        ).one()
        return _rate(int(present), int(total))

    absent_names: dict[uuid.UUID, list[str]] = {}
    for bid, name in await db.execute(
        select(AttendanceSession.batch_id, Student.name)
        .join(AttendanceRecord, AttendanceRecord.session_id == AttendanceSession.id)
        .join(Student, Student.id == AttendanceRecord.student_id)
        .where(AttendanceSession.date == today, AttendanceRecord.status == "absent")
        .order_by(Student.student_code)
    ):
        absent_names.setdefault(bid, []).append(name)
    today_classes = [
        TodayClass(**b.model_dump(), absent_names=absent_names.get(b.batch_id, [])[:3])
        for b in await today_batches(t, day=today)
        if b.scheduled_today
    ]

    marked_at = func.coalesce(AttendanceRecord.scanned_at, AttendanceRecord.created_at)
    live = [
        LiveMark(student_id=sid, name=name, student_code=code, batch_name=bname, status=st, at=at)
        for sid, name, code, bname, st, at in await db.execute(
            select(Student.id, Student.name, Student.student_code, Batch.name, AttendanceRecord.status, marked_at)
            .join(Student, Student.id == AttendanceRecord.student_id)
            .join(AttendanceSession, AttendanceSession.id == AttendanceRecord.session_id)
            .join(Batch, Batch.id == AttendanceSession.batch_id)
            .where(AttendanceRecord.date == today)
            # Absences and late arrivals first: those are what the owner acts on.
            .order_by((AttendanceRecord.status == "present").asc(), marked_at.desc(), Student.student_code)
            .limit(6)
        )
    ]

    # Consecutive absences counted back from each student's latest record (last 3 weeks).
    streaks: dict[uuid.UUID, list] = {}
    for sid, name, st in await db.execute(
        select(Student.id, Student.name, AttendanceRecord.status)
        .join(Student, Student.id == AttendanceRecord.student_id)
        .where(AttendanceRecord.date > today - timedelta(days=21), Student.status == "active")
        .order_by(Student.id, AttendanceRecord.date.desc())
    ):
        row = streaks.setdefault(sid, [name, 0, True])
        if row[2] and st == "absent":
            row[1] += 1
        else:
            row[2] = False
    absent_streaks = sorted(
        (AbsentStreak(student_id=sid, name=n, days=c) for sid, (n, c, _) in streaks.items() if c >= STREAK_MIN),
        key=lambda a: -a.days,
    )[:10]

    return TeacherDashboard(
        date=today,
        month=cm,
        today_collected=int(today_sum),
        today_count=int(today_cnt),
        month_collected=int(month_sum or 0),
        total_due=int(total_due),
        due_students=int(due_students),
        overdue_2m_students=int(overdue_2m),
        month_rate=await month_rate(cm),
        last_month_rate=await month_rate(add_months(cm, -1)),
        absent_today=int(absent or 0),
        active_students=int(active or 0),
        batches=batches,
        top_dues=top,
        collectors_today=collectors,
        attendance_rate_today=await attendance_rate(today, today),
        attendance_rate_last_week=await attendance_rate(today - timedelta(days=7), today - timedelta(days=1)),
        today_classes=today_classes,
        live_marks=live,
        absent_streaks=absent_streaks,
    )


# ---------------------------------------------------------------- staff (assistant)


class StaffDashboard(BaseModel):
    date: date
    cash: CashSummary
    today_batches: list[TodayBatch]
    call_list: list[DueStudentRow]


@router.get("/staff", response_model=StaffDashboard)
async def staff_dashboard(t: Tenant):
    """Assistant home: my cash today, today's batches, and who to call (2+ months due)."""
    if t.role == "owner":
        raise forbidden()
    batches = await today_batches(t, day=None)
    dues = await list_dues(t, batch_id=None, q=None, min_months=2, limit=10, offset=0)
    return StaffDashboard(
        date=today_local(),
        cash=await my_cash(t, day=None),
        today_batches=[b for b in batches if b.scheduled_today],
        call_list=dues.items,
    )


# ---------------------------------------------------------------- guardian & student


class ChildDue(BaseModel):
    month: date
    kind: str = "monthly"
    label: str | None = None
    batch_name: str
    outstanding: int
    status: str


class ChildAttendance(BaseModel):
    date: date
    batch_name: str
    status: str


class ChildPayment(BaseModel):
    received_at: datetime
    amount: int
    method: str
    reversed: bool


class ChildBatch(BaseModel):
    name: str
    days: list[str]
    start_time: time | None
    end_time: time | None
    location: str | None


class ChildNotice(BaseModel):
    body: str
    created_at: datetime


class ChildSummary(BaseModel):
    student_id: uuid.UUID
    name: str
    student_code: str
    class_level: str | None = None
    batch_names: list[str]
    batches: list[ChildBatch] = []
    notices: list[ChildNotice] = []
    outstanding: int
    open_dues: list[ChildDue]
    attendance_rate: float | None
    present: int
    absent: int
    recent_attendance: list[ChildAttendance]
    recent_payments: list[ChildPayment]


class FamilyDashboard(BaseModel):
    institution_name: str
    bkash_number: str | None
    children: list[ChildSummary]


async def _child_summary(db, student: Student) -> ChildSummary:
    cm = current_month()
    batches = list(
        (
            await db.execute(
                select(Batch)
                .join(Enrollment, Enrollment.batch_id == Batch.id)
                .where(Enrollment.student_id == student.id, Enrollment.end_month.is_(None), Enrollment.deleted_at.is_(None))
                .order_by(Batch.start_time.nulls_last())
            )
        ).scalars()
    )
    batch_names = [b.name for b in batches]
    notices = [
        ChildNotice(body=body, created_at=at)
        for body, at in await db.execute(
            select(OutboxMessage.body, OutboxMessage.created_at)
            .where(
                OutboxMessage.student_id == student.id,
                OutboxMessage.template_key == "notice",
                OutboxMessage.status != "cancelled",
            )
            .order_by(OutboxMessage.created_at.desc())
            .limit(5)
        )
    ]
    dues = [
        ChildDue(
            month=d.month, kind=d.kind, label=d.label, batch_name=bname,
            outstanding=d.amount - d.discount - d.paid_amount, status=d.status,
        )
        for d, bname in await db.execute(
            select(FeeDue, Batch.name)
            .join(Batch, Batch.id == FeeDue.batch_id)
            .where(FeeDue.student_id == student.id, FeeDue.status.in_(OPEN), FeeDue.month <= cm)
            .order_by(FeeDue.month)
        )
    ]
    counts = dict(
        (
            await db.execute(
                select(AttendanceRecord.status, func.count())
                .where(AttendanceRecord.student_id == student.id)
                .group_by(AttendanceRecord.status)
            )
        ).all()
    )
    present = counts.get("present", 0) + counts.get("late", 0)
    absent = counts.get("absent", 0)
    recent = [
        ChildAttendance(date=d, batch_name=n, status=st)
        for d, n, st in await db.execute(
            select(AttendanceRecord.date, Batch.name, AttendanceRecord.status)
            .join(AttendanceSession, AttendanceSession.id == AttendanceRecord.session_id)
            .join(Batch, Batch.id == AttendanceSession.batch_id)
            .where(AttendanceRecord.student_id == student.id)
            .order_by(AttendanceRecord.date.desc())
            .limit(10)
        )
    ]
    Rev = aliased(Payment)
    payments = [
        ChildPayment(received_at=p.received_at, amount=p.amount, method=p.method, reversed=rev is not None)
        for p, rev in await db.execute(
            select(Payment, Rev.id)
            .outerjoin(Rev, Rev.reverses_payment_id == Payment.id)
            .where(Payment.student_id == student.id, Payment.kind == "payment")
            .order_by(Payment.received_at.desc())
            .limit(5)
        )
    ]
    return ChildSummary(
        student_id=student.id,
        name=student.name,
        student_code=student.student_code,
        class_level=student.class_level,
        batch_names=batch_names,
        batches=[
            ChildBatch(name=b.name, days=b.days, start_time=b.start_time, end_time=b.end_time, location=b.location)
            for b in batches
        ],
        notices=notices,
        outstanding=sum(d.outstanding for d in dues),
        open_dues=dues,
        attendance_rate=round(present * 100 / (present + absent), 1) if present + absent else None,
        present=present,
        absent=absent,
        recent_attendance=recent,
        recent_payments=payments,
    )


async def _family_dashboard(f: FamilyContext) -> FamilyDashboard:
    return FamilyDashboard(
        institution_name=f.institution.name,
        bkash_number=f.institution.settings.get("bkash_number"),
        children=[await _child_summary(f.db, s) for s in f.students],
    )


@router.get("/guardian", response_model=FamilyDashboard)
async def guardian_dashboard(f: Family):
    """Blueprint F15 (Phase 2): a guardian sees only their own children."""
    if f.kind != "guardian":
        raise forbidden()
    return await _family_dashboard(f)


@router.get("/student", response_model=FamilyDashboard)
async def student_dashboard(f: Family):
    if f.kind != "student":
        raise forbidden()
    return await _family_dashboard(f)


class FamilyPlace(BaseModel):
    """One teacher (institution) the family is connected to."""

    institution_id: uuid.UUID
    institution_name: str
    teacher_name: str | None
    bkash_number: str | None
    children: list[ChildSummary]
    requests: list[RequestOut]


class FamilyOverview(BaseModel):
    places: list[FamilyPlace]


async def _request_places(user_id: uuid.UUID, phone: str) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Join requests sent by this login or for this guardian number, in any institution (ids only)."""
    since = now_utc() - timedelta(days=60)
    async with OpsSessionLocal() as ops:
        rows = (
            await ops.execute(
                select(AdmissionRequest.institution_id, AdmissionRequest.id)
                .where(
                    (AdmissionRequest.user_id == user_id) | (AdmissionRequest.guardian_phone == phone),
                    AdmissionRequest.status != "admitted",
                    AdmissionRequest.created_at > since,
                )
                .order_by(AdmissionRequest.created_at.desc())
            )
        ).all()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for inst, rid in rows:
        out.setdefault(inst, []).append(rid)
    return out


@router.get("/family", response_model=FamilyOverview)
async def family_dashboard(auth: Auth):
    """Student and guardian share one view: every child linked to this phone, at every teacher,
    plus join requests still waiting. Each institution is read under its own RLS context."""
    db, phone = auth.db, auth.user.phone
    students: dict[uuid.UUID, list[uuid.UUID]] = {}
    for inst_id, sid, _ in await family_links(phone):
        students.setdefault(inst_id, []).append(sid)
    requests = await _request_places(auth.user.id, phone)
    places = []
    for inst_id in list(dict.fromkeys([*students, *requests])):
        await set_context(db, institution_id=inst_id)
        inst = await db.get(Institution, inst_id)
        if not inst or inst.deleted_at:
            continue
        owner = await db.scalar(
            select(User.name)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.institution_id == inst_id, Membership.role == "owner")
            .limit(1)
        )
        await admissions.expire_stale(db)
        kids = [await db.get(Student, sid) for sid in dict.fromkeys(students.get(inst_id, []))]
        reqs = [await db.get(AdmissionRequest, rid) for rid in requests.get(inst_id, [])]
        places.append(
            FamilyPlace(
                institution_id=inst_id,
                institution_name=inst.name,
                teacher_name=owner,
                bkash_number=inst.settings.get("bkash_number"),
                children=[await _child_summary(db, k) for k in kids if k],
                requests=[await admissions.request_out(db, r, inst.name) for r in reqs if r],
            )
        )
    return FamilyOverview(places=places)
