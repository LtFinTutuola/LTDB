import base64
from typing import Optional
import httpx
from fastapi import HTTPException, status, Response
from sqlalchemy.orm import Session
from src.core.logger import get_logger
from src.repositories import staging_repo
from src.repositories.photo_repo import photo_repo
from src.models.pim import Brand, ArticleBlueprint, ArticlePhoto
from src.models.wms import Article
from src.schemas.data_ingestion import PhotoRetryRequest
from src.agents.llm_client import LLMClient
from src.agents.photo_search_agent import PhotoSearchAgent

logger = get_logger()


async def retry_photo_search(db: Session, job_id: str, request: PhotoRetryRequest) -> dict:
    job = staging_repo.get_job(db, job_id)
    if not job or not job.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    proposals = job.data.get("photo_proposals", [])
    proposal_idx = next((i for i, p in enumerate(proposals) if p.get("item_id") == request.item_id), None)

    if proposal_idx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item non trovato tra le photo proposals")

    proposal = proposals[proposal_idx]

    brand_id = job.data.get("brand_id")
    brand = db.query(Brand).filter(Brand.id == brand_id).first() if brand_id else None
    brand_name = brand.name if brand else ""
    item = next((it for it in job.data.get("items", []) if it.get("item_id") == request.item_id), None)

    if request.new_url:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        try:
            async with httpx.AsyncClient(headers=headers, timeout=5.0, follow_redirects=True) as client:
                async with client.stream("GET", request.new_url) as res:
                    if res.status_code >= 400:
                        raise HTTPException(status_code=422, detail="URL provided is not reachable")
                    ct = res.headers.get("content-type", "")
                    if not ct.startswith("image/"):
                        raise HTTPException(status_code=422, detail="URL provided does not point to an image")
        except httpx.RequestError:
            raise HTTPException(status_code=422, detail="URL provided is not reachable")

        proposal["photo_url"] = request.new_url
        proposal["photo_source"] = "manual_override"
        staging_repo.update_job_data(db, job_id, job.data)
        return proposal

    retry_count = proposal.get("retry_count", 0)
    if retry_count >= 3:
        raise HTTPException(status_code=422, detail="Max retry limit exceeded")

    if not item:
        raise HTTPException(status_code=404, detail="Item reference not found")

    blueprints = job.data.get("blueprints", [])
    blueprint = next((bp for bp in blueprints if bp.get("id") == item.get("article_blueprint_id")), {})

    client = LLMClient()
    photo_agent = PhotoSearchAgent(llm_client=client)
    agent_res = await photo_agent.aexecute({
        "item_id": request.item_id,
        "brand_name": brand_name,
        "vendor_code": item.get("vendor_code", ""),
        "color": proposal.get("canonical_color_candidate", ""),
        "article_name": blueprint.get("article_name", item.get("article_name", "")),
        "description": blueprint.get("description", ""),
        "user_feedback": request.user_feedback,
        "previous_ctx": proposal.get("_photo_search_ctx", []),
        "mode": "retry",
    })

    new_prop = agent_res.get("proposal") or {}
    candidate_url = new_prop.get("photo_url")

    # Only overwrite photo_url if the new candidate passed the guard;
    # otherwise preserve the previous valid value in staging.
    if candidate_url is not None:
        proposal["photo_url"] = candidate_url

    proposal["retry_count"] = retry_count + 1
    proposal["_photo_search_ctx"] = new_prop.get("_photo_search_ctx", proposal.get("_photo_search_ctx", []))
    proposal["search_query"] = new_prop.get("search_query")

    staging_repo.update_job_data(db, job_id, job.data)

    return proposal


