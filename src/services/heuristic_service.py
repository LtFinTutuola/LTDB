"""
heuristic_service.py
--------------------
Service layer orchestrating the heuristic deduction flow:
  - Accept and manage heuristic deduction jobs
  - Run DataExtractionAgent → CodesDeductionAgent pipeline
  - Confirm and persist heuristic to Brand record
  - Handle recalculation triggered by heuristic breaks
"""
import os
import uuid
import re
from typing import Dict, Any, Optional, List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.database import SessionLocal
from src.agents.base import AgentException
from src.agents.data_extraction_agent import DataExtractionAgent
from src.agents.codes_deduction_agent import CodesDeductionAgent
from src.models.pim import Brand, ArticleBlueprint
from src.models.wms import Article
from src.models.staging import JobStatus, JobType
from src.repositories import staging_repo
from src.core.logger import get_logger

logger = get_logger()


def accept_heuristic_job(db: Session, brand_id: str, file_path: str) -> str:
    """
    Validates brand and file, creates a staging job for heuristic deduction.
    Returns the job_id.
    """
    logger.log_execution("heuristic_service", "accept_heuristic_job_start", "ok",
                         brand_id=brand_id, file_path=file_path)

    # Validate brand exists
    brand = db.query(Brand).filter(Brand.id == brand_id).first()
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand ID '{brand_id}' not found in the database."
        )

    # Validate file exists and is PDF
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found on the server filesystem"
        )
    if not file_path.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only PDF files (.pdf) are supported."
        )

    # Check for existing in-progress heuristic job for this brand
    existing_job = staging_repo.get_job_by_status_and_path(
        db, status=JobStatus.ACCEPTED.value, file_path=file_path,
        job_type=JobType.HEURISTIC_DEDUCTION.value
    )
    if existing_job:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A heuristic deduction job for this file is already in progress."
        )

    job_id = str(uuid.uuid4())
    staging_repo.create_job(
        db, job_id=job_id,
        job_type=JobType.HEURISTIC_DEDUCTION.value,
        file_path=file_path
    )
    logger.log_execution("heuristic_service", "accept_heuristic_job_success", "ok", job_id=job_id)
    return job_id


async def process_heuristic(job_id: str, brand_id: str, file_path: str) -> None:
    """
    Background task: runs DataExtractionAgent → CodesDeductionAgent pipeline.
    Stages the result (regex, explanation, examples) without persisting to Brand.
    """
    with SessionLocal() as db:
        try:
            logger.log_execution("heuristic_service", "process_heuristic_start", "ok",
                                 job_id=job_id, brand_id=brand_id)

            brand_obj = db.query(Brand).filter(Brand.id == brand_id).first()
            if not brand_obj:
                raise AgentException(f"Brand ID '{brand_id}' not found in the database.")
            brand_name = brand_obj.name

            # Stage 1: Extract items from sample DDT
            extraction_agent = DataExtractionAgent()
            logger.log_execution("heuristic_service", "extraction_agent_dispatched", "ok")
            extraction_res = await extraction_agent.aexecute({
                "file_path": file_path,
                "brand": brand_name,
            })
            extracted_items = extraction_res["items"]

            # Extract vendor codes from items
            vendor_codes = _extract_vendor_codes(extracted_items)
            if not vendor_codes:
                raise AgentException(
                    message="No vendor codes found in the sample document. Cannot deduce heuristic.",
                    output={"items_count": len(extracted_items)},
                )

            # Stage 2: Run CodesDeductionAgent
            deduction_agent = CodesDeductionAgent()
            logger.log_execution("heuristic_service", "deduction_agent_dispatched", "ok",
                                 vendor_codes_count=len(vendor_codes))

            deduction_res = await deduction_agent.aexecute({
                "vendor_codes": vendor_codes,
                "brand_name": brand_name,
                "previous_explanation": "",
            })

            # Group the extracted items using the deduced regex
            regex_str = deduction_res.get("regex", "")
            compiled_regex = re.compile(regex_str) if regex_str else None
            
            grouped_items = {}
            for item in extracted_items:
                raw_code = item.get("vendor_code") or item.get("VendorCode") or ""
                raw_code = raw_code.strip().upper()
                
                key = raw_code
                if compiled_regex:
                    match = compiled_regex.search(raw_code)
                    if match and "color_code" in match.groupdict() and match.group("color_code") is not None:
                        key = raw_code[:match.start("color_code")] + "#" + raw_code[match.end("color_code"):]
                    else:
                        # If regex is provided but doesn't match color_code, it might be a valid non-colored variant
                        # but we fallback to raw_code to be safe.
                        pass
                        
                if key not in grouped_items:
                    grouped_items[key] = []
                # Ensure the item is serializable (it's typically a dict from extraction)
                item_dict = item if isinstance(item, dict) else item.model_dump()
                sanitized_item = {
                    k: v for k, v in item_dict.items()
                    if k == k.lower() and k != "item_id"
                }
                grouped_items[key].append(sanitized_item)

            # Stage result
            result = {
                "regex": deduction_res["regex"],
                "textual_explanation": deduction_res["explanation"],
                "grouped_items": grouped_items,
                "brand_id": brand_id,
                "vendor_codes_analyzed": len(vendor_codes),
            }
            staging_repo.update_job(
                db, job_id=job_id,
                status=JobStatus.COMPLETED.value,
                data=result
            )
            logger.log_execution("heuristic_service", "process_heuristic_success", "ok",
                                 job_id=job_id)

        except AgentException as exc:
            logger.log_execution("heuristic_service", "agent_exception", "err",
                                 job_id=job_id, exc=exc, output=exc.output)
            staging_repo.update_job(
                db, job_id=job_id,
                status=JobStatus.ERROR.value,
                data={"error": str(exc), "output": exc.output},
            )
            raise


