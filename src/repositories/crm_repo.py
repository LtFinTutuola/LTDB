"""
src/repositories/crm_repo.py
----------------------------
Data access layer for CRM Customer, Transaction, and LoyaltyPointLedger entities.
"""
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from src.models.crm import (
    Customer,
    CustomerStatus,
    LoyaltyLedgerReason,
    LoyaltyPointLedger,
    Transaction,
)
from src.models.excel_sales import ExcelSale
from src.repositories.base import BaseRepository
from src.schemas.crm import CustomerCreateInternal, CustomerUpdateInternal


class CustomerRepository(BaseRepository[Customer, CustomerCreateInternal, CustomerUpdateInternal]):
    """Repository for Customer ORM persistence, lifecycle queries, and loyalty ledger aggregations."""

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

    def get_points_balance(
        self,
        db: Session,
        customer_id: str,
        exclude_transaction_id: Optional[str] = None,
    ) -> int:
        """
        Single Source of Truth calculation for customer points balance:
        SELECT COALESCE(SUM(points_delta), 0) FROM loyalty_point_ledger WHERE customer_id = :id
        """
        q = db.query(func.coalesce(func.sum(LoyaltyPointLedger.points_delta), 0)).filter(
            LoyaltyPointLedger.customer_id == customer_id
        )
        if exclude_transaction_id:
            q = q.filter(
                or_(
                    LoyaltyPointLedger.transaction_id.is_(None),
                    LoyaltyPointLedger.transaction_id != exclude_transaction_id,
                )
            )
        val = q.scalar()
        return int(val or 0)

    def get_points_balances_map(
        self,
        db: Session,
        customer_ids: List[str],
    ) -> Dict[str, int]:
        """Returns a mapping {customer_id: points_balance} in a single batch query."""
        if not customer_ids:
            return {}
        rows = (
            db.query(
                LoyaltyPointLedger.customer_id,
                func.coalesce(func.sum(LoyaltyPointLedger.points_delta), 0),
            )
            .filter(LoyaltyPointLedger.customer_id.in_(customer_ids))
            .group_by(LoyaltyPointLedger.customer_id)
            .all()
        )
        result = {cid: 0 for cid in customer_ids}
        for cid, total in rows:
            result[str(cid)] = int(total or 0)
        return result

    def search_customers(
        self,
        db: Session,
        q: Optional[str] = None,
        status: Optional[CustomerStatus] = None,
    ) -> List[Customer]:
        """
        Searches customers by optional free-text query (first_name, last_name, full name, phone_number)
        and optional CustomerStatus filter.
        """
        query = db.query(Customer)
        if status is not None:
            query = query.filter(Customer.status == status)

        if q and q.strip():
            term = q.strip()
            like_pattern = f"%{term}%"
            # Also create a digits-only term for partial phone searches like "+39 340-123"
            digits_only = "".join(ch for ch in term if ch.isdigit())
            if digits_only.startswith("0039") and len(digits_only) > 4:
                digits_only = digits_only[4:]
            elif digits_only.startswith("39") and term.strip().startswith("+39") and len(digits_only) > 2:
                digits_only = digits_only[2:]

            full_name_fwd = (
                func.coalesce(Customer.first_name, "")
                + " "
                + func.coalesce(Customer.last_name, "")
            )
            full_name_rev = (
                func.coalesce(Customer.last_name, "")
                + " "
                + func.coalesce(Customer.first_name, "")
            )
            conditions = [
                Customer.first_name.ilike(like_pattern),
                Customer.last_name.ilike(like_pattern),
                Customer.phone_number.ilike(like_pattern),
                full_name_fwd.ilike(like_pattern),
                full_name_rev.ilike(like_pattern),
            ]
            if digits_only:
                conditions.append(Customer.phone_number.ilike(f"%{digits_only}%"))

            query = query.filter(or_(*conditions))

        return query.order_by(Customer.created_at.desc()).all()

    def get_customer_ledger(
        self,
        db: Session,
        customer_id: str,
    ) -> List[LoyaltyPointLedger]:
        """Returns all loyalty point ledger entries for a customer ordered newest first."""
        return (
            db.query(LoyaltyPointLedger)
            .filter(LoyaltyPointLedger.customer_id == customer_id)
            .order_by(LoyaltyPointLedger.created_at.desc(), LoyaltyPointLedger.id.desc())
            .all()
        )

    def get_customer_transactions(
        self,
        db: Session,
        customer_id: str,
    ) -> List[Transaction]:
        """Returns all transactions for a customer with their excel_sales and ledger entries."""
        return (
            db.query(Transaction)
            .options(
                joinedload(Transaction.excel_sales).joinedload(ExcelSale.article),
                joinedload(Transaction.loyalty_ledger_entries),
            )
            .filter(Transaction.customer_id == customer_id)
            .order_by(Transaction.date.desc(), Transaction.created_at.desc())
            .all()
        )

    def get_birthday_bonus_transaction_dates_in_year(
        self,
        db: Session,
        customer_id: str,
        year: int,
    ) -> List[date]:
        """
        Returns the distinct Transaction.date values for which the customer has already
        received a BIRTHDAY_BONUS ledger movement within the given calendar year.
        """
        year_str = f"{year:04d}"
        rows = (
            db.query(Transaction.date)
            .join(LoyaltyPointLedger, LoyaltyPointLedger.transaction_id == Transaction.id)
            .filter(
                LoyaltyPointLedger.customer_id == customer_id,
                LoyaltyPointLedger.reason == LoyaltyLedgerReason.BIRTHDAY_BONUS,
                func.strftime("%Y", Transaction.date) == year_str,
            )
            .distinct()
            .all()
        )
        return [r[0] for r in rows if r[0] is not None]


crm_customer_repo = CustomerRepository()
