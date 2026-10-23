"""Imports every module's models so Alembic and SQLAlchemy see the full schema."""

from app.modules.academics.models import Batch, Enrollment, Student  # noqa: F401
from app.modules.attendance.models import AttendanceRecord, AttendanceSession  # noqa: F401
from app.modules.audit.models import AuditLog  # noqa: F401
from app.modules.automation.models import AutomationRun  # noqa: F401
from app.modules.fees.models import CashHandover, FeeDue, Payment, PaymentAllocation  # noqa: F401
from app.modules.identity.models import Institution, Membership, OtpRequest, Session, User  # noqa: F401
from app.modules.messaging.models import MessageTemplate, OutboxMessage  # noqa: F401
