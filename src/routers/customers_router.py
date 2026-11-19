"""
src/routers/customers_router.py
-------------------------------
CRM Customers router (Phase 2: WhatsApp Onboarding, Resend Flow, and Mock Flow Completion).
"""
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from src.core.auth import get_current_user
from src.core.database import get_db
from src.core.logger import get_logger
from src.core.request_context import AuthUser
from src.schemas.crm import (
    CustomerOnboardingRequest,
    CustomerOnboardingResponse,
    CustomerOut,
    WhatsAppFlowCompletionPayload,
)
from src.services import customer_onboarding_service
from src.services.customer_onboarding_service import (
    CustomerAlreadyExistsError,
    CustomerNotFoundError,
)

logger = get_logger()

router = APIRouter(prefix="/api/v1/customers", tags=["Customers"])


@router.post(
    "/onboarding",
    response_model=CustomerOnboardingResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_customer_onboarding(
    payload: CustomerOnboardingRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Initiates customer onboarding via WhatsApp Flow from the cash register:
    - 201 Created: new PENDING_ONBOARDING customer created and Flow sent.
    - 200 OK: existing PENDING_ONBOARDING customer token regenerated, TTL extended +72h, Flow resent.
    - 409 Conflict: phone number already belongs to an ACTIVE or INACTIVE_EXPIRED customer.
    """
    try:
        customer, delivery, is_resent = customer_onboarding_service.initiate_onboarding(
            db=db,
            raw_phone=payload.phone_number,
        )
        response.status_code = status.HTTP_200_OK if is_resent else status.HTTP_201_CREATED
        return CustomerOnboardingResponse(
            customer=CustomerOut.model_validate(customer),
            whatsapp_delivery=delivery,
            is_resent=is_resent,
        )
    except CustomerAlreadyExistsError as exc:
        existing_out = CustomerOut.model_validate(exc.customer).model_dump(mode="json")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "existing_customer": existing_out,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("customers_router", "start_customer_onboarding", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/{customer_id}/resend-whatsapp",
    response_model=CustomerOnboardingResponse,
    status_code=status.HTTP_200_OK,
)
def resend_customer_onboarding_whatsapp(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Re-sends the WhatsApp Flow to a PENDING_ONBOARDING customer and resets their 72h TTL.
    """
    try:
        customer, delivery = customer_onboarding_service.resend_onboarding_whatsapp(
            db=db,
            customer_id=customer_id,
        )
        return CustomerOnboardingResponse(
            customer=CustomerOut.model_validate(customer),
            whatsapp_delivery=delivery,
            is_resent=True,
        )
    except CustomerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution(
            "customers_router", "resend_customer_onboarding_whatsapp", "err", exc=exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/{customer_id}/mock-complete-onboarding",
    response_model=CustomerOut,
    status_code=status.HTTP_200_OK,
)
def mock_complete_customer_onboarding(
    customer_id: str,
    payload: WhatsAppFlowCompletionPayload,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Development/Test helper with Mock WABA: simulates the customer submitting the WhatsApp Flow
    by internally generating and HMAC-signing the webhook payload and invoking the webhook logic.
    """
    try:
        customer = customer_onboarding_service.simulate_mock_onboarding_completion(
            db=db,
            customer_id=customer_id,
            payload=payload,
        )
        return customer
    except CustomerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution(
            "customers_router", "mock_complete_customer_onboarding", "err", exc=exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
