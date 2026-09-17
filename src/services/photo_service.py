import httpx
from fastapi import HTTPException, status, Response
from sqlalchemy.orm import Session
from src.core.logger import get_logger
from src.repositories import staging_repo
from src.repositories.photo_repo import photo_repo
from src.models.pim import Brand
from src.schemas.data_ingestion import PhotoRetryRequest
from src.agents.llm_client import LLMClient
from src.agents.article_blueprints_agent.nodes.color_photo_search_node import (
    _extract_best_url,
    _validate_photo_url,
    _SYSTEM_PROMPT,
    _PHOTO_CONSTRAINTS,
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
        try:
            async with httpx.AsyncClient() as client:
                res = await client.head(request.new_url, timeout=5.0, follow_redirects=True)
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
        
    # Build feedback prompt
    ctx = proposal.get("_photo_search_ctx", [])
    feedback = f"Sbagliato. L'utente dice: "
    if not request.model_ok:
        feedback += f"Il modello/prodotto nell'immagine non è corretto. Devi cercare '{brand_name} {item.get('article_name', '')}'. "
    if not request.color_ok:
        feedback += f"Il colore dell'articolo nell'immagine non è corretto. Deve essere '{proposal.get('canonical_color_candidate')}'. "
    if request.user_feedback:
        feedback += f"Inoltre, l'utente ha fornito questo feedback aggiuntivo: '{request.user_feedback}'. "
        
    feedback += (
        f"Per favore, esegui una NUOVA ricerca su internet e trova un'immagine diversa che rispetti rigorosamente questi vincoli.\n"
        f"{_PHOTO_CONSTRAINTS}\n"
        f"Restituisci SOLO l'URL grezzo dell'immagine trovata. Se non trovi nessun risultato reale, rispondi: NULL"
    )
    
    client = LLMClient()
    new_ctx = list(ctx)
    new_ctx.append({"role": "user", "content": feedback})
    
    context_str = "\n".join(f"{msg.get('role')}: {msg.get('content')}" for msg in new_ctx)
    full_prompt = f"Previous context:\n{context_str}\n\nRitenta la ricerca."
    
    raw_res, extracted_urls = await client.call_with_grounding(
        model_name="gemini-3.1-flash-lite",
        system_prompt=_SYSTEM_PROMPT,
        prompt=full_prompt,
        pipeline_stage="Stage 5 - Photo Retry",
        temperature=0.4, # Slightly higher temperature for retry
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
    new_ctx.append({"role": "model", "content": raw_res_stripped})
    
    # Only overwrite photo_url if the new candidate passed the guard.
    # If the guard rejected the URL (candidate_url is None), preserve the
    # previous value so the user still sees the last accepted photo.
    if candidate_url is not None:
        proposal["photo_url"] = candidate_url
    proposal["retry_count"] = retry_count + 1
    proposal["_photo_search_ctx"] = new_ctx
    
    staging_repo.update_job_data(db, job_id, job.data)
    
    return proposal


def serve_photo(db: Session, photo_id: str) -> Response:
    photo = photo_repo.get_photo_by_id(db, photo_id)
    if not photo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found")
        
    # Content type should probably be inferred or hardcoded as generic image
    return Response(content=photo.photo_data, media_type="image/jpeg")


def download_and_save_photo(db: Session, blueprint_id: str, color_name: str, photo_url: str, commit_changes: bool = True):
    if not photo_url or photo_url.startswith("/api/"):
        return
        
    # Check if duplicate exists before downloading
    existing = photo_repo.get_photo_by_blueprint_and_color(db, blueprint_id, color_name)
    if existing:
        return
        
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            resp = client.get(photo_url)
            resp.raise_for_status()
            
            photo_repo.create_photo(db, blueprint_id, color_name, resp.content, commit_changes=commit_changes)
    except Exception as exc:
        logger.log_execution("photo_service", "download_failed", "warn", url=photo_url, exc=str(exc))
        raise ValueError(f"Failed to download image from {photo_url}: {exc}")
