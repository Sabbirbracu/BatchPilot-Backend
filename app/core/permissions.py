"""Role-based access, enforced in one place: can(role, action). RLS is the second layer."""

PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        "reports.view_all",
        "batch.manage",
        "student.manage",
        "payment.record",
        "payment.reverse_any",
        "due.discount",
        "fees.generate",
        "attendance.take",
        "message.send_notice",
        "message.edit_templates",
        "members.manage",
        "settings.manage",
        "audit.view",
    },
    "assistant": {
        "student.manage",
        "payment.record",
        "attendance.take",
        "message.send_notice",
    },
    "teacher": {
        "attendance.take",
    },
}


def can(role: str | None, action: str) -> bool:
    return bool(role) and action in PERMISSIONS.get(role, set())


def permissions_for(role: str | None) -> list[str]:
    return sorted(PERMISSIONS.get(role or "", set()))
