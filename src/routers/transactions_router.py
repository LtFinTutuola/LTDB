"""
src/routers/transactions_router.py
----------------------------------
CRM Transactions & Active Sales router (Phase 3):
- POST /api/v1/transactions: Create a new Transaction from LTDB UI with one or more sale/return
  lines (`excel_row_index = None`), optional immediate WMS reconciliation, and atomic loyalty
  discount spreading + ledger movements.
- PATCH /api/v1/transactions/{transaction_id}: Modify an existing Transaction (customer,
  discount rule, date, or items), recalculate spread prices and `PRICE_CORRECTION` ledger
  delta, and automatically reset `manual_review = False` on all transaction lines.
- POST /api/v1/transactions/from-excel-sales: Group one or more existing orphan `excel_sales`
  rows (`transaction_id == None`) into a new Transaction with optional customer and discount rule.
- GET /api/v1/transactions/{transaction_id}: Retrieve full transaction details.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.core.auth import get_current_user
from src.core.database import get_db
from src.core.logger import get_logger
from src.core.request_context import AuthUser
from src.schemas.crm import (
    TransactionCreateRequest,
    TransactionFromExcelSalesRequest,
    TransactionOut,
    TransactionUpdateRequest,
)
from src.services import loyalty_service
from src.services.customer_onboarding_service import CustomerNotFoundError
from src.services.loyalty_service import TransactionNotFoundError

logger = get_logger()

router = APIRouter(prefix="/api/v1/transactions", tags=["Transactions"])


@router.post(
    "",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_transaction(
    payload: TransactionCreateRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Creates a new Transaction from LTDB UI with one or more sale/return items:
    - Each row in `excel_sales` is created with `excel_row_index = None`.
    - If `article_id` is provided, immediately executes WMS reconciliation (`SOLD`/`AVAILABLE` + `ArticleMovement`).
    - Executes proportional loyalty discount spreading and immutable `loyalty_point_ledger` calculation.
    """
    try:
        return loyalty_service.create_transaction(db=db, payload=payload)
    except CustomerNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("transactions_router", "create_transaction", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/from-excel-sales",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_transaction_from_excel_sales(
    payload: TransactionFromExcelSalesRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Groups one or more existing `excel_sales` rows (`transaction_id == None`) into a new Transaction,
    associating them with an optional `customer_id` and `discount_rule_code`.
    """
    try:
        return loyalty_service.create_transaction_from_excel_sales(db=db, payload=payload)
    except CustomerNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution(
            "transactions_router", "create_transaction_from_excel_sales", "err", exc=exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{transaction_id}",
    response_model=TransactionOut,
    status_code=status.HTTP_200_OK,
)
def get_transaction(
    transaction_id: str,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Returns full details of a Transaction including economic aggregates, `excel_sales` lines,
    and associated `loyalty_point_ledger` movements.
    """
    try:
        return loyalty_service.get_transaction_detail(db=db, transaction_id=transaction_id)
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.log_execution("transactions_router", "get_transaction", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.patch(
    "/{transaction_id}",
    response_model=TransactionOut,
    status_code=status.HTTP_200_OK,
)
def update_transaction(
    transaction_id: str,
    payload: TransactionUpdateRequest,
    db: Session = Depends(get_db),
    current_user: AuthUser = Depends(get_current_user),
):
    """
    Modifies an existing Transaction:
    - Recalculates proportional discount spreading and appends `PRICE_CORRECTION` to `loyalty_point_ledger`.
    - Resets `manual_review = False` on all `excel_sales` lines of the transaction.
    """
    try:
        return loyalty_service.update_transaction(
            db=db,
            transaction_id=transaction_id,
            payload=payload,
        )
    except (TransactionNotFoundError, CustomerNotFoundError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.log_execution("transactions_router", "update_transaction", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
