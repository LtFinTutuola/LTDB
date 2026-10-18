"""
src/services/catalog_service.py
--------------------------------
Service for direct (non-staging) catalog mutations.
Supports patching a confirmed ArticleBlueprint's mutable fields
and optionally regenerating its embedding vector.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.pim import ArticleBlueprint, Brand, Category, ArticlePhoto
from src.models.wms import Article, Batch, Supplier, ArticleStatus
from sqlalchemy import func
from src.repositories.pim_repo import pim_repo
from src.schemas.catalog import CatalogUpdateRequest, CatalogUpdateResponse, SearchResultItem
from src.agents.llm_client import LLMClient
from src.core.logger import get_logger

logger = get_logger()

# Fields whose change triggers embedding regeneration
_DESCRIPTIVE_FIELDS = {"article_name", "description", "extended_description", "tags", "materials"}


def update_blueprint(db: Session, blueprint_id: str, request: CatalogUpdateRequest) -> CatalogUpdateResponse:
    """
    Apply a partial update to a confirmed ArticleBlueprint.

    - Only fields explicitly set in the request are modified.
    - tags and materials, if provided, must be non-empty lists.
    - If any descriptive field changes, the embedding is regenerated synchronously.
      Embedding failure is non-blocking (logged but does not abort the update).
    """
    logger.log_execution("catalog_service", "update_blueprint_start", "ok",
                         blueprint_id=blueprint_id)

    # --- Fetch ---
    bp: ArticleBlueprint | None = db.query(ArticleBlueprint).filter(
        ArticleBlueprint.id == blueprint_id
    ).first()
    if not bp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint '{blueprint_id}' not found in the catalog."
        )

    update_data = request.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided for update."
        )

    # --- Prevent description loss ---
    if "description" in update_data and not update_data["description"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Il campo 'description' non può essere vuoto o nullo durante un aggiornamento."
        )

    # --- Validate lists ---
    if "tags" in update_data and not update_data["tags"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'tags' must be a non-empty list."
        )
    if "materials" in update_data and not update_data["materials"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'materials' must be a non-empty list."
        )

    # --- Apply fields ---
    should_regenerate_embedding = False
    for field, value in update_data.items():
        setattr(bp, field, value)
        if field in _DESCRIPTIVE_FIELDS:
            should_regenerate_embedding = True

    db.commit()
    db.refresh(bp)

    logger.log_execution("catalog_service", "update_blueprint_committed", "ok",
                         blueprint_id=blueprint_id, updated_fields=list(update_data.keys()))

    # --- Regenerate embedding (non-blocking) ---
    if should_regenerate_embedding:
        try:
            from src.services.pim_service import build_blueprint_embedding_text
            text_for_embedding = build_blueprint_embedding_text(db, bp)
            client = LLMClient()
            new_embedding = client.generate_embedding_sync(text_for_embedding)
            pim_repo.save_embedding(db, blueprint_id, new_embedding)
            logger.log_execution("catalog_service", "embedding_regenerated", "ok",
                                 blueprint_id=blueprint_id)
        except Exception as exc:
            logger.log_execution("catalog_service", "embedding_regeneration_failed", "err",
                                 exc=exc, blueprint_id=blueprint_id)

    return CatalogUpdateResponse(
        status="success",
        blueprint_id=blueprint_id,
        updated_fields=list(update_data.keys()),
    )

def list_latest_items(db: Session, limit: int = 50, offset: int = 0) -> dict:
    latest_article_subq = (
        db.query(
            Article.article_blueprint_id,
            func.max(Article.created_at).label("latest_article_date")
        )
        .group_by(Article.article_blueprint_id)
        .subquery()
    )

    query = (
        db.query(ArticleBlueprint, latest_article_subq.c.latest_article_date)
        .outerjoin(latest_article_subq, ArticleBlueprint.id == latest_article_subq.c.article_blueprint_id)
        .order_by(
            func.coalesce(latest_article_subq.c.latest_article_date, ArticleBlueprint.created_at).desc(),
            ArticleBlueprint.id
        )
    )

    total_blueprints = query.count()
    blueprint_rows = query.offset(offset).limit(limit).all()

    results = []
    for bp, _ in blueprint_rows:
        brand = db.query(Brand).filter(Brand.id == bp.brand_id).first()
        category = db.query(Category).filter(Category.id == bp.category_id).first() if bp.category_id else None
        
        main_category = None
        sub_category = None
        if category:
            if category.parent_id:
                sub_category = category
                main_category = db.query(Category).filter(Category.id == category.parent_id).first()
            else:
                main_category = category
        
        articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()

        articles_with_batch = []
        for a in articles:
            batch = db.query(Batch).filter(Batch.id == a.batch_id).first()
            if batch:
                articles_with_batch.append((a, batch))
        
        articles_with_batch.sort(key=lambda x: x[1].document_date, reverse=True)

        colors_dict = {}
        for a in articles:
            color_keys = [c.lower() for c in (a.colors or [])]
            if not color_keys:
                color_keys = ["unknown"]
            for c in color_keys:
                if c not in colors_dict:
                    colors_dict[c] = []
                colors_dict[c].append(a)

        if not colors_dict:
            colors_dict["unknown"] = []

        for color, variant_articles in colors_dict.items():
            stock = len([a for a in variant_articles if a.status == ArticleStatus.AVAILABLE])
            
            photo = None
            if color != "unknown":
                photo = db.query(ArticlePhoto).filter(
                    ArticlePhoto.article_blueprint_id == bp.id,
                    ArticlePhoto.canonical_color_name == color
                ).first()
            if not photo:
                photo = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp.id).first()
                
            name = f"{bp.article_name} - {color.capitalize()}" if color and color != "unknown" else bp.article_name

            vendor_code = next((a.supplier_code for a in variant_articles if a.supplier_code), None)
            barcode = next((a.ean for a in variant_articles if a.ean), None)

            results.append(SearchResultItem(
                blueprint_id=bp.id,
                article_name=name,
                brand_name=brand.name if brand else "Unknown",
                category_name=main_category.name if main_category else None,
                sub_category_name=sub_category.name if sub_category else None,
                description=bp.description,
                extended_description=bp.extended_description,
                vendor_code=vendor_code,
                barcode=barcode,
                photo_id=photo.id if photo else None,
                colors=[color] if color != "unknown" else [],
                tags=list(bp.tags or []),
                materials=list(bp.materials or []),
                stock=stock
            ))
            
    return {
        "results": results,
        "total_blueprints": total_blueprints
    }
