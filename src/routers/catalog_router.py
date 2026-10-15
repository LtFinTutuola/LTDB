"""
src/routers/catalog_router.py
------------------------------
Catalog API router.

Endpoints:
  GET  /api/v1/catalog/brands                    — Brand list for dropdown population.
  GET  /api/v1/catalog/categories/{brand_id}     — Category hierarchy for a brand.
  POST /api/v1/catalog/search/semantic           — Natural language catalog search.
  PATCH /api/v1/catalog/{blueprint_id}           — Direct ArticleBlueprint update.
"""
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.core.database import get_db
from src.core.logger import get_logger
from src.models.pim import Brand, Category, BrandCategory, ArticleBlueprint, BrandHeuristic
from src.repositories.pim_repo import category_repo
from src.schemas.catalog import (
    BrandListItem,
    BrandDetailItem,
    BrandCreateRequest,
    BrandUpdateRequest,
    CategoryDetailItem,
    CategoryCreateRequest,
    CategoryUpdateRequest,
    CatalogUpdateRequest,
    CatalogUpdateResponse,
    SemanticSearchRequest,
    SemanticSearchResponse,
    CatalogItemsResponse,
)
from src.services import catalog_service, search_service

logger = get_logger()

router = APIRouter(prefix="/api/v1/catalog", tags=["Catalog"])


@router.get("/items", response_model=CatalogItemsResponse)
def list_items(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """
    Return the latest items in the catalog.
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path="/api/v1/catalog/items", method="GET")
    try:
        return catalog_service.list_latest_items(db, limit, offset)
    except Exception as exc:
        logger.log_execution("catalog_router", "exception_caught", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc) or "unknown server error",
        )

def _format_brand_detail(brand: Brand, db: Session) -> BrandDetailItem:
    macro_cats = (
        db.query(Category)
        .join(BrandCategory, BrandCategory.category_id == Category.id)
        .filter(BrandCategory.brand_id == brand.id)
        .order_by(Category.name)
        .all()
    )
    bp_count = db.query(ArticleBlueprint).filter(ArticleBlueprint.brand_id == brand.id).count()
    return BrandDetailItem(
        id=brand.id,
        name=brand.name,
        category_ids=[c.id for c in macro_cats],
        category_names=[c.name for c in macro_cats],
        blueprint_count=bp_count,
        created_at=str(brand.created_at) if brand.created_at else None,
        updated_at=str(brand.updated_at) if brand.updated_at else None,
    )


def _format_category_detail(cat: Category, db: Session) -> CategoryDetailItem:
    parent_name = None
    if cat.parent_id:
        parent = db.query(Category).filter(Category.id == cat.parent_id).first()
        if parent:
            parent_name = parent.name

    sub_count = db.query(Category).filter(Category.parent_id == cat.id).count()

    brands = (
        db.query(Brand)
        .join(BrandCategory, BrandCategory.brand_id == Brand.id)
        .filter(BrandCategory.category_id == cat.id)
        .order_by(func.lower(Brand.name))
        .all()
    )
    bp_count = db.query(ArticleBlueprint).filter(ArticleBlueprint.category_id == cat.id).count()

    return CategoryDetailItem(
        id=cat.id,
        name=cat.name,
        description=cat.description or "",
        parent_id=cat.parent_id,
        parent_name=parent_name,
        subcategories_count=sub_count,
        brand_ids=[b.id for b in brands],
        brand_names=[b.name for b in brands],
        blueprint_count=bp_count,
        created_at=str(cat.created_at) if cat.created_at else None,
        updated_at=str(cat.updated_at) if cat.updated_at else None,
    )


# ---------------------------------------------------------------------------
# Brand CRUD Endpoints
# ---------------------------------------------------------------------------

@router.get("/brands", response_model=List[BrandListItem])
def list_brands(db: Session = Depends(get_db)):
    """
    Return all brands ordered by name, for dropdown population in the frontend.
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path="/api/v1/catalog/brands", method="GET")
    brands = db.query(Brand).order_by(func.lower(Brand.name)).all()
    return [{"id": b.id, "name": b.name} for b in brands]


