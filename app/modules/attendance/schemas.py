import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class RecordIn(BaseModel):
    student_id: uuid.UUID
    status: str = Field(pattern="^(present|absent|late)$")


class SyncIn(BaseModel):
    id: uuid.UUID | None = None  # client-generated session id (offline-safe retries)
    batch_id: uuid.UUID
    date: date
    method: str = Field(default="tap", pattern="^(tap|qr|fingerprint)$")
    records: list[RecordIn]


class RosterItem(BaseModel):
    student_id: uuid.UUID
    name: str
    student_code: str
    status: str  # stored status, or 'present' by default when not taken yet
    guardian_phone: str | None
    due_months: int


class RosterOut(BaseModel):
    batch_id: uuid.UUID
    batch_name: str
    date: date
    session_id: uuid.UUID | None
    taken_by_name: str | None
    taken_at: datetime | None
    present: int
    absent: int
    items: list[RosterItem]


class SyncOut(BaseModel):
    session_id: uuid.UUID
    present: int
    absent: int
    sms_queued: int


class TodayBatch(BaseModel):
    batch_id: uuid.UUID
    name: str
    subject: str | None = None
    start_time: str | None
    end_time: str | None = None
    scheduled_today: bool
    student_count: int
    taken: bool
    present: int
    absent: int


class StudentAttendanceRow(BaseModel):
    date: date
    batch_name: str
    status: str


class StudentAttendanceOut(BaseModel):
    sessions: int
    present: int
    absent: int
    rate: float | None
    recent: list[StudentAttendanceRow]
