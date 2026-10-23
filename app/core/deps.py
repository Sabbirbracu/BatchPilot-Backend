"""Request context: who is calling, which institution, which role.

Every authenticated request runs in one transaction with app.user_id / app.institution_id
set locally, so Row-Level Security only ever sees that institution's rows."""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db, set_context
from app.core.errors import AppError, forbidden, unauthorized
from app.core.permissions import can
from app.core.security import hash_token
from app.core.timeutil import now_utc
from app.modules.academics.models import Student
from app.modules.identity.models import Institution, Membership, Session, User

DB = Annotated[AsyncSession, Depends(get_db, scope="function")]


@dataclass
class AuthContext:
    db: AsyncSession
    user: User
    session: Session
    ip: str | None


@dataclass
class TenantContext(AuthContext):
    institution: Institution
    membership: Membership

    @property
    def institution_id(self) -> uuid.UUID:
        return self.institution.id

    @property
    def role(self) -> str:
        return self.membership.role

    def can(self, action: str) -> bool:
        return can(self.role, action)

    def require(self, action: str) -> None:
        if not self.can(action):
            raise forbidden()


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def get_auth(request: Request, db: DB) -> AuthContext:
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise unauthorized()
    now = now_utc()
    sess = (
        await db.execute(select(Session).where(Session.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if not sess or sess.revoked_at or sess.expires_at < now:
        raise unauthorized()
    user = await db.get(User, sess.user_id)
    if not user:
        raise unauthorized()
    # 30-day rolling session; avoid a write on every request.
    if now - sess.last_seen_at > timedelta(minutes=5):
        sess.last_seen_at = now
        sess.expires_at = now + timedelta(days=settings.session_days)
    await set_context(db, user_id=user.id)
    return AuthContext(db=db, user=user, session=sess, ip=client_ip(request))


Auth = Annotated[AuthContext, Depends(get_auth)]


async def get_tenant(auth: Auth) -> TenantContext:
    inst_id = auth.session.active_institution_id
    if not inst_id:
        raise AppError("no_institution", "Set up your institution first", "আগে প্রতিষ্ঠান সেটআপ করুন", 409)
    db = auth.db
    await set_context(db, institution_id=inst_id)
    membership = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == auth.user.id,
                Membership.institution_id == inst_id,
                Membership.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    institution = await db.get(Institution, inst_id)
    if not membership or not institution or institution.deleted_at:
        raise forbidden()
    return TenantContext(**auth.__dict__, institution=institution, membership=membership)


Tenant = Annotated[TenantContext, Depends(get_tenant)]


@dataclass
class FamilyContext(AuthContext):
    """A guardian or student session: sees only the students linked to their own phone."""

    institution: Institution
    kind: str  # guardian | student
    students: list[Student]


async def get_family(auth: Auth) -> FamilyContext:
    kind, inst_id = auth.session.portal, auth.session.active_institution_id
    if kind not in ("guardian", "student") or not inst_id:
        raise forbidden()
    db = auth.db
    await set_context(db, institution_id=inst_id)
    institution = await db.get(Institution, inst_id)
    if not institution or institution.deleted_at:
        raise forbidden()
    match = Student.guardian_phone if kind == "guardian" else Student.phone
    students = list(
        (
            await db.execute(
                select(Student)
                .where(match == auth.user.phone, Student.status == "active", Student.deleted_at.is_(None))
                .order_by(Student.created_at)
            )
        ).scalars()
    )
    return FamilyContext(**auth.__dict__, institution=institution, kind=kind, students=students)


Family = Annotated[FamilyContext, Depends(get_family)]
