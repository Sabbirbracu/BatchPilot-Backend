"""Default Bangla templates created for every new institution. Owners can edit them later."""

DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "receipt": {
        "bn": "{student}-এর {months} মাসের ৳{amount} গ্রহণ করা হয়েছে। ধন্যবাদ — {institution}",
        "en": "Received Tk {amount} for {student} ({months}). Thank you — {institution}",
    },
    "absent": {
        "bn": "{student} আজ ({date}) {batch} ক্লাসে অনুপস্থিত ছিল। — {institution}",
        "en": "{student} was absent today ({date}) from {batch}. — {institution}",
    },
    "reminder_1": {
        "bn": "সম্মানিত অভিভাবক, {student}-এর {months} মাসের ফি ৳{amount} বাকি আছে। অনুগ্রহ করে পরিশোধ করুন। — {institution}",
        "en": "Dear guardian, Tk {amount} fee for {student} ({months}) is due. Please pay. — {institution}",
    },
    "reminder_2": {
        "bn": "{student}-এর ফি ৳{amount} এখনো বাকি। বিকাশ: {bkash}, রেফারেন্সে রোল {roll} লিখুন। — {institution}",
        "en": "Tk {amount} fee for {student} is still due. bKash: {bkash}, reference roll {roll}. — {institution}",
    },
    "reminder_3": {
        "bn": "{student}-এর ফি ৳{amount} ({months}) বাকি। অফিসে যোগাযোগ করুন। — {institution}",
        "en": "Tk {amount} fee for {student} ({months}) is overdue. Please contact the office. — {institution}",
    },
    "welcome": {
        "bn": "{student}-কে {institution}-এ স্বাগতম। উপস্থিতি ও ফির খবর এই নম্বরে পাবেন।",
        "en": "Welcome {student} to {institution}. Attendance and fee updates will come to this number.",
    },
    "notice": {
        "bn": "{message} — {institution}",
        "en": "{message} — {institution}",
    },
    "summary": {
        "bn": "আজ আদায় ৳{today} ({today_count} জন) · এই মাস ৳{month} · বকেয়া ৳{due} ({due_count} জন) · আজ অনুপস্থিত {absent} জন",
        "en": "Today Tk {today} ({today_count}) · This month Tk {month} · Due Tk {due} ({due_count}) · Absent today {absent}",
    },
}

# Receipts are triggered by the guardian's own action, so they skip the daily cap.
CAP_EXEMPT = {"receipt"}
PRIORITY = {"receipt": 1, "absent": 3, "welcome": 3, "notice": 4}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(body: str, values: dict) -> str:
    return body.format_map(_SafeDict({k: "" if v is None else v for k, v in values.items()}))


def count_segments(body: str) -> int:
    """GSM-7 fits 160 chars (153 per part); any Bangla means UCS-2: 70 chars (67 per part)."""
    unicode = any(ord(c) > 127 for c in body)
    single, multi = (70, 67) if unicode else (160, 153)
    n = len(body)
    if n <= single:
        return 1
    return -(-n // multi)
