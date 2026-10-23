"""SMS gateway adapter (System Design §11): one interface, swappable providers.

- console          dev default: logs the message, sends nothing
- android_gateway  free: "SMS Gateway for Android" (sms-gate.app) sends from your own phone's SIM.
                   Install the app, enable Cloud server, copy its username/password into .env.
- bulksmsbd        paid Bangladeshi gateway (bulksmsbd.net) with masking/sender ID, for production.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx

from app.core.config import get_settings

log = logging.getLogger("batchpilot.sms")


@dataclass
class SmsResult:
    ok: bool
    provider: str
    ref: str | None = None
    error: str | None = None


class SmsProvider(Protocol):
    name: str

    async def send(self, to: str, body: str) -> SmsResult: ...


class ConsoleProvider:
    name = "console"

    async def send(self, to: str, body: str) -> SmsResult:
        log.warning("SMS (console) to %s: %s", to, body)
        return SmsResult(True, self.name, ref="console")


class AndroidGatewayProvider:
    name = "android_gateway"

    def __init__(self, url: str, username: str, password: str):
        self.url, self.auth = url, (username, password)

    async def send(self, to: str, body: str) -> SmsResult:
        payload = {"textMessage": {"text": body}, "phoneNumbers": [to]}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self.url, json=payload, auth=self.auth)
            if r.status_code >= 300:
                return SmsResult(False, self.name, error=f"HTTP {r.status_code}: {r.text[:200]}")
            return SmsResult(True, self.name, ref=str(r.json().get("id", "")))
        except httpx.HTTPError as e:
            return SmsResult(False, self.name, error=str(e))


class BulkSmsBdProvider:
    name = "bulksmsbd"
    URL = "https://bulksmsbd.net/api/smsapi"

    def __init__(self, api_key: str, sender_id: str):
        self.api_key, self.sender_id = api_key, sender_id

    async def send(self, to: str, body: str) -> SmsResult:
        form = {"api_key": self.api_key, "senderid": self.sender_id, "number": to.lstrip("+"), "message": body}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self.URL, data=form)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return SmsResult(False, self.name, error=str(e))
        code = str(data.get("response_code"))
        if code == "202":  # "SMS Submitted Successfully"
            return SmsResult(True, self.name, ref=str(data.get("message_id") or ""))
        return SmsResult(False, self.name, error=f"{code}: {data.get('error_message') or data.get('success_message')}")


@lru_cache
def get_sms_provider() -> SmsProvider:
    s = get_settings()
    if s.sms_provider == "android_gateway":
        if not (s.sms_gateway_username and s.sms_gateway_password):
            raise RuntimeError("SMS_PROVIDER=android_gateway needs SMS_GATEWAY_USERNAME and SMS_GATEWAY_PASSWORD")
        return AndroidGatewayProvider(s.sms_gateway_url, s.sms_gateway_username, s.sms_gateway_password)
    if s.sms_provider == "bulksmsbd":
        if not (s.bulksmsbd_api_key and s.bulksmsbd_sender_id):
            raise RuntimeError("SMS_PROVIDER=bulksmsbd needs BULKSMSBD_API_KEY and BULKSMSBD_SENDER_ID")
        return BulkSmsBdProvider(s.bulksmsbd_api_key, s.bulksmsbd_sender_id)
    return ConsoleProvider()


async def send_otp_sms(phone: str, code: str) -> None:
    """Runs after the response is sent; a failed OTP SMS is logged and the user can ask again."""
    body = f"BatchPilot লগইন কোড: {code}। ৫ মিনিট বৈধ। কাউকে জানাবেন না।"
    result = await get_sms_provider().send(phone, body)
    if not result.ok:
        log.error("OTP SMS to %s failed via %s: %s", phone, result.provider, result.error)
