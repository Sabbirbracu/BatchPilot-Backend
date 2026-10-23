import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OtpRequestIn(BaseModel):
    phone: str


class OtpRequestOut(BaseModel):
    phone: str
    expires_in: int
    dev_code: str | None = None  # only in local/dev


class OtpVerifyIn(BaseModel):
    phone: str
    code: str = Field(min_length=4, max_length=8)


class MembershipOut(BaseModel):
    institution_id: uuid.UUID
    institution_name: str
    role: str


class InstitutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    subject: str | None
    city: str | None
    slug: str | None
    fee_due_day: int
    plan: str
    plan_status: str
    trial_ends_at: datetime | None
    settings: dict


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone: str
    name: str | None
    language: str


class MeOut(BaseModel):
    user: UserOut
    memberships: list[MembershipOut]
    institution: InstitutionOut | None
    role: str | None
    portal: str | None  # teacher | staff | guardian | student; None until an institution is set up
    home: str  # where the app sends this user: /teacher, /staff, /guardian, /student or /setup
    permissions: list[str]


class InstitutionCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    subject: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    fee_due_day: int = Field(default=10, ge=1, le=28)
    owner_name: str | None = Field(default=None, max_length=120)


class InstitutionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    subject: str | None = None
    city: str | None = None
    fee_due_day: int | None = Field(default=None, ge=1, le=28)
    settings: dict | None = None


class SwitchIn(BaseModel):
    institution_id: uuid.UUID


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    language: str | None = Field(default=None, pattern="^(bn|en)$")


class MemberCreate(BaseModel):
    phone: str
    name: str | None = Field(default=None, max_length=120)
    role: str = Field(pattern="^(assistant|teacher)$")


class MemberUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(assistant|teacher)$")
    is_active: bool | None = None


class MemberOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str | None
    phone: str
    role: str
    is_active: bool
    created_at: datetime
