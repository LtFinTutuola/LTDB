"""
src/schemas/crm.py
------------------
Pydantic schemas for CRM customer onboarding, lifecycle, WhatsApp Flow webhooks,
transactions, and loyalty point ledger operations.
"""
from datetime import date as dt_date, datetime as dt_datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from src.models.crm import CustomerStatus, LoyaltyLedgerReason
from src.schemas.base import BaseSchema, BaseSchemaWithAudit
from src.schemas.excel_sales import ExcelSaleStatus


class CustomerOnboardingRequest(BaseModel):
    """Input payload for cashier-triggered WhatsApp onboarding."""

    phone_number: str = Field(..., min_length=1, description="Numero di cellulare del cliente")


class CustomerCreateInternal(BaseSchema):
    """Internal repository schema for creating a PENDING_ONBOARDING customer."""

    phone_number: str
    status: CustomerStatus = CustomerStatus.PENDING_ONBOARDING
    whatsapp_flow_token: str
    retention_expires_at: dt_datetime


class CustomerUpdateInternal(BaseSchema):
    """Internal repository schema for updating customer onboarding/lifecycle fields."""

    phone_number: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    birth_date: Optional[dt_date] = None
    address: Optional[str] = None
    status: Optional[CustomerStatus] = None
    terms_accepted_at: Optional[dt_datetime] = None
    consent_marketing: Optional[bool] = None
    consent_profiling: Optional[bool] = None
    consent_updated_at: Optional[dt_datetime] = None
    whatsapp_flow_token: Optional[str] = None
    last_purchase_date: Optional[dt_date] = None
    retention_expires_at: Optional[dt_datetime] = None


class CustomerOut(BaseSchemaWithAudit):
    """Full representation of a Customer record."""

    phone_number: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    birth_date: Optional[dt_date] = None
    address: Optional[str] = None
    status: CustomerStatus
    terms_accepted_at: Optional[dt_datetime] = None
    consent_marketing: bool = False
    consent_profiling: bool = False
    consent_updated_at: Optional[dt_datetime] = None
    whatsapp_flow_token: Optional[str] = None
    last_purchase_date: Optional[dt_date] = None
    retention_expires_at: Optional[dt_datetime] = None


class CustomerListItemOut(CustomerOut):
    """Customer summary item returned by GET /api/v1/customers."""

    points_balance: int = 0
    is_birthday_window_active: bool = False


class CustomerOnboardingResponse(BaseSchema):
    """Response returned by onboarding and resend endpoints."""

    customer: CustomerOut
    whatsapp_delivery: Dict[str, Any]
    is_resent: bool = False


class WhatsAppFlowCompletionPayload(BaseModel):
    """
    Payload submitted by the customer via WhatsApp Flow (or dev mock simulator)
    to complete onboarding and activate the CRM profile.
    """

    flow_token: Optional[str] = Field(
        None,
        description="Token univoco del Flow (opzionale per l'endpoint mock-complete-onboarding)",
    )
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    birth_date: dt_date
    address: str = Field(..., min_length=1, max_length=255)
    terms_accepted: bool
    consent_marketing: bool = False
    consent_profiling: bool = False


class LifecycleMaintenanceResult(BaseSchema):
    """Summary of automated 72h pending cleanup and 60-month retention expiration jobs."""

    purged_pending_count: int = 0
    expired_active_count: int = 0


# ---------------------------------------------------------------------------
# Phase 3: Loyalty Ledger, Transactions, and CRM Management Schemas
# ---------------------------------------------------------------------------


class LoyaltyLedgerEntryOut(BaseSchema):
    """Immutable loyalty point ledger entry representation."""

    id: str
    customer_id: str
    transaction_id: Optional[str] = None
    points_delta: int
    reason: LoyaltyLedgerReason
    created_at: dt_datetime


class CustomerKpiOut(BaseSchema):
    """Calculated KPIs for a customer profile."""

    points_balance: int = 0
    total_spent: Decimal = Decimal("0.00")
    transactions_count: int = 0
    avg_ticket: Decimal = Decimal("0.00")


