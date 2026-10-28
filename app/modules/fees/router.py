import uuid
from datetime import date

from fastapi import APIRouter, Header, Query
from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import aliased

from app.core.deps import Tenant
from app.core.errors import AppError, not_found
from app.core.timeutil import add_months, current_month, local_day_bounds_utc, parse_month, today_local
from app.modules.academics import service as academics
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.audit import service as audit
from app.modules.fees import service
from app.modules.fees.models import FeeDue, Payment, PaymentAllocation
from app.modules.fees.schemas import (
    AllocationOut,
    CashSummary,
    DiscountIn,
    DueOut,
    DuesPage,
    DueStudentRow,
    FeeOverview,
    GenerateIn,
    LedgerOut,
    PaymentIn,
    PaymentOut,
    ReceiptOut,
    RemindIn,
    ReverseIn,
)
from app.modules.identity.models import User
from app.modules.messaging.models import OutboxMessage

Rev = aliased(Payment)

router = APIRouter(prefix="/fees", tags=["fees"])


async def _student_outstanding(t: Tenant, student_id: uuid.UUID) -> int:
    return int(
        await t.db.scalar(
            select(func.coalesce(func.sum(FeeDue.amount - FeeDue.discount - FeeDue.paid_amount), 0)).where(
                FeeDue.student_id == student_id, FeeDue.status.in_(service.OPEN), FeeDue.month <= current_month()
            )
        )
    )


@router.post("/payments", response_model=ReceiptOut, status_code=201)
async def record_payment(
    body: PaymentIn,
    t: Tenant,
    idempotency_key: str = Header(min_length=8, max_length=80, alias="Idempotency-Key"),
):
    res = await service.record_payment(
        t,
        student_id=body.student_id,
        amount=body.amount,
        method=body.method,
        idempotency_key=idempotency_key,
        trx_id=body.trx_id,
        note=body.note,
        due_ids=body.due_ids,
        send_receipt=body.send_receipt,
    )
    p = res.payment
    st = await t.db.get(Student, p.student_id)
    receiver = await t.db.get(User, p.received_by)
    return ReceiptOut(
        payment_id=p.id,
        student_id=st.id,
        student_name=st.name,
        roll_no=st.roll_no,
        amount=p.amount,
        method=p.method,
        trx_id=p.trx_id,
        received_at=p.received_at,
        received_by_name=receiver.name if receiver else None,
        allocations=[
            AllocationOut(fee_due_id=d.id, month=d.month, batch_id=d.batch_id, amount=a, due_status=d.status)
            for d, a in res.allocations
        ],
        outstanding_after=await _student_outstanding(t, st.id),
        sms_status=res.sms_status,
        replayed=res.replayed,
    )


async def _payments(t: Tenant, conds: list, limit: int) -> list[PaymentOut]:
    Rev = aliased(Payment)
    rows = (
        await t.db.execute(
            select(Payment, Student.name, Student.roll_no, User.name, Rev.id)
            .join(Student, Student.id == Payment.student_id)
            .join(User, User.id == Payment.received_by)
            .outerjoin(Rev, Rev.reverses_payment_id == Payment.id)
            .where(and_(*conds))
            .order_by(Payment.received_at.desc())
            .limit(limit)
        )
    ).all()
    ids = [r[0].id for r in rows]
    months: dict[uuid.UUID, list[date]] = {i: [] for i in ids}
    if ids:
        for pid, m in await t.db.execute(
            select(PaymentAllocation.payment_id, FeeDue.month)
            .join(FeeDue, FeeDue.id == PaymentAllocation.fee_due_id)
            .where(PaymentAllocation.payment_id.in_(ids))
            .order_by(FeeDue.month)
        ):
            if m not in months[pid]:
                months[pid].append(m)
    names = academics.batch_names_subquery()
    batch_names = dict(
        (await t.db.execute(select(names.c.student_id, names.c.batch_names).where(names.c.student_id.in_({r[0].student_id for r in rows})))).all()
    ) if rows else {}
    return [
        PaymentOut(
            id=p.id, student_id=p.student_id, student_name=sname, roll_no=roll, kind=p.kind, amount=p.amount,
            method=p.method, trx_id=p.trx_id, note=p.note, received_by=p.received_by, received_by_name=rname,
            received_at=p.received_at, reversed=rev_id is not None, reversal_reason=p.reversal_reason,
            months=months.get(p.id, []), batch_names=list(batch_names.get(p.student_id) or []),
        )
        for p, sname, roll, rname, rev_id in rows
    ]


