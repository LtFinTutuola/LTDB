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

    has_new_color = "new_color" in update_data
    new_color = update_data.pop("new_color", None)
    has_colors = "colors" in update_data
    new_colors_list = update_data.pop("colors", None)

    has_category = "category_name" in update_data
    has_sub_category = "sub_category_name" in update_data
    category_name = update_data.pop("category_name", None)
    sub_category_name = update_data.pop("sub_category_name", None)

    has_quantity = "quantity" in update_data
    new_quantity = update_data.pop("quantity", None)

    wants_color_update = False
    cleaned_new_colors: list[str] = []
    if has_colors and isinstance(new_colors_list, list):
        cleaned_new_colors = [str(c).strip().lower() for c in new_colors_list if str(c).strip()]
        wants_color_update = True
    elif has_new_color:
        if new_color is not None and str(new_color).strip():
            cleaned_new_colors = [s.strip().lower() for s in str(new_color).split(',') if s.strip()]
        else:
            cleaned_new_colors = []
        wants_color_update = True
    elif target_color is not None and not has_quantity and not has_vendor_code and not has_barcode and not has_category and not has_sub_category and not update_data:
        cleaned_new_colors = [s.strip().lower() for s in str(target_color).split(',') if s.strip()]
        wants_color_update = True

    if (not update_data and not has_vendor_code and not has_barcode 
            and not has_category and not has_sub_category and not has_quantity
            and not wants_color_update):
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
        if wants_color_update and cleaned_new_colors:
            suffix_new = f" - {cleaned_new_colors[0]}"
            if clean_name.lower().endswith(suffix_new.lower()):
                clean_name = clean_name[:-len(suffix_new)].strip()
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
            if wants_color_update:
                color_list = cleaned_new_colors if cleaned_new_colors else None
            elif norm_color:
                color_list = [norm_color]
            else:
                color_list = target_articles[0].colors if target_articles and target_articles[0].colors else None

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

    # --- Apply article variant fields (color / vendor_code / barcode) ---
    if wants_color_update or has_vendor_code or has_barcode:
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

        if not target_articles and articles:
            target_articles = articles

        for art in target_articles:
            if wants_color_update:
                art.colors = cleaned_new_colors
            if has_vendor_code:
                art.supplier_code = new_vendor_code.strip() if new_vendor_code and new_vendor_code.strip() else None
            if has_barcode:
                art.ean = new_barcode.strip() if new_barcode and new_barcode.strip() else None

        if wants_color_update:
            updated_fields_list.append("color")
            # Update photo canonical_color_name if matching old target_color
            old_norm_col = norm_color
            new_canonical = cleaned_new_colors[0] if cleaned_new_colors else None
            if old_norm_col and new_canonical and old_norm_col != new_canonical:
                old_photo = db.query(ArticlePhoto).filter(
                    ArticlePhoto.article_blueprint_id == bp.id,
                    func.lower(ArticlePhoto.canonical_color_name) == old_norm_col,
                ).first()
                if old_photo:
                    conflict = db.query(ArticlePhoto).filter(
                        ArticlePhoto.article_blueprint_id == bp.id,
                        func.lower(ArticlePhoto.canonical_color_name) == new_canonical,
                    ).first()
                    if not conflict:
                        old_photo.canonical_color_name = new_canonical
            elif not old_norm_col and new_canonical:
                unknown_photo = db.query(ArticlePhoto).filter(
                    ArticlePhoto.article_blueprint_id == bp.id,
                    func.lower(ArticlePhoto.canonical_color_name) == "unknown",
                ).first()
                if unknown_photo:
                    conflict = db.query(ArticlePhoto).filter(
                        ArticlePhoto.article_blueprint_id == bp.id,
                        func.lower(ArticlePhoto.canonical_color_name) == new_canonical,
                    ).first()
                    if not conflict:
                        unknown_photo.canonical_color_name = new_canonical

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

def list_latest_items(
    db: Session,
    limit: int = 50,
    offset: int = 0,
    in_stock_only: bool = False,
    sort_by: str = "date",
    sort_dir: str = "desc",
) -> dict:
    from src.services.catalog_query_service import (
        build_variant_rows_for_blueprints,
        sort_variant_rows,
        paginate_rows,
    )

    query = db.query(ArticleBlueprint)

    if in_stock_only:
        avail_article_subq = (
            db.query(Article.article_blueprint_id)
            .filter(Article.status == ArticleStatus.AVAILABLE)
            .distinct()
            .subquery()
        )
        query = query.join(avail_article_subq, ArticleBlueprint.id == avail_article_subq.c.article_blueprint_id)

    blueprints = query.all()
    entries = [{"id": bp.id, "blueprint": bp} for bp in blueprints]

    rows = build_variant_rows_for_blueprints(db, entries, in_stock_only=in_stock_only)
    sorted_rows = sort_variant_rows(rows, sort_by=sort_by, sort_dir=sort_dir)

    page = (offset // limit) + 1 if limit > 0 else 1
    return paginate_rows(sorted_rows, page=page, page_size=limit)
