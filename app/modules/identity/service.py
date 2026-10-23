import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import OpsSessionLocal, SessionLocal, set_context
from app.core.errors import AppError
from app.core.ids import new_id
from app.core.permissions import permissions_for
from app.core.security import hash_otp, hash_token, new_otp_code, new_session_token
from app.core.timeutil import now_utc
from app.modules.academics.models import Student
from app.modules.audit import service as audit
from app.modules.identity.models import PORTAL_BY_ROLE, Institution, Membership, OtpRequest, Session, User
from app.modules.identity.schemas import (
    InstitutionCreate,
    InstitutionOut,
    MembershipOut,
    MeOut,
    UserOut,
)
from app.modules.messaging.service import ensure_default_templates

PORTAL_HOME = {"teacher": "/teacher", "staff": "/staff", "guardian": "/guardian", "student": "/student"}


async def request_otp(db: AsyncSession, phone: str, ip: str | None) -> str:
    s = get_settings()
    now = now_utc()
    recent = await db.scalar(
        select(func.count())
        .select_from(OtpRequest)
        .where(OtpRequest.phone == phone, OtpRequest.created_at > now - timedelta(minutes=15))
    )
    if (recent or 0) >= s.otp_max_per_window:
        raise AppError(
            "otp_rate_limited",
            "Too many codes requested. Try again in 15 minutes.",
            "অনেকবার কোড চাওয়া হয়েছে। ১৫ মিনিট পরে আবার চেষ্টা করুন।",
            429,
        )
    code = new_otp_code(s.otp_length)
    db.add(
        OtpRequest(
            phone=phone,
            code_hash=hash_otp(phone, code),
            expires_at=now + timedelta(minutes=s.otp_ttl_minutes),
            ip=ip,
            created_at=now,
        )
    )
    # Sent by the router after commit (adapters.sms.send_otp_sms); paid from platform credits.
    return code


