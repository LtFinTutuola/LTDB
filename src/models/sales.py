import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import String, ForeignKey, DateTime, Numeric, Enum, Integer, JSON, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, UUIDMixin, TimestampMixin


class PaymentMethod(str, enum.Enum):
    CASH = "Cash"
    CREDIT_CARD = "Credit_Card"
    DIGITAL_WALLET = "Digital_Wallet"


class Seller(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sellers"

    name: Mapped[str] = mapped_column(String, nullable=False)

    # Relationships
    sales: Mapped[List["Sale"]] = relationship("Sale", back_populates="seller")


class Sale(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sales"

    seller_id: Mapped[str] = mapped_column(String(36), ForeignKey("sellers.id"), nullable=False)
    transaction_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    total_paid: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    payment_method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod), nullable=False)

    # Relationships
    seller: Mapped["Seller"] = relationship("Seller", back_populates="sales")
    lines: Mapped[List["SaleLine"]] = relationship("SaleLine", back_populates="sale")


class SaleLine(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sale_lines"

    sale_id: Mapped[str] = mapped_column(String(36), ForeignKey("sales.id"), nullable=False)
    article_id: Mapped[str] = mapped_column(String(36), ForeignKey("articles.id"), nullable=False)
    
    # 1 for sale, -1 for return
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # The actual transaction value per unit for this line
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # Relationships
    sale: Mapped["Sale"] = relationship("Sale", back_populates="lines")
    article: Mapped["Article"] = relationship("Article", back_populates="sale_lines")


class SalesChatSession(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sales_chat_sessions"

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Nuova conversazione")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    messages: Mapped[List["SalesChatMessage"]] = relationship(
        "SalesChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="SalesChatMessage.created_at.asc()",
    )


class SalesChatMessage(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sales_chat_messages"

    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sales_chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # "user" or "assistant"
    content: Mapped[str] = mapped_column(String, nullable=False)
    context_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    query_plan: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_reported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    report_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    session: Mapped["SalesChatSession"] = relationship("SalesChatSession", back_populates="messages")

