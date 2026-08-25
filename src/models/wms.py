from datetime import date
from typing import List, Optional
import enum

from sqlalchemy import String, ForeignKey, Date, Enum, Numeric, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, UUIDMixin, TimestampMixin
from src.models.types import UppercaseString, LowercaseJSONList


class ArticleStatus(str, enum.Enum):
    AVAILABLE = "Available"
    SOLD = "Sold"
    LOST = "Lost"


class Supplier(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "suppliers"

    company_name: Mapped[str] = mapped_column(String, nullable=False)
    vat_number: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)

    # Relationships
    batches: Mapped[List["Batch"]] = relationship("Batch", back_populates="supplier")


class Batch(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "batches"

    supplier_id: Mapped[str] = mapped_column(String(36), ForeignKey("suppliers.id"), nullable=False)
    delivery_note_number: Mapped[str] = mapped_column(String, nullable=False)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Relationships
    supplier: Mapped["Supplier"] = relationship("Supplier", back_populates="batches")
    articles: Mapped[List["Article"]] = relationship("Article", back_populates="batch")


class Article(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "articles"

    article_blueprint_id: Mapped[str] = mapped_column(String(36), ForeignKey("article_blueprints.id"), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("batches.id"), nullable=False)
    supplier_code: Mapped[Optional[str]] = mapped_column(UppercaseString, index=True, nullable=True)
    ean: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    colors: Mapped[Optional[list]] = mapped_column(LowercaseJSONList, nullable=True)
    status: Mapped[ArticleStatus] = mapped_column(Enum(ArticleStatus), nullable=False, default=ArticleStatus.AVAILABLE)

    # Relationships
    blueprint: Mapped["ArticleBlueprint"] = relationship("ArticleBlueprint", back_populates="articles")
    batch: Mapped["Batch"] = relationship("Batch", back_populates="articles")
    prices: Mapped[List["ArticlePrice"]] = relationship("ArticlePrice", back_populates="article")
    movements: Mapped[List["ArticleMovement"]] = relationship("ArticleMovement", back_populates="article")
    sale_lines: Mapped[List["SaleLine"]] = relationship("SaleLine", back_populates="article")


class ArticlePrice(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "article_prices"

    article_id: Mapped[str] = mapped_column(String(36), ForeignKey("articles.id"), nullable=False)
    list_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    
    # Relationships
    article: Mapped["Article"] = relationship("Article", back_populates="prices")


class MovementReason(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "movement_reasons"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    sign: Mapped[int] = mapped_column(Integer, nullable=False) # 1, -1, or 0

    # Relationships
    movements: Mapped[List["ArticleMovement"]] = relationship("ArticleMovement", back_populates="reason")


class ArticleMovement(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "article_movements"

    article_id: Mapped[str] = mapped_column(String(36), ForeignKey("articles.id"), nullable=False)
    reason_id: Mapped[str] = mapped_column(String(36), ForeignKey("movement_reasons.id"), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    article: Mapped["Article"] = relationship("Article", back_populates="movements")
    reason: Mapped["MovementReason"] = relationship("MovementReason", back_populates="movements")
