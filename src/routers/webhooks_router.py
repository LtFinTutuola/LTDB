"""
src/routers/webhooks_router.py
------------------------------
Public webhook endpoints for Meta WhatsApp Business Platform (WABA).
Authenticated via hub.verify_token (GET challenge) and X-Hub-Signature-256 HMAC (POST).
"""
from typing import Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.logger import get_logger
from src.schemas.crm import CustomerOut, WhatsAppQuickReplyResponse
from src.services import customer_onboarding_service
from src.services.customer_onboarding_service import (
    CustomerNotFoundError,
    WebhookVerificationError,
)

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


@router.post(
    "/whatsapp",
    response_model=Union[CustomerOut, WhatsAppQuickReplyResponse],
    status_code=status.HTTP_200_OK,
)
async def receive_whatsapp_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    db: Session = Depends(get_db),
):
    """
    Receives WhatsApp webhook events signed with `X-Hub-Signature-256`:
    - WhatsApp Flow onboarding completion: validates data (18+ age, GDPR terms) and activates customer.
    - Quick Reply button tap: generates a 6-digit redemption OTP on the customer and sends it in chat.
    """
    raw_body = await request.body()
    try:
        customer_onboarding_service.verify_webhook_signature(raw_body, x_hub_signature_256)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    try:
        event_type, parsed_payload = customer_onboarding_service.extract_webhook_event_from_body(
            raw_body
        )
        if event_type == "quick_reply":
            return customer_onboarding_service.process_quick_reply_redemption_request(
                db=db,
                phone_number=parsed_payload.phone_number,
                discount_rule_code=parsed_payload.discount_rule_code,
            )
        customer = customer_onboarding_service.complete_onboarding_from_flow(db, parsed_payload)
        return customer
    except CustomerNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
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
