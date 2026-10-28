import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.academics.models import WEEKDAYS


class BatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    subject: str | None = Field(default=None, max_length=120)
    class_level: str | None = Field(default=None, max_length=60)
    days: list[str] = Field(default_factory=list)
    start_time: time | None = None
    end_time: time | None = None
    monthly_fee: int = Field(ge=0, description="poisha")
    admission_fee: int | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d not in WEEKDAYS]
        if bad:
            raise ValueError(f"unknown day(s): {', '.join(bad)}")
        return sorted(set(v), key=WEEKDAYS.index)


class BatchUpdate(BatchIn):
    name: str | None = Field(default=None, min_length=1, max_length=120)  # type: ignore[assignment]
    monthly_fee: int | None = Field(default=None, ge=0)  # type: ignore[assignment]
    days: list[str] | None = None  # type: ignore[assignment]
    version: int | None = None


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    subject: str | None
    class_level: str | None
    days: list[str]
    start_time: time | None
    end_time: time | None
    monthly_fee: int
    admission_fee: int | None
    capacity: int | None
    archived_at: datetime | None
    version: int
    # Live stats
    student_count: int = 0
    month_collected: int = 0
    month_due: int = 0
    today_present: int | None = None
    today_absent: int | None = None
    due_students: int = 0  # students with something left on this month's fee
    month_attendance: int | None = None  # % present/late this month, None before the first session
    last_month_attendance: int | None = None


class BatchMemberOut(BaseModel):
    """One row of a batch's student table: this month's fee for this batch and attendance here."""

    student_id: uuid.UUID
    roll_no: str
    name: str
    guardian_phone: str | None
    month_amount: int = 0  # fee after discount, poisha; 0 when no due exists yet
    month_paid: int = 0
    month_status: str | None = None  # due | partial | paid | waived
    outstanding: int = 0  # all open dues of the student, any batch
    attendance: int | None = None  # % this month in this batch


class EnrollmentIn(BaseModel):
    batch_id: uuid.UUID
    custom_fee: int | None = Field(default=None, ge=0, description="poisha; overrides batch fee")
    discount_note: str | None = Field(default=None, max_length=200)
    start_month: date | None = None


class StudentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = None
    guardian_name: str | None = Field(default=None, max_length=120)
    guardian_phone: str | None = None
    school: str | None = Field(default=None, max_length=160)
    class_level: str | None = Field(default=None, max_length=60)
    joined_at: date | None = None
    note: str | None = None
    roll_no: str | None = Field(default=None, max_length=30)
    enrollments: list[EnrollmentIn] = Field(default_factory=list)
    force: bool = False  # create even if a likely duplicate exists


class StudentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = None
    guardian_name: str | None = None
    guardian_phone: str | None = None
    school: str | None = None
    class_level: str | None = None
    note: str | None = None
    version: int | None = None


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    batch_id: uuid.UUID
    batch_name: str
    monthly_fee: int  # effective fee
    custom_fee: int | None
    discount_note: str | None
    start_month: date
    end_month: date | None


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    roll_no: str
    name: str
    phone: str | None
    guardian_name: str | None
    guardian_phone: str | None
    guardian_verified_at: datetime | None
    school: str | None
    class_level: str | None
    status: str
    joined_at: date
    left_at: date | None
    qr_token: str
    note: str | None
    version: int
    outstanding: int = 0
    due_months: int = 0
    partial: bool = False  # part of an open due is already paid
    batch_names: list[str] = []
    month_attendance: int | None = None  # % this month across all batches


class StudentSummary(BaseModel):
    active: int
    joined_this_month: int
    with_due: int
    left: int


class StudentPage(BaseModel):
    items: list[StudentOut]
    next_cursor: str | None
    total: int


class LeaveIn(BaseModel):
    reason: str | None = None
