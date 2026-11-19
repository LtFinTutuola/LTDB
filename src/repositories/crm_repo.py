"""
src/repositories/crm_repo.py
----------------------------
Data access layer for CRM Customer entities, supporting onboarding and lifecycle jobs.
"""
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.crm import Customer, CustomerStatus, LoyaltyPointLedger, Transaction
from src.repositories.base import BaseRepository
from src.schemas.crm import CustomerCreateInternal, CustomerUpdateInternal


class CustomerRepository(BaseRepository[Customer, CustomerCreateInternal, CustomerUpdateInternal]):
    """Repository for Customer ORM persistence and lifecycle queries."""

    def __init__(self) -> None:
        super().__init__(Customer)

    def get_by_phone_number(self, db: Session, phone_number: str) -> Optional[Customer]:
        """Finds a customer by normalized phone number."""
        return (
            db.query(Customer)
            .filter(Customer.phone_number == phone_number)
            .first()
        )

    def get_by_flow_token(self, db: Session, flow_token: str) -> Optional[Customer]:
        """Finds a customer by their active WhatsApp Flow token."""
        if not flow_token:
            return None
        return (
            db.query(Customer)
            .filter(Customer.whatsapp_flow_token == flow_token)
            .first()
        )

    def get_latest_transaction_date(self, db: Session, customer_id: str) -> Optional[date]:
        """Returns the most recent transaction date associated with the customer, if any."""
        return (
            db.query(func.max(Transaction.date))
            .filter(Transaction.customer_id == customer_id)
            .scalar()
        )

    def get_expired_pending_customers(self, db: Session, now: datetime) -> List[Customer]:
        """
        Returns PENDING_ONBOARDING customers whose 72h onboarding window has expired.
        """
        return (
            db.query(Customer)
            .filter(
                Customer.status == CustomerStatus.PENDING_ONBOARDING,
                Customer.retention_expires_at.isnot(None),
                Customer.retention_expires_at < now,
            )
            .all()
        )

    def get_expired_active_customers(self, db: Session, now: datetime) -> List[Customer]:
        """
        Returns ACTIVE customers whose 60-month rolling retention window has expired.
        """
        return (
            db.query(Customer)
            .filter(
                Customer.status == CustomerStatus.ACTIVE,
                Customer.retention_expires_at.isnot(None),
                Customer.retention_expires_at < now,
            )
            .all()
        )

    def purge_expired_pending_customer(self, db: Session, customer: Customer) -> None:
        """
        Executes GDPR 72h hard delete for an uncompleted PENDING_ONBOARDING customer:
        1. Unlinks associated Transactions (customer_id = NULL).
        2. Deletes provisional LoyaltyPointLedger rows.
        3. Hard-deletes the Customer record.
        """
        db.query(Transaction).filter(Transaction.customer_id == customer.id).update(
            {Transaction.customer_id: None},
            synchronize_session=False,
        )
        db.query(LoyaltyPointLedger).filter(
            LoyaltyPointLedger.customer_id == customer.id
        ).delete(synchronize_session=False)
        db.delete(customer)


crm_customer_repo = CustomerRepository()
