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

_MODEL = "gemini-3.1-flash-lite"

_SYSTEM_PROMPT_QUERY_GEN = (
    "Sei un esperto SEO e specialista nell'estrazione di immagini di prodotti e-commerce tramite motori di ricerca. "
    "Il tuo unico compito è generare la stringa di ricerca perfetta per trovare la foto di un prodotto, partendo dai suoi dati anagrafici.\n\n"
    "REGOLE TASSATIVE:\n"
    "1. Analizza i dettagli forniti e seleziona SOLO i termini essenziali per identificare il prodotto univocamente.\n"
    "2. DEVI SEMPRE INCLUDERE il Vendor Code (codice articolo) fornito, in quanto è l'identificativo primario per la ricerca.\n"
    "3. Seleziona il brand, il nome articolo e il colore di riferimento. Ignora descrizioni lunghe o concetti astratti.\n"
    "4. Aggiungi keyword di contesto come 'packshot', 'product shot' o 'white background' per favorire risultati da e-commerce puliti.\n"
    "5. Non usare alcun saluto, spiegazione o testo introduttivo.\n"
    "6. Non formattare la risposta in markdown (niente grassetto, niente virgolette o backticks).\n"
    "7. La risposta deve contenere ESCLUSIVAMENTE la stringa di ricerca grezza, pronta per essere immessa su Google."
)


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
        system_prompt=_SYSTEM_PROMPT_QUERY_GEN,
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