@router.get("/brands/details", response_model=List[BrandDetailItem])
def list_brands_details(db: Session = Depends(get_db)):
    """
    Return detailed list of all brands with category mappings and blueprint counts.
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path="/api/v1/catalog/brands/details", method="GET")
    brands = db.query(Brand).order_by(func.lower(Brand.name)).all()
    return [_format_brand_detail(b, db) for b in brands]



@router.post("/brands", response_model=BrandDetailItem, status_code=status.HTTP_201_CREATED)
def create_brand(request: BrandCreateRequest, db: Session = Depends(get_db)):
    """
    Create a new brand and optionally associate macro-categories.
    """
    clean_name = request.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Il nome del brand non può essere vuoto.")

    existing = db.query(Brand).filter(func.lower(Brand.name) == clean_name.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Un brand con il nome '{clean_name}' esiste già.")

    brand = Brand(name=clean_name)
    db.add(brand)
    db.flush()

    if request.category_ids:
        for cat_id in set(request.category_ids):
            cat = db.query(Category).filter(Category.id == cat_id).first()
            if cat:
                db.add(BrandCategory(brand_id=brand.id, category_id=cat.id))

    db.commit()
    db.refresh(brand)
    logger.log_execution("catalog_router", "brand_created", "ok", brand_id=brand.id, name=brand.name)
    return _format_brand_detail(brand, db)


@router.put("/brands/{brand_id}", response_model=BrandDetailItem)
def update_brand(brand_id: str, request: BrandUpdateRequest, db: Session = Depends(get_db)):
    """
    Update brand name and/or associated categories.
    """
    brand = db.query(Brand).filter(Brand.id == brand_id).first()
    if not brand:
        raise HTTPException(status_code=404, detail="Brand non trovato.")

    if request.name is not None:
        clean_name = request.name.strip()
        if not clean_name:
            raise HTTPException(status_code=400, detail="Il nome del brand non può essere vuoto.")
        existing = db.query(Brand).filter(
            func.lower(Brand.name) == clean_name.lower(),
            Brand.id != brand_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Un brand con il nome '{clean_name}' esiste già.")
        brand.name = clean_name

    if request.category_ids is not None:
        db.query(BrandCategory).filter(BrandCategory.brand_id == brand.id).delete()
        for cat_id in set(request.category_ids):
            cat = db.query(Category).filter(Category.id == cat_id).first()
            if cat:
                db.add(BrandCategory(brand_id=brand.id, category_id=cat.id))

    db.commit()
    db.refresh(brand)
    logger.log_execution("catalog_router", "brand_updated", "ok", brand_id=brand.id, name=brand.name)
    return _format_brand_detail(brand, db)


@router.delete("/brands/{brand_id}")
def delete_brand(brand_id: str, db: Session = Depends(get_db)):
    """
    Delete a brand if no article blueprints reference it.
    """
    brand = db.query(Brand).filter(Brand.id == brand_id).first()
    if not brand:
        raise HTTPException(status_code=404, detail="Brand non trovato.")

    bp_count = db.query(ArticleBlueprint).filter(ArticleBlueprint.brand_id == brand_id).count()
    if bp_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Impossibile eliminare il brand '{brand.name}': ci sono {bp_count} articoli/blueprint associati."
        )

    db.query(BrandCategory).filter(BrandCategory.brand_id == brand_id).delete()
    db.query(BrandHeuristic).filter(BrandHeuristic.brand_id == brand_id).delete()
    db.delete(brand)
    db.commit()
    logger.log_execution("catalog_router", "brand_deleted", "ok", brand_id=brand_id)
    return {"status": "deleted", "id": brand_id}


# ---------------------------------------------------------------------------
# Category CRUD Endpoints
# ---------------------------------------------------------------------------

@router.get("/categories", response_model=List[CategoryDetailItem])
def list_categories(db: Session = Depends(get_db)):
    """
    Return all categories with hierarchy details and associated brands.
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path="/api/v1/catalog/categories", method="GET")
    cats = db.query(Category).order_by(Category.name).all()
    return [_format_category_detail(c, db) for c in cats]


