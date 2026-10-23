"""Demo data: "রাফি বায়োলজি কেয়ার" in Bogura — 5 batches, ~400 students, 3 months of fees,
two weeks of attendance (Blueprint §24 demo plan).

    .venv/bin/python -m scripts.seed_demo

Owner login: 01711111111 · Assistant login: 01722222222 (OTP is shown on screen in dev).
"""

import asyncio
import random
from datetime import datetime, time, timedelta

from sqlalchemy import select, update

from app.core.db import SessionLocal, set_context
from app.core.deps import TenantContext
from app.core.ids import new_id
from app.core.phone import normalize_bd_phone
from app.core.timeutil import DHAKA, add_months, current_month, today_local
from app.modules.academics.models import Batch, Enrollment, Student
from app.modules.academics.service import _next_roll
from app.modules.attendance.models import AttendanceRecord, AttendanceSession
from app.modules.attendance.service import weekday_code
from app.modules.fees import service as fees
from app.modules.fees.models import FeeDue, Payment
from app.modules.identity.models import Institution, Membership, User
from app.core.security import new_qr_token
from app.modules.messaging.service import ensure_default_templates

OWNER_PHONE = "01711111111"
ASSISTANT_PHONE = "01722222222"
INST_NAME = "রাফি বায়োলজি কেয়ার"

FIRST = ["রাফি", "সাকিব", "তানভীর", "নাফিস", "আরিফ", "মাহিন", "ফাহিম", "রিয়াদ", "সজীব", "ইমন", "জিসান", "রাকিব",
         "নুসরাত", "তাসনিম", "সাদিয়া", "মিম", "জান্নাত", "লামিয়া", "আফসানা", "রুমানা", "সুমাইয়া", "ফারিহা", "নাবিলা", "তানহা"]
LAST = ["আহমেদ", "হোসেন", "ইসলাম", "রহমান", "খান", "সরকার", "প্রামানিক", "মণ্ডল", "আলম", "চৌধুরী", "তালুকদার", "শেখ"]
GUARDIAN_FIRST = ["আব্দুল", "মোঃ", "রফিকুল", "শহিদুল", "আনোয়ার", "জাহাঙ্গীর", "মিজানুর", "নজরুল", "হাবিবুর", "কামরুল"]
SCHOOLS = ["বগুড়া জিলা স্কুল", "সরকারি আজিজুল হক কলেজ", "বগুড়া সরকারি বালিকা উচ্চ বিদ্যালয়",
           "পুলিশ লাইন্স স্কুল অ্যান্ড কলেজ", "ক্যান্টনমেন্ট পাবলিক স্কুল", "বগুড়া ওয়াইএমসিএ স্কুল"]

BATCHES = [
    dict(name="SSC ২০২৭ — ক", subject="জীববিজ্ঞান", class_level="SSC 2027", days=["sat", "mon", "wed"], start=time(7, 0), end=time(8, 30), fee=80000, n=85),
    dict(name="SSC ২০২৭ — খ", subject="জীববিজ্ঞান", class_level="SSC 2027", days=["sun", "tue", "thu"], start=time(16, 0), end=time(17, 30), fee=80000, n=80),
    dict(name="HSC ২০২৭", subject="জীববিজ্ঞান ১ম পত্র", class_level="HSC 2027", days=["sat", "mon", "wed"], start=time(17, 0), end=time(18, 30), fee=120000, n=90),
    dict(name="HSC ২০২৬ ফাইনাল", subject="জীববিজ্ঞান ২য় পত্র", class_level="HSC 2026", days=["sun", "tue", "thu"], start=time(9, 0), end=time(10, 30), fee=150000, n=75),
    dict(name="মেডিকেল ভর্তি প্রস্তুতি", subject="জীববিজ্ঞান", class_level="Admission", days=["fri"], start=time(10, 0), end=time(13, 0), fee=150000, n=70),
]


def phone() -> str:
    return normalize_bd_phone("01" + random.choice("3456789") + "".join(random.choice("0123456789") for _ in range(8)))


def at_local(d, hour: int, minute: int) -> datetime:
    return datetime.combine(d, time(hour, minute), tzinfo=DHAKA)


