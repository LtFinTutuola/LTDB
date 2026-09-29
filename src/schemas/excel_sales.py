from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict
from enum import Enum


class ExcelSaleStatus(str, Enum):
    ORPHAN = "ORPHAN"
    RECONCILED = "RECONCILED"
    UNPROCESSABLE = "UNPROCESSABLE"


class ExcelSaleBase(BaseModel):
    date: date
    excel_row_index: int
    excel_file_column: Optional[str] = None
    status: ExcelSaleStatus = ExcelSaleStatus.ORPHAN
    starting_price: Optional[float] = None
    selling_price: Optional[float] = None
    is_exchange: bool = False
    article_id: Optional[str] = None


class ExcelSaleCreate(ExcelSaleBase):
    pass


class ExcelSaleUpdate(BaseModel):
    status: Optional[ExcelSaleStatus] = None
    article_id: Optional[str] = None


class ExcelSaleResponse(ExcelSaleBase):
    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
