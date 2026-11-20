"""
src/services/customer_onboarding_service.py
-------------------------------------------
Business logic for CRM customer WhatsApp onboarding, webhook verification,
flow completion/activation, and automated lifecycle maintenance (72h & 60 months).
"""
import calendar
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.logger import get_logger
from src.models.crm import Customer, CustomerStatus
from src.repositories.crm_repo import crm_customer_repo
from src.schemas.crm import LifecycleMaintenanceResult, WhatsAppFlowCompletionPayload
from src.services.whatsapp_service import get_whatsapp_provider, mock_whatsapp_provider

logger = get_logger()


class CustomerAlreadyExistsError(Exception):
    """Raised when onboarding is requested for a phone number already ACTIVE or INACTIVE_EXPIRED."""

    def __init__(self, customer: Customer) -> None:
        self.customer = customer
        super().__init__(
            f"Esiste già un profilo cliente ({customer.status.value}) associato al numero {customer.phone_number}"
        )


class CustomerNotFoundError(Exception):
    """Raised when a customer ID is not found in the database."""

    pass


class WebhookVerificationError(Exception):
    """Raised when webhook challenge token or HMAC signature validation fails."""

    pass


def _utc_now_naive() -> datetime:
    """Returns current UTC time as a naive datetime (matching SQLite DateTime columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_months(base_dt: datetime, months: int) -> datetime:
    """Adds a calendar number of months to base_dt, clamping day to month end (e.g. Feb 29 -> Feb 28)."""
    total_month = (base_dt.month - 1) + months
    target_year = base_dt.year + (total_month // 12)
    target_month = (total_month % 12) + 1
    max_day = calendar.monthrange(target_year, target_month)[1]
    target_day = min(base_dt.day, max_day)
    return base_dt.replace(year=target_year, month=target_month, day=target_day)


def normalize_phone_number(raw_phone: str) -> str:
    """
    Sterilizes a phone number string:
    - Removes spaces, hyphens, dots, parentheses.
    - Strips Italian international prefix (+39 or 0039).
    - Validates that the result is a numeric string of 9 to 15 digits.
    """
    if not raw_phone or not isinstance(raw_phone, str):
        raise ValueError("Il numero di telefono è obbligatorio")

    # Remove common formatting characters
    cleaned = re.sub(r"[\s\-\.\(\)]+", "", raw_phone.strip())

    # Strip Italian country prefix (+39 or 0039)
    if cleaned.startswith("+39"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("0039"):
        cleaned = cleaned[4:]
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:]

    if not cleaned.isdigit() or not (9 <= len(cleaned) <= 15):
        raise ValueError(
            "Formato numero di telefono non valido: inserire un numero composto da 9 a 15 cifre"
        )

    return cleaned


def normalize_title_case(raw_text: str, field_label: str) -> str:
    """Collapses whitespace and normalizes a string to Title Case."""
    if not raw_text or not isinstance(raw_text, str):
        raise ValueError(f"Il campo '{field_label}' è obbligatorio")
    collapsed = " ".join(raw_text.strip().split())
    if not collapsed:
        raise ValueError(f"Il campo '{field_label}' non può essere vuoto")
    return collapsed.title()


def initiate_onboarding(
    db: Session,
    raw_phone: str,
    now: Optional[datetime] = None,
) -> Tuple[Customer, Dict[str, Any], bool]:
    """
    Initiates or re-sends WhatsApp onboarding from the cash register:
    - If phone belongs to ACTIVE or INACTIVE_EXPIRED -> raises CustomerAlreadyExistsError (409).
    - If phone belongs to PENDING_ONBOARDING -> regenerates token, extends TTL +72h, resends Flow (200).
    - If phone is new -> creates PENDING_ONBOARDING record with TTL +72h, sends Flow (201).
    """
    current_dt = now or _utc_now_naive()
    phone_number = normalize_phone_number(raw_phone)
    ttl_delta = timedelta(hours=settings.WHATSAPP_ONBOARDING_TTL_HOURS)
    provider = get_whatsapp_provider()

    existing = crm_customer_repo.get_by_phone_number(db, phone_number)
    if existing is not None:
        if existing.status in (CustomerStatus.ACTIVE, CustomerStatus.INACTIVE_EXPIRED):
            logger.log_execution(
                "customer_onboarding_service",
                "onboarding_conflict",
                "warning",
                phone_number=phone_number,
                existing_customer_id=existing.id,
                existing_status=existing.status.value,
            )
            raise CustomerAlreadyExistsError(existing)

        if existing.status == CustomerStatus.PENDING_ONBOARDING:
            new_token = secrets.token_urlsafe(32)
            existing.whatsapp_flow_token = new_token
            existing.retention_expires_at = current_dt + ttl_delta
            db.add(existing)
            db.commit()
            db.refresh(existing)

            delivery = provider.send_onboarding_flow(phone_number, new_token)
            logger.log_execution(
                "customer_onboarding_service",
                "onboarding_resent_existing_pending",
                "ok",
                customer_id=existing.id,
                phone_number=phone_number,
            )
            return existing, delivery, True

    # Create new PENDING_ONBOARDING customer
    flow_token = secrets.token_urlsafe(32)
    new_customer = Customer(
        phone_number=phone_number,
        status=CustomerStatus.PENDING_ONBOARDING,
        whatsapp_flow_token=flow_token,
        retention_expires_at=current_dt + ttl_delta,
    )
    db.add(new_customer)
    db.commit()
    db.refresh(new_customer)

    delivery = provider.send_onboarding_flow(phone_number, flow_token)
    logger.log_execution(
        "customer_onboarding_service",
        "onboarding_created",
        "ok",
        customer_id=new_customer.id,
        phone_number=phone_number,
    )
    return new_customer, delivery, False


def resend_onboarding_whatsapp(
    db: Session,
    customer_id: str,
    now: Optional[datetime] = None,
) -> Tuple[Customer, Dict[str, Any]]:
    """
    Re-sends the WhatsApp onboarding Flow to an existing PENDING_ONBOARDING customer
    and resets their 72-hour expiration window.
    """
    current_dt = now or _utc_now_naive()
    customer = crm_customer_repo.get(db, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Cliente '{customer_id}' non trovato")

    if customer.status != CustomerStatus.PENDING_ONBOARDING:
        raise ValueError(
            f"Impossibile re-inviare il modulo WhatsApp: il cliente è in stato '{customer.status.value}'"
        )

    if not customer.phone_number:
        raise ValueError("Il cliente non possiede un numero di telefono valido")

    new_token = secrets.token_urlsafe(32)
    customer.whatsapp_flow_token = new_token
    customer.retention_expires_at = current_dt + timedelta(
        hours=settings.WHATSAPP_ONBOARDING_TTL_HOURS
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    provider = get_whatsapp_provider()
    delivery = provider.send_onboarding_flow(customer.phone_number, new_token)

    logger.log_execution(
        "customer_onboarding_service",
        "resend_onboarding_whatsapp",
        "ok",
        customer_id=customer.id,
        phone_number=customer.phone_number,
    )
    return customer, delivery


def verify_webhook_challenge(
    mode: Optional[str],
    verify_token: Optional[str],
    challenge: Optional[str],
) -> str:
    """
    Validates Meta's GET webhook verification challenge (hub.mode, hub.verify_token, hub.challenge).
    """
    expected_token = settings.WHATSAPP_VERIFY_TOKEN
    if (
        mode != "subscribe"
        or not verify_token
        or not challenge
        or not hmac.compare_digest(verify_token, expected_token)
    ):
        logger.log_security(
            "customer_onboarding_service",
            "webhook_challenge_failed",
            "warning",
            mode=mode,
        )
        raise WebhookVerificationError("Verifica webhook fallita: verify_token o mode non validi")

    logger.log_execution("customer_onboarding_service", "webhook_challenge_verified", "ok")
    return challenge


def verify_webhook_signature(raw_body: bytes, signature_header: Optional[str]) -> None:
    """
    Validates X-Hub-Signature-256 (`sha256=<hmac>`) on the raw webhook request body
    using WHATSAPP_WEBHOOK_SECRET.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        logger.log_security(
            "customer_onboarding_service",
            "webhook_signature_missing",
            "warning",
        )
        raise WebhookVerificationError("Header X-Hub-Signature-256 mancante o malformato")

    provided_digest = signature_header.split("sha256=", 1)[1].strip()
    expected_digest = hmac.new(
        settings.WHATSAPP_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(provided_digest, expected_digest):
        logger.log_security(
            "customer_onboarding_service",
            "webhook_signature_invalid",
            "warning",
        )
        raise WebhookVerificationError("Firma HMAC X-Hub-Signature-256 non valida")


def extract_flow_payload_from_webhook_body(raw_body: bytes) -> WhatsAppFlowCompletionPayload:
    """
    Parses the incoming webhook JSON body.
    Supports both direct flat flow payloads and Meta Cloud API's nested
    `entry[0].changes[0].value` envelope format.
    """
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Payload JSON webhook non valido: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Il payload del webhook deve essere un oggetto JSON")

    # Unwrap Meta Cloud API envelope if present
    if "entry" in data and isinstance(data["entry"], list) and data["entry"]:
        first_entry = data["entry"][0]
        if isinstance(first_entry, dict) and "changes" in first_entry and first_entry["changes"]:
            first_change = first_entry["changes"][0]
            if isinstance(first_change, dict) and isinstance(first_change.get("value"), dict):
                value_obj = first_change["value"]
                if "flow_response" in value_obj and isinstance(value_obj["flow_response"], dict):
                    data = value_obj["flow_response"]
                else:
                    data = value_obj

    try:
        return WhatsAppFlowCompletionPayload(**data)
    except Exception as exc:
        raise ValueError(f"Dati del modulo WhatsApp Flow non validi: {exc}") from exc


def complete_onboarding_from_flow(
    db: Session,
    payload: WhatsAppFlowCompletionPayload,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
) -> Customer:
    """
    Validates WhatsApp Flow form data and activates a PENDING_ONBOARDING customer:
    - Verifies flow_token matches a PENDING_ONBOARDING customer.
    - Verifies terms_accepted == True.
    - Verifies customer age >= 18 years.
    - Normalizes first_name, last_name, address in Title Case.
    - Sets status = ACTIVE, records GDPR timestamps/flags, and computes 60-month retention.
    """
    current_dt = now or _utc_now_naive()
    current_date = today or current_dt.date()

    if not payload.flow_token:
        raise ValueError("Token del modulo WhatsApp Flow (flow_token) mancante")

    customer = crm_customer_repo.get_by_flow_token(db, payload.flow_token)
    if not customer or customer.status != CustomerStatus.PENDING_ONBOARDING:
        raise ValueError("Token WhatsApp Flow non valido o cliente non in attesa di onboarding")

    if not payload.terms_accepted:
        raise ValueError(
            "È obbligatorio accettare il regolamento fedeltà e l'informativa privacy"
        )

    # Validate age >= 18 years
    bdate = payload.birth_date
    age = (
        current_date.year
        - bdate.year
        - ((current_date.month, current_date.day) < (bdate.month, bdate.day))
    )
    if age < 18:
        raise ValueError(
            "Registrazione rifiutata: il programma fedeltà è riservato ai clienti maggiorenni (età >= 18 anni)"
        )

    first_name = normalize_title_case(payload.first_name, "first_name")
    last_name = normalize_title_case(payload.last_name, "last_name")
    address = normalize_title_case(payload.address, "address")

    # Determine retention anchor: last_purchase_date (or latest associated transaction date) vs now
    purchase_date = customer.last_purchase_date
    if purchase_date is None:
        latest_tx_date = crm_customer_repo.get_latest_transaction_date(db, customer.id)
        if latest_tx_date is not None:
            purchase_date = latest_tx_date
            customer.last_purchase_date = latest_tx_date

    if purchase_date is not None:
        anchor_dt = datetime(
            purchase_date.year,
            purchase_date.month,
            purchase_date.day,
            current_dt.hour,
            current_dt.minute,
            current_dt.second,
        )
    else:
        anchor_dt = current_dt

    customer.first_name = first_name
    customer.last_name = last_name
    customer.birth_date = bdate
    customer.address = address
    customer.status = CustomerStatus.ACTIVE
    customer.terms_accepted_at = current_dt
    customer.consent_marketing = bool(payload.consent_marketing)
    customer.consent_profiling = bool(payload.consent_profiling)
    customer.consent_updated_at = current_dt
    customer.whatsapp_flow_token = None
    customer.retention_expires_at = _add_months(anchor_dt, settings.WHATSAPP_RETENTION_MONTHS)

    db.add(customer)
    db.flush()

    # Retroactive Birthday Bonus calculation for same-day transactions made while PENDING_ONBOARDING
    from src.services.loyalty_service import credit_retroactive_birthday_bonus_on_activation

    credit_retroactive_birthday_bonus_on_activation(
        db=db,
        customer=customer,
        activation_date=current_date,
        now=current_dt,
    )

    db.commit()
    db.refresh(customer)

    logger.log_execution(
        "customer_onboarding_service",
        "customer_activated",
        "ok",
        customer_id=customer.id,
        phone_number=customer.phone_number,
        consent_marketing=customer.consent_marketing,
        consent_profiling=customer.consent_profiling,
    )
    return customer


def simulate_mock_onboarding_completion(
    db: Session,
    customer_id: str,
    payload: WhatsAppFlowCompletionPayload,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
) -> Customer:
    """
    Dev/Test helper that simulates a customer completing the WhatsApp Flow:
    1. Fetches the PENDING_ONBOARDING customer and their active flow_token.
    2. Generates and signs the webhook payload via MockWhatsAppProvider.
    3. Executes HMAC signature verification and webhook flow completion end-to-end.
    """
    customer = crm_customer_repo.get(db, customer_id)
    if not customer:
        raise CustomerNotFoundError(f"Cliente '{customer_id}' non trovato")

    if customer.status != CustomerStatus.PENDING_ONBOARDING or not customer.whatsapp_flow_token:
        raise ValueError("Il cliente non è in stato PENDING_ONBOARDING o non ha un flow_token attivo")

    simulated = mock_whatsapp_provider.simulate_flow_completion(
        flow_token=customer.whatsapp_flow_token,
        payload=payload.model_dump(),
    )

    verify_webhook_signature(simulated["raw_body"], simulated["signature"])
    parsed_payload = extract_flow_payload_from_webhook_body(simulated["raw_body"])
    return complete_onboarding_from_flow(db, parsed_payload, now=now, today=today)


def run_customer_lifecycle_maintenance(
    db: Session,
    now: Optional[datetime] = None,
) -> LifecycleMaintenanceResult:
    """
    Periodic maintenance job for customer lifecycle & GDPR compliance:
    1. 72h Expiration (PENDING_ONBOARDING with retention_expires_at < now):
       - Unlinks any associated Transactions (customer_id = NULL).
       - Deletes provisional LoyaltyPointLedger entries.
       - Hard-deletes the Customer record.
    2. 60-Month Retention Expiration (ACTIVE with retention_expires_at < now):
       - Transitions status to INACTIVE_EXPIRED.
    """
    current_dt = now or _utc_now_naive()

    expired_pending = crm_customer_repo.get_expired_pending_customers(db, current_dt)
    purged_count = 0
    for pending_customer in expired_pending:
        crm_customer_repo.purge_expired_pending_customer(db, pending_customer)
        purged_count += 1

    expired_active = crm_customer_repo.get_expired_active_customers(db, current_dt)
    expired_active_count = 0
    for active_customer in expired_active:
        active_customer.status = CustomerStatus.INACTIVE_EXPIRED
        db.add(active_customer)
        expired_active_count += 1

    if purged_count > 0 or expired_active_count > 0:
        db.commit()
    else:
        db.flush()

    logger.log_execution(
        "customer_onboarding_service",
        "lifecycle_maintenance_completed",
        "ok",
        purged_pending_count=purged_count,
        expired_active_count=expired_active_count,
    )
    return LifecycleMaintenanceResult(
        purged_pending_count=purged_count,
        expired_active_count=expired_active_count,
    )
