"""
src/agents/photo_search_agent/nodes/query_gen_node.py
-----------------------------------------------------
Generazione 1-shot della query di ricerca fotografica per una tupla (Blueprint, color),
con prompt, temperature e pipeline_stage identici al comportamento precedente (HEAD).
"""
from __future__ import annotations

from typing import Optional
from src.agents.llm_client import LLMClient
from src.agents.photo_search_agent.state import PhotoSearchItemState
from src.utils.prompts import PHOTO_QUERY_GEN_SYSTEM_PROMPT

_MODEL = "gemini-3.1-flash-lite"


async def generate_query_node(state: PhotoSearchItemState, client: Optional[LLMClient] = None) -> dict:
    llm = client or LLMClient()

    bp_info = (
        f"Brand: {state.brand_name}\n"
        f"Article Name: {state.article_name}\n"
        f"Vendor Code: {state.vendor_code}\n"
        f"Color: {state.color}\n"
        f"Description: {state.description}"
    )

    new_working_ctx = list(state.previous_ctx)

    if state.mode == "retry":
        feedback = (
            f"Sbagliato. L'utente ha fornito questo feedback:\n'{state.user_feedback}'\n\n"
            f"Usa queste informazioni sull'articolo per generare una NUOVA query corretta:\n{bp_info}"
        )
        new_working_ctx.append({"role": "user", "content": feedback})
        context_str = "\n".join(f"{msg.get('role')}: {msg.get('content')}" for msg in new_working_ctx)
        prompt = f"Previous context:\n{context_str}\n\nGenera la query di ricerca."
        temp = 0.4
        stage_name = "Stage 5 - Photo Retry Query Gen"
    elif state.mode in ("catalog", "revision"):
        if state.user_feedback:
            prompt = (
                f"L'utente ha fornito questo feedback:\n'{state.user_feedback}'\n\n"
                f"Usa queste informazioni sull'articolo per generare una query di ricerca:\n{bp_info}"
            )
        else:
            prompt = f"Genera una query di ricerca Google mirata per trovare l'immagine del prodotto:\n{bp_info}"
        temp = 0.3
        stage_name = (
            "Catalog Photo Search - Query Gen"
            if state.mode == "catalog"
            else "Blueprint Revision - Photo Search Query Gen"
        )
        new_working_ctx.append({"role": "user", "content": prompt})
    else:
        prompt = (
            f"Genera la query di ricerca ottimizzata per trovare la foto di questo prodotto. "
            f"Devi assolutamente includere il Vendor Code nella stringa finale.\n\nDati Articolo:\n{bp_info}"
        )
        temp = 0.1
        stage_name = "Stage 5 - Photo Query Gen"
        new_working_ctx.append({"role": "user", "content": prompt})

    generated_query = await llm.call(
        model_name=_MODEL,
        system_prompt=PHOTO_QUERY_GEN_SYSTEM_PROMPT,
        prompt=prompt,
        pipeline_stage=stage_name,
        temperature=temp,
    )
    generated_query = (generated_query or "").strip()
    new_working_ctx.append({"role": "model", "content": generated_query})

    return {
        "current_query": generated_query,
        "working_ctx": new_working_ctx,
    }
