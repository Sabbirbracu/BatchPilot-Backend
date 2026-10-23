import phonenumbers

from app.core.errors import AppError


def normalize_bd_phone(raw: str, field: str = "phone") -> str:
    """Normalise to E.164 (+8801XXXXXXXXX). Accepts 01XXXXXXXXX, 8801…, +8801…."""
    try:
        num = phonenumbers.parse(raw.strip(), "BD")
    except phonenumbers.NumberParseException:
        num = None
    if not num or not phonenumbers.is_valid_number(num):
        raise AppError(
            "invalid_phone",
            "Enter a valid Bangladeshi mobile number",
            "সঠিক মোবাইল নম্বর দিন (01XXXXXXXXX)",
            422,
            field,
        )
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def normalize_optional(raw: str | None, field: str) -> str | None:
    if raw is None or not raw.strip():
        return None
    return normalize_bd_phone(raw, field)
