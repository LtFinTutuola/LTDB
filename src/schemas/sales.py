from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel
from src.models.sales import PaymentMethod
from src.schemas.base import BaseSchemaWithAudit, BaseSchema

class SellerCreate(BaseModel):
    name: str

class SellerResponse(BaseSchemaWithAudit):
    name: str

class SaleLineCreate(BaseModel):
    article_id: str
    quantity: int
    unit_price: float

class SaleLineResponse(BaseSchemaWithAudit):
    sale_id: str
    article_id: str
    quantity: int
    unit_price: float

class SaleCreate(BaseModel):
    seller_id: str
    total_paid: float
    payment_method: PaymentMethod
    lines: List[SaleLineCreate]

class SaleResponse(BaseSchemaWithAudit):
    seller_id: str
    transaction_date: datetime
    total_paid: float
    payment_method: PaymentMethod
    lines: List[SaleLineResponse]

class SaleSummary(BaseModel):
    total_sales: int
    total_revenue: float