def get_heuristic_job(db: Session, job_id: str) -> Dict[str, Any]:
    """Retrieves the heuristic deduction job data."""
    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found"
        )
    data = job.data.copy() if job.data else None
    if data and "regex" in data:
        data.pop("regex")
        
    return {
        "status": JobStatus(job.status).name,
        "data": data
    }


def confirm_heuristic(db: Session, job_id: str) -> None:
    """
    Confirms a completed heuristic deduction job.
    Persists regex, explanation, and heuristic_confirmed=True to the Brand record.
    Deletes the staging job.
    """
    logger.log_execution("heuristic_service", "confirm_heuristic_start", "ok",
                         job_id=job_id)

    job = staging_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found"
        )
    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job is not yet completed. Cannot confirm."
        )

    data = job.data or {}
    
    # Retrieve the correct brand_id from the job data (overriding the URL parameter)
    target_brand_id = data.get("brand_id")
    if not target_brand_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Staged heuristic data is missing the brand ID."
        )

    regex = data.get("regex")
    explanation = data.get("textual_explanation")

    # regex can be an empty string if no masking is needed
    if regex is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Staged heuristic data is missing the regex pattern."
        )

    # Update Brand record using the correct target_brand_id
    brand = db.query(Brand).filter(Brand.id == target_brand_id).first()
    if not brand:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Brand ID '{target_brand_id}' not found in the database."
        )

    from src.repositories.pim_repo import heuristic_repo
    heuristic_repo.create_heuristic(db, brand_id=target_brand_id, pattern=regex, explanation=explanation)

    # Clean up staging job
    try:
        staging_repo.delete_job(db, job_id)
    except Exception as exc:
        print(f"[heuristic_service] Warning: Failed to clean up staging job {job_id}: {exc}")

    logger.log_execution("heuristic_service", "confirm_heuristic_success", "ok",
                         brand_id=target_brand_id, job_id=job_id)


async def process_recalculation(brand_id: str, vendor_codes_corpus: List[str], previous_explanation: Optional[str]) -> None:
    """
    Background task: recalculates a brand's heuristic using the full historical
    vendor code corpus plus new breaking codes.
    Stages the result for operator confirmation.
    """
    with SessionLocal() as db:
        try:
            logger.log_execution("heuristic_service", "process_recalculation_start", "ok",
                                 brand_id=brand_id, corpus_size=len(vendor_codes_corpus))

            brand_obj = db.query(Brand).filter(Brand.id == brand_id).first()
            if not brand_obj:
                raise AgentException(f"Brand ID '{brand_id}' not found for recalculation.")

            # Create staging job for the recalculation
            job_id = str(uuid.uuid4())
            staging_repo.create_job(
                db, job_id=job_id,
                job_type=JobType.HEURISTIC_DEDUCTION.value,
            )

            deduction_agent = CodesDeductionAgent()
            deduction_res = await deduction_agent.aexecute({
                "vendor_codes": vendor_codes_corpus,
                "brand_name": brand_obj.name,
                "previous_explanation": previous_explanation,
            })

            result = {
                "regex": deduction_res["regex"],
                "explanation": deduction_res["explanation"],
                "examples": deduction_res["examples"],
                "brand_id": brand_id,
                "vendor_codes_analyzed": len(vendor_codes_corpus),
                "is_recalculation": True,
            }
            staging_repo.update_job(
                db, job_id=job_id,
                status=JobStatus.COMPLETED.value,
                data=result
            )
            logger.log_execution("heuristic_service", "process_recalculation_success", "ok",
                                 brand_id=brand_id, job_id=job_id)

        except AgentException as exc:
            logger.log_execution("heuristic_service", "recalculation_exception", "err",
                                 brand_id=brand_id, exc=exc)
            if 'job_id' in dir():
                staging_repo.update_job(
                    db, job_id=job_id,
                    status=JobStatus.ERROR.value,
                    data={"error": str(exc), "output": exc.output},
                )
            raise


