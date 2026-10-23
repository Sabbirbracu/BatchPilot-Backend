"""Money is integer poisha everywhere (৳800 = 80000). These helpers are for display only."""

from datetime import date

BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
BN_MONTHS = [
    "জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন",
    "জুলাই", "আগস্ট", "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর",
]


def taka(poisha: int) -> int:
    return poisha // 100


def format_taka(poisha: int, bangla: bool = True) -> str:
    """South Asian grouping: 310000 taka -> 3,10,000."""
    whole = abs(poisha) // 100
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    if poisha < 0:
        s = "-" + s
    return s.translate(BN_DIGITS) if bangla else s


def month_label(d: date, bangla: bool = True) -> str:
    return BN_MONTHS[d.month - 1] if bangla else d.strftime("%B")


def months_label(months: list[date], bangla: bool = True) -> str:
    return ", ".join(month_label(m, bangla) for m in sorted(set(months)))
