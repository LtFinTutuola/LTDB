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
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.core.logger import get_logger
from src.models.pim import Brand
from src.repositories.pim_repo import category_repo
from src.schemas.catalog import (
    BrandListItem,
    CatalogUpdateRequest,
    CatalogUpdateResponse,
    SemanticSearchRequest,
    SemanticSearchResponse,
)
from src.services import catalog_service, search_service

logger = get_logger()

router = APIRouter(prefix="/api/v1/catalog", tags=["Catalog"])


@router.get("/brands", response_model=List[BrandListItem])
def list_brands(db: Session = Depends(get_db)):
    """
    Return all brands ordered by name, for dropdown population in the frontend.
    """
    logger.log_execution("catalog_router", "request_received", "ok",
                         path="/api/v1/catalog/brands", method="GET")
    brands = db.query(Brand).order_by(Brand.name).all()
    return [{"id": b.id, "name": b.name} for b in brands]


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
        result = await search_service.execute_semantic_search(db, request.query)
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
