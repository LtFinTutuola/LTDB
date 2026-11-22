"""
src/routers/customers_router.py
-------------------------------
CRM Customers router:
- Phase 2: WhatsApp Onboarding, Resend Flow, and Mock Flow Completion.
- Phase 3: Customer Search, Full Profile Detail with KPIs, Demographic/Consent Patch,
  Admin Manual Points Adjustment, GDPR Anonymization, and Profile Merge.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from src.core.auth import get_current_user, require_cassa_or_admin
from src.core.database import get_db
from src.core.logger import get_logger
from src.core.request_context import AuthUser
from src.models.crm import CustomerStatus
from src.schemas.crm import (
    CustomerDetailOut,
    CustomerListItemOut,
    CustomerMergeRequest,
    CustomerOnboardingRequest,
    CustomerOnboardingResponse,
    CustomerOut,
    CustomerPatchRequest,
    CustomerPointsAdjustmentRequest,
    WhatsAppFlowCompletionPayload,
)
from src.services import customer_onboarding_service, loyalty_service
from src.services.customer_onboarding_service import (
    CustomerAlreadyExistsError,
    CustomerNotFoundError,
)

logger = get_logger()

router = APIRouter(prefix="/api/v1/customers", tags=["Customers"])


@router.get(
    "",
    response_model=List[CustomerListItemOut],
    status_code=status.HTTP_200_OK,
)
def list_customers(
    q: Optional[str] = Query(
        None,
        description="Ricerca per nome, cognome o numero di telefono (anche parziale)",
    ),
    status_filter: Optional[CustomerStatus] = Query(
        None,
        alias="status",
        description="Filtra per stato del profilo cliente",
    ),
    birthday_this_week: bool = Query(
        False,
        description="Se True, restituisce solo i clienti attivi nella finestra compleanno (+/- 3 giorni)",
    ),
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Searches and filters CRM customers, including dynamically calculated `points_balance`
    and `is_birthday_window_active` for each profile.
    """
    try:
        return loyalty_service.list_customers(
            db=db,
            q=q,
            status=status_filter,
            birthday_this_week=birthday_this_week,
        )
    except Exception as exc:
        logger.log_execution("customers_router", "list_customers", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


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
    "/merge",
    response_model=CustomerDetailOut,
    status_code=status.HTTP_200_OK,
)
def merge_customer_profiles(
    payload: CustomerMergeRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(require_cassa_or_admin),
):
    """
    Cassa/Admin operation: merges `source_customer_id` into `target_customer_id` by reassigning
    all transactions and loyalty ledger entries to the target profile and deleting the source profile.
    """
    try:
        return loyalty_service.merge_customers(db=db, payload=payload)
    except CustomerNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("customers_router", "merge_customer_profiles", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{customer_id}",
    response_model=CustomerDetailOut,
    status_code=status.HTTP_200_OK,
)
def get_customer_detail(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Returns full customer profile including demographics, GDPR consents, `points_balance`,
    KPIs (`total_spent`, `transactions_count`, `avg_ticket`), transactions with `excel_sales`
    lines and PIM metadata, and `loyalty_point_ledger` history.
    """
    try:
        return loyalty_service.get_customer_detail(db=db, customer_id=customer_id)
    except CustomerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.log_execution("customers_router", "get_customer_detail", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.patch(
    "/{customer_id}",
    response_model=CustomerDetailOut,
    status_code=status.HTTP_200_OK,
)
def update_customer_profile(
    customer_id: str,
    payload: CustomerPatchRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(require_cassa_or_admin),
):
    """
    Cassa/Admin operation: updates customer demographic data (`first_name`, `last_name`,
    `birth_date`, `address`, `phone_number`) and GDPR consent flags (`consent_marketing`,
    `consent_profiling`).
    """
    try:
        return loyalty_service.update_customer(
            db=db,
            customer_id=customer_id,
            payload=payload,
        )
    except CustomerNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except CustomerAlreadyExistsError as exc:
        db.rollback()
        existing_out = CustomerOut.model_validate(exc.customer).model_dump(mode="json")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "existing_customer": existing_out,
            },
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("customers_router", "update_customer_profile", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/{customer_id}/points-adjustment",
    response_model=CustomerDetailOut,
    status_code=status.HTTP_200_OK,
)
def adjust_customer_points(
    customer_id: str,
    payload: CustomerPointsAdjustmentRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(require_cassa_or_admin),
):
    """
    Cassa/Admin operation: inserts an immutable `ADMIN_ADJUSTMENT` entry into `loyalty_point_ledger`.
    """
    try:
        return loyalty_service.adjust_customer_points(
            db=db,
            customer_id=customer_id,
            points_delta=payload.points_delta,
        )
    except CustomerNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("customers_router", "adjust_customer_points", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/{customer_id}/anonymize",
    response_model=CustomerDetailOut,
    status_code=status.HTTP_200_OK,
)
def anonymize_customer_profile(
    customer_id: str,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(require_cassa_or_admin),
):
    """
    Cassa/Admin GDPR Right-to-be-Forgotten operation: anonymizes personal data,
    zeroes out any positive points balance via a closing ledger entry, and preserves
    associated `transactions` and `excel_sales`.
    """
    try:
        return loyalty_service.anonymize_customer(db=db, customer_id=customer_id)
    except CustomerNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("customers_router", "anonymize_customer_profile", "err", exc=exc)
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
