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



class ColumnAggregation(BaseModel):
    col_index: int
    total_revenue: float
    total_list_price: float = 0.0
    discount: float = 0.0
    total_sales: int
    avg_price: float

class MetricComparison(BaseModel):
    today: float
    dow_avg: float
    dow_diff: float
    general_avg: float
    general_diff: float
    is_constant: bool = False

class PnLSummary(BaseModel):
    total_list_price: MetricComparison
    discount: MetricComparison
    net_discounted: MetricComparison
    vat_amount: MetricComparison
    net_no_vat: MetricComparison
    cost_of_goods: MetricComparison
    gross_margin: MetricComparison
    sg_media_giorno: MetricComparison
    costo_lavoro: MetricComparison
    residuo: MetricComparison
    imposizione_mutuo: MetricComparison
    net_margin: MetricComparison

class AggregationsResponse(BaseModel):
    columns: list[ColumnAggregation]
    pl_column: ColumnAggregation
    pnl_summary: PnLSummary
    target_date_dow: int = 0


class SaleDetailResponse(BaseModel):
    article_name: str
    photo_url: Optional[str] = None
    inventory: int
    blueprint_id: str
    colors: list[str]
    is_exchange: bool