async def main() -> None:
    random.seed(42)
    async with SessionLocal() as db, db.begin():
        owner_phone = normalize_bd_phone(OWNER_PHONE)
        owner = (await db.execute(select(User).where(User.phone == owner_phone))).scalar_one_or_none()
        if owner:
            await set_context(db, user_id=owner.id)
            exists = await db.scalar(select(Membership.id).where(Membership.user_id == owner.id))
            if exists:
                print("Demo data already exists. Run scripts/reset_local_db.sh to start fresh.")
                return
        else:
            owner = User(phone=owner_phone, name="রাফি স্যার")
            db.add(owner)
        assistant = User(phone=normalize_bd_phone(ASSISTANT_PHONE), name="সজীব (অ্যাসিস্ট্যান্ট)")
        db.add(assistant)
        await db.flush()

        inst_id = new_id()
        await set_context(db, user_id=owner.id, institution_id=inst_id)
        inst = Institution(
            id=inst_id, name=INST_NAME, subject="জীববিজ্ঞান", city="বগুড়া", fee_due_day=10,
            settings={**fees_settings(), "receipt_sms": False, "absent_sms": False},
        )
        db.add(inst)
        await db.flush()
        owner_m = Membership(user_id=owner.id, institution_id=inst_id, role="owner")
        db.add_all([owner_m, Membership(user_id=assistant.id, institution_id=inst_id, role="assistant")])
        await ensure_default_templates(db, inst_id)

        cm = current_month()
        first_month = add_months(cm, -2)
        batches: list[tuple[Batch, int]] = []
        for b in BATCHES:
            batch = Batch(
                institution_id=inst_id, name=b["name"], subject=b["subject"], class_level=b["class_level"],
                days=b["days"], start_time=b["start"], end_time=b["end"], monthly_fee=b["fee"], capacity=100,
            )
            db.add(batch)
            batches.append((batch, b["n"]))
        await db.flush()

        # Students and enrollments
        students: list[Student] = []
        for batch, n in batches:
            for _ in range(n):
                name = f"{random.choice(FIRST)} {random.choice(LAST)}"
                st = Student(
                    institution_id=inst_id, roll_no=await _next_roll(db, inst), name=name,
                    phone=phone() if random.random() < 0.5 else None,
                    guardian_name=f"{random.choice(GUARDIAN_FIRST)} {name.split()[1]}",
                    guardian_phone=phone(), school=random.choice(SCHOOLS), class_level=batch.class_level,
                    joined_at=first_month, qr_token=new_qr_token(),
                )
                db.add(st)
                students.append(st)
                await db.flush()
                custom = 0 if random.random() < 0.02 else (60000 if random.random() < 0.04 else None)  # free / sibling discount
                db.add(Enrollment(
                    institution_id=inst_id, student_id=st.id, batch_id=batch.id, custom_fee=custom,
                    discount_note="ফ্রি স্টুডেন্ট" if custom == 0 else ("ভাই-বোন ছাড়" if custom else None),
                    start_month=first_month,
                ))
        await db.flush()

        for i in range(3):
            await fees.generate_month(db, inst, add_months(first_month, i), owner.id)
        print(f"Created {len(students)} students in {len(batches)} batches; dues for 3 months.")

        # Payments: older months mostly paid, this month partly (it's day {today}).
        ctx_owner = TenantContext(db=db, user=owner, session=None, ip=None, institution=inst, membership=owner_m)  # type: ignore[arg-type]
        ctx_asst = TenantContext(db=db, user=assistant, session=None, ip=None, institution=inst,
                                 membership=Membership(user_id=assistant.id, institution_id=inst_id, role="assistant"))  # type: ignore[arg-type]
        today = today_local()
        pay_rate = {first_month: 0.95, add_months(first_month, 1): 0.86, cm: 0.58}
        n_pay = 0
        for st in students:
            dues = list((await db.execute(select(FeeDue).where(FeeDue.student_id == st.id).order_by(FeeDue.month))).scalars())
            for d in dues:
                if d.status not in fees.OPEN or random.random() > pay_rate[d.month]:
                    continue
                amount = fees.outstanding_of(d)
                if random.random() < 0.06:
                    amount = amount // 2  # a few partial payments
                last_day = (today - d.month).days if d.month == cm else 24
                day = d.month + timedelta(days=random.randint(0, max(min(last_day, 24), 0)))
                ctx = ctx_asst if random.random() < 0.75 else ctx_owner
                method = random.choices(["cash", "bkash", "nagad"], weights=[75, 20, 5])[0]
                res = await fees.record_payment(
                    ctx, student_id=st.id, amount=amount, method=method, idempotency_key=f"seed:{d.id}",
                    trx_id=f"BK{random.randint(10**7, 10**8)}" if method != "cash" else None, due_ids=[d.id],
                )
                await db.execute(
                    update(Payment).where(Payment.id == res.payment.id)
                    .values(received_at=at_local(day, random.randint(9, 20), random.randint(0, 59)))
                )
                n_pay += 1
        print(f"Recorded {n_pay} payments.")

        # Attendance for the last 14 days (not today, so the demo can take it live).
        n_sessions = 0
        for batch, _ in batches:
            roster = list((await db.execute(
                select(Enrollment.student_id).where(Enrollment.batch_id == batch.id))).scalars())
            for back in range(14, 0, -1):
                d = today - timedelta(days=back)
                if weekday_code(d) not in batch.days:
                    continue
                sess = AttendanceSession(institution_id=inst_id, batch_id=batch.id, date=d, method="tap", taken_by=assistant.id)
                db.add(sess)
                await db.flush()
                db.add_all([
                    AttendanceRecord(institution_id=inst_id, session_id=sess.id, student_id=sid, date=d,
                                     status="absent" if random.random() < 0.08 else "present")
                    for sid in roster
                ])
                n_sessions += 1
        print(f"Created {n_sessions} attendance sessions.")

        # Turn SMS automations back on for live demo actions.
        inst.settings = fees_settings()
        await db.flush()
    print(f"\nDone. Owner: {OWNER_PHONE} · Assistant: {ASSISTANT_PHONE}")


def fees_settings() -> dict:
    from app.modules.identity.models import DEFAULT_SETTINGS

    return {**DEFAULT_SETTINGS, "bkash_number": "01711111111"}


if __name__ == "__main__":
    asyncio.run(main())
