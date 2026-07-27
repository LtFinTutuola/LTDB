from typing import List, Optional
from datetime import date
from pydantic import BaseModel
from src.models.wms import ArticleStatus
from src.schemas.base import BaseSchemaWithAudit, BaseSchema

class SupplierCreate(BaseModel):
    company_name: str
    vat_number: Optional[str] = None

class SupplierResponse(BaseSchemaWithAudit):
    company_name: str
    vat_number: Optional[str]

class BatchCreate(BaseModel):
    supplier_id: str
    delivery_note_number: str
    document_date: date

class BatchResponse(BaseSchemaWithAudit):
    supplier_id: str
    delivery_note_number: str
    document_date: date

class ArticleCreate(BaseModel):
    article_blueprint_id: str
    batch_id: str
    supplier_code: Optional[str] = None
    ean: Optional[str] = None
    colors: Optional[List[str]] = None
    status: ArticleStatus = ArticleStatus.AVAILABLE

class ArticleResponse(BaseSchemaWithAudit):
    article_blueprint_id: str
    batch_id: str
    supplier_code: Optional[str]
    ean: Optional[str]
    colors: Optional[List[str]]
    status: ArticleStatus

class StockUpdate(BaseModel):
    article_id: str
    reason_id: str
    notes: Optional[str] = None
