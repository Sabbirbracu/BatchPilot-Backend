import uuid

from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy import select

from app.adapters.sms import send_otp_sms
from app.core.config import get_settings
from app.core.deps import DB, Auth, Tenant, client_ip
from app.core.errors import AppError, forbidden, not_found
from app.core.phone import normalize_bd_phone
from app.core.timeutil import now_utc
from app.modules.audit import service as audit
from app.modules.identity import service
from app.modules.identity.models import PORTAL_BY_ROLE, Membership, User
from app.modules.identity.schemas import (
    InstitutionCreate,
    InstitutionOut,
    InstitutionUpdate,
    MemberCreate,
    MemberOut,
    MemberUpdate,
    MeOut,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    ProfileUpdate,
    SwitchIn,
)

router = APIRouter(tags=["identity"])

ALLOWED_SETTINGS = {"receipt_sms", "absent_sms", "reminders_enabled", "daily_summary", "summary_time", "bkash_number"}


def _set_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=s.session_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/auth/otp/request", response_model=OtpRequestOut)
async def otp_request(body: OtpRequestIn, request: Request, db: DB, background: BackgroundTasks):
    phone = normalize_bd_phone(body.phone)
    code = await service.request_otp(db, phone, client_ip(request))
    background.add_task(send_otp_sms, phone, code)
    s = get_settings()
    return OtpRequestOut(
        phone=phone,
        expires_in=s.otp_ttl_minutes * 60,
        dev_code=code if (s.is_local and s.dev_otp_echo) else None,
    )


@router.post("/auth/otp/verify", response_model=MeOut)
async def otp_verify(body: OtpVerifyIn, request: Request, response: Response, db: DB):
    phone = normalize_bd_phone(body.phone)
    user = await service.verify_otp(db, phone, body.code)
    token, sess = await service.create_session(
        db, user, request.headers.get("user-agent"), client_ip(request)
    )
    await db.flush()
    _set_cookie(response, token)
    return await service.build_me(db, user, sess)


@router.post("/auth/logout", status_code=204)
async def logout(auth: Auth, response: Response):
    auth.session.revoked_at = now_utc()
    response.delete_cookie(get_settings().session_cookie_name, path="/")


@router.get("/auth/me", response_model=MeOut)
async def me(auth: Auth):
    return await service.build_me(auth.db, auth.user, auth.session)


@router.patch("/auth/me", response_model=MeOut)
async def update_me(body: ProfileUpdate, auth: Auth):
    if body.name is not None:
        auth.user.name = body.name.strip() or None
    if body.language is not None:
        auth.user.language = body.language
    return await service.build_me(auth.db, auth.user, auth.session)


@router.post("/auth/switch", response_model=MeOut)
async def switch_institution(body: SwitchIn, auth: Auth):
    m = (
        await auth.db.execute(
            select(Membership).where(
                Membership.user_id == auth.user.id,
                Membership.institution_id == body.institution_id,
                Membership.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not m:
        raise forbidden()
    auth.session.active_institution_id = body.institution_id
    auth.session.portal = PORTAL_BY_ROLE[m.role]
    return await service.build_me(auth.db, auth.user, auth.session)


# ---- Institution (setup wizard step 1 + settings) ----


@router.post("/institutions", response_model=MeOut, status_code=201)
async def create_institution(body: InstitutionCreate, auth: Auth):
    await service.create_institution(auth.db, auth.user, auth.session, body, auth.ip)
    return await service.build_me(auth.db, auth.user, auth.session)


@router.get("/institutions/current", response_model=InstitutionOut)
async def get_current(t: Tenant):
    return InstitutionOut.model_validate(t.institution)


@router.patch("/institutions/current", response_model=InstitutionOut)
async def update_current(body: InstitutionUpdate, t: Tenant):
    t.require("settings.manage")
    inst = t.institution
    before = audit.snapshot(inst, ["name", "subject", "city", "fee_due_day", "settings"])
    for field in ("name", "subject", "city", "fee_due_day"):
        value = getattr(body, field)
        if value is not None:
            setattr(inst, field, value)
    if body.settings is not None:
        unknown = set(body.settings) - ALLOWED_SETTINGS
        if unknown:
            raise AppError("invalid_setting", f"Unknown setting: {', '.join(sorted(unknown))}", status_code=422)
        inst.settings = {**inst.settings, **body.settings}
    await audit.record(
        t.db,
        institution_id=inst.id,
        actor_id=t.user.id,
        action="institution.updated",
        entity="institution",
        entity_id=inst.id,
        before=before,
        after=audit.snapshot(inst, ["name", "subject", "city", "fee_due_day", "settings"]),
        ip=t.ip,
    )
    await t.db.flush()
    return InstitutionOut.model_validate(inst)


# ---- Team members (owner adds assistants by phone; they log in with OTP) ----


async def _member_rows(t: Tenant) -> list[MemberOut]:
    rows = (
        await t.db.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.institution_id == t.institution_id)
            .order_by(Membership.created_at)
        )
    ).all()
    return [
        MemberOut(
            id=m.id, user_id=u.id, name=u.name, phone=u.phone, role=m.role, is_active=m.is_active, created_at=m.created_at
        )
        for m, u in rows
    ]


@router.get("/members", response_model=list[MemberOut])
async def list_members(t: Tenant):
    t.require("members.manage")
    return await _member_rows(t)


@router.post("/members", response_model=list[MemberOut], status_code=201)
async def add_member(body: MemberCreate, t: Tenant):
    t.require("members.manage")
    phone = normalize_bd_phone(body.phone)
    user = await service.find_or_create_user(t.db, phone, body.name)
    existing = (
        await t.db.execute(
            select(Membership).where(Membership.user_id == user.id, Membership.institution_id == t.institution_id)
        )
    ).scalar_one_or_none()
    if existing:
        if existing.role == "owner":
            raise AppError("already_owner", "This number is the owner", "এই নম্বরটি মালিকের", 409, "phone")
        existing.role, existing.is_active = body.role, True
    else:
        t.db.add(Membership(user_id=user.id, institution_id=t.institution_id, role=body.role))
    await audit.record(
        t.db,
        institution_id=t.institution_id,
        actor_id=t.user.id,
        action="member.added",
        entity="membership",
        after={"phone": phone, "role": body.role},
        ip=t.ip,
    )
    await t.db.flush()
    return await _member_rows(t)


@router.patch("/members/{member_id}", response_model=list[MemberOut])
async def update_member(member_id: uuid.UUID, body: MemberUpdate, t: Tenant):
    t.require("members.manage")
    m = await t.db.get(Membership, member_id)
    if not m or m.institution_id != t.institution_id:
        raise not_found("Member")
    if m.role == "owner":
        raise AppError("owner_locked", "The owner can't be changed here", "মালিক পরিবর্তন করা যাবে না", 409)
    before = {"role": m.role, "is_active": m.is_active}
    if body.role is not None:
        m.role = body.role
    if body.is_active is not None:
        m.is_active = body.is_active
        if not body.is_active:
            await service.revoke_user_sessions_for(t.db, m.user_id, t.institution_id)
    await audit.record(
        t.db,
        institution_id=t.institution_id,
        actor_id=t.user.id,
        action="member.updated",
        entity="membership",
        entity_id=m.id,
        before=before,
        after={"role": m.role, "is_active": m.is_active},
        ip=t.ip,
    )
    await t.db.flush()
    return await _member_rows(t)
