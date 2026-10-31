import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.fees.schemas import ReceiptOut


class LinkIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=300)
    batch_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    offer_kind: str | None = Field(default=None, pattern="^(percent|amount)$")
    offer_value: int | None = Field(default=None, ge=1)  # percent, or poisha
    offer_on: str = Field(default="admission", pattern="^(admission|fee)$")
    offer_until: date | None = None
    hold_days: int = Field(default=3, ge=1, le=30)

    @model_validator(mode="after")
    def _offer(self):
        if self.offer_kind and not self.offer_value:
            raise ValueError("offer_value is required with an offer")
        if self.offer_kind == "percent" and (self.offer_value or 0) > 100:
            raise ValueError("percent offer must be 1-100")
        return self


class LinkUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=300)
    batch_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=20)
    offer_kind: str | None = Field(default=None, pattern="^(percent|amount)$")
    offer_value: int | None = Field(default=None, ge=1)
    offer_on: str | None = Field(default=None, pattern="^(admission|fee)$")
    offer_until: date | None = None
    hold_days: int | None = Field(default=None, ge=1, le=30)
    is_active: bool | None = None


class LinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    note: str | None
    code: str
    batch_ids: list[uuid.UUID]
    offer_kind: str | None
    offer_value: int | None
    offer_on: str
    offer_until: date | None
    hold_days: int
    is_active: bool
    scan_count: int
    created_at: datetime
    pending: int = 0
    admitted: int = 0
    batch_names: list[str] = []


class SeatBatchOut(BaseModel):
    """A batch as the admission screens see it: schedule, fees and seats."""

    id: uuid.UUID
    name: str
    subject: str | None
    class_level: str | None
    days: list[str]
    start_time: time | None
    end_time: time | None
    location: str | None
    starts_on: date | None
    fee_plan: str
    monthly_fee: int
    course_fee: int | None
    installments: int
    admission_fee: int | None
    capacity: int | None
    students: int  # active
    held: int  # pending requests still holding a seat
    seats_left: int | None  # None = no limit


class OfferOut(BaseModel):
    kind: str
    value: int
    on: str
    until: date | None


class JoinPageOut(BaseModel):
    code: str
    title: str
    note: str | None
    institution_name: str
    teacher_name: str | None
    city: str | None
    is_open: bool
    offer: OfferOut | None
    hold_days: int
    bkash_number: str | None
    nagad_number: str | None
    batches: list[SeatBatchOut]
    my_requests: list["RequestOut"] = []


class ApplyIn(BaseModel):
    batch_id: uuid.UUID
    student_name: str = Field(min_length=1, max_length=120)
    guardian_name: str | None = Field(default=None, max_length=120)
    guardian_phone: str | None = None  # defaults to the logged-in phone
    student_phone: str | None = None
    school: str | None = Field(default=None, max_length=160)
    class_level: str | None = Field(default=None, max_length=60)
    pay_method: str = Field(default="cash", pattern="^(cash|bkash|nagad)$")
    trx_id: str | None = Field(default=None, max_length=60)


class RequestOut(BaseModel):
    id: uuid.UUID
    status: str
    batch_id: uuid.UUID
    batch_name: str
    link_title: str | None
    institution_name: str | None = None
    student_name: str
    student_phone: str | None
    guardian_name: str | None
    guardian_phone: str
    school: str | None
    class_level: str | None
    pay_method: str
    trx_id: str | None
    admission_fee: int  # what joining costs now, after the frozen offer
    first_fee: int
    offer_discount_admission: int
    offer_discount_fee: int
    hold_until: datetime
    created_at: datetime
    student_id: uuid.UUID | None
    student_code: str | None = None
    reject_reason: str | None


class PayNowIn(BaseModel):
    method: str = Field(pattern="^(cash|bkash|nagad|bank)$")
    amount: int = Field(gt=0, description="poisha")
    trx_id: str | None = Field(default=None, max_length=60)


class FeeChoiceIn(BaseModel):
    """The fee card of the admit screen: what this student pays, and what was paid on the spot."""

    charge_admission: bool = True  # False = waive (মাফ)
    admission_fee: int | None = Field(default=None, ge=0)  # None = the batch's admission fee
    admission_discount: int = Field(default=0, ge=0)  # on top of any QR offer
    fee_discount: int = Field(default=0, ge=0)
    custom_fee: int | None = Field(default=None, ge=0)
    discount_note: str | None = Field(default=None, max_length=200)
    payment: PayNowIn | None = None


class OfficeAdmissionIn(FeeChoiceIn):
    batch_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    guardian_phone: str
    guardian_name: str | None = Field(default=None, max_length=120)
    guardian_relation: str | None = Field(default=None, pattern="^(father|mother|other)$")
    phone: str | None = None
    school: str | None = Field(default=None, max_length=160)
    class_level: str | None = Field(default=None, max_length=60)
    force: bool = False  # create even if a likely duplicate exists, or the batch is full


class AdmitIn(FeeChoiceIn):
    force: bool = False


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class AdmittedRow(BaseModel):
    """One admission (at the desk or from a QR request) for the "ভর্তি হয়েছে" tab."""

    student_id: uuid.UUID
    student_code: str
    name: str
    class_level: str | None
    school: str | None
    guardian_name: str | None
    guardian_phone: str | None
    batch_name: str
    at: datetime
    paid: int  # taken on the spot; 0 = will pay later
    admission_fee: int  # charged at admission, after discount
    outstanding: int  # what the student owes now, all batches
    from_qr: bool
    admitted_by: str | None
    status: str  # active | left


class AdmissionOverview(BaseModel):
    """The numbers on top of the admissions page."""

    pending: int
    admitted_month: int
    admission_fees_month: int  # admission fees charged this month, after discounts
    free_seats: int
    seat_batches: int  # batches that have a seat limit
    scans: int  # all QR page views


class AdmittedOut(BaseModel):
    student_id: uuid.UUID
    name: str
    student_code: str
    batch_name: str
    guardian_phone: str | None
    charged: int  # all dues created at admission, after discounts
    outstanding: int
    receipt: ReceiptOut | None


class SiblingOut(BaseModel):
    id: uuid.UUID
    name: str
    student_code: str
    class_level: str | None
    batch_names: list[str]


class GuardianLookupOut(BaseModel):
    guardian_name: str | None
    school: str | None
    children: list[SiblingOut]


JoinPageOut.model_rebuild()
