import logging

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.modules.academics.router import router as academics_router
from app.modules.admissions.router import public as public_router
from app.modules.admissions.router import router as admissions_router
from app.modules.attendance.router import router as attendance_router
from app.modules.dashboards.router import router as dashboards_router
from app.modules.fees.router import router as fees_router
from app.modules.identity.router import router as identity_router
from app.modules.messaging.router import router as messaging_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

settings = get_settings()
app = FastAPI(title="BatchPilot API", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

v1 = APIRouter(prefix="/api/v1")
for r in (
    identity_router, academics_router, admissions_router, public_router, fees_router, attendance_router,
    messaging_router, dashboards_router,
):
    v1.include_router(r)
app.include_router(v1)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