async def verify_otp(db: AsyncSession, phone: str, code: str) -> User:
    s = get_settings()
    now = now_utc()
    otp = (
        await db.execute(
            select(OtpRequest)
            .where(OtpRequest.phone == phone, OtpRequest.consumed_at.is_(None))
            .order_by(OtpRequest.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not otp or otp.expires_at < now:
        raise AppError("otp_expired", "Code expired. Request a new one.", "কোডের মেয়াদ শেষ। নতুন কোড নিন।", 400, "code")
    if otp.attempts >= s.otp_max_attempts:
        raise AppError("otp_locked", "Too many wrong attempts. Request a new code.", "অনেকবার ভুল হয়েছে। নতুন কোড নিন।", 429, "code")
    if otp.code_hash != hash_otp(phone, code.strip()):
        # Count the wrong attempt in its own transaction: the request's one rolls back on error.
        async with SessionLocal() as side, side.begin():
            await side.execute(
                update(OtpRequest).where(OtpRequest.id == otp.id).values(attempts=OtpRequest.attempts + 1)
            )
        raise AppError("otp_invalid", "Wrong code", "কোড সঠিক নয়", 400, "code")
    consumed = await db.execute(
        update(OtpRequest)
        .where(OtpRequest.id == otp.id, OtpRequest.consumed_at.is_(None))
        .values(consumed_at=now)
        .returning(OtpRequest.id)
    )
    if consumed.scalar_one_or_none() is None:  # used by a parallel request
        raise AppError("otp_expired", "Code already used. Request a new one.", "কোড আগেই ব্যবহার হয়েছে। নতুন কোড নিন।", 400, "code")

    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if not user:
        user = User(phone=phone)
        db.add(user)
        await db.flush()
    return user


async def family_links(phone: str) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """(institution_id, student_id, 'guardian'|'student') for every active student linked to this phone.

    A guardian isn't a member of any institution, so finding them is a cross-tenant read: it runs on
    the ops connection and returns only ids, which the request then uses under normal RLS."""
    async with OpsSessionLocal() as ops:
        rows = (
            await ops.execute(
                select(Student.institution_id, Student.id, Student.guardian_phone == phone)
                .where(
                    or_(Student.guardian_phone == phone, Student.phone == phone),
                    Student.status == "active",
                    Student.deleted_at.is_(None),
                )
                .order_by(Student.created_at)
            )
        ).all()
    return [(inst, sid, "guardian" if is_guardian else "student") for inst, sid, is_guardian in rows]


async def pick_portal(db: AsyncSession, user: User) -> tuple[uuid.UUID | None, str | None]:
    """Where a fresh login lands: own institution first (teacher/staff), then guardian, then student."""
    await set_context(db, user_id=user.id)
    m = (
        await db.execute(
            select(Membership)
            .where(Membership.user_id == user.id, Membership.is_active.is_(True))
            .order_by(Membership.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if m:
        return m.institution_id, PORTAL_BY_ROLE[m.role]
    links = await family_links(user.phone)
    for kind in ("guardian", "student"):
        hit = next((inst for inst, _, k in links if k == kind), None)
        if hit:
            return hit, kind
    return None, None  # new teacher: goes to the setup wizard


async def create_session(
    db: AsyncSession, user: User, user_agent: str | None, ip: str | None
) -> tuple[str, Session]:
    s = get_settings()
    now = now_utc()
    institution_id, portal = await pick_portal(db, user)
    token = new_session_token()
    sess = Session(
        user_id=user.id,
        token_hash=hash_token(token),
        active_institution_id=institution_id,
        portal=portal,
        user_agent=(user_agent or "")[:300],
        ip=ip,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=s.session_days),
    )
    db.add(sess)
    return token, sess


async def build_me(db: AsyncSession, user: User, sess: Session) -> MeOut:
    await set_context(db, user_id=user.id)
    rows = (
        await db.execute(
            select(Membership, Institution)
            .join(Institution, Institution.id == Membership.institution_id)
            .where(Membership.user_id == user.id, Membership.is_active.is_(True))
            .order_by(Membership.created_at)
        )
    ).all()
    memberships = [
        MembershipOut(institution_id=i.id, institution_name=i.name, role=m.role) for m, i in rows
    ]
    portal = sess.portal
    institution, role = None, None
    if portal in ("guardian", "student") and sess.active_institution_id:
        await set_context(db, institution_id=sess.active_institution_id)
        institution = await db.get(Institution, sess.active_institution_id)
    else:
        active = next(((m, i) for m, i in rows if i.id == sess.active_institution_id), None)
        if active:
            role, institution = active[0].role, active[1]
            portal = PORTAL_BY_ROLE[role]
        else:
            portal = None
    return MeOut(
        user=UserOut.model_validate(user),
        memberships=memberships,
        institution=InstitutionOut.model_validate(institution) if institution else None,
        role=role,
        portal=portal,
        home=PORTAL_HOME.get(portal or "", "/setup"),
        permissions=permissions_for(role),
    )


async def create_institution(
    db: AsyncSession, user: User, sess: Session, data: InstitutionCreate, ip: str | None
) -> Institution:
    inst_id = new_id()
    # The new row must pass the RLS WITH CHECK, so switch context to it first.
    await set_context(db, user_id=user.id, institution_id=inst_id)
    inst = Institution(
        id=inst_id,
        name=data.name.strip(),
        subject=data.subject,
        city=data.city,
        fee_due_day=data.fee_due_day,
    )
    db.add(inst)
    await db.flush()
    db.add(Membership(user_id=user.id, institution_id=inst_id, role="owner"))
    if data.owner_name and not user.name:
        user.name = data.owner_name.strip()
    await ensure_default_templates(db, inst_id)
    sess.active_institution_id, sess.portal = inst_id, "teacher"
    await audit.record(
        db,
        institution_id=inst_id,
        actor_id=user.id,
        action="institution.created",
        entity="institution",
        entity_id=inst_id,
        after={"name": inst.name, "fee_due_day": inst.fee_due_day},
        ip=ip,
    )
    await db.flush()
    return inst


async def find_or_create_user(db: AsyncSession, phone: str, name: str | None) -> User:
    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if not user:
        user = User(phone=phone, name=name)
        db.add(user)
        await db.flush()
    elif name and not user.name:
        user.name = name
    return user


async def revoke_user_sessions_for(db: AsyncSession, user_id: uuid.UUID, institution_id: uuid.UUID) -> None:
    """Removing a member logs them out of that institution immediately."""
    await db.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.active_institution_id == institution_id)
        .values(revoked_at=now_utc())
    )
