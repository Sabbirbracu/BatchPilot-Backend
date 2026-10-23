import hashlib
import hmac
import secrets

from app.core.config import get_settings


def _key() -> bytes:
    return get_settings().secret_key.encode()


def hash_token(token: str) -> str:
    return hmac.new(_key(), token.encode(), hashlib.sha256).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_otp_code(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_otp(phone: str, code: str) -> str:
    return hmac.new(_key(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


def new_qr_token() -> str:
    return secrets.token_urlsafe(12)
