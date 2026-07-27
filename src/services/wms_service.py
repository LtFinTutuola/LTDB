from typing import List, Optional
from datetime import date
from sqlalchemy.orm import Session
from src.repositories.base import BaseRepository
from src.repositories.wms_repo import wms_repo
from src.schemas.wms import BatchCreate, ArticleCreate, StockUpdate, SupplierCreate
from src.models.wms import Batch, ArticleStatus, Supplier, MovementReason

batch_repo = BaseRepository[Batch, BatchCreate, BatchCreate](Batch)
supplier_repo = BaseRepository[Supplier, SupplierCreate, SupplierCreate](Supplier)

def register_inbound_movement(
    db: Session, 
    job_id: str, 
    blueprint_id: str, 
    quantity: int, 
    supplier_code: Optional[str] = None,
    ean: Optional[str] = None,
    colors: Optional[list[str]] = None,
    commit_changes: bool = True
) -> List[str]:
    """
    Registers an inbound warehouse movement by creating a batch (if not exists for job_id)
    and creating the specified quantity of physical articles.
    """
    # Create or get Dummy Supplier
    dummy_supplier_name = "DUMMY_SUPPLIER"
    existing_supplier = db.query(Supplier).filter(Supplier.company_name == dummy_supplier_name).first()
    if existing_supplier:
        supplier_id = str(existing_supplier.id)
    else:
        supplier_in = SupplierCreate(company_name=dummy_supplier_name)
        new_supplier = supplier_repo.create(db, obj_in=supplier_in, commit_changes=False)
        supplier_id = str(new_supplier.id)
        
    # Create or get Dummy Reason
    dummy_reason_code = "INGESTION"
    existing_reason = db.query(MovementReason).filter(MovementReason.code == dummy_reason_code).first()
    if existing_reason:
        reason_id = str(existing_reason.id)
    else:
        new_reason = MovementReason(code=dummy_reason_code, sign=1)
        db.add(new_reason)
        db.flush()
        reason_id = str(new_reason.id)
    
    # Check if batch exists
    existing_batch = db.query(Batch).filter(Batch.delivery_note_number == f"DDT-{job_id}").first()
    if existing_batch:
        batch_id = str(existing_batch.id)
    else:
        batch_in = BatchCreate(
            supplier_id=supplier_id,
            delivery_note_number=f"DDT-{job_id}",
            document_date=date.today()
        )
        new_batch = batch_repo.create(db, obj_in=batch_in, commit_changes=False)
        batch_id = str(new_batch.id)
    
    article_ids = []
    for _ in range(quantity):
        article_in = ArticleCreate(
            article_blueprint_id=blueprint_id,
            batch_id=batch_id,
            supplier_code=supplier_code,
            ean=ean,
            colors=colors,
            status=ArticleStatus.AVAILABLE
        )
        new_article = wms_repo.create(db, obj_in=article_in, commit_changes=False)
        article_ids.append(str(new_article.id))
        
        # Add movement
        movement_in = StockUpdate(
            article_id=str(new_article.id),
            reason_id=reason_id,
            notes="Inbound from Data Ingestion"
        )
        wms_repo.add_movement(db, movement_in=movement_in, commit_changes=False)
        
    if commit_changes:
        db.commit()
    else:
        db.flush()
        
    return article_ids