@router.get("/overview", response_model=FeeOverview)
async def fee_overview(t: Tenant, month: str | None = None):
    """Collection for one month (default: this one) against what was billed, today's takings and open dues."""
    db = t.db
    m = parse_month(month) if month else current_month()
    start, end = local_day_bounds_utc(m)[0], local_day_bounds_utc(add_months(m, 1))[0]
    live = and_(Payment.kind == "payment", ~exists().where(Rev.reverses_payment_id == Payment.id))
    by_method = dict(
        (
            await db.execute(
                select(Payment.method, func.sum(Payment.amount))
                .where(live, Payment.received_at >= start, Payment.received_at < end)
                .group_by(Payment.method)
            )
        ).all()
    )
    billed = await db.scalar(select(func.coalesce(func.sum(FeeDue.amount - FeeDue.discount), 0)).where(FeeDue.month == m))
    t0, t1 = local_day_bounds_utc(today_local())
    today_amount, today_count = (
        await db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0), func.count(func.distinct(Payment.student_id))).where(
                live, Payment.received_at >= t0, Payment.received_at < t1
            )
        )
    ).one()
    receipts = await db.scalar(
        select(func.count()).select_from(OutboxMessage).where(
            OutboxMessage.template_key == "receipt", OutboxMessage.created_at >= t0, OutboxMessage.created_at < t1
        )
    )
    dues = academics.outstanding_subquery()
    total_due, due_students = (
        await db.execute(
            select(func.coalesce(func.sum(dues.c.outstanding), 0), func.count().filter(dues.c.outstanding > 0))
            .select_from(dues)
            .join(Student, Student.id == dues.c.student_id)
            .where(Student.deleted_at.is_(None))
        )
    ).one()
    return FeeOverview(
        month=m, collected=int(sum(by_method.values())), billed=int(billed or 0),
        today_amount=int(today_amount), today_count=int(today_count), today_receipts=int(receipts or 0),
        total_due=int(total_due), due_students=int(due_students), by_method={k: int(v) for k, v in by_method.items()},
    )


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(
    t: Tenant,
    date_from: date | None = None,
    date_to: date | None = None,
    student_id: uuid.UUID | None = None,
    received_by: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    conds = []
    if not t.can("reports.view_all"):
        # Assistants see only their own entries from today.
        received_by, date_from, date_to = t.user.id, today_local(), today_local()
    if date_from:
        conds.append(Payment.received_at >= local_day_bounds_utc(date_from)[0])
    if date_to:
        conds.append(Payment.received_at < local_day_bounds_utc(date_to)[1])
    if student_id:
        conds.append(Payment.student_id == student_id)
    if received_by:
        conds.append(Payment.received_by == received_by)
    return await _payments(t, conds, limit)


@router.post("/payments/{payment_id}/reverse", response_model=PaymentOut)
async def reverse_payment(payment_id: uuid.UUID, body: ReverseIn, t: Tenant):
    rev = await service.reverse_payment(t, payment_id, body.reason)
    return (await _payments(t, [Payment.id == rev.id], 1))[0]


@router.get("/dues", response_model=DuesPage)
async def list_dues(
    t: Tenant,
    batch_id: uuid.UUID | None = None,
    q: str | None = None,
    min_months: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Students who owe money, biggest first (blueprint F5 'বকেয়া তালিকা')."""
    cm = current_month()
    conds = [FeeDue.status.in_(service.OPEN), FeeDue.month <= cm, Student.status == "active", Student.deleted_at.is_(None)]
    if batch_id:
        conds.append(FeeDue.batch_id == batch_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        conds.append((Student.name.ilike(like)) | (Student.roll_no == q.strip()))
    outstanding = func.sum(FeeDue.amount - FeeDue.discount - FeeDue.paid_amount)
    months = func.count(func.distinct(FeeDue.month))
    last_pay = (
        select(func.max(Payment.received_at))
        .where(Payment.student_id == Student.id, Payment.kind == "payment")
        .correlate(Student)
        .scalar_subquery()
    )
    grouped = (
        select(
            Student.id, Student.name, Student.roll_no, Student.guardian_phone,
            func.array_agg(func.distinct(Batch.name)), outstanding.label("outstanding"),
            months.label("months"), func.min(FeeDue.month), last_pay,
        )
        .join(FeeDue, FeeDue.student_id == Student.id)
        .join(Batch, Batch.id == FeeDue.batch_id)
        .where(and_(*conds))
        .group_by(Student.id)
        .having(outstanding > 0)
    )
    if min_months:
        grouped = grouped.having(months >= min_months)
    sub = grouped.subquery()
    totals = (await t.db.execute(select(func.count(), func.coalesce(func.sum(sub.c.outstanding), 0)).select_from(sub))).one()
    rows = (await t.db.execute(grouped.order_by(outstanding.desc(), Student.roll_no).limit(limit).offset(offset))).all()
    return DuesPage(
        items=[
            DueStudentRow(
                student_id=r[0], name=r[1], roll_no=r[2], guardian_phone=r[3], batch_names=list(r[4] or []),
                outstanding=int(r[5]), months=int(r[6]), oldest_month=r[7], last_payment_at=r[8],
            )
            for r in rows
        ],
        total_students=int(totals[0]),
        total_outstanding=int(totals[1]),
    )


@router.get("/students/{student_id}/ledger", response_model=LedgerOut)
async def student_ledger(student_id: uuid.UUID, t: Tenant):
    st = await t.db.get(Student, student_id)
    if not st or st.deleted_at:
        raise not_found("Student")
    rows = (
        await t.db.execute(
            select(FeeDue, Batch.name)
            .join(Batch, Batch.id == FeeDue.batch_id)
            .where(FeeDue.student_id == student_id)
            .order_by(FeeDue.month.desc(), Batch.name)
        )
    ).all()
    cm = current_month()
    dues = [
        DueOut(
            id=d.id, month=d.month, batch_id=d.batch_id, batch_name=bname, amount=d.amount, discount=d.discount,
            discount_reason=d.discount_reason, paid_amount=d.paid_amount, outstanding=service.outstanding_of(d),
            status=d.status, due_date=d.due_date,
        )
        for d, bname in rows
    ]
    outstanding = sum(x.outstanding for x in dues if x.month <= cm and x.status in service.OPEN)
    advance = sum(x.paid_amount for x in dues if x.month > cm)
    payments = await _payments(t, [Payment.student_id == student_id], 200)
    return LedgerOut(student_id=student_id, outstanding=outstanding, advance=advance, dues=dues, payments=payments)


@router.post("/dues/{due_id}/discount", response_model=DueOut)
async def discount_due(due_id: uuid.UUID, body: DiscountIn, t: Tenant):
    d = await service.set_discount(t, due_id, body.discount, body.reason)
    batch = await t.db.get(Batch, d.batch_id)
    return DueOut(
        id=d.id, month=d.month, batch_id=d.batch_id, batch_name=batch.name if batch else "", amount=d.amount,
        discount=d.discount, discount_reason=d.discount_reason, paid_amount=d.paid_amount,
        outstanding=service.outstanding_of(d), status=d.status, due_date=d.due_date,
    )


@router.post("/generate")
async def generate_dues(body: GenerateIn, t: Tenant):
    """Normally the 1st-of-month scheduler does this; owners can also run it by hand (idempotent)."""
    t.require("fees.generate")
    month = parse_month(body.month) if body.month else current_month()
    if month > current_month():
        raise AppError("future_month", "Can't generate future months", "ভবিষ্যতের মাসের ফি তৈরি করা যাবে না", 422, "month")
    result = await service.generate_month(t.db, t.institution, month, t.user.id)
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="fees.generated",
        entity="fee_due", after=result, ip=t.ip,
    )
    return result


@router.post("/reminders")
async def send_reminders(body: RemindIn, t: Tenant):
    return await service.send_reminders(t, body.student_ids, body.batch_id)


@router.get("/cash/mine", response_model=CashSummary)
async def my_cash(t: Tenant, day: date | None = None):
    """'আমার হাতে নগদ' — what this user collected today, by method."""
    d = day or today_local()
    start, end = local_day_bounds_utc(d)
    rows = (
        await t.db.execute(
            select(Payment.method, func.sum(Payment.amount), func.count().filter(Payment.kind == "payment"))
            .where(Payment.received_by == t.user.id, Payment.received_at >= start, Payment.received_at < end)
            .group_by(Payment.method)
        )
    ).all()
    by = {m: int(s or 0) for m, s, _ in rows}
    unhanded = await t.db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.received_by == t.user.id, Payment.method == "cash", Payment.handover_id.is_(None)
        )
    )
    return CashSummary(
        date=d, cash=by.get("cash", 0), bkash=by.get("bkash", 0), nagad=by.get("nagad", 0), bank=by.get("bank", 0),
        count=sum(c for _, _, c in rows), unhanded_cash=int(unhanded or 0),
    )


# Exposed for the enrollment screen: effective monthly fee preview.
@router.get("/students/{student_id}/monthly")
async def monthly_total(student_id: uuid.UUID, t: Tenant):
    rows = (
        await t.db.execute(
            select(func.coalesce(Enrollment.custom_fee, Batch.monthly_fee))
            .join(Batch, Batch.id == Enrollment.batch_id)
            .where(Enrollment.student_id == student_id, Enrollment.end_month.is_(None), Enrollment.deleted_at.is_(None))
        )
    ).scalars()
    return {"monthly_total": int(sum(rows))}
