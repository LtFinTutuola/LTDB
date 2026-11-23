from datetime import date
from typing import TYPE_CHECKING, Optional

from sqlalchemy import String, ForeignKey, Date, Numeric, Boolean, UniqueConstraint, Integer, Enum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, UUIDMixin, TimestampMixin
from src.schemas.excel_sales import ExcelSaleStatus

if TYPE_CHECKING:
    from src.models.crm import Transaction


class ExcelSale(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "excel_sales"
    __table_args__ = (
        UniqueConstraint("date", "excel_row_index", name="uq_excel_sale_date_row"),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    excel_row_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    excel_file_column: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    status: Mapped[ExcelSaleStatus] = mapped_column(Enum(ExcelSaleStatus), nullable=False, default=ExcelSaleStatus.ORPHAN)
    
    starting_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    selling_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    
    is_exchange: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    
    raw_article_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    customer_phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    
    article_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("articles.id"), nullable=True, index=True)
    transaction_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("transactions.id", name="fk_excel_sales_transaction_id_transactions", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    article: Mapped[Optional["Article"]] = relationship("Article")
    transaction: Mapped[Optional["Transaction"]] = relationship("Transaction", back_populates="excel_sales")
