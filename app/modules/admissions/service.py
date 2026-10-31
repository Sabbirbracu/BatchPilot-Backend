"""Admissions: seats, QR-link offers, family join requests, and admitting a student in one step.

Seats: a batch's capacity is shared by its active students and the pending requests that still hold
a seat (hold_until in the future). The batch row is locked while a seat is taken, so two families can
never get the last seat at once. Expired holds are marked lazily whenever requests are read."""

import secrets
import uuid
from datetime import date, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import OpsSessionLocal
from app.core.deps import TenantContext
from app.core.errors import AppError, not_found
from app.core.money import BN_DIGITS, format_taka, month_label
from app.core.phone import normalize_bd_phone, normalize_optional
from app.core.timeutil import DHAKA, current_month, now_utc, today_local
from app.modules.academics import service as academics
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.academics.schemas import EnrollmentIn, StudentIn
from app.modules.admissions.models import AdmissionLink, AdmissionRequest
from app.modules.admissions.schemas import (
    AdmittedOut,
    FeeChoiceIn,
    LinkIn,
    RequestOut,
    SeatBatchOut,
)
from app.modules.audit import service as audit
from app.modules.fees import service as fees
from app.modules.fees.models import FeeDue, Payment
from app.modules.identity.models import Institution
from app.modules.messaging import service as messaging

CODE_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # no 0/o/1/l/i: codes get typed from leaflets


def new_link_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(7))


# ---------------------------------------------------------------- seats and fees


async def expire_stale(db: AsyncSession) -> None:
    await db.execute(
        update(AdmissionRequest)
        .where(AdmissionRequest.status == "pending", AdmissionRequest.hold_until < now_utc())
        .values(status="expired")
    )