def serve_photo(db: Session, photo_id: str) -> Response:
    photo = photo_repo.get_photo_by_id(db, photo_id)
    if not photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")

    return Response(
        content=photo.photo_data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def download_and_save_photo(
    db: Session,
    blueprint_id: str,
    color_name: str,
    photo_url: str,
    commit_changes: bool = True,
    force_update: bool = False,
) -> Optional[ArticlePhoto]:
    if not photo_url:
        return None
    if photo_url.startswith("/api/"):
        return None

    if not force_update:
        existing = photo_repo.get_photo_by_blueprint_and_color(db, blueprint_id, color_name or "")
        if existing:
            return existing

    # 1. Handle base64 data URLs
    if photo_url.startswith("data:"):
        try:
            if "," in photo_url:
                _, encoded = photo_url.split(",", 1)
            else:
                encoded = photo_url
            photo_bytes = base64.b64decode(encoded)
            return photo_repo.upsert_photo(
                db, blueprint_id, color_name or "", photo_bytes, commit_changes=commit_changes
            )
        except Exception as exc:
            logger.log_execution("photo_service", "data_url_decode_failed", "warn", exc=str(exc))
            raise ValueError(f"Failed to decode data URL: {exc}")

    # 2. Remote HTTP/HTTPS URL
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        with httpx.Client(headers=headers, timeout=10.0, follow_redirects=True) as client:
            resp = client.get(photo_url)
            resp.raise_for_status()

            return photo_repo.upsert_photo(
                db, blueprint_id, color_name or "", resp.content, commit_changes=commit_changes
            )
    except Exception as exc:
        logger.log_execution("photo_service", "download_failed", "warn", url=photo_url, exc=str(exc))
        raise ValueError(f"Failed to download image from {photo_url}: {exc}")


def save_photo_bytes(
    db: Session,
    blueprint_id: str,
    color_name: str,
    photo_bytes: bytes,
    commit_changes: bool = True,
) -> ArticlePhoto:
    """Save or update raw photo bytes for a blueprint and color."""
    if not photo_bytes:
        raise ValueError("Photo bytes cannot be empty")
    return photo_repo.upsert_photo(
        db, blueprint_id, color_name or "", photo_bytes, commit_changes=commit_changes
    )


def download_and_upsert_photo(
    db: Session,
    blueprint_id: str,
    color_name: str,
    photo_url: str,
    commit_changes: bool = True,
) -> ArticlePhoto:
    """Download from URL (or decode data URL) and upsert photo into article_photos."""
    if not photo_url:
        raise ValueError("Photo URL cannot be empty")
    res = download_and_save_photo(
        db, blueprint_id, color_name, photo_url, commit_changes=commit_changes, force_update=True
    )
    if not res:
        raise ValueError(f"Failed to process image from {photo_url}")
    return res


async def search_and_set_catalog_photo(
    db: Session,
    blueprint_id: str,
    color_name: str = "",
    user_feedback: Optional[str] = None,
) -> ArticlePhoto:
    """Use PhotoSearchAgent to find an image for a catalog blueprint and save it."""
    bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == blueprint_id).first()
    if not bp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint non trovato")

    brand = db.query(Brand).filter(Brand.id == bp.brand_id).first()
    brand_name = brand.name if brand else ""

    sample_article = (
        db.query(Article)
        .filter(Article.article_blueprint_id == blueprint_id)
        .first()
    )
    vendor_code = sample_article.supplier_code if sample_article else ""

    client = LLMClient()
    photo_agent = PhotoSearchAgent(llm_client=client)
    agent_res = await photo_agent.aexecute({
        "item_id": str(blueprint_id),
        "brand_name": brand_name,
        "vendor_code": vendor_code,
        "color": color_name,
        "article_name": bp.article_name,
        "description": bp.description or "",
        "user_feedback": user_feedback,
        "mode": "catalog",
    })

    prop = agent_res.get("proposal") or {}
    candidate_url = prop.get("photo_url")

    if not candidate_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nessuna foto valida trovata per questo articolo. Prova a inserire un URL manualmente."
        )

    return download_and_upsert_photo(db, blueprint_id, color_name, candidate_url, commit_changes=True)