@router.post("/categories", response_model=CategoryDetailItem, status_code=status.HTTP_201_CREATED)
def create_category(request: CategoryCreateRequest, db: Session = Depends(get_db)):
    """
    Create a new macro-category or sub-category.
    """
    clean_name = request.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Il nome della categoria non può essere vuoto.")

    parent_id = request.parent_id if request.parent_id else None
    if parent_id:
        parent = db.query(Category).filter(Category.id == parent_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Categoria genitore non trovata.")

    existing = db.query(Category).filter(
        func.lower(Category.name) == clean_name.lower(),
        Category.parent_id == parent_id
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Una categoria con il nome '{clean_name}' esiste già sotto questo livello."
        )

    cat = Category(name=clean_name, description=request.description.strip(), parent_id=parent_id)
    db.add(cat)
    db.flush()

    if parent_id is None and request.brand_ids:
        for b_id in set(request.brand_ids):
            b = db.query(Brand).filter(Brand.id == b_id).first()
            if b:
                db.add(BrandCategory(brand_id=b.id, category_id=cat.id))

    db.commit()
    db.refresh(cat)
    logger.log_execution("catalog_router", "category_created", "ok", category_id=cat.id, name=cat.name)
    return _format_category_detail(cat, db)


@router.put("/categories/{category_id}", response_model=CategoryDetailItem)
def update_category(category_id: str, request: CategoryUpdateRequest, db: Session = Depends(get_db)):
    """
    Update category name, description, parent category, and/or associated brands.
    """
    cat = db.query(Category).filter(Category.id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categoria non trovata.")

    new_parent_id = cat.parent_id
    if request.parent_id is not None:
        new_parent_id = request.parent_id if request.parent_id != "" else None
        if new_parent_id == category_id:
            raise HTTPException(status_code=400, detail="Una categoria non può essere genitore di se stessa.")
        if new_parent_id is not None:
            parent = db.query(Category).filter(Category.id == new_parent_id).first()
            if not parent:
                raise HTTPException(status_code=404, detail="Categoria genitore non trovata.")
            # Check for circular ancestry
            curr = parent
            visited = set()
            while curr:
                if curr.id == category_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Riferimento circolare: una categoria non può essere figlia di un proprio discendente."
                    )
                if curr.id in visited:
                    break
                visited.add(curr.id)
                curr = db.query(Category).filter(Category.id == curr.parent_id).first() if curr.parent_id else None
        cat.parent_id = new_parent_id

    if request.name is not None:
        clean_name = request.name.strip()
        if not clean_name:
            raise HTTPException(status_code=400, detail="Il nome della categoria non può essere vuoto.")
        existing = db.query(Category).filter(
            func.lower(Category.name) == clean_name.lower(),
            Category.parent_id == new_parent_id,
            Category.id != category_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Una categoria con il nome '{clean_name}' esiste già sotto questo livello."
            )
        cat.name = clean_name

    if request.description is not None:
        cat.description = request.description.strip()

    if request.brand_ids is not None:
        db.query(BrandCategory).filter(BrandCategory.category_id == cat.id).delete()
        if not cat.parent_id:
            for b_id in set(request.brand_ids):
                b = db.query(Brand).filter(Brand.id == b_id).first()
                if b:
                    db.add(BrandCategory(brand_id=b.id, category_id=cat.id))

    db.commit()
    db.refresh(cat)
    logger.log_execution("catalog_router", "category_updated", "ok", category_id=cat.id, name=cat.name)
    return _format_category_detail(cat, db)


@router.delete("/categories/{category_id}")
def delete_category(category_id: str, db: Session = Depends(get_db)):
    """
    Delete a category if it has no subcategories and no article blueprints.
    """
    cat = db.query(Category).filter(Category.id == category_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categoria non trovata.")

    sub_count = db.query(Category).filter(Category.parent_id == category_id).count()
    if sub_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Impossibile eliminare la categoria '{cat.name}': contiene {sub_count} sottocategorie. Elimina o sposta prima le sottocategorie."
        )

    bp_count = db.query(ArticleBlueprint).filter(ArticleBlueprint.category_id == category_id).count()
    if bp_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Impossibile eliminare la categoria '{cat.name}': ci sono {bp_count} articoli/blueprint associati."
        )

    db.query(BrandCategory).filter(BrandCategory.category_id == category_id).delete()
    db.delete(cat)
    db.commit()
    logger.log_execution("catalog_router", "category_deleted", "ok", category_id=category_id)
    return {"status": "deleted", "id": category_id}


@router.get("/categories/{brand_id}")
def get_brand_categories(brand_id: str, db: Session = Depends(get_db)):
    """
    Return the full category hierarchy for a specific brand.
    Delegates to the existing CategoryRepository.get_brand_hierarchy().
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path=f"/api/v1/catalog/categories/{brand_id}", method="GET")
    try:
        return category_repo.get_brand_hierarchy(db, brand_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.log_execution("catalog_router", "exception_caught", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc) or "unknown server error",
        )



@router.post("/search/semantic", response_model=SemanticSearchResponse)
async def semantic_search(
    request: SemanticSearchRequest,
    db: Session = Depends(get_db),
):
    """
    Accept a natural language query and return matching catalog items.
    Internally routes between SQL filter search and embedding similarity search.
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path="/api/v1/catalog/search/semantic", method="POST",
                         query=request.query)
    start = time.time()
    try:
        result = await search_service.execute_semantic_search(db, request.query, request.in_stock_only)
        latency_ms = int((time.time() - start) * 1000)
        logger.log_execution("catalog_router", "response_dispatched", "ok",
                             path="/api/v1/catalog/search/semantic",
                             results_count=len(result.get("results", [])),
                             latency_ms=latency_ms)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.log_execution("catalog_router", "exception_caught", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc) or "unknown server error",
        )


@router.patch("/{blueprint_id}", response_model=CatalogUpdateResponse)
def patch_blueprint(
    blueprint_id: str,
    request: CatalogUpdateRequest,
    db: Session = Depends(get_db),
):
    """
    Directly update mutable fields of a confirmed ArticleBlueprint.
    Bypasses the staging flow — intended for quick corrections from the search UI.
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path=f"/api/v1/catalog/{blueprint_id}", method="PATCH",
                         request_body=request.model_dump(exclude_unset=True))
    try:
        return catalog_service.update_blueprint(db, blueprint_id, request)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.log_execution("catalog_router", "exception_caught", "err", exc=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc) or "unknown server error",
        )
