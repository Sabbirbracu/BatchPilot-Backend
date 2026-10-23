"""One error shape for the whole API: { code, message_bn, message_en, field? }."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message_en: str,
        message_bn: str | None = None,
        status_code: int = 400,
        field: str | None = None,
    ):
        self.code = code
        self.message_en = message_en
        self.message_bn = message_bn or message_en
        self.status_code = status_code
        self.field = field

    def to_dict(self) -> dict:
        body = {"code": self.code, "message_bn": self.message_bn, "message_en": self.message_en}
        if self.field:
            body["field"] = self.field
        return body


def not_found(entity: str = "Record") -> AppError:
    return AppError("not_found", f"{entity} not found", "খুঁজে পাওয়া যায়নি", 404)


def forbidden() -> AppError:
    return AppError("forbidden", "You don't have permission for this", "এই কাজের অনুমতি নেই", 403)


def unauthorized() -> AppError:
    return AppError("unauthorized", "Please log in", "অনুগ্রহ করে লগইন করুন", 401)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
        msg = first.get("msg", "Invalid input")
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message_en": msg,
                "message_bn": "তথ্য সঠিক নয়, আবার দেখুন",
                "field": ".".join(loc) or None,
            },
        )
