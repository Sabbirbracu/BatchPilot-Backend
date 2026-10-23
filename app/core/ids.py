import uuid

from uuid6 import uuid7 as _uuid7


def new_id() -> uuid.UUID:
    """UUID v7: time-ordered, so it indexes well and can be generated on the device."""
    return uuid.UUID(str(_uuid7()))
