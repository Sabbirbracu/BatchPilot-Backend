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
        "bn": "{student}-এর ফি ৳{amount} এখনো বাকি। বিকাশ: {bkash}, রেফারেন্সে আইডি {student_id} লিখুন। — {institution}",
        "en": "Tk {amount} fee for {student} is still due. bKash: {bkash}, reference ID {student_id}. — {institution}",
    },
    "reminder_3": {
        "bn": "{student}-এর ফি ৳{amount} ({months}) বাকি। অফিসে যোগাযোগ করুন। — {institution}",
        "en": "Tk {amount} fee for {student} ({months}) is overdue. Please contact the office. — {institution}",
    },
    "welcome": {
        "bn": "{student}-কে {institution}-এ স্বাগতম। উপস্থিতি ও ফির খবর এই নম্বরে পাবেন।",
        "en": "Welcome {student} to {institution}. Attendance and fee updates will come to this number.",
    },
    "admission_request": {
        "bn": "{student}-এর ভর্তির আবেদন পেয়েছি ({batch})। {hold} তারিখের মধ্যে ফি দিলে সিট নিশ্চিত হবে। — {institution}",
        "en": "Got {student}'s admission request ({batch}). Pay by {hold} to confirm the seat. — {institution}",
    },
    "admitted": {
        "bn": "{student}-এর ভর্তি সম্পন্ন: {batch}, আইডি {student_id}। বাকি ৳{due}। হাজিরা ও ফির খবর এই নম্বরে পাবেন। — {institution}",
        "en": "{student} is admitted to {batch}, ID {student_id}. Due Tk {due}. Updates come to this number. — {institution}",
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

# Starter notice templates ("নোটিশের জন্য"). Keys start with "n_"; owners can add, edit and delete them.
DEFAULT_NOTICE_TEMPLATES: dict[str, dict[str, str]] = {
    "n_holiday": {"name": "ছুটির নোটিশ", "bn": "{date} পর্যন্ত ক্লাস বন্ধ থাকবে। আবার ক্লাস শুরু হলে জানানো হবে।"},
    "n_exam": {"name": "পরীক্ষার নোটিশ", "bn": "{date}-এ {batch}-এর মডেল টেস্ট হবে। {student} যেন সময়মতো আসে।"},
    "n_meeting": {"name": "অভিভাবক সভা", "bn": "{date}-এ {batch}-এর অভিভাবক সভা, অবশ্যই আসবেন।"},
}
NOTICE_PREFIX = "n_"

# Receipts are triggered by the guardian's own action, so they skip the daily cap.
CAP_EXEMPT = {"receipt"}
PRIORITY = {"receipt": 1, "admitted": 2, "admission_request": 2, "absent": 3, "welcome": 3, "notice": 4}


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
