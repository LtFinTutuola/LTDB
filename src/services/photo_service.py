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
from src.agents.article_blueprints_agent.nodes.color_photo_search_node import (
    _extract_best_url,
    _validate_photo_url,
    _SYSTEM_PROMPT_QUERY_GEN,
    _SYSTEM_PROMPT_SEARCH,
)

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
    
    if request.new_url:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
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
        
    brand_id = job.data.get("brand_id")
    brand = db.query(Brand).filter(Brand.id == brand_id).first()
    brand_name = brand.name if brand else ""
    
    item = next((it for it in job.data.get("items", []) if it.get("item_id") == request.item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item reference not found")
        
    # Get blueprint for context
    blueprints = job.data.get("blueprints", [])
    blueprint = next((bp for bp in blueprints if bp.get("id") == item.get("article_blueprint_id")), {})
    
    # Build feedback prompt
    ctx = proposal.get("_photo_search_ctx", [])
    
    bp_info = (
        f"Brand: {brand_name}\n"
        f"Article Name: {blueprint.get('article_name', item.get('article_name', ''))}\n"
        f"Vendor Code: {item.get('vendor_code', '')}\n"
        f"Color: {proposal.get('canonical_color_candidate', '')}\n"
        f"Description: {blueprint.get('description', '')}"
    )
    
    feedback = f"Sbagliato. L'utente ha fornito questo feedback:\n'{request.user_feedback}'\n\n"
    feedback += f"Usa queste informazioni sull'articolo per generare una NUOVA query corretta:\n{bp_info}"
    
    client = LLMClient()
    new_ctx = list(ctx)
    new_ctx.append({"role": "user", "content": feedback})
    
    context_str = "\n".join(f"{msg.get('role')}: {msg.get('content')}" for msg in new_ctx)
    full_prompt = f"Previous context:\n{context_str}\n\nGenera la query di ricerca."
    
    # Stage 1: Generate Query
    generated_query = await client.call(
        model_name="gemini-3.1-flash-lite",
        system_prompt=_SYSTEM_PROMPT_QUERY_GEN,
        prompt=full_prompt,
        pipeline_stage="Stage 5 - Photo Retry Query Gen",
        temperature=0.4, # Slightly higher temperature for retry
    )
    
    generated_query = generated_query.strip()
    
    # Stage 2: Grounded Search
    search_prompt = f"Esegui una ricerca su internet e trova una foto reale del prodotto usando questa query:\n{generated_query}"
    
    raw_res, extracted_urls = await client.call_with_grounding(
        model_name="gemini-3.1-flash-lite",
        system_prompt=_SYSTEM_PROMPT_SEARCH,
        prompt=search_prompt,
        pipeline_stage="Stage 5 - Photo Retry Grounded",
        temperature=0.2,
    )
    
    raw_res_stripped = raw_res.strip()
    
    # URL Guard: extract best candidate then validate it
    candidate_url = None if raw_res_stripped.upper() == "NULL" else _extract_best_url(extracted_urls, raw_res_stripped)
    if candidate_url:
        is_valid = await _validate_photo_url(candidate_url)
        if not is_valid:
            logger.log_execution("photo_service", "url_guard_rejected", "warn",
                                  rejected_url=candidate_url, item_id=request.item_id)
            candidate_url = None
    
    photo_url = candidate_url
    new_ctx.append({"role": "model", "content": generated_query})
    
    # Only overwrite photo_url if the new candidate passed the guard.
    # If the guard rejected the URL (candidate_url is None), preserve the
    # previous value so the user still sees the last accepted photo.
    if candidate_url is not None:
        proposal["photo_url"] = candidate_url
    proposal["retry_count"] = retry_count + 1
    proposal["_photo_search_ctx"] = new_ctx
    proposal["search_query"] = generated_query
    
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
    """Use Gemini grounded search to find an image for a catalog blueprint and save it."""
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

    bp_info = (
        f"Brand: {brand_name}\n"
        f"Article Name: {bp.article_name}\n"
        f"Vendor Code: {vendor_code}\n"
        f"Color: {color_name}\n"
        f"Description: {bp.description or ''}"
    )

    if user_feedback:
        prompt_content = f"L'utente ha fornito questo feedback:\n'{user_feedback}'\n\nUsa queste informazioni sull'articolo per generare una query di ricerca:\n{bp_info}"
    else:
        prompt_content = f"Genera una query di ricerca Google mirata per trovare l'immagine del prodotto:\n{bp_info}"

    client = LLMClient()
    generated_query = await client.call(
        model_name="gemini-3.1-flash-lite",
        system_prompt=_SYSTEM_PROMPT_QUERY_GEN,
        prompt=prompt_content,
        pipeline_stage="Catalog Photo Search - Query Gen",
        temperature=0.3,
    )
    generated_query = generated_query.strip()

    search_prompt = f"Esegui una ricerca su internet e trova una foto reale del prodotto usando questa query:\n{generated_query}"
    raw_res, extracted_urls = await client.call_with_grounding(
        model_name="gemini-3.1-flash-lite",
        system_prompt=_SYSTEM_PROMPT_SEARCH,
        prompt=search_prompt,
        pipeline_stage="Catalog Photo Search - Grounded",
        temperature=0.2,
    )

    raw_res_stripped = raw_res.strip()
    candidate_url = None if raw_res_stripped.upper() == "NULL" else _extract_best_url(extracted_urls, raw_res_stripped)
    if candidate_url:
        is_valid = await _validate_photo_url(candidate_url)
        if not is_valid:
            logger.log_execution("photo_service", "catalog_url_guard_rejected", "warn",
                                  rejected_url=candidate_url, blueprint_id=blueprint_id)
            candidate_url = None

    if not candidate_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nessuna foto valida trovata per questo articolo. Prova a inserire un URL manualmente."
        )

    return download_and_upsert_photo(db, blueprint_id, color_name, candidate_url, commit_changes=True)
