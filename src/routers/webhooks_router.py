"""
src/routers/webhooks_router.py
------------------------------
Public webhook endpoints for Meta WhatsApp Business Platform (WABA).
Authenticated via hub.verify_token (GET challenge) and X-Hub-Signature-256 HMAC (POST).
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.logger import get_logger
from src.schemas.crm import CustomerOut
from src.services import customer_onboarding_service
from src.services.customer_onboarding_service import WebhookVerificationError

logger = get_logger()

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


@router.get("/whatsapp", response_class=PlainTextResponse, status_code=status.HTTP_200_OK)
def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    """
    Standard Meta WABA webhook verification endpoint (`hub.challenge`).
    """
    try:
        challenge = customer_onboarding_service.verify_webhook_challenge(
            mode=hub_mode,
            verify_token=hub_verify_token,
            challenge=hub_challenge,
        )
        return PlainTextResponse(content=challenge, status_code=status.HTTP_200_OK)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


@router.post("/whatsapp", response_model=CustomerOut, status_code=status.HTTP_200_OK)
async def receive_whatsapp_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    db: Session = Depends(get_db),
):
    """
    Receives WhatsApp Flow onboarding completion events signed with `X-Hub-Signature-256`,
    validates customer data (18+ age, GDPR terms), normalizes fields, and activates the customer.
    """
    raw_body = await request.body()
    try:
        customer_onboarding_service.verify_webhook_signature(raw_body, x_hub_signature_256)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    try:
        flow_payload = customer_onboarding_service.extract_flow_payload_from_webhook_body(raw_body)
        customer = customer_onboarding_service.complete_onboarding_from_flow(db, flow_payload)
        return customer
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("webhooks_router", "receive_whatsapp_webhook", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
