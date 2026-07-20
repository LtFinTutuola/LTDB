import enum
from datetime import datetime
from typing import List

from sqlalchemy import String, ForeignKey, DateTime, Numeric, Enum, Integer
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
