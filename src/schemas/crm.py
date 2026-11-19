"""
src/schemas/crm.py
------------------
Pydantic schemas for CRM customer onboarding, lifecycle, and WhatsApp Flow webhooks.
"""
from datetime import date, datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from src.models.crm import CustomerStatus
from src.schemas.base import BaseSchema, BaseSchemaWithAudit


class CustomerOnboardingRequest(BaseModel):
    """Input payload for cashier-triggered WhatsApp onboarding."""

    phone_number: str = Field(..., min_length=1, description="Numero di cellulare del cliente")


class CustomerCreateInternal(BaseSchema):
    """Internal repository schema for creating a PENDING_ONBOARDING customer."""

    phone_number: str
    status: CustomerStatus = CustomerStatus.PENDING_ONBOARDING
    whatsapp_flow_token: str
    retention_expires_at: datetime


class CustomerUpdateInternal(BaseSchema):
    """Internal repository schema for updating customer onboarding/lifecycle fields."""

    phone_number: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    birth_date: Optional[date] = None
    address: Optional[str] = None
    status: Optional[CustomerStatus] = None
    terms_accepted_at: Optional[datetime] = None
    consent_marketing: Optional[bool] = None
    consent_profiling: Optional[bool] = None
    consent_updated_at: Optional[datetime] = None
    whatsapp_flow_token: Optional[str] = None
    last_purchase_date: Optional[date] = None
    retention_expires_at: Optional[datetime] = None


class CustomerOut(BaseSchemaWithAudit):
    """Full representation of a Customer record."""

    phone_number: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    birth_date: Optional[date] = None
    address: Optional[str] = None
    status: CustomerStatus
    terms_accepted_at: Optional[datetime] = None
    consent_marketing: bool = False
    consent_profiling: bool = False
    consent_updated_at: Optional[datetime] = None
    whatsapp_flow_token: Optional[str] = None
    last_purchase_date: Optional[date] = None
    retention_expires_at: Optional[datetime] = None


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
    birth_date: date
    address: str = Field(..., min_length=1, max_length=255)
    terms_accepted: bool
    consent_marketing: bool = False
    consent_profiling: bool = False


class LifecycleMaintenanceResult(BaseSchema):
    """Summary of automated 72h pending cleanup and 60-month retention expiration jobs."""

    purged_pending_count: int = 0
    expired_active_count: int = 0
