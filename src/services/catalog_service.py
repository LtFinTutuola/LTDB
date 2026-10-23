"""
src/services/catalog_service.py
--------------------------------
Service for direct (non-staging) catalog mutations.
Supports patching a confirmed ArticleBlueprint's mutable fields
and optionally regenerating its embedding vector.
"""
from datetime import date, datetime
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.pim import ArticleBlueprint, Brand, Category, ArticlePhoto
from src.models.wms import Article, Batch, Supplier, ArticleStatus, ArticlePrice, ArticleMovement
from src.models.sales import SaleLine
from src.models.excel_sales import ExcelSale
from sqlalchemy import func, inspect, text
from src.repositories.pim_repo import pim_repo
from src.schemas.catalog import (
    CatalogUpdateRequest,
    CatalogUpdateResponse,
    CatalogDeleteResponse,
    SearchResultItem,
)
from src.agents.llm_client import LLMClient
from src.core.logger import get_logger

logger = get_logger()

# Fields whose change triggers embedding regeneration
_DESCRIPTIVE_FIELDS = {"article_name", "description", "extended_description", "tags", "materials"}


def update_blueprint(db: Session, blueprint_id: str, request: CatalogUpdateRequest) -> CatalogUpdateResponse:
    """
    Apply a partial update to a confirmed ArticleBlueprint (and optionally its Article codes).

    - Only fields explicitly set in the request are modified.
    - tags, if provided, must be a non-empty list.
    - materials can be an empty list or populated list, aligned with staging.
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
    target_color = update_data.pop("color", None)

    has_vendor_code = "vendor_code" in update_data
    new_vendor_code = update_data.pop("vendor_code", None)
    has_barcode = "barcode" in update_data
    new_barcode = update_data.pop("barcode", None)

    has_category = "category_name" in update_data
    has_sub_category = "sub_category_name" in update_data
    category_name = update_data.pop("category_name", None)
    sub_category_name = update_data.pop("sub_category_name", None)

    has_quantity = "quantity" in update_data
    new_quantity = update_data.pop("quantity", None)

    if (not update_data and not has_vendor_code and not has_barcode 
            and not has_category and not has_sub_category and not has_quantity):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided for update."
        )

    if "article_name" in update_data:
        if not update_data["article_name"] or not str(update_data["article_name"]).strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Il campo 'article_name' non può essere vuoto."
            )
        clean_name = str(update_data["article_name"]).strip()
        if target_color and str(target_color).strip().lower() != "unknown":
            suffix = f" - {str(target_color).strip()}"
            if clean_name.lower().endswith(suffix.lower()):
                clean_name = clean_name[:-len(suffix)].strip()
        update_data["article_name"] = clean_name

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

    # --- Apply blueprint fields ---
    should_regenerate_embedding = False
    updated_fields_list = list(update_data.keys())
    for field, value in update_data.items():
        setattr(bp, field, value)
        if field in _DESCRIPTIVE_FIELDS:
            should_regenerate_embedding = True

    # --- Apply category & sub-category ---
    if has_category or has_sub_category:
        clean_cat = category_name.strip() if category_name and isinstance(category_name, str) else None
        clean_sub = sub_category_name.strip() if sub_category_name and isinstance(sub_category_name, str) else None

        target_cat = None
        if clean_sub:
            sub_candidates = db.query(Category).filter(
                Category.parent_id.isnot(None),
                (func.lower(Category.name) == clean_sub.lower()) | (func.lower(Category.description) == clean_sub.lower())
            ).all()
            if clean_cat:
                for sc in sub_candidates:
                    parent = db.query(Category).filter(Category.id == sc.parent_id).first()
                    if parent and (parent.name.lower() == clean_cat.lower() or parent.description.lower() == clean_cat.lower()):
                        target_cat = sc
                        break
            if not target_cat and sub_candidates:
                target_cat = sub_candidates[0]
        elif clean_cat:
            macro = db.query(Category).filter(
                Category.parent_id.is_(None),
                (func.lower(Category.name) == clean_cat.lower()) | (func.lower(Category.description) == clean_cat.lower())
            ).first()
            if macro:
                target_cat = macro

        bp.category_id = target_cat.id if target_cat else None
        should_regenerate_embedding = True
        updated_fields_list.append("category")

    # --- Apply quantity (available stock) ---
    if has_quantity:
        if new_quantity is None or new_quantity < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La quantità deve essere un numero intero non negativo."
            )

        articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()
        if target_color and str(target_color).strip().lower() != "unknown":
            norm_color = str(target_color).strip().lower()
            target_articles = [
                a for a in articles
                if norm_color in [str(c).strip().lower() for c in (a.colors or [])]
            ]
        else:
            target_articles = articles
            norm_color = None

        available_articles = [a for a in target_articles if a.status == ArticleStatus.AVAILABLE]
        current_stock = len(available_articles)
        diff = new_quantity - current_stock

        if diff > 0:
            from src.services.wms_service import register_inbound_movement
            sup_code = next((a.supplier_code for a in target_articles if a.supplier_code), None)
            ean_code = next((a.ean for a in target_articles if a.ean), None)
            color_list = [norm_color] if norm_color else (target_articles[0].colors if target_articles and target_articles[0].colors else None)
            register_inbound_movement(
                db=db,
                job_id=f"manual-{str(bp.id)[:8]}",
                blueprint_id=str(bp.id),
                quantity=diff,
                supplier_code=sup_code,
                ean=ean_code,
                colors=color_list,
                commit_changes=False
            )
        elif diff < 0:
            to_remove = available_articles[:abs(diff)]
            for art in to_remove:
                db.query(ArticlePrice).filter(ArticlePrice.article_id == art.id).delete()
                db.query(ArticleMovement).filter(ArticleMovement.article_id == art.id).delete()
                db.query(SaleLine).filter(SaleLine.article_id == art.id).delete()
                db.query(ExcelSale).filter(ExcelSale.article_id == art.id).update(
                    {ExcelSale.article_id: None}
                )
                db.delete(art)

        updated_fields_list.append("quantity")

    # --- Apply article variant fields (vendor_code / barcode) ---
    if has_vendor_code or has_barcode:
        articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()
        if target_color and str(target_color).strip().lower() != "unknown":
            norm_color = str(target_color).strip().lower()
            target_articles = [
                a for a in articles
                if norm_color in [str(c).strip().lower() for c in (a.colors or [])]
            ]
        else:
            target_articles = articles

        for art in target_articles:
            if has_vendor_code:
                art.supplier_code = new_vendor_code.strip() if new_vendor_code and new_vendor_code.strip() else None
            if has_barcode:
                art.ean = new_barcode.strip() if new_barcode and new_barcode.strip() else None

        if has_vendor_code:
            updated_fields_list.append("vendor_code")
        if has_barcode:
            updated_fields_list.append("barcode")

    db.commit()
    db.refresh(bp)

    logger.log_execution("catalog_service", "update_blueprint_committed", "ok",
                         blueprint_id=blueprint_id, updated_fields=updated_fields_list)

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
        updated_fields=updated_fields_list,
    )


def delete_catalog_item(
    db: Session,
    blueprint_id: str,
    color: str | None = None,
) -> CatalogDeleteResponse:
    """
    Delete a specific color variant of an ArticleBlueprint (or the entire blueprint
    if `color` is omitted or no other color variants remain).
    Cleans up dependent ArticlePrice, ArticleMovement, SaleLine, ExcelSale, and ArticlePhoto records.
    """
    logger.log_execution(
        "catalog_service",
        "delete_catalog_item_start",
        "ok",
        blueprint_id=blueprint_id,
        color=color,
    )

    bp: ArticleBlueprint | None = (
        db.query(ArticleBlueprint).filter(ArticleBlueprint.id == blueprint_id).first()
    )
    if not bp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Blueprint '{blueprint_id}' not found in the catalog.",
        )

    all_articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()

    norm_color = color.strip().lower() if color and color.strip() else None
    if norm_color and norm_color != "unknown":
        target_articles = [
            a for a in all_articles
            if norm_color in [str(c).strip().lower() for c in (a.colors or [])]
        ]
        db.query(ArticlePhoto).filter(
            ArticlePhoto.article_blueprint_id == bp.id,
            ArticlePhoto.canonical_color_name == norm_color,
        ).delete()
    else:
        target_articles = all_articles

    deleted_articles_count = len(target_articles)
    for art in target_articles:
        db.query(ArticlePrice).filter(ArticlePrice.article_id == art.id).delete()
        db.query(ArticleMovement).filter(ArticleMovement.article_id == art.id).delete()
        db.query(SaleLine).filter(SaleLine.article_id == art.id).delete()
        db.query(ExcelSale).filter(ExcelSale.article_id == art.id).update(
            {ExcelSale.article_id: None}
        )
        db.delete(art)

    db.flush()

    remaining_articles = (
        db.query(Article).filter(Article.article_blueprint_id == bp.id).count()
    )

    deleted_blueprint = False
    if not norm_color or norm_color == "unknown" or remaining_articles == 0:
        db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp.id).delete()
        # Legacy table with an FK to article_blueprints and no ORM model: clean it up
        # explicitly (when present) to avoid FOREIGN KEY violations.
        if inspect(db.connection()).has_table("blueprint_embedding_meta"):
            db.execute(
                text("DELETE FROM blueprint_embedding_meta WHERE blueprint_id = :bp_id"),
                {"bp_id": bp.id},
            )
        db.delete(bp)
        deleted_blueprint = True

    db.commit()

    logger.log_execution(
        "catalog_service",
        "delete_catalog_item_committed",
        "ok",
        blueprint_id=blueprint_id,
        color=norm_color,
        deleted_blueprint=deleted_blueprint,
        deleted_articles_count=deleted_articles_count,
    )

    return CatalogDeleteResponse(
        blueprint_id=blueprint_id,
        deleted_blueprint=deleted_blueprint,
        deleted_articles_count=deleted_articles_count,
    )

def list_latest_items(db: Session, limit: int = 50, offset: int = 0, in_stock_only: bool = False) -> dict:
    brands_map = {b.id: b for b in db.query(Brand).all()}
    cats_map = {c.id: c for c in db.query(Category).all()}
    batches_map = {b.id: b for b in db.query(Batch).all()}

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
    )

    if in_stock_only:
        avail_article_subq = (
            db.query(Article.article_blueprint_id)
            .filter(Article.status == ArticleStatus.AVAILABLE)
            .distinct()
            .subquery()
        )
        query = query.join(avail_article_subq, ArticleBlueprint.id == avail_article_subq.c.article_blueprint_id)

    query = query.order_by(
        func.coalesce(latest_article_subq.c.latest_article_date, ArticleBlueprint.created_at).desc(),
        ArticleBlueprint.id
    )

    blueprint_rows = query.all()
    bp_ids = [bp.id for bp, _ in blueprint_rows]

    all_articles = db.query(Article).filter(Article.article_blueprint_id.in_(bp_ids)).all() if bp_ids else []
    articles_by_bp = {}
    for a in all_articles:
        articles_by_bp.setdefault(a.article_blueprint_id, []).append(a)

    all_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id.in_(bp_ids)).all() if bp_ids else []
    photos_by_bp = {}
    for p in all_photos:
        photos_by_bp.setdefault(p.article_blueprint_id, []).append(p)

    all_results = []
    matched_blueprint_ids = set()

    for bp, _ in blueprint_rows:
        brand = brands_map.get(bp.brand_id)
        category = cats_map.get(bp.category_id) if bp.category_id else None
        
        main_category = None
        sub_category = None
        if category:
            if category.parent_id:
                sub_category = category
                main_category = cats_map.get(category.parent_id)
            else:
                main_category = category

        articles = articles_by_bp.get(bp.id, [])
        def _get_sort_key(a):
            batch = batches_map.get(a.batch_id)
            doc_date = batch.document_date if batch and batch.document_date else date.min
            created = a.created_at or datetime.min
            return (doc_date, created)
        articles.sort(key=_get_sort_key, reverse=True)

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

        bp_photos = photos_by_bp.get(bp.id, [])

        for color, variant_articles in colors_dict.items():
            stock = len([a for a in variant_articles if a.status == ArticleStatus.AVAILABLE])
            if in_stock_only and stock <= 0:
                continue

            photo = None
            if color != "unknown":
                photo = next((p for p in bp_photos if p.canonical_color_name == color), None)
            if not photo and bp_photos:
                photo = bp_photos[0]

            name = f"{bp.article_name} - {color.capitalize()}" if color and color != "unknown" else bp.article_name
            vendor_code = next((a.supplier_code for a in variant_articles if a.supplier_code), None)
            barcode = next((a.ean for a in variant_articles if a.ean), None)

            all_results.append(SearchResultItem(
                blueprint_id=bp.id,
                article_name=name,
                brand_name=brand.name if brand else "Unknown",
                brand_id=brand.id if brand else None,
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
            matched_blueprint_ids.add(bp.id)

    total_items = len(all_results)
    total_blueprints = len(matched_blueprint_ids)
    page_results = all_results[offset : offset + limit]

    page = (offset // limit) + 1 if limit > 0 else 1
    total_pages = (total_items + limit - 1) // limit if limit > 0 else 1
    if total_items == 0:
        total_pages = 1

    return {
        "results": page_results,
        "total_items": total_items,
        "total_blueprints": total_blueprints,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }
