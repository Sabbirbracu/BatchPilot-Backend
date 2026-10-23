"""Fees ledger service — the heart of BatchPilot.

Rules (System Design §8):
- one fee_dues row per enrollment per month; status derived from amount − discount vs paid_amount
- a payment is allocated to open dues oldest-first; any surplus goes to future months
  (created on demand) so money is never left unallocated
- payments are never edited or deleted; a mistake is a reversal row with negative allocations
- payment + allocations + receipt SMS + audit row commit in one transaction
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import TenantContext
from app.core.errors import AppError, forbidden, not_found
from app.core.ids import new_id
from app.core.money import format_taka, months_label
from app.core.timeutil import add_months, current_month, due_date_for, now_utc, today_local
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.audit import service as audit
from app.modules.automation.models import AutomationRun
from app.modules.fees.models import PAYMENT_METHODS, FeeDue, Payment, PaymentAllocation
from app.modules.identity.models import Institution
from app.modules.messaging import service as messaging

MAX_ADVANCE_MONTHS = 12
OPEN = ("due", "partial")


def due_status(amount: int, discount: int, paid: int) -> str:
    net = amount - discount
    if net <= 0:
        return "waived"
    if paid >= net:
        return "paid"
    if paid > 0:
        return "partial"
    return "due"


def outstanding_of(d: FeeDue) -> int:
    return max(d.amount - d.discount - d.paid_amount, 0)


def _fee_for(enrollment: Enrollment, batch: Batch) -> int:
    return enrollment.custom_fee if enrollment.custom_fee is not None else batch.monthly_fee


async def ensure_due(
    db: AsyncSession, inst: Institution, enrollment: Enrollment, batch: Batch, month: date
) -> FeeDue:
    """Idempotently create the due for (enrollment, month) and return it."""
    amount = _fee_for(enrollment, batch)
    await db.execute(
        insert(FeeDue)
        .values(
            id=new_id(),
            institution_id=inst.id,
            student_id=enrollment.student_id,
            enrollment_id=enrollment.id,
            batch_id=batch.id,
            month=month,
            amount=amount,
            discount=0,
            paid_amount=0,
            status=due_status(amount, 0, 0),
            due_date=due_date_for(month, inst.fee_due_day),
        )
        .on_conflict_do_nothing(index_elements=["enrollment_id", "month"])
    )
    return (
        await db.execute(
            select(FeeDue).where(FeeDue.enrollment_id == enrollment.id, FeeDue.month == month)
        )
    ).scalar_one()


# ---------------------------------------------------------------- monthly generation


async def generate_month(db: AsyncSession, inst: Institution, month: date, actor_id: uuid.UUID | None) -> dict:
    """Create dues for every active enrollment for `month`. Safe to run any number of times."""
    period = month.strftime("%Y-%m")
    await db.execute(
        insert(AutomationRun)
        .values(id=new_id(), institution_id=inst.id, type="fee.generate", period_key=period, status="running")
        .on_conflict_do_nothing(index_elements=["type", "institution_id", "period_key"])
    )
    rows = (
        await db.execute(
            select(Enrollment, Batch)
            .join(Batch, Batch.id == Enrollment.batch_id)
            .join(Student, Student.id == Enrollment.student_id)
            .where(
                Enrollment.deleted_at.is_(None),
                Enrollment.start_month <= month,
                or_(Enrollment.end_month.is_(None), Enrollment.end_month >= month),
                Student.status == "active",
                Student.deleted_at.is_(None),
                Batch.archived_at.is_(None),
                Batch.deleted_at.is_(None),
            )
        )
    ).all()
    values = []
    for enr, batch in rows:
        amount = _fee_for(enr, batch)
        values.append(
            dict(
                id=new_id(),
                institution_id=inst.id,
                student_id=enr.student_id,
                enrollment_id=enr.id,
                batch_id=batch.id,
                month=month,
                amount=amount,
                discount=0,
                paid_amount=0,
                status=due_status(amount, 0, 0),
                due_date=due_date_for(month, inst.fee_due_day),
            )
        )
    created = 0
    if values:
        res = await db.execute(
            insert(FeeDue).values(values).on_conflict_do_nothing(index_elements=["enrollment_id", "month"]).returning(FeeDue.id)
        )
        created = len(res.all())
    stats = {"enrollments": len(rows), "created": created, "by": str(actor_id) if actor_id else "scheduler"}
    run = (
        await db.execute(
            select(AutomationRun).where(
                AutomationRun.type == "fee.generate",
                AutomationRun.institution_id == inst.id,
                AutomationRun.period_key == period,
            )
        )
    ).scalar_one()
    run.status, run.stats = "done", stats
    return {"month": period, **stats}


# ---------------------------------------------------------------- payments


@dataclass
class PaymentResult:
    payment: Payment
    allocations: list[tuple[FeeDue, int]] = field(default_factory=list)
    replayed: bool = False
    sms_status: str | None = None


async def _load_student(db: AsyncSession, student_id: uuid.UUID) -> Student:
    st = await db.get(Student, student_id)
    if not st or st.deleted_at:
        raise not_found("Student")
    return st


async def _allocations_for(db: AsyncSession, payment_id: uuid.UUID) -> list[tuple[FeeDue, int]]:
    rows = (
        await db.execute(
            select(FeeDue, PaymentAllocation.amount)
            .join(PaymentAllocation, PaymentAllocation.fee_due_id == FeeDue.id)
            .where(PaymentAllocation.payment_id == payment_id)
            .order_by(FeeDue.month)
        )
    ).all()
    return [(d, a) for d, a in rows]


async def _future_dues(
    db: AsyncSession, inst: Institution, student_id: uuid.UUID, needed: int
) -> list[FeeDue]:
    """Create next months' dues on demand, month by month, only as many as `needed` covers,
    so an advance payment always has a target."""
    rows = (
        await db.execute(
            select(Enrollment, Batch)
            .join(Batch, Batch.id == Enrollment.batch_id)
            .where(
                Enrollment.student_id == student_id,
                Enrollment.deleted_at.is_(None),
                Enrollment.end_month.is_(None),
                Batch.archived_at.is_(None),
            )
            .order_by(Enrollment.created_at)
        )
    ).all()
    if not rows:
        return []
    cm = current_month()
    cursor: dict[uuid.UUID, date] = {}
    for enr, _ in rows:
        last = await db.scalar(select(func.max(FeeDue.month)).where(FeeDue.enrollment_id == enr.id))
        cursor[enr.id] = add_months(last, 1) if last else max(enr.start_month, cm)

    out: list[FeeDue] = []
    covered = 0
    month, limit = min(cursor.values()), add_months(cm, MAX_ADVANCE_MONTHS)
    while covered < needed and month <= limit:
        for enr, batch in rows:
            if cursor[enr.id] > month or covered >= needed:
                continue
            d = await ensure_due(db, inst, enr, batch, month)
            cursor[enr.id] = add_months(month, 1)
            if d.status in OPEN:
                out.append(d)
                covered += outstanding_of(d)
        month = add_months(month, 1)
    return out


async def record_payment(
    t: TenantContext,
    *,
    student_id: uuid.UUID,
    amount: int,
    method: str,
    idempotency_key: str,
    trx_id: str | None = None,
    note: str | None = None,
    due_ids: list[uuid.UUID] | None = None,
) -> PaymentResult:
    t.require("payment.record")
    db, inst = t.db, t.institution

    existing = (
        await db.execute(select(Payment).where(Payment.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing:
        # Safe retry: return the original receipt, never double-count.
        return PaymentResult(existing, await _allocations_for(db, existing.id), replayed=True)

    if amount <= 0:
        raise AppError("invalid_amount", "Amount must be more than zero", "টাকার পরিমাণ শূন্যের বেশি হতে হবে", 422, "amount")
    if method not in PAYMENT_METHODS:
        raise AppError("invalid_method", "Unknown payment method", "পেমেন্টের মাধ্যম সঠিক নয়", 422, "method")
    student = await _load_student(db, student_id)

    # Lock this student's open dues so two assistants can't allocate the same month twice.
    open_dues = list(
        (
            await db.execute(
                select(FeeDue)
                .where(FeeDue.student_id == student.id, FeeDue.status.in_(OPEN))
                .order_by(FeeDue.month, FeeDue.created_at)
                .with_for_update()
            )
        ).scalars()
    )
    if due_ids:
        wanted = set(due_ids)
        open_dues.sort(key=lambda d: (d.id not in wanted, d.month))

    remaining = amount
    plan: list[tuple[FeeDue, int]] = []
    for d in open_dues:
        if remaining <= 0:
            break
        take = min(outstanding_of(d), remaining)
        if take > 0:
            plan.append((d, take))
            remaining -= take
    if remaining > 0:
        for d in await _future_dues(db, inst, student.id, remaining):
            if remaining <= 0:
                break
            if d.status not in OPEN:
                continue
            take = min(outstanding_of(d), remaining)
            if take > 0:
                plan.append((d, take))
                remaining -= take
    if remaining > 0:
        raise AppError(
            "overpayment",
            f"Amount is Tk {format_taka(remaining, False)} more than this student's dues",
            f"এই স্টুডেন্টের বকেয়ার চেয়ে ৳{format_taka(remaining)} বেশি",
            422,
            "amount",
        )

    payment = Payment(
        id=new_id(),
        institution_id=inst.id,
        student_id=student.id,
        kind="payment",
        amount=amount,
        method=method,
        trx_id=(trx_id or "").strip() or None,
        note=(note or "").strip() or None,
        received_by=t.user.id,
        received_at=now_utc(),
        idempotency_key=idempotency_key,
    )
    db.add(payment)
    await db.flush()
    for d, take in plan:
        db.add(PaymentAllocation(institution_id=inst.id, payment_id=payment.id, fee_due_id=d.id, amount=take))
        d.paid_amount += take
        d.status = due_status(d.amount, d.discount, d.paid_amount)

    result = PaymentResult(payment, plan)
    if inst.settings.get("receipt_sms", True) and student.guardian_phone:
        msg = await messaging.enqueue(
            db,
            institution_id=inst.id,
            template_key="receipt",
            to_phone=student.guardian_phone,
            student_id=student.id,
            dedupe_key=f"receipt:{payment.id}",
            values={
                "student": student.name,
                "amount": format_taka(amount),
                "months": months_label([d.month for d, _ in plan]),
                "institution": inst.name,
            },
        )
        result.sms_status = msg.status if msg else None

    await audit.record(
        db,
        institution_id=inst.id,
        actor_id=t.user.id,
        action="payment.recorded",
        entity="payment",
        entity_id=payment.id,
        after={
            "student_id": student.id,
            "amount": amount,
            "method": method,
            "allocations": [{"due_id": d.id, "month": d.month, "amount": a} for d, a in plan],
        },
        ip=t.ip,
    )
    await db.flush()
    return result


async def reverse_payment(t: TenantContext, payment_id: uuid.UUID, reason: str) -> Payment:
    db, inst = t.db, t.institution
    payment = await db.get(Payment, payment_id, with_for_update=True)
    if not payment or payment.kind != "payment":
        raise not_found("Payment")
    already = await db.scalar(select(Payment.id).where(Payment.reverses_payment_id == payment.id))
    if already:
        raise AppError("already_reversed", "This payment was already reversed", "এই পেমেন্ট আগেই বাতিল হয়েছে", 409)

    if not t.can("payment.reverse_any"):
        window = timedelta(minutes=get_settings().assistant_reverse_minutes)
        own = payment.received_by == t.user.id
        if not (t.can("payment.record") and own and now_utc() - payment.received_at <= window):
            raise forbidden()
    if not reason.strip():
        raise AppError("reason_required", "Write a reason", "কারণ লিখুন", 422, "reason")

    allocs = await _allocations_for(db, payment.id)
    due_ids = [d.id for d, _ in allocs]
    if due_ids:
        await db.execute(select(FeeDue).where(FeeDue.id.in_(due_ids)).with_for_update())

    reversal = Payment(
        id=new_id(),
        institution_id=inst.id,
        student_id=payment.student_id,
        kind="reversal",
        amount=-payment.amount,
        method=payment.method,
        trx_id=payment.trx_id,
        received_by=t.user.id,
        received_at=now_utc(),
        idempotency_key=f"reversal:{payment.id}",
        reverses_payment_id=payment.id,
        reversal_reason=reason.strip(),
    )
    db.add(reversal)
    await db.flush()
    for d, a in allocs:
        db.add(PaymentAllocation(institution_id=inst.id, payment_id=reversal.id, fee_due_id=d.id, amount=-a))
        d.paid_amount -= a
        d.status = due_status(d.amount, d.discount, d.paid_amount)

    await messaging.cancel_pending(db, f"receipt:{payment.id}")
    await audit.record(
        db,
        institution_id=inst.id,
        actor_id=t.user.id,
        action="payment.reversed",
        entity="payment",
        entity_id=payment.id,
        before={"amount": payment.amount, "method": payment.method},
        after={"reversal_id": reversal.id, "reason": reversal.reversal_reason},
        ip=t.ip,
    )
    await db.flush()
    return reversal


async def set_discount(t: TenantContext, due_id: uuid.UUID, discount: int, reason: str) -> FeeDue:
    t.require("due.discount")
    db = t.db
    d = await db.get(FeeDue, due_id, with_for_update=True)
    if not d:
        raise not_found("Due")
    if discount < 0 or discount > d.amount:
        raise AppError("invalid_discount", "Discount can't exceed the fee", "ছাড় ফি-এর চেয়ে বেশি হতে পারে না", 422, "discount")
    if d.paid_amount > d.amount - discount:
        raise AppError(
            "discount_below_paid",
            "Already paid more than the fee after this discount; reverse a payment first",
            "এই ছাড়ের পর পরিশোধ বেশি হয়ে যায়; আগে পেমেন্ট বাতিল করুন",
            409,
            "discount",
        )
    if not reason.strip():
        raise AppError("reason_required", "Write a reason", "কারণ লিখুন", 422, "reason")
    before = {"discount": d.discount, "discount_reason": d.discount_reason, "status": d.status}
    d.discount, d.discount_reason = discount, reason.strip()
    d.status = due_status(d.amount, d.discount, d.paid_amount)
    await audit.record(
        db,
        institution_id=t.institution_id,
        actor_id=t.user.id,
        action="due.discounted",
        entity="fee_due",
        entity_id=d.id,
        before=before,
        after={"discount": d.discount, "discount_reason": d.discount_reason, "status": d.status},
        ip=t.ip,
    )
    await db.flush()
    return d


# ---------------------------------------------------------------- reminders


async def send_reminders(
    t: TenantContext, student_ids: list[uuid.UUID] | None, batch_id: uuid.UUID | None
) -> dict:
    """One-click reminder (blueprint F5). Deduped per student per day."""
    t.require("payment.record")
    db, inst = t.db, t.institution
    cm = current_month()
    conds = [FeeDue.status.in_(OPEN), FeeDue.month <= cm]
    if student_ids:
        conds.append(FeeDue.student_id.in_(student_ids))
    if batch_id:
        conds.append(FeeDue.batch_id == batch_id)
    rows = (
        await db.execute(
            select(FeeDue, Student)
            .join(Student, Student.id == FeeDue.student_id)
            .where(and_(*conds), Student.status == "active", Student.deleted_at.is_(None))
            .order_by(FeeDue.month)
        )
    ).all()
    per_student: dict[uuid.UUID, tuple[Student, int, list[date]]] = {}
    for d, s in rows:
        _, total, months = per_student.get(s.id, (s, 0, []))
        per_student[s.id] = (s, total + outstanding_of(d), months + [d.month])

    today = today_local().isoformat()
    queued = skipped = 0
    for sid, (s, total, months) in per_student.items():
        msg = await messaging.enqueue(
            db,
            institution_id=inst.id,
            template_key="reminder_1",
            to_phone=s.guardian_phone,
            student_id=sid,
            dedupe_key=f"remind:manual:{sid}:{today}",
            values={
                "student": s.name,
                "amount": format_taka(total),
                "months": months_label(months),
                "institution": inst.name,
                "bkash": inst.settings.get("bkash_number") or "",
                "roll": s.roll_no,
            },
        )
        if msg:
            queued += 1
        else:
            skipped += 1
    await audit.record(
        db,
        institution_id=inst.id,
        actor_id=t.user.id,
        action="reminders.sent",
        entity="fee_due",
        after={"queued": queued, "skipped": skipped, "batch_id": batch_id},
        ip=t.ip,
    )
    return {"queued": queued, "skipped": skipped}
