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
    excel_file_column: Optional[int] = None
    status: ExcelSaleStatus = ExcelSaleStatus.ORPHAN
    starting_price: Optional[float] = None
    selling_price: Optional[float] = None
    is_exchange: bool = False
    raw_article_code: Optional[str] = None
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


class ExcelSaleListItem(BaseModel):
    id: str
    date: str
    excel_row_index: int
    excel_file_column: Optional[int] = None
    status: ExcelSaleStatus
    starting_price: Optional[float] = None
    selling_price: Optional[float] = None
    is_exchange: bool = False
    raw_article_code: Optional[str] = None
    article_name: Optional[str] = None
    photo_url: Optional[str] = None
