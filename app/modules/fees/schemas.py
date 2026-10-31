import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class PaymentIn(BaseModel):
    student_id: uuid.UUID
    amount: int = Field(gt=0, description="poisha")
    method: str = Field(pattern="^(cash|bkash|nagad|bank)$")
    trx_id: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=300)
    due_ids: list[uuid.UUID] | None = None  # pick specific months first (optional)
    send_receipt: bool = True  # untick to skip the receipt SMS for this payment


class AllocationOut(BaseModel):
    fee_due_id: uuid.UUID
    month: date
    kind: str = "monthly"  # monthly | admission | course | installment
    label: str | None = None
    batch_id: uuid.UUID
    amount: int
    due_status: str


class ReceiptOut(BaseModel):
    payment_id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    student_code: str
    amount: int
    method: str
    trx_id: str | None
    received_at: datetime
    received_by_name: str | None
    allocations: list[AllocationOut]
    outstanding_after: int
    sms_status: str | None
    replayed: bool


class ReverseIn(BaseModel):
    reason: str = Field(min_length=2, max_length=300)


class DiscountIn(BaseModel):
    discount: int = Field(ge=0, description="poisha")
    reason: str = Field(min_length=2, max_length=200)


class GenerateIn(BaseModel):
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")


class RemindIn(BaseModel):
    student_ids: list[uuid.UUID] | None = None
    batch_id: uuid.UUID | None = None


class DueOut(BaseModel):
    id: uuid.UUID
    month: date
    kind: str = "monthly"  # monthly | admission | course | installment
    label: str | None = None
    batch_id: uuid.UUID
    batch_name: str
    amount: int
    discount: int
    discount_reason: str | None
    paid_amount: int
    outstanding: int
    status: str
    due_date: date


class PaymentOut(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    student_code: str
    kind: str
    amount: int
    method: str
    trx_id: str | None
    note: str | None
    received_by: uuid.UUID
    received_by_name: str | None
    received_at: datetime
    reversed: bool
    reversal_reason: str | None
    months: list[date]
    batch_names: list[str] = []


class FeeOverview(BaseModel):
    """Numbers for the fee page header: one month's collection plus today's and all open dues."""

    month: date
    collected: int
    billed: int  # this month's fees after discounts: the collection target
    today_amount: int
    today_count: int
    today_receipts: int  # receipt SMS queued today
    total_due: int
    due_students: int
    by_method: dict[str, int]


class LedgerOut(BaseModel):
    student_id: uuid.UUID
    outstanding: int
    advance: int
    dues: list[DueOut]
    payments: list[PaymentOut]


class DueStudentRow(BaseModel):
    student_id: uuid.UUID
    name: str
    student_code: str
    guardian_phone: str | None
    batch_names: list[str]
    outstanding: int
    months: int
    oldest_month: date
    last_payment_at: datetime | None


class DuesPage(BaseModel):
    items: list[DueStudentRow]
    total_students: int
    total_outstanding: int


class CashSummary(BaseModel):
    date: date
    cash: int
    bkash: int
    nagad: int
    bank: int
    count: int
    unhanded_cash: int
