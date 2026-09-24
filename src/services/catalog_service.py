"""
src/services/catalog_service.py
--------------------------------
Service for direct (non-staging) catalog mutations.
Supports patching a confirmed ArticleBlueprint's mutable fields
and optionally regenerating its embedding vector.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.pim import ArticleBlueprint
from src.repositories.pim_repo import pim_repo
from src.schemas.catalog import CatalogUpdateRequest, CatalogUpdateResponse
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
            text_for_embedding = " ".join(filter(None, [
                bp.article_name,
                bp.description,
                " ".join(bp.tags or []),
                " ".join(bp.materials or []),
            ]))
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
