import enum
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from src.models.excel_sales import ExcelSale


class CustomerStatus(str, enum.Enum):
    """Lifecycle status of a CRM customer profile."""

    PENDING_ONBOARDING = "PENDING_ONBOARDING"
    ACTIVE = "ACTIVE"
    INACTIVE_EXPIRED = "INACTIVE_EXPIRED"
    ANONYMIZED = "ANONYMIZED"


class LoyaltyLedgerReason(str, enum.Enum):
    """Immutable ledger reason codes for loyalty point movements."""

    PURCHASE_EARN = "PURCHASE_EARN"
    BIRTHDAY_BONUS = "BIRTHDAY_BONUS"
    DISCOUNT_REDEMPTION = "DISCOUNT_REDEMPTION"
    RETURN_REVERSAL = "RETURN_REVERSAL"
    PRICE_CORRECTION = "PRICE_CORRECTION"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"


class Customer(Base, UUIDMixin, TimestampMixin):
    """CRM customer profile with WhatsApp onboarding and GDPR consent tracking."""

    __tablename__ = "customers"

    phone_number: Mapped[Optional[str]] = mapped_column(
        String(20), unique=True, index=True, nullable=True
    )
    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    birth_date: Mapped[Optional[date]] = mapped_column(Date, index=True, nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[CustomerStatus] = mapped_column(
        Enum(CustomerStatus, validate_strings=True),
        nullable=False,
        default=CustomerStatus.PENDING_ONBOARDING,
    )

    terms_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    consent_marketing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_profiling: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    whatsapp_flow_token: Mapped[Optional[str]] = mapped_column(
        String(128), index=True, nullable=True
    )
    last_purchase_date: Mapped[Optional[date]] = mapped_column(Date, index=True, nullable=True)
    retention_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, index=True, nullable=True
    )

    # Relationships: keep transactions as anonymous sales (SET NULL) if customer is purged
    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction", back_populates="customer"
    )
    loyalty_ledger_entries: Mapped[List["LoyaltyPointLedger"]] = relationship(
        "LoyaltyPointLedger", back_populates="customer", cascade="all, delete-orphan"
    )


class Transaction(Base, UUIDMixin, TimestampMixin):
    """Sales transaction grouping one or more ExcelSale lines for a customer."""

    __tablename__ = "transactions"

    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("customers.id", name="fk_transactions_customer_id_customers", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    loyalty_discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    discount_rule_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Relationships
    customer: Mapped[Optional["Customer"]] = relationship(
        "Customer", back_populates="transactions"
    )
    excel_sales: Mapped[List["ExcelSale"]] = relationship(
        "ExcelSale", back_populates="transaction"
    )
    loyalty_ledger_entries: Mapped[List["LoyaltyPointLedger"]] = relationship(
        "LoyaltyPointLedger", back_populates="transaction"
    )


class LoyaltyPointLedger(Base, UUIDMixin):
    """Immutable append-only ledger of customer loyalty point deltas."""

    __tablename__ = "loyalty_point_ledger"

    customer_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("customers.id", name="fk_loyalty_ledger_customer_id_customers", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    transaction_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey(
            "transactions.id",
            name="fk_loyalty_ledger_transaction_id_transactions",
            ondelete="SET NULL",
        ),
        index=True,
        nullable=True,
    )
    points_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[LoyaltyLedgerReason] = mapped_column(
        Enum(LoyaltyLedgerReason, validate_strings=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(
        "Customer", back_populates="loyalty_ledger_entries"
    )
    transaction: Mapped[Optional["Transaction"]] = relationship(
        "Transaction", back_populates="loyalty_ledger_entries"
    )
