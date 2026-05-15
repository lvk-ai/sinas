"""Public, unauthenticated instance info for SDK/client discovery."""

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.auth import InfoResponse

router = APIRouter()


@router.get("/info", response_model=InfoResponse)
async def get_info() -> InfoResponse:
    """
    Return instance-level configuration that clients (custom frontends, JS/Python
    SDKs, the official console) need before login. Unauthenticated by design.
    """
    return InfoResponse(
        auth_mode=settings.auth_mode,  # type: ignore[arg-type]
        version="1.0.0",
        features={
            "clickhouse": bool(settings.clickhouse_host),
            "smtp": bool(settings.smtp_host),
        },
    )
