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


class ChatMessageRequest(BaseModel):
    message: str
    context_date: Optional[str] = None
    session_id: Optional[str] = None


class ChatMessageResponse(BaseModel):
    reply: str
    session_id: str
    session_title: str
    message_id: Optional[str] = None


class ReportMessageRequest(BaseModel):
    message_id: str
    reason: str


class ReportMessageResponse(BaseModel):
    status: str
    message_id: str
    is_reported: bool
    report_reason: Optional[str] = None


class SalesChatMessageResponse(BaseSchemaWithAudit):
    session_id: str
    role: str
    content: str
    context_date: Optional[str] = None
    query_plan: Optional[dict] = None
    is_reported: bool = False
    report_reason: Optional[str] = None


class SalesChatSessionSummary(BaseSchemaWithAudit):
    title: str


class SalesChatSessionDetail(BaseSchemaWithAudit):
    title: str
    messages: List[SalesChatMessageResponse] = []