class TransactionItemIn(BaseModel):
    """Input representation of a single sale/return line within a Transaction."""

    id: Optional[str] = Field(
        None,
        description="ID di una riga ExcelSale esistente (opzionale in PATCH per aggiornare la riga)",
    )
    starting_price: Decimal = Field(..., ge=0)
    selling_price: Decimal = Field(..., ge=0)
    excel_file_column: int = Field(..., ge=0, le=7, description="Indice colonna categoria 0..7 (Col B..I)")
    is_exchange: bool = False
    article_id: Optional[str] = None
    raw_article_code: Optional[str] = None


class TransactionSaleLineOut(BaseSchemaWithAudit):
    """Enriched representation of an ExcelSale line belonging to a Transaction."""

    date: dt_date
    excel_row_index: Optional[int] = None
    excel_file_column: Optional[int] = None
    status: ExcelSaleStatus
    starting_price: Optional[Decimal] = None
    selling_price: Optional[Decimal] = None
    is_exchange: bool = False
    raw_article_code: Optional[str] = None
    article_id: Optional[str] = None
    transaction_id: Optional[str] = None
    manual_review: bool = False

    # Enriched PIM metadata when article_id is reconciled
    article_name: Optional[str] = None
    brand_name: Optional[str] = None
    category_name: Optional[str] = None
    photo_url: Optional[str] = None


class TransactionOut(BaseSchemaWithAudit):
    """Full representation of a Transaction with economic aggregates, sale lines, and ledger entries."""

    date: dt_date
    customer_id: Optional[str] = None
    loyalty_discount_amount: Decimal = Decimal("0.00")
    discount_rule_code: Optional[str] = None

    # Economic aggregates (Approach 1)
    net_list_total: Decimal = Decimal("0.00")
    net_payable: Decimal = Decimal("0.00")
    base_cash_discount: Decimal = Decimal("0.00")

    # Loyalty points summary for this transaction
    points_earned_total: int = 0
    points_redeemed_total: int = 0
    points_net_delta: int = 0

    items: List[TransactionSaleLineOut] = Field(default_factory=list)
    ledger_entries: List[LoyaltyLedgerEntryOut] = Field(default_factory=list)


class TransactionCreateRequest(BaseModel):
    """Payload for POST /api/v1/transactions."""

    date: Optional[dt_date] = None
    customer_id: Optional[str] = None
    discount_rule_code: Optional[str] = None
    items: List[TransactionItemIn] = Field(..., min_length=1)


class TransactionUpdateRequest(BaseModel):
    """Payload for PATCH /api/v1/transactions/{transaction_id}."""

    date: Optional[dt_date] = None
    customer_id: Optional[str] = None
    discount_rule_code: Optional[str] = None
    items: Optional[List[TransactionItemIn]] = None


class TransactionFromExcelSalesRequest(BaseModel):
    """Payload for POST /api/v1/transactions/from-excel-sales."""

    excel_sale_ids: List[str] = Field(..., min_length=1)
    customer_id: Optional[str] = None
    discount_rule_code: Optional[str] = None


class CustomerDetailOut(CustomerOut):
    """Full customer profile returned by GET /api/v1/customers/{customer_id}."""

    points_balance: int = 0
    is_birthday_window_active: bool = False
    kpi: CustomerKpiOut = Field(default_factory=CustomerKpiOut)
    transactions: List[TransactionOut] = Field(default_factory=list)
    loyalty_ledger: List[LoyaltyLedgerEntryOut] = Field(default_factory=list)


class CustomerPatchRequest(BaseModel):
    """Payload for PATCH /api/v1/customers/{customer_id}."""

    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, min_length=1, max_length=100)
    birth_date: Optional[dt_date] = None
    address: Optional[str] = Field(None, min_length=1, max_length=255)
    phone_number: Optional[str] = Field(None, min_length=1)
    consent_marketing: Optional[bool] = None
    consent_profiling: Optional[bool] = None


class CustomerPointsAdjustmentRequest(BaseModel):
    """Payload for POST /api/v1/customers/{customer_id}/points-adjustment."""

    points_delta: int = Field(..., description="Variazione algebrica intera dei punti (+ / -)")


class CustomerMergeRequest(BaseModel):
    """Payload for POST /api/v1/customers/merge."""

    source_customer_id: str = Field(..., min_length=1)
    target_customer_id: str = Field(..., min_length=1)