def _extract_vendor_codes(items: list) -> List[str]:
    """Extract and deduplicate vendor codes from extracted items."""
    codes = []
    seen = set()
    for item in items:
        code = item.get("vendor_code") or item.get("VendorCode") or ""
        code = code.strip().upper()
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


async def process_single_shot_deduction(brand_id: str, raw_vendor_code: str, official_name: str) -> Optional[str]:
    """
    Background task: uses LLM to deduce the color-coding regex for a specific unmatched vendor code,
    based on its official web name. Instantly creates a BrandHeuristic.
    """
    with SessionLocal() as db:
        try:
            logger.log_execution("heuristic_service", "single_shot_start", "ok", brand_id=brand_id, raw_code=raw_vendor_code)
            brand_obj = db.query(Brand).filter(Brand.id == brand_id).first()
            if not brand_obj:
                return None

            from src.agents.llm_client import LLMClient
            import json
            
            client = LLMClient()
            system_prompt = (
                "Sei un esperto di codifiche di articoli per magazzini. "
                "Il tuo scopo è analizzare un codice fornitore (vendor code) e il nome/descrizione ufficiale del prodotto "
                "per capire se il codice contiene un'indicazione sul colore o sulla variante cromatica."
            )
            prompt = (
                f"Sto processando il vendor code '{raw_vendor_code}' del brand '{brand_obj.name}'.\n"
                f"Il nome/descrizione ufficiale trovato sul web per questo articolo è: '{official_name}'.\n\n"
                f"Analizza queste due informazioni ed effettua una ricerca sul web se necessario, per rispondere alla seguente domanda:\n"
                f"Ci sono dei caratteri all'interno del vendor code che indicano in modo specifico il colore o la variante cromatica del prodotto?\n\n"
                f"Se sì, deduci una Regular Expression compatibile con Python (re) che "
                f"CATTURI esplicitamente la parte di codice indicante il colore in un gruppo nominato `(?P<color_code>...)`.\n"
                f"Esempio: se il codice è 'ABC-123' e '123' indica il colore, la regex sarà `^(?P<model_code>.*)(?P<color_code>-[0-9]+)$`.\n\n"
                f"RISPONDI ESATTAMENTE CON UN JSON CON IL SEGUENTE FORMATO E NESSUN ALTRO TESTO (non formattare come markdown):\n"
                f"Se has_color è true:\n"
                f"{{\n"
                f"  \"has_color\": true,\n"
                f"  \"regex\": \"la_regex_dedotta\",\n"
                f"  \"explanation\": \"Fornisci una spiegazione ASTRATTA e GENERICA della regola, valida per l'intero Brand. È SEVERAMENTE VIETATO menzionare il codice specifico o il colore specifico forniti in input. Descrivi solo la struttura generale.\"\n"
                f"}}\n"
                f"Se has_color è false:\n"
                f"{{\n"
                f"  \"has_color\": false\n"
                f"}}"
            )
            
            raw_text = await client.call(
                model_name="gemini-3.1-flash-lite",
                system_prompt=system_prompt,
                prompt=prompt,
                pipeline_stage="Single Shot Deduction",
                temperature=0.0
            )
            
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                res_dict = json.loads(match.group(0))
                
                # Check if it has color or not
                has_color = res_dict.get("has_color", False)
                pattern = res_dict.get("regex", "") if has_color else ""
                explanation = res_dict.get("explanation") if has_color else "I vendor code di questo brand sono univoci per variante e non contengono suffissi o segmenti dedicati al colore."
                log_event = "single_shot_success" if has_color else "single_shot_no_color"

                from src.repositories.pim_repo import heuristic_repo
                heuristic_repo.create_heuristic(db, brand_id=brand_id, pattern=pattern, explanation=explanation)
                logger.log_execution("heuristic_service", log_event, "ok", brand_id=brand_id, pattern=pattern)
                return pattern
            else:
                logger.log_execution("heuristic_service", "single_shot_failed_parse", "err", brand_id=brand_id, text=raw_text)

        except Exception as exc:
            logger.log_execution("heuristic_service", "single_shot_exception", "err", brand_id=brand_id, exc=str(exc))

    return None