async def seat_stats(
    db: AsyncSession, batch_ids: list[uuid.UUID], exclude_request: uuid.UUID | None = None
) -> dict[uuid.UUID, tuple[int, int]]:
    """batch_id -> (active students, pending requests holding a seat)."""
    if not batch_ids:
        return {}
    students = dict(
        (
            await db.execute(
                select(Enrollment.batch_id, func.count(func.distinct(Enrollment.student_id)))
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
        ).all()
    )
    held_q = (
        select(AdmissionRequest.batch_id, func.count())
        .where(
            AdmissionRequest.batch_id.in_(batch_ids),
            AdmissionRequest.status == "pending",
            AdmissionRequest.hold_until > now_utc(),
        )
        .group_by(AdmissionRequest.batch_id)
    )
    if exclude_request:
        held_q = held_q.where(AdmissionRequest.id != exclude_request)
    held = dict((await db.execute(held_q)).all())
    return {b: (int(students.get(b, 0)), int(held.get(b, 0))) for b in batch_ids}


def seat_batch_out(b: Batch, stats: tuple[int, int]) -> SeatBatchOut:
    students, held = stats
    left = max(b.capacity - students - held, 0) if b.capacity else None
    return SeatBatchOut(
        id=b.id, name=b.name, subject=b.subject, class_level=b.class_level, days=b.days,
        start_time=b.start_time, end_time=b.end_time, location=b.location, starts_on=b.starts_on,
        fee_plan=b.fee_plan, monthly_fee=b.monthly_fee, course_fee=b.course_fee, installments=b.installments,
        admission_fee=b.admission_fee, capacity=b.capacity, students=students, held=held, seats_left=left,
    )


async def lock_seat(db: AsyncSession, batch: Batch, exclude_request: uuid.UUID | None = None) -> None:
    """Lock the batch row and fail if it has no free seat (capacity minus students minus holds)."""
    await db.execute(select(Batch.id).where(Batch.id == batch.id).with_for_update())
    if not batch.capacity:
        return
    students, held = (await seat_stats(db, [batch.id], exclude_request))[batch.id]
    if students + held >= batch.capacity:
        raise AppError("batch_full", "This batch is full", "এই ব্যাচে আর সিট খালি নেই", 409, "batch_id")


def first_fee(batch: Batch, custom_fee: int | None = None) -> int:
    """What the student pays first besides admission: the first month, the course fee or instalment 1."""
    if batch.fee_plan == "course":
        total = custom_fee if custom_fee is not None else (batch.course_fee or 0)
        n = max(batch.installments, 1)
        return total if n == 1 else (total // n // 100) * 100
    return custom_fee if custom_fee is not None else batch.monthly_fee


def offer_active(link: AdmissionLink, on: date | None = None) -> bool:
    return bool(link.offer_kind and link.offer_value) and (link.offer_until is None or (on or today_local()) <= link.offer_until)


def offer_discounts(link: AdmissionLink | None, batch: Batch) -> tuple[int, int]:
    """(off the admission fee, off the first fee / course fee) for joining today through this link."""
    if not link or not offer_active(link):
        return 0, 0
    if link.offer_on == "admission":
        base = batch.admission_fee or 0
    else:
        base = (batch.course_fee or 0) if batch.fee_plan == "course" else batch.monthly_fee
    if link.offer_kind == "percent":
        off = base * link.offer_value // 100 // 100 * 100  # whole taka
    else:
        off = link.offer_value
    off = min(off, base)
    return (off, 0) if link.offer_on == "admission" else (0, off)


# ---------------------------------------------------------------- links


async def link_counts(db: AsyncSession, link_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    if not link_ids:
        return {}
    rows = (
        await db.execute(
            select(AdmissionRequest.link_id, AdmissionRequest.status, func.count())
            .where(AdmissionRequest.link_id.in_(link_ids))
            .group_by(AdmissionRequest.link_id, AdmissionRequest.status)
        )
    ).all()
    out: dict[uuid.UUID, dict[str, int]] = {i: {} for i in link_ids}
    for lid, status, n in rows:
        out[lid][status] = int(n)
    return out


async def check_link_batches(db: AsyncSession, batch_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    ids = list(dict.fromkeys(batch_ids))
    found = set(
        (
            await db.execute(
                select(Batch.id).where(Batch.id.in_(ids), Batch.deleted_at.is_(None), Batch.archived_at.is_(None))
            )
        ).scalars()
    )
    if len(found) != len(ids):
        raise AppError("batch_not_found", "Pick open batches only", "শুধু চালু ব্যাচ বাছাই করুন", 422, "batch_ids")
    return ids


async def create_link(t: TenantContext, data: LinkIn) -> AdmissionLink:
    t.require("batch.manage")
    ids = await check_link_batches(t.db, data.batch_ids)
    code = new_link_code()
    async with OpsSessionLocal() as ops:  # codes are unique across all institutions
        while await ops.scalar(select(AdmissionLink.id).where(AdmissionLink.code == code)):
            code = new_link_code()
    link = AdmissionLink(
        institution_id=t.institution_id, code=code, created_by=t.user.id, is_active=True, scan_count=0,
        **{**data.model_dump(), "batch_ids": ids},
    )
    if not link.offer_kind:
        link.offer_value = None
    t.db.add(link)
    await t.db.flush()
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="admission_link.created",
        entity="admission_link", entity_id=link.id, after={"title": link.title, "batches": ids, "code": code}, ip=t.ip,
    )
    return link


async def find_link(code: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Public pages know only the code: find its institution on the ops connection (ids only)."""
    async with OpsSessionLocal() as ops:
        row = (
            await ops.execute(
                select(AdmissionLink.id, AdmissionLink.institution_id).where(
                    AdmissionLink.code == code.strip().lower(), AdmissionLink.deleted_at.is_(None)
                )
            )
        ).first()
    return (row[0], row[1]) if row else None


# ---------------------------------------------------------------- requests


async def request_out(db: AsyncSession, r: AdmissionRequest, institution_name: str | None = None) -> RequestOut:
    batch = await db.get(Batch, r.batch_id)
    link = await db.get(AdmissionLink, r.link_id) if r.link_id else None
    code = await db.scalar(select(Student.student_code).where(Student.id == r.student_id)) if r.student_id else None
    adm = (batch.admission_fee or 0) if batch else 0
    first = first_fee(batch) if batch else 0
    return RequestOut(
        id=r.id, status=r.status, batch_id=r.batch_id, batch_name=batch.name if batch else "",
        link_title=link.title if link else None, institution_name=institution_name,
        student_name=r.student_name, student_phone=r.student_phone, guardian_name=r.guardian_name,
        guardian_phone=r.guardian_phone, school=r.school, class_level=r.class_level, pay_method=r.pay_method,
        trx_id=r.trx_id, admission_fee=max(adm - r.offer_discount_admission, 0),
        first_fee=max(first - r.offer_discount_fee, 0), offer_discount_admission=r.offer_discount_admission,
        offer_discount_fee=r.offer_discount_fee, hold_until=r.hold_until, created_at=r.created_at,
        student_id=r.student_id, student_code=code, reject_reason=r.reject_reason,
    )


async def apply(
    db: AsyncSession, *, inst: Institution, link: AdmissionLink, user_id: uuid.UUID, user_phone: str, data
) -> AdmissionRequest:
    """A family applies from a QR page. Returns the existing request when the same child already applied."""
    if not link.is_active:
        raise AppError("link_closed", "Admission is closed on this link", "এই লিংকে ভর্তি বন্ধ আছে", 409)
    if data.batch_id not in link.batch_ids:
        raise AppError("batch_not_found", "Pick a batch from this page", "এই পেজের একটি ব্যাচ বাছাই করুন", 422, "batch_id")
    batch = await db.get(Batch, data.batch_id)
    if not batch or batch.deleted_at or batch.archived_at:
        raise AppError("batch_closed", "This batch is closed", "এই ব্যাচ বন্ধ করা হয়েছে", 409, "batch_id")
    guardian_phone = normalize_bd_phone(data.guardian_phone or user_phone, "guardian_phone")
    student_phone = normalize_optional(data.student_phone, "student_phone")
    name = data.student_name.strip()
    if data.pay_method != "cash" and not (data.trx_id or "").strip():
        raise AppError("trx_required", "Enter the transaction ID (TrxID)", "ট্রানজেকশন আইডি (TrxID) লিখুন", 422, "trx_id")

    await expire_stale(db)
    same = (
        await db.execute(
            select(AdmissionRequest).where(
                AdmissionRequest.batch_id == batch.id,
                AdmissionRequest.guardian_phone == guardian_phone,
                func.lower(AdmissionRequest.student_name) == name.lower(),
                AdmissionRequest.status.in_(("pending", "admitted")),
            )
        )
    ).scalars().first()
    if same:
        return same
    enrolled = await db.scalar(
        select(Student.id)
        .join(Enrollment, Enrollment.student_id == Student.id)
        .where(
            Enrollment.batch_id == batch.id, Enrollment.end_month.is_(None), Enrollment.deleted_at.is_(None),
            Student.status == "active", Student.deleted_at.is_(None),
            func.lower(Student.name) == name.lower(), Student.guardian_phone == guardian_phone,
        )
    )
    if enrolled:
        raise AppError("already_enrolled", "Already admitted to this batch", "এই ব্যাচে আগেই ভর্তি আছে", 409)

    await lock_seat(db, batch)
    adm_off, fee_off = offer_discounts(link, batch)
    req = AdmissionRequest(
        institution_id=inst.id, link_id=link.id, batch_id=batch.id, user_id=user_id, student_name=name,
        student_phone=student_phone, guardian_name=(data.guardian_name or "").strip() or None,
        guardian_phone=guardian_phone, school=(data.school or "").strip() or None,
        class_level=(data.class_level or "").strip() or None, pay_method=data.pay_method,
        trx_id=(data.trx_id or "").strip() or None, offer_discount_admission=adm_off, offer_discount_fee=fee_off,
        status="pending", hold_until=now_utc() + timedelta(days=link.hold_days),
    )
    db.add(req)
    await db.flush()
    await messaging.enqueue(
        db, institution_id=inst.id, template_key="admission_request", to_phone=guardian_phone,
        dedupe_key=f"admission_request:{req.id}",
        values={
            "student": name, "batch": batch.name, "institution": inst.name,
            "hold": format_date_bn(req.hold_until.astimezone(DHAKA).date()),
        },
    )
    return req


def format_date_bn(d: date) -> str:
    return f"{d.day} {month_label(d)}".translate(BN_DIGITS)


async def get_request(db: AsyncSession, request_id: uuid.UUID) -> AdmissionRequest:
    r = await db.get(AdmissionRequest, request_id, with_for_update=True)
    if not r:
        raise not_found("Request")
    return r


async def reject(t: TenantContext, request_id: uuid.UUID, reason: str | None) -> AdmissionRequest:
    t.require("student.manage")
    r = await get_request(t.db, request_id)
    if r.status not in ("pending", "expired"):
        raise AppError("request_closed", "This request is already handled", "এই আবেদনের কাজ আগেই হয়ে গেছে", 409)
    r.status, r.reject_reason = "rejected", (reason or "").strip() or None
    r.decided_by, r.decided_at = t.user.id, now_utc()
    await audit.record(
        t.db, institution_id=t.institution_id, actor_id=t.user.id, action="admission.rejected",
        entity="admission_request", entity_id=r.id, after={"reason": r.reject_reason}, ip=t.ip,
    )
    return r


# ---------------------------------------------------------------- admitting


async def admit(
    t: TenantContext,
    *,
    batch_id: uuid.UUID,
    student: StudentIn,
    fee: FeeChoiceIn,
    idempotency_key: str,
    offer: tuple[int, int] = (0, 0),
    request: AdmissionRequest | None = None,
    force: bool = False,
) -> AdmittedOut:
    """Create the student, enroll, charge admission/first fees, take the money if paid, and SMS the guardian.
    One transaction, so a failure leaves nothing half-done."""
    t.require("student.manage")
    db, inst = t.db, t.institution
    pay_key = f"admit:{idempotency_key}"
    done = (await db.execute(select(Payment).where(Payment.idempotency_key == pay_key))).scalar_one_or_none()
    if done:  # a retried tap after a slow network: show the first result again
        return await _admitted_out(t, await db.get(Student, done.student_id), batch_id, done)

    batch = await academics.get_batch(db, batch_id)
    if batch.archived_at:
        raise AppError("batch_archived", "This batch is closed", "এই ব্যাচ বন্ধ করা হয়েছে", 409, "batch_id")
    if not force:
        await lock_seat(db, batch, exclude_request=request.id if request else None)

    base_adm = fee.admission_fee if fee.admission_fee is not None else (batch.admission_fee or 0)
    waived = not fee.charge_admission and base_adm > 0
    if (waived or fee.admission_discount or fee.fee_discount or fee.custom_fee is not None) and not t.can("due.discount"):
        raise AppError(
            "forbidden", "Only the owner can waive or discount fees", "ফি মাফ বা ছাড় শুধু মালিক দিতে পারবেন", 403, "admission_fee"
        )
    adm = base_adm if fee.charge_admission else 0
    enr = EnrollmentIn(
        batch_id=batch.id,
        custom_fee=fee.custom_fee,
        discount_note=fee.discount_note or ("অফার" if any(offer) else None),
        admission_fee=adm or None,
        admission_discount=min(offer[0] + fee.admission_discount, adm),
        fee_discount=offer[1] + fee.fee_discount,
    )
    st = await academics.create_student(
        t, student.model_copy(update={"enrollments": [enr]}), welcome=False, checked=True
    )

    # Paid on the spot: the payment row also carries the retry key. Unpaid admissions rely on the
    # duplicate-student check instead.
    payment = None
    if fee.payment:
        t.require("payment.record")
        res = await fees.record_payment(
            t, student_id=st.id, amount=fee.payment.amount, method=fee.payment.method,
            idempotency_key=pay_key, trx_id=fee.payment.trx_id, note="ভর্তির সময়",
        )
        payment = res.payment

    out = await _admitted_out(t, st, batch.id, payment)
    if st.guardian_phone:
        await messaging.enqueue(
            db, institution_id=inst.id, template_key="admitted", to_phone=st.guardian_phone, student_id=st.id,
            dedupe_key=f"admitted:{st.id}:{batch.id}",
            values={
                "student": st.name, "batch": batch.name, "student_id": st.student_code,
                "due": format_taka(out.outstanding), "institution": inst.name,
            },
        )
    if request:
        request.status, request.student_id = "admitted", st.id
        request.decided_by, request.decided_at = t.user.id, now_utc()
    await audit.record(
        db, institution_id=inst.id, actor_id=t.user.id, action="admission.admitted", entity="student",
        entity_id=st.id,
        after={
            "batch_id": batch.id, "request_id": request.id if request else None, "admission_fee": adm,
            "offer": list(offer), "admission_discount": fee.admission_discount, "fee_discount": fee.fee_discount,
            "paid": fee.payment.amount if fee.payment else 0,
        },
        ip=t.ip,
    )
    await db.flush()
    return out


async def _admitted_out(t: TenantContext, st: Student, batch_id: uuid.UUID, payment: Payment | None) -> AdmittedOut:
    from app.modules.fees.router import receipt_out  # the same receipt the fee desk shows

    db = t.db
    batch = await db.get(Batch, batch_id)
    cm = current_month()
    net = FeeDue.amount - FeeDue.discount
    charged, outstanding = (
        await db.execute(
            select(
                func.coalesce(func.sum(net), 0),
                func.coalesce(func.sum(net - FeeDue.paid_amount).filter(FeeDue.status.in_(fees.OPEN)), 0),
            ).where(FeeDue.student_id == st.id, FeeDue.month <= cm)
        )
    ).one()
    receipt = None
    if payment:
        res = fees.PaymentResult(payment, await fees._allocations_for(db, payment.id))
        receipt = await receipt_out(t, res)
    return AdmittedOut(
        student_id=st.id, name=st.name, student_code=st.student_code, batch_name=batch.name if batch else "",
        guardian_phone=st.guardian_phone, charged=int(charged), outstanding=int(outstanding), receipt=receipt,
    )


async def guardian_children(db: AsyncSession, phone: str) -> tuple[str | None, str | None, list[Student]]:
    rows = list(
        (
            await db.execute(
                select(Student)
                .where(Student.guardian_phone == phone, Student.deleted_at.is_(None))
                .order_by(Student.created_at.desc())
            )
        ).scalars()
    )
    name = next((s.guardian_name for s in rows if s.guardian_name), None)
    school = next((s.school for s in rows if s.school), None)
    return name, school, rows
